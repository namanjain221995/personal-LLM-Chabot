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
