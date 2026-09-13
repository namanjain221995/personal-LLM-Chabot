"""tools/api_soak.py — the guards, the statistics and the run, against a fake engine.

The soak tool exists to put real load on the production engine during a
planned window and to STOP itself when the engine shows distress. Nothing in
this file touches a real engine: every rule is proven twice, once as a pure
function fed synthetic samples (so the 20 s and 30 s windows are tested with
exact timestamps), and once end to end against a fake `/v1` + `/metrics` +
chat-app server on a loopback port, with the windows shrunk to fractions of a
second (2026-09-13).

No database. The suite's autouse PostgreSQL fixtures are overridden below
because this module never opens the app, and a load tool's tests should run
on any machine that has httpx, starlette and uvicorn.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

_TOOL = Path(__file__).resolve().parents[2] / "tools" / "api_soak.py"
if not _TOOL.is_file():  # an image built from orchestrator/ alone
    pytest.skip(f"the soak tool is not in this checkout ({_TOOL})", allow_module_level=True)

_SPEC = importlib.util.spec_from_file_location("api_soak_tool", _TOOL)
assert _SPEC is not None and _SPEC.loader is not None
soak = importlib.util.module_from_spec(_SPEC)
# Registered before exec: dataclasses resolve their module through sys.modules.
sys.modules["api_soak_tool"] = soak
_SPEC.loader.exec_module(soak)

uvicorn = pytest.importorskip("uvicorn")
starlette_apps = pytest.importorskip("starlette.applications")
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402


# WHY these overrides (2026-09-13): tests/conftest.py makes a PostgreSQL test
# database autouse for every test, because the app applies its schema at
# startup. This module never imports the app, so the three autouse fixtures
# are replaced by name with no-ops — it then runs with no database at all.
@pytest.fixture(scope="session")
def app_database():
    yield None


@pytest.fixture
def isolated_app_db():
    yield None


@pytest.fixture
def ambient_identity():
    yield None


# ------------------------------------------------------------ fake engine --

API_KEY = "sk-soak-fake-key-0123456789"
MODEL = "techsara-35b"
SERVED = "fake/served-model"
CAPACITY_BODY = {
    "error": {
        "message": "The model is at capacity right now. This request is safe to retry.",
        "type": "service_unavailable_error",
        "code": "model_unavailable",
        "param": None,
        "request_id": "req_fake",
    }
}
RECOVERING_BODY = {
    "error": {
        "message": "The model is restarting. This request is safe to retry.",
        "type": "service_unavailable_error",
        "code": "model_recovering",
        "param": None,
        "request_id": "req_fake",
    }
}


class FakeEngine:
    """State and knobs behind the fake server. Mutated from the test thread
    and the server thread; every counter change holds the lock."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.base = ""
        self.ttft_s = 0.02
        self.tokens = 6
        self.token_gap_s = 0.005
        self.capacity: Optional[int] = None
        self.fail_on_post: Optional[Tuple[int, int, Any]] = None  # (nth post, status, body)
        self.stream_error_on_post: Optional[Tuple[int, str, str]] = None
        self.kv_base = 0.10
        self.kv_after_posts: Optional[Tuple[int, float]] = None
        self.preempt_on_post: Optional[int] = None
        self.wedge_on_post: Optional[int] = None
        self.silent_on_post: Optional[int] = None
        self.other_running = 0
        self.other_running_after_posts: Optional[Tuple[int, int]] = None
        self.drop_series: set = set()
        self.prom_age_s = 1.0
        self.chat_capacity_on_post: Optional[int] = None
        self.chat_parked_on_post: Optional[int] = None
        self.chat_error_sentence = "The model's queue is full right now. Please try again in a moment."
        self.adm_waiting = 0.0
        # observed
        self.posts = 0
        self.chat_posts = 0
        self.inflight = 0
        self.max_inflight = 0
        self.disconnects = 0
        self.metrics_hits = 0
        self.gen_total = 1000.0
        self.prompt_total = 5000.0
        self.iter_total = 300.0
        self.preemptions = 0.0
        self.wedged = False
        self.bodies: List[Dict[str, Any]] = []
        # the chat app's detached generations (main.py: a task per turn)
        self.chat_live: Dict[str, Any] = {}
        self.chat_running = 0
        self.chat_finished = 0
        self.chat_cancelled = 0
        self.stop_calls: List[Dict[str, Any]] = []
        self.stop_delay_s = 0.0
        self.adm_wait_sum = 0.0
        self.adm_wait_count = 0.0

    # metrics -------------------------------------------------------------

    def values(self) -> Dict[str, float]:
        with self.lock:
            others = self.other_running
            if self.other_running_after_posts and self.posts >= self.other_running_after_posts[0]:
                others += self.other_running_after_posts[1]
            running = self.inflight + others + self.chat_running
            # Other people GENERATE: without this, their running requests
            # with a flat token counter are a wedge, and the wedge guard
            # races the traffic check this knob exists to exercise.
            self.gen_total += others
            self.iter_total += 1 if others else 0
            kv = self.kv_base + 0.01 * self.inflight
            if self.kv_after_posts and self.posts >= self.kv_after_posts[0]:
                kv = self.kv_after_posts[1]
            return {
                "vllm:num_requests_running": float(running),
                "vllm:num_requests_waiting": 0.0,
                "vllm:kv_cache_usage_perc": kv,
                "vllm:num_preemptions_total": self.preemptions,
                "vllm:generation_tokens_total": self.gen_total,
                "vllm:prompt_tokens_total": self.prompt_total,
                "vllm:iteration_tokens_total_count": self.iter_total,
            }

    def exposition(self) -> str:
        lines = ["# HELP vllm:num_requests_running fake", "# TYPE vllm:num_requests_running gauge"]
        for name, value in self.values().items():
            if name in self.drop_series:
                continue
            lines.append(f'{name}{{engine="0",model_name="{SERVED}"}} {value}')
        # A second model on the same endpoint must not be added in.
        lines.append('vllm:num_requests_running{engine="0",model_name="other/model"} 99.0')
        return "\n".join(lines) + "\n"

    def admission(self) -> List[Tuple[str, Dict[str, str], float]]:
        """The orchestrator's lane series (app/admission.py): every post
        waited 0.5 s for the NORMAL lane; the LONG lane must not be added in."""
        with self.lock:
            return [
                ("llm_admission_lane_active", {"lane": "normal"}, float(self.inflight + self.chat_running)),
                ("llm_admission_waiting", {"lane": "normal"}, self.adm_waiting),
                ("llm_admission_wait_seconds_sum", {"lane": "normal"}, self.adm_wait_sum),
                ("llm_admission_wait_seconds_count", {"lane": "normal"}, self.adm_wait_count),
                ("llm_admission_lane_active", {"lane": "long"}, 7.0),
                ("llm_admission_waiting", {"lane": "long"}, 50.0),
            ]


def _chunk(obj: Dict[str, Any]) -> str:
    return "data: " + json.dumps(obj) + "\n\n"


def build_app(engine: FakeEngine):
    def authorised(request: Request) -> bool:
        return request.headers.get("authorization") == f"Bearer {API_KEY}"

    def unauthorised() -> JSONResponse:
        return JSONResponse({"error": {"message": "Invalid API key.", "type": "authentication_error",
                                       "code": "invalid_api_key", "param": None, "request_id": None}}, 401)

    async def models(request: Request):
        if not authorised(request):
            return unauthorised()
        return JSONResponse({"object": "list", "data": [{"id": MODEL, "object": "model"}]})

    async def metrics(request: Request):
        with engine.lock:
            engine.metrics_hits += 1
        return PlainTextResponse(engine.exposition())

    async def orchestrator_metrics(request: Request):
        lines = ["# TYPE llm_admission_waiting gauge"]
        for name, labels, value in engine.admission():
            lines.append(name + "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "} " + f"{value:g}")
        return PlainTextResponse("\n".join(lines) + "\n")

    async def prom_query(request: Request):
        query = request.query_params.get("query", "")
        now = time.time()
        if query.startswith("max(timestamp("):
            result = [{"metric": {}, "value": [now, str(now - engine.prom_age_s)]}]
        elif "llm_admission_" in query:
            result = [{"metric": {"__name__": name, "job": "orchestrator", **labels}, "value": [now, str(v)]}
                      for name, labels, v in engine.admission()]
        else:
            result = [{"metric": {"__name__": name, "job": "vllm-main", "model_name": SERVED}, "value": [now, str(v)]}
                      for name, v in engine.values().items() if name not in engine.drop_series]
        engine.bodies.append({"prom_query": query})
        return JSONResponse({"status": "success", "data": {"resultType": "vector", "result": result}})

    async def _admit(request: Request, surface: str):
        body = await request.json()
        with engine.lock:
            engine.posts += 1
            n = engine.posts
            engine.adm_wait_count += 1
            engine.adm_wait_sum += 0.5
            engine.bodies.append({"surface": surface, **{k: v for k, v in body.items() if k not in ("messages", "input")}})
            if engine.preempt_on_post is not None and n == engine.preempt_on_post:
                engine.preemptions += 1
            if engine.wedge_on_post is not None and n >= engine.wedge_on_post:
                engine.wedged = True
            at_capacity = engine.capacity is not None and engine.inflight >= engine.capacity
        return n, body, at_capacity

    async def completions(request: Request, surface: str = "v1-chat"):
        if not authorised(request):
            return unauthorised()
        n, body, at_capacity = await _admit(request, surface)
        if engine.fail_on_post and n == engine.fail_on_post[0]:
            status, payload = engine.fail_on_post[1], engine.fail_on_post[2]
            if isinstance(payload, dict):
                return JSONResponse(payload, status, headers={"Retry-After": "1"})
            return PlainTextResponse(str(payload), status)
        if at_capacity:
            return JSONResponse(CAPACITY_BODY, 503, headers={"Retry-After": "1"})

        async def stream():
            with engine.lock:
                engine.inflight += 1
                engine.max_inflight = max(engine.max_inflight, engine.inflight)
            finished = False
            try:
                if engine.silent_on_post is not None and n >= engine.silent_on_post:
                    await asyncio.sleep(3.0)  # headers sent, then not one byte
                    finished = True
                    return
                await asyncio.sleep(engine.ttft_s)
                yield ": ping\n\n"
                if surface == "v1-responses":
                    yield "event: response.created\n" + _chunk({"type": "response.created", "sequence_number": 1})
                for i in range(engine.tokens):
                    while engine.wedged:
                        yield ": ping\n\n"  # the server is alive; the engine is not
                        await asyncio.sleep(0.05)
                    if (engine.stream_error_on_post and n == engine.stream_error_on_post[0] and i == 2):
                        _, code, message = engine.stream_error_on_post
                        yield _chunk({"choices": [], "usage": None,
                                      "error": {"message": message, "type": "server_error", "code": code, "param": None}})
                        yield "data: [DONE]\n\n"
                        finished = True
                        return
                    with engine.lock:
                        engine.gen_total += 1
                        engine.iter_total += 1
                    if surface == "v1-responses":
                        yield "event: response.output_text.delta\n" + _chunk(
                            {"type": "response.output_text.delta", "delta": "tok ", "sequence_number": i + 2})
                    else:
                        yield _chunk({"id": "c", "object": "chat.completion.chunk", "created": 0, "model": MODEL,
                                      "choices": [{"index": 0, "delta": {"content": "tok "}, "finish_reason": None}]})
                    await asyncio.sleep(engine.token_gap_s)
                if surface == "v1-responses":
                    yield "event: response.completed\n" + _chunk({
                        "type": "response.completed", "sequence_number": engine.tokens + 2,
                        "response": {"status": "completed",
                                     "usage": {"input_tokens": 30, "output_tokens": engine.tokens, "total_tokens": 30 + engine.tokens}}})
                else:
                    yield _chunk({"id": "c", "object": "chat.completion.chunk", "created": 0, "model": MODEL,
                                  "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                    if (body.get("stream_options") or {}).get("include_usage"):
                        yield _chunk({"id": "c", "object": "chat.completion.chunk", "created": 0, "model": MODEL,
                                      "choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": engine.tokens,
                                                               "total_tokens": 30 + engine.tokens}})
                    yield "data: [DONE]\n\n"
                finished = True
            finally:
                with engine.lock:
                    engine.inflight -= 1
                    if not finished:
                        engine.disconnects += 1

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Request-Id": f"req_{n}"})

    async def responses(request: Request):
        return await completions(request, "v1-responses")

    async def login(request: Request):
        body = await request.json()
        if body.get("email") == "soak@test.local" and body.get("password") == "pw-fake":
            resp = JSONResponse({"ok": True})
            resp.headers["set-cookie"] = "ts_session=fake-session; Path=/; Secure; HttpOnly"
            return resp
        return JSONResponse({"detail": "bad credentials"}, 401)

    async def me(request: Request):
        if "ts_session=fake-session" not in request.headers.get("cookie", ""):
            return JSONResponse({"detail": "Sign in required."}, 401)
        return JSONResponse({"email": "soak@test.local"})

    async def chat(request: Request):
        if "ts_session=fake-session" not in request.headers.get("cookie", ""):
            return JSONResponse({"detail": "Sign in required."}, 401)
        body = await request.json()
        with engine.lock:
            engine.chat_posts += 1
            n = engine.chat_posts
            engine.bodies.append({"surface": "chat", **{k: v for k, v in body.items() if k not in ("messages", "message")}})
        conversation = str(body.get("conversation_id"))
        frames: "asyncio.Queue[Tuple[str, Dict[str, Any]]]" = asyncio.Queue()

        # DETACHED like main.py's /chat (review 2026-09-13): the turn is a
        # task of its own and the response only follows it, so a client that
        # disconnects stops NOTHING — only POST /chat/stop does. A fake that
        # stopped with the stream could never catch a tool that forgot that.
        async def generate():
            with engine.lock:
                engine.chat_running += 1
            try:
                await asyncio.sleep(engine.ttft_s)
                if engine.chat_capacity_on_post is not None and n == engine.chat_capacity_on_post:
                    frames.put_nowait(("error", {"message": engine.chat_error_sentence, "code": "TIMEOUT"}))
                    return
                if engine.chat_parked_on_post is not None and n == engine.chat_parked_on_post:
                    frames.put_nowait(("error", {"message": "The main model is still recovering. Your request is kept "
                                                            "and will resume automatically.",
                                                 "code": "MODEL_RECOVERING", "resumable": True}))
                    return
                frames.put_nowait(("step", {"id": 1, "title": "Thinking", "status": "running"}))
                for _ in range(engine.tokens):
                    with engine.lock:
                        engine.gen_total += 1
                        engine.iter_total += 1
                    frames.put_nowait(("token", {"text": "tok "}))
                    await asyncio.sleep(engine.token_gap_s)
                frames.put_nowait(("meta", {"route": "chat"}))
                frames.put_nowait(("done", {"session_id": body.get("session_id")}))
                with engine.lock:
                    engine.chat_finished += 1
            except asyncio.CancelledError:
                with engine.lock:
                    engine.chat_cancelled += 1
                frames.put_nowait(("error", {"message": "Stopped.", "code": "CANCELLED"}))
                raise
            finally:
                with engine.lock:
                    engine.chat_running -= 1

        engine.chat_live[conversation] = asyncio.get_running_loop().create_task(generate())

        async def follow():
            yield ": keep-alive\n\n"
            while True:
                event, payload = await frames.get()
                yield f"event: {event}\n" + _chunk(payload)
                if event in ("done", "error"):
                    return

        return StreamingResponse(follow(), media_type="text/event-stream")

    async def chat_stop(request: Request):
        if "ts_session=fake-session" not in request.headers.get("cookie", ""):
            return JSONResponse({"detail": "Sign in required."}, 401)
        body = await request.json()
        await asyncio.sleep(engine.stop_delay_s)
        task = engine.chat_live.get(str(body.get("conversation_id")))
        stopped = task is not None and not task.done()
        if stopped:
            task.cancel()
        with engine.lock:
            engine.stop_calls.append({**body, "stopped": stopped})
        return JSONResponse({"stopped": stopped})

    return starlette_apps.Starlette(routes=[
        Route("/v1/models", models, methods=["GET"]),
        Route("/v1/chat/completions", completions, methods=["POST"]),
        Route("/v1/responses", responses, methods=["POST"]),
        Route("/metrics", metrics, methods=["GET"]),
        Route("/orchestrator/metrics", orchestrator_metrics, methods=["GET"]),
        Route("/api/v1/query", prom_query, methods=["GET"]),
        Route("/auth/login", login, methods=["POST"]),
        Route("/auth/me", me, methods=["GET"]),
        Route("/chat", chat, methods=["POST"]),
        Route("/chat/stop", chat_stop, methods=["POST"]),
    ])


@pytest.fixture
def fake():
    engine = FakeEngine()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(build_app(engine), log_level="warning", lifespan="off", timeout_graceful_shutdown=1)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "the fake server did not start"
        time.sleep(0.02)
    engine.base = f"http://127.0.0.1:{port}"
    yield engine
    server.should_exit = True
    thread.join(10)
    sock.close()


def _args(engine: FakeEngine, out: Path, *extra: str) -> List[str]:
    base = [
        "--base-url", engine.base,
        "--metrics-url", engine.base + "/metrics", "--metrics-model-name", SERVED,
        "--stages", "2,4", "--stage-seconds", "0.6", "--cooldown-seconds", "0.4",
        "--traffic-settle-seconds", "0.1", "--traffic-window", "0.25", "--sample-interval", "0.05",
        "--kv-abort-seconds", "0.5", "--wedge-seconds", "0.5", "--metrics-blind-seconds", "1.0",
        "--mix", "tiny:1:20-40:4-8", "--drain-timeout", "5", "--stream-idle-timeout", "5",
        "--capacity-retry-cap", "0.05", "--out", str(out),
    ]
    return base + list(extra)


def _run(engine: FakeEngine, tmp_path: Path, *extra: str, confirm: bool = True):
    argv = _args(engine, tmp_path / "out", *extra) + (["--confirm-load"] if confirm and "--dry-run" not in extra else [])
    cfg = soak.config_from_args(soak.build_parser().parse_args(argv))
    lines: List[str] = []
    code, summary = asyncio.run(soak.run_soak(cfg, echo=lines.append))
    return code, summary, lines


def _wait_for(predicate, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _sample(t: float, **values: Any) -> Any:
    base = {"running": 1.0, "waiting": 0.0, "kv_usage": 0.2, "preemptions_total": 0.0,
            "generation_tokens_total": 100.0, "prompt_tokens_total": 50.0, "iterations_total": 10.0}
    base.update(values)
    return soak.EngineSample(t=t, wall=1_000_000.0 + t, values=base)


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("TECHSARA_API_KEY", API_KEY)
    return API_KEY


# ------------------------------------------------------------ pure pieces --


def test_nearest_rank_percentiles_match_a_hand_computed_list():
    data = [7, 1, 3, 10, 2, 9, 4, 8, 6, 5]
    assert soak.percentile(data, 50) == 5
    assert soak.percentile(data, 95) == 10
    assert soak.percentile(data, 90) == 9
    assert soak.percentile(data, 10) == 1
    assert soak.percentile(data, 0) == 1
    assert soak.percentile([], 50) is None
    assert soak.percentile([4.2], 99) == 4.2


def test_the_exposition_parser_sums_engines_takes_the_max_kv_and_ignores_other_models():
    text = "\n".join([
        "# TYPE vllm:num_requests_running gauge",
        'vllm:num_requests_running{engine="0",model_name="m"} 3.0',
        'vllm:num_requests_running{engine="1",model_name="m"} 4.0',
        'vllm:num_requests_running{engine="0",model_name="other"} 50.0',
        'vllm:kv_cache_usage_perc{engine="0",model_name="m"} 0.40',
        'vllm:kv_cache_usage_perc{engine="1",model_name="m"} 0.70',
        'vllm:generation_tokens_total{engine="0",model_name="m"} 1.5e3',
        'vllm:iteration_tokens_total_count{engine="0",model_name="m"} 12',
        'vllm:iteration_tokens_total_bucket{engine="0",le="1",model_name="m"} 99',
        'vllm:num_requests_running_extra{model_name="m"} 1000',
    ])
    values = soak.parse_exposition(text, "m")
    assert values["running"] == 7.0
    assert values["kv_usage"] == 0.70
    assert values["generation_tokens_total"] == 1500.0
    assert values["iterations_total"] == 12.0
    assert values["waiting"] is None and values["preemptions_total"] is None
    assert soak.parse_exposition(text)["running"] == 57.0  # no filter: every model counts


def test_a_stage_and_mix_spec_that_cannot_be_honoured_is_refused_before_anything_is_sent():
    assert soak.parse_stages("8, 16,32") == [8, 16, 32]
    for bad in ("", "8,x", "0", "5000"):
        with pytest.raises(soak.ConfigError):
            soak.parse_stages(bad)
    assert soak.parse_mix("a:1:10-20:5,b:2.5:30:40-50")[1].max_tokens == (40, 50)
    for bad in ("a:1:10-20", "a:0:10:10", "a:1:20-10:5", "a b:1:1:1", "a:1:1:1,a:1:1:1"):
        with pytest.raises(soak.ConfigError):
            soak.parse_mix(bad)
    cfg = soak.SoakConfig(base_url="http://127.0.0.1:1", mix=soak.parse_mix(soak.DEFAULT_MIX))
    errs = " | ".join(soak.validate_config(cfg))
    assert "metrics source" in errs
    assert "--confirm-load" in errs
    cfg.metrics_url, cfg.confirm_load, cfg.sample_interval_s = "http://127.0.0.1:1/metrics", True, 11.0
    assert any("half of the shortest guard window" in e for e in soak.validate_config(cfg))
    cfg.sample_interval_s = 2.0
    assert soak.validate_config(cfg) == []
    cfg.slo.ttft_p95_max_s = 0.0
    assert any("--ttft-p95-max must be > 0" in e for e in soak.validate_config(cfg))
    cfg.slo.ttft_p95_max_s = None


def test_an_unparseable_url_or_a_chat_surface_without_the_orchestrator_origin_is_a_config_error(tmp_path, capsys):
    cfg = soak.SoakConfig(base_url="http://127.0.0.1:1", mix=soak.parse_mix("m:1:1:1"), metrics_url="http://[::1",
                          confirm_load=True)
    assert any(e.startswith("--metrics-url is not a valid URL") for e in soak.validate_config(cfg))
    cfg.metrics_url = "http://127.0.0.1:1/metrics"
    cfg.chat_fraction, cfg.chat_email, cfg.chat_password_file = 0.5, "soak@test.local", "pw"
    assert any("needs --chat-base-url" in e for e in soak.validate_config(cfg))
    cfg.chat_base_url = "http://127.0.0.1:1"
    assert soak.validate_config(cfg) == []
    # the review's reproduction: this used to end in httpx.InvalidURL and exit 1
    code = soak.main(["--base-url", "http://127.0.0.1:1", "--metrics-url", "http://[::1", "--dry-run",
                      "--out", str(tmp_path / "out")])
    assert code == soak.EXIT_CONFIG
    assert "--metrics-url is not a valid URL" in capsys.readouterr().out


def test_kv_above_the_threshold_aborts_only_after_the_full_thirty_second_window_and_a_dip_resets_it():
    guards = soak.EngineGuards(soak.GuardConfig(), now=0.0)
    gen = 100.0
    for t in range(0, 30, 2):  # 0..28 s above the threshold: not yet
        gen += 10
        assert guards.observe(_sample(float(t), kv_usage=0.97, generation_tokens_total=gen)) is None
    assert guards.observe(_sample(29.0, kv_usage=0.50, generation_tokens_total=gen + 5)) is None  # the dip
    for t in range(30, 60, 2):  # a fresh 28 s window
        gen += 10
        assert guards.observe(_sample(float(t), kv_usage=0.96, generation_tokens_total=gen)) is None
    abort = guards.observe(_sample(60.0, kv_usage=0.96, generation_tokens_total=gen + 10))
    assert abort is not None and abort.reason == "kv_saturated"
    assert "0.960 > 0.95 for 30.0s" in abort.message


def test_a_rising_preemption_counter_aborts_at_once():
    guards = soak.EngineGuards(soak.GuardConfig(), now=0.0, baseline=_sample(0.0, preemptions_total=4.0))
    assert guards.observe(_sample(2.0, preemptions_total=4.0, generation_tokens_total=120.0)) is None
    abort = guards.observe(_sample(4.0, preemptions_total=5.0, generation_tokens_total=140.0))
    assert abort is not None and abort.reason == "preemption"
    tolerant = soak.EngineGuards(soak.GuardConfig(abort_on_preemption=False), now=0.0, baseline=_sample(0.0))
    assert tolerant.observe(_sample(2.0, preemptions_total=9.0, generation_tokens_total=120.0)) is None


def test_a_counter_that_goes_backwards_is_reported_as_an_engine_restart():
    guards = soak.EngineGuards(soak.GuardConfig(), now=0.0, baseline=_sample(0.0, generation_tokens_total=90_000.0))
    abort = guards.observe(_sample(2.0, generation_tokens_total=12.0, prompt_tokens_total=60.0))
    assert abort is not None and abort.reason == "engine_restarted"
    assert "generation_tokens_total went backwards" in abort.message


def test_flat_generation_with_requests_running_aborts_as_a_wedge_after_twenty_seconds():
    guards = soak.EngineGuards(soak.GuardConfig(), now=0.0)
    assert guards.observe(_sample(0.0, running=8.0)) is None  # progress clock starts here
    for t in range(2, 20, 2):
        assert guards.observe(_sample(float(t), running=8.0)) is None, t
    abort = guards.observe(_sample(20.0, running=8.0))
    assert abort is not None and abort.reason == "wedge"
    assert "flat at 100 for 20.0s with 8 requests running" in abort.message
    assert "scheduler-step counter is flat too" in abort.message


def test_flat_generation_is_not_a_wedge_when_nothing_is_running():
    guards = soak.EngineGuards(soak.GuardConfig(), now=0.0)
    for t in range(0, 120, 2):
        assert guards.observe(_sample(float(t), running=0.0)) is None
    # requests appear: the freeze is measured from THEN, not from minute 0
    assert guards.observe(_sample(120.0, running=3.0)) is None
    assert guards.observe(_sample(138.0, running=3.0)) is None
    assert guards.observe(_sample(140.0, running=3.0)).reason == "wedge"


def test_flat_generation_is_not_a_wedge_while_the_scheduler_is_still_stepping_through_a_long_prefill():
    # 2026-09-12: one ~950K prefill held both token counters flat for 12 min.
    guards = soak.EngineGuards(soak.GuardConfig(), now=0.0)
    for i, t in enumerate(range(0, 600, 2)):
        assert guards.observe(_sample(float(t), running=1.0, iterations_total=10.0 + i)) is None
    assert any("long prefill" in note for note in guards.notes)
    # ... and once the step counter stops as well, the plain rule applies again
    for t in range(600, 620, 2):
        assert guards.observe(_sample(float(t), running=1.0, iterations_total=5000.0)) is None
    assert guards.observe(_sample(622.0, running=1.0, iterations_total=5000.0)).reason == "wedge"


def test_fifteen_seconds_without_a_usable_metrics_sample_blinds_the_guards_and_aborts():
    guards = soak.EngineGuards(soak.GuardConfig(), now=0.0)
    assert guards.observe(_sample(0.0)) is None
    broken = soak.EngineSample(t=14.0, wall=0.0, values={"running": 1.0}, error="metrics HTTP 502")
    assert guards.observe(broken) is None
    missing = soak.EngineSample(t=15.0, wall=0.0, values={"running": 1.0, "waiting": 0.0})
    abort = guards.observe(missing)
    assert abort is not None and abort.reason == "metrics_blind"


def test_only_a_json_capacity_503_is_capacity_and_every_other_5xx_is_a_server_error():
    cap = re.compile(soak.DEFAULT_CAPACITY_PATTERN, re.I)
    body = lambda obj: json.dumps(obj).encode()  # noqa: E731
    assert soak.classify_http_failure(503, body(CAPACITY_BODY), cap)[0] == soak.CAPACITY
    assert soak.classify_http_failure(503, body(RECOVERING_BODY), cap)[0] == soak.SERVER_ERROR
    assert soak.classify_http_failure(503, b"<html>at capacity</html>", cap)[0] == soak.SERVER_ERROR
    assert soak.classify_http_failure(502, b"Bad gateway", cap)[0] == soak.SERVER_ERROR
    assert soak.classify_http_failure(500, body({"error": {"code": "internal_error", "message": "x"}}), cap)[0] == soak.SERVER_ERROR
    outcome, code, _ = soak.classify_http_failure(400, body({"error": {"code": "context_length_exceeded", "message": "too long"}}), cap)
    assert (outcome, code) == (soak.CLIENT_ERROR, "context_length_exceeded")
    assert soak.classify_http_failure(429, b"{}", cap)[0] == soak.CLIENT_ERROR


def test_in_stream_errors_are_classified_by_code_and_capacity_wording():
    cap = re.compile(soak.DEFAULT_CAPACITY_PATTERN, re.I)
    assert soak.classify_stream_error("v1-chat", "model_recovering", "restarting", cap) == soak.SERVER_ERROR
    assert soak.classify_stream_error("v1-chat", "", "", cap) == soak.SERVER_ERROR
    assert soak.classify_stream_error("v1-responses", "invalid_request_error", "bad", cap) == soak.CLIENT_ERROR
    # Both AdmissionRejected sentences of main.py _failure_sentence are capacity
    # (a /v1 caller gets model_at_capacity for both); only the read timeout is not.
    assert soak.classify_stream_error(
        "chat", "TIMEOUT", "The model's queue is full right now. Please try again in a moment.", cap) == soak.CAPACITY
    assert soak.classify_stream_error(
        "chat", "TIMEOUT", "The model is busy and could not start your request in time. Please try again.", cap) == soak.CAPACITY
    assert soak.classify_stream_error("v1-chat", "model_unavailable",
                                      "The model is at capacity right now. This request is safe to retry.", cap) == soak.CAPACITY
    assert soak.classify_stream_error("chat", "TIMEOUT", "The model did not answer in time.", cap) == soak.SERVER_ERROR
    assert soak.classify_stream_error("chat", "MODEL_RECOVERING", "The main model is still recovering.", cap) == soak.SERVER_ERROR


def test_the_sse_parser_reads_chat_completion_chunks_heartbeats_usage_and_done():
    raw = (": ping\n\n"
           + _chunk({"choices": [{"index": 0, "delta": {"content": "Hel"}, "finish_reason": None}]})
           + _chunk({"choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": None}]})
           + ": ping\n\n"
           + _chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})
           + _chunk({"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}})
           + "data: [DONE]\n\n")
    parser = soak.SSEParser()
    signals = []
    for line in raw.split("\n"):
        frame = parser.feed(line)
        if frame:
            signals.extend(soak.interpret_frame("v1-chat", *frame))
    assert parser.comments == 2
    assert [s for s in signals if s[0] == "delta"] == [("delta", "Hel"), ("delta", "lo")]
    assert ("finish", "length") in signals and ("usage", (9, 2)) in signals and signals[-1] == ("done", None)
    err = soak.interpret_frame("v1-chat", "message", json.dumps({"choices": [], "usage": None,
                                                                "error": {"code": "model_recovering", "message": "m"}}))
    assert err == [("error", ("model_recovering", "m"))]


def test_responses_and_chat_app_frames_become_the_same_signals():
    completed = {"type": "response.completed", "response": {"usage": {"input_tokens": 3, "output_tokens": 7}}}
    assert soak.interpret_frame("v1-responses", "response.output_text.delta",
                                json.dumps({"type": "response.output_text.delta", "delta": "x"})) == [("delta", "x")]
    assert soak.interpret_frame("v1-responses", "response.completed", json.dumps(completed)) == [
        ("usage", (3, 7)), ("finish", "stop"), ("done", None)]
    # a truncated answer: still response.completed, with incomplete_details (publicapi/models.py)
    truncated = {"type": "response.completed", "response": {"status": "completed", "incomplete_details": {"reason": "max_output_tokens"},
                                                            "usage": {"input_tokens": 3, "output_tokens": 64}}}
    assert soak.interpret_frame("v1-responses", "response.completed", json.dumps(truncated)) == [
        ("usage", (3, 64)), ("finish", "length"), ("done", None)]
    nulled = {"type": "response.completed", "response": {"incomplete_details": None, "usage": {"input_tokens": 3, "output_tokens": 7}}}
    assert ("finish", "stop") in soak.interpret_frame("v1-responses", "response.completed", json.dumps(nulled))
    assert soak.interpret_frame("v1-responses", "error", json.dumps({"type": "error", "code": "timeout", "message": "t"})) == [
        ("error", ("timeout", "t"))]
    assert soak.interpret_frame("chat", "token", json.dumps({"text": "hi"})) == [("delta", "hi")]
    assert soak.interpret_frame("chat", "step", json.dumps({"id": 1})) == []
    assert soak.interpret_frame("chat", "done", "{}") == [("done", None)]
    assert soak.interpret_frame("chat", "error", json.dumps({"message": "m", "code": "TIMEOUT"})) == [("error", ("TIMEOUT", "m"))]


def test_traffic_already_on_the_engine_refuses_the_start():
    cfg = soak.TrafficConfig()
    quiet = [_sample(0.0, running=1.0, generation_tokens_total=100.0), _sample(15.0, running=0.0, generation_tokens_total=104.0)]
    ok, detail = soak.evaluate_traffic(quiet, cfg)
    assert ok, detail
    busy = [_sample(0.0, running=2.0, waiting=1.0), _sample(15.0, running=0.0)]
    ok, detail = soak.evaluate_traffic(busy, cfg)
    assert not ok and "3 running+waiting" in detail["reason"]
    streaming_user = [_sample(0.0, running=1.0, generation_tokens_total=100.0),
                      _sample(10.0, running=1.0, generation_tokens_total=900.0)]
    ok, detail = soak.evaluate_traffic(streaming_user, cfg)
    assert not ok and "80.0 tokens/s" in detail["reason"]
    ok, detail = soak.evaluate_traffic([_sample(0.0)], cfg)
    assert not ok and "fewer than two usable" in detail["reason"]


def test_stage_statistics_report_ttft_percentiles_per_stream_decode_rate_and_aggregate_throughput():
    results = []
    for i in range(20):
        r = soak.RequestResult(0, 4, i % 4, i, "v1-chat", "tiny", 20, 64, started_at=0.0, outcome=soak.OK,
                               output_tokens=41, tokens_exact=True, finish_reason="stop")
        r.finish_timing(t_start=0.0, t_first=0.1 * (i + 1), t_end=0.1 * (i + 1) + 2.0)  # 40 tokens after the first in 2 s
        results.append(r)
    results.append(soak.RequestResult(0, 4, 0, 99, "v1-chat", "tiny", 20, 64, started_at=0.0, outcome=soak.CAPACITY))
    snaps = {"start": {"_t": 0.0, "generation_tokens_total": 1000.0, "preemptions_total": 0.0},
             "deadline": {"_t": 10.0, "generation_tokens_total": 1700.0, "preemptions_total": 0.0},
             "end": {"_t": 12.0, "generation_tokens_total": 1820.0, "preemptions_total": 0.0}}
    samples = [{"usable": True, "phase": "load", "inflight": 4, "running": 4.0, "waiting": 1.0, "kv_usage": 0.3},
               {"usable": True, "phase": "load", "inflight": 2, "running": 6.0, "waiting": 0.0, "kv_usage": 0.2}]
    stats = soak.stage_stats(results, samples, snaps, load_seconds=10.0, wall_seconds=12.0)
    assert stats["ttft_s"]["p50"] == 1.0 and stats["ttft_s"]["p95"] == 1.9 and stats["ttft_s"]["max"] == 2.0
    assert stats["decode_tps_per_stream"]["p50"] == 20.0
    assert stats["capacity_503"] == 1 and stats["ok"] == 20 and stats["errors"] == 0
    assert stats["client_tps_aggregate"] == round(20 * 41 / 12.0, 2)
    assert stats["engine_tps_load_window"] == 70.0 and stats["engine_tps_stage"] == round(820 / 12.0, 2)
    assert stats["engine_max"] == {"running": 6.0, "waiting": 1.0, "kv_usage": 0.3}
    assert stats["mean_inflight_load"] == 3.0 and stats["preemptions_delta"] == 0.0


def test_request_plans_are_deterministic_for_a_seed_and_follow_the_mix_weights():
    cfg = soak.SoakConfig(mix=soak.parse_mix("rare:1:10-20:5-9,common:9:30-40:50-60"), seed=7)
    a = soak.plan_request(cfg, "run1", 0, 8, 3, 5)
    b = soak.plan_request(cfg, "run1", 0, 8, 3, 5)
    assert (a.mix, a.prompt, a.max_tokens) == (b.mix, b.prompt, b.max_tokens)
    names = [soak.plan_request(cfg, "run1", 0, 8, w, s).mix for w in range(10) for s in range(100)]
    share = names.count("common") / len(names)
    assert 0.85 < share < 0.95
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", a.conversation_id)


def test_two_runs_with_the_same_seed_send_byte_identical_prompts_unless_asked_for_unique_ones():
    cfg = soak.SoakConfig(mix=soak.parse_mix(soak.DEFAULT_MIX), seed=3)
    first, second = soak.Soak(cfg, echo=lambda _l: None), soak.Soak(cfg, echo=lambda _l: None)
    assert first.run_id != second.run_id
    a = [soak.plan_request(cfg, first.run_id, 1, 8, w, s) for w in range(4) for s in range(5)]
    b = [soak.plan_request(cfg, second.run_id, 1, 8, w, s) for w in range(4) for s in range(5)]
    assert [p.prompt for p in a] == [p.prompt for p in b]
    # a conversation id never repeats across runs: reusing one appends to the old conversation
    assert all(x.conversation_id != y.conversation_id for x, y in zip(a, b))
    cfg.unique_prompts = True
    c = soak.plan_request(cfg, first.run_id, 1, 8, 0, 0)
    d = soak.plan_request(cfg, second.run_id, 1, 8, 0, 0)
    assert c.prompt != d.prompt and len(c.prompt) == len(d.prompt)


def test_the_kv_estimate_uses_fixed_state_blocks_plus_attention_blocks():
    layout = soak.KvLayout(total_blocks=799, block_tokens=2096, fixed_blocks=3)
    one = soak.parse_mix("m:1:1000-1000:96-96")  # 1,096 tokens -> 3 + 1 blocks
    assert soak.kv_estimate(one, 1, layout) == {"expected_usage": round(4 / 799, 3), "all_heaviest_usage": round(4 / 799, 3)}
    heavy = soak.parse_mix("p:1:32000-32000:1024-1024")  # 33,024 -> 3 + 16
    assert soak.kv_estimate(heavy, 64, layout)["all_heaviest_usage"] == round(64 * 19 / 799, 3)
    assert soak.kv_estimate(one, 1, soak.KvLayout(total_blocks=0)) is None


# --------------------------------------------------- the run, end to end --


def test_a_clean_ramp_passes_and_reports_every_stage_with_engine_samples(fake, tmp_path, api_key):
    code, summary, lines = _run(fake, tmp_path)
    assert code == soak.EXIT_PASS, summary.get("abort") or lines
    assert summary["verdict"] == "PASS"
    assert [s["concurrency"] for s in summary["stages"]] == [2, 4]
    for stage in summary["stages"]:
        assert stage["ok"] > 0 and stage["errors"] == 0 and stage["capacity_503"] == 0
        assert stage["ttft_s"]["p50"] is not None and stage["ttft_s"]["p95"] >= stage["ttft_s"]["p50"]
        assert stage["decode_tps_per_stream"]["p50"] > 0
        assert stage["engine_tps_load_window"] > 0 and stage["tokens_exact_share"] == 1.0
        assert stage["engine_max"]["running"] >= 1
        assert stage["traffic_after"] if stage["stage"] == 1 else True
    assert fake.max_inflight <= 4
    sent = [b for b in fake.bodies if b.get("surface") == "v1-chat"]
    assert sent and all(b["stream"] is True and b["stream_options"] == {"include_usage": True} and b["model"] == MODEL for b in sent)
    assert _wait_for(lambda: fake.inflight == 0)


def test_capacity_503s_are_counted_and_do_not_abort_the_ramp(fake, tmp_path, api_key):
    fake.capacity = 3
    code, summary, _ = _run(fake, tmp_path)
    assert code == soak.EXIT_PASS, summary.get("abort")
    assert summary["stages"][0]["capacity_503"] == 0
    assert summary["stages"][1]["capacity_503"] > 0
    assert summary["first_stage_with_capacity_503"] == 2
    assert summary["totals"]["capacity_503"] == summary["stages"][1]["capacity_503"]


def test_a_500_aborts_immediately_and_no_later_stage_runs(fake, tmp_path, api_key):
    fake.fail_on_post = (3, 500, {"error": {"message": "boom", "type": "server_error", "code": "internal_error", "param": None}})
    code, summary, lines = _run(fake, tmp_path, "--stages", "2,4,8", "--stage-seconds", "1.5")
    assert code == soak.EXIT_ABORTED
    assert summary["abort"]["reason"] == "server_error"
    assert summary["abort"]["evidence"]["status"] == 500
    assert len(summary["stages"]) == 1
    assert any(line.startswith("ABORT [server_error]") for line in lines)
    assert _wait_for(lambda: fake.inflight == 0), "open streams were not closed on abort"


def test_a_model_recovering_503_is_not_mistaken_for_capacity(fake, tmp_path, api_key):
    fake.fail_on_post = (2, 503, RECOVERING_BODY)
    code, summary, _ = _run(fake, tmp_path)
    assert code == soak.EXIT_ABORTED
    assert summary["abort"]["reason"] == "server_error" and summary["abort"]["evidence"]["code"] == "model_recovering"


def test_an_in_stream_server_error_after_a_200_aborts_the_run(fake, tmp_path, api_key):
    fake.stream_error_on_post = (2, "internal_error", "The answer could not be completed.")
    code, summary, _ = _run(fake, tmp_path)
    assert code == soak.EXIT_ABORTED
    assert summary["abort"]["reason"] == "server_error"
    assert summary["abort"]["evidence"]["status"] == 200


def test_kv_saturation_on_the_engine_aborts_the_run(fake, tmp_path, api_key):
    fake.kv_after_posts = (1, 0.97)
    code, summary, _ = _run(fake, tmp_path, "--stage-seconds", "3")
    assert code == soak.EXIT_ABORTED
    assert summary["abort"]["reason"] == "kv_saturated"
    assert summary["abort"]["evidence"]["kv_usage"] == 0.97


def test_a_preemption_during_the_stage_aborts_the_run(fake, tmp_path, api_key):
    fake.preempt_on_post = 5
    code, summary, _ = _run(fake, tmp_path, "--stage-seconds", "3")
    assert code == soak.EXIT_ABORTED
    assert summary["abort"]["reason"] == "preemption"


def test_a_wedged_engine_aborts_the_run_and_closes_every_open_stream(fake, tmp_path, api_key):
    fake.wedge_on_post = 3
    started = time.monotonic()
    code, summary, _ = _run(fake, tmp_path, "--stage-seconds", "5")
    assert code == soak.EXIT_ABORTED
    assert summary["abort"]["reason"] == "wedge"
    assert time.monotonic() - started < 5, "the wedge must end the run long before the stage would"
    assert summary["totals"]["cancelled"] >= 1
    assert _wait_for(lambda: fake.inflight == 0), f"{fake.inflight} streams still open on the server"
    assert fake.disconnects >= 1
    rows = [json.loads(line) for line in (tmp_path / "out" / "requests.jsonl").read_text().splitlines()]
    assert any(r["outcome"] == "cancelled" and "closed by the tool (abort)" in r["error"] for r in rows)


def test_real_traffic_before_the_start_refuses_without_sending_a_single_generation(fake, tmp_path, api_key):
    fake.other_running = 5
    code, summary, _ = _run(fake, tmp_path)
    assert code == soak.EXIT_REFUSED
    assert summary["verdict"] == "REFUSED" and summary["abort"]["reason"] == "traffic_present"
    assert fake.posts == 0


def test_the_engine_still_busy_after_a_stage_drains_aborts_between_stages(fake, tmp_path, api_key):
    fake.other_running_after_posts = (2, 5)  # someone else arrives during stage 1
    code, summary, _ = _run(fake, tmp_path, "--stages", "2,4,8")
    assert code == soak.EXIT_ABORTED
    assert summary["abort"]["reason"] == "traffic_between_stages"
    assert len(summary["stages"]) == 1


def test_an_idle_stream_counts_against_the_error_budget(fake, tmp_path, api_key):
    fake.silent_on_post = 1
    code, summary, _ = _run(fake, tmp_path, "--stream-idle-timeout", "0.3", "--stages", "1", "--stage-seconds", "0.2")
    assert code == soak.EXIT_ABORTED
    assert summary["abort"]["reason"] == "error_budget"
    assert summary["abort"]["evidence"]["outcome"] == "stream_idle"


def test_dry_run_checks_key_models_and_metrics_but_sends_no_generation(fake, tmp_path, api_key):
    code, summary, _ = _run(fake, tmp_path, "--dry-run")
    assert code == soak.EXIT_PASS and summary["verdict"] == "DRY_RUN_OK"
    assert summary["connectivity"]["v1_models"]["model_listed"] is True
    assert summary["connectivity"]["metrics"]["usable"] is True
    assert summary["traffic_pre_start"]["usable"] >= 2
    assert fake.posts == 0 and fake.chat_posts == 0


def test_dry_run_with_a_wrong_key_or_missing_engine_series_fails_connectivity(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("TECHSARA_API_KEY", "sk-wrong")
    code, summary, _ = _run(fake, tmp_path, "--dry-run")
    assert code == soak.EXIT_CONFIG and summary["connectivity"]["v1_models"]["status"] == 401
    monkeypatch.setenv("TECHSARA_API_KEY", API_KEY)
    fake.drop_series = {"vllm:kv_cache_usage_perc"}
    code, summary, _ = _run(fake, tmp_path, "--dry-run")
    assert code == soak.EXIT_CONFIG
    assert "missing series: kv_usage" in summary["connectivity"]["metrics"]["error"]
    monkeypatch.delenv("TECHSARA_API_KEY")
    code, summary, _ = _run(fake, tmp_path, "--dry-run")
    assert code == soak.EXIT_CONFIG and "no API key" in summary["errors"][0]
    assert fake.posts == 0


def test_load_is_refused_without_confirm_load_and_nothing_is_contacted(fake, tmp_path, api_key):
    code, summary, _ = _run(fake, tmp_path, confirm=False)
    assert code == soak.EXIT_CONFIG
    assert any("--confirm-load" in e for e in summary["errors"])
    assert fake.posts == 0 and fake.metrics_hits == 0


def _chat_args(fake: FakeEngine, tmp_path: Path) -> List[str]:
    password = tmp_path / "pw"
    password.write_text("pw-fake\n")
    return ["--chat-fraction", "1", "--chat-base-url", fake.base, "--chat-email", "soak@test.local",
            "--chat-password-file", str(password)]


@pytest.mark.parametrize("sentence", [
    "The model's queue is full right now. Please try again in a moment.",
    "The model is busy and could not start your request in time. Please try again.",
])
def test_chat_app_turns_sign_in_stream_tokens_and_count_both_lane_refusals_as_capacity(fake, tmp_path, sentence):
    fake.chat_capacity_on_post, fake.chat_error_sentence = 2, sentence
    code, summary, lines = _run(fake, tmp_path, *_chat_args(fake, tmp_path), "--stages", "2", "--stage-seconds", "0.5")
    assert code == soak.EXIT_PASS, summary.get("abort") or lines
    assert summary["connectivity"]["chat_sign_in"] == {"ok": True, "login_status": 200, "me_status": 200}
    stage = summary["stages"][0]
    assert stage["ok"] > 0 and stage["capacity_503"] == 1
    assert stage["tokens_exact_share"] == 0.0  # the chat app reports no usage: counted from deltas
    chat = [b for b in fake.bodies if b.get("surface") == "chat"]
    assert chat and all(b["mode"] == "assistant" and b["effort"] == "fast" and b["web_search"] == "off" for b in chat)
    assert fake.posts == 0
    # the refused turn got its (no-op) stop; the answered ones needed none
    assert summary["chat_turns"]["stops_sent"] == 1 and summary["chat_turns"]["stopped_while_generating"] == 0
    assert summary["chat_turns"]["requests"] == stage["requests"]
    rows = [json.loads(line) for line in (tmp_path / "out" / "requests.jsonl").read_text().splitlines()]
    assert all(r["conversation_id"].startswith(f"soak-{summary['run_id']}-") for r in rows)


def test_an_abort_sends_chat_stop_for_every_open_chat_turn_and_the_detached_turns_stop_generating(fake, tmp_path):
    # Review 2026-09-13: closing a /chat stream stops nothing (main.py detaches
    # the turn), so an abort that only closed streams left every turn
    # generating on the engine it was meant to protect.
    fake.tokens, fake.token_gap_s = 400, 0.01  # 4 s turns: all four are mid-answer at the abort
    argv = _args(fake, tmp_path / "out", *_chat_args(fake, tmp_path), "--stages", "4", "--stage-seconds", "5", "--confirm-load")
    cfg = soak.config_from_args(soak.build_parser().parse_args(argv))

    async def scenario():
        run = soak.Soak(cfg, echo=lambda _line: None)
        task = asyncio.create_task(run.run())
        for _ in range(250):
            await asyncio.sleep(0.02)
            if fake.chat_running >= 4:
                break
        await asyncio.sleep(0.2)
        run.trigger(soak.Abort("interrupted", "received SIGINT"))
        return await task

    started = time.monotonic()
    code, summary = asyncio.run(scenario())
    assert code == soak.EXIT_INTERRUPTED, summary.get("abort")
    assert time.monotonic() - started < 4
    chat = summary["chat_turns"]
    assert chat["stopped_while_generating"] == 4 and chat["stop_failures"] == []
    assert _wait_for(lambda: fake.chat_running == 0, 2.0), f"{fake.chat_running} chat turns still generating"
    assert fake.chat_cancelled == 4 and fake.chat_finished == 0
    rows = [json.loads(line) for line in (tmp_path / "out" / "requests.jsonl").read_text().splitlines()]
    closed = {r["conversation_id"] for r in rows if r["outcome"] == "cancelled"}
    assert closed == {c["conversation_id"] for c in fake.stop_calls if c["stopped"]}
    assert all(c["session_id"] == c["conversation_id"] for c in fake.stop_calls)


def test_chat_turns_left_open_by_a_drain_timeout_are_stopped_before_the_between_stage_traffic_check(fake, tmp_path):
    # Without the stop, the tool's own four orphaned turns were "real users"
    # to the traffic check and the run aborted as traffic_between_stages. The
    # stops are slow here, so the stage must WAIT for them before that check.
    fake.tokens, fake.token_gap_s, fake.stop_delay_s = 400, 0.01, 0.6
    code, summary, lines = _run(fake, tmp_path, *_chat_args(fake, tmp_path), "--stages", "4,4",
                                "--stage-seconds", "0.3", "--drain-timeout", "0.3")
    assert summary.get("abort") is None, summary.get("abort")
    assert code == soak.EXIT_FAIL and summary["verdict"] == "FAIL"
    assert [s["drain_cancelled"] for s in summary["stages"]] == [4, 4]
    assert summary["stages"][0]["traffic_after"]["max_busy"] <= 2
    assert summary["chat_turns"]["stopped_while_generating"] == 8
    assert _wait_for(lambda: fake.chat_running == 0, 2.0)


def test_a_chat_turn_the_orchestrator_parked_is_listed_for_an_operator(fake, tmp_path):
    fake.chat_parked_on_post = 2
    code, summary, lines = _run(fake, tmp_path, *_chat_args(fake, tmp_path), "--stages", "2", "--stage-seconds", "0.5")
    assert code == soak.EXIT_ABORTED and summary["abort"]["reason"] == "server_error"
    parked = summary["chat_turns"]["parked_conversation_ids"]
    assert len(parked) == 1 and parked[0].startswith(summary["chat_turns"]["conversation_prefix"])
    assert any(line.startswith("WARNING: 1 chat turn(s) were parked") for line in lines)


def test_the_responses_surface_measures_usage_from_the_completed_event(fake, tmp_path, api_key):
    code, summary, _ = _run(fake, tmp_path, "--surface", "v1-responses", "--stages", "2", "--stage-seconds", "0.4")
    assert code == soak.EXIT_PASS, summary.get("abort")
    assert summary["stages"][0]["tokens_exact_share"] == 1.0
    assert summary["stages"][0]["output_tokens"] == summary["stages"][0]["ok"] * fake.tokens
    assert all("max_output_tokens" in b for b in fake.bodies if b.get("surface") == "v1-responses")


def test_the_prometheus_source_reads_the_same_series_and_refuses_a_stale_scrape(fake):
    async def read(age: float):
        fake.prom_age_s = age
        async with httpx.AsyncClient() as client:
            return await soak.PrometheusSource(client, fake.base, 'job="vllm-main"', max_age_s=15.0).sample()

    fresh = asyncio.run(read(1.0))
    assert fresh.usable and fresh.values["kv_usage"] == pytest.approx(0.10)
    queries = [b["prom_query"] for b in fake.bodies if "prom_query" in b]
    assert any('job="vllm-main"' in q and "__name__=~" in q for q in queries)
    stale = asyncio.run(read(60.0))
    assert not stale.usable and stale.error.startswith("stale")


def test_the_cli_writes_requests_samples_and_a_summary_without_the_api_key(fake, tmp_path, api_key, capsys):
    out = tmp_path / "cli"
    argv = _args(fake, out, "--stages", "2", "--stage-seconds", "0.3", "--confirm-load")
    assert soak.main(argv) == soak.EXIT_PASS
    printed = capsys.readouterr().out
    assert "PASS" in printed and "stage 1:" in printed
    summary = json.loads((out / "summary.json").read_text())
    assert summary["verdict"] == "PASS" and summary["config"]["api_key_source"] == "env:TECHSARA_API_KEY"
    assert (out / "requests.jsonl").read_text().strip() and (out / "samples.jsonl").read_text().strip()
    for name in ("summary.json", "requests.jsonl", "samples.jsonl"):
        assert API_KEY not in (out / name).read_text()
    assert API_KEY not in printed


def test_concurrent_engine_reads_reach_the_guards_in_the_order_they_were_taken():
    # 2026-09-13: the monitor and a stage snapshot read /metrics at the same
    # time; the EARLIER reading finished second, and the guards aborted a
    # healthy run as "engine restarted" (generation_tokens_total 1126 -> 1120).
    class SlowFirstSource:
        calls = 0

        async def sample(self):
            SlowFirstSource.calls += 1
            n = SlowFirstSource.calls
            value = 1000.0 + n  # the counter as it was when this read began
            await asyncio.sleep(0.2 if n == 1 else 0.0)
            return _sample(time.monotonic(), running=2.0, generation_tokens_total=value)

    async def scenario():
        run = soak.Soak(soak.SoakConfig(mix=soak.parse_mix("m:1:1:1")), echo=lambda _line: None)
        run.source = SlowFirstSource()
        run.guards = soak.EngineGuards(soak.GuardConfig(), now=time.monotonic())
        await asyncio.gather(run._read_engine("monitor"), run._read_engine("snapshot:start"))
        return run

    run = asyncio.run(scenario())
    assert run.abort is None, run.abort
    assert [row["generation_tokens_total"] for row in run.samples] == [1001.0, 1002.0]


def test_a_frame_the_tool_cannot_read_is_recorded_as_a_tool_error_instead_of_killing_the_worker():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b'data: {"choices": [1]}\n\ndata: [DONE]\n\n')

    async def scenario():
        cfg = soak.SoakConfig(base_url="http://fake.invalid", mix=soak.parse_mix("m:1:10:5"))
        plan = soak.plan_request(cfg, "r", 0, 1, 0, 0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await soak.execute_request(client, plan, cfg, re.compile(soak.DEFAULT_CAPACITY_PATTERN))

    result = asyncio.run(scenario())
    assert result.outcome == soak.TOOL_ERROR and "AttributeError" in result.error
    assert soak.TOOL_ERROR in soak.BUDGET_OUTCOMES


def test_an_interrupt_mid_stage_closes_the_streams_and_reports_interrupted(fake, tmp_path, api_key):
    argv = _args(fake, tmp_path / "out", "--stages", "4,8", "--stage-seconds", "5", "--confirm-load")
    cfg = soak.config_from_args(soak.build_parser().parse_args(argv))
    fake.tokens, fake.token_gap_s = 400, 0.01  # long streams, so some are open at the interrupt

    async def scenario():
        run = soak.Soak(cfg, echo=lambda _line: None)
        task = asyncio.create_task(run.run())
        await asyncio.sleep(1.0)
        run.trigger(soak.Abort("interrupted", "received SIGINT"))
        return await task

    started = time.monotonic()
    code, summary = asyncio.run(scenario())
    assert code == soak.EXIT_INTERRUPTED and summary["verdict"] == "INTERRUPTED"
    assert time.monotonic() - started < 4
    assert summary["totals"]["cancelled"] >= 1 and len(summary["stages"]) == 1
    assert _wait_for(lambda: fake.inflight == 0)


def test_an_exception_inside_a_metrics_read_becomes_an_unusable_sample_and_the_blind_guard_aborts(fake, tmp_path, api_key, monkeypatch):
    original = soak.ExpositionSource.sample
    calls = {"n": 0}

    async def breaks_after_five(self):
        calls["n"] += 1
        if calls["n"] > 5:
            raise RuntimeError("a bug in a metrics parser")
        return await original(self)

    monkeypatch.setattr(soak.ExpositionSource, "sample", breaks_after_five)
    fake.tokens, fake.token_gap_s = 400, 0.01
    started = time.monotonic()
    code, summary, _ = _run(fake, tmp_path, "--stages", "4", "--stage-seconds", "5", "--traffic-window", "0.1")
    assert code == soak.EXIT_ABORTED and summary["abort"]["reason"] == "metrics_blind", summary.get("abort")
    assert time.monotonic() - started < 4
    assert "metrics read failed: RuntimeError" in (tmp_path / "out" / "samples.jsonl").read_text()
    assert _wait_for(lambda: fake.inflight == 0)


@pytest.mark.parametrize("which", ["_sample_fh", "_req_fh"])
def test_an_unwritable_evidence_file_aborts_the_run_instead_of_silently_killing_a_task(fake, tmp_path, api_key, monkeypatch, which):
    # The review's reproduction: ENOSPC on a monitor sample write, then a
    # wedge. Before the fix the monitor died, the wedge guard never fired and
    # the run held its streams until the stage and the drain ran out. The
    # same OSError on a request row killed the worker that wrote it.
    class DiskFull:
        name = "samples.jsonl"

        def write(self, _text):
            raise OSError(28, "No space left on device")

        def flush(self):
            pass

        def close(self):
            pass

    original = soak.Soak._record_sample
    calls = {"monitor": 0}

    def fills_the_disk(self, sample, kind="monitor"):
        if kind == "monitor":
            calls["monitor"] += 1
            if calls["monitor"] == 2:
                setattr(self, which, DiskFull())
        return original(self, sample, kind)

    monkeypatch.setattr(soak.Soak, "_record_sample", fills_the_disk)
    if which == "_sample_fh":
        fake.wedge_on_post = 3
    started = time.monotonic()
    code, summary, _ = _run(fake, tmp_path, "--stages", "4", "--stage-seconds", "5")
    assert code == soak.EXIT_ABORTED and summary["abort"]["reason"] == "evidence_unwritable", summary.get("abort")
    assert "No space left on device" in summary["abort"]["message"]
    assert time.monotonic() - started < 4
    assert _wait_for(lambda: fake.inflight == 0)


def test_a_monitor_task_that_dies_aborts_the_run_and_closes_every_stream(fake, tmp_path, api_key, monkeypatch):
    async def dies(self):
        await self._read_engine("monitor")
        raise RuntimeError("monitor bug")

    monkeypatch.setattr(soak.Soak, "_monitor", dies)
    fake.wedge_on_post = 3
    started = time.monotonic()
    code, summary, _ = _run(fake, tmp_path, "--stages", "4", "--stage-seconds", "5")
    assert code == soak.EXIT_ABORTED and summary["abort"]["reason"] == "monitor_died", summary.get("abort")
    assert "RuntimeError: monitor bug" in summary["abort"]["message"]
    assert time.monotonic() - started < 3
    assert _wait_for(lambda: fake.inflight == 0)


def test_a_hung_monitor_is_caught_by_the_stage_loop_within_the_blind_window(fake, tmp_path, api_key, monkeypatch):
    async def hangs(self):
        await asyncio.sleep(3600)

    monkeypatch.setattr(soak.Soak, "_monitor", hangs)
    fake.wedge_on_post = 3
    started = time.monotonic()
    code, summary, _ = _run(fake, tmp_path, "--stages", "4", "--stage-seconds", "5")
    assert code == soak.EXIT_ABORTED and summary["abort"]["reason"] == "metrics_blind", summary.get("abort")
    assert "checked by the stage loop" in summary["abort"]["evidence"]["last_error"]
    assert time.monotonic() - started < 4
    assert _wait_for(lambda: fake.inflight == 0)


def test_a_latency_gate_the_stage_misses_turns_pass_into_fail_and_an_ungated_pass_says_latency_was_not_judged(
        fake, tmp_path, api_key):
    code, summary, _ = _run(fake, tmp_path, "--stages", "2", "--stage-seconds", "0.4", "--ttft-p95-max", "0.0001")
    assert code == soak.EXIT_FAIL and summary["verdict"] == "FAIL" and summary.get("abort") is None
    assert summary["stages"][0]["slo_failures"][0].startswith("TTFT p95")
    assert summary["totals"]["stages_missing_latency_gates"] == [1]
    code, summary, _ = _run(fake, tmp_path, "--stages", "2", "--stage-seconds", "0.4", "--decode-p5-min", "1000000")
    assert code == soak.EXIT_FAIL and "per-stream decode p5" in summary["stages"][0]["slo_failures"][0]
    code, summary, _ = _run(fake, tmp_path, "--stages", "2", "--stage-seconds", "0.4",
                            "--ttft-p95-max", "30", "--decode-p5-min", "0.001")
    assert code == soak.EXIT_PASS and summary["latency_judged"] is True
    assert "NOT judged" not in soak.render_report(summary)
    code, summary, _ = _run(fake, tmp_path, "--stages", "2", "--stage-seconds", "0.4")
    assert code == soak.EXIT_PASS and summary["latency_judged"] is False
    assert "latency was NOT judged" in soak.render_report(summary).splitlines()[0]


def test_the_orchestrator_normal_lane_is_reported_per_stage_and_the_long_lane_is_not_added_in(fake, tmp_path, api_key):
    fake.adm_waiting = 3.0
    code, summary, lines = _run(fake, tmp_path, "--stages", "2", "--stage-seconds", "0.5",
                                "--admission-metrics-url", fake.base + "/orchestrator/metrics")
    assert code == soak.EXIT_PASS, summary.get("abort") or lines
    assert summary["connectivity"]["admission"]["error"] == ""
    stage = summary["stages"][0]
    lane = stage["orchestrator_normal_lane"]
    assert lane["waiting_max"] == 3.0 and 1.0 <= lane["active_max"] <= 2.0
    assert lane["queued_requests"] == stage["requests"] and lane["queued_wait_mean_s"] == 0.5
    assert lane["rejections"] == 0.0  # no rejection counter exported yet: nobody was refused
    assert "lane(normal)" in soak.render_report(summary)


def test_the_admission_lane_reads_through_prometheus_and_nothing_known_stays_null(fake):
    async def read():
        async with httpx.AsyncClient() as client:
            return await soak.AdmissionReader(client, prometheus=fake.base, selector='job="orchestrator"').read()

    fake.adm_waiting = 4.0
    values, error = asyncio.run(read())
    assert error == "" and values["adm_waiting"] == 4.0 and values["adm_active"] == 0.0
    assert values["adm_rejections_total"] == 0.0
    assert any('job="orchestrator"' in b["prom_query"] and "llm_admission_" in b["prom_query"]
               for b in fake.bodies if "prom_query" in b)
    values, error = soak.finish_admission({key: None for key, _ in soak.ADMISSION_SERIES.values()})
    assert error and all(v is None for v in values.values())


def test_an_unexpected_exception_exits_as_a_tool_crash_and_still_writes_the_summary(fake, tmp_path, api_key, monkeypatch, capsys):
    async def boom(self, window_s):
        raise RuntimeError("boom in the traffic check")

    monkeypatch.setattr(soak.Soak, "_traffic_window", boom)
    out = tmp_path / "crash"
    code = soak.main(_args(fake, out, "--stages", "2", "--stage-seconds", "0.3", "--confirm-load"))
    assert code == soak.EXIT_CRASH
    summary = json.loads((out / "summary.json").read_text())
    assert summary["verdict"] == "TOOL_CRASH" and summary["crash"] == "RuntimeError: boom in the traffic check"
    assert "TOOL_CRASH" in capsys.readouterr().out
    assert API_KEY not in (out / "summary.json").read_text()
    assert fake.posts == 0


def test_a_stage_where_every_request_was_refused_for_capacity_is_a_fail_not_a_pass(fake, tmp_path, api_key):
    fake.capacity = 0
    code, summary, _ = _run(fake, tmp_path, "--stages", "2", "--stage-seconds", "0.3")
    assert code == soak.EXIT_FAIL and summary["verdict"] == "FAIL"
    assert summary["totals"]["stages_without_success"] == [1]
    assert summary["stages"][0]["capacity_503"] > 0 and summary["stages"][0]["ok"] == 0


def _swallow_cancellation() -> None:
    task = asyncio.current_task()
    if task is not None and hasattr(task, "uncancel"):  # what anyio does when it absorbs one
        task.uncancel()


def test_a_cancellation_the_http_stack_swallows_is_sent_again_until_the_task_ends():
    # 2026-09-13: 48 of 1,500 single cancels of an httpx stream were swallowed
    # during connection setup and the request streamed on; this hung the
    # monitor-death test until the cancellation was re-sent.
    async def scenario():
        swallowed = {"n": 0}

        async def swallows_the_first_cancel():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                swallowed["n"] += 1
                _swallow_cancellation()
            await asyncio.sleep(3600)  # streams on, as the request did

        task = asyncio.create_task(swallows_the_first_cancel())
        await asyncio.sleep(0)
        resends = await asyncio.wait_for(soak.cancel_until_done([task], resend_s=0.05), 5)
        return resends, swallowed["n"], task.cancelled()

    resends, swallowed, cancelled = asyncio.run(scenario())
    assert resends >= 1 and swallowed == 1 and cancelled


def test_a_task_that_ignores_every_cancellation_is_cut_off_by_the_stuck_fallback():
    async def scenario():
        closed = asyncio.Event()
        calls = {"on_stuck": 0}

        async def ignores_cancellation_until_its_client_closes():
            # bounded, so a broken fallback fails this test instead of hanging it
            give_up = time.monotonic() + 2.0
            while not closed.is_set() and time.monotonic() < give_up:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    _swallow_cancellation()

        async def close_the_clients():
            calls["on_stuck"] += 1
            closed.set()

        task = asyncio.create_task(ignores_cancellation_until_its_client_closes())
        await asyncio.sleep(0)
        started = time.monotonic()
        await soak.cancel_until_done([task], resend_s=0.02, give_up_s=0.1, on_stuck=close_the_clients)
        return calls["on_stuck"], task.done(), time.monotonic() - started

    on_stuck, done, elapsed = asyncio.run(scenario())
    assert (on_stuck, done) == (1, True) and elapsed < 1.5
