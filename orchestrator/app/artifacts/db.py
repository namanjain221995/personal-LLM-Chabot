"""Every SQL accessor the Artifact Studio uses — the V31 tables, and nothing
else touches them.

OWNER-SCOPED BY CONSTRUCTION. Every accessor a request handler can reach takes
the caller's `user_id` and puts it in the WHERE clause, so another person's
artifact id, version or job id answers None exactly like a missing one — the
same shape uploads.download_upload keeps. The handful of accessors the job
runner itself uses (`load_job`, the lease trio, the queue listing) are
unscoped because the runner is not a caller: it holds a row it was handed at
acceptance, after the acceptance already checked ownership.

THE LEASE (V29 shape, V31 table). `claim_lease` takes or renews a row for one
process; it succeeds only when nobody holds it, the holder is this process,
or the holder's lease has lapsed. `release_lease` is owner-scoped so a run
whose lease was taken over cannot release the new owner's. `requeue_lapsed`
is what startup runs: rows still 'running' whose lease has expired belong to
a process that died, and go back to the queue with their finished stages on
disk. The claim bumps `updated_at` and `heartbeat_at` so the stalled-job
signal in /health moves with the heartbeat (health.py reads updated_at).

ONE TRANSACTION WHERE IT MATTERS. `create_artifact_job` inserts the artifact,
the version and the job together, or returns the job that already carries the
idempotency key. `publish_version` marks the version, bumps the artifact's
current_version and completes the job in one transaction, so a reader can
never see a completed job whose version is not the artifact's current one.

THE RUNNER'S TRANSITIONS ARE GUARDED ON THE CURRENT STATE. A cancel from the
API lands whenever it likes: between the runner's load of the row and
`mark_running`, or inside the last heartbeat interval (TTL/3 = 30 s) before
the publish. Until 2026-09-11 those writes were unconditional, so a job the
API had just answered `{status: cancelled}` for went back to 'running' and
on to 'completed' with its version published — the person pressed Cancel
and got the file anyway. So `mark_running` moves only a queued/running row
(None otherwise, and the run stops), `publish_version` completes only a
'running' job (returning 'cancelled' otherwise, with the version row left
as the cancel wrote it), and `set_job_status(only_from=...)` lets the
deferral and the failure paths refuse to overwrite a cancel the same way.

Blocking, like every accessor in app.db: callers on the event loop go
through `db.run_in_thread`.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence

from .. import db as core
from . import types as T

# ---------------------------------------------------------------- rows --

_JOB_JSON = frozenset({"requested_formats", "selected_formats", "progress"})
_JOB_TEXT = frozenset({
    "conversation_id", "generation_id", "format_reason", "effort", "mode",
    "template_id", "instruction", "status", "stage", "input_hash",
    "failure_category", "error", "diagnostic_ref",
})
_JOB_TIMES = ("created_at", "started_at", "heartbeat_at", "updated_at", "completed_at", "lease_expires_at")

#: Columns update_job may touch. Identity, ownership, the idempotency key
#: and created_at are not on it: they are what a row IS. The lease columns
#: move only through the lease accessors.
_JOB_UPDATABLE = frozenset({
    "status", "stage", "progress", "attempt", "failure_category", "error",
    "diagnostic_ref", "started_at", "heartbeat_at", "completed_at",
    "selected_formats", "format_reason", "template_id", "input_hash",
})

_VERSION_JSON = frozenset({"formats", "files", "warnings", "validation", "assumptions"})
_VERSION_TIMES = ("created_at", "completed_at")


def _jsonish(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    return value if value is not None else default


def _job_row(r: Any) -> dict:
    out = dict(r)
    for key in _JOB_TIMES:
        if key in out:
            out[key] = core._iso(out[key]) if out.get(key) else None
    for key in ("requested_formats", "selected_formats"):
        out[key] = list(_jsonish(out.get(key), []))
    out["progress"] = dict(_jsonish(out.get("progress"), {}))
    out["user_id"] = int(out["user_id"])
    out["version"] = int(out["version"])
    out["attempt"] = int(out.get("attempt") or 0)
    return out


def _version_row(r: Any) -> dict:
    out = dict(r)
    for key in _VERSION_TIMES:
        if key in out:
            out[key] = core._iso(out[key]) if out.get(key) else None
    for key in _VERSION_JSON:
        out[key] = _jsonish(out.get(key), {} if key == "validation" else [])
    out["version"] = int(out["version"])
    out["preview_pages"] = int(out.get("preview_pages") or 0)
    if out.get("parent_version") is not None:
        out["parent_version"] = int(out["parent_version"])
    return out


def _artifact_row(r: Any) -> dict:
    out = dict(r)
    for key in ("created_at", "updated_at"):
        out[key] = core._iso(out[key]) if out.get(key) else None
    out["user_id"] = int(out["user_id"])
    out["current_version"] = int(out.get("current_version") or 0)
    return out


class _Conflict(Exception):
    """The idempotency key was inserted by a concurrent acceptance."""


# ------------------------------------------------------------ acceptance --


def create_artifact_job(
    *,
    job_id: str,
    artifact_id: str,
    user_id: int,
    conversation_id: str,
    generation_id: str,
    operation: str,
    instruction: str,
    kind: str,
    requested_formats: Sequence[str],
    selected_formats: Sequence[str],
    format_reason: str,
    effort: str,
    mode: str,
    template_id: str,
    idempotency_key: str,
    parent_version: Optional[int] = None,
    title: str = "",
    input_hash: str = "",
) -> Optional[dict]:
    """The job row for this acceptance — created, or the one that already
    carries `idempotency_key`.

    `create` inserts a new artifact (`artifact_id` is the id it gets) and its
    version 1. `edit` and `convert` add the NEXT version to an artifact the
    caller owns, with `parent_version` recorded; the artifact row is locked
    for the numbering so two edits accepted at once cannot both become v2.
    None means the artifact is not the caller's (or the parent version does
    not exist) — the same answer a missing id gets.

    Returns the job row plus `created`.
    """
    ts = core._now()
    with core.connection() as con:
        existing = con.execute(
            "SELECT * FROM artifact_jobs WHERE idempotency_key = %s", (idempotency_key,)
        ).fetchone()
        if existing is not None:
            out = _job_row(existing)
            out["created"] = False
            return out
        try:
            with con.transaction():
                if operation == "create":
                    version = 1
                    con.execute(
                        """INSERT INTO artifacts
                               (id, user_id, conversation_id, title, kind, current_version, created_at, updated_at)
                           VALUES (%s, %s, %s, %s, %s, 0, %s, %s)""",
                        (artifact_id, int(user_id), core._text(conversation_id or "") or "",
                         core._text(title or "") or "", kind, ts, ts),
                    )
                else:
                    owned = con.execute(
                        "SELECT id FROM artifacts WHERE id = %s AND user_id = %s FOR UPDATE",
                        (artifact_id, int(user_id)),
                    ).fetchone()
                    if owned is None:
                        return None
                    if parent_version is not None:
                        parent = con.execute(
                            "SELECT version FROM artifact_versions WHERE artifact_id = %s AND version = %s",
                            (artifact_id, int(parent_version)),
                        ).fetchone()
                        if parent is None:
                            return None
                    row = con.execute(
                        "SELECT COALESCE(MAX(version), 0) AS v FROM artifact_versions WHERE artifact_id = %s",
                        (artifact_id,),
                    ).fetchone()
                    version = int(row["v"]) + 1
                con.execute(
                    """INSERT INTO artifact_versions
                           (artifact_id, version, job_id, parent_version, operation, instruction,
                            status, formats, template_id, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, 'queued', %s, %s, %s)""",
                    (artifact_id, version, job_id, parent_version, operation,
                     core._text(instruction or "") or "", core._json_param(list(selected_formats)),
                     template_id or "generic", ts),
                )
                row = con.execute(
                    """INSERT INTO artifact_jobs
                           (id, user_id, conversation_id, artifact_id, version, idempotency_key,
                            generation_id, operation, requested_formats, selected_formats,
                            format_reason, effort, mode, template_id, instruction, status, stage,
                            input_hash, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               'queued', 'intent', %s, %s, %s)
                       ON CONFLICT (idempotency_key) DO NOTHING
                       RETURNING *""",
                    (job_id, int(user_id), core._text(conversation_id or "") or "", artifact_id, version,
                     idempotency_key, generation_id or "", operation,
                     core._json_param(list(requested_formats)), core._json_param(list(selected_formats)),
                     core._text(format_reason or "") or "", effort or "fast", mode or "assistant",
                     template_id or "generic", core._text(instruction or "") or "",
                     input_hash or "", ts, ts),
                ).fetchone()
                if row is None:
                    # Somebody inserted the same key between our SELECT and
                    # our INSERT: the savepoint rolls our artifact/version
                    # back and theirs is the acceptance.
                    raise _Conflict()
        except _Conflict:
            row = con.execute(
                "SELECT * FROM artifact_jobs WHERE idempotency_key = %s", (idempotency_key,)
            ).fetchone()
            out = _job_row(row)
            out["created"] = False
            return out
    out = _job_row(row)
    out["created"] = True
    return out


# ------------------------------------------------------------- reading --

_JOB_SELECT = (
    "SELECT j.*, a.kind AS kind, a.title AS title "
    "FROM artifact_jobs j JOIN artifacts a ON a.id = j.artifact_id "
)


def load_job(job_id: str) -> Optional[dict]:
    """The runner's own view of a job — unscoped, with the artifact's kind
    and title joined in. Not for request handlers: they use `get_job`."""
    with core.connection() as con:
        row = con.execute(_JOB_SELECT + "WHERE j.id = %s", (job_id,)).fetchone()
    return _job_row(row) if row else None


def get_job_by_key(idempotency_key: str, user_id: int) -> Optional[dict]:
    """The job an earlier acceptance of the same turn created, owner-scoped
    (a key is derived from the user_id, but the WHERE clause says so too).
    Acceptance asks this BEFORE its refusal checks: the same acceptance
    returns the same job even when the person has since gone over quota."""
    with core.connection() as con:
        row = con.execute(
            _JOB_SELECT + "WHERE j.idempotency_key = %s AND j.user_id = %s",
            (idempotency_key, int(user_id)),
        ).fetchone()
    return _job_row(row) if row else None


def get_job(job_id: str, user_id: int) -> Optional[dict]:
    with core.connection() as con:
        row = con.execute(
            _JOB_SELECT + "WHERE j.id = %s AND j.user_id = %s", (job_id, int(user_id))
        ).fetchone()
    return _job_row(row) if row else None


def get_artifact(artifact_id: str, user_id: int) -> Optional[dict]:
    with core.connection() as con:
        row = con.execute(
            "SELECT * FROM artifacts WHERE id = %s AND user_id = %s", (artifact_id, int(user_id))
        ).fetchone()
    return _artifact_row(row) if row else None


def list_artifacts(user_id: int, conversation_id: Optional[str] = None, limit: int = 100) -> List[dict]:
    """The caller's artifacts, newest first, each with its CURRENT published
    version under `current` (None while the first version is still being
    made). Filtered to one conversation when given."""
    where = "a.user_id = %s"
    params: List[Any] = [int(user_id)]
    if conversation_id is not None:
        where += " AND a.conversation_id = %s"
        params.append(conversation_id)
    params.append(int(limit))
    with core.connection() as con:
        rows = con.execute(
            f"""SELECT a.*, row_to_json(v.*) AS current
                  FROM artifacts a
                  LEFT JOIN artifact_versions v
                    ON v.artifact_id = a.id AND v.version = a.current_version
                 WHERE {where}
                 ORDER BY a.updated_at DESC, a.id
                 LIMIT %s""",
            params,
        ).fetchall()
    out = []
    for r in rows:
        current = r["current"]
        art = _artifact_row({k: v for k, v in dict(r).items() if k != "current"})
        art["current"] = _version_row(_jsonish(current, None)) if current else None
        out.append(art)
    return out


def get_version(artifact_id: str, version: int, user_id: int) -> Optional[dict]:
    with core.connection() as con:
        row = con.execute(
            """SELECT v.*, a.kind AS kind, a.title AS title
                 FROM artifact_versions v JOIN artifacts a ON a.id = v.artifact_id
                WHERE v.artifact_id = %s AND v.version = %s AND a.user_id = %s""",
            (artifact_id, int(version), int(user_id)),
        ).fetchone()
    return _version_row(row) if row else None


def list_versions(artifact_id: str, user_id: int) -> List[dict]:
    with core.connection() as con:
        rows = con.execute(
            """SELECT v.*, a.kind AS kind, a.title AS title
                 FROM artifact_versions v JOIN artifacts a ON a.id = v.artifact_id
                WHERE v.artifact_id = %s AND a.user_id = %s
                ORDER BY v.version""",
            (artifact_id, int(user_id)),
        ).fetchall()
    return [_version_row(r) for r in rows]


def list_jobs(status: str, limit: int = 20) -> List[dict]:
    """Rows in one state, oldest first — the queue the drain reads."""
    with core.connection() as con:
        rows = con.execute(
            _JOB_SELECT + "WHERE j.status = %s ORDER BY j.created_at, j.id LIMIT %s",
            (status, int(limit)),
        ).fetchall()
    return [_job_row(r) for r in rows]


def list_conversation_jobs(user_id: int, conversation_id: str, limit: int = 50) -> List[dict]:
    with core.connection() as con:
        rows = con.execute(
            _JOB_SELECT + "WHERE j.user_id = %s AND j.conversation_id = %s "
            "ORDER BY j.created_at DESC, j.id LIMIT %s",
            (int(user_id), conversation_id, int(limit)),
        ).fetchall()
    return [_job_row(r) for r in rows]


# ------------------------------------------------------------- updates --


def update_job(job_id: str, **fields: Any) -> None:
    """Set the given columns (allow-listed) and bump updated_at. A None
    `stage` means "leave it"; every other None is written as NULL."""
    sets: List[str] = []
    params: List[Any] = []
    for key, value in fields.items():
        if key not in _JOB_UPDATABLE:
            raise ValueError(f"artifact_jobs.{key} is not updatable")
        if key == "stage" and value is None:
            continue
        if key in _JOB_JSON:
            value = core._json_param(value if value is not None else ({} if key == "progress" else []))
        elif key in _JOB_TEXT:
            value = core._text(value) if value is not None else None
        sets.append(f"{key} = %s")
        params.append(value)
    if not sets:
        return
    sets.append("updated_at = %s")
    params.append(core._now())
    params.append(job_id)
    with core.connection() as con:
        con.execute(f"UPDATE artifact_jobs SET {', '.join(sets)} WHERE id = %s", params)


def set_job_status(
    job_id: str,
    status: str,
    *,
    error: Optional[str] = None,
    failure_category: Optional[str] = None,
    diagnostic_ref: Optional[str] = None,
    completed: bool = False,
    only_from: Optional[Sequence[str]] = None,
) -> bool:
    """Move the job (and its version row, in the same transaction) to
    `status`. Terminal statuses stamp completed_at. `only_from` restricts
    the move to a row currently in one of those states — the runner passes
    ('queued', 'running') so a cancel that landed meanwhile is never
    overwritten. Returns whether a row moved."""
    if status not in T.JOB_STATUSES:
        raise ValueError(f"not a job status: {status!r}")
    for state in only_from or ():
        if state not in T.JOB_STATUSES:
            raise ValueError(f"not a job status: {state!r}")
    ts = core._now()
    sets = ["status = %s", "updated_at = %s"]
    params: List[Any] = [status, ts]
    if error is not None:
        sets.append("error = %s")
        params.append(core._text(error) or "")
    if failure_category is not None:
        sets.append("failure_category = %s")
        params.append(failure_category)
    if diagnostic_ref is not None:
        sets.append("diagnostic_ref = %s")
        params.append(diagnostic_ref)
    if completed or status in T.TERMINAL_STATUSES:
        sets.append("completed_at = %s")
        params.append(ts)
    elif status == "queued":
        sets.append("completed_at = NULL")
    params.append(job_id)
    where = "id = %s"
    if only_from:
        where += " AND status = ANY(%s)"
        params.append(list(only_from))
    with core.connection() as con:
        with con.transaction():
            row = con.execute(
                f"UPDATE artifact_jobs SET {', '.join(sets)} WHERE {where} RETURNING artifact_id, version",
                params,
            ).fetchone()
            if row is not None:
                con.execute(
                    "UPDATE artifact_versions SET status = %s WHERE artifact_id = %s AND version = %s "
                    "AND status NOT IN ('completed', 'completed_with_warnings')",
                    (status, row["artifact_id"], int(row["version"])),
                )
    return row is not None


def mark_running(job_id: str, attempt: int) -> Optional[dict]:
    """The run starts: job and version rows to 'running', the attempt
    counted, started_at stamped — one transaction. Only a queued or running
    row moves; None means the row was cancelled (or completed) between the
    runner's load and this write, and the run must not start."""
    ts = core._now()
    with core.connection() as con:
        with con.transaction():
            row = con.execute(
                "UPDATE artifact_jobs SET status = 'running', error = '', failure_category = '', "
                "attempt = %s, started_at = %s, updated_at = %s "
                "WHERE id = %s AND status IN ('queued', 'running') RETURNING artifact_id, version, status",
                (int(attempt), ts, ts, job_id),
            ).fetchone()
            if row is not None:
                con.execute(
                    "UPDATE artifact_versions SET status = 'running' WHERE artifact_id = %s AND version = %s "
                    "AND status NOT IN ('completed', 'completed_with_warnings')",
                    (row["artifact_id"], int(row["version"])),
                )
    return dict(row) if row is not None else None


def set_job_stage(job_id: str, stage: str) -> None:
    update_job(job_id, stage=stage)


def set_job_progress(job_id: str, progress: Dict[str, Any], stage: Optional[str] = None) -> None:
    update_job(job_id, progress=progress, stage=stage)


def publish_version(
    artifact_id: str,
    version: int,
    job_id: str,
    *,
    files: List[dict],
    validation: dict,
    warnings: Sequence[str],
    assumptions: Sequence[str],
    preview_kind: str,
    preview_pages: int,
    title: str = "",
    spec_version: int = 1,
    template_id: Optional[str] = None,
    template_version: str = "",
    renderer_version: str = "",
) -> str:
    """The version is published: files, validation and warnings recorded,
    the artifact's current_version bumped, the job completed — ONE
    transaction. Returns the status written ('completed' or
    'completed_with_warnings').

    Guarded: only a job still 'running' completes. A job cancelled inside
    the last heartbeat interval returns 'cancelled' and NOTHING is written —
    the version row stays as the cancel left it and the artifact's
    current_version does not move. The published directory on disk is the
    caller's to keep (a published version is never deleted); it is simply
    not a version the rows point at."""
    status = "completed_with_warnings" if warnings else "completed"
    ts = core._now()
    with core.connection() as con:
        with con.transaction():
            moved = con.execute(
                "UPDATE artifact_jobs SET status = %s, stage = 'preview', error = '', failure_category = '', "
                "completed_at = %s, updated_at = %s WHERE id = %s AND status = 'running' RETURNING id",
                (status, ts, ts, job_id),
            ).fetchone()
            if moved is None:
                return "cancelled"
            con.execute(
                """UPDATE artifact_versions
                      SET status = %s, files = %s, validation = %s, warnings = %s,
                          assumptions = %s, preview_kind = %s, preview_pages = %s,
                          spec_version = %s, template_id = COALESCE(%s, template_id),
                          template_version = %s, renderer_version = %s, completed_at = %s
                    WHERE artifact_id = %s AND version = %s""",
                (status, core._json_param(list(files)), core._json_param(dict(validation or {})),
                 core._json_param(list(warnings)), core._json_param(list(assumptions)),
                 preview_kind, int(preview_pages), int(spec_version), template_id,
                 template_version, renderer_version, ts, artifact_id, int(version)),
            )
            con.execute(
                """UPDATE artifacts
                      SET current_version = GREATEST(current_version, %s),
                          title = CASE WHEN %s <> '' THEN %s ELSE title END,
                          updated_at = %s
                    WHERE id = %s""",
                (int(version), core._text(title or "") or "", core._text(title or "") or "", ts, artifact_id),
            )
    return status


def cancel_job(job_id: str, user_id: int) -> Optional[dict]:
    """Owner-scoped, idempotent: a queued or running job becomes cancelled;
    a terminal one is left exactly as it is (a published version is never
    un-published). None for a job that is not the caller's."""
    ts = core._now()
    with core.connection() as con:
        with con.transaction():
            row = con.execute(
                """UPDATE artifact_jobs
                      SET status = 'cancelled', error = 'cancelled', failure_category = 'cancelled',
                          completed_at = %s, updated_at = %s
                    WHERE id = %s AND user_id = %s AND status IN ('queued', 'running')
                    RETURNING artifact_id, version""",
                (ts, ts, job_id, int(user_id)),
            ).fetchone()
            if row is not None:
                con.execute(
                    "UPDATE artifact_versions SET status = 'cancelled' WHERE artifact_id = %s AND version = %s "
                    "AND status NOT IN ('completed', 'completed_with_warnings')",
                    (row["artifact_id"], int(row["version"])),
                )
    return get_job(job_id, user_id)


def retry_job(job_id: str, user_id: int) -> Optional[dict]:
    """A failed job goes back to the queue for another attempt of the SAME
    version. Any other state is left alone; None when not the caller's."""
    ts = core._now()
    with core.connection() as con:
        with con.transaction():
            row = con.execute(
                """UPDATE artifact_jobs
                      SET status = 'queued', error = '', failure_category = '', diagnostic_ref = '',
                          completed_at = NULL, lease_owner = '', lease_expires_at = NULL, updated_at = %s
                    WHERE id = %s AND user_id = %s AND status = 'failed'
                    RETURNING artifact_id, version""",
                (ts, job_id, int(user_id)),
            ).fetchone()
            if row is not None:
                con.execute(
                    "UPDATE artifact_versions SET status = 'queued' WHERE artifact_id = %s AND version = %s",
                    (row["artifact_id"], int(row["version"])),
                )
    return get_job(job_id, user_id)


# --------------------------------------------------------------- lease --


def claim_lease(job_id: str, owner: str, ttl_s: float) -> bool:
    """Take (or renew) the run lease; False when another live owner holds it.
    Bumps heartbeat_at and updated_at so the stalled-job age in /health
    moves with every beat."""
    now = core._now()
    with core.connection() as con:
        row = con.execute(
            "UPDATE artifact_jobs SET lease_owner = %s, lease_expires_at = %s, heartbeat_at = %s, updated_at = %s "
            "WHERE id = %s AND (lease_owner = %s OR lease_owner = '' "
            "OR lease_expires_at IS NULL OR lease_expires_at < %s) RETURNING id",
            (owner, now + timedelta(seconds=float(ttl_s)), now, now, job_id, owner, now),
        ).fetchone()
    return row is not None


renew_lease = claim_lease


def release_lease(job_id: str, owner: str) -> None:
    with core.connection() as con:
        con.execute(
            "UPDATE artifact_jobs SET lease_owner = '', lease_expires_at = NULL "
            "WHERE id = %s AND lease_owner = %s",
            (job_id, owner),
        )


def lease_holder(job_id: str) -> str:
    with core.connection() as con:
        row = con.execute("SELECT lease_owner FROM artifact_jobs WHERE id = %s", (job_id,)).fetchone()
    return str((row or {}).get("lease_owner") or "")


def requeue_lapsed() -> int:
    """At startup: a row left 'running' whose lease has lapsed belongs to a
    process that died. Back to the queue; its stage files are on disk."""
    now = core._now()
    with core.connection() as con:
        rows = con.execute(
            "UPDATE artifact_jobs SET status = 'queued', updated_at = %s, "
            "lease_owner = '', lease_expires_at = NULL "
            "WHERE status = 'running' "
            "AND (lease_expires_at IS NULL OR lease_expires_at < %s) RETURNING id",
            (now, now),
        ).fetchall()
    return len(rows)


# ------------------------------------------------------ quota + health --


def count_open_jobs(user_id: int) -> int:
    """Queued + running jobs of one person — the in-flight half of the quota."""
    with core.connection() as con:
        row = con.execute(
            "SELECT count(*) AS n FROM artifact_jobs WHERE user_id = %s AND status IN ('queued', 'running')",
            (int(user_id),),
        ).fetchone()
    return int(row["n"] if row else 0)


def user_bytes(user_id: int) -> int:
    """Bytes this person has PUBLISHED, from the version rows' file sizes.
    The rows are the accounting: a walk of the volume would count working
    directories and previews, and would cost a stat per file."""
    with core.connection() as con:
        row = con.execute(
            """SELECT COALESCE(SUM((f->>'size')::bigint), 0) AS n
                 FROM artifact_versions v
                 JOIN artifacts a ON a.id = v.artifact_id
                 CROSS JOIN LATERAL jsonb_array_elements(v.files) AS f
                WHERE a.user_id = %s
                  AND v.status IN ('completed', 'completed_with_warnings')""",
            (int(user_id),),
        ).fetchone()
    return int(row["n"] or 0)


def work_snapshot() -> dict:
    """{queued, running, oldest_queued_age_s} — counts only, for /health."""
    out = {"queued": 0, "running": 0, "oldest_queued_age_s": 0}
    with core.connection() as con:
        for row in con.execute(
            "SELECT status, count(*) AS n FROM artifact_jobs "
            "WHERE status IN ('queued', 'running') GROUP BY status"
        ).fetchall():
            out[row["status"]] = int(row["n"])
        row = con.execute(
            "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - min(created_at))), 0) AS age "
            "FROM artifact_jobs WHERE status = 'queued'"
        ).fetchone()
        out["oldest_queued_age_s"] = int(float(row["age"] or 0))
    return out


def queue_depth() -> Dict[str, int]:
    snap = work_snapshot()
    return {"queued": snap["queued"], "running": snap["running"]}


def oldest_queued_age() -> int:
    return int(work_snapshot()["oldest_queued_age_s"])


__all__ = [
    "create_artifact_job", "load_job", "get_job_by_key", "get_job", "get_artifact", "list_artifacts",
    "get_version", "list_versions", "list_jobs", "list_conversation_jobs",
    "update_job", "mark_running", "set_job_status", "set_job_stage", "set_job_progress",
    "publish_version", "cancel_job", "retry_job",
    "claim_lease", "renew_lease", "release_lease", "lease_holder", "requeue_lapsed",
    "user_bytes", "work_snapshot", "queue_depth", "oldest_queued_age",
]
