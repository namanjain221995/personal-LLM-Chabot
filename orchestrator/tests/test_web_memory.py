"""V8 web-search memory: every search logged, every page stored, all reusable.

Before this, the search engine persisted nothing: page text was fetched,
truncated, answered from, and discarded — an identical question a minute later
re-fetched every page (measured), and a container restart lost even the
15-minute SERP cache. These tests pin the store, the change-detection hash,
the warm-cache fetch path, and the effort passthrough (the answer call ran a
full thinking pass whatever the picker said — 77-82% of measured wall-clock).
"""
import asyncio
import hashlib
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import db, web_index
from app.config import settings
from app.engines import search
from app.search.base import SearchResult


# ---------------------------------------------------------------------------
# Postgres store
# ---------------------------------------------------------------------------


def test_upsert_web_page_detects_change_via_hash():
    text1 = "release notes v1"
    text2 = "release notes v2 — updated"
    h = lambda t: hashlib.sha256(t.encode()).hexdigest()

    first = db.upsert_web_page(
        url_key="example.com/notes", url="https://example.com/notes",
        canonical_url="https://example.com/notes", title="Notes",
        text=text1, content_type="text/html", fetch_status=200, content_hash=h(text1),
    )
    assert first["changed"] is False and first["previous_hash"] is None

    same = db.upsert_web_page(
        url_key="example.com/notes", url="https://example.com/notes",
        canonical_url="https://example.com/notes", title="Notes",
        text=text1, content_type="text/html", fetch_status=200, content_hash=h(text1),
    )
    assert same["changed"] is False
    assert same["id"] == first["id"]  # global dedup: one row per URL

    changed = db.upsert_web_page(
        url_key="example.com/notes", url="https://example.com/notes",
        canonical_url="https://example.com/notes", title="Notes",
        text=text2, content_type="text/html", fetch_status=200, content_hash=h(text2),
    )
    assert changed["changed"] is True
    assert changed["previous_hash"] == h(text1)

    # A changed page re-enters the indexing queue (indexed_at reset to NULL).
    pending = {p["id"] for p in db.get_unindexed_web_pages(limit=50)}
    assert first["id"] in pending

    rows = db.get_web_pages(["example.com/notes"])
    assert len(rows) == 1 and rows[0]["text"] == text2
    assert rows[0]["fetch_status"] == 200


def test_log_web_search_records_queries_and_links():
    sid = db.log_web_search(
        user_id=None, conversation_id="conv-web-1",
        message="what is new in vllm", queries=["vllm latest release", "vllm changelog"],
        provider="searxng", effort="think",
        results=[{"query": "vllm latest release", "rank": 1,
                  "url": "https://a.example/x", "url_key": "a.example/x",
                  "title": "A", "snippet": "s"}],
    )
    assert isinstance(sid, int) and sid > 0


def test_mark_indexed_drains_the_queue():
    r = db.upsert_web_page(
        url_key="example.com/drain", url="https://example.com/drain",
        canonical_url="", title="D", text="body " * 100,
        content_type="text/html", fetch_status=200, content_hash="hh",
    )
    assert any(p["id"] == r["id"] for p in db.get_unindexed_web_pages(limit=100))
    db.mark_web_pages_indexed([r["id"]])
    assert not any(p["id"] == r["id"] for p in db.get_unindexed_web_pages(limit=100))


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def test_chunk_page_overlaps_and_caps():
    text = "x" * 10_000
    chunks = web_index.chunk_page(text)
    assert 2 <= len(chunks) <= web_index._MAX_CHUNKS_PER_PAGE
    assert all(len(c) >= web_index._MIN_CHUNK_CHARS for c in chunks)
    assert web_index.chunk_page("too short") == []


# ---------------------------------------------------------------------------
# Warm cache: a fresh stored page answers without the network
# ---------------------------------------------------------------------------


def _result(url="https://example.com/page"):
    return SearchResult(title="T", url=url, snippet="snip")


def test_fresh_stored_page_skips_the_network(monkeypatch):
    from datetime import datetime, timezone

    key = search._normalize_url("https://example.com/page")
    row = {
        "url_key": key, "url": "https://example.com/page", "canonical_url": "",
        "title": "Stored Title", "text": "stored body " * 50,
        "content_type": "text/html", "fetch_status": 200, "content_hash": "h",
        "fetched_at": datetime.now(timezone.utc),
    }
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(db, "get_web_pages", lambda keys: [row])

    async def boom(*a, **k):  # the network must NOT be touched
        raise AssertionError("network fetch attempted despite a fresh stored copy")

    monkeypatch.setattr(search.net, "safe_fetch", boom)
    sources = asyncio.run(search._fetch_sources([_result()], "tell me about the page"))
    assert len(sources) == 1
    assert sources[0].title == "Stored Title"
    assert "stored body" in sources[0].text


def test_stale_stored_page_is_refetched(monkeypatch):
    from datetime import datetime, timezone

    key = search._normalize_url("https://example.com/page")
    old = datetime.now(timezone.utc) - timedelta(days=30)
    row = {"url_key": key, "url": "https://example.com/page", "canonical_url": "",
           "title": "Old", "text": "old body", "content_type": "text/html",
           "fetch_status": 200, "content_hash": "h", "fetched_at": old}
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(db, "get_web_pages", lambda keys: [row])
    stored = asyncio.run(search._stored_pages([_result()], "tell me about the page"))
    assert stored == {}  # too old → the fetch layer goes to the network


def test_fresh_intent_uses_the_short_ttl(monkeypatch):
    from datetime import datetime, timezone

    key = search._normalize_url("https://example.com/page")
    three_hours = datetime.now(timezone.utc) - timedelta(hours=3)
    row = {"url_key": key, "url": "https://example.com/page", "canonical_url": "",
           "title": "T", "text": "body", "content_type": "text/html",
           "fetch_status": 200, "content_hash": "h", "fetched_at": three_hours}
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(db, "get_web_pages", lambda keys: [row])
    # "latest" trips _FRESH_RE → 1-hour TTL → a 3-hour copy is stale.
    stored = asyncio.run(search._stored_pages([_result()], "latest release notes"))
    assert stored == {}
    # The same copy IS fresh for a non-fresh question (24-hour TTL).
    stored = asyncio.run(search._stored_pages([_result()], "explain the page contents"))
    assert key in stored


# ---------------------------------------------------------------------------
# Memory sources: dated, deduped, never on fresh intent
# ---------------------------------------------------------------------------


def _evidence(url, title, text, *, lexical=1.0, fetched="2026-08-28", published=None):
    from datetime import datetime, timezone

    from app.web_memory import Evidence

    return Evidence(
        url=url, title=title, text=text, domain=url.split("/")[2], authority=40,
        fetched_at=datetime.fromisoformat(fetched).replace(tzinfo=timezone.utc),
        published_at=datetime.fromisoformat(published).replace(tzinfo=timezone.utc) if published else None,
        lexical=lexical,
    )


def test_memory_sources_consult_the_store_for_fresh_intent_too(monkeypatch):
    """Until 2026-09-03 a "fresh-intent" wording ("latest", "who is") skipped
    the store entirely, so Think answered from live results alone while the
    store held the page that answered. The store is consulted for every
    question; the freshness verdict's own supersession handles staleness,
    and a passage must be RELEVANT to appear."""
    from app import web_memory
    from app.freshness import Freshness

    monkeypatch.setattr(settings, "web_memory_enabled", True)
    seen = {}

    async def fake(question, *, level, top_k):
        seen["level"] = level
        r = web_memory.Retrieval(query=question, freshness=level)
        r.evidence = [
            _evidence("https://b.example/y", "Answering page", "the release notes say 0.11", lexical=1.0),
            _evidence("https://c.example/z", "Unrelated page", "nothing to do with it", lexical=0.0),
        ]
        return r

    monkeypatch.setattr(web_memory, "retrieve", fake)
    out = asyncio.run(search._memory_sources("latest vllm release", []))
    assert seen["level"] is Freshness.RECENT
    assert [s.url for s in out] == ["https://b.example/y"]  # the irrelevant one never appears
    assert out[0].from_store


def test_memory_sources_append_dated_and_deduped(monkeypatch):
    from app import web_memory

    monkeypatch.setattr(settings, "web_memory_enabled", True)
    live = [search._Source(n=1, title="Live", url="https://a.example/x", text="t")]

    async def fake(question, *, level, top_k):
        r = web_memory.Retrieval(query=question, freshness=level)
        r.evidence = [
            _evidence("https://a.example/x", "Dup", "dup", fetched="2026-08-29"),
            _evidence("https://b.example/y", "Memory", "remembered paragraph", fetched="2026-08-28", published="2026-06-15"),
        ]
        return r

    monkeypatch.setattr(web_memory, "retrieve", fake)
    out = asyncio.run(search._memory_sources("how does the framework work", live))
    assert len(out) == 2  # dup dropped, memory appended
    assert out[1].n == 2
    assert "read 2026-08-28" in out[1].title
    assert "published 2026-06-15" in out[1].title
    assert "verify against newer sources" in out[1].title


# ---------------------------------------------------------------------------
# The effort picker reaches the answer call
# ---------------------------------------------------------------------------


def test_search_answer_honours_the_effort(monkeypatch):
    rec = {}

    async def fake_rewrite(message, history, effort="medium"):
        return ["q1"]

    async def fake_collect(queries, effort="medium", emit=None, **kw):
        return [_result()]

    async def fake_fetch(results, message="", **attribution):
        return [search._Source(n=1, title="T", url="https://a.example/x", text="body")]

    async def fake_rerank(message, results, target):
        return results

    async def fake_memory(message, sources, budget=3):
        return sources

    def fake_stream(messages, **kw):
        rec.update(kw)

        async def gen():
            yield "token", "ok"

        return gen()

    async def collect_emit(kind, data):
        return None

    monkeypatch.setattr(search, "rewrite_queries", fake_rewrite)
    monkeypatch.setattr(search, "_collect_results", fake_collect)
    monkeypatch.setattr(search, "_fetch_sources", fake_fetch)
    monkeypatch.setattr(search, "_rerank_results", fake_rerank)
    monkeypatch.setattr(search, "_memory_sources", fake_memory)
    monkeypatch.setattr(search.llm, "stream_chat_events", fake_stream)

    async def fake_persist(*a, **k):
        return None

    monkeypatch.setattr(search, "_persist_and_index", fake_persist)
    asyncio.run(search.run_search_engine("q", [], collect_emit, effort="fast"))
    # The measured bug: this call carried NO effort, so it defaulted to a full
    # thinking pass — 851 reasoning tokens ahead of a 32-token answer.
    assert rec.get("effort") == "fast"


# ---------------------------------------------------------------------------
# Review round (2026-08-30): NUL bytes, stored links, search attribution
# ---------------------------------------------------------------------------


def test_upsert_web_page_survives_nul_bytes():
    # text/plain bodies and PDF text layers can carry \x00 through UTF-8
    # decoding; PostgreSQL rejects it. This aborted whole crawls and silently
    # killed the search path's write-behind store for the affected URL.
    row = db.upsert_web_page(
        url_key="example.com/nul", url="https://example.com/nul",
        canonical_url="", title="N\x00ul", text="before\x00after " * 40,
        content_type="text/plain", fetch_status=200, content_hash="h1",
    )
    assert row["id"] > 0
    stored = db.get_web_pages(["example.com/nul"])
    assert "\x00" not in stored[0]["text"] and "beforeafter" in stored[0]["text"]


def test_upsert_web_page_stores_links_and_returns_them():
    links = ["https://example.com/a", "https://example.com/b"]
    db.upsert_web_page(
        url_key="example.com/hub", url="https://example.com/hub",
        canonical_url="", title="Hub", text="hub page " * 40,
        content_type="text/html", fetch_status=200, content_hash="h1",
        links=links,
    )
    assert db.get_web_pages(["example.com/hub"])[0]["links"] == links
    # A refetch replaces the links along with the text they came from.
    db.upsert_web_page(
        url_key="example.com/hub", url="https://example.com/hub",
        canonical_url="", title="Hub", text="hub page v2 " * 40,
        content_type="text/html", fetch_status=200, content_hash="h2",
        links=["https://example.com/c"],
    )
    assert db.get_web_pages(["example.com/hub"])[0]["links"] == ["https://example.com/c"]


def test_search_log_carries_user_and_conversation(monkeypatch):
    # The engine hardcoded user_id=None / conversation_id="" — so the V8 log
    # was anonymous AND delete_conversation could never match its rows,
    # leaving deleted conversations' search text stored forever.
    captured = {}
    monkeypatch.setattr(
        search.db, "log_web_search", lambda **kw: captured.update(kw) or 1
    )
    monkeypatch.setattr(
        search, "get_provider", lambda: SimpleNamespace(name="test")
    )
    search._log_search_background(
        "who won", ["who won"],
        [SearchResult(title="t", url="https://a.example/x", snippet="s")],
        "fast", user_id=7, conversation_id="conv-attr-1",
    )
    assert captured["user_id"] == 7
    assert captured["conversation_id"] == "conv-attr-1"
