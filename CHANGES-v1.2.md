# v1.2 — security release

- This tag marks the security hardening described in CHANGES-v1.1.md (the v1.1 tag points at the earlier text cleanup).
- Fix: `python keyescrow.py status` crashed when printing the date. It now shows when the locked key copy was made.
- Test added for `keyescrow.py status`.
