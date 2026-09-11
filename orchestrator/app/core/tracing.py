"""Durable, privacy-bounded tracing for one chat generation.

The trace id is the existing ``generation_id``. Persistence is best-effort:
diagnostics must never turn a successful user request into a failed one.
Raw prompts, result rows, answer text, credentials and chain-of-thought do not
belong here. Callers record structured decisions and bounded summaries only.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
from time import perf_counter
from typing import Any, Dict, Optional

from .. import db

log = logging.getLogger(__name__)

_current: contextvars.ContextVar[Optional["TraceRecorder"]] = contextvars.ContextVar(
    "query_trace_recorder", default=None
)
_SECRET_PARTS = (
    "authorization", "cookie", "password", "passwd", "secret", "token",
    "api_key", "apikey", "private_key", "session",
)
_MAX_DEPTH = 5
_MAX_ITEMS = 60
_MAX_TEXT = 4000


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SECRET_PARTS)


def sanitize(value: Any, *, _depth: int = 0) -> Any:
    """Return a JSON-safe, bounded value with credential-like keys redacted."""
    if _depth >= _MAX_DEPTH:
        return "[depth-limit]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= _MAX_TEXT else value[:_MAX_TEXT] + "…[truncated]"
    if hasattr(value, "model_dump"):
        try:
            value = value.model_dump(mode="json")
        except TypeError:
            value = value.model_dump()
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                out["[truncated]"] = f"{len(value) - _MAX_ITEMS} more keys"
                break
            name = str(key)
            out[name] = "[redacted]" if _is_secret_key(name) else sanitize(
                item, _depth=_depth + 1
            )
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        out = [sanitize(item, _depth=_depth + 1) for item in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            out.append(f"[{len(items) - _MAX_ITEMS} more items]")
        return out
    return sanitize(str(value), _depth=_depth + 1)


def text_fingerprint(value: str) -> dict:
    """Identify a large evidence block without persisting its contents."""
    raw = (value or "").encode("utf-8", errors="replace")
    return {"characters": len(value or ""), "sha256": hashlib.sha256(raw).hexdigest()}


class TraceRecorder:
    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        self._sequence = 0
        self._started = perf_counter()
        self._finished = False
        self._last_stage = ""
        self.selected_route = ""
        self.resolved_mode = ""

    def activate(self) -> contextvars.Token:
        return _current.set(self)

    @staticmethod
    def deactivate(token: contextvars.Token) -> None:
        _current.reset(token)

    async def start(
        self,
        *,
        conversation_id: str,
        user_id: Optional[int],
        workspace_id: str,
        question: str,
        requested_mode: str,
    ) -> None:
        try:
            await db.run_in_thread(
                db.start_query_trace,
                self.trace_id,
                conversation_id,
                user_id,
                workspace_id,
                question,
                requested_mode,
            )
        except Exception:
            log.warning("query trace root could not be persisted trace_id=%s", self.trace_id, exc_info=True)

    async def event(
        self,
        stage: str,
        *,
        status: str = "success",
        component: str = "",
        details: Optional[dict] = None,
        duration_ms: Optional[int] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        if self._finished:
            return
        self._sequence += 1
        self._last_stage = stage
        safe = sanitize(details or {})
        error_type = type(error).__name__ if error else ""
        error_message = sanitize(str(error)) if error else ""
        # JSON in application logs is intentionally only an envelope. The
        # diagnostic detail lives in PostgreSQL with its normal access controls.
        log.info(
            "query_trace %s",
            json.dumps(
                {
                    "trace_id": self.trace_id,
                    "sequence": self._sequence,
                    "stage": stage,
                    "status": status,
                    "component": component,
                    "duration_ms": duration_ms,
                },
                separators=(",", ":"),
            ),
        )
        try:
            await db.run_in_thread(
                db.append_query_trace_event,
                self.trace_id,
                self._sequence,
                stage,
                status,
                component,
                safe,
                duration_ms,
                error_type,
                error_message,
            )
        except Exception:
            log.warning("query trace event could not be persisted trace_id=%s stage=%s", self.trace_id, stage, exc_info=True)

    async def finish(
        self,
        status: str,
        *,
        route: str = "",
        resolved_mode: str = "",
        error: Optional[BaseException] = None,
        meta: Optional[dict] = None,
    ) -> None:
        if self._finished:
            return
        self._finished = True
        self.selected_route = route or self.selected_route
        self.resolved_mode = resolved_mode or self.resolved_mode
        try:
            await db.run_in_thread(
                db.finish_query_trace,
                self.trace_id,
                status,
                round((perf_counter() - self._started) * 1000),
                resolved_mode=self.resolved_mode,
                selected_route=self.selected_route,
                error_stage=self._last_stage if error else "",
                error_type=type(error).__name__ if error else "",
                error_message=sanitize(str(error)) if error else "",
                meta=sanitize(meta or {}),
            )
        except Exception:
            log.warning("query trace could not be finalized trace_id=%s", self.trace_id, exc_info=True)


async def event(stage: str, **kwargs: Any) -> None:
    """Record on the current generation, or do nothing outside a request."""
    recorder = _current.get()
    if recorder is not None:
        await recorder.event(stage, **kwargs)


def current() -> Optional[TraceRecorder]:
    return _current.get()
