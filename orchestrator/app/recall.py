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

Brute-force cosine over ONE conversation's chunks was described here as
"microseconds". Measured 2026-09-06, it is not: the per-candidate shape cost
16 ms for 200 chunks at 1024 dimensions and 316 ms for 1000 at 4096, all of it
synchronous CPU on the event loop, where it stalls every concurrent request.
`get_conversation_chunks` is unbounded, so that grows with conversation length.
`cosine_many` scores the whole set in one pass instead.
"""
from __future__ import annotations

import array
import asyncio
import hashlib
import logging
import math
import operator
import time
from typing import List, Optional, Sequence

from . import db, llm, metrics
from .config import settings

log = logging.getLogger(__name__)

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


def cosine_many(query: Sequence[float], blobs: Sequence[bytes]) -> List[float]:
    """Cosine of `query` against each packed vector, scored in one pass.

    Same ranking as calling `cosine(query, unpack_vector(b))` per blob, but
    without the two costs that shape made unavoidable: `unpack_vector`
    materialised every candidate into a Python list, and `cosine` recomputed
    the *query* norm — identical for every candidate — once per candidate.

    This runs on the event loop, so its cost stalls every other in-flight
    request. Measured on this box, 2026-09-06, scoring one query against N
    packed vectors (wall-clock, per call):

        dim   N     per-candidate cosine()   this   pure-python fallback
        1024  300        ~24 ms             0.066 ms       ~24 ms
        4096  1000      315.9 ms            1.066 ms      198.2 ms

    NO BLAS, DELIBERATELY. The obvious spelling — `matrix @ query` with
    `np.linalg.norm` — is a BLAS call, and OpenBLAS answers it by waking a
    thread per core and busy-waiting afterwards. On this 20-core box that cost
    16x the CPU for no wall-clock gain whatsoever: 0.095 ms of wall time billed
    as 1.553 ms of CPU, measured in a fresh process. In a web server that is
    pure harm — the spin competes with the event loop it was meant to unblock,
    and with vLLM on the same machine. `einsum` computes the same products
    through numpy's own single-threaded loops: same result, 1.00x CPU-to-wall
    ratio, and faster than the BLAS form anyway. Do not "simplify" this to `@`.

    numpy is used when importable and a pure-python path is kept for when it
    is not — see requirements.txt. Results agree with the per-candidate path to
    ~6e-7 (float32 accumulation order), far below any gap that changes a
    ranking, and the ranking itself is asserted identical in the tests.
    """
    if not query or not blobs:
        return [0.0] * len(blobs)
    dim = len(query)
    try:
        import numpy as np
    except ImportError:
        return _cosine_many_py(query, blobs, dim)

    qa = np.asarray(query, dtype=np.float32)
    nq = float(np.sqrt(np.einsum("i,i->", qa, qa)))
    if nq == 0.0:
        return [0.0] * len(blobs)
    # A blob of the wrong width would corrupt the reshape, so anything that
    # is not exactly `dim` float32s is scored 0.0 rather than reinterpreted.
    width = dim * 4
    good = [i for i, b in enumerate(blobs) if len(b) == width]
    if not good:
        return [0.0] * len(blobs)
    matrix = np.frombuffer(
        b"".join(blobs[i] for i in good), dtype=np.float32
    ).reshape(len(good), dim)
    norms = np.sqrt(np.einsum("ij,ij->i", matrix, matrix))
    sims = np.einsum("ij,j->i", matrix, qa) / np.where(norms == 0.0, 1.0, norms * nq)
    sims = np.where(norms == 0.0, 0.0, sims)
    out = [0.0] * len(blobs)
    for slot, score in zip(good, sims.tolist()):
        out[slot] = float(score)
    return out


def _cosine_many_py(
    query: Sequence[float], blobs: Sequence[bytes], dim: int
) -> List[float]:
    """cosine_many without numpy: query norm hoisted, no list materialisation."""
    qa = array.array("f", query)
    nq = math.sqrt(sum(x * x for x in qa))
    if nq == 0.0:
        return [0.0] * len(blobs)
    width = dim * 4
    out: List[float] = []
    for blob in blobs:
        if len(blob) != width:
            out.append(0.0)
            continue
        vec = array.array("f")
        vec.frombytes(blob)
        nv = math.sqrt(sum(x * x for x in vec))
        out.append(
            0.0 if nv == 0.0 else sum(map(operator.mul, qa, vec)) / (nq * nv)
        )
    return out


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


#: EmbedUnavailable.reason -> the recall_block_dropped_total{reason} label.
#: Anything that is not busy or a timeout is an error.
_DROP_REASONS = {"busy": "embed_busy", "timeout": "embed_timeout"}

#: The least budget worth a second attempt: one query embedding measures
#: 20-60 ms on the sidecar (2026-09-13), so less than this left would only
#: spend the rest of the budget on a call that cannot land.
_RETRY_MIN_S = 0.05


def _conversation_ref(conversation_id: str) -> str:
    """What the log may say about a conversation: a short hash, never the id
    (ids are joinable to a person's history)."""
    return hashlib.sha256((conversation_id or "").encode("utf-8")).hexdigest()[:12]


async def _embed_question_as_before(question: str) -> Optional[List[float]]:
    """The call recall made before 2026-09-13, for Think and Max: the raw
    question through `embed_texts`, which waits out a sidecar restart
    (sidecar_recovery_s) instead of dropping the block."""
    return (await llm.embed_texts([question]))[0]


async def _embed_question(conversation_id: str, question: str) -> Optional[List[float]]:
    """The question's vector, or None when the recall block must be dropped.

    WHY (2026-09-13). In-conversation recall moved from `embed_texts` (a 90 s
    batch timeout plus the sidecar recovery window) to the bounded
    `embed_query` (EMBED_WAIT_S for a slot, EMBED_TIMEOUT_S for the call).
    That took the recall embedding off the tail of time-to-first-token, but
    it turned a burst or a sidecar blip into a block that silently vanished
    from the prompt — the folded turns the summary dropped. So a drop is now
    counted (recall_block_dropped_total{reason}) and logged at INFO with the
    conversation's hash only, and a failure that leaves budget is tried once
    more.

    THE BUDGET is EMBED_WAIT_S (default 1.0 s) from the first attempt's
    start — the wait `embed_query` already allows one caller — so the retry
    never moves the first token past that mark: it only runs when at least
    _RETRY_MIN_S is left, and it is cut off when the budget runs out. A busy
    first attempt has, by definition, spent the wait, so it is not retried;
    a fast failure (a pooled connection reset, a 503 in a restart) is.
    """
    budget = float(settings.embed_wait_s)
    started = time.perf_counter()
    try:
        return await llm.embed_query(question, normalise=False)
    except llm.EmbedUnavailable as exc:
        failure = exc
    retried = False
    remaining = budget - (time.perf_counter() - started)
    if failure.reason != "empty" and remaining >= _RETRY_MIN_S:
        retried = True
        try:
            async with asyncio.timeout(remaining):
                return await llm.embed_query(question, wait=remaining, timeout=remaining, normalise=False)
        except llm.EmbedUnavailable as exc:
            failure = exc
        except TimeoutError:
            failure = llm.EmbedUnavailable("recall embedding budget spent", reason="timeout")
    reason = _DROP_REASONS.get(failure.reason, "embed_error")
    metrics.inc("recall_block_dropped_total", reason=reason)
    log.info(
        "in-conversation recall block dropped: conversation=%s reason=%s retried=%s elapsed_ms=%.0f",
        _conversation_ref(conversation_id),
        reason,
        retried,
        (time.perf_counter() - started) * 1000.0,
    )
    return None


async def retrieve_block(
    conversation_id: str, question: str, top_k: Optional[int] = None, *, effort: str = ""
) -> Optional[str]:
    """The labelled block of folded chunks most relevant to `question`.

    None when recall is disabled, nothing has been folded yet, or the
    embedding service is unavailable — recall is an enhancement, never a
    precondition for answering.

    WHICH EMBEDDING (second prover pass, 2026-09-13). Only a Fast turn takes
    the bounded, cached `embed_query` path, whose budget is what keeps its
    first token inside a second — and which, during a sidecar restart or
    burst, drops the block (counted and logged) where the old call waited
    through the recovery window. Think and Max are not on that clock, so they
    keep the old call exactly (`_embed_question_as_before`): the raw question,
    `embed_texts`, recovery included. Both embed the question unnormalised,
    so the vector, and so the chunk ranking, is the one HEAD computed.
    """
    if not settings.semantic_recall_enabled or not (question or "").strip():
        return None
    try:
        chunks = await db.run_in_thread(db.get_conversation_chunks, conversation_id)
        if not chunks:
            return None
        # The cached, single-flight query embedding (2026-09-13, plan item
        # 4): cross-chat recall embeds this same question in the same turn,
        # concurrently, and the uncached `embed_texts` paid a second sidecar
        # round trip for an identical vector. A failure is retried once
        # inside the budget, then counted and logged (_embed_question).
        if effort == "fast":
            query = await _embed_question(conversation_id, question)
        else:
            query = await _embed_question_as_before(question)
        if query is None:
            return None
        scores = cosine_many(query, [c["embedding"] for c in chunks])
        scored = list(zip(scores, chunks))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        k = top_k or settings.retrieve_top_k
        best = [c for score, c in scored[:k] if score > 0.0]
        if not best:
            return None
        lines = [f"[{c['role']}] {c['text']}" for c in best]
        return RECALL_HEADER + "\n" + "\n\n".join(lines)
    except Exception:
        return None
