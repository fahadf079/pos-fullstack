"""
Emergency / admin tool for logins, run on the POS computer (no browser needed). Use it when the owner forgot their
password or PIN, or locked themselves out. Every use is written to the History (person "cli").

    python manage_users.py list
    python manage_users.py create-owner USERNAME      (asks for a password and a PIN)
    python manage_users.py reset-password USERNAME    (asks for a NEW password, and whether they must change it at next login)
    python manage_users.py reset-pin USERNAME         (asks for a NEW PIN)
    python manage_users.py unlock USERNAME            (clears password/PIN lockouts)
    python manage_users.py activate USERNAME          (re-enable a deactivated person)
"""
import getpass
import sys
from pathlib import Path

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth  # noqa: E402
import database  # noqa: E402
from database import db, transaction  # noqa: E402


def ask(label: str) -> str:
    a = getpass.getpass(f"{label}: ")
    if getpass.getpass(f"{label} (again): ") != a:
        raise SystemExit("They didn't match. Nothing was changed.")
    return a


def find(name: str):
    r = db.execute("SELECT * FROM users WHERE lower(username)=lower(?) FOR UPDATE", (name,)).fetchone()
    if not r:
        raise SystemExit(f'No person called "{name}". Try:  python manage_users.py list')
    return r


def run(cmd: str, name: str | None) -> None:
    if cmd == "create-owner":
        database.init()
    with transaction():
        if cmd == "list":
            for r in db.execute("SELECT username, role, active, COALESCE(locked_until > now(), false) AS locked FROM users ORDER BY id").fetchall():
                print(f"{r['username']:<20} {r['role']:<8} {'active' if r['active'] else 'DEACTIVATED'}{'  LOCKED' if r['locked'] else ''}")
        elif cmd == "create-owner":
            auth.create_user(name, "", "owner", ask("Password"), ask("PIN (4-8 digits)"), must_change=False, actor="cli")
            print(f"Owner {name} created.")
        elif cmd == "reset-password":
            r = find(name); pw = ask("New password"); auth.check_password(pw, r["username"])
            must = input("Must they choose their own password at next login? [Y/n]: ").strip().lower() not in ("n", "no")
            db.execute("UPDATE users SET pw_hash=?, must_change=?, failed_logins=0, locked_until=NULL WHERE id=?", (auth.hash_password(pw), 1 if must else 0, r["id"]))
            db.execute("DELETE FROM sessions WHERE user_id=?", (r["id"],))
            database.audit("user.password_reset", "user", r["username"], f"Password of {r['username']} reset from the command line ({'must change at next login' if must else 'permanent'})", {"must_change": must}, actor="cli")
            print("Done." + (" They must choose a new password at their next login." if must else " It is their password as it is."))
        elif cmd == "reset-pin":
            r = find(name); pin = auth.check_pin(ask("New PIN"))
            db.execute("UPDATE users SET pin_hash=?, pin_failed=0, pin_locked_until=NULL WHERE id=?", (auth.hash_pin(pin), r["id"]))
            database.audit("user.pin_reset", "user", r["username"], f"PIN of {r['username']} reset from the command line", actor="cli")
            print("Done.")
        elif cmd == "unlock":
            r = find(name)
            db.execute("UPDATE users SET failed_logins=0, locked_until=NULL, pin_failed=0, pin_locked_until=NULL WHERE id=?", (r["id"],))
            database.audit("user.unlocked", "user", r["username"], f"{r['username']} unlocked from the command line", actor="cli")
            print("Unlocked.")
        elif cmd == "activate":
            r = find(name)
            db.execute("UPDATE users SET active=1 WHERE id=?", (r["id"],))
            database.audit("user.activated", "user", r["username"], f"{r['username']} reactivated from the command line", actor="cli")
            print("Reactivated.")


def main() -> int:
    argv = sys.argv[1:]
    cmds = {"list": 0, "create-owner": 1, "reset-password": 1, "reset-pin": 1, "unlock": 1, "activate": 1}
    if not argv or argv[0] not in cmds or len(argv) != 1 + cmds[argv[0]]:
        print(__doc__); return 2
    try:
        run(argv[0], argv[1] if len(argv) > 1 else None)
    except HTTPException as e:
        print(f"Not done: {e.detail}"); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
