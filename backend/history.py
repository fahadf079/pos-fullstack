"""
history.py — the History tab's API: a searchable, filterable, read-only view of the append-only audit_log.

Every business event (sale, refund, stock adjustment, price/name/unit change, purchase order, delivery,
supplier payment, expense, supplier edit) is written to audit_log by the same transaction that makes the
change. The database itself refuses UPDATE / DELETE / TRUNCATE on audit_log, so history can't be rewritten
through the app. (Each row records WHO did it, and who approved it when a manager's PIN approved a cashier's action.)
"""
from typing import Optional

from fastapi import APIRouter, Query

from database import db, jl, transaction

router = APIRouter()


def _where(category, q, frm, to):
    w, a = [], []
    if category:
        w.append("split_part(action, '.', 1) = ?"); a.append(category)
    if q and q.strip():
        like = f"%{q.strip()}%"
        w.append("(summary ILIKE ? OR entity_id ILIKE ? OR actor ILIKE ? OR action ILIKE ? OR approved_by ILIKE ?)"); a += [like] * 5
    if frm:
        w.append("ts::date >= ?::date"); a.append(frm)
    if to:
        w.append("ts::date <= ?::date"); a.append(to)
    return (" WHERE " + " AND ".join(w)) if w else "", a


@router.get("/history/meta")
def history_meta():
    with transaction(snapshot=True):
        rows = db.execute("SELECT split_part(action, '.', 1) AS category, COUNT(*) AS n FROM audit_log GROUP BY 1 ORDER BY 1").fetchall()
    return {"categories": [{"category": r["category"], "count": r["n"]} for r in rows]}


@router.get("/history")
def history(category: Optional[str] = None, q: Optional[str] = None,
            frm: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
            limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    where, args = _where(category, q, frm, to)
    with transaction(snapshot=True):
        total = db.execute("SELECT COUNT(*) FROM audit_log" + where, args).fetchone()[0]
        rows = db.execute("SELECT id, ts, user_id, actor, approved_by, action, entity, entity_id, summary, details FROM audit_log"
                          + where + " ORDER BY id DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    return {"total": total, "entries": [dict(r, details=jl(r["details"]) if r["details"] is not None else None) for r in rows]}
