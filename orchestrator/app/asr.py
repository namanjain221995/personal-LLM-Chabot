"""Speech to text — the model-agnostic half of voice input.

WHAT IS HERE, AND WHAT IS NOT. The transcript dataclass, the provider protocol,
the error vocabulary the route already knows how to turn into sentences, the
fleet router and the admission control that keeps a workspace's microphones off
the main model's GPU. All of it is independent of which engine transcribes.

THERE IS CURRENTLY NO ENGINE. Qwen3-ASR-1.7B and TheWhisper were both
evaluated and rejected, and their implementations, services and weights have
been removed. `provider()` therefore raises: `ASR_ENABLED` defaults to false,
the route answers 404 before ever reaching here, and the composer hides the
microphone. That state is deliberate and temporary — the next engine plugs in
by implementing `ASRProvider` and being returned from `provider()`.

WHY THIS FILE SURVIVED THE REMOVAL. Everything above is the part that was never
about a particular model: the pool sizing, the least-active routing, the
cooldown on a failing endpoint, the metric names, and the promise that audio is
held in memory for one call and dropped. Deleting it would mean rediscovering
all of that for the next engine.

WHERE THE AUDIO GOES. Nowhere but the engine. The bytes arrive in a request,
are held in memory for the length of one call, and are dropped. Nothing is
written to disk, nothing reaches the database, and the transcript is returned
to the browser as a DRAFT — it becomes a message only if the person presses
Send. See app/audio_api.py for the route that enforces that.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Sequence

from . import metrics
from .config import settings

log = logging.getLogger(__name__)

class ASRUnavailable(Exception):
    """The engine could not be reached, or refused. Retryable."""


class ASRBusy(Exception):
    """Every transcription slot is taken and the queue wait ran out."""


class ASRRejected(Exception):
    """The audio itself is the problem — too long, unreadable, empty."""


@dataclass(frozen=True)
class Transcript:
    """One finished transcription. `language` is None when nobody identified it."""

    text: str
    language: Optional[str]
    language_code: Optional[str]
    #: Where the transcript came from, for the log and the metric — never
    #: shown to a member, who has no reason to learn the model's name.
    provider: str
    model: str
    engine_ms: int
    #: True when the primary path failed and the fallback answered.
    degraded: bool = False


class ASRProvider(Protocol):
    """What the route needs from a speech engine, and nothing more."""

    name: str
    model: str

    async def transcribe(
        self, audio: bytes, *, filename: str, content_type: str, language: str = ""
    ) -> Transcript: ...

    async def health(self) -> bool: ...


class RoutedProvider:
    """Several engines, one interface: send each clip to the freest one.

    WHY REPLICAS AND NOT ONE SHARDED MODEL. "Use both GPUs" has two possible
    meanings and only one of them is faster here. Splitting a single 1.7B
    model across two Sparks puts every layer's activations on the RoCE fabric
    — 13 Gb/s a link, already carrying the main model's own tensor-parallel
    traffic — to save memory that was never short: the weights are 4.4 GB and
    each node has room for them twice over. Two whole copies with requests
    balanced between them adds throughput without adding a single byte of
    cross-node chatter, and it degrades to one engine gracefully when a node
    goes away. That is what this class does.

    LEAST ACTIVE, not round robin. Clips are not the same size — a
    four-second question and a two-minute dictation are one request each —
    so counting requests sent would send the long one and the next one to the
    same engine. Counting requests still RUNNING sends work where there is
    room for it, which is the property that actually matters.

    A FAILING ENGINE IS SKIPPED, BRIEFLY. An endpoint that raises
    ASRUnavailable is stood down for `_COOLDOWN_S` and the request is retried
    on another. It is never removed permanently: a node that reboots must
    rejoin by itself, without anybody editing configuration.
    """

    #: Long enough that a restarting engine is not hammered, short enough that
    #: a recovered one is back before anyone notices it left.
    _COOLDOWN_S = 20.0

    def __init__(self, engines: Sequence[ASRProvider]) -> None:
        if not engines:
            raise ValueError("RoutedProvider needs at least one engine")
        self._engines = list(engines)
        self._active: Dict[int, int] = {i: 0 for i in range(len(self._engines))}
        self._down_until: Dict[int, float] = {i: 0.0 for i in range(len(self._engines))}
        self.name = self._engines[0].name
        self.model = self._engines[0].model

    def _order(self) -> List[int]:
        """Healthy engines first, freest first; then the ones standing down.

        The stood-down engines stay on the end rather than being dropped, so a
        fleet where every engine is cooling off still tries one instead of
        failing a request nobody had to lose.
        """
        now = time.monotonic()
        healthy = [i for i in range(len(self._engines)) if self._down_until[i] <= now]
        cooling = [i for i in range(len(self._engines)) if self._down_until[i] > now]
        healthy.sort(key=lambda i: self._active[i])
        cooling.sort(key=lambda i: self._down_until[i])
        return healthy + cooling

    async def transcribe(
        self, audio: bytes, *, filename: str, content_type: str, language: str = ""
    ) -> Transcript:
        last: Optional[Exception] = None
        for index in self._order():
            engine = self._engines[index]
            self._active[index] += 1
            try:
                result = await engine.transcribe(
                    audio,
                    filename=filename,
                    content_type=content_type,
                    language=language,
                )
                self._down_until[index] = 0.0
                return result
            except ASRRejected:
                # The engine understood the request and refused the AUDIO.
                # Another engine would refuse it identically, more slowly.
                raise
            except ASRUnavailable as exc:
                last = exc
                self._down_until[index] = time.monotonic() + self._COOLDOWN_S
                log.warning(
                    "ASR engine %s is unavailable (%s); standing it down for %.0fs",
                    getattr(engine, "base_url", index), exc, self._COOLDOWN_S,
                )
            finally:
                self._active[index] -= 1
        raise ASRUnavailable(str(last) if last else "no speech engine answered")

    async def health(self) -> bool:
        """True when ANY engine answers — the feature works on one node."""
        for engine in self._engines:
            if await engine.health():
                return True
        return False

    def stats(self) -> List[Dict[str, Any]]:
        """Per-engine state, for /audio/health and the admin console."""
        now = time.monotonic()
        return [
            {
                "endpoint": getattr(engine, "base_url", ""),
                "active": self._active[i],
                "available": self._down_until[i] <= now,
            }
            for i, engine in enumerate(self._engines)
        ]


# ---------------------------------------------------------------------------
# Admission control
#
# The engine batches happily — eight simultaneous 15-second clips finished in
# 1.10s of wall clock, measured 2026-09-04 — so the limit here is not about
# protecting the ASR engine. It is about the main model: an unbounded fan-out
# of audio requests would eventually contend for the same GPU the chat model
# runs on, and a person waiting for an ANSWER must never be slowed down by
# someone else's dictation. A bounded pool with a short queue is the whole
# mechanism: past it, callers are told to try again rather than queued
# indefinitely behind work they cannot see.
# ---------------------------------------------------------------------------


class _Pool:
    """A semaphore that reports its own depth, and refuses rather than hangs."""

    def __init__(self) -> None:
        self._sem: Optional[asyncio.Semaphore] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.waiting = 0
        self.active = 0

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        # Rebuilt per event loop: a Semaphore bound to a dead loop (the test
        # suite makes a new one per test) blocks forever on the next acquire.
        if self._sem is None or self._loop is not loop:
            # Per engine, times the fleet: two nodes carry twice the work at
            # the same pressure each. `settings.asr_base_urls` is never empty
            # (config falls back to the single URL), so this is at least one.
            self._sem = asyncio.Semaphore(
                max(1, settings.asr_max_concurrent) * max(1, len(settings.asr_base_urls))
            )
            self._loop = loop
        return self._sem

    async def __aenter__(self) -> "_Pool":
        sem = self._semaphore()
        self.waiting += 1
        metrics.set_gauge("asr_queue_depth", self.waiting, "requests waiting for a slot")
        try:
            await asyncio.wait_for(sem.acquire(), timeout=settings.asr_queue_wait_s)
        except asyncio.TimeoutError as exc:
            raise ASRBusy("every transcription slot is busy") from exc
        finally:
            self.waiting -= 1
            metrics.set_gauge("asr_queue_depth", self.waiting, "requests waiting for a slot")
        self.active += 1
        metrics.set_gauge("asr_active_requests", self.active, "transcriptions in flight")
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        self.active -= 1
        metrics.set_gauge("asr_active_requests", self.active, "transcriptions in flight")
        if self._sem is not None:
            self._sem.release()

    def reset_for_tests(self) -> None:
        self._sem = None
        self._loop = None
        self.waiting = 0
        self.active = 0


POOL = _Pool()

_provider: Optional[ASRProvider] = None


def provider() -> ASRProvider:
    """The configured engine — currently none.

    Both evaluated engines were rejected and removed, so there is nothing to
    return. This raises rather than returning a stub that would answer every
    recording with silence: a deployment with no speech engine must fail
    loudly here, and `audio_api` already refuses with 404 before it gets this
    far because `ASR_ENABLED` defaults to false.

    A stale deployment that still has ASR_ENABLED=true in its environment
    reaches this and gets a 503 with a sentence, which is the honest answer.

    THE NEXT ENGINE goes here: build it, wrap the fleet in `RoutedProvider`,
    and cache it in `_provider` exactly as before.
    """
    if _provider is not None:
        return _provider
    raise ASRUnavailable(
        "no speech engine is configured on this deployment "
        "(voice input is disabled until one is installed)"
    )


def set_provider(value: Optional[ASRProvider]) -> None:
    """Swap the engine. For tests, and for a future second provider."""
    global _provider
    _provider = value


async def transcribe(
    audio: bytes, *, filename: str, content_type: str, language: str = ""
) -> Transcript:
    """Transcribe one clip under the pool, with the metrics that go with it."""
    started = time.perf_counter()
    async with POOL:
        try:
            result = await provider().transcribe(
                audio, filename=filename, content_type=content_type, language=language
            )
        except ASRRejected:
            metrics.inc("asr_requests_total", "transcription attempts", result="fail")
            raise
        except Exception:
            metrics.inc("asr_requests_total", "transcription attempts", result="fail")
            raise
    metrics.inc("asr_requests_total", "transcription attempts", result="ok")
    metrics.observe(
        "asr_request_duration_seconds",
        time.perf_counter() - started,
        "wall clock for one transcription, orchestrator side",
    )
    metrics.inc(
        "asr_detected_language_total",
        "identified languages",
        language=result.language or "unknown",
    )
    return result
