"""`POST /v1/embeddings`, `POST /v1/rerank`, `POST /v1/audio/transcriptions`.

ADDED 2026-09-13 (owner request: `/v1` offers EVERY model TechSara runs, not
only `techsara-35b`). Three endpoints for the three models that do not
generate text — `techsara-embed`, `techsara-rerank`, `techsara-whisper`.

THE SAME ROUTE, IN THE SAME ORDER, AS THE EIGHT BEFORE THEM (CONTRACT §4):

    resolve the caller (Bearer only) → the scope → the origin
      → count the request (quotas.reserve, once) → refuse an Idempotency-Key
      → validate the body → the model (404 when this key may not use it)
      → the endpoint capability (400 when the model cannot do this)
      → the engine, under its capacity gate → record usage once → answer

Registered on the SAME `APIRouter` as the other routes (`register(router)`,
called from the bottom of `router.py`), so every one of them is a
`PublicRoute`: the §9 envelope on every failure, `X-Request-Id` on every
response, CORS without credentials, and no cookie read anywhere. There is no
second implementation of any of that here, and `main.py` needs no change.

WHAT THESE ROUTES DO NOT DO, DELIBERATELY.

* **No `Idempotency-Key`.** A retried embedding, rerank or transcription
  changes nothing on the server — there is no response object to replay and no
  row to find — so the header is refused (400, naming it) rather than accepted
  and ignored, which a caller could not tell apart from being honoured.
* **No `api_responses` row.** `GET /v1/responses/{id}` describes generations;
  these calls are metered in the usage ledgers exactly once each and that is
  all that is kept (CONTRACT §16: metadata, never content — no input text, no
  vector, no transcript).
* **No per-caller limit.** Owner decision 2026-09-13: the API has no usage
  limits. The one refusal that is not the caller's fault is the SHARED engine
  being busy with public work — the capacity gate — and that is
  `503 model_unavailable` with `Retry-After`, never a 429.

NO CLOCK ON THESE ROUTES (no-timeout design, 2026-09-14). Each request
checks its shape and lengths BEFORE any wait — so a refusal is a real status —
and then answers through `keepalive.CommittedJSONResponse(failure_mode=
"abort")`: the real status and body when the work finishes within the commit
window, otherwise `200`, a space at once and one every heartbeat, then the
object. The capacity gate waits with no limit and an engine is judged by its
own evidence (`sidecars`), so a long queue or a slow engine is bytes, never a
`503` "at capacity" or a `504`. A failure after the commit drops the
connection so the SDK retries (embeddings and rerank recompute; a
transcription's finished windows are cached).

THE AUDIO BODY GOES TO DISK AS IT ARRIVES. `request.form()` would spool it to
an unbounded temporary file; the JSON routes' cap would refuse every real
recording. `multipart.read_form` hands the file part to a `disk_ledger.DiskSink`
chunk by chunk (sha256 on the way, 0600, under the disk ledger), and
`audio_jobs` transcribes it in windows — any duration, up to 89 MiB per
request, larger recordings by Files API `file_id`.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Mapping, Optional, Set

from fastapi import APIRouter, Depends, Request, Response
from starlette.responses import StreamingResponse

from ..apiplatform.resolver import ApiCaller
from ..apiplatform.scopes import Scope, ScopeRequirement, requires
from . import audio_jobs, disk_ledger, endpoint_models, errors, keepalive, multipart, sidecars

log = logging.getLogger(__name__)

#: CONTRACT §7, one scope per route — data, so the OpenAPI document and the
#: tests read the requirement the handler enforces.
SCOPES: Dict[str, ScopeRequirement] = {
    "create_embedding": requires(Scope.EMBEDDINGS_WRITE),
    "create_rerank": requires(Scope.RERANK_WRITE),
    "create_transcription": requires(Scope.AUDIO_WRITE),
}

#: A transcription that names a Files API file also needs `files.read`
#: (Files design §2.1: "may use" is "may read"), checked before the id is.
FILE_ID_SCOPE: ScopeRequirement = requires(Scope.FILES_READ)

#: The public path of each operation, as the registry's `endpoints` spell it.
PATHS: Dict[str, str] = {
    "create_embedding": "/v1/embeddings",
    "create_rerank": "/v1/rerank",
    "create_transcription": "/v1/audio/transcriptions",
}

#: `usage_events.route` for each (CONTRACT §16).
LEDGER_ROUTES: Dict[str, str] = {
    "create_embedding": "v1_embeddings",
    "create_rerank": "v1_rerank",
    "create_transcription": "v1_audio_transcriptions",
}

#: The id prefix written as `usage_events.generation_id`. No object with this
#: id can be fetched; it exists so one request is one ledger row (the unique
#: index on generation_id) and so support can name it.
ID_PREFIXES: Dict[str, str] = {
    "create_embedding": "emb",
    "create_rerank": "rrk",
    "create_transcription": "asr",
}

#: Above this, JSON is decoded in a worker thread: a large body decoded on the
#: event loop stalls every chat stream in the process for the length of the
#: parse (the 2026-09-05 Fast-mode lesson: TTFT 0.7 → 11.7 s was the
#: orchestrator's own loop).
_OFF_LOOP_JSON_BYTES = 64 * 1024

KIND_SYNC = "sync"

#: Transcription settlements waiting for a job whose client left (so a job
#: that finishes after its request is still metered once). Referenced here so
#: the tasks are not collected mid-wait.
_SETTLING: Set["asyncio.Task[None]"] = set()


def _rt():
    """The router module, looked up when a request runs.

    Not imported at module load: `router.py` imports THIS module from its last
    line, and a top-level import back would read a half-initialised module."""
    from . import router

    return router


async def _caller(request: Request) -> ApiCaller:
    """`router.resolve_caller`: the Authorization header and nothing else."""
    return await _rt().resolve_caller(request)


# ---------------------------------------------------------------- gates --


async def _authorize(request: Request, caller: ApiCaller, operation: str) -> None:
    """Scope, then origin: `router.authorize_scope`, the same two checks every
    other `/v1` route makes, with THIS file's requirement."""
    await _rt().authorize_scope(request, caller, SCOPES[operation])


async def _admit(request: Request, caller: ApiCaller, *, estimated_input_tokens: int) -> Any:
    """Count this request once (`quotas.reserve`, kind `sync`).

    `max_output_tokens=1`: nothing is generated, and a None would reserve the
    platform's 8,192-token output default for the length of the call."""
    return await _rt().admit(
        request,
        caller,
        kind=KIND_SYNC,
        estimated_input_tokens=max(0, int(estimated_input_tokens or 0)),
        max_output_tokens=1,
    )


def _refuse_idempotency_key(request: Request, path: str) -> None:
    if (request.headers.get("idempotency-key") or "").strip():
        raise errors.invalid_request(
            f"Idempotency-Key is not supported on {path}: the request changes nothing a "
            "retry could repeat, so send it again without the header.",
            param="Idempotency-Key",
        )


def supports(model: Any, path: str) -> bool:
    """The registry's answer: a model lists the paths it may be used on."""
    return bool(model.supports_endpoint(path))


async def _model_for(caller: ApiCaller, model_id: str, operation: str) -> Any:
    """404 when this key may not use the model (the same 404 as no such
    model — CONTRACT §4), then 400 when it is permitted but cannot do this."""
    from . import registry

    model = await _rt().resolve_model(caller, model_id)
    path = PATHS[operation]
    if not supports(model, path):
        raise errors.invalid_request(
            registry.unsupported_endpoint_message(model.id, path), param="model"
        )
    return model


# -------------------------------------------------------------- bodies --


async def _read_json(request: Request, *, limit: Optional[int] = None) -> Any:
    """The JSON body under its cap (the route's, else the §12 JSON rule),
    counted while it arrives."""
    parsed, _size = await _read_json_sized(request, limit=limit)
    return parsed


def _declared_body_bytes(request: Request, limit: int) -> int:
    """What a body will hold before it is read: its Content-Length, or the
    cap when it is chunked (or declares more, which the read refuses)."""
    declared = (request.headers.get("content-length") or "").strip()
    if declared.isdigit():
        return min(int(declared), int(limit))
    return int(limit)


async def _read_json_sized(request: Request, *, limit: Optional[int] = None) -> "tuple[Any, int]":
    """`_read_json`, and how many bytes the body was."""
    limit = endpoint_models.max_json_body_bytes() if limit is None else int(limit)
    declared = request.headers.get("content-length")
    if declared and declared.strip().isdigit() and int(declared) > limit:
        raise errors.request_too_large(limit)
    chunks = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise errors.request_too_large(limit)
        chunks.append(chunk)
    raw = b"".join(chunks)
    del chunks
    if not raw.strip():
        raise errors.invalid_request("The request body must be a JSON object.")
    try:
        if len(raw) > _OFF_LOOP_JSON_BYTES:
            return await asyncio.to_thread(json.loads, raw), total
        return json.loads(raw), total
    except ValueError:
        # The decoder's message names an offset into the caller's text.
        raise errors.invalid_request("The request body is not valid JSON.") from None


async def _json_response(body: Mapping[str, Any], *, status: int = 200) -> Response:
    """Rendered off the loop when large: 256 embeddings of 1,024 floats is
    ~5 MB of JSON, and `json.dumps` of that is a few hundred milliseconds of
    CPU nobody else in the process can use."""

    def render() -> bytes:
        return json.dumps(
            body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")

    content = await asyncio.to_thread(render)
    return Response(content=content, status_code=status, media_type="application/json")


# ------------------------------------------------------------- the ledger --


@dataclass
class _Ledger:
    """The one write per request of CONTRACT §16, and the quota settlement.

    Every exit settles exactly once:

    * NOTHING RAN (a validation 400, a 404, a capacity 503 before the first
      engine call): the reservation is given back as `cancelled` — the request
      stays counted, nothing else is — and no usage_events row is written, the
      rule `router._nothing_ran` follows for a generation that never started;
    * THE ENGINE WAS CALLED and failed: `failed` (counted in the day's
      errors), charged for what the completed calls measured;
    * COMPLETED: the measured counts.

    A ledger write that fails never fails the answer.
    """

    caller: ApiCaller
    reservation: Any
    operation: str
    request_id: str
    started: float = field(default_factory=time.perf_counter)
    generation_id: str = ""
    model_id: str = ""
    settled: bool = False

    def __post_init__(self) -> None:
        self.generation_id = f"{ID_PREFIXES[self.operation]}_{secrets.token_hex(12)}"

    def _duration_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)

    async def _quota(self, input_tokens: Optional[int], output_tokens: Optional[int], status: str) -> None:
        from .. import db
        from ..apiplatform import quotas

        if self.reservation is None:
            return
        try:
            await db.run_in_thread(
                functools.partial(
                    quotas.record_usage,
                    self.caller,
                    input_tokens,
                    output_tokens,
                    status,
                    reservation=self.reservation,
                )
            )
        except Exception:  # noqa: BLE001
            log.warning("usage for %s was not settled", self.generation_id, exc_info=True)

    async def _event(
        self,
        *,
        status: str,
        input_tokens: Optional[int],
        output_tokens: Optional[int],
        error_code: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        from .. import usage as usage_ledger

        await usage_ledger.record_async(
            user_id=None,
            workspace_id=self.caller.workspace_id or None,
            conversation_id=None,
            generation_id=self.generation_id,
            route=LEDGER_ROUTES[self.operation],
            effort="",
            model=self.model_id,
            mode="api",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            ttft_ms=None,
            duration_ms=self._duration_ms(),
            status=status,
            error_kind=error_code,
            meta={
                "api_key_id": self.caller.key_id,
                "project_id": self.caller.project_id,
                "request_id": self.request_id,
                **(meta or {}),
            },
        )

    async def _shielded(self, coroutine: Any) -> None:
        # Shielded: a client that disconnects cancels the handler, and the
        # settlement that follows must not be cancelled with it or the
        # reservation stays spent for the rest of the minute.
        task = asyncio.ensure_future(coroutine)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("ledger write failed for %s", self.generation_id, exc_info=True)

    async def nothing_ran(self) -> None:
        if self.settled:
            return
        self.settled = True
        await self._shielded(self._quota(0, 0, "cancelled"))

    async def failed(
        self,
        failure: "sidecars.SidecarError",
        *,
        token_counted: bool = True,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self.settled:
            return
        if failure.engine_calls <= 0:
            await self.nothing_ran()
            return
        from .. import usage as usage_ledger

        self.settled = True
        measured = int(failure.measured_tokens) if token_counted else 0

        async def write() -> None:
            await self._event(
                status=usage_ledger.ERROR,
                input_tokens=measured if token_counted else None,
                output_tokens=0 if token_counted else None,
                error_code=failure.error.code,
                meta={"engine_calls": failure.engine_calls, **(meta or {})},
            )
            await self._quota(measured, 0, "failed")

        await self._shielded(write())

    async def completed(
        self,
        *,
        input_tokens: Optional[int],
        output_tokens: Optional[int],
        quota_input: Optional[int],
        meta: Dict[str, Any],
    ) -> None:
        if self.settled:
            return
        from .. import usage as usage_ledger

        self.settled = True

        async def write() -> None:
            await self._event(
                status=usage_ledger.OK,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                meta=meta,
            )
            await self._quota(quota_input, 0, "completed")

        await self._shielded(write())


def _request_id(request: Request) -> str:
    return str(_rt().request_id(request) or "")


# -------------------------------------------------------------- routes --


def _committed(
    request: Request,
    work: Any,
    *,
    response_class: type = keepalive.CommittedJSONResponse,
    spent_s: float = 0.0,
) -> Response:
    """The byte-invariant answer of a sidecar route (module docstring).

    `spent_s`: how long the route was silent BEFORE this response, counting
    lengths (`sidecars.check_*_lengths`). It comes off the commit window, so
    the first byte still leaves within PUBLIC_API_SYNC_COMMIT_S of the check
    starting, however slow `/tokenize` was (review 2026-09-14)."""
    commit_s = max(0.0, keepalive.sync_commit_s() - max(0.0, float(spent_s)))
    return response_class(
        work, failure_mode=keepalive.FAILURE_ABORT, request_id=_request_id(request), commit_s=commit_s
    )


def _render_embeddings(
    vectors: Any, *, base64_wanted: bool, model_id: str, prompt_tokens: Optional[int]
) -> bytes:
    """The embeddings object, rendered one vector at a time: byte-identical to
    `json.dumps` of the whole dict, without ever holding every vector as a
    list of Python floats (a 2,048-input answer was ~65 MiB of them, review
    2026-09-14). Run in a worker thread."""
    dumps = functools.partial(json.dumps, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    parts = [b'{"object":"list","data":[']
    for index, vector in enumerate(vectors):
        if index:
            parts.append(b",")
        if base64_wanted:
            embedding: Any = sidecars.encode_base64(vector)
        else:
            embedding = vector.tolist() if hasattr(vector, "tolist") else list(vector)
        parts.append(dumps({"object": "embedding", "index": index, "embedding": embedding}).encode("utf-8"))
    usage = None if prompt_tokens is None else {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens}
    parts.append(b'],"model":' + dumps(model_id).encode("utf-8") + b',"usage":' + dumps(usage).encode("utf-8") + b"}")
    return b"".join(parts)


async def create_embedding(request: Request, caller: ApiCaller = Depends(_caller)) -> Response:
    """OpenAI-shaped embeddings on `techsara-embed` (1,024 dimensions)."""
    operation = "create_embedding"
    await _authorize(request, caller, operation)
    cap = endpoint_models.max_pooling_body_bytes()
    # The physical memory guard, before a byte of the body is held
    # (`sidecars.PoolingMemory`): a real 503 with Retry-After, not counted.
    memory = sidecars.POOLING_MEMORY.reserve(sidecars.embed_memory_bytes(_declared_body_bytes(request, cap), 0))
    try:
        return await _embedding_response(request, caller, operation, memory, cap)
    except BaseException:
        memory.release()
        raise


async def _embedding_response(
    request: Request, caller: ApiCaller, operation: str, memory: "sidecars.MemoryReservation", cap: int
) -> Response:
    parsed: Optional[endpoint_models.EmbeddingsRequest] = None
    deferred: Optional[errors.ApiError] = None
    estimate = 0
    body_bytes = 0
    try:
        payload, body_bytes = await _read_json_sized(request, limit=cap)
        parsed = endpoint_models.parse_embeddings_request(payload)
        del payload
        from .. import context

        estimate = sum(context.estimate_tokens(text) for text in parsed.inputs())
    except errors.ApiError as refusal:
        # Refused AFTER the request is counted, as the generating routes do
        # (CONTRACT §4 puts quota before validation).
        deferred = refusal
    reservation = await _admit(request, caller, estimated_input_tokens=estimate)
    ledger = _Ledger(caller, reservation, operation, _request_id(request))
    try:
        _refuse_idempotency_key(request, PATHS[operation])
        if deferred is not None:
            raise deferred
        assert parsed is not None
        model = await _model_for(caller, parsed.model, operation)
        ledger.model_id = model.id
        inputs = parsed.inputs()
        single = isinstance(parsed.input, str)
        base64_wanted = parsed.encoding_format == "base64"
        # The exact charge now the inputs are known: their vectors until the
        # response is sent. A refusal here is still before the status line.
        memory.resize(sidecars.embed_memory_bytes(body_bytes, len(inputs), base64_wanted=base64_wanted))
        # Before any gate and before the commit clock: an over-length input
        # is a real 400 however long the queue is.
        checked_at = time.monotonic()
        lengths = await sidecars.check_embed_lengths(inputs, single_string=single)
        spent_s = time.monotonic() - checked_at
    except BaseException:
        await ledger.nothing_ran()
        raise

    async def work() -> Response:
        try:
            try:
                # wait_s=None: no clock on the route (module docstring).
                outcome = await sidecars.embed(inputs, single_string=single, lengths=lengths, wait_s=None)
            except sidecars.SidecarError as failure:
                await ledger.failed(failure, meta={"inputs": len(inputs)})
                raise failure.error from None
            except BaseException:
                await ledger.nothing_ran()
                raise
            content = await asyncio.to_thread(
                _render_embeddings,
                outcome.vectors,
                base64_wanted=base64_wanted,
                model_id=model.id,
                prompt_tokens=outcome.prompt_tokens,
            )
            del outcome.vectors[:]
            await ledger.completed(
                input_tokens=outcome.prompt_tokens,
                # 0, not None: a pooling pass generates nothing, and that is measured.
                output_tokens=0,
                quota_input=outcome.prompt_tokens,
                meta={"inputs": len(inputs), "engine_calls": outcome.engine_calls, **_resend_meta(outcome.resends)},
            )
            return Response(content=content, status_code=200, media_type="application/json")
        finally:
            memory.release()

    response = _committed(request, work, spent_s=spent_s)
    memory.bind(response)
    return response


def _resend_meta(resends: Mapping[str, int]) -> Dict[str, Any]:
    return {"resends": dict(resends)} if resends else {}


async def create_rerank(request: Request, caller: ApiCaller = Depends(_caller)) -> Response:
    """Cohere/Jina-shaped reranking on `techsara-rerank`."""
    operation = "create_rerank"
    await _authorize(request, caller, operation)
    cap = endpoint_models.max_pooling_body_bytes()
    memory = sidecars.POOLING_MEMORY.reserve(sidecars.rerank_memory_bytes(_declared_body_bytes(request, cap), 0))
    try:
        return await _rerank_response(request, caller, operation, memory, cap)
    except BaseException:
        memory.release()
        raise


async def _rerank_response(
    request: Request, caller: ApiCaller, operation: str, memory: "sidecars.MemoryReservation", cap: int
) -> Response:
    parsed: Optional[endpoint_models.RerankRequest] = None
    deferred: Optional[errors.ApiError] = None
    estimate = 0
    body_bytes = 0
    try:
        payload, body_bytes = await _read_json_sized(request, limit=cap)
        parsed = endpoint_models.parse_rerank_request(payload)
        del payload
        from .. import context

        query_tokens = context.estimate_tokens(parsed.query)
        estimate = sum(query_tokens + context.estimate_tokens(text) for text in parsed.texts())
    except errors.ApiError as refusal:
        deferred = refusal
    reservation = await _admit(request, caller, estimated_input_tokens=estimate)
    ledger = _Ledger(caller, reservation, operation, _request_id(request))
    try:
        _refuse_idempotency_key(request, PATHS[operation])
        if deferred is not None:
            raise deferred
        assert parsed is not None
        model = await _model_for(caller, parsed.model, operation)
        ledger.model_id = model.id
        texts = parsed.texts()
        memory.resize(sidecars.rerank_memory_bytes(body_bytes, len(texts)))
        checked_at = time.monotonic()
        lengths = await sidecars.check_rerank_lengths(
            sidecars.rerank_query_text(parsed.query, parsed.instruction),
            [sidecars.rerank_document_text(text) for text in texts],
        )
        spent_s = time.monotonic() - checked_at
    except BaseException:
        await ledger.nothing_ran()
        raise

    async def work() -> Response:
        try:
            return await scored()
        finally:
            memory.release()

    async def scored() -> Response:
        try:
            outcome = await sidecars.rerank_scores(
                parsed.query, texts, instruction=parsed.instruction, lengths=lengths, wait_s=None
            )
        except sidecars.SidecarError as failure:
            await ledger.failed(failure, meta={"documents": len(texts)})
            raise failure.error from None
        except BaseException:
            await ledger.nothing_ran()
            raise
        results = endpoint_models.ranked(
            outcome.scores, texts, top_n=parsed.top_n, return_documents=parsed.return_documents
        )
        body = endpoint_models.RerankResponse(
            id=ledger.generation_id,
            model=model.id,
            results=results,
            usage=(
                None
                if outcome.input_tokens is None
                else endpoint_models.RerankUsage(
                    input_tokens=outcome.input_tokens, total_tokens=outcome.input_tokens
                )
            ),
        ).to_wire()
        await ledger.completed(
            input_tokens=outcome.input_tokens,
            output_tokens=0,
            quota_input=outcome.input_tokens,
            meta={
                "documents": len(texts),
                "top_n": parsed.top_n,
                "engine_calls": outcome.engine_calls,
                **_resend_meta(outcome.resends),
            },
        )
        return await _json_response(body)

    response = _committed(request, work, spent_s=spent_s)
    memory.bind(response)
    return response


# -------------------------------------------------------- transcription --


class _CommittedText(keepalive.CommittedJSONResponse):
    """`response_format=text` after a commit: the whitespace heartbeats, then
    the transcript, as `text/plain` (CONTRACT §8.6: the documentation tells
    callers to strip the leading spaces; the gateway recognises a committed
    text transcript by this content type)."""

    media_type = "text/plain; charset=utf-8"

    def _committed_start(self) -> dict:
        start = super()._committed_start()
        start["headers"] = [
            (name, b"text/plain; charset=utf-8" if name == b"content-type" else value)
            for name, value in start["headers"]
        ]
        return start


#: The run name a tagged transcription answers with carries the response
#: format after the job key: a gateway re-attach sends an EMPTY body, and a
#: `json` and a `text` request share one job (the key's format CLASS), so the
#: format the client asked for travels in the name the gateway echoes back.
_RUN_FORMATS = tuple(endpoint_models.TRANSCRIPTION_FORMATS)


def _run_key(job_key: str, response_format: str) -> str:
    return f"{job_key}-{response_format}"


def _parse_run_key(value: str) -> Optional[tuple]:
    key, _, response_format = str(value or "").rpartition("-")
    if not key or response_format not in _RUN_FORMATS:
        return None
    return key, response_format


class _AudioIngest:
    """`multipart.FileWriter` into the job registry's incoming directory,
    under the disk ledger: the file part is on disk, hashed, before any gate."""

    def __init__(self, jobs: audio_jobs.AudioJobs) -> None:
        self.jobs = jobs
        self.sink: Optional[disk_ledger.DiskSink] = None
        self.part: Optional[multipart.FilePart] = None

    async def start(self, part: multipart.FilePart) -> None:
        if part.name != "file":
            return
        self.part = part
        self.sink = disk_ledger.DiskSink(
            os.path.join(self.jobs.root, "incoming"),
            cap_bytes=endpoint_models.max_audio_bytes(),
            ledger=self.jobs.ledger,
            purpose="asr-ingest",
        )
        await self.sink.__aenter__()

    async def write(self, data: bytes) -> None:
        if self.sink is not None:
            try:
                await self.sink.write(data)
            except disk_ledger.CapExceeded as exc:
                raise errors.request_too_large(exc.cap_bytes) from None

    async def commit(self) -> audio_jobs.AudioSource:
        assert self.sink is not None
        final = os.path.join(self.jobs.root, "incoming", f"src.{secrets.token_hex(12)}")
        stored = await self.sink.commit(final)
        self.sink = None
        return audio_jobs.AudioSource(path=stored.path, bytes=stored.bytes, sha256=stored.sha256, owned=True)

    async def abort(self) -> None:
        sink, self.sink = self.sink, None
        if sink is not None:
            with contextlib.suppress(Exception):
                await sink.abort()


async def _file_source(request: Request, caller: ApiCaller, file_id: str) -> audio_jobs.AudioSource:
    """A Files API file of THIS project as the audio, used in place.

    `files.read` first (a key without it learns nothing about ids), then one
    lookup whose 404 is the same for a malformed, deleted, expired or foreign
    id (Files design §7.2 rule 2)."""
    from .. import db
    from ..apifiles import ids, schema, service, storage
    from .files import wire

    await _rt().authorize_scope(request, caller, FILE_ID_SCOPE)
    lookup = file_id if ids.is_file_id(file_id) else "file-" + "0" * 24
    row = await db.run_in_thread(schema.get_api_file, caller.project_id, lookup)
    if row is None or lookup != file_id:
        raise wire.file_not_found("file_id")
    if row.get("error_code"):
        raise wire.invalid_request(
            "This file has no content: " + wire.default_processing_view(row)["status_details"], param="file_id"
        )
    if row.get("blob_id") is None or not row.get("blob_sha256"):
        raise wire.file_not_ready(
            5, "This file's bytes are still being assembled. Retry after the Retry-After interval."
        )
    record = service.record_from_row(row)
    if record is not None and record.kind not in ("unknown", "") and not record.media:
        raise errors.invalid_request("file_id must name an audio or video file.", param="file_id")
    path = storage.original_path(caller.project_id, str(row["blob_sha256"]))
    return await audio_jobs.AudioSource.from_path(path, sha256=str(row["blob_sha256"]), owned=False)


def _media_type(content_type: Optional[str]) -> str:
    return str(content_type or "").split(";")[0].strip().lower()


async def create_transcription(request: Request, caller: ApiCaller = Depends(_caller)) -> Response:
    """OpenAI-shaped speech-to-text on `techsara-whisper`, any duration."""
    operation = "create_transcription"
    path = PATHS[operation]
    await _authorize(request, caller, operation)
    tag = _rt().gateway_tag(request)
    if tag.attach_job is not None:
        return await _reattach_transcription(request, caller, tag)
    # Counted BEFORE the body is read: whisper reports no tokens to estimate,
    # and a refused request should not have made this process store the file.
    reservation = await _admit(request, caller, estimated_input_tokens=0)
    ledger = _Ledger(caller, reservation, operation, _request_id(request))
    jobs = audio_jobs.jobs()
    ingest = _AudioIngest(jobs)
    source: Optional[audio_jobs.AudioSource] = None
    parsed: Optional[endpoint_models.TranscriptionRequest] = None
    try:
        _refuse_idempotency_key(request, path)
        content_type = request.headers.get("content-type")
        file_part: Optional[multipart.FilePart] = None
        if _media_type(content_type) == "application/json":
            parsed = endpoint_models.parse_transcription_json(await _read_json(request))
        else:
            form = await multipart.read_form(
                request.stream(),
                content_type,
                max_body_bytes=endpoint_models.max_audio_body_bytes(),
                max_file_bytes=endpoint_models.max_audio_bytes(),
                declared_length=request.headers.get("content-length"),
                file_writer=ingest,
            )
            parsed = endpoint_models.parse_transcription_form(form.fields)
            file_part = form.file("file")
        if file_part is not None and parsed.file_id is not None:
            raise errors.invalid_request("Send either file or file_id, not both.", param="file_id")
        if file_part is None and parsed.file_id is None:
            raise errors.invalid_request("file is required.", param="file")
        if file_part is not None:
            if file_part.content_type not in sidecars.allowed_audio_types():
                raise errors.invalid_request(
                    "The file's Content-Type must be a supported audio type, such as audio/mpeg, "
                    "audio/wav, audio/webm or audio/mp4.",
                    param="file",
                )
            if file_part.size == 0:
                raise errors.invalid_request("The audio file is empty.", param="file")
        model = await _model_for(caller, parsed.model, operation)
        ledger.model_id = model.id
        if file_part is not None:
            source = await ingest.commit()
        else:
            source = await _file_source(request, caller, str(parsed.file_id))
        spec = audio_jobs.JobSpec.for_request(
            project_id=caller.project_id,
            sha256=source.sha256,
            language=parsed.language,
            response_format=parsed.response_format,
        )
        joined = await jobs.attach(spec.key(), caller.project_id)
        if joined is None:
            # Before the status line: a decode that could not be reserved now
            # is a real 503 Retry-After 60, not a job that fails later.
            await jobs.preflight_decode(source)
        job = await jobs.start(spec, source)
        source = None  # the job owns (or has discarded) it now
    except disk_ledger.DiskFull as full:
        await ledger.nothing_ran()
        raise full.api_error() from None
    except BaseException:
        await ledger.nothing_ran()
        raise
    finally:
        await ingest.abort()
        if source is not None and source.owned:
            with contextlib.suppress(FileNotFoundError):
                await asyncio.to_thread(os.unlink, source.path)
    _rt().name_run(request, job_key=_run_key(job.key, parsed.response_format))
    settlement = _Settlement(ledger, parsed, joined=joined is not None)
    settlement.settle_when_finished(job)
    return _transcription_response(request, job, parsed, settlement=settlement, tag=tag, start_after=0)


async def _reattach_transcription(request: Request, caller: ApiCaller, tag: Any) -> Response:
    """A v1-gateway re-attach (`X-TechSara-Attach-Job`, trusted peer only,
    empty body): follow the job this project started, from where the gateway
    left off. Not counted again and not metered again — it is the same client
    call. A job this process does not hold, running or stored, is a 404, which
    the gateway treats as final."""
    parsed_key = _parse_run_key(str(tag.attach_job))
    job = None
    if parsed_key is not None:
        job = await audio_jobs.jobs().attach(parsed_key[0], caller.project_id)
    if job is None or parsed_key is None:
        raise errors.response_not_found()
    from . import registry

    request_shape = endpoint_models.TranscriptionRequest(
        model=registry.TECHSARA_WHISPER,
        response_format=parsed_key[1],
        stream=tag.resume_after is not None,
    )
    _rt().name_run(request, job_key=_run_key(job.key, parsed_key[1]))
    return _transcription_response(
        request, job, request_shape, settlement=None, tag=tag, start_after=int(tag.resume_after or 0)
    )


@dataclass
class _Settlement:
    """One transcription request's ledger write, exactly once, when its job
    ends — inline when the response saw the end, or from a background wait
    when the client left first (the job runs on for its orphan grace)."""

    ledger: "_Ledger"
    request: endpoint_models.TranscriptionRequest
    joined: bool = False
    started: float = field(default_factory=time.perf_counter)

    _write: Optional["asyncio.Future[None]"] = None

    async def settle(self, job: audio_jobs.AudioJob) -> None:
        """Write the ledger once. Every caller awaits the SAME write, so the
        response that saw the end never returns before its row exists, even
        when the background waiter started the write first."""
        if self._write is None:
            self._write = asyncio.ensure_future(self._settle(job))
        await asyncio.shield(self._write)

    async def _settle(self, job: audio_jobs.AudioJob) -> None:
        if self.ledger.settled:
            return
        result = job.result
        if job.state == "done" and result is not None:
            dispatch = result.report.get("dispatch") if isinstance(result.report, Mapping) else None
            await self.ledger.completed(
                # Whisper reports no tokens: NOT MEASURED, never 0, in
                # usage_events; the token ledgers get 0 so they stay token-true.
                input_tokens=None,
                output_tokens=None,
                quota_input=0,
                meta={
                    "audio_seconds": int(result.usage_seconds),
                    "processing_ms": result.report.get("engine_ms") if isinstance(result.report, Mapping) else None,
                    "response_format": self.request.response_format,
                    "language_forced": self.request.language is not None,
                    "windows": result.report.get("windows") if isinstance(result.report, Mapping) else None,
                    "engine_calls": (dispatch or {}).get("engine_calls") if isinstance(dispatch, Mapping) else None,
                    **({"joined": True} if self.joined else {}),
                },
            )
            return
        error = job.error or errors.internal_error()
        # The decoder or an engine did work unless the job never left its queue.
        calls = 0 if (error.code in ("invalid_request_error", "request_too_large") and not self.joined) else 1
        await self.ledger.failed(
            sidecars.SidecarError(error, engine_calls=calls),
            token_counted=False,
            meta={"response_format": self.request.response_format},
        )

    def settle_when_finished(self, job: audio_jobs.AudioJob) -> None:
        async def wait() -> None:
            try:
                await job.settled()
                await self.settle(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - metering never fails a transcript
                log.warning("a transcription was not metered", exc_info=True)

        task = asyncio.ensure_future(wait())
        _SETTLING.add(task)
        task.add_done_callback(_SETTLING.discard)


def _transcription_response(
    request: Request,
    job: audio_jobs.AudioJob,
    parsed: endpoint_models.TranscriptionRequest,
    *,
    settlement: Optional[_Settlement],
    tag: Any,
    start_after: int,
) -> Response:
    if parsed.stream:
        from . import gateway_protocol, streaming

        async def frames() -> AsyncIterator[str]:
            async for chunk in audio_jobs.sse_stream(
                job, heartbeat_s=keepalive.heartbeat_s(), start_after=start_after
            ):
                yield chunk.decode("utf-8")
            if settlement is not None and job.terminal:
                await settlement.settle(job)

        body: AsyncIterator[str] = frames()
        if getattr(tag, "tagged", False):
            body = gateway_protocol.tag_frames(body, start_after=start_after)
        return StreamingResponse(body, media_type="text/event-stream", headers=streaming.SSE_HEADERS)

    async def work() -> Response:
        try:
            result = await job.wait()
        finally:
            if settlement is not None and job.terminal:
                await settlement.settle(job)
        body = endpoint_models.transcription_body(result, parsed)
        if isinstance(body, str):
            return Response(content=body, media_type="text/plain; charset=utf-8")
        return await _json_response(body)

    response_class = _CommittedText if parsed.response_format == "text" else keepalive.CommittedJSONResponse
    return _committed(request, work, response_class=response_class)


# ---------------------------------------------------------- registration --

_ROUTES = (
    ("/embeddings", "create_embedding", "createEmbedding", create_embedding),
    ("/rerank", "create_rerank", "createRerank", create_rerank),
    ("/audio/transcriptions", "create_transcription", "createTranscription", create_transcription),
)


def register(router: APIRouter) -> None:
    """Add the three routes to the public router. Idempotent.

    Called once from the bottom of `router.py`; a second call (a test that
    registers explicitly, a reload) adds nothing, because two routes on one
    path would make the first one's handler the only one that ever runs and
    the second a silent lie in the route table.
    """
    prefix = router.prefix or ""
    existing = {
        (getattr(route, "path", ""), method)
        for route in router.routes
        for method in (getattr(route, "methods", None) or ())
    }
    for suffix, name, operation_id, endpoint in _ROUTES:
        if (prefix + suffix, "POST") in existing:
            continue
        router.add_api_route(
            suffix,
            endpoint,
            methods=["POST"],
            name=name,
            operation_id=operation_id,
            response_class=Response,
        )
