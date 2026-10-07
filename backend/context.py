"""context.py — who is making the current request (set by pipeline.guard, read by endpoints and by credentials.confirm)."""
from contextvars import ContextVar
from typing import Optional

from fastapi import HTTPException


class Ctx:
    """The signed-in person for this request, plus the PIN they typed for it (if any)."""
    def __init__(self, user: dict, pin: Optional[str]):
        self.user, self.pin, self.pin_done = user, pin, False


_ctx: ContextVar[Optional[Ctx]] = ContextVar("pos_ctx", default=None)


def set_ctx(c: Optional[Ctx]) -> None:
    _ctx.set(c)


def get_ctx() -> Optional[Ctx]:
    return _ctx.get()


def current_user() -> dict:
    c = _ctx.get()
    if c is None:
        raise HTTPException(401, "Please log in.")
    return c.user
