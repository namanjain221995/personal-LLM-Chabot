"""The delivery queue's two reads that are not plain accessors: the FAIR,
CLAIMING sweep and the console's delivery history.

WHY THIS FILE EXISTS (2026-09-13). The adversarial review of the webhook
worker confirmed two faults in `db.due_webhook_deliveries`, the accessor the
sweep used:

1. ONE GLOBAL FIFO. `ORDER BY next_attempt_at, created_at, id LIMIT n` with no
   partition means a project with twenty due deliveries to a dead endpoint
   fills every batch. Each of those rows holds a 10 s delivery budget, the next
   sweep picks the next twenty, and every other tenant's deliveries wait behind
   one customer's broken receiver — cross-tenant starvation.
2. NO CLAIM. The select took no lock and left each row `pending` for the whole
   send, so two sweeps (a blue/green overlap during a deploy, which
   `worker.py` names) could both pick, both send, and both increment `attempt`.

`claim_due_deliveries` answers both in one transaction: rank the due rows per
project, take at most `per_project` of each project's oldest, round-robin
across projects (every project's first row before anybody's second), and
CLAIM what was picked with `FOR UPDATE SKIP LOCKED` plus a lease written to
`next_attempt_at`. A claimed row is not due again until its lease lapses, so a
second sweep skips it; a process that dies mid-send loses nothing, because the
lease lapses and the row comes back with its counter unmoved.

It is SQL outside `app/db.py` on purpose and in the open: `db.py` is a
single-owner file in this programme and the sweep is this package's own queue
discipline. Neither query is a caller's read of a row it named — the sweep is
the server acting on its own queue — except `list_deliveries`, which carries
BOTH tenancy predicates (project and workspace) in its WHERE clause.

WHY IT ALSO SETTLES AND PRUNES (2026-09-13, security review). A `pending` row
was never pruned, and the sweep only claims rows whose endpoint is `active`.
So deliveries queued while an endpoint was active and then left behind by
disabling it stayed `pending` until the endpoint or its project was deleted,
and with usage limits off nothing bounded how many a key holder could queue.
Two server-side jobs close that, both batched, both `SKIP LOCKED`, each batch
its own short transaction so neither holds a lock the sweep or a console write
would wait on:

* `settle_stale_pending` ends a delivery that can no longer be sent — its
  endpoint has been disabled for longer than a grace window, or the row has
  been undelivered for longer than the retention window — as `dropped`, with
  the reason in `error`, so the history says why it was never sent. The grace
  window is measured from the endpoint's `disabled_at` ONLY, which is what
  keeps the documented pause: an endpoint disabled and re-enabled within it
  resumes its whole queue, however old the rows are.
* `prune_settled_deliveries` deletes settled rows older than the retention
  window (`PUBLIC_API_WEBHOOK_DELIVERY_RETENTION_DAYS`, 30 by default).

Together they give every delivery row a bounded lifetime whatever the
endpoint's status. Deleting an endpoint already removes its deliveries (the
foreign key cascades), so removal needs neither job.

WHY THE QUEUE IS ALSO CAPPED PER ENDPOINT (2026-09-13, second review). A
bounded lifetime is not a bounded count: an ACTIVE endpoint that never answers
drains at the sweep's per-project share, so rows queued faster than that stay
`pending` for the whole retention window, and a re-enable clears `disabled_at`
so the grace window can be restarted. `enqueue_delivery` therefore refuses to
queue past `PUBLIC_API_WEBHOOK_MAX_PENDING_PER_ENDPOINT` pending rows. An
event it does not queue is not lost silently: it is counted on one `dropped`
row per endpoint, event type and hour, so the history shows how many were not
queued and why, while the rows that record it stay bounded too.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from ... import db
from ...config import settings

log = logging.getLogger(__name__)

#: At most this many of ONE project's deliveries per sweep. Two, not one, so a
#: project with a healthy backlog still drains at a useful rate when it is the
#: only one with work; small enough that a project with a dead endpoint takes
#: at most two of the sweep's four in-flight slots.
PER_PROJECT_PER_SWEEP = 2

#: How long a claimed row stays invisible to other sweeps. Longer than the
#: whole-delivery budget (`ssrf.TIMEOUT_SECONDS`, 10 s) with room for the
#: history write, so a live sender always records before the lease lapses.
CLAIM_LEASE_SECONDS = 60

#: Delivery-history page bounds (OWASP API4: a client-chosen page size is a
#: denial-of-service primitive).
MAX_HISTORY_LIMIT = 100
MAX_HISTORY_OFFSET = 10_000

#: How long settled deliveries are kept, and how long a delivery may stay
#: undelivered before it is settled. Matches `db.prune_api_platform`'s default.
DEFAULT_DELIVERY_RETENTION_DAYS = 30

#: Ten years. Past this a `timedelta` stops being meaningful long before it
#: overflows, and an operator typo must not turn into an exception every pass.
MAX_DELIVERY_RETENTION_DAYS = 3650

#: How long a disabled endpoint's pending deliveries are held before they are
#: settled. An hour is generous next to the roughly five minutes the whole
#: retry budget spans (`sender.MAX_ATTEMPTS`), and short enough that disabling
#: an endpoint is not a way to park rows in the table indefinitely.
DEFAULT_DISABLED_GRACE_SECONDS = 3600

#: How many `pending` deliveries one endpoint may hold before new events to it
#: are not queued. Large next to what a healthy consumer ever has waiting (the
#: sweep drains a backlog continuously), small enough that a dead or parked
#: endpoint cannot hold an unbounded share of the table.
DEFAULT_MAX_PENDING_PER_ENDPOINT = 1000
MAX_PENDING_PER_ENDPOINT_CEILING = 1_000_000

#: Rows per settle / prune statement. Each batch is one transaction, so a
#: batch is the longest any row lock is held.
SETTLE_BATCH_SIZE = 500
PRUNE_BATCH_SIZE = 1000

#: The `error` a settled delivery carries. Plain words, because the console
#: shows them in the delivery history.
REASON_ENDPOINT_DISABLED = "not sent: the endpoint was disabled"
REASON_EXPIRED = "not sent: still undelivered when the retention window ended"

#: The `event_id` prefix of an overflow record. Real event ids are `evt_` plus
#: a digest (`sender.event_id_for`), so the two can never collide on the
#: `(endpoint_id, event_id)` unique index.
OVERFLOW_EVENT_PREFIX = "overflow_"


def _utc(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def claim_due_deliveries(
    limit: int = 20,
    *,
    per_project: int = PER_PROJECT_PER_SWEEP,
    now: Optional[datetime] = None,
    lease_seconds: float = CLAIM_LEASE_SECONDS,
) -> List[dict]:
    """Claim up to `limit` due deliveries, fairly across projects.

    Returns rows in the shape `sender.deliver` reads — the delivery columns
    plus the endpoint it goes to under `endpoint` — in fairness order: every
    picked project's oldest row first, then every project's second.

    Same due rule as `db.due_webhook_deliveries`: `pending`, the endpoint
    `active` (a disabled endpoint's queue pauses rather than drops — for the
    grace window `settle_stale_pending` allows, then it is settled), attempts
    left, and `next_attempt_at` reached.
    """
    bounded = max(1, min(int(limit), 200))
    share = max(1, int(per_project))
    moment = _utc(now)
    lease_until = moment + timedelta(seconds=float(lease_seconds))
    with db.connection() as con:
        picked = [
            str(row["id"])
            for row in con.execute(
                "WITH ranked AS ("
                "  SELECT d.id, d.next_attempt_at, d.created_at, "
                "         row_number() OVER ("
                "             PARTITION BY d.project_id "
                "             ORDER BY d.next_attempt_at NULLS FIRST, d.created_at, d.id"
                "         ) AS rn "
                "    FROM api_webhook_deliveries d "
                "    JOIN api_webhook_endpoints e ON e.id = d.endpoint_id "
                "   WHERE d.status = 'pending' AND e.status = 'active' "
                "     AND d.attempt < d.max_attempts "
                "     AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= %s)"
                ") "
                "SELECT id FROM ranked WHERE rn <= %s "
                " ORDER BY rn, next_attempt_at NULLS FIRST, created_at, id "
                " LIMIT %s",
                (moment, share, bounded),
            ).fetchall()
        ]
        if not picked:
            return []
        # The claim. `SKIP LOCKED` lets a concurrent sweep pass over rows this
        # one is claiming instead of queueing behind them, and the due
        # predicate is RE-CHECKED on the locked row version: a row another
        # sweep claimed a moment ago now has a lease in `next_attempt_at` and
        # falls out here, under READ COMMITTED's re-evaluation of the WHERE.
        claimed = {
            str(row["id"])
            for row in con.execute(
                "UPDATE api_webhook_deliveries SET next_attempt_at = %s "
                " WHERE id IN ("
                "     SELECT id FROM api_webhook_deliveries "
                "      WHERE id = ANY(%s) AND status = 'pending' "
                "        AND attempt < max_attempts "
                "        AND (next_attempt_at IS NULL OR next_attempt_at <= %s) "
                "      FOR UPDATE SKIP LOCKED"
                " ) "
                "RETURNING id",
                (lease_until, picked, moment),
            ).fetchall()
        }
        if not claimed:
            return []
        rows = con.execute(
            "SELECT d.*, e.url AS endpoint_url, e.secret AS endpoint_secret, "
            "       e.previous_secret AS endpoint_previous_secret, "
            "       e.previous_secret_expires_at AS endpoint_previous_secret_expires_at, "
            "       e.include_output AS endpoint_include_output, "
            "       e.events AS endpoint_events "
            "  FROM api_webhook_deliveries d "
            "  JOIN api_webhook_endpoints e ON e.id = d.endpoint_id "
            " WHERE d.id = ANY(%s)",
            (sorted(claimed),),
        ).fetchall()
    by_id: Dict[str, dict] = {}
    for row in rows:
        delivery = db._api_row(row) or {}
        delivery["endpoint"] = {
            "id": delivery["endpoint_id"],
            "url": delivery.pop("endpoint_url"),
            "secret": delivery.pop("endpoint_secret"),
            "previous_secret": delivery.pop("endpoint_previous_secret"),
            "previous_secret_expires_at": delivery.pop("endpoint_previous_secret_expires_at"),
            "include_output": delivery.pop("endpoint_include_output"),
            "events": delivery.pop("endpoint_events"),
        }
        by_id[str(delivery["id"])] = delivery
    return [by_id[delivery_id] for delivery_id in picked if delivery_id in by_id]


#: What an overflow record says. `format()` fills the count of events not
#: queued this hour and the cap in force.
REASON_OVER_CAP = (
    "not queued: %s event(s) arrived while this endpoint already had %s "
    "deliveries waiting to be sent"
)


def enqueue_delivery(
    endpoint_id: str,
    project_id: str,
    event_type: str,
    event_id: str,
    payload: Optional[dict] = None,
    *,
    response_id: Optional[str] = None,
    max_attempts: int = 6,
    max_pending: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """Queue one delivery unless its endpoint already holds `max_pending`
    pending rows. Returns the queued row; None when this `event_id` is already
    queued to this endpoint (as `db.enqueue_webhook_delivery`) or when the
    endpoint is at its cap. ValueError when the endpoint is not this project's.

    WHY (2026-09-13, second security review). Retention bounds how long a
    delivery row lives, not how many there are: an active endpoint that never
    answers, or one re-enabled just before its grace window ended, keeps its
    rows `pending` for up to the retention window while new events keep
    arriving. The cap is a bound on BACKLOG, not on volume — a consumer that
    keeps up never meets it, which keeps it consistent with running the
    platform without usage limits.

    ONE STATEMENT, NO LOCK. The count and the insert are one INSERT ... SELECT,
    so there is no window between them inside a transaction; two enqueues that
    race can both see room, but each holds a pooled connection while it does,
    so the overshoot is at most the pool size. Nothing waits: a lock here
    would park a pool connection per queued event, which is a worse outage
    than a cap exceeded by a few rows.

    OVER THE CAP IS RECORDED, BOUNDEDLY. The event is counted on one `dropped`
    row per endpoint, event type and UTC hour (`OVERFLOW_EVENT_PREFIX`), with
    the count and the cap in `error`, so the console's history says events
    were not queued instead of going quiet — and a flood adds at most one row
    an hour per type, which the prune ages out like any settled row.

    COST. The count stops at `max_pending` rows. Today it is served by the
    existing pending-only index or by the `(endpoint_id, event_id)` unique
    index, whichever the planner prefers (2026-09-13: under 0.3 ms with 100k
    rows pending across 100 endpoints, and with 300k settled rows behind one
    endpoint; the capped enqueue measured about 0.6 ms slower than the
    uncapped one). A partial index on `(endpoint_id) WHERE status = 'pending'`
    in `db.py` would make it independent of both.
    """
    cap = (
        max_pending_per_endpoint()
        if max_pending is None
        else int(
            _explicit(
                max_pending, DEFAULT_MAX_PENDING_PER_ENDPOINT, 1, MAX_PENDING_PER_ENDPOINT_CEILING
            )
        )
    )
    body = db._json_object(
        "api_webhook_deliveries.payload", payload if payload is not None else {}
    )
    with db.connection() as con:
        row = con.execute(
            "INSERT INTO api_webhook_deliveries "
            "    (id, event_id, endpoint_id, project_id, event_type, response_id, "
            "     payload, max_attempts, next_attempt_at) "
            "SELECT %s, %s, e.id, e.project_id, %s, %s, %s, %s, now() "
            "  FROM api_webhook_endpoints e "
            " WHERE e.id = %s AND e.project_id = %s "
            "   AND (SELECT count(*) FROM ("
            "          SELECT 1 FROM api_webhook_deliveries held "
            "           WHERE held.endpoint_id = e.id AND held.status = 'pending' "
            "           LIMIT %s) waiting) < %s "
            "ON CONFLICT (endpoint_id, event_id) DO NOTHING "
            "RETURNING *",
            (
                db._api_id("whd"), db._text(event_id), db._text(event_type), response_id,
                body, int(max_attempts), endpoint_id, project_id, cap, cap,
            ),
        ).fetchone()
        if row is not None:
            return db._api_row(row)
        state = con.execute(
            "SELECT EXISTS (SELECT 1 FROM api_webhook_endpoints "
            "                WHERE id = %s AND project_id = %s) AS known, "
            "       EXISTS (SELECT 1 FROM api_webhook_deliveries "
            "                WHERE endpoint_id = %s AND event_id = %s) AS duplicate",
            (endpoint_id, project_id, endpoint_id, db._text(event_id)),
        ).fetchone()
        if not state["known"]:
            raise ValueError(
                f"enqueue_delivery: no webhook endpoint {endpoint_id!r} in project {project_id!r}"
            )
        if state["duplicate"]:
            return None
        bucket = _utc(now).astimezone(timezone.utc).strftime("%Y%m%d%H")
        marker = con.execute(
            "INSERT INTO api_webhook_deliveries AS d "
            "    (id, event_id, endpoint_id, project_id, event_type, response_id, "
            "     payload, status, max_attempts, error, next_attempt_at) "
            "SELECT %s, %s, e.id, e.project_id, %s, %s, "
            "       jsonb_build_object('not_queued', 1, 'max_pending', %s::bigint), "
            "       'dropped', %s, format(%s, 1, %s::bigint), NULL "
            "  FROM api_webhook_endpoints e "
            " WHERE e.id = %s AND e.project_id = %s "
            "ON CONFLICT (endpoint_id, event_id) DO UPDATE SET "
            "    payload = jsonb_build_object("
            "        'not_queued', COALESCE((d.payload->>'not_queued')::bigint, 0) + 1, "
            "        'max_pending', EXCLUDED.payload->'max_pending'), "
            "    response_id = EXCLUDED.response_id, "
            "    error = format(%s, COALESCE((d.payload->>'not_queued')::bigint, 0) + 1, "
            "                   EXCLUDED.payload->>'max_pending') "
            " WHERE d.status = 'dropped' "
            "RETURNING (xmax = 0) AS first",
            (
                db._api_id("whd"),
                f"{OVERFLOW_EVENT_PREFIX}{db._text(event_type)}_{bucket}",
                db._text(event_type), response_id, cap, int(max_attempts),
                REASON_OVER_CAP, cap, endpoint_id, project_id, REASON_OVER_CAP,
            ),
        ).fetchone()
    if marker is not None and marker["first"]:
        # Once per endpoint, type and hour — a flood must not become a log flood.
        log.warning(
            "webhook endpoint %s is at its cap of %d pending deliveries; "
            "further %s events this hour are counted, not queued",
            endpoint_id, cap, event_type,
        )
    return None


def _clamp(value: float, floor: float, ceiling: float) -> float:
    return min(ceiling, max(floor, value))


def _setting_number(
    attribute: str, env_name: str, default: float, *, floor: float, ceiling: float
) -> float:
    """A setting read the way `app/config.py` reads it, clamped to
    `floor`..`ceiling`.

    `settings.<attribute>` first, for the day `config.py` declares it (that
    file belongs to another team); else the environment variable with
    `config.py`'s rule — unset or blank means the default. Unlike `config.py`
    a malformed value falls back to the default with a warning instead of
    raising: this is read inside the worker loop, and a typo must not switch
    retention off by failing every pass.

    Malformed includes NOT FINITE (2026-09-13 review): `float()` accepts
    `inf` and `nan`, `max(0, nan)` is 0, and an infinite or huge day count
    overflows `timedelta` — so `nan` silently removed the grace window and
    `inf` raised on every pass. Anything out of range is clamped, not raised.
    """
    value = getattr(settings, attribute, None)
    source = f"settings.{attribute}"
    if value is None:
        raw = os.environ.get(env_name)
        if raw is None or raw.strip() == "":
            return _clamp(float(default), floor, ceiling)
        value, source = raw, env_name
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        number = float("nan")
    if not math.isfinite(number):
        log.warning("%s is not a finite number; using %s", source, default)
        return _clamp(float(default), floor, ceiling)
    return _clamp(number, floor, ceiling)


def delivery_retention_days() -> int:
    """`PUBLIC_API_WEBHOOK_DELIVERY_RETENTION_DAYS`, at least one day.

    Floored rather than allowed to reach zero: zero would delete a delivery's
    history the moment it settled, which is the silent drop the history exists
    to rule out. Capped at `MAX_DELIVERY_RETENTION_DAYS`."""
    return int(
        _setting_number(
            "public_api_webhook_delivery_retention_days",
            "PUBLIC_API_WEBHOOK_DELIVERY_RETENTION_DAYS",
            DEFAULT_DELIVERY_RETENTION_DAYS,
            floor=1,
            ceiling=MAX_DELIVERY_RETENTION_DAYS,
        )
    )


def disabled_grace_seconds() -> float:
    """`PUBLIC_API_WEBHOOK_DISABLED_GRACE_SECONDS`; zero settles on the next pass.

    At most the retention window: past it every pending row is settled as
    expired anyway, so a longer grace would only be a promise not kept."""
    return _setting_number(
        "public_api_webhook_disabled_grace_seconds",
        "PUBLIC_API_WEBHOOK_DISABLED_GRACE_SECONDS",
        DEFAULT_DISABLED_GRACE_SECONDS,
        floor=0,
        ceiling=delivery_retention_days() * 86400.0,
    )


def max_pending_per_endpoint() -> int:
    """`PUBLIC_API_WEBHOOK_MAX_PENDING_PER_ENDPOINT`, at least one."""
    return int(
        _setting_number(
            "public_api_webhook_max_pending_per_endpoint",
            "PUBLIC_API_WEBHOOK_MAX_PENDING_PER_ENDPOINT",
            DEFAULT_MAX_PENDING_PER_ENDPOINT,
            floor=1,
            ceiling=MAX_PENDING_PER_ENDPOINT_CEILING,
        )
    )


def _explicit(value: object, default: float, floor: float, ceiling: float) -> float:
    """An explicitly passed window, held to the same rules as the settings."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return _clamp(default, floor, ceiling)
    if not math.isfinite(number):
        return _clamp(default, floor, ceiling)
    return _clamp(number, floor, ceiling)


def _windows(
    grace_seconds: Optional[float], retention_days: Optional[int]
) -> "tuple[float, int]":
    retention = (
        delivery_retention_days()
        if retention_days is None
        else int(
            _explicit(
                retention_days, DEFAULT_DELIVERY_RETENTION_DAYS, 1, MAX_DELIVERY_RETENTION_DAYS
            )
        )
    )
    grace = (
        disabled_grace_seconds()
        if grace_seconds is None
        else _explicit(grace_seconds, DEFAULT_DISABLED_GRACE_SECONDS, 0.0, retention * 86400.0)
    )
    return grace, retention


def settle_stale_pending(
    *,
    now: Optional[datetime] = None,
    grace_seconds: Optional[float] = None,
    retention_days: Optional[int] = None,
    batch_size: int = SETTLE_BATCH_SIZE,
    max_batches: int = 20,
) -> Dict[str, int]:
    """Settle `pending` deliveries that can no longer be sent. Returns counts.

    Two cases, each marked `dropped` with its reason and no next attempt:

    * the endpoint is `disabled` and was disabled more than `grace_seconds`
      ago. Measured from `disabled_at` alone, never from the row's age: until
      2026-09-13 a second arm also settled any row older than the grace
      window, so a short disable silently dropped an ordinary backlog that
      was merely waiting its turn, which is the opposite of the documented
      pause. A re-enable clears `disabled_at` (`db.update_webhook_endpoint`)
      and so restarts this window; what bounds a backlog kept that way is the
      per-endpoint pending cap at enqueue plus the retention window below. A
      `disabled` endpoint with no `disabled_at` counts as disabled long ago;
    * the delivery is older than the retention window whatever its endpoint's
      status. With the attempt budget spanning minutes, a delivery that old
      was never going to be sent, and until it is settled no prune may touch it.

    The attempt counter and the endpoint's health columns do not move: no
    attempt was made. A row whose sender is mid-send when it is settled is
    re-recorded by that sender (`db.record_webhook_attempt`); a `pending`
    outcome is settled again on the next pass, a `delivered` one is the truth.

    Batched by `max_batches` × `batch_size`; the next pass continues. The
    endpoint row is share-locked with `SKIP LOCKED` for the batch, so a
    re-enable committed in the same instant either lands first (the row is not
    settled) or waits the few milliseconds the batch takes.
    """
    moment = _utc(now)
    grace, retention = _windows(grace_seconds, retention_days)
    disabled_cutoff = moment - timedelta(seconds=grace)
    expired_cutoff = moment - timedelta(days=retention)
    size = max(1, min(int(batch_size), 5000))
    counts = {"endpoint_disabled": 0, "expired": 0}
    jobs = (
        (
            "endpoint_disabled",
            REASON_ENDPOINT_DISABLED,
            "SELECT d.id FROM api_webhook_deliveries d "
            "  JOIN api_webhook_endpoints e ON e.id = d.endpoint_id "
            " WHERE d.status = 'pending' AND e.status = 'disabled' "
            "   AND COALESCE(e.disabled_at, '-infinity'::timestamptz) <= %s "
            " LIMIT %s "
            " FOR UPDATE OF d SKIP LOCKED FOR SHARE OF e SKIP LOCKED",
            (disabled_cutoff,),
        ),
        (
            "expired",
            REASON_EXPIRED,
            "SELECT d.id FROM api_webhook_deliveries d "
            " WHERE d.status = 'pending' AND d.created_at < %s "
            " LIMIT %s "
            " FOR UPDATE OF d SKIP LOCKED",
            (expired_cutoff,),
        ),
    )
    for name, reason, select, params in jobs:
        for _ in range(max(1, int(max_batches))):
            with db.connection() as con:
                touched = con.execute(
                    "UPDATE api_webhook_deliveries "
                    "   SET status = 'dropped', error = %s, next_attempt_at = NULL "
                    f" WHERE id IN ({select}) AND status = 'pending'",
                    (reason, *params, size),
                ).rowcount
            touched = max(0, int(touched))
            counts[name] += touched
            if touched < size:
                break
    return counts


def prune_settled_deliveries(
    *,
    now: Optional[datetime] = None,
    retention_days: Optional[int] = None,
    batch_size: int = PRUNE_BATCH_SIZE,
    max_batches: int = 10,
) -> int:
    """Delete settled deliveries created before the retention window. Returns
    how many were removed.

    The same rule `db.prune_api_platform` applies (`created_at`, the three
    settled statuses), run from the delivery worker so the table is kept in
    bounds by the process that fills it. A `pending` row is never deleted here:
    `settle_stale_pending` gives it a status and a reason first. A row it
    settled for age is already past the window, so it goes on the next prune;
    the worker logs how many it settled, which is the trace that remains.

    One short transaction per batch, rows taken with `SKIP LOCKED`, so a
    concurrent prune (a blue/green overlap, or `prune_api_platform`) and the
    sweep never wait on it. At most `max_batches` × `batch_size` rows per call;
    the caller comes back sooner when a call used its whole budget.
    """
    moment = _utc(now)
    _grace, retention = _windows(0.0, retention_days)
    cutoff = moment - timedelta(days=retention)
    size = max(1, min(int(batch_size), 5000))
    removed = 0
    for _ in range(max(1, int(max_batches))):
        with db.connection() as con:
            touched = con.execute(
                "DELETE FROM api_webhook_deliveries WHERE id IN ("
                " SELECT id FROM api_webhook_deliveries "
                "  WHERE status IN ('delivered', 'failed', 'dropped') AND created_at < %s "
                "  LIMIT %s FOR UPDATE SKIP LOCKED)",
                (cutoff, size),
            ).rowcount
        touched = max(0, int(touched))
        removed += touched
        if touched < size:
            break
    return removed


def list_deliveries(
    endpoint_id: str,
    project_id: str,
    workspace_id: str,
    /,
    *,
    limit: int = 50,
    offset: int = 0,
) -> List[dict]:
    """One endpoint's delivery history, newest first — METADATA ONLY.

    `payload` is not selected: with `include_output` it carries the generated
    text, which the console does not show (CONTRACT-3 §16). The tenancy
    arguments are positional-only for the reason `db.update_api_project`
    gives: nothing built from a request can supply them by keyword.
    """
    bounded = max(1, min(int(limit), MAX_HISTORY_LIMIT))
    skip = max(0, min(int(offset), MAX_HISTORY_OFFSET))
    with db.connection() as con:
        rows = con.execute(
            "SELECT d.id, d.event_id, d.event_type, d.response_id, d.status, "
            "       d.attempt, d.max_attempts, d.http_status, d.error, "
            "       d.next_attempt_at, d.created_at, d.delivered_at "
            "  FROM api_webhook_deliveries d "
            "  JOIN api_webhook_endpoints e ON e.id = d.endpoint_id "
            " WHERE d.endpoint_id = %s AND d.project_id = %s "
            "   AND e.project_id = %s AND e.workspace_id = %s "
            " ORDER BY d.created_at DESC, d.id DESC "
            " LIMIT %s OFFSET %s",
            (endpoint_id, project_id, project_id, workspace_id, bounded, skip),
        ).fetchall()
    return db._api_rows(rows)


__all__ = [
    "CLAIM_LEASE_SECONDS",
    "DEFAULT_DELIVERY_RETENTION_DAYS",
    "DEFAULT_DISABLED_GRACE_SECONDS",
    "DEFAULT_MAX_PENDING_PER_ENDPOINT",
    "MAX_DELIVERY_RETENTION_DAYS",
    "MAX_HISTORY_LIMIT",
    "MAX_HISTORY_OFFSET",
    "MAX_PENDING_PER_ENDPOINT_CEILING",
    "OVERFLOW_EVENT_PREFIX",
    "PER_PROJECT_PER_SWEEP",
    "PRUNE_BATCH_SIZE",
    "REASON_ENDPOINT_DISABLED",
    "REASON_EXPIRED",
    "REASON_OVER_CAP",
    "SETTLE_BATCH_SIZE",
    "claim_due_deliveries",
    "delivery_retention_days",
    "disabled_grace_seconds",
    "enqueue_delivery",
    "list_deliveries",
    "max_pending_per_endpoint",
    "prune_settled_deliveries",
    "settle_stale_pending",
]
