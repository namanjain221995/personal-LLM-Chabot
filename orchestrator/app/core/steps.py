"""The step timeline a person watches while a long turn works.

One `emit("step", …)` frame per transition, ids counted from 1, and an open
step ALWAYS closed — deep_research.py leaves its open step in a variable for
exactly this reason, because a phase that raises otherwise leaves a spinner
running in the UI forever. frontend/lib/sse.ts `mergeStep` updates a row in
place by id, so a running step may be re-emitted with a fresh `detail` to
report progress ("2 of 3 drafts done") without opening a new row.

Lived in core/max_loop.py as `_Steps` until 2026-09-28, when the best-of-N
branch of engines/chat.py needed the same timeline: a Max turn there showed
nothing for 11.2–17.4 s (n=5, load 3.9–5.2) because three drafts and a judge
ran with no frame on the wire until the winner was replayed.
"""
from __future__ import annotations

from typing import Awaitable, Callable, List, Optional

Emit = Callable[[str, dict], Awaitable[None]]

#: The UI shows this many characters of a step's detail (V2 §4e).
DETAIL_CHARS = 200


class Steps:
    def __init__(self, emit: Emit) -> None:
        self._emit = emit
        self._next = 0
        self._open: Optional[tuple] = None
        self.finished: List[dict] = []

    async def open(self, title: str, detail: str = "") -> int:
        await self.close_open("interrupted")
        self._next += 1
        self._open = (self._next, title)
        frame = {"id": self._next, "title": title, "status": "running"}
        if detail:
            frame["detail"] = detail[:DETAIL_CHARS]
        await self._emit("step", frame)
        return self._next

    async def progress(self, detail: str) -> None:
        """Re-emit the OPEN step, still running, with a new detail line.
        Nothing is emitted when no step is open."""
        if self._open is None:
            return
        step_id, title = self._open
        await self._emit(
            "step",
            {"id": step_id, "title": title, "status": "running", "detail": detail[:DETAIL_CHARS]},
        )

    async def done(self, detail: str = "") -> None:
        await self._close("done", detail)

    async def failed(self, detail: str = "") -> None:
        await self._close("failed", detail)

    async def close_open(self, detail: str = "") -> None:
        if self._open is not None:
            await self._close("failed", detail)

    async def _close(self, status: str, detail: str) -> None:
        if self._open is None:
            return
        step_id, title = self._open
        self._open = None
        frame = {"id": step_id, "title": title, "status": status}
        if detail:
            frame["detail"] = detail[:DETAIL_CHARS]
        self.finished.append(dict(frame))
        await self._emit("step", frame)
