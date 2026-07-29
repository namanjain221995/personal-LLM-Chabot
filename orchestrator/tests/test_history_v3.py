"""V3-DESIGN §1: pin / archive on conversations — schema migration, the
?archived list filter, pinned-first ordering, and the extended PUT.

All offline. The migration tests matter most: the real database holds the
owner's conversations, so the migration must be additive and idempotent.
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app

# The conversations table exactly as V2 shipped it — no pinned, no archived.
V2_SCHEMA = """
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE conversations (
    id         TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    meta            TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_conversations_user ON conversations(user_id, updated_at DESC);
CREATE INDEX idx_messages_conversation ON messages(conversation_id, id);
"""


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


def _columns(path) -> set:
    con = sqlite3.connect(str(path))
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(conversations)")}
    finally:
        con.close()


def _new(client, title: str) -> str:
    resp = client.post("/history/conversations", json={"title": title})
    assert resp.status_code == 200
    return resp.json()["id"]


def _ids(client, **params) -> list:
    resp = client.get("/history/conversations", params=params)
    assert resp.status_code == 200
    return [c["id"] for c in resp.json()]


# ---------------------------------------------------------------------------
# Migration — against a V2-era database holding real rows
# ---------------------------------------------------------------------------

def test_migration_upgrades_an_old_database_without_touching_rows(tmp_path, monkeypatch):
    """Simulate the live V2 database: old schema, existing user/conversation/
    message rows. Migrating (twice) must add the columns, default them to 0,
    and leave every existing row exactly as it was."""
    path = tmp_path / "app.sqlite3"
    monkeypatch.setattr(settings, "app_db_path", str(path))

    seed = sqlite3.connect(str(path))
    seed.executescript(V2_SCHEMA)
    seed.execute(
        "INSERT INTO users (id, username, password_hash, created_at) VALUES (?,?,?,?)",
        (1, "owner", "argon2-hash", "2026-01-01T00:00:00+00:00"),
    )
    seed.execute(
        "INSERT INTO conversations (id, user_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("real-conv", 1, "Q3 pipeline review", "2026-01-01T00:00:00+00:00",
         "2026-01-02T00:00:00+00:00"),
    )
    seed.execute(
        "INSERT INTO messages (conversation_id, role, content, meta, created_at) "
        "VALUES (?,?,?,?,?)",
        ("real-conv", "user", "top accounts?", None, "2026-01-02T00:00:00+00:00"),
    )
    seed.commit()
    seed.close()

    assert _columns(path) == {"id", "user_id", "title", "created_at", "updated_at"}

    # Run the migration twice — the second run must be a pure no-op.
    for _ in range(2):
        con = db.connect()
        db.migrate(con)  # explicit re-run on top of connect()'s own migration
        con.close()

    assert _columns(path) == {
        "id", "user_id", "title", "created_at", "updated_at", "pinned", "archived",
    }

    # The owner's row survived, unchanged, with the new flags defaulted to 0/0.
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM conversations").fetchall()
    messages = con.execute("SELECT role, content FROM messages").fetchall()
    users = con.execute("SELECT username FROM users").fetchall()
    con.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "real-conv"
    assert row["title"] == "Q3 pipeline review"
    assert row["created_at"] == "2026-01-01T00:00:00+00:00"
    assert row["updated_at"] == "2026-01-02T00:00:00+00:00"
    assert row["pinned"] == 0 and row["archived"] == 0
    assert [tuple(m) for m in messages] == [("user", "top accounts?")]
    assert [u["username"] for u in users] == ["owner"]

    # And the migrated row is readable through the normal accessors.
    conversation = db.get_conversation(1, "real-conv")
    assert conversation["pinned"] is False and conversation["archived"] is False
    assert db.list_conversations(1) == [
        {
            "id": "real-conv",
            "title": "Q3 pipeline review",
            "updated_at": "2026-01-02T00:00:00+00:00",
            "pinned": False,
            "archived": False,
        }
    ]


def test_migration_is_idempotent_on_a_current_database(env):
    """Repeated migrations of an already-current DB change nothing."""
    with TestClient(app) as c:
        c.post("/auth/register", json={"username": "carol", "password": "long-enough-3"})
        conv_id = _new(c, "keep me")
        assert c.put(f"/history/conversations/{conv_id}", json={"pinned": True}).status_code == 200
        before = c.get(f"/history/conversations/{conv_id}").json()

        for _ in range(3):
            con = db.connect()
            db.migrate(con)
            con.close()

        assert c.get(f"/history/conversations/{conv_id}").json() == before
        assert before["pinned"] is True


def test_new_conversations_default_to_unpinned_and_unarchived(alice):
    created = alice.post("/history/conversations", json={"title": "fresh"}).json()
    assert created["pinned"] is False and created["archived"] is False
    detail = alice.get(f"/history/conversations/{created['id']}").json()
    assert detail["pinned"] is False and detail["archived"] is False


# ---------------------------------------------------------------------------
# PUT — pin / archive round-trips and field subsets
# ---------------------------------------------------------------------------

def test_pin_and_unpin_roundtrip(alice):
    conv_id = _new(alice, "pin me")
    pinned = alice.put(f"/history/conversations/{conv_id}", json={"pinned": True})
    assert pinned.status_code == 200
    assert pinned.json()["pinned"] is True
    assert alice.get(f"/history/conversations/{conv_id}").json()["pinned"] is True

    unpinned = alice.put(f"/history/conversations/{conv_id}", json={"pinned": False})
    assert unpinned.json()["pinned"] is False
    assert alice.get("/history/conversations").json()[0]["pinned"] is False


def test_archive_and_unarchive_roundtrip(alice):
    conv_id = _new(alice, "archive me")
    archived = alice.put(f"/history/conversations/{conv_id}", json={"archived": True})
    assert archived.status_code == 200
    assert archived.json()["archived"] is True
    assert _ids(alice) == []
    assert _ids(alice, archived="true") == [conv_id]

    restored = alice.put(f"/history/conversations/{conv_id}", json={"archived": False})
    assert restored.json()["archived"] is False
    assert _ids(alice) == [conv_id]
    assert _ids(alice, archived="true") == []


def test_put_subset_leaves_other_fields_untouched(alice):
    conv_id = _new(alice, "original")
    alice.put(f"/history/conversations/{conv_id}", json={"pinned": True, "archived": True})

    # Renaming keeps the flags.
    renamed = alice.put(f"/history/conversations/{conv_id}", json={"title": "renamed"}).json()
    assert renamed["title"] == "renamed"
    assert renamed["pinned"] is True and renamed["archived"] is True

    # Unarchiving keeps the title and the pin.
    unarchived = alice.put(f"/history/conversations/{conv_id}", json={"archived": False}).json()
    assert unarchived["title"] == "renamed"
    assert unarchived["pinned"] is True and unarchived["archived"] is False

    # An empty body is a no-op read.
    assert alice.put(f"/history/conversations/{conv_id}", json={}).json() == unarchived


def test_archiving_does_not_bump_updated_at(alice):
    older = _new(alice, "older")
    newer = _new(alice, "newer")
    before = alice.get(f"/history/conversations/{older}").json()["updated_at"]

    alice.put(f"/history/conversations/{older}", json={"archived": True})
    assert alice.get(f"/history/conversations/{older}").json()["updated_at"] == before
    alice.put(f"/history/conversations/{older}", json={"archived": False})
    assert alice.get(f"/history/conversations/{older}").json()["updated_at"] == before

    # Ordering is unchanged by the archive round-trip: `newer` is still first.
    assert _ids(alice) == [newer, older]


def test_pinning_does_not_bump_updated_at(alice):
    conv_id = _new(alice, "pin me")
    before = alice.get(f"/history/conversations/{conv_id}").json()["updated_at"]
    alice.put(f"/history/conversations/{conv_id}", json={"pinned": True})
    assert alice.get(f"/history/conversations/{conv_id}").json()["updated_at"] == before


def test_unknown_field_is_rejected(alice):
    conv_id = _new(alice, "strict")
    assert alice.put(
        f"/history/conversations/{conv_id}", json={"pinnned": True}
    ).status_code == 422
    assert alice.put(
        f"/history/conversations/{conv_id}", json={"title": "ok", "user_id": 2}
    ).status_code == 422
    assert alice.put(
        f"/history/conversations/{conv_id}", json={"pinned": "not-a-bool"}
    ).status_code == 422
    # The rejected writes changed nothing.
    current = alice.get(f"/history/conversations/{conv_id}").json()
    assert current["title"] == "strict" and current["pinned"] is False


def test_empty_title_still_rejected_alongside_flags(alice):
    conv_id = _new(alice, "keep")
    assert alice.put(
        f"/history/conversations/{conv_id}", json={"title": "   ", "pinned": True}
    ).status_code == 400
    current = alice.get(f"/history/conversations/{conv_id}").json()
    assert current["title"] == "keep" and current["pinned"] is False


# ---------------------------------------------------------------------------
# Listing — filter + ordering
# ---------------------------------------------------------------------------

def test_archived_chats_hidden_by_default_and_visible_with_flag(alice):
    kept = _new(alice, "kept")
    filed = _new(alice, "filed")
    alice.put(f"/history/conversations/{filed}", json={"archived": True})

    assert _ids(alice) == [kept]
    assert _ids(alice, archived="false") == [kept]
    assert _ids(alice, archived="true") == [filed]
    # The archived row still reads fine by id (the detail route is unfiltered).
    assert alice.get(f"/history/conversations/{filed}").status_code == 200


def test_pinned_first_ordering_with_updated_at_tiebreak(alice):
    first = _new(alice, "first")
    second = _new(alice, "second")
    third = _new(alice, "third")
    # Newest-first to start.
    assert _ids(alice) == [third, second, first]

    alice.put(f"/history/conversations/{first}", json={"pinned": True})
    assert _ids(alice) == [first, third, second]

    # Two pinned rows order among themselves by updated_at desc.
    alice.put(f"/history/conversations/{second}", json={"pinned": True})
    assert _ids(alice) == [second, first, third]

    # Activity on a pinned chat re-sorts it within the pinned group only.
    alice.post(
        f"/history/conversations/{first}/messages",
        json={"role": "user", "content": "ping"},
    )
    assert _ids(alice) == [first, second, third]

    # Activity on an unpinned chat cannot lift it above the pinned ones.
    alice.post(
        f"/history/conversations/{third}/messages",
        json={"role": "user", "content": "ping"},
    )
    assert _ids(alice) == [first, second, third]


def test_pinned_ordering_applies_within_the_archive_too(alice):
    a = _new(alice, "a")
    b = _new(alice, "b")
    for conv_id in (a, b):
        alice.put(f"/history/conversations/{conv_id}", json={"archived": True})
    alice.put(f"/history/conversations/{a}", json={"pinned": True})
    assert _ids(alice, archived="true") == [a, b]


def test_invalid_archived_query_value_is_rejected(alice):
    assert alice.get("/history/conversations", params={"archived": "maybe"}).status_code == 422


# ---------------------------------------------------------------------------
# V2 user scoping is unchanged for the new fields
# ---------------------------------------------------------------------------

def test_another_owners_flags_cannot_be_touched(alice):
    """Pin/archive are writes like any other — still owner-scoped."""
    from app import db

    other = db.create_user("someone-else", "!x")
    db.create_conversation(other, "not-mine", "private")

    assert alice.put(
        "/history/conversations/not-mine", json={"pinned": True}
    ).status_code == 404
    assert alice.put(
        "/history/conversations/not-mine", json={"archived": True}
    ).status_code == 404
    assert alice.get("/history/conversations/not-mine").status_code == 404
    assert alice.get("/history/conversations", params={"archived": "true"}).json() == []


def test_archived_listing_needs_no_credentials(env):
    """Login was removed — the archive is open like the rest of history."""
    with TestClient(app) as anon:
        assert anon.get(
            "/history/conversations", params={"archived": "true"}
        ).status_code == 200
