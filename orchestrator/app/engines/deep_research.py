"""Deep Research: iterative, source-grounded investigation (2026-08-30).

Web Search answers a question in one pass — search, read a handful of pages,
answer. Deep Research is the other shape: PLAN the question into
subquestions, search each of them, read, judge what is still MISSING, search
again for exactly that, and only then write a long report. The difference
that matters is the loop: a gap analysis after every round decides whether
another round happens, so a question that needs six angles gets six, and one
that is already answered stops early instead of burning the budget.

WHY A PLAIN ASYNC LOOP AND NOT LANGGRAPH. LangGraph is already a dependency
and `engines/agent.py` compiles a graph — but that graph uses none of its
features (no conditional edges, no loops, no checkpointer), and every engine
written since (search, crawl, sf_intel) is a plain async function with
module-level tuned constants. An iterative gap-driven loop is precisely the
shape that fights a fixed three-node edge list, so it is written here the way
the rest of the codebase is written.

WHAT IT REUSES. Everything expensive already exists in `engines/search.py`
and is called directly rather than reimplemented: `_collect_results` (the
round-robin merge over parallel SearXNG queries, per-domain capped),
`_rerank_results` (the Qwen3-Reranker cross-encoder), `_fetch_sources`
(SSRF-guarded fetch + readable extraction + the PostgreSQL warm-page store),
and `_persist_and_index` (write-behind logging and embedding). Deep Research
adds the loop, the evidence registry, and the report — not a second search
stack.

COST. Every model call is the LOCAL vLLM/Qwen deployment and every search is
the LOCAL SearXNG: no paid API is required or contacted on this path.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from .. import db, llm
from ..config import settings
from ..core.sf_intel.planner import extract_json_object
from ..search.base import SearchResult, SearchUnavailableError
from . import recent_turns
from .search import (
    _Source,
    _apply_char_tiers,
    _collect_results,
    _fetch_sources,
    _normalize_url,
    _rerank_results,
    _spawn,
    _persist_and_index,
)

log = logging.getLogger(__name__)

Emit = Callable[[str, dict], Awaitable[None]]

#: Research reasoning runs on the main model, but NEVER at an unbounded
#: concurrency: interactive chat shares this vLLM. Two in flight keeps a
#: research run moving without pushing a chat turn behind a queue (the engine
#: is memory-bandwidth bound, so the third concurrent generation costs every
#: other stream latency rather than adding throughput).
_LLM_SEM = asyncio.Semaphore(2)

#: One research run at a time per orchestrator process. A second run would
#: double every budget below against the same SearXNG and the same GPU; the
#: user gets a plain answer that says so rather than a starved one.
_RUN_LOCK = asyncio.Lock()

#: A citation the model wrote: [1], [12].
_CITE_RE = re.compile(r"\[(\d{1,3})\]")

#: Category routing. Measured on this host 2026-08-30: a default general query
#: reaches only google cse / bing / mwmbl / yahoo — and google cse, the one
#: high-volume engine, IP-blocked this host during testing. `categories=science`
#: (arxiv, pubmed) answers with FULL ABSTRACTS — 1100-1900 character snippets
#: against ~140 for bing — from a pool nothing else here queries, so it neither
#: competes for the general engines' quota nor arrives thin.
#:
#: `categories=it` is deliberately NOT routed to, despite being the healthiest
#: pool by result count. Its engines are github/stackoverflow/mdn/DOCKER HUB,
#: and on the first live research run 9 of 23 sources came back as Docker Hub
#: image pages ("vllm/vllm-openai - Docker Image", "redislabs/memtier_benchmark")
#: plus MDN's `eval()` page — matched on the words "vllm", "benchmark", "memory"
#: and "eval" in the queries. A registry listing is not evidence. The pool stays
#: reachable through the provider for callers that want it; research does not.
_SCIENCE_RE = re.compile(
    r"\b(paper|papers|study|studies|research|arxiv|preprint|publication|"
    r"evaluation|benchmark results|architecture|algorithm|survey|"
    r"state of the art|sota)\b",
    re.I,
)


def route_category(query: str) -> str:
    """The SearXNG category pool most likely to answer this query well.

    Empty means the default general pool, which is the right answer for most
    queries — routing is an upgrade for the minority that clearly want the
    academic index, not a classifier the loop depends on.
    """
    if _SCIENCE_RE.search(query or ""):
        return "science"
    return ""


#: Time-sensitive wording, so the report knows to prefer recent evidence and
#: to say when a stored copy may be stale. Reuses the search engine's shape.
_RECENCY_RE = re.compile(
    r"\b(latest|current|today|this week|this month|this year|recent|now|"
    r"20\d\d|newest|up to date|state of the art)\b",
    re.I,
)


# ---------------------------------------------------------------------------
# Structured-output schemas. The local vLLM supports response_format
# json_schema (verified live), so these CANNOT come back malformed; when a
# runtime without guided decoding is used, llm.json_completion downgrades to
# an unconstrained retry and the validation below still holds.
# ---------------------------------------------------------------------------

def _plan_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "subquestions": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 8,
            },
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 10,
            },
        },
        "required": ["subquestions", "queries"],
        "additionalProperties": False,
    }


def _gap_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "sufficient": {"type": "boolean"},
            "missing": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
            "contradictions": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 4,
            },
            "followup_queries": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 6,
            },
        },
        "required": ["sufficient", "missing", "followup_queries"],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# Research state
# ---------------------------------------------------------------------------


@dataclass
class SourceRecord:
    """One real, fetched page. The registry entry a citation resolves to."""

    n: int
    title: str
    url: str
    text: str
    query: str
    iteration: int

    @property
    def domain(self) -> str:
        return urlparse(self.url).hostname or self.url


@dataclass
class ResearchState:
    research_id: str
    conversation_id: str
    question: str
    subquestions: List[str] = field(default_factory=list)
    queries_run: List[str] = field(default_factory=list)
    seen_urls: Set[str] = field(default_factory=set)
    sources: List[SourceRecord] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    contradictions: List[str] = field(default_factory=list)
    iterations: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def budget_left(self) -> bool:
        return (
            self.iterations < settings.deep_research_max_iterations
            and len(self.sources) < settings.deep_research_max_sources
            and self.elapsed < settings.deep_research_timeout_s
        )


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


async def _plan(question: str, history: Sequence[dict], effort: str) -> Tuple[List[str], List[str]]:
    """Decompose the question into subquestions + a first round of queries."""
    system = (
        "You are a research planner. Break the user's question into the "
        "distinct SUBQUESTIONS that must each be answered for the whole "
        "question to be answered well, then write web-search queries that "
        "would find evidence for them. Each query must look for something "
        "DIFFERENT — never paraphrase another query. Prefer specific, "
        "technical phrasing over conversational phrasing. Return JSON only."
    )
    msgs = [
        {"role": "system", "content": system},
        *recent_turns(history, 4),
        {"role": "user", "content": question},
    ]
    try:
        async with _LLM_SEM:
            raw = await llm.json_completion(
                msgs,
                json_schema=_plan_schema(),
                schema_name="research_plan",
                temperature=0.2,
                max_tokens=900,
                thinking=False,
            )
        data = extract_json_object(raw) or {}
    except Exception:  # noqa: BLE001 — planning is an upgrade, never a gate
        log.warning("research planning failed; falling back to the raw question", exc_info=True)
        data = {}

    subs = [s.strip() for s in (data.get("subquestions") or []) if isinstance(s, str) and s.strip()]
    queries = [q.strip() for q in (data.get("queries") or []) if isinstance(q, str) and q.strip()]
    if not queries:
        queries = [question]
    cap = settings.deep_research_max_queries_per_iteration
    return subs[:8], queries[:cap]


async def _assess(state: ResearchState, effort: str) -> dict:
    """Read what we have and decide whether another round is warranted."""
    covered = "\n".join(
        f"[{s.n}] {s.title} ({s.domain}) — {s.text[:400]}" for s in state.sources[-24:]
    )
    system = (
        "You are a research auditor. Given the question, its subquestions and "
        "the evidence gathered so far, decide whether the evidence is enough "
        "to write a well-supported report. Be strict: if a subquestion has no "
        "supporting source, it is NOT sufficient. List what is missing and "
        "write follow-up web-search queries that would close exactly those "
        "gaps — do not repeat queries already run. Note any place where "
        "sources contradict each other. Return JSON only."
    )
    user = (
        f"QUESTION: {state.question}\n\n"
        f"SUBQUESTIONS:\n" + "\n".join(f"- {s}" for s in state.subquestions) + "\n\n"
        f"QUERIES ALREADY RUN:\n" + "\n".join(f"- {q}" for q in state.queries_run) + "\n\n"
        f"EVIDENCE ({len(state.sources)} sources):\n{covered}"
    )
    try:
        async with _LLM_SEM:
            raw = await llm.json_completion(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                json_schema=_gap_schema(),
                schema_name="research_gaps",
                temperature=0.1,
                max_tokens=800,
                thinking=False,
            )
        data = extract_json_object(raw) or {}
    except Exception:  # noqa: BLE001
        log.warning("gap analysis failed; treating evidence as sufficient", exc_info=True)
        return {"sufficient": True, "missing": [], "followup_queries": []}
    if not isinstance(data.get("sufficient"), bool):
        data["sufficient"] = True
    return data


async def _gather(
    state: ResearchState,
    queries: List[str],
    effort: str,
    emit: Optional[Emit],
) -> List[SourceRecord]:
    """One round: search → rerank → fetch → register. Never raises."""
    # Group by routed category so each group is one _collect_results call
    # (which already parallelises within itself and merges round-robin).
    by_category: Dict[str, List[str]] = {}
    for q in queries:
        by_category.setdefault(route_category(q), []).append(q)

    results: List[SearchResult] = []
    for category, group in by_category.items():
        try:
            found = await _collect_results(group, effort, emit, category)
        except SearchUnavailableError:
            continue
        except Exception:  # noqa: BLE001 — one bad group must not end the run
            log.warning("research search group failed (%s)", category or "general", exc_info=True)
            continue
        results.extend(found)
    if not results:
        return []
    state.queries_run.extend(queries)

    # Drop anything already read in an earlier round BEFORE spending a fetch.
    fresh = [r for r in results if _normalize_url(r.url) not in state.seen_urls]
    if not fresh:
        return []

    room = max(0, settings.deep_research_max_sources - len(state.sources))
    want = min(settings.deep_research_sources_per_iteration, room)
    if want <= 0:
        return []
    fresh = await _rerank_results(state.question, fresh, len(fresh))
    fresh = fresh[:want]

    for r in fresh:
        state.seen_urls.add(_normalize_url(r.url))

    try:
        fetched = await _fetch_sources(fresh, state.question)
    except Exception:  # noqa: BLE001
        log.warning("research round fetch failed", exc_info=True)
        return []

    added: List[SourceRecord] = []
    for src in fetched:
        if not (src.text or "").strip():
            continue
        added.append(
            SourceRecord(
                n=len(state.sources) + len(added) + 1,
                title=src.title,
                url=src.url,
                text=src.text,
                query=queries[0] if queries else state.question,
                iteration=state.iterations,
            )
        )
    state.sources.extend(added)
    # Remember the round for the next question, exactly like a plain search.
    _spawn(_persist_and_index(state.question, queries, results, effort))
    return added


# ---------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------


def validate_citations(report: str, source_count: int) -> Tuple[str, List[int]]:
    """Remove any [n] that does not resolve to a real gathered source.

    The search engine's only defence against a fabricated citation is a
    sentence in the prompt, and the frontend STRIPS [n] markers before
    rendering — so an invented [99] is invisible rather than caught. A report
    is a document people quote, so here the marker is checked against the
    registry and dropped when it resolves to nothing. Returns the cleaned
    report and the list of invalid numbers found (for logging).
    """
    invalid: List[int] = []

    def _sub(m: re.Match) -> str:
        n = int(m.group(1))
        if 1 <= n <= source_count:
            return m.group(0)
        invalid.append(n)
        return ""

    cleaned = _CITE_RE.sub(_sub, report)
    # A citation run like "[1][99]" can leave a double space behind.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned, invalid


def _evidence_block(sources: Sequence[SourceRecord]) -> str:
    return "\n\n".join(
        f"[{s.n}] {s.title} ({s.url})\n{s.text}" for s in sources
    )


def _report_messages(state: ResearchState, history: Sequence[dict]) -> List[dict]:
    recency = (
        "\nThis question is time-sensitive: prefer the most recent evidence, "
        "give dates where the sources give them, and say plainly when the "
        "sources may be out of date."
        if _RECENCY_RE.search(state.question)
        else ""
    )
    gaps = (
        "\n\nEVIDENCE GAPS the auditor could not close — state these honestly "
        "in a short 'What this report could not establish' section:\n"
        + "\n".join(f"- {m}" for m in state.missing[:6])
        if state.missing
        else ""
    )
    conflicts = (
        "\n\nCONTRADICTIONS between sources — surface them rather than "
        "silently picking one side:\n"
        + "\n".join(f"- {c}" for c in state.contradictions[:4])
        if state.contradictions
        else ""
    )
    system = (
        "You are writing a research report from numbered sources that were "
        "actually fetched and read. Rules:\n"
        "1. EVERY factual claim carries an inline citation like [1] or [3].\n"
        f"2. Only cite numbers 1 to {len(state.sources)} — these are the only "
        "sources that exist. Never invent a citation, a URL or a source.\n"
        "3. If the sources do not answer part of the question, say so "
        "explicitly instead of filling the gap from memory.\n"
        "4. Where sources disagree, present the disagreement and cite both.\n"
        "5. Structure the report with markdown headings, and lead with a "
        "short direct answer before the detail."
        + recency
    )
    user = (
        f"RESEARCH QUESTION:\n{state.question}\n\n"
        f"SUBQUESTIONS THE PLAN IDENTIFIED:\n"
        + "\n".join(f"- {s}" for s in state.subquestions)
        + gaps
        + conflicts
        + f"\n\nSOURCES:\n{_evidence_block(state.sources)}"
    )
    return [
        {"role": "system", "content": system},
        *recent_turns(history, 2),
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# Engine entry point
# ---------------------------------------------------------------------------


async def run_deep_research_engine(
    message: str,
    history: Sequence[dict],
    emit: Emit,
    effort: str = "think",
    conversation_id: str = "",
    user_id: Optional[int] = None,
) -> str:
    """Plan → search → read → assess → search again → report, with citations."""
    if _RUN_LOCK.locked():
        text = (
            "A research run is already in progress on this machine. Deep "
            "Research uses the whole search and GPU budget, so it runs one at "
            "a time — try again when the current one finishes, or ask with "
            "Web Search for a quick cited answer instead."
        )
        await emit("token", {"text": text})
        await emit("meta", {"route": "deep_research", "sources": []})
        return text

    async with _RUN_LOCK:
        return await _run(message, history, emit, effort, conversation_id, user_id)


async def _run(
    message: str,
    history: Sequence[dict],
    emit: Emit,
    effort: str,
    conversation_id: str,
    user_id: Optional[int],
) -> str:
    state = ResearchState(
        research_id=uuid.uuid4().hex,
        conversation_id=conversation_id,
        question=message,
    )
    run_row: Optional[int] = None
    try:
        run_row = await db.run_in_thread(
            db.create_research_run, conversation_id, user_id, message, state.research_id
        )
    except Exception:  # noqa: BLE001 — the report matters, the record does not
        log.warning("could not record the research run", exc_info=True)

    step_id = 0

    async def step(title: str) -> int:
        nonlocal step_id
        step_id += 1
        await emit("step", {"id": step_id, "title": title, "status": "running"})
        return step_id

    async def finish(sid: int, title: str, detail: str = "") -> None:
        await emit(
            "step",
            {"id": sid, "title": title, "status": "done", "detail": detail},
        )

    try:
        # --- Plan ------------------------------------------------------
        await emit("status", {"text": "Planning the research…"})
        sid = await step("Planning the research")
        state.subquestions, queries = await _plan(message, history, effort)
        await finish(
            sid,
            "Planned the research",
            "\n".join(f"- {s}" for s in state.subquestions) or "no subquestions",
        )

        # --- Iterate ---------------------------------------------------
        while True:
            state.iterations += 1
            label = (
                "Searching the web"
                if state.iterations == 1
                else f"Following up on gaps (round {state.iterations})"
            )
            await emit("status", {"text": f"{label} — {len(queries)} queries…"})
            sid = await step(label)
            added = await _gather(state, queries, effort, emit)
            await finish(
                sid,
                label,
                f"{len(added)} new source(s); {len(state.sources)} total",
            )

            if not state.budget_left():
                break
            if len(state.sources) < settings.deep_research_min_sources and added:
                # Too thin to judge — search the plan's remaining angles first.
                queries = [s for s in state.subquestions if s not in state.queries_run][
                    : settings.deep_research_max_queries_per_iteration
                ]
                if queries:
                    continue

            await emit("status", {"text": "Checking what is still missing…"})
            sid = await step("Analyzing evidence")
            verdict = await _assess(state, effort)
            state.missing = [m for m in (verdict.get("missing") or []) if isinstance(m, str)]
            state.contradictions = [
                c for c in (verdict.get("contradictions") or []) if isinstance(c, str)
            ]
            await finish(
                sid,
                "Analyzed evidence",
                ("Gaps: " + "; ".join(state.missing[:4])) if state.missing else "no gaps found",
            )

            if verdict.get("sufficient") or not state.budget_left():
                break
            followups = [
                q
                for q in (verdict.get("followup_queries") or [])
                if isinstance(q, str) and q.strip() and q not in state.queries_run
            ][: settings.deep_research_max_queries_per_iteration]
            if not followups:
                break
            queries = followups

        if not state.sources:
            text = (
                "I could not gather any readable sources for this question — "
                "the search provider returned nothing usable. Nothing was "
                "invented to fill the gap. Try rephrasing, or ask with Web "
                "Search for a single-pass answer."
            )
            await emit("token", {"text": text})
            await emit("meta", {"route": "deep_research", "sources": []})
            return text

        # --- Report ----------------------------------------------------
        await emit("status", {"text": f"Writing the report from {len(state.sources)} sources…"})
        sid = await step("Writing the report")
        _apply_char_tiers(
            [_Source(n=s.n, title=s.title, url=s.url, text=s.text) for s in state.sources]
        )
        # The report STREAMS. Buffering it to validate citations first made a
        # measured 6,349-character report land in one lump after ~40 s of
        # nothing but a thinking indicator — the worst-looking part of an
        # otherwise good run. Validation still happens below, on the text that
        # is returned and stored; a stray marker that survives on the client
        # is already invisible there, because the frontend strips every [n]
        # before rendering and draws the source list from meta.sources.
        parts: List[str] = []
        async with _LLM_SEM:
            async for kind, delta in llm.stream_chat_events(
                _report_messages(state, history),
                effort=llm.normalize_effort(effort),
                max_tokens=settings.deep_research_report_max_tokens,
            ):
                await emit(kind, {"text": delta})
                if kind == "token":
                    parts.append(delta)
        report = "".join(parts)

        # Citation integrity: a marker that resolves to nothing is removed.
        report, invalid = validate_citations(report, len(state.sources))
        if invalid:
            log.warning(
                "research report cited %d non-existent source(s): %s",
                len(invalid),
                sorted(set(invalid))[:10],
            )
        cited = sorted({int(n) for n in _CITE_RE.findall(report)})
        await finish(sid, "Wrote the report", f"{len(cited)} of {len(state.sources)} sources cited")

        sources_meta = [
            {"n": s.n, "title": s.title, "url": s.url, "domain": s.domain}
            for s in state.sources
        ]
        await emit(
            "meta",
            {
                "route": "deep_research",
                "sources": sources_meta,
                "research_run": {
                    "research_id": state.research_id,
                    "iterations": state.iterations,
                    "queries": state.queries_run,
                    "subquestions": state.subquestions,
                    "sources_found": len(state.sources),
                    "sources_cited": len(cited),
                    "missing": state.missing,
                    "contradictions": state.contradictions,
                    "elapsed_s": round(state.elapsed, 1),
                    "invalid_citations_removed": len(invalid),
                },
            },
        )
        if run_row is not None:
            _spawn(
                db.run_in_thread(
                    db.finish_research_run,
                    run_row,
                    "done",
                    state.iterations,
                    len(state.queries_run),
                    len(state.sources),
                    len(cited),
                    report,
                    sources_meta,
                    "",
                )
            )
        log.info(
            "deep research done: id=%s iterations=%d queries=%d sources=%d cited=%d "
            "invalid_citations=%d elapsed=%.1fs",
            state.research_id,
            state.iterations,
            len(state.queries_run),
            len(state.sources),
            len(cited),
            len(invalid),
            state.elapsed,
        )
        return report

    except asyncio.CancelledError:
        if run_row is not None:
            try:
                await asyncio.shield(
                    db.run_in_thread(
                        db.finish_research_run,
                        run_row,
                        "cancelled",
                        state.iterations,
                        len(state.queries_run),
                        len(state.sources),
                        0,
                        "",
                        [],
                        "cancelled mid-run",
                    )
                )
            except Exception:  # noqa: BLE001 — cancellation still wins
                log.warning("could not mark the cancelled research run", exc_info=True)
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("deep research failed", exc_info=True)
        if run_row is not None:
            try:
                await db.run_in_thread(
                    db.finish_research_run,
                    run_row,
                    "failed",
                    state.iterations,
                    len(state.queries_run),
                    len(state.sources),
                    0,
                    "",
                    [],
                    str(exc)[:300],
                )
            except Exception:  # noqa: BLE001
                pass
        text = (
            f"The research run failed ({exc}). Sources already gathered stay "
            "in the web store, so asking again is cheaper than the first time."
        )
        await emit("token", {"text": text})
        await emit("meta", {"route": "deep_research", "sources": []})
        return text
