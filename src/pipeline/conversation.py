"""Conversation state: what a follow-up question inherits (§37).

One row per conversation id in SQLite: the last compiled IR and the last
result's shape. A follow-up ("only the ones from last month", "and for
recruiters?") is compiled with the previous IR in view, and the main model
restates the complete meaning -- what carries over is its decision (§30), not
a merge rule here. Nothing here is guessed from text.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .ir import SemanticIR, ir_from_dict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_state (
    conversation_id TEXT PRIMARY KEY,
    ir_json TEXT NOT NULL,
    result_json TEXT,
    updated_at REAL NOT NULL
)"""


class ConversationStore:
    def __init__(self, path: str | Path, ttl_seconds: int = 6 * 3600) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        # One short-lived connection per call: no descriptor outlives it.
        return sqlite3.connect(self.path, timeout=5)

    def load(self, conversation_id: str | None) -> tuple[SemanticIR | None, dict | None]:
        if not conversation_id:
            return None, None
        with self._lock, self._connect() as db:
            row = db.execute("SELECT ir_json, result_json, updated_at FROM conversation_state"
                             " WHERE conversation_id = ?", (conversation_id,)).fetchone()
        if not row or time.time() - row[2] > self.ttl:
            return None, None
        return ir_from_dict(json.loads(row[0])), json.loads(row[1] or "null")

    def save(self, conversation_id: str | None, ir: SemanticIR,
             result: dict | None = None) -> None:
        if not conversation_id:
            return
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO conversation_state VALUES (?, ?, ?, ?)",
                       (conversation_id, json.dumps(ir.as_dict(), default=str),
                        json.dumps(result, default=str) if result else None, time.time()))

    def close(self) -> None:
        pass


def previous_summary(ir: SemanticIR | None) -> dict[str, Any] | None:
    """What the compiler is shown of the previous turn: the IR, no rows."""
    if ir is None:
        return None
    data = ir.as_dict()
    for key in ("model", "endpoint", "mode", "duration_ms", "completion_tokens",
                "retried", "capabilities", "sources"):
        data.pop(key, None)
    return data
