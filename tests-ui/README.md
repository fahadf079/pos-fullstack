# Browser click-through test (simulated browser, real backend + PostgreSQL)

Drives the built screen like a person: setup, login, role-limited tabs, PIN pad, employee-own-PIN refunds, 2FA, the screen lock, cash-up, History, session expiry.

1. `cd frontend && npm install && npm run build`
2. Throw-away database (name ends in `_test`), EMPTY. Start the backend against it, e.g. in `backend\`:
   `set DATABASE_URL=postgresql://pos:pos@localhost:5432/pos_test` and `set POS_SECRET_KEY=ui-test` then `python -m uvicorn main:app --port 8000`
3. `cd tests-ui && npm install jsdom && node ui-clickthrough.mjs`   (needs a FRESH database every run: it creates the owner)
Expected last line: `UI CLICK-THROUGH: 35 CHECKS PASSED` (or similar; no FAILED line).
