"""
auth.py — signing in and out, the screen lock, my own password and 2FA.

  employee   GET /auth/people (name tiles) -> POST /auth/pin-login {username, pin}
  owner      POST /auth/login {username, password, code?}   code = 6-digit authenticator code or a one-time recovery code
  developer  same endpoint as the owner; 2FA is mandatory for a developer (set when created with manage_users.py)
  guest      POST /auth/guest (only while the owner has switched the Guest button on): read-only demo, 15 minutes
  lock       POST /auth/lock, POST /auth/unlock {pin | password}, POST /auth/activity (keep-alive ping)
Unknown name, wrong password/PIN and a deactivated person all get the SAME message and take the same time.
"""
import os
import threading
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr

import context
import credentials
import sessions
import settings
import totp
import users
from credentials import LOGIN_LOCK_MIN, MAX_FAILS
from database import audit, db, independent, transaction
from hashing import burn_time, check_password, fingerprint, hash_password, seal, unseal, verify_password

router = APIRouter()
GENERIC = "Wrong name, password or code."

# ── throttle: too many wrong sign-ins from ONE computer in a short time → that computer must wait ──
# (the per-account lockouts stop guessing one person; this stops someone going from name to name)
THROTTLE = {"max": int(os.environ.get("POS_THROTTLE_MAX", "30")), "window": 300}
_fail_log: dict[str, list[float]] = {}
_fail_lock = threading.Lock()


def _caller(request: Request) -> str:
    return (request.client.host if request.client else "") or "?"


def _throttled(request: Request) -> None:
    """Refuses (429) when this computer already failed too often in the last 5 minutes. Counted in memory only."""
    now_ = time.time()
    with _fail_lock:
        log = [t for t in _fail_log.get(_caller(request), []) if now_ - t < THROTTLE["window"]]
        _fail_log[_caller(request)] = log
        if len(log) >= THROTTLE["max"]:
            wait = int(THROTTLE["window"] - (now_ - log[0])) + 1
            raise HTTPException(429, f"Too many wrong sign-in attempts from this computer. Wait {max(1, wait // 60 + (1 if wait % 60 else 0))} minute(s) and try again.")


def _note_failure(request: Request) -> None:
    with _fail_lock:
        _fail_log.setdefault(_caller(request), []).append(time.time())
        if len(_fail_log) > 500:                                   # never grows without bound
            for k in [k for k, v in _fail_log.items() if not v or time.time() - v[-1] > THROTTLE["window"]]:
                _fail_log.pop(k, None)


def no_users() -> bool:
    with transaction():
        return db.execute("SELECT 1 FROM users WHERE role IN ('owner','employee','developer') LIMIT 1").fetchone() is None


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=200)
    code: Optional[str] = Field(None, max_length=32)


class PinLoginIn(BaseModel):
    username: str = Field(max_length=64)
    pin: str = Field(max_length=20)


class SetupIn(BaseModel):
    username: str
    full_name: str = ""
    password: str = Field(max_length=200)


class UnlockIn(BaseModel):
    pin: Optional[str] = Field(None, max_length=20)
    password: Optional[str] = Field(None, max_length=200)
    code: Optional[str] = Field(None, max_length=32)       # owner's 2FA code, only when the owner switched "unlock needs code" on


@router.get("/auth/status")
def status(request: Request):
    u = sessions.resolve(request.cookies.get(sessions.COOKIE))
    return {"needs_setup": no_users(), "user": sessions.public(u) if u else None, "locked": bool(u and u["locked"]),
            "guest_enabled": bool(settings.get("guest_enabled")), "lock_seconds": sessions.LOCK_SECONDS,
            "force_2fa": bool(settings.get("force_owner_2fa")), "unlock_needs_code": bool(settings.get("unlock_needs_code"))}


@router.get("/auth/people")
def people():
    """The name tiles on the sign-in screen: active employees only (never owners or the developer)."""
    with transaction(snapshot=True):
        rows = db.execute("SELECT username, full_name FROM users WHERE role='employee' AND active=1 ORDER BY lower(COALESCE(NULLIF(full_name,''), username))").fetchall()
    return {"people": [{"username": r["username"], "name": r["full_name"] or r["username"]} for r in rows]}


@router.post("/auth/setup")
def setup(b: SetupIn, request: Request, response: Response):
    """Creates the FIRST account (an owner). Works only while nobody exists, and only from this computer
    (POS_ALLOW_REMOTE_SETUP=1 allows another one)."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost") and os.environ.get("POS_ALLOW_REMOTE_SETUP") != "1":
        raise HTTPException(403, "The first account can only be created on the computer that runs the POS.")
    with transaction():
        db.execute("SELECT pg_advisory_xact_lock(727002)")
        if not no_users():
            raise HTTPException(409, "Setup is already done. Please sign in.")
        name = b.username.strip()
        uid = users.create_user(name, b.full_name, "owner", b.password, actor=name)
        sessions.start(response, request, uid)
        db.execute("UPDATE users SET last_login_ts=now() WHERE id=?", (uid,))
        audit("auth.login", "user", name, f"{name} signed in (first-time setup)", actor=name, user_id=uid)
        u = dict(db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone(), locked=False)
    return {"user": sessions.public(u)}


def _count_failure(r, why: str) -> None:
    """Inside the login transaction (row locked): one more wrong password/code for this account."""
    fails = r["failed_logins"] + 1
    if fails >= MAX_FAILS:
        db.execute("UPDATE users SET failed_logins=0, locked_until=now()+make_interval(mins => ?::int) WHERE id=?", (LOGIN_LOCK_MIN, r["id"]))
        audit("auth.locked", "user", r["id"], f"{r['username']} locked for {LOGIN_LOCK_MIN} minutes after {MAX_FAILS} wrong tries", actor=r["username"], user_id=r["id"])
    else:
        db.execute("UPDATE users SET failed_logins=? WHERE id=?", (fails, r["id"]))
        audit("auth.login_failed", "user", r["id"], f"Wrong {why} for {r['username']} ({fails} of {MAX_FAILS})", actor=r["username"], user_id=r["id"])


def _second_factor_ok(r, code: str) -> bool:
    """Authenticator code (each code works once) or a one-time recovery code."""
    step = totp.verify(unseal(r["totp_secret"]), code, r["totp_last_step"])
    if step is not None:
        db.execute("UPDATE users SET totp_last_step=? WHERE id=?", (step, r["id"]))
        return True
    fp = fingerprint(totp.normalise_recovery(code))
    left = list(r["recovery_codes"] or [])
    if fp in left:
        left.remove(fp)
        import json
        db.execute("UPDATE users SET recovery_codes=?::jsonb WHERE id=?", (json.dumps(left), r["id"]))
        audit("auth.recovery_used", "user", r["id"], f"{r['username']} signed in with a one-time recovery code ({len(left)} left)", actor=r["username"], user_id=r["id"])
        return True
    return False


@router.post("/auth/login")
def login(b: LoginIn, request: Request, response: Response):
    """Owner / developer sign-in: password, then the 2FA code if 2FA is on."""
    _throttled(request)
    uname = b.username.strip()[:64]
    problem: Optional[tuple[int, object]] = None
    user = None
    with transaction():                                   # committed even when we then answer "wrong"
        r = db.execute("SELECT *, COALESCE(locked_until > now(), false) AS locked FROM users WHERE lower(username)=lower(?) FOR UPDATE", (uname,)).fetchone()
        if r is None or r["role"] not in ("owner", "developer"):
            burn_time()
            audit("auth.login_failed", "user", uname[:40], f"Failed sign-in for “{uname[:40]}” (no such owner)", actor="unknown")
            problem = (401, GENERIC)
        elif r["locked"]:
            problem = (423, f"This account is locked until {str(r['locked_until'])[11:16]} after too many wrong tries. Another owner can unlock it, or wait.")
        elif not r["active"] or not verify_password(r["pw_hash"], b.password):
            if not r["active"]:
                audit("auth.login_failed", "user", r["id"], f"Sign-in refused: {r['username']} is deactivated", actor=r["username"], user_id=r["id"])
            else:
                _count_failure(r, "password")
            problem = (401, GENERIC)
        elif r["totp_enabled"] and not (b.code or "").strip():
            problem = (401, {"code": "code_required", "message": "Enter the 6-digit code from your authenticator app.", "override": False})
        elif r["totp_enabled"] and not _second_factor_ok(r, b.code or ""):
            _count_failure(r, "2FA code")
            problem = (401, GENERIC)
        else:
            db.execute("UPDATE users SET failed_logins=0, locked_until=NULL, last_login_ts=now() WHERE id=?", (r["id"],))
            sessions.start(response, request, r["id"])
            audit("auth.login", "user", r["username"], f"{r['username']} signed in", actor=r["username"], user_id=r["id"])
            user = dict(r, locked=False)
    if problem:
        if not (isinstance(problem[1], dict) and problem[1].get("code") == "code_required"):      # asking for the code is not a failure
            _note_failure(request)
        raise HTTPException(*problem)
    return {"user": sessions.public(user)}


@router.post("/auth/pin-login")
def pin_login(b: PinLoginIn, request: Request, response: Response):
    """Employee sign-in: pick your name, type your PIN."""
    _throttled(request)
    uname = b.username.strip()[:64]
    with transaction():
        row = db.execute("SELECT id FROM users WHERE lower(username)=lower(?)", (uname,)).fetchone()
    status_, msg, who = credentials.pin_attempt(row["id"] if row else None, b.pin)
    if status_ == "locked":
        _note_failure(request)
        raise HTTPException(423, msg)
    if status_ != "ok":
        _note_failure(request)
        raise HTTPException(401, msg)
    with transaction():
        db.execute("UPDATE users SET last_login_ts=now() WHERE id=?", (row["id"],))
        sessions.start(response, request, row["id"])
        audit("auth.login", "user", who, f"{who} signed in", actor=who, user_id=row["id"])
        u = dict(db.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone(), locked=False)
    return {"user": sessions.public(u)}


@router.post("/auth/guest")
def guest(request: Request, response: Response):
    if not settings.get("guest_enabled"):
        raise HTTPException(403, "The guest view is switched off.")
    with transaction():
        g = db.execute("SELECT id FROM users WHERE username='guest'").fetchone()
        gid = g["id"] if g else db.execute("INSERT INTO users(username, full_name, role) VALUES ('guest','Guest','guest')").lastrowid
        sessions.start(response, request, gid, minutes=sessions.GUEST_MINUTES)
        audit("auth.guest", "user", "guest", "Guest (read-only) view opened", actor="guest", user_id=gid)
        u = {"id": gid, "username": "guest", "full_name": "Guest", "role": "guest", "totp_enabled": 0}
    return {"user": sessions.public(u)}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    u = context.current_user()
    with transaction():
        db.execute("DELETE FROM sessions WHERE token_hash=?", (u["token_hash"],))
        audit("auth.logout", "user", u["username"], f"{u['username']} signed out")
    response.delete_cookie(sessions.COOKIE, path="/")
    return {"ok": True}


@router.post("/auth/lock")
def lock_now():
    u = context.current_user()
    with transaction():
        sessions.set_locked(u["token_hash"], True)
        audit("auth.screen_locked", "user", u["username"], f"{u['username']} locked the screen")
    return {"ok": True}


@router.post("/auth/activity")
def activity():
    return {"ok": True}                      # the guard already recorded the activity; nothing else to do


@router.post("/auth/unlock")
def unlock(b: UnlockIn, request: Request):
    """Same person, same credential as their sign-in: employee PIN, owner/developer password. Wrong tries count."""
    u = context.current_user()
    if u["role"] == "employee":
        status_, msg, _ = credentials.pin_attempt(u["id"], b.pin or "")
        if status_ == "locked":
            raise HTTPException(423, credentials.detail("pin_locked", msg))
        if status_ != "ok":
            raise HTTPException(403, credentials.detail("pin_invalid", msg))
    else:
        _throttled(request)
        try:
            credentials.check_my_password(u["id"], b.password or "")
        except HTTPException:
            _note_failure(request)
            raise
        if settings.get("unlock_needs_code") and u["role"] == "owner" and u.get("totp_enabled"):
            code = (b.code or "").strip()
            if not code:
                raise HTTPException(401, {"code": "code_required", "message": "Enter the 6-digit code from your authenticator app.", "override": False})
            with transaction():
                r = db.execute("SELECT * FROM users WHERE id=? FOR UPDATE", (u["id"],)).fetchone()
                ok_ = _second_factor_ok(r, code)
            if not ok_:
                _note_failure(request)
                with transaction():
                    r = db.execute("SELECT * FROM users WHERE id=? FOR UPDATE", (u["id"],)).fetchone()
                    _count_failure(r, "2FA code")
                raise HTTPException(403, "That code is wrong.")
    with transaction():
        sessions.set_locked(u["token_hash"], False)
        audit("auth.unlocked", "user", u["username"], f"{u['username']} unlocked the screen")
    return {"ok": True}


# ───────────── my own password and 2FA (owner / developer) ─────────────
class PwIn(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


class CodeIn(BaseModel):
    code: str = Field(max_length=32)


class DisableIn(BaseModel):
    password: SecretStr
    code: str = Field(max_length=32)


@router.post("/auth/password")
def change_password(b: PwIn):
    u = context.current_user()
    credentials.check_my_password(u["id"], b.current_password)
    check_password(b.new_password, u["username"])
    if b.new_password == b.current_password:
        raise HTTPException(422, "The new password must be different from the current one.")
    with transaction():
        db.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(b.new_password), u["id"]))
        db.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (u["id"], u["token_hash"]))
        audit("user.password_changed", "user", u["username"], f"{u['username']} changed their password")
    return {"ok": True}


@router.post("/auth/2fa/begin")
def begin_2fa():
    u = context.current_user()
    with transaction():
        r = db.execute("SELECT totp_enabled FROM users WHERE id=? FOR UPDATE", (u["id"],)).fetchone()
        if r["totp_enabled"]:
            raise HTTPException(409, "2FA is already on.")
        secret = totp.new_secret()
        db.execute("UPDATE users SET totp_secret=? WHERE id=?", (seal(secret), u["id"]))
    return {"secret": secret, "uri": totp.uri(secret, u["username"])}


@router.post("/auth/2fa/confirm")
def confirm_2fa(b: CodeIn):
    u = context.current_user()
    with transaction():
        r = db.execute("SELECT totp_enabled, totp_secret, totp_last_step FROM users WHERE id=? FOR UPDATE", (u["id"],)).fetchone()
        if r["totp_enabled"]:
            raise HTTPException(409, "2FA is already on.")
        if not r["totp_secret"]:
            raise HTTPException(409, "Start the 2FA setup first.")
        step = totp.verify(unseal(r["totp_secret"]), b.code, 0)
        if step is None:
            raise HTTPException(422, "That code is not right. Check the time on this computer and your phone, then try the next code.")
        codes, hashes = totp.new_recovery_codes()
        import json
        db.execute("UPDATE users SET totp_enabled=1, totp_last_step=?, recovery_codes=?::jsonb WHERE id=?", (step, json.dumps(hashes), u["id"]))
        audit("user.2fa_enabled", "user", u["username"], f"{u['username']} switched 2FA on")
    return {"ok": True, "recovery_codes": codes}


@router.post("/auth/2fa/disable")
def disable_2fa(b: DisableIn):
    u = context.current_user()
    if u["role"] == "developer":
        raise HTTPException(409, "2FA is mandatory for a developer.")
    credentials.check_my_password(u["id"], b.password.get_secret_value())
    with transaction():
        r = db.execute("SELECT * FROM users WHERE id=? FOR UPDATE", (u["id"],)).fetchone()
        if not r["totp_enabled"]:
            raise HTTPException(409, "2FA is not on.")
        if not _second_factor_ok(r, b.code):
            raise HTTPException(403, "That code is not right.")
        db.execute("UPDATE users SET totp_enabled=0, totp_secret=NULL, recovery_codes='[]'::jsonb, totp_last_step=0 WHERE id=?", (u["id"],))
        audit("user.2fa_disabled", "user", u["username"], f"{u['username']} switched 2FA off")
    return {"ok": True}
