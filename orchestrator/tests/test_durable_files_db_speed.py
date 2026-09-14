"""The durable /v1 log and the Files runner, database-speed round (2026-09-14).

Real PostgreSQL. What is pinned — each a measured cost of dev 6cc1d6f, each
without giving up a durability, resume or lease rule:

- `durable_store.append` writes a whole flush in ONE statement (it was
  1 + 4N: SELECT 1, then SAVEPOINT / FOR SHARE / INSERT / RELEASE per run),
  and still: takes a row lock a claim's FOR UPDATE waits for (rule 2); loses
  only the job whose sequence numbers conflict, with none of that job's
  rows committed (rule 3); loses the job whose lease moved; isolates a job
  the server refuses;
- a long answer is stored once: the terminal records keep a reference, and
  every reader (`list_events`, `poll_many`, a follower replaying from the
  log) gets records equal to what was written;
- `durable.outcome_from_log` aggregates in SQL instead of reading every row;
- `purge_events` finds due logs through the event primary key (no
  sequential scan of the log) and never purges an open resumable run's log
  for its expiry;
- the durable dispatcher backs off when idle and a launch still wakes it;
- a Files deferral wakes its lane when it falls due, not at the idle poll.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import threading
import time
import uuid

import psycopg
import pytest

from app import db
from app.apifiles import events as file_events, ids as file_ids, jobs, schema as file_schema, storage
from app.publicapi import blobs, capacity, durable, durable_store, events
from tests.publicapi_fake_engine import (
    FakeController,
    FakeMainEngine,
    collect,
    expected_text,
    fast_durable_settings,
    make_tenant,
    set_setting,
    spec as make_spec,
)

OWNER = "owner-a"


@pytest.fixture(autouse=True)
def _durable(monkeypatch, tmp_path):
    fast_durable_settings(monkeypatch)
    durable_store.ensure_schema()
    capacity.reset_for_tests()
    set_setting(monkeypatch, "PUBLIC_API_BLOB_DIR", str(tmp_path / "blobs"))
    monkeypatch.setattr(blobs, "min_free_disk_bytes", lambda: 0)
    yield


async def _allow_all(keys, model=None):
    return set(keys)


def _runtime(tmp_path, owner="A"):
    runtime = durable.Runtime()
    runtime.configure(
        owner=f"test-{owner}", view=FakeController(time.monotonic, state="READY"),
        authoriser=_allow_all, blob_store=blobs.BlobStore(tmp_path / "blobs"),
    )
    runtime.witnesses_enabled = False
    return runtime


class _Counting:
    """A pooled connection that counts the statements sent through it."""

    def __init__(self, con, sink):
        self._con = con
        self._sink = sink

    def execute(self, query, *args, **kwargs):
        self._sink.append(str(query))
        return self._con.execute(query, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._con, name)


@pytest.fixture
def statements(monkeypatch):
    sink = []
    real = db.connection

    @contextlib.contextmanager
    def counting():
        with real() as con:
            yield _Counting(con, sink)

    monkeypatch.setattr(db, "connection", counting)
    return sink


def _durable_run(tenant, owner=OWNER):
    row = db.create_api_response(tenant.project["id"], tenant.workspace_id, "techsara-35b", "req-" + uuid.uuid4().hex)
    durable_store.mark_durable(row["id"], dialect="responses", item_id="msg_1", engine="main", owner=owner, lease_ttl_s=600)
    return row["id"]


def _frame(record):
    """A rendered SSE frame as (event name, parsed data)."""
    frame = durable.render_responses_frame(record)
    name, data = frame.split("\n", 1)
    return name, json.loads(data[len("data: "):].strip())


def _delta(seq, text="w "):
    return (seq, durable_store.DELTA_EVENT, {"type": durable_store.DELTA_EVENT, "sequence_number": seq,
                                             "delta": text, durable_store.TOKENS_KEY: 1})


# ---------------------------------------------------------------- append --


def test_one_flush_of_five_runs_is_one_statement(statements):
    tenant = make_tenant()
    runs = [_durable_run(tenant) for _ in range(5)]
    statements.clear()
    result = durable_store.append(OWNER, {rid: [_delta(1), _delta(2)] for rid in runs})
    assert sorted(result.committed) == sorted(runs) and not result.lost
    assert all(result.committed[rid] == [1, 2] for rid in runs)
    assert len(statements) == 1, statements


def test_a_split_brain_loses_only_that_run_and_commits_none_of_its_batch():
    tenant = make_tenant()
    loser, winner, moved = _durable_run(tenant), _durable_run(tenant), _durable_run(tenant)
    assert durable_store.append(OWNER, {loser: [_delta(1)]}).committed == {loser: [1]}
    with db.connection() as con:
        con.execute("UPDATE api_responses SET lease_owner = 'owner-b' WHERE id = %s", (moved,))
    # seq 1 of `loser` is already in the log (another owner wrote it): 2 and 3
    # are new, but none of the three may commit.
    result = durable_store.append(OWNER, {
        loser: [_delta(1), _delta(2), _delta(3)], winner: [_delta(1), _delta(2)], moved: [_delta(1)],
    })
    assert result.committed == {winner: [1, 2]}
    assert result.lost == {loser, moved}
    assert [r[0] for r in durable_store.list_events(loser)] == [1]
    assert durable_store.list_events(moved) == []
    assert [r[0] for r in durable_store.list_events(winner)] == [1, 2]


def test_a_record_the_server_refuses_fails_only_its_own_run():
    tenant = make_tenant()
    bad, good = _durable_run(tenant), _durable_run(tenant)
    # sequence_number 0 violates api_response_events_seq: a server-side error.
    result = durable_store.append(OWNER, {bad: [_delta(0)], good: [_delta(1)]})
    assert result.committed == {good: [1]} and result.lost == {bad}
    assert durable_store.list_events(bad) == []


def test_an_append_waits_for_a_claims_row_lock_before_it_writes():
    """Rule 2 from the other side: while a claim holds FOR UPDATE on the row,
    the append must not have written (it waits for the row lock)."""
    tenant = make_tenant()
    rid = _durable_run(tenant)
    locker = psycopg.connect(db.dsn())
    try:
        locker.execute("SELECT id FROM api_responses WHERE id = %s FOR UPDATE", (rid,))
        done = {}

        def flush():
            done["result"] = durable_store.append(OWNER, {rid: [_delta(1)]})

        thread = threading.Thread(target=flush)
        thread.start()
        thread.join(0.5)
        assert thread.is_alive(), "append did not wait for the claim's row lock"
        with psycopg.connect(db.dsn()) as reader:
            assert reader.execute("SELECT count(*) FROM api_response_events WHERE response_id = %s",
                                  (rid,)).fetchone()[0] == 0
        locker.rollback()
        thread.join(10)
        assert done["result"].committed == {rid: [1]}
    finally:
        locker.close()


# -------------------------------------------------------- terminal text --


def _terminal_records(rid, first_seq, text):
    item = "msg_1"
    from app.publicapi import models

    payloads = (
        (events.RESPONSE_OUTPUT_TEXT_DONE, {"item_id": item, "output_index": 0, "content_index": 0, "text": text}),
        (events.RESPONSE_CONTENT_PART_DONE, events.content_part_done_payload(item, text, [{"type": "file_citation", "index": 3}])),
        (events.RESPONSE_OUTPUT_ITEM_DONE, events.output_item_done_payload(item, text, [])),
        (events.RESPONSE_COMPLETED, {"response": models.Response(
            id=rid, created_at=1, status="completed", model="techsara-35b",
            output=[models.OutputMessage.of(text)], usage=None, max_output_tokens=10,
            incomplete_details=None, error=None).to_wire()}),
    )
    return [(first_seq + i, name, {"type": name, "sequence_number": first_seq + i, **data})
            for i, (name, data) in enumerate(payloads)]


def test_a_long_answer_is_stored_once_and_every_reader_gets_the_records_it_was_given():
    tenant = make_tenant()
    rid = _durable_run(tenant)
    pieces = [f" piece{i}\x00é" for i in range(400)]  # a NUL is stripped from the log, as from any record
    text = "".join(pieces)
    assert len(text) > durable_store.TERMINAL_TEXT_INLINE_MAX_CHARS
    deltas = [_delta(i + 1, piece) for i, piece in enumerate(pieces)]
    assert durable_store.append(OWNER, {rid: deltas[:200]}).committed
    terminal = _terminal_records(rid, 401, text)
    row = durable_store.finish(OWNER, rid, deltas[200:] + terminal, {"status": "completed"}, text=text)
    assert row is not None and row["status"] == "completed"

    with db.connection() as con:
        stored = con.execute(
            "SELECT coalesce(sum(length(data::text)), 0) AS n FROM api_response_events "
            "WHERE response_id = %s AND sequence_number > 400", (rid,)
        ).fetchone()["n"]
    assert stored < 2000, stored  # four copies of a 4,000-character answer were ~16,000

    expected = [(s, n, json.loads(durable_store._clean_json(d))) for s, n, d in terminal]
    assert durable_store.list_events(rid, 400) == expected
    assert durable_store.poll_many({rid: 400})[rid].events == expected
    # The frames a client is sent carry the records as written (jsonb orders
    # keys its own way, so compare the parsed frames).
    assert [_frame(r) for r in durable_store.list_events(rid, 400)] == [_frame(r) for r in expected]
    assert expected[0][2]["text"] == text.replace("\x00", "")


def test_a_short_answer_keeps_its_terminal_records_inline():
    tenant = make_tenant()
    rid = _durable_run(tenant)
    text = "short answer"
    durable_store.append(OWNER, {rid: [_delta(1, text)]})
    durable_store.finish(OWNER, rid, _terminal_records(rid, 2, text), {"status": "completed"}, text=text)
    with db.connection() as con:
        data = con.execute("SELECT data FROM api_response_events WHERE response_id = %s AND sequence_number = 2",
                           (rid,)).fetchone()["data"]
    assert data["text"] == text and durable_store.TEXT_KEY not in data


def test_a_follower_replaying_a_long_finished_run_from_the_log_gets_the_whole_answer_in_every_terminal_frame(monkeypatch, tmp_path):
    set_setting(monkeypatch, "PUBLIC_API_TERMINAL_TEXT_BY_REFERENCE", "true")
    FakeMainEngine(answer_tokens=400).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True)
            live = await collect(handle)
            await handle.wait()
            replay = await collect(await runtime.attach(handle.id, caller=tenant.caller()), 0)
            outcome = await asyncio.to_thread(durable.outcome_from_log, durable_store.get_run(handle.id))
            return handle.id, live, replay, outcome
        finally:
            await runtime.stop()

    rid, live, replay, outcome = asyncio.run(scenario())
    text = expected_text(400)
    assert len(text) > durable_store.TERMINAL_TEXT_INLINE_MAX_CHARS
    with db.connection() as con:
        raw = con.execute("SELECT data FROM api_response_events WHERE response_id = %s AND event = %s",
                          (rid, events.RESPONSE_OUTPUT_TEXT_DONE)).fetchone()["data"]
    assert raw["text"] == "" and durable_store.TEXT_KEY in raw  # stored by reference...
    by_name = {r[1]: r[2] for r in replay}
    assert by_name[events.RESPONSE_OUTPUT_TEXT_DONE]["text"] == text  # ...and replayed whole
    assert by_name[events.RESPONSE_CONTENT_PART_DONE]["part"]["text"] == text
    assert by_name[events.RESPONSE_OUTPUT_ITEM_DONE]["item"]["content"][0]["text"] == text
    assert by_name[events.RESPONSE_COMPLETED]["response"]["output"][0]["content"][0]["text"] == text.strip()
    assert [_frame(r) for r in replay] == [_frame(r) for r in live]
    assert outcome.text == text and outcome.status == "completed"


def test_the_outcome_rebuilt_from_the_log_never_reads_every_event_row(monkeypatch):
    tenant = make_tenant()
    rid = _durable_run(tenant)
    durable_store.append(OWNER, {rid: [_delta(i, f"t{i} ") for i in range(1, 51)]})
    from app.publicapi import errors, models

    failure = {"response": models.Response(
        id=rid, created_at=1, status="failed", model="techsara-35b", output=[], usage=None, max_output_tokens=10,
        incomplete_details=None, error=models.ResponseError(code="model_unavailable", message="gone")).to_wire(),
        "_should_retry": True}
    durable_store.finish(OWNER, rid, [(51, events.RESPONSE_FAILED, {"type": events.RESPONSE_FAILED, **failure})],
                         {"status": "failed", "error_code": "model_unavailable"})

    def refuse(*_a, **_k):
        raise AssertionError("outcome_from_log read the log row by row")

    monkeypatch.setattr(durable_store, "list_events", refuse)
    outcome = durable.outcome_from_log(durable_store.get_run(rid))
    assert outcome.text == "".join(f"t{i} " for i in range(1, 51))
    assert isinstance(outcome.error, errors.ApiError) and outcome.error.code == "model_unavailable"
    assert getattr(outcome.error, "should_retry") is True


# ----------------------------------------------------------------- purge --


def test_purge_finds_due_logs_through_the_primary_key_not_a_scan_of_the_log():
    tenant = make_tenant()
    rid = _durable_run(tenant)
    with db.connection() as con:
        con.execute(
            "INSERT INTO api_response_events (response_id, sequence_number, event, data) "
            "SELECT %s, g, 'response.output_text.delta', '{\"delta\": \"x\"}' FROM generate_series(1, 20000) g",
            (rid,),
        )
    with psycopg.connect(db.dsn(), autocommit=True) as con:
        con.execute("ANALYZE api_response_events")
        con.execute("ANALYZE api_responses")
        plan = con.execute("EXPLAIN (FORMAT JSON) " + durable_store._PURGE_DUE_SQL, (3600.0, 50)).fetchone()[0]
    text = json.dumps(plan)
    assert "api_response_events" in text
    nodes = []

    def walk(node):
        nodes.append((node.get("Node Type"), node.get("Relation Name")))
        for child in node.get("Plans", []):
            walk(child)

    walk(plan[0]["Plan"])
    assert ("Seq Scan", "api_response_events") not in nodes, nodes


def test_purge_keeps_an_open_resumable_runs_log_past_its_expiry_and_takes_a_settled_ones():
    tenant = make_tenant()
    open_run, settled = _durable_run(tenant), _durable_run(tenant)
    for rid in (open_run, settled):
        durable_store.append(OWNER, {rid: [_delta(1), _delta(2)]})
    durable_store.finish(OWNER, settled, [], {"status": "completed"})
    with db.connection() as con:
        con.execute("UPDATE api_responses SET expires_at = now() - interval '1 minute' WHERE id = ANY(%s)",
                    ([open_run, settled],))
    assert durable_store.purge_events(retention_s=3600) == 2
    assert durable_store.events_retained(open_run) and not durable_store.events_retained(settled)


# -------------------------------------------------------------- dispatch --


def test_an_idle_dispatcher_backs_off_and_a_launch_still_wakes_it_at_once(monkeypatch, tmp_path):
    set_setting(monkeypatch, "PUBLIC_API_LAPSED_SWEEP_S", "3600")
    FakeMainEngine(answer_tokens=3).install(monkeypatch)
    tenant = make_tenant()
    calls = []
    real = durable_store.due_for_resume

    def counting(*args, **kwargs):
        calls.append(time.monotonic())
        return real(*args, **kwargs)

    monkeypatch.setattr(durable_store, "due_for_resume", counting)

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            await asyncio.sleep(6.5)
            idle_calls = len(calls)
            spec = make_spec()
            handle = await runtime.launch(spec, caller=tenant.caller(), background=True)
            started = time.monotonic()
            row = await asyncio.wait_for(handle.wait(), 10)
            return idle_calls, time.monotonic() - started, row
        finally:
            await runtime.stop()

    idle_calls, took, row = asyncio.run(scenario())
    # 1 s polling issued 6 scans in 6.5 s; backing off 1, 2, 4 s issues 3.
    assert idle_calls <= 3, idle_calls
    assert row["status"] == "completed" and took < 3.0, took


# ------------------------------------------------------------ files lane --


@pytest.fixture
def files_tables(app_database, tmp_path, monkeypatch):
    file_schema.ensure_schema()
    root = tmp_path / "api-files"
    root.mkdir()
    monkeypatch.setenv("PUBLIC_API_FILES_DIR", str(root))
    monkeypatch.setenv("PUBLIC_API_FILES_MIN_FREE_GIB", "0")
    file_events.reset_for_tests()
    with db.connection() as con:
        for table in ("api_upload_parts", "api_files", "api_uploads", "api_file_blobs"):
            con.execute(f"DELETE FROM {table}")
    yield


def _insert_blob(payload: bytes) -> str:
    workspace_id = "ws_" + uuid.uuid4().hex[:12]
    with db.connection() as con:
        con.execute("INSERT INTO workspaces (id, name) VALUES (%s, 'files')", (workspace_id,))
    project = db.create_api_project(workspace_id, "files-" + uuid.uuid4().hex[:6], "live")
    sha = hashlib.sha256(payload).hexdigest()
    original = storage.original_path(project["id"], sha)
    os.makedirs(os.path.dirname(original), exist_ok=True)
    with open(original, "wb") as fh:
        fh.write(payload)
    blob_id = file_ids.new_blob_id()
    with db.connection() as con:
        con.execute(
            "INSERT INTO api_file_blobs (id, project_id, workspace_id, sha256, bytes, kind, lane, status, progress) "
            "VALUES (%s, %s, %s, %s, %s, 'unknown', 'cpu', 'queued', '{}')",
            (blob_id, project["id"], workspace_id, sha, len(payload)),
        )
        con.execute(
            "INSERT INTO api_files (id, project_id, workspace_id, blob_id, filename, purpose, bytes) "
            "VALUES (%s, %s, %s, %s, 'notes.txt', 'user_data', %s)",
            (file_ids.new_file_id(), project["id"], workspace_id, blob_id, len(payload)),
        )
    return blob_id


def test_the_files_lane_idles_on_a_long_poll_and_a_deferral_wakes_it_when_it_falls_due(files_tables, tmp_path):
    assert jobs.POLL_S >= 30
    attempts = []

    async def flaky_index(ctx):
        attempts.append(time.monotonic())
        if len(attempts) == 1:
            raise jobs.Deferred("embedding engine down")
        return jobs.StageResult()

    async def stub_embed(texts):
        return [[0.1] * 8 for _ in texts], 1

    blob_id = _insert_blob(b"plain text that will be indexed\n" * 20)

    async def scenario():
        runner = jobs.JobRunner(
            "cpu", run_assemblies=False, emit_webhooks=False, record_usage=False, embed_documents=stub_embed,
            owner=f"test:{uuid.uuid4().hex[:8]}", stages={"index": flaky_index}, retry_delay_s=0.5, poll_s=60,
        )
        await runner.start()
        try:
            for _ in range(200):
                if file_schema.get_api_file_blob(blob_id)["status"] == "processed":
                    return
                await asyncio.sleep(0.05)
            raise AssertionError(f"the deferred blob was not re-claimed when due ({len(attempts)} attempts)")
        finally:
            await runner.stop()

    asyncio.run(scenario())
    assert len(attempts) == 2 and attempts[1] - attempts[0] >= 0.5


# ------------------------------------------------- verifier additions --


def test_the_writer_switch_off_stores_a_long_answer_inline_for_a_rollback_safe_first_deploy(monkeypatch, tmp_path):
    """PUBLIC_API_TERMINAL_TEXT_BY_REFERENCE=false: the terminal records carry
    the answer inline, exactly as the code before this change wrote them, so
    an automatic rollback to that code never meets a record it renders empty."""
    set_setting(monkeypatch, "PUBLIC_API_TERMINAL_TEXT_BY_REFERENCE", "false")
    FakeMainEngine(answer_tokens=400).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True)
            await collect(handle)
            await handle.wait()
            return handle.id
        finally:
            await runtime.stop()

    rid = asyncio.run(scenario())
    text = expected_text(400)
    assert len(text) > durable_store.TERMINAL_TEXT_INLINE_MAX_CHARS
    with db.connection() as con:
        rows = con.execute("SELECT event, data FROM api_response_events WHERE response_id = %s AND event <> %s",
                           (rid, durable_store.DELTA_EVENT)).fetchall()
    assert rows and not any(durable_store.TEXT_KEY in r["data"] for r in rows)
    done = next(r["data"] for r in rows if r["event"] == events.RESPONSE_OUTPUT_TEXT_DONE)
    assert done["text"] == text


def test_a_paged_replay_that_starts_inside_the_terminal_records_still_gets_the_whole_answer():
    tenant = make_tenant()
    rid = _durable_run(tenant)
    pieces = [f" chunk-{i:04d}" for i in range(300)]
    text = "".join(pieces)
    durable_store.append(OWNER, {rid: [_delta(i + 1, p) for i, p in enumerate(pieces)]})
    terminal = _terminal_records(rid, 301, text)
    assert durable_store.finish(OWNER, rid, terminal, {"status": "completed"}, text=text) is not None
    expected = [(s, n, json.loads(durable_store._clean_json(d))) for s, n, d in terminal]
    for after in (300, 301, 302, 303):
        assert durable_store.list_events(rid, after, 2) == [e for e in expected if e[0] > after][:2]
        assert durable_store.poll_many({rid: after}, limit_per=2)[rid].events == [e for e in expected if e[0] > after][:2]


def test_every_deferral_this_runner_writes_wakes_its_lane_not_only_the_earliest():
    runner = jobs.JobRunner("cpu", run_assemblies=False, emit_webhooks=False, record_usage=False,
                            owner="test:due", poll_s=60)
    runner._note_due(0.0)
    runner._note_due(0.3)
    time.sleep(0.1)
    assert runner._wait_s() == 0.0  # the first fell due
    wait = runner._wait_s()
    assert 0.0 < wait <= 0.3, wait  # the second still arms the timer
    time.sleep(wait + 0.01)
    assert runner._wait_s() == 0.0
    assert runner._wait_s() == 60.0


def test_a_purge_drains_many_short_due_runs_in_a_few_statements_not_one_per_run():
    """The statement budget bounds ROWS per statement, not runs: 300 settled
    three-event runs past retention go in one call with a budget of 5
    DELETE statements (a statement per run purged only 5 of them)."""
    tenant = make_tenant()
    runs = [_durable_run(tenant) for _ in range(300)]
    durable_store.append(OWNER, {rid: [_delta(1), _delta(2), _delta(3)] for rid in runs})
    with db.connection() as con:
        con.execute(
            "UPDATE api_responses SET status = 'completed', completed_at = now() - interval '2 hours' WHERE id = ANY(%s)",
            (runs,),
        )
    assert durable_store.purge_events(retention_s=3600, batch=5000, max_batches=5) == 900
    with db.connection() as con:
        left = con.execute("SELECT count(*) AS n FROM api_response_events WHERE response_id = ANY(%s)", (runs,)).fetchone()
    assert left["n"] == 0


def test_an_answer_that_contains_the_text_backslash_u0000_is_logged_as_written_and_a_nul_is_still_dropped():
    tenant = make_tenant()
    rid = _durable_run(tenant)
    pieces = ['JSON writes NUL as "\\u0000"; ', "and \\u0000n is not a newline", "\x00end\\\x00"]
    result = durable_store.append(OWNER, {rid: [_delta(i + 1, p) for i, p in enumerate(pieces)]})
    assert result.committed == {rid: [1, 2, 3]} and not result.lost
    assert [r[2]["delta"] for r in durable_store.list_events(rid)] == [p.replace("\x00", "") for p in pieces]


def test_the_writer_ships_off_by_default_so_an_automatic_rollback_never_renders_an_empty_answer(monkeypatch):
    """Cross-track review 2026-09-14: every push to main auto-deploys, and the
    deploy's automatic rollback starts def6b94-era code, which renders a
    by-reference terminal record with an empty answer. The writer is off until
    a reader is the rollback target; the readers understand both forms."""
    monkeypatch.delenv("PUBLIC_API_TERMINAL_TEXT_BY_REFERENCE", raising=False)
    assert durable.terminal_text_by_reference() is False
    from app.config import Settings

    assert Settings().public_api_terminal_text_by_reference is False
