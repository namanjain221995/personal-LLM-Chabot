"""One pooled reranker client per event loop (2026-09-13, plan item 3d).

`rerank._post` built a fresh httpx.AsyncClient for every /score call: a new
connection pool, a new TCP connection and an SSL context on the answer path,
once or more per Fast turn (rerank p95 0.92 s, knowledge_stage_seconds 7 d).
What is pinned: calls on one loop share one client, the client is built off
the loop, each call still carries its own timeout, a new loop or a swapped
client class never inherits a pool, and shutdown closes it.
"""
from __future__ import annotations

import asyncio
import json
import threading

import httpx
import pytest

from app import rerank
from app.config import settings


#: The real class, captured before any test patches httpx.AsyncClient, so a
#: second fake never subclasses the first.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


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
    monkeypatch.setattr(settings, "rerank_canary_enabled", False)
    rerank.reset_for_tests()
    yield
    rerank.reset_for_tests()


def _counting_client(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.setdefault("timeouts", []).append(request.extensions.get("timeout"))
        n = len(json.loads(request.content)["text_2"])
        return httpx.Response(200, json={"data": [{"index": i, "score": 0.1 * (i + 1)} for i in range(n)]})

    transport = httpx.MockTransport(handler)

    class _Client(_REAL_ASYNC_CLIENT):
        def __init__(self, *a, **k):
            seen["built"] = seen.get("built", 0) + 1
            seen.setdefault("built_on", []).append(threading.get_ident())
            k["transport"] = transport
            super().__init__(*a, **k)

    return _Client


def test_calls_on_one_loop_reuse_one_client(remote, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(rerank.httpx, "AsyncClient", _counting_client(seen))

    async def scenario():
        a = await rerank.score("q", ["a", "b"])
        b = await rerank.score("q", ["c"])
        c = await asyncio.gather(*(rerank.score("q", [str(i)]) for i in range(4)))
        return a, b, c, threading.get_ident()

    a, b, c, loop_thread = asyncio.run(scenario())
    assert a == pytest.approx([0.1, 0.2]) and b == pytest.approx([0.1])
    assert len(c) == 4
    assert seen["built"] == 1
    # Built off the loop: loading the CA bundle is blocking work.
    assert seen["built_on"] == [seen["built_on"][0]] and seen["built_on"][0] != loop_thread


def test_each_call_keeps_its_own_timeout(remote, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(rerank.httpx, "AsyncClient", _counting_client(seen))

    async def scenario():
        await rerank.score("q", ["a"], timeout=1.5)
        await rerank.score("q", ["a"], timeout=7.0)

    asyncio.run(scenario())
    assert seen["built"] == 1
    assert [t["read"] for t in seen["timeouts"]] == [1.5, 7.0]


def test_a_new_event_loop_gets_its_own_client(remote, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(rerank.httpx, "AsyncClient", _counting_client(seen))
    asyncio.run(rerank.score("q", ["a"]))
    asyncio.run(rerank.score("q", ["a"]))
    assert seen["built"] == 2


def test_a_swapped_client_class_is_not_served_the_old_pool(remote, monkeypatch):
    first: dict = {}
    second: dict = {}

    async def scenario():
        monkeypatch.setattr(rerank.httpx, "AsyncClient", _counting_client(first))
        await rerank.score("q", ["a"])
        monkeypatch.setattr(rerank.httpx, "AsyncClient", _counting_client(second))
        await rerank.score("q", ["a"])

    asyncio.run(scenario())
    assert first["built"] == 1 and second["built"] == 1


def test_shutdown_closes_the_pooled_client_and_a_later_call_rebuilds_it(remote, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(rerank.httpx, "AsyncClient", _counting_client(seen))

    async def scenario():
        await rerank.score("q", ["a"])
        client = await rerank._rerank_client()
        await rerank.close_rerank_client()
        closed = client.is_closed
        await rerank.score("q", ["a"])
        return closed

    assert asyncio.run(scenario()) is True
    assert seen["built"] == 2


def test_closing_when_nothing_was_built_is_a_no_op():
    asyncio.run(rerank.close_rerank_client())


def test_concurrent_first_calls_build_the_client_once(remote, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(rerank.httpx, "AsyncClient", _counting_client(seen))

    async def scenario():
        await asyncio.gather(*(rerank.score("q", [str(i)]) for i in range(6)))

    asyncio.run(scenario())
    assert seen["built"] == 1


def test_application_shutdown_closes_the_pooled_reranker_client(remote, monkeypatch):
    """The docstring said shutdown closed it; nothing called it (second prover
    pass, 2026-09-13: `grep close_rerank_client` found only the docstring)."""
    from fastapi.testclient import TestClient

    from app import main

    closed = []

    class Recording(_REAL_ASYNC_CLIENT):
        async def aclose(self):
            closed.append(True)
            await super().aclose()

    monkeypatch.setattr(httpx, "AsyncClient", Recording)
    with TestClient(main.app) as client:
        client.portal.call(rerank._rerank_client)
        assert closed == []
    assert closed == [True]
