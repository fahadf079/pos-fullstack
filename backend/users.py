"""
users.py — people: creating them and the owner's Security page.

  employee  signs in with name + PIN, no password. ONLY an owner can add one, set or change the PIN.
  owner     signs in with a password (+ 2FA once set up). Managers are treated as owners.
  developer created only from the command line (manage_users.py create-developer); never shown, listed or editable here.
  guest     a built-in read-only demo identity; never listed.
People are never deleted from the database (their history keeps their name). An employee can be removed: the login is
wiped, the name is freed and they disappear from every list. The last active owner can never be deactivated.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import context
import policy
from credentials import LOGIN_LOCK_MIN, MAX_FAILS, PIN_LOCK_MIN
from database import audit, db, transaction
from hashing import check_password, check_pin, check_username, hash_password, hash_pin
from sessions import SESSION_HOURS

router = APIRouter()
VISIBLE = ("owner", "employee")


def create_user(username: str, full_name: str, role: str, secret: str, actor: Optional[str] = None) -> int:
    """Adds a person after checking everything. `secret` is the password (owner/developer) or the PIN (employee)."""
    username = check_username(username)
    if role not in ("owner", "employee", "developer"):
        raise HTTPException(422, "A person is an employee or an owner.")
    if db.execute("SELECT 1 FROM users WHERE lower(username)=lower(?)", (username,)).fetchone():
        raise HTTPException(409, f'The name "{username}" is already taken.')
    name = (full_name or "").strip()[:80]
    if role == "employee":
        uid = db.execute("INSERT INTO users(username, full_name, role, pin_hash) VALUES (?,?,?,?)", (username, name, role, hash_pin(check_pin(secret)))).lastrowid
    else:
        uid = db.execute("INSERT INTO users(username, full_name, role, pw_hash) VALUES (?,?,?,?)", (username, name, role, hash_password(check_password(secret, username)))).lastrowid
    audit("user.created", "user", username, f"Person added: {username} ({role})", {"role": role, "full_name": name},
          **({"actor": actor, "user_id": uid if actor == username else None} if actor else {}))
    return uid


class UserIn(BaseModel):
    username: str
    full_name: str = ""
    role: str = "employee"
    pin: Optional[str] = Field(None, max_length=20)          # for an employee
    password: Optional[str] = Field(None, max_length=200)    # for an owner


class NameIn(BaseModel):
    full_name: Optional[str] = Field(None, max_length=80)
    username: Optional[str] = Field(None, max_length=64)


class PwIn(BaseModel):
    new_password: str = Field(max_length=200)


class PinIn(BaseModel):
    new_pin: str = Field(max_length=20)


class ActiveIn(BaseModel):
    active: bool


def _target(uid: int, roles=VISIBLE):
    r = db.execute("SELECT * FROM users WHERE id=? FOR UPDATE", (uid,)).fetchone()
    if not r or r["role"] not in roles or r["removed"]:
        raise HTTPException(404, "No such person.")          # developer / guest rows are invisible here
    return r


@router.get("/users")
def list_users():
    with transaction(snapshot=True):
        rows = db.execute("SELECT id, username, full_name, role, active, last_login_ts, created_ts, totp_enabled, "
                          "COALESCE(locked_until > now(), false) AS locked, COALESCE(pin_locked_until > now(), false) AS pin_locked, "
                          "(SELECT COUNT(*) FROM sessions s WHERE s.user_id=users.id AND s.expires_ts > now()) AS sessions "
                          "FROM users WHERE role IN ('owner','employee') AND removed=0 ORDER BY active DESC, CASE role WHEN 'owner' THEN 1 ELSE 2 END, lower(username)").fetchall()
    return {"users": [dict(r, active=bool(r["active"]), two_factor=bool(r["totp_enabled"])) for r in rows]}


@router.post("/users")
def add_user(b: UserIn):
    if b.role not in VISIBLE:
        raise HTTPException(422, "You can add employees and owners.")
    secret = b.pin if b.role == "employee" else b.password
    if not secret:
        raise HTTPException(422, "An employee needs a PIN." if b.role == "employee" else "An owner needs a password.")
    with transaction():
        return {"id": create_user(b.username, b.full_name, b.role, secret)}


@router.post("/users/{uid}/update")
def rename(uid: int, b: NameIn):
    """Changes a person's display name and/or username. History keeps the name they had at the time."""
    with transaction():
        r = _target(uid)
        if b.full_name is not None and b.full_name.strip() != r["full_name"]:
            db.execute("UPDATE users SET full_name=? WHERE id=?", (b.full_name.strip(), uid))
            audit("user.updated", "user", r["username"], f"{r['username']}: name {r['full_name']!r} → {b.full_name.strip()!r}")
        if b.username is not None and b.username.strip() != r["username"]:
            new = check_username(b.username)
            if db.execute("SELECT 1 FROM users WHERE lower(username)=lower(?) AND id<>?", (new, uid)).fetchone():
                raise HTTPException(409, f'The name "{new}" is already taken.')
            db.execute("UPDATE users SET username=? WHERE id=?", (new, uid))
            audit("user.renamed", "user", new, f"Username changed: {r['username']} → {new}", {"old": r["username"], "new": new})
    return {"ok": True}


@router.post("/users/{uid}/password")
def reset_password(uid: int, b: PwIn):
    me = context.current_user()
    if uid == me["id"]:
        raise HTTPException(409, "To change your own password use Account → Change my password.")
    with transaction():
        r = _target(uid, ("owner",))
        db.execute("UPDATE users SET pw_hash=?, must_change=0, failed_logins=0, locked_until=NULL WHERE id=?", (hash_password(check_password(b.new_password, r["username"])), uid))
        db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))                 # their old password and open logins stop at once
        audit("user.password_reset", "user", r["username"], f"Password of {r['username']} reset (it is their password as typed)")
    return {"ok": True}


@router.post("/users/{uid}/pin")
def set_pin(uid: int, b: PinIn):
    with transaction():
        r = _target(uid, ("employee",))
        db.execute("UPDATE users SET pin_hash=?, pin_failed=0, pin_locked_until=NULL WHERE id=?", (hash_pin(check_pin(b.new_pin)), uid))
        db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))                 # a changed PIN signs them out everywhere
        audit("user.pin_reset", "user", r["username"], f"PIN of {r['username']} set by an owner")
    return {"ok": True}


@router.post("/users/{uid}/active")
def set_active(uid: int, b: ActiveIn):
    me = context.current_user()
    with transaction():
        r = _target(uid)
        if bool(r["active"]) == b.active:
            return {"ok": True}
        if not b.active:
            if uid == me["id"]:
                raise HTTPException(409, "You can't deactivate yourself.")
            if r["role"] == "owner":
                db.execute("SELECT id FROM users WHERE role='owner' AND active=1 FOR UPDATE")
                if db.execute("SELECT COUNT(*) FROM users WHERE role='owner' AND active=1 AND id<>?", (uid,)).fetchone()[0] == 0:
                    raise HTTPException(409, "There must always be at least one active owner.")
        db.execute("UPDATE users SET active=? WHERE id=?", (1 if b.active else 0, uid))
        if not b.active:
            db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        audit("user.activated" if b.active else "user.deactivated", "user", r["username"], f"{r['username']} {'reactivated' if b.active else 'deactivated'}")
    return {"ok": True}


@router.post("/users/{uid}/remove")
def remove_employee(uid: int):
    """Removes an employee for good: login and PIN wiped, name freed, gone from every list. Their past sales, stock
    movements and History stay (they keep the name written at the time). Not for owners; not while a shift is open."""
    with transaction():
        r = _target(uid, ("employee",))
        if db.execute("SELECT 1 FROM shifts WHERE user_id=? AND closed_ts IS NULL", (uid,)).fetchone():
            raise HTTPException(409, "This employee has a shift open. Close the shift first, then remove them.")
        db.execute("UPDATE users SET removed=1, active=0, pin_hash=NULL, pw_hash=NULL, full_name='', username=?, failed_logins=0, locked_until=NULL, "
                   "pin_failed=0, pin_locked_until=NULL WHERE id=?", (f"removed-{uid}", uid))
        db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        audit("user.removed", "user", r["username"], f"Employee {r['username']} removed", {"full_name": r["full_name"]})
    return {"ok": True}


@router.post("/users/{uid}/unlock")
def unlock(uid: int):
    with transaction():
        r = _target(uid)
        db.execute("UPDATE users SET failed_logins=0, locked_until=NULL, pin_failed=0, pin_locked_until=NULL WHERE id=?", (uid,))
        audit("user.unlocked", "user", r["username"], f"{r['username']} unlocked")
    return {"ok": True}


@router.get("/security/policy")
def permission_table():
    out = []
    for key, (role, pin, label) in policy.POLICY.items():
        method, path = key.split(" ", 1)
        out.append({"method": method, "path": path, "role": role, "pin": pin, "label": label})
    return {"rules": out, "lockout": {"password_tries": MAX_FAILS, "password_minutes": LOGIN_LOCK_MIN, "pin_tries": MAX_FAILS, "pin_minutes": PIN_LOCK_MIN},
            "session_hours": SESSION_HOURS}
