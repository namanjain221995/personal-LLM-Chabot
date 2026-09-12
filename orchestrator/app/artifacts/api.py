"""The artifact HTTP surface — see docs/artifact-studio/API.md.

EVERY ROUTE IS OWNER-SCOPED. The caller's user_id is in the WHERE clause of
every lookup, so another person's artifact id answers 404 exactly as a
missing one does (the /reports resolver's rule, core/report_paths.py). The
only path components ever derived from a request are validated ids, a
version integer and a format from the fixed table; `store` re-checks that
every resolved file is inside its version directory before it is opened.

PREVIEW AND DOWNLOAD ARE DIFFERENT ACTIONS. `?disposition=inline` is what
the viewer fetches; `attachment` (the default) is what the Download button
gets, with an RFC 5987 filename. Both are `private, no-store` and carry the
file's sha256 as an ETag. Starlette's FileResponse honours Range and HEAD,
which is what a page-seeking viewer needs.

A FILE IS NAMED BY ITS ID (CONTRACT-2 §2). `GET …/f/{file_id}` serves one
file of a version by the sixteen-hex id the pipeline minted for it; the
older `GET …/file/{fmt}` stays as an alias for the FIRST file of a format.
`GET …/zip` streams every file of the version as one ZIP_STORED bundle
built on the fly — the files are read in a worker thread 64 KiB at a time
and never held whole in memory; the bound is on what a person downloads
(types.MAX_ZIP_BYTES over the recorded sizes → 413), not on memory.
`GET …/grid?file=` pages through a workbook sheet or a CSV as text
(formulas never evaluated); `/sheets` stays as the xlsx alias.

PAGE IMAGES are rasterised on first request and cached under the version's
`previews/` directory, so reopening a viewer never re-renders a document.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import zipfile
from typing import Any, Dict, Iterator, List, Literal, Optional, Sequence, Tuple
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .. import db, metrics
from ..auth import UserRow, require_user
from ..config import settings
from . import db as adb
from . import pipeline, store
from . import types as T

log = logging.getLogger(__name__)

router = APIRouter(prefix="/artifacts", tags=["artifacts"])

#: Sheet-grid bounds the viewer may ask for.
_GRID_MAX_ROWS = 500
_GRID_MAX_COLS = 60
#: How much of a file the zip route reads per step — one chunk in memory
#: per response, whatever the file's size.
_ZIP_CHUNK = 64 * 1024

#: Page images are rasterised at most this many at a time per process, and
#: one (version, page, width) at a time: a viewer that scrolls a 40-page
#: document asks for forty pages in a burst, and PDFium — serialised by a
#: process-wide lock in any case — must not be asked to do the same page
#: forty times over. Requests past the bound wait; they do not fail.
_RASTER_CONCURRENCY = 2
_raster_gate: Optional[asyncio.Semaphore] = None
_inflight: Dict[str, "asyncio.Future[None]"] = {}


def _raster_slot() -> asyncio.Semaphore:
    # Not `_gate`: every route has a `_gate` PARAMETER (the feature check),
    # which shadowed this name inside the handler and made it None.
    global _raster_gate
    if _raster_gate is None:
        _raster_gate = asyncio.Semaphore(_RASTER_CONCURRENCY)
    return _raster_gate


async def require_artifacts(request: Request) -> None:
    """403 when this account may not use documents; 404 when the deployment has it off."""
    from ..authn import features as feature_access
    from ..authn.principal import require_principal

    principal = await require_principal(request)
    if not feature_access.allowed(principal.features, feature_access.Feature.ARTIFACTS):
        raise HTTPException(status_code=403, detail="Documents, decks and workbooks are turned off for your account. Ask an administrator.")
    if not settings.artifacts_enabled:
        raise HTTPException(status_code=404, detail="document generation is not enabled")


def _id(value: str) -> str:
    if not T.is_artifact_id(value):
        raise HTTPException(status_code=404, detail="not found")
    return value


def _version(value: int) -> int:
    if value < 1 or value > 100_000:
        raise HTTPException(status_code=404, detail="not found")
    return int(value)


def _content_disposition(kind: str, filename: str) -> str:
    """`inline` or `attachment` with an ASCII fallback and the UTF-8 name."""
    ascii_name = filename.encode("ascii", "ignore").decode("ascii") or "download"
    return f'{kind}; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'


def _file_response(path: str, *, media_type: str, filename: str, disposition: str, etag: str = "") -> FileResponse:
    headers = {
        "Content-Disposition": _content_disposition(disposition, filename),
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if etag:
        headers["ETag"] = f'"{etag}"'
    return FileResponse(path, media_type=media_type, headers=headers)


async def _version_or_404(artifact_id: str, version: int, user: UserRow) -> dict:
    row = await db.run_in_thread(adb.get_version, _id(artifact_id), _version(version), int(user["id"]))
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return row


def _published_ref(job: Optional[dict], version_row: dict) -> dict:
    job = job or {
        "artifact_id": version_row["artifact_id"], "version": version_row["version"], "id": version_row.get("job_id") or "",
        "status": version_row.get("status"), "operation": version_row.get("operation"), "created_at": version_row.get("created_at"),
        "kind": version_row.get("kind"), "title": version_row.get("title"),
    }
    return pipeline.ref_for(job, version_row).to_json()


def _version_files(version_row: dict) -> List[dict]:
    """The version's files as the wire sees them — every entry with a
    `file_id`, a legacy row's synthesised by pipeline.ref_for, so a version
    from before ids existed is served by id like any other — and an entry
    the row holds in a shape that cannot be described (a text size, not a
    dict) left out with a log line, so every route reads the list the same
    way and a corrupt entry is a 404 for its file, never a 500 for the
    version (security review 2026-09-12)."""
    return [f.to_json() for f in pipeline.ref_for({"artifact_id": version_row["artifact_id"], "version": version_row["version"], "id": version_row.get("job_id") or ""}, version_row).files]


def _first_of_format(version_row: dict, fmt: str) -> Optional[dict]:
    """The FIRST file of a format — what the `/file/{fmt}` and `/sheets`
    aliases serve — read through `_version_files` like the id routes."""
    return next((f for f in _version_files(version_row) if f.get("format") == fmt), None)


#: The refusal categories that are a 409 (the person's own state: full
#: storage, too many jobs open, nothing left to retry from) rather than a
#: 400 (a request that cannot be a document).
_CONFLICT_CATEGORIES = frozenset({"quota_exceeded", "storage_failure", "source_unavailable"})


def _refused(exc: pipeline.ArtifactRefused) -> HTTPException:
    return HTTPException(status_code=409 if getattr(exc, "category", "") in _CONFLICT_CATEGORIES else 400, detail=str(exc))


def _resolve_by_id(user_id: int, artifact_id: str, version: int, file_id: str, files: Sequence[dict]) -> Tuple[str, dict]:
    try:
        path, entry = store.resolve_file_by_id(user_id, artifact_id, version, file_id, files)
    except (store.PathRefused, ValueError):
        raise HTTPException(status_code=404, detail="not found")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    return path, entry


def _count_download(fmt: str, disposition: str) -> None:
    label = fmt if fmt in T.FORMATS or fmt == "zip" else "other"
    metrics.inc("artifact_download_total", "artifact files served", format=label, result="inline" if disposition == "inline" else "attachment")


# ---------------------------------------------------------------- listing --


@router.get("")
async def list_artifacts(
    conversation_id: Optional[str] = Query(default=None, max_length=200),
    user: UserRow = Depends(require_user),
    _gate: None = Depends(require_artifacts),
) -> dict:
    rows = await db.run_in_thread(adb.list_artifacts, int(user["id"]), conversation_id)
    out = []
    for a in rows:
        current = a.get("current")
        if not current:
            continue
        out.append(_published_ref(None, {**current, "kind": a.get("kind"), "title": a.get("title")}))
    return {"artifacts": out}


@router.get("/jobs/{job_id}")
async def job_status(job_id: str, user: UserRow = Depends(require_user), _gate: None = Depends(require_artifacts)) -> dict:
    row = await db.run_in_thread(adb.get_job, _id(job_id), int(user["id"]))
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    progress = row.get("progress") or {}
    out = {
        "job_id": row["id"],
        "artifact_id": row["artifact_id"],
        "version": int(row["version"]),
        "status": row.get("status"),
        "stage": row.get("stage"),
        "stage_title": T.STAGE_TITLES.get(str(row.get("stage") or ""), ""),
        "progress": {"detail": str(progress.get("detail") or ""), "elapsed_s": progress.get("elapsed_s"), "stages": progress.get("stages") or {}},
        "attempt": int(row.get("attempt") or 0),
    }
    if row.get("failure_category"):
        out["failure_category"] = row["failure_category"]
    if row.get("error"):
        out["error"] = row["error"]
    if row.get("status") in ("completed", "completed_with_warnings"):
        version = await db.run_in_thread(adb.get_version, row["artifact_id"], int(row["version"]), int(user["id"]))
        if version:
            out["artifact"] = pipeline.ref_for(row, version).to_json()
    return out


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, user: UserRow = Depends(require_user), _gate: None = Depends(require_artifacts)) -> dict:
    row = await pipeline.cancel(_id(job_id), int(user["id"]))
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return {"job_id": row["id"], "status": row.get("status")}


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str, user: UserRow = Depends(require_user), _gate: None = Depends(require_artifacts)) -> dict:
    """409 with a sentence where accept() would refuse a new job (storage,
    the open-jobs ceiling) and when the sweep has taken what the retry
    would compose from; the row is left failed either way."""
    try:
        row = await pipeline.retry(_id(job_id), int(user["id"]))
    except pipeline.ArtifactRefused as exc:
        raise _refused(exc)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row.get("status") in ("completed", "completed_with_warnings", "cancelled"):
        # retry_job leaves any state but 'failed' alone and returns the row;
        # saying "retried" about a finished job would be a lie.
        raise HTTPException(status_code=409, detail="Only a failed job can be retried.")
    await pipeline.ensure_running(str(row["id"]))
    return {"job_id": row["id"], "status": row.get("status")}


@router.get("/{artifact_id}")
async def get_artifact(artifact_id: str, user: UserRow = Depends(require_user), _gate: None = Depends(require_artifacts)) -> dict:
    artifact = await db.run_in_thread(adb.get_artifact, _id(artifact_id), int(user["id"]))
    if artifact is None:
        raise HTTPException(status_code=404, detail="not found")
    versions = await db.run_in_thread(adb.list_versions, artifact_id, int(user["id"]))
    return {
        "artifact": {k: artifact.get(k) for k in ("id", "title", "kind", "current_version", "created_at", "updated_at")},
        "versions": [_published_ref(None, {**v, "kind": artifact.get("kind"), "title": artifact.get("title")}) for v in versions],
    }


@router.get("/{artifact_id}/v/{version}")
async def get_version(artifact_id: str, version: int, user: UserRow = Depends(require_user), _gate: None = Depends(require_artifacts)) -> dict:
    row = await _version_or_404(artifact_id, version, user)
    out = _published_ref(None, row)
    out["validation"] = row.get("validation") or {}
    out["assumptions"] = row.get("assumptions") or []
    out["instruction"] = row.get("instruction") or ""
    return out


# ------------------------------------------------------------------ files --


@router.api_route("/{artifact_id}/v/{version}/file/{fmt}", methods=["GET", "HEAD"])
async def get_file(
    artifact_id: str,
    version: int,
    fmt: str,
    disposition: str = Query(default="attachment", pattern="^(inline|attachment)$"),
    user: UserRow = Depends(require_user),
    _gate: None = Depends(require_artifacts),
):
    if fmt not in T.FORMATS:
        raise HTTPException(status_code=404, detail="not found")
    row = await _version_or_404(artifact_id, version, user)
    entry = _first_of_format(row, fmt)
    if entry is None:
        raise HTTPException(status_code=404, detail="not found")
    try:
        path = store.resolve_version_file(int(user["id"]), artifact_id, version, fmt, str(entry.get("filename") or ""))
    except (store.PathRefused, ValueError):
        raise HTTPException(status_code=404, detail="not found")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    _count_download(fmt, disposition)
    return _file_response(path, media_type=T.MIME_TYPES[fmt], filename=str(entry.get("filename") or f"file.{fmt}"), disposition=disposition, etag=str(entry.get("sha256") or ""))


@router.api_route("/{artifact_id}/v/{version}/f/{file_id}", methods=["GET", "HEAD"])
async def get_file_by_id(
    artifact_id: str,
    version: int,
    file_id: str,
    disposition: str = Query(default="attachment", pattern="^(inline|attachment)$"),
    user: UserRow = Depends(require_user),
    _gate: None = Depends(require_artifacts),
):
    """One file by its id — the URL every FileRef carries. A value that is
    not sixteen hex characters is 404 before any lookup; the owner check
    is the version lookup; the file is resolved through the version row's
    list, never from the request's text."""
    if not T.is_file_id(file_id):
        raise HTTPException(status_code=404, detail="not found")
    row = await _version_or_404(artifact_id, version, user)
    path, entry = _resolve_by_id(int(user["id"]), artifact_id, version, file_id, _version_files(row))
    fmt = str(entry.get("format") or "")
    _count_download(fmt, disposition)
    return _file_response(
        path, media_type=T.MIME_TYPES.get(fmt, "application/octet-stream"),
        filename=str(entry.get("filename") or f"file.{fmt}"), disposition=disposition, etag=str(entry.get("sha256") or ""),
    )


class _ZipSink:
    """The unseekable writer zipfile builds the bundle on: it keeps only
    what has been written since the last `take()`, so the response holds
    one chunk plus the zip's per-entry headers at a time. No `tell`/`seek`
    on purpose — zipfile then writes data descriptors after each entry
    instead of seeking back to patch the local header."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def write(self, data: bytes) -> int:
        self._buf += data
        return len(data)

    def flush(self) -> None:
        return None

    def take(self) -> bytes:
        out = bytes(self._buf)
        self._buf.clear()
        return out


def _zip_stream(entries: Sequence[Tuple[str, str, int]]) -> Iterator[bytes]:
    """The bundle, chunk by chunk: `entries` are (path, name, size) triples
    the caller already resolved and containment-checked. Runs in a worker
    thread (Starlette iterates a sync generator in its threadpool), reads
    each file in _ZIP_CHUNK pieces, and yields what zipfile wrote after
    every piece. ZIP_STORED: the files are already compressed containers
    (docx/pptx/xlsx are zips, a PDF has its own streams) and a CSV is
    small; deflating them again would cost CPU on the request thread for
    nothing."""
    sink = _ZipSink()
    stamp = time.gmtime()
    date_time = (max(1980, stamp.tm_year), stamp.tm_mon, stamp.tm_mday, stamp.tm_hour, stamp.tm_min, stamp.tm_sec)
    with zipfile.ZipFile(sink, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
        for path, name, size in entries:
            info = zipfile.ZipInfo(name, date_time)
            info.compress_type = zipfile.ZIP_STORED
            info.file_size = int(size)
            info.external_attr = (0o644 & 0xFFFF) << 16
            with bundle.open(info, mode="w") as member, open(path, "rb") as src:
                while True:
                    chunk = src.read(_ZIP_CHUNK)
                    if not chunk:
                        break
                    member.write(chunk)
                    piece = sink.take()
                    if piece:
                        yield piece
            piece = sink.take()
            if piece:
                yield piece
    tail = sink.take()
    if tail:
        yield tail


@router.get("/{artifact_id}/v/{version}/zip")
async def get_zip(artifact_id: str, version: int, user: UserRow = Depends(require_user), _gate: None = Depends(require_artifacts)):
    """Every file of the version as one ZIP, streamed (CONTRACT-2 §2). 404
    for a version with no file (never published, cancelled, failed) or a
    file that is not on disk — checked BEFORE the first byte, so a missing
    file is a status code and not a truncated archive; 413 when the
    recorded sizes sum past types.MAX_ZIP_BYTES."""
    row = await _version_or_404(artifact_id, version, user)
    files = _version_files(row)
    if not files:
        raise HTTPException(status_code=404, detail="not found")
    total = sum(int(f.get("size") or 0) for f in files)
    if total > int(T.MAX_ZIP_BYTES):
        raise HTTPException(status_code=413, detail=f"This version's files add up to {total // (1024 * 1024)} MB; the bundle limit is {int(T.MAX_ZIP_BYTES) // (1024 * 1024)} MB. Download the files one by one.")
    entries: List[Tuple[str, str, int]] = []
    names: set = set()
    for f in files:
        path, entry = _resolve_by_id(int(user["id"]), artifact_id, version, str(f.get("file_id") or ""), files)
        name = os.path.basename(str(entry.get("filename") or ""))
        if not name or name in names:
            # Two entries cannot share a name inside one archive; the
            # pipeline's one-file-per-(role, format, sheet) rule keeps
            # names distinct, so a duplicate is a corrupt row, not a case.
            raise HTTPException(status_code=404, detail="not found")
        names.add(name)
        entries.append((path, name, int(entry.get("size") or os.path.getsize(path))))
    bundle_name = f"{T.slug_for(str(row.get('title') or ''))}-v{int(version)}.zip"
    _count_download("zip", "attachment")
    headers = {
        "Content-Disposition": _content_disposition("attachment", bundle_name),
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    return StreamingResponse(_zip_stream(entries), media_type=T.MIME_TYPES["zip"], headers=headers)


@router.api_route("/{artifact_id}/v/{version}/preview", methods=["GET", "HEAD"])
async def get_preview_pdf(artifact_id: str, version: int, user: UserRow = Depends(require_user), _gate: None = Depends(require_artifacts)):
    row = await _version_or_404(artifact_id, version, user)
    try:
        path = store.resolve_preview_pdf(int(user["id"]), artifact_id, version)
    except (store.PathRefused, ValueError):
        raise HTTPException(status_code=404, detail="not found")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Preview unavailable — download the file instead.")
    return _file_response(path, media_type="application/pdf", filename=f"{T.slug_for(str(row.get('title') or ''))}-v{version}-preview.pdf", disposition="inline")


@router.get("/{artifact_id}/v/{version}/preview/{page}.png")
async def get_preview_page(
    artifact_id: str,
    version: int,
    page: int,
    w: int = Query(default=T.PREVIEW_WIDTHS[1]),
    user: UserRow = Depends(require_user),
    _gate: None = Depends(require_artifacts),
):
    row = await _version_or_404(artifact_id, version, user)
    if page < 1 or page > max(1, int(row.get("preview_pages") or 0)) or page > T.MAX_PREVIEW_PAGES:
        raise HTTPException(status_code=404, detail="not found")
    width = min(T.PREVIEW_WIDTHS, key=lambda cand: abs(cand - w))
    try:
        pdf_path = store.resolve_preview_pdf(int(user["id"]), artifact_id, version)
    except (store.PathRefused, ValueError):
        raise HTTPException(status_code=404, detail="not found")
    if not os.path.isfile(pdf_path):
        raise HTTPException(status_code=404, detail="Preview unavailable — download the file instead.")
    cache_dir = os.path.join(os.path.dirname(pdf_path), T.PREVIEWS_DIR)
    cached = os.path.join(cache_dir, f"{page}-{width}.png")
    if not os.path.isfile(cached):
        key = f"{artifact_id}:{version}:{page}:{width}"
        pending = _inflight.get(key)
        if pending is not None:
            # Somebody is rendering this very page: wait for them.
            await asyncio.shield(pending)
        else:
            fut: "asyncio.Future[None]" = asyncio.get_running_loop().create_future()
            _inflight[key] = fut
            try:
                async with _raster_slot():
                    started = asyncio.get_running_loop().time()
                    from .render.preview import rasterise_page

                    png = await asyncio.to_thread(rasterise_page, pdf_path, page, width)
                    os.makedirs(cache_dir, exist_ok=True)
                    await asyncio.to_thread(store.write_bytes, cached, png)
                    metrics.observe("artifact_preview_seconds", asyncio.get_running_loop().time() - started, "page-image rasterisation")
            except IndexError:
                raise HTTPException(status_code=404, detail="not found")
            except Exception as exc:  # noqa: BLE001 — a preview that will not render is not a missing file
                log.warning("artifact preview page failed: %s", type(exc).__name__)
                raise HTTPException(status_code=503, detail="Preview unavailable — download the file instead.")
            finally:
                _inflight.pop(key, None)
                if not fut.done():
                    fut.set_result(None)
        if not os.path.isfile(cached):
            raise HTTPException(status_code=503, detail="Preview unavailable — download the file instead.")
    # Five minutes, private: long enough for a scroll back up, short enough
    # that a shared machine does not keep another person's pages for an hour.
    return FileResponse(cached, media_type="image/png", headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})


@router.get("/{artifact_id}/v/{version}/sheets")
async def get_sheets(
    artifact_id: str,
    version: int,
    sheet: Optional[str] = Query(default=None, max_length=31),
    rows: int = Query(default=200, ge=1, le=_GRID_MAX_ROWS),
    cols: int = Query(default=50, ge=1, le=_GRID_MAX_COLS),
    user: UserRow = Depends(require_user),
    _gate: None = Depends(require_artifacts),
) -> JSONResponse:
    row = await _version_or_404(artifact_id, version, user)
    entry = _first_of_format(row, "xlsx")
    if entry is None:
        raise HTTPException(status_code=404, detail="not found")
    try:
        path = store.resolve_version_file(int(user["id"]), artifact_id, version, "xlsx", str(entry.get("filename") or ""))
    except (store.PathRefused, ValueError):
        raise HTTPException(status_code=404, detail="not found")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    try:
        from .render.preview import sheet_grid

        grid = await asyncio.to_thread(sheet_grid, path, sheet, rows, cols)
    except (KeyError, LookupError):
        raise HTTPException(status_code=404, detail="not found")
    except Exception as exc:  # noqa: BLE001
        log.warning("artifact sheet grid failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Preview unavailable — download the workbook instead.")
    return JSONResponse(grid, headers={"Cache-Control": "private, no-store"})


def _grid_page(path: str, fmt: str, *, title: str, sheet: str, offset: int, limit: int, max_cols: int) -> Dict[str, Any]:
    """CONTRACT-2 §11's grid dict for one file: `render.preview.grid_for`
    when the render package provides it (wave 2c), else the same shape
    built here from the two readers that exist today — `sheet_grid` for
    an xlsx (no offset of its own: the page is sliced out of the rows it
    returns) and `render.csv.read_csv_grid` for a csv. Formulas are text
    in both; nothing is evaluated. Raises KeyError for a sheet the
    workbook does not have."""
    from .render import preview as preview_mod

    grid_for = getattr(preview_mod, "grid_for", None)
    if callable(grid_for):
        return grid_for(path, fmt, sheet=sheet, offset=offset, limit=limit, max_cols=max_cols, title=title)
    if fmt == "csv":
        from .render.csv import read_csv_grid

        page = read_csv_grid(path, offset=offset, limit=limit, max_cols=max_cols)
        label = title or "Data"
        return {
            "sheets": [label], "sheet": label, "columns": list(page["columns"]), "rows": list(page["rows"]),
            "total_rows": int(page["total_rows"]), "total_columns": int(page["total_columns"]),
            "truncated": bool(page["truncated"]), "formulas_as_text": True,
        }
    raw = preview_mod.sheet_grid(path, sheet or None, offset + limit, max_cols)
    chosen = raw.get("sheet") or {}
    listing = raw.get("sheets") or []
    names = [str(s.get("name")) for s in listing]
    by_name = {str(s.get("name")): s for s in listing}
    facts = by_name.get(str(chosen.get("name")), {})
    all_rows = list(chosen.get("rows") or [])
    total_rows = max(0, int(facts.get("rows") or 0) - 1) if facts else len(all_rows)
    total_columns = int(facts.get("cols") or len(chosen.get("columns") or []))
    # sheet_grid pads every row to max_cols; the page carries the sheet's
    # real width (a 60-column row of blanks is not a two-column sheet).
    width = max(1, min(total_columns, max_cols))
    page_rows = [list(r)[:width] for r in all_rows[offset: offset + limit]]
    return {
        "sheets": names, "sheet": str(chosen.get("name") or ""), "columns": list(chosen.get("columns") or [])[:width], "rows": page_rows,
        "total_rows": total_rows, "total_columns": total_columns,
        "truncated": bool(chosen.get("truncated")) or total_rows > offset + len(page_rows) or total_columns > max_cols,
        "formulas_as_text": True,
    }


@router.get("/{artifact_id}/v/{version}/grid")
async def get_grid(
    artifact_id: str,
    version: int,
    file: Optional[str] = Query(default=None),
    sheet: Optional[str] = Query(default=None, max_length=31),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=_GRID_MAX_ROWS),
    cols: int = Query(default=50, ge=1, le=_GRID_MAX_COLS),
    user: UserRow = Depends(require_user),
    _gate: None = Depends(require_artifacts),
) -> JSONResponse:
    """A page of one grid file — an xlsx sheet or a csv — as text. `file`
    names the file by id; absent, the version's first xlsx (then csv) is
    read, so `/grid` alone previews a workbook the way `/sheets` does.
    Bounds are refused by validation (422), never clamped silently."""
    if file is not None and not T.is_file_id(file):
        raise HTTPException(status_code=404, detail="not found")
    row = await _version_or_404(artifact_id, version, user)
    files = _version_files(row)
    if file is not None:
        entry = next((f for f in files if f.get("file_id") == file), None)
    else:
        entry = next((f for f in files if f.get("format") == "xlsx"), None) or next((f for f in files if f.get("format") == "csv"), None)
    if entry is None or str(entry.get("format") or "") not in T.GRID_FORMATS:
        raise HTTPException(status_code=404, detail="not found")
    fmt = str(entry.get("format"))
    path, entry = _resolve_by_id(int(user["id"]), artifact_id, version, str(entry.get("file_id") or ""), files)
    try:
        grid = await asyncio.to_thread(
            _grid_page, path, fmt, title=str(entry.get("title") or row.get("title") or ""),
            sheet=str(sheet or ""), offset=int(offset), limit=int(limit), max_cols=int(cols),
        )
    except (KeyError, LookupError):
        raise HTTPException(status_code=404, detail="not found")
    except Exception as exc:  # noqa: BLE001
        log.warning("artifact grid failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Preview unavailable — download the file instead.")
    return JSONResponse(grid, headers={"Cache-Control": "private, no-store"})


# ---------------------------------------------------------------- convert --


class ConvertBody(BaseModel):
    """The one field a conversion takes. Any other shape is a 422 from
    FastAPI, never a 500 — and the value is never echoed back."""

    model_config = ConfigDict(extra="forbid")
    format: Literal["pdf", "docx", "pptx", "xlsx", "csv"]


@router.post("/{artifact_id}/convert")
async def convert_artifact(artifact_id: str, body: ConvertBody, user: UserRow = Depends(require_user), _gate: None = Depends(require_artifacts)) -> dict:
    fmt = body.format
    artifact = await db.run_in_thread(adb.get_artifact, _id(artifact_id), int(user["id"]))
    if artifact is None:
        raise HTTPException(status_code=404, detail="not found")
    kind = str(artifact.get("kind") or "document")
    if fmt not in T.FORMATS_FOR_KIND.get(kind, ()):
        allowed = list(T.FORMATS_FOR_KIND.get(kind, ()))
        options = " or ".join(allowed) if len(allowed) <= 2 else ", ".join(allowed[:-1]) + " or " + allowed[-1]
        raise HTTPException(status_code=400, detail=f"A {kind} can be made as {options}.")
    version = int(artifact.get("current_version") or 0)
    if version < 1:
        raise HTTPException(status_code=409, detail="This artifact has no finished version to convert yet.")
    current = await db.run_in_thread(adb.get_version, artifact_id, version, int(user["id"]))
    if current and _first_of_format(current, fmt) is not None:
        raise HTTPException(status_code=409, detail=f"The current version already has a {fmt.upper()} file.")
    conversation_id = str(artifact.get("conversation_id") or "")
    # The key names the artifact AND the version: two artifacts converted
    # to the same format are two jobs, and a convert that failed earlier
    # is retried rather than handed back as the answer.
    key = pipeline.idempotency_key(int(user["id"]), conversation_id, f"convert:{artifact_id}:v{version}", "convert", fmt)
    try:
        job = await db.run_in_thread(
            pipeline.accept,
            user_id=int(user["id"]), conversation_id=conversation_id, generation_id="",
            operation="convert", instruction=f"convert to {fmt}", kind=kind, formats=[fmt], format_reason=f"convert: {fmt}",
            effort="fast", mode="assistant", template_id="generic", parent=(artifact_id, version),
            idempotency_key=key, title=str(artifact.get("title") or ""),
        )
    except pipeline.ArtifactRefused as exc:
        raise _refused(exc)
    if job.get("status") == "failed":
        # A convert that failed earlier is retried, not handed back — and
        # meets the refusals a retry meets (the same 409s).
        try:
            job = await pipeline.retry(str(job["id"]), int(user["id"])) or job
        except pipeline.ArtifactRefused as exc:
            raise _refused(exc)
    await pipeline.ensure_running(str(job["id"]))
    return {"job_id": job["id"], "artifact_id": job["artifact_id"], "version": int(job["version"])}

