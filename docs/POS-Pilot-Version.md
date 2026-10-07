# POS — Pilot Version (MVP) · Prototype v1.0

**What this is:** the first version you put in front of the client's staff in the real mart. It is not a cut-down copy: it is the full Prototype v1.0 code run with the safe starting settings below. Selling, inventory, catalog, purchasing, refunds, History, backups and the restricted database login are all included. The interface is being finished separately; this version fixes the logic, the people model and the protection around them.

## What the pilot includes
| Area | In the pilot |
|---|---|
| Selling | Scan → stock drops at once; checkout; kg/l items; discount codes; per-item refunds |
| Inventory | Live stock, stock ledger with who moved it, stock counts, low-stock |
| Products | Catalog (add/edit/deactivate), price history from v7 on |
| Purchasing | Suppliers, purchase orders, partial receiving, purchases, expenses (void, never delete), reports |
| People | Owner (password + 2FA) who can add, rename and remove employees; employees (name tile + own PIN); developer (hidden); guest (off). A new installation has no people: the first screen creates the owner. |
| Safety | 1-minute lock, network shop-only, critical alerts, History append-only, daily backup |
| Cash | Drawer opens only for a cash sale; shift count with expected hidden from employees |

## Safe starting settings
- Guest button: **off**. "Cash sales need an open shift": **off** at first, switch **on** once staff are used to Cash up.
- Network: approved connection **empty** (any shop-network connection) while testing; choose the Ethernet address in Settings → Network before the real opening.
- Employees may open the drawer by hand: **off**.
- Cash difference alert limit: Rs 100.

## Why the structure helps (so products and rules are picked up easily)
Each job has ONE home, so you never hunt through a big file:

| Job | Home |
|---|---|
| What a person may do | `policy.py` (one table) |
| Order checks run in | `pipeline.py` |
| Selling and stock | `main.py` (`move()` is the single place stock changes) |
| Products | `catalog.py` — a new product or price rule is added here and nowhere else |
| Supplier stock-in | `purchasing.py` |
| Sign-in / lock / 2FA | `auth.py`, `sessions.py`, `totp.py` |
| Network and alerts | `netpolicy.py`, `alerts.py` |
| Cash | `cash.py` |
| Secrets | `hashing.py`, `cardvault.py` |

When a product is added in the Catalog, every other part (scan box, cart, invoices, purchasing, reports, History) reads it from the same `products` table, so nothing has to be copied or kept in step by hand.

## Pilot checklist (do in this order)
1. Unzip, double-click `start.bat`, create the owner on the first screen, switch on two-step sign-in, write the recovery codes on paper.
2. Add the employees with PINs (Security). Give each their PIN privately. Names and usernames can be changed, and an employee can be removed, from the same page.
3. Add a few real products (Catalog), receive opening stock through a purchase order (Purchasing).
4. Sell with the real barcode scanner for several minutes: the screen must NOT lock while scanning; it must lock after a minute of nothing.
5. Refund and discount with an employee PIN.
6. Run `secure_db.py` once, run `backup.bat`, test a restore.
7. Pull the network cable on purpose and watch the alert; fix it from Settings → Network.
8. Cash up at the end of a day with the real drawer count.

## Not part of the pilot (planned next)
HTTPS and tills on other computers · real card-machine driver · approval for removing cart items · tax/discount settings page · profit reports using cost · supplier returns · bulk import.

## Honest limits
Tested in a sandbox on PostgreSQL 16. Not yet tested on the shop's Windows PC with PostgreSQL 18, a real scanner, printer, cash drawer, card machine, real Ethernet/Wi-Fi switching or a real authenticator app. Report anything odd exactly as it appears on screen.


## Before the pilot starts
Also decide: (1) turn on HTTPS (`make_cert.py`) if more than one computer will connect; (2) set tax %, discount codes, shop name and receipt footer in Settings; (3) set a cost for every product so the Profit page is meaningful; (4) decide whether removing cart items needs a PIN and whether owners must use 2FA; (5) staff record every drop and payout in Cash up so the count matches. Still to be checked on the real shop hardware: printer, scanner, scale, drawer, card machine.
