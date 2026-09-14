"""Retrieval over a request's files: the part of a large file the model sees
(design §5.4, 2026-09-13).

WHEN. A request whose referenced files total more than the inline cap
(PUBLIC_API_FILES_INLINE_MAX_TOKENS, 100,000 estimated tokens) in `auto` mode,
or any request in `retrieval` mode. A 1,000-page report at the measured 1,756
characters per page is ~585,000 estimated tokens: inlining it would put every
such question into the LONG admission lane (one >131,072-token prompt at a
time) with a prefill measured at 878 s for ~950k tokens. Retrieval hands the
model a 32,000-token pack instead.

THE STEPS, in order:

1. Embed the question once (`vectors.QUERY_INSTRUCTION`). If the embed gate
   refuses or the engine is down, rank LEXICALLY instead (BM25 over the chunk
   table) — retrieval is an upgrade, never a gate (`video/index.retrieve`'s
   rule), and a file question must not fail because a 0.6B sidecar is busy.
2. Search each file's vectors (top 64), merge, keep the best 64 overall — 100
   when there are three or more files, so one file cannot crowd out the rest.
3. Rerank the merged candidates with `techsara-rerank` under its public gate.
   A refusal or failure keeps vector order and records `rerank: "skipped"`.
4. Pack in rank order to the budget. Each chosen chunk pulls in its next
   neighbour when the budget allows (a table or sentence cut at a chunk edge
   stays whole), and the document's first chunk (title, abstract) is kept
   when it costs at most an eighth of the budget.
5. SUMMARY MODE (the question is the generic "Describe the attached files."):
   no ranking at all — evenly spaced chunks across each document, so
   "summarise this" sees the document's shape rather than one dense passage.

Output is ordered by chunk number within each file — reading order — and
context.py prints `[…]` between excerpts that are not adjacent.
"""
from __future__ import annotations

import asyncio
import heapq
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import chunks as chunk_store
from . import vectors

log = logging.getLogger(__name__)

#: Candidates kept after merging (design §5.4 step 2).
CANDIDATES = 64
CANDIDATES_MANY_FILES = 100
MANY_FILES = 3
#: Tokens a label line and separators add per excerpt (`[name p.123]\n`).
EXCERPT_OVERHEAD_TOKENS = 8

#: The question as the embed and rerank engines see it: its last 2,000
#: characters (2026-09-13 review). Both engines have a 4,096-token window
#: (PUBLIC_API_EMBED_CONTEXT_TOKENS); a longer prompt was refused whole, so a
#: request with a >4k-token question silently lost vector ranking AND rerank.
#: 2,000 characters is at most ~2,000 tokens even for CJK, which leaves a
#: 1,500-character chunk room beside it in a rerank pair.
QUERY_MAX_CHARS = 2000
#: Chunks the lexical fallback reads per request. Measured 10,000 chunks of
#: 1,500 chars in 0.38 s (one thread), so 100,000 is ~4 s; the fallback runs
#: exactly when the embed gate is saturated, and 20 files of 50,000 chunks
#: each would otherwise be ~38 s of CPU per request.
LEXICAL_MAX_CHUNKS = 100_000

#: The rerank instruction for document passages. The chat app's default names
#: "a web search query"; the memory note on the reranker (2026-09-03) is that
#: a mis-prompted reranker was the cause of a measured quality divergence, so
#: the instruction says what the pairs really are.
RERANK_INSTRUCTION = (
    "Given a question about an uploaded document, judge whether the passage "
    "contains the answer to the question"
)

#: (question, documents) → one score per document, or None when the rerank
#: gate refused in time.
Reranker = Callable[[str, Sequence[str]], Awaitable[Optional[Sequence[float]]]]


@dataclass(frozen=True)
class Source:
    """One file's searchable table: its id, its chunk directory, its kind."""

    file_id: str
    derived_dir: str
    kind: str = "pdf"


@dataclass(frozen=True)
class Excerpt:
    file_id: str
    chunk: chunk_store.Chunk
    score: float = 0.0
    reason: str = "hit"  # hit | neighbour | first | summary | around


@dataclass
class Retrieved:
    by_file: Dict[str, List[Excerpt]] = field(default_factory=dict)
    tokens: int = 0
    meta: Dict[str, object] = field(default_factory=dict)

    def pages(self, file_id: str) -> List[int]:
        return sorted({e.chunk.page_start for e in self.by_file.get(file_id, [])})


def excerpt_tokens(chunk: chunk_store.Chunk) -> int:
    from .. import context

    return int(context.estimate_tokens(chunk.text)) + EXCERPT_OVERHEAD_TOKENS


# ------------------------------------------------------------- lexical --

_WORD_RE = re.compile(r"[0-9A-Za-zÀ-￿][0-9A-Za-zÀ-￿'\-.,]*")
_STOP = frozenset(
    "a an and are as at be by for from has have how in is it its of on or that the this to was were "
    "what when where which who whom why will with do does did about into than then there these those "
    "can could should would may might i you he she we they them his her our your".split()
)


def terms(text: str) -> List[str]:
    out = []
    for raw in _WORD_RE.findall((text or "").lower()):
        word = raw.strip("'-.,")
        if len(word) >= 2 and word not in _STOP:
            out.append(word)
    return out


def retrieval_query(question: str) -> str:
    """The last QUERY_MAX_CHARS of the question, cut at a word boundary."""
    text = " ".join((question or "").split())
    if len(text) <= QUERY_MAX_CHARS:
        return text
    tail = text[-QUERY_MAX_CHARS:]
    space = tail.find(" ")
    return tail[space + 1:] if 0 <= space < 200 else tail


def _lexical_rank(
    question: str, sources: Sequence[Source], limit: int, max_chunks: int = LEXICAL_MAX_CHUNKS
) -> List[Tuple[float, str, int]]:
    """BM25 (k1 1.2, b 0.75) over the chunks of every file, two passes over
    each table, in a worker thread, reading at most `max_chunks` chunks in
    all (an equal share per file, so the first file cannot starve the
    rest). Returns (score, file_id, chunk_no)."""
    wanted = set(terms(question))
    if not wanted:
        return []
    per_file = max(1, int(max_chunks) // max(1, len(sources)))
    tables: List[Tuple[Source, List[Tuple[int, Counter, int]]]] = []
    df: Counter = Counter()
    total_len = 0
    count = 0
    for source in sources:
        rows: List[Tuple[int, Counter, int]] = []
        try:
            with chunk_store.ChunkTable(source.derived_dir) as table:
                for chunk in table:
                    if len(rows) >= per_file:
                        break
                    words = terms(chunk.text)
                    counts = Counter(w for w in words if w in wanted)
                    rows.append((chunk.chunk_no, counts, len(words)))
                    for word in counts:
                        df[word] += 1
                    total_len += len(words)
                    count += 1
        except OSError:
            continue
        tables.append((source, rows))
    if not count:
        return []
    avg = total_len / count
    heap: List[Tuple[float, str, int]] = []
    for source, rows in tables:
        for chunk_no, counts, length in rows:
            score = 0.0
            for word, tf in counts.items():
                idf = math.log(1 + (count - df[word] + 0.5) / (df[word] + 0.5))
                score += idf * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * length / max(avg, 1.0)))
            if score <= 0:
                continue
            item = (score, source.file_id, -chunk_no)
            if len(heap) < limit:
                heapq.heappush(heap, item)
            elif item > heap[0]:
                heapq.heapreplace(heap, item)
    return [(s, f, -n) for s, f, n in sorted(heap, reverse=True)]


# ----------------------------------------------------------- the steps --


async def _vector_candidates(
    qvec: Sequence[float], sources: Sequence[Source], limit: int
) -> Tuple[List[Tuple[float, str, int]], List[Source]]:
    """Per-file search, merged. Files with no usable index are returned for
    the lexical path instead of being silently skipped."""
    merged: List[Tuple[float, str, int]] = []
    unindexed: List[Source] = []
    for source in sources:
        try:
            hits = await vectors.search(source.derived_dir, qvec, top_k=limit)
        except (vectors.IndexNotReady, ValueError, OSError):
            unindexed.append(source)
            continue
        merged.extend((hit.score, source.file_id, hit.row) for hit in hits)
    merged.sort(key=lambda item: (-item[0], item[1], item[2]))
    return merged[:limit], unindexed


def _load(sources: Sequence[Source], wanted: Dict[str, Set[int]]) -> Dict[Tuple[str, int], chunk_store.Chunk]:
    by_id = {s.file_id: s for s in sources}
    out: Dict[Tuple[str, int], chunk_store.Chunk] = {}
    for file_id, numbers in wanted.items():
        source = by_id.get(file_id)
        if source is None or not numbers:
            continue
        try:
            with chunk_store.ChunkTable(source.derived_dir) as table:
                for number in sorted(numbers):
                    chunk = table.get(number)
                    if chunk is not None:
                        out[(file_id, number)] = chunk
        except OSError:
            continue
    return out


def _table_sizes(sources: Sequence[Source]) -> Dict[str, int]:
    sizes: Dict[str, int] = {}
    for source in sources:
        try:
            with chunk_store.ChunkTable(source.derived_dir) as table:
                sizes[source.file_id] = len(table)
        except OSError:
            sizes[source.file_id] = 0
    return sizes


async def retrieve(
    question: str,
    sources: Sequence[Source],
    *,
    budget_tokens: int,
    embed_query: Optional[vectors.QueryEmbedder],
    rerank: Optional[Reranker],
    summary: bool = False,
) -> Retrieved:
    """The steps of the module docstring; never raises for an engine outage."""
    result = Retrieved()
    budget = max(0, int(budget_tokens))
    if not sources or budget <= 0:
        result.meta = {"retrieval": "empty", "retrieval_hits": 0, "rerank": "not_needed"}
        return result
    if summary:
        return await _summary(sources, budget)

    limit = CANDIDATES_MANY_FILES if len(sources) >= MANY_FILES else CANDIDATES
    question = retrieval_query(question)
    qvec = await embed_query(question) if embed_query is not None else None
    method = "vector"
    merged: List[Tuple[float, str, int]] = []
    lexical_sources: List[Source] = list(sources)
    if qvec is not None:
        merged, lexical_sources = await _vector_candidates(qvec, sources, limit)
        if lexical_sources:
            method = "vector+lexical"
    else:
        method = "lexical"
    if lexical_sources:
        merged.extend(await asyncio.to_thread(_lexical_rank, question, lexical_sources, limit))
        merged.sort(key=lambda item: (-item[0], item[1], item[2]))
        merged = merged[:limit]

    wanted: Dict[str, Set[int]] = {}
    for _score, file_id, number in merged:
        wanted.setdefault(file_id, set()).update({number, number + 1})
    for source in sources:
        wanted.setdefault(source.file_id, set()).add(0)
    loaded = await asyncio.to_thread(_load, sources, wanted)
    candidates = [(score, fid, n) for score, fid, n in merged if (fid, n) in loaded]

    rerank_state = "not_needed"
    if rerank is not None and len(candidates) > 1:
        try:
            scores = await rerank(question, [loaded[(fid, n)].text for _s, fid, n in candidates])
        except Exception:  # noqa: BLE001 - an upgrade, never a gate
            log.warning("file retrieval: rerank failed; keeping vector order", exc_info=True)
            scores = None
        if scores is not None and len(scores) == len(candidates):
            order = sorted(range(len(candidates)), key=lambda i: (-float(scores[i]), i))
            candidates = [(float(scores[i]), candidates[i][1], candidates[i][2]) for i in order]
            rerank_state = "done"
        else:
            rerank_state = "skipped"

    chosen: Dict[Tuple[str, int], Excerpt] = {}
    used = 0

    def take(file_id: str, number: int, score: float, reason: str) -> bool:
        nonlocal used
        key = (file_id, number)
        if key in chosen or key not in loaded:
            return False
        cost = excerpt_tokens(loaded[key])
        if used + cost > budget:
            return False
        chosen[key] = Excerpt(file_id=file_id, chunk=loaded[key], score=score, reason=reason)
        used += cost
        return True

    for source in sources:
        if source.kind in ("audio", "video"):
            continue  # the "first chunk" rule is a document's title and abstract
        first = loaded.get((source.file_id, 0))
        if first is not None and excerpt_tokens(first) <= budget // 8:
            take(source.file_id, 0, 0.0, "first")
    hits = 0
    for score, file_id, number in candidates:
        if take(file_id, number, score, "hit"):
            hits += 1
            take(file_id, number + 1, score, "neighbour")
        if used >= budget:
            break

    for (file_id, _number), excerpt in sorted(chosen.items()):
        result.by_file.setdefault(file_id, []).append(excerpt)
    result.tokens = used
    result.meta = {
        "retrieval": method,
        "retrieval_hits": hits,
        "retrieval_candidates": len(candidates),
        "rerank": rerank_state,
    }
    return result


async def _summary(sources: Sequence[Source], budget: int) -> Retrieved:
    sizes = await asyncio.to_thread(_table_sizes, sources)
    total = sum(sizes.values())
    result = Retrieved()
    if total == 0:
        result.meta = {"retrieval": "summary", "retrieval_hits": 0, "rerank": "not_needed"}
        return result
    # Assume a full-size chunk for the spacing; the pack below stops at the
    # budget exactly either way.
    from .chunks import chunk_chars

    per_chunk = max(1, chunk_chars() // 3 + EXCERPT_OVERHEAD_TOKENS)
    affordable = max(1, budget // per_chunk)
    wanted: Dict[str, Set[int]] = {}
    for source in sources:
        n = sizes.get(source.file_id, 0)
        if n == 0:
            continue
        share = max(1, round(affordable * n / total))
        step = max(1, math.ceil(n / share))
        wanted[source.file_id] = set(range(0, n, step))
    loaded = await asyncio.to_thread(_load, sources, wanted)
    used = 0
    for key in sorted(loaded):
        cost = excerpt_tokens(loaded[key])
        if used + cost > budget:
            continue
        result.by_file.setdefault(key[0], []).append(Excerpt(key[0], loaded[key], 0.0, "summary"))
        used += cost
    result.tokens = used
    result.meta = {"retrieval": "summary", "retrieval_hits": 0, "rerank": "not_needed"}
    return result


# ------------------------------------------------------ engine (production) --


def make_engine_reranker() -> Reranker:
    """`publicapi.sidecars.rerank_scores` under the public `rerank` gate. None
    on a gate refusal or engine failure (step 3's fallback). The wait is the
    sidecar's own request budget (`PUBLIC_API_GATE_WAIT_S`), packed at ≤ 16
    pairs per call so chat reranks interleave."""

    async def rerank(question: str, documents: Sequence[str]) -> Optional[Sequence[float]]:
        from ..publicapi import sidecars

        try:
            outcome = await sidecars.rerank_scores(question, list(documents), instruction=RERANK_INSTRUCTION)
        except sidecars.SidecarError:
            return None
        except Exception:  # noqa: BLE001
            log.warning("file retrieval: rerank engine failed", exc_info=True)
            return None
        return outcome.scores

    return rerank
