"""The developer console's Files tab, server side (files-hookup, 2026-09-13).

`console_api.py` declares the routes (the BFF's proxy suite reads its
decorators); the work is here, and it is the SAME work `/v1` does. Every call
goes through `publicapi.files.routes._Handlers` with the dependencies the public
router registered, so an upload the console makes is assembled, processed,
listed, resumed and deleted by exactly the code a key's upload is — only the
credential differs:

* the caller is the PROJECT the path names, resolved inside the principal's
  workspace by `console_api._project_or_404` (a foreign project is a 404);
* authorisation is the route's capability (`api.projects.read` to look,
  `api.projects.manage` to upload or delete), checked before this module runs,
  so the handlers' scope check is a no-op here;
* nothing is counted against the project's API usage and no usage row is
  written: a console action is not the project's API traffic (the playground's
  rule, CONTRACT-3 §11). A delete writes an audit event instead.

WHAT IS NEVER HERE. No byte route: neither a file's content nor a derived
output is served on a cookie-authenticated path (Files design D6). The console
lists derived names and sizes and says which API call fetches them with a key.

ERRORS. The handlers raise `publicapi.errors.ApiError`, whose status, envelope
and headers (`Retry-After`, `x-should-retry`) the uploader depends on — a busy
`complete` must reach the browser as `409` with `x-should-retry: true`, not as
Starlette's plain 500. `run` renders them; the console router has no route class
that would.
"""
from __future__ import annotations

import dataclasses
import functools
import logging
import secrets
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from .. import db
from ..publicapi import errors as api_errors

log = logging.getLogger(__name__)

#: The list route's page size bounds (the console table pages 50 at a time).
LIST_MAX_LIMIT = 100
#: How many rows one filtered list call may scan to fill a page. A status or
#: kind filter is applied to the rows the keyset returns (the accessor has no
#: such predicate), so a sparse filter walks a bounded number of pages and
#: answers `has_more` with the cursor where it stopped rather than scanning a
#: project's whole history in one request.
FILTER_SCAN_ROWS = 2_000
STATUS_FILTERS = ("queued", "processing", "processed", "failed")


@dataclasses.dataclass(frozen=True)
class ConsoleCaller:
    """The shape the file handlers read from a caller: the project, its
    workspace, and no key (a console action has none)."""

    project_id: str
    workspace_id: str
    key_id: Optional[str] = None


async def _allow(request: Request, caller: Any, scope: str) -> None:
    return None


async def _no_admit(request: Request, caller: Any, kind: str) -> Any:
    return None


async def _no_usage(record: Any) -> None:
    return None


def _request_id(request: Request) -> str:
    value = str(getattr(request.state, "public_request_id", "") or "")
    if not value:
        value = f"req_{secrets.token_hex(16)}"
        request.state.public_request_id = value
    return value


@functools.lru_cache(maxsize=1)
def _handlers_for(deps_id: int) -> Any:
    from ..publicapi import router as public_router
    from ..publicapi.files import routes as files_routes

    deps = dataclasses.replace(
        public_router.FILES_DEPENDENCIES,
        authorize=_allow,
        admit=_no_admit,
        record_usage=_no_usage,
        request_id=_request_id,
    )
    # `purges_in_flight` is the SAME dict as the public router's (replace
    # passes the field through), so a console DELETE and a key's upload of the
    # same bytes still share one purge per blob.
    return files_routes._Handlers(deps)


def handlers() -> Any:
    """The file handlers with the console's dependencies, or a 404 when the
    Files API is not mounted in this process (the console then has nothing to
    show, and says so the way every console refusal does)."""
    from ..publicapi import router as public_router

    deps = getattr(public_router, "FILES_DEPENDENCIES", None)
    if not getattr(public_router, "FILES_MOUNTED", False) or deps is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Not found.")
    return _handlers_for(id(deps))


def caller_for(project: Dict[str, Any], workspace_id: str) -> ConsoleCaller:
    """`workspace_id` is the principal's: `_project_or_404` already proved the
    project is in it (Files design §7.2 rule 1)."""
    return ConsoleCaller(project_id=str(project["id"]), workspace_id=str(workspace_id))


def _render(exc: api_errors.ApiError, request: Request) -> JSONResponse:
    request_id = _request_id(request)
    headers = dict(exc.headers())
    headers["X-Request-Id"] = request_id
    return JSONResponse(status_code=exc.status, content=exc.envelope(request_id), headers=headers)


async def run(request: Request, work: Callable[[], Awaitable[Response]]) -> Response:
    """One console Files call: the handler's own answer, or its `ApiError`
    rendered with status, envelope and headers (module docstring, ERRORS)."""
    _request_id(request)
    try:
        return await work()
    except api_errors.ApiError as exc:
        return _render(exc, request)


def require_json(request: Request) -> None:
    """Defence in depth under the BFF's same-origin guard: the upload handlers
    read a JSON body whatever its `Content-Type` and `complete` accepts an empty
    one, so a cross-site form post could otherwise reach them. A browser cannot
    send `application/json` cross-site without a preflight this route never
    answers."""
    kind = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if kind != "application/json":
        raise api_errors.invalid_request("This request must send JSON.", param="Content-Type")


def _console_file(row: Dict[str, Any]) -> Dict[str, Any]:
    from ..apifiles import jobs
    from ..publicapi.files import wire

    body = wire.file_object(row, processing_view=jobs.file_processing_view)
    # Design §14.1 puts "derived size" in the console table; not on /v1's
    # object. None — never 0 — before a blob exists.
    derived = row.get("blob_derived_bytes")
    body["derived_bytes"] = None if row.get("blob_id") is None or derived is None else int(derived)
    return body


async def list_files(
    project: Dict[str, Any],
    *,
    after: Optional[str],
    limit: int,
    status: Optional[str],
    kind: Optional[str],
) -> Dict[str, Any]:
    """The project's files, newest first, optionally filtered by
    `processing.state` and kind (module constants explain the bounded scan)."""
    from ..apifiles import ids, schema
    from ..publicapi.files import wire

    if after is not None and not ids.is_file_id(after):
        raise api_errors.invalid_request("after names an unknown cursor.", param="after")
    if status is not None and status not in STATUS_FILTERS:
        raise api_errors.invalid_request("status is not a processing state.", param="status")
    project_id = str(project["id"])
    filtered = status is not None or kind is not None
    wanted: List[Dict[str, Any]] = []
    cursor = after
    scanned = 0
    has_more = False
    while True:
        try:
            rows, more = await db.run_in_thread(
                functools.partial(
                    schema.list_api_files,
                    project_id,
                    after=cursor,
                    limit=(LIST_MAX_LIMIT if filtered else limit),
                    order="desc",
                    purpose=None,
                )
            )
        except schema.UnknownCursor:
            raise api_errors.invalid_request("after names an unknown cursor.", param="after") from None
        stopped_early = False
        for index, row in enumerate(rows):
            scanned += 1
            cursor = str(row["id"])
            body = _console_file(row)
            processing = body.get("processing") or {}
            if status is not None and processing.get("state") != status:
                continue
            if kind is not None and (processing.get("kind") or row.get("blob_kind")) != kind:
                continue
            wanted.append(body)
            if len(wanted) >= limit:
                stopped_early = index < len(rows) - 1
                break
        if len(wanted) >= limit:
            has_more = stopped_early or bool(more)
            break
        if not more or not rows:
            break
        if scanned >= FILTER_SCAN_ROWS:
            has_more = True
            break
    listed = wire.list_object(wanted, has_more)
    # The cursor for the next page: the last row LOOKED AT when there is more
    # (a sparse filter that stopped at the scan bound resumes from there).
    listed["last_id"] = cursor if has_more else (wanted[-1]["id"] if wanted else None)
    return listed


async def get_file(project: Dict[str, Any], file_id: str) -> Dict[str, Any]:
    from ..apifiles import ids, schema
    from ..publicapi.files import wire

    row = None
    if ids.is_file_id(file_id):
        row = await db.run_in_thread(schema.get_api_file, str(project["id"]), file_id)
    if row is None:
        raise wire.file_not_found()
    return _console_file(row)


async def storage(project: Dict[str, Any]) -> Dict[str, Any]:
    from ..apifiles import schema

    stats = await db.run_in_thread(schema.project_file_storage, str(project["id"]))
    return {
        key: (None if stats.get(key) is None else int(stats[key]))
        for key in ("files", "bytes", "derived_bytes", "uploads_pending", "uploads_pending_bytes")
    }
