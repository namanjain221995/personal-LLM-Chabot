"""The /auth surface: login, logout, me, password, sessions, invitations.

Every failure an unauthenticated caller can see is GENERIC on purpose:
"Incorrect email or password." covers wrong password, unknown email, disabled
account, and not-yet-bootstrapped accounts alike — the login form is not an
account-existence oracle. Timing is equalized in passwords.verify.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import db
from ..config import settings
from . import passwords, sessions, store
from .principal import (
    Principal,
    _build as build_principal,
    audit,
    current_principal,
    require_principal,
)

router = APIRouter(prefix="/auth", tags=["auth"])

GENERIC_LOGIN_ERROR = "Incorrect email or password."


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=passwords.MAX_LENGTH)
    remember: bool = True


def _me_payload(principal: Principal) -> Dict[str, Any]:
    return {
        # Legacy key: pre-auth clients read `username` from /auth/me. Kept so
        # a half-deployed frontend fails soft during rollout.
        "username": principal.username,
        "user": {
            "id": principal.user_id,
            "name": principal.display_name,
            "email": principal.email,
        },
        "workspace": {
            "id": principal.workspace_id,
            "name": principal.workspace_name,
            "role": principal.role.value,
        },
        "capabilities": sorted(c.value for c in principal.caps),
    }


def _login_sync(body: LoginRequest, ip: str, user_agent: str) -> Dict[str, Any]:
    """The blocking half of login. Returns {user, cookie} or raises."""
    email_key = f"email:{body.email.strip().lower()}"
    ip_key = f"ip:{ip or 'unknown'}"
    for key in (email_key, ip_key):
        if store.throttle_check(key) is not None:
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Try again in a few minutes.",
            )

    user = store.get_user_by_email(body.email)
    matches, needs_rehash = passwords.verify(
        user["password_hash"] if user else passwords.UNUSABLE, body.password
    )
    workspace = store.default_workspace()
    member = store.membership(int(user["id"])) if user else None
    ok = bool(user) and matches and user["status"] == "active" and member is not None

    if not ok:
        for key in (email_key, ip_key):
            store.throttle_failure(
                key,
                window_seconds=settings.auth_login_window_seconds,
                max_fails=settings.auth_login_max_fails,
                lock_seconds=settings.auth_login_lock_seconds,
            )
        store.record_audit(
            workspace_id=workspace["id"] if workspace else None,
            actor_user_id=None,
            action="login_failure",
            target_user_id=int(user["id"]) if user else None,
            meta={"email": body.email.strip().lower()[:200]},
            ip=ip,
            user_agent=user_agent,
        )
        raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)

    store.throttle_clear(email_key)
    if needs_rehash:
        store.set_credentials(
            int(user["id"]), password_hash=passwords.hash_password(body.password)
        )
    session_row, cookie_value = sessions.create(
        int(user["id"]), remember=body.remember, user_agent=user_agent, ip=ip
    )
    store.record_audit(
        workspace_id=member["workspace_id"],
        actor_user_id=int(user["id"]),
        action="login_success",
        meta={"session_id": session_row["id"], "remember": body.remember},
        ip=ip,
        user_agent=user_agent,
    )
    principal = build_principal(session_row)
    if principal is None:  # pragma: no cover — user/membership fetched above
        raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)
    return {"cookie": cookie_value, "principal": principal}


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response) -> dict:
    ip, user_agent = sessions.client_meta(request)
    result = await db.run_in_thread(_login_sync, body, ip, user_agent)
    sessions.set_cookie(
        response, result["cookie"], remember=body.remember, request=request
    )
    return _me_payload(result["principal"])


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    principal = await current_principal(request)
    if principal is not None:
        await db.run_in_thread(store.revoke_session, principal.session_id)
        await db.run_in_thread(
            audit, principal, request, "logout"
        )
    sessions.clear_cookie(response, request=request)
    return {"ok": True}


@router.get("/me")
async def me(request: Request) -> dict:
    """Who am I. 401 when signed out — the frontend's session probe."""
    principal = await current_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    return _me_payload(principal)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(max_length=passwords.MAX_LENGTH)
    new_password: str = Field(max_length=passwords.MAX_LENGTH)


@router.post("/password")
async def change_password(body: PasswordChangeRequest, request: Request) -> dict:
    principal = await require_principal(request)

    def work() -> None:
        user = store.get_user(principal.user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="Sign in required.")
        matches, _ = passwords.verify(user["password_hash"], body.current_password)
        if not matches:
            raise HTTPException(
                status_code=403, detail="Your current password is incorrect."
            )
        problem = passwords.validate_new_password(body.new_password)
        if problem:
            raise HTTPException(status_code=422, detail=problem)
        store.set_credentials(
            principal.user_id,
            password_hash=passwords.hash_password(body.new_password),
        )
        # Everyone else holding this account is signed out; this session stays.
        revoked = store.revoke_user_sessions(
            principal.user_id, keep=principal.session_id
        )
        audit(
            principal,
            request,
            "password_changed",
            meta={"other_sessions_revoked": revoked},
        )

    await db.run_in_thread(work)
    return {"ok": True}


@router.get("/sessions")
async def my_sessions(request: Request) -> dict:
    principal = await require_principal(request)
    rows = await db.run_in_thread(store.list_sessions, principal.user_id)
    return {
        "sessions": [
            {
                "id": r["id"],
                "current": r["id"] == principal.session_id,
                "created_at": r["created_at"].isoformat(),
                "last_seen_at": r["last_seen_at"].isoformat(),
                "expires_at": r["expires_at"].isoformat(),
                "user_agent": r["user_agent"] or "",
                # Coarse on purpose: the sessions UI shows "where roughly",
                # not a tracking log.
                "ip": r["ip"] or "",
            }
            for r in rows
        ]
    }


class SessionRevokeRequest(BaseModel):
    session_id: Optional[str] = None
    others: bool = False


@router.post("/sessions/revoke")
async def revoke_sessions(body: SessionRevokeRequest, request: Request) -> dict:
    principal = await require_principal(request)

    def work() -> int:
        if body.others:
            n = store.revoke_user_sessions(principal.user_id, keep=principal.session_id)
        elif body.session_id:
            # Own sessions only: the row must belong to this user.
            own = {r["id"] for r in store.list_sessions(principal.user_id)}
            if body.session_id not in own:
                raise HTTPException(status_code=404, detail="No such session.")
            n = 1 if store.revoke_session(body.session_id) else 0
        else:
            raise HTTPException(status_code=422, detail="Nothing to revoke.")
        if n:
            audit(principal, request, "session_revoked", meta={"count": n})
        return n

    revoked = await db.run_in_thread(work)
    return {"revoked": revoked}


# ---------------------------------------------------------------------------
# Preferences (personalization storage — server-side, per user)
# ---------------------------------------------------------------------------


class PreferencesRequest(BaseModel):
    prefs: Dict[str, Any] = Field(default_factory=dict)


@router.get("/preferences")
async def get_preferences(request: Request) -> dict:
    principal = await require_principal(request)
    prefs = await db.run_in_thread(store.get_preferences, principal.user_id)
    return {"prefs": prefs}


@router.put("/preferences")
async def put_preferences(body: PreferencesRequest, request: Request) -> dict:
    principal = await require_principal(request)
    if len(str(body.prefs)) > 20_000:
        raise HTTPException(status_code=422, detail="Preferences too large.")
    await db.run_in_thread(store.set_preferences, principal.user_id, body.prefs)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Invitations — the only account-creation path (no public signup)
# ---------------------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _live_invitation(token: str) -> Optional[Dict[str, Any]]:
    from datetime import datetime, timezone

    if not token or len(token) > 200:
        return None
    inv = store.get_invitation_by_token_hash(_token_hash(token))
    if inv is None or inv["accepted_at"] or inv["revoked_at"]:
        return None
    if inv["expires_at"] <= datetime.now(timezone.utc):
        return None
    return inv


@router.get("/invitations/{token}")
async def invitation_info(token: str) -> dict:
    """What the accept page shows. 404 for anything not currently valid —
    expired, used, revoked and never-existed are indistinguishable."""
    inv = await db.run_in_thread(_live_invitation, token)
    if inv is None:
        raise HTTPException(status_code=404, detail="This invitation is no longer valid.")
    workspace = await db.run_in_thread(store.default_workspace)
    return {
        "email": inv["email"],
        "name": inv["name"],
        "role": inv["role"],
        "workspace_name": workspace["name"] if workspace else "",
        "expires_at": inv["expires_at"].isoformat(),
    }


class AcceptInvitationRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    name: str = Field(default="", max_length=200)
    password: str = Field(max_length=passwords.MAX_LENGTH)


@router.post("/invitations/accept")
async def accept_invitation(
    body: AcceptInvitationRequest, request: Request, response: Response
) -> dict:
    problem = passwords.validate_new_password(body.password)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    ip, user_agent = sessions.client_meta(request)

    def work() -> Dict[str, Any]:
        inv = _live_invitation(body.token)
        if inv is None:
            raise HTTPException(
                status_code=404, detail="This invitation is no longer valid."
            )
        user = store.accept_invitation(
            inv["id"],
            display_name=body.name,
            password_hash=passwords.hash_password(body.password),
        )
        if user is None:
            raise HTTPException(
                status_code=404, detail="This invitation is no longer valid."
            )
        session_row, cookie_value = sessions.create(
            int(user["id"]), remember=True, user_agent=user_agent, ip=ip
        )
        store.record_audit(
            workspace_id=inv["workspace_id"],
            actor_user_id=int(user["id"]),
            action="invitation_accepted",
            target_user_id=int(user["id"]),
            resource_type="invitation",
            resource_id=inv["id"],
            meta={"role": inv["role"]},
            ip=ip,
            user_agent=user_agent,
        )
        principal = build_principal(session_row)
        if principal is None:  # pragma: no cover — membership created above
            raise HTTPException(
                status_code=404, detail="This invitation is no longer valid."
            )
        return {"cookie": cookie_value, "principal": principal}

    result = await db.run_in_thread(work)
    sessions.set_cookie(response, result["cookie"], remember=True, request=request)
    return _me_payload(result["principal"])
