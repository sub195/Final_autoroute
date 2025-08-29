# tools/account_info.py
from typing import Optional, Tuple
import re
from datetime import date, datetime
from sqlalchemy import text
from config import get_sql_engine

# ---- Natural phrase → (table, column) based on the schema ----
_FIELD_MAP = {
    # customers_data
    "name": ("customers_data", "Name"),
    "contact": ("customers_data", "ContactInfo"),
    "contact info": ("customers_data", "ContactInfo"),
    "contactinfo": ("customers_data", "ContactInfo"),
    "language": ("customers_data", "LanguagePreference"),
    "language preference": ("customers_data", "LanguagePreference"),
    "languagepreference": ("customers_data", "LanguagePreference"),
    "kyc": ("customers_data", "KYCStatus"),
    "kyc status": ("customers_data", "KYCStatus"),
    "kycstatus": ("customers_data", "KYCStatus"),
    "aml": ("customers_data", "AMLStatus"),
    "aml status": ("customers_data", "AMLStatus"),
    "amlstatus": ("customers_data", "AMLStatus"),
    "gdpr": ("customers_data", "GDPRConsent"),
    "gdpr consent": ("customers_data", "GDPRConsent"),
    "dpdp": ("customers_data", "DPDPConsent"),
    "dpdp consent": ("customers_data", "DPDPConsent"),
    "dob": ("customers_data", "DateOfBirth"),
    "date of birth": ("customers_data", "DateOfBirth"),

    # accounts_data
    "balance": ("accounts_data", "Balance"),
    "available balance": ("accounts_data", "AvailableBalance"),
    "availablebalance": ("accounts_data", "AvailableBalance"),
    "account balance": ("accounts_data", "AvailableBalance"),
    "accountstatus": ("accounts_data", "AccountStatus"),
    "account status": ("accounts_data", "AccountStatus"),
    "account type": ("accounts_data", "AccountType"),
    "card status": ("accounts_data", "CardStatus"),
    "cardstatus": ("accounts_data", "CardStatus"),
    "card type": ("accounts_data", "CardType"),
    "channelopened": ("accounts_data", "ChannelOpened"),
    "channel opened": ("accounts_data", "ChannelOpened"),
    "emi status": ("accounts_data", "EMIStatus"),
    "em i status": ("accounts_data", "EMIStatus"),
    "last login": ("accounts_data", "LastLogin"),
    "lastlogin": ("accounts_data", "LastLogin"),
    "2fa": ("accounts_data", "TwoFAStatus"),
    "twofa": ("accounts_data", "TwoFAStatus"),
    "two factor": ("accounts_data", "TwoFAStatus"),
    "netbanking registered": ("accounts_data", "NetBankingRegistered"),
    "upi registered": ("accounts_data", "UPIRegistered"),
    "mobile app registered": ("accounts_data", "MobileAppRegistered"),
    "risk category": ("accounts_data", "RiskCategory"),
    "sanction check": ("accounts_data", "SanctionCheck"),
    "relationship manager": ("accounts_data", "RelationshipManagerID"),
    "nominee name": ("accounts_data", "NomineeName"),
    "nominee relation": ("accounts_data", "NomineeRelation"),
    "inactive since": ("accounts_data", "InactiveSince"),
    "last transaction date": ("accounts_data", "LastTransactionDate"),

    # extra helpful synonyms
    "2fa status": ("accounts_data", "TwoFAStatus"),
    "accountnumber": ("accounts_data", "AccountNumber"),
    "account number": ("accounts_data", "AccountNumber"),
    "available limit": ("accounts_data", "AvailableBalance"),
    "credit limit": ("accounts_data", "OverdraftLimit"),
}

def _normalize(text_in: str) -> str:
    t = (text_in or "").lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def _resolve_field(info_needed: str) -> Optional[Tuple[str, str]]:
    key = _normalize(info_needed)
    return _FIELD_MAP.get(key)

def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, (date, datetime)):
        # using (YYYY-MM-DD HH:MM:SS)
        return v.isoformat(sep=" ")
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return str(v)

def _query_customers_single(customer_id: str, column: str) -> Optional[str]:
    engine = get_sql_engine()
    if not engine:
        raise RuntimeError("SQL engine not available.")
    sql = text(f"SELECT TOP (1) [{column}] AS val FROM dbo.customers_data WHERE CustomerID = :cid")
    with engine.connect() as conn:
        row = conn.execute(sql, {"cid": customer_id}).fetchone()
        if not row:
            return None
        return _fmt(row.val)

def _query_accounts_latest(customer_id: str, column: str) -> Optional[str]:
    """
    Picks the most recent account row (UpdatedAt, else LastTransactionDate, else DateOpened).
    """
    engine = get_sql_engine()
    if not engine:
        raise RuntimeError("SQL engine not available.")
    sql = text(f"""
        SELECT TOP (1) [{column}] AS val
        FROM dbo.accounts_data
        WHERE CustomerID = :cid
        ORDER BY 
          COALESCE([UpdatedAt], [LastTransactionDate], [DateOpened]) DESC,
          [AccountID] DESC
    """)
    with engine.connect() as conn:
        row = conn.execute(sql, {"cid": customer_id}).fetchone()
        if not row:
            return None
        return _fmt(row.val)

def get_account_info(customer_id: str, info_needed: str) -> str:
    """
    SQL-only lookup of a single field for a customer.
    Returns a short message "<Column> for CustomerID X: <value>" or a safe not-found note.
    """
    if not customer_id:
        return "Missing customer_id."
    resolved = _resolve_field(info_needed)
    if not resolved:
        return f"I couldn’t map '{info_needed}' to a known field."
    table, column = resolved

    try:
        if table == "customers_data":
            val = _query_customers_single(customer_id, column)
        elif table == "accounts_data":
            val = _query_accounts_latest(customer_id, column)
        else:
            return f"Unsupported table for '{info_needed}'."

        if val is None or val == "":
            return f"I could not find {column} for CustomerID {customer_id}."
        return f"{column} for CustomerID {customer_id}: {val}"
    except Exception:
        # Don’t leak internals to the user; logging server-side if needed
        return f"Sorry, I couldn’t retrieve {column} for CustomerID {customer_id}."


















# # tools/account_info.py
# from typing import Optional, Tuple
# import re
# from sqlalchemy import text
# from config import get_sql_engine

# # ---- Natural phrase → (table, column) based on your schema ----
# _FIELD_MAP = {
#     # customers_data
#     "name": ("customers_data", "Name"),
#     "contact": ("customers_data", "ContactInfo"),
#     "contact info": ("customers_data", "ContactInfo"),
#     "contactinfo": ("customers_data", "ContactInfo"),
#     "language": ("customers_data", "LanguagePreference"),
#     "language preference": ("customers_data", "LanguagePreference"),
#     "languagepreference": ("customers_data", "LanguagePreference"),
#     "kyc": ("customers_data", "KYCStatus"),
#     "kyc status": ("customers_data", "KYCStatus"),
#     "kycstatus": ("customers_data", "KYCStatus"),
#     "aml": ("customers_data", "AMLStatus"),
#     "aml status": ("customers_data", "AMLStatus"),
#     "amlstatus": ("customers_data", "AMLStatus"),
#     "gdpr": ("customers_data", "GDPRConsent"),
#     "gdpr consent": ("customers_data", "GDPRConsent"),
#     "dpdp": ("customers_data", "DPDPConsent"),
#     "dpdp consent": ("customers_data", "DPDPConsent"),
#     "dob": ("customers_data", "DateOfBirth"),
#     "date of birth": ("customers_data", "DateOfBirth"),

#     # accounts_data
#     "balance": ("accounts_data", "Balance"),
#     "available balance": ("accounts_data", "AvailableBalance"),
#     "availablebalance": ("accounts_data", "AvailableBalance"),
#     "account balance": ("accounts_data", "AvailableBalance"),
#     "accountstatus": ("accounts_data", "AccountStatus"),
#     "account status": ("accounts_data", "AccountStatus"),
#     "account type": ("accounts_data", "AccountType"),
#     "card status": ("accounts_data", "CardStatus"),
#     "cardstatus": ("accounts_data", "CardStatus"),
#     "card type": ("accounts_data", "CardType"),
#     "channelopened": ("accounts_data", "ChannelOpened"),
#     "channel opened": ("accounts_data", "ChannelOpened"),
#     "emi status": ("accounts_data", "EMIStatus"),
#     "em i status": ("accounts_data", "EMIStatus"),
#     "last login": ("accounts_data", "LastLogin"),
#     "lastlogin": ("accounts_data", "LastLogin"),
#     "2fa": ("accounts_data", "TwoFAStatus"),
#     "twofa": ("accounts_data", "TwoFAStatus"),
#     "two factor": ("accounts_data", "TwoFAStatus"),
#     "netbanking registered": ("accounts_data", "NetBankingRegistered"),
#     "upi registered": ("accounts_data", "UPIRegistered"),
#     "mobile app registered": ("accounts_data", "MobileAppRegistered"),
#     "risk category": ("accounts_data", "RiskCategory"),
#     "sanction check": ("accounts_data", "SanctionCheck"),
#     "relationship manager": ("accounts_data", "RelationshipManagerID"),
#     "nominee name": ("accounts_data", "NomineeName"),
#     "nominee relation": ("accounts_data", "NomineeRelation"),
#     "inactive since": ("accounts_data", "InactiveSince"),
#     "last transaction date": ("accounts_data", "LastTransactionDate"),
# }

# def _normalize(text_in: str) -> str:
#     t = (text_in or "").lower()
#     t = re.sub(r"[^a-z0-9\s]", " ", t)
#     t = re.sub(r"\s+", " ", t).strip()
#     return t

# def _resolve_field(info_needed: str) -> Optional[Tuple[str, str]]:
#     key = _normalize(info_needed)
#     return _FIELD_MAP.get(key)

# def _query_customers_single(customer_id: str, column: str) -> Optional[str]:
#     engine = get_sql_engine()
#     if not engine:
#         raise RuntimeError("SQL engine not available.")
#     sql = text(f"SELECT TOP (1) {column} AS val FROM dbo.customers_data WHERE CustomerID = :cid")
#     with engine.connect() as conn:
#         row = conn.execute(sql, {"cid": customer_id}).fetchone()
#         if not row:
#             return None
#         return "" if row.val is None else str(row.val)

# def _query_accounts_latest(customer_id: str, column: str) -> Optional[str]:
#     """
#     Picks the most recent account row (UpdatedAt, else LastTransactionDate, else DateOpened).
#     """
#     engine = get_sql_engine()
#     if not engine:
#         raise RuntimeError("SQL engine not available.")
#     sql = text(f"""
#         SELECT TOP (1) {column} AS val
#         FROM dbo.accounts_data
#         WHERE CustomerID = :cid
#         ORDER BY 
#           COALESCE(UpdatedAt, LastTransactionDate, DateOpened) DESC,
#           AccountID DESC
#     """)
#     with engine.connect() as conn:
#         row = conn.execute(sql, {"cid": customer_id}).fetchone()
#         if not row:
#             return None
#         return "" if row.val is None else str(row.val)

# def get_account_info(customer_id: str, info_needed: str) -> str:
#     """
#     SQL-only lookup of a single field for a customer.
#     Returns a short message "<Column> for CustomerID X: <value>" or a safe not-found note.
#     """
#     if not customer_id:
#         return "Missing customer_id."
#     resolved = _resolve_field(info_needed)
#     if not resolved:
#         return f"I couldn’t map '{info_needed}' to a known field."
#     table, column = resolved

#     try:
#         if table == "customers_data":
#             val = _query_customers_single(customer_id, column)
#         elif table == "accounts_data":
#             val = _query_accounts_latest(customer_id, column)
#         else:
#             return f"Unsupported table for '{info_needed}'."

#         if val is None:
#             return f"I could not find {column} for CustomerID {customer_id}."
#         return f"{column} for CustomerID {customer_id}: {val}"
#     except Exception as e:
#         # Don’t leak internals to the user; log in server if needed
#         return f"Sorry, I couldn’t retrieve {column} for CustomerID {customer_id}."
















# import pandas as pd
# from io import StringIO
# from config import container_client
# from request_context import get_context

# def get_account_info(customer_id: str, info_needed: str) -> str:
#     """
#     Retrieves specific information for a given customer from customers_data.csv in Blob.
#     RBAC:
#       - customer role: can only query their own CustomerID
#       - admin role: can query any customer
#     """
#     print(f"--- TOOL: Getting account info for customer '{customer_id}', field '{info_needed}' ---")
#     try:
#         ctx = get_context()
#         role = ctx.get("role", "customer")
#         requester_cid = str(ctx.get("customer_id") or "").strip()
#         if role != "admin" and str(customer_id).strip() != requester_cid:
#             return "Access denied: you can only view your own account information."

#         blob_name = "customer_data/customers_data.csv"
#         blob_client = container_client.get_blob_client(blob_name)
#         if not blob_client.exists():
#             return "Error: Customer data file not found."

#         df = pd.read_csv(StringIO(blob_client.download_blob().readall().decode('utf-8')))
#         if "CustomerID" not in df.columns:
#             return "Error: CSV missing 'CustomerID' column."
#         df["CustomerID"] = df["CustomerID"].astype(str)

#         customer_record = df[df['CustomerID'] == str(customer_id)]
#         if customer_record.empty:
#             return f"Error: Customer with ID '{customer_id}' not found."

#         if info_needed in customer_record.columns:
#             value = customer_record.iloc[0][info_needed]
#             return f"The {info_needed.replace('_', ' ')} for customer {customer_id} is: {value}"
#         else:
#             return f"Error: Could not find the field '{info_needed}' for customer {customer_id}."
#     except Exception as e:
#         print(f"Error getting account info: {e}")
#         return "An error occurred while retrieving account information."


















# import pandas as pd
# from io import StringIO
# from config import container_client

# # This function is a "tool" that the Account_Info_Agent can use.
# def get_account_info(customer_id: str, info_needed: str) -> str:
#     """
#     Retrieves specific information for a given customer from a CSV file
#     stored in Azure Blob Storage. This simulates a database lookup.
#     """
#     print(f"--- TOOL: Getting account info for customer '{customer_id}', field '{info_needed}' ---")
    
#     try:
#         # 1. Download the main customer data CSV file from Blob Storage.
#         blob_name = "customer_data/customers_data.csv"
#         blob_client = container_client.get_blob_client(blob_name)
        
#         if not blob_client.exists():
#             return "Error: Customer data file not found."
            
#         downloader = blob_client.download_blob()
#         blob_bytes = downloader.readall()
        
#         # 2. Load the CSV data into a pandas DataFrame.
#         df = pd.read_csv(StringIO(blob_bytes.decode('utf-8')))
        
#         # 3. Find the specific customer's record.
#         customer_record = df[df['CustomerID'] == customer_id]
        
#         if customer_record.empty:
#             return f"Error: Customer with ID '{customer_id}' not found."
            
#         # 4. Extract and return the requested information.
#         if info_needed in customer_record.columns:
#             value = customer_record.iloc[0][info_needed]
#             return f"The {info_needed.replace('_', ' ')} for customer {customer_id} is: {value}"
#         else:
#             return f"Error: Could not find the field '{info_needed}' for customer {customer_id}."

#     except Exception as e:
#         print(f"Error getting account info: {e}")
#         return "An error occurred while retrieving account information."