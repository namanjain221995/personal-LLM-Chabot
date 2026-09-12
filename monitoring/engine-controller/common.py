#!/usr/bin/env python3
"""Shared helpers for the engine controller (head) and the worker sentinel.

Both programs run on a stock ``python:3.12-slim`` with a bind-mounted script
and no pip install (the same reason monitoring/exporters/dgx-gpu is
stdlib-only: no build-time network access, nothing to keep patched, and a
container that restarts the model engine must not itself depend on a package
index being up). Everything here is the standard library.

What lives here, and why it is shared rather than duplicated:

  * ``DockerClient`` — the Docker Engine API over the unix socket with
    ``http.client``. Four endpoints only: inspect, top, logs, restart. Errors
    are split into "the daemon said no" (``DockerError``, carries the HTTP
    status) and "the socket cannot be observed" (``DockerUnavailable``): the
    controller's state machine treats the second as MONITORING_UNKNOWN, never
    as DOWN (contract §2), so the distinction is load-bearing.
  * ``open_http`` — an HTTP request with a SEPARATE connect timeout and read
    timeout. ``urllib`` has one timeout for both; the canary needs a 5 s
    connect budget and a 60 s read budget (contract §5), and it reports the
    connect latency on its own.
  * ``MetricsDoc`` — Prometheus text rendering with a one-hot helper whose
    label set is closed at the call site. Cardinality is the only real risk
    of a hand-rolled exporter, so the helper refuses a value outside the set
    instead of minting a series.
  * ``parse_vllm_metrics`` — the four vLLM series the controller reads, summed
    across engines, matched by EXACT name (``vllm:num_requests_waiting`` has
    a sibling ``vllm:num_requests_waiting_by_reason`` that a prefix match
    would fold in); ``parse_gpu_utilization`` — the one dgx-gpu exporter
    series the participation probe samples (contract §5 v2 step 4);
    ``read_mem_available`` — the host's ``MemAvailable`` from
    ``/proc/meminfo`` (the head-memory precondition of a recovery).
  * Every error that can reach ``/state`` carries a bounded ``kind``
    (``DockerUnavailable.kind``, ``DockerError.kind``, ``ConnectFailed.kind``)
    so the published document never repeats a socket path, a host:port or
    an exception message — those stay in the log.
  * ``Clock`` — wall and monotonic time behind one object so the tests drive
    the state machine with a fake clock instead of sleeping.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Bounded vocabularies shared by both programs (contract §4, §6.2)
# ---------------------------------------------------------------------------

#: The fatal log signatures the sentinel scans for (contract §6.2). The order
#: is the order they are tried; the FIRST match on a line is the one reported,
#: so the more specific CUDA faults come before the generic wrappers.
FATAL_SIGNATURES: Tuple[str, ...] = (
    "misaligned address",
    "illegal memory access",
    "AcceleratorError",
    "WorkerProc hit an exception",
    "died unexpectedly",
    "EngineDeadError",
    "NCCL error",
    "out of memory",
)

#: Compile-error signatures (contract §6.6): one of these in the head log
#: while a cold start times out means the persistent kernel cache is the
#: likely culprit, and the runbook's ``scripts/cluster-recover.sh
#: --clear-kernel-cache`` is the fix — the controller never deletes a cache
#: by itself. A line must also carry an error marker (``_COMPILE_ERROR_MARKERS``)
#: so an INFO line that merely mentions nvcc does not count.
COMPILE_SIGNATURES: Tuple[str, ...] = (
    "torch._dynamo",
    "flashinfer.jit",
    "nvcc",
    "cuda_nvrtc",
)
_COMPILE_ERROR_MARKERS: Tuple[str, ...] = ("Error", "ERROR", "error", "Traceback", "failed", "Failed")


def scan_compile_error_lines(text: str) -> Optional[str]:
    """The first compile signature found on an error-marked line of a head
    log capture, or ``None``. Only the bounded signature is returned — never
    the line, which can carry paths."""
    for line in text.splitlines():
        if not any(marker in line for marker in _COMPILE_ERROR_MARKERS):
            continue
        for sig in COMPILE_SIGNATURES:
            if sig in line:
                return sig
    return None


#: Failure categories (contract §4). Nothing else may ever appear as a
#: ``category`` label value. ``head_restarted_externally`` (2026-09-12,
#: drill 5): a head start the controller did NOT perform — Docker's restart
#: policy after ``vllm serve`` exited, an operator's ``docker restart`` —
#: while rank 1 was still paired with the previous head; the controller's
#: response is a worker re-pair (sentinel ``POST /restart``), never a head
#: restart, so it is an incident category but not a pair-restart attempt.
FAILURE_CATEGORIES: Tuple[str, ...] = (
    "none",
    "worker_rank_dead",
    "head_engine_dead",
    "head_api_dead",
    "wedged_frozen_tokens",
    "canary_timeout",
    "canary_http_error",
    "connect_error",
    "budget_exhausted",
    "manual",
    "cold_start_timeout",
    "head_restarted_externally",
)


def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        logging.getLogger(__name__).warning("%s=%r is not a number; using %s", name, raw, default)
        return float(default)


def env_int(name: str, default: int) -> int:
    return int(env_float(name, float(default)))


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


class Clock:
    """Wall time for timestamps people and Docker compare, monotonic time for
    intervals, and ``sleep`` — all in one object so tests can substitute a
    fake that advances on demand."""

    def time(self) -> float:
        return time.time()

    def mono(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


def parse_docker_time(raw: Optional[str]) -> Optional[float]:
    """RFC 3339 with nanoseconds (``2026-09-11T22:22:47.688511544Z``) → epoch.

    Docker's zero value ``0001-01-01T00:00:00Z`` means "never" and comes back
    as ``None`` rather than as a negative epoch that would sort as ancient.
    """
    if not raw or raw.startswith("0001-01-01"):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # datetime accepts at most six fractional digits; Docker sends nine.
    if "." in text:
        head, rest = text.split(".", 1)
        frac = ""
        tz = ""
        for i, ch in enumerate(rest):
            if ch.isdigit():
                frac += ch
            else:
                tz = rest[i:]
                break
        text = f"{head}.{frac[:6].ljust(6, '0')}{tz}"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def utc_stamp(epoch: float) -> str:
    """``20260911T221550Z`` — the incident id format of contract §6.2."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def configure_logging(prefix: str, level_env: str) -> logging.Logger:
    """One line per event, every line prefixed ``[controller]``/``[sentinel]``
    so a grep over a combined ``docker logs`` finds them; Docker adds the
    timestamp, the asctime here is for anyone reading without ``-t``."""
    logging.Formatter.converter = time.gmtime  # UTC, like the incident ids and Docker's own -t
    logging.basicConfig(
        level=os.environ.get(level_env, "INFO").upper(),
        format=f"[{prefix}] %(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger(prefix)


# ---------------------------------------------------------------------------
# Docker Engine API over the unix socket
# ---------------------------------------------------------------------------


class DockerError(Exception):
    """The daemon answered with an error status (404 for a missing container,
    409 for a container that is not running, 500 …).

    ``kind`` is the bounded rendering that may appear in ``/state``
    (``api_<status>``); the free-form ``message`` is for the log only, it
    can carry container ids and paths.
    """

    def __init__(self, status: int, message: str):
        super().__init__(f"docker API {status}: {message}")
        self.status = status
        self.message = message

    @property
    def kind(self) -> str:
        return f"api_{self.status}"


#: The bounded ways the Docker socket can be unobservable (``/state``'s
#: ``docker_error``): the socket file is not there (bind mount missing),
#: nothing listens on it, the daemon did not answer in time, the exchange
#: broke mid-way, or the reply was not JSON.
DOCKER_UNAVAILABLE_KINDS: Tuple[str, ...] = ("socket_missing", "refused", "timeout", "broken", "malformed")


class DockerUnavailable(DockerError):
    """The socket cannot be reached or the exchange broke: nothing is known.

    A subclass of ``DockerError`` so a caller that does not care about the
    distinction can catch one thing, and its own class for the caller that
    must (the controller's MONITORING_UNKNOWN rule).
    """

    def __init__(self, message: str, kind: str = "broken"):
        super().__init__(0, message)
        self._kind = kind if kind in DOCKER_UNAVAILABLE_KINDS else "broken"

    @property
    def kind(self) -> str:
        return self._kind


class _UnixHTTPConnection(http.client.HTTPConnection):
    """``http.client`` over ``AF_UNIX``: the base class only knows TCP."""

    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:  # noqa: D401 — http.client API
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def demux_docker_stream(data: bytes) -> str:
    """Undo the 8-byte frame headers of ``application/vnd.docker.multiplexed-stream``.

    A container started without a TTY (every vLLM container here) returns
    its logs as frames: ``[stream-type, 0, 0, 0, size(4, big-endian)]`` then
    ``size`` bytes. A TTY container returns raw text; the heuristic below
    keeps raw text intact when the first byte is not a frame type.
    """
    out: List[bytes] = []
    pos = 0
    n = len(data)
    while pos + 8 <= n:
        kind = data[pos]
        if kind not in (0, 1, 2) or data[pos + 1:pos + 4] != b"\x00\x00\x00":
            # Not framed after all: return everything from here verbatim.
            out.append(data[pos:])
            pos = n
            break
        size = int.from_bytes(data[pos + 4:pos + 8], "big")
        out.append(data[pos + 8:pos + 8 + size])
        pos += 8 + size
    if pos < n:
        out.append(data[pos:])
    return b"".join(out).decode("utf-8", "replace")


class DockerClient:
    """The four Docker calls the programs make, and nothing else.

    ``api_version`` is pinned (1.53 on this host) rather than negotiated: the
    daemon accepts any version it supports and the path is stable, and a
    negotiation round-trip on every tick would be one more thing that can
    fail in the middle of an incident.
    """

    def __init__(self, socket_path: str, api_version: str = "1.53", timeout: float = 10.0):
        self.socket_path = socket_path
        self.api_version = api_version
        self.timeout = timeout

    # -- transport --------------------------------------------------------

    def _request(self, method: str, path: str, timeout: Optional[float] = None) -> Tuple[int, bytes, str]:
        conn = _UnixHTTPConnection(self.socket_path, timeout or self.timeout)
        try:
            conn.request(method, f"/v{self.api_version}{path}", headers={"Host": "docker"})
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, body, resp.getheader("Content-Type", "") or ""
        except FileNotFoundError as exc:
            raise DockerUnavailable(f"{type(exc).__name__}: {exc}", "socket_missing") from exc
        except ConnectionRefusedError as exc:
            raise DockerUnavailable(f"{type(exc).__name__}: {exc}", "refused") from exc
        except socket.timeout as exc:
            raise DockerUnavailable(f"{type(exc).__name__}: {exc}", "timeout") from exc
        except (OSError, http.client.HTTPException) as exc:
            # A half-closed exchange, a reset: "cannot observe", not "down".
            raise DockerUnavailable(f"{type(exc).__name__}: {exc}", "broken") from exc
        finally:
            conn.close()

    def _json(self, method: str, path: str, timeout: Optional[float] = None) -> dict:
        status, body, _ = self._request(method, path, timeout)
        if status >= 400:
            raise DockerError(status, _docker_message(body))
        try:
            return json.loads(body.decode("utf-8", "replace") or "null")
        except ValueError as exc:
            raise DockerUnavailable(f"malformed JSON from the daemon: {exc}", "malformed") from exc

    # -- calls ------------------------------------------------------------

    def inspect(self, name: str) -> dict:
        """``GET /containers/{name}/json``."""
        return self._json("GET", f"/containers/{urllib.parse.quote(name, safe='')}/json")

    def top(self, name: str, ps_args: str = "-eo pid,ppid,etimes,args") -> List[str]:
        """``GET /containers/{name}/top`` → one string per process (all columns
        joined by single spaces) so callers can search for a command name
        without caring which column ``ps`` put it in."""
        doc = self._json(
            "GET",
            f"/containers/{urllib.parse.quote(name, safe='')}/top?ps_args={urllib.parse.quote(ps_args)}",
        )
        rows = doc.get("Processes") or []
        return [" ".join(str(cell) for cell in row) for row in rows]

    def logs(self, name: str, tail: int = 400, since: Optional[int] = None,
             timestamps: bool = True, timeout: Optional[float] = None) -> str:
        """``GET /containers/{name}/logs`` (stdout+stderr), de-multiplexed."""
        query = {"stdout": "1", "stderr": "1", "tail": str(int(tail))}
        if timestamps:
            query["timestamps"] = "1"
        if since is not None:
            query["since"] = str(int(since))
        status, body, ctype = self._request(
            "GET",
            f"/containers/{urllib.parse.quote(name, safe='')}/logs?{urllib.parse.urlencode(query)}",
            timeout,
        )
        if status >= 400:
            raise DockerError(status, _docker_message(body))
        if "multiplexed" in ctype or (body[:1] in (b"\x00", b"\x01", b"\x02")):
            return demux_docker_stream(body)
        return body.decode("utf-8", "replace")

    #: Seconds allowed for the daemon's restart reply beyond the stop timeout
    #: ``t``. The daemon answers only once the container is back; a reply
    #: that arrives later than this is a ``DockerUnavailable`` (timeout) and
    #: the caller re-inspects the container instead of calling it failed.
    restart_reply_extra_s: float = 60.0

    def restart(self, name: str, t: int = 10) -> None:
        """``POST /containers/{name}/restart?t=N`` — SIGTERM, then SIGKILL after
        ``t`` seconds. The daemon answers only once the container is back, so
        the call blocks for ``t`` plus the start; the timeout allows for it."""
        status, body, _ = self._request(
            "POST",
            f"/containers/{urllib.parse.quote(name, safe='')}/restart?t={int(t)}",
            timeout=float(t) + self.restart_reply_extra_s,
        )
        if status >= 400:
            raise DockerError(status, _docker_message(body))


def _docker_message(body: bytes) -> str:
    try:
        return str(json.loads(body.decode("utf-8", "replace")).get("message", ""))[:300]
    except (ValueError, AttributeError):
        return body.decode("utf-8", "replace")[:300]


# ---------------------------------------------------------------------------
# Plain HTTP with separate connect and read timeouts
# ---------------------------------------------------------------------------


#: The bounded ways a plain-HTTP exchange can fail before a status line:
#: ``refused`` (RST — an observation: nothing listens), ``timeout`` (the
#: connect did not complete), ``unreachable`` (no route, DNS), ``broken``
#: (the request or the response line broke mid-way). ``/state`` publishes
#: the kind; the message with host:port stays in the log.
CONNECT_KINDS: Tuple[str, ...] = ("refused", "timeout", "unreachable", "broken")


class ConnectFailed(Exception):
    """The TCP connect (or the request write) did not complete.

    ``refused`` distinguishes a listener that is not there (an observation:
    the port answered RST) from a network that did not answer at all (an
    unobservable condition); ``kind`` is the bounded rendering."""

    def __init__(self, message: str, refused: bool, kind: str = ""):
        super().__init__(message)
        self.refused = refused
        self.kind = kind if kind in CONNECT_KINDS else ("refused" if refused else "broken")


class ReadTimeout(Exception):
    """The peer accepted the request and then went quiet for the read budget."""


def split_url(url: str) -> Tuple[str, int, str]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", ""):
        raise ValueError(f"only http:// is supported, got {url!r}")
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return host, port, path


def tcp_connect(host: str, port: int, timeout: float) -> Tuple[bool, str]:
    """One TCP handshake. Returns ``(ok, kind)`` with ``kind`` in
    ``ok|refused|timeout|unreachable``."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "ok"
    except ConnectionRefusedError:
        return False, "refused"
    except socket.timeout:
        return False, "timeout"
    except OSError:
        return False, "unreachable"


def open_http(url: str, method: str = "GET", body: Optional[bytes] = None,
              headers: Optional[Dict[str, str]] = None, connect_timeout: float = 5.0,
              read_timeout: float = 5.0, clock: Optional[Clock] = None):
    """Open a request and return ``(response, connect_seconds, connection)``.

    The caller reads the body (possibly line by line for a stream) and closes
    the connection. A failed connect raises ``ConnectFailed``; a peer that
    accepted and then stayed silent past ``read_timeout`` raises
    ``ReadTimeout`` — the two things the canary must tell apart.
    """
    host, port, path = split_url(url)
    conn = http.client.HTTPConnection(host, port, timeout=connect_timeout)
    mono = (clock or Clock()).mono
    t0 = mono()
    try:
        conn.connect()
    except ConnectionRefusedError as exc:
        conn.close()
        raise ConnectFailed(f"connection refused by {host}:{port}", refused=True, kind="refused") from exc
    except socket.timeout as exc:
        conn.close()
        raise ConnectFailed(f"connect to {host}:{port} timed out", refused=False, kind="timeout") from exc
    except OSError as exc:
        conn.close()
        raise ConnectFailed(f"connect to {host}:{port} failed: {exc}", refused=False, kind="unreachable") from exc
    connect_s = mono() - t0
    # The socket keeps the connect timeout unless told otherwise; from here on
    # the read budget applies.
    conn.sock.settimeout(read_timeout)
    hdrs = {"Host": f"{host}:{port}", "Connection": "close"}
    if headers:
        hdrs.update(headers)
    try:
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
    except socket.timeout as exc:
        conn.close()
        raise ReadTimeout(f"{method} {path}: no response within {read_timeout:.0f}s") from exc
    except (OSError, http.client.HTTPException) as exc:
        conn.close()
        raise ConnectFailed(f"{method} {path} broke: {exc}", refused=False, kind="broken") from exc
    return resp, connect_s, conn


def fetch(url: str, method: str = "GET", body: Optional[bytes] = None,
          headers: Optional[Dict[str, str]] = None, connect_timeout: float = 5.0,
          read_timeout: float = 5.0, max_bytes: int = 4 * 1024 * 1024,
          clock: Optional[Clock] = None) -> Tuple[int, bytes]:
    """Whole-body convenience over ``open_http``. Raises the same errors."""
    resp, _, conn = open_http(url, method, body, headers, connect_timeout, read_timeout, clock)
    try:
        try:
            data = resp.read(max_bytes)
        except socket.timeout as exc:
            raise ReadTimeout(f"{method} {url}: body stalled") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise ConnectFailed(f"{method} {url}: body broke: {exc}", refused=False, kind="broken") from exc
        return resp.status, data
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# vLLM /metrics parsing
# ---------------------------------------------------------------------------

#: The series the controller reads (contract §3 signal 12). Summed across the
#: ``engine`` label (a data-parallel deployment would have several) but only
#: over samples that carry ``model_name`` — the label the served model puts on
#: every engine series, and the guard against an unrelated exposition line.
VLLM_SERIES = {
    "vllm:num_requests_running": "requests_running",
    "vllm:num_requests_waiting": "requests_waiting",
    "vllm:generation_tokens_total": "generation_tokens_total",
    "vllm:prompt_tokens_total": "prompt_tokens_total",
    # Two more PROGRESS witnesses (2026-09-12, the ~950K needle on candidate
    # B): vLLM counts a prompt's tokens only when its prefill FINISHES, so
    # during one chunked 950K prefill both token counters sat flat for
    # 12 minutes while requests were running — the exact shape of a wedge —
    # and the controller confirmed WEDGED at 09:04:15Z (its restart was
    # refused only by the budget). The scheduler-step counter advances on
    # every iteration, chunked prefills included, and the KV usage rises
    # with every chunk; either one moving means the engine is working.
    "vllm:iteration_tokens_total_count": "iterations_total",
    "vllm:kv_cache_usage_perc": "kv_cache_usage",
}


def parse_vllm_metrics(text: str) -> Dict[str, Optional[float]]:
    """Return ``{requests_running, requests_waiting, generation_tokens_total,
    prompt_tokens_total}``; a key is ``None`` when the series is absent."""
    sums: Dict[str, Optional[float]] = {v: None for v in VLLM_SERIES.values()}
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        # name{labels} value [timestamp]   or   name value
        brace = line.find("{")
        space = line.find(" ")
        if brace != -1 and (space == -1 or brace < space):
            name = line[:brace]
            end = line.find("}", brace)
            if end == -1:
                continue
            labels = line[brace + 1:end]
            rest = line[end + 1:].strip()
        else:
            name = line[:space] if space != -1 else line
            labels = ""
            rest = line[space + 1:].strip() if space != -1 else ""
        key = VLLM_SERIES.get(name)
        if key is None:
            continue
        if "model_name=" not in labels:
            continue
        value_text = rest.split(" ", 1)[0]
        try:
            value = float(value_text)
        except ValueError:
            continue
        sums[key] = (sums[key] or 0.0) + value
    return sums


def parse_gpu_utilization(text: str, metric: str = "dgx_gpu_utilization_percent") -> Optional[float]:
    """The highest ``<metric>{...} value`` sample in a dgx-gpu exporter
    document (one GPU per Spark, but a multi-GPU box is folded the same way),
    or ``None`` when the series is absent — the exporter says ``dgx_gpu_up 0``
    and emits no utilisation line when nvidia-smi did not answer."""
    best: Optional[float] = None
    for line in text.splitlines():
        if not line.startswith(metric):
            continue
        rest = line[len(metric):]
        if rest[:1] not in ("{", " "):
            continue  # a longer name with the same prefix
        if rest[:1] == "{":
            end = rest.find("}")
            if end == -1:
                continue
            rest = rest[end + 1:]
        try:
            value = float(rest.strip().split(" ", 1)[0])
        except ValueError:
            continue
        best = value if best is None else max(best, value)
    return best


# ---------------------------------------------------------------------------
# Host memory (the head-memory precondition of a recovery)
# ---------------------------------------------------------------------------

#: ``/proc/meminfo`` prints kibibytes with a ``kB`` suffix (and nothing else
#: for the memory rows); anything unexpected is "not observed", never zero.
_MEMINFO_UNITS: Dict[str, int] = {"kb": 1024, "mb": 1024 * 1024, "gb": 1024 ** 3, "b": 1, "": 1}


def parse_meminfo_available(text: str) -> Optional[int]:
    """``MemAvailable`` of a ``/proc/meminfo`` document, in bytes, or
    ``None`` when the row is absent or malformed. ``MemAvailable`` is the
    kernel's own estimate of what a new process can take without swapping
    (page cache included) — the number the runbook's memory check reads
    and the one a fresh 21.8 GiB weight load actually has to fit in."""
    for line in text.splitlines():
        if not line.startswith("MemAvailable:"):
            continue
        parts = line.split(":", 1)[1].split()
        if not parts:
            return None
        try:
            value = int(parts[0])
        except ValueError:
            return None
        unit = parts[1].lower() if len(parts) > 1 else ""
        factor = _MEMINFO_UNITS.get(unit)
        if factor is None or value < 0:
            return None
        return value * factor
    return None


def read_mem_available(path: str = "/proc/meminfo") -> Optional[int]:
    """The host's ``MemAvailable`` in bytes, or ``None`` when it cannot be
    read. With ``network_mode: host`` and no lxcfs, ``/proc/meminfo``
    inside the controller's container IS the host's (verified 2026-09-12:
    identical ``MemTotal``, ``MemAvailable`` within 11 MB of the host's
    reading at the same instant). Never raises: a missing ``/proc`` must
    not stop a recovery, it only leaves the precondition unobserved."""
    try:
        with open(path, "r", encoding="ascii", errors="replace") as fh:
            return parse_meminfo_available(fh.read(64 * 1024))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Prometheus text exposition with closed label sets
# ---------------------------------------------------------------------------


class MetricsDoc:
    """Builds one exposition document. ``# HELP``/``# TYPE`` are written once
    per metric name however many samples follow, and ``one_hot`` refuses a
    current value outside the allowed set instead of inventing a label."""

    def __init__(self) -> None:
        self._lines: List[str] = []
        self._declared: set = set()

    def _head(self, name: str, mtype: str, help_text: str) -> None:
        if name in self._declared:
            return
        self._declared.add(name)
        self._lines.append(f"# HELP {name} {help_text}")
        self._lines.append(f"# TYPE {name} {mtype}")

    @staticmethod
    def _fmt(value: float) -> str:
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, int):
            return str(value)
        return repr(float(value))

    @staticmethod
    def _labels(labels: Optional[Dict[str, str]]) -> str:
        if not labels:
            return ""
        return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items()) + "}"

    def gauge(self, name: str, value, help_text: str, labels: Optional[Dict[str, str]] = None) -> None:
        self._head(name, "gauge", help_text)
        self._lines.append(f"{name}{self._labels(labels)} {self._fmt(value)}")

    def counter(self, name: str, value, help_text: str, labels: Optional[Dict[str, str]] = None) -> None:
        self._head(name, "counter", help_text)
        self._lines.append(f"{name}{self._labels(labels)} {self._fmt(value)}")

    def one_hot(self, name: str, help_text: str, label: str, allowed: Iterable[str], current: str) -> None:
        values = tuple(allowed)
        if current not in values:
            raise ValueError(f"{name}: {current!r} is not in the bounded set {values}")
        self._head(name, "gauge", help_text)
        for value in values:
            self._lines.append(f'{name}{{{label}="{_escape(value)}"}} {1 if value == current else 0}')

    def render(self) -> str:
        return "\n".join(self._lines) + "\n"


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


# ---------------------------------------------------------------------------
# A sliding-window budget ("at most N per window")
# ---------------------------------------------------------------------------


class Budget:
    """``limit`` events per ``window_s``, sliding. Thread-safe."""

    def __init__(self, limit: int, window_s: float, clock: Clock):
        self.limit = int(limit)
        self.window_s = float(window_s)
        self._clock = clock
        self._events: List[float] = []
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s
        self._events = [t for t in self._events if t > cutoff]

    def used(self) -> int:
        with self._lock:
            self._prune(self._clock.mono())
            return len(self._events)

    def remaining(self) -> int:
        return max(0, self.limit - self.used())

    def exhausted(self) -> bool:
        return self.used() >= self.limit

    def record(self) -> None:
        with self._lock:
            now = self._clock.mono()
            self._prune(now)
            self._events.append(now)


# ---------------------------------------------------------------------------
# http.server helpers
# ---------------------------------------------------------------------------


def send_bytes(handler, status: int, body: bytes, content_type: str) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


def send_json(handler, status: int, obj) -> None:
    send_bytes(handler, status, json.dumps(obj, sort_keys=True).encode(), "application/json")


def send_text(handler, status: int, text: str) -> None:
    send_bytes(handler, status, text.encode("utf-8", "replace"), "text/plain; charset=utf-8")


def send_metrics(handler, text: str) -> None:
    send_bytes(handler, 200, text.encode(), "text/plain; version=0.0.4; charset=utf-8")


def read_json_body(handler, limit: int = 64 * 1024):
    """The request body as JSON, or ``None`` when there is none / it is not
    JSON. Bounded so a stray upload cannot balloon the process."""
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        return None
    if length <= 0:
        return {}
    raw = handler.rfile.read(min(length, limit))
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None


def is_loopback(addr: str) -> bool:
    """Exactly 127.0.0.1 (contract §6.1), also as seen through a dual-stack
    socket — not "anything that looks local"."""
    return addr in ("127.0.0.1", "::ffff:127.0.0.1")


def run_periodically(fn: Callable[[], None], interval_s: float, clock: Clock,
                     stop: threading.Event, log: logging.Logger, what: str) -> None:
    """The main loop of both programs: call ``fn`` every ``interval_s``,
    never let one exception end the process, and pace on the monotonic
    clock so a slow tick does not drift the schedule."""
    while not stop.is_set():
        started = clock.mono()
        try:
            fn()
        except Exception:  # noqa: BLE001 — the loop must outlive any one tick
            log.exception("%s tick failed", what)
        elapsed = clock.mono() - started
        delay = max(0.0, interval_s - elapsed)
        if stop.wait(delay):
            break
