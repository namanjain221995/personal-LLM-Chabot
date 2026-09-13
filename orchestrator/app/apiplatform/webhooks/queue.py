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
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from ... import db

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
    `active` (a disabled endpoint's queue pauses rather than drops), attempts
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
    "MAX_HISTORY_LIMIT",
    "MAX_HISTORY_OFFSET",
    "PER_PROJECT_PER_SWEEP",
    "claim_due_deliveries",
    "list_deliveries",
]
