# v1.1 — card security

- Encryption of small secrets (2FA seed, card machine credentials) is now AES-256-GCM instead of the home-made cipher.
  Old values still open; new ones are written as `seal2$`. The vault name is bound to each value.
- `keyescrow.py`: a locked copy of the secret key in the database, opened only by a recovery code that is shown once.
  `create`, `restore`, `status`. A stolen database alone is useless.
- Card numbers can no longer be typed into notes or reasons (refused with a message, nothing saved).
- Removed extra hint text from End of day, Profit and Reports pages.
- `.env.example` explains hidden developer settings versus shop settings kept encrypted in the database.
- New test: `backend/tests/test_card_security.py`.
- Security headers on every API answer (no-store, nosniff, no framing, no referrer, locked-down content policy; HSTS once HTTPS is on).
- Frontend dev dependencies updated (`npm audit fix`: Vite 8.3.5 and source-map-js); `npm audit` reports 0 vulnerabilities.
- `.gitignore` now also keeps the HTTPS private key (`backend/certs/`, `*.pem`, `*.key`), database dumps and other `.env` files out of git.
