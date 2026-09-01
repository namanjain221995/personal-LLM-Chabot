"""Identity resolution — the compatibility surface over `app.authn`.

Login is BACK (2026-09-01). The single-local-account era ended: every request
now resolves the opaque `ts_session` cookie to a real user through
authn.sessions/authn.principal, and `require_user` 401s again.

This module keeps the exact call surface the rest of the app already uses —
`UserRow`, `current_user(request)`, `require_user(request)` — so the 17
routes depending on them became enforced without an edit. The /auth routes
themselves (login, logout, me, sessions, invitations) live in authn/api.py;
the router exported here is that one.

Both functions are SYNC on purpose: FastAPI runs sync dependencies in its
threadpool, and main.py already wraps its direct calls in db.run_in_thread —
resolution does one primary-key session lookup plus a membership join, cached
on request.state so a request never pays it twice.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException, Request

from .authn.api import router  # noqa: F401 — re-exported for main.py
from .authn.principal import resolve_principal_sync

#: What `require_user` hands a route: a plain dict with at least id/username,
#: the shape history/uploads/memory have consumed since the SQLite era.
UserRow = Dict[str, Any]

#: The session cookie name (configurable via AUTH_COOKIE_NAME). Kept as a
#: module constant because tests and the frontend reference it.
SESSION_COOKIE = "ts_session"


def current_user(request: Request) -> Optional[UserRow]:
    """The signed-in user, or None. Blocking (DB) — call off the event loop."""
    principal = resolve_principal_sync(request)
    return principal.as_user_row() if principal is not None else None


def require_user(request: Request) -> UserRow:
    """FastAPI dependency: the signed-in user, or 401."""
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    return user
