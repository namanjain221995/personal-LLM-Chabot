"""The quota engine and the idempotency layer — CONTRACT-3 §12 and §13.

Both are per-request enforcement over the V34 ledgers, so they share a file
and a fixture: a real project, a real key, a real `ApiCaller`, and a real
PostgreSQL. A rate limiter tested against a dictionary is a rate limiter that
has never met a second process.

THE CLOCK IS DRIVEN, NEVER SLEPT THROUGH. Every time-sensitive function takes
`now=`, and every test below passes one. A suite that sleeps sixty seconds to
prove a window reopens is a suite nobody runs, which makes it a limit nobody
tests — so the sliding window's decay, its `Retry-After`, and the daily
ledger's midnight boundary are all asserted at exact instants.

WHAT EACH GROUP PINS:

1. THE WINDOW IS DURABLE AND SLIDING. The 61st request in a minute is refused
   and the same request is admitted a minute later; the counters live in
   PostgreSQL, so a restart changes nothing.

2. A REFUSAL RESERVES NOTHING. draft-ietf-httpapi-ratelimit-headers-11's
   Security Considerations: if a refused request consumed the quota it was
   refused for, a stranger could probe an endpoint and infer a tenant's
   traffic.

3. THE SLOT ALWAYS COMES BACK. A concurrency slot released only on the happy
   path ratchets a project's limit down to zero over a day of ordinary errors,
   and the only fix is a restart.

0. THE LIMITS ARE OFF BY DEFAULT (owner decision, 2026-09-13). The public API
   has no request, token, daily or concurrency limit unless
   PUBLIC_API_ENFORCE_LIMITS is true. Every enforcement test in this file runs
   WITH THE SWITCH ON (the autouse fixture below) so that code stays tested;
   section 7 runs with it off and proves nothing is refused, no header
   advertises a limit, and the ledgers are still written exactly once.

4. THE WORK RUNS ONCE. Two identical idempotent requests invoke the model
   once; two different bodies under one key are a 409, not a replay of
   somebody else's answer.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app import db
from app.apiplatform import idempotency, keys, projects, quotas, resolver
from app.config import settings
from app.publicapi import errors

WORKSPACE = "ws-quotas"

#: 32 characters, `keys.MIN_PEPPER_CHARS` exactly.
TEST_PEPPER = "quota-pepper-0123456789abcdefghi"

#: A fixed instant at :30 past the minute. HALF WAY IN ON PURPOSE: at :00 the
#: previous minute still counts in full and at :59 it has all but decayed, so
#: :30 is the only place where the weighting is visibly doing something and an
#: arithmetic mistake cannot hide behind a boundary.
NOON = datetime(2026, 9, 13, 12, 0, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _engine_state(monkeypatch):
    """A configured pepper, no jitter, and no leftover concurrency slots.

    Jitter is switched OFF here and only ON in the one test that asserts it:
    `Retry-After` is a number these tests pin to the second, and a random
    addition would make every one of them a coin toss.
    """
    monkeypatch.setattr(settings, "api_key_pepper", TEST_PEPPER)
    monkeypatch.setattr(settings, "public_api_ratelimit_jitter_seconds", 0.0)
    # ENFORCED for every test in this file unless it asks for `unlimited`
    # (2026-09-13): the default is off, and the enforcement code must keep
    # its tests for the operator who turns it back on.
    monkeypatch.setattr(settings, "public_api_enforce_limits", True)
    keys.reset_pepper_cache()
    quotas.reset_concurrency()
    yield
    quotas.reset_concurrency()
    keys.reset_pepper_cache()


@pytest.fixture()
def caller():
    """A real `ApiCaller`, resolved from a real token against a real row."""
    with db.connection() as con:
        con.execute(
            "INSERT INTO workspaces (id, name) VALUES (%s, %s)", (WORKSPACE, "Quotas")
        )
    project = projects.create_project(WORKSPACE, "Metered")
    created = projects.create_key(project["id"], WORKSPACE, "server")
    return resolver.resolve_api_caller(f"Bearer {created.token}")


def a_response(project_id, workspace_id, response_id):
    """A real `api_responses` row.

    `api_idempotency.response_id` is a foreign key into `api_responses`, so a
    test that attaches an invented id is testing nothing — the insert would
    fail in production too. Creating the row is also what the router does: the
    response is durable BEFORE the expensive work, which is what lets a
    background request survive the client that asked for it (CONTRACT-3 §14).
    """
    return db.create_api_response(
        project_id, workspace_id, "techsara-35b", "req_test", response_id=response_id
    )["id"]


def ledger(caller, *, at):
    """The raw rows for this caller's key minute bucket and its project's day.

    Read with SQL here rather than through `db.usage_window` so these tests
    measure the ledger itself, not another accessor's reading of it (that
    accessor was mid-rewrite by its owner on 2026-09-13 and raised a
    GroupingError, which is exactly the kind of neighbour failure a quota
    test must not inherit).
    """
    bucket = at.replace(second=0, microsecond=0)
    with db.connection() as con:
        minute = con.execute(
            "SELECT requests, input_tokens, output_tokens FROM api_usage_minute "
            "WHERE project_id = %s AND key_id = %s AND bucket = %s",
            (caller.project_id, caller.key_id, bucket),
        ).fetchone()
        daily = con.execute(
            "SELECT requests, input_tokens, output_tokens, errors, rate_limited "
            "FROM api_usage_daily WHERE project_id = %s AND day = %s",
            (caller.project_id, at.date()),
        ).fetchone()
    zero_minute = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
    zero_daily = dict(zero_minute, errors=0, rate_limited=0)
    return {
        "minute": {k: int(v) for k, v in minute.items()} if minute else zero_minute,
        "daily": {k: int(v) for k, v in daily.items()} if daily else zero_daily,
    }


#: The output reservation the request-counting tests ask for. Since
#: 2026-09-13 (wave-3 re-verify) every generation reserves its
#: max_output_tokens at admission, and sixty unsettled requests at the 8,192
#: default would trip output_tpm long before the rpm limit these tests are
#: about. One token keeps the output window out of their way; the tests that
#: are ABOUT the output reservation pass their own number.
TINY_OUTPUT = 1


def spend_requests(caller, count, *, now):
    """`count` admitted requests at one instant, the honest way — through the
    gate, so the reservation each one writes is the thing under test rather
    than a counter the test poked in by hand."""
    for _ in range(count):
        quotas.check_and_reserve(caller, now=now, max_output_tokens=TINY_OUTPUT)


# ---------------------------------------------------------------------------
# 1. The window is durable and sliding
# ---------------------------------------------------------------------------


def test_the_sliding_window_refuses_the_sixty_first_request_and_admits_it_a_minute_later(
    caller,
):
    """CONTRACT-3 §12's headline limit: 60 requests a minute, durable.

    The clock is driven, not slept through. Sixty requests land at 12:00:30
    and the 61st is refused; at 12:01:30 that same minute has decayed to half
    its weight — thirty requests' worth — so there is room again.

    This is also why the window SLIDES. Under a fixed per-minute counter the
    caller could spend sixty in the last second of one minute and sixty in the
    first second of the next: a hundred and twenty requests in two seconds, at
    twice the advertised rate, forever, just by aiming at the boundary.
    """
    assert caller.limits.rpm == 60
    spend_requests(caller, 60, now=NOON)

    with pytest.raises(errors.ApiError) as caught:
        quotas.check_and_reserve(caller, now=NOON)
    refusal = caught.value

    assert refusal.status == 429
    assert refusal.code == "rate_limit_error"
    assert refusal.type == "rate_limit_error"
    # `Retry-After` is integer seconds with a floor of 1 (RFC 9110, and the
    # OpenAI-compatible schema STANDARDS.md copies), and it points past the
    # rollover rather than at it: at 12:01:00 the spent minute still counts in
    # FULL, so "wait until this minute ends" would land in a second 429.
    assert refusal.headers() == {"Retry-After": "31"}
    advertised = NOON + timedelta(seconds=int(refusal.headers()["Retry-After"]))
    quotas.check_and_reserve(caller, now=advertised, max_output_tokens=TINY_OUTPUT)

    # And a whole minute later there is room for plenty more.
    later = NOON + timedelta(seconds=60)
    quotas.check_and_reserve(caller, now=later, max_output_tokens=TINY_OUTPUT)
    window = quotas.read_window(caller, now=later)
    assert window.requests == pytest.approx(31.5, abs=0.6)


def test_the_window_weights_the_previous_minute_by_how_far_into_this_one_we_are(caller):
    """The arithmetic itself, at three points, because the conclusion above
    would also hold for several wrong formulas."""
    spend_requests(caller, 10, now=NOON)
    minute_start = NOON.replace(second=0, microsecond=0)

    at_next_start = minute_start + timedelta(seconds=60)
    at_next_half = minute_start + timedelta(seconds=90)
    at_next_end = minute_start + timedelta(seconds=119)

    assert quotas.read_window(caller, now=at_next_start).requests == pytest.approx(10.0)
    assert quotas.read_window(caller, now=at_next_half).requests == pytest.approx(5.0)
    assert quotas.read_window(caller, now=at_next_end).requests == pytest.approx(
        10 * (1 / 60), abs=0.01
    )
    # Two minutes on, the bucket is outside the window entirely.
    assert quotas.read_window(
        caller, now=minute_start + timedelta(seconds=180)
    ).requests == 0


def test_the_daily_quota_survives_a_process_restart_because_a_fresh_interpreter_enforces_it(
    caller,
):
    """CONTRACT-3 §12: "daily tokens — durable, survives restart".

    A daily quota held in a module-level dictionary resets when the container
    does, which on this platform is every deploy and every GDN fault recovery.

    A NEW PYTHON PROCESS, not `importlib.reload` (2026-09-13, wave-2 review:
    the reload version could never fail, because the module keeps no daily
    state to throw away, and it left stale `_in_flight` references behind in
    every module that had imported the old one). The spend is written here;
    a separate interpreter with nothing in memory reads the window and runs
    the gate, and must refuse.
    """
    projects.update_project(caller.project_id, caller.workspace_id, daily_token_quota=1_000)
    metered = _reresolve(caller)

    quotas.record_usage(metered, 600, 350, "completed", now=NOON)

    script = (
        "import sys, json\n"
        "from datetime import datetime\n"
        "from app.apiplatform import quotas, resolver\n"
        "from app import db\n"
        "from app.publicapi import errors\n"
        "args = json.loads(sys.argv[1])\n"
        "noon = datetime.fromisoformat(args['noon'])\n"
        "project = db.get_api_project(args['project'], args['workspace'])\n"
        "caller = resolver.ApiCaller(workspace_id=args['workspace'], project_id=args['project'],\n"
        "    service_account_id=None, key_id=args['key'], scopes=frozenset(), models=(),\n"
        "    limits=resolver._effective_limits({}, project), environment='live')\n"
        "print('daily', quotas.read_window(caller, now=noon).daily_tokens)\n"
        "try:\n"
        "    quotas.reserve(caller, kind='sync', estimated_input_tokens=100, now=noon)\n"
        "    print('admitted')\n"
        "except errors.ApiError as exc:\n"
        "    print('refused', exc.code)\n"
    )
    import json

    # The fresh interpreter reads the switch from the environment, not from
    # this process's monkeypatch; the test is about enforcement surviving a
    # restart, so it runs the child with enforcement on.
    env = dict(
        os.environ,
        APP_DATABASE_URL=settings.app_database_url,
        PUBLIC_API_ENFORCE_LIMITS="true",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            json.dumps(
                {
                    "noon": NOON.isoformat(),
                    "project": metered.project_id,
                    "workspace": metered.workspace_id,
                    "key": metered.key_id,
                }
            ),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    lines = result.stdout.strip().splitlines()
    assert "daily 950" in lines
    assert "refused quota_exceeded" in lines

    with pytest.raises(errors.ApiError) as caught:
        quotas.check_and_reserve(metered, 100, now=NOON)
    assert caught.value.code == "quota_exceeded"
    # Until midnight UTC, which is when `api_usage_daily` starts a new row.
    assert caught.value.headers() == {"Retry-After": str(12 * 3600 - 30)}


def test_the_daily_quota_counts_what_was_spent_not_what_was_requested(caller):
    """`quota_exceeded` is a different code from `rate_limit_error` even
    though both are 429 (CONTRACT-3 §9): one means slow down, the other means
    you are done for the day, and a client that cannot tell them apart retries
    every `Retry-After` for hours."""
    projects.update_project(caller.project_id, caller.workspace_id, daily_token_quota=1_000)
    metered = _reresolve(caller)

    quotas.record_usage(metered, 400, 400, "completed", now=NOON)
    # 800 spent + 100 input + 50 reserved output <= 1000
    quotas.check_and_reserve(metered, 100, now=NOON, max_output_tokens=50)

    with pytest.raises(errors.ApiError) as caught:
        quotas.check_and_reserve(metered, 300, now=NOON, max_output_tokens=50)
    assert caught.value.code == "quota_exceeded"
    assert caught.value.status == 429


def test_the_token_per_minute_windows_refuse_before_the_request_is_admitted(caller):
    """Input tokens are known before generation and are checked against what
    the request is ABOUT to spend; output tokens are not knowable yet, so that
    limit binds the request after the one that crossed the line — the only
    honest thing a pre-flight check can do about a number that does not exist."""
    projects.update_project(
        caller.project_id, caller.workspace_id, input_tpm=1_000, output_tpm=500
    )
    fresh = _reresolve(caller)

    quotas.record_usage(fresh, 900, 0, "completed", now=NOON)
    with pytest.raises(errors.ApiError) as caught:
        quotas.check_and_reserve(fresh, 200, now=NOON)
    assert caught.value.code == "rate_limit_error"

    # A small request still fits under the input window.
    quotas.check_and_reserve(fresh, 50, now=NOON)

    # Output: already over, so the next request is refused whatever it asks for.
    quotas.record_usage(fresh, 0, 600, "completed", now=NOON)
    with pytest.raises(errors.ApiError) as caught:
        quotas.check_and_reserve(fresh, 0, now=NOON)
    assert caught.value.code == "rate_limit_error"


def _reresolve(caller):
    """The same identity, re-read after the project's limits changed.

    `ApiCaller` is frozen and its limits were resolved once, at the top of the
    request, which is exactly right for a request — but a test that changes a
    project's ceiling has to take a new caller to see it, and doing that
    through the real resolver keeps the limits arriving the way production's do.
    """
    project = db.get_api_project(caller.project_id, caller.workspace_id)
    return resolver.ApiCaller(
        workspace_id=caller.workspace_id,
        project_id=caller.project_id,
        service_account_id=caller.service_account_id,
        key_id=caller.key_id,
        scopes=caller.scopes,
        models=caller.models,
        limits=resolver._effective_limits({}, project),
        environment=caller.environment,
        public_id=caller.public_id,
    )


# ---------------------------------------------------------------------------
# 2. A refusal reserves nothing
# ---------------------------------------------------------------------------


def test_a_refused_request_does_not_spend_the_quota_it_was_refused_for(caller):
    """The rate-limiting twin of the 403-vs-404 existence oracle
    (draft-ietf-httpapi-ratelimit-headers-11, Security Considerations): if
    error responses consumed quota, a malicious client could probe an endpoint
    and infer another tenant's traffic from how fast its own allowance moved."""
    spend_requests(caller, 60, now=NOON)
    before = quotas.read_window(caller, now=NOON)

    for _ in range(5):
        with pytest.raises(errors.ApiError):
            quotas.check_and_reserve(caller, now=NOON)

    after = quotas.read_window(caller, now=NOON)
    assert after.requests == before.requests == 60
    assert after.current_minute["requests"] == 60

    # The refusals ARE counted, in the column that exists to count them.
    usage = ledger(caller, at=NOON)
    assert usage["daily"]["rate_limited"] == 5


def test_an_admitted_request_is_counted_once_and_record_usage_does_not_count_it_again(
    caller,
):
    """`reserve` counts the request when it admits it; settling writes only
    tokens. Counting in both would double the denominator of every rate limit
    and halve the effective allowance. The 100 estimated input tokens
    reserved at admission are replaced by the measured 100, not added to."""
    reservation = quotas.check_and_reserve(caller, 100, now=NOON)
    quotas.record_usage(caller, 100, 250, "completed", now=NOON, reservation=reservation)

    usage = ledger(caller, at=NOON)
    assert usage["minute"] == {"requests": 1, "input_tokens": 100, "output_tokens": 250}
    assert usage["daily"]["requests"] == 1
    assert usage["daily"]["input_tokens"] == 100
    assert usage["daily"]["output_tokens"] == 250
    assert usage["daily"]["errors"] == 0


def test_unmeasured_usage_is_recorded_as_nothing_rather_than_invented(caller):
    """`llm.get_usage()` returns None for "not measured". CONTRACT-3 §9 says
    the WIRE must then carry `usage: null` — never a zero, which would be a
    lie and an under-charge. The LEDGER cannot add a number it was not given,
    so it adds nothing. Both statements are honest; conflating them would
    either invent usage on the wire or drop it from the ledger."""
    quotas.record_usage(caller, None, None, "failed", now=NOON)

    usage = ledger(caller, at=NOON)
    assert usage["minute"]["input_tokens"] == 0
    assert usage["daily"]["errors"] == 1


def test_an_unmeasured_request_keeps_its_reserved_estimate_rather_than_refunding_it(caller):
    """With a reservation the ledger DOES have a number: the estimate it
    added at admission. Refunding it because the engine did not report counts
    would hand the caller free tokens every time the engine is silent."""
    reservation = quotas.reserve(caller, kind="sync", estimated_input_tokens=120, now=NOON)
    reservation.settle(None, None, "failed", now=NOON)

    usage = ledger(caller, at=NOON)
    assert usage["minute"]["input_tokens"] == 120
    assert usage["daily"]["input_tokens"] == 120


@pytest.mark.parametrize(
    "status,errors_expected",
    [("completed", 0), ("cancelled", 0), ("failed", 1), ("timeout", 1), ("", 1)],
)
def test_the_error_counter_moves_for_everything_that_is_not_a_clean_finish(
    caller, status, errors_expected
):
    quotas.record_usage(caller, 10, 10, status, now=NOON)
    usage = ledger(caller, at=NOON)
    assert usage["daily"]["errors"] == errors_expected


# ---------------------------------------------------------------------------
# 3. The slot always comes back
# ---------------------------------------------------------------------------


def test_the_concurrency_slot_is_released_when_the_body_raises(caller):
    """The entire reason this is a context manager rather than an
    acquire/release pair. An exception mid-stream, a client disconnect, a
    cancelled task: each must give the slot back, or the project's concurrency
    limit ratchets down to zero over a day of ordinary errors and the only fix
    is a restart."""
    assert quotas.in_flight(caller) == 0

    with pytest.raises(ZeroDivisionError):
        with quotas.concurrency_slot(caller):
            assert quotas.in_flight(caller) == 1
            1 / 0

    assert quotas.in_flight(caller) == 0
    # And the slot is genuinely reusable, not merely uncounted.
    with quotas.concurrency_slot(caller):
        assert quotas.in_flight(caller) == 1
    assert quotas.in_flight(caller) == 0


def test_the_fifth_concurrent_request_is_refused_and_the_slot_returns_on_exit(caller):
    """CONTRACT-3 §12: four concurrent requests by default. Non-blocking — over
    the limit is an immediate 429 with a `Retry-After`, never a queue, because
    a queue puts the wait INSIDE the request the caller is timing and they
    cannot tell a slow model from a slow queue."""
    assert caller.limits.max_concurrency == 4
    held = []
    try:
        for _ in range(4):
            slot = quotas.concurrency_slot(caller)
            slot.__enter__()
            held.append(slot)

        with pytest.raises(errors.ApiError) as caught:
            with quotas.concurrency_slot(caller):
                pytest.fail("a fifth slot must not be granted")
        assert caught.value.code == "concurrency_limit_exceeded"
        assert caught.value.status == 429
        assert caught.value.headers() == {"Retry-After": "1"}
    finally:
        for slot in held:
            slot.__exit__(None, None, None)

    assert quotas.in_flight(caller) == 0
    with quotas.concurrency_slot(caller):
        pass


def test_the_slot_counter_is_correct_under_threads(caller):
    """The counter is shared mutable process state, so it is guarded by a lock
    — and a lost increment there is a concurrency limit that does not limit."""
    started = threading.Barrier(4)
    errors_seen = []

    def run():
        try:
            started.wait(timeout=5)
            with quotas.concurrency_slot(caller):
                pass
        except Exception as exc:  # noqa: BLE001
            errors_seen.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors_seen == []
    assert quotas.in_flight(caller) == 0


# ---------------------------------------------------------------------------
# The advertised headers
# ---------------------------------------------------------------------------


def test_the_rate_limit_headers_are_the_two_fields_of_draft_eleven(caller):
    """draft-ietf-httpapi-ratelimit-headers-11 defines exactly TWO fields with
    Structured-Fields parameters. The three-header form most of the web still
    emits — `RateLimit-Limit` / `RateLimit-Remaining` / `RateLimit-Reset` —
    was REMOVED from the draft, and shipping it gives a header set no
    conformant client parses."""
    spend_requests(caller, 10, now=NOON)
    headers = quotas.limit_headers(caller, at=NOON)

    assert set(headers) == {"RateLimit", "RateLimit-Policy"}
    assert "RateLimit-Limit" not in headers
    assert "RateLimit-Remaining" not in headers
    assert "RateLimit-Reset" not in headers
    assert "X-RateLimit-Limit" not in headers

    # Policy names are quoted Strings; `qu` is a quoted String; q/w/r/t are
    # Integers. A quoting mistake does not error anywhere — the draft tells
    # clients to IGNORE a malformed field, so the headers would silently
    # vanish instead.
    assert headers["RateLimit-Policy"] == (
        '"requests";q=60;w=60, "concurrency";q=4;qu="concurrent-requests"'
    )
    assert headers["RateLimit"] == '"requests";r=50;t=30'


def test_the_reservation_advertises_the_remaining_count_after_its_own_request(caller):
    """The window was read BEFORE the reservation was written, so recomputing
    from it would advertise one more request than the caller actually has —
    and a client that trusts the header spends it into a 429."""
    spend_requests(caller, 10, now=NOON)
    reservation = quotas.check_and_reserve(caller, now=NOON, max_output_tokens=TINY_OUTPUT)

    assert reservation.requests_remaining == 49
    assert reservation.headers()["RateLimit"] == '"requests";r=49;t=30'


def test_retry_after_is_never_earlier_than_the_window_it_is_paired_with(caller, monkeypatch):
    """The draft makes "Retry-After earlier than the end of the effective
    window" a SHOULD NOT, and an inconsistent pair guarantees a second 429.
    Jitter is switched ON here — it is the draft's own anti-stampede
    requirement, and it must not be able to break the ordering."""
    monkeypatch.setattr(settings, "public_api_ratelimit_jitter_seconds", 5.0)
    spend_requests(caller, 60, now=NOON)

    for _ in range(20):
        with pytest.raises(errors.ApiError) as caught:
            quotas.check_and_reserve(caller, now=NOON)
        retry_after = int(caught.value.headers()["Retry-After"])
        # 31 is the unjittered answer; jitter only ever pushes it later, and
        # never past the bound the setting names.
        assert 31 <= retry_after <= 36


# ---------------------------------------------------------------------------
# 4. The work runs once (CONTRACT-3 §13)
# ---------------------------------------------------------------------------


def test_two_identical_idempotent_requests_invoke_the_work_once(caller):
    """The whole point. A client that times out at thirty seconds and retries
    must not be charged twice and must not get two different answers."""
    calls = []
    body = {"model": "techsara-35b", "input": "Explain RAG.", "temperature": 0.2}

    def work():
        calls.append("ran")
        response_id = a_response(caller.project_id, caller.workspace_id, None)
        return (response_id, {"id": response_id, "text": "an answer"})

    def replay(response_id):
        return {"id": response_id, "text": "an answer", "replayed": True}

    first = idempotency.execute_once(
        project_id=caller.project_id,
        endpoint="v1_responses",
        idem_key="client-chosen-key-1",
        body=body,
        work=work,
        replay=replay,
    )
    second = idempotency.execute_once(
        project_id=caller.project_id,
        endpoint="v1_responses",
        idem_key="client-chosen-key-1",
        body=dict(reversed(list(body.items()))),  # same request, re-serialised
        work=work,
        replay=replay,
    )

    assert calls == ["ran"]
    assert first["text"] == "an answer"
    assert second["replayed"] is True
    assert second["id"] == first["id"]
    # And the claim in the database points at the one response that exists.
    claim = db.get_idempotency(caller.project_id, "v1_responses", "client-chosen-key-1")
    assert claim["state"] == "completed"
    assert claim["response_id"] == first["id"]
    assert len(db.list_api_responses(caller.project_id)) == 1


def test_the_same_key_with_a_different_body_is_a_conflict_not_a_replay(caller):
    """Returning one request's response to a different request is worse than
    refusing both."""
    idempotency.execute_once(
        project_id=caller.project_id,
        endpoint="v1_responses",
        idem_key="reused",
        body={"input": "one"},
        work=lambda: (a_response(caller.project_id, caller.workspace_id, None), "A"),
        replay=lambda rid: "replayed",
    )

    with pytest.raises(errors.ApiError) as caught:
        idempotency.execute_once(
            project_id=caller.project_id,
            endpoint="v1_responses",
            idem_key="reused",
            body={"input": "two"},
            work=lambda: pytest.fail("the work must not run"),
            replay=lambda rid: pytest.fail("a different body must not replay"),
        )

    assert caught.value.code == "idempotency_conflict"
    assert caught.value.status == 409
    assert caught.value.param == "Idempotency-Key"


def test_one_tenants_idempotency_key_never_resolves_to_another_tenants_response(caller):
    """The claim is scoped by `(project_id, endpoint, key)`. Two tenants
    picking the same obvious string — `"1"`, a customer id, today's date — must
    not collide, and the scoping is the database's `WHERE` clause rather than
    a prefix somebody remembered to add."""
    other = projects.create_project(WORKSPACE, "Another project")
    shared_key = "the-same-obvious-string"

    mine = idempotency.execute_once(
        project_id=caller.project_id,
        endpoint="v1_responses",
        idem_key=shared_key,
        body={"input": "mine"},
        work=lambda: (
            a_response(caller.project_id, caller.workspace_id, None),
            "mine",
        ),
        replay=lambda rid: pytest.fail("no replay expected"),
    )
    theirs = idempotency.execute_once(
        project_id=other["id"],
        endpoint="v1_responses",
        idem_key=shared_key,
        body={"input": "theirs"},
        work=lambda: (a_response(other["id"], WORKSPACE, None), "theirs"),
        replay=lambda rid: pytest.fail("no replay expected"),
    )

    assert (mine, theirs) == ("mine", "theirs")


def test_the_same_key_on_a_different_endpoint_is_a_different_claim(caller):
    for endpoint in ("v1_responses", "v1_chat_completions"):
        assert idempotency.execute_once(
            project_id=caller.project_id,
            endpoint=endpoint,
            idem_key="shared",
            body={"input": "hello"},
            work=lambda ep=endpoint: (
                a_response(caller.project_id, caller.workspace_id, None),
                ep,
            ),
            replay=lambda rid: pytest.fail("no replay expected"),
        ) == endpoint


def test_a_request_whose_work_failed_does_not_poison_the_key_for_a_day(caller):
    """A claim left `in_flight` by a crashed request blocks that
    `Idempotency-Key` for the full retention window — twenty-four hours of a
    client being told to attach to an attempt that is not running, over one
    transient failure. So a failure marks the claim completed with NOTHING to
    replay, and `claim()` reads that as "the original produced nothing, run it
    again" — which is not a double invocation, because the first invocation
    produced nothing."""
    attempts = []

    def flaky():
        attempts.append("tried")
        if len(attempts) == 1:
            raise RuntimeError("the engine died mid-answer")
        return (
            a_response(caller.project_id, caller.workspace_id, None),
            "an answer at last",
        )

    with pytest.raises(RuntimeError):
        idempotency.execute_once(
            project_id=caller.project_id,
            endpoint="v1_responses",
            idem_key="retried",
            body={"input": "hello"},
            work=flaky,
            replay=lambda rid: pytest.fail("nothing to replay yet"),
        )

    result = idempotency.execute_once(
        project_id=caller.project_id,
        endpoint="v1_responses",
        idem_key="retried",
        body={"input": "hello"},
        work=flaky,
        replay=lambda rid: pytest.fail("nothing to replay yet"),
    )

    assert attempts == ["tried", "tried"]
    assert result == "an answer at last"


def test_a_claim_still_running_is_reported_as_such_rather_than_replayed(caller):
    """CONTRACT-3 §13 lets a second caller ATTACH to a request that is still
    running. Only the router knows how — joining a stream, or polling a
    background row — so this module reports the fact and does not decide."""
    held = idempotency.claim(
        caller.project_id, "v1_responses", "in-flight", {"input": "hello"}
    )
    assert held.claimed is True

    second = idempotency.claim(
        caller.project_id, "v1_responses", "in-flight", {"input": "hello"}
    )
    assert second.claimed is False
    assert second.in_progress is True
    assert second.replayable is False
    assert second.response_id is None


def test_a_request_without_the_header_simply_runs(caller):
    """The header is optional (CONTRACT-3 §13); an absent one is not an error
    and not a claim. A BLANK one is a different thing — the client asked for
    idempotency and sent nothing usable — and is refused rather than silently
    downgraded."""
    calls = []
    assert idempotency.execute_once(
        project_id=caller.project_id,
        endpoint="v1_responses",
        idem_key=None,
        body={"input": "hello"},
        work=lambda: calls.append(1) or ("resp_x", "ran"),
        replay=lambda rid: pytest.fail("no claim, no replay"),
    ) == "ran"
    assert calls == [1]
    assert db.get_idempotency(caller.project_id, "v1_responses", "") is None

    with pytest.raises(idempotency.IdempotencyKeyError):
        idempotency.normalise_key("   ")


def test_the_fingerprint_is_of_the_request_and_not_of_its_spelling():
    """Two requests that differ only in key order or whitespace ARE the same
    request; every HTTP library re-serialises its payload on a retry, and a
    client must not be accused of changing its mind."""
    assert idempotency.fingerprint({"a": 1, "b": [1, 2]}) == idempotency.fingerprint(
        {"b": [1, 2], "a": 1}
    )
    assert idempotency.fingerprint({"a": 1}) != idempotency.fingerprint({"a": 2})
    assert idempotency.fingerprint({"a": "1"}) != idempotency.fingerprint({"a": 1})
    # 64 lowercase hex: SHA-256, because a collision here would return one
    # caller's response to another caller's request inside the same project.
    digest = idempotency.fingerprint({"input": "hello"})
    assert len(digest) == 64 and digest == digest.lower()
    # And the body itself is never stored — a fingerprint answers "is this the
    # same request" without keeping the request (CONTRACT-3 §16).
    assert "hello" not in digest


@pytest.mark.parametrize(
    "value", ["", "   ", "a" * 256, "has space", "new\nline", "tab\tchar", 12345]
)
def test_an_unusable_idempotency_key_is_refused_without_echoing_it(value):
    """The value ends up in a unique index, a log line and the console, so it
    is validated like any other untrusted text — and the refusal does not
    quote it back (`publicapi/errors.py::_echoable` refuses the same thing,
    for the same log-injection reason)."""
    with pytest.raises(idempotency.IdempotencyKeyError) as caught:
        idempotency.normalise_key(value)
    assert repr(value) not in str(caught.value)


# ---------------------------------------------------------------------------
# 5. The wave-2 review, 2026-09-13: the limit holds under a real burst, is
#    counted per project, and zero means zero
# ---------------------------------------------------------------------------


def _key_in(caller, name, **fields):
    """A second real key in the same project, resolved the real way."""
    created = projects.create_key(caller.project_id, caller.workspace_id, name, **fields)
    return resolver.resolve_api_caller(f"Bearer {created.token}")


def _burst(
    callers, *, now, estimated_input_tokens=0, kind="sync", max_output_tokens=TINY_OUTPUT
):
    """Fire one `reserve` per caller AT THE SAME INSTANT from real threads,
    each on its own pooled PostgreSQL connection. Returns (admitted, refused
    codes, unexpected exceptions)."""
    gate = threading.Barrier(len(callers))
    admitted, refused, unexpected = [], [], []
    lock = threading.Lock()

    def fire(one):
        try:
            gate.wait(timeout=30)
            quotas.reserve(
                one,
                kind=kind,
                estimated_input_tokens=estimated_input_tokens,
                max_output_tokens=max_output_tokens,
                now=now,
            )
            with lock:
                admitted.append(one.key_id)
        except errors.ApiError as exc:
            with lock:
                refused.append(exc.code)
        except Exception as exc:  # noqa: BLE001
            with lock:
                unexpected.append(exc)

    threads = [threading.Thread(target=fire, args=(one,)) for one in callers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    return admitted, refused, unexpected


def test_twelve_simultaneous_requests_against_rpm_one_admit_exactly_one(caller):
    """The publicapi reviewer's reproduction: 12 concurrent requests against
    rpm=1 were ALL admitted, because the window was read, decided on and
    bumped in three separate statements. The advisory lock makes the read and
    the bump one transaction per project."""
    projects.update_project(caller.project_id, caller.workspace_id, rpm=1)
    limited = _reresolve(caller)

    admitted, refused, unexpected = _burst([limited] * 12, now=NOON)

    assert unexpected == []
    assert len(admitted) == 1
    assert refused == ["rate_limit_error"] * 11
    assert ledger(limited, at=NOON)["minute"]["requests"] == 1


def test_forty_simultaneous_requests_at_fifty_nine_of_sixty_admit_exactly_one(caller):
    """The platform-services reviewer's reproduction: 59/60 used, 40 threads,
    40 admitted and a counter of 99. The counter must end at exactly 60."""
    spend_requests(caller, 59, now=NOON)

    admitted, refused, unexpected = _burst([caller] * 40, now=NOON)

    assert unexpected == []
    assert len(admitted) == 1
    assert len(refused) == 39
    assert ledger(caller, at=NOON)["minute"]["requests"] == 60
    assert ledger(caller, at=NOON)["daily"]["rate_limited"] == 39


def test_a_simultaneous_burst_of_large_prompts_cannot_overspend_the_input_token_window(
    caller,
):
    """The estimated input tokens are RESERVED inside the same transaction,
    so twelve 300-token prompts racing at an input_tpm of 1,000 admit exactly
    three — not twelve that each saw an empty window."""
    projects.update_project(caller.project_id, caller.workspace_id, input_tpm=1_000)
    metered = _reresolve(caller)

    admitted, refused, unexpected = _burst(
        [metered] * 12, now=NOON, estimated_input_tokens=300
    )

    assert unexpected == []
    assert len(admitted) == 3
    assert set(refused) == {"rate_limit_error"}
    assert ledger(metered, at=NOON)["minute"]["input_tokens"] == 900


def test_a_simultaneous_burst_cannot_overspend_the_daily_quota(caller):
    projects.update_project(
        caller.project_id, caller.workspace_id, daily_token_quota=1_000
    )
    metered = _reresolve(caller)

    admitted, refused, unexpected = _burst(
        [metered] * 12, now=NOON, estimated_input_tokens=249
    )

    # 249 input + 1 reserved output = 250 each: four fit in 1,000.
    assert unexpected == []
    assert len(admitted) == 4
    assert set(refused) == {"quota_exceeded"}


def test_a_simultaneous_burst_of_long_generations_cannot_overspend_the_output_token_window(
    caller,
):
    """2026-09-13, wave-3 re-verify: output was checked only against what had
    ALREADY been spent, so twelve generations admitted together each saw an
    empty output window and all twelve could produce 300 tokens against an
    output_tpm of 1,000. The requested max_output_tokens is now reserved at
    admission, so exactly three fit."""
    projects.update_project(caller.project_id, caller.workspace_id, output_tpm=1_000)
    metered = _reresolve(caller)

    admitted, refused, unexpected = _burst(
        [metered] * 12, now=NOON, estimated_input_tokens=10, max_output_tokens=300
    )

    assert unexpected == []
    assert len(admitted) == 3
    assert set(refused) == {"rate_limit_error"}
    assert ledger(metered, at=NOON)["minute"]["output_tokens"] == 900


def test_a_simultaneous_burst_of_long_generations_cannot_overspend_the_daily_quota(caller):
    """The daily half of the same hole: input 10 + output 290 reserved per
    request, twelve at once, a daily quota of 1,000 — three admitted, and the
    ledger never shows more than the quota while they are all in flight."""
    projects.update_project(
        caller.project_id, caller.workspace_id, daily_token_quota=1_000
    )
    metered = _reresolve(caller)

    admitted, refused, unexpected = _burst(
        [metered] * 12, now=NOON, estimated_input_tokens=10, max_output_tokens=290
    )

    assert unexpected == []
    assert len(admitted) == 3
    assert set(refused) == {"quota_exceeded"}
    day = ledger(metered, at=NOON)["daily"]
    assert day["input_tokens"] + day["output_tokens"] == 900


def test_the_output_reservation_is_settled_to_the_measured_count_exactly_once(caller):
    """Reserved at admission, swapped for what the engine reported at the end,
    and a second settlement changes nothing. An unmeasured output keeps its
    reservation — the same rule the input estimate follows."""
    measured = quotas.reserve(
        caller, kind="sync", estimated_input_tokens=100, max_output_tokens=2_000, now=NOON
    )
    assert measured.reserved_output_tokens == 2_000
    assert measured.reserved_tokens == 2_100
    assert ledger(caller, at=NOON)["minute"]["output_tokens"] == 2_000

    measured.settle(90, 120, "completed", now=NOON)
    measured.settle(90, 120, "completed", now=NOON)
    usage = ledger(caller, at=NOON)
    assert usage["minute"]["output_tokens"] == 120
    assert usage["daily"]["output_tokens"] == 120

    silent = quotas.reserve(
        caller, kind="sync", estimated_input_tokens=100, max_output_tokens=500, now=NOON
    )
    silent.settle(None, None, "failed", now=NOON)
    assert ledger(caller, at=NOON)["minute"]["output_tokens"] == 620


def test_a_request_that_names_no_output_budget_reserves_the_default_capped_by_its_limits(
    caller, monkeypatch
):
    """The route may not know max_output_tokens yet; the gate then reserves
    the platform default (CONTRACT-3 §12: 8,192) — but never more than the
    output_tpm it is checked against, or a project with output_tpm below the
    default could never admit anything and its Retry-After would be a lie."""
    monkeypatch.setattr(settings, "public_api_default_max_output_tokens", 8_192)
    plain = quotas.reserve(caller, kind="stream", now=NOON)
    assert plain.reserved_output_tokens == 8_192

    projects.update_project(caller.project_id, caller.workspace_id, output_tpm=1_000)
    narrow = _reresolve(caller)
    later = NOON + timedelta(minutes=5)
    capped = quotas.reserve(narrow, kind="stream", now=later)
    assert capped.reserved_output_tokens == 1_000
    with pytest.raises(errors.ApiError) as caught:
        quotas.reserve(narrow, kind="stream", now=later)
    assert caught.value.code == "rate_limit_error"

    # A read spends no tokens and reserves none.
    assert quotas.reserve(caller, kind="read", now=later).reserved_tokens == 0


def test_minting_more_keys_does_not_multiply_the_projects_request_rate(caller):
    """The key-multiplier bypass: ten keys in a rpm=60 project got 600
    requests a minute. The window is the SUM over the project's keys, so ten
    keys racing against rpm=5 get five between them — and so does a
    sequential walk through the same keys."""
    projects.update_project(caller.project_id, caller.workspace_id, rpm=5)
    limited = _reresolve(caller)
    others = [_key_in(limited, f"extra-{n}") for n in range(9)]
    every_key = [limited] + others

    admitted, refused, unexpected = _burst(every_key * 2, now=NOON)

    assert unexpected == []
    assert len(admitted) == 5
    assert len(refused) == 15

    later = NOON + timedelta(minutes=5)
    sequential = 0
    for one in every_key:
        try:
            quotas.reserve(one, kind="sync", now=later)
            sequential += 1
        except errors.ApiError:
            pass
    assert sequential == 5


def test_a_key_may_tighten_its_own_share_without_taking_anything_from_its_siblings(
    caller,
):
    """Project rpm=10; one key parked at rpm=2. That key stops at 2; a sibling
    that inherits still has the project's remaining 8; and a key that asks for
    6,000 gets no more than the project has."""
    projects.update_project(caller.project_id, caller.workspace_id, rpm=10)
    inherits = _reresolve(caller)
    narrow = _key_in(inherits, "narrow", rpm=2)
    greedy = _key_in(inherits, "greedy", rpm=6_000)
    assert narrow.limits.key_rpm == 2
    assert narrow.limits.project_rpm == 10
    assert greedy.limits.key_rpm == 10

    quotas.reserve(narrow, kind="sync", now=NOON, max_output_tokens=TINY_OUTPUT)
    quotas.reserve(narrow, kind="sync", now=NOON, max_output_tokens=TINY_OUTPUT)
    with pytest.raises(errors.ApiError) as caught:
        quotas.reserve(narrow, kind="sync", now=NOON, max_output_tokens=TINY_OUTPUT)
    assert caught.value.code == "rate_limit_error"

    for _ in range(8):
        quotas.reserve(greedy, kind="sync", now=NOON, max_output_tokens=TINY_OUTPUT)
    for one in (inherits, greedy, narrow):
        with pytest.raises(errors.ApiError):
            quotas.reserve(one, kind="sync", now=NOON, max_output_tokens=TINY_OUTPUT)


def test_a_get_route_counts_one_request_against_rpm_and_is_not_blocked_by_tokens(caller):
    """Interface (5): every authenticated /v1 route, GET included, counts
    one request. A caller out of tokens can still read its own usage."""
    projects.update_project(
        caller.project_id, caller.workspace_id, rpm=2, daily_token_quota=0
    )
    limited = _reresolve(caller)

    quotas.reserve(limited, kind="read", now=NOON)
    with pytest.raises(errors.ApiError) as caught:
        quotas.reserve(limited, kind="sync", now=NOON)
    assert caught.value.code == "quota_exceeded"
    quotas.reserve(limited, kind="read", now=NOON)
    with pytest.raises(errors.ApiError) as caught:
        quotas.reserve(limited, kind="read", now=NOON)
    assert caught.value.code == "rate_limit_error"
    assert ledger(limited, at=NOON)["minute"]["requests"] == 2


def test_an_unknown_reservation_kind_is_an_error_rather_than_a_skipped_limit(caller):
    with pytest.raises(ValueError):
        quotas.reserve(caller, kind="get", now=NOON)
    with pytest.raises(ValueError):
        quotas.take_slot(caller, "read")


@pytest.mark.parametrize(
    "column,kind,expected_code",
    [
        ("rpm", "sync", "rate_limit_error"),
        ("rpm", "read", "rate_limit_error"),
        ("input_tpm", "sync", "rate_limit_error"),
        ("output_tpm", "stream", "rate_limit_error"),
        ("daily_token_quota", "background", "quota_exceeded"),
    ],
)
def test_a_project_limit_stored_as_zero_refuses_the_very_first_request(
    caller, column, kind, expected_code
):
    """The schema permits 0 and the console writes it to freeze a project.
    The first cut enforced 0 as 60 / 200,000 / 60,000 / 2,000,000."""
    projects.update_project(caller.project_id, caller.workspace_id, **{column: 0})
    frozen = _reresolve(caller)
    assert getattr(frozen.limits, column) == 0

    with pytest.raises(errors.ApiError) as caught:
        quotas.reserve(frozen, kind=kind, estimated_input_tokens=0, now=NOON)
    assert caught.value.code == expected_code
    assert ledger(frozen, at=NOON)["minute"]["requests"] == 0


def test_a_concurrency_limit_stored_as_zero_grants_no_slot(caller):
    """The first cut floored the concurrency limit at 1."""
    projects.update_project(caller.project_id, caller.workspace_id, max_concurrency=0)
    frozen = _reresolve(caller)
    assert frozen.limits.max_concurrency == 0
    for kind in ("sync", "stream", "background"):
        with pytest.raises(errors.ApiError) as caught:
            with quotas.concurrency_slot(frozen, kind):
                pytest.fail("a zero concurrency limit must grant nothing")
        assert caught.value.code == "concurrency_limit_exceeded"
    assert quotas.in_flight(frozen) == 0


def test_a_key_parked_at_zero_is_refused_while_its_siblings_keep_working(caller):
    parked = _key_in(caller, "parked", rpm=0, max_concurrency=0)
    assert parked.limits.key_rpm == 0
    with pytest.raises(errors.ApiError):
        quotas.reserve(parked, kind="sync", now=NOON)
    with pytest.raises(errors.ApiError):
        quotas.take_slot(parked, "sync")
    quotas.reserve(caller, kind="sync", now=NOON)
    with quotas.concurrency_slot(caller, "sync"):
        pass


def test_sync_stream_and_background_share_one_concurrency_counter_per_project(caller):
    """Default max_concurrency=4. Sync, stream and background from three
    different keys fill the same four slots; the fifth, of any kind and from
    any key, is refused; and a background lease held for a job's lifetime
    keeps its slot until it is released."""
    second = _key_in(caller, "second")
    third = _key_in(caller, "third")
    leases = [
        quotas.take_slot(caller, "sync"),
        quotas.take_slot(second, "stream"),
        quotas.take_slot(third, "background"),
        quotas.take_slot(second, "background"),
    ]
    try:
        assert quotas.in_flight(caller) == 4
        assert quotas.in_flight(caller, "background") == 2
        for one, kind in ((caller, "sync"), (second, "stream"), (third, "background")):
            with pytest.raises(errors.ApiError) as caught:
                quotas.take_slot(one, kind)
            assert caught.value.code == "concurrency_limit_exceeded"
    finally:
        for lease in leases:
            lease.release()
            lease.release()  # a second release from another cleanup path is a no-op
    assert quotas.in_flight(caller) == 0
    with quotas.concurrency_slot(third, "background"):
        assert quotas.in_flight(caller) == 1


def test_a_key_concurrency_limit_caps_that_key_and_leaves_the_project_ceiling_to_the_rest(
    caller,
):
    narrow = _key_in(caller, "narrow", max_concurrency=1)
    assert narrow.limits.key_max_concurrency == 1
    held = quotas.take_slot(narrow, "stream")
    try:
        with pytest.raises(errors.ApiError):
            quotas.take_slot(narrow, "sync")
        others = [quotas.take_slot(caller, "sync") for _ in range(3)]
        with pytest.raises(errors.ApiError):
            quotas.take_slot(caller, "sync")
        for lease in others:
            lease.release()
    finally:
        held.release()
    assert quotas.key_in_flight(narrow) == 0


def test_forty_threads_over_ten_keys_never_hold_more_than_the_projects_slots(caller):
    """The concurrency multiplier, raced: ten keys, forty threads all trying
    to hold a slot at once against max_concurrency=4. At no instant may more
    than four be held."""
    every_key = [caller] + [_key_in(caller, f"k{n}") for n in range(9)]
    gate = threading.Barrier(40)
    lock = threading.Lock()
    peak = {"now": 0, "max": 0, "granted": 0}
    release_all = threading.Event()

    def hold(one):
        gate.wait(timeout=30)
        try:
            lease = quotas.take_slot(one, "stream")
        except errors.ApiError:
            return
        with lock:
            peak["now"] += 1
            peak["granted"] += 1
            peak["max"] = max(peak["max"], peak["now"])
        release_all.wait(timeout=30)
        with lock:
            peak["now"] -= 1
        lease.release()

    threads = [
        threading.Thread(target=hold, args=(every_key[n % 10],)) for n in range(40)
    ]
    for thread in threads:
        thread.start()
    # Give every thread time to try before anyone lets go.
    deadline = datetime.now(timezone.utc) + timedelta(seconds=10)
    while datetime.now(timezone.utc) < deadline and any(
        t.is_alive() for t in threads
    ) and sum(1 for t in threads if t.is_alive()) > 4:
        threading.Event().wait(0.05)
    release_all.set()
    for thread in threads:
        thread.join(timeout=60)

    assert peak["granted"] == 4
    assert peak["max"] == 4
    assert quotas.in_flight(caller) == 0


def test_settling_a_reservation_replaces_the_estimate_with_the_measured_count_once(caller):
    reservation = quotas.reserve(caller, kind="stream", estimated_input_tokens=500, now=NOON)
    assert ledger(caller, at=NOON)["minute"]["input_tokens"] == 500

    reservation.settle(180, 40, "completed", now=NOON + timedelta(seconds=10))
    usage = ledger(caller, at=NOON)
    assert usage["minute"] == {"requests": 1, "input_tokens": 180, "output_tokens": 40}
    assert usage["daily"]["input_tokens"] == 180

    reservation.settle(180, 40, "completed", now=NOON + timedelta(seconds=10))
    assert ledger(caller, at=NOON)["minute"]["input_tokens"] == 180
    assert reservation.settled is True


def test_reading_the_window_is_one_round_trip(caller, monkeypatch):
    """The first cut opened two connections per read and discarded half of
    the second. One statement now answers project, key and day."""
    opened = []
    real = db.connection

    def counting():
        opened.append(1)
        return real()

    monkeypatch.setattr(db, "connection", counting)
    quotas.read_window(caller, now=NOON)
    assert len(opened) == 1


# ---------------------------------------------------------------------------
# 6. Idempotency claims cannot pin a key forever, and cannot collide
# ---------------------------------------------------------------------------


def test_a_claim_past_its_expiry_is_reclaimable_rather_than_pinning_the_key(caller):
    held = idempotency.claim(
        caller.project_id, "v1_responses", "expiring", {"input": "one"}, ttl_hours=1, now=NOON
    )
    assert held.claimed is True

    still = idempotency.claim(
        caller.project_id, "v1_responses", "expiring", {"input": "one"},
        now=NOON + timedelta(minutes=59),
    )
    assert still.claimed is False and still.in_progress is True

    after = idempotency.claim(
        caller.project_id, "v1_responses", "expiring", {"input": "two"},
        now=NOON + timedelta(hours=1, seconds=1),
    )
    assert after.claimed is True


def test_an_in_flight_claim_abandoned_by_a_dead_process_is_reclaimed_after_its_lease(
    caller, monkeypatch
):
    monkeypatch.setattr(settings, "public_api_idempotency_in_flight_lease_seconds", 600.0)
    idempotency.claim(caller.project_id, "v1_responses", "orphan", {"input": "x"}, now=NOON)

    within = idempotency.claim(
        caller.project_id, "v1_responses", "orphan", {"input": "x"},
        now=NOON + timedelta(seconds=599),
    )
    assert within.claimed is False and within.in_progress is True

    # A DIFFERENT body is still a conflict, lease or no lease: the retention
    # window has not passed.
    with pytest.raises(errors.ApiError) as caught:
        idempotency.claim(
            caller.project_id, "v1_responses", "orphan", {"input": "y"},
            now=NOON + timedelta(seconds=601),
        )
    assert caught.value.code == "idempotency_conflict"

    retaken = idempotency.claim(
        caller.project_id, "v1_responses", "orphan", {"input": "x"},
        now=NOON + timedelta(seconds=601),
    )
    assert retaken.claimed is True


def test_two_simultaneous_retries_of_a_failed_request_rerun_the_work_once(caller):
    """The completed-without-a-response case used to be decided in Python
    after a read, so two racing retries both got `claimed=True`."""
    first = idempotency.claim(caller.project_id, "v1_responses", "flaky", {"input": "x"})
    idempotency.finish(first, None)

    gate = threading.Barrier(8)
    outcomes = []
    lock = threading.Lock()

    def retry():
        gate.wait(timeout=30)
        got = idempotency.claim(caller.project_id, "v1_responses", "flaky", {"input": "x"})
        with lock:
            outcomes.append(got.claimed)

    threads = [threading.Thread(target=retry) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sorted(outcomes) == [False] * 7 + [True]


def test_eight_simultaneous_first_claims_of_one_key_have_exactly_one_winner(caller):
    gate = threading.Barrier(8)
    outcomes = []
    lock = threading.Lock()

    def attempt():
        gate.wait(timeout=30)
        got = idempotency.claim(caller.project_id, "v1_responses", "fresh", {"input": "x"})
        with lock:
            outcomes.append(got.claimed)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sorted(outcomes) == [False] * 7 + [True]


def test_a_body_json_cannot_represent_is_refused_rather_than_stringified_into_a_collision():
    """`default=str` made `{"a": Decimal("1.0")}` and `{"a": "1.0"}` the same
    fingerprint."""
    with pytest.raises(TypeError):
        idempotency.fingerprint({"a": Decimal("1.0")})
    with pytest.raises(TypeError):
        idempotency.fingerprint({"at": NOON})
    with pytest.raises(ValueError):
        idempotency.fingerprint({"t": float("nan")})


def test_a_pydantic_body_is_fingerprinted_by_its_own_json_form():
    from pydantic import BaseModel

    class Body(BaseModel):
        input: str
        temperature: float

    assert idempotency.fingerprint(Body(input="hi", temperature=0.2)) == (
        idempotency.fingerprint({"input": "hi", "temperature": 0.2})
    )


# ---------------------------------------------------------------------------
# 7. The pool rule, the one lock form, and the one clock (2026-09-13, wave-3)
# ---------------------------------------------------------------------------
#
# Every test in this group runs REAL threads against the REAL test database,
# and every wait in it is bounded: a regression must fail in seconds, never
# hang CI. A thread still alive after its join timeout is itself the failure.

import contextlib  # noqa: E402
import time  # noqa: E402

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

#: How long the out-of-pool holder keeps the project's advisory lock.
HOLD_SECONDS = 3.0


@contextlib.contextmanager
def _holding_the_project_lock(project_id):
    """A connection from OUTSIDE the pool (a second process, as far as the
    gate can tell) that takes `db.lock_api_project_usage` and keeps it until
    the block ends. Yields a callable that reads the database clock on that
    connection and then releases the lock, returning the release instant."""
    holder = psycopg.connect(
        db.dsn(), autocommit=False, row_factory=dict_row, options=db._server_options()
    )
    released = []

    def release():
        if not released:
            at = holder.execute("SELECT clock_timestamp() AS at").fetchone()["at"]
            holder.commit()
            released.append(at)
        return released[0]

    try:
        db.lock_api_project_usage(holder, project_id)
        yield release
    finally:
        try:
            release()
        finally:
            holder.close()


def _in_thread(fn):
    """Run `fn` on a real thread; returns (thread, box) where box holds
    `value` or `error` once it finishes."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def test_the_gate_waits_on_the_same_advisory_lock_the_database_helper_takes(caller):
    """The wave-2 gate built its own two-int advisory key while
    `db.lock_api_project_usage` hashed a text key: two locks for one project
    that exclude nothing. A reservation must now WAIT while anyone holds the
    helper's lock, and finish as soon as it is released."""
    with _holding_the_project_lock(caller.project_id) as release:
        thread, box = _in_thread(
            lambda: quotas.reserve(caller, kind="read")
        )
        thread.join(timeout=0.75)
        assert thread.is_alive(), "reserve() did not wait for db.lock_api_project_usage"
        release()
        thread.join(timeout=30)
    assert not thread.is_alive()
    assert "error" not in box, box.get("error")
    assert box["value"].requests_remaining == 59


def test_the_minute_is_read_after_the_lock_is_granted_not_before_the_request_waited(
    caller,
):
    """quotas.py stamped the moment BEFORE the pool checkout and the lock, so
    a request that waited across a minute boundary was decided against the
    previous minute and missed what had been admitted into the new one (the
    re-verifier admitted two requests at rpm=1). The reservation's instant
    must be no earlier than the moment the lock it waited for was released."""
    with _holding_the_project_lock(caller.project_id) as release:
        thread, box = _in_thread(
            lambda: quotas.reserve(caller, kind="sync", max_output_tokens=1)
        )
        time.sleep(1.5)
        released_at = release()
        thread.join(timeout=30)
    assert not thread.is_alive()
    assert "error" not in box, box.get("error")
    assert box["value"].at >= released_at, (box["value"].at, released_at)


def test_a_key_flooding_the_gate_leaves_the_shared_pool_fast_for_everyone_else(
    caller, monkeypatch
):
    """THE CONNECTION POOL RULE. The wave-2 gate checked a connection out and
    THEN waited on the advisory lock, so a flood on one project parked
    pool_max connections on that lock: a victim's `SELECT 1` waited until the
    lock was released (p50 0.015 s -> 0.088 s in the re-verifier's probe, and
    unbounded when the holder is slow). With the process-local gate taken
    before checkout, the flood holds at most ONE connection, and an unrelated
    query stays fast while the lock is held for seconds."""
    monkeypatch.setattr(settings, "public_api_quota_gate_wait_seconds", 60.0)
    pool_max = int(db.pool().max_size)
    flood = pool_max * 2 + 8

    latencies = []
    with _holding_the_project_lock(caller.project_id) as release:
        threads = [
            _in_thread(lambda: quotas.reserve(caller, kind="read"))
            for _ in range(flood)
        ]
        time.sleep(0.75)  # every flood thread has reached the gate or the lock
        for _ in range(5):
            started = time.monotonic()
            probe, probe_box = _in_thread(_select_one)
            probe.join(timeout=HOLD_SECONDS + 5)
            latencies.append(time.monotonic() - started)
            assert "error" not in probe_box, probe_box.get("error")
        release()
        for thread, _ in threads:
            thread.join(timeout=60)

    assert all(not thread.is_alive() for thread, _ in threads)
    failures = [box.get("error") for _, box in threads if "error" in box]
    assert failures == []
    # Without the gate every probe waits for the holder (seconds). One second
    # is an order of magnitude above a healthy checkout and far below that.
    assert max(latencies) < 1.0, latencies


def _select_one():
    with db.connection() as con:
        return con.execute("SELECT 1 AS one").fetchone()


def test_a_request_that_cannot_reach_its_projects_gate_in_time_is_refused_without_a_connection(
    caller, monkeypatch
):
    """The gate's wait is bounded, so a flood cannot park the worker threads
    the chat app shares either: past `public_api_quota_gate_wait_seconds` the
    request is a 429 with Retry-After, decided without ever checking out a
    pooled connection."""
    monkeypatch.setattr(settings, "public_api_quota_gate_wait_seconds", 0.3)
    with _holding_the_project_lock(caller.project_id) as release:
        first, first_box = _in_thread(lambda: quotas.reserve(caller, kind="read"))
        time.sleep(0.3)  # `first` now holds the gate and waits on the lock

        checkouts = []
        real = db.connection

        def counting():
            checkouts.append(threading.get_ident())
            return real()

        monkeypatch.setattr(db, "connection", counting)
        started = time.monotonic()
        second, second_box = _in_thread(lambda: quotas.reserve(caller, kind="read"))
        second.join(timeout=HOLD_SECONDS)
        elapsed = time.monotonic() - started
        still_waiting = second.is_alive()
        monkeypatch.setattr(db, "connection", real)
        release()
        first.join(timeout=30)
        second.join(timeout=30)

    assert not still_waiting, "the second request waited on the lock instead of the gate"
    assert elapsed < 2.0
    assert isinstance(second_box.get("error"), errors.ApiError)
    assert second_box["error"].code == "rate_limit_error"
    assert int(second_box["error"].headers()["Retry-After"]) >= 1
    assert second.ident not in checkouts
    assert "error" not in first_box, first_box.get("error")


@pytest.fixture()
def one_connection_at_a_time(monkeypatch):
    """Fail any code path that checks out a SECOND pooled connection while it
    still holds one on the same thread — the shape that stalls a shared pool
    at pool_max concurrency. Yields the list of violations."""
    depth = threading.local()
    violations = []
    real = db.connection

    @contextlib.contextmanager
    def guarded():
        level = getattr(depth, "level", 0)
        if level:
            violations.append("nested checkout")
        depth.level = level + 1
        try:
            with real() as con:
                yield con
        finally:
            depth.level = level

    monkeypatch.setattr(db, "connection", guarded)
    return violations


def test_reserving_and_settling_never_hold_two_pooled_connections_at_once(
    caller, one_connection_at_a_time
):
    reservation = quotas.reserve(
        caller, kind="stream", estimated_input_tokens=50, max_output_tokens=100
    )
    reservation.settle(40, 60, "completed")
    assert one_connection_at_a_time == []
    # The refusal path writes `rate_limited` inside the same block: it must
    # not open a second connection either.
    projects.update_project(caller.project_id, caller.workspace_id, rpm=0)
    frozen = _reresolve(caller)
    with pytest.raises(errors.ApiError):
        quotas.reserve(frozen, kind="read")
    assert one_connection_at_a_time == []


def test_an_idempotency_claim_never_holds_two_pooled_connections_at_once(
    caller, one_connection_at_a_time
):
    first = idempotency.claim(caller.project_id, "v1_responses", "pool", {"input": "x"})
    second = idempotency.claim(caller.project_id, "v1_responses", "pool", {"input": "x"})
    assert first.claimed is True and second.claimed is False
    assert one_connection_at_a_time == []


def test_an_idempotency_claim_decides_and_reads_on_one_clock_even_when_the_host_clock_is_behind(
    caller, monkeypatch
):
    """2026-09-13, wave-3 re-verify: the claim decided "still alive" on the
    Python clock while `db.get_idempotency` decided "expired" on the database
    clock, so a retry against a row between the two answers got neither the
    claim nor the row — a spurious 500. Here the host clock runs two hours
    behind the database; the claim must still take over the expired row."""
    with db.connection() as con:
        db_now = con.execute("SELECT clock_timestamp() AS at").fetchone()["at"]
    idempotency.claim(
        caller.project_id, "v1_responses", "skewed", {"input": "x"},
        ttl_hours=1, now=db_now - timedelta(hours=2),
    )  # expired an hour ago, by the database's clock

    class HostClockBehind(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) - timedelta(hours=2)

    monkeypatch.setattr(idempotency, "datetime", HostClockBehind)
    retried = idempotency.claim(caller.project_id, "v1_responses", "skewed", {"input": "x"})

    assert retried.claimed is True


# ---------------------------------------------------------------------------
# 7. Unlimited by default (owner decision, 2026-09-13)
# ---------------------------------------------------------------------------


@pytest.fixture()
def unlimited(monkeypatch):
    """PUBLIC_API_ENFORCE_LIMITS as it ships: off."""
    monkeypatch.setattr(settings, "public_api_enforce_limits", False)


def test_the_limits_are_off_unless_the_operator_sets_the_switch(monkeypatch):
    """The default IS the decision: a fresh `Settings` with nothing in the
    environment enforces nothing, and only an explicit true turns it on."""
    from app.config import Settings

    monkeypatch.delenv("PUBLIC_API_ENFORCE_LIMITS", raising=False)
    assert Settings().public_api_enforce_limits is False
    monkeypatch.setenv("PUBLIC_API_ENFORCE_LIMITS", "")
    assert Settings().public_api_enforce_limits is False
    monkeypatch.setenv("PUBLIC_API_ENFORCE_LIMITS", "false")
    assert Settings().public_api_enforce_limits is False
    monkeypatch.setenv("PUBLIC_API_ENFORCE_LIMITS", "true")
    assert Settings().public_api_enforce_limits is True


def test_with_the_limits_off_a_burst_far_above_the_old_rate_is_admitted_and_counted_once_each(
    caller, unlimited
):
    """150 simultaneous requests against a project whose stored rpm is 1 —
    the burst that admits exactly one when enforced. All 150 are admitted,
    and the ledgers show 150 requests: counted once each, never refused,
    never double-counted."""
    projects.update_project(caller.project_id, caller.workspace_id, rpm=1)
    stored_rpm_one = _reresolve(caller)

    admitted, refused, unexpected = _burst([stored_rpm_one] * 150, now=NOON)

    assert unexpected == []
    assert refused == []
    assert len(admitted) == 150
    usage = ledger(stored_rpm_one, at=NOON)
    assert usage["minute"]["requests"] == 150
    assert usage["daily"]["requests"] == 150
    assert usage["daily"]["rate_limited"] == 0


def test_with_the_limits_off_read_routes_from_many_keys_are_all_admitted_and_counted(
    caller, unlimited
):
    """The key's own tightening is a limit too: a key parked at rpm=0 and its
    siblings are all admitted, and every request lands in the project's day."""
    parked = _key_in(caller, "parked", rpm=0)
    siblings = [_key_in(caller, f"sibling-{n}") for n in range(4)]

    admitted, refused, unexpected = _burst(
        ([parked] * 20) + [one for one in siblings for _ in range(20)],
        now=NOON,
        kind="read",
    )

    assert unexpected == [] and refused == []
    assert len(admitted) == 100
    assert ledger(caller, at=NOON)["daily"]["requests"] == 100
    assert ledger(parked, at=NOON)["minute"]["requests"] == 20


def test_with_the_limits_off_a_huge_token_volume_is_admitted_and_recorded_as_measured(
    caller, unlimited
):
    """Token limits of 1,000 a minute and 1,000 a day, and twenty generations
    of a million input tokens each: all admitted, none refused, and once each
    is settled the ledgers hold exactly what was measured — twenty million in,
    eight million out — once."""
    projects.update_project(
        caller.project_id,
        caller.workspace_id,
        input_tpm=1_000,
        output_tpm=1_000,
        daily_token_quota=1_000,
    )
    metered = _reresolve(caller)

    reservations = [
        quotas.reserve(
            metered,
            kind="sync",
            estimated_input_tokens=1_000_000,
            max_output_tokens=500_000,
            now=NOON,
        )
        for _ in range(20)
    ]
    assert all(r.enforced is False for r in reservations)
    during = ledger(metered, at=NOON)
    assert during["minute"]["requests"] == 20
    assert during["minute"]["input_tokens"] == 20_000_000

    for reservation in reservations:
        reservation.settle(1_000_000, 400_000, "completed", now=NOON)
        # A second settlement is a no-op, exactly as when enforced.
        reservation.settle(1_000_000, 400_000, "completed", now=NOON)

    after = ledger(metered, at=NOON)
    assert after["minute"] == {
        "requests": 20,
        "input_tokens": 20_000_000,
        "output_tokens": 8_000_000,
    }
    assert after["daily"]["input_tokens"] == 20_000_000
    assert after["daily"]["output_tokens"] == 8_000_000
    assert after["daily"]["rate_limited"] == 0


def test_with_the_limits_off_limits_stored_as_zero_refuse_nothing(caller, unlimited):
    """Zero means zero ONLY when enforced. With the switch off a project
    frozen at rpm=0, input_tpm=0, output_tpm=0, daily_token_quota=0 and
    max_concurrency=0 still runs — and still records."""
    projects.update_project(
        caller.project_id,
        caller.workspace_id,
        rpm=0,
        input_tpm=0,
        output_tpm=0,
        daily_token_quota=0,
        max_concurrency=0,
    )
    frozen = _reresolve(caller)

    quotas.reserve(frozen, kind="stream", estimated_input_tokens=10, now=NOON)
    quotas.reserve(frozen, kind="read", now=NOON)
    with quotas.concurrency_slot(frozen, "stream"):
        assert quotas.in_flight(frozen) == 1

    assert ledger(frozen, at=NOON)["daily"]["requests"] == 2


def test_with_the_limits_off_many_concurrent_generations_all_get_a_slot_and_give_it_back(
    caller, unlimited
):
    """Fifty simultaneous generations against max_concurrency=4 across sync,
    stream and background: every slot granted, the counter still honest
    while they run, and zero once they finish."""
    every_key = [caller] + [_key_in(caller, f"k{n}", max_concurrency=1) for n in range(4)]
    gate = threading.Barrier(50)
    lock = threading.Lock()
    seen = {"granted": 0, "peak": 0, "now": 0, "refused": 0}
    release = threading.Event()
    kinds = ("sync", "stream", "background")

    def hold(n):
        gate.wait(timeout=30)
        try:
            lease = quotas.take_slot(every_key[n % 5], kinds[n % 3])
        except errors.ApiError:
            with lock:
                seen["refused"] += 1
            return
        with lock:
            seen["granted"] += 1
            seen["now"] += 1
            seen["peak"] = max(seen["peak"], seen["now"])
        release.wait(timeout=30)
        with lock:
            seen["now"] -= 1
        lease.release()

    threads = [threading.Thread(target=hold, args=(n,)) for n in range(50)]
    for thread in threads:
        thread.start()
    deadline = datetime.now(timezone.utc) + timedelta(seconds=20)
    while seen["granted"] + seen["refused"] < 50 and datetime.now(timezone.utc) < deadline:
        threading.Event().wait(0.02)
    assert quotas.in_flight(caller) == 50
    release.set()
    for thread in threads:
        thread.join(timeout=60)

    assert seen["refused"] == 0
    assert seen["granted"] == 50
    assert seen["peak"] == 50
    assert quotas.in_flight(caller) == 0


def test_with_the_limits_off_no_ratelimit_field_is_built_anywhere(caller, unlimited):
    """A `RateLimit-Policy` of q=60 on an API that admits the 61st request is
    a false statement a client would throttle itself by. Both builders — the
    reservation's and the refusal path's — return nothing."""
    reservation = quotas.reserve(caller, kind="read", now=NOON)
    assert reservation.headers() == {}
    assert reservation.window is None
    assert reservation.requests_remaining is None
    assert quotas.limit_headers(caller, remaining=0) == {}
    assert quotas.limit_headers(caller, at=NOON) == {}


def test_the_same_reservation_advertises_ratelimit_again_once_the_switch_is_on(
    caller, monkeypatch
):
    """The switch is read per call, not at import: turning it on makes the
    next request carry both fields again."""
    monkeypatch.setattr(settings, "public_api_enforce_limits", False)
    assert quotas.reserve(caller, kind="read", now=NOON).headers() == {}
    monkeypatch.setattr(settings, "public_api_enforce_limits", True)
    headers = quotas.reserve(caller, kind="read", now=NOON).headers()
    assert set(headers) == {"RateLimit", "RateLimit-Policy"}


def test_with_the_limits_off_a_busy_project_gate_never_turns_into_a_429(
    caller, unlimited, monkeypatch
):
    """The process gate's bounded wait is itself a refusal (429 after
    `public_api_quota_gate_wait_seconds`). With nothing to decide the gate is
    not taken at all, so a request is admitted even while another thread
    holds the project's gate."""
    monkeypatch.setattr(settings, "public_api_quota_gate_wait_seconds", 0.0)
    holding = threading.Event()
    done = threading.Event()

    def hold_the_gate():
        with quotas._project_gate(caller.project_id):
            holding.set()
            done.wait(timeout=30)

    holder = threading.Thread(target=hold_the_gate)
    holder.start()
    try:
        assert holding.wait(timeout=10)
        quotas.reserve(caller, kind="sync", estimated_input_tokens=5, now=NOON)
    finally:
        done.set()
        holder.join(timeout=30)
    assert ledger(caller, at=NOON)["minute"]["requests"] == 1


def test_an_unknown_kind_is_still_an_error_with_the_limits_off(caller, unlimited):
    """Unlimited is not unvalidated: a kind nobody declared is a programming
    error in both modes, and nothing is written for it."""
    with pytest.raises(ValueError):
        quotas.reserve(caller, kind="bulk", now=NOON)
    with pytest.raises(ValueError):
        quotas.take_slot(caller, "bulk")
    assert ledger(caller, at=NOON)["daily"]["requests"] == 0


def test_an_unlimited_reservation_uses_one_pooled_connection(
    caller, unlimited, one_connection_at_a_time
):
    reservation = quotas.reserve(
        caller, kind="stream", estimated_input_tokens=50, max_output_tokens=100
    )
    reservation.settle(40, 60, "completed")
    assert one_connection_at_a_time == []


@pytest.mark.parametrize("enforced", [True, False])
def test_a_settlement_writes_every_minute_row_before_the_daily_row(
    caller, monkeypatch, enforced
):
    """`reserve` locks minute(now) then the daily row. A settlement that
    crosses a minute boundary used to lock the daily row BETWEEN its two
    minute rows, so it and a reservation in the new minute could each hold
    the row the other wanted — a deadlock PostgreSQL resolves by failing one.
    Unlimited admission runs bursts unserialised, which makes that likelier;
    one statement order removes the cycle."""
    monkeypatch.setattr(settings, "public_api_enforce_limits", enforced)
    reservation = quotas.reserve(
        caller, kind="sync", estimated_input_tokens=100, max_output_tokens=50, now=NOON
    )
    tables = []
    real = db.connection

    class Recording:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *args, **kwargs):
            text = " ".join(str(sql).split())
            for table in ("api_usage_minute", "api_usage_daily"):
                if table in text and not text.startswith("SELECT"):
                    tables.append(table)
            return self._con.execute(sql, *args, **kwargs)

    @contextlib.contextmanager
    def recording():
        with real() as con:
            yield Recording(con)

    monkeypatch.setattr(db, "connection", recording)
    reservation.settle(80, 30, "completed", now=NOON + timedelta(seconds=45))

    assert tables == [
        "api_usage_minute",
        "api_usage_minute",
        "api_usage_daily",
        "api_usage_daily",
    ]
