"""The fact extractor must not race the answer's prefill for the GPU.

WHY THIS FILE EXISTS. `facts.remember_after_route` calls the ROUTER model,
which is resident on the same head Spark as rank 0 of the TP=2 main model.
Starting it concurrently with the answer meant the answer's PREFILL and the
router's completion fought for that GPU. Measured 2026-09-22, idle-gated, on
the owner's own shapes:

    ordinary question          time to first token  674.2 ms  ->  403.7 ms
    question over three PDFs                      1,729.7 ms  ->  966.6 ms

(the "after" column is the extractor switched off entirely — the price tag,
not the fix). The same request body replayed straight at vLLM seconds later
took 224 ms against the turn's 480 ms, and the effect reproduces with no
orchestrator at all: 225.7 ms alone, 485.8 ms with one router completion
started 5 ms earlier, 4 of 4 runs.

The fix is a second gate: extraction waits for the answer's first token, so
the contention lands on decode, where there is room for it, instead of on
prefill. What is extracted does not change — only when it starts.

These tests pin the three things that can go wrong: that the wait happens,
that it can never lose a person's memory, and that an artifact turn still
extracts nothing.
"""
from __future__ import annotations

import asyncio

from app import facts


def _gate(value: bool) -> "asyncio.Future[bool]":
    fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
    fut.set_result(value)
    return fut


def _pending() -> "asyncio.Future[None]":
    return asyncio.get_running_loop().create_future()


def test_extraction_waits_for_the_answers_first_token(monkeypatch):
    async def _run():
        """The router is not touched while the answer is still prefilling."""
        started = _pending()
        ran = asyncio.Event()

        async def _fake_remember(*args, **kwargs):
            ran.set()
            return [{"fact": "the user is called Sam"}]

        monkeypatch.setattr(facts, "remember_from_message", _fake_remember)

        task = asyncio.create_task(
            facts.remember_after_route(_gate(True), 1, "my name is Sam", "c", after=started)
        )
        # Give the task every chance to run ahead of the first token.
        for _ in range(10):
            await asyncio.sleep(0)
        assert not ran.is_set(), "the extractor called the router during the answer's prefill"

        started.set_result(None)
        assert await asyncio.wait_for(task, 5) == [{"fact": "the user is called Sam"}]
        assert ran.is_set()

    asyncio.run(_run())

def test_a_first_token_that_never_comes_does_not_lose_the_memory(monkeypatch):
    async def _run():
        """The wait is a safety bound, not a schedule.

        Every path out of the chat worker resolves the future in its `finally`,
        so this only fires if a future path forgets to. Losing a person's saved
        memory because a wait never ended would be worse than the contention the
        wait exists to avoid, so the extractor gives up waiting and runs.
        """
        monkeypatch.setattr(facts, "FIRST_TOKEN_WAIT_MAX_S", 0.05)

        async def _fake_remember(*args, **kwargs):
            return [{"fact": "saved anyway"}]

        monkeypatch.setattr(facts, "remember_from_message", _fake_remember)

        never = _pending()  # nothing will ever resolve it
        assert await asyncio.wait_for(
            facts.remember_after_route(_gate(True), 1, "hi", "c", after=never), 5
        ) == [{"fact": "saved anyway"}]

    asyncio.run(_run())

def test_an_already_started_answer_is_not_waited_on(monkeypatch):
    async def _run():
        """A resolved future costs the extractor nothing: no wait, no timeout."""
        done = _pending()
        done.set_result(None)

        async def _fake_remember(*args, **kwargs):
            return [{"fact": "x"}]

        monkeypatch.setattr(facts, "remember_from_message", _fake_remember)
        monkeypatch.setattr(facts, "FIRST_TOKEN_WAIT_MAX_S", 0.0)  # would fire if awaited

        assert await facts.remember_after_route(_gate(True), 1, "hi", "c", after=done) == [
            {"fact": "x"}
        ]

    asyncio.run(_run())

def test_an_artifact_turn_still_extracts_nothing(monkeypatch):
    async def _run():
        """CONTRACT-2 section 8, unchanged by the second gate: a request for a
        file is a task, not a fact, and must never reach the model or the table
        — even though its answer tokens would resolve `after`."""
        called = False

        async def _fake_remember(*args, **kwargs):  # pragma: no cover - must not run
            nonlocal called
            called = True
            return [{"fact": "should never be saved"}]

        monkeypatch.setattr(facts, "remember_from_message", _fake_remember)

        started = _pending()
        started.set_result(None)
        assert await facts.remember_after_route(
            _gate(False), 1, "make me a PDF of the audit", "c", after=started
        ) == []
        assert not called

    asyncio.run(_run())

def test_the_gate_order_is_route_then_answer(monkeypatch):
    async def _run():
        """The route gate is still first: an artifact turn must not be held on
        the answer's first token before it can decline to extract."""
        async def _fake_remember(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("an artifact turn reached the extractor")

        monkeypatch.setattr(facts, "remember_from_message", _fake_remember)
        monkeypatch.setattr(facts, "FIRST_TOKEN_WAIT_MAX_S", 30.0)

        never = _pending()
        assert await asyncio.wait_for(
            facts.remember_after_route(_gate(False), 1, "make a deck", "c", after=never), 1
        ) == []

    asyncio.run(_run())

def test_without_the_second_gate_nothing_changes(monkeypatch):
    async def _run():
        """`after=None` is the old behaviour, for every caller that is not the
        chat worker."""
        async def _fake_remember(*args, **kwargs):
            return [{"fact": "y"}]

        monkeypatch.setattr(facts, "remember_from_message", _fake_remember)
        assert await facts.remember_after_route(_gate(True), 1, "hi", "c") == [{"fact": "y"}]

    asyncio.run(_run())
