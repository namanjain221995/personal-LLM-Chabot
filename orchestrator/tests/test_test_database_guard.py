"""Regression tests for the destructive PostgreSQL fixture's positive guard."""
import importlib.util
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
