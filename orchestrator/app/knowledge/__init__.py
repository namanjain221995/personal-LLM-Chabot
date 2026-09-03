"""The knowledge contract (ADR-0001): scopes, the viewer, one retrieval call.

Everything the platform can put in front of the model comes from one of
three scopes, and the scope decides WHERE the filter lives:

    PUBLIC        the web corpus and resolved research claims. No user data,
                  by construction: `web_pages`/`web_claims` have no owner
                  column and nothing private is ever written to them
                  (engines/url.py stores a pasted link globally ON PURPOSE —
                  what one person read, everyone may know). Cacheable.
    USER          saved facts and cross-chat message recall. Filtered by
                  `user_id` INSIDE the SQL (db.fetch_message_embeddings,
                  db.recall_conversations, db.list_user_facts). Never cached
                  across users, never packed into a shared prompt fragment.
    CONVERSATION  uploaded documents, pasted-URL documents, indexed repos.
                  Keyed by conversation id and authorised by ownership
                  (`db.conversation_owner`) before any read (main.py /chat).

`retrieve()` is the single entry point every engine uses for PUBLIC
evidence — Fast grounding, the live search engine's stored-passage merge,
research seeding — so the same question yields the same evidence on every
route. It delegates to `web_memory.retrieve`, which is where candidates,
scoring, answerability and supersession live; this module owns the contract
(and asserts it), not the ranking.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..freshness import Freshness
from .. import web_memory

PUBLIC = "public"
USER = "user"
CONVERSATION = "conversation"

SCOPES = (PUBLIC, USER, CONVERSATION)


@dataclass(frozen=True)
class Viewer:
    """Who is asking. Carried by every retrieval that is not public."""

    user_id: int
    conversation_id: Optional[str] = None


def cacheable(scope: str) -> bool:
    """Only PUBLIC evidence may ever enter a cache shared between viewers."""
    return scope == PUBLIC


async def retrieve(
    question: str,
    *,
    level: Freshness = Freshness.RECENT,
    top_k: int = 5,
) -> web_memory.Retrieval:
    """Public evidence for `question`, ranked for the freshness it needs.

    Same signature as `web_memory.retrieve` on purpose; the addition is the
    contract check — every passage returned is public-scope, or the call
    fails loudly instead of letting a private passage ride into a shared
    prompt or cache.
    """
    result = await web_memory.retrieve(question, level=level, top_k=top_k)
    for ev in result.evidence:
        if ev.scope != PUBLIC:
            raise RuntimeError(f"non-public evidence in the public pipeline: {ev.url}")
    return result
