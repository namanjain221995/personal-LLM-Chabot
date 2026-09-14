"""Per-engine capacity gates for `/v1` — the capacity rule of 2026-09-13.

NOT A USAGE LIMIT. The owner removed every rate, token, quota and concurrency
limit from the public API (PUBLIC_API_ENFORCE_LIMITS=false). What remains is a
physical fact: the router, the OCR engine, the embeddings model, the reranker
and whisper are SHARED with the chat application, and a public caller must
never be able to starve the product of them. So public work on each shared
engine passes through ONE gate per engine — shared by every project and key,
first come first served. On /v1 that wait has no limit (NO CLOCK ENDS A /v1
WAIT, below); only a caller passing a finite wait can get the
`503 model_unavailable` "at capacity" with a `Retry-After`. Never a 429, and
never a number attached to a caller.

ONLY THE PUBLIC SIDE WAITS. The chat app's own calls (`llm.router_chat_
completion`, `llm.embed_query`, `rerank.score`, `engines/ocr.read_images`,
`asr.transcribe`) never pass through a gate in this module, so a chat turn
never queues behind public traffic. That asymmetry IS the priority rule.

THE NUMBERS (defaults; each is a PUBLIC_API_* setting, argued in the
architecture review of 2026-09-13 from the engines' own logs):

  main.long  1 at a time — a techsara-35b answer PLANNED above
             PUBLIC_API_MAIN_SOLO_OUTPUT_TOKENS (800,000). The engine's KV pool
             is 1,663,201 tokens: two such answers are the whole pool, so they
             never run together, whatever ADMISSION_KV_RESERVE_FRACTION says.
  main.extended
             a techsara-35b answer planned above the pre-2026-09-13 public
             ceiling (8,192, the v1 long-output threshold of app/admission.py).
             NOT A COUNTER OF ITS OWN (integration 2026-09-13, "one accounting"):
             the gate admits the answer into admission's LONG_OUTPUT lane —
             its seats (2) and its KV budget — BEFORE the status line, and
             hands that ticket to the generation. main.long does the same
             after its own one-at-a-time wait. The planned output travels
             with the ticket, so a retry inside the generation is admitted
             into LONG_OUTPUT again, and an answer admission does NOT take
             into LONG_OUTPUT (a LONG prompt, or
             ADMISSION_V1_LONG_OUTPUT_THRESHOLD_TOKENS set above
             PUBLIC_API_MAIN_EXTENDED_OUTPUT_TOKENS) is capped by this gate's
             own FIFO count, PUBLIC_API_MAIN_EXTENDED_MAX_CONCURRENT (2).
             Neither gate is sized by the PROMPT. A long prompt is admission's
             LONG lane, decided on the exact /tokenize count at the call
             (integration 2026-09-13: sizing a gate by the UTF-8 byte bound sent
             every ~123 KB document — 35-47k real tokens — through the one-at-
             a-time gate, and one 1M-output job refused them all for hours).
             Both main gates step aside while a chat LONG request is still
             before its first token (see `chat_long_admission_present`).
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

NO CLOCK ENDS A /v1 WAIT (no-timeout design, 2026-09-13). `hold(wait_s=None)`
waits until admitted, abandoned, or the caller's own physical guards say
otherwise; only a caller passing a FINITE `wait_s` can ever get
`model_at_capacity`, and no /v1 route does: the generations wait inside
their streams and committed responses, and since 2026-09-14 so do
`/v1/embeddings`, `/v1/rerank` and `/v1/audio/transcriptions` (sidecars.py,
audio_jobs.py). The one finite caller left is the bounded synchronous file
preparation (`sync_wait_s`). A
waiter can ask for `on_wait(position, waited_s)` — on entry, whenever its
place in line changes, and at least every 15 s — which is how a stream says
`response.queued` and a committed sync body writes its whitespace.

  main.normal
             6 at a time — every public techsara-35b generation that is not
             `main.long` (design capacity_waits, critical finding): at most 6
             public generations inside chat's 10-slot NORMAL lane, so a public
             flood can never reach chat's max_waiting refusal (measured with
             the real Lane: 400 patient waiters refused chat instantly). Held
             from dispatch to the end of the attempt, released while suspended.
             `main.extended` holders also hold `main.normal` (taken in that
             order, always, so the two gates cannot deadlock).

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
from typing import Any, AsyncIterator, Callable, Deque, Dict, List, Optional

from . import errors, registry

log = logging.getLogger(__name__)

GATE_MAIN_LONG = "main.long"
GATE_MAIN_EXTENDED = "main.extended"
GATE_MAIN_NORMAL = "main.normal"
#: The gates in front of the main engine: they hold an admission slot for a
#: long time, so they yield to a chat LONG request rather than to a busy chat.
MAIN_GATES = (GATE_MAIN_LONG, GATE_MAIN_EXTENDED)
GATE_ROUTER = registry.ENGINE_ROUTER
GATE_OCR = registry.ENGINE_OCR
GATE_EMBED = registry.ENGINE_EMBED
GATE_RERANK = registry.ENGINE_RERANK
GATE_ASR = registry.ENGINE_ASR
GATES = (
    GATE_MAIN_LONG, GATE_MAIN_EXTENDED, GATE_MAIN_NORMAL, GATE_ROUTER, GATE_OCR, GATE_EMBED,
    GATE_RERANK, GATE_ASR,
)

#: The longest a waiter goes without an `on_wait` call.
ON_WAIT_EVERY_S = 15.0

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
        # A count of its own ONLY when admission cannot take the answer before
        # the status line (`_admission_front_door` is None, or the caller named
        # no work): the pre-integration fallback. Otherwise admission's
        # LONG_OUTPUT seats are the cap (`snapshot` reports them).
        return GateConfig(max(1, s_int("PUBLIC_API_MAIN_EXTENDED_MAX_CONCURRENT", 2)), 0, 60.0)
    if engine == GATE_MAIN_NORMAL:
        return GateConfig(max(1, s_int("PUBLIC_API_MAIN_NORMAL_MAX_CONCURRENT", 6)), 0, 60.0)
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
    """PUBLIC_API_GATE_WAIT_S (30 s, retired): the gate budget of the one
    caller that still waits BEFORE its status line with a clock — the bounded
    synchronous file preparation (`file_inputs.bounded_engines`, and the
    retrieval reranker through `sidecars.BOUNDED_DEFAULT`). No /v1 route reads
    it any more; `config.STILL_READ_RETIRED_SETTINGS` names the readers."""
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
    __slots__ = ("future", "charge", "since")

    def __init__(self, future: "asyncio.Future[None]", charge: int, since: float = 0.0) -> None:
        self.future = future
        self.charge = charge
        self.since = since


class _Gate:
    """One engine's gate on one event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.in_flight = 0
        self.used_tokens = 0
        self.waiters: Deque[_Waiter] = collections.deque()
        #: Main gates only: holders waiting inside admission for their
        #: LONG_OUTPUT seat and KV (the wait is admission's; the gauge is ours).
        self.admitting = 0


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


def _waiting(gate: _Gate) -> int:
    return sum(1 for waiter in gate.waiters if not waiter.future.done()) + int(gate.admitting)


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
        waiting = [waiter for waiter in gate.waiters if not waiter.future.done()]
        metrics.set_gauge(
            "public_api_engine_waiting",
            _waiting(gate),
            "public /v1 requests waiting for a shared engine's capacity gate",
            engine=engine,
        )
        oldest = min((w.since for w in waiting), default=None)
        metrics.set_gauge(
            "public_api_engine_oldest_wait_seconds",
            0.0 if oldest is None else max(0.0, gate.loop.time() - oldest),
            "how long the oldest public /v1 waiter for a shared engine's gate has waited",
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


def _lanes_of_this_loop() -> Any:
    """The admission lanes of the running loop, or None — read, never created."""
    admission = _module("admission")
    by_loop = getattr(admission, "_by_loop", None)
    if by_loop is None:
        return None
    try:
        return by_loop.get(asyncio.get_running_loop())
    except RuntimeError:  # no running loop
        return None


def chat_long_admission_present() -> bool:
    """Is a large prefill still AHEAD of the main engine — a chat LONG request
    waiting for its seat or its KV, any LONG request waiting for the engine to
    go idle, or a LONG request holding the lanes closed until its first token?

    WHY (adversarial review 2026-09-13). `admission._wait_for_idle` holds a
    LONG request until the engine reports nothing running; a multi-hour public
    answer started meanwhile is a running request for its whole life. So no
    long public answer is STARTED while such a request is ahead.

    ONLY BEFORE ITS FIRST TOKEN (integration 2026-09-13, rereview P1). The
    first version counted `long.active + long.waiting`, and a LONG ticket stays
    active until its stream ends: a chat large-document turn already decoding
    — which needs nothing from public work, admission has reopened the lanes —
    refused every long public request for its whole generation. And it
    subtracted public jobs guessed from the prompt's byte bound, which hid a
    real chat LONG request behind a public prose job admission had put in
    NORMAL. Now:

    * waiting for the LONG seat or its KV: counted by ORIGIN, chat only (a
      public long prompt waiting there does not make public work wait);
    * waiting for idle (`Lanes.long_idle_waiting`) or holding the closure
      (`normal.closed`): any origin — a large prefill is about to run or is
      running, and admission itself holds LONG_OUTPUT grants in both states.

    With lanes that predate `long_idle_waiting`, an active LONG ticket that is
    not holding the closure is counted (the old over-count, never an
    under-count). Advisory: any failure reads as "no".
    """
    lanes = _lanes_of_this_loop()
    if lanes is None:
        return False
    try:
        long_lane = lanes.long
        chat = "chat"
        by_origin = getattr(long_lane, "waiting_by_origin", None)
        seat_waiting = int(by_origin(chat)) if callable(by_origin) else int(long_lane.waiting or 0)
        kv = getattr(lanes, "kv", None)
        kv_waiting = 0
        if kv is not None and hasattr(kv, "queue"):
            kv_waiting = int(kv.queue.depth(chat, "long"))
        closed = bool(getattr(lanes.normal, "closed", False))
        idle_waiting = getattr(lanes, "long_idle_waiting", None)
        if idle_waiting is None:
            idle_waiting = 0 if closed else int(getattr(long_lane, "active", 0) or 0)
        return seat_waiting + kv_waiting + int(idle_waiting) > 0 or closed
    except Exception:  # noqa: BLE001 - advisory, like the other probes
        return False


# ------------------------------------------- public main-engine origin --


def _set_public_origin() -> Any:
    """Mark this context's main-engine calls as /v1 work for the admission
    lanes (chat-first order, the NORMAL seat reserved for chat, the /v1 KV
    charges). Returns the token to reset, or None when admission is absent."""
    try:
        from .. import admission
    except Exception:  # noqa: BLE001 - a process without the lanes has nothing to mark
        return None
    set_origin = getattr(admission, "set_origin", None)
    origin = getattr(admission, "ORIGIN_V1", None)
    if set_origin is None or origin is None:
        return None
    return (admission, set_origin(origin))


def _reset_public_origin(token: Any) -> None:
    if token is None:
        return
    admission, value = token
    with contextlib.suppress(Exception):  # exited from another context
        admission._origin.reset(value)


class PublicMainGeneration:
    """One public generation on the main engine, entered INSIDE its producer
    task (streaming.Generation._run): marks the task's admission calls as /v1
    work, and unmarks them on exit.

    It used to keep a second count of public decoders for admission's idle
    test to subtract (`public_long_lived_decoding`). Admission's LONG_OUTPUT
    lane counts them now — the answer is admitted there (see `hold`) — so that
    count, and the double subtraction it invited, are gone. The keyword
    arguments are accepted and ignored so the producer's call is unchanged.
    Synchronous, no awaits: safe from a `finally` under cancellation."""

    def __init__(self, **_ignored: Any) -> None:
        self._token: Any = None

    def __enter__(self) -> "PublicMainGeneration":
        self._token = _set_public_origin()
        return self

    def first_token(self) -> None:
        """Kept for the producer's call; admission marks decoding itself."""

    def __exit__(self, *_exc: Any) -> None:
        token, self._token = self._token, None
        _reset_public_origin(token)


async def _yield_to_chat(
    engine: str,
    deadline: Optional[float],
    loop: asyncio.AbstractEventLoop,
    abandon: Optional[asyncio.Event] = None,
    on_wait: Optional["OnWait"] = None,
) -> None:
    """Wait while a person is waiting for an answer.

    Chat: up to PUBLIC_API_YIELD_TO_CHAT_MAX_WAIT_S (10 s), then the public
    work runs anyway, bounded by its gate; a chatty workspace must not starve
    an API caller for ever (the video pipeline's rule). Dictation (speech
    only) and a chat LONG request (main gates): for the whole gate wait —
    with `deadline=None` (every durable /v1 caller) that wait has no limit and
    ends only when the person is served or the waiter is abandoned; with a
    finite deadline (the legacy synchronous path) it is the 503.
    """

    def expired() -> bool:
        return deadline is not None and loop.time() >= deadline

    def step() -> float:
        if deadline is None:
            return YIELD_STEP_S
        return min(YIELD_STEP_S, max(0.0, deadline - loop.time())) or 0

    started = loop.time()
    notice = {"at": None}

    async def waiting() -> None:
        """`on_wait(0, waited)` while a person is being served first:
        position 0 means "not in the gate's line yet". On the first wait and
        at least every ON_WAIT_EVERY_S, like the line itself."""
        now = loop.time()
        if on_wait is not None and (notice["at"] is None or now - notice["at"] >= ON_WAIT_EVERY_S):
            notice["at"] = now
            await _notify_wait(on_wait, 0, now - started)

    if engine in MAIN_GATES:
        while chat_long_admission_present():
            if abandon is not None and abandon.is_set():
                raise Abandoned()
            if expired():
                raise errors.model_at_capacity(retry_after=_config(engine).retry_after)
            await waiting()
            await asyncio.sleep(step())
        return
    chat_deadline = loop.time() + yield_to_chat_max_wait_s()
    if deadline is not None:
        chat_deadline = min(deadline, chat_deadline)
    while loop.time() < chat_deadline and chat_is_busy():
        await asyncio.sleep(min(YIELD_STEP_S, max(0.0, chat_deadline - loop.time())) or 0)
    if engine != GATE_ASR:
        return
    while dictation_is_busy():
        if abandon is not None and abandon.is_set():
            raise Abandoned()
        if expired():
            raise errors.model_at_capacity(retry_after=_config(engine).retry_after)
        await waiting()
        await asyncio.sleep(step())


# ------------------------------------------------------------ the gate --


OnWait = Callable[[int, float], Any]


def _position(gate: _Gate, waiter: _Waiter) -> int:
    """1-based place in line among live waiters."""
    place = 0
    for other in gate.waiters:
        if other.future.done():
            continue
        place += 1
        if other is waiter:
            return place
    return 0


async def _notify_wait(on_wait: Optional[OnWait], position: int, waited: float) -> None:
    if on_wait is None:
        return
    try:
        result = on_wait(position, waited)
        if asyncio.iscoroutine(result):
            await result
    except Exception:  # noqa: BLE001 - a progress callback never fails a wait
        log.debug("on_wait callback raised", exc_info=True)


@contextlib.asynccontextmanager
async def hold(
    engine: str,
    *,
    weight_tokens: int = 0,
    wait_s: Optional[float] = None,
    yield_to_chat: bool = False,
    abandon: Optional[asyncio.Event] = None,
    on_wait: Optional[OnWait] = None,
    front: bool = False,
    work: Any = None,
) -> AsyncIterator[None]:
    """Hold one unit of `engine`'s public capacity for the body of the block.

    `wait_s=None` (the default since 2026-09-13): no limit — the wait ends on
    admission or abandonment only. A finite `wait_s` raises `errors.ApiError`
    (503 `model_unavailable`, "at capacity", with Retry-After) when not
    admitted in time. `Abandoned` when `abandon` is set while waiting.
    `front=True` puts the waiter at the head of the line (a run that yielded
    to chat goes back where it was). Released in `finally`, however the block
    ends — including cancellation, both while waiting and while held.

    `work`: for a main gate, the planned request (`GenerationPlan` or
    `GenerationSpec`: its `messages` and the `max_tokens` the engine is sent).
    With it, the answer is admitted into admission's LONG_OUTPUT lane before
    the block runs and the generation inside the block uses that ticket (module
    docstring, `main.extended`). Without it — or without an admission that has
    a front door — a main gate counts on its own, as before 2026-09-13.
    """
    if engine in MAIN_GATES:
        async with _hold_main(engine, wait_s=wait_s, abandon=abandon, work=work, on_wait=on_wait, front=front):
            yield
        return
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = None if wait_s is None else started + max(0.0, float(wait_s))
    if yield_to_chat:
        await _yield_to_chat(engine, deadline, loop, abandon, on_wait)
    async with _counted(
        engine, deadline=deadline, loop=loop, weight_tokens=weight_tokens, abandon=abandon,
        on_wait=on_wait, front=front, started=started,
    ):
        yield


@contextlib.asynccontextmanager
async def _counted(
    engine: str,
    *,
    deadline: Optional[float],
    loop: asyncio.AbstractEventLoop,
    weight_tokens: int = 0,
    abandon: Optional[asyncio.Event] = None,
    on_wait: Optional[OnWait] = None,
    front: bool = False,
    started: Optional[float] = None,
) -> AsyncIterator[None]:
    """This module's own FIFO count for `engine`, waiting until `deadline`
    (None: no limit — NO CLOCK ENDS A /v1 WAIT)."""
    config = _config(engine)
    if started is None:
        started = loop.time()
    gate = _gate(engine)
    weight = max(0, int(weight_tokens or 0))
    charge = min(weight, config.budget_tokens) if config.budget_tokens > 0 else 0

    if (not gate.waiters or front) and _admissible(gate, config, charge):
        gate.in_flight += 1
        gate.used_tokens += charge
        _publish(engine, gate)
    else:
        waiter = _Waiter(loop.create_future(), charge, started)
        if front:
            gate.waiters.appendleft(waiter)
        else:
            gate.waiters.append(waiter)
        _publish(engine, gate)
        abandon_task: Optional[asyncio.Task] = None
        position = _position(gate, waiter)
        await _notify_wait(on_wait, position, 0.0)
        last_notice = loop.time()
        try:
            waits = {waiter.future}
            if abandon is not None:
                abandon_task = loop.create_task(abandon.wait())
                waits.add(abandon_task)
            while not waiter.future.done():
                now = loop.time()
                timeout: Optional[float] = None
                if deadline is not None:
                    remaining = deadline - now
                    if remaining <= 0:
                        break
                    timeout = remaining
                if on_wait is not None:
                    until_notice = max(0.01, ON_WAIT_EVERY_S - (now - last_notice))
                    timeout = until_notice if timeout is None else min(timeout, until_notice)
                await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if abandon is not None and abandon.is_set() and not waiter.future.done():
                    break
                if on_wait is not None and not waiter.future.done():
                    now = loop.time()
                    moved = _position(gate, waiter)
                    if moved != position or now - last_notice >= ON_WAIT_EVERY_S:
                        position = moved
                        last_notice = now
                        await _notify_wait(on_wait, position, now - started)
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


@contextlib.contextmanager
def _tallied(engine: str) -> Any:
    """In flight for the gauge and `snapshot`, with no cap: admission's
    LONG_OUTPUT lane is the cap. Synchronous release."""
    gate = _gate(engine)
    gate.in_flight += 1
    _publish(engine, gate)
    try:
        yield
    finally:
        gate.in_flight = max(0, gate.in_flight - 1)
        _publish(engine, gate)


def _admission_front_door() -> Any:
    """app.admission when it can admit a long answer before its call
    (`preadmit` + `use_preadmitted`), else None — feature-detected, so this
    file is correct before and after admission.py's integration patch."""
    try:
        from .. import admission
    except Exception:  # noqa: BLE001
        return None
    if callable(getattr(admission, "preadmit", None)) and callable(getattr(admission, "use_preadmitted", None)):
        return admission
    return None


def _use_preadmitted(front_door: Any, ticket: Any, max_tokens: int) -> Any:
    """front_door.use_preadmitted with the planned output; an admission.py
    that predates the keyword gets the ticket alone (feature-detected, like
    `_admission_front_door`)."""
    try:
        return front_door.use_preadmitted(ticket, max_tokens=max_tokens)
    except TypeError:
        return front_door.use_preadmitted(ticket)


def _work_request(work: Any) -> Optional[tuple]:
    """(messages, max_tokens sent to the engine) of a plan or a spec."""
    if work is None:
        return None
    messages = getattr(work, "messages", None)
    max_tokens = getattr(work, "max_tokens_for_engine", None)
    if max_tokens is None:
        max_tokens = getattr(work, "max_tokens", None)
    if messages is None or max_tokens is None:
        return None
    return list(messages), int(max_tokens)


async def _preadmit(
    admission: Any,
    request: tuple,
    *,
    deadline: Optional[float],
    loop: asyncio.AbstractEventLoop,
    abandon: Optional[asyncio.Event],
    retry_after: float,
    on_wait: Optional["OnWait"] = None,
    started: Optional[float] = None,
) -> Any:
    """admission.preadmit, bounded by what is left of this gate's wait as a
    hard wall clock (whatever the lane defers behind a closure) and abandoned
    with the job. AdmissionRejected is the same retry-safe 503 the gate gives.

    `deadline=None` (every durable /v1 caller, NO CLOCK ENDS A /v1 WAIT): the
    pre-admission is PATIENT and has no wall clock; it ends on the grant or on
    `abandon`. `on_wait(0, waited)` is called at least every ON_WAIT_EVERY_S
    meanwhile — position 0, as in `_yield_to_chat`: the wait is admission's,
    not a place in this gate's line."""
    from ..config import settings

    messages, max_tokens = request
    remaining = None if deadline is None else max(0.0, deadline - loop.time())
    begun = loop.time() if started is None else started
    task = loop.create_task(
        admission.preadmit(
            messages,
            base_url=str(getattr(settings, "openai_base_url", "") or ""),
            model=str(getattr(settings, "llm_model", "") or ""),
            max_tokens=max_tokens,
            wait_s=remaining,
        )
    )
    abandon_task: Optional[asyncio.Task] = None
    waits = {task}
    if abandon is not None:
        abandon_task = loop.create_task(abandon.wait())
        waits.add(abandon_task)
    try:
        if remaining is not None:
            # A small grace past the lane's own bound: the lane refuses first, with
            # its reason; the wall clock only catches a wait the lane deferred.
            await asyncio.wait(waits, timeout=remaining + 0.5, return_when=asyncio.FIRST_COMPLETED)
        else:
            while not task.done() and not (abandon is not None and abandon.is_set()):
                await asyncio.wait(
                    waits, timeout=ON_WAIT_EVERY_S if on_wait is not None else None,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if on_wait is not None and not task.done():
                    await _notify_wait(on_wait, 0, loop.time() - begun)
    except BaseException:
        task.cancel()
        with contextlib.suppress(BaseException):
            ticket = await task
            if ticket is not None:
                ticket.release_nowait()
        raise
    finally:
        if abandon_task is not None:
            abandon_task.cancel()
    if not task.done():
        task.cancel()
        with contextlib.suppress(BaseException):
            late = await task
            if late is not None:  # granted as it was cancelled: give it back
                late.release_nowait()
        if abandon is not None and abandon.is_set():
            raise Abandoned()
        raise errors.model_at_capacity(retry_after=retry_after)
    exc = task.exception()
    if exc is not None:
        if type(exc).__name__ == "AdmissionRejected":
            raise errors.model_at_capacity(retry_after=retry_after)
        raise exc
    ticket = task.result()
    if abandon is not None and abandon.is_set():
        if ticket is not None:
            ticket.release_nowait()
        raise Abandoned()
    return ticket


@contextlib.asynccontextmanager
async def _hold_main(
    engine: str,
    *,
    wait_s: Optional[float],
    abandon: Optional[asyncio.Event],
    work: Any,
    on_wait: Optional["OnWait"] = None,
    front: bool = False,
) -> AsyncIterator[None]:
    """A main gate: yield to a large prefill ahead, the one-at-a-time wait for
    main.long, then the answer's admission (module docstring). `wait_s=None`
    waits with no limit at every step (NO CLOCK ENDS A /v1 WAIT)."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = None if wait_s is None else started + max(0.0, float(wait_s))
    retry_after = _config(engine).retry_after
    await _yield_to_chat(engine, deadline, loop, abandon, on_wait)
    request = _work_request(work)
    front_door = _admission_front_door() if request is not None else None
    # Set in THIS context, before the pre-admission and before the generation's
    # tasks are created: they see /v1 as their origin.
    origin = _set_public_origin()
    try:
        async with contextlib.AsyncExitStack() as stack:
            if engine == GATE_MAIN_LONG or front_door is None:
                # main.long's one-at-a-time wait comes FIRST: a second 1M
                # answer must not sit on a LONG_OUTPUT seat while it waits.
                await stack.enter_async_context(
                    _counted(engine, deadline=deadline, loop=loop, abandon=abandon, on_wait=on_wait,
                             front=front, started=started)
                )
            if front_door is not None:
                gate = _gate(engine)
                gate.admitting += 1
                _publish(engine, gate)
                try:
                    ticket = await _preadmit(
                        front_door, request, deadline=deadline, loop=loop, abandon=abandon,
                        retry_after=retry_after, on_wait=on_wait, started=started,
                    )
                finally:
                    gate.admitting = max(0, gate.admitting - 1)
                    _publish(engine, gate)
                if ticket is not None:
                    stack.callback(ticket.release_nowait)
                # Always, with the planned output — ticket or not: a retry after
                # the ticket's release and a LONG prompt (never pre-admitted)
                # are still admitted as the long answer they are, not NORMAL
                # with a prompt-only charge (integration review 2026-09-13).
                stack.enter_context(_use_preadmitted(front_door, ticket, request[1]))
                if engine != GATE_MAIN_LONG:
                    if ticket is None:
                        # Admission did not take this answer into LONG_OUTPUT —
                        # a LONG prompt, or ADMISSION_V1_LONG_OUTPUT_THRESHOLD_TOKENS
                        # set above PUBLIC_API_MAIN_EXTENDED_OUTPUT_TOKENS — so no
                        # LONG_OUTPUT seat caps it: the gate's own count does.
                        await stack.enter_async_context(
                            _counted(engine, deadline=deadline, loop=loop, abandon=abandon, on_wait=on_wait,
                                     front=front, started=started)
                        )
                    else:
                        stack.enter_context(_tallied(engine))
            yield
    finally:
        _reset_public_origin(origin)


def gates_for(engine: str, gate_engine: Optional[str]) -> List[str]:
    """Every gate one public generation holds, in acquisition order.

    Main model: `main.long` alone when its footprint is long (it runs in the
    LONG lane, not NORMAL); otherwise `main.extended` (when planned) THEN
    `main.normal` — one fixed order, so no two runs can each hold the gate
    the other waits for. Sidecars: their own gate, when planned."""
    if engine == registry.ENGINE_MAIN:
        if gate_engine == GATE_MAIN_LONG:
            return [GATE_MAIN_LONG]
        if gate_engine == GATE_MAIN_EXTENDED:
            return [GATE_MAIN_EXTENDED, GATE_MAIN_NORMAL]
        return [GATE_MAIN_NORMAL]
    return [gate_engine] if gate_engine else []


def has_room(engine: str, weight_tokens: int = 0) -> bool:
    """Would `hold(engine)` admit at once? The background dispatcher asks
    before it claims a row, so a queued job holds no coroutine."""
    try:
        config = _config(engine)
        gate = _gate(engine)
    except (ValueError, RuntimeError):
        return True
    charge = min(max(0, int(weight_tokens or 0)), config.budget_tokens) if config.budget_tokens > 0 else 0
    live = [w for w in gate.waiters if not w.future.done()]
    return not live and _admissible(gate, config, charge)


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
        max_concurrent = config.max_concurrent
        if engine == GATE_MAIN_EXTENDED:
            max_concurrent = _long_output_seats(max_concurrent)
        out[engine] = {
            "in_flight": gate.in_flight if gate else 0,
            "waiting": _waiting(gate) if gate else 0,
            "budget_tokens": config.budget_tokens,
            "used_tokens": gate.used_tokens if gate else 0,
            "max_concurrent": max_concurrent,
        }
    return out


def _long_output_seats(fallback: int) -> int:
    """Admission's LONG_OUTPUT seats — the real cap of main.extended — or the
    fallback count when admission has no front door."""
    admission = _admission_front_door()
    seats = getattr(admission, "long_output_max_seqs", None) if admission is not None else None
    if not callable(seats):
        return int(fallback)
    try:
        return int(seats())
    except Exception:  # noqa: BLE001
        return int(fallback)


__all__ = [
    "Abandoned",
    "GATES",
    "GATE_ASR",
    "GATE_EMBED",
    "GATE_MAIN_EXTENDED",
    "GATE_MAIN_LONG",
    "GATE_MAIN_NORMAL",
    "MAIN_GATES",
    "PublicMainGeneration",
    "GATE_OCR",
    "GATE_RERANK",
    "GATE_ROUTER",
    "background_wait_s",
    "chat_is_busy",
    "chat_long_admission_present",
    "dictation_is_busy",
    "gates_for",
    "has_room",
    "hold",
    "snapshot",
    "sync_wait_s",
]
