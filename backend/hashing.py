"""
hashing.py — secrets handling in one place: password / PIN hashing, input rules, and sealing small secrets.

  * Passwords (owner / developer only) and PINs (employees) are stored as salted scrypt hashes, never readable.
  * A PIN is first run through HMAC-SHA256 keyed by a secret "pepper" that lives OUTSIDE the database
    (backend/pos_secret.key or the POS_SECRET_KEY variable). A stolen database or backup alone therefore can't be
    used to guess PINs offline. Keep a copy of that file: without it every PIN must be reset and 2FA re-enrolled.
  * seal()/unseal() encrypt small values that must be read back later (the 2FA seed, card-machine credentials).
    AES-256-GCM (the `cryptography` library). Old values written by the earlier home-made cipher ("seal1$") can
    still be read; everything new is written as "seal2$".
  * Rule of thumb: what we only ever COMPARE (passwords, PINs, recovery codes) is hashed. What we must read back
    (the card machine's API key) is encrypted. Customer card data (number, PIN, CVV) is never stored at all.
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


# ───────────── sealing (AES-256-GCM) ─────────────
def _stream(key: bytes, nonce: bytes, n: int) -> bytes:       # old cipher, kept only to read "seal1$" values
    out, i = b"", 0
    while len(out) < n:
        out += hmac.new(key, nonce + i.to_bytes(4, "big"), "sha256").digest()
        i += 1
    return out[:n]


def _keys() -> tuple[bytes, bytes]:                            # old cipher keys
    base = hashlib.sha256(b"pos-seal|" + pepper()).digest()
    return hmac.new(base, b"enc", "sha256").digest(), hmac.new(base, b"mac", "sha256").digest()


def _aes_key() -> bytes:
    """A 256-bit key derived from the secret with HKDF, separate from the key used for PINs."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"pos-seal-aes-gcm-v2").derive(pepper())


def seal(plain: str, context: str = "") -> str:
    """Encrypts a small value. `context` (e.g. the vault name) is bound to the result: a sealed value copied under a
    different name will not open."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(_aes_key()).encrypt(nonce, plain.encode(), context.encode())
    return "seal2$" + base64.urlsafe_b64encode(nonce + ct).decode()


def unseal(token: str, context: str = "") -> str:
    if token.startswith("seal2$"):
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = base64.urlsafe_b64decode(token.split("$", 1)[1].encode())
        try:
            return AESGCM(_aes_key()).decrypt(raw[:12], raw[12:], context.encode()).decode()
        except InvalidTag:
            raise ValueError("sealed value is damaged or the secret key changed") from None
    ek, mk = _keys()                                           # "seal1$": value from the earlier version
    raw = base64.urlsafe_b64decode(token.split("$", 1)[1].encode())
    nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
    if not hmac.compare_digest(tag, hmac.new(mk, nonce + ct, "sha256").digest()):
        raise ValueError("sealed value is damaged or the secret key changed")
    return bytes(a ^ b for a, b in zip(ct, _stream(ek, nonce, len(ct)))).decode()


# ───────────── card numbers must never be typed into free-text boxes ─────────────
_CARDISH = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")


def _luhn(digits: str) -> bool:
    t, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        t += d
        alt = not alt
    return t % 10 == 0


def no_card_number(text: str) -> str:
    """For notes and reasons: refuses text that contains a real-looking card number, so nobody can type one into
    the POS by mistake (it would end up in the database and the History)."""
    for m in _CARDISH.finditer(text or ""):
        d = re.sub(r"\D", "", m.group())
        if 13 <= len(d) <= 19 and _luhn(d):
            raise HTTPException(422, "That looks like a card number. Never type card numbers into the POS.")
    return text


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
