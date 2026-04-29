# backend/guardrails/nemo_client.py
"""
NeMo Guardrails integration for AutoRouteAI.

Replaces Azure Content Safety. Runs INPUT rails on every user turn:
  - Colang rule-based flows (prompt injection, credential leaks,
    bank-data exfiltration attempts) -> fast, deterministic, cheap.
  - LLM-based `self_check_input` rail -> catch-all for novel attacks,
    abusive content, and off-topic queries.

Reuses the AzureChatOpenAI client already configured in backend/config.py
so we don't duplicate Azure credentials or retry/timeout settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_CONFIG_DIR = Path(__file__).parent / "config"

_rails = None          # lazy singleton
_init_error: Optional[str] = None


@dataclass
class GuardrailResult:
    allowed: bool          # True = safe, False = blocked
    reason: str            # "ok" | "rail_triggered" | "self_check_input" | "error:..."
    bot_response: str      # canned refusal text to surface to the user when blocked


# ---------------------------------------------------------------------------
# Refusal markers used to detect that an input rail fired.
# Keep these phrases in sync with rails.co and the self_check_input default.
# ---------------------------------------------------------------------------
_REFUSAL_MARKERS = (
    "i'm unable to process that request",
    "please do not share passwords",
    "i can only assist you with your own account",
    "i can only help with banking-related",
    # NeMo's default self_check_input refusal
    "i'm sorry, i can't respond to that",
    "i am not able to respond",
)


def _load_rails():
    """Initialize LLMRails once. Reuses the Azure LLM from config.py."""
    global _rails, _init_error
    if _rails is not None or _init_error is not None:
        return _rails

    try:
        from nemoguardrails import LLMRails, RailsConfig
        from config import llm as azure_llm  # shared AzureChatOpenAI

        config = RailsConfig.from_path(str(_CONFIG_DIR))
        _rails = LLMRails(config, llm=azure_llm)
        print("[Guardrails] NeMo Guardrails initialized from", _CONFIG_DIR)
        return _rails
    except Exception as e:
        _init_error = str(e)
        print(f"[Guardrails] Failed to initialize NeMo Guardrails: {e}")
        return None


def _looks_like_refusal(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    return any(marker in t for marker in _REFUSAL_MARKERS)


def check_input_guardrails(user_message: str) -> GuardrailResult:
    """
    Run NeMo Guardrails input rails against a user message.

    Fail-open policy: if the guardrail service itself errors (config load,
    network, etc.) we let the message through and log, rather than blocking
    all banking traffic on a rails outage. The orchestrator's downstream
    classifier + RBAC provide defense in depth.
    """
    if not user_message or not user_message.strip():
        return GuardrailResult(allowed=True, reason="ok", bot_response="")

    rails = _load_rails()
    if rails is None:
        return GuardrailResult(
            allowed=True,
            reason=f"error:init:{_init_error}",
            bot_response="",
        )

    try:
        result = rails.generate(
            messages=[{"role": "user", "content": user_message}]
        )
        # NeMo returns {"role": "assistant", "content": "..."} in recent versions
        if isinstance(result, dict):
            bot_text = result.get("content", "") or ""
        else:
            bot_text = str(result or "")

        if _looks_like_refusal(bot_text):
            return GuardrailResult(
                allowed=False,
                reason="rail_triggered",
                bot_response=bot_text.strip(),
            )

        return GuardrailResult(allowed=True, reason="ok", bot_response="")
    except Exception as e:
        print(f"[Guardrails] generate() failed: {e}")
        return GuardrailResult(
            allowed=True,
            reason=f"error:generate:{e}",
            bot_response="",
        )
