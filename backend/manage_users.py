"""
Emergency / admin tool for logins, run on the POS computer (no browser needed). Every use is written to the
History (person "cli"). Run it from the backend folder.

    python manage_users.py list
    python manage_users.py create-owner NAME         (asks for a password)
    python manage_users.py create-developer NAME     (asks for a password, then sets up the mandatory 2FA)
    python manage_users.py reset-password NAME       (owner / developer: asks for a NEW password)
    python manage_users.py set-pin NAME              (employee: asks for a NEW PIN)
    python manage_users.py reset-2fa NAME            (owner / developer: switches 2FA off; the owner sets it up again in Account.
                                                      A developer's 2FA is set up again straight away)
    python manage_users.py unlock NAME               (clears password/PIN lockouts)
    python manage_users.py activate NAME             (re-enable a deactivated person)
"""
import getpass
import sys
from pathlib import Path

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent))
import database  # noqa: E402
import hashing  # noqa: E402
import totp  # noqa: E402
import users  # noqa: E402
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


def enrol_2fa(uid: int, name: str) -> None:
    """Shows the 2FA seed and only switches 2FA on once the person has typed a working code (so nobody is locked out)."""
    secret = totp.new_secret()
    print(f"\nAdd this to the authenticator app (Google/Microsoft Authenticator, Aegis, …):\n  Account: {name}\n  Key:     {secret}\n  Link:    {totp.uri(secret, name)}\n")
    for _ in range(3):
        step = totp.verify(secret, input("Type the 6-digit code the app shows now: "), 0)
        if step is not None:
            db.execute("UPDATE users SET totp_secret=?, totp_enabled=1, totp_last_step=? WHERE id=?", (hashing.seal(secret), step, uid))
            database.audit("user.2fa_enabled", "user", name, f"2FA set up for {name} from the command line", actor="cli")
            print("2FA is on.")
            return
        print("That code is not right.")
    raise SystemExit("2FA was NOT set up. Nothing was changed for this person's 2FA.")


def run(cmd: str, name: str | None) -> None:
    if cmd in ("create-owner", "create-developer"):
        database.init()
    with transaction():
        if cmd == "list":
            for r in db.execute("SELECT username, role, active, totp_enabled, COALESCE(locked_until > now(), false) AS locked, removed FROM users ORDER BY id").fetchall():
                print(f"{r['username']:<20} {r['role']:<9} {'REMOVED' if r['removed'] else ('active' if r['active'] else 'DEACTIVATED')}{'  2FA' if r['totp_enabled'] else ''}{'  LOCKED' if r['locked'] else ''}")
        elif cmd in ("create-owner", "create-developer"):
            role = "owner" if cmd == "create-owner" else "developer"
            uid = users.create_user(name, "", role, ask("Password"), actor="cli")
            print(f"{role.capitalize()} {name} created.")
            if role == "developer":
                enrol_2fa(uid, name)
        elif cmd == "reset-password":
            r = find(name)
            if r["role"] not in ("owner", "developer"):
                raise SystemExit("Only an owner or developer has a password. For an employee use: set-pin")
            pw = hashing.check_password(ask("New password"), r["username"])
            db.execute("UPDATE users SET pw_hash=?, must_change=0, failed_logins=0, locked_until=NULL WHERE id=?", (hashing.hash_password(pw), r["id"]))
            db.execute("DELETE FROM sessions WHERE user_id=?", (r["id"],))
            database.audit("user.password_reset", "user", r["username"], f"Password of {r['username']} reset from the command line", actor="cli")
            print("Done. Their old password and open logins no longer work.")
        elif cmd == "set-pin":
            r = find(name)
            if r["role"] != "employee":
                raise SystemExit("Only an employee has a PIN. For an owner use: reset-password")
            pin = hashing.check_pin(ask("New PIN"))
            db.execute("UPDATE users SET pin_hash=?, pin_failed=0, pin_locked_until=NULL WHERE id=?", (hashing.hash_pin(pin), r["id"]))
            db.execute("DELETE FROM sessions WHERE user_id=?", (r["id"],))
            database.audit("user.pin_reset", "user", r["username"], f"PIN of {r['username']} set from the command line", actor="cli")
            print("Done.")
        elif cmd == "reset-2fa":
            r = find(name)
            if r["role"] not in ("owner", "developer"):
                raise SystemExit("Only an owner or developer has 2FA.")
            db.execute("UPDATE users SET totp_enabled=0, totp_secret=NULL, recovery_codes='[]'::jsonb, totp_last_step=0 WHERE id=?", (r["id"],))
            db.execute("DELETE FROM sessions WHERE user_id=?", (r["id"],))
            database.audit("user.2fa_reset", "user", r["username"], f"2FA of {r['username']} switched off from the command line", actor="cli")
            print("2FA is off for this person.")
            if r["role"] == "developer":
                enrol_2fa(r["id"], r["username"])
            else:
                print("They can set it up again under Account → Two-step sign-in.")
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
    cmds = {"list": 0, "create-owner": 1, "create-developer": 1, "reset-password": 1, "set-pin": 1, "reset-2fa": 1, "unlock": 1, "activate": 1}
    if not argv or argv[0] not in cmds or len(argv) != 1 + cmds[argv[0]]:
        print(__doc__); return 2
    try:
        run(argv[0], argv[1] if len(argv) > 1 else None)
    except HTTPException as e:
        print(f"Not done: {e.detail}"); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
