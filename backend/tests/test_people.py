"""Owner controls over people: change a name or username, remove an employee (throw-away *_test database).

Run from the backend folder (Windows):
    set POS_TEST_DATABASE_URL=postgresql://pos:pos@localhost:5432/pos_test
    python tests/test_people.py
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); BACK = os.path.dirname(HERE); sys.path.insert(0, BACK)
TEST_URL = os.environ.get("POS_TEST_DATABASE_URL", "postgresql://pos:pos@localhost:5432/pos_test")
assert TEST_URL.split("?")[0].rstrip("/").endswith("_test")
os.environ["DATABASE_URL"] = TEST_URL; os.environ["POS_SECRET_KEY"] = "test-secret-key"; os.environ["POS_TEST_MODE"] = "1"
os.environ["POS_ALLOW_REMOTE_SETUP"] = "1"; os.environ["POS_THROTTLE_MAX"] = "1000000"
import psycopg
with psycopg.connect(TEST_URL, autocommit=True) as _c:
    _c.execute("DROP SCHEMA public CASCADE"); _c.execute("CREATE SCHEMA public")
from fastapi.testclient import TestClient
import main

def ok(r, code=200): assert r.status_code == code, (r.status_code, r.text); return r.json()
def sql(q, *a): return main.db.execute(q, a).fetchall()
def client(): n = TestClient(main.app); n.headers["X-POS"] = "1"; return n
def pin(p): return {"X-POS-PIN": p}
def people(c): return {u["username"]: u for u in ok(c.get("/users"))["users"]}

# a fresh database has no people: the first screen creates the owner
fresh = client(); st = ok(fresh.get("/auth/status")); assert ok(fresh.get("/auth/people"))["people"] == []
owner = client(); ok(owner.post("/auth/setup", json={"username": "owner", "full_name": "Owner", "password": "Owner-Test-91"}))
for n, p in (("salesman1", "4829"), ("salesman2", "5731"), ("salesman3", "6482")):
    ok(owner.post("/users", json={"username": n, "full_name": n, "role": "employee", "pin": p}))
s1 = client(); ok(s1.post("/auth/pin-login", json={"username": "salesman1", "pin": "4829"}))
s2 = client(); ok(s2.post("/auth/pin-login", json={"username": "salesman2", "pin": "5731"}))
ids = {n: u["id"] for n, u in people(owner).items()}

# 1. rename: display name only, username only, both; clashes and bad names refused; old name still on past work
ok(s1.post("/scan", json={"sku": "8964001", "qty": 1})); ok(s1.post("/checkout", json={"payment_method": "Card"}))
ok(owner.post(f"/users/{ids['salesman1']}/update", json={"full_name": "Salesman One"}))
assert people(owner)["salesman1"]["full_name"] == "Salesman One"
ok(owner.post(f"/users/{ids['salesman1']}/update", json={"username": "cashier1"}))
assert "cashier1" in people(owner) and "salesman1" not in people(owner)
assert owner.post(f"/users/{ids['salesman2']}/update", json={"username": "CASHIER1"}).status_code == 409           # taken (any case)
assert owner.post(f"/users/{ids['salesman2']}/update", json={"username": "x"}).status_code == 422                  # too short
assert owner.post(f"/users/{ids['salesman2']}/update", json={"username": "guest"}).status_code == 422              # reserved
assert s2.post(f"/users/{ids['salesman2']}/update", json={"full_name": "Hacker"}).status_code == 403               # employees cannot
assert sql("SELECT actor FROM sales ORDER BY id DESC LIMIT 1")[0][0] == "salesman1"                                # history keeps the name used then
assert any(t["username"] == "cashier1" for t in ok(fresh.get("/auth/people"))["people"])                            # the sign-in tile follows
ok(client().post("/auth/pin-login", json={"username": "cashier1", "pin": "4829"}))                                  # same PIN, new username
assert client().post("/auth/pin-login", json={"username": "salesman1", "pin": "4829"}).status_code in (401, 403)
print("rename ok")

# 2. remove an employee
assert owner.post(f"/users/{ids['salesman3']}/remove").status_code == 200
assert "salesman3" not in people(owner) and all(t["username"] != "salesman3" for t in ok(fresh.get("/auth/people"))["people"])
assert client().post("/auth/pin-login", json={"username": "salesman3", "pin": "6482"}).status_code in (401, 403)     # cannot sign in any more
assert owner.post(f"/users/{ids['salesman3']}/remove").status_code == 404                                           # already gone
assert owner.post(f"/users/{ids['salesman3']}/pin", json={"new_pin": "7391"}).status_code == 404                    # and cannot be edited
ok(owner.post("/users", json={"username": "salesman3", "full_name": "New Salesman", "role": "employee", "pin": "7391"}))   # the name is free again
assert s2.post(f"/users/{ids['salesman2']}/remove").status_code == 403                                             # employees cannot remove
assert owner.post(f"/users/{ids['owner']}/remove").status_code == 404                                              # owners are not removed here
assert sql("SELECT COUNT(*) FROM users WHERE id=? AND removed=1", ids["salesman3"])[0][0] == 1                     # the row stays: history still points at it
assert sql("SELECT COUNT(*) FROM audit_log WHERE action IN ('user.removed','user.renamed')")[0][0] == 2

# a removed person's open session ends at once
s2b = client(); ok(s2b.post("/auth/pin-login", json={"username": "salesman2", "pin": "5731"})); ok(s2b.get("/cart"))
ok(owner.post(f"/users/{ids['salesman2']}/remove")); assert s2b.get("/cart").status_code == 401

# an employee with a shift open cannot be removed until it is closed
t = client(); ok(t.post("/auth/pin-login", json={"username": "salesman3", "pin": "7391"})); tid = people(owner)["salesman3"]["id"]
ok(t.post("/shifts/open", json={"opening_float": 100}))
r = owner.post(f"/users/{tid}/remove"); assert r.status_code == 409, r.text
ok(t.post("/shifts/close", json={"counted_cash": 100})); ok(owner.post(f"/users/{tid}/remove"))
print("remove ok")
print("\nPEOPLE TEST PASSED")
