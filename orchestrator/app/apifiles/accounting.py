"""Storage accounting and the processing usage row (design §11).

STORAGE IS ACCOUNTED, NOT METERED. There are no usage limits on `/v1` (owner
decision 2026-09-13), so nothing here refuses anything. What it does is answer
"how much of the disk is this project" — for `GET /v1/usage.storage`, the
console Files tab and the operator gauges — honestly: two files over one blob
cost the disk once and count once, and `derived_bytes` is measured by walking
the directory at `finalize`, not estimated.

PROCESSING IS NOT A REQUEST. It does not touch `api_usage_daily.requests`; it
writes one `usage_events` row per processing run (`record_processing`), through
the application's one ledger (`usage.record`), so the analytics console sees
API processing without a parallel set of numbers.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Iterable, Optional

from .. import db
from . import schema, storage

log = logging.getLogger(__name__)

PROCESSING_ROUTE = "v1_file_processing"


async def project_storage(project_id: str) -> dict:
    """`{files, bytes, derived_bytes, uploads_pending, uploads_pending_bytes}`."""
    return await db.run_in_thread(schema.project_file_storage, project_id)


def measure_derived_bytes(project_id: str, sha256: str, *, video_hash: Optional[str] = None) -> int:
    """Bytes under the blob's `derived/` plus its video analysis directory
    (audio/video), walked now. BLOCKING."""
    total = 0
    derived = storage.derived_dir(project_id, sha256)
    if os.path.isdir(derived):
        total += storage.dir_bytes(derived)
    if video_hash:
        from ..video import store as video_store

        try:
            analysis = video_store.analysis_dir(video_hash)
        except ValueError:
            analysis = ""
        if analysis and os.path.isdir(analysis):
            total += storage.dir_bytes(analysis)
    return total


def finalize_blob_accounting(blob: dict) -> Optional[dict]:
    """Measure and store `derived_bytes` for a blob at its terminal stage."""
    video_hash = None
    if blob.get("kind") in ("audio", "video"):
        video_hash = storage.api_video_hash(str(blob["project_id"]), str(blob["sha256"]))
    derived = measure_derived_bytes(str(blob["project_id"]), str(blob["sha256"]), video_hash=video_hash)
    return schema.update_api_file_blob(str(blob["id"]), derived_bytes=int(derived))


def processing_generation_id(blob: dict) -> str:
    """`<blob id>.<attempt>`: `usage_events.generation_id` is unique, and a
    blob whose processing is re-run (a deferral, a later pipeline version)
    is a second run that must be a second row, not a silent no-op."""
    return f"{blob['id']}.{int(blob.get('attempt') or 0)}"


async def record_processing(
    blob: dict,
    *,
    file_ids: Iterable[str],
    status: str,
    duration_ms: Optional[int],
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """One `usage_events` row per processing run. Never raises."""
    from .. import usage

    facts = dict(blob.get("facts") or {})
    row_meta: Dict[str, Any] = {
        "project_id": blob.get("project_id"),
        "file_ids": sorted(str(i) for i in file_ids),
        "kind": blob.get("kind"),
        "derived_bytes": int(blob.get("derived_bytes") or 0),
        "attempts": int(blob.get("attempt") or 0),
        "status": status,
    }
    for key in ("pages", "ocr_pages", "audio_seconds", "frames", "ocr_frames", "embed_input_tokens", "chunks_indexed"):
        if key in facts:
            row_meta[key] = facts[key]
    row_meta.update(meta or {})
    await usage.record_async(
        user_id=None,
        workspace_id=blob.get("workspace_id") or None,
        conversation_id=None,
        generation_id=processing_generation_id(blob),
        route=PROCESSING_ROUTE,
        model="",
        mode="api",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
        status=usage.OK if status == "processed" else usage.ERROR,
        error_kind=str(blob.get("error_code") or "") if status != "processed" else "",
        meta=row_meta,
    )


# ------------------------------------------------------------------ gauges --


class _Counter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def add(self, n: int) -> None:
        with self._lock:
            self._value += int(n)

    @property
    def value(self) -> int:
        with self._lock:
            return self._value


#: `public_api_upload_parts_in_flight` (design §3.1): parts whose bodies are
#: streaming right now, in this process. Docs advise ≤ 8 concurrent parts.
PARTS_IN_FLIGHT = _Counter()


class PartInFlight:
    """`with PartInFlight():` around one part body."""

    def __enter__(self) -> "PartInFlight":
        PARTS_IN_FLIGHT.add(1)
        return self

    def __exit__(self, *exc: Any) -> None:
        PARTS_IN_FLIGHT.add(-1)


def gauges() -> Dict[str, int]:
    """A snapshot for `/metrics` (integration: `app/metrics.py` owns the
    registry). Free space is cheap (`statvfs`); totals are two aggregates."""
    snapshot: Dict[str, int] = {"public_api_upload_parts_in_flight": PARTS_IN_FLIGHT.value}
    try:
        snapshot["api_files_disk_free_bytes"] = storage.free_bytes()
    except OSError:
        pass
    try:
        with db.connection() as con:
            row = con.execute(
                "SELECT COALESCE(sum(bytes), 0) AS bytes, COALESCE(sum(derived_bytes), 0) AS derived "
                "  FROM api_file_blobs WHERE status <> 'deleting'"
            ).fetchone()
        snapshot["api_files_bytes_total"] = int(row["bytes"])
        snapshot["api_files_derived_bytes_total"] = int(row["derived"])
    except Exception:  # noqa: BLE001 - a gauge must never break a scrape
        log.debug("files gauges unavailable", exc_info=True)
    return snapshot
