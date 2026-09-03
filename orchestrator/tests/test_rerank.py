"""The shared cross-encoder client (ADR-0001 D4/D13).

What is pinned: the prompt template the model was trained with is applied to
every pair; a malformed response can never mis-assign a score; a busy or
broken reranker degrades to "keep your order" instead of a hang or an
exception on the request path.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import rerank
from app.config import settings


def run(coro):
    return asyncio.run(coro)


def test_template_matches_the_model_card():
    q = rerank.format_query("who is the ceo of acme", instruction=None)
    assert q.startswith(rerank.PREFIX)
    assert "<Instruct>: " + rerank.DEFAULT_INSTRUCTION in q
    assert q.endswith("<Query>: who is the ceo of acme\n")
    d = rerank.format_document("Jane Doe, Founder and CEO")
    assert d.startswith("<Document>: Jane Doe, Founder and CEO")
    assert d.endswith(rerank.SUFFIX)


def test_document_and_query_are_clipped():
    assert len(rerank.format_document("x" * 10_000)) < 10_000
    assert len(rerank.format_query("y" * 5_000)) < 5_000


def test_score_url_strips_v1():
    assert rerank.score_url("http://vllm-reranker:30005/v1") == "http://vllm-reranker:30005/score"
    assert rerank.score_url("http://vllm-reranker:30005/") == "http://vllm-reranker:30005/score"


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"index": 0, "score": 0.5}]},  # too few
        {"data": [{"index": 0, "score": 0.5}, {"index": 0, "score": 0.4}]},  # duplicate
        {"data": [{"index": 0, "score": 0.5}, {"index": 5, "score": 0.4}]},  # out of range
        {"data": [{"index": 0, "score": float("nan")}, {"index": 1, "score": 0.4}]},
        {"data": "nope"},
        [],
    ],
)
def test_parse_scores_is_strict(payload):
    with pytest.raises(ValueError):
        rerank.parse_scores(payload, 2)


def test_parse_scores_returns_document_order():
    payload = {"data": [{"index": 1, "score": 0.9}, {"index": 0, "score": 0.1}]}
    assert rerank.parse_scores(payload, 2) == [0.1, 0.9]


class _Caps:
    enabled = True
    supports_reranking = True
    requires_authentication = False


@pytest.fixture()
def remote(monkeypatch):
    monkeypatch.setattr(settings, "rerank_enabled", True)
    monkeypatch.setattr(settings, "rerank_base_url", "http://reranker.test")
    monkeypatch.setattr(settings, "rerank_model", "test-reranker")
    monkeypatch.setattr(settings, "rerank_api_key", "")
    monkeypatch.setattr(settings, "reranker_capabilities", _Caps())
    monkeypatch.setattr(settings, "rerank_max_inflight", 2)
    monkeypatch.setattr(settings, "rerank_reserved_slots", 0)
    monkeypatch.setattr(settings, "rerank_wait_s", 0.2)
    # The canary has its own tests (test_knowledge_unified); here the fake
    # server answers arbitrary scores, which a live canary would rightly
    # refuse.
    monkeypatch.setattr(settings, "rerank_canary_enabled", False)
    rerank.reset_for_tests()
    yield
    rerank.reset_for_tests()


def _fake_client(handler):
    transport = httpx.MockTransport(handler)

    class _Client(httpx.AsyncClient):
        def __init__(self, *a, **k):
            k["transport"] = transport
            super().__init__(*a, **k)

    return _Client


def test_score_sends_templated_pairs_and_returns_probabilities(remote, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        n = len(seen["body"]["text_2"])
        return httpx.Response(200, json={"data": [{"index": i, "score": 0.1 * (i + 1)} for i in range(n)]})

    monkeypatch.setattr(rerank.httpx, "AsyncClient", _fake_client(handler))
    scores = run(rerank.score("what is x", ["a", "b", "c"]))
    assert scores == pytest.approx([0.1, 0.2, 0.3])
    assert seen["url"] == "http://reranker.test/score"
    assert seen["body"]["text_1"].startswith(rerank.PREFIX)
    assert all(t.startswith("<Document>: ") and t.endswith(rerank.SUFFIX) for t in seen["body"]["text_2"])


def test_order_keeps_input_order_when_reranker_fails(remote, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    monkeypatch.setattr(rerank.httpx, "AsyncClient", _fake_client(handler))
    items = [{"text": "a"}, {"text": "b"}]
    assert run(rerank.order("q", items)) == items
    with pytest.raises(rerank.RerankUnavailable):
        run(rerank.score("q", ["a", "b"]))


def test_order_ranks_by_score_and_annotates(remote, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "score": 0.2}, {"index": 1, "score": 0.9}]})

    monkeypatch.setattr(rerank.httpx, "AsyncClient", _fake_client(handler))
    out = run(rerank.order("q", [{"text": "a"}, {"text": "b"}], top_n=1))
    assert out == [{"text": "b", "rerank_score": 0.9}]


def test_disabled_reranker_is_unavailable_not_an_error(remote, monkeypatch):
    monkeypatch.setattr(settings, "rerank_enabled", False)
    with pytest.raises(rerank.RerankUnavailable):
        run(rerank.score("q", ["a"]))
    assert run(rerank.order("q", [{"text": "a"}, {"text": "b"}])) == [{"text": "a"}, {"text": "b"}]


def test_busy_reranker_times_out_instead_of_queueing(remote, monkeypatch):
    async def slow_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        n = len(json.loads(request.content)["text_2"])
        return httpx.Response(200, json={"data": [{"index": i, "score": 0.5} for i in range(n)]})

    monkeypatch.setattr(rerank.httpx, "AsyncClient", _fake_client(slow_handler))

    async def scenario():
        # Two slots are taken by slow calls; the third must give up after
        # rerank_wait_s (0.2 s) rather than wait 0.5 s behind them.
        first = asyncio.create_task(rerank.score("q", ["a"]))
        second = asyncio.create_task(rerank.score("q", ["b"]))
        await asyncio.sleep(0.05)
        started = asyncio.get_running_loop().time()
        with pytest.raises(rerank.RerankUnavailable):
            await rerank.score("q", ["c"])
        waited = asyncio.get_running_loop().time() - started
        await asyncio.gather(first, second)
        return waited

    waited = run(scenario())
    assert waited < 0.45
