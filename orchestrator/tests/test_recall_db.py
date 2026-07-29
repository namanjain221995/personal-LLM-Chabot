"""Cross-chat recall against a real temp SQLite DB (V9) — exercises the SQL."""
import pytest

from app import db
from app.config import settings


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_db_path", str(tmp_path / "app.sqlite3"))
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
