"""Share governance (/admin/api/shares/*) — the super admin's view.

AUTHORIZATION. Every route depends on `Cap.SHARES_MANAGE`, which rbac.py
grants to SUPER_ADMIN alone. `require_capability` answers 404, so to an admin
or a member this surface does not exist.

WHAT A SUPER ADMIN GETS, AND WHAT THEY DO NOT. They get the metadata needed to
govern: which conversations have live public links, who published them, when,
how often each has been opened, and the ability to revoke any of them
instantly. They do NOT get the conversation. There is no route here that
returns a snapshot payload or a message, because "govern what leaves the
workspace" and "read a colleague's chat" are different powers, and the second
one already exists elsewhere behind its own capability and its own audit
trail.

NEITHER DO THEY GET A WORKING LINK. Only `public_id` is ever returned — the
addressable half. The secret was hashed at creation and is not recoverable by
anyone, including the person reading this page. A governance console that
handed out working links to every private-ish conversation in the workspace
would be a worse leak than the feature it governs.

THE POLICY IS A CEILING, NOT A DEFAULT. Turning public links off here does not
merely hide the option: `sharing.evaluate` reads the same stored policy on
every create AND every republish, so an author who kept a tab open cannot
publish past it. Existing links are a separate decision — this route does not
revoke them silently; the console asks, and the caller says so explicitly.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .. import db, sharing
from ..config import settings
from .principal import Principal, audit, require_capability
from .rbac import Cap

router = APIRouter(prefix="/admin/api/shares", tags=["share-governance"])

Gate = Depends(require_capability(Cap.SHARES_MANAGE))


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _row(share: Dict[str, Any]) -> Dict[str, Any]:
    """One row of the governance table.

    `public_id` only — see the module docstring. The author is named because
    revoking someone's link without being able to tell them whose it was is
    not governance, it is vandalism.
    """
    return {
        "id": int(share["id"]),
        "conversation_id": share["conversation_id"],
        "title": share.get("title") or "Untitled conversation",
        "visibility": share["visibility"],
        "status": share["status"],
        "public_id": share.get("public_id"),
        "created_at": _iso(share.get("created_at")),
        "expires_at": _iso(share.get("expires_at")),
        "revoked_at": _iso(share.get("revoked_at")),
        "last_viewed_at": _iso(share.get("last_viewed_at")),
        "view_count": int(share.get("view_count") or 0),
        "message_count": int(share.get("message_count") or 0),
        "author": {
            "id": share.get("created_by"),
            "name": share.get("display_name") or share.get("username"),
            "email": share.get("email"),
        },
    }


@router.get("")
async def list_shares(
    status: str = Query("all", pattern="^(all|active|revoked)$"),
    limit: int = Query(200, ge=1, le=1000),
    principal: Principal = Gate,
) -> dict:
    rows = await db.run_in_thread(
        db.list_workspace_shares, principal.workspace_id, limit
    )
    shares = [_row(dict(r)) for r in rows]
    if status != "all":
        shares = [s for s in shares if s["status"] == status]
    active = [s for s in shares if s["status"] == "active"]
    return {
        "shares": shares,
        "summary": {
            "active": len(active),
            "public": sum(1 for s in active if s["visibility"] == "public"),
            "workspace": sum(1 for s in active if s["visibility"] == "workspace"),
            "views": sum(s["view_count"] for s in shares),
            "authors": len({s["author"]["id"] for s in active}),
        },
        "policy": await _policy(principal.workspace_id),
    }


@router.delete("/{share_id}")
async def revoke_any(
    request: Request,
    share_id: int,
    principal: Principal = Gate,
) -> dict:
    """Revoke one link, whoever created it.

    Scoped to the caller's workspace in the HANDLER, not only by the listing
    query: a share id from another workspace must not be revocable by guessing
    the number.
    """
    share = await db.run_in_thread(db.share_by_id, share_id)
    if not share or share.get("workspace_id") != principal.workspace_id:
        raise HTTPException(status_code=404, detail="Share not found.")
    done = await db.run_in_thread(
        db.revoke_share, share_id, revoked_by=principal.user_id
    )
    await db.run_in_thread(
        audit, principal, request, "conversation_share_revoked_by_admin",
        resource_type="conversation", resource_id=share["conversation_id"],
        meta={
            "share_id": share_id,
            "public_id": share.get("public_id"),
            "author_id": share.get("created_by"),
        },
    )
    return {"revoked": done}


class PolicyIn(BaseModel):
    public_enabled: Optional[bool] = None
    workspace_enabled: Optional[bool] = None
    allow_never: Optional[bool] = None
    allow_owner_name: Optional[bool] = None
    max_days: Optional[int] = Field(None, ge=1, le=3650)
    #: Revoke every live PUBLIC link as part of turning public sharing off.
    #: Explicit, never implied: an administrator tightening a policy for the
    #: future has not necessarily decided to break the links people have
    #: already sent to customers this morning.
    revoke_existing_public: bool = False


async def _policy(workspace_id: str) -> Dict[str, Any]:
    stored = await db.run_in_thread(db.workspace_sharing_policy, workspace_id)
    return {
        "public_enabled": stored.get("public_enabled", settings.public_sharing_enabled),
        "workspace_enabled": stored.get("workspace_enabled", True),
        "allow_never": stored.get("allow_never", settings.public_share_allow_never),
        "allow_owner_name": stored.get("allow_owner_name", True),
        "max_days": stored.get("max_days", settings.public_share_max_days),
    }


@router.get("/policy")
async def get_policy(principal: Principal = Gate) -> dict:
    return {"policy": await _policy(principal.workspace_id), "limits": {
        "max_days_ceiling": settings.public_share_max_days,
        "expiry_choices": list(sharing.EXPIRY_CHOICES),
    }}


@router.patch("/policy")
async def set_policy(
    request: Request,
    body: PolicyIn,
    principal: Principal = Gate,
) -> dict:
    current = await _policy(principal.workspace_id)
    incoming = body.model_dump(exclude_none=True)
    incoming.pop("revoke_existing_public", None)
    updated = {**current, **incoming}

    revoked = 0
    if body.revoke_existing_public and updated["public_enabled"] is False:
        rows = await db.run_in_thread(
            db.list_workspace_shares, principal.workspace_id, 1000
        )
        for r in rows:
            r = dict(r)
            if r["status"] == "active" and r["visibility"] == "public":
                if await db.run_in_thread(
                    db.revoke_share, int(r["id"]), revoked_by=principal.user_id
                ):
                    revoked += 1

    await db.run_in_thread(
        db.set_workspace_sharing_policy, principal.workspace_id, updated
    )
    await db.run_in_thread(
        audit, principal, request, "workspace_sharing_policy_changed",
        resource_type="workspace", resource_id=principal.workspace_id,
        meta={"changed": incoming, "revoked_existing_public": revoked},
    )
    return {"policy": updated, "revoked": revoked}
