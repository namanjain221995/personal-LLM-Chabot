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

PAGE IMAGES are rasterised on first request and cached under the version's
`previews/` directory, so reopening a viewer never re-renders a document.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, Literal, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from fastapi.responses import FileResponse, JSONResponse

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
    row = await pipeline.retry(_id(job_id), int(user["id"]))
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
    entry = next((f for f in (row.get("files") or []) if f.get("format") == fmt), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="not found")
    try:
        path = store.resolve_version_file(int(user["id"]), artifact_id, version, fmt, str(entry.get("filename") or ""))
    except (store.PathRefused, ValueError):
        raise HTTPException(status_code=404, detail="not found")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    metrics.inc("artifact_download_total", "artifact files served", format=fmt, result="inline" if disposition == "inline" else "attachment")
    return _file_response(path, media_type=T.MIME_TYPES[fmt], filename=str(entry.get("filename") or f"file.{fmt}"), disposition=disposition, etag=str(entry.get("sha256") or ""))


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
    entry = next((f for f in (row.get("files") or []) if f.get("format") == "xlsx"), None)
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


# ---------------------------------------------------------------- convert --


class ConvertBody(BaseModel):
    """The one field a conversion takes. Any other shape is a 422 from
    FastAPI, never a 500 — and the value is never echoed back."""

    model_config = ConfigDict(extra="forbid")
    format: Literal["pdf", "docx", "pptx", "xlsx"]


@router.post("/{artifact_id}/convert")
async def convert_artifact(artifact_id: str, body: ConvertBody, user: UserRow = Depends(require_user), _gate: None = Depends(require_artifacts)) -> dict:
    fmt = body.format
    artifact = await db.run_in_thread(adb.get_artifact, _id(artifact_id), int(user["id"]))
    if artifact is None:
        raise HTTPException(status_code=404, detail="not found")
    kind = str(artifact.get("kind") or "document")
    if fmt not in T.FORMATS_FOR_KIND.get(kind, ()):
        options = " or ".join(T.FORMATS_FOR_KIND.get(kind, ()))
        raise HTTPException(status_code=400, detail=f"A {kind} can be made as {options}.")
    version = int(artifact.get("current_version") or 0)
    if version < 1:
        raise HTTPException(status_code=409, detail="This artifact has no finished version to convert yet.")
    current = await db.run_in_thread(adb.get_version, artifact_id, version, int(user["id"]))
    if current and any(f.get("format") == fmt for f in (current.get("files") or [])):
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
        raise HTTPException(status_code=409 if getattr(exc, "category", "") in ("quota_exceeded", "storage_failure") else 400, detail=str(exc))
    if job.get("status") == "failed":
        job = await pipeline.retry(str(job["id"]), int(user["id"])) or job
    await pipeline.ensure_running(str(job["id"]))
    return {"job_id": job["id"], "artifact_id": job["artifact_id"], "version": int(job["version"])}

