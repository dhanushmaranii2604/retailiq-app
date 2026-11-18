import os
import logging
import sqlite3
from datetime import time, datetime
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Import your AI agent (adjust if your path/class names are different)
# from groq_agent import RetailIQAgent  <-- Usually handled in web_chat, not here

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("retailiq-telegram")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") # Make sure this is in your .env file!

IST = ZoneInfo("Asia/Kolkata")

if os.getenv("RENDER"):
    DB_PATH = "/opt/render/project/src/data/retailiq.db"
else:
    DB_PATH = os.path.join(os.path.dirname(__file__), "data", "retailiq.db")

# --- AUTOMATED REPORTS (CRON JOBS) ---

async def send_morning_report(context: ContextTypes.DEFAULT_TYPE):
    """Runs automatically at 9:00 AM IST."""
    if not CHAT_ID:
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. Get Pending Udhaar
    cursor.execute("SELECT SUM(amount) FROM credits WHERE status = 'pending'")
    udhaar = cursor.fetchone()[0] or 0
    
    # 2. Get Low Stock
    cursor.execute("SELECT name, stock FROM products WHERE stock <= 5")
    low_stock = cursor.fetchall()
    
    conn.close()

    msg = f"☀️ *Good Morning!*\n\nHere is your shop briefing for today:\n"
    msg += f"💰 *Pending Udhaar:* ₹{udhaar:,.0f}\n"
    
    if low_stock:
        msg += "\n⚠️ *Low Stock Alerts:*\n"
        for item in low_stock:
            msg += f"• {item[0]}: {item[1]} left\n"
    else:
        msg += "\n✅ All stock levels are healthy.\n"

    msg += "\nHave a great day of sales!"
    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")


async def send_evening_report(context: ContextTypes.DEFAULT_TYPE):
    """Runs automatically at 9:00 PM IST."""
    if not CHAT_ID:
        return

    today = datetime.now(IST).strftime('%Y-%m-%d')
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. Get Today's Sales
    cursor.execute("SELECT SUM(quantity * unit_price), COUNT(*) FROM sales WHERE sale_date = ?", (today,))
    row = cursor.fetchone()
    revenue = row[0] or 0
    bills = row[1] or 0
    
    # 2. Get Top Seller Today
    cursor.execute("""
        SELECT p.name, SUM(s.quantity) 
        FROM sales s JOIN products p ON s.product_id = p.id 
        WHERE s.sale_date = ? 
        GROUP BY p.id ORDER BY SUM(s.quantity) DESC LIMIT 1
    """, (today,))
    top = cursor.fetchone()
    
    conn.close()

    msg = f"🌙 *Good Evening!*\n\nHere is your end-of-day report:\n"
    msg += f"💵 *Revenue Today:* ₹{revenue:,.0f}\n"
    msg += f"🧾 *Bills Generated:* {bills}\n"
    
    if top:
        msg += f"🏆 *Top Seller:* {top[0]} ({top[1]} units)\n"
        
    msg += "\nGreat job today! Rest well."
    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")


# --- MANUAL CHAT COMMANDS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to RetailIQ Telegram!\n"
        "Ask me anything about your shop, just like on the web dashboard."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """This handles when you type a question to the bot."""
    # NOTE: If your telegram_bot.py already has AI integration logic, keep it here.
    # For now, it will just echo a placeholder so we don't break your existing agent setup.
    text = update.message.text
    await update.message.reply_text(f"🤖 Processing: '{text}'...\n(Your existing AI logic handles this).")


# --- STARTUP ---

def start_telegram():
    """Called by web_chat.py on startup."""
    if not TOKEN:
        log.warning("No TELEGRAM_BOT_TOKEN found. Bot will not start.")
        return

    application = Application.builder().token(TOKEN).build()

    # Add manual chat handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Add CRON Jobs (Automated Alerts)
    job_queue = application.job_queue
    if job_queue:
        # 9:00 AM IST
        job_queue.run_daily(send_morning_report, time=time(hour=9, minute=0, tzinfo=IST))
        # 9:00 PM IST
        job_queue.run_daily(send_evening_report, time=time(hour=21, minute=0, tzinfo=IST))
        log.info("Scheduled morning (9AM) and evening (9PM) reports.")

    log.info("Starting Telegram Bot polling...")
    application.run_polling(drop_pending_updates=True)

def stop_telegram():
    """Called by web_chat.py on shutdown."""
    pass