"""Object access counts, and the promotion decision they drive."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from .database import Database


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UsageTracker:
    def __init__(self, database: Database) -> None:
        self.db = database
        self._lock = threading.Lock()

    @property
    def _c(self):
        return self.db.connection

    def record_access(self, api_name: str, window_hours: int = 24) -> None:
        """Count one access, rolling the window when it has expired.

        The rolling counter is what auto-promotion reads, so it must reset
        rather than accumulate forever -- otherwise an object busy last month
        stays hot for good and the cache never reflects what is used now.
        """
        now = _now()
        with self._lock:
            row = self._c.execute(
                "SELECT access_count, access_count_window, window_started_at"
                " FROM object_usage_stats WHERE object_api_name=?",
                (api_name,)).fetchone()
            if row is None:
                self._c.execute(
                    "INSERT INTO object_usage_stats (object_api_name,"
                    " access_count, access_count_window, window_started_at,"
                    " last_accessed_at) VALUES (?,1,1,?,?)",
                    (api_name, now.isoformat(), now.isoformat()))
                return
            started = row["window_started_at"]
            expired = True
            if started:
                try:
                    expired = (now - datetime.fromisoformat(started)
                               ) > timedelta(hours=window_hours)
                except ValueError:
                    expired = True
            window_count = 1 if expired else int(row["access_count_window"] or 0) + 1
            window_start = now.isoformat() if expired else started
            self._c.execute(
                "UPDATE object_usage_stats SET access_count=access_count+1,"
                " access_count_window=?, window_started_at=?, last_accessed_at=?"
                " WHERE object_api_name=?",
                (window_count, window_start, now.isoformat(), api_name))

    def set_pinned(self, names: list[str]) -> None:
        """Pin exactly these; unpin everything else.

        Removing a name from YAML must actually unpin it, so this is a
        replacement rather than an addition.
        """
        with self._lock:
            self._c.execute("UPDATE object_usage_stats SET is_pinned=0")
            for name in names:
                self._c.execute(
                    "INSERT INTO object_usage_stats (object_api_name, is_pinned)"
                    " VALUES (?,1) ON CONFLICT(object_api_name)"
                    " DO UPDATE SET is_pinned=1", (name,))

    def promotion_candidates(self, *, min_access_count: int,
                             max_hot_objects: int) -> list[str]:
        pinned = self._c.execute(
            "SELECT count(*) FROM object_usage_stats WHERE is_pinned=1"
        ).fetchone()[0]
        room = max(0, max_hot_objects - int(pinned))
        if room == 0:
            return []
        return [r[0] for r in self._c.execute(
            "SELECT object_api_name FROM object_usage_stats"
            " WHERE is_pinned=0 AND access_count_window >= ?"
            " ORDER BY access_count_window DESC, last_accessed_at DESC LIMIT ?",
            (min_access_count, room))]

    def demotion_candidates(self, *, min_access_count: int) -> list[str]:
        """Auto-hot objects that have fallen below the bar. Never pinned ones."""
        return [r[0] for r in self._c.execute(
            "SELECT object_api_name FROM object_usage_stats"
            " WHERE is_pinned=0 AND is_auto_hot=1 AND access_count_window < ?",
            (min_access_count,))]

    def mark_auto_hot(self, names: list[str], value: bool) -> None:
        with self._lock:
            for name in names:
                self._c.execute(
                    "INSERT INTO object_usage_stats (object_api_name, is_auto_hot)"
                    " VALUES (?,?) ON CONFLICT(object_api_name)"
                    " DO UPDATE SET is_auto_hot=?",
                    (name, int(value), int(value)))

    def stats(self, api_name: str) -> dict[str, Any] | None:
        row = self._c.execute(
            "SELECT * FROM object_usage_stats WHERE object_api_name=?",
            (api_name,)).fetchone()
        return dict(row) if row else None

    def top(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(r) for r in self._c.execute(
            "SELECT * FROM object_usage_stats ORDER BY access_count_window DESC,"
            " access_count DESC LIMIT ?", (limit,))]
