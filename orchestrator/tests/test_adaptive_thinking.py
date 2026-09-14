"""Fast adaptive thinking end to end: where the decision is applied.

Two hunks carry it (core/effort_policy.py has the classifier and the grant):

- engines/chat.py `run_chat_engine` classifies a thinking-off turn and runs its
  generation inside `effort_policy.grant(FAST_THINKING_BUDGET)`, recording the
  decision on the query trace and in `fast_adaptive_thinking_total`;
- llm.py `stream_chat_events`, right where `enable_thinking` is decided, turns
  thinking on for a granted thinking-off call, BOUNDED: the budget is added to
  the answer ceiling and an overrun closes the thought and answers from it.

Everything without a grant — Think/Max, /v1, JSON and tool calls — sends the
request it sent before; that is pinned here too.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import llm, metrics
from app.config import settings
from app.core import effort_policy
from app.core import tracing as query_tracing
from app.engines import chat

PUZZLE = (
    "I have hot water and cold water and one empty bottle whose capacity I don't "
    "know. No measuring tools. How do I fill the bottle with hot and cold water in a 2:5 ratio?"
)
LOOKUP = "iPhone 15 price in India"


# ---------------------------------------------------------------------------
# A fake OpenAI-compatible engine, at the client boundary
# ---------------------------------------------------------------------------


def _chunk(reasoning=None, content=None, finish=None):
    delta = SimpleNamespace(reasoning=reasoning, content=content, model_extra=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish)], usage=None
    )


class FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self):
        self.closed = True


class FakeCompletions:
    def __init__(self, streams):
        self.requests = []
        self._streams = list(streams)

    async def create(self, **request):
        self.requests.append(request)
        return self._streams.pop(0)


@pytest.fixture()
def engine(monkeypatch):
    """Install a fake engine; returns a function that loads its streams."""

    async def passthrough(messages, *, base_url, model, requested_max_tokens=None):
        return list(messages), requested_max_tokens if requested_max_tokens else 8192

    monkeypatch.setattr(llm.context, "fit_request", passthrough)
    holder = {}
    monkeypatch.setattr(llm, "_client", lambda base_url, api_key=None, **kw: holder["client"])

    def load(*streams):
        completions = FakeCompletions(streams)
        holder["client"] = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        return completions

    return load


async def _collect(gen):
    return [item async for item in gen]


def _thinking(request) -> bool:
    return request["extra_body"]["chat_template_kwargs"]["enable_thinking"]


# ---------------------------------------------------------------------------
# llm.stream_chat_events: the apply hunk
# ---------------------------------------------------------------------------


def test_fast_without_a_grant_sends_the_request_it_always_sent(engine):
    completions = engine(FakeStream([_chunk(content="ok")]))
    asyncio.run(_collect(llm.stream_chat_events(
        [{"role": "user", "content": "q"}], effort="fast", temperature=0.6, max_tokens=8000,
    )))
    request = completions.requests[0]
    assert request["max_tokens"] == 8000
    assert _thinking(request) is False
    assert request["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_a_grant_turns_fast_thinking_on_with_the_budget_added_to_the_ceiling(engine):
    # THINKING_BUDGET_MODE stays at its default "off": a grant is bounded anyway.
    assert settings.thinking_budget_mode == "off"
    completions = engine(FakeStream([_chunk(reasoning="r"), _chunk(content="ok")]))
    with effort_policy.grant(700, "ratio"):
        events = asyncio.run(_collect(llm.stream_chat_events(
            [{"role": "user", "content": "q"}], effort="fast", temperature=0.6, max_tokens=8000,
        )))
    request = completions.requests[0]
    assert _thinking(request) is True
    assert request["max_tokens"] == 8000 + 700  # the answer is never starved
    assert request["temperature"] == 0.6        # sampling is not this hunk's business
    assert "thinking_token_budget" not in request["extra_body"]["chat_template_kwargs"]
    assert events == [("reasoning", "r"), ("token", "ok")]


def test_a_grant_applies_to_the_legacy_fast_model_choice_too(engine):
    completions = engine(FakeStream([_chunk(content="ok")]))
    with effort_policy.grant(300):
        asyncio.run(_collect(llm.stream_chat_events(
            [{"role": "user", "content": "q"}], model_choice="fast", effort="think", max_tokens=100,
        )))
    assert _thinking(completions.requests[0]) is True
    assert completions.requests[0]["max_tokens"] == 400


@pytest.mark.parametrize("effort", ["think", "max"])
def test_a_grant_changes_nothing_for_a_turn_that_already_thinks(engine, effort):
    without = engine(FakeStream([_chunk(content="ok")]))
    asyncio.run(_collect(llm.stream_chat_events(
        [{"role": "user", "content": "q"}], effort=effort, max_tokens=5000,
    )))
    with_grant = engine(FakeStream([_chunk(content="ok")]))
    with effort_policy.grant(10):
        asyncio.run(_collect(llm.stream_chat_events(
            [{"role": "user", "content": "q"}], effort=effort, max_tokens=5000,
        )))
    assert with_grant.requests[0] == without.requests[0]
    assert without.requests[0]["max_tokens"] == settings.max_output_tokens  # unbounded floor


def test_a_granted_overrun_closes_the_thought_and_answers_from_it(engine, monkeypatch, caplog):
    monkeypatch.setattr(settings, "thinking_budget_grace", 1.5)  # cap = 15
    runaway = FakeStream([_chunk(reasoning=f"t{i} ") for i in range(40)])
    answer = FakeStream([_chunk(content="final "), _chunk(content="answer", finish="stop")])
    completions = engine(runaway, answer)
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": PUZZLE}]
    with caplog.at_level("WARNING"), effort_policy.grant(10):
        events = asyncio.run(_collect(llm.stream_chat_events(
            messages, effort="fast", temperature=0.6, max_tokens=900,
        )))

    reasoning = [d for k, d in events if k == "reasoning"]
    assert len(reasoning) == 15
    assert "".join(d for k, d in events if k == "token") == "final answer"
    assert runaway.closed is True
    assert any("overran its budget" in r.message for r in caplog.records)

    retry = completions.requests[1]
    # Same prompt, plus the assistant turn holding the CLOSED thought.
    assert retry["messages"][:-1] == completions.requests[0]["messages"]
    closing = retry["messages"][-1]
    assert closing["role"] == "assistant"
    assert closing["content"].startswith("<think>\n" + "".join(reasoning).strip())
    assert effort_policy.THOUGHT_CLOSURE in closing["content"]
    assert closing["content"].endswith("\n</think>\n\n")
    assert _thinking(retry) is False
    assert retry["extra_body"]["continue_final_message"] is True
    assert retry["extra_body"]["add_generation_prompt"] is False
    assert retry["max_tokens"] == 900  # the answer's own ceiling


def test_an_ungranted_overrun_keeps_the_phase1_regeneration(engine, monkeypatch):
    """Budgeted Think (THINKING_BUDGET_MODE=client) is not adaptive thinking:
    its forced closure still re-asks the identical prompt thinking-off."""
    monkeypatch.setattr(settings, "thinking_budget_mode", "client")
    monkeypatch.setattr(settings, "thinking_budget_high", 10)
    monkeypatch.setattr(settings, "thinking_budget_grace", 1.2)
    runaway = FakeStream([_chunk(reasoning="t")] * 40)
    fallback = FakeStream([_chunk(content="direct")])
    completions = engine(runaway, fallback)
    asyncio.run(_collect(llm.stream_chat_events(
        [{"role": "user", "content": "q"}], effort="think", max_tokens=500,
    )))
    retry = completions.requests[1]
    assert retry["messages"] == completions.requests[0]["messages"]
    assert "continue_final_message" not in retry["extra_body"]
    assert _thinking(retry) is False


def test_a_granted_overrun_on_a_continuation_keeps_the_existing_prefix(engine, monkeypatch):
    """A continuation segment already extends an assistant message; the retry
    extends that same prefix rather than stacking a second assistant turn."""
    monkeypatch.setattr(settings, "thinking_budget_grace", 1.0)
    runaway = FakeStream([_chunk(reasoning="t")] * 20)
    fallback = FakeStream([_chunk(content="more")])
    completions = engine(runaway, fallback)
    messages = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "partial"}]
    with effort_policy.grant(5):
        asyncio.run(_collect(llm.stream_chat_events(
            messages, effort="fast", max_tokens=500, continue_final_message=True,
        )))
    retry = completions.requests[1]
    assert retry["messages"] == completions.requests[0]["messages"]
    assert retry["extra_body"]["continue_final_message"] is True


# ---------------------------------------------------------------------------
# engines/chat.py: the decision hunk
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.events = []

    async def event(self, stage, **kwargs):
        self.events.append((stage, kwargs))


def _run_chat(message, *, effort="fast", mode="assistant", model_choice="smart"):
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    recorder = _Recorder()

    async def main():
        token = query_tracing._current.set(recorder)
        try:
            return await chat.run_chat_engine(
                message, [], emit, mode=mode, model_choice=model_choice, effort=effort,
            )
        finally:
            query_tracing._current.reset(token)

    answer = asyncio.run(main())
    return answer, events, recorder


def _counter(decision, reason):
    key = (("decision", decision), ("reason", reason))
    return metrics._counters.get("fast_adaptive_thinking_total", {}).get(key, 0.0)


def test_a_fast_reasoning_prompt_thinks_streams_reasoning_and_is_recorded(engine):
    completions = engine(FakeStream([
        _chunk(reasoning="Seven parts in all. "),
        _chunk(content="Fill it ", finish=None),
        _chunk(content="like this.", finish="stop"),
    ]))
    reason = effort_policy.classify(PUZZLE).reason
    before = _counter("think", reason)

    answer, events, recorder = _run_chat(PUZZLE)

    request = completions.requests[0]
    assert _thinking(request) is True
    assert request["max_tokens"] == 8000 + settings.fast_thinking_budget
    assert answer == "Fill it like this."
    assert ("reasoning", {"text": "Seven parts in all. "}) in events
    meta = [d for k, d in events if k == "meta"]
    assert meta == [{
        "route": "chat",
        "adaptive_thinking": {"reason": reason, "budget_tokens": settings.fast_thinking_budget},
    }]
    assert _counter("think", reason) == before + 1
    stages = [(stage, kw) for stage, kw in recorder.events if stage == "ADAPTIVE_THINKING"]
    assert len(stages) == 1
    details = stages[0][1]["details"]
    assert details["think"] is True and details["reason"] == reason
    assert details["budget_tokens"] == settings.fast_thinking_budget
    assert "bottle" not in repr(details)  # names only, never the prompt
    assert effort_policy.current_grant() is None  # the grant ended with the turn


def test_a_fast_lookup_stays_thinking_off_and_its_turn_is_unchanged(engine):
    completions = engine(FakeStream([_chunk(content="About 70,000.", finish="stop")]))
    before = _counter("direct", "no_signal")
    answer, events, recorder = _run_chat(LOOKUP)
    request = completions.requests[0]
    assert _thinking(request) is False
    assert request["max_tokens"] == 8000
    assert [d for k, d in events if k == "meta"] == [{"route": "chat"}]
    assert not [k for k, _ in events if k == "reasoning"]
    assert _counter("direct", "no_signal") == before + 1
    details = [kw["details"] for stage, kw in recorder.events if stage == "ADAPTIVE_THINKING"][0]
    assert details["think"] is False and details["budget_tokens"] == 0


def test_salesforce_chat_at_fast_is_judged_the_same_way(engine):
    completions = engine(FakeStream([_chunk(content="hello", finish="stop")]))
    _run_chat("hi there", mode="salesforce")
    assert _thinking(completions.requests[0]) is False


@pytest.mark.parametrize("effort", ["think", "max"])
def test_turns_that_already_think_are_never_classified(engine, monkeypatch, effort):
    monkeypatch.setattr(settings, "extra_high_samples", 1)  # the single-stream path

    def must_not_classify(text):  # pragma: no cover - failing is the assertion
        raise AssertionError("classified a turn that already thinks")

    monkeypatch.setattr(effort_policy, "classify", must_not_classify)
    completions = engine(FakeStream([_chunk(content="ok", finish="stop")]))
    _, events, recorder = _run_chat(PUZZLE, effort=effort)
    assert _thinking(completions.requests[0]) is True
    assert "adaptive_thinking" not in [d for k, d in events if k == "meta"][0]
    assert not [s for s, _ in recorder.events if s == "ADAPTIVE_THINKING"]


def test_the_switch_returns_fast_to_thinking_off(engine, monkeypatch):
    monkeypatch.setattr(settings, "fast_adaptive_thinking", False)
    completions = engine(FakeStream([_chunk(content="ok", finish="stop")]))
    _, events, recorder = _run_chat(PUZZLE)
    assert _thinking(completions.requests[0]) is False
    assert completions.requests[0]["max_tokens"] == 8000
    assert [d for k, d in events if k == "meta"] == [{"route": "chat"}]
    assert not recorder.events


def test_the_budget_setting_sizes_the_grant(engine, monkeypatch):
    monkeypatch.setattr(settings, "fast_thinking_budget", 1234)
    completions = engine(FakeStream([_chunk(content="ok", finish="stop")]))
    _run_chat(PUZZLE)
    assert completions.requests[0]["max_tokens"] == 8000 + 1234


def test_the_grant_is_released_when_the_generation_fails(engine, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("engine down")

    monkeypatch.setattr(chat.continuation, "stream_long_completion", boom)
    engine()
    with pytest.raises(RuntimeError):
        _run_chat(PUZZLE)
    assert effort_policy.current_grant() is None


def _grant_after_in_the_same_context(emit, *, expect):
    """Run the engine awaited DIRECTLY (same context, not a task, not a fresh
    asyncio.run) and report the grant seen right after it ends — a grant that
    was not reset would still be visible here."""
    seen = {}

    async def main():
        with pytest.raises(expect):
            await chat.run_chat_engine(PUZZLE, [], emit, mode="assistant", model_choice="smart", effort="fast")
        seen["grant"] = effort_policy.current_grant()

    asyncio.run(main())
    return seen["grant"]


def test_the_grant_is_released_in_the_callers_context_when_the_generation_fails(engine, monkeypatch):
    async def boom(*args, **kwargs):
        assert effort_policy.current_grant() is not None  # it WAS in force
        raise RuntimeError("engine down")

    monkeypatch.setattr(chat.continuation, "stream_long_completion", boom)
    engine()

    async def emit(kind, data):
        return None

    assert _grant_after_in_the_same_context(emit, expect=RuntimeError) is None


def test_stop_mid_reasoning_releases_the_grant_and_closes_the_engine_stream(engine):
    """A person pressing Stop cancels the turn while it is still thinking."""
    stream = FakeStream([
        _chunk(reasoning="Seven parts. "),
        _chunk(reasoning="Two hot. "),
        _chunk(content="never reached", finish="stop"),
    ])
    completions = engine(stream)
    events = []

    async def emit(kind, data):
        events.append((kind, data))
        if kind == "reasoning":
            raise asyncio.CancelledError()

    assert _grant_after_in_the_same_context(emit, expect=asyncio.CancelledError) is None
    assert _thinking(completions.requests[0]) is True
    assert events == [("reasoning", {"text": "Seven parts. "})]
    assert stream.closed  # the engine request is not left generating
    assert not [k for k, _ in events if k in ("token", "meta")]


def test_the_metric_labels_are_closed(monkeypatch):
    metrics.inc("fast_adaptive_thinking_total", decision="think", reason="not-a-reason")
    key = (("decision", "think"), ("reason", "other"))
    assert metrics._counters["fast_adaptive_thinking_total"].get(key, 0) >= 1


# ---------------------------------------------------------------------------
# One thought per grant (verifier 2026-09-15)
# ---------------------------------------------------------------------------


def test_a_grant_pays_for_one_thought_later_calls_in_its_scope_stay_thinking_off(engine):
    completions = engine(
        FakeStream([_chunk(reasoning="r"), _chunk(content="a", finish="stop")]),
        FakeStream([_chunk(content="b", finish="stop")]),
    )
    msgs = [{"role": "user", "content": "q"}]
    with effort_policy.grant(700, "ratio"):
        asyncio.run(_collect(llm.stream_chat_events(msgs, effort="fast", max_tokens=900)))
        asyncio.run(_collect(llm.stream_chat_events(msgs, effort="fast", max_tokens=900)))
        assert effort_policy.current_grant() is not None  # still in scope
    first, second = completions.requests
    assert _thinking(first) is True and first["max_tokens"] == 900 + 700
    assert _thinking(second) is False and second["max_tokens"] == 900


def test_a_long_fast_answer_thinks_once_not_once_per_continuation_segment(engine, monkeypatch):
    """Live 2026-09-15: the answer written after a thought hit its ceiling, the
    continuation's second segment thought AGAIN (another ~3,000 tokens) and the
    turn took 91 s instead of 38 s."""
    monkeypatch.setattr(settings, "continuation_budget_fast", 100_000)
    monkeypatch.setattr(settings, "continuation_deadline_s", 0)
    long_text = "word " * 30
    completions = engine(
        FakeStream([_chunk(reasoning="Seven parts. "), _chunk(content=long_text, finish="length")]),
        FakeStream([_chunk(content="and the rest of the answer here.", finish="stop")]),
    )
    answer, events, _ = _run_chat(PUZZLE)
    assert len(completions.requests) == 2
    assert _thinking(completions.requests[0]) is True
    assert completions.requests[0]["max_tokens"] == 8000 + settings.fast_thinking_budget
    assert _thinking(completions.requests[1]) is False
    assert completions.requests[1]["max_tokens"] <= 8000
    assert [d["text"] for k, d in events if k == "reasoning"] == ["Seven parts. "]
    assert answer.endswith("rest of the answer here.")
    meta = [d for k, d in events if k == "meta"][0]
    assert meta["continuation"]["segments"] == 2
    assert meta["adaptive_thinking"]["budget_tokens"] == settings.fast_thinking_budget
