"""Scanner + card check with dummy values (throw-away database whose name ends in _test).

A barcode scanner is a keyboard: it types the digits and presses Enter, and the screen sends them to POST /scan. A card
sale is a normal checkout with the payment method Card (there is no card-machine driver yet). This script runs both with
dummy people and the sample products and checks that stock, cart, sale, History, refund and the card vault all agree,
that the request pipeline order holds, and that nothing hangs.

Run from the backend folder (Windows):
    set POS_TEST_DATABASE_URL=postgresql://pos:pos@localhost:5432/pos_test
    python tests/check_scan_and_card.py
"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); BACK = os.path.dirname(HERE); sys.path.insert(0, BACK)
TEST_URL = os.environ.get("POS_TEST_DATABASE_URL", "postgresql://pos:pos@localhost:5432/pos_test")
assert TEST_URL.split("?")[0].rstrip("/").endswith("_test")          # this script wipes its database: only a *_test one
os.environ["DATABASE_URL"] = TEST_URL; os.environ["POS_SECRET_KEY"] = "test-secret-key"; os.environ["POS_TEST_MODE"] = "1"
os.environ["POS_ALLOW_REMOTE_SETUP"] = "1"; os.environ["POS_THROTTLE_MAX"] = "1000000"
import psycopg
with psycopg.connect(TEST_URL, autocommit=True) as _c:
    _c.execute("DROP SCHEMA public CASCADE"); _c.execute("CREATE SCHEMA public")
from fastapi.testclient import TestClient
import main, pipeline

def ok(r, code=200): assert r.status_code == code, (r.status_code, r.text); return r.json()
def sql(q, *a): return main.db.execute(q, a).fetchall()
def client(): n = TestClient(main.app); n.headers["X-POS"] = "1"; return n
def pin(p): return {"X-POS-PIN": p}
def ean(code12): return code12 + str((10 - sum(int(ch) * (3 if i % 2 else 1) for i, ch in enumerate(code12)) % 10) % 10)

slow = []
def timed(label, fn):
    t = time.time(); r = fn(); slow.append((label, round(time.time() - t, 3))); return r

owner = client(); ok(owner.post("/auth/setup", json={"username": "owner", "full_name": "Owner", "password": "Owner-Test-91"}))
for n, p in (("salesman1", "4829"), ("salesman2", "5731"), ("salesman3", "6482")):
    ok(owner.post("/users", json={"username": n, "full_name": n, "role": "employee", "pin": p}))
s1 = client(); ok(s1.post("/auth/pin-login", json={"username": "salesman1", "pin": "4829"}))
stock = lambda sku: float(sql("SELECT stock FROM products WHERE sku=?", sku)[0][0])
skus = [r[0] for r in sql("SELECT sku FROM products ORDER BY id")]; assert len(skus) >= 10
start = {k: stock(k) for k in skus}; sold = {k: 0.0 for k in skus}

# 1. scanner: every sample barcode, one scan each
for k in skus:
    timed("scan", lambda: ok(s1.post("/scan", json={"sku": k, "qty": 1}))); sold[k] += 1
    assert stock(k) == start[k] - sold[k], k
assert len(ok(s1.get("/cart"))["items"]) == len(skus)
for _ in range(3): ok(s1.post("/scan", json={"sku": skus[0], "qty": 1})); sold[skus[0]] += 1       # same barcode scanned again and again
assert stock(skus[0]) == start[skus[0]] - 4
print("scanner: every barcode scanned, stock dropped at once, repeat scans add up")

# 2. bad scans change nothing
before = {k: stock(k) for k in skus}; cart0 = ok(s1.get("/cart"))["items"]
assert s1.post("/scan", json={"sku": "0000000000000", "qty": 1}).status_code == 404
assert s1.post("/scan", json={"sku": "", "qty": 1}).status_code in (404, 422)
assert s1.post("/scan", json={"sku": skus[1], "qty": 10000}).status_code == 409                    # more than is on the shelf
assert {k: stock(k) for k in skus} == before and ok(s1.get("/cart"))["items"] == cart0
print("scanner: unknown, empty and too-many scans are refused and change nothing")

# 3. weighed-item label from a price-computing scale
ok(owner.post("/catalog/products", json={"sku": "12345", "name": "Test Weighed Item", "cat": "Grocery", "unit": "kg", "price": 100, "cost": 60}))
ok(owner.post("/inventory/adjust", json={"sku": "12345", "new_stock": 10, "note": "dummy opening stock"}))
ok(owner.post("/settings", json={"key": "scale_barcodes", "value": True}))
ok(s1.post("/scan", json={"sku": ean("201234501500"), "qty": 1})); assert stock("12345") == 8.5          # 1.500 kg from the label
bad = ean("201234501500")[:-1] + str((int(ean("201234501500")[-1]) + 1) % 10)
assert s1.post("/scan", json={"sku": bad, "qty": 1}).status_code == 422 and stock("12345") == 8.5     # damaged label refused
sold["12345"] = 1.5; start["12345"] = 10.0
print("scanner: weighed label adds 1.5 kg; a damaged label is refused")

# 4. card sale
cart = ok(s1.get("/cart")); total = cart["totals"]["total"]
assert s1.post("/checkout", json={"payment_method": "Cheque"}).status_code == 400 and ok(s1.get("/cart"))["items"]      # unknown method: nothing lost
trace = pipeline.TRACE = []
card_sold = dict(sold)
rec = timed("checkout", lambda: ok(s1.post("/checkout", json={"payment_method": "Card"})))["receipt"]; sid = rec["id"]
assert trace == ["network", "identity", "lock", "permission", "confirmation"], trace          # safety gates first, then the sale
pipeline.TRACE = None
row = sql("SELECT payment, total, actor FROM sales WHERE id=?", sid)[0]
assert row[0] == "Card" and abs(float(row[1]) - total) < 0.01 and row[2] == "salesman1", row
assert ok(s1.get("/cart"))["items"] == []                                                          # cart emptied
assert all(stock(k) == start[k] - sold[k] for k in sold)                                           # checkout never deducts a second time
assert sql("SELECT COUNT(*) FROM drawer_events WHERE sale_id=?", sid)[0][0] == 0                   # a card sale never opens the drawer
assert sql("SELECT COUNT(*) FROM audit_log WHERE action='sale.checkout' AND entity_id=?", str(sid))[0][0] == 1
print("card: sale saved as Card by salesman1, cart emptied, stock correct, no drawer, History written")

# 5. wallet and cash sales
for method in ("Wallet", "Cash"):
    ok(s1.post("/scan", json={"sku": skus[2], "qty": 2})); sold[skus[2]] += 2
    r2 = ok(s1.post("/checkout", json={"payment_method": method}))["receipt"]
    assert sql("SELECT payment FROM sales WHERE id=?", r2["id"])[0][0] == method
    assert sql("SELECT COUNT(*) FROM drawer_events WHERE sale_id=?", r2["id"])[0][0] == (1 if method == "Cash" else 0)
print("wallet and cash: saved; only cash opens the drawer")

# 6. refund of the card sale needs the employee's own PIN and puts its stock back
r0 = s1.post(f"/sales/{sid}/refund", json={"note": "dummy refund"}); assert r0.status_code == 403 and r0.json()["detail"]["code"] == "pin_required", r0.text
ok(s1.post(f"/sales/{sid}/refund", json={"note": "dummy refund", "paid_via": "Card"}, headers=pin("4829")))
assert sql("SELECT refunded FROM sales WHERE id=?", sid)[0][0] == 1
for k in sold:
    assert stock(k) == start[k] - (sold[k] - card_sold[k]), (k, stock(k))                       # only the wallet/cash sales stay deducted
print("refund: needs the employee's own PIN; the card sale's stock is back")

# 7. card machine credentials: write-only
secret = "SECRET-ABC-999"
ok(owner.post("/integrations/card/credentials", json={"merchant_id": "MERCH-1", "api_key": secret}))
r = owner.get("/integrations/card"); assert r.json()["configured"] is True and secret not in r.text and "MERCH-1" not in r.text
assert s1.get("/integrations/card").status_code == 403
dump = " ".join(str(c) for row in sql("SELECT * FROM vault") for c in row); assert secret not in dump and "MERCH-1" not in dump
assert secret not in " ".join(str(c) for row in sql("SELECT summary, details FROM audit_log") for c in row)
print("card vault: stored sealed, never returned, never in History, employees refused")

# 8. nothing hung
worst = max(slow, key=lambda x: x[1]); assert worst[1] < 3, slow
print(f"slowest call: {worst[0]} {worst[1]}s")
print("\nSCANNER + CARD CHECK PASSED")
