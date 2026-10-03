# POS — Real-Time Inventory Point of Sale

A web-based point-of-sale system for a grocery store / mart, built around **real-time inventory**: scanning a barcode deducts stock the moment the item is added to the cart, receiving stock adds it through purchase orders, and every change is written to an append-only ledger and History.

Runs on **localhost**. Backend: Python / FastAPI / PostgreSQL. Frontend: React / TypeScript / Vite.

---

## Screenshots

| | |
|---|---|
| **Dashboard (sell screen)** ![Dashboard](docs/screenshots/01-dashboard.png) | **Weighed item (kg / l)** ![Weighed item](docs/screenshots/02-weighed-item.png) |
| **Inventory + live stock movements** ![Inventory](docs/screenshots/03-inventory.png) | **Catalog** ![Catalog](docs/screenshots/04-catalog.png) |
| **Purchasing: orders and receiving** ![Purchasing](docs/screenshots/05-purchasing.png) | **Invoices and partial refund** ![Invoices](docs/screenshots/06-invoices-refund.png) |
| **Reports and expenses** ![Reports](docs/screenshots/07-reports.png) | **History (who did what)** ![History](docs/screenshots/08-history.png) |
| **Login** ![Login](docs/screenshots/09-login.png) | **Manager approval (username + PIN)** ![Approval](docs/screenshots/10-pin-approval.png) |
| **Security: people and roles** ![Security](docs/screenshots/11-security.png) | **Account: own password and PIN** ![Account](docs/screenshots/12-account.png) |

---

## Features

**Selling**
- Deduct-on-scan: stock drops as soon as an item is scanned; removing it from the cart or clearing the cart returns it.
- Scan by barcode (SKU) or search by name; pieces (whole numbers) and weighed items (kg / l, up to 3 decimals).
- Cash / Card / Wallet, tax 8%, discount codes, itemised receipt showing who served the customer.
- Per-item and partial refunds; stock is restored exactly and refunds always add up to the invoice total.

**Inventory**
- Single choke point for every stock change; stock can never go negative (checked in the app and by a database constraint).
- Live stock-movement ledger (scan, void, receive, adjust, refund) with before/after values and the person responsible.
- Live updates on every open screen (Server-Sent Events).

**Catalog**
- Add and edit products, price/cost with live margin, below-cost warnings, deactivate/reactivate, per-product price history. Barcode is immutable and price must be above zero.

**Purchasing**
- Suppliers, purchase orders, partial receiving by barcode, purchases (paid/unpaid), expenses (voided with a reason, never deleted), and reports (net sales, refunds, purchases, expenses, cash-flow, unpaid to suppliers, top products).

**History (audit log)**
- Every business event is written in the same transaction as the change, append-only, filterable by type, text and date, and shows the person and the approver.

**Security**
- Personal logins, three roles (cashier, manager, owner), PIN for sensitive actions, manager approval on a cashier's screen, lockouts, a central permission table that stops the app from starting if any route has no rule, and an optional restricted database login that cannot edit history.

**Operations**
- Daily automatic backups, one-time SQLite to PostgreSQL migration tool, recovery tool for forgotten passwords/PINs, self-installing launcher.

---

## Quick start (Windows)

### 1. PostgreSQL (one time)

Install PostgreSQL, then in PowerShell (replace `18` with your installed version):

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -c "CREATE USER pos WITH PASSWORD 'pos';"
```

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -c "CREATE DATABASE pos OWNER pos;"
```

The password prompt shows nothing while you type. Type it and press Enter.
Docker alternative: `docker compose up -d db`.

**Before the real store opens, replace the default password `pos`** (and set it in `backend/.env`).

### 2. Run

Double-click **`start.bat`**. It checks Python and Node.js, installs missing backend and frontend packages, makes sure PostgreSQL is answering, starts the backend and the frontend in two windows, and opens http://localhost:5173. If anything is wrong it explains what to do in plain words.

Mac / Linux: `./start.sh`.

Manual start, in two terminals:

```
cd backend
python -m uvicorn main:app --reload --port 8000 --timeout-graceful-shutdown 2
```

```
cd frontend
npm install
npm run dev
```

Run the backend as **one process** (no `--workers`): the live-update counter lives in that process. A fresh database creates all tables and 10 sample products automatically.

### 3. First-time setup

The first time you open the app you see **First-time setup**. Create the **owner** (username, full name, password, 4 to 8 digit PIN). This is only possible while there are no users, and only from the POS computer itself. Then open **Security → Add a person** for each manager and cashier.

When adding a person or resetting a password, the box *"They must choose their own password"* is ticked by default (the password you type is only a starting password). Untick it and the password you type is their permanent password.

---

## Roles

| Role | Can do |
|---|---|
| **Cashier** | Sell (scan, cart, checkout), see products and stock, invoices, stock movements, own account |
| **Manager** | Everything a cashier can, plus Catalog, Purchasing, suppliers, expenses, Reports, History |
| **Owner** | Everything a manager can, plus the Security page: add and disable people, change roles, reset passwords and PINs, unlock accounts |

Tabs a role cannot use are hidden, and the server enforces the same rules. Nobody can change their own role or deactivate themselves, and the last active owner can never be removed.

## PIN and approval

A PIN is asked at the moment of: refund, stock adjustment, a discount code, changing a product's price or cost, receiving stock, cancelling or closing a purchase order, marking a purchase paid, voiding an expense, and every user-management action.

A **cashier** cannot refund, adjust stock or apply a discount alone. A **manager or owner types their username and PIN** on the cashier's screen, and History records both people. Cancelling the PIN box changes nothing.

- 5 wrong passwords lock the account for 10 minutes.
- 5 wrong PINs lock that PIN for 5 minutes.
- The owner can unlock either from the Security page.
- Sessions last 12 hours.

PINs are stored as salted scrypt of an HMAC keyed with a secret in `backend/pos_secret.key`. **Keep a copy of that file somewhere safe.** If it is lost, passwords still work but every PIN must be reset.

**Forgot the owner password or PIN?** On the POS computer, in `backend\`:

```
python manage_users.py list
python manage_users.py reset-password USERNAME
python manage_users.py reset-pin USERNAME
python manage_users.py unlock USERNAME
```

---

## Lock the database down (recommended, once, for the real store)

By default the app connects as the database owner `pos`, so anyone who learns that password can edit history. This step creates a restricted login `pos_app` that can run the POS but **cannot** edit or delete History or the stock ledger, delete sales, products or people, change or drop tables, or switch the guards off.

Stop the backend, then in `backend\` (use the PostgreSQL administrator `postgres` password; percent-encode symbols, e.g. `@` = `%40`):

```
python secure_db.py --admin-url postgresql://postgres:YOUR_POSTGRES_PASSWORD@localhost:5432/pos
```

It proves the lock works by attempting 10 forbidden actions, then writes the new `DATABASE_URL` into `backend\.env`. Restart the backend. Keep the `postgres` and `pos` passwords **out of** `.env`. You need them only for:

```
python init_db.py --admin-url postgresql://pos:PASSWORD@localhost:5432/pos
```

(after an update that changes tables), restoring backups, and migrations.

---

## Bring existing data over (one time)

To copy an old SQLite `pos.db` into PostgreSQL (the target database must be empty), run from `backend\` with the backend **not** started:

```
python migrate_sqlite_to_postgres.py --sqlite pos.db --dry-run
```

```
python migrate_sqlite_to_postgres.py --sqlite pos.db
```

It keeps every id and number, builds History from past activity, then verifies row counts, money totals and the stock ledger. If any check fails, nothing is saved. Keep `pos.db` as a backup.

## Backups

- `backup.bat` takes a backup now (`backups\pos-DATE-TIME.dump`, newest 14 kept, each verified after writing).
- Run `schedule-backup.bat` once to have Windows do it daily at 02:00 (log in `backups\backup.log`).
- Restore into an empty database: `pg_restore --no-owner -d pos backups\pos-20261002-020000.dump`
- Backups do not contain `pos_secret.key`. Copy it separately.
- Copy the `backups` folder off the computer now and then. A backup on the same disk does not survive a dead disk.
- `pg_dump` must be on PATH (add PostgreSQL's `bin` folder), or Docker is used as a fallback.

---

## Configuration

Environment variables or `backend/.env` (see `backend/.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql://pos:pos@localhost:5432/pos` | Database login used by the app |
| `DATABASE_ADMIN_URL` | unset | Owner login used only for table work |
| `POS_APP_ROLE` | `pos_app` | Name of the restricted login |
| `POS_TZ` | `Asia/Karachi` | Shop time zone (decides what "today" means) |
| `POS_POOL_MAX` | `20` | Database connection pool size |
| `POS_BACKUP_KEEP` | `14` | Number of backups kept |
| `POS_SESSION_HOURS` | `12` | Login lifetime |
| `POS_CORS_ORIGIN_REGEX` | this computer + private networks | Which screens may talk to the backend |
| `POS_SECRET_KEY` | unset (file `pos_secret.key` is used) | Secret mixed into PINs |
| `POS_ALLOW_REMOTE_SETUP` | unset | Allow first-owner creation from another computer (not recommended) |

---

## Tests

Use a throw-away database whose name ends in `_test` (wiped on every run; the tests refuse any other name):

```
psql -U postgres -c "CREATE DATABASE pos_test OWNER pos;"
pip install httpx
```

Run one at a time, in `backend\`:

```
python tests/test_backend.py
python tests/test_security.py
python tests/test_hardened.py
```

`test_backend.py` covers selling, refunds, purchasing, History and concurrency. `test_security.py` covers login, roles, PINs, approval, lockouts, sessions and CORS. `test_hardened.py` covers the restricted `pos_app` login and needs `POS_TEST_ADMIN_URL` (the `postgres` login for `pos_test`).

A simulated-browser click-through (25 checks) is in `tests-ui/`; see its README.

---

## Project structure

```
pos-fullstack/
  start.bat  start.sh  backup.bat  schedule-backup.bat  docker-compose.yml  README.md
  backend/
    main.py  database.py  auth.py  catalog.py  purchasing.py  history.py
    secure_db.py  init_db.py  manage_users.py  backup.py  migrate_sqlite_to_postgres.py
    requirements.txt  .env.example
    tests/  test_backend.py  test_security.py  test_hardened.py
  frontend/
    src/  App.tsx  api.ts  auth.tsx  SecurityPage.tsx  CatalogPage.tsx
          PurchasingPages.tsx  HistoryPage.tsx  *.css
  tests-ui/  ui-clickthrough.mjs  README.md
  docs/screenshots/
```

## API overview

All reads are `GET`, all writes are `POST`. Every route needs a login except `GET /auth/status`, `POST /auth/setup` and `POST /auth/login`. Every non-GET request must send the header `X-POS: 1`.

| Area | Routes |
|---|---|
| Auth | `/auth/status`, `/auth/setup`, `/auth/login`, `/auth/logout`, `/auth/password`, `/auth/pin` |
| People (owner) | `/users`, `/users/{id}/update`, `/users/{id}/password`, `/users/{id}/pin`, `/users/{id}/active`, `/users/{id}/unlock`, `/security/policy` |
| Selling | `/inventory`, `/scan`, `/cart`, `/cart/remove`, `/cart/set`, `/cart/clear`, `/checkout`, `/inventory/adjust`, `/sales`, `/sales/{id}`, `/sales/{id}/refund`, `/movements`, `/dashboard`, `/events` |
| Catalog (manager) | `/catalog/meta`, `/catalog/products`, `/catalog/products/{id}/update`, `/catalog/products/{id}/active`, `/catalog/products/{id}/history` |
| Purchasing (manager) | `/suppliers`, `/purchase-orders` (`cancel`, `close`, `receive`), `/purchases` (`pay`), `/expenses` (`void`), `/reports/purchases` |
| History (manager) | `/history`, `/history/meta` |

Every route must have an entry in the permission table (`POLICY` in `backend/auth.py`). The app refuses to start otherwise.

---

## Known limitations

- Anyone with the `postgres` or owner `pos` password can still change anything. Anyone who can read the POS computer's files can read `.env` and `pos_secret.key`.
- Traffic is plain HTTP. Add HTTPS before using more than one till on a shop network.
- No idle auto-lock (a login lasts up to 12 hours). A cashier can remove cart items without approval.
- One global cart (not per till), no cart timeout, and cart totals use the live price until checkout.
- Weights are typed by hand (no scale hookup or weight barcodes). Refunds do not record cash handed back.
- Tax rate, low-stock threshold and discount codes are hardcoded. The Settings page is a placeholder.
- Cash-flow is not profit (no cost of goods per sale). Dashboard stock value uses selling price, not cost.
- A purchase order cannot be edited after creation. No supplier returns. No bulk import, barcode generation or product images.
- The live-update counter is in-process, so the backend must run as a single process.
- Not yet tested on real hardware: barcode scanner, weighing scale, tills over a network.
