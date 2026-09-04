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

from datetime import datetime, timezone
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


def _effective_status(share: Dict[str, Any], now: datetime) -> str:
    """What the link ACTUALLY does right now.

    `conversation_shares.status` records revocation and nothing else — its
    CHECK permits only 'active' and 'revoked', and no sweep marks anything
    expired. So a link whose deadline has passed still reads 'active' in the
    column while `/public/shares/{token}` already answers 404. Reporting that
    as live made the governance console the one surface in the system that
    disagreed with the link itself.

    `now` is passed in and computed once per request so a long listing cannot
    straddle a second boundary and report two different truths.
    """
    if share.get("status") != "active":
        return "revoked"
    expires = share.get("expires_at")
    if expires is not None and expires <= now:
        return "expired"
    return "active"


def _row(share: Dict[str, Any], now: datetime) -> Dict[str, Any]:
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
        "status": _effective_status(share, now),
        # The raw column too, so "revoked at 3pm" stays distinguishable from
        # "expired at 3pm" for anyone reading the API directly.
        "db_status": share["status"],
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
    status: str = Query("all", pattern="^(all|active|expired|revoked)$"),
    limit: int = Query(200, ge=1, le=1000),
    principal: Principal = Gate,
) -> dict:
    now = datetime.now(timezone.utc)
    rows = await db.run_in_thread(
        db.list_workspace_shares, principal.workspace_id, limit
    )
    shares = [_row(dict(r), now) for r in rows]
    if status != "all":
        shares = [s for s in shares if s["status"] == status]
    # The summary is a WORKSPACE aggregate computed in SQL, deliberately NOT
    # derived from the rows above: those are one capped, filtered page, and
    # tiles that quietly track the toolbar are worse than no tiles. Choosing
    # "Revoked" used to make "Live links" read 0 — which also suppressed the
    # banner warning about public links still in the wild.
    summary = await db.run_in_thread(
        db.workspace_share_summary, principal.workspace_id
    )
    return {
        "shares": shares,
        # So the table can say "showing 200 of 250" instead of implying that
        # what fits on the page is everything there is.
        "returned": len(shares),
        "limit": limit,
        "summary": summary,
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
    # TWO DICTS, DELIBERATELY.
    #
    # `_policy()` returns a RESOLVED view: every key filled in, from the
    # server defaults where the workspace has not chosen. Writing that back
    # was a bug with a long fuse — the first time any admin flipped any one
    # switch, all five values were frozen into workspaces.sharing_policy,
    # including four nobody had chosen. From then on the server default was
    # dead: changing PUBLIC_SHARE_ALLOW_NEVER_EXPIRE in the environment did
    # nothing at all, silently, with the console still showing the toggle off
    # and no indication that a configuration change had been ignored.
    #
    # So: persist only what was CHOSEN, and decide and reply with the
    # resolved view. `exclude_none=True` drops absent fields only, so an
    # explicit `false` is still stored and still overrides the default —
    # which is the precedence we want.
    stored = await db.run_in_thread(
        db.workspace_sharing_policy, principal.workspace_id
    )
    incoming = body.model_dump(exclude_none=True)
    incoming.pop("revoke_existing_public", None)
    to_store = {**stored, **incoming}
    resolved = {**await _policy(principal.workspace_id), **incoming}

    revoked: List[int] = []
    if body.revoke_existing_public and resolved["public_enabled"] is False:
        # One statement, not a loop over a capped listing: a count reported as
        # a finished job must not be a count that stopped early.
        revoked = await db.run_in_thread(
            db.revoke_workspace_public_shares,
            principal.workspace_id,
            revoked_by=principal.user_id,
        )

    await db.run_in_thread(
        db.set_workspace_sharing_policy, principal.workspace_id, to_store
    )
    await db.run_in_thread(
        audit, principal, request, "workspace_sharing_policy_changed",
        resource_type="workspace", resource_id=principal.workspace_id,
        meta={"changed": incoming, "revoked_existing_public": len(revoked)},
    )
    return {"policy": resolved, "revoked": len(revoked)}
