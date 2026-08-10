"""Semantic recall over compacted turns (Phase B).

A rolling summary keeps what the summarizer judged important; anything it
dropped would otherwise be gone for good. So every turn that gets folded is
also embedded, and each new question retrieves the most relevant folded
chunks back into the prompt under a clearly labelled block.

SESSION ISOLATION is structural, not a filter applied afterwards: chunks live
in `conversation_chunks` keyed by conversation_id and the only read path is
`db.get_conversation_chunks(conversation_id)`. Nothing can return another
session's text because nothing else is ever loaded.

Storage note: these embeddings live in PostgreSQL next to the chunk, NOT in the
LanceDB `chunks` table. That table is the SALESFORCE corpus — the RAG engine
searches it and renders hits as sourced Salesforce citations, so putting
conversation text there would let private chat content surface as if it came
from the CRM, and would break the per-session boundary. Reuse is at the model
level instead: the same Qwen3-Embedding-0.6B service on :8003, same vectors.
Brute-force cosine over ONE conversation's chunks is microseconds.
"""
from __future__ import annotations

import array
import math
from typing import List, Optional, Sequence

from . import db, llm
from .config import settings

# Folded turns are chunked so a long message can be retrieved in parts.
_CHUNK_CHARS = 1200
_CHUNK_OVERLAP = 150
# Low enough to keep short factual turns ("port 9000 is the fallback", "the
# codename is ORION-7") — the very things a summary is most likely to drop —
# while still skipping bare acknowledgements ("ok", "thanks", "sounds good").
_MIN_CHUNK_CHARS = 15


def pack_vector(vector: Sequence[float]) -> bytes:
    return array.array("f", [float(v) for v in vector]).tobytes()


def unpack_vector(blob: bytes) -> List[float]:
    out = array.array("f")
    out.frombytes(blob)
    return list(out)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def chunk_text(text: str) -> List[str]:
    """Split one turn into overlapping windows; overlap avoids cutting a fact
    exactly at a boundary and losing it from both sides."""
    clean = (text or "").strip()
    if len(clean) <= _CHUNK_CHARS:
        return [clean] if len(clean) >= _MIN_CHUNK_CHARS else []
    chunks: List[str] = []
    start = 0
    while start < len(clean):
        piece = clean[start : start + _CHUNK_CHARS]
        if len(piece) >= _MIN_CHUNK_CHARS:
            chunks.append(piece)
        if start + _CHUNK_CHARS >= len(clean):
            break
        start += _CHUNK_CHARS - _CHUNK_OVERLAP
    return chunks


async def index_folded(
    conversation_id: str, folded: Sequence[dict], first_ordinal: int
) -> int:
    """Embed and store turns that were just folded into the summary."""
    if not settings.semantic_recall_enabled:
        return 0
    texts: List[str] = []
    meta: List[dict] = []
    ordinal = first_ordinal * 1000  # room for many chunks per turn
    for offset, turn in enumerate(folded):
        content = turn.get("content")
        if not isinstance(content, str):
            continue
        for piece in chunk_text(content):
            texts.append(piece)
            meta.append(
                {
                    "ordinal": ordinal + len(texts),
                    "role": turn.get("role", "user"),
                    "text": piece,
                }
            )
        ordinal = (first_ordinal + offset + 1) * 1000
    if not texts:
        return 0
    vectors = await llm.embed_texts(texts)
    rows = [
        {**m, "embedding": pack_vector(v)} for m, v in zip(meta, vectors)
    ]
    await db.run_in_thread(db.add_conversation_chunks, conversation_id, rows)
    return len(rows)


RECALL_HEADER = (
    "Relevant earlier messages from THIS conversation (retrieved because they "
    "were compacted out of the recent history):"
)


async def retrieve_block(
    conversation_id: str, question: str, top_k: Optional[int] = None
) -> Optional[str]:
    """The labelled block of folded chunks most relevant to `question`.

    None when recall is disabled, nothing has been folded yet, or the
    embedding service is unavailable — recall is an enhancement, never a
    precondition for answering.
    """
    if not settings.semantic_recall_enabled or not (question or "").strip():
        return None
    try:
        chunks = await db.run_in_thread(db.get_conversation_chunks, conversation_id)
        if not chunks:
            return None
        query = (await llm.embed_texts([question]))[0]
        scored = [
            (cosine(query, unpack_vector(c["embedding"])), c) for c in chunks
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        k = top_k or settings.retrieve_top_k
        best = [c for score, c in scored[:k] if score > 0.0]
        if not best:
            return None
        lines = [f"[{c['role']}] {c['text']}" for c in best]
        return RECALL_HEADER + "\n" + "\n\n".join(lines)
    except Exception:
        return None
