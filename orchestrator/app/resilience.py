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

from . import metrics
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
_NOTIFY: ContextVar[Optional[Callable[[str], Awaitable[None]]]] = ContextVar(
    "_llm_wait_notify", default=None
)


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
    token = _NOTIFY.set(notify)
    try:
        yield
    finally:
        _NOTIFY.reset(token)


def set_wait_notifier(notify: Optional[Callable[[str], Awaitable[None]]]) -> None:
    """Bind the notifier to the CURRENT task for the rest of its life — for a
    worker that owns its task (the chat worker) and has no block to wrap."""
    _NOTIFY.set(notify)


def effective_recovery_s() -> float:
    override = _RECOVERY_S.get()
    if override is not None:
        return max(0.0, float(override))
    return max(0.0, float(settings.llm_interactive_recovery_s))


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
        # 200 and then sent an error chunk (EngineDeadError mid-stream) — the
        # request itself was fine.
        return True
    return False


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
) -> bool:
    """Poll until the engine answers or ``deadline_s`` elapses. True if it did.

    Logs once a minute (not once per poll) with the ``llm.resilient`` prefix
    and observes the total wait in ``llm_engine_wait_seconds``.
    """
    poll = float(poll_s if poll_s is not None else settings.llm_health_poll_s)
    started = time.monotonic()
    last_log = started
    notified = False
    while True:
        if await engine_answers(base_url):
            waited = time.monotonic() - started
            if waited > 0.5:
                log.info("llm.resilient what=%s base_url=%s engine back after %.1fs", what, base_url, waited)
            metrics.observe("llm_engine_wait_seconds", waited,
                            "Seconds spent waiting for a model engine to answer /health", outcome="recovered")
            return True
        elapsed = time.monotonic() - started
        if elapsed >= deadline_s:
            metrics.observe("llm_engine_wait_seconds", elapsed,
                            "Seconds spent waiting for a model engine to answer /health", outcome="gave_up")
            return False
        notify = _NOTIFY.get()
        if notify is not None and not notified:
            notified = True
            with contextlib.suppress(Exception):
                await notify(
                    "The model is restarting — waiting for it to come back "
                    f"(up to {int(deadline_s) // 60 or 1} min)…"
                )
        now = time.monotonic()
        if now - last_log >= 60.0:
            last_log = now
            log.warning("llm.resilient what=%s base_url=%s still waiting for the engine (%.0fs of %.0fs)",
                        what, base_url, elapsed, deadline_s)
        await asyncio.sleep(min(poll, max(0.0, deadline_s - elapsed)))


def _backoff_s(attempt: int) -> float:
    """Exponential with full jitter: 2, 4, 8, 16, 30, 30 … seconds (×0.5-1.0)."""
    base = float(settings.llm_retry_base_s) * (2 ** max(0, attempt - 1))
    capped = min(float(settings.llm_retry_cap_s), base)
    return capped * (0.5 + random.random() / 2)


async def resilient(
    op: Callable[[], Awaitable[T]],
    *,
    what: str,
    base_url: str,
    recovery_s: Optional[float] = None,
) -> T:
    """Run ``op`` (a zero-argument coroutine factory); wait and retry through
    a recoverable model outage, up to the recovery window.

    ``recovery_s=None`` uses the task's ``recovery_window`` (or the
    interactive default); ``0`` means one attempt, no waiting — for callers
    with their own short budget (the embed query's 4 s).
    """
    window = float(recovery_s) if recovery_s is not None else effective_recovery_s()
    started = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            return await op()
        except BaseException as exc:  # noqa: BLE001 — classified below, re-raised when not ours
            if not is_recoverable(exc):
                raise
            elapsed = time.monotonic() - started
            remaining = window - elapsed
            reason = "connection" if is_connection_error(exc) else "engine_error"
            if remaining <= 0:
                metrics.inc("llm_engine_unavailable_total",
                            "Model calls abandoned after the recovery window", what=what, reason=reason)
                log.error("llm.resilient what=%s base_url=%s attempt=%d waited_s=%.0f GIVING UP: %s: %s",
                          what, base_url, attempt, elapsed, type(exc).__name__, str(exc)[:200])
                raise ModelUnavailable(base_url, elapsed, attempt, exc) from exc
            metrics.inc("llm_retry_total", "Model calls retried through a recoverable error",
                        what=what, reason=reason)
            log.warning("llm.resilient what=%s base_url=%s attempt=%d waited_s=%.0f retrying after %s: %s",
                        what, base_url, attempt, elapsed, type(exc).__name__, str(exc)[:200])
            if reason == "connection":
                # Do not hammer a port that is refusing: poll /health until the
                # engine is back (or the window closes), THEN back off briefly
                # so the first real request lands after the API's own warm-up.
                if not await wait_for_engine(base_url, deadline_s=remaining, what=what):
                    metrics.inc("llm_engine_unavailable_total",
                                "Model calls abandoned after the recovery window", what=what, reason=reason)
                    raise ModelUnavailable(base_url, time.monotonic() - started, attempt, exc) from exc
            pause = min(_backoff_s(attempt), max(0.0, window - (time.monotonic() - started)))
            if pause > 0:
                await asyncio.sleep(pause)
