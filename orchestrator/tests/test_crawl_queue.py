"""The background crawl queue (2026-09-03).

Sharing a URL used to read one page into one conversation. Now the page
joins the global corpus and its site is queued for a bounded background
crawl the knowledge worker drains — so "the whole site" becomes knowledge
without anyone waiting in the chat. These tests pin the queue's contract:
dedupe by scope, claim-and-run, resume after a restart, and the URL engine's
side of it (global store + queued job + a visible status).
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import db, web_worker
from app.config import settings
from app.core.net import FetchResult
from app.engines import crawl
from app.engines import url as url_engine


# ---------------------------------------------------------------------------
# db: the queue rows
# ---------------------------------------------------------------------------


def test_enqueue_is_deduped_by_scope_and_recent_crawls():
    first = db.enqueue_web_crawl("c1", "https://docs.example/en/", "docs.example/en", "share", 100, 5.0)
    assert first is not None
    assert db.enqueue_web_crawl("c2", "https://docs.example/en/page", "docs.example/en", "share", 100, 5.0) is None
    job = db.next_queued_web_crawl()
    assert job and job["id"] == first and job["kind"] == "share" and job["max_pages"] == 100
    assert db.next_queued_web_crawl() is None, "claimed exactly once"
    db.finish_web_crawl(first, "done", 10, 10, 0, 0)
    # Crawled minutes ago → a new share of the same site is not re-queued...
    assert db.enqueue_web_crawl("c3", "https://docs.example/en/", "docs.example/en", "share", 100, 5.0) is None
    # ...unless the recency window is zero.
    assert db.enqueue_web_crawl("c3", "https://docs.example/en/", "docs.example/en", "share", 100, 5.0, recent_hours=0) is not None


def test_interrupted_jobs_are_requeued_and_foreground_ones_closed():
    bg = db.enqueue_web_crawl("c1", "https://a.example/", "a.example", "research", 40, 5.0)
    assert db.next_queued_web_crawl()["id"] == bg  # now 'running'
    fg = db.create_web_crawl("c2", "https://b.example/", "b.example")  # foreground, 'running'
    assert db.requeue_interrupted_web_crawls() == 1
    assert db.get_web_crawl(bg)["status"] == "queued"
    closed = db.get_web_crawl(fg)
    assert closed["status"] == "capped" and "restart" in closed["detail"]
    counts = db.web_crawl_queue_counts()
    assert counts == {"queued": 1, "running": 0}


# ---------------------------------------------------------------------------
# engine: enqueue + drain
# ---------------------------------------------------------------------------


def test_enqueue_site_crawl_respects_the_flag_and_wakes_the_worker(monkeypatch):
    kicked = []
    monkeypatch.setattr(web_worker, "kick", lambda: kicked.append(True))
    monkeypatch.setattr(settings, "web_background_crawl_enabled", False)
    assert asyncio.run(crawl.enqueue_site_crawl("c1", "https://x.example/docs/")) is None
    monkeypatch.setattr(settings, "web_background_crawl_enabled", True)
    job = asyncio.run(crawl.enqueue_site_crawl("c1", "https://x.example/docs/page.html", kind="share"))
    assert job is not None and kicked
    row = db.get_web_crawl(job)
    assert row["status"] == "queued" and row["kind"] == "share"
    assert row["scope_prefix"].startswith("x.example/docs")
    assert asyncio.run(crawl.enqueue_site_crawl("c1", "not a url")) is None
    assert asyncio.run(crawl.enqueue_site_crawl("c1", "ftp://x.example/")) is None


def test_run_queued_crawls_runs_one_job_and_records_it(monkeypatch):
    job = db.enqueue_web_crawl("c1", "https://site.example/", "site.example", "share", 50, 2.0)

    async def fake_crawl(root_url, emit, *, max_pages, max_seconds, quiet=False):
        assert quiet and max_pages == 50 and max_seconds == 120.0
        return SimpleNamespace(fetched=7, from_store=2, failed=1), 9, "done"

    async def fake_drain(emit, quiet=False):
        return 21

    monkeypatch.setattr(crawl, "_crawl_site", fake_crawl)
    monkeypatch.setattr(crawl, "_drain_index", fake_drain)
    monkeypatch.setattr(settings, "web_background_crawl_enabled", True)
    assert asyncio.run(crawl.run_queued_crawls(max_jobs=3)) == 1
    row = db.get_web_crawl(job)
    assert row["status"] == "done" and row["pages_fetched"] == 7 and row["pages_from_store"] == 2
    assert asyncio.run(crawl.run_queued_crawls()) == 0, "queue drained"


def test_a_declined_site_marks_the_job_failed(monkeypatch):
    job = db.enqueue_web_crawl("c1", "https://closed.example/", "closed.example", "share", 50, 2.0)

    async def declined(root_url, emit, **kw):
        return SimpleNamespace(fetched=0, from_store=0, failed=0), 0, "declined: robots.txt unreadable"

    monkeypatch.setattr(crawl, "_crawl_site", declined)
    monkeypatch.setattr(settings, "web_background_crawl_enabled", True)
    asyncio.run(crawl.run_queued_crawls())
    row = db.get_web_crawl(job)
    assert row["status"] == "failed" and "declined" in row["detail"]


def test_worker_cycle_drains_the_queue_last(monkeypatch):
    async def no_index(limit=20):
        return 0

    async def one_job(max_jobs=1):
        return 1

    monkeypatch.setattr(web_worker.web_index, "index_pending", no_index)
    monkeypatch.setattr(settings, "web_knowledge_worker_enabled", False)  # skip the refresh half
    monkeypatch.setattr(settings, "web_background_crawl_enabled", True)
    monkeypatch.setattr(crawl, "run_queued_crawls", one_job)
    done = asyncio.run(web_worker.run_once())
    assert done["crawled"] == 1
    web_worker.kick()  # no loop running: a harmless no-op


# ---------------------------------------------------------------------------
# The URL engine: a shared page becomes shared knowledge
# ---------------------------------------------------------------------------


class Rec:
    def __init__(self):
        self.events = []

    async def emit(self, e, d):
        self.events.append((e, d))

    def of(self, k):
        return [d for e, d in self.events if e == k]


async def _fake_stream(messages, **kw):
    yield "token", "Pricing is $49 [1]."


def test_a_shared_page_joins_the_global_corpus_and_queues_its_site(monkeypatch):
    async def fake_fetch(u, **kw):
        return FetchResult(u, 200, "text/html", b"<h1>Pricing</h1><p>$49/mo</p>",
                           headers={"last-modified": "Tue, 01 Sep 2026 10:00:00 GMT"})

    monkeypatch.setattr(url_engine.net, "safe_fetch", fake_fetch)
    monkeypatch.setattr(
        url_engine.extract, "extract_readable",
        lambda ct, b, u: url_engine.extract.Extracted(
            title="Pricing Page", text="Pro plan is $49/mo. " * 20, published_at="2026-08-15"
        ),
    )
    monkeypatch.setattr(url_engine.llm, "stream_chat_events", _fake_stream)
    monkeypatch.setattr(settings, "web_background_crawl_enabled", True)
    monkeypatch.setattr(settings, "web_share_crawl_enabled", True)
    monkeypatch.setattr(web_worker, "kick", lambda: None)

    rec = Rec()
    asyncio.run(url_engine.run_url_engine(
        "summarize this", ["https://shop.example/docs/pricing"], "c9", [], rec.emit
    ))
    # Global store, with provenance.
    from app.engines.search import _normalize_url

    rows = db.get_web_pages([_normalize_url("https://shop.example/docs/pricing")])
    assert rows and rows[0]["title"] == "Pricing Page"
    assert rows[0]["published_at"] and rows[0]["published_at"].date().isoformat() == "2026-08-15"
    assert rows[0]["source_type"] == "docs"
    # The site is queued, and the user was told.
    assert db.web_crawl_queue_counts()["queued"] == 1
    assert any("Indexing shop.example in the background" in d["text"] for d in rec.of("status"))
    meta = rec.of("meta")[-1]
    assert meta["route"] == "url"
    assert meta["site_crawl"][0]["host"] == "shop.example" and meta["site_crawl"][0]["status"] == "queued"
    # Per-conversation memory is unchanged.
    assert db.get_url_document_urls("c9") == {"https://shop.example/docs/pricing"}


def test_sharing_with_the_crawl_off_still_stores_the_page(monkeypatch):
    async def fake_fetch(u, **kw):
        return FetchResult(u, 200, "text/html", b"<p>x</p>")

    monkeypatch.setattr(url_engine.net, "safe_fetch", fake_fetch)
    monkeypatch.setattr(url_engine.extract, "extract_readable",
                        lambda ct, b, u: url_engine.extract.Extracted(title="T", text="content " * 40))
    monkeypatch.setattr(url_engine.llm, "stream_chat_events", _fake_stream)
    monkeypatch.setattr(settings, "web_share_crawl_enabled", False)
    monkeypatch.setattr(web_worker, "kick", lambda: None)
    rec = Rec()
    asyncio.run(url_engine.run_url_engine("read", ["https://a.example/p"], "c1", [], rec.emit))
    from app.engines.search import _normalize_url

    assert db.get_web_pages([_normalize_url("https://a.example/p")])
    assert db.web_crawl_queue_counts()["queued"] == 0
    assert "site_crawl" not in rec.of("meta")[-1]
