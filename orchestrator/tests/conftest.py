"""Shared test setup.

The app applies its database schema at STARTUP (so a broken migration fails
the deploy instead of the first user request that touches it), which means
constructing a TestClient opens the database. Since 2026-08-10 that database
is PostgreSQL, not a SQLite file, so "give every test its own throwaway file"
is no longer available.

WHAT REPLACES IT — a session-scoped test DATABASE, truncated before every
test. Measured here at well under a millisecond per test, and it reproduces
the property the old fixture actually provided: each test starts with empty
tables and identity columns back at 1, so nothing leaks between tests.

Rejected alternatives: a database per test (~80 ms of CREATE DATABASE each,
minutes across this suite), a schema per test (`search_path` does not reach
the pool's `configure`, so the app would have to know about it), and wrapping
each test in a rolled-back transaction (the app opens its OWN pooled
connections, which cannot see an outer test transaction — every history test
would read an empty database).

THE ONE REAL COST: the suite now needs a reachable PostgreSQL. It stays
offline in every other sense — no vLLM, no GPU, no torch, no network. Point it
somewhere with TEST_DATABASE_URL, or start a throwaway server:

    docker run -d --name pg-test -p 55432:5432 \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_USER=test -e POSTGRES_DB=test \\
        postgres:18-alpine
    export TEST_DATABASE_URL=postgresql://test:test@127.0.0.1:55432/test
"""
from __future__ import annotations

import os

import pytest

from app.config import settings

#: Every table the app owns, parent-last so CASCADE has nothing to complain
#: about. `schema_migrations` is deliberately absent — truncating it would make
#: the next init_schema() re-run every migration.
_APP_TABLES = (
    "repo_chunks",
    "repos",
    "documents",
    "url_documents",
    "uploads",
    "conversation_chunks",
    "conversation_summaries",
    "messages",
    "conversations",
    "users",
)

_DEFAULT_TEST_DSN = "postgresql://postgres:postgres@127.0.0.1:55432/techsara_test"


def _suffixed(dsn: str) -> str:
    """`…/techsara` -> `…/techsara_test`.

    Never the production database itself: the fixture below TRUNCATEs every
    table before each test, so pointing the suite at the real one would delete
    the owner's entire history on the first `pytest`.
    """
    base, _, name = dsn.rpartition("/")
    name = name.split("?", 1)[0]
    if base and name and not name.endswith("_test"):
        return f"{base}/{name}_test"
    return dsn


def _test_dsn() -> str:
    """Where the suite's database lives, in order of preference.

    1. TEST_DATABASE_URL — an explicit override always wins.
    2. APP_DATABASE_URL with `_test` appended.
    3. POSTGRES_USER/PASSWORD/DB from the environment, against the compose
       service on its loopback-published port. `set -a; . ./.env` is then
       enough to run the suite against the database that is already up — no
       second server to remember.
    4. A throwaway container on 55432 (see the module docstring).
    """
    explicit = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    if explicit:
        return explicit
    app_dsn = (os.environ.get("APP_DATABASE_URL") or "").strip()
    if app_dsn:
        return _suffixed(app_dsn)
    user = (os.environ.get("POSTGRES_USER") or "").strip()
    password = (os.environ.get("POSTGRES_PASSWORD") or "").strip()
    if user and password:
        host = (os.environ.get("POSTGRES_HOST") or "127.0.0.1").strip()
        port = (os.environ.get("POSTGRES_PORT") or "5432").strip()
        name = (os.environ.get("POSTGRES_DB") or user).strip()
        return f"postgresql://{user}:{password}@{host}:{port}/{name}_test"
    return _DEFAULT_TEST_DSN


def _ensure_database(dsn: str) -> None:
    """CREATE DATABASE if it is missing; a clear failure if the server is not
    there at all. A skip would be worse than an error: the whole history, auth
    and upload surface would silently stop being tested."""
    import psycopg

    try:
        with psycopg.connect(dsn, connect_timeout=5):
            return  # already exists and accepts connections
    except psycopg.OperationalError as exc:
        if "does not exist" not in str(exc):
            raise pytest.UsageError(
                f"the test suite needs a PostgreSQL server at {dsn!r} and could not "
                f"reach it:\n    {exc}\n"
                "Start one with:\n"
                "    docker run -d --name pg-test -p 55432:5432 "
                "-e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres "
                "-e POSTGRES_DB=postgres postgres:18-alpine\n"
                "or point the suite elsewhere with TEST_DATABASE_URL."
            ) from exc

    base, _, dbname = dsn.rpartition("/")
    dbname = dbname.split("?", 1)[0]
    with psycopg.connect(f"{base}/postgres", autocommit=True, connect_timeout=5) as admin:
        # LC_COLLATE/LC_CTYPE = C, matching POSTGRES_INITDB_ARGS in compose.
        # Collation is not cosmetic here: it decides what lower() folds and how
        # ILIKE behaves, which is exactly what the username-uniqueness index and
        # every search test depend on. A test database on en_US.utf8 would
        # validate different semantics than production runs.
        admin.execute(
            f'CREATE DATABASE "{dbname}" TEMPLATE template0 '
            "LC_COLLATE 'C' LC_CTYPE 'C' ENCODING 'UTF8'"
        )


@pytest.fixture(scope="session", autouse=True)
def app_database():
    """Create the test database and apply the schema once for the whole run."""
    from app import db

    dsn = _test_dsn()
    _ensure_database(dsn)
    settings.app_database_url = dsn
    db.close_pool()  # a pool from a previous DSN must not survive
    db.init_schema()
    with db.connection() as con:
        collation = con.execute(
            "SELECT datcollate FROM pg_database WHERE datname = current_database()"
        ).fetchone()["datcollate"]
    if collation != "C":
        raise pytest.UsageError(
            f"the test database uses LC_COLLATE={collation!r} but production runs "
            "'C' (POSTGRES_INITDB_ARGS in docker-compose.yml). lower() and ILIKE "
            "fold differently, so the search and username tests would validate "
            "the wrong semantics. Drop the test database and let this fixture "
            "recreate it."
        )
    yield dsn
    db.close_pool()


@pytest.fixture(autouse=True)
def isolated_app_db(app_database, tmp_path, monkeypatch):
    """Empty tables and identity counters reset, before every test.

    Before, not after: a test that leaves rows behind then still fails is much
    easier to debug when the rows are still there to look at.
    """
    from app import db

    monkeypatch.setattr(settings, "app_database_url", app_database)
    # Still a real file per test — the session secret is unrelated to the DB
    # move, and pointing it at tmp_path keeps it out of the reports listing.
    monkeypatch.setattr(
        settings, "session_secret_file", str(tmp_path / "appdb" / ".session_secret")
    )
    with db.connection() as con:
        con.execute(
            f"TRUNCATE TABLE {', '.join(_APP_TABLES)} RESTART IDENTITY CASCADE"
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
