"""
totp.py — 2FA with an authenticator app (Google Authenticator, Microsoft Authenticator, Aegis, …). RFC 6238:
6 digits, 30-second steps, SHA-1. Standard library only.

  * The seed is stored sealed (hashing.seal), never in the clear.
  * A code can be used once (the last accepted step is remembered), so a shoulder-surfed code is useless.
  * Recovery: 8 one-time codes are shown ONCE when 2FA is switched on; only keyed hashes are stored.
    If those are lost too, a developer resets 2FA on the POS computer:  python manage_users.py reset-2fa NAME
"""
import base64
import hmac
import secrets
import struct
import time
from typing import Optional

import hashing

STEP, DIGITS, WINDOW = 30, 6, 1
RECOVERY_COUNT = 8


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    h = hmac.new(key, struct.pack(">Q", step), "sha1").digest()
    o = h[-1] & 0x0F
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** DIGITS).zfill(DIGITS)


def current_step(now: Optional[float] = None) -> int:
    return int((time.time() if now is None else now) // STEP)


def verify(secret: str, code: str, last_step: int = 0, now: Optional[float] = None) -> Optional[int]:
    """Returns the step that matched (to remember it) or None. Steps at or before last_step are refused (no reuse)."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != DIGITS:
        return None
    base = current_step(now)
    for s in range(base - WINDOW, base + WINDOW + 1):
        if s > last_step and hmac.compare_digest(_code(secret, s), code):
            return s
    return None


def uri(secret: str, account: str, issuer: str = "POS") -> str:
    from urllib.parse import quote
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}&digits={DIGITS}&period={STEP}"


def new_recovery_codes() -> tuple[list[str], list[str]]:
    """(codes to show once, hashes to store)"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    codes = ["".join(secrets.choice(alphabet) for _ in range(4)) + "-" + "".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(RECOVERY_COUNT)]
    return codes, [hashing.fingerprint(c) for c in codes]


def normalise_recovery(code: str) -> str:
    return (code or "").strip().upper().replace(" ", "")
