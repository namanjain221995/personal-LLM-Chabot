"""Thinking budgets: OFF by default, re-enable behind an env, hang guard.

Owner decision 2026-08-19 (local deployment, no per-token cost): thinking is
UNBOUNDED by default — no client-side cutoff, no forced closure, no
thinking-off regeneration. What protects the box instead is a wall-clock
hang guard that only catches degenerate repetition loops. The full Phase 1
enforcement stays in the tree and THINKING_BUDGET_MODE=client re-enables it
exactly as built — pinned here in both directions.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import llm
from app.config import settings


# ---------------------------------------------------------------------------
# Mode mapping
# ---------------------------------------------------------------------------


def test_budgets_are_off_by_default():
    assert settings.thinking_budget_mode == "off"
    for effort in ("fast", "low", "medium", "high", "extra_high"):
        assert llm.thinking_budget(effort) is None


def test_client_mode_restores_the_phase1_mapping(monkeypatch):
    monkeypatch.setattr(settings, "thinking_budget_mode", "client")
    assert llm.thinking_budget("fast") is None
    assert llm.thinking_budget("low") is None
    assert llm.thinking_budget("medium") == settings.thinking_budget_medium
    assert llm.thinking_budget("high") == settings.thinking_budget_high
    assert llm.thinking_budget("extra_high") == settings.thinking_budget_extra_high
    assert (
        settings.thinking_budget_medium
        < settings.thinking_budget_high
        < settings.thinking_budget_extra_high
    )


def test_budgets_are_env_overridable(monkeypatch):
    monkeypatch.setattr(settings, "thinking_budget_mode", "client")
    monkeypatch.setattr(settings, "thinking_budget_high", 7777)
    assert llm.thinking_budget("high") == 7777


# ---------------------------------------------------------------------------
# Fakes for the streaming path
# ---------------------------------------------------------------------------


def _chunk(reasoning=None, content=None):
    delta = SimpleNamespace(reasoning=reasoning, content=content, model_extra=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


class FakeStream:
    def __init__(self, chunks, delay=0.0):
        self._chunks = list(chunks)
        self._delay = delay
        self.closed = False

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        if self._delay:
            await asyncio.sleep(self._delay)
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


def _fake_client(streams):
    completions = FakeCompletions(streams)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


@pytest.fixture()
def stream_env(monkeypatch):
    async def passthrough(messages, *, base_url, model, requested_max_tokens=None):
        return list(messages), requested_max_tokens if requested_max_tokens else 8192

    monkeypatch.setattr(llm.context, "fit_request", passthrough)
    holder = {}
    monkeypatch.setattr(llm, "_client", lambda base_url, api_key=None: holder["client"])
    return holder


async def _collect(gen):
    out = []
    async for kind, delta in gen:
        out.append((kind, delta))
    return out


# ---------------------------------------------------------------------------
# DEFAULT (off): unbounded thinking, generous physical ceiling, no cutoff
# ---------------------------------------------------------------------------


def test_off_mode_never_cuts_a_long_thinking_stream(stream_env, monkeypatch):
    """A stream far past every Phase 1 budget flows through untouched and no
    fallback request is ever issued."""
    long_run = [_chunk(reasoning="t")] * (settings.thinking_budget_extra_high // 4)
    stream = FakeStream(long_run + [_chunk(content="done")])
    stream_env["client"], completions = _fake_client([stream])

    events = asyncio.run(_collect(llm.stream_chat_events(
        [{"role": "user", "content": "q"}], effort="high", max_tokens=5000,
    )))
    assert len([1 for k, _ in events if k == "reasoning"]) == len(long_run)
    assert events[-1] == ("token", "done")
    assert len(completions.requests) == 1  # no forced closure, no retry
    assert stream.closed is False


def test_off_mode_floors_thinking_requests_at_max_output_tokens(stream_env):
    stream = FakeStream([_chunk(content="hi")])
    stream_env["client"], completions = _fake_client([stream])
    asyncio.run(_collect(llm.stream_chat_events(
        [{"role": "user", "content": "q"}], effort="high", max_tokens=5000,
    )))
    request = completions.requests[0]
    assert request["max_tokens"] == settings.max_output_tokens
    kwargs = request["extra_body"]["chat_template_kwargs"]
    assert kwargs["enable_thinking"] is True
    assert "thinking_token_budget" not in kwargs


def test_off_mode_leaves_non_thinking_requests_alone(stream_env):
    for effort in ("fast", "low"):
        stream = FakeStream([_chunk(content="hi")])
        stream_env["client"], completions = _fake_client([stream])
        asyncio.run(_collect(llm.stream_chat_events(
            [{"role": "user", "content": "q"}], effort=effort, max_tokens=5000,
        )))
        request = completions.requests[0]
        assert request["max_tokens"] == 5000
        assert request["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


# ---------------------------------------------------------------------------
# RE-ENABLE (client): the Phase 1 enforcement, exactly as built
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_mode(monkeypatch):
    monkeypatch.setattr(settings, "thinking_budget_mode", "client")


def test_client_mode_grows_max_tokens_by_the_budget(stream_env, client_mode):
    stream = FakeStream([_chunk(content="hi")])
    stream_env["client"], completions = _fake_client([stream])
    asyncio.run(_collect(llm.stream_chat_events(
        [{"role": "user", "content": "q"}], effort="high", max_tokens=5000,
    )))
    request = completions.requests[0]
    assert request["max_tokens"] == 5000 + settings.thinking_budget_high
    kwargs = request["extra_body"]["chat_template_kwargs"]
    assert "thinking_token_budget" not in kwargs  # server key still off


def test_client_mode_server_key_only_behind_the_proven_flag(
    stream_env, client_mode, monkeypatch
):
    monkeypatch.setattr(settings, "server_thinking_budget", True)
    stream = FakeStream([_chunk(content="hi")])
    stream_env["client"], completions = _fake_client([stream])
    asyncio.run(_collect(llm.stream_chat_events(
        [{"role": "user", "content": "q"}], effort="medium", max_tokens=1000,
    )))
    kwargs = completions.requests[0]["extra_body"]["chat_template_kwargs"]
    assert kwargs["thinking_token_budget"] == settings.thinking_budget_medium


def test_client_mode_overrun_forces_closure_and_regenerates(
    stream_env, client_mode, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "thinking_budget_medium", 10)
    monkeypatch.setattr(settings, "thinking_budget_grace", 1.2)  # cap = 12

    runaway = FakeStream([_chunk(reasoning="t")] * 40)
    fallback = FakeStream([_chunk(content="direct "), _chunk(content="answer")])
    stream_env["client"], completions = _fake_client([runaway, fallback])

    with caplog.at_level("WARNING"):
        events = asyncio.run(_collect(llm.stream_chat_events(
            [{"role": "user", "content": "q"}], effort="medium", max_tokens=500,
        )))

    kinds = [k for k, _ in events]
    assert kinds.count("reasoning") == 12
    assert "".join(d for k, d in events if k == "token") == "direct answer"
    assert runaway.closed is True
    retry = completions.requests[1]
    assert retry["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert retry["max_tokens"] == 500
    assert any("overran its budget" in r.message for r in caplog.records)


def test_client_mode_within_budget_stream_is_untouched(
    stream_env, client_mode, monkeypatch
):
    monkeypatch.setattr(settings, "thinking_budget_medium", 100)
    stream = FakeStream([_chunk(reasoning="r")] * 5 + [_chunk(content="ok")])
    stream_env["client"], completions = _fake_client([stream])
    events = asyncio.run(_collect(llm.stream_chat_events(
        [{"role": "user", "content": "q"}], effort="medium", max_tokens=500,
    )))
    assert events == [("reasoning", "r")] * 5 + [("token", "ok")]
    assert len(completions.requests) == 1


# ---------------------------------------------------------------------------
# The hang guard (both modes): kills degenerate loops, never real thinking
# ---------------------------------------------------------------------------


def test_hang_guard_kills_a_stuck_stream_and_keeps_partial_output(
    stream_env, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "gen_wall_clock_s", 0.05)
    stuck = FakeStream(
        [_chunk(content="partial ")] + [_chunk(reasoning="loop")] * 500,
        delay=0.02,
    )
    stream_env["client"], completions = _fake_client([stuck])

    with caplog.at_level("ERROR"):
        events = asyncio.run(_collect(llm.stream_chat_events(
            [{"role": "user", "content": "q"}], effort="high", max_tokens=100,
        )))

    text = "".join(d for k, d in events if k == "token")
    assert text.startswith("partial ")
    assert "wall-clock guard" in text
    assert stuck.closed is True
    assert len(completions.requests) == 1  # killed, never regenerated
    assert any("WALL CLOCK EXCEEDED" in r.message for r in caplog.records)


def test_hang_guard_covers_the_non_streaming_collector(monkeypatch):
    monkeypatch.setattr(settings, "gen_wall_clock_s", 0.05)

    async def passthrough(messages, *, base_url, model, requested_max_tokens=None):
        return list(messages), requested_max_tokens or 8192

    class Stuck:
        async def create(self, **request):
            await asyncio.sleep(10)

    monkeypatch.setattr(llm.context, "fit_request", passthrough)
    monkeypatch.setattr(
        llm, "_openai_client",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=Stuck())),
    )
    with pytest.raises(RuntimeError, match="wall clock"):
        asyncio.run(llm.chat_completion_with_reasoning(
            [{"role": "user", "content": "q"}], effort="high", max_tokens=100,
        ))


# ---------------------------------------------------------------------------
# Tools path
# ---------------------------------------------------------------------------


def _tools_env(monkeypatch, captured):
    async def passthrough(messages, *, base_url, model, requested_max_tokens=None):
        captured["requested"] = requested_max_tokens
        return list(messages), requested_max_tokens or 8192

    class OneShot:
        async def create(self, **request):
            captured["request"] = request
            message = SimpleNamespace(content="ok", tool_calls=None, model_extra=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(llm.context, "fit_request", passthrough)
    monkeypatch.setattr(
        llm, "_openai_client",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=OneShot())),
    )


def test_tools_with_thinking_get_the_unbounded_floor_by_default(monkeypatch):
    captured = {}
    _tools_env(monkeypatch, captured)
    asyncio.run(llm.chat_with_tools(
        [{"role": "user", "content": "q"}],
        tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}],
        thinking=True, effort="high", max_tokens=2000,
    ))
    assert captured["requested"] == settings.max_output_tokens
    kwargs = captured["request"].get("extra_body", {}).get("chat_template_kwargs", {})
    assert "thinking_token_budget" not in kwargs


def test_tools_with_thinking_grow_by_budget_in_client_mode(monkeypatch):
    captured = {}
    _tools_env(monkeypatch, captured)
    monkeypatch.setattr(settings, "thinking_budget_mode", "client")
    asyncio.run(llm.chat_with_tools(
        [{"role": "user", "content": "q"}],
        tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}],
        thinking=True, effort="high", max_tokens=2000,
    ))
    assert captured["requested"] == 2000 + settings.thinking_budget_high
