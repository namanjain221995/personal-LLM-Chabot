"""Durable, privacy-bounded tracing for one chat generation.

The trace id is the existing ``generation_id``. Persistence is best-effort:
diagnostics must never turn a successful user request into a failed one.
Raw prompts, result rows, answer text, credentials and chain-of-thought do not
belong here. Callers record structured decisions and bounded summaries only.

CORRELATION (2026-10-03, programme task B-03). The trace's ``request_id`` is
the ONE correlation id of the HTTP request that started the generation: the
Next.js proxy mints it (or adopts a well-formed one it was given), sends it as
``X-Request-ID``, logs it, and returns it to the browser; ``POST /chat``
accepts it only in this module's shape (``accept_request_id``) and otherwise
mints its own, then returns it in the same header and the first ``meta``
event. The shape is the one the persisted evaluation contract already pins
(``evaluation/schemas/trace.schema.json``: ``^req_[A-Fa-f0-9]{32}$``), so an
adopted id is indistinguishable from a minted one downstream, and nothing a
client sends reaches a log line or a row unless it is exactly that shape.

STAGE TIMES ARE TAKEN WHEN THE STAGE HAPPENS. main.py's recorder writes
behind the answer, in group commits, so a row's write time can trail the
stage by the coalescing window or by a queued event that waits for its
details. Every event therefore carries its call-time clock (``at``,
perf_counter) and the row's ``started_at``/``completed_at`` are derived from
it on one monotonic base: ``completed_at - query_traces.started_at`` is the
stage's offset into the request, in order.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any, Dict, Optional

from .. import db

log = logging.getLogger(__name__)

TRACE_SCHEMA_VERSION = "1.0.0"
PIPELINE_VERSION = "salesforce-eval-trace-v1"

_current: contextvars.ContextVar[Optional["TraceRecorder"]] = contextvars.ContextVar(
    "query_trace_recorder", default=None
)
_SECRET_PARTS = (
    "authorization", "cookie", "password", "passwd", "secret", "token",
    "api_key", "apikey", "private_key", "session",
)
_MAX_DEPTH = 5
_MAX_ITEMS = 60
_MAX_TEXT = 4000

#: The correlation header, request and response (the Next.js proxy sends and
#: returns the same name; HTTP header names are case-insensitive).
REQUEST_ID_HEADER = "X-Request-ID"
#: The only shape an inbound id is adopted in. Anything else — too long, a
#: newline, a quote, a UUID from some other system — is replaced, never
#: trimmed or escaped into shape.
REQUEST_ID_RE = re.compile(r"^req_[0-9A-Fa-f]{32}$")
#: Where `request_id_for` caches the id on a Starlette request's state.
_STATE_KEY = "correlation_request_id"

#: Bounded stage families: a turn can make many model calls (a continuation
#: run, an agent) and many rerank calls (Deep Research). The first N of each
#: are traced; the rest are counted on the trace's final meta.
MAX_TRACED_MODEL_CALLS = 8
MAX_TRACED_RERANKS = 16

#: Background trace writes of the plain recorder (`event_nowait`), held so
#: the loop keeps a strong reference until they finish.
_nowait_tasks: set = set()


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex}"


def accept_request_id(value: Any) -> Optional[str]:
    """`value` when it is a well-formed correlation id, else None."""
    if isinstance(value, str) and REQUEST_ID_RE.fullmatch(value):
        return value
    return None


def request_id_for(request: Any) -> str:
    """The correlation id of one HTTP request: the inbound `X-Request-ID`
    when it is well formed, a new one otherwise. Cached on the request's
    state, so the route and the generation it starts agree on one id."""
    state = getattr(request, "state", None)
    cached = getattr(state, _STATE_KEY, None) if state is not None else None
    if cached:
        return cached
    headers = getattr(request, "headers", None)
    inbound = headers.get(REQUEST_ID_HEADER) if headers is not None else None
    value = accept_request_id(inbound) or new_request_id()
    if state is not None:
        try:
            setattr(state, _STATE_KEY, value)
        except Exception:  # noqa: BLE001 — a read-only state still gets an id
            pass
    return value


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SECRET_PARTS)


def sanitize(value: Any, *, _depth: int = 0) -> Any:
    """Return a JSON-safe, bounded value with credential-like keys redacted."""
    if _depth >= _MAX_DEPTH:
        return "[depth-limit]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= _MAX_TEXT else value[:_MAX_TEXT] + "…[truncated]"
    if hasattr(value, "model_dump"):
        try:
            value = value.model_dump(mode="json")
        except TypeError:
            value = value.model_dump()
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                out["[truncated]"] = f"{len(value) - _MAX_ITEMS} more keys"
                break
            name = str(key)
            # A credential is text. A count under a secret-looking key
            # (`tokens_used`, `prompt_tokens`) is the diagnostic itself, and
            # blanking it would leave the trace unable to say how full the
            # context was.
            if _is_secret_key(name) and not isinstance(item, (bool, int, float)):
                out[name] = "[redacted]"
            else:
                out[name] = sanitize(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        out = [sanitize(item, _depth=_depth + 1) for item in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            out.append(f"[{len(items) - _MAX_ITEMS} more items]")
        return out
    return sanitize(str(value), _depth=_depth + 1)


def text_fingerprint(value: str) -> dict:
    """Identify a large evidence block without persisting its contents."""
    raw = (value or "").encode("utf-8", errors="replace")
    return {"characters": len(value or ""), "sha256": hashlib.sha256(raw).hexdigest()}


class TraceRecorder:
    def __init__(
        self,
        trace_id: str,
        *,
        request_id: str = "",
        test_case_id: Optional[str] = None,
        versions: Optional[dict] = None,
    ) -> None:
        self.trace_id = trace_id
        # Validated even from in-process callers: the id is a unique key of
        # query_traces and the evaluation contract pins its shape.
        self.request_id = accept_request_id(request_id) or new_request_id()
        self.test_case_id = test_case_id
        self._sequence = 0
        # One clock pair: every stage time is `_started_wall` plus a
        # perf_counter offset, so stages order exactly as they happened even
        # when the wall clock steps.
        self._started = perf_counter()
        self._started_wall = datetime.now(timezone.utc)
        self._finished = False
        # Bounded families (MAX_TRACED_*): how many were seen this turn.
        self._seen: Dict[str, int] = {}
        self._last_stage = ""
        self.selected_route = ""
        self.resolved_mode = ""
        self.versions = {
            "trace_schema": TRACE_SCHEMA_VERSION,
            "pipeline": PIPELINE_VERSION,
            "prompt": "unversioned",
            "metadata_index": "runtime",
            **(versions or {}),
        }

    async def _persist(self, fn, *args: Any, **kwargs: Any) -> None:
        """Write one trace row. A subclass may buffer instead (main.py's
        queued recorder writes a whole burst in one transaction)."""
        await db.run_in_thread(fn, *args, **kwargs)

    def _wall(self, at: Optional[float]) -> datetime:
        """The wall-clock time of a perf_counter reading taken in this turn."""
        offset = (perf_counter() if at is None else at) - self._started
        return self._started_wall + timedelta(seconds=max(0.0, offset))

    def admit(self, family: str, limit: int) -> Optional[int]:
        """Count one member of a bounded family; its 1-based index while it
        is within `limit` (trace it), None past it (count only)."""
        seen = self._seen.get(family, 0) + 1
        self._seen[family] = seen
        return seen if seen <= limit else None

    def event_nowait(self, stage: str, **kwargs: Any) -> None:
        """`event` for a synchronous caller (a dispatch hook, a hot loop).
        The stage time is taken now; the write happens on its own task."""
        kwargs.setdefault("at", perf_counter())
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no running loop: a trace never fails a caller
            return
        task = loop.create_task(self.event(stage, **kwargs))
        _nowait_tasks.add(task)
        task.add_done_callback(_nowait_tasks.discard)

    def activate(self) -> contextvars.Token:
        return _current.set(self)

    @staticmethod
    def deactivate(token: contextvars.Token) -> None:
        _current.reset(token)

    async def start(
        self,
        *,
        conversation_id: str,
        user_id: Optional[int],
        workspace_id: str,
        question: str,
        requested_mode: str,
    ) -> None:
        try:
            await self._persist(
                db.start_query_trace,
                self.trace_id,
                conversation_id,
                user_id,
                workspace_id,
                question,
                requested_mode,
                request_id=self.request_id,
                test_case_id=self.test_case_id,
                versions=sanitize(self.versions),
                started_at=self._started_wall,
            )
        except Exception:
            log.warning("query trace root could not be persisted trace_id=%s", self.trace_id, exc_info=True)

    async def event(
        self,
        stage: str,
        *,
        status: str = "success",
        component: str = "",
        details: Optional[dict] = None,
        duration_ms: Optional[int] = None,
        error: Optional[BaseException] = None,
        component_version: str = PIPELINE_VERSION,
        at: Optional[float] = None,
    ) -> None:
        """`at`: the perf_counter reading when the stage happened (default:
        now). `duration_ms`, when given, is the time the stage took up to
        `at`, so the row's started_at is `at - duration_ms`."""
        if self._finished:
            return
        occurred_at = self._wall(at)
        self._sequence += 1
        self._last_stage = stage
        safe = sanitize(details or {})
        error_type = type(error).__name__ if error else ""
        error_message = sanitize(str(error)) if error else ""
        # JSON in application logs is intentionally only an envelope. The
        # diagnostic detail lives in PostgreSQL with its normal access controls.
        log.info(
            "query_trace %s",
            json.dumps(
                {
                    "trace_id": self.trace_id,
                    "request_id": self.request_id,
                    "test_case_id": self.test_case_id,
                    "sequence": self._sequence,
                    "stage": stage,
                    "status": status,
                    "component": component,
                    "duration_ms": duration_ms,
                    "t_ms": round((occurred_at - self._started_wall).total_seconds() * 1000),
                },
                separators=(",", ":"),
            ),
        )
        try:
            await self._persist(
                db.append_query_trace_event,
                self.trace_id,
                self._sequence,
                stage,
                status,
                component,
                safe,
                duration_ms,
                error_type,
                error_message,
                component_version,
                occurred_at=occurred_at,
            )
        except Exception:
            log.warning("query trace event could not be persisted trace_id=%s stage=%s", self.trace_id, stage, exc_info=True)

    async def finish(
        self,
        status: str,
        *,
        route: str = "",
        resolved_mode: str = "",
        error: Optional[BaseException] = None,
        meta: Optional[dict] = None,
        at: Optional[float] = None,
    ) -> None:
        if self._finished:
            return
        self._finished = True
        self.selected_route = route or self.selected_route
        self.resolved_mode = resolved_mode or self.resolved_mode
        ended = perf_counter() if at is None else at
        meta = dict(meta or {})
        if self._seen:
            # How many of each bounded family the turn made; the trace holds
            # the first MAX_TRACED_* of them.
            meta.setdefault("stage_counts", dict(self._seen))
        try:
            await self._persist(
                db.finish_query_trace,
                self.trace_id,
                status,
                max(0, round((ended - self._started) * 1000)),
                resolved_mode=self.resolved_mode,
                selected_route=self.selected_route,
                error_stage=self._last_stage if error else "",
                error_type=type(error).__name__ if error else "",
                error_message=sanitize(str(error)) if error else "",
                meta=sanitize(meta),
                completed_at=self._wall(ended),
            )
        except Exception:
            log.warning("query trace could not be finalized trace_id=%s", self.trace_id, exc_info=True)


async def event(stage: str, **kwargs: Any) -> None:
    """Record on the current generation, or do nothing outside a request."""
    recorder = _current.get()
    if recorder is not None:
        await recorder.event(stage, **kwargs)


def event_nowait(stage: str, **kwargs: Any) -> None:
    """`event` without awaiting: the stage time is taken now. A no-op
    outside a request, and it never raises into the caller."""
    recorder = _current.get()
    if recorder is None:
        return
    try:
        recorder.event_nowait(stage, **kwargs)
    except Exception:  # noqa: BLE001 — diagnostics never fail the caller
        log.debug("query trace event_nowait failed stage=%s", stage, exc_info=True)


def current() -> Optional[TraceRecorder]:
    return _current.get()


def admit(family: str, limit: int) -> Optional[int]:
    """`TraceRecorder.admit` on the current generation; None outside one."""
    recorder = _current.get()
    return recorder.admit(family, limit) if recorder is not None else None


def _ms(seconds: Optional[float]) -> Optional[int]:
    return None if seconds is None else max(0, round(seconds * 1000))


class ModelCallClock:
    """The §11 model stages of ONE streamed main-model call, on the current
    trace: prompt prepared → dispatched (past the breaker and the admission
    lane) → first engine chunk (prefill done, as the orchestrator sees it) →
    stream ended (generation). Each stage is recorded when it happens, with
    the time it took since the previous one as `duration_ms`.

    Built only by `model_call()`, which returns None outside a traced turn
    and past MAX_TRACED_MODEL_CALLS, so a caller does nothing at all then.
    Every method is synchronous and never raises: it runs on the token path.
    """

    __slots__ = ("_rec", "call", "_details", "started", "prepared_at", "first_dispatch_at",
                 "last_dispatch_at", "dispatches", "first_chunk_at", "first_content_at",
                 "first_reasoning_at", "ended_at")

    def __init__(self, recorder: TraceRecorder, call: int, details: dict) -> None:
        self._rec = recorder
        self.call = call
        self._details = details
        self.started = perf_counter()
        self.prepared_at: Optional[float] = None
        self.first_dispatch_at: Optional[float] = None
        self.last_dispatch_at: Optional[float] = None
        self.dispatches = 0
        self.first_chunk_at: Optional[float] = None
        self.first_content_at: Optional[float] = None
        self.first_reasoning_at: Optional[float] = None
        self.ended_at: Optional[float] = None

    def _record(self, stage: str, at: float, duration_s: Optional[float], **details: Any) -> None:
        try:
            self._rec.event_nowait(
                stage,
                component="orchestrator.app.llm.stream_chat_events",
                details={"call": self.call, **self._details, **details},
                duration_ms=_ms(duration_s),
                at=at,
            )
        except Exception:  # noqa: BLE001 — diagnostics never fail the stream
            log.debug("model call trace %s failed", stage, exc_info=True)

    def prepared(self, **details: Any) -> None:
        """Messages shaped and sized; the call is about to be sent."""
        if self.prepared_at is not None:
            return
        self.prepared_at = perf_counter()
        self._record("MODEL_PROMPT_PREPARED", self.prepared_at, self.prepared_at - self.started, **details)

    def dispatched(self) -> None:
        """Past the breaker and the admission lane, written to the engine.
        Once per attempt; the stage is recorded at the first, and the prefill
        is measured from the last (the attempt that served)."""
        now = perf_counter()
        self.dispatches += 1
        self.last_dispatch_at = now
        if self.first_dispatch_at is not None:
            return
        self.first_dispatch_at = now
        base = self.prepared_at if self.prepared_at is not None else self.started
        self._record("MODEL_DISPATCHED", now, now - base)

    def first_chunk(self) -> None:
        """The engine's first chunk: the prompt is prefilled (as the
        orchestrator sees it — engine queue, prefill and network)."""
        if self.first_chunk_at is not None:
            return
        now = perf_counter()
        self.first_chunk_at = now
        base = self.last_dispatch_at if self.last_dispatch_at is not None else self.started
        self._record("MODEL_FIRST_CHUNK", now, now - base, dispatches=self.dispatches)

    def delta(self, kind: str) -> None:
        """A text delta: "reasoning" or "token" (answer content). Only the
        first of each kind is kept; it rides MODEL_STREAM_ENDED."""
        if kind == "token":
            if self.first_content_at is None:
                self.first_content_at = perf_counter()
        elif self.first_reasoning_at is None:
            self.first_reasoning_at = perf_counter()

    def ended(self, outcome: str, **details: Any) -> None:
        """The call is over: `outcome` is completed, cancelled or error
        (`finish_reason` in the details tells a wall-clock stop apart)."""
        if self.ended_at is not None:
            return
        now = perf_counter()
        self.ended_at = now
        base = self.first_chunk_at if self.first_chunk_at is not None else self.started

        def since_chunk(at: Optional[float]) -> Optional[int]:
            return None if at is None or self.first_chunk_at is None else _ms(at - self.first_chunk_at)

        self._record(
            "MODEL_STREAM_ENDED",
            now,
            now - base,
            outcome=outcome,
            first_content_after_first_chunk_ms=since_chunk(self.first_content_at),
            first_reasoning_after_first_chunk_ms=since_chunk(self.first_reasoning_at),
            **details,
        )


def model_call(**details: Any) -> Optional[ModelCallClock]:
    """A stage clock for one streamed main-model call on the current trace,
    or None (no trace, or past MAX_TRACED_MODEL_CALLS)."""
    recorder = _current.get()
    if recorder is None:
        return None
    try:
        call = recorder.admit("model_call", MAX_TRACED_MODEL_CALLS)
        if call is None:
            return None
        return ModelCallClock(recorder, call, sanitize(details))
    except Exception:  # noqa: BLE001 — diagnostics never fail the caller
        return None
