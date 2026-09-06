"""Site crawler: "index this whole site" → every in-scope page stored + embedded.

Owner request 2026-08-30. A documentation site is a map of links; crawling it
into the V8 web store makes the chatbot genuinely expert on it — later
questions answer from the complete local copy, cited, with no network fetch.
The shape copies the GitHub repo engine exactly: paste a URL with intent,
watch progress, then just ask questions.

DISCOVERY is sitemap-first (a docs site publishes its full page list —
measured live: docs.vllm.ai's robots.txt names a flat sitemap of 2,451 URLs),
falling back to breadth-first link-walking when a site has no usable sitemap.
Every byte comes through ``net.safe_fetch`` (SSRF-guarded); robots.txt is
respected via ``core/robots.py``, with an honest User-Agent and a politeness
delay per fetch.

SCOPE is the pasted URL's host + path prefix. A crawl never leaves the site:
enqueue is filtered AND the post-redirect final URL is re-checked before
anything is stored or harvested.

STORAGE is the existing global ``web_pages`` upsert — content-hashed, so a
re-crawl updates only pages that actually changed, and pages already stored
by ordinary searches are recognised as done. The vector index drains from the
same ``indexed_at`` watermark the search path uses.

THE FRONTIER IS DURABLE (K12, 2026-09-07). The queue of unread in-scope URLs
lives in ``web_crawl_frontier``, keyed by the crawl's SCOPE rather than by any
one ``web_crawls`` row, so an interrupted crawl resumes at the page it stopped
on — including a link-walk crawl of a site with no sitemap, which used to
restart at the root, and any crawl resumed after the 24h page TTL, which used
to re-fetch everything. The opportunistic post-search expansion keeps the old
in-process queue on purpose; see ``_Frontier``.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from .. import db, llm, metrics, web_index
from ..config import settings
from ..core import net, robots
from ..core import extract
from . import recent_turns
from .search import _EXTRACT_POOL, _call_extract, _normalize_url, _provenance_of

log = logging.getLogger(__name__)

Emit = Callable[[str, dict], Awaitable[None]]

#: "index/crawl/scrape <url>" — intent word + URL. A bare pasted URL is NOT a
#: crawl (that is the single-page URL engine); asking to read "this site/whole
#: site/all pages" is. Matched against the message with every URL replaced by
#: the token `<url>`: the review found "what does …/index.html say" and any
#: URL containing "index"/"scrape" in its PATH launching thousand-page crawls.
#: "index" is also the one intent word that is an everyday noun, so alone it
#: only counts followed by an object ("index this site", "index <url>") —
#: crawl/scrape/mirror/ingest stay strong verbs on their own.
_INTENT_WORD = (
    # -ing/-ed forms included ("continue crawling <url>"), but never the
    # agent nouns: "the crawler at <url>" and "a scraper for <url>" describe
    # software, not a request.
    r"(?:\b(?:crawl(?:ing|ed)?|scrap(?:e|ing|ed)|mirror(?:ing)?|ingest(?:ing|ed)?)\b"
    # Lookahead, not consumption: when the object IS the URL ("index
    # <url>"), the URL token must stay available for the outer match.
    r"|\bindex\b(?=\s+(?:<url>|this|that|the|its|whole|entire|all|every|site|website|page|docs|documentation))"
    r")"
)
_INTENT_RE = re.compile(
    _INTENT_WORD + r".{0,80}?<url>"
    r"|<url>.{0,80}?(?:" + _INTENT_WORD
    + r"|\bwhole site\b|\bentire site\b|\ball (?:the )?pages\b|\bevery page\b)",
    re.I | re.S,
)
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)

#: "continue/resume crawling" — the resume phrase the capped-crawl message
#: advertises. It carries no URL, so detect_crawl can never see it; the
#: dispatcher checks this against the conversation's crawled sites instead
#: (the review found the advertised phrase routing to ordinary site Q&A).
_RESUME_RE = re.compile(
    r"\b(?:continue|resume|keep|finish)\b.{0,40}?\b(?:crawl\w*|index\w*|scrap\w*)\b"
    r"|\b(?:crawl|index|scrape)\b.{0,30}?\b(?:more|the rest|remaining)\b",
    re.I | re.S,
)

_SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_SITEMAP_INDEX_RE = re.compile(r"<\s*sitemapindex", re.I)
#: K11 (2026-09-06): a sitemap entry is <loc> PLUS, when the site publishes
#: it, <lastmod> — the site's own statement about when that page last
#: changed, and the only freshness signal available before a single byte of
#: the page is fetched. It was being parsed away: only <loc> was ever read.
#: Entry-scoped on purpose — a flat findall() of <loc> and a flat findall()
#: of <lastmod> cannot be zipped, because a sitemap may carry <lastmod> for
#: only some of its URLs and the two lists would silently misalign.
_SITEMAP_ENTRY_RE = re.compile(
    r"<(url|sitemap)\b[^>]*>(.*?)</\1\s*>", re.I | re.S
)
_SITEMAP_LASTMOD_RE = re.compile(r"<lastmod>\s*([^<]+?)\s*</lastmod>", re.I)
#: A sitemap this big is a generated index, not a page list worth holding
#: dates for. Bounds the per-crawl lastmod map.
_SITEMAP_MAX_ENTRIES = 50_000


def parse_sitemap(body: str) -> List[Tuple[str, str]]:
    """[(loc, lastmod)] from one sitemap document; lastmod "" when absent.

    The raw <lastmod> text is kept exactly as the site published it (W3C
    Datetime: "2026-08-19" or "2026-08-19T11:30:00+00:00"); interpreting it
    is `sitemap_lastmod_at`'s job.
    """
    out: List[Tuple[str, str]] = []
    matched = False
    for _tag, entry in _SITEMAP_ENTRY_RE.findall(body or ""):
        matched = True
        loc = _SITEMAP_LOC_RE.search(entry)
        if not loc:
            continue
        stamp = _SITEMAP_LASTMOD_RE.search(entry)
        out.append((loc.group(1), (stamp.group(1) if stamp else "").strip()))
        if len(out) >= _SITEMAP_MAX_ENTRIES:
            break
    if not matched:
        # A sitemap whose entries this parser did not recognise (or a bare
        # list of <loc>): previous behaviour, with no dates.
        out = [(loc, "") for loc in _SITEMAP_LOC_RE.findall(body or "")[:_SITEMAP_MAX_ENTRIES]]
    return out


def sitemap_lastmod_at(raw: str):
    """A tz-aware UTC datetime from a raw <lastmod>, or None.

    Rejects the dates a sitemap generator emits when it has nothing real to
    say — before the web, or in the future — so a build timestamp cannot
    make an ancient page look freshly written.
    """
    from ..core.provenance import parse_date

    stamp = parse_date(raw)
    if stamp is None:
        return None
    now = datetime.now(timezone.utc)
    if stamp.year < 1995 or stamp > now + timedelta(days=1):
        return None
    return stamp


#: Extensions that are never readable pages — skipped at enqueue so the fetch
#: budget goes to real content (extract would refuse them anyway).
_SKIP_EXT_RE = re.compile(
    r"\.(zip|tar|gz|tgz|whl|exe|dmg|iso|png|jpe?g|gif|svg|webp|ico|mp[34]|webm|"
    r"woff2?|ttf|css|js|map)$",
    re.I,
)


def detect_crawl(text: str) -> Optional[str]:
    """The URL to crawl, when the message asks for a whole-site crawl."""
    if not text:
        return None
    m = _URL_RE.search(text)
    if not m:
        return None
    url = m.group(0).rstrip(".,;:!?")
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    # Words inside a URL are addresses, not requests: match intent against
    # the message with URLs collapsed to a token.
    tokenized = _URL_RE.sub(" <url> ", text)
    if not _INTENT_RE.search(tokenized):
        return None
    return url


def detect_resume(text: str) -> bool:
    """True when the message asks to continue an earlier crawl."""
    return bool(text) and bool(_RESUME_RE.search(text)) and not _URL_RE.search(text)


@dataclass
class _CrawlState:
    scope_host: str
    scope_prefix: str  # normalized-URL prefix ("host/path")
    visited: Set[str] = field(default_factory=set)
    fetched: int = 0
    from_store: int = 0
    failed: int = 0
    #: K9: pages fetched fine but refused by the store-time quality gate,
    #: by reason ("thin", "js_shell", "empty"). Counted separately from
    #: `failed` so "the site returned shells" is distinguishable from "the
    #: fetches did not work".
    refused: Dict[str, int] = field(default_factory=dict)
    #: K11: url_key -> the raw <lastmod> its sitemap published for it.
    lastmod: Dict[str, str] = field(default_factory=dict)


#: K12: how many times one URL may fail to fetch before a campaign gives up
#: on it. A transient blip must not cost a page for the life of the crawl; a
#: permanently dead URL must not be re-attempted by every resume forever. The
#: retry lands on the NEXT run, not this one — within a run an attempted URL is
#: behind the frontier cursor.
_FRONTIER_MAX_FAILURES = 2

#: K12: a crawl campaign with no frontier activity for this long is abandoned,
#: and the next crawl of that site starts from a clean sheet instead of
#: inheriting a half-finished queue. Re-deriving costs one sitemap fetch, and
#: the pages themselves are re-validated by the store TTL anyway — correctness
#: (a site CAN be crawled again from scratch) beats saving a few fetches.
_FRONTIER_CAMPAIGN_MAX_AGE_S = 7 * 24 * 3600


class _Frontier:
    """A crawl's work queue: which in-scope URLs are still unread, and how deep.

    K12 (2026-09-07). This used to be a plain `List[Tuple[str, int]]` local,
    thrown away with the process. A restart re-derived it from the sitemap and
    leaned on `web_pages` freshness to skip what was already read — which
    covered nothing at all for a link-walk crawl of a site with no sitemap
    (it restarted at the root) and nothing for any crawl resumed after the 24h
    page TTL (it re-fetched everything).

    Durable when a `scope_prefix` is given, in-process when it is not. The
    opportunistic post-search expansion deliberately gets the in-process one:
    it is a handful of pages per domain on the idle path after an answer, its
    scope is whatever URL a search happened to return, and persisting one row
    per URL for each of those would fill the table with campaigns nobody will
    ever resume.

    The cursor is what keeps a run moving. Rows are settled AFTER the page is
    read, so a URL attempted and deferred is still 'pending' and would be
    handed back forever by a plain "next pending" query; ordering by
    (depth, id) and only ever asking for rows past the last one taken gives
    breadth-first order, no repeats within a run, and a full retry on the next
    run — where the cursor starts over.
    """

    def __init__(
        self, scope_prefix: Optional[str] = None, crawl_id: Optional[int] = None
    ) -> None:
        self.scope = scope_prefix or ""
        self.crawl_id = crawl_id
        self._mem: List[Tuple[str, int]] = []
        self._cursor: Tuple[int, int] = (-1, 0)

    @property
    def durable(self) -> bool:
        return bool(self.scope)

    async def add(self, entries: Sequence[Tuple[str, int]]) -> None:
        """Enqueue (url, depth) pairs. Already-known URLs keep their state."""
        if not entries:
            return
        if not self.durable:
            self._mem.extend(entries)
            return
        rows = [(_normalize_url(u), u, int(d)) for u, d in entries]
        await db.run_in_thread(db.add_crawl_frontier, self.scope, rows, self.crawl_id)

    async def take(self, limit: int) -> List[Tuple[str, int]]:
        """The next batch of unread URLs. [] means this run has drained it."""
        if not self.durable:
            batch, self._mem = self._mem[:limit], self._mem[limit:]
            return batch
        rows = await db.run_in_thread(
            db.take_crawl_frontier, self.scope, limit, self._cursor[0], self._cursor[1]
        )
        if rows:
            self._cursor = (int(rows[-1]["depth"]), int(rows[-1]["id"]))
        return [(r["url"], int(r["depth"])) for r in rows]

    async def settle(self, by_outcome: Dict[str, List[str]]) -> None:
        """Mark URLs read, with why. Terminal for this campaign."""
        if not self.durable:
            return
        for outcome, keys in by_outcome.items():
            if keys:
                await db.run_in_thread(
                    db.mark_crawl_frontier, self.scope, keys, outcome, self.crawl_id
                )

    async def defer(self, url_keys: Sequence[str]) -> None:
        """A fetch failed: count it and leave it for a later run, up to a cap."""
        if not self.durable or not url_keys:
            return
        await db.run_in_thread(
            db.defer_crawl_frontier,
            self.scope, list(url_keys), _FRONTIER_MAX_FAILURES, self.crawl_id,
        )

    async def counts(self, visited_hint: int = 0) -> Dict[str, int]:
        if not self.durable:
            return {
                "pending": len(self._mem),
                "visited": visited_hint,
                "total": len(self._mem) + visited_hint,
            }
        return await db.run_in_thread(db.crawl_frontier_counts, self.scope)

    async def retire_if_abandoned(self) -> None:
        """Drop a campaign nobody has touched in a week (see the constant)."""
        if not self.durable:
            return
        dropped = await db.run_in_thread(
            db.clear_crawl_frontier, self.scope, float(_FRONTIER_CAMPAIGN_MAX_AGE_S)
        )
        if dropped:
            log.info(
                "crawl frontier: retired %d abandoned entries for %s",
                dropped, self.scope[:120],
            )

    async def close(self) -> None:
        """The campaign finished: the next crawl of this site starts fresh."""
        if self.durable:
            await db.run_in_thread(db.clear_crawl_frontier, self.scope, None)


def _in_scope(state: _CrawlState, url: str) -> bool:
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower().removeprefix("www.")
    if host != state.scope_host:
        return False
    if _SKIP_EXT_RE.search(parts.path or ""):
        return False
    return _normalize_url(url).startswith(state.scope_prefix)


def _scope_of(root_url: str) -> Tuple[str, str]:
    parts = urlparse(root_url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = (parts.path or "/").rsplit("/", 1)[0] if "." in (parts.path or "").rsplit("/", 1)[-1] else (parts.path or "/")
    prefix = _normalize_url(f"{parts.scheme}://{host}{path}").rstrip("/")
    return host, prefix


async def _discover_sitemap(
    root_url: str, rules: robots.RobotRules, state: _CrawlState
) -> List[str]:
    """URLs from the site's sitemap(s), scope-filtered. [] → walk links."""
    candidates = list(rules.sitemaps)
    if not candidates:
        parts = urlparse(root_url)
        candidates = [f"{parts.scheme}://{parts.netloc}/sitemap.xml"]
    found: List[str] = []
    seen_maps: Set[str] = set()
    queue = candidates[:10]
    while queue and len(seen_maps) < 50:
        sm = queue.pop(0)
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        try:
            fetched = await net.safe_fetch(
                sm, timeout_ms=10000, max_bytes=5 * 1024 * 1024,
                accept="application/xml,text/xml,text/plain",
            )
        except Exception:  # noqa: BLE001 — no sitemap is normal
            continue
        body = fetched.body.decode("utf-8", errors="replace")
        entries = parse_sitemap(body)
        if _SITEMAP_INDEX_RE.search(body):
            queue.extend(loc for loc, _stamp in entries[:50])
            continue
        for url, stamp in entries:
            if _in_scope(state, url):
                found.append(url)
                if stamp:
                    # First sitemap that names a page wins; a page listed
                    # twice with two dates is a site bug, not a signal.
                    state.lastmod.setdefault(_normalize_url(url), stamp)
    # de-dup by the store key, preserving order
    out, seen = [], set()
    for url in found:
        key = _normalize_url(url)
        if key not in seen:
            seen.add(key)
            out.append(url)
    return out


async def _fetch_page(url: str) -> Tuple[str, extract.Extracted, List[str], str]:
    """→ (final_url, extracted, links, content_type). Raises on failure."""
    fetched = await net.safe_fetch(
        url,
        timeout_ms=settings.fetch_timeout_ms,
        max_bytes=settings.fetch_max_bytes,
        accept="text/html,application/pdf,text/plain",
    )
    loop = asyncio.get_running_loop()
    extracted, links = await loop.run_in_executor(
        _EXTRACT_POOL,
        _call_extract,
        fetched.content_type,
        fetched.body,
        fetched.url,
        getattr(fetched, "headers", None) or {},
    )
    return fetched.url, extracted, links, fetched.content_type


def _store(
    url: str,
    final_url: str,
    ext: extract.Extracted,
    content_type: str,
    links: Optional[List[str]] = None,
    origin: str = "crawl",
    conversation_id: str = "",
    user_id: Optional[int] = None,
    sitemap_lastmod: str = "",
) -> None:
    """Persist one crawled page with its trust class (V16, ADR-0001 D7).

    A crawl started because a member SHARED a link stores every page it
    reaches as origin 'share' — cited on its merits, never able to retire
    other evidence, never above neutral authority — and records the
    conversation (and user, when known) that introduced it. A research
    run's crawl is 'research'; an operator's manual crawl and the
    post-search expansion are 'crawl'.
    """
    meta = _provenance_of(ext, final_url or url, content_type, None)
    if meta.get("modified_at") is None and sitemap_lastmod:
        # K11: the page states no date of its own, so fall back to what its
        # SITE says about it in the sitemap. Only as a fallback — a page's
        # own statement always outranks a generator's, and a <lastmod> is
        # frequently just the last build.
        meta["modified_at"] = sitemap_lastmod_at(sitemap_lastmod)
    db.upsert_web_page(
        url_key=_normalize_url(url),
        url=url,
        canonical_url=final_url,
        title=ext.title or "",
        text=ext.text or "",
        content_type=content_type,
        fetch_status=200 if ext.text else 0,
        content_hash=hashlib.sha256((ext.text or "").encode("utf-8")).hexdigest(),
        links=links or [],
        origin=origin or "crawl",
        introduced_by_user_id=user_id,
        introduced_in_conversation_id=conversation_id or None,
        # V22: which extractor produced this text. Passing it is not optional
        # — a row left at 0 sits in the refresh worker's stale-extractor term
        # and is re-read on a schedule it can never satisfy.
        extract_version=extract.EXTRACT_VERSION,
        **meta,
    )


async def _crawl_site(
    root_url: str,
    emit: Optional[Emit],
    *,
    max_pages: int,
    max_seconds: float,
    quiet: bool = False,
    origin: str = "crawl",
    conversation_id: str = "",
    user_id: Optional[int] = None,
    crawl_id: Optional[int] = None,
) -> Tuple[_CrawlState, int, str]:
    """The frontier loop. → (state, pages_found, status).

    `origin`/`conversation_id`/`user_id` are stamped on every page stored
    (see _store): they decide how much a crawled page may do in the shared
    corpus and make it attributable.

    K12: passing `crawl_id` makes the frontier DURABLE — the work queue lives
    in `web_crawl_frontier`, keyed by this crawl's scope, so an interrupted
    run resumes at the page it stopped on. Both real entry points pass it
    (run_crawl_engine and the background queue's _run_queued); the
    opportunistic post-search expansion does not, and keeps the in-process
    queue (see _Frontier for why).
    """
    host, prefix = _scope_of(root_url)
    state = _CrawlState(scope_host=host, scope_prefix=prefix)
    frontier = _Frontier(prefix if crawl_id else None, crawl_id)

    async def status(text: str) -> None:
        if emit is not None and not quiet:
            await emit("status", {"text": text})

    rules = await robots.fetch_rules(root_url)
    if rules.declined:
        return state, 0, f"declined: {rules.decline_reason}"

    await status(f"Reading {host}'s sitemap…")
    # A campaign nobody has resumed in a week is abandoned: drop it before
    # seeding, or a site half-crawled long ago could never be crawled from
    # scratch again (every URL would already read 'visited').
    await frontier.retire_if_abandoned()
    resumed = (await frontier.counts())["pending"] if frontier.durable else 0
    sitemap_urls = await _discover_sitemap(root_url, rules, state)
    walk_links = len(sitemap_urls) < 10
    if walk_links:
        # Seeding the root on a RESUME is a no-op: the durable frontier keeps
        # it 'visited', so the run continues from the links the last one
        # harvested instead of re-walking the site from the front door.
        await frontier.add([(root_url, 0)])
    else:
        # The full sitemap (bounded for memory), NOT max_pages*2: pages past
        # that slice could never be reached by ANY number of resumes, which
        # silently contradicted the resume promise (review, 2026-08-30). The
        # quiet background expansion keeps the tight slice — its whole point
        # is smallness.
        cut = max_pages * 2 if quiet else 20_000
        await frontier.add([(u, 0) for u in sitemap_urls[:cut]])
    pages_found = len(sitemap_urls) if not walk_links else 0
    if resumed:
        log.info(
            "crawl: resuming %s with %d URL(s) still pending from an earlier run",
            prefix[:120], resumed,
        )

    # Politeness: few concurrent connections to ONE host, spaced out. The
    # crawl-delay from robots.txt wins when it is longer.
    delay = max(rules.crawl_delay_s, settings.web_crawl_delay_ms / 1000.0)
    sem = asyncio.Semaphore(settings.web_crawl_concurrency)
    started = time.monotonic()
    ttl = settings.web_page_ttl_s

    # Pages already stored fresh (by searches or an earlier crawl) are done —
    # this is what makes a re-crawl and a resume nearly free.
    #
    # → (links to enqueue, outcome). The outcome is what the durable frontier
    # records, and its three shapes matter:
    #   a name ("fetched", "store", …) — the page settled; done for good.
    #   ""                             — the FETCH failed; leave the URL
    #                                    pending so a later run retries it,
    #                                    up to _FRONTIER_MAX_FAILURES.
    #   None                           — this run already handled the URL;
    #                                    do not touch its row.
    async def process(url: str, depth: int) -> Tuple[List[Tuple[str, int]], Optional[str]]:
        key = _normalize_url(url)
        if key in state.visited:
            return [], None
        state.visited.add(key)
        if not rules.allows(url):
            return [], "blocked"
        stored = await db.run_in_thread(db.get_web_pages, [key])
        if stored:
            fetched_at = stored[0].get("fetched_at")
            age = time.time() - fetched_at.timestamp() if fetched_at else ttl + 1
            if age <= ttl and (stored[0].get("text") or "").strip():
                state.from_store += 1
                # The stored copy keeps the links its HTML pointed at (V10) —
                # without them, walk mode dead-ended on every warm page and a
                # "resume" re-read the store and stopped (review, 2026-08-30).
                if not walk_links or depth >= settings.web_crawl_max_depth:
                    return [], "store"
                return [
                    (l, depth + 1)
                    for l in (stored[0].get("links") or [])
                    if _in_scope(state, l) and _normalize_url(l) not in state.visited
                ], "store"
        async with sem:
            try:
                final_url, ext, links, ctype = await _fetch_page(url)
            except Exception:  # noqa: BLE001
                state.failed += 1
                return [], ""
            finally:
                await asyncio.sleep(delay)
        if not _in_scope(state, final_url) and _normalize_url(final_url) != key:
            # A redirect walked off the site — do not store or harvest it.
            state.failed += 1
            return [], "offsite"
        keep, reason = extract.page_quality(ext.text, final_url or url)
        if keep:
            await db.run_in_thread(
                _store, url, final_url, ext, ctype, links, origin,
                conversation_id, user_id, state.lastmod.get(key, ""),
            )
            state.fetched += 1
            outcome = "fetched"
        else:
            # K9: fetched, but not worth storing as a successful read. Say so
            # — out loud and with the reason — instead of writing a 4-char
            # row that later gets cited as if the page had been read.
            state.refused[reason] = state.refused.get(reason, 0) + 1
            state.failed += 1
            outcome = "refused"
            metrics.inc(
                "web_page_refused_total",
                "pages fetched but refused by the store-time quality gate",
                reason=reason,
            )
            log.info(
                "crawl: not storing %s — %s (%d chars extracted)",
                url[:160], reason, len((ext.text or "").strip()),
            )
        if not walk_links or depth >= settings.web_crawl_max_depth:
            return [], outcome
        return [
            (l, depth + 1)
            for l in links
            if _in_scope(state, l) and _normalize_url(l) not in state.visited
        ], outcome

    status_at = 0.0
    capped = False
    while True:
        # Take BEFORE checking the budget, so a run that has spent it leaves
        # the batch untouched and pending. Checking first would report
        # "capped" for a crawl whose last batch happened to exhaust both the
        # frontier and the budget — which used to read "done" and should.
        batch = await frontier.take(settings.web_crawl_concurrency * 2)
        if not batch:
            break
        # Only network fetches consume the page budget. Pages served fresh
        # from the store are free — otherwise a resume spent its whole budget
        # re-counting what the last run stored and stopped at the same spot
        # every time (review, 2026-08-30). Wall-clock still bounds the loop.
        if state.fetched >= max_pages or time.monotonic() - started > max_seconds:
            capped = True
            break
        results = await asyncio.gather(
            *(process(u, d) for u, d in batch), return_exceptions=True
        )
        settled: Dict[str, List[str]] = {}
        deferred: List[str] = []
        discovered: List[Tuple[str, int]] = []
        for (taken_url, _depth), result in zip(batch, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                # One page's DB hiccup fails that page, not the whole run —
                # plain gather() cancelled the siblings and aborted the crawl.
                # Transient by nature, so the URL is deferred, not retired.
                state.failed += 1
                log.debug("crawl page failed", exc_info=result)
                deferred.append(_normalize_url(taken_url))
                continue
            links, outcome = result
            discovered.extend(links)
            if outcome is None:
                continue
            if outcome:
                settled.setdefault(outcome, []).append(_normalize_url(taken_url))
            else:
                deferred.append(_normalize_url(taken_url))
        # Enqueue the new work BEFORE settling the batch that produced it: a
        # crash between the two then costs a re-read of pages already stored
        # (cheap, and the store TTL absorbs it) instead of losing links that
        # nothing would ever discover again.
        await frontier.add(discovered)
        await frontier.settle(settled)
        await frontier.defer(deferred)
        now = time.monotonic()
        if now - status_at > 2.0:
            # Counted on the status tick only. In walk mode the size of the
            # site is not known in advance, so "how many pages are there" IS
            # the frontier's size — but asking on every batch would be one
            # COUNT per six fetches for no extra accuracy in the message.
            status_at = now
            counts = await frontier.counts(visited_hint=len(state.visited))
            if walk_links:
                pages_found = max(pages_found, counts["total"])
            total = pages_found or (state.fetched + state.from_store + counts["pending"])
            await status(
                f"Crawling {host} — {state.fetched + state.from_store}/{total} pages…"
            )
    counts = await frontier.counts(visited_hint=len(state.visited))
    if walk_links:
        pages_found = max(pages_found, counts["total"])
    if not capped and counts["pending"] == 0:
        # The frontier is drained and nothing is owed a retry: the campaign is
        # finished, so clear it. Leaving a complete set of 'visited' rows
        # behind would make the NEXT crawl of this site a no-op. Deferred URLs
        # keep the campaign open — clearing them would lose the retry.
        await frontier.close()
    return state, pages_found or len(state.visited), "capped" if capped else "done"


async def _drain_index(emit: Optional[Emit], quiet: bool = False) -> int:
    """Embed everything the crawl stored. Bounded, with progress.

    Bails after two consecutive rounds with zero progress: index_pending never
    raises, so with the embedding service down the old loop spun its full 400
    rounds inside the user-facing request, re-reading the same queue (review,
    2026-08-30). Unindexed pages are not lost — every later search's indexing
    pass retries the same watermark.
    """
    total_chunks = 0
    stalled = 0
    for _round in range(400):  # safety bound ≫ any real crawl
        remaining = await db.run_in_thread(db.count_unindexed_web_pages)
        if remaining <= 0:
            break
        if emit is not None and not quiet:
            await emit("status", {"text": f"Indexing — {remaining} pages left…"})
        # repair_stale_chunks=False: this loop runs INSIDE a user's crawl and
        # its own progress check counts only unindexed pages. With the V24
        # chunker bump the queue also holds every page awaiting a re-chunk, so
        # a tail round could pull ~49 repair pages (~360 chunks, ~4.4 s of
        # embedding at the measured 12.2 ms/chunk) into a request that did not
        # ask for them. The refresh worker drains that backlog on its own time.
        wrote = await web_index.index_pending(limit=50, repair_stale_chunks=False)
        if wrote > 0:
            stalled = 0
            total_chunks += wrote
            continue
        stalled += 1
        if stalled >= 2:
            if emit is not None and not quiet:
                await emit(
                    "status",
                    {"text": "Indexing paused — it will finish in the background."},
                )
            break
    return total_chunks


async def run_crawl_engine(
    message: str,
    crawl_url: str,
    conversation_id: str,
    history: Sequence[dict],
    emit: Emit,
) -> str:
    """Crawl → store → embed → summarise. The repo engine's shape exactly."""
    host, prefix = _scope_of(crawl_url)
    crawl_id = await db.run_in_thread(
        db.create_web_crawl, conversation_id, crawl_url, prefix
    )
    await emit("status", {"text": f"Crawling {host}…"})
    state: Optional[_CrawlState] = None
    try:
        state, pages_found, status = await _crawl_site(
            crawl_url,
            emit,
            max_pages=settings.web_crawl_max_pages,
            max_seconds=settings.web_crawl_max_minutes * 60.0,
            origin="crawl",
            conversation_id=conversation_id,
            # K12: durable frontier. "Continue crawling" opens a NEW crawl row
            # for the same site, which is exactly why the frontier is keyed by
            # scope and not by this id — the resume finds the queue the capped
            # run left behind.
            crawl_id=crawl_id,
        )
        if status.startswith("declined"):
            await db.run_in_thread(
                db.finish_web_crawl, crawl_id, "failed", 0, 0, 0, 0, status
            )
            text = (
                f"I can't crawl {host}: its robots.txt could not be read, so I "
                "assume crawling is not welcome there. I can still read "
                "individual pages you paste, or search the web normally."
            )
            await emit("token", {"text": text})
            await emit("meta", {"route": "crawl"})
            return text

        chunks = await _drain_index(emit)
        await db.run_in_thread(
            db.finish_web_crawl,
            crawl_id,
            status,
            pages_found,
            state.fetched,
            state.from_store,
            state.failed,
            "",
        )
        stored = state.fetched + state.from_store
        lines = [
            f"Crawled **{host}** — {stored} pages are now stored and searchable "
            f"({state.fetched} fetched, {state.from_store} already fresh in the "
            f"store, {state.failed} unreadable; {chunks} text chunks indexed).",
        ]
        if status == "capped":
            lines.append(
                f"\nI stopped at the safety cap ({settings.web_crawl_max_pages} "
                f"pages / {settings.web_crawl_max_minutes} min). Say "
                "“continue crawling” to resume where it left off — "
                "already-stored pages are skipped, so resuming is cheap."
            )
        lines.append(
            "\nAsk me anything about it — I'll answer from the stored copy "
            "with citations."
        )
        text = "\n".join(lines)
        await emit("token", {"text": text})
        await emit(
            "meta",
            {
                "route": "crawl",
                "crawl": {
                    "root_url": crawl_url,
                    "host": host,
                    "status": status,
                    "pages_found": pages_found,
                    "pages_fetched": state.fetched,
                    "pages_from_store": state.from_store,
                    "pages_failed": state.failed,
                    "chunks_indexed": chunks,
                },
            },
        )
        return text
    except asyncio.CancelledError:
        # A closed tab or a stop click cancels this coroutine. Without this
        # the row stayed 'running' forever, and site Q&A (which only trusts
        # done/capped crawls) never activated (review, 2026-08-30). Partial
        # pages ARE stored — 'capped' is the honest status, and it resumes.
        partial = (state.fetched + state.from_store) if state else 0
        try:
            await asyncio.shield(
                db.run_in_thread(
                    db.finish_web_crawl,
                    crawl_id,
                    "capped" if partial > 0 else "failed",
                    partial,
                    state.fetched if state else 0,
                    state.from_store if state else 0,
                    state.failed if state else 0,
                    "cancelled mid-run",
                )
            )
        except Exception:  # noqa: BLE001 — cancellation still wins
            log.warning("could not mark cancelled crawl", exc_info=True)
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("crawl failed", exc_info=True)
        await db.run_in_thread(
            db.finish_web_crawl, crawl_id, "failed", 0, 0, 0, 0, str(exc)[:300]
        )
        text = f"The crawl of {host} failed ({exc}). Nothing partial was lost — pages already stored stay usable."
        await emit("token", {"text": text})
        await emit("meta", {"route": "crawl"})
        return text


# ---------------------------------------------------------------------------
# Site Q&A: follow-up questions in a conversation that crawled a site
# ---------------------------------------------------------------------------

_SITE_QA_SYSTEM = (
    "Answer using the numbered excerpts from {host}, a site the user asked "
    "you to index. Cite excerpts inline as [1], [2]. The excerpts are from a "
    "stored copy fetched on the dates shown; say so if the question needs "
    "newer information than a stored copy can give. If the excerpts do not "
    "cover the question, say what is missing rather than guessing."
)


#: How many chunks of ONE crawled page may ground a site answer.
#:
#: This path is dense-only. `web_memory.retrieve` runs a PostgreSQL
#: full-text half alongside the vector half, so when one oversized page
#: swamps the ANN budget the lexical candidates still drag other pages into
#: the ranking; site Q&A has nothing equivalent — `run_site_qa_engine` renders
#: whatever comes back straight into the prompt as excerpts [1..n]. So the
#: page-diversity guarantee is stated HERE at the call site rather than
#: inherited silently from the default: one excerpt per page, `top_k` pages.
#: A crawled site is exactly where the swamping is most likely (118 live pages
#: hold >= 36 chunks, and a crawl is the thing that stores pages that big).
_SITE_QA_CHUNKS_PER_PAGE = 1


async def site_hits_for(
    conversation_id: str, question: str, top_k: int = 6
) -> Tuple[List[dict], str]:
    """Relevant stored chunks from this conversation's crawled sites."""
    sites = await db.run_in_thread(db.get_conversation_crawl_sites, conversation_id)
    if not sites:
        return [], ""
    host = urlparse(sites[0]["root_url"]).hostname or ""
    hits = await web_index.retrieve(
        question,
        top_k=top_k,
        site_prefix=sites[0]["scope_prefix"],
        max_chunks_per_page=_SITE_QA_CHUNKS_PER_PAGE,
    )
    return hits, host


async def run_site_qa_engine(
    message: str,
    hits: List[dict],
    host: str,
    history: Sequence[dict],
    emit: Emit,
    effort: str = "think",
) -> str:
    """Stream an answer grounded in the crawled site's stored chunks."""
    blocks = []
    sources = []
    for n, hit in enumerate(hits, start=1):
        date = str(hit.get("fetched_at", ""))[:10]
        blocks.append(
            f"[{n}] {hit.get('title') or hit.get('url')} ({hit.get('url')}, "
            f"stored {date})\n{hit.get('text', '')}"
        )
        sources.append(
            {
                "n": n,
                "title": hit.get("title") or hit.get("url", ""),
                "url": hit.get("url", ""),
                "domain": urlparse(hit.get("url", "")).hostname or "",
            }
        )
    msgs = [
        {"role": "system", "content": _SITE_QA_SYSTEM.format(host=host)},
        *recent_turns(history, 4),
        {
            "role": "user",
            "content": "Excerpts:\n" + "\n\n".join(blocks) + f"\n\nQuestion: {message}",
        },
    ]
    parts: List[str] = []
    # The picker reaches this call — the same omission put a full thinking
    # pass in front of every search answer (77-82% of wall-clock, measured)
    # and every image answer before it. Third time is a pattern: never call
    # stream_chat_events on a user-facing route without the effort.
    async for kind, delta in llm.stream_chat_events(
        msgs, effort=llm.normalize_effort(effort), max_tokens=8000
    ):
        await emit(kind, {"text": delta})
        if kind == "token":
            parts.append(delta)
    await emit("meta", {"route": "crawl", "sources": sources})
    return "".join(parts)


# ---------------------------------------------------------------------------
# The background crawl queue (2026-09-03)
#
# A crawl used to be a foreground request only: "index this site" and wait
# in the chat for up to fifteen minutes. Sharing a URL, or finishing a
# research run, now ENQUEUES a bounded crawl of that site; the knowledge
# worker drains the queue one job at a time, quietly, and the pages land in
# the same global store every search reads from. The queue is a PostgreSQL
# row per job (db.web_crawls with status 'queued'), so a restart resumes
# rather than forgets, and every job carries its own caps.
# ---------------------------------------------------------------------------

_QUEUE_LOCK = asyncio.Lock()


async def enqueue_site_crawl(
    conversation_id: str,
    url: str,
    *,
    kind: str = "share",
    max_pages: Optional[int] = None,
    max_minutes: Optional[float] = None,
    priority: int = 0,
) -> Optional[int]:
    """Queue a bounded background crawl of the site `url` lives on.

    → the job id, or None when nothing was queued (feature off, bad URL, the
    scope is already queued/running, or it was crawled recently). Never
    raises: a queue failure must not cost the answer that triggered it.
    """
    if not settings.web_background_crawl_enabled or not url:
        return None
    try:
        parts = urlparse(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host, prefix = _scope_of(url)
    pages = int(max_pages or settings.web_share_crawl_max_pages)
    minutes = float(max_minutes or settings.web_share_crawl_max_minutes)
    try:
        job_id = await db.run_in_thread(
            db.enqueue_web_crawl,
            conversation_id or "",
            url,
            prefix,
            kind,
            pages,
            minutes,
            priority,
        )
    except Exception:  # noqa: BLE001 — enrichment, never the answer
        log.debug("could not enqueue a crawl for %s", host, exc_info=True)
        return None
    if job_id is None:
        log.info("crawl queue: %s already queued or crawled recently (%s)", host, kind)
        return None
    log.info(
        "crawl queue: job %d queued for %s (kind=%s, up to %d pages / %.0f min)",
        job_id, prefix, kind, pages, minutes,
    )
    try:
        from .. import web_worker

        web_worker.kick()
    except Exception:  # noqa: BLE001
        pass
    return job_id


async def _run_queued(job: dict) -> None:
    """One background job, start to finish. Marks the row whatever happens."""
    crawl_id = int(job["id"])
    root_url = job["root_url"]
    host, _prefix = _scope_of(root_url)
    max_pages = int(job.get("max_pages") or settings.web_share_crawl_max_pages)
    max_seconds = float(job.get("max_minutes") or settings.web_share_crawl_max_minutes) * 60.0
    started = time.monotonic()
    state: Optional[_CrawlState] = None
    try:
        kind = str(job.get("kind") or "")
        state, pages_found, status = await _crawl_site(
            root_url,
            None,
            max_pages=max_pages,
            max_seconds=max_seconds,
            quiet=True,
            # K12: a background job requeued by db.requeue_interrupted_web_crawls
            # comes back on the SAME row, and now to the same frontier too.
            crawl_id=crawl_id,
            # A shared link's crawl inherits the SHARE trust class for every
            # page it reaches; a research run's crawl is research material.
            origin="share" if kind == "share" else "research" if kind == "research" else "crawl",
            conversation_id=str(job.get("conversation_id") or ""),
        )
        if status.startswith("declined"):
            await db.run_in_thread(
                db.finish_web_crawl, crawl_id, "failed", 0, 0, 0, 0, status
            )
            log.info("background crawl[%d] %s declined: %s", crawl_id, host, status)
            return
        chunks = await _drain_index(None, quiet=True)
        await db.run_in_thread(
            db.finish_web_crawl,
            crawl_id,
            status,
            pages_found,
            state.fetched,
            state.from_store,
            state.failed,
            "",
        )
        log.info(
            "background crawl[%d] %s %s: %d fetched, %d from store, %d failed, "
            "%d chunks indexed in %.0fs (kind=%s)",
            crawl_id, host, status, state.fetched, state.from_store, state.failed,
            chunks, time.monotonic() - started, job.get("kind"),
        )
    except asyncio.CancelledError:
        # Shutdown mid-crawl: the pages already stored are real. The row goes
        # back to the queue at the next start (db.requeue_interrupted_web_crawls).
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("background crawl[%d] %s failed", crawl_id, host, exc_info=True)
        try:
            await db.run_in_thread(
                db.finish_web_crawl, crawl_id, "failed", 0,
                state.fetched if state else 0, state.from_store if state else 0,
                state.failed if state else 0, str(exc)[:300],
            )
        except Exception:  # noqa: BLE001
            pass


async def run_queued_crawls(max_jobs: int = 1) -> int:
    """Drain up to `max_jobs` queued crawls. Single-flight per process:
    two concurrent background crawls would double the politeness load on the
    same hosts and race the shared extraction worker. → jobs completed."""
    if not settings.web_background_crawl_enabled:
        return 0
    if _QUEUE_LOCK.locked():
        return 0
    done = 0
    async with _QUEUE_LOCK:
        for _ in range(max(1, int(max_jobs))):
            try:
                job = await db.run_in_thread(db.next_queued_web_crawl)
            except Exception:  # noqa: BLE001
                log.debug("could not read the crawl queue", exc_info=True)
                break
            if not job:
                break
            await _run_queued(job)
            done += 1
    return done


# ---------------------------------------------------------------------------
# Background expansion after a search (owner idea, 2026-08-30)
# ---------------------------------------------------------------------------


#: One expansion at a time. Concurrent searches each scheduling their own
#: expansion multiplied the politeness caps against the same hosts and raced
#: the per-crawl dedupe (review, 2026-08-30). Skipping is fine: expansion is
#: opportunistic warming, and the next idle search will run it again.
_EXPAND_LOCK = asyncio.Lock()


async def expand_search_domains(urls: List[str]) -> None:
    """Quietly deepen the sites a search just read, one hop, tightly capped.

    After the answer streams the machine is idle; following a few in-site
    links from the pages actually read makes the NEXT related question hit
    warm content. Caps keep it honest: a handful of pages per domain, few
    domains, robots respected, and never the open-ended crawler.
    """
    if not settings.web_expand_after_search or not urls:
        return
    if _EXPAND_LOCK.locked():
        return
    async with _EXPAND_LOCK:
        await _expand_search_domains_locked(urls)


async def _expand_search_domains_locked(urls: List[str]) -> None:
    try:
        by_host: dict = {}
        for u in urls:
            host = (urlparse(u).hostname or "").lower().removeprefix("www.")
            if host:
                by_host.setdefault(host, u)
        for root in list(by_host.values())[: settings.web_expand_max_domains]:
            try:
                await _crawl_site(
                    root,
                    None,
                    max_pages=settings.web_expand_pages_per_domain,
                    max_seconds=120.0,
                    quiet=True,
                )
            except Exception:  # noqa: BLE001
                continue
        await web_index.index_pending(limit=50, repair_stale_chunks=False)
    except Exception:  # noqa: BLE001 — background enrichment, never surfaces
        log.debug("search expansion skipped", exc_info=True)
