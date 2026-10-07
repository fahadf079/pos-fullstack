"""v12 additions: editable settings, PIN to remove cart items, refund payout method, cash drops/payouts, end-of-day and
profit reports, weighed-item barcodes, sign-in throttle, compulsory owner 2FA, 2FA at unlock, bulk import, barcode generator."""
import os, sys, json
HERE = os.path.dirname(os.path.abspath(__file__)); BACK = os.path.dirname(HERE); sys.path.insert(0, BACK)
TEST_URL = os.environ.get("POS_TEST_DATABASE_URL", "postgresql://pos:pos@localhost:5432/pos_test")
assert TEST_URL.split("?")[0].rstrip("/").endswith("_test")
os.environ["DATABASE_URL"] = TEST_URL; os.environ["POS_SECRET_KEY"] = "test-secret-key"; os.environ["POS_TEST_MODE"] = "1"
os.environ["POS_ALLOW_REMOTE_SETUP"] = "1"; os.environ["POS_THROTTLE_MAX"] = "1000000"
import psycopg
with psycopg.connect(TEST_URL, autocommit=True) as _c:
    _c.execute("DROP SCHEMA public CASCADE"); _c.execute("CREATE SCHEMA public")
from fastapi.testclient import TestClient
import main, auth, totp, settings

def ok(r, code=200): assert r.status_code == code, (r.status_code, r.text); return r.json()
def detail(r): return r.json()["detail"]
def sql(q, *a): return main.db.execute(q, a).fetchall()
def client(): n = TestClient(main.app); n.headers["X-POS"] = "1"; return n
def pin(p): return {"X-POS-PIN": p}
def setting(c, k, v): return ok(c.post("/settings", json={"key": k, "value": v}))
def ean(code12): return code12 + str((10 - sum(int(ch) * (3 if i % 2 else 1) for i, ch in enumerate(code12)) % 10) % 10)

boss = client()
ok(boss.post("/auth/setup", json={"username": "boss", "full_name": "Boss", "password": "Boss-pass-1"}))
ok(boss.post("/users", json={"username": "ana", "full_name": "Ana", "role": "employee", "pin": "4829"}))
ana = client(); ok(ana.post("/auth/pin-login", json={"username": "ana", "pin": "4829"}))

# ── 1. settings: tax, low stock, discount codes, receipt text ──
v = {s["key"]: s["value"] for s in ok(boss.get("/settings"))["settings"]}
assert v["tax_percent"] == 8 and v["low_stock"] == 5 and v["discount_codes"] == {"SAVE10": 10, "SAVE20": 20} and v["shop_name"] == "My Mart"
assert ana.post("/settings", json={"key": "tax_percent", "value": 0}).status_code == 403
for k, bad in [("tax_percent", 51), ("tax_percent", -1), ("tax_percent", True), ("low_stock", 2.5), ("discount_codes", {"x": 10}), ("discount_codes", {"GOOD": 0}),
               ("discount_codes", {"GOOD": 101}), ("discount_codes", "SAVE10=10"), ("shop_name", "x" * 61), ("receipt_footer", 5)]:
    assert boss.post("/settings", json={"key": k, "value": bad}).status_code == 422, (k, bad)
ok(ana.post("/scan", json={"sku": "8964001", "qty": 2}))                                    # 2 × 320 = 640
t = ok(ana.get("/cart"))["totals"]; assert t["subtotal"] == 640 and t["tax"] == 51.2 and t["total"] == 691.2
setting(boss, "tax_percent", 10); t = ok(ana.get("/cart"))["totals"]; assert t["tax"] == 64 and t["total"] == 704
setting(boss, "discount_codes", {"half": 50}); assert ok(ana.get("/cart?discount_code=HALF"))["totals"]["discount"] == 320
assert ok(ana.get("/cart?discount_code=SAVE10"))["totals"]["discount"] == 0                  # the old code is gone
setting(boss, "low_stock", 40); assert any(x["sku"] == "8964001" for x in ok(boss.get("/dashboard"))["low_stock"]) and ok(boss.get("/inventory"))["low_threshold"] == 40
setting(boss, "low_stock", 5); setting(boss, "tax_percent", 8); setting(boss, "discount_codes", {"SAVE10": 10, "SAVE20": 20})
setting(boss, "shop_name", "Test Mart"); setting(boss, "receipt_footer", "Come again")
assert sql("SELECT COUNT(*) FROM audit_log WHERE action='settings.changed'")[0][0] >= 6
ok(ana.post("/cart/clear", json={}))
print("settings ok")

# ── 2. removing from the cart can need the employee's own PIN ──
setting(boss, "remove_needs_pin", True)
ok(ana.post("/scan", json={"sku": "8964002", "qty": 3})); pid = sql("SELECT id FROM products WHERE sku='8964002'")[0][0]; s0 = sql("SELECT stock FROM products WHERE id=?", pid)[0][0]
assert detail(ana.post("/cart/remove", json={"product_id": pid, "qty": 1}))["code"] == "pin_required"
assert detail(ana.post("/cart/remove", json={"product_id": pid, "qty": 1}, headers=pin("1111")))["code"] == "pin_invalid"
ok(ana.post("/cart/remove", json={"product_id": pid, "qty": 1}, headers=pin("4829")))
assert detail(ana.post("/cart/set", json={"product_id": pid, "qty": 1}))["code"] == "pin_required"       # lowering is removing
ok(ana.post("/cart/set", json={"product_id": pid, "qty": 4}))                                            # raising needs no PIN
assert detail(ana.post("/cart/clear", json={}))["code"] == "pin_required"
ok(ana.post("/cart/clear", json={}, headers=pin("4829")))
assert sql("SELECT stock FROM products WHERE id=?", pid)[0][0] == s0 + 3                                  # all returned (3 scanned, 1 removed, 2 more added, cleared: net back)
ok(ok(boss.post("/scan", json={"sku": "8964002", "qty": 1})) and boss.post("/cart/remove", json={"product_id": pid, "qty": 1}))   # owners never need a PIN
setting(boss, "remove_needs_pin", False)
ok(ana.post("/scan", json={"sku": "8964002", "qty": 1})); ok(ana.post("/cart/clear", json={}))           # off again: no PIN
print("cart removal pin ok")

# ── 3. refunds record how the money went back; cash-up follows it ──
ok(ana.post("/shifts/open", json={"opening_float": 1000}))
def cash_sale(sku="8964003", qty=1, pay="Cash"):
    ok(ana.post("/scan", json={"sku": sku, "qty": qty})); return ok(ana.post("/checkout", json={"payment_method": pay}))["receipt"]
r1 = cash_sale(); r2 = cash_sale(); r3 = cash_sale(pay="Card")                                           # each 180 + 8% = 194.4
assert r1["totals"]["total"] == 194.4
assert detail(ana.post(f"/sales/{r1['id']}/refund", json={"note": "bad"}))["code"] == "pin_required"
assert ana.post(f"/sales/{r1['id']}/refund", json={"note": "bad", "paid_via": "Cheque"}, headers=pin("4829")).status_code == 422
ok(ana.post(f"/sales/{r1['id']}/refund", json={"note": "changed mind", "paid_via": "Wallet"}, headers=pin("4829")))     # cash sale, paid back to wallet: drawer unchanged
ok(ana.post(f"/sales/{r2['id']}/refund", json={"note": "damaged"}, headers=pin("4829")))                                # default = how it was paid (Cash): drawer −194.4
ok(ana.post(f"/sales/{r3['id']}/refund", json={"note": "damaged", "paid_via": "Cash"}, headers=pin("4829")))            # card sale refunded in cash: drawer −194.4
assert [x["paid_via"] for x in ok(ana.get(f"/sales/{r1['id']}"))["refunds"]] == ["Wallet"]
print("refund payout ok")

# ── 4. cash drops, payouts and additions ──
assert ana.post("/shifts/move", json={"kind": "drop", "amount": 200, "reason": "to the safe"}).status_code == 403
assert ana.post("/shifts/move", json={"kind": "steal", "amount": 5, "reason": "xxx"}, headers=pin("4829")).status_code == 422
assert ana.post("/shifts/move", json={"kind": "drop", "amount": 0, "reason": "xxx"}, headers=pin("4829")).status_code == 422
assert ana.post("/shifts/move", json={"kind": "drop", "amount": 5, "reason": "x"}, headers=pin("4829")).status_code == 422
m = ok(ana.post("/shifts/move", json={"kind": "drop", "amount": 200, "reason": "to the safe"}, headers=pin("4829"))); assert set(m) == {"ok"}
ok(ana.post("/shifts/move", json={"kind": "payout", "amount": 50, "reason": "milk delivery"}, headers=pin("4829")))
ok(ana.post("/shifts/move", json={"kind": "add", "amount": 100, "reason": "change from bank"}, headers=pin("4829")))
assert ana.get("/shifts/moves").status_code == 403 and ana.get("/reports/day").status_code == 403
assert len(ok(boss.get("/shifts/moves"))["moves"]) == 3
# expected = 1000 float + 194.4 (r1 cash sale) + 194.4 (r2) − 194.4 (r2 refund, cash) − 194.4 (r3 refund, cash) − 200 − 50 + 100 = 850.0
r = ok(ana.post("/shifts/close", json={"counted_cash": 850})); assert r == {"ok": True, "submitted": True}
sh = ok(boss.get("/shifts"))["shifts"][0]; assert sh["expected_cash"] == 850.0 and sh["variance"] == 0 and (sh["drops"], sh["payouts"], sh["added"]) == (200, 50, 100), sh
assert not [a for a in ok(ana.get("/alerts"))["alerts"] if "cash.variance" in a["key"]]
assert ana.post("/shifts/move", json={"kind": "drop", "amount": 5, "reason": "after close"}, headers=pin("4829")).status_code == 409   # no open shift
assert len(sql("SELECT * FROM audit_log WHERE action='shift.move'")) == 3
try: main.db.execute("UPDATE shift_moves SET amount=1"); raise SystemExit("shift_moves must be append-only")
except psycopg.errors.RaiseException: pass
print("cash moves ok")

# ── 5. end-of-day report ──
day = ok(boss.get("/reports/day"))
meth = {m["method"]: m for m in day["methods"]}
assert meth["Cash"]["sales"] == 2 and meth["Cash"]["sales_total"] == 388.8 and meth["Card"]["sales_total"] == 194.4
assert meth["Cash"]["refunds_total"] == 388.8 and meth["Wallet"]["refunds_total"] == 194.4 and meth["Wallet"]["sales"] == 0   # r2 + r3 went back in cash, r1 to wallet
assert day["sales_total"] == 583.2 and day["refunds_total"] == 583.2 and day["net_total"] == 0.0
assert len(day["shifts"]) == 1 and day["expected_total"] == 850.0 and day["counted_total"] == 850.0 and day["moves"]["drop"]["total"] == 200
assert ok(boss.get("/reports/day?date=2001-01-01"))["sales_total"] == 0
for bad in ("yesterday", "2026-13-40", "2026-02-30"): assert boss.get(f"/reports/day?date={bad}").status_code == 422
print("day report ok")

# ── 6. profit report (cost kept per sale, hidden from employees) ──
sql("UPDATE products SET cost=250 WHERE sku='8964001'")                                        # sells at 320
ok(ana.post("/scan", json={"sku": "8964001", "qty": 4}))
s1 = ok(ana.post("/checkout", json={"payment_method": "Card"}))["receipt"]["id"]               # 4 × 320 = 1280, tax 102.4
sql("UPDATE products SET cost=999 WHERE sku='8964001'")                                         # later cost change must NOT alter this sale's profit
assert "cost" not in json.dumps(ok(ana.get(f"/sales/{s1}"))["items"]) and "cost" in json.dumps(ok(boss.get(f"/sales/{s1}"))["items"])
assert "cost" not in json.dumps(ok(ana.get("/cart"))) and "cost" not in json.dumps(ok(ana.post("/scan", json={"sku": "8964003"})))
ok(ana.post("/cart/clear", json={}))
p = ok(boss.get("/reports/profit?date_from=2020-01-01&date_to=2099-01-01"))
# sales so far: s1 (net 1280) + three 180-sales r1,r2,r3 (all fully refunded → net 0). Their cost: product 8964003 cost 0 → "without cost"
assert p["revenue_net"] == 1280.0 and p["cost_of_goods"] == 1000.0 and p["gross_profit"] == 280.0 and p["margin_percent"] == 21.9, p
assert p["lines_without_cost"] == 3 and p["estimated_cost_lines"] == 0
ok(ana.post(f"/sales/{s1}/refund", json={"note": "one back", "items": [{"id": sql("SELECT id FROM products WHERE sku='8964001'")[0][0], "qty": 1}]}, headers=pin("4829")))
p = ok(boss.get("/reports/profit?date_from=2020-01-01&date_to=2099-01-01"))
assert p["revenue_net"] == 960.0 and p["cost_of_goods"] == 750.0 and p["gross_profit"] == 210.0, p            # 3 units left: 960 − 3×250
ok(boss.post("/expenses", json={"category": "Rent", "amount": 100, "payee": "x", "note": ""}))
assert ok(boss.get("/reports/profit?date_from=2020-01-01&date_to=2099-01-01"))["profit_after_expenses"] == 110.0
# an old sale with no stored cost uses today's cost and says so
sql("UPDATE sales SET items=(SELECT jsonb_agg(i - 'cost') FROM jsonb_array_elements(items) i) WHERE id=?", s1)
p = ok(boss.get("/reports/profit?date_from=2020-01-01&date_to=2099-01-01")); assert p["estimated_cost_lines"] == 1 and p["cost_of_goods"] == 3 * 999.0, p
assert p["stock_value_at_cost"] > 0 and p["stock_value_at_price"] > 0 and p["best_items"]
assert boss.get("/reports/profit?date_from=2026-05-01&date_to=2026-04-01").status_code == 422 and ana.get("/reports/profit").status_code == 403
print("profit ok")

# ── 7. weighed-item barcodes ──
ok(boss.post("/catalog/products", json={"sku": "12345", "name": "Apples", "unit": "kg", "price": 200, "cost": 120}))
ok(boss.post("/inventory/adjust", json={"sku": "12345", "new_stock": 20, "note": "count"}))
label = ean("20" + "12345" + "01500")
assert ana.post("/scan", json={"sku": label}).status_code == 404                                # switch is off: it is just an unknown barcode
setting(boss, "scale_barcodes", True)
ok(ana.post("/scan", json={"sku": label, "qty": 99}))                                            # the typed qty is ignored: the label weighs 1.500 kg
assert ok(ana.get("/cart"))["items"][0]["qty"] == 1.5 and sql("SELECT stock FROM products WHERE sku='12345'")[0][0] == 18.5
ok(ana.post("/scan", json={"sku": label}))
assert ok(ana.get("/cart"))["items"][0]["qty"] == 3.0
assert ana.post("/scan", json={"sku": label[:-1] + str((int(label[-1]) + 1) % 10)}).status_code == 422          # damaged label
assert ana.post("/scan", json={"sku": ean("20" + "12345" + "00000")}).status_code == 422                          # no weight
assert ana.post("/scan", json={"sku": ean("20" + "77777" + "00500")}).status_code == 404                          # unknown item code
ok(boss.post("/catalog/products", json={"sku": "22222", "name": "Eggs tray", "unit": "pc", "price": 300}))
assert ana.post("/scan", json={"sku": ean("20" + "22222" + "00500")}).status_code == 422                          # a piece item can't take a weight
ok(boss.post("/catalog/products", json={"sku": "00777", "name": "Onions", "unit": "kg", "price": 100})); ok(boss.post("/inventory/adjust", json={"sku": "00777", "new_stock": 9, "note": "c"}))
ok(ana.post("/scan", json={"sku": ean("20" + "00777" + "00250")}))                                              # leading zeros in the item code
assert any(i["name"] == "Onions" and i["qty"] == 0.25 for i in ok(ana.get("/cart"))["items"])
ok(ana.post("/cart/clear", json={})); ok(ana.post("/scan", json={"sku": "8964001"})); ok(ana.post("/cart/clear", json={}))      # ordinary barcodes unaffected
assert sql("SELECT stock FROM products WHERE sku='12345'")[0][0] == 20
setting(boss, "scale_barcodes", False)
print("scale barcodes ok")

# ── 8. barcode generator + bulk import ──
b1 = ok(boss.get("/catalog/next-barcode"))["sku"]; assert len(b1) == 13 and b1.startswith("999") and b1 == ean(b1[:12]) and not b1.startswith("2")
ok(boss.post("/catalog/products", json={"sku": b1, "name": "House brand tea", "price": 500}))
b2 = ok(boss.get("/catalog/next-barcode"))["sku"]; assert b2 != b1 and b2 == ean(b2[:12])
assert ana.get("/catalog/next-barcode").status_code == 403 and ana.post("/catalog/import", json={"rows": []}).status_code == 403
rows = [{"sku": "9001", "name": "Pens", "cat": "Stationery", "unit": "pc", "price": 30, "cost": 18}, {"sku": "9002", "name": "Loose sugar", "unit": "kg", "price": 150}]
dry = ok(boss.post("/catalog/import", json={"rows": rows})); assert dry["ok"] and dry["would_add"] == 2 and dry["saved"] == 0
assert not sql("SELECT 1 FROM products WHERE sku='9001'")                                        # dry run saves nothing
bad = ok(boss.post("/catalog/import", json={"rows": rows + [{"sku": "9001", "name": "dup", "price": 1}, {"sku": "8964001", "name": "exists", "price": 1},
         {"sku": "9003", "name": "x", "unit": "box", "price": 1}, {"sku": "9004", "name": "free", "price": 0}, {"sku": "", "name": "nobarcode", "price": 1}], "dry_run": False}))
assert not bad["ok"] and bad["error_count"] == 5 and bad["saved"] == 0 and {e["row"] for e in bad["errors"]} == {3, 4, 5, 6, 7}
assert not sql("SELECT 1 FROM products WHERE sku='9001'")                                        # all or nothing
done = ok(boss.post("/catalog/import", json={"rows": rows, "dry_run": False})); assert done["saved"] == 2
pr = sql("SELECT cat, unit, stock, cost FROM products WHERE sku='9002'")[0]; assert (pr[0], pr[1], pr[2]) == ("Grocery", "kg", 0)
assert sql("SELECT COUNT(*) FROM audit_log WHERE action='catalog.import'")[0][0] == 1 and sql("SELECT COUNT(*) FROM product_log WHERE sku IN ('9001','9002')")[0][0] == 2
assert boss.post("/catalog/import", json={"rows": []}).status_code == 422
print("catalog import + barcodes ok")

# ── 9. sign-in throttle ──
auth.THROTTLE["max"] = 4; auth._fail_log.clear()
nobody = client()
for _ in range(3): assert nobody.post("/auth/pin-login", json={"username": "ana", "pin": "0000"}).status_code == 401
assert nobody.post("/auth/pin-login", json={"username": "ana", "pin": "0000"}).status_code == 401        # 4th failure uses up the allowance (this one also locks the PIN at 5 total? no: 4 so far)
r = nobody.post("/auth/pin-login", json={"username": "ana", "pin": "4829"}); assert r.status_code == 429 and "Wait" in r.json()["detail"]           # even the RIGHT pin waits
assert nobody.post("/auth/login", json={"username": "boss", "password": "Boss-pass-1"}).status_code == 429                                         # owner form too
assert nobody.get("/auth/status").status_code == 200 and ana.get("/cart").status_code == 200                                                         # nothing else is throttled
auth._fail_log.clear(); auth.THROTTLE["max"] = 4
ok(nobody.post("/auth/pin-login", json={"username": "ana", "pin": "4829"}))
for _ in range(3): nobody.post("/auth/login", json={"username": "boss", "password": "nope-nope-1"})
assert len(sql("SELECT 1 FROM users WHERE failed_logins=3 AND username='boss'")) == 1
r = nobody.post("/auth/login", json={"username": "ghost", "password": "nope-nope-1"}); assert r.status_code == 401
assert nobody.post("/auth/login", json={"username": "boss", "password": "Boss-pass-1"}).status_code == 429
auth._fail_log.clear(); auth.THROTTLE["max"] = 1000000
sql("UPDATE users SET pin_failed=0, pin_locked_until=NULL, failed_logins=0, locked_until=NULL")
print("throttle ok")

# ── 10. 2FA: compulsory for owners, and at unlock ──
boss2 = client(); ok(boss.post("/users", json={"username": "zed", "full_name": "Zed", "role": "owner", "password": "Zed-pass-123"}))
zed = client(); ok(zed.post("/auth/login", json={"username": "zed", "password": "Zed-pass-123"}))
assert ok(zed.get("/settings"))                                                                       # off by default
setting(boss, "force_owner_2fa", True)
assert ok(client().get("/auth/status"))["force_2fa"] is True
r = zed.get("/settings"); assert r.status_code == 403 and detail(r)["code"] == "2fa_required"
assert zed.get("/cart").status_code == 403 and zed.post("/scan", json={"sku": "8964001"}).status_code == 403
assert ana.get("/cart").status_code == 200                                                            # employees are not affected
sec = ok(zed.post("/auth/2fa/begin", json={}))["secret"]                                              # …but the way out is open
ok(zed.post("/auth/2fa/confirm", json={"code": totp._code(sec, totp.current_step())}))
assert zed.get("/settings").status_code == 200 and zed.get("/cart").status_code == 200                # 2FA on → full access again
assert boss.get("/settings").status_code == 403                                                       # boss has none yet
bsec = ok(boss.post("/auth/2fa/begin", json={}))["secret"]; bstep = totp.current_step()
ok(boss.post("/auth/2fa/confirm", json={"code": totp._code(bsec, bstep)}))
assert boss.get("/settings").status_code == 200
setting(boss, "force_owner_2fa", False)
# unlock needs the code only when switched on
setting(boss, "unlock_needs_code", True)
ok(boss.post("/auth/lock", json={}))
assert boss.post("/auth/unlock", json={"password": "wrong-wrong"}).status_code == 403
r = boss.post("/auth/unlock", json={"password": "Boss-pass-1"}); assert r.status_code == 401 and detail(r)["code"] == "code_required"
assert boss.post("/auth/unlock", json={"password": "Boss-pass-1", "code": "000000"}).status_code == 403
assert boss.get("/cart").status_code == 423                                                           # still locked
ok(boss.post("/auth/unlock", json={"password": "Boss-pass-1", "code": totp._code(bsec, bstep + 1)}))
assert boss.get("/cart").status_code == 200
ok(boss.post("/auth/lock", json={})); setting_off = None
sql("UPDATE settings SET value='false'::jsonb WHERE key='unlock_needs_code'"); settings._cache["data"] = None
ok(boss.post("/auth/unlock", json={"password": "Boss-pass-1"}))                                       # back to password-only
print("2fa policy ok")

# ── 11. cookie flag ──
os.environ["POS_COOKIE_SECURE"] = "1"
c = client(); r = c.post("/auth/login", json={"username": "zed", "password": "Zed-pass-123", "code": totp._code(sec, totp.current_step() + 1)})
assert r.status_code == 200 and "secure" in r.headers["set-cookie"].lower(); del os.environ["POS_COOKIE_SECURE"]
# ── 12. adapter labels (Ethernet / Wi-Fi) and the /shop route ──
import netpolicy
li = netpolicy.parse_linux_adapters("2: eth0    inet 192.168.1.20/24 brd x\n3: wlan0    inet 192.168.1.31/24 brd x\n1: lo    inet 127.0.0.1/8 scope host lo\n", {"wlan0"})
assert li["192.168.1.20"]["type"] == "Ethernet" and li["192.168.1.31"]["type"] == "Wi-Fi" and li["127.0.0.1"]["type"] == "This computer"
wi = netpolicy.parse_windows_adapters('[{"ip":"10.0.0.2","name":"Ethernet","media":"802.3"},{"ip":"10.0.0.3","name":"Wi-Fi","media":"Native 802.11"}]')
assert wi["10.0.0.2"]["type"] == "Ethernet" and wi["10.0.0.3"]["type"] == "Wi-Fi" and netpolicy.parse_windows_adapters("") == {}
assert all({"type", "name"} <= set(d) for d in ok(boss.get("/network"))["detected"])
sh = ok(ana.get("/shop")); assert sh["shop_name"] == "Test Mart" and sh["receipt_footer"] == "Come again" and sh["tax_percent"] == 8
assert client().get("/shop").status_code == 401
print("adapters + shop ok")

print("V12: ALL TESTS PASSED")
