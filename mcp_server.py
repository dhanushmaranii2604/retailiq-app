import sqlite3
import os
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("RetailIQ: AI Sales & Stock Assistant")

# Render Cloud Path vs Local Path
if os.getenv("RENDER"):
    DB_PATH = Path("/opt/render/project/src/data/retailiq.db")
else:
    DB_PATH = Path(__file__).resolve().parent / "data" / "retailiq.db"

IST = ZoneInfo("Asia/Kolkata")
MAX_DAYS = 366

def connect():
    """Open the database read-only so read tools can never change shop data."""
    return sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)

def connect_write():
    """Open the database for writing (needed for Sales and Udhaar)."""
    return sqlite3.connect(str(DB_PATH))

def parse_range(start_date, end_date):
    """Return (start, end) dates. Defaults to yesterday in India time."""
    yesterday = datetime.now(IST).date() - timedelta(days=1)
    try:
        start = date.fromisoformat(start_date) if start_date else yesterday
        end = date.fromisoformat(end_date) if end_date else start
    except ValueError:
        raise ValueError("Dates must be in YYYY-MM-DD format, e.g. 2026-10-26")
    if end < start:
        start, end = end, start
    if (end - start).days >= MAX_DAYS:
        raise ValueError("Please ask for a range of one year or less")
    return start, end

# ==========================================
# READ TOOLS
# ==========================================

@mcp.tool()
def get_sales_summary(start_date: str | None = None, end_date: str | None = None) -> dict:
    """Get total sales revenue in rupees, units sold and number of sales records for one day or a date range."""
    start, end = parse_range(start_date, end_date)
    with closing(connect()) as db:
        row = db.execute(
            "SELECT COALESCE(SUM(quantity * unit_price), 0), COALESCE(SUM(quantity), 0), COUNT(*) FROM sales WHERE sale_date BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
    return {
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "total_sales": row[0], "units_sold": row[1], "transactions": row[2],
    }

@mcp.tool()
def get_daily_sales(start_date: str, end_date: str) -> list:
    """Get a day-by-day breakdown of sales revenue and units sold between two dates."""
    start, end = parse_range(start_date, end_date)
    with closing(connect()) as db:
        rows = db.execute(
            "SELECT sale_date, COALESCE(SUM(quantity * unit_price), 0), COALESCE(SUM(quantity), 0), COUNT(*) FROM sales WHERE sale_date BETWEEN ? AND ? GROUP BY sale_date ORDER BY sale_date",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    if not rows:
        return [{"note": f"No sales were recorded between {start} and {end}."}]
    return [{"date": r[0], "total_sales": r[1], "units_sold": r[2], "transactions": r[3]} for r in rows]

@mcp.tool()
def get_low_stock(threshold: int = 5) -> list:
    """List products with stock at or below the specified threshold."""
    threshold = max(0, min(threshold, 10000))
    with closing(connect()) as db:
        rows = db.execute("SELECT name, stock, price FROM products WHERE stock <= ? ORDER BY stock ASC", (threshold,)).fetchall()
    if not rows:
        return [{"note": f"No products have stock at or below {threshold}."}]
    return [{"product": r[0], "stock": r[1], "price": r[2]} for r in rows]

@mcp.tool()
def get_product_stock(product_name: str) -> list:
    """Look up the current stock and price of products whose name contains the given text."""
    text = product_name.strip()[:60]
    if not text:
        raise ValueError("Please give a product name")
    pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with closing(connect()) as db:
        rows = db.execute("SELECT name, stock, price FROM products WHERE name LIKE ? ESCAPE '\\' ORDER BY name LIMIT 20", (pattern,)).fetchall()
    if not rows:
        return [{"note": f"No product found matching '{text}'."}]
    return [{"product": r[0], "stock": r[1], "price": r[2]} for r in rows]

@mcp.tool()
def get_top_products(start_date: str | None = None, end_date: str | None = None, limit: int = 5) -> list:
    """Get the best-selling products by units sold, with revenue, for one day or a date range."""
    start, end = parse_range(start_date, end_date)
    limit = max(1, min(limit, 20))
    with closing(connect()) as db:
        rows = db.execute(
            "SELECT p.name, SUM(s.quantity), SUM(s.quantity * s.unit_price) FROM sales s JOIN products p ON p.id = s.product_id WHERE s.sale_date BETWEEN ? AND ? GROUP BY p.id, p.name ORDER BY SUM(s.quantity) DESC, p.name LIMIT ?",
            (start.isoformat(), end.isoformat(), limit),
        ).fetchall()
    if not rows:
        return [{"note": f"No sales were recorded between {start} and {end}."}]
    return [{"product": r[0], "units_sold": r[1], "revenue": r[2]} for r in rows]

@mcp.tool()
def get_dead_stock(days: int = 30) -> list:
    """Find products that have not been sold in the last X days."""
    days = max(1, min(days, 365))
    cutoff_date = (datetime.now(IST).date() - timedelta(days=days)).isoformat()
    with closing(connect()) as db:
        rows = db.execute(
            "SELECT p.name, p.stock, p.price FROM products p LEFT JOIN sales s ON p.id = s.product_id AND s.sale_date >= ? WHERE s.product_id IS NULL AND p.stock > 0 ORDER BY p.stock DESC",
            (cutoff_date,),
        ).fetchall()
    if not rows:
        return [{"note": f"Great news! No dead stock found. All items have sold in the last {days} days."}]
    return [{"product": r[0], "stock": r[1], "price": r[2]} for r in rows]

@mcp.tool()
def predict_stockout() -> list:
    """Predict which products will run out of stock in the next 7 days based on recent sales velocity."""
    cutoff_date = (datetime.now(IST).date() - timedelta(days=14)).isoformat()
    with closing(connect()) as db:
        rows = db.execute(
            "SELECT p.name, p.stock, COALESCE(SUM(s.quantity), 0) as units_sold_14d FROM products p LEFT JOIN sales s ON p.id = s.product_id AND s.sale_date >= ? WHERE p.stock > 0 GROUP BY p.id, p.name",
            (cutoff_date,),
        ).fetchall()
    predictions = []
    for r in rows:
        name, stock, units_sold = r
        if units_sold == 0: continue
        avg_daily_sales = units_sold / 14.0
        days_left = stock / avg_daily_sales
        if days_left <= 7:
            predictions.append({"product": name, "current_stock": stock, "daily_sales_rate": round(avg_daily_sales, 1), "days_until_empty": round(days_left, 1)})
    predictions = sorted(predictions, key=lambda x: x["days_until_empty"])
    if not predictions:
        return [{"note": "All products have healthy stock levels for the next 7 days."}]
    return predictions

@mcp.tool()
def calculate_profit(days: int = 1) -> dict:
    """Calculate revenue and profit for the last X days."""
    days = max(1, min(days, 365))
    start_date = (datetime.now(IST).date() - timedelta(days=days-1)).isoformat()
    end_date = datetime.now(IST).date().isoformat()
    with closing(connect()) as db:
        query = """
            SELECT COALESCE(SUM(s.quantity * s.unit_price), 0) as revenue, COALESCE(SUM((s.unit_price - p.cost_price) * s.quantity), 0) as profit
            FROM sales s JOIN products p ON s.product_id = p.id WHERE s.sale_date BETWEEN ? AND ?
        """
        row = db.execute(query, (start_date, end_date)).fetchone()
    return {"period": f"{start_date} to {end_date}", "revenue": round(row[0], 2), "profit": round(row[1], 2)}


# ==========================================
# WRITE TOOLS (SALES & UDHAAR)
# ==========================================

@mcp.tool()
def record_sale(product_name: str, quantity: int) -> dict:
    """Record a sale, deduct the stock, and add to revenue."""
    with closing(connect_write()) as db:
        cursor = db.cursor()
        
        # 1. Find the product (allows partial matching like "cap" for "Cap")
        cursor.execute("SELECT id, name, stock, price FROM products WHERE name LIKE ?", (f"%{product_name}%",))
        product = cursor.fetchone()

        if not product:
            return {"status": "error", "message": f"Product '{product_name}' not found."}

        prod_id, real_name, stock, price = product

        # 2. Check if enough stock exists
        if stock < quantity:
            return {"status": "error", "message": f"Not enough stock. You only have {stock} {real_name}(s) left."}

        # 3. Calculate total and date
        total_price = quantity * price
        date_now = datetime.now(IST).strftime("%Y-%m-%d")

        # 4. Insert into sales table
        cursor.execute(
            "INSERT INTO sales (product_id, quantity, unit_price, sale_date) VALUES (?, ?, ?, ?)",
            (prod_id, quantity, price, date_now)
        )

        # 5. Deduct from inventory
        cursor.execute(
            "UPDATE products SET stock = stock - ? WHERE id = ?",
            (quantity, prod_id)
        )
        db.commit()

    return {
        "status": "success",
        "message": f"Recorded sale: {quantity} {real_name}(s) for ₹{total_price}. Remaining stock: {stock - quantity}."
    }

@mcp.tool()
def add_udhaar(customer_name: str, amount: float, phone: str = "") -> dict:
    """Record a new credit (udhaar) for a customer."""
    with closing(connect_write()) as db:
        cursor = db.cursor()
        date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            "INSERT INTO credits (customer_name, amount, phone, created_at) VALUES (?, ?, ?, ?)",
            (customer_name, amount, phone, date_now)
        )
        db.commit()
    return {"status": "success", "message": f"Added ₹{amount} udhaar for {customer_name}."}

@mcp.tool()
def get_pending_udhaar() -> list:
    """Get a list of all customers who have pending udhaar (credit)."""
    with closing(connect_write()) as db:
        rows = db.execute("SELECT customer_name, amount, phone FROM credits WHERE status = 'pending'").fetchall()
    if not rows:
        return [{"note": "Great news! No pending udhaar."}]
    return [{"customer": r[0], "amount": r[1], "phone": r[2]} for r in rows]

@mcp.tool()
def clear_udhaar(customer_name: str) -> dict:
    """Mark a customer's udhaar as paid/cleared."""
    with closing(connect_write()) as db:
        cursor = db.cursor()
        cursor.execute("UPDATE credits SET status = 'paid' WHERE customer_name LIKE ? AND status = 'pending'", (f"%{customer_name}%",))
        db.commit()
        count = cursor.rowcount
    if count > 0:
        return {"status": "success", "message": f"Cleared udhaar for {customer_name}."}
    return {"status": "error", "message": f"No pending udhaar found for {customer_name}."}

@mcp.tool()
def explain_sales_change(day: str = "") -> dict:
    """Explain why sales on one day (YYYY-MM-DD, default yesterday) were higher or lower than the day before. Returns only facts found in the data: revenue, bills, average bill, biggest product gains and drops, price changes, possible stock-outs and the same-weekday average. Explain using these facts only and never invent causes."""
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date() if day else datetime.now(IST).date() - timedelta(days=1)
    except ValueError:
        return {"error": "Date must be YYYY-MM-DD"}
    p = d - timedelta(days=1)
    with closing(connect()) as db:
        def tot(x):
            r = db.execute("SELECT COALESCE(SUM(quantity*unit_price),0), COUNT(*) FROM sales WHERE substr(sale_date,1,10)=?", (x.isoformat(),)).fetchone()
            return float(r[0]), int(r[1])

        def prod(x):
            q = "SELECT p.name, SUM(s.quantity*s.unit_price), AVG(s.unit_price) FROM sales s JOIN products p ON p.id=s.product_id WHERE substr(s.sale_date,1,10)=? GROUP BY p.id"
            return {r[0]: (float(r[1]), float(r[2])) for r in db.execute(q, (x.isoformat(),))}

        rev, bills = tot(d)
        prev, pbills = tot(p)
        a, b = prod(d), prod(p)
        moves = sorted(((n, round(a.get(n, (0, 0))[0] - b.get(n, (0, 0))[0])) for n in set(a) | set(b)), key=lambda m: m[1])
        changes = [{"product": n, "old_price": round(b[n][1]), "new_price": round(a[n][1])} for n in a if n in b and abs(a[n][1] - b[n][1]) > 0.03 * b[n][1]]
        low = {r[0] for r in db.execute("SELECT name FROM products WHERE stock <= 5")}
        gone = [n for n in b if n not in a and n in low]
        same = [tot(d - timedelta(days=7 * k))[0] for k in range(1, 5)]
        same = [v for v in same if v > 0]
    change = round((rev - prev) / prev * 100, 1) if prev else None
    return {
        "date": d.isoformat(),
        "weekday": d.strftime("%A"),
        "revenue": round(rev),
        "previous_day_revenue": round(prev),
        "change_percent": change,
        "bills": bills,
        "previous_day_bills": pbills,
        "average_bill": round(rev / bills) if bills else 0,
        "previous_day_average_bill": round(prev / pbills) if pbills else 0,
        "biggest_drops": [{"product": n, "change": c} for n, c in moves[:3] if c < 0],
        "biggest_gains": [{"product": n, "change": c} for n, c in reversed(moves[-3:]) if c > 0],
        "price_changes": changes,
        "possible_stockouts": gone,
        "same_weekday_average": round(sum(same) / len(same)) if same else None,
        "note": "Stock history is not recorded, so stock-outs are possible, not certain.",
    }



if __name__ == "__main__":
    mcp.run(transport="stdio")