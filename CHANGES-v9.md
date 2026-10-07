# v9 — PostgreSQL, History tab, expense voiding, automatic backups

**Why:** your boss's gap list — no "who", expenses hard-deletable, no backups, file-based database anyone can edit.
The whole app now runs on **PostgreSQL** (SQLite is gone, not split: stock, ledger and history must commit together).

## How each gap is handled
| Gap | What v9 does | What it does NOT do |
|---|---|---|
| History in one place | New **History** tab: every sale, refund, stock count, product change, PO, delivery, supplier payment, expense, supplier edit. Filter by event type, search, date range, expand for details. Written in the SAME transaction as the change (a rolled-back change leaves no history). | Cart scans/voids stay in the movements ledger (Inventory), not in History, to keep it readable. |
| No "who" | History rows already have `user_id` / `actor` columns. Shown as "—" until there is a login. | **Nothing records who yet — that needs the Security step (login/roles).** |
| Expenses hard-deleted | `Delete` is gone. **Void** needs a reason, keeps the row (when + why), removes it from every total. "Show voided" reveals them. The old `/expenses/{id}/delete` endpoint no longer exists. | |
| No automatic backups | `backup.py` / `backup.bat` / `schedule-backup.bat`: daily 02:00 dump, verified after writing, newest 14 kept. | Backups on the same disk don't survive a dead disk — copy them elsewhere. |
| Edit-the-file tampering | No file to open any more: the database needs a password. The database itself **refuses UPDATE/DELETE/TRUNCATE** on `audit_log`, `movements` and `product_log`. Stock can't go negative even for code bypassing the app (CHECK constraint). | Anyone with the database password / the owner account can still change or drop things (owners can disable triggers). Real tamper-resistance = per-person logins + a separate restricted DB role for the app. |
| No price history before v7 | Unchanged — old prices exist only inside old invoices. | Can't be recovered. |

## Other changes
- **Real transactions + row locks** replace the single shared connection and global lock. Verified by tests: 12 tills scanning the last unit → exactly one wins; 6 simultaneous refunds of one sale → stock returns once; 8 simultaneous deliveries of one PO → received once; a mixed storm of scans/removes/clears/checkouts → no errors, every unit accounted for. Deadlock victims are retried automatically.
- **Bug found and fixed while testing:** *Clear cart* used to delete items another till had scanned at that instant without returning their stock. Cart changes now take a table lock first (Clear/Checkout exclusively).
- **Bug found and fixed:** reports/"today" used the server PC's date while timestamps use the shop clock; now everything uses `POS_TZ` (default Asia/Karachi).
- Live-update push now fires only AFTER the commit (screens never refetch half-saved data).
- Proper types: `NUMERIC` money (2 dp) and quantities (3 dp), `TIMESTAMPTZ`, `JSONB` invoice items.
- Reports run on a consistent snapshot; invoice list no longer re-reads every invoice (N+1 removed).
- Same API otherwise; frontend: new History tab, Void instead of Delete.

## Files
New: `backend/database.py`, `backend/history.py`, `backend/migrate_sqlite_to_postgres.py`, `backend/backup.py`, `backend/.env.example`, `docker-compose.yml`, `backup.bat`, `schedule-backup.bat`, `frontend/src/HistoryPage.tsx`, `history.css`.
Changed: `backend/main.py`, `catalog.py`, `purchasing.py`, `requirements.txt` (adds psycopg, tzdata), `tests/test_backend.py`, `frontend/src/App.tsx`, `PurchasingPages.tsx`, `start.bat`, `start.sh`, `README.md`.

## Verified (real PostgreSQL 16, real FastAPI 0.142 / psycopg 3.3)
- `tests/test_backend.py` passes — all v6/v7/v8 flows ported + History, voiding, append-only guards, CHECK constraint, rollback-leaves-no-history, concurrency races above. Run 9 times in a row, and once as a plain non-superuser database owner.
- Migration: built a real v8 SQLite database with your original v8 code (suppliers, PO partial receive, new kg product, price change, deactivation, sales with discount, partial + whole refunds, an old-style whole refund with empty money columns, a cart in progress, expenses), migrated it: all 12 tables, money/stock/ledger totals match, then used it in the live app (partial refund completed to the exact total, numbering continued: next sale 5, next PO-00003, History shows 17 migrated + new lines). Re-running refuses (target not empty).
- Backup: real `pg_dump` → verified → rotation (kept 3 of 4) → **restored into a fresh database, counts and stock identical**. Failure (database missing) reports a clear message, exit code 1, leaves no partial file.
- Real HTTP against running uvicorn: History endpoints, FastAPI validation text, SSE stream.
- Frontend: `tsc -b` and `vite build` clean; `oxlint src` 0 errors (1 old warning in your `useData` hook, unchanged). **UI click-through of the real App in a simulated browser (jsdom) against live uvicorn + Postgres: 24 checks** (History: rows, filters, search, expand, empty state, clear; Reports: void with/without reason, show voided; every other page renders; PART-REFUNDED badge).

## NOT verified — please test on your PC
- **A real browser** (jsdom is not Chrome): layout/CSS of the History tab, and the two-tab live refresh (the live stream was tested with the real server, but the UI test used a stub).
- **Windows specifics:** `start.bat`, `schedule-backup.bat` (Task Scheduler), `backup.bat`, native PostgreSQL install steps, `pg_dump` being on PATH (installing PostgreSQL's command-line tools adds it; otherwise add its `bin` folder to PATH).
- **Docker:** `docker-compose.yml` and the Docker fallback in `backup.py` (no Docker here; the YAML parses and the settings are standard).
- **Your real `pos.db`** (I tested on a v8-generated one). Do the `--dry-run` first; it saves nothing.
- Real barcode scanner / weighing scale (as before). Several uvicorn workers (not supported — run one).
- The browser's computer should be in the shop's time zone (it is for Lahore); the date pickers use the browser clock, stored times use `POS_TZ`.

## Next (unchanged advice)
**Security** (login, owner/manager/cashier, PIN for refunds/adjustments/price changes) — it fills the "who" in every History row and lets the app run under a restricted database role. Then Settings.
