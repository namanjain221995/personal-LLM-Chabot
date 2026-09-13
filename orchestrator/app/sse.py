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
import os
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


# --- keep-alive ---------------------------------------------------------
# A generation can legitimately go minutes without producing an event: agent
# planning, retrieval + reranking, or a long thinking pass on the dense 27B
# (~10-15 tok/s here) all run silent. An SSE body that emits nothing for that
# long is indistinguishable from a dead connection to anything in the middle:
# Node/undici cuts the response at 300s idle (UND_ERR_BODY_TIMEOUT), which is
# what the Next.js proxy was reporting to users as "the orchestrator is
# unreachable". A comment frame is the SSE spec's own keep-alive — every
# compliant parser (including frontend/lib/sse.ts) drops lines starting with
# ":" — so it resets those idle timers without touching the event contract.
HEARTBEAT_SECONDS: float = float(os.environ.get("SSE_HEARTBEAT_SECONDS", "15"))


def sse_comment(note: str = "keep-alive") -> str:
    """Format an SSE comment frame. Ignored by clients; keeps the pipe warm."""
    return f": {note}\n\n"


# --- relay coalescing ----------------------------------------------------
# One HTTP body write per token was the relay's shape until 2026-09-13: every
# decoded token became its own ASGI send, its own chunk through the Next.js
# proxy and Cloudflare, and its own parse + React render in the browser.
# Measured the same week: the engine decodes ~105 tok/s single-stream while
# people SAW a p50 of 87.6 tok/s after MTP was turned off (usage_events,
# n=13) — a 15-35% loss after the engine, largest on short answers. At
# ~9.4 ms per token a 25 ms frame carries two or three tokens, cutting those
# writes ~2-3x, and the FIRST frame after a quiet spell is never held back
# (LiveGeneration.follow), so time to first token does not move.
#
# Frames are concatenated, never merged: the bytes on the wire are exactly
# the per-event frames laid end to end, which every SSE parser already has
# to accept because TCP coalesces writes anyway. 0 turns coalescing off.
# Named as config.py would name it (SSE_COALESCE_MS); read here, beside
# SSE_HEARTBEAT_SECONDS, with config.py's blank-means-default rule.
def _coalesce_seconds() -> float:
    raw = os.environ.get("SSE_COALESCE_MS")
    if raw is None or raw.strip() == "":
        return 0.025
    return max(0.0, float(raw)) / 1000.0


COALESCE_SECONDS: float = _coalesce_seconds()

#: Events that stream at decode speed and so are worth holding a few ms to
#: share a write. Everything else (status, step, meta, done, error) is rare
#: and is simply carried by the next write.
STREAMED_EVENTS = ("token", "reasoning")
