"""`db._ensure_pg_stat_statements` (2026-09-14, database-speed programme).

compose.yaml preloads pg_stat_statements; the orchestrator creates the
extension at start-up so production needs no manual step. Pinned here against
the real test server: the step is a no-op when the library is not preloaded
(the test server does not preload it), it never raises out of init_schema,
and it issues CREATE EXTENSION only when the server reports the library
preloaded and the extension absent.
"""
from __future__ import annotations

import logging

import psycopg
import pytest

from app import db


def _installed() -> bool:
    with db.connection() as con:
        row = con.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements') AS i"
        ).fetchone()
    return bool(row["i"])


def test_not_preloaded_is_a_no_op():
    with db.connection() as con:
        preload = con.execute("SELECT current_setting('shared_preload_libraries') AS s").fetchone()["s"]
    if "pg_stat_statements" in preload:
        pytest.skip("this test server preloads pg_stat_statements")
    before = _installed()
    db._ensure_pg_stat_statements()
    assert _installed() == before
    db.init_schema()  # the start-up path still completes
    assert _installed() == before


class _FakeCon:
    def __init__(self, row, fail_on_create=None):
        self.row = row
        self.statements: list[str] = []
        self.fail_on_create = fail_on_create

    def execute(self, sql, *a):
        self.statements.append(sql)
        if sql.startswith("CREATE EXTENSION") and self.fail_on_create:
            raise self.fail_on_create
        outer = self

        class _R:
            def fetchone(self_inner):
                return outer.row

        return _R()

    def transaction(self):
        import contextlib

        return contextlib.nullcontext()


def _patch_connection(monkeypatch, con):
    import contextlib

    @contextlib.contextmanager
    def fake():
        yield con

    monkeypatch.setattr(db, "connection", fake)


@pytest.mark.parametrize(
    "row",
    [
        {"installed": True, "available": True, "preloaded": True},
        {"installed": False, "available": False, "preloaded": True},
        {"installed": False, "available": True, "preloaded": False},
    ],
)
def test_skips_create_unless_preloaded_available_and_absent(monkeypatch, row):
    con = _FakeCon(row)
    _patch_connection(monkeypatch, con)
    db._ensure_pg_stat_statements()
    assert not any(s.startswith("CREATE EXTENSION") for s in con.statements)


def test_creates_when_preloaded_and_absent(monkeypatch):
    con = _FakeCon({"installed": False, "available": True, "preloaded": True})
    _patch_connection(monkeypatch, con)
    db._ensure_pg_stat_statements()
    assert "CREATE EXTENSION IF NOT EXISTS pg_stat_statements" in con.statements


def test_a_database_error_never_fails_start_up(monkeypatch, caplog):
    con = _FakeCon(
        {"installed": False, "available": True, "preloaded": True},
        fail_on_create=psycopg.errors.InsufficientPrivilege("permission denied to create extension"),
    )
    _patch_connection(monkeypatch, con)
    with caplog.at_level(logging.WARNING):
        db._ensure_pg_stat_statements()
    assert "could not create extension pg_stat_statements" in caplog.text
