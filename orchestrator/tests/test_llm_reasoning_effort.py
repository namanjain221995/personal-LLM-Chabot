"""How hard a thinking call thinks, and the guard that keeps it legal.

2026-09-30: the main model became nvidia/Qwen3.8-27B-NVFP4. Its chat template
(rev 482ca0f3) reads `reasoning_effort` whenever thinking is on and accepts
exactly xhigh | medium | low (xhigh when absent); any other value is a
`raise_exception` inside the template, i.e. an HTTP 400 for the whole request.
vLLM's own top-level `reasoning_effort` field overrides that key whenever it
is not null, in a vocabulary (none|minimal|low|medium|high|xhigh|max) four of
whose values the template refuses.

The owner's levels: Fast = thinking off, Think = medium, Max = xhigh (best-of-N
candidates are Max calls). A thinking call that names no level thinks at
medium — the template's own default would be xhigh on every helper call.

Every assertion here is on what reaches `_primary_send` / `_open_stream` (or,
for the guard, what reaches the client), i.e. what would go on the wire.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from app import llm
from app.core import best_of

MSGS = [{"role": "user", "content": "q"}]


@pytest.fixture(autouse=True)
def _no_fast_turn_leaks():
    llm.mark_fast_turn(False)
    yield
    llm.mark_fast_turn(False)


class _Stream:
    def __init__(self):
        self._done = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._done:
            raise StopAsyncIteration
        self._done = True
        delta = SimpleNamespace(reasoning=None, content="ok", model_extra=None)
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="stop")], usage=None)

    async def close(self):
        return None


def _message():
    msg = SimpleNamespace(content='{"winner": 1}', reasoning=None, model_extra=None, tool_calls=[])
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=None)


@pytest.fixture()
def sends(monkeypatch):
    """Every request the entry points hand to the choke point."""
    recorded: list = []

    async def passthrough(messages, *, base_url, model, requested_max_tokens=None, **kw):
        return list(messages), requested_max_tokens or 8192

    monkeypatch.setattr(llm.context, "fit_request", passthrough)
    monkeypatch.setattr(llm, "_openai_client", lambda *a, **kw: object())
    monkeypatch.setattr(llm, "_client", lambda *a, **kw: object())

    async def primary_send(client, request, **kw):
        recorded.append(dict(request))
        return _message()

    async def open_stream(client, request, **send):
        recorded.append(dict(request))
        return _Stream()

    monkeypatch.setattr(llm, "_primary_send", primary_send)
    monkeypatch.setattr(llm, "_open_stream", open_stream)
    return recorded


def _template(request) -> dict:
    return request["extra_body"]["chat_template_kwargs"]


async def _drain(gen):
    return [item async for item in gen]


# ---------------------------------------------------------------------------
# 1. Our levels -> the template's
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "effort,expected",
    [
        (None, "medium"), ("", "medium"), ("think", "medium"), ("medium", "medium"),
        # Legacy stored preferences keep their chat meaning: "high" was Think.
        ("high", "medium"), ("banana", "medium"),
        ("max", "xhigh"), ("extra_high", "xhigh"), ("xhigh", "xhigh"), ("MAX", "xhigh"),
        ("fast", "low"), ("low", "low"),
    ],
)
def test_every_level_maps_to_a_value_the_template_accepts(effort, expected):
    assert llm.template_reasoning_effort(effort) == expected
    assert expected in llm.TEMPLATE_REASONING_EFFORTS


def test_the_accepted_set_is_the_templates_own():
    assert llm.TEMPLATE_REASONING_EFFORTS == ("xhigh", "medium", "low")


# ---------------------------------------------------------------------------
# 2. Fast off, Think medium, Max xhigh — on every entry point
# ---------------------------------------------------------------------------


def test_the_three_picker_levels_on_the_answer_stream(sends):
    async def scenario():
        for effort in ("fast", "think", "max"):
            await _drain(llm.stream_chat_events(MSGS, effort=effort, max_tokens=1200))

    asyncio.run(scenario())
    fast, think, top = (_template(r) for r in sends)
    # Fast: thinking off, and no level at all — the template never reads one.
    assert fast == {"enable_thinking": False}
    assert think == {"enable_thinking": True, "reasoning_effort": "medium"}
    assert top == {"enable_thinking": True, "reasoning_effort": "xhigh"}


def test_a_thinking_call_that_names_no_level_thinks_at_medium_not_the_template_default(sends):
    """Left alone the template would think at xhigh on every helper call."""

    async def scenario():
        await llm.chat_completion(MSGS, max_tokens=1200, thinking=True)
        await _drain(llm.stream_chat_completion(MSGS, max_tokens=1200, thinking=True))
        await llm.json_completion(MSGS, max_tokens=1200, thinking=True)
        await llm.chat_completion_with_reasoning(MSGS, max_tokens=1200)
        await llm.chat_with_tools(
            MSGS, tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
            max_tokens=1200, thinking=True,
        )

    asyncio.run(scenario())
    assert len(sends) == 5
    for request in sends:
        assert _template(request) == {"enable_thinking": True, "reasoning_effort": "medium"}, request


def test_an_explicit_max_reaches_every_entry_point_that_takes_one(sends):
    async def scenario():
        await llm.chat_completion_with_reasoning(MSGS, max_tokens=1200, effort="max")
        await llm.json_completion(MSGS, max_tokens=1200, thinking=True, effort="max")
        await llm.chat_with_tools(
            MSGS, tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
            max_tokens=1200, thinking=True, effort="max",
        )

    asyncio.run(scenario())
    assert [_template(r)["reasoning_effort"] for r in sends] == ["xhigh", "xhigh", "xhigh"]


def test_best_of_n_candidates_think_at_the_highest_level(sends):
    candidate = asyncio.run(best_of._generate_one(1, MSGS, temperature=0.7, max_tokens=1200))
    assert candidate.error == ""
    (request,) = sends
    assert _template(request) == {"enable_thinking": True, "reasoning_effort": "xhigh"}


def test_a_fast_turn_sends_no_level_whatever_the_caller_asks(sends):
    async def scenario():
        llm.mark_fast_turn(True)
        await _drain(llm.stream_chat_events(MSGS, effort="max", max_tokens=1200))
        await llm.chat_completion_with_reasoning(MSGS, max_tokens=1200, effort="max")
        await llm.json_completion(MSGS, max_tokens=1200, thinking=True, effort="max")

    asyncio.run(scenario())
    for request in sends:
        assert _template(request) == {"enable_thinking": False}, request


# ---------------------------------------------------------------------------
# 3. The guard at the choke point: nothing illegal reaches the wire
# ---------------------------------------------------------------------------


def _with(effort=..., top=...) -> dict:
    kwargs = {"enable_thinking": True}
    if effort is not ...:
        kwargs["reasoning_effort"] = effort
    request = {"model": "m", "messages": MSGS, "extra_body": {"chat_template_kwargs": kwargs}}
    if top is not ...:
        request["reasoning_effort"] = top
    return request


@pytest.mark.parametrize("value", ["xhigh", "medium", "low"])
def test_a_legal_request_is_returned_untouched(value):
    request = _with(value)
    assert llm._guard_reasoning_effort(request) is request
    bare = {"model": "m", "messages": MSGS}
    assert llm._guard_reasoning_effort(bare) is bare


@pytest.mark.parametrize(
    "value,expected",
    [("high", "xhigh"), ("max", "xhigh"), ("HIGH", "xhigh"), ("extra_high", "xhigh"), ("minimal", "low"),
     (" Medium ", "medium")],
)
def test_a_foreign_level_becomes_the_closest_template_value(value, expected):
    guarded = llm._guard_reasoning_effort(_with(value))
    assert _template(guarded) == {"enable_thinking": True, "reasoning_effort": expected}


@pytest.mark.parametrize("value", ["none", "off"])
def test_no_reasoning_means_thinking_off_not_a_level(value):
    guarded = llm._guard_reasoning_effort(_with(value))
    assert _template(guarded) == {"enable_thinking": False}


def test_a_null_or_unreadable_level_is_dropped_so_the_template_default_applies(caplog):
    assert _template(llm._guard_reasoning_effort(_with(None))) == {"enable_thinking": True}
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        guarded = llm._guard_reasoning_effort(_with("turbo"))
    assert _template(guarded) == {"enable_thinking": True}
    assert "turbo" in caplog.text


def test_a_top_level_reasoning_effort_never_reaches_vllm():
    """vLLM lets the top-level field override the template key — in a
    vocabulary the template refuses — so it is taken off and translated."""
    guarded = llm._guard_reasoning_effort(_with(top="high"))
    assert "reasoning_effort" not in guarded
    assert _template(guarded) == {"enable_thinking": True, "reasoning_effort": "xhigh"}
    # A level the call already names wins over the forwarded one.
    guarded = llm._guard_reasoning_effort(_with("medium", top="max"))
    assert "reasoning_effort" not in guarded
    assert _template(guarded) == {"enable_thinking": True, "reasoning_effort": "medium"}
    # With no chat_template_kwargs at all, "none" is thinking off.
    bare = {"model": "m", "messages": MSGS, "reasoning_effort": "none"}
    assert llm._guard_reasoning_effort(bare) == {
        "model": "m", "messages": MSGS, "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }


def test_the_guard_never_edits_the_callers_dicts():
    request = _with("high", top="max")
    snapshot = {**request, "extra_body": {"chat_template_kwargs": dict(_template(request))}}
    llm._guard_reasoning_effort(request)
    assert request == snapshot


def test_the_choke_point_runs_the_guard_before_the_client_sees_the_request(monkeypatch):
    seen: list = []

    class _Completions:
        async def create(self, **request):
            seen.append(request)
            return _message()

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))

    async def resilient(op, **kw):
        return await op()

    monkeypatch.setattr(llm, "resilient", resilient)
    # A sidecar URL: no breaker, no admission lane — the guard still applies.
    asyncio.run(llm._primary_send(client, _with("max", top="high"), what="t", base_url="http://sidecar:1/v1"))
    (request,) = seen
    assert "reasoning_effort" not in request
    assert request["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True, "reasoning_effort": "xhigh"}
