"""Fakes for the controller and sentinel tests — no Docker, no GPU, no vLLM.

Three small HTTP servers stand in for the real world:

  * ``FakeDocker``  — the four Docker Engine API endpoints the programs use,
    served over a temporary unix socket exactly as the daemon frames them
    (RFC 3339 nanosecond timestamps, multiplexed log stream, 204 on restart).
  * ``FakeHead``    — the head's OpenAI-compatible API: ``/health``,
    ``/v1/models``, ``/metrics`` in vLLM's exposition shape, and a
    ``/v1/chat/completions`` (streaming and not) whose behaviour is scripted
    per test: healthy, wedged (accepts and never answers), hung (nothing
    answers, TCP still accepts), engine-dead (5xx), api-dead (the listener
    is closed, so connects are refused). ``ignore_eos`` requests get exactly
    ``max_tokens`` chunks, and every completion moves the token counter, so
    the readiness sequence's progress step sees what vLLM would show.
  * ``FakeSentinel`` — the worker sentinel's three endpoints, recording every
    ``POST /restart`` with its peer and token; optionally requiring the token
    on every endpoint like the real one.
  * ``FakeGpuExporter`` — one dgx-gpu exporter, serving a scripted
    utilisation for the participation probe.

``FakeClock`` is real time plus an offset the test moves: durations the code
measures stay real (milliseconds), while the intervals the state machine
reasons about (90 s frozen, 900 s cold start, 3600 s windows) are jumped.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0):
        self._lock = threading.Lock()
        self._offset = start - time.time()
        self._mono_offset = start - time.monotonic()

    def time(self) -> float:
        with self._lock:
            return time.time() + self._offset

    def mono(self) -> float:
        with self._lock:
            return time.monotonic() + self._mono_offset

    def sleep(self, seconds: float) -> None:
        # Sleeping IS advancing: the jitter and any wait in the code move the
        # clock instead of the wall.
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._offset += seconds
            self._mono_offset += seconds


def rfc3339(epoch: float) -> str:
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    nanos = int(round((epoch - int(epoch)) * 1e9))
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z"


# ---------------------------------------------------------------------------
# Fake Docker
# ---------------------------------------------------------------------------


class FakeContainer:
    def __init__(self, name: str, clock: FakeClock, processes: Optional[List[str]] = None):
        self.name = name
        self.id = "0123456789ab" + name.encode().hex()[:52]
        self.clock = clock
        self.running = True
        self.status = "running"
        self.health: Optional[str] = "healthy"
        self.restart_count = 0
        self.started_at = clock.time() - 3600.0
        self.finished_at: Optional[float] = None
        self.processes: List[str] = list(processes or [])
        self.logs: List[Tuple[float, str]] = []
        self.on_restart: Optional[Callable[["FakeContainer"], None]] = None

    def log(self, text: str, at: Optional[float] = None) -> None:
        self.logs.append((at if at is not None else self.clock.time(), text))

    def to_json(self) -> dict:
        return {
            "Id": self.id,
            "Name": "/" + self.name,
            "RestartCount": self.restart_count,
            "State": {
                "Status": self.status,
                "Running": self.running,
                "Restarting": self.status == "restarting",
                "StartedAt": rfc3339(self.started_at),
                "FinishedAt": rfc3339(self.finished_at) if self.finished_at else "0001-01-01T00:00:00Z",
                "Health": {"Status": self.health, "FailingStreak": 0} if self.health else None,
            },
            "Config": {"Tty": False},
        }


class _UnixHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True

    def server_bind(self) -> None:
        path = self.server_address
        if os.path.exists(path):
            os.unlink(path)
        self.socket.bind(path)
        self.server_name = "unix"
        self.server_port = 0

    def get_request(self):
        sock, _ = self.socket.accept()
        return sock, ("unix", 0)


class FakeDocker:
    def __init__(self, socket_path: str, clock: FakeClock):
        self.socket_path = socket_path
        self.clock = clock
        self.containers: Dict[str, FakeContainer] = {}
        self.events: List[tuple] = []
        self._server: Optional[_UnixHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.fail_restart = False
        #: Seconds (real) the daemon holds the restart reply AFTER applying
        #: it — the "SIGKILL of a 25 GiB process plus re-create takes longer
        #: than the client waits" case.
        self.restart_reply_delay_s = 0.0

    def add(self, container: FakeContainer) -> FakeContainer:
        self.containers[container.name] = container
        self.containers[container.id] = container
        return container

    def start(self) -> None:
        docker = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _route(self):
                path, _, query = self.path.partition("?")
                parts = path.strip("/").split("/")
                # /v1.53/containers/<name>/<op>
                if len(parts) >= 4 and parts[0].startswith("v") and parts[1] == "containers":
                    return parts[2], parts[3], dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                return None, None, {}

            def do_GET(self) -> None:  # noqa: N802
                name, op, q = self._route()
                c = docker.containers.get(name or "")
                if c is None:
                    self._send(404, json.dumps({"message": f"No such container: {name}"}).encode())
                    return
                if op == "json":
                    self._send(200, json.dumps(c.to_json()).encode())
                elif op == "top":
                    if not c.running:
                        self._send(409, json.dumps({"message": f"container {name} is not running"}).encode())
                        return
                    rows = [["1", "0", "10", p] for p in c.processes]
                    self._send(200, json.dumps({"Titles": ["PID", "PPID", "ELAPSED", "COMMAND"],
                                                "Processes": rows}).encode())
                elif op == "logs":
                    since = float(q.get("since", "0") or 0)
                    tail = int(q.get("tail", "0") or 0)
                    lines = [(ts, t) for ts, t in c.logs if ts >= since]
                    if tail:
                        lines = lines[-tail:]
                    frames = b""
                    for ts, text in lines:
                        payload = (f"{rfc3339(ts)} {text}\n").encode()
                        frames += bytes([1, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload
                    self._send(200, frames, "application/vnd.docker.multiplexed-stream")
                else:
                    self._send(404, b'{"message":"page not found"}')

            def do_POST(self) -> None:  # noqa: N802
                name, op, q = self._route()
                c = docker.containers.get(name or "")
                if c is None:
                    self._send(404, json.dumps({"message": f"No such container: {name}"}).encode())
                    return
                if op != "restart":
                    self._send(404, b'{"message":"page not found"}')
                    return
                if docker.fail_restart:
                    self._send(500, b'{"message":"simulated daemon failure"}')
                    return
                at = docker.clock.time()
                c.restart_count += 1
                c.finished_at = at
                c.started_at = at + 0.001
                c.running = True
                c.status = "running"
                docker.events.append(("docker_restart", c.name, at, int(q.get("t", "10"))))
                if c.on_restart:
                    c.on_restart(c)
                if docker.restart_reply_delay_s:
                    time.sleep(docker.restart_reply_delay_s)
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, fmt, *args):
                pass

        self._server = _UnixHTTPServer(self.socket_path, Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Take the daemon away: the socket file goes too, as when the mount
        is missing — the client must see DockerUnavailable, not a 5xx."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

    def restarts_of(self, name: str) -> List[tuple]:
        return [e for e in self.events if e[0] == "docker_restart" and e[1] == name]


HEAD_PROCESSES = [
    "/sbin/docker-init -- vllm serve /models/repos/x --served-model-name m",
    "/usr/bin/python3 /usr/local/bin/vllm serve /models/repos/x --served-model-name m",
    "VLLM::EngineCore",
    "VLLM::Worker_TP0",
]
WORKER_PROCESSES = [
    "/sbin/docker-init -- vllm serve /models/repos/x --headless",
    "/usr/bin/python3 /usr/local/bin/vllm serve /models/repos/x --headless",
    "VLLM::EngineCore",
    "VLLM::Worker_TP1",
]


# ---------------------------------------------------------------------------
# Fake head engine
# ---------------------------------------------------------------------------


class _ReusableServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class FakeHead:
    """Modes: ``healthy`` | ``wedged`` (completions accepted, never answered)
    | ``hung`` (nothing answers: /health, /v1/models, /metrics and
    completions all hold the socket — the API event loop wedged) |
    ``engine_dead`` (5xx); ``stop()`` makes it api-dead (connection refused),
    ``start()`` brings the same port back."""

    def __init__(self, clock: FakeClock, model_id: str = "Qwen/Qwen3.6-35B-A3B-NVFP4"):
        self.clock = clock
        self.model_id = model_id
        self.mode = "healthy"
        self.running = 0.0
        self.waiting = 0.0
        self.gen_total = 1000.0
        self.prompt_total = 5000.0
        self.completions = 0
        self.release = threading.Event()
        self.wedged_hold_s = 30.0
        self.port: Optional[int] = None
        self._server: Optional[_ReusableServer] = None
        self._thread: Optional[threading.Thread] = None
        self.completion_status = 200
        #: tokens the counters advance by on every /metrics scrape — a
        #: saturated-but-working engine for the starvation tests.
        self.progress_per_scrape = 0.0
        #: When set, /metrics does NOT move for completions served — the
        #: readiness progress step must then fail.
        self.freeze_counters_for_completions = False
        #: Requests seen, by shape, so a test can assert the sequence order.
        self.requests: List[dict] = []
        #: Extra chunks/tokens to withhold: the stream sends this many fewer
        #: than asked (0 = exactly max_tokens under ignore_eos).
        self.short_by = 0
        #: Hang only STREAMING completions (non-stream keeps answering): the
        #: readiness sequence then fails at step 2 exactly.
        self.hang_streams = False
        #: Real seconds a completion takes before its first byte — a probe
        #: that is still in flight when something else happens.
        self.completion_delay_s = 0.0
        #: The dying-stream shape: 200, then an error chunk before any token
        #: (EngineDeadError mid-stream).
        self.error_chunk_in_stream = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def metrics_text(self) -> str:
        self.gen_total += self.progress_per_scrape
        lbl = f'engine="0",model_name="{self.model_id}"'
        return (
            "# HELP vllm:num_requests_running Number of requests in model execution batches.\n"
            "# TYPE vllm:num_requests_running gauge\n"
            f"vllm:num_requests_running{{{lbl}}} {self.running}\n"
            "# TYPE vllm:num_requests_waiting gauge\n"
            f"vllm:num_requests_waiting{{{lbl}}} {self.waiting}\n"
            f'vllm:num_requests_waiting_by_reason{{{lbl},reason="capacity"}} 99.0\n'
            "# TYPE vllm:prompt_tokens_total counter\n"
            f"vllm:prompt_tokens_total{{{lbl}}} {self.prompt_total}\n"
            "# TYPE vllm:generation_tokens_total counter\n"
            f"vllm:generation_tokens_total{{{lbl}}} {self.gen_total}\n"
        )

    def start(self) -> None:
        head = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _hang(self) -> None:
                head.release.wait(head.wedged_hold_s)
                try:
                    self.connection.close()
                except OSError:
                    pass

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?")[0]
                if head.mode == "hung":
                    self._hang()
                    return
                if path == "/health":
                    if head.mode == "engine_dead":
                        self._send(500, b"Internal Server Error", "text/plain")
                    else:
                        self._send(200, b"")
                elif path == "/v1/models":
                    self._send(200, json.dumps({"object": "list", "data": [
                        {"id": head.model_id, "object": "model", "permission": [{"id": "modelperm-x"}]}]}).encode())
                elif path == "/metrics":
                    self._send(200, head.metrics_text().encode(), "text/plain; version=0.0.4")
                else:
                    self._send(404, b"{}")

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                if self.path.split("?")[0] != "/v1/chat/completions":
                    self._send(404, b"{}")
                    return
                head.completions += 1
                req = json.loads(body) if body else None
                head.last_request = req
                if isinstance(req, dict):
                    head.requests.append(req)
                if head.mode == "engine_dead":
                    self._send(500, json.dumps({"error": {"message": "EngineDeadError", "type": "InternalServerError"}}).encode())
                    return
                if head.mode in ("wedged", "hung") or (head.hang_streams and (req or {}).get("stream")):
                    # Accept, send nothing, hold the socket until released.
                    self._hang()
                    return
                if head.completion_status != 200:
                    self._send(head.completion_status, b'{"error":"scripted"}')
                    return
                if head.completion_delay_s:
                    time.sleep(head.completion_delay_s)
                max_tokens = int((req or {}).get("max_tokens") or 4)
                # vLLM with ignore_eos generates exactly max_tokens; without
                # it the model stops after "ok" (one content chunk).
                n_tokens = max(0, max_tokens - head.short_by) if (req or {}).get("ignore_eos") else 1
                if not head.freeze_counters_for_completions:
                    head.gen_total += n_tokens
                    head.prompt_total += 8
                if not (req or {}).get("stream", False):
                    self._send(200, json.dumps({
                        "id": "chatcmpl-fake", "object": "chat.completion", "model": head.model_id,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok" * n_tokens},
                                     "finish_reason": "length" if (req or {}).get("ignore_eos") else "stop"}],
                        "usage": {"prompt_tokens": 8, "completion_tokens": n_tokens, "total_tokens": 8 + n_tokens},
                    }).encode())
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def chunk(obj) -> None:
                    payload = (f"data: {json.dumps(obj)}\n\n").encode()
                    self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
                    self.wfile.flush()

                base = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": head.model_id}
                if head.error_chunk_in_stream:
                    chunk({"error": {"message": "EngineDeadError", "type": "InternalServerError", "code": 500}})
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    return
                chunk({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})
                for _ in range(n_tokens):
                    chunk({**base, "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}]})
                chunk({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                done = b"data: [DONE]\n\n"
                self.wfile.write(f"{len(done):x}\r\n".encode() + done + b"\r\n0\r\n\r\n")
                self.wfile.flush()

            def log_message(self, fmt, *args):
                pass

        self._server = _ReusableServer(("127.0.0.1", self.port or 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """API dead: the listener is closed, connects are refused."""
        self.release.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self.release = threading.Event()


# ---------------------------------------------------------------------------
# Fake sentinel
# ---------------------------------------------------------------------------


class FakeSentinel:
    def __init__(self, clock: FakeClock, events: Optional[List[tuple]] = None):
        self.clock = clock
        self.events = events if events is not None else []
        self.container = {"running": True, "health": "healthy", "restart_count": 0,
                          "started_at": clock.time() - 3600.0}
        self.rank_process_alive: Optional[bool] = True
        self.rank_joined: Optional[bool] = True
        self.last_fault: Optional[dict] = None
        self.self_restarts_in_window = 0
        self.restart_calls: List[dict] = []
        self.get_calls: List[dict] = []
        self.diagnostics_text = "worker log line 1\nworker log line 2\n"
        self.port: Optional[int] = None
        self._server: Optional[_ReusableServer] = None
        self.restart_status = 200
        #: When set, every endpoint requires this X-Sentinel-Token (as the
        #: real sentinel does once configured).
        self.required_token: Optional[str] = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def state(self) -> dict:
        return {"container": dict(self.container), "rank_process_alive": self.rank_process_alive,
                "rank_joined": self.rank_joined, "last_fault": self.last_fault,
                "self_restarts_in_window": self.self_restarts_in_window, "autonomous": False,
                "observed_at": self.clock.time()}

    def start(self) -> None:
        sentinel = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?")[0]
                sentinel.get_calls.append({"path": path, "token": self.headers.get("X-Sentinel-Token")})
                if sentinel.required_token and self.headers.get("X-Sentinel-Token") != sentinel.required_token:
                    self._send(403, b'{"error":"not authorised"}')
                    return
                if path == "/state":
                    self._send(200, json.dumps(sentinel.state()).encode())
                elif path == "/diagnostics":
                    self._send(200, sentinel.diagnostics_text.encode(), "text/plain")
                elif path == "/healthz":
                    self._send(200, b"ok\n", "text/plain")
                else:
                    self._send(404, b"{}")

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length) if length else b""
                if self.path.split("?")[0] != "/restart":
                    self._send(404, b"{}")
                    return
                at = sentinel.clock.time()
                sentinel.restart_calls.append({"peer": self.client_address[0],
                                               "token": self.headers.get("X-Sentinel-Token"), "at": at})
                if sentinel.required_token and self.headers.get("X-Sentinel-Token") != sentinel.required_token:
                    self._send(403, b'{"error":"not authorised"}')
                    return
                if sentinel.restart_status != 200:
                    self._send(sentinel.restart_status, b'{"error":"scripted"}')
                    return
                sentinel.container["restart_count"] += 1
                sentinel.container["started_at"] = at
                sentinel.rank_process_alive = True
                sentinel.rank_joined = True
                sentinel.events.append(("sentinel_restart", at))
                self._send(200, json.dumps({"restarted_at": at}).encode())

            def log_message(self, fmt, *args):
                pass

        self._server = _ReusableServer(("127.0.0.1", self.port or 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


class FakeGpuExporter:
    """One dgx-gpu exporter (`dgx_gpu_utilization_percent`). ``util`` is
    what it reports; ``stop()`` makes it unreachable; ``no_reading`` answers
    200 without the series (nvidia-smi did not answer)."""

    def __init__(self, util: float = 93.0, gpu_index: int = 0):
        self.util = util
        self.gpu_index = gpu_index
        self.no_reading = False
        self.scrapes = 0
        self.port: Optional[int] = None
        self._server: Optional[_ReusableServer] = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/metrics"

    def text(self) -> str:
        self.scrapes += 1
        lines = ["# HELP dgx_gpu_up 1 if nvidia-smi answered this scrape.", "# TYPE dgx_gpu_up gauge",
                 f"dgx_gpu_up {0 if self.no_reading else 1}"]
        if not self.no_reading:
            lines += ["# TYPE dgx_gpu_utilization_percent gauge",
                      f'dgx_gpu_utilization_percent{{gpu="{self.gpu_index}",uuid="GPU-x",name="NVIDIA GB10"}} {float(self.util)}',
                      "# TYPE dgx_gpu_utilization_percent_other gauge",
                      'dgx_gpu_utilization_percent_other{gpu="0"} 100.0']
        return "\n".join(lines) + "\n"

    def start(self) -> None:
        exporter = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802
                body = exporter.text().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                pass

        self._server = _ReusableServer(("127.0.0.1", self.port or 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


__all__ = ["FakeClock", "FakeContainer", "FakeDocker", "FakeGpuExporter", "FakeHead", "FakeSentinel",
           "HEAD_PROCESSES", "WORKER_PROCESSES", "free_port", "rfc3339"]
