import os, sys, threading, re, json, time
HERE = os.path.dirname(os.path.abspath(__file__)); BACK = os.path.dirname(HERE); sys.path.insert(0, BACK)
# THROW-AWAY database only (name must end in _test). Wiped on every run.
TEST_URL = os.environ.get("POS_TEST_DATABASE_URL", "postgresql://pos:pos@localhost:5432/pos_test")
assert TEST_URL.split("?")[0].rstrip("/").endswith("_test"), "POS_TEST_DATABASE_URL must point at a database whose name ends in _test"
os.environ["POS_THROTTLE_MAX"] = "1000000"      # the sign-in throttle is tested separately (test_v12.py)
os.environ["DATABASE_URL"] = TEST_URL
os.environ["POS_SECRET_KEY"] = "test-secret-key"; os.environ["POS_TEST_MODE"] = "1"
os.environ.pop("POS_ALLOW_REMOTE_SETUP", None)
import psycopg
with psycopg.connect(TEST_URL, autocommit=True) as _c:
    _c.execute("DROP SCHEMA public CASCADE"); _c.execute("CREATE SCHEMA public")
from fastapi.testclient import TestClient
import main, database, policy, pipeline, totp, hashing, alerts, netpolicy, settings, cardvault, sessions, users, credentials

def ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.text); return r.json()
def detail(r): return r.json()["detail"]
def sql(q, *a): return main.db.execute(q, a).fetchall()
def stock(sku): return sql("SELECT stock FROM products WHERE sku=?", sku)[0][0]
def client(**kw):
    n = TestClient(main.app, **kw); n.headers["X-POS"] = "1"; return n
def owner_login(name, pw, code=None):
    n = client(); ok(n.post("/auth/login", json={"username": name, "password": pw, **({"code": code} if code else {})})); return n
def emp_login(name, pin):
    n = client(); ok(n.post("/auth/pin-login", json={"username": name, "pin": pin})); return n
def audit_rows(action): return sql("SELECT * FROM audit_log WHERE action=? ORDER BY id", action)

anon = client()
# ───────────────────────── first-time setup ─────────────────────────
assert ok(anon.get("/auth/status"))["needs_setup"] is True
assert anon.post("/auth/setup", json={"username": "boss", "password": "Boss-pass-1"}).status_code == 403            # not from this computer
os.environ["POS_ALLOW_REMOTE_SETUP"] = "1"
ok(anon.post("/auth/setup", json={"username": "bo", "password": "Boss-pass-1"}), 422)                              # name too short
ok(anon.post("/auth/setup", json={"username": "guest", "password": "Boss-pass-1"}), 422)                           # reserved name
ok(anon.post("/auth/setup", json={"username": "boss", "password": "short"}), 422)
ok(anon.post("/auth/setup", json={"username": "boss", "password": "password"}), 422)                               # too common
assert sql("SELECT COUNT(*) FROM users")[0][0] == 0
me = ok(anon.post("/auth/setup", json={"username": "boss", "full_name": "The Boss", "password": "Boss-pass-1"}))["user"]
assert me["role"] == "owner" and me["two_factor"] is False
ok(client().post("/auth/setup", json={"username": "evil", "password": "Evil-pass-12"}), 409)                       # only ONE setup, ever
assert sql("SELECT pin_hash FROM users WHERE username='boss'")[0][0] is None                                       # owners have no PIN at all
owner = anon
print("setup ok")

# ───────────────────────── nothing works without a login ─────────────────────────
def concrete(path): return re.sub(r"\{[^}]+\}", "1", path)
nolog = client(); checked = 0
for key in policy.POLICY:
    method, path = key.split(" ", 1)
    r = nolog.get(concrete(path)) if method == "GET" else nolog.post(concrete(path), json={})
    assert r.status_code == 401, (key, r.status_code, r.text); checked += 1
assert checked >= 70, checked
assert nolog.get("/docs").status_code == 404 and nolog.get("/openapi.json").status_code == 404
from fastapi import FastAPI
fake = FastAPI()
@fake.get("/sneaky")
def _s(): return 1
try: policy.install(fake); raise SystemExit("an unlisted route must stop the app")
except RuntimeError as e: assert "/sneaky" in str(e)
assert ok(nolog.post("/auth/activity", json={}) if False else nolog.get("/auth/people"))["people"] == []
nolog.headers.pop("X-POS"); assert nolog.post("/scan", json={"sku": "8964001"}).status_code in (403, 401)           # CSRF header
print("anonymous refused on", checked, "routes")

# ───────────────────────── people: employees (PIN), owners (password), developer (CLI only) ─────────────────────────
ok(owner.post("/users", json={"username": "ana", "full_name": "Ana Khan", "role": "employee", "pin": "1234"}), 422)  # easy PIN
ok(owner.post("/users", json={"username": "ana", "full_name": "Ana Khan", "role": "employee"}), 422)                 # PIN missing
ok(owner.post("/users", json={"username": "ana", "full_name": "Ana Khan", "role": "employee", "pin": "4829"}))
ok(owner.post("/users", json={"username": "bob", "full_name": "Bob Ali", "role": "employee", "pin": "7391"}))
ok(owner.post("/users", json={"username": "ANA", "role": "employee", "pin": "5820"}), 409)                           # names are unique, any case
ok(owner.post("/users", json={"username": "dev1", "role": "developer", "password": "Dev-pass-123"}), 422)            # a developer cannot be made in the app
ok(owner.post("/users", json={"username": "boss2", "full_name": "Second Owner", "role": "owner", "password": "Boss2-pass-1"}))
assert sql("SELECT pw_hash FROM users WHERE username='ana'")[0][0] is None                                          # an employee has NO password
assert [p["username"] for p in ok(anon.get("/auth/people"))["people"]] == ["ana", "bob"]                              # tiles: employees only, no owners
# the developer exists only through the command-line path (users.create_user directly)
with main.transaction():
    did = users.create_user("dev1", "Developer", "developer", "Dev-pass-123", actor="cli")
    dsecret = totp.new_secret()
    main.db.execute("UPDATE users SET totp_secret=?, totp_enabled=1 WHERE id=?", (hashing.seal(dsecret), did))
assert "dev1" not in [u["username"] for u in ok(owner.get("/users"))["users"]]                                       # never listed
for path in ("update", "password", "pin", "active", "unlock"):
    r = owner.post(f"/users/{did}/{path}", json={"full_name": "x", "new_password": "Whatever-123", "new_pin": "5820", "active": False}); assert r.status_code == 404, (path, r.text)
assert "dev1" not in [p["username"] for p in ok(anon.get("/auth/people"))["people"]]
print("people ok")

# ───────────────────────── employee sign-in: name + PIN ─────────────────────────
r = client().post("/auth/pin-login", json={"username": "ana", "pin": "0000"}); assert r.status_code == 401 and "tries left" in r.json()["detail"], r.text
r = client().post("/auth/pin-login", json={"username": "nobody", "pin": "4829"}); assert r.status_code == 401 and r.json()["detail"] == "Wrong name or PIN."
r = client().post("/auth/pin-login", json={"username": "boss", "pin": "4829"}); assert r.status_code == 401         # an owner cannot sign in with a PIN
r = client().post("/auth/login", json={"username": "ana", "password": "4829"}); assert r.status_code == 401 and r.json()["detail"] == "Wrong name, password or code."
ana = emp_login("ana", "4829"); bob = emp_login("bob", "7391")
assert ok(ana.get("/auth/status"))["user"]["username"] == "ana"
print("employee login ok")

# ───────────────────────── role walls ─────────────────────────
for key, (role, pin, _) in policy.POLICY.items():
    if role in ("owner", "developer"):
        method, path = key.split(" ", 1)
        r = ana.get(concrete(path)) if method == "GET" else ana.post(concrete(path), json={})
        assert r.status_code == 403, (key, r.status_code, r.text)
ok(ana.get("/inventory")); ok(ana.post("/scan", json={"sku": "8964001", "qty": 1})); ok(ana.get("/alerts")); ok(ana.post("/cart/clear"))
print("role walls ok: an employee is refused on every owner/developer route")

# ───────────────────────── an employee's own PIN confirms refund / discount / stock count ─────────────────────────
ok(ana.post("/scan", json={"sku": "8964001", "qty": 2}))
for code_, pin_ in ((403, None), (403, "0000")):                                   # discount: PIN missing, PIN wrong
    r = ana.post("/checkout", json={"payment_method": "Card", "discount_code": "SAVE10"}, headers={"X-POS-PIN": pin_} if pin_ else {})
    assert r.status_code == code_ and detail(r)["code"] in ("pin_required", "pin_invalid"), r.text
assert len(ok(ana.get("/cart"))["items"]) == 1                                      # a refused PIN changed nothing
ok(ana.post("/checkout", json={"payment_method": "Card", "discount_code": "NONSENSE"}))   # an invalid code needs no PIN
ok(ana.post("/scan", json={"sku": "8964001", "qty": 1}))
sale = ok(ana.post("/checkout", json={"payment_method": "Card", "discount_code": "SAVE10"}, headers={"X-POS-PIN": "4829"}))["receipt"]["id"]
before = stock("8964001")
assert detail(ana.post(f"/sales/{sale}/refund", json={"note": "x"}))["code"] == "pin_required"
assert detail(ana.post(f"/sales/{sale}/refund", json={"note": "x"}, headers={"X-POS-PIN": "9999"}))["code"] == "pin_invalid"
assert detail(ana.post(f"/sales/{sale}/refund", json={"note": "x"}, headers={"X-POS-PIN": "7391"}))["code"] == "pin_invalid"   # bob's PIN is not ana's
assert stock("8964001") == before
ok(ana.post(f"/sales/{sale}/refund", json={"note": "damaged"}, headers={"X-POS-PIN": "4829"})); assert stock("8964001") == before + 1
assert detail(ana.post("/inventory/adjust", json={"sku": "8964001", "new_stock": 5}))["code"] == "pin_required"
ok(ana.post("/inventory/adjust", json={"sku": "8964001", "new_stock": before + 1}, headers={"X-POS-PIN": "4829"}))
# an owner needs no PIN for any of it (they proved themselves with the password)
ok(owner.post("/scan", json={"sku": "8964002", "qty": 1})); s2 = ok(owner.post("/checkout", json={"payment_method": "Cash", "discount_code": "SAVE20"}))["receipt"]["id"]
ok(owner.post(f"/sales/{s2}/refund", json={"note": "owner refund"}))
a = audit_rows("sale.refund"); assert a[-2]["actor"] == "ana" and a[-1]["actor"] == "boss" and a[-1]["approved_by"] is None
assert sql("SELECT actor FROM refunds ORDER BY id")[0][0] == "ana"
print("own-PIN confirmations ok")

# ───────────────────────── PIN lockout, even under parallel guessing ─────────────────────────
ok(owner.post("/users", json={"username": "cara", "full_name": "Cara", "role": "employee", "pin": "6208"}))
cid = sql("SELECT id FROM users WHERE username='cara'")[0][0]
res = []
def guess(i):
    c = client(); r = c.post("/auth/pin-login", json={"username": "cara", "pin": f"{1000 + i * 7:04d}" if i % 2 else f"{5000 + i:04d}"}); res.append(r.status_code)
ts = [threading.Thread(target=guess, args=(i,)) for i in range(14)]; [t.start() for t in ts]; [t.join() for t in ts]
assert sorted(set(res)) == [401, 423] and res.count(401) == 4 and res.count(423) == 10, res
assert len(sql("SELECT 1 FROM audit_log WHERE action='auth.pin_failed' AND entity_id=?", str(cid))) == 4 and len(audit_rows("auth.pin_locked")) == 1
r = client().post("/auth/pin-login", json={"username": "cara", "pin": "6208"}); assert r.status_code == 423                         # the right PIN is refused while locked
assert ok(owner.get("/users"))["users"][0]["role"] == "owner"
ok(owner.post(f"/users/{cid}/unlock", json={})); emp_login("cara", "6208")
print("PIN lockout ok")

# ───────────────────────── owner password login and lockout ─────────────────────────
for i in range(4):
    r = client().post("/auth/login", json={"username": "boss2", "password": "wrong-pass"}); assert r.status_code == 401 and r.json()["detail"] == "Wrong name, password or code."
r = client().post("/auth/login", json={"username": "boss2", "password": "wrong-pass"}); assert r.status_code == 401
r = client().post("/auth/login", json={"username": "boss2", "password": "Boss2-pass-1"}); assert r.status_code == 423 and "locked" in r.json()["detail"]
b2 = sql("SELECT id FROM users WHERE username='boss2'")[0][0]
ok(owner.post(f"/users/{b2}/unlock", json={})); boss2 = owner_login("boss2", "Boss2-pass-1")
print("owner password ok")

# ───────────────────────── 2FA (authenticator app) ─────────────────────────
r = ok(owner.post("/auth/2fa/begin", json={})); secret = r["secret"]; assert r["uri"].startswith("otpauth://totp/")
raw = sql("SELECT totp_secret FROM users WHERE username='boss'")[0][0]; assert secret not in raw and raw.startswith("seal1$")     # stored sealed
assert owner.post("/auth/2fa/confirm", json={"code": "000000"}).status_code == 422
step = totp.current_step(); good = totp._code(secret, step)
rec = ok(owner.post("/auth/2fa/confirm", json={"code": good}))["recovery_codes"]; assert len(rec) == 8 and len(set(rec)) == 8
assert all(rec_ not in json.dumps(sql("SELECT recovery_codes FROM users WHERE username='boss'")[0][0]) for rec_ in rec)                # only hashes stored
ok(owner.post("/auth/2fa/begin", json={}), 409)
# sign-in now needs the code
r = client().post("/auth/login", json={"username": "boss", "password": "Boss-pass-1"}); assert r.status_code == 401 and detail(r)["code"] == "code_required"
assert sql("SELECT failed_logins FROM users WHERE username='boss'")[0][0] == 0                                                        # asking for the code is not a failure
r = client().post("/auth/login", json={"username": "boss", "password": "Boss-pass-1", "code": "123456"}); assert r.status_code == 401 and detail(r) == "Wrong name, password or code."
assert sql("SELECT failed_logins FROM users WHERE username='boss'")[0][0] == 1
nxt = totp._code(secret, step + 1)                                                                                                    # a valid code in the allowed window
owner2 = owner_login("boss", "Boss-pass-1", nxt)
r = client().post("/auth/login", json={"username": "boss", "password": "Boss-pass-1", "code": nxt}); assert r.status_code == 401        # the same code cannot be used twice
r = client().post("/auth/login", json={"username": "boss", "password": "Boss-pass-1", "code": rec[0]}); assert r.status_code == 200      # a recovery code works...
r = client().post("/auth/login", json={"username": "boss", "password": "Boss-pass-1", "code": rec[0]}); assert r.status_code == 401      # ...once
assert len(audit_rows("auth.recovery_used")) == 1
# switching 2FA off needs password AND a code
assert owner2.post("/auth/2fa/disable", json={"password": "wrong", "code": rec[1]}).status_code == 403
assert owner2.post("/auth/2fa/disable", json={"password": "Boss-pass-1", "code": "111111"}).status_code == 403
ok(owner2.post("/auth/2fa/disable", json={"password": "Boss-pass-1", "code": rec[1]}))
assert sql("SELECT totp_enabled, totp_secret FROM users WHERE username='boss'")[0] == (0, None)
owner = owner_login("boss", "Boss-pass-1")
print("2FA ok")

# ───────────────────────── developer: separate credential, mandatory 2FA, hidden ─────────────────────────
r = client().post("/auth/login", json={"username": "dev1", "password": "Dev-pass-123"}); assert r.status_code == 401 and detail(r)["code"] == "code_required"
dev = owner_login("dev1", "Dev-pass-123", totp._code(dsecret, totp.current_step()))
d = ok(dev.get("/developer/diagnostics")); assert [s["name"] for s in d["stages"]][:5] == ["network", "identity", "lock", "permission", "confirmation"]
assert owner.get("/developer/diagnostics").status_code == 403 and owner.post("/network/override", json={"enforce": False}).status_code == 403
ok(dev.get("/users"))                                                                                                                   # a developer outranks an owner
assert dev.post("/auth/2fa/disable", json={"password": "Dev-pass-123", "code": "123456"}).status_code == 409
print("developer ok")

# ───────────────────────── guest: read-only, off unless the owner allows it ─────────────────────────
assert ok(anon.get("/auth/status"))["guest_enabled"] is False and client().post("/auth/guest", json={}).status_code == 403
ok(owner.post("/settings", json={"key": "guest_enabled", "value": "yes"}), 422); ok(owner.post("/settings", json={"key": "nope", "value": 1}), 404)
assert ana.post("/settings", json={"key": "guest_enabled", "value": True}).status_code == 403
ok(owner.post("/settings", json={"key": "guest_enabled", "value": True}))
g = client(); assert ok(g.post("/auth/guest", json={}))["user"]["role"] == "guest"
ok(g.get("/inventory")); ok(g.get("/dashboard")); ok(g.get("/cart"))
for key, (role, pin, _) in policy.POLICY.items():
    if role != "guest" and key != "POST /auth/logout":
        method, path = key.split(" ", 1); r = g.get(concrete(path)) if method == "GET" else g.post(concrete(path), json={})
        assert r.status_code == 403, (key, r.status_code)
ok(g.post("/auth/logout", json={}))
assert "guest" not in [p["username"] for p in ok(anon.get("/auth/people"))["people"]]
g2 = client(); ok(g2.post("/auth/guest", json={})); main.db.execute("UPDATE sessions SET expires_ts = now() - interval '1 second' WHERE user_id=(SELECT id FROM users WHERE username='guest')")
assert g2.get("/inventory").status_code == 401                                                                                          # guest sessions end by themselves
ok(owner.post("/settings", json={"key": "guest_enabled", "value": False}))
print("guest ok")

# ───────────────────────── the 1-minute lock ─────────────────────────
assert ok(anon.get("/auth/status"))["lock_seconds"] == 60
ok(ana.post("/scan", json={"sku": "8964003", "qty": 3})); st0 = stock("8964003")
ok(ana.post("/auth/lock", json={}))
assert ok(ana.get("/auth/status"))["locked"] is True
for call in (lambda: ana.get("/inventory"), lambda: ana.post("/scan", json={"sku": "8964003"}), lambda: ana.get("/alerts"), lambda: ana.post(f"/sales/{sale}/refund", json={"note": "x"}, headers={"X-POS-PIN": "4829"})):
    r = call(); assert r.status_code == 423 and detail(r)["code"] == "locked", r.text
r = ana.post("/auth/unlock", json={"pin": "0000"}); assert r.status_code == 403 and detail(r)["code"] == "pin_invalid"
r = ana.post("/auth/unlock", json={"pin": "7391"}); assert r.status_code == 403                                                          # bob's PIN does not unlock ana
ok(ana.post("/auth/unlock", json={"pin": "4829"}))
assert [c["qty"] for c in ok(ana.get("/cart"))["items"]] == [3] and stock("8964003") == st0                                             # lock touches nothing
# the server locks it by itself when it hears nothing (closed tab / modified screen)
main.db.execute("UPDATE sessions SET last_input_ts = now() - interval '100 seconds' WHERE user_id=(SELECT id FROM users WHERE username='ana')")
r = ana.get("/inventory"); assert r.status_code == 423, r.text
assert len(audit_rows("auth.auto_locked")) == 1
ok(ana.post("/auth/unlock", json={"pin": "4829"}))
# activity: a scan (POST) and the ping count; a read does not
def idle_secs(): return sql("SELECT extract(epoch from now()-last_input_ts) FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='ana')")[0][0]
main.db.execute("UPDATE sessions SET last_input_ts = now() - interval '40 seconds' WHERE user_id=(SELECT id FROM users WHERE username='ana')")
ok(ana.get("/inventory")); assert idle_secs() > 39                                                                                      # reading is passive
ok(ana.post("/auth/activity", json={})); assert idle_secs() < 5
main.db.execute("UPDATE sessions SET last_input_ts = now() - interval '40 seconds' WHERE user_id=(SELECT id FROM users WHERE username='ana')")
ok(ana.post("/scan", json={"sku": "8964003", "qty": 1})); assert idle_secs() < 5                                                        # a scan is activity
ok(ana.post("/cart/clear"))
# owners unlock with their PASSWORD (counted towards lockout)
ok(owner.post("/auth/lock", json={}))
assert owner.get("/users").status_code == 423
assert owner.post("/auth/unlock", json={"pin": "4829"}).status_code == 403
r = owner.post("/auth/unlock", json={"password": "nope"}); assert r.status_code == 403
ok(owner.post("/auth/unlock", json={"password": "Boss-pass-1"})); ok(owner.get("/users"))
# a locked person can still sign out (shift change); the next person signs in on the same till
ok(ana.post("/auth/lock", json={})); ok(ana.post("/auth/logout", json={})); assert ana.get("/inventory").status_code == 401
ana = emp_login("ana", "4829")
print("lock ok")

# ───────────────────────── the ordered pipeline ─────────────────────────
def trace(fn):
    pipeline.TRACE = []
    try: r = fn()
    finally: t, pipeline.TRACE = pipeline.TRACE, None
    return t, r
T = ["network", "identity", "lock", "permission", "confirmation"]
t, r = trace(lambda: ana.post("/scan", json={"sku": "8964003"})); assert t == T and r.status_code == 200, t; ok(ana.post("/cart/clear"))
t, r = trace(lambda: nolog.get("/inventory")); assert t == ["network", "identity"] and r.status_code == 401, t
t, r = trace(lambda: anon.get("/auth/status")); assert t == ["network"], t
t, r = trace(lambda: ana.get("/users")); assert t == T[:4] and r.status_code == 403, t                                  # refused at permission: never reaches the PIN stage or the action
ok(ana.post("/auth/lock", json={}))
t, r = trace(lambda: ana.post("/scan", json={"sku": "8964003"})); assert t == ["network", "identity", "lock"] and r.status_code == 423, t      # a locked till never reaches permission or the action
ok(ana.post("/auth/unlock", json={"pin": "4829"}))
assert [s[1] for s in pipeline.STAGES] == ["high"] * 5 + ["low"] * 2                                                                         # safety gates first, business work after
print("pipeline order ok:", " → ".join(s[0] for s in pipeline.STAGES))

# ───────────────────────── network: shop network only, approved connection, alerts ─────────────────────────
PRESENT = {"127.0.0.1", "192.168.1.10", "192.168.1.11"}
netpolicy.is_present = lambda ip: ip in PRESENT
def till(client_ip, server_ip="192.168.1.10"):
    c = TestClient(main.app, client=(client_ip, 5000), base_url=f"http://{server_ip}"); tok = [ck.value for ck in ana.cookies.jar if ck.name == "pos_session"][0]; c.headers["Cookie"] = f"pos_session={tok}"; c.headers["X-POS"] = "1"; return c
assert till("8.8.8.8").get("/inventory").status_code == 403 and detail(till("8.8.8.8").get("/inventory"))["code"] == "network_blocked"      # the internet is never allowed
assert detail(till("8.8.8.8").get("/auth/status"))["code"] == "network_blocked"                                                              # not even the login screen
ok(till("192.168.1.50").get("/inventory"))                                                                                                  # a till on the shop network
t, r = trace(lambda: till("8.8.8.8").get("/inventory")); assert t == ["network"], t                                                          # refused before anything else ran
# the owner chooses the approved connection (password re-asked; only real addresses of this computer)
assert owner.post("/network/policy", json={"approved_ips": ["192.168.1.10"], "password": "wrong"}).status_code == 403
ok(owner.post("/network/policy", json={"approved_ips": ["10.9.9.9"], "password": "Boss-pass-1"}), 422)                                       # not on this computer: would lock tills out
ok(owner.post("/network/policy", json={"approved_ips": ["8.8.8.8"], "password": "Boss-pass-1"}), 422)                                        # not a shop address
ok(owner.post("/network/policy", json={"approved_ips": ["nonsense"], "password": "Boss-pass-1"}), 422)
assert ana.post("/network/policy", json={"approved_ips": [], "password": "x"}).status_code == 403
ok(owner.post("/network/policy", json={"approved_ips": ["192.168.1.10"], "password": "Boss-pass-1"}))
ok(till("192.168.1.50", "192.168.1.10").get("/inventory"))
r = till("192.168.1.50", "192.168.1.99").get("/inventory"); assert r.status_code == 403 and "approved network" in detail(r)["message"]          # arrived through another interface
ok(till("127.0.0.1", "127.0.0.1").get("/inventory"))                                                                                         # the POS computer itself is always allowed
net = ok(owner.get("/network")); assert net["approved_ips"] == ["192.168.1.10"] and any(d["ip"] == "127.0.0.1" for d in net["detected"])
# cable pulled: the approved address disappears -> a CRITICAL alert, after 2 misses in a row (no false alarms)
PRESENT.discard("192.168.1.10")
alerts.monitor_once(); assert not [a for a in ok(ana.get("/alerts"))["alerts"] if a["key"] == "network.interface_down"]
alerts.monitor_once()
al = [a for a in ok(ana.get("/alerts"))["alerts"] if a["key"] == "network.interface_down"]; assert len(al) == 1 and al[0]["severity"] == "critical" and al[0]["needs_ack"]
alerts.monitor_once(); alerts.monitor_once(); assert len([a for a in ok(ana.get("/alerts"))["alerts"] if a["key"] == "network.interface_down"]) == 1   # one alert, not one per check
assert al[0]["times"] >= 1 and len(audit_rows("alert.raised")) == 1
# explicit acknowledgement: who and when recorded, alert STAYS active
ok(bob.post(f"/alerts/{al[0]['id']}/ack", json={}))
al = [a for a in ok(ana.get("/alerts"))["alerts"] if a["key"] == "network.interface_down"][0]
assert al["ack_by"] == "bob" and al["ack_ts"] and al["still_active"] and not al["needs_ack"]
a_ = audit_rows("alert.acknowledged")[-1]; assert a_["actor"] == "bob" and "still active" in a_["summary"]
ok(bob.post(f"/alerts/{al['id']}/ack", json={})); assert len(audit_rows("alert.acknowledged")) == 1                                              # a second click changes nothing
ok(ana.post("/alerts/999999/ack", json={}) if False else ana.post("/alerts/999999/ack", json={}), 404)
assert g.get("/alerts").status_code in (401, 403)
# the owner fixes it himself in Settings (no developer call): choose the other connection
ok(owner.post("/network/policy", json={"approved_ips": ["192.168.1.11"], "password": "Boss-pass-1"}))
assert not [a for a in ok(ana.get("/alerts"))["alerts"] if a["key"] == "network.interface_down"]                                              # resolved and already acknowledged: gone
# a second incident that nobody acknowledges stays visible even after the cable is back
PRESENT.discard("192.168.1.11"); alerts.monitor_once(); alerts.monitor_once()
PRESENT.add("192.168.1.11"); alerts.monitor_once()
al = [a for a in ok(ana.get("/alerts"))["alerts"] if a["key"] == "network.interface_down"]; assert len(al) == 1 and not al[0]["still_active"] and al[0]["needs_ack"]
ok(ana.post(f"/alerts/{al[0]['id']}/ack", json={})); assert not [a for a in ok(ana.get("/alerts"))["alerts"] if a["key"] == "network.interface_down"]
# only a developer can switch protection off; it is flagged while off
assert owner.post("/network/override", json={"enforce": False}).status_code == 403
ok(dev.post("/network/override", json={"enforce": False})); assert till("8.8.8.8").get("/inventory").status_code == 200
assert [a for a in ok(ana.get("/alerts"))["alerts"] if a["key"] == "network.protection_off"]
ok(dev.post("/network/override", json={"enforce": True})); assert till("8.8.8.8").get("/inventory").status_code == 403
assert not [a for a in ok(ana.get("/alerts"))["alerts"] if a["key"] == "network.protection_off"]
ok(owner.post("/network/policy", json={"approved_ips": [], "password": "Boss-pass-1"}))
print("network + alerts ok")

# ───────────────────────── cash drawer ─────────────────────────
n0 = len(sql("SELECT 1 FROM drawer_events"))
ok(ana.post("/scan", json={"sku": "8964004"})); cs = ok(ana.post("/checkout", json={"payment_method": "Cash"}))["receipt"]["id"]
ev = sql("SELECT * FROM drawer_events ORDER BY id")[-1]; assert ev["kind"] == "sale" and ev["sale_id"] == cs and ev["actor"] == "ana" and ev["ok"] == 1
ok(ana.post("/scan", json={"sku": "8964004"})); ok(ana.post("/checkout", json={"payment_method": "Card"})); assert len(sql("SELECT 1 FROM drawer_events")) == n0 + 1   # card: no drawer
ok(ana.post("/checkout", json={"payment_method": "Cash"}), 400); assert len(sql("SELECT 1 FROM drawer_events")) == n0 + 1                                   # failed sale: no drawer
assert ana.post("/drawer/open", json={"reason": "change"}, headers={"X-POS-PIN": "4829"}).status_code == 403                                                  # employees: off by default
ok(owner.post("/drawer/open", json={"reason": "bank run"})); ok(owner.post("/drawer/open", json={"reason": "x"}), 422)
ok(owner.post("/settings", json={"key": "employee_manual_drawer", "value": True}))
assert detail(ana.post("/drawer/open", json={"reason": "give change"}))["code"] == "pin_required"
ok(ana.post("/drawer/open", json={"reason": "give change"}, headers={"X-POS-PIN": "4829"}))
assert [e["kind"] for e in ok(owner.get("/drawer/events"))["events"][:2]] == ["manual", "manual"] and ana.get("/drawer/events").status_code == 403
assert len(audit_rows("drawer.manual_open")) == 2
ok(owner.post("/settings", json={"key": "employee_manual_drawer", "value": False}))
print("drawer ok")

# ───────────────────────── shifts / cash-up ─────────────────────────
ok(owner.post("/settings", json={"key": "require_shift", "value": True}))
ok(ana.post("/scan", json={"sku": "8964005"})); assert ana.post("/checkout", json={"payment_method": "Cash"}).status_code == 409
ok(ana.post("/checkout", json={"payment_method": "Card"}))                                                                                                    # card needs no shift
ok(ana.post("/shifts/open", json={"opening_float": -5}), 422)
ok(ana.post("/shifts/open", json={"opening_float": 500})); ok(ana.post("/shifts/open", json={"opening_float": 500}), 409)
cur = ok(ana.get("/shifts/current")); assert cur["shift"]["opening_float"] == 500 and set(cur["shift"]) == {"id", "opened_ts", "opening_float"}
ok(ana.post("/scan", json={"sku": "8964005", "qty": 2})); r1 = ok(ana.post("/checkout", json={"payment_method": "Cash"}))["receipt"]["totals"]["total"]
ok(ana.post("/scan", json={"sku": "8964005", "qty": 1})); ok(ana.post("/checkout", json={"payment_method": "Card"}))
res = ok(ana.post("/shifts/close", json={"counted_cash": 500 + r1 - 400}))
assert res == {"ok": True, "submitted": True}                                                                                                                 # nothing about expected cash
assert "expected" not in json.dumps(ok(ana.get("/shifts/current"))) and ok(ana.get("/shifts/current"))["shift"] is None
sh = ok(owner.get("/shifts"))["shifts"][0]
assert abs(sh["expected_cash"] - (500 + r1)) < 0.01 and abs(sh["variance"] - (-400)) < 0.01 and sh["actor"] == "ana"
al = [a for a in ok(ana.get("/alerts"))["alerts"] if a["key"] == f"cash.variance.{sh['id']}"]; assert len(al) == 1 and al[0]["severity"] == "warning"
assert not re.search(r"\d{3}", al[0]["title"] + al[0]["detail"].replace("#" + str(sh["id"]), ""))                                                               # no figures in the alert text
assert ana.get("/shifts").status_code == 403 and ana.post(f"/shifts/{sh['id']}/review", json={}).status_code == 403
ok(owner.post(f"/shifts/{sh['id']}/review", json={"note": "recounted"})); assert not [a for a in ok(ana.get("/alerts"))["alerts"] if a["key"] == f"cash.variance.{sh['id']}"]
# a refund of a cash sale lowers the cash expected
ok(ana.post("/shifts/open", json={"opening_float": 100})); ok(ana.post("/scan", json={"sku": "8964006", "qty": 2})); cs2 = ok(ana.post("/checkout", json={"payment_method": "Cash"}))["receipt"]
ok(ana.post(f"/sales/{cs2['id']}/refund", json={"note": "returned"}, headers={"X-POS-PIN": "4829"}))
ok(ana.post("/shifts/close", json={"counted_cash": 100})); sh2 = ok(owner.get("/shifts"))["shifts"][0]; assert abs(sh2["expected_cash"] - 100) < 0.01 and sh2["variance"] == 0
assert not [a for a in ok(ana.get("/alerts"))["alerts"] if a["key"] == f"cash.variance.{sh2['id']}"]                                                          # a matching count raises nothing
ok(ana.post("/shifts/close", json={"counted_cash": 1}), 409)
ok(owner.post("/settings", json={"key": "require_shift", "value": False}))
print("shifts ok")

# ───────────────────────── card machine credentials never reach the screen ─────────────────────────
SENT = "SECRET-KEY-9f8e7d6c5b4a"; MERCH = "MERCHANT-77123"
assert ana.post("/integrations/card/credentials", json={"merchant_id": MERCH, "api_key": SENT}).status_code == 403
ok(owner.post("/integrations/card/credentials", json={"merchant_id": "", "api_key": SENT}), 422)
r = ok(owner.post("/integrations/card/credentials", json={"merchant_id": MERCH, "api_key": SENT})); assert r == {"ok": True, "ref": "card-terminal"}
st = ok(owner.get("/integrations/card")); assert st["configured"] and st["updated_by"] == "boss" and set(st) == {"ref", "configured", "updated_ts", "updated_by"}
blob = ""
for cl in (owner, dev, ana):
    for p in ("/integrations/card", "/users", "/history?limit=500", "/settings", "/network", "/alerts", "/security/policy", "/shifts", "/drawer/events", "/developer/diagnostics"):
        rr = cl.get(p)
        if rr.status_code == 200: blob += rr.text
assert SENT not in blob and MERCH not in blob and "SECRET-KEY" not in blob
dump = json.dumps([list(map(str, r)) for t in ("audit_log", "vault", "users", "settings", "alerts", "movements", "sales") for r in sql(f"SELECT * FROM {t}")])
assert SENT not in dump and MERCH not in dump                                                                                                                # not in the database in the clear either
assert cardvault.use()["api_key"] == SENT                                                                                                                    # the server-side driver can still use it
assert any(a["action"] == "integration.card_credentials_set" for a in ok(owner.get("/history?limit=50"))["entries"])
ok(owner.post("/integrations/card/clear", json={})); assert ok(owner.get("/integrations/card"))["configured"] is False
print("card vault ok")

# ───────────────────────── audit-log protection ─────────────────────────
assert ana.get("/history").status_code == 403                                                                      # employees cannot even read it
for p in ("/history", "/history/1"):
    for m in ("put", "delete", "patch"):
        assert getattr(owner, m)(p).status_code in (404, 405), (m, p)                                               # no route edits or deletes history
for q in ("UPDATE audit_log SET summary='x' WHERE id=1", "DELETE FROM audit_log WHERE id=1", "TRUNCATE audit_log",
          "UPDATE drawer_events SET reason='x'", "DELETE FROM drawer_events", "DELETE FROM movements WHERE id=1"):
    try: main.db.execute(q); raise SystemExit("history must be append-only: " + q)
    except Exception as e: assert "append-only" in str(e), (q, e)
print("audit protection ok")

# ───────────────────────── sessions and people rules ─────────────────────────
c2 = emp_login("bob", "7391"); ok(c2.post("/auth/logout", json={})); assert c2.get("/inventory").status_code == 401
old = client(); ok(old.post("/auth/pin-login", json={"username": "bob", "pin": "7391"})); bid = sql("SELECT id FROM users WHERE username='bob'")[0][0]
ok(owner.post(f"/users/{bid}/pin", json={"new_pin": "1234"}), 422); ok(owner.post(f"/users/{bid}/pin", json={"new_pin": "3047"}))
assert old.get("/inventory").status_code == 401 and client().post("/auth/pin-login", json={"username": "bob", "pin": "7391"}).status_code == 401   # the old PIN is dead, old logins ended
bob = emp_login("bob", "3047")
ok(owner.post(f"/users/{bid}/active", json={"active": False})); assert bob.get("/inventory").status_code == 401                                   # deactivated: next click is refused
assert client().post("/auth/pin-login", json={"username": "bob", "pin": "3047"}).status_code == 401 and "bob" not in [p["username"] for p in ok(anon.get("/auth/people"))["people"]]
ok(owner.post(f"/users/{bid}/active", json={"active": True}))
ok(owner.post(f"/users/{bid}/password", json={"new_password": "Whatever-123"}), 404)                                                             # employees have no password to reset
oid = sql("SELECT id FROM users WHERE username='boss'")[0][0]
ok(owner.post(f"/users/{oid}/password", json={"new_password": "Whatever-123"}), 409)                                                              # own password: Account page
ok(owner.post(f"/users/{oid}/active", json={"active": False}), 409)
ok(owner.post(f"/users/{b2}/password", json={"new_password": "short"}), 422)
b2c = owner_login("boss2", "Boss2-pass-1")
ok(owner.post(f"/users/{b2}/password", json={"new_password": "Boss2-newpass-9"})); assert b2c.get("/users").status_code == 401                 # reset signs them out at once
assert client().post("/auth/login", json={"username": "boss2", "password": "Boss2-pass-1"}).status_code == 401
boss2 = owner_login("boss2", "Boss2-newpass-9")
ok(boss2.post(f"/users/{oid}/active", json={"active": False})); assert owner.get("/users").status_code == 401                                   # boss deactivated by boss2
ok(boss2.post(f"/users/{oid}/active", json={"active": True})); owner = owner_login("boss", "Boss-pass-1")
ok(boss2.post(f"/users/{b2}/active", json={"active": False}), 409)
# own password change: needs the current one, signs out other sessions
other = owner_login("boss", "Boss-pass-1")
assert owner.post("/auth/password", json={"current_password": "wrong", "new_password": "Brand-new-pass-1"}).status_code == 403
ok(owner.post("/auth/password", json={"current_password": "Boss-pass-1", "new_password": "Boss-pass-1"}), 422)
ok(owner.post("/auth/password", json={"current_password": "Boss-pass-1", "new_password": "Brand-new-pass-1"}))
assert other.get("/users").status_code == 401 and ok(owner.get("/users"))
assert owner.post("/auth/password", json={"current_password": "Brand-new-pass-1", "new_password": "Boss-pass-1"}).status_code == 200
# hashes are salted and PINs are useless without the secret key
assert len({r[0] for r in sql("SELECT pin_hash FROM users WHERE pin_hash IS NOT NULL")}) == len(sql("SELECT pin_hash FROM users WHERE pin_hash IS NOT NULL"))
h = sql("SELECT pin_hash FROM users WHERE username='ana'")[0][0]; assert hashing.verify_pin(h, "4829")
hashing._pepper_cache = b"another-key"; assert not hashing.verify_pin(h, "4829"); hashing._pepper_cache = b"test-secret-key"
assert "4829" not in json.dumps([list(map(str, r)) for r in sql("SELECT * FROM users")])
sess = sql("SELECT token_hash FROM sessions")[0][0]; assert len(sess) == 64
print("sessions + people ok")

# ───────────────────────── who did what; two employees at once ─────────────────────────
ana = emp_login("ana", "4829"); bob = emp_login("bob", "3047")
ok(ana.post("/scan", json={"sku": "8964007"})); ok(ana.post("/checkout", json={"payment_method": "Card"})); ok(bob.post("/scan", json={"sku": "8964007"})); ok(bob.post("/checkout", json={"payment_method": "Card"}))
assert [s["actor"] for s in ok(owner.get("/sales?limit=2"))["sales"]] == ["bob", "ana"]
assert sql("SELECT actor FROM movements ORDER BY id DESC LIMIT 2") [0][0] == "bob"
print("who-did-what ok")

# ───────────────────────── CORS ─────────────────────────
r = anon.get("/auth/status", headers={"Origin": "https://evil.example"}); assert "access-control-allow-origin" not in r.headers
r = anon.get("/auth/status", headers={"Origin": "http://localhost:5173"}); assert r.headers["access-control-allow-origin"] == "http://localhost:5173" and r.headers["access-control-allow-credentials"] == "true"
print("cors ok")

# ───────────────────────── upgrading a v10 database (manager / cashier / password+PIN for everyone) ─────────────────────────
main.db.execute("DROP TABLE sessions CASCADE")                      # (sessions + users get rebuilt below in the old shape)
for q in ("ALTER TABLE users DROP CONSTRAINT users_role_check", "UPDATE users SET role='employee' WHERE role IN ('guest','developer')"):
    main.db.execute(q)
with main.transaction():
    main.db.execute("DELETE FROM users WHERE username IN ('guest')")
    main.db.execute("UPDATE users SET role='cashier', pw_hash=?, pin_hash=? WHERE username='ana'", (hashing.hash_password("old-password-1"), hashing.hash_pin("4829")))
    main.db.execute("UPDATE users SET role='manager', pin_hash=?, must_change=1 WHERE username='boss2'", (hashing.hash_pin("5820"),))
    main.db.execute("UPDATE users SET role='employee' WHERE username='dev1'")
    main.db.execute("ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('owner','manager','cashier','employee'))")
    main.db.execute("ALTER TABLE users ALTER COLUMN pw_hash SET NOT NULL") if False else None
database.init()
rows = {r["username"]: r for r in sql("SELECT username, role, pw_hash, pin_hash, must_change FROM users")}
assert rows["ana"]["role"] == "employee" and rows["ana"]["pw_hash"] is None and rows["ana"]["pin_hash"]                           # cashier -> employee, loses the password, keeps the PIN
assert rows["boss2"]["role"] == "owner" and rows["boss2"]["pin_hash"] is None and rows["boss2"]["pw_hash"]                       # manager -> owner, password kept, PIN dropped
assert sql("SELECT COUNT(*) FROM sales")[0][0] > 5 and database._schema_current()                                                  # business data untouched
print("v10 upgrade ok")

print("SECURITY: ALL TESTS PASSED")
