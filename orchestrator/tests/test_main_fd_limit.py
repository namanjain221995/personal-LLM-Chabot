"""Physical guards and the durable lifecycle wiring in main.py (2026-09-13).

What is pinned:

- `resources.raise_nofile` lifts a 1024 soft RLIMIT_NOFILE to the hard limit
  (a real child process started with soft 1024, the value measured in the
  production container), and the lifespan calls it first;
- above PUBLIC_API_FD_GUARD_RATIO of the soft limit a NEW /v1 request is
  refused 503 model_unavailable with Retry-After 30 in /v1's envelope, while
  /health, /metrics and a chat route still answer and a /v1 preflight is left
  alone; ratio 0 turns the guard off;
- /health shows the limit the process runs with;
- on a running event loop a stale sample is counted on the sampler thread,
  once per burst, while the last sample answers — the listing costs ~170 ms at
  the guard's fd count and must never run on the loop (T1 review,
  2026-09-14); /health shows the cached sample and lists nothing itself;
- the lifespan chains the shutdown signal to durable.request_suspend('restart'),
  starts the durable subsystem after the engine-state poller, and on the way
  out suspends every run and stops the subsystem BEFORE the pool closes;
- the disk guard trips below PUBLIC_API_MIN_FREE_DISK_BYTES and never on an
  unreadable filesystem.
"""
from __future__ import annotations

import asyncio
import os
import resource
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import metrics, resources
from app.config import settings

ORCHESTRATOR_DIR = Path(__file__).resolve().parents[1]


def test_raise_nofile_lifts_a_1024_soft_limit_to_the_hard_limit_in_a_real_process():
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY and hard <= 1024:
        pytest.skip(f"hard limit {hard} leaves nothing to raise")

    def lower():
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, hard))

    code = (
        "import resource; from app import resources;"
        "before = resource.getrlimit(resource.RLIMIT_NOFILE)[0];"
        "after = resources.raise_nofile();"
        "print(before, after[0], after[1])"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ORCHESTRATOR_DIR, preexec_fn=lower,
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    before, after_soft, after_hard = (int(v) for v in out.stdout.split()[-3:])
    assert before == 1024
    assert after_soft == after_hard, "soft raised to hard"
    assert after_soft > 1024


def test_fd_pressure_is_open_fds_over_the_soft_limit_cached_and_published(monkeypatch):
    metrics.reset()
    resources._reset_sample()
    monkeypatch.setattr(resources, "nofile_limits", lambda: (1000, 4000))
    monkeypatch.setattr(resources, "open_fds", lambda: 700)
    assert resources.fd_pressure(now=100.0) == pytest.approx(0.7)
    assert metrics._gauges["orchestrator_open_fds_ratio"][()] == pytest.approx(0.7)
    monkeypatch.setattr(resources, "open_fds", lambda: 900)
    assert resources.fd_pressure(now=100.5) == pytest.approx(0.7), "cached within a second"
    assert resources.fd_pressure(now=101.0) == pytest.approx(0.9)
    monkeypatch.setattr(settings, "public_api_fd_guard_ratio", 0.70)
    assert resources.fd_guard_tripped(now=101.2) is True
    monkeypatch.setattr(settings, "public_api_fd_guard_ratio", 0.0)
    assert resources.fd_guard_tripped(now=101.3) is False, "0 turns the guard off"
    resources._reset_sample()


def test_on_a_running_loop_a_stale_sample_is_counted_off_the_loop_once_per_burst(monkeypatch):
    import threading
    import time as _time

    metrics.reset()
    resources._reset_sample()
    monkeypatch.setattr(resources, "nofile_limits", lambda: (1000, 4000))
    listings: list = []

    def slow_listing():
        listings.append(threading.current_thread().name)
        _time.sleep(0.3)  # a listing at ~300k descriptors
        return 800

    async def run():
        monkeypatch.setattr(resources, "open_fds", lambda: 100)
        assert resources.fd_pressure() == pytest.approx(0.1), "the first sample is counted inline"
        monkeypatch.setattr(resources, "open_fds", slow_listing)
        resources._sample = (_time.monotonic() - 5.0, 100, 1000)  # stale
        started = _time.monotonic()
        readings = [resources.fd_pressure() for _ in range(100)]
        elapsed = _time.monotonic() - started
        assert elapsed < 0.1, f"the loop never waits for a listing ({elapsed:.3f}s)"
        assert readings == [pytest.approx(0.1)] * 100, "the last sample answers meanwhile"
        for _ in range(100):
            await asyncio.sleep(0.01)
            if resources.fd_pressure() == pytest.approx(0.8):
                break
        assert resources.fd_pressure() == pytest.approx(0.8)
        assert metrics._gauges["orchestrator_open_fds_ratio"][()] == pytest.approx(0.8)
        assert len(listings) == 1, "one listing for the burst"
        assert listings[0].startswith("fd-sample") and listings[0] != threading.current_thread().name

    asyncio.run(run())
    resources._reset_sample()


def test_health_shows_the_cached_sample_and_lists_nothing_on_the_loop(monkeypatch):
    resources._reset_sample()
    monkeypatch.setattr(resources, "nofile_limits", lambda: (1000, 4000))
    monkeypatch.setattr(resources, "open_fds", lambda: 250)

    async def run():
        resources.fd_pressure()

        def no_listing():
            raise AssertionError("/health listed /proc/self/fd on the loop")

        monkeypatch.setattr(resources, "open_fds", no_listing)
        return resources.describe()

    shown = asyncio.run(run())
    assert shown["open_fds"] == 250 and shown["fd_pressure"] == pytest.approx(0.25)
    resources._reset_sample()


def test_open_fds_counts_the_listing_minus_its_own_descriptor(monkeypatch):
    monkeypatch.setattr(resources.os, "listdir", lambda path: ["0", "1", "2", "3", "4"])
    assert resources.open_fds() == 4


def test_open_fds_follows_real_descriptors():
    """Real descriptors, with slack: the pool's and the loop's threads open and
    close sockets of their own at any moment in a long session."""
    before = resources.open_fds()
    handles = [open(os.devnull) for _ in range(200)]
    try:
        assert 180 <= resources.open_fds() - before <= 220
    finally:
        for h in handles:
            h.close()


@pytest.fixture()
def pressured(monkeypatch):
    """The guard ratio set just below this process's REAL pressure."""
    resources._reset_sample()
    real = resources.fd_pressure()
    assert real > 0
    monkeypatch.setattr(settings, "public_api_fd_guard_ratio", real / 2.0)
    yield
    resources._reset_sample()


def test_the_guard_refuses_new_v1_requests_before_headers_while_health_and_chat_answer(pressured, monkeypatch):
    from app import main as app_main

    async def quick_dependencies():
        return {"status": "ok", "checks": {}}

    monkeypatch.setattr(app_main, "check_dependencies", quick_dependencies)
    client = TestClient(app_main.app)

    refused = client.post("/v1/responses", json={"model": "techsara-35b", "input": "hi"},
                          headers={"authorization": "Bearer ts-live-anything", "origin": "https://dev.example"})
    assert refused.status_code == 503
    assert refused.headers["retry-after"] == "30"
    body = refused.json()
    assert body["error"]["code"] == "model_unavailable"
    assert body["error"]["request_id"] and refused.headers["x-request-id"] == body["error"]["request_id"]
    assert refused.headers["access-control-allow-origin"] == "https://dev.example"
    assert client.get("/v1/models").status_code == 503

    preflight = client.options("/v1/responses", headers={
        "origin": "https://dev.example", "access-control-request-method": "POST"})
    assert preflight.status_code != 503, "a preflight holds nothing and is not refused"

    health = client.get("/health")
    assert health.status_code == 200
    shown = health.json()["resources"]
    assert shown["nofile_soft"] == resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    assert shown["fd_pressure"] > shown["fd_guard_ratio"]
    assert client.get("/metrics").status_code == 200
    assert client.get("/history/conversations").status_code == 200, "chat is never refused"


def test_with_the_guard_off_v1_is_not_refused(monkeypatch):
    from app import main as app_main

    monkeypatch.setattr(settings, "public_api_fd_guard_ratio", 0.0)
    resources._reset_sample()
    client = TestClient(app_main.app)
    response = client.get("/v1/models")
    assert response.status_code != 503


def test_the_lifespan_raises_the_limit_chains_the_signal_and_orders_the_durable_lifecycle(monkeypatch):
    from app import db as real_db, engine_state, main as app_main, shutdown_signals, web_worker

    order: list = []

    class FakeDurable:
        def request_suspend(self, reason):
            order.append(("request_suspend", reason))

        async def start(self):
            order.append("durable.start")

        async def suspend_all(self, reason):
            order.append(("suspend_all", reason))

        def stop(self):  # sync is accepted too
            order.append("durable.stop")

    fake = FakeDurable()
    installed: dict = {}

    async def _noop():
        return None

    monkeypatch.setattr(web_worker, "start", lambda: None)
    monkeypatch.setattr(web_worker, "stop", _noop)
    monkeypatch.setattr(resources, "raise_nofile", lambda: order.append("raise_nofile") or (1, 1))
    real_start = engine_state.start
    monkeypatch.setattr(engine_state, "start", lambda: (order.append("engine_state.start"), real_start())[1])
    monkeypatch.setattr(app_main, "_durable_module", lambda: fake)
    monkeypatch.setattr(shutdown_signals, "install", lambda cb, loop=None: installed.setdefault("cb", cb) and True)
    monkeypatch.setattr(real_db, "close_pool", lambda: order.append("close_pool"))

    async def drive():
        async with app_main.lifespan(app_main.app):
            order.append("serving")
            installed["cb"]("SIGTERM")

    asyncio.run(drive())
    assert order[0] == "raise_nofile"
    assert order.index("engine_state.start") < order.index("durable.start") < order.index("serving")
    assert ("request_suspend", "restart") in order, "the signal callback suspends for a restart"
    tail = order[order.index("serving"):]
    assert tail.index(("suspend_all", "shutdown")) < tail.index("durable.stop") < tail.index("close_pool")


def test_the_lifespan_drives_the_real_durable_module_and_no_stand_in_remains(monkeypatch):
    """Assembler, 2026-09-14: T1's stand-in for a missing durable module is
    removed; the lifespan's interface is the real module's."""
    from app import main as app_main
    import app.publicapi.durable as durable

    assert app_main._durable_module() is durable
    assert not hasattr(app_main, "_DURABLE_SHIM")
    for name in ("request_suspend", "start", "suspend_all", "stop"):
        assert callable(getattr(durable, name)), name


def test_the_disk_guard_trips_below_the_floor_and_never_on_an_unreadable_filesystem(monkeypatch, tmp_path):
    class Stat:
        f_bavail = 10
        f_frsize = 1024 ** 3  # 10 GiB free

    monkeypatch.delenv("PUBLIC_API_MIN_FREE_DISK_BYTES", raising=False)
    assert resources.min_free_disk_bytes() == 20 * 1024 ** 3, "the design default"
    monkeypatch.setenv("PUBLIC_API_MIN_FREE_DISK_BYTES", str(25 * 1024 ** 3))
    assert resources.min_free_disk_bytes() == 25 * 1024 ** 3, "read at call time, like disk_ledger"
    monkeypatch.setattr(settings, "public_api_min_free_disk_bytes", 20 * 1024 ** 3, raising=False)
    monkeypatch.setattr(resources.os, "statvfs", lambda path: Stat())
    assert resources.disk_free_bytes("/data") == 10 * 1024 ** 3
    assert resources.disk_guard_tripped("/data") is True
    Stat.f_bavail = 30
    assert resources.disk_guard_tripped("/data") is False

    def unreadable(path):
        raise OSError("no such device")

    monkeypatch.setattr(resources.os, "statvfs", unreadable)
    assert resources.disk_free_bytes("/data") is None
    assert resources.disk_guard_tripped("/data") is False
