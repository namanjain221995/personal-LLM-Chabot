"""Server-side conversation history (V2-DESIGN §3c). All endpoints require a
valid session; every row is scoped to the authenticated user_id, and touching
someone else's conversation is a plain 404 (indistinguishable from missing).

GET    /history/conversations?archived=<bool>    → [{id, title, updated_at,
                                                    pinned, archived}]
POST   /history/conversations {id?, title}        → conversation
GET    /history/conversations/{id}                → {id, title, messages: [...]}
PUT    /history/conversations/{id}                → conversation
       {title?, pinned?, archived?}                 (any subset; extras → 422)
POST   /history/conversations/{id}/messages {role, content, meta?}
DELETE /history/conversations/{id}
GET    /history/search?q=<query>&limit=<n>  → {results: [{id, title, updated_at,
                                                pinned, archived, snippet,
                                                matched_in}]}  (V4-DESIGN §2)
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from . import db
from .auth import require_user

router = APIRouter(prefix="/history", tags=["history"])

# Client-supplied conversation ids (the frontend uses its own uuids).
_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_TITLE_LENGTH = 200
_MAX_QUERY_LENGTH = 100  # V4-DESIGN §2: q is 1-100 characters after trimming


class ConversationIn(BaseModel):
    id: Optional[str] = None
    title: str


class ConversationUpdate(BaseModel):
    """PUT body: any subset of the mutable fields (V3-DESIGN §1).

    extra="forbid" so a typo'd or made-up field is a 422 instead of a silently
    ignored no-op — an omitted field means "leave it alone".
    """

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    pinned: Optional[bool] = None
    archived: Optional[bool] = None


class MessageIn(BaseModel):
    role: str
    content: str
    meta: Optional[dict] = None


class MessagesReplaceIn(BaseModel):
    """Whole-thread replace used by the client's offline sync."""

    messages: List[MessageIn]


def _clean_title(title: str) -> str:
    title = " ".join(title.split()).strip()
    if not title:
        raise HTTPException(status_code=400, detail="title must not be empty")
    return title[:_MAX_TITLE_LENGTH]


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="conversation not found")


@router.get("/conversations")
def list_conversations(
    archived: bool = False, user: sqlite3.Row = Depends(require_user)
) -> list:
    """Active conversations by default; `?archived=true` for the archive."""
    return db.list_conversations(int(user["id"]), archived=archived)


@router.post("/conversations")
def create_conversation(
    body: ConversationIn, user: sqlite3.Row = Depends(require_user)
) -> dict:
    conversation_id = body.id or uuid.uuid4().hex
    if not _CONVERSATION_ID_RE.match(conversation_id):
        raise HTTPException(
            status_code=400,
            detail="conversation id must be 1-64 characters from A-Z a-z 0-9 _ -",
        )
    title = _clean_title(body.title)
    try:
        return db.create_conversation(int(user["id"]), conversation_id, title)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="conversation id already exists")


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str, user: sqlite3.Row = Depends(require_user)
) -> dict:
    conversation = db.get_conversation(int(user["id"]), conversation_id)
    if conversation is None:  # missing OR someone else's → 404 (§3c)
        raise _not_found()
    conversation["messages"] = db.list_messages(conversation_id)
    return conversation


@router.put("/conversations/{conversation_id}")
def update_conversation(
    conversation_id: str,
    body: ConversationUpdate,
    user: sqlite3.Row = Depends(require_user),
) -> dict:
    conversation = db.update_conversation(
        int(user["id"]),
        conversation_id,
        title=_clean_title(body.title) if body.title is not None else None,
        pinned=body.pinned,
        archived=body.archived,
    )
    if conversation is None:  # missing OR someone else's → 404 (§3c)
        raise _not_found()
    return conversation


@router.post("/conversations/{conversation_id}/messages")
def add_message(
    conversation_id: str,
    body: MessageIn,
    user: sqlite3.Row = Depends(require_user),
) -> dict:
    role = body.role.strip()
    if not role or len(role) > 32:
        raise HTTPException(status_code=400, detail="role must be 1-32 characters")
    message = db.add_message(
        int(user["id"]), conversation_id, role, body.content, body.meta
    )
    if message is None:  # missing OR someone else's → 404 (§3c)
        raise _not_found()
    return message


@router.put("/conversations/{conversation_id}/messages")
def replace_messages(
    conversation_id: str,
    body: MessagesReplaceIn,
    user: sqlite3.Row = Depends(require_user),
) -> dict:
    """Replace a conversation's whole thread, atomically and never shrinking.

    This exists so the client's offline sync can reconcile a diverged tail
    WITHOUT the old delete-and-recreate dance, which permanently destroyed
    conversations whenever the local copy was empty or stale. A shorter
    incoming thread is a bug on the caller's side, so it is refused with 409
    rather than silently applied; the client then pulls server truth.
    """
    for m in body.messages:
        role = m.role.strip()
        if not role or len(role) > 32:
            raise HTTPException(
                status_code=400, detail="role must be 1-32 characters"
            )
    try:
        result = db.replace_messages(
            int(user["id"]),
            conversation_id,
            [
                {"role": m.role.strip(), "content": m.content, "meta": m.meta}
                for m in body.messages
            ],
        )
    except db.MessageCountWouldShrink as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"refusing to shrink conversation from {exc.existing} to "
                f"{exc.incoming} messages"
            ),
        )
    if result is None:  # missing OR someone else's → 404 (§3c)
        raise _not_found()
    return result


class TruncateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keep: int
    expected_total: int


@router.post("/conversations/{conversation_id}/truncate")
def truncate_messages(
    conversation_id: str,
    body: TruncateIn,
    user: sqlite3.Row = Depends(require_user),
) -> dict:
    """Drop every message after the first `keep` (user-confirmed regenerate).

    Deliberately separate from the sync endpoints: PUT /messages can never
    shrink a thread, and this can only ever remove a tail. `expected_total`
    must match the stored count, so a stale client cannot delete turns it
    never saw (409).
    """
    if body.keep < 0 or body.expected_total < 0:
        raise HTTPException(status_code=400, detail="keep/expected_total must be >= 0")
    if body.keep > body.expected_total:
        raise HTTPException(
            status_code=400, detail="keep must not exceed expected_total"
        )
    try:
        result = db.truncate_messages(
            int(user["id"]), conversation_id, body.keep, body.expected_total
        )
    except db.ConversationChanged as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"conversation changed: expected {exc.expected} messages, "
                f"found {exc.actual}"
            ),
        )
    if result is None:  # missing OR someone else's → 404 (§3c)
        raise _not_found()
    # The stored summary describes turns that no longer exist, so keeping it
    # would let the model assert things the user just removed.
    db.clear_summary(conversation_id)
    return result


@router.get("/conversations/{conversation_id}/summary")
def get_summary(
    conversation_id: str, user: sqlite3.Row = Depends(require_user)
) -> dict:
    """The rolling summary of compacted turns — what the assistant still
    remembers about the older part of this conversation (read-only)."""
    if db.get_conversation(int(user["id"]), conversation_id) is None:
        raise _not_found()
    row = db.get_summary(conversation_id)
    if row is None:
        return {"summary": None, "covers_through": 0}
    return {
        "summary": row["summary"],
        "covers_through": row["covers_through"],
        "updated_at": row["updated_at"],
    }


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: str, user: sqlite3.Row = Depends(require_user)
) -> dict:
    if not db.delete_conversation(int(user["id"]), conversation_id):
        raise _not_found()
    return {"ok": True}


@router.get("/search")
def search_history(
    q: str = "",
    limit: int = db.SEARCH_LIMIT_DEFAULT,
    user: sqlite3.Row = Depends(require_user),
) -> dict:
    """Search the signed-in user's chat titles and message bodies (V4-DESIGN §2).

    Nobody else's rows can appear: the match runs inside a user_id-scoped
    query, so a term that only occurs in another account's chats returns
    nothing at all. An empty or whitespace-only query is a no-op rather than an
    error — the palette opens with an empty box. `limit` is clamped to the hard
    cap instead of failing.
    """
    query = q.strip()
    if not query:
        return {"results": []}
    if len(query) > _MAX_QUERY_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"q must be 1-{_MAX_QUERY_LENGTH} characters",
        )
    return {"results": db.search_conversations(int(user["id"]), query, limit)}
