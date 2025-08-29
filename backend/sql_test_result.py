
# sql_test_result.py
from config import get_sql_engine
from sqlalchemy import text

e = get_sql_engine()
assert e, "Engine is None – check SQLALCHEMY_URL / firewall"

with e.connect() as c:
    print(c.execute(text("SELECT DB_NAME() as dbname")).mappings().all())
    print(c.execute(text("SELECT TOP 1 * FROM dbo.customers_data")).fetchone())
