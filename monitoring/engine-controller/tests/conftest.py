"""Fixtures: a whole fake cluster (Docker over a unix socket, a head engine, a
worker sentinel) around one controller, driven tick by tick with a clock the
test moves. Nothing here touches Docker, a GPU or the network beyond
127.0.0.1 and a socket file under ``tmp_path``."""
from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from typing import List

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))   # controller.py / common.py / sentinel.py
sys.path.insert(0, HERE)                    # fakes.py

from common import DockerClient  # noqa: E402
from controller import Config, Controller  # noqa: E402
from fakes import (  # noqa: E402
    FakeClock, FakeContainer, FakeDocker, FakeGpuExporter, FakeHead, FakeSentinel, HEAD_PROCESSES, WORKER_PROCESSES,
    write_meminfo,
)

#: What the fake /proc/meminfo says unless a test lowers it: comfortably
#: above the 30 GiB default, so no recovery test depends on THIS box's
#: memory (a dev box below the threshold would otherwise shift every
#: restart by one tick).
FIXTURE_MEM_AVAILABLE_GIB = 60.0


@dataclass
class World:
    clock: FakeClock
    docker: FakeDocker
    head_container: FakeContainer
    head: FakeHead
    sentinel: FakeSentinel
    head_gpu: FakeGpuExporter
    worker_gpu: FakeGpuExporter
    events: List[tuple]
    cfg: Config
    ctl: Controller
    tmp: str

    # -- driving ----------------------------------------------------------

    def set_mem_available(self, gib: float) -> None:
        """Rewrite the fake /proc/meminfo the controller reads (kB rows, as
        the kernel prints them)."""
        write_meminfo(self.cfg.meminfo_path, gib)

    def tick(self, n: int = 1, advance: float = 0.0) -> None:
        for _ in range(n):
            if advance:
                self.clock.advance(advance)
            self.ctl.tick()

    def settle(self, canary_wait: float = 10.0) -> None:
        """tick → let the probe (routine canary or the whole readiness
        sequence) finish → tick (harvest)."""
        self.ctl.tick()
        self.ctl.canary.join(canary_wait)
        self.ctl.tick()

    def make_ready(self) -> None:
        self.settle()
        assert self.ctl.state in ("READY", "BUSY"), (self.ctl.state, self.ctl.reason)
        assert self.ctl.proof is not None

    def heal_on_restart(self, start_api: bool = False) -> None:
        """The head comes back healthy when Docker restarts it."""
        head = self.head

        def on_restart(_c: FakeContainer) -> None:
            head.mode = "healthy"
            head.running = 0.0
            head.completion_status = 200
            head.error_chunk_in_stream = False
            head.empty_completions = False
            head.truncate_stream = False
            head.release.set()
            head.release = threading.Event()
            if start_api and head._server is None:
                head.start()

        self.head_container.on_restart = on_restart

    def drive_recovery_to_ready(self, max_ticks: int = 20) -> None:
        """After a recovery has started: wait_load → canary → mark_ready."""
        for _ in range(max_ticks):
            self.ctl.tick()
            self.ctl.canary.join(5.0)
            if not self.ctl.rec.in_progress and self.ctl.state in ("READY", "BUSY"):
                return
            self.ctl.canary.join(10.0)
        raise AssertionError(f"recovery did not finish: state={self.ctl.state} step={self.ctl.rec.step} "
                             f"reason={self.ctl.reason}")

    @property
    def head_restarts(self) -> List[tuple]:
        return self.docker.restarts_of("head")

    @property
    def order(self) -> List[str]:
        return [e[0] for e in self.events]


@pytest.fixture
def world(tmp_path):
    clock = FakeClock()
    events: List[tuple] = []
    docker = FakeDocker(str(tmp_path / "docker.sock"), clock)
    docker.events = events
    head_container = docker.add(FakeContainer("head", clock, HEAD_PROCESSES))
    docker.start()
    head = FakeHead(clock)
    head.start()
    sentinel = FakeSentinel(clock, events)
    sentinel.start()
    head_gpu = FakeGpuExporter(util=93.0)
    head_gpu.start()
    worker_gpu = FakeGpuExporter(util=91.0)
    worker_gpu.start()
    os.makedirs(str(tmp_path / "locks"), exist_ok=True)   # the bind mount pre-exists on the host
    meminfo = str(tmp_path / "meminfo")
    write_meminfo(meminfo, FIXTURE_MEM_AVAILABLE_GIB)
    cfg = Config(
        head_container="head",
        head_api_url=head.url,
        sentinel_url=sentinel.url,
        sentinel_token="",
        router_health_url="",
        head_gpu_exporter_url=head_gpu.url,
        worker_gpu_exporter_url=worker_gpu.url,
        participation_min_util=30.0,
        participation_max_tokens=256,
        participation_sample_s=0.05,
        participation_tail_s=0.15,        # real seconds per readiness run
        readiness_max_tokens=32,
        readiness_progress_retry_s=0.05,
        canary_interval_s=30.0,
        canary_interval_fast_s=10.0,
        canary_timeout_s=3.0,          # real seconds when a probe hangs
        canary_connect_timeout_s=2.0,
        probe_timeout_s=2.0,
        frozen_s=90.0,
        cold_start_budget_s=900.0,
        recovery_budget=3,
        recovery_window_s=3600.0,
        recovery_cooldown_s=0.0,
        recovery_jitter_s=0.0,
        head_api_dead_gap_s=10.0,
        worker_rank_grace_s=120.0,
        head_restart_timeout_s=10,
        poll_s=5.0,
        docker_socket=str(tmp_path / "docker.sock"),
        lock_path=str(tmp_path / "locks" / "engine-recovery.lock"),
        incident_dir=str(tmp_path / "incidents"),
        meminfo_path=meminfo,           # the controller's default is the real /proc/meminfo
        dry_run=False,
    )
    ctl = Controller(cfg, DockerClient(cfg.docker_socket, timeout=5.0), clock)
    w = World(clock, docker, head_container, head, sentinel, head_gpu, worker_gpu, events, cfg, ctl, str(tmp_path))
    try:
        yield w
    finally:
        head.release.set()
        ctl.shutdown()
        ctl.canary.join(5.0)
        head.stop()
        sentinel.stop()
        head_gpu.stop()
        worker_gpu.stop()
        docker.stop()


@pytest.fixture
def worker_world(tmp_path):
    """A fake Docker with the WORKER container, for the sentinel tests."""
    from sentinel import Config as SentinelConfig, Sentinel

    clock = FakeClock()
    docker = FakeDocker(str(tmp_path / "docker.sock"), clock)
    container = docker.add(FakeContainer("sf-local-ai-worker-vllm-worker-1", clock, WORKER_PROCESSES))
    docker.start()
    cfg = SentinelConfig(
        bind="127.0.0.1", port=0, worker_container=container.name, head_ip="127.0.0.1", token="",
        autonomous=False, poll_s=5.0, rank_grace_s=120.0, restart_timeout_s=5, self_restart_budget=2,
        self_restart_window_s=3600.0, docker_socket=str(tmp_path / "docker.sock"),
    )
    sentinel = Sentinel(cfg, DockerClient(cfg.docker_socket, timeout=5.0), clock)

    @dataclass
    class WorkerWorld:
        clock: FakeClock
        docker: FakeDocker
        container: FakeContainer
        cfg: SentinelConfig
        sentinel: Sentinel

        @property
        def restarts(self) -> List[tuple]:
            return self.docker.restarts_of(self.container.name)

    try:
        yield WorkerWorld(clock, docker, container, cfg, sentinel)
    finally:
        docker.stop()


def connect_from(source_ip: str, port: int, request: bytes) -> bytes:
    """One raw HTTP exchange with a chosen SOURCE address (127.0.0.2 is a
    valid, distinct loopback peer on Linux) — the only way to exercise a
    peer-address check for real."""
    import socket

    s = socket.socket()
    s.settimeout(5.0)
    s.bind((source_ip, 0))
    s.connect(("127.0.0.1", port))
    s.sendall(request)
    out = b""
    while True:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        out += chunk
        if b"\r\n\r\n" in out:
            head, _, body = out.partition(b"\r\n\r\n")
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    if len(body) >= int(line.split(b":", 1)[1].strip()):
                        s.close()
                        return out
    s.close()
    return out


def http_status(raw: bytes) -> int:
    return int(raw.split(b" ", 2)[1])


def serve(handler_cls, attr: str, obj) -> "tuple":
    """Run a program's Handler on 127.0.0.1:0 for the duration of a test."""
    from http.server import ThreadingHTTPServer

    setattr(handler_cls, attr, obj)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


@pytest.fixture
def peer_tools():
    return connect_from, http_status, serve
