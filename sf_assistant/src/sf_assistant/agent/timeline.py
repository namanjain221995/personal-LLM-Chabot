"""The timeline of one answer: where the seconds of a question went (latency logging, 2026-10-29).

:func:`sf_assistant.pipeline.answer` opens it (:func:`begin`) and closes it (:func:`end`); ``result_row`` writes it
to the JSONL record as ``timeline = {t0_utc, entries}``, so it reaches the page log, the cli sidecars and the
evaluation runs. Every entry has ``kind`` (stage, model, tool or check), ``stage`` (the named block it ran in, or
``other``), ``at`` (seconds from the start of the answer) and ``s`` (its seconds), plus a few fields of its kind:

* stage entries: the blocks of ``pipeline.answer`` (spelling, records, lean, knowledge, wait_records, merge, assemble)
  and of the record session (triage, people_spots, route, names, value_spots, hints, post);
* model entries (``agent.llm``): round, what the call answered, tools offered, max tokens, tokens, finish reason,
  attempts;
* tool entries (``agent.knowledge_agent``): tool name, characters fed back to the model, cut, failed, repeated;
* check entries: the loop rules that can cost one more model call.

Only stage and tool names from fixed lists, numbers and fixed words go in: never a question, an answer, a tool
argument or result, a name or a stored value. ``python -m sf_assistant.evaluation timeline`` prints it.

One question is answered at a time in a process (the page has one worker thread and a lock, the cli and the
evaluation runner ask in a loop), so the open timeline is module state. A thread-local label names the stage of the
entries a thread adds: :func:`stage` sets it for a block, :func:`run_in` for a function handed to a thread pool (a
pool thread starts without it). With no timeline open every function does nothing, and no function ever raises.
Standard library only.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

__all__ = ["begin", "end", "add", "stage", "run_in", "MAX_ENTRIES"]

#: Entries kept per answer; later ones are counted in ``dropped`` (an answer has about 25 to 60).
MAX_ENTRIES = 2000

_lock = threading.Lock()
_local = threading.local()
_open: dict | None = None   # {"t0": perf_counter, "t0_utc": str, "entries": [...], "dropped": int}


def begin() -> None:
    """Open the timeline of a new answer (a timeline still open is dropped). Clears this thread's label."""
    global _open
    try:
        t0 = time.perf_counter()
        t0_utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with _lock:
            _open = {"t0": t0, "t0_utc": t0_utc, "entries": [], "dropped": 0}
        _local.label = None
    except Exception:  # noqa: BLE001 - logging never fails an answer
        pass


def end() -> dict:
    """Close the timeline: ``{"t0_utc", "entries"}`` with the entries sorted by ``at`` (and ``dropped`` when entries
    were dropped), or {} when none was open."""
    global _open
    try:
        with _lock:
            tl, _open = _open, None
        if tl is None:
            return {}
        out: dict[str, Any] = {"t0_utc": tl["t0_utc"], "entries": sorted(tl["entries"], key=lambda e: e["at"])}
        if tl["dropped"]:
            out["dropped"] = tl["dropped"]
        return out
    except Exception:  # noqa: BLE001
        return {}


def add(kind: str, start: float, **fields: Any) -> None:
    """Add one entry that started at ``start`` (a ``time.perf_counter()`` value) and ends now. ``stage`` defaults to
    this thread's label (``other`` outside every block); the other ``fields`` follow ``at`` and ``s``."""
    try:
        now = time.perf_counter()
        name = fields.pop("stage", None) or getattr(_local, "label", None) or "other"
        with _lock:
            tl = _open
            if tl is None:
                return
            if len(tl["entries"]) >= MAX_ENTRIES:
                tl["dropped"] += 1
                return
            tl["entries"].append({"kind": kind, "stage": name, "at": round(start - tl["t0"], 3),
                                  "s": round(now - start, 3), **fields})
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def stage(name: str) -> Iterator[None]:
    """A block of the answer: entries this thread adds inside it carry ``name``, and the block adds one stage entry
    when it ends (also when it raises; the exception goes on unchanged)."""
    start = time.perf_counter()
    previous = getattr(_local, "label", None)
    _local.label = name
    try:
        yield
    finally:
        _local.label = previous
        add("stage", start, stage=name)


def run_in(name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """``fn(*args, **kwargs)`` as stage ``name``: for ``ThreadPoolExecutor.submit``, whose thread has no label."""
    with stage(name):
        return fn(*args, **kwargs)
