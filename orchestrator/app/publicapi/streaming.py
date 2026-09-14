"""How `/v1` actually generates — the execution path of CONTRACT §11 and the
two streams of CONTRACT §10.

ONE ENTRY POINT PER ENGINE (2026-09-13). The MAIN model is reached through
`llm.stream_chat_events` and nothing lower: that is where
`assert_answer_engine` runs, where the breaker and the admission lanes are
entered, and where `_fit` sizes the call with the engine's own `/tokenize`. A
second path to the main model would be a second set of those guarantees, and
the one that got skipped would be the one nobody remembered. The two sidecar
chat models the owner asked to publish (techsara-8b-vision on the router,
techsara-ocr on Unlimited-OCR) have none of that machinery, and are reached
through `publicapi/engines.stream_chat`, which yields the same `(kind, delta)`
pairs — so everything below (the task, the queue, the heartbeat, the closing
rule, the single terminal) is shared by all three. Nothing in this module
builds a client or names a base URL.

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

NO WALL CLOCK (no-timeout design, 2026-09-13). A generation is one ATTEMPT
here: it ends when the engine finishes, when the caller or the durable runner
closes it, or when its liveness guard (`liveness.MainGuard` /
`liveness.SidecarGuard`, ticked at least every heartbeat) proves the engine
wedged, down, or has lost the request. The per-request wall clock
(`planning.wall_clock_for`, up to 6 h) and the 30 s backstop are deleted: a
clock cannot tell a 30-minute silent prefill from a dead engine. An interrupt
is recorded on `Generation.interrupt`; the durable runner (durable.py) resumes
from it by continuation, and the non-durable paths (`store: false`) report it
as the retry-safe `model_unavailable`.

It also never asks for thinking. `PUBLIC_EFFORT` is `fast`, which on this
deployment means the reasoning pass is off — see the constant for the billing
reason, which is not the obvious one.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence

from .. import llm
from ..config import settings
from . import capacity, engines, errors, events, models, planning, registry

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

#: What `usage["source"]` says when the engine never sent its usage report and
#: the counts were taken by this server (an interrupted attempt, 2026-09-13).
USAGE_COUNTED_AT_STOP = "counted_at_stop"


class ContinuationUnsupported(RuntimeError):
    """A resume needs `continue_final_message` and this engine path does not
    accept it (SHIM(T1): llm.stream_chat_events before its kwargs land). The
    durable runner fails the run retryably rather than regenerate the text."""


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
    messages: List[Dict[str, Any]]
    #: What the ENGINE is sent (`GenerationPlan.max_tokens_for_engine`).
    max_tokens: int
    temperature: float
    created_at: int
    #: The id deltas are tied to, so a client can group them into one message.
    item_id: str = field(default_factory=events.new_item_id)
    effort: str = PUBLIC_EFFORT
    #: The registry's engine key. `main` goes through `llm.stream_chat_events`;
    #: `router` and `ocr` through `engines.stream_chat`.
    engine: str = registry.ENGINE_MAIN
    #: RETIRED 2026-09-13 (no wall clock on /v1). Kept so a stored spec or a
    #: caller that still passes it keeps constructing; never read.
    wall_clock_s: Optional[float] = None
    requested_max_output_tokens: Optional[int] = None
    planned_max_output_tokens: Optional[int] = None
    context_window: Optional[int] = None
    context_reserve: int = 0
    #: The capacity gate a BACKGROUND job must hold while it runs (the
    #: synchronous and streaming paths take theirs in the router, before the
    #: status line). None: no public gate.
    gate_engine: Optional[str] = None
    gate_weight_tokens: int = 0
    yield_to_chat: bool = False
    #: The planner's input counts (2026-09-13): the estimate stands in for the
    #: prompt when a wall-clock stop leaves no engine report and no exact
    #: count; the byte bound sizes the one retry after an engine refusal that
    #: an estimated clamp caused (`Generation._window_retry_tokens`).
    estimated_input_tokens: Optional[int] = None
    bounded_input_tokens: Optional[int] = None

    @property
    def planned(self) -> int:
        """The ceiling announced before generation (the `max_output_tokens`
        of `response.created` and of a background 202)."""
        return int(self.planned_max_output_tokens or self.max_tokens)


def spec_from_plan(
    plan: "planning.GenerationPlan",
    *,
    response_id: str,
    created_at: int,
) -> GenerationSpec:
    """The one way a planned request becomes a spec, for every caller."""
    return GenerationSpec(
        response_id=response_id,
        model=plan.model.id,
        messages=plan.messages,
        max_tokens=plan.max_tokens_for_engine,
        temperature=plan.temperature,
        created_at=created_at,
        engine=plan.engine,
        requested_max_output_tokens=plan.requested_max_output_tokens,
        planned_max_output_tokens=plan.planned_max_output_tokens,
        context_window=plan.model.context_window,
        context_reserve=plan.context_reserve,
        gate_engine=plan.gate_engine,
        gate_weight_tokens=plan.gate_weight_tokens,
        yield_to_chat=plan.yield_to_chat,
        estimated_input_tokens=plan.estimated_input_tokens,
        bounded_input_tokens=plan.bounded_input_tokens,
    )


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
    #: The output ceiling APPLIED (2026-09-13): the planned value until the
    #: generation has run, the exact value after.
    max_output_tokens: Optional[int] = None
    #: True only for a BACKGROUND response, the one mode whose text is kept
    #: (SCHEMA-V34; CONTRACT §16 keeps no synchronous or streamed text). When
    #: set, `background.persisted_generation_fields` puts `output_text` in the
    #: SAME row update as the terminal status, so a caller polling
    #: `GET /v1/responses/{id}` can never read `completed`/`failed` with an
    #: empty `output` in between two writes (CONTRACT §8.3 promises the text
    #: a timed-out response produced).
    keep_text: bool = False

    def chat_finish_reason(self) -> str:
        return chat_finish_reason(self.finish_reason)

    def usage_model(self) -> Optional[models.Usage]:
        return models.Usage.from_llm(self.usage)

    def incomplete_details(self) -> Optional[models.IncompleteDetails]:
        return models.IncompleteDetails.for_finish(self.finish_reason)

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
            max_output_tokens=self.max_output_tokens,
            incomplete_details=(self.incomplete_details() if self.status == "completed" else None),
            error=(
                None
                if self.error is None
                else models.ResponseError(code=self.error.code, message=self.error.message)
            ),
        )


def _outcome_for(spec: GenerationSpec) -> StreamOutcome:
    return StreamOutcome(
        response_id=spec.response_id,
        model=spec.model,
        created_at=spec.created_at,
        max_output_tokens=spec.planned,
    )


# ------------------------------------------------------- engine failures --


def engine_error(exc: BaseException, engine: str = registry.ENGINE_MAIN) -> errors.ApiError:
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
    if engine != registry.ENGINE_MAIN:
        # A sidecar has no breaker, no lanes and no controller verdict: the
        # main engine's recovery state says nothing about the router's, so
        # its failures are mapped on their own terms (fixed sentences only).
        return engines.engine_error(exc, engine)

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
    # An engine 400 on the main model (an image its processor cannot read, a
    # prompt the engine refuses) is the caller's to fix, not a 500 — mapped
    # to a fixed sentence, never the engine's own text.
    mapped = engines.map_engine_exception(exc, engine)
    if mapped is not None and mapped.status == 400:
        return mapped
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

    def __init__(
        self,
        spec: GenerationSpec,
        *,
        heartbeat_s: Optional[float] = None,
        guard: Any = None,
        messages: Optional[List[Dict[str, Any]]] = None,
        max_tokens: Optional[int] = None,
        continue_final_message: bool = False,
        admission_patient: bool = False,
        on_dispatch: Optional[Callable[[], None]] = None,
        admission_run_id: Optional[str] = None,
    ) -> None:
        self.spec = spec
        self.heartbeat_s = float(
            heartbeat_s if heartbeat_s is not None else events.HEARTBEAT_SECONDS
        )
        #: The liveness guard ticked by `stream()`: `liveness.MainGuard` for
        #: the main model, `liveness.SidecarGuard` otherwise. Built here when
        #: the caller passes none, so no public attempt runs unguarded.
        self.guard = guard if guard is not None else _default_guard(spec)
        #: One ATTEMPT's shape: the durable runner passes the continuation
        #: messages and the remaining output budget on a resume.
        self.messages = list(messages) if messages is not None else spec.messages
        self.attempt_max_tokens = None if max_tokens is None else int(max_tokens)
        self.continue_final_message = bool(continue_final_message)
        self.admission_patient = bool(admission_patient)
        #: T1's `admission_run_id`: ties a patient LONG ticket to the run's
        #: `admission.register_yield(run_id, …)`, so a chat LONG turn can ask
        #: THIS run to yield. None: no yield can be matched.
        self.admission_run_id = admission_run_id
        self._on_dispatch = on_dispatch
        #: Set when the guard interrupted this attempt (a `liveness.Verdict`).
        #: Not an error: the durable runner resumes from it.
        self.interrupt: Any = None
        self.usage: Optional[Dict[str, Any]] = None
        self.error: Optional[BaseException] = None
        self.first_token_at: Optional[float] = None
        #: Read in the producer's `finally`, next to `usage`, for the same
        #: ContextVar reason. None means the engine did not say.
        self.finish_reason: Optional[str] = None
        #: `llm.get_applied_max_tokens()`, read in the producer, when llm.py
        #: exposes it (needs_integration); None otherwise.
        self.llm_applied_max_tokens: Optional[int] = None
        #: Every non-empty delta the engine streamed (answer and reasoning).
        #: One streamed chunk is one token on this deployment (llm.py,
        #: verified: usage.completion_tokens == chunk count), so after an
        #: interrupted attempt — closed BEFORE vLLM's final usage chunk —
        #: this is the output count.
        self.streamed_deltas = 0
        #: Set when the one window retry ran: the `max_tokens` it was sent.
        self.retried_max_tokens: Optional[int] = None
        self._queue: "asyncio.Queue[Optional[_Chunk]]" = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._last_tick = 0.0

    # -- the producer -----------------------------------------------------

    def _dispatched(self) -> None:
        """llm's on_dispatch: the request is past admission and written to the
        engine. The guard only judges silence from here."""
        with contextlib.suppress(Exception):
            self.guard.dispatched()
        if self._on_dispatch is not None:
            with contextlib.suppress(Exception):
                self._on_dispatch()

    def _open_engine_stream(self, max_tokens: Optional[int] = None) -> AsyncIterator[Any]:
        """The engine's `(kind, delta)` generator for this attempt."""
        spec = self.spec
        wanted = max_tokens if max_tokens is not None else self.attempt_max_tokens
        tokens = int(wanted if wanted is not None else spec.max_tokens)
        if spec.engine == registry.ENGINE_MAIN:
            kwargs: Dict[str, Any] = dict(
                model_choice="smart",
                effort=spec.effort,
                temperature=spec.temperature,
                max_tokens=tokens,
            )
            accepted = _accepted_keywords(
                llm.stream_chat_events,
                ("wall_clock_s", "wall_clock_marker", "read_timeout_s", "continue_final_message",
                 "admission_patient", "on_dispatch", "admission_run_id"),
            )
            # T1 kwargs, passed only when llm accepts them so the two files
            # land in either order. 0 means "no wall clock"; None means "no
            # read timeout" (TCP keepalive finds a half-open peer instead).
            if "wall_clock_s" in accepted:
                kwargs["wall_clock_s"] = 0
            if "wall_clock_marker" in accepted:
                kwargs["wall_clock_marker"] = False
            if "read_timeout_s" in accepted:
                kwargs["read_timeout_s"] = None
            if "admission_patient" in accepted:
                kwargs["admission_patient"] = self.admission_patient
            if "on_dispatch" in accepted:
                kwargs["on_dispatch"] = self._dispatched
            if self.admission_run_id and "admission_run_id" in accepted:
                kwargs["admission_run_id"] = self.admission_run_id
            if self.continue_final_message:
                if "continue_final_message" not in accepted:
                    raise ContinuationUnsupported()
                kwargs["continue_final_message"] = True
            return llm.stream_chat_events(self.messages, **kwargs)
        resolved = engines.target(spec.engine)
        if resolved is None:
            raise errors.model_unavailable(retry_after=UNAVAILABLE_RETRY_AFTER)
        sidecar_kwargs: Dict[str, Any] = dict(
            max_tokens=tokens,
            temperature=spec.temperature,
        )
        accepted = _accepted_keywords(engines.stream_chat, ("continue_final_message", "on_dispatch"))
        if "on_dispatch" in accepted:
            sidecar_kwargs["on_dispatch"] = self._dispatched
        if self.continue_final_message:
            if "continue_final_message" not in accepted:
                raise ContinuationUnsupported()
            sidecar_kwargs["continue_final_message"] = True
        return engines.stream_chat(resolved, self.messages, **sidecar_kwargs)

    def _window_retry_tokens(self) -> Optional[int]:
        """The `max_tokens` for the ONE retry after the main engine refused a
        request as too long before producing anything — or None when no retry
        can help.

        WHY (adversarial review 2026-09-13). techsara-35b is sent `requested`
        and `llm._fit` clamps it with the engine's `/tokenize` count. When
        `/tokenize` cannot answer (its 5 s timeout, a transient failure, a
        payload it refuses) `_fit` clamps on the 3-chars-per-token ESTIMATE,
        and a prompt the estimate under-counts (digits: one token each) plus
        a large `max_output_tokens` went past the window: vLLM answered 400
        and the caller got `context_length_exceeded` blaming an input that was
        inside `max_input_tokens`. The owner's rule is clamp, never refuse.
        So the request is retried once, clamped on the prompt's BYTE bound —
        which no prompt can exceed — leaving at least MIN_OUTPUT_TOKENS (256),
        because `max_input_tokens` is the window minus that and the reserve.
        """
        spec = self.spec
        if spec.engine != registry.ENGINE_MAIN or self.retried_max_tokens is not None:
            return None
        window = int(spec.context_window or 0)
        bounded = spec.bounded_input_tokens
        if window <= 0 or bounded is None:
            return None
        if self.continue_final_message:
            # A continuation is sized by the durable runner; never re-clamped here.
            return None
        requested = int(spec.requested_max_output_tokens or spec.max_tokens)
        room = window - int(bounded) - int(spec.context_reserve or 0)
        fallback = min(requested, room)
        if fallback < 1 or fallback >= int(spec.max_tokens):
            return None
        return int(fallback)

    def _counted_usage(self) -> Optional[Dict[str, Any]]:
        """Usage for an attempt closed before vLLM's final usage chunk arrived
        (an interrupt, or an older llm's chat wall clock) — the usage chunk is
        the stream's last, so every such attempt lacks it.

        Output: the deltas this server received and counted. Input: the exact
        `/tokenize` count `llm._fit` took for this very call when it took one
        (main engine), else the planner's estimate. `source` says the counts
        are ours, and the ledger meta carries it (CONTRACT §8.3)."""
        spec = self.spec
        prompt: Optional[int] = None
        if spec.engine == registry.ENGINE_MAIN:
            try:
                from .. import context

                measured = context._measured.get()
                if measured is not None:
                    prompt = int(measured[2])
            except Exception:  # noqa: BLE001 - a missing count falls back to the plan
                prompt = None
        if prompt is None and spec.estimated_input_tokens is not None:
            prompt = int(spec.estimated_input_tokens)
        if prompt is None:
            try:
                from .. import context

                prompt = int(context.estimate_messages(spec.messages))
            except Exception:  # noqa: BLE001
                return None
        return {
            "prompt_tokens": max(0, int(prompt)),
            "completion_tokens": max(0, int(self.streamed_deltas)),
            "calls": 1,
            "source": USAGE_COUNTED_AT_STOP,
        }

    async def _run(self) -> None:
        # INSIDE the task: `_usage` is a ContextVar and a task holds a copy of
        # the context it was created in. Resetting it out there would clear
        # the caller's variable and leave this task's own untouched.
        llm.reset_usage()
        with contextlib.suppress(Exception):
            llm.reset_finish_reason()
        reset_applied = getattr(llm, "reset_applied_max_tokens", None)
        if callable(reset_applied):
            with contextlib.suppress(Exception):
                reset_applied()
        stream: Any = None
        spec = self.spec
        tracked = contextlib.nullcontext()
        if spec.engine == registry.ENGINE_MAIN:
            threshold = int(getattr(settings, "admission_long_threshold_tokens", 131_072) or 131_072)
            tracked = capacity.PublicMainGeneration(
                possibly_long_prompt=int(spec.bounded_input_tokens or 0) > threshold,
                long_lived=spec.gate_engine in capacity.MAIN_GATES,
            )
        try:
            with tracked:
                max_tokens: Optional[int] = None
                while True:
                    stream = self._open_engine_stream(max_tokens)
                    try:
                        async for kind, delta in stream:
                            if delta and _wall_clock_marker_pending():
                                # The chat app's in-band "[generation stopped
                                # after Ns — wall-clock guard …]" token. It is
                                # set together with the WALL_CLOCK finish
                                # reason, in THIS task's context, the moment
                                # before it is yielded; on /v1 the stop is a
                                # `timeout` failure, never text in the answer.
                                continue
                            if delta:
                                self.streamed_deltas += 1
                                with contextlib.suppress(Exception):
                                    self.guard.chunk()
                                if isinstance(tracked, capacity.PublicMainGeneration):
                                    tracked.first_token()
                            if kind != TOKEN_KIND:
                                # A reasoning delta. `/v1` has no channel for
                                # it and must not append it to the answer.
                                continue
                            if delta:
                                self._queue.put_nowait(_Chunk(kind=TOKEN_KIND, text=str(delta)))
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - maybe the one retry
                        retry = (
                            self._window_retry_tokens()
                            if self.streamed_deltas == 0
                            and getattr(
                                engines.map_engine_exception(exc, spec.engine), "code", None
                            )
                            == "context_length_exceeded"
                            else None
                        )
                        if retry is None:
                            raise
                        log.warning(
                            "public %s generation %s refused as too long with max_tokens "
                            "from an estimated clamp; retrying once at %d from the byte bound",
                            spec.engine, spec.response_id, retry,
                        )
                        with contextlib.suppress(BaseException):
                            await stream.aclose()
                        stream = None
                        self.retried_max_tokens = retry
                        max_tokens = retry
                        continue
                    break
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - carried to the consumer
            self.error = exc
        finally:
            # CONTRACT §10: ALWAYS. An abandoned generator holds an admission
            # lane and an open upstream response until garbage collection.
            if stream is not None:
                with contextlib.suppress(BaseException):
                    await stream.aclose()
            # CONTRACT §9: None means NOT MEASURED and must never become 0.
            self.usage = llm.get_usage()
            try:
                self.finish_reason = llm.get_finish_reason()
            except Exception:  # noqa: BLE001 - a missing reason is "not reported"
                self.finish_reason = None
            applied = getattr(llm, "get_applied_max_tokens", None)
            if callable(applied):
                with contextlib.suppress(Exception):
                    value = applied()
                    self.llm_applied_max_tokens = int(value) if value else None
            cut = self.interrupt is not None or self.finish_reason == llm.WALL_CLOCK_FINISH
            if cut and self.usage is None:
                # Adversarial review 2026-09-13: vLLM sends usage in the LAST
                # chunk only, and a closed stream never gets it — so a
                # multi-hour answer cut short was ledgered as "not measured".
                # CONTRACT §8.3 promises its usage; these are the counts this
                # server can stand behind.
                self.usage = self._counted_usage()
            self._queue.put_nowait(None)

    # -- the consumer -----------------------------------------------------

    def _tick(self, now: float) -> bool:
        """Tick the guard; True when it interrupted this attempt."""
        self._last_tick = now
        try:
            verdict = self.guard.tick()
        except Exception:  # noqa: BLE001 - a broken guard never ends a generation
            log.debug("liveness guard tick failed", exc_info=True)
            return False
        if getattr(verdict, "interrupt", False):
            self.interrupt = verdict
            log.warning(
                "public %s generation %s interrupted by liveness (%s)",
                self.spec.engine, self.spec.response_id, getattr(verdict, "reason", "?"),
            )
            return True
        return False

    async def stream(self) -> AsyncIterator[_Chunk]:
        """Text chunks, with a heartbeat tick whenever the engine goes quiet.

        The liveness guard is ticked on every quiet heartbeat and at least
        every heartbeat interval while text flows. An interrupt closes the
        producer (vLLM frees the KV) and ends the iteration; `interrupt`
        says why. There is no clock here: a silent engine that the guard does
        not prove bad is waited on for as long as it takes."""
        self._task = asyncio.ensure_future(self._run())
        self._last_tick = time.monotonic()
        try:
            while True:
                try:
                    # asyncio.timeout, not asyncio.wait_for (2026-09-14). On
                    # Python 3.11 wait_for swallows a cancellation that lands
                    # in the same loop pass as the queue item it was waiting
                    # for (fixed in 3.12), so cancelling a busy stream — a
                    # client disconnect, a job cancel — could leave this loop
                    # consuming forever. It hung CI (Python 3.11) at
                    # test_a_chat_document_beside_a_decoding_public_1m_answer_
                    # follows_the_owner_policy[proceed]; the CPU image runs
                    # 3.11 too. asyncio.timeout propagates the cancellation on
                    # both versions.
                    async with asyncio.timeout(self.heartbeat_s):
                        item = await self._queue.get()
                except TimeoutError:
                    if self._tick(time.monotonic()):
                        return
                    yield _HEARTBEAT
                    continue
                if item is None:
                    return
                if self.first_token_at is None:
                    self.first_token_at = time.monotonic()
                now = time.monotonic()
                if now - self._last_tick >= self.heartbeat_s and self._tick(now):
                    return
                yield item
        finally:
            await self._close()

    def applied_max_output_tokens(self) -> int:
        """What this generation was actually allowed (planning's rule), never
        more than the window retry sent when it ran."""
        spec = self.spec
        usage = self.usage
        if usage and usage.get("source") == USAGE_COUNTED_AT_STOP:
            # An estimated prompt must not move the reported ceiling.
            usage = None
        applied = planning.applied_max_output_tokens(
            engine=spec.engine,
            requested=int(spec.requested_max_output_tokens or spec.max_tokens),
            planned=spec.planned,
            context_window=spec.context_window,
            reserve=int(spec.context_reserve or 0),
            usage=usage,
            llm_applied=self.llm_applied_max_tokens,
        )
        if self.retried_max_tokens is not None:
            applied = min(int(applied), int(self.retried_max_tokens))
        return applied

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


def _accepted_keywords(fn: Any, names: tuple) -> frozenset:
    """Which of `names` a callable accepts — the feature detection that lets
    this file and llm.py's per-call wall clock land in either order."""
    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return frozenset()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return frozenset(names)
    return frozenset(name for name in names if name in parameters)


def _wall_clock_marker_pending() -> bool:
    try:
        return llm.get_finish_reason() == llm.WALL_CLOCK_FINISH
    except Exception:  # noqa: BLE001
        return False


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
        outcome.max_output_tokens = generation.applied_max_output_tokens()
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        if generation.first_token_at is not None:
            outcome.ttft_ms = int((generation.first_token_at - started) * 1000)
    failure = attempt_failure(generation)
    if failure is not None:
        outcome.status = "failed"
        outcome.error = failure
    return outcome


def _default_guard(spec: GenerationSpec) -> Any:
    from . import liveness

    if spec.engine == registry.ENGINE_MAIN:
        return liveness.MainGuard()
    return liveness.SidecarGuard(None)


def attempt_failure(generation: Any) -> Optional[errors.ApiError]:
    """What a NON-durable caller reports for an ended attempt: the engine
    error, the retry-safe `model_unavailable` for a liveness interrupt, or the
    legacy `timeout` when an older llm's own chat wall clock cut the stream
    (SHIM(T1): impossible once `wall_clock_s=0` is accepted). None: success."""
    if getattr(generation, "error", None) is not None:
        engine = getattr(getattr(generation, "spec", None), "engine", registry.ENGINE_MAIN)
        return engine_error(generation.error, engine)
    if getattr(generation, "interrupt", None) is not None:
        return errors.model_unavailable(retry_after=UNAVAILABLE_RETRY_AFTER)
    if getattr(generation, "finish_reason", None) == llm.WALL_CLOCK_FINISH:
        return errors.timeout(float(getattr(settings, "gen_wall_clock_s", 0) or 0) or None)
    return None


class ClientGone(Exception):
    """The caller of a synchronous request disconnected before its answer
    was ready; the generation was stopped and its outcome filled in place."""


async def _wait_for_disconnect(receive: Callable[[], Awaitable[Mapping[str, Any]]]) -> None:
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            return


async def run_to_completion_watching(
    spec: GenerationSpec,
    *,
    receive: Optional[Callable[[], Awaitable[Mapping[str, Any]]]],
    outcome: StreamOutcome,
    heartbeat_s: Optional[float] = None,
) -> StreamOutcome:
    """`run_to_completion`, stopped the moment the client goes away.

    WHY (adversarial review 2026-09-13). A non-streaming handler is not
    cancelled by the server when its client disconnects, so an abandoned
    synchronous request kept its capacity gate, its admission slot and the
    engine working for nobody until the whole generation ended — measured
    9.4 s after the client left for a 10 s answer, and for a synchronous
    1,000,000-token request (documented unsuitable, not refused) that is the
    single `main.long` gate and a NORMAL admission slot for up to 5 h 48 m,
    while Cloudflare had long since answered the caller 524 at 100 s.

    `receive` is the ASGI receive of a request whose body has already been
    read: its next message is `http.disconnect`, when the client goes (or when
    the response is complete, which cannot happen before this returns). On a
    disconnect the generation is cancelled — its engine stream closed, its
    partial text and usage written into `outcome` by `run_to_completion`'s own
    `finally` — and `ClientGone` is raised for the caller to record. A
    `receive` that cannot be watched (it raises) is ignored, never read as a
    disconnect.
    """
    generation = asyncio.ensure_future(
        run_to_completion(spec, heartbeat_s=heartbeat_s, outcome=outcome)
    )
    watcher = asyncio.ensure_future(_wait_for_disconnect(receive)) if receive is not None else None
    try:
        if watcher is not None:
            done, _pending = await asyncio.wait(
                {generation, watcher}, return_when=asyncio.FIRST_COMPLETED
            )
            if generation not in done and watcher in done and watcher.exception() is None:
                generation.cancel()
                # `wait`, not `await`: the task's CancelledError is its own,
                # and one aimed at THIS task must still propagate.
                await asyncio.wait({generation})
                outcome.status = "cancelled"
                outcome.client_gone = True
                raise ClientGone()
        return await generation
    finally:
        pending = {task for task in (generation, watcher) if task is not None and not task.done()}
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending)


# ------------------------------------------------ the Responses stream --


OnFinish = Callable[[StreamOutcome], Awaitable[None]]


def _wire(
    spec: GenerationSpec,
    status: str,
    *,
    text: str = "",
    usage: Optional[Dict[str, Any]] = None,
    error: Optional[errors.ApiError] = None,
    max_output_tokens: Optional[int] = None,
    finish_reason: Optional[str] = None,
) -> Dict[str, Any]:
    return models.Response(
        id=spec.response_id,
        created_at=spec.created_at,
        status=status,  # type: ignore[arg-type]
        model=spec.model,
        output=[models.OutputMessage.of(text)] if text else [],
        usage=models.Usage.from_llm(usage),
        max_output_tokens=(spec.planned if max_output_tokens is None else max_output_tokens),
        incomplete_details=(
            models.IncompleteDetails.for_finish(finish_reason) if status == "completed" else None
        ),
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


#: What a stream calls with the finished text to get its `file_citation`
#: annotations (Files design §5.6; `file_inputs.FileRun.note_output`). Never
#: expected to raise; an empty list means none.
Annotator = Callable[[str], Sequence[Mapping[str, Any]]]


def annotations_for(annotate: Optional[Annotator], text: str) -> List[Dict[str, Any]]:
    """`annotate(text)`, as a list of dicts; a failure is "no annotations"
    (a citation that cannot be computed is plain text, never a failed
    answer)."""
    if annotate is None:
        return []
    try:
        return [dict(a) for a in (annotate(text) or ())]
    except Exception:  # noqa: BLE001
        log.warning("file citations were not computed for a stream", exc_info=True)
        return []


async def responses_sse(
    spec: GenerationSpec,
    *,
    on_finish: Optional[OnFinish] = None,
    heartbeat_s: Optional[float] = None,
    annotate: Optional[Annotator] = None,
) -> AsyncIterator[str]:
    """The CONTRACT §10.2 lifecycle for one `POST /v1/responses` with `stream: true`.

        response.created → [response.queued] → response.in_progress
          → response.output_item.added → response.content_part.added
          → response.output_text.delta (×N) → response.output_text.done
          → [response.output_text.annotation.added (×K)]
          → response.content_part.done → response.output_item.done
          → response.completed

    with `response.failed` replacing the tail on any engine-side failure.
    `events.SequencedEvents` numbers the frames and refuses a second terminal;
    nothing here emits one from a `finally`, because a generator that is being
    closed cannot yield and a client that has gone is not owed a frame.

    This is the NON-DURABLE stream (`store: false`, or a process whose durable
    runtime is not running); `durable.RecordBuilder` produces the same grammar
    from the write-ahead log. `annotate` adds the Files API's citations.
    """
    emitter = events.SequencedEvents(item_id=spec.item_id)
    generation = Generation(spec, heartbeat_s=heartbeat_s)
    outcome = _outcome_for(spec)
    started = time.monotonic()
    pieces: List[str] = []
    try:
        try:
            yield emitter.created(_wire(spec, "queued"))
            if spec.engine == registry.ENGINE_MAIN and engine_is_recovering():
                # CONTRACT §10: say why nothing is arriving, so the caller
                # does not read a legitimate wait as a dead connection.
                yield emitter.queued(_wire(spec, "queued"))
            yield emitter.in_progress(_wire(spec, "in_progress"))
            async for chunk in generation.stream():
                if chunk.kind != TOKEN_KIND:
                    yield emitter.heartbeat()
                    continue
                if not emitter.item_open:
                    yield emitter.output_item_added()
                    yield emitter.content_part_added()
                pieces.append(chunk.text)
                yield emitter.output_text_delta(chunk.text)
            failure = attempt_failure(generation)
            if failure is not None:
                raise failure
            text = "".join(pieces)
            outcome.text = text
            outcome.usage = generation.usage
            outcome.finish_reason = generation.finish_reason
            outcome.max_output_tokens = generation.applied_max_output_tokens()
            if not emitter.item_open:
                yield emitter.output_item_added()
                yield emitter.content_part_added()
            yield emitter.output_text_done(text)
            annotations = annotations_for(annotate, text)
            for index, annotation in enumerate(annotations):
                yield emitter.annotation_added(index, annotation)
            yield emitter.content_part_done(text, annotations)
            yield emitter.output_item_done(text, annotations)
            yield emitter.completed(
                events.with_annotations(
                    _wire(
                        spec,
                        "completed",
                        text=text,
                        usage=generation.usage,
                        max_output_tokens=outcome.max_output_tokens,
                        finish_reason=generation.finish_reason,
                    ),
                    annotations,
                )
            )
        except Exception as exc:  # noqa: BLE001 - every failure is a frame
            # GeneratorExit and CancelledError are BaseException and do NOT
            # come through here: a client that has gone gets no frame, and its
            # outcome is recorded by the `finally` below.
            failure = engine_error(exc, spec.engine)
            outcome.status = "failed"
            outcome.error = failure
            outcome.text = "".join(pieces)
            outcome.usage = generation.usage
            outcome.max_output_tokens = generation.applied_max_output_tokens()
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
                        max_output_tokens=outcome.max_output_tokens,
                    )
                )
    finally:
        await generation.aclose()
        if not emitter.finished:
            _cut_short(outcome, generation, pieces)
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        if generation.first_token_at is not None:
            outcome.ttft_ms = int((generation.first_token_at - started) * 1000)
        if outcome.usage is None:
            outcome.usage = generation.usage
        if outcome.finish_reason is None:
            outcome.finish_reason = generation.finish_reason
        await _settle(on_finish, outcome)


def _cut_short(outcome: StreamOutcome, generation: "Generation", pieces: List[str]) -> None:
    """The outcome of a non-durable stream closed before its terminal frame:
    the client left, or the process is shutting down.

    WHY (verifier, 2026-09-14, restart-gateway-TERM). The stream was cut after
    193 tokens by an orchestrator restart, and its usage row said `ok` with
    input and output NULL: the outcome still read `completed` (nothing had
    set another status) and vLLM's usage chunk — the stream's last — never
    arrived. A generation stopped before its end is `cancelled` (the caller,
    or the restart, stopped it; the same word the synchronous path and an
    abandoned stream use), and it is charged the tokens this server can stand
    behind: the deltas it received and the input it counted
    (`usage_source: counted_at_stop`)."""
    outcome.client_gone = True
    if outcome.status == "completed":
        outcome.status = "cancelled"
    outcome.text = outcome.text or "".join(pieces)
    if outcome.usage is None and generation.usage is None and (generation.streamed_deltas or pieces):
        with contextlib.suppress(Exception):
            outcome.usage = generation._counted_usage()


# ------------------------------------------ the Chat Completions stream --


async def chat_completions_sse(
    spec: GenerationSpec,
    *,
    completion_id: str,
    include_usage: bool = False,
    on_finish: Optional[OnFinish] = None,
    heartbeat_s: Optional[float] = None,
    annotate: Optional[Annotator] = None,
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
            failure = attempt_failure(generation)
            if failure is not None:
                raise failure
            outcome.text = "".join(pieces)
            outcome.usage = generation.usage
            outcome.finish_reason = generation.finish_reason
            outcome.max_output_tokens = generation.applied_max_output_tokens()
            yield chunks.stop(
                chat_finish_reason(generation.finish_reason),
                max_output_tokens=outcome.max_output_tokens,
                annotations=annotations_for(annotate, outcome.text),
            )
            if include_usage:
                yield chunks.usage_chunk(_completion_usage(generation.usage))
            yield chunks.done()
        except Exception as exc:  # noqa: BLE001
            failure = engine_error(exc, spec.engine)
            outcome.status = "failed"
            outcome.error = failure
            outcome.text = "".join(pieces)
            outcome.usage = generation.usage
            outcome.max_output_tokens = generation.applied_max_output_tokens()
            if not chunks.finished:
                yield chunks.error_chunk(failure)
                yield chunks.done()
    finally:
        await generation.aclose()
        if not chunks.finished:
            _cut_short(outcome, generation, pieces)
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
