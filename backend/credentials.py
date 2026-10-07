"""
credentials.py — checking what a person types: an employee's PIN, an owner's password (re-asked), and the
"confirm this action" rule.

  * Employees sign in with a name + PIN and confirm sensitive actions (refund, discount, stock count) with their
    OWN PIN. Owners and developers proved themselves with a password (and 2FA) when they signed in or unlocked, so
    their actions need no PIN: typing a PIN again and again is exactly what this model avoids.
  * Wrong tries are counted on their own connection (they survive the failed request): 5 wrong PINs lock that
    PIN for 5 minutes; 5 wrong passwords lock that account for 10. The owner can unlock.
"""
import re
from typing import Optional

from fastapi import HTTPException

import context
from database import audit, db, independent, transaction
from hashing import burn_time, verify_password, verify_pin

MAX_FAILS, LOGIN_LOCK_MIN, PIN_LOCK_MIN = 5, 10, 5
ROLES = ("guest", "employee", "owner", "developer")
RANK = {r: i for i, r in enumerate(ROLES)}


def detail(code: str, message: str) -> dict:
    return {"code": code, "message": message, "override": False}


def pin_attempt(user_id: Optional[int], pin: str) -> tuple[str, str, Optional[str]]:
    """Checks one employee PIN. Returns (status, message, username); status is ok / bad / locked."""
    with independent():
        r = db.execute("SELECT id, username, role, active, pin_hash, pin_failed, (pin_locked_until > now()) AS locked, pin_locked_until "
                       "FROM users WHERE id=? FOR UPDATE", (user_id,)).fetchone() if user_id else None
        if r is None or not r["active"] or r["role"] != "employee" or not r["pin_hash"]:
            burn_time()
            return "bad", "Wrong name or PIN.", None
        if r["locked"]:
            return "locked", f"This PIN is locked until {str(r['pin_locked_until'])[11:16]} after too many wrong tries. Ask the owner to unlock it.", r["username"]
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


def confirm(rule: Optional[str]) -> None:
    """Demand the person's PIN for this request. Safe inside an endpoint (conditional rules, e.g. only when a price
    really changes). Owners/developers pass without one. Raises 403 pin_required / pin_invalid (the screen turns it
    into a PIN box and retries) or 423 pin_locked."""
    ctx = context.get_ctx()
    if ctx is None:
        raise HTTPException(401, "Please log in.")
    if not rule or ctx.pin_done:
        return
    me = ctx.user
    if me["role"] in ("owner", "developer"):
        ctx.pin_done = True
        return
    if not ctx.pin:
        raise HTTPException(403, detail("pin_required", "Enter your PIN to confirm."))
    if not re.fullmatch(r"\d{4,8}", ctx.pin):
        raise HTTPException(403, detail("pin_invalid", "A PIN is 4 to 8 digits."))
    status, msg, _ = pin_attempt(me["id"], ctx.pin)
    if status == "locked":
        raise HTTPException(423, detail("pin_locked", msg))
    if status != "ok":
        raise HTTPException(403, detail("pin_invalid", msg))
    ctx.pin_done = True


def check_my_password(uid: int, pw: str) -> None:
    """Re-asks the password before a sensitive owner action; wrong tries count towards the account lockout."""
    with independent():
        r = db.execute("SELECT username, pw_hash, failed_logins FROM users WHERE id=? FOR UPDATE", (uid,)).fetchone()
        if r and verify_password(r["pw_hash"], pw or ""):
            return
        if r:
            fails = r["failed_logins"] + 1
            if fails >= MAX_FAILS:
                db.execute("UPDATE users SET failed_logins=0, locked_until=now()+make_interval(mins => ?::int) WHERE id=?", (LOGIN_LOCK_MIN, uid))
                audit("auth.locked", "user", uid, f"{r['username']} locked after {MAX_FAILS} wrong passwords (re-checking a password)", actor=r["username"], user_id=uid)
            else:
                db.execute("UPDATE users SET failed_logins=? WHERE id=?", (fails, uid))
    raise HTTPException(403, "That password is wrong.")
