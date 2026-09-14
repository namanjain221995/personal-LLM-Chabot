"""Requests with files never wait on a clock, and never stop the loop
(files-hookup senior fixes, 2026-09-14).

The adversarial review of the first hookup proved, on the merged tree, that a
request with a file part was the one `/v1` request that could still:

* stall the event loop (inline HTML stripping that was not linear);
* fail on a clock — a file stream's capacity gate, an `input_audio` clip's
  speech gate and 240 s read timeout, a background request's 45 s wait before
  its 202;
* crowd the default thread pool the chat app's token counting shares (one
  readiness query per waiter per second);
* be planned below its real size (file text at the 3-chars-per-token estimate);
* stall every stream in the process (the file lift on the event loop).

Each test here is one of those, driven through the real public router where
the finding was about the router. The live-router tests call the ASGI app in
the test's OWN event loop (`AsgiCall`), so the test can hold a capacity gate
busy in the same loop the router waits in, and can measure that loop's lag.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import secrets
import statistics
import threading
import time
import wave
from typing import Any, Dict, List, Optional

import httpx
import pytest
from fastapi import FastAPI

from app import db, llm
from app.apifiles import ids as file_ids, service
from app.config import settings
from app.publicapi import capacity, file_inputs, planning, registry, router as public_router
from tests.test_files_hookup import (  # noqa: F401 - fixtures
    _CapturingEngine,
    _engine_text,
    _file_body,
    files_world,
)
from tests.test_publicapi_routes import (  # noqa: F401 - fixtures
    TOKENS,
    WORKSPACE,
    _auth,
    _pepper,
    api,
    platform,
)


# ------------------------------------------------------------ the driver --


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(public_router.router)
    public_router.install_error_handlers(app)
    return app


class AsgiCall:
    """One HTTP request into an ASGI app, in the CURRENT event loop, with the
    response body observable while it is still being written."""

    def __init__(self, app: Any, method: str, path: str, *, headers: Dict[str, str], body: Any = None) -> None:
        raw = b"" if body is None else json.dumps(body).encode()
        self.app = app
        self.scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
            "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
            "root_path": "", "server": ("testserver", 80), "client": ("127.0.0.1", 5000),
            "headers": [(k.lower().encode(), v.encode()) for k, v in {**headers, "content-type": "application/json",
                                                                     "content-length": str(len(raw))}.items()],
        }
        self.raw = raw
        self.status: Optional[int] = None
        self.chunks: List[bytes] = []
        self.finished = asyncio.Event()
        self.gone = asyncio.Event()
        self._sent_body = False
        self.task: Optional[asyncio.Task] = None
        self.started_at = 0.0
        self.ended_at: Optional[float] = None

    async def _receive(self) -> Dict[str, Any]:
        if not self._sent_body:
            self._sent_body = True
            return {"type": "http.request", "body": self.raw, "more_body": False}
        await self.gone.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: Dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = int(message["status"])
        elif message["type"] == "http.response.body":
            if message.get("body"):
                self.chunks.append(bytes(message["body"]))
            if not message.get("more_body"):
                self.ended_at = time.monotonic()
                self.finished.set()

    def start(self) -> "AsgiCall":
        self.started_at = time.monotonic()
        self.task = asyncio.ensure_future(self.app(self.scope, self._receive, self._send))
        return self

    def text(self) -> str:
        return b"".join(self.chunks).decode("utf-8", "replace")

    async def done(self, timeout: float) -> "AsgiCall":
        assert self.task is not None
        await asyncio.wait_for(asyncio.shield(self.task), timeout=timeout)
        return self

    async def close(self) -> None:
        self.gone.set()
        if self.task is not None and not self.task.done():
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(self.task, timeout=5)


@contextlib.asynccontextmanager
async def _busy(gate: str, units: int):
    """Hold `units` of a public gate from the test, in the router's loop."""
    async with contextlib.AsyncExitStack() as stack:
        for _ in range(units):
            await stack.enter_async_context(capacity.hold(gate, wait_s=5.0))
        yield


class _LoopLag:
    """The longest the event loop was away while this ran (10 ms ticks)."""

    def __init__(self) -> None:
        self.worst = 0.0
        self._stop = False
        self._task: Optional[asyncio.Task] = None

    async def _tick(self) -> None:
        while not self._stop:
            before = time.perf_counter()
            await asyncio.sleep(0.01)
            self.worst = max(self.worst, time.perf_counter() - before - 0.01)

    async def __aenter__(self) -> "_LoopLag":
        self._task = asyncio.ensure_future(self._tick())
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._stop = True
        assert self._task is not None
        await self._task


def _wav(seconds: float, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(rate)
        w.writeframes(b"\x80" * int(seconds * rate))
    return buf.getvalue()


# ----------------------------------------------- critical: the loop freeze --


def test_hostile_inline_html_does_not_delay_a_parallel_request_through_the_live_router(platform, tmp_path, monkeypatch):
    """The same input as `test_hostile_inline_html_is_stripped_in_linear_time`,
    at 128,027 chars, through the router: the loop stays responsive and a
    parallel request is answered while it is read."""
    monkeypatch.setenv("PUBLIC_API_FILES_DIR", str(tmp_path / "api-files"))
    engine = _CapturingEngine("It is a page.")
    monkeypatch.setattr(llm, "stream_chat_events", engine)
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: None)
    hostile = "<html><p>Visible intro.</p>" + "<script " * 16_000  # 128,027 chars
    body = {"model": registry.TECHSARA_35B, "input": [{"role": "user", "content": [
        {"type": "input_file", "file_data": base64.b64encode(hostile.encode()).decode(), "filename": "x.html"},
        {"type": "input_text", "text": "What does the page say?"}]}]}

    async def scenario() -> Dict[str, float]:
        app = _app()
        warm = await AsgiCall(app, "GET", "/v1/models", headers=_auth()).start().done(30)
        assert warm.status == 200
        async with _LoopLag() as lag:
            call = AsgiCall(app, "POST", "/v1/responses", headers=_auth(), body=body).start()
            slowest = 0.0
            while not call.finished.is_set():
                probe = await AsgiCall(app, "GET", "/v1/models", headers=_auth()).start().done(60)
                assert probe.status == 200
                slowest = max(slowest, (probe.ended_at or time.monotonic()) - probe.started_at)
                await asyncio.sleep(0.01)
            await call.done(60)
        assert call.status == 200, call.text()
        return {"lag": lag.worst, "slowest": slowest, "post": (call.ended_at or 0) - call.started_at}

    measured = asyncio.run(scenario())
    assert "Visible intro." in _engine_text(engine.messages[-1])
    assert "<script" not in _engine_text(engine.messages[-1])
    assert measured["lag"] < 0.25, measured
    assert measured["slowest"] < 0.5, measured


# --------------------------------------------- high: file stream gate waits --


def test_a_file_stream_whose_gate_stays_busy_past_every_wait_setting_keeps_waiting_then_answers(files_world, monkeypatch):
    """Reproduces the review: `main.extended` busy, both gate waits at a
    fraction of a second, `max_output_tokens=20000`. The file stream used to
    end `response.failed` `model_unavailable` after 1 s; it now says
    `: queued` until the gate frees and then answers.

    The busy gate is `main.normal` since the merge with PR #65 (2026-09-14):
    `main.extended` has no count of its own there — it admits the answer into
    admission's LONG_OUTPUT seats — and every `main.extended` answer then takes
    `main.normal`, a plain count of 6, which the test fills."""
    monkeypatch.setenv("PUBLIC_API_GATE_WAIT_S", "0.2")
    monkeypatch.setenv("PUBLIC_API_BACKGROUND_GATE_WAIT_S", "0.2")
    monkeypatch.setattr(file_inputs, "HEARTBEAT_S", 0.1)
    fid, engine = files_world["file_id"], files_world["engine"]
    body = _file_body(fid, stream=True, max_output_tokens=20000)

    async def scenario() -> str:
        app = _app()
        async with _busy("main.normal", 6):
            call = AsgiCall(app, "POST", "/v1/responses", headers=_auth(), body=body).start()
            await asyncio.sleep(1.5)
            waiting = call.text()
            assert call.status == 200
            assert "response.failed" not in waiting and "model_unavailable" not in waiting, waiting
            assert ": queued" in waiting, waiting
            assert engine.messages == [], "nothing generated while the gate was busy"
        await call.done(20)
        return call.text()

    text = asyncio.run(scenario())
    assert "event: response.completed" in text and "AZURE-42" in text
    assert "AZURE-42" in _engine_text(engine.messages[-1])


def test_a_file_stream_is_started_by_the_routers_own_stream_launch(files_world, monkeypatch):
    """One launch path: the day the router launches durably, file streams do
    too (review finding: they called `streaming.*_sse` directly)."""
    launched: List[Any] = []
    real = public_router._stream_launch

    def spy(**kwargs):
        inner = real(**kwargs)

        def launch(spec):
            launched.append(spec)
            return inner(spec)

        return launch

    monkeypatch.setattr(public_router, "_stream_launch", spy)

    async def scenario() -> str:
        call = AsgiCall(_app(), "POST", "/v1/responses", headers=_auth(),
                        body=_file_body(files_world["file_id"], stream=True)).start()
        await call.done(20)
        return call.text()

    text = asyncio.run(scenario())
    assert "event: response.completed" in text
    assert len(launched) == 1
    assert "AZURE-42" in _engine_text(launched[0].messages), "the launched spec is the plan made with the file text"


def test_a_long_file_stream_generates_with_the_ticket_its_gate_task_was_given(files_world, monkeypatch):
    """Merge of the no-timeout release with PR #65 (2026-09-14). A file stream
    enters its gate in a helper task (so it can keep writing `: queued`), and
    `capacity.hold` pre-admits a long answer into LONG_OUTPUT in THAT task's
    context. Before the carry the generation never saw the ticket and admitted
    the same answer again — two LONG_OUTPUT seats per stream, so two streams
    filled both seats and then waited for ever. The generation must run on the
    one ticket, and give it back."""
    from app import admission

    monkeypatch.setenv("ADMISSION_KV_METRICS_URL", "off")
    tickets: List[Any] = []
    seen: List[tuple] = []
    real_preadmit = admission.preadmit
    engine = files_world["engine"]

    async def spy(*args, **kwargs):
        ticket = await real_preadmit(*args, **kwargs)
        tickets.append(ticket)
        return ticket

    def capturing(messages, **kwargs):
        pre = admission._preadmitted.get()
        seen.append((pre is not None and bool(tickets) and pre is tickets[-1], getattr(pre, "lane", None)))
        return engine(messages, **kwargs)

    monkeypatch.setattr(admission, "preadmit", spy)
    monkeypatch.setattr(llm, "stream_chat_events", capturing)

    async def scenario() -> str:
        call = AsgiCall(_app(), "POST", "/v1/responses", headers=_auth(),
                        body=_file_body(files_world["file_id"], stream=True, max_output_tokens=100_000)).start()
        await call.done(20)
        return call.text()

    text = asyncio.run(scenario())
    assert "event: response.completed" in text and "AZURE-42" in text
    assert len(tickets) == 1 and tickets[0] is not None and tickets[0].lane == admission.LONG_OUTPUT
    assert seen == [(True, admission.LONG_OUTPUT)]
    assert tickets[0].released


# ------------------------------------------------- high: input_audio timers --


def _whisper(monkeypatch, replies: List[Dict[str, Any]]) -> List[str]:
    from app.publicapi import sidecars

    seen: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"ready": True, "cuda_failures": 0})
        if request.url.path.endswith("/audio/transcriptions"):
            replies.append({"content_type": request.headers.get("content-type", ""), "bytes": len(request.content)})
            return httpx.Response(200, json={"text": "the parcel arrives on tuesday", "duration": 2.0})
        return httpx.Response(404)

    monkeypatch.setattr(sidecars, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(settings, "asr_base_urls", ("http://whisper.test/v1",), raising=False)
    monkeypatch.setenv("PUBLIC_API_ASR_HEALTH_POLL_S", "0.05")
    monkeypatch.setenv("PUBLIC_API_ASR_GATE_SHIM_WAIT_S", "0.2")
    monkeypatch.setenv("PUBLIC_API_GATE_WAIT_S", "0.2")
    return seen


def _audio_body(**extra: Any) -> Dict[str, Any]:
    clip = base64.b64encode(_wav(2.0)).decode()
    body = {"model": registry.TECHSARA_35B, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "When does the parcel arrive?"},
        {"type": "input_audio", "input_audio": {"data": clip, "format": "wav"}}]}]}
    body.update(extra)
    return body


def test_input_audio_in_a_stream_waits_out_a_busy_speech_gate_then_is_transcribed(platform, monkeypatch):
    """The review: `asr` busy, PUBLIC_API_GATE_WAIT_S=1 — the stream ended with
    a `model_unavailable` error chunk after 1.0 s. Now it waits, patiently,
    and whisper is reached through T4's dispatcher once the gate frees."""
    replies: List[Dict[str, Any]] = []
    _whisper(monkeypatch, replies)
    monkeypatch.setattr(file_inputs, "HEARTBEAT_S", 0.1)
    engine = _CapturingEngine("On Tuesday.")
    monkeypatch.setattr(llm, "stream_chat_events", engine)
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: None)

    async def scenario() -> str:
        app = _app()
        async with _busy(capacity.GATE_ASR, 1):
            call = AsgiCall(app, "POST", "/v1/chat/completions", headers=_auth(), body=_audio_body(stream=True)).start()
            await asyncio.sleep(1.2)
            waiting = call.text()
            assert call.status == 200
            assert "error" not in waiting and "[DONE]" not in waiting, waiting
            assert replies == [], "the clip was not sent while the speech gate was busy"
        await call.done(20)
        return call.text()

    text = asyncio.run(scenario())
    assert "[DONE]" in text and '"error"' not in text, text
    assert "the parcel arrives on tuesday" in _engine_text(engine.messages[-1])
    assert len(replies) == 1 and "multipart/form-data" in replies[0]["content_type"]


@pytest.mark.skipif(
    not hasattr(public_router, "keepalive"),
    reason="a build without the committed JSON response answers a synchronous request before any byte, "
    "so its file preparation is the one bounded delivery (file_inputs' docstring)",
)
def test_a_synchronous_input_audio_request_waits_out_a_busy_speech_gate_inside_the_committed_response(platform, monkeypatch):
    replies: List[Dict[str, Any]] = []
    _whisper(monkeypatch, replies)
    engine = _CapturingEngine("On Tuesday.")
    monkeypatch.setattr(llm, "stream_chat_events", engine)
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: None)

    async def scenario() -> AsgiCall:
        app = _app()
        async with _busy(capacity.GATE_ASR, 1):
            call = AsgiCall(app, "POST", "/v1/chat/completions", headers=_auth(), body=_audio_body()).start()
            await asyncio.sleep(1.2)
            assert not call.finished.is_set(), call.text()
        return await call.done(30)

    call = asyncio.run(scenario())
    assert call.status == 200
    body = json.loads(call.text())
    assert "error" not in body, body
    assert body["choices"][0]["message"]["content"] == "On Tuesday."
    assert len(replies) == 1


def test_a_request_without_a_deadline_is_prepared_with_patient_engines_and_the_lenient_ocr_rule(monkeypatch):
    """Stream, background and committed sync: `service.prepare` gets the
    stream delivery (no deadline, the 40-page inline OCR rule) and the patient
    transcriber; only a bounded sync keeps the bounded engines."""
    seen: List[Dict[str, Any]] = []

    async def fake_prepare(lifted, **kwargs):
        seen.append(kwargs)
        return object()

    monkeypatch.setattr(service, "prepare", fake_prepare)
    model = registry.resolve_public_model(registry.TECHSARA_35B, allowed=None, overrides=None)
    plan = planning.plan_generation(
        __import__("app.publicapi.models", fromlist=["x"]).parse_responses_request({"model": model.id, "input": "hi"}), model
    )
    lifted = service.lift_file_parts(_audio_body(), dialect=service.DIALECT_CHAT)
    run = file_inputs.FileRun(lifted, caller=type("C", (), {"project_id": "proj_" + "1" * 24})(), plan=plan,
                              request_id="req_" + "0" * 32, caller_messages=[])

    async def scenario() -> None:
        await run.prepare(file_inputs.DELIVERY_STREAM)
        await run.prepare(file_inputs.DELIVERY_BACKGROUND, no_deadline=True)
        await run.prepare(file_inputs.DELIVERY_BACKGROUND)
        await run.prepare(file_inputs.DELIVERY_SYNC, no_deadline=True)
        await run.prepare(file_inputs.DELIVERY_SYNC)

    asyncio.run(scenario())
    deliveries = [k["delivery"] for k in seen]
    assert deliveries == ["stream", "stream", "stream", "stream", "sync"]
    assert [k["sync_wait_s"] is service.NO_DEADLINE for k in seen] == [True, True, True, True, False]
    patient = [k["engines"].transcriber.__qualname__.startswith("patient_transcriber") for k in seen]
    assert patient == [True, True, True, True, False]
    assert isinstance(seen[0]["store"], file_inputs.SharedReadiness)


# ------------------------------------------------ high: background accepted --


def _get(api, response_id: str) -> Dict[str, Any]:
    response = api.get(f"/v1/responses/{response_id}", headers=_auth())
    assert response.status_code == 200, response.text
    return response.json()


def test_a_background_request_with_a_processing_file_is_accepted_at_once_and_runs_when_the_file_is_ready(api, files_world, monkeypatch):
    """The review: `409 file_not_ready` after the sync readiness wait, before
    any 202. Now the 202 comes at once (with the sync waits set far below the
    time the file takes) and the job runs when the file is processed."""
    monkeypatch.setenv("PUBLIC_API_FILES_SYNC_READY_WAIT_S", "0.1")
    monkeypatch.setenv("PUBLIC_API_FILES_SYNC_PREPARE_BUDGET_S", "0.2")
    store, fid, project_id, engine = files_world["store"], files_world["file_id"], files_world["project_id"], files_world["engine"]
    store.update(project_id, fid, blob_status="processing", blob_stage="index", blob_progress={"percent": 10})
    started = time.monotonic()
    accepted = api.post("/v1/responses", headers=_auth(), json=_file_body(fid, background=True))
    assert accepted.status_code == 202, accepted.text
    assert time.monotonic() - started < 2.0
    response_id = accepted.json()["id"]
    assert accepted.json()["status"] == "queued"
    time.sleep(0.8)
    assert _get(api, response_id)["status"] == "queued"
    assert engine.messages == []
    store.update(project_id, fid, blob_status="processed", blob_stage="finalize", blob_progress={"percent": 100})
    deadline = time.monotonic() + 15
    row = _get(api, response_id)
    while row["status"] in ("queued", "in_progress") and time.monotonic() < deadline:
        time.sleep(0.1)
        row = _get(api, response_id)
    assert row["status"] == "completed", row
    assert "AZURE-42" in json.dumps(row["output"])
    assert "AZURE-42" in _engine_text(engine.messages[-1])
    assert file_inputs.background_waits() == 0


def test_a_background_request_cancelled_while_its_file_is_processing_never_runs(api, files_world, monkeypatch):
    monkeypatch.setattr(file_inputs, "_cancel_poll_s", lambda: 0.05)
    store, fid, project_id, engine = files_world["store"], files_world["file_id"], files_world["project_id"], files_world["engine"]
    store.update(project_id, fid, blob_status="processing")
    accepted = api.post("/v1/responses", headers=_auth(), json=_file_body(fid, background=True))
    assert accepted.status_code == 202, accepted.text
    response_id = accepted.json()["id"]
    cancelled = api.post(f"/v1/responses/{response_id}/cancel", headers=_auth())
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    deadline = time.monotonic() + 5
    while file_inputs.background_waits() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert file_inputs.background_waits() == 0
    store.update(project_id, fid, blob_status="processed", blob_stage="finalize")
    time.sleep(0.5)
    assert _get(api, response_id)["status"] == "cancelled"
    assert engine.messages == []


def test_a_background_request_whose_file_fails_while_waiting_is_recorded_failed_with_the_files_sentence(api, files_world):
    store, fid, project_id, engine = files_world["store"], files_world["file_id"], files_world["project_id"], files_world["engine"]
    store.update(project_id, fid, blob_status="processing")
    accepted = api.post("/v1/responses", headers=_auth(), json=_file_body(fid, background=True))
    assert accepted.status_code == 202
    response_id = accepted.json()["id"]
    store.update(project_id, fid, blob_status="failed", blob_error_code="file_corrupt")
    deadline = time.monotonic() + 10
    row = _get(api, response_id)
    while row["status"] == "queued" and time.monotonic() < deadline:
        time.sleep(0.05)
        row = _get(api, response_id)
    assert row["status"] == "failed"
    assert row["error"]["message"] == service.FAILURE_SENTENCES["file_corrupt"]
    assert engine.messages == []


# ------------------------------------------------ medium: shared readiness --


class _CountingSqlStore:
    """A store with the SQL seam (`_query`): records the thread each query ran
    on and how many ids it asked for."""

    def __init__(self, rows: Dict[str, Dict[str, Any]], delay_s: float = 0.002) -> None:
        self.rows = rows
        self.delay_s = delay_s
        self.threads: List[str] = []
        self.sizes: List[int] = []

    def _query(self, project_id: str, ids: List[str]) -> List[Dict[str, Any]]:
        self.threads.append(threading.current_thread().name)
        self.sizes.append(len(ids))
        time.sleep(self.delay_s)
        return [dict(self.rows[i]) for i in ids if i in self.rows and self.rows[i]["project_id"] == project_id]


def _row(project_id: str, fid: str, status: str = "processing") -> Dict[str, Any]:
    return {
        "id": fid, "project_id": project_id, "blob_id": "blob_x", "assembling_upload_id": None, "error_code": None,
        "filename": "a.txt", "bytes": 1, "expires_at": None, "deleted_at": None, "blob_sha256": "a" * 64,
        "blob_kind": "text", "blob_mime_type": "text/plain", "blob_status": status, "blob_stage": "text",
        "blob_progress": {}, "blob_facts": {}, "blob_error_code": None, "blob_video_analysis_id": None,
    }


def test_two_thousand_waiters_share_a_few_queries_on_one_dedicated_thread(monkeypatch):
    projects_ = ["proj_" + "a" * 24, "proj_" + "b" * 24]
    rows: Dict[str, Dict[str, Any]] = {}
    mine: List[tuple] = []
    for n in range(2000):
        project_id = projects_[n % 2]
        fid = "file-" + secrets.token_hex(12)
        rows[fid] = _row(project_id, fid)
        mine.append((project_id, fid))
    store = _CountingSqlStore(rows)
    monkeypatch.setattr(service, "SqlFileStore", lambda *a, **k: store)

    async def scenario() -> Dict[str, Any]:
        shared = file_inputs.SharedReadiness()
        latencies: List[float] = []
        running = True

        async def probe() -> None:
            while running:
                before = time.perf_counter()
                await asyncio.to_thread(lambda: None)
                latencies.append(time.perf_counter() - before)
                await asyncio.sleep(0.005)

        async def waiter(project_id: str, fid: str) -> None:
            await asyncio.sleep(secrets.randbelow(100) / 1000)
            for _ in range(5):
                got = await shared.get_files(project_id, [fid])
                assert list(got) == [fid] and got[fid].project_id == project_id
                await asyncio.sleep(0.2)

        prober = asyncio.ensure_future(probe())
        started = time.perf_counter()
        await asyncio.gather(*(waiter(p, f) for p, f in mine))
        elapsed = time.perf_counter() - started
        running = False
        await prober
        return {"elapsed": elapsed, "p50": statistics.median(latencies), "flights": shared.flights}

    measured = asyncio.run(scenario())
    lookups = 2000 * 5
    assert set(store.threads) == {"files-readiness_0"}, set(store.threads)
    # Ten flights a second, one query per project per flight.
    assert measured["flights"] <= measured["elapsed"] / file_inputs.READINESS_FLIGHT_SPACING_S + 5, measured
    assert len(store.threads) <= 2 * measured["flights"] < lookups / 20, (len(store.threads), measured)
    assert measured["p50"] < 0.02, measured


def test_waiters_on_real_rows_resolve_through_the_shared_poller_without_crowding_the_default_pool(platform, monkeypatch):
    """Against the private Postgres: 1,500 requests wait on a processing file
    of the platform's project through `wait_until_ready` with the production
    store, then all see it processed."""
    monkeypatch.setattr(settings, "public_api_files_ready_poll_s", 0.05)
    project_id, workspace_id = platform["project"]["id"], WORKSPACE
    blob_id, fid = file_ids.new_blob_id(), file_ids.new_file_id()
    with db.connection() as con:
        con.execute(
            "INSERT INTO api_file_blobs (id, project_id, workspace_id, sha256, bytes, kind, lane, status, progress) "
            "VALUES (%s, %s, %s, %s, 10, 'text', 'cpu', 'processing', '{}')",
            (blob_id, project_id, workspace_id, "d" * 64),
        )
        con.execute(
            "INSERT INTO api_files (id, project_id, workspace_id, blob_id, filename, purpose, bytes) "
            "VALUES (%s, %s, %s, %s, 'n.txt', 'user_data', 10)",
            (fid, project_id, workspace_id, blob_id),
        )
    body = {"model": registry.TECHSARA_35B, "input": [{"role": "user", "content": [{"type": "input_file", "file_id": fid}]}]}
    lifted = service.lift_file_parts(body, dialect=service.DIALECT_RESPONSES)
    queries: List[str] = []
    real_query = service.SqlFileStore._query

    def counted(self, *args):
        queries.append(threading.current_thread().name)
        return real_query(self, *args)

    monkeypatch.setattr(service.SqlFileStore, "_query", counted)

    async def scenario() -> Dict[str, Any]:
        latencies: List[float] = []
        running = True

        async def probe() -> None:
            while running:
                before = time.perf_counter()
                await asyncio.to_thread(lambda: None)
                latencies.append(time.perf_counter() - before)
                await asyncio.sleep(0.005)

        async def finish_later() -> None:
            await asyncio.sleep(1.5)
            await asyncio.to_thread(_set_processed, blob_id)

        prober = asyncio.ensure_future(probe())
        started = time.perf_counter()
        results = await asyncio.gather(
            finish_later(),
            *(service.wait_until_ready(file_inputs.shared_readiness(), project_id, lifted, delivery=service.DELIVERY_STREAM)
              for _ in range(1500)),
        )
        elapsed = time.perf_counter() - started
        running = False
        await prober
        return {"records": results[1:], "elapsed": elapsed, "p50": statistics.median(latencies)}

    measured = asyncio.run(scenario())
    assert all(records[fid].state == "processed" for records, _waited in measured["records"])
    assert set(queries) == {"files-readiness_0"}
    assert len(queries) <= measured["elapsed"] / file_inputs.READINESS_FLIGHT_SPACING_S + 5, (len(queries), measured["elapsed"])
    assert measured["p50"] < 0.02, measured["p50"]


def _set_processed(blob_id: str) -> None:
    with db.connection() as con:
        con.execute("UPDATE api_file_blobs SET status = 'processed' WHERE id = %s", (blob_id,))


def test_a_synchronous_request_with_files_holds_its_concurrency_slot_before_it_waits(api, files_world, monkeypatch):
    from app.apiplatform import quotas

    order: List[str] = []
    real_slot = quotas.concurrency_slot
    real_prepare = service.prepare

    @contextlib.contextmanager
    def slot(caller, kind="sync"):
        order.append(f"slot:{kind}")
        with real_slot(caller, kind) as taken:
            yield taken
        order.append(f"release:{kind}")

    async def prepare(*args, **kwargs):
        order.append("prepare")
        return await real_prepare(*args, **kwargs)

    monkeypatch.setattr(quotas, "concurrency_slot", slot)
    monkeypatch.setattr(service, "prepare", prepare)
    response = api.post("/v1/responses", headers=_auth(), json=_file_body(files_world["file_id"]))
    assert response.status_code == 200, response.text
    assert order == ["slot:sync", "prepare", "release:sync"], order


# --------------------------------------------------- medium: exact counts --


def _model(model_id: str = registry.TECHSARA_35B):
    return registry.resolve_public_model(model_id, allowed=None, overrides=None)


def _request(**kwargs: Any):
    from app.publicapi import models

    return models.parse_responses_request({"model": registry.TECHSARA_35B, "input": "Sum the column.", **kwargs})


def test_a_digit_dense_file_the_engine_did_not_count_is_gated_at_its_byte_bound_like_typed_text():
    """The review's numbers: 120,000 random digits estimate 40,001 tokens and
    bound 120,016; the file used to be sized at the estimate while the same
    text typed in was sized at its bound.

    Since the merge with PR #65 (2026-09-14) no main gate is sized by the
    prompt (planning.main_gate_for: the planned output only), so the gate of
    both is `main.extended` at 90,000 output tokens; what this pins is that
    the file is still SIZED like the typed text — its byte bound, never the
    estimate — for the input ceiling, the footprint and the router's charge."""
    digits = "".join(str(secrets.randbelow(10)) for _ in range(120_000))
    model = _model()
    typed = planning.plan_generation(_request(input=digits, max_output_tokens=90_000), model)
    assert typed.gate_engine == "main.extended"
    spliced = [{"role": "user", "content": digits}, {"role": "user", "content": "Sum the column."}]
    files = planning.FileInputs(messages=spliced, tokens=40_001, bounded_tokens=120_016, images=0)
    plan = planning.plan_generation(_request(max_output_tokens=90_000), model, files=files)
    assert plan.gate_engine == typed.gate_engine
    assert plan.bounded_input_tokens >= 120_016
    assert plan.footprint_tokens >= typed.footprint_tokens - 16
    # The soft TPM reservation keeps the estimate.
    assert plan.estimated_input_tokens < 50_000


def test_a_file_the_engine_counted_is_planned_at_that_count_in_both_directions():
    model = _model()
    spliced = [{"role": "user", "content": "x"}]
    # Prose: 300,000 bytes that the engine counts at 100,000 stays out of main.long.
    prose = planning.FileInputs(messages=spliced, tokens=100_000, bounded_tokens=300_000, measured_tokens=100_000)
    plan = planning.plan_generation(_request(), model, files=prose)
    # 131,072: admission's LONG threshold, which decides the prompt's lane now
    # that no main gate is sized by it (PR #65, merged 2026-09-14).
    assert plan.gate_engine != "main.long" and plan.footprint_tokens < 131_072
    # Digits the estimate put at 40,001 and the engine at 120,000 are 120,000.
    dense = planning.FileInputs(messages=spliced, tokens=40_001, bounded_tokens=120_016, measured_tokens=120_000)
    plan = planning.plan_generation(_request(max_output_tokens=90_000), model, files=dense)
    assert plan.bounded_input_tokens >= 120_000
    assert plan.footprint_tokens > 131_072
    # A `full` file over the input ceiling by the engine's count is the 400.
    over = planning.FileInputs(messages=spliced, tokens=10, bounded_tokens=20, measured_tokens=int(model.max_input_tokens) + 1)
    with pytest.raises(Exception) as refused:
        planning.plan_generation(_request(), model, files=over)
    assert getattr(refused.value, "code", "") == "context_length_exceeded"


def test_the_file_count_asks_the_engine_for_the_file_text_and_adds_the_image_estimates(files_world, monkeypatch):
    asked: List[tuple] = []

    async def counter(base_url: str, model: str, text: str) -> Optional[int]:
        asked.append((base_url, model, text))
        return 7

    model = _model()
    plan = planning.plan_generation(_request(), model)
    body = _file_body(files_world["file_id"])
    lifted = service.lift_file_parts(body, dialect=service.DIALECT_RESPONSES)
    caller = type("C", (), {"project_id": files_world["project_id"]})()
    run = file_inputs.FileRun(lifted, caller=caller, plan=plan, request_id="req_" + "1" * 32, caller_messages=[])

    async def scenario():
        await run.prepare(file_inputs.DELIVERY_STREAM, store=files_world["store"])
        counted = await run.planning_inputs(counter=counter)

        async def refuses(*_a):
            return None

        uncounted = await run.planning_inputs(counter=refuses)
        return counted, uncounted

    counted, uncounted = asyncio.run(scenario())
    assert len(asked) == 1 and "AZURE-42" in asked[0][2]
    assert asked[0][0] == str(settings.openai_base_url).rstrip("/")
    assert counted.measured_tokens == 7
    assert uncounted.measured_tokens is None and uncounted.ceiling_tokens() == max(uncounted.bounded_tokens, uncounted.tokens)


# ------------------------------------------------ medium: lift off the loop --


def test_a_large_body_is_lifted_off_the_event_loop_and_a_small_one_on_it(platform, monkeypatch):
    threads: List[bool] = []
    real_lift = file_inputs.lift

    def spy(payload, endpoint):
        threads.append(threading.current_thread() is threading.main_thread())
        return real_lift(payload, endpoint)

    monkeypatch.setattr(file_inputs, "lift", spy)
    engine = _CapturingEngine("ok")
    monkeypatch.setattr(llm, "stream_chat_events", engine)
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: None)
    parts = [{"type": "input_text", "text": "x"} for _ in range(300_000)]
    large = {"model": registry.TECHSARA_35B, "max_output_tokens": 16, "input": [{"role": "user", "content": parts}]}
    small = {"model": registry.TECHSARA_35B, "input": "hello"}

    async def scenario() -> Dict[str, float]:
        app = _app()
        small_call = await AsgiCall(app, "POST", "/v1/responses", headers=_auth(), body=small).start().done(30)
        assert small_call.status == 200
        async with _LoopLag() as lag:
            large_call = await AsgiCall(app, "POST", "/v1/responses", headers=_auth(), body=large).start().done(120)
        assert large_call.status in (200, 400), large_call.text()
        return {"lag": lag.worst}

    measured = asyncio.run(scenario())
    # asyncio.run's loop is the main thread's: the small body on it, the large one off it.
    assert threads == [True, False], threads
    print("loop lag while a 300,000-part body was lifted and parsed:", round(measured["lag"], 3), "s")


# ---------------------------------- high: the context build's engine waits --


def test_the_patient_embedder_and_reranker_wait_out_busy_gates_the_bounded_ones_give_up_on(monkeypatch):
    from app.apifiles import retrieval, vectors
    from app.publicapi import sidecars

    monkeypatch.setenv("PUBLIC_API_GATE_WAIT_S", "0.2")
    calls: List[str] = []

    async def embed_call(texts):
        calls.append("embed")
        return [[0.5, 0.5]], 3

    async def score_call(client, query_text, documents):
        calls.append("rerank")
        return [0.9 for _ in documents], 5

    monkeypatch.setattr(sidecars, "_embed_call", embed_call)
    monkeypatch.setattr(sidecars, "_score_call", score_call)
    monkeypatch.setattr(registry, "_rerank_configured", lambda: True)

    async def scenario() -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        async with _busy(capacity.GATE_EMBED, 2), _busy(capacity.GATE_RERANK, 2):
            out["bounded_embed"] = await vectors.make_engine_query_embedder(capacity.sync_wait_s())("q")
            out["bounded_rerank"] = await retrieval.make_engine_reranker()("q", ["a", "b"])
            patient = file_inputs.patient_engines()
            embedding = asyncio.ensure_future(patient.embed_query("q"))
            ranking = asyncio.ensure_future(patient.rerank("q", ["a", "b"]))
            await asyncio.sleep(0.8)
            out["waiting"] = (embedding.done(), ranking.done(), list(calls))
        out["embed"] = await asyncio.wait_for(embedding, 10)
        out["rerank"] = await asyncio.wait_for(ranking, 10)
        return out

    out = asyncio.run(scenario())
    assert out["bounded_embed"] is None and out["bounded_rerank"] is None, "the bounded engines gave up (lexical)"
    assert out["waiting"] == (False, False, []), out["waiting"]
    assert out["embed"] == [0.5, 0.5]
    assert out["rerank"] == [0.9, 0.9]


def test_the_patient_reranker_keeps_the_lexical_order_quietly_where_no_reranker_is_deployed(monkeypatch, caplog):
    from app.publicapi import sidecars

    async def score_call(*_a):
        raise AssertionError("no engine call without a deployed reranker")

    monkeypatch.setattr(sidecars, "_score_call", score_call)
    monkeypatch.setattr(registry, "_rerank_configured", lambda: False)
    caplog.set_level("INFO")
    assert asyncio.run(file_inputs.patient_engines().rerank("q", ["a"])) is None
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]


def test_the_patient_engines_give_an_inline_pdf_the_patient_ocr_gate(monkeypatch):
    from app.apifiles import inline as inline_files

    seen: List[Any] = []
    real = inline_files.make_subprocess_extractor

    def spy(**kwargs):
        seen.append(kwargs.get("ocr_gate"))
        return real(**kwargs)

    monkeypatch.setattr(inline_files, "make_subprocess_extractor", spy)
    file_inputs.patient_engines()
    assert file_inputs.patient_ocr_gate in seen
    monkeypatch.setenv("PUBLIC_API_GATE_WAIT_S", "0.2")
    monkeypatch.setenv("PUBLIC_API_BACKGROUND_GATE_WAIT_S", "0.2")

    async def scenario() -> bool:
        async with _busy(capacity.GATE_OCR, 2):
            entering = asyncio.ensure_future(file_inputs.patient_ocr_gate().__aenter__())
            await asyncio.sleep(0.8)
            waited = not entering.done()
        await asyncio.wait_for(entering, 10)
        return waited

    assert asyncio.run(scenario())


def test_a_file_the_engine_counts_over_the_input_ceiling_is_refused_on_every_delivery(api, files_world, monkeypatch):
    """The router plans the second time with the engine's count (sync, stream
    and background alike), so a file whose estimate fits but whose real count
    does not is the context-length refusal, never an oversized prompt."""
    model = _model()

    async def over(base_url, served, text):
        return int(model.max_input_tokens) + 1

    monkeypatch.setattr(file_inputs, "_engine_token_count", over)
    fid, engine = files_world["file_id"], files_world["engine"]
    sync = api.post("/v1/responses", headers=_auth(), json=_file_body(fid))
    assert "context_length_exceeded" in sync.text, sync.text
    with api.stream("POST", "/v1/responses", headers=_auth(), json=_file_body(fid, stream=True)) as response:
        streamed = "".join(response.iter_text())
    assert "event: response.failed" in streamed and "context_length_exceeded" in streamed, streamed
    accepted = api.post("/v1/responses", headers=_auth(), json=_file_body(fid, background=True))
    assert accepted.status_code == 202
    deadline = time.monotonic() + 10
    row = _get(api, accepted.json()["id"])
    while row["status"] == "queued" and time.monotonic() < deadline:
        time.sleep(0.05)
        row = _get(api, accepted.json()["id"])
    assert row["status"] == "failed" and row["error"]["code"] == "context_length_exceeded", row
    assert engine.messages == []


def test_the_file_count_is_the_engines_tokenize_answer_with_a_budget_that_grows_with_the_text(monkeypatch):
    asked: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        asked.append({"path": request.url.path, "model": body["model"], "chars": len(body["messages"][0]["content"])})
        if body["messages"][0]["content"] == "broken":
            return httpx.Response(500)
        return httpx.Response(200, json={"count": 4321, "max_model_len": 1_000_000, "tokens": []})

    monkeypatch.setattr(file_inputs, "_count_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(file_inputs, "_COUNT_CLIENTS", __import__("weakref").WeakKeyDictionary())

    async def scenario():
        return (await file_inputs._engine_token_count("http://engine.test/v1", "served-model", "digits " * 100),
                await file_inputs._engine_token_count("http://engine.test/v1", "served-model", "broken"))

    counted, failed = asyncio.run(scenario())
    assert counted == 4321 and failed is None
    assert asked[0] == {"path": "/tokenize", "model": "served-model", "chars": 700}
    assert file_inputs.count_read_budget_s(0) == 60.0
    assert file_inputs.count_read_budget_s(3_600_000) == 240.0
    assert file_inputs.count_read_budget_s(10) >= float(settings.tokenize_timeout)


def test_a_file_stream_enters_the_routers_gate_for_every_plan_even_one_without_a_named_gate(files_world, monkeypatch):
    """The router's gate decides what a plan holds (T3 with T2: `main.normal`
    for a normal-size answer whose `gate_engine` is None)."""
    entered: List[Any] = []
    # The router's own gate where it has one; this module's otherwise.
    owner = public_router if hasattr(public_router, "_patient_gate") else file_inputs
    name = "_patient_gate" if owner is public_router else "patient_gate"
    real = getattr(owner, name)

    def spy(plan, **kwargs):
        entered.append(plan.gate_engine)
        return real(plan, **kwargs)

    monkeypatch.setattr(owner, name, spy)

    async def scenario() -> str:
        call = AsgiCall(_app(), "POST", "/v1/responses", headers=_auth(),
                        body=_file_body(files_world["file_id"], stream=True)).start()
        await call.done(20)
        return call.text()

    text = asyncio.run(scenario())
    assert "event: response.completed" in text
    assert entered == [None], entered
