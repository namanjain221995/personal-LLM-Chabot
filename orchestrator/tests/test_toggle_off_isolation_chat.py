"""Toggle-off isolation through POST /chat (design_v2 §7, owner order 2026-11-25).

With the Salesforce toggle OFF, no earlier Salesforce answer, no answer written
from a prompt that held one, and no quote of either reaches the model; the
summary, folded chunks and compaction are not read in a chat that holds a
Salesforce answer; a records question gets a fixed reply before any model call.
With the toggle ON nothing changes.

Offline: the model stream is a fake that records the messages it was handed;
nothing calls a model, an embedder or the web.
"""
from __future__ import annotations

import dataclasses
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import db, llm
from app import main as app_main
from app import sf_isolation as S
from app.config import settings
from app.main import app

MARKER = "ZQX7731 Ravi Kumar closed 14 placements at Globex"
SF_QUESTION = "who closed the most placements last quarter?"
EARLY = "Recruitment pipelines usually have five stages: sourcing, screening, interview, offer, joining."


def _sse(text: str) -> list:
    out, event = [], None
    for line in text.splitlines():
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event:
            out.append((event, json.loads(line[6:])))
    return out


@pytest.fixture(autouse=True)
def _own_session_memory():
    """The in-process session memory is process-wide: a fixed reply left under
    the default key would be the "last answer" of the next file's bare call."""
    app_main.memory._sessions.clear()
    yield
    app_main.memory._sessions.clear()


@pytest.fixture()
def quiet(monkeypatch):
    """No sidecar, router, extractor or web: only the main stream, faked."""
    monkeypatch.setattr(settings, "living_knowledge_enabled", False)
    monkeypatch.setattr(settings, "fact_extraction_enabled", False)
    monkeypatch.setattr(settings, "cross_chat_semantic_enabled", False)
    monkeypatch.setattr(settings, "search_enabled", False)
    captured: list = []

    async def fake_stream(messages, **_kwargs):
        captured.append(json.dumps(list(messages), ensure_ascii=False))
        yield "token", "ok"

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    return captured


@pytest.fixture()
def owner(as_user):
    return int(as_user("local")["id"])


def _seed(user_id: int, conv: str, *, tainted: bool = False) -> list:
    """A mixed chat: an early assistant-mode pair, a Salesforce pair, and
    optionally a pre-guard toggle-off answer that repeats the Salesforce one."""
    db.create_conversation(user_id, conv, "Mixed chat")
    rows = [
        ("user", "how do recruitment pipelines work?", None),
        ("assistant", EARLY, {"mode": "assistant"}),
        ("user", SF_QUESTION, None),
        ("assistant", MARKER, {"mode": "salesforce", "route": "sql"}),
    ]
    if tainted:
        rows += [
            ("user", "make me a summary of that", None),
            ("assistant", f"Summary: {MARKER}.", {"mode": "assistant"}),
        ]
    for role, content, meta in rows:
        db.add_message(user_id, conv, role, content, meta)
    return [{"role": r, "content": c} for r, c, _m in rows]


def _ask(client, conv, turns, text, mode="assistant", **extra):
    body = {
        "messages": [*turns, {"role": "user", "content": text}],
        "mode": mode,
        "effort": "fast",
        "web_search": "off",
        "session_id": f"iso-{uuid.uuid4().hex[:12]}",
        **({"conversation_id": conv} if conv else {}),
        **extra,
    }
    return client.post("/chat", json=body)


QUESTION = "what are good interview questions for a backend engineer?"


def test_an_off_turn_sees_placeholders_and_never_the_salesforce_answer(quiet, owner):
    turns = _seed(owner, "iso-1")
    with TestClient(app) as client:
        resp = _ask(client, "iso-1", turns, QUESTION)
    assert resp.status_code == 200, resp.text
    assert len(quiet) == 1
    prompt = quiet[0]
    assert "ZQX7731" not in prompt and "Ravi Kumar" not in prompt
    assert SF_QUESTION not in prompt, "the Salesforce question can name people too"
    assert S.SF_ANSWER_PLACEHOLDER in prompt and S.USER_PLACEHOLDER in prompt
    assert EARLY in prompt, "an answer before the first Salesforce answer is clean"


def test_a_pre_guard_answer_that_repeats_the_salesforce_one_is_hidden_too(quiet, owner):
    """C1: stored with meta.mode='assistant' but written from a prompt that held
    the Salesforce answer, and not stamped."""
    turns = _seed(owner, "iso-2", tainted=True)
    with TestClient(app) as client:
        _ask(client, "iso-2", turns, QUESTION)
    prompt = quiet[0]
    assert "ZQX7731" not in prompt
    assert S.TAINTED_PLACEHOLDER in prompt
    assert "make me a summary of that" in prompt, "the person's own toggle-off words stay"


def test_summary_recall_and_compaction_are_skipped_and_the_status_line_said_once(quiet, owner, monkeypatch):
    from app import compaction, recall

    started: list = []
    real_start = app_main._ContextReads.start

    def spy_start(self, key, factory):
        started.append(key)
        return real_start(self, key, factory)

    calls = {"summary": 0, "recall": 0, "compaction": 0, "background": 0}

    def no_summary(*_a, **_k):
        calls["summary"] += 1
        return None

    def no_recall(*_a, **_k):
        calls["recall"] += 1
        return ""

    async def no_compaction(*_a, **_k):
        calls["compaction"] += 1
        raise AssertionError("compaction must not run")

    def no_background(*_a, **_k):
        calls["background"] += 1

    monkeypatch.setattr(app_main._ContextReads, "start", spy_start)
    monkeypatch.setattr(db, "get_summary", no_summary)
    monkeypatch.setattr(recall, "retrieve_block", no_recall)
    monkeypatch.setattr(compaction, "prepare", no_compaction)
    monkeypatch.setattr(compaction, "prepare_deferred", no_compaction)
    monkeypatch.setattr(app_main, "_spawn_background_compaction", no_background)
    for concurrent in ("true", "false"):
        # A chat of its own per run: the first run's answer is guarded, and
        # after it the status line is no longer due.
        turns = _seed(owner, f"iso-3-{concurrent}")
        monkeypatch.setenv("CONTEXT_CONCURRENT_READS", concurrent)
        monkeypatch.setattr(settings, "context_concurrent_reads", concurrent == "true", raising=False)
        started.clear()
        with TestClient(app) as client:
            resp = _ask(client, f"iso-3-{concurrent}", turns, QUESTION)
        assert resp.status_code == 200
        assert "summary" not in started and "recall" not in started
        status = [d["text"] for e, d in _sse(resp.text) if e == "status"]
        assert status.count(S.STATUS_LINE) == 1
    assert calls == {"summary": 0, "recall": 0, "compaction": 0, "background": 0}


def test_the_status_line_stops_once_a_guarded_answer_follows(quiet, owner):
    turns = _seed(owner, "iso-3b")
    with TestClient(app) as client:
        first = _ask(client, "iso-3b", turns, QUESTION)
        stored = db.list_messages("iso-3b")[-1]
        second = _ask(client, "iso-3b", [*turns, {"role": "user", "content": QUESTION},
                                         {"role": "assistant", "content": stored["content"]}], "and for a frontend engineer?")
    assert S.STATUS_LINE in first.text
    assert S.STATUS_LINE not in second.text


def test_a_records_follow_up_gets_the_fixed_reply_before_any_model_call(quiet, owner, monkeypatch):
    """C17: no answer engine, artifact intent hook, auto-orchestration, knowledge
    pre-pass, extractor, read or embedding runs for a blocked turn."""
    from app import facts
    from app.artifacts import intent_llm
    from app.engines import chat as chat_engine
    from app.engines import orchestrate

    def forbidden(name):
        def _raise(*_a, **_k):
            raise AssertionError(f"{name} must not run on a blocked turn")

        async def _araise(*_a, **_k):
            raise AssertionError(f"{name} must not run on a blocked turn")

        return _raise, _araise

    started: list = []
    real_start = app_main._ContextReads.start
    monkeypatch.setattr(app_main._ContextReads, "start", lambda self, key, factory: started.append(key) or real_start(self, key, factory))
    monkeypatch.setattr(settings, "living_knowledge_enabled", True)
    monkeypatch.setattr(settings, "fact_extraction_enabled", True)
    monkeypatch.setattr(settings, "cross_chat_semantic_enabled", True)
    monkeypatch.setattr(chat_engine, "run_chat_engine", forbidden("the chat engine")[1])
    monkeypatch.setattr(orchestrate, "decide", forbidden("auto-orchestration")[1])
    monkeypatch.setattr(intent_llm, "make_hook", forbidden("the artifact intent hook")[0])
    monkeypatch.setattr(app_main, "_prepare_knowledge", forbidden("the knowledge pre-pass")[1])
    monkeypatch.setattr(facts, "remember_from_message", forbidden("the fact extractor")[1])
    monkeypatch.setattr(llm, "stream_chat_events", forbidden("the main model")[1])
    monkeypatch.setattr(llm, "router_chat_completion", forbidden("the router model")[1])
    monkeypatch.setattr(llm, "embed_texts", forbidden("the embedder")[1])
    turns = _seed(owner, "iso-4")
    with TestClient(app) as client:
        resp = _ask(client, "iso-4", turns, "plot this", effort="think")
    assert resp.status_code == 200, resp.text
    events = _sse(resp.text)
    tokens = [d["text"] for e, d in events if e == "token"]
    assert tokens == [S.REPLY_HIDDEN]
    meta = [d for e, d in events if e == "meta" and "route" in d][-1]
    assert meta["route"] == "chat" and meta["toggle_off_guard"] == "hidden_followup" and meta["rule"] == "D"
    assert meta["sf_isolation"] == 1 and meta["mode"] == "assistant"
    assert started == [], "a blocked turn starts no context read"
    stored = db.list_messages("iso-4")[-1]
    assert stored["role"] == "assistant" and stored["content"] == S.REPLY_HIDDEN
    assert stored["meta"]["sf_isolation"] == 1 and stored["meta"]["mode"] == "assistant"


def test_a_records_question_in_a_chat_with_no_salesforce_answer_gets_the_fixed_reply(quiet, owner):
    with TestClient(app) as client:
        resp = _ask(client, "iso-5", [], "how many placements last month")
    events = _sse(resp.text)
    assert [d["text"] for e, d in events if e == "token"] == [S.REPLY_NEEDS_SALESFORCE]
    assert quiet == []


def test_a_quote_of_a_hidden_answer_in_the_current_text_gets_reply_e(quiet, owner):
    turns = _seed(owner, "iso-6")
    quoted = (
        "Selected context from a previous assistant message:\n\n> Ravi Kumar closed 14 placements at Globex\n\n"
        "User follow-up:\n\ndraft a congratulations note to him"
    )
    with TestClient(app) as client:
        resp = _ask(client, "iso-6", turns, quoted)
    events = _sse(resp.text)
    assert [d["text"] for e, d in events if e == "token"] == [S.REPLY_HIDDEN]
    meta = [d for e, d in events if e == "meta" and "route" in d][-1]
    assert meta["rule"] == "E"
    assert quiet == []


def test_the_stored_answer_of_an_off_turn_carries_the_stamp(quiet, owner):
    turns = _seed(owner, "iso-7")
    with TestClient(app) as client:
        _ask(client, "iso-7", turns, QUESTION)
    stored = db.list_messages("iso-7")[-1]
    assert stored["role"] == "assistant" and stored["meta"]["sf_isolation"] == 1


def test_with_the_flag_off_the_prompt_is_todays(quiet, owner, monkeypatch):
    monkeypatch.setattr(settings, "toggle_off_isolation", False)
    turns = _seed(owner, "iso-8")
    with TestClient(app) as client:
        resp = _ask(client, "iso-8", turns, QUESTION)
        records = _ask(client, "iso-8", turns, "how many placements last month")
    assert "ZQX7731" in quiet[0], "no redaction"
    assert S.STATUS_LINE not in resp.text
    assert "sf_isolation" not in (db.list_messages("iso-8")[-1]["meta"] or {})
    assert S.REPLY_NEEDS_SALESFORCE not in records.text, "no fixed reply"
    assert len(quiet) == 2


def test_a_toggle_on_turn_in_the_same_chat_still_sees_the_salesforce_answer(quiet, owner, monkeypatch):
    seen: list = []

    class FakeGraph:
        async def ainvoke(self, state):
            seen.append(json.dumps(state["history"]))
            await state["emit"]("meta", {"route": "sql"})
            return {"answer": "from salesforce"}

    monkeypatch.setattr(app_main, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", False)
    monkeypatch.setattr(settings, "clarify_before_answering", False)
    turns = _seed(owner, "iso-9")
    with TestClient(app) as client:
        _ask(client, "iso-9", turns, "and the quarter before?", mode="salesforce")
    assert seen and "ZQX7731" in seen[0]


def test_a_chat_with_no_salesforce_answer_gets_the_same_prompt_as_before(quiet, owner, monkeypatch):
    db.create_conversation(owner, "iso-10", "Plain chat")
    db.add_message(owner, "iso-10", "user", "how do recruitment pipelines work?")
    db.add_message(owner, "iso-10", "assistant", EARLY, {"mode": "assistant"})
    turns = [{"role": "user", "content": "how do recruitment pipelines work?"}, {"role": "assistant", "content": EARLY}]
    with TestClient(app) as client:
        _ask(client, "iso-10", turns, QUESTION, session_id="iso-10-a")
        monkeypatch.setattr(settings, "toggle_off_isolation", False)
        _ask(client, "iso-10", turns, QUESTION, session_id="iso-10-b")
    assert len(quiet) == 2 and quiet[0] == quiet[1]


def test_the_artifact_lists_of_an_off_turn_leave_out_salesforce_made_files(quiet, owner, monkeypatch):
    from app.artifacts import db as adb
    from app.engines import artifact as artifact_engine

    def job(mode):
        artifact_id = uuid.uuid4().hex
        adb.create_artifact_job(
            job_id=uuid.uuid4().hex, artifact_id=artifact_id, user_id=owner, conversation_id="iso-11",
            generation_id=uuid.uuid4().hex, operation="create", instruction="sheet", kind="workbook",
            requested_formats=["xlsx"], selected_formats=["xlsx"], format_reason="asked", effort="fast",
            mode=mode, template_id="generic", idempotency_key=uuid.uuid4().hex, title=f"{mode} sheet",
        )
        return artifact_id

    db.create_conversation(owner, "iso-11", "Files")
    mine, theirs = job("assistant"), job("salesforce")
    listed: list = []
    real = adb.list_artifacts

    def spy(*args, **kwargs):
        rows = real(*args, **kwargs)
        listed.append((kwargs, {r["id"] for r in rows}))
        return rows

    monkeypatch.setattr(artifact_engine.adb, "list_artifacts", spy)
    with TestClient(app) as client:
        _ask(client, "iso-11", [], "what does this sheet have?")
        _ask(client, "iso-11", [], "add a column for the notice period")
    assert listed, "the turn read the chat's artifacts"
    for kwargs, ids in listed:
        assert kwargs == {"exclude_salesforce": True}
        assert theirs not in ids and mine in ids


def test_a_state_read_failure_hides_context_only_for_a_principal_with_salesforce(quiet, owner, ambient_identity, monkeypatch):
    """C16."""
    def broken(*_a, **_k):
        raise RuntimeError("database down")

    monkeypatch.setattr(db, "salesforce_chat_state", broken)
    turns = _seed(owner, "iso-12")
    with TestClient(app) as client:
        resp = _ask(client, "iso-12", turns, QUESTION, session_id="iso-12-a")
        ambient_identity["principal"] = dataclasses.replace(
            ambient_identity["principal"], features={"salesforce": False}
        )
        _ask(client, "iso-12", turns, QUESTION, session_id="iso-12-b")
    closed, opened = quiet
    assert "ZQX7731" not in closed and EARLY not in closed and S.TAINTED_PLACEHOLDER in closed
    # Review R6: the line says the rows could not be checked, not that they
    # came from Salesforce.
    assert S.STATUS_LINE_UNCHECKED in resp.text and S.STATUS_LINE not in resp.text
    assert "ZQX7731" in opened, "a member without Salesforce keeps today's context"
    answers = [m for m in db.list_messages("iso-12") if m["role"] == "assistant"][-2:]
    assert answers[0]["meta"].get("sf_isolation") == 1, "a fully hidden context is clean"
    assert "sf_isolation" not in answers[1]["meta"], "an unredacted context never counts as clean"


def test_session_memory_without_a_conversation_drops_salesforce_pairs(quiet, owner, monkeypatch):
    """C5: no conversation id and no turns read the account-wide session key."""
    class FakeGraph:
        async def ainvoke(self, state):
            await state["emit"]("token", {"text": MARKER})
            await state["emit"]("meta", {"route": "sql"})
            return {"answer": MARKER}

    monkeypatch.setattr(app_main, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", False)
    monkeypatch.setattr(settings, "clarify_before_answering", False)
    with TestClient(app) as client:
        client.post("/chat", json={"message": SF_QUESTION, "mode": "salesforce", "effort": "fast", "session_id": "shared"})
        client.post("/chat", json={"message": QUESTION, "mode": "assistant", "effort": "fast", "session_id": "shared"})
    assert quiet and "ZQX7731" not in quiet[-1] and SF_QUESTION not in quiet[-1]


def test_a_search_query_after_a_hidden_pair_holds_no_placeholder_words(quiet, owner, monkeypatch):
    """C6: the query rewriter reads conversation_turns, which drop placeholders."""
    from app.engines import search

    monkeypatch.setattr(settings, "search_enabled", True)
    rewritten: list = []

    async def fake_router(messages, **_kwargs):
        rewritten.append(json.dumps(messages))
        return '["exoplanet transit news"]'

    async def fake_search_engine(message, history, emit, *a, **k):
        await search.rewrite_queries(message, history)
        await emit("meta", {"route": "search"})
        return "searched"

    monkeypatch.setattr(llm, "router_chat_completion", fake_router)
    monkeypatch.setattr(search, "run_search_engine", fake_search_engine)
    turns = _seed(owner, "iso-14")
    with TestClient(app) as client:
        _ask(client, "iso-14", turns, "latest exoplanet transit news", web_search="on")
    assert rewritten, "the search path ran"
    for words in ("toggle", "not shown", "ZQX7731"):
        assert words not in rewritten[-1]
