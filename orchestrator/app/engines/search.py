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
import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from . import DIAGRAM_INSTRUCTION, conversation_turns, recent_turns
from .. import llm
from ..config import settings
from .. import db, web_index
from ..core import extract, net, provenance
from ..freshness import Verdict
from ..search.base import SearchResult, SearchUnavailableError, get_provider

Emit = Callable[[str, dict], Awaitable[None]]

log = logging.getLogger(__name__)

_MAX_QUERIES = 3
# Searches per request, by level. High is meant to be the one you reach for on
# a hard question, so it looks from more angles than Medium.
# fast used to be 0/0 — with the web pill ON that meant NO search at all and a
# "No web results found" fallback, which is not what forcing search means. One
# query and a small read set keeps Fast fast while actually searching
# (2026-08-30).
_QUERY_BUDGET = {"fast": 1, "think": 3, "max": 6}

# Sources actually READ, by level. This used to be one global
# settings.search_max_results for every level, applied as a head-slice AFTER
# all queries had run — so High issued 6 searches and then threw away
# everything past the first 10, which is why "high" never read more than
# "medium". High runs several web steps inside one agent plan, so the request
# total is a multiple of this.
_SOURCE_BUDGET = {"fast": 8, "think": 15, "max": 60}

# Pages allowed from any one site, by level. Without this, one SEO-heavy
# domain can supply a third of a large result set and the extra breadth buys
# nothing — 30 sources that are really 8 sites is not deep research.
_MAX_PER_DOMAIN = {"fast": 2, "think": 3, "max": 4}
# Floor below which the domain cap relaxes — a niche question where one site
# genuinely holds the answer should not be starved down to four pages.
_MIN_SOURCES = 8

# Characters of page text kept per source. A flat budget does not survive
# scale: 60 x 8000 would be 480k chars of prefill for ONE step. The top-ranked
# sources keep the full budget (so High is never shallower than Medium on the
# pages that matter most) and the long tail is kept short.
# WHY THE RERANKER ONLY REORDERS THE FETCH BUDGET, AND NOT A WIDER POOL.
# It looks like a bug that _collect_results truncates to the budget before
# _rerank_results sees anything — the cross-encoder can only reorder the
# handful engine rank already chose. It was tried (2026-08-30): gather 3x the
# budget, rerank down. It measurably made results WORSE on every query tested.
#
#   "vLLM continuous batching throughput"
#     narrow -> anyscale.com, microsoft.com, arpitbhayani.me
#     wide   -> dasroot.net, rajatpandit.com, heeviz.com
#   "Qwen3 open source model release"
#     narrow -> github.com, huggingface.co, openlm.ai
#     wide   -> 2coffee.dev, daconta.us, orcarouter.ai
#
# The reason is that the two signals measure different things. Engine rank is
# an AUTHORITY prior — Bing and Google already know anyscale.com outranks a
# personal blog on this topic. The reranker scores TOPICAL match on title +
# snippet, and knows nothing about authority; a keyword-dense blog post beats
# an authoritative page on that measure. Widening the pool throws the
# authority prior away and ranks purely on topicality. Keep both: engine rank
# selects, the reranker reorders within that selection.

_TIER_A_SOURCES = 10
_TIER_B_CHARS = 2500

_FETCH_CONCURRENCY = 16
# Extraction is CPU-bound and trafilatura is not thread-safe — one worker.
_EXTRACT_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="extract")


def source_budget(effort: str) -> int:
    """How many sources this level reads per search."""
    return _SOURCE_BUDGET.get(llm.normalize_effort(effort), _SOURCE_BUDGET["think"])


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
    # removeprefix, NOT lstrip: lstrip("www.") strips CHARACTERS, so
    # "web.example.com" became "eb.example.com" and the diversity cap grouped
    # unrelated sites (found by review, 2026-08-30).
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
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
    # --- provenance (2026-09-03). Every field defaults, so the four-field
    # constructor the tests and the agent use is unchanged. ---
    #: Links the page pointed at (harvested in the same parse), so a caller
    #: that reads the page can follow them without a second fetch.
    links: List[str] = field(default_factory=list)
    published_at: Optional[datetime] = None
    modified_at: Optional[datetime] = None
    fetched_at: Optional[datetime] = None
    content_hash: str = ""
    #: core/provenance.source_type — official / docs / news / community …
    source_type: str = ""
    #: web_memory's 0-100 authority prior for the domain.
    authority: int = 0
    #: True when served from the warm store rather than the network.
    from_store: bool = False

    @property
    def domain(self) -> str:
        return urlparse(self.url).hostname or self.url


def _call_extract(content_type: str, body: bytes, url: str, headers: Optional[dict]):
    """extract_readable_and_links, with or without the headers argument.

    Tests (and any operator's own extractor) may substitute a three-argument
    callable; the real one takes the response headers so a page with no date
    of its own can use Last-Modified. Passing what the callee accepts keeps
    both working without a try/except that would mask real TypeErrors."""
    fn = extract.extract_readable_and_links
    code = getattr(fn, "__code__", None)
    accepts_headers = bool(code) and (
        code.co_argcount >= 4 or "headers" in code.co_varnames[: code.co_argcount + 4]
    )
    if accepts_headers:
        return fn(content_type, body, url, headers)
    return fn(content_type, body, url)


def _provenance_of(ext: extract.Extracted, url: str, content_type: str, headers: Optional[dict]) -> dict:
    """The metadata the store and the ranking layers want for one page."""
    from ..web_memory import authority_of

    kind = provenance.source_type(url, content_type, getattr(ext, "sitename", "") or "")
    return {
        "published_at": provenance.parse_date(getattr(ext, "published_at", None)),
        "modified_at": provenance.parse_date(getattr(ext, "modified_at", None)),
        "source_type": kind,
        "authority": authority_of(url),
        "etag": (headers or {}).get("etag", "") or "",
        "last_modified": (headers or {}).get("last-modified", "") or "",
    }


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
    return _QUERY_BUDGET.get(llm.normalize_effort(effort), _MAX_QUERIES)


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
    # conversation_turns, NOT recent_turns. main.py pins the user's saved
    # facts, the cross-chat recall block and the excerpts of pages/documents
    # shared in this chat to `history` as system messages; recent_turns keeps
    # them because the answer prompt needs them. Here they would be rewritten
    # into search phrases and sent to SearXNG and the engines behind it — a
    # private term sheet becoming a web query. Only what was said in this
    # conversation is context for a query (security review 2026-09-03).
    msgs = [{"role": "system", "content": system}, *conversation_turns(history, 4),
            {"role": "user", "content": message}]
    try:
        raw = await llm.router_chat_completion(msgs, temperature=0.0, max_tokens=200)
        m = _JSON_ARRAY_RE.search(raw or "")
        queries = json.loads(m.group(0)) if m else []
        queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    except Exception:
        queries = []
    return (queries or [message])[:cap]


async def should_search(message: str, history: Sequence[dict] = ()) -> bool:
    """Auto-mode decision: heuristic first, then a cheap model yes/no.

    `history` is optional: a couple of real turns let the yes/no read a
    follow-up ("and is that still true?") that carries no signal on its own.
    Only conversation turns go in, never the pinned system blocks (saved
    facts, recall, shared-page and document excerpts) — the decision is about
    what the user ASKED, and no prompt on the search path may carry private
    context: this one is the model's view of the outbound question and sits
    one refactor away from being logged or forwarded alongside the queries.
    With no history the prompt is exactly the two messages it always was.
    """
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
                *conversation_turns(history, 2),
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
    categories: str = "",
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

    # The queries are independent lookups against an engine that answers each
    # in ~0.7-2.2 s (measured), and they used to run one after another — think
    # paid ~2-5 s and max ~5-13 s of pure serialisation. gather() makes the
    # whole phase cost the slowest single query (2026-08-30).
    async def _one(q: str):
        # The category is part of the key: the same query routed to
        # `science` returns a different result set from the general pool, and
        # a shared key silently served whichever ran first (review, 2026-08-30).
        cached = _cache_get(f"q:{provider.name}:{categories}:{q}")
        if cached is not None:
            return q, cached, None
        try:
            # The category hint is passed ONLY when there is one, so a
            # provider written against the two-argument signature (the
            # interface before 2026-08-30, including any operator's own) keeps
            # working untouched on the ordinary search path.
            results = await (
                provider.search(q, settings.search_max_results, categories)
                if categories
                else provider.search(q, settings.search_max_results)
            )
        except SearchUnavailableError as exc:
            return q, None, exc
        _cache_put(f"q:{provider.name}:{categories}:{q}", results)
        return q, results, None

    gathered = await asyncio.gather(*(_one(q) for q in queries))
    per_query: List[List[SearchResult]] = []
    for q, results, exc in gathered:
        if results is None:
            continue
        per_query.append(results)
        await _emit_query(emit, q, results)
    if not per_query:
        # One dead upstream engine is normal; ALL dead is unavailability.
        errors = [exc for _q, _r, exc in gathered if exc is not None]
        if errors:
            raise errors[-1]
        return []

    target = source_budget(effort)
    per_domain_cap = _MAX_PER_DOMAIN.get(llm.normalize_effort(effort), _MAX_PER_DOMAIN["think"])
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


async def _rerank_results(
    message: str, results: List[SearchResult], target: int
) -> List[SearchResult]:
    """Order candidate results by RELEVANCE, not engine rank.

    Engine rank was the only pre-read quality signal, and it measurably fails:
    in both probe runs the rank-1 source for "latest vLLM release" was an
    anime video page. Scoring title+snippet pairs costs ~50 ms for 40
    candidates (measured 2026-08-30, after the reranker was fixed to load as
    a cross-encoder rather than an embedding model). Any failure returns the
    input order — reranking is an upgrade, never a gate.

    `target` is the number of results to KEEP. Callers hand in a candidate
    pool several times larger than the fetch budget, so this is where "the
    best 8 of 120" happens; before 2026-08-30 the pool was truncated to the
    budget upstream and this function could only reorder what it was given.
    """
    keep = target if target > 0 else len(results)
    if len(results) <= 2:
        return results[:keep]
    # The shared, TEMPLATED client (app/rerank.py, ADR-0001 D4). Raw
    # title+snippet pairs through /score measurably ranked a careers page
    # above the passage naming the office holder; the model's own prompt
    # format separates them by three orders of magnitude.
    from .. import rerank

    try:
        scores = await rerank.score(
            message, [f"{r.title}\n{r.snippet}"[:1000] for r in results]
        )
    except rerank.RerankUnavailable:
        return results[:keep]
    order = sorted(range(len(results)), key=lambda i: scores[i], reverse=True)
    return [results[i] for i in order][:keep]



#: Pages served from the store during THIS request, for the research panel
#: and for tests: {url_key: fetched_at}.
#: Strong references to write-behind tasks. asyncio keeps only weak refs to
#: tasks, so an unreferenced create_task can be garbage-collected mid-flight,
#: and an unobserved exception dies in silence — the review found a page with
#: a NUL byte failing to store on EVERY search with no log line at all.
_BACKGROUND_TASKS: set = set()


def _spawn(coro) -> None:
    """create_task with a held reference and a logged (never raised) failure."""
    task = asyncio.get_running_loop().create_task(coro)
    _BACKGROUND_TASKS.add(task)

    def _done(t) -> None:
        _BACKGROUND_TASKS.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.warning("background web-memory task failed", exc_info=t.exception())

    task.add_done_callback(_done)


def _page_ttl(message: str, verdict: Optional[Verdict] = None) -> int:
    """How old a stored page may be and still count as fresh for this ask.

    With a freshness verdict (app.freshness — the classification every other
    stage of the pipeline already runs, ADR-0001 D2/D6) the decision is the
    verdict's: a VOLATILE one ("latest release", "current price", anything
    REALTIME) gets the short TTL, everything else the long one. Re-matching
    _FRESH_RE here disagreed with that verdict at the edges — "who is",
    "score", a bare "2026" and "what is the" all trip the regex — so an
    office-holder question (RECENT, its answer stable for months) threw away
    a two-hour-old copy of the page that answered it and paid a network
    fetch with a 3 s connect + 8 s read ceiling for the same text.

    Without a verdict the regex fallback stands unchanged, so a caller that
    has not classified the question (deep research's fetch path, the
    crawler) gets exactly the TTL it always did.
    """
    if verdict is not None:
        if verdict.volatile:
            return settings.web_page_fresh_ttl_s
        return settings.web_page_ttl_s
    if _FRESH_RE.search(message or ""):
        return settings.web_page_fresh_ttl_s
    return settings.web_page_ttl_s


async def _stored_pages(
    results: List[SearchResult], message: str, verdict: Optional[Verdict] = None
) -> dict:
    """{url_key: stored page} for results whose stored copy is still fresh.

    This is the speed dividend of the V8 store: a warm hit skips a network
    fetch with a 3 s connect + 8 s read ceiling. Failures return {} — the
    store is an accelerator, never a gate.

    `verdict`, when the caller has one, decides "fresh" (see _page_ttl);
    without it the wording of `message` does, as it always has.
    """
    if not settings.web_memory_enabled:
        return {}
    try:
        keys = [_normalize_url(r.url) for r in results]
        rows = await db.run_in_thread(db.get_web_pages, keys)
        ttl = _page_ttl(message, verdict)
        now = time.time()
        fresh: dict = {}
        for row in rows:
            fetched_at = row.get("fetched_at")
            age = now - fetched_at.timestamp() if fetched_at else ttl + 1
            if age <= ttl and (row.get("text") or "").strip():
                fresh[row["url_key"]] = row
        return fresh
    except Exception:  # noqa: BLE001
        return {}


def _store_page(
    r: SearchResult,
    canonical_url: str,
    title: str,
    text: str,
    content_type: str,
    links: Optional[List[str]] = None,
    meta: Optional[dict] = None,
    user_id: Optional[int] = None,
    conversation_id: str = "",
) -> None:
    """Persist one fetched page (blocking; called via run_in_thread).

    A page found by a search is origin 'search' (the default trust class);
    who searched, and in which conversation, is recorded as its introducer
    (V16) so every row in the shared corpus is attributable."""
    db.upsert_web_page(
        url_key=_normalize_url(r.url),
        url=r.url,
        canonical_url=canonical_url or "",
        title=title or r.title,
        text=text or "",
        content_type=content_type or "",
        fetch_status=200 if text else 0,
        content_hash=hashlib.sha256((text or "").encode("utf-8")).hexdigest(),
        links=links or [],
        origin="search",
        introduced_by_user_id=user_id,
        introduced_in_conversation_id=conversation_id or None,
        **(meta or {}),
    )


async def _fetch_source(
    idx: int,
    r: SearchResult,
    stored: Optional[dict] = None,
    *,
    user_id: Optional[int] = None,
    conversation_id: str = "",
) -> Optional[_Source]:
    # Warm path: a fresh stored copy of this exact URL answers without the
    # network. The FULL stored text is re-truncated to the prompt budget the
    # same way a live fetch would be.
    if stored:
        hit = stored.get(_normalize_url(r.url))
        if hit:
            text = extract.truncate_chars(
                hit["text"], settings.search_source_char_budget
            )
            return _Source(
                n=idx,
                title=hit["title"] or r.title,
                url=r.url,
                text=text,
                links=list(hit.get("links") or [])[:500],
                published_at=hit.get("published_at"),
                modified_at=hit.get("modified_at"),
                fetched_at=hit.get("fetched_at"),
                content_hash=hit.get("content_hash") or "",
                source_type=hit.get("source_type") or provenance.source_type(r.url),
                authority=int(hit.get("authority") or 0),
                from_store=True,
            )
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
        # extract_readable_and_links, not extract_readable: same parse cost,
        # and the harvested links ride into the store so a later crawl or the
        # post-search expansion can walk from a page served fresh-from-store
        # (the review found that path silently linkless).
        headers = getattr(fetched, "headers", None) or {}
        ext, page_links = await loop.run_in_executor(
            _EXTRACT_POOL,
            _call_extract,
            fetched.content_type,
            fetched.body,
            fetched.url,
            headers,
        )
        meta = _provenance_of(ext, fetched.url, fetched.content_type, headers)
        digest = hashlib.sha256((ext.text or "").encode("utf-8")).hexdigest()
        # Persist the FULL extracted text BEFORE the prompt truncation — the
        # store is the whole point (V8): the same URL next time costs a DB
        # read, and the vector index chunks from here. Fire-and-forget so the
        # answer never waits on PostgreSQL.
        if settings.web_memory_enabled and ext.text.strip():
            full_text, canon, ctype, title0 = ext.text, fetched.url, fetched.content_type, ext.title
            _spawn(
                db.run_in_thread(
                    _store_page, r, canon, title0, full_text, ctype, page_links, meta,
                    user_id, conversation_id,
                )
            )
        text = extract.truncate_chars(ext.text, settings.search_source_char_budget)
        if not text.strip():
            text = r.snippet
        return _Source(
            n=idx,
            title=ext.title or r.title,
            url=r.url,
            text=text,
            links=list(page_links or [])[:500],
            published_at=meta["published_at"],
            modified_at=meta["modified_at"],
            fetched_at=datetime.now(timezone.utc),
            content_hash=digest,
            source_type=meta["source_type"],
            authority=int(meta["authority"] or 0),
        )
    except Exception:
        # Any failure (SSRF block, timeout, unsupported) → fall back to the
        # provider snippet so the source is still citable.
        if r.snippet.strip():
            return _Source(
                n=idx, title=r.title, url=r.url, text=r.snippet,
                source_type=provenance.source_type(r.url),
            )
        return None


async def _fetch_sources(
    results: List[SearchResult],
    message: str = "",
    *,
    user_id: Optional[int] = None,
    conversation_id: str = "",
    verdict: Optional[Verdict] = None,
) -> List[_Source]:
    stored = await _stored_pages(results, message, verdict=verdict)
    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def guarded(i: int, r: SearchResult):
        async with sem:
            return await _fetch_source(
                i + 1, r, stored, user_id=user_id, conversation_id=conversation_id
            )

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


_MEMORY_HEADER_NOTE = (
    " (from a page read on {date} — verify against newer sources if this "
    "conflicts with them)"
)


async def _memory_sources(
    message: str, sources: List[_Source], budget: int = 3
) -> List[_Source]:
    """Stored passages that ANSWER the question, appended as dated sources.

    The web RAG never REPLACES live results — a cached paragraph about
    "latest release" is exactly how a bot confidently reports last month.
    It adds what the live set happens not to cover, each block dated so the
    model can weigh it.

    Through the same pipeline as every other route (ADR-0001 D2): hybrid
    candidates, the cross-encoder's answer probability, content-date
    supersession for a live fact. Until 2026-09-03 this ranked by vector
    distance alone and SKIPPED the store entirely for any "fresh-intent"
    wording — "who is …" included — so Think answered from live results
    only, at 15-19 s, while the store held the page that answered. Now a
    fresh-intent question just uses the freshness verdict's own supersession
    and a stored passage must clear the relevance bar to appear at all.
    """
    if not settings.web_memory_enabled:
        return sources
    try:
        from ..freshness import classify_offline
        from .. import web_memory

        verdict = classify_offline(message, now_year=datetime.now(timezone.utc).year)
        result = await web_memory.retrieve(
            message, level=verdict.requirement, top_k=budget * 2, verdict=verdict
        )
    except Exception:  # noqa: BLE001
        return sources
    have = {_normalize_url(s.url) for s in sources}
    added = 0
    per_domain: dict = {}
    for ev in result.evidence:
        if added >= budget:
            break
        if not ev.relevant:
            continue
        key = _normalize_url(ev.url)
        if not key or key in have:
            continue
        # One crawled site must not own every memory slot (measured: after a
        # site crawl the global top-k was all one domain). 2 of 3 max.
        dom = _registrable_domain(ev.url)
        if per_domain.get(dom, 0) >= max(1, budget - 1):
            continue
        text = (ev.text or "").strip()
        if not text:
            continue
        per_domain[dom] = per_domain.get(dom, 0) + 1
        have.add(key)
        added += 1
        read = ev.fetched_at.date().isoformat() if ev.fetched_at else "an earlier day"
        stamp = ""
        if ev.content_date is not None:
            stamp = f"published {ev.content_date.date().isoformat()}, "
        sources.append(
            _Source(
                n=len(sources) + 1,
                title=(ev.title or ev.url)
                + _MEMORY_HEADER_NOTE.format(date=f"{stamp}read {read}"),
                url=ev.url,
                text=extract.truncate_chars(text, _TIER_B_CHARS),
                published_at=ev.published_at,
                modified_at=ev.modified_at,
                fetched_at=ev.fetched_at,
                source_type=ev.source_type,
                authority=int(ev.authority or 0),
                from_store=True,
            )
        )
    return sources


def _log_search_background(
    message: str,
    queries: List[str],
    results: List[SearchResult],
    effort: str,
    user_id: Optional[int] = None,
    conversation_id: str = "",
) -> None:
    """Persist the search log + returned links; then nudge the indexer."""
    rows = []
    for rank, r in enumerate(results, start=1):
        rows.append(
            {
                "query": queries[0] if queries else "",
                "rank": rank,
                "url": r.url,
                "url_key": _normalize_url(r.url),
                "title": r.title,
                "snippet": r.snippet,
            }
        )
    db.log_web_search(
        # The ids come from the dispatcher. They are what make the V8 log a
        # per-conversation history at all — and what lets delete_conversation
        # actually delete these rows (hardcoded None/"" left every deleted
        # conversation's search text stored forever; review round 2026-08-30).
        user_id=user_id,
        conversation_id=conversation_id,
        message=message,
        queries=queries,
        provider=get_provider().name,
        effort=effort,
        results=rows,
    )


async def _persist_and_index(
    message: str,
    queries: List[str],
    results: List[SearchResult],
    effort: str,
    user_id: Optional[int] = None,
    conversation_id: str = "",
) -> None:
    """Background: write the search log, then index any new/changed pages.

    Runs AFTER the answer has started streaming; nothing here can slow the
    user down, and any failure only costs memory of this one search.
    """
    try:
        await db.run_in_thread(
            _log_search_background,
            message,
            queries,
            results,
            effort,
            user_id,
            conversation_id,
        )
    except Exception:  # noqa: BLE001
        pass
    # Give the write-behind page stores a moment to land, then index.
    await asyncio.sleep(2.0)
    await web_index.index_pending()
    # Owner idea (2026-08-30): with the answer already streamed, quietly
    # follow a few in-site links from the pages just read so the NEXT related
    # question hits warm content. Tightly capped; robots respected.
    try:
        from .crawl import expand_search_domains

        await expand_search_domains([r.url for r in results])
    except Exception:  # noqa: BLE001 — enrichment, never surfaces
        pass



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
    user_id: Optional[int] = None,
    conversation_id: str = "",
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
    results = await _rerank_results(question, results, len(results))
    if emit is not None:
        await emit("research", {"phase": "reading", "count": len(results)})
    sources = _apply_char_tiers(
        await _fetch_sources(
            results, question, user_id=user_id, conversation_id=conversation_id
        )
    )
    if not sources:
        return "", []
    sources = await _memory_sources(question, sources)
    _spawn(
        _persist_and_index(
            question, queries, results, effort, user_id, conversation_id
        )
    )
    answer = await llm.chat_completion(
        _answer_messages(question, sources, history), temperature=0.2, max_tokens=5000
    )
    if emit is not None:
        await emit("research", {"phase": "read", "count": len(sources)})
    return answer, [
        {"n": s.n, "title": s.title, "url": s.url, "domain": s.domain} for s in sources
    ]


async def run_search_engine(
    message: str,
    history: Sequence[dict],
    emit: Emit,
    effort: str = "medium",
    user_id: Optional[int] = None,
    conversation_id: str = "",
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

    # Relevance-ordered selection: score the candidate snippets with the
    # reranker BEFORE spending fetch time on them, so "best 15 of the pool"
    # replaces "first 15 by engine rank" (which put an anime page at [1]).
    results = await _rerank_results(message, results, len(results))

    await emit("status", {"text": f"Reading {len(results)} sources…"})
    await emit("research", {"phase": "reading", "count": len(results)})
    sources = _apply_char_tiers(
        await _fetch_sources(
            results, message, user_id=user_id, conversation_id=conversation_id
        )
    )
    if not sources:
        return await _fallback(
            message, history, emit, "Couldn't read the sources — answering from model knowledge."
        )
    # Paragraphs from pages read in EARLIER searches, dated, after the live set.
    sources = await _memory_sources(message, sources)

    await emit("research", {"phase": "read", "count": len(sources)})

    # Remember this search — log, pages, vectors — behind the answer.
    _spawn(
        _persist_and_index(
            message, queries, results, effort, user_id, conversation_id
        )
    )

    parts: List[str] = []
    async for kind, delta in llm.stream_chat_events(
        _answer_messages(message, sources, history),
        # The picker reaches the answer now. This call ran at the default
        # "medium" (= thinking ON) whatever the user chose: measured, the
        # thinking pass was 77-82% of search wall-clock — 851 reasoning
        # tokens ahead of a 32-token answer (2026-08-30).
        effort=llm.normalize_effort(effort),
        max_tokens=12000,
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


async def fetch_for_freshness(
    question: str,
    *,
    max_queries: int = 1,
    max_sources: int = 2,
    user_id: Optional[int] = None,
    conversation_id: str = "",
) -> int:
    """A deliberately tiny search+read, for the Fast-mode freshness fallback.

    Reuses THIS module's provider fan-out, SSRF-guarded fetch, extraction and
    page store — nothing here opens a socket of its own, so every protection
    that guards an ordinary search guards this too, and pages land in the same
    global corpus. Writing through the same store is what lets the NEXT
    conversation answer the question locally with no network at all.

    Returns how many sources were actually read. Never raises: the caller
    falls back to stale-but-labelled evidence when this returns 0.

    NOT a small `run_search_engine`. There is no query rewrite (one provider
    call on the user's own words), no rerank, and no answer generation — this
    exists to put two fresh pages on disk, not to compose a response.

    `user_id` / `conversation_id` attribute the lookup like any other search
    (V16, ADR-0001 D7): the search log is what ties the pages this call
    introduces to the conversation that asked — its result rows carry every
    url_key read here — so they are part of that chat's history and go when
    it is deleted, instead of an anonymous row nobody can purge. Until
    2026-09-03 the ids were hardcoded None/"" on this path.
    """
    if not settings.search_enabled:
        return 0
    try:
        results = await _collect_results([question], effort="fast")
    except Exception:  # noqa: BLE001 — no provider, no freshness; not fatal
        return 0
    if not results:
        return 0

    # One page per registrable domain: two copies of the same syndicated story
    # corroborate nothing, and the whole budget here is two reads.
    seen_domains: set = set()
    picked: List[SearchResult] = []
    for r in results:
        dom = _registrable_domain(r.url)
        if dom in seen_domains:
            continue
        seen_domains.add(dom)
        picked.append(r)
        if len(picked) >= max(1, int(max_sources)):
            break

    try:
        sources = await _fetch_sources(
            picked, question, user_id=user_id, conversation_id=conversation_id
        )
    except Exception:  # noqa: BLE001 — a failed read is a miss, not an error
        return 0

    # Index synchronously HERE, unlike the streaming path: the caller is about
    # to read the corpus back, so a write-behind index would mean answering
    # from evidence that has not landed yet.
    try:
        await web_index.index_pending()
    except Exception:  # noqa: BLE001 — evidence is stored; indexing retries
        pass

    # The pages themselves entered the store through the same _fetch_sources
    # a full search uses, so their origin stays 'search' — a page read on this
    # path earns no separate trust class and needs none.
    _spawn(
        db.run_in_thread(
            _log_search_background,
            question,
            [question],
            picked,
            "fast",
            user_id,
            conversation_id,
        )
    )
    return len(sources)


async def refetch_page(url: str, *, previous_hash: str = "") -> Optional[dict]:
    """Re-read one already-known page through the ordinary safe path.

    The refresh worker's only way to fetch. Everything here is the SAME code an
    ordinary search source goes through — net.safe_fetch (SSRF guards, redirect
    re-validation, size cap, timeout) and extraction on `_EXTRACT_POOL`, the
    single-worker executor that exists because trafilatura shares module-level
    lxml XPath objects that are not thread-safe and will abort the interpreter
    if two pages parse at once.

    Returns {'changed', 'title', 'hash'} or None when the page could not be
    read. Storing happens here so the caller cannot forget the content-hash
    rule that resets the vector watermark.
    """
    try:
        fetched = await net.safe_fetch(
            url,
            timeout_ms=settings.fetch_timeout_ms,
            max_bytes=settings.fetch_max_bytes,
            accept="text/html,application/pdf,text/plain",
        )
        loop = asyncio.get_running_loop()
        headers = getattr(fetched, "headers", None) or {}
        ext, page_links = await loop.run_in_executor(
            _EXTRACT_POOL,
            _call_extract,
            fetched.content_type,
            fetched.body,
            fetched.url,
            headers,
        )
    except Exception:  # noqa: BLE001 — an unreadable page is a miss
        return None

    text = (ext.text or "").strip()
    if not text:
        return None
    digest = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
    meta = _provenance_of(ext, fetched.url, fetched.content_type, headers)

    def _write() -> None:
        db.upsert_web_page(
            _normalize_url(url),
            url,
            fetched.url,
            ext.title or "",
            text,
            fetched.content_type,
            200,
            digest,
            list(page_links or [])[:500],
            **meta,
        )

    try:
        await db.run_in_thread(_write)
    except Exception:  # noqa: BLE001
        return None
    return {"changed": digest != (previous_hash or ""), "title": ext.title or "", "hash": digest}
