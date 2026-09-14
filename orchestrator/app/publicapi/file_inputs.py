"""Files as model input on `/v1/responses` and `/v1/chat/completions` — the
router's one seam into `apifiles.service` (files-hookup, 2026-09-13).

`apifiles/service.py` owns WHAT a file part becomes (lift, resolve inside the
caller's project, readiness, context, splice, citations). This module owns
WHEN each of those happens relative to the router's own order (CONTRACT §4,
Files design §5.2), so `router.py` gains a handful of calls instead of a
second copy of the flow:

    parse:     lift(payload)                    file parts out of the raw body,
                                                before the one validator runs
    plan:      pending_inputs(lifted)           the pre-admission plan leaves
                                                file-dependent image rules for later
    generate:  FileRun.authorize                403 `files.read` before any 404
               FileRun.prepare                  resolve → wait → build context
               replan(await FileRun.planning_inputs())
                                                planning with the spliced
                                                messages, the file counts and the
                                                engine's exact count of the file text
               FileRun.note_output / response_wire / chat_body
                                                `file_citation` annotations
               FileRun.wrap_finish(on_finish)   usage meta, inline bytes removed
                                                at the terminal state

NO WAIT ON A HEALTHY REQUEST ENDS ON A CLOCK (no-timeout design, capacity_waits;
senior fix 2026-09-14). Once a request is past the point where a refusal can
still be a real status line, every wait it makes is patient:

* readiness — no deadline; one SHARED poller per process (`SharedReadiness`)
  reads the rows of every waiting request in one query per flight, on its own
  thread, so ten thousand waiters cost ten queries a second and never queue in
  front of the chat app's `asyncio.to_thread` work (token counting);
* the generation's capacity gate — the router's own patient gate (passed in as
  `gate`), or `patient_gate` here, which is the same rule;
* the engines the context build calls — the embed and rerank gates, the OCR
  gate of an inline PDF, and `input_audio` transcription — through
  `patient_engines()`: `capacity.hold(wait_s=None)` when the capacity module
  has it, otherwise a re-queue loop, and whisper through T4's
  `audio_jobs.WhisperDispatcher` (liveness from the replica's /health and a
  silence rule, never a 240 s read timeout).

READINESS PER DELIVERY, AS WIRED.

* sync, bounded — only where the router answers BEFORE any byte (a build
  without the committed JSON response): the Files design's ONE deadline
  (PUBLIC_API_FILES_SYNC_PREPARE_BUDGET_S, 45 s), then `409 file_not_ready`.
* sync, `no_deadline=True` — inside the committed JSON response, which writes
  bytes while it waits: the same patient path as a stream.
* stream — the resolve check (404 / failed file 400) before the status line;
  the wait, the context build, the second plan and the gate inside the stream,
  with `: file file-… transcript 40%` comments on every progress change and a
  comment at least every 15 s. A refusal after the headers is a
  `response.failed` event (Chat: the error chunk and `[DONE]`).
* background — `start_background`: the row is written and the 202 returned AT
  ONCE; the wait, the build and the second plan run in a detached task while the
  row is still `queued`, then the job starts through the router's own
  `background.start`. A cancel while it waits stops it; nothing ran, nothing is
  charged. RESIDUAL (stated): a deploy during that wait fails the row with the
  retry-safe "service restarted" code — the file wait is in memory, not in the
  durable run log, which holds only launched generations.

ONE LAUNCH PATH. The generation itself is started by a function the ROUTER
passes in (`launch` for streams; the router's own synchronous work for sync;
`background.start` for background), so the day the router launches through the
durable runtime, requests with files do too — there is no second copy of the
launch here.

ANNOTATIONS. Synchronous Responses carry `file_citation` annotations on the
`output_text` part and Chat Completions on `choices[0].message.annotations`.
Streams and background rows record the citation counts in usage meta but do
not yet carry the annotation events: `response.output_text.annotation.added` is
not in `publicapi/events.EVENT_NAMES` (the stream grammar's closed set, owned
by the wire team), and `citations.splice_annotation_events` is ready for it.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import dataclasses
import logging
import secrets
import time
import weakref
from datetime import datetime, timezone
from typing import (
    Any,
    AsyncContextManager,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

import httpx

from .. import admission
from . import capacity, errors, events, planning, registry, streaming

log = logging.getLogger(__name__)

#: The deliveries `FileRun.prepare` distinguishes (apifiles.service's names).
DELIVERY_SYNC = "sync"
DELIVERY_STREAM = "stream"
DELIVERY_BACKGROUND = "background"

#: SSE comment cadence while a stream waits for files or for its gate: the
#: SSE + timeout invariant every other `/v1` stream keeps (15 s).
HEARTBEAT_S = 15.0


#: The least time between two readiness flights (`SharedReadiness`). Ten
#: flights a second whatever the number of waiters; a request arriving while
#: the poller is idle is read at once.
READINESS_FLIGHT_SPACING_S = 0.1


def _service() -> Any:
    # Imported per call: `apifiles` is a large package that is new this wave,
    # and a request with no file parts must never pay for — or fail on — it.
    from ..apifiles import service

    return service


# --------------------------------------------------------------- parsing --


def lift(payload: Any, endpoint: str) -> Any:
    """`service.lift_file_parts` for this endpoint's dialect. A body with no
    file parts (and no `file_context`) comes back with `payload` untouched,
    so the rest of the parse is exactly what it was. Raises the part's 400.

    CPU linear in the number of parts: the router calls it OFF the event loop
    for a large body, in the same thread call as the validator."""
    service = _service()
    dialect = service.DIALECT_CHAT if endpoint == registry.ENDPOINT_CHAT_COMPLETIONS else service.DIALECT_RESPONSES
    return service.lift_file_parts(payload, dialect=dialect)


def has_files(lifted: Any) -> bool:
    return bool(lifted is not None and getattr(lifted, "has_files", False))


def pending_inputs(lifted: Any) -> Optional[planning.FileInputs]:
    """The `files=` argument of the plan made before admission."""
    return planning.FILES_PENDING if has_files(lifted) else None


# ------------------------------------------------------- patient capacity --


@contextlib.asynccontextmanager
async def patient_hold(
    engine: str,
    *,
    weight_tokens: int = 0,
    yield_to_chat: bool = False,
    abandon: Optional[asyncio.Event] = None,
) -> AsyncIterator[None]:
    """One unit of `engine`'s public capacity, waited on WITHOUT a deadline.

    WHY (senior fix 2026-09-14, review finding "streams with files can still
    fail on a clock"). The first hookup took the gate of a file stream with
    PUBLIC_API_BACKGROUND_GATE_WAIT_S: after an hour in the one-at-a-time
    `main.long` queue the stream ended `model_unavailable`, and a retry joined
    the back of the line. The no-timeout rule is that only a caller passing a
    finite wait ever gets a capacity refusal, and no `/v1` caller does.

    T2's `hold(wait_s=None)`: one FIFO wait. Ends on admission, on
    cancellation (the waiter's place is given back), or on `abandon`
    (`capacity.Abandoned`). Assembler, 2026-09-14: the pre-T2 re-queue shim
    is gone with the capacity.py that needed it."""
    kwargs: Dict[str, Any] = {"weight_tokens": max(0, int(weight_tokens or 0)), "yield_to_chat": bool(yield_to_chat)}
    if abandon is not None:
        kwargs["abandon"] = abandon
    async with capacity.hold(engine, wait_s=None, **kwargs):
        yield


def patient_gate(plan: planning.GenerationPlan) -> AsyncContextManager[None]:
    """The plan's capacity gate with no deadline; a no-op when its engine
    needs none (NORMAL-lane techsara-35b work waits in the admission lanes
    inside `llm.stream_chat_events`). The router passes its own
    `_patient_gate` where it has one, so there is one rule per build."""
    if not plan.gate_engine:
        return contextlib.nullcontext()
    return patient_hold(
        plan.gate_engine, weight_tokens=plan.gate_weight_tokens, yield_to_chat=plan.yield_to_chat
    )


# ------------------------------------------------------- patient engines --


def _patient_embedder() -> Any:
    """`vectors.make_engine_query_embedder` with the embed gate waited on
    patiently. None — never an exception — on an engine failure: retrieval is
    an upgrade, and the build falls back to lexical ranking."""

    async def embed_query(question: str) -> Optional[Sequence[float]]:
        from ..apifiles import vectors
        from . import sidecars

        text = vectors.query_text(question)
        try:
            async with patient_hold(capacity.GATE_EMBED, weight_tokens=min(4096, len(text.encode("utf-8")) + 2)):
                found, _tokens = await sidecars._embed_call([text])
        except errors.ApiError:
            log.info("file retrieval: embed refused; falling back to lexical ranking")
            return None
        except Exception:  # noqa: BLE001
            log.warning("file retrieval: embed engine failed; falling back to lexical ranking", exc_info=True)
            return None
        return found[0] if found else None

    return embed_query


def _patient_reranker() -> Any:
    """`retrieval.make_engine_reranker` with the rerank gate waited on
    patiently: the same template, packing and score call as
    `sidecars.rerank_scores`, whose own total wait is PUBLIC_API_GATE_WAIT_S.
    None on an engine failure or a refused input (lexical ranking stays)."""

    async def rerank(question: str, documents: Sequence[str]) -> Optional[Sequence[float]]:
        from ..apifiles import retrieval
        from . import engines as engine_targets, sidecars

        if engine_targets.target(registry.ENGINE_RERANK) is None:
            # Not deployed here: lexical order, quietly (as the bounded
            # reranker's SidecarError path does).
            return None
        query_text = sidecars.rerank_query_text(question, retrieval.RERANK_INSTRUCTION)
        doc_texts = [sidecars.rerank_document_text(text) for text in documents]
        window = sidecars.rerank_context_tokens()
        query_bytes = sidecars._utf8_bytes(query_text)
        weights = [min(window, query_bytes + sidecars._utf8_bytes(text)) for text in doc_texts]
        scores: List[Optional[float]] = [None] * len(doc_texts)
        client = await sidecars._http_client(sidecars.RERANK_READ_TIMEOUT_S)
        try:
            for batch in sidecars.pack(
                weights, max_items=sidecars.RERANK_CALL_MAX_PAIRS, budget=sidecars.kv_budget_tokens(sidecars.RERANK)
            ):
                async with patient_hold(capacity.GATE_RERANK, weight_tokens=sum(weights[i] for i in batch)):
                    got, _tokens = await sidecars._score_call(client, query_text, [doc_texts[i] for i in batch])
                for index, value in zip(batch, got):
                    scores[index] = value
        except (httpx.HTTPError, sidecars.EngineRefusedInput) as failure:
            log.info("file retrieval: rerank unavailable (%s); keeping the lexical order", type(failure).__name__)
            return None
        except Exception:  # noqa: BLE001 - lexical ranking is the fallback
            log.warning("file retrieval: rerank failed; keeping the lexical order", exc_info=True)
            return None
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()
        if any(value is None for value in scores):
            return None
        return [float(value) for value in scores]  # type: ignore[arg-type]

    return rerank


_DISPATCHER_CLASS: Any = None


def _whisper_dispatcher_class() -> Any:
    global _DISPATCHER_CLASS
    if _DISPATCHER_CLASS is not None:
        return _DISPATCHER_CLASS
    from . import audio_jobs

    class InputAudioDispatcher(audio_jobs.WhisperDispatcher):
        """T4's one-window dispatcher for an `input_audio` clip: the clip's own
        container and content type (WAV or MP3; the engine decodes with
        ffmpeg), the engine's default silence check (the chat dialect sends a
        whole clip, not a voice-activity window), and `verbose_json` for the
        measured duration."""

        def __init__(self, content_type: str, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.content_type = content_type if content_type in ("audio/wav", "audio/mpeg") else "audio/wav"

        def _multipart(self, clip: bytes, language: Optional[str], index: int) -> Tuple[bytes, str]:  # type: ignore[override]
            from ..config import settings

            boundary = secrets.token_hex(16)
            fields = [
                ("model", str(getattr(settings, "asr_model", "") or "whisper")),
                ("response_format", "verbose_json"),
            ]
            if language:
                fields.append(("language", language))
            head = bytearray()
            for name, value in fields:
                head += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
            extension = "mp3" if self.content_type == "audio/mpeg" else "wav"
            head += (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"input_audio.{extension}\"\r\n"
                f"Content-Type: {self.content_type}\r\n\r\n"
            ).encode()
            return bytes(head) + clip + f"\r\n--{boundary}--\r\n".encode(), boundary

    _DISPATCHER_CLASS = InputAudioDispatcher
    return InputAudioDispatcher


def patient_transcriber(*, dispatcher_factory: Optional[Callable[[str], Any]] = None) -> Any:
    """An `inline.Transcriber` with no clock on a healthy request (review
    finding "chat input_audio still has timers"): `sidecars.transcribe` waited
    PUBLIC_API_GATE_WAIT_S (30 s) for the fleet-wide `asr` gate and 240 s for
    the reply. Whisper decodes one clip at a time and yields to dictation, so
    two public clips at once were enough to fail the second.

    This goes through T4's `WhisperDispatcher`: the patient `asr` gate
    (`audio_jobs.capacity_gate`), the replica's /health while the clip is out,
    fail-over, and PUBLIC_API_ASR_WINDOW_SILENCE_S of silence on a READY
    replica before it is sent once more — liveness, not a deadline.

    An MP3 whose container says it is longer than the clip limit is not sent
    at all: its probed length comes back as the measured duration, and
    `inline.transcribe_decoded` refuses it with the part's own 400."""

    async def transcribe(raw: bytes, content_type: str) -> Tuple[str, Optional[float]]:
        from ..apifiles import inline as inline_files
        from . import audio_jobs, sidecars

        limit = float(inline_files.audio_max_seconds())
        probed = await sidecars.probe_seconds(bytearray(raw))
        if probed is not None and probed > limit + 0.5:
            return "", float(probed)
        factory = dispatcher_factory or _whisper_dispatcher_class()
        dispatcher = factory(content_type)

        async def queued(_data: Dict[str, Any]) -> None:
            return None

        try:
            reply = await dispatcher.transcribe(bytes(raw), language=None, index=0, on_wait=queued)
        except audio_jobs.EngineFailure as failure:
            raise failure.error from None
        finally:
            with contextlib.suppress(Exception):
                await dispatcher.aclose()
        duration = reply.get("duration") if isinstance(reply, Mapping) else None
        seconds = float(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None
        return str((reply or {}).get("text") or ""), seconds

    return transcribe


def patient_ocr_gate() -> AsyncContextManager[None]:
    """The OCR gate of an inline PDF's thin pages, patient, yielding to chat
    (`ocr_pages.default_gate` waits PUBLIC_API_BACKGROUND_GATE_WAIT_S, then
    the inline file is refused `model_unavailable`)."""
    return patient_hold(capacity.GATE_OCR, yield_to_chat=True)


def patient_engines() -> Any:
    """`service.Engines.production` with every engine wait patient: embed,
    rerank, the OCR gate of an inline PDF, and `input_audio`. Built from
    `production` so an engine seam the Files core adds later is inherited."""
    service = _service()
    from ..apifiles import inline as inline_files

    base = service.Engines.production(gate_wait_s=capacity.sync_wait_s())
    return dataclasses.replace(
        base,
        embed_query=_patient_embedder(),
        rerank=_patient_reranker(),
        extractor=inline_files.make_subprocess_extractor(ocr_gate=patient_ocr_gate),
        transcriber=patient_transcriber(),
    )


def bounded_engines() -> Any:
    """The engines of a request with a pre-header deadline (service.py holds
    each to that deadline)."""
    return _service().Engines.production(gate_wait_s=capacity.sync_wait_s())


# ------------------------------------------------------ shared readiness --


_READINESS_EXECUTOR: Optional[concurrent.futures.ThreadPoolExecutor] = None


def _readiness_executor() -> concurrent.futures.ThreadPoolExecutor:
    """ONE thread for every readiness query in the process — never the
    default executor the chat app's token counting shares."""
    global _READINESS_EXECUTOR
    if _READINESS_EXECUTOR is None:
        _READINESS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="files-readiness"
        )
    return _READINESS_EXECUTOR


_Waiting = Tuple[str, Tuple[str, ...], "asyncio.Future[Dict[str, Any]]"]


class SharedReadiness:
    """A `service.FileStore` that coalesces every waiting request's lookups.

    WHY (senior fix 2026-09-14, review finding "each request waiting for a file
    polls Postgres every second on the default thread pool"). Measured on the
    first hookup against a real Postgres: 12,000 waiters made 1,576 queries a
    second and pushed an `asyncio.to_thread` no-op — the queue chat's
    `/tokenize` JSON parsing waits in — to a 6.6 s median, because every
    waiter's `SqlFileStore.get_files` took a default-executor thread.

    Now a waiter's `get_files` joins the next FLIGHT: one query per project in
    the flight, run on one dedicated thread (`_readiness_executor`), flights at
    least READINESS_FLIGHT_SPACING_S apart. A call made while the poller is
    idle flies at once. Each waiter keeps its own `wait_until_ready` loop
    (progress comments, abandon, poll interval); only the database read is
    shared. The store is resolved per flight (`service.SqlFileStore()`), so a
    store a test installs is honoured; one without the SQL seam is called
    through its own `get_files`.
    """

    def __init__(self, *, spacing_s: Optional[float] = None) -> None:
        self._spacing_s = spacing_s
        self._pending: List[_Waiting] = []
        self._runner: Optional["asyncio.Task[None]"] = None
        #: For the tests and a health payload: flights flown, queries made.
        self.flights = 0
        self.queries = 0

    def _spacing(self) -> float:
        return READINESS_FLIGHT_SPACING_S if self._spacing_s is None else max(0.0, float(self._spacing_s))

    async def get_files(self, project_id: str, file_ids: Sequence[str]) -> Dict[str, Any]:
        if not file_ids:
            return {}
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[Dict[str, Any]]" = loop.create_future()
        self._pending.append((str(project_id), tuple(dict.fromkeys(str(i) for i in file_ids)), future))
        if self._runner is None or self._runner.done():
            self._runner = loop.create_task(self._fly(), name="files-readiness")
        return await future

    async def _fly(self) -> None:
        try:
            while self._pending:
                batch, self._pending = self._pending, []
                live = [waiting for waiting in batch if not waiting[2].done()]
                if live:
                    self.flights += 1
                    await self._answer(live)
                await asyncio.sleep(self._spacing())
        except BaseException:
            # A poller torn down mid-flight (its loop closing) leaves no waiter
            # hanging: each is cancelled with it.
            for _owner, _ids, future in self._pending:
                if not future.done():
                    future.cancel()
            self._pending = []
            raise

    async def _answer(self, live: List[_Waiting]) -> None:
        wanted: Dict[str, Dict[str, None]] = {}
        for project_id, ids, _future in live:
            wanted.setdefault(project_id, {}).update(dict.fromkeys(ids))
        try:
            store = _service().SqlFileStore()
        except Exception as exc:  # noqa: BLE001 - each waiter sees the failure
            for _owner, _ids, future in live:
                if not future.done():
                    future.set_exception(exc)
            return
        for project_id, ids in wanted.items():
            try:
                found = await self._fetch(store, project_id, list(ids))
            except asyncio.CancelledError:
                for _owner, _ids, future in live:
                    if not future.done():
                        future.cancel()
                raise
            except Exception as exc:  # noqa: BLE001 - each waiter sees the failure
                for owner, _ids, future in live:
                    if owner == project_id and not future.done():
                        future.set_exception(exc)
                continue
            for owner, own_ids, future in live:
                if owner == project_id and not future.done():
                    future.set_result({i: found[i] for i in own_ids if i in found})

    async def _fetch(self, store: Any, project_id: str, file_ids: List[str]) -> Dict[str, Any]:
        self.queries += 1
        query = getattr(store, "_query", None)
        if not callable(query):
            return dict(await store.get_files(project_id, file_ids))
        service = _service()
        rows = await asyncio.get_running_loop().run_in_executor(_readiness_executor(), query, project_id, file_ids)
        now = datetime.now(timezone.utc)
        out: Dict[str, Any] = {}
        for row in rows:
            record = service.record_from_row(row, now=now)
            if record is not None and record.project_id == project_id:
                out[record.file_id] = record
        return out


_SHARED: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, SharedReadiness]" = weakref.WeakKeyDictionary()


def shared_readiness() -> SharedReadiness:
    """This event loop's shared readiness poller."""
    loop = asyncio.get_running_loop()
    found = _SHARED.get(loop)
    if found is None:
        found = SharedReadiness()
        _SHARED[loop] = found
    return found


# ------------------------------------------------------------------ the run --


class FileRun:
    """One generating request's files, from authorization to the terminal state."""

    def __init__(
        self,
        lifted: Any,
        *,
        caller: Any,
        plan: planning.GenerationPlan,
        request_id: str,
        caller_messages: List[Dict[str, Any]],
    ) -> None:
        self.lifted = lifted
        self.caller = caller
        self.plan = plan
        self.request_id = request_id
        #: `request_model.chat_messages()` of the LIFTED body, without
        #: planning's OCR prompt: `splice_messages` adds that itself when the
        #: caller typed no text (the lift's placeholder would hide the absence).
        self.caller_messages = [dict(message) for message in caller_messages]
        self.prepared: Any = None
        self.annotated: Any = None
        self._cleaned = False

    @classmethod
    def for_request(
        cls,
        lifted: Any,
        *,
        caller: Any,
        plan: Optional[planning.GenerationPlan],
        request_id: str,
        request_model: Any,
    ) -> Optional["FileRun"]:
        if not has_files(lifted) or plan is None or request_model is None:
            return None
        return cls(
            lifted,
            caller=caller,
            plan=plan,
            request_id=request_id,
            caller_messages=request_model.chat_messages(),
        )

    # -- 1. the scope, before any id is looked up --------------------------

    async def authorize(
        self, request: Any, authorize_scope: Callable[[Any, Any, Any], Awaitable[None]]
    ) -> None:
        """`files.read` when any part names a `file_id` (Files design §2.1: a
        model can be asked to repeat a file, so "may use" means "may read").
        Checked BEFORE resolution so a key without it learns nothing about
        which ids exist: 403, never a 404 first. Inline `file_data` and
        `input_audio` need no file scope — the caller already holds the bytes."""
        wanted = _service().required_scope(self.lifted)
        if not wanted:
            return
        from ..apiplatform.scopes import Scope, requires

        await authorize_scope(request, self.caller, requires(Scope(wanted)))

    # -- 2. resolve, wait, build --------------------------------------------

    def _caps(self) -> Any:
        from ..apifiles import context as file_context

        return file_context.ModelCaps.from_public_model(
            self.plan.model, planned_output_tokens=self.plan.planned_max_output_tokens
        )

    async def precheck(self, *, store: Any = None) -> None:
        """The refusals that need no wait, for a stream BEFORE its status line:
        an id this project does not have (404) or a file that failed (400).
        One lookup through the shared poller; a file still processing is fine."""
        service = _service()
        if not self.lifted.file_ids:
            return
        try:
            await service.wait_until_ready(
                store or shared_readiness(),
                self.caller.project_id,
                self.lifted,
                delivery=service.DELIVERY_SYNC,
                sync_wait_s=0.0,
            )
        except errors.ApiError as refusal:
            if refusal.code != "file_not_ready":
                raise

    async def prepare(
        self,
        delivery: str,
        *,
        on_progress: Optional[Callable[[str], Awaitable[None]]] = None,
        abandon: Optional[asyncio.Event] = None,
        store: Any = None,
        engines: Any = None,
        no_deadline: bool = False,
    ) -> Any:
        """`service.prepare` for this request.

        Bounded ONLY for a synchronous request without `no_deadline` — the
        build that answers before any byte. Everything else (a stream, a
        background job, a synchronous request inside the committed JSON
        response) has no deadline, patient engines, and the lenient inline OCR
        rule: `service`'s stream delivery. Before 2026-09-14 background was
        mapped to SYNC, so an inline PDF needing more than 8 OCR pages was
        refused on a background request with a sentence telling the caller to
        use background (review finding)."""
        service = _service()
        bounded = delivery == DELIVERY_SYNC and not no_deadline
        self.prepared = await service.prepare(
            self.lifted,
            project_id=self.caller.project_id,
            store=store or shared_readiness(),
            caps=self._caps(),
            delivery=(service.DELIVERY_SYNC if bounded else service.DELIVERY_STREAM),
            sync_wait_s=(service.USE_SETTING if bounded else service.NO_DEADLINE),
            request_id=self.request_id or "req_files",
            engines=engines or (bounded_engines() if bounded else patient_engines()),
            caller_text_tokens=int(self.plan.estimated_input_tokens),
            caller_images=int(self.plan.image_count),
            on_progress=on_progress,
            abandon=abandon,
        )
        return self.prepared

    def inputs(self) -> planning.FileInputs:
        """What the plan made after resolution is given, without the engine's
        count (`planning_inputs` adds it): file text at its byte bound."""
        if self.prepared is None:
            raise RuntimeError("FileRun.inputs() before prepare()")
        service = _service()
        counts = service.planning_inputs(self.prepared)
        spliced = service.splice_messages(self.caller_messages, self.prepared)
        return planning.FileInputs(
            messages=spliced,
            tokens=int(counts.file_tokens),
            bounded_tokens=int(counts.file_bounded_tokens),
            images=int(counts.file_images),
        )

    async def planning_inputs(self, *, counter: Optional[Callable[[str, str, str], Awaitable[Optional[int]]]] = None) -> planning.FileInputs:
        """`inputs()` plus the engine's EXACT count of the file text
        (`FileInputs.measured_tokens`, planning's HOW FILE TEXT IS COUNTED).
        None — and so the byte bound — when the engine cannot count."""
        base = self.inputs()
        measured = await self.measure_file_tokens(counter=counter)
        return dataclasses.replace(base, measured_tokens=measured)

    def _file_text_and_images(self) -> Tuple[str, int]:
        """Every text the files add (the citation addendum, each block, each
        transcript) and the image estimate of the blocks that are pictures."""
        from .. import context as token_context

        prepared = self.prepared
        texts: List[str] = []
        image_tokens = 0
        built = prepared.context
        if getattr(built, "system_addendum", ""):
            texts.append(built.system_addendum)
        for parts in (built.blocks or {}).values():
            for part in parts:
                if part.get("type") == "text":
                    texts.append(str(part.get("text") or ""))
                else:
                    image_tokens += int(token_context.estimate_image_tokens(part))
        for block in (prepared.audio_blocks or {}).values():
            texts.append(str(block.get("text") or ""))
        return "\n\n".join(t for t in texts if t), image_tokens

    async def measure_file_tokens(
        self, *, counter: Optional[Callable[[str, str, str], Awaitable[Optional[int]]]] = None
    ) -> Optional[int]:
        """The file text as the ENGINE tokenizes it, plus the image estimates.

        WHY (senior fix 2026-09-14, review finding "file text is sized by its
        token estimate"). The first hookup sized the gate and the hard input
        ceiling from the 3-chars-per-token estimate. The Qwen pre-tokenizer
        isolates every digit: 120,000 random digits estimated 40,001 tokens and
        planned `main.extended` where the same text typed in planned
        `main.long`, and a numeric CSV's `full` mode passed the 1M ceiling. The
        byte bound alone would push every ~43,000-token prose file into the
        one-at-a-time long gate. `/tokenize` is exact and cannot be gamed in
        either direction; it is what `llm._fit` asks anyway.

        Only engines with `/tokenize` in front of a text window (techsara-35b,
        the router); the OCR model plans on its own image bound. Counted in a
        child task so the count's context variables never touch the caller's."""
        if self.prepared is None:
            raise RuntimeError("FileRun.measure_file_tokens() before prepare()")
        model = self.plan.model
        if model.engine not in (registry.ENGINE_MAIN, registry.ENGINE_ROUTER):
            return None
        text, image_tokens = self._file_text_and_images()
        if not text:
            return image_tokens
        from . import engines as engine_targets

        target = engine_targets.target(model.engine)
        if target is None:
            return None
        count = counter or _engine_token_count
        try:
            exact = await asyncio.ensure_future(count(target.base_url, target.model, text))
        except Exception:  # noqa: BLE001 - the byte bound is the fallback
            log.warning("file text of %s was not counted; planning at its byte bound", self.request_id, exc_info=True)
            return None
        return None if exact is None else int(exact) + int(image_tokens)

    # -- 3. the answer --------------------------------------------------------

    def note_output(self, text: Optional[str]) -> Any:
        """Annotations for the answer's text (once). Never raises: a citation
        that cannot be resolved is plain text, not a failed answer."""
        if self.annotated is not None or self.prepared is None:
            return self.annotated
        try:
            self.annotated = _service().annotate(text or "", self.prepared)
        except Exception:  # noqa: BLE001
            log.warning("file citations were not computed for %s", self.request_id, exc_info=True)
            self.annotated = None
        return self.annotated

    def response_wire(self, wire: Dict[str, Any], text: Optional[str]) -> Dict[str, Any]:
        annotated = self.note_output(text)
        if annotated is None or not annotated.annotations:
            return wire
        from ..apifiles import citations

        return citations.with_annotations(wire, annotated.annotations)

    def chat_body(self, body: Dict[str, Any], text: Optional[str]) -> Dict[str, Any]:
        annotated = self.note_output(text)
        if annotated is None or not annotated.annotations:
            return body
        from ..apifiles import citations

        choices = [dict(choice) for choice in body.get("choices") or []]
        if choices:
            message = dict(choices[0].get("message") or {})
            message["annotations"] = citations.chat_message_annotations(annotated.annotations)
            choices[0]["message"] = message
        return {**body, "choices": choices}

    def usage_meta(self) -> Dict[str, Any]:
        """`usage_events.meta` additions (Files design §5.7): file ids, the
        context mode and tokens, retrieval and rerank, frames, citations and the
        readiness wait. Empty before `prepare` — a request refused before its
        files resolved records no file facts."""
        meta: Dict[str, Any] = {}
        try:
            if self.prepared is not None:
                meta.update(self.prepared.usage_meta())
                meta.setdefault("file_ids", list(self.lifted.file_ids))
            if self.annotated is not None:
                meta.update(self.annotated.meta())
        except Exception:  # noqa: BLE001 - telemetry never fails an answer
            log.warning("file usage meta was not built for %s", self.request_id, exc_info=True)
        return meta

    def cleanup(self) -> None:
        if self._cleaned or self.prepared is None:
            return
        self._cleaned = True
        try:
            self.prepared.cleanup()
        except Exception:  # noqa: BLE001
            log.warning("inline file bytes of %s were not removed", self.request_id, exc_info=True)

    def wrap_finish(self, on_finish: streaming.OnFinish) -> streaming.OnFinish:
        """The recorder, with the citation counts noted from the final text
        first (so they reach the one usage row) and the inline bytes removed
        after — at the response's terminal state, however it got there."""

        async def finish(outcome: streaming.StreamOutcome) -> None:
            try:
                self.note_output(outcome.text)
                await on_finish(outcome)
            finally:
                self.cleanup()

        return finish


#: Tests swap in an `httpx.MockTransport` (the `sidecars._transport` pattern).
_count_transport: Optional[httpx.AsyncBaseTransport] = None
_COUNT_CLIENTS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = weakref.WeakKeyDictionary()


def count_read_budget_s(chars: int) -> float:
    """How long the file count waits for `/tokenize` to answer: a minute plus
    a second per 20,000 characters (~4 minutes for a 3.6 MB file), never less
    than TOKENIZE_TIMEOUT.

    WHY NOT TOKENIZE_TIMEOUT (5 s). That budget is for the chat app's own
    sizing, which falls back to an estimate and loses nothing. Here a count
    that gives up falls back to the BYTE BOUND, which refuses a large `full`
    file the window really holds — a healthy request failed by a clock. The
    budget grows with the work; only an engine that does not answer at all
    reaches it, and that engine could not have generated either."""
    from ..config import settings

    return max(float(getattr(settings, "tokenize_timeout", 5.0) or 5.0), 60.0 + max(0, int(chars)) / 20_000.0)


async def _count_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _COUNT_CLIENTS.get(loop)
    if client is None or client.is_closed:
        transport = _count_transport
        # Built off the loop: constructing a client loads the CA bundle.
        client = await asyncio.to_thread(
            lambda: httpx.AsyncClient(timeout=None, transport=transport, follow_redirects=False)
        )
        _COUNT_CLIENTS[loop] = client
    return client


async def _engine_token_count(base_url: str, model: str, text: str) -> Optional[int]:
    """The engine's `/tokenize` count of one user turn holding `text`, or None
    when the engine did not give one (then the byte bound decides)."""
    from .. import context
    from ..config import settings

    client = await _count_client()
    timeout = httpx.Timeout(
        connect=float(getattr(settings, "llm_connect_timeout", 10.0) or 10.0),
        read=count_read_budget_s(len(text)),
        write=60.0,
        pool=60.0,
    )
    try:
        response = await client.post(
            f"{context.service_root(base_url)}/tokenize",
            json={"model": model, "messages": [{"role": "user", "content": text}]},
            timeout=timeout,
        )
        response.raise_for_status()
        data = await context._tokenize_json(response)
        return int(data["count"])
    except Exception as failure:  # noqa: BLE001 - the byte bound is the fallback
        log.info("file text was not counted by the engine (%s); planning at its byte bound", type(failure).__name__)
        return None


# ------------------------------------------------------------ the stream --


def _comment(text: str) -> str:
    safe = " ".join(str(text).split())
    return f": {safe}\n\n"


def _queued_frame() -> str:
    maker = getattr(events, "queued_comment", None)
    return maker() if callable(maker) else _comment("queued")


async def _wait_with_heartbeats(
    task: "asyncio.Future[Any]",
    notes: "asyncio.Queue[str]",
    heartbeat_s: float,
    idle_frame: Callable[[], str] = lambda: _comment("ping"),
) -> AsyncIterator[str]:
    """Yield progress comments, and `idle_frame()` every `heartbeat_s` of
    quiet, until `task` is done."""
    while not task.done():
        getter = asyncio.ensure_future(notes.get())
        try:
            done, _pending = await asyncio.wait({task, getter}, timeout=heartbeat_s, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not getter.done():
                getter.cancel()
                with contextlib.suppress(BaseException):
                    await getter
        if getter in done and not getter.cancelled():
            yield _comment(getter.result())
        elif not done:
            yield idle_frame()
    while not notes.empty():
        yield _comment(notes.get_nowait())


Launch = Callable[[streaming.GenerationSpec], AsyncIterator[str]]
Gate = Callable[[planning.GenerationPlan], AsyncContextManager[None]]


def default_launch(
    *, chat: bool, on_finish: streaming.OnFinish, completion_id: str = "", include_usage: bool = False
) -> Launch:
    """The stream launch of a build whose router passes none: exactly what the
    router itself calls for a stream without files."""

    def launch(final: streaming.GenerationSpec) -> AsyncIterator[str]:
        if chat:
            return streaming.chat_completions_sse(
                final, completion_id=completion_id, include_usage=include_usage, on_finish=on_finish
            )
        return streaming.responses_sse(final, on_finish=on_finish)

    return launch


async def stream_with_files(
    run: FileRun,
    *,
    chat: bool,
    spec: streaming.GenerationSpec,
    on_finish: streaming.OnFinish,
    replan: Callable[[planning.FileInputs], Awaitable[planning.GenerationPlan]],
    completion_id: str = "",
    include_usage: bool = False,
    heartbeat_s: Optional[float] = None,
    gate: Optional[Gate] = None,
    launch: Optional[Launch] = None,
) -> AsyncIterator[str]:
    """The SSE body of a streaming request with files (module docstring).

    `spec` is the pre-file spec: only its ids and model are used, for a failure
    frame. The generation runs on the spec of the plan made after the files
    resolved, through `launch` — the router's own stream launch, which records
    its own outcome. Everything before that is this function's to record.

    `gate` is the router's patient gate (`patient_gate` when none is given):
    the wait for the engine's capacity has no deadline, and says `: queued`
    at least every heartbeat while it lasts."""
    started = time.monotonic()
    heartbeat_s = HEARTBEAT_S if heartbeat_s is None else float(heartbeat_s)
    gate = gate or patient_gate
    launch = launch or default_launch(
        chat=chat, on_finish=on_finish, completion_id=completion_id, include_usage=include_usage
    )
    notes: "asyncio.Queue[str]" = asyncio.Queue()
    held = contextlib.AsyncExitStack()
    pending: List["asyncio.Future[Any]"] = []
    delegated = False
    recorded = False
    refusal: Optional[errors.ApiError] = None
    carried: Any = None

    async def progress(text: str) -> None:
        notes.put_nowait(text)

    try:
        try:
            preparing = asyncio.ensure_future(run.prepare(DELIVERY_STREAM, on_progress=progress))
            pending.append(preparing)
            async for frame in _wait_with_heartbeats(preparing, notes, heartbeat_s):
                yield frame
            preparing.result()
            counting = asyncio.ensure_future(run.planning_inputs())
            pending.append(counting)
            async for frame in _wait_with_heartbeats(counting, notes, heartbeat_s):
                yield frame
            plan = await replan(counting.result())
            # Always through `gate`, even when `plan.gate_engine` is None: the
            # router's gate decides which gates a plan holds (with T2 a
            # normal-size techsara-35b answer holds `main.normal`), and a plan
            # with none is admitted at once.
            # In a context this body can read afterwards: `capacity.hold`
            # pre-admits a long answer in the context of the task that enters
            # the gate, and the generation below must use that ticket rather
            # than admit the answer a second time (admission.preadmission_carry).
            gate_context = contextvars.copy_context()
            entering = asyncio.get_running_loop().create_task(
                held.enter_async_context(gate(plan)), context=gate_context
            )
            pending.append(entering)
            async for frame in _wait_with_heartbeats(entering, notes, heartbeat_s, _queued_frame):
                yield frame
            entering.result()
            carried = gate_context.run(admission.preadmission_carry)
        except errors.ApiError as exc:
            refusal = exc
        except Exception as exc:  # noqa: BLE001 - every failure is a frame
            refusal = errors.from_unexpected(exc, request_id=run.request_id)

        if refusal is not None:
            outcome = streaming.StreamOutcome(
                response_id=spec.response_id,
                model=spec.model,
                created_at=spec.created_at,
                status="failed",
                error=refusal,
                duration_ms=int((time.monotonic() - started) * 1000),
                max_output_tokens=spec.planned,
            )
            recorded = True
            await streaming.settle(on_finish, outcome)
            if chat:
                chunks = events.ChatCompletionChunks(
                    completion_id=completion_id, model=spec.model, created=spec.created_at, include_usage=include_usage
                )
                yield chunks.error_chunk(refusal)
                yield chunks.done()
            else:
                emitter = events.SequencedEvents(item_id=spec.item_id)
                wire = outcome.response().to_wire()
                yield emitter.created(dict(wire, status="queued", usage=None, output=[], error=None))
                yield emitter.failed(wire)
            return

        final = streaming.spec_from_plan(plan, response_id=spec.response_id, created_at=spec.created_at)
        inner = launch(final)
        delegated = True
        try:
            with admission.adopt_preadmission(carried):
                async for frame in inner:
                    yield frame
        finally:
            await inner.aclose()
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        with contextlib.suppress(BaseException):
            await held.aclose()
        if not delegated and not recorded:
            # The client left while its files were being waited for or its
            # gate was queued: nothing ran, the caller stopped it.
            await streaming.settle(
                on_finish,
                streaming.StreamOutcome(
                    response_id=spec.response_id,
                    model=spec.model,
                    created_at=spec.created_at,
                    status="cancelled",
                    client_gone=True,
                    duration_ms=int((time.monotonic() - started) * 1000),
                ),
            )


# -------------------------------------------------------- the background --


#: The detached file waits of background requests, held strongly so a task
#: is never collected mid-await.
_BACKGROUND_WAITS: Set["asyncio.Task[None]"] = set()

#: How often a background file wait re-reads its row's cancel flag: the
#: running job's own cadence (`background.CANCEL_POLL_SECONDS`).
def _cancel_poll_s() -> float:
    from . import background

    return max(0.05, float(getattr(background, "CANCEL_POLL_SECONDS", 3.0)))


def background_waits() -> int:
    """Background requests of this process still waiting for their files."""
    return sum(1 for task in _BACKGROUND_WAITS if not task.done())


async def start_background(
    run: FileRun,
    *,
    spec: streaming.GenerationSpec,
    caller: Any,
    on_finish: streaming.OnFinish,
    replan: Callable[[planning.FileInputs], Awaitable[planning.GenerationPlan]],
    start: Callable[[streaming.GenerationSpec], Awaitable[Any]],
    request_id: str,
    metadata: Optional[Mapping[str, Any]] = None,
    instructions_present: bool = False,
    fingerprint: str = "",
) -> Dict[str, Any]:
    """A background request with files: the row and the 202 NOW, the file wait
    in a detached task (senior fix 2026-09-14, review finding "background
    requests with files wait silently before the 202").

    Before: `prepare` ran bounded BEFORE the 202 — up to 45 s with no byte on
    the wire, then `409 file_not_ready` that both SDKs retry into the same
    silence. Now:

    1. the project's concurrency slot is taken HERE (a 429 is still a real
       status when limits are on) and held for the wait;
    2. the `api_responses` row is written `queued`, exactly as
       `background.start` writes it, and returned for the 202;
    3. the task waits for the files with no deadline and patient engines,
       plans again with the file text, gives the slot back and hands the final
       spec to `start` — the router's own `background.start`, which finds the
       row, takes the job's slot and runs (or launches durably);
    4. a cancel (`POST /v1/responses/{id}/cancel` closes a row no job holds)
       stops the wait at its next poll; the outcome is recorded `cancelled`;
    5. a refusal while waiting (the file failed, an input rule) is recorded
       `failed` through the router's recorder, which also releases the
       Idempotency-Key, and the `response.failed` webhook is queued.
    """
    from .. import db
    from ..apiplatform import quotas
    from . import background

    slot = contextlib.ExitStack()
    slot.enter_context(quotas.concurrency_slot(caller, background.SLOT_KIND))
    try:
        row = await db.run_in_thread(
            db.create_api_response,
            caller.project_id,
            caller.workspace_id,
            spec.model,
            request_id or spec.response_id,
            response_id=spec.response_id,
            key_id=(caller.key_id or None),
            status="queued",
            background=True,
            streamed=False,
            fingerprint=fingerprint,
            instructions_present=instructions_present,
            metadata=dict(metadata or {}),
            max_output_tokens=spec.planned,
        )
    except BaseException:
        slot.close()
        run.cleanup()
        raise
    task = asyncio.ensure_future(
        _wait_then_start(run, spec=spec, caller=caller, on_finish=on_finish, replan=replan, start=start, slot=slot)
    )
    _BACKGROUND_WAITS.add(task)
    task.add_done_callback(_BACKGROUND_WAITS.discard)
    # The slot is given back however the task ends, including a cancel before
    # its first step (a task cancelled before it runs never reaches `finally`).
    task.add_done_callback(lambda _task: slot.close())
    return row


async def _cancel_requested(project_id: str, response_id: str) -> bool:
    from .. import db

    row = await db.run_in_thread(db.get_api_response, response_id, project_id)
    return bool(row is None or row.get("cancel_requested") or str(row.get("status") or "") == "cancelled")


async def _watch_cancel(project_id: str, response_id: str, cancel: asyncio.Event) -> None:
    while not cancel.is_set():
        await asyncio.sleep(_cancel_poll_s())
        try:
            if await _cancel_requested(project_id, response_id):
                cancel.set()
                return
        except Exception:  # noqa: BLE001 - a failed poll is retried
            log.debug("cancel poll failed for %s", response_id, exc_info=True)


async def _wait_then_start(
    run: FileRun,
    *,
    spec: streaming.GenerationSpec,
    caller: Any,
    on_finish: streaming.OnFinish,
    replan: Callable[[planning.FileInputs], Awaitable[planning.GenerationPlan]],
    start: Callable[[streaming.GenerationSpec], Awaitable[Any]],
    slot: contextlib.ExitStack,
) -> None:
    started = time.monotonic()
    cancel = asyncio.Event()
    watcher = asyncio.ensure_future(_watch_cancel(caller.project_id, spec.response_id, cancel))
    outcome: Optional[streaming.StreamOutcome] = None
    final: Optional[streaming.GenerationSpec] = None
    try:
        try:
            await run.prepare(DELIVERY_BACKGROUND, abandon=cancel, no_deadline=True)
            if cancel.is_set() or await _cancel_requested(caller.project_id, spec.response_id):
                cancel.set()
                raise asyncio.CancelledError()
            plan = await replan(await run.planning_inputs())
            final = streaming.spec_from_plan(plan, response_id=spec.response_id, created_at=spec.created_at)
        except asyncio.CancelledError:
            if not cancel.is_set():
                # The loop is going away (a deploy): retry-safe, like a
                # running background job cut off the same way.
                outcome = _terminal(spec, started, "failed", errors.model_unavailable())
                with contextlib.suppress(BaseException):
                    await streaming.settle(on_finish, outcome)
                raise
            outcome = _terminal(spec, started, "cancelled", None)
        except errors.ApiError as refusal:
            outcome = _terminal(spec, started, "failed", refusal)
        except Exception as exc:  # noqa: BLE001 - a job never dies unrecorded
            log.warning("background response %s failed while its files were prepared", spec.response_id, exc_info=True)
            outcome = _terminal(spec, started, "failed", errors.from_unexpected(exc, request_id=run.request_id))
    finally:
        watcher.cancel()
        with contextlib.suppress(BaseException):
            await watcher
    if outcome is not None:
        await streaming.settle(on_finish, outcome)
        run.cleanup()
        await _notify_terminal(caller, spec.response_id)
        return
    assert final is not None
    # The wait's slot is given back just before `background.start` takes the
    # job's own: holding both would count one request twice.
    slot.close()
    try:
        await start(final)
    except BaseException as exc:
        failure = exc if isinstance(exc, errors.ApiError) else errors.from_unexpected(exc, request_id=run.request_id)
        with contextlib.suppress(BaseException):
            await streaming.settle(on_finish, _terminal(spec, started, "failed", failure))
        run.cleanup()
        if not isinstance(exc, Exception):
            raise


def _terminal(
    spec: streaming.GenerationSpec, started: float, status: str, error: Optional[errors.ApiError]
) -> streaming.StreamOutcome:
    return streaming.StreamOutcome(
        response_id=spec.response_id,
        model=spec.model,
        created_at=spec.created_at,
        status=status,
        error=error,
        duration_ms=int((time.monotonic() - started) * 1000),
        max_output_tokens=spec.planned,
    )


async def _notify_terminal(caller: Any, response_id: str) -> None:
    """The `response.failed` / `.cancelled` webhook `background._settle`
    queues for a job — this one ended before a job existed."""
    from .. import db
    from . import background

    notify = getattr(background, "_notify", None)
    if notify is None:  # pragma: no cover
        return
    try:
        row = await db.run_in_thread(db.get_api_response, response_id, caller.project_id)
        if row is not None:
            await notify(row, caller.workspace_id)
    except Exception:  # noqa: BLE001 - a webhook never changes the row
        log.warning("could not queue the terminal webhook of %s", response_id, exc_info=True)


__all__ = [
    "DELIVERY_BACKGROUND",
    "DELIVERY_STREAM",
    "DELIVERY_SYNC",
    "FileRun",
    "SharedReadiness",
    "background_waits",
    "has_files",
    "lift",
    "patient_engines",
    "patient_gate",
    "patient_hold",
    "patient_transcriber",
    "pending_inputs",
    "shared_readiness",
    "start_background",
    "stream_with_files",
]
