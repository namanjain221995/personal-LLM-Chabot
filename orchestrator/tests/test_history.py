"""Server-side history (V2-DESIGN §3c), all offline: CRUD, ordering, meta
round-trip, and strict per-user scoping — cross-user access is a 404."""
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_db_path", str(tmp_path / "app.sqlite3"))
    monkeypatch.setattr(settings, "session_secret_file", str(tmp_path / ".session_secret"))
    monkeypatch.delenv("SESSION_SECRET", raising=False)


@pytest.fixture()
def alice(env, as_user):
    # No registration: login is gone. The app runs AS this user.
    as_user("alice")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def bob(env, as_user):
    # No registration: login is gone. The app runs AS this user.
    as_user("bob")
    with TestClient(app) as c:
        yield c


def test_history_needs_no_credentials(env):
    """Login was removed — history is open to whoever can reach the port."""
    with TestClient(app) as anon:
        assert anon.get("/history/conversations").status_code == 200
        assert anon.post("/history/conversations", json={"title": "x"}).status_code == 200


def test_create_list_and_detail(alice):
    created = alice.post("/history/conversations", json={"title": "Q3 review"}).json()
    assert created["title"] == "Q3 review"
    conv_id = created["id"]

    listing = alice.get("/history/conversations").json()
    assert [c["id"] for c in listing] == [conv_id]
    # V3-DESIGN §1 added the pinned/archived flags to every listed row.
    assert set(listing[0]) == {"id", "title", "updated_at", "pinned", "archived"}
    assert listing[0]["pinned"] is False and listing[0]["archived"] is False

    detail = alice.get(f"/history/conversations/{conv_id}").json()
    assert detail["id"] == conv_id
    assert detail["messages"] == []


def test_client_supplied_id_and_conflict(alice):
    resp = alice.post("/history/conversations", json={"id": "conv-1", "title": "Mine"})
    assert resp.status_code == 200
    assert resp.json()["id"] == "conv-1"
    dup = alice.post("/history/conversations", json={"id": "conv-1", "title": "Again"})
    assert dup.status_code == 409
    bad = alice.post("/history/conversations", json={"id": "not ok!", "title": "x"})
    assert bad.status_code == 400


def test_messages_roundtrip_with_meta(alice):
    conv_id = alice.post("/history/conversations", json={"title": "t"}).json()["id"]
    m1 = alice.post(
        f"/history/conversations/{conv_id}/messages",
        json={"role": "user", "content": "top accounts?"},
    )
    assert m1.status_code == 200
    meta = {"route": "sql", "sql": "SELECT 1", "steps": [{"id": 1}]}
    m2 = alice.post(
        f"/history/conversations/{conv_id}/messages",
        json={"role": "assistant", "content": "Here you go.", "meta": meta},
    )
    assert m2.status_code == 200

    messages = alice.get(f"/history/conversations/{conv_id}").json()["messages"]
    assert messages == [
        {"role": "user", "content": "top accounts?", "meta": None},
        {"role": "assistant", "content": "Here you go.", "meta": meta},
    ]


def test_list_orders_by_most_recent_activity(alice):
    first = alice.post("/history/conversations", json={"title": "first"}).json()["id"]
    second = alice.post("/history/conversations", json={"title": "second"}).json()["id"]
    listing = alice.get("/history/conversations").json()
    assert [c["id"] for c in listing] == [second, first]

    # New message on the older conversation bumps it to the top.
    alice.post(
        f"/history/conversations/{first}/messages",
        json={"role": "user", "content": "ping"},
    )
    listing = alice.get("/history/conversations").json()
    assert [c["id"] for c in listing] == [first, second]


def test_rename_and_delete(alice):
    conv_id = alice.post("/history/conversations", json={"title": "old"}).json()["id"]
    renamed = alice.put(f"/history/conversations/{conv_id}", json={"title": "new name"})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "new name"

    empty_title = alice.put(f"/history/conversations/{conv_id}", json={"title": "   "})
    assert empty_title.status_code == 400

    assert alice.delete(f"/history/conversations/{conv_id}").status_code == 200
    assert alice.get(f"/history/conversations/{conv_id}").status_code == 404
    assert alice.delete(f"/history/conversations/{conv_id}").status_code == 404


def test_another_owners_conversation_reads_as_404(alice):
    """Login is gone, but rows are still owned. Seeded through the db layer,
    because the HTTP layer no longer has a second identity to act as — the
    scoping it enforces is what would matter if one ever appeared."""
    from app import db

    other = db.create_user("someone-else", "!x")
    db.create_conversation(other, "not-mine", "private")

    assert alice.get("/history/conversations/not-mine").status_code == 404
    assert alice.put(
        "/history/conversations/not-mine", json={"title": "hijack"}
    ).status_code == 404
    assert alice.post(
        "/history/conversations/not-mine/messages",
        json={"role": "user", "content": "sneak"},
    ).status_code == 404
    assert alice.delete("/history/conversations/not-mine").status_code == 404
    assert alice.get("/history/conversations").json() == []

    # The other owner's row is untouched.
    assert db.get_conversation(other, "not-mine")["title"] == "private"
