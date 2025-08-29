import pyodbc

conn_str = (
    "Driver={ODBC Driver 18 for SQL Server};"
    "Server=tcp:<server>.database.windows.net,1433;"
    "Database=<db>;"
    "Uid=<user>;"
    "Pwd=<password>;"
    "Encrypt=yes;"
    "TrustServerCertificate=no;"
    "Connection Timeout=30;"
)

try:
    conn = pyodbc.connect(conn_str)
    cursor = conn.cursor()
    cursor.execute("SELECT TOP 5 name FROM sys.tables;")
    for row in cursor.fetchall():
        print(row)
    conn.close()
    print("✅ Connection successful!")
except Exception as e:
    print("❌ Connection failed:", e)
