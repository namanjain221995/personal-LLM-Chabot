"""Is a silent `/v1` generation dead, or only slow? (no-timeout design, 2026-09-13)

WHY THIS FILE REPLACES A CLOCK. Until 2026-09-13 a public generation was cut
by a wall clock (`planning.wall_clock_for`, up to 6 h) and a backstop. A clock
cannot tell a 30-minute silent prefill of a 950k-token prompt (measured ~800 s
at ~1,190 tok/s on 2026-09-12) from a wedged engine, so every value was wrong
for somebody: too short killed legitimate work, too long held a lane for a
dead request. The owner's rule is "no timeouts on /v1", so liveness comes from
ENGINE EVIDENCE — the controller's three-way verdict and vLLM's own counters —
and never from elapsed time alone.

WHAT IT DECIDES, AND WHAT IT NEVER DOES.
* `MainGuard` answers "interrupt this attempt?" for the main model. An
  interrupt is NOT a failure: the durable runner closes the engine stream,
  waits for the engine to stop being proven bad, and resumes the answer by
  continuation (durable.py). Only counters (`attempt_counts`) turn repeated
  interrupts into a failure.
* Unknown never interrupts. A crashlooping or unreachable controller says
  nothing about the engine, and "cannot see" must not become "is down" — the
  only rule that fires while blind is 3,600 s of silence (4.5x the full-window
  prefill).
* `SidecarWitness` does the same for the router and OCR engines, which have no
  controller: it reads their `/metrics` counters.

EVERYTHING TAKES AN INJECTABLE CLOCK so the acceptance scenarios (a 30-minute
prefill, a 2-hour busy engine, a 1,800 s engine-down grace) run in
milliseconds under a virtual clock (tests/test_publicapi_liveness.py).

INTERFACE FROM T1 (engine_state.py). `EngineStateView` calls
`engine_state.proven_not_serving`, `serving_evidence_at`, `note_chunk`,
`head_started_at`, `recovery_headroom` and `engine_load` (with the sample's
own `sample_observed_at`). Assembler, 2026-09-14: the local fallbacks for an
engine_state without those functions are removed now that T1's are in the tree.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Protocol

from . import registry

log = logging.getLogger(__name__)

Clock = Callable[[], float]

# ------------------------------------------------------------ settings --


def quiet_s() -> float:
    """PUBLIC_API_LIVENESS_QUIET_S (120): below this much silence nothing is
    even evaluated — a decoding stream never gets past G0."""
    return max(1.0, registry.setting_float("PUBLIC_API_LIVENESS_QUIET_S", 120.0))


def not_serving_s() -> float:
    """PUBLIC_API_LIVENESS_NOT_SERVING_S (30): a proven-bad verdict must hold
    this long (two controller polls) before it interrupts, so one odd document
    does not cost a re-prefill."""
    return max(0.0, registry.setting_float("PUBLIC_API_LIVENESS_NOT_SERVING_S", 30.0))


def lost_min_s() -> float:
    """PUBLIC_API_LIVENESS_LOST_MIN_S (300): the 'lost' rule never fires on an
    attempt dispatched less than this long ago — API-server tokenisation of a
    full-window prompt happens before vLLM counts the request (unmeasured;
    operator step in the design)."""
    return max(0.0, registry.setting_float("PUBLIC_API_LIVENESS_LOST_MIN_S", 300.0))


def unknown_silence_s() -> float:
    """PUBLIC_API_LIVENESS_UNKNOWN_SILENCE_S (3,600): the only rule while the
    controller cannot see the engine."""
    return max(1.0, registry.setting_float("PUBLIC_API_LIVENESS_UNKNOWN_SILENCE_S", 3600.0))


def quarantine_serving_s() -> float:
    """PUBLIC_API_QUARANTINE_SERVING_S (300)."""
    return max(0.0, registry.setting_float("PUBLIC_API_QUARANTINE_SERVING_S", 300.0))


def quarantine_min_recoveries() -> int:
    """PUBLIC_API_QUARANTINE_MIN_RECOVERIES (2): a quarantined run may cost at
    most one recovery and still leave the chat app one."""
    return max(0, registry.setting_int("PUBLIC_API_QUARANTINE_MIN_RECOVERIES", 2))


def engine_down_grace_s() -> float:
    """PUBLIC_API_ENGINE_DOWN_GRACE_S (1,800)."""
    return max(0.0, registry.setting_float("PUBLIC_API_ENGINE_DOWN_GRACE_S", 1800.0))


def sidecar_silence_s() -> float:
    """PUBLIC_API_SIDECAR_SILENCE_S (1,800): the router/OCR fallback when the
    witness is unknown."""
    return max(1.0, registry.setting_float("PUBLIC_API_SIDECAR_SILENCE_S", 1800.0))


#: Consecutive idle engine samples, at least this far apart, for 'lost'.
LOST_SAMPLES = 3
LOST_SAMPLE_SPACING_S = 10.0
#: An engine sample older than this is not evidence for 'lost' (three
#: controller polls). 2026-09-14 review P3: when /metrics fails the controller
#: keeps publishing its LAST GOOD sample under a fresh `generated_at` (state
#: DEGRADED "metrics unavailable"), and a running=0 taken before this request
#: reached vLLM looked like three fresh idle samples — a healthy 5-minute
#: prefill was interrupted, re-prefilled, and after three rounds failed.
LOST_SAMPLE_MAX_AGE_S = 45.0
#: A gap in serving evidence longer than this during an attempt means the
#: attempt was NOT on a proven-serving engine throughout, so a silent end is
#: not counted as stalled (design: "no gap over 45 s").
EVIDENCE_GAP_S = 45.0
#: Controller polls without a proven-bad verdict before `wait_not_bad` returns.
NOT_BAD_POLLS = 2
NOT_BAD_POLL_S = 15.0

REASON_OK = "ok"
REASON_ENGINE = "engine"
REASON_LOST = "lost"
REASON_BLIND = "blind"
REASON_CONNECTION = "connection"
REASON_STALLED = "stalled"
REASON_RESTARTED = "restarted"
#: Sidecars (review 2026-09-14): the connection was refused, or the engine
#: answered 502/503/504 — the request never reached the model.
REASON_CONNECT = "connect"
#: Sidecars: the engine ANSWERED this request with another 5xx.
REASON_ENGINE_ERROR = "engine_error"
#: Interrupt reasons that are evidence the attempt was silent on a live engine.
SILENT_END_REASONS = frozenset({REASON_LOST, REASON_BLIND, REASON_CONNECTION, REASON_STALLED})


# ------------------------------------------------------- the engine view --


class EngineView(Protocol):
    """What the guard reads. `engine_state` in production, a fake in tests."""

    def proven_not_serving(self, now: float) -> Optional[str]: ...

    def serving(self, now: float) -> Optional[bool]: ...

    def engine_load(self, now: float) -> Optional[Mapping[str, float]]: ...

    def head_started_at(self, now: float) -> Optional[float]: ...

    def incident_id(self, now: float) -> Optional[str]: ...

    def sample_at(self, now: float) -> Optional[float]: ...

    def serving_evidence_at(self, now: float) -> Optional[float]: ...

    def recovery_headroom(self, now: float) -> Optional[int]: ...

    def note_chunk(self, now: float) -> None: ...


class EngineStateView:
    """`app.engine_state`, adapted. Never raises: monitoring must not fail a
    request (the same rule as `streaming._engine_state_name`)."""

    def __init__(self) -> None:
        pass

    @staticmethod
    def _es() -> Any:
        from .. import engine_state

        return engine_state

    def proven_not_serving(self, now: float) -> Optional[str]:
        try:
            return self._es().proven_not_serving(now)
        except Exception:  # noqa: BLE001
            log.debug("proven_not_serving unavailable", exc_info=True)
            return None

    def serving(self, now: float) -> Optional[bool]:
        try:
            return self._es().serving(now)
        except Exception:  # noqa: BLE001
            return None

    def engine_load(self, now: float) -> Optional[Mapping[str, float]]:
        """The controller's engine sample, or None when it cannot be trusted
        as CURRENT (review P3): a DEGRADED document whose reason says
        /metrics is unavailable carries the last good sample, not a new one.
        Adds `sample_age_s` when engine_state exposes the sample's own
        timestamp (`sample_observed_at`, the controller's
        signals.engine.observed_at, same clock as `generated_at`)."""
        try:
            es = self._es()
            load = es.engine_load(now)
            if load is None:
                return None
            snap = es.snapshot()
            reason = str(getattr(snap, "reason", "") or "").lower()
            if snap is not None and "metrics unavailable" in reason:
                return None
            out = dict(load)
            observed = out.get("sample_observed_at")
            generated = out.get("generated_at")
            if observed is not None and generated is not None and "sample_age_s" not in out:
                # engine_state.engine_load carries `sample_observed_at`
                # (signals.engine.observed_at) since the assembly of
                # 2026-09-14; a controller build without the stamp leaves
                # sample_age_s absent and G2 falls back to the document stamp
                # plus the metrics-unavailable rule above.
                out["sample_age_s"] = float(out.get("age_s") or 0.0) + max(0.0, float(generated) - float(observed))
            return out
        except Exception:  # noqa: BLE001
            return None

    def head_started_at(self, now: float) -> Optional[float]:
        try:
            return self._es().head_started_at()
        except Exception:  # noqa: BLE001
            return None

    def incident_id(self, now: float) -> Optional[str]:
        try:
            es = self._es()
            if es.unknown(now):
                return None
            snap = es.snapshot()
            return None if snap is None else snap.incident_id
        except Exception:  # noqa: BLE001
            return None

    def sample_at(self, now: float) -> Optional[float]:
        try:
            es = self._es()
            if es.unknown(now):
                return None
            snap = es.snapshot()
            return None if snap is None else float(snap.observed_at)
        except Exception:  # noqa: BLE001
            return None

    def serving_evidence_at(self, now: float) -> Optional[float]:
        try:
            return self._es().serving_evidence_at()
        except Exception:  # noqa: BLE001
            return None

    def recovery_headroom(self, now: float) -> Optional[int]:
        try:
            value = self._es().recovery_headroom()
            return None if value is None else int(value)
        except Exception:  # noqa: BLE001
            # None keeps quarantined runs waiting (never failing), durable.py.
            return None

    def note_chunk(self, now: float) -> None:
        try:
            self._es().note_chunk()
        except Exception:  # noqa: BLE001
            pass


_default_view: Optional[EngineStateView] = None


def default_view() -> EngineStateView:
    global _default_view
    if _default_view is None:
        _default_view = EngineStateView()
    return _default_view


# ------------------------------------------------------------- verdicts --


@dataclass(frozen=True)
class Verdict:
    interrupt: bool
    reason: str = REASON_OK
    #: The attempt was dispatched before the engine incident began: the run
    #: may be what crashed the engine (quarantine input).
    implicated: bool = False
    detail: str = ""


OK = Verdict(False)


class MainGuard:
    """G0-G3 of the design's liveness_guard, for ONE attempt on the main model.

    Tick it at least every 15 s from creation (before dispatch too: the guard
    has to know whether an incident began before or after this attempt was
    dispatched). `dispatched()` is llm's on_dispatch; `chunk()` every engine
    chunk, reasoning included.
    """

    def __init__(self, view: Optional[EngineView] = None, *, clock: Clock = time.monotonic) -> None:
        self.view = view if view is not None else default_view()
        self.clock = clock
        self.created_at = clock()
        self.dispatched_at: Optional[float] = None
        self.last_chunk_at: Optional[float] = None
        self.new_tokens = 0
        self._not_serving_since: Optional[float] = None
        self._incident_began_at: Optional[float] = None
        self._head: Optional[float] = None
        self._head_changed_at: Optional[float] = None
        self._incident_at_create: Optional[str] = None
        self._incident_seen = False
        self._idle_samples: list = []
        #: Serving evidence during the attempt: the longest gap seen.
        self._evidence_at: Optional[float] = None
        self.max_evidence_gap_s = 0.0
        self._sample(self.created_at, initial=True)

    # -- inputs ---------------------------------------------------------

    def dispatched(self, now: Optional[float] = None) -> None:
        if self.dispatched_at is None:
            self.dispatched_at = self.clock() if now is None else now
            self._evidence_at = self.dispatched_at if self._evidence_at is None else self._evidence_at

    def chunk(self, now: Optional[float] = None, *, token: bool = True) -> None:
        moment = self.clock() if now is None else now
        if self.dispatched_at is None:
            # A chunk proves dispatch even when on_dispatch never fired (an
            # llm without the callback, a sidecar).
            self.dispatched_at = moment
        self.last_chunk_at = moment
        if token:
            self.new_tokens += 1
        self._evidence(moment)
        try:
            self.view.note_chunk(moment)
        except Exception:  # noqa: BLE001
            pass

    # -- derived --------------------------------------------------------

    def silence(self, now: float) -> float:
        anchor = self.last_chunk_at if self.last_chunk_at is not None else self.dispatched_at
        if anchor is None:
            return 0.0
        return max(0.0, now - anchor)

    @property
    def evidence_covered(self) -> bool:
        return self.max_evidence_gap_s <= EVIDENCE_GAP_S

    def head_changed(self) -> bool:
        return self._head_changed_at is not None

    def _evidence(self, moment: float) -> None:
        if self.dispatched_at is None:
            return
        if self._evidence_at is not None:
            self.max_evidence_gap_s = max(self.max_evidence_gap_s, moment - self._evidence_at)
        self._evidence_at = moment

    def _implicated(self) -> bool:
        began = self._incident_began_at
        return self.dispatched_at is not None and began is not None and self.dispatched_at <= began

    def _sample(self, now: float, *, initial: bool = False) -> Optional[str]:
        view = self.view
        bad = view.proven_not_serving(now)
        if bad is not None:
            if self._not_serving_since is None:
                self._not_serving_since = now
                if self._incident_began_at is None:
                    self._incident_began_at = now
        else:
            self._not_serving_since = None
        head = view.head_started_at(now)
        if head is not None:
            if self._head is not None and head != self._head and self._head_changed_at is None:
                self._head_changed_at = now
                if self._incident_began_at is None:
                    self._incident_began_at = now
            self._head = head
        incident = view.incident_id(now)
        if initial:
            self._incident_at_create = incident
        elif incident is not None and incident != self._incident_at_create and not self._incident_seen:
            self._incident_seen = True
            if self._incident_began_at is None:
                self._incident_began_at = now
        serving = view.serving(now)
        if serving:
            self._evidence(now)
        return bad

    def tick(self, now: Optional[float] = None) -> Verdict:
        """The design's G0-G3, in order. Pure apart from the view reads."""
        moment = self.clock() if now is None else now
        bad = self._sample(moment)
        if self.dispatched_at is None:
            return OK
        silence = self.silence(moment)
        # G0: a decoding stream never gets past here.
        if silence < quiet_s():
            self._idle_samples.clear()
            return OK
        # G1: proven bad for the threshold, or the head container changed.
        if bad is not None and self._not_serving_since is not None:
            if moment - self._not_serving_since >= not_serving_s():
                return Verdict(True, REASON_ENGINE, self._implicated(), f"proven not serving: {bad}")
        if self._head_changed_at is not None:
            return Verdict(True, REASON_ENGINE, self._implicated(), "engine head restarted")
        serving = self.view.serving(moment)
        # G2: lost — serving, idle on 3 fresh samples >= 10 s apart, dispatched >= 300 s.
        if serving:
            load = self.view.engine_load(moment)
            sample = self.view.sample_at(moment)
            if load is None or sample is None:
                # No sample we can trust as current: the idle run is broken
                # ("consecutive" means consecutive trustworthy samples).
                self._idle_samples.clear()
                return OK
            taken = sample
            age = load.get("sample_age_s")
            if age is not None:
                if float(age) > LOST_SAMPLE_MAX_AGE_S:
                    self._idle_samples.clear()
                    return OK
                taken = moment - float(age)
                if taken < self.dispatched_at:
                    # Taken before this request was dispatched: it says
                    # nothing about whether the engine has it.
                    return OK
            elif load.get("generated_at") is not None:
                taken = float(load["generated_at"])
            busy = float(load.get("requests_running") or 0) + float(load.get("requests_waiting") or 0)
            if busy > 0:
                self._idle_samples.clear()
            elif not self._idle_samples or taken - self._idle_samples[-1] >= LOST_SAMPLE_SPACING_S:
                self._idle_samples.append(taken)
            if (
                len(self._idle_samples) >= LOST_SAMPLES
                and moment - self.dispatched_at >= lost_min_s()
            ):
                return Verdict(True, REASON_LOST, False, "engine idle while this request was outstanding")
            return OK
        self._idle_samples.clear()
        # G3: blind for an hour of silence.
        if serving is None and silence >= unknown_silence_s():
            return Verdict(True, REASON_BLIND, False, "no engine verdict for the silence bound")
        return OK

    def classify_error(self, now: Optional[float] = None, exc: Optional[BaseException] = None) -> Verdict:
        """An exception raised inside llm (ModelUnavailable, a connect or
        protocol error, AdmissionRejected): 'engine' when G1's conditions hold
        or the head changed, else 'connection'. `exc` is accepted for the
        SidecarGuard interface; the main model's verdict comes from the
        controller, not from the exception."""
        moment = self.clock() if now is None else now
        bad = self._sample(moment)
        if self._head_changed_at is not None or (
            bad is not None
            and self._not_serving_since is not None
            and moment - self._not_serving_since >= not_serving_s()
        ):
            return Verdict(True, REASON_ENGINE, self._implicated(), "engine incident")
        if bad is not None:
            return Verdict(True, REASON_ENGINE, self._implicated(), f"engine {bad}")
        return Verdict(True, REASON_CONNECTION, False, "engine connection failed")


# ------------------------------------------------------------- counters --


@dataclass(frozen=True)
class AttemptEnd:
    reason: str
    dispatched: bool
    new_tokens: int
    evidence_covered: bool
    implicated: bool


@dataclass(frozen=True)
class CounterChange:
    #: +1, 0, or None meaning "reset to 0".
    stalled: Optional[int]
    engine_fault: int


def attempt_counts(end: AttemptEnd) -> CounterChange:
    """The design's COUNTERS rule, as a pure function.

    stalled +1: ended silent (lost/blind/connection/stalled) with zero new
    tokens, dispatched, and serving evidence covered the whole attempt. Any new
    token resets it. engine_fault +1: implicated in an engine incident.
    Yields, restart/shutdown/store suspends, lease loss and pre-dispatch waits
    count nothing — they are not evidence about THIS request.
    """
    if end.reason in ("yield", "restart", "shutdown", "store", "lease_lost", REASON_CONNECT):
        return CounterChange(stalled=None if end.new_tokens > 0 else 0, engine_fault=0)
    stalled: Optional[int] = None if end.new_tokens > 0 else 0
    if (
        end.reason in SILENT_END_REASONS
        and end.new_tokens == 0
        and end.dispatched
        and end.evidence_covered
    ):
        stalled = 1
    if end.reason == REASON_ENGINE_ERROR and end.new_tokens == 0 and end.dispatched:
        # A sidecar that ANSWERED this request with a 5xx (review
        # 2026-09-14 P1b): counted like a silent end — re-dispatched once,
        # then failed retryably — instead of looping on connect backoff for
        # the whole 1,800 s grace.
        stalled = 1
    # Implicated in an engine incident: the main model's G1, or a sidecar
    # that restarted (witness) or dropped the connection while this call was
    # outstanding — "a second restart coinciding with the same call".
    fault = 1 if (end.reason in (REASON_ENGINE, REASON_RESTARTED) and end.implicated and end.dispatched) else 0
    return CounterChange(stalled=stalled, engine_fault=fault)


def max_stalled_attempts() -> int:
    """PUBLIC_API_RESUME_MAX_STALLED_ATTEMPTS (3)."""
    return max(1, registry.setting_int("PUBLIC_API_RESUME_MAX_STALLED_ATTEMPTS", 3))


# ---------------------------------------------------- quarantine and grace --


class ServingTracker:
    """How long the engine has been proven serving, continuously."""

    def __init__(self, view: Optional[EngineView] = None, *, clock: Clock = time.monotonic) -> None:
        self.view = view if view is not None else default_view()
        self.clock = clock
        self.since: Optional[float] = None

    def observe(self, now: Optional[float] = None) -> Optional[float]:
        moment = self.clock() if now is None else now
        if self.view.serving(moment) and self.view.proven_not_serving(moment) is None:
            if self.since is None:
                self.since = moment
        else:
            self.since = None
        return self.since

    def quarantine_ready(self, now: Optional[float] = None) -> bool:
        """Serving continuously >= 300 s AND the controller's recovery budget
        leaves >= 2. An unknown headroom is NOT ready: a quarantined run
        waits (heartbeats continue) rather than risk the chat app's last
        recovery."""
        moment = self.clock() if now is None else now
        since = self.observe(moment)
        if since is None or moment - since < quarantine_serving_s():
            return False
        headroom = self.view.recovery_headroom(moment)
        return headroom is not None and headroom >= quarantine_min_recoveries()


class EngineDownGrace:
    """ENGINE-DOWN GRACE, counted only while a run is NOT dispatched.

    Expires when proven_not_serving has been non-None continuously for the
    grace, or when every dispatch attempt failed at connect and there has
    been no serving evidence for the grace — counted from the LATER of the
    last evidence and the first connect failure, so a process that was idle
    for hours does not fail a run on its first refused connection. Unknown
    alone never counts.
    """

    def __init__(self, view: Optional[EngineView] = None, *, clock: Clock = time.monotonic) -> None:
        self.view = view if view is not None else default_view()
        self.clock = clock
        self.bad_since: Optional[float] = None
        self.connect_failures = 0
        self.first_failure_at: Optional[float] = None
        self.any_connected = False

    def observe(self, now: Optional[float] = None) -> None:
        moment = self.clock() if now is None else now
        if self.view.proven_not_serving(moment) is not None:
            if self.bad_since is None:
                self.bad_since = moment
        else:
            self.bad_since = None

    def connect_failed(self) -> None:
        self.connect_failures += 1
        if self.first_failure_at is None:
            self.first_failure_at = self.clock()

    def connected(self) -> None:
        self.any_connected = True
        self.connect_failures = 0
        self.first_failure_at = None

    def expired(self, now: Optional[float] = None) -> bool:
        moment = self.clock() if now is None else now
        self.observe(moment)
        grace = engine_down_grace_s()
        if self.bad_since is not None and moment - self.bad_since >= grace:
            return True
        if self.connect_failures > 0 and not self.any_connected and self.first_failure_at is not None:
            evidence = self.view.serving_evidence_at(moment)
            anchor = self.first_failure_at if evidence is None else max(evidence, self.first_failure_at)
            return moment - anchor >= grace
        return False


class EngineDownExpired(Exception):
    """The engine-down grace ran out while a run waited to dispatch."""


async def wait_not_bad(
    view: Optional[EngineView] = None,
    *,
    abandon: Optional[asyncio.Event] = None,
    grace: Optional[EngineDownGrace] = None,
    clock: Clock = time.monotonic,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    poll_s: Optional[float] = None,
    on_poll: Optional[Callable[[], Any]] = None,
) -> bool:
    """Return True after `NOT_BAD_POLLS` consecutive polls without a proven-bad
    verdict; False when `abandon` is set; raise `EngineDownExpired` when the
    grace runs out. Unknown counts as not bad — the engine is only waited on
    when the controller has PROVEN it cannot serve."""
    view = view if view is not None else default_view()
    interval = NOT_BAD_POLL_S if poll_s is None else float(poll_s)
    clean = 0
    while True:
        if abandon is not None and abandon.is_set():
            return False
        now = clock()
        if grace is not None and grace.expired(now):
            raise EngineDownExpired()
        if view.proven_not_serving(now) is None:
            clean += 1
            if clean >= NOT_BAD_POLLS:
                return True
        else:
            clean = 0
        if on_poll is not None:
            on_poll()
        if abandon is not None:
            try:
                async with asyncio.timeout(interval):
                    await asyncio.shield(abandon.wait())
            except asyncio.TimeoutError:
                pass
        else:
            await sleep(interval)


# ------------------------------------------------------ sidecar witnesses --

_WITNESS_METRICS = {
    "running": ("vllm:num_requests_running",),
    "waiting": ("vllm:num_requests_waiting",),
    "generation_tokens": ("vllm:generation_tokens_total",),
    "prompt_tokens": ("vllm:prompt_tokens_total",),
    "iterations": ("vllm:iteration_tokens_total_count",),
    "kv_usage": ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"),
    "process_start": ("process_start_time_seconds",),
}
_PROGRESS = ("generation_tokens", "prompt_tokens", "iterations")
_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([^\s]+)")


@dataclass(frozen=True)
class MetricsSample:
    at: float
    values: Mapping[str, float] = field(default_factory=dict)

    def get(self, name: str) -> Optional[float]:
        return self.values.get(name)


def parse_metrics(text: str, at: float) -> MetricsSample:
    """Prometheus text → the witness values, summed over label sets. Unknown
    lines are ignored; a NaN is dropped rather than compared."""
    sums: Dict[str, float] = {}
    lookup = {metric: key for key, metrics in _WITNESS_METRICS.items() for metric in metrics}
    for raw in text.splitlines():
        if not raw or raw.startswith("#"):
            continue
        match = _LINE.match(raw.strip())
        if not match:
            continue
        key = lookup.get(match.group(1))
        if key is None:
            continue
        try:
            value = float(match.group(3))
        except ValueError:
            continue
        if math.isnan(value):
            continue
        sums[key] = sums.get(key, 0.0) + value
    return MetricsSample(at=at, values=sums)


WITNESS_LOST_MIN_S = 30.0
WITNESS_STALL_S = 120.0


class SidecarWitness:
    """progressing / stalled / lost / restarted / unknown, from /metrics."""

    def __init__(self, *, clock: Clock = time.monotonic) -> None:
        self.clock = clock
        self._last: Optional[MetricsSample] = None
        self._failed = True
        self._idle_scrapes = 0
        self._moved_at: Optional[float] = None
        self._restarts: list = []

    def observe(self, sample: Optional[MetricsSample]) -> None:
        if sample is None:
            self._failed = True
            return
        previous = self._last
        self._failed = False
        running = sample.get("running")
        waiting = sample.get("waiting")
        if running is not None and waiting is not None and running + waiting == 0:
            self._idle_scrapes += 1
        else:
            self._idle_scrapes = 0
        if previous is not None:
            start_now, start_before = sample.get("process_start"), previous.get("process_start")
            if start_now is not None and start_before is not None and start_now != start_before:
                self._restarts.append(sample.at)
                self._moved_at = None
            elif any(
                sample.get(k) is not None and previous.get(k) is not None and sample.get(k) != previous.get(k)
                for k in _PROGRESS
            ):
                self._moved_at = sample.at
        elif self._moved_at is None:
            self._moved_at = sample.at
        self._last = sample

    def restarts_since(self, moment: float) -> int:
        return sum(1 for at in self._restarts if at >= moment)

    def verdict(self, outstanding_since: float, now: Optional[float] = None) -> str:
        moment = self.clock() if now is None else now
        last = self._last
        if self.restarts_since(outstanding_since) > 0:
            return REASON_RESTARTED
        if self._failed or last is None:
            return "unknown"
        present = sum(1 for k in _WITNESS_METRICS if last.get(k) is not None)
        if present < 2:
            return "unknown"
        running = last.get("running") or 0.0
        if self._idle_scrapes >= 2 and moment - outstanding_since >= WITNESS_LOST_MIN_S:
            return REASON_LOST
        if running > 0:
            if self._moved_at is not None and moment - self._moved_at < WITNESS_STALL_S:
                return "progressing"
            return REASON_STALLED
        return "unknown"


class SidecarGuard:
    """The router/OCR twin of `MainGuard`, with the same tick interface so
    `streaming.Generation` pumps both the same way."""

    def __init__(self, witness: Optional[SidecarWitness], *, clock: Clock = time.monotonic) -> None:
        self.witness = witness
        self.clock = clock
        self.created_at = clock()
        self.dispatched_at: Optional[float] = None
        self.last_chunk_at: Optional[float] = None
        self.new_tokens = 0
        self.max_evidence_gap_s = 0.0

    evidence_covered = True

    def dispatched(self, now: Optional[float] = None) -> None:
        if self.dispatched_at is None:
            self.dispatched_at = self.clock() if now is None else now

    def chunk(self, now: Optional[float] = None, *, token: bool = True) -> None:
        moment = self.clock() if now is None else now
        if self.dispatched_at is None:
            self.dispatched_at = moment
        self.last_chunk_at = moment
        if token:
            self.new_tokens += 1

    def silence(self, now: float) -> float:
        anchor = self.last_chunk_at if self.last_chunk_at is not None else self.dispatched_at
        return 0.0 if anchor is None else max(0.0, now - anchor)

    def head_changed(self) -> bool:
        return False

    def tick(self, now: Optional[float] = None) -> Verdict:
        moment = self.clock() if now is None else now
        if self.dispatched_at is None:
            return OK
        outstanding = self.last_chunk_at if self.last_chunk_at is not None else self.dispatched_at
        verdict = "unknown" if self.witness is None else self.witness.verdict(outstanding, moment)
        if verdict in (REASON_STALLED, REASON_LOST, REASON_RESTARTED):
            return Verdict(True, verdict, verdict == REASON_RESTARTED, f"sidecar witness: {verdict}")
        if verdict == "unknown" and self.silence(moment) >= sidecar_silence_s():
            return Verdict(True, REASON_BLIND, False, "sidecar silent with no witness")
        return OK

    def classify_error(self, now: Optional[float] = None, exc: Optional[BaseException] = None) -> Verdict:
        """A router/OCR call that raised (2026-09-14 review, high). Until
        this date every error was a non-implicated 'connection', so a prompt
        that crashed the router mid-prefill was re-dispatched until three
        stalls (18 router crashes per user call at SDK max_retries 5), and a
        deterministic 5xx looped on connect backoff for the whole grace.

        * connection refused / connect timeout, or HTTP 502/503/504 →
          'connect': the request never reached the model; backoff and the
          sidecar grace apply, nothing is counted.
        * any other HTTP 5xx → 'engine_error': the engine answered; counted
          as a silent end (re-dispatch once, then fail retryably).
        * the witness saw the process restart since dispatch, or the
          connection broke with the call outstanding (a protocol/read error
          after connecting) → 'restarted', IMPLICATED: a second one fails the
          run with should_retry false.
        * a read timeout (the 1,800 s silence fallback) → 'blind'."""
        moment = self.clock() if now is None else now
        kind = sidecar_error_kind(exc)
        if kind == "connect":
            return Verdict(True, REASON_CONNECT, False, "sidecar refused the connection")
        if kind == "status":
            return Verdict(True, REASON_ENGINE_ERROR, False, "sidecar answered with a server error")
        if kind == "timeout":
            return Verdict(True, REASON_BLIND, False, "sidecar silent for the read timeout")
        outstanding = self.dispatched_at if self.dispatched_at is not None else moment
        if self.witness is not None and self.witness.restarts_since(outstanding) > 0:
            return Verdict(True, REASON_RESTARTED, True, "sidecar restarted with the call outstanding")
        return Verdict(True, REASON_RESTARTED, True, "sidecar connection broke with the call outstanding")


_CONNECT_NAMES = frozenset({"ConnectError", "ConnectTimeout", "PoolTimeout", "ConnectionRefusedError"})
_READ_TIMEOUT_NAMES = frozenset({"ReadTimeout", "TimeoutError", "APITimeoutError"})
_UNAVAILABLE_STATUSES = frozenset({502, 503, 504})


def _error_chain(exc: Optional[BaseException]) -> list:
    chain: list = []
    seen: set = set()
    current = exc
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(current)
        last = getattr(current, "last", None)  # resilience.ModelUnavailable
        current = last if isinstance(last, BaseException) else (current.__cause__ or current.__context__)
    return chain


def _status(exc: BaseException) -> Optional[int]:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def sidecar_error_kind(exc: Optional[BaseException]) -> str:
    """'connect' | 'status' | 'timeout' | 'broken', from the whole cause
    chain (resilience wraps the engine's error in ModelUnavailable.last; the
    OpenAI client raises APIConnectionError from the httpx error)."""
    chain = _error_chain(exc)
    for item in chain:
        status = _status(item)
        if status is not None and status >= 500:
            return "connect" if status in _UNAVAILABLE_STATUSES else "status"
    names = {type(item).__name__ for item in chain}
    if names & _CONNECT_NAMES or any(isinstance(item, ConnectionRefusedError) for item in chain):
        return "connect"
    if names & _READ_TIMEOUT_NAMES:
        return "timeout"
    return "broken"


MetricsFetch = Callable[[str], Awaitable[Optional[str]]]


async def _http_metrics(root: str) -> Optional[str]:
    try:
        import httpx
        from ..core.net import shared_ssl_context

        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0), verify=shared_ssl_context()) as client:
            response = await client.get(f"{root.rstrip('/')}/metrics")
            if response.status_code != 200:
                return None
            return response.text
    except Exception:  # noqa: BLE001 - a failed scrape is "unknown"
        return None


class WitnessSampler:
    """One reference-counted scrape loop per engine key (every 15 s)."""

    SCRAPE_S = 15.0

    def __init__(self, *, fetch: MetricsFetch = _http_metrics, clock: Clock = time.monotonic) -> None:
        self.fetch = fetch
        self.clock = clock
        self._entries: Dict[str, Dict[str, Any]] = {}

    def acquire(self, key: str, root: str) -> SidecarWitness:
        entry = self._entries.get(key)
        if entry is None or entry["task"].done():
            witness = SidecarWitness(clock=self.clock)
            entry = {"witness": witness, "refs": 0, "root": root}
            entry["task"] = asyncio.ensure_future(self._loop(entry))
            self._entries[key] = entry
        entry["refs"] += 1
        return entry["witness"]

    def release(self, key: str) -> None:
        entry = self._entries.get(key)
        if entry is None:
            return
        entry["refs"] -= 1
        if entry["refs"] <= 0:
            entry["task"].cancel()
            self._entries.pop(key, None)

    async def scrape_once(self, entry: Mapping[str, Any]) -> None:
        text = await self.fetch(str(entry["root"]))
        entry["witness"].observe(None if text is None else parse_metrics(text, self.clock()))

    async def _loop(self, entry: Dict[str, Any]) -> None:
        while True:
            await self.scrape_once(entry)
            await asyncio.sleep(self.SCRAPE_S)


_sampler: Optional[WitnessSampler] = None


def sampler() -> WitnessSampler:
    global _sampler
    if _sampler is None:
        _sampler = WitnessSampler()
    return _sampler


__all__ = [
    "AttemptEnd",
    "CounterChange",
    "EngineDownExpired",
    "EngineDownGrace",
    "EngineStateView",
    "EngineView",
    "MainGuard",
    "MetricsSample",
    "ServingTracker",
    "SidecarGuard",
    "SidecarWitness",
    "Verdict",
    "WitnessSampler",
    "attempt_counts",
    "default_view",
    "parse_metrics",
    "sampler",
    "sidecar_error_kind",
    "wait_not_bad",
]
