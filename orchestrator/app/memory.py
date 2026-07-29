"""In-process conversation memory: a session dict, trimmed to a max turn count.

Phase 1 keeps memory in-process on purpose (single orchestrator container);
restarting the service clears all sessions.
"""
from __future__ import annotations

from typing import Dict, List

from .config import settings


class SessionMemory:
    def __init__(self, max_turns: int | None = None) -> None:
        self._sessions: Dict[str, List[dict]] = {}
        self.max_turns = max_turns or settings.session_max_turns

    def history(self, session_id: str) -> List[dict]:
        """Chat history as a list of {"role", "content"} dicts (copy)."""
        return list(self._sessions.get(session_id, []))

    def add_exchange(self, session_id: str, user_text: str, assistant_text: str) -> None:
        msgs = self._sessions.setdefault(session_id, [])
        msgs.append({"role": "user", "content": user_text})
        msgs.append({"role": "assistant", "content": assistant_text})
        # Keep the last `max_turns` exchanges (2 messages per turn).
        overflow = len(msgs) - self.max_turns * 2
        if overflow > 0:
            del msgs[:overflow]

    def clear(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


memory = SessionMemory()
