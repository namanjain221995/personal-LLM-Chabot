"""db.prune_api_platform and db.purge_api_content_batched — the V36 durable-run
log (no-timeout /v1, 2026-09-13).

Real PostgreSQL. What is pinned:

- events go for terminal runs past PUBLIC_API_EVENT_RETENTION_S, for terminal
  runs of a project that is no longer active, and for expired rows that are
  settled or not resumable — and NEVER for an open resumable run, leased or
  not: a live writer's resume point, a run suspended for a deploy, a
  background job still queued, a live owner whose lease lapsed (T1 review,
  2026-09-14: all of the last three lost log, spec and row);
- an expired response row is deleted only once no event of it remains, so its
  cascade never walks a log, and an open resumable row is never deleted for
  its expiry;
- specs and blob references go for terminal and expired prunable runs and stay
  for every open resumable run;
- a response with 1,000,000 events is purged and its workspace deleted with no
  statement anywhere near the 15 s statement_timeout; and the same cascade
  WITHOUT the purge deletes the whole log in ONE statement where the purge
  never deletes more than a batch in one (a row count, not a timing ratio:
  the review measured the old 10x timing assertion failing 2 runs in 4).
"""
from __future__ import annotations

import contextlib
import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app import db

WS = "ws-prune"


@pytest.fixture()
def world():
    with db.connection() as con:
        con.execute("INSERT INTO workspaces (id, name) VALUES (%s, 'Prune')", (WS,))
    owner = int(db.create_user("prune-owner", "hash"))
    project = db.create_api_project(WS, "Durable", "live", created_by=owner)
    return {"project": project, "owner": owner}


def _response(world, request_id: str, **fields) -> dict:
    row = db.create_api_response(world["project"]["id"], WS, "techsara-35b", request_id)
    if fields:
        row = db.update_api_response(row["id"], world["project"]["id"], **fields)
    return row


def _events(response_id: str, n: int) -> None:
    with db.connection() as con:
        con.execute(
            "INSERT INTO api_response_events (response_id, sequence_number, event, data) "
            "SELECT %s, g, 'response.output_text.delta', '{\"delta\": \"x\"}'::jsonb FROM generate_series(1, %s) g",
            (response_id, n),
        )


def _spec_and_blob(response_id: str) -> None:
    with db.connection() as con:
        con.execute("INSERT INTO api_response_requests (response_id, spec) VALUES (%s, '{}')", (response_id,))
        con.execute("INSERT INTO api_response_blobs (response_id, sha256, bytes) VALUES (%s, 'ab', 10)",
                    (response_id,))


def _count(table: str, response_id: str) -> int:
    with db.connection() as con:
        return int(con.execute(f"SELECT count(*) AS n FROM {table} WHERE response_id = %s",
                               (response_id,)).fetchone()["n"])


def _exists(response_id: str) -> bool:
    with db.connection() as con:
        return con.execute("SELECT 1 FROM api_responses WHERE id = %s", (response_id,)).fetchone() is not None


def test_prune_takes_only_the_logs_retention_says_it_may(world, monkeypatch):
    monkeypatch.setattr(db.settings, "public_api_event_retention_s", 3600.0)
    now = datetime.now(timezone.utc)
    old_done = _response(world, "r1", status="completed", completed_at=now - timedelta(hours=2))
    fresh_done = _response(world, "r2", status="completed", completed_at=now - timedelta(minutes=5))
    leased_open = _response(world, "r3", status="in_progress", resumable=True, lease_owner="proc-a",
                            lease_expires_at=now + timedelta(seconds=60), expires_at=now - timedelta(days=1))
    orphan_expired = _response(world, "r4", status="in_progress", resumable=True,
                               expires_at=now - timedelta(days=1))
    expired_done = _response(world, "r5", status="failed", completed_at=now - timedelta(minutes=1),
                             expires_at=now - timedelta(minutes=1))
    legacy_open = _response(world, "r6", status="in_progress", expires_at=now - timedelta(days=1))
    for row in (old_done, fresh_done, leased_open, orphan_expired, expired_done, legacy_open):
        _events(row["id"], 12_000)
        _spec_and_blob(row["id"])

    counts = db.prune_api_platform()

    assert _count("api_response_events", old_done["id"]) == 0, "terminal past retention"
    assert _count("api_response_events", fresh_done["id"]) == 12_000, "inside retention: replayable"
    assert _count("api_response_events", leased_open["id"]) == 12_000, "a live writer's log is never touched"
    assert _count("api_response_events", orphan_expired["id"]) == 12_000, "an open resumable run is not ours"
    assert _count("api_response_events", expired_done["id"]) == 0, "expired and settled"
    assert _count("api_response_events", legacy_open["id"]) == 0, "expired and never resumable"
    assert counts["api_response_events"] == 36_000
    # Specs and blob references: terminal and expired-prunable go; every open resumable run keeps its own.
    for row in (old_done, fresh_done, expired_done, legacy_open):
        assert _count("api_response_requests", row["id"]) == 0 and _count("api_response_blobs", row["id"]) == 0
    for row in (leased_open, orphan_expired):
        assert _count("api_response_requests", row["id"]) == 1
        assert _count("api_response_blobs", row["id"]) == 1
    # Rows: the expired prunable ones went once their logs were gone.
    assert not _exists(expired_done["id"]) and not _exists(legacy_open["id"])
    assert _exists(leased_open["id"]) and _exists(orphan_expired["id"])
    assert counts["api_responses"] == 2


def test_an_expired_open_resumable_run_keeps_its_log_spec_and_row_whatever_its_lease(world):
    """The review's three reachable cases (T1 review, 2026-09-14): with
    retention_days = 1 each is past `expires_at` with no live lease, and each
    lost its log, its spec and its row in one prune — a 404 on re-attach."""
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=1)
    suspended = _response(world, "s1", status="in_progress", resumable=True, expires_at=past,
                          suspend_reason="restart", suspended_at=now - timedelta(seconds=5))
    lapsed = _response(world, "s2", status="in_progress", resumable=True, expires_at=past,
                       lease_owner="proc-a", lease_expires_at=now - timedelta(seconds=1))
    queued = _response(world, "s3", status="queued", resumable=True, expires_at=past,
                       enqueued_at=now - timedelta(hours=30))
    with db.connection() as con:
        con.execute("UPDATE api_responses SET background = true WHERE id = %s", (queued["id"],))
    for row in (suspended, lapsed):
        _events(row["id"], 5_000)
    for row in (suspended, lapsed, queued):
        _spec_and_blob(row["id"])

    counts = db.prune_api_platform()

    assert counts["api_response_events"] == 0 and counts["api_responses"] == 0
    assert counts["api_response_requests"] == 0 and counts["api_response_blobs"] == 0
    for row in (suspended, lapsed):
        assert _count("api_response_events", row["id"]) == 5_000
    for row in (suspended, lapsed, queued):
        assert _exists(row["id"])
        assert _count("api_response_requests", row["id"]) == 1 and _count("api_response_blobs", row["id"]) == 1

    # Settled later by the runner, the same rows are retention's to take.
    for row in (suspended, lapsed, queued):
        db.update_api_response(row["id"], world["project"]["id"], status="cancelled")
    counts = db.prune_api_platform()
    assert counts["api_response_events"] == 10_000 and counts["api_responses"] == 3


def test_an_expired_row_waits_for_its_log_to_be_pruned_before_it_is_deleted(world):
    now = datetime.now(timezone.utc)
    row = _response(world, "r1", status="completed", completed_at=now - timedelta(hours=2),
                    expires_at=now - timedelta(minutes=1))
    _events(row["id"], 30_000)
    # max_batches=1 gives the durable half 4 statements: one due-list read and
    # three 5,000-row deletes — the log cannot be finished in this run.
    first = db.prune_api_platform(max_batches=1)
    assert first["api_response_events"] == 15_000
    assert first["api_responses"] == 0
    assert _exists(row["id"]), "a row with a log left is not cascaded"
    second = db.prune_api_platform()
    assert second["api_response_events"] == 15_000
    assert second["api_responses"] == 1
    assert not _exists(row["id"])


def test_a_disabled_projects_finished_logs_go_at_once_and_its_open_runs_logs_stay(world):
    now = datetime.now(timezone.utc)
    done = _response(world, "r1", status="failed", completed_at=now - timedelta(seconds=10))
    running = _response(world, "r2", status="in_progress", resumable=True, lease_owner="proc-a",
                        lease_expires_at=now + timedelta(seconds=60))
    _events(done["id"], 100)
    _events(running["id"], 100)
    with db.connection() as con:
        con.execute("UPDATE api_projects SET status = 'disabled' WHERE id = %s", (world["project"]["id"],))
    db.prune_api_platform()
    assert _count("api_response_events", done["id"]) == 0
    assert _count("api_response_events", running["id"]) == 100


def test_purge_content_batched_clears_logs_specs_and_blobs_of_the_named_projects_only(world):
    other_owner = int(db.create_user("prune-other", "hash"))
    other = db.create_api_project(WS, "Other", "live", created_by=other_owner)
    mine = _response(world, "r1", status="in_progress")
    theirs = db.create_api_response(other["id"], WS, "techsara-35b", "r2")
    for row in (mine, theirs):
        _events(row["id"], 7_000)
        _spec_and_blob(row["id"])
    counts = db.purge_api_content_batched([world["project"]["id"]], batch_size=5000)
    assert counts == {"api_response_events": 7_000, "api_response_requests": 1, "api_response_blobs": 1}
    assert _count("api_response_events", mine["id"]) == 0
    assert _count("api_response_events", theirs["id"]) == 7_000
    assert db.purge_api_content_batched([]) == {"api_response_events": 0, "api_response_requests": 0,
                                                "api_response_blobs": 0}


def _timed_connections(monkeypatch, durations: list):
    real = db.connection

    @contextlib.contextmanager
    def timed():
        started = time.monotonic()
        with real() as con:
            yield con
        durations.append(time.monotonic() - started)

    monkeypatch.setattr(db, "connection", timed)


def test_a_million_event_log_is_purged_and_its_workspace_deleted_with_no_long_statement(world, monkeypatch):
    row = _response(world, "r-million", status="completed", completed_at=datetime.now(timezone.utc))
    # Set-up outside the pool's 15 s statement_timeout.
    with psycopg.connect(db.dsn(), autocommit=True) as con:
        con.execute("SET statement_timeout = 0")
        con.execute(
            "INSERT INTO api_response_events (response_id, sequence_number, event, data) "
            "SELECT %s, g, 'response.output_text.delta', '{\"delta\": \"x\"}'::jsonb "
            "FROM generate_series(1, 1000000) g",
            (row["id"],),
        )
    assert _count("api_response_events", row["id"]) == 1_000_000
    durations: list = []
    _timed_connections(monkeypatch, durations)
    started = time.monotonic()
    counts = db.purge_api_content_batched([world["project"]["id"]])
    with db.connection() as con:
        con.execute("DELETE FROM workspaces WHERE id = %s", (WS,))
    total = time.monotonic() - started
    assert counts["api_response_events"] == 1_000_000
    assert not _exists(row["id"])
    longest = max(durations)
    print(f"purge+delete: {len(durations)} statements, longest {longest:.3f}s, total {total:.1f}s")
    assert longest < 15.0
    assert longest < 2.0, "each bounded statement is far below the timeout, not merely under it"


def test_without_the_purge_the_cascade_deletes_the_whole_log_in_one_statement_and_the_purge_never_does(
    world, monkeypatch
):
    """Mutation evidence for the test above, by ROW COUNT per statement rather
    than a timing ratio (T1 review, 2026-09-14: `one_statement > 10 *
    longest_bounded` measured 9.7x and failed 2 runs in 4 on a fsync-off
    database, which would turn the one test-then-deploy Pipeline red). The
    purge never deletes more than `batch_size` events in one statement; the
    workspace cascade without it deletes all of them in one."""
    def fill(response_id: str, n: int) -> None:
        with psycopg.connect(db.dsn(), autocommit=True) as con:
            con.execute("SET statement_timeout = 0")
            con.execute(
                "INSERT INTO api_response_events (response_id, sequence_number, event, data) "
                "SELECT %s, g, 'response.output_text.delta', '{\"delta\": \"x\"}'::jsonb "
                "FROM generate_series(1, %s) g",
                (response_id, n),
            )

    def events_total() -> int:
        with psycopg.connect(db.dsn(), autocommit=True) as con:
            return int(con.execute("SELECT count(*) FROM api_response_events").fetchone()[0])

    purged = _response(world, "r-purged", status="completed", completed_at=datetime.now(timezone.utc))
    fill(purged["id"], 23_456)
    per_statement: list = []
    real = db.connection

    @contextlib.contextmanager
    def counting():
        before = events_total()
        with real() as con:
            yield con
        per_statement.append(before - events_total())

    monkeypatch.setattr(db, "connection", counting)
    counts = db.purge_api_content_batched([world["project"]["id"]], batch_size=5_000)
    monkeypatch.undo()
    assert counts["api_response_events"] == 23_456
    deleting = [n for n in per_statement if n > 0]
    assert sum(deleting) == 23_456
    assert max(deleting) <= 5_000 and len(deleting) == 5, deleting

    cascaded = _response(world, "r-cascaded", status="completed", completed_at=datetime.now(timezone.utc))
    fill(cascaded["id"], 23_456)
    with psycopg.connect(db.dsn(), autocommit=True) as con:
        con.execute("SET statement_timeout = 0")
        before = int(con.execute("SELECT count(*) FROM api_response_events").fetchone()[0])
        con.execute("DELETE FROM workspaces WHERE id = %s", (WS,))
        after = int(con.execute("SELECT count(*) FROM api_response_events").fetchone()[0])
    print(f"purge: {len(deleting)} deleting statements, largest {max(deleting)} rows; "
          f"cascade: one statement, {before - after} rows")
    assert not _exists(cascaded["id"])
    assert before - after == 23_456, "the cascade takes the whole log in the one DELETE"
