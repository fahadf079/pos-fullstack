"""
settings.py — owner-editable settings, stored in the database (table `settings`) and cached for 2 seconds.

Only the keys in SPEC exist; each has a validator, so a bad value can never be saved. The network policy is
kept here too but is changed only through netpolicy.py (it has extra safety checks).
"""
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import context
import database
from database import audit, db, transaction

router = APIRouter()
LOCK_SECONDS = 60          # inactivity lock; fixed on purpose (sessions.py enforces it on the server too)


def _bool(v: Any) -> bool:
    if not isinstance(v, bool):
        raise HTTPException(422, "This setting is on or off.")
    return v


def _money(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v > 1_000_000:
        raise HTTPException(422, "Enter an amount of 0 or more.")
    return round(float(v), 2)


def _percent(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v > 50:
        raise HTTPException(422, "Enter a tax rate between 0 and 50 (percent).")
    return round(float(v), 2)


def _whole(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v > 100_000 or float(v) != int(v):
        raise HTTPException(422, "Enter a whole number of 0 or more.")
    return int(v)


def _text(limit: int):
    def check(v: Any) -> str:
        if not isinstance(v, str) or len(v.strip()) > limit or any(ord(ch) < 32 for ch in v):
            raise HTTPException(422, f"Enter text of at most {limit} letters.")
        return v.strip()
    return check


def _codes(v: Any) -> dict:
    """Discount codes: {"SAVE10": 10, ...}. At most 20; code = 3-20 letters/digits; percent 1-100."""
    if not isinstance(v, dict) or len(v) > 20:
        raise HTTPException(422, "Discount codes: at most 20, written like SAVE10=10.")
    out = {}
    for k, p in v.items():
        code = str(k).strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{3,20}", code):
            raise HTTPException(422, f"“{k}” is not a valid code (3 to 20 letters or digits).")
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not (0 < p <= 100):
            raise HTTPException(422, f"The discount for {code} must be above 0 and at most 100 (percent).")
        out[code] = round(float(p), 2)
    return out


# key -> (default, validator, what it is)
SPEC: dict[str, tuple[Any, Any, str]] = {
    "guest_enabled": (False, _bool, "Show a read-only Guest (demo) button on the login screen"),
    "require_shift": (False, _bool, "Employees must open a cash-drawer shift before taking cash"),
    "employee_manual_drawer": (False, _bool, "Employees may open the cash drawer by hand (with their PIN and a reason)"),
    "variance_threshold": (100.0, _money, "Cash difference (Rs) above which the owner is alerted at shift close"),
    "tax_percent": (8.0, _percent, "Sales tax (percent) added to every sale"),
    "low_stock": (5, _whole, "An item counts as LOW when its stock is at or below this number"),
    "discount_codes": ({"SAVE10": 10.0, "SAVE20": 20.0}, _codes, "Discount codes (percent off). Using one needs the employee's own PIN"),
    "shop_name": ("My Mart", _text(60), "Shop name printed on receipts"),
    "receipt_footer": ("Thank you for shopping with us!", _text(200), "Line printed at the bottom of every receipt"),
    "remove_needs_pin": (False, _bool, "Removing items from the cart (or clearing it) needs the employee's own PIN"),
    "force_owner_2fa": (False, _bool, "Every owner must switch on 2FA before they can use the POS"),
    "unlock_needs_code": (False, _bool, "Owners must also type their 2FA code to unlock a locked screen"),
    "scale_barcodes": (False, _bool, "Accept weighed-item barcodes from a price-computing scale (13 digits: 2x prefix, 5-digit item code, 5-digit grams, check digit)"),
}
NETWORK_DEFAULT = {"approved_ips": [], "enforce": True}
_cache: dict = {"ts": 0.0, "data": None}


def _load() -> dict:
    with transaction(snapshot=True):
        return {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}


def all_values() -> dict:
    if _cache["data"] is None or time.time() - _cache["ts"] > 2:
        stored = _load()
        data = {k: stored.get(k, d) for k, (d, _, _) in SPEC.items()}
        data["network"] = {**NETWORK_DEFAULT, **(stored.get("network") or {})}
        _cache["data"], _cache["ts"] = data, time.time()
    return _cache["data"]


def get(key: str) -> Any:
    return all_values()[key]


def put(key: str, value: Any, by: str) -> None:
    """Saves one setting (caller validates). Runs in the caller's transaction."""
    import json
    db.execute("INSERT INTO settings(key, value, updated_by) VALUES (?,?::jsonb,?) "
               "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_ts=now(), updated_by=EXCLUDED.updated_by",
               (key, json.dumps(value), by))
    _cache["data"] = None


class SettingIn(BaseModel):
    key: str
    value: Any


@router.get("/settings")
def read_settings():
    v = all_values()
    return {"settings": [{"key": k, "label": SPEC[k][2], "value": v[k]} for k in SPEC], "lock_seconds": LOCK_SECONDS}


@router.get("/shop")
def shop_info():
    """What every till needs to print a receipt and label totals. No secrets in here."""
    v = all_values()
    return {"shop_name": v["shop_name"], "receipt_footer": v["receipt_footer"], "tax_percent": v["tax_percent"], "scale_barcodes": v["scale_barcodes"]}


@router.post("/settings")
def change_setting(b: SettingIn):
    if b.key not in SPEC:
        raise HTTPException(404, "No such setting.")
    me = context.current_user()
    new = SPEC[b.key][1](b.value)
    with transaction():
        old = all_values()[b.key]
        if old != new:
            put(b.key, new, me["username"])
            audit("settings.changed", "settings", b.key, f"{SPEC[b.key][2]}: {old!r} → {new!r}", {"key": b.key, "old": old, "new": new})
    return {"ok": True}
