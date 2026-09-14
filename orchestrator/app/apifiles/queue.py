"""The entry into processing: ingest bytes as a blob, claim work under a lease,
and run the `assemble` stage that turns an upload's parts into a blob.

THE DATABASE ROW IS THE QUEUE. A blob in `queued`, or `processing` with a
lapsed lease, is due; a completed upload whose file still has
`assembling_upload_id` set is due for assembly. Nothing is held only in memory,
so a restart loses at most the in-flight unit and the next claim resumes it
(design §4.2, the `video/pipeline.py` precedent). The in-process hooks here
(`set_enqueue_hook`, `AssembleRunner.kick`) only wake a runner early.

WHY CLAIM SQL LIVES HERE AND NOT IN `db.py` (design §9.2): the same reason
`apiplatform/webhooks/queue.py` gives — the lease is this module's protocol,
and a reviewer should read the claim, the renewal and the release in one file.

FAIRNESS. Claims rank candidates per project and take at most one per project
per sweep, so one tenant's thousand uploads cannot starve another tenant's one
(the webhook queue's round-robin, same rationale).
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Sequence

from .. import db
from ..core import partfile
from . import limits, schema, sniff, storage

log = logging.getLogger(__name__)

#: `<host>:<pid>:<nonce>` — who holds a lease, readable in a support query.
OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

EnqueueHook = Callable[[dict], None]
_enqueue_hook: Optional[EnqueueHook] = None


def set_enqueue_hook(hook: Optional[EnqueueHook]) -> None:
    """Team B's `jobs.enqueue` (a wake-up, the row is already durable)."""
    global _enqueue_hook
    _enqueue_hook = hook


def _notify_enqueued(blob: dict) -> None:
    hook = _enqueue_hook
    if hook is None:
        return
    try:
        hook(blob)
    except Exception:  # noqa: BLE001 - a wake-up must never fail an upload
        log.warning("files enqueue hook failed for %s", blob.get("id"), exc_info=True)


# ------------------------------------------------------------------ ingest --


@dataclass(frozen=True)
class Ingested:
    file: dict
    blob: dict
    created: bool
    detection: sniff.Detection


def _placer(tmp_path: str, project_id: str, sha256: str) -> Callable[[dict, bool], None]:
    """Move `tmp_path` to the blob's `original` when the row is new or its
    bytes are missing; otherwise leave the tmp for the caller to discard.
    Same filesystem, so `os.replace` is a rename, never a copy."""

    def place(row: dict, created: bool) -> None:
        target = storage.original_path(project_id, sha256)
        if not created and os.path.exists(target):
            return
        storage.makedirs(os.path.dirname(target))
        os.chmod(tmp_path, storage.FILE_MODE)
        os.replace(tmp_path, target)
        storage.fsync_dir(os.path.dirname(target))

    return place


def detect_file(path: str, *, filename: str = "", mime_hint: str = "") -> sniff.Detection:
    """Never raises: an unreadable file or any classification failure is
    `unknown` (review 2026-09-13: a RecursionError from a sniff escaped
    `assemble_claimed` and left a full-size copy on disk per attempt)."""
    try:
        return sniff.detect_path(path, filename=filename, mime_hint=mime_hint)
    except OSError:
        return sniff.Detection(kind="unknown", mime_type="application/octet-stream", ext="", reason="unreadable")
    except Exception:  # noqa: BLE001 - total by contract
        log.warning("files sniff failed; recording the file as unknown", exc_info=True)
        return sniff.Detection(kind="unknown", mime_type="application/octet-stream", ext="", reason="sniff-error")


def ingest_single(
    *,
    project_id: str,
    workspace_id: str,
    key_id: Optional[str],
    tmp_path: str,
    sha256: str,
    bytes: int,
    filename: str,
    purpose: str,
    expires_after_seconds: Optional[int],
    mime_hint: str = "",
) -> Ingested:
    """`POST /v1/files`: the bytes at `tmp_path` become (or join) the project's
    blob and a new file references it. BLOCKING (a sniff read and a rename):
    call via `db.run_in_thread`. Raises `schema.BlobBeingPurged`."""
    detection = detect_file(tmp_path, filename=filename, mime_hint=mime_hint)
    file_row, blob, created = schema.create_api_file_with_blob(
        project_id, workspace_id, key_id,
        sha256=sha256, bytes=bytes, kind=_db_kind(detection), mime_type=detection.mime_type,
        lane=detection.lane, filename=filename, purpose=purpose, origin="file",
        expires_after_seconds=expires_after_seconds,
        place_bytes=_placer(tmp_path, project_id, sha256),
    )
    storage.discard(tmp_path)  # a dedupe hit left it; a create already moved it
    if created:
        _notify_enqueued(blob)
    return Ingested(file=file_row, blob=blob, created=created, detection=detection)


def purge_blob_bytes(blob_id: str) -> bool:
    """BLOCKING. Retire a `deleting` blob by id and remove its bytes.

    `schema.retire_deleting_blob` renames `<project>/<sha256>/` into `_trash/`
    and deletes the row in one transaction under the row's lock; the rmtree
    runs on the trash path after the commit. A second, stale or concurrent call
    for the same blob id finds no row and touches nothing — so it can never
    delete the bytes of a NEWER blob of the same content (review finding,
    2026-09-13: "delete a file, upload the same bytes again" lost the new
    file's bytes when two purges ran). True when this call retired the row.
    Team B's `retention.purge_blob` should end with this instead of its own
    rmtree + `delete_api_file_blob` (integration item); the HTTP layer's
    default purge (`routes.purge_blob_local`) is exactly this."""
    retired = schema.retire_deleting_blob(str(blob_id), move_aside=storage.move_blob_dir_aside)
    if retired is None:
        return False
    _row, trash = retired
    try:
        storage.remove_trash(trash)
    except Exception:  # noqa: BLE001 - the row is gone; the upload sweep clears `_trash/`
        log.warning("files purge left %s in the trash", trash, exc_info=True)
    return True


def _db_kind(detection: sniff.Detection) -> str:
    return detection.kind if detection.kind in sniff.KINDS else "unknown"


# ---------------------------------------------------- blob processing claim --


def claim_due_blobs(lane: str, limit: int, *, owner: str = OWNER, lease_s: Optional[float] = None) -> List[dict]:
    """Claim up to `limit` due blobs of `lane`, at most one per project, under a
    lease (design §4.2). Due: `queued` and `not_before` reached, or
    `processing` with a lapsed lease. For team B's runner."""
    lease = float(lease_s if lease_s is not None else limits.processing_lease_s())
    bounded = max(1, min(int(limit), 200))
    with db.connection() as con:
        picked = [
            str(row["id"])
            for row in con.execute(
                "WITH ranked AS ("
                "  SELECT id, updated_at, row_number() OVER (PARTITION BY project_id ORDER BY updated_at, id) AS rn "
                "    FROM api_file_blobs "
                "   WHERE lane = %s AND ((status = 'queued' AND (not_before IS NULL OR not_before <= now())) "
                "         OR (status = 'processing' AND lease_expires_at < now()))"
                ") SELECT id FROM ranked WHERE rn = 1 ORDER BY updated_at, id LIMIT %s",
                (lane, bounded),
            ).fetchall()
        ]
        if not picked:
            return []
        rows = con.execute(
            "UPDATE api_file_blobs SET status = 'processing', lease_owner = %s, "
            "       lease_expires_at = now() + make_interval(secs => %s::float8), "
            "       started_at = COALESCE(started_at, now()), updated_at = now() "
            " WHERE id IN (SELECT id FROM api_file_blobs WHERE id = ANY(%s) "
            "                 AND ((status = 'queued' AND (not_before IS NULL OR not_before <= now())) "
            "                      OR (status = 'processing' AND lease_expires_at < now())) "
            "               FOR UPDATE SKIP LOCKED) "
            "RETURNING *",
            (owner, lease, picked),
        ).fetchall()
    return [dict(row) for row in rows]


def renew_blob_leases(blob_ids: Sequence[str], *, owner: str = OWNER, lease_s: Optional[float] = None) -> List[str]:
    """Renew in ONE statement; returns the ids still held (a lost lease is
    absent — its task stops at the next unit boundary)."""
    if not blob_ids:
        return []
    lease = float(lease_s if lease_s is not None else limits.processing_lease_s())
    with db.connection() as con:
        rows = con.execute(
            "UPDATE api_file_blobs SET lease_expires_at = now() + make_interval(secs => %s::float8) "
            " WHERE id = ANY(%s) AND lease_owner = %s AND status = 'processing' RETURNING id",
            (lease, list(blob_ids), owner),
        ).fetchall()
    return [str(r["id"]) for r in rows]


def release_blob(blob_id: str, *, owner: str = OWNER, status: str, **fields) -> Optional[dict]:
    """End a claim: `processed` or `failed` (terminal) — only by the holder."""
    if status not in ("processed", "failed", "queued"):
        raise ValueError("release status must be processed, failed or queued")
    assignments = ["status = %s", "lease_owner = NULL", "lease_expires_at = NULL", "updated_at = now()"]
    values: list = [status]
    if status == "processed":
        assignments.append("processed_at = now()")
    for name in sorted(fields):
        if name not in schema._BLOB_UPDATABLE or name in ("status", "lease_owner", "lease_expires_at"):
            raise ValueError(f"not releasable: {name}")
        assignments.append(f"{name} = %s")
        values.append(schema.Jsonb(fields[name]) if name in schema._JSON_FIELDS else fields[name])
    with db.connection() as con:
        row = con.execute(
            f"UPDATE api_file_blobs SET {', '.join(assignments)} "
            " WHERE id = %s AND lease_owner = %s AND status = 'processing' RETURNING *",
            (*values, blob_id, owner),
        ).fetchone()
    return dict(row) if row else None


def defer_blob(blob_id: str, *, owner: str = OWNER, delay_s: Optional[float] = None, max_attempts: Optional[int] = None) -> Optional[dict]:
    """Engine or disk unavailable: back to `queued`, finished stages kept,
    `attempt += 1`, not due before now + delay; after `max_attempts` it fails
    with `processing_unavailable` (design §4.2)."""
    delay = float(delay_s if delay_s is not None else limits.processing_retry_delay_s())
    ceiling = int(max_attempts if max_attempts is not None else limits.processing_max_attempts())
    with db.connection() as con:
        row = con.execute(
            "UPDATE api_file_blobs SET attempt = attempt + 1, lease_owner = NULL, lease_expires_at = NULL, "
            "       updated_at = now(), "
            "       status = CASE WHEN attempt + 1 >= %s THEN 'failed' ELSE 'queued' END, "
            "       error_code = CASE WHEN attempt + 1 >= %s THEN 'processing_unavailable' ELSE error_code END, "
            "       not_before = CASE WHEN attempt + 1 >= %s THEN not_before "
            "                         ELSE now() + make_interval(secs => %s::float8) END "
            " WHERE id = %s AND lease_owner = %s AND status = 'processing' RETURNING *",
            (ceiling, ceiling, ceiling, delay, blob_id, owner),
        ).fetchone()
    return dict(row) if row else None


# ----------------------------------------------------------------- assembly --


def claim_assembly(
    *, owner: str = OWNER, lease_s: Optional[float] = None, project_ids: Optional[Sequence[str]] = None
) -> Optional[dict]:
    """Claim one completed upload whose file is still assembling, fairly across
    projects; `assembly_attempts += 1`. Returns the upload row (with `file_id`).

    The row's `assembly_lease_owner` is `<owner>#<claim nonce>`, unique PER
    CLAIM (review 2026-09-13): every runner in a process shares `owner`, so an
    owner-only lease let a stalled worker whose lease lapsed keep renewing
    while a second worker of the same process copied the same upload again.
    `renew_assembly`, the attach and the release all match the per-claim value,
    which `assemble_claimed` reads from the row.

    `project_ids` narrows the claim to those projects — an operator draining
    one tenant, and the test suite, which shares one database between tests
    and must never adopt another test's upload."""
    lease = float(lease_s if lease_s is not None else limits.assembly_lease_s())
    scope_sql = "" if project_ids is None else " AND u.project_id = ANY(%s)"
    scope_params: tuple = () if project_ids is None else (list(project_ids),)
    with db.connection() as con:
        candidates = [
            str(row["id"])
            for row in con.execute(
                "WITH ranked AS ("
                "  SELECT u.id, u.completed_at, "
                "         row_number() OVER (PARTITION BY u.project_id ORDER BY u.completed_at, u.id) AS rn "
                "    FROM api_uploads u JOIN api_files f ON f.id = u.file_id "
                "   WHERE u.status = 'completed' AND u.assembly_part_numbers IS NOT NULL "
                "     AND f.assembling_upload_id = u.id AND f.deleted_at IS NULL "
                "     AND (u.assembly_lease_expires_at IS NULL OR u.assembly_lease_expires_at < now())"
                + scope_sql +
                ") SELECT id FROM ranked ORDER BY rn, completed_at, id LIMIT 8",
                scope_params,
            ).fetchall()
        ]
        if not candidates:
            return None
        row = con.execute(
            "UPDATE api_uploads SET assembly_lease_owner = %s, "
            "       assembly_lease_expires_at = now() + make_interval(secs => %s::float8), "
            "       assembly_attempts = assembly_attempts + 1, updated_at = now() "
            " WHERE id = (SELECT id FROM api_uploads WHERE id = ANY(%s) AND status = 'completed' "
            "               AND assembly_part_numbers IS NOT NULL "
            "               AND (assembly_lease_expires_at IS NULL OR assembly_lease_expires_at < now()) "
            "             ORDER BY array_position(%s::text[], id) LIMIT 1 FOR UPDATE SKIP LOCKED) "
            "RETURNING *",
            (_claim_owner(owner), lease, candidates, candidates),
        ).fetchone()
    return dict(row) if row else None


def _claim_owner(owner: str) -> str:
    return f"{owner}#{uuid.uuid4().hex[:12]}"


def renew_assembly(upload_id: str, bytes_done: int, *, owner: str = OWNER, lease_s: Optional[float] = None) -> bool:
    """Renew the lease and publish progress; False when the lease is lost or
    the file was deleted (the copy then stops)."""
    lease = float(lease_s if lease_s is not None else limits.assembly_lease_s())
    with db.connection() as con:
        row = con.execute(
            "UPDATE api_uploads u SET assembly_bytes_done = %s, "
            "       assembly_lease_expires_at = now() + make_interval(secs => %s::float8) "
            " WHERE u.id = %s AND u.assembly_lease_owner = %s AND u.assembly_part_numbers IS NOT NULL "
            "   AND EXISTS (SELECT 1 FROM api_files f WHERE f.id = u.file_id AND f.deleted_at IS NULL "
            "               AND f.assembling_upload_id = u.id) "
            "RETURNING u.id",
            (int(bytes_done), lease, upload_id, owner),
        ).fetchone()
    return row is not None


def abandoned_assemblies(limit: int) -> List[dict]:
    """Completed uploads still holding assembly columns whose file is gone or
    no longer assembling from them (deleted mid-assembly) and whose lease is
    free: their parts are removed and the columns cleared by the caller."""
    with db.connection() as con:
        return [
            dict(r)
            for r in con.execute(
                "SELECT u.* FROM api_uploads u LEFT JOIN api_files f ON f.id = u.file_id "
                " WHERE u.status = 'completed' AND u.assembly_part_numbers IS NOT NULL "
                "   AND (u.assembly_lease_expires_at IS NULL OR u.assembly_lease_expires_at < now()) "
                "   AND (f.id IS NULL OR f.deleted_at IS NOT NULL OR f.assembling_upload_id IS DISTINCT FROM u.id) "
                " ORDER BY u.completed_at LIMIT %s",
                (max(1, int(limit)),),
            ).fetchall()
        ]


def clear_abandoned_assembly(upload_id: str) -> bool:
    with db.connection() as con:
        row = con.execute(
            "UPDATE api_uploads u SET assembly_part_numbers = NULL, assembly_lease_owner = NULL, "
            "       assembly_lease_expires_at = NULL, updated_at = now() "
            " WHERE u.id = %s AND u.status = 'completed' AND u.assembly_part_numbers IS NOT NULL "
            "   AND (u.assembly_lease_expires_at IS NULL OR u.assembly_lease_expires_at < now()) "
            "   AND NOT EXISTS (SELECT 1 FROM api_files f WHERE f.id = u.file_id AND f.deleted_at IS NULL "
            "                   AND f.assembling_upload_id = u.id) "
            "RETURNING u.id",
            (upload_id,),
        ).fetchone()
    return row is not None


@dataclass(frozen=True)
class AssemblyResult:
    """`outcome`: `attached` (the file now has a blob), `checksum_mismatch`,
    `failed` (internal_error after the attempts), `deferred` (disk below the
    watermark or a part unreadable — the lease is released for a later try),
    `abandoned` (deleted meanwhile or lease lost)."""

    outcome: str
    upload_id: str
    file: Optional[dict] = None
    blob: Optional[dict] = None
    created: bool = False
    assembly_ms: int = 0


#: A deferred assembly is not re-claimed for this long: the watermark or a
#: mid-purge blob does not clear in the 5 s poll, and re-claiming at poll speed
#: would spin a worker on it.
ASSEMBLY_DEFER_S = 60.0


def _release_assembly_lease(upload_id: str, owner: str, *, defer_s: float = 0.0, refund_attempt: bool = False) -> None:
    """Give the claim back. `defer_s` keeps it unclaimable for that long (the
    claim predicate is a NULL or lapsed expiry); `refund_attempt` undoes the
    claim's `assembly_attempts += 1` for a deferral that was not the upload's
    fault (disk below the watermark, a purge in flight), so a full disk can
    never exhaust the attempts and fail a good upload."""
    with db.connection() as con:
        con.execute(
            "UPDATE api_uploads SET assembly_lease_owner = NULL, "
            "       assembly_lease_expires_at = CASE WHEN %s::float8 > 0 "
            "            THEN now() + make_interval(secs => %s::float8) ELSE NULL END, "
            "       assembly_attempts = GREATEST(0, assembly_attempts - %s), updated_at = now() "
            " WHERE id = %s AND assembly_lease_owner = %s",
            (float(defer_s), float(defer_s), 1 if refund_attempt else 0, upload_id, owner),
        )


def _still_assembling(project_id: str, file_id: str, upload_id: str) -> bool:
    row = schema.get_api_file(project_id, file_id)
    return row is not None and row.get("assembling_upload_id") == upload_id


def _remove_upload_dir(upload_id: str) -> None:
    try:
        storage.remove_tree(storage.upload_dir(upload_id))
    except Exception:  # noqa: BLE001 - the sweep retries leftovers
        log.warning("could not remove the parts of %s", upload_id, exc_info=True)


class _LeaseKeeper:
    """Renews an assembly lease on a timer, independent of bytes copied.

    WHY (review 2026-09-13): renewal used to ride only on the 256 MiB progress
    tick. The final fsync of a 100 GiB copy can flush GiBs of dirty pages with
    no byte moving; a lease that lapses then lets another claimant start a
    duplicate copy (2x disk, 2x CPU) while this one still believes it holds it.
    A lost lease or a deleted file flips `alive[0]` to False, which the copy
    loop sees within one 1 MiB buffer."""

    def __init__(self, upload_id: str, owner: str, alive: List[bool], progress: List[int], interval_s: float) -> None:
        self._upload_id = upload_id
        self._owner = owner
        self._alive = alive
        self._progress = progress
        self._interval_s = max(0.05, float(interval_s))
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="apifiles-assembly-lease", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stopped.wait(self._interval_s):
            if not self._alive[0]:
                return
            try:
                if not renew_assembly(self._upload_id, self._progress[0], owner=self._owner):
                    self._alive[0] = False
                    return
            except Exception:  # noqa: BLE001 - a database blip: the lease has two more ticks of slack
                log.warning("files assembly lease renewal failed for %s", self._upload_id, exc_info=True)


def assemble_claimed(
    upload: dict, *, owner: str = OWNER, stop_event: Optional[threading.Event] = None
) -> AssemblyResult:
    """The `assemble` stage for a claimed upload. BLOCKING: run in a thread.

    1. The watermark is re-checked before the copy (finding #17).
    2. `partfile.assemble` copies the listed parts in the order `complete`
       stored, hashing sha256 (+ md5 when a checksum was supplied) in the same
       pass and checking each part against the sha256 its row recorded. The
       lease is renewed on a timer and on every 256 MiB of progress.
    3. A supplied checksum that does not match fails the FILE with
       `checksum_mismatch`; the parts and the copy are removed. A part that
       does not match its own row fails it with `internal_error`.
    4. Otherwise the blob is created or joined inside the project and the file
       attached; the parts directory is removed LAST, after the row says the
       file has its blob, so a crash anywhere before leaves parts to resume.

    `stop_event` (a `threading.Event`) is process shutdown: the copy stops
    within one 1 MiB buffer, the lease is released at once with the attempt
    refunded, and the outcome is `deferred` — so a restart re-claims the upload
    immediately instead of after the 90 s lease, and a deploy is not held by a
    7-minute copy (review 2026-09-13; design §4.2 lists shutdown as a unit
    boundary). The lease holder is the row's per-claim `assembly_lease_owner`;
    `owner` is only the fallback for a row without one.

    Whatever raises after the copy, the copy is removed and the lease released
    before the exception leaves (review 2026-09-13: a sniff RecursionError left
    a full-size copy per attempt and held the lease).
    """
    upload_id = str(upload["id"])
    project_id = str(upload["project_id"])
    file_id = str(upload["file_id"])
    lease_owner = str(upload.get("assembly_lease_owner") or owner)
    started = time.monotonic()
    attempts = int(upload.get("assembly_attempts") or 0)
    if attempts > limits.assembly_max_attempts():
        failed = schema.fail_assembling_file(project_id, file_id, upload_id, "internal_error")
        _remove_upload_dir(upload_id)
        log.warning("assembly of %s failed after %d attempts", upload_id, attempts - 1)
        return AssemblyResult("failed", upload_id, file=failed)
    if stop_event is not None and stop_event.is_set():
        _release_assembly_lease(upload_id, lease_owner, refund_attempt=True)
        return AssemblyResult("deferred", upload_id)
    numbers = [int(n) for n in (upload.get("assembly_part_numbers") or [])]
    parts = {int(p["part_number"]): p for p in schema.list_api_upload_parts(project_id, upload_id)}
    if any(n not in parts for n in numbers):
        failed = schema.fail_assembling_file(project_id, file_id, upload_id, "internal_error")
        _remove_upload_dir(upload_id)
        return AssemblyResult("failed", upload_id, file=failed)
    total = sum(int(parts[n]["bytes"]) for n in numbers)
    try:
        storage.require_free(total)
    except storage.StorageUnavailable:
        _release_assembly_lease(upload_id, lease_owner, defer_s=ASSEMBLY_DEFER_S, refund_attempt=True)
        return AssemblyResult("deferred", upload_id)
    want_md5 = bool(upload.get("expected_md5"))
    dest = storage.assembled_tmp_path(upload_id, uuid.uuid4().hex[:12])
    alive = [True]
    progress = [0]

    def on_progress(done: int) -> None:
        progress[0] = done
        if alive[0]:
            alive[0] = renew_assembly(upload_id, done, owner=lease_owner)

    def shutting_down() -> bool:
        return stop_event is not None and stop_event.is_set()

    def should_stop() -> bool:
        return not alive[0] or shutting_down()

    keeper = _LeaseKeeper(upload_id, lease_owner, alive, progress, limits.assembly_renew_s())
    keeper.start()
    try:
        try:
            assembled = partfile.assemble(
                [storage.part_path(upload_id, n) for n in numbers],
                dest,
                want_md5=want_md5,
                expected_sizes=[int(parts[n]["bytes"]) for n in numbers],
                expected_sha256s=[parts[n].get("sha256") for n in numbers],
                on_progress=on_progress,
                should_stop=should_stop,
            )
        except partfile.AssemblyStopped:
            if alive[0] and shutting_down():
                _release_assembly_lease(upload_id, lease_owner, refund_attempt=True)
                return AssemblyResult("deferred", upload_id)
            if not _still_assembling(project_id, file_id, upload_id):
                _remove_upload_dir(upload_id)
                clear_abandoned_assembly(upload_id)
            return AssemblyResult("abandoned", upload_id)
        except partfile.PartChecksumMismatch as mismatch:
            # Permanent: the bytes on disk are not the bytes the resume view
            # promised. Failing loudly beats stitching them in.
            log.warning("assembly of %s: part index %d disagrees with its row", upload_id, mismatch.index)
            failed = schema.fail_assembling_file(project_id, file_id, upload_id, "internal_error")
            _remove_upload_dir(upload_id)
            return AssemblyResult("failed", upload_id, file=failed, assembly_ms=int((time.monotonic() - started) * 1000))
        except partfile.AssemblyError:
            # A part file missing or short on disk. Released for another attempt;
            # `assembly_max_attempts` turns a permanent loss into `internal_error`.
            log.warning("assembly of %s could not read its parts", upload_id, exc_info=True)
            _release_assembly_lease(upload_id, lease_owner, defer_s=ASSEMBLY_DEFER_S)
            return AssemblyResult("deferred", upload_id)
        except OSError as exc:
            if storage.is_enospc(exc):
                _release_assembly_lease(upload_id, lease_owner, defer_s=ASSEMBLY_DEFER_S, refund_attempt=True)
                return AssemblyResult("deferred", upload_id)
            _release_assembly_lease(upload_id, lease_owner, defer_s=ASSEMBLY_DEFER_S)
            raise
        try:
            return _attach_assembled(upload, assembled, dest, lease_owner, started)
        except BaseException:
            storage.discard(dest)
            try:
                _release_assembly_lease(upload_id, lease_owner, defer_s=ASSEMBLY_DEFER_S)
            except Exception:  # noqa: BLE001 - the lease lapses on its own; the original error matters
                log.warning("could not release the assembly lease of %s", upload_id, exc_info=True)
            raise
    finally:
        keeper.stop()


def _attach_assembled(
    upload: dict, assembled: partfile.Assembled, dest: str, lease_owner: str, started: float
) -> AssemblyResult:
    """Steps 3–4 of `assemble_claimed`, after a whole copy exists at `dest`."""
    upload_id = str(upload["id"])
    project_id = str(upload["project_id"])
    file_id = str(upload["file_id"])
    elapsed_ms = int((time.monotonic() - started) * 1000)
    expected_md5 = upload.get("expected_md5")
    expected_sha = upload.get("expected_sha256")
    if (expected_md5 and assembled.md5 != expected_md5) or (expected_sha and assembled.sha256 != expected_sha):
        storage.discard(dest)
        failed = schema.fail_assembling_file(project_id, file_id, upload_id, "checksum_mismatch")
        _remove_upload_dir(upload_id)
        return AssemblyResult("checksum_mismatch", upload_id, file=failed, assembly_ms=elapsed_ms)
    detection = detect_file(dest, filename=str(upload.get("filename") or ""), mime_hint=str(upload.get("mime_type") or ""))
    try:
        attached = schema.attach_blob_to_assembling_file(
            project_id, file_id, upload_id,
            lease_owner=lease_owner, sha256=assembled.sha256, bytes=assembled.bytes,
            kind=_db_kind(detection), mime_type=detection.mime_type, lane=detection.lane,
            place_bytes=_placer(dest, project_id, assembled.sha256),
        )
    except schema.BlobBeingPurged:
        # The project's previous copy of these bytes is mid-purge; try again
        # once it is gone (the purge is seconds).
        storage.discard(dest)
        _release_assembly_lease(upload_id, lease_owner, defer_s=5.0, refund_attempt=True)
        return AssemblyResult("deferred", upload_id)
    if attached is None:
        storage.discard(dest)
        # Deleted meanwhile → the parts go. Lease lost → another claimant owns
        # the parts now; touch nothing of theirs.
        if not _still_assembling(project_id, file_id, upload_id):
            _remove_upload_dir(upload_id)
            clear_abandoned_assembly(upload_id)
        return AssemblyResult("abandoned", upload_id)
    file_row, blob, created = attached
    storage.discard(dest)
    _remove_upload_dir(upload_id)
    if created:
        _notify_enqueued(blob)
    return AssemblyResult("attached", upload_id, file=file_row, blob=blob, created=created, assembly_ms=elapsed_ms)


class AssembleRunner:
    """Runs the `assemble` stage in the cpu lane: at most `concurrency`
    assemblies at once, each a worker thread, woken by `kick()` after a
    `complete` and by a poll (lapsed leases after a crash, other processes'
    work). Interim home until team B's `jobs.py` runs `assemble` as its first
    stage; it calls `assemble_claimed` either way.

    `stop()` sets the runner's `threading.Event`, which every in-flight copy
    checks after each 1 MiB, then waits up to `grace_s` for the worker threads
    to hand their leases back. Cancelling the asyncio task alone never stopped
    the `to_thread` copy: the reviewer's probe held interpreter exit for the
    whole copy (8.01 s for an 8 s stand-in; a 100 GiB assembly is ~7 min).
    """

    def __init__(
        self, *, concurrency: Optional[int] = None, poll_s: float = 5.0, project_ids: Optional[Sequence[str]] = None
    ) -> None:
        self._concurrency = max(1, int(concurrency if concurrency is not None else limits.cpu_jobs()))
        self.project_ids = None if project_ids is None else list(project_ids)
        self._poll_s = float(poll_s)
        self._wake = asyncio.Event()
        self._tasks: List[asyncio.Task] = []
        self._stopping = False
        self._stop_event = threading.Event()
        self.completed: List[AssemblyResult] = []
        self.on_result: Optional[Callable[[AssemblyResult], Awaitable[None]]] = None

    def kick(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        if self._tasks:
            return
        self._stopping = False
        self._stop_event.clear()
        self._tasks = [asyncio.create_task(self._worker(i), name=f"apifiles-assemble-{i}") for i in range(self._concurrency)]

    async def stop(self, *, grace_s: float = 30.0) -> None:
        self._stopping = True
        self._stop_event.set()
        self._wake.set()
        tasks, self._tasks = list(self._tasks), []
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=max(0.0, float(grace_s)))
        for task in pending:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def run_once(self) -> Optional[AssemblyResult]:
        """Claim and run one assembly now (tests, and the worker loop)."""
        if self._stop_event.is_set():
            return None
        upload = await db.run_in_thread(functools.partial(claim_assembly, project_ids=self.project_ids))
        if upload is None:
            return None
        result = await asyncio.to_thread(assemble_claimed, upload, stop_event=self._stop_event)
        self.completed.append(result)
        del self.completed[:-64]
        if self.on_result is not None:
            try:
                await self.on_result(result)
            except Exception:  # noqa: BLE001
                log.warning("assembly result hook failed", exc_info=True)
        return result

    async def _worker(self, index: int) -> None:
        while not self._stopping:
            try:
                result = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one broken assembly must not stop the lane
                log.warning("files assembly worker %d failed a unit", index, exc_info=True)
                result = None
            if self._stopping:
                return
            if result is not None and result.outcome != "deferred":
                continue
            self._wake.clear()
            if self._stopping:
                return
            try:
                async with asyncio.timeout(self._poll_s):
                    await self._wake.wait()
            except asyncio.TimeoutError:
                pass
