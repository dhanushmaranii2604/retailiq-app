import base64
import json
import logging
import os
import re
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from difflib import get_close_matches
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

load_dotenv()
log = logging.getLogger("retailiq-bills")
DB = Path(__file__).resolve().parent / "data" / "retailiq.db"
IST = timezone(timedelta(hours=5, minutes=30))

PROMPT = (
    "Read this shop bill or handwritten sales page. Return ONLY JSON in this form: "
    '{"items":[{"name":"product name","qty":number,"price":unit price in rupees or null}]}. '
    "Use null for anything unreadable. Do not guess. No other text."
)


def owner_ok(user_id):
    v = os.getenv("TELEGRAM_OWNER_ID", "").strip()
    return v.isdigit() and int(v) == user_id


async def read_bill(image_bytes):
    model = os.getenv("VISION_MODEL", "qwen/qwen3.8-27b")
    b64 = base64.b64encode(image_bytes).decode()
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 2000,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ],
        }],
    }
    headers = {"Authorization": "Bearer " + os.getenv("GROQ_API_KEY", "")}
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=body)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"] or ""
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)).get("items", []) if m else []


def match(items):
    db = sqlite3.connect(str(DB))
    try:
        prods = {r[1].lower(): (r[0], r[1]) for r in db.execute("SELECT id, name FROM products")}
    finally:
        db.close()
    rows, missing = [], []
    for it in items:
        name = str(it.get("name") or "").strip()
        try:
            qty = int(round(float(it.get("qty"))))
        except (TypeError, ValueError):
            qty = 0
        if not name or qty <= 0:
            continue
        try:
            price = float(it.get("price"))
        except (TypeError, ValueError):
            price = None
        hit = get_close_matches(name.lower(), list(prods), n=1, cutoff=0.6)
        if hit:
            pid, pname = prods[hit[0]]
            rows.append((pid, pname, qty, price))
        else:
            missing.append(name)
    return rows, missing


def apply(job):
    shutil.copy(str(DB), str(DB.with_name("retailiq_backup_bills.db")))
    db = sqlite3.connect(str(DB))
    try:
        db.execute(
            "CREATE TABLE IF NOT EXISTS bill_entries (id INTEGER PRIMARY KEY, created_at TEXT, "
            "mode TEXT, product_id INTEGER, quantity INTEGER, price REAL)"
        )
        now = datetime.now(IST)
        sample = db.execute("SELECT sale_date FROM sales LIMIT 1").fetchone()
        fmt = "%Y-%m-%d %H:%M:%S" if sample and len(str(sample[0])) > 10 else "%Y-%m-%d"
        for pid, _, qty, price in job["rows"]:
            if job["mode"] == "sale":
                if price is None:
                    price = db.execute("SELECT price FROM products WHERE id=?", (pid,)).fetchone()[0]
                db.execute(
                    "INSERT INTO sales (product_id, quantity, unit_price, sale_date) VALUES (?,?,?,?)",
                    (pid, qty, price, now.strftime(fmt)),
                )
                db.execute("UPDATE products SET stock = MAX(stock - ?, 0) WHERE id=?", (qty, pid))
            else:
                db.execute("UPDATE products SET stock = stock + ? WHERE id=?", (qty, pid))
            db.execute(
                "INSERT INTO bill_entries (created_at, mode, product_id, quantity, price) VALUES (?,?,?,?,?)",
                (now.isoformat(), job["mode"], pid, qty, price),
            )
        db.commit()
    finally:
        db.close()


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_ok(update.effective_user.id):
        return
    msg = update.message
    mode = "sale" if "sale" in (msg.caption or "").lower() else "purchase"
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        f = await msg.photo[-1].get_file()
        data = bytes(await f.download_as_bytearray())
        rows, missing = match(await read_bill(data))
    except Exception:
        log.exception("Bill read failed")
        await msg.reply_text(
            "Could not read the photo. Send a clear, straight photo, "
            "or check VISION_MODEL in .env."
        )
        return
    if not rows:
        await msg.reply_text(
            "No items from your product list were found in this photo."
            + (" Not matched: " + ", ".join(missing) if missing else "")
        )
        return
    token = uuid.uuid4().hex[:10]
    ctx.application.bot_data.setdefault("bills", {})[token] = {"mode": mode, "rows": rows}
    lines = ["%s x %d%s" % (n, q, " @ Rs %g" % p if p else "") for _, n, q, p in rows]
    title = "Sales entry (stock goes down)" if mode == "sale" else "Purchase (stock goes up)"
    text = title + ":\n" + "\n".join(lines)
    if missing:
        text += "\nNot in your product list, skipped: " + ", ".join(missing)
    text += "\n\nSave this?"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Confirm", callback_data="bill:ok:" + token),
        InlineKeyboardButton("Cancel", callback_data="bill:no:" + token),
    ]])
    await msg.reply_text(text, reply_markup=kb)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not owner_ok(q.from_user.id):
        return
    _, action, token = q.data.split(":")
    job = ctx.application.bot_data.get("bills", {}).pop(token, None)
    if not job:
        await q.edit_message_text("This entry has expired. Send the photo again.")
        return
    if action == "no":
        await q.edit_message_text("Cancelled. Nothing was saved.")
        return
    try:
        apply(job)
        await q.edit_message_text("Saved %d item(s). Stock updated." % len(job["rows"]))
    except Exception:
        log.exception("Bill save failed")
        await q.edit_message_text("Could not save. Nothing was changed.")


def register_bills(app):
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(CallbackQueryHandler(on_button, pattern="^bill:"))
