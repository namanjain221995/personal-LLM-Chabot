"""Semantic cross-chat recall (V10, 2026-08-21).

`memory_recall.py` finds past conversations by literal keyword overlap, so
"who runs the company?" never recalls an answer phrased as "the CEO is …".
This module adds meaning-based recall over the SAME stored messages: each
persisted chat message is embedded once (Qwen3-Embedding via EMBED_BASE_URL,
packed-float32 in PostgreSQL — see recall.py for why chat vectors never go
into the Salesforce LanceDB corpus) and each new question retrieves the
nearest messages from the user's OTHER conversations.

Embedding happens in arrears: `ensure_message_embeddings` drains a batch of
not-yet-embedded messages per request, so no persistence path had to change,
old messages backfill themselves, and a dead embedding service degrades to
keyword-only recall instead of failing the chat. Everything here is an
enhancement, never a precondition for answering.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from . import db, llm
from .config import settings
from .memory_recall import format_recall_block, keywords
from .recall import cosine_many, pack_vector

log = logging.getLogger(__name__)

# Snippet length mirrors db._RECALL_SNIPPET_CHARS so both recall styles read
# the same in the prompt.
_SNIPPET_CHARS = 240
# How many not-yet-embedded messages one request may embed. One batch is a
# single /embeddings call (~tens of ms); the backlog drains across requests.
_EMBED_BATCH = 64
# How many candidate vectors to score per query. Brute-force cosine over
# packed float32 rows. This said "hundreds of vectors cost well under a
# millisecond"; measured 2026-09-06 the per-candidate shape cost ~40 ms for
# 500 rows at 1024 dimensions, on the event loop. `recall.cosine_many` scores
# the batch in one pass, which brings 500 rows back under ~2 ms.
_CANDIDATE_LIMIT = 500


def _embedding_available() -> bool:
    """False on profiles with no embedding service (cpu, external-without-
    embeddings), where the launcher sets EMBED_MODEL=disabled and points
    EMBED_BASE_URL at disabled.invalid. Without this gate every assistant
    turn would spend an SDK-retried, doomed /embeddings call."""
    if (settings.embed_model or "").strip().lower() == "disabled":
        return False
    return "disabled.invalid" not in (settings.embed_base_url or "")


def _snippet(text: str) -> str:
    clean = " ".join((text or "").split())
    if len(clean) > _SNIPPET_CHARS:
        clean = clean[:_SNIPPET_CHARS] + "…"
    return clean


async def ensure_message_embeddings(user_id: int) -> int:
    """Embed a batch of stored messages that have no vector yet.

    Returns how many were embedded; 0 on any failure (recall then simply
    sees fewer candidates).
    """
    if not settings.cross_chat_semantic_enabled or not _embedding_available():
        return 0
    try:
        pending = await db.run_in_thread(
            db.messages_missing_embeddings,
            user_id,
            settings.embed_model,
            _EMBED_BATCH,
        )
        if not pending:
            return 0
        vectors = await llm.embed_texts([m["content"] for m in pending])
        if len(vectors) != len(pending) or not vectors[0]:
            return 0
        rows = [
            {
                "message_id": m["id"],
                "conversation_id": m["conversation_id"],
                "embedding": pack_vector(v),
            }
            for m, v in zip(pending, vectors)
        ]
        return await db.run_in_thread(
            db.store_message_embeddings,
            user_id,
            settings.embed_model,
            len(vectors[0]),
            rows,
        )
    except Exception:
        log.warning("message embedding backfill failed", exc_info=True)
        return 0


_backfills: dict = {}


def _backfill_in_background(user_id: int) -> None:
    """At most one backfill in flight per user; the task holds its own
    reference so it cannot be garbage-collected mid-flight."""
    task = _backfills.get(user_id)
    if task is not None and not task.done():
        return
    try:
        task = asyncio.get_running_loop().create_task(ensure_message_embeddings(user_id))
    except RuntimeError:  # pragma: no cover — no running loop
        return
    _backfills[user_id] = task
    task.add_done_callback(lambda t: _backfills.pop(user_id, None) if _backfills.get(user_id) is t else None)


async def semantic_hits(
    user_id: int,
    query: str,
    exclude_conversation_id: Optional[str],
    limit: int = 3,
) -> List[dict]:
    """Nearest stored messages from the user's other conversations.

    Returns [{title, role, snippet, conversation_id, score}] sorted by
    similarity; [] when disabled, nothing qualifies, or embedding fails.
    """
    if (
        not settings.cross_chat_semantic_enabled
        or not _embedding_available()
        or not (query or "").strip()
    ):
        return []
    try:
        candidates = await db.run_in_thread(
            db.fetch_message_embeddings,
            user_id,
            settings.embed_model,
            exclude_conversation_id,
            _CANDIDATE_LIMIT,
        )
        if not candidates:
            return []
        query_vec = await llm.embed_query(query)
        norm_query = " ".join((query or "").lower().split())
        # Another conversation asking the same question carries no
        # information — this is exactly the failure mode keyword recall
        # had before its snippet fix; don't reintroduce it semantically.
        kept = [
            c
            for c in candidates
            if " ".join((c["content"] or "").lower().split()) != norm_query
        ]
        scores = cosine_many(query_vec, [c["embedding"] for c in kept])
        scored = list(zip(scores, kept))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        hits: List[dict] = []
        seen_snippets: set = set()
        for score, c in scored:
            if score < settings.semantic_recall_min_score or len(hits) >= limit:
                break
            snippet = _snippet(c["content"])
            if snippet in seen_snippets:
                continue  # the same text stored in several conversations
            seen_snippets.add(snippet)
            hits.append(
                {
                    "title": c["title"],
                    "role": c["role"],
                    "snippet": snippet,
                    "conversation_id": c["conversation_id"],
                    "score": score,
                }
            )
        return hits
    except Exception:
        log.warning("semantic recall failed", exc_info=True)
        return []


async def cross_chat_block(
    user_id: int,
    query: str,
    exclude_conversation_id: Optional[str],
    *,
    semantic_limit: int = 3,
    keyword_limit: int = 3,
    include_assistant: bool = True,
) -> Optional[str]:
    """One combined recall block: semantic hits first, keyword hits after.

    Both retrievers run over the same stored messages, so overlapping hits
    are deduplicated by snippet. None when neither finds anything.

    `include_assistant=False` keeps only what the USER said in earlier
    chats. An answer the assistant gave before is only as good as it ever
    was, and for a fact that changes (who holds an office, what a thing
    costs) it is not evidence — recalled verbatim it becomes the answer, and
    the model attaches the current sources' citations to it (measured
    2026-09-03: 0/3 vs 3/3 answers driven by one recalled reply).
    """
    # The backfill (embedding this user's messages that have no vector yet)
    # used to run INLINE before every answer: one synchronous batch of up to
    # 64 embeddings on the critical path, against the most contended sidecar.
    # It now runs behind the answer; recall sees the new vectors from the
    # next turn on, which is when they can matter.
    _backfill_in_background(user_id)
    semantic = await semantic_hits(
        user_id, query, exclude_conversation_id, limit=semantic_limit
    )
    if not include_assistant:
        semantic = [h for h in semantic if (h.get("role") or "").lower() != "assistant"]
    keyword: List[dict] = []
    if keywords(query):
        try:
            keyword = await db.run_in_thread(
                db.recall_conversations,
                user_id,
                keywords(query),
                exclude_conversation_id,
                keyword_limit,
            )
        except Exception:
            log.warning("keyword recall failed", exc_info=True)
    if not include_assistant:
        keyword = [h for h in keyword if (h.get("role") or "").lower() != "assistant"]
    merged: List[dict] = []
    seen: set = set()
    for hit in [*semantic, *keyword]:
        snippet = hit.get("snippet") or ""
        if not snippet or snippet in seen:
            continue
        seen.add(snippet)
        merged.append(hit)
    return format_recall_block(merged)
