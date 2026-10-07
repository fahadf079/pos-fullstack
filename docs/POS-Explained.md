# POS — How it works, module by module

Plain words first, function names after.

## 1. The idea in one paragraph
A cashier scans; stock drops at that instant. Every request first passes a fixed row of safety checks (the **pipeline**), then does its work, and the work and its history line are saved together or not at all. People prove who they are once (owner: password + 2FA; employee: name + PIN), the screen locks after a minute of nothing, and every change names the person who made it. Anything that goes wrong with the network raises an alert that cannot be ignored.

## 2. The path of one request (pipeline.py)
`guard()` runs before every endpoint, in this order (first five are HIGH priority gates):
1. **network** — `netpolicy.check`: public internet refused; if the owner picked an approved connection, other connections refused; this computer itself always allowed.
2. **identity** — needs the `X-POS` header on writes (blocks cross-site forms) and a valid session (`sessions.resolve`); a deactivated person is refused on their next click.
3. **lock** — a locked screen gets `423` for everything except unlock / sign out.
4. **permission** — `policy.POLICY` says the lowest role that may call the route.
5. **confirmation** — if the rule wants a PIN, `credentials.confirm` checks the employee's own PIN (owners are exempt: their password already proved them).
6. **action** — the endpoint itself. 7. **record** — History/ledger row in the same database transaction.
Why this order: a locked or foreign request is stopped before it can touch any business code; a scan can never run ahead of an alert or a lock. Tests switch `pipeline.TRACE` on and assert the exact order.
The live stream follows the same idea: `event: alert` is sent before ordinary stock/sale ticks.

## 3. Modules and their functions

### Business (unchanged logic)
- **main.py** — selling. `move()` is the ONLY place stock changes (locks the product, refuses negative stock, writes the ledger row). Endpoints: scan, cart remove/set/clear, checkout, refunds (whole or per item), stock adjust, sales, movements, dashboard, `/events` live stream. It wires every module in and calls `policy.install(app)` last. New in v11: a Cash checkout records a drawer event (after commit) and can require an open shift; refunds record who did them.
- **catalog.py** — products: add, edit, deactivate, per-product history. Changing price or cost calls `credentials.confirm("self")`.
- **purchasing.py** — suppliers, purchase orders, receiving stock (creates ledger rows), purchases, expenses (void with reason), reports.
- **history.py** — read-only History with filters (owner only). **database.py** — pool, transactions (`transaction`, `independent`, `after_commit`), `audit()`, schema and the v10→v11 upgrade (`MIGRATE`), append-only triggers, restricted-login grants.

### Who may do what
- **policy.py** — the single permission table, `PUBLIC`, `LOCKED_OK`, and `install()` which refuses to start if a route has no rule.
- **credentials.py** — `pin_attempt` (checks an employee PIN, counts wrong tries on its own connection, locks after 5), `confirm` (the "type your PIN" rule), `check_my_password` (re-asks an owner's password for sensitive actions).
- **context.py** — remembers who is making the current request.

### Sign-in
- **auth.py** — `GET /auth/status`, `/auth/people` (name tiles), `/auth/setup` (first owner, from this computer only), `/auth/pin-login`, `/auth/login` (password + 2FA code), `/auth/guest`, logout, `/auth/lock`, `/auth/unlock`, `/auth/activity`, change own password, 2FA begin/confirm/disable. Unknown name, wrong password, wrong code and deactivated person all get the same message and take the same time.
- **sessions.py** — `start`, `resolve` (also applies the server-side idle lock), `touch` (activity), `set_locked`. Only a hash of the cookie token is stored.
- **totp.py** — authenticator codes (RFC 6238), each code usable once, recovery codes. **hashing.py** — scrypt hashes, the secret "pepper" for PINs, `seal`/`unseal` (encrypt-then-MAC) for the 2FA seed and card credentials, rules for passwords/PINs/names.
- **users.py** — `create_user` and the Security routes: list, add employee/owner, rename (name and username), remove an employee, reset another owner's password, set an employee's PIN, activate/deactivate, unlock, permission table. Removing an employee wipes their login and frees the name but keeps the database row, so past sales and History still point at them; a removed person disappears from every list. The last active owner can't be deactivated; developer rows are invisible here.

### Protection and awareness
- **netpolicy.py** — `check` (the network gate), `local_ips`, `is_present`, `missing_interfaces`, routes to view/choose the approved connection (password re-asked; only real addresses of this computer can be saved, so a typo can't lock the tills out), developer override.
- **alerts.py** — `raise_alert` (one open alert per problem, repeats just count), `resolve_alert`, `active`, acknowledge (who/when), `monitor_once` (network, protection-off, stale backup) run every 10 s by `monitor_loop`. A critical alert stays until fixed AND acknowledged.
- **settings.py** — owner switches with validators (guest, require shift, employee drawer, difference limit); cached 2 s.

### Money handling
- **cash.py** — `open_for_sale` and manual `/drawer/open` (every opening logged in `drawer_events`; hardware pulse after commit); shifts: open with float, close by entering the counted cash — expected cash is computed only afterwards on the server and shown to owners only; differences above the limit raise an alert (no figures in its text) until an owner reviews.
- **cardvault.py** — card credentials: write-only, sealed in the database, `use()` for the server-side driver only; never in any response, History line or log.

## 4. How the pieces work together (examples)
- **Employee refunds a sale:** guard (network ✓, session ✓, not locked ✓, role employee ✓, PIN rule) → PIN box appears on screen → correct PIN → `refund` returns stock through `move()`, writes the refund row with their name, History line saved in the same transaction.
- **Cable pulled:** every 10 s `monitor_once` asks `netpolicy.missing_interfaces`; after 2 misses `raise_alert` stores a critical alert, bumps the alert version, the live stream announces it first, every screen shows the red bar; someone acknowledges (recorded); the owner picks the current address in Settings, the checker clears it at once.
- **Idle till:** 60 s without a key, tap or scan → screen locks and PIN boxes close; the server independently locks at 80 s; only that employee's PIN (or owner's password) unlocks; the cart is untouched.
- **Cash-up:** employee counts, types the number, submits; server computes float + cash sales − cash refunds; owner sees expected and difference; a big difference raises a warning alert.

## 5. Robustness choices
- Fail closed: an unlisted route is refused; an unparsable caller address is refused; a failed check never "falls through".
- Every write is one database transaction; a refused PIN or failed step changes nothing. Wrong-PIN counters use their own connection so they survive the failed request.
- Concurrency: row locks on products, carts, users; tests run 12 tills on the last unit and 14 parallel PIN guesses.
- History is append-only (triggers; with `secure_db.py` also permissions); no route edits or deletes it.
- Secrets never sit readable: hashes, sealed seeds, a pepper outside the database (keep `backend/pos_secret.key` backed up separately).
- The background checker can never crash the POS (errors are caught and shown in Developer).
- Safe to repeat: schema upgrade runs on every start; v10 databases are upgraded automatically.

## 6. New in v12 (plain words)
- **profit.py** — `profit_report` adds up sales without tax, takes off refunds, then subtracts what the goods cost. Each sale now remembers the item's cost at that moment, so changing a cost later does not rewrite old profit.
- **cash.py** — `cash_move` records drops, payouts and additions in `shift_moves` (append-only) and the expected cash counts them; `day_report` is the end-of-day page; refunds now record how the money went back (`paid_via`), and only Cash refunds lower the drawer.
- **settings.py** — tax, low-stock number, discount codes, shop name, receipt footer and the new switches (PIN to remove cart items, compulsory owner 2FA, 2FA at unlock, weighed-item barcodes) are owner settings with validators; `/shop` gives the till the harmless ones.
- **auth.py** — `_throttled` / `_note_failure`: one computer failing too often waits; unlock can ask the owner's 2FA code.
- **main.py** — `scale_label` reads a price-computing scale label (checks the check digit, takes the weight from the label); `public_product` hides cost from employees.
- **catalog.py** — `import_products` (all-or-nothing, dry-run first) and `next_barcode`.
- **netpolicy.py** — `adapter_info` labels each address Wi-Fi or Ethernet (best effort).
- **make_cert.py** — makes the HTTPS certificate; the launchers switch to https when it exists.
