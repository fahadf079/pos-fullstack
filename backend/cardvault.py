"""
cardvault.py — card-machine credentials: stored sealed, written once, NEVER sent back.

  * The screen only ever sees a REFERENCE ("card-terminal") and whether it is configured, plus who/when. The secret
    itself is accepted by one write-only endpoint, sealed (hashing.seal) and kept in the `vault` table.
  * The only way to read it is `use("card-terminal")`, a Python function for the server-side terminal driver. No API
    route calls it. The History records "credentials updated" without any value, and request bodies are never logged.
  * Real terminal integration is NOT built here (it depends on the machine's vendor); this is the safe place to plug it in.
"""
import json
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, SecretStr

import context
import hashing
from database import audit, db, transaction

router = APIRouter()
CARD_REF = "card-terminal"


def use(ref: str = CARD_REF) -> dict:
    """SERVER-SIDE ONLY: the decrypted credentials for the terminal driver. Never return this from an endpoint."""
    with transaction(snapshot=True):
        r = db.execute("SELECT sealed FROM vault WHERE ref=?", (ref,)).fetchone()
    if not r:
        raise RuntimeError("Card machine credentials are not set.")
    return json.loads(hashing.unseal(r["sealed"]))


class CredsIn(BaseModel):
    merchant_id: SecretStr = Field(max_length=200)
    api_key: SecretStr = Field(max_length=400)
    terminal_id: Optional[SecretStr] = Field(None, max_length=200)


@router.get("/integrations/card")
def card_status():
    with transaction(snapshot=True):
        r = db.execute("SELECT updated_ts, updated_by FROM vault WHERE ref=?", (CARD_REF,)).fetchone()
    return {"ref": CARD_REF, "configured": bool(r), "updated_ts": r["updated_ts"] if r else None, "updated_by": r["updated_by"] if r else None}


@router.post("/integrations/card/credentials")
def card_set(b: CredsIn):
    me = context.current_user()
    if not b.merchant_id.get_secret_value().strip() or not b.api_key.get_secret_value().strip():
        raise HTTPException(422, "Both the merchant ID and the key are needed.")
    payload = {"merchant_id": b.merchant_id.get_secret_value(), "api_key": b.api_key.get_secret_value(),
               "terminal_id": b.terminal_id.get_secret_value() if b.terminal_id else ""}
    with transaction():
        db.execute("INSERT INTO vault(ref, sealed, updated_by) VALUES (?,?,?) ON CONFLICT(ref) DO UPDATE SET sealed=EXCLUDED.sealed, updated_ts=now(), updated_by=EXCLUDED.updated_by",
                   (CARD_REF, hashing.seal(json.dumps(payload)), me["username"]))
        audit("integration.card_credentials_set", "integration", CARD_REF, "Card machine credentials were stored (values are never shown or logged)")
    return {"ok": True, "ref": CARD_REF}


@router.post("/integrations/card/clear")
def card_clear():
    with transaction():
        db.execute("DELETE FROM vault WHERE ref=?", (CARD_REF,))
        audit("integration.card_credentials_cleared", "integration", CARD_REF, "Card machine credentials were removed")
    return {"ok": True}
