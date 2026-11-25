import json
import logging
import sys
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from google.genai import types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel

from groq_agent import RetailIQAgent
from telegram_bot import start_telegram, stop_telegram

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("retailiq-web")

@asynccontextmanager
async def lifespan(app):
    async with AsyncExitStack() as stack:
        params = StdioServerParameters(command=sys.executable, args=["mcp_server.py"])
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        tools = (await session.list_tools()).tools
        gemini_tool = types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=t.name,
                    description=t.description or t.name,
                    parameters_json_schema=t.inputSchema,
                )
                for t in tools
            ]
        )
        app.state.agent = RetailIQAgent(session, gemini_tool)
        app.state.session = session
        log.info("RetailIQ: Ready.")
        tg = await start_telegram(app.state.agent)
        yield
        await stop_telegram(tg)

app = FastAPI(lifespan=lifespan)

class ChatRequest(BaseModel):
    message: str
    session: str = "web"
    language: str = "English"

@app.post("/chat")
async def chat(req: ChatRequest):
    text = req.message.strip()[:1000]
    if not text:
        return {"reply": "Please type a question."}
    try:
        reply = await app.state.agent.ask(req.session[:64], text, req.language)
    except Exception as exc:
        log.exception("Agent failed")
        reply = "Sorry, an error occurred. Please try again."
    return {"reply": reply}

@app.get("/health")
async def health():
    return {"status": "ok"}

RUPEE = "\u20b9"

def tool_items(result):
    items = []
    for c in result.content or []:
        try:
            items.append(json.loads(c.text))
        except (ValueError, AttributeError):
            pass
    return items

@app.get("/stats")
async def get_stats():
    session = app.state.session
    try:
        sales_items = tool_items(await session.call_tool("get_sales_summary", {}))
        sales = sales_items[0] if sales_items else {"total_sales": 0, "transactions": 0}
        
        profit_items = tool_items(await session.call_tool("calculate_profit", {"days": 1}))
        profit = profit_items[0].get("profit", 0) if profit_items else 0
        
        low_items = tool_items(await session.call_tool("get_low_stock", {}))
        low_count = len(low_items) if not (low_items and "note" in low_items[0]) else 0

        return {
            "revenue": sales.get("total_sales", 0),
            "profit": profit,
            "orders": sales.get("transactions", 0),
            "low_stock": low_count
        }
    except Exception as e:
        log.error(f"Stats error: {e}")
        return {"revenue": 0, "profit": 0, "orders": 0, "low_stock": 0}

@app.get("/quick/date/{date_str}")
async def quick_date(date_str: str):
    session = app.state.session
    try:
        result = await session.call_tool("get_daily_sales", {"start_date": date_str, "end_date": date_str})
        items = tool_items(result)
        if not items or len(items) == 0 or "note" in items[0]:
            return {"reply": f"No sales data found for {date_str}.", "chart_data": None}
        d = items[0]
        chart_data = {
            "labels": ["Units Sold", "Transactions"],
            "values": [d.get("units_sold", 0), d.get("transactions", 0)],
            "title": f"Daily Summary for {date_str}"
        }
        return {
            "reply": f"On {date_str}, you had {d.get('transactions', 0)} transactions generating {RUPEE}{d.get('total_sales', 0):,.0f}.",
            "chart_data": chart_data
        }
    except Exception:
        return {"reply": f"Could not find data for {date_str}.", "chart_data": None}

PAGE = """<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>RetailIQ Assistant</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <script defer src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <script defer src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script defer src="https://unpkg.com/html5-qrcode"></script>
    
    <style>
        :root {
            --primary: #10a37f; --primary-hover: #0d8c6d;
            --bg: #f4f6f8; --sidebar-bg: #111827;
            --card-bg: #ffffff; --text-main: #1f2937; --text-muted: #6b7280;
            --border: #e5e7eb; --input-bg: #ffffff;
        }
        body.dark { 
            --bg: #111827; --sidebar-bg: #1f2937; 
            --card-bg: #2d3748; --text-main: #f9fafb; 
            --text-muted: #9ca3af; --border: #4b5563; 
            --input-bg: #374151;
        }
        
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', sans-serif; transition: background 0.3s, color 0.3s;}
        body { background-color: var(--bg); color: var(--text-main); height: 100vh; display: flex; overflow: hidden; }
        
        /* SIDEBAR */
        #sidebar { width: 260px; background: var(--sidebar-bg); color: white; display: flex; flex-direction: column; z-index: 100; box-shadow: 2px 0 10px rgba(0,0,0,0.1); }
        .sidebar-header { padding: 20px; font-weight: 700; font-size: 22px; display: flex; justify-content: space-between; align-items: center; letter-spacing: -0.5px;}
        #new-chat-btn { margin: 0 20px 20px 20px; padding: 12px; background: var(--primary); border: none; color: white; border-radius: 8px; cursor: pointer; font-weight: 600; transition: 0.2s; box-shadow: 0 2px 5px rgba(16,163,127,0.3);}
        #new-chat-btn:hover { background: var(--primary-hover); transform: translateY(-1px); }
        .date-picker-container { margin: 0 20px 20px 20px; }
        #date-picker { width: 100%; padding: 10px; border-radius: 8px; border: 1px solid #374151; background: #374151; color: white; outline: none; }
        .chat-history { flex: 1; overflow-y: auto; padding: 0 12px; }
        .history-item { padding: 12px; border-radius: 8px; cursor: pointer; color: #d1d5db; font-size: 14px; margin-bottom: 6px; display: flex; align-items: center; gap: 10px; transition: 0.2s;}
        .history-item:hover { background: #1f2937; color: white; transform: translateX(4px); }
        
        /* MAIN AREA */
        #main { flex: 1; display: flex; flex-direction: column; height: 100vh; }
        header { background: var(--card-bg); padding: 15px 30px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; box-shadow: 0 1px 3px rgba(0,0,0,0.02);}
        .header-title { font-size: 20px; font-weight: 700; color: var(--text-main); letter-spacing: -0.5px;}
        #menu-btn { display: none; background: none; border: none; font-size: 24px; cursor: pointer; color: var(--text-main); }
        
        /* LANGUAGE PILLS */
        .lang-pills { display: flex; gap: 8px; background: var(--bg); padding: 4px; border-radius: 8px; border: 1px solid var(--border); margin-right: 20px;}
        .lang-pill { padding: 6px 14px; border-radius: 6px; border: none; background: transparent; color: var(--text-muted); cursor: pointer; font-weight: 600; font-size: 13px; transition: 0.2s; }
        .lang-pill:hover { background: var(--card-bg); color: var(--text-main); }
        .lang-pill.active { background: var(--primary); color: white; box-shadow: 0 2px 4px rgba(16,163,127,0.3); }

        .header-actions { display: flex; align-items: center; }
        .header-actions button { background: none; border: none; font-size: 20px; cursor: pointer; margin-left: 15px; color: var(--text-muted); transition: 0.2s; }
        .header-actions button:hover { color: var(--primary); transform: scale(1.1); }
        
        /* DASHBOARD CARDS */
        .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; padding: 25px 30px; background: var(--bg); }
        .stat-card { 
            background: linear-gradient(135deg, var(--card-bg) 0%, var(--bg) 100%); 
            padding: 24px; border-radius: 16px; border: 1px solid var(--border); 
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05); transition: transform 0.2s, box-shadow 0.2s;
        }
        .stat-card:hover { transform: translateY(-3px); box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1); }
        .stat-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
        .stat-title { font-size: 14px; color: var(--text-muted); font-weight: 600; }
        .stat-icon { font-size: 24px; }
        .stat-value { font-size: 28px; font-weight: 800; color: var(--text-main); letter-spacing: -1px;}
        .stat-value.green { color: var(--primary); }
        .stat-value.red { color: #ef4444; }

        #chat { flex: 1; overflow-y: auto; padding: 20px 30px; display: flex; flex-direction: column; gap: 20px; }
        .msg { max-width: 80%; padding: 16px 20px; border-radius: 16px; font-size: 15px; line-height: 1.6; box-shadow: 0 2px 4px rgba(0,0,0,0.02);}
        .user { background-color: var(--primary); color: white; align-self: flex-end; border-bottom-right-radius: 4px; }
        .bot { background-color: var(--card-bg); color: var(--text-main); align-self: flex-start; border-bottom-left-radius: 4px; border: 1px solid var(--border); }
        
        /* BEAUTIFUL TABLE CSS */
        .msg table { width: 100%; border-collapse: collapse; margin-top: 15px; margin-bottom: 10px; font-size: 14px; background: var(--card-bg); border-radius: 8px; overflow: hidden; border: 1px solid var(--border);}
        .msg th, .msg td { padding: 12px 15px; text-align: left; border-bottom: 1px solid var(--border); }
        .msg th { background-color: var(--bg); font-weight: 700; color: var(--text-main); text-transform: uppercase; font-size: 12px; letter-spacing: 0.5px;}
        .msg tr:last-child td { border-bottom: none; }
        .msg tr:nth-child(even) { background-color: rgba(0,0,0,0.02); }
        
        .action-btn { background: var(--bg); border: 1px solid var(--border); color: var(--text-main); padding: 6px 12px; border-radius: 8px; font-size: 12px; font-weight: 600; cursor: pointer; margin-top: 12px; margin-right: 8px; transition: 0.2s; display: inline-flex; align-items: center; gap: 4px;}
        .action-btn:hover { background: var(--border); }
        
        .bottom-area { padding: 20px 30px; background: var(--bg); }
        #input-area { display: flex; gap: 12px; align-items: center; max-width: 800px; margin: 0 auto; background: var(--input-bg); padding: 10px; border-radius: 30px; border: 1px solid var(--border); box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);}
        #q { flex: 1; padding: 10px 15px; border: none; font-size: 16px; background: transparent; color: var(--text-main); outline: none; }
        .icon-btn { background: var(--card-bg); border: 1px solid var(--border); border-radius: 50%; width: 44px; height: 44px; cursor: pointer; display: flex; align-items: center; justify-content: center; font-size: 18px; color: var(--text-main); transition: 0.2s;}
        .icon-btn:hover { background: var(--border); }
        #send { background: var(--primary); color: white; border: none; border-radius: 50%; width: 44px; height: 44px; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: 0.2s; }
        #send:hover { background: var(--primary-hover); transform: scale(1.05); }
        
        #toast { position: fixed; bottom: 30px; left: 50%; transform: translateX(-50%) translateY(100px); background: #1f2937; color: white; padding: 12px 24px; border-radius: 8px; font-size: 14px; font-weight: 500; opacity: 0; transition: 0.3s; z-index: 2000; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.1);}
        #toast.show { transform: translateX(-50%) translateY(0); opacity: 1; }

        .typing { display: flex; gap: 4px; align-items: center; height: 20px; }
        .dot { width: 6px; height: 6px; background-color: #9ca3af; border-radius: 50%; animation: bounce 1.4s infinite ease-in-out both; }
        .dot:nth-child(1) { animation-delay: -0.32s; }
        .dot:nth-child(2) { animation-delay: -0.16s; }
        @keyframes bounce { 0%, 80%, 100% { transform: scale(0); } 40% { transform: scale(1); } }

        @media (max-width: 900px) { .stats-grid { grid-template-columns: repeat(2, 1fr); } .lang-pills { display: none; } }
        @media (max-width: 768px) {
            #sidebar { position: absolute; height: 100%; transform: translateX(-100%); transition: 0.3s; }
            #sidebar.open { transform: translateX(0); }
            #menu-btn { display: block; }
            .msg { max-width: 95%; }
            .stats-grid { padding: 15px; gap: 10px; }
            .stat-card { padding: 16px; }
            .stat-value { font-size: 22px; }
            .bottom-area { padding: 10px 15px; }
        }
    </style>
</head>
<body>
    <div id="sidebar">
        <div class="sidebar-header">
            <div>🛍️ RetailIQ</div>
            <span style="cursor:pointer; display:none;" class="close-sidebar" onclick="toggleSidebar()">✖</span>
        </div>
        <button id="new-chat-btn" onclick="clearChat()">+ New Chat</button>
        <div class="date-picker-container">
            <label style="font-size:12px; color:#9ca3af; font-weight:600; margin-bottom:6px; display:block;">📅 Filter by Date:</label>
            <input type="date" id="date-picker" onchange="renderDateDashboard(this.value)">
        </div>
        <div class="chat-history">
            <div class="history-item" onclick="send('What is my profit today?')">💰 Profit Margin</div>
            <div class="history-item" onclick="send('What is my dead stock?')">🧟‍♂️ Dead Stock</div>
            <div class="history-item" onclick="send('What will run out soon?')">🔮 Predictions</div>
        </div>
    </div>

    <div id="main">
        <header>
            <div style="display:flex; align-items:center; gap:15px;">
                <button id="menu-btn" onclick="toggleSidebar()">☰</button>
                <div class="header-title">Dashboard</div>
            </div>
            
            <div class="header-actions">
                <!-- Visible Language Pills -->
                <div class="lang-pills">
                    <button class="lang-pill active" onclick="setLanguage('English', this)">English</button>
                    <button class="lang-pill" onclick="setLanguage('Hindi', this)">हिंदी</button>
                    <button class="lang-pill" onclick="setLanguage('Kannada', this)">ಕನ್ನಡ</button>
                    <button class="lang-pill" onclick="setLanguage('Telugu', this)">తెలుగు</button>
                </div>
                <button onclick="toggleDarkMode()" title="Toggle Dark Mode">🌗</button>
                <button onclick="clearChat()" title="Clear Chat">🗑️</button>
            </div>
        </header>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-header">
                    <div class="stat-title">Yesterday's Revenue</div>
                    <div class="stat-icon">💰</div>
                </div>
                <div class="stat-value green" id="stat-revenue">₹0</div>
            </div>
            <div class="stat-card">
                <div class="stat-header">
                    <div class="stat-title">Yesterday's Profit</div>
                    <div class="stat-icon">📈</div>
                </div>
                <div class="stat-value green" id="stat-profit">₹0</div>
            </div>
            <div class="stat-card">
                <div class="stat-header">
                    <div class="stat-title">Transactions</div>
                    <div class="stat-icon">🧾</div>
                </div>
                <div class="stat-value" id="stat-orders">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-header">
                    <div class="stat-title">Low Stock Alerts</div>
                    <div class="stat-icon">⚠️</div>
                </div>
                <div class="stat-value red" id="stat-low">0</div>
            </div>
        </div>
        
        <div id="chat"></div>

        <div class="bottom-area">
            <div id="input-area">
                <button class="icon-btn" onclick="startScanner()" title="Scan Barcode">📷</button>
                <input id="q" placeholder="Ask about sales, profit, or stock..." autocomplete="off">
                <button id="send" title="Send">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>
                </button>
            </div>
        </div>
    </div>

    <div id="toast">Message</div>

    <div id="scanner-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.95); z-index:9999; flex-direction:column; align-items:center; justify-content:center;">
        <button onclick="document.getElementById('scanner-modal').style.display='none'; if(window.html5QrCode) { try{ window.html5QrCode.stop(); window.html5QrCode.clear(); }catch(e){} }" style="position:absolute; top:20px; right:20px; background:#ef4444; color:white; border:none; padding:10px 20px; border-radius:8px; cursor:pointer; font-weight:bold; font-size:16px; z-index:10000;">✖ Close</button>
        <div id="reader" style="width:300px; background:white; border-radius:10px; overflow:hidden;"></div>
    </div>

    <script>
        const sid = Math.random().toString(36).slice(2);
        const chat = document.getElementById("chat");
        const q = document.getElementById("q");
        let currentLang = "English";

        function setLanguage(lang, btn) {
            currentLang = lang;
            document.querySelectorAll('.lang-pill').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
        }

        function toggleSidebar() { document.getElementById("sidebar").classList.toggle("open"); }
        function toggleDarkMode() { document.body.classList.toggle("dark"); }

        function showToast(message) {
            const toast = document.getElementById("toast");
            toast.innerText = message;
            toast.classList.add("show");
            setTimeout(() => { toast.classList.remove("show"); }, 3000);
        }

        window.html5QrCode = null;
        function startScanner() {
            if(typeof Html5Qrcode === 'undefined') { showToast("Scanner library not loaded."); return; }
            document.getElementById("scanner-modal").style.display = "flex";
            window.html5QrCode = new Html5Qrcode("reader");
            window.html5QrCode.start({ facingMode: "environment" }, { fps: 10, qrbox: { width: 250, height: 250 } }, (text) => {
                q.value = "Check stock for barcode: " + text;
                document.getElementById('scanner-modal').style.display='none';
                try { window.html5QrCode.stop(); window.html5QrCode.clear(); } catch(e) {}
                send();
            }).catch(err => {
                showToast("Camera error: " + err);
                document.getElementById('scanner-modal').style.display='none';
            });
        }

        function addMsg(text, who, isTyping = false) {
            const d = document.createElement("div");
            d.className = "msg " + (who === "me" ? "user" : "bot");
            
            if (isTyping) {
                d.innerHTML = '<div class="typing"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>';
            } else if (who === "bot") {
                let parsedText = text;
                if (typeof marked !== 'undefined') {
                    parsedText = marked.parse(text);
                } else {
                    parsedText = text.split(String.fromCharCode(10)).join('<br>');
                }
                d.innerHTML = parsedText;
                
                const exportBtn = document.createElement("button");
                exportBtn.className = "action-btn";
                exportBtn.innerHTML = "📥 Export (CSV)";
                exportBtn.onclick = () => exportToCSV(text);
                
                const copyBtn = document.createElement("button");
                copyBtn.className = "action-btn";
                copyBtn.innerHTML = "📋 Copy";
                copyBtn.onclick = () => { 
                    navigator.clipboard.writeText(text); 
                    showToast("✅ Copied to clipboard!"); 
                };
                
                d.appendChild(document.createElement("br"));
                d.appendChild(exportBtn);
                d.appendChild(copyBtn);
            } else {
                d.textContent = text;
            }
            chat.appendChild(d);
            chat.scrollTop = chat.scrollHeight;
            return d;
        }

        function exportToCSV(text) {
            let csvContent = "data:text/csv;charset=utf-8," + encodeURIComponent(text);
            const link = document.createElement("a");
            link.setAttribute("href", csvContent);
            link.setAttribute("download", "RetailIQ_Report.csv");
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            showToast("✅ Data exported successfully!");
        }

        async function loadStats() {
            try {
                const r = await fetch("/stats");
                const data = await r.json();
                document.getElementById("stat-revenue").innerText = "₹" + (data.revenue || 0).toLocaleString();
                document.getElementById("stat-profit").innerText = "₹" + (data.profit || 0).toLocaleString();
                document.getElementById("stat-orders").innerText = data.orders || 0;
                document.getElementById("stat-low").innerText = data.low_stock || 0;
            } catch (e) { console.log("Stats load failed"); }
        }

        async function send(text) {
            text = (text || q.value).trim();
            if (!text) return;
            q.value = "";
            addMsg(text, "me");
            
            document.getElementById("send").disabled = true;
            const wait = addMsg("", "bot", true);
            try {
                const r = await fetch("/chat", { 
                    method: "POST", 
                    headers: { "Content-Type": "application/json" }, 
                    body: JSON.stringify({ message: text, session: sid, language: currentLang }) 
                });
                const data = await r.json();
                wait.remove();
                addMsg(data.reply, "bot");
            } catch (e) {
                wait.remove();
                addMsg("⚠️ Server error.", "bot");
            } finally {
                document.getElementById("send").disabled = false;
                if(window.innerWidth <= 768) document.getElementById("sidebar").classList.remove("open");
            }
        }

        async function renderDateDashboard(dateStr) {
            if(!dateStr) return;
            addMsg(`📅 Show me sales for ${dateStr}`, "me");
            const wait = addMsg("", "bot", true);
            try {
                const r = await fetch("/quick/date/" + dateStr);
                const data = await r.json();
                wait.remove();
                addMsg(data.reply, "bot");
                if (data.chart_data && typeof Chart !== 'undefined') {
                    const container = document.createElement("div");
                    container.className = "msg bot";
                    const canvas = document.createElement("canvas");
                    container.innerHTML = `<div style="font-weight:bold; margin-bottom:10px;">${data.chart_data.title}</div>`;
                    const chartBox = document.createElement("div");
                    chartBox.className = "chart-container";
                    chartBox.appendChild(canvas);
                    container.appendChild(chartBox);
                    chat.appendChild(container);
                    new Chart(canvas, { type: 'bar', data: { labels: data.chart_data.labels, datasets: [{ label: 'Qty', data: data.chart_data.values, backgroundColor: '#10a37f', borderRadius: 6 }] }, options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } } });
                }
            } catch (e) { wait.remove(); }
        }

        function clearChat() {
            chat.innerHTML = '';
            addMsg("👋 Welcome to RetailIQ. I am ready to assist you.", "bot");
            loadStats();
            if(window.innerWidth <= 768) document.getElementById("sidebar").classList.remove("open");
        }

        document.addEventListener("DOMContentLoaded", () => {
            document.getElementById("send").addEventListener("click", () => send());
            q.addEventListener("keydown", e => { if (e.key === "Enter") send(); });
            
            addMsg("👋 Welcome to RetailIQ. Your business dashboard is ready.", "bot"); 
            loadStats(); 
        });
    </script>
</body>
</html>
"""


import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

_IST = timezone(timedelta(hours=5, minutes=30))
_DB = Path(__file__).resolve().parent / "data" / "retailiq.db"


def _q(sql, args=()):
    con = sqlite3.connect(str(_DB))
    try:
        con.execute("PRAGMA query_only=ON")
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _day(back):
    return (datetime.now(_IST) - timedelta(days=back)).strftime("%Y-%m-%d")


def _tot(d):
    r = _q("SELECT COALESCE(SUM(s.quantity*s.unit_price),0), COALESCE(SUM(s.quantity*(s.unit_price-p.cost_price)),0), COUNT(*), COALESCE(SUM(s.quantity),0) FROM sales s JOIN products p ON p.id=s.product_id WHERE substr(s.sale_date,1,10)=?", (d,))[0]
    return {"revenue": r[0], "profit": r[1], "orders": r[2], "units": r[3]}


@app.get("/dashboard/data")
async def dashboard_data():
    days = [_day(i) for i in range(6, -1, -1)]
    daily = [dict(_tot(d), date=d) for d in days]
    today, yest = daily[-1], daily[-2]
    top = _q("SELECT p.name, SUM(s.quantity), SUM(s.quantity*s.unit_price) FROM sales s JOIN products p ON p.id=s.product_id WHERE substr(s.sale_date,1,10)>=? GROUP BY p.id ORDER BY 3 DESC LIMIT 6", (days[0],))
    cats = _q("SELECT p.category, SUM(s.quantity*s.unit_price) FROM sales s JOIN products p ON p.id=s.product_id WHERE substr(s.sale_date,1,10)>=? GROUP BY p.category ORDER BY 2 DESC", (days[0],))
    sold = {r[0]: r[1] for r in _q("SELECT product_id, SUM(quantity) FROM sales WHERE substr(sale_date,1,10)>=? GROUP BY product_id", (days[0],))}
    prods = _q("SELECT id, name, stock, cost_price FROM products")
    low = []
    for pid, name, stock, cost in prods:
        if stock <= 5:
            rate = sold.get(pid, 0) / 7.0
            low.append({"name": name, "stock": stock, "days_left": round(stock / rate, 1) if rate else None})
    low.sort(key=lambda x: x["stock"])
    recent = {r[0] for r in _q("SELECT DISTINCT product_id FROM sales WHERE substr(sale_date,1,10)>=?", (_day(30),))}
    dead = [(n, st * c) for pid, n, st, c in prods if pid not in recent]
    week_rev = sum(d["revenue"] for d in daily)
    week_profit = sum(d["profit"] for d in daily)
    margin = week_profit / week_rev if week_rev else 0
    last3 = sum(d["revenue"] for d in daily[-3:]) / 3
    prev4 = sum(d["revenue"] for d in daily[:4]) / 4
    ratio = last3 / prev4 if prev4 else 1
    n = max(len(prods), 1)
    parts = {
        "Stock health": round(30 * (1 - len(low) / n)),
        "Profit margin": round(25 * min(margin / 0.35, 1)),
        "Sales trend": round(25 * min(max(ratio, 0), 1)),
        "Dead stock": round(20 * (1 - len(dead) / n)),
    }
    actions = []
    for x in low[:3]:
        extra = " (about %s days left)" % x["days_left"] if x["days_left"] else ""
        actions.append({"level": "high", "text": "Reorder %s: only %s left%s" % (x["name"], x["stock"], extra)})
    if yest["revenue"] and today["revenue"] < 0.6 * yest["revenue"]:
        actions.append({"level": "high", "text": "Sales today are well below yesterday. Check for a stock-out or a slow day."})
    for name, val in dead[:2]:
        actions.append({"level": "mid", "text": "Offer a discount on %s: no sales in 30 days, Rs %d tied up" % (name, val)})
    if top:
        actions.append({"level": "low", "text": "Keep %s well stocked: best seller this week" % top[0][0]})
    return {
        "today": today, "yesterday": yest, "daily": daily,
        "top": [{"name": r[0], "units": r[1], "revenue": r[2]} for r in top],
        "categories": [{"name": r[0], "revenue": r[1]} for r in cats],
        "low_stock": low,
        "dead_value": sum(v for _, v in dead), "dead_count": len(dead),
        "stock_value": sum(st * c for _, _, st, c in prods),
        "score": sum(parts.values()), "parts": parts,
        "actions": actions[:6],
    }


from manage import router as manage_router
app.include_router(manage_router)


@app.get("/", response_class=HTMLResponse)
async def home():
    f = Path(__file__).resolve().parent / "dashboard.html"
    return f.read_text(encoding="utf-8") if f.exists() else PAGE


@app.get("/classic", response_class=HTMLResponse)
async def classic():
    return PAGE
