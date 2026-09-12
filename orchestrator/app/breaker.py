"""The circuit breaker in front of each model engine (CONTRACT §8.2).

WHY THIS EXISTS. app/resilience.py already survives an outage: a failed
call waits for `/health`, then retries. That was the right shape for the
outage it was built from (connection refused for thirteen minutes). It is
the wrong shape for the one on 2026-09-11 22:15Z: the worker rank died,
the head kept answering `/health` with 200, and every caller that failed
went straight back to polling a port that was up and an engine that was
not — nine requests hung until vLLM's own 300 s timeout returned 500 to
all of them at once. The layer above needed a memory: "this engine has
just failed N times; stop sending callers into it, send ONE to find out
whether it is back".

That is all a breaker is. One engine in strict one-model mode (`main`,
the TP=2 model — nothing stands in for it, CONTRACT v2 §1), three states:

    CLOSED     every call is admitted; failures are counted in a window
    OPEN       nobody is admitted; after a cooldown → HALF_OPEN
    HALF_OPEN  exactly ONE canary call is admitted; success → CLOSED,
               an opening failure → OPEN again

THE CANARY IS SETTLED BY A TOKEN, NOT BY HEADERS. vLLM sends the response
headers of a streamed completion before the engine has scheduled the
request, so a wedged head "opens" a stream just fine and then never sends
a chunk — the 2026-09-11 shape for this process's own traffic. A streaming
canary therefore proves nothing until its first body chunk arrives
(app/resilience.py settles the permit there), and a stream that dies in
its body is counted as the failure it is, before or after a token.

THE CANARY HOLD IS BOUNDED. The canary is whichever real call arrives
first; a non-streaming one can run for minutes. After
LLM_BREAKER_CANARY_HOLD_S the outstanding canary stops blocking the
others and the next caller probes too — the late outcome is then an
ordinary one, never mistaken for the canary's.

Two things open it: LLM_BREAKER_FAILURES counted failures inside
LLM_BREAKER_WINDOW_S with no success between them, or the engine
controller (app/engine_state.py) reporting the engine WEDGED, RECOVERING,
DOWN or STARTING — the "external open", which also HOLDS it open (the
cooldown does not run) until the controller stops saying so. An unknown
controller verdict does neither; only observed facts move the breaker.

WHAT COUNTS. The failure reasons are the bounded set in CONTRACT §4. Only
`connection`, `readiness`, `engine_dead`, `worker_lost` and `capacity`
(429) open the breaker. A read timeout is a generation that ran the whole
wall clock — the engine was working, just slowly — so it is counted for
the metric but never opens anything by itself; a 4xx is the caller's
fault; a cancellation is nobody's and is not even counted. A success in
CLOSED clears the window: three stray 5xx across an hour of good answers
are not an outage, three in a row inside thirty seconds are.

THE CLOCK IS INJECTABLE and every transition is one metric and one log
line (`llm.breaker engine=… from=… to=… reason=…`), so the state machine
is unit-tested end to end without an engine and an incident can be read
back from the logs and from `llm_breaker_transitions_total`.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import weakref
from collections import deque
from typing import Callable, Deque, Dict, List, Optional

from . import metrics
from .config import settings

log = logging.getLogger(__name__)

CLOSED = "CLOSED"
OPEN = "OPEN"
HALF_OPEN = "HALF_OPEN"
STATE_CODES = {CLOSED: 0, OPEN: 1, HALF_OPEN: 2}

#: The one engine a breaker exists for (metrics.py bounds the label to it).
MAIN = "main"
ENGINES = (MAIN,)

#: The bounded failure vocabulary (CONTRACT §4) and the subset that opens.
REASONS = frozenset({
    "connection", "readiness", "request_timeout", "queue_timeout",
    "engine_dead", "worker_lost", "capacity", "malformed", "cancelled",
})
OPENING_REASONS = frozenset({"connection", "readiness", "engine_dead", "worker_lost", "capacity"})

#: Transition listeners: (engine, from_state, to_state, reason). Kept here
#: so breaker.py never has to import its consumers (app/continuity.py wakes
#: queued callers on the OPEN → HALF_OPEN edge through it).
_listeners: List[Callable[[str, str, str, str], None]] = []


class BreakerOpen(RuntimeError):
    """The breaker refused a call for the whole wait window.

    Never raised to a caller on its own: `resilience.resilient` wraps it in
    `ModelUnavailable` (or, on a chat turn, parks the generation through
    app/continuity.py) so callers need no new handling. It survives as the
    recorded cause so a log line can say the window closed on the breaker,
    not on a port that answered.
    """

    def __init__(self, engine: str, state: str) -> None:
        self.engine = engine
        self.state = state
        super().__init__(f"breaker for engine {engine} is {state}")


class Permit:
    """One admitted call. `canary` marks the single HALF_OPEN probe whose
    outcome decides the next state; every other permit is bookkeeping so a
    late outcome from a call admitted while CLOSED cannot be mistaken for
    the canary's."""

    __slots__ = ("engine", "canary", "issued_at")

    def __init__(self, engine: str, canary: bool, issued_at: float) -> None:
        self.engine = engine
        self.canary = canary
        self.issued_at = issued_at


class Breaker:
    """The state machine for one engine. Thread-safe; the orchestrator is a
    single event loop but a metric or a probe may read it from a thread."""

    def __init__(
        self,
        engine: str,
        *,
        clock: Optional[Callable[[], float]] = None,
        external_open: Optional[Callable[[], Optional[str]]] = None,
        failures: Optional[int] = None,
        window_s: Optional[float] = None,
        cooldown_s: Optional[float] = None,
    ) -> None:
        self.engine = engine
        #: None → time.monotonic looked up at each call (not captured here:
        #: a test that patches the clock must not have it frozen into a
        #: breaker built while the patch was on).
        self._clock = clock
        #: Returns the controller state that demands OPEN, or None. The main
        #: engine's is engine_state.external_open.
        self._external_open = external_open
        # Thresholds default to the live settings on every read, so an
        # operator's env change and a test's monkeypatch both take effect
        # without rebuilding the breaker.
        self._failures = failures
        self._window_s = window_s
        self._cooldown_s = cooldown_s
        # Re-entrant: a transition listener may read the breaker back while
        # the transition that woke it still holds the lock.
        self._lock = threading.RLock()
        self._state = CLOSED
        self._since = self._now()
        self._opened_at = 0.0
        self._held_by: Optional[str] = None
        self._recent: Deque[float] = deque()
        self._canary: Optional[Permit] = None
        self._last_reason = "init"
        metrics.set_gauge("llm_breaker_state", STATE_CODES[CLOSED],
                          "Circuit breaker state per engine: 0 CLOSED, 1 OPEN, 2 HALF_OPEN.", engine=engine)

    def _now(self) -> float:
        return self._clock() if self._clock is not None else time.monotonic()

    # -- thresholds ---------------------------------------------------------

    @property
    def failures_threshold(self) -> int:
        return max(1, int(self._failures if self._failures is not None else settings.llm_breaker_failures))

    @property
    def window_s(self) -> float:
        return max(0.0, float(self._window_s if self._window_s is not None else settings.llm_breaker_window_s))

    @property
    def cooldown_s(self) -> float:
        return max(0.0, float(self._cooldown_s if self._cooldown_s is not None else settings.llm_breaker_cooldown_s))

    @property
    def canary_hold_s(self) -> float:
        """How long one canary may keep everyone else out (module docstring)."""
        return max(0.0, float(settings.llm_breaker_canary_hold_s))

    # -- reading ------------------------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            self._refresh(self._now())
            return self._state

    def allows(self) -> bool:
        """Would a call be admitted right now? A peek, not an acquisition:
        the queued callers in app/resilience.py ask it on every tick of
        their wait, so a queue never touches the engine to find out."""
        with self._lock:
            now = self._now()
            self._refresh(now)
            if self._state == CLOSED:
                return True
            return self._state == HALF_OPEN and self._canary_slot_free(now)

    def describe(self) -> dict:
        """What /health shows. In-memory only."""
        with self._lock:
            now = self._now()
            self._refresh(now)
            return {
                "state": self._state,
                "code": STATE_CODES[self._state],
                "since_s": round(max(0.0, now - self._since), 1),
                "failures_in_window": len(self._recent),
                "threshold": self.failures_threshold,
                "held_open_by": self._held_by,
                "canary_in_flight": self._canary is not None,
                "last_reason": self._last_reason,
            }

    # -- admission ----------------------------------------------------------

    def acquire(self) -> Optional[Permit]:
        """Admit one call, or return None.

        CLOSED admits everyone; HALF_OPEN admits exactly one canary until its
        outcome is recorded (or the permit is released); OPEN admits nobody.
        Every admitted call MUST settle its permit through record_success,
        record_failure or release, or a HALF_OPEN breaker would wait forever
        for a canary that never reports back.
        """
        with self._lock:
            now = self._now()
            self._refresh(now)
            if self._state == CLOSED:
                return Permit(self.engine, False, now)
            if self._state == HALF_OPEN and self._canary_slot_free(now):
                self._canary = Permit(self.engine, True, now)
                return self._canary
            return None

    def _canary_slot_free(self, now: float) -> bool:
        """No canary out — or one out for longer than the hold allows, in
        which case it is forgotten (its late outcome becomes ordinary) and
        the next caller probes. Called under the lock."""
        if self._canary is None:
            return True
        if now - self._canary.issued_at <= self.canary_hold_s:
            return False
        log.warning(
            "llm.breaker engine=%s canary outstanding for %.0fs (hold %.0fs): letting the next caller probe",
            self.engine, now - self._canary.issued_at, self.canary_hold_s,
        )
        self._canary = None
        return True

    def release(self, permit: Optional[Permit]) -> None:
        """The call was abandoned before it reached the engine (cancelled
        while sizing, say): neither a success nor a failure."""
        with self._lock:
            if permit is not None and permit is self._canary:
                self._canary = None

    # -- outcomes -----------------------------------------------------------

    def record_success(self, permit: Optional[Permit] = None) -> None:
        with self._lock:
            now = self._now()
            self._refresh(now)
            if permit is not None and permit is self._canary:
                self._canary = None
                if self._state == HALF_OPEN:
                    self._recent.clear()
                    self._transition(CLOSED, "canary_ok", now)
                return
            if self._state == CLOSED:
                # A good answer between failures means the engine is serving:
                # the window restarts (see the module docstring).
                self._recent.clear()

    def record_failure(self, reason: Optional[str], permit: Optional[Permit] = None) -> None:
        """Count one failure by its CONTRACT §4 reason.

        `reason=None` means "not the engine's fault" (a parse error in our
        own code, say): the permit is settled and nothing is counted.
        """
        with self._lock:
            now = self._now()
            self._refresh(now)
            is_canary = permit is not None and permit is self._canary
            if is_canary:
                self._canary = None
            if reason is None or reason == "cancelled":
                # Nobody's fault (a person pressed Stop, a task was torn
                # down): not counted, and a cancelled canary simply hands the
                # probe to the next caller.
                return
            if reason not in REASONS:
                log.warning("llm.breaker engine=%s ignoring unknown failure reason %r", self.engine, reason)
                return
            metrics.inc("llm_breaker_failures_total",
                        "Model-call failures seen by the circuit breaker, by CONTRACT §4 reason.",
                        engine=self.engine, reason=reason)
            self._last_reason = reason
            if reason not in OPENING_REASONS:
                # Counted for the operator, never a reason to open: a read
                # timeout is a slow engine, a 4xx is a bad request. A canary
                # that ended this way proved nothing either way, so the
                # breaker stays HALF_OPEN and the next caller probes.
                return
            if is_canary and self._state == HALF_OPEN:
                self._transition(OPEN, f"canary_{reason}", now)
                return
            self._recent.append(now)
            self._prune(now)
            if self._state == CLOSED and len(self._recent) >= self.failures_threshold:
                self._transition(OPEN, f"{len(self._recent)}x{reason}_in_{int(self.window_s)}s", now)

    # -- internals ----------------------------------------------------------

    def _prune(self, now: float) -> None:
        window = self.window_s
        while self._recent and now - self._recent[0] > window:
            self._recent.popleft()

    def _refresh(self, now: float) -> None:
        """Apply the time- and controller-driven transitions. Called under
        the lock before every read or write, so the state a caller sees is
        never older than its own clock."""
        held = self._external_open() if self._external_open is not None else None
        if held:
            if self._state != OPEN:
                self._transition(OPEN, f"controller_{held}", now)
            # Held: the cooldown starts when the controller stops saying so.
            self._opened_at = now
            self._held_by = held
            return
        self._held_by = None
        self._prune(now)
        if self._state == OPEN and now - self._opened_at >= self.cooldown_s:
            self._transition(HALF_OPEN, "cooldown", now)

    def _transition(self, to: str, reason: str, now: float) -> None:
        frm = self._state
        if frm == to:
            return
        self._state = to
        self._since = now
        self._last_reason = reason
        if to == OPEN:
            self._opened_at = now
            self._canary = None
        elif to == HALF_OPEN:
            self._canary = None
        elif to == CLOSED:
            self._recent.clear()
            self._canary = None
        metrics.set_gauge("llm_breaker_state", STATE_CODES[to],
                          "Circuit breaker state per engine: 0 CLOSED, 1 OPEN, 2 HALF_OPEN.", engine=self.engine)
        metrics.inc("llm_breaker_transitions_total", "Circuit breaker transitions per engine and target state.",
                    engine=self.engine, to=to)
        line = "llm.breaker engine=%s from=%s to=%s reason=%s"
        if to == OPEN:
            log.warning(line, self.engine, frm, to, reason)
        else:
            log.info(line, self.engine, frm, to, reason)
        for fn in list(_listeners):
            try:
                fn(self.engine, frm, to, reason)
            except Exception:  # noqa: BLE001 — a listener must never break a transition
                log.exception("llm.breaker listener failed")


# ---------------------------------------------------------------------------
# The registry: one breaker per engine PER EVENT LOOP, built on first use.
#
# A breaker's memory is the outcomes of the calls made on one event loop,
# so it belongs to that loop — the same reason llm._CLIENTS keys its httpx
# pools by loop. The orchestrator runs one loop for its whole life, so in
# production this is simply "one breaker per engine". A process that runs
# many loops — the test suite, where every `asyncio.run` and every
# TestClient request is a fresh loop, and where hundreds of tests hand the
# wrapper a dead client on purpose — never carries one loop's verdict into
# the next: three refused connections in a routing test must not open the
# breaker for the budget test that follows it. Weakly keyed, so a finished
# loop takes its breakers with it.
# ---------------------------------------------------------------------------

_by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Dict[str, Breaker]]" = weakref.WeakKeyDictionary()
#: Outside any running loop (a synchronous probe, a CLI tool).
_no_loop: Dict[str, Breaker] = {}
#: Tests: breakers to hand EVERY new loop instead of building fresh ones —
#: a breaker with a hand-driven clock, installed by a fixture before the
#: test's own loop exists.
_installed: Dict[str, Breaker] = {}
_registry_lock = threading.Lock()


def _build(engine: str) -> Breaker:
    if engine == MAIN:
        from . import engine_state  # the controller's verdict is the main engine's external input

        return Breaker(MAIN, external_open=engine_state.external_open)
    return Breaker(engine)  # tests only: no second engine exists in production


def _bucket() -> Dict[str, Breaker]:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _no_loop
    bucket = _by_loop.get(loop)
    if bucket is None:
        bucket = _by_loop[loop] = dict(_installed)
    return bucket


def get(engine: str) -> Breaker:
    with _registry_lock:
        bucket = _bucket()
        brk = bucket.get(engine)
        if brk is None:
            brk = bucket[engine] = _installed.get(engine) or _build(engine)
        return brk


def install(engine: str, breaker: Breaker) -> None:
    """Tests: this breaker for `engine` on every loop from now on."""
    with _registry_lock:
        _installed[engine] = breaker
        _no_loop[engine] = breaker
        for bucket in list(_by_loop.values()):
            bucket[engine] = breaker


def all_breakers() -> Dict[str, Breaker]:
    """Every engine that has a breaker (for /health); built so the report
    is complete before the first call has been made."""
    return {name: get(name) for name in ENGINES}


def _norm(url: str) -> str:
    return str(url or "").rstrip("/")


def engine_for_base_url(base_url: str) -> Optional[str]:
    """Which breaker guards this endpoint, or None (the router, the
    embedding engine, OCR — sidecars with no breaker keep today's
    wait-and-retry alone; none of them answers a person).

    Resolved on every call: the URL lives in settings and a test may point
    it anywhere.
    """
    url = _norm(base_url)
    if not url:
        return None
    if url == _norm(settings.openai_base_url):
        return MAIN
    return None


def for_base_url(base_url: str) -> Optional[Breaker]:
    engine = engine_for_base_url(base_url)
    return get(engine) if engine is not None else None


def main_allows() -> bool:
    """Would the main engine admit a call now? The entry points' first question."""
    return get(MAIN).allows()


def on_transition(fn: Callable[[str, str, str, str], None]) -> None:
    if fn not in _listeners:
        _listeners.append(fn)


def reset() -> None:
    """Tests only: fresh breakers on every loop, listeners kept."""
    with _registry_lock:
        _installed.clear()
        _no_loop.clear()
        _by_loop.clear()
