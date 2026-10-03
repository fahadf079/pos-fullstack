"""
One-time move of your existing SQLite data (pos.db) into the PostgreSQL database.

    python migrate_sqlite_to_postgres.py                       # reads ./pos.db, writes to DATABASE_URL
    python migrate_sqlite_to_postgres.py --sqlite D:\\old\\pos.db --dry-run

What it does
  * creates the tables in PostgreSQL (if they don't exist yet) and refuses to run if the target already has data
  * copies every table with the SAME ids (so PO-00001 / PUR-00001 / EXP-00001 numbers and invoice numbers stay the same)
  * old timestamps are read as shop-local time (POS_TZ, default Asia/Karachi)
  * fills the new History (audit_log) with one line per past sale, refund, stock adjustment, price/name/unit change,
    purchase order, delivery and expense, marked actor "migrated" (who did it was never recorded before)
  * VERIFIES the copy (row counts, money totals, stock totals, ledger integrity). If ANY check fails nothing is saved.
  * your pos.db is only read, never modified — keep it as a backup.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import psycopg

import database

ORDER = ["products", "cart", "movements", "sales", "refunds", "product_log", "suppliers", "purchase_orders",
         "po_items", "receipts", "receipt_items", "expenses"]
HAS_ID = [t for t in ORDER if t != "cart"]
TS_COLS = {"ts", "refund_ts", "closed_ts", "paid_ts", "voided_ts"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sqlite", default="pos.db", help="path to the old SQLite file (default: ./pos.db)")
    ap.add_argument("--dry-run", action="store_true", help="do everything and verify, but save nothing")
    a = ap.parse_args()
    src_path = Path(a.sqlite)
    if not src_path.exists():
        print(f"Can't find {src_path}"); return 2

    src = sqlite3.connect(f"file:{src_path.resolve().as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "products" not in have:
        print("That file has no products table: is it the right pos.db?"); return 2

    database.init()
    out = psycopg.connect(database.DATABASE_URL, options=f"-c timezone={database.TZNAME}")
    try:
        cur = out.cursor()
        for t in ORDER + ["audit_log"]:
            if cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]:
                print(f"The PostgreSQL database already has data in '{t}'. Migrate into a fresh, empty database "
                      f"(nothing was changed)."); return 3

        counts = {}
        for t in ORDER:
            if t not in have:
                counts[t] = (0, 0); continue
            scols = [r[1] for r in src.execute(f"PRAGMA table_info({t})")]
            meta = cur.execute("SELECT column_name, is_nullable, data_type FROM information_schema.columns "
                               "WHERE table_schema='public' AND table_name=%s", (t,)).fetchall()
            pg = {m[0]: (m[1] == "YES", m[2]) for m in meta}
            cols = [c for c in scols if c in pg]
            rows = src.execute(f"SELECT {', '.join(cols)} FROM {t}" + (" ORDER BY id" if t in HAS_ID else "")).fetchall()
            data = []
            for r in rows:
                d = dict(zip(cols, r))
                if t == "sales":                       # very old invoices had no subtotal/discount/tax
                    if d.get("subtotal") is None: d["subtotal"] = d.get("total") or 0
                for c in cols:
                    nullable, typ = pg[c]
                    if c in TS_COLS and d[c] == "": d[c] = None
                    if d[c] is None and not nullable:
                        if "int" in typ or typ == "numeric": d[c] = 0
                        elif typ == "text": d[c] = ""
                        elif typ.startswith("timestamp"): raise SystemExit(f"{t} row {d.get('id')} has no timestamp in '{c}'")
                data.append([d[c] for c in cols])
            if data:
                cur.executemany(f"INSERT INTO {t}({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})", data)
            counts[t] = (len(rows), cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            print(f"  {t:<16} {len(rows):>6} rows copied")

        # ids continue from where the old data left off
        for t in HAS_ID:
            cur.execute(f"SELECT setval(pg_get_serial_sequence('{t}','id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM {t}")

        # History for everything that happened before the History existed
        cur.execute("""
        INSERT INTO audit_log(ts, actor, action, entity, entity_id, summary, details)
        SELECT ts, 'migrated', action, entity, entity_id, summary, details FROM (
          SELECT ts, 'sale.checkout' AS action, 'sale' AS entity, id::text AS entity_id,
                 'Sale #' || id || ': Rs ' || trim(trailing '.' from trim(trailing '0' from total::text)) || COALESCE(' by ' || payment, '') AS summary,
                 jsonb_build_object('total', total, 'payment', payment, 'items', items) AS details FROM sales
          UNION ALL
          SELECT ts, 'sale.refund', 'sale', sale_id::text,
                 'Refund on sale #' || sale_id || ': Rs ' || amount::text || ' — ' || note,
                 jsonb_build_object('amount', amount, 'reason', note, 'items', items) FROM refunds
          UNION ALL
          SELECT ts, 'stock.adjust', 'product', sku, name || ': stock ' || before::text || ' → ' || after::text || ' (' || note || ')',
                 jsonb_build_object('before', before, 'after', after, 'note', note) FROM movements WHERE type = 'ADJUST'
          UNION ALL
          SELECT ts, 'catalog.' || field, 'product', sku,
                 CASE WHEN field = 'created' THEN name || ': created (' || COALESCE(new, '') || ')' ELSE name || ': ' || field || ' ' || COALESCE(old, '') || ' → ' || COALESCE(new, '') END,
                 jsonb_build_object('field', field, 'old', old, 'new', new) FROM product_log
          UNION ALL
          SELECT ts, 'po.created', 'purchase_order', 'PO-' || lpad(id::text, 5, '0'), 'PO-' || lpad(id::text, 5, '0') || ' created', NULL FROM purchase_orders
          UNION ALL
          SELECT ts, 'purchase.received', 'purchase', 'PUR-' || lpad(id::text, 5, '0'),
                 'PUR-' || lpad(id::text, 5, '0') || ' received (PO-' || lpad(po_id::text, 5, '0') || '): Rs ' || total::text || CASE WHEN paid = 1 THEN ' — paid' ELSE ' — unpaid' END,
                 jsonb_build_object('total', total, 'paid', paid = 1) FROM receipts
          UNION ALL
          SELECT paid_ts, 'purchase.paid', 'purchase', 'PUR-' || lpad(id::text, 5, '0'), 'PUR-' || lpad(id::text, 5, '0') || ' marked paid (' || pay_method || ')', NULL FROM receipts WHERE paid = 1 AND paid_ts IS NOT NULL
          UNION ALL
          SELECT ts, 'expense.added', 'expense', 'EXP-' || lpad(id::text, 5, '0'),
                 'EXP-' || lpad(id::text, 5, '0') || ': Rs ' || amount::text || ' ' || category || CASE WHEN payee <> '' THEN ' to ' || payee ELSE '' END,
                 jsonb_build_object('category', category, 'amount', amount, 'payee', payee, 'note', note) FROM expenses
        ) h ORDER BY ts, entity_id
        """)
        n_audit = cur.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        print(f"  {'audit_log':<16} {n_audit:>6} history lines created")

        # ───────── verification: nothing is saved unless every check passes ─────────
        problems = []
        for t, (n_src, n_dst) in counts.items():
            if n_src != n_dst: problems.append(f"{t}: {n_src} rows in SQLite but {n_dst} in PostgreSQL")

        def total(sql_src, sql_dst, label, table=None):
            if table and table not in have: return
            s = src.execute(sql_src).fetchone()[0] or 0
            d = float(cur.execute(sql_dst).fetchone()[0] or 0)
            ok_ = abs(float(s) - d) < 0.005
            print(f"  check {label:<28} {float(s):>14,.2f} -> {d:>14,.2f}  {'OK' if ok_ else 'MISMATCH'}")
            if not ok_: problems.append(f"{label}: {s} vs {d}")
        total("SELECT SUM(stock) FROM products", "SELECT SUM(stock) FROM products", "total stock on hand", "products")
        total("SELECT SUM(total) FROM sales", "SELECT SUM(total) FROM sales", "sales total", "sales")
        total("SELECT SUM(COALESCE(refunded_amount,0)) FROM sales", "SELECT SUM(refunded_amount) FROM sales", "refunded amount", "sales")
        total("SELECT SUM(total) FROM receipts", "SELECT SUM(total) FROM receipts", "purchases total", "receipts")
        total("SELECT SUM(amount) FROM expenses", "SELECT SUM(amount) FROM expenses", "expenses total", "expenses")
        total("SELECT SUM(delta) FROM movements", "SELECT SUM(delta) FROM movements", "ledger movement total", "movements")
        total("SELECT SUM(qty) FROM cart", "SELECT SUM(qty) FROM cart", "qty sitting in cart", "cart")
        bad_src = src.execute("SELECT COUNT(*) FROM products p JOIN movements m ON m.id=(SELECT MAX(id) FROM movements WHERE sku=p.sku) WHERE ROUND(m.after,3)!=ROUND(p.stock,3)").fetchone()[0] if "movements" in have else 0
        bad_dst = cur.execute("SELECT COUNT(*) FROM products p JOIN movements m ON m.id=(SELECT MAX(id) FROM movements WHERE sku=p.sku) WHERE m.after!=p.stock").fetchone()[0]
        print(f"  check {'ledger matches stock':<28} products out of step: SQLite {bad_src}, PostgreSQL {bad_dst}  {'OK' if bad_dst <= bad_src else 'MISMATCH'}")
        if bad_dst > bad_src: problems.append("ledger no longer matches stock")

        if problems:
            out.rollback()
            print("\nVERIFICATION FAILED, nothing was saved:\n  - " + "\n  - ".join(problems)); return 1
        if a.dry_run:
            out.rollback(); print("\nDRY RUN: everything checks out; nothing was saved."); return 0
        out.commit()
        print("\nDONE: all data copied and verified. Keep pos.db as a backup; the app now uses PostgreSQL.")
        return 0
    finally:
        out.close()


if __name__ == "__main__":
    sys.exit(main())
