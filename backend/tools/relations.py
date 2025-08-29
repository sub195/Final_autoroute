# tools/relations.py
from __future__ import annotations
import os, urllib.parse
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from request_context import get_context  # we use this in graph as well

VALID_TABLES = {
    "customers_data": {"pk": "CustomerID", "columns": {
        "CustomerID","Name","ContactInfo","LanguagePreference","KYCStatus","AMLStatus",
        "GDPRConsent","DPDPConsent","DateOfBirth"
    }},
    "accounts_data": {"pk": "CustomerID", "columns": {
        "AccountID","CustomerID","BranchID","AccountNumber","AccountType","AccountStatus",
        "DateOpened","DateClosed","ClosureReason","Balance","AvailableBalance","Currency",
        "OverdraftLimit","InterestRate","MinimumBalanceRequirement","PenaltyCharges","CardType",
        "CardStatus","CardIssueDate","CardExpiryDate","LoanStatus","LoanType","LoanAmount",
        "OutstandingLoanBalance","EMIStatus","NextEMIDate","MobileAppRegistered","UPIRegistered",
        "NetBankingRegistered","LastLogin","TwoFAStatus","LoginFailureCount","DeviceLinked",
        "KYCStatus","AMLFlag","RiskCategory","SanctionCheck","RelationshipManagerID",
        "ChannelOpened","NomineeName","NomineeRelation","InsuranceLinked","LockerLinked",
        "CreatedAt","UpdatedAt","LastTransactionDate","InactiveSince"
    }},
    "complaints_data": {"pk": "CustomerID", "columns": {
        "ComplaintID","CustomerID","DateLogged","IssueType","Status","EscalationLevel"
    }},
}
TABLE_SYNONYMS = {"customers":"customers_data","accounts":"accounts_data","complaints":"complaints_data"}

_SQL_ENGINE: Optional[Engine] = None

def _engine() -> Engine:
    global _SQL_ENGINE
    if _SQL_ENGINE: return _SQL_ENGINE
    # Prefer a full SQLAlchemy URL if provided (e.g., mssql+pymssql://...)
    url = os.getenv("SQLALCHEMY_URL", "").strip()
    if not url:
        # Fallback to ODBC connection string (ODBC Driver 18 recommended)
        conn = os.getenv("AZURE_SQL_CONNECTION_STRING", "").strip()
        if not conn:
            raise RuntimeError("Set SQLALCHEMY_URL or AZURE_SQL_CONNECTION_STRING in env.")
        url = "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(conn)
    _SQL_ENGINE = create_engine(url, pool_pre_ping=True, fast_executemany=True)
    # ensuring test
    with _SQL_ENGINE.connect() as c: c.execute(text("SELECT 1"))
    return _SQL_ENGINE

def _normalize_table(name: str) -> str:
    k = (name or "").strip().lower()
    return TABLE_SYNONYMS.get(k, k)

def _validate(table: str, select: List[str] | None):
    if table not in VALID_TABLES:
        raise ValueError(f"Invalid table '{table}'.")
    if select:
        bad = [c for c in select if c not in VALID_TABLES[table]["columns"]]
        if bad: raise ValueError(f"Unknown columns for {table}: {', '.join(bad)}")

def _apply_rbac(table: str, where: Dict[str, Any] | None) -> Dict[str, Any]:
    where = dict(where or {})
    ctx = get_context() or {}
    role = (ctx.get("role") or "anonymous").lower()
    cid = ctx.get("customer_id")
    pk = VALID_TABLES[table]["pk"]
    if role == "customer":
        if not cid: raise PermissionError("Sign in required.")
        # forcing WHERE pk = customer_id
        where[pk] = {"eq": str(cid)}
    elif role != "admin":
        raise PermissionError("Sign in required.")
    return where

def _build_where(table: str, where: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    clauses, params, i = [], {}, 0
    cols = VALID_TABLES[table]["columns"]
    for col, cond in (where or {}).items():
        if col not in cols: continue
        if not isinstance(cond, dict):
            i+=1; p=f"p{i}"; clauses.append(f"[{col}] = :{p}"); params[p]=cond; continue
        for op, val in cond.items():
            i+=1; p=f"p{i}"
            if op == "eq":  clauses.append(f"[{col}] = :{p}"); params[p]=val
            elif op=="in" and isinstance(val,(list,tuple)) and val:
                names=[]; 
                for j,v in enumerate(val): pj=f"{p}_{j}"; names.append(f":{pj}"); params[pj]=v
                clauses.append(f"[{col}] IN ({', '.join(names)})")
            elif op=="like": clauses.append(f"[{col}] LIKE :{p}"); params[p]=str(val)
            elif op=="gt":   clauses.append(f"[{col}] > :{p}"); params[p]=val
            elif op=="gte":  clauses.append(f"[{col}] >= :{p}"); params[p]=val
            elif op=="lt":   clauses.append(f"[{col}] < :{p}"); params[p]=val
            elif op=="lte":  clauses.append(f"[{col}] <= :{p}"); params[p]=val
            else: i-=1
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

def query_relations(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(spec, dict): raise ValueError("spec must be a dict")
    table = _normalize_table(spec.get("table",""))
    select = spec.get("select")
    _validate(table, select)
    where = _apply_rbac(table, spec.get("where"))
    order_by = spec.get("order_by") or []
    limit = spec.get("limit")
    try:
        limit = int(limit) if limit is not None else None
    except Exception:
        limit = None

    cols = ", ".join(f"[{c}]" for c in (select or VALID_TABLES[table]["columns"]))
    where_sql, params = _build_where(table, where)
    order_sql = ""
    if order_by:
        items=[]
        for ob in order_by:
            c = ob.get("column"); d=(ob.get("direction") or "asc").lower()
            if c in VALID_TABLES[table]["columns"]:
                items.append(f"[{c}] {'DESC' if d.startswith('d') else 'ASC'}")
        if items: order_sql = " ORDER BY " + ", ".join(items)
    top_sql = f"TOP {limit} " if (isinstance(limit,int) and limit>0) else ""
    sql = text(f"SELECT {top_sql}{cols} FROM [{table}]{where_sql}{order_sql}")

    with _engine().begin() as conn:
        rows = conn.execute(sql, params).mappings().all()
        return [dict(r) for r in rows]















