# v12 — what changed (on top of v11)

## Fixed from the v11 gap list
**Security**
- **HTTPS**: `python make_cert.py` (in `backend\`) makes a certificate; once `backend\certs` exists, `start.bat` / `start.sh` serve the POS over `https://` and mark the sign-in cookie HTTPS-only. Browsers show a one-time warning (self-signed).
- **Sign-in throttle**: one computer that fails 30 sign-ins in 5 minutes must wait (`POS_THROTTLE_MAX`). A PIN that is right still waits. Asking for the 2FA code is not a failure.
- **Compulsory owner 2FA**: Settings switch. Owners without 2FA can only reach Account until it is set up.
- **2FA at unlock**: Settings switch. Off by default (a code every minute would be unusable).
- **QR code** for 2FA set-up (plus the text key).
- **Removing items from the cart** can need the employee's own PIN (Settings switch; includes lowering a weight and Clear cart).
- Employees and guests no longer receive product **cost** (inventory, scan response, invoices).

**Money and cash**
- **Refunds record how the money went back** (Cash / Card / Wallet; default = the way the customer paid). Cash-up uses it.
- **Cash drops, payouts and additions** during a shift (own PIN + reason). They are part of expected cash and appear in the owner's Cash up page.
- **End-of-day report**: takings by payment method, refunds by how they were paid back, shifts, cash moved.
- **Profit report**: revenue (no tax, after refunds) minus the cost of goods. A sale now remembers each item's cost at the time. Older sales use today's cost and the page says so. Also stock value at cost and at price.

**Settings instead of hardcoding**: tax %, low-stock number, discount codes, shop name, receipt footer.
**Receipts**: Print button (new sale and invoice copy) with shop name and footer.
**Catalog**: bulk import from CSV (checked first, all-or-nothing), in-shop barcode maker.
**Scale**: weighed-item labels (13 digits: 2x prefix, 5-digit item code, 5-digit grams, check digit) add the item at the printed weight when the Settings switch is on.
**Network**: the address list says Wi-Fi or Ethernet and the adapter name (labels only; rules still work by address).

## Database
Automatic on start: new table `shift_moves` (append-only), new column `refunds.paid_via`, new settings keys. A real v11 database with data was upgraded and checked.

## Still not done
See section 14 of CONTEXT.md.
