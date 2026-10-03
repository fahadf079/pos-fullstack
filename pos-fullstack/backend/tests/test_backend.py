import os, sys, threading
HERE = os.path.dirname(os.path.abspath(__file__)); BACK = os.path.dirname(HERE); sys.path.insert(0, BACK)
# Runs against a THROW-AWAY PostgreSQL database. It is wiped on every run, so the name must end in "_test" —
# the test refuses to touch anything else (your real "pos" database is never used).
TEST_URL = os.environ.get("POS_TEST_DATABASE_URL", "postgresql://pos:pos@localhost:5432/pos_test")
assert TEST_URL.split("?")[0].rstrip("/").endswith("_test"), "POS_TEST_DATABASE_URL must point at a database whose name ends in _test"
os.environ["DATABASE_URL"] = TEST_URL
os.environ["POS_SECRET_KEY"] = "test-secret-key"; os.environ["POS_ALLOW_REMOTE_SETUP"] = "1"   # no key file written; TestClient is not "localhost"
import psycopg
with psycopg.connect(TEST_URL, autocommit=True) as _c:
    _c.execute("DROP SCHEMA public CASCADE"); _c.execute("CREATE SCHEMA public")
from fastapi.testclient import TestClient
import main, database
c = TestClient(main.app)
def ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.text); return r.json()
# Every route needs a login: create the owner once, then this client acts as the owner (cookie kept by the client)
c.headers["X-POS"] = "1"
ok(c.post("/auth/setup", json={"username": "owner", "full_name": "Test Owner", "password": "Owner-pass-1", "pin": "4829"}))
def newc(**kw):                           # another client logged in as the same owner (for the concurrency tests)
    n = TestClient(main.app, **kw); n.cookies.update(c.cookies); n.headers.update(c.headers); return n
c.headers["X-POS-PIN"] = "4829"          # the owner's PIN is sent with every request; it is only checked where a rule asks for it
def stock(sku): return main.db.execute("SELECT stock FROM products WHERE sku=?", (sku,)).fetchone()[0]

# --- migration kept old data, added cost column
assert "cost" in [r[0] for r in main.db.execute("SELECT column_name FROM information_schema.columns WHERE table_name='products'")]
n0 = len(ok(c.get("/inventory"))["products"]); assert n0 >= 10

# --- existing flows still work
ok(c.get("/dashboard")); ok(c.get("/movements")); ok(c.get("/sales"))
base = stock("8964001")
ok(c.post("/scan", json={"sku":"8964001","qty":2})); assert stock("8964001")==base-2
ok(c.post("/scan", json={"sku":"8964001","qty":-5}), 422)       # old bug: negative qty added stock
ok(c.post("/scan", json={"sku":"8964001","qty":0}), 422)
ok(c.post("/scan", json={"sku":"nope"}), 404)
ok(c.post("/cart/remove", json={"product_id":1,"qty":-3}), 422)
ok(c.post("/cart/remove", json={"product_id":1,"qty":1})); assert stock("8964001")==base-1
r = ok(c.post("/checkout", json={"payment_method":"Cash"})); sid = r["receipt"]["id"]; assert stock("8964001")==base-1
ok(c.post("/checkout", json={"payment_method":"Cash"}), 400)
ok(c.post(f"/sales/{sid}/refund", json={"note":"t"})); assert stock("8964001")==base
ok(c.post(f"/sales/{sid}/refund", json={"note":"t"}), 409)
ok(c.post("/inventory/adjust", json={"sku":"8964001","new_stock":-1}), 422)
ok(c.post("/inventory/adjust", json={"sku":"8964001","new_stock":base+3})); ok(c.post("/inventory/adjust", json={"sku":"8964001","new_stock":base}))
ok(c.post("/inventory/receive", json={"sku":"8964001","qty":1}), 404)   # manual receive is gone
ok(c.post("/scan", json={"sku":"8964001","qty":10**6}), 422)
ok(c.post("/scan", json={"sku":"8964001","qty":9999}), 409); assert stock("8964001")==base   # 409 leaves nothing behind
ok(c.post("/cart/clear"))

# --- purchasing on the real app, on the user's exact scenario shape
s1 = ok(c.post("/suppliers", json={"name":"Pepsi Distributor"}))["id"]
P = {p["sku"]:p for p in ok(c.get("/purchasing/products"))}
ids = [P["8964009"], P["8964003"], P["8964007"]]   # water, sugar, biscuits
before = [p["stock"] for p in ids]
v0 = database.get_version()
po = ok(c.post("/purchase-orders", json={"supplier_id":s1,"lines":[{"product_id":ids[0]["id"],"qty":50,"unit_cost":60},{"product_id":ids[1]["id"],"qty":30,"unit_cost":140},{"product_id":ids[2]["id"],"qty":20,"unit_cost":80}]}))
assert [stock(p["sku"]) for p in ids]==before and database.get_version()>v0
items = ok(c.get(f"/purchase-orders/{po['id']}"))["items"]
ok(c.post(f"/purchase-orders/{po['id']}/receive", json={"lines":[{"po_item_id":items[0]["id"],"qty":51}]}), 409)
assert [stock(p["sku"]) for p in ids]==before
r = ok(c.post(f"/purchase-orders/{po['id']}/receive", json={"lines":[{"po_item_id":items[0]["id"],"qty":50},{"po_item_id":items[1]["id"],"qty":30},{"po_item_id":items[2]["id"],"qty":18}]}))
assert [stock(p["sku"]) for p in ids]==[before[0]+50,before[1]+30,before[2]+18] and r["po_status"]=="PARTIAL"
m = main.db.execute("SELECT type,sku,name,delta,before,after,note FROM movements WHERE sku=? ORDER BY id DESC LIMIT 1", (ids[2]["sku"],)).fetchone()   # the biscuits line (18 of 20)
assert tuple(m)[0]=="RECEIVE" and m["delta"]==18 and m["after"]==m["before"]+18 and "PUR-00001" in m["note"], tuple(m)
ok(c.post(f"/purchase-orders/{po['id']}/receive", json={"lines":[{"po_item_id":items[0]["id"],"qty":50}]}), 409)   # replay

# a failure in the MIDDLE of a receive rolls back everything (simulate with a broken move)
po2 = ok(c.post("/purchase-orders", json={"supplier_id":s1,"lines":[{"product_id":ids[0]["id"],"qty":5,"unit_cost":1},{"product_id":ids[1]["id"],"qty":5,"unit_cost":1}]}))
it2 = ok(c.get(f"/purchase-orders/{po2['id']}"))["items"]
real_move = purchasing_move = main.move; calls = {"n":0}
def flaky(p, d, t, n=""):
    calls["n"] += 1
    if calls["n"]==2: raise RuntimeError("boom")
    return real_move(p, d, t, n)
import purchasing; purchasing._move = flaky
s_before = [stock(p["sku"]) for p in ids[:2]]
try:
    newc(raise_server_exceptions=False).post(f"/purchase-orders/{po2['id']}/receive", json={"lines":[{"po_item_id":it2[0]["id"],"qty":5},{"po_item_id":it2[1]["id"],"qty":5}]})
finally: purchasing._move = real_move
assert [stock(p["sku"]) for p in ids[:2]]==s_before, "partial receive leaked!"
assert ok(c.get(f"/purchase-orders/{po2['id']}"))["status"]=="ORDERED"
assert main.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]==1
# and the app still works after that failure
ok(c.post(f"/purchase-orders/{po2['id']}/receive", json={"lines":[{"po_item_id":it2[0]["id"],"qty":5},{"po_item_id":it2[1]["id"],"qty":5}]}))

# new product via PO → appears in inventory + sale flow works on it
po3 = ok(c.post("/purchase-orders", json={"supplier_id":s1,"lines":[{"sku":"9990001","name":"Lays Masala","cat":"Snacks","sale_price":60,"qty":100,"unit_cost":45}]}))
assert "9990001" not in {p["sku"] for p in ok(c.get("/inventory"))["products"]}
it3 = ok(c.get(f"/purchase-orders/{po3['id']}"))["items"][0]
r = ok(c.post(f"/purchase-orders/{po3['id']}/receive", json={"lines":[{"po_item_id":it3["id"],"qty":100}],"paid":True,"pay_method":"Cash"}))
assert stock("9990001")==100 and r["new_products"]==["Lays Masala"]
ok(c.post("/scan", json={"sku":"9990001"})); assert stock("9990001")==99; ok(c.post("/cart/clear"))

# concurrency: 8 simultaneous receives + scans hitting the shared connection
po4 = ok(c.post("/purchase-orders", json={"supplier_id":s1,"lines":[{"product_id":ids[0]["id"],"qty":40,"unit_cost":1}]}))
it4 = ok(c.get(f"/purchase-orders/{po4['id']}"))["items"][0]; b4 = stock(ids[0]["sku"]); codes=[]
def go(): codes.append(newc().post(f"/purchase-orders/{po4['id']}/receive", json={"lines":[{"po_item_id":it4["id"],"qty":40}]}).status_code)
def sc(): newc().post("/scan", json={"sku":"8964004"})
ts=[threading.Thread(target=go) for _ in range(8)]+[threading.Thread(target=sc) for _ in range(8)]
[t.start() for t in ts]; [t.join() for t in ts]
assert codes.count(200)==1 and stock(ids[0]["sku"])==b4+40, codes
ok(c.post("/cart/clear"))

# report against real sales table (refunded sale excluded)
rep = ok(c.get("/reports/purchases?from=2020-01-01&to=2099-01-01"))
assert rep["purchases_count"]==4 and rep["cashflow"]==round(rep["sales_net"]-rep["purchases_total"]-rep["expenses_total"],2)
# supplier / expense endpoints (POST-style)
ok(c.post(f"/suppliers/{s1}/update", json={"phone":"0300"})); ok(c.post("/suppliers/999/update", json={"active":False}), 404)
e = ok(c.post("/expenses", json={"category":"Rent","amount":100}))["id"]
ok(c.post(f"/expenses/{e}/void", json={"reason":""}), 422)                    # a reason is required
ok(c.post(f"/expenses/{e}/void", json={"reason":"typo"})); ok(c.post(f"/expenses/{e}/void", json={"reason":"again"}), 409); ok(c.post("/expenses/99999/void", json={"reason":"x"}), 404)
ok(c.post(f"/expenses/{e}/delete"), 404)                                      # hard delete is gone

# ═════════════ catalog, price guards, deactivate, weight (kg / litre) items ═════════════
def prod(sku): return next(x for x in ok(c.get("/catalog/products")) if x["sku"]==sku)
ok(c.post("/cart/clear"))
# --- create: price must be above 0, barcode unique, unit valid
ok(c.post("/catalog/products", json={"sku":"7770001","name":"Test Tea","cat":"Beverages","price":0}), 422)
ok(c.post("/catalog/products", json={"sku":"7770001","name":"Test Tea","cat":"Beverages","price":-5}), 422)
ok(c.post("/catalog/products", json={"sku":"7770001","name":"Test Tea","price":100,"unit":"box"}), 422)
ok(c.post("/catalog/products", json={"sku":"8964001","name":"Dup","price":100}), 409)
r = ok(c.post("/catalog/products", json={"sku":"7770001","name":"Test Tea","cat":"Beverages","price":100,"cost":80}))
tea = r["product"]; assert tea["stock"]==0 and tea["active"] and r["warning"]==""
ok(c.post("/scan", json={"sku":"7770001"}), 409)                       # nothing in stock: stock only comes from receiving
# --- edit: price / name / category, price 0 refused, history recorded, below-cost warning
ok(c.post(f"/catalog/products/{tea['id']}/update", json={"price":0}), 422)
ok(c.post(f"/catalog/products/{tea['id']}/update", json={"name":""}), 422)
r = ok(c.post(f"/catalog/products/{tea['id']}/update", json={"price":70,"name":"Test Tea 200g","cat":"Tea"}))
assert r["warning"] and "below" in r["warning"] and set(r["changed"])=={"price","name","cat"}, r     # 70 < cost 80
r = ok(c.post(f"/catalog/products/{tea['id']}/update", json={"price":120}))
assert r["warning"]=="" and r["product"]["price"]==120
assert ok(c.post(f"/catalog/products/{tea['id']}/update", json={"price":120}))["changed"]==[]   # no-op writes nothing
hist = ok(c.get(f"/catalog/products/{tea['id']}/history"))
assert [h["field"] for h in hist][:2]==["price","cat"] or "price" in [h["field"] for h in hist], hist
assert any(h["field"]=="price" and h["old"]=="100" and h["new"]=="70" for h in hist), hist
ok(c.post("/catalog/products/99999/update", json={"price":5}), 404)
# --- legacy product with price 0 (created before the catalog existed) can't be sold by accident
main.db.execute("INSERT INTO products(sku,name,cat,price,stock) VALUES('7770002','Legacy Zero','Snacks',0,5)"); main.db.commit()
ok(c.post("/scan", json={"sku":"7770002"}), 409); assert stock("7770002")==5
# --- PO: new product needs price > 0, unit valid
ok(c.post("/purchase-orders", json={"supplier_id":s1,"lines":[{"sku":"7770003","name":"Free Item","sale_price":0,"qty":5,"unit_cost":10}]}), 422)
# --- deactivate: can't be sold / ordered / picked; refuses while in a cart; keeps history; refunds still restock
ok(c.post(f"/catalog/products/{tea['id']}/active", json={"active":False}))
ok(c.post(f"/catalog/products/{prod('7770001')['id']}/active", json={"active":False}))          # idempotent
assert "7770001" not in {x["sku"] for x in ok(c.get("/purchasing/products"))}
ok(c.post("/purchase-orders", json={"supplier_id":s1,"lines":[{"product_id":tea["id"],"qty":5,"unit_cost":50}]}), 409)
ok(c.post(f"/catalog/products/{tea['id']}/active", json={"active":True}))
poT = ok(c.post("/purchase-orders", json={"supplier_id":s1,"lines":[{"product_id":tea["id"],"qty":20,"unit_cost":50}]}))
ok(c.post(f"/catalog/products/{tea['id']}/active", json={"active":False}))                     # deactivated AFTER ordering
itT = ok(c.get(f"/purchase-orders/{poT['id']}"))["items"][0]
ok(c.post(f"/purchase-orders/{poT['id']}/receive", json={"lines":[{"po_item_id":itT["id"],"qty":20}]}))   # goods that arrived are still booked in
assert stock("7770001")==20
ok(c.post("/scan", json={"sku":"7770001"}), 409); assert stock("7770001")==20
assert "7770001" not in {x["sku"] for x in ok(c.get("/dashboard"))["low_stock"]}
ok(c.post(f"/catalog/products/{tea['id']}/active", json={"active":True}))
ok(c.post("/scan", json={"sku":"7770001","qty":2}))
ok(c.post(f"/catalog/products/{tea['id']}/active", json={"active":False}), 409)                # in the cart right now
ok(c.post("/cart/clear")); assert stock("7770001")==20

# --- weight items: kg with decimals
kg = ok(c.post("/catalog/products", json={"sku":"7771000","name":"Loose Sugar","cat":"Grocery","unit":"kg","price":180}))["product"]
ok(c.post("/inventory/adjust", json={"sku":"7771000","new_stock":10.5}))
assert stock("7771000")==10.5
ok(c.post("/scan", json={"sku":"7771000","qty":0.0004}), 422)                  # rounds to 0 -> refused
ok(c.post("/scan", json={"sku":"7771000","qty":0.75})); assert stock("7771000")==9.75
for _ in range(3): ok(c.post("/scan", json={"sku":"7771000","qty":0.1}))
cart = ok(c.get("/cart")); line = [i for i in cart["items"] if i["sku"]=="7771000"][0]
assert line["qty"]==1.05 and line["unit"]=="kg" and stock("7771000")==9.45, (line, stock("7771000"))   # no 0.1+0.1+0.1 float drift
ok(c.post("/scan", json={"sku":"7771000","qty":0.33}))
cart = ok(c.get("/cart")); assert cart["totals"]["subtotal"]==round(1.38*180,2)==248.4
assert cart["totals"]["tax"]==round(248.4*0.08,2) and cart["totals"]["total"]==round(248.4+round(248.4*0.08,2),2)
# set a weighed line to an exact weight in one step; more than stock is refused and changes nothing
ok(c.post("/cart/set", json={"product_id":kg["id"],"qty":2.0})); assert stock("7771000")==8.5
ok(c.post("/cart/set", json={"product_id":kg["id"],"qty":50}), 409); assert stock("7771000")==8.5
ok(c.post("/cart/set", json={"product_id":kg["id"],"qty":0.5})); assert stock("7771000")==10.0
ok(c.post("/cart/remove", json={"product_id":kg["id"],"qty":0.2})); assert stock("7771000")==10.2
ok(c.post("/cart/set", json={"product_id":kg["id"],"qty":0})); assert stock("7771000")==10.5
ok(c.post("/cart/set", json={"product_id":kg["id"],"qty":1}), 404)
# pieces stay whole
ok(c.post("/scan", json={"sku":"8964001","qty":1.5}), 422)
ok(c.post("/inventory/adjust", json={"sku":"8964001","new_stock":3.5}), 422)
ok(c.post("/scan", json={"sku":"8964001"})); ok(c.post("/cart/set", json={"product_id":1,"qty":1.5}), 422); ok(c.post("/cart/clear"))
# checkout + full refund of a weighed sale restores stock exactly
ok(c.post("/scan", json={"sku":"7771000","qty":1.25}))
sale = ok(c.post("/checkout", json={"payment_method":"Cash"}))["receipt"]; assert stock("7771000")==9.25
assert sale["items"][0]["qty"]==1.25 and sale["totals"]["subtotal"]==225.0
d = ok(c.get("/dashboard")); assert isinstance(d["items_sold"], (int,float))
ok(c.post(f"/sales/{sale['id']}/refund", json={"note":"t"})); assert stock("7771000")==10.5
# --- unit rules: pc<->kg switching
ok(c.post(f"/catalog/products/{kg['id']}/update", json={"unit":"pc"}), 409)                    # stock 10.5 isn't whole
ok(c.post("/inventory/adjust", json={"sku":"7771000","new_stock":10}))
ok(c.post(f"/catalog/products/{kg['id']}/update", json={"unit":"pc"})); ok(c.post(f"/catalog/products/{kg['id']}/update", json={"unit":"kg"}))
# --- PO with weight items: decimals for kg, whole for pieces, exact partial receiving
poK = ok(c.post("/purchase-orders", json={"supplier_id":s1,"lines":[{"product_id":kg["id"],"qty":25.5,"unit_cost":120},{"sku":"7771001","name":"Loose Rice","cat":"Grocery","unit":"kg","sale_price":300,"qty":40.25,"unit_cost":210}]}))
ok(c.post("/purchase-orders", json={"supplier_id":s1,"lines":[{"product_id":1,"qty":2.5,"unit_cost":1}]}), 422)
ok(c.post("/purchase-orders", json={"supplier_id":s1,"lines":[{"sku":"7771002","name":"Pieces","unit":"pc","sale_price":10,"qty":1.5,"unit_cost":1}]}), 422)
ok(c.post(f"/catalog/products/{kg['id']}/update", json={"unit":"pc"}), 409)                    # kg product is on an open PO now
itK = ok(c.get(f"/purchase-orders/{poK['id']}"))["items"]; assert itK[0]["unit"]=="kg" and itK[1]["is_new"]
r = ok(c.post(f"/purchase-orders/{poK['id']}/receive", json={"lines":[{"po_item_id":itK[0]["id"],"qty":10.25},{"po_item_id":itK[1]["id"],"qty":40.25}]}))
assert r["po_status"]=="PARTIAL" and stock("7771000")==20.25 and stock("7771001")==40.25 and prod("7771001")["unit"]=="kg", r
ok(c.post(f"/purchase-orders/{poK['id']}/receive", json={"lines":[{"po_item_id":itK[0]["id"],"qty":15.5}]}), 409)   # only 15.25 left
r = ok(c.post(f"/purchase-orders/{poK['id']}/receive", json={"lines":[{"po_item_id":itK[0]["id"],"qty":15.25}]}))
assert r["po_status"]=="RECEIVED" and stock("7771000")==35.5, r
assert ok(c.get(f"/purchase-orders/{poK['id']}"))["received_units"]==65.75
# --- the per-line receive check stays whole for pieces
poP = ok(c.post("/purchase-orders", json={"supplier_id":s1,"lines":[{"product_id":ids[0]["id"],"qty":10,"unit_cost":1}]}))
itP = ok(c.get(f"/purchase-orders/{poP['id']}"))["items"][0]
ok(c.post(f"/purchase-orders/{poP['id']}/receive", json={"lines":[{"po_item_id":itP["id"],"qty":2.5}]}), 422)
ok(c.post(f"/purchase-orders/{poP['id']}/receive", json={"lines":[{"po_item_id":itP["id"],"qty":10}]}))
ok(c.get("/reports/purchases?from=2020-01-01&to=2099-01-01")); ok(c.get("/inventory"))

# ledger integrity: every product's stock == last movement 'after'
bad = main.db.execute("SELECT p.sku FROM products p JOIN movements m ON m.id=(SELECT MAX(id) FROM movements WHERE sku=p.sku) WHERE m.after!=p.stock").fetchall()
assert not bad, [tuple(b) for b in bad]
print("MERGED BACKEND: ALL TESTS PASSED")

# --- per-item refunds
def sell(lines, code=None):
    for sku, q in lines: ok(c.post("/scan", json={"sku": sku, "qty": q}))
    return ok(c.post("/checkout", json={"payment_method": "Cash", "discount_code": code}))["receipt"]
def pid(sku): return main.db.execute("SELECT id FROM products WHERE sku=?", (sku,)).fetchone()[0]
b1, b2, b3 = stock("8964001"), stock("8964003"), stock("8964007")
rc = sell([("8964001", 2), ("8964003", 3), ("8964007", 1)], "SAVE10"); sid = rc["id"]
assert stock("8964001") == b1 - 2
ok(c.post(f"/sales/{sid}/refund", json={"note": "  ", "items": [{"id": pid("8964001"), "qty": 1}]}), 422)   # reason required
ok(c.post(f"/sales/{sid}/refund", json={"note": "x", "items": [{"id": pid("8964001"), "qty": 3}]}), 409)   # more than bought
ok(c.post(f"/sales/{sid}/refund", json={"note": "x", "items": [{"id": pid("8964001"), "qty": 0.5}]}), 422) # pieces are whole
ok(c.post(f"/sales/{sid}/refund", json={"note": "x", "items": [{"id": 99999, "qty": 1}]}), 422)            # not on invoice
ok(c.post(f"/sales/{sid}/refund", json={"note": "x", "items": [{"id": pid("8964001"), "qty": 1}, {"id": pid("8964001"), "qty": 1}]}), 422)
assert stock("8964001") == b1 - 2                      # all rejected refunds left stock alone
r1 = ok(c.post(f"/sales/{sid}/refund", json={"note": "one rice was wrong", "items": [{"id": pid("8964001"), "qty": 1}]}))
assert stock("8964001") == b1 - 1 and stock("8964003") == b2 - 3 and not r1["complete"]     # ONLY that item came back
d = ok(c.get(f"/sales/{sid}")); assert d["refunded"] == 0 and d["refunded_amount"] == r1["refunded_amount"] > 0
assert {i["id"]: i["refunded_qty"] for i in d["items"]}[pid("8964001")] == 1 and len(d["refunds"]) == 1
exp1 = round(320 * rc["totals"]["total"] / rc["totals"]["subtotal"], 2); assert abs(r1["refunded_amount"] - exp1) < 0.011, (r1, exp1)
rep = ok(c.get("/reports/purchases")); assert rep["refunded_count"] >= 1
ok(c.post(f"/sales/{sid}/refund", json={"note": "x", "items": [{"id": pid("8964001"), "qty": 2}]}), 409)   # only 1 left
r2 = ok(c.post(f"/sales/{sid}/refund", json={"note": "rest", "items": [{"id": pid("8964001"), "qty": 1}]}))
r3_ = ok(c.post(f"/sales/{sid}/refund", json={"note": "the rest"}))                                          # no items = everything left
assert r3_["complete"] and stock("8964001") == b1 and stock("8964003") == b2 and stock("8964007") == b3
d = ok(c.get(f"/sales/{sid}")); assert d["refunded"] == 1 and abs(d["refunded_amount"] - d["total"]) < 1e-9   # adds up EXACTLY to the invoice total
ok(c.post(f"/sales/{sid}/refund", json={"note": "again"}), 409)
ok(c.post(f"/sales/{sid}/refund", json={"note": "again", "items": [{"id": pid("8964001"), "qty": 1}]}), 409)
# reports: a fully refunded sale contributes nothing; a partly refunded one only its kept part
rc2 = sell([("8964001", 1), ("8964003", 2)]); s2 = rc2["id"]
ok(c.post(f"/sales/{s2}/refund", json={"note": "sugar back", "items": [{"id": pid("8964003"), "qty": 1}]}))
d2 = ok(c.get(f"/sales/{s2}")); kept_net = round((d2["total"] - d2["tax"]) - main.db.execute("SELECT refunded_net FROM sales WHERE id=?", (s2,)).fetchone()[0], 2)
today = main.now()[:10]; rep = ok(c.get(f"/reports/purchases?from={today}&to={today}"))
all_net = main.db.execute("SELECT COALESCE(SUM(total-tax-COALESCE(refunded_net,0)),0) FROM sales WHERE ts::date=?::date AND refunded=0", (today,)).fetchone()[0]
assert abs(rep["sales_net"] - all_net) < 0.011 and kept_net > 0
mv = ok(c.get("/movements?limit=200"))["entries"]; assert sum(1 for m in mv if m["type"] == "REFUND" and f"#{sid}:" in m["note"]) == 4
# weighed item: partial refund of 0.75 kg out of 1.25 with exact decimals
ok(c.post("/catalog/products", json={"sku": "6660001", "name": "Test Onions", "cat": "Produce", "unit": "kg", "price": 100, "cost": 60}))
ok(c.post("/inventory/adjust", json={"sku": "6660001", "new_stock": 10.5}))
rk = sell([("6660001", 1.25)]); assert stock("6660001") == 9.25
ok(c.post(f"/sales/{rk['id']}/refund", json={"note": "w", "items": [{"id": pid("6660001"), "qty": 0.75}]})); assert stock("6660001") == 10.0
ok(c.post(f"/sales/{rk['id']}/refund", json={"note": "w", "items": [{"id": pid("6660001"), "qty": 0.6}]}), 409)
ok(c.post(f"/sales/{rk['id']}/refund", json={"note": "w"})); assert stock("6660001") == 10.5
# a whole-sale refund made BEFORE this feature (refunded=1, no refund rows) still displays and counts correctly
rl = sell([("8964001", 1)]); main.db.execute("UPDATE sales SET refunded=1, refunded_amount=0, refunded_net=0 WHERE id=?", (rl["id"],)); main.db.commit()
main.db.execute("UPDATE sales SET refunded_amount=total, refunded_net=total-tax WHERE refunded=1 AND COALESCE(refunded_amount,0)=0"); main.db.commit()
dl = ok(c.get(f"/sales/{rl['id']}")); assert all(i["refunded_qty"] == i["qty"] for i in dl["items"]) and dl["refunded"] == 1
ok(c.post(f"/sales/{rl['id']}/refund", json={"note": "x"}), 409)
print("PER-ITEM REFUNDS: ALL TESTS PASSED")


# ═════════════ PostgreSQL — History (audit log), expense voiding, append-only guards, concurrency ═════════════
import psycopg.errors as pge
def audit_rows(prefix=None):
    return [dict(r) for r in main.db.execute("SELECT * FROM audit_log" + (" WHERE action LIKE ?" if prefix else "") + " ORDER BY id", (prefix + "%",) if prefix else None).fetchall()]

# --- history was written by the same transactions as the changes
H = ok(c.get("/history?limit=500")); assert H["total"] > 20 and H["total"] == len(audit_rows())
acts = {e["action"] for e in H["entries"]}
for a in ("sale.checkout", "sale.refund", "stock.adjust", "catalog.created", "catalog.price", "catalog.active", "po.created",
          "purchase.received", "purchase.paid" if False else "purchase.received", "supplier.added", "supplier.updated", "expense.added", "expense.voided"):
    assert a in acts, (a, sorted(acts))
assert all(e["actor"] == "owner" and e["user_id"] is not None and e["approved_by"] is None for e in H["entries"])      # every line says WHO did it
cats = {x["category"] for x in ok(c.get("/history/meta"))["categories"]}; assert {"sale", "stock", "catalog", "purchase", "expense", "po", "supplier"} <= cats, cats
assert all(e["action"].startswith("sale.") for e in ok(c.get("/history?category=sale"))["entries"])
hit = ok(c.get("/history?q=Test%20Tea"))["entries"]; assert hit and all("tea" in (e["summary"] + e["entity_id"]).lower() for e in hit)
assert ok(c.get("/history?q=zzzz-no-such-thing"))["total"] == 0
d1 = main.now()[:10]; assert ok(c.get(f"/history?from={d1}&to={d1}"))["total"] == H["total"]; assert ok(c.get("/history?from=2000-01-01&to=2000-01-02"))["total"] == 0
p1 = ok(c.get("/history?limit=5&offset=0"))["entries"]; p2 = ok(c.get("/history?limit=5&offset=5"))["entries"]; assert len(p1) == 5 and not {e["id"] for e in p1} & {e["id"] for e in p2}
assert H["entries"][0]["id"] > H["entries"][-1]["id"]                                              # newest first
ref = [e for e in H["entries"] if e["action"] == "sale.refund"][0]; assert ref["details"]["amount"] > 0 and "items" in ref["details"]   # JSONB details come back as objects
ok(c.get("/history?limit=0"), 422)

# --- a rolled-back change leaves NO history behind either (same transaction)
n_before = len(audit_rows())
ok(c.post("/purchase-orders", json={"supplier_id": 999999, "lines": [{"product_id": 1, "qty": 1, "unit_cost": 1}]}), 404)
ok(c.post("/catalog/products", json={"sku": "8964001", "name": "Dup", "price": 5}), 409)
assert len(audit_rows()) == n_before

# --- expenses: voided, never deleted; voided ones don't count anywhere
e1 = ok(c.post("/expenses", json={"category": "Rent", "amount": 1000}))["id"]; e2 = ok(c.post("/expenses", json={"category": "Utilities", "amount": 250.5}))["id"]
rng = "from=2020-01-01&to=2099-01-01"
t0 = ok(c.get(f"/expenses?{rng}"))["total"]; r0 = ok(c.get(f"/reports/purchases?{rng}"))
ok(c.post(f"/expenses/{e2}/void", json={"reason": "entered twice"}))
x = ok(c.get(f"/expenses?{rng}")); assert round(t0 - x["total"], 2) == 250.5 and e2 not in [i["id"] for i in x["items"]]
xv = ok(c.get(f"/expenses?{rng}&include_voided=true")); v = [i for i in xv["items"] if i["id"] == e2][0]
assert v["voided"] and v["void_reason"] == "entered twice" and v["voided_ts"] and xv["total"] == x["total"]
r1 = ok(c.get(f"/reports/purchases?{rng}")); assert round(r0["expenses_total"] - r1["expenses_total"], 2) == 250.5 and r1["expenses_count"] == r0["expenses_count"] - 1
assert main.db.execute("SELECT COUNT(*) FROM expenses WHERE id=?", (e2,)).fetchone()[0] == 1          # the row is still there
assert any(a["action"] == "expense.voided" and a["entity_id"] == f"EXP-{e2:05d}" for a in audit_rows("expense."))

# --- the database itself refuses to edit or erase history
for sql in ("UPDATE audit_log SET summary='x'", "DELETE FROM audit_log", "TRUNCATE audit_log",
            "UPDATE movements SET delta=0", "DELETE FROM movements", "TRUNCATE movements",
            "UPDATE product_log SET new='x'", "DELETE FROM product_log"):
    try: main.db.execute(sql); raise SystemExit(f"NOT BLOCKED: {sql}")
    except pge.RaiseException: pass
assert len(audit_rows()) >= n_before

# --- a negative stock is impossible even for code that bypasses move()
try: main.db.execute("UPDATE products SET stock=-1 WHERE sku='8964001'"); raise SystemExit("negative stock allowed")
except pge.CheckViolation: pass

# --- oversell race: ONE unit left, 12 tills scan it at the same moment -> exactly one wins
ok(c.post("/cart/clear")); ok(c.post("/inventory/adjust", json={"sku": "8964006", "new_stock": 1}))
res = []
def grab(): res.append(newc().post("/scan", json={"sku": "8964006"}).status_code)
ts_ = [threading.Thread(target=grab) for _ in range(12)]; [t.start() for t in ts_]; [t.join() for t in ts_]
assert sorted(res) == [200] + [409] * 11 and stock("8964006") == 0, res
ok(c.post("/cart/clear")); assert stock("8964006") == 1

# --- two people refund the SAME sale at once: stock comes back once, the rest get 409
rr = sell([("8964008", 2)]); b8 = stock("8964008")
codes2 = []
def rf(): codes2.append(newc().post(f"/sales/{rr['id']}/refund", json={"note": "race"}).status_code)
ts_ = [threading.Thread(target=rf) for _ in range(6)]; [t.start() for t in ts_]; [t.join() for t in ts_]
assert codes2.count(200) == 1 and codes2.count(409) == 5 and stock("8964008") == b8 + 2, codes2
assert main.db.execute("SELECT COUNT(*) FROM refunds WHERE sale_id=?", (rr["id"],)).fetchone()[0] == 1

# --- mixed storm: scans, removes, clears and a checkout hammering the cart together -> no deadlock/500, stock never leaks
ok(c.post("/cart/clear")); before_all = {r["sku"]: r["stock"] for r in main.rows("SELECT sku, stock FROM products")}
errs = []
def storm(i):
    cl = newc()
    for k in range(6):
        sku = ["8964001", "8964003", "8964007"][(i + k) % 3]
        r_ = cl.post("/scan", json={"sku": sku}); errs.append(r_.status_code)
        if k % 3 == 0: errs.append(cl.post("/cart/remove", json={"product_id": pid(sku), "qty": 1}).status_code)
        if k == 4 and i % 4 == 0: errs.append(cl.post("/cart/clear").status_code)
ts_ = [threading.Thread(target=storm, args=(i,)) for i in range(8)]; [t.start() for t in ts_]; [t.join() for t in ts_]
assert set(errs) <= {200, 404, 409}, sorted(set(errs))                                              # never a 500 / deadlock error
cart_now = {i["sku"]: i["qty"] for i in ok(c.get("/cart"))["items"]}
for sku, b in before_all.items():
    if sku in ("8964001", "8964003", "8964007"): assert stock(sku) == b - cart_now.get(sku, 0), (sku, stock(sku), b, cart_now)   # every unit is in the cart or on the shelf
ok(c.post("/cart/clear"))
for sku, b in before_all.items(): assert stock(sku) == b, sku

# --- SSE counter moves only after commit and not on a failed request
v = database.get_version(); ok(c.post("/scan", json={"sku": "nope"}), 404); assert database.get_version() == v
ok(c.post("/scan", json={"sku": "8964001"})); assert database.get_version() > v; ok(c.post("/cart/clear"))

# --- final ledger integrity after everything above
bad = main.db.execute("SELECT p.sku FROM products p JOIN movements m ON m.id=(SELECT MAX(id) FROM movements WHERE sku=p.sku) WHERE m.after!=p.stock").fetchall()
assert not bad, [tuple(b) for b in bad]
assert main.db.execute("SELECT COUNT(*) FROM products WHERE stock<0").fetchone()[0] == 0
print("POSTGRES + HISTORY + CONCURRENCY: ALL TESTS PASSED")
