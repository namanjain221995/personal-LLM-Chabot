"""Who the model is assisting — safe, server-derived, request-scoped.

main.py sets this from the authenticated Principal at the top of each chat
worker task; prompt builders append `identity_line()` to their system prompt.
A ContextVar so it rides the asyncio task without threading a parameter
through every engine signature.

Only ever the display name, email and workspace name — never roles, session
ids, or anything security-relevant: prompts are user-visible surfaces
(reasoning traces quote them), so nothing an attacker could use belongs here.
"""
from __future__ import annotations

from contextvars import ContextVar

_current: ContextVar[str] = ContextVar("techsara_identity", default="")


def set_identity(display_name: str, email: str, workspace_name: str) -> None:
    name = (display_name or "").strip()
    if not name:
        _current.set("")
        return
    who = f"{name} ({email})" if email else name
    where = f" in {workspace_name}" if workspace_name else ""
    _current.set(
        f"You are assisting {who}{where}. Address them naturally by name "
        "when it helps; never reveal information about other workspace members."
    )


def clear_identity() -> None:
    _current.set("")


def identity_line() -> str:
    """A newline-prefixed sentence to append to a system prompt, or ''."""
    value = _current.get()
    return f"\n{value}" if value else ""
