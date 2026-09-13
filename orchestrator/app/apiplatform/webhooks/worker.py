"""The delivery sweep: pick up what is due, send it, sleep, repeat.

WHY IN THIS PROCESS. Exactly the argument `app/web_worker.py` made in
2026-09-03 and the one CONTRACT-3 §12 repeats for the rate-limit counters:
the queue is a PostgreSQL column, so a restart resumes rather than forgets;
the work is a handful of HTTPS requests a minute; and the code it needs — the
pinned transport, the signer, the accessors — is already loaded here. A second
container to run one asyncio loop would be an image, a healthcheck and a
deployment surface bought for nothing, and a Redis-backed queue is explicitly
out of scope (CONTRACT-3 §18).

WHY IT DOES NOT TOUCH `main.py`. `orchestrator/app/main.py` is a single-owner
file for this programme (OWNERSHIP.md): exactly one wave edits it and that
change is reviewed alone. So this module exposes `start()` and `stop()` in the
same shape as `web_worker`, `continuity` and `engine_state`, and the
integration lead adds the two calls to the lifespan. Until that lands the loop
simply never runs — deliveries queue up durably and are swept the moment it
does, which is the same property that makes a restart safe.

THREE THINGS THE LOOP GUARANTEES

1. IT OUTLIVES ANY ONE DELIVERY. Every exception inside a cycle is caught and
   logged; a consumer that hangs up mid-TLS must not take the sweep down with
   it, or one broken endpoint would stop every other project's webhooks.
2. IT NEVER RUNS UNBOUNDED, AND NO PROJECT CROWDS OUT ANOTHER. At most
   `MAX_CONCURRENT_DELIVERIES` are in flight, each bounded to ten seconds by
   `ssrf.post_json`. Rows are CLAIMED fairly by `queue.claim_due_deliveries`:
   at most `queue.PER_PROJECT_PER_SWEEP` per project, every project's oldest
   before anyone's second. Until 2026-09-13 the sweep read one global FIFO,
   and one project with a dead endpoint could fill every batch and hold back
   every other tenant's webhooks (the adversarial review's cross-tenant
   starvation finding).
3. IT DOES NOT SPIN. An empty sweep sleeps the full interval; one that claimed
   anything comes back after the short busy interval, because a backlog
   draining at one batch per interval would take minutes to clear. Until
   2026-09-13 only a FULL batch counted as busy, and with the per-project
   share of two a single project's backlog never filled one, so it drained at
   two rows per five seconds — slow enough that ordinary backlogs aged past an
   hour, and slow enough that the per-endpoint pending cap (`queue.py`) would
   have been reachable by a consumer that was keeping up. Every claimed row
   leaves the due set (its attempt is recorded, or its lease holds), so a
   non-empty sweep is always progress, never a spin. The interval carries a
   little jitter, and — the part
   that actually makes a blue/green overlap safe — each row is claimed with
   `FOR UPDATE SKIP LOCKED` and a lease, so two sweeps never send the same
   delivery or double-count its attempt.
4. THE TABLE STAYS BOUNDED (2026-09-13, security review). Between sweeps the
   loop settles `pending` rows that can no longer be sent (an endpoint disabled
   past its grace window, or a row older than the retention window) and prunes
   settled rows older than `PUBLIC_API_WEBHOOK_DELIVERY_RETENTION_DAYS`. Before
   this, deliveries left behind by disabling an endpoint stayed `pending`, and
   no prune would touch them, until the endpoint was deleted. Both jobs are
   batched with a per-pass budget and run off the event loop, so a large
   backlog costs a few short transactions per pass, never one long lock, and
   never holds up the next delivery sweep for long. The two jobs fail
   independently and a failed job waits its normal interval before trying
   again, so a settle that keeps timing out neither stops the prune nor runs
   a statement-timeout-length query on every cycle.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from ... import db
from ...config import settings
from . import queue, sender

log = logging.getLogger(__name__)

#: How long an idle loop waits before looking again. A webhook is not a
#: real-time channel; five seconds of latency on a background job that took
#: thirty is not worth a busier poll.
POLL_INTERVAL_SECONDS = 5.0

#: The sweep comes straight back when the last batch was full, but never
#: faster than this — a permanently full queue must not become a tight loop.
BUSY_INTERVAL_SECONDS = 0.5

#: Rows per sweep. `queue.claim_due_deliveries` clamps to 1..200 anyway.
BATCH_SIZE = 20

#: In-flight deliveries. Four is the same order as the default project
#: concurrency (CONTRACT-3 §12) and keeps a slow consumer from occupying the
#: whole sweep.
MAX_CONCURRENT_DELIVERIES = 4

#: How often the loop settles pending rows that can no longer be sent. The
#: statement is cheap when nothing qualifies, and a disabled endpoint's rows
#: should not outlive their grace window by much more than this.
SETTLE_INTERVAL_SECONDS = 30.0

#: How often the loop prunes settled rows. Retention is measured in days, so
#: ten minutes is plenty; a pass that used its whole batch budget brings the
#: next one forward to the next cycle, so a large backlog still drains steadily.
PRUNE_INTERVAL_SECONDS = 600.0

#: Per-pass batch budgets. A pass that uses its whole budget is due again on
#: the next cycle instead of after the interval.
SETTLE_MAX_BATCHES = 20
PRUNE_MAX_BATCHES = 10

_task: Optional[asyncio.Task] = None
_wake: Optional[asyncio.Event] = None

#: Monotonic deadlines for the next settle / prune pass (0 = due now).
_next_settle_at = 0.0
_next_prune_at = 0.0


def enabled() -> bool:
    """Whether the loop may run at all.

    Read through `getattr` so this module works against the `Settings` that
    exists today: `app/config.py` belongs to another wave, and a webhook
    worker that could not be imported until a settings field landed would
    block the tests that prove it works. Absent the flag, on.
    """
    return bool(getattr(settings, "webhook_worker_enabled", True))


def kick() -> None:
    """Wake the loop now — a delivery was just queued. Safe from anywhere."""
    if _wake is not None:
        try:
            _wake.set()
        except RuntimeError:  # pragma: no cover — the loop is closing
            pass


async def _sleep_or_kick(seconds: float) -> None:
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    _wake.clear()
    try:
        await asyncio.wait_for(_wake.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def run_once(
    *,
    limit: int = BATCH_SIZE,
    now: Optional[datetime] = None,
    rng: Optional[random.Random] = None,
) -> Dict[str, int]:
    """One sweep. Returns counts, which is what the tests and the log read.

    Public and awaitable on its own so the whole delivery path can be driven
    from a test without ever starting the loop — the same shape
    `web_worker.run_once` has, for the same reason.
    """
    due: List[dict] = await db.run_in_thread(
        queue.claim_due_deliveries, limit, now=now
    )
    counts = {"due": len(due), "delivered": 0, "retrying": 0, "failed": 0, "dropped": 0}
    if not due:
        return counts
    gate = asyncio.Semaphore(MAX_CONCURRENT_DELIVERIES)

    async def one(row: Dict[str, Any]) -> Optional[sender.Attempt]:
        async with gate:
            try:
                return await sender.deliver(row, now=now, rng=rng)
            except Exception:  # noqa: BLE001 — one delivery never kills the sweep
                log.warning(
                    "webhook delivery %s raised out of sender.deliver",
                    row.get("id"), exc_info=True,
                )
                return None

    for attempt in await asyncio.gather(*(one(row) for row in due)):
        if attempt is None:
            continue
        if attempt.status == "delivered":
            counts["delivered"] += 1
        elif attempt.status == "pending":
            counts["retrying"] += 1
        elif attempt.status == "failed":
            counts["failed"] += 1
        elif attempt.status == "dropped":
            counts["dropped"] += 1
    return counts


async def maintain(
    *,
    now: Optional[datetime] = None,
    force: bool = False,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, int]:
    """Settle what can no longer be sent, then prune what is past retention.

    Returns counts: `endpoint_disabled` and `expired` rows settled, `pruned`
    rows deleted. Each job runs only when its interval has elapsed, unless
    `force`. A job that raises is logged, counts zero, and is next tried
    after its normal interval; it never stops the other job. Every batch is its own transaction on a worker thread, so the
    event loop and the other in-flight deliveries keep moving. A job that used
    its whole per-pass budget is due again on the next cycle rather than after
    the full interval, so a large backlog drains without one long pass.

    Public and awaitable on its own for the same reason as `run_once`: the
    tests drive it without starting the loop.
    """
    global _next_settle_at, _next_prune_at
    counts = {"endpoint_disabled": 0, "expired": 0, "pruned": 0}
    tick = clock()
    # Each job in its own try (2026-09-13 review): they used to share one, so
    # a settle that raised skipped the prune AND left its own deadline where
    # it was, retrying on every five-second cycle. A failure now waits the
    # job's normal interval — the backoff — and the other job still runs.
    if force or tick >= _next_settle_at:
        _next_settle_at = tick + SETTLE_INTERVAL_SECONDS
        try:
            settled = await db.run_in_thread(
                queue.settle_stale_pending, now=now, max_batches=SETTLE_MAX_BATCHES
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — retention failing must not stop delivery
            log.warning("webhook maintenance: settling failed", exc_info=True)
        else:
            counts["endpoint_disabled"] = int(settled.get("endpoint_disabled", 0))
            counts["expired"] = int(settled.get("expired", 0))
            budget = SETTLE_MAX_BATCHES * queue.SETTLE_BATCH_SIZE
            if max(counts["endpoint_disabled"], counts["expired"]) >= budget:
                _next_settle_at = tick
    if force or tick >= _next_prune_at:
        _next_prune_at = tick + PRUNE_INTERVAL_SECONDS
        try:
            pruned = await db.run_in_thread(
                queue.prune_settled_deliveries, now=now, max_batches=PRUNE_MAX_BATCHES
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — see above
            log.warning("webhook maintenance: pruning failed", exc_info=True)
        else:
            counts["pruned"] = int(pruned)
            if counts["pruned"] >= PRUNE_MAX_BATCHES * queue.PRUNE_BATCH_SIZE:
                _next_prune_at = tick
    if any(counts.values()):
        log.info(
            "webhook maintenance: settled endpoint_disabled=%(endpoint_disabled)d "
            "expired=%(expired)d, pruned=%(pruned)d",
            counts,
        )
    return counts


async def _loop() -> None:
    global _wake
    _wake = asyncio.Event()
    # A short delay before the first sweep: start-up already has a migration,
    # a model handshake and the first requests competing for this box, and a
    # webhook that arrives five seconds later has cost nobody anything.
    await _sleep_or_kick(10)
    while True:
        busy = False
        try:
            counts = await run_once()
            busy = counts["due"] > 0
            if counts["due"]:
                log.info(
                    "webhook sweep: due=%(due)d delivered=%(delivered)d "
                    "retrying=%(retrying)d failed=%(failed)d dropped=%(dropped)d",
                    counts,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop outlives any one cycle
            log.warning("webhook sweep failed", exc_info=True)
        try:
            await maintain()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — retention failing must not stop delivery
            log.warning("webhook maintenance failed", exc_info=True)
        interval = BUSY_INTERVAL_SECONDS if busy else POLL_INTERVAL_SECONDS
        # A little jitter so a blue/green overlap does not sweep in lockstep.
        await _sleep_or_kick(interval * random.uniform(0.85, 1.15))


def start() -> None:
    """Start the sweep. Idempotent, and safe to call before any delivery exists.

    THE INTEGRATION LEAD CALLS THIS FROM THE LIFESPAN — this module must not
    edit `main.py` (OWNERSHIP.md), so the two lines live there:

        from .apiplatform.webhooks import worker as webhook_worker
        webhook_worker.start()                      # beside web_worker.start()
        ...
        await webhook_worker.stop()                 # in the finally block
    """
    global _task
    if not enabled():
        log.info("webhook worker disabled by configuration")
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="webhook-delivery-worker")
    log.info(
        "webhook delivery worker started (every %.0fs, %d per sweep, %d in flight)",
        POLL_INTERVAL_SECONDS, BATCH_SIZE, MAX_CONCURRENT_DELIVERIES,
    )


async def stop() -> None:
    """Stop the sweep and wait for it to unwind. Idempotent.

    A delivery interrupted here is not lost: its row is still `pending` with
    its counter unmoved, so the next process's first sweep picks it up. That
    is the whole reason the queue is a table and not a list in memory.
    """
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _task = None


def running() -> bool:
    """Whether the loop is live — for a health payload or a test."""
    return _task is not None and not _task.done()


__all__ = [
    "BATCH_SIZE",
    "BUSY_INTERVAL_SECONDS",
    "MAX_CONCURRENT_DELIVERIES",
    "POLL_INTERVAL_SECONDS",
    "PRUNE_INTERVAL_SECONDS",
    "SETTLE_INTERVAL_SECONDS",
    "enabled",
    "kick",
    "maintain",
    "run_once",
    "running",
    "start",
    "stop",
]
