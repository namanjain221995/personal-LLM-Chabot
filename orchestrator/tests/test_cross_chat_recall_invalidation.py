"""The cross-chat recall cache never serves a renamed title or a replaced answer.

2026-09-13: memory_semantic caches a user's 500 recall candidates (message
text and conversation title) for 60 s and validates each hit against a
fingerprint of message_embeddings (count, max id, max created_at). That sees new
vectors and every delete, but a rename, a generated title, or the server's
in-place overwrite of a stored answer (main._overwrite_persisted_answer) leave
message_embeddings untouched — the prover showed another chat could recall the
old value for up to a minute. main.py now invalidates on exactly those writes.

Real rows, real routes, the real cache; only the embedding sidecar is absent
(the vectors are written directly).
"""
from __future__ import annotations

import array
import asyncio

import pytest

from app import db, main, memory_semantic
from app.config import settings

CURRENT = "conv-recall-current"
OTHER = "conv-recall-other"


@pytest.fixture(autouse=True)
def cache_on(monkeypatch):
    # The premise of every test here: the cache is on for a minute.
    monkeypatch.setattr(memory_semantic, "CROSS_CHAT_EMBEDDINGS_CACHE_S", 60.0)
    monkeypatch.setattr(settings, "cross_chat_embeddings_cache_s", 60.0, raising=False)
    memory_semantic.invalidate_message_embeddings()
    yield
    memory_semantic.invalidate_message_embeddings()


def _seed(user_id: int, conversation_id: str, title: str, content: str, generation_id: str = "") -> int:
    db.create_conversation(user_id, conversation_id, title)
    meta = {"generation_id": generation_id} if generation_id else None
    message = db.add_message(user_id, conversation_id, "assistant", content, meta)
    vector = array.array("f", [1.0, 0.0, 0.0]).tobytes()
    stored = db.store_message_embeddings(
        user_id,
        settings.embed_model,
        3,
        [{"message_id": message["id"], "conversation_id": conversation_id, "embedding": vector}],
    )
    assert stored == 1
    return int(message["id"])


def _candidates(user_id: int) -> list:
    return memory_semantic._load_candidates(user_id, settings.embed_model, CURRENT, 500)


def _fetch_counter(monkeypatch) -> list:
    calls: list = []
    real = db.fetch_message_embeddings

    def counted(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "fetch_message_embeddings", counted)
    return calls


def test_only_a_rename_a_delete_or_a_generated_title_is_treated_as_a_recall_cache_write():
    conversation = "/history/conversations/abc"
    assert main._is_recall_cache_write("PUT", conversation)
    assert main._is_recall_cache_write("DELETE", conversation)
    assert main._is_recall_cache_write("POST", conversation + "/title")
    assert not main._is_recall_cache_write("POST", conversation + "/messages")  # an append, every turn
    assert not main._is_recall_cache_write("PUT", conversation + "/messages")  # the fingerprint sees it
    assert not main._is_recall_cache_write("POST", conversation + "/truncate")  # the fingerprint sees it
    assert not main._is_recall_cache_write("GET", conversation)
    assert not main._is_recall_cache_write("POST", "/chat")


def test_the_cache_premise_an_in_place_edit_is_invisible_to_the_fingerprint(login_client, monkeypatch):
    # Guard: if this ever fails, the fingerprint learned to see edits and the
    # invalidation below may be simplified — not a regression.
    client = login_client("recall-premise")
    user = db.get_user_by_username("recall-premise")
    _seed(int(user["id"]), OTHER, "Falcon notes", "the falcon budget is 40k")
    calls = _fetch_counter(monkeypatch)
    assert _candidates(int(user["id"]))[0]["content"] == "the falcon budget is 40k"
    with db.connection() as con:
        con.execute("UPDATE messages SET content = 'edited' WHERE conversation_id = %s", (OTHER,))
    assert _candidates(int(user["id"]))[0]["content"] == "the falcon budget is 40k"
    assert len(calls) == 1, "the second read was served from the cache"
    client.close()


def test_renaming_a_conversation_reaches_cross_chat_recall_at_once(login_client):
    client = login_client("recall-rename")
    uid = int(db.get_user_by_username("recall-rename")["id"])
    _seed(uid, OTHER, "Falcon notes", "the falcon budget is 40k")
    assert _candidates(uid)[0]["title"] == "Falcon notes"

    resp = client.put(f"/history/conversations/{OTHER}", json={"title": "Budget 2027"})
    assert resp.status_code == 200, resp.text
    assert _candidates(uid)[0]["title"] == "Budget 2027"
    client.close()


def test_naming_a_conversation_from_its_first_exchange_reaches_cross_chat_recall_at_once(login_client, monkeypatch):
    from app import titling

    async def title(first_user, first_assistant):
        return "Falcon budget"

    monkeypatch.setattr(titling, "generate_title", title)
    client = login_client("recall-title")
    uid = int(db.get_user_by_username("recall-title")["id"])
    _seed(uid, OTHER, "New chat", "the falcon budget is 40k")
    assert _candidates(uid)[0]["title"] == "New chat"

    resp = client.post(f"/history/conversations/{OTHER}/title")
    assert resp.status_code == 200 and resp.json()["generated"] is True, resp.text
    assert _candidates(uid)[0]["title"] == "Falcon budget"
    client.close()


def test_a_deleted_conversation_is_never_recalled_from_the_cache(login_client):
    # Already covered by the fingerprint (the vectors cascade away); pinned so
    # the two mechanisms together keep covering it.
    client = login_client("recall-delete")
    uid = int(db.get_user_by_username("recall-delete")["id"])
    _seed(uid, OTHER, "Falcon notes", "the falcon budget is 40k")
    assert len(_candidates(uid)) == 1
    assert client.delete(f"/history/conversations/{OTHER}").status_code == 200
    assert _candidates(uid) == []
    client.close()


def test_the_servers_overwrite_of_a_stored_answer_reaches_cross_chat_recall_at_once(as_user):
    user = as_user("recall-overwrite")
    uid = int(user["id"])
    _seed(uid, OTHER, "Falcon notes", "the falcon budget is", generation_id="gen-overwrite")
    assert _candidates(uid)[0]["content"] == "the falcon budget is"

    gen = main.LiveGeneration(OTHER, uid)
    gen.generation_id = "gen-overwrite"
    gen.answer = "the falcon budget is 40k, approved in March"
    gen.final_meta = {"route": "chat"}
    asyncio.run(main._store_answer(gen))
    assert gen.persisted
    assert _candidates(uid)[0]["content"] == "the falcon budget is 40k, approved in March"


def test_a_rename_leaves_another_users_cached_candidates_alone(login_client, monkeypatch):
    alice = login_client("recall-alice")
    alice_id = int(db.get_user_by_username("recall-alice")["id"])
    login_client("recall-bob").close()
    bob_id = int(db.get_user_by_username("recall-bob")["id"])
    _seed(alice_id, OTHER, "Falcon notes", "alice's falcon budget")
    _seed(bob_id, "conv-recall-bob", "Heron notes", "bob's heron budget")
    _candidates(alice_id)
    _candidates(bob_id)
    calls = _fetch_counter(monkeypatch)

    assert alice.put(f"/history/conversations/{OTHER}", json={"title": "Renamed"}).status_code == 200
    assert _candidates(bob_id)[0]["title"] == "Heron notes"
    assert calls == [], "bob's entry was dropped by alice's rename"
    assert _candidates(alice_id)[0]["title"] == "Renamed"
    alice.close()


def test_a_failed_rename_or_an_append_does_not_empty_the_cache(login_client, monkeypatch):
    client = login_client("recall-noop")
    uid = int(db.get_user_by_username("recall-noop")["id"])
    _seed(uid, OTHER, "Falcon notes", "the falcon budget is 40k")
    _candidates(uid)
    calls = _fetch_counter(monkeypatch)
    assert client.put("/history/conversations/no-such-conversation", json={"title": "x"}).status_code == 404
    assert client.post(f"/history/conversations/{OTHER}/messages", json={"role": "user", "content": "hi"}).status_code == 200
    _candidates(uid)
    assert calls == [], "a 404 or a plain append invalidated the cache"
    client.close()
