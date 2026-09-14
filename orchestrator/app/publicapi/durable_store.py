"""The write-ahead log and lease of durable `/v1` generations (2026-09-13).

Every function here is SYNCHRONOUS and short: one pooled connection, one
transaction, no engine I/O inside it. Callers use `db.run_in_thread`. The
durable subsystem holds at most three pool connections at once (the writer,
the follower poller, the lease/sweep loop), so the chat app's pool of 16 is
never starved by public work.

THE FOUR CORRECTNESS RULES (design H, "store correctness"):

1. **Write-ahead.** A sequence number reaches a client only after the
   transaction that inserted it committed. `append` returns what committed;
   durable.py wakes followers from that, never from its in-memory buffer.
2. **Claim takes FOR UPDATE; append takes FOR SHARE.** A claim by process B
   waits for an in-flight append of process A to commit, so B's resume point
   (`max(sequence_number)` read inside the claim) includes A's last batch. The
   heartbeat's counters are never used as a resume point.
3. **ON CONFLICT DO NOTHING is a lease loss, for that job only.** If two
   owners ever write the same sequence number (a split brain after a lapsed
   lease), the loser's insert returns fewer rows than it sent, the rows it
   did insert are taken back inside the flush's transaction, and it stops.
   Every other job in the same flush commits.
4. **Terminal is atomic.** The terminal events, the status, the counters and
   the deletion of the spec and blob references are ONE transaction guarded by
   the lease (`finish`). A reader can never see `response.completed` on a row
   that still says `in_progress`, and two processes cannot both settle a run.

SCHEMA. `SCHEMA_SQL` is `db._MIGRATION_V36` (api_responses columns,
api_response_events, api_response_requests, api_response_blobs, indexes and
NOT VALID constraints), applied by `db.init_schema` at start-up. The Files
tables are V37 (apifiles/schema.py).
"""
from __future__ import annotations

import collections
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import psycopg

from .. import db

log = logging.getLogger(__name__)

OPEN_STATUSES = ("queued", "in_progress")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
DELTA_EVENT = "response.output_text.delta"
#: The private key inside a stored delta's `data` that carries how many engine
#: tokens it coalesced. Stripped before any frame is rendered (durable.py
#: `render_record`); summed by `claim` so a continuation knows how much of the
#: output budget is spent without trusting a lagging heartbeat.
TOKENS_KEY = "_t"

#: Advisory lock key for `ensure_schema` (distinct from db._MIGRATION_LOCK_KEY).
_SCHEMA_LOCK_KEY = 0x7D1B_36D0

#: The durable half of the no-timeout design's V36, which is `db._MIGRATION_V36`
#: itself. WHY (assembler, 2026-09-14): T2 shipped its own byte-for-byte copy
#: of this DDL for before T1's migration landed; two copies of one migration
#: drift, so there is now ONE text. `ensure_schema` below only reads the
#: catalogs unless something is missing (a test database, or a process that
#: starts the runtime without `db.init_schema`).
SCHEMA_SQL = db._MIGRATION_V36

_ensured_for: Optional[str] = None


_SCHEMA_COLUMNS = tuple(re.findall(r"ADD COLUMN IF NOT EXISTS (\w+)", SCHEMA_SQL))
_SCHEMA_RELATIONS = tuple(
    re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA_SQL)
    + re.findall(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)", SCHEMA_SQL)
)
_SCHEMA_CONSTRAINTS = tuple(re.findall(r"conname = '(\w+)'", SCHEMA_SQL))


def schema_present() -> bool:
    """Every V36 durable column, table, index and constraint exists — read
    from the catalogs, which takes no lock on api_responses."""
    with db.connection() as con:
        row = con.execute(
            """
            SELECT
              (SELECT count(*) FROM pg_attribute
                 WHERE attrelid = to_regclass('api_responses') AND attnum > 0 AND NOT attisdropped
                   AND attname = ANY(%s)) AS columns,
              (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = current_schema() AND c.relname = ANY(%s)) AS relations,
              (SELECT count(DISTINCT conname) FROM pg_constraint c
                 WHERE c.conrelid = to_regclass('api_responses') AND conname = ANY(%s)) AS constraints
            """,
            (list(_SCHEMA_COLUMNS), list(_SCHEMA_RELATIONS), list(_SCHEMA_CONSTRAINTS)),
        ).fetchone()
    return (
        int(row["columns"]) == len(set(_SCHEMA_COLUMNS))
        and int(row["relations"]) == len(set(_SCHEMA_RELATIONS))
        and int(row["constraints"]) == len(set(_SCHEMA_CONSTRAINTS))
    )


def ensure_schema(*, attempts: int = 10) -> None:
    """Make sure the V36 durable schema exists, once per DSN per process.

    2026-09-14 review P6: this used to run the DDL on EVERY process start.
    `ALTER TABLE api_responses ADD COLUMN IF NOT EXISTS` takes ACCESS
    EXCLUSIVE even when every column already exists, so one parked
    transaction that had merely read api_responses (a report, a pgAdmin tab)
    made start-up fail after ~95 s of retries — and queued every
    api_responses query behind each 3 s lock wait. V36 is T1's migration; so
    this now reads the catalogs first (no lock) and applies the DDL only when
    something is actually missing.

    When it must apply, it retries `LockNotAvailable` with 1 → 10 s backoff
    instead of hanging to statement_timeout (design L)."""
    global _ensured_for
    dsn = db.dsn()
    if _ensured_for == dsn:
        return
    if schema_present():
        _ensured_for = dsn
        return
    delay = 1.0
    for attempt in range(1, max(1, attempts) + 1):
        try:
            with db.connection() as con:
                con.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_KEY,))
                con.execute(SCHEMA_SQL)
            _ensured_for = dsn
            return
        except psycopg.errors.LockNotAvailable:
            if attempt >= attempts:
                raise
            log.warning("durable schema: lock not available (attempt %d); retrying", attempt)
            time.sleep(delay)
            delay = min(10.0, delay * 2)


def reset_schema_cache() -> None:
    global _ensured_for
    _ensured_for = None


# ---------------------------------------------------------------- types --

EventRecord = Tuple[int, str, Dict[str, Any]]


def _row(row: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    return None if row is None else db._api_row(dict(row))  # type: ignore[attr-defined]


#: A `\\u0000` escape json.dumps wrote for a NUL: an ODD run of backslashes
#: before `u0000` (an even run is literal backslashes followed by the text
#: "u0000", e.g. an answer that explains JSON escapes).
_NUL_ESCAPE = re.compile(r"(\\+)u0000")


def _drop_nul_escape(match: "re.Match[str]") -> str:
    slashes = match.group(1)
    return slashes[:-1] if len(slashes) % 2 else match.group(0)


def _clean_json(value: Dict[str, Any]) -> str:
    """JSON for a jsonb column. PostgreSQL jsonb refuses \\u0000 — a model can
    emit a NUL — so it is stripped here rather than failing the whole job.

    Only a real NUL escape is removed (verifier fix, 2026-09-14): the plain
    `.replace("\\u0000", "")` also cut the text `\\u0000` out of an answer
    that contained it, leaving an invalid escape (`::jsonb` refused the whole
    record, so the run lost its lease on every attempt) or a silently
    different character (`\\u0000n` became a newline)."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    return _NUL_ESCAPE.sub(_drop_nul_escape, text) if "u0000" in text else text


#: A terminal record carries its own copy of the answer only while the answer
#: is shorter than this many characters (2026-09-14, database-speed round).
#: A longer answer is stored ONCE, as the deltas every client already
#: streamed: `output_text.done`, `content_part.done`, `output_item.done` and
#: `response.completed` (or `response.failed`) used to repeat it four more
#: times in the one finish transaction — measured 527 ms, 5.8 MB of WAL and
#: 5.6 MB of TOAST for a 1M-token answer, all under FOR UPDATE on the
#: response row. Such a record stores "" in every slot that held the answer,
#: plus the private `TEXT_KEY` marker; `list_events` and `poll_many` put the
#: answer back from the deltas before anything renders the record.
TERMINAL_TEXT_INLINE_MAX_CHARS = 1024
#: {"paths": [[key or index, ...], ...], "strip": [bool, ...], "chars": n}.
#: `strip` marks a slot that held `text.strip()` (the Response model strips
#: surrounding whitespace). Private keys never reach a frame
#: (`durable.public_data`).
TEXT_KEY = "_text"


def _externalise_text(data: Dict[str, Any], text: str) -> Dict[str, Any]:
    """A copy of a terminal record's data with every string slot equal to
    `text` (or to `text.strip()`) emptied and listed under TEXT_KEY; `data`
    itself is not changed. Equality decides, not position: whatever held
    exactly the answer gets exactly the answer back."""
    stripped = text.strip()
    paths: List[List[Any]] = []
    strips: List[bool] = []

    def walk(node: Any, path: List[Any]) -> Any:
        if isinstance(node, dict):
            return {k: walk(v, path + [k]) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, path + [i]) for i, v in enumerate(node)]
        if isinstance(node, str) and len(node) >= TERMINAL_TEXT_INLINE_MAX_CHARS:
            if node == text or node == stripped:
                paths.append(path)
                strips.append(node != text)
                return ""
        return node

    copy = walk(data, [])
    if paths:
        copy[TEXT_KEY] = {"paths": paths, "strip": strips, "chars": len(text.replace("\x00", ""))}
    return copy


def _internalise_text(data: Dict[str, Any], text: str) -> Dict[str, Any]:
    """Put the answer back into the slots `_externalise_text` emptied (in
    place) and drop the marker."""
    marker = data.pop(TEXT_KEY, None)
    if not isinstance(marker, dict):
        return data
    paths = list(marker.get("paths") or [])
    strips = list(marker.get("strip") or [False] * len(paths))
    for path, strip in zip(paths, strips):
        node: Any = data
        try:
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = text.strip() if strip else text
        except (KeyError, IndexError, TypeError):
            log.error("durable log: a terminal record's text slot %r is missing", path)
    return data


def _delta_text(con: Any, response_id: str, before_sequence: int) -> str:
    """The answer as the log holds it: every delta before `before_sequence`,
    in order — one range scan of the primary key."""
    row = con.execute(
        "SELECT COALESCE(string_agg(data->>'delta', '' ORDER BY sequence_number), '') AS text "
        "FROM api_response_events WHERE response_id = %s AND sequence_number < %s AND event = %s",
        (response_id, int(before_sequence), DELTA_EVENT),
    ).fetchone()
    return str(row["text"] or "")


def _rehydrate(con: Any, response_id: str, records: List[Tuple[int, str, Dict[str, Any]]]) -> List[Tuple[int, str, Dict[str, Any]]]:
    """Fill the answer back into the terminal records of one run's page."""
    marked = [r for r in records if TEXT_KEY in r[2]]
    if not marked:
        return records
    text = _delta_text(con, response_id, min(r[0] for r in marked))
    for record in marked:
        chars = (record[2].get(TEXT_KEY) or {}).get("chars")
        if chars is not None and int(chars) != len(text):
            log.error(
                "durable log %s: the deltas hold %d characters, the terminal record was written for %s",
                response_id, len(text), chars,
            )
        _internalise_text(record[2], text)
    return records


@dataclass
class Claim:
    row: Dict[str, Any]
    last_sequence: int
    emitted_text: str
    generated_tokens: int
    last_event: Optional[str]
    key_service_account_id: Optional[str] = None
    #: The first output delta's sequence number (None: no output yet).
    first_delta_sequence: Optional[int] = None


@dataclass
class AppendResult:
    committed: Dict[str, List[int]] = field(default_factory=dict)
    lost: Set[str] = field(default_factory=set)


@dataclass
class PollResult:
    status: str
    lease_owner: Optional[str]
    lease_live: bool
    suspend_reason: Optional[str]
    events: List[EventRecord] = field(default_factory=list)


# ---------------------------------------------------------------- launch --


def mark_durable(
    response_id: str,
    *,
    dialect: str,
    item_id: str,
    engine: str,
    owner: Optional[str],
    lease_ttl_s: float,
    attempt_token: Optional[str] = None,
    body_sha256: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Make an existing open row durable and (when `owner`) take its first
    lease. Raises `psycopg.errors.UniqueViolation` when the attempt token is
    already taken in this project — the caller attaches instead."""
    with db.connection() as con:
        row = con.execute(
            """
            UPDATE api_responses SET
                resumable = true, dialect = %(dialect)s, item_id = %(item)s, engine = %(engine)s,
                attempt_token = %(token)s, body_sha256 = %(sha)s,
                enqueued_at = COALESCE(enqueued_at, now()),
                lease_owner = %(owner)s,
                lease_expires_at = CASE WHEN %(owner)s::text IS NULL THEN NULL
                                        ELSE now() + make_interval(secs => %(ttl)s) END,
                attempt = CASE WHEN %(owner)s::text IS NULL THEN attempt ELSE attempt + 1 END
            WHERE id = %(id)s AND status IN ('queued', 'in_progress')
            RETURNING *
            """,
            {
                "dialect": dialect, "item": item_id, "engine": engine, "token": attempt_token,
                "sha": body_sha256, "owner": owner, "ttl": float(lease_ttl_s), "id": response_id,
            },
        ).fetchone()
    return _row(row)


# ----------------------------------------------------------------- specs --


def put_spec(response_id: str, spec: Mapping[str, Any], blobs: Sequence[Tuple[str, int]] = ()) -> None:
    with db.connection() as con:
        con.execute(
            "INSERT INTO api_response_requests (response_id, spec) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (response_id) DO UPDATE SET spec = EXCLUDED.spec",
            (response_id, _clean_json(dict(spec))),
        )
        for sha, size in blobs:
            con.execute(
                "INSERT INTO api_response_blobs (response_id, sha256, bytes) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (response_id, sha, int(size)),
            )


def get_spec(response_id: str) -> Optional[Dict[str, Any]]:
    with db.connection() as con:
        row = con.execute(
            "SELECT spec FROM api_response_requests WHERE response_id = %s", (response_id,)
        ).fetchone()
    return None if row is None else dict(row["spec"])


def has_spec(response_ids: Sequence[str]) -> Set[str]:
    if not response_ids:
        return set()
    with db.connection() as con:
        rows = con.execute(
            "SELECT response_id FROM api_response_requests WHERE response_id = ANY(%s)",
            (list(response_ids),),
        ).fetchall()
    return {str(r["response_id"]) for r in rows}


def delete_spec(response_id: str) -> None:
    with db.connection() as con:
        con.execute("DELETE FROM api_response_requests WHERE response_id = %s", (response_id,))
        con.execute("DELETE FROM api_response_blobs WHERE response_id = %s", (response_id,))


def referenced_blobs(shas: Sequence[str]) -> Set[str]:
    if not shas:
        return set()
    with db.connection() as con:
        rows = con.execute(
            "SELECT DISTINCT sha256 FROM api_response_blobs WHERE sha256 = ANY(%s)", (list(shas),)
        ).fetchall()
    return {str(r["sha256"]) for r in rows}


# ----------------------------------------------------------------- claim --


def claim(response_id: str, owner: str, *, lease_ttl_s: float) -> Optional[Claim]:
    """Take the lease of a suspended or lapsed run, and read its resume point.

    One transaction: FOR UPDATE on the row (waits for any in-flight append's
    FOR SHARE), the lease CAS, then max(sequence_number) and the logged delta
    text. None when the run is terminal, not durable, or leased live by
    someone else."""
    with db.connection() as con:
        row = con.execute(
            """
            SELECT r.id, r.status, r.resumable, r.lease_owner,
                   (r.lease_expires_at IS NOT NULL AND r.lease_expires_at > now()) AS lease_live,
                   k.service_account_id AS key_service_account_id
            FROM api_responses r LEFT JOIN api_keys k ON k.id = r.key_id
            WHERE r.id = %s FOR UPDATE OF r
            """,
            (response_id,),
        ).fetchone()
        if row is None or not row["resumable"] or row["status"] not in OPEN_STATUSES:
            return None
        if row["lease_owner"] is not None and row["lease_live"] and row["lease_owner"] != owner:
            return None
        updated = con.execute(
            """
            UPDATE api_responses SET lease_owner = %s,
                lease_expires_at = now() + make_interval(secs => %s),
                attempt = attempt + 1, suspend_reason = NULL, suspended_at = NULL
            WHERE id = %s RETURNING *
            """,
            (owner, float(lease_ttl_s), response_id),
        ).fetchone()
        log_row = con.execute(
            f"""
            SELECT COALESCE(max(sequence_number), 0) AS last_seq,
                   min(sequence_number) FILTER (WHERE event = %s) AS first_delta,
                   COALESCE(string_agg(data->>'delta', '' ORDER BY sequence_number)
                            FILTER (WHERE event = %s), '') AS emitted,
                   COALESCE(sum(COALESCE((data->>'{TOKENS_KEY}')::bigint, 0))
                            FILTER (WHERE event = %s), 0) AS tokens
            FROM api_response_events WHERE response_id = %s
            """,
            (DELTA_EVENT, DELTA_EVENT, DELTA_EVENT, response_id),
        ).fetchone()
        last_event = None
        if int(log_row["last_seq"]) > 0:
            last = con.execute(
                "SELECT event FROM api_response_events WHERE response_id = %s AND sequence_number = %s",
                (response_id, int(log_row["last_seq"])),
            ).fetchone()
            last_event = None if last is None else str(last["event"])
    return Claim(
        row=_row(updated) or {},
        last_sequence=int(log_row["last_seq"]),
        emitted_text=str(log_row["emitted"] or ""),
        generated_tokens=int(log_row["tokens"] or 0),
        last_event=last_event,
        key_service_account_id=row["key_service_account_id"],
        first_delta_sequence=None if log_row["first_delta"] is None else int(log_row["first_delta"]),
    )


# ---------------------------------------------------------------- leases --


def renew_leases(
    owner: str,
    response_ids: Sequence[str],
    *,
    lease_ttl_s: float,
    generated_tokens: Optional[Mapping[str, int]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Renew every local lease in ONE statement, carrying each run's
    generated-token count for the deploy report (never a resume point).
    Returns {id: {key_id, cancel_requested}} for the leases still held; an id
    missing from the result has lost its lease and must stop at once."""
    if not response_ids:
        return {}
    ids = list(response_ids)
    counts = [
        None if generated_tokens is None or generated_tokens.get(i) is None else int(generated_tokens[i])
        for i in ids
    ]
    with db.connection() as con:
        rows = con.execute(
            """
            UPDATE api_responses r SET lease_expires_at = now() + make_interval(secs => %s),
                generated_tokens = COALESCE(q.tokens, r.generated_tokens)
            FROM unnest(%s::text[], %s::int[]) AS q(id, tokens)
            WHERE r.id = q.id AND r.lease_owner = %s AND r.status IN ('queued', 'in_progress')
            RETURNING r.id, r.key_id, r.cancel_requested
            """,
            (float(lease_ttl_s), ids, counts, owner),
        ).fetchall()
    return {
        str(r["id"]): {"key_id": r["key_id"], "cancel_requested": bool(r["cancel_requested"])}
        for r in rows
    }


def cancel_requested(response_ids: Sequence[str]) -> Set[str]:
    if not response_ids:
        return set()
    with db.connection() as con:
        rows = con.execute(
            "SELECT id FROM api_responses WHERE id = ANY(%s) AND cancel_requested",
            (list(response_ids),),
        ).fetchall()
    return {str(r["id"]) for r in rows}


def release(owner: str, response_ids: Sequence[str], reason: str) -> Set[str]:
    """Suspend: give the leases back with a reason. Only this owner's."""
    if not response_ids:
        return set()
    with db.connection() as con:
        rows = con.execute(
            """
            UPDATE api_responses SET lease_owner = NULL, lease_expires_at = NULL,
                suspend_reason = %s, suspended_at = now()
            WHERE id = ANY(%s) AND lease_owner = %s AND status IN ('queued', 'in_progress')
            RETURNING id
            """,
            (reason, list(response_ids), owner),
        ).fetchall()
    return {str(r["id"]) for r in rows}


_PROGRESS_COLUMNS = frozenset({
    "status", "stalled_attempts", "engine_fault_attempts", "yields", "generated_tokens",
    "recomputed_prompt_tokens", "last_incident_id", "orphaned_at_now", "orphaned_clear",
})


def update_progress(
    owner: str, response_id: str, *, metadata: Optional[Mapping[str, Any]] = None, **fields: Any
) -> bool:
    """Counters, status `in_progress`, attempt metadata — guarded by the lease."""
    sets: List[str] = []
    params: List[Any] = []
    for key, value in fields.items():
        if key not in _PROGRESS_COLUMNS:
            raise ValueError(f"api_responses.{key} is not a durable progress column")
        if key == "orphaned_at_now":
            if value:
                sets.append("orphaned_at = COALESCE(orphaned_at, now())")
            continue
        if key == "orphaned_clear":
            if value:
                sets.append("orphaned_at = NULL")
            continue
        sets.append(f"{key} = %s")
        params.append(value)
        if key == "status" and value == "in_progress":
            sets.append("started_at = COALESCE(started_at, now())")
    if metadata:
        sets.append("metadata = metadata || %s::jsonb")
        params.append(_clean_json(dict(metadata)))
    if not sets:
        return True
    params.extend([response_id, owner])
    with db.connection() as con:
        row = con.execute(
            f"UPDATE api_responses SET {', '.join(sets)} "
            "WHERE id = %s AND lease_owner = %s AND status IN ('queued', 'in_progress') RETURNING id",
            params,
        ).fetchone()
    return row is not None


def mark_followed(response_id: str) -> None:
    with db.connection() as con:
        con.execute(
            "UPDATE api_responses SET ever_followed = true WHERE id = %s AND NOT ever_followed",
            (response_id,),
        )


# ---------------------------------------------------------------- events --


def append(owner: str, batches: Mapping[str, Sequence[EventRecord]]) -> AppendResult:
    """Commit every job's records of one flush (design H).

    FAST PATH (2026-09-14, database-speed round): ONE statement for all the
    jobs. It takes FOR SHARE on each job's row while the lease is this
    owner's (rule 2 unchanged: a claim's FOR UPDATE still waits for this
    transaction), in id order, and inserts the records of the held rows with
    ON CONFLICT DO NOTHING RETURNING. A job whose row is not held (a missing
    row, a lease that moved) inserted nothing and is lost; a held job that
    got back fewer rows than it sent (a split brain, rule 3) has the rows it
    did insert deleted again in this same transaction before COMMIT, and is
    lost; every other job commits. The old shape cost 1 + 4N statements per
    flush (SELECT 1, then SAVEPOINT / SELECT FOR SHARE / INSERT / RELEASE per
    job): measured 2.1 ms p50 for 1 run and 7.0 ms for 10.

    Any database ERROR in the fast path (one job's record the server
    refuses) rolls the whole attempt back and replays the flush through the
    per-job SAVEPOINT path, so one bad job still never fails the others."""
    result = AppendResult()
    jobs = {rid: list(records) for rid, records in batches.items() if records}
    if not jobs:
        return result
    ids = sorted(jobs)
    rids: List[str] = []
    seqs: List[int] = []
    names: List[str] = []
    datas: List[str] = []
    for rid in ids:
        for record in jobs[rid]:
            rids.append(rid)
            seqs.append(int(record[0]))
            names.append(str(record[1]))
            datas.append(_clean_json(record[2]))
    with db.connection() as con:
        try:
            rows = con.execute(
                """
                WITH held AS MATERIALIZED (
                    SELECT r.id FROM api_responses r
                    WHERE r.id = ANY(%s::text[]) AND r.lease_owner = %s
                      AND r.status IN ('queued', 'in_progress')
                    ORDER BY r.id
                    FOR SHARE OF r
                ), ins AS (
                    INSERT INTO api_response_events (response_id, sequence_number, event, data)
                    SELECT t.rid, t.s, t.e, t.d::jsonb
                    FROM unnest(%s::text[], %s::int[], %s::text[], %s::text[]) AS t(rid, s, e, d)
                    WHERE t.rid IN (SELECT id FROM held)
                    ON CONFLICT (response_id, sequence_number) DO NOTHING
                    RETURNING response_id, sequence_number
                )
                SELECT id AS response_id, NULL::int AS sequence_number FROM held
                UNION ALL
                SELECT response_id, sequence_number FROM ins
                """,
                (ids, owner, rids, seqs, names, datas),
            ).fetchall()
        except psycopg.Error:
            log.warning("durable append: one statement failed; isolating each job", exc_info=True)
            con.rollback()
            return _append_isolated(con, owner, jobs)
        held: Set[str] = set()
        inserted: Dict[str, List[int]] = {}
        for row in rows:
            rid = str(row["response_id"])
            if row["sequence_number"] is None:
                held.add(rid)
            else:
                inserted.setdefault(rid, []).append(int(row["sequence_number"]))
        for rid in ids:
            got = inserted.get(rid, [])
            if rid in held and len(got) == len(jobs[rid]):
                result.committed[rid] = sorted(got)
                continue
            if got:
                # Rule 3: a conflicting sequence number. The rows this job
                # did insert are this transaction's own; take them back so
                # nothing of the loser's batch commits.
                con.execute(
                    "DELETE FROM api_response_events WHERE response_id = %s AND sequence_number = ANY(%s::int[])",
                    (rid, got),
                )
            result.lost.add(rid)
    return result


def _append_isolated(con: Any, owner: str, jobs: Mapping[str, Sequence[EventRecord]]) -> AppendResult:
    """The per-job SAVEPOINT shape (the pre-2026-09-14 `append`): each job's
    FOR SHARE + INSERT in its own savepoint, so a database error for one job
    rolls back only that job. Runs on `con` in a fresh transaction."""
    result = AppendResult()
    con.execute("SELECT 1")
    for response_id in sorted(jobs):
        records = jobs[response_id]
        con.execute("SAVEPOINT durable_job")
        try:
            held = con.execute(
                "SELECT id FROM api_responses WHERE id = %s AND lease_owner = %s "
                "AND status IN ('queued', 'in_progress') FOR SHARE",
                (response_id, owner),
            ).fetchone()
            if held is None:
                con.execute("ROLLBACK TO SAVEPOINT durable_job")
                result.lost.add(response_id)
                continue
            seqs = [int(r[0]) for r in records]
            names = [str(r[1]) for r in records]
            datas = [_clean_json(r[2]) for r in records]
            inserted = con.execute(
                """
                INSERT INTO api_response_events (response_id, sequence_number, event, data)
                SELECT %s, s, e, d::jsonb FROM unnest(%s::int[], %s::text[], %s::text[]) AS t(s, e, d)
                ON CONFLICT (response_id, sequence_number) DO NOTHING
                RETURNING sequence_number
                """,
                (response_id, seqs, names, datas),
            ).fetchall()
            if len(inserted) != len(records):
                con.execute("ROLLBACK TO SAVEPOINT durable_job")
                result.lost.add(response_id)
                continue
            con.execute("RELEASE SAVEPOINT durable_job")
            result.committed[response_id] = sorted(int(r["sequence_number"]) for r in inserted)
        except psycopg.Error:
            log.warning("durable append failed for %s; isolating the job", response_id, exc_info=True)
            con.execute("ROLLBACK TO SAVEPOINT durable_job")
            result.lost.add(response_id)
    return result


def discard_unlaunched(response_id: str) -> None:
    """Delete a row that never became durable (its attempt token lost a race)."""
    with db.connection() as con:
        con.execute(
            "DELETE FROM api_responses WHERE id = %s AND NOT resumable AND status = 'queued'",
            (response_id,),
        )


def list_events(response_id: str, after: int = 0, limit: int = 1000) -> List[EventRecord]:
    with db.connection() as con:
        rows = con.execute(
            "SELECT sequence_number, event, data FROM api_response_events "
            "WHERE response_id = %s AND sequence_number > %s ORDER BY sequence_number LIMIT %s",
            (response_id, int(after), int(limit)),
        ).fetchall()
        records = [(int(r["sequence_number"]), str(r["event"]), dict(r["data"])) for r in rows]
        return _rehydrate(con, response_id, records)


def log_outcome(response_id: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """What a synchronous result rebuilt from the log needs, in ONE statement:
    the answer (every delta, in order) and the data of the last
    `response.failed` record (None when there is none). Replaces fetching
    every event row into Python (2026-09-14: 509 ms measured for a
    100,000-event log, `durable.outcome_from_log`)."""
    with db.connection() as con:
        row = con.execute(
            """
            SELECT
              (SELECT COALESCE(string_agg(data->>'delta', '' ORDER BY sequence_number), '')
                 FROM api_response_events WHERE response_id = %s AND event = %s) AS text,
              (SELECT data FROM api_response_events
                 WHERE response_id = %s AND event = 'response.failed'
                 ORDER BY sequence_number DESC LIMIT 1) AS failed
            """,
            (response_id, DELTA_EVENT, response_id),
        ).fetchone()
    failed = row["failed"]
    return str(row["text"] or ""), (None if failed is None else dict(failed))


def poll_many(after_by_id: Mapping[str, int], *, limit_per: int = 500) -> Dict[str, PollResult]:
    """ONE statement for every remote follower of this process: each run's
    status and lease, plus its events after the follower's position."""
    if not after_by_id:
        return {}
    ids = list(after_by_id)
    afters = [int(after_by_id[i]) for i in ids]
    with db.connection() as con:
        rows = con.execute(
            """
            SELECT q.id, r.status, r.lease_owner, r.suspend_reason,
                   (r.lease_expires_at IS NOT NULL AND r.lease_expires_at > now()) AS lease_live,
                   e.sequence_number, e.event, e.data
            FROM unnest(%s::text[], %s::int[]) AS q(id, after)
            JOIN api_responses r ON r.id = q.id
            LEFT JOIN LATERAL (
                SELECT sequence_number, event, data FROM api_response_events ev
                WHERE ev.response_id = q.id AND ev.sequence_number > q.after
                ORDER BY sequence_number LIMIT %s
            ) e ON true
            ORDER BY q.id, e.sequence_number
            """,
            (ids, afters, int(limit_per)),
        ).fetchall()
    out: Dict[str, PollResult] = {}
    for r in rows:
        rid = str(r["id"])
        entry = out.get(rid)
        if entry is None:
            entry = PollResult(
                status=str(r["status"]), lease_owner=r["lease_owner"],
                lease_live=bool(r["lease_live"]), suspend_reason=r["suspend_reason"],
            )
            out[rid] = entry
        if r["sequence_number"] is not None:
            entry.events.append((int(r["sequence_number"]), str(r["event"]), dict(r["data"])))
    marked = [rid for rid, entry in out.items() if any(TEXT_KEY in e[2] for e in entry.events)]
    if marked:
        # Only a poll that reaches a long answer's terminal records (once per
        # run per follower) pays for this second, range-scan statement.
        with db.connection() as con:
            for rid in marked:
                out[rid].events = _rehydrate(con, rid, out[rid].events)
    return out


def discard_output_after(owner: str, response_id: str, after_sequence: int) -> bool:
    """OCR restart from scratch (design: only when never followed): drop the
    output events after `after_sequence`, under the lease."""
    with db.connection() as con:
        held = con.execute(
            "SELECT id FROM api_responses WHERE id = %s AND lease_owner = %s AND NOT ever_followed FOR UPDATE",
            (response_id, owner),
        ).fetchone()
        if held is None:
            return False
        con.execute(
            "DELETE FROM api_response_events WHERE response_id = %s AND sequence_number > %s",
            (response_id, int(after_sequence)),
        )
    return True


# -------------------------------------------------------------- terminal --

_FINISH_COLUMNS = frozenset({
    "status", "error_code", "error_message", "input_tokens", "output_tokens", "ttft_ms",
    "duration_ms", "output_text", "finish_reason", "max_output_tokens", "generated_tokens",
    "recomputed_prompt_tokens", "stalled_attempts", "engine_fault_attempts", "yields",
    "last_incident_id",
})


def _stored_data(record: EventRecord, text: Optional[str]) -> Dict[str, Any]:
    if text is None or len(text) < TERMINAL_TEXT_INLINE_MAX_CHARS or record[1] == DELTA_EVENT:
        return record[2]
    return _externalise_text(record[2], text)


def finish(
    owner: Optional[str],
    response_id: str,
    records: Sequence[EventRecord],
    fields: Mapping[str, Any],
    *,
    metadata: Optional[Mapping[str, Any]] = None,
    text: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Settle ONCE: terminal records + status + counters + spec deletion, in
    one transaction under the lease. `owner=None` settles a run whose lease is
    NULL or lapsed (the suspended-unread TTL, a revoked key's suspended run).
    Returns the settled row, or None when somebody else owns or already
    settled it.

    `text`: the whole answer, which MUST equal the run's logged deltas in
    order (the runner builds it from exactly those pieces). When it is at
    least TERMINAL_TEXT_INLINE_MAX_CHARS long, the non-delta records store
    it by reference (see `_externalise_text`); readers get it back."""
    status = str(fields.get("status") or "")
    if status not in TERMINAL_STATUSES:
        raise ValueError("finish needs a terminal status")
    with db.connection() as con:
        if owner is None:
            held = con.execute(
                "SELECT id FROM api_responses WHERE id = %s AND status IN ('queued','in_progress') "
                "AND (lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= now()) FOR UPDATE",
                (response_id,),
            ).fetchone()
        else:
            held = con.execute(
                "SELECT id FROM api_responses WHERE id = %s AND lease_owner = %s "
                "AND status IN ('queued','in_progress') FOR UPDATE",
                (response_id, owner),
            ).fetchone()
        if held is None:
            return None
        if records:
            inserted = con.execute(
                """
                INSERT INTO api_response_events (response_id, sequence_number, event, data)
                SELECT %s, s, e, d::jsonb FROM unnest(%s::int[], %s::text[], %s::text[]) AS t(s, e, d)
                ON CONFLICT (response_id, sequence_number) DO NOTHING RETURNING sequence_number
                """,
                (
                    response_id,
                    [int(r[0]) for r in records],
                    [str(r[1]) for r in records],
                    [_clean_json(_stored_data(r, text)) for r in records],
                ),
            ).fetchall()
            if len(inserted) != len(records):
                con.rollback()
                return None
        sets = ["lease_owner = NULL", "lease_expires_at = NULL", "orphaned_at = NULL",
                "completed_at = COALESCE(completed_at, now())"]
        params: List[Any] = []
        for key, value in fields.items():
            if key not in _FINISH_COLUMNS:
                raise ValueError(f"api_responses.{key} is not settable at finish")
            if isinstance(value, str):
                value = value.replace("\x00", "")
            sets.append(f"{key} = %s")
            params.append(value)
        if metadata:
            sets.append("metadata = metadata || %s::jsonb")
            params.append(_clean_json(dict(metadata)))
        params.append(response_id)
        row = con.execute(
            f"UPDATE api_responses SET {', '.join(sets)} WHERE id = %s RETURNING *", params
        ).fetchone()
        con.execute("DELETE FROM api_response_requests WHERE response_id = %s", (response_id,))
        con.execute("DELETE FROM api_response_blobs WHERE response_id = %s", (response_id,))
    return _row(row)


# ---------------------------------------------------------------- sweeps --


#: response id → gate_engine ('' for none). A run's gate never changes, and
#: reading it means detoasting the stored spec (the whole prompt), so each
#: row's is read once per process. Bounded.
_GATE_CACHE: "collections.OrderedDict[str, str]" = collections.OrderedDict()
_GATE_CACHE_MAX = 20_000


def _gates_of(con: Any, ids: Sequence[str]) -> Dict[str, str]:
    missing = [rid for rid in ids if rid not in _GATE_CACHE]
    if missing:
        found = {
            str(r["response_id"]): str(r["gate_engine"] or "")
            for r in con.execute(
                "SELECT response_id, spec->'spec'->>'gate_engine' AS gate_engine "
                "FROM api_response_requests WHERE response_id = ANY(%s)",
                (missing,),
            ).fetchall()
        }
        for rid in missing:
            if rid in found:  # no spec yet (a launch mid-write): not cached, read again next time
                _GATE_CACHE[rid] = found[rid]
        while len(_GATE_CACHE) > _GATE_CACHE_MAX:
            _GATE_CACHE.popitem(last=False)
    return {rid: _GATE_CACHE.get(rid, "") for rid in ids}


def due_for_resume(*, limit: int = 20, per_engine: Optional[int] = None) -> List[Dict[str, Any]]:
    """Background durable runs nobody holds: oldest enqueued first.

    `per_engine` (2026-09-14 review P5): at most that many rows PER ENGINE,
    so the rows of a busy engine can never hide another engine's head of
    line. The window runs over api_responses only; the gate is read from the
    stored spec just for the rows returned (and cached, see _GATE_CACHE)."""
    with db.connection() as con:
        if per_engine is None:
            rows = con.execute(
                """
                SELECT r.id, r.project_id, r.engine, r.enqueued_at, r.engine_fault_attempts, r.cancel_requested
                FROM api_responses r
                WHERE r.resumable AND r.background AND r.status IN ('queued', 'in_progress')
                  AND (r.lease_owner IS NULL OR r.lease_expires_at IS NULL OR r.lease_expires_at <= now())
                ORDER BY r.enqueued_at NULLS LAST, r.created_at LIMIT %s
                """,
                (int(limit),),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT id, project_id, engine, enqueued_at, engine_fault_attempts, cancel_requested
                FROM (
                    SELECT r.id, r.project_id, r.engine, r.enqueued_at, r.created_at, r.engine_fault_attempts,
                           r.cancel_requested,
                           row_number() OVER (PARTITION BY r.engine
                                              ORDER BY r.enqueued_at NULLS LAST, r.created_at) AS place
                    FROM api_responses r
                    WHERE r.resumable AND r.background AND r.status IN ('queued', 'in_progress')
                      AND (r.lease_owner IS NULL OR r.lease_expires_at IS NULL OR r.lease_expires_at <= now())
                ) due
                WHERE place <= %s
                ORDER BY enqueued_at NULLS LAST, created_at LIMIT %s
                """,
                (int(per_engine), int(limit)),
            ).fetchall()
        out = [dict(r) for r in rows]
        gates = _gates_of(con, [str(r["id"]) for r in out])
    for row in out:
        row["gate_engine"] = gates.get(str(row["id"])) or None
    return out


def open_unclaimed(response_ids: Sequence[str], owner: str) -> Set[str]:
    """Of `response_ids`, the rows still open whose lease is free or this
    owner's — the ones a process may keep a queued hook or a slot for."""
    ids = [rid for rid in dict.fromkeys(response_ids) if rid]
    if not ids:
        return set()
    with db.connection() as con:
        rows = con.execute(
            """
            SELECT id FROM api_responses
            WHERE id = ANY(%s) AND status IN ('queued', 'in_progress')
              AND (lease_owner IS NULL OR lease_owner = %s OR lease_expires_at IS NULL OR lease_expires_at <= now())
            """,
            (ids, owner),
        ).fetchall()
    return {str(r["id"]) for r in rows}


def suspended_unread(*, ttl_s: float, limit: int = 100) -> List[str]:
    """Non-background durable runs with no lease whose suspension (or lapsed
    lease) is older than the unread TTL."""
    with db.connection() as con:
        rows = con.execute(
            """
            SELECT id FROM api_responses
            WHERE resumable AND NOT background AND status IN ('queued', 'in_progress')
              AND (lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= now())
              AND COALESCE(suspended_at, lease_expires_at, created_at) < now() - make_interval(secs => %s)
            ORDER BY created_at LIMIT %s
            """,
            (float(ttl_s), int(limit)),
        ).fetchall()
    return [str(r["id"]) for r in rows]


def queued_head(engine: str) -> Optional[Dict[str, Any]]:
    with db.connection() as con:
        row = con.execute(
            "SELECT id, enqueued_at FROM api_responses WHERE engine = %s AND status = 'queued' "
            "AND background ORDER BY enqueued_at LIMIT 1",
            (engine,),
        ).fetchone()
    return None if row is None else dict(row)


def find_by_attempt(project_id: str, attempt_token: str) -> Optional[Dict[str, Any]]:
    with db.connection() as con:
        row = con.execute(
            "SELECT r.*, k.service_account_id AS key_service_account_id FROM api_responses r "
            "LEFT JOIN api_keys k ON k.id = r.key_id WHERE r.project_id = %s AND r.attempt_token = %s",
            (project_id, attempt_token),
        ).fetchone()
    return _row(row)


def find_implicit(
    project_id: str, key_id: str, dialect: str, body_sha256: str, *, window_s: float
) -> Optional[Dict[str, Any]]:
    """The design's implicit attach match: same key, route and body; created
    within the window; orphaned (still generating, nobody reading) or
    suspended/lapsed."""
    with db.connection() as con:
        row = con.execute(
            """
            SELECT r.*, k.service_account_id AS key_service_account_id FROM api_responses r
            LEFT JOIN api_keys k ON k.id = r.key_id
            WHERE r.project_id = %s AND r.key_id = %s AND r.body_sha256 = %s AND r.dialect = %s
              AND r.status IN ('queued', 'in_progress') AND r.resumable
              AND r.created_at > now() - make_interval(secs => %s)
              AND (r.orphaned_at IS NOT NULL OR r.lease_owner IS NULL
                   OR r.lease_expires_at IS NULL OR r.lease_expires_at <= now())
            ORDER BY r.created_at DESC LIMIT 1
            """,
            (project_id, key_id, body_sha256, dialect, float(window_s)),
        ).fetchone()
    return _row(row)


def get_run(response_id: str) -> Optional[Dict[str, Any]]:
    """Unscoped read for the runtime itself (never for a caller: every public
    lookup goes through the project-scoped `db.get_api_response` first)."""
    with db.connection() as con:
        row = con.execute(
            "SELECT r.*, k.service_account_id AS key_service_account_id, "
            "(r.lease_expires_at IS NOT NULL AND r.lease_expires_at > now()) AS lease_live "
            "FROM api_responses r LEFT JOIN api_keys k ON k.id = r.key_id WHERE r.id = %s",
            (response_id,),
        ).fetchone()
    return _row(row)


def still_authorised_shim(key_ids: Sequence[str], model: Optional[str] = None) -> Set[str]:
    """SHIM(T3): the resolver's predicate over stored rows, for when
    `resolver.still_authorised` has not landed (ASSEMBLER: delete once T3's
    batch function exists; durable.py prefers it automatically).

    Status, expiry, rotation window, service account, project, the tenancy
    consistency checks (key/project workspace, key/project and account/project
    ids), the workspace rung through the resolver's own reader, and — when
    `model` is given — the effective allowed_models narrowed project →
    service account → key exactly as the resolver computes it (2026-09-14
    review P8: without it an admin removing a model from a key or project
    could not stop a running job). Not the ip_allowlist rung: a run has no
    request address after the request that launched it."""
    from ..apiplatform import resolver

    ids = [k for k in dict.fromkeys(key_ids) if k]
    if not ids:
        return set()
    with db.connection() as con:
        rows = con.execute(
            """
            SELECT k.id, k.workspace_id AS key_workspace_id, k.project_id AS key_project_id,
                   k.allowed_models AS key_allowed_models,
                   p.id AS project_id, p.workspace_id AS project_workspace_id,
                   p.allowed_models AS project_allowed_models,
                   s.id AS account_id, s.project_id AS account_project_id,
                   s.allowed_models AS account_allowed_models
            FROM api_keys k
            JOIN api_projects p ON p.id = k.project_id
            LEFT JOIN api_service_accounts s ON s.id = k.service_account_id
            WHERE k.id = ANY(%s) AND k.status = 'active' AND p.status = 'active'
              AND (k.expires_at IS NULL OR k.expires_at > now())
              AND (k.rotation_expires_at IS NULL OR k.rotation_expires_at > now())
              AND (k.service_account_id IS NULL OR s.status = 'active')
            """,
            (ids,),
        ).fetchall()
    allowed: Set[str] = set()
    for row in rows:
        workspace_id = str(row["project_workspace_id"] or row["key_workspace_id"] or "")
        if not workspace_id or resolver.workspace_status(workspace_id) != "active":
            continue
        if str(row["key_workspace_id"] or "") != workspace_id:
            continue
        if str(row["key_project_id"] or "") != str(row["project_id"] or ""):
            continue
        if row["account_id"] is not None and str(row["account_project_id"] or "") != str(row["project_id"] or ""):
            continue
        if model:
            account = None if row["account_id"] is None else {"allowed_models": row["account_allowed_models"]}
            models = resolver._effective_models(
                {"allowed_models": row["key_allowed_models"]}, account,
                {"allowed_models": row["project_allowed_models"]},
            )
            if models and str(model) not in models:
                continue
        allowed.add(str(row["id"]))
    return allowed


# ------------------------------------------------------------- retention --


#: Runs whose stored log `purge_events` may delete now (see there).
_PURGE_DUE_SQL = (
    db._RETAINED_LOG_IDS_CTE
    + " SELECT r.id FROM logs JOIN api_responses r ON r.id = logs.response_id"
    f" WHERE (r.status IN {db._API_TERMINAL_SQL}"
    "         AND r.completed_at < now() - make_interval(secs => %s))"
    f"    OR (r.expires_at IS NOT NULL AND r.expires_at < now() AND {db._API_EXPIRED_PRUNABLE_SQL})"
    " LIMIT %s"
)


#: Due runs per list read: the DELETE below bounds rows, not runs, so a long
#: list costs nothing extra and many short runs drain in few statements.
_PURGE_DUE_IDS = 500

#: Up to %s events of the listed runs, through the event primary key.
_PURGE_DELETE_SQL = (
    "DELETE FROM api_response_events e USING ("
    " SELECT response_id, sequence_number FROM api_response_events"
    " WHERE response_id = ANY(%s::text[]) LIMIT %s) d"
    " WHERE e.response_id = d.response_id AND e.sequence_number = d.sequence_number"
)


def purge_events(*, retention_s: float, batch: int = 5000, max_batches: int = 200) -> int:
    """Delete stored events of runs terminal for longer than the retention
    (PUBLIC_API_EVENT_RETENTION_S, 3,600 s) and of expired prunable rows, in
    statements of at most `batch` rows each — never one DELETE that walks a
    million-event run under statement_timeout (design L); `max_batches`
    bounds the DELETE statements of one call. Also drops specs and blob
    references a crash left on terminal rows. Returns the events deleted.

    INDEX-DRIVEN (2026-09-14, database-speed round). The due set used to be
    a ctid batch whose join to api_responses carried an OR predicate, and
    PostgreSQL planned it as a sequential scan of the WHOLE log with a
    response probe per event: every 30 s sweep read every retained event
    even with nothing due (measured 6.9 ms at 54k events, 748 ms at 5.8M,
    196,863 cold buffers per call). Now the runs that still HAVE a log come
    from a loose index scan of the event primary key
    (`db._RETAINED_LOG_IDS_CTE`), each is checked on its response row, and a
    due run is deleted by sequence-number range
    (`db._delete_response_events_batched`). The cost follows the runs with a
    retained log, not the events.

    Expired rows follow `db._API_EXPIRED_PRUNABLE_SQL`, like the 30-min
    prune: an OPEN resumable run's log is never purged for its expiry — its
    runner settles it first (T1 review, 2026-09-14)."""
    size = max(1, int(batch))
    budget = [max(1, int(max_batches))]
    deleted = 0
    while budget[0] > 0:
        with db.connection() as con:
            ids = [
                str(r["id"])
                for r in con.execute(_PURGE_DUE_SQL, (float(retention_s), _PURGE_DUE_IDS)).fetchall()
            ]
        if not ids:
            break
        # ONE bounded statement for the whole due list, repeated while it
        # fills (verifier fix, 2026-09-14): a statement per RUN capped the
        # sweep at `max_batches` runs per call — measured 152 calls (76 min
        # of 30 s sweeps) to drain 30,000 short runs that the ctid form
        # purged in one call. The inner SELECT is an index scan of the event
        # primary key over the listed ids that stops at `size` rows.
        progressed = 0
        while budget[0] > 0:
            budget[0] -= 1
            with db.connection() as con:
                count = max(0, int(con.execute(_PURGE_DELETE_SQL, (ids, size)).rowcount or 0))
            progressed += count
            if count < size:
                break
        deleted += progressed
        if progressed == 0:
            break
    with db.connection() as con:
        con.execute(
            "DELETE FROM api_response_requests q USING api_responses r "
            "WHERE r.id = q.response_id AND r.status IN ('completed', 'failed', 'cancelled')"
        )
        con.execute(
            "DELETE FROM api_response_blobs b USING api_responses r "
            "WHERE r.id = b.response_id AND r.status IN ('completed', 'failed', 'cancelled')"
        )
    return deleted


def fail_interrupted_foreground(
    *, created_before: Any, error_code: str, error_message: str, limit: int = 200
) -> List[str]:
    """Close NON-durable foreground rows a previous process left open.

    WHY (verifier, 2026-09-14). A synchronous or streaming `/v1` request that
    was not durable (`store: false`, or a process whose durable runtime was
    not running) lives in the coroutine of its connection; a restart kills it
    between the row's `queued` write and the recorder's terminal write, and
    the row said `queued` for ever — `GET /v1/responses/{id}` told a client to
    keep waiting for a generation nobody runs. A row created before THIS
    process started cannot be running here (one orchestrator process owns
    `/v1`), and a non-resumable row is nobody else's to resume. Background rows
    are excluded: `background.repair_if_orphaned` closes those one at a time
    because each owes its webhook. Bounded per call; `SKIP LOCKED` so two
    sweeps never wait on each other."""
    with db.connection() as con:
        rows = con.execute(
            """
            UPDATE api_responses SET status = 'failed', error_code = %s, error_message = %s,
                   completed_at = COALESCE(completed_at, now())
            WHERE id IN (
                SELECT id FROM api_responses
                WHERE status IN ('queued', 'in_progress') AND NOT background
                  AND NOT COALESCE(resumable, false) AND created_at < %s
                ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED
            )
            RETURNING id
            """,
            (error_code, error_message, created_before, int(limit)),
        ).fetchall()
    return [str(r["id"]) for r in rows]


def events_retained(response_id: str) -> bool:
    with db.connection() as con:
        row = con.execute(
            "SELECT 1 FROM api_response_events WHERE response_id = %s LIMIT 1", (response_id,)
        ).fetchone()
    return row is not None


def durable_counts() -> Dict[str, int]:
    """For T1's `deploy-drain.sh api` report: running (live lease),
    suspended, queued background, quarantined, and the tokens a resume of
    every open run would re-prefill (generated so far)."""
    with db.connection() as con:
        row = con.execute(
            """
            SELECT
              count(*) FILTER (WHERE lease_owner IS NOT NULL AND lease_expires_at > now()) AS running,
              count(*) FILTER (WHERE lease_owner IS NULL OR lease_expires_at <= now()) AS suspended,
              count(*) FILTER (WHERE status = 'queued' AND background) AS queued,
              count(*) FILTER (WHERE engine_fault_attempts >= 1) AS quarantined,
              COALESCE(sum(generated_tokens), 0) AS reprefill_generated_tokens
            FROM api_responses WHERE resumable AND status IN ('queued', 'in_progress')
            """
        ).fetchone()
    return {k: int(v or 0) for k, v in dict(row).items()}


__all__ = [
    "AppendResult",
    "Claim",
    "EventRecord",
    "PollResult",
    "SCHEMA_SQL",
    "append",
    "cancel_requested",
    "claim",
    "delete_spec",
    "discard_output_after",
    "discard_unlaunched",
    "due_for_resume",
    "durable_counts",
    "ensure_schema",
    "events_retained",
    "find_by_attempt",
    "find_implicit",
    "finish",
    "get_run",
    "get_spec",
    "has_spec",
    "list_events",
    "log_outcome",
    "mark_durable",
    "mark_followed",
    "poll_many",
    "purge_events",
    "put_spec",
    "queued_head",
    "referenced_blobs",
    "release",
    "renew_leases",
    "still_authorised_shim",
    "suspended_unread",
    "update_progress",
]
