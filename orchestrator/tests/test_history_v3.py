"""V3-DESIGN §1: pin / archive on conversations — schema migration, the
?archived list filter, pinned-first ordering, and the extended PUT.

All offline apart from the test PostgreSQL (see conftest). The migration tests
matter most: the real database holds the owner's conversations, so applying the
schema must never touch a row that is already there.
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


def _columns(table: str = "conversations") -> set:
    with db.connection() as con:
        rows = con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table,),
        ).fetchall()
    return {r["column_name"] for r in rows}


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

def test_applying_the_schema_never_touches_existing_rows():
    """Simulate the live database: real user/conversation/message rows already
    present. Re-applying the schema (twice) must be a pure no-op — no dropped
    column, no rewritten row, no reset flag.

    Under SQLite this guarded an ALTER-only `migrate()`. The PostgreSQL runner
    replaces that with numbered migrations recorded in `schema_migrations`, and
    the property being protected is identical: an already-migrated database is
    left completely alone.
    """
    uid = db.create_user("owner", "argon2-hash")
    db.create_conversation(uid, "real-conv", "Q3 pipeline review")
    db.update_conversation(uid, "real-conv", pinned=True)
    db.add_message(uid, "real-conv", "user", "top accounts?")
    before = db.get_conversation(uid, "real-conv")
    before_messages = db.list_messages("real-conv")

    assert {"id", "user_id", "title", "created_at", "updated_at", "pinned",
            "archived", "seq", "title_source"} == _columns()
    assert db.schema_version() == db.LATEST_SCHEMA_VERSION

    for _ in range(2):
        db.init_schema()  # explicit re-run

    assert db.schema_version() == db.LATEST_SCHEMA_VERSION
    with db.connection() as con:
        applied = con.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()
    assert applied["n"] == db.LATEST_SCHEMA_VERSION, "a migration was recorded twice"

    assert db.get_conversation(uid, "real-conv") == before
    assert db.list_messages("real-conv") == before_messages
    assert before["title"] == "Q3 pipeline review"
    assert before["pinned"] is True and before["archived"] is False
    assert db.get_user_by_id(uid)["username"] == "owner"


def test_timestamps_keep_the_iso_8601_shape_the_frontend_parses():
    """`timestamptz` in, the same string out.

    The columns are real timestamps now, but every consumer — the sidebar's
    ordering, the search palette, the offline sync's toEpoch() — has always
    received `datetime.now(timezone.utc).isoformat()`. A `+00` offset or a
    space instead of the `T` would be a silent client-side break.
    """
    import re

    uid = db.create_user("stamp", "hash")
    created = db.create_conversation(uid, "ts-conv", "when")
    fetched = db.get_conversation(uid, "ts-conv")

    iso = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00$")
    for value in (created["created_at"], created["updated_at"],
                  fetched["created_at"], fetched["updated_at"]):
        assert iso.match(value), value
    # What create_conversation reported is what the database actually holds.
    assert fetched["created_at"] == created["created_at"]


def test_migration_is_idempotent_on_a_current_database(env):
    """Repeated migrations of an already-current DB change nothing."""
    with TestClient(app) as c:
        conv_id = _new(c, "keep me")
        assert c.put(f"/history/conversations/{conv_id}", json={"pinned": True}).status_code == 200
        before = c.get(f"/history/conversations/{conv_id}").json()

        for _ in range(3):
            db.init_schema()

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
