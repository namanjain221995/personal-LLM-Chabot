"""No `/v1` response is silent for more than 15 seconds (no-timeout design,
2026-09-13; CONTRACT §10 byte invariant).

WHAT IS PINNED, AND ON WHAT.

* `keepalive.CommittedJSONResponse` on a REAL uvicorn over a REAL socket,
  because what matters is when bytes leave the process: the first byte by the
  commit window, no gap longer than the heartbeat, the object last, and a
  failure after the commit expressed in the body or as a dropped connection.
  An in-process ASGI client buffers the body and would prove none of it.
* The generating routes on the same real uvicorn with the real platform
  (keys, quotas, rows) and a stubbed engine: a slow synchronous generation
  commits and completes, a failure after the commit is a failed object with
  its partial output, a stream opens at once while capacity is held elsewhere
  and waits in its body, and a client that leaves while waiting gives its
  place in the line back.

The windows are scaled (commit 0.3 s, heartbeat 0.2 s) so the suite stays
fast; the 15 s defaults are pinned separately, and the real-time proof with
openai-python and openai-node at their default settings lives in the T3-wire
hand-over (a 400 s synchronous call and a 300 s silent stream gap).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from app import admission, db, llm
from app.config import settings
from app.publicapi import capacity, errors, events, keepalive, registry
from app.publicapi import router as public_router
from tests.test_publicapi_routes import (  # noqa: F401 - fixtures
    TOKENS,
    _bare_app,
    _pepper,
    platform,
)

COMMIT_S = 0.3
BEAT_S = 0.2
#: Scheduling slack on a loaded CI box. The assertions are about seconds of
#: silence in production, so a quarter of a second of slack proves the rule.
SLACK_S = 0.25


# ------------------------------------------------------------ plumbing --


@contextlib.contextmanager
def serve(app: Any) -> Iterator[int]:
    """`app` on a real uvicorn in a thread; yields the port."""
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
        time.sleep(0.02)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(10)


def timed_post(port: int, path: str, *, json_body: Any = None, headers: Optional[Dict[str, str]] = None):
    """POST and read the raw body chunk by chunk, with arrival times.

    Returns (response, [(seconds since the request was sent, bytes)], error)
    — `error` is the exception a dropped connection raised, else None."""
    chunks: List[Tuple[float, bytes]] = []
    started = time.monotonic()
    error: Optional[BaseException] = None
    with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
        with client.stream("POST", f"http://127.0.0.1:{port}{path}", json=json_body, headers=headers or {}) as response:
            try:
                for chunk in response.iter_raw():
                    chunks.append((time.monotonic() - started, chunk))
            except httpx.HTTPError as exc:
                error = exc
    return response, chunks, error


def gaps(chunks: List[Tuple[float, bytes]]) -> List[float]:
    times = [0.0] + [at for at, _ in chunks]
    return [b - a for a, b in zip(times, times[1:])]


def body_of(chunks: List[Tuple[float, bytes]]) -> bytes:
    return b"".join(chunk for _, chunk in chunks)


def keepalive_app(work, **kwargs) -> FastAPI:
    app = FastAPI()
    options = {"commit_s": COMMIT_S, "heartbeat_s": BEAT_S, **kwargs}

    @app.post("/work")
    async def endpoint():
        response = keepalive.CommittedJSONResponse(work, **options)
        # What PublicRoute._decorate does after the handler returns.
        response.headers["X-Request-Id"] = "req_decorated"
        return response

    return app


# ------------------------------------------------ CommittedJSONResponse --


def test_a_quick_answer_keeps_its_real_status_body_and_length():
    async def work():
        return {"object": "thing", "n": 1}

    with serve(keepalive_app(work)) as port:
        response, chunks, error = timed_post(port, "/work")
    assert error is None
    assert response.status_code == 200
    assert json.loads(body_of(chunks)) == {"object": "thing", "n": 1}
    assert response.headers["content-length"] == str(len(body_of(chunks)))
    assert response.headers["x-request-id"] == "req_decorated"
    assert not body_of(chunks).startswith(b" ")


def test_a_quick_refusal_keeps_its_status_its_retry_after_and_the_envelope():
    async def work():
        raise errors.model_unavailable(retry_after=30)

    with serve(keepalive_app(work, request_id="req_decorated")) as port:
        response, chunks, _ = timed_post(port, "/work")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert response.headers["x-request-id"] == "req_decorated"
    envelope = json.loads(body_of(chunks))
    assert envelope["error"]["code"] == "model_unavailable"
    assert envelope["error"]["request_id"] == "req_decorated"


def test_a_slow_answer_commits_a_200_within_the_window_and_breathes_until_the_object():
    async def work():
        await asyncio.sleep(1.5)
        return {"object": "thing", "done": True}

    with serve(keepalive_app(work)) as port:
        response, chunks, error = timed_post(port, "/work")

    assert error is None
    assert response.status_code == 200
    assert chunks[0][0] <= COMMIT_S + SLACK_S, chunks[0]
    assert chunks[0][1] == b" "
    assert max(gaps(chunks)[1:]) <= BEAT_S + SLACK_S, gaps(chunks)
    body = body_of(chunks)
    assert body.strip() != body and json.loads(body) == {"object": "thing", "done": True}
    assert response.headers["cache-control"] == "no-store, no-transform"
    assert response.headers["content-type"] == "application/json"
    assert "content-length" not in response.headers
    assert response.headers["x-request-id"] == "req_decorated"
    assert len([c for _, c in chunks if c == b" "]) >= 5


def test_a_failure_after_the_commit_is_the_routes_failed_object_in_the_body():
    async def work():
        await asyncio.sleep(0.8)
        raise errors.model_unavailable(retry_after=30)

    def failed(exc):
        return {"object": "response", "status": "failed", "error": {"code": exc.code, "message": exc.message}}

    with serve(keepalive_app(work, failed_body=failed)) as port:
        response, chunks, error = timed_post(port, "/work")
    assert error is None and response.status_code == 200
    assert json.loads(body_of(chunks)) == {
        "object": "response",
        "status": "failed",
        "error": {"code": "model_unavailable", "message": "The model is not available at the moment."},
    }


@pytest.mark.parametrize("mode", [keepalive.FAILURE_ABORT, keepalive.FAILURE_BODY])
def test_a_failure_after_the_commit_without_a_failed_body_drops_the_connection(mode, caplog):
    async def work():
        await asyncio.sleep(0.8)
        raise errors.internal_error()

    kwargs = {"failure_mode": mode}
    if mode == keepalive.FAILURE_BODY:
        kwargs["failed_body"] = lambda exc: None  # a builder with nothing to say aborts too
    with serve(keepalive_app(work, **kwargs)) as port:
        response, chunks, error = timed_post(port, "/work")
    assert response.status_code == 200
    assert isinstance(error, httpx.RemoteProtocolError), error
    assert body_of(chunks).strip() == b""


def test_a_non_2xx_response_returned_after_the_commit_is_a_failure_not_a_body():
    from starlette.responses import JSONResponse

    async def work():
        await asyncio.sleep(0.8)
        return JSONResponse({"error": {"code": "x"}}, status_code=500)

    with serve(keepalive_app(work, failure_mode=keepalive.FAILURE_ABORT)) as port:
        _response, chunks, error = timed_post(port, "/work")
    assert isinstance(error, httpx.RemoteProtocolError)
    assert body_of(chunks).strip() == b""


@pytest.mark.parametrize("leave_after_s", [0.1, 0.8])
def test_a_client_that_leaves_before_or_after_the_commit_cancels_the_work(leave_after_s):
    cancelled = threading.Event()

    async def work():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {}

    with serve(keepalive_app(work)) as port:
        conn = socket.create_connection(("127.0.0.1", port), timeout=5)
        conn.sendall(b"POST /work HTTP/1.1\r\nHost: t\r\nContent-Length: 0\r\n\r\n")
        time.sleep(leave_after_s)
        left = time.monotonic()
        conn.close()
        assert cancelled.wait(5), "the work kept running for a client that left"
        assert time.monotonic() - left < 1.5


def test_the_commit_window_sits_inside_fifteen_seconds_and_can_never_be_configured_past_them(monkeypatch):
    monkeypatch.delenv("PUBLIC_API_SYNC_COMMIT_S", raising=False)
    if hasattr(settings, "public_api_sync_commit_s"):
        monkeypatch.delattr(settings, "public_api_sync_commit_s")
    assert keepalive.sync_commit_s() == 12.0
    monkeypatch.setattr(settings, "public_api_sync_commit_s", 90.0, raising=False)
    assert keepalive.sync_commit_s() == 15.0
    monkeypatch.setattr(settings, "public_api_sync_commit_s", 2.0, raising=False)
    assert keepalive.sync_commit_s() == 2.0
    # The heartbeat leaves a second for a late timer inside the 15 s promise.
    assert keepalive.heartbeat_s() <= 14.0 and events.HEARTBEAT_SECONDS <= 14.0


# -------------------------------------------------- the generating routes --


class _SlowEngine:
    """`llm.stream_chat_events` that is silent, then speaks — or fails."""

    def __init__(self, *, silent_s: float, pieces=("Hello", " world"), fail_after: Optional[BaseException] = None,
                 hold_first: Optional[threading.Event] = None) -> None:
        self.silent_s = silent_s
        self.pieces = pieces
        self.fail_after = fail_after
        self.hold_first = hold_first
        self.calls = 0
        self.started = threading.Event()

    def __call__(self, messages, **kwargs):
        self.calls += 1
        return self._run(self.calls)

    async def _run(self, call: int):
        self.started.set()
        if self.hold_first is not None and call == 1:
            deadline = time.monotonic() + 20
            while not self.hold_first.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
        else:
            await asyncio.sleep(self.silent_s)
        for piece in self.pieces:
            yield ("token", piece)
        if self.fail_after is not None:
            await asyncio.sleep(self.silent_s)
            raise self.fail_after


@pytest.fixture()
def scaled(monkeypatch):
    monkeypatch.setattr(settings, "public_api_sync_commit_s", COMMIT_S, raising=False)
    monkeypatch.setattr(events, "HEARTBEAT_SECONDS", BEAT_S)
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: None)
    capacity.reset_for_tests()
    yield
    capacity.reset_for_tests()


@pytest.fixture()
def api_port(platform, scaled):
    app = _bare_app()
    public_router.install_error_handlers(app)
    with serve(app) as port:
        yield port


def _auth(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {"Authorization": f"Bearer {TOKENS['live']}"}
    headers.update(extra or {})
    return headers


def test_a_slow_synchronous_generation_commits_and_returns_the_completed_response(api_port, platform, monkeypatch):
    engine = _SlowEngine(silent_s=1.2)
    monkeypatch.setattr(llm, "stream_chat_events", engine)

    response, chunks, error = timed_post(
        api_port, "/v1/responses", json_body={"model": registry.TECHSARA_35B, "input": "hi"}, headers=_auth()
    )
    assert error is None and response.status_code == 200
    assert chunks[0][0] <= COMMIT_S + SLACK_S
    assert max(gaps(chunks)[1:]) <= BEAT_S + SLACK_S
    body = json.loads(body_of(chunks))
    assert body["status"] == "completed"
    assert body["output"][0]["content"][0]["text"] == "Hello world"
    assert response.headers["x-request-id"].startswith("req_")
    rows = db.list_api_responses(platform["project"]["id"])
    assert [row["status"] for row in rows] == ["completed"]


def test_a_synchronous_chat_completion_that_fails_after_the_commit_is_choices_empty_with_the_error(
    api_port, platform, monkeypatch
):
    engine = _SlowEngine(silent_s=0.6, pieces=("partial ",), fail_after=RuntimeError("engine at 10.0.0.7 died"))
    monkeypatch.setattr(llm, "stream_chat_events", engine)

    response, chunks, error = timed_post(
        api_port,
        "/v1/chat/completions",
        json_body={"model": registry.TECHSARA_35B, "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )
    assert error is None and response.status_code == 200
    body = json.loads(body_of(chunks))
    assert body["object"] == "chat.completion"
    assert body["choices"] == []
    assert body["error"]["code"] == "internal_error"
    assert set(body["error"]) == {"message", "type", "code", "param"}
    assert "10.0.0.7" not in body_of(chunks).decode()
    assert body["id"].startswith("chatcmpl_")
    _wait_for(lambda: [row["status"] for row in db.list_api_responses(platform["project"]["id"])] == ["failed"])


def test_a_synchronous_response_that_fails_after_the_commit_carries_no_partial_text_but_keeps_its_usage(
    api_port, platform, monkeypatch
):
    """Adversarial review 2026-09-14: a failed body whose `output` held the
    partial message let `resp.output_text` hand back a truncated answer with
    no exception. The failed object says `failed`, the error and the usage —
    and nothing a caller could mistake for the answer."""
    usage = {"prompt_tokens": 3, "completion_tokens": 2}
    monkeypatch.setattr(llm, "get_usage", lambda: dict(usage))
    engine = _SlowEngine(silent_s=0.6, pieces=("partial", " answer"), fail_after=RuntimeError("boom"))
    monkeypatch.setattr(llm, "stream_chat_events", engine)

    response, chunks, error = timed_post(
        api_port, "/v1/responses", json_body={"model": registry.TECHSARA_35B, "input": "hi"}, headers=_auth()
    )
    assert error is None and response.status_code == 200
    body = json.loads(body_of(chunks))
    assert body["status"] == "failed"
    assert body["error"]["code"] == "internal_error"
    assert body["output"] == []
    assert b"partial" not in body_of(chunks)
    assert body["usage"]["output_tokens"] == 2


def test_a_quick_500_after_the_generation_started_says_do_not_retry_only_without_a_key(
    api_port, platform, monkeypatch
):
    engine = _SlowEngine(silent_s=0.0, pieces=(), fail_after=RuntimeError("boom"))
    monkeypatch.setattr(llm, "stream_chat_events", engine)
    monkeypatch.setattr(settings, "public_api_sync_commit_s", 5.0, raising=False)
    body = {"model": registry.TECHSARA_35B, "input": "hi"}

    unkeyed, chunks, _ = timed_post(api_port, "/v1/responses", json_body=body, headers=_auth())
    assert unkeyed.status_code == 500
    assert unkeyed.headers["x-should-retry"] == "false"
    assert json.loads(body_of(chunks))["error"]["code"] == "internal_error"

    keyed, _, _ = timed_post(
        api_port, "/v1/responses", json_body=body, headers=_auth({"Idempotency-Key": "k-1789"})
    )
    assert keyed.status_code == 500
    assert "x-should-retry" not in keyed.headers


def test_openai_python_reads_a_committed_body_and_a_committed_failure(api_port, platform, monkeypatch):
    openai = pytest.importorskip("openai")
    client = openai.OpenAI(base_url=f"http://127.0.0.1:{api_port}/v1", api_key=TOKENS["live"], max_retries=0)

    monkeypatch.setattr(llm, "stream_chat_events", _SlowEngine(silent_s=1.0))
    completed = client.responses.create(model=registry.TECHSARA_35B, input="hi")
    assert completed.status == "completed" and completed.output_text == "Hello world"
    chat = client.chat.completions.create(model=registry.TECHSARA_35B, messages=[{"role": "user", "content": "hi"}])
    assert chat.choices[0].message.content == "Hello world"

    monkeypatch.setattr(
        llm, "stream_chat_events", _SlowEngine(silent_s=0.6, pieces=("partial ",), fail_after=RuntimeError("x"))
    )
    failed = client.responses.create(model=registry.TECHSARA_35B, input="hi")
    assert failed.status == "failed" and failed.error.code == "internal_error"
    # The canonical read of an answer gives nothing back for a failed one.
    assert failed.output_text == ""
    failed_chat = client.chat.completions.create(
        model=registry.TECHSARA_35B, messages=[{"role": "user", "content": "hi"}]
    )
    assert failed_chat.choices == []
    assert failed_chat.model_extra["error"]["code"] == "internal_error"


# ------------------------------------------- capacity waits in the body --

#: Planned above PUBLIC_API_MAIN_SOLO_OUTPUT_TOKENS (800,000), so it takes
#: `main.long`, one at a time. PR #65 (merged 2026-09-14) picks the main gate
#: from the planned output only; 200,000 — this test's number before — now
#: plans `main.extended`, whose cap is admission's two LONG_OUTPUT seats.
LONG = {"model": registry.TECHSARA_35B, "input": "Write a book.", "max_output_tokens": 900_000}


def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.02)


def _hold_the_long_gate(port: int, engine: _SlowEngine) -> threading.Thread:
    """A first long request that holds `main.long` until the engine's event is set."""
    def run():
        with contextlib.suppress(Exception):
            timed_post(port, "/v1/responses", json_body=LONG, headers=_auth())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    _wait_for(lambda: capacity.snapshot().get("main.long", {}).get("in_flight") == 1)
    assert engine.started.wait(5)
    return thread


@pytest.mark.parametrize("path, body, opener", [
    ("/v1/responses", dict(LONG, stream=True), b"event: response.created"),
    ("/v1/chat/completions", {"model": registry.TECHSARA_35B, "stream": True, "max_tokens": 900_000,
                              "messages": [{"role": "user", "content": "Write a book."}]}, b": ping"),
])
def test_a_stream_opens_at_once_and_waits_for_capacity_in_its_body(api_port, platform, monkeypatch, path, body, opener):
    monkeypatch.setattr(settings, "model_max_context", 1_000_000)
    release = threading.Event()
    engine = _SlowEngine(silent_s=0.0, hold_first=release)
    monkeypatch.setattr(llm, "stream_chat_events", engine)
    holder = _hold_the_long_gate(api_port, engine)

    received: List[Tuple[float, bytes]] = []
    started = time.monotonic()
    with httpx.Client(timeout=30) as client:
        with client.stream("POST", f"http://127.0.0.1:{api_port}{path}", json=body, headers=_auth()) as response:
            assert response.status_code == 200
            headers_at = time.monotonic() - started
            stream = response.iter_raw()
            while time.monotonic() - started < 1.2:
                received.append((time.monotonic() - started, next(stream)))
            assert capacity.snapshot()["main.long"]["waiting"] == 1
            assert engine.calls == 1, "the waiting stream reached the engine before holding the gate"
            release.set()
            for chunk in stream:
                received.append((time.monotonic() - started, chunk))

    holder.join(10)
    text = body_of(received)
    assert headers_at < 1.0
    assert text.startswith(opener)
    assert received[0][0] <= SLACK_S
    assert text.count(b": queued") >= 3
    assert max(gaps(received)) <= BEAT_S + SLACK_S, gaps(received)
    assert b"Hello" in text
    assert engine.calls == 2


def test_a_synchronous_request_waiting_for_capacity_commits_instead_of_refusing(api_port, platform, monkeypatch):
    monkeypatch.setattr(settings, "model_max_context", 1_000_000)
    _set(monkeypatch, "PUBLIC_API_GATE_WAIT_S", 0.3)  # the old refusal bound: it no longer ends the wait
    release = threading.Event()
    engine = _SlowEngine(silent_s=0.0, hold_first=release)
    monkeypatch.setattr(llm, "stream_chat_events", engine)
    holder = _hold_the_long_gate(api_port, engine)

    threading.Timer(2.5, release.set).start()
    response, chunks, error = timed_post(api_port, "/v1/responses", json_body=LONG, headers=_auth())
    holder.join(10)

    assert error is None and response.status_code == 200
    assert chunks[0][0] <= COMMIT_S + SLACK_S
    assert max(gaps(chunks)[1:]) <= BEAT_S + SLACK_S
    assert json.loads(body_of(chunks))["status"] == "completed"


@pytest.mark.parametrize("path, body", [
    ("/v1/responses", dict(LONG, stream=True)),
    # The chat stream's generator is not even started while it waits: its
    # record has to come from the stream wrapper, or the row stays `queued`.
    ("/v1/chat/completions", {"model": registry.TECHSARA_35B, "stream": True, "max_tokens": 900_000,
                              "messages": [{"role": "user", "content": "Write a book."}]}),
])
def test_a_stream_abandoned_while_waiting_for_capacity_gives_its_place_back_and_is_recorded_cancelled(
    api_port, platform, monkeypatch, path, body
):
    monkeypatch.setattr(settings, "model_max_context", 1_000_000)
    release = threading.Event()
    engine = _SlowEngine(silent_s=0.0, hold_first=release)
    monkeypatch.setattr(llm, "stream_chat_events", engine)
    holder = _hold_the_long_gate(api_port, engine)
    try:
        with httpx.Client(timeout=30) as client:
            with client.stream("POST", f"http://127.0.0.1:{api_port}{path}", json=body, headers=_auth()) as response:
                stream = response.iter_raw()
                next(stream)
                _wait_for(lambda: capacity.snapshot()["main.long"]["waiting"] == 1)
        _wait_for(lambda: capacity.snapshot()["main.long"]["waiting"] == 0, timeout=5)
        assert capacity.snapshot()["main.long"]["in_flight"] == 1  # still the first request's
        assert engine.calls == 1
        project_id = platform["project"]["id"]
        _wait_for(
            lambda: sorted(row["status"] for row in db.list_api_responses(project_id))
            == ["cancelled", "in_progress"]
        )
    finally:
        release.set()
        holder.join(10)


# ------------------------------------ the gates and the admission line --
#
# Adversarial review of T3-wire, 2026-09-14 (high): a normal-size techsara-35b
# answer took NO public gate (`plan.gate_engine` is None for it) and waited in
# the shared NORMAL admission lane as a non-patient request, so after
# ADMISSION_NORMAL_WAIT_S (600 s) it failed — a clock ending a public wait —
# and it never took T2's `main.normal` gate (6 of chat's 10 NORMAL slots).

NORMAL = {"model": registry.TECHSARA_35B, "input": "hi"}
NORMAL_CHAT = {"model": registry.TECHSARA_35B, "messages": [{"role": "user", "content": "hi"}]}

@pytest.fixture()
def patience(monkeypatch):
    """`admission.patient()`: T1's own (assembler, 2026-09-14: the stand-in
    for a tree without T1 is no longer needed)."""
    return admission.patient


class _RecordingGates:
    """`capacity.gates_for` and T2's patient `capacity.hold`, recording what
    the router asked for (the order, and the deadline it passed)."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Optional[float]]] = []
        self.released: List[str] = []

    def gates_for(self, engine: str, gate_engine: Optional[str]) -> List[str]:
        if engine == registry.ENGINE_MAIN:
            if gate_engine == "main.long":
                return ["main.long"]
            if gate_engine == "main.extended":
                return ["main.extended", "main.normal"]
            return ["main.normal"]
        return [gate_engine] if gate_engine else []

    def hold(self, engine, *, weight_tokens=0, wait_s=None, yield_to_chat=False, abandon=None, on_wait=None, front=False,
             work=None):
        self.calls.append((engine, wait_s))

        @contextlib.asynccontextmanager
        async def held():
            try:
                yield
            finally:
                self.released.append(engine)

        return held()


class _PatienceEngine(_SlowEngine):
    """Records, per engine call, whether admission would see a patient waiter
    — read inside the generation's producer task, where llm.py reads it."""

    def __init__(self, patient_of, **kwargs) -> None:
        super().__init__(**kwargs)
        self.patient_of = patient_of
        self.patient: List[bool] = []

    async def _run(self, call: int):
        self.patient.append(bool(self.patient_of()))
        async for item in super()._run(call):
            yield item


@pytest.mark.parametrize("path, body", [
    ("/v1/responses", NORMAL),
    ("/v1/responses", dict(NORMAL, stream=True)),
    ("/v1/chat/completions", NORMAL_CHAT),
    ("/v1/chat/completions", dict(NORMAL_CHAT, stream=True)),
])
def test_a_normal_size_generation_holds_every_gate_capacity_names_with_no_deadline_and_waits_patiently(
    api_port, platform, monkeypatch, patience, path, body
):
    gates = _RecordingGates()
    monkeypatch.setattr(capacity, "gates_for", gates.gates_for, raising=False)
    monkeypatch.setattr(capacity, "hold", gates.hold)
    engine = _PatienceEngine(patience, silent_s=0.0)
    monkeypatch.setattr(llm, "stream_chat_events", engine)

    response, chunks, error = timed_post(api_port, path, json_body=body, headers=_auth())

    assert error is None and response.status_code == 200
    assert b"Hello" in body_of(chunks)
    assert gates.calls == [("main.normal", None)]
    _wait_for(lambda: gates.released == ["main.normal"])
    assert engine.patient == [True]
    assert public_router._GATE_LINE.waiting == {}


def test_gates_are_taken_in_capacitys_order_and_given_back_in_reverse(monkeypatch):
    from types import SimpleNamespace

    gates = _RecordingGates()
    monkeypatch.setattr(capacity, "gates_for", gates.gates_for, raising=False)
    monkeypatch.setattr(capacity, "hold", gates.hold)
    public_router._GATE_LINE.reset_for_tests()
    plan = SimpleNamespace(engine=registry.ENGINE_MAIN, gate_engine="main.extended", gate_weight_tokens=7, yield_to_chat=False)
    seen = {}

    async def scenario():
        async with public_router._patient_gate(plan):
            seen["line_while_held"] = dict(public_router._GATE_LINE.waiting)

    asyncio.run(scenario())
    assert gates.calls == [("main.extended", None), ("main.normal", None)]
    assert gates.released == ["main.normal", "main.extended"]
    assert seen["line_while_held"] == {}  # admitted: no longer counted as waiting
    assert public_router._GATE_LINE.waiting == {}


def test_the_router_takes_exactly_the_gates_capacity_names_for_a_plan():
    """Assembler, 2026-09-14: the pre-T2 fallback (the plan's own gate only)
    is removed; a normal-size techsara-35b answer holds main.normal."""
    from types import SimpleNamespace

    normal = SimpleNamespace(engine=registry.ENGINE_MAIN, gate_engine=None)
    long = SimpleNamespace(engine=registry.ENGINE_MAIN, gate_engine="main.long")
    assert public_router._plan_gates(normal) == capacity.gates_for(registry.ENGINE_MAIN, None) == ["main.normal"]
    assert public_router._plan_gates(long) == capacity.gates_for(registry.ENGINE_MAIN, "main.long")


def test_patient_admission_reaches_a_task_started_inside_it_and_ends_with_it(patience):
    async def scenario():
        outside = patience()
        with public_router.patient_admission():
            inner = await asyncio.ensure_future(_read(patience))
        after = patience()
        return outside, inner, after

    async def _read(reader):
        return reader()

    assert asyncio.run(scenario()) == (False, True, False)


def test_a_public_generation_queued_behind_a_full_normal_lane_past_its_wait_bound_still_completes(
    api_port, platform, monkeypatch
):
    """The review's scaled proof, inverted: ONE NORMAL slot, a 0.5 s NORMAL
    wait bound (production: 10 slots, 600 s), a first public answer holding
    the slot for ~1.5 s. The second waited past the bound and completed."""
    from app import context, engine_state

    monkeypatch.setattr(settings, "admission_normal_max", 1)
    monkeypatch.setattr(settings, "admission_normal_wait_s", 0.5)
    monkeypatch.setattr(settings, "model_max_context", 1_000_000)
    monkeypatch.setattr(admission, "_POLL_S", 0.02, raising=False)
    monkeypatch.setattr(engine_state, "engine_load", lambda: None)

    async def estimate_count(base_url, model, messages):
        return context.estimate_messages(messages), 1_000_000

    monkeypatch.setattr(context, "count_tokens", estimate_count)
    fake = _FakeOpenAIMain(tick=0.01)
    monkeypatch.setattr(llm, "_client", fake.client)
    admission.reset()

    results: Dict[str, Any] = {}

    def run(name: str, tokens: int) -> None:
        started = time.monotonic()
        response, chunks, error = timed_post(
            api_port, "/v1/responses", json_body=dict(NORMAL, max_output_tokens=tokens), headers=_auth()
        )
        results[name] = (response.status_code, json.loads(body_of(chunks)), time.monotonic() - started, error)

    first = threading.Thread(target=run, args=("first", 150))
    first.start()
    _wait_for(lambda: fake.running == 1)
    run("second", 5)
    first.join(20)

    status, body, waited, error = results["second"]
    assert error is None and status == 200
    assert body["status"] == "completed", body
    assert waited > 0.5 + 0.3, "the second answer did not wait behind the first"
    assert results["first"][1]["status"] == "completed"
    assert len(fake.requests) == 2


@pytest.mark.skipif(
    not hasattr(capacity, "GATE_MAIN_NORMAL"),
    reason="needs T2's main.normal gate (publicapi/capacity.py)",
)
def test_a_seventh_concurrent_public_normal_generation_waits_on_main_normal(api_port, platform, monkeypatch):
    _set(monkeypatch, "PUBLIC_API_MAIN_NORMAL_MAX_CONCURRENT", 6)  # the design's number
    release = threading.Event()
    engine = _GatedEngine(release)
    monkeypatch.setattr(llm, "stream_chat_events", engine)
    outcomes: List[bytes] = []

    def stream_one() -> None:
        _response, chunks, _error = timed_post(api_port, "/v1/responses", json_body=dict(NORMAL, stream=True), headers=_auth())
        outcomes.append(body_of(chunks))

    threads = [threading.Thread(target=stream_one) for _ in range(7)]
    for thread in threads:
        thread.start()
    try:
        _wait_for(lambda: capacity.snapshot().get("main.normal", {}).get("waiting") == 1)
        assert capacity.snapshot()["main.normal"]["in_flight"] == 6
        time.sleep(0.5)
        assert engine.calls == 6, "the seventh reached the engine without its main.normal slot"
    finally:
        release.set()
        for thread in threads:
            thread.join(20)
    assert len(outcomes) == 7
    assert all(b"response.completed" in body for body in outcomes)
    assert sum(b": queued" in body for body in outcomes) >= 1
    assert engine.calls == 7


class _GatedEngine:
    """`llm.stream_chat_events` whose every call waits for one event."""

    def __init__(self, release: threading.Event) -> None:
        self.release = release
        self.calls = 0

    def __call__(self, messages, **kwargs):
        self.calls += 1
        return self._run()

    async def _run(self):
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        yield ("token", "done")


class _FakeOpenAIMain:
    """The OpenAI client `llm._client` hands back for the main engine: one
    token every `tick`, `max_tokens` of them. Accepts whatever client options
    llm.py passes (T1 adds `unbounded_read`)."""

    def __init__(self, tick: float) -> None:
        self.tick = tick
        self.running = 0
        self.requests: List[Dict[str, Any]] = []
        self.chat = self
        self.completions = self

    def client(self, base_url, api_key=None, **options):
        return self

    async def create(self, **request):
        self.requests.append(request)
        return self._stream(int(request.get("max_tokens") or 16))

    async def _stream(self, count: int):
        self.running += 1
        try:
            for _ in range(count):
                await asyncio.sleep(self.tick)
                yield _Obj(choices=[_Obj(delta=_Obj(content="x", model_extra={}, reasoning=None, reasoning_content=None),
                                         finish_reason=None)], usage=None)
            yield _Obj(choices=[_Obj(delta=_Obj(content=None, model_extra={}, reasoning=None, reasoning_content=None),
                                     finish_reason="length")], usage=None)
        finally:
            self.running -= 1


class _Obj:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


# ---------------------------------------------- the waiter bound, the shim --


def _set(monkeypatch, name: str, value: Any) -> None:
    """A PUBLIC_API_* setting for one test, whether config.py names it (the
    attribute wins in `registry.setting_*`) or not (the environment)."""
    monkeypatch.setattr(settings, name.lower(), value, raising=False)
    monkeypatch.setenv(name, str(value))


def _long_plan():
    from types import SimpleNamespace

    return SimpleNamespace(engine=registry.ENGINE_MAIN, gate_engine="main.long", gate_weight_tokens=0, yield_to_chat=False)


def test_four_thousand_patient_waiters_leave_the_event_loop_responsive(monkeypatch):
    """Measured by the review on the first shim: 730 ms of loop stall at 5,000
    waiters on a 2 s turn. The loop chat shares must stay quiet while
    thousands of /v1 calls wait."""
    _set(monkeypatch, "PUBLIC_API_GATE_WAIT_S", 1)
    _set(monkeypatch, "PUBLIC_API_GATE_MAX_WAITERS", 0)
    capacity.reset_for_tests()
    public_router._GATE_LINE.reset_for_tests()

    async def wait_one():
        async with public_router._patient_gate(_long_plan()):
            pass

    async def scenario():
        loop = asyncio.get_running_loop()
        async with capacity.hold("main.long", wait_s=1):
            tasks = [asyncio.ensure_future(wait_one()) for _ in range(4000)]
            await asyncio.sleep(0.5)
            assert capacity.snapshot()["main.long"]["waiting"] == 4000
            worst = 0.0
            until = loop.time() + 2.5
            while loop.time() < until:
                before = loop.time()
                await asyncio.sleep(0.01)
                worst = max(worst, loop.time() - before - 0.01)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return worst

    worst = asyncio.run(scenario())
    assert worst < 0.2, f"event loop stalled {worst * 1000:.0f} ms"
    assert public_router._GATE_LINE.waiting == {}
    capacity.reset_for_tests()


def test_the_waiter_bound_refuses_before_any_wait_and_counts_each_request_once_per_gate(monkeypatch):
    _set(monkeypatch, "PUBLIC_API_GATE_MAX_WAITERS", 2)
    line = public_router._GateLine()
    first = line.reserve(["main.extended", "main.normal"])
    second = line.reserve(["main.normal"])
    assert line.waiting == {"main.extended": 1, "main.normal": 2}
    with pytest.raises(errors.ApiError) as refused:
        line.reserve(["main.extended", "main.normal"])
    assert refused.value.status == 503 and refused.value.code == "model_unavailable"
    assert refused.value.headers()["Retry-After"] == "30"
    assert line.waiting == {"main.extended": 1, "main.normal": 2}  # a refusal takes no place
    first.release()
    first.release()  # idempotent
    assert line.waiting == {"main.normal": 1}
    second.release()
    assert line.waiting == {}
    _set(monkeypatch, "PUBLIC_API_GATE_MAX_WAITERS", 0)
    places = [line.reserve(["main.long"]) for _ in range(50)]
    assert line.waiting == {"main.long": 50}
    for place in places:
        place.release()


def test_a_generation_past_the_waiter_bound_is_a_real_503_and_its_place_comes_back(api_port, platform, monkeypatch):
    monkeypatch.setattr(settings, "model_max_context", 1_000_000)
    _set(monkeypatch, "PUBLIC_API_GATE_MAX_WAITERS", 1)
    public_router._GATE_LINE.reset_for_tests()
    release = threading.Event()
    engine = _SlowEngine(silent_s=0.0, hold_first=release)
    monkeypatch.setattr(llm, "stream_chat_events", engine)
    holder = _hold_the_long_gate(api_port, engine)
    url = f"http://127.0.0.1:{api_port}/v1/responses"
    project_id = platform["project"]["id"]
    try:
        with httpx.Client(timeout=30) as client:
            with client.stream("POST", url, json=dict(LONG, stream=True), headers=_auth()) as waiting:
                assert waiting.status_code == 200
                # Kept: a dropped iter_raw() generator closes the response.
                waiting_body = waiting.iter_raw()
                next(waiting_body)
                _wait_for(lambda: capacity.snapshot()["main.long"]["waiting"] == 1)
                assert public_router._GATE_LINE.waiting == {"main.long": 1}
                rows_before = len(db.list_api_responses(project_id))

                crowded_stream = client.post(url, json=dict(LONG, stream=True), headers=_auth())
                started = time.monotonic()
                crowded_sync = client.post(url, json=LONG, headers=_auth())
                sync_took = time.monotonic() - started
            _wait_for(lambda: public_router._GATE_LINE.waiting == {})
            for refused in (crowded_stream, crowded_sync):
                assert refused.status_code == 503, refused.text
                assert refused.headers["retry-after"] == "30"
                assert refused.headers["content-type"].startswith("application/json")
                assert refused.json()["error"]["code"] == "model_unavailable"
            assert sync_took < COMMIT_S + 1.0
            assert len(db.list_api_responses(project_id)) == rows_before  # nothing written for them
            with client.stream("POST", url, json=dict(LONG, stream=True), headers=_auth()) as again:
                assert again.status_code == 200
                again_body = again.iter_raw()
                assert next(again_body).startswith(b"event: response.created")
    finally:
        release.set()
        holder.join(10)
    assert engine.calls == 1


@pytest.mark.parametrize("fails_in", ["prepare", "body"])
def test_a_stream_that_ends_before_or_while_waiting_gives_its_place_in_line_back(monkeypatch, fails_in):
    """The place is taken before the row write; a refused row write, and a
    body that ends while still waiting, must both give it back — a leaked
    place would count against PUBLIC_API_GATE_MAX_WAITERS for ever."""
    from types import SimpleNamespace

    from app.apiplatform import quotas

    monkeypatch.setattr(quotas, "concurrency_slot", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(events, "HEARTBEAT_SECONDS", 0.05)
    public_router._GATE_LINE.reset_for_tests()
    sent: List[Dict[str, Any]] = []

    async def send(message):
        sent.append(message)

    async def prepare():
        if fails_in == "prepare":
            raise errors.internal_error()

    async def nothing():
        return None

    @contextlib.asynccontextmanager
    async def gate(place=None):
        await asyncio.sleep(0.2)
        raise errors.model_unavailable(retry_after=30)
        yield  # pragma: no cover

    async def frames():
        yield "event: response.created\ndata: {}\n\n"
        yield "data: never\n\n"

    async def scenario():
        stream = public_router._SlotStream(
            frames(),
            caller=SimpleNamespace(project_id="p"),
            request=SimpleNamespace(state=SimpleNamespace(), headers={}),
            prepare=prepare,
            on_refused=nothing,
            on_abandoned=nothing,
            capacity_for=gate,
            reserve_place=lambda: public_router._GATE_LINE.reserve(["main.long"]),
        )
        seen = {}

        async def receive():
            seen.setdefault("line", dict(public_router._GATE_LINE.waiting))
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        await stream({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
        return seen

    seen = asyncio.run(scenario())
    start = next(m for m in sent if m["type"] == "http.response.start")
    if fails_in == "prepare":
        assert start["status"] == 500
    else:
        assert start["status"] == 200
        assert seen["line"] == {"main.long": 1}  # counted while it waited
    assert public_router._GATE_LINE.waiting == {}


# ------------------------------------------------ the SDK's own timeout --


@pytest.mark.parametrize("sdk_headers, attempt, level", [
    ({"X-Stainless-Retry-Count": "1", "X-Stainless-Timeout": "600"}, "retry", "WARNING"),
    ({"X-Stainless-Retry-Count": "0"}, "first", "INFO"),
    ({}, "first", "INFO"),
])
def test_a_synchronous_generation_that_loses_its_client_is_logged_and_counted_louder_for_an_sdk_retry(
    api_port, platform, monkeypatch, caplog, sdk_headers, attempt, level
):
    """openai-node 7.x counts `timeout` across the whole response, so no
    heartbeat keeps a longer synchronous call alive, and each of its retries
    starts a new generation. Visible to the operator, at least."""
    monkeypatch.setattr(llm, "stream_chat_events", _SlowEngine(silent_s=30.0))
    labels = (("attempt", attempt),)
    from app import metrics

    def count() -> float:
        return (metrics._counters.get("public_api_sync_client_gone_total") or {}).get(labels, 0.0)

    before = count()
    with caplog.at_level("INFO", logger=public_router.__name__):
        with httpx.Client(timeout=30) as client:
            with client.stream(
                "POST", f"http://127.0.0.1:{api_port}/v1/responses", json=NORMAL, headers=_auth(sdk_headers)
            ) as response:
                assert response.status_code == 200
                started = time.monotonic()
                for _chunk in response.iter_raw():
                    if time.monotonic() - started > 0.8:
                        break
        project_id = platform["project"]["id"]
        _wait_for(lambda: [row["status"] for row in db.list_api_responses(project_id)] == ["cancelled"])
    lines = [r for r in caplog.records if "lost its client" in r.getMessage()]
    assert [r.levelname for r in lines] == [level]
    message = lines[0].getMessage()
    assert "stream or use background" in message
    assert ("declared timeout 600 s" in message) is ("X-Stainless-Timeout" in sdk_headers)
    assert count() - before == 1


# ---------------------------------------------- queue time is not TTFT --


def test_a_responses_stream_that_waited_for_its_gate_records_engine_time_not_queue_time(api_port, platform, monkeypatch):
    monkeypatch.setattr(settings, "model_max_context", 1_000_000)
    recorded: Dict[str, Dict[str, Any]] = {}
    real_record = public_router.usage_ledger.record_async

    async def capture(**fields):
        recorded[fields["generation_id"]] = fields
        return await real_record(**fields)

    monkeypatch.setattr(public_router.usage_ledger, "record_async", capture)
    release = threading.Event()
    engine = _SlowEngine(silent_s=0.0, hold_first=release)
    monkeypatch.setattr(llm, "stream_chat_events", engine)
    holder = _hold_the_long_gate(api_port, engine)
    threading.Timer(1.2, release.set).start()

    response, chunks, error = timed_post(api_port, "/v1/responses", json_body=dict(LONG, stream=True), headers=_auth())
    holder.join(10)
    assert error is None and response.status_code == 200
    created = events.parse_frames(body_of(chunks).decode())[0]["data"]["response"]["id"]
    total_s = chunks[-1][0]
    assert total_s >= 1.0
    _wait_for(lambda: created in recorded)
    fields = recorded[created]
    assert fields["ttft_ms"] is not None and fields["ttft_ms"] < 500, fields
    assert fields["duration_ms"] < 500, fields
