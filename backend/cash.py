"""
cash.py — the cash drawer and end-of-shift cash count.

Drawer
  * It opens only for a reason the system recorded: a CASH sale (automatically, after the sale is really saved) or a
    manual opening (owner always; an employee only if the owner allowed it in Settings, with their PIN and a reason).
  * Every opening is a row in drawer_events (append-only) with who and why.
  * The hardware side is a plug-in: POS_DRAWER_DRIVER=none (default: record only) or =command with
    POS_DRAWER_COMMAND='["path\\to\\open.exe","arg"]' (a program that pulses your drawer). NOT tested with a real drawer.

Shifts (cash-up)
  * An employee opens a shift with the opening float, and closes it by typing the cash they COUNTED. The server only
    then works out the expected cash (float + cash sales − refunds of cash sales, by that employee, in the shift),
    so the employee can never see the expected figure first or while counting.
  * The difference goes to the owner: GET /shifts (owner) shows expected and difference; the employee's screen only
    says "submitted". A difference above the threshold raises an alert (no figures in its text) until an owner reviews it.
  * Settings: require_shift (cash sales need an open shift), employee_manual_drawer, variance_threshold.
"""
import json
import os
import re
import subprocess
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import alerts
import context
import database
import settings
from database import audit, db, transaction

router = APIRouter()


def _pulse() -> tuple[bool, str]:
    drv = os.environ.get("POS_DRAWER_DRIVER", "none")
    if drv == "none":
        return True, "no drawer driver configured: recorded only"
    if drv == "command":
        try:
            cmd = json.loads(os.environ.get("POS_DRAWER_COMMAND", "[]"))
            subprocess.run(cmd, timeout=5, check=True, capture_output=True)
            return True, "command ran"
        except Exception as e:
            return False, f"drawer command failed: {type(e).__name__}"
    return False, f"unknown drawer driver {drv!r}"


def _record(kind: str, sale_id=None, reason: str = "") -> None:
    """Writes the drawer event now (inside the caller's transaction); pulses the hardware only after the commit."""
    uid, who = database.actor_pair()
    eid = db.execute("INSERT INTO drawer_events(user_id, actor, kind, sale_id, reason) VALUES (?,?,?,?,?)", (uid, who, kind, sale_id, reason)).lastrowid

    def go():
        ok, info = _pulse()
        with transaction() as c:
            c.execute("UPDATE drawer_events SET ok=?, detail=? WHERE id=?", (1 if ok else 0, info, eid))
    database.after_commit(go)


def open_for_sale(sale_id: int) -> None:
    _record("sale", sale_id=sale_id, reason="cash sale")


def require_shift_for_cash() -> None:
    if settings.get("require_shift"):
        uid, _ = database.actor_pair()
        if not db.execute("SELECT 1 FROM shifts WHERE user_id=? AND closed_ts IS NULL", (uid,)).fetchone():
            raise HTTPException(409, "Open your cash-drawer shift (Cash up) before taking cash.")


class ReasonIn(BaseModel):
    reason: str = Field(max_length=200)


@router.post("/drawer/open")
def manual_open(b: ReasonIn):
    me = context.current_user()
    if me["role"] == "employee" and not settings.get("employee_manual_drawer"):
        raise HTTPException(403, "Only the owner can open the cash drawer by hand.")
    reason = b.reason.strip()
    if len(reason) < 3:
        raise HTTPException(422, "Give a reason (at least 3 letters).")
    with transaction():
        _record("manual", reason=reason)
        audit("drawer.manual_open", "drawer", "", f"Cash drawer opened by hand: {reason}", {"reason": reason})
    return {"ok": True}


@router.get("/drawer/events")
def drawer_events(limit: int = 100):
    with transaction(snapshot=True):
        return {"events": [dict(r, ok=bool(r["ok"])) for r in db.execute("SELECT * FROM drawer_events ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 500),)).fetchall()]}


# ───────────── shifts ─────────────
class OpenIn(BaseModel):
    opening_float: float = Field(ge=0, le=10_000_000)


class CloseIn(BaseModel):
    counted_cash: float = Field(ge=0, le=100_000_000)
    note: str = Field("", max_length=200)


class ReviewIn(BaseModel):
    note: str = Field("", max_length=200)


def _expected(shift) -> float:
    # the window is defined by record numbers (exact), not by clock times (which are only stored to the second)
    cash_in = db.execute("SELECT COALESCE(SUM(total),0) FROM sales WHERE user_id=? AND payment='Cash' AND id > ?", (shift["user_id"], shift["open_sale_id"])).fetchone()[0]
    # a refund took cash out of the drawer only when the money went back as Cash (paid_via; older refunds: the way the sale was paid)
    cash_out = db.execute("SELECT COALESCE(SUM(r.amount),0) FROM refunds r JOIN sales s ON s.id=r.sale_id WHERE r.user_id=? AND COALESCE(r.paid_via, s.payment)='Cash' AND r.id > ?",
                          (shift["user_id"], shift["open_refund_id"])).fetchone()[0]
    mv = {r["kind"]: float(r["t"]) for r in db.execute("SELECT kind, SUM(amount) AS t FROM shift_moves WHERE shift_id=? GROUP BY kind", (shift["id"],)).fetchall()}
    return round(float(shift["opening_float"]) + float(cash_in) - float(cash_out) - mv.get("drop", 0) - mv.get("payout", 0) + mv.get("add", 0) + 1e-9, 2)


@router.get("/shifts/current")
def current_shift():
    me = context.current_user()
    with transaction(snapshot=True):
        s = db.execute("SELECT id, opened_ts, opening_float FROM shifts WHERE user_id=? AND closed_ts IS NULL", (me["id"],)).fetchone()
    return {"shift": dict(s) if s else None, "required": bool(settings.get("require_shift"))}      # never the expected cash


@router.post("/shifts/open")
def open_shift(b: OpenIn):
    me = context.current_user()
    with transaction():
        db.execute("SELECT id FROM users WHERE id=? FOR UPDATE", (me["id"],))
        if db.execute("SELECT 1 FROM shifts WHERE user_id=? AND closed_ts IS NULL", (me["id"],)).fetchone():
            raise HTTPException(409, "You already have an open shift.")
        sid = db.execute("INSERT INTO shifts(user_id, actor, opening_float, open_sale_id, open_refund_id) VALUES (?,?,?,(SELECT COALESCE(MAX(id),0) FROM sales),(SELECT COALESCE(MAX(id),0) FROM refunds))",
                         (me["id"], me["username"], round(b.opening_float, 2))).lastrowid
        audit("shift.opened", "shift", sid, f"Shift #{sid} opened by {me['username']} with a float of Rs {b.opening_float:g}", {"opening_float": b.opening_float})
    return {"ok": True, "id": sid}


class MoveIn(BaseModel):
    kind: str
    amount: float = Field(gt=0, le=10_000_000)
    reason: str = Field(max_length=200)


KINDS = {"drop": "Cash drop (taken out to the safe)", "payout": "Cash payout (paid out of the drawer)", "add": "Cash added to the drawer"}


@router.post("/shifts/move")
def cash_move(b: MoveIn):
    """Money moved in or out of the drawer in the middle of a shift (a drop to the safe, a payout, extra change put in).
    It is part of the shift's expected cash. The employee confirms with their own PIN; the response never shows expected cash."""
    me = context.current_user()
    if b.kind not in KINDS:
        raise HTTPException(422, "Kind must be drop, payout or add.")
    reason = b.reason.strip()
    if len(reason) < 3:
        raise HTTPException(422, "Give a reason (at least 3 letters).")
    amount = round(b.amount, 2)
    with transaction():
        db.execute("SELECT id FROM users WHERE id=? FOR UPDATE", (me["id"],))
        s = db.execute("SELECT id FROM shifts WHERE user_id=? AND closed_ts IS NULL FOR UPDATE", (me["id"],)).fetchone()
        if not s:
            raise HTTPException(409, "Open your cash-drawer shift first.")
        mid = db.execute("INSERT INTO shift_moves(shift_id, user_id, actor, kind, amount, reason) VALUES (?,?,?,?,?,?)", (s["id"], me["id"], me["username"], b.kind, amount, reason)).lastrowid
        audit("shift.move", "shift", s["id"], f"{me['username']}: {KINDS[b.kind]} Rs {amount:g}: {reason}", {"kind": b.kind, "amount": amount, "reason": reason, "move_id": mid})
    return {"ok": True}


@router.post("/shifts/close")
def close_shift(b: CloseIn):
    me = context.current_user()
    with transaction():
        db.execute("SELECT id FROM users WHERE id=? FOR UPDATE", (me["id"],))          # no sale or refund by this person can slip in unseen
        s = db.execute("SELECT * FROM shifts WHERE user_id=? AND closed_ts IS NULL FOR UPDATE", (me["id"],)).fetchone()
        if not s:
            raise HTTPException(409, "You have no open shift.")
        exp = _expected(s)
        counted = round(b.counted_cash, 2)
        var = round(counted - exp, 2)
        db.execute("UPDATE shifts SET closed_ts=now(), counted_cash=?, expected_cash=?, variance=?, note=? WHERE id=?", (counted, exp, var, b.note.strip(), s["id"]))
        audit("shift.closed", "shift", s["id"], f"Shift #{s['id']} closed by {me['username']}: counted Rs {counted:g}, expected Rs {exp:g}, difference Rs {var:g}",
              {"counted": counted, "expected": exp, "variance": var, "note": b.note.strip()})
        if abs(var) > float(settings.get("variance_threshold")):
            alerts.raise_alert(f"cash.variance.{s['id']}", "warning", f"Cash difference on shift #{s['id']} needs the owner's review",
                               "The counted cash does not match. The owner can see the figures in Cash up.")
    return {"ok": True, "submitted": True}               # deliberately nothing about expected cash or the difference


@router.get("/shifts")
def list_shifts(limit: int = 50):
    with transaction(snapshot=True):
        rows = db.execute("SELECT * FROM shifts ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 300),)).fetchall()
        out = []
        for r in rows:
            mv = {m["kind"]: float(m["t"]) for m in db.execute("SELECT kind, SUM(amount) AS t FROM shift_moves WHERE shift_id=? GROUP BY kind", (r["id"],)).fetchall()}
            out.append(dict(r, open=r["closed_ts"] is None, drops=mv.get("drop", 0.0), payouts=mv.get("payout", 0.0), added=mv.get("add", 0.0)))
        return {"shifts": out, "threshold": settings.get("variance_threshold")}


@router.get("/shifts/moves")
def list_moves(limit: int = 100):
    with transaction(snapshot=True):
        return {"moves": [dict(r) for r in db.execute("SELECT * FROM shift_moves ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 500),)).fetchall()]}


@router.get("/reports/day")
def day_report(date: Optional[str] = None):
    """End-of-day report for the owner: takings by payment method, refunds by how they were paid back, every shift closed that
    day with its drops/payouts, and what the drawers should hold. One page to compare with the cash actually banked."""
    d = (date or database.today()).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        raise HTTPException(422, "Date must look like 2026-10-05.")
    try:
        import datetime as _dt
        _dt.date.fromisoformat(d)
    except ValueError:
        raise HTTPException(422, "That date does not exist.")
    with transaction(snapshot=True):
        sales = db.execute("SELECT payment, COUNT(*) AS n, COALESCE(SUM(total),0) AS t FROM sales WHERE ts::date = ?::date GROUP BY payment ORDER BY payment", (d,)).fetchall()
        refs = db.execute("SELECT COALESCE(r.paid_via, s.payment) AS via, COUNT(*) AS n, COALESCE(SUM(r.amount),0) AS t FROM refunds r JOIN sales s ON s.id=r.sale_id "
                          "WHERE r.ts::date = ?::date GROUP BY 1 ORDER BY 1", (d,)).fetchall()
        shifts = db.execute("SELECT * FROM shifts WHERE closed_ts::date = ?::date ORDER BY id", (d,)).fetchall()
        still_open = db.execute("SELECT COUNT(*) FROM shifts WHERE closed_ts IS NULL").fetchone()[0]
        moves = db.execute("SELECT kind, COUNT(*) AS n, COALESCE(SUM(amount),0) AS t FROM shift_moves WHERE ts::date = ?::date GROUP BY kind", (d,)).fetchall()
        manual = db.execute("SELECT COUNT(*) FROM drawer_events WHERE kind='manual' AND ts::date = ?::date", (d,)).fetchone()[0]
        by_pay = {r["payment"]: {"sales": int(r["n"]), "sales_total": round(float(r["t"]), 2)} for r in sales}
        for r in refs:
            e = by_pay.setdefault(r["via"], {"sales": 0, "sales_total": 0.0}); e["refunds"] = int(r["n"]); e["refunds_total"] = round(float(r["t"]), 2)
        methods = []
        for k, e in sorted(by_pay.items()):
            e.setdefault("refunds", 0); e.setdefault("refunds_total", 0.0)
            methods.append({"method": k, **e, "net": round(e["sales_total"] - e["refunds_total"], 2)})
        sh = [{"id": r["id"], "actor": r["actor"], "opened_ts": r["opened_ts"], "closed_ts": r["closed_ts"], "opening_float": float(r["opening_float"]), "counted_cash": float(r["counted_cash"]),
               "expected_cash": float(r["expected_cash"]), "variance": float(r["variance"])} for r in shifts]
        return {"date": d, "methods": methods,
                "sales_total": round(sum(m["sales_total"] for m in methods), 2), "refunds_total": round(sum(m["refunds_total"] for m in methods), 2),
                "net_total": round(sum(m["net"] for m in methods), 2),
                "shifts": sh, "shifts_open_now": int(still_open),
                "counted_total": round(sum(x["counted_cash"] for x in sh), 2), "expected_total": round(sum(x["expected_cash"] for x in sh), 2),
                "variance_total": round(sum(x["variance"] for x in sh), 2),
                "moves": {m["kind"]: {"count": int(m["n"]), "total": round(float(m["t"]), 2)} for m in moves}, "manual_drawer_opens": int(manual)}


@router.post("/shifts/{sid}/review")
def review(sid: int, b: ReviewIn):
    me = context.current_user()
    with transaction():
        s = db.execute("SELECT * FROM shifts WHERE id=?", (sid,)).fetchone()
        if not s or s["closed_ts"] is None:
            raise HTTPException(404, "No such closed shift.")
        audit("shift.reviewed", "shift", sid, f"{me['username']} reviewed the cash difference of shift #{sid}" + (f": {b.note.strip()}" if b.note.strip() else ""), {"note": b.note.strip()})
        alerts.resolve_alert(f"cash.variance.{sid}")
    return {"ok": True}
