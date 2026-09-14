"""The three answer-quality slices together: where they meet in llm.py and
engines/chat.py.

- the sampling plan (core/answer_sampling.py) rides on every Fast call,
- the adaptive-thinking grant (core/effort_policy.py) turns a reasoning
  prompt's first call thinking-on, bounded,
- the loop guard (core/answer_guard.py) watches the ANSWER deltas only.

Each slice has its own suite; this file pins that none of them undoes another.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app import continuation, llm
from app.config import settings
from app.core import answer_sampling, effort_policy
from app.engines import chat

from tests.test_adaptive_thinking import FakeStream, PUZZLE, _chunk, _collect, _thinking, engine  # noqa: F401 — fixture


def test_a_granted_call_keeps_its_sampling_plan_and_its_bounded_budget(engine):
    completions = engine(FakeStream([_chunk(reasoning="r"), _chunk(content="ok", finish="stop")]))
    plan = SimpleNamespace(sampling={"temperature": 0.6, "top_p": 0.8, "top_k": 20, "min_p": 0.0}, enable_thinking=None)
    with effort_policy.grant(700):
        asyncio.run(_collect(llm.stream_chat_events(
            [{"role": "user", "content": PUZZLE}], effort="fast", temperature=0.6, max_tokens=1000,
            answer_plan=plan,
        )))
    request = completions.requests[0]
    assert _thinking(request) is True
    assert request["max_tokens"] == 1700  # answer ceiling + the grant
    assert (request["temperature"], request["top_p"]) == (0.6, 0.8)
    assert (request["extra_body"]["top_k"], request["extra_body"]["min_p"]) == (20, 0.0)


def test_a_granted_overrun_closure_keeps_the_plans_sampling_extensions(engine, monkeypatch):
    monkeypatch.setattr(settings, "thinking_budget_grace", 1.0)
    runaway = FakeStream([_chunk(reasoning=f"t{i} ") for i in range(20)])
    answer = FakeStream([_chunk(content="final", finish="stop")])
    completions = engine(runaway, answer)
    plan = SimpleNamespace(sampling={"temperature": 0.6, "top_k": 20}, enable_thinking=None)
    with effort_policy.grant(5):
        events = asyncio.run(_collect(llm.stream_chat_events(
            [{"role": "user", "content": PUZZLE}], effort="fast", temperature=0.6, max_tokens=900,
            answer_plan=plan,
        )))
    assert "".join(d for k, d in events if k == "token") == "final"
    retry = completions.requests[1]
    assert _thinking(retry) is False
    assert retry["extra_body"]["continue_final_message"] is True
    assert retry["messages"][-1]["content"].endswith("\n</think>\n\n")
    assert retry["extra_body"]["top_k"] == 20
    assert retry["temperature"] == 0.6


def test_a_plan_that_decides_thinking_leaves_the_grant_unclaimed(engine):
    completions = engine(FakeStream([_chunk(content="ok", finish="stop")]))
    plan = SimpleNamespace(sampling={"temperature": 0.6}, enable_thinking=False)
    with effort_policy.grant(700):
        asyncio.run(_collect(llm.stream_chat_events(
            [{"role": "user", "content": PUZZLE}], effort="fast", temperature=0.6, max_tokens=1000,
            answer_plan=plan,
        )))
        assert effort_policy.claim_grant() is not None  # still unclaimed
    request = completions.requests[0]
    assert _thinking(request) is False
    assert request["max_tokens"] == 1000


def test_a_fast_puzzle_thinks_samples_by_plan_and_the_guard_stops_a_looping_answer(engine, monkeypatch):
    """Engine level: grant + plan on the request, reasoning streams untouched,
    and an answer that loops after the thought is cut by the guard."""
    monkeypatch.setattr(continuation, "budget_for", lambda effort: 8000)
    loop = "Fill the bottle with hot water and pour it out again. This doesn't help.\n"
    content = [_chunk(content="Here is the method.\n")] + [_chunk(content=loop) for _ in range(40)]
    completions = engine(FakeStream([_chunk(reasoning="thinking ")] + content + [_chunk(finish="stop")]))
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    shown = asyncio.run(chat.run_chat_engine(PUZZLE, [], emit, mode="assistant", model_choice="smart", effort="fast"))
    request = completions.requests[0]
    assert len(completions.requests) == 1
    assert _thinking(request) is True
    assert request["max_tokens"] == answer_sampling.FAST_SEGMENT_MAX_TOKENS + settings.fast_thinking_budget
    assert request["temperature"] == answer_sampling.LEGACY_FAST_TEMPERATURE
    assert [d["text"] for k, d in events if k == "reasoning"] == ["thinking "]
    meta = events[-1][1]
    assert meta["adaptive_thinking"]["budget_tokens"] == settings.fast_thinking_budget
    assert "loop_guard" in meta
    assert shown == "".join(d["text"] for k, d in events if k == "token")
    assert shown.count(loop.strip()) < 5
