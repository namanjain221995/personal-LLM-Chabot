"""app/shutdown_signals.py — SIGTERM reaches the durable subsystem at +0 s (2026-09-13).

What is pinned:

- `install` chains: the callback runs on the event loop AND the handler that
  was there before (uvicorn's) still runs; twice is one chain; `uninstall`
  restores the previous handler; off the main thread it installs nothing;
- END TO END, a real uvicorn process with --timeout-graceful-shutdown 90 and
  an endless streaming response whose reader is aborted by the chained
  callback (what durable.suspend_all does to /v1 readers): SIGTERM gives the
  client an incomplete read (httpx.RemoteProtocolError) and the process exits
  in under 3 s. Without the chain the same process is still serving the
  stream 3 s later — which is what makes the first half a real test.
"""
from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import httpx
import pytest

from app import shutdown_signals

ORCHESTRATOR_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture()
def restore_handlers():
    saved = {sig: signal.getsignal(sig) for sig in shutdown_signals.HANDLED}
    yield
    shutdown_signals.uninstall()
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def test_install_chains_the_callback_in_front_of_the_previous_handler(restore_handlers):
    previous_calls: list = []
    heard: list = []

    def previous(signum, frame):
        previous_calls.append(signum)

    signal.signal(signal.SIGTERM, previous)

    async def run():
        loop_thread = threading.get_ident()

        def callback(name):
            heard.append((name, threading.get_ident() == loop_thread))

        assert shutdown_signals.install(callback) is True
        assert shutdown_signals.install(callback) is True  # idempotent
        handler = signal.getsignal(signal.SIGTERM)
        assert isinstance(handler, shutdown_signals._Chained)
        assert handler.previous is previous, "chained once, not onto itself"
        signal.raise_signal(signal.SIGTERM)
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert previous_calls == [signal.SIGTERM], "uvicorn's handler still runs"
    assert heard == [("SIGTERM", True)], "the callback ran once, on the loop"
    shutdown_signals.uninstall()
    assert signal.getsignal(signal.SIGTERM) is previous


def test_a_coroutine_callback_is_scheduled_on_the_loop(restore_handlers):
    signal.signal(signal.SIGTERM, lambda s, f: None)
    done: list = []

    async def run():
        async def callback(name):
            await asyncio.sleep(0)
            done.append(name)

        shutdown_signals.install(callback)
        signal.raise_signal(signal.SIGTERM)
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert done == ["SIGTERM"]


def test_off_the_main_thread_nothing_is_installed(restore_handlers):
    result: list = []

    def worker():
        async def run():
            result.append(shutdown_signals.install(lambda name: None))

        asyncio.run(run())

    t = threading.Thread(target=worker)
    t.start()
    t.join(5)
    assert result == [False]
    assert not isinstance(signal.getsignal(signal.SIGTERM), shutdown_signals._Chained)


_APP = textwrap.dedent(
    """
    import asyncio, contextlib, os
    from starlette.applications import Starlette
    from starlette.responses import StreamingResponse, PlainTextResponse
    from starlette.routing import Route
    from app import shutdown_signals

    suspended = None

    @contextlib.asynccontextmanager
    async def lifespan(app):
        global suspended
        suspended = asyncio.Event()
        if os.environ.get("CHAIN") == "1":
            shutdown_signals.install(lambda name: suspended.set())
        yield

    async def stream(request):
        async def body():
            while True:
                if suspended.is_set():
                    # what durable.suspend_all does to a /v1 reader: raise in
                    # its body iterator, an incomplete chunked read
                    raise RuntimeError("suspended for restart")
                yield b"data: tick\\n\\n"
                await asyncio.sleep(0.1)
        return StreamingResponse(body(), media_type="text/event-stream")

    async def ready(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/stream", stream), Route("/ready", ready)], lifespan=lifespan)
    """
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(tmp_path: Path, chain: bool):
    (tmp_path / "sigapp.py").write_text(_APP)
    port = _free_port()
    env = dict(os.environ, CHAIN="1" if chain else "0",
               PYTHONPATH=f"{tmp_path}{os.pathsep}{ORCHESTRATOR_DIR}")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "sigapp:app", "--host", "127.0.0.1", "--port", str(port),
         "--timeout-graceful-shutdown", "90", "--log-level", "warning"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/ready", timeout=0.5).status_code == 200:
                return proc, port
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.kill()
    raise AssertionError("uvicorn did not start: " + proc.stdout.read().decode(errors="replace"))


def _stream_then_sigterm(proc, port):
    """Read two frames, SIGTERM the server, then keep reading. Returns the
    exception the client saw (or None) and seconds from the signal to exit
    (None when still running after 3 s)."""
    seen = None
    signalled_at = None
    try:
        with httpx.stream("GET", f"http://127.0.0.1:{port}/stream", timeout=httpx.Timeout(10.0)) as response:
            frames = 0
            for _chunk in response.iter_raw():
                frames += 1
                if frames == 2:
                    signalled_at = time.monotonic()
                    proc.send_signal(signal.SIGTERM)
                if signalled_at is not None and time.monotonic() - signalled_at > 3.0:
                    break
    except httpx.HTTPError as exc:
        seen = exc
    try:
        proc.wait(timeout=max(0.0, 3.0 - (time.monotonic() - signalled_at)))
        exited = time.monotonic() - signalled_at
    except subprocess.TimeoutExpired:
        exited = None
    return seen, exited


def test_sigterm_aborts_a_durable_reader_at_once_and_uvicorn_exits_in_under_3s(tmp_path):
    proc, port = _serve(tmp_path, chain=True)
    try:
        seen, exited = _stream_then_sigterm(proc, port)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(5)
    assert isinstance(seen, httpx.RemoteProtocolError), repr(seen)
    assert exited is not None and exited < 3.0, exited


def test_without_the_chain_the_same_stream_holds_the_process_past_3s(tmp_path):
    proc, port = _serve(tmp_path, chain=False)
    try:
        seen, exited = _stream_then_sigterm(proc, port)
        still_running = proc.poll() is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(5)
    assert seen is None, "the client kept reading: nothing aborted it"
    assert exited is None and still_running, "uvicorn's 90 s grace held the process"
