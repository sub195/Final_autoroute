# backend/tracking.py
import datetime, json
from config import container_client

# simple blob paths
LLM_LOG_BLOB = "logs/llm_events.jsonl"
TOOL_LOG_BLOB = "logs/tool_events.jsonl"

def _append_to_blob(blob_name: str, record: dict):
    """Append JSONL record to a blob."""
    try:
        try:
            existing = container_client.download_blob(blob_name).readall().decode("utf-8")
        except Exception:
            existing = ""
        line = json.dumps(record, ensure_ascii=False) + "\n"
        container_client.upload_blob(blob_name, existing + line, overwrite=True)
    except Exception as e:
        print(f"[tracking] Failed to append to {blob_name}: {e}")

def add_llm_event(prompt_messages, response_message, started_at, ended_at, **kwargs):
    record = {
        "ts": datetime.datetime.utcnow().isoformat(),
        "prompt": [getattr(m, "content", str(m)) for m in prompt_messages],
        "response": getattr(response_message, "content", str(response_message)),
        "duration": round(ended_at - started_at, 3),
    }
    record.update(kwargs)
    _append_to_blob(LLM_LOG_BLOB, record)

def add_tool_event(tool, args, started_at, ended_at, ok, **kwargs):
    record = {
        "ts": datetime.datetime.utcnow().isoformat(),
        "tool": tool,
        "args": args,
        "ok": ok,
        "duration": round(ended_at - started_at, 3),
    }
    record.update(kwargs)
    _append_to_blob(TOOL_LOG_BLOB, record)










