"""The end of a chat turn under a second cancellation and a slow meter count
(second prover pass, 2026-09-13).

Two things the prover reproduced against the first version of this change:

  - A SECOND cancellation of a turn — a double click on Stop, or a Stop then
    a new message (`previous.task.cancel()`) — that landed while the worker's
    `finally` awaited the queued trace writes skipped `_finalize_generation`:
    `gen.finish()` never ran, the stream never ended (keep-alives forever)
    and the generation stayed registered until the process restarted
    (test_zz_double_stop.py: gen.done=False with a 0.1 s or 0.3 s trace
    INSERT; HEAD: stream_ended=True).
  - The meter's exact count, deferred behind the answer, held a STOPPED
    turn's stream open: the queued CONTEXT_ASSEMBLED write awaited /tokenize
    (test_zz_stop_waits_tokenize.py: stream ended 2.94 s after Stop with a
    3 s count; HEAD 0.01 s).

Everything is stubbed: the engine stream, the planner, /tokenize.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import context, db, llm, main
from app.config import settings


@pytest.fixture()
def offline_chat(monkeypatch):
    from app.engines import orchestrate

    async def plan(message, history, effort):
        return orchestrate.Plan(agent=False, search=False)

    monkeypatch.setattr(orchestrate, "decide", plan)
    monkeypatch.setattr(settings, "living_knowledge_enabled", False)
    monkeypatch.setattr(settings, "fact_extraction_enabled", False)

    async def endless(messages, **_kw):
        for i in range(2000):
            await asyncio.sleep(0.02)
            yield ("token", f"t{i} ")

    monkeypatch.setattr(llm, "stream_chat_events", endless)
    yield


def _wait_for_tokens(key, n=3):
    for _ in range(1500):
        gen = main._live_generations.get(key) if key else next(iter(main._live_generations.values()), None)
        if gen is not None and sum(1 for e, _ in gen.events if e == "token") >= n:
            return gen
        time.sleep(0.01)
    raise AssertionError("the turn never streamed")


def _cleanup(client, gen):
    if not gen.done:
        client.portal.call(gen.finish)
    for k, g in list(main._live_generations.items()):
        if g is gen:
            main._live_generations.pop(k, None)


@pytest.mark.parametrize("insert_delay", [0.1, 0.3])
def test_a_second_stop_while_the_trace_writes_drain_still_ends_the_stream(offline_chat, monkeypatch, insert_delay):
    real_append, real_finish = db.append_query_trace_event, db.finish_query_trace

    def slow_append(*a, **k):
        time.sleep(insert_delay)
        return real_append(*a, **k)

    def slow_finish(*a, **k):
        time.sleep(insert_delay)
        return real_finish(*a, **k)

    monkeypatch.setattr(db, "append_query_trace_event", slow_append)
    monkeypatch.setattr(db, "finish_query_trace", slow_finish)
    out = {"ended": None}
    with TestClient(main.app) as client:
        def reader():
            client.post("/chat", json={"message": "hello there", "mode": "assistant", "effort": "fast",
                                       "session_id": "double-stop"})
            out["ended"] = time.perf_counter()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        gen = _wait_for_tokens(None)
        stopped = time.perf_counter()
        client.post("/chat/stop", json={"session_id": "double-stop"})
        time.sleep(0.25)
        client.post("/chat/stop", json={"session_id": "double-stop"})
        t.join(timeout=8.0)
        state = (gen.done, any(g is gen for g in main._live_generations.values()), out["ended"])
        _cleanup(client, gen)
        t.join(timeout=10)
    done, registered, ended = state
    assert done, "a second Stop wedged the generation: gen.finish() never ran"
    assert not registered, "the finished generation is still registered"
    assert ended is not None and ended - stopped < 5.0


@pytest.mark.parametrize("second", ["none", "stop", "send"])
def test_a_stop_ends_the_stream_at_once_when_the_meters_count_is_slow(offline_chat, monkeypatch, second):
    base_url, _k, _m = llm.resolve_model_choice("smart")
    monkeypatch.setitem(context._window_cache, base_url, 1_000_000)

    async def slow_count(base_url, model, messages):
        await asyncio.sleep(3.0)
        return 100, 1_000_000

    monkeypatch.setattr(context, "count_tokens", slow_count)
    out = {}
    key = f"stop-slow-count-{second}"
    with TestClient(main.app) as client:
        def reader():
            client.post("/chat", json={"message": "hello there", "mode": "assistant", "effort": "fast",
                                       "conversation_id": key})
            out["ended"] = time.perf_counter()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        gen = _wait_for_tokens(key)
        stopped = time.perf_counter()
        client.post("/chat/stop", json={"conversation_id": key})
        if second == "none":
            t.join(timeout=10)
        else:
            time.sleep(0.5)
        if second == "stop":
            client.post("/chat/stop", json={"conversation_id": key})
        else:
            resend = threading.Thread(
                target=lambda: client.post("/chat", json={"message": "ask again", "mode": "assistant",
                                                          "effort": "fast", "conversation_id": key}),
                daemon=True,
            )
            resend.start()
        t.join(timeout=10)
        ended, done = out.get("ended"), gen.done
        if second == "send":
            newer = main._live_generations.get(key)
            if newer is not None and newer is not gen:
                client.post("/chat/stop", json={"conversation_id": key})
                resend.join(timeout=10)
        _cleanup(client, gen)
        t.join(timeout=10)
    assert done, "the first generation never finished"
    # One Stop alone must end the stream well before the 3 s count (HEAD:
    # 0.01 s); a second action 0.5 s later must not wedge or delay it.
    limit = 0.4 if second == "none" else 1.0
    assert ended is not None and ended - stopped < limit, f"the stream ended {ended and ended - stopped:.2f} s after Stop"


@pytest.mark.parametrize("bound", [None, 1.0])
def test_a_cancellation_during_one_end_of_turn_step_runs_the_rest_and_is_raised_after(bound):
    order = []

    async def slow(name, delay):
        await asyncio.sleep(delay)
        order.append(name)

    async def worker():
        ending = main._TurnEnd()
        await ending.step(slow("flush", 0.2), bound=bound)
        await ending.step(slow("finalize", 0.05))
        order.append(("cancelled", ending.cancelled))
        if ending.cancelled:
            raise asyncio.CancelledError()

    async def scenario():
        task = asyncio.ensure_future(worker())
        await asyncio.sleep(0.05)
        task.cancel()  # the second Stop, mid-flush
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.3)  # a bounded step still lands on its own task

    asyncio.run(scenario())
    assert "finalize" in order and ("cancelled", True) in order
    assert order.index("finalize") < order.index(("cancelled", True))
    assert "flush" in order, "the interrupted step itself was not cancelled"
    if bound is None:
        # An unbounded step (the row's status) still finishes before the next.
        assert order.index("flush") < order.index("finalize")


def test_a_slow_trace_flush_holds_the_stream_for_its_bound_at_most():
    async def scenario():
        ending = main._TurnEnd()
        started = time.perf_counter()
        await ending.step(asyncio.sleep(5.0), bound=0.1)
        return time.perf_counter() - started, ending.cancelled

    elapsed, cancelled = asyncio.run(scenario())
    assert elapsed < 0.5 and not cancelled
