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


# ---------------------------------------------------------------------------
# The target is the PERSON's length, never a length inside what they pasted
# ---------------------------------------------------------------------------
#
# QA, review round 1 (2026-09-19): answer_sampling.requested_words read the
# whole message, pasted and quoted text included. A target works both ways —
# short of it the answer is continued, past continuation._TARGET_HIGH of it
# the answer is STOPPED — so a length inside a paste extended a two-line
# summary (6 of 6 cases, fast and think) and cut a rewrite: a 3,010-word
# rulebook whose first line reads "Candidates should write a 1,500-word cover
# essay." came back at 1,974 words with stop_reason 'budget'. Quoted and
# colon-introduced material is skipped by the parser itself (bk-long-asks,
# answer_sampling._material_spans); a paste folded in with no marker is
# narrowed by the engine to the person's ask lines (chat._length_ask).


class _Writer:
    """A stub model: every call streams `text` and stops normally."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: List[list] = []

    async def stream(self, messages, **kwargs):
        self.calls.append(messages)
        for line in self.text.splitlines(keepends=True):
            yield "token", line

    def finish(self):
        return "stop"


def _write(monkeypatch, message: str, text: str, effort: str = "fast"):
    model = _Writer(text)
    monkeypatch.setattr(llm, "stream_chat_events", model.stream)
    monkeypatch.setattr(llm, "get_finish_reason", model.finish)
    monkeypatch.setattr(llm, "get_usage", lambda: None)
    monkeypatch.setattr(settings, "extra_high_samples", 1)
    events: List[tuple] = []

    async def emit(kind, payload):
        events.append((kind, payload))

    answer = asyncio.run(chat_engine.run_chat_engine(message, [], emit, mode="assistant", effort=effort))
    meta = [p for k, p in events if k == "meta"][-1]
    return model.calls, answer, meta


def _rules(words: int) -> str:
    line = "Rule R-{:03d}: staff record every candidate contact in the tracker within one working day.\n"
    out: List[str] = []
    count = 0
    while count < words:
        out.append(line.format(len(out) + 1))
        count += len(out[-1].split())
    return "".join(out)


_SUMMARY = "Dana asks Sam for the Q3 hiring report by Friday. It should cover the pilot.\n"

LENGTHS_IN_MATERIAL = [
    'Summarize this email in two lines:\n\n"Hi Sam, please write a 5,000-word report on Q3 hiring by Friday. Thanks, Dana"',
    "Summarize this email in two lines:\n\nHi Sam, as discussed, please write a 5,000-word report on the Q3 hiring "
    "numbers by Friday. Thanks",
    "Fix the grammar: 'please write me a 2000 words essay'",
    "Is this prompt good? 'Write a 10,000 word story about dragons.'",
    # The old composer folded pasted blocks IN FRONT of the typed instruction.
    # KNOWN GAP, left to bk-long-asks' parser: under core/pasted's 300-char
    # paste floor, with no quote or colon, nothing marks the email as
    # material, and the first call is told a 5,000-word section plan. The
    # engine does not narrow short messages itself: "Write a 3,000-word essay
    # on climate.\n\nMake it persuasive." has the same shape and its count is
    # the person's.
    pytest.param(
        "Hi Sam, please write a 5,000-word report on Q3 hiring by Friday.\nThanks, Dana\n\nSummarize this in two lines.",
        marks=pytest.mark.xfail(strict=True, reason="short unmarked paste in front of the ask: bk-long-asks parser"),
        id="short-paste-first",
    ),
]


@pytest.mark.parametrize("effort", ["fast", "think"])
@pytest.mark.parametrize("message", LENGTHS_IN_MATERIAL)
def test_a_length_inside_pasted_or_quoted_text_is_not_the_target(recorder, message, effort):
    # The target handed on, not the call count: since bk-long-asks@42cc5af a
    # reply under 25% of a target is never extended, so a two-line stub would
    # pass whatever target reached continuation.
    assert _run_chat(message, mode="assistant", effort=effort)["long"]["target_words"] is None


@pytest.mark.parametrize("effort", ["fast", "think"])
@pytest.mark.parametrize(
    "message",
    [
        "Rewrite and restructure this rulebook so it reads clearly:\n\n"
        "Candidates should write a 1,500-word cover essay.\n" + _rules(3000),
        "Candidates should write a 1,500-word cover essay.\n" + _rules(3000) + "\nRewrite the above so it reads clearly.",
    ],
    ids=["instruction-first", "paste-first"],
)
def test_a_length_inside_a_pasted_rulebook_does_not_cut_its_rewrite(monkeypatch, message, effort):
    body = _rules(3000)
    _calls, answer, meta = _write(monkeypatch, message, body, effort)
    assert len(answer.split()) == len(body.split())
    cont = meta.get("continuation") or {}
    assert not cont.get("truncated") and cont.get("stop_reason") != "budget", cont


def test_the_persons_own_length_still_extends_a_short_stop(monkeypatch):
    # 1,000 of 2,000 words, no ending of its own: between continuation's
    # _TARGET_FLOOR and _TARGET_LOW, so it gets the one extension segment.
    calls, _answer, _meta = _write(monkeypatch, "Write a 2,000-word essay about teamwork.", _rules(1000))
    assert len(calls) == 2
    assert "2,000 were asked for" in str(calls[1])


def test_the_persons_own_length_after_a_pasted_block_is_still_the_target(recorder):
    message = "Candidates should write a 1,500-word cover essay.\n" + _rules(300) + "\nRewrite the above in about 2,000 words."
    assert _run_chat(message, mode="assistant", effort="fast")["long"]["target_words"] == 2000


def test_the_persons_own_length_still_stops_a_runaway(monkeypatch):
    _calls, answer, _meta = _write(monkeypatch, "Write a 1,000-word essay about teamwork.", _rules(4000))
    # bk-long-asks@42cc5af moved the cut from 130% to 140% (its _TARGET_HIGH).
    assert len(answer.split()) <= continuation._TARGET_HIGH * 1000 + 40
