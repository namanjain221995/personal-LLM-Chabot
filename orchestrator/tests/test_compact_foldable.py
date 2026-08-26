"""What "Compact now" is allowed to claim BEFORE it is pressed.

The button used to look equally actionable whether or not anything could be
folded. `GET /history/conversations/{id}/summary` and `POST /chat/compact` now
both report `foldable_turns` / `total_turns`, computed from the same inputs
`compaction.compact(force=True)` uses — so the count the UI advertises cannot
disagree with what the button actually does.

The load-bearing claims:
- a fresh conversation reports 0 foldable, and so does one with only the turn
  currently being answered;
- a long conversation reports exactly what a forced compaction would fold;
- an already-compacted prefix is subtracted, via the stored `covers_through`;
- the fields are ADDITIVE — every existing key on the summary response stays.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import compaction, db, history as history_mod, summarize
from app.config import settings
from app.main import app


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings, "session_secret_file", str(tmp_path / ".session_secret")
    )
    monkeypatch.delenv("SESSION_SECRET", raising=False)


@pytest.fixture()
def alice(env, as_user):
    as_user("alice")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def signed_in():
    """A conversation owned by a real user, so summaries can be stored."""
    uid = db.create_user("alice", "hash")
    db.create_conversation(uid, "conv-1", "chat")
    return uid


def turns(n: int) -> list:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(n)
    ]


def fake_summarizer():
    async def _summarize(existing, folded):
        merged = existing + " | " if existing else ""
        return merged + " ".join(t["content"] for t in folded)

    return _summarize


def _conversation(client, messages: int) -> str:
    conv_id = client.post("/history/conversations", json={"title": "t"}).json()["id"]
    for i in range(messages):
        client.post(
            f"/history/conversations/{conv_id}/messages",
            json={
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"m{i}",
            },
        )
    return conv_id


# ---------------------------------------------------------------------------
# The summary endpoint's two additive fields
# ---------------------------------------------------------------------------


def test_fresh_conversation_has_nothing_foldable(alice):
    conv_id = _conversation(alice, 0)
    body = alice.get(f"/history/conversations/{conv_id}/summary").json()
    assert body["foldable_turns"] == 0
    assert body["total_turns"] == 0


def test_the_turn_being_answered_is_never_foldable(alice):
    # One message is the turn being answered; folding it would fold the
    # question the model is about to reply to.
    conv_id = _conversation(alice, 1)
    body = alice.get(f"/history/conversations/{conv_id}/summary").json()
    assert body["total_turns"] == 1
    assert body["foldable_turns"] == 0


def test_long_conversation_reports_what_would_be_folded(alice):
    conv_id = _conversation(alice, 28)
    body = alice.get(f"/history/conversations/{conv_id}/summary").json()
    assert body["total_turns"] == 28
    assert body["foldable_turns"] == 27


def test_an_already_folded_prefix_is_subtracted(alice):
    conv_id = _conversation(alice, 28)
    db.save_summary(conv_id, "older stuff", 10, 4)
    body = alice.get(f"/history/conversations/{conv_id}/summary").json()
    assert body["covers_through"] == 10
    assert body["foldable_turns"] == 17, "27 foldable minus the 10 already folded"
    assert body["total_turns"] == 28


def test_a_fully_folded_conversation_reports_zero(alice):
    conv_id = _conversation(alice, 28)
    db.save_summary(conv_id, "everything older", 27, 4)
    body = alice.get(f"/history/conversations/{conv_id}/summary").json()
    assert body["foldable_turns"] == 0


def test_the_new_fields_are_purely_additive(alice):
    conv_id = _conversation(alice, 4)
    empty = alice.get(f"/history/conversations/{conv_id}/summary").json()
    assert empty["summary"] is None and empty["covers_through"] == 0

    db.save_summary(conv_id, "older stuff", 2, 4)
    body = alice.get(f"/history/conversations/{conv_id}/summary").json()
    # Everything the endpoint served before is still served, unrenamed.
    assert body["summary"] == "older stuff"
    assert body["covers_through"] == 2
    assert "updated_at" in body
    assert {"foldable_turns", "total_turns"} <= set(body)


def test_someone_elses_conversation_is_still_a_404(alice, as_user, env):
    conv_id = _conversation(alice, 4)
    as_user("bob")
    with TestClient(app) as bob:
        assert bob.get(f"/history/conversations/{conv_id}/summary").status_code == 404


# ---------------------------------------------------------------------------
# The invariant: the advertised count IS what the button folds
# ---------------------------------------------------------------------------


def test_reported_count_equals_what_a_forced_compaction_folds(
    signed_in, monkeypatch
):
    monkeypatch.setattr(summarize, "summarize", fake_summarizer())
    history = turns(28)

    before = history_mod.foldable_counts("conv-1", history)
    assert before == {"foldable_turns": 27, "total_turns": 28}

    result = asyncio.run(compaction.compact("conv-1", history, force=True))
    assert result is not None
    # The promise the button made, and what it actually did.
    assert result["folded"] == before["foldable_turns"]

    after = history_mod.foldable_counts("conv-1", history)
    assert after["foldable_turns"] == 0
    # ...and pressing it again really would do nothing.
    assert asyncio.run(compaction.compact("conv-1", history, force=True)) is None


def test_counts_degrade_to_zero_rather_than_raising(signed_in):
    # An unknown conversation, and rows the shape check cannot survive: this
    # decorates a read-only endpoint and must never be able to fail it.
    assert history_mod.foldable_counts("no-such-conversation") == {
        "foldable_turns": 0,
        "total_turns": 0,
    }
    assert history_mod.foldable_counts("conv-1", [{"nope": 1}]) == {
        "foldable_turns": 0,
        "total_turns": 0,
    }


def test_system_blocks_are_not_countable_turns(signed_in):
    history = [{"role": "system", "content": "pinned"}] + turns(10)
    counts = history_mod.foldable_counts("conv-1", history)
    assert counts["total_turns"] == 10, "pinned system blocks are never folded"
    assert counts["foldable_turns"] == 9


def test_a_blank_turn_is_never_advertised_as_foldable(alice, monkeypatch):
    """A stored blank message must not inflate the promise.

    `POST /chat/compact` drops blank turns from the client's message list, and
    the automatic path builds its history from `ChatRequest.history_messages`,
    which applies the same rule — so `covers_through` counts blank-filtered
    turns. Counting a blank row here advertised a turn the button then refused
    to fold: with 10 real turns plus one blank row the meter promised "Folds 10
    earlier messages" and the button folded 9.
    """
    monkeypatch.setattr(summarize, "summarize", fake_summarizer())
    conv_id = _conversation(alice, 10)
    alice.post(
        f"/history/conversations/{conv_id}/messages",
        json={"role": "assistant", "content": ""},
    )
    assert len(db.list_messages(conv_id)) == 11, "the blank row really is stored"

    advertised = alice.get(f"/history/conversations/{conv_id}/summary").json()
    assert advertised["total_turns"] == 10, "the blank row is not a turn"
    assert advertised["foldable_turns"] == 9

    # What the button actually does, over the wire, the way the frontend calls
    # it (it filters blank content out of the body).
    sent = [
        {"role": m["role"], "content": m["content"]}
        for m in db.list_messages(conv_id)
        if m["content"] and m["content"].strip()
    ]
    posted = alice.post(
        "/chat/compact", json={"conversation_id": conv_id, "messages": sent}
    ).json()
    assert posted["folded_turns"] == advertised["foldable_turns"]
    assert posted["foldable_turns"] == 0


def test_the_db_fallback_branch_folds_the_same_turns_as_the_client_branch(
    alice, monkeypatch
):
    """`POST /chat/compact` with no `messages` body must not disagree with itself."""
    monkeypatch.setattr(summarize, "summarize", fake_summarizer())
    conv_id = _conversation(alice, 10)
    alice.post(
        f"/history/conversations/{conv_id}/messages",
        json={"role": "assistant", "content": "   "},
    )
    advertised = alice.get(f"/history/conversations/{conv_id}/summary").json()
    posted = alice.post("/chat/compact", json={"conversation_id": conv_id}).json()
    assert posted["folded_turns"] == advertised["foldable_turns"] == 9


def test_the_advertised_count_matches_the_button_over_real_http(alice, monkeypatch):
    """The invariant across the TWO endpoints the frontend actually calls.

    The count is read from `GET /history/conversations/{id}/summary`, which
    resolves its own history from the database; the fold happens in
    `POST /chat/compact`, which prefers the message list the client sends. The
    unit-level check passes one list to both, so it cannot see a disagreement
    between those two resolutions. This drives them the way the UI does.
    """
    monkeypatch.setattr(summarize, "summarize", fake_summarizer())
    conv_id = _conversation(alice, 28)

    advertised = alice.get(f"/history/conversations/{conv_id}/summary").json()
    sent = [
        {"role": m["role"], "content": m["content"]}
        for m in db.list_messages(conv_id)
    ]
    posted = alice.post(
        "/chat/compact", json={"conversation_id": conv_id, "messages": sent}
    ).json()

    assert posted["compacted"] is True
    assert posted["folded_turns"] == advertised["foldable_turns"] == 27
    # The response carries the state AFTER the fold, so the popover needs no
    # second round trip, and pressing again really is a no-op.
    assert posted["foldable_turns"] == 0
    again = alice.post(
        "/chat/compact", json={"conversation_id": conv_id, "messages": sent}
    ).json()
    assert again["compacted"] is False
    assert again["reason"] == "nothing older to summarize"
    assert (
        alice.get(f"/history/conversations/{conv_id}/summary").json()["foldable_turns"]
        == 0
    )
