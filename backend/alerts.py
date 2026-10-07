"""
alerts.py — critical alerts that cannot be ignored, and the background checker that raises them.

Rules (from the requirements):
  * A CRITICAL alert needs an explicit acknowledgement: who and when are recorded (and written to the History).
  * An alert stays ACTIVE until the underlying problem is really fixed: acknowledging it does not hide it.
  * It then also stays visible until a critical one has been acknowledged, so nobody can miss that it happened.
  * Alerts are visible to every signed-in employee, so their text never contains money figures or private detail.

The checker (monitor_once) runs every 10 seconds inside the backend:
    network      the approved connection is present on this computer           (critical, after 2 failures in a row)
    protection   network protection has been switched off by a developer       (warning)
    backup       the newest backup is older than 36 hours (only once backups exist)  (warning)
Other modules raise alerts too (cash.py: a cash difference above the limit).
"""
import asyncio
import os
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

import context
import database
import netpolicy
import settings
from database import audit, db, transaction

router = APIRouter()
VERSION = 0                          # bumped after every change; the live stream announces it first (high priority)
INTERVAL = float(os.environ.get("POS_MONITOR_SECONDS", "10"))
DEBOUNCE = int(os.environ.get("POS_MONITOR_DEBOUNCE", "2"))
_streak: dict[str, int] = {}
STATE: dict = {"last_run": None, "last_error": None}


def _changed() -> None:
    global VERSION
    VERSION += 1
    database._bump_now()


def raise_alert(key: str, severity: str, title: str, detail: str = "") -> int:
    with transaction():
        r = db.execute("SELECT id FROM alerts WHERE key=? AND resolved_ts IS NULL FOR UPDATE", (key,)).fetchone()
        if r:
            db.execute("UPDATE alerts SET last_seen_ts=now(), times=times+1, detail=? WHERE id=?", (detail, r["id"]))
            return r["id"]
        aid = db.execute("INSERT INTO alerts(key, severity, title, detail) VALUES (?,?,?,?)", (key, severity, title, detail)).lastrowid
        audit("alert.raised", "alert", aid, f"{severity.upper()} alert: {title}", {"key": key, "detail": detail}, actor="system")
        database.after_commit(_changed)
        return aid


def resolve_alert(key: str) -> None:
    with transaction():
        r = db.execute("UPDATE alerts SET resolved_ts=now() WHERE key=? AND resolved_ts IS NULL RETURNING id, title", (key,)).fetchone()
        if r:
            audit("alert.resolved", "alert", r["id"], f"Alert resolved: {r['title']}", {"key": key}, actor="system")
            database.after_commit(_changed)


def active() -> list[dict]:
    with transaction(snapshot=True):
        rows = db.execute("SELECT id, key, severity, title, detail, raised_ts, last_seen_ts, times, resolved_ts, ack_ts, ack_by FROM alerts "
                          "WHERE resolved_ts IS NULL OR (severity='critical' AND ack_ts IS NULL) "
                          "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, raised_ts").fetchall()
    return [dict(r, still_active=r["resolved_ts"] is None, needs_ack=r["severity"] == "critical" and r["ack_ts"] is None) for r in rows]


@router.get("/alerts")
def list_alerts():
    return {"alerts": active(), "version": VERSION}


@router.post("/alerts/{aid}/ack")
def acknowledge(aid: int):
    me = context.current_user()
    with transaction():
        r = db.execute("SELECT * FROM alerts WHERE id=? FOR UPDATE", (aid,)).fetchone()
        if not r:
            raise HTTPException(404, "No such alert.")
        if r["ack_ts"] is None:
            db.execute("UPDATE alerts SET ack_ts=now(), ack_by=?, ack_user_id=? WHERE id=?", (me["username"], me["id"], aid))
            audit("alert.acknowledged", "alert", aid, f"{me['username']} acknowledged: {r['title']}" + ("" if r["resolved_ts"] else " (the problem is still active)"),
                  {"key": r["key"], "still_active": r["resolved_ts"] is None})
            database.after_commit(_changed)
    return {"ok": True}


# ───────────── the checker ─────────────
def _streak_hit(name: str, bad: bool, need: int) -> bool:
    _streak[name] = _streak.get(name, 0) + 1 if bad else 0
    return _streak[name] >= need


def monitor_once(debounce: int | None = None) -> None:
    need = DEBOUNCE if debounce is None else debounce
    try:
        miss = netpolicy.missing_interfaces()
        if _streak_hit("net", bool(miss), need):
            raise_alert("network.interface_down", "critical", "The approved network connection is down",
                        f"This computer no longer has the approved network address ({', '.join(miss)}). The cable may be unplugged, Wi-Fi switched off, "
                        "or the address changed. The owner can choose the current connection in Settings → Network.")
        elif not miss:
            resolve_alert("network.interface_down")
        if not settings.get("network").get("enforce", True):
            raise_alert("network.protection_off", "warning", "Network protection is switched off", "A developer switched it off for maintenance. It should be switched back on.")
        else:
            resolve_alert("network.protection_off")
        dumps = list((Path(__file__).resolve().parent.parent / "backups").glob("*.dump"))
        if dumps and time.time() - max(f.stat().st_mtime for f in dumps) > 36 * 3600:
            raise_alert("backup.stale", "warning", "No backup for more than 36 hours", "Run backup.bat, or check the scheduled backup task.")
        else:
            resolve_alert("backup.stale")
        STATE["last_error"] = None
    except Exception as e:                                  # the checker must never take the POS down
        STATE["last_error"] = f"{type(e).__name__}: {e}"
    STATE["last_run"] = database.now()


async def monitor_loop() -> None:
    while True:
        await run_in_threadpool(monitor_once)
        await asyncio.sleep(INTERVAL)
