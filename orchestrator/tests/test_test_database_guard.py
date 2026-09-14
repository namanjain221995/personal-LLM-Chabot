"""Regression tests for the destructive PostgreSQL fixture's positive guard."""
import importlib.util
import uuid
from pathlib import Path

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "orchestrator_test_suite_setup", Path(__file__).with_name("conftest.py")
)
assert _SPEC is not None and _SPEC.loader is not None
suite_setup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(suite_setup)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://user:secret@localhost/test",
        "postgresql://user:secret@localhost/techsara_test",
        "postgres://user:secret@localhost/test_isolated",
        "postgresql://user:secret@localhost/history-test?sslmode=disable",
    ],
)
def test_unmistakable_test_database_names_are_accepted(dsn):
    assert suite_setup._assert_safe_test_dsn(dsn) == dsn


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://user:secret@localhost/techsara",
        "postgresql://user:secret@localhost/contest",
        "postgresql://user:secret@localhost/testimony",
        "mysql://user:secret@localhost/techsara_test",
        "postgresql://user:secret@localhost/",
    ],
)
def test_ambiguous_or_non_postgres_database_names_are_rejected(dsn):
    with pytest.raises(pytest.UsageError, match="Refusing to run destructive fixtures"):
        suite_setup._assert_safe_test_dsn(dsn)


def test_explicit_unsafe_test_database_url_is_rejected_before_connection(monkeypatch):
    monkeypatch.setenv(
        "TEST_DATABASE_URL", "postgresql://user:secret@localhost/production"
    )
    with pytest.raises(pytest.UsageError):
        suite_setup._test_dsn()


def test_ensure_database_checks_name_before_any_driver_or_network_work():
    with pytest.raises(pytest.UsageError):
        suite_setup._ensure_database("postgresql://user:secret@localhost/production")


# ---------------------------------------------------------------------------
# The SERVER guard (2026-09-14). The name guard above let about twenty `*_test`
# databases onto the production instance: 22.1M of its 22.5M row writes in 14
# days, every AccessExclusiveLock, every WAL excursion to max_wal_size and up
# to 13 of its 60 connection slots. A `_test` suffix says nothing about WHERE
# the database is, so the suite now also refuses a server that holds any
# database that is not itself a test database.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "names, foreign",
    [
        (["postgres", "template0", "template1", "test"], []),
        (["postgres", "techsara_orchestrator_test", "fresh_install_test"], []),
        (["postgres", "test_rehearsal_fresh_20260914", "techsara_test_v34_fresh"], []),
        (["postgres", "crawl_durability_test", "ws_know_20260906_test"], []),
        # The production instance as it stands: the app database among tests.
        (["postgres", "techsara", "techsara_video_test", "techsara_e2e_test"], ["techsara"]),
        (["postgres", "contest", "testimony", "history-test"], ["contest", "testimony"]),
    ],
)
def test_a_server_is_a_test_server_only_if_every_database_on_it_is_a_test_database(names, foreign):
    assert suite_setup._foreign_databases(names) == foreign


def test_the_production_instance_is_refused_whatever_the_dsn_says(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_ALLOW_SHARED_SERVER", raising=False)
    monkeypatch.setattr(
        suite_setup,
        "_server_databases",
        lambda dsn: ["postgres", "techsara", "techsara_video_test"],
    )
    with pytest.raises(pytest.UsageError, match="not a dedicated test server") as caught:
        suite_setup._ensure_database("postgresql://u:p@127.0.0.1:5432/techsara_video_test")
    assert "techsara" in str(caught.value)


def test_the_shared_server_escape_hatch_names_databases_and_never_the_app_database(monkeypatch):
    monkeypatch.setattr(
        suite_setup, "_server_databases", lambda dsn: ["postgres", "devnotes", "x_test"]
    )
    monkeypatch.setenv("TEST_DATABASE_ALLOW_SHARED_SERVER", "devnotes")
    suite_setup._assert_test_server("postgresql://u:p@localhost:5999/x_test")  # allowed

    monkeypatch.setattr(
        suite_setup, "_server_databases", lambda dsn: ["postgres", "techsara", "x_test"]
    )
    monkeypatch.setenv("TEST_DATABASE_ALLOW_SHARED_SERVER", "techsara")
    with pytest.raises(pytest.UsageError, match="never be allowed"):
        suite_setup._assert_test_server("postgresql://u:p@localhost:5999/x_test")


@pytest.mark.parametrize(
    "env",
    [
        {"APP_DATABASE_URL": "postgresql://techsara:secret@postgres:5432/techsara"},
        {"POSTGRES_USER": "techsara", "POSTGRES_PASSWORD": "secret", "POSTGRES_DB": "techsara"},
    ],
)
def test_the_app_environment_no_longer_steers_the_suite_onto_the_app_server(monkeypatch, env):
    """`set -a; . ./.env` (or pytest inside the production container) used to
    turn into `<production server>/techsara_test`. Only TEST_DATABASE_URL, or
    the throwaway default, choose the server now."""
    for key in ("TEST_DATABASE_URL", "APP_DATABASE_URL", "POSTGRES_USER",
                "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_HOST", "POSTGRES_PORT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert suite_setup._test_dsn() == suite_setup._DEFAULT_TEST_DSN


def test_the_real_session_server_passes_and_a_foreign_database_on_it_is_refused(
    app_database, monkeypatch
):
    """Against the server this suite is running on: the listing is real SQL
    and the real server passes; the refusal is then proven on that same real
    listing with one foreign name added.

    Deliberately NOT a real `CREATE DATABASE` of a non-test name: while such a
    probe exists, every other session starting on the same shared test server
    (pg-test, ci311-pg, parallel CI shards) is refused by this very guard, and
    a run killed between the CREATE and the DROP (a CI timeout, Ctrl-C twice,
    the OOM killer) leaves it behind and bricks every later run on that server.
    """
    monkeypatch.delenv("TEST_DATABASE_ALLOW_SHARED_SERVER", raising=False)
    names = suite_setup._server_databases(app_database)
    assert app_database.rpartition("/")[2].split("?", 1)[0] in names
    suite_setup._assert_test_server(app_database)

    probe = f"zz_guard_probe_{uuid.uuid4().hex[:8]}"
    real = suite_setup._server_databases
    monkeypatch.setattr(suite_setup, "_server_databases", lambda dsn: [*real(dsn), probe])
    with pytest.raises(pytest.UsageError, match=probe):
        suite_setup._assert_test_server(app_database)
    monkeypatch.setenv("TEST_DATABASE_ALLOW_SHARED_SERVER", probe)
    suite_setup._assert_test_server(app_database)  # the escape hatch, on the real listing
