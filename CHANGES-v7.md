# v7 — Catalog, price safety, weighed items

## What's new
**Catalog page (sidebar, between Inventory and Purchasing)**
- Add a product directly (barcode, name, category, sold-by unit, selling price, optional cost). It starts with 0 stock; stock still only comes from receiving a PO or Inventory → Adjust.
- Edit name, category, selling price, cost and unit. The barcode can't be edited (the stock history and old invoices refer to it).
- Price below cost: the row is tagged BELOW COST and saving asks for confirmation ("you lose Rs X on every unit").
- Deactivate / Reactivate. A deactivated product can't be scanned, ordered or picked; it keeps its stock, history and refunds. Deactivating is refused while the product is in the open cart.
- History button per product: every change to name / category / price / cost / unit / active, with time and old → new.
- A banner appears when products have no price or are priced below cost.

**Price safety**
- The selling price must be above 0 everywhere (Catalog, new product on a PO).
- A product that already has price 0 (created before v7) can't be scanned: "has no selling price".
- New-product lines on a PO show a below-cost warning.

**Weighed items (kg / litre)**
- Each product has a unit: piece (whole numbers only), kg or litre (up to 3 decimals).
- Dashboard: scanning a kg/litre item asks for the weight, then adds it. In the cart use Weight (change it in one step) or Remove.
- Purchasing accepts decimal quantities for kg/litre items (ordering and partial receiving) and whole numbers for pieces.
- Adjust accepts decimals for kg/litre items. The movements ledger shows decimals.
- Money and quantities are rounded (rupees to 2 decimals, quantities to 3), so 0.1 + 0.1 + 0.1 never becomes 0.30000000000000004.

**Other fixes found while doing this**
- Refund now asks you to confirm the whole invoice and requires a reason.
- Scanning before the product list has loaded says "still loading" instead of "no product matches".
- Low-stock alerts ignore deactivated products.
- Receipts and invoices show up to 2 decimals (Rs 113.40 instead of Rs 113).

## Refunds (how they work)
Invoices → click the sale → "Refund this whole sale". The whole receipt is refunded: every item goes back into stock (REFUND rows in the ledger), and the sale is marked refunded and excluded from Reports. A sale can only be refunded once. There is deliberately no per-item refund.

## Upgrading
pos.db upgrades itself on start (adds the unit / active / cost columns and the change-history table). No data is lost. Delete the file to start fresh.

## Verified / not verified
Verified:
- Backend tests (`cd backend && python tests/test_backend.py`) cover all v6 flows plus catalog, price guards, deactivation, decimal stock, cart/set, weighed PO receiving and refunds.
- The upgrade was checked on a copy of your real pos.db.
- The frontend type-checks with `tsc -b`.
- The UI was driven in headless Chrome: catalog, weighed sale, refund, deactivation, purchase orders.

Not verified:
- The backend test run used a small stand-in for FastAPI/pydantic, because the packages couldn't be installed where I built this. Please run the tests once on your machine.
- `vite build` and `oxlint` couldn't run here (your node_modules has Windows binaries).
- No real barcode scanner or weighing scale.

## Still open
- No login/roles/PIN: anyone with the URL can now also change prices. Price history records what changed, not who. Security is the next priority.
- Cart totals use the live price: changing a price while a sale is open changes that open cart.
- Weights are typed in by hand (no scale hookup, no price-embedded barcodes).
- Refund is whole-receipt only and doesn't record cash paid back.
- Tax rate, discount codes and low-stock threshold are still hardcoded (Settings page).
- Dashboard stock value uses selling price, not cost.
