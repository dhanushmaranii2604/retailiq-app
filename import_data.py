import argparse
import csv
import hashlib
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB = Path("data/retailiq.db")
FOLDER = Path("imports")
DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y")

EXAMPLE_PRODUCTS = """name,category,price,stock
Jeans,Bottomwear,1200,25
Kurta,Ethnic,900,18
"""
EXAMPLE_SALES = """date,product,quantity,unit_price
2026-11-05,Jeans,2,1200
2026-11-05,Kurta,1,900
"""


def parse_date(text):
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"bad date '{text}' (use YYYY-MM-DD or DD-MM-YYYY)")


def whole(text, what):
    n = float(text)
    if n != int(n):
        raise ValueError(f"{what} must be a whole number")
    return int(n)


def read_rows(path, needed):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = [c.strip().lower() for c in (reader.fieldnames or [])]
        missing = [c for c in needed if c not in cols]
        if missing:
            sys.exit(
                f"{path.name}: missing column(s): {', '.join(missing)}. "
                f"Found: {', '.join(cols)}"
            )
        rows = []
        for line, raw in enumerate(reader, start=2):
            row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
            if any(row.values()):
                rows.append((line, row))
        return rows


def check_products(rows):
    good, errors = [], []
    for line, r in rows:
        try:
            if not r["name"]:
                raise ValueError("name is empty")
            price = float(r["price"])
            stock = whole(r["stock"], "stock")
            if price < 0 or stock < 0:
                raise ValueError("price and stock cannot be negative")
            good.append((r["name"], r.get("category") or "General", price, stock))
        except (ValueError, KeyError) as e:
            errors.append(f"products.csv line {line}: {e}")
    return good, errors


def check_sales(rows, known):
    good, errors = [], []
    for line, r in rows:
        try:
            key = r["product"].lower()
            if key not in known:
                raise ValueError(f"unknown product '{r['product']}'")
            qty = whole(r["quantity"], "quantity")
            price = float(r["unit_price"])
            if qty <= 0 or price < 0:
                raise ValueError("quantity must be above 0 and price cannot be negative")
            good.append((known[key], qty, price, parse_date(r["date"])))
        except (ValueError, KeyError) as e:
            errors.append(f"sales.csv line {line}: {e}")
    return good, errors


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--templates", action="store_true", help="create example CSV files")
    ap.add_argument("--apply", action="store_true", help="really write to the database")
    ap.add_argument("--fresh", action="store_true", help="erase current products and sales first")
    args = ap.parse_args()

    if args.templates:
        FOLDER.mkdir(exist_ok=True)
        for name, text in (("products.csv", EXAMPLE_PRODUCTS), ("sales.csv", EXAMPLE_SALES)):
            f = FOLDER / name
            if f.exists():
                print(f"{f} already exists, left unchanged")
            else:
                f.write_text(text, encoding="utf-8")
                print(f"Created {f}")
        return

    if not DB.exists():
        sys.exit("data/retailiq.db not found. Run this inside the retailiq folder.")
    p_file = FOLDER / "products.csv"
    s_file = FOLDER / "sales.csv"
    if not p_file.exists() and not s_file.exists():
        sys.exit("No CSV files found. Run: python import_data.py --templates")

    db = sqlite3.connect(DB)
    db.execute("PRAGMA foreign_keys = ON")
    has_log = db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='import_log'"
    ).fetchone()

    def done_before(h):
        return bool(
            has_log
            and not args.fresh
            and db.execute("SELECT 1 FROM import_log WHERE file_hash=?", (h,)).fetchone()
        )

    errors, products, sales, hashes = [], [], [], []

    if p_file.exists():
        h = file_hash(p_file)
        if done_before(h):
            print("products.csv was already imported (same contents). Skipping.")
        else:
            products, errs = check_products(read_rows(p_file, ["name", "price", "stock"]))
            errors += errs
            hashes.append((p_file.name, h))

    known = (
        {}
        if args.fresh
        else {n.lower(): n for (n,) in db.execute("SELECT name FROM products")}
    )
    for name, *_ in products:
        known[name.lower()] = name

    if s_file.exists():
        h = file_hash(s_file)
        if done_before(h):
            print("sales.csv was already imported (same contents). Skipping.")
        else:
            sales, errs = check_sales(
                read_rows(s_file, ["date", "product", "quantity", "unit_price"]), known
            )
            errors += errs
            hashes.append((s_file.name, h))

    print(f"Products to add or update: {len(products)}")
    print(f"Sales rows to add: {len(sales)}")
    if args.fresh:
        print("--fresh: ALL current products and sales will be erased first")

    if errors:
        print(f"\n{len(errors)} problem(s) found. Nothing was imported:")
        for e in errors[:20]:
            print(" -", e)
        sys.exit(1)
    if not products and not sales:
        print("Nothing new to import.")
        return
    if not args.apply:
        print("\nDry run only. Nothing was changed. Add --apply to import.")
        return

    if args.fresh and input("Type YES to erase ALL current products and sales: ").strip() != "YES":
        sys.exit("Cancelled. Nothing was changed.")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = DB.with_name(f"retailiq_before_import_{stamp}.db")
    shutil.copy(DB, backup)
    print("Backup saved to", backup)

    db.execute(
        "CREATE TABLE IF NOT EXISTS import_log "
        "(file_name TEXT, file_hash TEXT PRIMARY KEY, imported_at TEXT)"
    )
    with db:
        if args.fresh:
            db.execute("DELETE FROM sales")
            db.execute("DELETE FROM products")
            db.execute("DELETE FROM import_log")
        db.executemany(
            "INSERT INTO products (name, category, price, stock) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET category=excluded.category, "
            "price=excluded.price, stock=excluded.stock",
            products,
        )
        ids = {n: i for i, n in db.execute("SELECT id, name FROM products")}
        db.executemany(
            "INSERT INTO sales (product_id, quantity, unit_price, sale_date) "
            "VALUES (?, ?, ?, ?)",
            [(ids[n], q, p, d) for n, q, p, d in sales],
        )
        now = datetime.now().isoformat(timespec="seconds")
        db.executemany(
            "INSERT OR REPLACE INTO import_log VALUES (?, ?, ?)",
            [(n, h, now) for n, h in hashes],
        )
    db.close()
    print("Import complete.")


if __name__ == "__main__":
    main()