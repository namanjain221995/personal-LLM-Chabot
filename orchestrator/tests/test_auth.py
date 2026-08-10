"""Single-user local mode — there is no login.

This file used to test registration, password verification, signed session
cookies and secret persistence. All of that is gone: the app runs as ONE local
account and every request resolves to it.

What is tested instead is the part that still protects data — conversations are
STILL scoped by user_id, and the resolver must land on a stable, correct
account. Getting that wrong would not throw; it would quietly start an empty
history beside the real one, or show one install's chats under another
identity.
"""
import os

import pytest
from fastapi.testclient import TestClient

from app import auth, db
from app.main import app


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.delenv("LOCAL_USERNAME", raising=False)
    auth._cached_user_id = None
    yield


# ---------------------------------------------------------------------------
# No login exists any more
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/auth/login", "/auth/register", "/auth/logout"])
def test_the_login_endpoints_are_gone(env, path):
    with TestClient(app) as c:
        assert c.post(path, json={"username": "x", "password": "y"}).status_code == 404


def test_history_is_reachable_without_any_credentials(env):
    """The whole point of the change: open the app, see your chats."""
    with TestClient(app) as c:
        assert c.get("/history/conversations").status_code == 200


def test_me_reports_the_local_user_and_never_401s(env):
    with TestClient(app) as c:
        resp = c.get("/auth/me")
        assert resp.status_code == 200
        assert resp.json()["local"] is True
        assert isinstance(resp.json()["username"], str)


def test_a_stale_session_cookie_is_ignored_not_rejected(env):
    """Browsers still hold the old ts_session cookie. It must not matter."""
    with TestClient(app) as c:
        c.cookies.set("ts_session", "garbage-from-the-old-login")
        assert c.get("/history/conversations").status_code == 200


# ---------------------------------------------------------------------------
# Which account the app runs as
# ---------------------------------------------------------------------------


def test_a_fresh_install_creates_one_account(env):
    with TestClient(app) as c:
        c.get("/history/conversations")
    with db.connection() as con:
        users = con.execute("SELECT username FROM users").fetchall()
    assert len(users) == 1


def test_an_existing_install_adopts_its_OLDEST_account(env):
    """THE important one: this machine already had accounts holding history.
    The resolver must land on the one that owns it, not mint a new empty user
    and make every existing conversation disappear."""
    first = db.create_user("original-owner", "!x")
    db.create_user("someone-later", "!x")
    db.create_conversation(first, "c-existing", "Existing chat")

    auth._cached_user_id = None
    with TestClient(app) as c:
        titles = [x["title"] for x in c.get("/history/conversations").json()]
    assert "Existing chat" in titles


def test_local_username_overrides_which_account_is_used(env, monkeypatch):
    older = db.create_user("older", "!x")
    db.create_user("chosen", "!x")
    db.create_conversation(older, "c1", "Older account chat")

    monkeypatch.setenv("LOCAL_USERNAME", "chosen")
    auth._cached_user_id = None
    with TestClient(app) as c:
        titles = [x["title"] for x in c.get("/history/conversations").json()]
    assert titles == [], "must show the CHOSEN account's history, not the oldest"


def test_local_username_naming_a_new_account_creates_it(env, monkeypatch):
    monkeypatch.setenv("LOCAL_USERNAME", "brand-new")
    auth._cached_user_id = None
    assert auth.local_user()["username"] == "brand-new"


def test_the_same_user_is_returned_every_time(env):
    """Resolution is cached; a second answer would split history in two."""
    first = auth.local_user()
    assert int(auth.local_user()["id"]) == int(first["id"])


def test_a_deleted_account_is_re_resolved_rather_than_crashing(env):
    user_id = int(auth.local_user()["id"])
    with db.connection() as con:
        con.execute("DELETE FROM users WHERE id = %s", (user_id,))
    assert auth.local_user() is not None


# ---------------------------------------------------------------------------
# Scoping still exists underneath
# ---------------------------------------------------------------------------


def test_conversations_are_still_stored_per_user(env):
    """Auth was removed; ownership was NOT. If this ever stops holding, a
    second account would start seeing the first one's chats."""
    a = db.create_user("owner-a", "!x")
    b = db.create_user("owner-b", "!x")
    db.create_conversation(a, "conv-a", "A's chat")
    db.create_conversation(b, "conv-b", "B's chat")

    assert [c["id"] for c in db.list_conversations(a)] == ["conv-a"]
    assert [c["id"] for c in db.list_conversations(b)] == ["conv-b"]
    assert db.get_conversation(b, "conv-a") is None


def test_no_usable_password_is_stored_for_the_local_account(env):
    """Nothing verifies passwords now — a real hash would imply it does."""
    assert not auth.local_user()["password_hash"].startswith("$argon2")


def test_the_session_secret_file_is_no_longer_created(env, tmp_path):
    """It only existed to sign session cookies. Nothing should recreate it."""
    from app.config import settings

    path = tmp_path / "never-created" / ".session_secret"
    settings.session_secret_file = str(path)
    with TestClient(app) as c:
        c.get("/history/conversations")
    assert not os.path.exists(path)
