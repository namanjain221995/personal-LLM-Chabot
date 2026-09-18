"""engines/chat.py + continuation.py — how the Fast sampling decision reaches
the model call (answer-quality C8/C9).

`llm.stream_chat_events` is replaced by a recorder, so these pin the ARGUMENTS
each effort produces, not engine behaviour:

- Fast assistant prose: one call (segment 8,000 = total 8,000), temperature
  0.6, and the legacy plan on the call;
- Fast long-form and structured asks may continue: total 64,000;
- Fast Salesforce: 8,000 per call (was 6,000), structured;
- Think and Max: exactly the old arguments and NO answer_plan keyword at all;
- continuation hands the plan to every segment, and omits the keyword when
  there is none.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from app import continuation, llm
from app.config import settings
from app.engines import chat as chat_engine


class _Recorder:
    def __init__(self, script=None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.script = list(script or [("ok", "stop")])
        self.reason = None

    async def stream(self, messages, **kwargs):
        text, reason = self.script[min(len(self.calls), len(self.script) - 1)]
        self.calls.append(dict(kwargs))
        yield "token", text
        self.reason = reason

    def finish(self):
        return self.reason


@pytest.fixture()
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(llm, "stream_chat_events", rec.stream)
    monkeypatch.setattr(llm, "get_finish_reason", rec.finish)
    monkeypatch.setattr(llm, "get_usage", lambda: None)
    monkeypatch.setattr(settings, "extra_high_samples", 1)
    return rec


def _run_chat(message: str, **kwargs) -> dict:
    events: List[tuple] = []
    long_kwargs: Dict[str, Any] = {}
    real = continuation.stream_long_completion

    async def spy(messages, **kw):
        long_kwargs.update(kw)
        return await real(messages, **kw)

    async def emit(kind, payload):
        events.append((kind, payload))

    async def run():
        continuation.stream_long_completion = spy
        try:
            return await chat_engine.run_chat_engine(message, [], emit, **kwargs)
        finally:
            continuation.stream_long_completion = real

    answer = asyncio.run(run())
    return {"answer": answer, "long": long_kwargs, "events": events}


def test_fast_prose_is_one_legacy_call(recorder):
    out = _run_chat("hey, what's a good name for a grey kitten?", mode="assistant", effort="fast")
    assert out["answer"] == "ok"
    long = out["long"]
    assert (long["segment_max_tokens"], long["total_max_tokens"], long["temperature"]) == (8000, 1_000_000, 0.6)
    plan = long["answer_plan"]
    assert plan.sampling == {"temperature": 0.6} and plan.shape == "prose"
    [call] = recorder.calls
    assert call["answer_plan"] is plan
    assert call["temperature"] == 0.6 and call["max_tokens"] == 8000


def test_low_is_fast(recorder):
    out = _run_chat("hi there", mode="assistant", effort="low")
    assert out["long"]["answer_plan"].shape == "prose"
    assert out["long"]["total_max_tokens"] == 1_000_000


def test_fast_prose_that_runs_out_of_room_continues_to_the_ceiling(recorder, monkeypatch):
    # Owner decision 2026-09-15: a Fast prose call that spends its 8,000 tokens
    # and hits `length` is continued, up to the 1M system ceiling, until the
    # model says it is done (a 50-problem coding answer had been cut at 8,000).
    recorder.script = [("a" * 50, "length"), ("b" * 50, "stop")]
    monkeypatch.setattr(llm, "get_usage", lambda: {"completion_tokens": 8000 * len(recorder.calls)})
    out = _run_chat("tell me everything about tea", mode="assistant", effort="fast")
    assert len(recorder.calls) == 2
    assert out["long"]["total_max_tokens"] == 1_000_000


@pytest.mark.parametrize(
    "message, shape",
    [("write a python script that renames files", "longform"), ("a markdown table of the planets", "structured")],
)
def test_fast_longform_and_structured_may_continue(recorder, message, shape):
    out = _run_chat(message, mode="assistant", effort="fast")
    long = out["long"]
    assert long["answer_plan"].shape == shape
    assert (long["segment_max_tokens"], long["total_max_tokens"]) == (8000, 1_000_000)


def test_fast_salesforce_is_structured(recorder):
    out = _run_chat("show my open opportunities", mode="salesforce", effort="fast")
    long = out["long"]
    assert long["answer_plan"].shape == "structured"
    assert (long["segment_max_tokens"], long["total_max_tokens"], long["temperature"]) == (8000, 1_000_000, 0.6)


@pytest.mark.parametrize("effort, mode, max_tokens", [
    # Salesforce mode used to be capped at the small-talk ceiling on every
    # turn; since 2026-09-18 only actual small talk is (this question is a
    # maths proof, so it gets the full room in either mode).
    ("think", "salesforce", 16000),
    ("max", "assistant", 16000),
])
def test_think_and_max_are_unchanged(recorder, effort, mode, max_tokens):
    out = _run_chat("prove there are infinitely many primes", mode=mode, effort=effort)
    long = out["long"]
    assert "answer_plan" not in long
    assert long["segment_max_tokens"] == max_tokens
    assert long["temperature"] == 0.3
    assert long["total_max_tokens"] == continuation.budget_for(effort)
    for call in recorder.calls:
        assert "answer_plan" not in call


def test_continuation_passes_the_plan_to_every_segment(recorder):
    recorder.script = [("first part of the answer " * 3, "length"), ("second part", "stop")]
    plan = object()

    async def run():
        async def on_delta(kind, text):
            pass

        return await continuation.stream_long_completion(
            [{"role": "user", "content": "go"}], on_delta=on_delta, effort="fast",
            segment_max_tokens=100, total_max_tokens=10_000, answer_plan=plan,
        )

    asyncio.run(run())
    assert len(recorder.calls) == 2
    assert all(call["answer_plan"] is plan for call in recorder.calls)


def test_continuation_without_a_plan_sends_no_keyword(recorder):
    async def run():
        async def on_delta(kind, text):
            pass

        return await continuation.stream_long_completion(
            [{"role": "user", "content": "go"}], on_delta=on_delta, effort="fast",
            segment_max_tokens=100, total_max_tokens=100,
        )

    asyncio.run(run())
    assert "answer_plan" not in recorder.calls[0]


def test_a_lower_operator_fast_budget_still_wins(recorder, monkeypatch):
    monkeypatch.setattr(settings, "continuation_budget_fast", 20000)
    out = _run_chat("write a python script that renames files", mode="assistant", effort="fast")
    assert out["long"]["total_max_tokens"] == 20000
    out = _run_chat("hello there", mode="assistant", effort="fast")
    assert out["long"]["total_max_tokens"] == 20000  # prose too: the operator's lower budget is the total


def test_the_legacy_temperature_line_is_kept_verbatim():
    # tests/test_effort_depth.py pins this exact source line.
    import inspect

    assert 'temperature = 0.3 if effort in ("think", "max") else 0.6' in inspect.getsource(chat_engine.run_chat_engine)


# ---------------------------------------------------------------------------
# The requested length reaches continuation as a target (backlog 14)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("effort", ["fast", "think"])
@pytest.mark.parametrize(
    "message",
    [
        "Produce a 12,000 word employee manual for a recruiting agency.",
        "Write a 1200-word blog post about remote onboarding.",
    ],
)
def test_the_requested_length_reaches_continuation_as_the_target(recorder, message, effort):
    """"10,000 words" came back as 24,364 words one run and 5,340 the next:
    continuation had the budget but never the length that was asked for.
    The parse is answer_sampling's; the engine only has to hand it on, at
    every effort."""
    from app.core import answer_sampling

    wanted = answer_sampling.requested_words(message)
    assert wanted is not None and wanted >= 1000
    out = _run_chat(message, mode="assistant", effort=effort)
    assert out["long"]["target_words"] == wanted


def test_an_ask_with_no_length_hands_on_no_target(recorder):
    from app.core import answer_sampling

    message = "hey, what's a good name for a grey kitten?"
    assert answer_sampling.requested_words(message) is None
    out = _run_chat(message, mode="assistant", effort="fast")
    assert out["long"]["target_words"] is None
