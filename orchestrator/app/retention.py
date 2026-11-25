"""Chat retention: the owner-approved deletion periods, enforced once a day
(Salesforce memory design v2 §12.1-§12.2, 2026-11-25; docs/retention.md).

WHAT IT DELETES. Only what the owner approved, and nothing younger:
  * `query_traces` started more than QUERY_TRACES_RETENTION_DAYS ago (180);
    their `query_trace_events` go by the foreign key's cascade, the only FK
    onto that table;
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
text the delete uses (one `_Item.where` feeds both statements), logs the
counts only, sets the `retention_candidates{table}` gauge and deletes
nothing. A real run counts `retention_deleted_total{table}`. Logs carry
counts, never content.

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
    are what the real run's items delete."""
    terminal = _sql_list(TERMINAL_STATUSES)
    resumable = _sql_list(RESUMABLE_STATUSES)
    older = "{col} < now() - make_interval(days => %s::int)"
    younger = "{col} >= now() - make_interval(days => %s::int)"
    traces_days = (cfg.query_traces_days,)
    requests_days = (cfg.chat_requests_days,)
    grace = (ORPHAN_GRACE_DAYS,)
    return [
        _Item(
            "query_traces", "query_traces",
            older.format(col="t.started_at"),
            traces_days,
        ),
        _Item(
            "query_traces_orphans", "query_traces",
            older.format(col="t.started_at")
            + " AND " + younger.format(col="t.started_at")
            + " AND t.test_case_id IS NULL AND " + _orphan(),
            grace + traces_days,
        ),
        _Item(
            "chat_requests", "chat_requests",
            older.format(col="t.created_at") + f" AND t.status IN {terminal}",
            requests_days,
        ),
        _Item(
            "chat_requests_orphans", "chat_requests",
            older.format(col="t.created_at")
            + " AND " + younger.format(col="t.created_at")
            + f" AND t.status IN {terminal} AND " + _orphan(),
            grace + requests_days,
        ),
        _Item(
            "chat_requests_resumable_old", "chat_requests",
            older.format(col="t.created_at") + f" AND t.status IN {resumable}",
            requests_days,
            delete=False,
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


def delete_sql(item: _Item) -> str:
    """One batch of the real run for `item`: the SAME WHERE, by `ctid`, with
    LIMIT (the last parameter). `rowcount`, never RETURNING."""
    return (
        f"DELETE FROM {item.table} WHERE ctid IN ("
        f" SELECT t.ctid FROM {item.table} t WHERE {item.where} LIMIT %s)"
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


def _delete_batch(con: psycopg.Connection, statement: str, params: Tuple[Any, ...]) -> int:
    """One batch, one transaction."""
    with con.transaction():
        touched = con.execute(statement, params).rowcount
    return max(0, int(touched or 0))


def _delete(con: psycopg.Connection, item: _Item, cfg: RetentionConfig) -> Tuple[int, bool]:
    """Delete `item`'s rows in batches. Returns (rows deleted, capped): capped
    is True when the run stopped at `max_batches` with full batches, so more
    rows may remain for the next tick."""
    statement = delete_sql(item)
    params = (*item.params, cfg.batch)
    total = 0
    for _ in range(cfg.max_batches):
        touched = _delete_batch(con, statement, params)
        total += touched
        if touched < cfg.batch:
            return total, False
    return total, True


def _report_deleted(name: str, n: int) -> None:
    for _ in range(n):
        metrics.inc("retention_deleted_total", "chat retention: rows deleted", table=name)


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
                continue
            if item.old_route and not cfg.old_route_orphans:
                continue  # D9 off: the real run leaves the older route alone
            n, hit_cap = _delete(con, item, cfg)
            counts[item.name] = n
            _report_deleted(item.name, n)
            if hit_cap:
                capped += 1
                log.info(
                    "retention: %s reached the cap of %d batches of %d; the next tick continues",
                    item.name, cfg.max_batches, cfg.batch,
                )
        except Exception:  # noqa: BLE001 - one rule failing must not stop the others
            errors += 1
            log.warning("retention: %s failed", item.name, exc_info=True)
    skipped_words = (" skipped=" + ",".join(skipped)) if skipped else ""
    if cfg.dry_run:
        log.info("retention dry-run: %s%s", _log_words(counts, list(counts)), skipped_words)
    else:
        # What was deleted and what was only counted go on separate lines,
        # so the "deleted" line never names a row still in the table.
        deleted = [name for name in counts if name not in kept]
        log.info("retention: deleted %s%s", _log_words(counts, deleted), skipped_words)
        if kept:
            log.info("retention: counted, kept %s", _log_words(counts, kept))
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
      * status "dry_run": one count per item (`<item>`), nothing deleted.
      * status "deleted": rows deleted per item, plus
        `chat_requests_resumable_old` (counted, never deleted).
      In the last two, `capped` is the number of items that stopped at the
      batch cap and `errors` the number that failed; either leaves the day
      open, so the next tick runs again. An item whose table does not exist
      yet (`sf_query_state` before V45), or an older-route item while D9 is
      off in a real run, has no key.

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
