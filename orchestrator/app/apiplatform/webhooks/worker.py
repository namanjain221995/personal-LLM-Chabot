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
3. IT DOES NOT SPIN. An empty sweep sleeps the full interval; a full one comes
   straight back, because a backlog draining at one batch per interval would
   take minutes to clear. The interval carries a little jitter, and — the part
   that actually makes a blue/green overlap safe — each row is claimed with
   `FOR UPDATE SKIP LOCKED` and a lease, so two sweeps never send the same
   delivery or double-count its attempt.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from typing import Any, Dict, List, Optional

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

_task: Optional[asyncio.Task] = None
_wake: Optional[asyncio.Event] = None


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
            busy = counts["due"] >= BATCH_SIZE
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
    "enabled",
    "kick",
    "run_once",
    "running",
    "start",
    "stop",
]
