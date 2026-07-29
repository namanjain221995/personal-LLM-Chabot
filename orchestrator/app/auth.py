"""Single-user local mode — no login, no signup, no password.

This machine runs one person's assistant, so an account boundary bought
nothing but a login screen. Every request now resolves to ONE local account.

What deliberately did NOT change: conversations, uploads, documents and repo
chunks are still keyed by `user_id`, and `history.py` still takes the user as
a dependency. Ripping that out would have meant touching every ownership check
in the app — the kind of edit that quietly turns "which chats are mine?" into
"all rows in the table". Keeping the scoping and collapsing the identity is a
much smaller change, and it means existing history keeps working untouched.

WHICH account: `LOCAL_USERNAME` if set, otherwise the OLDEST existing account —
so an install that already has chat history adopts it rather than starting an
empty one. A fresh install creates the account on first use.

SECURITY NOTE: there is now no authentication whatsoever. Anyone who can reach
the port can read every conversation and query the Salesforce data. That is
fine for a machine only you can reach, and NOT fine if the port is published to
a network you do not control — see the compose port bindings.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

from fastapi import APIRouter, Request

from . import db

router = APIRouter(prefix="/auth", tags=["auth"])

#: Kept so any still-set cookie from the old login is simply ignored, not read.
SESSION_COOKIE = "ts_session"

DEFAULT_LOCAL_USERNAME = "local"

_cached_user_id: Optional[int] = None


def _local_username() -> str:
    return (os.environ.get("LOCAL_USERNAME") or "").strip() or DEFAULT_LOCAL_USERNAME


def _oldest_user() -> Optional[sqlite3.Row]:
    """The first account ever created — the one whose history to adopt."""
    conn = db.connect()
    return conn.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()


def local_user() -> sqlite3.Row:
    """THE user. Resolved once, then cached for the process lifetime."""
    global _cached_user_id
    if _cached_user_id is not None:
        row = db.get_user_by_id(_cached_user_id)
        if row is not None:
            return row
        _cached_user_id = None  # deleted underneath us — re-resolve

    configured = os.environ.get("LOCAL_USERNAME")
    if configured:
        row = db.get_user_by_username(configured.strip())
        if row is not None:
            _cached_user_id = int(row["id"])
            return row

    if not configured:
        row = _oldest_user()
        if row is not None:
            _cached_user_id = int(row["id"])
            return row

    # Fresh install (or LOCAL_USERNAME names an account that does not exist
    # yet): create it. The password hash is unusable on purpose — nothing
    # verifies passwords any more, and storing a real one would imply it does.
    username = _local_username()
    try:
        user_id = db.create_user(username, "!local-no-login")
    except sqlite3.IntegrityError:
        pass  # raced with another worker
    row = db.get_user_by_username(username)
    if row is None:  # pragma: no cover — create + lookup both failing
        raise RuntimeError(f"could not resolve the local user {username!r}")
    _cached_user_id = int(row["id"])
    return row


def current_user(request: Request) -> Optional[sqlite3.Row]:
    """The local user. Signature kept so callers in main.py are unchanged."""
    del request  # no cookie is read — there is no session any more
    return local_user()


def require_user(request: Request) -> sqlite3.Row:
    """FastAPI dependency for history routes. Never 401s now."""
    return current_user(request)


@router.get("/me")
def me() -> dict:
    """Who the app is running as. The UI shows this; it is not a login check."""
    return {"username": local_user()["username"], "local": True}
