"""Agent engine (V2-DESIGN §3b), all offline: pydantic plan validation
(≤8 steps, kinds sql|rag|llm), retry-then-fallback planning, step events,
concurrency cap 3, and merged meta (last sql, union citations)."""
import asyncio
import json

import pytest

from app import llm
from app.engines import agent as agent_mod
from app.engines import rag as rag_mod
from app.engines import sql as sql_mod
from app.engines.agent import (
    MAX_STEPS,
    STEP_CONCURRENCY,
    AgentPlan,
    PlanStep,
    execute_steps,
    make_plan,
    merge_step_meta,
    parse_agent_plan,
    run_agent_engine,
)

# ---------------------------------------------------------------------------
# Plan JSON validation
# ---------------------------------------------------------------------------


def _plan_json(n, kind="llm"):
    return json.dumps(
        {
            "steps": [
                {"id": i, "title": f"step {i}", "kind": kind, "input": f"do {i}"}
                for i in range(1, n + 1)
            ]
        }
    )


def test_parse_valid_plan():
    plan = parse_agent_plan(_plan_json(3, kind="sql"))
    assert [s.id for s in plan.steps] == [1, 2, 3]
    assert all(s.kind == "sql" for s in plan.steps)


def test_parse_plan_handles_fences_and_think():
    raw = "<think>plan...</think>```json\n" + _plan_json(1) + "\n```"
    assert len(parse_agent_plan(raw).steps) == 1


def test_plan_rejects_more_than_max_steps():
    with pytest.raises(ValueError):
        parse_agent_plan(_plan_json(MAX_STEPS + 1))


def test_plan_rejects_empty_steps():
    with pytest.raises(ValueError):
        parse_agent_plan('{"steps": []}')


def test_plan_rejects_unknown_kind():
    with pytest.raises(ValueError):
        # "web" is a real kind now; anything outside sql/rag/llm/web is not.
        parse_agent_plan(
            '{"steps": [{"id": 1, "title": "t", "kind": "shell", "input": "x"}]}'
        )


def test_plan_rejects_duplicate_ids():
    raw = json.dumps(
        {
            "steps": [
                {"id": 1, "title": "a", "kind": "llm", "input": "x"},
                {"id": 1, "title": "b", "kind": "llm", "input": "y"},
            ]
        }
    )
    with pytest.raises(ValueError):
        parse_agent_plan(raw)


def test_plan_rejects_garbage_and_non_dict():
    for bad in ("banana", "", None, "[1, 2]", '{"steps": "nope"}'):
        with pytest.raises(ValueError):
            parse_agent_plan(bad)


def test_make_plan_retries_once_then_falls_back(monkeypatch):
    calls = []

    async def bad_planner(messages, **kwargs):
        calls.append(messages)
        return "not json at all"

    monkeypatch.setattr(llm, "chat_completion", bad_planner)
    plan = asyncio.run(make_plan("summarize the pipeline", []))
    assert len(calls) == 2  # one retry (§3b)
    assert len(plan.steps) == 1
    assert plan.steps[0].kind == "llm"  # single-step llm fallback
    assert plan.steps[0].input == "summarize the pipeline"


def test_make_plan_carries_no_effort_system_line():
    """Effort is enable_thinking now, not a prepended "Reasoning:" line."""
    from app import llm as _llm

    messages = [{"role": "user", "content": "plan it"}]
    assert _llm.apply_reasoning_effort(messages, "high", "smart") == messages



def _mixed_plan():
    return AgentPlan(
        steps=[
            PlanStep(id=1, title="Pipeline numbers", kind="sql", input="sum pipeline"),
            PlanStep(id=2, title="Renewal concerns", kind="rag", input="concerns"),
            PlanStep(id=3, title="Summarize", kind="llm", input="summarize"),
        ]
    )


def _patch_step_engines(monkeypatch, *, sql_fails=False):
    async def fake_sql(question, *, history=(), fetch_cap=None):
        if sql_fails:
            raise RuntimeError("boom: bad column")
        return "SELECT 1 AS n", ["n"], [[1]]

    async def fake_select_context(query):
        return [{"record_id": "001A", "object": "Account", "text": "note"}]

    async def fake_chat(messages, **kwargs):
        return "step prose"

    monkeypatch.setattr(sql_mod, "generate_and_run_sql", fake_sql)
    monkeypatch.setattr(rag_mod, "select_context", fake_select_context)
    monkeypatch.setattr(llm, "chat_completion", fake_chat)


def _run_execute(plan, emit):
    return asyncio.run(execute_steps(plan, [], emit))


def test_execute_emits_running_then_done_and_reuses_engines(monkeypatch):
    _patch_step_engines(monkeypatch)
    events = []

    async def emit(event, data):
        events.append((event, data))

    results = _run_execute(_mixed_plan(), emit)

    assert all(e == "step" for e, _ in events)
    by_id = {}
    for _e, data in events:
        by_id.setdefault(data["id"], []).append(data["status"])
    assert by_id == {1: ["running", "done"], 2: ["running", "done"], 3: ["running", "done"]}
    # done events carry a short detail
    done = [d for _e, d in events if d["status"] == "done"]
    assert all(d.get("detail") for d in done)
    assert [r["status"] for r in results] == ["done", "done", "done"]
    # A sql step carries its whole result, not just the SQL text: the data
    # is what a chart is drawn over, and dropping it here is what used to
    # make agent-routed answers unchartable.
    assert results[0]["meta"]["sql"] == "SELECT 1 AS n"
    assert results[0]["meta"]["data"] == [{"n": 1}]
    assert results[0]["meta"]["truncated"] is False
    assert results[1]["meta"]["citations"][0]["record_id"] == "001A"


def test_execute_failed_step_emits_failed_and_keeps_going(monkeypatch):
    _patch_step_engines(monkeypatch, sql_fails=True)
    events = []

    async def emit(event, data):
        events.append(data)

    results = _run_execute(_mixed_plan(), emit)
    sql_events = [d for d in events if d["id"] == 1]
    assert [d["status"] for d in sql_events] == ["running", "failed"]
    assert "boom" in sql_events[-1]["detail"]
    assert [r["status"] for r in results] == ["failed", "done", "done"]


def test_concurrency_capped_at_three(monkeypatch):
    state = {"current": 0, "max": 0}

    async def slow_llm(messages, **kwargs):
        state["current"] += 1
        state["max"] = max(state["max"], state["current"])
        await asyncio.sleep(0.01)
        state["current"] -= 1
        return "ok"

    monkeypatch.setattr(llm, "chat_completion", slow_llm)
    plan = AgentPlan(
        steps=[
            PlanStep(id=i, title=f"s{i}", kind="llm", input=f"i{i}")
            for i in range(1, MAX_STEPS + 1)
        ]
    )

    async def emit(event, data):
        pass

    asyncio.run(execute_steps(plan, [], emit))
    assert state["max"] <= STEP_CONCURRENCY
    assert state["max"] >= 2  # steps really did overlap


# ---------------------------------------------------------------------------
# Meta merge + full engine run
# ---------------------------------------------------------------------------


def test_merge_step_meta_last_sql_union_citations():
    plan = _mixed_plan()
    results = [
        {
            "step": plan.steps[0],
            "status": "done",
            "output": "o1",
            "meta": {"sql": "SELECT 1", "citations": [{"record_id": "001A"}]},
        },
        {
            "step": plan.steps[1],
            "status": "done",
            "output": "o2",
            "meta": {"sql": "SELECT 2", "citations": [{"record_id": "001A"}, {"record_id": "001B"}]},
        },
        {"step": plan.steps[2], "status": "failed", "output": "o3", "meta": {}},
    ]
    meta = merge_step_meta(results)
    assert meta["route"] == "agent"
    assert meta["sql"] == "SELECT 2"  # last sql wins
    assert [c["record_id"] for c in meta["citations"]] == ["001A", "001B"]  # union
    assert meta["steps"] == [
        {"id": 1, "title": "Pipeline numbers", "status": "done"},
        {"id": 2, "title": "Renewal concerns", "status": "done"},
        {"id": 3, "title": "Summarize", "status": "failed"},
    ]
    assert "report_files" not in meta


def test_run_agent_engine_end_to_end_offline(monkeypatch):
    plan_payload = json.dumps(
        {
            "steps": [
                {"id": 1, "title": "Numbers", "kind": "sql", "input": "sum it"},
                {"id": 2, "title": "Write-up", "kind": "llm", "input": "write it"},
            ]
        }
    )

    async def fake_chat(messages, **kwargs):
        # First call is the planner; later calls are llm steps.
        if any("plan multi-step" in str(m.get("content", "")) for m in messages):
            return plan_payload
        return "llm step output"

    async def fake_sql(question, *, history=(), fetch_cap=None):
        return "SELECT 42 AS answer", ["answer"], [[42]]

    async def fake_stream(messages, *, model_choice="smart", effort="medium", **kwargs):
        assert model_choice == "smart"  # synthesis uses the smart model (§3b)
        yield "reasoning", "combining"
        yield "token", "Final "
        yield "token", "answer."

    monkeypatch.setattr(llm, "chat_completion", fake_chat)
    monkeypatch.setattr(sql_mod, "generate_and_run_sql", fake_sql)
    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)

    events = []

    async def emit(event, data):
        events.append((event, data))

    answer = asyncio.run(run_agent_engine("analyze", [], emit, effort="medium"))

    assert answer == "Final answer."
    kinds = [e for e, _ in events]
    assert kinds.count("meta") == 1  # §10: exactly one meta
    assert kinds[-1] == "meta"  # after the token stream
    assert "reasoning" in kinds and "token" in kinds
    meta = events[-1][1]
    assert meta["route"] == "agent"
    assert meta["sql"] == "SELECT 42 AS answer"
    assert meta["steps"] == [
        {"id": 1, "title": "Numbers", "status": "done"},
        {"id": 2, "title": "Write-up", "status": "done"},
    ]
