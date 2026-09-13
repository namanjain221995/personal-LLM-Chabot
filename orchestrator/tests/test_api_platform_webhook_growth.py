"""The delivery table stays bounded: settle what can no longer be sent, prune
what is past retention, and keep normal deliveries retrying and signed.

WHY (2026-09-13, security review). `pending` deliveries whose endpoint had
been disabled were never claimed (the sweep only claims `active` endpoints)
and never pruned (`db.prune_api_platform` deletes only settled rows), so they
stayed until the endpoint or its project was deleted. With usage limits off,
nothing bounded how many could be queued before the endpoint was disabled.

What is pinned here, against real PostgreSQL with thousands of rows:

1. a disabled endpoint's pending deliveries are held for the grace window
   (so the documented pause still resumes), then settled as `dropped` with a
   reason, without moving the attempt counter or the endpoint's health;
2. the grace window runs from the endpoint's `disabled_at` only, so a short
   disable never drops an old backlog, and a backlog kept alive by toggling is
   bounded by the per-endpoint pending cap instead;
3. removing an endpoint removes its deliveries;
4. settled rows older than the retention window are pruned, pending and
   recent rows are not, in short batches that skip locked rows;
5. under a sustained queue-then-disable pattern the table size stays under
   a fixed ceiling set by the retention window;
6. an ordinary delivery still retries with backoff, is not settled while it
   is retrying, and arrives signed with the endpoint's secret;
7. the worker loop runs this maintenance between sweeps and survives it
   failing, each job failing on its own and backing off;
8. (second review, 2026-09-13) an endpoint holds at most a fixed number of
   pending deliveries whatever its status, events past that are counted on a
   bounded overflow record, a backlog drains continuously, and out-of-range
   settings fall back instead of raising or removing the grace window.
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.apiplatform.webhooks import queue, sender, signer, ssrf, worker
from app.config import settings

WORKSPACE = "ws-webhook-growth"
SECRET = "whsec_aaaaaaaaaaaaaaaaaaaaaaaaaaaa"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def project():
    with db.connection() as con:
        con.execute("INSERT INTO workspaces (id, name) VALUES (%s, %s)", (WORKSPACE, "Growth"))
    return db.create_api_project(WORKSPACE, "Growth project", "live")


def _endpoint(project, url="https://hooks.customer.example/growth"):
    return db.create_webhook_endpoint(
        project["id"], WORKSPACE, url, list(sender.SUBSCRIBABLE_EVENTS), SECRET
    )


@pytest.fixture()
def endpoint(project):
    return _endpoint(project)


@pytest.fixture(autouse=True)
def _fresh_maintenance_clock(monkeypatch):
    """`maintain` keeps its next-pass deadlines in module globals; every test
    starts with both passes due."""
    monkeypatch.setattr(worker, "_next_settle_at", 0.0)
    monkeypatch.setattr(worker, "_next_prune_at", 0.0)


def _bulk(endpoint_row, count, *, created_at, status="pending", prefix="bulk", attempt=0):
    """Insert `count` deliveries in one statement. The queue accessors are
    exercised elsewhere; here the point is the volume."""
    with db.connection() as con:
        con.execute(
            "INSERT INTO api_webhook_deliveries "
            "    (id, event_id, endpoint_id, project_id, event_type, payload, status, "
            "     attempt, next_attempt_at, created_at, delivered_at) "
            "SELECT %s || '_' || g::text, 'evt_' || %s || '_' || g::text, %s, %s, "
            "       'response.cancelled', '{}'::jsonb, %s, %s, "
            "       CASE WHEN %s = 'pending' THEN %s::timestamptz ELSE NULL END, "
            "       %s::timestamptz, "
            "       CASE WHEN %s = 'delivered' THEN %s::timestamptz ELSE NULL END "
            "  FROM generate_series(1, %s) g",
            (
                f"whd_{prefix}", prefix, endpoint_row["id"], endpoint_row["project_id"],
                status, attempt, status, created_at, created_at, status, created_at, count,
            ),
        )


def _counts(endpoint_id=None):
    with db.connection() as con:
        rows = con.execute(
            "SELECT status, count(*) AS n FROM api_webhook_deliveries "
            " WHERE (%s::text IS NULL OR endpoint_id = %s) GROUP BY status",
            (endpoint_id, endpoint_id),
        ).fetchall()
    return {row["status"]: int(row["n"]) for row in rows}


def _disable(endpoint_row, at):
    with db.connection() as con:
        con.execute(
            "UPDATE api_webhook_endpoints SET status = 'disabled', disabled_at = %s WHERE id = %s",
            (at, endpoint_row["id"]),
        )


def _drain_maintenance(now):
    """Run forced passes until one does nothing; returns the summed counts."""
    total = {"endpoint_disabled": 0, "expired": 0, "pruned": 0}
    for _ in range(100):
        counts = asyncio.run(worker.maintain(now=now, force=True))
        for key, value in counts.items():
            total[key] += value
        if not any(counts.values()):
            return total
    raise AssertionError("maintenance never came to rest")


# ---------------------------------------------------------------------------
# 1-3. Settling
# ---------------------------------------------------------------------------


def test_a_disabled_endpoints_backlog_is_held_for_the_grace_window_then_settled(project, endpoint):
    now = datetime.now(timezone.utc)
    _bulk(endpoint, 3000, created_at=now - timedelta(minutes=1))
    db.update_webhook_endpoint(endpoint["id"], WORKSPACE, status="disabled")
    health_before = db.get_webhook_endpoint(endpoint["id"], WORKSPACE)

    inside_grace = asyncio.run(worker.maintain(now=now, force=True))
    assert inside_grace["endpoint_disabled"] == 0
    assert _counts(endpoint["id"]) == {"pending": 3000}
    # The finding itself: the platform-wide prune never touches these rows.
    db.prune_api_platform(now=now + timedelta(days=400))
    assert _counts(endpoint["id"]) == {"pending": 3000}

    # Slack past the window: `disabled_at` came from the database clock a
    # moment after `now` was read.
    after_grace = now + timedelta(seconds=queue.disabled_grace_seconds() + 120)
    settled = _drain_maintenance(after_grace)

    assert settled["endpoint_disabled"] == 3000
    assert _counts(endpoint["id"]) == {"dropped": 3000}
    with db.connection() as con:
        shape = con.execute(
            "SELECT count(*) AS n, min(attempt) AS lo, max(attempt) AS hi, "
            "       count(next_attempt_at) AS scheduled, "
            "       count(*) FILTER (WHERE error = %s) AS with_reason "
            "  FROM api_webhook_deliveries WHERE endpoint_id = %s",
            (queue.REASON_ENDPOINT_DISABLED, endpoint["id"]),
        ).fetchone()
    # Not an attempt: the counter did not move, nothing is scheduled, and the
    # history row says why it was never sent.
    assert (shape["lo"], shape["hi"], shape["scheduled"], shape["with_reason"]) == (0, 0, 0, 3000)
    health_after = db.get_webhook_endpoint(endpoint["id"], WORKSPACE)
    assert health_after["consecutive_failures"] == health_before["consecutive_failures"]
    assert health_after["last_delivery_status"] == health_before["last_delivery_status"]

    # Re-enabling afterwards sends none of them: they are settled, not paused.
    db.update_webhook_endpoint(endpoint["id"], WORKSPACE, status="active")
    assert queue.claim_due_deliveries(200, per_project=200, now=after_grace) == []


def test_a_quick_disable_and_re_enable_inside_the_grace_window_still_resumes_the_queue(
    monkeypatch, project, endpoint
):
    now = datetime.now(timezone.utc)
    _bulk(endpoint, 50, created_at=now - timedelta(seconds=30))
    db.update_webhook_endpoint(endpoint["id"], WORKSPACE, status="disabled")

    asyncio.run(worker.maintain(now=now + timedelta(seconds=60), force=True))
    db.update_webhook_endpoint(endpoint["id"], WORKSPACE, status="active")

    claimed = queue.claim_due_deliveries(
        200, per_project=200, now=now + timedelta(seconds=61)
    )
    assert len(claimed) == 50


def test_a_short_disable_never_drops_a_backlog_older_than_the_grace_window(project, endpoint):
    """The second review's first finding. The settle rule used to have a
    second arm — any row older than the grace window — so a 30-second disable
    dropped an ordinary backlog that was only waiting for the sweep. The
    grace window now runs from `disabled_at` alone."""
    now = datetime.now(timezone.utc)
    grace = queue.disabled_grace_seconds()
    _bulk(endpoint, 1200, created_at=now - timedelta(seconds=grace + 60), prefix="old")
    _bulk(endpoint, 30, created_at=now - timedelta(seconds=10), prefix="new")
    db.update_webhook_endpoint(endpoint["id"], WORKSPACE, status="disabled")

    settled = _drain_maintenance(now + timedelta(seconds=30))
    db.update_webhook_endpoint(endpoint["id"], WORKSPACE, status="active")

    assert settled == {"endpoint_disabled": 0, "expired": 0, "pruned": 0}
    assert _counts(endpoint["id"]) == {"pending": 1230}
    claimed = queue.claim_due_deliveries(
        200, per_project=200, now=now + timedelta(seconds=31)
    )
    assert len(claimed) == 200 and all(row["attempt"] == 0 for row in claimed)


def test_re_enabling_restarts_the_grace_window_but_the_cap_bounds_what_is_kept(
    monkeypatch, project, endpoint
):
    """A re-enable clears `disabled_at`, so toggling restarts the grace
    window; what bounds a backlog kept that way is the pending cap at
    enqueue (and, past it, the retention window)."""
    monkeypatch.setattr(settings, "public_api_webhook_max_pending_per_endpoint", 40, raising=False)
    now = datetime.now(timezone.utc)
    grace = queue.disabled_grace_seconds()
    for cycle in range(5):
        db.update_webhook_endpoint(endpoint["id"], WORKSPACE, status="active")
        for index in range(60):
            queue.enqueue_delivery(
                endpoint["id"], project["id"], sender.RESPONSE_CANCELLED,
                f"evt_cycle{cycle}_{index}", {}, now=now,
            )
        db.update_webhook_endpoint(endpoint["id"], WORKSPACE, status="disabled")
        # Just inside the window: nothing is settled, nothing is lost.
        assert _drain_maintenance(now + timedelta(seconds=grace - 60))["endpoint_disabled"] == 0
        assert _counts(endpoint["id"]).get("pending") == 40

    counts = _counts(endpoint["id"])
    assert counts == {"pending": 40, "dropped": 1}  # one overflow record, not 260 rows
    with db.connection() as con:
        marker = con.execute(
            "SELECT payload, error FROM api_webhook_deliveries WHERE status = 'dropped'"
        ).fetchone()
    assert marker["payload"]["not_queued"] == 5 * 60 - 40
    assert "260 event(s)" in marker["error"] and "40 deliveries" in marker["error"]


def test_a_zero_grace_setting_settles_on_the_next_pass_and_a_bad_value_falls_back(
    monkeypatch, project, endpoint
):
    now = datetime.now(timezone.utc)
    _bulk(endpoint, 10, created_at=now)
    _disable(endpoint, now)

    # The environment path, whether or not config.py has declared the fields yet.
    monkeypatch.delattr(settings, "public_api_webhook_disabled_grace_seconds", raising=False)
    monkeypatch.delattr(settings, "public_api_webhook_delivery_retention_days", raising=False)
    monkeypatch.setenv("PUBLIC_API_WEBHOOK_DISABLED_GRACE_SECONDS", "not-a-number")
    assert queue.disabled_grace_seconds() == queue.DEFAULT_DISABLED_GRACE_SECONDS
    monkeypatch.setenv("PUBLIC_API_WEBHOOK_DELIVERY_RETENTION_DAYS", "0")
    assert queue.delivery_retention_days() == 1  # floored: zero would erase history at once
    monkeypatch.setenv("PUBLIC_API_WEBHOOK_DELIVERY_RETENTION_DAYS", "")
    assert queue.delivery_retention_days() == queue.DEFAULT_DELIVERY_RETENTION_DAYS

    monkeypatch.setenv("PUBLIC_API_WEBHOOK_DISABLED_GRACE_SECONDS", "0")
    assert queue.disabled_grace_seconds() == 0
    counts = asyncio.run(worker.maintain(now=now + timedelta(milliseconds=1), force=True))
    assert counts["endpoint_disabled"] == 10

    # A declared setting wins over the environment (the day config.py has it).
    monkeypatch.setattr(settings, "public_api_webhook_disabled_grace_seconds", 42, raising=False)
    assert queue.disabled_grace_seconds() == 42


def test_deleting_an_endpoint_removes_its_deliveries_with_it(project, endpoint):
    now = datetime.now(timezone.utc)
    other = _endpoint(project, "https://hooks.customer.example/kept")
    _bulk(endpoint, 2000, created_at=now, prefix="gone")
    _bulk(other, 5, created_at=now, prefix="kept")

    assert db.delete_webhook_endpoint(endpoint["id"], WORKSPACE) is True

    assert _counts(endpoint["id"]) == {}
    assert _counts(other["id"]) == {"pending": 5}


def test_an_active_endpoints_pending_rows_are_left_alone_until_the_retention_window_ends(
    project, endpoint
):
    now = datetime.now(timezone.utc)
    retention = queue.delivery_retention_days()
    _bulk(endpoint, 40, created_at=now - timedelta(days=retention - 1), prefix="young")
    _bulk(endpoint, 60, created_at=now - timedelta(days=retention, hours=1), prefix="stale")

    first = queue.settle_stale_pending(now=now)

    assert first == {"endpoint_disabled": 0, "expired": 60}
    with db.connection() as con:
        reasons = {
            row["error"]
            for row in con.execute(
                "SELECT DISTINCT error FROM api_webhook_deliveries WHERE status = 'dropped'"
            ).fetchall()
        }
    assert reasons == {queue.REASON_EXPIRED}
    # The expired rows are past the window, so the next prune takes them; the
    # young pending rows are never touched.
    _drain_maintenance(now)
    assert _counts(endpoint["id"]) == {"pending": 40}


# ---------------------------------------------------------------------------
# 4. Pruning
# ---------------------------------------------------------------------------


def test_prune_deletes_only_settled_rows_past_retention_in_bounded_batches(project, endpoint):
    now = datetime.now(timezone.utc)
    retention = queue.delivery_retention_days()
    old = now - timedelta(days=retention + 2)
    recent = now - timedelta(days=1)
    for status, count in (("delivered", 4000), ("failed", 1500), ("dropped", 1500)):
        _bulk(endpoint, count, created_at=old, status=status, prefix=f"old_{status}", attempt=1)
        _bulk(endpoint, 100, created_at=recent, status=status, prefix=f"new_{status}", attempt=1)
    _bulk(endpoint, 250, created_at=recent, prefix="waiting")

    # One call is bounded by its batch budget and reports what it removed.
    first = queue.prune_settled_deliveries(now=now, batch_size=1000, max_batches=3)
    assert first == 3000

    rest = _drain_maintenance(now)

    assert first + rest["pruned"] == 7000
    assert _counts(endpoint["id"]) == {
        "delivered": 100, "failed": 100, "dropped": 100, "pending": 250
    }


def test_prune_and_settle_skip_locked_rows_instead_of_waiting_for_them(project, endpoint):
    """No long locks: a row another transaction holds (a sweep's claim, a
    console write to the endpoint) is passed over, and picked up next pass."""
    now = datetime.now(timezone.utc)
    retention = queue.delivery_retention_days()
    _bulk(endpoint, 20, created_at=now - timedelta(days=retention + 1), status="delivered",
          prefix="prunable", attempt=1)
    held_row = "whd_prunable_1"
    other = _endpoint(project, "https://hooks.customer.example/locked")
    _bulk(other, 15, created_at=now - timedelta(hours=3), prefix="parked")
    _disable(other, now - timedelta(hours=2))

    holder_ready = threading.Event()
    release = threading.Event()

    def hold_locks():
        with db.connection() as con:
            con.execute(
                "SELECT id FROM api_webhook_deliveries WHERE id = %s FOR UPDATE", (held_row,)
            ).fetchall()
            con.execute(
                "SELECT id FROM api_webhook_endpoints WHERE id = %s FOR UPDATE", (other["id"],)
            ).fetchall()
            holder_ready.set()
            release.wait(timeout=30)

    holder = threading.Thread(target=hold_locks)
    holder.start()
    try:
        assert holder_ready.wait(timeout=10)
        started = time.monotonic()
        pruned = queue.prune_settled_deliveries(now=now)
        settled = queue.settle_stale_pending(now=now)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(timeout=30)

    assert elapsed < 5.0
    assert pruned == 19
    assert settled["endpoint_disabled"] == 0  # its endpoint row was locked
    assert queue.prune_settled_deliveries(now=now) == 1
    assert queue.settle_stale_pending(now=now)["endpoint_disabled"] == 15


# ---------------------------------------------------------------------------
# 5. Growth is bounded
# ---------------------------------------------------------------------------


def test_a_sustained_queue_then_disable_pattern_keeps_the_table_under_a_fixed_ceiling(
    monkeypatch, project
):
    """Simulated time. Every step queues a burst to an ACTIVE endpoint (the
    only kind anything is queued to), then disables it, as the finding
    describes, and re-enables it for the next burst. Before this change every
    one of those rows stayed `pending` forever; now the table holds at most
    the retention window's worth plus one step."""
    monkeypatch.setattr(settings, "public_api_webhook_delivery_retention_days", 2, raising=False)
    monkeypatch.setattr(settings, "public_api_webhook_disabled_grace_seconds", 3600, raising=False)
    target = _endpoint(project)
    burst = 400
    steps_per_day = 6
    days = 6
    step = timedelta(hours=24 / steps_per_day)
    start = datetime.now(timezone.utc) - timedelta(days=days + 1)
    ceiling = (2 * steps_per_day + 2) * burst  # retention days of bursts, plus slack for one pass
    peak = 0
    inserted = 0

    for index in range(days * steps_per_day):
        sim_now = start + index * step
        db.update_webhook_endpoint(target["id"], WORKSPACE, status="active")
        _bulk(target, burst, created_at=sim_now, prefix=f"step{index}")
        inserted += burst
        _disable(target, sim_now)
        # The worker's next cycles, an hour and a bit later in simulated time.
        _drain_maintenance(sim_now + timedelta(hours=1, minutes=5))
        counts = _counts(target["id"])
        total = sum(counts.values())
        peak = max(peak, total)
        assert counts.get("pending", 0) == 0, (index, counts)
        assert total <= ceiling, (index, counts)

    assert inserted == 14_400
    assert peak <= ceiling
    # And once the pattern stops, the rest ages out entirely.
    _drain_maintenance(start + days * steps_per_day * step + timedelta(days=3))
    assert _counts(target["id"]) == {}


# ---------------------------------------------------------------------------
# 6. Ordinary deliveries are unaffected
# ---------------------------------------------------------------------------


def test_a_normal_delivery_still_retries_is_not_settled_meanwhile_and_arrives_signed(
    monkeypatch, project, endpoint
):
    response = db.create_api_response(project["id"], WORKSPACE, "techsara-35b", "req_growth",
                                      background=True)
    response = db.update_api_response(response["id"], project["id"], status="completed")
    # Surrounding noise: another endpoint's settled history past retention.
    noisy = db.create_webhook_endpoint(
        project["id"], WORKSPACE, "https://hooks.customer.example/noisy",
        [sender.RESPONSE_FAILED], SECRET,
    )
    now = datetime.now(timezone.utc)
    _bulk(noisy, 2500, created_at=now - timedelta(days=queue.delivery_retention_days() + 1),
          status="failed", prefix="noise", attempt=6)

    outcomes = iter([500, 502, 200])
    captured = []

    async def post_json(url, body, headers, **kwargs):
        captured.append((body, headers))
        return ssrf.WebhookResponse(status=next(outcomes), url=url)

    monkeypatch.setattr(sender.ssrf, "post_json", post_json)
    [delivery_id] = asyncio.run(
        sender.emit_response_event(response, sender.RESPONSE_COMPLETED, workspace_id=WORKSPACE)
    )

    # After the enqueue: its first attempt is due at the database's `now()`.
    moment = datetime.now(timezone.utc) + timedelta(seconds=2)
    for expected in ("pending", "pending", "delivered"):
        counts = asyncio.run(worker.run_once(now=moment))
        assert counts["due"] == 1
        maintained = asyncio.run(worker.maintain(now=moment, force=True))
        assert maintained["endpoint_disabled"] == 0 and maintained["expired"] == 0
        with db.connection() as con:
            row = con.execute(
                "SELECT status, attempt, next_attempt_at FROM api_webhook_deliveries WHERE id = %s",
                (delivery_id,),
            ).fetchone()
        assert row["status"] == expected
        if expected == "pending":
            assert row["next_attempt_at"] > moment  # backoff scheduled
            moment = row["next_attempt_at"] + timedelta(seconds=1)

    assert row["attempt"] == 3
    assert len(captured) == 3
    for body, headers in captured:
        assert signer.verify(
            body, headers[signer.SIGNATURE_HEADER], SECRET, now=moment
        ) is True
    assert _counts(noisy["id"]) == {}  # the noise was pruned along the way
    # The delivered row itself goes once it is past retention, and not before.
    assert queue.prune_settled_deliveries(now=moment) == 0
    later = moment + timedelta(days=queue.delivery_retention_days() + 1)
    assert queue.prune_settled_deliveries(now=later) == 1


# ---------------------------------------------------------------------------
# 7. The loop
# ---------------------------------------------------------------------------


def test_the_loop_runs_maintenance_between_sweeps_and_survives_it_failing(monkeypatch):
    calls = {"sweeps": 0, "maintain": 0}

    async def fake_run_once(**kwargs):
        calls["sweeps"] += 1
        return {"due": 0, "delivered": 0, "retrying": 0, "failed": 0, "dropped": 0}

    async def fake_maintain(**kwargs):
        calls["maintain"] += 1
        if calls["maintain"] == 1:
            raise RuntimeError("the database went away for a moment")
        return {"endpoint_disabled": 0, "expired": 0, "pruned": 0}

    sleeps = {"n": 0}

    async def fake_sleep(seconds):
        sleeps["n"] += 1
        if sleeps["n"] > 3:  # the initial delay plus three cycles
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "run_once", fake_run_once)
    monkeypatch.setattr(worker, "maintain", fake_maintain)
    monkeypatch.setattr(worker, "_sleep_or_kick", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker._loop())

    assert calls == {"sweeps": 3, "maintain": 3}


def test_maintenance_waits_for_its_interval_unless_a_pass_used_its_whole_budget(monkeypatch):
    seen = {"settle": 0, "prune": 0}
    budget_full = {"prune": False}

    def fake_settle(**kwargs):
        seen["settle"] += 1
        return {"endpoint_disabled": 0, "expired": 0}

    def fake_prune(**kwargs):
        seen["prune"] += 1
        if budget_full["prune"]:
            return worker.PRUNE_MAX_BATCHES * queue.PRUNE_BATCH_SIZE
        return 0

    monkeypatch.setattr(queue, "settle_stale_pending", fake_settle)
    monkeypatch.setattr(queue, "prune_settled_deliveries", fake_prune)
    clock = {"t": 1000.0}

    def tick():
        return clock["t"]

    asyncio.run(worker.maintain(clock=tick))
    asyncio.run(worker.maintain(clock=tick))
    assert seen == {"settle": 1, "prune": 1}  # both waited for their interval

    clock["t"] += worker.SETTLE_INTERVAL_SECONDS
    asyncio.run(worker.maintain(clock=tick))
    assert seen == {"settle": 2, "prune": 1}

    clock["t"] += worker.PRUNE_INTERVAL_SECONDS
    budget_full["prune"] = True
    asyncio.run(worker.maintain(clock=tick))
    asyncio.run(worker.maintain(clock=tick))  # same instant: a full pass is due again
    assert seen["prune"] == 3


# ---------------------------------------------------------------------------
# 8. The second review: the pending cap, the drain rate, the settings
# ---------------------------------------------------------------------------


def _response(project_row, index):
    """A response row as the sender reads it. The delivery table has no
    foreign key to `api_responses`, and the payload is built from the mapping."""
    return {
        "id": f"resp_cap_{index:06d}", "project_id": project_row["id"],
        "status": "cancelled", "model": "techsara-35b", "background": True,
    }


def test_an_active_endpoint_that_never_answers_holds_at_most_the_cap(monkeypatch, project):
    """The second review's second finding: 5,000 rows to an ACTIVE, dead
    endpoint stayed pending through 20 sweep-and-maintain cycles, and nothing
    at enqueue limited how many more could join them. Now the queue stops at
    the cap, the rest are counted on one record, and the backlog still drains
    through the ordinary retry budget."""
    cap = 50
    monkeypatch.setattr(settings, "public_api_webhook_max_pending_per_endpoint", cap, raising=False)
    dead = _endpoint(project, "https://dead.customer.example/hook")
    posts = {"n": 0}

    async def post_json(url, body, headers, **kwargs):
        posts["n"] += 1
        return ssrf.WebhookResponse(status=500, url=url)

    monkeypatch.setattr(sender.ssrf, "post_json", post_json)

    queued = 0
    for index in range(400):
        queued += len(asyncio.run(
            sender.emit_response_event(
                _response(project, index), sender.RESPONSE_CANCELLED, workspace_id=WORKSPACE
            )
        ))
    assert queued == cap
    assert _counts(dead["id"]) == {"pending": cap, "dropped": 1}

    moment = datetime.now(timezone.utc) + timedelta(seconds=2)
    for _cycle in range(20):
        asyncio.run(worker.run_once(now=moment))
        asyncio.run(worker.maintain(now=moment, force=True))
        # Every sweep adds more events; the pending count never passes the cap.
        for index in range(20):
            asyncio.run(
                sender.emit_response_event(
                    _response(project, 10_000 + _cycle * 100 + index),
                    sender.RESPONSE_CANCELLED, workspace_id=WORKSPACE,
                )
            )
        assert _counts(dead["id"]).get("pending", 0) <= cap
        moment += timedelta(minutes=1)

    with db.connection() as con:
        marker = con.execute(
            "SELECT event_id, event_type, payload FROM api_webhook_deliveries "
            " WHERE endpoint_id = %s AND event_id LIKE %s",
            (dead["id"], queue.OVERFLOW_EVENT_PREFIX + "%"),
        ).fetchall()
        total = con.execute(
            "SELECT count(*) AS n FROM api_webhook_deliveries WHERE endpoint_id = %s",
            (dead["id"],),
        ).fetchone()["n"]
    # Honest accounting: every event is either a row or counted on the record,
    # and the record is one row per event type per hour, not one per event.
    not_queued = sum(int(row["payload"]["not_queued"]) for row in marker)
    assert len(marker) <= 2 and {row["event_type"] for row in marker} == {sender.RESPONSE_CANCELLED}
    assert (total - len(marker)) + not_queued == 400 + 20 * 20
    assert posts["n"] == 20 * queue.PER_PROJECT_PER_SWEEP  # the ordinary retry path still ran


def test_at_the_cap_a_duplicate_is_still_a_duplicate_and_a_test_event_is_not_queued(
    monkeypatch, project, endpoint
):
    monkeypatch.setattr(settings, "public_api_webhook_max_pending_per_endpoint", 3, raising=False)
    monkeypatch.setattr(worker, "kick", lambda: None)
    ids = [
        queue.enqueue_delivery(endpoint["id"], project["id"], sender.RESPONSE_COMPLETED, f"evt_{i}", {})
        for i in range(3)
    ]
    assert all(row is not None for row in ids)

    again = queue.enqueue_delivery(endpoint["id"], project["id"], sender.RESPONSE_COMPLETED, "evt_1", {})
    test_event = asyncio.run(sender.enqueue_test_delivery(endpoint))

    assert again is None and test_event is None
    with db.connection() as con:
        rows = con.execute(
            "SELECT event_type, payload FROM api_webhook_deliveries WHERE status = 'dropped'"
        ).fetchall()
    # The duplicate is not counted as "not queued"; the refused test event is.
    assert [(row["event_type"], row["payload"]["not_queued"]) for row in rows] == [
        (sender.EVENT_TEST, 1)
    ]
    with pytest.raises(ValueError):
        queue.enqueue_delivery("whe_missing", project["id"], sender.RESPONSE_COMPLETED, "evt_x", {})

    # Room opens as the backlog is delivered or settled, and queueing resumes.
    with db.connection() as con:
        con.execute("UPDATE api_webhook_deliveries SET status = 'delivered' WHERE id = %s",
                    (ids[0]["id"],))
    assert queue.enqueue_delivery(
        endpoint["id"], project["id"], sender.RESPONSE_COMPLETED, "evt_after", {}
    ) is not None


def test_concurrent_enqueues_overshoot_the_cap_by_less_than_their_own_number(
    monkeypatch, project, endpoint
):
    """No lock by design: racing enqueues can each see room. The overshoot is
    bounded by how many run at once (each holds a pooled connection), and the
    accounting stays exact."""
    cap = 5
    monkeypatch.setattr(settings, "public_api_webhook_max_pending_per_endpoint", cap, raising=False)
    threads_n = 8
    per_thread = 40
    barrier = threading.Barrier(threads_n)
    errors = []

    def flood(worker_index):
        try:
            barrier.wait()
            for index in range(per_thread):
                queue.enqueue_delivery(
                    endpoint["id"], project["id"], sender.RESPONSE_CANCELLED,
                    f"evt_t{worker_index}_{index}", {},
                )
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=flood, args=(i,)) for i in range(threads_n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    with db.connection() as con:
        pending = con.execute(
            "SELECT count(*) AS n FROM api_webhook_deliveries WHERE status = 'pending'"
        ).fetchone()["n"]
        not_queued = con.execute(
            "SELECT COALESCE(sum((payload->>'not_queued')::int), 0) AS n "
            "  FROM api_webhook_deliveries WHERE status = 'dropped'"
        ).fetchone()["n"]
    assert cap <= pending <= cap + threads_n - 1
    assert pending + not_queued == threads_n * per_thread


def test_the_loop_comes_straight_back_after_any_sweep_that_claimed_work(monkeypatch):
    """The drain rate. Only a FULL batch used to count as busy, and a single
    project's share of two never fills one, so its backlog drained at two
    rows per five seconds."""
    dues = iter([2, 2, 0, 1, 0])
    slept = []

    async def fake_run_once(**kwargs):
        return {"due": next(dues), "delivered": 0, "retrying": 0, "failed": 0, "dropped": 0}

    async def fake_maintain(**kwargs):
        return {"endpoint_disabled": 0, "expired": 0, "pruned": 0}

    async def fake_sleep(seconds):
        slept.append(seconds)
        if len(slept) > 5:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "run_once", fake_run_once)
    monkeypatch.setattr(worker, "maintain", fake_maintain)
    monkeypatch.setattr(worker, "_sleep_or_kick", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker._loop())

    busy = worker.BUSY_INTERVAL_SECONDS * 1.15
    idle = worker.POLL_INTERVAL_SECONDS * 0.85
    kinds = ["busy" if value <= busy else "idle" if value >= idle else "?" for value in slept[1:]]
    assert kinds == ["busy", "busy", "idle", "busy", "idle"]


def test_a_failing_settle_neither_blocks_the_prune_nor_retries_every_cycle(monkeypatch):
    """The second review's third finding: one try around both jobs, and the
    settle deadline moved only on success, so a settle that kept timing out
    ran on every five-second cycle and the prune never ran at all."""
    seen = {"settle": 0, "prune": 0}

    def failing_settle(**kwargs):
        seen["settle"] += 1
        raise RuntimeError("canceling statement due to statement timeout")

    def prune(**kwargs):
        seen["prune"] += 1
        return 3

    monkeypatch.setattr(queue, "settle_stale_pending", failing_settle)
    monkeypatch.setattr(queue, "prune_settled_deliveries", prune)
    clock = {"t": 5000.0}
    results = []
    for _cycle in range(13):  # one minute of five-second cycles
        results.append(asyncio.run(worker.maintain(clock=lambda: clock["t"])))
        clock["t"] += 5.0

    assert seen == {"settle": 3, "prune": 1}  # at 0 s, 30 s and 60 s; prune once
    assert results[0] == {"endpoint_disabled": 0, "expired": 0, "pruned": 3}

    # And the other way round: a failing prune does not stop the settle.
    monkeypatch.setattr(worker, "_next_settle_at", 0.0)
    monkeypatch.setattr(worker, "_next_prune_at", 0.0)
    seen.update(settle=0, prune=0)

    def settle(**kwargs):
        seen["settle"] += 1
        return {"endpoint_disabled": 2, "expired": 0}

    def failing_prune(**kwargs):
        seen["prune"] += 1
        raise RuntimeError("the database went away")

    monkeypatch.setattr(queue, "settle_stale_pending", settle)
    monkeypatch.setattr(queue, "prune_settled_deliveries", failing_prune)
    for _cycle in range(13):
        asyncio.run(worker.maintain(clock=lambda: clock["t"]))
        clock["t"] += 5.0
    assert seen == {"settle": 3, "prune": 1}


@pytest.mark.parametrize(
    "env_name, raw, expected",
    [
        ("PUBLIC_API_WEBHOOK_DELIVERY_RETENTION_DAYS", "inf", queue.DEFAULT_DELIVERY_RETENTION_DAYS),
        ("PUBLIC_API_WEBHOOK_DELIVERY_RETENTION_DAYS", "-inf", queue.DEFAULT_DELIVERY_RETENTION_DAYS),
        ("PUBLIC_API_WEBHOOK_DELIVERY_RETENTION_DAYS", "nan", queue.DEFAULT_DELIVERY_RETENTION_DAYS),
        ("PUBLIC_API_WEBHOOK_DELIVERY_RETENTION_DAYS", "1e400", queue.DEFAULT_DELIVERY_RETENTION_DAYS),
        ("PUBLIC_API_WEBHOOK_DELIVERY_RETENTION_DAYS", "1e9", queue.MAX_DELIVERY_RETENTION_DAYS),
        ("PUBLIC_API_WEBHOOK_DISABLED_GRACE_SECONDS", "nan", queue.DEFAULT_DISABLED_GRACE_SECONDS),
        ("PUBLIC_API_WEBHOOK_DISABLED_GRACE_SECONDS", "inf", queue.DEFAULT_DISABLED_GRACE_SECONDS),
        ("PUBLIC_API_WEBHOOK_DISABLED_GRACE_SECONDS", "1e12",
         queue.DEFAULT_DELIVERY_RETENTION_DAYS * 86400.0),
        ("PUBLIC_API_WEBHOOK_DISABLED_GRACE_SECONDS", "-5", 0),
        ("PUBLIC_API_WEBHOOK_MAX_PENDING_PER_ENDPOINT", "nan", queue.DEFAULT_MAX_PENDING_PER_ENDPOINT),
        ("PUBLIC_API_WEBHOOK_MAX_PENDING_PER_ENDPOINT", "0", 1),
        ("PUBLIC_API_WEBHOOK_MAX_PENDING_PER_ENDPOINT", "1e12", queue.MAX_PENDING_PER_ENDPOINT_CEILING),
    ],
)
def test_out_of_range_settings_fall_back_or_clamp_and_every_pass_still_runs(
    monkeypatch, env_name, raw, expected
):
    """The second review's fourth finding: `inf` and huge values raised
    OverflowError on every pass, and `nan` became a zero grace window."""
    for attribute in (
        "public_api_webhook_delivery_retention_days",
        "public_api_webhook_disabled_grace_seconds",
        "public_api_webhook_max_pending_per_endpoint",
    ):
        monkeypatch.delattr(settings, attribute, raising=False)
    for name in (
        "PUBLIC_API_WEBHOOK_DELIVERY_RETENTION_DAYS",
        "PUBLIC_API_WEBHOOK_DISABLED_GRACE_SECONDS",
        "PUBLIC_API_WEBHOOK_MAX_PENDING_PER_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(env_name, raw)

    accessor = {
        "PUBLIC_API_WEBHOOK_DELIVERY_RETENTION_DAYS": queue.delivery_retention_days,
        "PUBLIC_API_WEBHOOK_DISABLED_GRACE_SECONDS": queue.disabled_grace_seconds,
        "PUBLIC_API_WEBHOOK_MAX_PENDING_PER_ENDPOINT": queue.max_pending_per_endpoint,
    }[env_name]
    assert accessor() == expected
    # Neither job raises with the value in force.
    now = datetime.now(timezone.utc)
    assert queue.settle_stale_pending(now=now) == {"endpoint_disabled": 0, "expired": 0}
    assert queue.prune_settled_deliveries(now=now) == 0


def test_a_non_finite_declared_setting_is_treated_like_a_malformed_one(monkeypatch):
    monkeypatch.setattr(
        settings, "public_api_webhook_disabled_grace_seconds", float("nan"), raising=False
    )
    monkeypatch.setattr(
        settings, "public_api_webhook_delivery_retention_days", float("inf"), raising=False
    )
    assert queue.disabled_grace_seconds() == queue.DEFAULT_DISABLED_GRACE_SECONDS
    assert queue.delivery_retention_days() == queue.DEFAULT_DELIVERY_RETENTION_DAYS
    # Explicit arguments are held to the same rules.
    now = datetime.now(timezone.utc)
    assert queue.settle_stale_pending(
        now=now, grace_seconds=float("nan"), retention_days=10**9
    ) == {"endpoint_disabled": 0, "expired": 0}
