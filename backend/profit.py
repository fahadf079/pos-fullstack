"""
profit.py — profit report for the owner: what the shop earned on the goods it sold, using what it paid for them.

  revenue (net)  = what customers paid, without tax, minus what was refunded (also without tax)
  cost of goods  = the cost of every unit sold minus the cost of every unit that came back in a refund
  gross profit   = revenue − cost of goods;   after expenses = gross profit − expenses (voided ones excluded)

Cost per item: since v12 a sale remembers the item's cost AT THE TIME of the sale (hidden from employees). Older sales have
no such memory, so the item's CURRENT cost is used and the report says how many lines were estimated that way, and how many
had no cost at all (cost 0, so their profit is overstated). Also shows the stock on the shelves valued at cost and at price.
"""
import datetime as dt
from typing import Optional

from fastapi import APIRouter, HTTPException

import database
from database import db, jl, transaction

router = APIRouter()


def _day(v: Optional[str], default: str) -> str:
    try:
        return dt.date.fromisoformat((v or default).strip()).isoformat()
    except ValueError:
        raise HTTPException(422, "Dates must look like 2026-10-05.")


def _r2(x) -> float:
    return round(float(x) + 1e-9, 2)


@router.get("/reports/profit")
def profit_report(date_from: Optional[str] = None, date_to: Optional[str] = None):
    today = database.today()
    d2 = _day(date_to, today)
    d1 = _day(date_from, (dt.date.fromisoformat(d2) - dt.timedelta(days=29)).isoformat())
    if d1 > d2:
        raise HTTPException(422, "The start date is after the end date.")
    with transaction(snapshot=True):
        cur_cost = {r["id"]: float(r["cost"] or 0) for r in db.execute("SELECT id, cost FROM products").fetchall()}
        sales = db.execute("SELECT id, total, tax, items FROM sales WHERE ts::date BETWEEN ?::date AND ?::date", (d1, d2)).fetchall()
        revenue = _r2(sum(float(s["total"]) - float(s["tax"]) for s in sales))
        refunds = db.execute("SELECT r.sale_id, r.net, r.items FROM refunds r JOIN sales s ON s.id=r.sale_id WHERE s.ts::date BETWEEN ?::date AND ?::date", (d1, d2)).fetchall()
        refunded_net = _r2(sum(float(r["net"] or 0) for r in refunds))
        # legacy whole-sale refunds (before per-item refunds existed) have a refunded flag but no refund rows
        legacy = db.execute("SELECT id, refunded_net, items FROM sales WHERE ts::date BETWEEN ?::date AND ?::date AND refunded=1 AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.sale_id=sales.id)", (d1, d2)).fetchall()
        refunded_net = _r2(refunded_net + sum(float(r["refunded_net"] or 0) for r in legacy))
        snap_cost: dict[tuple[int, int], float] = {}
        cogs = estimated = no_cost = 0.0
        est_lines = zero_lines = 0
        for s in sales:
            for i in jl(s["items"]):
                if "cost" in i and i["cost"] is not None:
                    c = float(i["cost"])
                else:
                    c = cur_cost.get(i["id"], 0.0); est_lines += 1
                if c <= 0: zero_lines += 1
                snap_cost[(s["id"], i["id"])] = c
                cogs += c * float(i["qty"])
        returned = 0.0
        for r in refunds:
            for i in jl(r["items"]):
                returned += snap_cost.get((r["sale_id"], i["id"]), cur_cost.get(i["id"], 0.0)) * float(i["qty"])
        for r in legacy:
            for i in jl(r["items"]):
                returned += snap_cost.get((r["id"], i["id"]), cur_cost.get(i["id"], 0.0)) * float(i["qty"])
        net_rev = _r2(revenue - refunded_net)
        cost_of_goods = _r2(cogs - returned)
        gross = _r2(net_rev - cost_of_goods)
        expenses = _r2(db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE ts::date BETWEEN ?::date AND ?::date AND voided=0", (d1, d2)).fetchone()[0])
        stock = db.execute("SELECT COALESCE(SUM(cost*stock),0) AS at_cost, COALESCE(SUM(price*stock),0) AS at_price FROM products WHERE active=1").fetchone()
        top = {}
        for s in sales:
            for i in jl(s["items"]):
                c = snap_cost[(s["id"], i["id"])]
                e = top.setdefault(i["id"], {"name": i["name"], "qty": 0.0, "revenue": 0.0, "cost": 0.0})
                e["qty"] += float(i["qty"]); e["revenue"] += float(i["price"]) * float(i["qty"]); e["cost"] += c * float(i["qty"])
        best = sorted(({"name": e["name"], "qty": round(e["qty"], 3), "revenue": _r2(e["revenue"]), "profit": _r2(e["revenue"] - e["cost"])} for e in top.values()), key=lambda x: -x["profit"])[:10]
    return {"from": d1, "to": d2, "sales_count": len(sales), "revenue_net": net_rev, "cost_of_goods": cost_of_goods, "gross_profit": gross,
            "margin_percent": round(gross * 100 / net_rev, 1) if net_rev > 0 else None, "expenses": expenses, "profit_after_expenses": _r2(gross - expenses),
            "estimated_cost_lines": est_lines, "lines_without_cost": zero_lines,
            "stock_value_at_cost": _r2(stock["at_cost"]), "stock_value_at_price": _r2(stock["at_price"]), "best_items": best,
            "note": "Sale prices of items sold are shown before discount; revenue and profit use what was really paid (no tax, minus refunds)."}
