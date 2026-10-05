"""Questions about the system's own state: sync time, freshness (§33).

"When was Interview__c last synchronised?" is answered from the sync worker's
own bookkeeping -- raw._sync_meta, one row per object -- never by searching
Interview__c for a field called "last synchronized". What the warehouse does
not record (sync failures, API usage) is reported as unavailable, not guessed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..results import TypedResult


class OperationalEngine:
    def __init__(self, records: Any, retriever: Any) -> None:
        self.records, self.retriever = records, retriever

    def run(self, objects: list[str]) -> TypedResult:
        """`objects`: the API names discovery (the main model) chose."""
        connection = self.records.executor.connection
        rows: list[dict[str, Any]] = []
        values: dict[str, Any] = {}
        try:
            count, newest, oldest = connection.execute(
                "SELECT count(*), max(updated_at), min(updated_at) FROM raw._sync_meta"
            ).fetchone()
        except Exception as exc:                        # noqa: BLE001
            return TypedResult(kind="unsupported", source="operational",
                               values={"unavailable": "sync bookkeeping (raw._sync_meta)"},
                               error=None, error_detail=str(exc)[:200])
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        values.update({"objects_tracked": count,
                       "latest_sync_utc": newest.isoformat(sep=" ", timespec="seconds") if newest else None,
                       "oldest_sync_utc": oldest.isoformat(sep=" ", timespec="seconds") if oldest else None,
                       "minutes_since_latest_sync": round((now - newest).total_seconds() / 60, 1)
                       if newest else None,
                       "not_recorded": "sync failures, API usage and batch history are "
                                       "not stored in the warehouse",
                       # Stated as a fact so no answer can read "no failure
                       # recorded" as "did not fail".
                       "whether_any_sync_failed": "unknown: failures are not recorded"})
        for name in objects:
            row = connection.execute(
                "SELECT object_name, watermark, updated_at FROM raw._sync_meta"
                " WHERE lower(object_name) = lower(?)", [name]).fetchone()
            if row is None:
                rows.append({"object": name, "finding": "not tracked by the sync"})
                continue
            rows.append({"object": row[0],
                         "last_synchronised_utc": row[2].isoformat(sep=" ", timespec="seconds"),
                         "minutes_ago": round((now - row[2]).total_seconds() / 60, 1),
                         "salesforce_watermark": row[1]})
        return TypedResult(kind="operational_facts", source="operational", rows=rows,
                           values=values, returned_count=len(rows), total_count=len(rows))
