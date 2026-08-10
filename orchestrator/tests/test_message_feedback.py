"""Per-message thumbs, stored with the chat (2026-08-11).

This used to live in the browser's localStorage, keyed by a client-side
message id. That had a bug nobody had reported: a live message is keyed by a
browser uuid but a rehydrated one is keyed positionally
(`srv-<conversation>-<index>`), so a thumb given to a fresh reply vanished on
the next reload. The load-bearing claims now:

- a thumb round-trips through the database and comes back with the thread;
- it is OWNER-SCOPED, and another account's message is a 404, not a 403 —
  indistinguishable from one that does not exist, so ids cannot be probed;
- clearing is a real third state, not "write the string 'null'";
- the database rejects anything that is not up/down, so a broken client
  cannot poison the column;
- and it SURVIVES the offline sync's whole-thread rewrite, which deletes and
  reinserts every row and would otherwise silently drop it.
"""
import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_secret_file", str(tmp_path / ".session_secret"))
    monkeypatch.delenv("SESSION_SECRET", raising=False)


@pytest.fixture()
def alice(env, as_user):
    as_user("alice")
    with TestClient(app) as c:
        yield c


def _thread(client, conv_id: str, *contents: str) -> list:
    client.post("/history/conversations", json={"id": conv_id, "title": "t"})
    for i, text in enumerate(contents):
        client.post(
            f"/history/conversations/{conv_id}/messages",
            json={"role": "user" if i % 2 == 0 else "assistant", "content": text},
        )
    return client.get(f"/history/conversations/{conv_id}").json()["messages"]


# ---------------------------------------------------------------------------
# The server now gives the client a stable handle on a message
# ---------------------------------------------------------------------------


def test_messages_come_back_with_a_server_id_and_feedback_field(alice):
    """Without an id the browser has nothing to attach a thumb to — which is
    exactly why this was localStorage-only before."""
    messages = _thread(alice, "c1", "question", "answer")
    assert [m["role"] for m in messages] == ["user", "assistant"]
    for m in messages:
        assert isinstance(m["id"], int)
        assert m["feedback"] is None
    # Ids are the real row ids, so they are distinct and ordered.
    assert messages[0]["id"] < messages[1]["id"]


# ---------------------------------------------------------------------------
# Storing, reading back, and clearing
# ---------------------------------------------------------------------------


def test_a_thumb_is_stored_and_returned_with_the_thread(alice):
    messages = _thread(alice, "c1", "question", "answer")
    target = messages[1]["id"]

    resp = alice.put(
        f"/history/conversations/c1/messages/{target}/feedback",
        json={"feedback": "up"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == target and body["feedback"] == "up"
    assert body["feedback_at"], "a stored thumb records when it happened"

    reloaded = alice.get("/history/conversations/c1").json()["messages"]
    assert [m["feedback"] for m in reloaded] == [None, "up"]


def test_switching_and_clearing_a_thumb(alice):
    messages = _thread(alice, "c1", "question", "answer")
    target = messages[1]["id"]
    url = f"/history/conversations/c1/messages/{target}/feedback"

    alice.put(url, json={"feedback": "up"})
    assert alice.put(url, json={"feedback": "down"}).json()["feedback"] == "down"

    # Clearing is the UI's third state (clicking the same thumb again), and it
    # must store NULL rather than a "null" string or the literal 'none'.
    cleared = alice.put(url, json={"feedback": None})
    assert cleared.status_code == 200
    assert cleared.json()["feedback"] is None
    assert cleared.json()["feedback_at"] is None
    assert alice.get("/history/conversations/c1").json()["messages"][1]["feedback"] is None


def test_setting_the_same_thumb_twice_is_idempotent(alice):
    messages = _thread(alice, "c1", "q", "a")
    url = f"/history/conversations/c1/messages/{messages[1]['id']}/feedback"
    first = alice.put(url, json={"feedback": "up"}).json()
    second = alice.put(url, json={"feedback": "up"}).json()
    assert first["feedback"] == second["feedback"] == "up"
    assert len(alice.get("/history/conversations/c1").json()["messages"]) == 2


# ---------------------------------------------------------------------------
# Rejecting nonsense
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "UP", "thumbs-up", "1", "like", 1, True])
def test_an_invalid_thumb_is_rejected(alice, bad):
    messages = _thread(alice, "c1", "q", "a")
    resp = alice.put(
        f"/history/conversations/c1/messages/{messages[1]['id']}/feedback",
        json={"feedback": bad},
    )
    assert resp.status_code == 422, f"{bad!r} should not be accepted"


def test_extra_fields_are_rejected(alice):
    """extra='forbid', like the other bodies here: a typo'd field should be a
    422, not a silently ignored no-op."""
    messages = _thread(alice, "c1", "q", "a")
    resp = alice.put(
        f"/history/conversations/c1/messages/{messages[1]['id']}/feedback",
        json={"feedback": "up", "comment": "nice"},
    )
    assert resp.status_code == 422


def test_the_database_itself_refuses_a_bad_value():
    """Belt and braces: the CHECK constraint, not just pydantic. A different
    client — or a future code path — must not be able to poison the column."""
    uid = db.create_user("checker", "hash")
    db.create_conversation(uid, "c1", "t")
    msg = db.add_message(uid, "c1", "assistant", "a")
    with pytest.raises(ValueError):
        db.set_message_feedback(uid, "c1", msg["id"], "sideways")


# ---------------------------------------------------------------------------
# Ownership — the same rule as every other per-conversation store
# ---------------------------------------------------------------------------


def test_another_owners_message_cannot_be_rated(alice):
    """404, not 403: a forbidden id and a missing id must look identical, or
    the endpoint becomes a way to discover that a message exists."""
    other = db.create_user("someone-else", "!x")
    db.create_conversation(other, "theirs", "Confidential")
    theirs = db.add_message(other, "theirs", "assistant", "secret")

    resp = alice.put(
        f"/history/conversations/theirs/messages/{theirs['id']}/feedback",
        json={"feedback": "down"},
    )
    assert resp.status_code == 404
    # …and nothing was written.
    assert db.list_messages("theirs")[0]["feedback"] is None


def test_a_message_from_another_conversation_cannot_be_rated(alice):
    """The id is real and yours, but not in the conversation named in the URL.
    Scoping on both is what stops a mismatched pair writing to the wrong row."""
    _thread(alice, "c1", "q", "a")
    elsewhere = _thread(alice, "c2", "other q", "other a")
    stray = elsewhere[1]["id"]

    resp = alice.put(
        f"/history/conversations/c1/messages/{stray}/feedback",
        json={"feedback": "up"},
    )
    assert resp.status_code == 404
    assert alice.get("/history/conversations/c2").json()["messages"][1]["feedback"] is None


def test_an_unknown_message_id_is_404(alice):
    _thread(alice, "c1", "q", "a")
    resp = alice.put(
        "/history/conversations/c1/messages/999999/feedback", json={"feedback": "up"}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# The one that would actually have lost data
# ---------------------------------------------------------------------------


def test_feedback_survives_the_offline_syncs_whole_thread_rewrite(alice):
    """PUT /messages deletes and reinserts every row, so each one gets a NEW
    id. Without carrying the thumbs across, any client re-sync — which the
    offline path does whenever the tail diverges — would silently drop them."""
    messages = _thread(alice, "c1", "q1", "a1", "q2", "a2")
    alice.put(
        f"/history/conversations/c1/messages/{messages[1]['id']}/feedback",
        json={"feedback": "up"},
    )
    alice.put(
        f"/history/conversations/c1/messages/{messages[3]['id']}/feedback",
        json={"feedback": "down"},
    )

    # The client re-pushes the whole thread, one turn longer, carrying no
    # feedback of its own (the old clients do not know the field exists).
    resp = alice.put(
        "/history/conversations/c1/messages",
        json={
            "messages": [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
                {"role": "assistant", "content": "a2"},
                {"role": "user", "content": "q3"},
            ]
        },
    )
    assert resp.status_code == 200

    after = alice.get("/history/conversations/c1").json()["messages"]
    assert [m["content"] for m in after] == ["q1", "a1", "q2", "a2", "q3"]
    assert [m["feedback"] for m in after] == [None, "up", None, "down", None]
    # New rows, old thumbs.
    assert after[1]["id"] != messages[1]["id"]


def test_an_explicit_feedback_in_the_sync_payload_wins(alice):
    """A client that DOES know about the field is authoritative for it —
    otherwise a thumb cleared in one tab would be resurrected by the other."""
    messages = _thread(alice, "c1", "q", "a")
    alice.put(
        f"/history/conversations/c1/messages/{messages[1]['id']}/feedback",
        json={"feedback": "up"},
    )
    alice.put(
        "/history/conversations/c1/messages",
        json={
            "messages": [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a", "feedback": "down"},
            ]
        },
    )
    assert [m["feedback"] for m in alice.get("/history/conversations/c1").json()["messages"]] == [
        None,
        "down",
    ]


def test_deleting_the_conversation_takes_the_feedback_with_it(alice):
    """It lives ON the message, so the existing cascade covers it — no orphan
    rows in a side table to clean up later."""
    messages = _thread(alice, "c1", "q", "a")
    alice.put(
        f"/history/conversations/c1/messages/{messages[1]['id']}/feedback",
        json={"feedback": "up"},
    )
    assert alice.delete("/history/conversations/c1").status_code == 200
    with db.connection() as con:
        left = con.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE feedback IS NOT NULL"
        ).fetchone()
    assert left["n"] == 0


def test_truncating_a_thread_keeps_the_thumbs_on_the_surviving_turns(alice):
    """Regenerating an older answer drops a tail; the earlier ratings are
    about messages that still exist and must not be disturbed."""
    messages = _thread(alice, "c1", "q1", "a1", "q2", "a2")
    alice.put(
        f"/history/conversations/c1/messages/{messages[1]['id']}/feedback",
        json={"feedback": "up"},
    )
    resp = alice.post(
        "/history/conversations/c1/truncate", json={"keep": 2, "expected_total": 4}
    )
    assert resp.status_code == 200
    after = alice.get("/history/conversations/c1").json()["messages"]
    assert [m["feedback"] for m in after] == [None, "up"]
