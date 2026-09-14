"""Synchronous and streaming `/v1` generations run through the durable
runtime, and the resume route reads them back (no-timeout release gap 1,
2026-09-14).

WHAT THE VERIFIER FOUND (restart-gateway-TERM, conformance
`--feature resumable_streams=built`): only background jobs were durable; a
foreground stream was cut by a restart, `GET /v1/responses/{id}?stream=true
&starting_after=N` answered 200 JSON and ignored both parameters, SDK retries
started second generations, and a run settled by another process never
released its Idempotency-Key. Each test below is one of those behaviours,
end to end.

ON WHAT. A real uvicorn over a real socket (what matters is what leaves the
process: frames, dropped connections, headers), the real router, the real
platform (keys, scopes, quotas, rows) on this test run's PostgreSQL, and a
REAL `durable.Runtime` started in the server's own lifespan. The one stub is
the engine: `tests/publicapi_fake_engine.FakeMainEngine`, whose token i is
`w{i} ` and whose continuation carries on from the words already in the final
assistant message — so "the resumed text is identical" is a string
comparison. A test-only route runs code in the server's loop: suspend every
run as SIGTERM does, and swap in a fresh runtime that stands for the process
that replaces this one.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
import time
from typing import Any, Dict, Iterator, List, Optional

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from app import db
from app.apiplatform import idempotency
from app.publicapi import (
    background,
    blobs,
    capacity,
    durable,
    durable_store,
    errors,
    events,
    gateway_protocol,
    registry,
    router as public_router,
    streaming,
)
from tests.publicapi_fake_engine import FakeController, FakeMainEngine, expected_text, fast_durable_settings, set_setting
from tests.test_files_hookup import _file_body, files_world  # noqa: F401 - fixture
from tests.test_publicapi_routes import TOKENS, _pepper, platform  # noqa: F401 - fixtures

MODEL = registry.TECHSARA_35B


# ------------------------------------------------------------ plumbing --


def _new_runtime(tmp_path: Any, owner: str) -> durable.Runtime:
    runtime = durable.reset_for_tests()
    runtime.configure(
        owner=f"test-{owner}", view=FakeController(time.monotonic, state="READY"),
        blob_store=blobs.BlobStore(tmp_path / "blobs"),
    )
    runtime.witnesses_enabled = False
    return runtime


@contextlib.contextmanager
def serve(tmp_path: Any) -> Iterator[int]:
    """The public router on a real uvicorn whose lifespan starts a durable
    runtime, plus two test-only routes that run in the server's loop."""
    app = FastAPI()
    app.include_router(public_router.router)
    public_router.install_error_handlers(app)

    @app.post("/_test/restart")
    async def restart() -> Dict[str, Any]:
        # SIGTERM in the old process (suspend every run, abort every reader)
        # and the replacement process's runtime, sharing only PostgreSQL.
        released = await durable.suspend_all("restart")
        await durable.stop()
        replacement = _new_runtime(tmp_path, f"B{time.monotonic_ns()}")
        await replacement.start()
        return {"released": released}

    @app.post("/_test/stop")
    async def stop() -> Dict[str, Any]:
        await durable.stop()
        return {}

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        runtime = _new_runtime(tmp_path, "A")
        await runtime.start()
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await durable.stop()

    app.router.lifespan_context = lifespan
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
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


@pytest.fixture(autouse=True)
def _durable_world(monkeypatch, tmp_path):
    fast_durable_settings(monkeypatch)
    durable_store.ensure_schema()
    capacity.reset_for_tests()
    public_router._GATE_LINE.reset_for_tests()
    set_setting(monkeypatch, "PUBLIC_API_BLOB_DIR", str(tmp_path / "blobs"))
    set_setting(monkeypatch, "PUBLIC_API_SYNC_COMMIT_S", "0.3")
    monkeypatch.setattr(events, "HEARTBEAT_SECONDS", 0.2)
    monkeypatch.setattr(blobs, "min_free_disk_bytes", lambda: 0)
    yield
    capacity.reset_for_tests()
    public_router._GATE_LINE.reset_for_tests()
    durable.reset_for_tests()


@pytest.fixture()
def port(platform, tmp_path):
    with serve(tmp_path) as bound:
        yield bound


def _auth(which: str = "live", extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {"Authorization": f"Bearer {TOKENS[which]}"}
    headers.update(extra or {})
    return headers


def _url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


def _responses_body(**extra: Any) -> Dict[str, Any]:
    return {"model": MODEL, "input": "Count for me.", **extra}


def _chat_body(**extra: Any) -> Dict[str, Any]:
    return {"model": MODEL, "messages": [{"role": "user", "content": "Count for me."}], **extra}


def _post_restart(port: int) -> None:
    response = httpx.post(_url(port, "/_test/restart"), timeout=30)
    assert response.status_code == 200, response.text


def _wait_for(predicate, timeout: float = 15.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.02)


class Wire:
    """One SSE response read frame by frame: the raw text, the parsed
    Responses events, and whether the body ended cleanly."""

    def __init__(self) -> None:
        self.text = ""
        self.records: List[Dict[str, Any]] = []
        self.error: Optional[BaseException] = None
        self.status = 0
        self.headers: Dict[str, str] = {}


def read_sse(
    method: str, url: str, *, headers: Dict[str, str], json_body: Any = None, params: Any = None,
    stop_after_deltas: Optional[int] = None, on_delta: Optional[Any] = None,
) -> Wire:
    wire = Wire()
    with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
        with client.stream(method, url, headers=headers, json=json_body, params=params) as response:
            wire.status = response.status_code
            wire.headers = dict(response.headers)
            deltas = 0
            buffer = ""
            try:
                for chunk in response.iter_text():
                    wire.text += chunk
                    buffer += chunk
                    while "\n\n" in buffer:
                        block, buffer = buffer.split("\n\n", 1)
                        if block.strip() == events.DONE_SENTINEL.strip():
                            continue
                        parsed = events.parse_frames(block + "\n\n")
                        if not parsed:
                            continue
                        wire.records.extend(parsed)
                        if parsed[0]["event"] == "response.output_text.delta" or (
                            parsed[0]["event"] is None and "content" in json.dumps(parsed[0]["data"])
                        ):
                            deltas += 1
                            if on_delta is not None:
                                on_delta(deltas)
                            if stop_after_deltas is not None and deltas >= stop_after_deltas:
                                return wire
            except httpx.HTTPError as exc:
                wire.error = exc
    return wire


def _names(records: List[Dict[str, Any]]) -> List[str]:
    return [r["event"] for r in records]


def _seqs(records: List[Dict[str, Any]]) -> List[int]:
    return [r["data"]["sequence_number"] for r in records]


def _text(records: List[Dict[str, Any]]) -> str:
    return "".join(r["data"]["delta"] for r in records if r["event"] == "response.output_text.delta")


def _response_id(records: List[Dict[str, Any]]) -> str:
    return next(r["data"]["response"]["id"] for r in records if r["event"] == "response.created")


# ------------------------------------------------- launched durably ----


def test_a_synchronous_response_is_a_durable_run_with_its_events_logged_and_the_same_body(port, platform, monkeypatch):
    engine = FakeMainEngine(answer_tokens=12, delay_s=0.01).install(monkeypatch)

    response = httpx.post(_url(port, "/v1/responses"), json=_responses_body(), headers=_auth(), timeout=30)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    # `models._Strict` strips the output text's edges (a long-standing rule of
    # the Response object, not of this path); the log keeps every byte.
    assert body["output"][0]["content"][0]["text"] == expected_text(12).strip()
    row = durable_store.get_run(body["id"])
    assert row["resumable"] is True and row["status"] == "completed" and row["dialect"] == "responses"
    logged = durable_store.list_events(body["id"], 0, 1000)
    assert "".join(r[2].get("delta", "") for r in logged if r[1] == "response.output_text.delta") == expected_text(12)
    assert logged[-1][1] == "response.completed"
    assert len(engine.calls) == 1 and engine.calls[0].kwargs["admission_patient"] is True
    # The waiter bound counts WAITING generations; this one was admitted.
    assert public_router._GATE_LINE.waiting == {}


def test_a_responses_stream_is_rendered_from_the_log_with_the_item_events_and_contiguous_numbers(port, platform, monkeypatch):
    FakeMainEngine(answer_tokens=20, delay_s=0.005).install(monkeypatch)

    wire = read_sse("POST", _url(port, "/v1/responses"), headers=_auth(), json_body=_responses_body(stream=True))

    assert wire.status == 200 and wire.error is None
    names = _names(wire.records)
    assert names[:2] == ["response.created", "response.in_progress"]
    assert names[2:4] == ["response.output_item.added", "response.content_part.added"]
    assert names[-4:] == [
        "response.output_text.done", "response.content_part.done", "response.output_item.done", "response.completed",
    ]
    assert _seqs(wire.records) == list(range(1, len(wire.records) + 1))
    assert _text(wire.records) == expected_text(20)
    response_id = _response_id(wire.records)
    stored = durable_store.list_events(response_id, 0, 1000)
    assert [(s, n) for s, n, _ in stored] == list(zip(_seqs(wire.records), names))
    assert events.reserved_field_lines(wire.text) == [] and ": ts-seq" not in wire.text


def test_a_chat_stream_and_a_chat_call_run_durably_in_the_compatibility_dialect(port, platform, monkeypatch):
    FakeMainEngine(answer_tokens=6, delay_s=0.005).install(monkeypatch)

    wire = read_sse(
        "POST", _url(port, "/v1/chat/completions"), headers=_auth(),
        json_body=_chat_body(stream=True, stream_options={"include_usage": True}),
    )
    sync = httpx.post(_url(port, "/v1/chat/completions"), json=_chat_body(), headers=_auth(), timeout=30)

    assert wire.status == 200 and wire.error is None
    assert wire.text.startswith(": ping")
    assert wire.text.rstrip().endswith("data: [DONE]")
    chunks = [r["data"] for r in wire.records if isinstance(r["data"], dict)]
    content = "".join((c["choices"][0]["delta"].get("content") or "") for c in chunks if c["choices"])
    assert content == expected_text(6)
    assert chunks[-1]["usage"] == {"prompt_tokens": 17, "completion_tokens": 6, "total_tokens": 23}
    completion_id = chunks[0]["id"]
    row = durable_store.get_run("resp_" + completion_id.split("_", 1)[1])
    assert row["resumable"] is True and row["dialect"] == "chat"
    assert sync.status_code == 200 and sync.json()["choices"][0]["message"]["content"] == expected_text(6)


def test_a_vision_model_generation_on_the_router_engine_runs_durably_too(port, platform, monkeypatch):
    from app.publicapi import engines
    from tests.publicapi_fake_engine import FakeSidecarEngine

    async def no_probe(engine):
        return None

    monkeypatch.setattr(engines, "served_window", no_probe)
    fake = FakeSidecarEngine(answer_tokens=5).install(monkeypatch)

    response = httpx.post(
        _url(port, "/v1/responses"), json={"model": "techsara-8b-vision", "input": "Describe the sky.",
                                           "max_output_tokens": 64},
        headers=_auth(), timeout=30,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == "techsara-8b-vision" and body["output"][0]["content"][0]["text"] == expected_text(5).strip()
    row = durable_store.get_run(body["id"])
    assert row["resumable"] is True and row["engine"] == registry.ENGINE_ROUTER
    assert [call["engine"] for call in fake.calls] == [registry.ENGINE_ROUTER]


def test_store_false_keeps_the_non_durable_path_and_names_no_run_for_the_gateway(port, platform, monkeypatch):
    FakeMainEngine(answer_tokens=4).install(monkeypatch)
    set_setting(monkeypatch, "PUBLIC_API_GATEWAY_PEERS", "127.0.0.1")
    tagged = _auth(extra={gateway_protocol.ATTEMPT_HEADER: "attempt-store-false-0001"})

    wire = read_sse("POST", _url(port, "/v1/responses"), headers=tagged, json_body=_responses_body(stream=True, store=False))

    assert wire.status == 200 and wire.headers.get("x-techsara-run") == "none"
    row = durable_store.get_run(_response_id(wire.records))
    assert not row["resumable"] and row["status"] == "completed"
    assert durable_store.list_events(row["id"], 0, 10) == []


# ------------------------------------------------------- the resume route --


def test_a_stream_dropped_mid_answer_resumes_from_starting_after_with_contiguous_events_and_the_same_text(port, platform, monkeypatch):
    FakeMainEngine(answer_tokens=60, delay_s=0.01).install(monkeypatch)

    first = read_sse(
        "POST", _url(port, "/v1/responses"), headers=_auth(), json_body=_responses_body(stream=True),
        stop_after_deltas=5,
    )
    response_id = _response_id(first.records)
    last = first.records[-1]["data"]["sequence_number"]
    resumed = read_sse(
        "GET", _url(port, f"/v1/responses/{response_id}"), headers=_auth(),
        params={"stream": "true", "starting_after": last},
    )

    assert resumed.status == 200 and resumed.headers["content-type"].startswith("text/event-stream")
    assert resumed.records, "the resume stream carried no events"
    numbers = _seqs(first.records) + _seqs(resumed.records)
    assert numbers == list(range(1, len(numbers) + 1))
    assert sum(r["event"] in ("response.completed", "response.failed") for r in resumed.records) == 1
    text = _text(first.records + resumed.records)
    done = next(r for r in resumed.records if r["event"] == "response.output_text.done")
    assert text == done["data"]["text"] == expected_text(60)


def test_a_finished_response_replays_after_n_and_the_whole_stream_without_n(port, platform, monkeypatch):
    FakeMainEngine(answer_tokens=5).install(monkeypatch)
    live = read_sse("POST", _url(port, "/v1/responses"), headers=_auth(), json_body=_responses_body(stream=True))
    response_id = _response_id(live.records)

    after_two = read_sse("GET", _url(port, f"/v1/responses/{response_id}"), headers=_auth(),
                         params={"stream": "true", "starting_after": 2})
    full = read_sse("GET", _url(port, f"/v1/responses/{response_id}"), headers=_auth(), params={"stream": "true"})

    assert _seqs(after_two.records) == _seqs(live.records)[2:]
    assert after_two.records[-1]["event"] == "response.completed" and after_two.error is None
    assert _names(full.records) == _names(live.records)


def test_the_resume_route_checks_lookup_scope_creator_and_streamability_in_the_contracts_order(port, platform, monkeypatch):
    FakeMainEngine(answer_tokens=3).install(monkeypatch)
    durable_run = httpx.post(_url(port, "/v1/responses"), json=_responses_body(), headers=_auth(), timeout=30).json()
    unstored = httpx.post(_url(port, "/v1/responses"), json=_responses_body(store=False), headers=_auth(), timeout=30).json()

    def get(response_id: str, params: Dict[str, Any], which: str = "live") -> httpx.Response:
        return httpx.get(_url(port, f"/v1/responses/{response_id}"), params=params, headers=_auth(which), timeout=30)

    # 1. the lookup first: an unknown id is 404 whatever the parameters say.
    assert get("resp_000000000000000000000000", {"starting_after": 1}).status_code == 404
    assert get("resp_000000000000000000000000", {"stream": "maybe"}).status_code == 404
    # 1b. another project's key: the same 404.
    assert get(durable_run["id"], {"stream": "true"}, which="other").status_code == 404
    # 3. a key of the same project that did not create the run: 404, not 403.
    narrow = get(durable_run["id"], {"stream": "true"}, which="narrow")
    assert narrow.status_code == 404 and narrow.json()["error"]["code"] == "response_not_found"
    # 4. streamability: store false is 400 on `stream`.
    refused = get(unstored["id"], {"stream": "true", "starting_after": 0})
    assert refused.status_code == 400 and refused.json()["error"]["param"] == "stream"
    # 5. starting_after without stream=true.
    alone = get(durable_run["id"], {"starting_after": 1})
    assert alone.status_code == 400 and alone.json()["error"]["param"] == "starting_after"
    bad = get(durable_run["id"], {"stream": "maybe"})
    assert bad.status_code == 400 and bad.json()["error"]["param"] == "stream"
    # stream=false is the plain JSON read.
    plain = get(durable_run["id"], {"stream": "false"})
    assert plain.status_code == 200 and plain.json()["id"] == durable_run["id"]


# ---------------------------------------------------------- restarts ---


def test_a_restart_ends_the_stream_without_a_terminal_and_the_replacement_resumes_it_by_continuation(port, platform, monkeypatch):
    engine = FakeMainEngine(answer_tokens=80, delay_s=0.01).install(monkeypatch)
    restarted = threading.Event()

    def restart_once(deltas: int) -> None:
        if deltas == 6 and not restarted.is_set():
            restarted.set()
            threading.Thread(target=_post_restart, args=(port,), daemon=True).start()

    cut = read_sse(
        "POST", _url(port, "/v1/responses"), headers=_auth(), json_body=_responses_body(stream=True),
        on_delta=restart_once,
    )
    assert restarted.is_set()
    terminal = [r for r in cut.records if r["event"] in ("response.completed", "response.failed")]
    assert terminal == [], "a suspended run must not look finished"
    assert cut.error is not None, "the body must end as an incomplete read so the client knows"
    response_id = _response_id(cut.records)
    _wait_for(lambda: durable_store.get_run(response_id)["lease_owner"] is None)

    resumed = read_sse(
        "GET", _url(port, f"/v1/responses/{response_id}"), headers=_auth(),
        params={"stream": "true", "starting_after": cut.records[-1]["data"]["sequence_number"]},
    )

    numbers = _seqs(cut.records) + _seqs(resumed.records)
    assert numbers == list(range(1, len(numbers) + 1))
    assert _text(cut.records + resumed.records) == expected_text(80)
    assert resumed.records[-1]["event"] == "response.completed"
    assert len(engine.calls) == 2 and engine.calls[1].kwargs["continue_final_message"] is True
    row = durable_store.get_run(response_id)
    assert row["status"] == "completed" and row["metadata"]["resume_count"] == 1


def test_an_sdk_retry_of_a_synchronous_call_cut_by_a_restart_attaches_and_returns_the_whole_answer_once(port, platform, monkeypatch):
    engine = FakeMainEngine(answer_tokens=150, delay_s=0.01).install(monkeypatch)
    result: Dict[str, Any] = {}

    def first_call() -> None:
        try:
            result["first"] = httpx.post(_url(port, "/v1/responses"), json=_responses_body(), headers=_auth(), timeout=30)
        except httpx.HTTPError as exc:
            result["first_error"] = exc

    thread = threading.Thread(target=first_call)
    thread.start()
    _wait_for(lambda: engine.calls and engine.calls[0].tokens_sent >= 40)
    _post_restart(port)
    thread.join(30)
    assert "first_error" in result, "a committed call cut by a restart drops its connection"
    rows = _wait_for(lambda: [r for r in db.list_api_responses(platform["project"]["id"]) if r.get("resumable")])
    response_id = rows[0]["id"]
    _wait_for(lambda: durable_store.get_run(response_id)["lease_owner"] is None)

    retry = httpx.post(
        _url(port, "/v1/responses"), json=_responses_body(),
        headers=_auth(extra={"x-stainless-retry-count": "1"}), timeout=30,
    )

    assert retry.status_code == 200, retry.text
    body = retry.json()
    assert body["id"] == response_id
    assert body["output"][0]["content"][0]["text"] == expected_text(150).strip()
    assert len([r for r in db.list_api_responses(platform["project"]["id"]) if r.get("resumable")]) == 1
    assert len(engine.calls) == 2 and engine.calls[1].kwargs["continue_final_message"] is True


def test_a_first_attempt_retry_count_of_zero_launches_a_new_generation(port, platform, monkeypatch):
    engine = FakeMainEngine(answer_tokens=3).install(monkeypatch)
    first = httpx.post(_url(port, "/v1/responses"), json=_responses_body(), headers=_auth(), timeout=30).json()
    second = httpx.post(
        _url(port, "/v1/responses"), json=_responses_body(),
        headers=_auth(extra={"x-stainless-retry-count": "0"}), timeout=30,
    ).json()
    assert first["id"] != second["id"] and len(engine.calls) == 2


def test_an_sdk_retry_never_attaches_to_a_run_whose_process_crashed_before_writing_its_spec(port, platform, monkeypatch):
    engine = FakeMainEngine(answer_tokens=3).install(monkeypatch)
    raw = json.dumps(_responses_body()).encode()
    crashed = db.create_api_response(
        platform["project"]["id"], "ws-public-api", MODEL, "req_crashed", key_id=platform["live"].key["id"],
        status="in_progress", background=False, streamed=False,
    )
    durable_store.mark_durable(crashed["id"], dialect="responses", item_id="msg_crashed", engine="main", owner=None,
                               lease_ttl_s=60, body_sha256=gateway_protocol.body_sha256(raw))

    retry = httpx.post(
        _url(port, "/v1/responses"), content=raw,
        headers=_auth(extra={"x-stainless-retry-count": "1", "Content-Type": "application/json"}), timeout=30,
    )

    assert retry.status_code == 200, retry.text
    assert retry.json()["id"] != crashed["id"] and retry.json()["status"] == "completed"
    assert len(engine.calls) == 1


# ----------------------------------------------------- the gateway --


def test_a_gateway_re_post_with_the_same_attempt_resumes_after_n_without_a_second_generation(port, platform, monkeypatch):
    engine = FakeMainEngine(answer_tokens=40, delay_s=0.01).install(monkeypatch)
    set_setting(monkeypatch, "PUBLIC_API_GATEWAY_PEERS", "127.0.0.1")
    attempt = "attempt-gateway-000000000001"
    tagged = _auth(extra={gateway_protocol.ATTEMPT_HEADER: attempt})

    first = read_sse("POST", _url(port, "/v1/responses"), headers=tagged, json_body=_responses_body(stream=True),
                     stop_after_deltas=4)
    response_id = _response_id(first.records)
    marks = [int(line.split("=", 1)[1]) for line in first.text.splitlines() if line.startswith(": ts-seq=")]
    assert first.headers["x-techsara-run"] == response_id
    assert marks == _seqs(first.records)[: len(marks)] and marks
    resume_after = marks[-1]

    again = read_sse(
        "POST", _url(port, "/v1/responses"),
        headers={**tagged, gateway_protocol.RESUME_AFTER_HEADER: str(resume_after)},
        json_body=_responses_body(stream=True),
    )

    assert again.status == 200 and again.headers["x-techsara-run"] == response_id
    assert _seqs(again.records)[0] == resume_after + 1
    assert _text([r for r in first.records if r["data"]["sequence_number"] <= resume_after] + again.records) == expected_text(40)
    assert len(engine.calls) == 1


def test_a_gateway_re_post_whose_body_differs_or_whose_attempt_is_unknown_is_a_404(port, platform, monkeypatch):
    FakeMainEngine(answer_tokens=3).install(monkeypatch)
    set_setting(monkeypatch, "PUBLIC_API_GATEWAY_PEERS", "127.0.0.1")
    attempt = "attempt-gateway-000000000002"
    tagged = _auth(extra={gateway_protocol.ATTEMPT_HEADER: attempt})
    httpx.post(_url(port, "/v1/responses"), json=_responses_body(), headers=tagged, timeout=30)

    other_body = httpx.post(_url(port, "/v1/responses"), json=_responses_body(input="Something else."),
                            headers=tagged, timeout=30)
    unknown = httpx.post(
        _url(port, "/v1/responses"), json=_responses_body(stream=True),
        headers=_auth(extra={gateway_protocol.ATTEMPT_HEADER: "attempt-never-launched-01",
                             gateway_protocol.RESUME_AFTER_HEADER: "7"}),
        timeout=30,
    )

    assert other_body.status_code == 404
    assert unknown.status_code == 404


def test_a_gateway_json_re_post_attaches_to_the_running_call_and_returns_the_same_body(port, platform, monkeypatch):
    engine = FakeMainEngine(answer_tokens=30, delay_s=0.02).install(monkeypatch)
    set_setting(monkeypatch, "PUBLIC_API_GATEWAY_PEERS", "127.0.0.1")
    tagged = _auth(extra={gateway_protocol.ATTEMPT_HEADER: "attempt-gateway-json-000003"})
    bodies: List[Dict[str, Any]] = []

    def call() -> None:
        bodies.append(httpx.post(_url(port, "/v1/responses"), json=_responses_body(), headers=tagged, timeout=30).json())

    first = threading.Thread(target=call)
    first.start()
    _wait_for(lambda: engine.calls and engine.calls[0].tokens_sent >= 5)
    second = threading.Thread(target=call)
    second.start()
    first.join(30)
    second.join(30)

    assert len(bodies) == 2 and bodies[0]["id"] == bodies[1]["id"]
    assert bodies[0]["output"][0]["content"][0]["text"] == bodies[1]["output"][0]["content"][0]["text"] == expected_text(30).strip()
    assert len(engine.calls) == 1


# ------------------------------------------------------ Idempotency-Key --


def test_the_same_idempotency_key_attaches_to_a_running_stream_and_replays_it_from_the_start(port, platform, monkeypatch):
    engine = FakeMainEngine(answer_tokens=30, delay_s=0.01).install(monkeypatch)
    keyed = _auth(extra={"Idempotency-Key": "attach-me-1"})
    first = read_sse("POST", _url(port, "/v1/responses"), headers=keyed, json_body=_responses_body(stream=True),
                     stop_after_deltas=3)

    again = read_sse("POST", _url(port, "/v1/responses"), headers=keyed, json_body=_responses_body(stream=True))

    assert _response_id(again.records) == _response_id(first.records)
    assert _seqs(again.records) == list(range(1, len(again.records) + 1))
    assert _text(again.records) == expected_text(30)
    assert len(engine.calls) == 1


def test_the_same_idempotency_key_from_another_key_of_the_project_is_a_409_that_says_do_not_retry(port, platform, monkeypatch):
    from app.apiplatform import projects
    from app.apiplatform.scopes import Scope

    FakeMainEngine(answer_tokens=3).install(monkeypatch)
    second = projects.create_key(platform["project"]["id"], "ws-public-api", "second", scopes=list(Scope))
    body = _responses_body()
    first = httpx.post(_url(port, "/v1/responses"), json=body, headers=_auth(extra={"Idempotency-Key": "k-409"}), timeout=30)

    other = httpx.post(
        _url(port, "/v1/responses"), json=body,
        headers={"Authorization": f"Bearer {second.token}", "Idempotency-Key": "k-409"}, timeout=30,
    )

    assert first.status_code == 200
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_conflict"
    assert other.headers.get("x-should-retry") == "false"


# ----------------------------------------------- recorded elsewhere --


def test_a_failed_run_settled_by_another_process_releases_its_idempotency_key_and_records_its_request(port, platform, monkeypatch):
    fail_resumed = {"on": False}

    async def maybe_fail(engine, call, index):
        if fail_resumed["on"] and call.kwargs["continue_final_message"]:
            raise RuntimeError("the engine refused the continuation")

    engine = FakeMainEngine(answer_tokens=200, delay_s=0.01, before_token=maybe_fail).install(monkeypatch)
    keyed = _auth(extra={"Idempotency-Key": "released-after-failure"})
    first = read_sse("POST", _url(port, "/v1/responses"), headers=keyed, json_body=_responses_body(stream=True),
                     stop_after_deltas=5)
    response_id = _response_id(first.records)
    fail_resumed["on"] = True
    _post_restart(port)
    _wait_for(lambda: durable_store.get_run(response_id)["lease_owner"] is None)

    # The replacement process claims the run for its reader and fails it.
    resumed = read_sse("GET", _url(port, f"/v1/responses/{response_id}"), headers=_auth(), params={"stream": "true"})
    assert resumed.records[-1]["event"] == "response.failed"

    def claim_state():
        with db.connection() as con:
            return con.execute(
                "SELECT state, response_id FROM api_idempotency WHERE idem_key = %s", ("released-after-failure",)
            ).fetchone()

    _wait_for(lambda: claim_state() is not None and claim_state()["response_id"] is None)
    retried = idempotency.claim(platform["project"]["id"], "v1_responses", "released-after-failure",
                                _responses_body(stream=True))
    assert retried.claimed is True
    with db.connection() as con:
        usage = con.execute(
            "SELECT route, status, meta FROM usage_events WHERE generation_id = %s", (response_id,)
        ).fetchall()
    assert len(usage) == 1
    assert usage[0]["route"] == "v1_responses" and usage[0]["meta"]["settled_by"] == "durable"
    assert usage[0]["meta"]["request_id"] and usage[0]["meta"]["api_key_id"] == platform["live"].key["id"]
    assert len(engine.calls) == 2


# --------------------------------------------- stream and background --


def test_stream_and_background_together_follow_the_job_and_leaving_does_not_cancel_it(port, platform, monkeypatch):
    FakeMainEngine(answer_tokens=40, delay_s=0.01).install(monkeypatch)

    left = read_sse("POST", _url(port, "/v1/responses"), headers=_auth(),
                    json_body=_responses_body(stream=True, background=True), stop_after_deltas=3)
    response_id = _response_id(left.records)
    row = _wait_for(lambda: (lambda r: r if r["status"] == "completed" else None)(durable_store.get_run(response_id)))
    whole = read_sse("POST", _url(port, "/v1/responses"), headers=_auth(),
                     json_body=_responses_body(stream=True, background=True, input="Another."))

    assert left.status == 200 and left.text.startswith(": ping")
    assert row["background"] is True and row["output_text"] == expected_text(40)
    assert whole.records[-1]["event"] == "response.completed" and _text(whole.records) == expected_text(40)


# ------------------------------------------------ orphaned foreground --


def test_a_non_durable_foreground_row_left_open_by_a_previous_process_is_closed_on_read_and_by_the_sweep(platform, monkeypatch):
    from datetime import datetime, timedelta, timezone

    project = platform["project"]
    rows = [
        db.create_api_response(project["id"], "ws-public-api", MODEL, f"req_{i}", status="queued",
                               background=False, streamed=True)
        for i in range(2)
    ]
    monkeypatch.setattr(background, "PROCESS_STARTED_AT", datetime.now(timezone.utc) + timedelta(seconds=5))

    repaired = asyncio.run(background.repair_if_orphaned(db.get_api_response(rows[0]["id"], project["id"])))
    runtime = durable.Runtime()
    swept = asyncio.run(runtime.sweep_once())

    assert repaired["status"] == "failed" and repaired["error_code"] == background.INTERRUPTED_CODE
    assert swept["foreground_interrupted"] >= 1
    assert db.get_api_response(rows[1]["id"], project["id"])["status"] == "failed"


def test_a_non_durable_stream_cut_before_its_terminal_is_recorded_cancelled_with_the_tokens_it_produced(monkeypatch):
    FakeMainEngine(answer_tokens=50, delay_s=0.005, report_usage=False).install(monkeypatch)
    recorded: List[streaming.StreamOutcome] = []

    async def on_finish(outcome: streaming.StreamOutcome) -> None:
        recorded.append(outcome)

    async def scenario() -> None:
        spec = streaming.GenerationSpec(
            response_id="resp_cut", model=MODEL, messages=[{"role": "user", "content": "x"}], max_tokens=100,
            temperature=0.0, created_at=1, estimated_input_tokens=11,
        )
        frames = streaming.responses_sse(spec, on_finish=on_finish)
        deltas = 0
        async for frame in frames:
            if "response.output_text.delta" in frame:
                deltas += 1
                if deltas == 7:
                    break
        await frames.aclose()
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert len(recorded) == 1
    outcome = recorded[0]
    assert outcome.status == "cancelled" and outcome.client_gone is True
    assert outcome.usage["source"] == streaming.USAGE_COUNTED_AT_STOP
    assert outcome.usage["prompt_tokens"] == 11 and outcome.usage["completion_tokens"] >= 7


# ------------------------------------------------------ files, durably --


def test_a_durable_file_stream_announces_its_citations_and_so_do_a_durable_file_call_and_a_chat_stream(port, files_world):
    fid = files_world["file_id"]
    annotation = {"type": "file_citation", "file_id": fid, "filename": "memo.txt",
                  "index": "The code is AZURE-42 [memo.txt §1].".index("[memo.txt"), "page": 1}

    wire = read_sse("POST", _url(port, "/v1/responses"), headers=_auth(), json_body=_file_body(fid, stream=True))
    sync = httpx.post(_url(port, "/v1/responses"), json=_file_body(fid), headers=_auth(), timeout=30)
    chat = read_sse(
        "POST", _url(port, "/v1/chat/completions"), headers=_auth(),
        json_body={"model": MODEL, "stream": True, "messages": [{"role": "user", "content": [
            {"type": "file", "file": {"file_id": fid}}, {"type": "text", "text": "What is the launch code?"}]}]},
    )

    assert wire.status == 200 and wire.error is None
    names = _names(wire.records)
    added = [r["data"] for r in wire.records if r["event"] == "response.output_text.annotation.added"]
    assert added and added[0]["annotation"] == annotation
    assert names.index("response.output_text.done") < names.index("response.output_text.annotation.added") \
        < names.index("response.content_part.done")
    assert wire.records[-1]["data"]["response"]["output"][0]["content"][0]["annotations"] == [annotation]
    assert _seqs(wire.records) == list(range(1, len(wire.records) + 1))
    assert durable_store.get_run(_response_id(wire.records))["resumable"] is True

    assert sync.status_code == 200, sync.text
    assert sync.json()["output"][0]["content"][0]["annotations"] == [annotation]
    assert durable_store.get_run(sync.json()["id"])["resumable"] is True

    chunks = [r["data"] for r in chat.records if isinstance(r["data"], dict) and r["data"].get("choices")]
    final = next(c for c in chunks if c["choices"][0]["finish_reason"])
    assert final["choices"][0]["delta"]["annotations"] == [annotation]


def test_the_disk_guard_refuses_a_durable_stream_before_its_status_line_with_or_without_files(port, files_world, monkeypatch):
    """A durable stream with files is launched before its status line like any
    other (release review 2026-09-14), so the disk guard's refusal is the same
    real 503 for both, and neither leaves a row behind."""
    monkeypatch.setattr(blobs, "min_free_disk_bytes", lambda: 1 << 62)
    fid = files_world["file_id"]

    plain = httpx.post(_url(port, "/v1/responses"), json=_responses_body(stream=True), headers=_auth(), timeout=30)
    with_file = httpx.post(_url(port, "/v1/responses"), json=_file_body(fid, stream=True), headers=_auth(), timeout=30)

    for refused in (plain, with_file):
        assert refused.status_code == 503 and refused.headers["retry-after"], refused.text
    assert db.list_api_responses(files_world["project_id"]) == []
    assert files_world["engine"].messages == []


# ---------------------------------------- files still being prepared --
#
# Release review 2026-09-14 (medium): a durable request whose files were still
# being prepared was not a run yet. A restart held the old process's shutdown
# for its whole grace and then cut the stream with ZERO events (no id to
# resume), the synchronous call failed with a connection error, the run gave
# its project slot back when its client left, and it was never counted in the
# gates' waiter lines. Now it is launched at once and prepares in the runner.


def _stream_in_thread(url: str, *, headers: Dict[str, str], json_body: Any) -> "tuple[threading.Thread, Wire]":
    """`read_sse` on a thread, filling the returned Wire as frames arrive."""
    wire = Wire()

    def run() -> None:
        with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
            try:
                with client.stream("POST", url, headers=headers, json=json_body) as response:
                    wire.status = response.status_code
                    wire.headers = dict(response.headers)
                    buffer = ""
                    for chunk in response.iter_text():
                        wire.text += chunk
                        buffer += chunk
                        while "\n\n" in buffer:
                            block, buffer = buffer.split("\n\n", 1)
                            parsed = events.parse_frames(block + "\n\n")
                            if parsed:
                                wire.records.extend(parsed)
            except httpx.HTTPError as exc:
                wire.error = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, wire


def _preparing_runs() -> List[durable.Run]:
    return [run for run in list(durable.RUNTIME.runs.values()) if run.preparing]


def _in_flight(project_id: str) -> int:
    from app.apiplatform import quotas

    return int(quotas._in_flight.get(project_id, 0))


def test_a_durable_file_stream_has_its_id_slot_and_place_while_its_file_processes_then_answers(port, files_world):
    store, fid, project_id = files_world["store"], files_world["file_id"], files_world["project_id"]
    store.update(project_id, fid, blob_status="processing", blob_stage="index", blob_progress={"percent": 10})

    thread, wire = _stream_in_thread(_url(port, "/v1/responses"), headers=_auth(), json_body=_file_body(fid, stream=True))
    _wait_for(lambda: wire.records)
    assert _names(wire.records) == ["response.created"]
    response_id = _response_id(wire.records)
    assert _preparing_runs() and _preparing_runs()[0].id == response_id
    # The run holds the project's slot and its place in the gates' lines.
    assert _in_flight(project_id) == 1
    assert sum(public_router._GATE_LINE.waiting.values()) >= 1
    store.update(project_id, fid, blob_progress={"percent": 40})
    _wait_for(lambda: f": file {fid}" in wire.text)
    store.update(project_id, fid, blob_status="processed", blob_stage="finalize", blob_progress={"percent": 100})
    thread.join(30)

    assert wire.error is None and wire.status == 200
    assert wire.records[-1]["event"] == "response.completed"
    assert _seqs(wire.records) == list(range(1, len(wire.records) + 1))
    assert "AZURE-42" in _text(wire.records)
    added = [r for r in wire.records if r["event"] == "response.output_text.annotation.added"]
    assert added and added[0]["data"]["annotation"]["file_id"] == fid
    assert len(files_world["engine"].messages) == 1
    assert "AZURE-42" in json.dumps(files_world["engine"].messages[0])
    _wait_for(lambda: _in_flight(project_id) == 0 and not public_router._GATE_LINE.waiting)
    assert durable_store.get_run(response_id)["status"] == "completed"


def test_a_restart_while_files_are_prepared_ends_streams_and_calls_at_once_retry_safe_and_the_retry_answers(port, platform, files_world):
    store, fid, project_id = files_world["store"], files_world["file_id"], files_world["project_id"]
    store.update(project_id, fid, blob_status="processing", blob_stage="index", blob_progress={"percent": 10})
    keyed = _auth(extra={"Idempotency-Key": "prepared-across-a-restart"})
    stream_thread, wire = _stream_in_thread(_url(port, "/v1/responses"), headers=keyed, json_body=_file_body(fid, stream=True))
    result: Dict[str, Any] = {}

    def sync_call() -> None:
        try:
            result["response"] = httpx.post(_url(port, "/v1/responses"), json=_file_body(fid), headers=_auth(), timeout=30)
        except httpx.HTTPError as exc:
            result["error"] = exc

    sync_thread = threading.Thread(target=sync_call, daemon=True)
    sync_thread.start()
    _wait_for(lambda: len(_preparing_runs()) == 2 and wire.records)
    ids = sorted(run.id for run in _preparing_runs())
    time.sleep(0.5)  # the synchronous call is committed (PUBLIC_API_SYNC_COMMIT_S 0.3)

    began = time.monotonic()
    _post_restart(port)
    stream_thread.join(15)
    sync_thread.join(15)
    elapsed = time.monotonic() - began

    assert elapsed < 10, "a restart must not wait for a file still being prepared"
    # The stream: its id at once, then ONE terminal event that says what to do.
    assert wire.error is None and _names(wire.records) == ["response.created", "response.failed"]
    failure = wire.records[-1]["data"]["response"]["error"]
    assert failure["code"] == "model_unavailable"
    assert failure["message"] == errors.PREPARATION_INTERRUPTED_MESSAGE
    # The committed synchronous call: its connection dropped, so an SDK retries.
    assert "error" in result, result.get("response") and result["response"].text
    for response_id in ids:
        row = durable_store.get_run(response_id)
        assert row["status"] == "failed" and row["error_code"] == "model_unavailable"
    # The plan made before the files resolved is never stored as a spec.
    assert durable_store.has_spec(ids) in (set(), [], {})
    assert files_world["engine"].messages == []
    _wait_for(lambda: _in_flight(project_id) == 0 and not public_router._GATE_LINE.waiting)
    with db.connection() as con:
        claim = con.execute(
            "SELECT response_id FROM api_idempotency WHERE idem_key = %s", ("prepared-across-a-restart",)
        ).fetchone()
    assert claim is None or claim["response_id"] is None, "a failure that ran nothing releases its key"

    # The files kept processing; the SDK's retry answers in the new process.
    store.update(project_id, fid, blob_status="processed", blob_stage="finalize", blob_progress={"percent": 100})
    retry = httpx.post(
        _url(port, "/v1/responses"), json=_file_body(fid),
        headers=_auth(extra={"x-stainless-retry-count": "1"}), timeout=30,
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["id"] not in ids and "AZURE-42" in retry.json()["output"][0]["content"][0]["text"]
    assert len(files_world["engine"].messages) == 1


def test_a_synchronous_file_call_whose_client_left_keeps_its_slot_until_its_run_ends(port, files_world, monkeypatch):
    set_setting(monkeypatch, "PUBLIC_API_UNKEYED_ORPHAN_GRACE_S", "3")
    store, fid, project_id = files_world["store"], files_world["file_id"], files_world["project_id"]
    store.update(project_id, fid, blob_status="processing", blob_stage="index", blob_progress={"percent": 10})

    with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
        with client.stream("POST", _url(port, "/v1/responses"), headers=_auth(), json=_file_body(fid)) as response:
            assert response.status_code == 200
            next(response.iter_bytes())  # the committed response's first space
    run = _wait_for(lambda: (_preparing_runs() or [None])[0])
    _wait_for(lambda: not run.readers, timeout=5)

    # The client is gone and the run still prepares: its slot is still counted.
    assert run.preparing and _in_flight(project_id) == 1
    _wait_for(lambda: durable_store.get_run(run.id)["status"] == "cancelled", timeout=15)
    _wait_for(lambda: _in_flight(project_id) == 0 and not public_router._GATE_LINE.waiting)
    assert files_world["engine"].messages == []


def test_a_client_that_leaves_while_its_run_is_being_launched_never_leaves_the_row_queued(port, platform, monkeypatch):
    """Durable-resume review 2026-09-14 (medium): a committed call cancelled
    between the row's write and the runner's start left the row `queued`, the
    claim unbound and the waiter place taken, until the next restart."""
    set_setting(monkeypatch, "PUBLIC_API_STREAM_ORPHAN_GRACE_S", "0.5")
    engine = FakeMainEngine(answer_tokens=400, delay_s=0.02).install(monkeypatch)
    real_mark = durable_store.mark_durable
    slowed = threading.Event()

    def slow_mark(*args: Any, **kwargs: Any) -> Any:
        slowed.set()
        time.sleep(1.0)
        return real_mark(*args, **kwargs)

    monkeypatch.setattr(durable_store, "mark_durable", slow_mark)
    keyed = _auth(extra={"Idempotency-Key": "left-during-launch"})
    with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
        with client.stream("POST", _url(port, "/v1/responses"), headers=keyed, json=_responses_body()) as response:
            assert response.status_code == 200
            next(response.iter_bytes())
            assert slowed.wait(5)
    rows = _wait_for(lambda: db.list_api_responses(platform["project"]["id"]))
    response_id = rows[0]["id"]

    row = _wait_for(
        lambda: (lambda r: r if r and r["status"] in durable_store.TERMINAL_STATUSES else None)(durable_store.get_run(response_id)),
        timeout=20,
    )
    assert row["resumable"] is True and row["status"] == "cancelled"
    with db.connection() as con:
        claim = con.execute("SELECT response_id FROM api_idempotency WHERE idem_key = %s", ("left-during-launch",)).fetchone()
    assert claim is not None and claim["response_id"] == response_id, "the launch bound the key to its run"
    _wait_for(lambda: _in_flight(platform["project"]["id"]) == 0 and not public_router._GATE_LINE.waiting)
    assert len(engine.calls) <= 1


def test_a_restart_ends_a_background_request_still_waiting_for_its_file_on_the_record(port, files_world, monkeypatch):
    notified: List[str] = []

    async def record_notify(row: Any, workspace_id: Any) -> None:
        notified.append(str(row["id"]))

    monkeypatch.setattr(background, "_notify", record_notify)
    store, fid, project_id = files_world["store"], files_world["file_id"], files_world["project_id"]
    store.update(project_id, fid, blob_status="processing", blob_stage="index", blob_progress={"percent": 10})
    accepted = httpx.post(_url(port, "/v1/responses"), json=_file_body(fid, background=True), headers=_auth(), timeout=30)
    assert accepted.status_code == 202, accepted.text
    response_id = accepted.json()["id"]
    time.sleep(0.3)

    _post_restart(port)

    row = db.get_api_response(response_id, project_id)
    assert row["status"] == "failed" and row["error_code"] == "model_unavailable"
    assert row["error_message"] == errors.PREPARATION_INTERRUPTED_MESSAGE
    assert notified == [response_id]
    assert files_world["engine"].messages == []


def test_a_keyed_file_stream_cut_mid_answer_resumes_from_the_spec_its_files_produced(port, files_world, monkeypatch):
    """The spec a preparing run stores is the FINAL one, written when its files
    resolved: a restart during the answer resumes with the file text, never
    from the plan made before the files were read."""
    engine = FakeMainEngine(answer_tokens=60, delay_s=0.01).install(monkeypatch)
    fid = files_world["file_id"]
    keyed = _auth(extra={"Idempotency-Key": "file-stream-resumed"})
    restarted = threading.Event()

    def restart_once(deltas: int) -> None:
        if deltas == 5 and not restarted.is_set():
            restarted.set()
            threading.Thread(target=_post_restart, args=(port,), daemon=True).start()

    cut = read_sse("POST", _url(port, "/v1/responses"), headers=keyed, json_body=_file_body(fid, stream=True),
                   on_delta=restart_once)
    response_id = _response_id(cut.records)
    assert cut.error is not None and restarted.is_set()
    _wait_for(lambda: durable_store.get_run(response_id)["lease_owner"] is None)
    resumed = read_sse(
        "GET", _url(port, f"/v1/responses/{response_id}"), headers=_auth(),
        params={"stream": "true", "starting_after": cut.records[-1]["data"]["sequence_number"]},
    )

    assert resumed.records[-1]["event"] == "response.completed"
    assert _text(cut.records + resumed.records) == expected_text(60)
    assert len(engine.calls) == 2 and engine.calls[1].kwargs["continue_final_message"] is True
    assert "AZURE-42" in json.dumps(engine.calls[0].messages) and "AZURE-42" in json.dumps(engine.calls[1].messages)


def test_a_waiter_place_moves_to_the_final_plans_gates_and_is_counted_in_exactly_one_line():
    public_router._GATE_LINE.reset_for_tests()
    holder = public_router._MovablePlace(public_router._place_for(["main.normal"]))
    assert public_router._GATE_LINE.waiting == {"main.normal": 1}
    holder.move_to(["main.normal"])
    assert public_router._GATE_LINE.waiting == {"main.normal": 1}
    holder.move_to(["main.long"])
    assert public_router._GATE_LINE.waiting == {"main.long": 1}
    holder.release()
    holder.move_to(["main.normal"])
    assert public_router._GATE_LINE.waiting == {}
