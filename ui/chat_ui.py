import os
import time
import requests
import streamlit as st
from langdetect import detect, LangDetectException
from dotenv import load_dotenv

# -----------------------------
# Load env (works locally & cloud)
# -----------------------------
load_dotenv("../backend/.env")
DEFAULT_BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")

# -----------------------------
# Page config & basic styling
# -----------------------------
st.set_page_config(
    page_title="AutoRouteAI Support",
    layout="centered",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      .stChatFloatingInputContainer { bottom: 1rem; }
      .stMarkdown { line-height: 1.45; }
      .status-ok { color: #0f9d58; font-weight: 600; }
      .status-bad { color: #d93025; font-weight: 600; }
      .status-warn { color: #f9ab00; font-weight: 600; }
      .small { font-size: 0.9rem; color: #6b7280; }
      .pill {
        display:inline-block; padding:0.15rem 0.5rem; border-radius:9999px;
        font-size:0.85rem; font-weight:600; background:#eef2ff; color:#3730a3;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

# -----------------------------
# Helpers
# -----------------------------
def detect_lang_safe(text: str) -> str:
    try:
        return detect(text) if text and text.strip() else "en"
    except LangDetectException:
        return "en"

def ping_health(url: str) -> tuple[bool, str]:
    """Ping /health quickly to show connection status."""
    try:
        r = requests.get(f"{url}/health", timeout=3)
        if r.status_code == 200:
            return True, "Backend reachable"
        return False, f"Unexpected health status: {r.status_code}"
    except Exception as e:
        return False, f"Health check failed: {e}"

def api_login(url: str, customer_id: str, password: str) -> tuple[bool, str | None, str | None, str | None]:
    """Call /login and return (ok, token, role, customer_id_or_none)."""
    try:
        r = requests.post(
            f"{url}/login",
            json={"customer_id": customer_id, "password": password},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            return True, data.get("token"), data.get("role"), data.get("customer_id")
        elif r.status_code == 401:
            return False, None, None, None
        else:
            return False, None, None, None
    except Exception:
        return False, None, None, None

def call_backend_chat(url: str, query: str, lang: str, token: str | None) -> tuple[bool, str, str | None]:
    """Call /chat and return (ok, message, complaint_id). Includes Authorization if token present."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.post(
            f"{url}/chat",
            json={"query": query, "lang": lang},
            timeout=120,
            headers=headers,
        )
        if resp.status_code == 200:
            data = resp.json()
            reply = data.get("response", "Sorry, got an unexpected response format.")
            complaint_id = data.get("complaint_id")
            return True, reply, complaint_id
        elif resp.status_code == 500:
            return False, "⚠️ Sorry, something went wrong while processing your request. Please try again.", None
        else:
            return False, f"⚠️ Unexpected error (HTTP {resp.status_code}). Please try again.", None
    except requests.exceptions.Timeout:
        return False, "⚠️ The AI service took too long to respond. Please try again.", None
    except requests.exceptions.ConnectionError as e:
        return False, f"⚠️ Could not connect to the AI service. Please ensure the backend is running. Details: {e}", None
    except Exception as e:
        return False, f"⚠️ An unexpected error occurred. Details: {e}", None

def call_backend_feedback(url: str, token: str | None, rating: int, comment: str, transcript: str) -> tuple[bool, str]:
    """POST feedback to /feedback. Returns (ok, message)."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        payload = {"rating": rating, "comment": comment, "transcript": transcript}
        resp = requests.post(f"{url}/feedback", json=payload, headers=headers, timeout=30)
        if resp.status_code == 200:
            fid = resp.json().get("feedback_id", "")
            return True, f"Thanks! Feedback saved (ID: {fid})."
        return False, f"⚠️ Couldn't save feedback (HTTP {resp.status_code})."
    except Exception as e:
        return False, f"⚠️ Error sending feedback: {e}"

def init_session():
    if "messages" not in st.session_state:
        st.session_state.messages = [
            {"role": "assistant", "content": "Hello! Please sign in to start a secure chat."}
        ]
    if "backend_url" not in st.session_state:
        st.session_state.backend_url = DEFAULT_BACKEND_URL
    if "last_health" not in st.session_state:
        st.session_state.last_health = (False, "Not checked")
    if "complaint_ids" not in st.session_state:
        st.session_state.complaint_ids = []
    # Auth session
    if "is_authenticated" not in st.session_state:
        st.session_state.is_authenticated = False
    if "token" not in st.session_state:
        st.session_state.token = None
    if "role" not in st.session_state:
        st.session_state.role = None
    if "customer_id" not in st.session_state:
        st.session_state.customer_id = None

def add_message(role: str, content: str):
    st.session_state.messages.append({"role": role, "content": content})

def build_transcript() -> str:
    """Convert current chat into a plain text transcript."""
    lines = []
    for m in st.session_state.messages:
        role = m.get("role", "assistant")
        text = m.get("content", "")
        lines.append(f"[{role}] {text}")
    return "\n".join(lines)

# -----------------------------
# Sidebar: Connection, Auth, Settings
# -----------------------------
init_session()

# toasting after rerun
if st.session_state.get("feedback_submitted"):
    st.toast("✅ Thanks for your feedback!")
    st.session_state["feedback_submitted"] = False


with st.sidebar:
    st.header("⚙️ Settings")

    be_url = st.text_input(
        "Backend URL",
        value=st.session_state.backend_url,
        help="FastAPI endpoint base URL (e.g., http://127.0.0.1:8000)",
        key="inp_backend_url",
    )
    if be_url != st.session_state.backend_url:
        st.session_state.backend_url = be_url.rstrip("/")

    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("🔁 Check Backend", key="btn_check_backend"):
            st.session_state.last_health = ping_health(st.session_state.backend_url)
            time.sleep(0.1)

    ok, msg = st.session_state.last_health
    st.markdown(
        f"**Status:** "
        + (f"<span class='status-ok'>● ONLINE</span>" if ok else f"<span class='status-bad'>● OFFLINE</span>")
        + f"<br><span class='small'>{msg}</span>",
        unsafe_allow_html=True,
    )

    st.divider()
    st.subheader("🔐 Sign in")

    if not st.session_state.is_authenticated:
        # Login form -> rerun on submit so UI flips to "Sign out"
        with st.form("login_form", clear_on_submit=True):
            login_cid = st.text_input("Customer ID", value="", autocomplete="username", key="login_cid")
            login_pwd = st.text_input("Password", value="", type="password", autocomplete="current-password", key="login_pwd")
            login_submit = st.form_submit_button("Sign in", use_container_width=True)
        if login_submit:
            success, token, role, cid = api_login(st.session_state.backend_url, (login_cid or "").strip(), login_pwd or "")
            if success and token:
                st.session_state.is_authenticated = True
                st.session_state.token = token
                st.session_state.role = role
                st.session_state.customer_id = cid
                # Update greeting if default
                if st.session_state.messages and "Please sign in" in st.session_state.messages[0]["content"]:
                    st.session_state.messages[0] = {"role": "assistant", "content": "You're signed in. How can I help you today?"}
                st.success("Signed in successfully.")
                st.rerun()  # flip to signed-in state instantly
            else:
                st.error("Invalid credentials.")
    else:
        st.markdown(
            f"**Signed in as:** <span class='pill'>{st.session_state.customer_id} · {st.session_state.role}</span>",
            unsafe_allow_html=True,
        )
        if st.button("Sign out", key="btn_signout", use_container_width=True):
            st.session_state.is_authenticated = False
            st.session_state.token = None
            st.session_state.role = None
            st.session_state.customer_id = None
            st.session_state.messages = [
                {"role": "assistant", "content": "Signed out. Please sign in to continue."}
            ]
            st.session_state.complaint_ids = []
            st.rerun()

    st.divider()
    if st.button("🗑️ Clear chat", key="btn_clear_chat"):
        st.session_state.messages = [
            {"role": "assistant", "content": "Chat cleared. How can I help you now?"}
        ]
        st.session_state.complaint_ids = []

    # Complaint IDs (this session)
    if st.session_state.complaint_ids:
        st.subheader("🧾 Complaint IDs (this session)")
        for cid in reversed(st.session_state.complaint_ids[-5:]):
            st.code(cid)

    # -----------------------------
    # End Chat & Feedback (with form + rerun)
    # -----------------------------
    
    st.divider()
    st.subheader("📝 End Chat & Feedback")

    can_feedback = st.session_state.is_authenticated and (len(st.session_state.messages) > 1)

    if not can_feedback:
        st.caption("Sign in and have a conversation to enable feedback.")
    else:
        # Feedback form
        with st.form("feedback_form", clear_on_submit=True):
            fb_rating = st.radio(
                "How helpful was this chat?",
                options=[5, 4, 3, 2, 1],
                index=0,
                key="fb_rating",  # widget key only
                format_func=lambda x: {
                    5: "⭐⭐⭐⭐⭐ Excellent",
                    4: "⭐⭐⭐⭐ Good",
                    3: "⭐⭐⭐ Okay",
                    2: "⭐⭐ Poor",
                    1: "⭐ Bad",
                }[x],
                horizontal=False,
            )
            fb_comment = st.text_area(
                "Anything to add? (optional)",
                key="fb_comment",  # widget key only
                placeholder="Tell us what worked or what didn’t…",
            )
            submit_fb = st.form_submit_button("📨 Submit feedback", use_container_width=True)

        # Independent reset button (not in form)
        reset_chat = st.button("🧹 Start new chat", key="btn_reset_chat", use_container_width=True)

        if submit_fb:
            tr = build_transcript()
            ok2, msg2 = call_backend_feedback(
                st.session_state.backend_url,
                st.session_state.token,
                fb_rating,
                fb_comment or "",
                tr,
            )
            if ok2:
                st.success(msg2)
                # Reset chat & complaints list — don't touch widget keys
                st.session_state.messages = [
                    {"role": "assistant", "content": "New chat started. How can I help you?"}
                ]
                st.session_state.complaint_ids = []
                # Marking feedback flag to show toast on next run
                st.session_state["feedback_submitted"] = True
                st.rerun()
            else:
                st.error(msg2)

        if reset_chat and not submit_fb:
            st.session_state.messages = [
                {"role": "assistant", "content": "New chat started. How can I help you?"}
            ]
            st.session_state.complaint_ids = []
            st.rerun()

# -----------------------------
# Header
# -----------------------------
st.title("🏦 Global Bank Support")
st.caption("Powered by a collaborative AI Agent Swarm (secure access)")

# -----------------------------
# Render chat history
# -----------------------------
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

# -----------------------------
# Main input
# -----------------------------
disabled_hint = None
if not st.session_state.is_authenticated:
    disabled_hint = "Please sign in to start chatting."

prompt = st.chat_input(
    disabled=not st.session_state.is_authenticated,
    placeholder=disabled_hint or "Ask about your account, cards, loans, or any other issue...",
)

if prompt:
    # Echo user
    add_message("user", prompt)
    with st.chat_message("user"):
        st.markdown(prompt)

    # Detect language
    user_lang = detect_lang_safe(prompt)

    # Assistant placeholder while processing
    with st.chat_message("assistant"):
        message_placeholder = st.empty()
        message_placeholder.markdown("_Thinking…_")

        # Quick health pre-check
        ok, _ = ping_health(st.session_state.backend_url)
        if not ok:
            ai_response = "⚠️ Backend looks offline. Please start the backend service or update the URL in the sidebar."
            message_placeholder.markdown(ai_response)
            add_message("assistant", ai_response)
        else:
            # Calling backend (includes Authorization header if logged in)
            success, result_text, complaint_id = call_backend_chat(
                st.session_state.backend_url, prompt, user_lang, st.session_state.token
            )

            # Show the backend reply (success or friendly error message)
            message_placeholder.markdown(result_text)
            add_message("assistant", result_text)

            # If escalation happens, showing complaint ID separately only if it's not already in the text
            if complaint_id and "Complaint ID:" not in result_text:
                st.info(f"**Complaint ID:** `{complaint_id}`")

            # Store complaint IDs for the sidebar list
            if complaint_id:
                st.session_state.complaint_ids.append(complaint_id)

            # rerun once to ensure Feedback section appears immediately after first reply
            st.rerun()

# hint
st.caption("Tip: When you’re done, use the Feedback section on the right to rate this chat.")



















# import os
# import time
# import requests
# import streamlit as st
# from langdetect import detect, LangDetectException
# from dotenv import load_dotenv

# # -----------------------------
# # Load env (works locally & cloud)
# # -----------------------------
# load_dotenv("../backend/.env")
# DEFAULT_BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")

# # -----------------------------
# # Page config & basic styling
# # -----------------------------
# st.set_page_config(
#     page_title="AutoRouteAI Support",
#     layout="centered",
#     initial_sidebar_state="expanded",
# )

# st.markdown(
#     """
#     <style>
#       .stChatFloatingInputContainer { bottom: 1rem; }
#       .stMarkdown { line-height: 1.45; }
#       .status-ok { color: #0f9d58; font-weight: 600; }
#       .status-bad { color: #d93025; font-weight: 600; }
#       .status-warn { color: #f9ab00; font-weight: 600; }
#       .small { font-size: 0.9rem; color: #6b7280; }
#       .pill {
#         display:inline-block; padding:0.15rem 0.5rem; border-radius:9999px;
#         font-size:0.85rem; font-weight:600; background:#eef2ff; color:#3730a3;
#       }
#     </style>
#     """,
#     unsafe_allow_html=True,
# )

# # -----------------------------
# # Helpers
# # -----------------------------
# def detect_lang_safe(text: str) -> str:
#     try:
#         return detect(text) if text and text.strip() else "en"
#     except LangDetectException:
#         return "en"

# def ping_health(url: str) -> tuple[bool, str]:
#     """Ping /health quickly to show connection status."""
#     try:
#         r = requests.get(f"{url}/health", timeout=3)
#         if r.status_code == 200:
#             return True, "Backend reachable"
#         return False, f"Unexpected health status: {r.status_code}"
#     except Exception as e:
#         return False, f"Health check failed: {e}"

# def api_login(url: str, customer_id: str, password: str) -> tuple[bool, str | None, str | None, str | None]:
#     """
#     Call /login and return (ok, token, role, customer_id_or_none)
#     """
#     try:
#         r = requests.post(
#             f"{url}/login",
#             json={"customer_id": customer_id, "password": password},
#             timeout=15,
#         )
#         if r.status_code == 200:
#             data = r.json()
#             return True, data.get("token"), data.get("role"), data.get("customer_id")
#         elif r.status_code == 401:
#             return False, None, None, None
#         else:
#             return False, None, None, None
#     except Exception:
#         return False, None, None, None

# def call_backend_chat(url: str, query: str, lang: str, token: str | None) -> tuple[bool, str, str | None]:
#     """
#     Call /chat and return (ok, message, complaint_id).
#     Includes Authorization header if token is provided.
#     """
#     headers = {}
#     if token:
#         headers["Authorization"] = f"Bearer {token}"
#     try:
#         resp = requests.post(
#             f"{url}/chat",
#             json={"query": query, "lang": lang},
#             timeout=120,
#             headers=headers,
#         )
#         if resp.status_code == 200:
#             data = resp.json()
#             reply = data.get("response", "Sorry, got an unexpected response format.")
#             complaint_id = data.get("complaint_id")
#             return True, reply, complaint_id
#         elif resp.status_code == 500:
#             return False, "⚠️ Sorry, something went wrong while processing your request. Please try again.", None
#         else:
#             return False, f"⚠️ Unexpected error (HTTP {resp.status_code}). Please try again.", None
#     except requests.exceptions.Timeout:
#         return False, "⚠️ The AI service took too long to respond. Please try again.", None
#     except requests.exceptions.ConnectionError as e:
#         return False, f"⚠️ Could not connect to the AI service. Please ensure the backend is running. Details: {e}", None
#     except Exception as e:
#         return False, f"⚠️ An unexpected error occurred. Details: {e}", None

# def call_backend_feedback(url: str, token: str | None, rating: int, comment: str, transcript: str) -> tuple[bool, str]:
#     """
#     POST feedback to /feedback. Returns (ok, message).
#     """
#     headers = {}
#     if token:
#         headers["Authorization"] = f"Bearer {token}"
#     try:
#         payload = {
#             "rating": rating,
#             "comment": comment,
#             "transcript": transcript,
#         }
#         resp = requests.post(f"{url}/feedback", json=payload, headers=headers, timeout=30)
#         if resp.status_code == 200:
#             fid = resp.json().get("feedback_id", "")
#             return True, f"Thanks! Feedback saved (ID: {fid})."
#         return False, f"⚠️ Couldn't save feedback (HTTP {resp.status_code})."
#     except Exception as e:
#         return False, f"⚠️ Error sending feedback: {e}"

# def init_session():
#     if "messages" not in st.session_state:
#         st.session_state.messages = [
#             {"role": "assistant", "content": "Hello! Please sign in to start a secure chat."}
#         ]
#     if "backend_url" not in st.session_state:
#         st.session_state.backend_url = DEFAULT_BACKEND_URL
#     if "last_health" not in st.session_state:
#         st.session_state.last_health = (False, "Not checked")
#     if "complaint_ids" not in st.session_state:
#         st.session_state.complaint_ids = []
#     # Auth session
#     if "is_authenticated" not in st.session_state:
#         st.session_state.is_authenticated = False
#     if "token" not in st.session_state:
#         st.session_state.token = None
#     if "role" not in st.session_state:
#         st.session_state.role = None
#     if "customer_id" not in st.session_state:
#         st.session_state.customer_id = None

# def add_message(role: str, content: str):
#     st.session_state.messages.append({"role": role, "content": content})

# def build_transcript() -> str:
#     """
#     Convert the current chat messages in session_state into a plain text transcript.
#     """
#     lines = []
#     for m in st.session_state.messages:
#         role = m.get("role", "assistant")
#         text = m.get("content", "")
#         lines.append(f"[{role}] {text}")
#     return "\n".join(lines)

# # -----------------------------
# # Sidebar: Connection, Auth, Settings
# # -----------------------------
# init_session()

# with st.sidebar:
#     st.header("⚙️ Settings")

#     be_url = st.text_input(
#         "Backend URL",
#         value=st.session_state.backend_url,
#         help="FastAPI endpoint base URL (e.g., http://127.0.0.1:8000)",
#     )
#     if be_url != st.session_state.backend_url:
#         st.session_state.backend_url = be_url.rstrip("/")

#     col1, col2 = st.columns([1, 1])
#     with col1:
#         if st.button("🔁 Check Backend"):
#             st.session_state.last_health = ping_health(st.session_state.backend_url)
#             time.sleep(0.1)

#     ok, msg = st.session_state.last_health
#     st.markdown(
#         f"**Status:** "
#         + (f"<span class='status-ok'>● ONLINE</span>" if ok else f"<span class='status-bad'>● OFFLINE</span>")
#         + f"<br><span class='small'>{msg}</span>",
#         unsafe_allow_html=True,
#     )

#     st.divider()
#     st.subheader("🔐 Sign in")

#     if not st.session_state.is_authenticated:
#         login_cid = st.text_input("Customer ID", value="", autocomplete="username")
#         login_pwd = st.text_input("Password", value="", type="password", autocomplete="current-password")
#         if st.button("Sign in"):
#             success, token, role, cid = api_login(st.session_state.backend_url, login_cid.strip(), login_pwd)
#             if success and token:
#                 st.session_state.is_authenticated = True
#                 st.session_state.token = token
#                 st.session_state.role = role
#                 st.session_state.customer_id = cid
#                 st.success("Signed in successfully.")
#                 if st.session_state.messages and "Please sign in" in st.session_state.messages[0]["content"]:
#                     st.session_state.messages[0] = {"role": "assistant", "content": "You're signed in. How can I help you today?"}
#             else:
#                 st.error("Invalid credentials.")
#     else:
#         st.markdown(
#             f"**Signed in as:** <span class='pill'>{st.session_state.customer_id} · {st.session_state.role}</span>",
#             unsafe_allow_html=True,
#         )
#         if st.button("Sign out"):
#             st.session_state.is_authenticated = False
#             st.session_state.token = None
#             st.session_state.role = None
#             st.session_state.customer_id = None
#             st.session_state.messages = [
#                 {"role": "assistant", "content": "Signed out. Please sign in to continue."}
#             ]
#             st.session_state.complaint_ids = []

#     st.divider()
#     if st.button("🗑️ Clear chat"):
#         st.session_state.messages = [
#             {"role": "assistant", "content": "Chat cleared. How can I help you now?"}
#         ]
#         st.session_state.complaint_ids = []

#     # Complaint IDs (this session)
#     if st.session_state.complaint_ids:
#         st.subheader("🧾 Complaint IDs (this session)")
#         for cid in reversed(st.session_state.complaint_ids[-5:]):
#             st.code(cid)

#     # -----------------------------
#     # New: End Chat & Feedback
#     # -----------------------------
#     st.divider()
#     st.subheader("📝 End Chat & Feedback")

#     can_feedback = st.session_state.is_authenticated and (len(st.session_state.messages) > 1)
#     if not can_feedback:
#         st.caption("Sign in and have a conversation to enable feedback.")
#     else:
#         rating = st.radio(
#             "How helpful was this chat?",
#             options=[5,4,3,2,1],
#             format_func=lambda x: {5:"⭐⭐⭐⭐⭐ Excellent",4:"⭐⭐⭐⭐ Good",3:"⭐⭐⭐ Okay",2:"⭐⭐ Poor",1:"⭐ Bad"}[x],
#             index=0,
#         )
#         comment = st.text_area("Anything to add? (optional)", placeholder="Tell us what worked or what didn’t…")

#         colf1, colf2 = st.columns([1,1])
#         with colf1:
#             submit_fb = st.button("📨 Submit feedback", use_container_width=True)
#         with colf2:
#             reset_chat = st.button("🧹 Start new chat", use_container_width=True)

#         if submit_fb:
#             tr = build_transcript()
#             ok2, msg2 = call_backend_feedback(
#                 st.session_state.backend_url,
#                 st.session_state.token,
#                 rating,
#                 comment,
#                 tr
#             )
#             if ok2:
#                 st.success(msg2)
#                 st.session_state.messages = [{"role": "assistant", "content": "New chat started. How can I help you?"}]
#                 st.session_state.complaint_ids = []
#             else:
#                 st.error(msg2)

#         if reset_chat and not submit_fb:
#             st.session_state.messages = [{"role": "assistant", "content": "New chat started. How can I help you?"}]
#             st.session_state.complaint_ids = []
#             st.info("Chat has been reset.")

# # -----------------------------
# # Header
# # -----------------------------
# st.title("🏦 Global Bank Support")
# st.caption("Powered by a collaborative AI Agent Swarm (secure access)")

# # -----------------------------
# # Render chat history
# # -----------------------------
# for m in st.session_state.messages:
#     with st.chat_message(m["role"]):
#         st.markdown(m["content"])

# # -----------------------------
# # Main input
# # -----------------------------
# disabled_hint = None
# if not st.session_state.is_authenticated:
#     disabled_hint = "Please sign in to start chatting."

# prompt = st.chat_input(
#     disabled=not st.session_state.is_authenticated,
#     placeholder=disabled_hint or "Ask about your account, cards, loans, or any other issue...",
# )

# if prompt:
#     # Echo user
#     add_message("user", prompt)
#     with st.chat_message("user"):
#         st.markdown(prompt)

#     # Detect language
#     user_lang = detect_lang_safe(prompt)

#     # Assistant placeholder while processing
#     with st.chat_message("assistant"):
#         message_placeholder = st.empty()
#         message_placeholder.markdown("_Thinking…_")

#         # Quick health pre-check
#         ok, _ = ping_health(st.session_state.backend_url)
#         if not ok:
#             ai_response = "⚠️ Backend looks offline. Please start the backend service or update the URL in the sidebar."
#             message_placeholder.markdown(ai_response)
#             add_message("assistant", ai_response)
#         else:
#             # Call backend (includes Authorization header if logged in)
#             success, result_text, complaint_id = call_backend_chat(
#                 st.session_state.backend_url, prompt, user_lang, st.session_state.token
#             )

#             # Show the backend reply (success or friendly error message)
#             message_placeholder.markdown(result_text)
#             add_message("assistant", result_text)

#             # If escalation happened, show complaint ID separately only if it's not already in the text
#             if complaint_id and "Complaint ID:" not in result_text:
#                 st.info(f"**Complaint ID:** `{complaint_id}`")

#             # Store complaint IDs for the sidebar list
#             if complaint_id:
#                 st.session_state.complaint_ids.append(complaint_id)

# # Optional hint
# st.caption("Tip: When you’re done, use the Feedback section on the right to rate this chat.")






# import os
# import time
# import requests
# import streamlit as st
# from langdetect import detect, LangDetectException
# from dotenv import load_dotenv

# # -----------------------------
# # Load env (works locally & cloud)
# # -----------------------------
# load_dotenv("../backend/.env")
# DEFAULT_BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")

# # -----------------------------
# # Page config & basic styling
# # -----------------------------
# st.set_page_config(
#     page_title="AutoRouteAI Support",
#     layout="centered",
#     initial_sidebar_state="expanded",
# )

# st.markdown(
#     """
#     <style>
#       .stChatFloatingInputContainer { bottom: 1rem; }
#       .stMarkdown { line-height: 1.45; }
#       .status-ok { color: #0f9d58; font-weight: 600; }
#       .status-bad { color: #d93025; font-weight: 600; }
#       .status-warn { color: #f9ab00; font-weight: 600; }
#       .small { font-size: 0.9rem; color: #6b7280; }
#       .pill {
#         display:inline-block; padding:0.15rem 0.5rem; border-radius:9999px;
#         font-size:0.85rem; font-weight:600; background:#eef2ff; color:#3730a3;
#       }
#     </style>
#     """,
#     unsafe_allow_html=True,
# )

# # -----------------------------
# # Helpers
# # -----------------------------
# def detect_lang_safe(text: str) -> str:
#     try:
#         return detect(text) if text and text.strip() else "en"
#     except LangDetectException:
#         return "en"

# def ping_health(url: str) -> tuple[bool, str]:
#     """Ping /health quickly to show connection status."""
#     try:
#         r = requests.get(f"{url}/health", timeout=3)
#         if r.status_code == 200:
#             return True, "Backend reachable"
#         return False, f"Unexpected health status: {r.status_code}"
#     except Exception as e:
#         return False, f"Health check failed: {e}"

# def api_login(url: str, customer_id: str, password: str) -> tuple[bool, str | None, str | None, str | None]:
#     """
#     Call /login and return (ok, token, role, customer_id_or_none)
#     """
#     try:
#         r = requests.post(
#             f"{url}/login",
#             json={"customer_id": customer_id, "password": password},
#             timeout=15,
#         )
#         if r.status_code == 200:
#             data = r.json()
#             return True, data.get("token"), data.get("role"), data.get("customer_id")
#         elif r.status_code == 401:
#             return False, None, None, None
#         else:
#             return False, None, None, None
#     except Exception:
#         return False, None, None, None

# def call_backend_chat(url: str, query: str, lang: str, token: str | None) -> tuple[bool, str, str | None]:
#     """
#     Call /chat and return (ok, message, complaint_id).
#     Includes Authorization header if token is provided.
#     """
#     headers = {}
#     if token:
#         headers["Authorization"] = f"Bearer {token}"
#     try:
#         resp = requests.post(
#             f"{url}/chat",
#             json={"query": query, "lang": lang},
#             timeout=120,
#             headers=headers,
#         )
#         if resp.status_code == 200:
#             data = resp.json()
#             reply = data.get("response", "Sorry, got an unexpected response format.")
#             complaint_id = data.get("complaint_id")
#             return True, reply, complaint_id
#         elif resp.status_code == 500:
#             return False, "⚠️ Sorry, something went wrong while processing your request. Please try again.", None
#         else:
#             return False, f"⚠️ Unexpected error (HTTP {resp.status_code}). Please try again.", None
#     except requests.exceptions.Timeout:
#         return False, "⚠️ The AI service took too long to respond. Please try again.", None
#     except requests.exceptions.ConnectionError as e:
#         return False, f"⚠️ Could not connect to the AI service. Please ensure the backend is running. Details: {e}", None
#     except Exception as e:
#         return False, f"⚠️ An unexpected error occurred. Details: {e}", None
    
# # a feedback API helper
# def call_backend_feedback(url: str, token: str | None, rating: int, comment: str, transcript: str) -> tuple[bool, str]:
#     headers = {}
#     if token:
#         headers["Authorization"] = f"Bearer {token}"
#     try:
#         payload = {
#             "rating": rating,
#             "comment": comment,
#             "transcript": transcript,
#         }
#         resp = requests.post(f"{url}/feedback", json=payload, headers=headers, timeout=30)
#         if resp.status_code == 200:
#             fid = resp.json().get("feedback_id", "")
#             return True, f"Thanks! Feedback saved (ID: {fid})."
#         return False, f"⚠️ Couldn't save feedback (HTTP {resp.status_code})."
#     except Exception as e:
#         return False, f"⚠️ Error sending feedback: {e}"




# def init_session():
#     if "messages" not in st.session_state:
#         st.session_state.messages = [
#             {"role": "assistant", "content": "Hello! Please sign in to start a secure chat."}
#         ]
#     if "backend_url" not in st.session_state:
#         st.session_state.backend_url = DEFAULT_BACKEND_URL
#     if "last_health" not in st.session_state:
#         st.session_state.last_health = (False, "Not checked")
#     if "complaint_ids" not in st.session_state:
#         st.session_state.complaint_ids = []
#     # Auth session
#     if "is_authenticated" not in st.session_state:
#         st.session_state.is_authenticated = False
#     if "token" not in st.session_state:
#         st.session_state.token = None
#     if "role" not in st.session_state:
#         st.session_state.role = None
#     if "customer_id" not in st.session_state:
#         st.session_state.customer_id = None

# def add_message(role: str, content: str):
#     st.session_state.messages.append({"role": role, "content": content})


# def build_transcript() -> str:
#     """
#     Convert the current chat messages in session_state into a plain text transcript.
#     """
#     lines = []
#     for m in st.session_state.messages:
#         role = m.get("role", "assistant")
#         text = m.get("content", "")
#         lines.append(f"[{role}] {text}")
#     return "\n".join(lines)

# # -----------------------------
# # Sidebar: Connection, Auth, Settings
# # -----------------------------
# init_session()

# with st.sidebar:
#     st.header("⚙️ Settings")

#     be_url = st.text_input(
#         "Backend URL",
#         value=st.session_state.backend_url,
#         help="FastAPI endpoint base URL (e.g., http://127.0.0.1:8000)",
#     )
#     if be_url != st.session_state.backend_url:
#         st.session_state.backend_url = be_url.rstrip("/")

#     col1, col2 = st.columns([1, 1])
#     with col1:
#         if st.button("🔁 Check Backend"):
#             st.session_state.last_health = ping_health(st.session_state.backend_url)
#             time.sleep(0.1)

#     ok, msg = st.session_state.last_health
#     st.markdown(
#         f"**Status:** "
#         + (f"<span class='status-ok'>● ONLINE</span>" if ok else f"<span class='status-bad'>● OFFLINE</span>")
#         + f"<br><span class='small'>{msg}</span>",
#         unsafe_allow_html=True,
#     )

#     st.divider()
#     st.subheader("🔐 Sign in")

#     if not st.session_state.is_authenticated:
#         login_cid = st.text_input("Customer ID", value="", autocomplete="username")
#         login_pwd = st.text_input("Password", value="", type="password", autocomplete="current-password")
#         if st.button("Sign in"):
#             success, token, role, cid = api_login(st.session_state.backend_url, login_cid.strip(), login_pwd)
#             if success and token:
#                 st.session_state.is_authenticated = True
#                 st.session_state.token = token
#                 st.session_state.role = role
#                 st.session_state.customer_id = cid
#                 st.success("Signed in successfully.")
#                 # First message update if default greeting
#                 if st.session_state.messages and "Please sign in" in st.session_state.messages[0]["content"]:
#                     st.session_state.messages[0] = {"role": "assistant", "content": "You're signed in. How can I help you today?"}
#             else:
#                 st.error("Invalid credentials.")
#     else:
#         st.markdown(
#             f"**Signed in as:** <span class='pill'>{st.session_state.customer_id} · {st.session_state.role}</span>",
#             unsafe_allow_html=True,
#         )
#         if st.button("Sign out"):
#             st.session_state.is_authenticated = False
#             st.session_state.token = None
#             st.session_state.role = None
#             st.session_state.customer_id = None
#             st.session_state.messages = [
#                 {"role": "assistant", "content": "Signed out. Please sign in to continue."}
#             ]
#             st.session_state.complaint_ids = []

#     st.divider()
#     if st.button("🗑️ Clear chat"):
#         st.session_state.messages = [
#             {"role": "assistant", "content": "Chat cleared. How can I help you now?"}
#         ]
#         st.session_state.complaint_ids = []

#     # Complaint IDs (this session)
#     if st.session_state.complaint_ids:
#         st.subheader("🧾 Complaint IDs (this session)")
#         for cid in reversed(st.session_state.complaint_ids[-5:]):
#             st.code(cid)

# # feedback
#     st.divider()
#     st.subheader("📝 End Chat & Feedback")

#     can_feedback = st.session_state.is_authenticated and (len(st.session_state.messages) > 1)
#     if not can_feedback:
#         st.caption("Sign in and have a conversation to enable feedback.")
#     else:
#         rating = st.radio(
#             "How helpful was this chat?",
#             options=[5,4,3,2,1],
#             format_func=lambda x: {5:"⭐⭐⭐⭐⭐ Excellent",4:"⭐⭐⭐⭐ Good",3:"⭐⭐⭐ Okay",2:"⭐⭐ Poor",1:"⭐ Bad"}[x],
#             index=0,
#         )
#         comment = st.text_area("Anything to add? (optional)", placeholder="Tell us what worked or what didn’t…")

#         colf1, colf2 = st.columns([1,1])
#         with colf1:
#             submit_fb = st.button("📨 Submit feedback", use_container_width=True)
#         with colf2:
#             reset_chat = st.button("🧹 Start new chat", use_container_width=True)

#         if submit_fb:
#             tr = build_transcript()
#             ok, msg = call_backend_feedback(
#                 st.session_state.backend_url,
#                 st.session_state.token,
#                 rating,
#                 comment,
#                 tr
#             )
#             if ok:
#                 st.success(msg)
#                 # Reset chat after feedback
#                 st.session_state.messages = [{"role": "assistant", "content": "New chat started. How can I help you?"}]
#                 st.session_state.complaint_ids = []
#             else:
#                 st.error(msg)

#         if reset_chat and not submit_fb:
#             st.session_state.messages = [{"role": "assistant", "content": "New chat started. How can I help you?"}]
#             st.session_state.complaint_ids = []
#             st.info("Chat has been reset.")


# # -----------------------------
# # Header
# # -----------------------------
# st.title("🏦 Global Bank Support")
# st.caption("Powered by a collaborative AI Agent Swarm (secure access)")

# # -----------------------------
# # Render chat history
# # -----------------------------
# for m in st.session_state.messages:
#     with st.chat_message(m["role"]):
#         st.markdown(m["content"])

# # -----------------------------
# # Main input
# # -----------------------------
# disabled_hint = None
# if not st.session_state.is_authenticated:
#     disabled_hint = "Please sign in to start chatting."

# prompt = st.chat_input(
#     disabled=not st.session_state.is_authenticated,
#     placeholder=disabled_hint or "Ask about your account, cards, loans, or any other issue...",
# )

# if prompt:
#     # Echo user
#     add_message("user", prompt)
#     with st.chat_message("user"):
#         st.markdown(prompt)

#     # Detect language
#     user_lang = detect_lang_safe(prompt)

#     # Assistant placeholder while processing
#     with st.chat_message("assistant"):
#         message_placeholder = st.empty()
#         message_placeholder.markdown("_Thinking…_")

#         # Quick health pre-check
#         ok, _ = ping_health(st.session_state.backend_url)
#         if not ok:
#             ai_response = "⚠️ Backend looks offline. Please start the backend service or update the URL in the sidebar."
#             message_placeholder.markdown(ai_response)
#             add_message("assistant", ai_response)
#         else:
#             # Call backend (includes Authorization header if logged in)
#             success, result_text, complaint_id = call_backend_chat(
#                 st.session_state.backend_url, prompt, user_lang, st.session_state.token
#             )

#             # Show the backend reply (success or friendly error message)
#             message_placeholder.markdown(result_text)
#             add_message("assistant", result_text)

#             # If escalation happened, show complaint ID separately only if it's not already in the text
#             if complaint_id and "Complaint ID:" not in result_text:
#                 st.info(f"**Complaint ID:** `{complaint_id}`")

#             # Store complaint IDs for the sidebar list
#             if complaint_id:
#                 st.session_state.complaint_ids.append(complaint_id)















