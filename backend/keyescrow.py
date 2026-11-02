"""
keyescrow.py — a second, safe home for the secret key (run on the POS computer, from the backend folder).

The secret key (backend/pos_secret.key, or POS_SECRET_KEY) opens every PIN check, 2FA seed and the card-machine
credentials. If that file is lost, all of those must be set up again. This tool keeps a COPY of the key in the database
too, but never in the clear:

    python keyescrow.py create     makes a recovery code, stores the key in the database locked with that code,
                                   and shows the code ONCE. Write it down and keep it OFF this computer.
    python keyescrow.py restore    asks for the recovery code and puts the key back into backend/pos_secret.key.
    python keyescrow.py status     says whether a database copy exists.

A stolen database or backup is useless without the recovery code, and the recovery code is useless without the
database. Running `create` again replaces the old code. Every use is written to the History (person "cli");
no key or code is ever written there.
"""
import base64
import getpass
import hashlib
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import database  # noqa: E402
import hashing  # noqa: E402
from database import db, transaction  # noqa: E402

KEY_FILE = Path(__file__).resolve().parent / "pos_secret.key"
_AAD = b"pos-key-escrow-v1"


def _kek(code: str, salt: bytes) -> bytes:
    clean = "".join(ch for ch in code.upper() if ch.isalnum())
    return hashlib.scrypt(clean.encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def new_code() -> str:
    raw = base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=")        # 128 bits
    return "-".join(raw[i:i + 5] for i in range(0, len(raw), 5))


def wrap(key: bytes, code: str) -> tuple[str, str]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    ct = AESGCM(_kek(code, salt)).encrypt(nonce, key, _AAD)
    return salt.hex(), base64.urlsafe_b64encode(nonce + ct).decode()


def unwrap(salt_hex: str, wrapped: str, code: str) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.urlsafe_b64decode(wrapped.encode())
    try:
        return AESGCM(_kek(code, bytes.fromhex(salt_hex))).decrypt(raw[:12], raw[12:], _AAD)
    except InvalidTag:
        raise SystemExit("That recovery code is wrong.") from None


def run(cmd: str) -> None:
    database.init()
    with transaction():
        row = db.execute("SELECT salt, wrapped, created_ts FROM key_escrow WHERE id=1").fetchone()
        if cmd == "status":
            print(f"A locked copy of the key is in the database (made {row['created_ts']:%Y-%m-%d %H:%M})." if row
                  else "No copy in the database yet. Run:  python keyescrow.py create")
        elif cmd == "create":
            if row and input("A copy already exists. Replace it and issue a NEW recovery code? (yes/no): ").strip().lower() != "yes":
                raise SystemExit("Nothing changed.")
            code = new_code()
            salt, wrapped = wrap(hashing.pepper(), code)
            db.execute("INSERT INTO key_escrow(id, salt, wrapped) VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET salt=EXCLUDED.salt, wrapped=EXCLUDED.wrapped, created_ts=now()", (salt, wrapped))
            database.audit("key.escrow_created", "key", "escrow", "A locked copy of the secret key was stored in the database", actor="cli")
            print("\nRECOVERY CODE (shown only now — write it down and keep it away from this computer):\n")
            print("    " + code + "\n")
        elif cmd == "restore":
            if not row:
                raise SystemExit("There is no copy in the database to restore from.")
            if KEY_FILE.exists() and input(f"{KEY_FILE.name} already exists and will be overwritten. Continue? (yes/no): ").strip().lower() != "yes":
                raise SystemExit("Nothing changed.")
            key = unwrap(row["salt"], row["wrapped"], getpass.getpass("Recovery code: "))
            KEY_FILE.write_bytes(key)
            try:
                KEY_FILE.chmod(0o600)
            except OSError:
                pass
            database.audit("key.escrow_restored", "key", "escrow", "The secret key file was restored from the database copy", actor="cli")
            print("Done. Restart the POS. Logins, 2FA and the card machine credentials work again.")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("create", "restore", "status"):
        print(__doc__)
        return 2
    run(sys.argv[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
