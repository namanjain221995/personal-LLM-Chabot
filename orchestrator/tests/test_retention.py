"""Chat retention (app/retention.py; design v2 §12.1-§12.2, §16.2; docs/retention.md).

Every test runs against the suite's test PostgreSQL (conftest truncates it
before each test). Rows are made up: placeholder ids, no real text. Ages are
set by the DATABASE clock (`now() - interval`), the clock the job uses.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
import subprocess
import threading
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Json

from app import analytics, db, metrics, retention, sf_isolation, sf_memory_store
from app.config import settings

_DOCS = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "retention.md")

#: Every setting the module reads. Tests start from "not declared" so they
#: exercise the getattr defaults whether or not config.py declares them yet.
_SETTINGS = (
    "chat_retention_enabled",
    "chat_retention_dry_run",
    "query_traces_retention_days",
    "chat_requests_retention_days",
    "chat_retention_batch",
    "chat_retention_max_batches",
    "chat_retention_old_route_orphans",
    "chat_retention_backup_date",
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_settings_and_gate(monkeypatch):
    for name in _SETTINGS:
        monkeypatch.delattr(settings, name, raising=False)
    retention._reset_gate_for_tests()
    yield
    retention._reset_gate_for_tests()


def _set(monkeypatch, **values):
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value, raising=False)


def _enable(monkeypatch, *, dry_run: bool, **values):
    _set(monkeypatch, chat_retention_enabled=True, chat_retention_dry_run=dry_run, **values)


def _user(name: str) -> int:
    return db.create_user(name, "not-a-real-hash")


def _chat(user_id: int, conversation_id: str) -> str:
    db.create_conversation(user_id, conversation_id, "Made-up chat")
    return conversation_id


def _trace(trace_id, conversation_id, user_id, age, *, test_case_id=None, events=2,
           question="made-up question about blue widgets"):
    with db.connection() as con:
        con.execute(
            "INSERT INTO query_traces (trace_id, conversation_id, user_id, original_question,"
            " test_case_id, started_at) VALUES (%s, %s, %s, %s, %s, now() - %s::interval)",
            (trace_id, conversation_id, user_id, question, test_case_id, age),
        )
        for seq in range(events):
            con.execute(
                "INSERT INTO query_trace_events (trace_id, sequence_number, stage, created_at,"
                " started_at, completed_at) VALUES (%s, %s, 'route', now(), now(), now())",
                (trace_id, seq),
            )


def _request(intent_id, user_id, conversation_id, status, age, *, body=None):
    with db.connection() as con:
        con.execute(
            "INSERT INTO chat_requests (intent_id, user_id, conversation_id, generation_id, status,"
            " request, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s::jsonb, now() - %s::interval, now() - %s::interval)",
            (intent_id, user_id, conversation_id, f"gen-{intent_id}", status,
             Json(body or {"text": "made-up"}), age, age),
        )


def _old_route_rows(key: str, conversation_id: str, age: str) -> None:
    """One row in each older-route table for `conversation_id`."""
    with db.connection() as con:
        con.execute(
            "INSERT INTO sf_conversation_state (conversation_id, payload, updated_at)"
            " VALUES (%s, '{}'::jsonb, now() - %s::interval)",
            (conversation_id, age),
        )
        con.execute(
            "INSERT INTO sf_intents (intent_id, conversation_id, status, payload, created_at, updated_at)"
            " VALUES (%s, %s, 'pending', '{}'::jsonb, now() - %s::interval, now() - %s::interval)",
            (f"intent-{key}", conversation_id, age, age),
        )
        con.execute(
            "INSERT INTO sf_clarifications (clarification_id, conversation_id, intent_id, state,"
            " resume_token, question_fingerprint, payload, created_at)"
            " VALUES (%s, %s, %s, 'answered', 'tok', 'fp', '{}'::jsonb, now() - %s::interval)",
            (f"clar-{key}", conversation_id, f"intent-{key}", age),
        )


def _ids(table: str, column: str) -> set:
    with db.connection() as con:
        return {r[column] for r in con.execute(f"SELECT {column} FROM {table}").fetchall()}


def _count(table: str) -> int:
    with db.connection() as con:
        return int(con.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"])


def _raw() -> psycopg.Connection:
    """A connection outside the pool and outside the job, autocommit."""
    return psycopg.connect(settings.app_database_url, autocommit=True)


def _counter(name: str, **labels) -> float:
    return metrics._counters.get(name, {}).get(tuple(sorted(labels.items())), 0.0)


def _gauge(name: str, **labels):
    return metrics._gauges.get(name, {}).get(tuple(sorted(labels.items())))


@pytest.fixture
def sf_query_state():
    """The V45 table, as migration V45 made it (the suite's session fixture
    applies every migration). Nothing is created or dropped here. The rows a
    test adds are removed afterwards; the conftest truncation of
    `conversations` and `users` would also reach them through the cascades."""
    with _raw() as con:
        existed = con.execute("SELECT to_regclass('sf_query_state') IS NOT NULL").fetchone()[0]
    assert existed, "migration V45 did not create sf_query_state"
    yield
    with _raw() as con:
        con.execute("DELETE FROM sf_query_state")


def _dataset():
    """A mixed chat database. Returns (expected per item, rows that survive)."""
    alice = _user("ret-alice")
    bob = _user("ret-bob")
    live = _chat(alice, "conv-live-0001")
    _chat(bob, "conv-live-0002")
    bare = f"u{alice}-default"
    gone = "conv-gone-0001"  # no conversations row: the chat is gone

    # query_traces
    _trace("tr-old-live", live, alice, "200 days")          # period rule
    _trace("tr-old-gone", gone, None, "190 days")           # period rule (orphan too, but old)
    _trace("tr-old-bare", bare, alice, "181 days")          # period rule, bare key
    _trace("tr-young-live", live, alice, "10 days")         # kept
    _trace("tr-orphan", gone, None, "3 days")               # orphan rule
    _trace("tr-orphan-2", "conv-gone-0002", None, "2 days")  # orphan rule
    _trace("tr-orphan-bare", bare, alice, "5 days")         # spared: bare key
    _trace("tr-orphan-case", gone, None, "5 days", test_case_id="case-0001")  # spared: test case
    _trace("tr-orphan-new", gone, None, "6 hours")          # spared: under a day

    # chat_requests
    _request("rq-old-done", alice, live, "completed", "40 days")
    _request("rq-old-failed", bob, "conv-live-0002", "failed", "31 days")
    _request("rq-old-bare", alice, bare, "cancelled", "35 days")
    _request("rq-old-open", alice, live, "interrupted", "45 days")   # resumable: kept, counted
    _request("rq-old-queued", alice, live, "queued", "45 days")      # resumable: kept, counted
    _request("rq-young", alice, live, "completed", "29 days")        # kept
    _request("rq-orphan", alice, gone, "completed", "2 days")        # orphan rule
    _request("rq-orphan-bare", alice, bare, "completed", "2 days")   # spared: bare key
    _request("rq-orphan-open", alice, gone, "running", "2 days")     # spared: resumable
    _request("rq-orphan-new", alice, gone, "completed", "3 hours")   # spared: under a day

    # the older route (D9)
    _old_route_rows("gone", gone, "3 days")       # orphans
    _old_route_rows("live", live, "3 days")       # chat alive: kept
    _old_route_rows("bare", bare, "3 days")       # bare key: kept

    expected = {
        "query_traces": 3,
        "query_traces_orphans": 2,
        "chat_requests": 3,
        "chat_requests_orphans": 1,
        "chat_requests_resumable_old": 2,
        "old_route_sf_conversation_state": 1,
        "old_route_sf_intents": 1,
        "old_route_sf_clarifications": 1,
    }
    survivors = {
        "traces": {"tr-young-live", "tr-orphan-bare", "tr-orphan-case", "tr-orphan-new"},
        "requests": {"rq-old-open", "rq-old-queued", "rq-young", "rq-orphan-bare",
                     "rq-orphan-open", "rq-orphan-new"},
    }
    return expected, survivors


# ---------------------------------------------------------------------------
# Defaults and switches
# ---------------------------------------------------------------------------


def test_defaults_before_config_declares_anything():
    cfg = retention.load_config()
    assert cfg == retention.RetentionConfig(
        enabled=False, dry_run=True, query_traces_days=180, chat_requests_days=30,
        batch=1000, max_batches=20, old_route_orphans=False, backup_date="",
    )


def test_disabled_by_default_touches_nothing(monkeypatch):
    alice = _user("ret-alice")
    _trace("tr-old", "conv-gone-0001", alice, "400 days")

    def no_connection(*_a, **_k):
        raise AssertionError("a disabled run must not connect")

    monkeypatch.setattr(retention, "_open", no_connection)
    assert retention.run_chat_retention() == {"status": "disabled"}
    assert retention.run_chat_retention(force=True) == {"status": "disabled"}
    assert _ids("query_traces", "trace_id") == {"tr-old"}


def test_enabled_alone_is_a_dry_run(monkeypatch):
    _set(monkeypatch, chat_retention_enabled=True)
    expected, _ = _dataset()
    traces, requests = _count("query_traces"), _count("chat_requests")
    out = retention.run_chat_retention()
    assert out["status"] == "dry_run"
    for name, n in expected.items():
        assert out[name] == n, name
    assert (_count("query_traces"), _count("chat_requests")) == (traces, requests)
    assert _count("sf_intents") == 3


def test_string_settings_are_read_like_the_environment(monkeypatch):
    _set(monkeypatch, chat_retention_enabled="true", chat_retention_dry_run="false",
         chat_retention_batch="50", chat_retention_max_batches="3")
    cfg = retention.load_config()
    assert (cfg.enabled, cfg.dry_run, cfg.batch, cfg.max_batches) == (True, False, 50, 3)


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def test_dry_run_counts_equal_what_a_real_run_deletes(monkeypatch, sf_query_state):
    _enable(monkeypatch, dry_run=True, chat_retention_old_route_orphans=True)
    expected, survivors = _dataset()
    with db.connection() as con:
        alice = con.execute("SELECT id FROM users WHERE username = 'ret-alice'").fetchone()["id"]
        for gen, expires in (("g1", "now() - interval '1 hour'"), ("g2", "now() + interval '1 day'"),
                             ("g3", "NULL")):
            con.execute(
                "INSERT INTO sf_query_state (conversation_id, user_id, generation_id, answer_sha1,"
                f" contract, expires_at) VALUES ('conv-live-0001', %s, %s, 'a', 2, {expires})",
                (alice, gen),
            )
    expected["sf_query_state_expired"] = 1

    dry = retention.run_chat_retention()
    assert dry["status"] == "dry_run" and dry["errors"] == 0
    _enable(monkeypatch, dry_run=False, chat_retention_old_route_orphans=True)
    real = retention.run_chat_retention()
    assert real["status"] == "deleted" and real["errors"] == 0 and real["capped"] == 0

    for name, n in expected.items():
        assert dry[name] == n, name
        assert real[name] == dry[name], name
    # No row of this data set asked for Salesforce, so nothing is stamped.
    assert dry[retention.STAMPED] == real[retention.STAMPED] == 0
    assert _ids("query_traces", "trace_id") == survivors["traces"]
    assert _ids("chat_requests", "intent_id") == survivors["requests"]
    assert _ids("sf_query_state", "generation_id") == {"g2", "g3"}
    # Nothing qualifies any more: a second dry run counts zero everywhere
    # but the kept resumable rows.
    _enable(monkeypatch, dry_run=True, chat_retention_old_route_orphans=True)
    again = retention.run_chat_retention(force=True)
    assert {k: v for k, v in again.items() if k not in ("status", "capped", "errors")} == {
        **{name: 0 for name in expected}, "chat_requests_resumable_old": 2, retention.STAMPED: 0,
    }


def test_count_and_delete_are_built_from_one_where():
    for item in retention.items(retention.load_config()):
        assert f"WHERE {item.where}" in retention.count_sql(item)
        assert f"WHERE {item.where} LIMIT %s" in retention.delete_sql(item)


def test_period_boundaries_follow_the_database_clock(monkeypatch):
    _enable(monkeypatch, dry_run=False)
    alice = _user("ret-alice")
    live = _chat(alice, "conv-live-0001")
    _trace("tr-inside", live, alice, "179 days 23 hours")
    _trace("tr-past", live, alice, "180 days 1 minute")
    _request("rq-inside", alice, live, "completed", "29 days 23 hours")
    _request("rq-past", alice, live, "completed", "30 days 1 minute")
    out = retention.run_chat_retention()
    assert (out["query_traces"], out["chat_requests"]) == (1, 1)
    assert _ids("query_traces", "trace_id") == {"tr-inside"}
    assert _ids("chat_requests", "intent_id") == {"rq-inside"}


def test_events_go_with_their_trace(monkeypatch):
    _enable(monkeypatch, dry_run=False)
    alice = _user("ret-alice")
    live = _chat(alice, "conv-live-0001")
    _trace("tr-old", live, alice, "200 days", events=5)
    _trace("tr-new", live, alice, "1 day", events=3)
    retention.run_chat_retention()
    with db.connection() as con:
        rows = con.execute("SELECT trace_id, count(*) AS n FROM query_trace_events GROUP BY 1").fetchall()
    assert {r["trace_id"]: r["n"] for r in rows} == {"tr-new": 3}


def test_only_terminal_statuses_are_deleted_and_resumable_ones_are_counted(monkeypatch, caplog):
    _enable(monkeypatch, dry_run=False)
    alice = _user("ret-alice")
    live = _chat(alice, "conv-live-0001")
    for status in (*retention.TERMINAL_STATUSES, *retention.RESUMABLE_STATUSES):
        _request(f"rq-{status}", alice, live, status, "60 days")
    with caplog.at_level(logging.INFO, logger="app.retention"):
        out = retention.run_chat_retention()
    assert out["chat_requests"] == 3
    assert out["chat_requests_resumable_old"] == 4
    assert _ids("chat_requests", "intent_id") == {f"rq-{s}" for s in retention.RESUMABLE_STATUSES}
    assert "4 chat_requests in a resumable status" in caplog.text
    assert _gauge("retention_candidates", table="chat_requests_resumable_old") == 4


def test_a_real_run_logs_kept_rows_apart_from_deleted_ones(monkeypatch, caplog):
    """Review F4. The "deleted" line names only what was deleted; the
    count-only item (D8) has its own "counted, kept" line. The dry run's one
    line and the returned dict are unchanged."""
    alice = _user("ret-alice")
    live = _chat(alice, "conv-live-0001")
    _request("rq-kept", alice, live, "interrupted", "40 days")
    _request("rq-gone", alice, live, "completed", "40 days")
    with caplog.at_level(logging.INFO, logger="app.retention"):
        _enable(monkeypatch, dry_run=True)
        retention.run_chat_retention()
        _enable(monkeypatch, dry_run=False)
        out = retention.run_chat_retention()
    lines = [r.getMessage() for r in caplog.records]
    dry = next(line for line in lines if line.startswith("retention dry-run: "))
    assert "chat_requests=1 " in dry and "chat_requests_resumable_old=1 " in dry
    deleted = next(line for line in lines if line.startswith("retention: deleted "))
    assert "chat_requests=1 " in deleted
    assert "resumable" not in deleted
    assert "retention: counted, kept chat_requests_resumable_old=1" in lines
    assert (out["status"], out["chat_requests"], out["chat_requests_resumable_old"]) == (
        "deleted", 1, 1,
    )
    assert _ids("chat_requests", "intent_id") == {"rq-kept"}


def test_the_query_state_item_names_the_stores_table():
    """The sweep's table name is the store's, so a rename cannot leave the
    expiry rule pointing at nothing."""
    names = {item.name: item.table for item in retention.items(retention.load_config())}
    assert names["sf_query_state_expired"] == sf_memory_store.TABLE


def test_orphan_sweeps_spare_bare_api_keys_test_cases_and_young_rows(monkeypatch):
    _enable(monkeypatch, dry_run=False)
    alice = _user("ret-alice")
    live = _chat(alice, "conv-live-0001")
    bare = f"u{alice}-sess-1"
    gone = "conv-gone-0001"
    _trace("tr-orphan", gone, None, "2 days")
    _trace("tr-bare", bare, alice, "2 days")
    _trace("tr-case", gone, None, "2 days", test_case_id="case-0001")
    _trace("tr-young", gone, None, "23 hours")
    _trace("tr-live", live, alice, "2 days")
    _request("rq-orphan", alice, gone, "failed", "2 days")
    _request("rq-bare", alice, bare, "completed", "2 days")
    _request("rq-young", alice, gone, "completed", "23 hours")
    _request("rq-live", alice, live, "completed", "2 days")
    out = retention.run_chat_retention()
    assert (out["query_traces_orphans"], out["chat_requests_orphans"]) == (1, 1)
    assert _ids("query_traces", "trace_id") == {"tr-bare", "tr-case", "tr-young", "tr-live"}
    assert _ids("chat_requests", "intent_id") == {"rq-bare", "rq-young", "rq-live"}


def test_bare_api_rows_and_test_case_traces_still_meet_their_period(monkeypatch):
    _enable(monkeypatch, dry_run=False)
    alice = _user("ret-alice")
    bare = f"u{alice}-sess-1"
    _trace("tr-bare-old", bare, alice, "181 days")
    _trace("tr-case-old", "conv-gone-0001", None, "181 days", test_case_id="case-0001")
    _request("rq-bare-old", alice, bare, "completed", "31 days")
    retention.run_chat_retention()
    assert _count("query_traces") == 0 and _count("chat_requests") == 0


def test_a_deleted_user_leaves_orphan_traces_that_the_sweep_removes(monkeypatch):
    """The users-FK cascade takes the chat, and `user_id ON DELETE SET NULL`
    leaves the trace: exactly the orphan the sweep is for (§12.1)."""
    _enable(monkeypatch, dry_run=False)
    carol = _user("ret-carol")
    chat = _chat(carol, "conv-carol-0001")
    _trace("tr-carol", chat, carol, "3 days")
    with db.connection() as con:
        con.execute("DELETE FROM users WHERE id = %s", (carol,))
    assert _ids("query_traces", "trace_id") == {"tr-carol"}
    out = retention.run_chat_retention()
    assert out["query_traces_orphans"] == 1 and _count("query_traces") == 0


def test_expired_query_state_rows_are_removed(monkeypatch, sf_query_state):
    _enable(monkeypatch, dry_run=False)
    alice = _user("ret-alice")
    _chat(alice, "conv-live-0001")
    with db.connection() as con:
        for gen, expires in (("past", "now() - interval '1 minute'"),
                             ("future", "now() + interval '1 hour'"), ("never", "NULL")):
            con.execute(
                "INSERT INTO sf_query_state (conversation_id, user_id, generation_id, answer_sha1,"
                f" contract, expires_at) VALUES ('conv-live-0001', %s, %s, 'a', 2, {expires})",
                (alice, gen),
            )
    out = retention.run_chat_retention()
    assert out["sf_query_state_expired"] == 1
    assert _ids("sf_query_state", "generation_id") == {"future", "never"}


def test_a_missing_query_state_table_is_skipped_cleanly(monkeypatch, caplog):
    _enable(monkeypatch, dry_run=False)
    with _raw() as con:
        con.row_factory = dict_row
        assert retention._table_exists(con, "query_traces") is True
        assert retention._table_exists(con, "sf_query_state_not_here") is False
    real = retention._table_exists
    monkeypatch.setattr(
        retention, "_table_exists",
        lambda con, table: False if table == "sf_query_state" else real(con, table),
    )
    with caplog.at_level(logging.INFO, logger="app.retention"):
        out = retention.run_chat_retention()
    assert out["status"] == "deleted" and out["errors"] == 0
    assert "sf_query_state_expired" not in out
    assert "skipped=sf_query_state_expired" in caplog.text


def test_old_route_orphans_are_counted_but_kept_until_their_setting_is_on(monkeypatch):
    alice = _user("ret-alice")
    live = _chat(alice, "conv-live-0001")
    _old_route_rows("gone", "conv-gone-0001", "2 days")
    _old_route_rows("young", "conv-gone-0002", "20 hours")
    _old_route_rows("live", live, "9 days")
    _old_route_rows("bare", f"u{alice}-sess-1", "9 days")
    names = ("old_route_sf_conversation_state", "old_route_sf_intents", "old_route_sf_clarifications")

    _enable(monkeypatch, dry_run=True)
    dry = retention.run_chat_retention()
    assert [dry[n] for n in names] == [1, 1, 1]

    _enable(monkeypatch, dry_run=False)
    off = retention.run_chat_retention(force=True)
    assert not any(n in off for n in names)
    assert _count("sf_intents") == _count("sf_clarifications") == _count("sf_conversation_state") == 4

    _enable(monkeypatch, dry_run=False, chat_retention_old_route_orphans=True)
    on = retention.run_chat_retention(force=True)
    assert [on[n] for n in names] == [1, 1, 1]
    assert _ids("sf_intents", "intent_id") == {"intent-young", "intent-live", "intent-bare"}
    assert _ids("sf_clarifications", "clarification_id") == {"clar-young", "clar-live", "clar-bare"}
    assert _ids("sf_conversation_state", "conversation_id") == {
        "conv-gone-0002", live, f"u{alice}-sess-1",
    }


def test_a_period_below_the_approved_one_is_raised_to_it(monkeypatch, caplog):
    _enable(monkeypatch, dry_run=False, query_traces_retention_days=7,
            chat_requests_retention_days="1")
    alice = _user("ret-alice")
    live = _chat(alice, "conv-live-0001")
    _trace("tr-10d", live, alice, "10 days")
    _request("rq-5d", alice, live, "completed", "5 days")
    with caplog.at_level(logging.WARNING, logger="app.retention"):
        cfg = retention.load_config()
        out = retention.run_chat_retention()
    assert (cfg.query_traces_days, cfg.chat_requests_days) == (180, 30)
    assert "shorter than the approved 180 days" in caplog.text
    assert "shorter than the approved 30 days" in caplog.text
    assert (out["query_traces"], out["chat_requests"]) == (0, 0)
    assert _count("query_traces") == 1 and _count("chat_requests") == 1


def test_a_longer_period_is_honoured(monkeypatch):
    _enable(monkeypatch, dry_run=False, query_traces_retention_days=365)
    alice = _user("ret-alice")
    live = _chat(alice, "conv-live-0001")
    _trace("tr-200d", live, alice, "200 days")
    _trace("tr-400d", live, alice, "400 days")
    retention.run_chat_retention()
    assert _ids("query_traces", "trace_id") == {"tr-200d"}


def test_status_lists_match_main_and_the_table_check():
    from app import main

    assert tuple(main._RESUMABLE_STATUSES) == retention.RESUMABLE_STATUSES
    assert not set(retention.TERMINAL_STATUSES) & set(retention.RESUMABLE_STATUSES)
    with db.connection() as con:
        definition = con.execute(
            "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint"
            " WHERE conname = 'chat_requests_status'"
        ).fetchone()["d"]
    allowed = set(re.findall(r"'([a-z_]+)'", definition))
    assert allowed == set(retention.TERMINAL_STATUSES) | set(retention.RESUMABLE_STATUSES)


# ---------------------------------------------------------------------------
# The stamp before the delete (review DS2-1; toggle-off review F4)
# ---------------------------------------------------------------------------

#: Meta shapes the server wrote before the toggle-off guard stamped `mode`
#: (main.py `_store_failure`, `_finalize_generation`, and the leading meta a
#: tab stores for a stopped answer). Made-up values.
_FAILURE_META = {"intent_id": "it-fail", "attempt": 1,
                 "error": {"message": "made-up", "code": "x", "status": None, "resumable": False}}
_DETACHED_META = {"route": "sql", "trace_id": "made-up", "request_id": "req-0001"}
_STOPPED_META = {"trace_id": "made-up", "request_id": "req-0002", "intent_id": "it-stop", "attempt": 2}


def _sf_trace(trace_id, conversation_id, user_id, age, *, mode="salesforce", test_case_id=None):
    with db.connection() as con:
        con.execute(
            "INSERT INTO query_traces (trace_id, conversation_id, user_id, original_question,"
            " requested_mode, test_case_id, started_at)"
            " VALUES (%s, %s, %s, 'made-up', %s, %s, now() - %s::interval)",
            (trace_id, conversation_id, user_id, mode, test_case_id, age),
        )


def _sf_request(generation_id, user_id, conversation_id, age, *, mode="salesforce", status="completed"):
    with db.connection() as con:
        con.execute(
            "INSERT INTO chat_requests (intent_id, user_id, conversation_id, generation_id, status,"
            " request, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s::jsonb, now() - %s::interval, now() - %s::interval)",
            (f"it-{generation_id}", user_id, conversation_id, generation_id, status,
             Json({"mode": mode, "text": "made-up"}), age, age),
        )


def _answer(user_id, conversation_id, generation_id, meta, text=None) -> int:
    """A made-up question and its stored answer under `generation_id`."""
    db.add_message(user_id, conversation_id, "user", f"made-up question {generation_id}", None)
    stored = db.add_message(
        user_id, conversation_id, "assistant", text or f"made-up answer {generation_id}",
        {**meta, "generation_id": generation_id},
    )
    return int(stored["id"])


def _messages() -> dict:
    with db.connection() as con:
        rows = con.execute("SELECT id, role, content, meta FROM messages ORDER BY id").fetchall()
    return {int(r["id"]): (r["role"], r["content"], r["meta"]) for r in rows}


def _hidden(user_id, conversation_id, text) -> bool:
    """Does the toggle-off guard hide this answer text in the chat's history?"""
    sf_isolation.forget_all()
    state = asyncio.run(sf_isolation.load_state(user_id, conversation_id, fail_closed=True))
    history = [{"role": "user", "content": "made-up earlier question"},
               {"role": "assistant", "content": text}]
    out, _ = sf_isolation.redact(history, state)
    return text not in " ".join(str(m.get("content")) for m in out)


def _stamp_dataset():
    """Answers of every shape, with the rows that mark them. Returns (user,
    {name: (conversation, message id)}, the names a real run must stamp)."""
    alice = _user("ret-alice")
    chats = {}

    def chat(name, generation, meta, *rows):
        conv = _chat(alice, f"conv-{name}")
        chats[name] = (conv, _answer(alice, conv, generation, meta))
        for make in rows:
            make(conv)

    # Stamped: the only Salesforce mark of each is a row this run deletes.
    chat("failure", "gen-fail", _FAILURE_META,
         lambda c: _sf_trace("gen-fail", c, alice, "200 days"),
         lambda c: _sf_request("gen-fail", alice, c, "200 days"))   # trace and request together
    chat("detached", "gen-det", _DETACHED_META,
         lambda c: _sf_trace("gen-det", c, alice, "190 days"))      # a trace alone
    chat("stopped", "gen-stop", _STOPPED_META,
         lambda c: _sf_request("gen-stop", alice, c, "40 days"))    # a request with no trace
    chat("null-meta", "gen-null", {},
         lambda c: _sf_request("gen-null", alice, c, "35 days"))
    chat("null-mode", "gen-nmode", {**_STOPPED_META, "mode": None},  # a null mode is no mode
         lambda c: _sf_request("gen-nmode", alice, c, "35 days"))
    # Not stamped.
    chat("young", "gen-young", _FAILURE_META,
         lambda c: _sf_trace("gen-young", c, alice, "10 days"))     # the row stays
    chat("trace-decides", "gen-r12", _DETACHED_META,                # assistant-mode answer (R12)
         lambda c: _sf_trace("gen-r12", c, alice, "200 days", mode="assistant"),
         lambda c: _sf_request("gen-r12", alice, c, "200 days"))
    chat("assistant-turn", "gen-asst", _DETACHED_META,
         lambda c: _sf_trace("gen-asst", c, alice, "200 days", mode="assistant"),
         lambda c: _sf_request("gen-asst", alice, c, "40 days", mode="assistant"))
    chat("moded-assistant", "gen-ma", {**_FAILURE_META, "mode": "assistant"},
         lambda c: _sf_trace("gen-ma", c, alice, "200 days"))
    chat("moded-salesforce", "gen-ms", {**_FAILURE_META, "mode": "salesforce"},
         lambda c: _sf_trace("gen-ms", c, alice, "200 days"))
    chat("engine-says", "gen-eng", {**_DETACHED_META, "engine": "sf_assistant"},
         lambda c: _sf_request("gen-eng", alice, c, "40 days"))
    chat("resumable", "gen-open", _STOPPED_META,
         lambda c: _sf_request("gen-open", alice, c, "40 days", status="interrupted"))
    return alice, chats, {"failure", "detached", "stopped", "null-meta", "null-mode"}


def test_a_real_run_stamps_each_answer_a_deleted_row_marked_and_the_guard_still_hides_it(monkeypatch, caplog):
    """The review's probe, in the tree: the answers whose only Salesforce
    mark is a row the run deletes get `mode='salesforce'` added to their meta
    (nothing else changes), and the guard hides them after the run as it did
    before. Every other answer is left exactly as it was. The counter and the
    log line docs/retention.md names report the stamps (review R2-5)."""
    alice, chats, stamped = _stamp_dataset()
    marking = ("chat_requests", "chat_requests_orphans", "query_traces", "query_traces_orphans")
    counters = {name: _counter("retention_stamped_total", table=name) for name in marking}
    with db.connection() as con:  # the null-meta answer has no meta at all
        con.execute("UPDATE messages SET meta = NULL WHERE id = %s", (chats["null-meta"][1],))
    before = _messages()
    for name in stamped:
        conv, mid = chats[name]
        assert _hidden(alice, conv, before[mid][1]), name
    sf_chats = set(db.salesforce_chat_ids(alice))
    assert {chats[name][0] for name in stamped} <= sf_chats
    # The guard's own per-row reading (db.salesforce_chat_state): the answers
    # it calls Salesforce through a server row alone, with no mode in meta.
    server_only = {
        row["id"]
        for conv, _mid in chats.values()
        for row in db.salesforce_chat_state(alice, conv)
        if row["is_sf"] and row["mode"] is None
        and not sf_isolation.meta_says_salesforce(before[row["id"]][2])
    }
    assert server_only == {chats[name][1] for name in (*stamped, "young", "resumable")}

    _enable(monkeypatch, dry_run=False)
    with caplog.at_level(logging.INFO, logger="app.retention"):
        out = retention.run_chat_retention()
    assert out["status"] == "deleted" and out["errors"] == 0
    assert out[retention.STAMPED] == len(stamped)
    assert (out["chat_requests"], out["query_traces"]) == (7, 6)
    # Three answers are read through a request with no trace (stopped,
    # null-meta, null-mode); two through a trace (failure, detached).
    assert {name: _counter("retention_stamped_total", table=name) - counters[name] for name in marking} == {
        "chat_requests": 3, "chat_requests_orphans": 0, "query_traces": 2, "query_traces_orphans": 0,
    }
    lines = [r.getMessage() for r in caplog.records]
    assert ("retention: stamped messages_stamped=5 (meta.mode=salesforce on answers whose only "
            "Salesforce mark was a deleted row)") in lines

    after = _messages()
    assert set(after) == set(before)
    for mid, (role, content, meta) in before.items():
        name = next((n for n, (_c, m) in chats.items() if m == mid), None)
        if name in stamped:
            assert after[mid] == (role, content, {**(meta or {}), "mode": "salesforce"}), name
        else:
            assert after[mid] == (role, content, meta), name  # questions included
    for name in stamped:
        conv, mid = chats[name]
        assert _hidden(alice, conv, after[mid][1]), name
    # Exactly the rows the guard read through a deleted row were stamped:
    # the young trace and the open request still stand, so their answers
    # need no stamp yet.
    assert {mid for mid in before if after[mid] != before[mid]} == (
        server_only - {chats["young"][1], chats["resumable"][1]}
    )
    sf_isolation.forget_all()
    # No chat left the user's Salesforce set, but one: an answer stored as
    # 'assistant' under a row that said Salesforce keeps its mode (only a
    # client could write that, toggle-off review F5; a mode is never
    # overwritten), so the guard reads it by its meta once the row is gone.
    assert set(db.salesforce_chat_ids(alice)) == sf_chats - {chats["moded-assistant"][0]}
    assert _ids("query_traces", "trace_id") == {"gen-young"}
    assert _ids("chat_requests", "intent_id") == {"it-gen-open"}


def test_the_dry_run_counts_exactly_the_answers_a_real_run_stamps(monkeypatch):
    alice, chats, stamped = _stamp_dataset()
    before = _messages()
    _enable(monkeypatch, dry_run=True)
    dry = retention.run_chat_retention()
    assert dry["status"] == "dry_run" and dry[retention.STAMPED] == len(stamped)
    assert _messages() == before  # a dry run stamps nothing
    assert _gauge("retention_candidates", table=retention.STAMPED) == len(stamped)

    _enable(monkeypatch, dry_run=False)
    real = retention.run_chat_retention()
    changed = {mid for mid, row in _messages().items() if row != before[mid]}
    assert changed == {chats[name][1] for name in stamped}
    assert real[retention.STAMPED] == dry[retention.STAMPED] == len(changed)


def test_answers_with_a_mode_or_another_salesforce_mark_are_never_stamped(monkeypatch):
    """An answer stored as 'assistant' stays 'assistant' and one stored as
    'salesforce' is not written again, whatever its row said; an answer
    whose meta already says Salesforce another way needs no stamp."""
    alice = _user("ret-alice")
    shapes = {
        "gen-ma": {**_FAILURE_META, "mode": "assistant"},
        "gen-ms": {**_FAILURE_META, "mode": "salesforce"},
        "gen-eng": {**_DETACHED_META, "engine": "sf_assistant"},
        "gen-key": {**_DETACHED_META, "salesforce_mode": "made-up"},
    }
    for gen, meta in shapes.items():
        conv = _chat(alice, f"conv-{gen}")
        _answer(alice, conv, gen, meta)
        _sf_trace(gen, conv, alice, "200 days")
        _sf_request(gen, alice, conv, "200 days")
    before = _messages()
    _enable(monkeypatch, dry_run=True)
    assert retention.run_chat_retention()[retention.STAMPED] == 0
    _enable(monkeypatch, dry_run=False)
    out = retention.run_chat_retention()
    assert out[retention.STAMPED] == 0 and (out["query_traces"], out["chat_requests"]) == (4, 4)
    assert _messages() == before


def test_assistant_mode_rows_are_never_stamped(monkeypatch):
    """A turn the server answered in assistant mode is not a Salesforce
    answer for the guard, so its rows going stamp nothing: a trace that says
    'assistant', a request that says 'assistant', and a trace that says
    'assistant' over a request that asked for Salesforce (the trace decides,
    toggle-off review R12; the request items run first for this case)."""
    alice = _user("ret-alice")
    for gen, trace_mode, request_mode in (("gen-a1", "assistant", "assistant"),
                                         ("gen-a2", "assistant", "salesforce"),
                                         ("gen-a3", None, "assistant")):
        conv = _chat(alice, f"conv-{gen}")
        _answer(alice, conv, gen, _DETACHED_META)
        if trace_mode:
            _sf_trace(gen, conv, alice, "200 days", mode=trace_mode)
        _sf_request(gen, alice, conv, "200 days", mode=request_mode)
    before = _messages()
    names = [item.name for item in retention.items(retention.load_config())]
    assert names.index("chat_requests_orphans") < names.index("query_traces")
    _enable(monkeypatch, dry_run=True)
    assert retention.run_chat_retention()[retention.STAMPED] == 0
    _enable(monkeypatch, dry_run=False)
    out = retention.run_chat_retention()
    assert out[retention.STAMPED] == 0 and (out["query_traces"], out["chat_requests"]) == (2, 3)
    assert _messages() == before


def test_bare_api_and_test_case_rows_are_still_spared_and_stamp_nothing(monkeypatch):
    """The orphan rules still spare a bare-API key and a trace with a
    test_case_id (C24), Salesforce or not, and a spared row stamps nothing,
    not even an answer that shares its generation."""
    _enable(monkeypatch, dry_run=False)
    alice = _user("ret-alice")
    live = _chat(alice, "conv-live-0001")
    bare = f"u{alice}-sess-1"
    _sf_trace("gen-bare", bare, alice, "5 days")
    _sf_trace("gen-case", "conv-gone-0001", None, "5 days", test_case_id="case-0001")
    _sf_request("gen-rq-bare", alice, bare, "2 days")
    _sf_trace("gen-gone", "conv-gone-0002", None, "3 days")  # a real orphan: deleted
    mid = _answer(alice, live, "gen-case", _FAILURE_META, text="made-up answer under a test case")
    before = _messages()
    out = retention.run_chat_retention()
    assert (out["query_traces_orphans"], out["chat_requests_orphans"]) == (1, 0)
    assert out[retention.STAMPED] == 0
    assert _ids("query_traces", "trace_id") == {"gen-bare", "gen-case"}
    assert _ids("chat_requests", "intent_id") == {"it-gen-rq-bare"}
    assert _messages() == before and before[mid][2].get("mode") is None


def test_a_second_run_stamps_nothing_and_changes_nothing(monkeypatch, caplog):
    alice, chats, stamped = _stamp_dataset()
    _enable(monkeypatch, dry_run=False)
    first = retention.run_chat_retention()
    assert first[retention.STAMPED] == len(stamped)
    after_first = _messages()
    with caplog.at_level(logging.INFO, logger="app.retention"):
        again = retention.run_chat_retention(force=True)
    assert again["status"] == "deleted" and again["errors"] == 0
    assert again[retention.STAMPED] == 0
    assert "retention: stamped" not in caplog.text  # the line only when N is not 0
    assert all(again[name] == 0 for name in ("chat_requests", "query_traces"))
    assert _messages() == after_first
    _enable(monkeypatch, dry_run=True)
    assert retention.run_chat_retention(force=True)[retention.STAMPED] == 0


def test_a_batch_that_fails_leaves_no_stamp(monkeypatch):
    """The stamp and the delete are one statement: when the delete cannot
    take its row (another session holds it, the run's lock wait gives up),
    the stamp is rolled back with it, and the next run does both."""
    alice = _user("ret-alice")
    conv = _chat(alice, "conv-live-0001")
    mid = _answer(alice, conv, "gen-held", _STOPPED_META)
    _sf_request("gen-held", alice, conv, "40 days")
    before = _messages()
    real_open = retention._open

    def short_lock_wait():
        con = real_open()
        con.execute("SET lock_timeout = '300ms'")
        return con

    monkeypatch.setattr(retention, "_open", short_lock_wait)
    _enable(monkeypatch, dry_run=False)
    with _raw() as holder:
        holder.autocommit = False
        holder.execute("SELECT 1 FROM chat_requests WHERE generation_id = 'gen-held' FOR UPDATE")
        out = retention.run_chat_retention()
        holder.rollback()
    assert out["errors"] == 1 and out[retention.STAMPED] == 0
    assert _messages() == before and _ids("chat_requests", "intent_id") == {"it-gen-held"}
    again = retention.run_chat_retention()  # the error left the day open
    assert again[retention.STAMPED] == 1 and again["chat_requests"] == 1
    assert _messages()[mid][2] == {**before[mid][2], "mode": "salesforce"}


def _r12_chat(user_id, age="200 days"):
    """An answer the server gave in assistant mode to a request that asked
    for Salesforce (toggle-off review R12): the trace says 'assistant', the
    request 'salesforce'. The guard reads the trace and shows the answer."""
    conv = _chat(user_id, "conv-r12-0001")
    mid = _answer(user_id, conv, "gen-r12", _DETACHED_META)
    _sf_trace("gen-r12", conv, user_id, age, mode="assistant")
    _sf_request("gen-r12", user_id, conv, age)
    return conv, mid


@pytest.mark.parametrize("how", ["error", "cap"])
def test_a_request_item_that_does_not_finish_holds_the_traces_back(monkeypatch, caplog, how):
    """Review R1-1: a run whose request item fails or stops at the cap
    deletes no trace, so a trace never goes while its request stands and
    the next run never stamps the R12 answer. The day stays open."""
    alice = _user("ret-alice")
    extra = {}
    if how == "cap":
        filler = _chat(alice, "conv-filler-0001")
        for i in range(2):  # stored first, so they fill the two capped batches
            _sf_request(f"gen-fill-{i}", alice, filler, "60 days", mode="assistant")
        extra = {"chat_retention_batch": 1, "chat_retention_max_batches": 2}
    conv, mid = _r12_chat(alice)
    before = _messages()
    assert not _hidden(alice, conv, before[mid][1])
    _enable(monkeypatch, dry_run=True, **extra)
    assert retention.run_chat_retention()[retention.STAMPED] == 0
    real_open = retention._open

    def short_lock_wait():
        con = real_open()
        con.execute("SET lock_timeout = '300ms'")
        return con

    monkeypatch.setattr(retention, "_open", short_lock_wait)
    _enable(monkeypatch, dry_run=False, **extra)
    with _raw() as holder, caplog.at_level(logging.INFO, logger="app.retention"):
        holder.autocommit = False
        if how == "error":
            holder.execute("SELECT 1 FROM chat_requests WHERE generation_id = 'gen-r12' FOR UPDATE")
        first = retention.run_chat_retention()
        holder.rollback()
    assert (first["errors"], first["capped"]) == ((1, 0) if how == "error" else (0, 1))
    assert "query_traces" not in first and "query_traces_orphans" not in first
    assert first[retention.STAMPED] == 0 and "gen-r12" in _ids("query_traces", "trace_id")
    assert "retention: held back query_traces,query_traces_orphans" in caplog.text
    assert not _hidden(alice, conv, before[mid][1])
    second = retention.run_chat_retention()  # the day stayed open
    assert (second["chat_requests"], second["query_traces"], second[retention.STAMPED]) == (1, 1, 0)
    assert (second["errors"], second["capped"]) == (0, 0)
    assert _messages() == before and not _hidden(alice, conv, before[mid][1])


def test_a_trace_waits_while_its_request_is_within_a_longer_request_period(monkeypatch):
    """Review R1-1: with CHAT_REQUESTS_RETENTION_DAYS past the trace period,
    a trace whose request still stands in a live chat waits, and goes in
    the run that deletes the request. A trace with no request still goes at
    its own period, and so does one whose chat is gone. Each dry run counts
    what the real run then deletes and stamps."""
    alice = _user("ret-alice")
    conv, mid = _r12_chat(alice, age="190 days")
    alone = _chat(alice, "conv-alone-0001")
    lone = _answer(alice, alone, "gen-alone", _DETACHED_META)
    _sf_trace("gen-alone", alone, alice, "190 days")
    _sf_trace("gen-gone", "conv-gone-0001", None, "190 days")
    _sf_request("gen-gone", alice, "conv-gone-0001", "190 days")
    before = _messages()
    keys = ("query_traces", "chat_requests", "chat_requests_orphans", retention.STAMPED)
    for dry_run in (True, False):
        _enable(monkeypatch, dry_run=dry_run, chat_requests_retention_days=200)
        out = retention.run_chat_retention(force=True)
        assert tuple(out[k] for k in keys) == (2, 0, 1, 1), dry_run
    assert _ids("query_traces", "trace_id") == {"gen-r12"}
    assert _messages()[lone][2] == {**before[lone][2], "mode": "salesforce"}
    assert not _hidden(alice, conv, before[mid][1])  # the trace still decides
    with db.connection() as con:  # 15 days later
        con.execute("UPDATE chat_requests SET created_at = now() - interval '205 days'")
    for dry_run in (True, False):
        _enable(monkeypatch, dry_run=dry_run, chat_requests_retention_days=200)
        out = retention.run_chat_retention(force=True)
        assert tuple(out[k] for k in keys) == (1, 1, 0, 0), dry_run
    assert _count("query_traces") == 0 and _count("chat_requests") == 0
    assert _messages()[mid] == before[mid] and not _hidden(alice, conv, before[mid][1])


@pytest.mark.parametrize("raw, stamps", [("null", 1), ("[]", 0), ('"made-up"', 0)])
def test_a_json_null_meta_is_stamped_and_an_array_or_string_meta_is_left_alone(monkeypatch, raw, stamps):
    """Review R1-4: the guard reads a meta that is not an object as {}. A
    JSON null holds nothing, so the stamp turns it into the one-key object
    and the answer stays hidden. An array or a string is never rewritten,
    because the merge would not be additive (docs/retention.md, "What it
    cannot reach"). The dry run counts the same."""
    alice = _user("ret-alice")
    conv = _chat(alice, "conv-odd-0001")
    mid = _answer(alice, conv, "gen-odd", {})
    with db.connection() as con:
        con.execute("UPDATE messages SET meta = %s::jsonb WHERE id = %s", (raw, mid))
    _sf_request("gen-odd", alice, conv, "40 days")
    before = _messages()
    assert _hidden(alice, conv, before[mid][1])
    _enable(monkeypatch, dry_run=True)
    assert retention.run_chat_retention()[retention.STAMPED] == stamps
    _enable(monkeypatch, dry_run=False)
    out = retention.run_chat_retention()
    assert out["chat_requests"] == 1 and out[retention.STAMPED] == stamps
    if stamps:
        assert _messages()[mid][2] == {"mode": "salesforce"} and _hidden(alice, conv, before[mid][1])
    else:
        assert _messages() == before


def test_the_stamp_and_its_count_are_built_from_one_text():
    """The dry-run count and the real stamp share `stamp_where`, and both
    carry the item's own WHERE; only the two tables the guard reads stamp."""
    for item in retention.items(retention.load_config()):
        if not item.marks:
            assert item.table not in ("query_traces", "chat_requests") or not item.delete
            with pytest.raises(ValueError):
                retention.stamp_where(item, "SELECT 1")
            continue
        count, delete = retention.stamp_count_sql(item), retention.delete_sql(item)
        assert retention.stamp_where(item, retention._rows_generations(item)) in count
        assert f"WHERE {item.where}" in count
        assert retention.stamp_where(item, "SELECT b.generation_id FROM batch b") in delete
        assert f"WHERE {item.where} LIMIT %s" in delete
        assert sf_isolation.sf_answer_sql("m") in count and sf_isolation.sf_answer_sql("m") in delete
        assert "content" not in count and "content" not in delete


# ---------------------------------------------------------------------------
# Review round 2: what the stamp cannot reach, what it changes outside the
# guard, and a dry run whose stamp total is not known
# ---------------------------------------------------------------------------


def _doc_text() -> str:
    """docs/retention.md with its line breaks folded, for phrase checks."""
    with open(_DOCS, encoding="utf-8") as fh:
        return " ".join(fh.read().split())


def _shown(user_id, conversation_id, history) -> list:
    """For each turn of `history`: does the guard leave it as it is?"""
    sf_isolation.forget_all()
    state = asyncio.run(sf_isolation.load_state(user_id, conversation_id, fail_closed=True))
    out, _ = sf_isolation.redact(history, state)
    return [o.get("content") == h["content"] for o, h in zip(out, history)]


def _whole_chat_count_ids() -> set:
    """The chats the read-only count in docs/retention.md counts, run as
    written (and once more with its count turned into the chat ids)."""
    sql = _doc_block("retention-count-whole-chats")
    assert sql.startswith("SELECT count(*) AS chats\n")
    with _raw() as con:
        con.row_factory = dict_row
        total = int(con.execute(sql).fetchone()["chats"])
        ids = {r["id"] for r in con.execute(sql.replace("SELECT count(*) AS chats", "SELECT c.id", 1))}
    assert total == len(ids)
    return ids


def test_the_documented_count_names_the_chats_that_go_clean_whole(monkeypatch):
    """Review R2-1. A chat whose only Salesforce answer is of kind (a), (b),
    (c) or (d) goes clean WHOLE once its rows go: it leaves the user's
    Salesforce set, and a later answer hidden only because the chat held
    Salesforce is shown. The read-only count in docs/retention.md names
    exactly those chats before the run. Where another answer keeps its mark
    the chat still holds, and only what the docs list under "one message at
    a time" is shown."""
    text = _doc_text()
    assert "And, one message at a time:" not in text
    assert "**Each of (a) to (d) does this on its own**" in text
    sql = _doc_block("retention-count-whole-chats")
    assert f"'{sf_isolation.SF_META_JSONPATH}'" in sql
    assert f"k.status IN {retention._sql_list(retention.RESUMABLE_STATUSES)}" in sql

    alice = _user("ret-alice")
    convs = {name: _chat(alice, f"conv-{name}") for name in (
        "a-only", "b-only", "c-only", "d-only", "c-then-mark", "mark-then-cdb", "meta-says", "kept-request",
        "plain")}

    def later(name, text):  # an answer with no Salesforce mark of its own
        db.add_message(alice, convs[name], "user", "made-up later question", None)
        db.add_message(alice, convs[name], "assistant", text, {"route": "chat"})

    moded = {**_STOPPED_META, "mode": "assistant"}
    # (a) an answer stored with no generation; the chat's trace names none.
    db.add_message(alice, convs["a-only"], "user", "made-up question a", None)
    db.add_message(alice, convs["a-only"], "assistant", "made-up answer a", {"route": "sql", "trace_id": "made-up"})
    _sf_trace("gen-a-gone", convs["a-only"], alice, "200 days")
    # (b) a question asked in Salesforce mode, its answer never stored.
    db.add_message(alice, convs["b-only"], "user", "made-up question b", {"intent": {"id": "it-gen-b"}})
    _sf_request("gen-b", alice, convs["b-only"], "40 days")
    # (c) and (d), each the chat's only Salesforce answer.
    _answer(alice, convs["c-only"], "gen-c", moded)
    _sf_request("gen-c", alice, convs["c-only"], "40 days")
    odd = [_answer(alice, convs["d-only"], "gen-d", {})]
    _sf_request("gen-d", alice, convs["d-only"], "40 days")
    for name in ("a-only", "b-only", "c-only", "d-only"):
        later(name, f"made-up later answer {name}")
    # Another answer keeps its mark (stamped by its request or its trace).
    _answer(alice, convs["c-then-mark"], "gen-c1", moded, "made-up c answer first")
    _sf_request("gen-c1", alice, convs["c-then-mark"], "40 days")
    _answer(alice, convs["c-then-mark"], "gen-m1", _STOPPED_META)
    _sf_request("gen-m1", alice, convs["c-then-mark"], "40 days")
    _answer(alice, convs["mark-then-cdb"], "gen-m2", _FAILURE_META)
    _sf_trace("gen-m2", convs["mark-then-cdb"], alice, "200 days")
    _answer(alice, convs["mark-then-cdb"], "gen-c2", moded, "made-up c answer after")
    _sf_request("gen-c2", alice, convs["mark-then-cdb"], "40 days")
    odd.append(_answer(alice, convs["mark-then-cdb"], "gen-d2", {}, "made-up d answer"))
    _sf_request("gen-d2", alice, convs["mark-then-cdb"], "40 days")
    db.add_message(alice, convs["mark-then-cdb"], "user", "made-up question b2", {"intent": {"id": "it-gen-b2"}})
    _sf_request("gen-b2", alice, convs["mark-then-cdb"], "40 days")
    later("mark-then-cdb", "made-up later answer kept")
    # The meta said Salesforce already; the job keeps the request (D8).
    _answer(alice, convs["meta-says"], "gen-ms", {**_FAILURE_META, "mode": "salesforce"})
    _sf_trace("gen-ms", convs["meta-says"], alice, "200 days")
    _answer(alice, convs["kept-request"], "gen-k", moded)
    _sf_request("gen-k", alice, convs["kept-request"], "40 days", status="interrupted")
    # Holding nothing: the trace says assistant (R12); a plain chat.
    _r12_chat(alice)
    later("plain", "made-up plain answer")
    with db.connection() as con:
        con.execute("UPDATE messages SET meta = '[]'::jsonb WHERE id = ANY(%s)", (odd,))

    clean_whole = {convs[n] for n in ("a-only", "b-only", "c-only", "d-only")}
    before = set(db.salesforce_chat_ids(alice))
    assert before == set(convs.values()) - {convs["plain"]}
    for name in ("a-only", "b-only", "c-only", "d-only"):
        assert _hidden(alice, convs[name], f"made-up later answer {name}"), name
    assert _hidden(alice, convs["c-then-mark"], "made-up c answer first")
    b2 = [{"role": "user", "content": "made-up question b2"},
          {"role": "assistant", "content": "made-up toggle-off answer"}]
    assert _shown(alice, convs["mark-then-cdb"], b2)[0] is False
    assert _whole_chat_count_ids() == clean_whole

    _enable(monkeypatch, dry_run=False)
    out = retention.run_chat_retention()
    assert out["errors"] == 0 and out[retention.STAMPED] == 2  # gen-m1, gen-m2
    sf_isolation.forget_all()
    assert before - set(db.salesforce_chat_ids(alice)) == clean_whole
    for name in ("a-only", "b-only", "c-only", "d-only"):
        assert not _hidden(alice, convs[name], f"made-up later answer {name}"), name
    # One message at a time: a (c) answer before the first marked answer
    # and the (b) question are shown; (c) after it, (d) and the later
    # answer stay hidden while the chat holds.
    assert not _hidden(alice, convs["c-then-mark"], "made-up c answer first")
    assert _shown(alice, convs["mark-then-cdb"], b2)[0] is True
    for kept in ("made-up c answer after", "made-up d answer", "made-up later answer kept"):
        assert _hidden(alice, convs["mark-then-cdb"], kept), kept


def test_the_stamp_moves_past_salesforce_analytics_as_the_docs_say(monkeypatch):
    """Review R2-2. The Salesforce analytics count an answer by `meta.mode`
    (or a sql/dataset route), in the window of its own `created_at`. A
    failure record and a stopped copy stored before the guard were not
    counted; once a real run stamps them, a past window counts them. A
    detached answer (route 'sql') was counted already. docs/retention.md
    says so under "Side effects" and no longer says nothing else changes."""
    alice = _user("ret-alice")
    with db.connection() as con:
        con.execute("INSERT INTO workspaces (id, name) VALUES ('ws-ret-0001', 'Made-up')")
        con.execute("INSERT INTO workspace_memberships (workspace_id, user_id, role)"
                    " VALUES ('ws-ret-0001', %s, 'member')", (alice,))
    for gen, meta in (("gen-fail", _FAILURE_META), ("gen-stop", _STOPPED_META), ("gen-det", _DETACHED_META)):
        conv = _chat(alice, f"conv-{gen}")
        _answer(alice, conv, gen, meta)
        _sf_trace(gen, conv, alice, "200 days")
    with db.connection() as con:
        con.execute("UPDATE messages SET created_at = now() - interval '200 days'")
    now = datetime.now(timezone.utc)
    window = ("ws-ret-0001", now - timedelta(days=365), now - timedelta(days=1))

    def answers():
        return (int(analytics.salesforce_totals(*window)["answers"]),
                sum(int(r["answers"]) for r in analytics.salesforce_series(*window)))

    assert answers() == (1, 1)
    _enable(monkeypatch, dry_run=False)
    assert retention.run_chat_retention()[retention.STAMPED] == 3  # the detached one too
    assert answers() == (3, 3)
    text = _doc_text()
    assert "Nothing else changes:" not in text
    side = text.split("## Side effects the approved policy accepts", 1)[1]
    assert "The stamp changes past numbers on `/admin/analytics/salesforce`" in side
    assert "a window in the past gains up to `messages_stamped` answers" in side
    assert "a stamped answer now shows as `salesforce`" in side


def test_an_answer_of_another_chat_under_a_deleted_rows_generation_is_stamped_on_purpose(monkeypatch):
    """Review R2-3. The stamp matches the generation in any chat, with no
    chat gate. A copy in chat B of a generation that a Salesforce trace of
    chat A marks is shown by the guard in B before the run (no row of B says
    Salesforce); the run stamps and hides it, and B joins the Salesforce
    set: more, never less. Deliberate, so the code and the docs say so."""
    alice = _user("ret-alice")
    a = _chat(alice, "conv-a-0001")
    b = _chat(alice, "conv-b-0001")
    _answer(alice, a, "gen-x", _STOPPED_META, "made-up answer in A")
    _answer(alice, b, "gen-x", _STOPPED_META, "made-up copy in B")
    _sf_trace("gen-x", a, alice, "200 days")
    assert not _hidden(alice, b, "made-up copy in B") and b not in db.salesforce_chat_ids(alice)
    _enable(monkeypatch, dry_run=True)
    assert retention.run_chat_retention()[retention.STAMPED] == 2
    _enable(monkeypatch, dry_run=False)
    assert retention.run_chat_retention()[retention.STAMPED] == 2
    sf_isolation.forget_all()
    assert _hidden(alice, b, "made-up copy in B") and b in db.salesforce_chat_ids(alice)
    assert "There is deliberately NO chat gate (review R2-3)" in retention.stamp_where.__doc__
    assert "(f) an answer stored in a chat other than the deleted row's" in _doc_text()


@pytest.mark.parametrize("fails, item, known", [
    ("_count_stamps", "query_traces", False),
    ("_count", "query_traces", False),
    ("_count", "sf_query_state_expired", True),
])
def test_a_dry_run_whose_marking_item_fails_reports_the_stamp_total_as_unknown(
        monkeypatch, caplog, fails, item, known):
    """Review R2-4. When an item whose rows mark answers fails in a dry run
    (its count or its stamp count), a total without its answers would read
    as the whole count. The line says `messages_stamped=unknown`, the result
    has no key and the gauge keeps its value. A failing item that marks no
    answer leaves the total as it is."""
    alice = _user("ret-alice")
    conv = _chat(alice, "conv-dry-0001")
    _answer(alice, conv, "gen-t", _FAILURE_META)
    _sf_trace("gen-t", conv, alice, "200 days")
    other = _chat(alice, "conv-dry-0002")
    _answer(alice, other, "gen-r", _STOPPED_META)
    _sf_request("gen-r", alice, other, "40 days")
    _enable(monkeypatch, dry_run=True)
    assert retention.run_chat_retention(force=True)[retention.STAMPED] == 2
    assert _gauge("retention_candidates", table=retention.STAMPED) == 2
    real = getattr(retention, fails)

    def failing(con, it):
        if it.name == item:
            raise RuntimeError("made-up statement timeout")
        return real(con, it)

    monkeypatch.setattr(retention, fails, failing)
    with caplog.at_level(logging.INFO, logger="app.retention"):
        out = retention.run_chat_retention(force=True)
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("retention dry-run: "))
    assert out["errors"] == 1 and out["chat_requests"] == 1
    assert (item in out) is (fails == "_count_stamps")
    if known:
        assert out[retention.STAMPED] == 2 and line.endswith(" messages_stamped=2")
    else:
        assert retention.STAMPED not in out
        assert line.endswith(" messages_stamped=unknown") and "messages_stamped=1" not in line
    assert _gauge("retention_candidates", table=retention.STAMPED) == 2


# ---------------------------------------------------------------------------
# Batches, the lock and the daily gate
# ---------------------------------------------------------------------------


def test_batches_and_max_batches(monkeypatch):
    _enable(monkeypatch, dry_run=False, chat_retention_batch=2, chat_retention_max_batches=2)
    alice = _user("ret-alice")
    live = _chat(alice, "conv-live-0001")
    for i in range(5):
        _request(f"rq-{i}", alice, live, "completed", "40 days")
    calls = []
    real_batch = retention._delete_batch
    monkeypatch.setattr(
        retention, "_delete_batch",
        lambda con, statement, params: calls.append(statement) or real_batch(con, statement, params),
    )
    period = next(i for i in retention.items(retention.load_config()) if i.name == "chat_requests")

    first = retention.run_chat_retention()
    assert first["chat_requests"] == 4 and first["capped"] == 1
    assert sum(s == retention.delete_sql(period) for s in calls) == 2
    assert _count("chat_requests") == 1
    # The cap left the day open, so the next tick continues without `force`.
    second = retention.run_chat_retention()
    assert second["chat_requests"] == 1 and second["capped"] == 0
    assert _count("chat_requests") == 0
    assert retention.run_chat_retention() == {"status": "not_due"}


def test_once_per_business_day_per_mode(monkeypatch):
    _enable(monkeypatch, dry_run=True)
    assert retention.run_chat_retention()["status"] == "dry_run"
    assert retention.run_chat_retention() == {"status": "not_due"}
    _enable(monkeypatch, dry_run=False)
    assert retention.run_chat_retention()["status"] == "deleted"
    assert retention.run_chat_retention() == {"status": "not_due"}
    assert retention.run_chat_retention(force=True)["status"] == "deleted"


def test_a_lock_held_by_another_runner_skips_the_run(monkeypatch):
    _enable(monkeypatch, dry_run=False)
    alice = _user("ret-alice")
    live = _chat(alice, "conv-live-0001")
    _request("rq-old", alice, live, "completed", "40 days")
    with _raw() as other:
        other.execute("SELECT pg_advisory_lock(%s)", (retention._LOCK_KEY,))
        assert retention.run_chat_retention() == {"status": "locked"}
        assert _count("chat_requests") == 1
        other.execute("SELECT pg_advisory_unlock(%s)", (retention._LOCK_KEY,))
    assert retention.run_chat_retention()["chat_requests"] == 1
    # The run released its lock: another session can take it at once.
    with _raw() as other:
        assert other.execute("SELECT pg_try_advisory_lock(%s)", (retention._LOCK_KEY,)).fetchone()[0]
        other.execute("SELECT pg_advisory_unlock(%s)", (retention._LOCK_KEY,))


def test_two_runners_at_once_the_session_lock_lets_one_through(monkeypatch):
    """Runner A pauses BETWEEN two committed batches. Runner B, started then,
    finds the lock taken: the lock spans batches (session level), not one
    batch's transaction (C27)."""
    _enable(monkeypatch, dry_run=False, chat_retention_batch=1)
    alice = _user("ret-alice")
    live = _chat(alice, "conv-live-0001")
    for i in range(3):
        _request(f"rq-{i}", alice, live, "completed", "40 days")
    paused, release = threading.Event(), threading.Event()
    real_batch = retention._delete_batch

    def pausing(con, statement, params):
        touched = real_batch(con, statement, params)
        if "chat_requests" in statement and not paused.is_set():
            paused.set()
            release.wait(20)
        return touched

    monkeypatch.setattr(retention, "_delete_batch", pausing)
    results = {}
    runner_a = threading.Thread(target=lambda: results.setdefault("a", retention.run_chat_retention()))
    runner_a.start()
    try:
        assert paused.wait(20), "runner A never reached its first batch"
        assert _count("chat_requests") == 2  # A's first batch is committed
        with _raw() as probe:
            held = probe.execute(
                "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid"
                " WHERE l.locktype = 'advisory' AND l.granted"
                "   AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())"
                "   AND a.application_name = 'techsara-retention' AND a.state = 'idle'"
            ).fetchone()[0]
        assert held == 1  # held by A's session while A has no open transaction
        results["b"] = retention.run_chat_retention(force=True)
    finally:
        release.set()
        runner_a.join(30)
    assert results["b"] == {"status": "locked"}
    assert results["a"]["status"] == "deleted" and results["a"]["chat_requests"] == 3
    assert _count("chat_requests") == 0


# ---------------------------------------------------------------------------
# Never raises, logs, metrics, the backup reminder
# ---------------------------------------------------------------------------


def test_never_raises_when_it_cannot_connect(monkeypatch):
    _enable(monkeypatch, dry_run=False)

    def broken():
        raise RuntimeError("APP_DATABASE_URL is not set")

    monkeypatch.setattr(db, "dsn", broken)
    assert retention.run_chat_retention() == {"status": "failed"}
    assert _counter("retention_runs_total", result="fail") >= 1


def test_one_failing_rule_does_not_stop_the_others_and_keeps_the_day_open(monkeypatch):
    _enable(monkeypatch, dry_run=False)
    alice = _user("ret-alice")
    live = _chat(alice, "conv-live-0001")
    _request("rq-old", alice, live, "completed", "40 days")
    real_items = retention.items

    def with_a_broken_rule(cfg):
        broken = retention._Item("broken", "no_such_table_here", "true", ())
        return [broken, *real_items(cfg)]

    monkeypatch.setattr(retention, "items", with_a_broken_rule)
    out = retention.run_chat_retention()
    assert out["errors"] == 1 and out["chat_requests"] == 1
    assert retention.run_chat_retention()["status"] == "deleted"  # not marked done


def test_sweep_never_raises(monkeypatch):
    def explode(*_a, **_k):
        raise RuntimeError("thread pool gone")

    monkeypatch.setattr(db, "run_in_thread", explode)
    assert asyncio.run(retention.sweep()) is None


def test_sweep_runs_the_pass_in_a_thread(monkeypatch):
    _enable(monkeypatch, dry_run=True)
    assert asyncio.run(retention.sweep())["status"] == "dry_run"


def test_logs_carry_counts_never_content(monkeypatch, caplog):
    planted = "zebra-quokka planted question 4711"
    alice = _user("ret-alice")
    live = _chat(alice, "conv-live-0001")
    _trace("tr-planted-id", live, alice, "200 days", question=planted)
    _request("rq-planted-id", alice, live, "completed", "40 days", body={"text": planted})
    _request("rq-open-id", alice, live, "interrupted", "40 days", body={"text": planted})
    with caplog.at_level(logging.DEBUG, logger="app.retention"):
        _enable(monkeypatch, dry_run=True)
        retention.run_chat_retention()
        _enable(monkeypatch, dry_run=False)
        retention.run_chat_retention()
    lines = [r.getMessage() for r in caplog.records]
    dry = next(line for line in lines if line.startswith("retention dry-run: "))
    deleted = next(line for line in lines if line.startswith("retention: deleted "))
    assert dry.startswith("retention dry-run: chat_requests=1 ") and " query_traces=1 " in dry
    assert deleted.startswith("retention: deleted chat_requests=1 ") and " query_traces=1 " in deleted
    for secret in (planted, "tr-planted-id", "rq-planted-id", "rq-open-id", "conv-live-0001"):
        assert secret not in caplog.text


def test_metrics_dry_run_candidates_and_real_deletes(monkeypatch):
    alice = _user("ret-alice")
    live = _chat(alice, "conv-live-0001")
    _trace("tr-a", live, alice, "200 days")
    _trace("tr-b", live, alice, "300 days")
    before = _counter("retention_deleted_total", table="query_traces")
    _enable(monkeypatch, dry_run=True)
    retention.run_chat_retention()
    assert _gauge("retention_candidates", table="query_traces") == 2
    assert _counter("retention_deleted_total", table="query_traces") == before
    _enable(monkeypatch, dry_run=False)
    retention.run_chat_retention()
    assert _counter("retention_deleted_total", table="query_traces") == before + 2
    assert _counter("retention_runs_total", result="ok") >= 2


@pytest.mark.parametrize("age_days, warns", [(31, True), (30, False), (5, False)])
def test_the_backup_reminder(monkeypatch, caplog, age_days, warns):
    taken = (retention.business_today() - timedelta(days=age_days)).isoformat()
    _enable(monkeypatch, dry_run=True, chat_retention_backup_date=taken)
    with caplog.at_level(logging.WARNING, logger="app.retention"):
        retention.run_chat_retention()
    assert ("pre-prune backup of " + taken in caplog.text) is warns


def test_an_unreadable_backup_date_is_one_warning_and_the_run_goes_on(monkeypatch, caplog):
    _enable(monkeypatch, dry_run=True, chat_retention_backup_date="last tuesday")
    with caplog.at_level(logging.WARNING, logger="app.retention"):
        assert retention.run_chat_retention()["status"] == "dry_run"
    assert "not a YYYY-MM-DD date" in caplog.text


# ---------------------------------------------------------------------------
# docs/retention.md: the rollback SQL, run exactly as the document gives it
# ---------------------------------------------------------------------------


def _doc_block(marker: str) -> str:
    with open(_DOCS, encoding="utf-8") as fh:
        text = fh.read()
    found = re.search(
        rf"<!-- {marker} -->\s*```sql\n(.*?)```\s*<!-- /{marker} -->", text, flags=re.S
    )
    assert found, f"docs/retention.md has no {marker} block"
    return found.group(1)


def _run_block(con: psycopg.Connection, sql: str) -> None:
    for statement in (s.strip() for s in sql.split(";")):
        if statement:
            con.execute(statement)


def test_the_documented_restore_brings_back_live_chats_rows_only(monkeypatch):
    _enable(monkeypatch, dry_run=False)
    alice = _user("ret-alice")
    bob = _user("ret-bob")
    keep = _chat(alice, "conv-keep-0001")
    later_deleted = _chat(alice, "conv-later-0001")
    _chat(bob, "conv-bob-0001")
    bare = f"u{alice}-sess-1"
    _trace("tr-keep", keep, alice, "200 days", events=3)
    _trace("tr-later", later_deleted, alice, "200 days", events=2)
    _trace("tr-bare", bare, alice, "200 days", events=1)
    _trace("tr-bob", "conv-bob-0001", bob, "200 days", events=1)
    _trace("tr-young", keep, alice, "1 day", events=1)
    _request("rq-keep", alice, keep, "completed", "40 days")
    _request("rq-later", alice, later_deleted, "completed", "40 days")
    _request("rq-bare", alice, bare, "completed", "40 days")
    _request("rq-bob", bob, "conv-bob-0001", "completed", "40 days")

    with _raw() as con:
        con.execute("DROP SCHEMA IF EXISTS retention_restore CASCADE")
    try:
        with _raw() as con:
            _run_block(con, _doc_block("retention-restore-staging"))
            # What step 2 (the dump loaded with `sed`) puts in the staging
            # tables: every row as it was before the run.
            for table in ("query_traces", "query_trace_events", "chat_requests"):
                con.execute(f"INSERT INTO retention_restore.{table} SELECT * FROM public.{table}")

        out = retention.run_chat_retention()
        assert (out["query_traces"], out["chat_requests"]) == (4, 4)
        db.delete_conversation(alice, later_deleted)  # the person deletes a chat after the backup
        with db.connection() as con:
            con.execute("DELETE FROM users WHERE id = %s", (bob,))  # and an account goes

        with _raw() as con:
            # The insert block holds BEGIN ... COMMIT: run it whole.
            con.execute(_doc_block("retention-restore-insert"))
            _run_block(con, _doc_block("retention-restore-cleanup"))
            assert con.execute("SELECT to_regnamespace('retention_restore')").fetchone()[0] is None
    finally:
        with _raw() as con:
            con.execute("DROP SCHEMA IF EXISTS retention_restore CASCADE")

    assert _ids("query_traces", "trace_id") == {"tr-keep", "tr-bare", "tr-young"}
    assert _ids("chat_requests", "intent_id") == {"rq-keep", "rq-bare"}
    with db.connection() as con:
        rows = con.execute("SELECT trace_id, count(*) AS n FROM query_trace_events GROUP BY 1").fetchall()
    assert {r["trace_id"]: r["n"] for r in rows} == {"tr-keep": 3, "tr-bare": 1, "tr-young": 1}


def test_the_documented_restore_never_files_rows_under_another_users_chat(monkeypatch):
    """Review DS-2. A chat id is chosen by the client, so a deleted chat's id
    can be claimed again by someone else. The restore puts a row back only
    under a chat its own user still owns, or under that user's own bare-API
    key; a trace whose user was deleted (user_id NULL) is not put back."""
    _enable(monkeypatch, dry_run=False)
    alice = _user("ret-alice")
    carol = _user("ret-carol")
    keep = _chat(alice, "conv-keep-0001")
    reclaimed = _chat(alice, "conv-reclaimed-0001")
    bare = f"u{alice}-sess-1"
    others_bare = f"u{carol}-sess-1"
    _trace("tr-keep", keep, alice, "200 days", events=1)
    _trace("tr-bare", bare, alice, "200 days", events=1)
    _trace("tr-reclaimed", reclaimed, alice, "200 days", events=2,
           question="ret-alice private made-up question")
    _trace("tr-null-user", keep, None, "200 days", events=1)
    _trace("tr-others-bare", others_bare, alice, "200 days", events=1)
    _request("rq-keep", alice, keep, "completed", "40 days")
    _request("rq-bare", alice, bare, "completed", "40 days")
    _request("rq-reclaimed", alice, reclaimed, "completed", "40 days")
    _request("rq-others-bare", alice, others_bare, "completed", "40 days")

    with _raw() as con:
        con.execute("DROP SCHEMA IF EXISTS retention_restore CASCADE")
    try:
        with _raw() as con:
            _run_block(con, _doc_block("retention-restore-staging"))
            for table in ("query_traces", "query_trace_events", "chat_requests"):
                con.execute(f"INSERT INTO retention_restore.{table} SELECT * FROM public.{table}")

        out = retention.run_chat_retention()
        assert (out["query_traces"], out["chat_requests"]) == (5, 4)
        # Alice deletes the chat; Carol's client then claims the same id.
        assert db.delete_conversation(alice, reclaimed)
        _chat(carol, reclaimed)

        with _raw() as con:
            con.execute(_doc_block("retention-restore-insert"))
            _run_block(con, _doc_block("retention-restore-cleanup"))
    finally:
        with _raw() as con:
            con.execute("DROP SCHEMA IF EXISTS retention_restore CASCADE")

    assert _ids("query_traces", "trace_id") == {"tr-keep", "tr-bare"}
    assert _ids("chat_requests", "intent_id") == {"rq-keep", "rq-bare"}
    with db.connection() as con:
        events = {r["trace_id"] for r in con.execute("SELECT trace_id FROM query_trace_events")}
    assert events == {"tr-keep", "tr-bare"}


def test_the_restore_section_names_both_backups():
    """Review DS-7: deleted rows can come back from the pre-prune backup or
    from a whole-database backup taken before the run (B1)."""
    with open(_DOCS, encoding="utf-8") as fh:
        text = fh.read()
    assert "nothing else\nholds them" not in text
    section = text.split("## Rollback", 1)[1]
    assert "or from a\nwhole-database backup taken before the run (B1)" in section


# ---------------------------------------------------------------------------
# docs/retention.md: the shell blocks, run on a temporary folder. `docker`
# is a shell function printing made-up output; a stand-in that fails is put
# first on PATH as well, so nothing can reach a real docker.
# ---------------------------------------------------------------------------

_FAKE_DOCKER = r"""
docker() {
  printf 'COPY public.query_traces (trace_id) FROM stdin;\n%s\n\\.\n' "$FAKE_TAG"
  printf 'COPY public.query_trace_events (id) FROM stdin;\n\\.\n'
  printf 'COPY public.chat_requests (intent_id) FROM stdin;\n\\.\n'
}
"""


def _doc_bash_blocks(heading: str) -> list:
    """Every ```bash block of the docs section that starts at `heading`."""
    with open(_DOCS, encoding="utf-8") as fh:
        text = fh.read()
    section = text.split(heading + "\n", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"```bash\n(.*?)```", section, flags=re.S)


def _shell(tmp_path, script: str, *, cwd=None, **env):
    stand_in = tmp_path / "stand-in-bin"
    stand_in.mkdir(exist_ok=True)
    blocker = stand_in / "docker"
    blocker.write_text("#!/bin/sh\necho 'real docker reached' >&2\nexit 97\n")
    blocker.chmod(0o700)
    base = {"PATH": f"{stand_in}:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(tmp_path / "home")}
    base.update(env)
    return subprocess.run(
        ["bash", "-c", _FAKE_DOCKER + script], cwd=str(cwd or tmp_path), env=base,
        capture_output=True, text=True, timeout=30,
    )


def test_the_documented_backup_block_never_overwrites_the_pre_prune_dump(tmp_path):
    """Review DS-1. The pre-prune dump is the only rollback copy: a second
    run of the block on the same day (after a real run deleted the rows)
    refuses and leaves the first dump as it was."""
    (block,) = _doc_bash_blocks("## Before the first real run: the backup")
    first = _shell(tmp_path, block, FAKE_TAG="PRE-PRUNE-ROWS")
    assert first.returncode == 0, first.stderr
    assert "real docker reached" not in first.stderr
    folders = list((tmp_path / "home" / "Documents" / "project" / "backups").glob("retention_*"))
    assert len(folders) == 1
    dump = folders[0] / "retention_tables.sql"
    assert stat.S_IMODE(folders[0].stat().st_mode) == 0o700
    assert stat.S_IMODE(dump.stat().st_mode) == 0o600
    assert "PRE-PRUNE-ROWS" in dump.read_text()

    second = _shell(tmp_path, block, FAKE_TAG="POST-PRUNE-ROWS")
    assert second.returncode != 0
    assert "refusing" in second.stderr and "real docker reached" not in second.stderr
    assert "PRE-PRUNE-ROWS" in dump.read_text()
    assert "POST-PRUNE-ROWS" not in dump.read_text()
    assert stat.S_IMODE(dump.stat().st_mode) == 0o600

    # The redirect refuses on its own too (noclobber), should the check above
    # ever be edited away.
    assert "set -o noclobber" in block
    unguarded = block.replace('if [ -e "$dir/retention_tables.sql" ]; then', "if false; then")
    third = _shell(tmp_path, unguarded, FAKE_TAG="POST-PRUNE-ROWS")
    assert third.returncode != 0
    assert "PRE-PRUNE-ROWS" in dump.read_text()


def _old_folder(path, *, manifest: bool, age_days: int):
    path.mkdir(parents=True)
    if manifest:
        (path / "manifest.json").write_text("{}")
    (path / "made-up.txt").write_text("made-up")
    subprocess.run(["touch", "-d", f"{age_days} days ago", str(path)], check=True)


def test_the_documented_b1_delete_takes_only_old_backups_the_script_made(tmp_path):
    """Review DS-6. Only an old folder that carries the script's
    manifest.json goes; any other folder there, a partial run or a recent
    backup stays. Without BACKUP_DEST the commands refuse."""
    delete_block = _doc_bash_blocks("## Whole-database backups (B1)")[0]
    repo = tmp_path / "repo"
    backups = repo / "backups"
    _old_folder(backups / "20261001T000000Z", manifest=True, age_days=40)
    _old_folder(backups / "20261002T000000Z.partial", manifest=False, age_days=40)
    _old_folder(backups / "2025-made-up-photos", manifest=False, age_days=400)
    _old_folder(backups / "20261120T000000Z", manifest=True, age_days=5)

    done = _shell(tmp_path, delete_block, cwd=repo)
    assert done.returncode == 0, done.stderr
    assert "20261001T000000Z" in done.stdout  # the -print line lists it first
    assert sorted(p.name for p in backups.iterdir()) == sorted([
        "20261002T000000Z.partial", "20261120T000000Z", "2025-made-up-photos",
    ])

    rm_line = next(line for line in delete_block.splitlines() if "-exec rm -rf" in line)
    _old_folder(backups / "20261003T000000Z", manifest=True, age_days=40)
    refused = _shell(tmp_path, rm_line, cwd=repo)
    assert refused.returncode != 0 and "set BACKUP_DEST" in refused.stderr
    assert (backups / "20261003T000000Z").exists()


def test_the_documented_mode_repair_makes_whole_database_backups_private(tmp_path):
    """Review DS-4 and DS2-4. The script sets `umask 077` itself, so the doc
    no longer tells the operator to wrap it; it states the modes, says that
    existing backups keep their old ones, and its one-time repair command
    makes existing dumps 700/600."""
    blocks = _doc_bash_blocks("## Whole-database backups (B1)")
    with open(_DOCS, encoding="utf-8") as fh:
        text = fh.read()
    script = os.path.join(os.path.dirname(_DOCS), "..", "scripts", "backup-knowledge.sh")
    with open(script, encoding="utf-8") as fh:
        assert "\numask 077\n" in fh.read()
    section = text.split("## Whole-database backups (B1)\n", 1)[1].split("\n## ", 1)[0]
    assert "(umask 077;" not in text and "does not set its own umask" not in text
    assert "the script sets `umask 077` itself" in section
    assert "It\ndoes not change what already exists" in section
    repair = next(block for block in blocks if "chmod -R go-rwx" in block)
    repo = tmp_path / "repo"
    stamp = repo / "backups" / "20261001T000000Z"
    stamp.mkdir(parents=True)
    dump = stamp / "postgres.dump"
    dump.write_text("made-up")
    for path, mode in ((repo / "backups", 0o775), (stamp, 0o775), (dump, 0o664)):
        path.chmod(mode)
    done = _shell(tmp_path, repair, cwd=repo)
    assert done.returncode == 0, done.stderr
    assert stat.S_IMODE((repo / "backups").stat().st_mode) == 0o700
    assert stat.S_IMODE(stamp.stat().st_mode) == 0o700
    assert stat.S_IMODE(dump.stat().st_mode) == 0o600
