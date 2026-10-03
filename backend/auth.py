"""
auth.py — who is using the POS, and what they are allowed to do.

  * LOGIN: every person has their own username + password (stored only as a salted scrypt hash). A session is a
    random token kept in an HttpOnly cookie; the database keeps only its SHA-256, so a copy of the database
    can't be used to log in. Sessions last POS_SESSION_HOURS (default 12). 5 wrong passwords lock that account
    for 10 minutes.
  * ROLES: cashier < manager < owner.  Cashier sells.  Manager also runs the catalog, purchasing, expenses,
    reports and History.  Owner also manages people (Security page).
  * PIN: a 4–8 digit code per person, asked at the moment of a sensitive action (refund, stock count, discount,
    price change, receiving stock, voiding an expense, paying a supplier, managing users). PINs are stored as
    salted scrypt hashes of an HMAC that uses a secret key kept OUTSIDE the database (backend/pos_secret.key),
    so a stolen database/backup alone can't be used to guess PINs. 5 wrong PINs lock that PIN for 5 minutes.
  * APPROVAL: a cashier can't refund / count stock / give a discount alone, but a manager or owner can approve
    it by typing their username and PIN on the cashier's screen. History records both people.
  * EVERY route must appear in POLICY below (or in PUBLIC). install() refuses to start the app if one is missing,
    so a new endpoint can never be added by accident without a permission rule.
  * Every History line, stock movement and invoice records the logged-in person.
"""
import hashlib
import hmac
import os
import re
import secrets
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

import database
from database import audit, db, independent, transaction

COOKIE = "pos_session"
SESSION_HOURS = float(os.environ.get("POS_SESSION_HOURS", "12"))
MAX_FAILS, LOGIN_LOCK_MIN, PIN_LOCK_MIN = 5, 10, 5
RANK = {"cashier": 1, "manager": 2, "owner": 3}
ROLES = tuple(RANK)

router = APIRouter()


# ───────────────────────────── hashing ─────────────────────────────
_pepper_cache: Optional[bytes] = None


def _pepper() -> bytes:
    """Secret mixed into every PIN hash. Lives in the POS_SECRET_KEY variable or backend/pos_secret.key (created on
    first start). Keep that file with your backups' safe copy: without it, PINs must be reset by the owner."""
    global _pepper_cache
    if _pepper_cache is None:
        env = os.environ.get("POS_SECRET_KEY")
        if env:
            _pepper_cache = env.encode()
        else:
            f = Path(__file__).with_name("pos_secret.key")
            if not f.exists():
                f.write_text(secrets.token_hex(32))
                try:
                    f.chmod(0o600)
                except OSError:
                    pass
            _pepper_cache = f.read_text().strip().encode()
    return _pepper_cache


def _scrypt(secret: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(secret, salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    return f"scrypt1${salt.hex()}${_scrypt(pw.encode(), salt).hex()}"


def hash_pin(pin: str) -> str:
    salt = os.urandom(16)
    return f"scrypt1${salt.hex()}${_scrypt(hmac.new(_pepper(), pin.encode(), 'sha256').digest(), salt).hex()}"


def _verify(stored: str, secret: bytes) -> bool:
    try:
        _, salt, dk = stored.split("$")
        return hmac.compare_digest(_scrypt(secret, bytes.fromhex(salt)).hex(), dk)
    except Exception:
        return False


def verify_password(stored: str, pw: str) -> bool:
    return _verify(stored, pw.encode())


def verify_pin(stored: str, pin: str) -> bool:
    return _verify(stored, hmac.new(_pepper(), pin.encode(), "sha256").digest())


_DUMMY: Optional[str] = None


def _burn_time() -> None:
    """Unknown usernames take as long as real ones, so response time doesn't reveal which usernames exist."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("not-a-real-password")
    verify_password(_DUMMY, "x")


COMMON = {"password", "12345678", "123456789", "1234567890", "qwertyui", "qwerty123", "11111111", "00000000", "password1", "iloveyou", "admin123", "letmein1"}


def check_password(pw: str, username: str = "") -> str:
    if len(pw) < 8:
        raise HTTPException(422, "Password must be at least 8 characters.")
    if len(pw) > 200:
        raise HTTPException(422, "Password is too long (200 characters at most).")
    if pw.lower() in COMMON or (username and pw.lower() == username.lower()):
        raise HTTPException(422, "That password is too easy to guess. Pick something else.")
    return pw


def check_pin(pin: str) -> str:
    if not re.fullmatch(r"\d{4,8}", pin or ""):
        raise HTTPException(422, "PIN must be 4 to 8 digits (numbers only).")
    if len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
        raise HTTPException(422, "That PIN is too easy to guess (like 1111 or 1234). Pick another.")
    return pin


def check_username(u: str) -> str:
    u = (u or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", u):
        raise HTTPException(422, "Username must be 3–32 characters: letters, numbers, dot, dash or underscore.")
    return u


# ───────────────────────────── who is calling ─────────────────────────────
class Ctx:
    """The logged-in person for this request, plus the PIN / approver they supplied."""
    def __init__(self, user: dict, pin: Optional[str], approver: Optional[str]):
        self.user, self.pin, self.approver, self.pin_done = user, pin, approver, False


_ctx: ContextVar[Optional[Ctx]] = ContextVar("pos_auth_ctx", default=None)


def current_user() -> dict:
    c = _ctx.get()
    if c is None:
        raise HTTPException(401, "Please log in.")
    return c.user


def _sha(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def _lookup(token: Optional[str]) -> Optional[dict]:
    """Session cookie -> active user (or None). Role and active-status are read fresh on EVERY request, so
    deactivating someone or changing their role takes effect immediately."""
    if not token:
        return None
    h = _sha(token)
    with transaction():
        r = db.execute("SELECT u.id, u.username, u.full_name, u.role, u.must_change, (s.last_seen_ts < now() - interval '1 minute') AS stale "
                       "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=? AND s.expires_ts > now() AND u.active=1", (h,)).fetchone()
        if r and r["stale"]:
            db.execute("UPDATE sessions SET last_seen_ts=now() WHERE token_hash=?", (h,))
    if not r:
        return None
    d = dict(r)
    d["must_change"], d["token_hash"] = bool(d["must_change"]), h
    d.pop("stale", None)
    return d


# ───────────────────────────── permission table ─────────────────────────────
# key = "METHOD /path"  ->  (lowest role allowed, PIN rule, what it is)
#   PIN rule:  None       no PIN
#              "self"     the person doing it types their OWN PIN
#              "approval" a manager/owner types their own PIN; a cashier needs a manager/owner to approve (username + PIN)
POLICY: dict[str, tuple[str, Optional[str], str]] = {}
PUBLIC = {"GET /auth/status", "POST /auth/setup", "POST /auth/login"}
MUST_CHANGE_OK = {"POST /auth/logout", "POST /auth/password"}


def _p(method: str, path: str, role: str, pin: Optional[str] = None, label: str = "") -> None:
    POLICY[f"{method} {path}"] = (role, pin, label)


# selling (everyone who works the till)
for m_, p_, l_ in [("GET", "/inventory", "See products and stock"), ("POST", "/scan", "Scan / add an item to the cart"),
                   ("POST", "/cart/remove", "Remove an item from the cart"), ("POST", "/cart/set", "Change a cart quantity / weight"),
                   ("POST", "/cart/clear", "Clear the cart"), ("GET", "/cart", "See the cart"),
                   ("GET", "/sales", "See the invoice list"), ("GET", "/sales/{sale_id}", "Open / reprint an invoice"),
                   ("GET", "/movements", "See the stock ledger"), ("GET", "/dashboard", "Dashboard"), ("GET", "/events", "Live updates")]:
    _p(m_, p_, "cashier", None, l_)
_p("POST", "/checkout", "cashier", None, "Check out a sale (a discount code needs a manager's PIN: see below)")
_p("POST", "/sales/{sale_id}/refund", "cashier", "approval", "Refund (whole or part of an invoice)")
_p("POST", "/inventory/adjust", "cashier", "approval", "Correct stock after a physical count")
# catalog
for m_, p_, l_ in [("GET", "/catalog/meta", "Catalog: lists"), ("GET", "/catalog/products", "Catalog: see products"),
                   ("GET", "/catalog/products/{pid}/history", "Catalog: a product's change history"),
                   ("POST", "/catalog/products", "Catalog: add a product"), ("POST", "/catalog/products/{pid}/update", "Catalog: edit a product (changing price or cost needs the PIN)"),
                   ("POST", "/catalog/products/{pid}/active", "Catalog: deactivate / reactivate a product")]:
    _p(m_, p_, "manager", None, l_)
# purchasing, suppliers, expenses, reports, history
for m_, p_, l_ in [("GET", "/purchasing/products", "Purchasing: product list"), ("GET", "/purchasing/meta", "Purchasing: lists"),
                   ("GET", "/suppliers", "See suppliers"), ("POST", "/suppliers", "Add a supplier"), ("POST", "/suppliers/{sid}/update", "Edit / deactivate a supplier"),
                   ("GET", "/purchase-orders", "See purchase orders"), ("GET", "/purchase-orders/{po_id}", "Open a purchase order"),
                   ("POST", "/purchase-orders", "Create a purchase order"), ("GET", "/purchases", "See purchases (deliveries)"),
                   ("GET", "/purchases/{rid}", "Open a purchase"), ("GET", "/expenses", "See expenses"), ("POST", "/expenses", "Add an expense"),
                   ("GET", "/reports/purchases", "Reports"), ("GET", "/history", "History tab"), ("GET", "/history/meta", "History tab: filters")]:
    _p(m_, p_, "manager", None, l_)
_p("POST", "/purchase-orders/{po_id}/receive", "manager", "self", "Receive stock from a supplier")
_p("POST", "/purchase-orders/{po_id}/cancel", "manager", "self", "Cancel a purchase order")
_p("POST", "/purchase-orders/{po_id}/close", "manager", "self", "Close a purchase order short")
_p("POST", "/purchases/{rid}/pay", "manager", "self", "Mark a supplier purchase as paid")
_p("POST", "/expenses/{eid}/void", "manager", "self", "Void an expense")
# people & security
_p("POST", "/auth/logout", "cashier", None, "Log out")
_p("POST", "/auth/password", "cashier", None, "Change my own password")
_p("POST", "/auth/pin", "cashier", None, "Change my own PIN")
_p("GET", "/users", "owner", None, "Security: see people")
_p("POST", "/users", "owner", "self", "Security: add a person")
_p("POST", "/users/{uid}/update", "owner", "self", "Security: change someone's name / role")
_p("POST", "/users/{uid}/password", "owner", "self", "Security: reset someone's password")
_p("POST", "/users/{uid}/pin", "owner", "self", "Security: reset someone's PIN")
_p("POST", "/users/{uid}/active", "owner", "self", "Security: deactivate / reactivate a person")
_p("POST", "/users/{uid}/unlock", "owner", None, "Security: unlock a locked account")
_p("GET", "/security/policy", "owner", None, "Security: this permission table")


def install(app) -> None:
    """Startup check: every route must have an explicit rule. Forgetting one stops the app from starting."""
    seen = {f"{m.upper()} {path}" for path, ops in app.openapi()["paths"].items() for m in ops}
    missing = sorted(seen - set(POLICY) - PUBLIC)
    if missing:
        raise RuntimeError("These routes have no permission rule in auth.POLICY (add them): " + ", ".join(missing))
    stale = sorted(set(POLICY) - seen)
    if stale:
        raise RuntimeError("auth.POLICY lists routes that don't exist: " + ", ".join(stale))


def _detail(code: str, message: str, override: bool = False) -> dict:
    return {"code": code, "message": message, "override": override}


async def authed(request: Request) -> None:
    """Runs before EVERY endpoint (app-wide dependency): checks the login, the role and, where the rule says so, the PIN."""
    _ctx.set(None)
    route = request.scope.get("route")
    key = f"{request.method} {getattr(route, 'path', request.url.path)}"
    if key in PUBLIC:
        return
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-pos") != "1":
        raise HTTPException(403, "Missing X-POS header (blocked: requests must come from the POS screen).")
    user = await run_in_threadpool(_lookup, request.cookies.get(COOKIE))
    if user is None:
        raise HTTPException(401, "Please log in.")
    pol = POLICY.get(key)
    if pol is None:
        raise HTTPException(403, "No permission rule for this action.")            # fail closed
    if user["must_change"] and key not in MUST_CHANGE_OK:
        raise HTTPException(403, _detail("must_change_password", "Choose a new password before continuing."))
    role, pin_rule, _ = pol
    if RANK[user["role"]] < RANK[role]:
        raise HTTPException(403, f"Your role ({user['role']}) can't do this. It needs {role} or higher.")
    _ctx.set(Ctx(user, request.headers.get("x-pos-pin"), (request.headers.get("x-pos-approver") or "").strip() or None))
    database.set_actor(user["id"], user["username"])
    if pin_rule:
        await run_in_threadpool(confirm, pin_rule)


# ───────────────────────────── PIN confirmation ─────────────────────────────
def _pin_attempt(user_id: Optional[int], pin: str, need_role: str) -> tuple[str, str, Optional[str]]:
    """Checks one PIN on its own connection (so a wrong try is remembered even though the request then fails).
    Returns (status, message, username) with status ok / bad / locked."""
    with independent():
        r = db.execute("SELECT id, username, role, active, pin_hash, pin_failed, (pin_locked_until > now()) AS locked, pin_locked_until "
                       "FROM users WHERE id=? FOR UPDATE", (user_id,)).fetchone() if user_id else None
        if r is None or not r["active"] or RANK[r["role"]] < RANK[need_role]:
            _burn_time()
            return "bad", "Wrong username or PIN.", None
        if r["locked"]:
            return "locked", f"That PIN is locked until {str(r['pin_locked_until'])[11:16]} after too many wrong tries.", r["username"]
        if verify_pin(r["pin_hash"], pin):
            if r["pin_failed"]:
                db.execute("UPDATE users SET pin_failed=0 WHERE id=?", (r["id"],))
            return "ok", "", r["username"]
        fails = r["pin_failed"] + 1
        if fails >= MAX_FAILS:
            db.execute("UPDATE users SET pin_failed=0, pin_locked_until=now()+make_interval(mins => ?::int) WHERE id=?", (PIN_LOCK_MIN, r["id"]))
            audit("auth.pin_locked", "user", r["id"], f"PIN of {r['username']} locked for {PIN_LOCK_MIN} minutes after {MAX_FAILS} wrong tries", actor=r["username"], user_id=r["id"])
            return "locked", f"Wrong PIN. It is now locked for {PIN_LOCK_MIN} minutes.", r["username"]
        db.execute("UPDATE users SET pin_failed=? WHERE id=?", (fails, r["id"]))
        audit("auth.pin_failed", "user", r["id"], f"Wrong PIN for {r['username']} ({fails} of {MAX_FAILS})", actor=r["username"], user_id=r["id"])
        left = MAX_FAILS - fails
        return "bad", f"Wrong PIN ({left} {'try' if left == 1 else 'tries'} left).", r["username"]


def confirm(rule: str) -> None:
    """Demand the PIN for this request. Safe to call from inside an endpoint (conditional rules, e.g. only when a
    price actually changes). Raises 403 pin_required / pin_invalid, which the screen turns into a PIN box and a retry."""
    ctx = _ctx.get()
    if ctx is None:
        raise HTTPException(401, "Please log in.")
    if ctx.pin_done:
        return
    me = ctx.user
    override = rule == "approval" and RANK[me["role"]] < RANK["manager"]
    if override and not ctx.approver:
        raise HTTPException(403, _detail("pin_required", "A manager or owner must approve this: enter their username and PIN.", True))
    if not override and not ctx.pin:
        raise HTTPException(403, _detail("pin_required", "Enter your PIN to confirm.", False))
    if not re.fullmatch(r"\d{4,8}", ctx.pin or ""):
        raise HTTPException(403, _detail("pin_invalid", "A PIN is 4 to 8 digits.", override))
    if override:
        with transaction():
            row = db.execute("SELECT id FROM users WHERE lower(username)=lower(?)", (ctx.approver,)).fetchone()
        status, msg, who = _pin_attempt(row["id"] if row else None, ctx.pin, "manager")
    else:
        status, msg, who = _pin_attempt(me["id"], ctx.pin, "cashier")
    if status == "locked":
        raise HTTPException(423, _detail("pin_locked", msg, override))
    if status != "ok":
        raise HTTPException(403, _detail("pin_invalid", msg, override))
    ctx.pin_done = True
    if override:
        database.set_approver(who)


# ───────────────────────────── sessions ─────────────────────────────
def _start_session(response: Response, request: Request, user_id: int) -> None:
    tok = secrets.token_urlsafe(32)
    db.execute("DELETE FROM sessions WHERE expires_ts < now()")
    db.execute("INSERT INTO sessions(token_hash, user_id, expires_ts, ip, agent) VALUES (?,?, now() + make_interval(secs => ?::float8), ?, ?)",
               (_sha(tok), user_id, SESSION_HOURS * 3600, (request.client.host if request.client else "")[:64], request.headers.get("user-agent", "")[:200]))
    response.set_cookie(COOKIE, tok, max_age=int(SESSION_HOURS * 3600), httponly=True, samesite="lax", path="/")


def _public(u) -> dict:
    return {"id": u["id"], "username": u["username"], "full_name": u["full_name"], "role": u["role"], "must_change": bool(u["must_change"])}


def no_users() -> bool:
    with transaction():
        return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None


def create_user(username: str, full_name: str, role: str, password: str, pin: str, must_change: bool, actor: Optional[str] = None) -> int:
    """Adds a person (checks everything). Used by the web app and by manage_users.py."""
    username = check_username(username)
    if role not in ROLES:
        raise HTTPException(422, "Role must be owner, manager or cashier.")
    check_password(password, username)
    check_pin(pin)
    if db.execute("SELECT 1 FROM users WHERE lower(username)=lower(?)", (username,)).fetchone():
        raise HTTPException(409, f'The username "{username}" is already taken.')
    uid = db.execute("INSERT INTO users(username, full_name, role, pw_hash, pin_hash, must_change) VALUES (?,?,?,?,?,?)",
                     (username, (full_name or "").strip()[:80], role, hash_password(password), hash_pin(pin), 1 if must_change else 0)).lastrowid
    audit("user.created", "user", username, f"Person added: {username} ({role})", {"role": role, "full_name": (full_name or "").strip()[:80]},
          **({"actor": actor, "user_id": uid if actor == username else None} if actor else {}))      # first-time setup: the new owner creates themselves
    return uid


# ───────────────────────────── public endpoints ─────────────────────────────
class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=200)


class SetupIn(BaseModel):
    username: str
    full_name: str = ""
    password: str = Field(max_length=200)
    pin: str = Field(max_length=20)


@router.get("/auth/status")
def status(request: Request):
    u = _lookup(request.cookies.get(COOKIE))
    return {"needs_setup": no_users(), "user": _public(u) if u else None}


@router.post("/auth/setup")
def setup(b: SetupIn, request: Request, response: Response):
    """Creates the FIRST account (the owner). Only works while there are no users, and only from this computer
    (set POS_ALLOW_REMOTE_SETUP=1 to allow it from another one)."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost") and os.environ.get("POS_ALLOW_REMOTE_SETUP") != "1":
        raise HTTPException(403, "The first account can only be created on the computer that runs the POS.")
    with transaction():
        db.execute("SELECT pg_advisory_xact_lock(727002)")
        if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise HTTPException(409, "Setup is already done. Please log in.")
        uid = create_user(b.username, b.full_name, "owner", b.password, b.pin, must_change=False, actor=b.username.strip())
        _start_session(response, request, uid)
        db.execute("UPDATE users SET last_login_ts=now() WHERE id=?", (uid,))
        audit("auth.login", "user", b.username.strip(), f"{b.username.strip()} logged in (first-time setup)", actor=b.username.strip(), user_id=uid)
        u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return {"user": _public(u)}


@router.post("/auth/login")
def login(b: LoginIn, request: Request, response: Response):
    uname = b.username.strip()[:64]
    problem: Optional[tuple[int, str]] = None
    user = None
    with transaction():                                   # committed even when we then answer "wrong password"
        r = db.execute("SELECT *, COALESCE(locked_until > now(), false) AS locked FROM users WHERE lower(username)=lower(?) FOR UPDATE", (uname,)).fetchone()
        if r is None:
            _burn_time()
            audit("auth.login_failed", "user", uname[:40], f"Failed login for unknown username “{uname[:40]}”", actor="unknown")
            problem = (401, "Wrong username or password.")
        elif r["locked"]:
            problem = (423, f"This account is locked until {str(r['locked_until'])[11:16]} after too many wrong passwords. Ask the owner to unlock it.")
        elif not verify_password(r["pw_hash"], b.password) or not r["active"]:
            if not r["active"]:
                audit("auth.login_failed", "user", r["id"], f"Login refused: {r['username']} is deactivated", actor=r["username"], user_id=r["id"])
            else:
                fails = r["failed_logins"] + 1
                if fails >= MAX_FAILS:
                    db.execute("UPDATE users SET failed_logins=0, locked_until=now()+make_interval(mins => ?::int) WHERE id=?", (LOGIN_LOCK_MIN, r["id"]))
                    audit("auth.locked", "user", r["id"], f"{r['username']} locked for {LOGIN_LOCK_MIN} minutes after {MAX_FAILS} wrong passwords", actor=r["username"], user_id=r["id"])
                else:
                    db.execute("UPDATE users SET failed_logins=? WHERE id=?", (fails, r["id"]))
                    audit("auth.login_failed", "user", r["id"], f"Wrong password for {r['username']} ({fails} of {MAX_FAILS})", actor=r["username"], user_id=r["id"])
            problem = (401, "Wrong username or password.")
        else:
            db.execute("UPDATE users SET failed_logins=0, locked_until=NULL, last_login_ts=now() WHERE id=?", (r["id"],))
            _start_session(response, request, r["id"])
            audit("auth.login", "user", r["username"], f"{r['username']} logged in", actor=r["username"], user_id=r["id"])
            user = r
    if problem:
        raise HTTPException(*problem)
    return {"user": _public(user)}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    u = current_user()
    with transaction():
        db.execute("DELETE FROM sessions WHERE token_hash=?", (u["token_hash"],))
        audit("auth.logout", "user", u["username"], f"{u['username']} logged out")
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


# ───────────────────────────── my own account ─────────────────────────────
class PwIn(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


class PinIn(BaseModel):
    current_password: str = Field(max_length=200)
    new_pin: str = Field(max_length=20)


def _check_my_password(uid: int, pw: str) -> None:
    """Re-asks the password before a sensitive self-service change; wrong tries count towards the lockout."""
    with independent():
        r = db.execute("SELECT username, pw_hash, failed_logins FROM users WHERE id=? FOR UPDATE", (uid,)).fetchone()
        if verify_password(r["pw_hash"], pw):
            return
        fails = r["failed_logins"] + 1
        if fails >= MAX_FAILS:
            db.execute("UPDATE users SET failed_logins=0, locked_until=now()+make_interval(mins => ?::int) WHERE id=?", (LOGIN_LOCK_MIN, uid))
            audit("auth.locked", "user", uid, f"{r['username']} locked after {MAX_FAILS} wrong passwords (while changing a password/PIN)", actor=r["username"], user_id=uid)
        else:
            db.execute("UPDATE users SET failed_logins=? WHERE id=?", (fails, uid))
    raise HTTPException(403, "Your current password is wrong.")


@router.post("/auth/password")
def change_password(b: PwIn, request: Request):
    u = current_user()
    _check_my_password(u["id"], b.current_password)
    check_password(b.new_password, u["username"])
    if b.new_password == b.current_password:
        raise HTTPException(422, "The new password must be different from the current one.")
    with transaction():
        db.execute("UPDATE users SET pw_hash=?, must_change=0 WHERE id=?", (hash_password(b.new_password), u["id"]))
        db.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (u["id"], u["token_hash"]))      # every other login is logged out
        audit("user.password_changed", "user", u["username"], f"{u['username']} changed their password")
    return {"ok": True}


@router.post("/auth/pin")
def change_pin(b: PinIn, request: Request):
    u = current_user()
    _check_my_password(u["id"], b.current_password)
    check_pin(b.new_pin)
    with transaction():
        db.execute("UPDATE users SET pin_hash=?, pin_failed=0, pin_locked_until=NULL WHERE id=?", (hash_pin(b.new_pin), u["id"]))
        audit("user.pin_changed", "user", u["username"], f"{u['username']} changed their PIN")
    return {"ok": True}


# ───────────────────────────── owner: manage people ─────────────────────────────
class UserIn(BaseModel):
    username: str
    full_name: str = ""
    role: str
    password: str = Field(max_length=200)
    pin: str = Field(max_length=20)
    must_change: bool = True        # True: it is only a starting password and they must choose their own at first login; False: it stays as it is


class UserPatch(BaseModel):
    full_name: Optional[str] = Field(None, max_length=80)
    role: Optional[str] = None


class ResetPw(BaseModel):
    new_password: str = Field(max_length=200)
    must_change: bool = True        # True: they must replace it at next login; False: it becomes their password as it is


class ResetPin(BaseModel):
    new_pin: str = Field(max_length=20)


class ActiveIn(BaseModel):
    active: bool


def _target(uid: int, lock: bool = True):
    r = db.execute("SELECT * FROM users WHERE id=?" + (" FOR UPDATE" if lock else ""), (uid,)).fetchone()
    if not r:
        raise HTTPException(404, "No such person.")
    return r


def _other_active_owners(excluding: int) -> int:
    db.execute("SELECT id FROM users WHERE role='owner' AND active=1 FOR UPDATE")          # serialises two owners demoting each other
    return db.execute("SELECT COUNT(*) FROM users WHERE role='owner' AND active=1 AND id<>?", (excluding,)).fetchone()[0]


@router.get("/users")
def list_users():
    with transaction(snapshot=True):
        rows = db.execute("SELECT id, username, full_name, role, active, must_change, last_login_ts, created_ts, "
                          "COALESCE(locked_until > now(), false) AS locked, COALESCE(pin_locked_until > now(), false) AS pin_locked, "
                          "(SELECT COUNT(*) FROM sessions s WHERE s.user_id=users.id AND s.expires_ts > now()) AS sessions "
                          "FROM users ORDER BY active DESC, CASE role WHEN 'owner' THEN 1 WHEN 'manager' THEN 2 ELSE 3 END, lower(username)").fetchall()
    return {"users": [dict(r, active=bool(r["active"]), must_change=bool(r["must_change"])) for r in rows]}


@router.post("/users")
def add_user(b: UserIn):
    with transaction():
        uid = create_user(b.username, b.full_name, b.role, b.password, b.pin, must_change=b.must_change)
    return {"id": uid}


@router.post("/users/{uid}/update")
def update_user(uid: int, b: UserPatch):
    me = current_user()
    with transaction():
        r = _target(uid)
        diff = {}
        if b.full_name is not None and b.full_name.strip() != r["full_name"]:
            diff["full_name"] = (r["full_name"], b.full_name.strip())
        if b.role is not None and b.role != r["role"]:
            if b.role not in ROLES:
                raise HTTPException(422, "Role must be owner, manager or cashier.")
            if uid == me["id"]:
                raise HTTPException(409, "You can't change your own role.")
            if r["role"] == "owner" and _other_active_owners(uid) == 0:
                raise HTTPException(409, "There must always be at least one active owner.")
            diff["role"] = (r["role"], b.role)
        if diff:
            db.execute("UPDATE users SET full_name=?, role=? WHERE id=?", (diff.get("full_name", (0, r["full_name"]))[1], diff.get("role", (0, r["role"]))[1], uid))
            audit("user.updated", "user", r["username"], f"{r['username']}: " + ", ".join(f"{k} {o!r} → {n!r}" for k, (o, n) in diff.items()), diff)
    return {"ok": True, "changed": list(diff)}


@router.post("/users/{uid}/password")
def reset_password(uid: int, b: ResetPw):
    me = current_user()
    if uid == me["id"]:
        raise HTTPException(409, "To change your own password use “Change my password”.")
    with transaction():
        r = _target(uid)
        check_password(b.new_password, r["username"])
        db.execute("UPDATE users SET pw_hash=?, must_change=?, failed_logins=0, locked_until=NULL WHERE id=?", (hash_password(b.new_password), 1 if b.must_change else 0, uid))
        db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))                    # their old password and any open login stop working at once
        audit("user.password_reset", "user", r["username"], f"Password of {r['username']} reset ({'they must choose a new one at next login' if b.must_change else 'set as their permanent password'})",
              {"must_change": b.must_change})
    return {"ok": True}


@router.post("/users/{uid}/pin")
def reset_pin(uid: int, b: ResetPin):
    with transaction():
        r = _target(uid)
        check_pin(b.new_pin)
        db.execute("UPDATE users SET pin_hash=?, pin_failed=0, pin_locked_until=NULL WHERE id=?", (hash_pin(b.new_pin), uid))
        audit("user.pin_reset", "user", r["username"], f"PIN of {r['username']} reset")
    return {"ok": True}


@router.post("/users/{uid}/active")
def set_user_active(uid: int, b: ActiveIn):
    me = current_user()
    with transaction():
        r = _target(uid)
        if bool(r["active"]) == b.active:
            return {"ok": True}
        if not b.active:
            if uid == me["id"]:
                raise HTTPException(409, "You can't deactivate yourself.")
            if r["role"] == "owner" and _other_active_owners(uid) == 0:
                raise HTTPException(409, "There must always be at least one active owner.")
        db.execute("UPDATE users SET active=? WHERE id=?", (1 if b.active else 0, uid))
        if not b.active:
            db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))                    # logged out immediately
        audit("user.activated" if b.active else "user.deactivated", "user", r["username"], f"{r['username']} {'reactivated' if b.active else 'deactivated'}")
    return {"ok": True}


@router.post("/users/{uid}/unlock")
def unlock_user(uid: int):
    with transaction():
        r = _target(uid)
        db.execute("UPDATE users SET failed_logins=0, locked_until=NULL, pin_failed=0, pin_locked_until=NULL WHERE id=?", (uid,))
        audit("user.unlocked", "user", r["username"], f"{r['username']} unlocked")
    return {"ok": True}


@router.get("/security/policy")
def policy():
    out = []
    for key, (role, pin, label) in POLICY.items():
        method, path = key.split(" ", 1)
        out.append({"method": method, "path": path, "role": role, "pin": pin, "label": label})
    return {"rules": out, "lockout": {"password_tries": MAX_FAILS, "password_minutes": LOGIN_LOCK_MIN, "pin_tries": MAX_FAILS, "pin_minutes": PIN_LOCK_MIN},
            "session_hours": SESSION_HOURS}
