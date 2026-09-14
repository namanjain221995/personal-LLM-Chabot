"""Processing progress: in-process pub/sub, throttled row writes, and the
`GET /v1/files/{file_id}/events` SSE stream (design §2.8, §4.2).

THE THREE CONSUMERS OF ONE PROGRESS TICK.

1. `publish(blob_id, event)` wakes every in-process subscriber at once — the
   SSE route, `jobs.wait_until_terminal` (model-input readiness) — without a
   database round trip. Never blocks: a subscriber that is not reading keeps
   only the LATEST events (a bounded queue that drops its oldest), because a
   progress stream is a state, not a log.
2. `ProgressThrottle` decides when the blob row's `progress` / `stages` jsonb
   may be written: at most once per 2 s per blob (design §4.2), except that a
   stage transition is always written — a restart must see the stage that is
   durably done, and a percent tick that loses a race costs nothing.
3. The SSE stream itself (below).

THE STREAM'S RULES (design §2.8, CONTRACT §10's framing rules applied to the
file vocabulary):

* events `file.processing` (data = the File object) on every stage or percent
  change, at most one per second; then EXACTLY ONE terminal `file.processed`
  or `file.failed`; then the stream closes. A file already terminal gets its
  terminal event at once and nothing else;
* `sequence_number` starts at 1 and increases by exactly 1; a heartbeat is a
  comment (`: ping`) that consumes no number, sent after 15 s of silence for
  the whole life of the stream — there is no server deadline (owner: no
  timeouts), the stream ends only at a terminal state or a client disconnect;
* the in-process pub/sub is only a doorbell: the File object is always
  re-read through the loader, and a DB poll every 5 s covers a job running in
  ANOTHER process (a blue/green overlap, or after a restart) that this
  process's pub/sub never hears;
* a file deleted while the stream is open ends it with `file.failed` carrying
  the last File object with `status: "deleted"` — the SDKs' own terminal
  literal — so a consumer waiting for a terminal event is never left hanging.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import contextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Iterator, Mapping, Optional, Set

log = logging.getLogger(__name__)

FILE_PROCESSING = "file.processing"
FILE_PROCESSED = "file.processed"
FILE_FAILED = "file.failed"
EVENT_NAMES = (FILE_PROCESSING, FILE_PROCESSED, FILE_FAILED)
TERMINAL_EVENTS = (FILE_PROCESSED, FILE_FAILED)

#: CONTRACT §10: a heartbeat at least every 15 s.
HEARTBEAT_S = 15.0
#: Design §2.8: the DB poll that covers a job in another process.
POLL_S = 5.0
#: Design §2.8: `file.processing` throttled to one per second.
EVENT_THROTTLE_S = 1.0
#: Design §4.2: progress row writes at most one per 2 s per blob.
ROW_WRITE_INTERVAL_S = 2.0
#: Events a slow in-process subscriber keeps (the newest win).
SUBSCRIBER_QUEUE = 16
#: One File-row load may take this long before it counts as failed: shorter
#: than the heartbeat, so a stalled database cannot silence the stream.
LOAD_TIMEOUT_S = 10.0


class StreamProtocolError(RuntimeError):
    """A frame the grammar forbids (a second terminal, an unknown name)."""


# -------------------------------------------------------------- pub / sub --

_subscribers: Dict[str, Set["asyncio.Queue[dict]"]] = {}


def publish(blob_id: str, event: Mapping[str, Any]) -> None:
    """Wake every local subscriber of `blob_id`. Loop-thread only; never
    blocks, never raises into the job that published."""
    for queue in list(_subscribers.get(str(blob_id), ())):
        payload = dict(event)
        while True:
            try:
                queue.put_nowait(payload)
                break
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - raced to empty
                    pass


@contextmanager
def subscription(blob_id: str) -> Iterator["asyncio.Queue[dict]"]:
    queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE)
    key = str(blob_id)
    _subscribers.setdefault(key, set()).add(queue)
    try:
        yield queue
    finally:
        group = _subscribers.get(key)
        if group is not None:
            group.discard(queue)
            if not group:
                _subscribers.pop(key, None)


def subscriber_count(blob_id: str) -> int:
    return len(_subscribers.get(str(blob_id), ()))


def reset_for_tests() -> None:
    _subscribers.clear()


# ------------------------------------------------------- row-write throttle --


class ProgressThrottle:
    """At most one progress write per `interval_s` per blob; a forced write
    (stage transition, terminal) always passes and restarts the interval."""

    def __init__(self, interval_s: float = ROW_WRITE_INTERVAL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.interval_s = float(interval_s)
        self._clock = clock
        self._last: Dict[str, float] = {}

    def should_write(self, blob_id: str, *, force: bool = False) -> bool:
        now = self._clock()
        last = self._last.get(blob_id)
        if force or last is None or now - last >= self.interval_s:
            self._last[blob_id] = now
            return True
        return False

    def forget(self, blob_id: str) -> None:
        self._last.pop(blob_id, None)


# ------------------------------------------------------------ the framing --


class FileEventFramer:
    """Frames for one `/events` stream: numbering and the one-terminal rule."""

    def __init__(self) -> None:
        self.sequence_number = 0
        self.terminal: Optional[str] = None

    def frame(self, name: str, file_object: Mapping[str, Any]) -> str:
        if name not in EVENT_NAMES:
            raise StreamProtocolError(f"unknown file event {name!r}")
        if self.terminal is not None:
            raise StreamProtocolError(f"{name!r} after the terminal {self.terminal!r}")
        self.sequence_number += 1
        body = {"type": name, "sequence_number": self.sequence_number, "data": dict(file_object)}
        if name in TERMINAL_EVENTS:
            self.terminal = name
        return f"event: {name}\ndata: {json.dumps(body, ensure_ascii=False, default=str)}\n\n"

    @staticmethod
    def heartbeat() -> str:
        return ": ping\n\n"


def _terminal_for(file_object: Mapping[str, Any]) -> Optional[str]:
    status = str(file_object.get("status") or "")
    if status == "processed":
        return FILE_PROCESSED
    if status in ("error", "deleted"):
        return FILE_FAILED
    return None


def _signature(file_object: Mapping[str, Any]) -> str:
    """What counts as "changed": status and the processing state, stage,
    step and percent — not timestamps that tick without news."""
    processing = file_object.get("processing") or {}
    if not isinstance(processing, Mapping):
        processing = {}
    parts = {
        "status": file_object.get("status"),
        "state": processing.get("state"),
        "stage": processing.get("stage"),
        "step": processing.get("step"),
        "percent": processing.get("percent"),
        "stages": processing.get("stages"),
    }
    return json.dumps(parts, sort_keys=True, default=str)


#: Returns the current File object, or None once the file is gone.
Loader = Callable[[], Awaitable[Optional[Dict[str, Any]]]]


async def file_event_frames(
    load: Loader,
    *,
    blob_id: Optional[str] = None,
    heartbeat_s: float = HEARTBEAT_S,
    poll_s: float = POLL_S,
    throttle_s: float = EVENT_THROTTLE_S,
    is_disconnected: Optional[Callable[[], Awaitable[bool]]] = None,
    clock: Callable[[], float] = time.monotonic,
    load_timeout_s: float = LOAD_TIMEOUT_S,
) -> AsyncIterator[str]:
    """The SSE body for one file. Ends after the terminal frame, or when the
    client disconnects. `blob_id` subscribes to local progress (a file still
    assembling has none yet: it re-subscribes once a blob appears).

    LOADS ARE COALESCED (review finding, 2026-09-13). A local event is a
    doorbell, and the job rings it on every OCR page and embed batch: loading
    the File row on each ring measured 401 row reads in 20.1 s for ONE stream
    at 20 publishes/s, on the Postgres pool that once ran out of slots. Now a
    ring inside `throttle_s` of the last load waits for the window to open, so
    a stream reads at most once per `throttle_s` plus the `poll_s` poll.

    A LOADER THAT FAILS OR STALLS does not end the stream: it is retried with
    backoff up to `poll_s`, bounded by `load_timeout_s` (shorter than the
    heartbeat), and `: ping` keeps flowing meanwhile."""
    framer = FileEventFramer()
    last_signature: Optional[str] = None
    last_event_at = -1e18
    last_write_at = clock()
    last_load_at = -1e18
    last_known: Optional[Dict[str, Any]] = None
    current_blob = blob_id
    failures = 0

    def subscribe_to(bid: Optional[str]):
        return subscription(bid) if bid else None

    context = subscribe_to(current_blob)
    queue = context.__enter__() if context is not None else None
    try:
        while True:
            last_load_at = clock()
            try:
                snapshot = await asyncio.wait_for(load(), timeout=load_timeout_s)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a database blip must not end a stream with no terminal
                failures += 1
                log.warning("files events: loading the file failed (%d in a row)", failures, exc_info=True)
                wait_for = min(poll_s, throttle_s * (2 ** min(failures, 6)))
                snapshot = _LOAD_FAILED
            if snapshot is _LOAD_FAILED:
                pass
            elif snapshot is None:
                gone = dict(last_known or {})
                gone["status"] = "deleted"
                processing = gone.get("processing")
                if isinstance(processing, Mapping):
                    # The last object said "processing"; a deleted file's
                    # processing is over, and did not succeed.
                    gone["processing"] = {**processing, "state": "failed"}
                yield framer.frame(FILE_FAILED, gone)
                return
            else:
                failures = 0
                new_blob = snapshot.get("_blob_id") if isinstance(snapshot.get("_blob_id"), str) else None
                # Private keys never reach the wire, not even in the deleted
                # frame built from this later (review, 2026-09-13).
                public = {k: v for k, v in snapshot.items() if not k.startswith("_")}
                last_known = public
                terminal = _terminal_for(public)
                if terminal is not None:
                    yield framer.frame(terminal, public)
                    return
                signature = _signature(public)
                now = clock()
                if signature != last_signature:
                    if now - last_event_at >= throttle_s:
                        yield framer.frame(FILE_PROCESSING, public)
                        last_signature = signature
                        last_event_at = now
                        last_write_at = now
                        wait_for = poll_s
                    else:
                        # Changed inside the throttle window: come back when it opens.
                        wait_for = max(0.0, throttle_s - (now - last_event_at))
                else:
                    wait_for = poll_s
                if new_blob and new_blob != current_blob:
                    if context is not None:
                        context.__exit__(None, None, None)
                    current_blob = new_blob
                    context = subscribe_to(current_blob)
                    queue = context.__enter__() if context is not None else None
            # Sleep until the poll/throttle timer or the heartbeat, or a local
            # progress event — which only cuts the sleep short once the
            # throttle window since the last load has opened.
            deadline = clock() + wait_for
            while True:
                if is_disconnected is not None and await is_disconnected():
                    return
                now = clock()
                if now >= deadline:
                    break
                until_heartbeat = heartbeat_s - (now - last_write_at)
                if until_heartbeat <= 0:
                    yield framer.heartbeat()
                    last_write_at = now
                    continue
                if await _wait(queue, min(deadline - now, until_heartbeat)) and not failures:
                    deadline = min(deadline, last_load_at + throttle_s)
    finally:
        if context is not None:
            context.__exit__(None, None, None)


#: Marks a load that raised or timed out (distinct from None = file gone).
_LOAD_FAILED: Any = object()


async def _wait(queue: Optional["asyncio.Queue[dict]"], timeout: float) -> bool:
    """True when a local event arrived (drained), False on timeout."""
    if queue is None:
        await asyncio.sleep(timeout)
        return False
    try:
        async with asyncio.timeout(timeout):
            await queue.get()
    except asyncio.TimeoutError:
        return False
    while not queue.empty():
        queue.get_nowait()
    return True


# ------------------------------------------------------------- the route --


async def stream_file_events(
    request: Any,
    caller: Any,
    file_row: Mapping[str, Any],
    *,
    render: Optional[Callable[[Mapping[str, Any]], Dict[str, Any]]] = None,
    load_row: Optional[Callable[[str, str], Awaitable[Optional[Mapping[str, Any]]]]] = None,
):
    """The StreamingResponse for route 6. `file_row` is the caller's live file
    (already project-checked by the route: an absent or foreign id never gets
    here). `render` builds the File object from a row (team INGEST's wire
    module); `load_row` re-reads the row project-scoped (`schema.get_api_file`)."""
    from starlette.responses import StreamingResponse

    from .. import db
    from ..publicapi import streaming

    project_id = str(file_row["project_id"])
    file_id = str(file_row["id"])
    if render is None:
        from ..publicapi.files import wire  # team INGEST's File object
        from . import jobs

        def render(row: Mapping[str, Any]) -> Dict[str, Any]:
            return wire.file_object(row, processing_view=jobs.file_processing_view)
    if load_row is None:
        from . import schema

        async def load_row(pid: str, fid: str):
            return await db.run_in_thread(schema.get_api_file, pid, fid)

    async def load() -> Optional[Dict[str, Any]]:
        row = await load_row(project_id, file_id)
        if row is None:
            return None
        body = dict(render(row))
        if row.get("blob_id"):
            body["_blob_id"] = str(row["blob_id"])
        return body

    async def body() -> AsyncIterator[bytes]:
        frames = file_event_frames(
            load,
            blob_id=str(file_row["blob_id"]) if file_row.get("blob_id") else None,
            is_disconnected=getattr(request, "is_disconnected", None),
        )
        try:
            async for frame in frames:
                yield frame.encode("utf-8")
        finally:
            await frames.aclose()

    return StreamingResponse(body(), media_type="text/event-stream", headers=dict(streaming.SSE_HEADERS))
