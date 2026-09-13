"""Per-engine capacity gates for `/v1` — the capacity rule of 2026-09-13.

NOT A USAGE LIMIT. The owner removed every rate, token, quota and concurrency
limit from the public API (PUBLIC_API_ENFORCE_LIMITS=false). What remains is a
physical fact: the router, the OCR engine, the embeddings model, the reranker
and whisper are SHARED with the chat application, and a public caller must
never be able to starve the product of them. So public work on each shared
engine passes through ONE gate per engine — shared by every project and key,
first come first served, with a bounded wait that ends in the existing
`503 model_unavailable` "at capacity" with a `Retry-After`. Never a 429, and
never a number attached to a caller.

ONLY THE PUBLIC SIDE WAITS. The chat app's own calls (`llm.router_chat_
completion`, `llm.embed_query`, `rerank.score`, `engines/ocr.read_images`,
`asr.transcribe`) never pass through a gate in this module, so a chat turn
never queues behind public traffic. That asymmetry IS the priority rule.

THE NUMBERS (defaults; each is a PUBLIC_API_* setting, argued in the
architecture review of 2026-09-13 from the engines' own logs):

  main.long  1 at a time — techsara-35b when input (at its byte bound) +
             planned output is above the long-admission threshold (131,072).
             KV pool 1,663,201 tokens = 1.66 full windows; the chat app's own
             LONG lane admits one >131k prompt at a time.
  main.extended
             2 at a time — every other techsara-35b request that plans more
             output than the pre-2026-09-13 public ceiling (8,192). Adversarial
             review 2026-09-13: with no gate below the long threshold, ten
             public 130k-token answers held all ten of the chat app's NORMAL
             admission slots for ~22 minutes each and a chat turn was refused.
             At or under 8,192 a request holds a slot for at most ~115 s at
             71 tok/s — exactly what public work could do before this wave —
             so it still takes no public gate. Long-lived public holders are
             therefore at most 1 + 2 = 3 of the 10 NORMAL slots.
             Both main gates also step aside while a chat request is in the
             LONG admission lane (see `chat_long_admission_present`).
  router     4 concurrent, 24,576-token budget. KV 52,512 tokens (1.07 x the
             49,152 window) and the chat app classifies EVERY turn on it
             (~2.3k tokens): public use capped at 47% of the pool.
  ocr        2 concurrent, yields to chat. --max-num-seqs 8; chat read_images
             uses 4; the engine shares a GPU with a main-model TP rank (chat
             decode 75 → ~20 tok/s during OCR, 2026-09-09).
  embed      2 concurrent, 8,192-token budget (pool 18,720; chat query
             embeddings are on the TTFT path and fail soft when busy).
  rerank     2 concurrent, 8,192-token budget (same pool size).
  asr        1 fleet-wide, yields to chat AND to dictation: whisper decodes
             ONE clip per replica, and saturating either Spark takes chat
             decode 71 → ~24 tok/s because the main model is TP=2.

FIFO, AND SYNCHRONOUS RELEASE. Waiters are served strictly in arrival order
(a heavy request is not starved by a stream of light ones), and giving a slot
back takes no lock and no await — so a release in a `finally` running under
cancellation cannot be lost, which is the leak shape the streaming slot had
until 2026-09-13.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import sys
from dataclasses import dataclass
from typing import Any, AsyncIterator, Deque, Dict, Optional

from . import errors, registry

log = logging.getLogger(__name__)

GATE_MAIN_LONG = "main.long"
GATE_MAIN_EXTENDED = "main.extended"
#: The gates in front of the main engine: they hold an admission slot for a
#: long time, so they yield to a chat LONG request rather than to a busy chat.
MAIN_GATES = (GATE_MAIN_LONG, GATE_MAIN_EXTENDED)
GATE_ROUTER = registry.ENGINE_ROUTER
GATE_OCR = registry.ENGINE_OCR
GATE_EMBED = registry.ENGINE_EMBED
GATE_RERANK = registry.ENGINE_RERANK
GATE_ASR = registry.ENGINE_ASR
GATES = (
    GATE_MAIN_LONG, GATE_MAIN_EXTENDED, GATE_ROUTER, GATE_OCR, GATE_EMBED, GATE_RERANK, GATE_ASR,
)

#: How often a yield-to-chat wait asks again. One second is the video
#: pipeline's pace (owner-accepted policy, video/pipeline.pace); tests shrink it.
YIELD_STEP_S = 1.0


class Abandoned(Exception):
    """The waiter's owner gave up before a slot came free (a background job
    cancelled while still queued)."""


@dataclass(frozen=True)
class GateConfig:
    max_concurrent: int
    budget_tokens: int
    retry_after: float


def _config(engine: str) -> GateConfig:
    """Read at call time: an operator's change and a test's monkeypatch take
    effect on the next request."""
    s_int = registry.setting_int
    if engine == GATE_MAIN_LONG:
        return GateConfig(max(1, s_int("PUBLIC_API_MAIN_LONG_MAX_CONCURRENT", 1)), 0, 60.0)
    if engine == GATE_MAIN_EXTENDED:
        return GateConfig(max(1, s_int("PUBLIC_API_MAIN_EXTENDED_MAX_CONCURRENT", 2)), 0, 60.0)
    if engine == GATE_ROUTER:
        return GateConfig(
            max(1, s_int("PUBLIC_API_ROUTER_MAX_CONCURRENT", 4)),
            max(0, s_int("PUBLIC_API_ROUTER_KV_BUDGET_TOKENS", 24_576)),
            5.0,
        )
    if engine == GATE_OCR:
        return GateConfig(max(1, s_int("PUBLIC_API_OCR_MAX_CONCURRENT", 2)), 0, 5.0)
    if engine == GATE_EMBED:
        return GateConfig(
            max(1, s_int("PUBLIC_API_EMBED_MAX_CONCURRENT", 2)),
            max(0, s_int("PUBLIC_API_EMBED_KV_BUDGET_TOKENS", 8192)),
            5.0,
        )
    if engine == GATE_RERANK:
        return GateConfig(
            max(1, s_int("PUBLIC_API_RERANK_MAX_CONCURRENT", 2)),
            max(0, s_int("PUBLIC_API_RERANK_KV_BUDGET_TOKENS", 8192)),
            5.0,
        )
    if engine == GATE_ASR:
        return GateConfig(max(1, s_int("PUBLIC_API_ASR_MAX_CONCURRENT", 1)), 0, 5.0)
    raise ValueError(f"unknown capacity gate {engine!r} (known: {GATES})")


def sync_wait_s() -> float:
    """PUBLIC_API_GATE_WAIT_S (30 s): how long a synchronous, streaming,
    embeddings, rerank or transcription request may wait for its gate. It
    waits BEFORE the status line, so the refusal is a real HTTP 503 — and the
    silent pre-header wait stays well under Cloudflare's 100 s origin timeout."""
    return max(0.0, registry.setting_float("PUBLIC_API_GATE_WAIT_S", 30.0))


def background_wait_s() -> float:
    """PUBLIC_API_BACKGROUND_GATE_WAIT_S (3,600 s): a background job waits
    INSIDE the detached task, its row still `queued`, so an hour costs the
    caller nothing but latency."""
    return max(0.0, registry.setting_float("PUBLIC_API_BACKGROUND_GATE_WAIT_S", 3600.0))


def yield_to_chat_max_wait_s() -> float:
    return max(0.0, registry.setting_float("PUBLIC_API_YIELD_TO_CHAT_MAX_WAIT_S", 10.0))


# ----------------------------------------------------------------- state --


class _Waiter:
    __slots__ = ("future", "charge")

    def __init__(self, future: "asyncio.Future[None]", charge: int) -> None:
        self.future = future
        self.charge = charge


class _Gate:
    """One engine's gate on one event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.in_flight = 0
        self.used_tokens = 0
        self.waiters: Deque[_Waiter] = collections.deque()


#: engine → gate. REBUILT when the running loop changes, like asr._Pool: a
#: future bound to a dead loop (the suite makes one per test) never resolves.
_gates: Dict[str, _Gate] = {}


def _gate(engine: str) -> _Gate:
    loop = asyncio.get_running_loop()
    gate = _gates.get(engine)
    if gate is None or gate.loop is not loop:
        gate = _Gate(loop)
        _gates[engine] = gate
    return gate


def reset_for_tests() -> None:
    _gates.clear()
    _main_trackers.clear()


def _admissible(gate: _Gate, config: GateConfig, charge: int) -> bool:
    if gate.in_flight >= config.max_concurrent:
        return False
    # A request heavier than the whole budget is charged the budget, so it is
    # admitted — alone — rather than never.
    if config.budget_tokens > 0 and gate.in_flight > 0:
        return gate.used_tokens + charge <= config.budget_tokens
    return True


def _grant_waiters(engine: str, gate: _Gate) -> None:
    """Hand free capacity to the head of the line, in order. Synchronous."""
    config = _config(engine)
    while gate.waiters:
        head = gate.waiters[0]
        if head.future.done():
            gate.waiters.popleft()
            continue
        if not _admissible(gate, config, head.charge):
            break
        gate.waiters.popleft()
        gate.in_flight += 1
        gate.used_tokens += head.charge
        head.future.set_result(None)
    _publish(engine, gate)


def _release(engine: str, gate: _Gate, charge: int) -> None:
    gate.in_flight = max(0, gate.in_flight - 1)
    gate.used_tokens = max(0, gate.used_tokens - charge)
    _grant_waiters(engine, gate)


def _publish(engine: str, gate: _Gate) -> None:
    """Prometheus gauges. Never raises: telemetry must not fail a request.
    `public_api_engine_in_flight{engine="main.long"}` is also what a deploy
    guard should read before restarting the orchestrator under a multi-hour
    generation."""
    try:
        from .. import metrics

        metrics.set_gauge(
            "public_api_engine_in_flight",
            gate.in_flight,
            "public /v1 requests holding a shared engine's capacity gate",
            engine=engine,
        )
        metrics.set_gauge(
            "public_api_engine_waiting",
            sum(1 for waiter in gate.waiters if not waiter.future.done()),
            "public /v1 requests waiting for a shared engine's capacity gate",
            engine=engine,
        )
    except Exception:  # noqa: BLE001
        log.debug("capacity gauges not published", exc_info=True)


# ------------------------------------------------------ yield to chat --


def _module(dotted_suffix: str) -> Any:
    """A sibling module ONLY if the process already imported it. Importing the
    video pipeline or the dictation stack from here would drag both into a
    process (or a lint job) that never uses them; and if they were never
    imported, nothing of theirs is running to yield to."""
    package = __name__.rsplit(".", 2)[0]
    return sys.modules.get(f"{package}.{dotted_suffix}")


def chat_is_busy() -> bool:
    """Is a chat generation in flight? The same probe main.py installs for the
    video pipeline's pacing (owner-accepted policy, 2026-09-09)."""
    pipeline = _module("video.pipeline")
    probe = getattr(pipeline, "chat_is_busy", None) or getattr(pipeline, "_busy_probe", None)
    if probe is None:
        return False
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001 - the probe is advisory
        return False


def dictation_is_busy() -> bool:
    """Would starting a public clip NOW leave a dictation without a free
    whisper replica?

    Adversarial review 2026-09-13: the old test (`active >= replicas`) let a
    public clip start beside one running dictation on a two-replica fleet, so
    the NEXT dictation found both GPUs taken and waited behind the public
    clip inside the engine's lock (6.72 s for a 1 s clip, scaled; ~43 s for a
    300 s clip in production). A replica must remain free AFTER the public
    clip starts: `active + 1 < replicas`. One replica has no spare, so there
    public work starts only while no dictation runs at all — a dictation that
    arrives during the clip still waits for it; that residual is documented
    (CONTRACT §12.3), because whisper cannot be preempted mid-clip.
    """
    asr = _module("asr")
    pool = getattr(asr, "POOL", None)
    if pool is None:
        return False
    try:
        from ..config import settings

        replicas = max(1, len(tuple(getattr(settings, "asr_base_urls", ()) or ())))
        waiting = int(getattr(pool, "waiting", 0) or 0)
        active = int(getattr(pool, "active", 0) or 0)
        if waiting > 0:
            return True
        if replicas == 1:
            return active > 0
        return active + 1 >= replicas
    except Exception:  # noqa: BLE001
        return False


def chat_long_admission_present() -> bool:
    """Is a CHAT request in (or queued for) the main engine's LONG admission
    lane — a large-document turn waiting for the engine to go idle?

    WHY (adversarial review 2026-09-13). `admission._wait_for_idle` holds a
    LONG request until the engine reports no running requests, for up to
    ADMISSION_LONG_WAIT_S (600 s). A multi-hour public generation is a running
    request for its whole life, so every chat large-document turn behind one
    paid the full 600 s. The real fix is admission's idle test not counting
    long-lived public decodes (needs integration: `public_long_lived_decoding`
    below is the seam). What this module can do on its own is not START a new
    long-lived public generation while such a chat request is there.

    Public main-engine generations that may themselves sit in the LONG lane
    (their prompt's byte bound is over the threshold) are subtracted, so a
    public long job does not make every other public job wait for its whole
    life. Only the lanes of the running loop are read, and never created.
    """
    admission = _module("admission")
    by_loop = getattr(admission, "_by_loop", None)
    if by_loop is None:
        return False
    try:
        lanes = by_loop.get(asyncio.get_running_loop())
        if lanes is None:
            return False
        long_lane = lanes.long
        present = int(getattr(long_lane, "active", 0) or 0) + int(
            getattr(long_lane, "waiting", 0) or 0
        )
        return present > _tracker().possibly_long_prompt
    except Exception:  # noqa: BLE001 - advisory, like the other probes
        return False


# ------------------------------------------- public main-engine tracking --


class _MainTracker:
    """What public work is doing on the main engine, per event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        #: Public generations whose prompt's byte bound is above the LONG
        #: admission threshold — each may be holding the LONG lane.
        self.possibly_long_prompt = 0
        #: Public generations holding a main gate that are past their first
        #: token: decoding for up to hours, no longer prefilling.
        self.long_lived_decoding = 0


_main_trackers: Dict[int, _MainTracker] = {}


def _tracker() -> _MainTracker:
    loop = asyncio.get_running_loop()
    found = _main_trackers.get(id(loop))
    if found is None or found.loop is not loop:
        found = _MainTracker(loop)
        _main_trackers.clear()
        _main_trackers[id(loop)] = found
    return found


class PublicMainGeneration:
    """One public generation on the main engine, as the trackers see it.

    Synchronous enter/exit and first-token calls, no awaits: safe from a
    `finally` running under cancellation."""

    def __init__(self, *, possibly_long_prompt: bool, long_lived: bool) -> None:
        self._long_prompt = bool(possibly_long_prompt)
        self._long_lived = bool(long_lived)
        self._decoding = False
        self._entered = False

    def __enter__(self) -> "PublicMainGeneration":
        tracker = _tracker()
        self._tracker = tracker
        self._entered = True
        if self._long_prompt:
            tracker.possibly_long_prompt += 1
        return self

    def first_token(self) -> None:
        if self._entered and self._long_lived and not self._decoding:
            self._decoding = True
            self._tracker.long_lived_decoding += 1

    def __exit__(self, *_exc: Any) -> None:
        if not self._entered:
            return
        self._entered = False
        tracker = self._tracker
        if self._long_prompt:
            tracker.possibly_long_prompt = max(0, tracker.possibly_long_prompt - 1)
        if self._decoding:
            self._decoding = False
            tracker.long_lived_decoding = max(0, tracker.long_lived_decoding - 1)


def public_long_lived_decoding() -> int:
    """How many long-lived public generations (holders of a main gate) are
    decoding on the main engine right now, in this process.

    THE SEAM for `admission._ahead` (needs integration, admission.py is not
    this wave's file): subtracting this from `requests_running` — or from the
    NORMAL occupancy when there is no engine sample — lets a chat LONG request
    find the engine "idle" beside at most three decoding public jobs, instead
    of waiting ADMISSION_LONG_WAIT_S for a job that runs for hours. Decoding,
    not prefilling: a public request still in prefill is exactly the mixed
    prefill the idle wait exists to avoid, and is not subtracted.
    """
    try:
        return int(_tracker().long_lived_decoding)
    except RuntimeError:  # no running loop
        return 0


async def _yield_to_chat(
    engine: str,
    deadline: float,
    loop: asyncio.AbstractEventLoop,
    abandon: Optional[asyncio.Event] = None,
) -> None:
    """Wait while a person is waiting for an answer — bounded.

    Chat: up to PUBLIC_API_YIELD_TO_CHAT_MAX_WAIT_S (10 s), then the public
    work runs anyway, bounded by its gate; a chatty workspace must not starve
    an API caller for ever (the video pipeline's rule). Dictation (speech
    only): for the whole gate wait, because a person holding a microphone
    must always find a free replica — past the deadline it is the 503.
    """
    if engine in MAIN_GATES:
        # A chat LONG request is waiting for the engine to go idle; a new
        # multi-hour public generation would make it wait its whole bound.
        # For the whole gate wait: past it, the 503 (or, in a background job,
        # another turn of the queue).
        while chat_long_admission_present():
            if abandon is not None and abandon.is_set():
                raise Abandoned()
            if loop.time() >= deadline:
                raise errors.model_at_capacity(retry_after=_config(engine).retry_after)
            await asyncio.sleep(min(YIELD_STEP_S, max(0.0, deadline - loop.time())) or 0)
        return
    chat_deadline = min(deadline, loop.time() + yield_to_chat_max_wait_s())
    while loop.time() < chat_deadline and chat_is_busy():
        await asyncio.sleep(min(YIELD_STEP_S, max(0.0, chat_deadline - loop.time())) or 0)
    if engine != GATE_ASR:
        return
    while dictation_is_busy():
        if loop.time() >= deadline:
            raise errors.model_at_capacity(retry_after=_config(engine).retry_after)
        await asyncio.sleep(min(YIELD_STEP_S, max(0.0, deadline - loop.time())) or 0)


# ------------------------------------------------------------ the gate --


@contextlib.asynccontextmanager
async def hold(
    engine: str,
    *,
    weight_tokens: int = 0,
    wait_s: float,
    yield_to_chat: bool = False,
    abandon: Optional[asyncio.Event] = None,
) -> AsyncIterator[None]:
    """Hold one unit of `engine`'s public capacity for the body of the block.

    Raises `errors.ApiError` (503 `model_unavailable`, "at capacity", with
    Retry-After) when not admitted within `wait_s`, and `Abandoned` when
    `abandon` is set while waiting. Released in `finally`, however the block
    ends — including cancellation, both while waiting and while held.
    """
    config = _config(engine)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(wait_s))
    if yield_to_chat or engine in MAIN_GATES:
        await _yield_to_chat(engine, deadline, loop, abandon)
    gate = _gate(engine)
    weight = max(0, int(weight_tokens or 0))
    charge = min(weight, config.budget_tokens) if config.budget_tokens > 0 else 0

    if not gate.waiters and _admissible(gate, config, charge):
        gate.in_flight += 1
        gate.used_tokens += charge
        _publish(engine, gate)
    else:
        waiter = _Waiter(loop.create_future(), charge)
        gate.waiters.append(waiter)
        _publish(engine, gate)
        abandon_task: Optional[asyncio.Task] = None
        try:
            waits = {waiter.future}
            if abandon is not None:
                abandon_task = loop.create_task(abandon.wait())
                waits.add(abandon_task)
            while not waiter.future.done():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                await asyncio.wait(waits, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                if abandon is not None and abandon.is_set() and not waiter.future.done():
                    break
        except BaseException:
            # Cancelled while waiting. A grant that landed in the same tick is
            # given straight back, so it can never be stranded.
            _forget(engine, gate, waiter)
            raise
        finally:
            if abandon_task is not None:
                abandon_task.cancel()
        if not waiter.future.done() or waiter.future.cancelled():
            _forget(engine, gate, waiter)
            if abandon is not None and abandon.is_set():
                raise Abandoned()
            raise errors.model_at_capacity(retry_after=config.retry_after)
    try:
        yield
    finally:
        _release(engine, gate, charge)


def _forget(engine: str, gate: _Gate, waiter: _Waiter) -> None:
    if waiter.future.done() and not waiter.future.cancelled():
        # Granted after all: the grant already counted us in; give it back.
        _release(engine, gate, waiter.charge)
        return
    waiter.future.cancel()
    with contextlib.suppress(ValueError):
        gate.waiters.remove(waiter)
    # Leaving from the head of the line may let the next waiter in.
    _grant_waiters(engine, gate)


def snapshot() -> Dict[str, Dict[str, int]]:
    """{engine: {in_flight, waiting, budget_tokens, used_tokens,
    max_concurrent}} for a health payload and the tests. Never names a URL."""
    out: Dict[str, Dict[str, int]] = {}
    for engine in GATES:
        config = _config(engine)
        gate = _gates.get(engine)
        out[engine] = {
            "in_flight": gate.in_flight if gate else 0,
            "waiting": (sum(1 for w in gate.waiters if not w.future.done()) if gate else 0),
            "budget_tokens": config.budget_tokens,
            "used_tokens": gate.used_tokens if gate else 0,
            "max_concurrent": config.max_concurrent,
        }
    return out


__all__ = [
    "Abandoned",
    "GATES",
    "GATE_ASR",
    "GATE_EMBED",
    "GATE_MAIN_EXTENDED",
    "GATE_MAIN_LONG",
    "MAIN_GATES",
    "PublicMainGeneration",
    "GATE_OCR",
    "GATE_RERANK",
    "GATE_ROUTER",
    "background_wait_s",
    "chat_is_busy",
    "chat_long_admission_present",
    "dictation_is_busy",
    "public_long_lived_decoding",
    "hold",
    "snapshot",
    "sync_wait_s",
]
