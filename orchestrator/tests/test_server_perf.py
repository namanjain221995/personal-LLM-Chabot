"""Server performance track (2026-09-15): event-loop blocking fixes.

Covers: the shared verifying SSL context (O1), the SSRF guard's private DNS
pool (O6), the lifespan warm-up and GIL switch interval (O2/O4), the
event-loop lag probe and fault gauge (O8), and app/core/cpu_pool.py (O3).

Offline: no engine, no GPU. The cpu_pool process tests start real forkserver
children (a few hundred ms each); nothing here needs the test database.
"""
from __future__ import annotations

import asyncio
import contextvars
import os
import pathlib
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time

import httpx
import pytest

from app import metrics
from app.core import cpu_pool, net

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

#: Every module whose per-call httpx client now takes the shared context.
SHARED_CONTEXT_SITES = [
    "health.py", "asr.py", "resilience.py", "search/brave.py", "search/tavily.py",
    "search/searxng.py", "core/salesforce.py", "engine_state.py", "analytics/infra.py",
    "publicapi/liveness.py", "publicapi/engines.py",
]


# ---------------------------------------------------------------------------
# O1: one verifying SSL context
# ---------------------------------------------------------------------------


def _ctx_fingerprint(ctx: ssl.SSLContext) -> tuple:
    return (
        int(ctx.options), ctx.verify_mode, ctx.check_hostname, ctx.minimum_version,
        ctx.maximum_version, int(ctx.verify_flags), len(ctx.get_ca_certs()),
    )


def test_every_client_site_passes_the_shared_context():
    """A3 statically: no per-call client in these modules builds its own
    context (11.7 ms of CA-bundle load each, on the loop)."""
    for rel in SHARED_CONTEXT_SITES:
        src = (APP / rel).read_text()
        calls = re.findall(r"httpx\.AsyncClient\((.*)\)", src)
        assert calls, rel
        for args in calls:
            assert "verify=shared_ssl_context()" in args, (rel, args)


def test_kv_budget_keeps_its_trust_env_false_default():
    """kv_budget builds AsyncClient(trust_env=False): SSL_CERT_FILE must keep
    NOT applying there, and the shared context is built with trust_env=True."""
    src = (APP / "kv_budget.py").read_text()
    assert "trust_env=False" in src
    assert "shared_ssl_context" not in src


def test_no_site_mutates_or_upgrades_the_shared_context():
    """The shared-context contract: HTTP/1.1 only, no client certs, no ALPN h2,
    no verification changes, anywhere the context is used."""
    forbidden = ("load_cert_chain", "set_alpn_protocols", "http2=True", "verify=False",
                 ".verify_mode =", ".check_hostname =")
    for path in APP.rglob("*.py"):
        src = path.read_text()
        if "shared_ssl_context()" not in src or path.name == "net.py":
            continue
        for word in forbidden:
            assert word not in src, (str(path), word)


def test_shared_context_is_cached_verifying_and_env_keyed(monkeypatch, tmp_path):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    a = net.shared_ssl_context()
    assert net.shared_ssl_context() is a
    assert a.verify_mode == ssl.CERT_REQUIRED and a.check_hostname is True
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    b = net.shared_ssl_context()
    assert b is not a and b.verify_mode == ssl.CERT_REQUIRED
    other = tmp_path / "bundle.pem"
    other.write_text("")
    monkeypatch.delenv("SSL_CERT_DIR")
    monkeypatch.setenv("SSL_CERT_FILE", str(other))
    try:
        c = net.shared_ssl_context()
    except ssl.SSLError:
        c = None  # an empty bundle may be refused outright; either way not `a`
    assert c is not a


def test_client_builds_with_the_shared_context_build_no_new_context(monkeypatch):
    """A3: after the first build, a client per call costs zero context builds."""
    net.shared_ssl_context()
    builds = []
    real = ssl.create_default_context
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: builds.append(1) or real(*a, **k))
    before = _ctx_fingerprint(net.shared_ssl_context())
    for _ in range(25):
        httpx.AsyncClient(timeout=2.0, verify=net.shared_ssl_context())
    assert builds == []
    assert _ctx_fingerprint(net.shared_ssl_context()) == before


@pytest.fixture
def ip_cert(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl binary not available")
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=127.0.0.1",
         "-addext", "subjectAltName=IP:127.0.0.1"],
        check=True, capture_output=True,
    )
    return str(key), str(cert)


def test_custom_ca_is_honoured_and_a_handshake_leaves_the_context_unchanged(monkeypatch, ip_cert):
    """SSL_CERT_FILE still wins (a private CA verifies through a patched call
    site), and a real TLS handshake through the shared context -- httpcore sets
    ALPN per connect -- leaves its options and verification settings as built."""
    from app.search.searxng import SearxngProvider
    from app.search.base import SearchUnavailableError

    key, cert = ip_cert
    monkeypatch.setenv("SSL_CERT_FILE", cert)

    async def main():
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert, key)

        async def handler(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                body = b'{"results": [{"url": "https://example.org/", "title": "t", "content": "c"}]}'
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
                await writer.drain()
            except Exception:  # noqa: BLE001
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=server_ctx)
        port = server.sockets[0].getsockname()[1]
        ctx = net.shared_ssl_context()
        before = _ctx_fingerprint(ctx)
        try:
            results = await SearxngProvider(f"https://127.0.0.1:{port}").search("q", 3)
        finally:
            server.close()
        return ctx, before, results

    ctx, before, results = asyncio.run(main())
    assert [r.url for r in results] == ["https://example.org/"]
    assert _ctx_fingerprint(ctx) == before

    # And without the private CA the same call must FAIL verification.
    monkeypatch.delenv("SSL_CERT_FILE")

    async def untrusted():
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert, key)

        async def handler(reader, writer):
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=server_ctx)
        port = server.sockets[0].getsockname()[1]
        try:
            await SearxngProvider(f"https://127.0.0.1:{port}").search("q", 3)
        finally:
            server.close()

    with pytest.raises(SearchUnavailableError):
        asyncio.run(untrusted())


# ---------------------------------------------------------------------------
# O6: the SSRF guard resolves on its own pool
# ---------------------------------------------------------------------------


def test_fetch_dns_runs_on_the_private_pool_and_keeps_context(monkeypatch):
    seen = []
    marker = contextvars.ContextVar("marker", default="unset")

    def slow_validate(url, backend):
        seen.append((threading.current_thread().name, marker.get()))
        time.sleep(0.4)  # a blackholing nameserver, shortened
        return url

    monkeypatch.setattr(net, "_validate_and_pin", slow_validate)

    async def main():
        marker.set("request-ctx")
        stuck = [asyncio.ensure_future(net._off_loop_validate(f"https://h{i}.test/", None))
                 for i in range(net.DNS_RESOLVER_THREADS * 2)]
        await asyncio.sleep(0.05)
        started = time.perf_counter()
        # The loop's default executor (health, uploads, ...) is not queued behind them.
        assert await asyncio.to_thread(lambda: 42) == 42
        default_wait = time.perf_counter() - started
        await asyncio.gather(*stuck)
        return default_wait

    default_wait = asyncio.run(main())
    assert default_wait < 0.2, default_wait
    assert all(name.startswith("fetch-dns") for name, _ in seen), seen
    assert all(value == "request-ctx" for _, value in seen)


# ---------------------------------------------------------------------------
# O2 / O4: lifespan warm-up and the switch interval
# ---------------------------------------------------------------------------


def test_warm_up_makes_no_network_calls(monkeypatch):
    """Safe with every engine unreachable: warm-up is imports and object
    construction only. Any socket connect fails the test."""
    from app import main

    attempts = []

    def refuse(*a, **k):
        attempts.append(a)
        raise OSError("network is off in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    called = []
    monkeypatch.setattr(main.llm, "warm_clients", lambda: called.append(1), raising=False)
    asyncio.run(main._warm_process())
    assert attempts == []
    assert called == [1]  # the public hook is used when llm.py exposes it


def test_warm_up_survives_a_failing_hook(monkeypatch):
    from app import main

    def boom():
        raise RuntimeError("no engine")

    monkeypatch.setattr(main.llm, "warm_clients", boom, raising=False)
    asyncio.run(main._warm_process())  # must not raise


def test_switch_interval_unset_changes_nothing(monkeypatch):
    from app import main

    before = sys.getswitchinterval()
    monkeypatch.setattr(main.settings, "py_switch_interval_s", 0.0, raising=False)
    assert main._apply_switch_interval() is None
    assert sys.getswitchinterval() == before


def test_switch_interval_applies_and_restores(monkeypatch):
    from app import main

    before = sys.getswitchinterval()
    monkeypatch.setattr(main.settings, "py_switch_interval_s", 0.001, raising=False)
    previous = main._apply_switch_interval()
    try:
        assert previous == before
        assert abs(sys.getswitchinterval() - 0.001) < 1e-6
    finally:
        main._restore_switch_interval(previous)
    assert sys.getswitchinterval() == before


# ---------------------------------------------------------------------------
# O8: event-loop lag and major faults
# ---------------------------------------------------------------------------


def _lag_series():
    return dict(metrics._hists.get(metrics.LOOP_LAG, {}))


def test_lag_probe_sees_a_blocked_loop_and_discards_the_first_sample():
    metrics.reset()

    async def main():
        task = asyncio.ensure_future(metrics.event_loop_lag_probe(0.01, sample_every=5))
        await asyncio.sleep(0.1)  # past the discarded first sample
        time.sleep(0.2)  # a 200 ms stall of the serving loop
        await asyncio.sleep(0.1)  # past the sample_every read too
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    (counts, total, n), = _lag_series().values()
    buckets = metrics._buckets_for(metrics.LOOP_LAG)
    over_100ms = n - counts[buckets.index(0.1)]
    assert over_100ms >= 1, (counts, n)
    text = metrics.render()
    assert "orchestrator_event_loop_lag_seconds_bucket" in text
    if sys.platform.startswith("linux"):
        assert "orchestrator_process_major_faults " in text
        assert "orchestrator_thread_limiter_total " in text


def test_lag_probe_first_sample_is_not_observed():
    metrics.reset()

    async def main():
        task = asyncio.ensure_future(metrics.event_loop_lag_probe(0.01))
        await asyncio.sleep(0)  # the probe starts its first sleep
        time.sleep(0.15)
        await asyncio.sleep(0.001)  # the first (late) wake-up happens here
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert sum(v[2] for v in _lag_series().values()) == 0


def test_major_fault_parser_handles_parentheses_in_comm(tmp_path):
    stat = tmp_path / "stat"
    fields = ["S"] + [str(i) for i in range(4, 60)]  # field 3 onwards
    fields[12 - 3] = "4242"  # majflt is field 12
    stat.write_text("123 (a) b) c) " + " ".join(fields))
    assert metrics._read_major_faults(str(stat)) == 4242.0
    assert metrics._read_major_faults(str(tmp_path / "missing")) == -1.0


# ---------------------------------------------------------------------------
# O3: cpu_pool
# ---------------------------------------------------------------------------


def _impure(x):  # defined in a test module, so not in PURE_MODULES
    return x


def test_cpu_pool_refuses_impure_modules_even_in_thread_mode():
    pool = cpu_pool.CpuPool(0, 1)
    with pytest.raises(ValueError):
        asyncio.run(pool.run(_impure, 1))


def test_cpu_pool_in_thread_mode_runs_and_is_the_default(monkeypatch):
    monkeypatch.delenv("CPU_POOL_WORKERS", raising=False)
    assert cpu_pool.configured_workers() == 0
    pool = cpu_pool.CpuPool(0, 2)
    pid = asyncio.run(pool.run(cpu_pool.child_pid))
    assert pid == os.getpid()
    assert pool.executor() is None


@pytest.fixture
def process_pool():
    pools = []

    def make(workers=2, slots=2, **kw):
        p = cpu_pool.CpuPool(workers, slots, **kw)
        pools.append(p)
        return p

    yield make
    for p in pools:
        ex = p._executor
        if ex is not None:
            for proc in list((getattr(ex, "_processes", None) or {}).values()):
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            ex.shutdown(wait=False, cancel_futures=True)


def test_child_imports_nothing_process_bound(process_pool):
    """A child is a fresh interpreter: no app.config, app.db, lancedb, duckdb
    or openai -- whatever the parent (this test process) has imported."""
    import app.config  # noqa: F401 — the parent has it; the child must not
    pool = process_pool(1, 1)

    async def main():
        return await pool.run(cpu_pool.child_modules), await pool.run(cpu_pool.child_pid)

    modules, pid = asyncio.run(main())
    assert pid != os.getpid()
    assert "app.core.cpu_pool" in modules
    for name in cpu_pool.FORBIDDEN_IN_CHILD:
        assert name not in modules, name


def test_cancelled_calls_never_over_admit_children(process_pool):
    """A6: cancel 20 in-flight calls; in-flight in children never exceeds the
    slot count, because slots are released when the CHILD finishes."""
    pool = process_pool(2, 2)
    peak = {"in_flight": 0}
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            peak["in_flight"] = max(peak["in_flight"], pool.in_flight)
            time.sleep(0.002)

    async def main():
        await pool.run(cpu_pool.child_pid)  # children up
        tasks = [asyncio.ensure_future(pool.run(cpu_pool.burn, 0.3)) for _ in range(20)]
        await asyncio.sleep(0.1)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # New work after the cancellation still respects the two busy slots.
        started = time.perf_counter()
        more = [asyncio.ensure_future(pool.run(cpu_pool.burn, 0.05)) for _ in range(6)]
        await asyncio.gather(*more)
        return time.perf_counter() - started

    t = threading.Thread(target=sampler, daemon=True)
    t.start()
    try:
        elapsed = asyncio.run(main())
    finally:
        stop.set()
        t.join()
    assert 1 <= peak["in_flight"] <= 2, peak
    # The two cancelled-but-running calls held their slots until done (~0.3 s).
    assert elapsed >= 0.15, elapsed
    assert pool.in_flight == 0 and pool.waiting == 0


def test_a_killed_child_falls_back_rebuilds_once_and_counts(process_pool):
    metrics.reset()
    pool = process_pool(1, 1)

    async def main():
        pid = await pool.run(cpu_pool.child_pid)
        broken_executor = pool._executor
        call = asyncio.ensure_future(pool.run(cpu_pool.burn, 1.0))
        await asyncio.sleep(0.2)
        os.kill(pid, signal.SIGKILL)
        result = await call  # re-run in a thread: returns this process's pid
        after = await pool.run(cpu_pool.child_pid)
        return pid, broken_executor, result, after

    pid, broken_executor, result, after = asyncio.run(main())
    assert result == os.getpid()
    assert pool.broken_total == 1 and pool.fallback_total >= 1
    assert pool._executor is not broken_executor and pool.workers == 1
    assert after not in (pid, os.getpid())
    assert "cpu_pool_broken_total 1" in metrics.render()


def test_stop_leaves_no_orphan_children(process_pool):
    pool = process_pool(2, 2)

    async def main():
        pids = set(await asyncio.gather(*(pool.run(cpu_pool.burn, 0.2) for _ in range(4))))
        pending = asyncio.ensure_future(pool.run(cpu_pool.burn, 30.0))
        await asyncio.sleep(0.1)
        started = time.perf_counter()
        await pool.stop(timeout=2.0)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        return pids, time.perf_counter() - started

    pids, took = asyncio.run(main())
    assert took < 10
    deadline = time.monotonic() + 10
    alive = set(pids)
    while alive and time.monotonic() < deadline:
        for pid in list(alive):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive.discard(pid)
        time.sleep(0.05)
    assert not alive, alive


def test_stop_does_not_cancel_innocent_queued_callers(process_pool):
    """Verifier 2026-09-15: stop() shuts the executor down with
    cancel_futures=True. A caller whose call was still queued, and whose task
    nobody cancelled, must get its result (thread re-run), not end cancelled."""
    pool = process_pool(1, 8)

    async def main():
        await pool.run(cpu_pool.child_pid)
        tasks = [asyncio.ensure_future(pool.run(cpu_pool.burn, 0.3)) for _ in range(6)]
        await asyncio.sleep(0.1)
        await pool.stop(timeout=3.0)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return tasks, results

    tasks, results = asyncio.run(main())
    assert not any(t.cancelled() for t in tasks), results
    assert all(isinstance(r, int) for r in results), results


def test_a_cancelled_caller_still_sees_its_cancellation(process_pool):
    pool = process_pool(1, 4)

    async def main():
        await pool.run(cpu_pool.child_pid)
        busy = asyncio.ensure_future(pool.run(cpu_pool.burn, 0.3))
        queued = asyncio.ensure_future(pool.run(cpu_pool.burn, 0.3))
        await asyncio.sleep(0.05)
        queued.cancel()
        await asyncio.gather(busy, queued, return_exceptions=True)
        return queued

    queued = asyncio.run(main())
    assert queued.cancelled()


def test_prestart_spawns_children_off_the_loop(process_pool):
    pool = process_pool(2, 2)
    assert pool.prestart() >= 1
    procs = list((getattr(pool._executor, "_processes", None) or {}).values())
    assert procs and all(p.is_alive() for p in procs)
    assert process_pool(0, 1).prestart() == 0
