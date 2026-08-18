"""Thinking budgets: the mode→params mapping and the client-side guillotine.

Phase 1 claims pinned here:
- each effort maps to exactly one thinking budget (fast/low: none), derived
  from the measured 46.6 tok/s and env-overridable;
- the budget GROWS the request's max_tokens so reasoning cannot starve the
  answer;
- the server-side budget key is sent ONLY behind the off-by-default flag —
  this vLLM build ignores it (measured 2026-08-19), so client-side counting
  is the enforcement;
- a stream that overruns budget × grace is force-closed and the answer is
  regenerated with thinking OFF, on the caller's original answer ceiling.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import llm
from app.config import settings


def test_thinking_budget_maps_each_effort_once():
    assert llm.thinking_budget("fast") is None
    assert llm.thinking_budget("low") is None
    assert llm.thinking_budget("medium") == settings.thinking_budget_medium
    assert llm.thinking_budget("high") == settings.thinking_budget_high
    assert llm.thinking_budget("extra_high") == settings.thinking_budget_extra_high
    # Ordered: more effort never buys less thinking.
    assert (
        settings.thinking_budget_medium
        < settings.thinking_budget_high
        < settings.thinking_budget_extra_high
    )


def test_budgets_are_env_overridable(monkeypatch):
    monkeypatch.setattr(settings, "thinking_budget_high", 7777)
    assert llm.thinking_budget("high") == 7777


# ---------------------------------------------------------------------------
# Fakes for the streaming path
# ---------------------------------------------------------------------------


def _chunk(reasoning=None, content=None):
    delta = SimpleNamespace(reasoning=reasoning, content=content, model_extra=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


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
    """Records every request; serves streams from a queue."""

    def __init__(self, streams):
        self.requests = []
        self._streams = list(streams)

    async def create(self, **request):
        self.requests.append(request)
        return self._streams.pop(0)


def _fake_client(streams):
    completions = FakeCompletions(streams)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


@pytest.fixture()
def stream_env(monkeypatch):
    """stream_chat_events against fakes: no network, pass-through sizing."""

    async def passthrough(messages, *, base_url, model, requested_max_tokens=None):
        return list(messages), requested_max_tokens if requested_max_tokens else 8192

    monkeypatch.setattr(llm.context, "fit_request", passthrough)

    holder = {}

    def client_factory(base_url, api_key=None):
        return holder["client"]

    monkeypatch.setattr(llm, "_client", client_factory)
    return holder


async def _collect(gen):
    out = []
    async for kind, delta in gen:
        out.append((kind, delta))
    return out


# ---------------------------------------------------------------------------
# Request shaping
# ---------------------------------------------------------------------------


def test_thinking_budget_grows_max_tokens(stream_env):
    stream = FakeStream([_chunk(content="hi")])
    stream_env["client"], completions = _fake_client([stream])

    asyncio.run(_collect(llm.stream_chat_events(
        [{"role": "user", "content": "q"}], effort="high", max_tokens=5000,
    )))
    request = completions.requests[0]
    assert request["max_tokens"] == 5000 + settings.thinking_budget_high
    # Thinking is ON for high and no server-side budget key rides along —
    # the flag is off because this build provably ignores it.
    kwargs = request["extra_body"]["chat_template_kwargs"]
    assert kwargs["enable_thinking"] is True
    assert "thinking_token_budget" not in kwargs


def test_fast_and_low_neither_think_nor_grow(stream_env):
    for effort in ("fast", "low"):
        stream = FakeStream([_chunk(content="hi")])
        stream_env["client"], completions = _fake_client([stream])
        asyncio.run(_collect(llm.stream_chat_events(
            [{"role": "user", "content": "q"}], effort=effort, max_tokens=5000,
        )))
        request = completions.requests[0]
        assert request["max_tokens"] == 5000
        assert request["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_server_budget_key_only_behind_the_proven_flag(stream_env, monkeypatch):
    monkeypatch.setattr(settings, "server_thinking_budget", True)
    stream = FakeStream([_chunk(content="hi")])
    stream_env["client"], completions = _fake_client([stream])
    asyncio.run(_collect(llm.stream_chat_events(
        [{"role": "user", "content": "q"}], effort="medium", max_tokens=1000,
    )))
    kwargs = completions.requests[0]["extra_body"]["chat_template_kwargs"]
    assert kwargs["thinking_token_budget"] == settings.thinking_budget_medium


# ---------------------------------------------------------------------------
# The guillotine
# ---------------------------------------------------------------------------


def test_overrun_forces_closure_and_answers_without_thinking(
    stream_env, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "thinking_budget_medium", 10)
    monkeypatch.setattr(settings, "thinking_budget_grace", 1.2)  # cap = 12

    runaway = FakeStream([_chunk(reasoning="t")] * 40)  # never stops thinking
    fallback = FakeStream([_chunk(content="direct "), _chunk(content="answer")])
    stream_env["client"], completions = _fake_client([runaway, fallback])

    with caplog.at_level("WARNING"):
        events = asyncio.run(_collect(llm.stream_chat_events(
            [{"role": "user", "content": "q"}], effort="medium", max_tokens=500,
        )))

    # Exactly cap (12) reasoning deltas got through, then the answer arrived
    # from the thinking-off retry.
    kinds = [k for k, _ in events]
    assert kinds.count("reasoning") == 12
    assert "".join(d for k, d in events if k == "token") == "direct answer"
    # The runaway stream was actually closed, not abandoned.
    assert runaway.closed is True
    # The retry runs thinking-OFF on the caller's original answer ceiling.
    retry = completions.requests[1]
    assert retry["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert retry["max_tokens"] == 500
    assert any("overran its budget" in r.message for r in caplog.records)


def test_within_budget_stream_is_untouched(stream_env, monkeypatch):
    monkeypatch.setattr(settings, "thinking_budget_medium", 100)
    stream = FakeStream(
        [_chunk(reasoning="r")] * 5 + [_chunk(content="ok")]
    )
    stream_env["client"], completions = _fake_client([stream])
    events = asyncio.run(_collect(llm.stream_chat_events(
        [{"role": "user", "content": "q"}], effort="medium", max_tokens=500,
    )))
    assert events == [("reasoning", "r")] * 5 + [("token", "ok")]
    assert len(completions.requests) == 1  # no fallback issued


# ---------------------------------------------------------------------------
# Tools path: generous sizing, never a server-side cut
# ---------------------------------------------------------------------------


def test_tools_with_thinking_get_the_budget_added_to_max_tokens(monkeypatch):
    captured = {}

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

    asyncio.run(llm.chat_with_tools(
        [{"role": "user", "content": "q"}],
        tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}],
        thinking=True, effort="high", max_tokens=2000,
    ))
    assert captured["requested"] == 2000 + settings.thinking_budget_high
    # No server-side thinking budget key ever rides on a tools request.
    kwargs = captured["request"].get("extra_body", {}).get("chat_template_kwargs", {})
    assert "thinking_token_budget" not in kwargs
