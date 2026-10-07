# v8 — per-item refunds

**Why:** refunds were whole-receipt only. If a customer brings back one item, the cashier had to refund the entire invoice.
This reverses the earlier "whole receipt only" decision.

## What changed
- **Invoices → open a sale → "Return now" column.** Type how many of each item is coming back (or press "All"), then
  **Refund selected items**. Only those items go back into stock (REFUND rows in the movements ledger, kg/l decimals exact).
- **Refund everything left** still exists (refunds whatever hasn't been returned yet).
- A sale can be refunded in several steps (e.g. 1 rice today, the sugar next week). You can never return more than was sold
  (409), pieces must be whole numbers (422), a reason is required (422).
- **Money is now recorded.** Each refund stores the amount owed back to the customer: the item's share of the invoice total
  (discount and 8% tax spread proportionally). The final refund on an invoice takes the exact remainder, so all refunds always
  add up to the invoice total to the paisa. Invoice list shows `PART-REFUNDED Rs …`; the invoice shows every refund with time,
  amount and reason.
- **Reports:** "Sales (excl. tax)" now subtracts refunded amounts, whole or partial (a partly-refunded sale counts only its kept part).
  Fully refunded invoices are excluded from the invoice count, as before.
- Old whole-sale refunds (made before v8) are migrated automatically: they show as fully returned and count as fully refunded money.

## Files touched
backend/main.py (refunds table, sales.refunded_amount / refunded_net, refund endpoint, invoice detail), backend/purchasing.py
(report query), backend/tests/test_backend.py, frontend/src/api.ts, frontend/src/App.tsx (RefundPanel).

## API
`POST /sales/{id}/refund {note, items?: [{id, qty}]}` — `items` omitted = everything still refundable.
Returns `{ok, refunded_amount, complete}`. `GET /sales/{id}` now has `refunded_qty` per item, `refunds[]`, `refunded_amount`.

## Verified
- `cd backend && python tests/test_backend.py` passes on **real FastAPI 0.142 / pydantic** (not the stand-in): all v6/v7 flows plus
  per-item refunds (partial, repeated, over-refund, fractional pieces, wrong product, duplicate lines, missing reason, exact
  sum-to-total, kg partial 0.75 of 1.25, reports maths, legacy whole refund).
- Real HTTP against a running uvicorn: partial refund, validation error text, final refund completing the invoice (226.8 + 691.2 = 918.0).
- `tsc -b` and `vite build` clean.
## Not verified
- Clicking through the new Refund panel in a real browser (typed checks only), and a two-tab live refresh.
- Cash paid back is still not tracked against a till/drawer; there is still no login/PIN, so anyone can refund.
