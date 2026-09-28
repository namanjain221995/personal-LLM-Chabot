"""A Fast pre-pass that goes to the network says so, at once (2026-09-28).

WHAT WAS MEASURED. On 2 of 20 live Fast turns the pre-pass fetched a page
(searxng, robots.txt, two page fetches, a second embed and a rerank) and the
answer's first visible character arrived at 7.8 s — the p95 of 10 s in that
sample. The status line written for exactly that case
("Checking recent sources…") sat on the `elif knowledge_task is not None`
branch of the assistant dispatch, which `prepared_early` makes unreachable:
the pre-pass is awaited earlier, under KNOWLEDGE_PREPARE_DEADLINE_S, so by the
time the dispatch runs `prepared_early` is always set and the branch is
skipped. It never fired.

Pinned here:
  - the status is emitted from the await, as soon as the pre-pass says it has
    entered the live lookup, and BEFORE the pre-pass finishes;
  - a pre-pass that never touches the network emits nothing (announcing a
    lookup that is not happening is what broke the assistant-mode event
    contract in 2026-09);
  - it is emitted once, however long the lookup runs;
  - the deadline is unchanged: past it the caller still sees TimeoutError.
"""
from __future__ import annotations

import asyncio

import pytest

from app import main as app_main


def run(coro):
    return asyncio.run(coro)


def _emitter():
    events = []

    async def emit(event, data):
        events.append((event, dict(data or {})))

    return events, emit


def test_the_status_is_emitted_while_the_lookup_is_still_running():
    events, emit = _emitter()

    async def body():
        started = asyncio.Event()
        finish = asyncio.Event()

        async def prepass():
            started.set()          # "I am fetching now"
            await finish.wait()
            return "prepared"

        task = asyncio.ensure_future(prepass())
        waiting = asyncio.ensure_future(
            app_main._await_knowledge(task, started, emit, deadline_s=5.0)
        )
        # The status must arrive before the pre-pass returns, not after it.
        for _ in range(200):
            if events:
                break
            await asyncio.sleep(0.005)
        assert events == [("status", {"text": "Checking recent sources…"})], events
        assert not task.done(), "announced only after the lookup finished"
        finish.set()
        return await waiting

    assert run(body()) == "prepared"
    assert len(events) == 1


def test_a_prepass_that_never_fetches_says_nothing():
    events, emit = _emitter()

    async def body():
        never = asyncio.Event()

        async def prepass():
            await asyncio.sleep(0.02)
            return "prepared"

        task = asyncio.ensure_future(prepass())
        return await app_main._await_knowledge(task, never, emit, deadline_s=5.0)

    assert run(body()) == "prepared"
    assert events == []


def test_the_status_is_emitted_once():
    events, emit = _emitter()

    async def body():
        started = asyncio.Event()

        async def prepass():
            started.set()
            for _ in range(6):
                await asyncio.sleep(0.005)
            return "prepared"

        task = asyncio.ensure_future(prepass())
        return await app_main._await_knowledge(task, started, emit, deadline_s=5.0)

    assert run(body()) == "prepared"
    assert len(events) == 1, events


def test_the_deadline_still_bounds_the_wait_and_leaves_the_task_alive():
    events, emit = _emitter()

    async def body():
        started = asyncio.Event()

        async def prepass():
            started.set()
            await asyncio.Event().wait()

        task = asyncio.ensure_future(prepass())
        with pytest.raises(asyncio.TimeoutError):
            await app_main._await_knowledge(task, started, emit, deadline_s=0.05)
        # The shield means the deadline does not cancel the pre-pass; the
        # caller does that, exactly as it did with asyncio.wait_for.
        assert not task.cancelled()
        task.cancel()
        return True

    assert run(body()) is True
    assert events == [("status", {"text": "Checking recent sources…"})]


def test_an_emit_failure_does_not_cost_the_grounding():
    """The status is a courtesy; a client that has gone away must not turn a
    successful pre-pass into a failed turn."""

    async def boom(event, data):
        raise RuntimeError("client gone")

    async def body():
        started = asyncio.Event()

        async def prepass():
            started.set()
            await asyncio.sleep(0.02)
            return "prepared"

        task = asyncio.ensure_future(prepass())
        return await app_main._await_knowledge(task, started, boom, deadline_s=5.0)

    assert run(body()) == "prepared"


# ── through the real handler, offline ────────────────────────────────────────

from tests.test_fast_lane_route import _parse_sse, _send, wired  # noqa: E402,F401


def test_a_fast_turn_whose_prepass_fetches_shows_the_status_before_the_answer(wired, monkeypatch):
    """The end-to-end shape the reconstruction found at 7.8 s with nothing on
    screen: the pre-pass announces its live lookup, the handler relays it, and
    the status precedes the first token."""
    import asyncio

    from fastapi.testclient import TestClient

    from app import living_knowledge, main
    from app.living_knowledge import Prepared

    async def fetching_prepare(question, *, emit=None, **kw):
        if emit is not None:
            await emit("lookup", {"query": question})  # "I am fetching now"
        await asyncio.sleep(0.05)                      # the fetch
        return Prepared()

    monkeypatch.setattr(living_knowledge, "prepare", fetching_prepare)

    with TestClient(main.app) as client:
        events, meta = _send(
            client, "status-conv-1", [{"role": "user", "content": "what changed in the latest release?"}]
        )

    kinds = [k for k, _ in events]
    statuses = [d.get("text") for k, d in events if k == "status"]
    assert "Checking recent sources…" in statuses, events
    assert kinds.index("status") < kinds.index("token"), kinds[:8]
