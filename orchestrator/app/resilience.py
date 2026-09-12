"""Surviving a model-engine outage — the wait-and-retry primitive.

WHY THIS EXISTS. The main model is one vLLM engine tensor-parallel across two
DGX Sparks, and when either rank faults the pair reloads: measured on
2026-09-10, 21:31:42Z worker CUDA fault → head container restart 21:38:25Z →
first token 21:47Z, i.e. ~13 minutes of "connection refused" on vllm:8000.
Every caller in llm.py went straight to an ``AsyncOpenAI`` client with
``max_retries=0`` (deliberately: the SDK also retries READ timeouts, and one
slow call silently became three re-runs of minutes of GPU work). So a chat
died in 0.1 s with ``openai.APIConnectionError``, a video job's required
fusion stage stamped the row ``failed`` with nothing to re-run it, and Deep
Research closed its run with the raw text "Connection error.".

The shape of the fix is one wrapper, ``resilient()``, that every model call in
llm.py opens through:

    job  →  model unavailable  →  wait_for_engine() polls /health  →
    engine answers 200  →  the call is re-issued  →  job resumes

with bounded exponential backoff + jitter between attempts, a hard ceiling on
the whole recovery window, and a classification that retries ONLY what a
restart can fix — connection refused/reset, a connect timeout, a 5xx from an
engine that has just died, the ``{"error": ...}`` chunk a dying stream sends
before any token — and NEVER a 4xx (a bad request is bad again on the next
try), NEVER a read timeout (that is a generation that ran the whole wall
clock and must not be re-run), NEVER a cancellation.

TWO WINDOWS, NOT ONE. A background job (video analysis, a sweep) should wait
out a full engine reload: ``LLM_RECOVERY_WINDOW_S`` (20 min) covers the
measured 13 with margin. A person watching a chat should not stare at a
spinner for 20 minutes: ``LLM_INTERACTIVE_RECOVERY_S`` (2 min) absorbs the
blips — an API-server restart, an NCCL re-init — and then fails fast with the
MODEL_UNAVAILABLE sentence the client already knows how to show. The window
is a ContextVar so a job sets it once (``recovery_window(...)``) and every
model call it makes, however deep, inherits it.

STREAMS: only the OPEN of a stream is retried. Once a token has been yielded
the caller has already forwarded it, and re-opening would duplicate text; a
stream that dies mid-body surfaces as the transport error it is.

THE BREAKER (2026-09-12, docs/availability/CONTRACT.md §8.2). The outage of
2026-09-11 22:15Z did not look like the one above: the head kept answering
/health with 200 while every completion hung on a dead worker rank, so a
wait that trusts /health waited on a port that was never going to answer.
``resilient()`` now consults the engine's circuit breaker (app/breaker.py)
BEFORE every attempt: while it is OPEN nobody touches the engine — no
call, and no /health poll either — and only the breaker's single HALF_OPEN
canary does. Every outcome is reported back to it, classified with the
bounded reasons of CONTRACT §4 — including a stream that dies in its body,
which the wrapper sees through ``GuardedStream``: a streaming permit is
settled by the FIRST CHUNK, not by the response headers vLLM sends before
the engine has scheduled anything. The retry semantics above are
unchanged: what was retried is still retried, what was never retried
still is not.

THE WAIT IS EVENT-DRIVEN (v2, CONTRACT §8.3). A caller held at the gate
sleeps on the engine-state client's READY event (app/engine_state.py) and
re-asks the breaker on every tick — it never polls the dead port. The
/health poll survives only as the fallback for a controller that is
unreachable or stale. On a chat turn the wait is a DURABLE queue
(app/continuity.py): the V29 row says `queued`, the person reads one exact
sentence, the window is LLM_QUEUE_MAX_WAIT_S, and its end parks the row
for the resume sweep instead of failing the request. Nothing is ever
answered by another model.

EVIDENCE TRAIL. Container logs on this cluster rotate away the very windows
that matter (the head's json log lost 2026-09-10 14:00Z-22:08Z entirely), so
every wait and every give-up is ALSO counted in Prometheus
(``llm_engine_wait_seconds``, ``llm_retry_total``,
``llm_engine_unavailable_total``), which has 15 days of retention, and the log
lines carry one greppable prefix, ``llm.resilient``.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from contextvars import ContextVar
from typing import Awaitable, Callable, Iterator, Optional, TypeVar

from . import breaker as _breaker
from . import continuity as _continuity
from . import engine_state, metrics
from .breaker import BreakerOpen
from .config import settings
from .context import service_root

log = logging.getLogger(__name__)

T = TypeVar("T")

#: The recovery window for the CURRENT task. None → the interactive default.
_RECOVERY_S: ContextVar[Optional[float]] = ContextVar("_llm_recovery_s", default=None)

#: Optional per-task notifier: called with a one-line human status while the
#: wrapper waits, so a chat can show "the model is restarting" instead of a
#: silent spinner. Set by the chat worker; a job that has no one watching
#: leaves it unset.
class _Notifier:
    """The callback plus whether it has already spoken for this outage.

    A chat turn is several model calls — the route classification and the
    answer run in sibling tasks and wait on the same outage — and the person
    should read the line once. Child tasks get a COPY of the context but the
    same holder object, so the flag is shared where a plain ContextVar[bool]
    would not be.
    """

    __slots__ = ("fn", "announced")

    def __init__(self, fn: Callable[[str], Awaitable[None]]) -> None:
        self.fn = fn
        self.announced = False


_NOTIFY: ContextVar[Optional[_Notifier]] = ContextVar("_llm_wait_notify", default=None)


class ModelUnavailable(RuntimeError):
    """The engine stayed unreachable for the whole recovery window.

    Carries what an operator needs to size the window and what a job runner
    needs to decide "defer" rather than "fail": how long was waited, how many
    attempts were made, and the last transport-level error.
    """

    def __init__(self, base_url: str, waited_s: float, attempts: int, last: BaseException):
        self.base_url = base_url
        self.waited_s = waited_s
        self.attempts = attempts
        self.last = last
        super().__init__(
            f"model at {base_url} unavailable after {waited_s:.0f}s and {attempts} attempt(s): "
            f"{type(last).__name__}: {str(last)[:160]}"
        )


@contextlib.contextmanager
def recovery_window(seconds: Optional[float]) -> Iterator[None]:
    """Run a block with a specific recovery window for every model call in it.

    ``None`` restores the interactive default. Used by long-running jobs
    (``video/pipeline.py``) around the stage that may wait out a reload.
    """
    token = _RECOVERY_S.set(seconds)
    try:
        yield
    finally:
        _RECOVERY_S.reset(token)


@contextlib.contextmanager
def wait_notifier(notify: Optional[Callable[[str], Awaitable[None]]]) -> Iterator[None]:
    """Route the wrapper's "waiting for the model" line to ``notify``."""
    token = _NOTIFY.set(_Notifier(notify) if notify is not None else None)
    try:
        yield
    finally:
        _NOTIFY.reset(token)


def set_wait_notifier(notify: Optional[Callable[[str], Awaitable[None]]]) -> None:
    """Bind the notifier to the CURRENT task for the rest of its life — for a
    worker that owns its task (the chat worker) and has no block to wrap."""
    _NOTIFY.set(_Notifier(notify) if notify is not None else None)


def effective_recovery_s(engine: Optional[str] = None) -> float:
    """The window for a call on `engine` (a breaker name, or None for an
    engine without one): a window the task DECLARED wins; a chat turn
    waiting for the MAIN model gets the queue window (CONTRACT §8.3 step
    3); everything else the interactive default."""
    override = _RECOVERY_S.get()
    if override is not None:
        return max(0.0, float(override))
    interactive = max(0.0, float(settings.llm_interactive_recovery_s))
    if engine == _breaker.MAIN and _continuity.held():
        # Never shorter than the interactive window an operator sized.
        return max(interactive, _continuity.queue_window_s())
    return interactive


def declared_recovery_s() -> Optional[float]:
    """The window the current task DECLARED with ``recovery_window``, or None
    for the interactive default."""
    return _RECOVERY_S.get()


async def notify(line: str) -> None:
    """Send one status line through the task's wait notifier, if any.
    Silent when no one is watching; never raises."""
    holder = _NOTIFY.get()
    if holder is None:
        return
    with contextlib.suppress(Exception):
        await holder.fn(line)


#: The exact sentence for a caller held for the main model (CONTRACT §8.3).
#: Only the main engine earns it: a sidecar on a person's turn never waits
#: (sidecar_recovery_s), and a job has no one to tell. Defined in
#: app/continuity.py, which owns both sentences; re-exported here for the
#: gate.
QUEUED_LINE = _continuity.QUEUED_LINE

#: How often a queued caller re-asks the breaker. In-memory and cheap; short
#: enough that the HALF_OPEN → CLOSED moment admits the queue promptly. The
#: READY event wakes a sleeper earlier than this.
_BREAKER_GATE_POLL_S = 1.0


async def _gate_sleep(step: float) -> None:
    """Sleep at most `step` seconds — less when the controller says READY.

    The sleep and the READY event race; whichever ends first ends the wait.
    Looked up through this module's ``asyncio`` so a test that patches
    ``resilience.asyncio.sleep`` with a fake clock drives it too.
    """
    step = max(0.0, float(step))
    event = engine_state.ready_event()
    if event.is_set() or step == 0.0:
        await asyncio.sleep(step)
        return
    sleeper = asyncio.ensure_future(asyncio.sleep(step))
    waiter = asyncio.ensure_future(event.wait())
    try:
        await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for fut in (sleeper, waiter):
            if not fut.done():
                fut.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await fut


def _hold_for(engine: Optional[str]) -> Optional["_continuity.Hold"]:
    """The chat turn's hold — only the MAIN engine is ever queued for."""
    return _continuity.current() if engine == _breaker.MAIN else None


async def _tell_once(line: str) -> None:
    """The wrapper's own status line, once per outage, when a person is
    watching and no hold speaks for them."""
    holder = _NOTIFY.get()
    if holder is not None and not holder.announced:
        holder.announced = True
        with contextlib.suppress(Exception):
            await holder.fn(line)


def sidecar_recovery_s() -> float:
    """The window a SIDECAR call — the router, an embedding — may wait.

    The interactive default exists for the main model: the person is waiting
    for the answer itself and nothing can stand in for it. A sidecar on the
    chat path is different — every caller has a fallback (the route
    classifier falls back to a plain plan, recall and dense retrieval fall
    back to lexical-only, the Salesforce router to the main model) and those
    fallbacks cost well under a second. Waiting the interactive window on
    each of them turned a down embedding engine into a two-minute stall per
    turn — and a down router plus a down embedder into four — on turns the
    main model could have answered at once, with the person told "the model
    is restarting" about an engine that was not the model (review finding,
    2026-09-11; the embed engine has been down while the main model was up
    on 2026-09-09 and under the launcher RestartCount bug).

    So: on an interactive turn — recognised by the wait notifier the chat
    worker binds to its task — a sidecar makes ONE attempt and fails to its
    fallback. Anywhere else (an indexer, a title job, a video stage inside its
    ``recovery_window``) it waits like any other call, because there nobody
    is watching and the fallback is the worse outcome.
    """
    if _NOTIFY.get() is not None:
        return 0.0
    return effective_recovery_s()


# ---------------------------------------------------------------------------
# Classification. Verified against openai 2.36.0 (the container) and 3.7.0
# (the venv): the hierarchy is APIError → {APIConnectionError → APITimeoutError,
# APIStatusError → BadRequestError/…/InternalServerError}; the SDK wraps
# httpx.ConnectError/ConnectTimeout into APIConnectionError/APITimeoutError
# with the httpx error as __cause__; a stream that dies mid-body raises the
# RAW httpx error from the iterator, and an {"error":…} chunk raises a bare
# APIError. A read timeout is an APITimeoutError whose cause is
# httpx.ReadTimeout.
# ---------------------------------------------------------------------------


def _cause_chain(exc: BaseException):
    seen = 0
    while exc is not None and seen < 8:
        yield exc
        exc = exc.__cause__ or exc.__context__  # type: ignore[assignment]
        seen += 1


def is_read_timeout(exc: BaseException) -> bool:
    """A generation that ran the whole read timeout — NEVER re-run it."""
    import httpx

    return any(isinstance(e, httpx.ReadTimeout) for e in _cause_chain(exc))


def is_connection_error(exc: BaseException) -> bool:
    """The engine could not be reached at all (refused, reset, DNS, connect
    timeout). This is the class that means "wait for /health before retrying"."""
    import httpx

    try:
        import openai
    except ImportError:  # pragma: no cover — the SDK is a hard dependency
        openai = None  # type: ignore[assignment]
    if is_read_timeout(exc):
        return False
    if openai is not None and isinstance(exc, openai.APIConnectionError):
        return True
    return any(
        isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError,
                       httpx.ReadError, httpx.WriteError, httpx.PoolTimeout, ConnectionError))
        for e in _cause_chain(exc)
    )


#: vLLM validates a request AFTER the headers went out: a streamed request it
#: refuses (a prompt over the window, a bad sampling parameter, a tool schema
#: it cannot follow) arrives as a 200 followed by one {"error": {...}} chunk
#: carrying vLLM's own status ("code": 400) and type ("BadRequestError").
#: The SDK raises that chunk as a bare ``openai.APIError`` — the same shape
#: as an engine dying mid-stream — so the code and type inside the chunk are
#: what tell a client mistake (never retried, §4 'malformed') from a dead
#: engine (retried/queued). Bounded on purpose: only the numeric code and a
#: few type names are read; the message text never reaches a label.
_CLIENT_ERROR_TYPES = ("badrequest", "validation", "notfound", "invalid", "unprocessable")


def error_chunk_is_client_error(exc: BaseException) -> bool:
    """True when a bare APIError's chunk says the REQUEST was wrong (4xx)."""
    try:
        import openai
    except ImportError:  # pragma: no cover
        return False
    if not isinstance(exc, openai.APIError) or isinstance(exc, openai.APIStatusError):
        return False
    code = getattr(exc, "code", None)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        inner = body.get("error") if isinstance(body.get("error"), dict) else body
        code = code if code is not None else inner.get("code")
        kind = str(inner.get("type") or "").lower()
        if any(mark in kind for mark in _CLIENT_ERROR_TYPES):
            return True
    try:
        return 400 <= int(code) < 500
    except (TypeError, ValueError):
        return False


def is_recoverable(exc: BaseException) -> bool:
    """Whether a restart of the engine could make this call succeed.

    True: connection-class errors; 5xx and 429 status errors; a bare
    ``openai.APIError`` (the dying-stream shape). False: everything a client
    did wrong (4xx), a read timeout, a cancellation, and any non-model error.
    """
    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
        return False
    if is_read_timeout(exc):
        return False
    if is_connection_error(exc):
        return True
    try:
        import openai
    except ImportError:  # pragma: no cover
        return False
    if isinstance(exc, openai.APIStatusError):
        status = int(getattr(exc, "status_code", 0) or 0)
        return status >= 500 or status == 429
    if isinstance(exc, openai.APIError):
        # Not a status error and not a connection error: the engine answered
        # 200 and then sent an error chunk. EngineDeadError mid-stream is
        # recoverable; vLLM's own 4xx inside the chunk (a request it refused
        # after the headers) is the client's mistake and never retried.
        return not error_chunk_is_client_error(exc)
    return False


#: Substrings (lower-cased) in an engine's 5xx body or error chunk that say
#: the WORKER half of the pair died (the 2026-09-11 signatures, CONTRACT
#: §6.2); anything else on a 5xx is the engine itself. Bounded on purpose:
#: the reason becomes a metric label, so the text of the error must never
#: reach it.
_WORKER_LOST_MARKS = (
    "worker", "nccl", "misaligned address", "illegal memory access", "died unexpectedly",
)


def failure_reason(exc: BaseException) -> Optional[str]:
    """Classify one failed model call into the bounded CONTRACT §4 vocabulary
    for the breaker (`llm_breaker_failures_total{reason}`).

    None means "not the engine's fault" — a bug in our own code between the
    call and the return — and is not counted at all. The mapping is by the
    SDK's exception class and the HTTP status, with the error text consulted
    only to tell `worker_lost` from `engine_dead` on a 5xx; the text itself
    never leaves this function.
    """
    import httpx

    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
        return "cancelled"
    if is_read_timeout(exc):
        return "request_timeout"
    if any(isinstance(e, httpx.PoolTimeout) for e in _cause_chain(exc)):
        # Waited too long for a connection from our own pool: our queue, not
        # the engine's answer.
        return "queue_timeout"
    if is_connection_error(exc):
        return "connection"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        # A wall-clock guard around the call (chat_completion_with_reasoning):
        # the engine was working, just too slowly — the same class as a read
        # timeout.
        return "request_timeout"
    try:
        import openai
    except ImportError:  # pragma: no cover
        return None
    if isinstance(exc, openai.APIStatusError):
        status = int(getattr(exc, "status_code", 0) or 0)
        if status == 429:
            return "capacity"
        if status == 503:
            # vLLM answers 503 while the engine is not ready to serve.
            return "readiness"
        if status in (502, 504):
            # A proxy in front of a dead upstream.
            return "connection"
        if status >= 500:
            return _dead_half(exc)
        return "malformed"
    if isinstance(exc, openai.APIError):
        # The dying-stream shape: 200, then an {"error": …} chunk — unless
        # the chunk carries vLLM's own 4xx, which is a malformed request.
        if error_chunk_is_client_error(exc):
            return "malformed"
        return _dead_half(exc)
    return None


def _dead_half(exc: BaseException) -> str:
    text = " ".join(str(e) for e in _cause_chain(exc)).lower()
    if any(mark in text for mark in _WORKER_LOST_MARKS):
        return "worker_lost"
    return "engine_dead"


# ---------------------------------------------------------------------------
# Waiting for the engine
# ---------------------------------------------------------------------------


async def engine_answers(base_url: str, *, timeout: Optional[float] = None) -> bool:
    """One probe: GET {root}/health AND GET {root}/v1/models both 200.

    vLLM's /health is answered by the API process, which is why the head's
    watchdog trusts only a real completion; here the completion IS the retry
    that follows, so a cheap "the server is back up" is the right gate.
    """
    import httpx

    root = service_root(base_url)
    probe_timeout = float(timeout if timeout is not None else settings.health_probe_timeout)
    try:
        async with httpx.AsyncClient(timeout=probe_timeout) as client:
            health = await client.get(f"{root}/health")
            if health.status_code != 200:
                return False
            models = await client.get(f"{root}/v1/models")
            return models.status_code == 200
    except Exception:  # noqa: BLE001 — any transport failure is "not yet"
        return False


async def wait_for_engine(
    base_url: str,
    *,
    deadline_s: float,
    poll_s: Optional[float] = None,
    what: str = "",
    interrupt: Optional[Callable[[], bool]] = None,
    hold: Optional["_continuity.Hold"] = None,
) -> bool:
    """Wait until the engine is back or ``deadline_s`` elapses. True if it is.

    For the MAIN engine the controller's verdict comes first: while it is
    fresh and says the primary is not serving, the wait sleeps on the READY
    event and asks nothing of the port; when it says READY the retry that
    follows is the probe. Only an unreachable or stale controller falls
    back to polling ``/health`` + ``/v1/models`` (the pre-controller
    behaviour, and the whole story for engines without a breaker).

    Logs once a minute (not once per poll) with the ``llm.resilient`` prefix
    and observes the total wait in ``llm_engine_wait_seconds``. On a chat
    turn the wait is durable (app/continuity.py): the row says `queued` and
    the person reads QUEUED_LINE once.

    ``interrupt`` is asked before every poll; when it says True the wait
    ends early with False. ``resilient`` passes the breaker: the moment it
    opens, a /health that still answers 200 is no longer worth asking
    (that is exactly the 2026-09-11 shape), and the caller goes back to
    the breaker's gate instead. ``hold`` is the chat turn's hold when the
    caller owns one (``resilient`` decides: a call with no window never
    enters it); a direct caller of this function waits without one.
    """
    poll = float(poll_s if poll_s is not None else settings.llm_health_poll_s)
    started = time.monotonic()
    last_log = started
    engine = _breaker.engine_for_base_url(base_url)
    main = engine == _breaker.MAIN

    def _observe(outcome: str) -> None:
        metrics.observe("llm_engine_wait_seconds", time.monotonic() - started,
                        "Seconds spent waiting for a model engine to come back", outcome=outcome)

    while True:
        if interrupt is not None and interrupt():
            _observe("interrupted")
            return False
        verdict = engine_state.serving() if main else None
        if verdict is True or (verdict is None and await engine_answers(base_url)):
            waited = time.monotonic() - started
            if waited > 0.5:
                log.info("llm.resilient what=%s base_url=%s engine back after %.1fs (%s)",
                         what, base_url, waited, "controller READY" if verdict else "/health")
            _observe("recovered")
            holder = _NOTIFY.get()
            if holder is not None:
                holder.announced = False
            return True
        elapsed = time.monotonic() - started
        if elapsed >= deadline_s:
            _observe("gave_up")
            return False
        if hold is not None:
            await hold.enter(_continuity.RECOVERY)
        elif main:
            await _tell_once(QUEUED_LINE)
        now = time.monotonic()
        if now - last_log >= 60.0:
            last_log = now
            log.warning("llm.resilient what=%s base_url=%s still waiting for the engine (%.0fs of %.0fs, %s)",
                        what, base_url, elapsed, deadline_s,
                        "controller says not serving" if verdict is False else "polling /health")
        if interrupt is not None and interrupt():
            # Asked again here, not only at the top: the breaker may have
            # opened during the probe itself, and a poll interval is a long
            # time to keep asking a port that is known to be lying.
            _observe("interrupted")
            return False
        step = min(poll, max(0.0, deadline_s - elapsed))
        if verdict is False:
            await _gate_sleep(step)  # woken early by READY
        else:
            await asyncio.sleep(step)


def _backoff_s(attempt: int) -> float:
    """Exponential with full jitter: 2, 4, 8, 16, 30, 30 … seconds (×0.5-1.0)."""
    base = float(settings.llm_retry_base_s) * (2 ** max(0, attempt - 1))
    capped = min(float(settings.llm_retry_cap_s), base)
    return capped * (0.5 + random.random() / 2)


async def _give_up(
    hold: Optional["_continuity.Hold"], *, what: str, base_url: str, waited_s: float,
    attempts: int, last: BaseException, reason: str,
) -> None:
    """The window closed. A chat turn's request is PARKED, not failed
    (CONTRACT §8.3 step 5: the row stays `queued`, the person reads the
    second sentence, the sweep resumes it) — QueuedForRecovery. Anything
    else gets ModelUnavailable, the give-up every job already handles.

    ``hold`` is None for a call that does not own the turn's hold (a
    sidecar with no window, a job): such a call can never park the turn
    (round-2 review, resilience.py:549). The owning call parks whenever
    it is still waiting at the gate — and also when its window closed on
    a failed attempt while the engine is (now) away: the breaker refuses
    or the controller says not serving. A person is never told
    MODEL_UNAVAILABLE for an engine that is being waited out (§8.3 v2).
    """
    metrics.inc("llm_engine_unavailable_total",
                "Model calls abandoned after the recovery window", what=what, reason=reason)
    log.error("llm.resilient what=%s base_url=%s attempt=%d waited_s=%.0f GIVING UP: %s: %s",
              what, base_url, attempts, waited_s, type(last).__name__, str(last)[:200])
    if hold is not None:
        if hold.waiting:
            await hold.expire()  # raises QueuedForRecovery
        brk = _breaker.for_base_url(base_url)
        if brk is not None and (not brk.allows() or engine_state.serving() is False):
            await hold.enter(_continuity.RECOVERY, line=None)  # the row goes `queued`; EXPIRED_LINE follows
            await hold.expire()  # raises QueuedForRecovery
    raise ModelUnavailable(base_url, waited_s, attempts, last) from last


async def _admit(
    brk: "_breaker.Breaker",
    *,
    what: str,
    base_url: str,
    started: float,
    window: float,
    attempts: int,
    hold: Optional["_continuity.Hold"] = None,
) -> "_breaker.Permit":
    """Hold the caller at the breaker until it is admitted.

    Returns the permit the outcome must be recorded against; raises when
    the window closes first (see ``_give_up``). While held, nobody touches
    the engine: the caller sleeps on the READY event and re-asks the
    breaker in memory. On a chat turn the hold is durable — the row says
    `queued` and the person is told once (CONTRACT §8.3); the moment the
    breaker admits the call, the SAME generation resumes as a new attempt.
    The log says so once a minute, never once per tick. ``hold`` is the
    turn's hold when the caller owns one (``resilient`` decides).
    """
    last_log = time.monotonic()
    while True:
        permit = brk.acquire()
        if permit is not None:
            if hold is not None and hold.waiting:
                await hold.resume()
            return permit
        elapsed = time.monotonic() - started
        remaining = window - elapsed
        if remaining <= 0:
            refused = BreakerOpen(brk.engine, brk.state)
            await _give_up(hold, what=what, base_url=base_url, waited_s=elapsed,
                           attempts=attempts, last=refused, reason="breaker_open")
        if hold is not None:
            await hold.enter(_continuity.RECOVERY)
        elif brk.engine == _breaker.MAIN:
            await _tell_once(QUEUED_LINE)
        now = time.monotonic()
        if now - last_log >= 60.0:
            last_log = now
            log.warning("llm.resilient what=%s base_url=%s queued behind breaker %s=%s (%.0fs of %.0fs)",
                        what, base_url, brk.engine, brk.state, elapsed, window)
        await _gate_sleep(min(_BREAKER_GATE_POLL_S, remaining))


#: "No chunk held" — distinct from None, which a stream may legitimately yield.
_NO_CHUNK = object()


async def wait_admitted(*, what: str, base_url: str, recovery_s: Optional[float] = None) -> None:
    """Wait at the breaker's gate WITHOUT making a call — for a caller that
    must know the engine is admitting before it spends anything of its own
    (Deep Research parks before its stage budgets start; the prompt sizing
    asks /tokenize only of an engine the breaker admits). Same semantics as
    the gate inside ``resilient``: a chat turn queues on its hold and, at
    the window, parks (QueuedForRecovery); anything else gets
    ModelUnavailable. Returns at once for an engine with no breaker or a
    breaker that admits. Acquires no permit: the call that follows does."""
    brk = _breaker.for_base_url(base_url)
    if brk is None or brk.allows():
        return
    window = float(recovery_s) if recovery_s is not None else effective_recovery_s(brk.engine)
    hold = _hold_for(brk.engine) if window > 0 else None
    started = time.monotonic()
    last_log = started
    try:
        while not brk.allows():
            elapsed = time.monotonic() - started
            remaining = window - elapsed
            if remaining <= 0:
                refused = BreakerOpen(brk.engine, brk.state)
                await _give_up(hold, what=what, base_url=base_url, waited_s=elapsed,
                               attempts=0, last=refused, reason="breaker_open")
            if hold is not None:
                await hold.enter(_continuity.RECOVERY)
            elif brk.engine == _breaker.MAIN:
                await _tell_once(QUEUED_LINE)
            now = time.monotonic()
            if now - last_log >= 60.0:
                last_log = now
                log.warning("llm.resilient what=%s base_url=%s queued behind breaker %s=%s (%.0fs of %.0fs)",
                            what, base_url, brk.engine, brk.state, elapsed, window)
            await _gate_sleep(min(_BREAKER_GATE_POLL_S, remaining))
        if hold is not None and hold.waiting:
            await hold.resume()
    except BaseException:
        if hold is not None and hold.waiting:
            hold.abandon()
        raise


class GuardedStream:
    """A streamed completion whose FIRST CHUNK settles the breaker permit.

    vLLM sends the response headers before the engine has scheduled the
    request, so ``create(stream=True)`` returning proves nothing about the
    engine (the 2026-09-11 wedge answered every open and sent no token).
    The permit — and, for a HALF_OPEN canary, the breaker's next state — is
    therefore decided by the first body chunk, which ``prime()`` pulls
    INSIDE the retry loop's attempt (round-2 review, resilience.py:663):
    the chunk records success and is replayed to the consumer; an
    exception before it is the attempt's failure — recorded against the
    permit (a canary re-opens the breaker) and retried or queued like a
    refused connection, never handed to the consumer; an exception after
    it is counted as an ordinary failure (a stream is never re-opened
    after a token, CONTRACT §8.4); a stream that ends or is closed before
    any chunk releases the permit so a HALF_OPEN breaker is not pinned by
    a probe nobody read.

    Iterates exactly like the stream it wraps and forwards ``close()`` (or
    ``aclose()``: the SDK's stream has the former, an async generator the
    latter).
    """

    __slots__ = ("_stream", "_brk", "_permit", "_settled", "_iter", "_holder", "_hold", "_first", "exhausted")

    def __init__(self, stream, brk: Optional["_breaker.Breaker"], permit, holder, hold=None) -> None:
        self._stream = stream
        self._brk = brk
        self._permit = permit
        self._settled = False
        self._iter = None
        self._holder = holder
        #: The chat turn's hold, when the opening call owned one.
        self._hold = hold
        #: The first chunk, pulled by ``prime()`` inside the retry loop and
        #: replayed by the first ``__anext__``.
        self._first = _NO_CHUNK
        #: True once the wrapped stream said StopAsyncIteration: a consumer
        #: that closes an exhausted stream "to be safe" closes nothing.
        self.exhausted = False

    async def prime(self) -> None:
        """Pull the FIRST chunk now, inside the attempt that opened the
        stream (CONTRACT §8.4: a retry is allowed before the first token).
        vLLM sends the headers before the engine has scheduled anything, so
        a stream that dies before its first chunk — the head restarted
        under it, the dying-stream error chunk — is a failed OPEN: the
        exception propagates to ``_resilient``, which records it against
        the permit and retries or queues exactly as it would a refused
        connection (round-2 review, resilience.py:663). An empty stream
        proves nothing and releases the permit, as before."""
        if self._iter is None:
            self._iter = self._stream.__aiter__()
        try:
            chunk = await self._iter.__anext__()
        except StopAsyncIteration:
            self.exhausted = True
            self._release()
            return
        except BaseException:
            # Settled by the caller's record_failure(permit); a later close()
            # must not hand the permit back a second time. The wrapped
            # stream is closed here because no consumer ever receives it.
            self._settled = True
            closer = getattr(self._stream, "close", None) or getattr(self._stream, "aclose", None)
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer()
            raise
        self._succeed()
        self._first = chunk

    @property
    def stream(self):
        """The wrapped stream, for a caller that needs the SDK object."""
        return self._stream

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._first is not _NO_CHUNK:
            chunk, self._first = self._first, _NO_CHUNK
            return chunk
        if self.exhausted:
            raise StopAsyncIteration
        if self._iter is None:
            self._iter = self._stream.__aiter__()
        try:
            chunk = await self._iter.__anext__()
        except StopAsyncIteration:
            self.exhausted = True
            self._release()
            raise
        except BaseException as exc:  # noqa: BLE001 — classified, always re-raised
            self._fail(exc)
            raise
        self._succeed()
        return chunk

    def _succeed(self) -> None:
        if self._settled:
            return
        self._settled = True
        if self._brk is not None:
            self._brk.record_success(self._permit)
        if self._holder is not None:
            self._holder.announced = False  # the engine served: the next outage earns a new line
        if self._hold is not None:
            self._hold.served()

    def _fail(self, exc: BaseException) -> None:
        if self._brk is None:
            return
        if self._settled:
            # After a token: counted (the engine died mid-answer), but no
            # permit to settle and never a retry.
            self._brk.record_failure(failure_reason(exc))
            return
        self._settled = True
        self._brk.record_failure(failure_reason(exc), permit=self._permit)

    def _release(self) -> None:
        """A stream that ended (or was closed) before any chunk proved
        nothing: the permit is handed back, not counted either way."""
        if self._settled:
            return
        self._settled = True
        if self._brk is not None:
            self._brk.release(self._permit)

    async def close(self) -> None:
        """Close the wrapped stream (a no-op once it is exhausted: the SDK
        already released the connection, and a consumer's safety close must
        not read as a forced closure)."""
        self._release()
        if self.exhausted:
            return
        closer = getattr(self._stream, "close", None) or getattr(self._stream, "aclose", None)
        if closer is not None:
            await closer()

    async def aclose(self) -> None:
        await self.close()


async def resilient(
    op: Callable[[], Awaitable[T]],
    *,
    what: str,
    base_url: str,
    recovery_s: Optional[float] = None,
    stream: bool = False,
) -> T:
    """Run ``op`` (a zero-argument coroutine factory); wait and retry through
    a recoverable model outage, up to the recovery window.

    ``recovery_s=None`` uses the task's ``recovery_window`` (or the
    interactive default — or, for a chat turn waiting on the main model,
    the queue window); ``0`` means one attempt, no waiting — for callers
    with their own short budget (the embed query's 4 s).

    ``stream=True`` says ``op`` opens a streamed completion: the result is
    returned wrapped in ``GuardedStream`` so the breaker learns its
    outcome from the body (first chunk = success, an exception = the
    failure it is) instead of from the headers. Engines without a breaker
    (embeddings, OCR, the router) behave exactly as before either way.
    """
    brk = _breaker.for_base_url(base_url)
    window = float(recovery_s) if recovery_s is not None else effective_recovery_s(brk.engine if brk else None)
    # The turn's hold belongs to a call that has a window to wait — the
    # answer and its siblings. A sidecar on the main URL (window 0,
    # sidecar_recovery_s) never enters it and never expires it: it makes
    # its one attempt and fails to its own fallback (round-2 review,
    # resilience.py:549).
    hold = _hold_for(brk.engine) if brk is not None and window > 0 else None
    try:
        return await _resilient(op, brk=brk, hold=hold, window=window, what=what, base_url=base_url, stream=stream)
    except BaseException:
        # Cancelled (a Stop, a teardown) or failed while HELD: the hold's
        # gauge must not keep counting a wait that is over. The row's
        # terminal status is whoever ended the turn's to write.
        if hold is not None and hold.waiting:
            hold.abandon()
        raise


async def _resilient(op, *, brk, hold, window: float, what: str, base_url: str, stream: bool):
    started = time.monotonic()
    attempt = 0
    while True:
        permit = None
        if hold is not None and hold.lost is not None:
            # The row belongs to another resumer's generation now: this
            # turn must not answer, whatever swallowed the first raise.
            raise hold.lost
        if brk is not None:
            # An OPEN breaker is consulted BEFORE the first attempt: a dead
            # engine sees no request from here, only the breaker's canary.
            permit = await _admit(brk, what=what, base_url=base_url, started=started,
                                  window=window, attempts=attempt, hold=hold)
        attempt += 1
        try:
            result = await op()
            if stream:
                # The first chunk is part of the OPEN (GuardedStream.prime):
                # a body that dies before it is this attempt's failure and
                # is retried or queued below, never handed to the caller.
                result = GuardedStream(result, brk, permit, _NOTIFY.get(), hold)
                await result.prime()
        except BaseException as exc:  # noqa: BLE001 — classified below, re-raised when not ours
            if brk is not None:
                brk.record_failure(failure_reason(exc), permit=permit)
            if not is_recoverable(exc):
                raise
            elapsed = time.monotonic() - started
            remaining = window - elapsed
            reason = "connection" if is_connection_error(exc) else "engine_error"
            if remaining <= 0:
                await _give_up(hold, what=what, base_url=base_url, waited_s=elapsed,
                               attempts=attempt, last=exc, reason=reason)
            metrics.inc("llm_retry_total", "Model calls retried through a recoverable error",
                        what=what, reason=reason)
            log.warning("llm.resilient what=%s base_url=%s attempt=%d waited_s=%.0f retrying after %s: %s",
                        what, base_url, attempt, elapsed, type(exc).__name__, str(exc)[:200])
            if reason == "connection" and (brk is None or brk.allows()):
                # Do not hammer a port that is refusing: wait until the
                # engine is back (or the window closes), THEN back off briefly
                # so the first real request lands after the API's own warm-up.
                # Unless the breaker has opened meanwhile — then nothing is
                # polled either; the gate above waits on the breaker.
                blocked = (lambda: not brk.allows()) if brk is not None else None
                if not await wait_for_engine(base_url, deadline_s=remaining, what=what, interrupt=blocked,
                                             hold=hold):
                    if brk is not None and not brk.allows():
                        # Interrupted by the breaker, not by the clock: back to
                        # the top, where the gate queues.
                        continue
                    await _give_up(hold, what=what, base_url=base_url,
                                   waited_s=time.monotonic() - started, attempts=attempt,
                                   last=exc, reason=reason)
            pause = min(_backoff_s(attempt), max(0.0, window - (time.monotonic() - started)))
            if pause > 0:
                await asyncio.sleep(pause)
        else:
            if hold is not None and hold.waiting:
                # A wait that ended through the engine wait (not the gate):
                # the row goes on as the same generation, new attempt.
                await hold.resume()
            if stream:
                # Settled by its first chunk, already pulled (prime above).
                return result
            if brk is not None:
                brk.record_success(permit)
                holder = _NOTIFY.get()
                if holder is not None:
                    # The engine served: the next outage earns a new line.
                    holder.announced = False
                if hold is not None:
                    hold.served()
            return result
