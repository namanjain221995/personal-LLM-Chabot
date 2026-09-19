"""An agent turn's token usage reaches the turn's ledger (hotfix 1.2, P8).

`llm._usage` is a ContextVar, and the agent engine runs inside LangGraph,
which runs every node in a COPY of the caller's context; `execute_steps`
gathers the steps as asyncio tasks, each in a copy again. Every call the
planner, the steps and the synthesis recorded died with its copy, so
`llm.get_usage()` read None after `run_agent_engine` and main.py stored an
agent turn with NULL tokens (measured on the pasted-JD Think turns: usage {}
on 3 of 3 agent runs while the engine served 5 calls each).

These tests stub every model call with one that reports known counts and pin:

- the caller's total is the planner + every step + the synthesis, added to
  what the turn already had (the orchestration call before the agent);
- None still means "not measured" when no call reported — never a zero.

Offline: no engine, no database.
"""
import asyncio
import json

from app import llm
from app.engines import agent as agent_mod
from app.engines.agent import run_agent_engine

_PLAN = json.dumps(
    {
        "steps": [
            {"id": 1, "title": "Part one", "kind": "llm", "input": "one"},
            {"id": 2, "title": "Part two", "kind": "llm", "input": "two"},
        ]
    }
)


def _stub(monkeypatch, *, report: bool):
    async def fake_chat(messages, **kwargs):
        planner = any("plan multi-step" in str(m.get("content", "")) for m in messages)
        await asyncio.sleep(0.01)  # the two steps genuinely overlap
        if report:
            llm._record_usage(100, 10) if planner else llm._record_usage(50, 5)
        return _PLAN if planner else "step output"

    async def fake_stream(messages, **kwargs):
        if report:
            llm._record_usage(200, 20)
        yield "token", "Merged answer."

    monkeypatch.setattr(llm, "chat_completion", fake_chat)
    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)


async def _emit(kind, data):
    return None


async def _turn(preset=None):
    llm.reset_usage()
    if preset:
        llm._record_usage(*preset)
    answer = await run_agent_engine(
        "do it in two parts", [], _emit, effort="think", salesforce=False, web=False
    )
    return answer, llm.get_usage()


def test_the_agent_turn_reports_every_call_it_made(monkeypatch):
    _stub(monkeypatch, report=True)
    answer, usage = asyncio.run(_turn(preset=(7, 1)))
    assert answer == "Merged answer."
    # 7/1 is the orchestration call the turn made before the agent ran.
    assert usage == {
        "prompt_tokens": 7 + 100 + 50 + 50 + 200,
        "completion_tokens": 1 + 10 + 5 + 5 + 20,
        "calls": 1 + 1 + 2 + 1,
    }


def test_an_agent_turn_with_no_reported_usage_stays_not_measured(monkeypatch):
    _stub(monkeypatch, report=False)
    _, usage = asyncio.run(_turn())
    assert usage is None


def test_the_step_concurrency_does_not_change_the_total(monkeypatch):
    """One step at a time or all at once, the same calls were made."""
    _stub(monkeypatch, report=True)
    monkeypatch.setattr(agent_mod, "STEP_CONCURRENCY", 1)
    _, usage = asyncio.run(_turn())
    assert usage == {"prompt_tokens": 400, "completion_tokens": 40, "calls": 4}
