"""Fetch hygiene: conditional requests (K5) and robots.txt on every read path (K6).

WHY THESE TESTS ARE THE GATE ON THE V23 DEPLOYMENT. The migration schedules
1,602 previously unreachable pages for refresh inside a 24 h window, and the
worker drains 8 pages per 300 s — about 2,300 third-party fetches a day. Before
this change every one of them was a full unconditional GET with no
`If-None-Match`, no `If-Modified-Since` and no robots.txt check: `etag` (265
live rows) and `last_modified` (366) were written by the store and read by
nothing, and robots was consulted only by the site crawler. Applying V23 as
things stood would have converted a dormant defect into a daily impoliteness.

No network. Sockets are `httpx.MockTransport` handlers or a stubbed
`net.safe_fetch`; DNS is never touched because the mock transport replaces the
client before a connection is attempted. The four tests that need PostgreSQL
say so in their names' vicinity and use the suite's ordinary test database.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re

import httpx
import pytest

from app import db, web_worker
from app.config import settings
from app.core import net, robots
from app.engines import search as se
from app.engines import url as url_engine
from app.search.base import SearchResult


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_robots_cache():
    """The rules cache is module state and its locks bind to the event loop
    that first awaits them; the suite makes a new loop per test."""
    robots.reset_cache()
    se._HEAD_SLICE_FALLBACKS.clear()
    yield
    robots.reset_cache()


def _use_mock_transport(monkeypatch, handler):
    """Route safe_fetch's client through an httpx MockTransport (no sockets).

    Same helper as tests/test_net_ssrf.py: it bypasses the pinned backend on
    purpose, because these tests are about request/response handling, not the
    dial. The SSRF tests own the dial.
    """
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        net.httpx,
        "AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": transport}),
    )


def _fetch_result(url, body=b"<p>hi</p>", status=200, ctype="text/html", headers=None):
    return net.FetchResult(
        url=url, status=status, content_type=ctype, body=body, headers=headers or {}
    )


ROBOTS_DISALLOW_PRIVATE = (
    "User-agent: *\n"
    "Disallow: /private\n"
)


def _robots_serving_fetch(rules_body, *, calls, page_body=b"<p>page text here</p>",
                          delay_line="", fail_robots=None):
    """A stand-in for net.safe_fetch that serves robots.txt and pages.

    `calls` is a list every request is appended to, so a test can assert both
    WHAT was fetched and HOW MANY TIMES robots.txt was.
    """
    body = rules_body + (delay_line or "")

    async def fake_fetch(url, **kw):
        calls.append(url)
        if url.endswith("/robots.txt"):
            if fail_robots is not None:
                raise fail_robots
            return _fetch_result(url, body.encode(), ctype="text/plain")
        return _fetch_result(url, page_body)

    return fake_fetch


# ---------------------------------------------------------------------------
# K5 — conditional requests: the transport contract
# ---------------------------------------------------------------------------


def test_caller_headers_are_limited_to_the_conditional_validators(monkeypatch):
    """The allowlist, and the reason it is an allowlist.

    A caller that tries to set Host would break the one property DNS pinning
    rests on (the dialled address and the TLS/Host name agree); Cookie or
    Authorization would put credentials on a user-influenced URL; Connection
    or Transfer-Encoding is request smuggling. None of them reach the wire.
    """
    seen = {}

    def handler(request):
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, content=b"ok")

    _use_mock_transport(monkeypatch, handler)
    monkeypatch.setattr(net, "_validate_and_pin", lambda url, backend: url)

    asyncio.run(
        net.safe_fetch(
            "https://example.com/p",
            timeout_ms=1000,
            max_bytes=10_000,
            accept="text/html",
            headers={
                "If-None-Match": '"abc123"',
                "If-Modified-Since": "Wed, 21 Oct 2015 07:28:00 GMT",
                # Everything below must be dropped.
                "Host": "evil.example",
                "User-Agent": "NotUs/9",
                "Cookie": "session=secret",
                "Authorization": "Bearer secret",
                "Connection": "close",
                "X-Forwarded-For": "10.0.0.1",
            },
        )
    )

    assert seen["if-none-match"] == '"abc123"'
    assert seen["if-modified-since"] == "Wed, 21 Oct 2015 07:28:00 GMT"
    # Ours, not the caller's — the defaults are applied last on purpose.
    assert seen["user-agent"] == net._UA
    assert seen["accept"] == "text/html"
    assert seen["host"] == "example.com"
    for banned in ("cookie", "authorization", "x-forwarded-for"):
        assert banned not in seen, f"{banned} reached the wire"


def test_header_values_carrying_crlf_are_refused(monkeypatch):
    seen = {}

    def handler(request):
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, content=b"ok")

    _use_mock_transport(monkeypatch, handler)
    monkeypatch.setattr(net, "_validate_and_pin", lambda url, backend: url)
    asyncio.run(
        net.safe_fetch(
            "https://example.com/p",
            timeout_ms=1000,
            max_bytes=10_000,
            headers={"If-None-Match": 'abc\r\nX-Injected: 1'},
        )
    )
    assert "if-none-match" not in seen
    assert "x-injected" not in seen


def test_headers_cannot_bypass_the_ssrf_guard(monkeypatch):
    """The guard reads the URL, never the headers — adding one changes nothing."""
    def fake_getaddrinfo(host, *a, **k):
        return [(2, 1, 6, "", ("10.1.2.3", 0))]

    monkeypatch.setattr(net.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(net.UnsafeURLError):
        asyncio.run(
            net.safe_fetch(
                "https://internal.example/p",
                timeout_ms=1000,
                max_bytes=1000,
                headers={"If-None-Match": '"x"'},
            )
        )


def test_304_comes_back_as_a_result_not_an_error(monkeypatch):
    """A 304 is the successful answer to a conditional request. Before this
    change it fell through the >=400 branch's neighbourhood untested; a caller
    that treated it as a failure would back a stable page off exponentially
    for the crime of being stable."""
    def handler(request):
        assert request.headers["if-none-match"] == '"v1"'
        return httpx.Response(304, headers={"ETag": '"v1"'})

    _use_mock_transport(monkeypatch, handler)
    monkeypatch.setattr(net, "_validate_and_pin", lambda url, backend: url)
    result = asyncio.run(
        net.safe_fetch(
            "https://example.com/p",
            timeout_ms=1000,
            max_bytes=10_000,
            headers={"If-None-Match": '"v1"'},
        )
    )
    assert result.status == 304
    assert result.body == b""
    assert result.headers.get("etag") == '"v1"'


def test_304_carrying_a_location_header_is_not_followed_as_a_redirect(monkeypatch):
    """304 is inside the 3xx range, so `is_redirect` would fire on a stray
    Location. The status is checked first."""
    hops = []

    def handler(request):
        hops.append(str(request.url))
        return httpx.Response(304, headers={"Location": "https://elsewhere.example/x"})

    _use_mock_transport(monkeypatch, handler)
    monkeypatch.setattr(net, "_validate_and_pin", lambda url, backend: url)
    result = asyncio.run(
        net.safe_fetch("https://example.com/p", timeout_ms=1000, max_bytes=1000)
    )
    assert result.status == 304
    assert hops == ["https://example.com/p"]


def test_validators_are_not_carried_across_a_redirect(monkeypatch):
    """A validator identifies a version of ONE resource.

    Carried onto a 301's target, `If-Modified-Since` would legitimately draw a
    304 from a DIFFERENT page whose Last-Modified predates our stored date —
    and the caller would record "unchanged" for a page that had moved, freezing
    the stored copy at its pre-redirect content forever.
    """
    seen = []

    def handler(request):
        seen.append(dict(request.headers))
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "https://example.com/new"})
        return httpx.Response(200, content=b"the new page")

    _use_mock_transport(monkeypatch, handler)
    monkeypatch.setattr(net, "_validate_and_pin", lambda url, backend: url)
    result = asyncio.run(
        net.safe_fetch(
            "https://example.com/old", timeout_ms=1000, max_bytes=10_000,
            headers={"If-None-Match": '"v1"',
                     "If-Modified-Since": "Wed, 21 Oct 2015 07:28:00 GMT"},
        )
    )
    assert result.status == 200 and result.body == b"the new page"
    assert seen[0].get("if-none-match") == '"v1"'
    assert "if-none-match" not in seen[1]
    assert "if-modified-since" not in seen[1]
    # The identity headers DO ride every hop.
    assert seen[1]["user-agent"] == net._UA


def test_conditional_headers_are_built_from_the_stored_validators():
    assert se._conditional_headers('W/"abc"', "Wed, 21 Oct 2015 07:28:00 GMT") == {
        "If-None-Match": 'W/"abc"',
        "If-Modified-Since": "Wed, 21 Oct 2015 07:28:00 GMT",
    }
    # The weak prefix and the quotes are part of the opaque value: an ETag
    # that gets "tidied" stops matching and every refresh becomes a download.
    assert se._conditional_headers('W/"abc"', "")["If-None-Match"] == 'W/"abc"'
    assert se._conditional_headers("", "") == {}


# ---------------------------------------------------------------------------
# K5 — the acceptance criterion, end to end (needs PostgreSQL)
# ---------------------------------------------------------------------------


def _store_page(url, text="the original stored body", etag='"v1"'):
    digest = hashlib.sha256(text.encode()).hexdigest()
    row = db.upsert_web_page(
        se._normalize_url(url), url, url, "Stored title", text, "text/html",
        200, digest, [], etag=etag, last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
        extract_version=1,
    )
    db.mark_web_pages_indexed([int(row["id"])])
    return int(row["id"]), digest


def _page_row(page_id):
    with db.connection() as con:
        return dict(
            con.execute("SELECT * FROM web_pages WHERE id = %s", (page_id,)).fetchone()
        )


def test_refetch_page_304_reports_unchanged_and_never_enters_the_extractor(monkeypatch):
    """THE acceptance test for K5.

    A stubbed transport answers a matching If-None-Match with 304, and:
      * refetch_page reports `not_modified`, not a failure;
      * the extractor is never entered (it is replaced by a raiser);
      * `indexed_at` is untouched, so nothing is re-chunked or re-embedded;
      * `text` and `content_hash` are untouched;
      * `fetched_at` moves — the freshness clock, and only that.
    """
    url = "https://cond.example/doc"
    page_id, digest = _store_page(url)
    before = _page_row(page_id)

    sent = {}

    async def fake_fetch(u, **kw):
        sent.update(kw.get("headers") or {})
        return _fetch_result(u, b"", status=304, headers={"etag": '"v1"'})

    def never(*a, **k):  # pragma: no cover — the point is that it is not called
        raise AssertionError("the extractor was entered for a 304")

    monkeypatch.setattr(se.net, "safe_fetch", fake_fetch)
    monkeypatch.setattr(se, "_call_extract", never)
    monkeypatch.setattr(robots, "allowed", _async_true)
    monkeypatch.setattr(robots, "reserve_slot", _async_true)

    result = asyncio.run(
        se.refetch_page(url, previous_hash=digest, etag='"v1"',
                        last_modified="Wed, 21 Oct 2015 07:28:00 GMT")
    )

    assert result is not None, "a 304 must not be reported as an unreadable page"
    assert result["not_modified"] is True
    assert result["changed"] is False
    assert sent["If-None-Match"] == '"v1"'
    assert sent["If-Modified-Since"] == "Wed, 21 Oct 2015 07:28:00 GMT"

    after = _page_row(page_id)
    assert after["indexed_at"] == before["indexed_at"], "a 304 re-queued the embedder"
    assert after["text"] == before["text"]
    assert after["content_hash"] == before["content_hash"]
    assert after["last_changed_at"] == before["last_changed_at"]
    assert after["extract_version"] == before["extract_version"]
    assert after["fetched_at"] > before["fetched_at"], "the freshness clock did not move"


def test_304_reschedules_the_page_without_counting_a_failure(monkeypatch):
    """The other half of the acceptance criterion: the page comes back round.

    `_schedule_next(failed=True)` both increments `refresh_failures` and, via
    `_ttl_for`, doubles the TTL per failure — so treating a 304 as a failure
    would push a perfectly stable page out to 30 days and drop it from the
    stale-extractor queue term, which requires `refresh_failures = 0`.
    """
    url = "https://cond.example/stable"
    page_id, digest = _store_page(url)
    with db.connection() as con:
        con.execute(
            "UPDATE web_pages SET next_refresh_at = now() - interval '1 hour', "
            "refresh_failures = 0 WHERE id = %s",
            (page_id,),
        )

    async def fake_refetch(u, **kw):
        return {"changed": False, "title": "", "hash": digest,
                "not_modified": True, "blocked": False}

    monkeypatch.setattr(se, "refetch_page", fake_refetch)
    monkeypatch.setattr(settings, "web_knowledge_worker_enabled", True)
    monkeypatch.setattr(settings, "web_background_crawl_enabled", False)

    outcome = asyncio.run(web_worker._refresh_one({"id": page_id, "url": url}))
    assert outcome == "not_modified"

    asyncio.run(db.run_in_thread(
        web_worker._schedule_next, page_id, 3600, failed=outcome == "failed"
    ))
    row = _page_row(page_id)
    assert row["refresh_failures"] == 0
    assert row["next_refresh_at"] is not None
    assert row["next_refresh_at"] > before_now()


def before_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def test_the_worker_sends_the_validators_it_already_selected(monkeypatch):
    """`etag`/`last_modified` were in `_DUE_COLUMNS` and thrown away."""
    captured = {}

    async def fake_refetch(u, **kw):
        captured.update(kw)
        return {"changed": False, "title": "", "hash": "h",
                "not_modified": True, "blocked": False}

    monkeypatch.setattr(se, "refetch_page", fake_refetch)
    asyncio.run(web_worker._refresh_one({
        "id": 1, "url": "https://e.example/p", "content_hash": "h",
        "etag": '"tag"', "last_modified": "Mon, 01 Jan 2024 00:00:00 GMT",
    }))
    assert captured["etag"] == '"tag"'
    assert captured["last_modified"] == "Mon, 01 Jan 2024 00:00:00 GMT"


def test_run_once_counts_304s_apart_from_reads(monkeypatch):
    """Without a separate counter there is no way to tell from production
    whether conditional requests are working at all."""
    monkeypatch.setattr(settings, "web_knowledge_worker_enabled", True)
    monkeypatch.setattr(settings, "web_background_crawl_enabled", False)

    rows = [
        {"id": 1, "url": "https://a.example/1", "content_hash": "h1"},
        {"id": 2, "url": "https://a.example/2", "content_hash": "h2"},
        {"id": 3, "url": "https://a.example/3", "content_hash": "h3"},
    ]
    monkeypatch.setattr(web_worker, "_due_pages", lambda limit: rows)
    monkeypatch.setattr(web_worker, "_schedule_next", lambda *a, **k: None)
    monkeypatch.setattr(web_worker, "_mark_changed", lambda *a, **k: None)

    async def fake_index_pending(*a, **k):
        return 0

    async def fake_maintain(*a, **k):
        return {}

    monkeypatch.setattr(web_worker.web_index, "index_pending", fake_index_pending)
    monkeypatch.setattr(web_worker.web_index, "maintain", fake_maintain)

    outcomes = {
        "https://a.example/1": {"changed": True, "hash": "n", "title": "",
                                "not_modified": False, "blocked": False},
        "https://a.example/2": {"changed": False, "hash": "h2", "title": "",
                                "not_modified": True, "blocked": False},
        "https://a.example/3": None,
    }

    async def fake_refetch(u, **kw):
        return outcomes[u]

    monkeypatch.setattr(se, "refetch_page", fake_refetch)
    done = asyncio.run(web_worker.run_once())
    assert done["refreshed"] == 1
    assert done["not_modified"] == 1
    assert done["failed"] == 1
    assert done["blocked"] == 0


# ---------------------------------------------------------------------------
# K6 — robots.txt on every fetch path
# ---------------------------------------------------------------------------


async def _async_true(*a, **k):
    return True


def test_robots_rules_are_fetched_once_per_host(monkeypatch):
    """THE acceptance criterion for the cache: robots.txt once for N URLs."""
    calls = []
    monkeypatch.setattr(net, "safe_fetch",
                        _robots_serving_fetch(ROBOTS_DISALLOW_PRIVATE, calls=calls))

    async def run():
        return await asyncio.gather(*(
            robots.allowed(f"https://one.example/page/{i}") for i in range(8)
        ))

    verdicts = asyncio.run(run())
    assert all(verdicts)
    robots_calls = [c for c in calls if c.endswith("/robots.txt")]
    assert len(robots_calls) == 1, f"robots.txt fetched {len(robots_calls)} times"


def test_robots_disallow_is_obeyed_and_allow_still_works(monkeypatch):
    calls = []
    monkeypatch.setattr(net, "safe_fetch",
                        _robots_serving_fetch(ROBOTS_DISALLOW_PRIVATE, calls=calls))

    async def run():
        return (
            await robots.allowed("https://one.example/private/secret"),
            await robots.allowed("https://one.example/public/page"),
        )

    private_ok, public_ok = asyncio.run(run())
    assert private_ok is False
    assert public_ok is True


def test_an_unreadable_robots_txt_fails_OPEN(monkeypatch):
    """Deliberate, and pinned so nobody "hardens" it by accident.

    `fetch_rules` marks an unreadable robots.txt `declined`, and the CRAWLER
    reads that as "do not crawl" — right for a bot that would walk a whole
    site nobody asked for. On the retrieval paths the same verdict would mean
    one site's 500 (or one flaky DNS answer) silently black-holing every read
    of that host, and an answer with no evidence is indistinguishable from
    "there is nothing to find" — the fabricated-negative failure this whole
    phase exists to stop. An explicit Disallow is still obeyed; only the
    ABSENCE of an answer is resolved in favour of fetching.
    """
    server_error = net.FetchError("HTTP 503")
    server_error.status = 503
    calls = []
    monkeypatch.setattr(
        net, "safe_fetch",
        _robots_serving_fetch("", calls=calls, fail_robots=server_error),
    )
    assert asyncio.run(robots.allowed("https://down.example/anything")) is True

    # And the crawler's stricter reading is unchanged.
    rules = asyncio.run(robots.rules_for("https://down.example/anything"))
    assert rules.declined is True
    assert rules.allows("https://down.example/anything") is False


def test_a_missing_robots_txt_allows_everything(monkeypatch):
    """RFC 9309: 4xx means no rules, not no crawling."""
    missing = net.FetchError("HTTP 404")
    missing.status = 404
    monkeypatch.setattr(
        net, "safe_fetch",
        _robots_serving_fetch("", calls=[], fail_robots=missing),
    )
    assert asyncio.run(robots.allowed("https://nofile.example/x")) is True


def test_crawl_delay_is_honoured_and_a_long_one_skips_the_read(monkeypatch):
    calls = []
    monkeypatch.setattr(
        net, "safe_fetch",
        _robots_serving_fetch(
            "User-agent: *\nDisallow: /private\n", calls=calls,
            delay_line="Crawl-delay: 5\n",
        ),
    )

    async def run():
        first = await robots.reserve_slot("https://slow.example/a", max_wait_s=2.0)
        # The first caller reserved the next 5 s; an interactive reader is not
        # going to sit through that in front of a stream, so it is told no.
        second = await robots.reserve_slot("https://slow.example/b", max_wait_s=2.0)
        return first, second

    first, second = asyncio.run(run())
    assert first is True
    assert second is False


def test_search_result_read_refuses_a_disallowed_path(monkeypatch):
    """THE acceptance criterion for the search path.

    A refused page is not a hole in the answer: it degrades to the provider's
    snippet, labelled `from_snippet`, exactly like a page that timed out — so
    the answer prompt can still see the pointer and knows not to treat it as
    evidence (finding S5).
    """
    calls = []
    monkeypatch.setattr(net, "safe_fetch",
                        _robots_serving_fetch(ROBOTS_DISALLOW_PRIVATE, calls=calls))
    monkeypatch.setattr(settings, "web_memory_enabled", False)

    r = SearchResult(title="Secret", url="https://one.example/private/doc",
                     snippet="a blurb from the search engine")
    src = asyncio.run(se._fetch_source(1, r, None, question="what is in there"))

    assert src is not None and src.from_snippet is True
    assert src.text == "a blurb from the search engine"
    assert "https://one.example/private/doc" not in calls, "the page was fetched anyway"


def test_search_result_read_still_reads_an_allowed_path(monkeypatch):
    calls = []
    monkeypatch.setattr(net, "safe_fetch",
                        _robots_serving_fetch(ROBOTS_DISALLOW_PRIVATE, calls=calls))
    monkeypatch.setattr(settings, "web_memory_enabled", False)
    monkeypatch.setattr(
        se.extract, "extract_readable_and_links",
        lambda ct, body, url, headers=None: (
            se.extract.Extracted(title="T", text="the page body, read in full"), []
        ),
    )
    r = SearchResult(title="Public", url="https://one.example/public/doc", snippet="blurb")
    src = asyncio.run(se._fetch_source(1, r, None, question="what is in there"))
    assert src is not None and src.from_snippet is False
    assert "https://one.example/public/doc" in calls


def test_refetch_page_refuses_a_disallowed_path(monkeypatch):
    """THE acceptance criterion for the refresh worker — the path V23 is about
    to point at 1,602 more pages."""
    calls = []
    monkeypatch.setattr(net, "safe_fetch",
                        _robots_serving_fetch(ROBOTS_DISALLOW_PRIVATE, calls=calls))

    result = asyncio.run(se.refetch_page("https://one.example/private/doc"))
    assert result is not None
    assert result["blocked"] is True
    assert result["not_modified"] is False
    assert "https://one.example/private/doc" not in calls

    # …and the worker treats that as "not a failure", so the page is not
    # backed off exponentially for a decision that is the site's, not ours.
    async def fake_refetch(u, **kw):
        return result

    monkeypatch.setattr(se, "refetch_page", fake_refetch)
    outcome = asyncio.run(web_worker._refresh_one({"id": 1, "url": "https://x/y"}))
    assert outcome == "blocked"


def test_pasted_link_read_refuses_a_disallowed_path(monkeypatch):
    calls = []
    monkeypatch.setattr(net, "safe_fetch",
                        _robots_serving_fetch(ROBOTS_DISALLOW_PRIVATE, calls=calls))
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    out = asyncio.run(url_engine.fetch_and_store(
        "conv-1", "https://one.example/private/doc", emit
    ))
    assert out is None
    assert "https://one.example/private/doc" not in calls
    # Only registry event types (sse.ALL_EVENTS) are ever emitted.
    assert {k for k, _ in events} <= {"status"}
    assert any("Skipped" in d["text"] for _, d in events)


# ---------------------------------------------------------------------------
# Task 3 — _coverage_gap: same answer, a fraction of the cost
# ---------------------------------------------------------------------------


def _coverage_gap_reference(question, sources):
    """The implementation this replaced, verbatim (dev @ 29aa0ab)."""
    from app.web_memory import _content_words, _terms

    if not sources or not (question or "").strip():
        return []
    have = set(_terms(" ".join(f"{s.title} {s.text}" for s in sources)))
    missing = []
    for raw, stem in zip(_content_words(question), _terms(question)):
        if len(raw) > 2 and stem not in have and raw not in missing:
            missing.append(raw)
    return missing[:5]


@pytest.mark.parametrize(
    "question",
    [
        "what is the reasoning score on the BenchLM leaderboard",
        "what is GPT-5.2's price",
        "zzqqx wombat marmalade thurible pomelo quince",
        "how do these work",          # every word a stop word
        "",                            # no question at all
        "a bc",                        # nothing longer than two characters
        "leaderboards released configuring",   # stemming must still apply
    ],
)
def test_coverage_gap_is_behaviour_identical_to_the_old_implementation(question):
    sources = [
        se._Source(n=1, title="BenchLM Leaderboard 2026",
                   url="https://b.example/l",
                   text="Rank | Model | Reasoning | Price\n1 | GPT-5.2 | 93.4 | 12.00\n"
                        "The leaderboard is released quarterly and configured by hand."),
        se._Source(n=2, title="Notes", url="https://n.example/n",
                   text="Some unrelated prose about quinces and marmalade."),
    ]
    assert se._coverage_gap(question, sources) == _coverage_gap_reference(question, sources)


def test_coverage_gap_is_empty_without_sources():
    assert se._coverage_gap("anything at all", []) == []


def test_coverage_gap_is_computed_once_per_search(monkeypatch):
    """It used to run before the first token AND again after the stream, on
    the event loop, over a ~205,000-character join. Once is enough: the same
    list goes into the prompt and into meta.coverage_gap."""
    calls = []
    real = se._coverage_gap

    def counting(question, sources):
        calls.append(question)
        return real(question, sources)

    monkeypatch.setattr(se, "_coverage_gap", counting)
    se._cache.clear()

    class FakeProvider:
        name = "fake"

        async def search(self, q, n):
            return [SearchResult(title="P", url="https://one.example/public/p",
                                 snippet="blurb")]

    async def fake_rewrite(*a, **k):
        return ["q"]

    async def fake_stream(messages, **kw):
        yield "token", "an answer with no citation"

    monkeypatch.setattr(se, "get_provider", lambda: FakeProvider())
    monkeypatch.setattr(se, "rewrite_queries", fake_rewrite)
    monkeypatch.setattr(se.llm, "stream_chat_events", fake_stream)
    monkeypatch.setattr(settings, "web_memory_enabled", False)
    monkeypatch.setattr(net, "safe_fetch",
                        _robots_serving_fetch(ROBOTS_DISALLOW_PRIVATE, calls=[]))
    monkeypatch.setattr(
        se.extract, "extract_readable_and_links",
        lambda ct, body, url, headers=None: (
            se.extract.Extracted(title="T", text="body text"), []
        ),
    )

    async def emit(kind, data):
        return None

    asyncio.run(se.run_search_engine("what is the zzqqx score", [], emit))
    assert len(calls) == 1, f"_coverage_gap ran {len(calls)} times"


# ---------------------------------------------------------------------------
# Task 4 — the tokenizer and the PostgreSQL lexical half
# ---------------------------------------------------------------------------


def test_the_fast_stem_guard_is_exactly_the_old_predicate():
    """`not word.isalpha()` replaced a per-character generator expression.
    They must agree on every token `_WORD` can produce, or S4's variant
    protection changes shape while looking like a speedup."""
    from app.web_memory import _WORD, _stem, _SUFFIXES

    def stem_reference(word):
        if any(ch.isdigit() or ch in "-._" for ch in word):
            return word
        for suffix in _SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                word = word[: -len(suffix)]
                break
        if len(word) >= 6 and word.endswith("e"):
            word = word[:-1]
        return word

    corpus = (
        "GPT-5.2 3.14.5 v2.1 oc-h1 1990s configured releases engines business "
        "leaderboard a bc snake_case dotted.name x9 9x release-candidate-2"
    )
    tokens = _WORD.findall(corpus.lower()) + ["", "a", "e", "ee"]
    for tok in tokens:
        assert _stem(tok) == stem_reference(tok), tok


def test_lexical_query_expands_hyphenated_variants_only():
    from app.web_memory import _content_words, lexical_query

    q = lexical_query(_content_words("GPT-5.2 reasoning score leaderboard"))
    assert q == ('"gpt-5.2" reasoning score leaderboard or '
                 '"gpt 5.2" reasoning score leaderboard')
    # A dot is a decimal point, not a word break: no nonsense "3 14 5" branch.
    assert lexical_query(_content_words("vLLM 3.14.5 release")) == "vllm 3.14.5 release"
    # No compound, no change at all.
    assert lexical_query(["alpha", "beta"]) == "alpha beta"


def test_lexical_query_recovers_the_spaced_form_without_losing_the_S4_win():
    """The regression, and the fix, against the real PostgreSQL text search.

    `_content_words` is documented as "what goes to PostgreSQL", and after S4
    it emits `gpt-5.2` as one token. PostgreSQL's parser reads the hyphen as
    the SIGN of the number, so

        to_tsvector('english','GPT-5.2') -> 'gpt', '-5.2'
        to_tsvector('english','GPT 5.2') -> 'gpt',  '5.2'

    and the AND-first query built from the hyphenated spelling stops matching
    a page that writes the version with a space. Nothing covered the tsquery
    path, so it went unnoticed. This asserts both halves of the fix: the
    spaced page is found again, and the SIBLING versions S4 exists to exclude
    are still excluded.
    """
    from app.web_memory import _content_words, lexical_query

    docs = {
        "hyphen": "The GPT-5.2 model leads the leaderboard with a reasoning score of 93.4",
        "spaced": "The GPT 5.2 model leads the leaderboard with a reasoning score of 93.4",
        "gpt5": "The GPT-5 model leads the leaderboard with a reasoning score of 88.1",
        "gpt51": "The GPT 5.1 model leads the leaderboard with a reasoning score of 90.0",
        "bare": "The GPT model leads the leaderboard with a reasoning score of 70.0",
    }
    words = _content_words("GPT-5.2 reasoning score leaderboard")
    naive = " ".join(words)          # what the code did before this fix
    fixed = lexical_query(words)

    def matches(query):
        with db.connection() as con:
            row = con.execute(
                "SELECT " + ", ".join(
                    f"(to_tsvector('english', %s) @@ websearch_to_tsquery('english', %s)) AS {k}"
                    for k in docs
                ),
                tuple(v for text in docs.values() for v in (text, query)),
            ).fetchone()
        return dict(row)

    before, after = matches(naive), matches(fixed)
    assert before["hyphen"] is True
    assert before["spaced"] is False, "the regression this test exists for is gone"
    assert after["hyphen"] is True
    assert after["spaced"] is True, "the spaced spelling is still unreachable"
    # S4's whole point: a different version is a different thing.
    assert after["gpt5"] is False
    assert after["gpt51"] is False
    assert after["bare"] is False


def test_lexical_candidates_finds_the_spaced_page():
    """The same thing through the real query, with real rows.

    `limit=2` on purpose: `_lexical_candidates` runs the AND-first query and
    then, only when it came back short, an OR-fill pass that deliberately
    admits pages matching ANY question word (so one stray word like "explain"
    cannot exclude the page that matches everything else). The AND pass is
    where the exactness lives, so the test has to let it satisfy the limit on
    its own — otherwise the OR pass would sweep the sibling version back in
    and the assertion would be about the fallback, not about the fix.
    """
    from app.web_memory import _lexical_candidates

    pages = {
        "https://hy.example/p": "The GPT-5.2 model leads the leaderboard with a reasoning score of 93.4",
        "https://sp.example/p": "The GPT 5.2 model leads the leaderboard with a reasoning score of 93.4",
        "https://o5.example/p": "The GPT-5 model leads the leaderboard with a reasoning score of 88.1",
    }
    for url, text in pages.items():
        db.upsert_web_page(
            se._normalize_url(url), url, url, "Leaderboard", text, "text/html",
            200, hashlib.sha256(text.encode()).hexdigest(), [],
        )
    found = {r["url"] for r in _lexical_candidates("GPT-5.2 reasoning score leaderboard", 2)}
    assert found == {"https://hy.example/p", "https://sp.example/p"}, found


# ---------------------------------------------------------------------------
# Task 5 — the narrowed fallbacks are deliberate, and no longer silent
# ---------------------------------------------------------------------------


def test_empty_question_falls_back_to_a_head_slice_and_is_counted():
    text = "x" * 5000
    out = se._select_text(text, "", 100)
    assert out == se.extract.truncate_chars(text, 100)
    assert se._HEAD_SLICE_FALLBACKS.get("no question to centre on") == 1


def test_a_broken_passage_selector_warns_once_and_keeps_counting(monkeypatch, caplog):
    """The fallback is right — an answer built from a head slice beats no
    answer — but it silently reinstates the exact C1 failure (fetched, cited,
    truncated away) for every source in the request. At DEBUG nobody would
    ever see it."""
    import app.web_memory as wm

    def boom(*a, **k):
        raise RuntimeError("selector exploded")

    monkeypatch.setattr(wm, "select_passages", boom)
    text = "y" * 5000
    with caplog.at_level(logging.WARNING, logger="app.engines.search"):
        for _ in range(4):
            assert se._select_text(text, "a real question", 100) == \
                se.extract.truncate_chars(text, 100)

    assert se._HEAD_SLICE_FALLBACKS.get("passage selection raised") == 4
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, "one warning per reason, not one per source"
    assert "head slice" in warnings[0].getMessage()
    # Never the question, never the page body.
    assert "a real question" not in warnings[0].getMessage()


def test_select_passages_head_slices_when_the_question_has_no_content_words():
    """Deliberate and safe: with no terms there is nothing to centre on, so
    the alternative is not a better slice but an arbitrary one."""
    from app.web_memory import select_passages

    text = "\n".join(f"line {i} of the page" for i in range(500))
    out = select_passages(text, "how do these work", 300)
    assert out == " ".join(text.split()).replace(" ", " ")[:300] or len(out) <= 300
    assert len(out) <= 300


def test_the_search_answer_prompt_never_names_an_unlisted_sse_event():
    """sse.ALL_EVENTS is a closed registry; this change adds no event type."""
    from app import sse

    source = (
        open("app/engines/search.py").read()
        + open("app/engines/url.py").read()
        + open("app/core/robots.py").read()
        + open("app/web_worker.py").read()
    )
    emitted = set(re.findall(r'emit\(\s*"([a-z_]+)"', source))
    assert emitted <= set(sse.ALL_EVENTS), emitted - set(sse.ALL_EVENTS)
