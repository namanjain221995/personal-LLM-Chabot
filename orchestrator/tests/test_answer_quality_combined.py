"""The answer-quality slices together: where they meet in llm.py and
engines/chat.py.

- the sampling plan (core/answer_sampling.py) rides on every Fast call,
- the loop guard (core/answer_guard.py) watches the ANSWER deltas only.

Each slice has its own suite; this file pins that neither undoes the other.

THE THIRD SLICE IS GONE (2026-09-17). Adaptive thinking (PR #71) used to sit
between them: a Fast turn whose prompt the classifier read as a multi-step
reasoning task ran its first call thinking-on, bounded by a grant. Fast now
never thinks — the owner's rule, and in production every grant was a false
positive — so `engines/chat.py` opens no grant and `tests/test_adaptive_
thinking.py` is deleted. Its fake OpenAI-compatible engine lives on here,
because it is the only place in the suite that drives `llm.stream_chat_events`
at the client boundary; tests/test_chat_format_instruction.py imports it too.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import continuation, llm
from app.core import answer_sampling
from app.engines import chat

PUZZLE = (
    "I have hot water and cold water and one empty bottle whose capacity I don't "
    "know. No measuring tools. How do I fill the bottle with hot and cold water in a 2:5 ratio?"
)


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
# The plan and the guard on one turn
# ---------------------------------------------------------------------------


def test_a_fast_call_carries_its_sampling_plan_and_asks_for_no_thinking(engine):
    completions = engine(FakeStream([_chunk(content="ok", finish="stop")]))
    plan = SimpleNamespace(
        sampling={"temperature": 0.6, "top_p": 0.8, "top_k": 20, "min_p": 0.0},
        enable_thinking=None,
    )
    asyncio.run(_collect(llm.stream_chat_events(
        [{"role": "user", "content": PUZZLE}], effort="fast", temperature=0.6, max_tokens=1000,
        answer_plan=plan,
    )))
    request = completions.requests[0]
    assert _thinking(request) is False
    assert request["max_tokens"] == 1000  # the answer ceiling, with nothing added
    assert (request["temperature"], request["top_p"]) == (0.6, 0.8)
    assert (request["extra_body"]["top_k"], request["extra_body"]["min_p"]) == (20, 0.0)


def test_a_fast_puzzle_samples_by_plan_and_the_guard_stops_a_looping_answer(engine, monkeypatch):
    """Engine level: the plan on the request, no thought, and an answer that
    loops is cut by the guard."""
    monkeypatch.setattr(continuation, "budget_for", lambda effort: 8000)
    loop = "Fill the bottle with hot water and pour it out again. This doesn't help.\n"
    content = [_chunk(content="Here is the method.\n")] + [_chunk(content=loop) for _ in range(40)]
    completions = engine(FakeStream(content + [_chunk(finish="stop")]))
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    shown = asyncio.run(chat.run_chat_engine(PUZZLE, [], emit, mode="assistant", model_choice="smart", effort="fast"))
    request = completions.requests[0]
    assert len(completions.requests) == 1
    assert _thinking(request) is False
    assert request["max_tokens"] == answer_sampling.FAST_SEGMENT_MAX_TOKENS
    assert request["temperature"] == answer_sampling.LEGACY_FAST_TEMPERATURE
    assert not [k for k, _ in events if k == "reasoning"]
    meta = events[-1][1]
    assert "adaptive_thinking" not in meta
    assert "loop_guard" in meta
    assert shown == "".join(d["text"] for k, d in events if k == "token")
    assert shown.count(loop.strip()) < 5
