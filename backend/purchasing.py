"""
purchasing.py — Purchasing, Suppliers, Expenses & Purchase Report for the POS.

Flow
  1. Create Purchase Order  -> status ORDERED, stock is NOT touched.
  2. Supplier delivers      -> Receive Stock: enter what actually arrived.
                               Stock goes up through the movements ledger.
  3. Each delivery becomes a Purchase (PUR-xxxxx). One PO can have several
     deliveries (partial), or be closed short.

Everything that changes stock happens inside ONE database transaction,
so a failure halfway leaves no half-received order.
"""
from datetime import date, datetime, timedelta
from typing import List, Optional

import psycopg
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import database
from database import audit, now as _now, tx, transaction

router = APIRouter()

# Injected by setup() from main.py, so purchasing shares main's move()
# (the single choke point for stock changes) and its live-update counter.
_move = None
_bump = lambda: None


def db(write: bool = False):
    """One real PostgreSQL transaction (joins the surrounding one): everything commits together,
    and any error rolls EVERYTHING back, so a failed receive leaves no half-updated stock.
    Reads get a consistent, read-only snapshot."""
    return transaction(snapshot=not write)


def _stock_in(c, product_id: int, qty: float, note: str) -> None:
    p = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if p is None:
        raise HTTPException(404, f"Product {product_id} not found")
    _move(dict(p), qty, "RECEIVE", note)   # main.move(): updates stock + writes the ledger row


EXPENSE_CATEGORIES = ["Rent", "Utilities", "Salaries", "Transport", "Maintenance",
                      "Packaging", "Marketing", "Taxes & Fees", "Other"]


def setup(move, bump) -> None:
    """Call once at startup from main.py. (Tables are created by database.init().)"""
    global _move, _bump
    _move, _bump = move, bump


# ───────────────────────────── helpers ─────────────────────────────
def _today() -> date:
    """Today in the SHOP's timezone (POS_TZ), the same clock every timestamp is stored in."""
    return date.fromisoformat(database.today())


def po_no(i: int) -> str: return f"PO-{i:05d}"
def pur_no(i: int) -> str: return f"PUR-{i:05d}"
def exp_no(i: int) -> str: return f"EXP-{i:05d}"
def money(x: float) -> float: return round(float(x) + 1e-9, 2)
def q3(x: float) -> float: return round(float(x), 3)   # quantities: up to 3 decimals (kg / l), pieces stay whole


def _check_qty(unit: str, qty: float, label: str) -> float:
    q = q3(qty)
    if q <= 0:
        raise HTTPException(422, f"{label}: quantity must be above 0")
    if unit == "pc" and abs(q - round(q)) > 1e-9:
        raise HTTPException(422, f"{label}: sold by the piece, so enter a whole number")
    return q


def _clean(s: Optional[str]) -> str:
    return (s or "").strip()


def _parse_day(s: Optional[str], default: date) -> date:
    if not s:
        return default
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, f"Bad date '{s}', use YYYY-MM-DD")


def _range(frm: Optional[str], to: Optional[str]):
    today = _today()
    d1 = _parse_day(frm, today - timedelta(days=29))
    d2 = _parse_day(to, today)
    if d1 > d2:
        raise HTTPException(422, "'from' is after 'to'")
    return d1.isoformat(), d2.isoformat()


def _po_row(conn, po_id: int, lock: bool = False):
    r = conn.execute(
        "SELECT p.*, s.name AS supplier FROM purchase_orders p JOIN suppliers s ON s.id=p.supplier_id WHERE p.id=?"
        + (" FOR UPDATE OF p" if lock else ""),
        (po_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "Purchase order not found")
    return r


def _po_dict(conn, r, with_items=True):
    items = conn.execute("SELECT * FROM po_items WHERE po_id=? ORDER BY id", (r["id"],)).fetchall()
    ordered_value = money(sum(i["ordered"] * i["unit_cost"] for i in items))
    d = dict(id=r["id"], po_no=po_no(r["id"]), supplier_id=r["supplier_id"], supplier=r["supplier"],
             ts=r["ts"], expected=r["expected"], status=r["status"], note=r["note"],
             closed_ts=r["closed_ts"], line_count=len(items), ordered_value=ordered_value,
             ordered_units=q3(sum(i["ordered"] for i in items)),
             received_units=q3(sum(i["received"] for i in items)))
    if with_items:
        d["items"] = [dict(id=i["id"], product_id=i["product_id"], sku=i["sku"], name=i["name"], cat=i["cat"],
                           sale_price=i["sale_price"], unit=i["unit"], ordered=i["ordered"], received=i["received"],
                           remaining=q3(i["ordered"] - i["received"]), unit_cost=i["unit_cost"],
                           is_new=i["product_id"] is None) for i in items]
        rc = conn.execute("SELECT * FROM receipts WHERE po_id=? ORDER BY id", (r["id"],)).fetchall()
        d["receipts"] = [dict(id=x["id"], pur_no=pur_no(x["id"]), ts=x["ts"], total=x["total"],
                              paid=bool(x["paid"])) for x in rc]
    return d


# ───────────────────────────── models ─────────────────────────────
class SupplierIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: str = Field(default="", max_length=40)
    note: str = Field(default="", max_length=300)


class SupplierPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    phone: Optional[str] = Field(default=None, max_length=40)
    note: Optional[str] = Field(default=None, max_length=300)
    active: Optional[bool] = None


class POLine(BaseModel):
    product_id: Optional[int] = None            # existing product…
    sku: Optional[str] = Field(default=None, max_length=40)      # …or a brand-new one:
    name: Optional[str] = Field(default=None, max_length=120)    #   sku + name + sale_price
    cat: str = Field(default="Grocery", max_length=40)
    unit: str = Field(default="pc", max_length=4)                 # new products only: pc | kg | l
    sale_price: float = Field(default=0, ge=0, le=10_000_000)    # new products must be > 0 (checked below)
    qty: float = Field(gt=0, le=1_000_000)
    unit_cost: float = Field(ge=0, le=10_000_000)


class POIn(BaseModel):
    supplier_id: int
    expected: str = Field(default="", max_length=10)
    note: str = Field(default="", max_length=300)
    lines: List[POLine] = Field(min_length=1, max_length=200)


class RecvLine(BaseModel):
    po_item_id: int
    qty: float = Field(ge=0, le=1_000_000)
    unit_cost: Optional[float] = Field(default=None, ge=0, le=10_000_000)  # actual invoice cost


class ReceiveIn(BaseModel):
    lines: List[RecvLine] = Field(min_length=1)
    close_short: bool = False    # true = supplier won't send the rest, close the PO
    paid: bool = False
    pay_method: str = Field(default="", max_length=20)
    note: str = Field(default="", max_length=300)


class PayIn(BaseModel):
    pay_method: str = Field(default="Cash", max_length=20)


class ExpenseIn(BaseModel):
    category: str = Field(min_length=1, max_length=40)
    amount: float = Field(gt=0, le=1_000_000_000)
    payee: str = Field(default="", max_length=80)
    note: str = Field(default="", max_length=300)
    date: Optional[str] = None   # YYYY-MM-DD, defaults to today


# ───────────────────────────── products list for the pickers ─────────────────────────────
@router.get("/purchasing/products")
def purchasing_products():
    with db() as c:
        rows = c.execute("SELECT id, sku, name, cat, price, stock, cost, unit FROM products WHERE active=1 ORDER BY lower(name)").fetchall()
    return [dict(r) for r in rows]


@router.get("/purchasing/meta")
def purchasing_meta():
    return {"expense_categories": EXPENSE_CATEGORIES}


# ───────────────────────────── suppliers ─────────────────────────────
@router.get("/suppliers")
def list_suppliers(include_inactive: bool = True):
    q = "SELECT * FROM suppliers" + ("" if include_inactive else " WHERE active=1") + " ORDER BY lower(name)"
    with db() as c:
        rows = c.execute(q).fetchall()
        out = []
        for r in rows:
            open_pos = c.execute("SELECT COUNT(*) FROM purchase_orders WHERE supplier_id=? AND status IN ('ORDERED','PARTIAL')",
                                 (r["id"],)).fetchone()[0]
            owed = c.execute("SELECT COALESCE(SUM(total),0) FROM receipts WHERE supplier_id=? AND paid=0",
                             (r["id"],)).fetchone()[0]
            out.append(dict(id=r["id"], name=r["name"], phone=r["phone"], note=r["note"],
                            active=bool(r["active"]), open_pos=open_pos, owed=money(owed)))
    return out


@router.post("/suppliers")
def add_supplier(b: SupplierIn):
    name = _clean(b.name)
    if not name:
        raise HTTPException(422, "Supplier name is required")
    try:
        with db(True) as c:
            cur = c.execute("INSERT INTO suppliers(name,phone,note) VALUES (?,?,?)",
                            (name, _clean(b.phone), _clean(b.note)))
            sid = cur.lastrowid
            audit("supplier.added", "supplier", sid, f"Supplier added: {name}", {"phone": _clean(b.phone), "note": _clean(b.note)})
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, f"Supplier '{name}' already exists")
    _bump()
    return {"id": sid, "name": name}


@router.post("/suppliers/{sid}/update")
def edit_supplier(sid: int, b: SupplierPatch):
    try:
        with db(True) as c:
            r = c.execute("SELECT * FROM suppliers WHERE id=? FOR UPDATE", (sid,)).fetchone()
            if r is None:
                raise HTTPException(404, "Supplier not found")
            name = _clean(b.name) if b.name is not None else r["name"]
            if not name:
                raise HTTPException(422, "Supplier name is required")
            c.execute("UPDATE suppliers SET name=?, phone=?, note=?, active=? WHERE id=?",
                      (name, _clean(b.phone) if b.phone is not None else r["phone"],
                       _clean(b.note) if b.note is not None else r["note"],
                       (1 if b.active else 0) if b.active is not None else r["active"], sid))
            new = dict(name=name, phone=_clean(b.phone) if b.phone is not None else r["phone"],
                       note=_clean(b.note) if b.note is not None else r["note"],
                       active=(1 if b.active else 0) if b.active is not None else r["active"])
            diff = {k: [r[k], v] for k, v in new.items() if r[k] != v}
            if diff:
                audit("supplier.updated", "supplier", sid, f"Supplier {r['name']}: " + ", ".join(f"{k} {o!r} → {n!r}" for k, (o, n) in diff.items()), diff)
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "Another supplier already has that name")
    _bump()
    return {"ok": True}


# ───────────────────────────── purchase orders ─────────────────────────────
@router.get("/purchase-orders")
def list_pos(status: Optional[str] = None, limit: int = Query(100, ge=1, le=500)):
    q = ("SELECT p.*, s.name AS supplier FROM purchase_orders p JOIN suppliers s ON s.id=p.supplier_id")
    args: list = []
    if status:
        q += " WHERE p.status=?"
        args.append(status.upper())
    q += " ORDER BY p.id DESC LIMIT ?"
    args.append(limit)
    with db() as c:
        return [_po_dict(c, r, with_items=False) for r in c.execute(q, args).fetchall()]


@router.get("/purchase-orders/{po_id}")
def get_po(po_id: int):
    with db() as c:
        return _po_dict(c, _po_row(c, po_id))


@router.post("/purchase-orders")
def create_po(b: POIn):
    if b.expected:
        _parse_day(b.expected, _today())
    with db(True) as c:
        s = c.execute("SELECT active FROM suppliers WHERE id=?", (b.supplier_id,)).fetchone()
        if s is None:
            raise HTTPException(404, "Supplier not found")
        if not s["active"]:
            raise HTTPException(409, "Supplier is inactive")

        prepared, seen_pid, seen_sku = [], set(), set()
        for n, ln in enumerate(b.lines, 1):
            if ln.product_id is not None:
                p = c.execute("SELECT id, sku, name, cat, price, unit, active FROM products WHERE id=?", (ln.product_id,)).fetchone()
                if p is None:
                    raise HTTPException(404, f"Line {n}: product {ln.product_id} not found")
                if not p["active"]:
                    raise HTTPException(409, f"Line {n}: '{p['name']}' is deactivated: reactivate it in Catalog first")
                if p["id"] in seen_pid:
                    raise HTTPException(422, f"Line {n}: '{p['name']}' is already on this order")
                seen_pid.add(p["id"])
                prepared.append((p["id"], p["sku"], p["name"], p["cat"], p["price"], _check_qty(p["unit"], ln.qty, f"Line {n}"), ln.unit_cost, p["unit"]))
            else:
                sku, name = _clean(ln.sku), _clean(ln.name)
                if not sku or not name:
                    raise HTTPException(422, f"Line {n}: pick a product, or give SKU and name for a new one")
                if ln.sale_price <= 0:
                    raise HTTPException(422, f"Line {n}: the selling price of a new product must be above 0")
                unit = _clean(ln.unit) or "pc"
                if unit not in ("pc", "kg", "l"):
                    raise HTTPException(422, f"Line {n}: unit must be pc, kg or l")
                if sku in seen_sku:
                    raise HTTPException(422, f"Line {n}: SKU {sku} appears twice on this order")
                seen_sku.add(sku)
                ex = c.execute("SELECT name FROM products WHERE sku=?", (sku,)).fetchone()
                if ex:
                    raise HTTPException(409, f"Line {n}: SKU {sku} already exists as '{ex['name']}' — pick it from the list")
                prepared.append((None, sku, name, _clean(ln.cat) or "Grocery", ln.sale_price, _check_qty(unit, ln.qty, f"Line {n}"), ln.unit_cost, unit))

        cur = c.execute("INSERT INTO purchase_orders(supplier_id, ts, expected, note) VALUES (?,?,?,?)",
                        (b.supplier_id, _now(), _clean(b.expected), _clean(b.note)))
        pid = cur.lastrowid
        sup = c.execute("SELECT name FROM suppliers WHERE id=?", (b.supplier_id,)).fetchone()["name"]
        audit("po.created", "purchase_order", po_no(pid), f"{po_no(pid)} created for {sup}: {len(prepared)} line(s)",
              {"supplier": sup, "lines": [dict(sku=x[1], name=x[2], qty=x[5], unit_cost=x[6]) for x in prepared]})
        c.executemany(
            "INSERT INTO po_items(po_id, product_id, sku, name, cat, sale_price, ordered, unit_cost, unit) VALUES (?,?,?,?,?,?,?,?,?)",
            [(pid, a, sku, nm, cat, sp, q, uc, un) for (a, sku, nm, cat, sp, q, uc, un) in prepared])
    _bump()
    return {"id": pid, "po_no": po_no(pid)}


@router.post("/purchase-orders/{po_id}/cancel")
def cancel_po(po_id: int):
    with db(True) as c:
        r = _po_row(c, po_id, lock=True)
        if r["status"] != "ORDERED":
            raise HTTPException(409, "Only an untouched order can be cancelled. "
                                     "If it was partly received, close it short instead.")
        c.execute("UPDATE purchase_orders SET status='CANCELLED', closed_ts=? WHERE id=?", (_now(), po_id))
        audit("po.cancelled", "purchase_order", po_no(po_id), f"{po_no(po_id)} cancelled")
    _bump()
    return {"ok": True}


@router.post("/purchase-orders/{po_id}/receive")
@tx
def receive_po(po_id: int, b: ReceiveIn):
    with db(True) as c:  # the PO row is locked: two people receiving the same PO are serialised
        po = _po_row(c, po_id, lock=True)
        if po["status"] not in ("ORDERED", "PARTIAL"):
            raise HTTPException(409, f"{po_no(po_id)} is {po['status']} — nothing left to receive")

        items = {i["id"]: i for i in c.execute("SELECT * FROM po_items WHERE po_id=? ORDER BY id FOR UPDATE", (po_id,)).fetchall()}
        wanted, seen = [], set()
        for ln in b.lines:
            it = items.get(ln.po_item_id)
            if it is None:
                raise HTTPException(422, f"Line {ln.po_item_id} does not belong to {po_no(po_id)}")
            if ln.po_item_id in seen:
                raise HTTPException(422, f"Line {ln.po_item_id} sent twice")
            seen.add(ln.po_item_id)
            remaining = q3(it["ordered"] - it["received"])
            qty = q3(ln.qty)
            if qty > remaining:
                raise HTTPException(409, f"'{it['name']}': only {remaining:g} still expected, you entered {qty:g}. "
                                         f"(Already received? Refresh the page.)")
            if qty > 0:
                _check_qty(it["unit"], qty, f"'{it['name']}'")
                wanted.append((it, qty, ln.unit_cost if ln.unit_cost is not None else it["unit_cost"]))
        if not wanted:
            raise HTTPException(422, "Enter a received quantity for at least one line")

        supplier = po["supplier"]
        cur = c.execute("INSERT INTO receipts(po_id, supplier_id, ts, total, note, paid, paid_ts, pay_method) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (po_id, po["supplier_id"], _now(), 0, _clean(b.note),
                         1 if b.paid else 0, _now() if b.paid else None, _clean(b.pay_method) if b.paid else ""))
        rid = cur.lastrowid
        total, created = 0.0, []
        wanted.sort(key=lambda w: (w[0]["product_id"] or 0, w[0]["id"]))     # same lock order every time → no deadlocks
        for it, qty, cost in wanted:
            pid = it["product_id"]
            if pid is None:  # brand-new product: link if SKU appeared meanwhile, else create it now
                ex = c.execute("SELECT id FROM products WHERE sku=?", (it["sku"],)).fetchone()
                if ex:
                    pid = ex["id"]
                else:
                    pid = c.execute("INSERT INTO products(sku, name, cat, price, stock, cost, unit, active) VALUES (?,?,?,?,0,?,?,1)",
                                    (it["sku"], it["name"], it["cat"], it["sale_price"], cost, it["unit"])).lastrowid
                    created.append(it["name"])
                c.execute("UPDATE po_items SET product_id=? WHERE id=?", (pid, it["id"]))
            _stock_in(c, pid, qty, f"{pur_no(rid)} from {supplier} ({po_no(po_id)})")
            c.execute("UPDATE products SET cost=? WHERE id=?", (cost, pid))
            c.execute("UPDATE po_items SET received=ROUND((received+?)::numeric,3) WHERE id=?", (qty, it["id"]))
            c.execute("INSERT INTO receipt_items(receipt_id, po_item_id, product_id, name, qty, unit_cost) VALUES (?,?,?,?,?,?)",
                      (rid, it["id"], pid, it["name"], qty, cost))
            total += qty * cost
        total = money(total)
        c.execute("UPDATE receipts SET total=? WHERE id=?", (total, rid))

        left = q3(c.execute("SELECT COALESCE(SUM(ordered-received),0) FROM po_items WHERE po_id=?", (po_id,)).fetchone()[0])
        if left <= 0:
            status = "RECEIVED"
        elif b.close_short:
            status = "CLOSED"
        else:
            status = "PARTIAL"
        c.execute("UPDATE purchase_orders SET status=?, closed_ts=? WHERE id=?",
                  (status, _now() if status in ("RECEIVED", "CLOSED") else None, po_id))
        audit("purchase.received", "purchase", pur_no(rid),
              f"{pur_no(rid)} received from {supplier} ({po_no(po_id)}): Rs {total:g}, order now {status}" + (" — paid" if b.paid else " — unpaid"),
              {"po": po_no(po_id), "supplier": supplier, "total": total, "status": status, "paid": b.paid, "pay_method": _clean(b.pay_method) if b.paid else "",
               "lines": [dict(name=it["name"], qty=q, unit_cost=co) for it, q, co in wanted], "new_products": created})
    _bump()
    return {"purchase_id": rid, "pur_no": pur_no(rid), "total": total, "po_status": status,
            "units_short": left if status == "CLOSED" else 0, "new_products": created}


@router.post("/purchase-orders/{po_id}/close")
def close_po_short(po_id: int):
    """Supplier will not deliver the rest of a partly received order."""
    with db(True) as c:
        r = _po_row(c, po_id, lock=True)
        if r["status"] != "PARTIAL":
            raise HTTPException(409, "Only a partly received order can be closed short")
        c.execute("UPDATE purchase_orders SET status='CLOSED', closed_ts=? WHERE id=?", (_now(), po_id))
        audit("po.closed", "purchase_order", po_no(po_id), f"{po_no(po_id)} closed short")
    _bump()
    return {"ok": True}


# ───────────────────────────── purchases (received deliveries) ─────────────────────────────
@router.get("/purchases")
def list_purchases(limit: int = Query(100, ge=1, le=500), unpaid_only: bool = False):
    q = ("SELECT r.*, s.name AS supplier FROM receipts r JOIN suppliers s ON s.id=r.supplier_id"
         + (" WHERE r.paid=0" if unpaid_only else "") + " ORDER BY r.id DESC LIMIT ?")
    with db() as c:
        out = []
        for r in c.execute(q, (limit,)).fetchall():
            n = c.execute("SELECT COALESCE(SUM(qty),0), COUNT(*) FROM receipt_items WHERE receipt_id=?", (r["id"],)).fetchone()
            out.append(dict(id=r["id"], pur_no=pur_no(r["id"]), po_id=r["po_id"], po_no=po_no(r["po_id"]),
                            supplier=r["supplier"], ts=r["ts"], total=r["total"], units=n[0], lines=n[1],
                            paid=bool(r["paid"]), paid_ts=r["paid_ts"], pay_method=r["pay_method"]))
        return out


@router.get("/purchases/{rid}")
def get_purchase(rid: int):
    with db() as c:
        r = c.execute("SELECT r.*, s.name AS supplier FROM receipts r JOIN suppliers s ON s.id=r.supplier_id WHERE r.id=?",
                      (rid,)).fetchone()
        if r is None:
            raise HTTPException(404, "Purchase not found")
        items = c.execute("SELECT * FROM receipt_items WHERE receipt_id=? ORDER BY id", (rid,)).fetchall()
        return dict(id=r["id"], pur_no=pur_no(rid), po_no=po_no(r["po_id"]), supplier=r["supplier"], ts=r["ts"],
                    total=r["total"], note=r["note"], paid=bool(r["paid"]), paid_ts=r["paid_ts"],
                    pay_method=r["pay_method"],
                    items=[dict(name=i["name"], qty=i["qty"], unit_cost=i["unit_cost"],
                                line_total=money(i["qty"] * i["unit_cost"])) for i in items])


@router.post("/purchases/{rid}/pay")
def pay_purchase(rid: int, b: PayIn):
    with db(True) as c:
        r = c.execute("SELECT paid, total FROM receipts WHERE id=? FOR UPDATE", (rid,)).fetchone()
        if r is None:
            raise HTTPException(404, "Purchase not found")
        if r["paid"]:
            raise HTTPException(409, "Already marked as paid")
        c.execute("UPDATE receipts SET paid=1, paid_ts=?, pay_method=? WHERE id=?",
                  (_now(), _clean(b.pay_method) or "Cash", rid))
        audit("purchase.paid", "purchase", pur_no(rid), f"{pur_no(rid)} marked paid (Rs {r['total']:g}, {_clean(b.pay_method) or 'Cash'})",
              {"total": r["total"], "pay_method": _clean(b.pay_method) or "Cash"})
    _bump()
    return {"ok": True}


# ───────────────────────────── expenses ─────────────────────────────
@router.get("/expenses")
def list_expenses(frm: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
                  limit: int = Query(200, ge=1, le=1000), include_voided: bool = False):
    d1, d2 = _range(frm, to)
    with db() as c:
        rows = c.execute("SELECT * FROM expenses WHERE ts::date BETWEEN ? AND ?" + ("" if include_voided else " AND voided=0")
                         + " ORDER BY ts DESC, id DESC LIMIT ?", (d1, d2, limit)).fetchall()
        tot = c.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE ts::date BETWEEN ? AND ? AND voided=0", (d1, d2)).fetchone()[0]
    return {"from": d1, "to": d2, "total": money(tot),
            "items": [dict(id=r["id"], exp_no=exp_no(r["id"]), ts=r["ts"], category=r["category"],
                           amount=r["amount"], payee=r["payee"], note=r["note"],
                           voided=bool(r["voided"]), voided_ts=r["voided_ts"], void_reason=r["void_reason"]) for r in rows]}


@router.post("/expenses")
def add_expense(b: ExpenseIn):
    cat = _clean(b.category)
    if cat not in EXPENSE_CATEGORIES:
        raise HTTPException(422, f"Category must be one of: {', '.join(EXPENSE_CATEGORIES)}")
    day = _parse_day(b.date, _today())
    if day > _today():
        raise HTTPException(422, "Expense date can't be in the future")
    ts = datetime.combine(day, datetime.now(database.TZ).time()).isoformat(timespec="seconds")   # stored in the shop's timezone
    with db(True) as c:
        eid = c.execute("INSERT INTO expenses(ts, category, amount, payee, note) VALUES (?,?,?,?,?)",
                        (ts, cat, money(b.amount), _clean(b.payee), _clean(b.note))).lastrowid
        audit("expense.added", "expense", exp_no(eid), f"{exp_no(eid)}: Rs {money(b.amount):g} {cat}" + (f" to {_clean(b.payee)}" if _clean(b.payee) else ""),
              {"category": cat, "amount": money(b.amount), "payee": _clean(b.payee), "note": _clean(b.note), "date": day.isoformat()})
    _bump()
    return {"id": eid, "exp_no": exp_no(eid)}


class VoidIn(BaseModel):
    reason: str = Field(default="", max_length=200)


@router.post("/expenses/{eid}/void")
def void_expense(eid: int, b: VoidIn):
    """Expenses are never deleted: a voided one stays on record (with who/when/why) but no longer counts in any total."""
    reason = _clean(b.reason)
    if not reason:
        raise HTTPException(422, "A reason is required to void an expense")
    with db(True) as c:
        r = c.execute("SELECT * FROM expenses WHERE id=? FOR UPDATE", (eid,)).fetchone()
        if r is None:
            raise HTTPException(404, "Expense not found")
        if r["voided"]:
            raise HTTPException(409, "Already voided")
        c.execute("UPDATE expenses SET voided=1, voided_ts=?, void_reason=? WHERE id=?", (_now(), reason, eid))
        audit("expense.voided", "expense", exp_no(eid), f"{exp_no(eid)} voided (Rs {r['amount']:g} {r['category']}) — {reason}",
              {"amount": r["amount"], "category": r["category"], "reason": reason})
    _bump()
    return {"ok": True}


# ───────────────────────────── report ─────────────────────────────
@router.get("/reports/purchases")
def purchase_report(frm: Optional[str] = Query(None, alias="from"), to: Optional[str] = None):
    d1, d2 = _range(frm, to)
    with db() as c:
        purchases = c.execute("SELECT COALESCE(SUM(total),0), COUNT(*) FROM receipts WHERE ts::date BETWEEN ? AND ?",
                              (d1, d2)).fetchone()
        by_supplier = c.execute(
            "SELECT s.name, COUNT(*) AS deliveries, COALESCE(SUM(r.total),0) AS total, "
            "COALESCE(SUM(CASE WHEN r.paid=0 THEN r.total END),0) AS unpaid "
            "FROM receipts r JOIN suppliers s ON s.id=r.supplier_id WHERE r.ts::date BETWEEN ? AND ? "
            "GROUP BY s.id ORDER BY total DESC", (d1, d2)).fetchall()
        by_product = c.execute(
            "SELECT MIN(ri.name) AS name, SUM(ri.qty) AS qty, SUM(ri.qty*ri.unit_cost) AS total "
            "FROM receipt_items ri JOIN receipts r ON r.id=ri.receipt_id WHERE r.ts::date BETWEEN ? AND ? "
            "GROUP BY ri.product_id ORDER BY total DESC LIMIT 10", (d1, d2)).fetchall()
        unpaid_all = c.execute("SELECT COALESCE(SUM(total),0) FROM receipts WHERE paid=0").fetchone()[0]
        open_pos = c.execute(
            "SELECT COUNT(DISTINCT p.id), COALESCE(SUM((i.ordered-i.received)*i.unit_cost),0) "
            "FROM purchase_orders p JOIN po_items i ON i.po_id=p.id WHERE p.status IN ('ORDERED','PARTIAL')").fetchone()
        exp = c.execute("SELECT COALESCE(SUM(amount),0), COUNT(*) FROM expenses WHERE ts::date BETWEEN ? AND ? AND voided=0",
                        (d1, d2)).fetchone()
        exp_cat = c.execute("SELECT category, SUM(amount) AS total FROM expenses WHERE ts::date BETWEEN ? AND ? AND voided=0 "
                            "GROUP BY category ORDER BY total DESC", (d1, d2)).fetchall()
        # sales (net of tax, minus whatever was refunded — whole or per item) — from the existing sales table
        sales = c.execute("SELECT COALESCE(SUM(total-tax-COALESCE(refunded_net,0)),0), COUNT(*) FROM sales "
                          "WHERE ts::date BETWEEN ? AND ? AND refunded=0", (d1, d2)).fetchone()
        refunds = c.execute("SELECT COALESCE(SUM(refunded_net),0), COUNT(*) FROM sales "
                            "WHERE ts::date BETWEEN ? AND ? AND COALESCE(refunded_amount,0)>0", (d1, d2)).fetchone()

    p_total, e_total, s_total = money(purchases[0]), money(exp[0]), money(sales[0])
    return {
        "from": d1, "to": d2,
        "sales_net": s_total, "sales_count": sales[1],
        "refunded_net": money(refunds[0]), "refunded_count": refunds[1],
        "purchases_total": p_total, "purchases_count": purchases[1],
        "expenses_total": e_total, "expenses_count": exp[1],
        "cashflow": money(s_total - p_total - e_total),
        "unpaid_to_suppliers": money(unpaid_all),
        "open_po_count": open_pos[0], "open_po_value": money(open_pos[1]),
        "by_supplier": [dict(name=r["name"], deliveries=r["deliveries"], total=money(r["total"]),
                             unpaid=money(r["unpaid"])) for r in by_supplier],
        "top_products": [dict(name=r["name"], qty=r["qty"], total=money(r["total"])) for r in by_product],
        "expenses_by_category": [dict(category=r["category"], total=money(r["total"])) for r in exp_cat],
    }
