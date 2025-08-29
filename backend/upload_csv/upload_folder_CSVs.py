import os
import sys
import glob
from dotenv import load_dotenv
from azure.storage.blob import BlobServiceClient, ContentSettings

# Canonical blob paths our backend expects
BLOB_CUSTOMERS   = "customer_data/customers_data.csv"
BLOB_ACCOUNTS    = "accounts_data/accounts_data.csv"
BLOB_COMPLAINTS  = "complaints_data/complaints_data.csv"

def log(msg: str):
    print(f"[CSV-UP] {msg}")

def fail(msg: str):
    log(f"ERROR: {msg}")
    sys.exit(1)

def find_csv_by_keywords(root: str, any_of: list[str]) -> str | None:
    """
    Return the first *.csv file in 'root' whose filename contains ANY of the given keywords.
    Matching is case-insensitive and only checks the base filename.
    """
    for path in glob.glob(os.path.join(root, "*.csv")):
        name = os.path.basename(path).lower()
        if any(k.lower() in name for k in any_of):
            return path
    return None

def main():
    # Load .env from the same folder as this script (fallback to env if absent)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(script_dir, ".env"))

    conn_str = os.getenv("AZURE_BLOB_CONNECTION_STRING") or os.getenv("BLOB_CONNECTION_STRING")
    container_name = os.getenv("BLOB_CONTAINER_NAME")
    if not conn_str:
        fail("Missing AZURE_BLOB_CONNECTION_STRING (or BLOB_CONNECTION_STRING). Put it in .env next to this script.")
    if not container_name:
        fail("Missing BLOB_CONTAINER_NAME in .env (e.g., checkrouterai).")

    # Auto-detect local CSVs (ANY keyword match is enough)
    customers_path  = find_csv_by_keywords(script_dir, ["customer", "customers"])
    accounts_path   = find_csv_by_keywords(script_dir, ["account", "accounts"])
    complaints_path = find_csv_by_keywords(script_dir, ["complaint", "complaints", "ticket", "tickets", "grievance"])

    # Validation
    missing = []
    if not customers_path:  missing.append("customers*.csv")
    if not accounts_path:   missing.append("accounts*.csv")
    if not complaints_path: missing.append("complaints* / tickets* / grievance*.csv")
    if missing:
        fail("Missing required CSV(s) in this folder: " + ", ".join(missing))

    log(f"Detected files:\n  customers -> {os.path.basename(customers_path)}\n"
        f"  accounts  -> {os.path.basename(accounts_path)}\n"
        f"  complaints-> {os.path.basename(complaints_path)}")

    # Connect to Azure Blob
    blob_service = BlobServiceClient.from_connection_string(conn_str)
    container = blob_service.get_container_client(container_name)
    try:
        container.create_container()
        log(f"Created container '{container_name}'.")
    except Exception:
        pass  # already exists

    def upload_file(local_path: str, blob_path: str):
        with open(local_path, "rb") as f:
            data = f.read()
        container.upload_blob(
            name=blob_path,
            data=data,
            overwrite=True,
            content_settings=ContentSettings(content_type="text/csv; charset=utf-8"),
        )
        log(f"Uploaded -> {blob_path} ({len(data)} bytes)")

    # Upload to canonical locations expected by backend
    upload_file(customers_path,  BLOB_CUSTOMERS)
    upload_file(accounts_path,   BLOB_ACCOUNTS)
    upload_file(complaints_path, BLOB_COMPLAINTS)

    # Quick verify: fetch first bytes back
    for blob_path in (BLOB_CUSTOMERS, BLOB_ACCOUNTS, BLOB_COMPLAINTS):
        try:
            head = container.download_blob(blob_path, offset=0, length=256).readall()
            preview = head.decode("utf-8", errors="ignore").replace("\n", "\\n")
            log(f"Verified {blob_path}: {preview[:120]}{'...' if len(preview) > 120 else ''}")
        except Exception as e:
            log(f"Verify failed for {blob_path}: {e}")

    log("🎉 Done. All CSVs uploaded to canonical paths.")

if __name__ == "__main__":
    main()
