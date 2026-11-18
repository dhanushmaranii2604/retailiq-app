import sqlite3
import os
from pathlib import Path

# Detect Render environment
if os.getenv("RENDER"):
    DB_PATH = Path("/opt/render/project/src/data/retailiq.db")
else:
    DB_PATH = Path(__file__).resolve().parent / "data" / "retailiq.db"

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE IF NOT EXISTS credits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_name TEXT,
        amount REAL,
        phone TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )
''')
conn.commit()
conn.close()
print("✅ Udhaar table created successfully!")