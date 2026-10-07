# v10.1: Security (login, roles, PINs, who-did-what, restricted database login)

## v10.1 fixes (after your first test run on Windows)
- **start.bat now installs what is missing** (backend packages, frontend packages), checks/starts PostgreSQL, waits for the backend, opens the browser, and explains any problem in plain words. Fixes *'vite' is not recognized* for good: the zip ships without node_modules, the old launcher never installed them. `start.sh` does the same on Mac/Linux.
- **Passwords: a real one can now be set.** Add person and Reset password have a checkbox *must choose their own at next login* (ticked = starting password, as before; unticked = the password you type is their real password). Labels changed from "temporary password" to "starting password". `manage_users.py reset-password` asks the same question. A reset always logs the person out and their previous password stops working (tested).
- New `tests-ui/` browser click-through (25 checks) shipped with the project.


Closes the boss's gap #1 ("no user is recorded") and the remaining part of gap #5 ("anyone with the database password can edit history").

## What changed
**People & login**
- Every person has their own username + password (scrypt-hashed, never stored readable). Sessions are random tokens in an HttpOnly cookie; the database stores only their SHA-256.
- First start shows *First-time setup* to create the owner (only from the POS computer, only while there are no users). Owner adds managers/cashiers on the new **Security** page; new people must replace their temporary password at first login. **Account** page: change own password / PIN.
- 5 wrong passwords lock the account 10 min; unknown usernames and wrong passwords give the same message and take the same time. Deactivating a person, or changing a role, takes effect on their very next click; deactivated people can't log in or approve. The last active owner can't be deactivated or demoted; nobody can deactivate/demote themselves.
**Roles & permissions**
- cashier < manager < owner. One permission table in `auth.py` (`POLICY`) lists every route; **the app refuses to start if any route has no rule**, so a future endpoint can't be left open by accident. Unknown route/rule = denied. Tabs a role can't use are hidden, but the server enforces the same walls. Interactive API docs are switched off.
**PIN & approval**
- PIN (4–8 digits, easy ones like 1234/1111 refused) is asked at the moment of: refund, stock count, discount code, price/cost change, receive stock, cancel/close PO, mark purchase paid, void expense, all user management. A cashier can't do refund / stock count / discount alone: a manager or owner types **their username + PIN** on the cashier's screen; History records both ("approved by").
- PINs are stored as salted scrypt of an HMAC with a secret in `backend/pos_secret.key` (not in the database or backups): a stolen database/backup alone can't be used to guess PINs offline. 5 wrong PINs lock that PIN for 5 minutes (counted correctly even under parallel guessing). Cancelling the PIN box changes nothing.
**Who did it, everywhere**
- History (`actor`, `approved_by`), stock ledger (`movements.actor`), invoices (`sales.actor`, "Served by", receipt line). Logins, logouts, failed logins, lockouts, wrong PINs and every people change are written to History too (new `auth.*` / `user.*` events). Rows from before v10 keep actor `system` (shown as "before login").
**Database lock-down (optional but recommended)**
- `secure_db.py` creates a restricted login `pos_app`: can run the POS, but by permission cannot edit/delete History / ledger / product log, delete sales/products/people, change or drop tables, TRUNCATE, or disable the append-only triggers. It proves this by trying, then writes `DATABASE_URL` into `.env`. `init_db.py` (owner) upgrades tables when the app runs restricted; `manage_users.py` recovers lost owner passwords/PINs.
**Other hardening**
- CORS: only this computer and private-network addresses (was `*`); POSTs need an `X-POS` header (blocks cross-site forms). Frontend now talks to `http://<this host>:8000`.
- DB schema additions are `ADD COLUMN IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS`: a v9 database upgrades itself on first start (old data kept).

## Verified (by Claude, real PostgreSQL 16)
- `test_backend.py` (all v6–v9 behaviour, now logged in as owner): passes. `test_security.py` (new): every route refuses anonymous access; role walls route-by-route; first-setup rules; weak password/PIN rules; forced password change; wrong/locked logins and PINs incl. 14 parallel guesses (exactly 4 counted, 5th locks); approval flow (wrong/unknown/cashier/self approver, deactivated approver); discount/price/receive/pay/cancel/void need PIN and a refused PIN changes nothing; sessions (logout, replay, expiry, password change logs out other devices, token not stored raw); last-owner and self-protection rules; two cashiers working at once are attributed correctly; hashes salted, PIN useless with a different secret; CORS allow/deny; append-only guards still hold. `test_hardened.py` (new): app flow as `pos_app` (sell, refund, adjust, price, PO receive, expense void, users), 10 forbidden actions refused, backup works as `pos_app`, structure-upgrade message and `init_db.py`.
- **Upgrade of a real v9 database (built with your v9 code, with sales/refund/adjust/expense data) opened by v10:** all data kept, old rows tagged `system`, new activity attributed.
- Frontend: `tsc -b` clean, `vite build` clean, `oxlint` 0 errors (4 hot-reload warnings). Simulated-browser click-through (jsdom) against live uvicorn + PostgreSQL: **20 checks** (setup, role-limited tabs, PIN box incl. wrong PIN + retry keeps manager username, forced password change, cashier sells, manager-approved refund, discount approval + cancel, History shows both people, account PIN change, session-expired screen, live updates over the login cookie).

## NOT verified: needs your testing
- A real browser (look/feel of login, PIN box, Security page); two tills at once.
- **Windows**: `secure_db.py`, `manage_users.py`, scrypt on Python 3.14 (standard OpenSSL build, should be fine), `pos_secret.key` creation, `start.bat`. **PostgreSQL 18** (tested on 16). Docker.
- `migrate_sqlite_to_postgres.py` after this change (not re-run; it only writes named columns, so it should be unaffected).
- Tills reached over the shop network: untested, and traffic is plain HTTP (passwords/PINs readable by someone on that network): for a real multi-till setup add HTTPS first.

## Known limits (honest)
- Anyone who has the owner `pos` / `postgres` password can still do anything: keep those out of `.env`, off the till PCs.
- Anyone who can open the POS computer's files can read `.env` (the restricted `pos_app` password) and `pos_secret.key`. Restricted ≠ unbreakable.
- No automatic idle lock (a logged-in till stays logged in for 12 h; log out when leaving). Cart is still one shared cart (no per-till carts). Settings page still a placeholder. A cashier can still remove items from a cart without approval (stock is returned and the ledger records it, with their name).
- Wrong-PIN lockout also applies to the person being used as approver: someone could deliberately lock a manager's PIN for 5 minutes (owner can unlock).
