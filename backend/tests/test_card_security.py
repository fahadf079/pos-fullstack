"""AES-GCM sealing, the card-number guard and the key copy in the database (throw-away database whose name ends in _test).

Run from the backend folder (Windows):
    set POS_TEST_DATABASE_URL=postgresql://pos:pos@localhost:5432/pos_test
    python tests/test_card_security.py
"""
import base64, hashlib, hmac, os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); BACK = os.path.dirname(HERE); sys.path.insert(0, BACK)
TEST_URL = os.environ.get("POS_TEST_DATABASE_URL", "postgresql://pos:pos@localhost:5432/pos_test")
assert TEST_URL.split("?")[0].rstrip("/").endswith("_test")
os.environ["DATABASE_URL"] = TEST_URL; os.environ["POS_SECRET_KEY"] = "test-secret-key"; os.environ["POS_TEST_MODE"] = "1"
os.environ["POS_ALLOW_REMOTE_SETUP"] = "1"; os.environ["POS_THROTTLE_MAX"] = "1000000"
import psycopg
with psycopg.connect(TEST_URL, autocommit=True) as _c:
    _c.execute("DROP SCHEMA public CASCADE"); _c.execute("CREATE SCHEMA public")
from fastapi.testclient import TestClient
import main, hashing, keyescrow, cardvault

def ok(r, code=200): assert r.status_code == code, (r.status_code, r.text); return r.json()
def sql(q, *a): return main.db.execute(q, a).fetchall()
def client(): n = TestClient(main.app); n.headers["X-POS"] = "1"; return n

# 1. AES-GCM sealing
t = hashing.seal("secret-api-key", "card-terminal")
assert t.startswith("seal2$") and "secret-api-key" not in t and hashing.seal("secret-api-key", "card-terminal") != t
assert hashing.unseal(t, "card-terminal") == "secret-api-key"
for bad in (t, ):
    try: hashing.unseal(bad, "other-name"); raise SystemExit("context not enforced")
    except ValueError: pass
raw = bytearray(base64.urlsafe_b64decode(t.split("$", 1)[1])); raw[-1] ^= 1
try: hashing.unseal("seal2$" + base64.urlsafe_b64encode(bytes(raw)).decode(), "card-terminal"); raise SystemExit("tampering not detected")
except ValueError: pass
# a value written by the earlier home-made cipher can still be read
ek, mk = hashing._keys(); nonce = os.urandom(16); data = b"old-2fa-seed"
ct = bytes(a ^ b for a, b in zip(data, hashing._stream(ek, nonce, len(data)))); tag = hmac.new(mk, nonce + ct, "sha256").digest()
assert hashing.unseal("seal1$" + base64.urlsafe_b64encode(nonce + ct + tag).decode()) == "old-2fa-seed"
print("aes-gcm sealing ok (tamper, wrong name and old values)")

# 2. card numbers are refused in free text, ordinary numbers are not
for good in ("order 20260105123", "Rs 1500000 float", "6001234567890", "short 1234 5678"):
    assert hashing.no_card_number(good) == good
for bad in ("4111 1111 1111 1111", "customer card 5500-0000-0000-0004", "4111111111111111"):
    try: hashing.no_card_number(bad); raise SystemExit("missed " + bad)
    except Exception as e: assert getattr(e, "status_code", 0) == 422
owner = client(); ok(owner.post("/auth/setup", json={"username": "owner", "full_name": "Owner", "password": "Owner-Test-91"}))
ok(owner.post("/users", json={"username": "emp", "full_name": "emp", "role": "employee", "pin": "4829"}))
emp = client(); ok(emp.post("/auth/pin-login", json={"username": "emp", "pin": "4829"}))
ok(emp.post("/shifts/open", json={"opening_float": 100}))
assert emp.post("/shifts/close", json={"counted_cash": 100, "note": "card 4111 1111 1111 1111"}).status_code == 422
assert sql("SELECT count(*) FROM shifts WHERE closed_ts IS NOT NULL")[0][0] == 0           # nothing saved
ok(emp.post("/shifts/close", json={"counted_cash": 100, "note": "all good"}))
dump = repr([list(map(str, r)) for t_ in ("audit_log", "shifts") for r in sql(f"SELECT * FROM {t_}")])
assert "4111" not in dump
print("card-number guard ok (refused in notes, nothing saved, nothing in History)")

# 3. card machine credentials are AES sealed under their name
ok(owner.post("/integrations/card/credentials", json={"merchant_id": "M-1", "api_key": "KEY-ABC"}))
sealed = sql("SELECT sealed FROM vault WHERE ref='card-terminal'")[0][0]
assert sealed.startswith("seal2$") and "KEY-ABC" not in sealed and cardvault.use()["api_key"] == "KEY-ABC"
main.db.execute("INSERT INTO vault(ref, sealed) VALUES ('other', ?)", (sealed,))
try: hashing.unseal(sql("SELECT sealed FROM vault WHERE ref='other'")[0][0], "other"); raise SystemExit("copied value opened under another name")
except ValueError: pass
print("card vault ok (AES, bound to its name)")

# 4. key copy in the database: locked with a recovery code, restorable, useless without it
key = hashing.pepper(); code = keyescrow.new_code()
salt, wrapped = keyescrow.wrap(key, code)
assert key.decode() not in wrapped and keyescrow.unwrap(salt, wrapped, code) == key
assert keyescrow.unwrap(salt, wrapped, code.lower().replace("-", " ")) == key           # typing style does not matter
try: keyescrow.unwrap(salt, wrapped, keyescrow.new_code()); raise SystemExit("wrong code accepted")
except SystemExit as e: assert "wrong" in str(e)
KNOWN = keyescrow.new_code(); keyescrow.new_code = lambda: KNOWN; keyescrow.run("create")
row = sql("SELECT salt, wrapped FROM key_escrow WHERE id=1")[0]
assert key.decode() not in str(row)
with tempfile.TemporaryDirectory() as d:
    from pathlib import Path; keyescrow.KEY_FILE = Path(d) / "pos_secret.key"
    import getpass; getpass.getpass = lambda *_: "x"
    try: keyescrow.run("restore"); raise SystemExit("restored with a wrong code")
    except SystemExit as e: assert "wrong" in str(e) and not keyescrow.KEY_FILE.exists()
    getpass.getpass = lambda *_: KNOWN; keyescrow.run("restore")
    assert keyescrow.KEY_FILE.read_bytes() == key                                       # the right code brings the key back
print("key copy ok (locked, restorable, wrong code refused)")
# 5. every answer carries the safety headers (and nothing is cacheable)
for path in ("/auth/status", "/cart", "/integrations/card"):
    h = {k.lower(): v for k, v in owner.get(path).headers.items()}
    assert h["cache-control"] == "no-store" and h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY", (path, h)
    assert h["referrer-policy"] == "no-referrer" and "frame-ancestors 'none'" in h["content-security-policy"], path
h = {k.lower(): v for k, v in client().get("/auth/status").headers.items()}; assert h["x-frame-options"] == "DENY"       # also when signed out
assert "strict-transport-security" not in h                                                                      # only once the POS runs over HTTPS
print("security headers ok")
print("\nCARD SECURITY: ALL TESTS PASSED")
