"""Read-side aggregations for the super-admin analytics console.

DESIGN. Every function here answers one question over one window and returns
plain rows. There is no ORM, no cache and no materialised view: at this
workspace's scale (thousands of messages, tens of thousands of usage events)
an indexed aggregate is single-digit milliseconds, and a materialised view
would be a staleness bug waiting to happen. The indexes that make that true
are in migration V18; the moment a query here stops being fast the fix is a
rollup table, not a bigger machine.

WHERE THE NUMBERS COME FROM.

  usage_events   requests, tokens, TTFT, duration, model mix, error rate
                 (V18 — one row per model turn, written in app/usage.py)
  messages       messages, answers, conversations, daily active people
  research_runs  Deep Research runs, outcome, duration, sources
  web_searches   searches, providers, results, the domains behind them
  sf_intents     Salesforce planning activity
  voice_transcriptions  dictation: clip length, the wait, the language
                 heard, and how the attempt ended (V19)

NOTHING IS ESTIMATED. A count nobody made comes back NULL and the console
says so. That rule is why `usage_events.input_tokens` is nullable and why
none of these queries use coalesce() on a token column.

PRIVACY. Metadata only: no message content, no search query text, no report
bodies. Identity is limited to what the member list already shows.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from .. import db

#: The optional model filter, as a SQL fragment. Returned rather than
#: interpolated so every caller binds it as a parameter — no user input is
#: ever formatted into a query here.
def _model_clause(model: str) -> tuple:
    return (" AND model = %s", (model,)) if model else ("", ())


def _bucket_expr(alias: str, bucket: str) -> str:
    """`date_trunc` for the chosen granularity, guarded to a closed set."""
    unit = "hour" if bucket == "hour" else "day"
    return f"date_trunc('{unit}', {alias})"


def _series_gap(bucket: str) -> str:
    return "1 hour" if bucket == "hour" else "1 day"


# NOTE ON THE END BOUND. Every series below runs to
# `date_trunc(unit, until - 1 microsecond)`, NOT to `until - one bucket`.
# `until` is exclusive, so subtracting a whole bucket drops the bucket the
# window ENDS in — which is the current hour on a 24-hour view and TODAY on a
# 30-day one. A chart whose last point is always yesterday reads as "nobody
# used it today", which is the exact failure the admin overview was fixed for
# once already.


# ---------------------------------------------------------------------------
# usage_events — the request/token/latency half
# ---------------------------------------------------------------------------


def totals(
    workspace_id: str, since: datetime, until: datetime, model: str = ""
) -> Dict[str, Any]:
    """Headline numbers for one window.

    Token sums are NULL — not 0 — when nothing in the window reported usage,
    which is the difference between "no tokens were used" and "this build
    predates token telemetry".
    """
    model_sql, model_params = _model_clause(model)
    with db.connection() as con:
        row = con.execute(
            f"""SELECT count(*)                                    AS requests,
                      count(*) FILTER (WHERE status = 'ok')       AS ok,
                      count(*) FILTER (WHERE status = 'error')    AS errors,
                      count(*) FILTER (WHERE status = 'cancelled') AS cancelled,
                      count(DISTINCT user_id)                     AS users,
                      sum(input_tokens)                           AS input_tokens,
                      sum(output_tokens)                          AS output_tokens,
                      avg(ttft_ms)                                AS avg_ttft_ms,
                      percentile_disc(0.5) WITHIN GROUP (ORDER BY ttft_ms)  AS p50_ttft_ms,
                      percentile_disc(0.95) WITHIN GROUP (ORDER BY ttft_ms) AS p95_ttft_ms,
                      percentile_disc(0.99) WITHIN GROUP (ORDER BY ttft_ms) AS p99_ttft_ms,
                      avg(duration_ms)                            AS avg_duration_ms,
                      percentile_disc(0.5) WITHIN GROUP (ORDER BY duration_ms)  AS p50_duration_ms,
                      percentile_disc(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_duration_ms,
                      percentile_disc(0.99) WITHIN GROUP (ORDER BY duration_ms) AS p99_duration_ms,
                      -- Throughput is per REQUEST, then averaged: dividing
                      -- summed tokens by summed seconds would let one long
                      -- answer dominate the figure for every short one.
                      avg(output_tokens::numeric * 1000
                          / nullif(duration_ms, 0))               AS avg_tokens_per_second
                 FROM usage_events
                WHERE workspace_id = %s AND created_at >= %s AND created_at < %s
                  {model_sql}""",
            (workspace_id, since, until, *model_params),
        ).fetchone()
    return dict(row or {})


def series(
    workspace_id: str,
    since: datetime,
    until: datetime,
    bucket: str = "day",
    model: str = "",
) -> List[Dict[str, Any]]:
    """Requests, tokens and distinct people per bucket, with the quiet
    buckets filled in — a gap in a time series reads as missing data, not as
    a quiet Sunday.

    `errors` counts FAILURES only, here and in every other query on this
    module. A cancelled turn is someone pressing Stop; folding it into the
    error rate reports the platform working exactly as intended as a fault.
    Cancellations are counted separately, by `totals`.
    """
    trunc = _bucket_expr("created_at", bucket)
    trunc_unit = "hour" if bucket == "hour" else "day"
    step = _series_gap(bucket)
    model_sql, model_params = _model_clause(model)
    with db.connection() as con:
        return con.execute(
            f"""SELECT g.t AS bucket,
                       coalesce(s.requests, 0)  AS requests,
                       coalesce(s.errors, 0)    AS errors,
                       coalesce(s.users, 0)     AS users,
                       s.input_tokens, s.output_tokens,
                       s.avg_ttft_ms
                  FROM generate_series(
                          date_trunc('{trunc_unit}', %s::timestamptz),
                          date_trunc('{trunc_unit}', %s::timestamptz - interval '1 microsecond'),
                          interval '{step}') g(t)
                  LEFT JOIN (
                      SELECT {trunc} AS t,
                             count(*)                 AS requests,
                             count(*) FILTER (WHERE status = 'error') AS errors,
                             count(DISTINCT user_id)  AS users,
                             sum(input_tokens)        AS input_tokens,
                             sum(output_tokens)       AS output_tokens,
                             avg(ttft_ms)             AS avg_ttft_ms
                        FROM usage_events
                       WHERE workspace_id = %s AND created_at >= %s AND created_at < %s
                         {model_sql}
                       GROUP BY 1
                  ) s ON s.t = g.t
                 ORDER BY g.t""",
            (since, until, workspace_id, since, until, *model_params),
        ).fetchall()


def by_route(
    workspace_id: str, since: datetime, until: datetime, model: str = ""
) -> List[Dict[str, Any]]:
    """Which engine answered, with its own cost and speed."""
    model_sql, model_params = _model_clause(model)
    with db.connection() as con:
        return con.execute(
            f"""SELECT coalesce(nullif(route, ''), 'chat') AS route,
                      count(*) AS requests,
                      count(*) FILTER (WHERE status = 'error') AS errors,
                      sum(input_tokens)  AS input_tokens,
                      sum(output_tokens) AS output_tokens,
                      avg(ttft_ms)       AS avg_ttft_ms,
                      avg(duration_ms)   AS avg_duration_ms
                 FROM usage_events
                WHERE workspace_id = %s AND created_at >= %s AND created_at < %s
                  {model_sql}
                GROUP BY 1 ORDER BY requests DESC""",
            (workspace_id, since, until, *model_params),
        ).fetchall()


def by_model(
    workspace_id: str, since: datetime, until: datetime
) -> List[Dict[str, Any]]:
    """Per-model workload and speed — which local model carries the platform,
    and what it costs in time per token."""
    with db.connection() as con:
        return con.execute(
            """SELECT nullif(model, '') AS model,
                      count(*) AS requests,
                      count(*) FILTER (WHERE status = 'error')     AS errors,
                      count(*) FILTER (WHERE status = 'cancelled') AS cancelled,
                      count(DISTINCT user_id) AS users,
                      sum(input_tokens)  AS input_tokens,
                      sum(output_tokens) AS output_tokens,
                      avg(ttft_ms)       AS avg_ttft_ms,
                      percentile_disc(0.95) WITHIN GROUP (ORDER BY ttft_ms) AS p95_ttft_ms,
                      avg(duration_ms)   AS avg_duration_ms,
                      avg(output_tokens::numeric * 1000
                          / nullif(duration_ms, 0)) AS avg_tokens_per_second
                 FROM usage_events
                WHERE workspace_id = %s AND created_at >= %s AND created_at < %s
                  AND model <> ''
                GROUP BY 1 ORDER BY requests DESC""",
            (workspace_id, since, until),
        ).fetchall()


def by_effort(
    workspace_id: str, since: datetime, until: datetime
) -> List[Dict[str, Any]]:
    """Fast / Think / Max — the effort mix, and what each tier really costs."""
    with db.connection() as con:
        return con.execute(
            """SELECT coalesce(nullif(effort, ''), 'unset') AS effort,
                      count(*) AS requests,
                      avg(ttft_ms)     AS avg_ttft_ms,
                      avg(duration_ms) AS avg_duration_ms,
                      sum(output_tokens) AS output_tokens
                 FROM usage_events
                WHERE workspace_id = %s AND created_at >= %s AND created_at < %s
                GROUP BY 1 ORDER BY requests DESC""",
            (workspace_id, since, until),
        ).fetchall()


def latency_histogram(
    workspace_id: str, since: datetime, until: datetime
) -> List[Dict[str, Any]]:
    """TTFT distribution in fixed buckets — a mean hides the tail that people
    actually complain about."""
    with db.connection() as con:
        return con.execute(
            """SELECT b.label, b.lo, coalesce(c.n, 0) AS n
                 FROM (VALUES
                        ('<1s', 0, 1000), ('1-2s', 1000, 2000),
                        ('2-5s', 2000, 5000), ('5-10s', 5000, 10000),
                        ('10-30s', 10000, 30000), ('30s+', 30000, 2147483647)
                      ) AS b(label, lo, hi)
                 LEFT JOIN (
                     SELECT width_bucket(ttft_ms, ARRAY[1000, 2000, 5000, 10000, 30000]) AS i,
                            count(*) AS n
                       FROM usage_events
                      WHERE workspace_id = %s AND created_at >= %s AND created_at < %s
                        AND ttft_ms IS NOT NULL
                      GROUP BY 1
                 ) c ON c.i = (CASE b.lo WHEN 0 THEN 0 WHEN 1000 THEN 1 WHEN 2000 THEN 2
                                          WHEN 5000 THEN 3 WHEN 10000 THEN 4 ELSE 5 END)
                ORDER BY b.lo""",
            (workspace_id, since, until),
        ).fetchall()


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


def leaderboard(
    workspace_id: str,
    since: datetime,
    until: datetime,
    *,
    limit: int = 50,
    offset: int = 0,
    search: str = "",
    order: str = "output_tokens",
) -> Dict[str, Any]:
    """Per-person usage, ranked, paginated, searchable.

    LEFT JOINed from the roster so a member who used nothing still appears
    with zeros: "who is not using this" is half of what a leaderboard is
    opened to answer. Three correlated aggregates (research, searches,
    messages) are computed as scalar subqueries rather than more JOINs — with
    a dozen members that is cheaper than the row multiplication a fan-out
    join produces, and it keeps every count independent of the others.
    """
    orders = {
        "output_tokens": "coalesce(ue.output_tokens, 0)",
        "input_tokens": "coalesce(ue.input_tokens, 0)",
        "total_tokens": "coalesce(ue.input_tokens, 0) + coalesce(ue.output_tokens, 0)",
        "requests": "coalesce(ue.requests, 0)",
        "messages": "msg.messages",
        "research": "res.runs",
        "web_searches": "ws.searches",
    }
    order_sql = orders.get(order, orders["output_tokens"])
    like = f"%{search.strip().lower()}%" if search.strip() else None
    with db.connection() as con:
        rows = con.execute(
            f"""SELECT u.id, u.display_name, u.username, u.email, u.status,
                       wm.role, u.last_active_at,
                       coalesce(ue.requests, 0)  AS requests,
                       ue.input_tokens, ue.output_tokens,
                       ue.avg_ttft_ms,
                       coalesce(ue.errors, 0)    AS errors,
                       msg.messages, msg.conversations,
                       coalesce(res.runs, 0)     AS research_runs,
                       coalesce(ws.searches, 0)  AS web_searches,
                       count(*) OVER ()          AS total_rows
                  FROM workspace_memberships wm
                  JOIN users u ON u.id = wm.user_id
                  LEFT JOIN (
                      SELECT user_id, count(*) AS requests,
                             count(*) FILTER (WHERE status = 'error') AS errors,
                             sum(input_tokens) AS input_tokens,
                             sum(output_tokens) AS output_tokens,
                             avg(ttft_ms) AS avg_ttft_ms
                        FROM usage_events
                       WHERE workspace_id = %s AND created_at >= %s AND created_at < %s
                       GROUP BY user_id
                  ) ue ON ue.user_id = u.id
                  LEFT JOIN LATERAL (
                      SELECT count(*) FILTER (WHERE m.role = 'user') AS messages,
                             count(DISTINCT c.id) AS conversations
                        FROM conversations c
                        JOIN messages m ON m.conversation_id = c.id
                       WHERE c.user_id = u.id
                         AND m.created_at >= %s AND m.created_at < %s
                  ) msg ON true
                  LEFT JOIN LATERAL (
                      SELECT count(*) AS runs FROM research_runs r
                       WHERE r.user_id = u.id
                         AND r.started_at >= %s AND r.started_at < %s
                  ) res ON true
                  LEFT JOIN LATERAL (
                      SELECT count(*) AS searches FROM web_searches s
                       WHERE s.user_id = u.id
                         AND s.requested_at >= %s AND s.requested_at < %s
                  ) ws ON true
                 WHERE wm.workspace_id = %s
                   AND (%s::text IS NULL
                        OR lower(coalesce(u.display_name, '')) LIKE %s
                        OR lower(coalesce(u.email, '')) LIKE %s
                        OR lower(u.username) LIKE %s)
                 ORDER BY {order_sql} DESC NULLS LAST,
                          lower(coalesce(u.display_name, u.username))
                 LIMIT %s OFFSET %s""",
            (
                workspace_id, since, until,
                since, until,
                since, until,
                since, until,
                workspace_id,
                like, like, like, like,
                limit, offset,
            ),
        ).fetchall()
    total = int(rows[0]["total_rows"]) if rows else 0
    return {"rows": rows, "total": total}


def daily_active(
    workspace_id: str, since: datetime, until: datetime, bucket: str = "day"
) -> List[Dict[str, Any]]:
    """Distinct people per bucket, split by what they did.

    Counted from `messages`, not from `usage_events`: it reaches back through
    the whole history of the workspace, where usage events only start on the
    day telemetry was deployed. A chart of active people must not appear to
    begin at zero because the instrumentation did.
    """
    trunc_unit = "hour" if bucket == "hour" else "day"
    step = _series_gap(bucket)
    with db.connection() as con:
        return con.execute(
            f"""SELECT g.t AS bucket,
                       coalesce(s.active, 0)        AS active,
                       coalesce(s.messages, 0)      AS messages,
                       coalesce(s.chat, 0)          AS chat,
                       coalesce(s.research, 0)      AS research,
                       coalesce(s.web_search, 0)    AS web_search,
                       coalesce(s.salesforce, 0)    AS salesforce
                  FROM generate_series(
                          date_trunc('{trunc_unit}', %s::timestamptz),
                          date_trunc('{trunc_unit}', %s::timestamptz - interval '1 microsecond'),
                          interval '{step}') g(t)
                  LEFT JOIN (
                      SELECT date_trunc('{trunc_unit}', m.created_at) AS t,
                             count(DISTINCT c.user_id) AS active,
                             count(*) FILTER (WHERE m.role = 'user') AS messages,
                             count(DISTINCT c.user_id) FILTER (
                                 WHERE coalesce(m.meta->>'route', 'chat')
                                       IN ('chat', 'vision', 'agent', 'clarify')) AS chat,
                             count(DISTINCT c.user_id) FILTER (
                                 WHERE m.meta->>'route' = 'deep_research') AS research,
                             count(DISTINCT c.user_id) FILTER (
                                 WHERE m.meta->>'route' IN ('search', 'url')) AS web_search,
                             count(DISTINCT c.user_id) FILTER (
                                 WHERE m.meta->>'route' IN ('sql', 'dataset')) AS salesforce
                        FROM messages m
                        JOIN conversations c ON c.id = m.conversation_id
                        JOIN workspace_memberships wm ON wm.user_id = c.user_id
                       WHERE wm.workspace_id = %s
                         AND m.created_at >= %s AND m.created_at < %s
                       GROUP BY 1
                  ) s ON s.t = g.t
                 ORDER BY g.t""",
            (since, until, workspace_id, since, until),
        ).fetchall()


# ---------------------------------------------------------------------------
# Chat, research, search, Salesforce — the product half
# ---------------------------------------------------------------------------


def chat_totals(
    workspace_id: str, since: datetime, until: datetime
) -> Dict[str, Any]:
    with db.connection() as con:
        row = con.execute(
            """SELECT count(*) FILTER (WHERE m.role = 'user')      AS messages,
                      count(*) FILTER (WHERE m.role = 'assistant') AS answers,
                      count(DISTINCT c.id)                         AS conversations,
                      count(DISTINCT c.user_id)                    AS users,
                      count(*) FILTER (WHERE m.role = 'assistant'
                                       AND m.feedback = 'up')      AS thumbs_up,
                      count(*) FILTER (WHERE m.role = 'assistant'
                                       AND m.feedback = 'down')    AS thumbs_down
                 FROM messages m
                 JOIN conversations c ON c.id = m.conversation_id
                 JOIN workspace_memberships wm ON wm.user_id = c.user_id
                WHERE wm.workspace_id = %s
                  AND m.created_at >= %s AND m.created_at < %s""",
            (workspace_id, since, until),
        ).fetchone()
        started = con.execute(
            """SELECT count(*) AS new_conversations
                 FROM conversations c
                 JOIN workspace_memberships wm ON wm.user_id = c.user_id
                WHERE wm.workspace_id = %s
                  AND c.created_at >= %s AND c.created_at < %s""",
            (workspace_id, since, until),
        ).fetchone()
    out = dict(row or {})
    out.update(dict(started or {}))
    return out


def chat_series(
    workspace_id: str, since: datetime, until: datetime, bucket: str = "day"
) -> List[Dict[str, Any]]:
    trunc_unit = "hour" if bucket == "hour" else "day"
    step = _series_gap(bucket)
    with db.connection() as con:
        return con.execute(
            f"""SELECT g.t AS bucket,
                       coalesce(s.messages, 0)      AS messages,
                       coalesce(s.answers, 0)       AS answers,
                       coalesce(s.conversations, 0) AS conversations
                  FROM generate_series(
                          date_trunc('{trunc_unit}', %s::timestamptz),
                          date_trunc('{trunc_unit}', %s::timestamptz - interval '1 microsecond'),
                          interval '{step}') g(t)
                  LEFT JOIN (
                      SELECT date_trunc('{trunc_unit}', m.created_at) AS t,
                             count(*) FILTER (WHERE m.role = 'user') AS messages,
                             count(*) FILTER (WHERE m.role = 'assistant') AS answers,
                             count(DISTINCT c.id) AS conversations
                        FROM messages m
                        JOIN conversations c ON c.id = m.conversation_id
                        JOIN workspace_memberships wm ON wm.user_id = c.user_id
                       WHERE wm.workspace_id = %s
                         AND m.created_at >= %s AND m.created_at < %s
                       GROUP BY 1
                  ) s ON s.t = g.t
                 ORDER BY g.t""",
            (since, until, workspace_id, since, until),
        ).fetchall()


def research_totals(
    workspace_id: str, since: datetime, until: datetime
) -> Dict[str, Any]:
    with db.connection() as con:
        row = con.execute(
            """SELECT count(*) AS runs,
                      count(*) FILTER (WHERE r.status = 'done')      AS completed,
                      count(*) FILTER (WHERE r.status = 'failed')    AS failed,
                      count(*) FILTER (WHERE r.status = 'cancelled') AS cancelled,
                      count(*) FILTER (WHERE r.status = 'running')   AS running,
                      count(DISTINCT r.user_id) AS users,
                      avg(r.iterations)    AS avg_iterations,
                      avg(r.queries_run)   AS avg_queries,
                      avg(r.sources_found) AS avg_sources_found,
                      avg(r.sources_cited) AS avg_sources_cited,
                      sum(r.queries_run)   AS queries,
                      sum(r.sources_cited) AS citations,
                      avg(extract(epoch FROM (r.finished_at - r.started_at)))
                          FILTER (WHERE r.finished_at IS NOT NULL) AS avg_seconds,
                      percentile_disc(0.95) WITHIN GROUP (
                          ORDER BY extract(epoch FROM (r.finished_at - r.started_at)))
                          FILTER (WHERE r.finished_at IS NOT NULL) AS p95_seconds,
                      avg(length(r.report)) FILTER (WHERE r.report <> '') AS avg_report_chars
                 FROM research_runs r
                 JOIN workspace_memberships wm ON wm.user_id = r.user_id
                WHERE wm.workspace_id = %s
                  AND r.started_at >= %s AND r.started_at < %s""",
            (workspace_id, since, until),
        ).fetchone()
    return dict(row or {})


def research_series(
    workspace_id: str, since: datetime, until: datetime, bucket: str = "day"
) -> List[Dict[str, Any]]:
    trunc_unit = "hour" if bucket == "hour" else "day"
    step = _series_gap(bucket)
    with db.connection() as con:
        return con.execute(
            f"""SELECT g.t AS bucket,
                       coalesce(s.runs, 0)      AS runs,
                       coalesce(s.completed, 0) AS completed,
                       coalesce(s.failed, 0)    AS failed
                  FROM generate_series(
                          date_trunc('{trunc_unit}', %s::timestamptz),
                          date_trunc('{trunc_unit}', %s::timestamptz - interval '1 microsecond'),
                          interval '{step}') g(t)
                  LEFT JOIN (
                      SELECT date_trunc('{trunc_unit}', r.started_at) AS t,
                             count(*) AS runs,
                             count(*) FILTER (WHERE r.status = 'done') AS completed,
                             count(*) FILTER (WHERE r.status = 'failed') AS failed
                        FROM research_runs r
                        JOIN workspace_memberships wm ON wm.user_id = r.user_id
                       WHERE wm.workspace_id = %s
                         AND r.started_at >= %s AND r.started_at < %s
                       GROUP BY 1
                  ) s ON s.t = g.t
                 ORDER BY g.t""",
            (since, until, workspace_id, since, until),
        ).fetchall()


def search_totals(
    workspace_id: str, since: datetime, until: datetime
) -> Dict[str, Any]:
    """Web-search activity. `queries` unnests the stored query array, so a
    search that fanned out to four queries counts as four — that is what the
    search backend actually served."""
    with db.connection() as con:
        row = con.execute(
            """SELECT count(*) AS searches,
                      count(DISTINCT s.user_id) AS users,
                      coalesce(sum(jsonb_array_length(
                          CASE WHEN jsonb_typeof(s.queries) = 'array'
                               THEN s.queries ELSE '[]'::jsonb END)), 0) AS queries
                 FROM web_searches s
                 JOIN workspace_memberships wm ON wm.user_id = s.user_id
                WHERE wm.workspace_id = %s
                  AND s.requested_at >= %s AND s.requested_at < %s""",
            (workspace_id, since, until),
        ).fetchone()
        results = con.execute(
            """SELECT count(*) AS results, count(DISTINCT r.url_key) AS unique_urls
                 FROM web_results r
                 JOIN web_searches s ON s.id = r.search_id
                 JOIN workspace_memberships wm ON wm.user_id = s.user_id
                WHERE wm.workspace_id = %s
                  AND s.requested_at >= %s AND s.requested_at < %s""",
            (workspace_id, since, until),
        ).fetchone()
        pages = con.execute(
            """SELECT count(*) AS pages_fetched,
                      count(DISTINCT split_part(
                          regexp_replace(url, '^https?://(www\\.)?', ''), '/', 1)) AS domains
                 FROM web_pages
                WHERE fetched_at >= %s AND fetched_at < %s""",
            (since, until),
        ).fetchone()
    out = dict(row or {})
    out.update(dict(results or {}))
    out.update(dict(pages or {}))
    return out


def search_providers(
    workspace_id: str, since: datetime, until: datetime
) -> List[Dict[str, Any]]:
    with db.connection() as con:
        return con.execute(
            """SELECT s.provider, count(*) AS searches
                 FROM web_searches s
                 JOIN workspace_memberships wm ON wm.user_id = s.user_id
                WHERE wm.workspace_id = %s
                  AND s.requested_at >= %s AND s.requested_at < %s
                GROUP BY 1 ORDER BY searches DESC""",
            (workspace_id, since, until),
        ).fetchall()


def search_domains(
    since: datetime, until: datetime, limit: int = 12
) -> List[Dict[str, Any]]:
    """The domains the platform actually read. Global by design: the page
    store is shared knowledge, not per-workspace content."""
    with db.connection() as con:
        return con.execute(
            """SELECT split_part(regexp_replace(url, '^https?://(www\\.)?', ''), '/', 1) AS domain,
                      count(*) AS pages
                 FROM web_pages
                WHERE fetched_at >= %s AND fetched_at < %s
                GROUP BY 1 ORDER BY pages DESC LIMIT %s""",
            (since, until, limit),
        ).fetchall()


def search_series(
    workspace_id: str, since: datetime, until: datetime, bucket: str = "day"
) -> List[Dict[str, Any]]:
    trunc_unit = "hour" if bucket == "hour" else "day"
    step = _series_gap(bucket)
    with db.connection() as con:
        return con.execute(
            f"""SELECT g.t AS bucket, coalesce(s.searches, 0) AS searches
                  FROM generate_series(
                          date_trunc('{trunc_unit}', %s::timestamptz),
                          date_trunc('{trunc_unit}', %s::timestamptz - interval '1 microsecond'),
                          interval '{step}') g(t)
                  LEFT JOIN (
                      SELECT date_trunc('{trunc_unit}', s.requested_at) AS t, count(*) AS searches
                        FROM web_searches s
                        JOIN workspace_memberships wm ON wm.user_id = s.user_id
                       WHERE wm.workspace_id = %s
                         AND s.requested_at >= %s AND s.requested_at < %s
                       GROUP BY 1
                  ) s ON s.t = g.t
                 ORDER BY g.t""",
            (since, until, workspace_id, since, until),
        ).fetchall()


def salesforce_totals(
    workspace_id: str, since: datetime, until: datetime
) -> Dict[str, Any]:
    """Salesforce activity as METADATA only — how many questions were asked
    of the CRM and how they were served, never which records came back."""
    with db.connection() as con:
        answers = con.execute(
            """SELECT count(*) AS answers,
                      count(DISTINCT c.user_id) AS users,
                      count(*) FILTER (WHERE m.meta ? 'salesforce_error') AS failed,
                      count(*) FILTER (WHERE m.meta->>'salesforce_mode' = 'live') AS live,
                      count(*) FILTER (WHERE m.meta->>'route' = 'sql')     AS sql_route,
                      count(*) FILTER (WHERE m.meta->>'route' = 'dataset') AS dataset_route
                 FROM messages m
                 JOIN conversations c ON c.id = m.conversation_id
                 JOIN workspace_memberships wm ON wm.user_id = c.user_id
                WHERE wm.workspace_id = %s AND m.role = 'assistant'
                  AND m.created_at >= %s AND m.created_at < %s
                  AND (m.meta->>'mode' = 'salesforce'
                       OR m.meta->>'route' IN ('sql', 'dataset'))""",
            (workspace_id, since, until),
        ).fetchone()
        intents = con.execute(
            """SELECT count(*) AS intents FROM sf_intents
                WHERE created_at >= %s AND created_at < %s""",
            (since, until),
        ).fetchone()
    out = dict(answers or {})
    out.update(dict(intents or {}))
    return out


def salesforce_series(
    workspace_id: str, since: datetime, until: datetime, bucket: str = "day"
) -> List[Dict[str, Any]]:
    trunc_unit = "hour" if bucket == "hour" else "day"
    step = _series_gap(bucket)
    with db.connection() as con:
        return con.execute(
            f"""SELECT g.t AS bucket,
                       coalesce(s.answers, 0) AS answers,
                       coalesce(s.live, 0)    AS live
                  FROM generate_series(
                          date_trunc('{trunc_unit}', %s::timestamptz),
                          date_trunc('{trunc_unit}', %s::timestamptz - interval '1 microsecond'),
                          interval '{step}') g(t)
                  LEFT JOIN (
                      SELECT date_trunc('{trunc_unit}', m.created_at) AS t,
                             count(*) AS answers,
                             count(*) FILTER (WHERE m.meta->>'salesforce_mode' = 'live') AS live
                        FROM messages m
                        JOIN conversations c ON c.id = m.conversation_id
                        JOIN workspace_memberships wm ON wm.user_id = c.user_id
                       WHERE wm.workspace_id = %s AND m.role = 'assistant'
                         AND m.created_at >= %s AND m.created_at < %s
                         AND (m.meta->>'mode' = 'salesforce'
                              OR m.meta->>'route' IN ('sql', 'dataset'))
                       GROUP BY 1
                  ) s ON s.t = g.t
                 ORDER BY g.t""",
            (since, until, workspace_id, since, until),
        ).fetchall()


# ---------------------------------------------------------------------------
# Voice dictation
# ---------------------------------------------------------------------------


def voice_totals(
    workspace_id: str, since: datetime, until: datetime
) -> Dict[str, Any]:
    """Dictation over one window: how much was said, how long people waited
    for the words to come back, and how each attempt ended.

    Durations are summed and averaged WITHOUT coalesce. A clip whose length
    the browser never reported is not a clip of zero seconds, and a window
    nobody dictated in has no average to report — both come back NULL so the
    console can say so instead of printing a confident zero.

    The counts are the opposite case and are safe to read as numbers: a
    status nobody produced happened zero times. `processing_ms` is measured
    on failures too, because the wait before a 503 is still a wait somebody
    sat through.

    Scoped through `workspace_memberships`: the table records who spoke, not
    which workspace they spoke in, exactly like research runs and searches.
    """
    with db.connection() as con:
        row = con.execute(
            """SELECT count(*)                                         AS transcriptions,
                      count(DISTINCT v.user_id)                        AS users,
                      count(*) FILTER (WHERE v.status = 'ok')          AS ok,
                      count(*) FILTER (WHERE v.status = 'busy')        AS busy,
                      count(*) FILTER (WHERE v.status = 'rejected')    AS rejected,
                      count(*) FILTER (WHERE v.status = 'unavailable') AS unavailable,
                      count(*) FILTER (WHERE v.status = 'error')       AS error,
                      count(*) FILTER (WHERE v.degraded)               AS degraded,
                      count(*) FILTER (WHERE v.language IS NOT NULL)   AS language_identified,
                      count(DISTINCT v.language)                       AS languages,
                      sum(v.duration_ms)                               AS duration_ms,
                      avg(v.duration_ms)                               AS avg_duration_ms,
                      percentile_disc(0.95) WITHIN GROUP (ORDER BY v.duration_ms)
                          AS p95_duration_ms,
                      avg(v.processing_ms)                             AS avg_processing_ms,
                      percentile_disc(0.95) WITHIN GROUP (ORDER BY v.processing_ms)
                          AS p95_processing_ms
                 FROM voice_transcriptions v
                 JOIN workspace_memberships wm ON wm.user_id = v.user_id
                WHERE wm.workspace_id = %s
                  AND v.created_at >= %s AND v.created_at < %s""",
            (workspace_id, since, until),
        ).fetchone()
    return dict(row or {})


def voice_series(
    workspace_id: str, since: datetime, until: datetime, bucket: str = "day"
) -> List[Dict[str, Any]]:
    """Dictation per bucket, split by outcome.

    `failed` is every status that is not 'ok' — busy, rejected, unavailable
    and error together. They are told apart by `voice_totals`; on a trend
    line the only question is whether the person got their words back.
    """
    trunc_unit = "hour" if bucket == "hour" else "day"
    step = _series_gap(bucket)
    with db.connection() as con:
        return con.execute(
            f"""SELECT g.t AS bucket,
                       coalesce(s.transcriptions, 0) AS transcriptions,
                       coalesce(s.ok, 0)             AS ok,
                       coalesce(s.failed, 0)         AS failed
                  FROM generate_series(
                          date_trunc('{trunc_unit}', %s::timestamptz),
                          date_trunc('{trunc_unit}', %s::timestamptz - interval '1 microsecond'),
                          interval '{step}') g(t)
                  LEFT JOIN (
                      SELECT date_trunc('{trunc_unit}', v.created_at) AS t,
                             count(*) AS transcriptions,
                             count(*) FILTER (WHERE v.status = 'ok')  AS ok,
                             count(*) FILTER (WHERE v.status <> 'ok') AS failed
                        FROM voice_transcriptions v
                        JOIN workspace_memberships wm ON wm.user_id = v.user_id
                       WHERE wm.workspace_id = %s
                         AND v.created_at >= %s AND v.created_at < %s
                       GROUP BY 1
                  ) s ON s.t = g.t
                 ORDER BY g.t""",
            (since, until, workspace_id, since, until),
        ).fetchall()


def voice_languages(
    workspace_id: str, since: datetime, until: datetime, limit: int = 12
) -> List[Dict[str, Any]]:
    """The languages the model actually heard, ranked.

    Only rows where identification survived: `language` is NULL when the
    engine did not name one, and an "unknown" bar built out of those would
    be a count of the parser's silence, not of a language anybody spoke.
    How many clips were identified at all is reported by `voice_totals`, so
    the share below always has an honest denominator.
    """
    with db.connection() as con:
        return con.execute(
            """SELECT v.language,
                      count(*)                  AS transcriptions,
                      count(DISTINCT v.user_id) AS users
                 FROM voice_transcriptions v
                 JOIN workspace_memberships wm ON wm.user_id = v.user_id
                WHERE wm.workspace_id = %s
                  AND v.created_at >= %s AND v.created_at < %s
                  AND v.language IS NOT NULL
                GROUP BY 1 ORDER BY transcriptions DESC, v.language LIMIT %s""",
            (workspace_id, since, until, limit),
        ).fetchall()


def voice_top_users(
    workspace_id: str, since: datetime, until: datetime, limit: int = 10
) -> List[Dict[str, Any]]:
    """Who dictates, ranked by how often.

    Joined FROM the transcriptions rather than from the roster, unlike
    `leaderboard`: "who has not tried voice" is the whole member list, which
    is a list this page has no reason to draw.
    """
    with db.connection() as con:
        return con.execute(
            """SELECT u.id, u.display_name, u.username, u.email, u.status,
                      wm.role, u.last_active_at,
                      count(*)                                AS transcriptions,
                      count(*) FILTER (WHERE v.status = 'ok') AS ok,
                      sum(v.duration_ms)                      AS duration_ms
                 FROM voice_transcriptions v
                 JOIN users u ON u.id = v.user_id
                 JOIN workspace_memberships wm
                      ON wm.user_id = u.id AND wm.workspace_id = %s
                WHERE v.created_at >= %s AND v.created_at < %s
                GROUP BY u.id, u.display_name, u.username, u.email, u.status,
                         wm.role, u.last_active_at
                ORDER BY transcriptions DESC,
                         lower(coalesce(u.display_name, u.username))
                LIMIT %s""",
            (workspace_id, since, until, limit),
        ).fetchall()


def voice_coverage(workspace_id: str) -> Dict[str, Any]:
    """Dictation over ALL time, so the page can tell the two empty states
    apart: a deployment where nobody has ever pressed the microphone reads
    very differently from a quiet fortnight, and only one of them is worth
    explaining to the reader."""
    with db.connection() as con:
        row = con.execute(
            """SELECT min(v.created_at) AS first_transcription,
                      max(v.created_at) AS last_transcription,
                      count(*)          AS transcriptions
                 FROM voice_transcriptions v
                 JOIN workspace_memberships wm ON wm.user_id = v.user_id
                WHERE wm.workspace_id = %s""",
            (workspace_id,),
        ).fetchone()
    return dict(row or {})


def top_users(
    workspace_id: str,
    since: datetime,
    until: datetime,
    *,
    metric: str,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """The right-hand rail's short list: the same numbers as the leaderboard,
    ordered by one metric, without the pagination machinery."""
    board = leaderboard(
        workspace_id, since, until, limit=limit, offset=0, order=metric
    )
    return board["rows"]


def coverage(workspace_id: str) -> Dict[str, Any]:
    """When telemetry started, so the console can say "no data before X"
    instead of drawing a cliff and letting the reader infer a collapse."""
    with db.connection() as con:
        row = con.execute(
            """SELECT min(created_at) AS first_event, max(created_at) AS last_event,
                      count(*) AS events
                 FROM usage_events WHERE workspace_id = %s""",
            (workspace_id,),
        ).fetchone()
    return dict(row or {})


def known_models(workspace_id: str) -> List[str]:
    """Models that have actually served a request here — the filter's options
    come from the data, never from a hardcoded list that drifts."""
    with db.connection() as con:
        rows = con.execute(
            """SELECT DISTINCT model FROM usage_events
                WHERE workspace_id = %s AND model <> ''
                ORDER BY model""",
            (workspace_id,),
        ).fetchall()
    return [str(r["model"]) for r in rows]


