# v11 — new access model

**Why:** v10 gave every person a password AND a PIN plus manager approval; the temporary-password flow caused real trouble. v11 simplifies to what a shop actually does, and adds the missing security layers.

## Changed
- Passwords only for owners (managers = owners). Employees: name tile + own PIN. Only an owner sets/changes PINs.
- Refund, discount code, stock count: the **employee's own PIN** (no manager approval). Owners need no PIN.
- New roles: **developer** (CLI-created, hidden, full power, mandatory 2FA) and **guest** (read-only demo, off by default).
- Upgrade of a v10 database is automatic: manager→owner, cashier→employee, employees lose the password, owners lose the PIN.
- Removed: temporary/forced password change, manager approval box, `/auth/pin`, `must_change` flows.

## Added
- **1-minute inactivity lock** (screen + server), same-person unlock, PIN boxes die with the lock.
- **2FA** (authenticator app) with one-time recovery codes; `manage_users.py reset-2fa`.
- **Ordered request pipeline** (`pipeline.py`): network → identity → lock → permission → PIN → action → record; alerts announced first on the live stream.
- **Network policy**: shop network only; owner-chosen approved connection (Settings → Network); critical alert when it breaks; developer override.
- **Alerts** with explicit acknowledgement (who/when) that stay until fixed.
- **Cash drawer** events and **shift cash-up** (count first, expected hidden from employees, difference to the owner + alert).
- **Card-machine credentials vault** (write-only, sealed).
- **Settings page**, **Developer page**, **Cash up page**, alert bar.
- Backend split into small modules (see CONTEXT.md §5 and docs/POS-Explained.md).

## Verified / not verified
See CONTEXT.md §13. In short: all three backend suites and a 31-check simulated-browser run pass on PostgreSQL 16; **not** verified on Windows / PostgreSQL 18 / real scanner, drawer, card machine, real Ethernet-Wi-Fi switching, a real authenticator app.
