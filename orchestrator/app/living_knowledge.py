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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, List, Optional

from . import db, metrics
from .config import settings
from .freshness import Freshness, Verdict, classify
from .web_memory import (
    Retrieval,
    _stale_after,
    claims_block,
    claims_for,
    grounding_block,
    retrieve,
    staleness_note,
    topical_block,
)

log = logging.getLogger(__name__)

Emit = Callable[[str, dict], Awaitable[None]]


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


async def _topical(question: str, out: Prepared) -> Prepared:
    """A timeless question answered from a STRONG local match, or nothing.

    This is what makes an indexed site a knowledge base: "how do I enable X"
    about a product whose documentation was crawled here answers from that
    documentation, cited, in any conversation and any mode. The gate is
    deliberately high — a strong hybrid score AND topical relevance — so an
    ordinary question never drags in a loosely related page.
    """
    started = time.perf_counter()
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
            and e.lexical >= 0.34
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
    out.sources = [e.as_source() for e in result.evidence]
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

    now = datetime.now(timezone.utc)
    verdict = await classify(
        question,
        now_year=now.year,
        allow_router=settings.freshness_router_enabled,
    )
    out.verdict = verdict
    metrics.freshness_classified(verdict.requirement.value, verdict.reason)

    if not verdict.needs_evidence:
        # Timeless. The model's own knowledge is the right source — unless
        # this platform has already read something that answers it closely.
        if settings.living_knowledge_topical:
            return await _topical(question, out)
        return out

    started = time.perf_counter()
    result = await retrieve(
        question, level=verdict.requirement, top_k=5, effort=effort, verdict=verdict
    )
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
            out.sources = [e.as_source() for e in result.evidence]
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
        out.sources = [e.as_source() for e in result.evidence]
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
            out.sources = [e.as_source() for e in result.evidence]
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
            out.sources = [e.as_source() for e in result.evidence]
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
        out.sources = [e.as_source() for e in fresh.evidence]
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
        out.sources = [e.as_source() for e in result.evidence]
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
