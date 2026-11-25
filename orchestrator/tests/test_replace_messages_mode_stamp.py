"""A client PUT cannot change how the toggle-off guard reads a stored row
(db.replace_messages, design_v2 G9/C19).

The replace is a DELETE + INSERT, so every row's meta is whatever the client
sends. By position (the function can only append, never reorder or shrink):
every row the full Salesforce predicate matched keeps or gains
`mode='salesforce'`, and every row that carried the `sf_isolation` stamp keeps
it. A client can only add `mode='salesforce'` or drop the stamp, and both only
hide more.
"""
from __future__ import annotations

import pytest

from app import db
from app.config import settings


@pytest.fixture()
def chat():
    uid = db.create_user("put-owner", "hash")
    db.create_conversation(uid, "put-chat", "Mixed")
    db.add_message(uid, "put-chat", "user", "q1")
    db.add_message(uid, "put-chat", "assistant", "a salesforce answer", {"mode": "salesforce", "generation_id": "g1"})
    db.add_message(uid, "put-chat", "user", "q2")
    db.add_message(uid, "put-chat", "assistant", "scope-only answer", {"salesforce_scope": {"owner": "me"}})
    db.add_message(uid, "put-chat", "user", "q3")
    db.add_message(uid, "put-chat", "assistant", "a guarded answer", {"mode": "assistant", "sf_isolation": 1})
    return uid


def _metas(conv: str) -> list:
    return [m["meta"] or {} for m in db.list_messages(conv)]


def _put(uid: int, metas: list) -> None:
    contents = ["q1", "a salesforce answer", "q2", "scope-only answer", "q3", "a guarded answer"]
    roles = ["user", "assistant"] * 3
    db.replace_messages(uid, "put-chat", [
        {"role": r, "content": c, "meta": m} for r, c, m in zip(roles, contents, metas)
    ])


def test_a_put_that_drops_or_changes_the_mode_keeps_salesforce(chat):
    _put(chat, [{}, {"mode": "assistant", "generation_id": "g1"}, {}, {}, {}, {"mode": "assistant"}])
    metas = _metas("put-chat")
    assert metas[1]["mode"] == "salesforce"
    assert metas[1]["generation_id"] == "g1", "the rest of the client's meta stands"


def test_a_row_marked_only_by_a_salesforce_key_gains_the_mode(chat):
    _put(chat, [{}, {}, {}, {}, {}, {}])
    metas = _metas("put-chat")
    assert metas[3]["mode"] == "salesforce"


def test_the_isolation_stamp_is_carried_by_position(chat):
    _put(chat, [{}, {}, {}, {}, {}, {"mode": "assistant"}])
    metas = _metas("put-chat")
    assert metas[5]["sf_isolation"] == 1
    assert "sf_isolation" not in metas[1] and "mode" not in metas[0]


def test_the_toggle_off_state_reads_the_carried_rows(chat):
    _put(chat, [{}, {}, {}, {}, {}, {"mode": "assistant"}])
    rows = {r["content"]: r for r in db.salesforce_chat_state(chat, "put-chat")}
    assert rows["a salesforce answer"]["is_sf"] and rows["scope-only answer"]["is_sf"]
    assert rows["a guarded answer"]["stamped"] and not rows["a guarded answer"]["is_sf"]


def test_with_isolation_off_only_the_engine_stamp_is_carried(chat, monkeypatch):
    monkeypatch.setattr(settings, "toggle_off_isolation", False)
    _put(chat, [{}, {}, {}, {}, {}, {}])
    metas = _metas("put-chat")
    assert "mode" not in metas[1] and "sf_isolation" not in metas[5]
