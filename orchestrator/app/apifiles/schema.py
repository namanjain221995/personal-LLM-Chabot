"""The Files API tables (migration V37 in db.py) and the accessors every module uses.

WHERE THE DDL LIVES (files-hookup, 2026-09-13). The tables are migration V37 in
`db.py`'s `_MIGRATIONS` chain, so `db.init_schema` creates them at start-up like
every other table. `MIGRATION_SQL` below is that same string, and
`ensure_schema()` still applies it idempotently for the suites that use a
private database without the lifespan. The accessors keep the names and
contracts of design §9.2.

NUMBERING. V36 is the no-timeout durable-generation migration; the files tables
are their own migration so neither can half-apply the other. See the comment at
the top of `db._MIGRATION_V37`, including the shape guard.

ISOLATION (design §7.2). Every accessor that a caller-supplied id can reach
takes `project_id` and puts it in the WHERE clause, and "absent" and "another
project's" return the same None. `PROJECT_SCOPED_ACCESSORS` lists them for the
isolation test, which calls each with a foreign project. Accessors keyed only
by a blob id (`get_api_file_blob`, `update_api_file_blob`, …) are
server-internal: no route passes a caller's id to them.

TIMESTAMPS stay `datetime` in the rows returned here (the wire renders epoch
seconds); `db._row`'s ISO strings would be parsed straight back.

DEVIATIONS FROM THE DESIGN DDL, each needed and each additive:
* `api_files.error_code` also allows `internal_error`: an upload whose part file
  is gone from disk can never assemble, and without a terminal code its file
  would sit at `uploaded / assemble` forever while `wait_for_processing` spins.
* `api_uploads` carries the assembly's durable inputs and lease —
  `assembly_part_numbers` (the ORDER `part_ids` named; a sequential upload's
  order is not `part_number` order), `expected_md5` / `expected_sha256` (the
  checksums `complete` accepted, checked by the `assemble` stage after a
  crash too), `assembly_bytes_done` (the File object's percent),
  `assembly_attempts`, `assembly_lease_owner` / `assembly_lease_expires_at`
  (so two processes never assemble one upload, and a crashed assembly is
  re-claimed when its lease lapses instead of by an idle timer).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from psycopg.types.json import Jsonb

from .. import db
from . import ids

log = logging.getLogger(__name__)

SCHEMA_VERSION = db.FILES_MIGRATION_VERSION

#: Distinct from `db._MIGRATION_LOCK_KEY`: this is only for `ensure_schema`
#: racing itself (two test workers, two processes at start-up).
_ENSURE_LOCK_KEY = 0x41504946  # "APIF"

PURPOSES = ("user_data", "assistants", "vision")
FILE_ERROR_CODES = ("checksum_mismatch", "internal_error")

#: MOVED 2026-09-13 (files-hookup): the DDL lives in `db.py`'s migration chain
#: as V37 and is applied by `db.init_schema` at start-up. This name stays so the
#: tests' `ensure_schema()` (a private database, no lifespan) and anything that
#: reads the text keep working — and it is the SAME string, not a copy.
MIGRATION_SQL = db.FILES_MIGRATION_SQL

#: The tables this module owns, children first — for a test fixture's TRUNCATE
#: and for `conftest._APP_TABLES` (integration item).
TABLES = ("api_upload_parts", "api_files", "api_uploads", "api_file_blobs")


def ensure_schema() -> None:
    """Apply `MIGRATION_SQL`. Idempotent; serialised by an advisory lock."""
    with db.connection() as con:
        with con.transaction():
            con.execute("SELECT pg_advisory_xact_lock(%s)", (_ENSURE_LOCK_KEY,))
            con.execute(MIGRATION_SQL)


# ----------------------------------------------------------------- helpers --


class BlobBeingPurged(Exception):
    """The project's blob for these bytes is being deleted. The caller waits
    for (or runs) the purge, then creates a fresh blob."""

    def __init__(self, blob: dict) -> None:
        super().__init__("blob is being purged")
        self.blob = blob


class UnknownCursor(Exception):
    """`after` names an id that never existed in this project."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _one(con, sql: str, params: Sequence[Any]) -> Optional[dict]:
    row = con.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def _all(con, sql: str, params: Sequence[Any]) -> List[dict]:
    return [dict(row) for row in con.execute(sql, params).fetchall()]


def _jsonb(value: Any) -> Optional[Jsonb]:
    return None if value is None else Jsonb(value)


_FILE_SELECT = (
    "SELECT f.*, "
    "       b.sha256 AS blob_sha256, b.kind AS blob_kind, b.mime_type AS blob_mime_type, "
    "       b.status AS blob_status, b.stage AS blob_stage, b.stages AS blob_stages, "
    "       b.progress AS blob_progress, b.facts AS blob_facts, b.error_code AS blob_error_code, "
    "       b.started_at AS blob_started_at, b.processed_at AS blob_processed_at, "
    "       b.derived_bytes AS blob_derived_bytes, b.lane AS blob_lane, b.attempt AS blob_attempt, "
    "       b.video_analysis_id AS blob_video_analysis_id, b.bytes AS blob_bytes, "
    "       u.assembly_bytes_done AS assembly_bytes_done, u.completed_at AS assembly_started_at, "
    "       u.assembly_lease_expires_at AS assembly_lease_expires_at "
    "  FROM api_files f "
    "  LEFT JOIN api_file_blobs b ON b.id = f.blob_id "
    "  LEFT JOIN api_uploads u ON u.id = f.assembling_upload_id "
)

_LIVE = "f.deleted_at IS NULL AND (f.expires_at IS NULL OR f.expires_at > now())"


# ------------------------------------------------------------------ uploads --


def create_api_upload(
    project_id: str,
    workspace_id: str,
    key_id: Optional[str],
    filename: str,
    purpose: str,
    mime_type: str,
    bytes: int,
    file_expires_after_seconds: Optional[int],
    *,
    idle_ttl_s: float,
    max_ttl_s: float,
    upload_id: Optional[str] = None,
) -> dict:
    """A `pending` upload. `expires_at = now + min(idle, max)` (§7.3)."""
    new_id = upload_id or ids.new_upload_id()
    with db.connection() as con:
        return _one(
            con,
            "INSERT INTO api_uploads (id, project_id, workspace_id, key_id, filename, purpose, "
            "                         mime_type, bytes, file_expires_after_seconds, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "        now() + make_interval(secs => LEAST(%s::float8, %s::float8))) "
            "RETURNING *",
            (
                new_id, project_id, workspace_id, key_id or None, filename, purpose,
                mime_type or "", int(bytes), file_expires_after_seconds,
                float(idle_ttl_s), float(max_ttl_s),
            ),
        )  # type: ignore[return-value]


def get_api_upload(project_id: str, upload_id: str) -> Optional[dict]:
    """Project-scoped; `lapsed` is true for a pending upload past `expires_at`
    that the sweep has not reached yet (it is reported and treated as expired)."""
    with db.connection() as con:
        return _one(
            con,
            "SELECT *, (status = 'pending' AND expires_at <= now()) AS lapsed "
            "  FROM api_uploads WHERE id = %s AND project_id = %s",
            (upload_id, project_id),
        )


def list_api_upload_parts(project_id: str, upload_id: str) -> List[dict]:
    """Ordered by part_number; project-scoped through the upload row."""
    with db.connection() as con:
        return _all(
            con,
            "SELECT p.* FROM api_upload_parts p "
            "  JOIN api_uploads u ON u.id = p.upload_id AND u.project_id = %s "
            " WHERE p.upload_id = %s ORDER BY p.part_number",
            (project_id, upload_id),
        )


def next_api_upload_part_number(project_id: str, upload_id: str) -> Optional[int]:
    """Informational: the number a sequential part would get now. The real
    assignment happens under the row lock in `upsert_api_upload_part`."""
    with db.connection() as con:
        row = _one(
            con,
            "SELECT COALESCE(max(p.part_number) + 1, 0) AS n FROM api_uploads u "
            "  LEFT JOIN api_upload_parts p ON p.upload_id = u.id "
            " WHERE u.id = %s AND u.project_id = %s GROUP BY u.id",
            (upload_id, project_id),
        )
    return None if row is None else int(row["n"])


@dataclass(frozen=True)
class PartOutcome:
    """`state`: `ok`, `gone` (absent / other project), `state` (not pending),
    `mode` (numbering mode conflict), `budget` (over the part budget),
    `parts_full` (a sequential part past max_parts)."""

    state: str
    part: Optional[dict] = None
    upload: Optional[dict] = None


def upsert_api_upload_part(
    project_id: str,
    upload_id: str,
    *,
    part_number: Optional[int],
    bytes: int,
    sha256: str,
    mode: str,
    part_max_bytes: int,
    max_parts: int,
    upload_max_bytes: int,
    idle_ttl_s: float,
    max_ttl_s: float,
    before_commit: Callable[[int], None],
) -> PartOutcome:
    """Record one part under the upload row lock.

    ORDER (the V29 ordering, tightened): lock the upload row → it must still be
    `pending` and unlapsed → the numbering mode must match (finding #5) → the
    number is assigned (sequential) or taken (numbered) → the part budget →
    `before_commit(n)` renames the temporary file into `parts/<n>` → the row
    is upserted → commit. The rename runs while the row lock is held, so a
    `complete` or `cancel` that wins the row after this commit always finds the
    part file, and one that won before it makes this call return `state`
    without touching the directory. A retry of a numbered part keeps its id
    (`ON CONFLICT … DO UPDATE` never changes `id`), so a lost acknowledgement
    creates no orphan.

    THE BUDGET (finding #18) is disk-based, not the declared size: accepted
    bytes plus this part may not exceed
    `min(upload_max × 1.1, declared + (max_parts − accepted_parts) × part_max)`.
    `complete`'s exact byte sum over the listed parts is what guarantees
    correctness; this only stops an upload from filling the disk.
    """
    if mode not in ("numbered", "sequential"):
        raise ValueError("mode must be numbered or sequential")
    with db.connection() as con:
        upload = _one(
            con,
            "SELECT *, (expires_at <= now()) AS lapsed FROM api_uploads "
            " WHERE id = %s AND project_id = %s FOR UPDATE",
            (upload_id, project_id),
        )
        if upload is None:
            return PartOutcome("gone")
        if upload["status"] != "pending" or upload["lapsed"]:
            return PartOutcome("state", upload=upload)
        if upload.get("part_mode") and upload["part_mode"] != mode:
            return PartOutcome("mode", upload=upload)
        totals = _one(
            con,
            "SELECT COALESCE(max(part_number) + 1, 0) AS next_number, count(*) AS parts, "
            "       COALESCE(sum(bytes), 0) AS accepted "
            "  FROM api_upload_parts WHERE upload_id = %s",
            (upload_id,),
        ) or {"next_number": 0, "parts": 0, "accepted": 0}
        number = int(totals["next_number"]) if part_number is None else int(part_number)
        if number >= int(max_parts):
            return PartOutcome("parts_full", upload=upload)
        existing = _one(
            con,
            "SELECT bytes FROM api_upload_parts WHERE upload_id = %s AND part_number = %s",
            (upload_id, number),
        )
        accepted = int(totals["accepted"]) - (int(existing["bytes"]) if existing else 0)
        accepted_parts = int(totals["parts"]) - (1 if existing else 0)
        budget = min(
            int(upload_max_bytes * 1.1),
            int(upload["bytes"]) + max(0, int(max_parts) - accepted_parts) * int(part_max_bytes),
        )
        if accepted + int(bytes) > budget:
            return PartOutcome("budget", upload=upload)
        before_commit(number)
        part = _one(
            con,
            "INSERT INTO api_upload_parts (id, upload_id, part_number, bytes, sha256) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (upload_id, part_number) DO UPDATE "
            "   SET bytes = EXCLUDED.bytes, sha256 = EXCLUDED.sha256, created_at = now() "
            "RETURNING *",
            (ids.new_part_id(), upload_id, number, int(bytes), sha256),
        )
        upload = _one(
            con,
            "UPDATE api_uploads SET part_mode = COALESCE(part_mode, %s), "
            "       bytes_received = (SELECT COALESCE(sum(bytes), 0) FROM api_upload_parts WHERE upload_id = %s), "
            "       last_part_at = now(), updated_at = now(), "
            "       expires_at = LEAST(created_at + make_interval(secs => %s::float8), "
            "                          now() + make_interval(secs => %s::float8)) "
            " WHERE id = %s RETURNING *, false AS lapsed",
            (mode, upload_id, float(max_ttl_s), float(idle_ttl_s), upload_id),
        )
    return PartOutcome("ok", part=part, upload=upload)


def try_begin_api_upload_finalize(project_id: str, upload_id: str) -> Tuple[str, Optional[dict]]:
    """Row lock, `pending → finalizing`. Returns one of
    `('won'|'completed'|'rejected'|'busy'|'conflict'|'gone', row)`."""
    with db.connection() as con:
        row = _one(
            con,
            "SELECT *, (status = 'pending' AND expires_at <= now()) AS lapsed "
            "  FROM api_uploads WHERE id = %s AND project_id = %s FOR UPDATE",
            (upload_id, project_id),
        )
        if row is None:
            return "gone", None
        status = row["status"]
        if status == "completed":
            return "completed", row
        if status == "failed":
            return "rejected", row
        if status == "finalizing":
            return "busy", row
        if status != "pending" or row["lapsed"]:
            return "conflict", row
        won = _one(
            con,
            "UPDATE api_uploads SET status = 'finalizing', updated_at = now() "
            " WHERE id = %s RETURNING *, false AS lapsed",
            (upload_id,),
        )
    return "won", won


def return_api_upload_to_pending(project_id: str, upload_id: str) -> Optional[dict]:
    with db.connection() as con:
        return _one(
            con,
            "UPDATE api_uploads SET status = 'pending', updated_at = now() "
            " WHERE id = %s AND project_id = %s AND status = 'finalizing' RETURNING *",
            (upload_id, project_id),
        )


def complete_api_upload(
    project_id: str,
    upload_id: str,
    *,
    part_numbers: Sequence[int],
    expected_md5: Optional[str],
    expected_sha256: Optional[str],
    render_result: Callable[[dict, dict], dict],
) -> Optional[Tuple[dict, dict]]:
    """Step 3 of design §2.14, in ONE transaction: the file is created eagerly
    (`blob_id` NULL, `assembling_upload_id` set), parts not listed are dropped,
    the upload becomes `completed` with the assembly order and the checksums
    stored, and the Upload JSON (with its nested file) is written to `result`
    for replay. No byte is read. None when the row is not `finalizing` here."""
    with db.connection() as con:
        upload = _one(
            con,
            "SELECT * FROM api_uploads WHERE id = %s AND project_id = %s AND status = 'finalizing' FOR UPDATE",
            (upload_id, project_id),
        )
        if upload is None:
            return None
        file_row = _one(
            con,
            "INSERT INTO api_files (id, project_id, workspace_id, key_id, assembling_upload_id, filename, "
            "                       purpose, bytes, origin, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'upload', "
            "        CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(secs => %s::int) END) "
            "RETURNING *",
            (
                ids.new_file_id(), project_id, upload["workspace_id"], upload["key_id"], upload_id,
                upload["filename"], upload["purpose"], int(upload["bytes"]),
                upload["file_expires_after_seconds"], upload["file_expires_after_seconds"],
            ),
        )
        con.execute(
            "DELETE FROM api_upload_parts WHERE upload_id = %s AND NOT (part_number = ANY(%s))",
            (upload_id, list(int(n) for n in part_numbers)),
        )
        upload = _one(
            con,
            "UPDATE api_uploads SET status = 'completed', file_id = %s, assembly_part_numbers = %s, "
            "       expected_md5 = %s, expected_sha256 = %s, assembly_bytes_done = 0, "
            "       completed_at = now(), updated_at = now() "
            " WHERE id = %s RETURNING *",
            (file_row["id"], list(int(n) for n in part_numbers), expected_md5, expected_sha256, upload_id),
        )
        result = render_result(upload, file_row)
        upload = _one(
            con,
            "UPDATE api_uploads SET result = %s WHERE id = %s RETURNING *",
            (Jsonb(result), upload_id),
        )
    return upload, file_row  # type: ignore[return-value]


def finish_api_upload(
    project_id: str,
    upload_id: str,
    *,
    status: str,
    result: Optional[dict] = None,
    error_status: Optional[int] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> Optional[dict]:
    """Record a stored rejection (`failed`) or another terminal state."""
    with db.connection() as con:
        return _one(
            con,
            "UPDATE api_uploads SET status = %s, result = COALESCE(%s, result), error_status = %s, "
            "       error_code = %s, error_message = %s, updated_at = now() "
            " WHERE id = %s AND project_id = %s RETURNING *",
            (status, _jsonb(result), error_status, error_code, error_message, upload_id, project_id),
        )


def cancel_api_upload(project_id: str, upload_id: str) -> Tuple[str, Optional[dict]]:
    """`('cancelled'|'already'|'conflict'|'gone', row)`. `pending → cancelled`
    under the row lock; the caller removes the parts directory after commit
    (a part still streaming then finds the row cancelled and discards)."""
    with db.connection() as con:
        row = _one(
            con,
            "SELECT * FROM api_uploads WHERE id = %s AND project_id = %s FOR UPDATE",
            (upload_id, project_id),
        )
        if row is None:
            return "gone", None
        if row["status"] == "cancelled":
            return "already", row
        if row["status"] != "pending":
            return "conflict", row
        row = _one(
            con,
            "UPDATE api_uploads SET status = 'cancelled', updated_at = now() WHERE id = %s RETURNING *",
            (upload_id,),
        )
    return "cancelled", row


def reset_stale_api_upload_finalizing(older_than_s: float) -> int:
    """`finalizing` untouched for `older_than_s` → `pending` (crash between the
    lock and the record; startup and every sweep)."""
    with db.connection() as con:
        rows = con.execute(
            "UPDATE api_uploads SET status = 'pending', updated_at = now() "
            " WHERE status = 'finalizing' AND updated_at < now() - make_interval(secs => %s::float8) "
            "RETURNING id",
            (float(older_than_s),),
        ).fetchall()
    return len(rows)


def expire_api_uploads(limit: int) -> List[str]:
    """Pending uploads past `expires_at` → `expired`; returns their ids so the
    caller removes their parts directories."""
    with db.connection() as con:
        rows = con.execute(
            # A MATERIALIZED CTE, evaluated once: see
            # webhooks.queue.prune_settled_deliveries for the rescan hazard.
            "WITH due AS MATERIALIZED (SELECT id FROM api_uploads WHERE status = 'pending' AND expires_at <= now() "
            "              ORDER BY expires_at LIMIT %s FOR UPDATE SKIP LOCKED) "
            "UPDATE api_uploads u SET status = 'expired', updated_at = now() "
            "  FROM due WHERE u.id = due.id "
            "RETURNING u.id",
            (max(1, int(limit)),),
        ).fetchall()
    return [str(row["id"]) for row in rows]


def purge_api_upload_records(older_than_s: float, limit: int) -> List[str]:
    """Terminal upload rows older than `older_than_s` are deleted (§7.3: kept
    30 days). Never one whose file is still assembling."""
    with db.connection() as con:
        rows = con.execute(
            "WITH doomed AS MATERIALIZED ("
            "  SELECT id FROM api_uploads "
            "   WHERE status IN ('completed','cancelled','expired','failed') "
            "     AND assembly_part_numbers IS NULL "
            "     AND updated_at < now() - make_interval(secs => %s::float8) "
            "   ORDER BY updated_at LIMIT %s FOR UPDATE SKIP LOCKED) "
            "DELETE FROM api_uploads u USING doomed WHERE u.id = doomed.id "
            "RETURNING u.id",
            (float(older_than_s), max(1, int(limit))),
        ).fetchall()
    return [str(row["id"]) for row in rows]


# -------------------------------------------------------------- blobs+files --


#: `_lock_or_create_blob` re-tries its INSERT when the conflicting row was
#: deleted under it. One retry suffices for one purge; three bound a pathological
#: delete/re-upload loop on the same bytes.
_BLOB_VANISHED_RETRIES = 3

#: A failed blob whose failure was NOT a verdict on its bytes: the same bytes
#: uploaded again re-queue it (`_requeue_recoverable_blob`). `unsupported_file`,
#: `file_corrupt` and `file_too_complex` are verdicts — identical bytes would
#: only fail identically — and stay failed.
RECOVERABLE_BLOB_ERRORS = ("processing_unavailable", "internal_error")


def _requeue_recoverable_blob(con, project_id: str, sha256: str) -> Optional[dict]:
    """Put a recoverably failed blob of (project, sha256) back in the queue.

    WHY (verifier, 2026-09-14). A blob that failed while the embedding engine
    was down kept `status = failed`, and `_lock_or_create_blob` joined every
    later upload of the same bytes to it: the new file read `status: error`
    0.0 s after its upload, with the engine back up, and no upload could ever
    recover those bytes in that project. The chat app already treats a
    re-upload as the natural "try again" (`db.upsert_video_analysis`).

    What is reset: status, error code, the attempt count, the retry time and
    the failed stage's marker in `stages`, so the File object shows the work
    ahead again. What is kept: every finished stage's files and markers —
    the runner resumes at the first stage without one, so a PDF whose index
    failed does not extract its text again.

    What is counted: `progress.recoveries` goes up by one and is never reset.
    The processing usage row is keyed `<blob>.<attempt>`
    (`accounting.processing_generation_id`), `usage_events.generation_id` is
    unique and the insert is ON CONFLICT DO NOTHING: with `attempt` back at 0
    the recovered run's row collided with the failed run's `<blob>.0` and was
    silently dropped, with its embed tokens, OCR pages and audio seconds
    (review, 2026-09-14). The recovery count makes each run's key its own.

    NOT re-queued: a blob failed by the crash-loop guard (`progress.crashes`
    reached the attempt ceiling — its runs kept taking the process down, so a
    re-upload must not buy another five), and the verdicts listed above.

    Runs BEFORE the share-locking SELECT, in the same transaction: the UPDATE
    takes the row lock first, so two concurrent re-uploads serialise on it and
    the second finds `queued` and changes nothing (a share lock taken first
    and upgraded by both would deadlock)."""
    from . import limits

    return _one(
        con,
        "UPDATE api_file_blobs SET status = 'queued', error_code = NULL, attempt = 0, not_before = NULL, "
        "       lease_owner = NULL, lease_expires_at = NULL, processed_at = NULL, updated_at = now(), "
        "       progress = jsonb_set(progress - 'error_ceiling' - 'derived' - 'running_owner' - 'outage_retries' "
        "                                     - 'outage_since', "
        "                            '{recoveries}', to_jsonb((CASE WHEN progress->>'recoveries' ~ '^[0-9]{1,9}$' "
        "                                                       THEN (progress->>'recoveries')::int ELSE 0 END) + 1)), "
        "       stages = COALESCE((SELECT jsonb_object_agg(e.key, e.value) FROM jsonb_each(stages) e "
        "                           WHERE e.value->>'status' IS DISTINCT FROM 'failed'), '{}'::jsonb) "
        " WHERE project_id = %s AND sha256 = %s AND status = 'failed' AND error_code = ANY(%s) "
        "   AND (CASE WHEN progress->>'crashes' ~ '^[0-9]{1,9}$' THEN (progress->>'crashes')::int ELSE 0 END) < %s "
        "RETURNING id",
        (project_id, sha256, list(RECOVERABLE_BLOB_ERRORS), max(1, int(limits.processing_max_attempts()))),
    )


def _lock_or_create_blob(
    con,
    project_id: str,
    workspace_id: str,
    *,
    sha256: str,
    bytes: int,
    kind: str,
    mime_type: str,
    lane: str,
    place_bytes: Callable[[dict, bool], None],
) -> Tuple[dict, bool]:
    """The blob for (project, sha256), share-locked, bytes in place.

    `INSERT … ON CONFLICT DO NOTHING` then `SELECT … FOR SHARE`: the share lock
    is what stops a concurrent DELETE from marking this blob `deleting` between
    the check below and the file row that will reference it (the delete takes
    FOR UPDATE on the blob and so waits for this transaction, then counts the
    new live file). `place_bytes(row, created)` moves the caller's bytes to
    `original` while the row is locked: on a create, and on an existing row
    whose `original` is missing (a disk repair), so a committed blob row always
    has its bytes.

    A blob that failed for a reason other than its bytes is re-queued first
    (`_requeue_recoverable_blob`): the returned row is then `queued`, and the
    caller wakes the runner exactly as for a created one."""
    row = None
    created_row = None
    for _ in range(_BLOB_VANISHED_RETRIES):
        created_row = _one(
            con,
            "INSERT INTO api_file_blobs (id, project_id, workspace_id, sha256, bytes, kind, mime_type, lane) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (project_id, sha256) DO NOTHING RETURNING id",
            (ids.new_blob_id(), project_id, workspace_id, sha256, int(bytes), kind, mime_type, lane),
        )
        if created_row is None:
            _requeue_recoverable_blob(con, project_id, sha256)
        row = _one(
            con,
            "SELECT * FROM api_file_blobs WHERE project_id = %s AND sha256 = %s FOR SHARE",
            (project_id, sha256),
        )
        if row is not None:
            break
        # The conflicting row was a `deleting` blob that `retire_deleting_blob`
        # held FOR UPDATE and then deleted: the INSERT saw it and did nothing,
        # the SELECT waited on its lock and found it gone (READ COMMITTED
        # re-checks a locked row after the locker commits). The next INSERT
        # sees no conflict. Before 2026-09-13 this surfaced as a 500.
    if row is None:
        raise RuntimeError("blob row vanished")
    if row["status"] == "deleting":
        raise BlobBeingPurged(row)
    created = created_row is not None
    place_bytes(row, created)
    return row, created


def create_api_file_with_blob(
    project_id: str,
    workspace_id: str,
    key_id: Optional[str],
    *,
    sha256: str,
    bytes: int,
    kind: str,
    mime_type: str,
    lane: str,
    filename: str,
    purpose: str,
    origin: str,
    expires_after_seconds: Optional[int],
    place_bytes: Callable[[dict, bool], None],
) -> Tuple[dict, dict, bool]:
    """`POST /v1/files` in one transaction: blob (deduplicated within the
    project) + file. Returns `(file_row_with_blob_columns, blob_row, created)`."""
    with db.connection() as con:
        blob, created = _lock_or_create_blob(
            con, project_id, workspace_id, sha256=sha256, bytes=bytes, kind=kind,
            mime_type=mime_type, lane=lane, place_bytes=place_bytes,
        )
        file_id = ids.new_file_id()
        con.execute(
            "INSERT INTO api_files (id, project_id, workspace_id, key_id, blob_id, filename, purpose, bytes, origin, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "        CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(secs => %s::int) END)",
            (
                file_id, project_id, workspace_id, key_id or None, blob["id"], filename, purpose,
                int(bytes), origin, expires_after_seconds, expires_after_seconds,
            ),
        )
        file_row = _one(con, _FILE_SELECT + " WHERE f.id = %s", (file_id,))
    return file_row, blob, created  # type: ignore[return-value]


def create_api_file(
    project_id: str,
    workspace_id: str,
    key_id: Optional[str],
    blob_id: str,
    filename: str,
    purpose: str,
    bytes: int,
    origin: str,
    expires_after_seconds: Optional[int],
) -> Optional[dict]:
    """A file over an EXISTING live blob of the same project (design §9.2
    name). None when the blob is absent, another project's, or deleting."""
    with db.connection() as con:
        blob = _one(
            con,
            "SELECT id FROM api_file_blobs WHERE id = %s AND project_id = %s AND status <> 'deleting' FOR SHARE",
            (blob_id, project_id),
        )
        if blob is None:
            return None
        file_id = ids.new_file_id()
        con.execute(
            "INSERT INTO api_files (id, project_id, workspace_id, key_id, blob_id, filename, purpose, bytes, origin, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "        CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(secs => %s::int) END)",
            (file_id, project_id, workspace_id, key_id or None, blob_id, filename, purpose, int(bytes), origin,
             expires_after_seconds, expires_after_seconds),
        )
        return _one(con, _FILE_SELECT + " WHERE f.id = %s", (file_id,))


def attach_blob_to_assembling_file(
    project_id: str,
    file_id: str,
    upload_id: str,
    *,
    lease_owner: str,
    sha256: str,
    bytes: int,
    kind: str,
    mime_type: str,
    lane: str,
    place_bytes: Callable[[dict, bool], None],
) -> Optional[Tuple[dict, dict, bool]]:
    """The end of the `assemble` stage: the blob (deduplicated in the project)
    gets the bytes, the file points at it, and the upload's assembly columns
    are cleared. None when the file was deleted meanwhile or this process no
    longer holds the assembly lease — the caller then discards its copy."""
    with db.connection() as con:
        upload = _one(
            con,
            "SELECT id FROM api_uploads WHERE id = %s AND project_id = %s AND assembly_lease_owner = %s FOR UPDATE",
            (upload_id, project_id, lease_owner),
        )
        if upload is None:
            return None
        live = _one(
            con,
            "SELECT id FROM api_files WHERE id = %s AND project_id = %s AND deleted_at IS NULL "
            "   AND assembling_upload_id = %s FOR UPDATE",
            (file_id, project_id, upload_id),
        )
        if live is None:
            return None
        blob, created = _lock_or_create_blob(
            con, project_id, _workspace_of_upload(con, upload_id), sha256=sha256, bytes=bytes,
            kind=kind, mime_type=mime_type, lane=lane, place_bytes=place_bytes,
        )
        con.execute(
            "UPDATE api_files SET blob_id = %s, assembling_upload_id = NULL WHERE id = %s",
            (blob["id"], file_id),
        )
        con.execute(
            "UPDATE api_uploads SET assembly_part_numbers = NULL, assembly_lease_owner = NULL, "
            "       assembly_lease_expires_at = NULL, assembly_bytes_done = %s, updated_at = now() "
            " WHERE id = %s",
            (int(bytes), upload_id),
        )
        file_row = _one(con, _FILE_SELECT + " WHERE f.id = %s", (file_id,))
    return file_row, blob, created  # type: ignore[return-value]


def _workspace_of_upload(con, upload_id: str) -> str:
    row = _one(con, "SELECT workspace_id FROM api_uploads WHERE id = %s", (upload_id,))
    return str(row["workspace_id"]) if row else ""


def fail_assembling_file(project_id: str, file_id: str, upload_id: str, error_code: str) -> Optional[dict]:
    """Record `checksum_mismatch` (or `internal_error`) on an assembling file
    and clear the upload's assembly columns. The Upload stays `completed`: its
    parts were accepted; the FILE is what failed (§2.14 step 5)."""
    if error_code not in FILE_ERROR_CODES:
        raise ValueError("unknown file error code")
    with db.connection() as con:
        row = _one(
            con,
            "UPDATE api_files SET error_code = %s, assembling_upload_id = NULL "
            " WHERE id = %s AND project_id = %s AND deleted_at IS NULL AND assembling_upload_id = %s "
            "RETURNING id",
            (error_code, file_id, project_id, upload_id),
        )
        con.execute(
            "UPDATE api_uploads SET assembly_part_numbers = NULL, assembly_lease_owner = NULL, "
            "       assembly_lease_expires_at = NULL, updated_at = now() "
            " WHERE id = %s AND project_id = %s",
            (upload_id, project_id),
        )
        if row is None:
            return None
        return _one(con, _FILE_SELECT + " WHERE f.id = %s", (file_id,))


def get_api_file(project_id: str, file_id: str) -> Optional[dict]:
    """Live only (not deleted, not expired); joined with blob columns prefixed
    `blob_` and, while assembling, the upload's progress."""
    with db.connection() as con:
        return _one(con, _FILE_SELECT + f" WHERE f.project_id = %s AND f.id = %s AND {_LIVE}", (project_id, file_id))


def get_api_files(project_id: str, file_ids: Sequence[str]) -> Dict[str, dict]:
    """Batch read for model input; ids of other projects are simply absent."""
    wanted = [str(i) for i in file_ids if ids.is_file_id(i)]
    if not wanted:
        return {}
    with db.connection() as con:
        rows = _all(con, _FILE_SELECT + f" WHERE f.project_id = %s AND f.id = ANY(%s) AND {_LIVE}", (project_id, wanted))
    return {str(row["id"]): row for row in rows}


def list_api_files(
    project_id: str,
    *,
    after: Optional[str],
    limit: int,
    order: str,
    purpose: Optional[str],
) -> Tuple[List[dict], bool]:
    """Keyset on `(created_at, id)`. `after` resolves against this project's
    rows INCLUDING tombstones (finding #8), so a loop that deletes the last
    item of a page before asking for the next one keeps working; an id that
    never existed in this project raises `UnknownCursor` (the same for an id
    of another project and for no id at all)."""
    descending = order != "asc"
    bounded = max(1, int(limit))
    with db.connection() as con:
        cursor_row = None
        if after is not None:
            cursor_row = _one(
                con,
                "SELECT created_at, id FROM api_files WHERE project_id = %s AND id = %s",
                (project_id, after),
            )
            if cursor_row is None:
                raise UnknownCursor()
        clauses = ["f.project_id = %s", _LIVE]
        params: List[Any] = [project_id]
        if purpose is not None:
            clauses.append("f.purpose = %s")
            params.append(purpose)
        if cursor_row is not None:
            clauses.append("(f.created_at, f.id) < (%s, %s)" if descending else "(f.created_at, f.id) > (%s, %s)")
            params.extend([cursor_row["created_at"], cursor_row["id"]])
        direction = "DESC" if descending else "ASC"
        rows = _all(
            con,
            _FILE_SELECT + " WHERE " + " AND ".join(clauses)
            + f" ORDER BY f.created_at {direction}, f.id {direction} LIMIT %s",
            (*params, bounded + 1),
        )
    return rows[:bounded], len(rows) > bounded


def delete_api_file(
    project_id: str, file_id: str, *, include_expired: bool = False
) -> Optional[Tuple[dict, Optional[dict]]]:
    """Design §2.7's transaction. Returns `(tombstone_row, blob_row_now_deleting)`
    — the second is None when other live files still reference the bytes (or
    the file had no blob yet). None when there was no live file to delete.

    The tombstone keeps filename, bytes and timestamps with `blob_id` and
    `assembling_upload_id` NULL. The returned row carries `old_blob_id` and
    `old_assembling_upload_id` so the caller can clean up after commit."""
    expiry = "" if include_expired else " AND (expires_at IS NULL OR expires_at > now())"
    with db.connection() as con:
        tomb = _one(
            con,
            "WITH old AS (SELECT id, blob_id, assembling_upload_id FROM api_files "
            f"              WHERE id = %s AND project_id = %s AND deleted_at IS NULL{expiry} FOR UPDATE) "
            "UPDATE api_files f SET deleted_at = now(), blob_id = NULL, assembling_upload_id = NULL "
            "  FROM old WHERE f.id = old.id "
            "RETURNING f.*, old.blob_id AS old_blob_id, old.assembling_upload_id AS old_assembling_upload_id",
            (file_id, project_id),
        )
        if tomb is None:
            return None
        blob = None
        if tomb["old_blob_id"]:
            locked = _one(con, "SELECT * FROM api_file_blobs WHERE id = %s FOR UPDATE", (tomb["old_blob_id"],))
            if locked is not None:
                live = _one(
                    con,
                    "SELECT count(*) AS n FROM api_files WHERE blob_id = %s AND deleted_at IS NULL",
                    (tomb["old_blob_id"],),
                )
                if int(live["n"]) == 0:
                    blob = _one(
                        con,
                        "UPDATE api_file_blobs SET status = 'deleting', updated_at = now() WHERE id = %s RETURNING *",
                        (tomb["old_blob_id"],),
                    )
    return tomb, blob


def get_api_file_blob(blob_id: str) -> Optional[dict]:
    """Server-internal (jobs, retention): never behind a caller-supplied id."""
    with db.connection() as con:
        return _one(con, "SELECT * FROM api_file_blobs WHERE id = %s", (blob_id,))


_BLOB_UPDATABLE = frozenset({
    "status", "stage", "stages", "progress", "facts", "error_code", "attempt", "not_before", "kind",
    "mime_type", "lane", "lease_owner", "lease_expires_at", "video_analysis_id", "derived_bytes",
    "started_at", "processed_at", "pipeline_version",
})
_JSON_FIELDS = frozenset({"stages", "progress", "facts"})


def update_api_file_blob(blob_id: str, **fields: Any) -> Optional[dict]:
    """Allow-listed update (design §9.2); `updated_at` is always touched."""
    unknown = sorted(set(fields) - _BLOB_UPDATABLE)
    if unknown:
        raise ValueError(f"not updatable on api_file_blobs: {unknown[0]}")
    if not fields:
        return get_api_file_blob(blob_id)
    names = sorted(fields)
    assignments = ", ".join(f"{name} = %s" for name in names)
    values = [Jsonb(fields[n]) if n in _JSON_FIELDS else fields[n] for n in names]
    with db.connection() as con:
        return _one(
            con,
            f"UPDATE api_file_blobs SET {assignments}, updated_at = now() WHERE id = %s RETURNING *",
            (*values, blob_id),
        )


def live_files_for_blob(blob_id: str) -> List[dict]:
    with db.connection() as con:
        return _all(
            con,
            "SELECT * FROM api_files WHERE blob_id = %s AND deleted_at IS NULL ORDER BY created_at",
            (blob_id,),
        )


def delete_api_file_blob(blob_id: str) -> bool:
    """Only a `deleting` blob that no live file references."""
    with db.connection() as con:
        row = con.execute(
            "DELETE FROM api_file_blobs b WHERE b.id = %s AND b.status = 'deleting' "
            "   AND NOT EXISTS (SELECT 1 FROM api_files f WHERE f.blob_id = b.id AND f.deleted_at IS NULL) "
            "RETURNING b.id",
            (blob_id,),
        ).fetchone()
    return row is not None


def retire_deleting_blob(
    blob_id: str, *, move_aside: Callable[[dict], Optional[str]]
) -> Optional[Tuple[dict, Optional[str]]]:
    """THE purge primitive, keyed by blob id (review finding, 2026-09-13).

    In ONE transaction: lock the row `WHERE id = %s AND status = 'deleting'
    FOR UPDATE`, check no live file references it, call `move_aside(row)`
    (`storage.move_blob_dir_aside`: rename `<project>/<sha256>/` into
    `_trash/`) while the lock is held, and delete the row. Returns
    `(row, trash_path_or_None)`; the caller removes the trash path AFTER this
    returns. None when there is nothing of this blob id left to purge (never
    existed, already purged, or revived) — and then NOTHING on disk was touched.

    WHY THIS SHAPE. The blob directory is keyed by (project, sha256), so a
    purge that derives the path from a row it read earlier can delete a NEWER
    blob's bytes for the same content. Here the path is only ever moved while
    this blob's row exists and is locked; the unique index on
    `(project_id, sha256)` means no other blob can own that path at that
    moment, and a concurrent re-upload's `_lock_or_create_blob` waits on the
    lock and creates its blob (and its directory) only after this commits.
    A second or stale purge of the same id finds no row and touches nothing.

    A failure after the rename rolls the row back to `deleting` with its bytes
    already in `_trash/`: the next purge finds no directory, deletes the row,
    and the upload sweep clears the trash entry."""
    if not ids.is_blob_id(blob_id):
        return None
    with db.connection() as con:
        row = _one(
            con,
            "SELECT * FROM api_file_blobs WHERE id = %s AND status = 'deleting' FOR UPDATE",
            (blob_id,),
        )
        if row is None:
            return None
        live = _one(
            con,
            "SELECT 1 AS live FROM api_files WHERE blob_id = %s AND deleted_at IS NULL LIMIT 1",
            (blob_id,),
        )
        if live is not None:  # pragma: no cover - `deleting` is only set with no live file
            return None
        moved = move_aside(row)
        con.execute("DELETE FROM api_file_blobs WHERE id = %s", (blob_id,))
    return row, moved


def api_blobs_in_state(state: str, limit: int) -> List[dict]:
    with db.connection() as con:
        return _all(
            con,
            "SELECT * FROM api_file_blobs WHERE status = %s ORDER BY updated_at LIMIT %s",
            (state, max(1, int(limit))),
        )


def expired_api_files(limit: int) -> List[Tuple[str, str]]:
    """(project_id, file_id) of live files past `expires_at`, oldest first."""
    with db.connection() as con:
        rows = con.execute(
            "SELECT project_id, id FROM api_files WHERE deleted_at IS NULL AND expires_at IS NOT NULL "
            "   AND expires_at <= now() ORDER BY expires_at LIMIT %s",
            (max(1, int(limit)),),
        ).fetchall()
    return [(str(r["project_id"]), str(r["id"])) for r in rows]


def purge_api_file_tombstones(older_than_s: float) -> int:
    with db.connection() as con:
        rows = con.execute(
            "DELETE FROM api_files WHERE deleted_at IS NOT NULL "
            "   AND deleted_at < now() - make_interval(secs => %s::float8) RETURNING id",
            (float(older_than_s),),
        ).fetchall()
    return len(rows)


def project_file_storage(project_id: str) -> dict:
    """`{files, bytes, derived_bytes, uploads_pending, uploads_pending_bytes}`.
    `bytes` sums DISTINCT blobs with at least one live file: two files over one
    blob cost the disk once, and are accounted once."""
    with db.connection() as con:
        row = _one(
            con,
            "SELECT "
            " (SELECT count(*) FROM api_files f WHERE f.project_id = %s AND " + _LIVE + ") AS files, "
            " (SELECT COALESCE(sum(b.bytes), 0) FROM api_file_blobs b WHERE b.project_id = %s AND EXISTS "
            "     (SELECT 1 FROM api_files f WHERE f.blob_id = b.id AND f.deleted_at IS NULL)) AS bytes, "
            " (SELECT COALESCE(sum(b.derived_bytes), 0) FROM api_file_blobs b WHERE b.project_id = %s AND EXISTS "
            "     (SELECT 1 FROM api_files f WHERE f.blob_id = b.id AND f.deleted_at IS NULL)) AS derived_bytes, "
            " (SELECT count(*) FROM api_uploads u WHERE u.project_id = %s AND u.status = 'pending') AS uploads_pending, "
            " (SELECT COALESCE(sum(u.bytes_received), 0) FROM api_uploads u WHERE u.project_id = %s "
            "     AND u.status = 'pending') AS uploads_pending_bytes",
            (project_id, project_id, project_id, project_id, project_id),
        ) or {}
    return {key: int(row.get(key) or 0) for key in ("files", "bytes", "derived_bytes", "uploads_pending", "uploads_pending_bytes")}


def workspace_file_storage(workspace_id: str) -> List[dict]:
    """Per project of one workspace, for the console (metadata only)."""
    with db.connection() as con:
        rows = _all(
            con,
            "SELECT p.id AS project_id, "
            "  (SELECT count(*) FROM api_files f WHERE f.project_id = p.id AND " + _LIVE + ") AS files, "
            "  (SELECT COALESCE(sum(b.bytes), 0) FROM api_file_blobs b WHERE b.project_id = p.id AND EXISTS "
            "      (SELECT 1 FROM api_files f WHERE f.blob_id = b.id AND f.deleted_at IS NULL)) AS bytes, "
            "  (SELECT COALESCE(sum(b.derived_bytes), 0) FROM api_file_blobs b WHERE b.project_id = p.id AND EXISTS "
            "      (SELECT 1 FROM api_files f WHERE f.blob_id = b.id AND f.deleted_at IS NULL)) AS derived_bytes "
            "  FROM api_projects p WHERE p.workspace_id = %s ORDER BY p.id",
            (workspace_id,),
        )
    return [{**row, "files": int(row["files"]), "bytes": int(row["bytes"]), "derived_bytes": int(row["derived_bytes"])} for row in rows]


def api_video_analyses_without_blob(older_than_s: float, limit: int) -> List[dict]:
    """`lane = 'api'` analyses no blob references, older than `older_than_s`
    (reconciliation after a crash between purge steps, §7.3)."""
    with db.connection() as con:
        return _all(
            con,
            "SELECT a.* FROM video_analyses a WHERE a.lane = 'api' "
            "   AND a.created_at < now() - make_interval(secs => %s::float8) "
            "   AND NOT EXISTS (SELECT 1 FROM api_file_blobs b WHERE b.video_analysis_id = a.id) "
            " ORDER BY a.created_at LIMIT %s",
            (float(older_than_s), max(1, int(limit))),
        )


#: Accessors reachable with a caller-supplied id, each taking `project_id`
#: first. The isolation test calls every one with a foreign project.
PROJECT_SCOPED_ACCESSORS = (
    "get_api_upload",
    "list_api_upload_parts",
    "next_api_upload_part_number",
    "upsert_api_upload_part",
    "try_begin_api_upload_finalize",
    "return_api_upload_to_pending",
    "complete_api_upload",
    "finish_api_upload",
    "cancel_api_upload",
    "create_api_file",
    "attach_blob_to_assembling_file",
    "fail_assembling_file",
    "get_api_file",
    "get_api_files",
    "list_api_files",
    "delete_api_file",
    "project_file_storage",
)
