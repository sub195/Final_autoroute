# backend/graph.py
"""
AutoRouteAI multi-agent workflow (local tools only):
- Deterministic local LLM classifier (no MCP)
- Azure Content Safety gate (optional)
- Dual-mode Complaints agent: info vs explicit human escalation
- Account_Info agent uses get_account_info tool (RBAC via AUTH_CONTEXT; SQL under the hood)
- All other specialists use search_knowledge_base with domain tags
- Conversational memory entrypoint: run_swarm_step (trimmed rolling history + summary)
- Flexible tracking hooks for LLM & tool calls (supports multiple signatures)
"""

import os
import time
import inspect
import datetime
import random
import string
from typing import TypedDict, Annotated, List, Optional, Dict, Any

from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, AIMessage, ToolMessage
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolExecutor
from langgraph.prebuilt.tool_executor import ToolInvocation
from langchain_core.tools import StructuredTool
from azure.core.exceptions import ResourceNotFoundError

from config import llm, container_client
from prompts import AGENT_SYSTEM_PROMPTS
from tools.knowledge_base import search_knowledge_base
from tools.account_info import get_account_info

# === Optional Content Safety
try:
    from config import safety_client
    from azure.ai.contentsafety.models import AnalyzeTextOptions
except Exception:
    safety_client = None
    AnalyzeTextOptions = None

# -------------------------------------------------------------------
# Support contact (from .env )
# -------------------------------------------------------------------
SUPPORT_PHONE = os.getenv("SUPPORT_PHONE", "1800-000-0000")
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "support@yourbank.example")
SUPPORT_LIVECHAT = os.getenv("SUPPORT_LIVECHAT_URL", "https://yourbank.example/support/chat")
COMPLAINT_PREFIX = os.getenv("COMPLAINT_PREFIX", "CMP")

# Agent registry (names must match prompts + classifier)
AGENT_DOMAINS = [
    ("Moderation_Agent", None),
    ("Out_Of_Scope_Agent", None),
    ("Complaints_Escalations_Agent", "complaints"),
    ("Account_Info_Agent", "account_info"),
    ("Account_Management_Agent", "account_management"),
    ("Loan_EMI_Agent", "loans_EMI"),
    ("Digital_Banking_Agent", "digital_banking"),
    ("Cards_Agent", "cards"),
    ("Regulatory_Compliance_Agent", "regulatory_compliance"),
]
VALID_AGENT_NAMES = [name for name, _ in AGENT_DOMAINS]

# -------------------------------------------------------------------
# Optional tracking (flexible, signature-aware)
# -------------------------------------------------------------------
try:
    import tracking as _trk  # your module if present
    _ADD_LLM = getattr(_trk, "add_llm_event", None)
    _ADD_TOOL = getattr(_trk, "add_tool_event", None)
except Exception:
    _ADD_LLM = None
    _ADD_TOOL = None

def _safe_add_llm_event(
    *,
    label: str,
    prompt_messages: list,
    response_message: Any | None,
    tools_bound: list | None,
    started_at: float,
    ended_at: float,
    ok: bool,
    input_tokens: int = 0,
    output_tokens: int = 0,
    error: str | None = None,
):
    """Call user-provided tracking.add_llm_event regardless of its expected shape."""
    if not _ADD_LLM:
        return
    try:
        sig = inspect.signature(_ADD_LLM)
        params = list(sig.parameters.values())

        # If function looks flexible (**kwargs / single-arg),we pass kwargs
        if any(p.kind == p.VAR_KEYWORD for p in params) or len(params) <= 1:
            _ADD_LLM(
                label=label,
                prompt_messages=prompt_messages,
                response_message=response_message,
                tools_bound=tools_bound or [],
                started_at=started_at,
                ended_at=ended_at,
                ok=ok,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                error=error,
            )
            return

        # Classic positional: (prompt_messages, response_message, started_at, ended_at, **extras?)
        if len(params) >= 4:
            if any(p.kind == p.VAR_KEYWORD for p in params):
                _ADD_LLM(
                    prompt_messages,
                    response_message,
                    started_at,
                    ended_at,
                    label=label,
                    tools_bound=tools_bound or [],
                    ok=ok,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    error=error,
                )
            else:
                _ADD_LLM(prompt_messages, response_message, started_at, ended_at)
            return

        # Fallback: try kw dict
        _ADD_LLM(
            label=label,
            prompt_messages=prompt_messages,
            response_message=response_message,
            tools_bound=tools_bound or [],
            started_at=started_at,
            ended_at=ended_at,
            ok=ok,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error=error,
        )
    except Exception:
        pass  # never break flow for tracking

def _safe_add_tool_event(
    *,
    tool: str,
    args: dict,
    started_at: float,
    ended_at: float,
    ok: bool,
    output_preview_len: int = 0,
    error: str | None = None,
):
    if not _ADD_TOOL:
        return
    try:
        sig = inspect.signature(_ADD_TOOL)
        params = list(sig.parameters.values())

        if any(p.kind == p.VAR_KEYWORD for p in params) or len(params) <= 1:
            _ADD_TOOL(
                tool=tool,
                args=args,
                started_at=started_at,
                ended_at=ended_at,
                ok=ok,
                output_preview_len=int(output_preview_len or 0),
                error=error,
            )
            return

        if len(params) >= 5:
            _ADD_TOOL(tool, args, started_at, ended_at, ok)
            return

        _ADD_TOOL(
            tool=tool,
            args=args,
            started_at=started_at,
            ended_at=ended_at,
            ok=ok,
            output_preview_len=int(output_preview_len or 0),
            error=error,
        )
    except Exception:
        pass

def _extract_token_usage_from_generation(gen_obj: Any) -> Dict[str, int]:
    """Try to pull token counts from LC/Azure metadata; be defensive."""
    usage_in = usage_out = 0
    meta = getattr(gen_obj, "response_metadata", None) or getattr(gen_obj, "usage_metadata", None) or {}
    try:
        usage_in = int(meta.get("input_tokens", 0) or meta.get("prompt_tokens", 0) or 0)
        usage_out = int(meta.get("output_tokens", 0) or meta.get("completion_tokens", 0) or 0)
    except Exception:
        pass

    if (usage_in == 0 and usage_out == 0) and isinstance(meta, dict):
        inner = meta.get("usage") or meta.get("token_usage") or {}
        try:
            usage_in = usage_in or int(inner.get("prompt_tokens", inner.get("input_tokens", 0)) or 0)
            usage_out = usage_out or int(inner.get("completion_tokens", inner.get("output_tokens", 0)) or 0)
        except Exception:
            pass

    try:
        gi = getattr(gen_obj, "generation_info", None) or {}
        usage_in = usage_in or int(gi.get("prompt_tokens", 0) or 0)
        usage_out = usage_out or int(gi.get("completion_tokens", 0) or 0)
    except Exception:
        pass

    return {"input_tokens": usage_in, "output_tokens": usage_out}

def _invoke_llm_with_tracking(label: str, bound_llm: Any, messages: List[BaseMessage]) -> AIMessage:
    """Invoke an LLM (raw or tool-bound) and emit tracking event with tokens."""
    started = time.time()
    tools = getattr(bound_llm, "_lc_tools", None)
    try:
        out = bound_llm.invoke(messages)
        ended = time.time()
        usage = _extract_token_usage_from_generation(out)
        _safe_add_llm_event(
            label=label,
            prompt_messages=messages,
            response_message=out,
            tools_bound=[getattr(t, "name", str(t)) for t in (tools or [])],
            started_at=started,
            ended_at=ended,
            ok=True,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )
        return out
    except Exception as e:
        ended = time.time()
        _safe_add_llm_event(
            label=label,
            prompt_messages=messages,
            response_message=None,
            tools_bound=[getattr(t, "name", str(t)) for t in (tools or [])],
            started_at=started,
            ended_at=ended,
            ok=False,
            error=str(e),
        )
        raise

# -------------------------------------------------------------------
# Utilities
# -------------------------------------------------------------------
def _check_content_safety(text: str) -> dict:
    if not safety_client or not AnalyzeTextOptions:
        return {}
    try:
        resp = safety_client.analyze_text(AnalyzeTextOptions(text=text or ""))
        return {c.category: int(c.severity) for c in resp.categories_analysis}
    except Exception:
        return {}

def _content_is_abusive(safety: dict) -> bool:
    # High or VeryHigh
    return any(sev >= 2 for sev in safety.values())

def _generate_complaint_id() -> str:
    ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{COMPLAINT_PREFIX}-{ts}-{suffix}"

def _log_complaint_to_blob(complaint_id: str, user_text: str):
    timestamp = datetime.datetime.utcnow().isoformat()
    line = f"{complaint_id},{timestamp},{user_text}\n"
    blob_name = "complaints_log.csv"
    try:
        existing = container_client.download_blob(blob_name).readall().decode("utf-8")
    except ResourceNotFoundError:
        existing = "complaint_id,timestamp,query\n"
    updated = existing + line
    container_client.upload_blob(blob_name, updated, overwrite=True)

def _auth_system_message(auth: Optional[Dict[str, str]]) -> SystemMessage:
    if not auth:
        return SystemMessage(content="AUTH_CONTEXT: role=anonymous; customer_id=")
    role = auth.get("role", "anonymous")
    cid = auth.get("customer_id", "")
    return SystemMessage(content=(
        f"AUTH_CONTEXT:\n"
        f"- role={role}\n"
        f"- customer_id={cid}\n"
        "Rules:\n"
        "1) If role=customer, 'my/me/mine' refers to this customer_id.\n"
        "2) Do NOT ask for customer ID unless role=admin querying another user explicitly.\n"
        "3) Specialists MUST respect RBAC.\n"
    ))

def _summarize(prev_summary: Optional[str], messages: List[BaseMessage]) -> str:
    """Keep a short rolling summary (<= 60 words). Deterministic (temp=0)."""
    last_turn = messages[-4:]
    text = "\n".join(
        f"{type(m).__name__}: {m.content}"
        for m in last_turn
        if isinstance(m, (HumanMessage, AIMessage))
    )
    prompt = [
        SystemMessage(content="Summarize conversation focus in <=60 words for downstream routing; no fluff."),
        HumanMessage(content=f"Previous summary: {prev_summary or ''}\n\nRecent:\n{text}")
    ]
    # tracking the summarizer too
    return _invoke_llm_with_tracking("summary", llm, prompt).content.strip()

def _wants_human(text: str) -> bool:
    """
    True only when the user explicitly wants a human/agent OR urgent escalation.
    """
    t = (text or "").lower()
    triggers = [
        "talk to a human", "talk to an agent", "connect me to an agent",
        "speak to someone", "speak to a human", "human agent", "escalate this",
        "escalation", "file a complaint now", "raise a complaint now",
        "call me", "call back", "urgent help", "urgent assistance",
    ]
    return any(p in t for p in triggers)

# -------------------------------------------------------------------
# Local tools (LangChain StructuredTool)
# -------------------------------------------------------------------
search_knowledge_base_tool = StructuredTool.from_function(
    func=search_knowledge_base,
    name="search_knowledge_base",
    description="Search the bank knowledge base for a domain. Inputs: query (str), domain (str). Returns text with 'Sources:' block when available.",
)

get_account_info_tool = StructuredTool.from_function(
    func=get_account_info,
    name="get_account_info",
    description="Fetch customer-specific data (RBAC via AUTH_CONTEXT). Inputs: customer_id (str), info_needed (str).",
)

tool_executor = ToolExecutor([search_knowledge_base_tool, get_account_info_tool])

# -------------------------------------------------------------------
# Local deterministic LLM classifier (no MCP)
# -------------------------------------------------------------------
def _local_llm_classify(message: str, summary: Optional[str]) -> List[str]:
    SYSTEM = """
You are a strict, deterministic ROUTING CLASSIFIER for a banking assistant.
Given the user's latest message and a short rolling summary, return the BEST
specialist agent(s) in PRIORITY ORDER. Output ONLY a comma-separated list from this set:

Account_Info_Agent, Account_Management_Agent, Loan_EMI_Agent,
Complaints_Escalations_Agent, Digital_Banking_Agent, Cards_Agent,
Regulatory_Compliance_Agent

NEVER output anything else. No extra words, no punctuation beyond commas.

ROUTING POLICY (priority):

1) Human/Escalation intent:
   Only if the user explicitly asks to speak to a human or to escalate NOW
   (e.g., “agent”, “human”, “manager”, “escalate this”, “urgent help”, “call me”,"care executive"),
   put: Complaints_Escalations_Agent FIRST.
   If the message is about the PROCESS of filing a complaint (steps/online portal),
   DO NOT treat as escalation; still use Complaints_Escalations_Agent, but for INFORMATIONAL handling.

2) Personal account data:
   If the message is about the user’s own account/card/loan/profile (has “my/me/mine” or clear personal ask),
   choose: Account_Info_Agent.

3) Domain/process questions (SOP/steps/eligibility/fees/docs):
   - Credit/debit cards → Cards_Agent
   - Loans/EMI/interest/eligibility → Loan_EMI_Agent
   - Online/mobile/UPI/netbanking/security → Digital_Banking_Agent
   - General account management (change address, open/close account, reset password) → Account_Management_Agent
   - Regulatory/RBI/KYC/AML/policy/legal/compliance → Regulatory_Compliance_Agent
   - Complaints processes/portals/forms → Complaints_Escalations_Agent (informational mode)

4) Multi-intent:
   If the message clearly contains multiple distinct intents, return a comma-separated list in priority order.
   Example: “How to file a complaint and how to apply for a credit card?”
   → Complaints_Escalations_Agent, Cards_Agent

5) Ambiguous:
   If banking-related but unclear, prefer Account_Management_Agent.

Use the conversation summary when present to disambiguate follow-ups.
Do NOT return more than 3 agents. Do NOT include duplicates.
Output exactly the agent names, comma-separated if multiple, no extra text.
"""
    USER = f"Message: {message}\nSummary: {summary or ''}"
    out = _invoke_llm_with_tracking(
        "router",
        llm,
        [SystemMessage(content=SYSTEM.strip()), HumanMessage(content=USER.strip())],
    ).content.strip()
    raw = [x.strip() for x in out.split(",") if x.strip()]
    valid = set(VALID_AGENT_NAMES)
    routes = [r for r in raw if r in valid] or ["Account_Management_Agent"]
    return routes[:3]

# -------------------------------------------------------------------
# State
# -------------------------------------------------------------------
class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], lambda x, y: x + y]
    next_agent: str
    pending_agents: List[str]
    complaint_id: Optional[str]
    auth: Optional[Dict[str, str]]
    summary: Optional[str]

def _trim_messages(msgs: List[BaseMessage], keep_last: int = 12) -> List[BaseMessage]:
    """Keep last N conversational messages; preserve earliest AUTH_CONTEXT system if present."""
    if not msgs:
        return []
    auth_idx = None
    for i, m in enumerate(msgs[:3]):
        if isinstance(m, SystemMessage) and "AUTH_CONTEXT:" in (m.content or ""):
            auth_idx = i
            break
    convo = [m for m in msgs if isinstance(m, (HumanMessage, AIMessage, ToolMessage))]
    trimmed = convo[-keep_last:]
    out: List[BaseMessage] = []
    if auth_idx is not None:
        out.append(msgs[auth_idx])
    out.extend(trimmed)
    return out

# -------------------------------------------------------------------
# Nodes
# -------------------------------------------------------------------
def orchestrator_node(state: AgentState):
    print("--- ORCHESTRATOR: Deciding next agent(s) ---")
    last = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
    user_text = last.content if last else ""

    new_summary = _summarize(state.get("summary"), state["messages"])

    # Content Safety guard
    safety = _check_content_safety(user_text)
    if safety and _content_is_abusive(safety):
        print("--- ORCHESTRATOR: Content Safety high -> Moderation_Agent ---")
        return {"next_agent": "Moderation_Agent", "pending_agents": [], "summary": new_summary}

    # Local classification (no MCP)
    routes = _local_llm_classify(user_text, new_summary)
    print(f"--- ORCHESTRATOR: Classifier -> {routes} ---")

    first, rest = routes[0], routes[1:]
    print(f"--- ORCHESTRATOR: Next -> {first} | Queue -> {rest} ---")
    return {"next_agent": first, "pending_agents": rest, "summary": new_summary}

def specialist_node_factory(agent_name: str, domain_tag: Optional[str] = None):
    def specialist_node(state: AgentState):
        print(f"--- AGENT: Executing {agent_name} ---")
        auth_msg = _auth_system_message(state.get("auth"))

        # Moderation
        if agent_name == "Moderation_Agent":
            msg = AIMessage(content=(
                "I’m here to help with your banking questions. Let’s keep it respectful. "
                "Tell me what you need help with (e.g., card activation, loan options, netbanking). "
                "To speak with a human, say: “I want to speak to an agent.”"
            ))
            msg.name = agent_name
            return {"messages": [msg], "pending_agents": []}

        # Dual-mode Complaints agent
        if agent_name == "Complaints_Escalations_Agent":
            last_user = next((m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), "") or ""
            if _wants_human(last_user):
                complaint_id = _generate_complaint_id()
                try:
                    _log_complaint_to_blob(complaint_id, last_user)
                except Exception:
                    pass
                content = (
                    "I’m connecting you to a human agent now.\n\n"
                    f"**Complaint ID:** `{complaint_id}`\n\n"
                    f"**Immediate options:**\n"
                    f"- 📞 Phone (24×7): {SUPPORT_PHONE}\n"
                    f"- 💬 Live Chat: {SUPPORT_LIVECHAT}\n"
                    f"- ✉️ Email: {SUPPORT_EMAIL}\n\n"
                    "Please keep these ready: Full name, registered phone/email, last 4 digits of card/account, "
                    "brief summary of the issue, any error messages/transaction refs."
                )
                msg = AIMessage(content=content); msg.name = agent_name
                return {"messages": [msg], "pending_agents": [], "complaint_id": complaint_id}

            # Informational mode → use KB
            tools_for_agent = [search_knowledge_base_tool]
            sys_prompt = AGENT_SYSTEM_PROMPTS["DEFAULT_SPECIALIST"].format(
                domain_description="complaints & escalations processes",
                domain_tag="complaints",
            )
            print(f"--- AGENT: {agent_name} bound tools -> {[t.name for t in tools_for_agent]} ---")
            bound_llm = llm.bind_tools(tools_for_agent)
            prompt_with_history = [auth_msg, SystemMessage(content=sys_prompt)] + state["messages"]
            result = _invoke_llm_with_tracking(f"{agent_name.lower()}", bound_llm, prompt_with_history)
            result.name = agent_name
            return {"messages": [result]}

        # Account info → get_account_info tool
        if agent_name == "Account_Info_Agent":
            sys_prompt = AGENT_SYSTEM_PROMPTS["Account_Info_Agent"]
            tools_for_agent = [get_account_info_tool]
        else:
            # Other domain specialists → KB search only
            sys_prompt = AGENT_SYSTEM_PROMPTS["DEFAULT_SPECIALIST"].format(
                domain_description=f"questions about {domain_tag}",
                domain_tag=domain_tag or "",
            )
            tools_for_agent = [search_knowledge_base_tool]

        print(f"--- AGENT: {agent_name} bound tools -> {[t.name for t in tools_for_agent]} ---")
        bound_llm = llm.bind_tools(tools_for_agent)
        prompt_with_history = [auth_msg, SystemMessage(content=sys_prompt)] + state["messages"]
        result = _invoke_llm_with_tracking(f"{agent_name.lower()}", bound_llm, prompt_with_history)
        result.name = agent_name
        return {"messages": [result]}
    return specialist_node

def tool_node(state: AgentState):
    print("--- TOOL EXECUTOR: Checking for and running tools ---")
    last_message = state["messages"][-1]
    if not hasattr(last_message, "tool_calls") or not last_message.tool_calls:
        print("--- TOOL EXECUTOR: No tool calls found. ---")
        return {"messages": []}
    tool_messages: List[ToolMessage] = []
    for tc in last_message.tool_calls:
        t0 = time.time()
        tool_name = tc.get("name", "<unknown>")
        tool_args = tc.get("args", {})
        tool_id = tc.get("id", "")
        try:
            inv = ToolInvocation(tool=tool_name, tool_input=tool_args, id=tool_id)
            out = tool_executor.invoke(inv)
            t1 = time.time()
            _safe_add_tool_event(
                tool=tool_name,
                args=tool_args,
                started_at=t0,
                ended_at=t1,
                ok=True,
                output_preview_len=len(str(out)) if out is not None else 0,
            )
            print(f"--- TOOL EXECUTOR: Tool '{tool_name}' output: {str(out)[:200]} ---")
            tool_messages.append(ToolMessage(content=str(out), tool_call_id=tool_id))
        except Exception as e:
            t1 = time.time()
            _safe_add_tool_event(
                tool=tool_name,
                args=tool_args,
                started_at=t0,
                ended_at=t1,
                ok=False,
                error=str(e),
            )
            print(f"--- TOOL EXECUTOR ERROR {tool_name}: {e} ---")
            tool_messages.append(ToolMessage(content=f"Error running tool {tool_name}: {e}", tool_call_id=tool_id))
    return {"messages": tool_messages}

def dispatch_node(state: AgentState):
    queue = state.get("pending_agents", [])
    if not queue:
        print("--- DISPATCH: No more agents in queue. FINISH. ---")
        return {"next_agent": "FINISH", "pending_agents": []}
    nxt, rest = queue[0], queue[1:]
    print(f"--- DISPATCH: Routing to {nxt} | Remaining queue -> {rest} ---")
    return {"next_agent": nxt, "pending_agents": rest}

def escalation_node(state: AgentState):
    print("--- ESCALATION: Preparing message for human agent. ---")
    msg = AIMessage(content="I am escalating your request to a human agent who will be in touch shortly.")
    return {"messages": [msg]}

# -------- Building Graph --------
workflow = StateGraph(AgentState)
workflow.add_node("Orchestrator", orchestrator_node)
workflow.add_node("Tool_Executor", tool_node)
workflow.add_node("Dispatch", dispatch_node)
workflow.add_node("Escalate", escalation_node)

for name, domain in AGENT_DOMAINS:
    workflow.add_node(name, specialist_node_factory(name, domain))

workflow.set_entry_point("Orchestrator")

def _router(state: AgentState):
    last = state["messages"][-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "Tool_Executor"
    if isinstance(last, ToolMessage):
        for msg in reversed(state["messages"][:-1]):
            if isinstance(msg, AIMessage) and getattr(msg, "name", None):
                return msg.name
    if state.get("pending_agents"):
        return "Dispatch"
    return END

workflow.add_conditional_edges(
    "Orchestrator",
    lambda s: s["next_agent"],
    {name: name for name, _ in AGENT_DOMAINS} | {"Escalate": "Escalate"},
)
workflow.add_conditional_edges("Tool_Executor", _router, {name: name for name, _ in AGENT_DOMAINS})
for name, _ in AGENT_DOMAINS:
    workflow.add_conditional_edges(name, _router, {"Tool_Executor": "Tool_Executor", "Dispatch": "Dispatch", END: END})
workflow.add_conditional_edges("Dispatch", lambda s: s["next_agent"], {name: name for name, _ in AGENT_DOMAINS} | {"FINISH": END})
workflow.add_edge("Escalate", END)

app = workflow.compile()

# -------------------------------------------------------------------
# Public APIs
# -------------------------------------------------------------------
def run_swarm_step(
    query: str,
    auth: Optional[Dict[str, str]] = None,
    prev_state: Optional[Dict[str, Any]] = None,
    history_keep_last: int = 12,
) -> Dict[str, Any]:
    """
    Single conversational step with optional previous memory.
    prev_state supports keys: 'messages' (List[BaseMessage]) and 'summary' (str).
    Returns: { content, complaint_id, state: {messages, summary, complaint_id} }
    """
    # rebuild trimmed history if present
    messages: List[BaseMessage] = []
    if prev_state and isinstance(prev_state.get("messages"), list):
        messages = _trim_messages(prev_state["messages"], keep_last=history_keep_last)

    # append the new human message
    messages = messages + [HumanMessage(content=query, name="User")]

    initial_state: AgentState = {
        "messages": messages,
        "pending_agents": [],
        "next_agent": "",
        "complaint_id": prev_state.get("complaint_id") if prev_state else None,
        "auth": auth or None,
        "summary": prev_state.get("summary") if prev_state else None,
    }

    final_state = app.invoke(initial_state, {"recursion_limit": 20})
    final_message = final_state["messages"][-1]

    # prepare compact state to return
    new_msgs = _trim_messages(final_state["messages"], keep_last=history_keep_last)
    new_state = {
        "messages": new_msgs,
        "summary": final_state.get("summary"),
        "complaint_id": final_state.get("complaint_id"),
    }
    return {
        "content": final_message.content,
        "complaint_id": final_state.get("complaint_id"),
        "state": new_state
    }

def run_swarm_conversation(query: str, auth: Optional[Dict[str, str]] = None) -> dict:
    """
    Legacy one-shot call (no memory). Kept for backward compatibility.
    """
    initial_state = {
        "messages": [HumanMessage(content=query, name="User")],
        "pending_agents": [],
        "next_agent": "",
        "complaint_id": None,
        "auth": auth or None,
        "summary": None,
    }
    final_state = app.invoke(initial_state, {"recursion_limit": 20})
    final_message = final_state["messages"][-1]
    return {"content": final_message.content, "complaint_id": final_state.get("complaint_id")}














# latest working code


# # backend/graph.py
# """
# AutoRouteAI multi-agent workflow (local tools only):
# - Deterministic local LLM classifier (no MCP)
# - Azure Content Safety gate (optional)
# - Dual-mode Complaints agent: info vs explicit human escalation
# - Account_Info agent uses get_account_info tool (RBAC via AUTH_CONTEXT; SQL under the hood)
# - All other specialists use search_knowledge_base with domain tags
# - Adds conversational memory entrypoint: run_swarm_step (trimmed rolling history + summary)
# """

# import os
# import datetime
# import random
# import string
# from typing import TypedDict, Annotated, List, Optional, Dict, Any

# from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, AIMessage, ToolMessage
# from langgraph.graph import StateGraph, END
# from langgraph.prebuilt import ToolExecutor
# from langgraph.prebuilt.tool_executor import ToolInvocation
# from langchain_core.tools import StructuredTool
# from azure.core.exceptions import ResourceNotFoundError

# from config import llm, container_client
# from prompts import AGENT_SYSTEM_PROMPTS
# from tools.knowledge_base import search_knowledge_base
# from tools.account_info import get_account_info
# from uuid import uuid4
# from langchain_core.messages import HumanMessage
# from typing import Any

# # === Optional Content Safety
# try:
#     from config import safety_client
#     from azure.ai.contentsafety.models import AnalyzeTextOptions
# except Exception:
#     safety_client = None
#     AnalyzeTextOptions = None

# # -------------------------------------------------------------------
# # Support contact (from .env with safe defaults)
# # -------------------------------------------------------------------
# SUPPORT_PHONE = os.getenv("SUPPORT_PHONE", "1800-000-0000")
# SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "support@yourbank.example")
# SUPPORT_LIVECHAT = os.getenv("SUPPORT_LIVECHAT_URL", "https://yourbank.example/support/chat")
# COMPLAINT_PREFIX = os.getenv("COMPLAINT_PREFIX", "CMP")

# # Agent registry (names must match prompts + classifier)
# AGENT_DOMAINS = [
#     ("Moderation_Agent", None),
#     ("Out_Of_Scope_Agent", None),
#     ("Complaints_Escalations_Agent", "complaints"),
#     ("Account_Info_Agent", "account_info"),
#     ("Account_Management_Agent", "account_management"),
#     ("Loan_EMI_Agent", "loans_EMI"),
#     ("Digital_Banking_Agent", "digital_banking"),
#     ("Cards_Agent", "cards"),
#     ("Regulatory_Compliance_Agent", "regulatory_compliance"),
# ]
# VALID_AGENT_NAMES = [name for name, _ in AGENT_DOMAINS]

# # -------------------------------------------------------------------
# # Utilities
# # -------------------------------------------------------------------
# def _check_content_safety(text: str) -> dict:
#     if not safety_client or not AnalyzeTextOptions:
#         return {}
#     try:
#         resp = safety_client.analyze_text(AnalyzeTextOptions(text=text or ""))
#         return {c.category: int(c.severity) for c in resp.categories_analysis}
#     except Exception:
#         return {}

# def _content_is_abusive(safety: dict) -> bool:
#     # High or VeryHigh
#     return any(sev >= 2 for sev in safety.values())

# def _generate_complaint_id() -> str:
#     ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
#     suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
#     return f"{COMPLAINT_PREFIX}-{ts}-{suffix}"

# def _log_complaint_to_blob(complaint_id: str, user_text: str):
#     timestamp = datetime.datetime.utcnow().isoformat()
#     line = f"{complaint_id},{timestamp},{user_text}\n"
#     blob_name = "complaints_log.csv"
#     try:
#         existing = container_client.download_blob(blob_name).readall().decode("utf-8")
#     except ResourceNotFoundError:
#         existing = "complaint_id,timestamp,query\n"
#     updated = existing + line
#     container_client.upload_blob(blob_name, updated, overwrite=True)

# def _auth_system_message(auth: Optional[Dict[str, str]]) -> SystemMessage:
#     if not auth:
#         return SystemMessage(content="AUTH_CONTEXT: role=anonymous; customer_id=")
#     role = auth.get("role", "anonymous")
#     cid = auth.get("customer_id", "")
#     return SystemMessage(content=(
#         f"AUTH_CONTEXT:\n"
#         f"- role={role}\n"
#         f"- customer_id={cid}\n"
#         "Rules:\n"
#         "1) If role=customer, 'my/me/mine' refers to this customer_id.\n"
#         "2) Do NOT ask for customer ID unless role=admin querying another user explicitly.\n"
#         "3) Specialists MUST respect RBAC.\n"
#     ))

# def _summarize(prev_summary: Optional[str], messages: List[BaseMessage]) -> str:
#     """Keep a short rolling summary (<= 60 words). Deterministic (temp=0)."""
#     last_turn = messages[-4:]
#     text = "\n".join(
#         f"{type(m).__name__}: {m.content}"
#         for m in last_turn
#         if isinstance(m, (HumanMessage, AIMessage))
#     )
#     prompt = [
#         SystemMessage(content="Summarize conversation focus in <=60 words for downstream routing; no fluff."),
#         HumanMessage(content=f"Previous summary: {prev_summary or ''}\n\nRecent:\n{text}")
#     ]
#     return llm.invoke(prompt).content.strip()

# def _wants_human(text: str) -> bool:
#     """
#     True only when the user explicitly wants a human/agent OR urgent escalation.
#     """
#     t = (text or "").lower()
#     triggers = [
#         "talk to a human", "talk to an agent", "connect me to an agent",
#         "speak to someone", "speak to a human", "human agent", "escalate this",
#         "escalation", "file a complaint now", "raise a complaint now",
#         "call me", "call back", "urgent help", "urgent assistance",
#     ]
#     return any(p in t for p in triggers)

# # -------------------------------------------------------------------
# # Local tools (LangChain StructuredTool)
# # -------------------------------------------------------------------
# search_knowledge_base_tool = StructuredTool.from_function(
#     func=search_knowledge_base,
#     name="search_knowledge_base",
#     description="Search the bank knowledge base for a domain. Inputs: query (str), domain (str). Returns text with 'Sources:' block when available.",
# )

# get_account_info_tool = StructuredTool.from_function(
#     func=get_account_info,
#     name="get_account_info",
#     description="Fetch customer-specific data (RBAC via AUTH_CONTEXT). Inputs: customer_id (str), info_needed (str).",
# )

# tool_executor = ToolExecutor([search_knowledge_base_tool, get_account_info_tool])

# # -------------------------------------------------------------------
# # Local deterministic LLM classifier (no MCP)
# # -------------------------------------------------------------------
# def _local_llm_classify(message: str, summary: Optional[str]) -> List[str]:
#     SYSTEM = """
# You are a strict, deterministic ROUTING CLASSIFIER for a banking assistant.
# Given the user's latest message and a short rolling summary, return the BEST
# specialist agent(s) in PRIORITY ORDER. Output ONLY a comma-separated list from this set:

# Account_Info_Agent, Account_Management_Agent, Loan_EMI_Agent,
# Complaints_Escalations_Agent, Digital_Banking_Agent, Cards_Agent,
# Regulatory_Compliance_Agent

# NEVER output anything else. No extra words, no punctuation beyond commas.

# ROUTING POLICY (priority):

# 1) Human/Escalation intent:
#    Only if the user explicitly asks to speak to a human or to escalate NOW
#    (e.g., “agent”, “human”, “manager”, “escalate this”, “urgent help”, “call me”,"care executive"),
#    put: Complaints_Escalations_Agent FIRST.
#    If the message is about the PROCESS of filing a complaint (steps/online portal),
#    DO NOT treat as escalation; still use Complaints_Escalations_Agent, but for INFORMATIONAL handling.

# 2) Personal account data:
#    If the message is about the user’s own account/card/loan/profile (has “my/me/mine” or clear personal ask),
#    choose: Account_Info_Agent.

# 3) Domain/process questions (SOP/steps/eligibility/fees/docs):
#    - Credit/debit cards → Cards_Agent
#    - Loans/EMI/interest/eligibility → Loan_EMI_Agent
#    - Online/mobile/UPI/netbanking/security → Digital_Banking_Agent
#    - General account management (change address, open/close account, reset password) → Account_Management_Agent
#    - Regulatory/RBI/KYC/AML/policy/legal/compliance → Regulatory_Compliance_Agent
#    - Complaints processes/portals/forms → Complaints_Escalations_Agent (informational mode)

# 4) Multi-intent:
#    If the message clearly contains multiple distinct intents, return a comma-separated list in priority order.
#    Example: “How to file a complaint and how to apply for a credit card?”
#    → Complaints_Escalations_Agent, Cards_Agent

# 5) Ambiguous:
#    If banking-related but unclear, prefer Account_Management_Agent.

# Use the conversation summary when present to disambiguate follow-ups.
# Do NOT return more than 3 agents. Do NOT include duplicates.
# Output exactly the agent names, comma-separated if multiple, no extra text.
# """
#     USER = f"Message: {message}\nSummary: {summary or ''}"
#     out = llm.invoke([SystemMessage(content=SYSTEM.strip()), HumanMessage(content=USER.strip())]).content.strip()
#     raw = [x.strip() for x in out.split(",") if x.strip()]
#     valid = set(VALID_AGENT_NAMES)
#     routes = [r for r in raw if r in valid] or ["Account_Management_Agent"]
#     return routes[:3]

# # -------------------------------------------------------------------
# # State
# # -------------------------------------------------------------------
# class AgentState(TypedDict):
#     messages: Annotated[List[BaseMessage], lambda x, y: x + y]
#     next_agent: str
#     pending_agents: List[str]
#     complaint_id: Optional[str]
#     auth: Optional[Dict[str, str]]
#     summary: Optional[str]

# def _trim_messages(msgs: List[BaseMessage], keep_last: int = 12) -> List[BaseMessage]:
#     """Keep last N conversational messages; preserve earliest AUTH_CONTEXT system if present."""
#     auth_idx = None
#     for i, m in enumerate(msgs[:3]):
#         if isinstance(m, SystemMessage) and "AUTH_CONTEXT:" in (m.content or ""):
#             auth_idx = i
#             break

#     convo = [m for m in msgs if isinstance(m, (HumanMessage, AIMessage, ToolMessage))]
#     trimmed = convo[-keep_last:]

#     out: List[BaseMessage] = []
#     if auth_idx is not None:
#         out.append(msgs[auth_idx])
#     out.extend(trimmed)
#     return out

# # -------------------------------------------------------------------
# # Nodes
# # -------------------------------------------------------------------
# def orchestrator_node(state: AgentState):
#     print("--- ORCHESTRATOR: Deciding next agent(s) ---")
#     last = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
#     user_text = last.content if last else ""

#     new_summary = _summarize(state.get("summary"), state["messages"])

#     # Content Safety guard
#     safety = _check_content_safety(user_text)
#     if safety and _content_is_abusive(safety):
#         print("--- ORCHESTRATOR: Content Safety high -> Moderation_Agent ---")
#         return {"next_agent": "Moderation_Agent", "pending_agents": [], "summary": new_summary}

#     # Local classification (no MCP)
#     routes = _local_llm_classify(user_text, new_summary)
#     print(f"--- ORCHESTRATOR: Classifier -> {routes} ---")

#     first, rest = routes[0], routes[1:]
#     print(f"--- ORCHESTRATOR: Next -> {first} | Queue -> {rest} ---")
#     return {"next_agent": first, "pending_agents": rest, "summary": new_summary}

# def specialist_node_factory(agent_name: str, domain_tag: Optional[str] = None):
#     def specialist_node(state: AgentState):
#         print(f"--- AGENT: Executing {agent_name} ---")
#         auth_msg = _auth_system_message(state.get("auth"))

#         # Moderation
#         if agent_name == "Moderation_Agent":
#             msg = AIMessage(content=(
#                 "I’m here to help with your banking questions. Let’s keep it respectful. "
#                 "Tell me what you need help with (e.g., card activation, loan options, netbanking). "
#                 "To speak with a human, say: “I want to speak to an agent.”"
#             ))
#             msg.name = agent_name
#             return {"messages": [msg], "pending_agents": []}

#         # Dual-mode Complaints agent
#         if agent_name == "Complaints_Escalations_Agent":
#             last_user = next((m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), "") or ""
#             if _wants_human(last_user):
#                 complaint_id = _generate_complaint_id()
#                 try:
#                     _log_complaint_to_blob(complaint_id, last_user)
#                 except Exception:
#                     pass
#                 content = (
#                     "I’m connecting you to a human agent now.\n\n"
#                     f"**Complaint ID:** `{complaint_id}`\n\n"
#                     f"**Immediate options:**\n"
#                     f"- 📞 Phone (24×7): {SUPPORT_PHONE}\n"
#                     f"- 💬 Live Chat: {SUPPORT_LIVECHAT}\n"
#                     f"- ✉️ Email: {SUPPORT_EMAIL}\n\n"
#                     "Please keep these ready: Full name, registered phone/email, last 4 digits of card/account, "
#                     "brief summary of the issue, any error messages/transaction refs."
#                 )
#                 msg = AIMessage(content=content); msg.name = agent_name
#                 return {"messages": [msg], "pending_agents": [], "complaint_id": complaint_id}

#             # Informational mode → use KB (complaints domain)
#             tools_for_agent = [search_knowledge_base_tool]
#             sys_prompt = AGENT_SYSTEM_PROMPTS["DEFAULT_SPECIALIST"].format(
#                 domain_description="complaints & escalations processes",
#                 domain_tag="complaints",
#             )
#             print(f"--- AGENT: {agent_name} bound tools -> {[t.name for t in tools_for_agent]} ---")
#             bound_llm = llm.bind_tools(tools_for_agent)
#             prompt_with_history = [auth_msg, SystemMessage(content=sys_prompt)] + state["messages"]
#             result = bound_llm.invoke(prompt_with_history)
#             result.name = agent_name
#             return {"messages": [result]}

#         # Account info → get_account_info tool (single-customer lookups with RBAC via AUTH_CONTEXT)
#         if agent_name == "Account_Info_Agent":
#             sys_prompt = AGENT_SYSTEM_PROMPTS["Account_Info_Agent"]
#             tools_for_agent = [get_account_info_tool]
#         else:
#             # Other domain specialists → KB search only (no SQL relations here)
#             sys_prompt = AGENT_SYSTEM_PROMPTS["DEFAULT_SPECIALIST"].format(
#                 domain_description=f"questions about {domain_tag}",
#                 domain_tag=domain_tag or "",
#             )
#             tools_for_agent = [search_knowledge_base_tool]

#         print(f"--- AGENT: {agent_name} bound tools -> {[t.name for t in tools_for_agent]} ---")
#         bound_llm = llm.bind_tools(tools_for_agent)
#         prompt_with_history = [auth_msg, SystemMessage(content=sys_prompt)] + state["messages"]
#         result = bound_llm.invoke(prompt_with_history)
#         result.name = agent_name
#         return {"messages": [result]}
#     return specialist_node

# def tool_node(state: AgentState):
#     print("--- TOOL EXECUTOR: Checking for and running tools ---")
#     last_message = state["messages"][-1]
#     if not hasattr(last_message, "tool_calls") or not last_message.tool_calls:
#         print("--- TOOL EXECUTOR: No tool calls found. ---")
#         return {"messages": []}
#     tool_messages: List[ToolMessage] = []
#     for tc in last_message.tool_calls:
#         try:
#             inv = ToolInvocation(tool=tc["name"], tool_input=tc.get("args", {}), id=tc["id"])
#             out = tool_executor.invoke(inv)
#             print(f"--- TOOL EXECUTOR: Tool '{tc['name']}' output: {str(out)[:200]} ---")
#             tool_messages.append(ToolMessage(content=str(out), tool_call_id=tc["id"]))
#         except Exception as e:
#             print(f"--- TOOL EXECUTOR ERROR {tc.get('name','<unknown>')}: {e} ---")
#             tool_messages.append(ToolMessage(content=f"Error running tool {tc.get('name','<unknown>')}: {e}", tool_call_id=tc.get("id","")))
#     return {"messages": tool_messages}

# def dispatch_node(state: AgentState):
#     queue = state.get("pending_agents", [])
#     if not queue:
#         print("--- DISPATCH: No more agents in queue. FINISH. ---")
#         return {"next_agent": "FINISH", "pending_agents": []}
#     nxt, rest = queue[0], queue[1:]
#     print(f"--- DISPATCH: Routing to {nxt} | Remaining queue -> {rest} ---")
#     return {"next_agent": nxt, "pending_agents": rest}

# def escalation_node(state: AgentState):
#     print("--- ESCALATION: Preparing message for human agent. ---")
#     msg = AIMessage(content="I am escalating your request to a human agent who will be in touch shortly.")
#     return {"messages": [msg]}

# # -------- Build Graph --------
# workflow = StateGraph(AgentState)
# workflow.add_node("Orchestrator", orchestrator_node)
# workflow.add_node("Tool_Executor", tool_node)
# workflow.add_node("Dispatch", dispatch_node)
# workflow.add_node("Escalate", escalation_node)

# for name, domain in AGENT_DOMAINS:
#     workflow.add_node(name, specialist_node_factory(name, domain))

# workflow.set_entry_point("Orchestrator")

# def _router(state: AgentState):
#     last = state["messages"][-1]
#     if hasattr(last, "tool_calls") and last.tool_calls:
#         return "Tool_Executor"
#     if isinstance(last, ToolMessage):
#         for msg in reversed(state["messages"][:-1]):
#             if isinstance(msg, AIMessage) and getattr(msg, "name", None):
#                 return msg.name
#     if state.get("pending_agents"):
#         return "Dispatch"
#     return END

# workflow.add_conditional_edges(
#     "Orchestrator",
#     lambda s: s["next_agent"],
#     {name: name for name, _ in AGENT_DOMAINS} | {"Escalate": "Escalate"},
# )
# workflow.add_conditional_edges("Tool_Executor", _router, {name: name for name, _ in AGENT_DOMAINS})
# for name, _ in AGENT_DOMAINS:
#     workflow.add_conditional_edges(name, _router, {"Tool_Executor": "Tool_Executor", "Dispatch": "Dispatch", END: END})
# workflow.add_conditional_edges("Dispatch", lambda s: s["next_agent"], {name: name for name, _ in AGENT_DOMAINS} | {"FINISH": END})
# workflow.add_edge("Escalate", END)

# app = workflow.compile()

# # -------------------------------------------------------------------
# # Public APIs
# # -------------------------------------------------------------------
# # def run_swarm_step(
# #     query: str,
# #     auth: Optional[Dict[str, str]] = None,
# #     prev_state: Optional[Dict[str, Any]] = None,
# #     history_keep_last: int = 12,
# # ) -> Dict[str, Any]:
# #     """
# #     Single conversational step with optional previous memory.
# #     prev_state supports keys: 'messages' (List[BaseMessage]) and 'summary' (str).
# #     Returns: { content, complaint_id, state: {messages, summary} }
# #     """
# #     # rebuild trimmed history if present
# #     messages: List[BaseMessage] = []
# #     if prev_state and isinstance(prev_state.get("messages"), list):
# #         messages = _trim_messages(prev_state["messages"], keep_last=history_keep_last)

# #     # append the new human message
# #     messages = messages + [HumanMessage(content=query, name="User")]

# #     initial_state: AgentState = {
# #         "messages": messages,
# #         "pending_agents": [],
# #         "next_agent": "",
# #         "complaint_id": prev_state.get("complaint_id") if prev_state else None,
# #         "auth": auth or None,
# #         "summary": prev_state.get("summary") if prev_state else None,
# #     }

# #     final_state = app.invoke(initial_state, {"recursion_limit": 20})
# #     final_message = final_state["messages"][-1]

# #     # prepare compact state to return
# #     new_msgs = _trim_messages(final_state["messages"], keep_last=history_keep_last)
# #     new_state = {
# #         "messages": new_msgs,
# #         "summary": final_state.get("summary"),
# #         "complaint_id": final_state.get("complaint_id"),
# #     }
# #     return {"content": final_message.content, "complaint_id": final_state.get("complaint_id"), "state": new_state}


# # def run_swarm_conversation(query: str, auth: Optional[Dict[str, str]] = None) -> dict:
# #     """
# #     Legacy one-shot call (no memory). Kept for backward compatibility.
# #     """
# #     initial_state = {
# #         "messages": [HumanMessage(content=query, name="User")],
# #         "pending_agents": [],
# #         "next_agent": "",
# #         "complaint_id": None,
# #         "auth": auth or None,
# #         "summary": None,
# #     }
# #     final_state = app.invoke(initial_state, {"recursion_limit": 20})
# #     final_message = final_state["messages"][-1]
# #     return {"content": final_message.content, "complaint_id": final_state.get("complaint_id")}


# def _trim_messages(msgs: List[BaseMessage], keep_last: int = 12) -> List[BaseMessage]:
#     """Keep only the last N messages to bound context size."""
#     if not msgs:
#         return []
#     return msgs[-keep_last:]

# def run_swarm_step(
#     query: str,
#     auth: Optional[Dict[str, str]] = None,
#     prev_state: Optional[Dict[str, Any]] = None,
#     history_keep_last: int = 12,
# ) -> Dict[str, Any]:
#     """
#     Single conversational step with optional previous memory.
#     prev_state supports keys: 'messages' (List[BaseMessage]) and 'summary' (str).
#     Returns: { content, complaint_id, state: {messages, summary, complaint_id} }
#     """
#     # rebuild trimmed history if present
#     messages: List[BaseMessage] = []
#     if prev_state and isinstance(prev_state.get("messages"), list):
#         messages = _trim_messages(prev_state["messages"], keep_last=history_keep_last)

#     # append the new human message
#     messages = messages + [HumanMessage(content=query, name="User")]

#     initial_state: AgentState = {
#         "messages": messages,
#         "pending_agents": [],
#         "next_agent": "",
#         "complaint_id": prev_state.get("complaint_id") if prev_state else None,
#         "auth": auth or None,
#         "summary": prev_state.get("summary") if prev_state else None,
#     }

#     final_state = app.invoke(initial_state, {"recursion_limit": 20})
#     final_message = final_state["messages"][-1]

#     # prepare compact state to return
#     new_msgs = _trim_messages(final_state["messages"], keep_last=history_keep_last)
#     new_state = {
#         "messages": new_msgs,
#         "summary": final_state.get("summary"),
#         "complaint_id": final_state.get("complaint_id"),
#     }
#     return {
#         "content": final_message.content,
#         "complaint_id": final_state.get("complaint_id"),
#         "state": new_state
#     }

# def run_swarm_conversation(query: str, auth: Optional[Dict[str, str]] = None) -> dict:
#     """
#     Legacy one-shot call (no memory). Kept for backward compatibility.
#     """
#     initial_state = {
#         "messages": [HumanMessage(content=query, name="User")],
#         "pending_agents": [],
#         "next_agent": "",
#         "complaint_id": None,
#         "auth": auth or None,
#         "summary": None,
#     }
#     final_state = app.invoke(initial_state, {"recursion_limit": 20})
#     final_message = final_state["messages"][-1]
#     return {"content": final_message.content, "complaint_id": final_state.get("complaint_id")}






























