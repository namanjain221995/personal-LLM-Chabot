"""Cross-user isolation (IDOR) with two REAL logged-in sessions.

Alice and Bob each hold a genuine session cookie; every test proves that
knowing another user's identifier (conversation id, fact id, filename,
generation key) yields a 404 — indistinguishable from the object never
existing — never the object itself.
"""
import pytest

from app import db, llm
from app.authn import store
from app.config import settings
from app.main import LiveGeneration, _live_generations, app


def _uid(username: str) -> int:
    return int(db.get_user_by_username(username)["id"])


@pytest.fixture()
def alice(login_client):
    return login_client("alice")


@pytest.fixture()
def bob(login_client):
    return login_client("bob")


@pytest.fixture()
def bobs_conversation(bob):
    """A conversation owned by Bob, with one message in it."""
    resp = bob.post(
        "/history/conversations", json={"id": "conv-bob", "title": "Bob's chat"}
    )
    assert resp.status_code == 200, resp.text
    resp = bob.post(
        "/history/conversations/conv-bob/messages",
        json={"role": "user", "content": "bob's private question"},
    )
    assert resp.status_code == 200, resp.text
    return "conv-bob"


class _DummyTask:
    """Stands in for the asyncio.Task of a running generation."""

    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


@pytest.fixture()
def bobs_generation(bob, bobs_conversation):
    gen = LiveGeneration(bobs_conversation, _uid("bob"))
    gen.task = _DummyTask()
    _live_generations[bobs_conversation] = gen
    yield gen
    _live_generations.pop(bobs_conversation, None)


# ---------------------------------------------------------------------------
# Conversations + messages
# ---------------------------------------------------------------------------


def test_conversation_read_update_delete_are_owner_scoped(
    alice, bob, bobs_conversation
):
    assert alice.get(f"/history/conversations/{bobs_conversation}").status_code == 404
    hijack = alice.put(
        f"/history/conversations/{bobs_conversation}", json={"title": "hijacked"}
    )
    assert hijack.status_code == 404
    assert alice.delete(f"/history/conversations/{bobs_conversation}").status_code == 404
    assert bobs_conversation not in [
        c["id"] for c in alice.get("/history/conversations").json()
    ]

    # Bob's copy is intact and only he can read the messages.
    mine = bob.get(f"/history/conversations/{bobs_conversation}")
    assert mine.status_code == 200
    assert mine.json()["title"] == "Bob's chat"
    assert [m["content"] for m in mine.json()["messages"]] == [
        "bob's private question"
    ]


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------


def test_uploads_are_owner_scoped(alice, bob, bobs_conversation):
    smuggle = alice.post(
        "/uploads",
        files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")},
        data={"conversation_id": bobs_conversation},
    )
    assert smuggle.status_code == 404

    assert alice.get(f"/uploads/{bobs_conversation}").status_code == 404
    listing = bob.get(f"/uploads/{bobs_conversation}")
    assert listing.status_code == 200
    assert listing.json() == {"uploads": []}  # Alice's smuggle never landed


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def test_reports_are_owner_scoped(alice, bob, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path))
    (tmp_path / "q3.pdf").write_bytes(b"%PDF-1.4 bob's numbers")
    store.bind_report("q3.pdf", _uid("bob"), None)

    assert alice.get("/reports/q3.pdf").status_code == 404
    assert alice.get("/reports").json()["reports"] == []

    mine = bob.get("/reports/q3.pdf")
    assert mine.status_code == 200
    assert mine.content == b"%PDF-1.4 bob's numbers"
    assert [r["filename"] for r in bob.get("/reports").json()["reports"]] == ["q3.pdf"]


# ---------------------------------------------------------------------------
# Memory facts
# ---------------------------------------------------------------------------


def test_memory_facts_are_owner_scoped(alice, bob):
    stored = bob.post(
        "/memory/facts", json={"facts": ["Bob prefers green dashboards"]}
    ).json()["stored"]
    fact_id = stored[0]["id"]

    assert alice.get("/memory/facts").json()["facts"] == []
    assert alice.delete(f"/memory/facts/{fact_id}").status_code == 404

    facts = bob.get("/memory/facts").json()["facts"]
    assert [f["fact"] for f in facts] == ["Bob prefers green dashboards"]
    assert bob.delete(f"/memory/facts/{fact_id}").json() == {"deleted": fact_id}


# ---------------------------------------------------------------------------
# Live generations: stop / attach / active
# ---------------------------------------------------------------------------


def test_stop_is_owner_scoped_and_never_cancels_foreign_work(
    alice, bob, bobs_generation
):
    theft = alice.post("/chat/stop", json={"conversation_id": "conv-bob"})
    assert theft.json() == {"stopped": False}  # indistinguishable from absent
    assert bobs_generation.task.cancelled is False

    own = bob.post("/chat/stop", json={"conversation_id": "conv-bob"})
    assert own.json() == {"stopped": True}
    assert bobs_generation.task.cancelled is True


def test_attach_and_active_are_owner_scoped(alice, bob, bobs_generation):
    assert alice.get("/chat/attach/conv-bob").status_code == 404
    assert alice.get("/chat/active").json() == {"active": []}
    # The owner's listing names it — nobody else's ever does.
    assert bob.get("/chat/active").json() == {"active": ["conv-bob"]}


# ---------------------------------------------------------------------------
# Salesforce clarification state
# ---------------------------------------------------------------------------


def test_salesforce_state_is_owner_scoped(alice, bob, bobs_conversation):
    assert alice.get(f"/chat/salesforce/{bobs_conversation}").status_code == 404
    cancel = alice.post(
        "/chat/salesforce/cancel", json={"conversation_id": bobs_conversation}
    )
    assert cancel.status_code == 404
    assert bob.get(f"/chat/salesforce/{bobs_conversation}").status_code == 200


# ---------------------------------------------------------------------------
# POST /chat: the conversation gate
# ---------------------------------------------------------------------------


def test_chat_into_a_foreign_conversation_is_404(alice, bobs_conversation):
    # The ownership gate runs before any model call, so no stub is needed.
    resp = alice.post(
        "/chat",
        json={
            "message": "what did bob ask?",
            "mode": "assistant",
            "conversation_id": bobs_conversation,
        },
    )
    assert resp.status_code == 404


def test_chat_with_a_fresh_conversation_id_claims_it(alice, bob, monkeypatch):
    async def fake_stream(messages, **kwargs):
        yield "token", "Hello!"

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    resp = alice.post(
        "/chat",
        json={
            "message": "hello there",
            "mode": "assistant",
            "conversation_id": "fresh-alice-1",
        },
    )
    assert resp.status_code == 200

    # The id now belongs to Alice: a row exists with her as owner, and Bob
    # can no longer create-and-inherit it.
    assert db.conversation_owner("fresh-alice-1") == _uid("alice")
    assert db.get_conversation(_uid("alice"), "fresh-alice-1") is not None
    late = bob.post(
        "/chat",
        json={
            "message": "mine now",
            "mode": "assistant",
            "conversation_id": "fresh-alice-1",
        },
    )
    assert late.status_code == 404


def test_anonymous_chat_is_401(anonymous_mode):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        resp = c.post("/chat", json={"message": "hi", "mode": "assistant"})
    assert resp.status_code == 401
