"""The workspace administration surface (/admin/api/*).

Every route is capability-gated (RBAC), not role-string-gated. Content
inspection — reading a member's conversations, uploads, reports — exists ONLY
here, is READ-ONLY, and records an audit event per access. There is no
impersonation: nothing on this surface can act AS another user, start a chat
in their name, or feed their content into a model context.

Unauthorized access answers 404, not 403, so the admin surface neither
confirms its own existence nor which objects exist (`require_capability`
handles the first; explicit 404s on foreign/missing ids handle the second).
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .. import db
from ..config import settings
from . import features as feature_access
from . import passwords, store
from .api import _token_hash
from .principal import Principal, audit, require_capability
from .rbac import Cap, Role, assignable_roles, outranks

router = APIRouter(prefix="/admin/api", tags=["admin"])


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _member_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": row.get("display_name") or row["username"],
        "email": row.get("email") or "",
        "role": row["role"],
        "status": row["status"],
        "joined_at": _iso(row.get("member_since") or row.get("created_at")),
        "last_active_at": _iso(row.get("last_active_at")),
    }


async def _target_member(principal: Principal, user_id: int) -> Dict[str, Any]:
    """The target user's row + membership, 404 when not in this workspace."""

    def work() -> Optional[Dict[str, Any]]:
        user = store.get_user(user_id)
        if user is None:
            return None
        member = store.membership(user_id)
        if member is None or member["workspace_id"] != principal.workspace_id:
            return None
        return {**user, "role": member["role"], "member_since": member["member_since"]}

    row = await db.run_in_thread(work)
    if row is None:
        raise HTTPException(status_code=404, detail="No such member.")
    return row


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def _invite_status(row: Dict[str, Any]) -> str:
    """The single source of truth for an invitation's state.

    The client derives the same four values for its chips; deriving them HERE
    too is what lets the list be filtered server-side without the two
    definitions drifting apart.
    """
    if row.get("accepted_at"):
        return "accepted"
    if row.get("revoked_at"):
        return "revoked"
    expires = row.get("expires_at")
    if expires is not None and expires <= datetime.now(timezone.utc):
        return "expired"
    return "pending"


@router.get("/overview")
async def overview(
    principal: Principal = Depends(require_capability(Cap.WORKSPACE_READ)),
) -> dict:
    stats = await db.run_in_thread(store.workspace_overview, principal.workspace_id)
    return {
        "workspace": {
            "id": principal.workspace_id,
            "name": principal.workspace_name,
        },
        "stats": stats,
    }


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


@router.get("/members")
async def members(
    q: str = Query("", max_length=200),
    role: str = Query("", pattern="^(|super_admin|admin|member)$"),
    status: str = Query("", pattern="^(|active|disabled)$"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(require_capability(Cap.MEMBERS_READ)),
) -> dict:
    rows, total = await db.run_in_thread(
        lambda: store.list_members(
            principal.workspace_id,
            query=q,
            role=role,
            status=status,
            limit=limit,
            offset=offset,
        )
    )
    counts = await db.run_in_thread(store.member_counts, principal.workspace_id)
    return {
        "members": [_member_payload(r) for r in rows],
        "total": total,
        # Named distinctly — spreading counts here once clobbered the array
        # (its "members" count key landed on top of the list).
        "active_members": counts["members"],
        "pending_invites": counts["pending_invites"],
    }


@router.get("/members/{user_id}")
async def member_detail(
    user_id: int,
    principal: Principal = Depends(require_capability(Cap.MEMBERS_READ)),
) -> dict:
    row = await _target_member(principal, user_id)
    stats = await db.run_in_thread(store.admin_user_overview, user_id)
    return {"member": _member_payload(row), "stats": stats}


class RoleChangeRequest(BaseModel):
    role: str = Field(pattern="^(super_admin|admin|member)$")


@router.post("/members/{user_id}/role")
async def change_role(
    user_id: int,
    body: RoleChangeRequest,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.ROLES_MANAGE)),
) -> dict:
    target = await _target_member(principal, user_id)
    new_role = Role(body.role)
    if new_role not in assignable_roles(principal.role):
        raise HTTPException(status_code=403, detail="You cannot assign that role.")
    if user_id == principal.user_id and new_role is not Role.SUPER_ADMIN:
        # Self-demotion falls under the last-super-admin guard below, but a
        # clear message beats a puzzling one.
        pass
    if target["role"] == Role.SUPER_ADMIN.value and new_role is not Role.SUPER_ADMIN:
        remaining = await db.run_in_thread(
            store.count_active_super_admins, principal.workspace_id, user_id
        )
        if remaining == 0:
            raise HTTPException(
                status_code=409,
                detail="The workspace must keep at least one active super admin.",
            )
    await db.run_in_thread(
        store.upsert_membership, principal.workspace_id, user_id, new_role.value
    )
    await db.run_in_thread(
        audit,
        principal,
        request,
        "role_changed",
        target_user_id=user_id,
        meta={"from": target["role"], "to": new_role.value},
    )
    return {"ok": True, "role": new_role.value}


class StatusChangeRequest(BaseModel):
    disabled: bool


@router.post("/members/{user_id}/status")
async def change_status(
    user_id: int,
    body: StatusChangeRequest,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.MEMBERS_MANAGE)),
) -> dict:
    target = await _target_member(principal, user_id)
    if user_id == principal.user_id:
        raise HTTPException(status_code=409, detail="You cannot deactivate yourself.")
    if not outranks(principal.role, target["role"]):
        raise HTTPException(status_code=403, detail="You cannot manage that member.")
    if body.disabled and target["role"] == Role.SUPER_ADMIN.value:
        remaining = await db.run_in_thread(
            store.count_active_super_admins, principal.workspace_id, user_id
        )
        if remaining == 0:
            raise HTTPException(
                status_code=409,
                detail="The workspace must keep at least one active super admin.",
            )

    def work() -> int:
        store.set_status(user_id, "disabled" if body.disabled else "active")
        # Deactivation is immediate: every live session dies with it — and
        # each carries the reason, so the person's browser can say so.
        return (
            store.revoke_user_sessions(user_id, reason=store.REVOKE_ACCOUNT_DISABLED)
            if body.disabled
            else 0
        )

    revoked = await db.run_in_thread(work)
    await db.run_in_thread(
        audit,
        principal,
        request,
        "user_disabled" if body.disabled else "user_enabled",
        target_user_id=user_id,
        meta={"sessions_revoked": revoked} if body.disabled else None,
    )
    return {"ok": True, "status": "disabled" if body.disabled else "active"}


@router.delete("/members/{user_id}")
async def remove_member(
    user_id: int,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.MEMBERS_MANAGE)),
) -> dict:
    """Remove from the workspace: membership deleted, account disabled, every
    session revoked. The user's DATA is deliberately kept — deleting a departed
    employee's work is a separate, explicit decision, not a side effect."""
    target = await _target_member(principal, user_id)
    if user_id == principal.user_id:
        raise HTTPException(status_code=409, detail="You cannot remove yourself.")
    if not outranks(principal.role, target["role"]):
        raise HTTPException(status_code=403, detail="You cannot manage that member.")
    if target["role"] == Role.SUPER_ADMIN.value:
        remaining = await db.run_in_thread(
            store.count_active_super_admins, principal.workspace_id, user_id
        )
        if remaining == 0:
            raise HTTPException(
                status_code=409,
                detail="The workspace must keep at least one active super admin.",
            )

    def work() -> int:
        store.remove_membership(principal.workspace_id, user_id)
        store.set_status(user_id, "disabled")
        return store.revoke_user_sessions(user_id, reason=store.REVOKE_ACCOUNT_REMOVED)

    revoked = await db.run_in_thread(work)
    await db.run_in_thread(
        audit,
        principal,
        request,
        "user_removed",
        target_user_id=user_id,
        meta={"sessions_revoked": revoked, "data_kept": True},
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Member sessions
# ---------------------------------------------------------------------------


@router.get("/members/{user_id}/sessions")
async def member_sessions(
    user_id: int,
    principal: Principal = Depends(require_capability(Cap.SESSIONS_MANAGE)),
) -> dict:
    await _target_member(principal, user_id)
    rows = await db.run_in_thread(
        lambda: store.list_sessions(user_id, live_only=False)
    )
    return {
        "sessions": [
            {
                "id": r["id"],
                "created_at": _iso(r["created_at"]),
                "last_seen_at": _iso(r["last_seen_at"]),
                "expires_at": _iso(r["expires_at"]),
                "revoked_at": _iso(r["revoked_at"]),
                "user_agent": r["user_agent"] or "",
                "ip": r["ip"] or "",
            }
            for r in rows[:50]
        ]
    }


@router.post("/members/{user_id}/sessions/revoke")
async def revoke_member_sessions(
    user_id: int,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.SESSIONS_MANAGE)),
) -> dict:
    target = await _target_member(principal, user_id)
    if user_id != principal.user_id and not outranks(principal.role, target["role"]):
        raise HTTPException(status_code=403, detail="You cannot manage that member.")
    revoked = await db.run_in_thread(
        store.revoke_user_sessions, user_id, reason=store.REVOKE_ADMIN
    )
    await db.run_in_thread(
        audit,
        principal,
        request,
        "session_revoked",
        target_user_id=user_id,
        meta={"count": revoked, "by_admin": True},
    )
    return {"revoked": revoked}


class CredentialResetRequest(BaseModel):
    new_password: str = Field(max_length=passwords.MAX_LENGTH)


@router.post("/members/{user_id}/reset-password")
async def reset_member_password(
    user_id: int,
    body: CredentialResetRequest,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.MEMBERS_MANAGE)),
) -> dict:
    """Set a temporary password for a locked-out member. Their sessions are
    revoked; they sign in with the new password and should change it."""
    target = await _target_member(principal, user_id)
    if user_id != principal.user_id and not outranks(principal.role, target["role"]):
        raise HTTPException(status_code=403, detail="You cannot manage that member.")
    problem = passwords.validate_new_password(body.new_password)
    if problem:
        raise HTTPException(status_code=422, detail=problem)

    def work() -> int:
        store.set_credentials(
            user_id, password_hash=passwords.hash_password(body.new_password)
        )
        return store.revoke_user_sessions(user_id, reason=store.REVOKE_PASSWORD_RESET)

    revoked = await db.run_in_thread(work)
    await db.run_in_thread(
        audit,
        principal,
        request,
        "password_reset_by_admin",
        target_user_id=user_id,
        meta={"sessions_revoked": revoked},
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Content inspection (read-only, audited)
# ---------------------------------------------------------------------------


@router.get("/members/{user_id}/conversations")
async def member_conversations(
    user_id: int,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(require_capability(Cap.WORKSPACE_CONTENT_READ)),
) -> dict:
    await _target_member(principal, user_id)
    rows, total = await db.run_in_thread(
        lambda: store.admin_user_conversations(user_id, limit=limit, offset=offset)
    )
    return {
        "conversations": [
            {
                "id": r["id"],
                "title": r["title"],
                "created_at": _iso(r["created_at"]),
                "updated_at": _iso(r["updated_at"]),
                "archived": r["archived"],
                "message_count": int(r["message_count"]),
            }
            for r in rows
        ],
        "total": total,
    }


@router.get("/members/{user_id}/conversations/{conversation_id}")
async def member_conversation(
    user_id: int,
    conversation_id: str,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.WORKSPACE_CONTENT_READ)),
) -> dict:
    """The audit conversation viewer: full messages, read-only. Audited."""
    await _target_member(principal, user_id)

    def work() -> Optional[Dict[str, Any]]:
        conversation = db.get_conversation(user_id, conversation_id)
        if conversation is None:
            return None
        messages = store.admin_conversation_messages(conversation_id)
        return {"conversation": conversation, "messages": messages}

    data = await db.run_in_thread(work)
    if data is None:
        raise HTTPException(status_code=404, detail="No such conversation.")
    await db.run_in_thread(
        audit,
        principal,
        request,
        "admin_viewed_conversation",
        target_user_id=user_id,
        resource_type="conversation",
        resource_id=conversation_id,
    )
    return {
        "conversation": {
            "id": data["conversation"]["id"],
            "title": data["conversation"]["title"],
            "created_at": data["conversation"]["created_at"],
            "updated_at": data["conversation"]["updated_at"],
        },
        "messages": [
            {
                "id": m["id"],
                "role": m["role"],
                "content": m["content"],
                "created_at": _iso(m["created_at"]),
                # Model/mode/engine metadata is useful oversight context;
                # pass it through as-is (it never contains secrets).
                "meta": m.get("meta"),
            }
            for m in data["messages"]
        ],
    }


@router.get("/members/{user_id}/uploads")
async def member_uploads(
    user_id: int,
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(require_capability(Cap.WORKSPACE_CONTENT_READ)),
) -> dict:
    await _target_member(principal, user_id)
    rows, total = await db.run_in_thread(
        lambda: store.admin_user_uploads(user_id, limit=limit, offset=offset)
    )
    return {
        "uploads": [
            {
                "id": r["id"],
                "conversation_id": r["conversation_id"],
                "conversation_title": r["conversation_title"],
                "filename": r["filename"],
                "bytes": int(r["bytes"]),
                "status": r["status"],
                "created_at": _iso(r["created_at"]),
            }
            for r in rows
        ],
        "total": total,
    }


@router.get("/members/{user_id}/reports")
async def member_reports(
    user_id: int,
    principal: Principal = Depends(require_capability(Cap.WORKSPACE_CONTENT_READ)),
) -> dict:
    await _target_member(principal, user_id)
    rows = await db.run_in_thread(store.list_report_files, user_id)
    return {
        "reports": [
            {
                "filename": r["filename"],
                "conversation_id": r["conversation_id"],
                "created_at": _iso(r["created_at"]),
            }
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


class InviteRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(default="", max_length=200)
    role: str = Field(default="member", pattern="^(admin|member)$")


@router.get("/invitations")
async def invitations(
    status: str = Query("", pattern="^(|pending|accepted|revoked|expired)$"),
    principal: Principal = Depends(require_capability(Cap.INVITES_MANAGE)),
) -> dict:
    """Invitations, newest first. `status` filters to one kind.

    The Members page asks for `pending` — its tab is called "Pending invites"
    and used to list every invitation ever sent, so a workspace whose invites
    had all been accepted showed nine rows saying "Accepted" under a heading
    promising one pending (owner report, 2026-09-03). The /admin/invitations
    page keeps the full history and filters client-side.
    """
    rows = await db.run_in_thread(store.list_invitations, principal.workspace_id)
    if status:
        rows = [r for r in rows if _invite_status(r) == status]
    return {
        "invitations": [
            {
                "id": r["id"],
                "email": r["email"],
                "name": r["name"],
                "role": r["role"],
                "invited_by": r.get("invited_by_name") or "",
                "created_at": _iso(r["created_at"]),
                "expires_at": _iso(r["expires_at"]),
                "accepted_at": _iso(r["accepted_at"]),
                "revoked_at": _iso(r["revoked_at"]),
            }
            for r in rows
        ]
    }


@router.post("/invitations")
async def create_invitation(
    body: InviteRequest,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.INVITES_MANAGE)),
) -> dict:
    """Create an invitation and return the ONE-TIME link. The token is shown
    exactly once — only its hash is stored — so the admin UI must offer
    copy-to-clipboard immediately. No email is sent (no SMTP dependency)."""
    email = body.email.strip()
    if "@" not in email or " " in email:
        raise HTTPException(status_code=422, detail="That email address looks wrong.")
    role = Role(body.role)
    if role not in assignable_roles(principal.role):
        raise HTTPException(status_code=403, detail="You cannot invite that role.")

    def work() -> Dict[str, Any]:
        existing = store.get_user_by_email(email)
        if existing is not None:
            member = store.membership(int(existing["id"]))
            if member is not None and existing["status"] == "active":
                raise HTTPException(
                    status_code=409, detail="That person is already a member."
                )
        token = secrets.token_urlsafe(32)
        inv = store.create_invitation(
            workspace_id=principal.workspace_id,
            email=email,
            name=body.name,
            role=role.value,
            token_hash=_token_hash(token),
            invited_by=principal.user_id,
            ttl=timedelta(days=settings.auth_invitation_ttl_days),
        )
        audit(
            principal,
            request,
            "user_invited",
            resource_type="invitation",
            resource_id=inv["id"],
            meta={"email": email, "role": role.value},
        )
        return {"invitation": inv, "token": token}

    result = await db.run_in_thread(work)
    inv = result["invitation"]
    return {
        "id": inv["id"],
        "email": inv["email"],
        "role": inv["role"],
        "expires_at": _iso(inv["expires_at"]),
        # The path the frontend turns into a full link (its own origin).
        "accept_path": f"/accept-invite?token={result['token']}",
    }


@router.post("/invitations/{invitation_id}/revoke")
async def revoke_invitation(
    invitation_id: str,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.INVITES_MANAGE)),
) -> dict:
    def work() -> bool:
        inv = store.get_invitation(invitation_id)
        if inv is None or inv["workspace_id"] != principal.workspace_id:
            raise HTTPException(status_code=404, detail="No such invitation.")
        return store.revoke_invitation(invitation_id)

    revoked = await db.run_in_thread(work)
    if revoked:
        await db.run_in_thread(
            audit,
            principal,
            request,
            "invitation_revoked",
            resource_type="invitation",
            resource_id=invitation_id,
        )
    return {"ok": revoked}


# ---------------------------------------------------------------------------
# Audit log (super admin)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Feature access (V17) — which TOOLS a person may use
# ---------------------------------------------------------------------------


class FeatureOverrides(BaseModel):
    """A partial map: only the keys being set. Absent means "inherit"."""

    features: Dict[str, bool] = Field(default_factory=dict)


@router.get("/access")
async def access_settings(
    principal: Principal = Depends(require_capability(Cap.MEMBERS_READ)),
) -> dict:
    """The catalog, the workspace defaults, and what those resolve to."""
    stored = await db.run_in_thread(
        store.workspace_feature_defaults, principal.workspace_id
    )
    return {
        "catalog": feature_access.catalog(),
        # What an admin explicitly set (may be empty = all built-in defaults).
        "workspace_defaults": feature_access.clean(stored),
        # What a NEW member would actually get, built-ins included.
        "resolved": feature_access.resolve(role="member", workspace_defaults=stored),
        "can_manage": principal.can(Cap.SETTINGS_MANAGE),
    }


@router.put("/access")
async def set_access_settings(
    body: FeatureOverrides,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.SETTINGS_MANAGE)),
) -> dict:
    """Set the workspace default for each tool. Members who have their own
    override keep it — that is what an override is for."""
    cleaned = feature_access.clean(body.features)
    stored = await db.run_in_thread(
        store.set_workspace_feature_defaults, principal.workspace_id, cleaned
    )
    await db.run_in_thread(
        audit, principal, request, "workspace_access_changed", meta={"features": cleaned}
    )
    return {
        "ok": True,
        "workspace_defaults": feature_access.clean(stored),
        "resolved": feature_access.resolve(role="member", workspace_defaults=stored),
    }


@router.get("/members/{user_id}/access")
async def member_access(
    user_id: int,
    principal: Principal = Depends(require_capability(Cap.MEMBERS_READ)),
) -> dict:
    """One member's tool access: the catalog, what the workspace grants by
    default, this member's overrides, and the resolved answer."""
    target = await _target_member(principal, user_id)
    defaults = await db.run_in_thread(
        store.workspace_feature_defaults, principal.workspace_id
    )
    overrides = await db.run_in_thread(
        store.member_feature_overrides, principal.workspace_id, user_id
    )
    return {
        "catalog": feature_access.catalog(),
        "workspace_resolved": feature_access.resolve(
            role="member", workspace_defaults=defaults
        ),
        "overrides": feature_access.clean(overrides),
        "resolved": feature_access.resolve(
            role=target["role"], workspace_defaults=defaults, member_overrides=overrides
        ),
        "role": target["role"],
        # A super admin's access is unconditional (features.resolve), so the
        # dialog says so instead of offering toggles that cannot bite.
        "locked": target["role"] == Role.SUPER_ADMIN.value,
    }


@router.put("/members/{user_id}/access")
async def set_member_access(
    user_id: int,
    body: FeatureOverrides,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.MEMBERS_MANAGE)),
) -> dict:
    target = await _target_member(principal, user_id)
    if not outranks(principal.role, target["role"]):
        raise HTTPException(status_code=403, detail="You cannot manage that member.")
    cleaned = feature_access.clean(body.features)
    stored = await db.run_in_thread(
        store.set_member_feature_overrides, principal.workspace_id, user_id, cleaned
    )
    defaults = await db.run_in_thread(
        store.workspace_feature_defaults, principal.workspace_id
    )
    await db.run_in_thread(
        audit,
        principal,
        request,
        "member_access_changed",
        target_user_id=user_id,
        meta={"features": cleaned},
    )
    return {
        "ok": True,
        "overrides": feature_access.clean(stored),
        "resolved": feature_access.resolve(
            role=target["role"], workspace_defaults=defaults, member_overrides=stored
        ),
    }


# ---------------------------------------------------------------------------
# Usage analytics
# ---------------------------------------------------------------------------

#: Selectable windows. Keys match the pills in the UI; the value is days.
_RANGES = {"7d": 7, "1m": 30, "3m": 90, "6m": 182, "12m": 365}


def _window(range_key: str) -> tuple[datetime, datetime, int]:
    """(since, until, days) for a range key. `until` is tomorrow midnight so
    today's messages count — a report that silently excluded the current day
    reads as "nobody used it today"."""
    days = _RANGES.get(range_key, 30)
    now = datetime.now(timezone.utc)
    until = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return until - timedelta(days=days), until, days


def _analytics_member_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        "id": int(row["id"]),
        "name": row.get("display_name") or row["username"],
        "email": row.get("email") or "",
        "role": row["role"],
        "status": row["status"],
        "last_active_at": _iso(row.get("last_active_at")),
    }
    for key in ("messages", "answers", "conversations", *store._TOOL_ROUTES):
        out[key] = int(row.get(key) or 0)
    out["tool_runs"] = sum(int(row.get(t) or 0) for t in store._TOOL_ROUTES)
    return out


@router.get("/analytics")
async def analytics(
    range: str = Query("1m", pattern="^(7d|1m|3m|6m|12m)$"),
    principal: Principal = Depends(require_capability(Cap.WORKSPACE_READ)),
) -> dict:
    """Workspace usage for a window: totals, a daily series, the route mix,
    and one row per member (including the members who used nothing)."""
    since, until, days = _window(range)
    summary = await db.run_in_thread(
        store.usage_summary, principal.workspace_id, since, until
    )
    rows = await db.run_in_thread(
        store.usage_by_member, principal.workspace_id, since, until
    )
    daily = await db.run_in_thread(
        store.usage_daily, principal.workspace_id, since, until
    )
    routes = await db.run_in_thread(
        store.usage_routes, principal.workspace_id, since, until
    )
    return {
        "workspace": {"id": principal.workspace_id, "name": principal.workspace_name},
        "range": {
            "key": range,
            "days": days,
            "since": _iso(since),
            "until": _iso(until),
        },
        "summary": summary,
        "tools": [
            {"id": tool, "label": feature_access.BY_ID[tool].label if tool in feature_access.BY_ID else tool.replace("_", " ").title(), "count": int(summary.get(tool) or 0)}
            for tool in store._TOOL_ROUTES
        ],
        "daily": [
            {
                "day": r["day"].isoformat(),
                "messages": int(r["messages"]),
                "active_users": int(r["active_users"]),
            }
            for r in daily
        ],
        "routes": [{"route": r["route"], "count": int(r["n"])} for r in routes],
        "members": [_analytics_member_row(r) for r in rows],
    }


@router.get("/analytics/export")
async def analytics_export(
    range: str = Query("1m", pattern="^(7d|1m|3m|6m|12m)$"),
    request: Request = None,  # type: ignore[assignment]
    principal: Principal = Depends(require_capability(Cap.WORKSPACE_READ)),
):
    """The per-member table as CSV — the same numbers the page shows.

    Audited: a usage export is a list of who used what, which is exactly the
    kind of read the audit log exists to record.
    """
    from fastapi.responses import StreamingResponse
    import csv
    import io

    since, until, _days = _window(range)
    rows = await db.run_in_thread(
        store.usage_by_member, principal.workspace_id, since, until
    )
    await db.run_in_thread(
        audit, principal, request, "analytics_exported", meta={"range": range}
    )
    columns = [
        "name", "email", "role", "status", "last_active_at",
        "messages", "answers", "conversations", "tool_runs", *store._TOOL_ROUTES,
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in rows:
        payload = _analytics_member_row(row)
        writer.writerow([payload.get(c, "") for c in columns])
    buffer.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="usage-{range}-{stamp}.csv"'
        },
    )


@router.get("/audit")
async def audit_log(
    action: str = Query("", max_length=64),
    actor: Optional[int] = Query(None),
    target: Optional[int] = Query(None),
    before_id: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(require_capability(Cap.AUDIT_READ)),
) -> dict:
    rows = await db.run_in_thread(
        lambda: store.list_audit_events(
            principal.workspace_id,
            action=action,
            actor=actor,
            target=target,
            limit=limit,
            before_id=before_id,
        )
    )
    return {
        "events": [
            {
                "id": int(r["id"]),
                "action": r["action"],
                "actor": {
                    "id": r["actor_user_id"],
                    "name": r.get("actor_name") or "",
                    "email": r.get("actor_email") or "",
                },
                "target": {
                    "id": r["target_user_id"],
                    "name": r.get("target_name") or "",
                    "email": r.get("target_email") or "",
                },
                "resource_type": r["resource_type"],
                "resource_id": r["resource_id"],
                "meta": r["meta"],
                "ip": r["ip"] or "",
                "created_at": _iso(r["created_at"]),
            }
            for r in rows
        ],
        "next_before_id": int(rows[-1]["id"]) if rows else None,
    }


# ---------------------------------------------------------------------------
# Admin downloads (read-only, audited)
# ---------------------------------------------------------------------------


@router.get("/members/{user_id}/uploads/{upload_id}/download")
async def download_member_upload(
    user_id: int,
    upload_id: str,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.WORKSPACE_CONTENT_READ)),
):
    """The original uploaded file, exactly as the member sent it. Audited.
    Ownership is re-derived server-side (upload → conversation → user); the
    upload_id in the URL cannot reach any other member's file."""
    import mimetypes
    import os

    from fastapi.responses import FileResponse

    from ..uploads import upload_root

    await _target_member(principal, user_id)

    def work() -> Optional[Dict[str, Any]]:
        upload = db.get_upload(upload_id)
        if upload is None:
            return None
        owner = db.conversation_owner(upload["conversation_id"])
        if owner != user_id:
            return None
        return upload

    upload = await db.run_in_thread(work)
    if upload is None:
        raise HTTPException(status_code=404, detail="No such upload.")
    path = os.path.join(
        upload_root(upload["conversation_id"], upload_id),
        "_original",
        os.path.basename(upload["filename"]),
    )
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="The file has expired.")
    await db.run_in_thread(
        audit,
        principal,
        request,
        "admin_downloaded_upload",
        target_user_id=user_id,
        resource_type="upload",
        resource_id=upload_id,
        meta={"filename": upload["filename"]},
    )
    media_type = mimetypes.guess_type(upload["filename"])[0] or "application/octet-stream"
    return FileResponse(path, filename=upload["filename"], media_type=media_type)


@router.get("/members/{user_id}/reports/{filename}")
async def download_member_report(
    user_id: int,
    filename: str,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.WORKSPACE_CONTENT_READ)),
):
    """A member's generated report file, through the audited admin surface —
    the ONLY path to a report someone else owns."""
    import mimetypes

    from fastapi.responses import FileResponse

    from ..config import settings
    from ..core.report_paths import ReportPathError, resolve_report_file

    await _target_member(principal, user_id)
    owner = await db.run_in_thread(store.report_owner, filename)
    if owner != user_id:
        raise HTTPException(status_code=404, detail="No such report.")
    try:
        path = resolve_report_file(settings.reports_dir, filename)
    except ReportPathError:
        raise HTTPException(status_code=404, detail="No such report.")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No such report.")
    await db.run_in_thread(
        audit,
        principal,
        request,
        "admin_downloaded_report",
        target_user_id=user_id,
        resource_type="report",
        resource_id=filename,
    )
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(path, filename=filename, media_type=media_type)
