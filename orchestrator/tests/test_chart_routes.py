"""Charts across the routes that can produce them, end to end and offline.

Three things are pinned here:

  1. A direct SQL answer and an AGENT-routed answer produce the same chart
     for the same question. They did not: `merge_step_meta` carried only
     `sql` forward, so an agent-routed result had no `meta.data` and
     therefore no chart at all.
  2. Exactly ONE meta frame per turn, on both routes (§10) — the frontend
     replaces meta wholesale, so a second frame loses the first.
  3. A chart failure never ends the stream. The answer, the SQL and the
     table all still arrive.
"""
import asyncio
import json

import duckdb
import pytest

from app.config import settings
from app.engines import agent as agent_engine
from app.engines import sql as sql_engine


@pytest.fixture()
def warehouse(tmp_path, monkeypatch):
    db_path = str(tmp_path / "warehouse.duckdb")
    con = duckdb.connect(db_path)
    con.execute("CREATE TABLE opportunities (stage VARCHAR, total BIGINT)")
    con.execute(
        # Deliberately NOT in trusted stage order — the SQL's own ORDER BY
        # is by total, which is what the Data tab must keep showing.
        "INSERT INTO opportunities VALUES "
        "('Qualification', 7), ('Prospecting', 10), ('Closed Won', 3)"
    )
    con.close()
    monkeypatch.setattr(settings, "duckdb_path", db_path)
    return db_path


SQL_TEXT = "SELECT stage, total FROM opportunities"


@pytest.fixture()
def offline_llm(monkeypatch):
    """SQL writer + chart designer + narrative, all offline."""

    async def fake_chat_completion(messages, **kwargs):
        system = messages[0].get("content", "")
        if "chart" in system.lower():
            return '{"type":"bar","x_key":"stage","y_keys":["total"],"title":"By stage"}'
        return SQL_TEXT

    async def fake_stream(messages, **kwargs):
        yield "Three stages."

    async def fake_stream_events(messages, **kwargs):
        yield "token", "Three stages."

    monkeypatch.setattr(sql_engine.llm, "chat_completion", fake_chat_completion)
    monkeypatch.setattr(sql_engine.llm, "stream_chat_completion", fake_stream)
    monkeypatch.setattr(agent_engine.llm, "chat_completion", fake_chat_completion)
    monkeypatch.setattr(agent_engine.llm, "stream_chat_events", fake_stream_events)


def collect(coro):
    events = []

    async def emit(event, data):
        events.append((event, data))

    answer = asyncio.run(coro(emit))
    return answer, events


def metas(events):
    return [d for e, d in events if e == "meta"]


# ---------------------------------------------------------------------------
# Direct SQL route
# ---------------------------------------------------------------------------


def test_direct_sql_route_charts_an_explicit_request(warehouse, offline_llm):
    _, events = collect(
        lambda emit: sql_engine.run_sql_engine("bar chart of total by stage", [], emit)
    )
    meta = metas(events)[0]
    assert len(metas(events)) == 1
    assert meta["chart"]["type"] == "bar"
    assert meta["chart"]["x_key"] == "stage"
    assert meta["data"][0]["stage"] == "Qualification"


def test_direct_sql_route_leaves_an_ordinary_question_unCharted(
    warehouse, offline_llm, monkeypatch
):
    # This asserts EXPLICIT-mode gating, so pin the mode: the deployed .env
    # sets CHART_TRIGGER_MODE=hybrid, and a suite run that inherits it made
    # this fail as a phantom regression (Phase 0 baseline, 2026-08-19).
    monkeypatch.setattr(settings, "chart_trigger_mode", "explicit")
    _, events = collect(
        lambda emit: sql_engine.run_sql_engine("how many per stage", [], emit)
    )
    assert "chart" not in metas(events)[0]


def test_a_funnel_request_ships_stage_ordered_rows_alongside_the_table(
    warehouse, offline_llm
):
    _, events = collect(
        lambda emit: sql_engine.run_sql_engine("show the opportunity funnel", [], emit)
    )
    meta = metas(events)[0]
    assert meta["chart"]["type"] == "funnel"
    # The chart is drawn in TRUSTED stage order...
    assert [r["stage"] for r in meta["chart_data"]] == [
        "Prospecting",
        "Qualification",
        "Closed Won",
    ]
    # ...and the Data tab still shows what the SQL actually returned.
    assert [r["stage"] for r in meta["data"]] == [
        "Qualification",
        "Prospecting",
        "Closed Won",
    ]
    assert "sql" in meta


# ---------------------------------------------------------------------------
# Agent route — the gap this change closes
# ---------------------------------------------------------------------------


def _sql_plan():
    return agent_engine.AgentPlan(
        steps=[agent_engine.PlanStep(id=1, title="Pipeline", kind="sql",
                                     input="total by stage")]
    )


def test_agent_routed_sql_carries_the_chart_all_the_way_to_meta(warehouse, offline_llm):
    """THE BUG: merge_step_meta forwarded only `sql`, so the identical
    question answered through the agent produced no chart."""
    plan = _sql_plan()

    async def go(emit):
        results = await agent_engine.execute_steps(
            plan, [], emit, True, "medium", "bar chart of total by stage"
        )
        return agent_engine.merge_step_meta(results)

    meta = asyncio.run(go(lambda *_: asyncio.sleep(0)))
    assert meta["route"] == "agent"
    assert meta["sql"] == SQL_TEXT
    assert meta["chart"]["type"] == "bar"
    assert len(meta["data"]) == 3
    assert meta["truncated"] is False


def test_the_agent_and_direct_routes_agree_on_the_chart(warehouse, offline_llm):
    question = "bar chart of total by stage"
    _, events = collect(lambda emit: sql_engine.run_sql_engine(question, [], emit))
    direct = metas(events)[0]

    async def go(emit):
        results = await agent_engine.execute_steps(
            _sql_plan(), [], emit, True, "medium", question
        )
        return agent_engine.merge_step_meta(results)

    routed = asyncio.run(go(lambda *_: asyncio.sleep(0)))
    assert routed["chart"] == direct["chart"]
    assert routed["data"] == direct["data"]


def test_chart_intent_is_read_from_the_user_not_the_planner_step(
    warehouse, offline_llm, monkeypatch
):
    """A plan step's input is written by the planner ("Count opportunities
    grouped by stage") and does not contain the word the user typed."""
    # Explicit-mode assertion — pin the mode against the ambient .env
    # (CHART_TRIGGER_MODE=hybrid would legitimately chart the plain question).
    monkeypatch.setattr(settings, "chart_trigger_mode", "explicit")
    plan = agent_engine.AgentPlan(
        steps=[agent_engine.PlanStep(id=1, title="Pipeline", kind="sql",
                                     input="Count opportunities grouped by stage")]
    )

    async def go(message):
        results = await agent_engine.execute_steps(
            plan, [], lambda *_: asyncio.sleep(0), True, "medium", message
        )
        return agent_engine.merge_step_meta(results)

    charted = asyncio.run(go("give me a bar chart of the pipeline"))
    plain = asyncio.run(go("how many opportunities per stage"))
    assert "chart" in charted
    assert "chart" not in plain


def test_a_non_sql_step_never_donates_sql_data_to_the_merged_meta():
    """Carrying the payload key-by-key would let a rag step's meta and a
    sql step's meta be stitched into one result that never existed."""
    steps = [
        agent_engine.PlanStep(id=1, title="a", kind="sql", input="x"),
        agent_engine.PlanStep(id=2, title="b", kind="rag", input="y"),
    ]
    results = [
        {"step": steps[0], "status": "done", "output": "o",
         "meta": {"sql": "SELECT 1", "data": [{"n": 1}],
                  "chart": {"type": "bar", "x_key": "n", "y_keys": ["n"],
                            "title": "", "stacked": False}}},
        {"step": steps[1], "status": "done", "output": "o",
         "meta": {"citations": [{"record_id": "001A"}]}},
    ]
    meta = agent_engine.merge_step_meta(results)
    assert meta["chart"]["x_key"] == "n"
    assert meta["data"] == [{"n": 1}]
    assert meta["citations"][0]["record_id"] == "001A"


def test_the_last_sql_step_wins_as_a_whole():
    steps = [
        agent_engine.PlanStep(id=i, title=str(i), kind="sql", input="x")
        for i in (1, 2)
    ]
    results = [
        {"step": steps[0], "status": "done", "output": "o",
         "meta": {"sql": "SELECT 1", "data": [{"a": 1}],
                  "chart": {"type": "bar", "x_key": "a", "y_keys": ["a"],
                            "title": "first", "stacked": False}}},
        {"step": steps[1], "status": "done", "output": "o",
         "meta": {"sql": "SELECT 2", "data": [{"b": 2}]}},
    ]
    meta = agent_engine.merge_step_meta(results)
    assert meta["sql"] == "SELECT 2"
    assert meta["data"] == [{"b": 2}]
    # The first step's chart must NOT survive onto the second step's rows.
    assert "chart" not in meta


def test_a_plan_with_no_sql_step_has_no_sql_keys():
    step = agent_engine.PlanStep(id=1, title="s", kind="llm", input="x")
    meta = agent_engine.merge_step_meta(
        [{"step": step, "status": "done", "output": "o", "meta": {}}]
    )
    for key in ("sql", "data", "truncated", "chart", "chart_data"):
        assert key not in meta


# ---------------------------------------------------------------------------
# SSE safety: a chart failure must not end the stream
# ---------------------------------------------------------------------------


# Two dimensions and two measures: no single obvious reading, so this is
# the one shape that reaches the model.
AMBIGUOUS_SQL = (
    "SELECT stage, total, stage AS owner, total * 2 AS amount FROM opportunities"
)


def test_the_answer_completes_when_the_chart_model_raises(warehouse, monkeypatch):
    async def fake_chat_completion(messages, **kwargs):
        if "chart" in messages[0].get("content", "").lower():
            raise RuntimeError("vLLM refused the chart call")
        return AMBIGUOUS_SQL

    async def fake_stream(messages, **kwargs):
        yield "Three stages."

    monkeypatch.setattr(sql_engine.llm, "chat_completion", fake_chat_completion)
    monkeypatch.setattr(sql_engine.llm, "stream_chat_completion", fake_stream)

    answer, events = collect(
        # Four columns' worth of ambiguity is what forces the model path.
        lambda emit: sql_engine.run_sql_engine("chart this", [], emit)
    )
    kinds = [e for e, _ in events]
    assert answer == "Three stages."
    assert "token" in kinds
    assert kinds.count("meta") == 1
    meta = metas(events)[0]
    assert "chart" not in meta          # no chart...
    assert meta["sql"] == AMBIGUOUS_SQL  # ...but answer, SQL and table survive
    assert len(meta["data"]) == 3


def test_only_allowlisted_sse_events_are_emitted(warehouse, offline_llm):
    """`sse_event()` raises on an unlisted name from INSIDE the stream, so
    an unregistered event kills the answer with no error frame. The chart
    work adds no new event — it rides the existing meta."""
    from app.sse import ALL_EVENTS

    _, events = collect(
        lambda emit: sql_engine.run_sql_engine("bar chart of total by stage", [], emit)
    )
    assert {e for e, _ in events} <= set(ALL_EVENTS)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_a_chart_survives_a_json_round_trip(warehouse, offline_llm):
    """History is stored as JSON. Whatever cannot be round-tripped is lost
    on reload, and the chart lives in meta precisely so it is not."""
    _, events = collect(
        lambda emit: sql_engine.run_sql_engine("show the opportunity funnel", [], emit)
    )
    meta = metas(events)[0]
    restored = json.loads(json.dumps(meta))
    assert restored["chart"] == meta["chart"]
    assert restored["chart_data"] == meta["chart_data"]


def test_a_legacy_five_key_chart_payload_still_validates():
    """Exactly what conversations persisted before this change contain."""
    from app.core.chart_spec import parse_chart_spec

    legacy = {
        "type": "bar", "x_key": "stage", "y_keys": ["total"],
        "title": "Cases", "stacked": False,
    }
    spec = parse_chart_spec(legacy, columns=["stage", "total"])
    assert spec is not None
    assert spec.wire_dump() == legacy  # and re-serializes byte-identically
