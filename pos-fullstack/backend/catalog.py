"""
catalog.py — Product catalog for the POS: add products, edit price / name / category / unit / cost,
deactivate & reactivate, and a change history (who-changed-what-price-when).

Rules (behaviour to preserve)
  * Stock is NEVER touched here. Stock only changes through main.move() (scan, receive, adjust, refund).
  * A product needs a selling price above 0. Price below the last purchase cost is allowed but the
    response carries a warning (the screen asks for confirmation before saving).
  * The barcode (SKU) can't be edited: the movements ledger and old invoices refer to it.
  * A deactivated product can't be scanned, ordered or picked, but keeps its history and stock,
    and refunds of old sales still put its stock back.
"""
from typing import Optional

import psycopg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import auth
from database import audit, now as _now, transaction

router = APIRouter()

UNITS = {"pc": "piece", "kg": "kilogram", "l": "litre"}   # pc = whole numbers only; kg / l allow decimals

_bump = lambda: None


def db(write: bool = False):
    """One real PostgreSQL transaction (joins the surrounding one). Reads get a consistent snapshot."""
    return transaction(snapshot=not write)


def setup(bump) -> None:
    """Call once from main.py. (The product_log table is created by database.init().)"""
    global _bump
    _bump = bump


def _clean(s: Optional[str]) -> str:
    return (s or "").strip()


def money(x: float) -> float:
    return round(float(x) + 1e-9, 2)


def _fmt(v) -> str:
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


def _log(c, p, field: str, old, new) -> None:
    o, n = (None if old is None else _fmt(old)), (None if new is None else _fmt(new))
    c.execute("INSERT INTO product_log(ts, product_id, sku, name, field, old, new) VALUES (?,?,?,?,?,?,?)",
              (_now(), p["id"], p["sku"], p["name"], field, o, n))
    audit(f"catalog.{field}", "product", p["sku"],
          f"{p['name']}: created ({n})" if field == "created" else f"{p['name']}: {field} {o} → {n}",
          {"field": field, "old": o, "new": n})   # same transaction as the change itself


def _row(c, pid: int, lock: bool = False):
    r = c.execute("SELECT * FROM products WHERE id=?" + (" FOR UPDATE" if lock else ""), (pid,)).fetchone()
    if r is None:
        raise HTTPException(404, "Product not found")
    return r


def _out(c, r) -> dict:
    in_cart = c.execute("SELECT COALESCE(SUM(qty),0) FROM cart WHERE product_id=?", (r["id"],)).fetchone()[0]
    return dict(id=r["id"], sku=r["sku"], name=r["name"], cat=r["cat"], price=r["price"], cost=r["cost"],
                stock=r["stock"], unit=r["unit"], active=bool(r["active"]), in_cart=in_cart)


def _warning(price: float, cost: float) -> str:
    if cost > 0 and price < cost:
        return (f"Selling price Rs {price:g} is below the purchase cost Rs {cost:g}: "
                f"loses Rs {money(cost - price):g} on every unit.")
    return ""


# ───────────────────────────── models ─────────────────────────────
class ProductIn(BaseModel):
    sku: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    cat: str = Field(default="Grocery", max_length=40)
    unit: str = Field(default="pc", max_length=4)
    price: float = Field(gt=0, le=10_000_000)
    cost: float = Field(default=0, ge=0, le=10_000_000)


class ProductPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    cat: Optional[str] = Field(default=None, max_length=40)
    unit: Optional[str] = Field(default=None, max_length=4)
    price: Optional[float] = Field(default=None, gt=0, le=10_000_000)
    cost: Optional[float] = Field(default=None, ge=0, le=10_000_000)


class ActiveIn(BaseModel):
    active: bool


# ───────────────────────────── endpoints ─────────────────────────────
@router.get("/catalog/meta")
def meta():
    with db() as c:
        cats = [r[0] for r in c.execute("SELECT DISTINCT cat FROM products WHERE cat<>'' ORDER BY cat").fetchall()]
    return {"categories": cats, "units": [{"code": k, "label": v} for k, v in UNITS.items()]}


@router.get("/catalog/products")
def list_products(include_inactive: bool = True):
    q = "SELECT * FROM products" + ("" if include_inactive else " WHERE active=1") + " ORDER BY lower(name)"
    with db() as c:
        return [_out(c, r) for r in c.execute(q).fetchall()]


@router.post("/catalog/products")
def create_product(b: ProductIn):
    sku, name = _clean(b.sku), _clean(b.name)
    cat, unit = _clean(b.cat) or "Grocery", _clean(b.unit) or "pc"
    if not sku or not name:
        raise HTTPException(422, "Barcode and name are required")
    if unit not in UNITS:
        raise HTTPException(422, f"Unit must be one of: {', '.join(UNITS)}")
    try:
        with db(True) as c:
            if c.execute("SELECT 1 FROM products WHERE sku=?", (sku,)).fetchone():
                raise HTTPException(409, f"Barcode {sku} already exists")
            pid = c.execute("INSERT INTO products(sku, name, cat, price, stock, cost, unit, active) VALUES (?,?,?,?,0,?,?,1)",
                            (sku, name, cat, money(b.price), money(b.cost), unit)).lastrowid
            r = _row(c, pid)
            _log(c, r, "created", None, f"price {money(b.price):g}, unit {unit}")
            out = _out(c, r)
    except psycopg.errors.UniqueViolation:       # two people adding the same barcode at the same moment
        raise HTTPException(409, f"Barcode {sku} already exists")
    _bump()
    return {"product": out, "warning": _warning(out["price"], out["cost"])}


@router.post("/catalog/products/{pid}/update")
def update_product(pid: int, b: ProductPatch):
    with db(True) as c:
        r = _row(c, pid, lock=True)
        sets, changes = {}, []

        if b.name is not None:
            n = _clean(b.name)
            if not n:
                raise HTTPException(422, "Name can't be empty")
            if n != r["name"]:
                sets["name"] = n
                changes.append(("name", r["name"], n))
        if b.cat is not None:
            ct = _clean(b.cat) or "Grocery"
            if ct != r["cat"]:
                sets["cat"] = ct
                changes.append(("cat", r["cat"], ct))
        if b.price is not None:
            pr = money(b.price)
            if pr <= 0:
                raise HTTPException(422, "Selling price must be above 0")
            if pr != r["price"]:
                sets["price"] = pr
                changes.append(("price", r["price"], pr))
        if b.cost is not None:
            co = money(b.cost)
            if co != r["cost"]:
                sets["cost"] = co
                changes.append(("cost", r["cost"], co))
        if b.unit is not None:
            u = _clean(b.unit) or "pc"
            if u not in UNITS:
                raise HTTPException(422, f"Unit must be one of: {', '.join(UNITS)}")
            if u != r["unit"]:
                if u == "pc":
                    if abs(r["stock"] - round(r["stock"])) > 1e-9:
                        raise HTTPException(409, f"Stock is {r['stock']:g}: adjust it to a whole number before switching to pieces")
                    frac = c.execute("SELECT 1 FROM cart WHERE product_id=? AND ABS(qty-ROUND(qty))>1e-9", (pid,)).fetchone()
                    if frac:
                        raise HTTPException(409, "A fractional quantity of this product is in the cart: finish or clear that sale first")
                open_po = c.execute("SELECT 1 FROM po_items i JOIN purchase_orders p ON p.id=i.po_id "
                                    "WHERE i.product_id=? AND p.status IN ('ORDERED','PARTIAL') AND i.ordered>i.received",
                                    (pid,)).fetchone()
                if open_po:
                    raise HTTPException(409, "This product is on an open purchase order: receive or close that order before changing its unit")
                sets["unit"] = u
                changes.append(("unit", r["unit"], u))

        if "price" in sets or "cost" in sets:
            auth.confirm("self")                 # changing what something sells for / costs needs your own PIN
        if sets:
            c.execute("UPDATE products SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?", (*sets.values(), pid))
            for field, old, new in changes:
                _log(c, r, field, old, new)   # r still holds the old name/sku, so the log reads correctly
        out = _out(c, _row(c, pid))
    if sets:
        _bump()
    return {"product": out, "changed": [x[0] for x in changes], "warning": _warning(out["price"], out["cost"])}


@router.post("/catalog/products/{pid}/active")
def set_active(pid: int, b: ActiveIn):
    with db(True) as c:
        r = _row(c, pid, lock=True)
        if bool(r["active"]) == b.active:
            return {"product": _out(c, r)}
        if not b.active:
            q = c.execute("SELECT COALESCE(SUM(qty),0) FROM cart WHERE product_id=?", (pid,)).fetchone()[0]
            if q:
                raise HTTPException(409, f"'{r['name']}' is in the current cart: checkout or clear the sale first")
        c.execute("UPDATE products SET active=? WHERE id=?", (1 if b.active else 0, pid))
        _log(c, r, "active", "yes" if r["active"] else "no", "yes" if b.active else "no")
        out = _out(c, _row(c, pid))
    _bump()
    return {"product": out}


@router.get("/catalog/products/{pid}/history")
def history(pid: int, limit: int = 50):
    with db() as c:
        _row(c, pid)
        rows = c.execute("SELECT ts, field, old, new FROM product_log WHERE product_id=? ORDER BY id DESC LIMIT ?",
                         (pid, max(1, min(limit, 500)))).fetchall()
    return [dict(r) for r in rows]

