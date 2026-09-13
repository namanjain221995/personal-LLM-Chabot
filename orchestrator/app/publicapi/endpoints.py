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

THE AUDIO BODY IS READ ONCE, INTO MEMORY, UNDER ITS OWN CAP. `request.form()`
would spool a clip over 1 MB to a temporary file (see `multipart.py`), and the
JSON routes' 1 MiB cap would refuse every real recording. The file part lands
in one `bytearray` while the body streams in, and is streamed out to the
engine from that same buffer.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from fastapi import APIRouter, Depends, Request, Response

from ..apiplatform.resolver import ApiCaller
from ..apiplatform.scopes import Scope, ScopeRequirement, requires
from . import endpoint_models, errors, multipart, sidecars

log = logging.getLogger(__name__)

#: CONTRACT §7, one scope per route — data, so the OpenAPI document and the
#: tests read the requirement the handler enforces.
SCOPES: Dict[str, ScopeRequirement] = {
    "create_embedding": requires(Scope.EMBEDDINGS_WRITE),
    "create_rerank": requires(Scope.RERANK_WRITE),
    "create_transcription": requires(Scope.AUDIO_WRITE),
}

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

#: Above this, JSON is decoded in a worker thread: a 1 MiB body of 256 inputs
#: decoded on the event loop stalls every chat stream in the process for the
#: length of the parse (the 2026-09-05 Fast-mode lesson: TTFT 0.7 → 11.7 s was
#: the orchestrator's own loop).
_OFF_LOOP_JSON_BYTES = 64 * 1024

KIND_SYNC = "sync"


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


async def _read_json(request: Request) -> Any:
    """The JSON body under the §12 cap, counted while it arrives."""
    limit = endpoint_models.max_json_body_bytes()
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
    if not raw.strip():
        raise errors.invalid_request("The request body must be a JSON object.")
    try:
        if len(raw) > _OFF_LOOP_JSON_BYTES:
            return await asyncio.to_thread(json.loads, raw)
        return json.loads(raw)
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


async def create_embedding(request: Request, caller: ApiCaller = Depends(_caller)) -> Response:
    """OpenAI-shaped embeddings on `techsara-embed` (1,024 dimensions)."""
    operation = "create_embedding"
    await _authorize(request, caller, operation)
    parsed: Optional[endpoint_models.EmbeddingsRequest] = None
    deferred: Optional[errors.ApiError] = None
    estimate = 0
    try:
        parsed = endpoint_models.parse_embeddings_request(await _read_json(request))
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
        outcome = await sidecars.embed(inputs, single_string=isinstance(parsed.input, str))
    except sidecars.SidecarError as failure:
        await ledger.failed(failure, meta={"inputs": len(parsed.inputs()) if parsed else 0})
        raise failure.error from None
    except BaseException:
        await ledger.nothing_ran()
        raise

    base64_wanted = parsed.encoding_format == "base64"

    def build() -> Dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "index": index,
                    "embedding": sidecars.encode_base64(vector) if base64_wanted else vector,
                }
                for index, vector in enumerate(outcome.vectors)
            ],
            "model": model.id,
            "usage": (
                None
                if outcome.prompt_tokens is None
                else {"prompt_tokens": outcome.prompt_tokens, "total_tokens": outcome.prompt_tokens}
            ),
        }

    body = await asyncio.to_thread(build) if base64_wanted else build()
    await ledger.completed(
        input_tokens=outcome.prompt_tokens,
        # 0, not None: a pooling pass generates nothing, and that is measured.
        output_tokens=0,
        quota_input=outcome.prompt_tokens,
        meta={"inputs": len(inputs), "engine_calls": outcome.engine_calls},
    )
    return await _json_response(body)


async def create_rerank(request: Request, caller: ApiCaller = Depends(_caller)) -> Response:
    """Cohere/Jina-shaped reranking on `techsara-rerank`."""
    operation = "create_rerank"
    await _authorize(request, caller, operation)
    parsed: Optional[endpoint_models.RerankRequest] = None
    deferred: Optional[errors.ApiError] = None
    estimate = 0
    try:
        parsed = endpoint_models.parse_rerank_request(await _read_json(request))
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
        outcome = await sidecars.rerank_scores(parsed.query, texts, instruction=parsed.instruction)
    except sidecars.SidecarError as failure:
        await ledger.failed(failure, meta={"documents": len(parsed.texts()) if parsed else 0})
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
        },
    )
    return await _json_response(body)


async def create_transcription(request: Request, caller: ApiCaller = Depends(_caller)) -> Response:
    """OpenAI-shaped speech-to-text on `techsara-whisper`, multipart/form-data."""
    operation = "create_transcription"
    path = PATHS[operation]
    await _authorize(request, caller, operation)
    # Counted BEFORE the body is read, unlike the JSON routes: there is no
    # token estimate to reserve (whisper reports no tokens), and a refused
    # request should not have made this process hold 26 MiB first.
    reservation = await _admit(request, caller, estimated_input_tokens=0)
    ledger = _Ledger(caller, reservation, operation, _request_id(request))
    form_request: Optional[endpoint_models.TranscriptionRequest] = None
    audio: Optional[multipart.FilePart] = None
    try:
        _refuse_idempotency_key(request, path)
        form = await multipart.read_form(
            request.stream(),
            request.headers.get("content-type"),
            max_body_bytes=endpoint_models.max_audio_body_bytes(),
            max_file_bytes=endpoint_models.max_audio_bytes(),
            declared_length=request.headers.get("content-length"),
        )
        form_request = endpoint_models.parse_transcription_form(form.fields)
        audio = form.file("file")
        if audio is None:
            raise errors.invalid_request("file is required.", param="file")
        if audio.content_type not in sidecars.allowed_audio_types():
            raise errors.invalid_request(
                "The file's Content-Type must be a supported audio type, such as audio/mpeg, "
                "audio/wav, audio/webm or audio/mp4.",
                param="file",
            )
        if audio.size == 0:
            raise errors.invalid_request("The audio file is empty.", param="file")
        model = await _model_for(caller, form_request.model, operation)
        ledger.model_id = model.id
        outcome = await sidecars.transcribe(
            audio.data,
            content_type=audio.content_type,
            language=form_request.language,
            verbose=form_request.verbose,
        )
        limit = endpoint_models.max_audio_seconds()
        if outcome.duration_s is not None and outcome.duration_s > limit + 0.5:
            # The probe could not read this container from a pipe, so the
            # engine measured it. Refused anyway: a documented ceiling that
            # holds only for the formats ffprobe can read is not a ceiling.
            raise sidecars.SidecarError(sidecars.audio_too_long(limit), engine_calls=1)
    except sidecars.SidecarError as failure:
        await ledger.failed(
            failure,
            token_counted=False,
            meta={"response_format": getattr(form_request, "response_format", None)},
        )
        raise failure.error from None
    except BaseException:
        await ledger.nothing_ran()
        raise
    finally:
        # The audio is dropped when the request ends, whatever happened —
        # nothing about it is kept (CONTRACT §16).
        if audio is not None:
            audio.data = bytearray()

    body = endpoint_models.transcription_body(outcome.reply, form_request)
    await ledger.completed(
        # Whisper reports no tokens: NOT MEASURED, never 0, in usage_events;
        # the token ledgers get 0 so they stay token-true.
        input_tokens=None,
        output_tokens=None,
        quota_input=0,
        meta={
            "audio_seconds": outcome.duration_s,
            "processing_ms": outcome.processing_ms,
            "response_format": form_request.response_format,
            "language_forced": form_request.language is not None,
        },
    )
    if isinstance(body, str):
        return Response(content=body, media_type="text/plain; charset=utf-8")
    return await _json_response(body)


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
