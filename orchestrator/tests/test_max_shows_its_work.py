"""Max does not go silent while it plans.

MEASURED 2026-09-28 on this box, three runs per effort, interleaved, load
5-7, through the app's real ASGI stack over a real socket — time from the
request to the FIRST EVENT OF EACH KIND:

  FAST   first token (the answer)        341 ms
  THINK  first reasoning               1,574 ms
  MAX    first step                    1,267 ms
         first reasoning              16,823 ms
         first answer token           16,823 ms

Max emits a step line at 1.3 s and then NOTHING for 15.5 seconds. Reasoning
and the answer arrive together, which is the tell: nothing before the draft
was streaming anything at all.

The cause is NOT reasoning being discarded. `_plan` runs with thinking OFF
deliberately — measured on the owner's own prompt, thinking ON cost 71.4 s
for a plan that was worse — so there is no reasoning to show. The cause is
that the plan itself, a readable 421-word account of how the answer will be
structured, came from a NON-STREAMING call and was shown to nobody until it
was finished.

These tests pin the delivery, not the wall clock: a latency test that waits
for a real model is a test nobody runs.
"""
from __future__ import annotations

import asyncio
from typing import List

import pytest

from app import llm
from app.core import max_loop as M

PLAN_PIECES = ["1. Executive Summary", " — scope.\n", "2. Architecture", " — two nodes.\n"]


def _fake_stream(recorder: List[dict]):
    async def stream(messages, **kw):
        recorder.append(dict(kw))
        for piece in PLAN_PIECES:
            yield ("token", piece)
    return stream


def test_the_plan_streams_when_a_sink_is_given(monkeypatch):
    calls: List[dict] = []
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream(calls))

    seen: List[str] = []

    async def go():
        async def sink(text: str) -> None:
            seen.append(text)
        return await M._plan("q", "", "assistant", on_text=sink)

    plan = asyncio.run(go())
    assert seen == PLAN_PIECES, "the plan did not arrive delta by delta"
    assert "".join(seen).strip() == plan, "what was shown is not what was planned"


def test_the_streamed_plan_keeps_thinking_off_and_the_same_ceiling(monkeypatch):
    """Same model, same temperature, same ceiling, same thinking-off: only the
    DELIVERY changes. `effort="fast"` is how the streaming API spells the
    `thinking=False` of the non-streaming call."""
    calls: List[dict] = []
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream(calls))

    async def go():
        async def sink(_t: str) -> None:
            return None
        await M._plan("q", "", "assistant", on_text=sink)

    asyncio.run(go())
    assert len(calls) == 1
    assert calls[0]["effort"] == "fast", "thinking must stay off; it cost 71.4 s when on"
    assert calls[0]["max_tokens"] == M.PLAN_MAX_TOKENS
    assert calls[0]["temperature"] == 0.2


def test_without_a_sink_the_call_is_the_non_streaming_one_it_always_was(monkeypatch):
    """Every offline caller and every test that does not care about deltas
    keeps the old call, byte for byte."""
    used = {}

    async def fake_chat(messages, **kw):
        used.update(kw)
        return "a plan"

    monkeypatch.setattr(llm, "chat_completion", fake_chat)

    async def boom(*_a, **_k):
        raise AssertionError("the streaming path ran without a sink")
        yield  # pragma: no cover

    monkeypatch.setattr(llm, "stream_chat_events", boom)

    plan = asyncio.run(M._plan("q", "", "assistant"))
    assert plan == "a plan"
    assert used["thinking"] is False
    assert used["max_tokens"] == M.PLAN_MAX_TOKENS


def test_a_sink_that_raises_does_not_lose_the_plan(monkeypatch):
    """The panel is a nicety; the plan is what the draft is built from."""
    calls: List[dict] = []
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream(calls))

    async def go():
        async def bad(_t: str) -> None:
            raise RuntimeError("the client went away")
        return await M._plan("q", "", "assistant", on_text=bad)

    plan = asyncio.run(go())
    assert plan.startswith("1. Executive Summary")


def test_the_plan_goes_to_the_thinking_panel_and_never_into_the_answer():
    """`sink` in `run()` tees into `pieces`, which becomes `result.text`. The
    plan must not go through it, or the structure notes would be prepended to
    the answer the person reads."""
    import ast
    import pathlib

    source = pathlib.Path(M.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_plan_delta"
    )
    # The BODY, with the docstring dropped — the prose explains why the answer's
    # sink is not used and would otherwise trip the check below.
    body = [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    code = "\n".join(ast.unparse(n) for n in body)
    assert "emit('reasoning'" in code or 'emit("reasoning"' in code, code
    assert "sink" not in code, f"the plan must not go through the answer's sink: {code}"
    assert len(body) == 1, f"the plan sink should do one thing: {code}"
    # And the call that produces it passes the reasoning sink, not the answer's.
    call = source[source.index("result.plan = await _plan"):]
    call = call[: call.index("\n")]
    assert "on_text=_plan_delta" in call
