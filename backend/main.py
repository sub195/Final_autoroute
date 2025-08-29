import os
import sys
import json
import time
import datetime
from typing import Optional, Dict, Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
from azure.core.exceptions import ResourceNotFoundError

from config import container_client, translate_text
from auth import login_verify, decode_token
from request_context import set_context

# IMPORTANT: use the step runner (session-aware)
from graph import run_swarm_step

# --- Tracking (LLM + tools + pricing) ---
try:
    from tracking import (
        set_session as tracking_set_session,
        clear_session as tracking_clear_session,
        get_events_and_rollup as tracking_get_events_and_rollup,
    )
except Exception:
    # no-op fallbacks if tracking.py not present
    def tracking_set_session(session_key: str) -> None:  # type: ignore
        pass
    def tracking_clear_session(session_key: str) -> None:  # type: ignore
        pass
    def tracking_get_events_and_rollup(session_key: str) -> Dict[str, Any]:  # type: ignore
        return {"events": [], "llm_tokens_in": 0, "llm_tokens_out": 0}

current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

app = FastAPI(
    title="AutoRouteAI Swarm Backend",
    description="API for the AutoRouteAI multi-agent system.",
    version="1.0.0",
)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/echo")
def echo(msg: dict):
    return {"you_sent": msg}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten for prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- Models ----------------
class LoginRequest(BaseModel):
    customer_id: str
    password: str

class FeedbackRequest(BaseModel):
    session_id: str | None = None
    rating: int
    comment: str | None = None
    transcript: str | None = None

class ChatRequest(BaseModel):
    query: str
    lang: str = "en"
    session_id: Optional[str] = None   # <-- keep the same session across turns

# ------------- Simple in-memory sessions -------------
SESSIONS: Dict[str, Dict[str, Any]] = {}  # session_key -> {"state": {...}, "start_ts": float, "role": str, "customer_id": str}
MAX_HISTORY_MSGS = 30

def _get_or_create_session(session_id: Optional[str]) -> str:
    sid = session_id or str(uuid4())
    if sid not in SESSIONS:
        SESSIONS[sid] = {"state": None, "start_ts": time.time()}
    return sid

def _truncate_history(msgs: list) -> list:
    return msgs[-MAX_HISTORY_MSGS:]

def _session_key_from_claims(claims: Optional[dict], provided_session_id: Optional[str]) -> str:
    """
    Prefer explicit request.session_id, else derive from token, else 'anon'.
    Stable per user if token present.
    """
    if provided_session_id:
        return provided_session_id
    if not claims:
        return "anon"
    role = claims.get("role", "anonymous")
    cid = claims.get("customer_id")
    sid = claims.get("session_id") or f"{role}:{cid or 'anon'}"
    return sid

# ---- Pricing (env-configurable; defaults are placeholders) ----
try:
    PRICE_IN = float(os.getenv("AOAI_PRICE_INPUT_PER_1K", "0.01"))    # $/1K prompt tokens
    PRICE_OUT = float(os.getenv("AOAI_PRICE_OUTPUT_PER_1K", "0.03"))  # $/1K completion tokens
except Exception:
    PRICE_IN, PRICE_OUT = 0.01, 0.03

LOGGING_ENABLED = True  # toggle if needed

# ---------------- Helpers ----------------
def _append_feedback_to_blob(record: dict) -> tuple[str, str | None]:
    ts = datetime.datetime.utcnow().isoformat()
    fid = f"FB-{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S-%f')[:-3]}"
    csv_blob = "feedback/feedback_log.csv"

    transcript_blob = None
    if record.get("transcript"):
        transcript_blob = f"feedback/transcripts/{fid}.txt"
        container_client.upload_blob(
            name=transcript_blob,
            data=(record["transcript"] or "").encode("utf-8"),
            overwrite=True
        )

    try:
        existing = container_client.download_blob(csv_blob).readall().decode("utf-8")
    except ResourceNotFoundError:
        existing = "feedback_id,timestamp,role,customer_id,rating,comment,transcript_blob\n"

    role = (record.get("role") or "").replace(",", " ")
    cid  = (record.get("customer_id") or "").replace(",", " ")
    rating = str(record.get("rating") or "").strip()
    comment = (record.get("comment") or "").replace("\n", " ").replace("\r", " ").replace(",", " ")

    line = f"{fid},{ts},{role},{cid},{rating},{comment},{transcript_blob or ''}\n"
    updated = existing + line
    container_client.upload_blob(csv_blob, updated.encode("utf-8"), overwrite=True)
    return fid, transcript_blob

def _dump_messages_for_log(state_obj: Optional[Dict[str, Any]]) -> list[dict]:
    """
    Convert LangChain messages to a compact JSON-friendly list.
    state_obj is the 'state' returned by run_swarm_step: {"messages":[BaseMessage...], "summary": "..."}
    """
    msgs = []
    if not state_obj or not state_obj.get("messages"):
        return msgs
    for m in state_obj["messages"]:
        try:
            role = getattr(m, "type", None) or m.__class__.__name__
            name = getattr(m, "name", None)
            content = getattr(m, "content", "")
            msgs.append({"role": role, "name": name, "content": content})
        except Exception:
            msgs.append({"role": "Unknown", "content": str(m)})
    return msgs

def _flush_conversation_to_blob(session_key: str) -> tuple[bool, str | None]:
    """
    Writes the conversation JSON + tracking events to Blob and clears memory for this session.
    Returns (ok, blob_path_or_none)
    """
    if not LOGGING_ENABLED:
        SESSIONS.pop(session_key, None)
        tracking_clear_session(session_key)
        return True, None

    srec = SESSIONS.get(session_key)
    # tracking events + roll-up tokens (authoritative if present)
    tr = tracking_get_events_and_rollup(session_key)  # {"events":[], "llm_tokens_in": int, "llm_tokens_out": int}
    llm_in = int(tr.get("llm_tokens_in", 0))
    llm_out = int(tr.get("llm_tokens_out", 0))

    # If no state and no events, nothing to store
    if (not srec or not srec.get("state")) and not tr.get("events"):
        SESSIONS.pop(session_key, None)
        tracking_clear_session(session_key)
        return True, None

    # Extract messages & summary from graph state
    state_obj = srec.get("state") if srec else None
    msgs = _dump_messages_for_log(state_obj)
    summary = (state_obj or {}).get("summary")

    # Compute price from tracking LLM totals
    tokens_in = llm_in
    tokens_out = llm_out
    cost = (tokens_in / 1000.0) * PRICE_IN + (tokens_out / 1000.0) * PRICE_OUT

    payload = {
        "session_key": session_key,
        "role": srec.get("role") if srec else None,
        "customer_id": srec.get("customer_id") if srec else None,
        "start_ts": srec.get("start_ts") if srec else None,
        "end_ts": time.time(),
        "summary": summary,
        "messages": msgs,
        "events": tr.get("events", []),   # detailed LLM + tool events
        "llm_tokens_in": int(tokens_in),
        "llm_tokens_out": int(tokens_out),
        "approx_cost_usd": round(cost, 6),
    }

    dt = datetime.datetime.utcnow().strftime("%Y%m%d")
    name = f"conversations/{dt}/conv-{session_key}-{uuid4().hex}.json"
    try:
        container_client.upload_blob(name=name, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), overwrite=True)
        # clear memory after success
        SESSIONS.pop(session_key, None)
        tracking_clear_session(session_key)
        return True, name
    except Exception as e:
        print(f"[CHAT] LOG SAVE ERROR: {e}")
        return False, None

# ---------------- Routes ----------------
@app.post("/login")
def login(req: LoginRequest):
    try:
        result = login_verify(req.customer_id, req.password)
        if not result.get("ok"):
            raise HTTPException(status_code=401, detail=result.get("error","Invalid credentials"))
        return {"token": result["token"], "role": result["role"], "customer_id": result["customer_id"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/chat")
def chat(request: ChatRequest, authorization: Optional[str] = Header(default=None)):
    try:
        # 1) Auth context (RBAC)
        role, customer_id, claims = "customer", None, None
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization.split(" ", 1)[1].strip()
            claims = decode_token(token)
            if claims:
                role = claims.get("role", "customer")
                customer_id = claims.get("customer_id")
        set_context(role, customer_id)

        # 2) Session key (prefer explicit request.session_id, else derive from token, else anon)
        session_key = _session_key_from_claims(claims, request.session_id)
        if session_key not in SESSIONS:
            SESSIONS[session_key] = {"state": None, "start_ts": time.time(), "role": role, "customer_id": customer_id}

        # Tracking context: tie all LLM/tool events in graph to this session
        tracking_set_session(session_key)

        print(f"[CHAT] START role={role} cid={customer_id} sid={session_key} lang={request.lang} raw='{request.query[:200]}'")

        # 3) Translate in → en
        translated_query = translate_text(request.query, to_lang="en", from_lang=request.lang)
        print(f"[CHAT] translated='{translated_query[:200]}'")

        # 4) Run one conversational step with memory
        auth_ctx = {"role": role, "customer_id": customer_id} if customer_id else {"role": role}
        step = run_swarm_step(
            query=translated_query,
            auth=auth_ctx,
            prev_state=SESSIONS[session_key]["state"],  # may be None on first turn
            history_keep_last=12,
        )

        # 5) Save updated state back to session
        SESSIONS[session_key]["state"] = step.get("state")
        SESSIONS[session_key]["role"] = role
        SESSIONS[session_key]["customer_id"] = customer_id

        ai_reply_en = step.get("content", "")
        complaint_id = step.get("complaint_id")

        print(f"[CHAT] ai_reply_en='{ai_reply_en[:200]}'")

        # 6) Translate en → requested lang
        final_reply = translate_text(ai_reply_en, to_lang=request.lang, from_lang="en")
        print(f"[CHAT] DONE reply_lang={request.lang}")

        return {"response": final_reply, "complaint_id": complaint_id, "session_id": session_key}

    except Exception as e:
        print(f"[CHAT] ERROR: {e}")
        raise HTTPException(status_code=500, detail="An error occurred while processing your request.")

@app.post("/feedback")
def feedback(req: FeedbackRequest, authorization: Optional[str] = Header(default=None)):
    """
    Saves feedback CSV + transcript file (if provided),
    and also flushes the conversation (messages + LLM/tool events + pricing) to Blob.
    """
    try:
        role, customer_id, claims = "anonymous", None, None
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization.split(" ", 1)[1].strip()
            claims = decode_token(token)
            if claims:
                role = claims.get("role", "anonymous")
                customer_id = claims.get("customer_id")

        record = {
            "role": role,
            "customer_id": customer_id or "",
            "rating": req.rating,
            "comment": req.comment or "",
            "transcript": req.transcript or "",
        }
        fid, tblob = _append_feedback_to_blob(record)

        # Flush conversation (if a session_id is provided, else no-op)
        sid = req.session_id or _session_key_from_claims(claims, None)
        ok, conv_blob = _flush_conversation_to_blob(sid)

        msg = "conversation logged" if ok else "conversation log failed"
        return {"ok": True, "feedback_id": fid, "transcript_blob": tblob, "conversation_log": conv_blob, "message": msg}
    except Exception as e:
        print(f"[FEEDBACK] ERROR: {e}")
        raise HTTPException(status_code=500, detail="Failed to save feedback.")

@app.post("/chat/clear")
def clear_chat(session_id: Optional[str] = None, authorization: Optional[str] = Header(default=None)):
    """
    Explicit endpoint to end a session and log its conversation if present.
    Use when user starts a new chat without giving feedback.
    """
    try:
        claims = None
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization.split(" ", 1)[1].strip()
            claims = decode_token(token)

        sid = _session_key_from_claims(claims, session_id)
        ok, conv_blob = _flush_conversation_to_blob(sid)
        msg = "conversation logged" if ok else "conversation log failed"
        return {"ok": ok, "conversation_log": conv_blob, "message": msg}
    except Exception as e:
        print(f"[CLEAR] ERROR: {e}")
        raise HTTPException(status_code=500, detail="Failed to clear chat.")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)













