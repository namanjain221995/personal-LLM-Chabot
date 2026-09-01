"""The pre-answer stage: decide what a question needs, and get it cheaply.

One entry point, `prepare()`, called before the chat engine streams. It runs
the freshness classifier, looks in the local corpus, and — only when the
question is time-sensitive AND the corpus cannot answer it — spends a small
amount of network to close the gap.

THE BUDGET IS THE POINT. Turning every question into a web search would make
Fast mode slow and pointless; never searching is how the platform answered
"who's vice president of india" from 2024 weights while holding 19 pages that
said otherwise. So the ladder is:

    STATIC question            -> nothing at all (0 ms, no I/O)
    fresh local evidence       -> use it (one vector + one SQL query)
    stale/absent, effort=fast  -> ONE query, 2 sources, hard deadline
    stale/absent, think/max    -> hand back to the full search engine

Everything here fails soft: any error returns "no grounding" and the caller
answers exactly as it does today.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from . import metrics
from .config import settings
from .freshness import Freshness, Verdict, classify
from .web_memory import Retrieval, grounding_block, retrieve, staleness_note

log = logging.getLogger(__name__)


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

    def __post_init__(self) -> None:
        if self.sources is None:
            self.sources = []


#: Fast-mode live lookup: deliberately a fraction of a real search. A full
#: search rewrites the query, runs several providers, reranks and reads 5-8
#: pages; this reads two. It exists to correct a single stale fact, not to
#: research a topic.
FAST_QUERIES = 1
FAST_SOURCES = 2
FAST_DEADLINE_S = 12.0


async def prepare(
    question: str,
    *,
    effort: str,
    mode: str,
    web_search_pref: str,
    allow_network: bool,
) -> Prepared:
    """Freshness-aware grounding for one question.

    `allow_network` is the caller's policy (search enabled, not rate-limited,
    not an attachment turn). When False this degrades to local-only, which is
    also the offline path: stale evidence still answers, but it is labelled.
    """
    out = Prepared()
    if not settings.web_memory_enabled or not (question or "").strip():
        return out

    now = datetime.now(timezone.utc)
    verdict = await classify(
        question,
        now_year=now.year,
        allow_router=settings.freshness_router_enabled,
    )
    out.verdict = verdict
    metrics.freshness_classified(verdict.requirement.value, verdict.reason)

    if not verdict.needs_evidence:
        # Timeless. The model's own knowledge is the right source, and this
        # path costs nothing at all beyond the regex above.
        return out

    started = time.perf_counter()
    result = await retrieve(question, level=verdict.requirement, top_k=5)
    metrics.web_memory_query(
        hit=result.found,
        fresh=result.found and result.newest_age <= verdict.max_age_seconds,
        seconds=time.perf_counter() - started,
    )
    out.retrieval = result

    if result.sufficient(verdict.max_age_seconds):
        # THE FIX. Evidence already on this machine, new enough to trust —
        # answered without touching the network, in any mode, at any effort.
        out.grounding = grounding_block(result, today_iso())
        out.sources = [e.as_source() for e in result.evidence]
        return out

    # Not sufficient. Whether that is worth network depends on the caller.
    if not allow_network or web_search_pref == "off" and effort != "fast":
        # Explicitly offline, or the user turned search off at think/max where
        # they get the full engine anyway. Answer from what we have and SAY
        # how old it is, rather than implying it is current.
        if result.found:
            out.grounding = "\n".join(
                x for x in (
                    grounding_block(result, today_iso()),
                    staleness_note(result, verdict.max_age_seconds),
                ) if x
            )
            out.sources = [e.as_source() for e in result.evidence]
        return out

    if effort != "fast":
        # think/max already run the full search engine when the orchestrator
        # asks for it; duplicating a lookup here would be two searches for one
        # question. Pass what we have as a floor and let that path do its job.
        if result.found:
            out.grounding = grounding_block(result, today_iso())
            out.sources = [e.as_source() for e in result.evidence]
        return out

    # Fast mode, time-sensitive question, nothing fresh locally: the one case
    # that justifies spending network in a mode whose whole promise is speed.
    fresh = await _fast_lookup(question, verdict)
    if fresh is not None and fresh.found:
        out.searched = True
        out.grounding = grounding_block(fresh, today_iso())
        out.sources = [e.as_source() for e in fresh.evidence]
        out.retrieval = fresh
        metrics.freshness_auto_search(True)
        return out

    metrics.freshness_auto_search(False)
    # The lookup failed (offline, rate limit, deadline). Stale evidence with an
    # honest date beats a confident wrong answer from 2024 weights.
    if result.found:
        out.grounding = "\n".join(
            x for x in (
                grounding_block(result, today_iso()),
                staleness_note(result, verdict.max_age_seconds),
            ) if x
        )
        out.sources = [e.as_source() for e in result.evidence]
    return out


async def _fast_lookup(question: str, verdict: Verdict) -> Optional[Retrieval]:
    """One small search + fetch, then re-read the corpus.

    Reuses the search engine's own provider, SSRF-safe fetch, extraction and
    storage — nothing here fetches a URL by itself, so every protection that
    guards a normal search guards this too. Writing through the same store is
    what makes the NEXT conversation able to answer locally.
    """
    try:
        from .engines.search import fetch_for_freshness
    except Exception:  # noqa: BLE001
        return None

    try:
        async with asyncio.timeout(FAST_DEADLINE_S):
            stored = await fetch_for_freshness(
                question, max_queries=FAST_QUERIES, max_sources=FAST_SOURCES
            )
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        log.debug("fast freshness lookup did not complete", exc_info=True)
        return None

    if not stored:
        return None
    # Read back through the SAME ranking the local path uses, so a freshly
    # fetched page is judged on authority and recency like any other.
    return await retrieve(question, level=verdict.requirement, top_k=5)
