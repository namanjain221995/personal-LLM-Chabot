"""Conversation sharing: the owner's controls, and the one anonymous route.

TWO SURFACES, AND THEY SHARE NO CODE PATH.

`/conversations/{id}/share*` is authenticated, owner-only, and does the
deciding: policy evaluation, snapshot building, expiry, revocation.

`/public/shares/{token}` is the ONLY route in this application that answers
without a session. It reads one row, compares one hash, and returns a payload
that was sanitised when it was written. It cannot reach `messages` — there is
no query here that could — so there is no filter to get wrong.

EVERY FAILURE ON THE PUBLIC ROUTE LOOKS THE SAME. Malformed, unknown, revoked
and expired all answer 404 with one sentence. Anything else tells a stranger
which links exist, which is the first half of guessing one.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import BaseModel, Field

from . import db, sharing
from .authn.principal import Principal, audit, current_principal
from .config import settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["sharing"])

#: Per-user share creations, and per-public-id views. In-process, like the
#: search limiter: this deployment is one orchestrator, and a second one would
#: need a shared store for sessions long before it needed one for this.
_create_hits: Dict[int, List[float]] = {}
_view_hits: Dict[str, List[float]] = {}


def _rate_ok(bucket: Dict[Any, List[float]], key: Any, limit: int, window_s: float) -> bool:
    now = time.monotonic()
    hits = [t for t in bucket.get(key, []) if now - t < window_s]
    if len(hits) >= limit:
        bucket[key] = hits
        return False
    hits.append(now)
    bucket[key] = hits
    return True


def reset_for_tests() -> None:
    _create_hits.clear()
    _view_hits.clear()


# ---------------------------------------------------------------------------
# Owner-facing
# ---------------------------------------------------------------------------


class ShareCreate(BaseModel):
    visibility: str = Field("public", pattern="^(public|workspace)$")
    expiry: str = Field("30d")
    show_owner_name: bool = False


class ShareUpdate(BaseModel):
    visibility: Optional[str] = Field(None, pattern="^(public|workspace)$")
    expiry: Optional[str] = None
    show_owner_name: Optional[bool] = None


async def _owned(conversation_id: str, principal: Principal) -> None:
    """404 unless this principal owns the conversation.

    404 rather than 403: a stranger must not learn that a conversation id
    exists, which is the same rule the rest of the admin surface follows.
    """
    owner = await db.run_in_thread(db.conversation_owner, conversation_id)
    if owner is None or owner != principal.user_id:
        raise HTTPException(status_code=404, detail="Conversation not found.")


async def _policy_for(principal: Principal) -> Dict[str, Any]:
    stored = await db.run_in_thread(
        db.workspace_sharing_policy, principal.workspace_id
    )
    return {
        "public_enabled": stored.get("public_enabled", settings.public_sharing_enabled),
        "workspace_enabled": stored.get("workspace_enabled", True),
        "allow_never": stored.get("allow_never", settings.public_share_allow_never),
        "max_days": stored.get("max_days", settings.public_share_max_days),
        "allow_owner_name": stored.get("allow_owner_name", True),
    }


async def _messages_for(conversation_id: str) -> List[Dict[str, Any]]:
    rows = await db.run_in_thread(db.list_messages, conversation_id)
    # `list_messages` has no notion of a streaming turn — an unfinished
    # assistant message is simply not persisted yet — but meta carries the
    # status for one that failed, and the snapshot builder re-checks anyway.
    return rows


def _share_payload(share: Optional[Dict[str, Any]], base_url: str) -> Optional[Dict[str, Any]]:
    """What the owner's modal shows. Never the token — that is returned ONCE,
    at creation, and is not recoverable afterwards by design."""
    if not share:
        return None
    return {
        "id": int(share["id"]),
        "visibility": share["visibility"],
        "status": share["status"],
        "url": f"{base_url}/share/{share['public_id']}",
        "created_at": share["created_at"].isoformat(),
        "expires_at": share["expires_at"].isoformat() if share.get("expires_at") else None,
        "show_owner_name": bool(share.get("show_owner_name")),
        "version": share.get("version_number"),
        "message_count": share.get("message_count") or 0,
        "last_message_id": share.get("last_message_id"),
        "view_count": int(share.get("view_count") or 0),
        "last_viewed_at": (
            share["last_viewed_at"].isoformat() if share.get("last_viewed_at") else None
        ),
    }


def _base_url(request: Request) -> str:
    """Where a link points.

    The configured public name wins: behind the tunnel the request's own host
    is an internal one, and a link built from it would not open for anybody.
    """
    if settings.public_share_base_url:
        return settings.public_share_base_url
    return str(request.base_url).rstrip("/")


@router.get("/conversations/{conversation_id}/share")
async def get_share(
    request: Request,
    conversation_id: str = Path(...),
    principal: Principal = Depends(current_principal),
) -> dict:
    """The owner's current share state, plus what policy would allow."""
    if principal is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    await _owned(conversation_id, principal)

    share = await db.run_in_thread(db.share_for_conversation, conversation_id)
    messages = await _messages_for(conversation_id)
    policy = await _policy_for(principal)
    verdict = sharing.evaluate(messages, policy=policy)

    payload = _share_payload(share, _base_url(request))
    unshared = 0
    if share and share.get("last_message_id") is not None:
        unshared = sum(
            1
            for m in messages
            if int(m.get("id") or 0) > int(share["last_message_id"])
            and m.get("role") in ("user", "assistant")
            and str(m.get("content") or "").strip()
        )
    return {
        "enabled": settings.conversation_sharing_enabled,
        "share": payload,
        "policy": verdict.as_dict(),
        "unshared_messages": unshared,
        "expiry_choices": [
            c for c in sharing.EXPIRY_CHOICES
            if c != "never" or policy["allow_never"]
        ],
        "default_expiry": f"{settings.public_share_default_days}d"
        if f"{settings.public_share_default_days}d" in sharing.EXPIRY_CHOICES
        else "30d",
    }


@router.post("/conversations/{conversation_id}/share")
async def create_share(
    request: Request,
    body: ShareCreate,
    conversation_id: str = Path(...),
    principal: Principal = Depends(current_principal),
) -> dict:
    if principal is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    if not settings.conversation_sharing_enabled:
        raise HTTPException(status_code=404, detail="Sharing is not available.")
    await _owned(conversation_id, principal)
    if not _rate_ok(
        _create_hits, principal.user_id, settings.share_create_rate_per_hour, 3600.0
    ):
        raise HTTPException(status_code=429, detail="Too many links created. Try later.")

    policy = await _policy_for(principal)
    messages = await _messages_for(conversation_id)
    verdict = sharing.evaluate(messages, policy=policy)

    allowed = verdict.public_allowed if body.visibility == "public" else verdict.workspace_allowed
    if not allowed:
        await db.run_in_thread(
            audit, principal, request, "conversation_share_blocked_by_policy",
            resource_type="conversation", resource_id=conversation_id,
            meta={"visibility": body.visibility, "reasons": verdict.blocking_reasons},
        )
        raise HTTPException(
            status_code=422,
            detail=verdict.blocking_reasons[0]
            if verdict.blocking_reasons
            else "This conversation cannot be shared.",
        )

    expires_at, error = sharing.resolve_expiry(body.expiry, policy=policy)
    if error:
        raise HTTPException(status_code=422, detail=error)

    show_name = bool(body.show_owner_name and policy["allow_owner_name"])
    conv = await db.run_in_thread(
        db.get_conversation, principal.user_id, conversation_id
    )
    owner_name = None
    if show_name:
        owner_name = principal.display_name or principal.username

    snapshot = sharing.build(
        conversation_title=(conv or {}).get("title") or "Shared conversation",
        messages=messages,
        owner_name=owner_name,
        created_at=datetime.now(timezone.utc),
    )
    public_id, token, secret_hash = sharing.mint_token()
    created = await db.run_in_thread(
        db.create_share,
        conversation_id=conversation_id,
        workspace_id=principal.workspace_id,
        created_by=principal.user_id,
        public_id=public_id,
        secret_hash=secret_hash,
        visibility=body.visibility,
        expires_at=expires_at,
        show_owner_name=show_name,
        payload=snapshot.payload,
        content_hash=snapshot.content_hash,
        last_message_id=snapshot.last_message_id,
        message_count=snapshot.message_count,
    )
    if created is None:
        # Lost the race with another click. The other one is live; return it,
        # WITHOUT a token, because that token belongs to the winning request.
        existing = await db.run_in_thread(db.share_for_conversation, conversation_id)
        return {"share": _share_payload(existing, _base_url(request)), "token": None}

    await db.run_in_thread(
        audit, principal, request, "conversation_share_created",
        resource_type="conversation", resource_id=conversation_id,
        meta={
            "share_id": created["id"], "visibility": body.visibility,
            # The addressable half only. A full token in an audit row is a
            # working link in an audit row.
            "public_id": public_id,
            "messages": snapshot.message_count,
            "truncated": snapshot.truncated,
        },
    )
    return {
        "share": _share_payload(created, _base_url(request)),
        # The ONLY time the secret half is ever returned.
        "token": token,
        "url": f"{_base_url(request)}/share/{token}",
        "truncated": snapshot.truncated,
    }


@router.post("/conversations/{conversation_id}/share/refresh")
async def refresh_share(
    request: Request,
    conversation_id: str = Path(...),
    principal: Principal = Depends(current_principal),
) -> dict:
    """Publish the conversation as it stands now, on the same link.

    Re-runs the WHOLE policy evaluation. New content that is not publicly
    shareable leaves the previous snapshot live and untouched — the failure
    mode to avoid is publishing something nobody reviewed because an earlier
    version was fine.
    """
    if principal is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    await _owned(conversation_id, principal)
    share = await db.run_in_thread(db.share_for_conversation, conversation_id)
    if not share:
        raise HTTPException(status_code=404, detail="This conversation is not shared.")

    policy = await _policy_for(principal)
    messages = await _messages_for(conversation_id)
    verdict = sharing.evaluate(messages, policy=policy)
    allowed = (
        verdict.public_allowed
        if share["visibility"] == "public"
        else verdict.workspace_allowed
    )
    if not allowed:
        raise HTTPException(
            status_code=422,
            detail=(verdict.blocking_reasons[0] if verdict.blocking_reasons else
                    "The new messages cannot be shared.")
            + " The existing link still shows the earlier version.",
        )

    conv = await db.run_in_thread(
        db.get_conversation, principal.user_id, conversation_id
    )
    owner_name = (
        (principal.display_name or principal.username)
        if share.get("show_owner_name")
        else None
    )
    snapshot = sharing.build(
        conversation_title=(conv or {}).get("title") or "Shared conversation",
        messages=messages,
        owner_name=owner_name,
        created_at=datetime.now(timezone.utc),
    )
    await db.run_in_thread(
        db.add_share_version,
        share_id=int(share["id"]),
        payload=snapshot.payload,
        content_hash=snapshot.content_hash,
        last_message_id=snapshot.last_message_id,
        message_count=snapshot.message_count,
        created_by=principal.user_id,
    )
    await db.run_in_thread(
        audit, principal, request, "conversation_share_updated",
        resource_type="conversation", resource_id=conversation_id,
        meta={"share_id": share["id"], "messages": snapshot.message_count},
    )
    fresh = await db.run_in_thread(db.share_for_conversation, conversation_id)
    return {"share": _share_payload(fresh, _base_url(request)), "truncated": snapshot.truncated}


@router.patch("/conversations/{conversation_id}/share")
async def patch_share(
    request: Request,
    body: ShareUpdate,
    conversation_id: str = Path(...),
    principal: Principal = Depends(current_principal),
) -> dict:
    if principal is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    await _owned(conversation_id, principal)
    share = await db.run_in_thread(db.share_for_conversation, conversation_id)
    if not share:
        raise HTTPException(status_code=404, detail="This conversation is not shared.")

    policy = await _policy_for(principal)
    expires_at, clear = None, False
    if body.expiry is not None:
        expires_at, error = sharing.resolve_expiry(body.expiry, policy=policy)
        if error:
            raise HTTPException(status_code=422, detail=error)
        clear = expires_at is None
    if body.visibility == "public":
        messages = await _messages_for(conversation_id)
        if not sharing.evaluate(messages, policy=policy).public_allowed:
            raise HTTPException(
                status_code=422, detail="This conversation cannot be shared publicly."
            )

    await db.run_in_thread(
        db.update_share_settings,
        int(share["id"]),
        visibility=body.visibility,
        expires_at=expires_at,
        clear_expiry=clear,
        show_owner_name=(
            None if body.show_owner_name is None
            else bool(body.show_owner_name and policy["allow_owner_name"])
        ),
    )
    action = (
        "conversation_share_visibility_changed"
        if body.visibility is not None
        else "conversation_share_expiration_changed"
    )
    await db.run_in_thread(
        audit, principal, request, action,
        resource_type="conversation", resource_id=conversation_id,
        meta={"share_id": share["id"], "visibility": body.visibility,
              "expiry": body.expiry},
    )
    fresh = await db.run_in_thread(db.share_for_conversation, conversation_id)
    return {"share": _share_payload(fresh, _base_url(request))}


@router.delete("/conversations/{conversation_id}/share")
async def revoke(
    request: Request,
    conversation_id: str = Path(...),
    principal: Principal = Depends(current_principal),
) -> dict:
    if principal is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    await _owned(conversation_id, principal)
    share = await db.run_in_thread(db.share_for_conversation, conversation_id)
    if not share:
        return {"revoked": False}
    done = await db.run_in_thread(
        db.revoke_share, int(share["id"]), revoked_by=principal.user_id
    )
    await db.run_in_thread(
        audit, principal, request, "conversation_share_revoked",
        resource_type="conversation", resource_id=conversation_id,
        meta={"share_id": share["id"]},
    )
    return {"revoked": done}


# ---------------------------------------------------------------------------
# The anonymous route
# ---------------------------------------------------------------------------

#: One sentence for every failure. See the module docstring.
_GONE = "This link is not available."


@router.get("/public/shares/{token}")
async def public_share(
    response: Response,
    token: str = Path(..., max_length=200),
    principal: Principal = Depends(current_principal),
) -> dict:
    """The public snapshot. No session required for a public link.

    A WORKSPACE link still requires one: it is checked here, after the token,
    so a wrong token and a wrong workspace are equally uninformative.
    """
    # A secret-bearing URL must not sit in a shared cache.
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response.headers["Referrer-Policy"] = "no-referrer"

    parts = sharing.split_token(token)
    if not parts:
        raise HTTPException(status_code=404, detail=_GONE)
    public_id, secret = parts

    if not _rate_ok(_view_hits, public_id, settings.share_view_rate_per_minute, 60.0):
        raise HTTPException(status_code=429, detail="Too many requests.")

    row = await db.run_in_thread(db.share_by_public_id, public_id)
    # Compare even when the row is missing, so a hit and a miss cost the same.
    expected = (row or {}).get("secret_hash") or "0" * 64
    if not sharing.secret_matches(secret, expected) or row is None:
        raise HTTPException(status_code=404, detail=_GONE)
    if row["status"] != "active":
        raise HTTPException(status_code=404, detail=_GONE)
    if row.get("expires_at") and row["expires_at"] <= datetime.now(timezone.utc):
        raise HTTPException(status_code=404, detail=_GONE)
    if row["visibility"] == "workspace":
        if principal is None or principal.workspace_id != row["workspace_id"]:
            raise HTTPException(status_code=404, detail=_GONE)
    payload = row.get("payload")
    if not payload:
        raise HTTPException(status_code=404, detail=_GONE)

    await db.run_in_thread(db.touch_share_view, int(row["id"]))
    return {
        "snapshot": payload,
        "visibility": row["visibility"],
        "shared_at": row["created_at"].isoformat(),
        "expires_at": row["expires_at"].isoformat() if row.get("expires_at") else None,
    }
