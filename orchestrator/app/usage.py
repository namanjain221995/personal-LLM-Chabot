"""The usage event — one durable row per completed model turn (V18).

WHY THIS EXISTS. Before it, three sources each held part of the answer and
none could be joined to the others: `messages.meta` knew the route and the
prompt size, vLLM's Prometheus metrics knew latency and token totals for the
SERVER, and `users.last_active_at` knew someone had been here. Nothing could
say what one person's turn cost or how fast it was, so the admin console
could only count messages.

WHY NOT PROMETHEUS. Per-user analytics needs a user id, and a user id as a
Prometheus label is unbounded cardinality — the one failure mode
app/metrics.py refuses by design. Prometheus keeps the per-engine half (it is
excellent at it); PostgreSQL keeps the per-person half. Nothing is duplicated.

WHAT IS NOT HERE. No prompt, no completion, no retrieved passage, no search
query. The console answers who, how much, when, how fast — reading content is
a different capability with its own audit trail.

Every function must survive a broken database without breaking a chat: a
missing telemetry row is a gap in a report, an exception here would be a
failed answer.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from . import db

log = logging.getLogger(__name__)

#: Status vocabulary, matching the CHECK constraint in migration V18.
OK = "ok"
ERROR = "error"
CANCELLED = "cancelled"


def _int(value: Any) -> Optional[int]:
    """A non-negative int, or None for "not measured".

    Zero and None are different answers and are kept different: 0 output
    tokens means the model produced nothing, None means nobody counted.
    """
    if value is None:
        return None
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def record(
    *,
    user_id: Optional[int],
    workspace_id: Optional[str],
    conversation_id: Optional[str],
    generation_id: Optional[str],
    route: str,
    effort: str = "",
    model: str = "",
    mode: str = "",
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    reasoning_tokens: Optional[int] = None,
    ttft_ms: Optional[int] = None,
    duration_ms: Optional[int] = None,
    status: str = OK,
    error_kind: str = "",
    meta: Optional[Dict[str, Any]] = None,
    created_at: Optional[datetime] = None,
) -> None:
    """Persist one turn. Never raises.

    Idempotent per generation: two browser tabs attached to the same detached
    answer both reach the emitter, and the unique index on `generation_id`
    turns the second write into a no-op rather than a double-counted request.
    """
    try:
        with db.connection() as con:
            con.execute(
                """INSERT INTO usage_events
                       (created_at, user_id, workspace_id, conversation_id,
                        generation_id, route, effort, model, mode,
                        input_tokens, output_tokens, reasoning_tokens,
                        ttft_ms, duration_ms, status, error_kind, meta)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (
                    created_at or datetime.now(timezone.utc),
                    user_id,
                    workspace_id or None,
                    conversation_id or None,
                    generation_id or None,
                    (route or "chat")[:64],
                    (effort or "")[:32],
                    (model or "")[:128],
                    (mode or "")[:32],
                    _int(input_tokens),
                    _int(output_tokens),
                    _int(reasoning_tokens),
                    _int(ttft_ms),
                    _int(duration_ms),
                    status if status in (OK, ERROR, CANCELLED) else OK,
                    (error_kind or "")[:64],
                    db._json_param(meta or {}),
                ),
            )
    except Exception:  # noqa: BLE001 — telemetry must never break a request
        log.debug("usage event not recorded", exc_info=True)


async def record_async(**kwargs: Any) -> None:
    """`record` off the event loop. Awaited at the end of a turn, where a
    millisecond of database time costs nothing that is still streaming."""
    try:
        await db.run_in_thread(lambda: record(**kwargs))
    except Exception:  # noqa: BLE001
        log.debug("usage event not recorded", exc_info=True)
