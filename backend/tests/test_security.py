import os, sys, threading, re
HERE = os.path.dirname(os.path.abspath(__file__)); BACK = os.path.dirname(HERE); sys.path.insert(0, BACK)
# THROW-AWAY database only (name must end in _test). Wiped on every run.
TEST_URL = os.environ.get("POS_TEST_DATABASE_URL", "postgresql://pos:pos@localhost:5432/pos_test")
assert TEST_URL.split("?")[0].rstrip("/").endswith("_test"), "POS_TEST_DATABASE_URL must point at a database whose name ends in _test"
os.environ["DATABASE_URL"] = TEST_URL
os.environ["POS_SECRET_KEY"] = "test-secret-key"
os.environ.pop("POS_ALLOW_REMOTE_SETUP", None)
import psycopg
with psycopg.connect(TEST_URL, autocommit=True) as _c:
    _c.execute("DROP SCHEMA public CASCADE"); _c.execute("CREATE SCHEMA public")
from fastapi.testclient import TestClient
import main, database, auth

def ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.text); return r.json()
def detail(r): return r.json()["detail"]
def sql(q, *a): return main.db.execute(q, a).fetchall()
def stock(sku): return sql("SELECT stock FROM products WHERE sku=?", sku)[0][0]
def client(): 
    n = TestClient(main.app); n.headers["X-POS"] = "1"; return n
def login(user, pw, pin=None):
    n = client(); ok(n.post("/auth/login", json={"username": user, "password": pw}))
    if pin: n.headers["X-POS-PIN"] = pin
    return n

anon = client()

# ───────────────────────── first-time setup ─────────────────────────
assert ok(anon.get("/auth/status")) == {"needs_setup": True, "user": None}
r = anon.post("/auth/setup", json={"username": "boss", "password": "Boss-pass-1", "pin": "4829"}); assert r.status_code == 403, r.text   # not from this computer
os.environ["POS_ALLOW_REMOTE_SETUP"] = "1"
ok(anon.post("/auth/setup", json={"username": "bo", "password": "Boss-pass-1", "pin": "4829"}), 422)          # username too short
ok(anon.post("/auth/setup", json={"username": "boss", "password": "short", "pin": "4829"}), 422)
ok(anon.post("/auth/setup", json={"username": "boss", "password": "password", "pin": "4829"}), 422)           # too common
ok(anon.post("/auth/setup", json={"username": "boss", "password": "Boss-pass-1", "pin": "1234"}), 422)        # easy PIN
ok(anon.post("/auth/setup", json={"username": "boss", "password": "Boss-pass-1", "pin": "1111"}), 422)
ok(anon.post("/auth/setup", json={"username": "boss", "password": "Boss-pass-1", "pin": "12"}), 422)
ok(anon.post("/auth/setup", json={"username": "boss", "password": "Boss-pass-1", "pin": "abcd"}), 422)
assert sql("SELECT COUNT(*) FROM users")[0][0] == 0
me = ok(anon.post("/auth/setup", json={"username": "boss", "full_name": "The Boss", "password": "Boss-pass-1", "pin": "4829"}))["user"]
assert me["role"] == "owner" and not me["must_change"]
ok(client().post("/auth/setup", json={"username": "evil", "password": "Evil-pass-12", "pin": "4829"}), 409)    # only ONE setup, ever
assert ok(client().get("/auth/status"))["needs_setup"] is False
owner = anon; owner.headers["X-POS-PIN"] = "4829"

# ───────────────────────── nothing works without a login ─────────────────────────
def concrete(path): return re.sub(r"\{[^}]+\}", "1", path)
nolog = client()
checked = 0
for key in auth.POLICY:
    method, path = key.split(" ", 1)
    r = nolog.get(concrete(path)) if method == "GET" else nolog.post(concrete(path), json={})
    assert r.status_code == 401, (key, r.status_code, r.text); checked += 1
assert checked >= 50, checked
assert nolog.get("/docs").status_code == 404 and nolog.get("/openapi.json").status_code == 404                  # API map is not published
# every route in the app has a rule (the startup check), and an unlisted route would stop the app from starting
from fastapi import FastAPI
fake = FastAPI(); 
@fake.get("/sneaky")
def _s(): return 1
try: auth.install(fake); raise SystemExit("install() accepted a route with no rule")
except RuntimeError as e: assert "/sneaky" in str(e)
# POSTs must carry the X-POS header (cross-site forms can't)
nohdr = TestClient(main.app); nohdr.cookies.update(owner.cookies)
assert nohdr.post("/cart/clear", json={}).status_code == 403
assert nohdr.get("/inventory").status_code == 200

# ───────────────────────── people: create manager + cashiers ─────────────────────────
def add(u, role, pw, pin, name=""):
    return ok(owner.post("/users", json={"username": u, "full_name": name, "role": role, "password": pw, "pin": pin}))["id"]
no_pin = client(); no_pin.cookies.update(owner.cookies)
assert detail(no_pin.post("/users", json={"username": "x1x", "role": "cashier", "password": "Some-pass-12", "pin": "2864"}))["code"] == "pin_required"   # even the owner needs a PIN
mgr_id = add("maria", "manager", "Maria-temp-1", "7351", "Maria")
cas_id = add("sara", "cashier", "Sara-temp-12", "2864", "Sara")
cas2_id = add("zoya", "cashier", "Zoya-temp-12", "3917", "Zoya")
ok(owner.post("/users", json={"username": "MARIA", "role": "cashier", "password": "Other-pass-12", "pin": "2864"}), 409)   # names are case-insensitive unique
ok(owner.post("/users", json={"username": "kim", "role": "boss", "password": "Other-pass-12", "pin": "2864"}), 422)
# the temporary password must be replaced before anything else works
t = login("sara", "Sara-temp-12")
assert detail(t.get("/inventory"))["code"] == "must_change_password"
ok(t.post("/auth/password", json={"current_password": "Sara-temp-12", "new_password": "Sara-temp-12"}), 422)    # must differ
ok(t.post("/auth/password", json={"current_password": "wrong-wrong", "new_password": "Sara-new-pass-1"}), 403)
ok(t.post("/auth/password", json={"current_password": "Sara-temp-12", "new_password": "Sara-new-pass-1"}))
ok(t.get("/inventory"))
for u, old, new in [("maria", "Maria-temp-1", "Maria-new-pass-1"), ("zoya", "Zoya-temp-12", "Zoya-new-pass-1")]:
    n = login(u, old); ok(n.post("/auth/password", json={"current_password": old, "new_password": new}))
manager = login("maria", "Maria-new-pass-1", "7351"); cashier = login("sara", "Sara-new-pass-1", "2864"); cashier2 = login("zoya", "Zoya-new-pass-1", "3917")

# ───────────────────────── role walls, route by route ─────────────────────────
for key, (role, pin, _) in auth.POLICY.items():
    method, path = key.split(" ", 1)
    if path in ("/events",) or path.startswith("/auth/"): continue
    call = lambda cl: cl.get(concrete(path)) if method == "GET" else cl.post(concrete(path), json={})
    if role in ("manager", "owner"):
        r = call(cashier); assert r.status_code == 403 and isinstance(r.json()["detail"], str) and "cashier" in r.json()["detail"], (key, r.status_code, r.text)
    if role == "owner":
        r = call(manager); assert r.status_code == 403 and "manager" in r.json()["detail"], (key, r.status_code, r.text)
for p in ("/history", "/reports/purchases", "/catalog/products", "/suppliers", "/purchase-orders", "/expenses", "/users", "/security/policy"):
    assert cashier.get(p).status_code == 403, p
for p in ("/history", "/reports/purchases", "/catalog/products", "/suppliers", "/purchase-orders", "/expenses"):
    ok(manager.get(p))
assert manager.get("/users").status_code == 403 and manager.get("/security/policy").status_code == 403
ok(owner.get("/users")); pol = ok(owner.get("/security/policy")); assert any(x["pin"] == "approval" for x in pol["rules"])

# ───────────────────────── selling as a cashier is recorded against the cashier ─────────────────────────
base = stock("8964001")
ok(cashier.post("/scan", json={"sku": "8964001", "qty": 2}))
m = sql("SELECT actor, user_id FROM movements ORDER BY id DESC LIMIT 1")[0]; assert (m[0], m[1]) == ("sara", cas_id), tuple(m)
sale = ok(cashier.post("/checkout", json={"payment_method": "Cash"}))["receipt"]["id"]
s = sql("SELECT actor, user_id FROM sales WHERE id=?", sale)[0]; assert (s[0], s[1]) == ("sara", cas_id)
h = ok(manager.get("/history?limit=5"))["entries"]; e = [x for x in h if x["action"] == "sale.checkout"][0]
assert e["actor"] == "sara" and e["user_id"] == cas_id and e["approved_by"] is None
assert ok(manager.get(f"/sales/{sale}"))["actor"] == "sara"

# ───────────────────────── approval: a cashier's refund needs a manager/owner PIN ─────────────────────────
r = cashier.post(f"/sales/{sale}/refund", json={"note": "customer return"}); d = detail(r)
assert r.status_code == 403 and d["code"] == "pin_required" and d["override"] is True, d
assert stock("8964001") == base - 2                                                       # nothing happened
def appr(cl, who, pin, path, body):
    return cl.post(path, json=body, headers={"X-POS-Approver": who, "X-POS-PIN": pin})
r = appr(cashier, "maria", "9999", f"/sales/{sale}/refund", {"note": "x"}); assert r.status_code == 403 and detail(r)["code"] == "pin_invalid" and "4 tries left" in detail(r)["message"], r.text
r = appr(cashier, "nobody", "7351", f"/sales/{sale}/refund", {"note": "x"}); assert detail(r)["code"] == "pin_invalid" and detail(r)["message"] == "Wrong username or PIN."
r = appr(cashier, "zoya", "3917", f"/sales/{sale}/refund", {"note": "x"}); assert detail(r)["code"] == "pin_invalid"      # another CASHIER can't approve
r = appr(cashier, "sara", "2864", f"/sales/{sale}/refund", {"note": "x"}); assert detail(r)["code"] == "pin_invalid"      # nor can you approve yourself
r = appr(cashier, "maria", "7351", f"/sales/{sale}/refund", {"note": "customer return"}); assert r.status_code == 200, r.text
assert stock("8964001") == base
h = [x for x in ok(manager.get("/history?limit=5"))["entries"] if x["action"] == "sale.refund"][0]
assert h["actor"] == "sara" and h["approved_by"] == "maria", h                         # BOTH people are on the record
assert sql("SELECT actor FROM movements WHERE type='REFUND' ORDER BY id DESC LIMIT 1")[0][0] == "sara"
assert sql("SELECT pin_failed FROM users WHERE username='maria'")[0][0] == 0           # a correct PIN resets the counter
# a manager refunds with her OWN PIN (no approver needed); without it she's asked for it
ok(cashier.post("/scan", json={"sku": "8964002", "qty": 1})); s2 = ok(cashier.post("/checkout", json={"payment_method": "Card"}))["receipt"]["id"]
nopin = client(); nopin.cookies.update(manager.cookies)
r = nopin.post(f"/sales/{s2}/refund", json={"note": "x"}); assert detail(r)["code"] == "pin_required" and detail(r)["override"] is False
r = manager.post(f"/sales/{s2}/refund", json={"note": "damaged"}, headers={"X-POS-PIN": "0000"}); assert detail(r)["code"] == "pin_invalid" and "4 tries left" in detail(r)["message"]
ok(manager.post(f"/sales/{s2}/refund", json={"note": "damaged"}))
h = [x for x in ok(manager.get("/history?limit=5"))["entries"] if x["action"] == "sale.refund"][0]; assert h["actor"] == "maria" and h["approved_by"] is None

# ───────────────────────── discounts and stock counts also need approval ─────────────────────────
ok(cashier.post("/scan", json={"sku": "8964003", "qty": 1}))
r = cashier.post("/checkout", json={"payment_method": "Cash", "discount_code": "SAVE10"}); assert detail(r)["code"] == "pin_required" and detail(r)["override"]
assert ok(cashier.get("/cart"))["items"]                                                # the sale is still open, nothing was recorded
ok(cashier.post("/checkout", json={"payment_method": "Cash", "discount_code": "NOT-A-CODE"}))                      # a code that gives no discount needs no PIN
ok(cashier.post("/scan", json={"sku": "8964003", "qty": 1}))
r = appr(cashier, "maria", "7351", "/checkout", {"payment_method": "Cash", "discount_code": "SAVE10"}); assert r.status_code == 200, r.text
h = [x for x in ok(manager.get("/history?limit=3"))["entries"] if x["action"] == "sale.checkout"][0]; assert h["actor"] == "sara" and h["approved_by"] == "maria"
r = cashier.post("/inventory/adjust", json={"sku": "8964001", "new_stock": 5}); assert detail(r)["code"] == "pin_required" and detail(r)["override"]
b0 = stock("8964001"); r = appr(cashier, "maria", "7351", "/inventory/adjust", {"sku": "8964001", "new_stock": b0 + 1}); assert r.status_code == 200
ok(appr(cashier, "maria", "7351", "/inventory/adjust", {"sku": "8964001", "new_stock": b0}))

# ───────────────────────── manager-only actions each ask for the PIN; a failed PIN changes nothing ─────────────────────────
pid = ok(manager.get("/catalog/products"))[0]["id"]; prod = ok(manager.get("/catalog/products"))[0]
def mgr_nopin(): n = client(); n.cookies.update(manager.cookies); return n
mn = mgr_nopin()
ok(mn.post(f"/catalog/products/{pid}/update", json={"name": prod["name"] + " X"}))                                  # a rename needs no PIN
r = mn.post(f"/catalog/products/{pid}/update", json={"price": prod["price"] + 10}); assert detail(r)["code"] == "pin_required"
assert sql("SELECT price FROM products WHERE id=?", pid)[0][0] == prod["price"]
r = manager.post(f"/catalog/products/{pid}/update", json={"price": prod["price"] + 10}, headers={"X-POS-PIN": "5555"}); assert detail(r)["code"] == "pin_invalid"
assert sql("SELECT price FROM products WHERE id=?", pid)[0][0] == prod["price"]
ok(manager.post(f"/catalog/products/{pid}/update", json={"price": prod["price"] + 10}))
ok(manager.post(f"/catalog/products/{pid}/update", json={"price": prod["price"], "name": prod["name"]}))
sid = ok(manager.post("/suppliers", json={"name": "Acme Wholesale"}))["id"]
items = [{"product_id": pid, "qty": 10, "unit_cost": 5}]
po = ok(manager.post("/purchase-orders", json={"supplier_id": sid, "lines": items}))
poi = ok(manager.get(f"/purchase-orders/{po['id']}"))["items"][0]["id"]; s0 = stock(prod["sku"])
r = mn.post(f"/purchase-orders/{po['id']}/receive", json={"lines": [{"po_item_id": poi, "qty": 10}]}); assert detail(r)["code"] == "pin_required"
assert stock(prod["sku"]) == s0
ok(manager.post(f"/purchase-orders/{po['id']}/receive", json={"lines": [{"po_item_id": poi, "qty": 10}]})); assert stock(prod["sku"]) == s0 + 10
rid = ok(manager.get("/purchases"))[0]["id"]
assert detail(mn.post(f"/purchases/{rid}/pay", json={"pay_method": "Cash"}))["code"] == "pin_required"
ok(manager.post(f"/purchases/{rid}/pay", json={"pay_method": "Cash"}))
po2 = ok(manager.post("/purchase-orders", json={"supplier_id": sid, "lines": items}))["id"]
assert detail(mn.post(f"/purchase-orders/{po2}/cancel"))["code"] == "pin_required"; ok(manager.post(f"/purchase-orders/{po2}/cancel"))
eid = ok(manager.post("/expenses", json={"category": "Rent", "amount": 100}))["id"]
r = mn.post(f"/expenses/{eid}/void", json={"reason": "typo"}); assert detail(r)["code"] == "pin_required"
assert sql("SELECT voided FROM expenses WHERE id=?", eid)[0][0] == 0
ok(manager.post(f"/expenses/{eid}/void", json={"reason": "typo"}))
ev = [x for x in ok(manager.get("/history?limit=30"))["entries"] if x["action"] in ("expense.voided", "purchase.received")]
assert ev and all(x["actor"] == "maria" for x in ev)

# ───────────────────────── PIN lockout, also under parallel guessing ─────────────────────────
codes = []
def guess():
    codes.append(zoya_c.post(f"/sales/{s2}/refund", json={"note": "x"}, headers={"X-POS-Approver": "zoya_victim", "X-POS-PIN": "0001"}).status_code)
add("victor", "manager", "Victor-temp-12", "8426", "Victor")
ok(login("victor", "Victor-temp-12").post("/auth/password", json={"current_password": "Victor-temp-12", "new_password": "Victor-new-pass-1"}))
zoya_c = cashier2
def guess():
    codes.append(zoya_c.post(f"/sales/{s2}/refund", json={"note": "x"}, headers={"X-POS-Approver": "victor", "X-POS-PIN": "0001"}).status_code)
ts = [threading.Thread(target=guess) for _ in range(14)]; [t.start() for t in ts]; [t.join() for t in ts]
assert codes.count(403) == 4 and codes.count(423) == 10, sorted(codes)                  # exactly 4 wrong tries counted, the 5th locks, the rest are refused
assert sql("SELECT COUNT(*) FROM audit_log WHERE action='auth.pin_failed' AND actor='victor'")[0][0] == 4
assert sql("SELECT COUNT(*) FROM audit_log WHERE action='auth.pin_locked' AND actor='victor'")[0][0] == 1
r = appr(cashier2, "victor", "8426", f"/sales/{s2}/refund", {"note": "x"}); assert r.status_code == 423                     # even the RIGHT pin is refused while locked
vic = ok(owner.get("/users"))["users"]; assert [u for u in vic if u["username"] == "victor"][0]["pin_locked"] is True
vid = [u for u in vic if u["username"] == "victor"][0]["id"]
ok(owner.post(f"/users/{vid}/unlock", json={}))
r = appr(cashier2, "victor", "8426", f"/sales/{s2}/refund", {"note": "x"}); assert r.status_code == 409, r.text            # unlocked: the PIN works (409 = already refunded, i.e. we got past the PIN)

# ───────────────────────── password lockout, generic messages ─────────────────────────
a = client().post("/auth/login", json={"username": "sara", "password": "nope-nope-1"}); b = client().post("/auth/login", json={"username": "ghost", "password": "nope-nope-1"})
assert a.status_code == b.status_code == 401 and a.json() == b.json() == {"detail": "Wrong username or password."}   # doesn't reveal which usernames exist
for _ in range(3): assert client().post("/auth/login", json={"username": "sara", "password": "nope-nope-1"}).status_code == 401   # 4 wrong so far
assert client().post("/auth/login", json={"username": "sara", "password": "nope-nope-1"}).status_code == 401                   # 5th: locks
r = client().post("/auth/login", json={"username": "sara", "password": "Sara-new-pass-1"}); assert r.status_code == 423 and "locked" in r.json()["detail"]   # right password refused while locked
assert sql("SELECT COUNT(*) FROM audit_log WHERE action='auth.locked' AND actor='sara'")[0][0] == 1
assert sql("SELECT COUNT(*) FROM audit_log WHERE action='auth.login_failed' AND actor='unknown'")[0][0] >= 1
ok(owner.post(f"/users/{cas_id}/unlock", json={})); cashier = login("sara", "Sara-new-pass-1", "2864")

# ───────────────────────── sessions ─────────────────────────
tok = cashier.cookies.get("pos_session"); assert tok and len(tok) > 30
assert sql("SELECT COUNT(*) FROM sessions WHERE token_hash=?", tok)[0][0] == 0                                  # the raw token is NOT in the database
assert sql("SELECT COUNT(*) FROM sessions WHERE token_hash=?", auth._sha(tok))[0][0] == 1
ok(cashier.post("/auth/logout", json={})); assert cashier.get("/inventory").status_code == 401
replay = client(); replay.cookies.set("pos_session", tok); assert replay.get("/inventory").status_code == 401        # a logged-out token is dead
cashier = login("sara", "Sara-new-pass-1", "2864")
main.db.execute("UPDATE sessions SET expires_ts = now() - interval '1 minute' WHERE user_id=?", (cas_id,))
assert cashier.get("/inventory").status_code == 401                                                              # expired session
cashier = login("sara", "Sara-new-pass-1", "2864")
c = client(); c.cookies.set("pos_session", "garbage"); assert c.get("/inventory").status_code == 401
# changing your password logs your other devices out
d1, d2 = login("zoya", "Zoya-new-pass-1"), login("zoya", "Zoya-new-pass-1")
ok(d1.post("/auth/password", json={"current_password": "Zoya-new-pass-1", "new_password": "Zoya-newer-pass-2"})); assert d1.get("/inventory").status_code == 200 and d2.get("/inventory").status_code == 401
cashier2 = login("zoya", "Zoya-newer-pass-2", "3917")
# changing your own PIN needs your password
ok(cashier2.post("/auth/pin", json={"current_password": "bad-bad-bad", "new_pin": "6482"}), 403)
ok(cashier2.post("/auth/pin", json={"current_password": "Zoya-newer-pass-2", "new_pin": "1234"}), 422)
ok(cashier2.post("/auth/pin", json={"current_password": "Zoya-newer-pass-2", "new_pin": "6482"}))
cashier2.headers["X-POS-PIN"] = "6482"
assert appr(cashier2, "maria", "7351", "/inventory/adjust", {"sku": "8964001", "new_stock": stock("8964001")}).status_code == 200

# ───────────────────────── managing people: guards ─────────────────────────
ok(owner.post(f"/users/{me['id']}/active", json={"active": False}), 409)                                         # can't deactivate yourself
ok(owner.post(f"/users/{me['id']}/update", json={"role": "manager"}), 409)                                       # can't demote yourself
ok(owner.post(f"/users/{me['id']}/password", json={"new_password": "Another-pass-1"}), 409)
ok(owner.post(f"/users/{mgr_id}/update", json={"role": "owner"}))                                                # maria promoted to second owner
maria_owner = login("maria", "Maria-new-pass-1", "7351"); ok(maria_owner.get("/users"))                          # takes effect immediately
ok(maria_owner.post(f"/users/{me['id']}/active", json={"active": False}))                                        # the second owner may deactivate the first...
assert owner.get("/inventory").status_code == 401                                                                # ...who is logged out at once
ok(maria_owner.post(f"/users/{me['id']}/active", json={"active": True}))
ok(maria_owner.post(f"/users/{me['id']}/update", json={"role": "cashier"}))                                     # ...and demote them
assert login("boss", "Boss-pass-1", "4829").get("/users").status_code == 403                                      # role changes bite at once
ok(maria_owner.post(f"/users/{me['id']}/update", json={"role": "owner"}))
owner = login("boss", "Boss-pass-1", "4829"); ok(owner.post(f"/users/{mgr_id}/update", json={"role": "manager"}))
assert maria_owner.get("/users").status_code == 403                                                              # maria is a manager again
# the LAST owner can never be removed or demoted
ok(owner.post(f"/users/{me['id']}/active", json={"active": False}), 409)
owners = [u for u in ok(owner.get("/users"))["users"] if u["role"] == "owner" and u["active"]]; assert len(owners) == 1
# deactivate someone: logged out immediately, and can't log back in
ok(owner.post(f"/users/{cas2_id}/active", json={"active": False}))
assert cashier2.get("/inventory").status_code == 401
assert client().post("/auth/login", json={"username": "zoya", "password": "Zoya-newer-pass-2"}).json() == {"detail": "Wrong username or password."}
assert appr(cashier, "zoya", "6482", "/inventory/adjust", {"sku": "8964001", "new_stock": 1}).status_code == 403          # a deactivated person can't approve either
ok(owner.post(f"/users/{cas2_id}/active", json={"active": True}))
# owner resets a password: sessions die, must change
ok(owner.post(f"/users/{cas_id}/password", json={"new_password": "123"}), 422)
# the owner may set a PERMANENT password directly (no forced change), for a new person and for a reset
perm = ok(owner.post("/users", json={"username": "noor", "role": "cashier", "password": "Noor-perm-pass-1", "pin": "5731", "must_change": False}))["id"]
n_ = login("noor", "Noor-perm-pass-1"); assert n_.get("/inventory").status_code == 200                             # works straight away, no forced change
ok(owner.post(f"/users/{perm}/password", json={"new_password": "Noor-perm-pass-2", "must_change": False}))
assert n_.get("/inventory").status_code == 401                                                                    # old login dies on reset either way
assert client().post("/auth/login", json={"username": "noor", "password": "Noor-perm-pass-1"}).status_code == 401  # the previous password no longer works
assert login("noor", "Noor-perm-pass-2").get("/inventory").status_code == 200
ok(owner.post(f"/users/{perm}/password", json={"new_password": "Noor-temp-pass-3"}))                              # default: must change
assert detail(login("noor", "Noor-temp-pass-3").get("/inventory"))["code"] == "must_change_password"
pr = [e for e in ok(owner.get("/history?limit=50&category=user"))["entries"] if e["action"] == "user.password_reset" and e["entity_id"] == "noor"]
assert len(pr) == 2 and any("permanent" in e["summary"] for e in pr)
ok(owner.post(f"/users/{cas_id}/password", json={"new_password": "Temp-reset-pass-1"}))
assert cashier.get("/inventory").status_code == 401
t = login("sara", "Temp-reset-pass-1"); assert detail(t.get("/inventory"))["code"] == "must_change_password"
ok(t.post("/auth/password", json={"current_password": "Temp-reset-pass-1", "new_password": "Sara-final-pass-1"})); cashier = login("sara", "Sara-final-pass-1", "2864")
acts = {e["action"] for e in ok(owner.get("/history?limit=500"))["entries"]}
for a in ("user.created", "user.updated", "user.deactivated", "user.activated", "user.password_reset", "user.password_changed", "user.pin_changed", "user.unlocked",
          "auth.login", "auth.logout", "auth.login_failed", "auth.locked", "auth.pin_failed", "auth.pin_locked"): assert a in acts, (a, sorted(acts))
assert sql("SELECT COUNT(*) FROM audit_log WHERE action='user.pin_changed' AND actor='zoya'")[0][0] == 1

# ───────────────────────── who-did-what stays correct when two cashiers work at once ─────────────────────────
before = {u: sql("SELECT COUNT(*) FROM movements WHERE actor=?", u)[0][0] for u in ("sara", "zoya")}
def work(cl, n):
    for _ in range(n): cl.post("/scan", json={"sku": "8964009", "qty": 1})
t1 = threading.Thread(target=work, args=(cashier, 12)); t2 = threading.Thread(target=work, args=(cashier2 := login("zoya", "Zoya-newer-pass-2", "6482"), 12))
t1.start(); t2.start(); t1.join(); t2.join()
after = {u: sql("SELECT COUNT(*) FROM movements WHERE actor=?", u)[0][0] for u in ("sara", "zoya")}
assert after["sara"] - before["sara"] == 12 and after["zoya"] - before["zoya"] == 12, (before, after)               # no mix-ups between people
ok(cashier.post("/cart/clear", json={}))

# ───────────────────────── secrets are not stored in the clear; the PIN secret matters ─────────────────────────
rows = sql("SELECT username, pw_hash, pin_hash FROM users")
assert all(r[1].startswith("scrypt1$") and r[2].startswith("scrypt1$") for r in rows)
assert not any(p in (r[1] + r[2]) for r in rows for p in ("Boss-pass-1", "4829", "7351"))
assert len({r[2] for r in rows}) == len(rows)                                                                     # salted: same PIN never hashes the same
stored = sql("SELECT pin_hash FROM users WHERE username='boss'")[0][0]
assert auth.verify_pin(stored, "4829") and not auth.verify_pin(stored, "4828")
auth._pepper_cache = b"a-different-secret"; assert not auth.verify_pin(stored, "4829")                              # stolen database + wrong key = PIN can't be checked
auth._pepper_cache = b"test-secret-key"; assert auth.verify_pin(stored, "4829")

# ───────────────────────── browser rules (CORS) ─────────────────────────
r = anon.options("/scan", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type,x-pos,x-pos-pin"})
assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "http://localhost:5173" and r.headers["access-control-allow-credentials"] == "true", dict(r.headers)
r = anon.options("/scan", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
assert "access-control-allow-origin" not in r.headers
r = anon.get("/auth/status", headers={"Origin": "https://evil.example"}); assert "access-control-allow-origin" not in r.headers
r = anon.get("/auth/status", headers={"Origin": "http://192.168.1.20:5173"}); assert r.headers["access-control-allow-origin"] == "http://192.168.1.20:5173"       # a till on the shop's network

# ───────────────────────── history protections still hold, and now name the person ─────────────────────────
for q in ("UPDATE audit_log SET actor='x'", "DELETE FROM audit_log", "UPDATE movements SET actor='x'", "TRUNCATE audit_log"):
    try: main.db.execute(q); raise SystemExit("append-only guard failed: " + q)
    except psycopg.errors.RaiseException: pass
assert sql("SELECT COUNT(*) FROM audit_log WHERE actor='system'")[0][0] == 0                                    # nothing anonymous any more
print("SECURITY: ALL TESTS PASSED")
