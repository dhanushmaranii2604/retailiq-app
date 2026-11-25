import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
log = logging.getLogger("retailiq-telegram")
logging.getLogger("httpx").setLevel(logging.WARNING)
IST = timezone(timedelta(hours=5, minutes=30))
DB = Path(__file__).resolve().parent / "data" / "retailiq.db"

EVENING_HOUR = 21    # 9 PM: today's report
YESTERDAY_HOUR = 9   # 9 AM: detailed report for yesterday

REPORT_PROMPT = (
    "Give the owner a short end-of-day report for today: total sales, profit, "
    "number of transactions, items low on stock, and the top 3 selling products. "
    "Use only tool data. If something is not available, say so."
)

YESTERDAY_PROMPT = (
    "Give a detailed sales report for %s (yesterday): total revenue, profit, "
    "units and transactions, every product sold with units and revenue, items "
    "low on stock, and anything unusual compared with earlier days. "
    "Use only tool data. If something is not available, say so."
)


def clean(text):
    out, header = [], None
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if header is None:
                header = cells
                continue
            out.append(", ".join("%s: %s" % (h, c) for h, c in zip(header, cells)))
            continue
        header = None
        s = s.replace("**", "").replace("__", "")
        s = re.sub(r"^#+\s*", "", s)
        out.append(s)
    return "\n".join(out)[:4000]


def owner_id():
    v = os.getenv("TELEGRAM_OWNER_ID", "").strip()
    return int(v) if v.isdigit() else None


def allowed(update):
    oid = owner_id()
    return oid is not None and update.effective_user.id == oid


def udhaar_line():
    try:
        db = sqlite3.connect(str(DB))
        try:
            v = db.execute("SELECT COALESCE(SUM(amount),0) FROM credits WHERE status='pending'").fetchone()[0]
        finally:
            db.close()
        return "\n\nPending udhaar: Rs %s" % format(round(v), ",") if v else ""
    except Exception:
        return ""


async def build_report(agent):
    from reports import day_report
    now = datetime.now(IST)
    return "RetailIQ daily report (%s)\n\n%s" % (now.strftime("%d-%m-%Y"), day_report(now.date()))


async def build_yesterday(agent):
    from reports import day_report
    now = datetime.now(IST)
    d = (now - timedelta(days=1)).date()
    greet = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening"
    return greet + "! Here is your detailed report for " + d.isoformat() + "\n\n" + day_report(d) + udhaar_line()


async def myid_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Your Telegram ID: %s\nAdd this line to .env and restart the server:\n"
        "TELEGRAM_OWNER_ID=%s" % (update.effective_user.id, update.effective_user.id)
    )


async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if owner_id() is None:
        await update.message.reply_text("RetailIQ bot is almost ready. Send /myid and follow the steps.")
    elif not allowed(update):
        await update.message.reply_text("This is a private bot.")
    else:
        await update.message.reply_text(
            "RetailIQ is ready. Ask about sales, profit or stock.\n"
            "/report today's report, /yesterday yesterday's detailed report, "
            "/autopilot reorder suggestions, /impact automation results.\n"
            "Send a bill photo to add stock (add the word 'sale' in the caption for a sales page)."
        )


async def report_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        await update.message.reply_text(await build_report(ctx.application.bot_data["agent"]))
    except Exception:
        log.exception("Report failed")
        await update.message.reply_text("Sorry, could not build the report.")


async def yesterday_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        await update.message.reply_text(await build_yesterday(ctx.application.bot_data["agent"]))
    except Exception:
        log.exception("Yesterday report failed")
        await update.message.reply_text("Sorry, could not build the report.")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    text = (update.message.text or "").strip()[:1000]
    if not text:
        return
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        reply = await ctx.application.bot_data["agent"].ask(
            "tg-%s" % update.effective_chat.id, text, "English"
        )
        await update.message.reply_text(clean(reply))
    except Exception:
        log.exception("Telegram chat failed")
        await update.message.reply_text("Sorry, an error occurred. Please try again.")


async def send_at(app, hour, make_text):
    while True:
        now = datetime.now(IST)
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        log.info("Next scheduled report at %s", target.strftime("%d-%m %H:%M"))
        await asyncio.sleep((target - now).total_seconds())
        oid = owner_id()
        if not oid:
            continue
        try:
            await app.bot.send_message(oid, await make_text(app.bot_data["agent"]))
        except Exception:
            log.exception("Scheduled report failed")


async def schedules(app):
    jobs = [
        send_at(app, EVENING_HOUR, build_report),
        send_at(app, YESTERDAY_HOUR, build_yesterday),
    ]
    try:
        from autopilot import autopilot_loop
        jobs.append(autopilot_loop(app))
    except Exception:
        log.exception("autopilot not loaded")
    await asyncio.gather(*jobs)


async def start_telegram(agent):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.info("TELEGRAM_BOT_TOKEN not set, Telegram bot disabled")
        return None
    try:
        app = Application.builder().token(token).build()
        app.bot_data["agent"] = agent
        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(CommandHandler("myid", myid_cmd))
        app.add_handler(CommandHandler("report", report_cmd))
        app.add_handler(CommandHandler("yesterday", yesterday_cmd))
        for mod, fn in (("autopilot", "register_autopilot"), ("bill_photo", "register_bills")):
            try:
                getattr(__import__(mod), fn)(app)
            except Exception:
                log.exception("%s not loaded", mod)
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        app.bot_data["task"] = asyncio.create_task(schedules(app))
        log.info("Telegram bot started")
        return app
    except Exception:
        log.exception("Telegram failed to start")
        return None


async def stop_telegram(app):
    if app is None:
        return
    try:
        app.bot_data["task"].cancel()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
    except Exception:
        log.exception("Telegram stop error")
