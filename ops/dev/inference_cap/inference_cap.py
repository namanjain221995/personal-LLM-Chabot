#!/usr/bin/env python3
"""Inference cap: hold the dev stack to at most two in-flight requests to the production engines.

The dev copy of the application reuses the production model engines (vLLM, OpenAI-compatible
HTTP). The programme allows the dev stack at most two in-flight engine requests in total, at the
lowest priority. This reverse proxy is that cap, enforced in code: the dev orchestrator's engine
base URLs point here (for example http://inference-cap:9100/main/v1) and every request that can
make an engine do work waits for one of at most two global slots.

Standard library only (Python 3.11). Configuration comes from the environment and is validated at
start; see parse_config(). Tests build a server from a config mapping with parse_config() and
build_server() without touching the process environment or calling main().

Slots: one threading.BoundedSemaphore(CAP_MAX_INFLIGHT) shared by every upstream. GET and HEAD
(metadata: /v1/models, /health, /version, /metrics) skip it. A slot is taken before the upstream
connection opens and released exactly once (try/finally) when the response has been relayed, the
client has gone, the upstream failed or timed out, or anything raised. While a request holds a
slot a watcher notices the client hanging up and shuts the upstream connection, so an abandoned
request stops occupying the engine and the slot. A request still queued for a slot is dropped if
its client hangs up.

Logging: one line per request to stderr with the upstream name, method, path without the query,
status, wait_ms, duration_ms and bytes out. Bodies, query strings and header values are never
logged; /_cap/stats lists upstream names, never their URLs.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import select
import signal
import socket
import socketserver
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Mapping, Optional
from urllib.parse import unquote, urlsplit

HARD_MAX_INFLIGHT = 2  # the programme's limit; CAP_MAX_INFLIGHT may lower it, never raise it
MIN_READ_TIMEOUT_S = 4200.0  # the application's generation wall clock; the read timeout must exceed it
DRAIN_TIMEOUT_S = 30.0
RETRY_AFTER_S = 30
CLIENT_IDLE_TIMEOUT_S = 600
RELAY_CHUNK = 65536
QUEUE_POLL_S = 0.25
WATCH_POLL_S = 0.25
LINGER_S = 2.0
LINGER_MAX_BYTES = 16 * 1024 * 1024

DEFAULTS = {
    "CAP_LISTEN": "0.0.0.0:9100",
    "CAP_MAX_INFLIGHT": "2",
    "CAP_QUEUE_TIMEOUT_S": "900",
    "CAP_CONNECT_TIMEOUT_S": "10",
    "CAP_READ_TIMEOUT_S": "4500",
    "CAP_MAX_BODY_BYTES": "268435456",
}

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
_HOST_RE = re.compile(r"[a-z0-9._-]+|[0-9a-f:.]+")
_BASE_PATH_RE = re.compile(r"[A-Za-z0-9\-._~!$&'()*+,;=:@%/]*")
_TOKEN_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_DIGITS_RE = re.compile(r"[0-9]+")
_CTL_RE = re.compile(r"[\x00-\x20\x7f]")
_FOLD_RE = re.compile(r"\r?\n[ \t]+")

HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-connection", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
})
# Never copied from the client: the cap sets Host and Content-Length itself and has already
# answered any Expect: 100-continue.
_REQUEST_ONLY_DROP = frozenset({"host", "content-length", "expect"})
SLOT_FREE_METHODS = frozenset({"GET", "HEAD"})
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

GRANTED, TIMED_OUT, ABANDONED = "granted", "timed_out", "abandoned"


# --------------------------------------------------------------------------- configuration

class ConfigError(ValueError):
    """A configuration value is missing or not allowed; the message is one line."""


@dataclass(frozen=True)
class Upstream:
    name: str
    host: str
    port: int
    base_path: str  # "" or "/something" without a trailing slash

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == 80 else f"{host}:{self.port}"


@dataclass(frozen=True)
class Config:
    upstreams: dict  # name -> Upstream, in configuration order
    listen_host: str = "0.0.0.0"
    listen_port: int = 9100
    max_inflight: int = 2
    queue_timeout_s: float = 900.0
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 4500.0
    max_body_bytes: int = 268435456


def _setting(env: Mapping[str, str], key: str) -> str:
    value = env.get(key)
    if value is None or not str(value).strip():
        return DEFAULTS[key]
    return str(value).strip()


def _parse_listen(value: str) -> tuple[str, int]:
    host, sep, port_text = value.rpartition(":")
    if not sep or not host or not _DIGITS_RE.fullmatch(port_text):
        raise ConfigError("CAP_LISTEN must be host:port (for example 0.0.0.0:9100)")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    port = int(port_text)
    if not host or port > 65535:
        raise ConfigError("CAP_LISTEN must be host:port with a port from 0 to 65535")
    return host, port


def _parse_int(env: Mapping[str, str], key: str, lo: int, hi: Optional[int], why: str = "") -> int:
    text = _setting(env, key)
    if not _DIGITS_RE.fullmatch(text):
        raise ConfigError(f"{key} must be a whole number{why}")
    value = int(text)
    if value < lo or (hi is not None and value > hi):
        bound = f"from {lo} to {hi}" if hi is not None else f"at least {lo}"
        raise ConfigError(f"{key} must be {bound}{why}")
    return value


def _parse_seconds(env: Mapping[str, str], key: str, above: float, why: str = "") -> float:
    text = _setting(env, key)
    try:
        value = float(text)
    except ValueError:
        raise ConfigError(f"{key} must be a number of seconds") from None
    if not math.isfinite(value) or value <= above:
        raise ConfigError(f"{key} must be a number of seconds above {above:g}{why}")
    return value


def _parse_upstream_url(index: int, name: str, url: str) -> Upstream:
    where = f"CAP_UPSTREAMS entry {index} ({name})"
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ConfigError(f"{where}: not a valid URL") from None
    if parts.scheme != "http" or not url.lower().startswith("http://"):
        raise ConfigError(f"{where}: only http:// upstreams are supported")
    if parts.username is not None or parts.password is not None:
        raise ConfigError(f"{where}: the URL must not carry credentials")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise ConfigError(f"{where}: the URL must not carry a query or fragment")
    host = parts.hostname or ""
    if not host or not _HOST_RE.fullmatch(host):
        raise ConfigError(f"{where}: the URL has no valid host")
    try:
        port = parts.port
    except ValueError:
        raise ConfigError(f"{where}: the URL has an invalid port") from None
    if port is None:
        port = 80
    if not 1 <= port <= 65535:
        raise ConfigError(f"{where}: the URL has an invalid port")
    base = parts.path.rstrip("/")
    if not _BASE_PATH_RE.fullmatch(base):
        raise ConfigError(f"{where}: the base path has characters that are not allowed")
    for segment in base.split("/")[1:]:
        if _decoded_fully(segment) in (".", ".."):
            raise ConfigError(f"{where}: the base path must not contain . or .. segments")
    return Upstream(name=name, host=host, port=port, base_path=base)


def parse_upstreams(raw: Optional[str]) -> dict:
    if raw is None or not raw.strip():
        raise ConfigError("CAP_UPSTREAMS is required: comma-separated name=http://host:port[/base-path]")
    upstreams: dict = {}
    for index, item in enumerate(raw.split(","), 1):
        item = item.strip()
        if not item:
            raise ConfigError(f"CAP_UPSTREAMS entry {index} is empty")
        name, sep, url = item.partition("=")
        name, url = name.strip(), url.strip()
        if not sep or not url:
            raise ConfigError(f"CAP_UPSTREAMS entry {index} must be name=http://host:port[/base-path]")
        if not _NAME_RE.fullmatch(name):
            raise ConfigError(f"CAP_UPSTREAMS entry {index}: the name must match ^[a-z0-9][a-z0-9-]*$")
        if name in upstreams:
            raise ConfigError(f"CAP_UPSTREAMS: the name {name!r} appears more than once")
        upstreams[name] = _parse_upstream_url(index, name, url)
    return upstreams


def parse_config(env: Mapping[str, str]) -> Config:
    """Build a validated Config from an environment-like mapping; raise ConfigError on bad values."""
    listen_host, listen_port = _parse_listen(_setting(env, "CAP_LISTEN"))
    upstreams = parse_upstreams(env.get("CAP_UPSTREAMS"))
    max_inflight = _parse_int(
        env, "CAP_MAX_INFLIGHT", 1, HARD_MAX_INFLIGHT,
        " (the dev stack's cap on production engines; it cannot be raised)")
    return Config(
        upstreams=upstreams,
        listen_host=listen_host,
        listen_port=listen_port,
        max_inflight=max_inflight,
        queue_timeout_s=_parse_seconds(env, "CAP_QUEUE_TIMEOUT_S", 0.0),
        connect_timeout_s=_parse_seconds(env, "CAP_CONNECT_TIMEOUT_S", 0.0),
        read_timeout_s=_parse_seconds(
            env, "CAP_READ_TIMEOUT_S", MIN_READ_TIMEOUT_S,
            " (it must outlast the application's generation wall clock)"),
        max_body_bytes=_parse_int(env, "CAP_MAX_BODY_BYTES", 1, None),
    )


# --------------------------------------------------------------------------- paths

class PathRefused(ValueError):
    pass


def _decoded_fully(segment: str) -> str:
    """Percent-decode until stable (bounded), so %2e%2e and %252e%252e are both seen as '..'."""
    for _ in range(4):
        decoded = unquote(segment)
        if decoded == segment:
            break
        segment = decoded
    return segment


def split_target(raw_path: str) -> tuple[str, str]:
    """Split '/<name>/<rest>' into (name, '/<rest>' or ''). Refuse anything that could escape the
    name prefix: dot segments (raw or percent-encoded), encoded slashes and backslashes."""
    if not raw_path.startswith("/"):
        raise PathRefused("the request target must be an absolute path")
    if _CTL_RE.search(raw_path):
        raise PathRefused("the request path has control characters")
    segments = raw_path[1:].split("/")
    for segment in segments:
        decoded = _decoded_fully(segment)
        if decoded in (".", "..") or "/" in decoded or "\\" in decoded or "\x00" in decoded:
            raise PathRefused("dot segments, encoded slashes and backslashes are not allowed in the path")
    name = segments[0]
    rest = "/" + "/".join(segments[1:]) if len(segments) > 1 else ""
    return name, rest


def _loggable_path(raw_path: str) -> str:
    clipped = "".join(c if 0x21 <= ord(c) < 0x7F else "?" for c in raw_path[:200])
    return clipped + ("..." if len(raw_path) > 200 else "") if clipped else "-"


# --------------------------------------------------------------------------- slots

class SlotGate:
    """The global cap: one BoundedSemaphore for every upstream, plus counters for /_cap/stats."""

    def __init__(self, limit: int):
        self.limit = limit
        self._sem = threading.BoundedSemaphore(limit)
        self._lock = threading.Lock()
        self.inflight = 0
        self.queued = 0
        self.peak = 0

    def _granted(self) -> str:
        with self._lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        return GRANTED

    def acquire(self, timeout: float, should_abandon: Optional[Callable[[], bool]] = None,
                poll_s: float = QUEUE_POLL_S) -> str:
        """Wait up to `timeout` seconds for a slot. Returns GRANTED, TIMED_OUT or ABANDONED (when
        should_abandon() turned true while queued, e.g. the client hung up)."""
        if self._sem.acquire(blocking=False):
            return self._granted()
        deadline = time.monotonic() + timeout
        with self._lock:
            self.queued += 1
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return TIMED_OUT
                if self._sem.acquire(timeout=min(remaining, poll_s)):
                    return self._granted()
                if should_abandon is not None and should_abandon():
                    return ABANDONED
        finally:
            with self._lock:
                self.queued -= 1

    def release(self) -> None:
        with self._lock:
            self.inflight -= 1
        self._sem.release()


# --------------------------------------------------------------------------- client liveness

def _peer_closed(sock: socket.socket) -> bool:
    """True when the client has hung up (EOF or error); never consumes request bytes."""
    try:
        poller = select.poll()
        poller.register(sock, select.POLLIN | select.POLLPRI)
        events = poller.poll(0)
        if not events:
            return False
        if events[0][1] & (select.POLLERR | select.POLLNVAL):
            return True
        return sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
    except (BlockingIOError, InterruptedError):
        return False
    except (OSError, ValueError):
        return True


class ClientWatch:
    """While a request is being relayed, notice the client hanging up and shut the upstream socket
    so the blocked relay returns at once (and the engine sees its client go)."""

    def __init__(self, client_sock: socket.socket, poll_s: float = WATCH_POLL_S):
        self._client = client_sock
        self._poll_ms = max(1, int(poll_s * 1000))
        self.gone = threading.Event()
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._upstream: Optional[socket.socket] = None
        self._thread = threading.Thread(target=self._run, name="cap-client-watch", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def attach(self, upstream_sock: Optional[socket.socket]) -> None:
        with self._lock:
            self._upstream = upstream_sock
            if self.gone.is_set():
                self._shutdown_upstream_locked()

    def stop(self) -> None:
        with self._lock:
            self._done.set()
            self._upstream = None

    def _run(self) -> None:
        try:
            poller = select.poll()
            poller.register(self._client, select.POLLIN | select.POLLPRI)
        except (OSError, ValueError):
            return
        while not self._done.is_set():
            try:
                events = poller.poll(self._poll_ms)
            except (OSError, ValueError):
                return
            if self._done.is_set() or not events:
                continue
            if events[0][1] & (select.POLLERR | select.POLLNVAL):
                self._mark_gone()
                return
            try:
                data = self._client.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
            except (BlockingIOError, InterruptedError):
                self._done.wait(0.05)
                continue
            except OSError:
                self._mark_gone()
                return
            if data:
                return  # the client pipelined its next request; nothing more to learn here
            self._mark_gone()
            return

    def _mark_gone(self) -> None:
        with self._lock:
            if self._done.is_set():
                return
            self.gone.set()
            self._shutdown_upstream_locked()

    def _shutdown_upstream_locked(self) -> None:
        if self._upstream is not None:
            try:
                self._upstream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


# --------------------------------------------------------------------------- request handling

class _ClientGone(Exception):
    pass


class _HeaderRefused(ValueError):
    pass


class _Record:
    __slots__ = ("name", "method", "path", "status", "wait_ms", "started", "bytes_out",
                 "outcome", "headers_sent")

    def __init__(self, method: str):
        self.name = "-"
        self.method = method or "-"
        self.path = "-"
        self.status: object = "-"
        self.wait_ms = 0
        self.started = time.monotonic()
        self.bytes_out = 0
        self.outcome = "ok"
        self.headers_sent = False


def _connection_tokens(values) -> set:
    tokens = set()
    for value in values or []:
        tokens.update(t.strip().lower() for t in str(value).split(",") if t.strip())
    return tokens


class CapHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "inference-cap"
    sys_version = ""
    timeout = CLIENT_IDLE_TIMEOUT_S
    disable_nagle_algorithm = True  # each streamed event leaves at once instead of waiting on an ACK

    server: "CapServer"

    # The base class logs request lines, which carry query strings; this proxy logs its own line.
    def log_request(self, code="-", size="-"):
        pass

    def log_message(self, format, *args):
        pass

    def log_error(self, format, *args):
        if str(format).startswith("Request timed out"):
            return  # an idle keep-alive connection timing out is routine
        code = args[0] if args and isinstance(args[0], int) else "-"
        self.server.log(f"protocol_error status={code}")

    def do_GET(self):
        self._dispatch()

    do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_GET

    # ------------------------------------------------------------------ framing

    def _framing_problem(self) -> Optional[tuple]:
        """None when the request body is acceptable, else (status, error type, message)."""
        self._body_length = 0
        if self.headers.get("Transfer-Encoding") is not None:
            return 411, "length_required", "chunked request bodies are not accepted; send Content-Length"
        values = self.headers.get_all("Content-Length") or []
        lengths = set()
        for value in values:
            for part in str(value).split(","):
                part = part.strip()
                if not _DIGITS_RE.fullmatch(part):
                    return 400, "bad_request", "Content-Length is not a whole number"
                lengths.add(int(part))
        if len(lengths) > 1:
            return 400, "bad_request", "conflicting Content-Length headers"
        length = lengths.pop() if lengths else 0
        if length > self.server.config.max_body_bytes:
            return 413, "body_too_large", "the request body exceeds CAP_MAX_BODY_BYTES"
        self._body_length = length
        return None

    def _declares_body(self) -> bool:
        if self.headers.get("Transfer-Encoding") is not None:
            return True
        return any(str(v).strip() not in ("", "0") for v in (self.headers.get_all("Content-Length") or []))

    def handle_expect_100(self):
        problem = self._framing_problem()
        if problem is None and not self.server.draining:
            return super().handle_expect_100()
        rec = _Record(self.command)
        rec.path = _loggable_path(self.path.partition("?")[0])
        try:
            if problem is None:
                self._reject_unread(rec, 503, "shutting_down", "the inference cap is shutting down",
                                    retry_after=True)
            else:
                self._reject_unread(rec, *problem)
        except _ClientGone:
            rec.outcome = "client_gone"
        self.close_connection = True
        self.server.log_record(rec)
        return False

    def _linger(self) -> None:
        """After refusing a request whose body was not read, read and drop what the client is still
        sending for a moment, so the refusal is not lost to a TCP reset."""
        sock = self.connection
        try:
            self.wfile.flush()
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            return
        deadline = time.monotonic() + LINGER_S
        total = 0
        try:
            while total < LINGER_MAX_BYTES:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                sock.settimeout(left)
                data = sock.recv(65536)
                if not data:
                    break
                total += len(data)
        except OSError:
            pass

    # ------------------------------------------------------------------ responses we generate

    def _send_json(self, rec: _Record, status: int, obj, *, close: bool = False,
                   retry_after: bool = False, extra: tuple = ()) -> None:
        body = json.dumps(obj).encode("utf-8")
        rec.status = status
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if retry_after:
                self.send_header("Retry-After", str(RETRY_AFTER_S))
            for key, value in extra:
                self.send_header(key, value)
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            rec.headers_sent = True
            if self.command != "HEAD":
                self.wfile.write(body)
                rec.bytes_out = len(body)
        except OSError:
            raise _ClientGone() from None

    def _send_error_json(self, rec: _Record, status: int, err_type: str, message: str, **kwargs) -> None:
        rec.outcome = err_type
        self._send_json(rec, status, {"error": {"message": message, "type": err_type, "code": status}},
                        **kwargs)

    def _reject_unread(self, rec: _Record, status: int, err_type: str, message: str,
                       retry_after: bool = False) -> None:
        self._send_error_json(rec, status, err_type, message, close=True, retry_after=retry_after)
        if self._declares_body():
            self._linger()

    def _upstream_failed(self, rec: _Record, status: int, err_type: str, message: str) -> None:
        self.server.count("upstream_errors")
        self._send_error_json(rec, status, err_type, message)

    # ------------------------------------------------------------------ dispatch

    def _dispatch(self) -> None:
        srv = self.server
        rec = _Record(self.command)
        srv.request_started()
        try:
            self._handle(rec)
        except _ClientGone:
            rec.outcome = "client_gone"
            self.close_connection = True
        except Exception as exc:  # never let one request take the process down
            rec.outcome = f"internal_error:{type(exc).__name__}"
            self.close_connection = True
            if not rec.headers_sent:
                try:
                    self._send_json(rec, 500, {"error": {"message": "inference cap internal error",
                                                         "type": "internal_error", "code": 500}},
                                    close=True)
                except _ClientGone:
                    pass
        finally:
            if srv.draining:
                self.close_connection = True
            srv.request_finished()
            srv.log_record(rec)

    def _handle(self, rec: _Record) -> None:
        srv = self.server
        cfg = srv.config
        raw_path, has_query, query = self.path.partition("?")
        rec.path = _loggable_path(raw_path)

        if srv.draining:
            return self._reject_unread(rec, 503, "shutting_down", "the inference cap is shutting down",
                                       retry_after=True)
        problem = self._framing_problem()
        if problem is not None:
            return self._reject_unread(rec, *problem)
        body = self._read_body()

        if raw_path == "/_cap" or raw_path.startswith("/_cap/"):
            rec.name = "_cap"
            return self._admin(rec, raw_path)

        try:
            name, rest = split_target(raw_path)
            if _CTL_RE.search(query):
                raise PathRefused("the query string has control characters")
        except PathRefused as exc:
            return self._send_error_json(rec, 400, "bad_request", str(exc))
        upstream = cfg.upstreams.get(name)
        if upstream is None:
            return self._send_error_json(rec, 404, "unknown_upstream",
                                         "no such upstream; use /<name>/... with a configured name")
        rec.name = name
        try:
            headers = self._forward_headers(upstream, len(body))
        except _HeaderRefused as exc:
            return self._send_error_json(rec, 400, "bad_request", str(exc))
        target = (upstream.base_path + rest) or "/"
        if has_query:
            target += "?" + query

        slotted = self.command not in SLOT_FREE_METHODS
        if slotted:
            waited_from = time.monotonic()
            result = srv.gate.acquire(
                cfg.queue_timeout_s,
                should_abandon=lambda: srv.draining or _peer_closed(self.connection))
            rec.wait_ms = int((time.monotonic() - waited_from) * 1000)
            if result == TIMED_OUT:
                srv.count("rejected_queue_timeout")
                return self._send_error_json(
                    rec, 503, "cap_queue_timeout",
                    f"the dev inference cap is full ({cfg.max_inflight} in flight); retry later",
                    retry_after=True)
            if result == ABANDONED:
                if srv.draining:
                    return self._send_error_json(rec, 503, "shutting_down",
                                                 "the inference cap is shutting down",
                                                 retry_after=True, close=True)
                raise _ClientGone()
        try:
            self._relay(rec, upstream, target, headers, body)
        finally:
            if slotted:
                srv.gate.release()

    def _read_body(self) -> bytes:
        length = self._body_length
        if not length:
            return b""
        try:
            body = self.rfile.read(length)
        except OSError:
            raise _ClientGone() from None
        if len(body) != length:
            raise _ClientGone()
        return body

    def _admin(self, rec: _Record, raw_path: str) -> None:
        if self.command not in SLOT_FREE_METHODS:
            return self._send_error_json(rec, 405, "method_not_allowed", "use GET",
                                         extra=(("Allow", "GET, HEAD"),))
        if raw_path == "/_cap/health":
            return self._send_json(rec, 200, {"ok": True})
        if raw_path == "/_cap/stats":
            return self._send_json(rec, 200, self.server.stats())
        return self._send_error_json(rec, 404, "not_found", "unknown admin endpoint")

    def _forward_headers(self, upstream: Upstream, body_length: int) -> list:
        dropped = _connection_tokens(self.headers.get_all("Connection"))
        out = [("Host", upstream.host_header)]
        for key, value in self.headers.items():
            lowered = key.lower()
            if (lowered in HOP_BY_HOP or lowered.startswith("proxy-") or lowered in dropped
                    or lowered in _REQUEST_ONLY_DROP):
                continue
            if not _TOKEN_RE.fullmatch(key):
                raise _HeaderRefused("a request header name is not valid")
            value = _FOLD_RE.sub(" ", str(value))
            if "\r" in value or "\n" in value or "\x00" in value:
                raise _HeaderRefused("a request header value is not valid")
            try:
                value.encode("latin-1")
            except UnicodeEncodeError:
                raise _HeaderRefused("a request header value is not valid") from None
            out.append((key, value))
        if body_length or self.command in _BODY_METHODS:
            out.append(("Content-Length", str(body_length)))
        out.append(("Connection", "close"))
        return out

    # ------------------------------------------------------------------ relay

    def _relay(self, rec: _Record, upstream: Upstream, target: str, headers: list, body: bytes) -> None:
        cfg = self.server.config
        conn = http.client.HTTPConnection(upstream.host, upstream.port, timeout=cfg.connect_timeout_s)
        watch = ClientWatch(self.connection)
        watch.start()
        try:
            try:
                conn.connect()
            except OSError:
                if watch.gone.is_set():
                    raise _ClientGone() from None
                return self._upstream_failed(rec, 502, "upstream_unreachable",
                                             f"upstream {upstream.name!r} is unreachable")
            watch.attach(conn.sock)
            conn.sock.settimeout(cfg.read_timeout_s)
            try:
                conn.putrequest(self.command, target, skip_host=True, skip_accept_encoding=True)
                for key, value in headers:
                    conn.putheader(key, value)
                conn.endheaders(body if body else None)
                resp = conn.getresponse()
            except TimeoutError:
                if watch.gone.is_set():
                    raise _ClientGone() from None
                return self._upstream_failed(rec, 504, "upstream_timeout",
                                             f"upstream {upstream.name!r} did not answer in time")
            except (OSError, http.client.HTTPException, ValueError):
                if watch.gone.is_set():
                    raise _ClientGone() from None
                return self._upstream_failed(rec, 502, "upstream_error",
                                             f"upstream {upstream.name!r} failed before responding")
            self._relay_response(rec, resp, watch)
        finally:
            watch.stop()
            conn.close()

    def _abort_midstream(self, rec: _Record, watch: ClientWatch, outcome: str) -> None:
        """Headers are already out: the only honest signal left is closing without completing."""
        self.close_connection = True
        if watch.gone.is_set():
            raise _ClientGone()
        rec.outcome = outcome
        self.server.count("upstream_errors")

    def _relay_response(self, rec: _Record, resp: http.client.HTTPResponse, watch: ClientWatch) -> None:
        srv = self.server
        status = resp.status
        no_body = self.command == "HEAD" or status in (204, 304) or 100 <= status < 200
        dropped = _connection_tokens(resp.msg.get_all("Connection"))
        out = []
        upstream_length = None
        for key, value in resp.getheaders():
            lowered = key.lower()
            if lowered in HOP_BY_HOP or lowered.startswith("proxy-") or lowered in dropped:
                continue
            if lowered == "content-length":
                upstream_length = value
                continue
            out.append((key, value))
        if no_body:
            mode = "none"
            if upstream_length is not None and status != 204 and not 100 <= status < 200:
                out.append(("Content-Length", upstream_length))
        elif not resp.chunked and resp.length is not None:
            mode = "length"
            out.append(("Content-Length", str(resp.length)))
        elif self.request_version != "HTTP/1.0":
            mode = "chunked"
            out.append(("Transfer-Encoding", "chunked"))
        else:
            mode = "close"
            out.append(("Connection", "close"))
        try:
            self.send_response_only(status, resp.reason or None)
            for key, value in out:
                self.send_header(key, value)
            self.end_headers()
        except OSError:
            raise _ClientGone() from None
        rec.status = status
        rec.headers_sent = True
        if mode == "close":
            self.close_connection = True
        if mode == "none":
            srv.count("served")
            return

        remaining = resp.length if mode == "length" else None
        while True:
            try:
                chunk = resp.read1(RELAY_CHUNK)
            except TimeoutError:
                return self._abort_midstream(rec, watch, "upstream_timeout_midstream")
            except (OSError, http.client.HTTPException, ValueError):
                return self._abort_midstream(rec, watch, "upstream_error_midstream")
            if not chunk:
                break
            try:
                if mode == "chunked":
                    self.wfile.write(b"%X\r\n%b\r\n" % (len(chunk), chunk))
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            except OSError:
                raise _ClientGone() from None
            rec.bytes_out += len(chunk)
            if remaining is not None:
                remaining -= len(chunk)
        if remaining:
            return self._abort_midstream(rec, watch, "upstream_short_body")
        if not resp.chunked and resp.length is None and watch.gone.is_set():
            # A close-delimited body "ended" because the watcher shut the upstream socket.
            raise _ClientGone()
        if mode == "chunked":
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                raise _ClientGone() from None
        srv.count("served")


# --------------------------------------------------------------------------- server

class CapServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    block_on_close = False  # draining is bounded by drain(), not by joining every thread

    def __init__(self, config: Config, log_stream=None):
        self.config = config
        self.address_family = socket.AF_INET6 if ":" in config.listen_host else socket.AF_INET
        self.gate = SlotGate(config.max_inflight)
        self.draining = False
        self._log_stream = log_stream if log_stream is not None else sys.stderr
        self._log_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._counters = {"served": 0, "rejected_queue_timeout": 0, "upstream_errors": 0}
        self._active = 0
        self._active_cond = threading.Condition()
        self._serve_thread: Optional[threading.Thread] = None
        super().__init__((config.listen_host, config.listen_port), CapHandler)

    def server_bind(self):
        # HTTPServer.server_bind does a reverse DNS lookup for server_name; nothing here needs it.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port

    @property
    def port(self) -> int:
        return self.server_address[1]

    def start(self, poll_interval: float = 0.5) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": poll_interval},
                                  name="cap-accept", daemon=True)
        self._serve_thread = thread
        thread.start()
        return thread

    def stop(self) -> None:
        """Stop accepting and close the listening socket (no draining); for tests."""
        if self._serve_thread is not None and self._serve_thread.is_alive():
            self.shutdown()
        self.server_close()

    def drain(self, timeout: float = DRAIN_TIMEOUT_S) -> bool:
        """Stop accepting, then wait up to `timeout` for in-flight requests. True if all finished."""
        self.draining = True
        self.stop()
        deadline = time.monotonic() + timeout
        with self._active_cond:
            while self._active > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._active_cond.wait(left)
        return True

    def request_started(self) -> None:
        with self._active_cond:
            self._active += 1

    def request_finished(self) -> None:
        with self._active_cond:
            self._active -= 1
            self._active_cond.notify_all()

    def count(self, key: str) -> None:
        with self._stats_lock:
            self._counters[key] += 1

    def stats(self) -> dict:
        with self._stats_lock:
            counters = dict(self._counters)
        return {
            "max_inflight": self.config.max_inflight,
            "inflight": self.gate.inflight,
            "queued": self.gate.queued,
            "peak_inflight": self.gate.peak,
            "served": counters["served"],
            "rejected_queue_timeout": counters["rejected_queue_timeout"],
            "upstream_errors": counters["upstream_errors"],
            "upstreams": list(self.config.upstreams),
        }

    def log(self, line: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._log_lock:
            try:
                self._log_stream.write(f"{stamp} inference-cap {line}\n")
                self._log_stream.flush()
            except (OSError, ValueError):
                pass

    def log_record(self, rec: _Record) -> None:
        duration_ms = int((time.monotonic() - rec.started) * 1000)
        self.log(f"name={rec.name} method={rec.method} path={rec.path} status={rec.status} "
                 f"wait_ms={rec.wait_ms} duration_ms={duration_ms} bytes_out={rec.bytes_out} "
                 f"outcome={rec.outcome}")

    def handle_error(self, request, client_address):
        # The default prints a traceback whose messages can carry request data; keep it to a type.
        self.log(f"connection_error type={type(sys.exc_info()[1]).__name__}")


def build_server(config: Config, log_stream=None) -> CapServer:
    """Bind a CapServer for `config` (not yet serving; call .start())."""
    return CapServer(config, log_stream=log_stream)


# --------------------------------------------------------------------------- entry point

def main(environ: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if environ is None else environ
    try:
        config = parse_config(env)
    except ConfigError as exc:
        print(f"inference-cap: configuration error: {exc}", file=sys.stderr, flush=True)
        return 2
    try:
        server = build_server(config)
    except OSError as exc:
        print(f"inference-cap: cannot listen on {config.listen_host}:{config.listen_port}: "
              f"{exc.strerror or type(exc).__name__}", file=sys.stderr, flush=True)
        return 1

    stop = threading.Event()

    def on_signal(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    serving = server.start()
    host, port = server.server_address[:2]
    server.log(f"listening on {host}:{port} max_inflight={config.max_inflight} "
               f"queue_timeout_s={config.queue_timeout_s:g} read_timeout_s={config.read_timeout_s:g} "
               f"upstreams={','.join(config.upstreams)}")
    while not stop.wait(1.0):
        if not serving.is_alive():
            server.log("the accept loop stopped unexpectedly")
            return 1
    server.log(f"stopping: no new connections; waiting up to {DRAIN_TIMEOUT_S:g} s for in-flight requests")
    drained = server.drain(DRAIN_TIMEOUT_S)
    server.log("stopped" if drained else "stopped with requests still in flight after the drain timeout")
    return 0


if __name__ == "__main__":
    sys.exit(main())
