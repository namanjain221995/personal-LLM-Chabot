from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.core import tracing
from app.main import ChatRequest


def test_sanitize_redacts_secrets_and_bounds_payloads():
    value = tracing.sanitize(
        {
            "authorization": "Bearer private",
            "nested": {"password": "private", "safe": "visible"},
            "long": "x" * 5000,
        }
    )

    assert value["authorization"] == "[redacted]"
    assert value["nested"] == {"password": "[redacted]", "safe": "visible"}
    assert value["long"].endswith("…[truncated]")
    assert len(value["long"]) < 5000


def test_sanitize_keeps_numeric_counts_under_secret_looking_keys():
    # The context meter's `tokens_used` is a number, not a credential.
    value = tracing.sanitize(
        {"tokens_used": 812345, "prompt_tokens": 12, "token": "abc", "tokens": ["a"]}
    )
    assert value == {
        "tokens_used": 812345,
        "prompt_tokens": 12,
        "token": "[redacted]",
        "tokens": "[redacted]",
    }


def test_recorder_orders_events_and_finishes_once(monkeypatch):
    calls = []

    async def fake_run_in_thread(fn, *args, **kwargs):
        calls.append((fn.__name__, args, kwargs))

    monkeypatch.setattr(tracing.db, "run_in_thread", fake_run_in_thread)
    recorder = tracing.TraceRecorder(
        "generation-1",
        test_case_id="SF-DATA-001",
        versions={"application": "test", "database_schema": 32},
    )

    async def go():
        await recorder.start(
            conversation_id="conversation-1",
            user_id=7,
            workspace_id="workspace-1",
            question="How many candidates were placed?",
            requested_mode="salesforce",
        )
        await recorder.event("REQUEST_RECEIVED", details={"token": "do-not-store"})
        await recorder.event("QUERY_EXECUTED", details={"returned_rows": 3})
        await recorder.finish("ok", route="sql", resolved_mode="salesforce")
        await recorder.finish("error")

    asyncio.run(go())

    assert [call[0] for call in calls] == [
        "start_query_trace",
        "append_query_trace_event",
        "append_query_trace_event",
        "finish_query_trace",
    ]
    assert calls[1][1][1] == 1
    assert calls[2][1][1] == 2
    assert calls[1][1][5] == {"token": "[redacted]"}
    assert calls[3][2]["selected_route"] == "sql"
    assert recorder.request_id.startswith("req_")
    assert len(recorder.request_id) == 36
    assert calls[0][2]["test_case_id"] == "SF-DATA-001"
    assert calls[0][2]["versions"]["trace_schema"] == "1.0.0"
    assert calls[1][1][-1] == tracing.PIPELINE_VERSION


def test_context_helper_is_a_noop_without_active_trace(monkeypatch):
    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("database should not be called")

    monkeypatch.setattr(tracing.db, "run_in_thread", fail_if_called)
    asyncio.run(tracing.event("NOT_IN_A_REQUEST"))


def test_trace_round_trips_through_the_database(isolated_app_db):
    """The V30 tables, the FK from events to their root, and the owner scope
    on the read path — against a real PostgreSQL, not a fake."""
    from app import db

    user_id = db.create_user("tracer", "!x")
    db.create_conversation(user_id, "conversation-1", "traced")
    recorder = tracing.TraceRecorder(
        "generation-db-1",
        test_case_id="SF-DATA-001",
        versions={"application": "test", "database_schema": db.LATEST_SCHEMA_VERSION},
    )

    async def go():
        await recorder.start(
            conversation_id="conversation-1",
            user_id=user_id,
            workspace_id="ws-1",
            question="How many candidates were placed?",
            requested_mode="salesforce",
        )
        await recorder.event("REQUEST_RECEIVED", details={"token": "secret", "ok": 1})
        await recorder.event(
            "QUERY_EXECUTED",
            status="failed",
            component="sql",
            duration_ms=12,
            error=RuntimeError("boom"),
        )
        await recorder.finish("error", route="sql", error=RuntimeError("boom"))

    asyncio.run(go())

    trace = db.get_query_trace("generation-db-1", user_id)
    assert trace is not None
    assert trace["final_status"] == "error"
    assert trace["selected_route"] == "sql"
    assert trace["error_type"] == "RuntimeError"
    assert trace["error_stage"] == "QUERY_EXECUTED"
    assert trace["completed_at"] is not None
    assert trace["request_id"] == recorder.request_id
    assert trace["test_case_id"] == "SF-DATA-001"
    assert trace["versions"]["pipeline"] == tracing.PIPELINE_VERSION
    assert all(e["started_at"] for e in trace["events"])
    assert all(e["completed_at"] for e in trace["events"])
    assert all(e["component_version"] == tracing.PIPELINE_VERSION for e in trace["events"])
    assert [e["sequence_number"] for e in trace["events"]] == [1, 2]
    assert trace["events"][0]["details"] == {"token": "[redacted]", "ok": 1}
    assert trace["events"][1]["status"] == "failed"
    assert trace["events"][1]["duration_ms"] == 12
    assert trace["events"][1]["error_message"] == "boom"

    # A closed trace is not rewritten by a late finalizer.
    db.finish_query_trace("generation-db-1", "ok", 1)
    assert db.get_query_trace("generation-db-1", user_id)["final_status"] == "error"

    # Another user cannot read it.
    assert db.get_query_trace("generation-db-1", user_id + 1) is None

    # Deleting the conversation clears the root (query_traces is in
    # _SIDE_TABLES) and the events cascade from it.
    assert db.delete_conversation(user_id, "conversation-1") is True
    assert db.get_query_trace("generation-db-1", user_id) is None
    with db.connection() as con:
        left = con.execute(
            "SELECT count(*) AS n FROM query_trace_events WHERE trace_id = %s",
            ("generation-db-1",),
        ).fetchone()
    assert left["n"] == 0


def test_trace_schema_covers_the_persisted_evaluation_contract():
    path = Path(__file__).resolve().parents[2] / "evaluation" / "schemas" / "trace.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))

    required = set(schema["required"])
    assert {"trace_id", "request_id", "versions", "events"} <= required
    assert "test_case_id" in schema["properties"]
    event_required = set(schema["$defs"]["event"]["required"])
    assert {"stage", "status", "started_at", "completed_at", "duration_ms"} <= event_required


def test_chat_request_accepts_only_a_case_identifier_not_golden_data():
    request = ChatRequest(message="question", test_case_id="SF-DATA-001")
    assert request.test_case_id == "SF-DATA-001"
    assert not hasattr(request, "expected")

    try:
        ChatRequest(message="question", test_case_id="bad id with spaces")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid test_case_id was accepted")
