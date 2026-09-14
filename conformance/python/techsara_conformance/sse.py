"""Read a raw `text/event-stream`, and time one on the wire.

The SDK hides two things a conformance run must see: SSE comment lines (the
`: ping` heartbeat of CONTRACT-3 §10 — openai-python drops them in
`_streaming.SSEDecoder.decode`, `if line.startswith(":"): return None`) and
WHEN bytes arrived. `read` keeps comments for a raw httpx stream; `WireClock`
times every chunk inside the SDK's own transport, so a heartbeating server is
seen as not silent even though the SDK yields no event for a ping.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional


@dataclass
class Event:
    name: Optional[str]
    data: str
    arrived: float

    def json(self) -> Any:
        return json.loads(self.data)


@dataclass
class Transcript:
    events: List[Event] = field(default_factory=list)
    comments: List[float] = field(default_factory=list)


def read(lines: Iterator[str]) -> Transcript:
    out = Transcript()
    name: Optional[str] = None
    data: List[str] = []
    for line in lines:
        now = time.monotonic()
        if line == "":
            if data:
                out.events.append(Event(name, "\n".join(data), now))
            name, data = None, []
            continue
        if line.startswith(":"):
            out.comments.append(now)
            continue
        field_name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field_name == "event":
            name = value
        elif field_name == "data":
            data.append(value)
    if data:
        out.events.append(Event(name, "\n".join(data), time.monotonic()))
    return out


class WireClock:
    """When the bytes of ONE streamed response arrived, measured in the transport.

    WHY (2026-09-13, review finding): the first long-stream test timed SDK
    events from before `create()`, so a client-side pacing sleep or a
    capacity-gate wait before the status line (≤ 30 s, §10) counted as stream
    silence, and a `: ping` never reset the clock because the SDK drops
    comments. Silence here starts when the response headers arrive and is
    reset by ANY byte, heartbeat comments included — which is what keeps a
    proxy between caller and edge from closing the connection.

    Pass one to `make_client(wire=...)`; the transport calls `start`, `chunk`
    and `end`. The time before the headers is kept apart as
    `pre_header_wait_s` and reported, not counted as silence.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self.sent_at: Optional[float] = None
        self.headers_at: Optional[float] = None
        self.ended_at: Optional[float] = None
        self.arrivals: List[float] = []
        self.comment_lines = 0
        self.bytes = 0
        self._partial = b""

    def start(self, sent_at: float) -> None:
        with self._lock:
            self.sent_at, self.headers_at, self.ended_at = sent_at, self._clock(), None
            self.arrivals, self.comment_lines, self.bytes, self._partial = [], 0, 0, b""

    def chunk(self, data: bytes) -> None:
        now = self._clock()
        with self._lock:
            self.arrivals.append(now)
            self.bytes += len(data)
            lines = (self._partial + data).split(b"\n")
            self._partial = lines.pop()
            self.comment_lines += sum(1 for line in lines if line.startswith(b":"))

    def end(self) -> None:
        with self._lock:
            self.ended_at = self._clock()

    @property
    def pre_header_wait_s(self) -> Optional[float]:
        if self.sent_at is None or self.headers_at is None:
            return None
        return self.headers_at - self.sent_at

    def silences_s(self) -> List[float]:
        """Every gap with no byte: headers → first chunk, chunk → chunk, and
        last chunk → end of stream when the stream was read to its end."""
        if self.headers_at is None:
            return []
        marks = [self.headers_at, *self.arrivals]
        if self.ended_at is not None:
            marks.append(self.ended_at)
        return [b - a for a, b in zip(marks, marks[1:])]

    def max_silence_s(self) -> float:
        return max(self.silences_s(), default=0.0)


def error_body(payload: Dict[str, Any]) -> Dict[str, Any]:
    return payload.get("error") if isinstance(payload, dict) else {}
