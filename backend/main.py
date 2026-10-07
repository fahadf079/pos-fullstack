"""POS Backend v12 — FastAPI + PostgreSQL + roles (employee PIN / owner password + 2FA / developer / guest), 1-minute lock, ordered request pipeline. Real-time inventory: stock is deducted the moment an item is
scanned; every stock change is written to a movements ledger, every business event to the append-only
History (audit_log), and every change is pushed to all open screens via Server-Sent Events.
Run: pip install -r requirements.txt  ->  uvicorn main:app --reload --port 8000   (needs PostgreSQL: see README)"""
import asyncio, json, re
from typing import Optional
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import os
import context, database, catalog, purchasing, history, auth, users, settings, alerts, netpolicy, pipeline, policy, credentials, cash, cardvault, profit
from contextlib import asynccontextmanager
from database import db, tx, audit, jl, now, bump, transaction

# Tax rate, the low-stock number and the discount codes now live in Settings (owner-editable); these helpers read them.
def tax_rate(): return float(settings.get("tax_percent")) / 100
def low_threshold(): return int(settings.get("low_stock"))
def discount_rate(code):
    return float(settings.get("discount_codes").get((code or "").strip().upper(), 0)) / 100
SEED = [("8964001","Basmati Rice 1kg","Grocery",320,40),("8964002","Cooking Oil 1L","Grocery",580,25),
 ("8964003","Sugar 1kg","Grocery",180,50),("8964004","Milk 1L","Dairy",210,35),("8964005","Yogurt 500g","Dairy",150,30),
 ("8964006","Bread Loaf","Bakery",170,18),("8964007","Biscuits Pack","Snacks",120,60),("8964008","Chips Pack","Snacks",150,45),
 ("8964009","Bottled Water 1.5L","Beverages",90,70),("8964010","Dish Soap","Household",260,22)]

database.init()                        # connect + create tables/guards if they don't exist yet
with transaction():
    if not db.execute("SELECT 1 FROM products LIMIT 1").fetchone():     # brand-new database: sample products
        db.executemany("INSERT INTO products(sku,name,cat,price,stock) VALUES(?,?,?,?,?)", SEED)

def rows(q, *a): return [dict(r) for r in db.execute(q, a).fetchall()]
def sees_cost():
    """What the shop pays for goods is the owner's business: employees and guests never get the cost column."""
    u = context.get_ctx()
    return bool(u and u.user["role"] in ("owner", "developer"))
def public_product(p):
    return p if sees_cost() else {k: v for k, v in p.items() if k != "cost"}
def product(sku, lock=False):
    r = db.execute("SELECT * FROM products WHERE sku=?" + (" FOR UPDATE" if lock else ""), (sku,)).fetchone()
    if not r: raise HTTPException(404, f"No product with SKU {sku}")
    return dict(r)

def q3(x): return round(float(x), 3)             # quantities: up to 3 decimals (kg / litre items); pieces stay whole
def money(x): return round(float(x) + 1e-9, 2)   # rupees: 2 decimals
def ean13_ok(code):
    d = [int(c) for c in code]
    return (10 - (sum(d[i] * (3 if i % 2 else 1) for i in range(12)) % 10)) % 10 == d[12]

def scale_label(sku):
    """A price-computing scale prints 13 digits: prefix 20-29, 5-digit item code, 5-digit grams, check digit. Returns
    (item code, kilograms) or None when this is not such a label, or the Settings switch is off."""
    if not settings.get("scale_barcodes") or not re.fullmatch(r"2\d{12}", sku or ""): return None
    if not ean13_ok(sku): raise HTTPException(422, "This weighed-item barcode is damaged (wrong check digit). Scan it again.")
    grams = int(sku[7:12])
    if grams <= 0: raise HTTPException(422, "This weighed-item barcode has no weight on it.")
    return sku[2:7], round(grams / 1000, 3)

def qty_ok(p, q):
    """Validates a quantity for this product: above 0, and whole numbers for items sold by the piece."""
    q = q3(q)
    if q <= 0: raise HTTPException(422, "Quantity must be above 0")
    if p.get("unit", "pc") == "pc" and abs(q - round(q)) > 1e-9:
        raise HTTPException(422, f'"{p["name"]}" is sold by the piece: enter a whole number')
    return q

def move(p, delta, typ, note=""):
    """The ONLY place stock changes — guarantees every change is ledgered. Locks the product row, so two
    tills can never both take the last unit (the second one waits, then sees the real stock and gets a 409)."""
    cur = db.execute("SELECT stock FROM products WHERE id=? FOR UPDATE", (p["id"],)).fetchone()
    if cur is None: raise HTTPException(404, "Product not found")
    before = q3(cur["stock"]); delta = q3(delta); after = q3(before + delta)
    if after < 0: raise HTTPException(409, f'Only {before:g} of "{p["name"]}" in stock')
    db.execute("UPDATE products SET stock=? WHERE id=?", (after, p["id"]))
    uid, who = database.actor_pair()
    db.execute("INSERT INTO movements(ts,sku,name,type,delta,before,after,note,user_id,actor) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (now(), p["sku"], p["name"], typ, delta, before, after, note, uid, who))
    p["stock"] = after; bump()

def cart_lock(exclusive=False):
    """FIRST statement of every endpoint that changes the cart. Scan / remove / set can run side by side
    (SHARED); Clear and Checkout need the whole cart to themselves (EXCLUSIVE), otherwise an item scanned
    by another till at that instant could be wiped without its stock being returned. Always taking this
    lock before any row lock keeps the lock order fixed, so these endpoints can't deadlock each other."""
    db.execute("LOCK TABLE cart IN " + ("SHARE ROW EXCLUSIVE" if exclusive else "ROW EXCLUSIVE") + " MODE")

def totals(code=None):
    sub = money(sum(round(r["price"] * r["qty"], 2) for r in db.execute("SELECT p.price,c.qty FROM cart c JOIN products p ON p.id=c.product_id")))
    disc = money(sub * discount_rate(code))
    tax = money((sub - disc) * tax_rate())
    return {"subtotal": sub, "discount": disc, "tax": tax, "total": money(sub - disc + tax)}

@asynccontextmanager
async def lifespan(_):
    task = asyncio.create_task(alerts.monitor_loop())          # background checker: network, backups (alerts.py)
    try:
        yield
    finally:
        task.cancel()

# Every route goes through pipeline.guard (network → identity → lock → permission → PIN). No interactive docs page.
app = FastAPI(title="POS Backend v12", dependencies=[Depends(pipeline.guard)], lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# Only this shop's own screens may talk to the backend (this computer, or tills on the shop's private network).
# Cookies need an explicit origin list, never "*". Override with POS_CORS_ORIGIN_REGEX if you serve the screen elsewhere.
ORIGIN_RE = os.environ.get("POS_CORS_ORIGIN_REGEX", r"^https?://(localhost|127\.0\.0\.1|\[::1\]|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)(:\d+)?$")
app.add_middleware(CORSMiddleware, allow_origin_regex=ORIGIN_RE, allow_credentials=True, allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type", "X-POS", "X-POS-PIN"])

catalog.setup(bump)
purchasing.setup(move, bump)
app.include_router(catalog.router)
app.include_router(purchasing.router)
app.include_router(history.router)
for r_ in (auth.router, users.router, settings.router, alerts.router, netpolicy.router, pipeline.router, cash.router, cardvault.router, profit.router):
    app.include_router(r_)

class Scan(BaseModel): sku: str; qty: float = Field(1, gt=0, le=10000)          # qty<=0 used to ADD stock via /scan; decimals only for kg / l items
class Remove(BaseModel): product_id: int; qty: float = Field(1, gt=0, le=10000)
class CartSet(BaseModel): product_id: int; qty: float = Field(ge=0, le=10000)     # set a line to an exact quantity (0 = remove)
class Adjust(BaseModel): sku: str; new_stock: float = Field(ge=0, le=10_000_000); note: str = Field("manual count", max_length=200)
class Checkout(BaseModel): payment_method: str; discount_code: Optional[str] = None
class RefundLine(BaseModel): id: int; qty: float = Field(gt=0, le=10000)      # product id + quantity to take back
class Refund(BaseModel): note: str = "refunded"; items: Optional[list[RefundLine]] = None; paid_via: Optional[str] = None   # how the money went back: Cash / Card / Wallet (default: the way the customer paid)   # items omitted = refund everything still refundable

@app.get("/inventory")
def inventory(): return {"products": [public_product(p) for p in rows("SELECT * FROM products ORDER BY cat, name")], "low_threshold": low_threshold()}

@app.post("/scan")  # scan = instant deduction + add to cart
@tx
def scan(r: Scan):
    cart_lock()
    label = scale_label(r.sku.strip())
    if label:                                                       # weighed item: the weight comes from the label, not from the till
        code, kg = label
        row = db.execute("SELECT sku FROM products WHERE sku IN (?, ?) ORDER BY (sku=?) DESC LIMIT 1", (code, code.lstrip("0") or "0", code)).fetchone()
        if not row: raise HTTPException(404, f"No product with item code {code} (add it in Catalog with that code as its barcode)")
        p = product(row["sku"])
        if p.get("unit", "pc") == "pc": raise HTTPException(422, f'"{p["name"]}" is sold by the piece, but this label carries a weight. Set its unit to kg in Catalog.')
        r.qty = kg
    else:
        p = product(r.sku)
    if not p["active"]: raise HTTPException(409, f'"{p["name"]}" is deactivated and can\'t be sold')
    if p["price"] <= 0: raise HTTPException(409, f'"{p["name"]}" has no selling price: set one in Catalog first')
    q = qty_ok(p, r.qty)
    db.execute("SELECT 1 FROM cart WHERE product_id=? FOR UPDATE", (p["id"],))   # same lock order everywhere: cart line, then product
    move(p, -q, "SCAN", "sold — in cart")
    db.execute("INSERT INTO cart(product_id,qty) VALUES(?,?) ON CONFLICT(product_id) DO UPDATE SET qty=ROUND((cart.qty+?)::numeric,3)", (p["id"], q, q))
    return {"product": public_product(p)}

@app.post("/cart/remove")  # removing from cart gives the stock back
@tx
def remove(r: Remove):
    if settings.get("remove_needs_pin"): credentials.confirm("self")
    cart_lock()
    row = db.execute("SELECT qty FROM cart WHERE product_id=? FOR UPDATE", (r.product_id,)).fetchone()
    if not row: raise HTTPException(404, "Not in cart")
    have = q3(row["qty"]); p = dict(db.execute("SELECT * FROM products WHERE id=?", (r.product_id,)).fetchone())
    q = min(qty_ok(p, r.qty), have)
    move(p, q, "VOID", "removed from cart")
    if q3(have - q) <= 0: db.execute("DELETE FROM cart WHERE product_id=?", (r.product_id,))
    else: db.execute("UPDATE cart SET qty=ROUND((qty-?)::numeric,3) WHERE product_id=?", (q, r.product_id))
    return {"ok": True}

@app.post("/cart/set")  # change a line to an exact quantity in ONE step (used for weighed items)
@tx
def cart_set(r: CartSet):
    cart_lock()
    row = db.execute("SELECT qty FROM cart WHERE product_id=? FOR UPDATE", (r.product_id,)).fetchone()
    if not row: raise HTTPException(404, "Not in cart")
    if settings.get("remove_needs_pin") and r.qty < q3(row["qty"]): credentials.confirm("self")      # lowering a line is removing
    p = dict(db.execute("SELECT * FROM products WHERE id=?", (r.product_id,)).fetchone())
    new = qty_ok(p, r.qty) if r.qty > 0 else 0.0
    delta = q3(new - q3(row["qty"]))
    if delta != 0: move(p, -delta, "SCAN" if delta > 0 else "VOID", "cart quantity changed")
    if new <= 0: db.execute("DELETE FROM cart WHERE product_id=?", (r.product_id,))
    else: db.execute("UPDATE cart SET qty=? WHERE product_id=?", (new, r.product_id))
    return {"ok": True}

@app.post("/cart/clear")
@tx
def clear():
    if settings.get("remove_needs_pin"): credentials.confirm("self")
    cart_lock(exclusive=True)
    for c in rows("SELECT * FROM cart ORDER BY product_id FOR UPDATE"):
        move(dict(db.execute("SELECT * FROM products WHERE id=?", (c["product_id"],)).fetchone()), c["qty"], "VOID", "cart cleared")
    db.execute("DELETE FROM cart"); return {"ok": True}

@app.get("/cart")
def cart(discount_code: Optional[str] = None):
    with transaction(snapshot=True):          # items and totals from the same instant
        items = rows("SELECT p.id,p.sku,p.name,p.price,p.unit,c.qty FROM cart c JOIN products p ON p.id=c.product_id ORDER BY p.name")
        return {"items": items, "totals": totals(discount_code)}

@app.post("/checkout")  # stock already deducted at scan, so checkout only records the sale
@tx
def checkout(r: Checkout):
    if r.payment_method not in ("Cash", "Card", "Wallet"): raise HTTPException(400, "Invalid payment method")
    if discount_rate(r.discount_code) > 0:      # a discount needs the employee's own PIN
        credentials.confirm("self")
    if r.payment_method == "Cash": cash.require_shift_for_cash()
    cart_lock(exclusive=True)                                     # nothing can slip into the cart between totalling and saving
    c = cart(r.discount_code)
    if not c["items"]: raise HTTPException(400, "Cart is empty")
    t, ts = c["totals"], now()
    uid, who = database.actor_pair()
    costs = {x["id"]: x["cost"] for x in rows("SELECT id, cost FROM products WHERE id = ANY(?)", [i["id"] for i in c["items"]])}
    snap = [{**i, "cost": float(costs.get(i["id"]) or 0)} for i in c["items"]]       # cost-at-sale, for the profit report; hidden from employees
    sid = db.execute("INSERT INTO sales(ts,subtotal,discount,tax,total,discount_code,payment,items,user_id,actor) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (ts, t["subtotal"], t["discount"], t["tax"], t["total"], (r.discount_code or "").upper() or None, r.payment_method, json.dumps(snap), uid, who)).lastrowid
    db.execute("DELETE FROM cart")
    if r.payment_method == "Cash": cash.open_for_sale(sid)         # drawer opens only for a saved cash sale (after the commit)
    audit("sale.checkout", "sale", sid, f"Sale #{sid}: {len(c['items'])} item(s), Rs {t['total']:g} by {r.payment_method}",
          {"total": t["total"], "payment": r.payment_method, "discount_code": (r.discount_code or "").upper() or None, "items": c["items"]})
    bump()
    return {"receipt": {**c, "id": sid, "payment_method": r.payment_method, "timestamp": ts}}

@app.post("/inventory/adjust")
@tx
def adjust(r: Adjust):
    p = product(r.sku, lock=True); ns = q3(r.new_stock)
    if p["unit"] == "pc" and abs(ns - round(ns)) > 1e-9: raise HTTPException(422, f'"{p["name"]}" is sold by the piece: enter a whole number')
    old = p["stock"]; move(p, q3(ns - old), "ADJUST", r.note)
    audit("stock.adjust", "product", p["sku"], f'{p["name"]}: stock {old:g} → {ns:g} ({r.note})', {"before": old, "after": ns, "note": r.note})
    return {"product": public_product(p)}

@app.get("/sales")  # History list — newest first
def sales(limit: int = 100):
    out = rows("SELECT id,ts,total,payment,discount_code,refunded,refunded_amount,actor,jsonb_array_length(items) AS item_count FROM sales ORDER BY id DESC LIMIT ?", limit)
    return {"sales": out}

def refunded_qty(sale_id):
    """{product_id: quantity already refunded} across every refund of this sale."""
    done: dict[int, float] = {}
    for r in db.execute("SELECT items FROM refunds WHERE sale_id=?", (sale_id,)).fetchall():
        for i in jl(r["items"]): done[i["id"]] = q3(done.get(i["id"], 0) + i["qty"])
    return done

@app.get("/sales/{sale_id}")  # full invoice for reprint
def sale_detail(sale_id: int):
    with transaction(snapshot=True):
        r = db.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
        if not r: raise HTTPException(404, "Sale not found")
        d = dict(r); d["items"] = jl(d["items"])
        if context.current_user()["role"] not in ("owner", "developer"):      # what the shop paid for an item is the owner's business
            for i in d["items"]: i.pop("cost", None)
        done = refunded_qty(sale_id)
        legacy = bool(d["refunded"]) and not done          # whole-sale refund made before per-item refunds existed
        for i in d["items"]: i["refunded_qty"] = i["qty"] if legacy else q3(done.get(i["id"], 0))
        d["refunds"] = rows("SELECT id,ts,note,amount,paid_via FROM refunds WHERE sale_id=? ORDER BY id", sale_id)
        d["refunded_amount"] = money(d.get("refunded_amount") or 0)
        return d

@app.post("/sales/{sale_id}/refund")  # takes back chosen items (or everything left): stock returns, money is recorded
@tx
def refund(sale_id: int, r: Refund):
    row = db.execute("SELECT * FROM sales WHERE id=? FOR UPDATE", (sale_id,)).fetchone()    # two refunds of one sale are serialised
    if not row: raise HTTPException(404, "Sale not found")
    if row["refunded"]: raise HTTPException(409, "Already fully refunded")
    note = (r.note or "").strip()
    if not note: raise HTTPException(422, "A refund needs a reason")
    items = jl(row["items"]); done = refunded_qty(sale_id)
    left = {i["id"]: q3(i["qty"] - done.get(i["id"], 0)) for i in items}      # still refundable per product
    if r.items is None: want = {pid: q for pid, q in left.items() if q > 0}
    else:
        want = {}
        for l in r.items:
            if l.id not in left: raise HTTPException(422, "That item is not on this invoice")
            if l.id in want: raise HTTPException(422, "Each item can only be listed once")
            want[l.id] = q3(l.qty)
    if not want: raise HTTPException(409, "Nothing left to refund on this invoice")
    lines = []
    for i in items:
        if i["id"] not in want: continue
        q = want[i["id"]]
        if q <= 0: raise HTTPException(422, "Refund quantity must be above 0")
        if i.get("unit", "pc") == "pc" and abs(q - round(q)) > 1e-9:
            raise HTTPException(422, f'"{i["name"]}" is sold by the piece: enter a whole number')
        if q > left[i["id"]] + 1e-9:
            raise HTTPException(409, f'Only {left[i["id"]]:g} of "{i["name"]}" can still be refunded')
        lines.append({**i, "qty": q, "amount": round(i["price"] * q, 2)})
    for l in sorted(lines, key=lambda x: x["id"]):
        p = db.execute("SELECT * FROM products WHERE id=?", (l["id"],)).fetchone()
        if p: move(dict(p), l["qty"], "REFUND", f"refund of sale #{sale_id}: {note}")
    # money: each line's share of the invoice total (discount + tax spread evenly); the LAST refund takes the
    # remainder so the refunds always add up to exactly the invoice total, with no rounding leftovers
    gross = sum(l["amount"] for l in lines); sub = row["subtotal"] or 0
    completes = all(q3(left[pid] - want.get(pid, 0)) <= 0 for pid in left)
    if completes:
        amount = money(row["total"] - (row["refunded_amount"] or 0)); net = money((row["total"] - row["tax"]) - (row["refunded_net"] or 0))
    else:
        amount = money(gross * row["total"] / sub) if sub > 0 else 0.0
        net = money(gross * (sub - (row["discount"] or 0)) / sub) if sub > 0 else 0.0
    ts = now()
    detail = [{"id": l["id"], "name": l["name"], "qty": l["qty"], "unit": l.get("unit", "pc"), "price": l["price"], "amount": l["amount"]} for l in lines]
    ruid, rwho = database.actor_pair()
    paid_via = r.paid_via or row["payment"]
    if paid_via not in ("Cash", "Card", "Wallet"): raise HTTPException(422, "Money can go back as Cash, Card or Wallet.")
    db.execute("INSERT INTO refunds(sale_id,ts,note,amount,net,items,user_id,actor,paid_via) VALUES(?,?,?,?,?,?,?,?,?)", (sale_id, ts, note, amount, net, json.dumps(detail), ruid, rwho, paid_via))
    db.execute("UPDATE sales SET refunded=?, refund_note=?, refund_ts=?, refunded_amount=ROUND((COALESCE(refunded_amount,0)+?)::numeric,2), refunded_net=ROUND((COALESCE(refunded_net,0)+?)::numeric,2) WHERE id=?",
               (1 if completes else 0, note, ts, amount, net, sale_id))
    audit("sale.refund", "sale", sale_id, f"Refund on sale #{sale_id}: Rs {amount:g} ({'complete' if completes else 'partial'}) — {note}",
          {"amount": amount, "complete": completes, "reason": note, "paid_via": paid_via, "items": detail})
    bump()
    return {"ok": True, "refunded_amount": amount, "complete": completes}

@app.get("/movements")
def movements(limit: int = 50): return {"entries": rows("SELECT * FROM movements ORDER BY id DESC LIMIT ?", limit)}

@app.get("/dashboard")
def dashboard():
    with transaction(snapshot=True):
        s = rows("SELECT total, items FROM sales WHERE ts::date = ?::date", database.today())
        sold: dict[str, float] = {}
        for t in s:
            for i in jl(t["items"]): sold[i["name"]] = q3(sold.get(i["name"], 0) + i["qty"])
        top = sorted(sold.items(), key=lambda x: -x[1])[:5]
        return {"transactions": len(s), "revenue": money(sum(t["total"] for t in s)), "items_sold": q3(sum(sold.values())),
                "top_items": [{"name": n, "qty": q} for n, q in top],
                "low_stock": rows("SELECT sku,name,stock,unit FROM products WHERE active=1 AND stock<=? ORDER BY stock", low_threshold()),
                "stock_value": db.execute("SELECT COALESCE(SUM(price*stock),0) FROM products").fetchone()[0]}

@app.get("/events")  # live push: browser refetches whenever `version` changes
async def events(request: Request):
    async def gen():
        seen, idle, seen_alert = -1, 0, -1
        try:
            while not await request.is_disconnected():
                if alerts.VERSION != seen_alert:                # HIGH priority first: an alert is announced before any stock/sale change
                    seen_alert = alerts.VERSION
                    yield f"event: alert\ndata: {seen_alert}\n\n"
                v = database.get_version()
                if v != seen:
                    seen, idle = v, 0
                    yield f"data: {seen}\n\n"
                else:
                    idle += 1
                    if idle % 37 == 0:  # keep-alive roughly every 15s
                        yield ": ping\n\n"
                await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            return
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

# Must stay LAST: checks that every route above has a permission rule in policy.POLICY (refuses to start otherwise).
policy.install(app)                    # refuses to start if any route has no permission rule
if auth.no_users():
    print("\n*** No users yet: open the POS in the browser and create the OWNER account (first-time setup). ***\n")
