"""Toggle-off isolation: regression tests for the review fixes of 2026-11-25
(review_escapes F1-F6, review_regression R2/R5-R8/R10/R12/R16/R17,
review_data F2/F5, review_mask F08/F09).

Offline and synthetic: the model stream is a fake that records what it was
handed, and repeats the marker when its prompt held it, like a model that was
shown the Salesforce text. No model, embedder or web call is made.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
import types
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
QUESTION = "what are good interview questions for a backend engineer?"


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
    app_main.memory._sessions.clear()
    yield
    app_main.memory._sessions.clear()


@pytest.fixture()
def quiet(monkeypatch):
    monkeypatch.setattr(settings, "living_knowledge_enabled", False)
    monkeypatch.setattr(settings, "fact_extraction_enabled", False)
    monkeypatch.setattr(settings, "cross_chat_semantic_enabled", False)
    monkeypatch.setattr(settings, "search_enabled", False)
    captured: list = []

    async def fake_stream(messages, **_kwargs):
        prompt = json.dumps(list(messages), ensure_ascii=False)
        captured.append(prompt)
        yield "token", (f"Echo: {MARKER}" if "ZQX7731" in prompt else "ok")

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    return captured


@pytest.fixture()
def owner(as_user):
    return int(as_user("local")["id"])


def _ask(client, conv, turns, text, mode="assistant", **extra):
    body = {
        "messages": [*turns, {"role": "user", "content": text}],
        "mode": mode,
        "effort": "fast",
        "web_search": "off",
        "session_id": f"rf-{uuid.uuid4().hex[:12]}",
        **({"conversation_id": conv} if conv else {}),
        **extra,
    }
    return client.post("/chat", json=body)


def _seed(user_id: int, conv: str, *, sf_answer: str = MARKER, tainted: bool = False) -> list:
    db.create_conversation(user_id, conv, "Mixed chat")
    rows = [
        ("user", "how do recruitment pipelines work?", None),
        ("assistant", EARLY, {"mode": "assistant"}),
        ("user", SF_QUESTION, None),
        ("assistant", sf_answer, {"mode": "salesforce", "route": "sql"}),
    ]
    if tainted:
        rows += [
            ("user", "make me a summary of that", None),
            ("assistant", f"Summary: {MARKER}.", {"mode": "assistant"}),
        ]
    for role, content, meta in rows:
        db.add_message(user_id, conv, role, content, meta)
    return [{"role": r, "content": c} for r, c, _m in rows]


def _quote(text: str, follow: str) -> str:
    """What frontend/lib/selectedContext.ts foldSelectedContext sends."""
    quoted = "\n".join(f"> {line}" if line else ">" for line in text.split("\n"))
    return f"Selected context from a previous assistant message:\n\n{quoted}\n\nUser follow-up:\n\n{follow}"


# ---------------------------------------------------------------------------
# F1: a Salesforce answer stopped or not stored yet
# ---------------------------------------------------------------------------


def _unstored_first_salesforce_answer(owner: int, conv: str):
    intent, gen = uuid.uuid4().hex, uuid.uuid4().hex
    db.create_conversation(owner, conv, "New chat")
    db.add_message(owner, conv, "user", "how do recruitment pipelines work?")
    db.add_message(owner, conv, "assistant", EARLY, {"mode": "assistant", "sf_isolation": 1})
    db.add_message(owner, conv, "user", SF_QUESTION, {"intent": {"id": intent}})
    db.create_chat_request(intent, owner, conv, gen, {"mode": "salesforce", "text": SF_QUESTION}, status="running")
    turns = [
        {"role": "user", "content": "how do recruitment pipelines work?"},
        {"role": "assistant", "content": EARLY},
        {"role": "user", "content": SF_QUESTION},
        {"role": "assistant", "content": MARKER},
    ]
    return intent, gen, turns


def test_an_unstored_salesforce_answer_makes_its_chat_holding(quiet, owner):
    """F1: the server's request row says the chat asked for Salesforce, so
    the partial the tab sends back matches no clean row and is hidden."""
    _intent, _gen, turns = _unstored_first_salesforce_answer(owner, "rf-f1a")
    with TestClient(app) as client:
        resp = _ask(client, "rf-f1a", turns, QUESTION)
    assert resp.status_code == 200, resp.text
    assert len(quiet) == 1 and "ZQX7731" not in quiet[0]
    assert S.TAINTED_PLACEHOLDER in quiet[0] and S.USER_PLACEHOLDER in quiet[0]
    assert EARLY in quiet[0], "the clean exchange before it stays"
    stored = db.list_messages("rf-f1a")[-1]
    assert stored["meta"].get("sf_isolation") == 1, "written from a clean prompt"


def test_the_answer_written_beside_an_unstored_partial_never_repeats_it(quiet, owner):
    intent, gen, turns = _unstored_first_salesforce_answer(owner, "rf-f1b")
    with TestClient(app) as client:
        _ask(client, "rf-f1b", turns, QUESTION)
        off_answer = db.list_messages("rf-f1b")[-1]
        # The tab's write-behind sync lands with the browser's leading meta.
        db.add_message(owner, "rf-f1b", "assistant", MARKER,
                       {"generation_id": gen, "intent_id": intent, "attempt": 1, "trace_id": gen})
        turns2 = [*turns, {"role": "user", "content": QUESTION}, {"role": "assistant", "content": off_answer["content"]}]
        _ask(client, "rf-f1b", turns2, "and three questions for a frontend engineer, in detail please")
    assert all("ZQX7731" not in p for p in quiet)


def test_the_leading_meta_names_the_mode(quiet, owner):
    """F1/F4: the browser's copy of a stopped answer is stored with this meta."""
    db.create_conversation(owner, "rf-lead", "Lead")
    with TestClient(app) as client:
        resp = _ask(client, "rf-lead", [], QUESTION, intent_id=uuid.uuid4().hex)
    events = _sse(resp.text)
    assert events[0][0] == "meta" and events[0][1]["mode"] == "assistant"


def test_a_salesforce_request_joins_the_cached_salesforce_chats(owner):
    """F1 + R2: the per-user set includes a chat whose only Salesforce trace is
    a request row, and a request accepted later drops a stale set."""
    db.create_conversation(owner, "rf-set", "Set")
    assert "rf-set" not in S.salesforce_chats(owner)
    db.create_chat_request(uuid.uuid4().hex, owner, "rf-set", uuid.uuid4().hex, {"mode": "salesforce"})
    S.note_salesforce_chat(owner, "rf-set")
    assert "rf-set" in S.salesforce_chats(owner)


# ---------------------------------------------------------------------------
# F2 / F3: quotes in the current turn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rules", ["ABCD", "", "off"])
def test_a_salesforce_quote_never_reaches_the_model_or_the_extractor(quiet, owner, monkeypatch, rules):
    """F2: E has no switch."""
    from app import facts

    monkeypatch.setattr(settings, "toggle_off_detector_rules", rules)
    monkeypatch.setattr(settings, "fact_extraction_enabled", True)
    seen: list = []

    async def spy_remember(gate, user_id, text, *a, **k):
        seen.append(text)
        return []

    monkeypatch.setattr(facts, "remember_after_route", spy_remember)
    conv = f"rf-f2-{rules or 'none'}"
    turns = _seed(owner, conv)
    quoted = _quote("Ravi Kumar closed 14 placements at Globex",
                    "Please write a warm congratulations note for this achievement for our team channel.")
    with TestClient(app) as client:
        resp = _ask(client, conv, turns, quoted)
    assert [d["text"] for e, d in _sse(resp.text) if e == "token"] == [S.REPLY_HIDDEN]
    assert quiet == [] and not any("Ravi Kumar" in t for t in seen)


LIST_ANSWER = "Top recruiters last quarter:\n\n1. Ravi Kumar - 14\n2. Anita Shah - 9\n3. Joel Dsouza - 7"
LIST_SELECTION = "Ravi Kumar - 14\nAnita Shah - 9\nJoel Dsouza - 7"
LINK_ANSWER = (
    "Contact: [Ravi Kumar](https://acme.lightning.force.com/lightning/r/Contact/0035g00000XyZAB/view), "
    "mobile 98765 43210, joined 2021."
)
LINK_SELECTION = "Contact: Ravi Kumar, mobile 98765 43210, joined 2021."


@pytest.mark.parametrize(
    "answer,selection",
    [(LIST_ANSWER, LIST_SELECTION), (LINK_ANSWER, LINK_SELECTION)],
    ids=["numbered-list", "record-link"],
)
def test_a_rendered_selection_of_a_salesforce_answer_is_kept_out(answer, selection):
    """F3: rendered text loses list numbers and link targets."""
    state = S.build_state([
        {"id": 1, "content": EARLY, "is_sf": False, "mode": "assistant", "stamped": False},
        {"id": 2, "content": answer, "is_sf": True, "mode": "salesforce", "stamped": False},
    ])
    assert S.quote_check(_quote(selection, "write a note"), state)
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": EARLY},
        {"role": "user", "content": _quote(selection, "write a note about these people")},
        {"role": "assistant", "content": "a later answer"},
    ]
    redacted, hidden = S.redact(history, state)
    assert "Ravi Kumar" not in redacted[2]["content"] and hidden.quotes_removed == 1
    assert redacted[2]["content"].endswith("write a note about these people")


def test_a_quote_stored_in_an_earlier_question_is_not_clean_text():
    """The allow-list reads only the person's own words of a stored question:
    a quote folded in before is still kept out when quoted again. A chart's
    legend renders in another order than the stored Markdown, so only the
    allow-list catches it."""
    chart = '```mermaid\npie\n"Ravi Kumar" : 14\n"Anita Shah" : 9\n```'
    legend = "Anita Shah 9\nRavi Kumar 14"
    state = S.build_state([
        {"id": 1, "content": EARLY, "is_sf": False, "mode": "assistant", "stamped": False},
        {"id": 2, "content": chart, "is_sf": True, "mode": "salesforce", "stamped": False},
        {"id": 3, "role": "user", "content": _quote(legend, "write a note"), "is_sf_question": False},
    ])
    assert S.quote_check(_quote(legend, "again please"), state) is True
    assert S.quote_check(_quote("write a note", "expand"), state) is False, "their own words stay"


def test_a_rendered_selection_of_a_clean_list_answer_is_kept():
    clean = "Interview stages:\n\n1. Phone screen with the recruiter\n2. Technical round with two engineers\n3. Culture fit"
    state = S.build_state([
        {"id": 1, "content": clean, "is_sf": False, "mode": "assistant", "stamped": False},
        {"id": 2, "content": LIST_ANSWER, "is_sf": True, "mode": "salesforce", "stamped": False},
    ])
    selection = "Phone screen with the recruiter\nTechnical round with two engineers\nCulture fit"
    assert S.quote_check(_quote(selection, "expand"), state) is False


def test_a_numbered_list_quote_never_reaches_the_web_query_rewrite(quiet, owner, monkeypatch):
    from app.engines import search

    monkeypatch.setattr(settings, "search_enabled", True)
    rewritten: list = []

    async def fake_router(messages, **_kwargs):
        rewritten.append(json.dumps(messages, ensure_ascii=False))
        return '["recruiter congratulations message ideas"]'

    async def fake_search_engine(message, history, emit, *a, **k):
        await search.rewrite_queries(message, history)
        await emit("meta", {"route": "search"})
        return "searched"

    monkeypatch.setattr(llm, "router_chat_completion", fake_router)
    monkeypatch.setattr(search, "run_search_engine", fake_search_engine)
    turns = _seed(owner, "rf-f3", sf_answer=LIST_ANSWER)
    quoted = _quote(LIST_SELECTION, "Write a congratulations note for the people in this quote, one line each, warm tone.")
    with TestClient(app) as client:
        resp = _ask(client, "rf-f3", turns, quoted, web_search="on")
    assert resp.status_code == 200, resp.text
    assert not any("Anita Shah" in r for r in rewritten) and quiet == []


# ---------------------------------------------------------------------------
# F4 / F5 / F6: rows the server writes, owns and titles
# ---------------------------------------------------------------------------


def test_a_failed_salesforce_answer_is_stored_with_its_mode(quiet, owner):
    """F4: it stays a Salesforce answer when its request row is gone."""
    conv, intent, gen = "rf-f4", uuid.uuid4().hex, uuid.uuid4().hex
    db.create_conversation(owner, conv, "Failed answer")
    db.add_message(owner, conv, "user", "how do recruitment pipelines work?")
    db.add_message(owner, conv, "assistant", EARLY, {"mode": "assistant", "sf_isolation": 1})
    db.add_message(owner, conv, "user", SF_QUESTION, {"intent": {"id": intent}})
    db.create_chat_request(intent, owner, conv, gen, {"mode": "salesforce", "text": SF_QUESTION}, status="running")
    failed = types.SimpleNamespace(
        user_id=owner, conversation_id=conv, generation_id=gen, intent_id=intent, attempt=1,
        answer_branch=None, error="The answer stopped.", error_code="engine_error", mode="salesforce",
    )
    asyncio.run(app_main._store_failure(failed, MARKER, resumable=False))
    partial = db.list_messages(conv)[-1]
    assert partial["meta"]["mode"] == "salesforce"
    with db.connection() as con:  # retention, simulated in the TEST database
        con.execute("DELETE FROM chat_requests WHERE intent_id = %s", (intent,))
    S.forget_all()
    assert S.build_state(db.salesforce_chat_state(owner, conv)).has_salesforce
    pending = {r["id"] for r in db.messages_missing_embeddings(owner, settings.embed_model or "m", limit=64)}
    assert partial["id"] not in pending


def test_a_detached_answer_is_stored_with_its_mode(owner):
    gen = app_main.LiveGeneration("rf-det", owner)
    db.create_conversation(owner, "rf-det", "Detached")
    gen.mode = "salesforce"
    gen.answer = MARKER
    gen.final_meta = {"route": "sql"}
    asyncio.run(app_main._finalize_generation("rf-det-key", gen))
    assert db.list_messages("rf-det")[-1]["meta"]["mode"] == "salesforce"


def test_a_client_put_cannot_stamp_a_tainted_answer_clean(quiet, owner):
    """F5: the stamp and mode are server-owned on PUT."""
    _seed(owner, "rf-f5", tainted=True)
    stored = db.list_messages("rf-f5")
    forged = []
    for m in stored:
        meta = dict(m.get("meta") or {})
        if m["role"] == "assistant" and m["content"].startswith("Summary:"):
            meta.update({"mode": "assistant", "sf_isolation": 1})
        if m["role"] == "assistant" and m["content"] == MARKER:
            meta.update({"mode": "assistant"})
        forged.append({"role": m["role"], "content": m["content"], "meta": meta})
    assert db.replace_messages(owner, "rf-f5", forged) is not None
    after = {m["content"]: m["meta"] for m in db.list_messages("rf-f5") if m["role"] == "assistant"}
    assert "sf_isolation" not in after[f"Summary: {MARKER}."]
    assert after[MARKER]["mode"] == "salesforce"
    turns = [{"role": m["role"], "content": m["content"]} for m in stored]
    with TestClient(app) as client:
        _ask(client, "rf-f5", turns, QUESTION)
    assert "ZQX7731" not in quiet[-1]


def test_a_put_keeps_the_servers_stamp_and_mode_by_position(owner):
    db.create_conversation(owner, "rf-f5b", "Carry")
    db.add_message(owner, "rf-f5b", "user", "q")
    db.add_message(owner, "rf-f5b", "assistant", "a guarded answer", {"mode": "assistant", "sf_isolation": 1})
    stripped = [{"role": m["role"], "content": m["content"], "meta": None} for m in db.list_messages("rf-f5b")]
    stripped.append({"role": "assistant", "content": "appended", "meta": {"mode": "assistant", "sf_isolation": 1}})
    db.replace_messages(owner, "rf-f5b", stripped)
    rows = db.list_messages("rf-f5b")
    assert rows[1]["meta"] == {"mode": "assistant", "sf_isolation": 1}
    assert rows[2]["meta"] == {"mode": "assistant"}, "a client can never add the stamp"


def test_a_client_post_cannot_add_the_stamp(owner):
    db.create_conversation(owner, "rf-f5c", "Post")
    with TestClient(app) as client:
        resp = client.post(
            "/history/conversations/rf-f5c/messages",
            json={"role": "assistant", "content": "x", "meta": {"mode": "assistant", "sf_isolation": 1}},
        )
    assert resp.status_code == 200, resp.text
    assert db.list_messages("rf-f5c")[-1]["meta"] == {"mode": "assistant"}


def test_a_title_is_never_written_from_a_salesforce_answer(owner, monkeypatch):
    """F6: any Salesforce answer, not only an sf_assistant one."""
    from app import titling

    db.create_conversation(owner, "rf-f6", "New chat")
    db.add_message(owner, "rf-f6", "user", "who closed the most placements at Globex last quarter?")
    db.add_message(owner, "rf-f6", "assistant", MARKER, {"mode": "salesforce", "route": "sql"})
    seen: list = []

    async def fake_title(question, answer):
        seen.append(answer)
        return "Placements question"

    monkeypatch.setattr(titling, "generate_title", fake_title)
    with TestClient(app) as client:
        resp = client.post("/history/conversations/rf-f6/title")
    assert resp.status_code == 200, resp.text
    assert seen == [""]


# ---------------------------------------------------------------------------
# R2 / R12 / R15: the server rows and the cached set
# ---------------------------------------------------------------------------


def test_a_downgraded_request_is_not_a_salesforce_answer(owner):
    """R12: the request snapshot is taken before the feature gate; the trace,
    written after it, says the turn ran in assistant mode."""
    db.create_conversation(owner, "rf-r12", "Downgraded")
    db.create_chat_request("int-r12", owner, "rf-r12", "gen-r12", {"message": "q", "mode": "salesforce"})
    db.start_query_trace("gen-r12", "rf-r12", owner, "", "q", "assistant")
    db.add_message(owner, "rf-r12", "user", "q", {"intent": {"id": "int-r12"}})
    db.add_message(owner, "rf-r12", "assistant", "an assistant answer", {"generation_id": "gen-r12", "mode": "assistant"})
    assert db.salesforce_chat_state(owner, "rf-r12") == []
    assert "rf-r12" not in db.salesforce_chat_ids(owner)


def test_the_set_holds_every_kind_of_salesforce_chat(owner):
    for conv in ("rf-meta", "rf-trace", "rf-req", "rf-plain"):
        db.create_conversation(owner, conv, conv)
    db.add_message(owner, "rf-meta", "assistant", "x", {"salesforce_scope": {"a": 1}})
    db.start_query_trace("gen-t", "rf-trace", owner, "", "q", "salesforce")
    db.create_chat_request("int-q", owner, "rf-req", "gen-q", {"mode": "salesforce"})
    db.add_message(owner, "rf-plain", "assistant", "y", {"mode": "assistant"})
    assert set(db.salesforce_chat_ids(owner)) >= {"rf-meta", "rf-trace", "rf-req"}
    assert "rf-plain" not in db.salesforce_chat_ids(owner)
    other = db.create_user("rf-other", "hash")
    assert db.salesforce_chat_ids(other) == []


def test_a_salesforce_answer_drops_only_a_set_that_misses_its_chat(owner, monkeypatch):
    db.create_conversation(owner, "rf-cache", "Cache")
    calls = {"n": 0}
    real = db.salesforce_chat_ids

    def counted(uid):
        calls["n"] += 1
        return real(uid)

    monkeypatch.setattr(db, "salesforce_chat_ids", counted)
    S.salesforce_chats(owner)
    S.salesforce_chats(owner)
    assert calls["n"] == 1, "cached"
    db.add_message(owner, "rf-cache", "assistant", "sf", {"mode": "salesforce"})
    assert "rf-cache" in S.salesforce_chats(owner) and calls["n"] == 2
    db.add_message(owner, "rf-cache", "assistant", "sf again", {"mode": "salesforce"})
    S.salesforce_chats(owner)
    assert calls["n"] == 2, "a chat already in the set changes nothing"


# ---------------------------------------------------------------------------
# data F2 / F5: questions
# ---------------------------------------------------------------------------


def test_a_salesforce_question_with_no_answer_is_hidden(owner):
    """data F2: an empty or missing answer hid nothing next to its question."""
    db.create_conversation(owner, "rf-d2", "Questions")
    db.add_message(owner, "rf-d2", "user", "general question")
    db.add_message(owner, "rf-d2", "assistant", EARLY, {"mode": "assistant"})
    db.create_chat_request("int-d2", owner, "rf-d2", "gen-d2", {"mode": "salesforce"})
    db.add_message(owner, "rf-d2", "user", "who joined in March?", {"intent": {"id": "int-d2"}})
    state = S.build_state(db.salesforce_chat_state(owner, "rf-d2"))
    history = [
        {"role": "user", "content": "general question"},
        {"role": "assistant", "content": EARLY},
        {"role": "user", "content": "who joined in March?"},
    ]
    out, hidden = S.redact(history, state)
    assert out[2]["content"] == S.USER_PLACEHOLDER and out[0]["content"] == "general question"


def test_a_toggle_off_answer_that_repeats_a_salesforce_one_keeps_its_question():
    """data F5: the answer is hidden; the question is the person's own."""
    rows = [
        {"id": 1, "role": "user", "content": SF_QUESTION, "is_sf_question": True},
        {"id": 2, "content": MARKER, "is_sf": True, "mode": "salesforce", "stamped": False},
        {"id": 3, "role": "user", "content": "say it again", "is_sf_question": False},
        {"id": 4, "content": MARKER, "is_sf": False, "mode": "assistant", "stamped": False},
    ]
    state = S.build_state(rows)
    history = [
        {"role": "user", "content": SF_QUESTION},
        {"role": "assistant", "content": MARKER},
        {"role": "user", "content": "say it again"},
        {"role": "assistant", "content": MARKER},
    ]
    out, _ = S.redact(history, state)
    assert out[0]["content"] == S.USER_PLACEHOLDER
    assert out[2]["content"] == "say it again"
    assert out[1]["content"] == out[3]["content"] == S.SF_ANSWER_PLACEHOLDER


# ---------------------------------------------------------------------------
# R5 / R6 / R8 / R16: the turn
# ---------------------------------------------------------------------------


def test_a_member_without_salesforce_is_never_told_to_turn_it_on(quiet, owner, ambient_identity):
    """R5."""
    ambient_identity["principal"] = dataclasses.replace(ambient_identity["principal"], features={"salesforce": False})
    db.create_conversation(owner, "rf-r5", "Plain")
    with TestClient(app) as client:
        resp = _ask(client, "rf-r5", [], "how many placements did we make last month")
    assert S.REPLY_NEEDS_SALESFORCE not in resp.text and len(quiet) == 1


def test_a_slow_state_read_counts_as_failed(monkeypatch):
    """R6: the read is bounded."""
    import time

    def slow(*_a, **_k):
        time.sleep(1.0)
        return []

    monkeypatch.setattr(db, "salesforce_chat_state", slow)
    monkeypatch.setattr(settings, "toggle_off_state_timeout_s", 0.1)
    started = time.perf_counter()
    prepared = asyncio.run(S.prepare(1, "x", [{"role": "assistant", "content": EARLY}], "q", fail_closed=True))
    assert time.perf_counter() - started < 0.8
    assert prepared.state.read_failed and prepared.history[0]["content"] == S.TAINTED_PLACEHOLDER


def test_a_failed_read_in_a_plain_chat_says_so_without_blaming_salesforce(quiet, owner, monkeypatch):
    """R6: hidden context (fail-closed), its own status line, no D reply."""
    def broken(*_a, **_k):
        raise RuntimeError("database down")

    monkeypatch.setattr(db, "salesforce_chat_state", broken)
    db.create_conversation(owner, "rf-r6", "Plain")
    turns = [{"role": "user", "content": "q"}, {"role": "assistant", "content": EARLY}]
    with TestClient(app) as client:
        resp = _ask(client, "rf-r6", turns, "what about him")
    assert S.STATUS_LINE_UNCHECKED in resp.text and S.STATUS_LINE not in resp.text
    tokens = "".join(d["text"] for e, d in _sse(resp.text) if e == "token")
    assert tokens == "ok", tokens
    assert len(quiet) == 1 and EARLY not in quiet[0]


def test_the_state_is_built_off_the_event_loop(monkeypatch):
    """R8: hashing and shingling a long chat runs in a worker thread."""
    seen: list = []
    real = S.build_state

    def spy(rows):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(rows)

    monkeypatch.setattr(S, "build_state", spy)
    monkeypatch.setattr(db, "salesforce_chat_state", lambda *_a: [])
    asyncio.run(S.prepare(1, "x", [], "q", fail_closed=True))
    assert seen == [False]


def test_the_own_material_reads_run_once(quiet, owner, monkeypatch):
    """R16: a records shape reads uploads, documents, pages and files once,
    and the context reads reuse the rows."""
    counts = {"uploads": 0, "documents": 0}
    real_uploads, real_documents = db.get_uploads, db.get_documents

    def uploads(*a, **k):
        counts["uploads"] += 1
        return real_uploads(*a, **k)

    def documents(*a, **k):
        counts["documents"] += 1
        return real_documents(*a, **k)

    monkeypatch.setattr(db, "get_uploads", uploads)
    monkeypatch.setattr(db, "get_documents", documents)
    db.create_conversation(owner, "rf-r16", "Plain")
    with TestClient(app) as client:
        resp = _ask(client, "rf-r16", [], "how many placements did we make last month")
    assert S.REPLY_NEEDS_SALESFORCE in resp.text
    assert counts == {"uploads": 1, "documents": 1}


# ---------------------------------------------------------------------------
# R10: the prompt rule has a switch
# ---------------------------------------------------------------------------


def test_the_prompt_rule_switch_restores_the_prompt_before_it(monkeypatch):
    from app.core.personal_details import PERSONAL_DETAILS_RULE
    from app.engines import agent, chat

    system = chat._messages("q", [], "assistant")[0]["content"]
    assert PERSONAL_DETAILS_RULE in system and "fictional sample is fine" in PERSONAL_DETAILS_RULE
    monkeypatch.setattr(settings, "toggle_off_prompt_rule", False)
    system = chat._messages("q", [], "assistant")[0]["content"]
    assert PERSONAL_DETAILS_RULE not in system and system.startswith(chat.ASSISTANT_SYSTEM_BASE)
    assert agent._personal_details_rule(False) == ""
    assert PERSONAL_DETAILS_RULE not in agent._synthesis_messages("q", [], salesforce=False)[0]["content"]


# ---------------------------------------------------------------------------
# F08 / F09 / R17: the engine around the mask
# ---------------------------------------------------------------------------

INVENTED = "98450 12345"


def _run_engine(monkeypatch, *, reasoning=(), tokens=(), effort="fast", script=None):
    from app import continuation
    from app.engines import chat

    async def fake_stream(messages, **_kwargs):
        if script is not None:
            for kind, text in script:
                yield kind, text
            return
        for r in reasoning:
            yield "reasoning", r
        for t in tokens:
            yield "token", t

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    monkeypatch.setattr(continuation, "budget_for", lambda effort: 8000)
    events: list = []

    async def emit(event, data):
        events.append((event, data))

    answer = asyncio.run(chat.run_chat_engine("q", [], emit, mode="assistant", effort=effort))
    return answer, events


def test_a_phone_split_across_a_token_in_the_reasoning_stays_masked(monkeypatch):
    """F09: reasoning that resumes after a token finishes the held number."""
    _answer, events = _run_engine(
        monkeypatch,
        script=[("reasoning", "his number might be +91 98450"), ("token", "Answer."),
                ("reasoning", " 12345 I think"), ("token", " Done.")],
        effort="think",
    )
    reasoning = "".join(d["text"] for e, d in events if e == "reasoning")
    assert INVENTED not in reasoning.replace("+91 ", "") and "[phone not available]" in reasoning


def test_a_released_reasoning_tail_keeps_its_event_keys(monkeypatch):
    """R17."""
    from app.engines import chat

    sent: list = []

    async def emit(event, data):
        sent.append((event, data))

    mask = chat._DetailsMask([{"role": "user", "content": "q"}], emit)

    async def run():
        await mask.emit("reasoning", {"text": "thinking about it", "segment": 2})
        await mask.emit("meta", {"route": "chat"})

    asyncio.run(run())
    tails = [d for e, d in sent if e == "reasoning"]
    assert tails and all(d.get("segment") == 2 for d in tails)
    assert "".join(d["text"] for d in tails) == "thinking about it"


def test_the_best_of_judge_reason_is_masked(monkeypatch):
    """F08."""
    from app.core import best_of

    monkeypatch.setattr(settings, "extra_high_samples", 3)

    async def fake_generate(messages, *, n, temperature, max_tokens, on_candidate=None):
        cands = [best_of.Candidate(index=i + 1, reasoning=f"his number is {INVENTED}", answer=f"Ravi: {INVENTED}")
                 for i in range(n)]
        if on_candidate:
            for k, c in enumerate(cands, 1):
                await on_candidate(c, k, n)
        return cands

    async def fake_select(question, candidates):
        return candidates[0], f"Draft 1 gives the number {INVENTED} that was asked for."

    monkeypatch.setattr(best_of, "generate_candidates", fake_generate)
    monkeypatch.setattr(best_of, "select_best", fake_select)
    answer, events = _run_engine(monkeypatch, tokens=["fallback answer"], effort="max")
    assert INVENTED not in answer
    assert INVENTED not in json.dumps([d for _e, d in events], ensure_ascii=False)
