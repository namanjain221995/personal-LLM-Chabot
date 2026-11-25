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
from datetime import timedelta

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Json

from app import db, metrics, retention, sf_memory_store
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
    assert _ids("query_traces", "trace_id") == survivors["traces"]
    assert _ids("chat_requests", "intent_id") == survivors["requests"]
    assert _ids("sf_query_state", "generation_id") == {"g2", "g3"}
    # Nothing qualifies any more: a second dry run counts zero everywhere
    # but the kept resumable rows.
    _enable(monkeypatch, dry_run=True, chat_retention_old_route_orphans=True)
    again = retention.run_chat_retention(force=True)
    assert {k: v for k, v in again.items() if k not in ("status", "capped", "errors")} == {
        **{name: 0 for name in expected}, "chat_requests_resumable_old": 2,
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

    first = retention.run_chat_retention()
    assert first["chat_requests"] == 4 and first["capped"] == 1
    assert sum("DELETE FROM chat_requests" in s and "NOT EXISTS" not in s for s in calls) == 2
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
    assert "retention dry-run: query_traces=1 " in caplog.text
    assert "retention: deleted query_traces=1 " in caplog.text
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
    """Review DS-4 (the documentation half; the script's own umask is a
    separate change). The doc states the modes, tells how to run the script
    under umask 077, and its repair command makes existing dumps 700/600."""
    blocks = _doc_bash_blocks("## Whole-database backups (B1)")
    with open(_DOCS, encoding="utf-8") as fh:
        text = fh.read()
    assert "(umask 077; scripts/backup-knowledge.sh)" in text
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
