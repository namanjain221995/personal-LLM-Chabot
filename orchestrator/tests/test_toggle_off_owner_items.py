"""Toggle-off guard, owner items 3 and 4 (2026-11-25).

Item 3 (data review F3): conversation ids listed in
TOGGLE_OFF_FROZEN_HOLDING_CHATS count as Salesforce-holding although no
stored or server row says so. Their unstamped answers are hidden on a
toggle-off turn; cross-chat recall and new vectors leave them out. No row
changes.

Item 4 (owner decision 3 widened): a chat that holds Salesforce gets no NEW
folded-chunk vector either; chunks already stored stay.

Made-up data only. Offline: the embedder and the main stream are fakes.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import compaction, db, llm, recall, summarize
from app import main as app_main
from app import sf_isolation as S
from app.config import _id_set, settings
from app.main import app

FROZEN = "frozen-chat-1"
# A pre-guard toggle-off answer that repeats a Salesforce answer of another chat.
REPEAT = "SELECT Name FROM Widget__c WHERE Stage__c = 'Quokka Ready' AND Tier__c = 'Q7'"
QUESTION_1 = "write me a query for the widgets that are ready"


@pytest.fixture()
def frozen(monkeypatch):
    def _set(*ids: str) -> None:
        monkeypatch.setattr(settings, "toggle_off_frozen_holding_chats", frozenset(ids), raising=False)
        S.forget_all()

    return _set


@pytest.fixture()
def world():
    """ana: a frozen-to-be chat (no Salesforce row at all) and a plain chat;
    ben: a chat with the same id string listed, to prove the owner scope."""
    uid = db.create_user("owner-ana", "hash")
    db.create_conversation(uid, FROZEN, "Quokka widgets")
    db.add_message(uid, FROZEN, "user", QUESTION_1)
    db.add_message(uid, FROZEN, "assistant", REPEAT, {"mode": "assistant"})
    db.create_conversation(uid, "plain-chat", "Quokka garden")
    db.add_message(uid, "plain-chat", "user", "Quokka garden plans for spring")
    db.add_message(uid, "plain-chat", "assistant", "Quokka garden: plant tulips in October.", {"mode": "assistant"})
    other = db.create_user("owner-ben", "hash")
    db.create_conversation(other, "ben-chat", "Quokka for ben")
    db.add_message(other, "ben-chat", "user", "Quokka question from ben")
    db.add_message(other, "ben-chat", "assistant", "Quokka answer for ben.", {"mode": "assistant"})
    return uid, other


# ---------------------------------------------------------------------------
# Item 3: the setting
# ---------------------------------------------------------------------------


def test_the_setting_lists_ids_by_comma_or_space_and_is_empty_by_default(monkeypatch):
    assert _id_set("a-1, b-2  c-3,,\n") == frozenset({"a-1", "b-2", "c-3"})
    assert _id_set("") == frozenset() and _id_set(None) == frozenset()
    from app import config

    monkeypatch.delenv("TOGGLE_OFF_FROZEN_HOLDING_CHATS", raising=False)
    assert config.Settings().toggle_off_frozen_holding_chats == frozenset()
    monkeypatch.setenv("TOGGLE_OFF_FROZEN_HOLDING_CHATS", "x-1,y-2")
    assert config.Settings().toggle_off_frozen_holding_chats == frozenset({"x-1", "y-2"})


def test_a_frozen_chat_holds_salesforce_and_its_unstamped_answer_is_hidden(world, frozen):
    uid, _ = world
    assert db.salesforce_chat_state(uid, FROZEN) == [], "before: no row says Salesforce"
    frozen(FROZEN)
    rows = db.salesforce_chat_state(uid, FROZEN)
    assert rows and all(r["holding"] for r in rows) and not any(r["is_sf"] for r in rows)
    state = S.build_state(rows)
    assert state.has_salesforce and state.tainted_answers == 1 and state.announce
    history = [{"role": "user", "content": QUESTION_1}, {"role": "assistant", "content": REPEAT}]
    out, hidden = S.redact(history, state)
    assert out[1]["content"] == S.TAINTED_PLACEHOLDER and hidden.tainted == 1
    assert out[0]["content"] == QUESTION_1, "the person's own words stay"
    quoted = f"Selected context from a previous assistant message:\n> {REPEAT}\n\nUser follow-up:\n\nwhy?"
    assert S.quote_check(quoted, state), "a quote of the hidden answer is kept out"


def test_a_stamped_answer_in_a_frozen_chat_stays_clean(world, frozen):
    uid, _ = world
    frozen(FROZEN)
    db.add_message(uid, FROZEN, "user", "and a general tip?")
    db.add_message(uid, FROZEN, "assistant", "Name tables in the singular.", {"mode": "assistant", "sf_isolation": 1})
    state = S.build_state(db.salesforce_chat_state(uid, FROZEN))
    history = [{"role": "assistant", "content": REPEAT}, {"role": "assistant", "content": "Name tables in the singular."}]
    out, _ = S.redact(history, state)
    assert out[0]["content"] == S.TAINTED_PLACEHOLDER
    assert out[1]["content"] == "Name tables in the singular."
    assert not state.announce, "a guarded answer already followed"


def test_the_frozen_list_is_scoped_to_the_owner_and_ignores_unknown_ids(world, frozen):
    uid, other = world
    frozen(FROZEN, "ben-chat", "no-such-chat")
    assert set(db.salesforce_chat_ids(uid)) == {FROZEN}
    assert set(db.salesforce_chat_ids(other)) == {"ben-chat"}
    assert db.salesforce_chat_state(uid, "ben-chat") == [], "another user's chat is never read"
    assert db.salesforce_chat_state(uid, "no-such-chat") == []


def test_cross_chat_recall_and_new_vectors_leave_a_frozen_chat_out(world, frozen):
    uid, _ = world
    from app.recall import pack_vector

    # Vectors written before the freeze stay in the table; the read filters them.
    rows = db.messages_missing_embeddings(uid, settings.embed_model, limit=64, min_chars=1)
    assert FROZEN in {r["conversation_id"] for r in rows}
    db.store_message_embeddings(
        uid, settings.embed_model, 3,
        [{"message_id": r["id"], "conversation_id": r["conversation_id"], "embedding": pack_vector([1.0, 0.0, 0.0])}
         for r in rows if r["role"] == "user"],
    )
    frozen(FROZEN)
    pending = {r["conversation_id"] for r in db.messages_missing_embeddings(uid, settings.embed_model, limit=64, min_chars=1)}
    assert FROZEN not in pending and "plain-chat" in pending
    candidates = {r["conversation_id"] for r in db.fetch_message_embeddings(uid, settings.embed_model, None)}
    assert candidates == {"plain-chat"}
    hits = db.recall_conversations(uid, ["quokka"], "current", limit=5, exclude_salesforce_chats=True)
    assert [h["id"] for h in hits] == ["plain-chat"]
    with db.connection() as con:
        kept = con.execute(
            "SELECT count(*) AS n FROM message_embeddings WHERE conversation_id = %s", (FROZEN,)
        ).fetchone()["n"]
    assert kept == 1, "nothing is deleted"
    assert [m["meta"].get("mode") for m in db.list_messages(FROZEN) if m["role"] == "assistant"] == ["assistant"], (
        "no stored row is rewritten"
    )


def test_with_isolation_off_the_frozen_list_does_nothing(world, frozen, monkeypatch):
    uid, _ = world
    frozen(FROZEN)
    monkeypatch.setattr(settings, "toggle_off_isolation", False)
    S.forget_all()
    assert S.frozen_holding_chats() == frozenset()
    assert db.salesforce_chat_state(uid, FROZEN) == []
    assert db.salesforce_chat_ids(uid) == []


# -- through POST /chat ------------------------------------------------------


def _ask(client, conv, turns, text):
    return client.post(
        "/chat",
        json={
            "messages": [*turns, {"role": "user", "content": text}],
            "mode": "assistant",
            "effort": "fast",
            "web_search": "off",
            "session_id": f"own-{uuid.uuid4().hex[:12]}",
            "conversation_id": conv,
        },
    )


@pytest.fixture()
def quiet(monkeypatch):
    monkeypatch.setattr(settings, "living_knowledge_enabled", False)
    monkeypatch.setattr(settings, "fact_extraction_enabled", False)
    monkeypatch.setattr(settings, "cross_chat_semantic_enabled", False)
    monkeypatch.setattr(settings, "search_enabled", False)
    captured: list = []

    async def fake_stream(messages, **_kwargs):
        captured.append(json.dumps(list(messages), ensure_ascii=False))
        yield "token", "ok"

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    app_main.memory._sessions.clear()
    yield captured
    app_main.memory._sessions.clear()


@pytest.mark.parametrize("listed", [False, True])
def test_a_toggle_off_turn_in_a_frozen_chat_never_sees_the_repeated_answer(quiet, as_user, frozen, listed):
    uid = int(as_user("local")["id"])
    conv = f"frozen-e2e-{int(listed)}"
    db.create_conversation(uid, conv, "Quokka widgets")
    db.add_message(uid, conv, "user", QUESTION_1)
    db.add_message(uid, conv, "assistant", REPEAT, {"mode": "assistant"})
    if listed:
        frozen(conv)
    turns = [{"role": "user", "content": QUESTION_1}, {"role": "assistant", "content": REPEAT}]
    with TestClient(app) as client:
        resp = _ask(client, conv, turns, "what does a left join do?")
    assert resp.status_code == 200, resp.text
    assert len(quiet) == 1
    if listed:
        assert "Quokka Ready" not in quiet[0] and S.TAINTED_PLACEHOLDER in quiet[0]
        assert S.STATUS_LINE in resp.text
    else:
        assert "Quokka Ready" in quiet[0], "an unlisted chat is unchanged"


# ---------------------------------------------------------------------------
# Item 4: folded-chunk vectors
# ---------------------------------------------------------------------------


@pytest.fixture()
def embed_calls(monkeypatch):
    calls: list = []

    async def embed(texts, **_kwargs):
        calls.append(list(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(llm, "embed_texts", embed)
    monkeypatch.setattr(settings, "semantic_recall_enabled", True)
    return calls


FOLDED = [
    {"role": "user", "content": "Quokka placements for the quarter?"},
    {"role": "assistant", "content": "Quokka: Asha Verma closed 14 placements."},
]


def _chunks(conv: str) -> int:
    return len(db.get_conversation_chunks(conv))


def test_a_chat_holding_a_salesforce_answer_gets_no_new_chunk_vector(world, embed_calls):
    uid, _ = world
    db.create_conversation(uid, "sf-chat", "Quokka placements")
    db.add_message(uid, "sf-chat", "user", FOLDED[0]["content"])
    db.add_message(uid, "sf-chat", "assistant", FOLDED[1]["content"], {"mode": "salesforce", "route": "sql"})
    assert asyncio.run(recall.index_folded("sf-chat", FOLDED, 0)) == 0
    assert embed_calls == [] and _chunks("sf-chat") == 0
    assert asyncio.run(recall.index_folded("plain-chat", FOLDED, 0)) == 2, "a plain chat is indexed as before"
    assert _chunks("plain-chat") == 2


def test_a_chat_holding_salesforce_only_by_a_server_row_gets_none_either(world, embed_calls):
    uid, _ = world
    db.create_conversation(uid, "req-chat", "Quokka request")
    db.create_chat_request("int-own-1", uid, "req-chat", "gen-own-1", {"message": "q", "mode": "salesforce"})
    assert asyncio.run(recall.index_folded("req-chat", FOLDED, 0)) == 0
    assert embed_calls == []


def test_a_frozen_chat_gets_no_new_chunk_vector(world, frozen, embed_calls):
    frozen(FROZEN)
    assert asyncio.run(recall.index_folded(FROZEN, FOLDED, 0)) == 0
    assert embed_calls == []


def test_with_isolation_off_salesforce_turns_are_folded_into_vectors_as_before(world, embed_calls, monkeypatch):
    uid, _ = world
    db.create_conversation(uid, "sf-chat", "Quokka placements")
    db.add_message(uid, "sf-chat", "assistant", FOLDED[1]["content"], {"mode": "salesforce"})
    monkeypatch.setattr(settings, "toggle_off_isolation", False)
    assert asyncio.run(recall.index_folded("sf-chat", FOLDED, 0)) == 2


def test_a_failed_check_stores_no_vector(world, embed_calls, monkeypatch):
    def boom(_conversation_id):
        raise RuntimeError("database away")

    monkeypatch.setattr(db, "conversation_owner", boom)
    assert asyncio.run(recall.index_folded("plain-chat", FOLDED, 0)) == 0
    assert embed_calls == [] and _chunks("plain-chat") == 0


def test_compaction_in_salesforce_mode_still_writes_the_summary_but_no_chunk(world, embed_calls, monkeypatch):
    """The paths the final report named: background compaction of a
    Salesforce-mode turn and POST /chat/compact both fold through `_fold`."""
    uid, _ = world
    db.create_conversation(uid, "sf-chat", "Quokka placements")
    db.add_message(uid, "sf-chat", "user", FOLDED[0]["content"])
    db.add_message(uid, "sf-chat", "assistant", FOLDED[1]["content"], {"mode": "salesforce"})

    async def fake_summarize(existing, turns):
        return "A summary of the folded turns."

    monkeypatch.setattr(summarize, "summarize", fake_summarize)
    history = [*FOLDED, {"role": "user", "content": "and next quarter?"}]
    result = asyncio.run(compaction.compact("sf-chat", history, force=True))
    assert result is not None and result["folded"] == 2
    assert db.get_summary("sf-chat")["summary"] == "A summary of the folded turns."
    assert embed_calls == [] and _chunks("sf-chat") == 0
