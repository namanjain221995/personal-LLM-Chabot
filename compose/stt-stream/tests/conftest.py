"""Harness: the real server module around fake recognizers.

server.py is loaded as `stt_stream_server` (not `server`, a name too common to
own in a shared test session). Nothing here needs sherpa-onnx, a model, a GPU
or the network: the engine runs in-process through Starlette's TestClient and
its decode workers drive stt_fakes.FakeRecognizer.

Starlette's test session has no receive timeout, so every read here is bounded
(`recv`): a server that forgets to answer fails its test instead of hanging it.

    python -m pytest -q -p no:cacheprovider compose/stt-stream/tests
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from contextlib import contextmanager
from typing import Iterator, List, Optional, Sequence, Tuple

import anyio
import numpy as np
from starlette.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from stt_fakes import fake_factory  # noqa: E402


def _load_server():
    existing = sys.modules.get("stt_stream_server")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("stt_stream_server", os.path.join(ENGINE_DIR, "server.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["stt_stream_server"] = module
    spec.loader.exec_module(module)
    return module


server = _load_server()

#: Low entropy on purpose: the secret scanner blocks random-looking fixtures.
TOKEN = "test-key-1-test-key-1"
AUTH = {"authorization": f"Bearer {TOKEN}"}


def make_profile(pid: str = "fast", chunk_ms: int = 160, max_streams: int = 4,
                 languages: Sequence[str] = ("auto", "en", "hi"), flush_pad_ms: Optional[int] = None):
    pad = server.default_flush_pad_ms(chunk_ms) if flush_pad_ms is None else flush_pad_ms
    return server.Profile(pid, f"/models/{pid}", chunk_ms, max_streams, tuple(languages), pad, f"model-{pid}")


def make_settings(**overrides):
    values = dict(
        profiles=(make_profile(),), token=TOKEN, workers=2, threads=1,
        idle_s=10.0, start_timeout_s=10.0, resume_max_s=600.0,
    )
    values.update(overrides)
    return server.Settings(**values)


def wait_ready(client: TestClient, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get("/health").json()["ready"]:
            return
        time.sleep(0.005)
    raise AssertionError("the engine never became ready")


@contextmanager
def running(settings=None, factory=None, *, ready: bool = True) -> Iterator[TestClient]:
    app = server.create_app(settings or make_settings(), factory or fake_factory())
    with TestClient(app) as client:
        if ready:
            wait_ready(client)
        yield client


def recv(ws, timeout: float = 10.0) -> dict:
    """The next message from the server, or AssertionError after `timeout`."""

    async def _next():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()

    try:
        return ws.portal.call(_next)
    except TimeoutError:
        raise AssertionError(f"no message from the engine within {timeout} s") from None


class Closed(Exception):
    def __init__(self, code: Optional[int]):
        super().__init__(f"closed {code}")
        self.code = code


def recv_json(ws, timeout: float = 10.0) -> dict:
    message = recv(ws, timeout)
    if message["type"] == "websocket.close":
        raise Closed(message.get("code"))
    return json.loads(message["text"])


def start(ws, **fields) -> dict:
    """Send a start message and return the engine's `ready`."""
    message = {"type": "start", "sample_rate": 16000, "encoding": "pcm_s16le",
               "first_sample": 0, "first_u": 0, "mode": "dictation"}
    message.update(fields)
    ws.send_text(json.dumps(message))
    ready = recv_json(ws)
    assert ready["type"] == "ready", ready
    return ready


def send_pcm(ws, pcm: np.ndarray, frame: int = 640) -> None:
    data = np.asarray(pcm, dtype="<i2")
    for i in range(0, len(data), frame):
        ws.send_bytes(data[i:i + frame].tobytes())


def drain(ws, timeout: float = 10.0) -> Tuple[List[dict], Optional[int]]:
    """Every event until the engine closes, and the close code."""
    events: List[dict] = []
    while True:
        message = recv(ws, timeout)
        if message["type"] == "websocket.close":
            return events, message.get("code")
        events.append(json.loads(message["text"]))


def finals(events: Sequence[dict]) -> List[dict]:
    return [e for e in events if e["type"] == "final"]


def words_of(events: Sequence[dict]) -> List[str]:
    """Every word the finals carry, in order (punctuation dropped)."""
    out: List[str] = []
    for event in finals(events):
        out.extend(w for w in event["text"].replace(",", " ").replace(".", " ").split())
    return out


def metrics_value(client: TestClient, series: str) -> float:
    for line in client.get("/metrics").text.splitlines():
        if line.startswith(series + " "):
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"{series} is not exported")
