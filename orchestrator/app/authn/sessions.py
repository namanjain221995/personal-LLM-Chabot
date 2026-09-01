"""Opaque server-side sessions and the cookie that carries them.

The browser holds `ts_session=<id>.<secret>`. The database holds
sha256(secret) — never the secret — so neither a DB dump nor a log line yields
a usable session. Resolution is one primary-key SELECT; rolling renewal writes
at most once per five minutes per session, so the request path stays cheap.

Lifetime contract (the persistent-login experience):
- "Stay signed in" (default): cookie Max-Age = AUTH_SESSION_ABSOLUTE_DAYS,
  server `expires_at` rolls forward with activity up to AUTH_SESSION_DAYS of
  idleness, hard ceiling `absolute_expires_at` at creation + absolute days.
- Unticked: browser-session cookie (dies with the browser) and a server
  lifetime of AUTH_SESSION_UNREMEMBERED_HOURS.

Deliberately NOT JWTs and NOT itsdangerous-signed cookies (the removed V2
login used those): a signed stateless cookie cannot be revoked, and "logout
revokes access" is a hard requirement.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import Request, Response

from ..config import settings
from . import store

#: Session ids are 16 random bytes (hex), secrets 32 random bytes (urlsafe).
_SID_BYTES = 16
_SECRET_BYTES = 32

#: How stale last_seen_at may get before a resolve writes a rolling renewal.
ROLL_AFTER_SECONDS = 300


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def mint() -> Tuple[str, str, str]:
    """(session_id, cookie_value, token_hash)."""
    sid = secrets.token_hex(_SID_BYTES)
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    return sid, f"{sid}.{secret}", _hash(secret)


def split_cookie(value: str) -> Optional[Tuple[str, str]]:
    """(session_id, secret) — None for anything malformed (old-format cookies
    from the removed login land here and are simply ignored)."""
    if not value or "." not in value:
        return None
    sid, _, secret = value.partition(".")
    if not sid or not secret or len(sid) != _SID_BYTES * 2:
        return None
    return sid, secret


def lifetime_for(remember: bool) -> timedelta:
    if remember:
        return timedelta(days=settings.auth_session_days)
    return timedelta(hours=settings.auth_session_unremembered_hours)


def absolute_lifetime_for(remember: bool) -> timedelta:
    if remember:
        return timedelta(days=settings.auth_session_absolute_days)
    return timedelta(hours=settings.auth_session_unremembered_hours)


def create(
    user_id: int, *, remember: bool, user_agent: str = "", ip: str = ""
) -> Tuple[Dict[str, Any], str]:
    """(session_row, cookie_value)."""
    sid, cookie_value, token_hash = mint()
    row = store.create_session(
        session_id=sid,
        token_hash=token_hash,
        user_id=user_id,
        remember=remember,
        lifetime=lifetime_for(remember),
        absolute_lifetime=absolute_lifetime_for(remember),
        user_agent=user_agent,
        ip=ip,
    )
    return row, cookie_value


def resolve(cookie_value: str) -> Optional[Dict[str, Any]]:
    """The live session row for a cookie, or None. Rolls the expiry forward
    when the session has been quiet for a few minutes (persistent login)."""
    parts = split_cookie(cookie_value)
    if parts is None:
        return None
    sid, secret = parts
    row = store.get_session(sid)
    if row is None:
        return None
    if not hmac.compare_digest(row["token_hash"], _hash(secret)):
        return None
    now = datetime.now(timezone.utc)
    if row["revoked_at"] is not None:
        return None
    if row["expires_at"] <= now or row["absolute_expires_at"] <= now:
        return None
    if (now - row["last_seen_at"]).total_seconds() > ROLL_AFTER_SECONDS:
        store.roll_session(sid, lifetime_for(bool(row["remember"])))
    return row


def _cookie_secure(request: Optional[Request]) -> bool:
    mode = settings.auth_cookie_secure
    if mode == "true":
        return True
    if mode == "false":
        return False
    # auto: secure when the request itself arrived over TLS, or a TRUSTED
    # proxy says it did. An untrusted X-Forwarded-Proto is ignored.
    if request is None:
        return False
    if request.url.scheme == "https":
        return True
    if settings.auth_trust_proxy_headers:
        return request.headers.get("x-forwarded-proto", "").lower() == "https"
    return False


def set_cookie(
    response: Response,
    cookie_value: str,
    *,
    remember: bool,
    request: Optional[Request] = None,
) -> None:
    kwargs: Dict[str, Any] = dict(
        key=settings.auth_cookie_name,
        value=cookie_value,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )
    if remember:
        kwargs["max_age"] = int(absolute_lifetime_for(True).total_seconds())
    # No max_age for unremembered sessions: a browser-session cookie.
    response.set_cookie(**kwargs)


def clear_cookie(response: Response, *, request: Optional[Request] = None) -> None:
    response.delete_cookie(
        key=settings.auth_cookie_name,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
    )


def client_meta(request: Request) -> Tuple[str, str]:
    """(ip, user_agent) for session rows and audit events.

    The direct peer address unless AUTH_TRUST_PROXY_HEADERS opts into
    X-Forwarded-For — an unauthenticated header is an attacker-controlled
    string and must not become the audit trail's idea of "where from".
    """
    ip = request.client.host if request.client else ""
    if settings.auth_trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            ip = forwarded.split(",")[0].strip()
    return ip, request.headers.get("user-agent", "")
