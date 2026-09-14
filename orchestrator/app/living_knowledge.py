"""The pre-answer stage: decide what a question needs, and get it cheaply.

One entry point, `prepare()`, called before the chat engine streams. It runs
the freshness classifier, looks in the local corpus, and — only when the
question is time-sensitive AND the corpus cannot answer it — spends a small
amount of network to close the gap.

THE BUDGET IS THE POINT. Turning every question into a web search would make
Fast mode slow and pointless; never searching is how the platform answered
"who's vice president of india" from 2024 weights while holding 19 pages that
said otherwise. So the ladder is:

    STATIC question            -> one local lookup; grounded ONLY on a strong
                                  match (a site indexed here, a doc research
                                  read) — otherwise nothing at all
    fresh local evidence       -> use it (one vector + one SQL query)
    resolved research claim    -> use it (a fact a Deep Research run verified)
    stale/absent, effort=fast  -> ONE query, 2 sources, hard deadline
    stale/absent, think/max    -> hand back to the full search engine

THE CORPUS IS SHARED. `web_pages` and `web_claims` hold PUBLIC web content
with no user attached: a page one user's search read, or a site one user
shared, grounds the next user's Fast answer the same way. That is by design —
it is what makes the platform's knowledge compound — and it is why nothing
private is ever written to either table.

Everything here fails soft: any error returns "no grounding" and the caller
answers exactly as it does today.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, List, Optional, Sequence

from . import db, metrics
from .config import settings
from .freshness import (
    Freshness,
    Verdict,
    classify,
    classify_offline,
    clearly_timeless,
    router_would_be_asked,
    static_timeless_task,
)
from .web_memory import (
    Retrieval,
    _stale_after,
    as_sources,
    cache_result,
    claims_block,
    claims_for,
    grounding_block,
    retrieve,
    staleness_note,
    supersession_allowed,
    topical_block,
)

log = logging.getLogger(__name__)

Emit = Callable[[str, dict], Awaitable[None]]


# ---------------------------------------------------------------------------
# Fast-effort pre-pass budget (performance plan item 2, 2026-09-13).
#
# Measured over 7 days before this: Fast/chat time to first token p50 1.46 s /
# p95 6.16 s against an engine TTFT of 0.05-0.2 s. The pre-pass here was the
# largest single share of the difference: the static-question retrieval cost
# p50 0.72 s / p90 2.09 s (techsara_web_memory_seconds, n=462) and found
# nothing on 282 of 308 static turns (92%); the freshness router added a mean
# 0.216 s on 72% of turns, BEFORE retrieval started.
#
# These tunables are read here with config.py's own parsing rules until they
# move into Settings under the same names; a Settings attribute of that name,
# once it exists, wins. Think and Max read none of them.
#
# Revised 2026-09-13 after the prover's FIX_FIRST: with retrieval at 0.6 s the
# flat 0.3 s deadline took prepare_fast answer@5 from 0.900 to 0.300 on the
# web_eval harness (7 real topical hits answered from weights), and the router
# skip answered "euro to dollar" / "is AWS down" from weights (live lookup 0/4
# where HEAD made 4/4). The deadline now applies only while the pre-check has
# not cleared the question, and the router skip is an allowlist of timeless
# tasks (freshness.clearly_timeless).
#
# Revised again 2026-09-13 (closing engineer, second prover pass): BOTH
# wall-clock bounds are now OFF by default. Under load they drop the very hits
# they were tuned to keep: conc_eval at a 0.72 s retrieval with 24x200k-char
# lexical pages answered 8/24 at c=8 and 15/48 at c=16 (HEAD 24/24, 48/48),
# every loss topical_hit_budget or topical_deadline, and 24/24 with the budget
# at 0. A bound measured from turn start cannot tell a slow hit under load
# from a miss. What stays on is the one skip that loses nothing: the
# pre-check proving that no stored page can pass the gate.
# ---------------------------------------------------------------------------

_TRUTHY = {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUTHY


#: KNOWLEDGE_FAST_TOPICAL_DEADLINE_S — OPT-IN, default 0 (off). How long a
#: Fast timeless question may wait for topical grounding while the pre-check
#: has NOT yet said a page can pass the gate. 0 or below = no short deadline
#: AND no hit budget: the whole wait as before 2026-09-13 (a proven miss still
#: ends early). Setting it trades answers for latency under load (see above).
_FAST_TOPICAL_DEADLINE_S = _env_float("KNOWLEDGE_FAST_TOPICAL_DEADLINE_S", 0.0)
#: KNOWLEDGE_FAST_TOPICAL_HIT_BUDGET_S — OPT-IN, default 0 (off); only read
#: when the deadline above is on. How long the wait may run, from the start of
#: the topical retrieval, once the pre-check HAS said a page can pass the
#: gate. 1.5 s kept answer@5 at 0.900 one turn at a time and dropped 16 of
#: 24 hits at c=8 (2026-09-13). 0 or below = no bound once cleared.
_FAST_TOPICAL_HIT_BUDGET_S = _env_float("KNOWLEDGE_FAST_TOPICAL_HIT_BUDGET_S", 0.0)
#: KNOWLEDGE_FAST_TOPICAL_PRECHECK — ask the page vocabulary whether ANY page
#: could pass the topical gate before waiting on the full hybrid retrieval.
_FAST_TOPICAL_PRECHECK = _env_bool("KNOWLEDGE_FAST_TOPICAL_PRECHECK", True)
#: FRESHNESS_FAST_SKIP_ROUTER — OPT-IN, default false (off; 2026-09-14). When
#: on, settle a Fast question that is clearly a timeless task
#: (freshness.clearly_timeless) as STATIC instead of asking the router. Off,
#: every undecided Fast question asks the router as before: the allowlist
#: still let 13 of 50 live-value questions in timeless-task shapes skip it.
_FAST_SKIP_ROUTER = _env_bool("FRESHNESS_FAST_SKIP_ROUTER", False)
#: KNOWLEDGE_FAST_CONCURRENT_RETRIEVE — start the time-sensitive retrieval
#: while the router is still deciding, instead of after it.
_FAST_CONCURRENT_RETRIEVE = _env_bool("KNOWLEDGE_FAST_CONCURRENT_RETRIEVE", True)


def fast_topical_deadline_s() -> float:
    return float(getattr(settings, "knowledge_fast_topical_deadline_s", _FAST_TOPICAL_DEADLINE_S))


def fast_topical_hit_budget_s() -> float:
    return float(getattr(settings, "knowledge_fast_topical_hit_budget_s", _FAST_TOPICAL_HIT_BUDGET_S))


def fast_topical_precheck() -> bool:
    return bool(getattr(settings, "knowledge_fast_topical_precheck", _FAST_TOPICAL_PRECHECK))


def fast_skip_router() -> bool:
    return bool(getattr(settings, "freshness_fast_skip_router", _FAST_SKIP_ROUTER))


def fast_concurrent_retrieve() -> bool:
    return bool(getattr(settings, "knowledge_fast_concurrent_retrieve", _FAST_CONCURRENT_RETRIEVE))


def _quiet(fut: "asyncio.Future") -> "asyncio.Future":
    """Mark an abandoned future's outcome as read, so a speculative run that
    failed after it stopped mattering does not log 'exception never retrieved'."""
    fut.add_done_callback(lambda f: f.cancelled() or f.exception())
    return fut


#: Why a Fast answer went on without topical grounding it was still waiting
#: for. Rides on Prepared.degraded (meta.knowledge.degraded) and on
#: knowledge_degraded_total{reason}, next to rerank_busy and prepare_timeout.
TOPICAL_DEADLINE = "topical_deadline"
#: Why a Fast answer went on without grounding the pre-check had said a page
#: COULD supply: the longer hit budget ran out too. Kept apart from
#: TOPICAL_DEADLINE because this one is a likely real hit dropped — the
#: failure the budget exists to prevent — and must be countable on its own.
TOPICAL_HIT_BUDGET = "topical_hit_budget"


def today_iso() -> str:
    """The server's real date. Never hardcoded — the whole failure this
    module addresses is a model reasoning from a frozen sense of 'now'."""
    return datetime.now(timezone.utc).date().isoformat()


@dataclass
class Prepared:
    """What the answer path should do with this question."""

    grounding: str = ""
    verdict: Optional[Verdict] = None
    retrieval: Optional[Retrieval] = None
    #: True when a live lookup ran. Surfaced on meta so the UI can show it.
    searched: bool = False
    #: Sources to attach to meta for citation, in the existing shape.
    sources: List[dict] = None  # type: ignore[assignment]
    #: Resolved research claims that grounded the answer, if any.
    claims: List[dict] = None  # type: ignore[assignment]
    #: Best answer probability among the relevant local passages (0 when the
    #: cross-encoder did not run). Drives local-first (ADR-0001 D6).
    confidence: float = 0.0
    #: How the question was served: local | stale_offline | escalate_search
    #: | fast_lookup | fast_lookup_failed | static_topical | static_model.
    decision: str = ""
    #: The store could not answer a time-sensitive question and the caller
    #: may spend network: run the full search engine (think/max).
    escalate: bool = False
    #: Why the cross-encoder's judgement is missing, when it is (rerank_busy,
    #: rerank_error, …). Rides out on meta.knowledge so a degraded answer is
    #: visible, and no escalation is spent on an unjudged verdict under load.
    degraded: str = ""

    @property
    def local_first(self) -> bool:
        """The store answers this well enough that an AUTO-decided web
        search would be spent for nothing: grounded, fresh for the verdict,
        and the cross-encoder is confident the evidence answers."""
        if not settings.knowledge_local_first or not self.grounding or self.searched:
            return False
        if self.retrieval is None or self.verdict is None:
            return False
        if self.verdict.volatile:
            # "latest release", "current price": a page inside the window can
            # still be three releases old. Think keeps searching (merged with
            # the stored passages); Fast is bounded by the volatile max age.
            return False
        if not self.retrieval.sufficient(self.verdict.max_age_seconds):
            return False
        return self.confidence >= float(settings.knowledge_local_first_confidence)

    def __post_init__(self) -> None:
        if self.sources is None:
            self.sources = []
        if self.claims is None:
            self.claims = []


#: Fast-mode live lookup: deliberately a fraction of a real search. A full
#: search rewrites the query, runs several providers, reranks and reads 5-8
#: pages; this reads two. It exists to correct a single stale fact, not to
#: research a topic. The deadline lives in settings (FRESHNESS_FAST_DEADLINE_S).
FAST_QUERIES = 1
FAST_SOURCES = 2
FAST_DEADLINE_S = 8.0


def _join(*parts: str) -> str:
    return "\n\n".join(p for p in parts if p)


#: A question this short is treated as a follow-up whose subject lives in the
#: previous turn. Same threshold the search path uses for the same judgement.
_TERSE_CONTENT_WORDS = 3
#: How many recovered terms to append. Enough to name an entity and its topic;
#: small enough that the retrieval query is still about what was asked.
_CONTEXT_TERMS = 6


def resolve_from_history(message: str, history: Sequence[dict]) -> str:
    """A terse follow-up, restored to something retrievable.

    `_topical`'s gate is deliberately high — a strong dense score AND lexical
    overlap — so an ordinary question never drags in a loosely related page.
    That is right for a question that stands on its own and wrong for a
    follow-up, which is artificially impoverished: "and the B200?" carries one
    content word, scores below every threshold, and falls through to
    `static_model`, i.e. the model answers from its own memory with no evidence
    at all.

    Measured on this box, 2026-09-06, against a seeded corpus:

        "What does an H100 cost per GPU-hour on Orbital Compute?"
            -> 1,584 chars of grounding, 2 sources, decision 'local'
        "and the B200?"
            -> 0 chars, 0 sources, decision 'static_model'
        "and the B200 price on Orbital Compute?"
            -> 1,584 chars, 2 sources, decision 'local'

    The middle one is what a user actually types, and in the benchmark it
    produced an invented price of $3.50 for a page that plainly states $6.75.
    Restoring the subject is what lets the high gate work as designed instead
    of silently disabling grounding.

    NO MODEL CALL. This is on the Fast path, where the whole point is latency,
    and the referent is already sitting in the conversation. The search path
    solves the same problem with `engines.search.resolve_question`, which reads
    the query rewrite it was already making; there is no rewrite here, so the
    terms come from the turns themselves.

    PRIVACY: `conversation_turns` drops every system message — saved facts,
    cross-chat recall, shared-page and document excerpts. Only what the user
    said and what the assistant answered can enter, which matters because this
    string becomes a retrieval query and, on the escalation path, a web search.

    The user's literal words are kept and the recovered terms are appended in
    parentheses, so nothing the user wrote is replaced.
    """
    text = (message or "").strip()
    if not text or not history:
        return text
    from .engines import conversation_turns
    from .web_memory import _content_words

    own = _content_words(text)
    if len(own) > _TERSE_CONTENT_WORDS:
        return text  # it stands on its own
    have = set(own)

    def _harvest(role: str) -> list:
        for message_ in reversed(conversation_turns(history, 4)):
            if message_.get("role") != role:
                continue
            content = message_.get("content")
            if not isinstance(content, str):
                continue
            picked = [w for w in _content_words(content) if w not in have]
            if picked:
                return picked
        return []

    # The user's own previous question first: it names the topic, and it is
    # what they would have repeated if asked to be explicit. The assistant's
    # answer is the fallback, for "and its score?" where the entity was named
    # only in the reply.
    extra = _harvest("user") or _harvest("assistant")
    if not extra:
        return text
    # Appended PLAIN, not parenthesised. The search path brackets its
    # resolution because that string is shown as the question line; this one
    # is embedded, and the punctuation measurably wrecks the dense score.
    # Measured 2026-09-06 on the seeded corpus, "what about 5.2?" after a
    # GPT-5 question (the gate needs dense >= 0.35):
    #
    #   "what about 5.2? (gpt-5 benchlm reasoning)"   dense 0.308  FAILS
    #   "what about 5.2? gpt-5 benchlm reasoning"     dense 0.493  passes
    #
    # Same terms, same order; only the brackets differ. With them the page
    # was not retrieved at all and the model invented a score of 89.2 for a
    # leaderboard that plainly states 82.7.
    return f"{text} {' '.join(extra[:_CONTEXT_TERMS])}"


#: The lexical floor BOTH topical gates share: `_topical_hit` below and the
#: STATIC pre-gate in `web_memory._answerability`, without which the
#: cross-encoder never judges a timeless question. A test pins the three
#: literals together.
_TOPICAL_LEXICAL_FLOOR = 0.34


# ---------------------------------------------------------------------------
# The pre-check's page vocabulary (2026-09-13, closing engineer).
#
# WHY NOT POSTGRESQL'S search_tsv ANY MORE. The first pre-check asked the GIN
# index whether any page carried enough of the question's words. The prover
# showed it was not sound, and on this box it is wrong in more ways than the
# two it demonstrated, because search_tsv and the gate tokenise differently:
#   - accents: the gate's `_WORD` splits "crème" into "cr" + "me", search_tsv
#     holds 'crème' ("explain crème brûlée caramelising": pre-check False,
#     HEAD grounded the answer);
#   - length: search_tsv covers the first 200,000 chars; the dense index
#     reaches 717,200 and a lexical window can sit anywhere ("orion-9 kestrel
#     reasoning suite", answer at char 205,000: pre-check False, HEAD grounded);
#   - the parser: on the 55432 test server to_tsvector keeps
#     '/usr/local/bin/python', 'user@example.com', 'ab’cd' and 'cost…' as
#     single lexemes, where `_WORD` yields "python", "example.com", "ab", "cost";
#   - stemming: `_stem` meets "lens"/"lenses" at 'lens', snowball keeps
#     'len' and 'lens' apart.
# Each turns a page the gate accepts into a pre-check "no page can", and the
# Fast answer is then given from weights where HEAD cited the page.
#
# SO THE PRE-CHECK NOW READS THE SAME TOKENS THE GATE READS. Every page's
# title and full text go through `web_memory._terms`' own rules once per
# process, and the set of stems is kept as a Bloom filter (no false
# negatives: a wrong "maybe" only costs the pre-change wait). A page whose
# vectors are waiting for a re-index (indexed_at IS NULL) also keeps the
# stems of its previous version, which the dense index may still serve.
# PostgreSQL is asked only for a one-row fingerprint of the corpus per turn,
# and the vocabulary is brought up to date before any answer is given from
# it; until then the pre-check says "could not tell".
#
# WHAT IS STILL NOT COVERED, measured as nothing and argued as rare: a
# LanceDB chunk for a page that no longer exists in PostgreSQL (retrieve keeps
# a dense hit without metadata), a title changed without a content change
# while the index kept the old one, and a word fragment made by a chunk or
# window edge ("configur|ation" cut into "configur").
# ---------------------------------------------------------------------------

#: Bloom filter shape. 16 bits and 4 hashes per stem: a false "maybe" on
#: about 0.24% of stems at the minimum fill, which only ever means waiting.
_VOCAB_BITS_PER_STEM = 16
_VOCAB_HASHES = 4
_VOCAB_MIN_BITS = 1 << 12
_VOCAB_MAX_BITS = 1 << 24
#: Tokenise in slices this long, cut at whitespace, yielding the GIL between
#: them: one `findall` over a 2,367,944-char page otherwise holds it for
#: about 80 ms, which every stream on the event loop would feel.
_VOCAB_SLICE_CHARS = 65_536
#: Pages read per query while (re)building.
_VOCAB_FETCH_BATCH = 16
#: Past these the vocabulary is not kept and the pre-check says "could not
#: tell" (the pre-change wait), rather than grow without bound. The live
#: corpus is 2,209 servable pages (2026-09-07).
_VOCAB_MAX_PAGES = 100_000
_VOCAB_MAX_TEXT_BYTES = 4 * 1024 * 1024 * 1024
_VOCAB_MAX_BYTES = 256 * 1024 * 1024
_UINT64 = (1 << 64) - 1
_WHITESPACE = re.compile(r"\s")

_VOCAB_FINGERPRINT_SQL = """
    SELECT count(*) AS n, coalesce(max(id), 0) AS top,
           coalesce(sum(hashtext(
               coalesce(content_hash, '') || ':' || coalesce(octet_length(text), 0) || ':' ||
               coalesce(title, '') || ':' || (indexed_at IS NULL)::text
           )::bigint), 0) AS h
      FROM web_pages"""


def _corpus_fingerprint() -> tuple:
    """What the vocabulary is keyed on: (pages, generation, 0) from V38's
    `web_corpus_state` row, which the database bumps at commit on exactly the
    changes `_VOCAB_FINGERPRINT_SQL` hashes (a page inserted or deleted, its
    content_hash, text length, title or indexed-ness). One primary-key read
    per turn instead of that statement's Seq Scan of web_pages (measured
    2.1 ms at today's corpus, 22.9 ms at 10x). The scan remains the fallback
    for a database without the row."""
    state = db.web_corpus_state()
    if state is not None:
        return (state[0], state[1], 0)
    with db.connection() as con:
        row = con.execute(_VOCAB_FINGERPRINT_SQL).fetchone()
    return (int(row["n"]), int(row["top"]), int(row["h"]))


def _page_stems(*texts: str) -> set:
    """The set `web_memory._terms` would produce over `texts`, computed over
    distinct words (a stem per word, not per occurrence)."""
    from .web_memory import _STOP, _WORD, _stem

    words: set = set()
    for text in texts:
        low = (text or "").lower()
        pos, size = 0, len(low)
        while pos < size:
            end = size
            if size - pos > _VOCAB_SLICE_CHARS:
                cut = _WHITESPACE.search(low, pos + _VOCAB_SLICE_CHARS)
                end = cut.start() if cut else size
            words.update(_WORD.findall(low, pos, end))
            pos = end
            if pos < size:
                time.sleep(0)
    return {_stem(w) for w in words if w not in _STOP and len(w) > 1}


def _stem_hashes(stems) -> List[int]:
    return [hash(s) & _UINT64 for s in stems]


def _bloom(stems: set) -> "tuple[int, bytes]":
    import numpy as np

    bits = _VOCAB_MIN_BITS
    while bits < len(stems) * _VOCAB_BITS_PER_STEM and bits < _VOCAB_MAX_BITS:
        bits <<= 1
    field = np.zeros(bits, dtype=bool)
    if stems:
        h = np.array(_stem_hashes(stems), dtype=np.uint64)
        h1, h2 = h & np.uint64(0xFFFFFFFF), (h >> np.uint64(32)) | np.uint64(1)
        for i in range(_VOCAB_HASHES):
            field[(h1 + np.uint64(i) * h2) & np.uint64(bits - 1)] = True
    return bits, np.packbits(field, bitorder="little").tobytes()


@dataclass
class _VocabState:
    fingerprint: tuple
    #: page id -> (listing key, bits, filter bytes)
    pages: Dict[int, tuple]
    #: [(bits, uint8 matrix pages x bits/8)], one per filter size
    matrices: list
    #: Over a bound: kept so the same corpus is not measured again every turn.
    disabled: bool = False

    def could_pass(self, stems: set, need: int) -> bool:
        """Does any page hold at least `need` of `stems`? Never a false no."""
        import numpy as np

        hashes = _stem_hashes(stems)
        n = len(hashes)
        for bits, matrix in self.matrices:
            mask = bits - 1
            pos = [((h & 0xFFFFFFFF) + i * ((h >> 32) | 1)) & mask for h in hashes for i in range(_VOCAB_HASHES)]
            cols = np.fromiter((p >> 3 for p in pos), dtype=np.intp, count=len(pos))
            bit = np.fromiter((1 << (p & 7) for p in pos), dtype=np.uint8, count=len(pos))
            present = ((matrix[:, cols] & bit) != 0).reshape(matrix.shape[0], n, _VOCAB_HASHES).all(axis=2)
            if bool((present.sum(axis=1) >= need).any()):
                return True
        return False


def _matrices(pages: Dict[int, tuple]) -> list:
    import numpy as np

    by_size: Dict[int, List[bytes]] = {}
    for _key, bits, blob in pages.values():
        by_size.setdefault(bits, []).append(blob)
    return [
        (bits, np.frombuffer(b"".join(blobs), dtype=np.uint8).reshape(len(blobs), bits // 8))
        for bits, blobs in sorted(by_size.items())
    ]


class _PageVocabulary:
    """Per-process stems of every stored page, kept in step with PostgreSQL."""

    def __init__(self) -> None:
        self.state: Optional[_VocabState] = None
        self._sync_lock = threading.Lock()

    def reset(self) -> None:
        with self._sync_lock:
            self.state = None

    def current(self) -> Optional[_VocabState]:
        """The vocabulary as of the corpus right now, or None when it cannot
        be had without waiting (another thread is building it) or at all.
        Blocking; no pooled connection is held while pages are tokenised."""
        fingerprint = _corpus_fingerprint()
        state = self.state
        if state is None or state.fingerprint != fingerprint:
            if fingerprint[0] > _VOCAB_MAX_PAGES:
                return None
            if not self._sync_lock.acquire(blocking=False):
                return None
            try:
                state = self.state
                if state is None or state.fingerprint != fingerprint:
                    state = self._sync(fingerprint, state)
                    self.state = state
            finally:
                self._sync_lock.release()
        return None if state.disabled else state

    def _sync(self, fingerprint: tuple, old: Optional[_VocabState]) -> Optional[_VocabState]:
        with db.connection() as con:
            listing = con.execute(
                """SELECT id, content_hash, octet_length(text) AS len,
                          md5(coalesce(title, '')) AS th, (indexed_at IS NULL) AS pending
                     FROM web_pages"""
            ).fetchall()
        if sum(int(r["len"] or 0) for r in listing) > _VOCAB_MAX_TEXT_BYTES:
            log.warning("topical pre-check: corpus text over %d bytes; pre-check says 'could not tell'", _VOCAB_MAX_TEXT_BYTES)
            return _VocabState(fingerprint=fingerprint, pages={}, matrices=[], disabled=True)
        previous = old.pages if old is not None else {}
        pages: Dict[int, tuple] = {}
        todo: Dict[int, tuple] = {}
        for r in listing:
            key = (r["content_hash"], r["len"], r["th"], bool(r["pending"]))
            kept = previous.get(int(r["id"]))
            if kept is not None and kept[0] == key:
                pages[int(r["id"])] = kept
            else:
                todo[int(r["id"])] = key
        ids = list(todo)
        size = sum(len(p[2]) for p in pages.values())
        for start in range(0, len(ids), _VOCAB_FETCH_BATCH):
            batch = ids[start : start + _VOCAB_FETCH_BATCH]
            pending = [i for i in batch if todo[i][3]]
            versions = {}
            with db.connection() as con:
                rows = con.execute(
                    "SELECT id, title, text FROM web_pages WHERE id = ANY(%s)", (batch,)
                ).fetchall()
                if pending:
                    versions = {
                        int(v["page_id"]): v
                        for v in con.execute(
                            """SELECT DISTINCT ON (page_id) page_id, title, text
                                 FROM web_page_versions WHERE page_id = ANY(%s)
                                ORDER BY page_id, superseded_at DESC""",
                            (pending,),
                        ).fetchall()
                    }
            for r in rows:
                pid = int(r["id"])
                prior = versions.get(pid) or {}
                stems = _page_stems(r["title"] or "", r["text"] or "", prior.get("title") or "", prior.get("text") or "")
                bits, blob = _bloom(stems)
                pages[pid] = (todo[pid], bits, blob)
                size += len(blob)
                if size > _VOCAB_MAX_BYTES:
                    log.warning("topical pre-check: vocabulary over %d bytes; pre-check says 'could not tell'", _VOCAB_MAX_BYTES)
                    return _VocabState(fingerprint=fingerprint, pages={}, matrices=[], disabled=True)
        return _VocabState(fingerprint=fingerprint, pages=pages, matrices=_matrices(pages))


_page_vocabulary = _PageVocabulary()


def _topical_precheck(question: str) -> Optional[bool]:
    """Could ANY stored page pass the topical gate? False only when none can.

    Sync; runs in the DB thread.

    WHY IT IS SOUND. A topical hit needs `lexical >= 0.34` on some candidate:
    directly in `_topical_hit`, and through `_answerability`, which judges a
    STATIC question only when a candidate already clears dense >= 0.35 AND
    lexical >= 0.34. `_lexical_score` counts at most 1 per distinct stem of
    `_terms(question)` found in the candidate's title or passage (title 1,
    body 0.5), so 0.34 over n stems needs at least ceil(0.34 * n) of them in
    the title or passage — and both are text of one stored page, whose stems
    `_page_vocabulary` holds in full (see the block comment above for what it
    does not hold).

    None = could not tell (the vocabulary is being built by another turn, is
    over its bounds, or the database failed): the caller must not skip on it.
    """
    from .web_memory import _terms

    stems = set(_terms(question))
    n = len(stems)
    if n == 0:
        return False  # lexical is 0 for every page; neither gate can pass
    need = math.ceil(_TOPICAL_LEXICAL_FLOOR * n - 1e-9)
    try:
        state = _page_vocabulary.current()
        if state is None:
            return None
        return state.could_pass(stems, need)
    except Exception:  # noqa: BLE001 — a pre-check must never cost grounding
        log.debug("topical pre-check unavailable", exc_info=True)
        return None


async def _fast_topical_retrieval(question: str, out: Prepared) -> Optional[Retrieval]:
    """The STATIC retrieval under Fast's budget, or None when it was skipped.

    The pre-check runs BESIDE the retrieval, not ahead of it, so a question
    that does have a strong page pays nothing extra for it. How long the
    retrieval may take depends on what the pre-check has said so far:

      "no page can"     -> skip now: a proven miss, not a degraded answer
      "a page can"      -> the pre-change wait, unless the opt-in hit budget
                           (KNOWLEDGE_FAST_TOPICAL_HIT_BUDGET_S) is on:
                           measured from the start, never shorter than the
                           short deadline
      not answered yet  -> the pre-change wait, unless the opt-in short
                           deadline (KNOWLEDGE_FAST_TOPICAL_DEADLINE_S) is on
      could not tell    -> the pre-change wait: no bound here at all (the
                           caller's KNOWLEDGE_PREPARE_DEADLINE_S still holds)
      pre-check off     -> the pre-change wait: without it a deadline cannot
                           tell a hit from a miss, and dropping hits is the
                           failure measured on 2026-09-13 (answer@5 0.9 -> 0.3)

    Whichever bound fires first, the model answers without grounding and the
    miss is recorded on Prepared.degraded and knowledge_degraded_total.
    """
    short = fast_topical_deadline_s()
    loop = asyncio.get_running_loop()
    start = loop.time()
    task = asyncio.ensure_future(retrieve(question, level=Freshness.STATIC, top_k=4))
    pre: Optional[asyncio.Future] = None
    if fast_topical_precheck():
        pre = _quiet(asyncio.ensure_future(db.run_in_thread(_topical_precheck, question)))
    # The bound in force: (absolute end or None, the reason recorded if it fires).
    ends: Optional[float] = start + short if (pre is not None and short > 0) else None
    reason = TOPICAL_DEADLINE
    try:
        while True:
            waiting = {task} if pre is None else {task, pre}
            timeout = None if ends is None else max(0.0, ends - loop.time())
            done, _ = await asyncio.wait(waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return task.result()
            if pre is not None and pre in done:
                try:
                    could = pre.result()
                except Exception:  # noqa: BLE001
                    could = None
                pre = None
                metrics.topical_precheck("miss" if could is False else "hit" if could else "fail")
                if could is False:
                    return None
                if could:
                    # A short deadline of 0 is the documented rollback to the
                    # pre-change wait, so it switches the hit budget off too.
                    budget = fast_topical_hit_budget_s()
                    ends = start + max(budget, short) if (budget > 0 and short > 0) else None
                    reason = TOPICAL_HIT_BUDGET
                else:
                    ends = None  # could not tell: wait as before 2026-09-13
                continue
            # A bound fired. The model answers from its own knowledge, exactly
            # as a static_model turn does, and the miss is countable.
            out.degraded = reason
            metrics.inc("knowledge_degraded_total", reason=reason)
            return None
    finally:
        for pending in (task, pre):
            if pending is not None and not pending.done():
                pending.cancel()


async def _topical(question: str, out: Prepared, *, effort: str = "") -> Prepared:
    """A timeless question answered from a STRONG local match, or nothing.

    This is what makes an indexed site a knowledge base: "how do I enable X"
    about a product whose documentation was crawled here answers from that
    documentation, cited, in any conversation and any mode. The gate is
    deliberately high — a strong hybrid score AND topical relevance — so an
    ordinary question never drags in a loosely related page.

    At Fast effort the retrieval is bounded (`_fast_topical_retrieval`);
    Think and Max wait for it as they always have.
    """
    started = time.perf_counter()
    if effort == "fast":
        fetched = await _fast_topical_retrieval(question, out)
        if fetched is None:
            _decided(out, "static_model")
            return out
        result = fetched
    else:
        result = await retrieve(question, level=Freshness.STATIC, top_k=4)
    out.retrieval = result
    # BOTH signals, not a high blend. Measured on the live corpus
    # (2026-09-02): the right documentation page scored 0.44-0.61 — a dense
    # match in the relevant band plus the question's own words on the page —
    # while the best unrelated page reached 0.25 with NO dense match at all.
    # A single blended threshold high enough to exclude the latter excluded
    # the former; requiring vector agreement AND lexical overlap separates
    # them cleanly, and the score floor only guards against junk.
    def _topical_hit(e) -> bool:
        # The cross-encoder's verdict, when it ran (ADR-0001 D4): a passage
        # that probably answers is topical whatever its vector distance.
        if e.scored and e.answer >= float(settings.knowledge_answer_threshold):
            return True
        return (
            e.dense >= 0.35
            and e.lexical >= _TOPICAL_LEXICAL_FLOOR
            and e.score >= settings.living_knowledge_topical_min_score
        )

    best = next((e for e in result.evidence if _topical_hit(e)), None)
    hit = best is not None
    metrics.web_memory_query(hit=hit, fresh=hit, seconds=time.perf_counter() - started)
    if not hit:
        _decided(out, "static_model")
        return out
    result.evidence = [e for e in result.evidence if _topical_hit(e) or e.relevant]
    out.grounding = topical_block(result, today_iso())
    out.sources = as_sources(result.evidence)
    out.confidence = result.confidence
    _decided(out, "static_topical")
    return out


def _decided(out: "Prepared", decision: str) -> None:
    """Record how this question was served — the route-mix number the
    escalation ladder is tuned by (ADR-0001 D12)."""
    out.decision = decision
    level = out.verdict.requirement.value if out.verdict else "unknown"
    metrics.inc("knowledge_decision_total", decision=decision, freshness=level)


async def prepare(
    question: str,
    *,
    effort: str,
    mode: str,
    web_search_pref: str,
    allow_network: bool,
    emit: Optional[Emit] = None,
    user_id: Optional[int] = None,
    conversation_id: str = "",
    history: Sequence[dict] = (),
) -> Prepared:
    """Freshness-aware grounding for one question.

    `allow_network` is the caller's policy (search enabled, not rate-limited,
    not an attachment turn). When False this degrades to local-only, which is
    also the offline path: stale evidence still answers, but it is labelled.
    `emit` lets the one slow branch (the live lookup) say what it is doing.
    """
    out = Prepared()
    if not settings.web_memory_enabled or not (question or "").strip():
        return out
    # A terse follow-up is resolved BEFORE retrieval, freshness classification
    # or any escalation decision — every one of them reads the question, and
    # all of them were reading a phrase with its subject missing.
    question = resolve_from_history(question, history)

    now = datetime.now(timezone.utc)
    fast = effort == "fast"
    router_on = bool(settings.freshness_router_enabled)
    # A time-sensitive retrieval started while the router decides (Fast only).
    speculative: Optional[asyncio.Future] = None
    speculative_verdict: Optional[Verdict] = None
    started = time.perf_counter()
    try:
        if fast and router_on and router_would_be_asked(question, now_year=now.year):
            if fast_skip_router() and clearly_timeless(question, now_year=now.year):
                # OPT-IN (FRESHNESS_FAST_SKIP_ROUTER, default off). A timeless
                # task with no live-value signal in it ("write me a haiku",
                # "hello, how are you?"): no router round trip. Any doubt goes
                # to the router below, as it did before. With the default,
                # every Fast question takes the router branch.
                verdict = static_timeless_task()
            else:
                if fast_concurrent_retrieve():
                    # The router answers RECENT for a question that carries a
                    # recency word far more often than anything else, so the
                    # retrieval that answer needs starts NOW, from the offline
                    # verdict re-labelled as the router's. It is used only if
                    # the real verdict reads it identically (below).
                    # It never WRITES the evidence cache (2026-09-13, second
                    # prover pass): the key carries no verdict, so a partition
                    # computed under a guessed 'router' reason that HEAD would
                    # never have computed could otherwise be served to a later
                    # turn whose router timed out ('default', no supersession).
                    # A reused result is cached below, under the real verdict.
                    speculative_verdict = replace(
                        classify_offline(question, now_year=now.year), reason="router"
                    )
                    speculative = _quiet(asyncio.ensure_future(
                        retrieve(
                            question,
                            level=speculative_verdict.requirement,
                            top_k=5,
                            effort=effort,
                            verdict=speculative_verdict,
                            cache_store=False,
                        )
                    ))
                verdict = await classify(question, now_year=now.year, allow_router=True)
        else:
            verdict = await classify(question, now_year=now.year, allow_router=router_on)
        out.verdict = verdict
        metrics.freshness_classified(verdict.requirement.value, verdict.reason)

        # `retrieve` reads the verdict for ONE thing: whether supersession may
        # run. Same level, same answer to that, same retrieval.
        reusable = (
            speculative is not None
            and speculative_verdict is not None
            and verdict.requirement is speculative_verdict.requirement
            and supersession_allowed(verdict.requirement, verdict)
            == supersession_allowed(speculative_verdict.requirement, speculative_verdict)
        )
        if speculative is not None and not reusable:
            speculative.cancel()

        if not verdict.needs_evidence:
            # Timeless. The model's own knowledge is the right source — unless
            # this platform has already read something that answers it closely.
            if settings.living_knowledge_topical:
                return await _topical(question, out, effort=effort)
            return out

        if reusable:
            result = await speculative  # type: ignore[misc]
            cache_result(question, level=verdict.requirement, top_k=5, result=result)
        else:
            # The call Think/Max always made. A cancelled or diverging
            # speculative run never wrote the cache, so reading it is safe.
            started = time.perf_counter()
            result = await retrieve(
                question, level=verdict.requirement, top_k=5, effort=effort, verdict=verdict
            )
    finally:
        if speculative is not None and not speculative.done():
            speculative.cancel()
    metrics.web_memory_query(
        hit=result.found,
        fresh=result.found and result.newest_age <= verdict.max_age_seconds,
        seconds=time.perf_counter() - started,
    )
    out.retrieval = result
    out.degraded = result.degraded

    # Facts an earlier Deep Research run RESOLVED — dated, sourced, already
    # judged against contradicting pages. Cheap (one tsvector query) and
    # the strongest local evidence there is for a live fact.
    claim_rows: List[dict] = []
    try:
        claim_rows = await db.run_in_thread(claims_for, question)
    except Exception:  # noqa: BLE001
        claim_rows = []
    claims_text = claims_block(claim_rows)

    def _claim_fresh(r: dict) -> bool:
        # Fresh as a RECORD (the run is recent) and as a FACT (its as_of is
        # not past the level's stale cutoff — a claim true in 2023 is not
        # evidence for who holds the office now).
        made = r.get("created_at")
        if not made or (now - made).total_seconds() > verdict.max_age_seconds:
            return False
        as_of = r.get("as_of")
        cutoff = _stale_after(verdict.requirement)
        if as_of and cutoff:
            if not isinstance(as_of, datetime):
                as_of = datetime.combine(as_of, datetime.min.time(), tzinfo=timezone.utc)
            if (now - as_of).total_seconds() > cutoff:
                return False
        return True

    claims_fresh = any(_claim_fresh(r) for r in claim_rows)
    metrics.inc(
        "knowledge_verdict_total",
        verdict=(
            "sufficient" if (result.sufficient(verdict.max_age_seconds) or claims_fresh)
            else "stale" if result.stale_answer
            else "insufficient"
        ),
        freshness=verdict.requirement.value,
    )

    if result.degraded == "rerank_busy" and not (
        result.sufficient(verdict.max_age_seconds) or claims_fresh
    ):
        # Under load the judge could not run. Spending a live lookup or a
        # full search on an UNJUDGED verdict is the feedback loop the design
        # critique warned about (every busy request escalating into the
        # search engine's own reranking). Answer from the labelled floor.
        if result.found or claims_text:
            out.grounding = _join(
                grounding_block(result, today_iso()),
                staleness_note(result, verdict.max_age_seconds),
                "NOTE: source verification was skipped because the platform is "
                "busy; treat these passages as unverified context.",
                claims_text,
            )
            out.sources = as_sources(result.evidence)
        _decided(out, "degraded_busy")
        return out
    if claim_rows:
        out.claims = [
            {"claim": r.get("claim"), "value": r.get("value"),
             "as_of": str(r.get("as_of") or ""), "url": r.get("url")}
            for r in claim_rows
        ]

    out.confidence = result.confidence
    if result.sufficient(verdict.max_age_seconds) or claims_fresh:
        # THE FIX. Evidence already on this machine, new enough to trust —
        # answered without touching the network, in any mode, at any effort.
        # Only passages that bear on the question reach the prompt: the
        # audit found two profile pages that never named the office holder
        # being cited for the answer, which a recalled earlier answer had
        # supplied.
        if any(e.relevant for e in result.evidence):
            result.evidence = [e for e in result.evidence if e.relevant]
        out.grounding = _join(grounding_block(result, today_iso()), claims_text)
        if not out.grounding:
            out.grounding = f"Current date: {today_iso()}.\n" + claims_text
        out.sources = as_sources(result.evidence)
        _decided(out, "local")
        return out

    # Not sufficient. Whether that is worth network depends on the caller.
    if not allow_network or web_search_pref == "off":
        # Explicitly offline, or the user turned search off. Answer from
        # what we have and SAY how old it is, rather than implying it is
        # current. (Until 2026-09-03 this read `off and effort != fast`, so
        # Fast with the search pill OFF still spent network — outside the
        # per-user rate limit and unattributed in the search log.)
        if result.found or claims_text:
            out.grounding = _join(
                grounding_block(result, today_iso()),
                staleness_note(result, verdict.max_age_seconds),
                claims_text,
            )
            out.sources = as_sources(result.evidence)
        _decided(out, "stale_offline")
        return out

    if effort != "fast":
        # The store cannot answer and the network may be spent: escalate to
        # the full search engine (ADR-0001 D6, stage 3), which merges these
        # stored passages with what it reads live. Until 2026-09-03 this
        # handed back stale evidence as a silent "floor" under an
        # instruction to prefer it — on the assumption that think/max were
        # about to search, which was false whenever the auto classifier had
        # decided not to.
        if result.found or claims_text:
            out.grounding = _join(
                grounding_block(result, today_iso()),
                staleness_note(result, verdict.max_age_seconds),
                claims_text,
            )
            out.sources = as_sources(result.evidence)
        # Only a CONFIRMED time-sensitive question escalates. The classifier
        # settles the ambiguous case as RECENT ("default") so that a local
        # lookup runs — that is cheap; a full web search for "write me a
        # poem" because the router timed out is not.
        out.escalate = verdict.reason != "default"
        _decided(out, "escalate_search" if out.escalate else "stale_offline")
        return out

    # Fast mode, time-sensitive question, nothing fresh locally: the one case
    # that justifies spending network in a mode whose whole promise is speed.
    if emit is not None:
        try:
            await emit("status", {"text": "Checking recent sources…"})
        except Exception:  # noqa: BLE001
            pass
    fresh = await _fast_lookup(
        question, verdict, user_id=user_id, conversation_id=conversation_id
    )
    if fresh is not None and fresh.found:
        out.searched = True
        if any(e.relevant for e in fresh.evidence):
            fresh.evidence = [e for e in fresh.evidence if e.relevant]
        out.grounding = _join(grounding_block(fresh, today_iso()), claims_text)
        out.sources = as_sources(fresh.evidence)
        out.retrieval = fresh
        out.confidence = fresh.confidence
        metrics.freshness_auto_search(True)
        _decided(out, "fast_lookup")
        return out

    metrics.freshness_auto_search(False)
    # The lookup failed (offline, rate limit, deadline). Stale evidence with an
    # honest date beats a confident wrong answer from 2024 weights.
    if result.found or claims_text:
        out.grounding = _join(
            grounding_block(result, today_iso()),
            staleness_note(result, verdict.max_age_seconds),
            claims_text,
        )
        out.sources = as_sources(result.evidence)
    _decided(out, "fast_lookup_failed")
    return out


async def _fast_lookup(
    question: str,
    verdict: Verdict,
    *,
    user_id: Optional[int] = None,
    conversation_id: str = "",
) -> Optional[Retrieval]:
    """One small search + fetch, then re-read the corpus.

    Reuses the search engine's own provider, SSRF-safe fetch, extraction and
    storage — nothing here fetches a URL by itself, so every protection that
    guards a normal search guards this too. Writing through the same store is
    what makes the NEXT conversation able to answer locally.
    """
    if not settings.freshness_fast_lookup:
        return None
    try:
        from .engines.search import fetch_for_freshness
    except Exception:  # noqa: BLE001
        return None

    deadline = float(getattr(settings, "freshness_fast_deadline_s", FAST_DEADLINE_S) or FAST_DEADLINE_S)
    sources = int(getattr(settings, "freshness_fast_sources", FAST_SOURCES) or FAST_SOURCES)
    try:
        async with asyncio.timeout(deadline):
            stored = await fetch_for_freshness(
                question,
                max_queries=FAST_QUERIES,
                max_sources=sources,
                user_id=user_id,
                conversation_id=conversation_id,
            )
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        log.debug("fast freshness lookup did not complete", exc_info=True)
        return None

    if not stored:
        return None
    # Read back through the SAME ranking the local path uses, so a freshly
    # fetched page is judged on authority and recency like any other —
    # bypassing the evidence cache, which still holds the pre-fetch result.
    return await retrieve(
        question,
        level=verdict.requirement,
        top_k=5,
        use_cache=False,
        effort="fast",
        verdict=verdict,
    )
