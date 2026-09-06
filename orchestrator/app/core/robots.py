"""robots.txt: rules for the site crawler, and for every other fetch path.

Written for the crawler (2026-08-30); extended to the retrieval paths on
2026-09-06 (finding K6) — the search-result read, the pasted-link read and the
refresh worker had been fetching third-party pages with no rules check and no
politeness delay. The bottom half of this file holds the per-host cache and
the fail-open gate those paths use; `fetch_rules` and `RobotRules` above are
unchanged and are still what `engines/crawl.py` calls directly.

Parsing is stdlib ``urllib.robotparser`` — but the FETCH goes through
``net.safe_fetch``, never ``RobotFileParser.read()``: read() would open the
URL itself, bypassing the SSRF guard (private-IP refusal, DNS-rebinding
check, size cap) and announcing Python-urllib's default User-Agent instead
of ours.

Status semantics follow RFC 9309: a 4xx robots.txt means "no rules — crawl
allowed"; a 5xx (or unreachable host) means the site could not state its
rules, so the polite reading is "assume disallowed" and the crawl declines.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Tuple
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from . import net

log = logging.getLogger(__name__)

#: The honest identity this crawler announces. A local personal assistant is
#: not a stealth scraper; sites that refuse bots get to refuse this one.
USER_AGENT = "TechSaraBot/1.0 (+local personal AI assistant)"

_SITEMAP_RE = re.compile(r"(?im)^\s*sitemap\s*:\s*(\S+)")

#: robots.txt files are small; anything bigger is not a rules file.
_MAX_ROBOTS_BYTES = 512 * 1024


@dataclass
class RobotRules:
    allowed_all: bool = False
    #: When the crawl must not proceed at all (5xx robots, unreachable host).
    declined: bool = False
    decline_reason: str = ""
    sitemaps: List[str] = field(default_factory=list)
    crawl_delay_s: float = 0.0
    _parser: RobotFileParser | None = None

    def allows(self, url: str) -> bool:
        if self.declined:
            return False
        if self.allowed_all or self._parser is None:
            return True
        try:
            return self._parser.can_fetch(USER_AGENT, url)
        except Exception:  # noqa: BLE001 — a broken rules file blocks nothing
            return True


async def fetch_rules(root_url: str) -> RobotRules:
    """Rules for the host of `root_url`, fetched safely, parsed with stdlib."""
    parts = urlparse(root_url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        fetched = await net.safe_fetch(
            robots_url,
            timeout_ms=8000,
            max_bytes=_MAX_ROBOTS_BYTES,
            accept="text/plain",
        )
    except net.FetchError as exc:
        status = getattr(exc, "status", None)
        if status is not None and 400 <= status < 500:
            # RFC 9309: no robots file = no restrictions.
            return RobotRules(allowed_all=True)
        return RobotRules(
            declined=True,
            decline_reason=f"robots.txt could not be read ({exc})",
        )
    except Exception as exc:  # noqa: BLE001 — includes UnsafeURLError
        return RobotRules(declined=True, decline_reason=str(exc))

    text = fetched.body.decode("utf-8", errors="replace")
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    delay = 0.0
    try:
        raw = parser.crawl_delay(USER_AGENT)
        if raw:
            delay = min(float(raw), 10.0)  # a 60 s delay is a "no", capped
    except Exception:  # noqa: BLE001
        delay = 0.0
    sitemaps = [m.group(1).strip() for m in _SITEMAP_RE.finditer(text)]
    return RobotRules(
        allowed_all=False,
        sitemaps=sitemaps[:10],
        crawl_delay_s=delay,
        _parser=parser,
    )


# ---------------------------------------------------------------------------
# Per-host rules cache + politeness, for the RETRIEVAL paths (finding K6)
# ---------------------------------------------------------------------------
#
# Until 2026-09-06 robots.txt was consulted by the site crawler ONLY. The
# search-result read, the pasted-link read and the refresh worker's re-read
# all fetched third-party pages with no rules check and no delay between
# them — and the V23 backfill is about to hand the refresh worker 1,602
# previously unreachable pages, i.e. ~2,300 fetches a day. That is the
# difference between a dormant defect and a daily impoliteness, which is why
# these three paths now go through here.
#
# WHY THIS IS A SEPARATE ENTRY POINT FROM `fetch_rules`/`RobotRules.allows`.
# The crawler and a retrieval read carry different risk, so they get
# different defaults on the ONE case where the site did not answer:
#
#   * the crawler walks a whole site nobody asked for, so an unreadable
#     robots.txt (5xx, DNS failure) means `declined=True` and it does not
#     crawl. That behaviour is unchanged; `engines/crawl.py` still calls
#     `fetch_rules` directly and still reads `declined`.
#
#   * a retrieval read is ONE page a person is waiting on, already chosen by
#     a search engine or pasted by hand. FAIL OPEN: if robots.txt cannot be
#     read we allow the fetch. Failing closed here would mean a single site
#     outage — or one flaky DNS answer — silently black-holing web retrieval
#     for that host, and an empty answer is indistinguishable from "nothing
#     exists", which is exactly the fabricated-negative failure this phase
#     exists to stop. An explicit `Disallow` is always obeyed; only the
#     ABSENCE of an answer is resolved in favour of fetching.
#
# The cache is what makes this affordable: one robots.txt per host per TTL,
# shared by every path, with a single-flight lock so sixteen concurrent
# search reads of the same host fetch it once rather than sixteen times.

#: How long a parsed robots.txt is reused. An hour is the common convention
#: and is short enough that a site that starts refusing us is obeyed the same
#: working session.
_RULES_TTL_S = 3600.0
#: A host whose robots.txt could not be read is remembered too — briefly — so
#: a dead host does not earn a robots fetch per page. Short, because the
#: cached verdict is "allow" and we want to notice the moment it can be read.
_RULES_ERROR_TTL_S = 300.0
#: Cache ceiling. Bounded so a crawl across many hosts cannot grow it without
#: limit; eviction is oldest-expiry-first and the cost of a miss is one fetch.
_MAX_HOSTS = 512
#: Longest politeness wait an interactive read will sit through. A site may
#: legitimately ask for 10 s between requests (`fetch_rules` caps it there);
#: waiting that long in front of a streaming answer is not an option, so the
#: caller is told the slot is not available and drops the source instead of
#: taking it impolitely.
DEFAULT_MAX_WAIT_S = 2.0

_rules_cache: Dict[str, Tuple[float, RobotRules]] = {}
_rules_locks: Dict[str, asyncio.Lock] = {}
#: host -> monotonic time at which the next request to it may start.
_next_slot: Dict[str, float] = {}


def _origin(url: str) -> str:
    """The cache key: scheme://host:port. robots.txt is per-origin."""
    parts = urlparse(url or "")
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}".lower()


def reset_cache() -> None:
    """Forget every cached ruleset, lock and politeness slot.

    For tests (asyncio locks bind to the loop that first awaits them, and the
    suite makes a new loop per test) and for an operator who has just changed
    a site's robots.txt and does not want to wait out the TTL.
    """
    _rules_cache.clear()
    _rules_locks.clear()
    _next_slot.clear()


def _evict_if_full() -> None:
    if len(_rules_cache) <= _MAX_HOSTS:
        return
    for key, _ in sorted(_rules_cache.items(), key=lambda kv: kv[1][0])[:64]:
        _rules_cache.pop(key, None)
        _rules_locks.pop(key, None)
        _next_slot.pop(key, None)


def _lock_for(origin: str) -> asyncio.Lock:
    lock = _rules_locks.get(origin)
    if lock is None:
        lock = asyncio.Lock()
        _rules_locks[origin] = lock
    return lock


async def rules_for(url: str) -> RobotRules:
    """Cached rules for the origin of `url`. Fetches robots.txt at most once
    per origin per TTL, even under concurrency.

    Never raises: a failure is cached briefly as a `declined` ruleset, which
    `allowed()` reads as "allow" (see the fail-open note above) and the
    crawler would read as "decline" — the SAME object, interpreted by the
    caller's own risk appetite.
    """
    origin = _origin(url)
    if not origin:
        return RobotRules(allowed_all=True)
    now = time.monotonic()
    hit = _rules_cache.get(origin)
    if hit and hit[0] > now:
        return hit[1]
    async with _lock_for(origin):
        # Someone may have filled it while we waited for the lock.
        hit = _rules_cache.get(origin)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        try:
            rules = await fetch_rules(origin + "/")
        except Exception as exc:  # noqa: BLE001 — fetch_rules is already
            # total, but a cache must never be the thing that raises.
            rules = RobotRules(declined=True, decline_reason=str(exc))
        ttl = _RULES_ERROR_TTL_S if rules.declined else _RULES_TTL_S
        _rules_cache[origin] = (time.monotonic() + ttl, rules)
        _evict_if_full()
        return rules


async def allowed(url: str) -> bool:
    """May a RETRIEVAL path fetch `url`? Fail-open on an unreadable robots.txt.

    An explicit `Disallow` for our User-Agent is always obeyed. A robots.txt
    that 5xx'd, timed out or whose host would not resolve is treated as "no
    rules stated" and the fetch is allowed — see the fail-open rationale at
    the top of this section; `tests/test_fetch_hygiene.py` pins it so nobody
    tightens it back into a retrieval black hole by accident.
    """
    rules = await rules_for(url)
    if rules.declined:
        return True
    return rules.allows(url)


async def reserve_slot(url: str, *, max_wait_s: float = DEFAULT_MAX_WAIT_S) -> bool:
    """Wait out this host's `Crawl-delay` before the caller fetches `url`.

    Returns True when the slot was taken (the caller may fetch now) and False
    when honouring the delay would mean waiting longer than `max_wait_s` — the
    caller then does NOT fetch. Skipping is the honest option: the search path
    already degrades a source it could not read to the provider's snippet and
    labels it as such, which is a better answer than either a stalled stream
    or a request the site asked us not to make yet.

    The next slot is reserved BEFORE the sleep and under the origin's lock, so
    sixteen concurrent readers of one host queue up behind each other instead
    of all reading the same "last fetched" value and firing at once.
    """
    origin = _origin(url)
    if not origin:
        return True
    rules = await rules_for(url)
    delay = float(rules.crawl_delay_s or 0.0)
    if delay <= 0:
        return True
    async with _lock_for(origin):
        now = time.monotonic()
        ready = _next_slot.get(origin, 0.0)
        wait = max(0.0, ready - now)
        if wait > max_wait_s:
            return False
        _next_slot[origin] = max(now, ready) + delay
    if wait > 0:
        await asyncio.sleep(wait)
    return True
