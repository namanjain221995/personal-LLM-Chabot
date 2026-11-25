"""In-process conversation memory: a session dict, trimmed to a max turn count.

Phase 1 keeps memory in-process on purpose (single orchestrator container);
restarting the service clears all sessions.

Each exchange remembers the mode it was asked in (toggle-off isolation,
2026-11-25). A client that sends no turns and no conversation id reads the
account-wide key `u<user>-default`, which can hold a Salesforce-mode exchange
from another request; a toggle-off turn asks for the history without it, and a
Salesforce-mode follow-up still sees everything.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .config import settings


class SessionMemory:
    def __init__(self, max_turns: int | None = None) -> None:
        self._sessions: Dict[str, List[dict]] = {}
        self.max_turns = max_turns or settings.session_max_turns

    def history(self, session_id: str, *, exclude_salesforce: bool = False) -> List[dict]:
        """Chat history as a list of {"role", "content"} dicts (copy).

        `exclude_salesforce=True` drops every exchange asked in Salesforce
        mode and keeps the order of the rest."""
        return [
            {"role": m["role"], "content": m["content"]}
            for m in self._sessions.get(session_id, [])
            if not (exclude_salesforce and m.get("mode") == "salesforce")
        ]

    def add_exchange(
        self, session_id: str, user_text: str, assistant_text: str, *, mode: Optional[str] = None
    ) -> None:
        msgs = self._sessions.setdefault(session_id, [])
        msgs.append({"role": "user", "content": user_text, "mode": mode})
        msgs.append({"role": "assistant", "content": assistant_text, "mode": mode})
        # Keep the last `max_turns` exchanges (2 messages per turn).
        overflow = len(msgs) - self.max_turns * 2
        if overflow > 0:
            del msgs[:overflow]

    def clear(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


memory = SessionMemory()
