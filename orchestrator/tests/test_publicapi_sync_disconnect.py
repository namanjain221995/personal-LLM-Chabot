"""An abandoned synchronous `/v1` request gives its capacity back at once
(adversarial review 2026-09-13).

A non-streaming handler is not cancelled by uvicorn when its client resets
the connection, so the synchronous path used to hold its capacity gate, its
concurrency slot, its admission slot and the engine until a generation nobody
would read had finished — 9.4 s after the client left for a 10 s answer; for a
synchronous 1,000,000-token request (documented unsuitable, not refused) the
single `main.long` gate for up to 5 h 48 min, long after Cloudflare had
answered the caller 524. Streams already released at once and still do.

The route test runs a REAL uvicorn and resets a REAL socket, because that is
the only place the missing cancellation is visible: an in-process ASGI client
cancels the handler itself.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from typing import Any, Dict, List, Mapping

import pytest
import uvicorn

from app import db, llm
from app.apiplatform import quotas
from app.config import settings
from app.publicapi import capacity, errors, streaming
from tests.test_publicapi_routes import (  # noqa: F401
    TOKENS,
    _bare_app,
    _caller,
    _pepper,
    platform,
)
from app.publicapi import router as public_router


class _HangsAfterAToken:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.closed = threading.Event()
        self.calls = 0

    def __call__(self, messages, **kwargs):
        self.calls += 1
        return self._run()

    async def _run(self):
        try:
            yield ("token", "partial ")
            self.started.set()
            await asyncio.sleep(3600)
            yield ("token", "never")
        finally:
            self.closed.set()


def _spec(**overrides) -> streaming.GenerationSpec:
    base: Dict[str, Any] = dict(
        response_id="resp_gone",
        model="techsara-35b",
        messages=[{"role": "user", "content": "write"}],
        max_tokens=200_000,
        temperature=0.2,
        created_at=1789200000,
        engine="main",
        wall_clock_s=5000.0,
        requested_max_output_tokens=200_000,
        planned_max_output_tokens=200_000,
        context_window=1_000_000,
        context_reserve=512,
    )
    base.update(overrides)
    return streaming.GenerationSpec(**base)


def test_the_generation_is_cancelled_and_its_partial_outcome_kept_when_the_client_disconnects(monkeypatch):
    engine = _HangsAfterAToken()
    monkeypatch.setattr(llm, "stream_chat_events", engine)
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: None)

    async def scenario():
        async def receive() -> Mapping[str, Any]:
            while not engine.started.is_set():
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            return {"type": "http.disconnect"}

        outcome = streaming.new_outcome(_spec())
        started = time.monotonic()
        with pytest.raises(streaming.ClientGone):
            await streaming.run_to_completion_watching(_spec(), receive=receive, outcome=outcome)
        return outcome, time.monotonic() - started

    outcome, elapsed = asyncio.run(asyncio.wait_for(scenario(), 10))
    assert elapsed < 2
    assert engine.closed.is_set()
    assert outcome.status == "cancelled" and outcome.client_gone is True
    assert outcome.text == "partial "


def test_a_request_body_message_is_not_mistaken_for_a_disconnect_and_the_answer_is_returned(monkeypatch):
    def engine(messages, **kwargs):
        async def run():
            await asyncio.sleep(0.2)
            yield ("token", "done")

        return run()

    monkeypatch.setattr(llm, "stream_chat_events", engine)
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: None)

    async def scenario():
        async def receive() -> Mapping[str, Any]:
            await asyncio.sleep(0.01)
            return {"type": "http.request", "body": b"", "more_body": False}

        return await streaming.run_to_completion_watching(
            _spec(), receive=receive, outcome=streaming.new_outcome(_spec())
        )

    outcome = asyncio.run(asyncio.wait_for(scenario(), 10))
    assert outcome.status == "completed" and outcome.text == "done"


def test_a_receive_that_cannot_be_watched_never_ends_the_generation(monkeypatch):
    def engine(messages, **kwargs):
        async def run():
            await asyncio.sleep(0.1)
            yield ("token", "done")

        return run()

    monkeypatch.setattr(llm, "stream_chat_events", engine)
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: None)

    async def scenario():
        async def receive() -> Mapping[str, Any]:
            raise RuntimeError("receive is not available here")

        return await streaming.run_to_completion_watching(
            _spec(), receive=receive, outcome=streaming.new_outcome(_spec())
        )

    outcome = asyncio.run(asyncio.wait_for(scenario(), 10))
    assert outcome.status == "completed" and outcome.text == "done"


@pytest.fixture()
def served_api(platform):
    """The public router on a real uvicorn, in a thread."""
    app = _bare_app()
    public_router.install_error_handlers(app)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield port
    server.should_exit = True
    thread.join(10)


def _raw_post_then_reset(port: int, path: str, body: Dict[str, Any], engine: _HangsAfterAToken) -> None:
    payload = json.dumps(body).encode()
    request = (
        f"POST {path} HTTP/1.1\r\nHost: api.test\r\nContent-Type: application/json\r\n"
        f"Authorization: Bearer {TOKENS['live']}\r\nContent-Length: {len(payload)}\r\n\r\n"
    ).encode() + payload
    conn = socket.create_connection(("127.0.0.1", port), timeout=10)
    conn.sendall(request)
    assert engine.started.wait(15), "the generation never started"
    # RST, like a proxy giving up: SO_LINGER 0 then close.
    import struct

    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    conn.close()


def _wait(predicate, timeout: float) -> float:
    started = time.monotonic()
    while not predicate():
        if time.monotonic() - started > timeout:
            raise AssertionError("condition not reached in time")
        time.sleep(0.02)
    return time.monotonic() - started


def test_an_abandoned_synchronous_long_request_gives_its_gate_and_slot_back_within_a_second(
    served_api, platform, monkeypatch
):
    engine = _HangsAfterAToken()
    monkeypatch.setattr(llm, "stream_chat_events", engine)
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: None)
    monkeypatch.setattr(settings, "model_max_context", 1_000_000)
    capacity.reset_for_tests()

    _raw_post_then_reset(
        served_api,
        "/v1/responses",
        {"model": "techsara-35b", "input": "Write a book.", "max_output_tokens": 200_000},
        engine,
    )
    assert capacity.snapshot()["main.long"]["in_flight"] == 1
    released = _wait(lambda: capacity.snapshot()["main.long"]["in_flight"] == 0, 10)

    assert released < 1.0
    assert engine.closed.wait(5)
    assert quotas.in_flight(_caller()) == 0
    project_id = platform["project"]["id"]
    _wait(lambda: [row["status"] for row in db.list_api_responses(project_id)] == ["cancelled"], 10)
    # And the next long request is admitted rather than refused at capacity.
    assert capacity.snapshot()["main.long"]["waiting"] == 0
