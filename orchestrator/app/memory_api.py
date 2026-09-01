"""Memory management routes (V10): list, add, and delete saved facts.

The same §3c contract as history.py: everything is scoped to the requesting
user inside the SQL, and a fact that is missing or another user's is a 404.
POST accepts a list so a ChatGPT-export import is one bulk call.
"""
from __future__ import annotations



from fastapi import APIRouter, Depends, HTTPException
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
            db.add_user_fact(user_id, fact, body.source_conversation_id)
        )
    return {"stored": stored, "skipped": len(body.facts) - len(stored)}


@router.delete("/facts/{fact_id}")
def delete_fact(fact_id: int, user: UserRow = Depends(require_user)) -> dict:
    if not db.delete_user_fact(int(user["id"]), fact_id):
        raise HTTPException(status_code=404, detail="fact not found")
    return {"deleted": fact_id}
