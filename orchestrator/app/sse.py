"""The ONE place that formats Server-Sent Events (spec §10 + V2-DESIGN §2).

Wire format — one of the event types below, each carrying a JSON payload:

    event: token
    data: {"text": "<incremental model text>"}

    event: meta
    data: {<engine-specific metadata: route, data, chart, citations, report_files, ...>}

    event: done
    data: {"session_id": "..."}     # terminal on success

    event: error
    data: {"message": "..."}        # terminal on failure (no `done` after `error`)

V2 extends the contract backward-compatibly (V2-DESIGN §2) — the four v1
frames above stay byte-identical:

    event: reasoning
    data: {"text": "<model thinking delta>"}

    event: step
    data: {"id": 1, "title": "...", "status": "running|done|failed", "detail"?: "..."}

Every event ends with a blank line, per the SSE specification.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

# The v1 contract events (spec §10) — unchanged, byte-identical on the wire.
ALLOWED_EVENTS = ("token", "meta", "done", "error")
# V2 additions (V2-DESIGN §2): model thinking deltas + agent step progress.
V2_EVENTS = ("reasoning", "step")
# Phase 1: transient progress line for web search / URL / repo work
# ("Searching the web…", "Reading N sources…"). Same {"text": ...} shape.
PROGRESS_EVENTS = ("status",)
# Research panel: the searches behind an answer, streamed as they run.
# {"phase": "query", "query": str, "results": [{title, url, domain}]} for each
# search, then {"phase": "reading"|"read", "count": int} around the fetch.
RESEARCH_EVENTS = ("research",)
ALL_EVENTS = ALLOWED_EVENTS + V2_EVENTS + PROGRESS_EVENTS + RESEARCH_EVENTS

STEP_STATUSES = ("running", "done", "failed")


def sse_event(event: str, data: Optional[Mapping[str, Any]] = None) -> str:
    """Format a single SSE frame. `event` must be one of ALL_EVENTS."""
    if event not in ALL_EVENTS:
        raise ValueError(f"unknown SSE event type: {event!r} (allowed: {ALL_EVENTS})")
    payload = json.dumps(dict(data or {}), ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def token_event(text: str) -> str:
    return sse_event("token", {"text": text})


def meta_event(data: Mapping[str, Any]) -> str:
    return sse_event("meta", data)


def done_event(data: Optional[Mapping[str, Any]] = None) -> str:
    return sse_event("done", data or {})


def error_event(message: str) -> str:
    return sse_event("error", {"message": message})


def reasoning_event(text: str) -> str:
    """V2: model thinking delta — same {"text": ...} payload shape as token."""
    return sse_event("reasoning", {"text": text})


def step_event(id: int, title: str, status: str, detail: Optional[str] = None) -> str:
    """V2: agent step progress. `detail` is included only when provided."""
    if status not in STEP_STATUSES:
        raise ValueError(f"unknown step status: {status!r} (allowed: {STEP_STATUSES})")
    payload: dict = {"id": id, "title": title, "status": status}
    if detail is not None:
        payload["detail"] = detail
    return sse_event("step", payload)
