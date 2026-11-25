import sqlite3
from datetime import timedelta
from pathlib import Path

DB = Path(__file__).resolve().parent / "data" / "retailiq.db"


def _n(x):
    return format(round(x), ",")


def day_report(d):
    db = sqlite3.connect(str(DB))
    try:
        def tot(x):
            return db.execute(
                "SELECT COALESCE(SUM(s.quantity*s.unit_price),0), "
                "COALESCE(SUM(s.quantity*(s.unit_price-COALESCE(p.cost_price,0))),0), "
                "COUNT(*), COALESCE(SUM(s.quantity),0) FROM sales s "
                "JOIN products p ON p.id=s.product_id WHERE substr(s.sale_date,1,10)=?",
                (x.isoformat(),)).fetchone()

        rev, pro, bills, units = tot(d)
        prev = tot(d - timedelta(days=1))[0]
        rows = db.execute(
            "SELECT p.name, SUM(s.quantity), SUM(s.quantity*s.unit_price) FROM sales s "
            "JOIN products p ON p.id=s.product_id WHERE substr(s.sale_date,1,10)=? "
            "GROUP BY p.id ORDER BY 3 DESC", (d.isoformat(),)).fetchall()
        low = db.execute("SELECT name, stock FROM products WHERE stock<=5 ORDER BY stock").fetchall()
    finally:
        db.close()
    out = []
    if not bills:
        out.append("No sales recorded for this day.")
    else:
        chg = " (%+.0f%% vs the day before)" % ((rev - prev) / prev * 100) if prev else ""
        out.append("Revenue: Rs %s%s" % (_n(rev), chg))
        out.append("Profit: Rs %s (margin %.0f%%)" % (_n(pro), pro / rev * 100 if rev else 0))
        out.append("Bills: %d, units sold: %d, average bill: Rs %s" % (bills, units, _n(rev / bills)))
        out.append("")
        out.append("Products sold:")
        for name, q, amt in rows:
            out.append("- %s: %d units, Rs %s" % (name, q, _n(amt)))
    out.append("")
    if low:
        out.append("Low stock: " + ", ".join("%s (%d)" % (n, s) for n, s in low))
    else:
        out.append("Stock levels are healthy.")
    return "\n".join(out)
