# backend/request_context.py
import contextvars

# This Holds per-request context so tools can enforce RBAC (role/customer_id)
_current = contextvars.ContextVar(
    "current_request_context",
    default={"role": "customer", "customer_id": None},
)

def set_context(role: str, customer_id: str | None):
    _current.set({
        "role": role,
        "customer_id": str(customer_id) if customer_id is not None else None
    })

def get_context() -> dict:
    return _current.get()
