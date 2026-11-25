import hmac
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

load_dotenv()
router = APIRouter()
ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "retailiq.db"
IST = timezone(timedelta(hours=5, minutes=30))
bad = []


def db():
    c = sqlite3.connect(str(DB))
    c.execute("CREATE TABLE IF NOT EXISTS stock_ledger (id INTEGER PRIMARY KEY, created_at TEXT, "
              "product_id INTEGER, delta INTEGER, reason TEXT, ref TEXT)")
    if not any(r[1] == "bill_id" for r in c.execute("PRAGMA table_info(sales)")):
        c.execute("ALTER TABLE sales ADD COLUMN bill_id TEXT")
    return c


def ledger(c, pid, delta, reason, ref=""):
    c.execute("INSERT INTO stock_ledger (created_at, product_id, delta, reason, ref) VALUES (?,?,?,?,?)",
              (datetime.now(IST).isoformat(), pid, delta, reason, ref))


def check(pin):
    now = time.time()
    bad[:] = [t for t in bad if now - t < 300]
    real = os.getenv("APP_PIN", "")
    if not real:
        raise HTTPException(400, "Set APP_PIN in .env and restart the server")
    if len(bad) >= 5:
        raise HTTPException(429, "Too many wrong PINs. Wait 5 minutes.")
    if not hmac.compare_digest(pin or "", real):
        bad.append(now)
        raise HTTPException(401, "Wrong PIN")


class P(BaseModel):
    name: str
    category: str = ""
    price: float
    cost_price: float = 0
    stock: int = 0


class Price(BaseModel):
    price: float
    cost_price: float


class Stock(BaseModel):
    product_id: int
    delta: int


class Item(BaseModel):
    product_id: int
    qty: int


class Bill(BaseModel):
    items: list[Item]


@router.get("/manage", response_class=HTMLResponse)
async def page():
    return (ROOT / "manage.html").read_text(encoding="utf-8")


@router.get("/api/products")
async def products():
    c = db()
    try:
        rows = c.execute("SELECT id, name, category, price, cost_price, stock FROM products ORDER BY name").fetchall()
    finally:
        c.close()
    return [dict(zip(("id", "name", "category", "price", "cost_price", "stock"), r)) for r in rows]


@router.post("/api/products")
async def add_product(p: P, x_pin: str = Header("")):
    check(x_pin)
    name = p.name.strip()[:80]
    if not name or p.price < 0 or p.cost_price < 0 or p.stock < 0:
        raise HTTPException(400, "Check the values")
    c = db()
    try:
        if c.execute("SELECT 1 FROM products WHERE lower(name)=lower(?)", (name,)).fetchone():
            raise HTTPException(400, "Product already exists")
        cur = c.execute("INSERT INTO products (name, category, price, stock, cost_price) VALUES (?,?,?,?,?)",
                        (name, p.category.strip()[:40], p.price, p.stock, p.cost_price))
        if p.stock:
            ledger(c, cur.lastrowid, p.stock, "opening stock")
        c.commit()
    finally:
        c.close()
    return {"ok": True}


@router.put("/api/products/{pid}")
async def set_price(pid: int, p: Price, x_pin: str = Header("")):
    check(x_pin)
    if p.price < 0 or p.cost_price < 0:
        raise HTTPException(400, "Check the values")
    c = db()
    try:
        c.execute("UPDATE products SET price=?, cost_price=? WHERE id=?", (p.price, p.cost_price, pid))
        c.commit()
    finally:
        c.close()
    return {"ok": True}


@router.post("/api/stock")
async def add_stock(s: Stock, x_pin: str = Header("")):
    check(x_pin)
    c = db()
    try:
        r = c.execute("SELECT stock FROM products WHERE id=?", (s.product_id,)).fetchone()
        if not r or r[0] + s.delta < 0:
            raise HTTPException(400, "Stock cannot go below zero")
        c.execute("UPDATE products SET stock=stock+? WHERE id=?", (s.delta, s.product_id))
        ledger(c, s.product_id, s.delta, "manual adjustment")
        c.commit()
    finally:
        c.close()
    return {"ok": True}


@router.post("/api/sale")
async def sale(b: Bill, x_pin: str = Header("")):
    check(x_pin)
    if not b.items or len(b.items) > 50:
        raise HTTPException(400, "Add at least one item")
    c = db()
    try:
        now = datetime.now(IST)
        row = c.execute("SELECT sale_date FROM sales WHERE sale_date IS NOT NULL LIMIT 1").fetchone()
        fmt = "%Y-%m-%d %H:%M:%S" if row and len(str(row[0])) > 10 else "%Y-%m-%d"
        bill, total = uuid.uuid4().hex[:8], 0
        for it in b.items:
            r = c.execute("SELECT name, price, stock FROM products WHERE id=?", (it.product_id,)).fetchone()
            if not r or it.qty <= 0:
                raise HTTPException(400, "Invalid item")
            if r[2] < it.qty:
                raise HTTPException(400, "Not enough stock for %s (have %d)" % (r[0], r[2]))
            c.execute("INSERT INTO sales (product_id, quantity, unit_price, sale_date, bill_id) VALUES (?,?,?,?,?)",
                      (it.product_id, it.qty, r[1], now.strftime(fmt), bill))
            c.execute("UPDATE products SET stock=stock-? WHERE id=?", (it.qty, it.product_id))
            ledger(c, it.product_id, -it.qty, "sale", bill)
            total += r[1] * it.qty
        c.commit()
    except HTTPException:
        c.rollback()
        raise
    finally:
        c.close()
    return {"ok": True, "bill": bill, "total": total}
