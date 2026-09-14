"""Durable `/v1` generations in one process: the log, followers, attach,
orphans, background rows, yields, the store guard, blobs and retention
(no-timeout design, 2026-09-13).

Against real PostgreSQL (the write-ahead rules are SQL rules) and the
deterministic fake engine of tests/publicapi_fake_engine.py, whose token i is
`w{i} ` — so "the resumed text is identical" is a string comparison.
"""
from __future__ import annotations

import asyncio
import os
import stat
import time

import psycopg
import pytest

from app import db
from app.publicapi import blobs, capacity, durable, durable_store, errors
from tests.publicapi_fake_engine import (
    set_setting,
    FakeController,
    FakeMainEngine,
    FakeSidecarEngine,
    collect,
    expected_text,
    fast_durable_settings,
    make_tenant,
    spec as make_spec,
)


@pytest.fixture(autouse=True)
def _durable(monkeypatch, tmp_path):
    fast_durable_settings(monkeypatch)
    durable_store.ensure_schema()
    capacity.reset_for_tests()
    set_setting(monkeypatch, "PUBLIC_API_BLOB_DIR", str(tmp_path / "blobs"))
    monkeypatch.setattr(blobs, "min_free_disk_bytes", lambda: 0)
    yield


async def _allow_all(keys):
    return set(keys)


def _runtime(tmp_path, owner="A", **kwargs):
    runtime = durable.Runtime()
    runtime.configure(
        owner=f"test-{owner}", view=FakeController(time.monotonic, state="READY"),
        authoriser=kwargs.pop("authoriser", _allow_all), blob_store=blobs.BlobStore(tmp_path / "blobs"),
        **kwargs,
    )
    runtime.witnesses_enabled = False  # the fake sidecars have no /metrics to scrape
    return runtime


def _events(response_id):
    return durable_store.list_events(response_id, 0, 100_000)


# ------------------------------------------------------------ the log --


def test_a_launched_stream_is_followed_from_the_log_with_contiguous_numbers_and_settles_once(monkeypatch, tmp_path):
    engine = FakeMainEngine(answer_tokens=30, delay_s=0.002).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True)
            records = await collect(handle)
            row = await handle.wait()
            return handle, records, row
        finally:
            await runtime.stop()

    handle, records, row = asyncio.run(scenario())
    seqs = [r[0] for r in records]
    assert seqs == list(range(1, len(records) + 1))
    names = [r[1] for r in records]
    assert names[0] == "response.created" and names[1] == "response.in_progress"
    assert names[-2:] == ["response.output_text.done", "response.completed"]
    text = "".join(r[2]["delta"] for r in records if r[1] == "response.output_text.delta")
    assert text == expected_text(30)
    assert records[-1][2]["response"]["usage"] == {"input_tokens": 17, "output_tokens": 30, "total_tokens": 47}
    assert row["status"] == "completed" and row["lease_owner"] is None
    assert row["generated_tokens"] == 30 and row["input_tokens"] == 17 and row["output_tokens"] == 30
    # The stored log IS what the follower saw; the spec is gone at terminal.
    assert [(s, n) for s, n, _ in _events(handle.id)] == [(s, n) for s, n, _ in records]
    assert durable_store.get_spec(handle.id) is None
    assert len(engine.calls) == 1 and engine.calls[0].kwargs["admission_patient"] is True
    # T1's admission links the patient LONG ticket to register_yield by this id.
    assert engine.calls[0].kwargs["admission_run_id"] == handle.id
    assert engine.calls[0].kwargs["wall_clock_s"] == 0 and engine.calls[0].kwargs["read_timeout_s"] is None


def test_deltas_are_coalesced_so_a_long_answer_writes_few_events(monkeypatch, tmp_path):
    FakeMainEngine(answer_tokens=400, delay_s=0.0005).install(monkeypatch)
    set_setting(monkeypatch, "PUBLIC_API_EVENT_FLUSH_S", "0.05")
    tenant = make_tenant()

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True)
            return handle, await collect(handle)
        finally:
            await runtime.stop()

    handle, records = asyncio.run(scenario())
    deltas = [r for r in records if r[1] == "response.output_text.delta"]
    assert "".join(r[2]["delta"] for r in deltas) == expected_text(400)
    assert sum(r[2]["_t"] for r in deltas) == 400
    assert len(deltas) < 400


def test_no_event_reaches_a_follower_before_its_transaction_committed(monkeypatch, tmp_path):
    FakeMainEngine(answer_tokens=25, delay_s=0.003).install(monkeypatch)
    tenant = make_tenant()
    committed = {}
    real_append = durable_store.append

    def slow_append(owner, batches):
        time.sleep(0.03)  # a slow commit: pending records sit in memory meanwhile
        result = real_append(owner, batches)
        for rid, seqs in result.committed.items():
            committed[rid] = max(seqs + [committed.get(rid, 0)])
        return result

    real_finish = durable_store.finish

    def recording_finish(owner, response_id, records, fields, **kwargs):
        row = real_finish(owner, response_id, records, fields, **kwargs)
        if row is not None and records:
            committed[response_id] = max([r[0] for r in records] + [committed.get(response_id, 0)])
        return row

    monkeypatch.setattr(durable_store, "append", slow_append)
    monkeypatch.setattr(durable_store, "finish", recording_finish)
    violations = []

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True)
            async for item in handle.follow(0, heartbeat=0.05):
                if item is durable.HEARTBEAT:
                    continue
                if item[0] > committed.get(handle.id, 0):
                    violations.append(item[0])
            return handle
        finally:
            await runtime.stop()

    handle = asyncio.run(scenario())
    assert violations == []
    assert len(_events(handle.id)) >= 5


def test_no_pool_connection_is_held_while_the_engine_is_silent(monkeypatch, tmp_path):
    silence = asyncio.Event()

    async def hang(engine, call, index):
        if index == 3:
            silence.set()
            await asyncio.sleep(0.6)

    FakeMainEngine(answer_tokens=6, before_token=hang).install(monkeypatch)
    tenant = make_tenant()
    in_use = []

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True)
            await asyncio.wait_for(silence.wait(), 5)
            await asyncio.sleep(0.15)  # the last pending batch has flushed
            for _ in range(20):
                stats = db.pool().get_stats()
                in_use.append(stats["pool_size"] - stats["pool_available"])
                await asyncio.sleep(0.02)
            await handle.wait()
        finally:
            await runtime.stop()

    asyncio.run(scenario())
    assert max(in_use) == 0


# ---------------------------------------------------------- followers --


def test_the_ninth_follower_closes_the_oldest(monkeypatch, tmp_path):
    release = asyncio.Event()

    async def gate(engine, call, index):
        if index == 2:
            await release.wait()

    FakeMainEngine(answer_tokens=5, before_token=gate).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        outcomes = {}
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True)

            async def follower(n):
                try:
                    async for _ in handle.follow(0, heartbeat=0.05):
                        pass
                    outcomes[n] = "ended"
                except durable.FollowerEvicted:
                    outcomes[n] = "evicted"

            tasks = []
            for n in range(8):
                tasks.append(asyncio.ensure_future(follower(n)))
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            tasks.append(asyncio.ensure_future(follower(8)))
            await asyncio.sleep(0.2)
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            return outcomes
        finally:
            await runtime.stop()

    outcomes = asyncio.run(scenario())
    assert outcomes[0] == "evicted"
    assert all(outcomes[n] == "ended" for n in range(1, 9))


def test_a_follower_of_a_terminal_run_closes_after_replay_whatever_its_position(monkeypatch, tmp_path):
    FakeMainEngine(answer_tokens=8).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True)
            await handle.wait()
            late = await runtime.attach(handle.id, caller=tenant.caller())
            everything = await collect(late, 0)
            beyond = await collect(await runtime.attach(handle.id, caller=tenant.caller()), 10_000, limit_s=5)
            tail = await collect(await runtime.attach(handle.id, caller=tenant.caller()), 3)
            return everything, beyond, tail
        finally:
            await runtime.stop()

    everything, beyond, tail = asyncio.run(scenario())
    assert everything[-1][1] == "response.completed"
    assert beyond == []
    assert [r[0] for r in tail] == [r[0] for r in everything if r[0] > 3]


def test_the_shared_poller_issues_at_most_one_query_per_second_for_two_hundred_remote_followers(monkeypatch, tmp_path):
    release = asyncio.Event()

    async def gate(engine, call, index):
        if index == 4:
            await release.wait()

    FakeMainEngine(answer_tokens=8, before_token=gate).install(monkeypatch)
    tenant = make_tenant()
    polls = []
    real_poll = durable_store.poll_many

    def counting_poll(after_by_id, **kwargs):
        polls.append((time.monotonic(), len(after_by_id)))
        return real_poll(after_by_id, **kwargs)

    monkeypatch.setattr(durable_store, "poll_many", counting_poll)
    set_setting(monkeypatch, "PUBLIC_API_MAX_FOLLOWERS", "1000")
    set_setting(monkeypatch, "PUBLIC_API_FOLLOWER_POLL_S", "1.0")

    async def scenario():
        owner = _runtime(tmp_path, "A")
        reader = _runtime(tmp_path, "B")
        await owner.start()
        await reader.start()
        try:
            handle = await owner.launch(make_spec(), caller=tenant.caller(), streamed=True)
            await asyncio.sleep(0.2)
            remote = await reader.attach(handle.id, caller=tenant.caller())
            assert remote.run is None  # owned elsewhere: served by B's shared poller
            got = [[] for _ in range(200)]

            async def follow(n):
                async for item in remote.follow(0, heartbeat=0.2):
                    if item is not durable.HEARTBEAT:
                        got[n].append(item[0])

            tasks = [asyncio.ensure_future(follow(n)) for n in range(200)]
            window_start = time.monotonic()
            await asyncio.sleep(3.05)
            in_window = [p for p in polls if p[0] >= window_start]
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 15)
            return in_window, got
        finally:
            await owner.stop()
            await reader.stop()

    in_window, got = asyncio.run(scenario())
    assert 1 <= len(in_window) <= 4  # ≤ 1 per second over 3 s, never one per follower
    assert all(ids == 1 for _, ids in in_window)  # one run id, however many followers
    assert all(seqs == got[0] and seqs == list(range(1, len(seqs) + 1)) for seqs in got)
    assert got[0] and len(got[0]) >= 6


# ------------------------------------------------------------ orphans --


def test_an_orphaned_unkeyed_stream_is_cancelled_after_its_grace_with_measured_usage(monkeypatch, tmp_path):
    set_setting(monkeypatch, "PUBLIC_API_UNKEYED_ORPHAN_GRACE_S", "0.2")
    engine = FakeMainEngine(answer_tokens=100_000, delay_s=0.002).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            handle = await runtime.launch(
                make_spec(), caller=tenant.caller(), dialect=durable.DIALECT_CHAT, streamed=True
            )
            seen = 0
            async for item in handle.follow(0, heartbeat=0.05):
                if item is not durable.HEARTBEAT:
                    seen += 1
                if seen >= 3:
                    break  # the client went away
            await asyncio.wait_for(handle.run.done.wait(), 10)
            return handle
        finally:
            await runtime.stop()

    handle = asyncio.run(scenario())
    row = durable_store.get_run(handle.id)
    assert row["status"] == "cancelled"
    assert row["output_tokens"] and row["output_tokens"] > 0
    assert engine.open_streams == 0


def test_a_reader_returning_within_the_grace_keeps_the_run(monkeypatch, tmp_path):
    set_setting(monkeypatch, "PUBLIC_API_STREAM_ORPHAN_GRACE_S", "0.5")
    FakeMainEngine(answer_tokens=60, delay_s=0.01).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True)
            position = 0
            async for item in handle.follow(0, heartbeat=0.05):
                if item is not durable.HEARTBEAT:
                    position = item[0]
                    break
            await asyncio.sleep(0.2)
            again = await runtime.attach(handle.id, caller=tenant.caller())
            rest = await collect(again, position)
            return handle, position, rest
        finally:
            await runtime.stop()

    handle, position, rest = asyncio.run(scenario())
    assert rest[0][0] == position + 1 and rest[-1][1] == "response.completed"
    assert durable_store.get_run(handle.id)["status"] == "completed"


# -------------------------------------------------------------- attach --


def test_implicit_attach_joins_an_orphan_only_for_an_sdk_retry_of_the_same_key_and_body(monkeypatch, tmp_path):
    set_setting(monkeypatch, "PUBLIC_API_UNKEYED_ORPHAN_GRACE_S", "30")
    FakeMainEngine(answer_tokens=100_000, delay_s=0.005).install(monkeypatch)
    tenant = make_tenant(keys=2)

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            handle = await runtime.launch(
                make_spec(), caller=tenant.caller(), dialect=durable.DIALECT_CHAT, streamed=True,
                body_sha256="a" * 64,
            )
            async for item in handle.follow(0, heartbeat=0.05):
                if item is not durable.HEARTBEAT:
                    break
            await asyncio.sleep(0.1)  # orphaned_at is written
            joined = await runtime.attach_implicit(
                caller=tenant.caller(), dialect=durable.DIALECT_CHAT, body_sha256="a" * 64, retry_count=1)
            fresh = await runtime.attach_implicit(
                caller=tenant.caller(), dialect=durable.DIALECT_CHAT, body_sha256="a" * 64, retry_count=0)
            other_body = await runtime.attach_implicit(
                caller=tenant.caller(), dialect=durable.DIALECT_CHAT, body_sha256="b" * 64, retry_count=1)
            other_key = await runtime.attach_implicit(
                caller=tenant.caller(1), dialect=durable.DIALECT_CHAT, body_sha256="a" * 64, retry_count=1)
            other_route = await runtime.attach_implicit(
                caller=tenant.caller(), dialect=durable.DIALECT_RESPONSES, body_sha256="a" * 64, retry_count=1)
            runtime.cancel_local(handle.id)
            await handle.run.done.wait()
            return handle, joined, fresh, other_body, other_key, other_route
        finally:
            await runtime.stop()

    handle, joined, fresh, other_body, other_key, other_route = asyncio.run(scenario())
    assert joined is not None and joined.id == handle.id
    assert fresh is None and other_body is None and other_key is None and other_route is None


def test_attach_is_creator_only_and_a_service_account_sibling_key_may_attach(monkeypatch, tmp_path):
    FakeMainEngine(answer_tokens=5).install(monkeypatch)
    tenant = make_tenant(keys=1, service_account_keys=2)
    stranger = make_tenant("ws-other")

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            plain = await runtime.launch(make_spec(), caller=tenant.caller(0), streamed=True)
            svc = await runtime.launch(make_spec(), caller=tenant.caller(1), streamed=True)
            await plain.wait()
            await svc.wait()
            results = {}
            for label, response_id, caller in (
                ("same key", plain.id, tenant.caller(0)),
                ("other key same project", plain.id, tenant.caller(1)),
                ("sibling service account key", svc.id, tenant.caller(2)),
                ("other project", plain.id, stranger.caller(0)),
                ("missing", "resp_missing", tenant.caller(0)),
            ):
                try:
                    await runtime.attach(response_id, caller=caller)
                    results[label] = "allowed"
                except durable.AttachForbidden:
                    results[label] = "forbidden"
            return results
        finally:
            await runtime.stop()

    results = asyncio.run(scenario())
    assert results == {
        "same key": "allowed",
        "other key same project": "forbidden",
        "sibling service account key": "allowed",
        "other project": "forbidden",
        "missing": "forbidden",  # indistinguishable from another project's run
    }


def test_a_run_whose_events_are_past_retention_is_not_streamable(monkeypatch, tmp_path):
    FakeMainEngine(answer_tokens=5).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True)
            await handle.wait()
            with db.connection() as con:
                con.execute("UPDATE api_responses SET completed_at = now() - interval '2 hours' WHERE id = %s", (handle.id,))
            purged = durable_store.purge_events(retention_s=3600)
            with pytest.raises(durable.NotStreamable):
                await runtime.attach(handle.id, caller=tenant.caller())
            return purged
        finally:
            await runtime.stop()

    assert asyncio.run(scenario()) >= 5


# ---------------------------------------------------------- background --


def test_while_the_gate_is_full_one_background_row_waits_in_its_line_and_the_rest_stay_rows(monkeypatch, tmp_path):
    """2026-09-14 review P5: a background row is given a place in the gate's
    FIFO line — ONE claimed representative per gate group — instead of being
    claimed only when the gate would admit at once. The other rows stay rows
    (no lease, no coroutine), and all complete, oldest first, once the gate
    frees."""
    FakeMainEngine(answer_tokens=12).install(monkeypatch)
    set_setting(monkeypatch, "PUBLIC_API_MAIN_NORMAL_MAX_CONCURRENT", "1")
    tenant = make_tenant()
    specs = [make_spec() for _ in range(3)]

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            async with capacity.hold("main.normal"):
                for spec in specs:
                    await runtime.launch(spec, caller=tenant.caller(), background=True)
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.4)
                queued = (len(runtime.runs), [durable_store.get_run(s.response_id) for s in specs],
                          capacity.snapshot()["main.normal"]["waiting"])
            rows = []
            for _ in range(200):
                await asyncio.sleep(0.05)
                rows = [durable_store.get_run(s.response_id) for s in specs]
                if all(r["status"] == "completed" for r in rows):
                    break
            return queued, rows
        finally:
            await runtime.stop()

    (held_runs, queued_rows, waiting), rows = asyncio.run(scenario())
    assert held_runs == 1 and waiting == 1
    assert queued_rows[0]["lease_owner"] is not None and queued_rows[0]["status"] == "queued"
    assert [r["lease_owner"] for r in queued_rows[1:]] == [None, None]
    assert [r["status"] for r in rows] == ["completed"] * 3
    assert all(r["output_text"] == expected_text(12) for r in rows)
    started = [r["started_at"] for r in rows]
    assert started == sorted(started)  # oldest first


def test_ten_thousand_queued_background_rows_hold_no_coroutines_and_little_memory(monkeypatch, tmp_path):
    set_setting(monkeypatch, "PUBLIC_API_MAIN_NORMAL_MAX_CONCURRENT", "1")
    FakeMainEngine(answer_tokens=3).install(monkeypatch)
    tenant = make_tenant()
    stored, _ = durable.spec_to_json(make_spec(response_id="resp_bulk_template"))
    body = {"spec": stored, "dialect": "responses", "background": True, "keyed": False, "streamed": False,
            "extra": {}, "workspace_id": tenant.workspace_id}
    import json as _json

    with db.connection() as con:
        con.execute(
            """
            INSERT INTO api_responses (id, project_id, workspace_id, model, status, background, request_id,
                                       resumable, dialect, engine, enqueued_at)
            SELECT 'resp_bulk_' || g, %s, %s, 'techsara-35b', 'queued', true, 'r', true, 'responses', 'main',
                   now() + make_interval(secs => g)
            FROM generate_series(1, 10000) g
            """,
            (tenant.project["id"], tenant.workspace_id),
        )
        # Every row carries its stored spec, as a launch writes it: the
        # dispatcher's representative must be runnable, not "spec missing".
        con.execute(
            """
            INSERT INTO api_response_requests (response_id, spec)
            SELECT 'resp_bulk_' || g, jsonb_set(%s::jsonb, '{spec,response_id}', to_jsonb('resp_bulk_' || g))
            FROM generate_series(1, 10000) g
            """,
            (_json.dumps(body),),
        )

    def rss_kib():
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
        return 0

    async def scenario():
        runtime = _runtime(tmp_path)
        before = rss_kib()
        tasks_before = len(asyncio.all_tasks())
        fds_before = len(os.listdir("/proc/self/fd"))
        async with capacity.hold("main.normal"):
            for _ in range(3):
                await runtime.dispatch_once()
                await asyncio.sleep(0.05)
            measured = (rss_kib() - before, len(runtime.runs), len(asyncio.all_tasks()) - tasks_before,
                        len(os.listdir("/proc/self/fd")) - fds_before)
            for run in list(runtime.runs.values()):
                runtime.cancel_local(run.id)
                await asyncio.wait_for(run.done.wait(), 10)
        return measured

    delta_kib, runs, extra_tasks, extra_fds = asyncio.run(scenario())
    # One representative waits in the gate's line (review P5); 9,999 stay rows.
    assert runs == 1 and extra_tasks <= 2
    assert delta_kib < 64 * 1024
    assert extra_fds <= 2


def test_a_queued_ocr_background_response_survives_suspend_all_and_completes(monkeypatch, tmp_path):
    sidecar = FakeSidecarEngine(answer_tokens=9).install(monkeypatch)
    set_setting(monkeypatch, "PUBLIC_API_OCR_MAX_CONCURRENT", "1")
    monkeypatch.setattr(capacity, "chat_is_busy", lambda: False)
    tenant = make_tenant()
    ocr_spec = make_spec(model="techsara-ocr", engine="ocr", gate_engine="ocr", max_tokens=64,
                         planned_max_output_tokens=64, context_window=8192)

    async def scenario():
        first = _runtime(tmp_path, "A")
        await first.start()
        async with capacity.hold("ocr"):
            await first.launch(ocr_spec, caller=tenant.caller(), background=True)
            await asyncio.sleep(0.2)
            assert durable_store.get_run(ocr_spec.response_id)["status"] == "queued"
            await first.suspend_all("restart")
            await first.stop()
        second = _runtime(tmp_path, "B")
        await second.start()
        try:
            for _ in range(200):
                await asyncio.sleep(0.05)
                if durable_store.get_run(ocr_spec.response_id)["status"] == "completed":
                    break
        finally:
            await second.stop()

    asyncio.run(scenario())
    row = durable_store.get_run(ocr_spec.response_id)
    assert row["status"] == "completed" and row["output_text"] == expected_text(9)
    assert len(sidecar.calls) == 1


def test_a_cancel_of_a_suspended_durable_run_settles_it_with_no_engine_work(monkeypatch, tmp_path):
    from app.publicapi import background

    engine = FakeMainEngine(answer_tokens=10_000, delay_s=0.005).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True)
        await asyncio.sleep(0.2)
        await runtime.suspend_all("restart")
        await runtime.stop()
        calls = len(engine.calls)
        monkeypatch.setattr(durable, "RUNTIME", durable.Runtime())
        row = await background.request_cancel(handle.id, tenant.project["id"])
        return row, calls

    row, calls = asyncio.run(scenario())
    assert row["status"] == "cancelled"
    assert len(engine.calls) == calls


def test_repair_if_orphaned_never_touches_a_resumable_row(monkeypatch, tmp_path):
    from app.publicapi import background

    tenant = make_tenant()
    row = db.create_api_response(
        tenant.project["id"], tenant.workspace_id, "techsara-35b", "r", background=True, status="in_progress"
    )
    durable_store.mark_durable(row["id"], dialect="responses", item_id="msg_x", engine="main", owner=None, lease_ttl_s=60)
    with db.connection() as con:
        con.execute("UPDATE api_responses SET created_at = now() - interval '1 day' WHERE id = %s", (row["id"],))
    fresh = db.get_api_response(row["id"], tenant.project["id"])
    repaired = asyncio.run(background.repair_if_orphaned(fresh))
    assert repaired["status"] == "in_progress"


# ------------------------------------------------- yields and the store --


def test_a_chat_yield_suspends_within_a_second_and_the_resumed_text_is_identical(monkeypatch, tmp_path):
    engine = FakeMainEngine(answer_tokens=120, delay_s=0.004).install(monkeypatch)
    tenant = make_tenant()
    long_chat = {"present": False}
    monkeypatch.setattr(capacity, "chat_long_admission_present", lambda: long_chat["present"])
    monkeypatch.setattr(capacity, "YIELD_STEP_S", 0.02)

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True)
            follower = asyncio.ensure_future(collect(handle))
            while handle.run.generated_tokens < 30:
                await asyncio.sleep(0.005)
            long_chat["present"] = True
            asked = time.monotonic()
            assert runtime.request_yield(handle.id)
            while engine.open_streams:
                await asyncio.sleep(0.005)
            closed_after = time.monotonic() - asked
            await asyncio.sleep(0.2)
            calls_while_chat = len(engine.calls)
            long_chat["present"] = False
            records = await follower
            return handle, records, closed_after, calls_while_chat
        finally:
            await runtime.stop()

    handle, records, closed_after, calls_while_chat = asyncio.run(scenario())
    assert closed_after <= 1.0
    assert calls_while_chat == 1
    text = "".join(r[2]["delta"] for r in records if r[1] == "response.output_text.delta")
    assert text == expected_text(120)
    row = durable_store.get_run(handle.id)
    assert row["yields"] == 1 and row["stalled_attempts"] == 0
    assert engine.calls[1].kwargs["continue_final_message"] is True


def test_pending_buffer_overflow_during_a_database_outage_suspends_store_then_resumes(monkeypatch, tmp_path):
    engine = FakeMainEngine(answer_tokens=3000, delay_s=0.0005).install(monkeypatch)
    set_setting(monkeypatch, "PUBLIC_API_PENDING_EVENTS_MAX_BYTES", "1024")
    tenant = make_tenant()
    outage = {"on": False}
    real_append = durable_store.append

    def flaky_append(owner, batches):
        if outage["on"]:
            raise psycopg.OperationalError("database is down")
        return real_append(owner, batches)

    monkeypatch.setattr(durable_store, "append", flaky_append)

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True)
            while handle.run.generated_tokens < 20:
                await asyncio.sleep(0.002)
            outage["on"] = True
            for _ in range(500):
                await asyncio.sleep(0.01)
                if runtime.stats["store_suspends"] and not engine.open_streams:
                    break
            suspended = runtime.stats["store_suspends"]
            tokens_at_pause = handle.run.generated_tokens
            await asyncio.sleep(0.2)
            still_paused = handle.run.generated_tokens == tokens_at_pause
            outage["on"] = False
            records = await collect(await runtime.attach(handle.id, caller=tenant.caller()))
            return handle, suspended, still_paused, records
        finally:
            await runtime.stop()

    handle, suspended, still_paused, records = asyncio.run(scenario())
    assert suspended >= 1 and still_paused
    text = "".join(r[2]["delta"] for r in records if r[1] == "response.output_text.delta")
    assert text == expected_text(3000)
    assert [r[0] for r in records] == list(range(1, len(records) + 1))
    assert len(engine.calls) >= 2
    assert engine.calls[1].kwargs["continue_final_message"] is True


def test_a_resume_with_no_output_budget_left_settles_as_an_output_limit_stop(monkeypatch, tmp_path):
    async def stuck_at_the_budget(engine, call, index):
        if index == 50:
            await asyncio.sleep(3600)  # the 50 tokens are out; the stream has not ended

    engine = FakeMainEngine(
        answer_tokens=1000, delay_s=0.001, before_token=stuck_at_the_budget, respect_max_tokens=False
    ).install(monkeypatch)
    tenant = make_tenant()
    budget = make_spec(max_tokens=50, planned_max_output_tokens=50, requested_max_output_tokens=50)

    async def scenario():
        first = _runtime(tmp_path, "A")
        await first.start()
        handle = await first.launch(budget, caller=tenant.caller(), streamed=True)
        while handle.run.generated_tokens < 50:
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.1)  # flushed
        await first.suspend_all("restart")
        await first.stop()
        second = _runtime(tmp_path, "B")
        await second.start()
        try:
            resumed = await second.attach(budget.response_id, caller=tenant.caller())
            return await asyncio.wait_for(resumed.wait(), 20)  # bounded: a regression must fail, not hang
        finally:
            await second.stop()

    row = asyncio.run(scenario())
    assert row["status"] == "completed"
    assert row["finish_reason"] == "length"
    assert row["error_code"] is None
    assert len(engine.calls) == 1  # the resume settled without asking the engine
    assert row["output_tokens"] == 50


# -------------------------------------------------------- blobs, disk --


def test_images_in_a_spec_are_stored_once_as_private_blobs_and_restored_at_dispatch(tmp_path):
    store = blobs.BlobStore(tmp_path / "blobs")
    image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": image}},
        {"type": "image_url", "image_url": {"url": image}},
    ]}]
    stored, refs = blobs.externalize_images(messages, store)
    assert len(refs) == 1  # identical content, one file
    assert stored[0]["content"][1]["image_url"]["url"].startswith(blobs.BLOB_SCHEME)
    assert "base64" not in str(stored)
    path = store.path(refs[0][0])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert blobs.internalize_images(stored, store) == messages


def test_the_blob_reaper_keeps_referenced_blobs_and_deletes_old_orphans(tmp_path):
    store = blobs.BlobStore(tmp_path / "blobs")
    kept, _ = store.put(b"referenced")
    dropped, _ = store.put(b"orphan")
    young, _ = store.put(b"young orphan")
    old = time.time() - 7200
    os.utime(store.path(kept), (old, old))
    os.utime(store.path(dropped), (old, old))
    removed = store.reap(lambda shas: {kept} & set(shas), min_age_s=3600)
    assert removed == 1
    assert store.exists(kept) and store.exists(young) and not store.exists(dropped)


def test_the_disk_guard_refuses_a_launch_before_any_row(monkeypatch, tmp_path):
    FakeMainEngine(answer_tokens=3).install(monkeypatch)
    monkeypatch.setattr(blobs, "min_free_disk_bytes", lambda: 1 << 62)
    tenant = make_tenant()
    launched = make_spec()

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            with pytest.raises(errors.ApiError) as refused:
                await runtime.launch(launched, caller=tenant.caller(), streamed=True)
            return refused.value
        finally:
            await runtime.stop()

    refusal = asyncio.run(scenario())
    assert refusal.status == 503 and refusal.retry_after == 60
    assert db.get_api_response(launched.response_id, tenant.project["id"]) is None


# ---------------------------------------------------------- the schema --


def test_the_schema_applies_twice_and_its_constraints_hold_for_new_rows():
    durable_store.reset_schema_cache()
    durable_store.ensure_schema()
    durable_store.reset_schema_cache()
    durable_store.ensure_schema()
    tenant = make_tenant()
    row = db.create_api_response(tenant.project["id"], tenant.workspace_id, "techsara-35b", "r")
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.connection() as con:
            con.execute("UPDATE api_responses SET dialect = 'soap' WHERE id = %s", (row["id"],))
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.connection() as con:
            con.execute("UPDATE api_responses SET suspend_reason = 'boredom' WHERE id = %s", (row["id"],))
    with db.connection() as con:
        con.execute(
            "INSERT INTO api_response_events (response_id, sequence_number, event, data) VALUES (%s, 1, 'e', '{}')",
            (row["id"],),
        )
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.connection() as con:
            con.execute(
                "INSERT INTO api_response_events (response_id, sequence_number, event, data) VALUES (%s, 1, 'e', '{}')",
                (row["id"],),
            )
    with db.connection() as con:
        validated = con.execute(
            "SELECT convalidated FROM pg_constraint WHERE conname = 'api_responses_dialect'"
        ).fetchone()
    assert validated["convalidated"] is False  # NOT VALID: no scan at migration time


def test_a_held_lock_makes_the_schema_fail_fast_and_succeed_once_released():
    import threading

    durable_store.reset_schema_cache()
    with db.connection() as con:  # something is missing: the DDL must run
        con.execute("DROP INDEX IF EXISTS idx_api_response_blobs_sha")
    blocker = psycopg.connect(db.dsn(), autocommit=False)
    try:
        blocker.execute("LOCK TABLE api_responses IN ACCESS EXCLUSIVE MODE")
        started = time.monotonic()
        with pytest.raises(psycopg.errors.LockNotAvailable):
            durable_store.ensure_schema(attempts=1)
        assert time.monotonic() - started < 5
        timer = threading.Timer(0.5, blocker.rollback)
        timer.start()
        durable_store.ensure_schema(attempts=3)
        timer.join()
    finally:
        blocker.close()


def test_stored_events_are_purged_after_retention_in_batches():
    tenant = make_tenant()
    old = db.create_api_response(tenant.project["id"], tenant.workspace_id, "techsara-35b", "r1", status="completed")
    recent = db.create_api_response(tenant.project["id"], tenant.workspace_id, "techsara-35b", "r2", status="completed")
    with db.connection() as con:
        for rid in (old["id"], recent["id"]):
            con.execute(
                "INSERT INTO api_response_events (response_id, sequence_number, event, data) "
                "SELECT %s, g, 'response.output_text.delta', '{}' FROM generate_series(1, 1234) g",
                (rid,),
            )
        con.execute("UPDATE api_responses SET completed_at = now() - interval '2 hours' WHERE id = %s", (old["id"],))
    deleted = durable_store.purge_events(retention_s=3600, batch=100)
    assert deleted == 1234
    assert durable_store.events_retained(recent["id"]) and not durable_store.events_retained(old["id"])


# ------------------------------------------------------------ rendering --


def test_frames_carry_no_id_or_retry_lines_strip_private_keys_and_mark_seq_only_when_tagged():
    record = (7, "response.output_text.delta", {"type": "response.output_text.delta", "sequence_number": 7,
                                               "delta": "hi", "_t": 2})
    plain = durable.render_responses_frame(record)
    tagged = durable.render_responses_frame(record, tagged=True)
    assert '"_t"' not in plain and "\nid:" not in plain and "retry:" not in plain
    assert ": ts-seq" not in plain
    assert tagged.endswith(": ts-seq=7\n\n")


def test_the_chat_dialect_is_rendered_from_the_same_log():
    renderer = durable.ChatRenderer(completion_id="chatcmpl-1", model="techsara-35b", created=1, include_usage=True)
    frames = "".join([
        renderer.render((1, "response.created", {"response": {}})),
        renderer.render((3, "response.output_text.delta", {"delta": "w0 ", "_t": 1})),
        renderer.render((4, "response.output_text.done", {"text": "w0 "})),
        renderer.render((5, "response.completed", {"response": {
            "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
            "incomplete_details": None, "max_output_tokens": 10}})),
    ])
    assert '"content": "w0 "' in frames
    assert '"finish_reason": "stop"' in frames
    assert '"prompt_tokens": 3' in frames
    assert frames.endswith("data: [DONE]\n\n")


def test_a_router_run_whose_witness_says_lost_is_resumed_by_continuation(monkeypatch, tmp_path):
    """The sidecar liveness path end to end: the router's /metrics shows
    nothing running while our call is outstanding, the attempt is
    interrupted, and the run continues from its partial answer."""
    from app.publicapi import liveness

    sidecar = FakeSidecarEngine(answer_tokens=12, hang_first_after=4).install(monkeypatch)
    monkeypatch.setattr(liveness, "WITNESS_LOST_MIN_S", 0.1)
    monkeypatch.setattr(liveness.WitnessSampler, "SCRAPE_S", 0.02)
    idle = (
        'vllm:num_requests_running{model_name="m"} 0\n'
        'vllm:num_requests_waiting{model_name="m"} 0\n'
        'vllm:generation_tokens_total{model_name="m"} 5\n'
        "process_start_time_seconds 1700000000\n"
    )
    scraped = []

    async def fetch(root):
        scraped.append(root)
        return idle

    monkeypatch.setattr(liveness, "_sampler", liveness.WitnessSampler(fetch=fetch))
    tenant = make_tenant()
    router_spec = make_spec(model="techsara-8b-vision", engine="router", gate_engine="router",
                            max_tokens=64, planned_max_output_tokens=64, context_window=24_576)

    async def scenario():
        runtime = _runtime(tmp_path)
        runtime.witnesses_enabled = True
        await runtime.start()
        try:
            handle = await runtime.launch(router_spec, caller=tenant.caller(), streamed=True)
            return await collect(handle, limit_s=20)
        finally:
            await runtime.stop()

    records = asyncio.run(scenario())
    text = "".join(r[2]["delta"] for r in records if r[1] == "response.output_text.delta")
    assert text == expected_text(12)
    assert len(sidecar.calls) == 2 and sidecar.calls[1]["start"] == 4
    assert scraped and scraped[0] == "http://fake-router:8000"
    row = durable_store.get_run(router_spec.response_id)
    assert row["status"] == "completed" and row["stalled_attempts"] == 0
    assert [a["reason"] for a in row["metadata"]["attempts"]][:1] == ["lost"]


def test_a_run_waiting_at_its_gate_announces_queued_once_and_its_frames_follow_the_grammar(monkeypatch, tmp_path):
    from app.publicapi import events

    FakeMainEngine(answer_tokens=6).install(monkeypatch)
    set_setting(monkeypatch, "PUBLIC_API_MAIN_NORMAL_MAX_CONCURRENT", "1")
    tenant = make_tenant()

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            async with capacity.hold("main.normal"):
                handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True)
                await asyncio.sleep(0.2)
            wire = []
            async for frame in durable.sse_frames(handle, heartbeat=0.05):
                wire.append(frame)
            tagged = []
            async for frame in durable.sse_frames(await runtime.attach(handle.id, caller=tenant.caller()), after=2, tagged=True):
                tagged.append(frame)
            return "".join(wire), "".join(tagged)
        finally:
            await runtime.stop()

    wire, tagged = asyncio.run(scenario())
    records = events.parse_frames(wire)
    names = [r["event"] for r in records]
    assert names[:3] == ["response.created", "response.queued", "response.in_progress"]
    assert names.count("response.queued") == 1 and names[-1] == "response.completed"
    assert [r["data"]["sequence_number"] for r in records] == list(range(1, len(records) + 1))
    assert "\nid:" not in wire and "retry:" not in wire and ": ts-seq" not in wire
    # A heartbeat may come first: the attach reads the log from the database,
    # and a read slower than the heartbeat (a loaded runner: 1 in 5 parallel
    # suite runs, 2026-09-14) is covered by `: ping`, which the grammar allows
    # anywhere. What matters is the first EVENT.
    replay = tagged
    while replay.startswith(": ping\n\n"):
        replay = replay[len(": ping\n\n"):]
    assert replay.startswith("event: response.in_progress") and ": ts-seq=3\n\n" in replay


def test_a_yield_during_a_silent_prefill_closes_the_engine_stream_within_a_second(monkeypatch, tmp_path):
    """The durable heartbeat is 15 s in production; a yield must not wait for
    it. The engine here is silent (a long prefill) when chat asks."""
    monkeypatch.setattr(durable, "heartbeat_s", lambda: 15.0)
    silent = asyncio.Event()

    async def hook(engine, call, index):
        if len(engine.calls) == 1 and index == 5:
            silent.set()
            await asyncio.sleep(3600)

    engine = FakeMainEngine(answer_tokens=20, delay_s=0.001, before_token=hook).install(monkeypatch)
    monkeypatch.setattr(capacity, "chat_long_admission_present", lambda: False)
    tenant = make_tenant()

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True)
            await asyncio.wait_for(silent.wait(), 5)
            asked = time.monotonic()
            runtime.request_yield(handle.id)
            while engine.calls[0].closed is False:
                await asyncio.sleep(0.005)
            closed_after = time.monotonic() - asked
            records = await collect(handle, limit_s=20)
            return closed_after, records
        finally:
            await runtime.stop()

    closed_after, records = asyncio.run(scenario())
    assert closed_after < 1.0
    assert "".join(r[2]["delta"] for r in records if r[1] == "response.output_text.delta") == expected_text(20)


@pytest.mark.parametrize("followed", [False, True], ids=["never-read", "read"])
def test_an_ocr_run_suspended_mid_page_restarts_unread_or_fails_retryably_once_read(monkeypatch, tmp_path, followed):
    sidecar = FakeSidecarEngine(answer_tokens=400, delay_s=0.002).install(monkeypatch)
    monkeypatch.setattr(capacity, "chat_is_busy", lambda: False)
    tenant = make_tenant()
    ocr_spec = make_spec(model="techsara-ocr", engine="ocr", gate_engine="ocr", max_tokens=1000,
                         planned_max_output_tokens=1000, context_window=8192)

    async def scenario():
        a = _runtime(tmp_path, "A")
        await a.start()
        handle = await a.launch(ocr_spec, caller=tenant.caller(), streamed=followed, keyed=True)
        if followed:
            async for item in handle.follow(0, heartbeat=0.05):
                if item is not durable.HEARTBEAT and item[1] == "response.output_text.delta":
                    break
        while handle.run.generated_tokens < 20:
            await asyncio.sleep(0.002)
        await asyncio.sleep(0.1)
        await a.suspend_all("restart")
        await a.stop()
        b = _runtime(tmp_path, "B")
        await b.start()
        try:
            return await collect(await b.attach(ocr_spec.response_id, caller=tenant.caller()), 0, limit_s=20)
        finally:
            await b.stop()

    records = asyncio.run(scenario())
    row = durable_store.get_run(ocr_spec.response_id)
    if followed:
        assert row["status"] == "failed" and row["error_code"] == "model_unavailable"
        assert len(sidecar.calls) == 1
    else:
        assert row["status"] == "completed" and row["output_text"] is None
        text = "".join(r[2]["delta"] for r in records if r[1] == "response.output_text.delta")
        assert text == expected_text(400)
        assert [c["start"] for c in sidecar.calls] == [0, 0]  # re-read from scratch, never continued
        assert [r[0] for r in records] == list(range(1, len(records) + 1))


def test_a_suspend_100_tokens_before_the_window_is_full_resumes_as_an_output_limit_stop(monkeypatch, tmp_path):
    """Design: 'suspend at planned − 100 → output-limit stop, not failed'. The
    window is what is short here (not the budget): a continuation needs
    MIN_OUTPUT_TOKENS + CONTEXT_SAFETY_MARGIN of room, and 100 is not that."""
    window, reserve, prompt = 2000, 512, 10
    planned = window - prompt - reserve  # 1478: the planner's clamp

    async def stuck(engine, call, index):
        if index == planned - 100:
            await asyncio.sleep(3600)

    engine = FakeMainEngine(answer_tokens=10_000, delay_s=0.0002, before_token=stuck).install(monkeypatch)
    tenant = make_tenant()
    tight = make_spec(max_tokens=planned, planned_max_output_tokens=planned, requested_max_output_tokens=5000,
                      context_window=window, context_reserve=reserve, estimated_input_tokens=prompt)

    async def scenario():
        a = _runtime(tmp_path, "A")
        await a.start()
        handle = await a.launch(tight, caller=tenant.caller(), streamed=True)
        while handle.run.generated_tokens < planned - 100:
            await asyncio.sleep(0.002)
        await asyncio.sleep(0.1)
        await a.suspend_all("restart")
        await a.stop()
        b = _runtime(tmp_path, "B")
        await b.start()
        try:
            return await (await b.attach(tight.response_id, caller=tenant.caller())).result()
        finally:
            await b.stop()

    outcome = asyncio.run(scenario())
    assert outcome.status == "completed" and outcome.finish_reason == "length"
    assert outcome.response().to_wire()["incomplete_details"] == {"reason": "max_output_tokens"}
    assert len(outcome.text.split()) == planned - 100
    assert len(engine.calls) == 1


def test_a_gateway_repost_attaches_only_with_the_same_attempt_key_and_body(monkeypatch, tmp_path):
    FakeMainEngine(answer_tokens=30, delay_s=0.002).install(monkeypatch)
    tenant = make_tenant(keys=2)

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            first = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True,
                                         attempt_token="att-1", body_sha256="a" * 64)
            again = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True,
                                         attempt_token="att-1", body_sha256="a" * 64)
            outcomes = {"same": again.id == first.id}
            for label, caller, sha in (("other body", tenant.caller(), "b" * 64),
                                       ("other key", tenant.caller(1), "a" * 64)):
                try:
                    await runtime.launch(make_spec(), caller=caller, streamed=True, attempt_token="att-1", body_sha256=sha)
                    outcomes[label] = "attached"
                except durable.AttachForbidden:
                    outcomes[label] = "refused"
            await first.wait()
            return outcomes
        finally:
            await runtime.stop()

    assert asyncio.run(scenario()) == {"same": True, "other body": "refused", "other key": "refused"}
    with db.connection() as con:
        assert con.execute("SELECT count(*) AS n FROM api_responses").fetchone()["n"] == 1


# ---------------------------------------- adversarial review fixes (2026-09-14) --


def test_a_launched_run_that_no_reader_ever_follows_is_cancelled_at_its_orphan_grace(monkeypatch, tmp_path):
    """Review P2: the client disconnected before its body started iterating,
    so no reader was ever added. The orphan clock now starts at launch: the
    run is cancelled at its grace instead of generating for nobody."""
    set_setting(monkeypatch, "PUBLIC_API_UNKEYED_ORPHAN_GRACE_S", "0.3")
    monkeypatch.setattr(durable, "LAUNCH_ATTACH_GRACE_S", 0.1)
    engine = FakeMainEngine(answer_tokens=100_000, delay_s=0.002).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), dialect=durable.DIALECT_CHAT,
                                          streamed=True)
            started = time.monotonic()
            await asyncio.wait_for(handle.run.done.wait(), 10)
            return handle, time.monotonic() - started, runtime.stats["orphan_cancels"]
        finally:
            await runtime.stop()

    handle, took, orphan_cancels = asyncio.run(scenario())
    row = durable_store.get_run(handle.id)
    assert row["status"] == "cancelled" and orphan_cancels == 1
    assert took < 3.0
    assert engine.open_streams == 0


def test_an_sdk_retry_of_a_run_whose_client_left_before_its_body_started_attaches_instead_of_launching(monkeypatch, tmp_path):
    """Review P2b: orphaned_at is written once the attach grace passes with no
    reader, so the SDK's retry (x-stainless-retry-count 1, same key and body)
    joins the run rather than starting a second full generation."""
    set_setting(monkeypatch, "PUBLIC_API_UNKEYED_ORPHAN_GRACE_S", "30")
    monkeypatch.setattr(durable, "LAUNCH_ATTACH_GRACE_S", 0.1)
    FakeMainEngine(answer_tokens=100_000, delay_s=0.002).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            first = await runtime.launch(make_spec(), caller=tenant.caller(), dialect=durable.DIALECT_CHAT,
                                         streamed=True, body_sha256="c" * 64)
            await asyncio.sleep(0.6)
            orphaned = durable_store.get_run(first.id)["orphaned_at"]
            joined = await runtime.attach_implicit(caller=tenant.caller(), dialect=durable.DIALECT_CHAT,
                                                   body_sha256="c" * 64, retry_count=1)
            follower = joined.follow(0, heartbeat=0.05)
            await follower.__anext__()
            await asyncio.sleep(0.3)
            cleared = durable_store.get_run(first.id)["orphaned_at"]
            await follower.aclose()
            runtime.cancel_local(first.id)
            await asyncio.wait_for(first.run.done.wait(), 10)
            return first, joined, orphaned, cleared
        finally:
            await runtime.stop()

    first, joined, orphaned, cleared = asyncio.run(scenario())
    assert orphaned is not None
    assert joined is not None and joined.id == first.id and joined.run is first.run
    assert cleared is None  # the retry's reader cleared the mark
    with db.connection() as con:
        assert con.execute("SELECT count(*) AS n FROM api_responses").fetchone()["n"] == 1


def test_a_reader_that_attaches_right_after_launch_leaves_no_orphan_mark_and_no_cancel(monkeypatch, tmp_path):
    """The launch-time orphan clock must not touch the ordinary case: the body
    starts at once, nothing is written, and the run completes."""
    set_setting(monkeypatch, "PUBLIC_API_UNKEYED_ORPHAN_GRACE_S", "0.2")
    monkeypatch.setattr(durable, "LAUNCH_ATTACH_GRACE_S", 0.1)
    FakeMainEngine(answer_tokens=300, delay_s=0.003).install(monkeypatch)
    tenant = make_tenant()
    marks = []
    real_update = durable_store.update_progress

    def spy(owner, response_id, **fields):
        if fields.get("orphaned_at_now"):
            marks.append(response_id)
        return real_update(owner, response_id, **fields)

    monkeypatch.setattr(durable_store, "update_progress", spy)

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), dialect=durable.DIALECT_CHAT,
                                          streamed=True)
            return handle, await collect(handle)
        finally:
            await runtime.stop()

    handle, records = asyncio.run(scenario())
    assert records[-1][1] == "response.completed"
    assert marks == []
    assert durable_store.get_run(handle.id)["orphaned_at"] is None


def test_a_router_row_behind_sixty_main_rows_with_a_full_gate_is_dispatched(monkeypatch, tmp_path):
    """Review P5: a global LIMIT 50 scan let main rows whose gate was full hide
    a router row whose gate had room. The scan is per engine now."""
    set_setting(monkeypatch, "PUBLIC_API_MAIN_NORMAL_MAX_CONCURRENT", "1")
    sidecar = FakeSidecarEngine(answer_tokens=3).install(monkeypatch)
    FakeMainEngine(answer_tokens=3).install(monkeypatch)
    monkeypatch.setattr(capacity, "chat_is_busy", lambda: False)
    tenant = make_tenant()
    stored, _ = durable.spec_to_json(make_spec(response_id="resp_hol_template"))
    body = {"spec": stored, "dialect": "responses", "background": True, "keyed": False, "streamed": False,
            "extra": {}, "workspace_id": tenant.workspace_id}
    import json as _json

    with db.connection() as con:
        con.execute(
            """
            INSERT INTO api_responses (id, project_id, workspace_id, model, status, background, request_id,
                                       resumable, dialect, engine, enqueued_at)
            SELECT 'resp_hol_' || g, %s, %s, 'techsara-35b', 'queued', true, 'r', true, 'responses', 'main',
                   now() - make_interval(secs => 1000 - g)
            FROM generate_series(1, 60) g
            """,
            (tenant.project["id"], tenant.workspace_id),
        )
        con.execute(
            """
            INSERT INTO api_response_requests (response_id, spec)
            SELECT 'resp_hol_' || g, jsonb_set(%s::jsonb, '{spec,response_id}', to_jsonb('resp_hol_' || g))
            FROM generate_series(1, 60) g
            """,
            (_json.dumps(body),),
        )
    router_spec = make_spec(model="techsara-8b-vision", engine="router", gate_engine="router",
                            max_tokens=16, planned_max_output_tokens=16, context_window=24_576)
    monkeypatch.setattr(durable, "DISPATCH_SCAN_PER_ENGINE", 20)  # fewer than the main rows ahead of it

    async def scenario():
        runtime = _runtime(tmp_path)
        runtime._started_at = time.monotonic() - 3600
        await runtime.launch(router_spec, caller=tenant.caller(), background=True)
        runtime._launched_here.clear()  # as after a restart: a leftover row
        async with capacity.hold("main.normal"):
            for _ in range(5):
                await runtime.dispatch_once()
                await asyncio.sleep(0.05)
            for _ in range(100):
                if durable_store.get_run(router_spec.response_id)["status"] == "completed":
                    break
                await asyncio.sleep(0.05)
            router_row = durable_store.get_run(router_spec.response_id)
            main_claimed = sum(1 for rid in runtime.runs if rid.startswith("resp_hol_"))
            for run in list(runtime.runs.values()):
                runtime.cancel_local(run.id)
                await asyncio.wait_for(run.done.wait(), 10)
        return router_row, main_claimed

    router_row, main_claimed = asyncio.run(scenario())
    assert router_row["status"] == "completed" and len(sidecar.calls) == 1
    assert main_claimed <= 1  # the main group got one representative, no more


def test_a_background_row_is_not_starved_by_a_steady_foreground_backlog(monkeypatch, tmp_path):
    """Review P5: `has_room` is false whenever anyone waits, and released slots
    go straight to waiters — so with foreground requests always queued, a
    background row was never claimed. Now it takes a place in the FIFO line
    and runs in its turn."""
    set_setting(monkeypatch, "PUBLIC_API_MAIN_NORMAL_MAX_CONCURRENT", "1")
    FakeMainEngine(answer_tokens=3).install(monkeypatch)
    tenant = make_tenant()
    spec = make_spec()

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        stop = asyncio.Event()
        served = {"foreground": 0}

        async def foreground():
            # Each foreground holder queues its successor BEFORE it releases,
            # so the gate always has a waiter.
            successor = None
            async with capacity.hold("main.normal"):
                if not stop.is_set():
                    successor = asyncio.ensure_future(foreground())
                    await asyncio.sleep(0.02)
                served["foreground"] += 1
                await asyncio.sleep(0.03)
            if successor is not None:
                await successor

        chain = asyncio.ensure_future(foreground())
        try:
            await asyncio.sleep(0.1)
            await runtime.launch(spec, caller=tenant.caller(), background=True)
            status = None
            for _ in range(100):
                await asyncio.sleep(0.05)
                status = durable_store.get_run(spec.response_id)["status"]
                if status == "completed":
                    break
            return status, served["foreground"]
        finally:
            stop.set()
            await asyncio.wait_for(chain, 20)
            await runtime.stop()

    status, served = asyncio.run(scenario())
    assert status == "completed"
    assert served > 3  # the foreground backlog really was steady


def test_an_applied_schema_is_checked_without_waiting_for_a_lock_on_api_responses():
    """Review P6: every process start re-ran `ALTER TABLE ... ADD COLUMN IF NOT
    EXISTS`, which needs ACCESS EXCLUSIVE even when nothing is missing — one
    parked transaction that had read api_responses failed start-up. The check
    reads the catalogs and returns at once, even under ACCESS EXCLUSIVE."""
    durable_store.ensure_schema()
    durable_store.reset_schema_cache()
    blocker = psycopg.connect(db.dsn(), autocommit=False)
    try:
        blocker.execute("LOCK TABLE api_responses IN ACCESS EXCLUSIVE MODE")
        started = time.monotonic()
        durable_store.ensure_schema(attempts=1)
        took = time.monotonic() - started
    finally:
        blocker.rollback()
        blocker.close()
    assert took < 1.0


def test_the_runtime_stays_inactive_instead_of_raising_when_its_schema_cannot_be_applied(monkeypatch, tmp_path):
    """Review P6: `durable.start()` must never fail the orchestrator lifespan
    (chat included). A runtime without its schema stays inactive, so background
    jobs take the legacy path, and a launch is a retryable 503."""
    def refuse(**_kwargs):
        raise psycopg.errors.LockNotAvailable("lock timeout")

    monkeypatch.setattr(durable_store, "ensure_schema", refuse)
    tenant = make_tenant()

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        started = runtime.started
        with pytest.raises(errors.ApiError) as refused:
            await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True)
        return started, refused.value

    started, refusal = asyncio.run(scenario())
    assert started is False
    assert refusal.code == "model_unavailable" and refusal.status == 503


def test_a_sync_waiter_ends_within_a_fraction_of_a_second_of_a_suspend(monkeypatch, tmp_path):
    """Review P7: `suspend_all` aborts readers, and a synchronous waiter now
    wakes on that at once instead of at its next heartbeat (15 s in
    production), so the gateway re-attaches and uvicorn shuts down promptly.
    Heartbeats still arrive on their own clock."""
    FakeMainEngine(answer_tokens=100_000, delay_s=0.01).install(monkeypatch)
    tenant = make_tenant()
    beats = []

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), keyed=True)

            async def on_heartbeat():
                beats.append(time.monotonic())

            async def nothing():
                return None

            # The production heartbeat is 15 s; 3 s here is long enough that
            # waking only at a heartbeat is visible.
            slow = asyncio.ensure_future(handle.wait(heartbeat=3.0, on_heartbeat=nothing))
            beating = asyncio.ensure_future(handle.wait(heartbeat=0.2, on_heartbeat=on_heartbeat))
            await asyncio.sleep(0.7)
            t0 = time.monotonic()
            await runtime.suspend_all("restart")
            ended = []
            for waiter in (slow, beating):
                with pytest.raises(durable.FollowerAborted):
                    await waiter
                ended.append(time.monotonic() - t0)
            return ended
        finally:
            await runtime.stop()

    ended = asyncio.run(scenario())
    assert max(ended) < 0.5
    assert 2 <= len(beats) <= 5  # ~0.7 s of 0.2 s heartbeats, while tokens were committing


def test_narrowing_the_models_a_key_or_project_allows_stops_a_running_job_at_the_next_lease_tick(monkeypatch, tmp_path):
    """Review P8: the mid-run re-check passes the run's model, and the
    authorisation shim applies the resolver's allowed_models narrowing."""
    FakeMainEngine(answer_tokens=100_000, delay_s=0.005).install(monkeypatch)
    tenant = make_tenant()

    async def scenario(scope):
        runtime = durable.Runtime()
        runtime.configure(owner=f"test-auth-{scope}", view=FakeController(time.monotonic, state="READY"),
                          blob_store=blobs.BlobStore(tmp_path / "blobs"))  # the DEFAULT authoriser
        runtime.witnesses_enabled = False
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True, keyed=True)
            follower = asyncio.ensure_future(collect(handle))
            await asyncio.sleep(0.2)
            await runtime.lease_tick()
            still_running = not handle.run.terminal
            table = "api_projects" if scope == "project" else "api_keys"
            row_id = tenant.project["id"] if scope == "project" else tenant.keys[0]["id"]
            with db.connection() as con:
                con.execute(f"UPDATE {table} SET allowed_models = '[\"techsara-8b-vision\"]'::jsonb WHERE id = %s",
                            (row_id,))
            await runtime.lease_tick()
            try:
                await asyncio.wait_for(asyncio.shield(handle.run.done.wait()), 5)
            except asyncio.TimeoutError:
                runtime.cancel_local(handle.id)  # not stopped: end it, the assertions below fail
                await asyncio.wait_for(handle.run.done.wait(), 10)
            await follower
            with db.connection() as con:
                con.execute(f"UPDATE {table} SET allowed_models = '[]'::jsonb WHERE id = %s", (row_id,))
            return still_running, durable_store.get_run(handle.id)
        finally:
            await runtime.stop()

    for scope in ("key", "project"):
        still_running, row = asyncio.run(scenario(scope))
        assert still_running is True
        assert row["status"] == "failed" and row["error_code"] == "invalid_api_key", scope
        assert row["metadata"]["should_retry"] is False


def test_the_authorisation_shim_applies_every_narrowing_level_and_the_tenancy_checks():
    tenant = make_tenant(keys=1, service_account_keys=1)
    plain, account_key = tenant.keys[0]["id"], tenant.keys[1]["id"]
    both = [plain, account_key]
    assert durable_store.still_authorised_shim(both, "techsara-35b") == set(both)
    with db.connection() as con:
        con.execute("UPDATE api_service_accounts SET allowed_models = '[\"techsara-ocr\"]'::jsonb WHERE id = %s",
                    (tenant.service_account["id"],))
    assert durable_store.still_authorised_shim(both, "techsara-35b") == {plain}
    assert durable_store.still_authorised_shim(both, "techsara-ocr") == set(both)
    assert durable_store.still_authorised_shim(both, None) == set(both)  # no model: status rungs only
    with db.connection() as con:
        con.execute("INSERT INTO workspaces (id, name) VALUES ('ws-elsewhere', 'Elsewhere') ON CONFLICT (id) DO NOTHING")
        con.execute("UPDATE api_keys SET workspace_id = %s WHERE id = %s", ("ws-elsewhere", plain))
    assert plain not in durable_store.still_authorised_shim(both, "techsara-ocr")


def test_a_run_whose_prefill_every_chat_turn_preempts_keeps_its_place_after_three_yields(monkeypatch, tmp_path):
    """Review P9: each yield throws a re-prefill away. After
    PUBLIC_API_MAX_YIELDS_WITHOUT_PROGRESS (3) yields with no first token in
    between, further yields are refused until the run's next first token, so
    the run makes progress and completes."""
    monkeypatch.setattr(capacity, "chat_long_admission_present", lambda: False)

    async def prefill(engine, call, index):
        if index == call.start_index:
            await asyncio.sleep(0.25)  # every attempt's (re-)prefill

    FakeMainEngine(answer_tokens=50, delay_s=0.001, before_token=prefill).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        runtime = _runtime(tmp_path)
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True, keyed=True)
            follower = asyncio.ensure_future(collect(handle))
            refused = 0
            deadline = time.monotonic() + 15.0
            while not handle.run.terminal and time.monotonic() < deadline:
                await asyncio.sleep(0.1)  # a chat LONG turn arrives during every prefill
                if not handle.run.terminal and not runtime.request_yield(handle.id):
                    refused += 1
            if not handle.run.terminal:
                runtime.cancel_local(handle.id)
            records = await follower
            return handle, records, refused
        finally:
            await runtime.stop()

    handle, records, refused = asyncio.run(scenario())
    row = durable_store.get_run(handle.id)
    assert records[-1][1] == "response.completed"
    text = "".join(r[2]["delta"] for r in records if r[1] == "response.output_text.delta")
    assert text == expected_text(50)
    assert row["yields"] >= 3 and refused >= 1
    assert row["stalled_attempts"] == 0


def test_two_concurrent_claims_of_one_run_in_one_process_start_one_runner(monkeypatch, tmp_path):
    """Found while testing the review fixes: the store lets an owner re-take
    its own lease, so the dispatcher and an attach (or the follower poller)
    claiming the same row at once each started a runner. One claim at a time
    per run per process now."""
    engine = FakeMainEngine(answer_tokens=20, delay_s=0.005).install(monkeypatch)
    tenant = make_tenant()
    launched = make_spec()

    async def scenario():
        first = _runtime(tmp_path, "A")
        await first.start()
        await first.launch(launched, caller=tenant.caller(), streamed=True, keyed=True)
        await asyncio.sleep(0.05)
        await first.suspend_all("restart")
        await first.stop()
        second = _runtime(tmp_path, "B")
        await second.start()
        try:
            calls_before = len(engine.calls)
            runs = await asyncio.gather(*(second.claim_and_resume(launched.response_id) for _ in range(4)))
            live = [run for run in runs if run is not None]
            handle = durable.Handle(second, launched.response_id, live[0])
            records = await collect(handle)
            return live, records, len(engine.calls) - calls_before
        finally:
            await second.stop()

    live, records, calls = asyncio.run(scenario())
    assert len({id(run) for run in live}) == 1
    assert calls == 1
    text = "".join(r[2]["delta"] for r in records if r[1] == "response.output_text.delta")
    assert text.endswith(expected_text(20)[-8:]) and records[-1][1] == "response.completed"


def test_a_cancel_that_lands_while_this_process_is_claiming_the_row_is_honoured_before_any_engine_work(monkeypatch, tmp_path):
    """The dispatcher picked a queued row and its claim is in flight (it read
    the row before the cancel flag was written). The cancel waits for that
    claim and stops the run at once, not at the next lease tick."""
    from app.publicapi import background

    engine = FakeMainEngine(answer_tokens=20, delay_s=0.005).install(monkeypatch)
    tenant = make_tenant()
    launched = make_spec()
    real_get_spec = durable_store.get_spec
    entered = {"at": None}

    def slow_get_spec(response_id):
        entered["at"] = time.monotonic()
        time.sleep(0.4)
        return real_get_spec(response_id)

    async def scenario():
        runtime = _runtime(tmp_path)
        monkeypatch.setattr(durable, "RUNTIME", runtime)
        await runtime.launch(launched, caller=tenant.caller(), background=True)
        monkeypatch.setattr(durable_store, "get_spec", slow_get_spec)
        await runtime.start()
        try:
            for _ in range(200):
                if entered["at"] is not None:
                    break
                await asyncio.sleep(0.005)
            asked = time.monotonic()
            await background.request_cancel(launched.response_id, tenant.project["id"])
            for _ in range(200):
                if durable_store.get_run(launched.response_id)["status"] == "cancelled":
                    break
                await asyncio.sleep(0.01)
            return time.monotonic() - asked
        finally:
            await runtime.stop()

    took = asyncio.run(scenario())
    assert durable_store.get_run(launched.response_id)["status"] == "cancelled"
    assert took < 2.0
    assert engine.calls == []
