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
import re
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
#: B12 (2026-09-18): audio is the video pipeline with no picture — the frames
#: stage skips ("the file has no video stream") and the transcript, summary
#: and index run as for any recording. The composer's list is the same one
#: (frontend/lib/attachments.ts).
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".aac")
#: Declared audio/* (or HLS) but not a recording: a playlist is a TEXT list of
#: paths or URLs, MIDI is a score with nothing to hear until a synthesiser
#: plays it. Browsers type .m3u as audio/mpegurl or audio/x-mpegurl and .pls
#: as audio/x-scpls; QA measured all three reaching ffmpeg on the audio/*
#: rule (2026-09-18), where they fail at the probe at best. The composer's
#: list is the same one (frontend/lib/attachments.ts NOT_RECORDING_*).
NOT_RECORDING_TYPES = frozenset({
    "audio/mpegurl", "audio/x-mpegurl", "application/vnd.apple.mpegurl",
    "application/x-mpegurl", "audio/x-scpls", "audio/scpls",
    "audio/midi", "audio/x-midi", "audio/mid", "audio/sp-midi",
})
NOT_RECORDING_EXTENSIONS = (".m3u", ".m3u8", ".pls", ".hls", ".mid", ".midi", ".kar")


def looks_like_video(filename: str, content_type: str = "") -> bool:
    """True for a file the video pipeline should analyse, audio included."""
    lower = (filename or "").strip().lower()
    if lower.endswith(NOT_RECORDING_EXTENSIONS):
        return False
    if lower.endswith(VIDEO_EXTENSIONS + AUDIO_EXTENSIONS):
        return True
    declared = (content_type or "").split(";")[0].strip().lower()
    if declared in NOT_RECORDING_TYPES:
        return False
    return declared.startswith("video/") or declared.startswith("audio/")


def _stored_name(filename: str) -> str:
    """The name the source is stored under — its extension is what ffmpeg sees.

    The upload keeps whatever name the person (or a direct POST) gave, and
    ffmpeg 6.1 demuxes a file NAMED *.m3u/*.m3u8 as HLS and opens the local
    media paths it lists (QA, 2026-09-18: a 96,078-byte WAV decoded from
    another directory). A known media extension is kept; anything else is
    stored as .bin and probed by its bytes, which HLS does not match without
    a playlist extension.
    """
    lower = (filename or "").strip().lower()
    if lower.endswith(VIDEO_EXTENSIONS + AUDIO_EXTENSIONS):
        return filename
    return "source.bin"


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
        kind = "recording" if (filename or "").strip().lower().endswith(AUDIO_EXTENSIONS) else "video"
        raise HTTPException(
            status_code=413,
            detail=f"That {kind} is larger than {settings.video_max_upload_mb} MB.",
        )
    content_hash = await asyncio.to_thread(store.hash_file, raw_path)
    media_type = mimetypes.guess_type(filename)[0] or ""
    row = await db.run_in_thread(db.upsert_video_analysis, content_hash, size, media_type, filename)
    await asyncio.to_thread(store.adopt_source, content_hash, raw_path, _stored_name(filename))
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


#: B25b (2026-09-18): until this date the vision stage stored "11/12 frames
#: described by Qwen3-VL-8B-Instruct". The pipeline no longer writes the
#: name, but finished rows keep their stored text for the life of the bytes,
#: so the surface drops it on the way out. Only that one sentence shape.
_DESCRIBED_BY = re.compile(r"^(\d+/\d+ frames described) by \S+$")


def _public_detail(stage: str, detail: str) -> str:
    """A stage detail as the person may read it: no model, no engine address.

    Rows written before 2026-09-18 stored a failed or deferred stage's
    `str(exc)` as it was, and ModelUnavailable's text carries the engine's
    base URL (QA measured "model at http://…:8000/v1 unavailable after 780s"
    in both the fusion detail and `error`); the pipeline now scrubs it when
    it writes, this scrubs what is already stored.
    """
    detail = pipeline.public_text(detail)
    if stage == "vision":
        match = _DESCRIBED_BY.match(detail)
        if match:
            return match.group(1)
    return detail


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
            entry["detail"] = _public_detail(name, str(state["detail"]))
        if latest.get("stage") == name and latest.get("status") == "running":
            entry["status"] = "running"
            entry["percent"] = latest.get("percent")
            entry["detail"] = _public_detail(name, str(latest.get("detail") or "")) or entry.get("detail", "")
            entry["elapsed_s"] = latest.get("elapsed_s")
        timeline.append(entry)
    return {
        "status": row["status"],
        "stage": row.get("stage"),
        "error": pipeline.public_text(str(row.get("error") or "")),
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
