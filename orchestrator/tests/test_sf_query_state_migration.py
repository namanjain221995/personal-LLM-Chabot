"""Migration V45, `sf_query_state`, and the rest of lane P0's scaffold for the
Salesforce conversation memory (design 2026-11-25 §2.2, §3.3, §15.1, §16.2).

What these pin:
  * V45 is the last migration, applies on a fresh install and as an upgrade
    from V44 to the same schema, and is additive only (one new table, three
    indexes on it, nothing else touched);
  * the store runs the migration's own text: one copy in the tree, and db.py
    does not import the store;
  * the table's columns, defaults, foreign keys (both cascade, both lead an
    index), the UNIQUE constraint the store's ON CONFLICT names, and both
    CHECKs; rows go with their chat and with their user;
  * the jsonb writer removes NUL from the object, not from the text, so a
    typed backslash followed by "u0000" is stored exactly (stage-1 open
    point 16);
  * the platform settings of §15.1 and §12.2: exact names, safe defaults,
    strict parsing (a typo fails start-up rather than choosing);
  * `Outcome.sf_memory` defaults to None, and `sf_isolation.answer_key` is
    the guard's own hash.

All names, texts and ids here are made up.
"""
from __future__ import annotations

import ast
import dataclasses
import decimal
import hashlib
import json
import logging
import pathlib
import re
import threading
import time
import types

import psycopg
import pytest

from app import db, retention, sf_isolation, sf_memory_store, sf_summary
from app.config import Settings, settings
from app.engines.sf_intel import Outcome
from app.sse import strict_json

_APP = pathlib.Path(__file__).resolve().parents[1] / "app"
_REPO = pathlib.Path(__file__).resolve().parents[2]

_COLUMNS = {
    # name: (data_type, is_nullable, column_default or None)
    "id": ("bigint", "NO", None),
    "conversation_id": ("text", "NO", None),
    "user_id": ("integer", "NO", None),
    "workspace_id": ("text", "NO", "''::text"),
    "access_profile": ("text", "YES", None),
    "generation_id": ("text", "NO", None),
    "answer_sha1": ("text", "NO", None),
    "parent_sha1": ("text", "NO", "''::text"),
    "question_sha1": ("text", "NO", "''::text"),
    "contract": ("smallint", "NO", None),
    "state": ("jsonb", "YES", None),
    "plan_line": ("text", "NO", "''::text"),
    "summary": ("text", "NO", "''::text"),
    "summary_pairs": ("integer", "NO", "0"),
    "path_pairs": ("integer", "NO", "0"),
    "created_at": ("timestamp with time zone", "NO", "now()"),
    "summary_at": ("timestamp with time zone", "YES", None),
    "expires_at": ("timestamp with time zone", "YES", None),
}


def _h(text: str) -> str:
    return hashlib.sha1(" ".join(text.split()).encode("utf-8")).hexdigest()


def _user(name: str) -> int:
    row = db.get_user_by_username(name)
    return int(row["id"]) if row else db.create_user(name, "!p0-test")


def _chat(user_id: int, conversation_id: str) -> str:
    db.create_conversation(user_id, conversation_id, "made-up chat")
    return conversation_id


def _row(conversation_id, user_id, generation_id, **columns):
    """One row by plain SQL (the store has its own tests): only what the
    migration itself decides is exercised here."""
    values = {
        "conversation_id": conversation_id,
        "user_id": user_id,
        "generation_id": generation_id,
        "answer_sha1": _h(f"answer {generation_id}"),
        "contract": 2,
        **columns,
    }
    names = ", ".join(values)
    marks = ", ".join(["%s"] * len(values))
    with db.connection() as con:
        return con.execute(
            f"INSERT INTO sf_query_state ({names}) VALUES ({marks}) RETURNING *",
            tuple(values.values()),
        ).fetchone()


def _count(**where) -> int:
    clause = " AND ".join(f"{k} = %s" for k in where) or "true"
    with db.connection() as con:
        return con.execute(
            f"SELECT count(*) AS n FROM sf_query_state WHERE {clause}", tuple(where.values())
        ).fetchone()["n"]


# ------------------------------------------------------- the migration list --


def test_v45_is_the_last_migration_and_the_test_database_ran_it():
    versions = [version for version, _ddl in db._MIGRATIONS]
    assert versions == list(range(1, 46))
    assert dict(db._MIGRATIONS)[45] is db._MIGRATION_V45
    assert db.LATEST_SCHEMA_VERSION == 45
    assert db.schema_version() == 45
    db.init_schema()  # an applied migration is skipped
    assert db.schema_version() == 45
    with db.connection() as con:
        assert con.execute("SELECT to_regclass('public.sf_query_state') AS t").fetchone()["t"]


def test_the_store_runs_the_migrations_own_text():
    """One copy of the DDL in the tree (stage-1 open point 11)."""
    assert sf_memory_store.DDL is db._MIGRATION_V45
    assert "CONSTRAINT sf_query_state_generation UNIQUE (conversation_id, generation_id)" in db._MIGRATION_V45
    assert sf_memory_store.TABLE == "sf_query_state"


def test_db_does_not_import_the_store():
    """An import cycle otherwise, and design §3.6 limits who imports it."""
    tree = ast.parse((_APP / "db.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any("sf_memory_store" in name for name in imported)


def _statements(ddl: str):
    without_comments = re.sub(r"--[^\n]*", "", ddl)
    return [" ".join(s.split()) for s in without_comments.split(";") if s.strip()]


def test_v45_is_additive_only():
    """One new table and indexes on that table only: nothing an existing
    table or row could lose, and no index built on an existing table."""
    statements = _statements(db._MIGRATION_V45)
    assert statements[0].startswith("CREATE TABLE IF NOT EXISTS sf_query_state (")
    assert len(statements) == 4
    for statement in statements[1:]:
        assert re.fullmatch(r"CREATE INDEX IF NOT EXISTS idx_sf_query_state_\w+ ON sf_query_state \(.*", statement)
    # A foreign key's own "ON DELETE CASCADE" is not a statement.
    body = " ".join(statements).upper().replace("ON DELETE CASCADE", "")
    for word in ("ALTER ", "DROP ", "DELETE ", "UPDATE ", "TRUNCATE ", "INSERT ", "RENAME "):
        assert word not in body


def test_the_table_cascades_so_it_is_not_a_side_table():
    assert "sf_query_state" not in db._SIDE_TABLES


# ------------------------------------------------------------- the shape --


def test_columns_types_and_defaults():
    with db.connection() as con:
        rows = con.execute(
            "SELECT column_name, data_type, is_nullable, column_default, is_identity "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'sf_query_state'"
        ).fetchall()
    got = {r["column_name"]: (r["data_type"], r["is_nullable"], r["column_default"]) for r in rows}
    assert got == _COLUMNS
    assert {r["column_name"] for r in rows if r["is_identity"] == "YES"} == {"id"}


def test_foreign_keys_cascade_and_each_leads_an_index():
    with db.connection() as con:
        fks = con.execute(
            "SELECT a.attname AS col, con.confrelid::regclass::text AS ref, con.confdeltype AS del, "
            "       EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = con.conrelid "
            "               AND i.indkey[0] = con.conkey[1]) AS indexed "
            "  FROM pg_constraint con "
            "  JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1] "
            " WHERE con.contype = 'f' AND con.conrelid = 'sf_query_state'::regclass"
        ).fetchall()
    assert {(r["col"], r["ref"], r["del"]) for r in fks} == {
        ("conversation_id", "conversations", "c"),
        ("user_id", "users", "c"),
    }
    assert all(r["indexed"] for r in fks)


def test_constraints_and_indexes_are_the_designs():
    with db.connection() as con:
        constraints = {
            r["conname"]: r["def"]
            for r in con.execute(
                "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint "
                "WHERE conrelid = 'sf_query_state'::regclass AND contype IN ('u', 'c')"
            ).fetchall()
        }
        indexes = {
            r["indexname"]: r["indexdef"]
            for r in con.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'sf_query_state'"
            ).fetchall()
        }
    assert constraints == {
        "sf_query_state_generation": "UNIQUE (conversation_id, generation_id)",
        "sf_query_state_summary_len": "CHECK ((length(summary) <= 8000))",
        "sf_query_state_plan_line_len": "CHECK ((length(plan_line) <= 600))",
    }
    assert set(indexes) == {
        "sf_query_state_pkey",
        "sf_query_state_generation",
        "idx_sf_query_state_lookup",
        "idx_sf_query_state_user",
        "idx_sf_query_state_expiry",
    }
    assert indexes["idx_sf_query_state_lookup"].endswith(
        "USING btree (conversation_id, answer_sha1, created_at DESC)"
    )
    assert indexes["idx_sf_query_state_user"].endswith("USING btree (user_id)")
    assert indexes["idx_sf_query_state_expiry"].endswith(
        "USING btree (expires_at) WHERE (expires_at IS NOT NULL)"
    )


def test_an_insert_with_only_the_required_columns_gets_the_defaults():
    alice = _user("p0-alice")
    row = _row(_chat(alice, "p0-chat-defaults"), alice, "gen-defaults")
    assert row["workspace_id"] == "" and row["access_profile"] is None
    assert row["parent_sha1"] == "" and row["question_sha1"] == ""
    assert row["state"] is None and row["plan_line"] == "" and row["summary"] == ""
    assert row["summary_pairs"] == 0 and row["path_pairs"] == 0
    assert row["created_at"] is not None
    assert row["summary_at"] is None and row["expires_at"] is None


def test_both_checks_hold_at_their_limits():
    alice = _user("p0-alice")
    conv = _chat(alice, "p0-chat-checks")
    _row(conv, alice, "gen-plan-600", plan_line="p" * 600)
    _row(conv, alice, "gen-summary-8000", summary="s" * 8000)
    with pytest.raises(psycopg.errors.CheckViolation, match="sf_query_state_plan_line_len"):
        _row(conv, alice, "gen-plan-601", plan_line="p" * 601)
    with pytest.raises(psycopg.errors.CheckViolation, match="sf_query_state_summary_len"):
        _row(conv, alice, "gen-summary-8001", summary="s" * 8001)


def test_one_row_per_generation_in_a_chat():
    alice = _user("p0-alice")
    one = _chat(alice, "p0-chat-unique-1")
    two = _chat(alice, "p0-chat-unique-2")
    _row(one, alice, "gen-same")
    _row(two, alice, "gen-same")  # another chat: its own row
    with pytest.raises(psycopg.errors.UniqueViolation, match="sf_query_state_generation"):
        _row(one, alice, "gen-same")


def test_a_row_needs_its_chat_and_its_user():
    alice = _user("p0-alice")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _row("p0-chat-never-made", alice, "gen-no-chat")
    conv = _chat(alice, "p0-chat-no-user")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _row(conv, 987654, "gen-no-user")


def test_rows_go_with_their_chat():
    alice = _user("p0-alice")
    gone = _chat(alice, "p0-chat-deleted")
    kept = _chat(alice, "p0-chat-kept")
    _row(gone, alice, "gen-1")
    _row(gone, alice, "gen-2")
    _row(kept, alice, "gen-3")
    assert db.delete_conversation(alice, gone)
    assert _count(conversation_id=gone) == 0
    assert _count(conversation_id=kept) == 1


def test_rows_go_with_their_user_even_in_someone_elses_chat():
    """The user foreign key cascades on its own: a row filed under a user is
    removed with that user, even where the chat belongs to another."""
    alice = _user("p0-alice")
    bob = _user("p0-bob")
    conv = _chat(alice, "p0-chat-shared-ids")
    _row(conv, alice, "gen-alice")
    _row(conv, bob, "gen-bob")
    with db.connection() as con:
        con.execute("DELETE FROM users WHERE id = %s", (bob,))
    assert _count(conversation_id=conv) == 1
    assert _count(user_id=alice) == 1


# ------------------------------------------- fresh install against upgrade --


def _fingerprint(con) -> dict:
    """Every column, index and constraint of the public schema, ordered."""
    return {
        "columns": con.execute(
            "SELECT table_name, column_name, ordinal_position, data_type, is_nullable, "
            "COALESCE(column_default, '') AS d, is_identity FROM information_schema.columns "
            "WHERE table_schema = 'public' ORDER BY 1, 3"
        ).fetchall(),
        "indexes": con.execute(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' ORDER BY 1, 2"
        ).fetchall(),
        "constraints": con.execute(
            "SELECT conrelid::regclass::text AS t, conname, pg_get_constraintdef(oid) AS d "
            "FROM pg_constraint WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2"
        ).fetchall(),
    }


def _companion_name(session_name: str) -> str:
    """A build database private to this session's database (as the crawl
    durability test does), within PostgreSQL's 63-byte names."""
    tail = "_v45_test"
    head = session_name.split("?", 1)[0].encode()[: 63 - len(tail)].decode("utf-8", "ignore")
    return head + tail


def _companion_dsn(app_database: str) -> str:
    """The companion database's DSN, created on the test server when missing."""
    base, _, session_name = app_database.rpartition("/")
    name = _companion_name(session_name)
    with psycopg.connect(f"{base}/postgres", autocommit=True, connect_timeout=5) as admin:
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            admin.execute(
                f'CREATE DATABASE "{name}" TEMPLATE template0 '
                "LC_COLLATE 'C' LC_CTYPE 'C' ENCODING 'UTF8'"
            )
    return f"{base}/{name}"


@pytest.fixture
def schema_build(app_database, monkeypatch):
    """Build the schema from nothing in a companion database, with the real
    `init_schema`, optionally stopping at a version first (an upgrade)."""
    dsn = _companion_dsn(app_database)

    def build(up_to=None):
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as con:
            con.execute("DROP SCHEMA public CASCADE")
            con.execute("CREATE SCHEMA public")
        monkeypatch.setattr(settings, "app_database_url", dsn)
        db.close_pool()
        staged = None
        try:
            if up_to is not None:
                full = db._MIGRATIONS
                monkeypatch.setattr(db, "_MIGRATIONS", tuple(m for m in full if m[0] <= up_to))
                db.init_schema()
                staged = {"version": db.schema_version()}
                with db.connection() as con:
                    staged["table"] = con.execute(
                        "SELECT to_regclass('public.sf_query_state') AS t"
                    ).fetchone()["t"]
                monkeypatch.setattr(db, "_MIGRATIONS", full)
            db.init_schema()
            db.init_schema()  # a second boot applies nothing
            with db.connection() as con:
                fingerprint = _fingerprint(con)
                versions = [
                    r["version"]
                    for r in con.execute("SELECT version FROM schema_migrations ORDER BY 1").fetchall()
                ]
        finally:
            db.close_pool()
            monkeypatch.setattr(settings, "app_database_url", app_database)
            db.close_pool()
        return fingerprint, versions, staged

    yield build

    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as con:
        con.execute("DROP SCHEMA IF EXISTS public CASCADE")
        con.execute("CREATE SCHEMA public")


def test_a_fresh_install_and_an_upgrade_from_v44_converge(schema_build):
    fresh, fresh_versions, _ = schema_build()
    upgraded, upgraded_versions, staged = schema_build(up_to=44)
    assert staged == {"version": 44, "table": None}
    assert fresh_versions == upgraded_versions == list(range(1, 46))
    assert fresh == upgraded
    assert any(r["table_name"] == "sf_query_state" for r in fresh["columns"])


@pytest.fixture
def companion_at_v44(app_database, monkeypatch):
    """The companion database at V44, built with the real `init_schema`, and
    the app pointed at it until the test ends."""
    dsn = _companion_dsn(app_database)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as con:
        con.execute("DROP SCHEMA public CASCADE")
        con.execute("CREATE SCHEMA public")
    monkeypatch.setattr(settings, "app_database_url", dsn)
    db.close_pool()
    full = db._MIGRATIONS
    try:
        monkeypatch.setattr(db, "_MIGRATIONS", tuple(m for m in full if m[0] <= 44))
        db.init_schema()
        monkeypatch.setattr(db, "_MIGRATIONS", full)
        assert db.schema_version() == 44
        yield dsn
    finally:
        db.close_pool()
        monkeypatch.setattr(settings, "app_database_url", app_database)
        db.close_pool()
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as con:
            con.execute("DROP SCHEMA IF EXISTS public CASCADE")
            con.execute("CREATE SCHEMA public")


def test_v45_runs_behind_the_house_lock_timeout_though_its_text_is_frozen():
    assert db._MIGRATION_LOCK_TIMEOUTS == {45: "3s"}
    assert "lock_timeout" not in db._MIGRATION_V45, "the design's DDL, byte for byte"


def test_an_open_writer_makes_v45_fail_fast_and_init_schema_retry_it(companion_at_v44, monkeypatch, caplog):
    """Review DS2-2: one open write on `conversations` (a parked psql session,
    an old orchestrator during an overlapping deploy) makes V45 wait for its
    lock. Behind the house lock_timeout the wait ends fast with
    LockNotAvailable, `init_schema` retries, and the boot reaches V45 once the
    writer is gone, instead of holding every writer behind it until
    statement_timeout and then failing start-up. The timeout is shortened
    here only to keep the test quick."""
    owner = db.create_user("p0-lock-owner", "!p0-test")
    db.create_conversation(owner, "p0-lock-chat", "made-up chat")
    monkeypatch.setitem(db._MIGRATION_LOCK_TIMEOUTS, 45, "300ms")
    monkeypatch.setattr(db, "INIT_SCHEMA_LOCK_BACKOFF_S", (0.5,))
    holder = psycopg.connect(companion_at_v44, autocommit=False, connect_timeout=5)
    holder.execute("UPDATE conversations SET title = title WHERE id = 'p0-lock-chat'")
    timer = threading.Timer(1.2, holder.rollback)
    db.close_pool()
    caplog.set_level(logging.WARNING, logger="app.db")
    started = time.monotonic()
    timer.start()
    try:
        db.init_schema()
    finally:
        timer.join(5)
        holder.close()
    assert db.schema_version() == 45
    assert any("could not take its lock" in record.getMessage() for record in caplog.records)
    assert time.monotonic() - started < 10
    with db.connection() as con:
        assert con.execute("SHOW lock_timeout").fetchone()["lock_timeout"] != "300ms", "SET LOCAL only"


# ------------------------------------------- the jsonb writer (point 16) --


@pytest.mark.parametrize(
    "value",
    [
        {"path": "C:\\u0000temp"},           # a typed backslash, then u0000
        {"tail": "ends with \\u0000"},       # used to leave invalid JSON
        {"\\u0000key": ["\\u0000", ("\\u0000n",)]},
        {"double": "\\\\u0000 and \\u0000\\u0000"},
    ],
    ids=["middle", "end", "key-and-nested", "repeated"],
)
def test_a_typed_backslash_u0000_is_stored_exactly(value):
    text = db._dumps_jsonb(value)
    assert json.loads(text) == json.loads(json.dumps(value))


def test_a_real_nul_is_removed_from_strings_keys_and_nested_values():
    value = {"a\x00b": "x\x00y", "list": [1, "\x00", ("t\x00",), {"k\x00": None}]}
    assert json.loads(db._dumps_jsonb(value)) == {
        "ab": "xy",
        "list": [1, "", ["t"], {"k": None}],
    }


def test_a_real_nul_beside_a_typed_escape_keeps_the_typed_text():
    value = {"mixed": "C:\\u0000\x00temp", "plain": "no nul"}
    assert json.loads(db._dumps_jsonb(value)) == {"mixed": "C:\\u0000temp", "plain": "no nul"}


def test_odd_values_are_written_as_before():
    class Odd:
        def __str__(self):
            return "odd\x00value"

    from datetime import datetime

    when = datetime(2026, 11, 25, 9, 30)
    value = {"when": when, "odd": Odd(), "nan": float("nan"), "nul": "\x00"}
    assert json.loads(db._dumps_jsonb(value)) == {
        "when": str(when), "odd": "oddvalue", "nan": None, "nul": "",
    }
    # With nothing to remove, the text is exactly strict_json's.
    clean = {"when": when, "n": 1.5, "s": "text", "nested": [{"a": True}]}
    assert db._dumps_jsonb(clean) == strict_json(clean)


def test_the_object_is_walked_only_when_the_escape_appears(monkeypatch):
    def boom(_value):
        raise AssertionError("walked a value with no NUL escape")

    monkeypatch.setattr(db, "_without_nul", boom)
    assert json.loads(db._dumps_jsonb({"a": "plain", "b": [1, 2]})) == {"a": "plain", "b": [1, 2]}


def _old_dumps_jsonb(obj):
    """`db._dumps_jsonb` before 2026-11-25: the NUL escape cut from the text."""
    text = strict_json(obj)
    return text.replace("\\u0000", "") if "\\u0000" in text else text


class _Odd:
    def __str__(self):
        return "odd\x00value"


@pytest.mark.parametrize(
    "value",
    [
        {"proxy": types.MappingProxyType({"a": 1}), "score": float("nan"), "nul": "x\x00"},
        {"proxy": types.MappingProxyType({"k\x00": "v\x00"}), "inf": float("inf")},
        {"proxy": types.MappingProxyType({"a": 1}), "nul": "x\x00"},
        {1: "a\x00", 2.5: "b", False: "c", None: "d", "set": {"s"}, "odd": _Odd()},
        {"a": 1, "a\x00": 2},
        [("x\x00", 1), ("y", 2)],
        {float("nan"): 1, "k": "\x00", "when": decimal.Decimal("1.50")},
        {"p": "a\\\x00b", "deep": [{"l": [("t\x00", {"u\x00": None})]}]},
    ],
    ids=["mapping-nan-nul", "mapping-keys-inf", "mapping-nul", "odd-keys-and-types", "key-collision",
         "tuples", "nan-key", "nested"],
)
def test_a_value_with_a_nul_is_stored_as_before_but_for_the_nul(value):
    """Review DS2-3: the NUL goes, and everything else is stored exactly as the
    old helper stored it (a non-dict Mapping beside a NaN stays an object; an
    odd value is its `str()` as before). The values hold no typed escape, so
    the old helper's text replace was right for them."""
    assert json.loads(db._dumps_jsonb(value)) == json.loads(_old_dumps_jsonb(value))
    assert "\\u0000" not in db._dumps_jsonb(value)


def test_message_meta_with_a_typed_escape_survives_a_round_trip():
    alice = _user("p0-alice")
    conv = _chat(alice, "p0-chat-meta")
    meta = {"note": "C:\\u0000temp", "tail": "x\\u0000", "nul": "a\x00b"}
    assert db.add_message(alice, conv, "assistant", "made-up answer", meta)
    stored = db.list_messages(conv)[-1]["meta"]
    stored = json.loads(stored) if isinstance(stored, str) else stored
    assert stored["note"] == "C:\\u0000temp"
    assert stored["tail"] == "x\\u0000"
    assert stored["nul"] == "ab"


def test_a_state_written_through_the_house_helper_survives_a_round_trip():
    alice = _user("p0-alice")
    conv = _chat(alice, "p0-chat-state-jsonb")
    state = {"v": 1, "calls": [{"args": {"name": "C:\\u0000temp\x00"}}]}
    row = _row(conv, alice, "gen-jsonb", state=db._json_param(state))
    assert row["state"] == {"v": 1, "calls": [{"args": {"name": "C:\\u0000temp"}}]}


# ------------------------------------------------------- settings (§15.1) --

#: attribute, environment name, default (design §15.1, §12.2; stage-1 open
#: point 12: these exact names are read by sf_summary and retention).
_FLAGS = [
    ("sf_assistant_memory", "SF_ASSISTANT_MEMORY", False),
    ("sf_assistant_memory_pairs", "SF_ASSISTANT_MEMORY_PAIRS", 6),
    ("sf_assistant_memory_tokens", "SF_ASSISTANT_MEMORY_TOKENS", 6000),
    ("sf_assistant_summary", "SF_ASSISTANT_SUMMARY", False),
    ("chat_retention_enabled", "CHAT_RETENTION_ENABLED", False),
    ("chat_retention_dry_run", "CHAT_RETENTION_DRY_RUN", True),
    ("query_traces_retention_days", "QUERY_TRACES_RETENTION_DAYS", 180),
    ("chat_requests_retention_days", "CHAT_REQUESTS_RETENTION_DAYS", 30),
    ("chat_retention_batch", "CHAT_RETENTION_BATCH", 1000),
    ("chat_retention_max_batches", "CHAT_RETENTION_MAX_BATCHES", 20),
    ("chat_retention_old_route_orphans", "CHAT_RETENTION_OLD_ROUTE_ORPHANS", False),
    ("chat_retention_backup_date", "CHAT_RETENTION_BACKUP_DATE", ""),
]
_BOOLS = [env for _attr, env, default in _FLAGS if isinstance(default, bool)]
_INTS = [env for _attr, env, default in _FLAGS if type(default) is int]


@pytest.fixture
def clean_env(monkeypatch):
    for _attr, env, _default in _FLAGS:
        monkeypatch.delenv(env, raising=False)
    return monkeypatch


def test_every_flag_has_the_designs_default(clean_env):
    fresh = Settings()
    for attr, _env, default in _FLAGS:
        value = getattr(fresh, attr)
        assert value == default and type(value) is type(default), attr


@pytest.mark.parametrize("env", _BOOLS)
@pytest.mark.parametrize("word,expected", [("true", True), ("ON", True), ("1", True), ("yes", True),
                                           ("false", False), ("Off", False), ("0", False), ("no", False)])
def test_a_switch_reads_the_usual_words(clean_env, env, word, expected):
    clean_env.setenv(env, word)
    attr = env.lower()
    assert getattr(Settings(), attr) is expected


@pytest.mark.parametrize("env", _BOOLS)
def test_a_blank_switch_is_its_default(clean_env, env):
    clean_env.setenv(env, "   ")
    default = dict((e, d) for _a, e, d in _FLAGS)[env]
    assert getattr(Settings(), env.lower()) is default


@pytest.mark.parametrize("env", _BOOLS)
def test_an_unreadable_switch_stops_start_up_without_echoing_the_value(clean_env, env):
    """`_bool` would read "ture" as false, which for the dry run means a
    real delete; these switches refuse instead."""
    clean_env.setenv(env, "ture-PLANTED-a06000000000099AAA")
    with pytest.raises(ValueError) as caught:
        Settings()
    assert env in str(caught.value)
    assert "PLANTED" not in str(caught.value)


@pytest.mark.parametrize("raw", ["six", "6k", "1e3", "4.5"])
@pytest.mark.parametrize("env", _INTS)
def test_a_number_that_is_not_one_stops_start_up_naming_the_variable(clean_env, env, raw):
    """Review N3: the message names the variable and does not echo the value."""
    clean_env.setenv(env, raw)
    with pytest.raises(ValueError) as caught:
        Settings()
    assert env in str(caught.value) and raw not in str(caught.value)


@pytest.mark.parametrize("env", _INTS)
def test_a_number_reads_as_int_reads_it_and_blank_is_its_default(clean_env, env):
    default = dict((e, d) for _a, e, d in _FLAGS)[env]
    clean_env.setenv(env, "  ")
    assert getattr(Settings(), env.lower()) == default
    clean_env.setenv(env, " 4 ")
    assert getattr(Settings(), env.lower()) == 4


def test_the_window_is_held_to_what_contract_v2_takes(clean_env):
    for raw, expected in (("0", 1), ("-3", 1), ("4", 4), ("6", 6), ("9", 6)):
        clean_env.setenv("SF_ASSISTANT_MEMORY_PAIRS", raw)
        assert Settings().sf_assistant_memory_pairs == expected
    for raw, expected in (("0", 1), ("-1", 1), ("2500", 2500)):
        clean_env.setenv("SF_ASSISTANT_MEMORY_TOKENS", raw)
        assert Settings().sf_assistant_memory_tokens == expected


def test_overrides_reach_the_retention_settings(clean_env):
    clean_env.setenv("CHAT_RETENTION_ENABLED", "true")
    clean_env.setenv("CHAT_RETENTION_DRY_RUN", "false")
    clean_env.setenv("QUERY_TRACES_RETENTION_DAYS", "365")
    clean_env.setenv("CHAT_RETENTION_BATCH", "250")
    clean_env.setenv("CHAT_RETENTION_BACKUP_DATE", " 2026-10-30 ")
    fresh = Settings()
    assert fresh.chat_retention_enabled is True and fresh.chat_retention_dry_run is False
    assert fresh.query_traces_retention_days == 365 and fresh.chat_retention_batch == 250
    assert fresh.chat_retention_backup_date == "2026-10-30"


@pytest.mark.parametrize("raw", ["30-10-2026", "2026-13-01", "2026-02-30", "20261030", "2026-W44-5", "soon"])
def test_an_unreadable_backup_date_stops_start_up(clean_env, raw):
    clean_env.setenv("CHAT_RETENTION_BACKUP_DATE", raw)
    with pytest.raises(ValueError, match="CHAT_RETENTION_BACKUP_DATE"):
        Settings()


def test_the_readers_find_the_declared_settings_at_their_defaults(clean_env):
    """sf_summary and retention read these names with getattr: declared at
    their defaults, the fold stays off and retention stays disabled and dry."""
    fresh = Settings()
    for attr, _env, _default in _FLAGS:
        clean_env.setattr(settings, attr, getattr(fresh, attr), raising=False)
    assert sf_summary.enabled() is False
    cfg = retention.load_config()
    assert (cfg.enabled, cfg.dry_run, cfg.old_route_orphans) == (False, True, False)
    assert (cfg.query_traces_days, cfg.chat_requests_days) == (180, 30)
    assert (cfg.batch, cfg.max_batches, cfg.backup_date) == (1000, 20, "")
    clean_env.setattr(settings, "sf_assistant_summary", True)
    assert sf_summary.enabled() is True


def test_the_memory_switch_row_says_who_still_reads_the_table():
    """Review DS2-5: with SF_ASSISTANT_MEMORY false the bridge never reads or
    writes `sf_query_state`, but the retention sweep (when on) still does."""
    config_doc = (_REPO / "docs" / "CONFIG.md").read_text(encoding="utf-8")
    row = next(line for line in config_doc.splitlines() if line.startswith("| `SF_ASSISTANT_MEMORY` |"))
    assert "is never read or written" not in row
    assert "the bridge never reads or writes `sf_query_state`" in row and "retention sweep" in row


def test_every_flag_is_documented_with_its_default():
    config_doc = (_REPO / "docs" / "CONFIG.md").read_text(encoding="utf-8")
    example = (_REPO / ".env.example").read_text(encoding="utf-8").splitlines()
    for _attr, env, default in _FLAGS:
        shown = str(default).lower() if isinstance(default, bool) else str(default)
        assert f"#{env}={shown}" in example, env
        row = next((line for line in config_doc.splitlines() if line.startswith(f"| `{env}` |")), "")
        assert row, f"{env} has no row in docs/CONFIG.md"
        assert (f"| `{shown}` |" in row) if shown else ("| empty |" in row), env


# ------------------------------------------- Outcome and the shared hash --


def test_outcome_carries_no_memory_by_default():
    fields = {f.name: f for f in dataclasses.fields(Outcome)}
    assert "sf_memory" in fields and fields["sf_memory"].default is None
    outcome = Outcome()
    assert outcome.sf_memory is None
    assert outcome == Outcome(handled=False)
    assert "sf_memory" not in outcome.meta_extras
    # Each outcome gets its own dict when the bridge sets one.
    assert Outcome(sf_memory={"plan_line": "made-up plan"}).sf_memory == {"plan_line": "made-up plan"}


def test_answer_key_is_the_guards_own_hash():
    for content in ("An answer.", "  An\n answer.  ", [{"type": "text", "text": "An answer."}], ""):
        assert sf_isolation.answer_key(content) == sf_isolation._key(content)
    assert sf_isolation.answer_key("An   answer.\n") == _h("An answer.")
    assert re.fullmatch(r"[0-9a-f]{40}", sf_isolation.answer_key("x"))


def test_the_guard_does_not_import_the_store():
    tree = ast.parse((_APP / "sf_isolation.py").read_text(encoding="utf-8"))
    names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any("sf_memory" in name for name in names)
