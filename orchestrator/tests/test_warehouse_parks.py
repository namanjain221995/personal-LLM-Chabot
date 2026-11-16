"""A turn the model could not finish ends as the worker's, through the real /chat.

The engine used to catch everything, so a parked turn answered "the database refused both
attempts", the row read `completed`, and the sweep never came back: "Your request is kept"
was false. Real /chat, llm.py, resilience and continuity.Hold on the test database. The
engine is a fake client, and the warehouse holds one invented table.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import duckdb
import pytest

from app import continuity, db
from app.config import settings
from app.main import _live_generations
from tests.test_continuity import (  # noqa: F401 — `engine` and `_clean_process_state` are fixtures
    _assistant_rows,
    _async_client,
    _clean_process_state,
    _parse_sse,
    _status,
    _Stream,
    _verdict,
    _wait_until,
    engine,
)


@pytest.fixture()
def sql_model(engine, tmp_path, monkeypatch):  # noqa: F811
    path = str(tmp_path / "warehouse.duckdb")
    con = duckdb.connect(path)
    con.execute('CREATE TABLE Invoice__c ("Id" VARCHAR, "Name" VARCHAR)')
    con.execute("INSERT INTO Invoice__c VALUES ('a0X000000000001AAA', 'INV-1'), ('a0X000000000002AAA', 'INV-2')")
    con.close()
    monkeypatch.setattr(settings, "duckdb_path", path)

    async def create(**kwargs):
        if kwargs.get("stream"):
            engine.stream_calls.append(kwargs)
            return _Stream("There are 2 invoices.")
        engine.other_calls.append(kwargs)
        message = SimpleNamespace(
            content="SELECT COUNT(*) AS n FROM Invoice__c", reasoning_content=None, reasoning=None, tool_calls=None, model_extra=None
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        )

    engine.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    async def route(*_a, **_k):
        return "sql"

    monkeypatch.setattr("app.engines.router.route_request", route)
    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", False)
    monkeypatch.setattr(settings, "clarify_before_answering", False)
    monkeypatch.setattr(settings, "clarify_mode", "off")
    return engine


def _body(conversation, intent):
    return {
        "message": "how many invoices are there?",
        "mode": "salesforce",
        "conversation_id": conversation,
        "intent_id": intent,
        "effort": "fast",
    }


def _said(events):
    return " ".join(str(data.get("text") or data.get("message") or "") for kind, data in events if kind in ("token", "error"))


def test_a_parked_writer_keeps_the_row_queued_and_the_sweep_answers_it(sql_model, monkeypatch):
    monkeypatch.setattr(settings, "llm_queue_max_wait_s", 0.6)
    conversation, intent = "park-1", "int-park-1"

    async def scenario():
        _verdict("RECOVERING")
        async with _async_client() as client:
            response = await client.post("/chat", json=_body(conversation, intent))
            await _wait_until(lambda: conversation not in _live_generations)
            status = _status(intent)
            rows = _assistant_rows(conversation)
            _verdict("READY")
            swept = await continuity.resume_sweep("ready")
            await asyncio.sleep(0.5)
            await _wait_until(lambda: conversation not in _live_generations)
        return _parse_sse(response.text), status, rows, swept

    events, status, rows, swept = asyncio.run(scenario())
    assert status == "queued"
    assert not rows, "nothing was answered"
    assert "database refused" not in _said(events)
    assert swept["resumed"] == 1
    assert _status(intent) == "completed"
    assert [row["content"] for row in _assistant_rows(conversation)][0].startswith("There are 2 invoices.")


def test_a_lost_lease_leaves_the_row_to_its_new_owner(sql_model, as_user):
    conversation, intent = "lease-1", "int-lease-1"
    db.create_conversation(int(as_user("user_a")["id"]), conversation, "Lease")

    async def scenario():
        _verdict("RECOVERING")
        async with _async_client() as client:
            request = asyncio.create_task(client.post("/chat", json=_body(conversation, intent)))
            await _wait_until(lambda: _status(intent) == "queued")
            generation = _live_generations[conversation]
            taken = db.resume_chat_request(intent, "gen-elsewhere", expected_generation_id=generation.generation_id)
            assert taken["generation_id"] == "gen-elsewhere"
            db.set_chat_request_status(intent, "running")
            _verdict("READY")
            await request
            await _wait_until(lambda: conversation not in _live_generations)
        return generation

    generation = asyncio.run(scenario())
    row = db.get_chat_request(intent)
    assert generation.lease_lost is True
    assert row["generation_id"] == "gen-elsewhere"
    assert row["status"] == "running"
    assert not _assistant_rows(conversation)


def test_a_dead_model_fails_the_turn_in_the_workers_words(sql_model, monkeypatch):
    conversation, intent = "dead-1", "int-dead-1"

    async def create(**_kwargs):
        raise ConnectionError("refused")

    sql_model.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    monkeypatch.setattr(settings, "llm_recovery_window_s", 0.0, raising=False)
    monkeypatch.setattr(settings, "llm_queue_max_wait_s", 0.2)

    async def scenario():
        _verdict("READY")
        async with _async_client() as client:
            response = await client.post("/chat", json=_body(conversation, intent))
            await _wait_until(lambda: conversation not in _live_generations)
        return _parse_sse(response.text)

    events = asyncio.run(scenario())
    assert "database refused" not in _said(events)
    assert not any(data.get("salesforce_error") for kind, data in events if kind == "meta")
    assert not _assistant_rows(conversation) or "database" not in _assistant_rows(conversation)[0]["content"]
