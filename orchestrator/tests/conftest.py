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

# Clarification is ON by default in production: every Salesforce question gets
# a one-click confirmation before it is answered. The routing tests assert that
# a question REACHES its engine, so they must not be intercepted by it. Set
# before `app.config` is imported — Settings() reads the environment once, at
# module import. The feature has its own coverage in tests/test_clarify.py,
# which exercises both modes explicitly.
os.environ.setdefault("CLARIFY_MODE", "ambiguous")

from urllib.parse import unquote, urlsplit  # noqa: E402

import pytest  # noqa: E402

from app.config import settings  # noqa: E402

#: Every table the app owns, parent-last so CASCADE has nothing to complain
#: about. `schema_migrations` is deliberately absent — truncating it would make
#: the next init_schema() re-run every migration.
_APP_TABLES = (
    # V11 Deep Research runs. The user_id FK cascades, but a run started by an
    # anonymous API call has user_id NULL and would survive every other
    # truncation — so it is listed explicitly.
    "research_runs",
    # V8 web-search memory: web_results cascades from web_searches, but the
    # explicit order keeps TRUNCATE happy either way; web_pages is global.
    "web_crawls",
    "web_results",
    "web_searches",
    # V14: claims and page versions hang off web_pages (cascade / set null),
    # listed first so the order is explicit.
    "web_claims",
    "web_page_versions",
    "web_pages",
    "sf_clarifications",
    "sf_intents",
    "sf_conversation_state",
    "repo_chunks",
    "repos",
    "documents",
    "url_documents",
    "uploads",
    # V20 sharing. Both cascade from `conversations`, but TRUNCATE CASCADE
    # only restarts identity on tables it was told about — and a share id
    # that keeps climbing across tests makes failures harder to read.
    "conversation_share_versions",
    "conversation_shares",
    "conversation_chunks",
    "conversation_summaries",
    "messages",
    "conversations",
    # V12 identity tables, children before parents. login_throttle has no FK
    # but holds cross-test state (lockouts) all the same.
    "report_files",
    "user_preferences",
    "audit_events",
    "workspace_invitations",
    "auth_sessions",
    "workspace_memberships",
    "login_throttle",
    "workspaces",
    "users",
)

_DEFAULT_TEST_DSN = "postgresql://postgres:postgres@127.0.0.1:55432/techsara_test"


def _assert_safe_test_dsn(dsn: str) -> str:
    """Refuse any database name that is not unmistakably test-only.

    This guard is deliberately positive: merely differing from the configured
    production DSN is not enough protection for the unconditional TRUNCATE
    fixture below.
    """
    parsed = urlsplit(dsn)
    database = unquote(parsed.path.lstrip("/")).strip().lower()
    marked_test = (
        database == "test"
        or database.startswith(("test_", "test-"))
        or database.endswith(("_test", "-test"))
    )
    if parsed.scheme not in {"postgres", "postgresql"} or not database or not marked_test:
        raise pytest.UsageError(
            "Refusing to run destructive fixtures: TEST_DATABASE_URL must use "
            "PostgreSQL and a database named `test`, prefixed `test_`/`test-`, "
            "or suffixed `_test`/`-test`."
        )
    return dsn


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
        return _assert_safe_test_dsn(explicit)
    app_dsn = (os.environ.get("APP_DATABASE_URL") or "").strip()
    if app_dsn:
        return _assert_safe_test_dsn(_suffixed(app_dsn))
    user = (os.environ.get("POSTGRES_USER") or "").strip()
    password = (os.environ.get("POSTGRES_PASSWORD") or "").strip()
    if user and password:
        host = (os.environ.get("POSTGRES_HOST") or "127.0.0.1").strip()
        port = (os.environ.get("POSTGRES_PORT") or "5432").strip()
        name = (os.environ.get("POSTGRES_DB") or user).strip()
        return _assert_safe_test_dsn(
            f"postgresql://{user}:{password}@{host}:{port}/{name}_test"
        )
    return _assert_safe_test_dsn(_DEFAULT_TEST_DSN)


def _ensure_database(dsn: str) -> None:
    """CREATE DATABASE if it is missing; a clear failure if the server is not
    there at all. A skip would be worse than an error: the whole history, auth
    and upload surface would silently stop being tested."""
    _assert_safe_test_dsn(dsn)
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
    # Defense in depth immediately adjacent to the destructive statement: even
    # a fixture override cannot smuggle a production DSN past session setup.
    _assert_safe_test_dsn(app_database)
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


def _materialize_test_user(username: str, role: str = "member") -> dict:
    """A real user row + workspace membership for a test identity."""
    from app import db
    from app.authn import store
    from app.authn.rbac import Role

    Role(role)
    row = db.get_user_by_username(username)
    if row is None:
        try:
            db.create_user(username, "!test-ambient")
        except db.IntegrityError:
            pass
        row = db.get_user_by_username(username)
    store.set_credentials(
        int(row["id"]), email=f"{username}@test.local", display_name=username
    )
    workspace = store.ensure_workspace(settings.workspace_name)
    store.upsert_membership(workspace["id"], int(row["id"]), role)
    return db.get_user_by_username(username)


def _principal_for(username: str, role: str = "member"):
    from app.authn.principal import Principal
    from app.authn.rbac import Role, capabilities

    row = _materialize_test_user(username, role)
    from app.authn import store

    workspace = store.default_workspace()
    return Principal(
        user_id=int(row["id"]),
        username=row["username"],
        email=row.get("email") or "",
        display_name=row.get("display_name") or row["username"],
        role=Role(role),
        workspace_id=workspace["id"],
        workspace_name=workspace["name"],
        session_id=f"ambient-{username}",
        caps=capabilities(Role(role)),
    )


@pytest.fixture(autouse=True)
def ambient_identity(isolated_app_db, monkeypatch):
    """The pre-login corpus's auth shim — AND the door to real auth.

    Login is back (2026-09-01), so a bare request now carries no identity and
    would 401 everywhere. The ~1,600 pre-auth tests assert routing, scoping
    and engine behaviour, not authentication; giving them an ambient signed-in
    "local" member preserves exactly what they were written to prove.

    THE DOOR: a request that carries a session cookie is resolved by the REAL
    session machinery — so the auth/RBAC/IDOR suites log in over HTTP and
    exercise genuine resolution end to end, in the same process, with the
    shim standing aside. `anonymous_mode` turns the shim off entirely.
    """
    from app import auth as auth_module
    from app.authn import principal as principal_module

    real_resolve = principal_module.resolve_principal_sync
    state: dict = {"principal": None, "ambient_enabled": True}

    def resolver(request):
        if request.cookies.get(settings.auth_cookie_name):
            return real_resolve(request)
        if not state["ambient_enabled"]:
            return None
        if state["principal"] is None:
            state["principal"] = _principal_for("local", "member")
        return state["principal"]

    monkeypatch.setattr(principal_module, "resolve_principal_sync", resolver)
    monkeypatch.setattr(auth_module, "resolve_principal_sync", resolver)
    yield state


@pytest.fixture()
def anonymous_mode(ambient_identity):
    """No ambient identity: a cookie-less request is genuinely anonymous."""
    ambient_identity["ambient_enabled"] = False
    ambient_identity["principal"] = None
    return ambient_identity


@pytest.fixture()
def as_user(ambient_identity):
    """Run the app as a named user (ambient — no cookie needed).

    The signature the pre-auth suite has always used: `as_user("alice")`
    materialises the account and points cookie-less resolution at it, which
    lets a test act as two different owners in turn. `role=` mints admins.
    """

    def _switch(username: str, role: str = "member"):
        ambient_identity["principal"] = _principal_for(username, role)
        from app import db

        return db.get_user_by_username(username)

    return _switch


@pytest.fixture()
def login_client():
    """A factory for REAL authenticated clients: creates the user with a
    password, logs in over HTTP, returns a TestClient carrying the session
    cookie. This path exercises the genuine session machinery."""
    from fastapi.testclient import TestClient

    from app import db
    from app.authn import passwords, store
    from app.authn.rbac import Role
    from app.main import app

    def _make(
        username: str,
        *,
        role: str = "member",
        password: str = "correct-horse-battery",
    ) -> TestClient:
        row = _materialize_test_user(username, role)
        store.set_credentials(
            int(row["id"]), password_hash=passwords.hash_password(password)
        )
        client = TestClient(app)
        response = client.post(
            "/auth/login",
            json={"email": f"{username}@test.local", "password": password},
        )
        assert response.status_code == 200, response.text
        return client

    return _make


@pytest.fixture(autouse=True)
def _knowledge_process_state_clear():
    """The knowledge layer keeps process-wide state — the public evidence
    cache, the query-embedding LRU, the reranker breaker — that tests which
    TRUNCATE tables between runs would otherwise see leak across tests."""
    from app import llm as _llm, rerank as _rerank, web_memory as _web_memory

    _web_memory.cache_clear()
    _llm.embed_cache_clear()
    _rerank.reset_for_tests()
    yield
    _web_memory.cache_clear()
    _llm.embed_cache_clear()
    _rerank.reset_for_tests()
