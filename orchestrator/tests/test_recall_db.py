"""Cross-chat recall against a real temp SQLite DB (V9) — exercises the SQL."""
import pytest

from app import db
from app.config import settings


@pytest.fixture()
def temp_db():
    uid = db.create_user("alice", "hash")
    db.create_conversation(uid, "c1", "Zephyr launch planning")
    db.add_message(uid, "c1", "user", "Zephyr launch date is September 30, budget 250000.")
    db.add_message(uid, "c1", "assistant", "Noted: Zephyr Sept 30, budget 250000.")
    db.create_conversation(uid, "c2", "Unrelated cooking chat")
    db.add_message(uid, "c2", "user", "How do I bake sourdough bread?")
    # a second user must never leak in
    other = db.create_user("bob", "hash")
    db.create_conversation(other, "c9", "Bob secret")
    db.add_message(other, "c9", "user", "Zephyr is bob's private budget note 999999.")
    return uid


def test_recall_finds_relevant_other_conversation(temp_db):
    hits = db.recall_conversations(temp_db, ["zephyr", "budget"], "current", limit=3)
    assert len(hits) == 1
    assert hits[0]["title"] == "Zephyr launch planning"
    assert "250000" in hits[0]["snippet"]


def test_recall_excludes_current_conversation(temp_db):
    hits = db.recall_conversations(temp_db, ["zephyr"], "c1", limit=3)
    assert hits == []


def test_recall_is_user_scoped(temp_db):
    # alice's search must never surface bob's "Zephyr" message
    hits = db.recall_conversations(temp_db, ["zephyr"], "current", limit=5)
    titles = [h["title"] for h in hits]
    assert "Bob secret" not in titles


def test_recall_empty_keywords(temp_db):
    assert db.recall_conversations(temp_db, [], "current") == []


def test_recall_wildcards_are_literal(temp_db):
    # "%" must match literally (no rows contain it) — not act as a wildcard
    assert db.recall_conversations(temp_db, ["%"], "current") == []


# ---------------------------------------------------------------------------
# Toggle-off isolation (app/sf_isolation.py, design_v2 G4 and owner decision 3)
# ---------------------------------------------------------------------------


@pytest.fixture()
def mixed_world():
    """alice: a chat holding a Salesforce answer (with a toggle-off answer after
    it), and a plain chat; bob: a Salesforce chat of his own."""
    uid = db.create_user("iso-alice", "hash")
    db.create_conversation(uid, "sf-chat", "Zephyr placements review")
    db.add_message(uid, "sf-chat", "user", "Zephyr placements for the quarter?")
    db.add_message(uid, "sf-chat", "assistant", "Zephyr: Ravi Kumar closed 14 placements.", {"mode": "salesforce"})
    db.add_message(uid, "sf-chat", "user", "Zephyr chart please")
    db.add_message(uid, "sf-chat", "assistant", "Zephyr chart of Ravi Kumar's placements.", {"mode": "assistant"})
    db.create_conversation(uid, "plain-chat", "Zephyr launch planning")
    db.add_message(uid, "plain-chat", "user", "Zephyr launch date is September 30.")
    db.add_message(uid, "plain-chat", "assistant", "Noted: Zephyr launches Sept 30.", {"mode": "assistant"})
    other = db.create_user("iso-bob", "hash")
    db.create_conversation(other, "bob-chat", "Bob Zephyr")
    db.add_message(other, "bob-chat", "user", "Zephyr for bob")
    return uid


def test_keyword_recall_can_leave_out_every_salesforce_holding_chat(mixed_world):
    default = db.recall_conversations(mixed_world, ["zephyr"], "current", limit=5)
    assert {h["id"] for h in default} == {"sf-chat", "plain-chat"}, "Salesforce mode's recall keeps it"
    isolated = db.recall_conversations(mixed_world, ["zephyr"], "current", limit=5, exclude_salesforce_chats=True)
    assert [h["id"] for h in isolated] == ["plain-chat"]
    joined = " ".join(f"{h['title']} {h['snippet']}" for h in isolated)
    assert "placements" not in joined and "Ravi" not in joined, "title and questions go with the chat"
    assert all(h["id"] != "bob-chat" for h in isolated), "the owner scope still holds"


def _store_vectors(uid: int) -> None:
    from app.recall import pack_vector

    rows = db.messages_missing_embeddings(uid, settings.embed_model, limit=64, min_chars=1)
    db.store_message_embeddings(
        uid, settings.embed_model, 3,
        [{"message_id": r["id"], "conversation_id": r["conversation_id"], "embedding": pack_vector([1.0, 0.0, 0.0])} for r in rows],
    )


def test_cross_chat_vectors_of_a_salesforce_holding_chat_are_not_candidates(mixed_world, monkeypatch):
    # Vectors stored BEFORE the guard (isolation off at write time) stay in the
    # table; the read leaves them out.
    monkeypatch.setattr(settings, "toggle_off_isolation", False)
    _store_vectors(mixed_world)
    assert {r["conversation_id"] for r in db.fetch_message_embeddings(mixed_world, settings.embed_model, None)} == {
        "sf-chat", "plain-chat"
    }
    monkeypatch.setattr(settings, "toggle_off_isolation", True)
    rows = db.fetch_message_embeddings(mixed_world, settings.embed_model, None)
    assert {r["conversation_id"] for r in rows} == {"plain-chat"}
    with db.connection() as con:
        stored = con.execute("SELECT count(*) AS n FROM message_embeddings WHERE conversation_id = 'sf-chat'").fetchone()["n"]
    assert stored == 4, "nothing is deleted"


def test_no_new_vector_for_a_salesforce_answer_or_its_question(mixed_world):
    """Owner decision 3: neither the answer nor the user question of a
    Salesforce-mode turn is embedded from now on."""
    pending = {r["content"] for r in db.messages_missing_embeddings(mixed_world, settings.embed_model, limit=64, min_chars=1)}
    assert "Zephyr: Ravi Kumar closed 14 placements." not in pending
    assert "Zephyr placements for the quarter?" not in pending, "the row right before a Salesforce answer"
    # Review R15: no row of a Salesforce-holding chat gets a NEW vector. The
    # toggle-off read leaves such a chat out whole, so its toggle-off rows'
    # vectors would never be read; one cached set replaces three EXISTS per row.
    assert "Zephyr chart please" not in pending
    assert "Noted: Zephyr launches Sept 30." in pending


def test_a_salesforce_question_is_known_by_its_request_or_its_answer_branch(mixed_world):
    db.create_conversation(mixed_world, "q-chat", "Questions")
    db.add_message(mixed_world, "q-chat", "user", "A general question first.")
    db.add_message(mixed_world, "q-chat", "assistant", "A general answer first.", {"mode": "assistant"})
    db.create_chat_request("int-sf-q", mixed_world, "q-chat", "gen-sf-q", {"message": "q", "mode": "salesforce"})
    db.add_message(mixed_world, "q-chat", "user", "Zephyr headcount by team?", {"intent": {"id": "int-sf-q"}})
    db.add_message(mixed_world, "q-chat", "user", "Zephyr branch question?", {"branch": {"self": "b-7"}})
    db.add_message(mixed_world, "q-chat", "assistant", "A general reply in between.", {"mode": "assistant"})
    db.add_message(mixed_world, "q-chat", "assistant", "Zephyr branch answer.", {"mode": "salesforce", "branch": {"parent": "b-7"}})
    rows = {r["content"]: r for r in db.salesforce_chat_state(mixed_world, "q-chat")}
    assert rows["Zephyr headcount by team?"]["is_sf_question"], "by its request"
    assert rows["Zephyr branch question?"]["is_sf_question"], "by the answer under it"
    assert not rows["A general question first."]["is_sf_question"]
    pending = {r["content"] for r in db.messages_missing_embeddings(mixed_world, settings.embed_model, limit=64, min_chars=1)}
    assert not {"Zephyr headcount by team?", "Zephyr branch question?", "Zephyr branch answer."} & pending
    assert "Noted: Zephyr launches Sept 30." in pending, "a chat with no Salesforce turn is embedded as before"


def test_with_isolation_off_salesforce_mode_turns_are_embedded_as_before(mixed_world, monkeypatch):
    monkeypatch.setattr(settings, "toggle_off_isolation", False)
    pending = {r["content"] for r in db.messages_missing_embeddings(mixed_world, settings.embed_model, limit=64, min_chars=1)}
    assert "Zephyr: Ravi Kumar closed 14 placements." in pending


def test_a_new_salesforce_answer_invalidates_the_cross_chat_cache(mixed_world, monkeypatch):
    """C22: the candidate cache is fingerprinted on vectors only; a chat that has
    just become Salesforce-holding must not keep serving its rows."""
    import asyncio

    from app import main, memory_semantic

    _store_vectors(mixed_world)
    monkeypatch.setattr(memory_semantic, "CROSS_CHAT_EMBEDDINGS_CACHE_S", 300.0)
    monkeypatch.setattr(settings, "cross_chat_embeddings_cache_s", 300.0, raising=False)
    memory_semantic.invalidate_message_embeddings()
    first = memory_semantic._load_candidates(mixed_world, settings.embed_model, "current", 500)
    assert "plain-chat" in {r["conversation_id"] for r in first}
    gen = main.LiveGeneration("plain-chat", mixed_world)
    gen.answer = "Zephyr launch: 3 placements are due."
    gen.final_meta = {"route": "sql", "mode": "salesforce"}
    asyncio.run(main._store_answer(gen))
    second = memory_semantic._load_candidates(mixed_world, settings.embed_model, "current", 500)
    assert "plain-chat" not in {r["conversation_id"] for r in second}
    memory_semantic.invalidate_message_embeddings()
