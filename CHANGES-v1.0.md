# Prototype v1.0 — final changes before the interface work

- **Fresh start:** no people, no sign-ins and no personal or placeholder names in the code, docs or tests. A new installation shows the first-time setup screen, which creates the owner.
- **Minimal text:** the explanatory sentences and sub-headings were removed from every screen (sign-in, setup, lock, Security, Account, Settings, Catalog, Inventory, Cash up, Reports, History). Labels, buttons, data, errors and confirmations stay.
- **Owner controls over people (Security page):** change a person's name and username, remove an employee. A removed employee can no longer sign in, the name can be reused, and past sales, stock movements and History keep the name used at the time. Not for owners; refused while the employee has a shift open.
- **New checks:** `backend/tests/check_scan_and_card.py` (scanner, card/wallet/cash sale, refund, card vault, pipeline order, no stalls with dummy values) and `backend/tests/test_people.py` (rename and remove).
- **Docs:** README (status, roles, flows, current screenshots), Pilot Version and Explained documents brought up to date.
- **Database:** one new column, `users.removed`, added automatically on start. If the app runs as the restricted `pos_app` login, run `python init_db.py --admin-url postgresql://OWNER:PASSWORD@localhost:5432/pos` once.
