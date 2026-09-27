"""Restart and resume across processes (no-timeout design, 2026-09-13).

Two `durable.Runtime` instances with different lease owners stand for the
process being replaced (A) and its replacement (B). Everything they share is
PostgreSQL, exactly as in production, so every property here is a property of
the log and the lease, not of shared memory.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app import db
from app.publicapi import blobs, capacity, durable, durable_store
from tests.publicapi_fake_engine import (
    set_setting,
    FakeController,
    FakeMainEngine,
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
    monkeypatch.setattr(blobs, "min_free_disk_bytes", lambda: 0)
    yield


async def _allow_all(keys):
    return set(keys)


def _runtime(tmp_path, owner, **kwargs):
    runtime = durable.Runtime()
    runtime.configure(
        owner=f"test-{owner}", view=FakeController(time.monotonic, state="READY"),
        authoriser=kwargs.pop("authoriser", _allow_all), blob_store=blobs.BlobStore(tmp_path / "blobs"),
        **kwargs,
    )
    return runtime


def _text(records):
    return "".join(r[2]["delta"] for r in records if r[1] == "response.output_text.delta")


class _SteppedClock:
    """Stands in for the `time` module inside `durable`: `monotonic()` moves
    only when the test moves it, everything else is the real module."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


def _append(owner, response_id, seqs, text="x"):
    return durable_store.append(owner, {
        response_id: [(s, "response.output_text.delta", {"delta": text, "_t": 1}) for s in seqs]
    })


def _durable_row(tenant, *, owner=None, background=False):
    row = db.create_api_response(
        tenant.project["id"], tenant.workspace_id, "techsara-35b", "r", key_id=tenant.keys[0]["id"],
        background=background, status="in_progress",
    )
    durable_store.mark_durable(row["id"], dialect="responses", item_id="msg_1", engine="main",
                               owner=owner, lease_ttl_s=60)
    return row


# ------------------------------------------------------ suspend → claim --


def test_a_run_suspended_in_a_is_claimed_by_b_within_two_seconds_and_its_text_continues_exactly(monkeypatch, tmp_path):
    engine = FakeMainEngine(answer_tokens=200, delay_s=0.002).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        a = _runtime(tmp_path, "A")
        await a.start()
        handle = await a.launch(make_spec(), caller=tenant.caller(), streamed=True, keyed=True)
        seen = []
        try:
            async for item in handle.follow(0, heartbeat=0.05):
                if item is not durable.HEARTBEAT:
                    seen.append(item)
                if len(seen) >= 6:
                    await a.suspend_all("restart")
        except durable.FollowerAborted:
            pass
        await a.stop()
        row = durable_store.get_run(handle.id)
        suspended_at = time.monotonic()
        b = _runtime(tmp_path, "B")
        await b.start()
        try:
            resumed = await b.attach(handle.id, caller=tenant.caller())
            claimed_after = time.monotonic() - suspended_at
            rest = await collect(resumed, seen[-1][0])
            everything = await collect(await b.attach(handle.id, caller=tenant.caller()), 0)
            return row, seen, rest, everything, claimed_after
        finally:
            await b.stop()

    row, seen, rest, everything, claimed_after = asyncio.run(scenario())
    assert row["suspend_reason"] == "restart" and row["lease_owner"] is None
    assert claimed_after < 2.0
    assert rest[0][0] == seen[-1][0] + 1  # the reader rejoins exactly where it left
    assert [r[0] for r in everything] == list(range(1, len(everything) + 1))
    assert _text(everything) == expected_text(200)  # no duplicate, no gap
    assert [r[1] for r in everything].count("response.created") == 1
    assert engine.calls[-1].kwargs["continue_final_message"] is True
    assert engine.calls[-1].start_index > 0
    final = durable_store.get_run(everything[0][2]["response"]["id"])
    assert final["status"] == "completed" and final["stalled_attempts"] == 0 and final["attempt"] == 2
    # Settled by B, whose process never saw the request: its recorder writes
    # the ONE usage row, with input counted once and the re-prefill recorded.
    # The settling recorder writes after the terminal event is visible; on a
    # slow CI runner that row can land a moment after stop() returns, so wait
    # for it (bounded) instead of reading once.
    deadline = time.monotonic() + 10
    while True:
        with db.connection() as con:
            usage_rows = con.execute(
                "SELECT input_tokens, output_tokens, meta FROM usage_events WHERE generation_id = %s",
                (final["id"],),
            ).fetchall()
        if usage_rows or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    assert len(usage_rows) == 1
    assert usage_rows[0]["output_tokens"] == 200 and usage_rows[0]["input_tokens"] == 17
    assert usage_rows[0]["meta"]["settled_by"] == "durable"
    assert usage_rows[0]["meta"]["resume_count"] == 1
    assert final["recomputed_prompt_tokens"] and final["recomputed_prompt_tokens"] > 17


def test_a_claim_waits_for_an_in_flight_append_and_resumes_after_its_last_event():
    """The FOR SHARE / FOR UPDATE ordering, through the REAL `append`: its
    insert is slowed by a temporary trigger, and a claim started meanwhile
    (A's lease has lapsed) blocks until the append commits — so its resume
    point includes A's batch instead of re-generating it."""
    tenant = make_tenant()
    row = _durable_row(tenant, owner="A")
    assert _append("A", row["id"], [1, 2]).committed
    with db.connection() as con:
        con.execute("UPDATE api_responses SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
                    (row["id"],))
        con.execute(
            "CREATE OR REPLACE FUNCTION t2_slow_event() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN PERFORM pg_sleep(0.6); RETURN NEW; END $$"
        )
        con.execute(
            "CREATE TRIGGER t2_slow_event BEFORE INSERT ON api_response_events "
            "FOR EACH ROW EXECUTE FUNCTION t2_slow_event()"
        )
    try:
        result = {}

        def append_in_flight():
            result["append"] = _append("A", row["id"], [3], text="z")
            result["append_at"] = time.monotonic()

        def claim():
            result["claim"] = durable_store.claim(row["id"], "B", lease_ttl_s=60)
            result["claim_at"] = time.monotonic()

        appender = threading.Thread(target=append_in_flight)
        claimer = threading.Thread(target=claim)
        appender.start()
        time.sleep(0.2)  # the append holds FOR SHARE and sits in its insert
        claimer.start()
        appender.join(10)
        claimer.join(10)
    finally:
        with db.connection() as con:
            con.execute("DROP TRIGGER IF EXISTS t2_slow_event ON api_response_events")
            con.execute("DROP FUNCTION IF EXISTS t2_slow_event()")
    assert result["append"].committed == {row["id"]: [3]}
    claimed = result["claim"]
    assert claimed is not None and result["claim_at"] >= result["append_at"] - 0.05
    assert claimed.last_sequence == 3 and claimed.emitted_text == "xxz"


def test_a_crash_with_a_lagging_heartbeat_resumes_from_the_log_not_the_counter():
    tenant = make_tenant()
    row = _durable_row(tenant, owner="A")
    for start in range(1, 151, 50):
        _append("A", row["id"], range(start, start + 50))
    with db.connection() as con:
        # The heartbeat last reported 10 tokens, then the process was killed:
        # no release, the lease simply lapses.
        con.execute(
            "UPDATE api_responses SET generated_tokens = 10, lease_expires_at = now() - interval '1 second' "
            "WHERE id = %s", (row["id"],),
        )
    claimed = durable_store.claim(row["id"], "B", lease_ttl_s=60)
    assert claimed.last_sequence == 150
    assert claimed.generated_tokens == 150 and len(claimed.emitted_text) == 150


def test_deleting_a_project_mid_run_stops_only_that_job():
    doomed = make_tenant("ws-doomed")
    alive = make_tenant("ws-alive")
    first = _durable_row(doomed, owner="A")
    second = _durable_row(alive, owner="A")
    with db.connection() as con:
        con.execute("DELETE FROM api_projects WHERE id = %s", (doomed.project["id"],))
    result = durable_store.append("A", {
        first["id"]: [(1, "response.created", {})],
        second["id"]: [(1, "response.created", {}), (2, "response.in_progress", {})],
    })
    assert result.lost == {first["id"]}
    assert result.committed == {second["id"]: [1, 2]}
    assert len(durable_store.list_events(second["id"])) == 2


def test_two_claims_race_and_exactly_one_wins():
    tenant = make_tenant()
    row = _durable_row(tenant, owner=None)
    barrier = threading.Barrier(8)
    winners = []

    def contender(n):
        barrier.wait()
        if durable_store.claim(row["id"], f"owner-{n}", lease_ttl_s=60) is not None:
            winners.append(n)

    threads = [threading.Thread(target=contender, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(winners) == 1
    assert durable_store.get_run(row["id"])["lease_owner"] == f"owner-{winners[0]}"


def test_a_split_brain_writer_conflicts_on_its_sequence_and_loses_only_its_own_lease():
    tenant = make_tenant()
    row = _durable_row(tenant, owner="A")
    assert _append("A", row["id"], [1, 2]).committed
    with db.connection() as con:
        con.execute("UPDATE api_responses SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
                    (row["id"],))
    claimed = durable_store.claim(row["id"], "B", lease_ttl_s=60)
    assert claimed.last_sequence == 2
    # A still believes it owns the run: its next write is refused as a lease
    # loss (the lease moved), and B's own write of seq 3 commits.
    assert _append("A", row["id"], [3]).lost == {row["id"]}
    assert _append("B", row["id"], [3]).committed == {row["id"]: [3]}
    # Even if the lease had not moved, a reused sequence number is a lease
    # loss for that job, never a duplicate event.
    assert _append("B", row["id"], [3, 4]).lost == {row["id"]}
    assert [s for s, _, _ in durable_store.list_events(row["id"])] == [1, 2, 3]


def test_a_writer_whose_lease_was_taken_stops_and_the_new_owner_finishes(monkeypatch, tmp_path):
    FakeMainEngine(answer_tokens=150, delay_s=0.003).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        a = _runtime(tmp_path, "A")
        await a.start()
        handle = await a.launch(make_spec(), caller=tenant.caller(), streamed=True, keyed=True)
        while handle.run.committed_seq < 5:
            await asyncio.sleep(0.005)
        with db.connection() as con:  # A's lease lapses (a long GC pause, a partition)
            con.execute("UPDATE api_responses SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
                        (handle.id,))
        b = _runtime(tmp_path, "B")
        await b.start()
        try:
            taken = await b.claim_and_resume(handle.id)
            assert taken is not None
            for _ in range(200):
                await asyncio.sleep(0.01)
                if a.stats["lease_lost"]:
                    break
            everything = await collect(await b.attach(handle.id, caller=tenant.caller()), 0)
            return a.stats["lease_lost"], everything
        finally:
            await a.stop()
            await b.stop()

    lost, everything = asyncio.run(scenario())
    assert lost == 1
    assert [r[0] for r in everything] == list(range(1, len(everything) + 1))
    assert _text(everything) == expected_text(150)


# ------------------------------------------------------- authorisation --


def test_a_revoked_key_mid_run_closes_the_engine_stream_at_the_next_lease_tick(monkeypatch, tmp_path):
    engine = FakeMainEngine(answer_tokens=100_000, delay_s=0.003).install(monkeypatch)
    tenant = make_tenant()
    revoked = set()

    async def authoriser(keys):
        return {k for k in keys if k not in revoked}

    async def scenario():
        runtime = _runtime(tmp_path, "A", authoriser=authoriser)
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True, background=False)
            while handle.run.generated_tokens < 10:
                await asyncio.sleep(0.005)
            revoked.add(tenant.keys[0]["id"])
            db.revoke_api_key(tenant.keys[0]["id"], tenant.workspace_id)
            ticked = time.monotonic()
            await runtime.lease_tick()
            await asyncio.wait_for(handle.run.done.wait(), 5)
            return handle, time.monotonic() - ticked
        finally:
            await runtime.stop()

    handle, closed_in = asyncio.run(scenario())
    row = durable_store.get_run(handle.id)
    assert row["status"] == "failed" and row["error_code"] == "invalid_api_key"
    assert closed_in < 1.0 and engine.open_streams == 0
    assert (row["metadata"] or {}).get("should_retry") is False


def test_a_suspended_run_of_a_revoked_key_is_never_dispatched(monkeypatch, tmp_path):
    engine = FakeMainEngine(answer_tokens=100_000, delay_s=0.003).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        a = _runtime(tmp_path, "A")
        await a.start()
        handle = await a.launch(make_spec(), caller=tenant.caller(), streamed=True, keyed=True)
        while handle.run.generated_tokens < 5:
            await asyncio.sleep(0.005)
        await a.suspend_all("restart")
        await a.stop()
        calls = len(engine.calls)
        db.revoke_api_key(tenant.keys[0]["id"], tenant.workspace_id)
        b = durable.Runtime()
        b.configure(owner="test-B", view=FakeController(time.monotonic, state="READY"),
                    blob_store=blobs.BlobStore(tmp_path / "blobs"))  # the REAL shim authoriser
        run = await b.claim_and_resume(handle.id)
        return handle, run, calls

    handle, run, calls = asyncio.run(scenario())
    assert run is None
    assert len(engine.calls) == calls
    row = durable_store.get_run(handle.id)
    assert row["status"] == "failed" and row["error_code"] == "invalid_api_key"


def test_the_authorisation_shim_refuses_every_rung_identically():
    tenant = make_tenant(keys=4)
    active, revoked, expired, rotated = (k["id"] for k in tenant.keys)
    db.revoke_api_key(revoked, tenant.workspace_id)
    with db.connection() as con:
        con.execute("UPDATE api_keys SET expires_at = now() - interval '1 minute' WHERE id = %s", (expired,))
        con.execute("UPDATE api_keys SET rotation_expires_at = now() - interval '1 minute' WHERE id = %s", (rotated,))
    assert durable_store.still_authorised_shim([active, revoked, expired, rotated]) == {active}
    with db.connection() as con:
        con.execute("UPDATE api_projects SET status = 'disabled' WHERE id = %s", (tenant.project["id"],))
    assert durable_store.still_authorised_shim([active]) == set()


# ---------------------------------------------------------------- sweeps --


def test_an_unread_suspended_stream_costs_no_engine_work_and_is_cancelled_at_ttl(monkeypatch, tmp_path):
    engine = FakeMainEngine(answer_tokens=100_000, delay_s=0.003).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        a = _runtime(tmp_path, "A")
        await a.start()
        handle = await a.launch(make_spec(), caller=tenant.caller(), streamed=True)
        while handle.run.generated_tokens < 8:
            await asyncio.sleep(0.005)
        await a.suspend_all("restart")
        await a.stop()
        calls = len(engine.calls)
        b = _runtime(tmp_path, "B")
        early = await b.sweep_once()
        with db.connection() as con:
            con.execute("UPDATE api_responses SET suspended_at = now() - interval '901 seconds' WHERE id = %s",
                        (handle.id,))
        late = await b.sweep_once()
        return handle, calls, early, late

    handle, calls, early, late = asyncio.run(scenario())
    assert early["unread_cancelled"] == 0 and late["unread_cancelled"] == 1
    assert len(engine.calls) == calls
    row = durable_store.get_run(handle.id)
    assert row["status"] == "cancelled" and row["output_tokens"] >= 8
    assert durable_store.get_spec(handle.id) is None


def test_background_resumes_are_staggered_oldest_first_after_continuity(monkeypatch, tmp_path):
    FakeMainEngine(answer_tokens=3).install(monkeypatch)
    set_setting(monkeypatch, "PUBLIC_API_RESUME_STAGGER_S", "0.3")
    tenant = make_tenant()
    ids = []
    for n in range(3):
        row = _durable_row(tenant, owner=None, background=True)
        with db.connection() as con:
            con.execute(
                "UPDATE api_responses SET enqueued_at = now() - make_interval(secs => %s) WHERE id = %s",
                (100 - n, row["id"]),
            )
        durable_store.put_spec(row["id"], {"spec": durable.spec_to_json(make_spec(row["id"]))[0],
                                           "dialect": "responses"})
        ids.append(row["id"])
    claims = []
    real_claim = durable_store.claim
    # The dispatcher compares `time.monotonic()` against its own last claim.
    # Reading the same stepped clock here, a claim is recorded at the instant
    # the dispatcher measured, not after a thread hop and a database round
    # trip on a busy host, so the gaps are the stagger and nothing else.
    clock = _SteppedClock()
    tick = 1 / 16  # binary-exact: the sums carry no float noise

    def recording_claim(response_id, owner, **kwargs):
        claimed = real_claim(response_id, owner, **kwargs)
        if claimed is not None:
            claims.append((response_id, clock.monotonic()))
        return claimed

    monkeypatch.setattr(durable_store, "claim", recording_claim)

    async def scenario():
        continuity = asyncio.Event()
        b = _runtime(tmp_path, "B")  # configured first: its liveness clock stays real
        monkeypatch.setattr(durable, "time", clock)
        b._started_at = clock.monotonic()
        b.continuity_done = continuity
        before = await b.dispatch_once()
        continuity.set()
        while len(claims) < 3 and clock.monotonic() < 5:
            await b.dispatch_once()
            for run in list(b.runs.values()):
                await asyncio.wait_for(run.done.wait(), 5)
            clock.advance(tick)
        return before

    before = asyncio.run(scenario())
    assert before == 0  # nothing before chat's continuity sweep (or the stagger)
    assert [rid for rid, _ in claims] == ids  # oldest enqueued first
    gaps = [b - a for (_, a), (_, b) in zip(claims, claims[1:])]
    assert all(0.3 <= gap < 0.3 + tick for gap in gaps), gaps  # one per stagger, at the first tick after it
    assert all(durable_store.get_run(rid)["status"] == "completed" for rid in ids)


def test_resume_disabled_fails_a_suspended_run_retryably_and_keeps_its_log(monkeypatch, tmp_path):
    engine = FakeMainEngine(answer_tokens=100_000, delay_s=0.003).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        a = _runtime(tmp_path, "A")
        await a.start()
        handle = await a.launch(make_spec(), caller=tenant.caller(), streamed=True)
        while handle.run.committed_seq < 4:
            await asyncio.sleep(0.005)
        await a.suspend_all("restart")
        await a.stop()
        calls = len(engine.calls)
        set_setting(monkeypatch, "PUBLIC_API_RESUME_ENABLED", "false")
        b = _runtime(tmp_path, "B")
        await b.start()
        try:
            attached = await b.attach(handle.id, caller=tenant.caller())
            records = await collect(attached)
            return handle, calls, records
        finally:
            await b.stop()

    handle, calls, records = asyncio.run(scenario())
    assert len(engine.calls) == calls
    assert records[-1][1] == "response.failed"
    assert records[-1][2]["response"]["error"]["code"] == "model_unavailable"
    assert durable_store.get_run(handle.id)["status"] == "failed"


def test_a_long_generation_survives_five_restarts_with_identical_text_and_no_stalled_count(monkeypatch, tmp_path):
    """The scaled four-hour run: five deploys during one answer, each resumed
    by a new process, the answer byte-identical to an uninterrupted one."""
    total = 1500
    engine = FakeMainEngine(answer_tokens=total, delay_s=0.0005).install(monkeypatch)
    tenant = make_tenant()
    launched = make_spec()

    async def scenario():
        runtime = _runtime(tmp_path, "gen0")
        await runtime.start()
        await runtime.launch(launched, caller=tenant.caller(), streamed=True, keyed=True)
        for n in range(1, 6):
            target = n * total // 7
            while True:
                run = runtime.runs.get(launched.response_id)
                if run is None or run.terminal or run.generated_tokens >= target:
                    break
                await asyncio.sleep(0.002)
            await runtime.suspend_all("restart")
            await runtime.stop()
            runtime = _runtime(tmp_path, f"gen{n}")
            await runtime.start()
            await runtime.attach(launched.response_id, caller=tenant.caller())
        try:
            return await collect(await runtime.attach(launched.response_id, caller=tenant.caller()), 0, limit_s=60)
        finally:
            await runtime.stop()

    records = asyncio.run(scenario())
    assert _text(records) == expected_text(total)
    assert [r[0] for r in records] == list(range(1, len(records) + 1))
    row = durable_store.get_run(launched.response_id)
    assert row["status"] == "completed" and row["stalled_attempts"] == 0
    assert row["attempt"] == 6 and len(engine.calls) == 6


def test_a_sync_caller_that_attaches_after_a_restart_gets_the_whole_text_and_usage(monkeypatch, tmp_path):
    """The sync path across a deploy: B settles the run; the outcome a body
    is rendered from is rebuilt from the row and the log."""
    FakeMainEngine(answer_tokens=90, delay_s=0.002).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        a = _runtime(tmp_path, "A")
        await a.start()
        handle = await a.launch(make_spec(), caller=tenant.caller(), keyed=True)
        while handle.run.generated_tokens < 20:
            await asyncio.sleep(0.002)
        await a.suspend_all("restart")
        await a.stop()
        b = _runtime(tmp_path, "B")
        c = _runtime(tmp_path, "C")
        await b.start()
        try:
            resumed = await b.attach(handle.id, caller=tenant.caller())
            local = await asyncio.wait_for(resumed.result(), 20)
            # C never started its loops: the replay still works (on-demand poller).
            remote = await asyncio.wait_for((await c.attach(handle.id, caller=tenant.caller())).result(), 20)
            return local, remote
        finally:
            await b.stop()
            await c.stop()

    local, remote = asyncio.run(scenario())
    for outcome in (local, remote):
        assert outcome.status == "completed" and outcome.text == expected_text(90)
        assert outcome.response().to_wire()["usage"]["output_tokens"] == 90


def test_a_cancel_written_while_another_process_held_the_run_is_honoured_at_claim(monkeypatch, tmp_path):
    """A's lease tick never saw the flag (it suspended first); B must not
    resume a run its caller cancelled."""
    engine = FakeMainEngine(answer_tokens=100_000, delay_s=0.003).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        a = _runtime(tmp_path, "A")
        await a.start()
        handle = await a.launch(make_spec(), caller=tenant.caller(), streamed=True, keyed=True)
        while handle.run.generated_tokens < 5:
            await asyncio.sleep(0.005)
        db.update_api_response(handle.id, tenant.project["id"], cancel_requested=True)
        await a.suspend_all("restart")
        await a.stop()
        calls = len(engine.calls)
        b = _runtime(tmp_path, "B")
        resumed = await b.claim_and_resume(handle.id)
        return handle, resumed, calls

    handle, resumed, calls = asyncio.run(scenario())
    assert resumed is None and len(engine.calls) == calls
    row = durable_store.get_run(handle.id)
    assert row["status"] == "cancelled" and row["output_tokens"] >= 5


def test_a_suspend_lets_the_stopped_attempt_be_recorded_before_the_lease_is_released(monkeypatch, tmp_path):
    """`metadata.attempts` is written under the lease by the runner after its
    attempt ends. A SIGTERM whose release won that race lost the entry, and
    the resumed run settled with resume_count 0 (2026-09-14). A slow progress
    write makes the race certain; the entry must still be stored."""
    FakeMainEngine(answer_tokens=100_000, delay_s=0.002).install(monkeypatch)
    tenant = make_tenant()
    real_update = durable_store.update_progress

    def slow_update(owner, response_id, **fields):
        if fields.get("metadata", {}).get("attempts"):
            time.sleep(0.3)
        return real_update(owner, response_id, **fields)

    monkeypatch.setattr(durable_store, "update_progress", slow_update)

    async def scenario():
        a = _runtime(tmp_path, "A")
        await a.start()
        try:
            handle = await a.launch(make_spec(), caller=tenant.caller(), streamed=True, keyed=True)
            seen = []
            try:
                async for item in handle.follow(0, heartbeat=0.05):
                    if item is not durable.HEARTBEAT:
                        seen.append(item)
                    if len(seen) >= 6:
                        await a.suspend_all("restart")
            except durable.FollowerAborted:
                pass
            return durable_store.get_run(handle.id)
        finally:
            await a.stop()

    row = asyncio.run(scenario())
    assert row["lease_owner"] is None and row["suspend_reason"] == "restart"
    attempts = row["metadata"].get("attempts") or []
    assert [a["reason"] for a in attempts] == ["restart"] and attempts[0]["dispatched"] is True
