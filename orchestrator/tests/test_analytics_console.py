"""The super-admin analytics console.

Three properties are load-bearing and each has a test here:

  1. THE GATE. `analytics.read` belongs to SUPER_ADMIN alone. An ordinary
     admin — who may add members, reset passwords and read their conversations
     — must not reach platform telemetry by typing the URL. The refusal is a
     404, so the surface does not confirm its own existence.

  2. NULL IS NOT ZERO. A window nobody measured reports `null` tokens, not 0.
     Rendering "0 tokens processed" for a period that predates the telemetry
     is the kind of quiet lie a console exists to avoid.

  3. THE COMPARISON IS HONEST. Percentage change is computed against a window
     of the SAME LENGTH, and it is `null` — not 0, not infinity — whenever the
     previous window is empty.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import db


def _uid(username: str) -> int:
    return int(db.get_user_by_username(username)["id"])


def _workspace() -> str:
    with db.connection() as con:
        row = con.execute(
            "SELECT workspace_id FROM workspace_memberships LIMIT 1"
        ).fetchone()
    return str(row["workspace_id"])


def _event(
    user: int,
    *,
    hours_ago: float = 1.0,
    route: str = "chat",
    model: str = "Qwen/Test-7B",
    effort: str = "fast",
    input_tokens: int | None = 100,
    output_tokens: int | None = 400,
    ttft_ms: int | None = 900,
    duration_ms: int | None = 8000,
    status: str = "ok",
) -> None:
    """One usage event, as app/usage.py would have written it."""
    from app import usage

    usage.record(
        user_id=user,
        workspace_id=_workspace(),
        conversation_id=f"conv-{user}",
        generation_id=None,
        route=route,
        effort=effort,
        model=model,
        mode="assistant",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        ttft_ms=ttft_ms,
        duration_ms=duration_ms,
        status=status,
        created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    )


@pytest.fixture()
def console(login_client):
    """A super admin, an admin and a member, with a little real usage."""
    root = login_client("root", role="super_admin")
    admin = login_client("ada", role="admin")
    member = login_client("mo")
    bob = _uid("ada")
    for i in range(3):
        _event(bob, hours_ago=1 + i, output_tokens=400 + i)
    _event(_uid("mo"), hours_ago=2, route="deep_research", output_tokens=2000)
    _event(_uid("mo"), hours_ago=3, status="error", output_tokens=0, ttft_ms=None)
    return root, admin, member


# ---------------------------------------------------------------------------
# 1. The gate
# ---------------------------------------------------------------------------

ENDPOINTS = [
    "/admin/api/analytics/overview",
    "/admin/api/analytics/leaderboard",
    "/admin/api/analytics/models",
    "/admin/api/analytics/chat",
    "/admin/api/analytics/research",
    "/admin/api/analytics/search",
    "/admin/api/analytics/salesforce",
    "/admin/api/analytics/voice",
    "/admin/api/analytics/performance",
    "/admin/api/analytics/infrastructure",
]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_a_super_admin_reaches_every_analytics_endpoint(console, path):
    root, _admin, _member = console
    assert root.get(path).status_code == 200


@pytest.mark.parametrize("path", ENDPOINTS)
def test_an_admin_cannot_reach_analytics_by_url(console, path):
    """The whole point of a separate capability. An admin runs the workspace's
    people; platform telemetry is not part of that job, and hiding the nav
    link would not stop anyone who knows the URL."""
    _root, admin, _member = console
    assert admin.get(path).status_code == 404


@pytest.mark.parametrize("path", ENDPOINTS)
def test_a_member_cannot_reach_analytics_by_url(console, path):
    _root, _admin, member = console
    assert member.get(path).status_code == 404


def test_the_capability_is_not_granted_to_admins_in_the_table(console):
    """Guards the grant itself, not just today's routes: a future capability
    added to _ADMIN_CAPS by habit would be caught here."""
    from app.authn.rbac import Cap, ROLE_CAPS, Role

    assert Cap.ANALYTICS_READ in ROLE_CAPS[Role.SUPER_ADMIN]
    assert Cap.ANALYTICS_READ not in ROLE_CAPS[Role.ADMIN]
    assert Cap.ANALYTICS_READ not in ROLE_CAPS[Role.MEMBER]


def test_the_capability_reaches_the_client_through_auth_me(console):
    """The sidebar decides what to draw from ME_PAYLOAD.capabilities."""
    root, admin, _member = console
    assert "analytics.read" in root.get("/auth/me").json()["capabilities"]
    assert "analytics.read" not in admin.get("/auth/me").json()["capabilities"]


# ---------------------------------------------------------------------------
# 2. The numbers
# ---------------------------------------------------------------------------


def test_the_overview_counts_requests_tokens_and_failures(console):
    root, _admin, _member = console
    data = root.get("/admin/api/analytics/overview", params={"range": "24h"}).json()
    totals = data["totals"]
    assert totals["requests"] == 5
    assert totals["errors"] == 1
    assert totals["users"] == 2
    # 3 × (400,401,402) + 2000 + 0 output, 5 × 100 input.
    assert totals["output_tokens"] == 400 + 401 + 402 + 2000
    assert totals["input_tokens"] == 500
    assert totals["total_tokens"] == totals["input_tokens"] + totals["output_tokens"]


def test_a_window_with_no_telemetry_reports_null_tokens_not_zero(console):
    """The distinction the whole console rests on: 0 means "measured, and it
    was nothing"; null means "nobody measured"."""
    root, _admin, _member = console
    # 1 hour reaches back past nothing — every seeded event is older.
    data = root.get("/admin/api/analytics/overview", params={"range": "1h"}).json()
    assert data["totals"]["requests"] == 0
    assert data["totals"]["output_tokens"] is None
    assert data["totals"]["total_tokens"] is None
    assert data["totals"]["avg_ttft_ms"] is None


def test_percent_change_is_null_when_the_previous_window_is_empty(console):
    root, _admin, _member = console
    data = root.get("/admin/api/analytics/overview", params={"range": "24h"}).json()
    # Nothing was seeded 24-48 hours ago, so "up from zero" has no percentage.
    assert data["deltas"]["requests"] is None
    assert data["previous"]["requests"] == 0


def test_the_previous_window_is_the_same_length_as_the_current_one(console):
    root, _admin, _member = console
    for key in ("24h", "7d", "30d"):
        r = root.get("/admin/api/analytics/overview", params={"range": key}).json()[
            "range"
        ]
        current = datetime.fromisoformat(r["until"]) - datetime.fromisoformat(r["since"])
        previous = datetime.fromisoformat(r["previous_until"]) - datetime.fromisoformat(
            r["previous_since"]
        )
        assert current == previous
        assert r["previous_until"] == r["since"]


def test_the_series_has_one_point_per_bucket_with_no_gaps(console):
    root, _admin, _member = console
    data = root.get("/admin/api/analytics/overview", params={"range": "24h"}).json()
    assert data["range"]["bucket"] == "hour"
    points = data["series"]["usage"]
    # 25, not 24: a 24-hour window that ends NOW spans 24 whole hours plus the
    # partial one in progress, and dropping that one would hide the last hour
    # of activity. Both ends are partial; the count is right.
    assert len(points) == 25
    assert [p["bucket"] for p in points] == sorted(p["bucket"] for p in points)
    assert sum(p["requests"] for p in points) == 5
    # No holes: generate_series fills the quiet hours so a gap in the line
    # always means missing DATA, never a quiet afternoon.
    assert all("bucket" in p for p in points)


def test_the_series_reaches_the_bucket_the_window_ends_in(console):
    """The classic off-by-one that makes a dashboard say nobody used it today.

    `until` is exclusive, so a series that stops one whole bucket before it
    drops the CURRENT hour on a 24-hour view and TODAY on a 30-day one.
    """
    root, _admin, _member = console
    from datetime import datetime, timezone

    _event(_uid("mo"), hours_ago=0.02, output_tokens=7)  # about a minute ago

    hourly = root.get(
        "/admin/api/analytics/overview", params={"range": "24h"}
    ).json()
    last = hourly["series"]["usage"][-1]
    now_hour = datetime.now(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    assert datetime.fromisoformat(last["bucket"]) == now_hour
    assert last["requests"] >= 1, "the event from a minute ago fell off the end"

    daily = root.get(
        "/admin/api/analytics/overview", params={"range": "30d"}
    ).json()
    today = datetime.now(timezone.utc).date()
    assert datetime.fromisoformat(daily["series"]["usage"][-1]["bucket"]).date() == today
    assert daily["series"]["usage"][-1]["requests"] >= 1


def test_every_series_on_every_page_ends_today(console):
    """The same bound, on all six of them — they are separate queries and
    only one of them had a test."""
    root, _admin, _member = console
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date()
    pages = [
        ("chat", lambda d: d["series"]),
        ("research", lambda d: d["series"]),
        ("search", lambda d: d["series"]),
        ("salesforce", lambda d: d["series"]),
        ("voice", lambda d: d["series"]),
    ]
    for name, pick in pages:
        data = root.get(
            f"/admin/api/analytics/{name}", params={"range": "30d"}
        ).json()
        points = pick(data)
        assert points, f"{name} returned no series"
        assert (
            datetime.fromisoformat(points[-1]["bucket"]).date() == today
        ), f"{name}'s series stops before today"
    active = root.get(
        "/admin/api/analytics/overview", params={"range": "30d"}
    ).json()["series"]["active_users"]
    assert datetime.fromisoformat(active[-1]["bucket"]).date() == today


def test_a_bad_range_is_refused_rather_than_silently_defaulted(console):
    root, _admin, _member = console
    assert (
        root.get("/admin/api/analytics/overview", params={"range": "all"}).status_code
        == 422
    )


def test_the_leaderboard_ranks_paginates_and_searches(console):
    root, _admin, _member = console
    board = root.get(
        "/admin/api/analytics/leaderboard",
        params={"range": "24h", "order": "output_tokens", "limit": 1},
    ).json()
    assert board["total"] >= 3  # every member is listed, including silent ones
    assert len(board["rows"]) == 1
    assert board["rows"][0]["name"] == "mo"  # 2000 output tokens leads

    by_requests = root.get(
        "/admin/api/analytics/leaderboard",
        params={"range": "24h", "order": "requests", "limit": 1},
    ).json()
    assert by_requests["rows"][0]["name"] == "ada"  # three requests leads

    found = root.get(
        "/admin/api/analytics/leaderboard", params={"range": "24h", "search": "ada"}
    ).json()
    assert [r["name"] for r in found["rows"]] == ["ada"]


def test_the_leaderboard_lists_members_who_used_nothing(console):
    """"Who is not using this" is half of what a leaderboard answers."""
    root, _admin, _member = console
    board = root.get(
        "/admin/api/analytics/leaderboard", params={"range": "24h", "limit": 50}
    ).json()
    names = {r["name"] for r in board["rows"]}
    assert "root" in names
    assert next(r for r in board["rows"] if r["name"] == "root")["requests"] == 0


def test_models_report_share_latency_and_throughput(console):
    root, _admin, _member = console
    data = root.get("/admin/api/analytics/models", params={"range": "24h"}).json()
    models = {m["model"]: m for m in data["models"]}
    assert "Qwen/Test-7B" in models
    row = models["Qwen/Test-7B"]
    assert row["requests"] == 5 and row["share"] == 100.0
    assert row["avg_ttft_ms"] == pytest.approx(900, rel=0.01)
    assert row["errors"] == 1


def test_the_model_filter_narrows_requests_but_not_history(console):
    """The filter applies to REQUEST-shaped figures only.

    Messages and active people are counted from conversation history, which
    carries no model, so narrowing them would make two numbers on one page
    mean different things without saying so.
    """
    root, _admin, _member = console
    _event(_uid("mo"), hours_ago=1, model="Qwen/Other-3B", output_tokens=90)

    everything = root.get(
        "/admin/api/analytics/overview", params={"range": "24h"}
    ).json()
    assert everything["totals"]["requests"] == 6
    assert set(everything["available_models"]) == {"Qwen/Test-7B", "Qwen/Other-3B"}
    assert everything["model"] == ""

    narrowed = root.get(
        "/admin/api/analytics/overview",
        params={"range": "24h", "model": "Qwen/Other-3B"},
    ).json()
    assert narrowed["totals"]["requests"] == 1
    assert narrowed["totals"]["output_tokens"] == 90
    assert narrowed["model"] == "Qwen/Other-3B"
    # History is untouched by a model filter, and says so by staying equal.
    assert narrowed["chat"]["messages"] == everything["chat"]["messages"]


def test_an_unknown_model_filters_to_nothing_rather_than_everything(console):
    """A filter that silently matched everything would be worse than an empty
    page: the reader would believe a number that answers a different question."""
    root, _admin, _member = console
    data = root.get(
        "/admin/api/analytics/overview",
        params={"range": "24h", "model": "not-a-model"},
    ).json()
    assert data["totals"]["requests"] == 0
    assert data["totals"]["output_tokens"] is None


def test_routes_are_reported_separately(console):
    root, _admin, _member = console
    data = root.get("/admin/api/analytics/overview", params={"range": "24h"}).json()
    routes = {r["route"]: r["requests"] for r in data["routes"]}
    # Three chat turns plus the failed one, which kept its route: a failure is
    # still a request the chat engine was asked to serve.
    assert routes["chat"] == 4
    assert routes["deep_research"] == 1


def test_a_cancelled_turn_is_not_counted_as_a_failure(console):
    """Someone pressing Stop is the platform working. Counting it as an error
    would make the reliability figure report normal use as a fault."""
    root, _admin, _member = console
    _event(_uid("mo"), hours_ago=1, status="cancelled")

    data = root.get("/admin/api/analytics/overview", params={"range": "24h"}).json()
    assert data["totals"]["cancelled"] == 1
    assert data["totals"]["errors"] == 1  # the seeded failure, and only it
    assert sum(r["errors"] for r in data["routes"]) == 1

    perf = root.get("/admin/api/analytics/performance", params={"range": "24h"}).json()
    assert sum(p["errors"] for p in perf["series"]) == 1


def test_performance_reports_percentiles_and_a_histogram(console):
    root, _admin, _member = console
    data = root.get("/admin/api/analytics/performance", params={"range": "24h"}).json()
    assert data["totals"]["p50_ttft_ms"] == 900
    assert data["totals"]["p95_ttft_ms"] == 900
    buckets = {b["label"]: b["count"] for b in data["ttft_histogram"]}
    # Four events measured 900ms; the errored one recorded no TTFT at all and
    # must not be counted as instant.
    assert buckets["<1s"] == 4
    assert sum(buckets.values()) == 4


def test_voice_reports_the_attempts_the_wait_and_the_languages(console):
    """Dictation as METADATA, and the two places that shape is easy to get
    wrong.

    A clip whose length the browser never reported is not a clip of zero
    seconds, so it must not be summed as one; and the language share is taken
    against the clips that were IDENTIFIED, because dividing by every attempt
    would report the parser's silence as a language nobody spoke.
    """
    root, _admin, _member = console
    speaker = _uid("mo")
    for language, duration in (("English", 4200), ("Hindi", 3100), (None, None)):
        db.record_voice_transcription(
            user_id=speaker,
            duration_ms=duration,
            language=language,
            processing_ms=910,
            status="ok",
        )
    db.record_voice_transcription(
        user_id=speaker,
        duration_ms=2000,
        language=None,
        processing_ms=8000,
        status="unavailable",
    )

    body = root.get("/admin/api/analytics/voice", params={"range": "7d"}).json()
    totals = body["totals"]
    assert totals["transcriptions"] == 4
    assert totals["ok"] == 3
    assert totals["failed"] == 1
    assert totals["unavailable"] == 1
    assert totals["users"] == 1
    # The clip nobody timed is absent from the sum, not counted as silence.
    assert totals["total_duration_ms"] == 4200 + 3100 + 2000
    assert totals["language_identified"] == 2

    # The failure's eight-second wait is in the average: it is a wait somebody
    # sat through, and leaving it out would report latency for the successes
    # only — the one population nobody complains about.
    assert totals["avg_processing_ms"] > 910

    shares = {row["language"]: row["share"] for row in body["languages"]}
    assert shares == {"English": 50.0, "Hindi": 50.0}


def test_a_deployment_where_nobody_has_dictated_says_so_rather_than_nothing(console):
    """`coverage` is LIFETIME on purpose: it is the whole difference between
    "nobody dictated last week" and "voice has never been used here", and the
    page says something different for each."""
    root, _admin, _member = console

    body = root.get("/admin/api/analytics/voice", params={"range": "7d"}).json()
    assert body["coverage"]["transcriptions"] == 0
    assert body["coverage"]["first_transcription"] is None
    assert body["totals"]["success_rate"] is None


# ---------------------------------------------------------------------------
# 3. Infrastructure degrades honestly
# ---------------------------------------------------------------------------


def test_infrastructure_says_unavailable_rather_than_reporting_zeros(console, monkeypatch):
    """A GPU reported at 0% because nobody asked is indistinguishable from an
    idle GPU. The console must never draw that."""
    root, _admin, _member = console
    from app.analytics import infra

    async def refuse(_expr):
        raise infra.Unavailable("connection refused")

    monkeypatch.setattr(infra, "_query", refuse)
    data = root.get("/admin/api/analytics/infrastructure").json()
    assert data["nodes"]["available"] is False
    assert "refused" in data["nodes"]["reason"]
    assert "nodes" not in data["nodes"]


def test_infrastructure_reports_nodes_when_prometheus_answers(console, monkeypatch):
    root, _admin, _member = console
    from app.analytics import infra

    sample = {
        "dgx_gpu_up": [
            {"metric": {"node": "spark-1", "role": "head"}, "value": [0, "1"]},
        ],
        "dgx_gpu_utilization_percent": [
            {"metric": {"node": "spark-1"}, "value": [0, "42.5"]},
        ],
    }

    async def answer(expr):
        return sample.get(expr.strip(), [])

    monkeypatch.setattr(infra, "_query", answer)
    data = root.get("/admin/api/analytics/infrastructure").json()
    assert data["nodes"]["available"] is True
    node = next(n for n in data["nodes"]["nodes"] if n["node"] == "spark-1")
    assert node["gpu_utilization"] == 42.5
    assert node["role"] == "head"
    # Nothing answered for CPU, so it is absent — not 0.
    assert node["cpu_percent"] is None


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


def test_a_generation_is_only_counted_once(console):
    """Two tabs attached to one detached answer both reach the emitter."""
    root, _admin, _member = console
    from app import usage

    for _ in range(2):
        usage.record(
            user_id=_uid("mo"),
            workspace_id=_workspace(),
            conversation_id="conv-dup",
            generation_id="gen-1",
            route="chat",
            model="Qwen/Test-7B",
        )
    data = root.get("/admin/api/analytics/overview", params={"range": "24h"}).json()
    assert data["totals"]["requests"] == 6  # 5 seeded + exactly one of these


def test_the_writer_never_raises(monkeypatch):
    """Telemetry failing must cost a row in a report, never an answer."""
    from app import usage

    def explode(*_args, **_kwargs):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(db, "connection", explode)
    usage.record(
        user_id=1,
        workspace_id="w",
        conversation_id="c",
        generation_id="g",
        route="chat",
    )  # no exception


def test_unmeasured_counts_are_stored_as_null(console):
    """The writer must not turn "not measured" into 0 on the way in."""
    root, _admin, _member = console
    _event(_uid("mo"), hours_ago=4, input_tokens=None, output_tokens=None, ttft_ms=None)
    with db.connection() as con:
        row = con.execute(
            "SELECT input_tokens, output_tokens, ttft_ms FROM usage_events "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["ttft_ms"] is None


# ---------------------------------------------------------------------------
# End to end: a real chat turn writes its own telemetry
# ---------------------------------------------------------------------------


def test_a_chat_turn_records_a_usage_event(login_client, monkeypatch):
    """The wiring, not the writer.

    `usage.record` has its own tests above; this one proves the generation
    lifecycle actually calls it — with the route the engine reported, the model
    that served it, and the token counts the runtime returned. That path runs
    in the generation's `finally`, which is easy to break without any test
    noticing until a month of analytics comes back empty.
    """
    from app import llm

    client = login_client("zed", role="super_admin")

    async def fake_stream(messages, **kwargs):
        # The runtime reporting usage, exactly as the stream_options chunk does.
        llm._record_usage(1200, 340)
        yield "token", "hello"

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    resp = client.post(
        "/chat",
        json={
            "message": "hello there",
            "mode": "assistant",
            "effort": "fast",
            "conversation_id": "conv-usage-e2e",
        },
    )
    assert resp.status_code == 200

    with db.connection() as con:
        row = con.execute(
            "SELECT * FROM usage_events WHERE conversation_id = %s",
            ("conv-usage-e2e",),
        ).fetchone()
    assert row is not None, "the turn produced no usage event"
    assert row["status"] == "ok"
    assert row["route"]
    assert row["effort"] == "fast"
    assert row["input_tokens"] == 1200
    assert row["output_tokens"] == 340
    assert row["duration_ms"] is not None
    assert row["user_id"] == _uid("zed")
    # METADATA ONLY. No column may carry the question or the answer — the
    # console reports who/how much/how fast, and reading content is a separate
    # capability with its own audit trail.
    text_columns = [
        v for k, v in dict(row).items()
        if isinstance(v, str) and k not in ("conversation_id", "generation_id")
    ]
    assert not any("hello" in v.lower() for v in text_columns)
