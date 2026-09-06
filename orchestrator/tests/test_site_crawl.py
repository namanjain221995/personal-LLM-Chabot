"""V9 site crawler: scoped, polite, resumable — and retrieval that stays sane.

Measured grounding (2026-08-30): docs.vllm.ai publishes a flat 2,451-URL
sitemap; link harvesting costs 41 ms on a 727 KB page; without a distance
floor and per-URL grouping, one crawled site fills every weak query's memory
slots and top-6 collapses to one page. The crawler ships WITH those fixes.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import db, web_index
from app.config import settings
from app.core import robots
from app.core.extract import extract_readable_and_links
from app.engines import crawl
from app.engines.search import _normalize_url


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expect",
    [
        ("index this whole site https://docs.example.ai/en/latest/", "https://docs.example.ai/en/latest/"),
        ("crawl https://docs.example.ai please", "https://docs.example.ai"),
        ("can you scrape all the pages of https://blog.example.com ?", "https://blog.example.com"),
        ("https://docs.example.ai/page — index the entire site", "https://docs.example.ai/page"),
        # A bare URL is the single-page engine, never a crawl.
        ("https://docs.example.ai/en/latest/", None),
        ("what does https://docs.example.ai say about tools?", None),
        ("index my salesforce data", None),
    ],
)
def test_detect_crawl(text, expect):
    assert crawl.detect_crawl(text) == expect


# ---------------------------------------------------------------------------
# Scope: same site + path prefix, junk extensions skipped
# ---------------------------------------------------------------------------


def test_scope_keeps_the_crawl_on_the_site():
    host, prefix = crawl._scope_of("https://docs.example.ai/en/latest/")
    state = crawl._CrawlState(scope_host=host, scope_prefix=prefix)
    assert crawl._in_scope(state, "https://docs.example.ai/en/latest/api/config/")
    assert crawl._in_scope(state, "https://www.docs.example.ai/en/latest/guide")
    assert not crawl._in_scope(state, "https://other.example.ai/en/latest/")
    assert not crawl._in_scope(state, "https://docs.example.ai/de/stable/")  # off-prefix
    assert not crawl._in_scope(state, "https://docs.example.ai/en/latest/release.zip")


def test_scope_of_a_page_url_uses_its_directory():
    host, prefix = crawl._scope_of("https://docs.example.ai/en/latest/index.html")
    assert host == "docs.example.ai"
    assert prefix.endswith("/en/latest")


# ---------------------------------------------------------------------------
# Link harvesting (combined pass)
# ---------------------------------------------------------------------------


def test_extract_readable_and_links_harvests_absolute_defragged():
    html = (
        "<html><head><title>T</title><base href='https://x.example/sub/'></head>"
        "<body><p>" + "content " * 60 + "</p>"
        "<a href='a.html#sec'>a</a>"
        "<a href='/root.html'>r</a>"
        "<a href='https://other.example/x'>o</a>"
        "<a href='mailto:x@y.z'>m</a>"
        "<a href='javascript:void(0)'>j</a></body></html>"
    ).encode()
    ext, links = extract_readable_and_links("text/html", html, "https://x.example/page")
    assert "https://x.example/sub/a.html" in links      # base-href honoured, defragged
    assert "https://x.example/root.html" in links
    assert "https://other.example/x" in links           # scope filtering is the CALLER's job
    assert all(not l.startswith(("mailto:", "javascript:")) for l in links)
    assert all("#" not in l for l in links)


def test_non_html_pages_yield_no_links():
    # Only HTML is harvested; text/plain (and PDFs) walk no links. A malformed
    # PDF still raises from extract_readable itself — unchanged behaviour.
    ext, links = extract_readable_and_links(
        "text/plain", b"just words " * 30, "https://x.example/notes.txt"
    )
    assert links == []
    assert "just words" in ext.text


# ---------------------------------------------------------------------------
# robots.txt semantics (RFC 9309)
# ---------------------------------------------------------------------------


def _fetch_error(status=None):
    from app.core.net import FetchError

    err = FetchError(f"HTTP {status}" if status else "boom")
    err.status = status
    return err


def test_robots_404_means_allowed(monkeypatch):
    async def fake_fetch(url, **kw):
        raise _fetch_error(404)

    monkeypatch.setattr(robots.net, "safe_fetch", fake_fetch)
    rules = asyncio.run(robots.fetch_rules("https://x.example/docs/"))
    assert rules.allowed_all and not rules.declined
    assert rules.allows("https://x.example/anything")


def test_robots_5xx_declines_the_crawl(monkeypatch):
    async def fake_fetch(url, **kw):
        raise _fetch_error(503)

    monkeypatch.setattr(robots.net, "safe_fetch", fake_fetch)
    rules = asyncio.run(robots.fetch_rules("https://x.example/docs/"))
    assert rules.declined
    assert not rules.allows("https://x.example/anything")


def test_robots_disallow_and_sitemap_are_read(monkeypatch):
    body = (
        "User-agent: *\nDisallow: /private/\nCrawl-delay: 2\n"
        "Sitemap: https://x.example/sitemap.xml\n"
    ).encode()

    async def fake_fetch(url, **kw):
        return SimpleNamespace(url=url, body=body, content_type="text/plain")

    monkeypatch.setattr(robots.net, "safe_fetch", fake_fetch)
    rules = asyncio.run(robots.fetch_rules("https://x.example/docs/"))
    assert rules.sitemaps == ["https://x.example/sitemap.xml"]
    assert rules.crawl_delay_s == 2.0
    assert rules.allows("https://x.example/docs/page")
    assert not rules.allows("https://x.example/private/secret")


# ---------------------------------------------------------------------------
# Sitemap discovery: flat + nested-index, scope-filtered, deduped
# ---------------------------------------------------------------------------


def test_sitemap_discovery(monkeypatch):
    index = b"<sitemapindex><sitemap><loc>https://x.example/sm1.xml</loc></sitemap></sitemapindex>"
    leaf = (
        b"<urlset>"
        b"<url><loc>https://x.example/docs/a</loc></url>"
        b"<url><loc>https://x.example/docs/a/</loc></url>"      # dedup with the above
        b"<url><loc>https://x.example/other/skip</loc></url>"   # off-scope
        b"<url><loc>https://x.example/docs/b</loc></url>"
        b"</urlset>"
    )

    async def fake_fetch(url, **kw):
        body = index if url.endswith("sitemap.xml") else leaf
        return SimpleNamespace(url=url, body=body, content_type="text/xml")

    monkeypatch.setattr(crawl.net, "safe_fetch", fake_fetch)
    host, prefix = crawl._scope_of("https://x.example/docs/")
    state = crawl._CrawlState(scope_host=host, scope_prefix=prefix)
    rules = robots.RobotRules(allowed_all=True, sitemaps=["https://x.example/sitemap.xml"])
    urls = asyncio.run(crawl._discover_sitemap("https://x.example/docs/", rules, state))
    assert urls == ["https://x.example/docs/a", "https://x.example/docs/b"]


# ---------------------------------------------------------------------------
# Retrieval fixes: floor, per-URL grouping, site filter plumbing
# ---------------------------------------------------------------------------


def test_retrieve_groups_per_url_and_applies_the_floor(monkeypatch):
    monkeypatch.setattr(settings, "web_memory_enabled", True)

    async def fake_embed(texts, **kw):
        return [[0.0] * 4]

    hits = [
        {"url": "https://a.example/p", "title": "A", "text": "c1", "fetched_at": "d", "_distance": 0.40},
        {"url": "https://a.example/p", "title": "A", "text": "c2", "fetched_at": "d", "_distance": 0.45},
        {"url": "https://a.example/p", "title": "A", "text": "c3", "fetched_at": "d", "_distance": 0.50},
        {"url": "https://b.example/q", "title": "B", "text": "c4", "fetched_at": "d", "_distance": 0.60},
        {"url": "https://junk.example/z", "title": "J", "text": "c5", "fetched_at": "d", "_distance": 1.30},
    ]

    monkeypatch.setattr(web_index.llm, "embed_texts", fake_embed)
    monkeypatch.setattr(
        web_index, "_open", lambda create_dim=None: (None, _FakeTable(hits), SimpleNamespace(dimension=4))
    )
    monkeypatch.setattr(web_index, "validate_query_dimension", lambda *a, **k: None)
    out = asyncio.run(web_index.retrieve("q", top_k=3))
    # one chunk per URL, junk beyond the floor dropped
    assert [o["url"] for o in out] == ["https://a.example/p", "https://b.example/q"]
    assert out[0]["text"] == "c1"  # the BEST chunk of the page, not three copies


class _FakeTable:
    def __init__(self, hits):
        self._hits = hits
        self.where_clause = None

    def search(self, vector):
        return self

    def limit(self, n):
        return self

    def where(self, clause):
        self.where_clause = clause
        return self

    def to_list(self):
        return self._hits


def test_retrieve_site_prefix_becomes_a_where_clause(monkeypatch):
    monkeypatch.setattr(settings, "web_memory_enabled", True)

    async def fake_embed(texts, **kw):
        return [[0.0] * 4]

    table = _FakeTable([])
    monkeypatch.setattr(web_index.llm, "embed_texts", fake_embed)
    monkeypatch.setattr(web_index, "_open", lambda create_dim=None: (None, table, SimpleNamespace(dimension=4)))
    monkeypatch.setattr(web_index, "validate_query_dimension", lambda *a, **k: None)
    asyncio.run(web_index.retrieve("q", top_k=3, site_prefix="docs.example.ai/en/latest"))
    assert "docs.example.ai/en/latest" in (table.where_clause or "")


# ---------------------------------------------------------------------------
# The crawl run record
# ---------------------------------------------------------------------------


def test_crawl_record_lifecycle():
    cid = db.create_web_crawl("conv-crawl-1", "https://x.example/docs/", "x.example/docs")
    db.finish_web_crawl(cid, "done", 40, 35, 3, 2)
    sites = db.get_conversation_crawl_sites("conv-crawl-1")
    assert len(sites) == 1
    assert sites[0]["scope_prefix"] == "x.example/docs"
    assert sites[0]["pages_fetched"] == 35
    # a failed crawl is not offered for follow-up Q&A
    cid2 = db.create_web_crawl("conv-crawl-2", "https://y.example/", "y.example")
    db.finish_web_crawl(cid2, "failed", 0, 0, 0, 0, "boom")
    assert db.get_conversation_crawl_sites("conv-crawl-2") == []


def test_site_qa_honours_the_effort(monkeypatch):
    """The third instance of the same bug class: a user-facing route calling
    stream_chat_events without the effort runs a full thinking pass whatever
    the picker says (vision 2026-08-28, search 2026-08-30, and this)."""
    from app import llm

    rec = {}

    def fake_stream(messages, **kw):
        rec.update(kw)

        async def gen():
            yield "token", "ok"

        return gen()

    async def collect(kind, data):
        return None

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    hits = [{"url": "https://x.example/a", "title": "A", "text": "t", "fetched_at": "2026-08-30"}]
    asyncio.run(crawl.run_site_qa_engine("q", hits, "x.example", [], collect, effort="fast"))
    assert rec.get("effort") == "fast"


# ---------------------------------------------------------------------------
# Review round (2026-08-30): intent precision, resume, budget, stall, cancel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expect",
    [
        # Intent words INSIDE a URL are addresses, not requests.
        ("what does https://docs.example.ai/index.html say", None),
        ("read https://a.example/scrape-tips for me", None),
        # "index" as an everyday noun does not start a thousand-page crawl.
        ("compare the index performance of https://a.example vs local", None),
        # …but as a verb with an object it still does.
        ("index https://docs.example.ai", "https://docs.example.ai"),
        ("continue crawling https://docs.example.ai", "https://docs.example.ai"),
    ],
)
def test_detect_crawl_ignores_urlish_and_noun_index(text, expect):
    assert crawl.detect_crawl(text) == expect


@pytest.mark.parametrize(
    "text, expect",
    [
        ("continue crawling", True),
        ("please resume the crawl", True),
        ("keep indexing the site", True),
        ("crawl the rest", True),
        ("continue", False),
        ("resume our discussion about pricing", False),
        # With a URL, detect_crawl owns the message instead.
        ("continue crawling https://x.example", False),
    ],
)
def test_detect_resume(text, expect):
    assert crawl.detect_resume(text) == expect


from app.core import extract  # noqa: E402 — used by the crawl fakes below


def _allow_all_rules():
    return robots.RobotRules(allowed_all=True)


def _fresh_row(key, links=()):
    from datetime import datetime, timezone

    return {
        "url_key": key,
        "url": f"https://{key}",
        "title": "t",
        "text": "stored body " * 30,
        "fetched_at": datetime.now(timezone.utc),
        "links": list(links),
    }


def _crawl_env(monkeypatch, stored_keys, sitemap, fetch_log, stored_links=None):
    """Wire _crawl_site's collaborators: canned robots/sitemap/store/fetch."""
    async def fake_rules(url):
        return _allow_all_rules()

    async def fake_sitemap(root, rules, state):
        return list(sitemap)

    async def fake_run_in_thread(func, *args):
        if func is crawl.db.get_web_pages:
            key = args[0][0]
            if key in stored_keys:
                return [_fresh_row(key, (stored_links or {}).get(key, ()))]
            return []
        return None  # _store and friends: recorded via fetch_log only

    async def fake_fetch_page(url):
        fetch_log.append(url)
        return url, extract.Extracted(title="T", text="fetched body " * 30), [], "text/html"

    monkeypatch.setattr(crawl.robots, "fetch_rules", fake_rules)
    monkeypatch.setattr(crawl, "_discover_sitemap", fake_sitemap)
    monkeypatch.setattr(crawl.db, "run_in_thread", fake_run_in_thread)
    monkeypatch.setattr(crawl, "_fetch_page", fake_fetch_page)
    monkeypatch.setattr(crawl.settings, "web_crawl_delay_ms", 0)


def test_stored_pages_do_not_consume_the_fetch_budget(monkeypatch):
    # A resume used to spend its whole max_pages budget re-counting pages the
    # last run stored, stopping at the same spot forever.
    sitemap = [f"https://x.example/docs/p{i}" for i in range(12)]
    stored = {crawl._normalize_url(u) for u in sitemap[:10]}
    fetched: list = []
    _crawl_env(monkeypatch, stored, sitemap, fetched)
    state, found, status = asyncio.run(
        crawl._crawl_site(
            "https://x.example/docs/", None, max_pages=2, max_seconds=30.0
        )
    )
    assert state.from_store == 10
    assert state.fetched == 2 and len(fetched) == 2
    assert status == "done"  # the frontier finished; nothing was silently cut


def test_from_store_pages_feed_walk_mode_links(monkeypatch):
    # Walk mode: the root is fresh in the store — its STORED links (V10) must
    # keep the walk alive instead of dead-ending the crawl at one page.
    root = "https://y.example/docs/"
    root_key = crawl._normalize_url(root)
    kids = ["https://y.example/docs/a", "https://y.example/docs/b"]
    fetched: list = []
    _crawl_env(
        monkeypatch, {root_key}, sitemap=[], fetch_log=fetched,
        stored_links={root_key: kids},
    )
    state, found, status = asyncio.run(
        crawl._crawl_site(root, None, max_pages=10, max_seconds=30.0)
    )
    assert sorted(fetched) == sorted(kids)
    assert state.from_store == 1 and state.fetched == 2


def test_drain_index_bails_when_nothing_progresses(monkeypatch):
    # Embedding down: index_pending returns 0 forever. The old loop spun its
    # full 400 rounds inside the user-facing request.
    events: list = []

    async def emit(kind, payload):
        events.append(payload.get("text", ""))

    async def fake_run_in_thread(func, *args):
        return 5  # always five pages left

    # The stub mirrors the real signature, and records the flag: this loop
    # runs inside a user's request, so it must NOT pull the V24 re-chunk
    # backlog in with it (its own progress check counts unindexed pages only,
    # so repair work would look like no progress AND cost the user seconds of
    # embedding they did not ask for).
    calls: list = []

    async def fake_index_pending(limit=20, page_ids=None, repair_stale_chunks=True):
        calls.append(repair_stale_chunks)
        return 0

    monkeypatch.setattr(crawl.db, "run_in_thread", fake_run_in_thread)
    monkeypatch.setattr(crawl.web_index, "index_pending", fake_index_pending)
    chunks = asyncio.run(crawl._drain_index(emit))
    assert chunks == 0
    assert any("background" in e for e in events)
    assert len(events) <= 4  # two stalled rounds, not four hundred
    assert calls and not any(calls), (
        "a user-facing crawl must not drain the stale-chunk repair queue"
    )


def test_cancelled_crawl_is_not_left_running(monkeypatch):
    # A closed tab cancels the coroutine; the run record must not stay
    # 'running' forever (site Q&A only trusts done/capped crawls).
    finished = {}

    async def fake_run_in_thread(func, *args):
        if func is crawl.db.create_web_crawl:
            return 99
        if func is crawl.db.finish_web_crawl:
            finished["args"] = args
            return None
        return None

    async def fake_crawl_site(*a, **kw):
        state = crawl._CrawlState(scope_host="z.example", scope_prefix="z.example")
        state.fetched = 3
        return state, 3, "done"

    async def hang(emit, quiet=False):
        await asyncio.Event().wait()

    monkeypatch.setattr(crawl.db, "run_in_thread", fake_run_in_thread)
    monkeypatch.setattr(crawl, "_crawl_site", fake_crawl_site)
    monkeypatch.setattr(crawl, "_drain_index", hang)

    async def emit(kind, payload):
        return None

    async def scenario():
        task = asyncio.get_running_loop().create_task(
            crawl.run_crawl_engine("crawl https://z.example", "https://z.example", "c1", [], emit)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    args = finished["args"]
    assert args[0] == 99 and args[1] == "capped"  # partial pages stay usable
    assert args[6] == "cancelled mid-run"


def test_retrieve_site_prefix_matches_www_hosts(monkeypatch):
    # scope_prefix strips "www." but stored URLs keep it — the filter must
    # match both spellings or site Q&A silently never fires on www sites.
    monkeypatch.setattr(settings, "web_memory_enabled", True)

    async def fake_embed(texts, **kw):
        return [[0.0] * 4]

    table = _FakeTable([])
    monkeypatch.setattr(web_index.llm, "embed_texts", fake_embed)
    monkeypatch.setattr(web_index, "_open", lambda create_dim=None: (None, table, SimpleNamespace(dimension=4)))
    monkeypatch.setattr(web_index, "validate_query_dimension", lambda *a, **k: None)
    asyncio.run(web_index.retrieve("q", top_k=3, site_prefix="tensorflow.org/guide"))
    clause = table.where_clause or ""
    assert "https://tensorflow.org/guide" in clause
    assert "https://www.tensorflow.org/guide" in clause
    assert "http://www.tensorflow.org/guide" in clause


def test_expand_search_domains_is_single_flight(monkeypatch):
    # Two searches finishing together must not stack two polite crawlers
    # against the same hosts.
    monkeypatch.setattr(settings, "web_expand_after_search", True)
    runs: list = []

    async def slow_crawl(root, emit, **kw):
        runs.append(root)
        await asyncio.sleep(0.1)
        return crawl._CrawlState(scope_host="h", scope_prefix="h"), 0, "done"

    async def fake_index(limit=50):
        return 0

    monkeypatch.setattr(crawl, "_crawl_site", slow_crawl)
    monkeypatch.setattr(crawl.web_index, "index_pending", fake_index)

    async def scenario():
        await asyncio.gather(
            crawl.expand_search_domains(["https://a.example/x"]),
            crawl.expand_search_domains(["https://b.example/y"]),
        )

    asyncio.run(scenario())
    assert len(runs) == 1  # the second call saw the lock and skipped
