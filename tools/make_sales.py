import random
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
shutil.copy("data/retailiq.db", "data/retailiq_backup2.db")
db = sqlite3.connect("data/retailiq.db")
row = db.execute("select sale_date from sales limit 1").fetchone()
with_time = bool(row and len(str(row[0])) > 10)
prods = db.execute("select id, price from products").fetchall()
db.execute("delete from sales")
now = datetime.now(IST)
n = 0
for back in range(7):
    day = now - timedelta(days=back)
    for _ in range(random.randint(8, 14)):
        pid, price = random.choice(prods)
        top = max(10, min(20, now.hour)) if back == 0 else 20
        d = day.replace(hour=random.randint(10, top), minute=random.randint(0, 59), second=0)
        fmt = "%Y-%m-%d %H:%M:%S" if with_time else "%Y-%m-%d"
        db.execute(
            "insert into sales (product_id, quantity, unit_price, sale_date) values (?,?,?,?)",
            (pid, random.randint(1, 3), price, d.strftime(fmt)),
        )
        n += 1
db.commit()
print("Added", n, "sample sales for the last 7 days")