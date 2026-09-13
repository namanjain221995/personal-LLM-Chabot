"""answer_branch on POST /chat (2026-09-13, the duplicate answers).

Since V29 the server stores every answer itself, before `done`. "Try again"
in the browser was never updated for that: the request said nothing about
where the new answer belongs, the server stored a plain row, and a row
without `meta.branch` attaches to whatever row precedes it
(frontend/lib/branching.ts) — for a regenerate, the previous answer. Nine
clicks in production rendered as three stacked copies after a truncate had
silently removed six others.

The browser now sends `answer_branch` ({self, parent}) with a regenerate, an
edit and a send in a conversation with versions. These tests prove the
server half: it is validated like intent_id, stored on the answer's
`meta.branch` (and on a failure record's), kept in the request snapshot so a
resume files the answer in the same place, logged with ids only — and an
ordinary send is stored exactly as before.
"""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app import db, llm, metrics
from app import main as app_main
from app.main import _live_generations, app

BRANCH = {"self": "b-int-regen", "parent": "b-question"}


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


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    _live_generations.clear()
    metrics.reset()
    monkeypatch.setattr(app_main, "_shutting_down", False)
    yield
    _live_generations.clear()


@pytest.fixture()
def hello_stream(monkeypatch):
    calls: list = []
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream([("token", "Hello!")], calls))
    return calls


def _answers(conversation_id: str) -> list[dict]:
    return [m for m in db.list_messages(conversation_id) if m["role"] == "assistant"]


def test_the_stored_answer_carries_the_branch_the_request_named(hello_stream):
    body = {
        "message": "hi",
        "mode": "assistant",
        "conversation_id": "br-1",
        "intent_id": "int-regen",
        "answer_branch": BRANCH,
    }
    with TestClient(app) as client:
        events = _parse_sse(client.post("/chat", json=body).text)
    assert events[-1][0] == "done"
    generation_id = events[0][1]["generation_id"]
    stored = app_main._persisted_answer("br-1", generation_id)
    assert stored is not None and stored["content"] == "Hello!"
    assert stored["meta"]["branch"] == BRANCH
    assert stored["meta"]["generation_id"] == generation_id
    # The snapshot a resume runs from keeps it.
    row = db.get_chat_request("int-regen")
    assert row["request"]["answer_branch"] == BRANCH


def test_a_positional_parent_and_a_missing_parent_are_both_accepted(hello_stream):
    with TestClient(app) as client:
        for n, branch in enumerate(({"self": "b-a", "parent": "#0"}, {"self": "b-b"})):
            resp = client.post(
                "/chat",
                json={
                    "message": "hi",
                    "mode": "assistant",
                    "conversation_id": f"br-pos-{n}",
                    "intent_id": f"int-pos-{n}",
                    "answer_branch": branch,
                },
            )
            assert resp.status_code == 200, resp.text
            generation_id = _parse_sse(resp.text)[0][1]["generation_id"]
            assert app_main._persisted_answer(f"br-pos-{n}", generation_id)["meta"]["branch"] == branch


def test_an_ordinary_send_stores_no_branch_key_at_all(hello_stream):
    with TestClient(app) as client:
        events = _parse_sse(
            client.post(
                "/chat",
                json={"message": "hi", "mode": "assistant", "conversation_id": "br-plain", "intent_id": "int-plain"},
            ).text
        )
    stored = app_main._persisted_answer("br-plain", events[0][1]["generation_id"])
    assert stored is not None
    assert "branch" not in stored["meta"]
    assert "answer_branch" not in db.get_chat_request("int-plain")["request"]


@pytest.mark.parametrize(
    "bad",
    [
        {"self": "no-prefix"},
        {"self": "b-"},
        {"self": "b-" + "x" * 65},
        {"self": "b-has space"},
        {"self": "b-ok", "parent": "question"},
        {"self": "b-ok", "parent": "#1234567"},
        {"self": "b-ok", "parent": "#-1"},
        {"self": "b-ok", "parent": 3},
        {"self": 7},
        {"parent": "b-question"},
        {"self": "b-ok", "admin": True},
        "b-ok",
        ["b-ok"],
    ],
)
def test_a_malformed_answer_branch_is_refused_with_422(bad, hello_stream):
    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={
                "message": "hi",
                "mode": "assistant",
                "conversation_id": "br-bad",
                "intent_id": "int-bad",
                "answer_branch": bad,
            },
        )
    assert resp.status_code == 422, (bad, resp.text)
    assert hello_stream == []
    assert db.get_chat_request("int-bad") is None


def test_the_same_intent_again_replays_the_answer_with_its_branch_and_stores_nothing_new(monkeypatch):
    calls: list = []
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream([("token", "Hello!")], calls))
    body = {
        "message": "hi",
        "mode": "assistant",
        "conversation_id": "br-replay",
        "intent_id": "int-replay",
        "answer_branch": BRANCH,
    }
    with TestClient(app) as client:
        first = _parse_sse(client.post("/chat", json=body).text)
        again = _parse_sse(client.post("/chat", json=body).text)
    generation_id = first[0][1]["generation_id"]
    assert calls == [1], "a replay never runs the model again"
    assert [k for k, _ in again] == ["meta", "token", "meta", "done"]
    assert again[2][1]["branch"] == BRANCH
    assert len(_answers("br-replay")) == 1
    assert _answers("br-replay")[0]["meta"]["generation_id"] == generation_id


def test_a_request_resumed_from_its_snapshot_stores_the_answer_under_the_same_branch(hello_stream, as_user):
    uid = int(as_user("alice")["id"])
    db.create_conversation(uid, "br-lost", "Lost")
    assert db.create_chat_request(
        "int-lost-branch",
        uid,
        "br-lost",
        "gen-old",
        {
            "message": "hi again",
            "mode": "assistant",
            "conversation_id": "br-lost",
            "answer_branch": BRANCH,
        },
    )
    with TestClient(app) as client:
        resp = client.get("/chat/attach/br-lost")
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
    leading = events[0][1]
    assert leading["intent_id"] == "int-lost-branch" and leading["attempt"] == 2
    row = db.get_chat_request("int-lost-branch")
    assert row["status"] == "completed" and row["attempt"] == 2
    stored = app_main._persisted_answer("br-lost", row["generation_id"])
    assert stored is not None and stored["meta"]["branch"] == BRANCH


def test_a_failure_record_sits_under_the_branch_and_the_retry_replaces_it(monkeypatch, hello_stream):
    from app.engines import chat as chat_engine

    real = chat_engine.run_chat_engine

    async def boom(*args, **kwargs):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(chat_engine, "run_chat_engine", boom)
    body = {
        "message": "hi",
        "mode": "assistant",
        "conversation_id": "br-fail",
        "intent_id": "int-fail-branch",
        "answer_branch": BRANCH,
    }
    with TestClient(app) as client:
        failed = _parse_sse(client.post("/chat", json=body).text)
        assert failed[-1][0] == "error"
        first_generation = db.get_chat_request("int-fail-branch")["generation_id"]
        record = app_main._persisted_answer("br-fail", first_generation)
        assert record is not None and record["meta"]["error"]
        assert record["meta"]["branch"] == BRANCH
        monkeypatch.setattr(chat_engine, "run_chat_engine", real)
        retried = _parse_sse(client.post("/chat", json=body).text)
    assert retried[0][1]["attempt"] == 2 and retried[-1][0] == "done"
    answers = _answers("br-fail")
    assert len(answers) == 1
    assert answers[0]["content"] == "Hello!"
    assert answers[0]["meta"]["branch"] == BRANCH


def test_a_version_send_is_logged_with_ids_and_an_ordinary_send_is_not(hello_stream, caplog):
    caplog.set_level(logging.INFO, logger="app.main")
    with TestClient(app) as client:
        client.post(
            "/chat",
            json={
                "message": "a secret question",
                "mode": "assistant",
                "conversation_id": "br-log",
                "intent_id": "int-log-plain",
            },
        )
        plain = [r.getMessage() for r in caplog.records if r.getMessage().startswith("answer_branch ")]
        assert plain == []
        client.post(
            "/chat",
            json={
                "message": "a secret question",
                "mode": "assistant",
                "conversation_id": "br-log",
                "intent_id": "int-log-regen",
                "answer_branch": BRANCH,
            },
        )
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("answer_branch ")]
    assert lines == [
        "answer_branch intent=int-log-regen conversation=br-log self=b-int-regen parent=b-question"
    ]
    assert all("secret" not in line for line in lines)
