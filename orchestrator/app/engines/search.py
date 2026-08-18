"""Web-search engine (Phase 1) — ChatGPT-style search + cited answer.

Pipeline: rewrite the question into 1-3 queries → run the configured provider →
fetch+extract the top sources through the SSRF-safe path → build a numbered
context block → stream a cited answer. Emits `status` events for live progress
and a final `meta` carrying the sources panel. Falls back to model knowledge
(with a visible notice) when search is unavailable.

Cache (query→sources, TTL) and a per-user rate limit keep it cheap and bounded.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from . import DIAGRAM_INSTRUCTION, recent_turns
from .. import llm
from ..config import settings
from ..core import extract, net
from ..search.base import SearchResult, SearchUnavailableError, get_provider

Emit = Callable[[str, dict], Awaitable[None]]

_MAX_QUERIES = 3
# Searches per request, by level. High is meant to be the one you reach for on
# a hard question, so it looks from more angles than Medium.
_QUERY_BUDGET = {"fast": 0, "low": 2, "medium": 3, "high": 6, "extra_high": 6}

# Sources actually READ, by level. This used to be one global
# settings.search_max_results for every level, applied as a head-slice AFTER
# all queries had run — so High issued 6 searches and then threw away
# everything past the first 10, which is why "high" never read more than
# "medium". High runs several web steps inside one agent plan, so the request
# total is a multiple of this.
_SOURCE_BUDGET = {"fast": 0, "low": 10, "medium": 15, "high": 60, "extra_high": 60}

# Pages allowed from any one site, by level. Without this, one SEO-heavy
# domain can supply a third of a large result set and the extra breadth buys
# nothing — 30 sources that are really 8 sites is not deep research.
_MAX_PER_DOMAIN = {"fast": 0, "low": 3, "medium": 3, "high": 4, "extra_high": 4}
# Floor below which the domain cap relaxes — a niche question where one site
# genuinely holds the answer should not be starved down to four pages.
_MIN_SOURCES = 8

# Characters of page text kept per source. A flat budget does not survive
# scale: 60 x 8000 would be 480k chars of prefill for ONE step. The top-ranked
# sources keep the full budget (so High is never shallower than Medium on the
# pages that matter most) and the long tail is kept short.
_TIER_A_SOURCES = 10
_TIER_B_CHARS = 2500

_FETCH_CONCURRENCY = 16
# Extraction is CPU-bound and trafilatura is not thread-safe — one worker.
_EXTRACT_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="extract")


def source_budget(effort: str) -> int:
    """How many sources this level reads per search."""
    return _SOURCE_BUDGET.get(effort, _SOURCE_BUDGET["medium"])


def _normalize_url(url: str) -> str:
    """Dedup key. Exact-url matching let the same page in three times over
    http/https, a trailing slash, and utm_* tracking parameters."""
    try:
        u = urlparse(url)
    except ValueError:
        return url
    host = (u.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (u.path or "/").rstrip("/") or "/"
    query = "&".join(
        sorted(
            p for p in (u.query or "").split("&")
            if p and not p.split("=")[0].lower().startswith(("utm_", "fbclid", "gclid"))
        )
    )
    return f"{host}{path}?{query}" if query else f"{host}{path}"


def _registrable_domain(url: str) -> str:
    """Rough eTLD+1 for the diversity cap ("a.b.example.co.uk" -> example.co.uk)."""
    host = (urlparse(url).hostname or "").lower().lstrip("www.")
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # Two-label public suffixes we actually meet (co.uk, com.au, co.in, ...).
    if len(parts[-2]) <= 3 and len(parts[-1]) <= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.S)

# Cheap "is this a web question?" heuristic for Auto mode, backed up by a model
# call. Fresh/current/lookup-y intent → search.
_FRESH_RE = re.compile(
    r"\b(latest|current|today|todays|this week|this month|this year|right now|"
    r"news|recent|20\d\d|price|stock|weather|release|version|who is|what is the|"
    r"how much|when did|when is|score|update)\b",
    re.I,
)


@dataclass
class _Source:
    n: int
    title: str
    url: str
    text: str

    @property
    def domain(self) -> str:
        return urlparse(self.url).hostname or self.url


# --------------------------------------------------------------------------
# small in-process cache + rate limiter (single orchestrator container)
# --------------------------------------------------------------------------
_cache: dict = {}


def _cache_get(key: str):
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    _cache.pop(key, None)
    return None


def _cache_put(key: str, value) -> None:
    _cache[key] = (time.monotonic() + settings.search_cache_ttl, value)


_rate: dict = {}


def rate_ok(user_key: str) -> bool:
    """Sliding-window per-user limit (searches per minute)."""
    now = time.monotonic()
    window = [t for t in _rate.get(user_key, []) if now - t < 60.0]
    if len(window) >= settings.search_rate_per_min:
        _rate[user_key] = window
        return False
    window.append(now)
    _rate[user_key] = window
    return True


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------
def query_budget(effort: str) -> int:
    """How many distinct searches this level may run.

    This is the concrete difference between Medium and High on a research
    question: High looks from more angles, so it reads more independent
    sources before answering.
    """
    return _QUERY_BUDGET.get(effort, _MAX_QUERIES)


async def rewrite_queries(
    message: str, history: Sequence[dict], effort: str = "medium"
) -> List[str]:
    """LLM → concise search queries (falls back to the raw message).

    Runs on the SMALL model: turning a question into search phrases is a
    mechanical rewrite, and spending the main model's reasoning pass on it made
    every search wait seconds before the first fetch even started.
    """
    cap = query_budget(effort)
    system = (
        f"Turn the user's request into 1 to {cap} concise web-search queries. "
        "Each query must look for something DIFFERENT — do not paraphrase the "
        "same search. Respond with ONLY a JSON array of strings, no prose."
    )
    msgs = [{"role": "system", "content": system}, *recent_turns(history, 4),
            {"role": "user", "content": message}]
    try:
        raw = await llm.router_chat_completion(msgs, temperature=0.0, max_tokens=200)
        m = _JSON_ARRAY_RE.search(raw or "")
        queries = json.loads(m.group(0)) if m else []
        queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    except Exception:
        queries = []
    return (queries or [message])[:cap]


async def should_search(message: str) -> bool:
    """Auto-mode decision: heuristic first, then a cheap model yes/no."""
    if _FRESH_RE.search(message):
        return True
    try:
        raw = await llm.router_chat_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "Does answering this need fresh, current, or web-lookup "
                        'information? Answer only "yes" or "no".'
                    ),
                },
                {"role": "user", "content": message},
            ],
            max_tokens=5,
        )
        return "yes" in (raw or "").lower()
    except Exception:
        return False


async def _emit_query(emit: Optional[Emit], query: str, results: List[SearchResult]) -> None:
    """Publish one search and what it found, for the live research panel.

    Sent as each query returns rather than at the end, so the panel fills in
    while the work happens instead of appearing all at once when it is over.
    """
    if emit is None:
        return
    await emit(
        "research",
        {
            "phase": "query",
            "query": query,
            "results": [
                {
                    "title": r.title,
                    "url": r.url,
                    "domain": _registrable_domain(r.url),
                }
                for r in results
            ],
        },
    )


async def _collect_results(
    queries: List[str],
    effort: str = "medium",
    emit: Optional[Emit] = None,
) -> List[SearchResult]:
    """Search every query and merge the results fairly.

    The old version concatenated results query by query and then head-sliced
    the whole list to 10. With more than one query that silently discarded the
    later ones: query 1 alone could fill the slice, so asking six different
    questions produced the same answer as asking one. The merge is now
    round-robin — rank 1 of every query, then rank 2 of every query — so each
    angle contributes before any angle contributes twice.
    """
    provider = get_provider()
    per_query: List[List[SearchResult]] = []
    for q in queries:
        cached = _cache_get(f"q:{provider.name}:{q}")
        if cached is not None:
            per_query.append(cached)
            await _emit_query(emit, q, cached)
            continue
        try:
            results = await provider.search(q, settings.search_max_results)
        except SearchUnavailableError:
            # One dead query must not sink the others — a suspended upstream
            # engine is normal, and the remaining angles still have answers.
            if not per_query and q is queries[-1]:
                raise
            continue
        _cache_put(f"q:{provider.name}:{q}", results)
        per_query.append(results)
        await _emit_query(emit, q, results)
    if not per_query:
        return []

    target = source_budget(effort)
    per_domain_cap = _MAX_PER_DOMAIN.get(effort, _MAX_PER_DOMAIN["medium"])
    seen: set = set()
    domains: dict = {}
    out: List[SearchResult] = []
    overflow: List[SearchResult] = []
    for rank in range(max(len(r) for r in per_query)):
        for results in per_query:
            if rank >= len(results):
                continue
            r = results[rank]
            key = _normalize_url(r.url)
            if key in seen:
                continue
            seen.add(key)
            dom = _registrable_domain(r.url)
            if domains.get(dom, 0) >= per_domain_cap:
                overflow.append(r)
                continue
            domains[dom] = domains.get(dom, 0) + 1
            out.append(r)
            if len(out) >= target:
                return out
    # The cap is strict while alternatives exist: once we have a usable number
    # of distinct sites, reading a 5th page from one of them adds far less than
    # it dilutes. Overflow only rescues a genuinely thin result set — the niche
    # question where one site really does hold most of the answer.
    if len(out) < _MIN_SOURCES:
        out.extend(overflow[: _MIN_SOURCES - len(out)])
    return out[:target]


async def _fetch_source(idx: int, r: SearchResult) -> Optional[_Source]:
    try:
        fetched = await net.safe_fetch(
            r.url,
            timeout_ms=settings.fetch_timeout_ms,
            max_bytes=settings.fetch_max_bytes,
            accept="text/html,application/pdf,text/plain",
        )
        # trafilatura/lxml (and pypdfium2 for PDFs) parse bodies up to 5 MB of
        # CPU-bound work. Inline, that stalls the event loop once per source.
        # It must NOT go on the default executor: trafilatura shares
        # module-level compiled lxml XPath objects that are not thread-safe,
        # and parsing two pages at once can abort the interpreter. A dedicated
        # single-worker pool keeps the loop free AND keeps extraction serial.
        loop = asyncio.get_running_loop()
        ext = await loop.run_in_executor(
            _EXTRACT_POOL,
            extract.extract_readable,
            fetched.content_type,
            fetched.body,
            fetched.url,
        )
        text = extract.truncate_chars(ext.text, settings.search_source_char_budget)
        if not text.strip():
            text = r.snippet
        return _Source(n=idx, title=ext.title or r.title, url=r.url, text=text)
    except Exception:
        # Any failure (SSRF block, timeout, unsupported) → fall back to the
        # provider snippet so the source is still citable.
        if r.snippet.strip():
            return _Source(n=idx, title=r.title, url=r.url, text=r.snippet)
        return None


async def _fetch_sources(results: List[SearchResult]) -> List[_Source]:
    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def guarded(i: int, r: SearchResult):
        async with sem:
            return await _fetch_source(i + 1, r)

    fetched = await asyncio.gather(*(guarded(i, r) for i, r in enumerate(results)))
    sources = [s for s in fetched if s is not None]
    # renumber contiguously after drops
    for new_n, s in enumerate(sources, start=1):
        s.n = new_n
    return sources


def _apply_char_tiers(sources: List[_Source]) -> List[_Source]:
    """Trim the long tail so a big result set stays a sane prompt.

    Search rank is the only quality signal available before reading, so the
    top-ranked pages keep the full per-source budget — meaning High is never
    shallower than Medium on the pages most likely to matter — and everything
    after them is cut to a summary-sized excerpt.
    """
    for s in sources:
        if s.n > _TIER_A_SOURCES:
            s.text = extract.truncate_chars(s.text, _TIER_B_CHARS)
    return sources


def _context_block(sources: List[_Source]) -> str:
    blocks = [
        f"[{s.n}] {s.title} ({s.url})\n{s.text}" for s in sources
    ]
    return "\n\n".join(blocks)


def _answer_messages(
    message: str, sources: List[_Source], history: Sequence[dict]
) -> List[dict]:
    system = (
        "You answer using the numbered web sources provided. Cite the sources "
        "you rely on inline with bracketed numbers like [1] or [2]. Prefer the "
        "most recent and authoritative sources; if they conflict or don't cover "
        "the question, say so. Do not invent citations — never cite a number "
        "that is not in the list above.\n"
        "Many sources are given because breadth is the point: draw on the FULL "
        "set rather than the first few. Where several sources agree, say so and "
        "cite them together. Where they DISAGREE, surface the disagreement "
        "explicitly instead of silently picking one. Call out anything only a "
        "single source claims. A long source list is not permission to write a "
        "longer answer — it is material for a better-supported one."
    )
    user = f"Web sources:\n{_context_block(sources)}\n\nQuestion: {message}"
    return [{"role": "system", "content": system + DIAGRAM_INSTRUCTION}, *recent_turns(history, 4),
            {"role": "user", "content": user}]


async def _fallback(message: str, history: Sequence[dict], emit: Emit, note: str) -> str:
    await emit("status", {"text": note})
    parts: List[str] = []
    msgs = [
        {"role": "system", "content": "You are a helpful assistant. Web search is "
         "unavailable, so answer from your own knowledge and say so if the answer "
         "may be out of date."},
        *recent_turns(history, 6),
        {"role": "user", "content": message},
    ]
    async for kind, delta in llm.stream_chat_events(msgs, max_tokens=8000):
        await emit(kind, {"text": delta})
        if kind == "token":
            parts.append(delta)
    await emit("meta", {"route": "search", "search_unavailable": True})
    return "".join(parts)


async def research_step(
    question: str,
    history: Sequence[dict] = (),
    effort: str = "medium",
    emit: Optional[Emit] = None,
) -> Tuple[str, List[dict]]:
    """Search → read → answer for ONE agent step. → (answer, sources).

    Same pipeline as run_search_engine but it does not stream the answer: inside
    a plan the prose belongs to the synthesis, and the sources are merged into
    the final citation list rather than published per step. It DOES report its
    searches through `emit` when given one, so a multi-step plan's research
    shows up in the panel as one combined effort.

    Returns ("", []) when search is unavailable or finds nothing readable, so
    the caller can fall back to answering from model knowledge instead of
    failing the whole step.
    """
    try:
        queries = await rewrite_queries(question, history, effort)
        results = await _collect_results(queries, effort, emit)
    except SearchUnavailableError:
        return "", []
    if not results:
        return "", []
    if emit is not None:
        await emit("research", {"phase": "reading", "count": len(results)})
    sources = _apply_char_tiers(await _fetch_sources(results))
    if not sources:
        return "", []
    answer = await llm.chat_completion(
        _answer_messages(question, sources, history), temperature=0.2, max_tokens=5000
    )
    if emit is not None:
        await emit("research", {"phase": "read", "count": len(sources)})
    return answer, [
        {"n": s.n, "title": s.title, "url": s.url, "domain": s.domain} for s in sources
    ]


async def run_search_engine(
    message: str, history: Sequence[dict], emit: Emit, effort: str = "medium"
) -> str:
    """Full search pipeline with status events, cited streaming, and fallback."""
    await emit("status", {"text": "Searching the web…"})
    try:
        queries = await rewrite_queries(message, history, effort)
        results = await _collect_results(queries, effort, emit)
    except SearchUnavailableError:
        return await _fallback(
            message, history, emit, "Web search unavailable — answering from model knowledge."
        )
    if not results:
        return await _fallback(
            message, history, emit, "No web results found — answering from model knowledge."
        )

    await emit("status", {"text": f"Reading {len(results)} sources…"})
    await emit("research", {"phase": "reading", "count": len(results)})
    sources = _apply_char_tiers(await _fetch_sources(results))
    if not sources:
        return await _fallback(
            message, history, emit, "Couldn't read the sources — answering from model knowledge."
        )

    await emit("research", {"phase": "read", "count": len(sources)})
    parts: List[str] = []
    async for kind, delta in llm.stream_chat_events(
        _answer_messages(message, sources, history), max_tokens=12000
    ):
        await emit(kind, {"text": delta})
        if kind == "token":
            parts.append(delta)

    await emit(
        "meta",
        {
            "route": "search",
            "sources": [
                {"n": s.n, "title": s.title, "url": s.url, "domain": s.domain}
                for s in sources
            ],
        },
    )
    return "".join(parts)
