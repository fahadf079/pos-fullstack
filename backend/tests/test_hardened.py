"""Runs the POS as the RESTRICTED database login (pos_app) and proves it works AND can't tamper.
Needs a throw-away database whose name ends in _test, and the OWNER login for it (default pos/pos)."""
import os, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); BACK = os.path.dirname(HERE); sys.path.insert(0, BACK)
OWNER_URL = os.environ.get("POS_TEST_DATABASE_URL", "postgresql://pos:pos@localhost:5432/pos_test")
assert OWNER_URL.split("?")[0].rstrip("/").endswith("_test"), "POS_TEST_DATABASE_URL must point at a database whose name ends in _test"
# like a real install: "pos" owns the database but cannot create logins; the postgres administrator creates pos_app
ADMIN_URL = os.environ.get("POS_TEST_ADMIN_URL", "postgresql://postgres:pg@localhost:5432/pos_test")

if len(sys.argv) > 1 and sys.argv[1] == "flow":                      # ── child process: the POS running as pos_app ──
    os.environ["POS_SECRET_KEY"] = "test-secret-key"; os.environ["POS_ALLOW_REMOTE_SETUP"] = "1"
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app); c.headers["X-POS"] = "1"
    def ok(r, code=200): assert r.status_code == code, (r.status_code, r.text); return r.json()
    assert main.db.execute("SELECT current_user").fetchone()[0] == "pos_app"
    ok(c.post("/auth/setup", json={"username": "boss", "password": "Boss-pass-1", "pin": "4829"})); c.headers["X-POS-PIN"] = "4829"
    ok(c.post("/scan", json={"sku": "8964001", "qty": 2})); sid = ok(c.post("/checkout", json={"payment_method": "Cash", "discount_code": "SAVE10"}))["receipt"]["id"]
    ok(c.post(f"/sales/{sid}/refund", json={"note": "t", "items": [{"id": 1, "qty": 1}]})); ok(c.post(f"/sales/{sid}/refund", json={"note": "t"}))
    ok(c.post("/inventory/adjust", json={"sku": "8964001", "new_stock": 33}))
    p = ok(c.get("/catalog/products"))[0]; ok(c.post(f"/catalog/products/{p['id']}/update", json={"price": p["price"] + 1}))
    new = ok(c.post("/catalog/products", json={"sku": "999", "name": "Test Tea", "price": 50, "cost": 30, "unit": "kg"}))["product"]
    s = ok(c.post("/suppliers", json={"name": "S1"}))["id"]; po = ok(c.post("/purchase-orders", json={"supplier_id": s, "lines": [{"product_id": new["id"], "qty": 5, "unit_cost": 30}]}))
    it = ok(c.get(f"/purchase-orders/{po['id']}"))["items"][0]["id"]; ok(c.post(f"/purchase-orders/{po['id']}/receive", json={"lines": [{"po_item_id": it, "qty": 5}], "paid": True, "pay_method": "Cash"}))
    e = ok(c.post("/expenses", json={"category": "Rent", "amount": 10}))["id"]; ok(c.post(f"/expenses/{e}/void", json={"reason": "x"}))
    ok(c.post("/users", json={"username": "sara", "role": "cashier", "password": "Sara-temp-12", "pin": "2864"}))
    n = TestClient(main.app); n.headers["X-POS"] = "1"; ok(n.post("/auth/login", json={"username": "sara", "password": "Sara-temp-12"})); ok(n.post("/auth/logout", json={}))
    ok(c.get("/history?limit=5")); ok(c.get("/reports/purchases")); ok(c.get("/dashboard"))
    print("flow ok"); sys.exit(0)

import psycopg
import secure_db, database
with psycopg.connect(OWNER_URL, autocommit=True) as _c:
    _c.execute("DROP SCHEMA public CASCADE"); _c.execute("CREATE SCHEMA public"); _c.execute("DROP ROLE IF EXISTS pos_app") if False else None
app_url = secure_db.harden(ADMIN_URL, "pos_app", "app-test-password-123")
assert secure_db.prove(app_url) == [], secure_db.prove(app_url)
base_env = {k: v for k, v in os.environ.items() if k != "DATABASE_ADMIN_URL"}
env = {**base_env, "DATABASE_URL": app_url}
def run(*args, e=env, **kw): return subprocess.run([sys.executable, *args], cwd=BACK, env=e, capture_output=True, text=True, **kw)

r = run("tests/test_hardened.py", "flow"); assert r.returncode == 0 and "flow ok" in r.stdout, (r.stdout[-800:], r.stderr[-1500:])
# the app, running as pos_app, tried nothing forbidden; now confirm the database still refuses tampering from that login
assert secure_db.prove(app_url) == []
with psycopg.connect(app_url) as c:
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE actor='boss'").fetchone()[0] > 5
    try: c.execute("INSERT INTO audit_log(action) VALUES ('forged')"); c.commit()                      # adding is allowed (append-only)...
    except Exception as ex: raise SystemExit(f"pos_app can't append to history: {ex}")
    c.execute("DELETE FROM audit_log WHERE action='forged'") if False else None
# backups work as the restricted login (pg_dump needs read access only)
with tempfile.TemporaryDirectory() as d:
    r = run("backup.py", "--dir", d); assert r.returncode == 0 and "Backup OK" in r.stdout, (r.stdout, r.stderr)
# manage_users (the recovery tool) works as pos_app, and unlock/activate leave a History line
r = run("manage_users.py", "list"); assert r.returncode == 0 and "boss" in r.stdout and "owner" in r.stdout, (r.stdout, r.stderr)
r = run("manage_users.py", "unlock", "sara"); assert r.returncode == 0 and "Unlocked" in r.stdout, (r.stdout, r.stderr)
r = run("manage_users.py", "unlock", "nobody"); assert r.returncode != 0 and "No person" in r.stdout + r.stderr
with psycopg.connect(app_url) as c: assert c.execute("SELECT COUNT(*) FROM audit_log WHERE actor='cli' AND action='user.unlocked'").fetchone()[0] == 1

# structure upgrade: pos_app can't change tables, so the app says exactly what to do, then init_db.py (owner) fixes it
with psycopg.connect(OWNER_URL, autocommit=True) as c: c.execute("ALTER TABLE audit_log DROP COLUMN approved_by")
r = run("-c", "import main"); assert r.returncode != 0 and "init_db.py" in r.stderr, r.stderr[-600:]
r = run("init_db.py", "--admin-url", OWNER_URL); assert r.returncode == 0, r.stderr
r = run("-c", "import main"); assert r.returncode == 0, r.stderr[-600:]
# with the owner login, the app upgrades itself (the old, simple setup)
with psycopg.connect(OWNER_URL, autocommit=True) as c: c.execute("ALTER TABLE audit_log DROP COLUMN approved_by")
r = run("-c", "import main", e={**base_env, "DATABASE_URL": OWNER_URL}); assert r.returncode == 0, r.stderr[-600:]
print("HARDENED DATABASE: ALL TESTS PASSED")
