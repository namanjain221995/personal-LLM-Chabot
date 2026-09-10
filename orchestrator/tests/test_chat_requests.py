"""V29 durable send intents (docs/upload-reliability/API.md, Chat section)
and the lease on video analysis runs.

A generation is an in-process object; the REQUEST that started it is a
`chat_requests` row keyed by the browser's intent_id. These tests prove the
row's lifecycle end to end: a send records it, a retry of the same intent
attaches or replays instead of starting a second generation, a request the
process lost is resumed under a new attempt, Stop marks it cancelled, a
restart marks it interrupted — and the answer is durable server-side under
its generation_id without a duplicate when the viewer later PUTs the thread.

Two harnesses, as in test_live_generation.py: TestClient for flows that run
to completion (it buffers a whole streaming response before returning), and
httpx's ASGI transport on ONE asyncio loop for the flows that need a
generation to be live while a second request arrives.
"""
from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, llm, metrics
from app import main as app_main
from app.config import settings
from app.main import _live_generations, app


@pytest.fixture(autouse=True)
def _fresh_metrics():
    """Counters are process-global and every POST /chat in the whole suite
    records an intent, so an absolute assertion here reads whatever ran
    before it. Reset per test: what these tests mean is "this action
    incremented exactly this counter", which is only expressible from zero.
    """
    metrics.reset()
    yield
    metrics.reset()


def _parse_sse(text: str):
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) >= 2 and lines[0].startswith("event: "):
            events.append((lines[0][7:], json.loads(lines[1][6:])))
    return events


def _fake_stream(deltas, calls=None):
    async def fake(messages, **kwargs):
        if calls is not None:
            calls.append(1)
        for kind, text in deltas:
            yield kind, text

    return fake


def _gated_stream(gate: "asyncio.Event", deltas, calls):
    """Streams `deltas`, then holds the generation open until `gate` is set —
    the way a long thinking pass keeps a generation live."""

    async def fake(messages, **kwargs):
        calls.append(1)
        for kind, text in deltas:
            yield kind, text
        await gate.wait()

    return fake


def _async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


def _message_rows(conversation_id: str, generation_id: str) -> int:
    with db.connection() as con:
        return int(
            con.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = %s AND generation_id = %s",
                (conversation_id, generation_id),
            ).fetchone()["n"]
        )


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    _live_generations.clear()
    metrics.reset()
    # A TestClient block's lifespan exit leaves the process-wide shutdown
    # flag raised (in production the process is gone by then); the ASGI-
    # transport tests below run no lifespan, so they clear it themselves.
    monkeypatch.setattr(app_main, "_shutting_down", False)
    yield
    _live_generations.clear()


@pytest.fixture()
def hello_stream(monkeypatch):
    calls: list = []
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream([("token", "Hello!")], calls))
    return calls


# ---------------------------------------------------------------------------
# POST /chat records the intent
# ---------------------------------------------------------------------------


def test_intent_id_is_validated():
    with TestClient(app) as client:
        for bad in ("bad id!", "x" * 65, ""):
            resp = client.post(
                "/chat",
                json={"message": "hi", "mode": "assistant", "intent_id": bad},
            )
            assert resp.status_code == 422, bad


def test_post_chat_records_the_intent_and_the_durable_answer(hello_stream):
    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={
                "message": "hi",
                "mode": "assistant",
                "conversation_id": "req-1",
                "intent_id": "int-1",
            },
        )
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        kinds = [e for e, _ in events]
        # The FIRST event names the generation, before any token exists.
        assert kinds[0] == "meta" and kinds[-1] == "done"
        leading = events[0][1]
        assert leading == {
            "generation_id": leading["generation_id"],
            "intent_id": "int-1",
            "attempt": 1,
        }
        # The engine's meta still comes last and still wins — and it carries
        # the ids too, because that is what gets persisted with the answer.
        final = [d for k, d in events if k == "meta"][-1]
        assert final["route"] == "chat"
        assert final["generation_id"] == leading["generation_id"]
        assert final["intent_id"] == "int-1" and final["attempt"] == 1

    row = db.get_chat_request("int-1")
    assert row is not None
    assert row["status"] == "completed"
    assert row["generation_id"] == leading["generation_id"]
    assert row["attempt"] == 1 and row["resumable"] is True
    assert row["conversation_id"] == "req-1"
    assert row["request"]["message"] == "hi"
    assert row["request"]["mode"] == "assistant"
    assert row["finished_at"] is not None
    # The answer is durable server-side, under the generation id, even though
    # a viewer WAS attached (the old rule persisted only when nobody was).
    stored = app_main._persisted_answer("req-1", row["generation_id"])
    assert stored is not None and stored["content"] == "Hello!"
    assert stored["meta"]["generation_id"] == row["generation_id"]
    assert stored["meta"]["intent_id"] == "int-1"
    # The usage event joins the turn to its intent.
    with db.connection() as con:
        usage = con.execute(
            "SELECT meta FROM usage_events WHERE generation_id = %s", (row["generation_id"],)
        ).fetchone()
    assert usage is not None and usage["meta"]["intent_id"] == "int-1"
    assert usage["meta"]["attempt"] == 1
    assert metrics._counters["chat_request_total"] == {(("result", "accepted"),): 1.0}


def test_an_old_client_without_an_intent_keeps_the_old_event_shape(hello_stream):
    """No intent_id → the server mints one and keeps the event order and meta
    keys the pre-V29 clients (and their tests) were built on."""
    with TestClient(app) as client:
        resp = client.post(
            "/chat", json={"message": "hi", "mode": "assistant", "conversation_id": "old-1"}
        )
        events = _parse_sse(resp.text)
    kinds = [e for e, _ in events]
    assert kinds == ["token", "meta", "done"]
    assert "intent_id" not in events[1][1] and "attempt" not in events[1][1]
    row = db.latest_chat_request("old-1")
    assert row is not None and row["status"] == "completed"
    assert len(row["intent_id"]) == 32  # server-minted uuid4 hex


def test_inline_bytes_are_dropped_from_the_snapshot_and_mark_it_not_resumable(monkeypatch):
    async def fake_vision(message, images, history, emit, **kwargs):
        await emit("token", {"text": "a cat"})
        await emit("meta", {"route": "vision"})
        return "a cat"

    from app.engines import vision as vision_engine

    monkeypatch.setattr(vision_engine, "run_vision_engine", fake_vision)
    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={
                "message": "what is this",
                "mode": "assistant",
                "conversation_id": "img-1",
                "intent_id": "int-img",
                "images": ["data:image/png;base64,AAAA"],
            },
        )
        assert resp.status_code == 200
    row = db.get_chat_request("int-img")
    assert row["resumable"] is False
    assert "images" not in row["request"] and "image" not in row["request"]
    assert row["request"]["message"] == "what is this"


def test_a_known_intent_on_another_conversation_is_a_conflict(hello_stream):
    with TestClient(app) as client:
        body = {"message": "hi", "mode": "assistant", "conversation_id": "c-a", "intent_id": "int-x"}
        assert client.post("/chat", json=body).status_code == 200
        resp = client.post("/chat", json={**body, "conversation_id": "c-b"})
        assert resp.status_code == 409
    assert metrics._counters["chat_request_total"][(("result", "conflict"),)] == 1.0


# ---------------------------------------------------------------------------
# Same intent twice while live → one generation
# ---------------------------------------------------------------------------


def test_same_intent_twice_while_live_attaches_to_the_one_generation(monkeypatch):
    calls: list = []

    async def scenario():
        gate = asyncio.Event()
        monkeypatch.setattr(
            llm, "stream_chat_events", _gated_stream(gate, [("token", "Hel")], calls)
        )
        body = {"message": "hi", "mode": "assistant", "conversation_id": "live-1", "intent_id": "int-live"}
        async with _async_client() as client:
            first = asyncio.create_task(client.post("/chat", json=body))
            await _wait_until(lambda: bool(calls))
            gen = _live_generations["live-1"]
            second = asyncio.create_task(client.post("/chat", json=body))
            # The retry is attached to the SAME generation: two followers,
            # one worker, one model call.
            await _wait_until(lambda: gen.subscribers >= 2)
            assert len(_live_generations) == 1 and _live_generations["live-1"] is gen
            assert calls == [1]
            row = db.get_chat_request("int-live")
            assert row["status"] == "running" and row["generation_id"] == gen.generation_id
            gate.set()
            r1, r2 = await asyncio.gather(first, second)
        return gen, r1, r2

    gen, r1, r2 = asyncio.run(scenario())
    assert r1.status_code == 200 and r2.status_code == 200
    e1, e2 = _parse_sse(r1.text), _parse_sse(r2.text)
    assert e1[0][0] == "meta" and e2[0][0] == "meta"
    assert e1[0][1]["generation_id"] == gen.generation_id
    assert e2[0][1]["generation_id"] == gen.generation_id  # the same stream
    assert e1[-1][0] == "done" and e2[-1][0] == "done"
    assert calls == [1]
    assert metrics._counters["chat_request_total"] == {
        (("result", "accepted"),): 1.0,
        (("result", "attached"),): 1.0,
    }
    assert db.get_chat_request("int-live")["status"] == "completed"
    assert _message_rows("live-1", gen.generation_id) == 1


# ---------------------------------------------------------------------------
# Completed intent → replay, no new generation
# ---------------------------------------------------------------------------


def test_a_completed_intent_replays_the_persisted_answer(monkeypatch):
    calls: list = []
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream([("token", "Hello!")], calls))
    body = {"message": "hi", "mode": "assistant", "conversation_id": "done-1", "intent_id": "int-done"}
    with TestClient(app) as client:
        first = _parse_sse(client.post("/chat", json=body).text)
        generation_id = first[0][1]["generation_id"]
        assert calls == [1]
        # The person presses Send again (or the browser retries a lost ack).
        resp = client.post("/chat", json=body)
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
    kinds = [e for e, _ in events]
    assert kinds == ["meta", "token", "meta", "done"]
    assert events[0][1] == {"generation_id": generation_id, "intent_id": "int-done", "attempt": 1}
    assert events[1][1] == {"text": "Hello!"}
    assert events[2][1]["route"] == "chat" and events[2][1]["generation_id"] == generation_id
    assert calls == [1], "a replay never runs the model again"
    assert "done-1" not in _live_generations
    assert _message_rows("done-1", generation_id) == 1
    assert metrics._counters["chat_request_total"][(("result", "replayed"),)] == 1.0


# ---------------------------------------------------------------------------
# A lost process: resume on attach, under a new attempt
# ---------------------------------------------------------------------------


def test_a_lost_request_is_resumed_by_attach_under_a_new_attempt(hello_stream, as_user):
    alice = as_user("alice")
    uid = int(alice["id"])
    db.create_conversation(uid, "lost-1", "Lost")
    assert db.create_chat_request(
        "int-lost",
        uid,
        "lost-1",
        "gen-old",
        {"message": "hi again", "mode": "assistant", "conversation_id": "lost-1"},
    )
    assert db.get_chat_request("int-lost")["status"] == "accepted"

    with TestClient(app) as client:
        # Startup marked what the dead process held.
        assert db.get_chat_request("int-lost")["status"] == "interrupted"
        resp = client.get("/chat/attach/lost-1")
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        kinds = [e for e, _ in events]
        assert kinds[0] == "meta" and kinds[-1] == "done"
        leading = events[0][1]
        assert leading["intent_id"] == "int-lost" and leading["attempt"] == 2
        assert leading["generation_id"] != "gen-old"
        # Nothing left to resume once it is answered.
        assert client.get("/chat/attach/lost-1").status_code == 404

    row = db.get_chat_request("int-lost")
    assert row["status"] == "completed" and row["attempt"] == 2
    assert row["generation_id"] == leading["generation_id"]
    assert _message_rows("lost-1", leading["generation_id"]) == 1
    assert hello_stream == [1]
    assert metrics._counters["chat_request_total"] == {(("result", "resumed"),): 1.0}
    assert metrics._counters["chat_request_resume_total"] == {(): 1.0}


def test_attach_refuses_a_request_that_cannot_be_resumed(hello_stream, as_user):
    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "lost-2", "Lost")
    db.create_chat_request(
        "int-inline", uid, "lost-2", "gen-inline",
        {"message": "read this", "mode": "assistant", "conversation_id": "lost-2"},
        resumable=False,
    )
    with TestClient(app) as client:
        assert client.get("/chat/attach/lost-2").status_code == 404
    assert db.get_chat_request("int-inline")["status"] == "interrupted"
    assert hello_stream == []


def test_attach_never_resumes_someone_elses_request(hello_stream, as_user):
    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "lost-3", "Lost")
    db.create_chat_request(
        "int-alice", uid, "lost-3", "gen-alice",
        {"message": "mine", "mode": "assistant", "conversation_id": "lost-3"},
    )
    as_user("bob")
    with TestClient(app) as client:
        assert client.get("/chat/attach/lost-3").status_code == 404
    assert hello_stream == []


def test_a_retry_of_a_failed_intent_runs_a_new_attempt(monkeypatch, hello_stream):
    from app.engines import chat as chat_engine

    real = chat_engine.run_chat_engine

    async def boom(*args, **kwargs):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(chat_engine, "run_chat_engine", boom)
    body = {"message": "hi", "mode": "assistant", "conversation_id": "fail-1", "intent_id": "int-fail"}
    safe = "The answer could not be completed. Please try again."
    with TestClient(app) as client:
        events = _parse_sse(client.post("/chat", json=body).text)
        # ORCH-01: the wire carries the safe sentence and a category, never
        # the exception's own text; the row records the same sentence; and
        # a failure record is persisted so a reload shows the failure.
        assert events[-1] == ("error", {"message": safe, "code": "APPLICATION_ERROR"})
        row = db.get_chat_request("int-fail")
        assert row["status"] == "failed" and row["error"] == safe
        first_generation = row["generation_id"]
        record = app_main._persisted_answer("fail-1", first_generation)
        assert record is not None and record["content"] == ""
        assert record["meta"]["error"] == {
            "message": safe,
            "code": "APPLICATION_ERROR",
            "status": None,
            "resumable": True,
        }
        assert record["meta"]["intent_id"] == "int-fail"
        # A failure record is not an answer.
        status = client.get("/chat/requests/int-fail").json()
        assert status["status"] == "failed" and status["answer_persisted"] is False
        monkeypatch.setattr(chat_engine, "run_chat_engine", real)
        events = _parse_sse(client.post("/chat", json=body).text)
    assert events[0][1]["attempt"] == 2 and events[-1][0] == "done"
    row = db.get_chat_request("int-fail")
    assert row["status"] == "completed" and row["attempt"] == 2 and row["error"] == ""
    assert row["generation_id"] == events[0][1]["generation_id"]
    # The new attempt supersedes the failure record: one assistant row, the
    # answer — so the viewer's whole-thread PUT is never refused as a shrink.
    assert app_main._persisted_answer("fail-1", first_generation) is None
    assert [m["role"] for m in db.list_messages("fail-1")] == ["assistant"]
    assert _message_rows("fail-1", row["generation_id"]) == 1


def test_a_failed_generation_is_logged_with_its_ids_and_not_its_content(monkeypatch, caplog):
    import logging

    from app.engines import chat as chat_engine

    async def boom(*args, **kwargs):
        raise RuntimeError("secret upstream body http://vllm:8000")

    monkeypatch.setattr(chat_engine, "run_chat_engine", boom)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        with TestClient(app) as client:
            resp = client.post(
                "/chat",
                json={"message": "the question", "mode": "assistant", "conversation_id": "log-1", "intent_id": "int-log"},
            )
    row = db.get_chat_request("int-log")
    line = next(r for r in caplog.records if "failed:" in r.getMessage())
    assert row["generation_id"] in line.getMessage() and "int-log" in line.getMessage()
    assert "RuntimeError" in line.getMessage()
    assert "the question" not in line.getMessage()
    assert "http://vllm:8000" not in resp.text  # the wire carries the safe sentence


def test_a_newer_message_replaces_the_running_generation_with_a_terminal_frame(monkeypatch):
    calls: list = []

    async def scenario():
        gate = asyncio.Event()
        monkeypatch.setattr(llm, "stream_chat_events", _gated_stream(gate, [("token", "First "), ("token", "answer")], calls))
        base = {"mode": "assistant", "conversation_id": "rep-1"}
        async with _async_client() as client:
            first = asyncio.create_task(client.post("/chat", json={**base, "message": "one", "intent_id": "int-a"}))
            await _wait_until(lambda: len(calls) == 1)
            old = _live_generations["rep-1"]
            second = asyncio.create_task(client.post("/chat", json={**base, "message": "two", "intent_id": "int-b"}))
            await _wait_until(lambda: len(calls) == 2 and old.cancelled)
            assert _live_generations["rep-1"] is not old
            gate.set()
            r1, r2 = await asyncio.gather(first, second)
        return old, r1, r2

    old, r1, r2 = asyncio.run(scenario())
    e1 = _parse_sse(r1.text)
    # ORCH-02: the replaced generation's follower sees a terminal frame that
    # says so, instead of a stream that merely ends and reads as "done".
    assert e1[-1] == ("error", {"message": "This answer was replaced by a newer message.", "code": "replaced"})
    kinds = [k for k, _ in e1]
    assert kinds[0] == "meta" and kinds[-1] == "error" and set(kinds[1:-1]) <= {"token"}
    row_a = db.get_chat_request("int-a")
    assert row_a["status"] == "cancelled" and row_a["error"] == "replaced by a newer message"
    assert _message_rows("rep-1", old.generation_id) == 0  # a cancel persists nothing
    e2 = _parse_sse(r2.text)
    assert e2[-1][0] == "done" and e2[0][1]["intent_id"] == "int-b"
    row_b = db.get_chat_request("int-b")
    assert row_b["status"] == "completed"
    assert _message_rows("rep-1", row_b["generation_id"]) == 1


# ---------------------------------------------------------------------------
# GET /chat/requests/{intent_id}
# ---------------------------------------------------------------------------


def test_request_status_shape_and_ownership(hello_stream, as_user):
    as_user("alice")
    with TestClient(app) as client:
        client.post(
            "/chat",
            json={"message": "hi", "mode": "assistant", "conversation_id": "st-1", "intent_id": "int-st"},
        )
        resp = client.get("/chat/requests/int-st")
        assert resp.status_code == 200
        row = db.get_chat_request("int-st")
        assert resp.json() == {
            "intent_id": "int-st",
            "conversation_id": "st-1",
            "status": "completed",
            "generation_id": row["generation_id"],
            "attempt": 1,
            "resumable": True,
            "answer_persisted": True,
            "live": False,
        }
        assert client.get("/chat/requests/never-seen").status_code == 404
    as_user("bob")
    with TestClient(app) as client:
        assert client.get("/chat/requests/int-st").status_code == 404  # not yours → 404, not 403


def test_request_status_reports_live_and_unpersisted(as_user):
    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "st-2", "S")
    db.create_chat_request("int-st2", uid, "st-2", "gen-st2", {"message": "hi", "mode": "assistant"})
    gen = app_main.LiveGeneration("st-2", uid)
    gen.generation_id = "gen-st2"
    _live_generations["st-2"] = gen
    with TestClient(app) as client:
        body = client.get("/chat/requests/int-st2").json()
    # (The lifespan marked the row interrupted — the registry says live.)
    assert body["live"] is True and body["answer_persisted"] is False
    assert body["status"] == "interrupted"


# ---------------------------------------------------------------------------
# POST /chat/stop marks the row cancelled
# ---------------------------------------------------------------------------


def test_stop_marks_the_request_cancelled(monkeypatch):
    calls: list = []

    async def scenario():
        gate = asyncio.Event()
        monkeypatch.setattr(llm, "stream_chat_events", _gated_stream(gate, [("token", "Hel")], calls))
        body = {"message": "hi", "mode": "assistant", "conversation_id": "stop-1", "intent_id": "int-stop"}
        async with _async_client() as client:
            first = asyncio.create_task(client.post("/chat", json=body))
            await _wait_until(lambda: bool(calls))
            stopped = await client.post("/chat/stop", json={"conversation_id": "stop-1"})
            assert stopped.json() == {"stopped": True}
            resp = await first
        return resp

    resp = asyncio.run(scenario())
    events = _parse_sse(resp.text)
    kinds = [e for e, _ in events]
    # The leading meta, whatever streamed, and NO terminal frame: the tab that
    # pressed Stop aborted its own read, and a cancel is not a failure.
    assert kinds[0] == "meta" and set(kinds[1:]) <= {"token"}
    row = db.get_chat_request("int-stop")
    assert row["status"] == "cancelled" and row["finished_at"] is not None
    assert _message_rows("stop-1", row["generation_id"]) == 0


# ---------------------------------------------------------------------------
# Startup / shutdown reconciliation
# ---------------------------------------------------------------------------


def test_startup_marks_open_requests_interrupted(as_user):
    uid = int(as_user("alice")["id"])
    for intent, gen, status in (("i-acc", "g1", "accepted"), ("i-run", "g2", "running"), ("i-done", "g3", "completed")):
        db.create_chat_request(intent, uid, "conv-x", gen, {"message": "m"})
        if status != "accepted":
            db.set_chat_request_status(intent, status)
    assert asyncio.run(app_main._interrupt_open_requests()) == 2
    assert db.get_chat_request("i-acc")["status"] == "interrupted"
    assert db.get_chat_request("i-run")["status"] == "interrupted"
    assert db.get_chat_request("i-done")["status"] == "completed"
    assert asyncio.run(app_main._interrupt_open_requests()) == 0


def test_startup_returns_a_stale_finalizing_upload_session_to_uploading(as_user):
    from datetime import datetime, timedelta, timezone

    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "up-1", "Up")
    stale, live = "a" * 32, "b" * 32
    for upload_id in (stale, live):
        db.create_upload_session(upload_id, uid, "up-1", "big.mp4", "video", expected_parts=7)
    with db.connection() as con:
        con.execute("UPDATE upload_sessions SET status = 'finalizing'")
        con.execute(
            "UPDATE upload_sessions SET updated_at = %s WHERE id = %s",
            (datetime.now(timezone.utc) - timedelta(minutes=20), stale),
        )
    assert asyncio.run(app_main._reset_stale_upload_finalisations()) == 1
    assert db.get_upload_session(stale)["status"] == "uploading"
    assert db.get_upload_session(live)["status"] == "finalizing"  # a finaliser that is merely slow
    assert db.get_upload_session(stale)["accepted_parts"] == db.get_upload_session(live)["accepted_parts"]


def test_a_worker_cancelled_by_shutdown_records_interrupted_not_cancelled(monkeypatch, as_user):
    uid = int(as_user("alice")["id"])
    db.create_chat_request("i-shut", uid, "conv-s", "g-shut", {"message": "m"})
    gen = app_main.LiveGeneration("conv-s", uid)
    gen.intent_id = "i-shut"
    gen.cancelled = True
    monkeypatch.setattr(app_main, "_shutting_down", True)
    asyncio.run(app_main._settle_chat_request(gen))
    assert db.get_chat_request("i-shut")["status"] == "interrupted"
    monkeypatch.setattr(app_main, "_shutting_down", False)
    gen.request_status = "running"
    asyncio.run(app_main._settle_chat_request(gen))
    assert db.get_chat_request("i-shut")["status"] == "cancelled"


# ---------------------------------------------------------------------------
# The durable answer and the viewer's whole-thread PUT
# ---------------------------------------------------------------------------


def test_server_persist_then_client_put_does_not_duplicate(hello_stream):
    with TestClient(app) as client:
        events = _parse_sse(
            client.post(
                "/chat",
                json={"message": "hi", "mode": "assistant", "conversation_id": "put-1", "intent_id": "int-put"},
            ).text
        )
        generation_id = events[0][1]["generation_id"]
        final_meta = [d for k, d in events if k == "meta"][-1]
        assert _message_rows("put-1", generation_id) == 1
        # The browser persists the whole thread the way frontend/lib/streams.ts
        # finalize() does: its user turn plus the answer with the server's meta.
        resp = client.put(
            "/history/conversations/put-1/messages",
            json={
                "messages": [
                    {"role": "user", "content": "hi", "meta": {"intent": {"id": "int-put", "state": "completed"}}},
                    {"role": "assistant", "content": "Hello!", "meta": final_meta},
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        # And appends the answer again, as a second attached tab would.
        again = client.post(
            "/history/conversations/put-1/messages",
            json={"role": "assistant", "content": "Hello!", "meta": final_meta},
        )
        assert again.status_code == 200 and again.json().get("deduplicated") is True
    thread = db.list_messages("put-1")
    assert [m["role"] for m in thread] == ["user", "assistant"]
    assert _message_rows("put-1", generation_id) == 1
    with TestClient(app) as client:
        assert client.get("/chat/requests/int-put").json()["answer_persisted"] is True


def test_the_finished_answer_overwrites_a_partial_a_viewer_persisted(monkeypatch, as_user):
    """A tab that loses its stream mid-answer persists what it had under the
    generation_id it learned from the leading meta (frontend markInterrupted).
    The server's finished answer must replace that partial, not dedupe
    against it and leave the truncated text as the durable copy."""
    uid = int(as_user("alice")["id"])
    calls: list = []

    async def scenario():
        gate = asyncio.Event()
        monkeypatch.setattr(llm, "stream_chat_events", _gated_stream(gate, [("token", "Hel")], calls))
        body = {"message": "hi", "mode": "assistant", "conversation_id": "part-1", "intent_id": "int-part"}
        async with _async_client() as client:
            first = asyncio.create_task(client.post("/chat", json=body))
            await _wait_until(lambda: bool(calls))
            gen = _live_generations["part-1"]
            await db.run_in_thread(
                db.add_message, uid, "part-1", "assistant", "Hel", {"generation_id": gen.generation_id}
            )
            gate.set()
            await first
        return gen

    gen = asyncio.run(scenario())
    stored = app_main._persisted_answer("part-1", gen.generation_id)
    assert stored["content"] == "Hel"  # the stub streams one token, then the gate
    assert stored["meta"]["route"] == "chat" and stored["meta"]["intent_id"] == "int-part"
    assert _message_rows("part-1", gen.generation_id) == 1


def test_the_finished_answer_overwrites_a_shorter_partial(as_user):
    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "part-2", "P")
    gen = app_main.LiveGeneration("part-2", uid)
    gen.intent_id = "int-part2"
    gen.answer = "Hello, world."
    gen.final_meta = {"route": "chat", "generation_id": gen.generation_id}
    db.add_message(uid, "part-2", "assistant", "Hel", {"generation_id": gen.generation_id})
    asyncio.run(app_main._store_answer(gen))
    stored = app_main._persisted_answer("part-2", gen.generation_id)
    assert stored == {"content": "Hello, world.", "meta": gen.final_meta}
    assert gen.persisted is True
    assert _message_rows("part-2", gen.generation_id) == 1


# ---------------------------------------------------------------------------
# The lease on a video analysis run
# ---------------------------------------------------------------------------


def _analysis_row(tmp_path, monkeypatch, content_hash: str):
    from app.video import store

    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    os.makedirs(store.analysis_dir(content_hash), exist_ok=True)
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00" * 64)
    store.adopt_source(content_hash, str(src), "clip.mp4")
    return db.upsert_video_analysis(content_hash, 64, "video/mp4", "clip.mp4")


def _lease(analysis_id: int):
    with db.connection() as con:
        row = con.execute(
            "SELECT lease_owner, lease_expires_at FROM video_analyses WHERE id = %s", (analysis_id,)
        ).fetchone()
    return row["lease_owner"], row["lease_expires_at"]


def test_a_second_runner_leaves_a_leased_run_to_its_owner(monkeypatch, tmp_path):
    from app.video import pipeline

    row = _analysis_row(tmp_path, monkeypatch, "f" * 64)
    ran: list = []

    def make(name):
        async def run(ctx, progress):
            ran.append(name)
            return pipeline._StageResult("done", name)

        return run

    monkeypatch.setattr(pipeline, "_STAGE_FNS", {s: make(s) for s in pipeline.STAGES})
    monkeypatch.setattr(settings, "video_lease_ttl_s", 60.0)

    # Another live process holds the lease: this one runs nothing.
    assert db.claim_video_lease(row["id"], "other-host:4242:deadbeef", 300.0)
    asyncio.run(pipeline._run(row["id"]))
    assert ran == []
    assert db.get_video_analysis(row["id"])["status"] == "queued"
    assert _lease(row["id"])[0] == "other-host:4242:deadbeef"
    assert "video_lease_steal_total" not in metrics._counters

    # That process died: its lease lapses and this one takes the run over,
    # counting the steal, then releases the lease on finish.
    assert db.claim_video_lease(row["id"], "other-host:4242:deadbeef", -1.0)
    asyncio.run(pipeline._run(row["id"]))
    assert sorted(ran) == sorted(pipeline.STAGES)
    fresh = db.get_video_analysis(row["id"])
    assert fresh["status"] == "done"
    assert _lease(row["id"]) == ("", None)
    assert metrics._counters["video_lease_steal_total"] == {(): 1.0}
    db.delete_video_analysis(row["id"])


def test_the_run_heartbeats_its_lease_and_startup_requeue_respects_it(monkeypatch, tmp_path):
    from app.video import pipeline

    row = _analysis_row(tmp_path, monkeypatch, "a" * 64)
    monkeypatch.setattr(settings, "video_lease_ttl_s", 3.0)  # heartbeat every 1 s
    seen: dict = {}

    async def probe(ctx, progress):
        seen["at_probe"] = _lease(row["id"])
        # While a stage runs, a live process is being requeued by nobody.
        assert db.requeue_interrupted_video_analyses() == 0
        await asyncio.sleep(1.3)
        return pipeline._StageResult("done", "probe")

    async def fusion(ctx, progress):
        seen["at_fusion"] = _lease(row["id"])
        return pipeline._StageResult("done", "fusion")

    async def other(ctx, progress):
        return pipeline._StageResult("done", "x")

    fns = {s: other for s in pipeline.STAGES}
    fns["probe"], fns["fusion"] = probe, fusion
    monkeypatch.setattr(pipeline, "_STAGE_FNS", fns)
    asyncio.run(pipeline._run(row["id"]))
    assert seen["at_probe"][0] == pipeline._OWNER
    assert seen["at_fusion"][0] == pipeline._OWNER
    assert seen["at_fusion"][1] > seen["at_probe"][1], "the heartbeat moved the lease forward"
    assert _lease(row["id"]) == ("", None)
    assert db.get_video_analysis(row["id"])["status"] == "done"
    db.delete_video_analysis(row["id"])


def test_a_cancelled_run_releases_its_lease(monkeypatch, tmp_path):
    from app.video import pipeline

    row = _analysis_row(tmp_path, monkeypatch, "b" * 64)
    started = {}

    async def slow(ctx, progress):
        started["yes"] = True
        await asyncio.sleep(30)
        return pipeline._StageResult("done", "slow")

    monkeypatch.setattr(pipeline, "_STAGE_FNS", {s: slow for s in pipeline.STAGES})

    async def scenario():
        pipeline.reset_for_tests()
        assert await pipeline.ensure_running(row["id"])
        await _wait_until(lambda: "yes" in started)
        assert _lease(row["id"])[0] == pipeline._OWNER
        await pipeline.stop()

    asyncio.run(scenario())
    assert _lease(row["id"]) == ("", None), "stop() waits for the release before the pool closes"
    assert db.get_video_analysis(row["id"])["status"] == "running"
    # The next process's startup reconciliation requeues it at once.
    assert db.requeue_interrupted_video_analyses() == 1
    db.delete_video_analysis(row["id"])
