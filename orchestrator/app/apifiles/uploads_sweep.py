"""The upload sweep: expiry, stale finalize, abandoned assemblies, leftovers.

WHY A SWEEP AND NOT TIMERS ON REQUESTS (design §7.3). Nothing about an upload
may depend on a request arriving: a client that walks away mid-upload never
sends another one, and a process that dies mid-`complete` never finishes the
one it had. So the rows carry their own deadlines (`expires_at`, the
`finalizing` age, the assembly lease) and this sweep enforces them — at start-up
and every PUBLIC_API_FILES_SWEEP_INTERVAL_S (10 min), each pass bounded.

ORDER OF A PASS, and why:
1. `finalizing` older than 600 s → `pending`: a crash between the lock and the
   record must not leave an upload that refuses its own retry forever.
2. `pending` past `expires_at` → `expired`, parts removed. The expiry slides
   with each part (last part + 24 h, never past created + 7 d): a 100 GiB upload
   at 10 Mbit/s takes ~24 h, and V29's fixed TTL would expire it mid-way.
3. Assemblies whose file was deleted mid-way: parts removed, columns cleared.
4. Terminal upload rows older than 30 days: deleted, directories with them.
5. Leftovers: `_single/*.tmp`, `_inline/*`, stray `*.tmp` / `*.assembling`
   older than 24 h; `_uploads/<id>` with no row (or a terminal non-assembling
   row) older than 1 h; `_trash/*` (a purged blob's directory whose rmtree a
   crash interrupted) once its rename is a minute old.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

from .. import db
from . import ids, limits, queue, schema, storage

log = logging.getLogger(__name__)


@dataclass
class SweepReport:
    finalizing_reset: int = 0
    expired: List[str] = field(default_factory=list)
    abandoned_assemblies: List[str] = field(default_factory=list)
    purged_records: List[str] = field(default_factory=list)
    leftovers_removed: int = 0
    orphan_upload_dirs_removed: int = 0


def _older_than(path: str, age_s: float, now: float) -> bool:
    try:
        return now - os.lstat(path).st_mtime > age_s
    except OSError:
        return False


def _remove(path: str) -> bool:
    try:
        return storage.remove_tree(path)
    except FileNotFoundError:
        return False
    except Exception:  # noqa: BLE001 - one stuck entry must not end the pass
        log.warning("files sweep could not remove an entry", exc_info=True)
        return False


#: A `_trash/` entry is left to the purge that renamed it for this long. The
#: age is the entry's ctime — a rename updates ctime, not mtime, and a blob
#: directory's mtime can be months old at the moment it is moved aside.
TRASH_GRACE_S = 60.0


def _sweep_trash(report: SweepReport, now: float) -> None:
    try:
        entries = list(os.scandir(storage.trash_root()))
    except FileNotFoundError:
        return
    for entry in entries:
        try:
            changed = os.lstat(entry.path).st_ctime
        except OSError:
            continue
        if now - changed > TRASH_GRACE_S and _remove(entry.path):
            report.leftovers_removed += 1


def _sweep_leftovers(report: SweepReport, now: float) -> None:
    base = storage.root()
    _sweep_trash(report, now)
    max_age = limits.leftover_max_age_s()
    for sub in (storage.SINGLE_DIR, storage.INLINE_DIR):
        directory = os.path.join(base, sub)
        try:
            entries = list(os.scandir(directory))
        except FileNotFoundError:
            continue
        for entry in entries:
            if _older_than(entry.path, max_age, now) and _remove(entry.path):
                report.leftovers_removed += 1
    uploads_root = os.path.join(base, storage.UPLOADS_DIR)
    try:
        upload_entries = list(os.scandir(uploads_root))
    except FileNotFoundError:
        return
    orphan_age = limits.orphan_dir_min_age_s()
    for entry in upload_entries:
        if not entry.is_dir(follow_symlinks=False):
            if _older_than(entry.path, orphan_age, now) and _remove(entry.path):
                report.orphan_upload_dirs_removed += 1
            continue
        if not ids.is_upload_id(entry.name):
            if _older_than(entry.path, orphan_age, now) and _remove(entry.path):
                report.orphan_upload_dirs_removed += 1
            continue
        row = _upload_row_any_project(entry.name)
        keep = row is not None and (
            row["status"] in ("pending", "finalizing")
            or (row["status"] == "completed" and row.get("assembly_part_numbers") is not None)
        )
        if not keep:
            if _older_than(entry.path, orphan_age, now) and _remove(entry.path):
                report.orphan_upload_dirs_removed += 1
            continue
        # A live upload: only its stray temporaries go (a part body in flight
        # for 24 h does not exist — a 64 MiB part at the slow-link floor is 5 min).
        for base_dir, _dirs, files in os.walk(entry.path):
            for name in files:
                if name.endswith((".tmp", ".assembling")):
                    path = os.path.join(base_dir, name)
                    if _older_than(path, max_age, now) and _remove(path):
                        report.leftovers_removed += 1


def _upload_row_any_project(upload_id: str) -> Optional[dict]:
    """Server-internal read for the sweep (a directory name is not a caller id)."""
    with db.connection() as con:
        row = con.execute(
            "SELECT id, status, assembly_part_numbers FROM api_uploads WHERE id = %s", (upload_id,)
        ).fetchone()
    return dict(row) if row else None


def run_once(*, now: Optional[float] = None) -> SweepReport:
    """One bounded pass. BLOCKING: call via `db.run_in_thread`."""
    moment = time.time() if now is None else float(now)
    batch = limits.sweep_batch()
    report = SweepReport()
    report.finalizing_reset = schema.reset_stale_api_upload_finalizing(limits.finalizing_stale_s())
    report.expired = schema.expire_api_uploads(batch)
    for upload_id in report.expired:
        _remove(storage.upload_dir(upload_id))
    for upload in queue.abandoned_assemblies(batch):
        upload_id = str(upload["id"])
        if queue.clear_abandoned_assembly(upload_id):
            _remove(storage.upload_dir(upload_id))
            report.abandoned_assemblies.append(upload_id)
    report.purged_records = schema.purge_api_upload_records(limits.upload_record_ttl_s(), batch)
    for upload_id in report.purged_records:
        _remove(storage.upload_dir(upload_id))
    _sweep_leftovers(report, moment)
    if report.expired or report.finalizing_reset or report.abandoned_assemblies or report.leftovers_removed:
        log.info(
            "files upload sweep: expired=%d finalizing_reset=%d abandoned=%d purged=%d leftovers=%d orphans=%d",
            len(report.expired), report.finalizing_reset, len(report.abandoned_assemblies),
            len(report.purged_records), report.leftovers_removed, report.orphan_upload_dirs_removed,
        )
    return report


_task: Optional[asyncio.Task] = None


async def _loop(interval_s: float, on_pass) -> None:
    while True:
        try:
            report = await db.run_in_thread(run_once)
            if on_pass is not None:
                on_pass(report)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the sweep must outlive a bad pass
            log.warning("files upload sweep pass failed", exc_info=True)
        await asyncio.sleep(interval_s)


async def start(*, interval_s: Optional[float] = None, on_pass=None) -> None:
    """Start the periodic sweep (integration: `main.py` startup). Idempotent."""
    global _task
    if _task is not None and not _task.done():
        return
    storage.ensure_dirs()
    _task = asyncio.create_task(
        _loop(float(interval_s if interval_s is not None else limits.sweep_interval_s()), on_pass),
        name="apifiles-uploads-sweep",
    )


async def stop() -> None:
    global _task
    task, _task = _task, None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
