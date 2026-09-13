"""The quota gate — CONTRACT-3 §12, in front of admission and in front of work.

Four limits, three storage strategies, one entry point:

    requests / minute       durable sliding window over `api_usage_minute`
    input tokens / minute    "        "        "      "        "
    output tokens / minute   "        "        "      "        "
    daily tokens            durable ledger over `api_usage_daily`
    concurrent requests     in-process counter, per PROJECT

EVERY LIMIT IS A PROJECT LIMIT (2026-09-13, wave-2 review). The first cut
compared the project's numbers with counters kept PER KEY, so a console user
holding `api.keys.create` multiplied the project's rpm, token rates and
concurrency by the number of keys they minted — ten keys gave 600 req/min
against rpm=60 and 40 in flight against max_concurrency=4. Now the window is
the SUM over every key in the project, the concurrency counter is keyed by
project, and a key's own `rpm` / `max_concurrency` is a second, narrower check
on that key's share: a key can only TIGHTEN, never add capacity.

THE DECISION AND THE WRITE ARE ONE TRANSACTION (2026-09-13, wave-2 review).
The first cut read the window, decided, and bumped the counter in separate
statements from worker threads; 40 concurrent requests on a key at 59/60 were
all admitted (counter 99), and the publicapi reviewer got 12/12 through rpm=1.
`reserve()` now takes `db.lock_api_project_usage` (a `pg_advisory_xact_lock`
on the project id), reads the window, decides and bumps inside that one
transaction, so concurrent requests for one project serialise and each sees
the previous one's reservation. The estimated input tokens AND the requested
output tokens are reserved the same way and settled to the measured counts by
`record_usage(reservation=…)`.

THE CONNECTION POOL RULE (2026-09-13, wave-3 re-verify). The app has ONE
psycopg pool (max 16) shared with the chat application. The wave-2 gate
checked a connection out FIRST and then waited on the advisory lock, so one
key flooding `reserve()` from 40 threads parked 16 pooled connections on the
lock: an unrelated `SELECT 1` went from p50 0.015 s to 0.088 s, and with a
slow holder every chat request waits for a connection none of the waiters
will release. Now a PROCESS-LOCAL per-project lock is taken BEFORE a
connection is checked out, with a bounded wait that answers 429 rather than
parking the thread pool, and the advisory lock stays inside the one
connection that does all the work — it is what keeps the decision correct
across processes, and within this process it is never contended.

ONE LOCK FORM (2026-09-13, wave-3 re-verify). The wave-2 gate built its own
two-int advisory key while `db.lock_api_project_usage` used
`hashtextextended('api_project_usage:' || id)`: two different locks for the
same project, which exclude nothing from each other. The gate calls the db
helper; the database team keeps that form stable.

ONE CLOCK (2026-09-13, wave-3 re-verify). The minute is read from the
database with `clock_timestamp()` AFTER the lock is held. Stamped in Python
before the checkout, a request that waited across a minute boundary was
decided against the previous minute's buckets and could not see what had
already been admitted into the new one (probe at rpm=1: two admitted).

ZERO MEANS ZERO. A stored limit of 0 refuses everything; only NULL inherits a
default (`resolver._effective_limits`).

WHY THE GATE SITS IN FRONT OF ADMISSION (CONTRACT-3 §11). The ten NORMAL
admission lanes are SHARED with the chat application. Without a quota check
ahead of them, one developer key looping `POST /v1/responses` would take every
lane and every signed-in person would watch their chat hang — a public API
turned into a denial-of-service button against the product it sits beside.
So: refuse here, before a lane is taken.

WHY A SLIDING WINDOW AND NOT A FIXED ONE. A fixed per-minute counter lets a
caller spend the whole minute's allowance in the last second of one minute and
the whole of the next in the first second of the next — twice the advertised
rate, sustained, at the boundary. The standard fix is the weighted
approximation implemented in `_sliding()`: count this minute in full, plus the
fraction of the previous minute the window still covers.

WHY POSTGRESQL AND NOT REDIS (CONTRACT-3 §12). There is one orchestrator
process, so the only thing a second store would add is a second thing to lose.
The durable counters must survive a restart — a daily quota that resets when
the container does is not a quota.

WHY THIS MODULE CARRIES ITS OWN SQL. The reservation needs the lock, the read
and the two upserts on ONE connection, and most of `app/db.py`'s accessors
each open their own — calling one inside the locked block would check out a
SECOND pooled connection while holding the first, which at pool_max
concurrency is a stall until PoolTimeout. The statements below are the same upserts as
`db.bump_usage_minute` / `db.bump_usage_daily`, run inside the gate's
transaction; if the database owner adds a transactional accessor, these are
the lines to replace.

TOKENS ARE WRITTEN ONCE PER REQUEST. Never per token, never per SSE event.

UNLIMITED UNLESS THE OPERATOR SAYS OTHERWISE (owner decision, 2026-09-13,
explicit and final). The public developer API has NO request, token-per-minute,
daily-quota or concurrency limit. `settings.public_api_enforce_limits`
(PUBLIC_API_ENFORCE_LIMITS, default false) is read HERE and nowhere else that
decides: `reserve()` and `take_slot()` are the only two admission decisions,
and `/v1`, background jobs and the console playground all go through them, so
every caller inherits the switch without a check of its own.

  * OFF (the default): `reserve()` still writes the request counter and the
    token reservation to BOTH ledgers in one transaction — the console usage
    page, the request logs and `/v1/usage` read those rows — but it reads no
    window, takes neither lock and never raises; `take_slot()` still counts
    in-flight generations (the console reads the counter) but never refuses;
    `limit_headers()` returns nothing, because a `RateLimit` field would
    advertise a ceiling that does not exist.
  * ON: everything documented above, byte for byte as before the switch.

What the switch does NOT touch, because those are technical safety limits and
not usage limits: the model's context window, the max_output_tokens ceilings,
the request body cap and the engine's shared admission queue (a full lane is
still a 429 `concurrency_limit_exceeded` from `streaming.engine_error`).

WHAT THIS MODULE DOES NOT DO. It never decides WHO the caller is — that is
`resolver.py`. It emits no quota state for an unauthenticated caller: every
public function here takes an `ApiCaller`, and there is no way to obtain one
without a key that verified (draft-ietf-httpapi-ratelimit-headers-11, Security
Considerations).
"""
from __future__ import annotations

import logging
import math
import random
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, Mapping, Optional

from .. import db
from ..config import settings
from ..publicapi import errors
from .resolver import ApiCaller

log = logging.getLogger(__name__)

#: The window every per-minute limit is expressed over. Sixty seconds is not a
#: tuning knob: `api_usage_minute.bucket` is minute-truncated, so the storage
#: and the window are the same thing.
WINDOW_SECONDS = 60

#: The window the daily ledger is expressed over, for the `RateLimit-Policy`
#: field. A day here is a UTC day, because `api_usage_daily.day` is a `date`
#: written from `now()` on a session pinned to UTC (`db._server_options`) —
#: a caller in Kolkata gets a reset at 05:30 local, and the documentation says
#: so rather than the code pretending otherwise.
DAY_SECONDS = 86_400

def limits_enforced() -> bool:
    """Whether the usage limits are enforced — read on EVERY call, never
    cached at import, so an operator's change and a test's monkeypatch take
    effect on the next request (owner decision 2026-09-13; default False)."""
    return bool(getattr(settings, "public_api_enforce_limits", False))


#: `Retry-After` is integer seconds with a floor of 1 (RFC 9110 + the OpenAI
#: compatibility schema STANDARDS.md copies). A zero invites the retry storm
#: we just throttled.
MIN_RETRY_AFTER = 1


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------


def _now(now: Optional[datetime] = None) -> datetime:
    """Always timezone-aware UTC.

    Every function that can be affected by the passage of time takes `now` so
    a test can DRIVE the clock instead of sleeping through it. A rate-limit
    suite that sleeps for sixty seconds to prove the window reopens is a suite
    nobody runs, which means it is a limit nobody tests.
    """
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("a naive datetime cannot be compared with a timestamptz")
    return now.astimezone(timezone.utc)


def _database_now(con: Any) -> datetime:
    """The database's clock, read on the connection that holds the lock.

    `clock_timestamp()` and not `now()`: `now()` is frozen at the start of the
    transaction, and the point of reading it here is to take the time AFTER
    the lock was granted, not when the transaction that waited for it began.
    Sessions are pinned to UTC (`db._server_options`).
    """
    row = con.execute("SELECT clock_timestamp() AS at").fetchone()
    value = row["at"] if isinstance(row, Mapping) else row[0]
    return _now(value if value.tzinfo else value.replace(tzinfo=timezone.utc))


def _seconds_into_minute(moment: datetime) -> float:
    return moment.second + moment.microsecond / 1_000_000


# ---------------------------------------------------------------------------
# The durable sliding window
# ---------------------------------------------------------------------------

#: The request kinds `reserve()` accepts. The three that generate spend tokens
#: and are measured against the token limits; `read` (GET /v1/models,
#: /v1/responses/{id}, /v1/usage) counts one request against RPM and nothing
#: else, so a caller out of tokens can still read its own usage.
GENERATING_KINDS = frozenset({"sync", "stream", "background"})
RESERVE_KINDS = GENERATING_KINDS | frozenset({"read"})

#: The process-local gate: one lock per PROJECT, taken BEFORE a pooled
#: connection is checked out (see "THE CONNECTION POOL RULE" above). Entries
#: are reference-counted and dropped when nobody holds or waits for them, so
#: a million distinct projects over a process's life cost nothing once idle.
_gates: Dict[str, list] = {}
_gates_guard = threading.Lock()


@contextmanager
def _project_gate(project_id: str) -> Iterator[None]:
    """Serialise this process's usage decisions for one project, holding NO
    database connection while waiting.

    The wait is bounded by `settings.public_api_quota_gate_wait_seconds`: a
    waiter sits on a worker thread, and those threads are shared with the chat
    app, so a project that cannot get through its own gate in that time is
    told to come back (429 `rate_limit_error`, Retry-After) instead of parking
    the thread pool behind it.
    """
    with _gates_guard:
        entry = _gates.get(project_id)
        if entry is None:
            entry = _gates[project_id] = [threading.Lock(), 0]
        entry[1] += 1
    try:
        wait = max(0.0, float(settings.public_api_quota_gate_wait_seconds))
        if not entry[0].acquire(timeout=wait):
            log.warning(
                "quota gate for project %s busy for %.1fs; refusing", project_id, wait
            )
            raise errors.rate_limit(_jittered(MIN_RETRY_AFTER))
        try:
            yield
        finally:
            entry[0].release()
    finally:
        with _gates_guard:
            entry[1] -= 1
            if entry[1] <= 0 and _gates.get(project_id) is entry:
                del _gates[project_id]


def _bucket(moment: datetime) -> datetime:
    # The same truncation as `db._minute_bucket`, so the rows this module
    # writes and the rows `db.bump_usage_minute` writes are the same rows.
    return moment.replace(second=0, microsecond=0)


@dataclass(frozen=True)
class WindowState:
    """What the ledgers say at one instant. Pure numbers, no decisions.

    `requests`, `input_tokens` and `output_tokens` are the WEIGHTED sliding
    estimates over the WHOLE PROJECT (every key summed); `key_requests` is the
    same estimate over this caller's key alone, for the key's own tightening.
    `daily_*` are exact integers from `api_usage_daily`.
    """

    at: datetime
    requests: float
    input_tokens: float
    output_tokens: float
    daily_input_tokens: int
    daily_output_tokens: int
    #: The raw PROJECT counters, kept for the Retry-After arithmetic and for
    #: the tests that pin the arithmetic rather than its conclusion.
    current_minute: Mapping[str, int] = None  # type: ignore[assignment]
    previous_minute: Mapping[str, int] = None  # type: ignore[assignment]
    key_requests: float = 0.0
    key_current_requests: int = 0
    key_previous_requests: int = 0

    @property
    def daily_tokens(self) -> int:
        return int(self.daily_input_tokens) + int(self.daily_output_tokens)


def _sliding(previous: float, current: float, moment: datetime) -> float:
    """The weighted estimate: this minute in full, plus what is left of the last.

    At :00 the previous minute counts entirely and the current one is empty;
    at :30 the previous counts half; at :59.999 it has all but decayed. It
    needs exactly two buckets and cannot be gamed at the minute boundary the
    way a fixed counter can.
    """
    weight = (WINDOW_SECONDS - _seconds_into_minute(moment)) / WINDOW_SECONDS
    return float(previous) * max(0.0, min(1.0, weight)) + float(current)


_WINDOW_SQL = """
SELECT
    COALESCE(SUM(m.requests)      FILTER (WHERE m.bucket = %(cur)s), 0)  AS cur_requests,
    COALESCE(SUM(m.input_tokens)  FILTER (WHERE m.bucket = %(cur)s), 0)  AS cur_input,
    COALESCE(SUM(m.output_tokens) FILTER (WHERE m.bucket = %(cur)s), 0)  AS cur_output,
    COALESCE(SUM(m.requests)      FILTER (WHERE m.bucket = %(prev)s), 0) AS prev_requests,
    COALESCE(SUM(m.input_tokens)  FILTER (WHERE m.bucket = %(prev)s), 0) AS prev_input,
    COALESCE(SUM(m.output_tokens) FILTER (WHERE m.bucket = %(prev)s), 0) AS prev_output,
    COALESCE(SUM(m.requests) FILTER (WHERE m.bucket = %(cur)s  AND m.key_id = %(key)s), 0)
        AS key_cur_requests,
    COALESCE(SUM(m.requests) FILTER (WHERE m.bucket = %(prev)s AND m.key_id = %(key)s), 0)
        AS key_prev_requests,
    (SELECT d.input_tokens  FROM api_usage_daily d
      WHERE d.project_id = %(project)s AND d.day = %(day)s) AS daily_input,
    (SELECT d.output_tokens FROM api_usage_daily d
      WHERE d.project_id = %(project)s AND d.day = %(day)s) AS daily_output
FROM api_usage_minute m
WHERE m.project_id = %(project)s AND m.bucket IN (%(cur)s, %(prev)s)
"""


def _read_window_on(con: Any, caller: ApiCaller, moment: datetime) -> WindowState:
    """Both minute buckets for the project (and the key's share of them) and
    today's ledger, in ONE statement on the caller's connection — so inside
    `reserve()` it is read under the project lock, and outside it costs one
    round trip rather than the two the first cut spent."""
    bucket = _bucket(moment)
    row = con.execute(
        _WINDOW_SQL,
        {
            "cur": bucket,
            "prev": bucket - timedelta(seconds=WINDOW_SECONDS),
            "key": caller.key_id,
            "project": caller.project_id,
            "day": moment.date(),
        },
    ).fetchone()
    cur = {
        "requests": int(row["cur_requests"]),
        "input_tokens": int(row["cur_input"]),
        "output_tokens": int(row["cur_output"]),
    }
    prev = {
        "requests": int(row["prev_requests"]),
        "input_tokens": int(row["prev_input"]),
        "output_tokens": int(row["prev_output"]),
    }
    key_cur = int(row["key_cur_requests"])
    key_prev = int(row["key_prev_requests"])
    return WindowState(
        at=moment,
        requests=_sliding(prev["requests"], cur["requests"], moment),
        input_tokens=_sliding(prev["input_tokens"], cur["input_tokens"], moment),
        output_tokens=_sliding(prev["output_tokens"], cur["output_tokens"], moment),
        daily_input_tokens=int(row["daily_input"] or 0),
        daily_output_tokens=int(row["daily_output"] or 0),
        current_minute=cur,
        previous_minute=prev,
        key_requests=_sliding(key_prev, key_cur, moment),
        key_current_requests=key_cur,
        key_previous_requests=key_prev,
    )


def read_window(caller: ApiCaller, *, now: Optional[datetime] = None) -> WindowState:
    """The project's window and today's ledger, from the database, now.

    RE-READ EVERY REQUEST, never cached. STANDARDS.md names a cached
    authorization state as the failure that makes revocation silently
    ineffective, and the same logic applies to a quota: whatever TTL you pick
    IS the window during which a limit does not exist.
    """
    with db.connection() as con:
        moment = _now(now) if now is not None else _database_now(con)
        return _read_window_on(con, caller, moment)


def limits_for(key_row: Mapping[str, Any], project: Mapping[str, Any]):
    """The effective limits for a key row inside a project row — the row
    first, `settings` only for a NULL column, and 0 enforced as 0. The rule
    itself lives in `resolver._effective_limits` (one implementation, used by
    the resolver on every request); this name is the one `config.py` points
    readers at."""
    from .resolver import _effective_limits

    return _effective_limits(key_row, project)


def _seconds_until_available(
    previous: float, current: float, limit: float, wanted: float, moment: datetime
) -> float:
    """How long until `wanted` more units fit under `limit` in this window.

    Solved, not guessed — and the second case below is the one a "wait until
    the minute ends" answer gets WRONG. With `s` seconds into the minute the
    estimate is `previous * (60 - s)/60 + current`, so the request fits once

        previous * (60 - s')/60  <=  limit - current - wanted

    which rearranges to `s' >= 60 * (1 - room/previous)`. Two shapes:

    * THE CURRENT MINUTE STILL HAS ROOM and only the decaying previous minute
      is in the way. Solve directly for `s'` and wait that long.

    * THE CURRENT MINUTE ALONE IS ALREADY OVER (`room <= 0`). Waiting out the
      rest of this minute is NOT enough, and answering that is how a caller
      retries straight into a second 429: at the rollover, today's `current`
      becomes tomorrow's `previous` and starts decaying from full. So the
      answer is the remainder of this minute PLUS however far into the next
      one the old count has to decay — solved the same way, against a
      `current` that starts at zero. Sixty requests in a 60-a-minute window
      at :30 gives 30 + 1 = 31 seconds, and at :30+31 the estimate is exactly
      59, which admits one more.

    Never less than one second: a `Retry-After: 0` is an invitation to retry
    into the same refusal.
    """
    seconds = _seconds_into_minute(moment)
    remainder = WINDOW_SECONDS - seconds
    room_now = float(limit) - float(current) - float(wanted)

    if room_now > 0:
        if previous <= 0:
            # Nothing is in the way at all; only reachable if the caller asked
            # about a request that would have been admitted.
            return MIN_RETRY_AFTER
        target = WINDOW_SECONDS * (1.0 - room_now / float(previous))
        if target <= seconds:
            return MIN_RETRY_AFTER
        return max(MIN_RETRY_AFTER, min(remainder, target - seconds))

    if current <= 0:
        # The window is over its limit on the previous minute alone and this
        # minute has spent nothing — one full rollover is the whole answer.
        return max(MIN_RETRY_AFTER, remainder)
    room_next = float(limit) - float(wanted)
    into_next = WINDOW_SECONDS * (1.0 - room_next / float(current))
    into_next = max(0.0, min(float(WINDOW_SECONDS), into_next))
    return max(MIN_RETRY_AFTER, remainder + into_next)


def _seconds_until_midnight(moment: datetime) -> float:
    """Until the UTC day rolls over, which is when `api_usage_daily` starts a
    new row. Not the caller's local midnight: the ledger's `day` column is a
    UTC date and inventing a per-caller timezone would make the counter and
    the header disagree."""
    tomorrow = (moment + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(MIN_RETRY_AFTER, (tomorrow - moment).total_seconds())


def _jittered(seconds: float) -> int:
    """Integer seconds, with the draft's mandated jitter added.

    draft-ietf-httpapi-ratelimit-headers-11 asks for jitter by name and gives
    the worked example: without it, every client throttled in the same minute
    returns at the same second and the stampede is worse than the load that
    caused the throttle. The draw is taken ONCE per refusal so that the
    `Retry-After` a caller is given and the `t` it is paired with come from
    the same number — the same draft makes "Retry-After earlier than the end
    of the effective window" a SHOULD NOT, and an inconsistent pair guarantees
    a second 429.

    `settings.public_api_ratelimit_jitter_seconds = 0` makes this the identity
    function, which is what a test that pins an exact second sets.
    """
    jitter = max(0.0, float(settings.public_api_ratelimit_jitter_seconds))
    drawn = random.uniform(0.0, jitter) if jitter else 0.0
    # ROUNDED BEFORE THE CEILING. `_seconds_until_available` solves a linear
    # equation in floats, and an exact answer of 31 arrives as
    # 31.000000000000004 — which `ceil` faithfully turns into 32. One second
    # of extra backoff is harmless; an arithmetic identity that only holds to
    # fifteen decimal places and then silently does not is the kind of thing
    # that makes a test suite feel haunted. Six places is far finer than any
    # real window and coarser than the noise.
    return max(MIN_RETRY_AFTER, int(math.ceil(round(float(seconds) + drawn, 6))))



# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class Reservation:
    """What `reserve` decided, and what it wrote.

    The request counter — and the estimated input tokens — have ALREADY been
    added to the project's ledgers by the time this exists, inside the same
    transaction that made the decision. That is what makes a burst of
    concurrent requests see each other.

    Settle it exactly once with `settle()` (or `record_usage(reservation=…)`)
    when the request finishes: the estimate is swapped for the measured count.
    A reservation that is never settled leaves its estimate counted, which is
    the safe direction for a gate in front of a shared resource.
    """

    caller: ApiCaller
    at: datetime
    #: None when the limits are not enforced: no window is read to decide
    #: anything (owner decision 2026-09-13).
    window: Optional[WindowState]
    #: Requests still available AFTER this one was reserved — the smaller of
    #: the project's remainder and the key's own. None when unlimited: there
    #: is no remainder of a limit that does not exist.
    requests_remaining: Optional[int]
    #: The effective window, in seconds, for the `RateLimit` field's `t`.
    window_seconds: int
    kind: str = "sync"
    #: Input tokens added to the ledgers at reservation time.
    reserved_input_tokens: int = 0
    #: Output tokens added to the ledgers at reservation time — the request's
    #: max_output_tokens (or the default), capped at the limits it is checked
    #: against. Settled to the measured count exactly once.
    reserved_output_tokens: int = 0
    #: Whether this reservation was DECIDED against the limits. False means it
    #: was only recorded (PUBLIC_API_ENFORCE_LIMITS off), and it then carries
    #: no `RateLimit` fields.
    enforced: bool = True

    @property
    def reserved_tokens(self) -> int:
        """Everything this reservation still holds in the ledgers until it is
        settled. A caller deciding whether a request that never ran needs
        settling must look at this, not at `reserved_input_tokens` alone."""
        return int(self.reserved_input_tokens) + int(self.reserved_output_tokens)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_settle_lock", threading.Lock())
        object.__setattr__(self, "_settled", False)

    def headers(self) -> Dict[str, str]:
        """The `RateLimit` fields for the response this reservation admits.

        `requests_remaining` is passed explicitly because `self.window` was
        read BEFORE the reservation was written: recomputing from it would
        advertise one more request than the caller actually has left.

        Empty for an unenforced reservation (owner decision 2026-09-13): the
        fields would advertise a limit that does not exist.
        """
        if not self.enforced:
            return {}
        return limit_headers(
            self.caller,
            window=self.window,
            at=self.at,
            remaining=self.requests_remaining,
        )

    def _claim_settlement(self) -> bool:
        with self._settle_lock:  # type: ignore[attr-defined]
            if self._settled:  # type: ignore[attr-defined]
                return False
            object.__setattr__(self, "_settled", True)
            return True

    @property
    def settled(self) -> bool:
        return bool(self._settled)  # type: ignore[attr-defined]

    def settle(
        self,
        input_tokens: Optional[int],
        output_tokens: Optional[int],
        status: str = "completed",
        *,
        now: Optional[datetime] = None,
    ) -> None:
        record_usage(
            self.caller, input_tokens, output_tokens, status, now=now, reservation=self
        )


_UPSERT_MINUTE_SQL = (
    "INSERT INTO api_usage_minute "
    "    (project_id, key_id, bucket, requests, input_tokens, output_tokens) "
    "VALUES (%s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (project_id, key_id, bucket) DO UPDATE SET "
    "    requests = api_usage_minute.requests + excluded.requests, "
    "    input_tokens = api_usage_minute.input_tokens + excluded.input_tokens, "
    "    output_tokens = api_usage_minute.output_tokens + excluded.output_tokens"
)

_UPSERT_DAILY_SQL = (
    "INSERT INTO api_usage_daily "
    "    (project_id, day, requests, input_tokens, output_tokens, errors, rate_limited) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (project_id, day) DO UPDATE SET "
    "    requests = api_usage_daily.requests + excluded.requests, "
    "    input_tokens = api_usage_daily.input_tokens + excluded.input_tokens, "
    "    output_tokens = api_usage_daily.output_tokens + excluded.output_tokens, "
    "    errors = api_usage_daily.errors + excluded.errors, "
    "    rate_limited = api_usage_daily.rate_limited + excluded.rate_limited"
)


def _decide(
    caller: ApiCaller,
    kind: str,
    window: WindowState,
    wanted_input: int,
    moment: datetime,
    output_charge: int = 0,
):
    """None to admit, or `(factory, seconds, what)` naming the refusal.

    Order: the project's requests per minute, the key's own requests per
    minute, input tokens per minute, output tokens per minute, the daily
    token quota. Pure: it reads the window it is given and writes nothing.
    """
    limits = caller.limits
    cur = window.current_minute
    prev = window.previous_minute

    project_rpm = limits.project_rpm_limit
    if window.requests + 1 > project_rpm:
        return (
            errors.rate_limit,
            _seconds_until_available(
                prev["requests"], cur["requests"], project_rpm, 1, moment
            ),
            "requests",
        )

    if limits.key_rpm is not None and window.key_requests + 1 > limits.key_rpm:
        return (
            errors.rate_limit,
            _seconds_until_available(
                window.key_previous_requests,
                window.key_current_requests,
                limits.key_rpm,
                1,
                moment,
            ),
            "key requests",
        )

    if kind not in GENERATING_KINDS:
        return None

    # A generation spends at least one input token, so the check is made with
    # at least one: an estimate of 0 against an input_tpm of 0 is a request
    # the operator said may not run.
    spend = max(1, wanted_input)
    if window.input_tokens + spend > limits.input_tpm:
        return (
            errors.rate_limit,
            _seconds_until_available(
                prev["input_tokens"], cur["input_tokens"], limits.input_tpm, spend, moment
            ),
            "input tokens",
        )

    # OUTPUT IS RESERVED, NOT GUESSED AFTER THE FACT (2026-09-13, wave-3
    # re-verify). The wave-2 check compared only what had ALREADY been spent,
    # so max_concurrency long generations admitted together could overshoot
    # output_tpm and the daily quota by max_concurrency x max_output_tokens.
    # `output_charge` is what the request may generate (see `_output_charge`),
    # and it is added to the window here exactly like the input estimate.
    # A limit of 0 refuses: the charge is at least one token.
    if limits.output_tpm <= 0 or window.output_tokens + output_charge > limits.output_tpm:
        return (
            errors.rate_limit,
            _seconds_until_available(
                prev["output_tokens"],
                cur["output_tokens"],
                limits.output_tpm,
                output_charge,
                moment,
            ),
            "output tokens",
        )

    if window.daily_tokens + spend + output_charge > limits.daily_token_quota:
        # A different code from the ones above even though both are 429:
        # CONTRACT-3 §9 separates `rate_limit_error` (slow down) from
        # `quota_exceeded` (you are done for the day).
        return (errors.quota_exceeded, _seconds_until_midnight(moment), "daily tokens")

    return None


def _output_charge(caller: ApiCaller, max_output_tokens: Optional[int]) -> int:
    """The output tokens a generation reserves at admission.

    The request's own `max_output_tokens` when the route passes it, else the
    default (narrowed by the project's `max_output_tokens` ceiling when it
    sets one), at least 1. CAPPED at output_tpm and at the daily quota: a
    request allowed to ask for 8,192 tokens in a project whose output_tpm is
    1,000 must still be admissible in an empty window — otherwise it is a 429
    whose Retry-After can never come true. The cap is only a bound on the
    reservation; the engine's own ceiling still bounds the generation.
    """
    if max_output_tokens is None:
        wanted = int(settings.public_api_default_max_output_tokens)
        ceiling = getattr(caller.limits, "max_output_tokens", None)
        if ceiling is not None and int(ceiling) > 0:
            wanted = min(wanted, int(ceiling))
    else:
        wanted = int(max_output_tokens)
    wanted = max(1, wanted)
    limits = caller.limits
    for bound in (limits.output_tpm, limits.daily_token_quota):
        if int(bound) > 0:
            wanted = min(wanted, int(bound))
    return max(1, wanted)


def reserve(
    caller: ApiCaller,
    *,
    kind: str,
    estimated_input_tokens: int = 0,
    max_output_tokens: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Reservation:
    """Admit this request, or raise the 429 that says why — CONTRACT-3 §12.

    ATOMIC PER PROJECT, AND POLITE TO THE POOL. First the process-local
    project gate (no connection held while waiting; a bounded wait, then
    429). Then ONE pooled connection and ONE transaction:
    `db.lock_api_project_usage`, the database clock, the window read, the
    decision, and — on admission — the request counter, the estimated input
    tokens and the reserved output tokens added to both ledgers. Nothing
    inside that block checks out a second connection.
    The lock is released by the COMMIT, after the bump is durable, so the
    next request for the project reads a window that already contains this
    one. Requests for different projects do not wait for each other.

    `kind` is one of `sync`, `stream`, `background` (a generation: every
    limit applies) or `read` (an authenticated GET: counts one request
    against RPM, spends no tokens). An unknown kind is a programming error and
    raises ValueError rather than silently skipping limits.

    ON REFUSAL, NOTHING IS RESERVED and `api_usage_daily.rate_limited` is
    incremented, in the same transaction. That counter is observability, not
    quota: a refused request must never consume the allowance it was refused
    for.

    Concurrency is NOT checked here; it is a slot held for the duration of
    the work — `concurrency_slot()` / `take_slot()`, taken after this returns.

    WITH THE LIMITS OFF (PUBLIC_API_ENFORCE_LIMITS=false, the default since the
    owner decision of 2026-09-13) the same kinds are validated and the same
    rows are written, and nothing is decided: see `_record_unenforced`.
    """
    if kind not in RESERVE_KINDS:
        raise ValueError(f"unknown reservation kind {kind!r}")
    if now is not None:
        _now(now)  # a naive datetime is refused before any lock is taken
    generating = kind in GENERATING_KINDS
    wanted_input = max(0, int(estimated_input_tokens or 0))
    reserved_input = wanted_input if generating else 0
    reserved_output = _output_charge(caller, max_output_tokens) if generating else 0

    if not limits_enforced():
        return _record_unenforced(
            caller,
            kind=kind,
            reserved_input=reserved_input,
            reserved_output=reserved_output,
            now=now,
        )

    with _project_gate(caller.project_id):
        with db.connection() as con:
            db.lock_api_project_usage(con, caller.project_id)
            # THE MOMENT IS TAKEN HERE, UNDER THE LOCK — never before the
            # checkout (see "ONE CLOCK" in the module docstring).
            moment = _now(now) if now is not None else _database_now(con)
            bucket = _bucket(moment)
            day = moment.date()
            window = _read_window_on(con, caller, moment)
            refusal = _decide(
                caller, kind, window, wanted_input, moment, output_charge=reserved_output
            )
            if refusal is None:
                con.execute(
                    _UPSERT_MINUTE_SQL,
                    (
                        caller.project_id, caller.key_id, bucket, 1,
                        reserved_input, reserved_output,
                    ),
                )
                con.execute(
                    _UPSERT_DAILY_SQL,
                    (caller.project_id, day, 1, reserved_input, reserved_output, 0, 0),
                )
            else:
                con.execute(
                    _UPSERT_DAILY_SQL, (caller.project_id, day, 0, 0, 0, 0, 1)
                )

    if refusal is not None:
        factory, seconds, what = refusal
        log.info(
            "api key %s refused on %s (project %s)",
            caller.public_id or caller.key_id,
            what,
            caller.project_id,
        )
        raise factory(_jittered(seconds))

    limits = caller.limits
    remaining = limits.project_rpm_limit - window.requests - 1
    if limits.key_rpm is not None:
        remaining = min(remaining, limits.key_rpm - window.key_requests - 1)
    return Reservation(
        caller=caller,
        at=moment,
        window=window,
        requests_remaining=max(0, int(math.floor(remaining))),
        window_seconds=int(math.ceil(WINDOW_SECONDS - _seconds_into_minute(moment))),
        kind=kind,
        reserved_input_tokens=reserved_input,
        reserved_output_tokens=reserved_output,
    )


def _record_unenforced(
    caller: ApiCaller,
    *,
    kind: str,
    reserved_input: int,
    reserved_output: int,
    now: Optional[datetime],
) -> Reservation:
    """Count this request in both ledgers and admit it — no decision at all.

    THE LIMITS ARE OFF (owner decision 2026-09-13), BUT USAGE IS NOT. The
    request counter, the input estimate and the output reservation are written
    exactly as an enforced admission writes them, in ONE transaction on ONE
    connection, and settled the same way by `record_usage(reservation=…)`, so
    the console's usage page, the request logs and `/v1/usage` are unchanged.

    WHY NEITHER LOCK IS TAKEN. The process gate and the advisory lock exist so
    that concurrent DECISIONS see each other's reservations; with no decision
    there is nothing to serialise, and each upsert is already atomic on its own
    row. Keeping the gate would also keep its one refusal — a 429 when a
    project's gate is busy for longer than the bounded wait — which is exactly
    the limit the owner removed. Nothing is ever raised here but a database
    error.
    """
    with db.connection() as con:
        moment = _now(now) if now is not None else _database_now(con)
        con.execute(
            _UPSERT_MINUTE_SQL,
            (
                caller.project_id, caller.key_id, _bucket(moment), 1,
                reserved_input, reserved_output,
            ),
        )
        con.execute(
            _UPSERT_DAILY_SQL,
            (caller.project_id, moment.date(), 1, reserved_input, reserved_output, 0, 0),
        )
    return Reservation(
        caller=caller,
        at=moment,
        window=None,
        requests_remaining=None,
        window_seconds=WINDOW_SECONDS,
        kind=kind,
        reserved_input_tokens=reserved_input,
        reserved_output_tokens=reserved_output,
        enforced=False,
    )


def check_and_reserve(
    caller: ApiCaller,
    estimated_input_tokens: int = 0,
    *,
    now: Optional[datetime] = None,
    kind: str = "sync",
    max_output_tokens: Optional[int] = None,
) -> Reservation:
    """The wave-1 name, kept so a route not yet moved to `reserve()` still
    goes through the atomic gate rather than failing to import. New code
    calls `reserve(caller, kind=…, estimated_input_tokens=…, max_output_tokens=…)`."""
    return reserve(
        caller,
        kind=kind,
        estimated_input_tokens=estimated_input_tokens,
        max_output_tokens=max_output_tokens,
        now=now,
    )


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

#: The kinds that hold a concurrency slot. ONE counter per project covers all
#: three (2026-09-13, wave-2 review: sync and stream were counted per key and
#: background in a separate per-project dict in `background.py`, so a project
#: at max_concurrency=4 could run 4 × keys + 4). A background job holds its
#: slot for the whole life of the job, not just the 202.
SLOT_KINDS = frozenset({"sync", "stream", "background"})

#: In-flight generations per PROJECT id — the ceiling CONTRACT-3 §12 names.
_in_flight: Dict[str, int] = {}
#: In-flight generations per KEY id — only consulted for a key that tightens
#: its own share with `api_keys.max_concurrency`.
_key_in_flight: Dict[str, int] = {}
#: Per (project, kind), for the console and the tests. Never a limit.
_kind_in_flight: Dict[tuple, int] = {}
_in_flight_lock = threading.Lock()

#: SCHEMA-V34 HAS NO DURABLE IN-FLIGHT ROW, which §12 also mentions. That half
#: is not implemented and is recorded rather than faked: a durable counter only
#: ever incremented in memory is worse than none, because a crash leaks slots
#: that nothing ever releases.


def in_flight(caller: ApiCaller, kind: Optional[str] = None) -> int:
    """How many generations this caller's PROJECT is running right now (or, with
    `kind`, how many of that kind)."""
    with _in_flight_lock:
        if kind is None:
            return int(_in_flight.get(caller.project_id, 0))
        return int(_kind_in_flight.get((caller.project_id, kind), 0))


def key_in_flight(caller: ApiCaller) -> int:
    with _in_flight_lock:
        return int(_key_in_flight.get(caller.key_id, 0))


def reset_concurrency() -> None:
    """Forget every in-flight slot. Tests only."""
    with _in_flight_lock:
        _in_flight.clear()
        _key_in_flight.clear()
        _kind_in_flight.clear()


def _decrement(table: Dict[Any, int], key: Any) -> None:
    left = int(table.get(key, 1)) - 1
    if left > 0:
        table[key] = left
    else:
        table.pop(key, None)


class SlotLease:
    """One held concurrency slot. `release()` is idempotent.

    An object rather than only a context manager because a streaming response
    and a background job are both taken in the handler (so the 429 can still
    be the status line) and released somewhere else entirely — the end of a
    body iterator, the `finally` of a detached task. A double release from two
    such cleanup paths must not hand a project a slot it never had, so the
    second call is a no-op.

    THE HOLDER MUST KEEP THE LEASE AND RELEASE IT IN A `finally`. There is no
    release-on-garbage-collection: a lease dropped early would hand back a
    slot whose generation is still running, and the limit is the property
    that matters here.
    """

    __slots__ = ("project_id", "key_id", "kind", "taken", "_released", "_lock")

    def __init__(self, project_id: str, key_id: str, kind: str, taken: int) -> None:
        self.project_id = project_id
        self.key_id = key_id
        self.kind = kind
        self.taken = taken
        self._released = False
        self._lock = threading.Lock()

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        with _in_flight_lock:
            _decrement(_in_flight, self.project_id)
            _decrement(_key_in_flight, self.key_id)
            _decrement(_kind_in_flight, (self.project_id, self.kind))

    # Context-manager form, so `with take_slot(...)` reads like the old API.
    def __enter__(self) -> "SlotLease":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def take_slot(caller: ApiCaller, kind: str) -> SlotLease:
    """Take one of the PROJECT's concurrency slots, or raise
    `429 concurrency_limit_exceeded`. Non-blocking.

    Checked atomically under one lock: the project's in-flight count against
    the project's ceiling, then — only for a key that tightens its own share —
    the key's in-flight count against the key's. A limit of 0 refuses every
    request (zero means zero; the first cut floored it at 1).

    WITH THE LIMITS OFF (owner decision 2026-09-13) the slot is still taken
    and counted — the console and `in_flight()` read the counter, and the
    lease is released the same way — but it is never refused. The engine's
    own shared admission queue still stands behind it.
    """
    if kind not in SLOT_KINDS:
        raise ValueError(f"unknown concurrency kind {kind!r}")
    enforced = limits_enforced()
    limits = caller.limits
    project_limit = max(0, int(limits.project_concurrency_limit))
    key_limit = limits.key_max_concurrency
    with _in_flight_lock:
        held = int(_in_flight.get(caller.project_id, 0))
        key_held = int(_key_in_flight.get(caller.key_id, 0))
        if enforced and (held >= project_limit or (
            key_limit is not None and key_held >= max(0, int(key_limit))
        )):
            # One second: a slot is freed by a request FINISHING, and nothing
            # about the window tells us when that is.
            raise errors.concurrency_limit_exceeded(_jittered(MIN_RETRY_AFTER))
        _in_flight[caller.project_id] = held + 1
        _key_in_flight[caller.key_id] = key_held + 1
        kind_key = (caller.project_id, kind)
        _kind_in_flight[kind_key] = int(_kind_in_flight.get(kind_key, 0)) + 1
        taken = held + 1
    return SlotLease(caller.project_id, caller.key_id, kind, taken)


@contextmanager
def concurrency_slot(caller: ApiCaller, kind: str = "sync") -> Iterator[int]:
    """Hold one of this PROJECT's concurrency slots for the body of the `with`.

    Non-blocking: over the limit is an immediate
    `429 concurrency_limit_exceeded`, never a queue — CONTRACT-3 §9 gives the
    caller a `Retry-After` so the decision to wait is theirs.

    THE SLOT IS RELEASED IN A `finally`: an exception mid-stream, a client
    disconnect, a cancelled task must all give it back, or a project's
    concurrency ratchets down to zero over a day of ordinary errors.
    """
    lease = take_slot(caller, kind)
    try:
        yield lease.taken
    finally:
        lease.release()


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def record_usage(
    caller: ApiCaller,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    status: str = "completed",
    *,
    now: Optional[datetime] = None,
    reservation: Optional[Reservation] = None,
) -> None:
    """Write this request's token spend to both ledgers. ONCE, at the end.

    `None` means the engine did not measure. On the wire that is `null`
    (CONTRACT-3 §9); in the ledger it is 0 — or, when a `reservation` is
    given, the reserved ESTIMATE stays counted, because a gate that forgot
    tokens it could not measure would be an under-charge nobody authorised.

    WITH A RESERVATION the input estimate AND the output reservation added at
    admission are taken back out of the bucket and day they were added to
    (never below zero) and the measured counts are added to the current ones,
    in one transaction on one connection. A reservation is settled at most
    once; a second call is a no-op. An unmeasured output (`None`) keeps its
    reservation, for the same reason an unmeasured input does.

    THE REQUEST COUNTER IS NOT TOUCHED HERE. `reserve` already counted this
    request when it admitted it.

    `status` is the terminal state. Anything that is not `completed` or
    `cancelled` increments `api_usage_daily.errors`.
    """
    if reservation is not None and not reservation._claim_settlement():
        return
    if now is not None:
        _now(now)
    reserved = int(reservation.reserved_input_tokens) if reservation is not None else 0
    reserved_out = (
        int(getattr(reservation, "reserved_output_tokens", 0) or 0)
        if reservation is not None
        else 0
    )
    if input_tokens is None and reserved:
        spent_in = reserved
    else:
        spent_in = max(0, int(input_tokens or 0))
    if output_tokens is None and reserved_out:
        spent_out = reserved_out
    else:
        spent_out = max(0, int(output_tokens or 0))
    failed = str(status or "").strip().lower() not in {"completed", "cancelled"}

    with db.connection() as con:
        # The same clock `reserve` used, so a measured count lands in the
        # bucket the database calls "now", not the one this host does.
        moment = _now(now) if now is not None else _database_now(con)
        # EVERY MINUTE ROW BEFORE ANY DAILY ROW (2026-09-13, with the switch
        # to unlimited). The first cut went minute(reserved) → daily →
        # minute(now) → daily, while `reserve` locks minute(now) → daily. A
        # settlement crossing a minute boundary and a reservation landing in
        # the new minute then held one row each and waited for the other —
        # a deadlock PostgreSQL breaks by failing one of them, and one that
        # unlimited admission (no gate serialising a burst) makes likelier at
        # every minute boundary. One order, no cycle.
        if reserved or reserved_out:
            con.execute(
                "UPDATE api_usage_minute "
                "   SET input_tokens = GREATEST(0, input_tokens - %s), "
                "       output_tokens = GREATEST(0, output_tokens - %s) "
                " WHERE project_id = %s AND key_id = %s AND bucket = %s",
                (
                    reserved, reserved_out,
                    caller.project_id, caller.key_id, _bucket(reservation.at),
                ),
            )
        con.execute(
            _UPSERT_MINUTE_SQL,
            (caller.project_id, caller.key_id, _bucket(moment), 0, spent_in, spent_out),
        )
        if reserved or reserved_out:
            con.execute(
                "UPDATE api_usage_daily "
                "   SET input_tokens = GREATEST(0, input_tokens - %s), "
                "       output_tokens = GREATEST(0, output_tokens - %s) "
                " WHERE project_id = %s AND day = %s",
                (reserved, reserved_out, caller.project_id, reservation.at.date()),
            )
        con.execute(
            _UPSERT_DAILY_SQL,
            (caller.project_id, moment.date(), 0, spent_in, spent_out, 1 if failed else 0, 0),
        )


# ---------------------------------------------------------------------------
# The headers
# ---------------------------------------------------------------------------


def limit_headers(
    caller: ApiCaller,
    *,
    window: Optional[WindowState] = None,
    at: Optional[datetime] = None,
    remaining: Optional[int] = None,
) -> Dict[str, str]:
    """`RateLimit` and `RateLimit-Policy`, draft-ietf-httpapi-ratelimit-headers-11.

    THE DRAFT REVISION IS PINNED IN THIS DOCSTRING ON PURPOSE. The spec is an
    Internet-Draft and has already REPLACED its own field names once (the
    removed `RateLimit-Limit` / `-Remaining` / `-Reset` triple). Draft-11
    defines exactly two fields with Structured-Fields parameters:

        RateLimit-Policy: "requests";q=60;w=60, "concurrency";q=4;qu="concurrent-requests"
        RateLimit: "requests";r=41;t=23

    `q` is the tightest ceiling that applies to THIS key (the project's, or
    the key's own when it tightens); `r` is the smaller of the project's and
    the key's remainder.

    WHY THE TOKEN QUOTAS ARE NOT ADVERTISED. `qu` has three registered units:
    `requests`, `content-bytes`, `concurrent-requests`. LLM tokens are none of
    them; the token limits are documented in `/docs` and still enforced.

    NOTHING AT ALL WHEN THE LIMITS ARE OFF (owner decision 2026-09-13). A
    `RateLimit-Policy` of `q=60` on an API that admits the 61st request is a
    false statement a client would throttle itself by. This is the one place
    both fields are built, so every route — and every refusal path — inherits
    the omission.
    """
    if not limits_enforced():
        return {}
    moment = _now(at)
    limits = caller.limits
    if remaining is None:
        state = window if window is not None else read_window(caller, now=moment)
        figure = limits.project_rpm_limit - state.requests
        if limits.key_rpm is not None:
            figure = min(figure, limits.key_rpm - state.key_requests)
        remaining = int(math.floor(figure))
    remaining = max(0, int(remaining))
    effective = int(math.ceil(WINDOW_SECONDS - _seconds_into_minute(moment))) or WINDOW_SECONDS
    return {
        "RateLimit-Policy": (
            f'"requests";q={int(limits.rpm)};w={WINDOW_SECONDS}, '
            f'"concurrency";q={int(limits.max_concurrency)};qu="concurrent-requests"'
        ),
        "RateLimit": f'"requests";r={remaining};t={effective}',
    }


__all__ = [
    "DAY_SECONDS",
    "GENERATING_KINDS",
    "MIN_RETRY_AFTER",
    "RESERVE_KINDS",
    "Reservation",
    "SLOT_KINDS",
    "SlotLease",
    "WINDOW_SECONDS",
    "WindowState",
    "check_and_reserve",
    "concurrency_slot",
    "in_flight",
    "key_in_flight",
    "limit_headers",
    "limits_enforced",
    "limits_for",
    "read_window",
    "record_usage",
    "reserve",
    "reset_concurrency",
    "take_slot",
]
