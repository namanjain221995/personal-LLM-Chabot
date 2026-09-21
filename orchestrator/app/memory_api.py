"""Memory management routes (V10): list, add, and delete saved facts.

The same §3c contract as history.py: everything is scoped to the requesting
user inside the SQL, and a fact that is missing or another user's is a 404.
POST accepts a list so a ChatGPT-export import is one bulk call. DELETE on
the collection clears the caller's whole memory, and only with
`confirm=all`.
"""
from __future__ import annotations



from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Annotated, List, Optional

from pydantic import BaseModel, Field, StringConstraints

from . import db
from .auth import UserRow, require_user
from .config import settings

router = APIRouter(prefix="/memory", tags=["memory"])


class FactsIn(BaseModel):
    facts: List[Annotated[str, StringConstraints(max_length=2000)]] = Field(
        min_length=1, max_length=500
    )
    source_conversation_id: Optional[
        Annotated[str, StringConstraints(max_length=128)]
    ] = None


@router.get("/facts")
def list_facts(user: UserRow = Depends(require_user)) -> dict:
    """Every saved fact, each one carrying where it came from.

    Per row: `source` / `source_excerpt` are the V40 columns — how the row
    was written and a fragment of the words behind it — and `origin`
    ('stated', 'manual', 'unknown') plus `trusted` are that provenance judged
    by db.fact_origin, the same judgement the identity line makes. A row
    written before V40 comes back origin 'unknown', trusted false and no
    excerpt: that is the honest answer, and it is the shape of the rows a
    pasted interview prompt left behind on 2026-09-16.
    """
    facts = db.list_user_facts(int(user["id"]), settings.memory_max_facts)
    return {"facts": facts}


@router.post("/facts")
def add_facts(body: FactsIn, user: UserRow = Depends(require_user)) -> dict:
    user_id = int(user["id"])
    # The provenance pointer must point at the CALLER's own conversation —
    # accepting an arbitrary id would let a fact claim to originate from a
    # chat its author cannot even read.
    if body.source_conversation_id:
        owner = db.conversation_owner(body.source_conversation_id)
        if owner is None or owner != user_id:
            body.source_conversation_id = None
    existing = {
        " ".join(f["fact"].lower().split()).rstrip(".")
        for f in db.list_user_facts(user_id, settings.memory_max_facts)
    }
    stored = []
    for raw in body.facts:
        fact = " ".join((raw or "").split())[:300]
        if len(fact) < 3:
            continue
        key = fact.lower().rstrip(".")
        if key in existing:  # already saved, or earlier in this same batch
            continue
        if len(existing) >= settings.memory_max_facts:
            break
        existing.add(key)
        stored.append(
            # Provenance (V40): the person added this one themselves, so it
            # is 'manual' — never 'stated', which means the extractor read it
            # out of a message.
            db.add_user_fact(
                user_id, fact, body.source_conversation_id, source="manual"
            )
        )
    return {"stored": stored, "skipped": len(body.facts) - len(stored)}


@router.delete("/facts/{fact_id}")
def delete_fact(fact_id: int, user: UserRow = Depends(require_user)) -> dict:
    if not db.delete_user_fact(int(user["id"]), fact_id):
        raise HTTPException(status_code=404, detail="fact not found")
    return {"deleted": fact_id}


#: One listing page per pass of the clear-all loop.
_CLEAR_PAGE = 500


@router.delete("/facts")
def clear_facts(
    confirm: Optional[str] = Query(default=None, max_length=16),
    user: UserRow = Depends(require_user),
) -> dict:
    """Delete every fact the caller has saved (B11).

    Irreversible, so a bare DELETE on the collection — a client bug, a
    replayed request — is refused: only `confirm=all` clears. Rows go
    through db.delete_user_fact one by one, the same owner-scoped statement
    as the single delete, and the loop keeps listing until nothing is left,
    because the listing is capped per call."""
    if confirm != "all":
        raise HTTPException(
            status_code=422, detail="clearing all memory needs confirm=all"
        )
    user_id = int(user["id"])
    deleted = 0
    while True:
        page = db.list_user_facts(user_id, _CLEAR_PAGE)
        removed = sum(1 for f in page if db.delete_user_fact(user_id, f["id"]))
        deleted += removed
        if not page or removed == 0:
            break
    return {"deleted": deleted}
