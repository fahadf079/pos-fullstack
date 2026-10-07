"""
hashing.py — secrets handling in one place: password / PIN hashing, input rules, and sealing small secrets.

  * Passwords (owner / developer only) and PINs (employees) are stored as salted scrypt hashes, never readable.
  * A PIN is first run through HMAC-SHA256 keyed by a secret "pepper" that lives OUTSIDE the database
    (backend/pos_secret.key or the POS_SECRET_KEY variable). A stolen database or backup alone therefore can't be
    used to guess PINs offline. Keep a copy of that file: without it every PIN must be reset and 2FA re-enrolled.
  * seal()/unseal() encrypt small values that must be read back later (the 2FA seed, card-machine credentials).
    Standard library only: HMAC-SHA256 counter-mode keystream + an HMAC tag (encrypt-then-MAC).
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

_pepper_cache: Optional[bytes] = None
RESERVED = {"guest", "system", "cli", "unknown", "migrated", "developer", "dev"}


def pepper() -> bytes:
    global _pepper_cache
    if _pepper_cache is None:
        env = os.environ.get("POS_SECRET_KEY")
        if env:
            _pepper_cache = env.encode()
        else:
            f = Path(__file__).with_name("pos_secret.key")
            if not f.exists():
                f.write_text(secrets.token_hex(32))
                try:
                    f.chmod(0o600)
                except OSError:
                    pass
            _pepper_cache = f.read_text().strip().encode()
    return _pepper_cache


def _scrypt(secret: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(secret, salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def _pin_key(pin: str) -> bytes:
    return hmac.new(pepper(), pin.encode(), "sha256").digest()


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    return f"scrypt1${salt.hex()}${_scrypt(pw.encode(), salt).hex()}"


def hash_pin(pin: str) -> str:
    salt = os.urandom(16)
    return f"scrypt1${salt.hex()}${_scrypt(_pin_key(pin), salt).hex()}"


def _verify(stored: Optional[str], secret: bytes) -> bool:
    try:
        _, salt, dk = (stored or "").split("$")
        return hmac.compare_digest(_scrypt(secret, bytes.fromhex(salt)).hex(), dk)
    except Exception:
        return False


def verify_password(stored: Optional[str], pw: str) -> bool:
    return _verify(stored, pw.encode())


def verify_pin(stored: Optional[str], pin: str) -> bool:
    return _verify(stored, _pin_key(pin))


_DUMMY: Optional[str] = None


def burn_time() -> None:
    """Unknown names take as long as real ones, so response time doesn't reveal who exists."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("not-a-real-password")
    verify_password(_DUMMY, "x")


def fingerprint(value: str) -> str:
    """Keyed hash for one-time recovery codes (compared, never read back)."""
    return hmac.new(pepper(), value.encode(), "sha256").hexdigest()


# ───────────── sealing (encrypt-then-MAC) ─────────────
def _stream(key: bytes, nonce: bytes, n: int) -> bytes:
    out, i = b"", 0
    while len(out) < n:
        out += hmac.new(key, nonce + i.to_bytes(4, "big"), "sha256").digest()
        i += 1
    return out[:n]


def _keys() -> tuple[bytes, bytes]:
    base = hashlib.sha256(b"pos-seal|" + pepper()).digest()
    return hmac.new(base, b"enc", "sha256").digest(), hmac.new(base, b"mac", "sha256").digest()


def seal(plain: str) -> str:
    ek, mk = _keys()
    nonce, data = os.urandom(16), plain.encode()
    ct = bytes(a ^ b for a, b in zip(data, _stream(ek, nonce, len(data))))
    tag = hmac.new(mk, nonce + ct, "sha256").digest()
    return "seal1$" + base64.urlsafe_b64encode(nonce + ct + tag).decode()


def unseal(token: str) -> str:
    ek, mk = _keys()
    raw = base64.urlsafe_b64decode(token.split("$", 1)[1].encode())
    nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
    if not hmac.compare_digest(tag, hmac.new(mk, nonce + ct, "sha256").digest()):
        raise ValueError("sealed value is damaged or the secret key changed")
    return bytes(a ^ b for a, b in zip(ct, _stream(ek, nonce, len(ct)))).decode()


# ───────────── input rules ─────────────
COMMON = {"password", "12345678", "123456789", "1234567890", "qwertyui", "qwerty123", "11111111", "00000000",
          "password1", "iloveyou", "admin123", "letmein1"}


def check_password(pw: str, username: str = "") -> str:
    if len(pw or "") < 8:
        raise HTTPException(422, "Password must be at least 8 characters.")
    if len(pw) > 200:
        raise HTTPException(422, "Password is too long (200 characters at most).")
    if pw.lower() in COMMON or (username and pw.lower() == username.lower()):
        raise HTTPException(422, "That password is too easy to guess. Pick something else.")
    return pw


def check_pin(pin: str) -> str:
    if not re.fullmatch(r"\d{4,8}", pin or ""):
        raise HTTPException(422, "PIN must be 4 to 8 digits (numbers only).")
    if len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
        raise HTTPException(422, "That PIN is too easy to guess (like 1111 or 1234). Pick another.")
    return pin


def check_username(u: str) -> str:
    u = (u or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", u):
        raise HTTPException(422, "Name must be 3–32 characters: letters, numbers, dot, dash or underscore.")
    if u.lower() in RESERVED:
        raise HTTPException(422, "That name is reserved. Pick another.")
    return u
