"""Best-of-N token usage reaches the turn's ledger (2026-09-13, F045 follow-up).

F045 made llm.chat_completion_with_reasoning record the usage the engine
reports into the llm._usage ContextVar. core/best_of.generate_candidates runs
its candidates through asyncio.gather, and every gathered coroutine runs in
its own task with a COPY of the caller's context — so each candidate's
recorded usage died with its task and the turn read None (or only what it
had before). These tests stub the engine call with candidates that report
known counts, one that reports nothing and one that raises, and pin:

- the caller's total is the sum of every candidate that reported;
- None still means "not measured" when no candidate reported — never zero;
- a running total the caller already had is added to, not double-counted
  (the child's context copy starts WITH the caller's total);
- a parked candidate's tokens are counted before the park is re-raised.

Offline: no engine, no database. Each test fails on best_of.py as it was
before the fix (the caller's get_usage() stayed None / unchanged).
"""
import asyncio

import pytest

from app import llm
from app.continuity import QueuedForRecovery
from app.core import best_of

_Q = [{"role": "user", "content": "q"}]


def _stub_candidates(monkeypatch, plan):
    """plan[i] drives the i-th call: ("ok", prompt, completion) records and
    answers; ("silent",) answers without recording; ("raise",) raises with
    nothing recorded; ("raise_after", p, c) records then raises; ("park", p, c)
    records then parks."""
    calls = {"n": 0}

    async def fake(messages, *, effort, temperature, max_tokens):
        step = plan[calls["n"]]
        calls["n"] += 1
        await asyncio.sleep(0.01)  # let the siblings genuinely overlap
        kind = step[0]
        if kind in ("ok", "raise_after", "park"):
            llm._record_usage(step[1], step[2])
        if kind == "raise" or kind == "raise_after":
            raise RuntimeError("backend hiccup")
        if kind == "park":
            raise QueuedForRecovery(1.0)
        return "thought", "answer"

    monkeypatch.setattr(llm, "chat_completion_with_reasoning", fake)


async def _turn(n, *, preset=None):
    llm.reset_usage()
    if preset:
        llm._record_usage(*preset)
    candidates = await best_of.generate_candidates(
        _Q, n=n, temperature=0.3, max_tokens=100
    )
    return candidates, llm.get_usage()


def test_the_turn_usage_is_the_sum_of_every_candidate_that_reported(monkeypatch):
    _stub_candidates(
        monkeypatch,
        [("ok", 100, 40), ("silent",), ("raise",), ("ok", 7, 3)],
    )
    candidates, usage = asyncio.run(_turn(4))
    assert [c.usable for c in candidates] == [True, True, False, True]
    assert usage == {"prompt_tokens": 107, "completion_tokens": 43, "calls": 2}
    # The silent and the failed-before-reporting candidates carry None, not zero.
    assert candidates[1].usage is None
    assert candidates[2].usage is None


def test_no_candidate_reporting_leaves_the_turn_not_measured_rather_than_zero(monkeypatch):
    _stub_candidates(monkeypatch, [("silent",), ("raise",), ("silent",)])
    _, usage = asyncio.run(_turn(3))
    assert usage is None


def test_a_running_total_the_turn_already_had_is_added_to_not_counted_twice(monkeypatch):
    # Each candidate task's context copy starts with the caller's 50/5 — a fix
    # that summed the children's totals without resetting them would add it
    # once per candidate.
    _stub_candidates(monkeypatch, [("ok", 10, 1), ("ok", 20, 2), ("ok", 30, 3)])
    _, usage = asyncio.run(_turn(3, preset=(50, 5)))
    assert usage == {"prompt_tokens": 110, "completion_tokens": 11, "calls": 4}


def test_a_candidate_that_reported_and_then_failed_still_counts_its_tokens(monkeypatch):
    _stub_candidates(monkeypatch, [("raise_after", 12, 0), ("ok", 8, 4)])
    candidates, usage = asyncio.run(_turn(2))
    assert candidates[0].error and not candidates[0].usable
    assert usage == {"prompt_tokens": 20, "completion_tokens": 4, "calls": 2}


def test_tokens_spent_before_a_park_are_on_the_ledger_when_the_park_is_raised(monkeypatch):
    _stub_candidates(monkeypatch, [("ok", 9, 9), ("park", 1, 0)])
    seen = {}

    async def run():
        llm.reset_usage()
        with pytest.raises(QueuedForRecovery):
            await best_of.generate_candidates(_Q, n=2, temperature=0.3, max_tokens=100)
        seen["usage"] = llm.get_usage()

    asyncio.run(run())
    assert seen["usage"] == {"prompt_tokens": 10, "completion_tokens": 9, "calls": 2}
