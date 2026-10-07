# POS v6 — Purchasing, Suppliers, Expenses & Reports (on top of v5)

## Run
    # backend
    cd backend
    pip install fastapi uvicorn
    uvicorn main:app --reload --port 8000 --timeout-graceful-shutdown 2
    # frontend (second terminal)
    cd frontend
    npm install
    npm run dev

Keep your data: copy your existing `pos.db` into `backend/` BEFORE the first start.
It upgrades itself (adds the new tables and a `products.cost` column); nothing is lost.
Without a pos.db, a fresh one is created with the 10 sample products.

## New sidebar pages
- **Purchasing** — Purchase Orders | Purchases (received) | Suppliers
  1. New PO: stock does NOT change. Lines can be existing products or a brand-new product (SKU, name, category, selling price) that is created when its stock arrives.
  2. Receive stock: enter what actually arrived + actual unit cost. Barcode scanning works here: each scan adds 1 to that line's received quantity ("Start counting from 0" first).
  3. Short delivery: PO stays "Partly received" for a later delivery, or tick "close order" if the rest isn't coming.
  4. Every delivery becomes a Purchase (PUR-00001…) with paid/unpaid tracking; suppliers show what you owe.
- **Reports** — date-range report (sales excl. tax, purchases, expenses, cash-flow view, unpaid to suppliers, open orders, by supplier / product / expense category) + expense entry (Rent, Utilities, Salaries, Transport, Maintenance, Packaging, Marketing, Taxes & Fees, Other).
- Invoices stays the SALES history (INV); purchases are separate (PUR).

## Files
- NEW backend/purchasing.py, backend/tests/test_backend.py
- NEW frontend/src/PurchasingPages.tsx, frontend/src/purchasing.css
- CHANGED backend/main.py, frontend/src/api.ts, frontend/src/App.tsx

## What changed in existing code (and why)
main.py
- Wires in purchasing (shares your db connection, your move() and your live-update counter).
- REMOVED POST /inventory/receive and the Receive form in Inventory: stock now only goes up through a received purchase order (Adjust stays for physical counts).
- FIX: /scan and /cart/remove accepted qty <= 0, so a negative qty on /scan ADDED stock. Now qty must be 1–10000; /inventory/adjust rejects negative stock.
- FIX: all writing endpoints now run under one lock and roll back on error. Before, all requests shared one SQLite connection: a failed request left half-done changes that the next request's commit made permanent, and simultaneous requests could interleave.
api.ts
- `req` is exported; validation errors (422) now show readable text instead of "[object Object]".
App.tsx
- NAV: Purchasing, Reports. Inventory: manual Receive form replaced by a pointer to Purchasing.
- FIX: Invoices rows used keys on the wrong element (React warning, risky re-renders) -> keyed Fragment.
- FIX: useData ignores stale responses (a slow earlier fetch could overwrite newer data).

## Verified
- backend/tests/test_backend.py (run: `cd backend && python tests/test_backend.py`): all existing flows (scan, cart, checkout, refund, adjust), PO -> partial receive -> close short, your 50/30/18-of-20 example, replay/double-click protection, over-receive rejection, mid-receive failure rolls back completely, new-product creation, 8 simultaneous receives + scans (stock added exactly once), ledger integrity (every product's stock equals its last movement), report maths. Also run against a copy of your real v5 pos.db (migrated without data loss).
- Live server smoke test: SSE pushes a refresh to open tabs after a purchase.
- Frontend: `tsc -b` clean under your strict tsconfig, `vite build` OK, oxlint 0 errors (1 warning is the existing useData pattern).
- NOT done: clicking through in a real browser. Please do a quick pass: create supplier -> PO -> Receive (scan) -> check Inventory + Movements -> Reports.

## Known limits (unchanged / by design)
- No login yet (anyone can receive stock or delete an expense) -> Security module is the next step.
- A PO can't be edited after creation (cancel and recreate); no supplier returns; scanning an item that isn't on the PO shows a message rather than adding it.
- Abandoned carts and single global cart limits from v5 still apply.
