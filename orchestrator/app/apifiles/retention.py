"""Retention: DELETE's purge, file expiry, tombstones and reconciliation
(design §2.7, §7.3, acceptance A-5).

`purge_blob(blob_id)` IS WHAT MAKES DELETE TRUE. After `schema.delete_api_file`
commits a blob to `deleting` (no live file references it any more), the purge:

1. cancels a local processing job (`jobs.cancel_blob`: its extraction child is
   killed before anything is removed, so nothing writes into a directory that
   is being deleted); a job in ANOTHER process sees `deleting` at its next unit
   boundary, within a second of work;
2. for audio/video, cancels the video pipeline task, removes the analysis
   directory (`/data/video/<api hash>/`: source link, audio.wav, frames,
   transcript, artifacts) and deletes the `video_analyses` row;
3. retires the blob through `schema.retire_deleting_blob`: in ONE
   transaction, holding the row FOR UPDATE, it renames
   `<PUBLIC_API_FILES_DIR>/<project_id>/<sha256>/` — `original`, `derived/`
   with pages, chunks, vectors — into `_trash/` and deletes the row; the
   rmtree then runs on the trash path (`storage.remove_trash`). The blob
   directory is keyed by CONTENT, so an rmtree of the content path after the
   row was gone could delete a re-upload's bytes; a rename under the row lock
   cannot (ingest team's review finding, 2026-09-13).

Every step is idempotent, and the row goes LAST: a crash anywhere leaves a
`deleting` row that the next pass finishes. Vectors are files in the blob
directory, not LanceDB rows, precisely so this `rmtree` removes them physically
and at once (design §1.3: a LanceDB delete keeps the bytes in older versions).

THE LOOP (`start`), every 10 min plus an immediate `kick()` on DELETE, each
pass bounded to PUBLIC_API_FILES_SWEEP_BATCH (200) rows per step:

* `deleting` blobs — purges a crash or a restart interrupted. One blob whose
  purge raises (EACCES, EBUSY) is logged, moved to the back of the queue
  (`updated_at` bumped) and skipped: before 2026-09-13 its exception ended
  the step, it stayed first by `updated_at`, and no later purge or expiry
  ever ran (review finding, reproduced over three passes);
* expired files (`expires_at` reached) — the same delete path as DELETE, then
  the purge when that was the last reference;
* tombstones older than PUBLIC_API_FILES_TOMBSTONE_DAYS (30) — hard-deleted;
* blob directories with no row, older than 1 h (a workspace cascade, or a
  crash between the row delete and the rmtree) — removed. The age gate keeps a
  directory created a moment before its row commits;
* `lane = 'api'` analyses no blob references, older than 1 h — removed;
* `derived/renders/*` and stray `*.tmp` inside blob directories older than
  24 h — crash leftovers of the OCR stage and the atomic writers.

`_single/`, `_inline/` and `_uploads/` are the upload sweep's
(`apifiles/uploads_sweep.py`); this module never touches them.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Set, Tuple

from .. import db
from . import ids, limits, schema, storage

log = logging.getLogger(__name__)

#: A render or temporary file older than this is a crash leftover (§7.3).
LEFTOVER_AGE_S = 86_400.0


@dataclass
class PurgeResult:
    blob_id: str
    removed_dir: bool = False
    removed_analysis: bool = False
    removed_row: bool = False
    skipped: str = ""


@dataclass
class RetentionReport:
    purged: List[str] = field(default_factory=list)
    expired_files: List[str] = field(default_factory=list)
    tombstones_removed: int = 0
    orphan_dirs_removed: int = 0
    orphan_analyses_removed: int = 0
    leftovers_removed: int = 0
    #: Blob ids whose purge raised this pass (requeued behind the others).
    failed: List[str] = field(default_factory=list)


# ------------------------------------------------------------------ purge --


def _analysis_hash(blob: Dict) -> Optional[str]:
    """The analysis directory's key: from the row the blob points at when it
    still exists, else the project-keyed hash (the row may already be gone
    after a crash between steps)."""
    analysis_id = blob.get("video_analysis_id")
    if analysis_id:
        row = db.get_video_analysis(int(analysis_id))
        if row and row.get("content_hash"):
            return str(row["content_hash"])
    if blob.get("kind") in ("audio", "video"):
        return storage.api_video_hash(str(blob["project_id"]), str(blob["sha256"]))
    return None


async def _cancel_pipeline(analysis_id: int) -> None:
    try:
        from . import media

        await media.default_deps().cancel(int(analysis_id))
    except Exception:  # noqa: BLE001 — a pipeline that is not running is fine
        log.debug("files purge: pipeline cancel for %s", analysis_id, exc_info=True)


_purging: Dict[str, "asyncio.Future[PurgeResult]"] = {}


def _tolerant_remove_tree(path: str) -> bool:
    """`storage.remove_tree`, where a concurrent remover finishing first
    (ENOENT mid-walk) is success, not an error."""
    for _ in range(3):
        try:
            return storage.remove_tree(path)
        except FileNotFoundError:
            if not os.path.lexists(path):
                return True
    return storage.remove_tree(path)


def _requeue_deleting(blob_id: str) -> None:
    """Move a `deleting` row whose purge failed behind the others."""
    with db.connection() as con:
        con.execute(
            "UPDATE api_file_blobs SET updated_at = now() WHERE id = %s AND status = 'deleting'", (blob_id,)
        )


def _retire(blob_id: str) -> Tuple[bool, bool]:
    """BLOCKING. (row retired, directory removed)."""
    retired = schema.retire_deleting_blob(blob_id, move_aside=storage.move_blob_dir_aside)
    if retired is None:
        return False, False
    _row, trash = retired
    removed = False
    if trash:
        try:
            removed = bool(storage.remove_trash(trash))
        except Exception:  # noqa: BLE001 — the row is gone; the upload sweep clears `_trash/`
            log.warning("files purge of %s left its bytes in the trash", blob_id, exc_info=True)
    return True, removed


async def purge_blob(blob: "str | Mapping[str, Any]") -> PurgeResult:
    """Remove a `deleting` blob's bytes, derived data, analysis and row.

    Takes the blob id (design §13.2) or a blob row (the shape
    `publicapi.files.routes.FilesDependencies.purge_blob` passes); the row is
    always RE-READ, so a stale row can never purge a blob that was revived.
    One purge per blob id runs at a time in this process: a DELETE's kick and
    the periodic pass racing on one blob share the first purge's result."""
    blob_id = str(blob.get("id") if isinstance(blob, Mapping) else blob)
    loop = asyncio.get_running_loop()
    running = _purging.get(blob_id)
    if running is None or running.done() or running.get_loop() is not loop:
        running = asyncio.ensure_future(_purge_blob(blob_id))
        _purging[blob_id] = running
        running.add_done_callback(lambda f, bid=blob_id: _purging.pop(bid, None) if _purging.get(bid) is f else None)
    return await asyncio.shield(running)


async def _purge_blob(blob_id: str) -> PurgeResult:
    result = PurgeResult(blob_id=blob_id)
    if not ids.is_blob_id(blob_id):
        result.skipped = "not a blob id"
        return result
    blob = await db.run_in_thread(schema.get_api_file_blob, blob_id)
    if blob is None:
        result.skipped = "gone"
        return result
    if blob.get("status") != "deleting":
        # A live blob (another file still references the bytes, or a DELETE
        # that never committed): never purge it.
        result.skipped = "not deleting"
        return result
    try:
        from . import jobs

        await jobs.cancel_blob(str(blob_id))
    except Exception:  # noqa: BLE001
        log.warning("files purge: cancelling the local job of %s failed", blob_id, exc_info=True)
    analysis_id = blob.get("video_analysis_id")
    if analysis_id:
        await _cancel_pipeline(int(analysis_id))
    content_hash = await db.run_in_thread(_analysis_hash, blob)
    if content_hash:
        from ..video import store as video_store

        try:
            directory = video_store.analysis_dir(content_hash)
            if os.path.lexists(directory):
                result.removed_analysis = await asyncio.to_thread(_tolerant_remove_tree, directory)
        except ValueError:
            log.warning("files purge: refused to remove the analysis of %s", blob_id)
        if analysis_id:
            await db.run_in_thread(db.delete_video_analysis, int(analysis_id))
    try:
        result.removed_row, result.removed_dir = await db.run_in_thread(_retire, str(blob_id))
    except ValueError:
        log.error("files purge: refused to move the directory of %s", blob_id)
        result.skipped = "fence"
        return result
    log.info(
        "files purge %s: dir=%s analysis=%s row=%s", blob_id, result.removed_dir, result.removed_analysis, result.removed_row
    )
    return result


# ------------------------------------------------------------ the passes --


async def _purge_one(report: RetentionReport, blob_id: str) -> None:
    """One purge, isolated: its failure is logged and the row requeued, and
    the caller moves on to the next blob."""
    try:
        outcome = await purge_blob(blob_id)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.warning("files purge of %s failed; it is retried after the others", blob_id, exc_info=True)
        report.failed.append(blob_id)
        try:
            await db.run_in_thread(_requeue_deleting, blob_id)
        except Exception:  # noqa: BLE001
            log.debug("files purge: could not requeue %s", blob_id, exc_info=True)
        return
    if outcome.removed_row:
        report.purged.append(outcome.blob_id)


async def _purge_deleting(report: RetentionReport, batch: int) -> None:
    rows = await db.run_in_thread(schema.api_blobs_in_state, "deleting", batch)
    for row in rows:
        await _purge_one(report, str(row["id"]))


async def _expire_files(report: RetentionReport, batch: int) -> None:
    expired = await db.run_in_thread(schema.expired_api_files, batch)
    for project_id, file_id in expired:
        try:
            deleted = await db.run_in_thread(schema.delete_api_file, project_id, file_id, include_expired=True)
        except Exception:  # noqa: BLE001 — one file must not stop every later expiry
            log.warning("files expiry of %s failed", file_id, exc_info=True)
            continue
        if deleted is None:
            continue
        tomb, blob = deleted
        report.expired_files.append(file_id)
        if blob is not None:
            await _purge_one(report, str(blob["id"]))


def _blob_dirs(root: str) -> List[Tuple[str, str, str]]:
    """(project_id, sha256, path) for every well-formed blob directory."""
    out: List[Tuple[str, str, str]] = []
    try:
        projects = list(os.scandir(root))
    except OSError:
        return out
    for project in projects:
        if not ids.is_project_id(project.name) or not project.is_dir(follow_symlinks=False):
            continue
        try:
            blobs = list(os.scandir(project.path))
        except OSError:
            continue
        for blob in blobs:
            if ids.is_sha256(blob.name) and blob.is_dir(follow_symlinks=False):
                out.append((project.name, blob.name, blob.path))
    return out


def _known_blobs(pairs: List[Tuple[str, str]]) -> Set[Tuple[str, str]]:
    if not pairs:
        return set()
    with db.connection() as con:
        rows = con.execute(
            "SELECT project_id, sha256 FROM api_file_blobs "
            " WHERE (project_id, sha256) IN (SELECT * FROM unnest(%s::text[], %s::text[]))",
            ([p for p, _ in pairs], [s for _, s in pairs]),
        ).fetchall()
    return {(str(r["project_id"]), str(r["sha256"])) for r in rows}


def _age_s(path: str, now: float) -> float:
    try:
        return now - os.lstat(path).st_mtime
    except OSError:
        return 0.0


def _reconcile_dirs(report: RetentionReport, batch: int, now: float) -> None:
    """BLOCKING. Orphan blob directories, then leftovers inside live ones."""
    root = storage.root()
    entries = _blob_dirs(root)
    min_age = limits.orphan_dir_min_age_s()
    for start in range(0, len(entries), 500):
        chunk = entries[start:start + 500]
        known = _known_blobs([(p, s) for p, s, _ in chunk])
        for project_id, sha256, path in chunk:
            if (project_id, sha256) in known:
                report.leftovers_removed += _remove_leftovers(path, now)
                continue
            if report.orphan_dirs_removed >= batch or _age_s(path, now) < min_age:
                continue
            try:
                if storage.remove_tree(path):
                    report.orphan_dirs_removed += 1
            except (OSError, ValueError):
                log.warning("files retention could not remove an orphan blob directory", exc_info=True)


def _remove_leftovers(blob_path: str, now: float) -> int:
    removed = 0
    renders = os.path.join(blob_path, storage.DERIVED_NAME, "renders")
    candidates: List[str] = []
    try:
        candidates.extend(entry.path for entry in os.scandir(renders))
    except OSError:
        pass
    for base, _dirs, files in os.walk(blob_path, followlinks=False):
        for name in files:
            if name.endswith(".tmp") or name.startswith(".stage-"):
                candidates.append(os.path.join(base, name))
    for path in candidates:
        if _age_s(path, now) < LEFTOVER_AGE_S:
            continue
        try:
            if storage.remove_file(path):
                removed += 1
        except (OSError, ValueError):
            continue
    return removed


async def _reconcile_analyses(report: RetentionReport, batch: int) -> None:
    try:
        rows = await db.run_in_thread(schema.api_video_analyses_without_blob, limits.orphan_dir_min_age_s(), batch)
    except Exception:  # noqa: BLE001 — `video_analyses.lane` is V36; absent before the migration
        log.debug("files retention: api analyses reconciliation unavailable", exc_info=True)
        return
    from ..video import store as video_store

    for row in rows:
        await _cancel_pipeline(int(row["id"]))
        try:
            directory = video_store.analysis_dir(str(row["content_hash"]))
            if os.path.lexists(directory):
                await asyncio.to_thread(storage.remove_tree, directory)
        except ValueError:
            continue
        if await db.run_in_thread(db.delete_video_analysis, int(row["id"])):
            report.orphan_analyses_removed += 1


async def run_pass(*, now: Optional[float] = None) -> RetentionReport:
    """One bounded pass of every retention step, in the order of the module
    docstring. Each step's failure is logged and the next step still runs."""
    report = RetentionReport()
    batch = limits.sweep_batch()
    moment = time.time() if now is None else float(now)
    steps: List[Tuple[str, Callable[[], Awaitable[None]]]] = [
        ("deleting", lambda: _purge_deleting(report, batch)),
        ("expiry", lambda: _expire_files(report, batch)),
    ]
    for name, step in steps:
        try:
            await step()
        except Exception:  # noqa: BLE001
            log.warning("files retention step %s failed", name, exc_info=True)
    try:
        report.tombstones_removed = await db.run_in_thread(
            schema.purge_api_file_tombstones, float(limits.tombstone_days()) * 86_400.0
        )
    except Exception:  # noqa: BLE001
        log.warning("files retention step tombstones failed", exc_info=True)
    try:
        await asyncio.to_thread(_reconcile_dirs, report, batch, moment)
    except Exception:  # noqa: BLE001
        log.warning("files retention step directories failed", exc_info=True)
    await _reconcile_analyses(report, batch)
    return report


# ------------------------------------------------------------------ loop --

_kick = None  # type: Optional[asyncio.Event]
_task: Optional[asyncio.Task] = None
_pending_purges: Set[str] = set()


def kick(blob_id: Optional[str] = None) -> None:
    """Run a pass now (after a DELETE). With `blob_id`, that purge runs first
    even if the pass itself is mid-way. Safe to call from the event loop only."""
    if blob_id:
        _pending_purges.add(str(blob_id))
    if _kick is not None:
        _kick.set()


async def _loop(interval_s: float, on_pass: Optional[Callable[[RetentionReport], None]]) -> None:
    global _kick
    _kick = asyncio.Event()
    while True:
        while _pending_purges:
            blob_id = _pending_purges.pop()
            await _purge_one(RetentionReport(), blob_id)
        try:
            report = await run_pass()
            if on_pass is not None:
                on_pass(report)
        except Exception:  # noqa: BLE001
            log.warning("files retention pass failed", exc_info=True)
        _kick.clear()
        try:
            await asyncio.wait_for(_kick.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass


async def start(*, interval_s: Optional[float] = None, on_pass: Optional[Callable[[RetentionReport], None]] = None) -> None:
    """Start the loop (integration: main.py startup, after `jobs.start()`)."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(float(interval_s or limits.sweep_interval_s()), on_pass), name="apifiles-retention")


async def stop() -> None:
    global _task, _kick
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _task = None
    _kick = None
