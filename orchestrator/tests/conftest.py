"""Shared test setup.

The app now applies its database schema at STARTUP (so a broken migration
fails the deploy instead of the first user request that happens to touch
app.sqlite3). That means constructing a TestClient opens the database, and
the production default path — /data/app.sqlite3 — is not writable in the test
environment.

Rather than weakening the startup guarantee, every test gets its own
throwaway database directory. Tests that pin `app_db_path` themselves still
win: monkeypatch inside a test is applied after this fixture.
"""
import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def isolated_app_db(tmp_path, monkeypatch):
    # A dedicated SUBDIRECTORY: tests that point reports_dir at tmp_path would
    # otherwise list app.sqlite3 as a downloadable report.
    db_dir = tmp_path / "appdb"
    monkeypatch.setattr(settings, "app_db_path", str(db_dir / "app.sqlite3"))
    monkeypatch.setattr(
        settings, "session_secret_file", str(db_dir / ".session_secret")
    )
    yield


@pytest.fixture(autouse=True)
def reset_local_user():
    """`auth.local_user()` caches the resolved id for the process lifetime.

    Without this, the first test to resolve a user pins that id for the whole
    session and every later test silently reads and writes ANOTHER test's rows
    — the isolation tests would pass while proving nothing.
    """
    from app import auth

    auth._cached_user_id = None
    yield
    auth._cached_user_id = None


@pytest.fixture()
def as_user(monkeypatch):
    """Run the app as a named local user.

    Login is gone, but conversations are still scoped by user_id, so the
    isolation those tests describe still matters. `LOCAL_USERNAME` is how the
    single-user resolver is pointed at a specific account, which lets a test
    act as two different owners in turn.
    """
    from app import auth

    def _switch(username: str):
        monkeypatch.setenv("LOCAL_USERNAME", username)
        auth._cached_user_id = None
        # Materialise the row NOW. Resolution is lazy in production (first
        # request creates it), but a test that seeds through the db layer
        # before making any request would otherwise look up an account that
        # does not exist yet.
        return auth.local_user()

    return _switch
