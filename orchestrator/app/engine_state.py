"""The engine controller's verdict, as the orchestrator sees it (CONTRACT §8.1).

WHY THIS EXISTS. On 2026-09-11 at 22:15:50Z the worker rank of the TP=2
engine died. For the next five minutes the head answered `/health`,
`/v1/models` and `/metrics` with 200 — Prometheus said `up == 1`, the token
counters simply stopped moving — while every completion hung on a collective
that was never going to complete. Nothing inside this process could tell
"slow" from "dead": the wait-and-retry layer (app/resilience.py) trusts
`/health`, and `/health` was lying.

The engine controller (compose service `engine-controller`, CONTRACT §6)
is the one component that runs a REAL completion against the engine on a
timer and watches both ranks. It publishes a nine-state verdict at
`GET /state`. This module polls that document and keeps the latest
snapshot in memory so that:

- the circuit breaker (app/breaker.py) can OPEN at once when the controller
  says WEDGED / RECOVERING / DOWN / STARTING — before the first request of
  this process has had a chance to fail into it;
- a generation queued for the engine (app/continuity.py) can sleep on an
  asyncio.Event and be woken the moment the controller says READY, instead
  of polling a port that is known to be dead;
- `/health` and the `llm_engine_state_code` gauge can show what the
  orchestrator believes about the engine, and how old that belief is.

THE ONE RULE. An unreachable controller, a malformed document or a snapshot
that is too old is `unknown`, and unknown NEVER opens the breaker. The
controller is a monitoring component; if it is down the engine may be
perfectly fine, and a chat must not be queued because a sidecar restarted.
Only observed failures — a bad state the controller actually reported, or
real calls that actually failed — do.

WHAT "TOO OLD" MEANS (v2). Two clocks: how long ago THIS process read the
document, and how old the document already was when it was read (its own
`generated_at` against the wall clock — the controller runs on the same
host, so the clocks agree). A controller that keeps serving a document it
stopped refreshing (its poll loop hung, its clock froze) is as unknown as
one that stopped answering. Both ages add up against `stale_after_s()`,
which follows the poll interval so a slower poll cannot make every read
stale before the next one arrives.

Nothing here performs network I/O at import time; the poller is started
from the app lifespan and stopped with it.
"""
from __future__ import annotations

import asyncio
import logging
import time
import weakref
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from . import metrics
from .config import settings

log = logging.getLogger(__name__)

#: The nine top-level states and their stable codes (CONTRACT §2 v2).
STATE_CODES = {
    "MONITORING_UNKNOWN": 0,
    "STARTING": 1,
    "READY": 2,
    "BUSY": 3,
    "DEGRADED": 4,
    "WEDGED": 5,
    "RECOVERING": 6,
    "QUEUEING": 7,
    "DOWN": 8,
}

#: The v1 name of code 7, still accepted from an older controller build so a
#: mixed deploy does not read as "unknown" (the code is what is stable).
_LEGACY_STATE_NAMES = {"FALLBACK_ACTIVE": "QUEUEING"}

#: States in which the controller has established that the primary is not
#: serving: the breaker opens on them without waiting for a failure of its
#: own (CONTRACT §8.2). MONITORING_UNKNOWN is deliberately absent — it is
#: "cannot observe", never "down". STARTING is in the set but qualified: see
#: `external_open`.
OPENS_BREAKER = frozenset({"STARTING", "WEDGED", "RECOVERING", "DOWN"})

#: States in which the controller has proven the primary serves (a real
#: completion within the last two probe intervals): what a queued
#: generation waits for. DEGRADED is "the canary still succeeds but a
#: non-critical signal failed" — the model answers.
SERVING = frozenset({"READY", "BUSY", "DEGRADED"})

#: A snapshot older than this is unknown (CONTRACT §8.1): three missed polls
#: at the default 5 s interval. The effective value follows the configured
#: poll interval — see `stale_after_s()`; this is the floor.
STALE_AFTER_S = 15.0

#: The controller reports STARTING for at least one tick after ITS OWN
#: restart — no canary has succeeded "since the head started" as far as the
#: new controller process knows — while the head may have been serving for
#: hours. Opening the breaker on that would queue every chat for the 10-20 s
#: it takes the controller to run its first canary, on every deploy of the
#: controller. So STARTING opens the breaker only when the document also
#: says the head really is starting: an incident is open, the head
#: container is not running or its engine process is gone, or the container
#: started within the cold-start budget (measured 5 m 20 s; the budget is
#: the controller's COLD_START_BUDGET_S). A document that carries none of
#: this is taken at its word.
STARTING_HEAD_AGE_S = 900.0

#: The gauge value for "unreachable or stale" (CONTRACT §7.2).
UNKNOWN_CODE = -1

#: While the verdict stays unknown for this long, one WARNING per period
#: says so — a controller that is misconfigured (single-node mode without
#: an ENGINE_CONTROLLER_URL the orchestrator can reach) is otherwise silent
#: forever, because unknown never changes any decision.
_UNKNOWN_NAG_S = 3600.0


@dataclass(frozen=True)
class EngineSnapshot:
    """One successful read of the controller's `/state` document.

    `observed_at` is this process's monotonic clock at the time of the read;
    `generated_at` is the controller's own wall-clock stamp and
    `generated_lag_s` how old the document already was when it was read.
    Age — what freshness is judged by — is the sum of the two.
    """

    state: str
    state_code: int
    reason: str
    primary_ready: bool
    router_available: Optional[bool]
    generated_at: float
    generated_lag_s: float
    observed_at: float
    recovery_step: str
    incident_id: Optional[str]
    #: The head container as the controller saw it (CONTRACT §6.2 signals).
    head_running: Optional[bool]
    head_engine_alive: Optional[bool]
    head_started_at: Optional[float]
    #: The controller's engine sample: what vLLM's /metrics said about load.
    requests_running: Optional[int]
    requests_waiting: Optional[int]
    #: The controller's recovery budget (`recovery.budget`) and how much of it
    #: the rolling window has used (`recovery.attempts_in_window`); None when
    #: the document does not carry them (an older controller build). Read by
    #: `recovery_headroom` (no-timeout /v1, 2026-09-13: POISON QUARANTINE).
    recovery_budget: Optional[int] = None
    recovery_attempts_in_window: Optional[int] = None
    #: Seconds the engine's token counters have not moved while requests ran
    #: (`signals.engine.frozen_seconds`); None when absent.
    frozen_seconds: Optional[float] = None
    #: The controller's wall-clock stamp of its engine sample
    #: (`signals.engine.observed_at`). It stops moving while /metrics is
    #: unavailable even though `generated_at` keeps advancing, so a consumer
    #: can tell a CURRENT sample from the last good one (assembler,
    #: 2026-09-14: the interface publicapi/liveness.py's 'lost' rule asked for).
    engine_observed_at: Optional[float] = None

    def age_s(self, now: Optional[float] = None) -> float:
        read_age = max(0.0, (time.monotonic() if now is None else now) - self.observed_at)
        return read_age + self.generated_lag_s

    def serving(self) -> bool:
        return self.state in SERVING


_snapshot: Optional[EngineSnapshot] = None
#: Why the last poll produced no snapshot ("" after a good poll): shown in
#: /health, never used for a decision.
_last_error: str = ""
_last_attempt_at: Optional[float] = None
_task: Optional[asyncio.Task] = None
#: Whether the last published verdict was "serving" — the edge the READY
#: listeners fire on. None until the first verdict, and None again after
#: ANY gap in which the engine was not proven serving: a bad state, a
#: MONITORING_UNKNOWN, an unreachable or stale controller. The engine may
#: have died and been restarted inside such a gap (the breaker then opened
#: from observed failures and rows were parked), so the next fresh SERVING
#: snapshot is an edge and the resume sweep runs (round-2 review,
#: engine_state.py:356). A spurious sweep costs one indexed query.
_was_serving: Optional[bool] = None
#: Called (synchronously, on the poller's loop) when the verdict turns from
#: not-serving/unknown to serving: the resume sweep (app/continuity.py).
_ready_listeners: List[Callable[[], None]] = []


def stale_after_s() -> float:
    """Three missed polls, never less than the contract's 15 s."""
    return max(STALE_AFTER_S, 3.0 * float(settings.engine_state_poll_s))


# ---------------------------------------------------------------------------
# Reading the snapshot
# ---------------------------------------------------------------------------


def snapshot() -> Optional[EngineSnapshot]:
    """The latest good read, fresh or not. `unknown()` says whether to trust it."""
    return _snapshot


def unknown(now: Optional[float] = None) -> bool:
    """True when there is nothing fresh to act on.

    No read yet, an unreachable controller, a document that was already old
    when it was read, or a read older than `stale_after_s()` — all the same
    thing to a caller: no verdict.
    """
    snap = _snapshot
    return snap is None or snap.age_s(now) > stale_after_s()


def serving(now: Optional[float] = None) -> Optional[bool]:
    """Has the controller proven the primary serves? True / False when the
    verdict is fresh, None when it is unknown (including MONITORING_UNKNOWN,
    which is "cannot see", not "no")."""
    if unknown(now):
        return None
    state = _snapshot.state  # type: ignore[union-attr]
    if state == "MONITORING_UNKNOWN":
        return None
    return state in SERVING


def _starting_is_real(snap: EngineSnapshot) -> bool:
    """See STARTING_HEAD_AGE_S: does the document say the head is starting,
    or only that the controller has not proven it yet?"""
    if snap.incident_id:
        return True
    if snap.head_running is False or snap.head_engine_alive is False:
        return True
    if snap.head_started_at is None or snap.generated_at <= 0:
        return True  # nothing to qualify it with: taken at its word
    return (snap.generated_at - snap.head_started_at) <= STARTING_HEAD_AGE_S


def external_open(now: Optional[float] = None) -> Optional[str]:
    """The controller state that demands an OPEN breaker, or None.

    This is the breaker's external input (CONTRACT §8.2). It answers with a
    state NAME so the breaker's transition log can say which one, and it
    answers None for everything that is not a fresh, observed bad state —
    including MONITORING_UNKNOWN, a stale snapshot, and a STARTING that is
    only the controller's own cold start.
    """
    if unknown(now):
        return None
    snap = _snapshot
    assert snap is not None  # unknown() checked
    if snap.state not in OPENS_BREAKER:
        return None
    if snap.state == "STARTING" and not _starting_is_real(snap):
        return None
    return snap.state


def engine_load(now: Optional[float] = None) -> Optional[Dict[str, float]]:
    """The controller's last engine sample — `requests_running`,
    `requests_waiting` and the snapshot's age — or None when the verdict is
    unknown or the document carried no sample. app/admission.py reads it to
    let a long prompt wait for an idle engine without touching /metrics."""
    if unknown(now):
        return None
    snap = _snapshot
    assert snap is not None
    if snap.requests_running is None:
        return None
    load = {
        "requests_running": float(snap.requests_running),
        "requests_waiting": float(snap.requests_waiting or 0),
        "age_s": snap.age_s(now),
        # The controller's own stamp: two samples with the same value are ONE
        # observation, which the /v1 'lost' rule must not count twice.
        "generated_at": float(snap.generated_at),
    }
    if snap.engine_observed_at is not None:
        # When the SAMPLE was taken (same wall clock as generated_at); absent
        # from a controller build that does not publish it.
        load["sample_observed_at"] = float(snap.engine_observed_at)
    return load


def describe(now: Optional[float] = None) -> dict:
    """What /health shows: the snapshot, its age and the last poll error.

    In-memory only — /health must stay cheap, and the poller already did
    the network round trip.
    """
    snap = _snapshot
    out: dict = {
        "url": settings.engine_controller_url or None,
        "unknown": unknown(now),
        "stale_after_s": stale_after_s(),
    }
    if snap is None:
        out["state"] = None
        out["state_code"] = UNKNOWN_CODE
    else:
        out.update(
            state=snap.state,
            state_code=snap.state_code,
            reason=snap.reason,
            primary_ready=snap.primary_ready,
            router_available=snap.router_available,
            age_s=round(snap.age_s(now), 1),
            generated_at=snap.generated_at,
            generated_lag_s=round(snap.generated_lag_s, 1),
            recovery_step=snap.recovery_step,
            incident_id=snap.incident_id,
            requests_running=snap.requests_running,
            requests_waiting=snap.requests_waiting,
            recovery_budget=snap.recovery_budget,
            recovery_attempts_in_window=snap.recovery_attempts_in_window,
            proven_not_serving=proven_not_serving(now),
        )
    if _last_attempt_at is not None:
        out["last_poll_age_s"] = round(max(0.0, (time.monotonic() if now is None else now) - _last_attempt_at), 1)
    if _last_error:
        out["last_error"] = _last_error
    return out


# ---------------------------------------------------------------------------
# The READY event — what a queued generation sleeps on
# ---------------------------------------------------------------------------
#
# One asyncio.Event per event loop, like the breaker registry: an Event is
# bound to the loop that first waits on it, and the test suite runs a loop
# per test. Set while the last verdict was "serving", cleared while it was
# a bad state; left alone on MONITORING_UNKNOWN and on a stale read (no
# verdict changes nothing — a waiter re-asks the breaker on every tick
# anyway, so a set event never admits anyone by itself).

_events: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Event]" = weakref.WeakKeyDictionary()


def ready_event() -> asyncio.Event:
    """The current loop's READY event, created (and initialised from the
    snapshot) on first use."""
    loop = asyncio.get_running_loop()
    event = _events.get(loop)
    if event is None:
        event = _events[loop] = asyncio.Event()
        if serving() is True:
            event.set()
    return event


async def wait_ready(timeout: float) -> bool:
    """Sleep until the controller says the primary serves, or `timeout`.
    True when the event is set (now or within the timeout)."""
    event = ready_event()
    if event.is_set():
        return True
    try:
        async with asyncio.timeout(max(0.0, float(timeout))):
            await event.wait()
    except asyncio.TimeoutError:
        return False
    return True


def on_ready(fn: Callable[[], None]) -> None:
    """Register a listener for the not-serving → serving edge. Idempotent."""
    if fn not in _ready_listeners:
        _ready_listeners.append(fn)


def _publish_event(snap: Optional[EngineSnapshot]) -> None:
    """Set or clear every loop's event from the snapshot, and fire the edge
    listeners when the verdict turns to serving."""
    global _was_serving
    if snap is None:
        return
    if snap.state in SERVING:
        now_serving: Optional[bool] = True
    elif snap.state in OPENS_BREAKER:
        now_serving = False
    else:
        now_serving = None  # MONITORING_UNKNOWN, QUEUEING: no verdict either way
    if now_serving is None:
        # No verdict: the event is left alone, but the engine is no longer
        # PROVEN serving — the next fresh SERVING snapshot is an edge.
        _was_serving = None
        return
    for event in list(_events.values()):
        if now_serving:
            event.set()
        else:
            event.clear()
    edge = now_serving and not _was_serving
    _was_serving = now_serving
    if edge:
        for fn in list(_ready_listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001 — a listener must never break the poller
                log.exception("llm.engine_state READY listener failed")


def _note_unknown() -> None:
    """The verdict is unknown (unreachable or stale controller): whatever
    was proven before is no longer proven, so the next fresh SERVING
    snapshot fires the READY edge (see `_was_serving`)."""
    global _was_serving
    _was_serving = None


# ---------------------------------------------------------------------------
# Evidence for a caller that must never cut a healthy silent prefill
# (no-timeout /v1 design, revision 2, 2026-09-13: LIVENESS GUARD)
# ---------------------------------------------------------------------------
#
# WHY A SECOND READING OF THE SAME VERDICT. `external_open` answers the chat
# BREAKER's question — "should a new chat turn be queued rather than sent?" —
# and a false positive there costs a person seconds. The public API asks a
# different question of the same document: "may I CUT a generation that has
# been silent for twenty minutes?" A false positive there throws away a 950K
# prefill (~800 s at the measured ~1,190 tok/s) and pays it again. So the /v1
# reading is stricter about what counts as proven:
#
# - unknown stays unknown: MONITORING_UNKNOWN, QUEUEING, a stale or
#   unreachable controller, a STARTING that is only the controller's own cold
#   start — exactly as for the breaker;
# - AND a DOWN whose reason is the controller's cold-start budget running out
#   ("cold start timeout") on a head that is alive, carries no incident and
#   has already served, is unknown too. The controller publishes that DOWN
#   900 s after IT restarted during a long prefill (controller.py, cold start
#   branch): the head was never unhealthy. The breaker's use of
#   `external_open` is deliberately NOT changed here (flagged to the
#   controller owner instead).
#
# - AND a STARTING on a head younger than STARTING_HEAD_AGE_S is unknown too
#   when the head is running, its engine process alive, there is no incident,
#   and this process has seen THIS head serve (evidence after its start). The
#   controller restarting within 15 minutes of a head (re)start — after one of
#   the 172-188 s recoveries, say — says STARTING "readiness sequence not
#   passed yet" until its own probe passes; during a silent public prefill the
#   guard read that as proven down and threw a healthy prefill away after 30 s
#   (T1 review, 2026-09-14). A head restart moves `started_at` past the
#   evidence, so a real start stays proven.
#
# "Has already served" is this process's own evidence when it has any: a
# fresh SERVING snapshot, or an engine chunk received by any llm stream, at a
# wall-clock time after the head started. A head this process watched start
# and never saw serve is a real cold-start failure and stays proven bad. With
# no evidence at all (this process started after the head's last answer) the
# document's own qualification decides, as the design states.

#: The reason prefix the controller gives its cold-start-budget DOWN.
COLD_START_TIMEOUT_REASON = "cold start timeout"

#: Monotonic and wall-clock time of the last fresh SERVING snapshot.
_serving_seen_at: Optional[float] = None
_serving_seen_wall: Optional[float] = None
#: Monotonic and wall-clock time of the last engine chunk any llm stream of
#: this process received (`note_chunk`).
_chunk_at: Optional[float] = None
_chunk_wall: Optional[float] = None
#: Since when (monotonic) the /v1 verdict has been continuously proven bad,
#: and which state it was; None while it is not.
_bad_since: Optional[float] = None
_bad_state: Optional[str] = None


def note_chunk() -> None:
    """An engine chunk arrived on some llm stream of this process.

    Called on EVERY chunk (llm.stream_chat_events), so it is two clock reads
    and two assignments — no lock, no allocation. A chunk is the strongest
    serving evidence there is: the engine produced a token for us just now.
    """
    global _chunk_at, _chunk_wall
    _chunk_at = time.monotonic()
    _chunk_wall = time.time()


def serving_evidence_at() -> Optional[float]:
    """Monotonic time of the latest serving evidence: the later of the last
    fresh SERVING snapshot and the last engine chunk. None when this process
    has seen neither."""
    seen = [t for t in (_serving_seen_at, _chunk_at) if t is not None]
    return max(seen) if seen else None


def _serving_evidence_wall() -> Optional[float]:
    seen = [t for t in (_serving_seen_wall, _chunk_wall) if t is not None]
    return max(seen) if seen else None


def _fresh_snapshot(now: Optional[float] = None) -> Optional[EngineSnapshot]:
    return None if unknown(now) else _snapshot


def head_started_at(now: Optional[float] = None) -> Optional[float]:
    """The head container's start (controller wall clock) from a FRESH
    snapshot, or None. A change between two non-None readings is a head
    restart: the liveness guard interrupts on it."""
    snap = _fresh_snapshot(now)
    return None if snap is None else snap.head_started_at


def incident_id(now: Optional[float] = None) -> Optional[str]:
    """The open incident's id from a fresh snapshot, or None."""
    snap = _fresh_snapshot(now)
    return None if snap is None else snap.incident_id


def recovery_headroom(now: Optional[float] = None) -> Optional[int]:
    """`recovery.budget − recovery.attempts_in_window` from a fresh snapshot,
    never below 0; None when the verdict is unknown or either number is
    absent. A quarantined /v1 run is dispatched only while this is at least
    PUBLIC_API_QUARANTINE_MIN_RECOVERIES, so a request that crashed the engine
    once can never spend the recoveries chat needs (design review, 2026-09-13)."""
    snap = _fresh_snapshot(now)
    if snap is None or snap.recovery_budget is None or snap.recovery_attempts_in_window is None:
        return None
    return max(0, int(snap.recovery_budget) - int(snap.recovery_attempts_in_window))


def frozen_seconds(now: Optional[float] = None) -> Optional[float]:
    """How long the engine's token counters have been frozen with requests
    running, per a fresh snapshot; None when unknown or absent."""
    snap = _fresh_snapshot(now)
    return None if snap is None else snap.frozen_seconds


def _cold_start_timeout_on_served_head(snap: EngineSnapshot) -> bool:
    """See the section comment: is this DOWN only the controller's cold-start
    budget running out on a head that is (as far as anything shows) healthy?"""
    if snap.state != "DOWN" or not snap.reason.lower().startswith(COLD_START_TIMEOUT_REASON):
        return False
    if _starting_is_real(snap):
        # An incident, a dead head or engine process, or a head younger than
        # STARTING_HEAD_AGE_S: the document itself says it is real.
        return False
    evidence = _serving_evidence_wall()
    if evidence is not None and snap.head_started_at is not None and evidence < snap.head_started_at:
        # This process watched this head start and never saw it serve.
        return False
    return True


def _controller_starting_on_served_head(snap: EngineSnapshot) -> bool:
    """See the section comment: is this STARTING (on a young head) only the
    controller's own cold start, on a head this process has seen serve since
    it started? Positive evidence only: a head or engine process the document
    does not call alive, an incident, or no head start leaves it proven."""
    if snap.state != "STARTING" or snap.incident_id:
        return False
    if snap.head_running is not True or snap.head_engine_alive is not True:
        return False
    if snap.head_started_at is None:
        return False
    evidence = _serving_evidence_wall()
    return evidence is not None and evidence > snap.head_started_at


def proven_not_serving(now: Optional[float] = None) -> Optional[str]:
    """The state name when the controller has PROVEN the primary is not
    serving, else None — `external_open` minus the cold-start-timeout DOWN on
    an old, alive head, and minus the controller's own STARTING on a young
    head this process has seen serve (section comment). Non-None only for a
    fresh WEDGED, RECOVERING, a real STARTING, or any other DOWN (worker_rank_dead,
    head_engine_dead, head_api_dead, manual, recovery budget exhausted)."""
    state = external_open(now)
    if state is None:
        return None
    snap = _snapshot
    assert snap is not None  # external_open returned a state
    if _cold_start_timeout_on_served_head(snap) or _controller_starting_on_served_head(snap):
        return None
    return state


def not_serving_since(now: Optional[float] = None) -> Optional[float]:
    """Monotonic time since which `proven_not_serving` has been non-None at
    every poll, or None when it is not now. Tracked by the poller, so "30 s
    continuously" means every read in those 30 s said so — a single unknown
    read in between restarts the count (unknown is never down)."""
    if proven_not_serving(now) is None:
        return None
    return _bad_since


def _note_verdict() -> None:
    """After every poll: move the serving-evidence clock and the proven-bad
    streak. Unknown ends a streak — it must never extend one."""
    global _serving_seen_at, _serving_seen_wall, _bad_since, _bad_state
    now = time.monotonic()
    if serving(now) is True:
        _serving_seen_at = now
        _serving_seen_wall = time.time()
    bad = proven_not_serving(now)
    if bad is None:
        _bad_since = None
        _bad_state = None
    elif _bad_since is None:
        _bad_since = now
        _bad_state = bad
    else:
        _bad_state = bad


async def wait_not_bad(
    abandon: "Optional[Callable[[], bool] | asyncio.Event]" = None,
    *,
    poll_s: Optional[float] = None,
    polls: int = 2,
) -> bool:
    """Sleep until `polls` consecutive checks, one poll interval apart, find
    the engine NOT proven bad; True then. False as soon as `abandon` (a
    callable returning True, or a set Event) says the caller left.

    No deadline, on purpose: an interrupted /v1 attempt waits here for as long
    as the engine is proven down, and the ENGINE-DOWN GRACE — counted by the
    caller from `not_serving_since` — is what ends a wait that should end.
    Unknown is not bad, so a crashlooping controller never parks a waiter.
    """
    interval = max(0.05, float(poll_s if poll_s is not None else max(0.5, float(settings.engine_state_poll_s))))
    need = max(1, int(polls))
    good = 0

    def left() -> bool:
        if abandon is None:
            return False
        if isinstance(abandon, asyncio.Event):
            return abandon.is_set()
        try:
            return bool(abandon())
        except Exception:  # noqa: BLE001 — a broken probe is not an abandon
            return False

    while True:
        if left():
            return False
        if proven_not_serving() is None:
            good += 1
            if good >= need:
                return True
        else:
            good = 0
        if isinstance(abandon, asyncio.Event):
            try:
                async with asyncio.timeout(interval):
                    await abandon.wait()
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------


def _opt_bool(value: object) -> Optional[bool]:
    return None if value is None else bool(value)


def _opt_int(value: object) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: object) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_state_document(
    doc: object, *, observed_at: float, read_wall: Optional[float] = None
) -> EngineSnapshot:
    """Validate one `/state` body (CONTRACT §6.2) into a snapshot.

    Strict on the two fields a decision is made from — `state` must be one
    of the nine names, and `state_code` must agree with it — and lenient on
    the rest, which is informational. A document that fails here is treated
    exactly like an unreachable controller: unknown. `read_wall` is the wall
    clock at the read (defaults to now) and, against `generated_at`, decides
    how old the document already was; a document with no `generated_at` is
    infinitely old and therefore never fresh.
    """
    if not isinstance(doc, dict):
        raise ValueError("state document is not an object")
    state = str(doc.get("state") or "")
    state = _LEGACY_STATE_NAMES.get(state, state)
    if state not in STATE_CODES:
        raise ValueError(f"unknown engine state {state!r}")
    code = doc.get("state_code")
    if not isinstance(code, int) or isinstance(code, bool) or code != STATE_CODES[state]:
        raise ValueError(f"state_code {code!r} does not match state {state!r}")
    recovery = doc.get("recovery") if isinstance(doc.get("recovery"), dict) else {}
    incident = doc.get("incident") if isinstance(doc.get("incident"), dict) else None
    signals = doc.get("signals") if isinstance(doc.get("signals"), dict) else {}
    head = signals.get("head_container") if isinstance(signals.get("head_container"), dict) else {}
    engine = signals.get("engine") if isinstance(signals.get("engine"), dict) else {}
    generated_at = _opt_float(doc.get("generated_at")) or 0.0
    wall = time.time() if read_wall is None else float(read_wall)
    lag = max(0.0, wall - generated_at) if generated_at > 0 else float("inf")
    return EngineSnapshot(
        state=state,
        state_code=code,
        reason=str(doc.get("reason") or "")[:200],
        primary_ready=bool(doc.get("primary_ready", False)),
        router_available=_opt_bool(doc.get("router_available")),
        generated_at=generated_at,
        generated_lag_s=lag,
        observed_at=observed_at,
        recovery_step=str(recovery.get("step") or "idle")[:40],
        incident_id=str(incident["id"])[:40] if incident and incident.get("id") else None,
        head_running=_opt_bool(head.get("running")),
        head_engine_alive=_opt_bool(head.get("engine_process_alive")),
        head_started_at=_opt_float(head.get("started_at")),
        requests_running=_opt_int(engine.get("requests_running")),
        requests_waiting=_opt_int(engine.get("requests_waiting")),
        recovery_budget=_opt_int(recovery.get("budget")),
        recovery_attempts_in_window=_opt_int(recovery.get("attempts_in_window")),
        frozen_seconds=_opt_float(engine.get("frozen_seconds")),
        engine_observed_at=_opt_float(engine.get("observed_at")),
    )


def _publish(snap: Optional[EngineSnapshot]) -> None:
    metrics.set_gauge(
        "llm_engine_state_code",
        UNKNOWN_CODE if snap is None else snap.state_code,
        "Engine state the orchestrator last read from the controller; -1 when unreachable or stale.",
    )


def _record(snap: Optional[EngineSnapshot], error: str) -> None:
    """Store one poll's outcome and publish the gauge and the READY event.
    Logs only on CHANGE: the poller runs every few seconds for the life of
    the process, and a controller that is down for an hour must not write
    720 identical lines."""
    global _snapshot, _last_error
    previous = _snapshot
    was_error = _last_error
    if snap is not None:
        _snapshot = snap
        _last_error = ""
        if previous is None or previous.state != snap.state or was_error:
            log.info(
                "llm.engine_state controller says %s (code %d): %s",
                snap.state, snap.state_code, snap.reason,
            )
        if snap.age_s() > stale_after_s():
            # Read fine, but the document itself is old: the controller is
            # answering without observing. Said once per change of error.
            _last_error = f"document {snap.generated_lag_s:.0f}s old at read"
            if _last_error != was_error:
                log.warning("llm.engine_state controller document is stale: %s", _last_error)
    else:
        _last_error = error
        if error != was_error:
            log.warning("llm.engine_state controller unreachable: %s", error)
    fresh = not unknown()
    _publish(_snapshot if fresh else None)
    if fresh:
        _publish_event(_snapshot)
    else:
        _note_unknown()
    _note_verdict()


async def poll_once(client=None) -> Optional[EngineSnapshot]:
    """One GET of the controller's /state. Never raises.

    Returns the fresh snapshot, or None when the controller could not be
    read (the reason is kept for /health). A body that is not a valid state
    document counts as unreachable — a decision must not be made from a
    document that failed validation.
    """
    global _last_attempt_at
    import httpx
    from .core.net import shared_ssl_context

    url = settings.engine_controller_url
    _last_attempt_at = time.monotonic()
    if not url:
        _record(None, "ENGINE_CONTROLLER_URL is not configured")
        return None
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=float(settings.health_probe_timeout), verify=shared_ssl_context())
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            _record(None, f"HTTP {resp.status_code}")
            return None
        snap = parse_state_document(resp.json(), observed_at=time.monotonic())
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — any failure is "unknown", never a crash
        _record(None, f"{type(exc).__name__}: {str(exc)[:120]}")
        return None
    finally:
        if own_client:
            await client.aclose()
    _record(snap, "")
    return snap


async def _loop() -> None:
    """Poll forever at ENGINE_STATE_POLL_S. One client for the life of the
    loop: a fresh connection per poll against a host-network service is
    pointless churn, and the timeout is the short health-probe one."""
    import httpx
    from .core.net import shared_ssl_context

    interval = max(0.5, float(settings.engine_state_poll_s))
    unknown_since: Optional[float] = None
    last_nag = 0.0
    async with httpx.AsyncClient(timeout=float(settings.health_probe_timeout), verify=shared_ssl_context()) as client:
        while True:
            await poll_once(client)
            # The gauge must go to -1 when the controller has been silent
            # long enough, even though nothing new arrived to publish.
            if unknown():
                _publish(None)
                _note_unknown()
                now = time.monotonic()
                if unknown_since is None:
                    unknown_since = now
                elif now - unknown_since >= _UNKNOWN_NAG_S and now - last_nag >= _UNKNOWN_NAG_S:
                    last_nag = now
                    log.warning(
                        "llm.engine_state verdict unknown for %.0f min (%s): the breaker works "
                        "from observed failures alone; check ENGINE_CONTROLLER_URL",
                        (now - unknown_since) / 60.0, _last_error or "no error recorded",
                    )
            else:
                unknown_since = None
            await asyncio.sleep(interval)


def start() -> None:
    """Called from the FastAPI lifespan. Idempotent; a blank URL or a zero
    interval leaves the poller off (the breaker then works from observed
    failures alone, which is the documented degraded mode)."""
    global _task
    if not settings.engine_controller_url or float(settings.engine_state_poll_s) <= 0:
        _publish(None)
        return
    if _task is not None and not _task.done():
        return
    _publish(None)
    _task = asyncio.create_task(_loop(), name="engine-state-poller")
    log.info(
        "engine state poller started (%s every %ss)",
        settings.engine_controller_url, settings.engine_state_poll_s,
    )


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _task = None


def reset() -> None:
    """Tests only: forget every read, every event and every listener."""
    global _snapshot, _last_error, _last_attempt_at, _was_serving
    global _serving_seen_at, _serving_seen_wall, _chunk_at, _chunk_wall, _bad_since, _bad_state
    _snapshot = None
    _last_error = ""
    _last_attempt_at = None
    _was_serving = None
    _serving_seen_at = None
    _serving_seen_wall = None
    _chunk_at = None
    _chunk_wall = None
    _bad_since = None
    _bad_state = None
    _events.clear()
    _ready_listeners.clear()
    _publish(None)
