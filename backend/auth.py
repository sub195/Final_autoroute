# backend/auth.py
import os, time
import pandas as pd
from io import StringIO
from typing import Optional
import bcrypt, jwt

from config import container_client

JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
JWT_ALG = os.getenv("JWT_ALG", "HS256")
BCRYPT_ROUNDS = int(os.getenv("BCRYPT_ROUNDS", "12"))

CUSTOMERS_BLOB = "customer_data/customers_data.csv"  # blob path

def _load_customers_df() -> pd.DataFrame:
    blob = container_client.get_blob_client(CUSTOMERS_BLOB)
    if not blob.exists():
        raise RuntimeError(f"Customers CSV not found at '{CUSTOMERS_BLOB}' in container '{container_client.container_name}'")
    raw = blob.download_blob().readall().decode("utf-8")
    df = pd.read_csv(StringIO(raw))
    if "CustomerID" in df.columns:
        df["CustomerID"] = df["CustomerID"].astype(str)
    return df

def _verify_password(plain: str, stored: str) -> bool:
    """
    Accepts either bcrypt hash (recommended) or plain text (for PoC only).
    """
    if not stored:
        return False
    stored = str(stored).strip()
    if stored.startswith("$2"):  # bcrypt marker
        try:
            return bcrypt.checkpw(plain.encode(), stored.encode())
        except Exception:
            return False
    return plain == stored  # fallback; do not use in prod

def issue_token(role: str, customer_id: Optional[str], ttl_sec: int = 3600) -> str:
    now = int(time.time())
    payload = {"role": role, "customer_id": str(customer_id) if customer_id else None, "iat": now, "exp": now + ttl_sec}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)

def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except Exception:
        return None

def login_verify(customer_id: str, password: str) -> dict:
    df = _load_customers_df()
    row = df[df["CustomerID"] == str(customer_id)]
    if row.empty:
        return {"ok": False, "error": "Invalid credentials"}
    rec = row.iloc[0].to_dict()
    stored = str(rec.get("Password", "")).strip()
    if not _verify_password(password, stored):
        return {"ok": False, "error": "Invalid credentials"}

    # mark admin if we want a special CustomerID (e.g., "admin" or "0")
    role = "admin" if str(customer_id).lower() in {"admin", "0"} else "customer"
    token = issue_token(role, str(customer_id))
    return {"ok": True, "token": token, "role": role, "customer_id": str(customer_id)}
