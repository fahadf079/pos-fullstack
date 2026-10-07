"""
pipeline.py — the ORDERED path every request takes. Nothing reaches an action before the stages in front of it.

   #  stage          priority  question it answers                                              refuses with
   1  network        HIGH      Is this caller on the shop network / the approved connection?    403 network_blocked
   2  identity       HIGH      Who is this (valid session, active person, X-POS header)?        401 / 403
   3  lock           HIGH      Is their screen locked? Then nothing but unlock / log out.       423 locked
   4  permission     HIGH      Is this role allowed to call this route (policy.POLICY)?         403
   5  confirmation   HIGH      Does the rule want the employee's own PIN? Is it right?          403 pin_* / 423
   6  action         LOW       The endpoint itself (scan, checkout, refund, …).                 —
   7  record         LOW       History line, stock ledger row, saved in the SAME transaction.   —

HIGH stages are safety gates and always run first, in this order. LOW stages are the business work. The same idea is
used for live messages: the stream announces an alert change BEFORE it announces ordinary stock/sales changes (main.py).
Tests switch TRACE on to prove the order (e.g. a locked session never reaches "permission").
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

import alerts
import context
import credentials
import database
import netpolicy
import policy
import sessions
import settings
from context import Ctx

router = APIRouter()
HIGH, LOW = "high", "low"
STAGES = [
    ("network", HIGH, "Is the caller on the shop network / approved connection?"),
    ("identity", HIGH, "Who is this? Valid session, active person, X-POS header."),
    ("lock", HIGH, "Locked screen? Only unlock / log out are allowed."),
    ("permission", HIGH, "Is this role allowed to call this route?"),
    ("confirmation", HIGH, "Does the rule need the employee's own PIN, and is it right?"),
    ("action", LOW, "The business action itself."),
    ("record", LOW, "History + ledger, in the same transaction."),
]
TRACE: Optional[list] = None          # tests set this to a list


def _t(name: str) -> None:
    if TRACE is not None:
        TRACE.append(name)


async def guard(request: Request) -> None:
    """App-wide dependency: runs stages 1–5 before any endpoint."""
    context.set_ctx(None)
    route = request.scope.get("route")
    key = f"{request.method} {getattr(route, 'path', request.url.path)}"

    _t("network")                                                         # 1
    await run_in_threadpool(netpolicy.check, request)
    if key in policy.PUBLIC:
        return

    _t("identity")                                                        # 2
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-pos") != "1":
        raise HTTPException(403, "Missing X-POS header (blocked: requests must come from the POS screen).")
    user = await run_in_threadpool(sessions.resolve, request.cookies.get(sessions.COOKIE))
    if user is None:
        raise HTTPException(401, "Please log in.")

    _t("lock")                                                            # 3
    if user["locked"] and key not in policy.LOCKED_OK:
        raise HTTPException(423, {"code": "locked", "message": "The screen is locked. Sign in again to continue.", "override": False})
    if request.method not in ("GET", "HEAD", "OPTIONS") and not user["locked"] and key not in policy.NO_TOUCH:
        await run_in_threadpool(sessions.touch, user["token_hash"])

    _t("permission")                                                      # 4
    pol = policy.POLICY.get(key)
    if pol is None:
        raise HTTPException(403, "No permission rule for this action.")            # fail closed
    role, pin_rule, _ = pol
    if credentials.RANK[user["role"]] < credentials.RANK[role]:
        raise HTTPException(403, f"Your role ({user['role']}) can't do this. It needs {role} or higher.")
    if (user["role"] == "owner" and not user.get("totp_enabled") and key not in policy.TWOFA_SETUP_OK and settings.get("force_owner_2fa")):
        raise HTTPException(403, {"code": "2fa_required", "message": "The shop requires two-step sign-in for owners. Set it up in Account first.", "override": False})
    context.set_ctx(Ctx(user, request.headers.get("x-pos-pin")))
    database.set_actor(user["id"], user["username"])

    _t("confirmation")                                                    # 5
    if pin_rule:
        await run_in_threadpool(credentials.confirm, pin_rule)


@router.get("/developer/diagnostics")
def diagnostics():
    return {"stages": [{"order": i + 1, "name": n, "priority": p, "question": q} for i, (n, p, q) in enumerate(STAGES)],
            "monitor": {**alerts.STATE, "interval_seconds": alerts.INTERVAL, "debounce": alerts.DEBOUNCE},
            "settings": settings.all_values(), "alert_version": alerts.VERSION, "live_version": database.get_version(),
            "lock": {"seconds": sessions.LOCK_SECONDS, "server_grace_seconds": sessions.LOCK_GRACE},
            "routes_with_rules": len(policy.POLICY)}
