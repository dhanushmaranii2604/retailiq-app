import asyncio
import logging
import math
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from difflib import get_close_matches
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

load_dotenv()
log = logging.getLogger("retailiq-autopilot")
ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "retailiq.db"
IST = timezone(timedelta(hours=5, minutes=30))
RUN_HOUR = 10       # daily check time (10 AM)
THRESHOLD = 5       # reorder when stock is at or below this
COVER_DAYS = 14     # order enough for this many days of sales
MAX_PER_RUN = 8     # most items per run


def owner():
    v = os.getenv("TELEGRAM_OWNER_ID", "").strip()
    return int(v) if v.isdigit() else None


def connect():
    db = sqlite3.connect(str(DB))
    db.execute(
        "CREATE TABLE IF NOT EXISTS reorders (id INTEGER PRIMARY KEY, created_at TEXT, "
        "product_id INTEGER, qty INTEGER, est_cost REAL, status TEXT, po_file TEXT)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS automation_log (id INTEGER PRIMARY KEY, created_at TEXT, "
        "kind TEXT, summary TEXT, value REAL)"
    )
    db.execute("CREATE TABLE IF NOT EXISTS suppliers (id INTEGER PRIMARY KEY, name TEXT UNIQUE, phone TEXT)")
    for table, col, ddl in (("reorders", "batch", "TEXT"), ("products", "supplier_id", "INTEGER")):
        if not any(r[1] == col for r in db.execute("PRAGMA table_info(%s)" % table)):
            db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, ddl))
    db.execute("UPDATE reorders SET status='expired' WHERE batch IS NULL AND status='pending'")
    db.commit()
    return db


def make_po_file(folder, po, supplier, rows, total, date):
    """Write a PDF if reportlab is installed, otherwise a text file."""
    shop = os.getenv("SHOP_NAME", "RetailIQ Shop")
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        path = folder / (po + ".pdf")
        c = canvas.Canvas(str(path), pagesize=A4)
        w, h = A4
        y = h - 60
        c.setFont("Helvetica-Bold", 18)
        c.drawString(50, y, shop)
        c.setFont("Helvetica", 11)
        y -= 24
        c.drawString(50, y, "PURCHASE ORDER  " + po)
        y -= 16
        c.drawString(50, y, "Date: " + date)
        y -= 16
        c.drawString(50, y, "To: " + (supplier or "Supplier ____________________"))
        y -= 36
        c.setFont("Helvetica-Bold", 11)
        for x, t in ((50, "Item"), (320, "Qty"), (380, "Rate (Rs)"), (470, "Amount (Rs)")):
            c.drawString(x, y, t)
        c.line(50, y - 4, 545, y - 4)
        c.setFont("Helvetica", 11)
        y -= 22
        for name, qty, rate, amt in rows:
            c.drawString(50, y, name[:40])
            c.drawString(320, y, str(qty))
            c.drawString(380, y, "%d" % rate)
            c.drawString(470, y, "%d" % amt)
            y -= 18
        c.line(50, y + 8, 545, y + 8)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(380, y - 8, "Total: Rs %d" % total)
        c.setFont("Helvetica", 10)
        c.drawString(50, y - 40, "Please confirm availability and delivery date.")
        c.save()
        return path
    except ImportError:
        path = folder / (po + ".txt")
        lines = ["%s x %d @ Rs %d = Rs %d" % (n, q, r, a) for n, q, r, a in rows]
        path.write_text(
            "%s\nPURCHASE ORDER %s\nDate: %s\nTo: %s\n\n%s\n\nTotal: Rs %d\n"
            "Please confirm availability and delivery date." % (
                shop, po, date, supplier or "Supplier ____________", "\n".join(lines), total),
            encoding="utf-8")
        return path


async def run_check(app, always_reply=False):
    oid = owner()
    if not oid:
        return 0
    now = datetime.now(IST)
    since = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    recent = (now - timedelta(days=3)).isoformat()
    msgs, count = [], 0
    db = connect()
    try:
        sold = dict(db.execute(
            "SELECT product_id, SUM(quantity) FROM sales WHERE substr(sale_date,1,10)>=? GROUP BY product_id",
            (since,)).fetchall())
        q = ("SELECT p.id, p.name, p.stock, p.cost_price, COALESCE(p.supplier_id,0), COALESCE(s.name,'') "
             "FROM products p LEFT JOIN suppliers s ON s.id=p.supplier_id")
        items = []
        for pid, name, stock, cost, sid, sname in db.execute(q).fetchall():
            rate = sold.get(pid, 0) / 7.0
            days = stock / rate if rate else None
            if stock > THRESHOLD and not (days is not None and days <= 3):
                continue
            if db.execute("SELECT 1 FROM reorders WHERE product_id=? AND created_at>=? AND status!='expired'",
                          (pid, recent)).fetchone():
                continue
            qty = max(5, math.ceil(rate * COVER_DAYS) - stock)
            items.append((days, sid, sname, pid, name, stock, qty, qty * (cost or 0)))
        items.sort(key=lambda x: (x[0] is None, x[0] or 0))
        items = items[:MAX_PER_RUN]
        count = len(items)
        groups = {}
        for it in items:
            groups.setdefault((it[1], it[2]), []).append(it)
        for (sid, sname), rows in groups.items():
            batch = uuid.uuid4().hex[:8]
            lines, total = [], 0
            for days, _, _, pid, name, stock, qty, est in rows:
                db.execute(
                    "INSERT INTO reorders (created_at, product_id, qty, est_cost, status, batch) VALUES (?,?,?,?,?,?)",
                    (now.isoformat(), pid, qty, est, "pending", batch))
                left = " (about %.1f days)" % days if days else ""
                lines.append("- %s: %d left%s, order %d (about Rs %d)" % (name, stock, left, qty, est))
                total += est
            msgs.append((batch, sname, lines, total))
        db.commit()
    finally:
        db.close()
    for batch, sname, lines, total in msgs:
        text = "Reorder for %s\n%s\nTotal about Rs %d" % (sname or "no supplier set", "\n".join(lines), total)
        if not sname:
            text += "\nTip: /supplier Name | phone | item1, item2"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Approve order", callback_data="ap:ok:" + batch),
            InlineKeyboardButton("Skip", callback_data="ap:no:" + batch),
        ]])
        await app.bot.send_message(oid, text, reply_markup=kb)
    if not count and always_reply:
        await app.bot.send_message(oid, "Autopilot: nothing needs reordering right now.")
    return count


async def autopilot_loop(app):
    while True:
        now = datetime.now(IST)
        t = now.replace(hour=RUN_HOUR, minute=0, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        log.info("Next autopilot check at %s", t.strftime("%d-%m %H:%M"))
        await asyncio.sleep((t - now).total_seconds())
        try:
            await run_check(app)
        except Exception:
            log.exception("Autopilot check failed")


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != owner():
        return
    _, action, batch = q.data.split(":")
    now = datetime.now(IST)
    db = connect()
    try:
        rows = db.execute(
            "SELECT r.id, r.qty, r.est_cost, p.name, p.cost_price, COALESCE(s.name,''), COALESCE(s.phone,'') "
            "FROM reorders r JOIN products p ON p.id=r.product_id "
            "LEFT JOIN suppliers s ON s.id=p.supplier_id WHERE r.batch=? AND r.status='pending'",
            (batch,)).fetchall()
        if not rows:
            await q.edit_message_text("Already handled.")
            return
        sname, phone = rows[0][5], rows[0][6]
        total = sum(r[2] for r in rows)
        if action == "no":
            db.execute("UPDATE reorders SET status='skipped' WHERE batch=?", (batch,))
            db.execute("INSERT INTO automation_log (created_at, kind, summary, value) VALUES (?,?,?,?)",
                       (now.isoformat(), "reorder_skipped", sname or "no supplier", 0))
            db.commit()
            await q.edit_message_text("Skipped the order for %s." % (sname or "this batch"))
            return
        folder = ROOT / "orders"
        folder.mkdir(exist_ok=True)
        po = "PO-%s-%s" % (now.strftime("%Y%m%d"), batch[:4].upper())
        table = [(r[3], r[1], r[4] or 0, r[2]) for r in rows]
        path = make_po_file(folder, po, sname, table, total, now.strftime("%d-%m-%Y"))
        db.execute("UPDATE reorders SET status='approved', po_file=? WHERE batch=?", (str(path), batch))
        db.execute("INSERT INTO automation_log (created_at, kind, summary, value) VALUES (?,?,?,?)",
                   (now.isoformat(), "reorder_approved", "%s: %d items" % (sname or "no supplier", len(rows)), total))
        db.commit()
    finally:
        db.close()
    await q.edit_message_text("Approved: %d item(s), about Rs %d. Purchase order %s is ready." % (len(rows), total, po))
    kb = None
    digits = re.sub(r"\D", "", phone)
    if digits:
        if len(digits) == 10:
            digits = "91" + digits
        msg = "Purchase order %s:\n%s\nPlease confirm availability and delivery date." % (
            po, "\n".join("- %s x %d" % (r[3], r[1]) for r in rows))
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "Send to %s on WhatsApp" % sname, url="https://wa.me/%s?text=%s" % (digits, quote(msg)))]])
    with open(path, "rb") as f:
        await q.message.reply_document(f, filename=path.name, reply_markup=kb)


async def autopilot_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != owner():
        return
    await run_check(ctx.application, always_reply=True)


async def supplier_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != owner():
        return
    parts = [p.strip() for p in " ".join(ctx.args).split("|")]
    if len(parts) < 3 or not parts[0]:
        await update.message.reply_text("Use: /supplier Name | phone | item1, item2\n"
                                        "Example: /supplier Ravi Textiles | 9876543210 | Cap, T-Shirt")
        return
    name = parts[0]
    phone = re.sub(r"[^\d+]", "", parts[1])
    items = [i.strip() for i in parts[2].split(",") if i.strip()]
    db = connect()
    try:
        db.execute("INSERT INTO suppliers (name, phone) VALUES (?,?) "
                   "ON CONFLICT(name) DO UPDATE SET phone=excluded.phone", (name, phone))
        sid = db.execute("SELECT id FROM suppliers WHERE name=?", (name,)).fetchone()[0]
        prods = {r[1].lower(): r[0] for r in db.execute("SELECT id, name FROM products")}
        done, miss = [], []
        for it in items:
            hit = get_close_matches(it.lower(), list(prods), n=1, cutoff=0.6)
            if hit:
                db.execute("UPDATE products SET supplier_id=? WHERE id=?", (sid, prods[hit[0]]))
                done.append(hit[0].title())
            else:
                miss.append(it)
        db.commit()
    finally:
        db.close()
    text = "Saved supplier %s (%s).\nItems: %s" % (name, phone or "no phone", ", ".join(done) or "none")
    if miss:
        text += "\nNot found in your products: " + ", ".join(miss)
    await update.message.reply_text(text)


async def suppliers_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != owner():
        return
    db = connect()
    try:
        rows = db.execute(
            "SELECT s.name, s.phone, GROUP_CONCAT(p.name, ', ') FROM suppliers s "
            "LEFT JOIN products p ON p.supplier_id=s.id GROUP BY s.id").fetchall()
    finally:
        db.close()
    if not rows:
        await update.message.reply_text("No suppliers yet. Use /supplier Name | phone | item1, item2")
        return
    await update.message.reply_text("\n".join("%s (%s): %s" % (n, p or "no phone", i or "no items") for n, p, i in rows))


async def impact_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != owner():
        return
    since = (datetime.now(IST) - timedelta(days=30)).isoformat()
    db = connect()
    try:
        rows = db.execute(
            "SELECT status, COUNT(*), COALESCE(SUM(est_cost),0) FROM reorders "
            "WHERE created_at>=? AND status!='expired' GROUP BY status", (since,)).fetchall()
    finally:
        db.close()
    d = {s: (c, v) for s, c, v in rows}
    ok = d.get("approved", (0, 0))
    await update.message.reply_text(
        "Autopilot, last 30 days (items):\nSuggested: %d\nApproved: %d (about Rs %d ordered)\n"
        "Skipped: %d\nWaiting for you: %d" % (
            sum(c for c, _ in d.values()), ok[0], ok[1],
            d.get("skipped", (0, 0))[0], d.get("pending", (0, 0))[0]))


def register_autopilot(app):
    app.add_handler(CommandHandler("autopilot", autopilot_cmd))
    app.add_handler(CommandHandler("supplier", supplier_cmd))
    app.add_handler(CommandHandler("suppliers", suppliers_cmd))
    app.add_handler(CommandHandler("impact", impact_cmd))
    app.add_handler(CallbackQueryHandler(on_button, pattern="^ap:"))
