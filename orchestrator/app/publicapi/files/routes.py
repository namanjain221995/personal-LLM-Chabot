"""`/v1/files` and `/v1/uploads` — the route handlers, as a factory.

THE SAME ORDER AS EVERY OTHER `/v1` ROUTE (CONTRACT §4, design §2):

    resolve the caller (Bearer only, before any body byte is read)
      → scope, then origin → count the request once (admit)
      → refuse an Idempotency-Key → validate → work → one usage record → answer

WHY A FACTORY WITH ITS DEPENDENCIES AS PARAMETERS. Authentication, scopes,
admission and usage recording belong to `publicapi/router.py`,
`apiplatform/scopes.py` and `apiplatform/quotas.py`, which other engineers own
this wave (and `Scope` has no `files.*` members yet). `FilesDependencies`
carries them in, so the integration is one call —
`register(router.router, router_dependencies())` — and the tests and the SDK
parity run build the SAME handlers with stub auth.

WHAT NEVER HAPPENS HERE.
* No `request.form()`: bodies stream to disk (`multipart_disk`, `partfile`).
* No existence disclosure: absent, malformed, deleted, expired and another
  project's ids get one 404 body, after the same single indexed lookup.
* No synchronous byte copy in `complete` (finding #1): it does O(parts) row
  work, creates the file, answers 200 with the nested file, and the
  `assemble` stage does the bytes.
* No wall clock (no-timeout design): nothing here waits on a timer except the
  2 s a second `complete` gives the winner, which stays far inside the 15 s
  first-byte invariant.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import functools
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Protocol, Tuple

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from ... import db
from ...apifiles import accounting, ids, limits, queue, schema, storage
from ...core import partfile
from .. import errors
from . import content, multipart_disk, wire

log = logging.getLogger(__name__)

SCOPE_READ = "files.read"
SCOPE_WRITE = "files.write"

KIND_READ = "read"
KIND_SYNC = "sync"
KIND_STREAM = "stream"


# ------------------------------------------------------------ dependencies --


@dataclass(frozen=True)
class RouteUsage:
    """One request's usage record (design §11). `status` is `ok` or `error`;
    `reservation` is whatever `admit` returned, for the quota settlement."""

    route: str
    generation_id: str
    caller: Any
    request_id: str
    status: str
    error_code: str
    duration_ms: int
    meta: Dict[str, Any]
    reservation: Any = None
    http_status: int = 200


class DerivedProvider(Protocol):
    """Team B's `apifiles.derived`: the closed per-kind allowlist."""

    def list_names(self, blob_row: Mapping[str, Any]) -> List[dict]: ...

    def open_name(self, blob_row: Mapping[str, Any], name: str) -> Tuple[str, str, int]: ...


async def _no_admit(request: Request, caller: Any, kind: str) -> Any:
    return None


async def _no_usage(record: RouteUsage) -> None:
    return None


def _state_request_id(request: Request) -> str:
    return str(getattr(request.state, "public_request_id", "") or "")


#: Re-exported: the purge primitive lives beside `ingest_single` in
#: `apifiles.queue`, where team B's `retention.purge_blob` can call it without
#: importing the HTTP layer.
purge_blob_bytes = queue.purge_blob_bytes


async def purge_blob_local(blob: Mapping[str, Any]) -> None:
    """The default `FilesDependencies.purge_blob`: `purge_blob_bytes` off the
    event loop. Team B's `retention.purge_blob` replaces it and adds the job
    cancellation and the video analysis cleanup (design §2.7 step 2)."""
    await db.run_in_thread(purge_blob_bytes, str(blob["id"]))


@dataclass
class FilesDependencies:
    """What the handlers need from the rest of the platform.

    * `resolve_caller(request) -> caller` — a FastAPI dependency; the caller
      has `project_id`, `workspace_id`, `key_id`. Raise `ApiError` to refuse.
    * `authorize(request, caller, scope)` — `scope` is `files.read` or
      `files.write`; checks the scope, then the origin.
    * `admit(request, caller, kind)` — count once; returns a reservation.
    * `record_usage(RouteUsage)` — one row per request; must not raise.
    * `processing_view(file_row) -> {status, status_details, processing}` —
      team B's `jobs.processing_view`; default renders the row's columns.
    * `purge_blob(blob_row)` — team B's `retention.purge_blob`.
    * `assembler` — the runner to wake after `complete`.
    * `derived` / `events` — routes 7–8 and 6 are registered only when given.
    """

    resolve_caller: Callable[[Request], Awaitable[Any]]
    authorize: Callable[[Request, Any, str], Awaitable[None]]
    admit: Callable[[Request, Any, str], Awaitable[Any]] = _no_admit
    record_usage: Callable[[RouteUsage], Awaitable[None]] = _no_usage
    request_id: Callable[[Request], str] = _state_request_id
    processing_view: Optional[wire.ProcessingView] = None
    purge_blob: Callable[[Mapping[str, Any]], Awaitable[None]] = purge_blob_local
    assembler: Optional[queue.AssembleRunner] = None
    derived: Optional[DerivedProvider] = None
    events: Optional[Callable[[Request, Any, dict], Awaitable[Response]]] = None
    background_tasks: set = field(default_factory=set)
    #: blob id → the ONE purge task this process runs for it. A DELETE starts
    #: it; a re-upload of the same bytes waits on it instead of starting a
    #: second purge (review finding, 2026-09-13: two purges of one blob, the
    #: later one rmtree-ing the re-uploaded copy).
    purges_in_flight: Dict[str, "asyncio.Task[None]"] = field(default_factory=dict)


def router_dependencies(*, assembler: Optional[queue.AssembleRunner] = None) -> FilesDependencies:
    """The dependencies of the real `/v1` router (integration seam).

    Needs `Scope.FILES_READ` / `Scope.FILES_WRITE` in `apiplatform/scopes.py`
    (values `files.read` / `files.write`); until they exist `Scope(value)`
    raises and every file route answers 500, which is why this is not called
    anywhere yet."""
    from ...apiplatform.scopes import Scope, requires
    from .. import router as public_router

    async def resolve(request: Request) -> Any:
        return await public_router.resolve_caller(request)

    async def authorize(request: Request, caller: Any, scope: str) -> None:
        await public_router.authorize_scope(request, caller, requires(Scope(scope)))

    async def admit(request: Request, caller: Any, kind: str) -> Any:
        return await public_router.admit(request, caller, kind=kind, max_output_tokens=1)

    async def record(usage: RouteUsage) -> None:
        from ... import usage as usage_ledger
        from ...apiplatform import quotas

        await usage_ledger.record_async(
            user_id=None,
            workspace_id=getattr(usage.caller, "workspace_id", None) or None,
            conversation_id=None,
            generation_id=usage.generation_id,
            route=usage.route,
            model="",
            mode="api",
            duration_ms=usage.duration_ms,
            status=usage_ledger.OK if usage.status == "ok" else usage_ledger.ERROR,
            error_kind=usage.error_code,
            meta={
                "api_key_id": getattr(usage.caller, "key_id", None),
                "project_id": getattr(usage.caller, "project_id", None),
                "request_id": usage.request_id,
                **usage.meta,
            },
        )
        if usage.reservation is not None:
            settled = "completed" if usage.status == "ok" else ("failed" if usage.http_status >= 500 else "cancelled")
            try:
                await db.run_in_thread(
                    functools.partial(quotas.record_usage, usage.caller, 0, 0, settled, reservation=usage.reservation)
                )
            except Exception:  # noqa: BLE001
                log.warning("files usage for %s was not settled", usage.generation_id, exc_info=True)

    return FilesDependencies(
        resolve_caller=resolve,
        authorize=authorize,
        admit=admit,
        record_usage=record,
        request_id=public_router.request_id,
        assembler=assembler,
    )


# ----------------------------------------------------------------- helpers --


class _Call:
    """Timing, the generation id and the usage record of one request."""

    def __init__(self, deps: FilesDependencies, request: Request, caller: Any, route: str, reservation: Any) -> None:
        self.deps = deps
        self.request = request
        self.caller = caller
        self.route = route
        self.reservation = reservation
        self.started = time.perf_counter()
        self.generation_id = ""
        self.meta: Dict[str, Any] = {}
        self.done = False

    async def finish(self, failure: Optional[BaseException] = None) -> None:
        if self.done:
            return
        self.done = True
        status, code, http_status = "ok", "", 200
        if failure is not None:
            status = "error"
            if isinstance(failure, errors.ApiError):
                code, http_status = failure.code, failure.status
            elif isinstance(failure, asyncio.CancelledError):
                code, http_status = "cancelled", 499
            else:
                code, http_status = "internal_error", 500
        record = RouteUsage(
            route=self.route,
            generation_id=self.generation_id or ids.new_list_id(),
            caller=self.caller,
            request_id=self.deps.request_id(self.request),
            status=status,
            error_code=code,
            duration_ms=int((time.perf_counter() - self.started) * 1000),
            meta=dict(self.meta),
            reservation=self.reservation,
            http_status=http_status,
        )
        task = asyncio.ensure_future(self.deps.record_usage(record))
        try:
            # Shielded: a client that leaves cancels the handler, and the one
            # usage row of this request must not be cancelled with it.
            await asyncio.shield(task)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - telemetry never fails an answer
            log.warning("files usage record failed", exc_info=True)


def _json(body: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status)


def _refuse_idempotency_key(request: Request) -> None:
    """Parts are idempotent by `part_number`, `complete`/`cancel` by state; a
    key that is accepted and ignored could not be told apart from one that is
    honoured, so it is refused (design §2)."""
    if (request.headers.get("idempotency-key") or "").strip():
        raise wire.invalid_request(
            "Idempotency-Key is not supported on file routes; parts are idempotent by part_number.",
            param="Idempotency-Key",
        )


async def _read_json(request: Request, *, allow_empty: bool) -> Dict[str, Any]:
    limit = limits.json_body_max_bytes()
    declared = wire.ascii_int(request.headers.get("content-length"))
    if declared is not None and declared > limit:
        raise wire.request_too_large(limit)
    raw = bytearray()
    try:
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > limit:
                raise wire.request_too_large(limit)
    except multipart_disk._disconnect_types():  # type: ignore[misc]
        raise wire.incomplete_body() from None
    if not bytes(raw).strip():
        if allow_empty:
            return {}
        raise wire.invalid_request("The request body must be a JSON object.")
    try:
        payload = json.loads(bytes(raw))
    except (ValueError, RecursionError):
        # RecursionError: 100,000 `[` is valid-looking JSON nested past the
        # decoder's recursion limit; it is neither ValueError nor a server
        # fault, and it was a 500 until 2026-09-13 (review).
        raise wire.invalid_request("The request body is not valid JSON.") from None
    if not isinstance(payload, dict):
        raise wire.invalid_request("The request body must be a JSON object.")
    return payload


def _unknown_keys(payload: Mapping[str, Any], allowed: Tuple[str, ...]) -> None:
    unknown = sorted(key for key in payload if key not in allowed)
    if unknown:
        raise wire.invalid_request(f"Unsupported field: {unknown[0]}.", param=unknown[0])


def _storage_error(exc: storage.StorageUnavailable) -> wire.FilesApiError:
    return wire.storage_unavailable(exc.retry_after)


async def _require_free(extra_bytes: int) -> None:
    try:
        await db.run_in_thread(storage.require_free, int(extra_bytes))
    except storage.StorageUnavailable as exc:
        raise _storage_error(exc) from None


def _declared_length(request: Request) -> Optional[int]:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    value = wire.ascii_int(raw)
    if value is None:
        raise wire.invalid_request("Content-Length must be a non-negative integer.", param="Content-Length")
    return value


def _part_number(raw: Any) -> int:
    ceiling = limits.max_parts() - 1
    value = wire.ascii_int(None if raw is None else str(raw))
    if value is None or value > ceiling:
        raise wire.invalid_request(
            f"part_number must be an integer from 0 to {ceiling} (part numbers are 0-based).",
            param="part_number",
        )
    return value


def _sha256_field(raw: Optional[str], *, param: str) -> Optional[str]:
    if raw is None:
        return None
    value = raw.strip().lower()
    if not ids.is_sha256(value):
        raise wire.invalid_request(f"{param} must be 64 hexadecimal characters.", param=param)
    return value


def _declared_part_digest(request: Request) -> Optional[str]:
    """`Content-Digest: sha-256=:<base64>:` (RFC 9530) or `X-Part-SHA256: <hex>`."""
    hex_value = _sha256_field(request.headers.get("x-part-sha256"), param="X-Part-SHA256")
    digest_header = request.headers.get("content-digest")
    from_digest = None
    if digest_header:
        for item in digest_header.split(","):
            name, _, value = item.strip().partition("=")
            if name.strip().lower() != "sha-256":
                continue
            value = value.strip()
            if not (value.startswith(":") and value.endswith(":")):
                raise wire.invalid_request("Content-Digest sha-256 must be :base64:.", param="Content-Digest")
            try:
                raw = base64.b64decode(value[1:-1], validate=True)
            except (binascii.Error, ValueError):
                raise wire.invalid_request("Content-Digest sha-256 is not valid base64.", param="Content-Digest") from None
            if len(raw) != 32:
                raise wire.invalid_request("Content-Digest sha-256 must be 32 bytes.", param="Content-Digest")
            from_digest = raw.hex()
    if hex_value and from_digest and hex_value != from_digest:
        raise wire.invalid_request("X-Part-SHA256 and Content-Digest disagree.", param="Content-Digest")
    return hex_value or from_digest


def _part_outcome(outcome: schema.PartOutcome) -> Dict[str, Any]:
    if outcome.state == "ok":
        return wire.part_object(outcome.part or {}, str((outcome.upload or {}).get("id")))
    if outcome.state == "gone":
        raise wire.upload_not_found()
    if outcome.state == "state":
        status = wire.upload_status(outcome.upload or {})
        raise wire.upload_state_conflict(f"This upload is {status}; it no longer accepts parts.")
    if outcome.state == "mode":
        mode = (outcome.upload or {}).get("part_mode")
        raise wire.invalid_request(
            f"This upload is {mode}; do not mix numbered and sequential parts.", param="part_number"
        )
    if outcome.state == "budget":
        raise wire.invalid_request("This upload would exceed its part budget.", param="data")
    if outcome.state == "parts_full":
        raise wire.invalid_request(
            f"This upload already has the maximum of {limits.max_parts()} parts.", param="part_number"
        )
    raise errors.internal_error()  # pragma: no cover


def _commit_into(upload_id: str, written: partfile.PartWritten) -> Callable[[int], None]:
    def commit(number: int) -> None:
        partfile.commit_part(written, storage.part_path(upload_id, number))

    return commit


# ---------------------------------------------------------------- handlers --


class _Handlers:
    def __init__(self, deps: FilesDependencies) -> None:
        self.deps = deps

    # -- plumbing ----------------------------------------------------------

    async def _begin(self, request: Request, caller: Any, *, scope: str, kind: str, route: str) -> _Call:
        await self.deps.authorize(request, caller, scope)
        reservation = await self.deps.admit(request, caller, kind)
        call = _Call(self.deps, request, caller, route, reservation)
        try:
            _refuse_idempotency_key(request)
        except BaseException as exc:
            await call.finish(exc)
            raise
        return call

    def _view(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        return wire.file_object(row, processing_view=self.deps.processing_view)

    def _spawn(self, coroutine: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coroutine)
        self.deps.background_tasks.add(task)
        task.add_done_callback(self.deps.background_tasks.discard)

    def _kick_assembler(self) -> None:
        if self.deps.assembler is not None:
            self.deps.assembler.kick()

    async def _file_row(self, caller: Any, file_id: str, *, param: Optional[str] = None) -> dict:
        if not ids.is_file_id(file_id):
            # The same single lookup shape either way (design §7.2 rule 2).
            await db.run_in_thread(schema.get_api_file, caller.project_id, "file-" + "0" * 24)
            raise wire.file_not_found(param)
        row = await db.run_in_thread(schema.get_api_file, caller.project_id, file_id)
        if row is None:
            raise wire.file_not_found(param)
        return row

    async def _upload_row(self, caller: Any, upload_id: str) -> dict:
        lookup = upload_id if ids.is_upload_id(upload_id) else "upload_" + "0" * 24
        row = await db.run_in_thread(schema.get_api_upload, caller.project_id, lookup)
        if row is None or lookup != upload_id:
            raise wire.upload_not_found()
        return row

    # -- 1. POST /v1/files -------------------------------------------------

    async def create_file(self, request: Request, caller: Any) -> Response:
        call = await self._begin(request, caller, scope=SCOPE_WRITE, kind=KIND_SYNC, route="v1_files_create")
        tmp = None
        try:
            content_type = request.headers.get("content-type")
            multipart_disk.boundary_of(content_type)
            declared = _declared_length(request)
            single = limits.single_max_bytes()
            too_large = (
                f"A file sent to /v1/files may be at most {single} bytes; "
                "use /v1/uploads for larger files."
            )
            if declared is not None and declared > limits.max_body_bytes():
                raise wire.request_too_large(single, too_large)
            await _require_free(declared if declared is not None else single)
            await db.run_in_thread(storage.ensure_dirs)
            tmp = storage.single_tmp()
            try:
                form = await multipart_disk.read_form_to_disk(
                    request.stream(), content_type, tmp_path=tmp, file_field="file",
                    max_body_bytes=limits.max_body_bytes(), max_file_bytes=single,
                    declared_length=request.headers.get("content-length"), too_large_message=too_large,
                )
            except multipart_disk.ClientGone:
                raise wire.incomplete_body() from None
            _unknown_keys(form.fields, ("purpose", "expires_after[anchor]", "expires_after[seconds]"))
            if form.file is None:
                raise wire.invalid_request("A file part named `file` is required.", param="file")
            purpose = wire.parse_purpose(form.fields.get("purpose"))
            expires = wire.parse_expires_after(
                form.fields.get("expires_after[anchor]"), form.fields.get("expires_after[seconds]")
            )
            filename = wire.normalize_filename(form.file.filename)
            ingest = functools.partial(
                queue.ingest_single,
                project_id=caller.project_id, workspace_id=caller.workspace_id, key_id=caller.key_id,
                tmp_path=tmp, sha256=form.file.sha256, bytes=form.file.bytes, filename=filename,
                purpose=purpose, expires_after_seconds=expires, mime_hint=form.file.content_type,
            )
            try:
                result = await db.run_in_thread(ingest)
            except schema.BlobBeingPurged as purging:
                # Never a second purge of our own: wait (bounded) on the ONE
                # purge of that blob id, then ingest again. Still purging →
                # a retryable 503; the bytes stay in `_single/` until then
                # and are discarded below.
                await self._await_purge(purging.blob, limits.purge_wait_s())
                try:
                    result = await db.run_in_thread(ingest)
                except schema.BlobBeingPurged:
                    raise wire.storage_busy(2) from None
            call.generation_id = result.file["id"]
            call.meta = {"bytes": form.file.bytes, "kind_hint": result.detection.kind, "deduplicated": not result.created}
            body = self._view(result.file)
        except BaseException as exc:
            storage.discard(tmp)
            await call.finish(exc)
            raise
        await call.finish()
        return _json(body)

    # -- 2. GET /v1/files --------------------------------------------------

    async def list_files(self, request: Request, caller: Any) -> Response:
        call = await self._begin(request, caller, scope=SCOPE_READ, kind=KIND_READ, route="v1_files_list")
        try:
            query = request.query_params
            _unknown_keys(query, ("after", "limit", "order", "purpose"))
            ceiling = limits.list_max_limit()
            raw_limit = query.get("limit")
            parsed_limit = wire.ascii_int(raw_limit)
            if raw_limit is None:
                limit = ceiling
            elif parsed_limit is not None and 1 <= parsed_limit <= ceiling:
                limit = parsed_limit
            else:
                raise wire.invalid_request(f"limit must be an integer from 1 to {ceiling}.", param="limit")
            order = query.get("order") or "desc"
            if order not in ("asc", "desc"):
                raise wire.invalid_request("order must be asc or desc.", param="order")
            purpose = query.get("purpose")
            empty = False
            if purpose is not None and purpose not in wire.PURPOSES:
                if purpose in wire.UNSUPPORTED_PURPOSES:
                    empty = True
                else:
                    raise wire.invalid_request("purpose is not a known file purpose.", param="purpose")
            after = query.get("after")
            unknown_cursor = wire.invalid_request("after names an unknown cursor.", param="after")
            if after is not None and not ids.is_file_id(after):
                raise unknown_cursor
            if empty:
                rows, has_more = [], False
            else:
                try:
                    rows, has_more = await db.run_in_thread(
                        functools.partial(
                            schema.list_api_files, caller.project_id, after=after, limit=limit, order=order, purpose=purpose
                        )
                    )
                except schema.UnknownCursor:
                    raise unknown_cursor from None
            data = [self._view(row) for row in rows]
            call.generation_id = ids.new_list_id()
            call.meta = {"returned": len(data), "limit": limit}
        except BaseException as exc:
            await call.finish(exc)
            raise
        await call.finish()
        return _json(wire.list_object(data, has_more))

    # -- 3. GET /v1/files/{file_id} ----------------------------------------

    async def retrieve_file(self, request: Request, file_id: str, caller: Any) -> Response:
        call = await self._begin(request, caller, scope=SCOPE_READ, kind=KIND_READ, route="v1_files_get")
        try:
            row = await self._file_row(caller, file_id)
            call.generation_id = row["id"]
            body = self._view(row)
        except BaseException as exc:
            await call.finish(exc)
            raise
        await call.finish()
        return _json(body)

    # -- 4. GET /v1/files/{file_id}/content --------------------------------

    async def file_content(self, request: Request, file_id: str, caller: Any) -> Response:
        call = await self._begin(request, caller, scope=SCOPE_READ, kind=KIND_READ, route="v1_files_content")
        try:
            row = await self._file_row(caller, file_id)
            call.generation_id = row["id"]
            if row.get("error_code"):
                raise wire.invalid_request(
                    "This file has no content: " + wire.default_processing_view(row)["status_details"],
                    param="file_id",
                )
            if row.get("blob_id") is None or not row.get("blob_sha256"):
                raise wire.file_not_ready(5, "This file's bytes are still being assembled. Retry after the Retry-After interval.")
            path = storage.original_path(caller.project_id, row["blob_sha256"])
            response, sent, span = await db.run_in_thread(
                functools.partial(
                    content.file_response, path, filename=row["filename"], etag=row["blob_sha256"],
                    range_header=request.headers.get("range"), if_none_match=request.headers.get("if-none-match"),
                )
            )
            call.meta = {"bytes_sent": sent, "range": None if span is None else f"{span[0]}-{span[1]}"}
        except BaseException as exc:
            await call.finish(exc)
            raise
        await call.finish()
        return response

    # -- 5. DELETE /v1/files/{file_id} -------------------------------------

    async def delete_file(self, request: Request, file_id: str, caller: Any) -> Response:
        call = await self._begin(request, caller, scope=SCOPE_WRITE, kind=KIND_SYNC, route="v1_files_delete")
        try:
            if not ids.is_file_id(file_id):
                raise wire.file_not_found()
            outcome = await db.run_in_thread(schema.delete_api_file, caller.project_id, file_id)
            if outcome is None:
                raise wire.file_not_found()
            tomb, blob = outcome
            call.generation_id = tomb["id"]
            call.meta = {"bytes": int(tomb.get("bytes") or 0), "blob_purged": blob is not None}
            if blob is not None:
                self._ensure_purge(blob)
            if tomb.get("old_assembling_upload_id"):
                self._spawn(self._drop_assembly(str(tomb["old_assembling_upload_id"])))
        except BaseException as exc:
            await call.finish(exc)
            raise
        await call.finish()
        return _json(wire.deleted_object(file_id))

    async def _purge(self, blob: Mapping[str, Any]) -> None:
        try:
            await self.deps.purge_blob(blob)
        except Exception:  # noqa: BLE001 - retention's reconciliation retries
            log.warning("purge of blob %s failed; reconciliation will retry", blob.get("id"), exc_info=True)

    def _ensure_purge(self, blob: Mapping[str, Any]) -> "asyncio.Task[None]":
        """The in-flight purge of `blob`'s id, started if there is none."""
        blob_id = str(blob.get("id") or "")
        running = self.deps.purges_in_flight.get(blob_id)
        if running is not None and not running.done():
            return running
        task = asyncio.ensure_future(self._purge(blob))
        self.deps.purges_in_flight[blob_id] = task
        self.deps.background_tasks.add(task)

        def forget(done: "asyncio.Task[None]") -> None:
            self.deps.background_tasks.discard(done)
            if self.deps.purges_in_flight.get(blob_id) is done:
                del self.deps.purges_in_flight[blob_id]

        task.add_done_callback(forget)
        return task

    async def _await_purge(self, blob: Mapping[str, Any], wait_s: float) -> None:
        """Wait up to `wait_s` for the purge of `blob`; never cancels it (a
        client that leaves must not abort another request's DELETE)."""
        task = self._ensure_purge(blob)
        await asyncio.wait({task}, timeout=max(0.0, float(wait_s)))

    async def _drop_assembly(self, upload_id: str) -> None:
        def work() -> None:
            if queue.clear_abandoned_assembly(upload_id):
                storage.remove_tree(storage.upload_dir(upload_id))

        try:
            await db.run_in_thread(work)
        except Exception:  # noqa: BLE001 - the sweep retries
            log.warning("could not drop the assembly of %s", upload_id, exc_info=True)

    # -- 7/8. derived --------------------------------------------------------

    async def list_derived(self, request: Request, file_id: str, caller: Any) -> Response:
        call = await self._begin(request, caller, scope=SCOPE_READ, kind=KIND_READ, route="v1_files_derived")
        try:
            row = await self._file_row(caller, file_id)
            call.generation_id = row["id"]
            if row.get("blob_status") != "processed":
                raise wire.file_not_ready(5)
            blob = await db.run_in_thread(schema.get_api_file_blob, row["blob_id"])
            names = self.deps.derived.list_names(blob or {})  # type: ignore[union-attr]
        except BaseException as exc:
            await call.finish(exc)
            raise
        await call.finish()
        return _json({"object": "list", "data": names})

    async def download_derived(self, request: Request, file_id: str, name: str, caller: Any) -> Response:
        call = await self._begin(request, caller, scope=SCOPE_READ, kind=KIND_READ, route="v1_files_derived")
        try:
            row = await self._file_row(caller, file_id)
            call.generation_id = row["id"]
            if row.get("blob_status") != "processed":
                raise wire.file_not_ready(5)
            blob = await db.run_in_thread(schema.get_api_file_blob, row["blob_id"])
            try:
                path, media_type, _size = self.deps.derived.open_name(blob or {}, name)  # type: ignore[union-attr]
            except (KeyError, LookupError, ValueError):
                raise wire.file_not_found(param="name") from None
            response, sent, span = await db.run_in_thread(
                functools.partial(
                    content.file_response, path, filename=name, etag=None, media_type=media_type,
                    range_header=request.headers.get("range"), missing=wire.file_not_found(param="name"),
                )
            )
            call.meta = {"name": name, "bytes_sent": sent}
        except BaseException as exc:
            await call.finish(exc)
            raise
        await call.finish()
        return response

    async def file_events(self, request: Request, file_id: str, caller: Any) -> Response:
        call = await self._begin(request, caller, scope=SCOPE_READ, kind=KIND_STREAM, route="v1_files_events")
        try:
            row = await self._file_row(caller, file_id)
            call.generation_id = row["id"]
            response = await self.deps.events(request, caller, row)  # type: ignore[misc]
        except BaseException as exc:
            await call.finish(exc)
            raise
        await call.finish()
        return response

    # -- 9. POST /v1/uploads -----------------------------------------------

    async def create_upload(self, request: Request, caller: Any) -> Response:
        call = await self._begin(request, caller, scope=SCOPE_WRITE, kind=KIND_SYNC, route="v1_uploads_create")
        try:
            payload = await _read_json(request, allow_empty=False)
            _unknown_keys(payload, ("bytes", "filename", "mime_type", "purpose", "expires_after"))
            size = payload.get("bytes")
            ceiling = limits.upload_max_bytes()
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise wire.invalid_request("bytes must be a non-negative integer.", param="bytes")
            if size > ceiling:
                raise wire.invalid_request(
                    f"An upload may hold at most {ceiling} bytes ({ceiling / 1024 ** 3:g} GiB).", param="bytes"
                )
            raw_name = payload.get("filename")
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise wire.invalid_request("filename is required.", param="filename")
            filename = wire.normalize_filename(raw_name)
            mime_type = payload.get("mime_type")
            if not isinstance(mime_type, str) or not mime_type.strip() or len(mime_type) > 255 or not wire.is_encodable(mime_type):
                raise wire.invalid_request("mime_type is required (at most 255 characters).", param="mime_type")
            purpose = wire.parse_purpose(payload.get("purpose"))
            expires = None
            if payload.get("expires_after") is not None:
                block = payload["expires_after"]
                if not isinstance(block, dict):
                    raise wire.invalid_request("expires_after must be an object.", param="expires_after")
                _unknown_keys(block, ("anchor", "seconds"))
                if not isinstance(block.get("seconds"), int) or isinstance(block.get("seconds"), bool):
                    raise wire.invalid_request("expires_after.seconds must be an integer.", param="expires_after.seconds")
                expires = wire.parse_expires_after(block.get("anchor"), block.get("seconds"))
            # Parts plus the assembled copy (design §2.10).
            await _require_free(2 * size)
            row = await db.run_in_thread(
                functools.partial(
                    schema.create_api_upload, caller.project_id, caller.workspace_id, caller.key_id,
                    filename, purpose, mime_type.strip(), size, expires,
                    idle_ttl_s=limits.upload_idle_ttl_s(), max_ttl_s=limits.upload_max_ttl_s(),
                )
            )
            await db.run_in_thread(storage.makedirs, storage.parts_dir(row["id"]))
            call.generation_id = row["id"]
            call.meta = {"bytes": size}
        except BaseException as exc:
            await call.finish(exc)
            raise
        await call.finish()
        return _json(wire.upload_object(row))

    # -- 10. GET /v1/uploads/{upload_id} -----------------------------------

    async def retrieve_upload(self, request: Request, upload_id: str, caller: Any) -> Response:
        call = await self._begin(request, caller, scope=SCOPE_WRITE, kind=KIND_READ, route="v1_uploads_get")
        try:
            row = await self._upload_row(caller, upload_id)
            call.generation_id = row["id"]
            parts = await db.run_in_thread(schema.list_api_upload_parts, caller.project_id, upload_id)
            file_body = None
            if row.get("file_id"):
                file_row = await db.run_in_thread(schema.get_api_file, caller.project_id, row["file_id"])
                file_body = self._view(file_row) if file_row else None
            body = wire.upload_object(row, file=file_body, parts=parts)
        except BaseException as exc:
            await call.finish(exc)
            raise
        await call.finish()
        return _json(body)

    # -- 11. POST /v1/uploads/{upload_id}/parts ----------------------------

    async def add_part(self, request: Request, upload_id: str, caller: Any) -> Response:
        call = await self._begin(request, caller, scope=SCOPE_WRITE, kind=KIND_SYNC, route="v1_uploads_part")
        tmp = None
        try:
            row = await self._upload_row(caller, upload_id)
            call.generation_id = row["id"]
            if row["status"] != "pending" or row.get("lapsed"):
                raise wire.upload_state_conflict(f"This upload is {wire.upload_status(row)}; it no longer accepts parts.")
            content_type = request.headers.get("content-type")
            multipart_disk.boundary_of(content_type)
            declared = _declared_length(request)
            part_max = limits.part_max_bytes()
            too_large = f"A part may be at most {part_max} bytes."
            if declared is not None and declared > limits.max_body_bytes():
                raise wire.request_too_large(part_max, too_large)
            if declared == 0:
                raise wire.invalid_request(f"A part must be 1 to {part_max} bytes.", param="data")
            await _require_free(declared if declared is not None else part_max)
            tmp = storage.incoming_part_tmp_path(upload_id)
            with accounting.PartInFlight():
                try:
                    form = await multipart_disk.read_form_to_disk(
                        request.stream(), content_type, tmp_path=tmp, file_field="data",
                        max_body_bytes=limits.max_body_bytes(), max_file_bytes=part_max,
                        declared_length=request.headers.get("content-length"), too_large_message=too_large,
                    )
                except multipart_disk.ClientGone:
                    raise wire.incomplete_body() from None
            _unknown_keys(form.fields, ("part_number", "sha256"))
            if form.file is None:
                raise wire.invalid_request("A file part named `data` is required.", param="data")
            if form.file.bytes == 0:
                raise wire.invalid_request(f"A part must be 1 to {part_max} bytes.", param="data")
            number = _part_number(form.fields["part_number"]) if "part_number" in form.fields else None
            declared_sha = _sha256_field(form.fields.get("sha256"), param="sha256")
            if declared_sha is not None and declared_sha != form.file.sha256:
                raise wire.checksum_mismatch("sha256")
            written = partfile.PartWritten(path=tmp, bytes=form.file.bytes, sha256=form.file.sha256)
            outcome = await db.run_in_thread(
                functools.partial(
                    schema.upsert_api_upload_part, caller.project_id, upload_id,
                    part_number=number, bytes=form.file.bytes, sha256=form.file.sha256,
                    mode="numbered" if number is not None else "sequential",
                    part_max_bytes=part_max, max_parts=limits.max_parts(), upload_max_bytes=limits.upload_max_bytes(),
                    idle_ttl_s=limits.upload_idle_ttl_s(), max_ttl_s=limits.upload_max_ttl_s(),
                    before_commit=_commit_into(upload_id, written),
                )
            )
            body = _part_outcome(outcome)
            call.meta = {"part_number": body["part_number"], "bytes": body["bytes"], "route": "multipart"}
        except BaseException as exc:
            storage.discard(tmp)
            await call.finish(exc)
            raise
        storage.discard(tmp)
        await call.finish()
        return _json(body)

    # -- 12. PUT /v1/uploads/{upload_id}/parts/{part_number} ---------------

    async def put_part(self, request: Request, upload_id: str, part_number: str, caller: Any) -> Response:
        call = await self._begin(request, caller, scope=SCOPE_WRITE, kind=KIND_SYNC, route="v1_uploads_part")
        written: Optional[partfile.PartWritten] = None
        try:
            row = await self._upload_row(caller, upload_id)
            call.generation_id = row["id"]
            number = _part_number(part_number)
            if row["status"] != "pending" or row.get("lapsed"):
                raise wire.upload_state_conflict(f"This upload is {wire.upload_status(row)}; it no longer accepts parts.")
            declared = _declared_length(request)
            part_max = limits.part_max_bytes()
            if declared is None:
                raise wire.length_required()
            if declared > part_max:
                raise wire.request_too_large(part_max, f"A part may be at most {part_max} bytes.")
            if declared == 0:
                raise wire.invalid_request(f"A part must be 1 to {part_max} bytes.", param="data")
            digest = _declared_part_digest(request)
            await _require_free(declared)
            final = storage.part_path(upload_id, number)
            with accounting.PartInFlight():
                try:
                    written = await partfile.write_part_stream(
                        request.stream(), final_path=final, cap_bytes=part_max,
                        declared_sha256=digest, expected_bytes=declared,
                    )
                except partfile.PartTooLarge:
                    raise wire.request_too_large(part_max, f"A part may be at most {part_max} bytes.") from None
                except partfile.PartDigestMismatch:
                    raise wire.checksum_mismatch("sha256") from None
                except partfile.PartIncomplete:
                    raise wire.incomplete_body() from None
            outcome = await db.run_in_thread(
                functools.partial(
                    schema.upsert_api_upload_part, caller.project_id, upload_id,
                    part_number=number, bytes=written.bytes, sha256=written.sha256, mode="numbered",
                    part_max_bytes=part_max, max_parts=limits.max_parts(), upload_max_bytes=limits.upload_max_bytes(),
                    idle_ttl_s=limits.upload_idle_ttl_s(), max_ttl_s=limits.upload_max_ttl_s(),
                    before_commit=_commit_into(upload_id, written),
                )
            )
            body = _part_outcome(outcome)
            call.meta = {"part_number": number, "bytes": written.bytes, "route": "raw"}
        except BaseException as exc:
            if written is not None:
                partfile.discard_part(written)
            await call.finish(exc)
            raise
        partfile.discard_part(written)
        await call.finish()
        return _json(body)

    # -- 13. POST /v1/uploads/{upload_id}/complete -------------------------

    async def complete_upload(self, request: Request, upload_id: str, caller: Any) -> Response:
        call = await self._begin(request, caller, scope=SCOPE_WRITE, kind=KIND_SYNC, route="v1_uploads_complete")
        try:
            row = await self._upload_row(caller, upload_id)
            call.generation_id = row["id"]
            payload = await _read_json(request, allow_empty=True)
            _unknown_keys(payload, ("part_ids", "md5", "sha256"))
            part_ids = payload.get("part_ids")
            if part_ids is not None:
                if not isinstance(part_ids, list) or not all(isinstance(p, str) for p in part_ids):
                    raise wire.invalid_request("part_ids must be a list of part ids.", param="part_ids")
                if len(set(part_ids)) != len(part_ids):
                    raise wire.invalid_request("part_ids names a part more than once.", param="part_ids")
                if len(part_ids) > limits.max_parts():
                    raise wire.invalid_request("part_ids names more parts than an upload may hold.", param="part_ids")
            md5 = payload.get("md5")
            if md5 is not None and (not isinstance(md5, str) or not ids.is_md5(md5.lower())):
                raise wire.invalid_request("md5 must be 32 hexadecimal characters.", param="md5")
            sha = payload.get("sha256")
            if sha is not None and (not isinstance(sha, str) or not ids.is_sha256(sha.lower())):
                raise wire.invalid_request("sha256 must be 64 hexadecimal characters.", param="sha256")
            if row["status"] == "pending" and not row.get("lapsed"):
                await _require_free(int(row["bytes"]))
            body = await self._finalize(caller, upload_id, part_ids, md5.lower() if md5 else None, sha.lower() if sha else None)
            call.meta = {"bytes": int(row["bytes"]), "parts": len(part_ids) if part_ids is not None else None, "sync": False}
        except BaseException as exc:
            await call.finish(exc)
            raise
        await call.finish()
        return _json(body)

    async def _replay(self, caller: Any, row: Mapping[str, Any]) -> Dict[str, Any]:
        """The stored Upload JSON, with the nested file re-read so its
        `status`/`processing` are current (the ids never change)."""
        body = dict(row.get("result") or wire.upload_object(row))
        file_id = row.get("file_id") or (body.get("file") or {}).get("id")
        if file_id:
            file_row = await db.run_in_thread(schema.get_api_file, caller.project_id, file_id)
            if file_row is not None:
                body["file"] = self._view(file_row)
        body["status"] = wire.upload_status(row)
        return body

    async def _finalize(
        self, caller: Any, upload_id: str, part_ids: Optional[List[str]], md5: Optional[str], sha: Optional[str]
    ) -> Dict[str, Any]:
        state, row = await db.run_in_thread(schema.try_begin_api_upload_finalize, caller.project_id, upload_id)
        if state == "busy":
            # The winner's work is O(parts) row writes; give it up to 2 s to
            # publish, then replay its result (or take over if it returned the
            # row to pending after a validation refusal).
            deadline = time.monotonic() + limits.complete_busy_wait_s()
            while state == "busy" and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                state, row = await db.run_in_thread(schema.try_begin_api_upload_finalize, caller.project_id, upload_id)
            if state == "busy":
                raise wire.upload_state_conflict(
                    "This upload is being completed by another request. Retry to receive its result.",
                    retry_after=2, should_retry=True,
                )
        if state == "gone":
            raise wire.upload_not_found()
        if state == "completed":
            return await self._replay(caller, row or {})
        if state == "rejected":
            stored = row or {}
            raise wire.FilesApiError(
                str(stored.get("error_code") or "invalid_request_error"),
                str(stored.get("error_message") or "This upload was rejected."),
                status=int(stored.get("error_status") or 400),
            )
        if state == "conflict":
            raise wire.upload_state_conflict(f"This upload is {wire.upload_status(row or {})}; it cannot be completed.")
        try:
            parts = await db.run_in_thread(schema.list_api_upload_parts, caller.project_id, upload_id)
            numbers = self._assembly_order(row or {}, parts, part_ids)
            by_number = {int(p["part_number"]): p for p in parts}
            total = sum(int(by_number[n]["bytes"]) for n in numbers)
            declared = int((row or {})["bytes"])
            if total != declared:
                raise wire.invalid_request(
                    f"The listed parts hold {total} bytes; the upload declared {declared}.", param="part_ids"
                )

            def render(upload_row: dict, file_row: dict) -> Dict[str, Any]:
                return wire.upload_object(upload_row, file=self._view(file_row))

            done = await db.run_in_thread(
                functools.partial(
                    schema.complete_api_upload, caller.project_id, upload_id,
                    part_numbers=numbers, expected_md5=md5, expected_sha256=sha, render_result=render,
                )
            )
            if done is None:
                raise errors.internal_error()
        except BaseException:
            await db.run_in_thread(schema.return_api_upload_to_pending, caller.project_id, upload_id)
            raise
        upload_row, _file_row = done
        self._kick_assembler()
        return dict(upload_row["result"])

    @staticmethod
    def _assembly_order(row: Mapping[str, Any], parts: List[dict], part_ids: Optional[List[str]]) -> List[int]:
        if part_ids is not None:
            by_id = {str(p["id"]): int(p["part_number"]) for p in parts}
            missing = [pid for pid in part_ids if pid not in by_id]
            if missing:
                raise wire.invalid_request("part_ids names a part that is not in this upload.", param="part_ids")
            return [by_id[pid] for pid in part_ids]
        if row.get("part_mode") == "sequential":
            raise wire.invalid_request(
                "This upload's parts were sent without part_number, so their order is only known "
                "from part_ids; list the part ids in order.",
                param="part_ids",
            )
        numbers = sorted(int(p["part_number"]) for p in parts)
        expected = list(range(len(numbers)))
        if numbers != expected:
            present = set(numbers)
            gaps = [n for n in range((max(numbers) + 1) if numbers else 0) if n not in present][:5]
            listed = ", ".join(str(n) for n in gaps)
            raise wire.invalid_request(
                f"Part {listed} is missing." if len(gaps) == 1 else f"Parts {listed} are missing.",
                param="part_ids",
            )
        return numbers

    # -- 14. POST /v1/uploads/{upload_id}/cancel ---------------------------

    async def cancel_upload(self, request: Request, upload_id: str, caller: Any) -> Response:
        call = await self._begin(request, caller, scope=SCOPE_WRITE, kind=KIND_SYNC, route="v1_uploads_cancel")
        try:
            if not ids.is_upload_id(upload_id):
                raise wire.upload_not_found()
            state, row = await db.run_in_thread(schema.cancel_api_upload, caller.project_id, upload_id)
            if state == "gone":
                raise wire.upload_not_found()
            call.generation_id = upload_id
            if state == "conflict":
                status = wire.upload_status(row or {})
                if status == "finalizing":
                    raise wire.upload_state_conflict(
                        "This upload is being completed; it cannot be cancelled.", retry_after=2
                    )
                raise wire.upload_state_conflict(f"This upload is {status}; it cannot be cancelled.")
            if state == "cancelled":
                await db.run_in_thread(storage.remove_tree, storage.upload_dir(upload_id))
            body = wire.upload_object(row or {})
        except BaseException as exc:
            await call.finish(exc)
            raise
        await call.finish()
        return _json(body)


# ------------------------------------------------------------ route class --


class FilesRoute(APIRoute):
    """The CONTRACT §9 envelope and `X-Request-Id` for a standalone router
    (tests, the SDK parity app). The real `/v1` router uses `PublicRoute`,
    which does the same and CORS besides."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            request_id = f"req_{uuid.uuid4().hex}"
            request.state.public_request_id = request_id
            try:
                response = await original(request)
            except errors.ApiError as exc:
                response = JSONResponse(exc.envelope(request_id), status_code=exc.status, headers=exc.headers())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never a traceback on the wire
                failure = errors.from_unexpected(exc, request_id=request_id)
                response = JSONResponse(failure.envelope(request_id), status_code=failure.status, headers=failure.headers())
            response.headers["X-Request-Id"] = request_id
            return response

        return handler


# --------------------------------------------------------------- registration --


def _routes(handlers: _Handlers, deps: FilesDependencies) -> List[Tuple[str, str, str, Callable[..., Any]]]:
    async def resolve(request: Request) -> Any:
        # Wrapped so the dependency's own signature (annotated or not) never
        # decides what FastAPI reads from the request: it gets the Request.
        return await deps.resolve_caller(request)

    caller_dep = Depends(resolve)

    async def create_file(request: Request, caller: Any = caller_dep) -> Response:
        return await handlers.create_file(request, caller)

    async def list_files(request: Request, caller: Any = caller_dep) -> Response:
        return await handlers.list_files(request, caller)

    async def retrieve_file(request: Request, file_id: str, caller: Any = caller_dep) -> Response:
        return await handlers.retrieve_file(request, file_id, caller)

    async def file_content(request: Request, file_id: str, caller: Any = caller_dep) -> Response:
        return await handlers.file_content(request, file_id, caller)

    async def delete_file(request: Request, file_id: str, caller: Any = caller_dep) -> Response:
        return await handlers.delete_file(request, file_id, caller)

    async def create_upload(request: Request, caller: Any = caller_dep) -> Response:
        return await handlers.create_upload(request, caller)

    async def retrieve_upload(request: Request, upload_id: str, caller: Any = caller_dep) -> Response:
        return await handlers.retrieve_upload(request, upload_id, caller)

    async def add_part(request: Request, upload_id: str, caller: Any = caller_dep) -> Response:
        return await handlers.add_part(request, upload_id, caller)

    async def put_part(request: Request, upload_id: str, part_number: str, caller: Any = caller_dep) -> Response:
        return await handlers.put_part(request, upload_id, part_number, caller)

    async def complete_upload(request: Request, upload_id: str, caller: Any = caller_dep) -> Response:
        return await handlers.complete_upload(request, upload_id, caller)

    async def cancel_upload(request: Request, upload_id: str, caller: Any = caller_dep) -> Response:
        return await handlers.cancel_upload(request, upload_id, caller)

    table: List[Tuple[str, str, str, Callable[..., Any]]] = [
        ("POST", "/files", "createFile", create_file),
        ("GET", "/files", "listFiles", list_files),
        ("GET", "/files/{file_id}", "retrieveFile", retrieve_file),
        ("GET", "/files/{file_id}/content", "downloadFile", file_content),
        ("DELETE", "/files/{file_id}", "deleteFile", delete_file),
        ("POST", "/uploads", "createUpload", create_upload),
        ("GET", "/uploads/{upload_id}", "retrieveUpload", retrieve_upload),
        ("POST", "/uploads/{upload_id}/parts", "addUploadPart", add_part),
        ("PUT", "/uploads/{upload_id}/parts/{part_number}", "putUploadPart", put_part),
        ("POST", "/uploads/{upload_id}/complete", "completeUpload", complete_upload),
        ("POST", "/uploads/{upload_id}/cancel", "cancelUpload", cancel_upload),
    ]
    if deps.derived is not None:
        async def list_derived(request: Request, file_id: str, caller: Any = caller_dep) -> Response:
            return await handlers.list_derived(request, file_id, caller)

        async def download_derived(request: Request, file_id: str, name: str, caller: Any = caller_dep) -> Response:
            return await handlers.download_derived(request, file_id, name, caller)

        table.append(("GET", "/files/{file_id}/derived", "listFileDerived", list_derived))
        table.append(("GET", "/files/{file_id}/derived/{name}", "downloadFileDerived", download_derived))
    if deps.events is not None:
        async def file_events(request: Request, file_id: str, caller: Any = caller_dep) -> Response:
            return await handlers.file_events(request, file_id, caller)

        table.append(("GET", "/files/{file_id}/events", "streamFileEvents", file_events))
    return table


#: `(method, path)` of every route this module can add, for the surface tests.
ROUTE_TABLE = (
    ("POST", "/v1/files"),
    ("GET", "/v1/files"),
    ("GET", "/v1/files/{file_id}"),
    ("GET", "/v1/files/{file_id}/content"),
    ("DELETE", "/v1/files/{file_id}"),
    ("GET", "/v1/files/{file_id}/events"),
    ("GET", "/v1/files/{file_id}/derived"),
    ("GET", "/v1/files/{file_id}/derived/{name}"),
    ("POST", "/v1/uploads"),
    ("GET", "/v1/uploads/{upload_id}"),
    ("POST", "/v1/uploads/{upload_id}/parts"),
    ("PUT", "/v1/uploads/{upload_id}/parts/{part_number}"),
    ("POST", "/v1/uploads/{upload_id}/complete"),
    ("POST", "/v1/uploads/{upload_id}/cancel"),
)


_PARTS_POST_RE = re.compile(r"^/v1/uploads/[^/]+/parts$")
_PARTS_PUT_RE = re.compile(r"^/v1/uploads/[^/]+/parts/[^/]+$")


def body_cap_for(method: str, path: str) -> Optional[int]:
    """The transport body cap of a file route, or None for any other request.

    THE SEAM for `publicapi/models.body_cap_for` → `main.body_cap_for`
    (integration): the application's body-size middleware caps every `/v1`
    path at 1 MiB today, which would refuse every part. Exact paths only — a
    prefix match would hand a 65 MiB cap to routes that parse JSON in memory.
    `POST /v1/uploads` and `complete` stay under the 1 MiB JSON rule."""
    verb = str(method or "").upper()
    normalised = "/" + str(path or "").strip("/")
    if verb == "POST" and (normalised == "/v1/files" or _PARTS_POST_RE.match(normalised)):
        return limits.max_body_bytes()
    if verb == "PUT" and _PARTS_PUT_RE.match(normalised):
        return limits.part_max_bytes()
    return None


def register(router: APIRouter, deps: FilesDependencies) -> int:
    """Add the file routes to `router` (paths relative to its prefix).
    Idempotent: a `(path, method)` already present is skipped, because two
    routes on one path make the first one's handler the only one that runs.
    Returns how many routes were added."""
    handlers = _Handlers(deps)
    prefix = router.prefix or ""
    existing = {
        (getattr(route, "path", ""), method)
        for route in router.routes
        for method in (getattr(route, "methods", None) or ())
    }
    added = 0
    for method, path, operation_id, endpoint in _routes(handlers, deps):
        if (prefix + path, method) in existing:
            continue
        router.add_api_route(
            path, endpoint, methods=[method], name=operation_id, operation_id=operation_id, response_class=Response
        )
        added += 1
    return added


def create_files_router(deps: FilesDependencies, *, prefix: str = "/v1", route_class: Optional[type] = None) -> APIRouter:
    """A fresh router with the file routes (tests, the SDK parity app)."""
    router = APIRouter(prefix=prefix, route_class=route_class or FilesRoute)
    register(router, deps)
    return router
