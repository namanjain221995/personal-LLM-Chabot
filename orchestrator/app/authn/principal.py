"""The authenticated Principal and the FastAPI dependencies that mint it.

One resolution per request: the first dependency to run stores the outcome on
`request.state`, so a route stacking `require_user` + `require_capability`
costs a single session lookup. SSE streams resolve once at stream start —
never per token.

Everything downstream (history scoping, generation ownership, memory, the
admin surface) keys off this object. The client can influence NOTHING in it:
it is built from the session row and the membership row, both server-side.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional

from fastapi import Depends, HTTPException, Request

from .. import db
from . import features as features_mod
from . import sessions, store
from .rbac import Cap, Role, capabilities

_STATE_KEY = "techsara_principal"


@dataclass(frozen=True)
class Principal:
    user_id: int
    username: str
    email: str
    display_name: str
    role: Role
    workspace_id: str
    workspace_name: str
    session_id: str
    caps: FrozenSet[Cap] = field(default_factory=frozenset)
    #: Resolved tool access (authn/features.py): every feature id → bool.
    #: Built from the workspace default and this member's override in the
    #: same query that resolved the role, so the /chat gate costs nothing.
    features: Dict[str, bool] = field(default_factory=dict)

    def can(self, cap: Cap) -> bool:
        return cap in self.caps

    def may_use(self, feature: "features_mod.Feature") -> bool:
        """May this person use this TOOL? (`can` answers about administering.)"""
        return features_mod.allowed(self.features, feature)

    def as_user_row(self) -> Dict[str, Any]:
        """The legacy UserRow shape (`user["id"]`, `user["username"]`) that
        history/uploads/memory routes have always consumed."""
        return {
            "id": self.user_id,
            "username": self.username,
            "email": self.email,
            "display_name": self.display_name,
            "workspace_name": self.workspace_name,
        }


def _build(session_row: Dict[str, Any]) -> Optional[Principal]:
    """Session row → Principal. None when the user is gone/disabled or has no
    workspace membership (a revoked member's live session dies here)."""
    user = store.get_user(int(session_row["user_id"]))
    if user is None or user["status"] != "active":
        return None
    member = store.membership(int(user["id"]))
    if member is None:
        return None
    role = Role(member["role"])
    store.touch_last_active(int(user["id"]))
    return Principal(
        user_id=int(user["id"]),
        username=user["username"],
        email=user.get("email") or "",
        display_name=user.get("display_name") or user["username"],
        role=role,
        workspace_id=member["workspace_id"],
        workspace_name=member["workspace_name"],
        session_id=session_row["id"],
        caps=capabilities(role),
        features=features_mod.resolve(
            role=role.value,
            workspace_defaults=member.get("feature_defaults"),
            member_overrides=member.get("member_features"),
        ),
    )


def resolve_principal_sync(request: Request) -> Optional[Principal]:
    """Blocking resolution (session lookup + membership). Callers on the
    async path go through `current_principal` which runs this in a thread."""
    cached = getattr(request.state, _STATE_KEY, "unset")
    if cached != "unset":
        return cached
    cookie = request.cookies.get(_cookie_name())
    principal: Optional[Principal] = None
    if cookie:
        session_row = sessions.resolve(cookie)
        if session_row is not None:
            principal = _build(session_row)
    setattr(request.state, _STATE_KEY, principal)
    return principal


def _cookie_name() -> str:
    from ..config import settings

    return settings.auth_cookie_name


async def current_principal(request: Request) -> Optional[Principal]:
    cached = getattr(request.state, _STATE_KEY, "unset")
    if cached != "unset":
        return cached
    return await db.run_in_thread(resolve_principal_sync, request)


async def require_principal(request: Request) -> Principal:
    principal = await current_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    return principal


def require_capability(cap: Cap):
    """Dependency factory: 401 when signed out, 404 when signed in without the
    capability. 404 — not 403 — for the admin surface, so its very existence
    is not confirmed to members probing /admin endpoints."""

    async def dependency(request: Request) -> Principal:
        principal = await require_principal(request)
        if not principal.can(cap):
            raise HTTPException(status_code=404, detail="Not found.")
        return principal

    return dependency


def audit(
    principal: Principal,
    request: Optional[Request],
    action: str,
    *,
    target_user_id: Optional[int] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Record an audit event for an authenticated actor (sync; callers on the
    async path wrap in db.run_in_thread). Never raises — an audit failure must
    not take the action down with it, but it is logged loudly."""
    import logging

    ip, user_agent = ("", "")
    if request is not None:
        ip, user_agent = sessions.client_meta(request)
    try:
        store.record_audit(
            workspace_id=principal.workspace_id,
            actor_user_id=principal.user_id,
            action=action,
            target_user_id=target_user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            meta=meta,
            ip=ip,
            user_agent=user_agent,
        )
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("audit event %s was not recorded", action)


RequirePrincipal = Depends(require_principal)
