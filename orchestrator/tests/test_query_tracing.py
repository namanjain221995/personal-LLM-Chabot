from __future__ import annotations

import pytest

from app.core import tracing


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


@pytest.mark.asyncio
async def test_recorder_orders_events_and_finishes_once(monkeypatch):
    calls = []

    async def fake_run_in_thread(fn, *args, **kwargs):
        calls.append((fn.__name__, args, kwargs))

    monkeypatch.setattr(tracing.db, "run_in_thread", fake_run_in_thread)
    recorder = tracing.TraceRecorder("generation-1")

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


@pytest.mark.asyncio
async def test_context_helper_is_a_noop_without_active_trace(monkeypatch):
    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("database should not be called")

    monkeypatch.setattr(tracing.db, "run_in_thread", fail_if_called)
    await tracing.event("NOT_IN_A_REQUEST")
