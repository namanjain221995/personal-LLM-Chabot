"""Chat retention: the owner-approved deletion periods, enforced once a day
(Salesforce memory design v2 §12.1-§12.2, 2026-11-25; docs/retention.md).

WHAT IT DELETES. Only what the owner approved, and nothing younger:
  * `query_traces` started more than QUERY_TRACES_RETENTION_DAYS ago (180);
    their `query_trace_events` go by the foreign key's cascade, the only FK
    onto that table. A trace waits while a request of its generation is
    still within its own period (see the stamp below);
  * orphan traces, older than one day, whose chat has no conversations row
    any more (a chat removed by the users-FK cascade, which the traces'
    `user_id ON DELETE SET NULL` leaves behind). A bare-API key (`u<id>-…`,
    main.py's `scoped_session`) never has a conversations row and is spared,
    and so is a trace an evaluator tagged with a `test_case_id` (C24): both
    wait for the 180-day rule;
  * `chat_requests` created more than CHAT_REQUESTS_RETENTION_DAYS ago (30)
    and in a TERMINAL status only: completed, failed, cancelled (C25). A row
    in a resumable status (main.py `_RESUMABLE_STATUSES`: interrupted,
    accepted, running, queued) is counted and logged when it is that old and
    never deleted (D8: that needs the owner's word);
  * orphan `chat_requests`, older than one day, terminal, whose chat is gone,
    sparing bare-API keys (C24): those keep the 30-day rule;
  * `sf_query_state` rows past their own `expires_at` (NULL = the row lives
    with its chat). The table arrives with V45; until then the item is
    skipped cleanly;
  * the older Salesforce route's orphans (`sf_conversation_state`,
    `sf_intents`, `sf_clarifications` rows whose chat is gone), ONLY when
    CHAT_RETENTION_OLD_ROUTE_ORPHANS is on (D9, off by default: not in the
    approved list). A dry run always counts them, so the owner sees the
    numbers before deciding.
Chats, messages and everything else stay until the person deletes the chat.

IT STAMPS BEFORE IT DELETES (review DS2-1; toggle-off review F4). The
toggle-off guard reads a `query_traces` or `chat_requests` row as the
server's word that an answer was a Salesforce answer
(`sf_isolation.sf_answer_sql`, matched on the answer's `generation_id`).
Answers stored before the guard stamped `meta.mode` (a failure record, a
detached answer, the tab's copy of a stopped answer) have no other mark, so
deleting that row would let the guard pass the answer into toggle-off
turns. So each batch that deletes trace or request rows first stamps
`meta.mode = 'salesforce'` on the answers those rows mark, in the SAME
transaction (one statement): only an assistant row the guard reads as a
Salesforce answer through one of the batch's rows, only when its meta has
no mode and no other Salesforce mark, and only by adding that one key; the
content and every other key stay as they are. The guard then hides what
it hid before the run, apart from the cases docs/retention.md lists under
"What it cannot reach". Requests go before traces because the guard
reads a request only for a generation with no trace: with the traces
still in place, each answer is stamped by the row the guard actually reads.
So a trace does not go while its request still stands in a live chat,
unless the job keeps that request for good (a resumable one, D8) (review
R1-1): a run whose request item fails or stops at the cap holds the trace
items back until the next tick, and a trace waits while a request of its
generation is still within its own period (CHAT_REQUESTS_RETENTION_DAYS
set past the trace period).
A dry run counts the answers it would stamp with the same text
(`messages_stamped`); when an item whose rows mark answers fails, that
total is reported as unknown, never as a short number (review R2-4). The
rows still go at the approved periods.

ONE RUNNER (C27). The run opens ONE dedicated connection (not a pooled one,
so a lock can never be handed back to the pool with it), takes the
session-level `pg_try_advisory_lock(_LOCK_KEY)` and holds it for the whole
run. A transaction-level lock would cover a single batch; this one spans
every batch. A second process (a deploy briefly overlaps two orchestrators)
finds the lock taken and returns at once. Closing the connection ends the
session, so the lock cannot outlive the run even when the unlock fails.

BATCHED (the house style of `db.prune_api_platform`). Each delete takes at
most CHAT_RETENTION_BATCH rows by `ctid`, one transaction per batch, until a
batch comes back short or CHAT_RETENTION_MAX_BATCHES have run; the next tick
continues. Every cut-off is computed by the DATABASE clock (`now()`), never
by this process's clock.

DRY RUN FIRST. CHAT_RETENTION_ENABLED is off by default and
CHAT_RETENTION_DRY_RUN on. A dry run counts every item with the SAME `WHERE`
text the delete uses (one `_Item.where` feeds both statements), counts the
answers a real run would stamp the same way, logs the counts only, sets the
`retention_candidates{table}` gauge and deletes and stamps nothing. A real
run counts `retention_deleted_total{table}` and
`retention_stamped_total{table}`. Logs carry counts, never content.

ONCE A BUSINESS DAY. The maintenance loop calls this every 30 minutes; real
work happens once per business day (Asia/Kolkata) per process. A run that
hit a batch cap or met an error is not marked done, so the next tick goes on
where it stopped. Repeats are idempotent.

SETTINGS. Read with `getattr` and safe defaults, so this module works before
and after config.py declares them (design §12.2). A period below the approved
one is raised to it with a warning: a setting can lengthen what is kept,
never delete more than the owner approved.

NEVER RAISES. Every failure is logged and the run returns a status word; one
item that fails does not stop the others.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.rows import dict_row

from . import db, metrics
from .config import settings
from .sf_isolation import sf_answer_sql, sf_meta_sql

log = logging.getLogger(__name__)

#: chat_requests statuses a retention delete may take: the request has ended
#: and no resume will ever act on it. An allow-list, so a status added later
#: is kept until someone decides about it.
TERMINAL_STATUSES: Tuple[str, ...] = ("completed", "failed", "cancelled")

#: The statuses a resume may act on. A copy of main.py `_RESUMABLE_STATUSES`
#: (importing main here would be a cycle); tests/test_retention.py pins the
#: two equal, and pins terminal + resumable to the table's CHECK.
RESUMABLE_STATUSES: Tuple[str, ...] = ("interrupted", "accepted", "running", "queued")

#: A bare API call's conversation key (main.py `scoped_session`,
#: f"u{viewer}-{session_id}"). It never has a conversations row, so "no
#: conversations row" is not "orphan" for it.
BARE_API_KEY_PATTERN = "^u[0-9]+-"

#: The approved periods (owner, 2026-11-25). Settings may raise them only.
APPROVED_QUERY_TRACES_DAYS = 180
APPROVED_CHAT_REQUESTS_DAYS = 30

#: An orphan must be at least this old: a chat row can lag its first trace
#: or request by a moment, and a day leaves no doubt.
ORPHAN_GRACE_DAYS = 1

#: The pre-prune backup is deleted after this many days (D11).
BACKUP_KEEP_DAYS = 30

#: What a run merges into the meta of an answer whose only Salesforce mark
#: is a row it is about to delete (review DS2-1). One key, added with `||`.
STAMP = '{"mode": "salesforce"}'

#: The count key (and the log word) of the answers stamped, or in a dry run
#: the answers a real run would stamp.
STAMPED = "messages_stamped"

#: The business day the once-a-day gate counts in. India keeps no daylight
#: saving, so a fixed offset is exact and needs no tz database.
BUSINESS_TZ = timezone(timedelta(hours=5, minutes=30), "Asia/Kolkata")

#: Any stable 64-bit constant that no other advisory lock in this database
#: uses ("CHTRET"); `_MIGRATION_LOCK_KEY` in db.py is the other constant one.
_LOCK_KEY = 0x4348_5452_4554

#: Bounds on the batch settings, so a typo cannot ask for a million-row
#: statement or an endless run.
_BATCH_RANGE = (1, 10_000)
_MAX_BATCHES_RANGE = (1, 1_000)
#: Upper bound on a period setting (ten years); anything longer is a typo.
_MAX_DAYS = 3650

_TRUE_WORDS = {"1", "true", "yes", "on"}
_FALSE_WORDS = {"0", "false", "no", "off", ""}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionConfig:
    """The settings of one run, already checked and bounded."""

    enabled: bool
    dry_run: bool
    query_traces_days: int
    chat_requests_days: int
    batch: int
    max_batches: int
    old_route_orphans: bool
    backup_date: str


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    word = str(value).strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return default if word == "" else False
    log.warning("retention: unreadable boolean setting %r, using %s", word[:20], default)
    return default


def _as_int(value: Any, default: int, low: int, high: int, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        log.warning("retention: unreadable %s, using %d", name, default)
        return default
    return max(low, min(high, number))


def _period(value: Any, approved: int, name: str) -> int:
    """A retention period in days, never shorter than the approved one."""
    days = _as_int(value, approved, 1, _MAX_DAYS, name)
    if days < approved:
        log.warning(
            "retention: %s=%d is shorter than the approved %d days; using %d "
            "(a shorter period needs the owner's word and a code change)",
            name, days, approved, approved,
        )
        return approved
    return days


def load_config() -> RetentionConfig:
    """The settings, by `getattr` with the design's defaults (§12.2), so the
    module runs the same before and after config.py declares them."""
    return RetentionConfig(
        enabled=_as_bool(getattr(settings, "chat_retention_enabled", False), False),
        dry_run=_as_bool(getattr(settings, "chat_retention_dry_run", True), True),
        query_traces_days=_period(
            getattr(settings, "query_traces_retention_days", APPROVED_QUERY_TRACES_DAYS),
            APPROVED_QUERY_TRACES_DAYS, "QUERY_TRACES_RETENTION_DAYS",
        ),
        chat_requests_days=_period(
            getattr(settings, "chat_requests_retention_days", APPROVED_CHAT_REQUESTS_DAYS),
            APPROVED_CHAT_REQUESTS_DAYS, "CHAT_REQUESTS_RETENTION_DAYS",
        ),
        batch=_as_int(
            getattr(settings, "chat_retention_batch", 1000), 1000, *_BATCH_RANGE,
            name="CHAT_RETENTION_BATCH",
        ),
        max_batches=_as_int(
            getattr(settings, "chat_retention_max_batches", 20), 20, *_MAX_BATCHES_RANGE,
            name="CHAT_RETENTION_MAX_BATCHES",
        ),
        old_route_orphans=_as_bool(getattr(settings, "chat_retention_old_route_orphans", False), False),
        backup_date=str(getattr(settings, "chat_retention_backup_date", "") or "").strip(),
    )


# ---------------------------------------------------------------------------
# The items
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Item:
    """One retention rule. `where` (table alias `t`) is the ONLY place the
    rule is written: the dry-run count and the batched delete are both built
    from it, so the two can never disagree about which rows qualify."""

    name: str  # the count key, the log word and the metric's `table` label
    table: str
    where: str
    params: Tuple[Any, ...]
    delete: bool = True  # False: counted and logged, never deleted (D8)
    needs_table: bool = False  # skip cleanly while the table does not exist
    old_route: bool = False  # D9: deleted only when its own setting is on
    #: The column holding the generation id the toggle-off guard matches an
    #: answer on (`messages.generation_id`), for the two tables the guard
    #: reads; "" for every other table. A batch of an item with `marks`
    #: stamps before it deletes (review DS2-1).
    marks: str = ""


def _orphan(alias: str = "t") -> str:
    """The row's chat is gone, and its key is not a bare-API key (C24)."""
    return (
        f"{alias}.conversation_id !~ '{BARE_API_KEY_PATTERN}'"
        f" AND NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id = {alias}.conversation_id)"
    )


def _sql_list(values: Tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def items(cfg: RetentionConfig) -> List[_Item]:
    """Every rule, in run order. The WHERE clauses are DISJOINT on purpose:
    an orphan old enough for the period rule belongs to the period rule, so
    each row is counted by exactly one item and a dry run's per-item counts
    are what the real run's items delete.

    The request items run BEFORE the trace items (review DS2-1). The guard
    reads a request only for a generation with no trace, so while the
    traces still stand each request batch stamps exactly the answers the
    guard reads through a request, and each trace batch then stamps the
    answers it reads through a trace. The other order would let a trace
    delete hand an answer to its request in the middle of a run, and the
    request batch would then stamp an answer the guard did not read as
    Salesforce before the run (an answer given in assistant mode to a
    request that asked for Salesforce, toggle-off review R12).

    For the same reason a trace waits while a request of its generation is
    still within its own period in a chat that still exists (review R1-1).
    That only happens when CHAT_REQUESTS_RETENTION_DAYS is set past the
    trace period; the trace then goes in the run that deletes its request.
    No request item deletes such a request, so a dry run counts what the
    real run deletes. An orphan trace needs no wait: its chat is gone, so
    no answer is left to read. A run whose request item does not finish
    holds the trace items back (`_run_items`)."""
    terminal = _sql_list(TERMINAL_STATUSES)
    resumable = _sql_list(RESUMABLE_STATUSES)
    older = "{col} < now() - make_interval(days => %s::int)"
    younger = "{col} >= now() - make_interval(days => %s::int)"
    traces_days = (cfg.query_traces_days,)
    requests_days = (cfg.chat_requests_days,)
    grace = (ORPHAN_GRACE_DAYS,)
    request_stands = (
        " AND NOT EXISTS (SELECT 1 FROM chat_requests r WHERE r.generation_id = t.trace_id"
        " AND " + younger.format(col="r.created_at")
        + " AND EXISTS (SELECT 1 FROM conversations c WHERE c.id = r.conversation_id))"
    )
    return [
        _Item(
            "chat_requests", "chat_requests",
            older.format(col="t.created_at") + f" AND t.status IN {terminal}",
            requests_days,
            marks="generation_id",
        ),
        _Item(
            "chat_requests_orphans", "chat_requests",
            older.format(col="t.created_at")
            + " AND " + younger.format(col="t.created_at")
            + f" AND t.status IN {terminal} AND " + _orphan(),
            grace + requests_days,
            marks="generation_id",
        ),
        _Item(
            "chat_requests_resumable_old", "chat_requests",
            older.format(col="t.created_at") + f" AND t.status IN {resumable}",
            requests_days,
            delete=False,
        ),
        _Item(
            "query_traces", "query_traces",
            older.format(col="t.started_at") + request_stands,
            traces_days + requests_days,
            marks="trace_id",
        ),
        _Item(
            "query_traces_orphans", "query_traces",
            older.format(col="t.started_at")
            + " AND " + younger.format(col="t.started_at")
            + " AND t.test_case_id IS NULL AND " + _orphan(),
            grace + traces_days,
            marks="trace_id",
        ),
        _Item(
            "sf_query_state_expired", "sf_query_state",
            "t.expires_at IS NOT NULL AND t.expires_at < now()",
            (),
            needs_table=True,
        ),
        _Item(
            "old_route_sf_conversation_state", "sf_conversation_state",
            older.format(col="t.updated_at") + " AND " + _orphan(),
            grace,
            old_route=True,
        ),
        _Item(
            "old_route_sf_intents", "sf_intents",
            older.format(col="t.updated_at") + " AND " + _orphan(),
            grace,
            old_route=True,
        ),
        _Item(
            "old_route_sf_clarifications", "sf_clarifications",
            older.format(col="COALESCE(t.resolved_at, t.created_at)") + " AND " + _orphan(),
            grace,
            old_route=True,
        ),
    ]


def count_sql(item: _Item) -> str:
    """The dry run's statement for `item`: the item's own WHERE."""
    return f"SELECT count(*) AS n FROM {item.table} t WHERE {item.where}"


def stamp_where(item: _Item, generations: str) -> str:
    """The answers (alias `m` on `messages`) that a delete of `item`'s rows
    would leave with no Salesforce mark: the ONE text both the dry-run count
    and the real stamp are built from. `generations` is a sub-select of the
    generation ids of the rows to delete.

    The match is the guard's own: `sf_isolation.sf_answer_sql`, the SQL form
    of the per-row rule `db.salesforce_chat_state` applies through
    `_server_salesforce` and `server_says_salesforce` (a trace by
    `trace_id = generation_id` decides; with no trace, the request by
    `generation_id`). So: an assistant row the guard reads as a Salesforce
    answer, whose `generation_id` is one of the rows'. The guard reads a
    request only when the generation has no trace, so a request stamps only
    then; while a trace stands, the trace is the mark and stamps when it
    goes. Never an answer whose meta has a mode (one stored as 'assistant'
    stays as it is), never one whose meta already says Salesforce (it needs
    no stamp). A meta that is SQL NULL or JSON null holds nothing, so the
    stamp makes it the one-key object (review R1-4); a meta that is an
    array, a string, a number or a boolean is never stamped, because the
    merge would not be additive (docs/retention.md lists it under "What it
    cannot reach").

    There is deliberately NO chat gate (review R2-3). `sf_answer_sql` matches
    on the generation alone, in any chat, as the guard's per-row reading
    does; the guard also reads a chat's rows only while a server row of
    THAT chat (or an answer's meta) says Salesforce, and this text does not
    copy that probe. So an answer stored in another chat under the
    generation of a deleted Salesforce row is stamped too, and hidden from
    then on, although the guard showed it before: the stamp fails closed.
    No writer is known to store one generation in two chats. A gate would
    keep a copy of db.py's probe here, which could drift from it, and would
    leave such a copy of a Salesforce answer readable in toggle-off turns."""
    if item.marks not in ("trace_id", "generation_id"):
        raise ValueError(f"{item.name} marks no answer")
    request_decides = (
        " AND NOT EXISTS (SELECT 1 FROM query_traces x WHERE x.trace_id = m.generation_id)"
        if item.table == "chat_requests"
        else ""
    )
    return (
        f"m.generation_id IN ({generations}){request_decides}"
        " AND (m.meta IS NULL OR jsonb_typeof(m.meta) IN ('object', 'null'))"
        " AND m.meta->>'mode' IS NULL"
        f" AND NOT {sf_meta_sql('m')}"
        f" AND {sf_answer_sql('m')}"
    )


def _rows_generations(item: _Item) -> str:
    """The generation ids of every row `item` would delete: its own WHERE."""
    return f"SELECT t.{item.marks} FROM {item.table} t WHERE {item.where}"


def stamp_count_sql(item: _Item) -> str:
    """The dry run's count of the answers a real run of `item` would stamp:
    the item's own WHERE, inside `stamp_where`."""
    return f"SELECT count(*) AS n FROM messages m WHERE {stamp_where(item, _rows_generations(item))}"


def delete_sql(item: _Item) -> str:
    """One batch of the real run for `item`: the SAME WHERE, by `ctid`, with
    LIMIT (the last parameter). A plain delete is read by `rowcount`, never
    RETURNING.

    For an item whose rows mark answers (`marks`), the batch is ONE statement
    and so one transaction: it fixes the batch's rows, stamps the answers
    they mark (`stamp_where`, review DS2-1), deletes those rows, and returns
    two counts. Every part reads the same snapshot, so the stamp sees the
    rows the delete removes. `RETURNING 1` only feeds those counts: no
    column of any row leaves the database."""
    if not item.marks:
        return (
            f"DELETE FROM {item.table} WHERE ctid IN ("
            f" SELECT t.ctid FROM {item.table} t WHERE {item.where} LIMIT %s)"
        )
    return (
        "WITH batch AS MATERIALIZED ("
        f" SELECT t.ctid AS row_ctid, t.{item.marks} AS generation_id FROM {item.table} t"
        f" WHERE {item.where} LIMIT %s),"
        " stamped AS ("
        " UPDATE messages m SET meta = (CASE WHEN jsonb_typeof(m.meta) = 'object' THEN m.meta"
        f" ELSE '{{}}'::jsonb END) || '{STAMP}'::jsonb"
        f" WHERE {stamp_where(item, 'SELECT b.generation_id FROM batch b')}"
        " RETURNING 1),"
        " deleted AS ("
        f" DELETE FROM {item.table} WHERE ctid IN (SELECT b.row_ctid FROM batch b)"
        " RETURNING 1)"
        " SELECT (SELECT count(*) FROM deleted) AS deleted, (SELECT count(*) FROM stamped) AS stamped"
    )


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

#: The once-a-day gate: (business date, dry run) of the last finished run in
#: this process. A run that hit a cap or an error does not set it.
_gate_lock = threading.Lock()
_done: Optional[Tuple[date, bool]] = None


def business_today() -> date:
    return datetime.now(BUSINESS_TZ).date()


def _open() -> psycopg.Connection:
    """The run's own connection: autocommit (each batch opens its own
    transaction), the pool's session options (UTC, statement timeout), and
    a name an operator can find in pg_stat_activity."""
    return psycopg.connect(
        db.dsn(),
        autocommit=True,
        row_factory=dict_row,
        options=db._server_options(),
        connect_timeout=10,
        application_name="techsara-retention",
    )


def _table_exists(con: psycopg.Connection, table: str) -> bool:
    row = con.execute("SELECT to_regclass(%s) IS NOT NULL AS present", (table,)).fetchone()
    return bool(row and row["present"])


def _count(con: psycopg.Connection, item: _Item) -> int:
    row = con.execute(count_sql(item), item.params).fetchone()
    return int(row["n"]) if row else 0


def _count_stamps(con: psycopg.Connection, item: _Item) -> int:
    """The dry run: how many answers a real run of `item` would stamp."""
    row = con.execute(stamp_count_sql(item), item.params).fetchone()
    return int(row["n"]) if row else 0


def _delete_batch(con: psycopg.Connection, statement: str, params: Tuple[Any, ...]) -> Tuple[int, int]:
    """One batch, one transaction. Returns (rows deleted, answers stamped):
    the stamping statement returns both as its one row; a plain delete
    returns its rowcount and stamps nothing."""
    with con.transaction():
        cursor = con.execute(statement, params)
        if cursor.description is not None:
            row = cursor.fetchone() or {}
            return max(0, int(row.get("deleted") or 0)), max(0, int(row.get("stamped") or 0))
        touched = cursor.rowcount
    return max(0, int(touched or 0)), 0


def _delete(con: psycopg.Connection, item: _Item, cfg: RetentionConfig) -> Tuple[int, int, bool]:
    """Delete `item`'s rows in batches. Returns (rows deleted, answers
    stamped, capped): capped is True when the run stopped at `max_batches`
    with full batches, so more rows may remain for the next tick."""
    statement = delete_sql(item)
    params = (*item.params, cfg.batch)
    total = 0
    stamped = 0
    for _ in range(cfg.max_batches):
        touched, marked = _delete_batch(con, statement, params)
        total += touched
        stamped += marked
        if touched < cfg.batch:
            return total, stamped, False
    return total, stamped, True


def _report_deleted(name: str, n: int) -> None:
    for _ in range(n):
        metrics.inc("retention_deleted_total", "chat retention: rows deleted", table=name)


def _report_stamped(name: str, n: int) -> None:
    for _ in range(n):
        metrics.inc(
            "retention_stamped_total",
            "chat retention: answers stamped mode=salesforce before the rows marking them went",
            table=name,
        )


def _report_candidates(name: str, n: int) -> None:
    metrics.set_gauge(
        "retention_candidates", n,
        "chat retention: rows that match a rule at the last run", table=name,
    )


def _backup_reminder(cfg: RetentionConfig, today: date) -> None:
    """D11: the pre-prune backup is deleted after 30 days. The orchestrator
    cannot see the host folder, so the operator records the backup's date in
    CHAT_RETENTION_BACKUP_DATE (docs/retention.md); while it is set and older
    than 30 days, each daily run logs a warning."""
    if not cfg.backup_date:
        return
    try:
        taken = date.fromisoformat(cfg.backup_date[:10])
    except ValueError:
        log.warning("retention: CHAT_RETENTION_BACKUP_DATE is not a YYYY-MM-DD date")
        return
    age = (today - taken).days
    if age > BACKUP_KEEP_DAYS:
        log.warning(
            "retention: the pre-prune backup of %s is %d days old (kept %d days, D11): "
            "delete ~/Documents/project/backups/retention_%s as docs/retention.md says, "
            "then clear CHAT_RETENTION_BACKUP_DATE",
            taken.isoformat(), age, BACKUP_KEEP_DAYS, taken.isoformat(),
        )


def _log_words(counts: Dict[str, Any], names: List[str]) -> str:
    return " ".join(f"{name}={counts[name]}" for name in names if name in counts)


def _run_items(con: psycopg.Connection, cfg: RetentionConfig) -> Dict[str, Any]:
    counts: Dict[str, Any] = {}
    #: Count-only items (D8) counted in a real run: logged as "kept", never
    #: under "deleted" (review F4).
    kept: List[str] = []
    skipped: List[str] = []
    capped = 0
    errors = 0
    #: Answers stamped (dry run: that a real run would stamp), over every
    #: item whose rows mark answers (review DS2-1).
    stamped = 0
    #: Set in a real run when a request item whose rows mark answers did
    #: not finish (it failed, or stopped at the cap). The trace items then
    #: wait for the next tick (review R1-1): a trace deleted while its
    #: request still stands would hand the guard's reading to the request,
    #: and the request's batch would later stamp an answer the server gave
    #: in assistant mode (toggle-off review R12). The failure or the cap
    #: already leaves the day open, so the next tick runs both again.
    requests_open = False
    waiting: List[str] = []
    #: Set in a dry run when an item whose rows mark answers failed (its
    #: count or its stamp count): `stamped` then leaves that item's answers
    #: out, so the run reports the total as unknown instead of a number
    #: that reads as complete (review R2-4).
    stamps_unknown = False
    for item in items(cfg):
        try:
            if item.needs_table and not _table_exists(con, item.table):
                skipped.append(item.name)
                continue
            if cfg.dry_run or not item.delete:
                n = _count(con, item)
                counts[item.name] = n
                _report_candidates(item.name, n)
                if not item.delete:
                    kept.append(item.name)
                elif item.marks:
                    stamped += _count_stamps(con, item)
                continue
            if item.old_route and not cfg.old_route_orphans:
                continue  # D9 off: the real run leaves the older route alone
            if requests_open and item.table == "query_traces":
                waiting.append(item.name)
                continue
            n, marked, hit_cap = _delete(con, item, cfg)
            counts[item.name] = n
            stamped += marked
            _report_deleted(item.name, n)
            _report_stamped(item.name, marked)
            if hit_cap:
                capped += 1
                requests_open = requests_open or (item.table == "chat_requests" and bool(item.marks))
                log.info(
                    "retention: %s reached the cap of %d batches of %d; the next tick continues",
                    item.name, cfg.max_batches, cfg.batch,
                )
        except Exception:  # noqa: BLE001 - one rule failing must not stop the others
            errors += 1
            requests_open = requests_open or (item.table == "chat_requests" and bool(item.marks))
            stamps_unknown = stamps_unknown or (cfg.dry_run and bool(item.marks))
            log.warning("retention: %s failed", item.name, exc_info=True)
    if waiting:
        log.info(
            "retention: held back %s: a chat_requests item did not finish, so the traces "
            "wait for the next tick",
            ",".join(waiting),
        )
    skipped_words = (" skipped=" + ",".join(skipped)) if skipped else ""
    if cfg.dry_run:
        if stamps_unknown:
            # No key and no gauge: like the failed item's own count, the
            # total is missing rather than wrong.
            words = " ".join(w for w in (_log_words(counts, list(counts)), f"{STAMPED}=unknown") if w)
        else:
            counts[STAMPED] = stamped
            _report_candidates(STAMPED, stamped)
            words = _log_words(counts, list(counts))
        log.info("retention dry-run: %s%s", words, skipped_words)
    else:
        # What was deleted, what was only counted and what was stamped go on
        # separate lines, so the "deleted" line never names a row still in
        # the table.
        deleted = [name for name in counts if name not in kept]
        log.info("retention: deleted %s%s", _log_words(counts, deleted), skipped_words)
        if kept:
            log.info("retention: counted, kept %s", _log_words(counts, kept))
        counts[STAMPED] = stamped
        if stamped:
            log.info(
                "retention: stamped %s=%d (meta.mode=salesforce on answers whose only "
                "Salesforce mark was a deleted row)",
                STAMPED, stamped,
            )
    kept_open = int(counts.get("chat_requests_resumable_old", 0) or 0)
    if kept_open:
        log.warning(
            "retention: %d chat_requests in a resumable status are older than %d days and are "
            "kept (D8: deleting them needs the owner's word)",
            kept_open, cfg.chat_requests_days,
        )
    return {
        "status": "dry_run" if cfg.dry_run else "deleted",
        **counts,
        "capped": capped,
        "errors": errors,
    }


def run_chat_retention(*, force: bool = False) -> Dict[str, Any]:
    """One retention pass; blocking (the maintenance loop runs it in a
    thread, see `sweep`). Never raises.

    Returns a dict: `status` is one word, every other value a count.
      * status "disabled": CHAT_RETENTION_ENABLED is off; the database is not
        touched.
      * status "not_due": this process already finished today's run.
      * status "locked": another runner holds the lock; nothing done.
      * status "failed": the run could not start (connection, lock read).
      * status "dry_run": one count per item (`<item>`), plus
        `messages_stamped` (the answers a real run would stamp), nothing
        deleted or stamped. When an item whose rows mark answers failed,
        `messages_stamped` has no key (its log word reads `unknown`): the
        total would leave that item's answers out (review R2-4).
      * status "deleted": rows deleted per item, plus
        `chat_requests_resumable_old` (counted, never deleted) and
        `messages_stamped` (the answers stamped before their rows went).
      In the last two, `capped` is the number of items that stopped at the
      batch cap and `errors` the number that failed; either leaves the day
      open, so the next tick runs again. An item whose table does not exist
      yet (`sf_query_state` before V45), an older-route item while D9 is
      off in a real run, or a trace item held back because a request item
      did not finish in this real run, has no key.

    `force` skips the once-a-day gate only (tests, an operator's manual run);
    the enabled flag, the dry-run flag and the lock still hold.
    """
    global _done
    try:
        # The two cheap reads first: a disabled or already-done tick (every
        # 30 minutes) neither reads the other settings nor logs about them.
        if not _as_bool(getattr(settings, "chat_retention_enabled", False), False):
            return {"status": "disabled"}
        dry_run = _as_bool(getattr(settings, "chat_retention_dry_run", True), True)
        today = business_today()
        with _gate_lock:
            if not force and _done == (today, dry_run):
                return {"status": "not_due"}
        cfg = load_config()
        _backup_reminder(cfg, today)
        try:
            con = _open()
        except Exception:  # noqa: BLE001
            log.warning("retention: could not open its connection", exc_info=True)
            metrics.inc("retention_runs_total", "chat retention: runs", result="fail")
            return {"status": "failed"}
        try:
            got = con.execute("SELECT pg_try_advisory_lock(%s) AS got", (_LOCK_KEY,)).fetchone()
            if not (got and got["got"]):
                log.info("retention: another runner holds the lock; skipping this tick")
                return {"status": "locked"}
            try:
                result = _run_items(con, cfg)
            finally:
                try:
                    con.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
                except Exception:  # noqa: BLE001 - closing the session releases it anyway
                    log.warning("retention: unlock failed; closing the session releases it")
        finally:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass
        ok = not result.get("errors") and not result.get("capped")
        metrics.inc(
            "retention_runs_total", "chat retention: runs",
            result="ok" if not result.get("errors") else "fail",
        )
        if ok:
            with _gate_lock:
                _done = (today, cfg.dry_run)
        return result
    except Exception:  # noqa: BLE001 - the loop must never lose its sweep
        log.warning("retention: run failed", exc_info=True)
        metrics.inc("retention_runs_total", "chat retention: runs", result="fail")
        return {"status": "failed"}


async def sweep() -> Optional[Dict[str, Any]]:
    """The maintenance loop's entry (main.py `_API_PLATFORM_SWEEPS` names a
    main-module wrapper that awaits this; stage 2 wires it). Runs the pass in
    a worker thread and never raises, cancellation apart."""
    try:
        return await db.run_in_thread(run_chat_retention)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.warning("retention: sweep failed", exc_info=True)
        return None


def _reset_gate_for_tests() -> None:
    """Forget the once-a-day gate (tests only)."""
    global _done
    with _gate_lock:
        _done = None
