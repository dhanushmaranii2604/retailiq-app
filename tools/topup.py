import random, shutil, sqlite3
from datetime import datetime, timedelta, timezone
IST = timezone(timedelta(hours=5, minutes=30))
shutil.copy("data/retailiq.db", "data/retailiq_backup3.db")
db = sqlite3.connect("data/retailiq.db")
last = db.execute("select max(substr(sale_date,1,10)) from sales").fetchone()[0]
row = db.execute("select sale_date from sales limit 1").fetchone()
fmt = "%Y-%m-%d %H:%M:%S" if row and len(str(row[0])) > 10 else "%Y-%m-%d"
prods = db.execute("select id, price from products").fetchall()
now = datetime.now(IST)
day = datetime.strptime(last, "%Y-%m-%d").replace(tzinfo=IST) + timedelta(days=1)
n = 0
while day.date() <= now.date():
    for _ in range(random.randint(8, 14)):
        pid, price = random.choice(prods)
        t = day.replace(hour=random.randint(10, 20), minute=random.randint(0, 59))
        db.execute("insert into sales (product_id, quantity, unit_price, sale_date) values (?,?,?,?)", (pid, random.randint(1, 3), price, t.strftime(fmt)))
        n += 1
    day += timedelta(days=1)
db.commit()
print("Added", n)