"""URL engine + storage (Phase 2). Fetch mocked; DB is a real temp SQLite."""
import asyncio

import pytest

from app import db
from app.config import settings
from app.core.net import FetchResult
from app.engines import url as url_engine


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_db_path", str(tmp_path / "app.sqlite3"))


class Rec:
    def __init__(self):
        self.events = []

    async def emit(self, e, d):
        self.events.append((e, d))

    def of(self, k):
        return [d for e, d in self.events if e == k]


async def _fake_stream(messages, **kw):
    yield "token", "The page says pricing is $49 [1]."


def test_url_document_roundtrip(temp_db):
    db.save_url_document("c1", "https://x.com", "X", "hello world")
    docs = db.get_url_documents("c1")
    assert docs == [{"url": "https://x.com", "title": "X", "text": "hello world"}]
    assert db.get_url_document_urls("c1") == {"https://x.com"}
    # upsert replaces
    db.save_url_document("c1", "https://x.com", "X2", "updated")
    assert db.get_url_documents("c1")[0]["title"] == "X2"


def test_run_url_engine_fetches_stores_and_cites(temp_db, monkeypatch):
    async def fake_fetch(u, **kw):
        return FetchResult(u, 200, "text/html", b"<h1>Pricing</h1><p>$49/mo</p>")

    monkeypatch.setattr(url_engine.net, "safe_fetch", fake_fetch)
    monkeypatch.setattr(
        url_engine.extract,
        "extract_readable",
        lambda ct, b, u: url_engine.extract.Extracted(title="Pricing Page", text="Pro plan is $49/mo"),
    )
    monkeypatch.setattr(url_engine.llm, "stream_chat_events", _fake_stream)

    rec = Rec()
    ans = asyncio.run(
        url_engine.run_url_engine(
            "summarize this", ["https://shop.example/pricing"], "c9", [], rec.emit
        )
    )
    assert "$49" in ans
    # status "Reading …"
    assert any("Reading" in d["text"] for d in rec.of("status"))
    # meta route url + sources
    meta = rec.of("meta")[-1]
    assert meta["route"] == "url"
    assert meta["sources"][0]["domain"] == "shop.example"
    # stored for follow-ups
    assert db.get_url_document_urls("c9") == {"https://shop.example/pricing"}


def test_follow_up_uses_stored_and_does_not_refetch(temp_db, monkeypatch):
    db.save_url_document("c2", "https://x.com/p", "P", "Pricing is $49 per month.")

    calls = {"n": 0}

    async def fake_fetch(u, **kw):
        calls["n"] += 1
        return FetchResult(u, 200, "text/html", b"x")

    monkeypatch.setattr(url_engine.net, "safe_fetch", fake_fetch)
    monkeypatch.setattr(url_engine.llm, "stream_chat_events", _fake_stream)

    rec = Rec()
    # same URL already stored → run_url_engine must NOT refetch it
    asyncio.run(
        url_engine.run_url_engine(
            "what about pricing?", ["https://x.com/p"], "c2", [], rec.emit
        )
    )
    assert calls["n"] == 0  # no fetch — served from storage
