"""How old the replicated records are.

DuckDB is a copy of Salesforce, refreshed on a cycle. An answer from it is
true as of the last sync, and a user deserves to know which. Age is read from
whatever sync state the warehouse carries; when it carries none, the answer is
"unknown" rather than a guess -- claiming freshness nobody measured is worse
than admitting the gap.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .config import FreshnessSettings
from .models import Freshness

log = logging.getLogger(__name__)

# Sync bookkeeping this warehouse might carry, newest convention first.
# Each entry is (schema, table, expression); a schema of None means the schema
# the query itself runs against.
#
# raw._sync_meta is what the current warehouse actually writes: one row per
# object, 993 of them, with the wall-clock time that object last landed. It is
# in `raw` rather than `main`, which is why the configured schema alone is not
# enough to find it.
CANDIDATE_SOURCES = (
    (None, "sync_runs", "MAX(completed_at)"),
    (None, "object_sync_state", "MAX(last_successful_sync_at)"),
    (None, "_sync_runs", "MAX(completed_at)"),
    ("raw", "_sync_meta", "MAX(updated_at)"),
    (None, "_sync_meta", "MAX(updated_at)"),
)


def _parse(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class FreshnessTracker:
    def __init__(self, settings: FreshnessSettings) -> None:
        self.settings = settings

    def _last_sync(self, connection: Any, schema: str) -> datetime | None:
        for source_schema, table, expression in CANDIDATE_SOURCES:
            target = source_schema or schema
            try:
                row = connection.execute(
                    f'SELECT {expression} FROM "{target}"."{table}"').fetchone()
            except Exception:
                continue                       # table absent: try the next one
            parsed = _parse(row[0] if row else None)
            if parsed:
                return parsed
        # Fall back to the file's own mtime: the sync rewrites it, so its age
        # is a real lower bound on staleness even without bookkeeping tables.
        return None

    def check(self, connection: Any, schema: str,
              database_path: str | None = None) -> Freshness:
        last = self._last_sync(connection, schema)
        if last is None and database_path:
            try:
                from pathlib import Path
                stamp = Path(database_path).stat().st_mtime
                last = datetime.fromtimestamp(stamp, tz=timezone.utc)
            except OSError:
                last = None
        if last is None:
            return Freshness(status="unknown",
                             expected_sync_minutes=self.settings.expected_sync_minutes)

        age = (datetime.now(timezone.utc) - last).total_seconds() / 60.0
        if age >= self.settings.reject_after_minutes:
            status = "very_stale"
        elif age >= self.settings.warning_after_minutes:
            status = "stale"
        else:
            status = "fresh"
        return Freshness(last_sync_at=last.isoformat(), age_minutes=round(age, 1),
                         status=status,
                         expected_sync_minutes=self.settings.expected_sync_minutes)

    def should_reject(self, freshness: Freshness) -> bool:
        return (self.settings.reject_when_stale
                and freshness.status == "very_stale")
