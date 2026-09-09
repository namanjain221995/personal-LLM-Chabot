"""The HTTP surface of video understanding, and the upload hand-off.

Two things live here:

* `attach_upload` — called by the upload routes when a file arrives with
  purpose=video. It hashes the bytes, finds or creates the analysis row,
  links it to the conversation, adopts the bytes into the analysis
  directory (outside the 24-hour workspace sweep) and starts the job.
  Nothing waits: the response goes back while the job runs, and the chat
  turn that follows subscribes to it.

* `GET /video/{conversation_id}/{upload_id}/status` — what the job is doing
  right now, for the person who owns the conversation. The chat stream
  carries the same progress live; this is for a tab that reloaded and for
  the operator's curiosity. It never names a model.

The feature gate is `require_video`: the per-member switch (403, like every
other tool) and the deployment switch (404, like voice). The order is the
one every gate here uses — a member who may not use the tool learns that
first, whatever the deployment has installed.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import db
from ..auth import UserRow, require_user
from ..config import settings
from . import pipeline, store
from .types import STAGE_TITLES, STAGES

log = logging.getLogger(__name__)

router = APIRouter(prefix="/video", tags=["video"])

VIDEO_EXTENSIONS = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mpg", ".mpeg", ".ts", ".3gp", ".ogv", ".wmv", ".flv")


def looks_like_video(filename: str, content_type: str = "") -> bool:
    lower = (filename or "").lower()
    if lower.endswith(VIDEO_EXTENSIONS):
        return True
    return (content_type or "").lower().startswith("video/")


async def require_video(request: Request) -> None:
    """403 when this account may not use video; 404 when the deployment has it off."""
    from ..authn import features as feature_access
    from ..authn.principal import require_principal

    principal = await require_principal(request)
    if not feature_access.allowed(principal.features, feature_access.Feature.VIDEO_ANALYSIS):
        raise HTTPException(
            status_code=403,
            detail="Video understanding is turned off for your account. Ask an administrator.",
        )
    if not settings.video_analysis_enabled:
        raise HTTPException(status_code=404, detail="video understanding is not enabled")


async def attach_upload(
    *,
    conversation_id: str,
    upload_id: str,
    filename: str,
    raw_path: str,
    size: int,
    user_id: Optional[int],
) -> dict:
    """Register an uploaded video and start (or reuse) its analysis."""
    cap = settings.video_max_upload_mb * 1024 * 1024
    if size > cap:
        raise HTTPException(
            status_code=413,
            detail=f"That video is larger than {settings.video_max_upload_mb} MB.",
        )
    content_hash = await asyncio.to_thread(store.hash_file, raw_path)
    media_type = mimetypes.guess_type(filename)[0] or ""
    row = await db.run_in_thread(db.upsert_video_analysis, content_hash, size, media_type, filename)
    await asyncio.to_thread(store.adopt_source, content_hash, raw_path, filename)
    await db.run_in_thread(
        db.link_video_attachment, int(row["id"]), conversation_id, user_id, upload_id, filename
    )
    await db.run_in_thread(
        db.save_upload, upload_id, conversation_id, filename, size, "ready", None, "video"
    )
    started = await pipeline.ensure_running(int(row["id"]))
    log.info(
        "video %s: %s (%d bytes) attached to %s — analysis %s%s",
        content_hash[:12], filename, size, conversation_id, row["id"],
        " started" if started else (" already done" if row["status"] == "done" else " queued"),
    )
    # A row finished under an older pipeline was just put back to work; say
    # so, or the composer would show "done" for a job that is running.
    status = row["status"] if not (started and row["status"] == "done") else "queued"
    return {
        "upload_id": upload_id,
        "filename": filename,
        "bytes": size,
        "files": 1,
        "notes": [],
        "profile": [],
        "video": {
            "analysis_id": int(row["id"]),
            "status": status,
            "reused": not row.get("created", False) and status == "done",
        },
    }


def status_payload(row: dict) -> dict:
    """The row as the UI may see it: progress, never engine names."""
    latest = pipeline._latest.get(int(row["id"])) or {}
    stages = row.get("stages") or {}
    timeline = []
    for name in STAGES:
        state = stages.get(name) or {}
        entry = {"stage": name, "title": STAGE_TITLES[name], "status": state.get("status") or "pending"}
        if state.get("ms") is not None:
            entry["ms"] = state["ms"]
        if state.get("detail"):
            entry["detail"] = state["detail"]
        if latest.get("stage") == name and latest.get("status") == "running":
            entry["status"] = "running"
            entry["percent"] = latest.get("percent")
            entry["detail"] = latest.get("detail") or entry.get("detail", "")
            entry["elapsed_s"] = latest.get("elapsed_s")
        timeline.append(entry)
    return {
        "status": row["status"],
        "stage": row.get("stage"),
        "error": row.get("error") or "",
        "duration_ms": row.get("duration_ms"),
        "has_audio": row.get("has_audio"),
        "has_video": row.get("has_video"),
        "language": row.get("language"),
        "counts": row.get("counts") or {},
        "stages": timeline,
        "artifacts": [
            {"filename": a.get("filename"), "bytes": a.get("bytes"), "kind": a.get("kind")}
            for a in (row.get("artifacts") or [])
        ],
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
    }


@router.get("/{conversation_id}/{upload_id}/status")
async def video_status(
    conversation_id: str,
    upload_id: str,
    user: UserRow = Depends(require_user),
    _video: None = Depends(require_video),
) -> dict:
    owner = await db.run_in_thread(db.conversation_owner, conversation_id)
    if owner != int(user["id"]):
        raise HTTPException(status_code=404, detail="not found")
    row = await db.run_in_thread(db.get_video_by_upload, conversation_id, upload_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    payload = status_payload(row)
    payload["filename"] = row.get("display_name") or row.get("filename")
    payload["source_present"] = bool(store.source_path(row["content_hash"]))
    return payload


def artifact_path(row: dict, filename: str) -> Optional[str]:
    """Absolute path of one of an analysis's artifact files, or None."""
    for a in row.get("artifacts") or []:
        if a.get("filename") == filename:
            path = os.path.join(store.artifacts_dir(row["content_hash"]), filename)
            return path if os.path.isfile(path) else None
    return None
