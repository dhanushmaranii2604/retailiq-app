# RetailIQ: AI Sales & Stock Assistant

An AI assistant for small retail shops. The owner asks about sales, profit and stock in plain language (English, Hindi, Kannada or Telugu) on a web dashboard or on Telegram. The system also runs routine work automatically.

## Features
- Dashboard: business health score, revenue and profit trends, top sellers, low-stock bars, daily action plan
- AI chat in 4 languages, answering only from database tools (no invented numbers)
- "Why?" engine: explains sales changes using only facts found in the data
- Telegram bot (owner-only): questions, reports, automatic 9 AM and 9 PM reports
- Autopilot reorder: groups low-stock items by supplier, asks for approval, creates a purchase order PDF and a WhatsApp message
- Bill photo to entry: a vision model reads a bill photo, the owner confirms before saving
- Manage page (PIN protected): products, restock, sales entry, stock ledger

## Architecture
Browser or Telegram -> FastAPI -> AI agent (Groq, tool calling) -> MCP server tools -> SQLite (read-only for the AI).
Writes happen only through the PIN-protected Manage API or confirmed Telegram actions.

## Tech
Python, FastAPI, MCP (FastMCP), SQLite, Groq API, python-telegram-bot, Chart.js, reportlab

## Run
1. `pip install -r requirements.txt` and `pip install reportlab`
2. Create `.env` with GROQ_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID, APP_PIN
3. Create sample data: `python tools/make_sales.py`
4. Run `start.bat` and open http://localhost:8000

## Notes
Sample data is for demos only. Use real shop data only with the owner's written permission.

## Roadmap
Login and roles, always-on hosting, expenses and net profit, GST summary, POS/ERP import.