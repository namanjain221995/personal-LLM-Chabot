"""Shared data types of okrec. Build A owns this file; Build B relies on it. No duckdb import."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sf_assistant.config import B_DIR

#: Tools whose results are record data (counts, rows, values, history, sync times).
DATA_TOOLS: frozenset[str] = frozenset({
    "get_field_values", "count_records", "aggregate_records", "group_records", "trend_records",
    "compare_records", "percentage_of_records", "list_records", "search_records", "find_references",
    "about_people", "get_field_history", "get_sync_status",
})
#: The person tool and its alias (RECORDS_SPEC §22.5): who named people are, where they are referenced, how they are
#: linked. Calls of the alias ``find_references`` are logged as ``about_people``; code that reads their results checks
#: ``tool in PERSON_TOOLS``.
PERSON_TOOLS: frozenset[str] = frozenset({"about_people", "find_references"})
#: Tools that help pick objects, fields and values (they still carry counts, so they are logged too).
ORIENTATION_TOOLS: frozenset[str] = frozenset({"find_objects", "describe_fields"})

_B_DIR = B_DIR   # sf_assistant/.private (was notebooks/object_knowledge_eval): the snapshot shows as okeval_run/...


def display_path(path: str) -> str:
    """``path`` relative to the object_knowledge_eval folder when it is inside it, else as given."""
    try:
        return str(Path(path).resolve().relative_to(_B_DIR))
    except (ValueError, OSError):
        return str(path)


class RecordsUnavailable(Exception):
    """Records cannot be used in this session; ``str(exc)`` is the reason shown to the user."""


@dataclass
class Statement:
    """One SQL statement run by the record tools (or reused from the value cache)."""

    sql: str
    params: list[Any]               # JSON-safe copies of the bound parameters, in order
    columns: list[str]
    rows: list[list[Any]]           # JSON-safe rows actually fetched (<= the fetch cap)
    row_count: int                  # len(rows)
    truncated: bool                 # more rows existed than were fetched, or the byte cap was hit
    elapsed_ms: float
    cached: bool = False            # True when served from the value cache built at open
    reused: bool = False            # True when it ran earlier in this question (per-question name and scan cache)


@dataclass
class QueryEvidence:
    """One record tool call: what the model asked, what code ran, what came back."""

    query_id: str                   # "q1", "q2", ... reset by RecordTools.begin_question()
    tool: str
    kind: str                       # "data" or "orientation"
    object: str | None              # resolved base object API name (None when unresolved)
    arguments: dict[str, Any]       # as received from the model
    ok: bool
    error: str | None               # the error text the model saw (ok=False)
    applied: dict[str, Any]         # the same "applied" block the model saw ({} when it failed early)
    notes: list[str]
    statements: list[Statement]     # main statement first; empty when it failed before SQL
    tables: list[str]               # objects read, base object first
    synced_at: dict[str, str | None]  # object -> "YYYY-MM-DD HH:MM UTC" (raw._sync_meta.updated_at)
    summary: str                    # one line: "84", "9 groups", "5 of 42 rows", "6 buckets", "error"
    model_result: dict[str, Any] | None  # the result dict the model saw (ok=True)
    elapsed_ms: float
    error_code: str | None = None   # code of the RecordError/PeriodError of a failed call ("E4h", "E2b", ...); None when ok
    fields_used: list[tuple[str, str]] = field(default_factory=list)  # (object, field) of every filter, path hop,
                                    # group_by, split_by, measure and date field the call used (call.fields_used)
    not_found: list[dict[str, Any]] = field(default_factory=list)  # names a failed call (E4h, E15a) found no record for:
                                    # {"name", "target" (label text), "object", "field", "match": exact|contains,
                                    # "close": the stored names the error lists instead (first name, contains, spelling)}
    names_matched: list[dict[str, Any]] = field(default_factory=list)  # positive name filters of a successful call
                                    # that matched stored records: {"filter", "names" (distinct stored names),
                                    # "match": exact|contains}

    # The spelling help (docs/resolver_integration.md (3)): two attributes that are not dataclass fields, so the
    # evidence of a run with SFA_RESOLVER off is field for field what it was (the parity replays compare the fields).
    @property
    def read_as(self) -> list[dict[str, Any]]:
        """The arguments this call read as their closest names: ``{"arg", "typed", "used", "kind", "target", "how"}``
        (``how``: "closest object name", "closest field name", "closest stored value", "closest picklist value");
        [] without any (always with the switch off). Failed calls keep theirs too."""
        return self.__dict__.setdefault("_read_as", [])

    @read_as.setter
    def read_as(self, value: list[dict[str, Any]]) -> None:
        self.__dict__["_read_as"] = list(value)

    @property
    def did_you_mean(self) -> list[dict[str, Any]]:
        """The did-you-means of a failed call (``RecordError.did_you_mean``): ``{"typed", "kind", "suggestions"}``;
        [] for a successful call and with the switch off. The error code and text are the usual ones."""
        return self.__dict__.setdefault("_did_you_mean", [])

    @did_you_mean.setter
    def did_you_mean(self, value: list[dict[str, Any]]) -> None:
        self.__dict__["_did_you_mean"] = list(value)

    # The business-term contracts (2026-11-24, tools/term_enforce.py): not a dataclass field either, for the same
    # reason; ``arguments`` stays what the model sent (traces, timeline).
    @property
    def enforced_arguments(self) -> dict[str, Any] | None:
        """The arguments the handler ran with when code changed them (an added candidate filter, a completed support
        call); None when it ran with ``arguments``. Code that reads what ran (charts, the nudges) reads
        ``enforced_arguments or arguments``."""
        return self.__dict__.get("_enforced_arguments")

    @enforced_arguments.setter
    def enforced_arguments(self, value: dict[str, Any] | None) -> None:
        self.__dict__["_enforced_arguments"] = value

    # Long lists (development A of 2026-11-27): not a dataclass field either, for the same reason.
    @property
    def full_rows(self) -> list[dict[str, Any]] | None:
        """Every row of a list_records result whose rows the result cap cut (``model_result`` holds the rows the
        model read, ``shown`` of ``returned``): the same cells, columns and privacy rules, before the cut. None when
        nothing was cut and for every other tool. Code writes a list question's rows from it (``agent.tables``)."""
        return self.__dict__.get("_full_rows")

    @full_rows.setter
    def full_rows(self, value: list[dict[str, Any]] | None) -> None:
        self.__dict__["_full_rows"] = list(value) if value is not None else None


@dataclass
class RecordsStatus:
    """Where the record data comes from and how fresh it is."""

    snapshot_path: str              # shown relative to B when inside it
    source: str                     # "sf-local-ai-orchestrator-1:/data/warehouse.read.duckdb"
    copied_at_utc: str | None       # "YYYY-MM-DD HH:MM UTC"
    copied_at_from: str             # "meta" (.meta.json) or "file time"
    org_is_sandbox: bool | None
    origin: str                     # "preprod SANDBOX copy" | "PRODUCTION copy" | "copy of unknown origin"
    newest_sync_utc: str | None     # max(raw._sync_meta.updated_at), "YYYY-MM-DD HH:MM UTC"
    age_hours: float | None         # now (UTC) - newest sync
    stale: bool                     # age_hours > RecordsConfig.max_age_h
    time_zone: str
    time_zone_from: str             # "OKEVAL_RECORDS_TZ" | "Organization.TimeZoneSidKey" | "default UTC"
    today: str                      # ISO date in time_zone (pinned or real)
    today_pinned: bool
    objects: int                    # queryable objects
    duckdb_version: str

    def line(self) -> str:
        """Startup line, e.g. 'records: okeval_run/warehouse.duckdb | preprod SANDBOX copy | copied
        2026-10-13 14:56 UTC (file time) | newest sync 2026-10-13 14:56 UTC (6.2 h ago) | today
        2026-10-13 (America/New_York) | 60 objects' (one line; '(file time)' only when copied_at_from
        is 'file time'; 'today ... pinned' when today_pinned)."""
        copied = self.copied_at_utc or "unknown"
        if self.copied_at_from == "file time":
            copied += " (file time)"
        newest = self.newest_sync_utc or "unknown"
        if self.age_hours is not None:
            newest += f" ({self.age_hours:.1f} h ago)"
        today = f"{self.today} pinned" if self.today_pinned else self.today
        return (f"records: {self.snapshot_path} | {self.origin} | copied {copied} | newest sync {newest} | "
                f"today {today} ({self.time_zone}) | {self.objects} objects")

    def warning(self) -> str | None:
        """'WARNING records snapshot is 31.0 h old (newest sync ...); run ./ask.sh --refresh-warehouse'
        when stale, else None. Also warns when org_is_sandbox is False: 'WARNING this snapshot is a
        PRODUCTION copy (Organization.IsSandbox = false)'."""
        out: list[str] = []
        if self.stale:
            age = f"{self.age_hours:.1f}" if self.age_hours is not None else "?"
            out.append(f"WARNING records snapshot is {age} h old (newest sync {self.newest_sync_utc or 'unknown'}); "
                       "run ./ask.sh --refresh-warehouse")
        if self.org_is_sandbox is False:
            out.append("WARNING this snapshot is a PRODUCTION copy (Organization.IsSandbox = false)")
        return "\n".join(out) or None


@dataclass
class SnapshotInfo:
    """Result of a refresh."""

    path: str
    copied_at_utc: str
    bytes: int
    newest_sync_utc: str | None
    org_is_sandbox: bool | None
    duckdb_version: str

    def line(self) -> str:
        """'snapshot refreshed: okeval_run/warehouse.duckdb (428.6 MB), copied 2026-10-14 09:01 UTC,
        newest sync 2026-10-14 08:57 UTC, preprod SANDBOX'."""
        origin = {True: "preprod SANDBOX", False: "PRODUCTION"}.get(self.org_is_sandbox, "unknown origin")  # type: ignore[arg-type]
        return (f"snapshot refreshed: {display_path(self.path)} ({self.bytes / 1e6:.1f} MB), copied {self.copied_at_utc}, "
                f"newest sync {self.newest_sync_utc or 'unknown'}, {origin}")
