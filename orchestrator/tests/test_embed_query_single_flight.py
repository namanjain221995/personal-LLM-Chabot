"""embed_query's single-flight under cancellation and mixed budgets
(second prover pass, 2026-09-13).

Context assembly runs cross-chat recall and in-conversation recall at once and
both embed the same question, so one sidecar call serves both (single-flight).
The prover's embed_probe.py found two ways the first version differed from
HEAD, where every caller made its own bounded call:

  - an abandoned flight kept its EMBED_MAX_INFLIGHT slot for wait + timeout
    (1 s + 4 s). The Fast topical path cancels retrieval on every pre-check
    miss, so with a sidecar that accepts calls and then hangs, abandoned
    flights filled every slot and the next question failed 'busy' after 1.0 s
    (HEAD: ok in 0.01 s);
  - a joiner inherited the budget of whoever started the flight: a caller with
    the default 1 s wait that joined a 0.05 s flight failed 'busy' after
    0.04 s (HEAD: ok in 0.291 s).

The sidecar is a stub: 'slow…' texts hang until their timeout, 'fast…' texts
answer in 10 ms. No network.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app import llm
from app.config import settings


@pytest.fixture(autouse=True)
def _stub_sidecar(monkeypatch):
    calls = []

    async def fake_embed_texts(texts, *, timeout=None, kind="", **kw):
        calls.append(texts[0])
        if texts[0].startswith("slow"):
            await asyncio.sleep(float(timeout or 4.0))
            raise TimeoutError("sidecar hung")
        await asyncio.sleep(0.01)
        return [[0.1, 0.2]]

    monkeypatch.setattr(llm, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(settings, "embed_max_inflight", 2)
    llm.embed_cache_clear()
    yield calls
    llm.embed_cache_clear()


def test_callers_that_leave_free_their_slots_for_the_next_question():
    async def scenario():
        callers = [asyncio.ensure_future(llm.embed_query(f"slow question {i}", timeout=4.0)) for i in range(2)]
        await asyncio.sleep(0.1)
        for c in callers:
            c.cancel()
        await asyncio.gather(*callers, return_exceptions=True)
        await asyncio.sleep(0.05)
        started = time.perf_counter()
        vector = await llm.embed_query("fast question", wait=1.0)
        return vector, time.perf_counter() - started

    vector, elapsed = asyncio.run(scenario())
    assert vector == [0.1, 0.2]
    assert elapsed < 0.5, f"the next question waited {elapsed:.3f} s for slots nobody needed"


def test_a_caller_never_inherits_a_shorter_budget_from_the_flight_it_would_join():
    async def scenario():
        blockers = [asyncio.ensure_future(llm.embed_query(f"slow blocker {i}", timeout=0.3)) for i in range(2)]
        await asyncio.sleep(0.01)
        short = asyncio.ensure_future(llm.embed_query("fast shared", wait=0.05))
        await asyncio.sleep(0.01)
        started = time.perf_counter()
        try:
            return await llm.embed_query("fast shared", wait=1.0), time.perf_counter() - started
        finally:
            await asyncio.gather(short, *blockers, return_exceptions=True)

    vector, elapsed = asyncio.run(scenario())
    assert vector == [0.1, 0.2]
    assert 0.2 < elapsed < 1.0, elapsed  # it waited for a slot on its own budget


def test_two_callers_with_the_same_budget_still_share_one_sidecar_call(_stub_sidecar):
    async def scenario():
        return await asyncio.gather(llm.embed_query("fast twin"), llm.embed_query("fast twin"))

    a, b = asyncio.run(scenario())
    assert a == b == [0.1, 0.2]
    assert _stub_sidecar.count("fast twin") == 1


def test_one_caller_leaving_does_not_fail_the_one_still_waiting(_stub_sidecar):
    async def scenario():
        first = asyncio.ensure_future(llm.embed_query("fast pair"))
        second = asyncio.ensure_future(llm.embed_query("fast pair"))
        await asyncio.sleep(0)
        first.cancel()
        return await second

    assert asyncio.run(scenario()) == [0.1, 0.2]
    assert _stub_sidecar.count("fast pair") == 1


def test_a_caller_arriving_after_the_last_one_left_starts_a_fresh_flight(_stub_sidecar):
    async def scenario():
        gone = asyncio.ensure_future(llm.embed_query("fast again"))
        await asyncio.sleep(0)
        gone.cancel()
        await asyncio.sleep(0)
        return await llm.embed_query("fast again")

    assert asyncio.run(scenario()) == [0.1, 0.2]
