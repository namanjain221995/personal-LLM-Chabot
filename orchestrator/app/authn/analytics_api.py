"""The super-admin analytics console API (/admin/api/analytics/*).

AUTHORIZATION. Every route here depends on `Cap.ANALYTICS_READ`, which
rbac.py grants to SUPER_ADMIN alone. `require_capability` answers 404, so to
an admin or a member these endpoints do not exist — typing the URL reveals
neither the surface nor that they were refused. Hidden navigation is a
courtesy for the people who DO have the capability; this is the gate.

SHAPE. Every response carries the resolved window and a `coverage` block
saying when telemetry actually starts, because a chart that begins at zero on
the day instrumentation was deployed is a lie the reader cannot see. Values
that were never measured are `null`, never 0.

PRIVACY. Aggregates and the identity the member list already shows (name,
email, role). No message content, no prompts, no search text, no CRM records.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from .. import analytics, db
from ..analytics import infra
from .principal import Principal, require_capability
from .rbac import Cap

router = APIRouter(prefix="/admin/api/analytics", tags=["analytics"])

#: Selectable windows, in hours. A closed set: the range key reaches SQL only
#: as a number of hours, never as text.
RANGES: Dict[str, int] = {
    "1h": 1,
    "24h": 24,
    "7d": 24 * 7,
    "30d": 24 * 30,
    "90d": 24 * 90,
}
RANGE_PATTERN = "^(1h|24h|7d|30d|90d)$"

#: Order keys the leaderboard accepts, mirrored in analytics.leaderboard.
ORDER_PATTERN = "^(output_tokens|input_tokens|total_tokens|requests|messages|research|web_searches)$"

Gate = Depends(require_capability(Cap.ANALYTICS_READ))


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _num(value: Any) -> Optional[float]:
    """A JSON number, or None. Decimal (which psycopg returns for avg/sum)
    is not JSON-serialisable, and rounding here keeps the wire small."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return round(out, 4)


def _int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class Window:
    """A resolved time window, plus the equally-long window before it.

    The previous window is what every "↑ 15.2%" on the console compares
    against, and it must be the SAME LENGTH — comparing a 7-day total against
    a 30-day one is the classic way a dashboard invents a collapse.
    """

    def __init__(self, key: str) -> None:
        hours = RANGES.get(key, 24 * 30)
        now = datetime.now(timezone.utc)
        if hours <= 24:
            # Sub-daily windows end NOW: rounding an hourly view to midnight
            # would drop the part of the day people are asking about.
            self.until = now
        else:
            self.until = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        self.since = self.until - timedelta(hours=hours)
        self.hours = hours
        self.key = key
        self.bucket = "hour" if hours <= 48 else "day"
        self.prev_until = self.since
        self.prev_since = self.since - timedelta(hours=hours)

    def payload(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "hours": self.hours,
            "bucket": self.bucket,
            "since": _iso(self.since),
            "until": _iso(self.until),
            "previous_since": _iso(self.prev_since),
            "previous_until": _iso(self.prev_until),
        }


def _delta(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    """Percent change, or None when the comparison would be meaningless.

    A zero denominator has no percentage — "up from nothing" is infinity, not
    a number to print — and None on either side means one of the two windows
    was never measured.
    """
    if current is None or previous is None or previous == 0:
        return None
    return round((current - previous) / previous * 100, 1)


def _member(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": _int(row.get("id")),
        "name": row.get("display_name") or row.get("username") or "",
        "email": row.get("email") or "",
        "role": row.get("role") or "member",
        "status": row.get("status") or "active",
        "last_active_at": _iso(row.get("last_active_at")),
        "requests": _int(row.get("requests")) or 0,
        "errors": _int(row.get("errors")) or 0,
        "input_tokens": _int(row.get("input_tokens")),
        "output_tokens": _int(row.get("output_tokens")),
        "total_tokens": (
            None
            if row.get("input_tokens") is None and row.get("output_tokens") is None
            else (_int(row.get("input_tokens")) or 0) + (_int(row.get("output_tokens")) or 0)
        ),
        "avg_ttft_ms": _num(row.get("avg_ttft_ms")),
        "messages": _int(row.get("messages")) or 0,
        "conversations": _int(row.get("conversations")) or 0,
        "research_runs": _int(row.get("research_runs")) or 0,
        "web_searches": _int(row.get("web_searches")) or 0,
    }


def _totals(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "requests": _int(row.get("requests")) or 0,
        "ok": _int(row.get("ok")) or 0,
        "errors": _int(row.get("errors")) or 0,
        "cancelled": _int(row.get("cancelled")) or 0,
        "users": _int(row.get("users")) or 0,
        "input_tokens": _int(row.get("input_tokens")),
        "output_tokens": _int(row.get("output_tokens")),
        "total_tokens": (
            None
            if row.get("input_tokens") is None and row.get("output_tokens") is None
            else (_int(row.get("input_tokens")) or 0) + (_int(row.get("output_tokens")) or 0)
        ),
        "avg_ttft_ms": _num(row.get("avg_ttft_ms")),
        "p50_ttft_ms": _num(row.get("p50_ttft_ms")),
        "p95_ttft_ms": _num(row.get("p95_ttft_ms")),
        "p99_ttft_ms": _num(row.get("p99_ttft_ms")),
        "avg_duration_ms": _num(row.get("avg_duration_ms")),
        "p50_duration_ms": _num(row.get("p50_duration_ms")),
        "p95_duration_ms": _num(row.get("p95_duration_ms")),
        "p99_duration_ms": _num(row.get("p99_duration_ms")),
        "avg_tokens_per_second": _num(row.get("avg_tokens_per_second")),
    }


async def _coverage(workspace_id: str) -> Dict[str, Any]:
    row = await db.run_in_thread(analytics.coverage, workspace_id)
    return {
        "first_event": _iso(row.get("first_event")),
        "last_event": _iso(row.get("last_event")),
        "events": _int(row.get("events")) or 0,
    }


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


@router.get("/overview")
async def overview(
    range: str = Query("30d", pattern=RANGE_PATTERN),
    model: str = Query("", max_length=128),
    principal: Principal = Gate,
) -> dict:
    """The console's front page: totals against the previous window, the four
    time series behind them, and the right-hand rail's short lists.

    `model` narrows the REQUEST-shaped figures (requests, tokens, latency,
    route mix) to one served model. It deliberately does NOT narrow the
    message and active-people series: those are counted from conversation
    history, which has no model on it, and silently filtering them would make
    two numbers on one page mean different things.
    """
    w = Window(range)
    ws = principal.workspace_id
    (
        now_totals,
        prev_totals,
        usage_series,
        active,
        chat,
        prev_chat,
        routes,
        models_rows,
        available,
        top,
        coverage,
    ) = await asyncio.gather(
        db.run_in_thread(analytics.totals, ws, w.since, w.until, model),
        db.run_in_thread(analytics.totals, ws, w.prev_since, w.prev_until, model),
        db.run_in_thread(analytics.series, ws, w.since, w.until, w.bucket, model),
        db.run_in_thread(analytics.daily_active, ws, w.since, w.until, w.bucket),
        db.run_in_thread(analytics.chat_totals, ws, w.since, w.until),
        db.run_in_thread(analytics.chat_totals, ws, w.prev_since, w.prev_until),
        db.run_in_thread(analytics.by_route, ws, w.since, w.until, model),
        db.run_in_thread(analytics.by_model, ws, w.since, w.until),
        db.run_in_thread(analytics.known_models, ws),
        db.run_in_thread(
            analytics.top_users, ws, w.since, w.until, metric="output_tokens", limit=10
        ),
        _coverage(ws),
    )
    models = models_rows
    cur, prev = _totals(now_totals), _totals(prev_totals)
    model_requests = sum(_int(r.get("requests")) or 0 for r in models) or 0
    return {
        "workspace": {"id": ws, "name": principal.workspace_name},
        "range": w.payload(),
        "coverage": coverage,
        # The filter's options come from the DATA, so they can never drift
        # from what has actually served a request here.
        "available_models": available,
        "model": model,
        "totals": cur,
        "previous": prev,
        "deltas": {
            "requests": _delta(cur["requests"], prev["requests"]),
            "total_tokens": _delta(cur["total_tokens"], prev["total_tokens"]),
            "users": _delta(cur["users"], prev["users"]),
            "messages": _delta(
                _int(chat.get("messages")), _int(prev_chat.get("messages"))
            ),
            "avg_ttft_ms": _delta(cur["avg_ttft_ms"], prev["avg_ttft_ms"]),
        },
        "chat": {
            "messages": _int(chat.get("messages")) or 0,
            "answers": _int(chat.get("answers")) or 0,
            "conversations": _int(chat.get("conversations")) or 0,
            "new_conversations": _int(chat.get("new_conversations")) or 0,
            "users": _int(chat.get("users")) or 0,
        },
        "series": {
            "usage": [
                {
                    "bucket": _iso(r["bucket"]),
                    "requests": _int(r.get("requests")) or 0,
                    "errors": _int(r.get("errors")) or 0,
                    "users": _int(r.get("users")) or 0,
                    "input_tokens": _int(r.get("input_tokens")),
                    "output_tokens": _int(r.get("output_tokens")),
                    "avg_ttft_ms": _num(r.get("avg_ttft_ms")),
                }
                for r in usage_series
            ],
            "active_users": [
                {
                    "bucket": _iso(r["bucket"]),
                    "active": _int(r.get("active")) or 0,
                    "messages": _int(r.get("messages")) or 0,
                    "chat": _int(r.get("chat")) or 0,
                    "research": _int(r.get("research")) or 0,
                    "web_search": _int(r.get("web_search")) or 0,
                    "salesforce": _int(r.get("salesforce")) or 0,
                }
                for r in active
            ],
        },
        "routes": [
            {
                "route": r["route"],
                "requests": _int(r.get("requests")) or 0,
                "errors": _int(r.get("errors")) or 0,
                "output_tokens": _int(r.get("output_tokens")),
                "avg_ttft_ms": _num(r.get("avg_ttft_ms")),
            }
            for r in routes
        ],
        "models": [
            {
                "model": r["model"],
                "requests": _int(r.get("requests")) or 0,
                "share": (
                    round((_int(r.get("requests")) or 0) / model_requests * 100, 1)
                    if model_requests
                    else None
                ),
                "output_tokens": _int(r.get("output_tokens")),
                "avg_ttft_ms": _num(r.get("avg_ttft_ms")),
                "avg_tokens_per_second": _num(r.get("avg_tokens_per_second")),
            }
            for r in models
        ],
        "top_users": [_member(r) for r in top],
    }


# ---------------------------------------------------------------------------
# Leaderboards
# ---------------------------------------------------------------------------


@router.get("/leaderboard")
async def leaderboard(
    range: str = Query("30d", pattern=RANGE_PATTERN),
    order: str = Query("output_tokens", pattern=ORDER_PATTERN),
    search: str = Query("", max_length=120),
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    principal: Principal = Gate,
) -> dict:
    """Ranked people, paginated in the DATABASE — the table never receives
    rows it will not draw, whatever the workspace grows to."""
    w = Window(range)
    board = await db.run_in_thread(
        analytics.leaderboard,
        principal.workspace_id,
        w.since,
        w.until,
        limit=limit,
        offset=offset,
        search=search,
        order=order,
    )
    return {
        "range": w.payload(),
        "order": order,
        "limit": limit,
        "offset": offset,
        "total": board["total"],
        "rows": [_member(r) for r in board["rows"]],
    }


@router.get("/models")
async def models(
    range: str = Query("30d", pattern=RANGE_PATTERN),
    principal: Principal = Gate,
) -> dict:
    """Per-model workload, speed and reliability, plus the live engine state
    behind each one where Prometheus can see it."""
    w = Window(range)
    rows, effort, live, coverage = await asyncio.gather(
        db.run_in_thread(analytics.by_model, principal.workspace_id, w.since, w.until),
        db.run_in_thread(analytics.by_effort, principal.workspace_id, w.since, w.until),
        infra.safe(infra.engines()),
        _coverage(principal.workspace_id),
    )
    total = sum(_int(r.get("requests")) or 0 for r in rows) or 0
    return {
        "range": w.payload(),
        "coverage": coverage,
        "models": [
            {
                "model": r["model"],
                "requests": _int(r.get("requests")) or 0,
                "share": (
                    round((_int(r.get("requests")) or 0) / total * 100, 1)
                    if total
                    else None
                ),
                "errors": _int(r.get("errors")) or 0,
                "cancelled": _int(r.get("cancelled")) or 0,
                "users": _int(r.get("users")) or 0,
                "input_tokens": _int(r.get("input_tokens")),
                "output_tokens": _int(r.get("output_tokens")),
                "avg_ttft_ms": _num(r.get("avg_ttft_ms")),
                "p95_ttft_ms": _num(r.get("p95_ttft_ms")),
                "avg_duration_ms": _num(r.get("avg_duration_ms")),
                "avg_tokens_per_second": _num(r.get("avg_tokens_per_second")),
            }
            for r in rows
        ],
        "effort": [
            {
                "effort": r["effort"],
                "requests": _int(r.get("requests")) or 0,
                "avg_ttft_ms": _num(r.get("avg_ttft_ms")),
                "avg_duration_ms": _num(r.get("avg_duration_ms")),
                "output_tokens": _int(r.get("output_tokens")),
            }
            for r in effort
        ],
        "engines": live,
    }


# ---------------------------------------------------------------------------
# Feature analytics
# ---------------------------------------------------------------------------


@router.get("/chat")
async def chat(
    range: str = Query("30d", pattern=RANGE_PATTERN),
    principal: Principal = Gate,
) -> dict:
    w = Window(range)
    ws = principal.workspace_id
    totals, prev, series, top, routes = await asyncio.gather(
        db.run_in_thread(analytics.chat_totals, ws, w.since, w.until),
        db.run_in_thread(analytics.chat_totals, ws, w.prev_since, w.prev_until),
        db.run_in_thread(analytics.chat_series, ws, w.since, w.until, w.bucket),
        db.run_in_thread(
            analytics.top_users, ws, w.since, w.until, metric="messages", limit=10
        ),
        db.run_in_thread(analytics.by_route, ws, w.since, w.until),
    )
    messages = _int(totals.get("messages")) or 0
    conversations = _int(totals.get("conversations")) or 0
    return {
        "range": w.payload(),
        "totals": {
            "messages": messages,
            "answers": _int(totals.get("answers")) or 0,
            "conversations": conversations,
            "new_conversations": _int(totals.get("new_conversations")) or 0,
            "users": _int(totals.get("users")) or 0,
            "thumbs_up": _int(totals.get("thumbs_up")) or 0,
            "thumbs_down": _int(totals.get("thumbs_down")) or 0,
            "messages_per_conversation": (
                round(messages / conversations, 2) if conversations else None
            ),
        },
        "deltas": {
            "messages": _delta(messages, _int(prev.get("messages"))),
            "conversations": _delta(
                conversations, _int(prev.get("conversations"))
            ),
        },
        "series": [
            {
                "bucket": _iso(r["bucket"]),
                "messages": _int(r.get("messages")) or 0,
                "answers": _int(r.get("answers")) or 0,
                "conversations": _int(r.get("conversations")) or 0,
            }
            for r in series
        ],
        "routes": [
            {"route": r["route"], "requests": _int(r.get("requests")) or 0}
            for r in routes
        ],
        "top_users": [_member(r) for r in top],
    }


@router.get("/research")
async def research(
    range: str = Query("30d", pattern=RANGE_PATTERN),
    principal: Principal = Gate,
) -> dict:
    w = Window(range)
    ws = principal.workspace_id
    totals, prev, series, top = await asyncio.gather(
        db.run_in_thread(analytics.research_totals, ws, w.since, w.until),
        db.run_in_thread(analytics.research_totals, ws, w.prev_since, w.prev_until),
        db.run_in_thread(analytics.research_series, ws, w.since, w.until, w.bucket),
        db.run_in_thread(
            analytics.top_users, ws, w.since, w.until, metric="research", limit=10
        ),
    )
    runs = _int(totals.get("runs")) or 0
    completed = _int(totals.get("completed")) or 0
    return {
        "range": w.payload(),
        "totals": {
            "runs": runs,
            "completed": completed,
            "failed": _int(totals.get("failed")) or 0,
            "cancelled": _int(totals.get("cancelled")) or 0,
            "running": _int(totals.get("running")) or 0,
            "users": _int(totals.get("users")) or 0,
            "queries": _int(totals.get("queries")) or 0,
            "citations": _int(totals.get("citations")) or 0,
            "success_rate": round(completed / runs * 100, 1) if runs else None,
            "avg_iterations": _num(totals.get("avg_iterations")),
            "avg_queries": _num(totals.get("avg_queries")),
            "avg_sources_found": _num(totals.get("avg_sources_found")),
            "avg_sources_cited": _num(totals.get("avg_sources_cited")),
            "avg_seconds": _num(totals.get("avg_seconds")),
            "p95_seconds": _num(totals.get("p95_seconds")),
            "avg_report_chars": _num(totals.get("avg_report_chars")),
        },
        "deltas": {"runs": _delta(runs, _int(prev.get("runs")))},
        "series": [
            {
                "bucket": _iso(r["bucket"]),
                "runs": _int(r.get("runs")) or 0,
                "completed": _int(r.get("completed")) or 0,
                "failed": _int(r.get("failed")) or 0,
            }
            for r in series
        ],
        "top_users": [_member(r) for r in top],
    }


@router.get("/search")
async def search(
    range: str = Query("30d", pattern=RANGE_PATTERN),
    principal: Principal = Gate,
) -> dict:
    w = Window(range)
    ws = principal.workspace_id
    totals, prev, series, providers, domains, top = await asyncio.gather(
        db.run_in_thread(analytics.search_totals, ws, w.since, w.until),
        db.run_in_thread(analytics.search_totals, ws, w.prev_since, w.prev_until),
        db.run_in_thread(analytics.search_series, ws, w.since, w.until, w.bucket),
        db.run_in_thread(analytics.search_providers, ws, w.since, w.until),
        db.run_in_thread(analytics.search_domains, w.since, w.until),
        db.run_in_thread(
            analytics.top_users, ws, w.since, w.until, metric="web_searches", limit=10
        ),
    )
    searches = _int(totals.get("searches")) or 0
    return {
        "range": w.payload(),
        "totals": {
            "searches": searches,
            "queries": _int(totals.get("queries")) or 0,
            "users": _int(totals.get("users")) or 0,
            "results": _int(totals.get("results")) or 0,
            "unique_urls": _int(totals.get("unique_urls")) or 0,
            "pages_fetched": _int(totals.get("pages_fetched")) or 0,
            "domains": _int(totals.get("domains")) or 0,
            "results_per_search": (
                round((_int(totals.get("results")) or 0) / searches, 1)
                if searches
                else None
            ),
        },
        "deltas": {"searches": _delta(searches, _int(prev.get("searches")))},
        "series": [
            {"bucket": _iso(r["bucket"]), "searches": _int(r.get("searches")) or 0}
            for r in series
        ],
        "providers": [
            {"provider": r["provider"], "searches": _int(r.get("searches")) or 0}
            for r in providers
        ],
        "domains": [
            {"domain": r["domain"], "pages": _int(r.get("pages")) or 0}
            for r in domains
        ],
        "top_users": [_member(r) for r in top],
    }


@router.get("/salesforce")
async def salesforce(
    range: str = Query("30d", pattern=RANGE_PATTERN),
    principal: Principal = Gate,
) -> dict:
    w = Window(range)
    ws = principal.workspace_id
    totals, prev, series = await asyncio.gather(
        db.run_in_thread(analytics.salesforce_totals, ws, w.since, w.until),
        db.run_in_thread(analytics.salesforce_totals, ws, w.prev_since, w.prev_until),
        db.run_in_thread(analytics.salesforce_series, ws, w.since, w.until, w.bucket),
    )
    answers = _int(totals.get("answers")) or 0
    return {
        "range": w.payload(),
        "totals": {
            "answers": answers,
            "users": _int(totals.get("users")) or 0,
            "failed": _int(totals.get("failed")) or 0,
            "live": _int(totals.get("live")) or 0,
            "synced": max(0, answers - (_int(totals.get("live")) or 0)),
            "sql_route": _int(totals.get("sql_route")) or 0,
            "dataset_route": _int(totals.get("dataset_route")) or 0,
            "intents": _int(totals.get("intents")) or 0,
            "success_rate": (
                round((answers - (_int(totals.get("failed")) or 0)) / answers * 100, 1)
                if answers
                else None
            ),
        },
        "deltas": {"answers": _delta(answers, _int(prev.get("answers")))},
        "series": [
            {
                "bucket": _iso(r["bucket"]),
                "answers": _int(r.get("answers")) or 0,
                "live": _int(r.get("live")) or 0,
            }
            for r in series
        ],
    }


@router.get("/voice")
async def voice(
    range: str = Query("30d", pattern=RANGE_PATTERN),
    principal: Principal = Gate,
) -> dict:
    """Voice dictation, as metadata.

    The table behind this endpoint holds no transcript text — only how long
    someone spoke, how long they waited, which language the model named and
    whether it worked — so there is nothing here that could leak what was
    said, by construction rather than by filtering.

    `coverage` is LIFETIME, not windowed, and it is the difference between
    "nobody dictated last week" and "voice has never been used here". The
    page says something different for each, and cannot tell them apart from
    a windowed zero.
    """
    w = Window(range)
    ws = principal.workspace_id
    totals, prev, series, languages, top, lifetime = await asyncio.gather(
        db.run_in_thread(analytics.voice_totals, ws, w.since, w.until),
        db.run_in_thread(analytics.voice_totals, ws, w.prev_since, w.prev_until),
        db.run_in_thread(analytics.voice_series, ws, w.since, w.until, w.bucket),
        db.run_in_thread(analytics.voice_languages, ws, w.since, w.until),
        db.run_in_thread(analytics.voice_top_users, ws, w.since, w.until),
        db.run_in_thread(analytics.voice_coverage, ws),
    )
    transcriptions = _int(totals.get("transcriptions")) or 0
    ok = _int(totals.get("ok")) or 0
    duration_ms = _int(totals.get("duration_ms"))
    identified = _int(totals.get("language_identified")) or 0
    return {
        "range": w.payload(),
        "coverage": {
            "first_transcription": _iso(lifetime.get("first_transcription")),
            "last_transcription": _iso(lifetime.get("last_transcription")),
            "transcriptions": _int(lifetime.get("transcriptions")) or 0,
        },
        "totals": {
            "transcriptions": transcriptions,
            "users": _int(totals.get("users")) or 0,
            "ok": ok,
            "failed": transcriptions - ok,
            "busy": _int(totals.get("busy")) or 0,
            "rejected": _int(totals.get("rejected")) or 0,
            "unavailable": _int(totals.get("unavailable")) or 0,
            "error": _int(totals.get("error")) or 0,
            "degraded": _int(totals.get("degraded")) or 0,
            "success_rate": (
                round(ok / transcriptions * 100, 1) if transcriptions else None
            ),
            # Nobody reporting a clip length is not zero speech, so the sum
            # stays None and the minutes derived from it stay None with it.
            "total_duration_ms": duration_ms,
            "total_minutes": (
                None if duration_ms is None else round(duration_ms / 60000, 1)
            ),
            "avg_duration_ms": _num(totals.get("avg_duration_ms")),
            "p95_duration_ms": _num(totals.get("p95_duration_ms")),
            "avg_processing_ms": _num(totals.get("avg_processing_ms")),
            "p95_processing_ms": _num(totals.get("p95_processing_ms")),
            "languages": _int(totals.get("languages")) or 0,
            "language_identified": identified,
        },
        "deltas": {
            "transcriptions": _delta(
                transcriptions, _int(prev.get("transcriptions"))
            ),
        },
        "series": [
            {
                "bucket": _iso(r["bucket"]),
                "transcriptions": _int(r.get("transcriptions")) or 0,
                "ok": _int(r.get("ok")) or 0,
                "failed": _int(r.get("failed")) or 0,
            }
            for r in series
        ],
        "languages": [
            {
                "language": r["language"],
                "transcriptions": _int(r.get("transcriptions")) or 0,
                "users": _int(r.get("users")) or 0,
                # Of the clips that WERE identified — sharing against every
                # transcription would quietly report the parser's silence as
                # a language nobody spoke.
                "share": (
                    round((_int(r.get("transcriptions")) or 0) / identified * 100, 1)
                    if identified
                    else None
                ),
            }
            for r in languages
        ],
        "top_users": [
            {**_member(r), "transcriptions": _int(r.get("transcriptions")) or 0}
            for r in top
        ],
    }


# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------


@router.get("/performance")
async def performance(
    range: str = Query("30d", pattern=RANGE_PATTERN),
    hours: int = Query(6, ge=1, le=48),
    model: str = Query("", max_length=128),
    principal: Principal = Gate,
) -> dict:
    """Two views of speed side by side: what USERS experienced (orchestrator
    telemetry, which includes retrieval and every pre-pass) and what the
    ENGINES did (vLLM's own histograms). They differ, and the difference is
    the platform's own overhead — which is the number worth watching."""
    w = Window(range)
    ws = principal.workspace_id
    (
        totals, prev, series, histogram, routes, available, live, live_series
    ) = await asyncio.gather(
        db.run_in_thread(analytics.totals, ws, w.since, w.until, model),
        db.run_in_thread(analytics.totals, ws, w.prev_since, w.prev_until, model),
        db.run_in_thread(analytics.series, ws, w.since, w.until, w.bucket, model),
        db.run_in_thread(analytics.latency_histogram, ws, w.since, w.until),
        db.run_in_thread(analytics.by_route, ws, w.since, w.until, model),
        db.run_in_thread(analytics.known_models, ws),
        infra.safe(infra.engines()),
        infra.safe(infra.inference_series(hours=hours)),
    )
    cur, before = _totals(totals), _totals(prev)
    return {
        "range": w.payload(),
        "available_models": available,
        "model": model,
        "totals": cur,
        "previous": before,
        "deltas": {
            "avg_ttft_ms": _delta(cur["avg_ttft_ms"], before["avg_ttft_ms"]),
            "p95_ttft_ms": _delta(cur["p95_ttft_ms"], before["p95_ttft_ms"]),
            "avg_duration_ms": _delta(cur["avg_duration_ms"], before["avg_duration_ms"]),
            "error_rate": _delta(
                (cur["errors"] / cur["requests"] * 100) if cur["requests"] else None,
                (before["errors"] / before["requests"] * 100)
                if before["requests"]
                else None,
            ),
        },
        "series": [
            {
                "bucket": _iso(r["bucket"]),
                "requests": _int(r.get("requests")) or 0,
                "errors": _int(r.get("errors")) or 0,
                "avg_ttft_ms": _num(r.get("avg_ttft_ms")),
            }
            for r in series
        ],
        "ttft_histogram": [
            {"label": r["label"], "count": _int(r.get("n")) or 0} for r in histogram
        ],
        "routes": [
            {
                "route": r["route"],
                "requests": _int(r.get("requests")) or 0,
                "errors": _int(r.get("errors")) or 0,
                "avg_ttft_ms": _num(r.get("avg_ttft_ms")),
                "avg_duration_ms": _num(r.get("avg_duration_ms")),
            }
            for r in routes
        ],
        "engines": live,
        "engine_series": live_series,
    }


@router.get("/infrastructure")
async def infrastructure(
    hours: int = Query(6, ge=1, le=48),
    principal: Principal = Gate,
) -> dict:
    """Nodes, GPUs and engines. Entirely from Prometheus — and honest about
    it: when the monitoring profile is not running, `available` is false and
    the console says so instead of drawing an idle-looking zero."""
    node_state, engine_state, gpu = await asyncio.gather(
        infra.safe(infra.nodes()),
        infra.safe(infra.engines()),
        infra.safe(infra.gpu_series(hours=hours)),
    )
    return {
        "hours": hours,
        "nodes": node_state,
        "engines": engine_state,
        "gpu_series": gpu,
    }
