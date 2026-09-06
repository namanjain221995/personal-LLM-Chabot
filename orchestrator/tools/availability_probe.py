"""Availability measured from REQUEST evidence, not from container metadata.

WHY THIS EXISTS
---------------
"Nothing else was interrupted" is routinely argued from ``docker inspect``:
``RestartCount`` is still 0 and ``StartedAt`` is unchanged. Both facts are
about the CONTAINER; neither is about the SERVICE.

A container stays up while the process inside it

* refuses new connections (listener backlog full, worker pool exhausted),
* answers 502/503 from a proxy in front of it,
* accepts the connection and never replies — this project has a recorded case
  of a wedged vLLM engine holding ``/health`` green for 5.5 hours,
* opens an SSE stream and dies half way through the body, which a status-code
  probe scores as a success because the status line said 200,
* or serves everything correctly, which is what we hope to show.

An unchanged ``StartedAt`` says only that nothing restarted the container. It
cannot say that any request succeeded during a deploy, because during that
deploy nobody made one. The only evidence that a service was available is a
request that was served. This tool makes those requests continuously, records
EVERY outcome with a timestamp, and reports the windows in which requests were
NOT served — with the resolution of the measurement stated, so the numbers can
be read honestly rather than rounded into a pass.

RUN IT OUTSIDE THE CONTAINER BEING DEPLOYED
-------------------------------------------
Unlike the other tools in this directory, this one is NOT run with
``docker exec`` into the orchestrator: a probe that lives inside the container
being recreated dies with it and measures nothing across the only window that
matters. It therefore imports the standard library ONLY — no ``httpx``, no
``app`` package, no third-party dependency — so it runs from the host (or any
other machine that can reach the endpoint) against any Python 3.9+.

    # from the repository host, across a deploy
    python3 orchestrator/tools/availability_probe.py \
        --preset orchestrator --duration 600 --out /tmp/deploy-availability.json
    # ... run the deploy in another terminal, then Ctrl-C this one (or let it
    # reach --duration); the JSON timeline and the summary are written either way.

WHAT IT WILL NOT DO (these are enforced, not advisory)
------------------------------------------------------
* **GET only, and generation routes are refused outright.** ``POST /chat``,
  ``/v1/chat/completions``, ``/v1/completions``, ``/v1/embeddings``,
  ``/score`` and ``/rerank`` are rejected by :func:`_check_target_safe` before
  a socket is opened. Model time is expensive and is being measured
  separately; an availability probe that consumes it corrupts that
  measurement and its own.
* **No credentials, ever.** No cookie, no Authorization header, no API key
  argument exists. Probe auth-free endpoints; an authenticated route is
  probed for the fact that it answers 401 promptly, which is itself proof the
  app is routing and its session layer is alive.
* **No redirect is followed.** A 3xx is recorded as the status it is. Chasing
  it would resolve a different host and destroy the evidence that *this*
  endpoint answered.
* **No response body is stored.** Bodies are counted, not kept. For a JSON
  target the operator may whitelist a few TOP-LEVEL scalar keys (default:
  ``status``) which are recorded truncated to 64 characters — that is how
  "connected, HTTP 200, and the app says it is degraded" becomes visible
  without logging content. Query strings are recorded as given by the
  operator and must not carry secrets.

ON ``core.net.safe_fetch``
--------------------------
The repository rule that outbound fetches go through ``safe_fetch`` is about
the APPLICATION fetching attacker-influenced third-party URLs: its SSRF
blocklist exists to forbid exactly the loopback and private addresses an
availability probe must reach, and it lives in the container this tool must
outlive. This tool is not part of that path and does not weaken it: it is an
operator instrument, GET-only, non-redirect-following, byte-capped,
timeout-bounded, and pointed only at URLs an operator typed on the command
line. It imports nothing from ``app``.

THE OUTCOME TAXONOMY (a closed set)
-----------------------------------
Every probe lands in exactly one class. The first two are SERVED; the rest are
unavailability, and the distinctions are the point of the tool:

``ok``               connected, expected status, body completed inside the
                     slow threshold.
``slow``             the same, but the response took at least ``slow_ms``.
                     Served is served — reported apart because a deploy that
                     never drops a request can still make every request crawl.
``error``            connected and answered, but not with an expected status
                     (500, 502, 503, an unexpected 404). The caller's request
                     failed; only the cause differs from a refusal.
``refused``          the TCP connection could not be established — connection
                     refused, host unreachable, DNS failure. The service is
                     gone: this is the classic container-recreate signature.
``connect_timeout``  the connection did not complete within the connect
                     budget. NOT the same as refused: something is listening
                     and not accepting, which is a saturated backlog or a
                     wedged accept loop, and a restart would show neither.
``timeout``          connected, request sent, no response headers (or no
                     progress) inside the budget. "Up but not answering" —
                     the wedge case; a container healthcheck of the same
                     endpoint would be timing out too, yet the container is
                     reported healthy until its retries run out.
``truncated``        headers arrived, the body did not finish: reset or EOF
                     mid-body. For a stream this is the "stream opened and
                     never completed" case, and no status-code probe can see
                     it.

RESOLUTION, STATED RATHER THAN IMPLIED
--------------------------------------
A probe every ``--interval`` seconds cannot resolve an outage shorter than
that interval, and cannot place the edges of a longer one more precisely than
that. So every outage is reported twice:

* **observed** — from the start of the first failing probe to the end of the
  last failing probe. Positive evidence of failure; a lower bound.
* **bracketed** — from the end of the last request that was served to the
  start of the next request that was served. Outside that window the service
  is PROVEN to have been working; an upper bound.

The true outage is between the two. The summary prints both, plus the longest
window in the whole run containing no positive proof of service — which, in a
run with no failures at all, is approximately one interval, and saying so is
the honest way to report "100% available at 1 s resolution".
"""
from __future__ import annotations

import argparse
import json
import re
import signal
import socket
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

TOOL_NAME = "availability_probe"
SCHEMA_VERSION = 1
USER_AGENT = "techsara-availability-probe/1"

#: Served classes. Everything else in CLASSES is unavailability.
SERVED_CLASSES = ("ok", "slow")
#: Closed outcome registry — see the module docstring. Nothing else is emitted.
CLASSES = (
    "ok",
    "slow",
    "error",
    "refused",
    "connect_timeout",
    "timeout",
    "truncated",
)

#: SSE event types this project may legitimately put on the wire
#: (``app/sse.py::ALL_EVENTS``). Copied deliberately rather than imported:
#: this tool must not import ``app``. Anything else observed is counted as
#: "other" — the probe OBSERVES the registry, it never emits into it.
SSE_EVENTS = (
    "token",
    "meta",
    "done",
    "error",
    "reasoning",
    "step",
    "status",
    "research",
)
#: Terminal frames: seeing one means the stream completed at the protocol
#: level even if the connection is held open afterwards.
SSE_TERMINAL = ("done", "error")

#: Paths that would spend model time. Refused before a socket is opened.
_FORBIDDEN_PATH_RE = re.compile(
    r"(?:^|/)(?:chat/completions|completions|embeddings|rerank|score|generate|"
    r"responses|audio/speech|images/generations)(?:/|$)",
    re.IGNORECASE,
)
#: The orchestrator's generation route itself (POST-only in the app; refused
#: here as well so a typo cannot become a request that costs a GPU second).
_FORBIDDEN_EXACT = ("/chat",)


def _now_wall() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True)
class Target:
    """One endpoint, and what "served" means for it.

    ``kind`` is ``http`` (read the whole body, then classify) or ``stream``
    (read incrementally and record whether the stream OPENED, whether frames
    kept arriving, and whether it COMPLETED). The stream defaults are wider
    because this project's SSE contract heartbeats every 15 s
    (``sse-heartbeat-and-timeout-invariant``): a read timeout below that would
    report a healthy idle stream as truncated.
    """

    name: str
    url: str
    kind: str = "http"
    expect: Tuple[int, ...] = (200,)
    #: 0 → use the run's global --interval. Set per target because the probes
    #: do not cost the same: /health fans out to a GPU service, /metrics and a
    #: 404 route do not (see :func:`preset_targets`).
    interval_s: float = 0.0
    connect_timeout_s: float = 3.0
    timeout_s: float = 10.0
    budget_s: float = 0.0  # 0 → derived in __post_init__ terms below
    slow_ms: float = 2000.0
    max_bytes: int = 1 << 20
    json_keys: Tuple[str, ...] = ()

    @property
    def effective_budget_s(self) -> float:
        if self.budget_s > 0:
            return self.budget_s
        return max(self.timeout_s, 30.0) if self.kind == "stream" else self.timeout_s

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "kind": self.kind,
            "expect": list(self.expect),
            "interval_s": self.interval_s,
            "connect_timeout_s": self.connect_timeout_s,
            "timeout_s": self.timeout_s,
            "budget_s": self.effective_budget_s,
            "slow_ms": self.slow_ms,
            "max_bytes": self.max_bytes,
            "json_keys": list(self.json_keys),
        }


@dataclass
class Probe:
    """One request and everything that is known about how it went.

    Times: ``*_wall`` is an ISO-8601 UTC stamp for the report, ``*_mono`` is
    a monotonic reading used for every duration and interval computation —
    a wall clock that steps (NTP, a suspend/resume) must never be able to
    invent or erase an outage.
    """

    seq: int
    target: str
    scheduled_mono: float
    start_mono: float
    end_mono: float
    start_wall: str
    end_wall: str
    cls: str
    served: bool
    status: Optional[int] = None
    connect_ms: Optional[float] = None
    first_byte_ms: Optional[float] = None
    total_ms: float = 0.0
    bytes_read: int = 0
    detail: str = ""
    drift_ms: float = 0.0
    stream: Optional[Dict[str, Any]] = None
    json_fields: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "seq": self.seq,
            "target": self.target,
            "start": self.start_wall,
            "end": self.end_wall,
            "class": self.cls,
            "served": self.served,
            "status": self.status,
            "connect_ms": _round(self.connect_ms),
            "first_byte_ms": _round(self.first_byte_ms),
            "total_ms": _round(self.total_ms),
            "bytes": self.bytes_read,
            "schedule_drift_ms": _round(self.drift_ms),
        }
        if self.detail:
            row["detail"] = self.detail
        if self.stream is not None:
            row["stream"] = self.stream
        if self.json_fields:
            row["json"] = self.json_fields
        return row


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 3)


class TargetError(ValueError):
    """A target the tool refuses to probe, with the reason."""


def _check_target_safe(url: str) -> None:
    """Refuse anything that would spend model time, or that is not HTTP.

    Called before any socket exists. The tool is GET-only, so on this
    application the generation route (``POST /chat``) is already unreachable;
    the path check matters when the probe is pointed straight at a model
    server, where a GET to the wrong path is still a request an inference
    engine will queue.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise TargetError(f"{url!r}: only http:// and https:// are probed")
    if not parts.hostname:
        raise TargetError(f"{url!r}: no host")
    path = parts.path or "/"
    if path.rstrip("/").lower() in _FORBIDDEN_EXACT:
        raise TargetError(
            f"{url!r}: refusing to probe the generation route — this tool never "
            "sends chat or generation requests"
        )
    if _FORBIDDEN_PATH_RE.search(path):
        raise TargetError(
            f"{url!r}: refusing to probe an inference path (completions, "
            "embeddings, rerank/score, generate). Model time is measured "
            "separately and a probe must not consume it."
        )


_TARGET_DEFAULTS_BY_KIND = {
    "http": {"timeout_s": 10.0, "slow_ms": 2000.0},
    # 20 s read allowance sits above the 15 s SSE heartbeat invariant.
    "stream": {"timeout_s": 20.0, "slow_ms": 5000.0},
}


def parse_target(spec: str, defaults: Dict[str, Any]) -> Target:
    """Parse ``url=...,name=...,kind=...,expect=200|401,...``.

    A bare URL is accepted as shorthand for ``url=<it>``.
    """
    fields: Dict[str, str] = {}
    if "=" not in spec.split(",", 1)[0]:
        fields["url"] = spec.split(",", 1)[0]
        rest = spec.split(",", 1)[1] if "," in spec else ""
    else:
        rest = spec
    for chunk in rest.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise TargetError(f"{chunk!r} in {spec!r} is not key=value")
        key, _, value = chunk.partition("=")
        fields[key.strip()] = value.strip()
    url = fields.get("url", "")
    if not url:
        raise TargetError(f"{spec!r}: no url=")
    _check_target_safe(url)
    kind = fields.get("kind", "http")
    if kind not in ("http", "stream"):
        raise TargetError(f"{spec!r}: kind must be http or stream")
    kind_defaults = dict(_TARGET_DEFAULTS_BY_KIND[kind])
    kind_defaults.update({k: v for k, v in defaults.items() if v is not None})
    expect_raw = fields.get("expect", "200")
    try:
        expect = tuple(int(part) for part in expect_raw.replace("|", " ").split())
    except ValueError as exc:
        raise TargetError(f"{spec!r}: bad expect={expect_raw!r}") from exc
    if not expect:
        raise TargetError(f"{spec!r}: expect= is empty")
    name = fields.get("name") or (urlsplit(url).path.strip("/").replace("/", "_") or "root")
    json_keys = tuple(
        part.strip()
        for part in fields.get("json_keys", "").replace("|", " ").split()
        if part.strip()
    )

    def _num(key: str, fallback: float) -> float:
        if key in fields:
            try:
                return float(fields[key])
            except ValueError as exc:
                raise TargetError(f"{spec!r}: bad {key}={fields[key]!r}") from exc
        return float(fallback)

    return Target(
        name=name,
        url=url,
        kind=kind,
        expect=expect,
        interval_s=_num("interval", 0.0),
        connect_timeout_s=_num("connect_timeout", kind_defaults.get("connect_timeout_s", 3.0)),
        timeout_s=_num("timeout", kind_defaults.get("timeout_s", 10.0)),
        budget_s=_num("budget", 0.0),
        slow_ms=_num("slow_ms", kind_defaults.get("slow_ms", 2000.0)),
        max_bytes=int(_num("max_bytes", 1 << 20)),
        json_keys=json_keys,
    )


def preset_targets(
    base: str,
    defaults: Dict[str, Any],
    health_interval_s: float = 0.0,
    with_health: bool = True,
) -> List[Target]:
    """The three auth-free orchestrator probes, chosen for what each proves.

    ``route``    GET a path the app does not define. A 404 from the ASGI app
                 is proof the process accepted the connection and routed the
                 request, at zero dependency cost — it touches no database,
                 no vector store and no model server. This is the probe that
                 stays meaningful when a dependency is down but the service
                 is up, and it is cheap enough to run at 1 Hz indefinitely.
    ``metrics``  GET /metrics — the Prometheus exposition. Auth-free by
                 design, one small database count, no GPU. Proves the app can
                 still reach PostgreSQL and render a response body.
    ``health``   GET /health — the real dependency report.
                 **This one is NOT free**: on this deployment it fans out to
                 every vLLM service's /health, GET /v1/models on the main
                 engine, AND a POST /score to the reranker, which is a (tiny)
                 GPU forward pass. The container healthcheck already runs it
                 every 30 s. Probing it at 1 Hz multiplies that by 30, so it
                 is given a longer interval multiplier by --preset and is the
                 first thing to drop with --no-health when someone is
                 measuring the GPU.

    Every probe leaves one INFO access-log line in the orchestrator
    (``"GET /... " 200``), so a 1 Hz run adds ~60 lines a minute per target.
    That is the visible cost of the evidence; size the run accordingly rather
    than discovering it in the log later.
    """
    base = base.rstrip("/")
    targets = [
        parse_target(
            f"url={base}/__availability_probe__,name=route,expect=404,timeout=5", defaults
        ),
        parse_target(f"url={base}/metrics,name=metrics,expect=200,timeout=10", defaults),
    ]
    if with_health:
        targets.append(
            parse_target(
                f"url={base}/health,name=health,expect=200,timeout=15,"
                f"json_keys=status,interval={health_interval_s}",
                defaults,
            )
        )
    return targets


# ---------------------------------------------------------------------------
# One probe
# ---------------------------------------------------------------------------


def _connection(target: Target, insecure: bool):
    parts = urlsplit(target.url)
    host = parts.hostname or ""
    port = parts.port
    if parts.scheme == "https":
        context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
        return HTTPSConnection(
            host, port or 443, timeout=target.connect_timeout_s, context=context
        ), parts
    return HTTPConnection(host, port or 80, timeout=target.connect_timeout_s), parts


def _request_path(parts) -> str:
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def _scalar(value: Any) -> Any:
    """Whitelisted JSON values only: a short scalar, never a structure.

    A response body is never stored. This exists so that "HTTP 200 and the app
    says degraded" is distinguishable from "HTTP 200 and all is well", which
    is a fact about availability, not content.
    """
    if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
        return value
    if isinstance(value, str):
        return value[:64]
    return f"<{type(value).__name__}>"


def _read_stream(resp, target: Target, deadline: float) -> Dict[str, Any]:
    """Read a response incrementally, counting SSE frames — never keeping them.

    Returns the stream record. ``stopped_by`` says who ended it, which is the
    whole point: ``eof`` (the server finished), ``terminal`` (a `done`/`error`
    frame — the SSE contract's own end), ``budget``/``max_bytes`` (WE stopped
    a healthy stream), or ``error`` (it died under us).
    """
    frames = 0
    events: Dict[str, int] = {}
    comments = 0
    total = 0
    first_byte: Optional[float] = None
    last_frame_mono: Optional[float] = None
    max_frame_gap_ms = 0.0
    terminal = ""
    stopped_by = "eof"
    detail = ""
    buffer = b""
    started = time.monotonic()
    while True:
        if time.monotonic() >= deadline:
            stopped_by = "budget"
            break
        if total >= target.max_bytes:
            stopped_by = "max_bytes"
            break
        try:
            chunk = resp.read1(65536)
        except (socket.timeout, TimeoutError):
            stopped_by = "read_timeout"
            detail = "read timed out mid-body"
            break
        except (HTTPException, OSError) as exc:
            stopped_by = "error"
            detail = f"{type(exc).__name__}: {exc}"
            break
        if not chunk:
            stopped_by = "eof"
            break
        now = time.monotonic()
        if first_byte is None:
            first_byte = (now - started) * 1000.0
        total += len(chunk)
        buffer += chunk
        # Frame accounting only: names are matched against the closed SSE
        # registry and counted; `data:` payloads are never inspected or kept.
        while b"\n\n" in buffer:
            raw, _, buffer = buffer.partition(b"\n\n")
            frames += 1
            if last_frame_mono is not None:
                max_frame_gap_ms = max(max_frame_gap_ms, (now - last_frame_mono) * 1000.0)
            last_frame_mono = now
            name = ""
            for line in raw.split(b"\n"):
                if line.startswith(b":"):
                    comments += 1
                elif line.startswith(b"event:"):
                    name = line[6:].strip().decode("ascii", "replace")[:32]
            if name:
                key = name if name in SSE_EVENTS else "other"
                events[key] = events.get(key, 0) + 1
                if name in SSE_TERMINAL:
                    terminal = name
        if terminal:
            stopped_by = "terminal"
            break
        if len(buffer) > 1 << 16:  # an unframed body: count bytes, keep nothing
            buffer = buffer[-1024:]
    return {
        "opened": True,
        "completed": stopped_by in ("eof", "terminal"),
        "stopped_by": stopped_by,
        "frames": frames,
        "events": events,
        "heartbeats": comments,
        "terminal_event": terminal or None,
        "max_frame_gap_ms": _round(max_frame_gap_ms) if frames > 1 else None,
        "bytes": total,
        "first_byte_ms": _round(first_byte),
        "detail": detail,
    }


def probe_once(target: Target, seq: int, scheduled_mono: float, insecure: bool) -> Probe:
    """Make one request and classify the outcome. Never raises.

    A NEW connection every time, with ``Connection: close``. Keep-alive would
    reuse a socket that is already established and would therefore not test
    the thing that fails first in a deploy — whether a *new* client can
    connect at all.
    """
    start_mono = time.monotonic()
    start_wall = _now_wall()
    deadline = start_mono + target.effective_budget_s

    def done(
        cls: str,
        *,
        status: Optional[int] = None,
        connect_ms: Optional[float] = None,
        first_byte_ms: Optional[float] = None,
        bytes_read: int = 0,
        detail: str = "",
        stream: Optional[Dict[str, Any]] = None,
        json_fields: Optional[Dict[str, Any]] = None,
    ) -> Probe:
        end_mono = time.monotonic()
        return Probe(
            seq=seq,
            target=target.name,
            scheduled_mono=scheduled_mono,
            start_mono=start_mono,
            end_mono=end_mono,
            start_wall=start_wall,
            end_wall=_now_wall(),
            cls=cls,
            served=cls in SERVED_CLASSES,
            status=status,
            connect_ms=connect_ms,
            first_byte_ms=first_byte_ms,
            total_ms=(end_mono - start_mono) * 1000.0,
            bytes_read=bytes_read,
            detail=detail[:300],
            drift_ms=(start_mono - scheduled_mono) * 1000.0,
            stream=stream,
            json_fields=json_fields,
        )

    conn, parts = _connection(target, insecure)
    try:
        try:
            conn.connect()
        except (socket.timeout, TimeoutError) as exc:
            return done("connect_timeout", detail=f"{type(exc).__name__}: {exc}")
        except (ConnectionRefusedError, socket.gaierror, OSError) as exc:
            # Refused / unreachable / DNS: nothing is accepting connections.
            return done("refused", detail=f"{type(exc).__name__}: {exc}")
        connect_ms = (time.monotonic() - start_mono) * 1000.0
        try:
            conn.sock.settimeout(target.timeout_s)
        except (AttributeError, OSError):  # pragma: no cover - defensive
            pass
        accept = "text/event-stream" if target.kind == "stream" else "*/*"
        try:
            conn.request(
                "GET",
                _request_path(parts),
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": accept,
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                    "Cache-Control": "no-cache",
                },
            )
        except (HTTPException, OSError) as exc:
            return done("refused", connect_ms=connect_ms, detail=f"{type(exc).__name__}: {exc}")
        try:
            resp = conn.getresponse()
        except (socket.timeout, TimeoutError) as exc:
            # Connected, request sent, no reply: up but not answering.
            return done("timeout", connect_ms=connect_ms, detail=f"{type(exc).__name__}: {exc}")
        except (HTTPException, OSError) as exc:
            return done("truncated", connect_ms=connect_ms, detail=f"{type(exc).__name__}: {exc}")
        status = resp.status
        header_ms = (time.monotonic() - start_mono) * 1000.0

        if target.kind == "stream":
            stream = _read_stream(resp, target, deadline)
            first_byte_ms = header_ms
            if stream["first_byte_ms"] is not None:
                first_byte_ms = header_ms + float(stream["first_byte_ms"])
            if status not in target.expect:
                return done(
                    "error",
                    status=status,
                    connect_ms=connect_ms,
                    first_byte_ms=first_byte_ms,
                    bytes_read=int(stream["bytes"]),
                    detail=f"unexpected HTTP {status}",
                    stream=stream,
                )
            if stream["stopped_by"] in ("error", "read_timeout"):
                # The stream opened and did not complete. This is the failure
                # a status-code probe scores as a success.
                return done(
                    "truncated",
                    status=status,
                    connect_ms=connect_ms,
                    first_byte_ms=first_byte_ms,
                    bytes_read=int(stream["bytes"]),
                    detail=str(stream["detail"] or stream["stopped_by"]),
                    stream=stream,
                )
            slow = header_ms >= target.slow_ms
            return done(
                "slow" if slow else "ok",
                status=status,
                connect_ms=connect_ms,
                first_byte_ms=first_byte_ms,
                bytes_read=int(stream["bytes"]),
                stream=stream,
            )

        # Non-streaming: read the whole body, bounded, and keep none of it.
        body = b""
        read_bytes = 0
        first_byte_ms: Optional[float] = None
        try:
            while True:
                if time.monotonic() >= deadline:
                    return done(
                        "truncated",
                        status=status,
                        connect_ms=connect_ms,
                        first_byte_ms=first_byte_ms,
                        bytes_read=read_bytes,
                        detail="body did not complete inside the budget",
                    )
                chunk = resp.read1(65536)
                if not chunk:
                    break
                if first_byte_ms is None:
                    first_byte_ms = (time.monotonic() - start_mono) * 1000.0
                read_bytes += len(chunk)
                if read_bytes > target.max_bytes:
                    return done(
                        "truncated",
                        status=status,
                        connect_ms=connect_ms,
                        first_byte_ms=first_byte_ms,
                        bytes_read=read_bytes,
                        detail=f"body exceeded max_bytes={target.max_bytes}",
                    )
                if target.json_keys and len(body) < 65536:
                    body += chunk
        except (socket.timeout, TimeoutError) as exc:
            return done(
                "truncated" if read_bytes else "timeout",
                status=status,
                connect_ms=connect_ms,
                first_byte_ms=first_byte_ms,
                bytes_read=read_bytes,
                detail=f"{type(exc).__name__}: {exc}",
            )
        except (HTTPException, OSError) as exc:
            return done(
                "truncated",
                status=status,
                connect_ms=connect_ms,
                first_byte_ms=first_byte_ms,
                bytes_read=read_bytes,
                detail=f"{type(exc).__name__}: {exc}",
            )
        json_fields: Optional[Dict[str, Any]] = None
        if target.json_keys and body:
            try:
                payload = json.loads(body.decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                json_fields = {"_parse": "not JSON"}
            else:
                if isinstance(payload, dict):
                    json_fields = {
                        key: _scalar(payload.get(key))
                        for key in target.json_keys
                        if key in payload
                    }
        total_ms = (time.monotonic() - start_mono) * 1000.0
        if status not in target.expect:
            return done(
                "error",
                status=status,
                connect_ms=connect_ms,
                first_byte_ms=first_byte_ms,
                bytes_read=read_bytes,
                detail=f"unexpected HTTP {status}",
                json_fields=json_fields,
            )
        return done(
            "slow" if total_ms >= target.slow_ms else "ok",
            status=status,
            connect_ms=connect_ms,
            first_byte_ms=first_byte_ms,
            bytes_read=read_bytes,
            json_fields=json_fields,
        )
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — closing must never mask an outcome
            pass


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


class Runner:
    """Fixed-interval sampling that a stalled endpoint cannot slow down.

    Each target keeps its own absolute grid (``t0 + n * interval``) and each
    tick is handed to a worker thread. ``sleep(interval)`` after the response
    would have been simpler and wrong: a service that takes 10 s to answer
    would then be sampled every 11 s, thinning the evidence at exactly the
    moment it matters and inflating every measured gap. Ticks are only ever
    skipped when a target already has ``max_inflight`` probes outstanding
    (i.e. it has been unresponsive for longer than its whole budget); those
    skips are counted and reported rather than hidden.
    """

    def __init__(
        self,
        targets: Sequence[Target],
        interval_s: float,
        duration_s: float,
        *,
        insecure: bool = False,
        jsonl_path: str = "",
        verbose: bool = False,
        quiet: bool = False,
        max_probes: int = 0,
    ) -> None:
        self.targets = list(targets)
        self.interval_s = interval_s
        self.duration_s = duration_s
        self.insecure = insecure
        self.verbose = verbose
        self.quiet = quiet
        self.max_probes = max_probes
        self.stop_event = threading.Event()
        self.stopped_by = "duration"
        self._lock = threading.Lock()
        self._records: List[Probe] = []
        self._seq = 0
        self._inflight: Dict[str, int] = {t.name: 0 for t in self.targets}
        self._skipped: Dict[str, int] = {t.name: 0 for t in self.targets}
        self._last_class: Dict[str, str] = {}
        self._jsonl = open(jsonl_path, "a", encoding="utf-8") if jsonl_path else None
        self.t0_mono = 0.0
        self.t0_wall = ""
        self.t1_mono = 0.0
        self.t1_wall = ""

    # -- plumbing ---------------------------------------------------------
    def _interval_for(self, target: Target) -> float:
        return target.interval_s if target.interval_s > 0 else self.interval_s

    def _max_inflight(self, target: Target) -> int:
        span = self._interval_for(target)
        return max(2, int(target.effective_budget_s / max(span, 0.05)) + 1)

    def _record(self, probe: Probe) -> None:
        with self._lock:
            self._records.append(probe)
            if self._jsonl is not None:
                self._jsonl.write(json.dumps(probe.as_dict()) + "\n")
                self._jsonl.flush()
            previous = self._last_class.get(probe.target)
            self._last_class[probe.target] = probe.cls
        if self.quiet:
            return
        changed = previous != probe.cls
        if self.verbose or changed:
            marker = "->" if changed and previous else "  "
            status = probe.status if probe.status is not None else "-"
            line = (
                f"{probe.start_wall}  {probe.target:<10} {marker} {probe.cls:<15} "
                f"http={status:<5} {probe.total_ms:8.1f} ms"
            )
            if probe.detail:
                line += f"  {probe.detail[:90]}"
            print(line, file=sys.stderr, flush=True)

    def _worker(self, target: Target, seq: int, scheduled: float) -> None:
        try:
            probe = probe_once(target, seq, scheduled, self.insecure)
        except Exception as exc:  # noqa: BLE001 — a probe must never kill the run
            now = time.monotonic()
            probe = Probe(
                seq=seq,
                target=target.name,
                scheduled_mono=scheduled,
                start_mono=now,
                end_mono=now,
                start_wall=_now_wall(),
                end_wall=_now_wall(),
                cls="error",
                served=False,
                detail=f"probe raised {type(exc).__name__}: {exc}",
            )
        self._record(probe)
        with self._lock:
            self._inflight[target.name] -= 1

    def _schedule(self, target: Target, pool: ThreadPoolExecutor) -> None:
        span = self._interval_for(target)
        tick = 0
        while not self.stop_event.is_set():
            scheduled = self.t0_mono + tick * span
            wait = scheduled - time.monotonic()
            if wait > 0 and self.stop_event.wait(wait):
                return
            tick += 1
            with self._lock:
                if self.max_probes and self._seq >= self.max_probes:
                    self.stopped_by = "max_probes"
                    self.stop_event.set()
                    return
                if self._inflight[target.name] >= self._max_inflight(target):
                    self._skipped[target.name] += 1
                    continue
                self._seq += 1
                seq = self._seq
                self._inflight[target.name] += 1
            pool.submit(self._worker, target, seq, scheduled)

    # -- entry point ------------------------------------------------------
    def run(self) -> Dict[str, Any]:
        workers = min(64, sum(self._max_inflight(t) for t in self.targets) + 2)
        self.t0_mono = time.monotonic()
        self.t0_wall = _now_wall()
        threads: List[threading.Thread] = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="probe") as pool:
            for target in self.targets:
                thread = threading.Thread(
                    target=self._schedule, args=(target, pool), daemon=True
                )
                thread.start()
                threads.append(thread)
            if self.duration_s > 0:
                if self.stop_event.wait(self.duration_s):
                    pass
                else:
                    self.stop_event.set()
            else:
                while not self.stop_event.wait(0.25):
                    pass
            for thread in threads:
                thread.join(timeout=5.0)
        self.t1_mono = time.monotonic()
        self.t1_wall = _now_wall()
        if self._jsonl is not None:
            self._jsonl.close()
        return self.report()

    def request_stop(self, reason: str) -> None:
        self.stopped_by = reason
        self.stop_event.set()

    def report(self) -> Dict[str, Any]:
        records = sorted(self._records, key=lambda p: (p.scheduled_mono, p.seq))
        return build_report(
            targets=self.targets,
            records=records,
            interval_s=self.interval_s,
            t0_mono=self.t0_mono,
            t1_mono=self.t1_mono,
            t0_wall=self.t0_wall,
            t1_wall=self.t1_wall,
            stopped_by=self.stopped_by,
            skipped=dict(self._skipped),
        )


# ---------------------------------------------------------------------------
# Analysis — the part that must not overstate what was measured
# ---------------------------------------------------------------------------


def _percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile; ``None`` for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(pct / 100.0 * len(ordered) + 0.5)) - 1))
    return round(ordered[index], 3)


def _wall_clock(t0_wall: str, t0_mono: float):
    base = datetime.fromisoformat(t0_wall.replace("Z", "+00:00"))

    def at(mono: float) -> str:
        stamp = base.timestamp() + (mono - t0_mono)
        return (
            datetime.fromtimestamp(stamp, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    return at


def _unavailable_intervals(
    records: Sequence[Probe], t0_mono: float, t1_mono: float, wall_at
) -> List[Dict[str, Any]]:
    """Maximal runs of probes that were not served, reported two ways.

    ``observed_*``  first failing probe start → last failing probe end.
                    Positive evidence of failure: a LOWER bound.
    ``bracket_*``   end of the last served request → start of the next served
                    request. Outside this window the service is PROVEN to have
                    been working: an UPPER bound.

    ``open_start``/``open_end`` mark an outage that was already under way when
    the run began, or had not ended when it stopped — the case where the tool
    must not pretend to know the edge.
    """
    intervals: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    last_served_end: Optional[float] = None
    for probe in records:
        if probe.served:
            if current is not None:
                current["bracket_end_mono"] = probe.start_mono
                current["open_end"] = False
                intervals.append(current)
                current = None
            last_served_end = probe.end_mono
        else:
            if current is None:
                current = {
                    "observed_start_mono": probe.start_mono,
                    "observed_end_mono": probe.end_mono,
                    "bracket_start_mono": last_served_end
                    if last_served_end is not None
                    else t0_mono,
                    "open_start": last_served_end is None,
                    "probes": 0,
                    "classes": {},
                    "statuses": {},
                }
            current["observed_end_mono"] = probe.end_mono
            current["probes"] += 1
            current["classes"][probe.cls] = current["classes"].get(probe.cls, 0) + 1
            if probe.status is not None:
                key = str(probe.status)
                current["statuses"][key] = current["statuses"].get(key, 0) + 1
    if current is not None:
        current["bracket_end_mono"] = t1_mono
        current["open_end"] = True
        intervals.append(current)

    out: List[Dict[str, Any]] = []
    for item in intervals:
        observed = item["observed_end_mono"] - item["observed_start_mono"]
        bracket = item["bracket_end_mono"] - item["bracket_start_mono"]
        out.append(
            {
                "observed_start": wall_at(item["observed_start_mono"]),
                "observed_end": wall_at(item["observed_end_mono"]),
                "observed_duration_s": round(observed, 3),
                "bracket_start": wall_at(item["bracket_start_mono"]),
                "bracket_end": wall_at(item["bracket_end_mono"]),
                "bracket_duration_s": round(bracket, 3),
                "open_start": item["open_start"],
                "open_end": item["open_end"],
                "probes": item["probes"],
                "classes": item["classes"],
                "statuses": item["statuses"],
            }
        )
    return out


def _longest_unproven_window(
    records: Sequence[Probe], t0_mono: float, t1_mono: float, wall_at
) -> Dict[str, Any]:
    """The longest stretch with NO positive proof that the service worked.

    Sentinels at both ends of the run: a probe that has not happened yet
    proves nothing either. In a run with no failures this is approximately one
    sampling interval, which is the honest floor of the measurement.
    """
    best = 0.0
    best_span = (t0_mono, t0_mono)
    previous_end = t0_mono
    for probe in records:
        if not probe.served:
            continue
        gap = probe.start_mono - previous_end
        if gap > best:
            best, best_span = gap, (previous_end, probe.start_mono)
        previous_end = probe.end_mono
    gap = t1_mono - previous_end
    if gap > best:
        best, best_span = gap, (previous_end, t1_mono)
    return {
        "seconds": round(best, 3),
        "from": wall_at(best_span[0]),
        "to": wall_at(best_span[1]),
    }


def build_report(
    *,
    targets: Sequence[Target],
    records: Sequence[Probe],
    interval_s: float,
    t0_mono: float,
    t1_mono: float,
    t0_wall: str,
    t1_wall: str,
    stopped_by: str,
    skipped: Dict[str, int],
    note: str = "",
) -> Dict[str, Any]:
    wall_at = _wall_clock(t0_wall, t0_mono)
    by_target: Dict[str, Any] = {}
    all_intervals: List[Dict[str, Any]] = []
    for target in targets:
        mine = [p for p in records if p.target == target.name]
        served = [p for p in mine if p.served]
        latencies = [p.total_ms for p in served]
        connects = [p.connect_ms for p in served if p.connect_ms is not None]
        by_class = {cls: 0 for cls in CLASSES}
        for probe in mine:
            by_class[probe.cls] = by_class.get(probe.cls, 0) + 1
        intervals = _unavailable_intervals(mine, t0_mono, t1_mono, wall_at)
        for item in intervals:
            all_intervals.append(dict(item, target=target.name))
        starts = [p.start_mono for p in mine]
        gaps = [b - a for a, b in zip(starts, starts[1:])] if len(starts) > 1 else []
        json_values: Dict[str, Dict[str, int]] = {}
        for probe in mine:
            for key, value in (probe.json_fields or {}).items():
                bucket = json_values.setdefault(key, {})
                bucket[str(value)] = bucket.get(str(value), 0) + 1
        entry: Dict[str, Any] = {
            "target": target.as_dict(),
            "probes": len(mine),
            "served": len(served),
            "unavailable": len(mine) - len(served),
            "availability_pct": round(100.0 * len(served) / len(mine), 4) if mine else None,
            "by_class": {k: v for k, v in by_class.items() if v},
            "latency_ms": {
                "p50": _percentile(latencies, 50),
                "p95": _percentile(latencies, 95),
                "max": round(max(latencies), 3) if latencies else None,
            },
            "connect_ms": {"p50": _percentile(connects, 50), "max": round(max(connects), 3) if connects else None},
            "longest_unproven_window": _longest_unproven_window(mine, t0_mono, t1_mono, wall_at),
            "unavailable_intervals": intervals,
            "sampling": {
                "nominal_interval_s": target.interval_s or interval_s,
                "max_sample_gap_s": round(max(gaps), 3) if gaps else None,
                "max_schedule_drift_ms": round(max((p.drift_ms for p in mine), default=0.0), 3),
                "skipped_ticks": skipped.get(target.name, 0),
            },
        }
        if json_values:
            entry["response_fields"] = json_values
        if target.kind == "stream":
            streams = [p.stream for p in mine if p.stream]
            entry["stream"] = {
                "opened": sum(1 for s in streams if s.get("opened")),
                "completed": sum(1 for s in streams if s.get("completed")),
                "not_completed": sum(1 for s in streams if not s.get("completed")),
                "stopped_by": _counter(s.get("stopped_by", "?") for s in streams),
                "terminal_events": _counter(
                    s["terminal_event"] for s in streams if s.get("terminal_event")
                ),
                "frames_p50": _percentile([float(s.get("frames", 0)) for s in streams], 50),
                "max_frame_gap_ms": max(
                    (float(s["max_frame_gap_ms"]) for s in streams if s.get("max_frame_gap_ms")),
                    default=None,
                ),
            }
        by_target[target.name] = entry

    total = len(records)
    served_total = sum(1 for p in records if p.served)
    return {
        "tool": TOOL_NAME,
        "schema_version": SCHEMA_VERSION,
        "run": {
            "started_at": t0_wall,
            "ended_at": t1_wall,
            "duration_s": round(t1_mono - t0_mono, 3),
            "interval_s": interval_s,
            "stopped_by": stopped_by,
            "note": note,
            "probe_host": socket.gethostname(),
            "python": sys.version.split()[0],
        },
        "summary": {
            "probes": total,
            "served": served_total,
            "unavailable": total - served_total,
            "availability_pct": round(100.0 * served_total / total, 4) if total else None,
            "by_class": _counter(p.cls for p in records),
            "unavailable_intervals": sorted(
                all_intervals, key=lambda i: i["observed_start"]
            ),
            "by_target": by_target,
        },
        "timeline": [p.as_dict() for p in records],
    }


def _counter(values) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for value in values:
        key = str(value)
        out[key] = out.get(key, 0) + 1
    return out


# ---------------------------------------------------------------------------
# Human summary
# ---------------------------------------------------------------------------


def format_summary(report: Dict[str, Any]) -> str:
    run = report["run"]
    summary = report["summary"]
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("AVAILABILITY FROM REQUEST EVIDENCE")
    lines.append("=" * 78)
    lines.append(f"window        {run['started_at']} -> {run['ended_at']}")
    lines.append(
        f"duration      {run['duration_s']:.1f}s   interval {run['interval_s']}s   "
        f"stopped by {run['stopped_by']}"
    )
    if run.get("note"):
        lines.append(f"note          {run['note']}")
    availability = summary["availability_pct"]
    lines.append(
        f"probes        {summary['probes']} total   served {summary['served']}   "
        f"unavailable {summary['unavailable']}   "
        f"availability {availability if availability is not None else 'n/a'}%"
    )
    lines.append(f"outcomes      {summary['by_class'] or '{}'}")
    lines.append("")
    for name, entry in summary["by_target"].items():
        target = entry["target"]
        lines.append(f"--- {name}  {target['url']}  ({target['kind']}, expect {target['expect']})")
        lines.append(
            f"    probes {entry['probes']:>5}   served {entry['served']:>5}   "
            f"unavailable {entry['unavailable']:>4}   "
            f"availability {entry['availability_pct']}%"
        )
        lines.append(f"    classes  {entry['by_class']}")
        latency = entry["latency_ms"]
        lines.append(
            f"    latency  p50 {latency['p50']} ms   p95 {latency['p95']} ms   "
            f"max {latency['max']} ms   (connect p50 {entry['connect_ms']['p50']} ms)"
        )
        if entry.get("response_fields"):
            lines.append(f"    body     {entry['response_fields']}")
        if entry.get("stream"):
            stream = entry["stream"]
            lines.append(
                f"    stream   opened {stream['opened']}   completed {stream['completed']}   "
                f"not completed {stream['not_completed']}   "
                f"ended by {stream['stopped_by']}"
            )
            if stream.get("max_frame_gap_ms"):
                lines.append(f"             largest gap between frames {stream['max_frame_gap_ms']} ms")
        sampling = entry["sampling"]
        lines.append(
            f"    sampling nominal {sampling['nominal_interval_s']}s   "
            f"widest actual gap {sampling['max_sample_gap_s']}s   "
            f"max drift {sampling['max_schedule_drift_ms']} ms   "
            f"skipped ticks {sampling['skipped_ticks']}"
        )
        window = entry["longest_unproven_window"]
        lines.append(
            f"    longest window with no proof of service: {window['seconds']}s "
            f"({window['from']} -> {window['to']})"
        )
        if not entry["unavailable_intervals"]:
            lines.append("    UNAVAILABLE INTERVALS: none observed")
        else:
            lines.append("    UNAVAILABLE INTERVALS:")
            for item in entry["unavailable_intervals"]:
                edge = []
                if item["open_start"]:
                    edge.append("already unavailable when the run started")
                if item["open_end"]:
                    edge.append("still unavailable when the run stopped")
                lines.append(
                    f"      observed  {item['observed_start']} -> {item['observed_end']}"
                    f"  ({item['observed_duration_s']}s, {item['probes']} probes, "
                    f"{item['classes']})"
                )
                lines.append(
                    f"      bracketed {item['bracket_start']} -> {item['bracket_end']}"
                    f"  ({item['bracket_duration_s']}s: last request served -> next request served)"
                )
                if item["statuses"]:
                    lines.append(f"      statuses  {item['statuses']}")
                if edge:
                    lines.append(f"      note      {'; '.join(edge)}")
        lines.append("")
    lines.append(
        "READ THIS AS: an outage is at least the observed window and at most the"
    )
    lines.append(
        "bracketed one; nothing shorter than the sampling interval is resolvable,"
    )
    lines.append(
        "so '100% available' means 'every request in this sample was served', not"
    )
    lines.append("'no request could have failed between two samples'.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Self-test — proof that the classifier can SEE an outage
# ---------------------------------------------------------------------------
#
# A probe that reports "100% available" against a healthy service proves
# nothing about the probe: a tool that always says 100% would pass that test
# too. This mode stands up a local fixture on an ephemeral port, drives every
# outcome class through it, then kills and replaces the fixture mid-run and
# checks that the reconstructed outage window brackets the real one. It needs
# no network, no database, no model server and no pytest, so it can be re-run
# on any machine before the tool's output is believed.


def _fixture_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # noqa: ANN002 - silence the fixture
            pass

        def _body(self, status: int, payload: bytes, ctype: str = "application/json"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _chunk(self, payload: bytes) -> None:
            self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
            self.wfile.flush()

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
            path = self.path.split("?")[0]
            if path == "/ok":
                self._body(200, b'{"status": "ok"}')
            elif path == "/degraded":
                self._body(200, b'{"status": "degraded"}')
            elif path == "/slow":
                time.sleep(0.4)
                self._body(200, b'{"status": "ok"}')
            elif path == "/err":
                self._body(503, b"upstream unavailable", "text/plain")
            elif path == "/stall":
                time.sleep(10)  # connected, never answers
            elif path == "/sse":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self._chunk(b'event: token\ndata: {"text": "x"}\n\n')
                self._chunk(b'event: done\ndata: {"session_id": "fixture"}\n\n')
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            elif path == "/sse-abort":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self._chunk(b'event: token\ndata: {"text": "x"}\n\n')
                self.close_connection = True  # die mid-stream: no `done`, no last chunk
                try:
                    self.connection.close()
                except OSError:
                    pass
            else:
                self._body(404, b"not found", "text/plain")

    return Handler


def _serve(port: int = 0):
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", port), _fixture_handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def self_test() -> int:
    """Drive every outcome class through a local fixture. Returns an exit code."""
    failures: List[str] = []

    def check(label: str, got: Any, want: Any) -> None:
        ok = got == want
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:<52} {got!r}")
        if not ok:
            failures.append(f"{label}: got {got!r}, want {want!r}")

    server, _thread = _serve()
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    print(f"fixture on {base}")
    print("outcome classes:")
    defaults: Dict[str, Any] = {}
    cases = [
        ("url=%s/ok,name=t,json_keys=status" % base, "ok", "served"),
        ("url=%s/slow,name=t,slow_ms=100" % base, "slow", "served"),
        ("url=%s/err,name=t" % base, "error", "unavailable"),
        ("url=%s/stall,name=t,timeout=1" % base, "timeout", "unavailable"),
        ("url=%s/sse,name=t,kind=stream,timeout=3" % base, "ok", "served"),
        ("url=%s/sse-abort,name=t,kind=stream,timeout=3" % base, "truncated", "unavailable"),
    ]
    for spec, want_class, _ in cases:
        target = parse_target(spec, defaults)
        probe = probe_once(target, 1, time.monotonic(), False)
        check(f"{urlsplit(target.url).path:<12} {target.kind:<6} -> {want_class}", probe.cls, want_class)

    # The body whitelist: a 200 that says "degraded" is still served, and the
    # app's own opinion is recorded without keeping the body.
    probe = probe_once(parse_target(f"url={base}/degraded,name=t,json_keys=status", defaults), 1, time.monotonic(), False)
    check("/degraded    json whitelist captures status", (probe.cls, probe.json_fields), ("ok", {"status": "degraded"}))

    # A stream that opens with 200 and dies is NOT a success, however good the
    # status line looked — the case a status-code-only probe scores as served.
    probe = probe_once(parse_target(f"url={base}/sse-abort,name=t,kind=stream,timeout=3", defaults), 1, time.monotonic(), False)
    check("/sse-abort   http 200 but stream not completed", (probe.status, (probe.stream or {}).get("completed")), (200, False))

    # Refused: a port nobody is listening on.
    spare = socket.socket()
    spare.bind(("127.0.0.1", 0))
    dead_port = spare.getsockname()[1]
    spare.close()
    probe = probe_once(parse_target(f"url=http://127.0.0.1:{dead_port}/ok,name=t", defaults), 1, time.monotonic(), False)
    check("closed port  -> refused", probe.cls, "refused")

    print("safety guards:")
    for bad in (f"{base}/chat", f"{base}/v1/chat/completions", f"{base}/v1/embeddings", f"{base}/score", "ftp://example.com/x"):
        try:
            parse_target(f"url={bad},name=t", defaults)
            check(f"refuses {bad}", "accepted", "refused")
        except TargetError:
            check(f"refuses {bad}", "refused", "refused")

    print("outage reconstruction (fixture killed and replaced mid-run):")
    targets = [parse_target(f"url={base}/ok,name=ok,timeout=1", defaults)]
    runner = Runner(targets, interval_s=0.2, duration_s=4.0, quiet=True)
    truth: Dict[str, float] = {}

    def chaos() -> None:
        time.sleep(1.2)
        server.shutdown()
        server.server_close()
        truth["down"] = time.monotonic()
        time.sleep(1.6)
        again, _t = _serve(port)
        truth["up"] = time.monotonic()
        truth["server"] = again  # type: ignore[assignment]

    breaker = threading.Thread(target=chaos, daemon=True)
    breaker.start()
    report = runner.run()
    breaker.join(timeout=5)
    replacement = truth.get("server")
    if replacement is not None:
        replacement.shutdown()  # type: ignore[union-attr]
        replacement.server_close()  # type: ignore[union-attr]

    entry = report["summary"]["by_target"]["ok"]
    intervals = entry["unavailable_intervals"]
    check("exactly one unavailable interval", len(intervals), 1)
    if intervals:
        item = intervals[0]
        # The class MIX is the point, not one label. A request that was
        # already in flight when the listener went away comes back
        # `truncated` (its connection died mid-response); every request after
        # that is `refused`. Both are unavailability, and telling them apart
        # is what distinguishes "the service was replaced" from "the service
        # never answered", which is the whole reason the taxonomy is closed.
        classes = set(item["classes"])
        check("every probe in the window is unavailability", classes - set(CLASSES), set())
        check("the window contains refusals", "refused" in classes, True)
        check("served classes never appear in an outage", classes & set(SERVED_CLASSES), set())
        check("observed window <= bracketed window", item["observed_duration_s"] <= item["bracket_duration_s"], True)
        real = truth.get("up", 0.0) - truth.get("down", 0.0)
        print(f"        real outage {real:.3f}s   observed {item['observed_duration_s']}s   bracketed {item['bracket_duration_s']}s")
        check("observed is a lower bound on the real outage", item["observed_duration_s"] <= real + 0.05, True)
        check("bracket is an upper bound on the real outage", item["bracket_duration_s"] >= real - 0.05, True)
        check("availability is not 100%", entry["availability_pct"] < 100.0, True)

    print()
    if failures:
        print(f"SELF-TEST FAILED — {len(failures)} check(s):")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("SELF-TEST PASSED — every outcome class reproduced, outage window bracketed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="availability_probe",
        description=(
            "Probe endpoints at a fixed interval across a deploy and report the "
            "exact windows in which requests were not served."
        ),
        epilog=(
            "examples:\n"
            "  # across a deploy, from the HOST (never inside the container being recreated)\n"
            "  python3 tools/availability_probe.py --preset orchestrator --duration 600 \\\n"
            "      --out /tmp/deploy.json --note 'orchestrator recreate'\n"
            "  # one endpoint, one minute, 1 Hz\n"
            "  python3 tools/availability_probe.py --url http://127.0.0.1:8080/health \\\n"
            "      --interval 1 --duration 60\n"
            "  # an SSE endpoint: does the stream OPEN, and does it COMPLETE?\n"
            "  python3 tools/availability_probe.py \\\n"
            "      --target url=http://127.0.0.1:8080/events,kind=stream,expect=200\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", action="append", default=[], help="probe this URL (repeatable)")
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="full spec: url=..,name=..,kind=http|stream,expect=200|404,interval=..,"
        "timeout=..,slow_ms=..,json_keys=status (repeatable)",
    )
    parser.add_argument(
        "--preset",
        choices=("orchestrator",),
        help="the three auth-free orchestrator probes: a 404 route, /metrics and /health",
    )
    parser.add_argument(
        "--base", default="http://127.0.0.1:8080", help="base URL for --preset"
    )
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between probes")
    parser.add_argument(
        "--health-interval",
        type=float,
        default=0.0,
        help="separate interval for the preset's /health probe (default: 3x --interval, "
        "because /health fans out to the reranker GPU service)",
    )
    parser.add_argument(
        "--no-health",
        action="store_true",
        help="drop /health from --preset entirely (use while someone is measuring the GPU)",
    )
    parser.add_argument(
        "--duration", type=float, default=60.0, help="seconds to run; 0 = until interrupted"
    )
    parser.add_argument("--max-probes", type=int, default=0, help="stop after N probes")
    parser.add_argument("--timeout", type=float, default=None, help="per-probe read timeout")
    parser.add_argument("--connect-timeout", type=float, default=None)
    parser.add_argument(
        "--slow-ms",
        type=float,
        default=None,
        help="served-but-slow threshold in milliseconds",
    )
    parser.add_argument("--out", default="", help="JSON report path (default: ./availability-<UTC>.json)")
    parser.add_argument("--jsonl", default="", help="append every probe here as it completes")
    parser.add_argument("--note", default="", help="free-text label recorded in the report")
    parser.add_argument("--insecure", action="store_true", help="do not verify TLS certificates")
    parser.add_argument("--verbose", action="store_true", help="print every probe")
    parser.add_argument("--quiet", action="store_true", help="print nothing until the summary")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="prove the classifier on a local fixture (every outcome class, plus a "
        "killed-and-replaced service whose outage window must be bracketed). No "
        "network, no database, no model server.",
    )
    return parser


def collect_targets(args: argparse.Namespace) -> List[Target]:
    defaults = {
        "timeout_s": args.timeout,
        "connect_timeout_s": args.connect_timeout,
        "slow_ms": args.slow_ms,
    }
    defaults = {k: v for k, v in defaults.items() if v is not None}
    targets: List[Target] = []
    if args.preset == "orchestrator":
        health_interval = args.health_interval or (args.interval * 3.0)
        targets.extend(
            preset_targets(
                args.base,
                defaults,
                health_interval_s=health_interval,
                with_health=not args.no_health,
            )
        )
    for url in args.url:
        targets.append(parse_target(f"url={url}", defaults))
    for spec in args.target:
        targets.append(parse_target(spec, defaults))
    if not targets:
        raise TargetError("nothing to probe: pass --preset, --url or --target")
    seen = set()
    for target in targets:
        if target.name in seen:
            raise TargetError(f"duplicate target name {target.name!r} — give one a name=")
        seen.add(target.name)
    return targets


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    try:
        targets = collect_targets(args)
    except TargetError as exc:
        parser.error(str(exc))
        return 1
    out_path = args.out or datetime.now(timezone.utc).strftime(
        "availability-%Y%m%dT%H%M%SZ.json"
    )
    runner = Runner(
        targets,
        interval_s=args.interval,
        duration_s=args.duration,
        insecure=args.insecure,
        jsonl_path=args.jsonl,
        verbose=args.verbose,
        quiet=args.quiet,
        max_probes=args.max_probes,
    )

    def _stop(signum, _frame):  # noqa: ANN001 - signal handler signature
        print(
            f"\n[{_now_wall()}] signal {signum}: stopping, writing what was measured",
            file=sys.stderr,
            flush=True,
        )
        runner.request_stop("signal")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):  # pragma: no cover - not the main thread
            pass

    if not args.quiet:
        print(
            f"[{_now_wall()}] probing {len(targets)} target(s) every "
            f"{args.interval}s for {args.duration or float('inf')}s "
            f"-> {out_path}",
            file=sys.stderr,
            flush=True,
        )
        for target in targets:
            print(
                f"    {target.name:<10} {target.url}  ({target.kind}, expect "
                f"{list(target.expect)}, every "
                f"{target.interval_s or args.interval}s)",
                file=sys.stderr,
                flush=True,
            )

    report = runner.run()
    report["run"]["note"] = args.note
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(format_summary(report))
    print(f"\ntimeline: {out_path} ({len(report['timeline'])} probes)")
    return 2 if report["summary"]["unavailable"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
