"""How `/v1` actually generates — the execution path of CONTRACT §11 and the
two streams of CONTRACT §10.

ONE ENTRY POINT INTO THE ENGINE, AND IT IS `llm.stream_chat_events`. Nothing
in this module builds a vLLM client, names a base URL, calls the router, the
embeddings service, OCR or the reranker, or reaches into `app/main.py`. That
is not tidiness: `stream_chat_events` is where `assert_answer_engine` runs,
where the breaker and the admission lanes are entered, and where the wall
clock lives. A second path to the model would be a second set of those
guarantees, and the one that got skipped would be the one nobody remembered.

FOUR PROPERTIES THIS FILE EXISTS TO HOLD.

1. **The generator is always closed.** `finally: await stream.aclose()`, in
   the task that opened it. Without it an abandoned request holds an
   admission lane and an open upstream response until garbage collection —
   the failure app/continuation.py was written around, and the reason a
   browser tab that navigates away must not cost the next caller a lane.

2. **Usage is read where it was written.** `llm.get_usage()` reads a
   `ContextVar`, and an asyncio task gets a COPY of the context at creation.
   So `llm.reset_usage()` is called INSIDE the generating task and
   `llm.get_usage()` is read in that task's `finally`; the value travels back
   to the caller as a value, not through the variable. Reading it from the
   outer task would always have returned None, and None rendered as 0 is an
   under-charge that looks like a working meter (CONTRACT §9).

3. **A silent generation still breathes.** A heartbeat comment goes out at
   least every `events.HEARTBEAT_SECONDS`, which the public contract caps at
   15 s whatever an operator set for the chat app. The model thinking, an
   admission wait and an engine reload are all legitimately silent for
   minutes, and an idle SSE body is indistinguishable from a dead socket to
   every proxy in the middle (the undici 300 s body timeout that used to be
   reported to people as "the orchestrator is unreachable").

4. **Exactly one terminal event.** `events.SequencedEvents` refuses a second
   one; this module never asks for one, and never tries to emit a frame from
   a `finally` — a generator that is being closed cannot yield, and a client
   that has gone is not owed a goodbye. What it does owe is the SERVER-SIDE
   record, which is why `on_finish` is awaited under a shield.

WHAT THIS MODULE DELIBERATELY DOES NOT DO. It does not register the
generation in `main.py::_live_generations`. That registry is keyed one per
conversation and a second request under the same key CANCELS the first: on
the chat surface that is correct (one person, one tab, one answer), and on
this surface it would be a denial of service one customer could inflict on
themselves by running two requests with the same key.

It also never asks for thinking. `PUBLIC_EFFORT` is `fast`, which on this
deployment means the reasoning pass is off — see the constant for the billing
reason, which is not the obvious one.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Mapping, Optional

from .. import llm
from . import errors, events, models

log = logging.getLogger(__name__)

#: CONTRACT §10 headers, minus the content type (the response class sets that)
#: and minus `Content-Length`, which must not be there at all: a streamed body
#: has no length, and a proxy that sees one truncates at it.
#:
#: `no-store` as well as `no-cache`: a generated answer is the caller's data,
#: and `no-transform` stops a compressing intermediary from buffering the body
#: to recompress it, which is the other way an SSE stream arrives all at once.
SSE_HEADERS: Dict[str, str] = {
    "Cache-Control": "no-store, no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

#: The effort every public request runs at — and the only one, because CONTRACT
#: §8's body has no field for it and a parameter we cannot honour must not be
#: invented.
#:
#: WHY `fast`, WHICH MEANS THINKING OFF. Not latency, though that follows:
#: `llm.stream_chat_events` sizes a THINKING call as
#: `max(max_tokens, settings.max_output_tokens)` — 65,536 on this deployment —
#: because on the chat surface the answer must still have room after the model
#: has finished thinking. On this surface that would silently overrun the
#: caller's `max_output_tokens`, against CONTRACT §8 ("a parameter this
#: platform cannot honour is rejected, never silently ignored") and CONTRACT
#: §12's clamp, and bill them for tokens they capped. With thinking off,
#: `max_tokens` is passed through exactly as the caller asked for it.
PUBLIC_EFFORT = "fast"

#: `llm.stream_chat_events` yields `(kind, delta)`; only these two kinds exist
#: today and only the first is the answer. A reasoning delta is dropped rather
#: than streamed: `/v1` publishes no thinking channel, and appending it to the
#: answer is the bug fourteen chat call sites had to be taught not to make.
TOKEN_KIND = "token"
REASONING_KIND = "reasoning"

#: What a 503 tells the caller to wait before retrying. Short enough that a
#: well-behaved client comes back inside a warm reload (3 m 32 s measured
#: 2026-09-11) without hammering, long enough that a thousand of them do not
#: arrive in the same second.
RECOVERING_RETRY_AFTER = 20.0
UNAVAILABLE_RETRY_AFTER = 30.0


# --------------------------------------------------------------- the spec --


@dataclass(frozen=True)
class GenerationSpec:
    """Everything one generation needs, decided BEFORE it starts.

    Frozen on purpose. Every field here was resolved from the API key and the
    validated body (the model from the key's allowlist, the ceiling from the
    registry), and CONTRACT §8 says nothing in the body may change the target
    — a spec that could be edited mid-flight would be a place for that to
    happen by accident.
    """

    response_id: str
    model: str
    messages: List[Dict[str, str]]
    max_tokens: int
    temperature: float
    created_at: int
    #: The id deltas are tied to, so a client can group them into one message.
    item_id: str = field(default_factory=events.new_item_id)
    effort: str = PUBLIC_EFFORT


@dataclass
class StreamOutcome:
    """What happened, as the SERVER saw it — the only thing worth metering.

    Billing from what the client received under-counts every disconnect: the
    tokens were generated and the engine time was spent whether or not the
    socket was still open to carry them.
    """

    response_id: str
    model: str
    created_at: int
    status: str = "completed"
    text: str = ""
    usage: Optional[Dict[str, Any]] = None
    error: Optional[errors.ApiError] = None
    ttft_ms: Optional[int] = None
    duration_ms: Optional[int] = None
    #: True when the caller's socket went away before the terminal event. Kept
    #: because "the answer was produced and nobody read it" is a different
    #: operational fact from "the answer failed", and both can be `completed`.
    client_gone: bool = False
    #: The engine's own finish reason (`stop`, `length`, …) or None when it did
    #: not report one. Carried as a VALUE, like `usage`, because the ContextVar
    #: it comes from was set inside the producer task (verifier finding
    #: 2026-09-13: read from the consumer it was always None, so a truncated
    #: answer went out as `finish_reason: "stop"`).
    finish_reason: Optional[str] = None

    def chat_finish_reason(self) -> str:
        return chat_finish_reason(self.finish_reason)

    def usage_model(self) -> Optional[models.Usage]:
        return models.Usage.from_llm(self.usage)

    def response(self) -> models.Response:
        """The CONTRACT §9 object for this outcome — the same one the
        streaming terminal carries and `GET /v1/responses/{id}` returns."""
        return models.Response(
            id=self.response_id,
            created_at=self.created_at,
            status=self.status,  # type: ignore[arg-type]
            model=self.model,
            output=[models.OutputMessage.of(self.text)] if self.text else [],
            usage=self.usage_model(),
            error=(
                None
                if self.error is None
                else models.ResponseError(code=self.error.code, message=self.error.message)
            ),
        )


def _outcome_for(spec: GenerationSpec) -> StreamOutcome:
    return StreamOutcome(
        response_id=spec.response_id, model=spec.model, created_at=spec.created_at
    )


# ------------------------------------------------------- engine failures --


def engine_error(exc: BaseException) -> errors.ApiError:
    """An engine-side failure → the CONTRACT §9 code that describes it.

    The distinction that matters to a caller is RETRY-SAFETY, which is why
    `model_recovering` and `model_unavailable` are separate codes for what
    looks like one condition: the first says "come back, this exact request
    will work", the second says "something is wrong". The controller's own
    verdict (`engine_state.external_open`) is what tells them apart, and when
    it has no fresh opinion the answer is the retry-safe one — a caller told
    to retry a request that then fails again has lost twenty seconds, while a
    caller told the model is down abandons work that would have succeeded.
    """
    if isinstance(exc, errors.ApiError):
        return exc

    name = type(exc).__name__
    # Imported inside the function: these modules pull in the breaker, the
    # controller client and the admission lanes, and `publicapi` must stay
    # importable by the OpenAPI generator (a lint job) without starting any of
    # that. The names are matched structurally for the same reason.
    if name == "AdmissionRejected":
        # The NORMAL and LONG lanes are shared with the chat application
        # (CONTRACT §11). A refusal there is the ENGINE at capacity, not a
        # limit on this caller: a 503 with a Retry-After, never a 500, and
        # never a 429 naming a concurrency limit the API does not enforce
        # (owner decision 2026-09-13, limits removed).
        return errors.model_at_capacity(retry_after=5)
    if name in ("QueuedForRecovery", "BreakerOpen", "ModelUnavailable"):
        return _recovery_error()
    if isinstance(exc, asyncio.TimeoutError):
        return errors.timeout()
    return errors.from_unexpected(exc)


def _recovery_error() -> errors.ApiError:
    state = _engine_state_name()
    if state in ("DOWN", "WEDGED"):
        return errors.model_unavailable(retry_after=UNAVAILABLE_RETRY_AFTER)
    return errors.model_recovering(retry_after=RECOVERING_RETRY_AFTER)


def _engine_state_name() -> Optional[str]:
    """The controller's verdict, or None when it has no fresh one.

    Never raises: a public request must not fail because the monitoring
    sidecar is having a bad day.
    """
    try:
        from .. import engine_state

        return engine_state.external_open()
    except Exception:  # noqa: BLE001 - monitoring must never break a request
        log.debug("engine state unavailable to the public API", exc_info=True)
        return None


def engine_is_recovering() -> bool:
    """Whether to announce `response.queued` before the first token."""
    return _engine_state_name() in ("STARTING", "RECOVERING")


# ------------------------------------------------------------- the pump --


@dataclass
class _Chunk:
    """One thing to come out of the pump: text, or a keep-alive tick."""

    kind: str
    text: str = ""


_HEARTBEAT = _Chunk(kind="heartbeat")


class Generation:
    """One call to `llm.stream_chat_events`, run as its own task.

    WHY A TASK AND A QUEUE rather than iterating the engine's generator
    directly. Two reasons, both load-bearing:

    * usage is a `ContextVar`, so it must be reset and read inside the task
      that runs the generation (see the module docstring). A task is the only
      thing that gives us that boundary;
    * a heartbeat has to go out on a SCHEDULE, and a plain `async for` over a
      silent generator cannot be interrupted to send one. `wait_for` over a
      queue can.

    The queue is unbounded and that is deliberate rather than careless: what
    goes into it is bounded by `max_output_tokens`, which CONTRACT §12 clamps
    to the model ceiling before this class is ever constructed. A bounded
    queue would make the producer block on `put` while a slow client drains,
    which turns a slow reader into a held admission lane — the exact failure
    the aclose rule exists to prevent.
    """

    def __init__(self, spec: GenerationSpec, *, heartbeat_s: Optional[float] = None) -> None:
        self.spec = spec
        self.heartbeat_s = float(
            heartbeat_s if heartbeat_s is not None else events.HEARTBEAT_SECONDS
        )
        self.usage: Optional[Dict[str, Any]] = None
        self.error: Optional[BaseException] = None
        self.first_token_at: Optional[float] = None
        #: Read in the producer's `finally`, next to `usage`, for the same
        #: ContextVar reason. None means the engine did not say.
        self.finish_reason: Optional[str] = None
        self._queue: "asyncio.Queue[Optional[_Chunk]]" = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None

    # -- the producer -----------------------------------------------------

    async def _run(self) -> None:
        # INSIDE the task: `_usage` is a ContextVar and a task holds a copy of
        # the context it was created in. Resetting it out there would clear
        # the caller's variable and leave this task's own untouched.
        llm.reset_usage()
        with contextlib.suppress(Exception):
            llm.reset_finish_reason()
        stream = llm.stream_chat_events(
            self.spec.messages,
            model_choice="smart",
            effort=self.spec.effort,
            temperature=self.spec.temperature,
            max_tokens=self.spec.max_tokens,
        )
        try:
            async for kind, delta in stream:
                if kind != TOKEN_KIND:
                    # A reasoning delta. `/v1` has no channel for it and must
                    # not append it to the answer.
                    continue
                if delta:
                    self._queue.put_nowait(_Chunk(kind=TOKEN_KIND, text=str(delta)))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - carried to the consumer
            self.error = exc
        finally:
            # CONTRACT §10: ALWAYS. An abandoned generator holds an admission
            # lane and an open upstream response until garbage collection.
            with contextlib.suppress(BaseException):
                await stream.aclose()
            # CONTRACT §9: None means NOT MEASURED and must never become 0.
            self.usage = llm.get_usage()
            try:
                self.finish_reason = llm.get_finish_reason()
            except Exception:  # noqa: BLE001 - a missing reason is "not reported"
                self.finish_reason = None
            self._queue.put_nowait(None)

    # -- the consumer -----------------------------------------------------

    async def stream(self) -> AsyncIterator[_Chunk]:
        """Text chunks, with a heartbeat tick whenever the engine goes quiet."""
        self._task = asyncio.ensure_future(self._run())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(self._queue.get(), self.heartbeat_s)
                except asyncio.TimeoutError:
                    yield _HEARTBEAT
                    continue
                if item is None:
                    return
                if self.first_token_at is None:
                    self.first_token_at = time.monotonic()
                yield item
        finally:
            await self._close()

    async def aclose(self) -> None:
        """Stop the producer NOW, from outside the consumer loop.

        WHY A CALLER HAS TO ASK. Closing an async generator does not close the
        async generators it was iterating: `async for` has no `with`-like
        teardown, so when a client disconnects and Starlette closes the SSE
        body, this class's own `finally` does not run until the event loop's
        async-generator finalizer gets round to it — which measured as "never,
        within the life of the request" (2026-09-13, the two disconnect tests
        in test_publicapi_streaming.py failed on exactly this). Every consumer
        here therefore calls `aclose()` in its own `finally`, which is what
        makes CONTRACT §10's closing rule prompt rather than eventual.

        Idempotent: awaiting a task that is already done is a no-op.
        """
        await self._close()

    async def _close(self) -> None:
        """Stop the producer and let its `finally` run.

        Reached on the happy path (the task is already done and this is a
        no-op) and on an abandoned request, where the consumer's generator is
        being closed underneath us — which is precisely when the engine's
        generator must be closed too.
        """
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        if self.usage is None:
            # The task was cancelled before its `finally` could read the
            # ContextVar. None is the honest answer — not measured — and the
            # ledger writes NULL rather than a zero somebody would bill.
            log.debug("public API generation ended without a usage report")


# ------------------------------------------------------ the sync path --


def new_outcome(spec: GenerationSpec) -> StreamOutcome:
    """A fresh outcome for `spec`, for a caller that must keep hold of it."""
    return _outcome_for(spec)


async def run_to_completion(
    spec: GenerationSpec,
    *,
    heartbeat_s: Optional[float] = None,
    outcome: Optional[StreamOutcome] = None,
) -> StreamOutcome:
    """Generate the whole answer and return it — `stream: false`.

    The same pump as the streaming path, drained into a string. There is no
    second code path to the engine, so the two modes cannot diverge in what
    they count, when they close the generator, or which failure they report.

    `outcome`, when given, is filled in place — INCLUDING when this coroutine
    is cancelled, because the `finally` below writes text, usage and timings
    before the cancellation propagates. The router passes one so that a
    cancellation after the engine has run can still be charged for what it
    generated (re-verifier finding 2026-09-13: the tokens were handed back as
    "nothing ran" and the row was left `in_progress`).
    """
    if outcome is None:
        outcome = _outcome_for(spec)
    generation = Generation(spec, heartbeat_s=heartbeat_s)
    started = time.monotonic()
    pieces: List[str] = []
    try:
        async for chunk in generation.stream():
            if chunk.kind == TOKEN_KIND:
                pieces.append(chunk.text)
    finally:
        await generation.aclose()
        outcome.text = "".join(pieces)
        outcome.usage = generation.usage
        outcome.finish_reason = generation.finish_reason
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        if generation.first_token_at is not None:
            outcome.ttft_ms = int((generation.first_token_at - started) * 1000)
    if generation.error is not None:
        outcome.status = "failed"
        outcome.error = engine_error(generation.error)
    return outcome


# ------------------------------------------------ the Responses stream --


OnFinish = Callable[[StreamOutcome], Awaitable[None]]


def _wire(
    spec: GenerationSpec,
    status: str,
    *,
    text: str = "",
    usage: Optional[Dict[str, Any]] = None,
    error: Optional[errors.ApiError] = None,
) -> Dict[str, Any]:
    return models.Response(
        id=spec.response_id,
        created_at=spec.created_at,
        status=status,  # type: ignore[arg-type]
        model=spec.model,
        output=[models.OutputMessage.of(text)] if text else [],
        usage=models.Usage.from_llm(usage),
        error=(
            None
            if error is None
            else models.ResponseError(code=error.code, message=error.message)
        ),
    ).to_wire()


async def _settle(on_finish: Optional[OnFinish], outcome: StreamOutcome) -> None:
    """Record the outcome even though the caller may already have gone.

    Shielded: this runs from a generator's `finally`, which on a client
    disconnect is running under cancellation. Without the shield the usage row
    and the durable status of every abandoned request would be lost, which is
    the same under-count as billing from the wire.
    """
    if on_finish is None:
        return
    try:
        await asyncio.shield(asyncio.ensure_future(on_finish(outcome)))
    except asyncio.CancelledError:
        # The shield's own await was cancelled; the task it wraps carries on.
        pass
    except Exception:  # noqa: BLE001 - recording must never break a response
        log.warning("public API outcome was not recorded", exc_info=True)


#: The shielded recorder, for the router's synchronous path (2026-09-13),
#: which records a cancelled generation from its own `except`.
settle = _settle


async def responses_sse(
    spec: GenerationSpec,
    *,
    on_finish: Optional[OnFinish] = None,
    heartbeat_s: Optional[float] = None,
) -> AsyncIterator[str]:
    """The CONTRACT §10 lifecycle for one `POST /v1/responses` with `stream: true`.

        response.created → [response.queued] → response.in_progress
          → response.output_text.delta (×N) → response.output_text.done
          → response.completed

    with `response.failed` replacing the tail on any engine-side failure.
    `events.SequencedEvents` numbers the frames and refuses a second terminal;
    nothing here emits one from a `finally`, because a generator that is being
    closed cannot yield and a client that has gone is not owed a frame.
    """
    emitter = events.SequencedEvents(item_id=spec.item_id)
    generation = Generation(spec, heartbeat_s=heartbeat_s)
    outcome = _outcome_for(spec)
    started = time.monotonic()
    pieces: List[str] = []
    try:
        try:
            yield emitter.created(_wire(spec, "queued"))
            if engine_is_recovering():
                # CONTRACT §10: say why nothing is arriving, so the caller
                # does not read a legitimate wait as a dead connection.
                yield emitter.queued(_wire(spec, "queued"))
            yield emitter.in_progress(_wire(spec, "in_progress"))
            async for chunk in generation.stream():
                if chunk.kind != TOKEN_KIND:
                    yield emitter.heartbeat()
                    continue
                pieces.append(chunk.text)
                yield emitter.output_text_delta(chunk.text)
            if generation.error is not None:
                raise generation.error
            text = "".join(pieces)
            outcome.text = text
            outcome.usage = generation.usage
            yield emitter.output_text_done(text)
            yield emitter.completed(
                _wire(spec, "completed", text=text, usage=generation.usage)
            )
        except Exception as exc:  # noqa: BLE001 - every failure is a frame
            # GeneratorExit and CancelledError are BaseException and do NOT
            # come through here: a client that has gone gets no frame, and its
            # outcome is recorded by the `finally` below.
            failure = engine_error(exc)
            outcome.status = "failed"
            outcome.error = failure
            outcome.text = "".join(pieces)
            outcome.usage = generation.usage
            if not emitter.finished:
                # Partial usage belongs on this terminal: the tokens were
                # produced and the engine time was spent.
                yield emitter.failed(
                    _wire(
                        spec,
                        "failed",
                        text=outcome.text,
                        usage=generation.usage,
                        error=failure,
                    )
                )
    finally:
        await generation.aclose()
        if not emitter.finished:
            outcome.client_gone = True
        if not pieces and outcome.status == "completed" and not outcome.text:
            outcome.text = ""
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        if generation.first_token_at is not None:
            outcome.ttft_ms = int((generation.first_token_at - started) * 1000)
        if outcome.usage is None:
            outcome.usage = generation.usage
        if outcome.finish_reason is None:
            outcome.finish_reason = generation.finish_reason
        await _settle(on_finish, outcome)


# ------------------------------------------ the Chat Completions stream --


async def chat_completions_sse(
    spec: GenerationSpec,
    *,
    completion_id: str,
    include_usage: bool = False,
    on_finish: Optional[OnFinish] = None,
    heartbeat_s: Optional[float] = None,
) -> AsyncIterator[str]:
    """The compatibility dialect: anonymous chunks, then `data: [DONE]`.

    Same pump, same closing rule, same metering. The wire shape differs
    because an existing client library reads it, and every difference from
    `responses_sse` below is one of those clients' requirements rather than a
    preference (see `events.ChatCompletionChunks`).
    """
    chunks = events.ChatCompletionChunks(
        completion_id=completion_id,
        model=spec.model,
        created=spec.created_at,
        include_usage=include_usage,
    )
    generation = Generation(spec, heartbeat_s=heartbeat_s)
    outcome = _outcome_for(spec)
    started = time.monotonic()
    pieces: List[str] = []
    try:
        try:
            async for chunk in generation.stream():
                if chunk.kind != TOKEN_KIND:
                    yield chunks.heartbeat()
                    continue
                pieces.append(chunk.text)
                yield chunks.delta(chunk.text)
            if generation.error is not None:
                raise generation.error
            outcome.text = "".join(pieces)
            outcome.usage = generation.usage
            outcome.finish_reason = generation.finish_reason
            yield chunks.stop(chat_finish_reason(generation.finish_reason))
            if include_usage:
                yield chunks.usage_chunk(_completion_usage(generation.usage))
            yield chunks.done()
        except Exception as exc:  # noqa: BLE001
            failure = engine_error(exc)
            outcome.status = "failed"
            outcome.error = failure
            outcome.text = "".join(pieces)
            outcome.usage = generation.usage
            if not chunks.finished:
                yield chunks.error_chunk(failure)
                yield chunks.done()
    finally:
        await generation.aclose()
        if not chunks.finished:
            outcome.client_gone = True
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        if generation.first_token_at is not None:
            outcome.ttft_ms = int((generation.first_token_at - started) * 1000)
        if outcome.usage is None:
            outcome.usage = generation.usage
        if outcome.finish_reason is None:
            outcome.finish_reason = generation.finish_reason
        await _settle(on_finish, outcome)


def chat_finish_reason(reason: Optional[str]) -> str:
    """`length` when the answer hit its ceiling, `stop` otherwise.

    `reason` is the value the PRODUCER task captured (`Generation.finish_reason`),
    never `llm.get_finish_reason()` read here: that ContextVar is set inside
    the generating task, and until 2026-09-13 this function read it from the
    consumer, where it is always None — so every truncated answer was reported
    as `stop` and a client that continues on `length` kept half an answer.
    Anything that is not literally `length` is `stop`; there is no third value
    in the compatibility dialect a client would know what to do with.
    """
    return "length" if str(reason or "") == "length" else "stop"


def _completion_usage(usage: Optional[Mapping[str, Any]]) -> Optional[Dict[str, int]]:
    """`llm.get_usage()` in the Chat Completions spelling, or None.

    None stays None all the way to the wire: CONTRACT §9's rule that a
    not-measured count is never rendered as zero is not a Responses-only rule,
    and a proxy doing cost accounting off this field would book the zero.
    """
    counted = models.Usage.from_llm(usage)
    if counted is None:
        return None
    return {
        "prompt_tokens": counted.input_tokens,
        "completion_tokens": counted.output_tokens,
        "total_tokens": counted.total_tokens,
    }
