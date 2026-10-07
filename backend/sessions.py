"""
sessions.py — logins that expire, and the 1-minute inactivity LOCK.

  * A session is a random token in an HttpOnly cookie; the database keeps only its SHA-256.
  * Lock = the screen is covered and the server refuses everything (423) until the SAME person signs in again
    (employee: PIN, owner/developer: password). The cart and the sale are not touched: only the access is.
  * Who locks it?  (1) the screen after 60 s without a key, tap or scan; (2) the server itself, if it has heard
    nothing for 60 s + a 20 s grace (tab closed, browser frozen, a modified front end). Every POST (a scan, a
    checkout, …) and the screen's activity ping count as activity; passive reads and live updates do not.
  * Guests have no PIN or password, so they are not locked: their session just ends after 15 minutes.
"""
import hashlib
import os
import secrets
from typing import Optional

from fastapi import Request, Response

import database
from database import audit, db, transaction

COOKIE = "pos_session"
SESSION_HOURS = float(os.environ.get("POS_SESSION_HOURS", "12"))
LOCK_SECONDS = int(os.environ.get("POS_LOCK_SECONDS", "60"))
LOCK_GRACE = int(os.environ.get("POS_LOCK_GRACE", "20"))
GUEST_MINUTES = 15


def sha(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def start(response: Response, request: Request, user_id: int, minutes: Optional[float] = None) -> None:
    tok = secrets.token_urlsafe(32)
    secs = (minutes * 60) if minutes else SESSION_HOURS * 3600
    db.execute("DELETE FROM sessions WHERE expires_ts < now()")
    db.execute("INSERT INTO sessions(token_hash, user_id, expires_ts, ip, agent) VALUES (?,?, now() + make_interval(secs => ?::float8), ?, ?)",
               (sha(tok), user_id, secs, (request.client.host if request.client else "")[:64], request.headers.get("user-agent", "")[:200]))
    response.set_cookie(COOKIE, tok, max_age=int(secs), httponly=True, samesite="lax", path="/", secure=os.environ.get("POS_COOKIE_SECURE") == "1")   # POS_COOKIE_SECURE=1 once the POS is served over HTTPS


def resolve(token: Optional[str]) -> Optional[dict]:
    """Cookie -> the active person, or None. Role and active-status are read fresh on EVERY request, so deactivating
    someone takes effect on their next click. Also applies the server-side idle lock."""
    if not token:
        return None
    h = sha(token)
    with transaction():
        r = db.execute("SELECT u.id, u.username, u.full_name, u.role, u.totp_enabled, s.locked, "
                       "(s.last_input_ts < now() - make_interval(secs => ?::float8)) AS idle "
                       "FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_ts > now() AND u.active=1",
                       (LOCK_SECONDS + LOCK_GRACE, h)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["locked"] = bool(d["locked"])
        if d["idle"] and not d["locked"] and d["role"] != "guest":
            db.execute("UPDATE sessions SET locked=1 WHERE token_hash=?", (h,))
            audit("auth.auto_locked", "user", d["username"], f"{d['username']}: screen locked by the server after {LOCK_SECONDS}s without activity",
                  actor=d["username"], user_id=d["id"])
            d["locked"] = True
        d.pop("idle", None)
    d["token_hash"] = h
    return d


def touch(token_hash: str) -> None:
    """Record real activity (not for a locked session: only unlocking can wake it)."""
    with transaction():
        db.execute("UPDATE sessions SET last_input_ts=now(), last_seen_ts=now() WHERE token_hash=? AND locked=0", (token_hash,))


def set_locked(token_hash: str, locked: bool) -> None:
    db.execute("UPDATE sessions SET locked=?, last_input_ts=now() WHERE token_hash=?", (1 if locked else 0, token_hash))


def public(u) -> dict:
    return {"id": u["id"], "username": u["username"], "full_name": u["full_name"], "role": u["role"],
            "two_factor": bool(u.get("totp_enabled", False))}
