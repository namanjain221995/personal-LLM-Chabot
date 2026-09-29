"""`retrieve(..., on_candidates=...)`: the merged candidates, with their stored
dates, before rank and rerank (2026-09-29, track retrieval-does-less, B2).

Why it exists: a Fast lookup for a time-sensitive question waited for the
router, the retrieval, the rerank of 12-17 passages and the claims read
(430-990 ms at one turn at a time) before it started, although
`Retrieval.sufficient` can only pass with a passage no older than the
verdict's max age. Once the candidates' fetch dates are known, "nothing stored
is fresh enough" can be certain, and living_knowledge can start the lookup
while the rerank still runs. This file pins the hook's contract; what the
caller does with it is living_knowledge's to test.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import rerank, web_index, web_memory
from app.config import settings
from app.freshness import Freshness

FROZEN = datetime.now(timezone.utc).replace(microsecond=0)
QUESTION = "current exchange rate of the kestrel dollar"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    web_memory.cache_clear()
    monkeypatch.setattr(web_memory, "_now", lambda: FROZEN)
    for name, value in dict(
        web_memory_enabled=True, knowledge_rerank=True, knowledge_evidence_cache_ttl_s=60.0,
        knowledge_rerank_candidates=12,
    ).items():
        monkeypatch.setattr(settings, name, value, raising=False)
    yield
    web_memory.cache_clear()


def _stub(monkeypatch, *, dense=(), rows=(), meta=None):
    order = []

    async def dense_half(query, top_k=6, site_prefix=""):
        return [dict(h) for h in dense]

    def lexical(query, limit):
        return [dict(r) for r in rows]

    def page_meta(urls, ids=()):
        order.append("meta")
        return dict(meta or {})

    real_rank = web_memory._rank_candidates

    def rank(*args, **kwargs):
        order.append("rank")
        return real_rank(*args, **kwargs)

    async def score(query, docs, **kwargs):
        order.append("rerank")
        return [0.8] * len(docs)

    monkeypatch.setattr(web_index, "retrieve", dense_half)
    monkeypatch.setattr(web_memory, "_lexical_candidates", lexical)
    monkeypatch.setattr(web_memory, "_page_meta", page_meta)
    monkeypatch.setattr(web_memory, "_rank_candidates", rank)
    monkeypatch.setattr(rerank, "score", score)
    return order


_DENSE = [
    {"url": "https://rates.example/kestrel", "title": "Kestrel dollar rate", "page_id": 11,
     "text": "The kestrel dollar exchange rate today is 1.07.", "fetched_at": "", "score": 0.2},
    {"url": "https://pulled.example/kestrel", "title": "Pulled page", "page_id": 12,
     "text": "The kestrel dollar exchange rate is 0.91.", "fetched_at": "", "score": 0.25},
]
_ROWS = [
    {"id": 13, "url": "https://news.example/kestrel", "title": "Kestrel dollar news",
     "text": "Kestrel dollar exchange rate news: the current rate moved again. " * 5,
     "domain": "news.example", "authority": 40, "fetched_at": FROZEN - timedelta(hours=30),
     "published_at": None, "modified_at": None, "source_type": "", "origin": "search",
     "content_hash": "news13"},
]
_META = {
    "id:11": {"id": 11, "url": "https://rates.example/kestrel", "title": "Kestrel dollar rate",
              "domain": "rates.example", "authority": 70, "fetched_at": FROZEN - timedelta(hours=2),
              "published_at": None, "last_changed_at": None, "modified_at": None,
              "source_type": "", "origin": "search", "quarantined_at": None},
    "id:12": {"id": 12, "url": "https://pulled.example/kestrel", "title": "Pulled page",
              "domain": "pulled.example", "authority": 40, "fetched_at": FROZEN - timedelta(hours=1),
              "published_at": None, "last_changed_at": None, "modified_at": None,
              "source_type": "", "origin": "search", "quarantined_at": FROZEN},
}


def test_the_hook_runs_once_after_meta_and_before_rank_and_rerank(monkeypatch):
    order = _stub(monkeypatch, dense=_DENSE, rows=_ROWS, meta=_META)
    seen = []

    def hook(candidates):
        order.append("hook")
        seen.append([(e.url, e.fetched_at) for e in candidates])

    result = asyncio.run(web_memory.retrieve(
        QUESTION, level=Freshness.REALTIME, top_k=5, use_cache=False, on_candidates=hook,
    ))
    assert order == ["meta", "hook", "rank", "rerank"]
    assert len(seen) == 1
    got = dict(seen[0])
    # The dense-only hit carries the date PostgreSQL holds, not the index's.
    assert got["https://rates.example/kestrel"] == FROZEN - timedelta(hours=2)
    assert got["https://news.example/kestrel"] == FROZEN - timedelta(hours=30)
    # The quarantined page never reaches the hook.
    assert "https://pulled.example/kestrel" not in got
    assert {e.url for e in result.evidence} <= set(got)


def test_the_hook_gets_an_empty_sequence_when_neither_half_found_anything(monkeypatch):
    order = _stub(monkeypatch)
    seen = []
    result = asyncio.run(web_memory.retrieve(
        QUESTION, level=Freshness.REALTIME, top_k=5, use_cache=False, on_candidates=lambda c: seen.append(c),
    ))
    assert seen == [()]
    assert result.evidence == [] and order == []


def test_a_failing_hook_changes_nothing(monkeypatch):
    _stub(monkeypatch, dense=_DENSE, rows=_ROWS, meta=_META)

    def shape(result):
        return [(e.url, e.text, e.score, e.answer) for e in result.evidence]

    def boom(candidates):
        raise RuntimeError("observer broke")

    plain = asyncio.run(web_memory.retrieve(QUESTION, level=Freshness.REALTIME, top_k=5, use_cache=False))
    hooked = asyncio.run(web_memory.retrieve(
        QUESTION, level=Freshness.REALTIME, top_k=5, use_cache=False, on_candidates=boom,
    ))
    assert plain.evidence and shape(hooked) == shape(plain)


def test_the_hook_cannot_reorder_what_retrieve_ranks(monkeypatch):
    """It is handed a snapshot: clearing or appending to what it gets does not
    reach the candidate list retrieve goes on to rank."""
    _stub(monkeypatch, dense=_DENSE, rows=_ROWS, meta=_META)
    received = []
    result = asyncio.run(web_memory.retrieve(
        QUESTION, level=Freshness.REALTIME, top_k=5, use_cache=False, on_candidates=received.append,
    ))
    assert isinstance(received[0], tuple)
    assert len(result.evidence) == 2


def test_a_result_served_from_the_cache_does_not_call_the_hook(monkeypatch):
    _stub(monkeypatch, dense=_DENSE, rows=_ROWS, meta=_META)

    async def servable(value):
        return True

    monkeypatch.setattr(web_memory, "_cache_entry_still_servable", servable)
    calls = []
    first = asyncio.run(web_memory.retrieve(QUESTION, level=Freshness.REALTIME, top_k=5, on_candidates=calls.append))
    second = asyncio.run(web_memory.retrieve(QUESTION, level=Freshness.REALTIME, top_k=5, on_candidates=calls.append))
    assert first.evidence and [e.url for e in second.evidence] == [e.url for e in first.evidence]
    assert len(calls) == 1
