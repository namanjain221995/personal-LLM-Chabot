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
from ..core import extract, net, provenance, robots
from ..freshness import Freshness, Verdict, classify_offline
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
# WHY THE RERANKER GETS A CANDIDATE POOL, AND WHAT HAD TO COME WITH IT.
# Until 2026-09-18 _collect_results truncated to the fetch budget before
# _rerank_results saw anything, and run_search_engine then called it with
# `target=len(results)` — so `keep` was the whole list and the cross-encoder
# could only REORDER what engine rank had already chosen. Nothing could be
# dropped. The platform audit measured the cost: on "today's weather forecast
# for Surat", 7 of the 15 sources read and cited were keyword noise (two
# weather.com pages for Kolar, Karnataka 1,500 km away, "Electric current —
# Wikipedia", a JEE physics worksheet), while weather.com/Surat sat in the
# pool the engines returned and was never fetched.
#
# Widening the pool HAD been tried once (2026-08-30) and made results worse:
#
#   "vLLM continuous batching throughput"
#     narrow -> anyscale.com, microsoft.com, arpitbhayani.me
#     wide   -> dasroot.net, rajatpandit.com, heeviz.com
#   "Qwen3 open source model release"
#     narrow -> github.com, huggingface.co, openlm.ai
#     wide   -> 2coffee.dev, daconta.us, orcarouter.ai
#
# That experiment is the reason for the shape of the fix rather than an
# argument against it. Engine rank is an AUTHORITY prior — Bing and Google
# already know anyscale.com outranks a personal blog on this topic — and the
# cross-encoder scores TOPICAL match on title+snippet alone, so ranking a wide
# pool purely on topicality promotes whichever keyword-dense blog repeats the
# query terms most. The pool only widens now that the sort key carries an
# authority term of its own (`web_memory.authority_of`, the score this module
# has always computed and nothing has ever read) and a relevance floor drops
# the noise outright. Engine rank still selects the pool; relevance x
# authority selects what is read.

_TIER_A_SOURCES = 10
_TIER_B_CHARS = 2500

# How many candidates per source actually read. The reranker needs something
# to choose BETWEEN; at 1x it can only reorder. Five is enough that the right
# page is in the pool (the audit's weather.com/Surat sat at engine rank 23 of
# 72) without paying for a second round of provider calls — the provider is
# already asked for `settings.search_max_results` per query and this only
# changes how many of them survive the merge.
_CANDIDATE_MULTIPLIER = 5
# Relevance below this is not a worse answer, it is a different subject: the
# audit scored the seven junk sources of one production batch at 0.0045,
# 0.0037, 0.0020, 0.0002, 0.0000, 0.0000, 0.0000, while every genuine page for
# the same question scored >= 0.9995. Anything in that gap is noise.
_RERANK_FLOOR = 0.05
# Authority moves a relevance score by at most half (official = 100 -> x1.50,
# reference = 70 -> x1.35, neutral = 40 -> x1.20, UGC/low = 15 -> x1.075).
# Deliberately small: it is a tie-breaker between pages that BOTH answer the
# question, and must never lift an off-topic page over a relevant one.
_AUTHORITY_DIVISOR = 200.0
# `web_memory.authority_of` scores the HOST and cannot tell a project's own
# release notes from a site that rewrites them: docs.vllm.ai,
# github.com/vllm-project/vllm/releases, whatsnew.fyi and patchletter.com are
# all neutral 40, which is exactly how the audit's vLLM answer came to cite
# three aggregators and leave docs.vllm.ai uncited at [12]. `core/provenance`
# classifies the PAGE and does tell them apart — 'docs', 'press', 'reference'
# against 'unknown' — so the class earns a second, smaller step.
_FIRST_HAND_TYPES = frozenset({"official", "academic", "docs", "press", "reference"})
_FIRST_HAND_BONUS = 0.15

_FETCH_CONCURRENCY = 16
# Extraction is CPU-bound and trafilatura is not thread-safe — one worker.
_EXTRACT_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="extract")


def _import_extractor() -> None:
    """Import trafilatura (and its lxml) on the extraction thread."""
    try:
        import trafilatura  # noqa: F401
    except Exception:  # noqa: BLE001 — the fallback extractor still works
        pass


def warm_extractor():
    """Start trafilatura's cold import on ``_EXTRACT_POOL`` without waiting.

    Called by the lifespan (2026-09-15): the first search of a process used to
    pay the import inside its first extraction. On the extraction thread, never
    the loop, and serial with every extraction as the pool already guarantees.
    """
    return _EXTRACT_POOL.submit(_import_extractor)


def source_budget(effort: str) -> int:
    """How many sources this level reads per search."""
    return _SOURCE_BUDGET.get(llm.normalize_effort(effort), _SOURCE_BUDGET["think"])


def candidate_budget(effort: str) -> int:
    """How many candidates the reranker gets to choose the read set FROM."""
    return source_budget(effort) * _CANDIDATE_MULTIPLIER


def domain_cap(effort: str) -> int:
    """Pages allowed from any one site in the set actually READ."""
    return _MAX_PER_DOMAIN.get(llm.normalize_effort(effort), _MAX_PER_DOMAIN["think"])


def _today_iso() -> str:
    """Today HERE, as the prompts say it.

    NOTHING on the ordinary search route knew the date. Deep Research has
    carried `state.today` since 2026-09-03 (its module docstring still opens
    with "NO NOTION OF TIME" as the defect it was built to close); the search
    route never got it, so the audit's "what is the latest iPhone?" answer read
    a page saying "Pre-order iPhone 18 Pro now", decided the phone had shipped,
    and wrote that a device "became available in October 2026" — a month in the
    future — because the only calendar in the prompt was the model's training
    data. One function, so a test can move the clock.

    LOCAL, not UTC. This box runs IST (UTC+05:30), so between 00:00 and 05:30
    every night UTC is still yesterday — measured on the first live run of
    this change at 00:54 IST, where a "today's weather for Surat" answer came
    back stamped "As of 2026-09-17". A person asking what today's weather is
    means their today. `_now_stamp` carries the UTC reading alongside it so
    nothing downstream has to guess which calendar a date is on.
    """
    return datetime.now().astimezone().date().isoformat()


def _now_stamp() -> str:
    """'2026-09-18 00:54 IST (2026-09-17 19:24 UTC)'.

    The TIME, not just the day. A spot price, a match score or a forecast is
    minutes old, and the audit's gold answer "gave a date but no time of day"
    — which reads as though it were true for the whole day.
    """
    local = datetime.now().astimezone()
    utc = datetime.now(timezone.utc)
    return (
        f"{local.date().isoformat()} {local:%H:%M} {local.tzname() or 'local time'} "
        f"({utc:%Y-%m-%d %H:%M} UTC)"
    )


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


# `site:host` as a search operator: a whole token (not the tail of
# "website:x"), at the start or after a space or "(". A leading "-" makes it
# an EXCLUSION, which this pattern deliberately does not read as a scope.
_SITE_OP_RE = re.compile(r"(?:^|(?<=[\s(]))site:(\S+)", re.I)


def _site_scope(query: str) -> Tuple[str, ...]:
    """The hosts a query's `site:` operators confine it to; () when none.

    Measured 2026-09-18 on the production SearXNG: 'site:qdrant.tech
    documentation performance benchmark 10 million vectors' returned six
    off-site pages in its top twelve (bing answered the word "documentation";
    yandex honoured the operator). An engine that ignores the operator cannot
    be fixed upstream, so the scope is enforced on what comes back.
    """
    hosts: List[str] = []
    for m in _SITE_OP_RE.finditer(query or ""):
        raw = m.group(1).strip("\"'()[],;")
        if "://" in raw:
            try:
                raw = urlparse(raw).hostname or ""
            except ValueError:
                raw = ""
        host = raw.split("/")[0].split(":")[0].lower().removeprefix("*.").strip(".")
        host = host.removeprefix("www.")
        if host:
            hosts.append(host)
    return tuple(dict.fromkeys(hosts))


def _on_site(url: str, hosts: Sequence[str]) -> bool:
    """The host itself or any subdomain of it — the search engines' meaning.

    A host-suffix test, not `_registrable_domain` equality: `site:docs.python.org`
    must not admit bugs.python.org, and `site:gov.in` (a public suffix, whose
    hosts have no common registrable domain) must admit mea.gov.in."""
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in hosts)


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
    #: True when the PAGE could not be read and this is the search provider's
    #: snippet standing in for it (finding S5). A failed fetch used to be
    #: rebuilt from `r.snippet` and rendered byte-identically to a page that
    #: was fully read, so a 150-character blurb was cited as a source.
    from_snippet: bool = False

    @property
    def domain(self) -> str:
        return urlparse(self.url).hostname or self.url

    def label(self) -> str:
        """'official · published 2026-08-28 · read 2026-09-18'.

        The provenance this dataclass has carried since 2026-09-03 and the
        prompt never showed. Deep Research renders the same line
        (`deep_research._Source.label`); the search route rendered title and
        URL alone, so a page dated three weeks ago and a page dated three years
        ago were indistinguishable in the context block, and "undated" —
        the state an SEO rewrite is usually in — could not be told from fresh.
        """
        bits: List[str] = []
        if self.source_type and self.source_type != "unknown":
            bits.append(self.source_type)
        if self.published_at:
            bits.append(f"published {self.published_at.date().isoformat()}")
        elif self.modified_at:
            bits.append(f"updated {self.modified_at.date().isoformat()}")
        else:
            bits.append("undated")
        if self.modified_at and self.published_at and self.modified_at > self.published_at:
            bits.append(f"updated {self.modified_at.date().isoformat()}")
        if self.fetched_at:
            bits.append(f"read {self.fetched_at.date().isoformat()}")
        return " · ".join(bits)


#: Why a source ended up on a head slice instead of query-centred passages,
#: and how often. A counter rather than a log line per source: the answer path
#: touches this up to 60 times per search. Read it from a shell
#: (`search._HEAD_SLICE_FALLBACKS`) or a test; the FIRST occurrence of each
#: reason is also logged, with the traceback where there is one.
_HEAD_SLICE_FALLBACKS: dict = {}


def _note_head_slice(reason: str, *, exc: bool = False) -> None:
    first = reason not in _HEAD_SLICE_FALLBACKS
    _HEAD_SLICE_FALLBACKS[reason] = _HEAD_SLICE_FALLBACKS.get(reason, 0) + 1
    if not first:
        return
    if exc:
        log.warning(
            "falling back to a head slice (%s); query-centred passage "
            "selection is off for this request", reason, exc_info=True,
        )
    else:
        log.debug("head slice: %s", reason)


class _RobotsRefusal(RuntimeError):
    """The site's robots.txt says not to read this URL (or not to read it yet).

    A distinct type only so the reason is readable in a traceback; it is
    handled by the same `except` as a timeout, because the OUTCOME is the
    same — the page was not read and the provider's snippet stands in for it.
    """


def _select_text(text: str, question: str, budget: int) -> str:
    """`budget` characters of `text`, centred on the question (finding S1/C1).

    `extract.truncate_chars` is a pure head slice, and it is how the answer
    row of a long page stopped reaching the model: fetched, cited in the
    panel, and truncated away. `web_memory.select_passages` spends the SAME
    budget on the parts the question points at.

    With no question there is nothing to centre on, so the head slice stands —
    which is what a caller that never had a question (the char-tier trim in a
    unit test, an operator's own call) keeps getting.
    """
    if not (question or "").strip():
        # DELIBERATE, and the safe direction: with no question there is no
        # signal to centre on, so this is the pre-C1 behaviour, not a
        # degradation of the fix. Counted anyway — see `_HEAD_SLICE_FALLBACKS`.
        _note_head_slice("no question to centre on")
        return extract.truncate_chars(text, budget)
    from ..web_memory import select_passages

    try:
        return select_passages(text or "", question, budget)
    except Exception:  # noqa: BLE001 — selection is an upgrade, never a gate
        # NOT silent. This is the fix C1 exists to deliver, and a bug inside
        # `select_passages` would otherwise put every source back on the head
        # slice — reinstating "fetched, cited, and truncated away" — while
        # emitting nothing above DEBUG. First occurrence is a warning with the
        # traceback; the rest are counted, because one broken page must not
        # produce sixty warnings per search.
        _note_head_slice("passage selection raised", exc=True)
        return extract.truncate_chars(text, budget)


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
        f"Today is {_today_iso()}. "
        f"Turn the user's request into 1 to {cap} concise web-search queries. "
        "Each query must look for something DIFFERENT — do not paraphrase the "
        "same search. Never put a year or a version number into a query unless "
        "the user gave one: the newest release you remember is older than the "
        "web, and searching for it finds last year's answer. Search for the "
        "CURRENT one instead. Respond with ONLY a JSON array of strings, no "
        "prose."
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
    queries = _strip_unasked_pins(message, history, queries)
    return (queries or [message])[:cap]


#: A token that is nothing but a calendar year.
_BARE_YEAR_RE = re.compile(r"^[\(\[\"']*(?:19|20)\d{2}[\)\]\"'.,:;?!]*$")
#: A token that is nothing but a version number — "v0.4.0", "0.4.0", "v2.1".
#: Anchored and whole-token ON PURPOSE: "GPT-5.2" and "Qwen3-VL-8B" are names,
#: not pins, and a substring rule would gut them.
_BARE_VERSION_RE = re.compile(r"^v?\d+(?:\.\d+)+[\)\]\"'.,:;?!]*$", re.I)


def _strip_unasked_pins(
    message: str, history: Sequence[dict], queries: List[str]
) -> List[str]:
    """Remove a year or version the CONVERSATION never mentioned.

    Audit finding 3. The rewriter runs on the router model, whose training
    cutoff is fixed, and it volunteers that cutoff as a search term: asked
    "what is the latest version of vLLM and what changed in it?" it produced
    "what's new in vLLM v0.4.0"; asked "what is the latest iPhone model?" it
    produced "Apple iPhone launch date 2024"; asked "who is the CEO of Intel?"
    it produced "Intel CEO 2024". Three of the audit's eight cases, and
    production `web_searches` rows show the same shape on live traffic
    ("… official statement on AGI status 2025", run on 2026-09-07). Each one
    spends a third of the query budget asking the web to confirm what the
    model already believes — the exact motion that turns a stale memory into a
    cited fact.

    The prompt above now says not to. This is the half that does not depend on
    a 8B model obeying an instruction: a pin the conversation never contained
    cannot have come from the person, so it comes out. A pin the person DID
    give ("what changed in Python 3.13?", or a follow-up whose referent
    "GPT-5.2" was named two turns ago) is theirs and is kept — which is why
    `history` is consulted and not just `message`.

    Token-wise, never substring-wise. A substring rule that deleted every
    digit-dot-digit run would turn "GPT-5.2 reasoning score" into
    "GPT- reasoning score" and break the S2 follow-up resolution built on it.
    """
    if not queries:
        return queries
    said = message + " " + " ".join(
        str(t.get("content") or "") for t in conversation_turns(history, 4)
    )
    said_tokens = {t.strip("()[]\"'.,:;?!").lower() for t in said.split()}
    has_year = any(_BARE_YEAR_RE.match(t) for t in said.split())
    has_version = any(_BARE_VERSION_RE.match(t) for t in said.split())
    if has_year and has_version:
        return queries

    out: List[str] = []
    seen: set = set()
    for q in queries:
        tokens = q.split()
        kept = [
            t
            for t in tokens
            if t.strip("()[]\"'.,:;?!").lower() in said_tokens
            or not (
                (not has_year and _BARE_YEAR_RE.match(t))
                or (not has_version and _BARE_VERSION_RE.match(t))
            )
        ]
        # A query stripped down to nothing is worse than a pinned one. Two
        # content words is the floor — "Intel CEO" searches, "CEO" does not.
        cleaned = " ".join(kept).strip() if len(kept) >= 2 else q
        key = cleaned.lower()
        if key in seen:
            # Stripping can collide two rewrites into one search; a duplicate
            # query spends a slot of the budget on an answer we already have.
            continue
        seen.add(key)
        out.append(cleaned)
    return out


#: A question this short is carrying its subject somewhere else — the previous
#: turn. Three content words is "and its score?" and "what about 5.2?"; it is
#: not "what is GPT-5.2's reasoning score on the BenchLM leaderboard".
_TERSE_CONTENT_WORDS = 3


def resolve_question(message: str, queries: Sequence[str]) -> str:
    """The RETRIEVAL-facing form of a terse follow-up.

    Finding S2: `rewrite_queries` resolves "and its score?" into a real search
    phrase, and that resolution reached SearXNG and nothing else. The
    reranker, the stored-page TTL, stored-evidence retrieval and the
    `Question:` line of the answer prompt all still got the bare phrase, which
    tokenises to `['score']` — no entity, so the cross-encoder scored every
    passage the same and the store was searched for the word "score".

    No second model call and no new prompt: the rewrite ALREADY did the
    anaphora resolution (that is what makes it a usable search query), so the
    referent is recovered from its own output. If the rewrite fell back to the
    raw message, or the message was never terse, this returns it unchanged.

    The user's literal words are kept — this only APPENDS what the
    conversation supplied, so the question line still reads as they asked it.
    Nothing private can enter here: `rewrite_queries` builds its prompt from
    `conversation_turns`, which drops the pinned memory/recall/document blocks.
    """
    from ..web_memory import _content_words

    text = (message or "").strip()
    if not text or not queries:
        return text
    if len(_content_words(text)) > _TERSE_CONTENT_WORDS:
        return text
    extra = (queries[0] or "").strip()
    if not extra or extra.lower() == text.lower():
        return text
    have = set(_content_words(text))
    if not set(_content_words(extra)) - have:
        return text  # the rewrite added no referent this phrase was missing
    return f"{text} ({extra})"


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
    degraded: Optional[dict] = None,
    candidates: Optional[int] = None,
) -> List[SearchResult]:
    """Search every query and merge the results fairly.

    `candidates` is how many merged results to return. Default (None) is the
    fetch budget, which is what every caller wanted while the reranker could
    only reorder. A caller that reranks DOWN to the budget afterwards passes
    `candidate_budget(effort)` instead, so the cross-encoder has something to
    choose between; see the note above `_TIER_A_SOURCES`.

    The old version concatenated results query by query and then head-sliced
    the whole list to 10. With more than one query that silently discarded the
    later ones: query 1 alone could fill the slice, so asking six different
    questions produced the same answer as asking one. The merge is now
    round-robin — rank 1 of every query, then rank 2 of every query — so each
    angle contributes before any angle contributes twice.

    `degraded`, when a caller passes a dict, is FILLED IN with what went wrong
    upstream — `{"engines": [...], "failed_queries": n, "queries": total}`
    (finding S6). It is an out-parameter rather than a second return value so
    every existing caller, and every test double, keeps the list return they
    were written against.
    """
    provider = get_provider()
    failed = 0

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
        # A result assembled while some engines were timing out is NOT the
        # result this query has — caching it for 900 s (finding S6) makes one
        # bad minute upstream the answer to every repeat of the question for
        # the next quarter of an hour. A miss is cheap; a wrong hit is not.
        if not getattr(provider, "unresponsive", {}).get(q):
            _cache_put(f"q:{provider.name}:{categories}:{q}", results)
        return q, results, None

    gathered = await asyncio.gather(*(_one(q) for q in queries))
    per_query: List[List[SearchResult]] = []
    for q, results, exc in gathered:
        if results is None:
            failed += 1
            continue
        # A `site:` query keeps only its site, per QUERY: deep research sends
        # a scoped rescue query alongside unscoped ones, and one operator
        # must not empty its neighbours. Filtered before the panel sees it,
        # so the panel shows what was actually eligible. An empty list is
        # the honest result and every caller already handles one.
        scope = _site_scope(q)
        if scope:
            results = [r for r in results if _on_site(r.url, scope)]
        per_query.append(results)
        await _emit_query(emit, q, results)
    if degraded is not None:
        engines = sorted(
            {e for names in getattr(provider, "unresponsive", {}).values() for e in names}
        )
        if engines or failed:
            # Engine names and counts only — never the query text (S6 must not
            # become the thing that logs what a user asked).
            degraded.update(
                {"engines": engines, "failed_queries": failed, "queries": len(queries)}
            )
            log.info(
                "search degraded: %d/%d queries failed, engines down: %s",
                failed, len(queries), ",".join(engines) or "none",
            )
    if not per_query:
        # One dead upstream engine is normal; ALL dead is unavailability.
        errors = [exc for _q, _r, exc in gathered if exc is not None]
        if errors:
            raise errors[-1]
        return []

    budget = source_budget(effort)
    target = candidates if candidates and candidates > 0 else budget
    per_domain_cap = domain_cap(effort)
    # The domain cap is a rule about what is READ, not about what may be
    # considered. Applied unchanged to a 5x pool it starves exactly the case
    # this pool exists for: weather.com holds a page for every city, and three
    # wrong ones at better engine rank would keep the right one out. It scales
    # with the pool here, and the real cap is re-applied after reranking
    # (`_rerank_results(..., per_domain=...)`), so the read set is as broad as
    # it ever was — just chosen better.
    if target > budget:
        per_domain_cap = max(1, -(-per_domain_cap * target // max(budget, 1)))
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
    message: str,
    results: List[SearchResult],
    target: int,
    per_domain: int = 0,
) -> List[SearchResult]:
    """Choose the read set by RELEVANCE x AUTHORITY, not by engine rank.

    Engine rank was the only pre-read quality signal, and it measurably fails:
    in both probe runs the rank-1 source for "latest vLLM release" was an
    anime video page. Scoring title+snippet pairs costs ~50 ms for 40
    candidates (measured 2026-08-30, after the reranker was fixed to load as
    a cross-encoder rather than an embedding model). Any failure returns the
    input order — reranking is an upgrade, never a gate.

    `target` is the number of results to KEEP, and it must be the FETCH
    BUDGET, not `len(results)`. Both production callers passed the latter
    until 2026-09-18, which made `keep` the whole input and left this function
    unable to drop anything at all — the audit read seven keyword-noise pages
    out of fifteen because of it.

    Three things decide the order, and the second and third are new:

    * RELEVANCE, the cross-encoder's score for the question against
      title+snippet. Anything below `_RERANK_FLOOR` is a different subject
      rather than a worse answer, and is dropped outright — unless the whole
      batch is below it, because reranking is an upgrade and never a gate:
      a search that found only weak matches still answers from them, and the
      coverage check and the prompt say so.
    * AUTHORITY, `web_memory.authority_of` — the 0-100 prior this module has
      computed on every source since 2026-09-03 and which, until now, NOTHING
      read. It multiplies by at most 1.5 (see `_AUTHORITY_DIVISOR`), enough to
      put github.com/vllm-project/vllm/releases above whatsnew.fyi when both
      answer the question, never enough to promote an off-topic page.
    * `per_domain`, the read-set domain cap, re-applied here because
      `_collect_results` widened its own cap to build the pool. Applied in
      score order, so it is the BEST page from a site that survives.
    """
    keep = target if target > 0 else len(results)
    if len(results) <= 2:
        return results[:keep]
    # The shared, TEMPLATED client (app/rerank.py, ADR-0001 D4). Raw
    # title+snippet pairs through /score measurably ranked a careers page
    # above the passage naming the office holder; the model's own prompt
    # format separates them by three orders of magnitude.
    from .. import rerank
    from ..web_memory import authority_of

    try:
        scores = await rerank.score(
            message, [f"{r.title}\n{r.snippet}"[:1000] for r in results]
        )
    except rerank.RerankUnavailable:
        return results[:keep]

    def _prior(url: str) -> float:
        """Host authority x page class, in [1.0, ~1.73]. Never zero: this
        multiplies relevance, it does not replace it."""
        prior = 1.0 + authority_of(url) / _AUTHORITY_DIVISOR
        if provenance.source_type(url) in _FIRST_HAND_TYPES:
            prior *= 1.0 + _FIRST_HAND_BONUS
        return prior

    def _ranked(i: int) -> float:
        return scores[i] * _prior(results[i].url)

    order = sorted(range(len(results)), key=_ranked, reverse=True)
    # The floor is applied to the RAW relevance score. An authority bonus may
    # order two pages that both answer the question; it may never carry an
    # irrelevant one over the bar because it happens to be on a .gov domain.
    relevant = [i for i in order if scores[i] >= _RERANK_FLOOR]
    if not relevant:
        log.info(
            "rerank: every candidate scored below the floor (best %.4f) — "
            "keeping the top %d unfiltered",
            max(scores) if scores else 0.0, keep,
        )
        relevant = order
    if per_domain > 0:
        per_dom: dict = {}
        capped: List[int] = []
        for i in relevant:
            dom = _registrable_domain(results[i].url)
            if per_dom.get(dom, 0) >= per_domain:
                continue
            per_dom[dom] = per_dom.get(dom, 0) + 1
            capped.append(i)
        # Same relaxation as `_collect_results`: a niche question where one
        # site genuinely holds the answer must not be starved below the floor
        # the fetch stage needs.
        if len(capped) < min(keep, _MIN_SOURCES):
            capped.extend(i for i in relevant if i not in set(capped))
        relevant = capped
    dropped = len(results) - len(relevant)
    if dropped:
        log.debug("rerank dropped %d of %d candidates", dropped, len(results))
    return [results[i] for i in relevant][:keep]


def _drop_unread_sources(sources: List[_Source]) -> List[_Source]:
    """Take the SEARCH SNIPPET ONLY sources back out, once enough pages were read.

    Audit finding 7. A source whose page could not be fetched (403, robots,
    timeout) is rebuilt from the search engine's one-line blurb and goes into
    the prompt labelled `_SNIPPET_LABEL`, with the system prompt telling the
    model in as many words to treat it "as a pointer, never as evidence for a
    specific number or quotation". That is an instruction and nothing checked
    it: the audit's weather answer wrote "current temperatures hovering around
    30°C with broken clouds [2][3]" where [2] was a timeanddate.com page that
    had 403'd, so the whole of [2] in the prompt was a 150-character blurb.
    Six of that turn's fifteen sources were snippets; 3.5 per turn across the
    eight cases.

    The deterministic half of the fix, in the shape this repo already uses for
    `_coverage_gap`: when `_MIN_SOURCES` pages were genuinely read, a blurb
    adds nothing that can be cited, so it does not reach the prompt OR the
    panel — the panel then stops counting unread pages as sources, which is
    the same lie in the other direction. Below that floor they stay: a thin
    result set is exactly when a pointer is worth having, and the label and
    the prompt rule still apply to it.

    Renumbers contiguously, because `[n]` in the answer indexes this list.
    """
    read = [s for s in sources if not s.from_snippet]
    if len(read) < _MIN_SOURCES or len(read) == len(sources):
        return sources
    for new_n, s in enumerate(read, start=1):
        s.n = new_n
    return read


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


#: How old a stored page may be for a REALTIME question (a price, a score, the
#: weather "right now"). Until 2026-09-18 REALTIME shared the volatile TTL,
#: WEB_PAGE_FRESH_TTL_S = 3600, with questions like "latest release", so a
#: quote read 59 minutes earlier answered "right now". Five minutes is a
#: choice, not a measurement: long enough that a regenerate or an immediate
#: follow-up reuses the page it just read, short enough that "now" means now.
#: A module constant rather than a setting: nobody has needed to tune it yet.
_REALTIME_PAGE_TTL_S = 300


def _question_verdict(message: str) -> Optional[Verdict]:
    """The offline freshness verdict for `message`, the one `_memory_sources`
    already computes — regex only, microseconds, never a model call. None on
    any failure: the verdict sharpens the page TTL and must never cost the
    search."""
    try:
        return classify_offline(message, now_year=datetime.now(timezone.utc).year)
    except Exception:  # noqa: BLE001
        return None


def _page_ttl(message: str, verdict: Optional[Verdict] = None) -> int:
    """How old a stored page may be and still count as fresh for this ask.

    With a freshness verdict (app.freshness — the classification every other
    stage of the pipeline already runs, ADR-0001 D2/D6) the decision is the
    verdict's: REALTIME gets `_REALTIME_PAGE_TTL_S`, any other VOLATILE one
    ("latest release", "current price") the short TTL, everything else the
    long one. Re-matching _FRESH_RE here disagreed with that verdict at the
    edges — "who is", "score", a bare "2026" and "what is the" all trip the
    regex — so an office-holder question (RECENT, its answer stable for months) threw away
    a two-hour-old copy of the page that answered it and paid a network
    fetch with a 3 s connect + 8 s read ceiling for the same text.

    Without a verdict the regex fallback stands unchanged, so a caller that
    has not classified the question (deep research's fetch path, the
    crawler) gets exactly the TTL it always did.
    """
    if verdict is not None:
        if verdict.requirement is Freshness.REALTIME:
            # Never longer than the volatile TTL: an operator who shortened
            # that one below five minutes meant it for these too.
            return min(_REALTIME_PAGE_TTL_S, settings.web_page_fresh_ttl_s)
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
        # V22: which extractor produced this text. Not optional — a row left
        # at 0 sits in the refresh worker's stale-extractor term and is re-read
        # on a schedule it can never satisfy. 'search' is the dominant store
        # path (1871 of 2208 live rows), so omitting it here would put most of
        # the corpus into a permanent re-fetch loop.
        extract_version=extract.EXTRACT_VERSION,
        **(meta or {}),
    )


async def _fetch_source(
    idx: int,
    r: SearchResult,
    stored: Optional[dict] = None,
    *,
    user_id: Optional[int] = None,
    conversation_id: str = "",
    question: str = "",
) -> Optional[_Source]:
    # Warm path: a fresh stored copy of this exact URL answers without the
    # network. The FULL stored text is cut to the prompt budget the same way a
    # live fetch would be — query-centred since 2026-09-06 (finding S1).
    if stored:
        hit = stored.get(_normalize_url(r.url))
        if hit:
            text = _select_text(
                hit["text"], question, settings.search_source_char_budget
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
        # robots.txt, on the READ path too (finding K6). Until 2026-09-06 only
        # the site crawler asked; a search-result read fetched whatever the
        # provider returned. Refusals and the politeness delay raise, which
        # lands on the snippet fallback below — so a page we may not read is
        # still citable as the provider's blurb, labelled `from_snippet`,
        # exactly like one that timed out.
        if not await robots.allowed(r.url):
            raise _RobotsRefusal("robots.txt disallows this path")
        if not await robots.reserve_slot(r.url):
            raise _RobotsRefusal("crawl-delay longer than an interactive read may wait")
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
        text = _select_text(ext.text, question, settings.search_source_char_budget)
        empty = not text.strip()
        if empty:
            text = r.snippet
        return _Source(
            n=idx,
            title=ext.title or r.title,
            url=r.url,
            text=text,
            from_snippet=empty,
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
            # NOT a read page. Before 2026-09-06 this was rendered
            # byte-identically to a fully read source (finding S5), so a
            # 150-character blurb from a page that timed out was cited as if
            # the page had been read end to end.
            return _Source(
                n=idx, title=r.title, url=r.url, text=r.snippet,
                source_type=provenance.source_type(r.url),
                from_snippet=True,
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
                i + 1, r, stored, user_id=user_id, conversation_id=conversation_id,
                question=message,
            )

    fetched = await asyncio.gather(*(guarded(i, r) for i, r in enumerate(results)))
    sources = [s for s in fetched if s is not None]
    # renumber contiguously after drops
    for new_n, s in enumerate(sources, start=1):
        s.n = new_n
    return sources


def _apply_char_tiers(sources: List[_Source], question: str = "") -> List[_Source]:
    """Trim the long tail so a big result set stays a sane prompt.

    Search rank is the only quality signal available before reading, so the
    top-ranked pages keep the full per-source budget — meaning High is never
    shallower than Medium on the pages most likely to matter — and everything
    after them is cut to a summary-sized excerpt.

    That excerpt is where finding C1 bites hardest: on `max` effort this is 50
    of 60 sources, each cut to 2,500 characters OFF THE TOP. `question` makes
    the cut query-centred instead; without one the head slice stands, so a
    caller that passes only the list behaves exactly as before.
    """
    for s in sources:
        if s.n > _TIER_A_SOURCES:
            s.text = _select_text(s.text, question, _TIER_B_CHARS)
    return sources


#: How a source that was NOT read is labelled in the prompt (finding S5).
_SNIPPET_LABEL = " — SEARCH SNIPPET ONLY, the page itself could not be read"


def _context_block(sources: List[_Source]) -> str:
    """The numbered sources, each with its provenance line.

    `[n] title (url) — official · published 2026-08-28 · read 2026-09-18`.
    Without the dates the model had no way to tell a pre-order announcement
    from a shipping one, and no way to weigh a three-year-old page against
    today's; it filled the gap from training data (audit finding 1).
    """
    blocks = [
        f"[{s.n}] {s.title} ({s.url}) — {s.label()}"
        f"{_SNIPPET_LABEL if s.from_snippet else ''}\n{s.text}"
        for s in sources
    ]
    return "\n\n".join(blocks)


def _coverage_gap(question: str, sources: Sequence[_Source]) -> List[str]:
    """Question terms that appear in NONE of the assembled sources.

    Finding S3: nothing on the live path could tell "the sources do not
    mention this" from "this is not the case", so a page whose answer row had
    been truncated away (C1) produced a confident "GPT-5.2 is not ranked" with
    a full citation panel behind it. The store path has `_answerability` for
    exactly this and it is unreachable from `run_search_engine`; this is the
    cheap deterministic half — no model call, no network.

    Stemmed comparison, so "leaderboards" covers "leaderboard". Returns the
    RAW words, for a message a person can read.

    COST. The first version asked this question the expensive way round: it
    built `set(_terms(" ".join(title + text for every source)))` — on `max`
    effort a ~205,000-character join, tokenised, stemmed and turned into a
    20,000-entry set — in order to look up five words. It ran here, BEFORE the
    first answer token, and again after the stream for `meta.coverage_gap`:
    measured on this box, 12.0 ms a call and 24.0 ms a search, all of it on
    the event loop in front of TTFT. This repo has already paid for a
    CPU-bound pre-pass on that loop once (2026-09-05, TTFT 0.7 s -> 11.7 s at
    8 concurrent).

    `web_memory.terms_present` inverts it: the source texts are streamed
    rather than joined, only tokens whose first two characters could match a
    question term reach the stemmer, and the scan stops as soon as every term
    has been found — which is the ordinary case. Same answer, 0.16 ms.
    `run_search_engine` also computes it ONCE now and passes it to both
    consumers.
    """
    from ..web_memory import _content_words, _stem, terms_present

    if not sources or not (question or "").strip():
        return []
    raws = _content_words(question)
    if not raws:
        return []
    stems = [_stem(w) for w in raws]
    have = terms_present(
        (part for s in sources for part in (s.title, s.text)), set(stems)
    )
    missing: List[str] = []
    for raw, stem in zip(raws, stems):
        if len(raw) > 2 and stem not in have and raw not in missing:
            missing.append(raw)
    return missing[:5]


#: Deliberately narrow. It forbids ONE thing — asserting an absence the
#: sources never state — and does not ask the model to refuse: over-hedging a
#: question it can answer would just be a different wrong answer.
_COVERAGE_NOTE = (
    "\n\nCOVERAGE CHECK: these words from the question appear in NONE of the "
    "sources above: {terms}. That means THE SOURCES RETRIEVED DO NOT COVER "
    "them. It does NOT establish that they do not exist, are not ranked, are "
    "not listed or have no value. If the answer depends on one of them, say "
    "the sources found do not cover it; never state an absence as a fact "
    "unless a source says so in as many words."
)


def _answer_messages(
    message: str,
    sources: List[_Source],
    history: Sequence[dict],
    gap: Optional[List[str]] = None,
) -> List[dict]:
    system = (
        f"Today is {_now_stamp()}. The numbered sources below were fetched from "
        "the live web during this request and are NEWER than your training "
        "data: where a source contradicts what you remember, the source wins. "
        "Each source carries its own dates — read them. A date LATER than "
        "today has not happened yet: write \"expected\", \"announced for\" or "
        "\"scheduled\", never the past tense, and never call something released, "
        "shipped, launched or available unless a source says it already is. "
        "Where the answer can move, stamp it: \"as of "
        f"{_today_iso()}\".\n"
        "EVERY factual claim you make — a date, a number, a name, a version, a "
        "status — must be supported by one of the numbered sources. If the "
        "sources do not support a claim, DO NOT MAKE IT: say what the sources "
        "do establish and state the uncertainty plainly. An answer that names "
        "what is still unknown is correct; one that fills the gap from memory "
        "is not.\n"
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
        "longer answer — it is material for a better-supported one.\n"
        "A source marked SEARCH SNIPPET ONLY is a one-line blurb from the "
        "search engine, not a page anyone read — treat it as a pointer, never "
        "as evidence for a specific number or quotation."
    )
    # `gap` is passed in by `run_search_engine`, which needs the same list
    # again for `meta.coverage_gap` after the stream — computing it here as
    # well was the second half of the 24 ms. Still computed on demand for a
    # caller (or a test) that has only the question and the sources.
    if gap is None:
        gap = _coverage_gap(message, sources)
    if gap:
        system += _COVERAGE_NOTE.format(terms=", ".join(gap))
    user = f"Web sources:\n{_context_block(sources)}\n\nQuestion: {message}"
    return [{"role": "system", "content": system + DIAGRAM_INSTRUCTION}, *recent_turns(history, settings.chat_history_turns),
            {"role": "user", "content": user}]


_CITE_MARKER = re.compile(r"\[(\d{1,3})\]")


def _meta_sources(sources: Sequence[_Source], answer: str = "") -> List[dict]:
    """The sources panel. Three states, not one (finding S5).

    `meta.sources` was the FETCHED set, rendered identically whether the page
    was read end to end, served from the store, or silently replaced by the
    search engine's blurb after the fetch failed — and with no way to tell
    which ones the answer actually leant on. `read`/`cited` say so.
    """
    cited = {int(n) for n in _CITE_MARKER.findall(answer or "")}
    return [
        {
            "n": s.n,
            "title": s.title,
            "url": s.url,
            "domain": s.domain,
            "read": not s.from_snippet,
            "from_store": s.from_store,
            "cited": s.n in cited,
        }
        for s in sources
    ]


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
                # Query-centred, like every other source since 2026-09-06
                # (finding S1): a stored passage head-sliced to 2,500 chars
                # loses its answer exactly the way a live page does.
                text=_select_text(text, message, _TIER_B_CHARS),
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
    #
    # `repair_stale_chunks=False` for the same reason as the Fast path, one
    # step removed. This runs after the answer has streamed, so it cannot slow
    # THIS request — but a chunker bump makes the repair backlog the whole
    # corpus, and `embed_query` sheds load ("busy") rather than queueing, so a
    # 20-page re-embed started here can degrade the NEXT question's recall.
    # The repair has an owner with nobody waiting on it: the worker.
    await asyncio.sleep(2.0)
    await web_index.index_pending(repair_stale_chunks=False)
    # Owner idea (2026-08-30): with the answer already streamed, quietly
    # follow a few in-site links from the pages just read so the NEXT related
    # question hits warm content. Tightly capped; robots respected.
    try:
        from .crawl import expand_search_domains

        await expand_search_domains([r.url for r in results])
    except Exception:  # noqa: BLE001 — enrichment, never surfaces
        pass


def _degraded_note(degraded: dict) -> str:
    """One human line about a reduced search, for a `status` event.

    Contains engine names and counts and NOTHING else — never the query, which
    is what makes this safe to emit and safe to log (finding S6).
    """
    bits: List[str] = []
    failed = int(degraded.get("failed_queries") or 0)
    total = int(degraded.get("queries") or 0)
    if failed:
        bits.append(f"{failed} of {total} searches failed")
    engines = degraded.get("engines") or []
    if engines:
        bits.append("engines unavailable: " + ", ".join(engines[:6]))
    detail = "; ".join(bits) or "some engines did not answer"
    return f"Partial search results — {detail}."


async def _fallback(
    message: str,
    history: Sequence[dict],
    emit: Emit,
    note: str,
    effort: str = "think",
) -> str:
    """Answer from model knowledge when the search pipeline produced nothing.

    Runs at the TURN's effort. Left at the default it ran at "think" — so the
    one path a Fast turn reaches when SearXNG is unreachable was also the one
    path that spent a reasoning pass the person had switched off.
    """
    await emit("status", {"text": note})
    parts: List[str] = []
    msgs = [
        {"role": "system", "content": "You are a helpful assistant. Web search is "
         "unavailable, so answer from your own knowledge and say so if the answer "
         "may be out of date."},
        *recent_turns(history, settings.chat_history_turns),
        {"role": "user", "content": message},
    ]
    async for kind, delta in llm.stream_chat_events(
        msgs, effort=llm.normalize_effort(effort), max_tokens=8000
    ):
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
        results = await _collect_results(
            queries, effort, emit, candidates=candidate_budget(effort)
        )
    except SearchUnavailableError:
        return "", []
    if not results:
        return "", []
    # The rewrite resolved the referent; every consumer below gets it, not the
    # bare phrase (finding S2).
    asked = resolve_question(question, queries)
    results = await _rerank_results(
        asked, results, source_budget(effort), per_domain=domain_cap(effort)
    )
    if emit is not None:
        await emit("research", {"phase": "reading", "count": len(results)})
    sources = _apply_char_tiers(
        _drop_unread_sources(
            await _fetch_sources(
                results, asked, user_id=user_id, conversation_id=conversation_id,
                # No caller passed a verdict until 2026-09-18, so a REALTIME
                # question fell back to `_FRESH_RE` and the 3600 s TTL.
                verdict=_question_verdict(asked),
            )
        ),
        asked,
    )
    if not sources:
        return "", []
    sources = await _memory_sources(asked, sources)
    _spawn(
        _persist_and_index(
            question, queries, results, effort, user_id, conversation_id
        )
    )
    answer = await llm.chat_completion(
        _answer_messages(asked, sources, history),
        temperature=0.2,
        max_tokens=5000,
        # A plan step's answer is written at the turn's level, not at
        # chat_completion's thinking-on default.
        thinking=llm.wants_thinking("smart", effort),
    )
    if emit is not None:
        # Pages opened, not rows in the panel — same correction as
        # `run_search_engine`.
        await emit(
            "research",
            {"phase": "read", "count": sum(1 for s in sources if not s.from_snippet)},
        )
    return answer, _meta_sources(sources, answer)


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
    degraded: dict = {}
    try:
        queries = await rewrite_queries(message, history, effort)
        results = await _collect_results(
            queries, effort, emit, degraded=degraded,
            candidates=candidate_budget(effort),
        )
    except SearchUnavailableError:
        return await _fallback(
            message, history, emit,
            "Web search unavailable — answering from model knowledge.", effort,
        )
    if degraded:
        # An existing event type, never a new one: `status` is what the panel
        # already renders (finding S6 — this was invisible in every channel).
        await emit("status", {"text": _degraded_note(degraded)})
    if not results:
        return await _fallback(
            message, history, emit,
            "No web results found — answering from model knowledge.", effort,
        )

    # The rewrite already resolved "and its score?" into a query naming the
    # entity; everything downstream used to get the bare phrase back
    # (finding S2). Retrieval, the TTL and the question line get the resolved
    # form; `history` still carries the user's literal words.
    asked = resolve_question(message, queries)

    # Relevance-ordered SELECTION: score the candidate snippets with the
    # reranker BEFORE spending fetch time on them, so "best 15 of the pool"
    # replaces "first 15 by engine rank" (which put an anime page at [1]).
    # `source_budget(effort)`, never `len(results)` — handing this the fetch
    # budget as the pool is what made the comment above untrue for a fortnight.
    results = await _rerank_results(
        asked, results, source_budget(effort), per_domain=domain_cap(effort)
    )

    await emit("status", {"text": f"Reading {len(results)} sources…"})
    await emit("research", {"phase": "reading", "count": len(results)})
    sources = _apply_char_tiers(
        _drop_unread_sources(
            await _fetch_sources(
                results, asked, user_id=user_id, conversation_id=conversation_id,
                # No caller passed a verdict until 2026-09-18, so a REALTIME
                # question fell back to `_FRESH_RE` and the 3600 s TTL.
                verdict=_question_verdict(asked),
            )
        ),
        asked,
    )
    if not sources:
        return await _fallback(
            message, history, emit,
            "Couldn't read the sources — answering from model knowledge.", effort,
        )
    # Paragraphs from pages read in EARLIER searches, dated, after the live set.
    sources = await _memory_sources(asked, sources)

    # What was READ, not what is in the panel: `len(sources)` counted
    # snippet-only rows and store-memory rows as pages someone opened, so the
    # panel said "15 read" for a turn in which six pages had 403'd.
    await emit(
        "research",
        {"phase": "read", "count": sum(1 for s in sources if not s.from_snippet)},
    )

    # Remember this search — log, pages, vectors — behind the answer.
    _spawn(
        _persist_and_index(
            message, queries, results, effort, user_id, conversation_id
        )
    )

    # Once per request, not once per consumer: the prompt below and the
    # `meta` after the stream want the same list.
    gap = _coverage_gap(asked, sources)

    parts: List[str] = []
    async for kind, delta in llm.stream_chat_events(
        _answer_messages(asked, sources, history, gap=gap),
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

    answer = "".join(parts)
    meta = {"route": "search", "sources": _meta_sources(sources, answer)}
    if gap:
        # What the model was told, recorded where the client and the stored
        # turn can see it (finding S3).
        meta["coverage_gap"] = gap
    if degraded:
        meta["search_degraded"] = degraded
    await emit("meta", meta)
    return answer


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
            picked, question, user_id=user_id, conversation_id=conversation_id,
            verdict=_question_verdict(question),
        )
    except Exception:  # noqa: BLE001 — a failed read is a miss, not an error
        return 0

    # Index synchronously HERE, unlike the streaming path: the caller is about
    # to read the corpus back, so a write-behind index would mean answering
    # from evidence that has not landed yet.
    #
    # `repair_stale_chunks=False` keeps that promise cheap. V24 added a second
    # reason to index — vectors built by an older chunker — and on a corpus
    # that has just had a chunker bump that backlog is EVERY page. Draining it
    # here would put up to `limit` pages of re-chunking and re-embedding inside
    # a user's deadline for work nobody is waiting on. The repair belongs to
    # the worker, which has nobody waiting on it; this call still stamps the
    # current chunker on what it writes.
    try:
        await web_index.index_pending(repair_stale_chunks=False)
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


def _conditional_headers(etag: str, last_modified: str) -> dict:
    """The validators for a conditional re-fetch (RFC 9110 §13).

    `etag` and `last_modified` have been WRITTEN by the store since V14 and
    read by nothing (finding K5), so every refresh was a full re-download of a
    page that had usually not changed. Both are sent when both are known: an
    ETag is the stronger validator, and a server that ignores it may still
    honour the date.

    The stored ETag is echoed back exactly as the server sent it, weak prefix
    (`W/"…"`) and quotes included — an ETag is an opaque string and rewriting
    it is how a conditional request silently stops matching. Anything with a
    CR/LF in it is dropped rather than sent; `net.safe_fetch` would refuse it
    anyway, and a validator is not worth failing a refresh over.
    """
    out: dict = {}
    tag = (etag or "").strip()
    if tag and "\r" not in tag and "\n" not in tag:
        out["If-None-Match"] = tag
    since = (last_modified or "").strip()
    if since and "\r" not in since and "\n" not in since:
        out["If-Modified-Since"] = since
    return out


async def refetch_page(
    url: str,
    *,
    previous_hash: str = "",
    etag: str = "",
    last_modified: str = "",
    conditional: bool = True,
) -> Optional[dict]:
    """Re-read one already-known page through the ordinary safe path.

    The refresh worker's only way to fetch. Everything here is the SAME code an
    ordinary search source goes through — net.safe_fetch (SSRF guards, redirect
    re-validation, size cap, timeout) and extraction on `_EXTRACT_POOL`, the
    single-worker executor that exists because trafilatura shares module-level
    lxml XPath objects that are not thread-safe and will abort the interpreter
    if two pages parse at once.

    Two things this path did not do until 2026-09-06, and the V23 backfill is
    what makes them urgent — it schedules 1,602 previously unreachable pages,
    taking the worker to ~2,300 third-party fetches a day:

    * **Conditional requests (K5).** The stored `etag`/`last_modified` are
      sent as `If-None-Match`/`If-Modified-Since`. A **304** means the copy on
      disk is still correct: the freshness clock advances and nothing else
      happens — no body downloaded, no extraction, no re-chunk, no re-embed.
      It is reported as `not_modified`, is NOT a failure, and must not
      increment `refresh_failures`.
    * **robots.txt (K6).** The refresh worker is an unattended bot re-reading
      pages nobody asked for right now, so a `Disallow` is obeyed and a
      `Crawl-delay` is waited out in full.

    `conditional=False` sends NO validators even when they are stored, and it
    is not a politeness regression — it is the only way to repair a page whose
    stored TEXT is what is wrong. Re-extraction needs the original HTML, which
    nothing keeps; a 304 returns no body, so a conditional request for such a
    page can never do anything but confirm the bad copy and requeue it. The
    caller (`web_worker._stale_extractor`) asks for this ONLY for rows below
    `extract.EXTRACT_VERSION`; ordinary freshness keeps the conditional
    request, which is what makes the refresh affordable. Robots, the SSRF
    guards, the byte cap and the timeout are identical either way — the flag
    changes two request headers and nothing else.

    Returns {'changed', 'title', 'hash', 'not_modified', 'blocked'} or None
    when the page could not be read. Storing happens here so the caller cannot
    forget the content-hash rule that resets the vector watermark.
    """
    url_key = _normalize_url(url)
    # The worker is the least interactive fetcher we have — nobody is watching
    # a stream — so it waits out the FULL crawl-delay rather than skipping.
    try:
        if not await robots.allowed(url):
            log.debug("refresh skipped, robots.txt disallows %s", url[:120])
            return {
                "changed": False, "title": "", "hash": previous_hash or "",
                "not_modified": False, "blocked": True,
            }
        await robots.reserve_slot(url, max_wait_s=15.0)
    except Exception:  # noqa: BLE001 — robots must never break the refresh
        log.debug("robots check unavailable for %s", url[:120], exc_info=True)

    try:
        fetched = await net.safe_fetch(
            url,
            timeout_ms=settings.fetch_timeout_ms,
            max_bytes=settings.fetch_max_bytes,
            accept="text/html,application/pdf,text/plain",
            headers=_conditional_headers(etag, last_modified) if conditional else {},
        )
        if fetched.status == 304:
            # Return BEFORE the executor hop: entering extraction here would
            # parse an empty body and store a blank page.
            resp = getattr(fetched, "headers", None) or {}
            try:
                await db.run_in_thread(
                    db.touch_web_page_unchanged,
                    url_key,
                    (resp.get("etag") or "").strip(),
                    (resp.get("last-modified") or "").strip(),
                )
            except Exception:  # noqa: BLE001 — the deadline still gets stamped
                log.debug("could not stamp 304 freshness for %s", url[:120], exc_info=True)
            return {
                "changed": False,
                "title": "",
                "hash": previous_hash or "",
                "not_modified": True,
                "blocked": False,
            }
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
            # V22: the refresh worker's whole point is to re-read pages whose
            # extractor is behind; if the re-read stored 0 again the page would
            # never leave the queue.
            extract_version=extract.EXTRACT_VERSION,
            **meta,
        )

    try:
        await db.run_in_thread(_write)
    except Exception:  # noqa: BLE001
        return None
    return {
        "changed": digest != (previous_hash or ""),
        "title": ext.title or "",
        "hash": digest,
        "not_modified": False,
        "blocked": False,
    }
