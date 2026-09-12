"""llm.py in strict one-model mode (availability CONTRACT v2 §1, §8.2–8.3).

Two fake OpenAI-compatible engines — the main model and the router — a
hand-driven clock shared by the breaker and the resilient wrapper, and no
network. What is pinned:

- during an outage NO call is routed to the router for an answer: a plain
  chat turn, chat_completion, chat_completion_with_reasoning,
  json_completion, stream_chat_completion and chat_with_tools all wait for
  the main model, told once with the exact sentence, and the router's call
  log stays empty;
- the fallback path is gone — the module, its settings, its metrics, its
  status sentence;
- the wait ends on the controller's READY (the event), never on a /health
  poll of the dead port, and the /health poll survives only for an
  unreachable controller;
- HALF_OPEN admits one canary; its success closes; a failed one re-opens;
- a read timeout never opens; a 4xx is never retried and never opens;
- an unknown verdict never opens; RECOVERING opens at once and READY
  releases.
"""
from __future__ import annotations

import asyncio
import importlib
import time
from types import SimpleNamespace

import httpx
import openai
import pytest

from app import breaker, continuity, engine_state, llm, metrics, resilience
from app.config import settings

MAIN_URL = "http://vllm:8000/v1"
ROUTER_URL = "http://vllm-router:30002/v1"
MAIN_MODEL = "Qwen/Qwen3.6-35B-A3B-NVFP4"
ROUTER_MODEL = "Qwen/Qwen3-VL-8B-Instruct-FP8"

_REQ = httpx.Request("POST", f"{MAIN_URL}/chat/completions")


def _conn_error() -> openai.APIConnectionError:
    exc = openai.APIConnectionError(request=_REQ)
    exc.__cause__ = httpx.ConnectError("connection refused")
    return exc


def _read_timeout() -> openai.APITimeoutError:
    exc = openai.APITimeoutError(request=_REQ)
    exc.__cause__ = httpx.ReadTimeout("read")
    return exc


def _status(code: int, text: str = "engine dead") -> openai.APIStatusError:
    response = httpx.Response(code, request=_REQ)
    if code == 400:
        return openai.BadRequestError("bad request", response=response, body=None)
    if code == 429:
        return openai.RateLimitError("slow down", response=response, body=None)
    if code == 503:
        return openai.APIStatusError("not ready", response=response, body=None)
    return openai.InternalServerError(text, response=response, body=None)


class _Stream:
    def __init__(self, text: str) -> None:
        self._chunks = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text[: len(text) // 2]), finish_reason=None)], usage=None),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text[len(text) // 2:]), finish_reason="stop")], usage=None),
            SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2)),
        ]
        self.closed = False

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for chunk in self._chunks:
            yield chunk

    async def close(self) -> None:
        self.closed = True


class _Engine:
    """One fake engine at one base URL: fails `failures` times with
    `exc_factory()`, then answers `text` (streamed when asked)."""

    def __init__(self, base_url: str, *, exc_factory=None, failures: int = 0, text: str = "hello") -> None:
        self.base_url = base_url
        self.exc_factory = exc_factory
        self.failures = failures
        self.text = text
        self.calls: list = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.failures:
            raise self.exc_factory()
        if kwargs.get("stream"):
            return _Stream(self.text)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=self.text, reasoning_content=None, reasoning=None, tool_calls=None, model_extra=None),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        )


class _Clock:
    """One clock for the breaker, the wrapper and the state poller; fake
    sleeps advance it so cooldowns and windows elapse without waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list = []
        #: Run on every fake sleep: how a test flips the verdict mid-wait.
        self.on_sleep = None

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.now)


class _World:
    def __init__(self, clock: _Clock, main: _Engine, router: _Engine, sized: list, said: list) -> None:
        self.clock = clock
        self.main = main
        self.router = router
        self.sized = sized
        self.said = said

    @property
    def breaker(self) -> breaker.Breaker:
        return breaker.get("main")

    def open_main(self) -> None:
        for _ in range(3):
            self.breaker.record_failure("connection", permit=self.breaker.acquire())
        assert self.breaker.state == breaker.OPEN

    async def notify(self, line: str) -> None:
        self.said.append(line)


def _poller(clock: _Clock, bad: str, ready_after_s: float):
    """What the real poller does every few seconds: keep the verdict fresh
    — `bad` until `ready_after_s` of fake time have passed, READY after."""

    def on_sleep(now: float) -> None:
        state = "READY" if now - 1000.0 >= ready_after_s else bad
        if engine_state.snapshot() is None or engine_state.snapshot().state != state or engine_state.unknown():
            _verdict(state, clock)

    return on_sleep


def _verdict(state: str, clock: _Clock, **extra) -> None:
    """Plant a controller verdict the way the poller records one."""
    doc = {"schema": 1, "state": state, "state_code": engine_state.STATE_CODES[state],
           "reason": "test", "generated_at": time.time(), "primary_ready": state in engine_state.SERVING,
           "signals": {"head_container": {"running": True, "started_at": time.time() - 20.0,
                                          "engine_process_alive": True}}}
    doc.update(extra)
    engine_state._record(engine_state.parse_state_document(doc, observed_at=clock.now), "")


@pytest.fixture()
def world(monkeypatch):
    metrics.reset()
    breaker.reset()
    engine_state.reset()
    continuity.reset()
    clock = _Clock()
    monkeypatch.setattr(resilience.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(resilience.asyncio, "sleep", clock.sleep)
    monkeypatch.setattr(resilience.random, "random", lambda: 1.0)
    monkeypatch.setattr(settings, "openai_base_url", MAIN_URL)
    monkeypatch.setattr(settings, "llm_model", MAIN_MODEL)
    monkeypatch.setattr(settings, "router_base_url", ROUTER_URL)
    monkeypatch.setattr(settings, "router_model", ROUTER_MODEL)
    monkeypatch.setattr(settings, "llm_breaker_failures", 3)
    monkeypatch.setattr(settings, "llm_breaker_window_s", 30.0)
    monkeypatch.setattr(settings, "llm_breaker_cooldown_s", 10.0)
    monkeypatch.setattr(settings, "llm_interactive_recovery_s", 0.0)
    monkeypatch.setattr(settings, "llm_queue_max_wait_s", 0.0)
    monkeypatch.setattr(settings, "llm_health_poll_s", 5.0)
    breaker.install("main", breaker.Breaker("main", clock=clock.monotonic, external_open=engine_state.external_open))

    async def up(base_url, timeout=None):
        return True

    monkeypatch.setattr(resilience, "engine_answers", up)
    main = _Engine(MAIN_URL, text="from the main model")
    router = _Engine(ROUTER_URL, text="from the router")

    def client(base_url, api_key=None, **kwargs):
        if base_url.rstrip("/") == MAIN_URL:
            return main
        if base_url.rstrip("/") == ROUTER_URL:
            return router
        raise AssertionError(f"no fake engine at {base_url}")

    monkeypatch.setattr(llm, "_client", client)
    sized: list = []

    async def fit(messages, *, base_url, model, requested_max_tokens=None):
        sized.append(base_url)
        return list(messages), requested_max_tokens or 64

    monkeypatch.setattr(llm.context, "fit_request", fit)
    said: list = []
    w = _World(clock, main, router, sized, said)
    yield w
    breaker.reset()
    engine_state.reset()
    continuity.reset()
    metrics.reset()


_HISTORY = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]


def _turn(world: _World, coro_factory, *, queue_s: float = 0.0):
    """Run one chat turn the way the chat worker sets one up: the notifier
    and the continuity hold bound to the task (a generation without a row,
    so the store is not touched)."""

    async def run():
        settings.llm_queue_max_wait_s = queue_s
        resilience.set_wait_notifier(world.notify)
        gen = SimpleNamespace(intent_id=None, generation_id="g", attempt=1, retry_reason="none",
                              request_status="running", parked=False)
        continuity.bind(gen, world.notify)
        try:
            return await coro_factory()
        finally:
            settings.llm_queue_max_wait_s = 0.0

    return asyncio.run(run())


def _counter(name: str, **labels) -> float:
    key = tuple(sorted(labels.items()))
    return metrics._counters.get(name, {}).get(key, 0.0)


async def _stream_turn(**kwargs):
    return [e async for e in llm.stream_chat_events(_HISTORY, model_choice="smart", effort="fast", **kwargs)]


# ---------------------------------------------------------------------------
# No router answer, ever
# ---------------------------------------------------------------------------


def test_the_fallback_path_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.fallback")
    for name in ("fallback_enabled", "fallback_base_url", "fallback_model", "fallback_max_input_tokens",
                 "fallback_max_output_tokens", "fallback_capabilities"):
        assert not hasattr(settings, name), name
    for name in ("_via_engine", "_fallback_completion", "_fallback_stream", "_fallback_request", "_fallback_raw"):
        assert not hasattr(llm, name), name
    assert not hasattr(breaker, "FALLBACK")
    assert "fallback" not in metrics._ALLOWED["engine"]
    assert "Qwen3-VL-8B" not in continuity.QUEUED_LINE and "Qwen3-VL-8B" not in continuity.EXPIRED_LINE
    assert "fallback" not in continuity.QUEUED_LINE.lower()


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: _stream_turn(), id="chat"),
        pytest.param(lambda: llm.chat_completion(_HISTORY, thinking=False), id="chat_completion"),
        pytest.param(lambda: llm.chat_completion_with_reasoning(_HISTORY, effort="fast"), id="chat_completion_with_reasoning"),
        pytest.param(lambda: llm.json_completion(_HISTORY, json_schema={"type": "object"}), id="json_completion"),
        pytest.param(lambda: _collect(llm.stream_chat_completion(_HISTORY, thinking=False)), id="stream_chat_completion"),
        pytest.param(lambda: llm.chat_with_tools(_HISTORY, tools=[{"type": "function", "function": {"name": "f"}}]), id="chat_with_tools"),
    ],
)
def test_during_an_outage_no_call_reaches_the_router_and_ready_resumes_on_the_main_model(world, call):
    """The controller says RECOVERING: every entry point waits for the main
    model — told once, the exact sentence — and the router sees nothing.
    When the verdict turns READY (the event wakes the gate, the cooldown
    runs), the main model answers."""
    _verdict("RECOVERING", world.clock)
    assert world.breaker.state == breaker.OPEN
    world.clock.on_sleep = _poller(world.clock, "RECOVERING", ready_after_s=30.0)
    out = _turn(world, call, queue_s=900.0)
    text = "".join(d for _, d in out) if isinstance(out, list) and out and isinstance(out[0], tuple) else out
    if isinstance(text, tuple):
        text = text[1] if isinstance(text[1], str) else text[0]
    assert "from the main model" in str(text)
    assert world.router.calls == [], "the router never receives a user-answer request"
    assert len(world.main.calls) == 1
    assert world.said == [continuity.QUEUED_LINE]
    assert world.breaker.state == breaker.CLOSED
    assert _counter("llm_resumed_generations_total", outcome="resumed") == 1
    assert world.sized == [MAIN_URL] or world.sized == [], "nothing is ever sized against the router"
    # Held through the whole RECOVERING phase (30 s) and the cooldown (10 s,
    # started at the tick that saw READY).
    assert 38.0 <= sum(world.clock.slept) <= 42.0


async def _collect(agen):
    return "".join([d async for d in agen])


def test_the_fast_picker_choice_is_the_main_model_without_thinking(world):
    events = _turn(world, _fast)
    assert "".join(d for _, d in events) == "from the main model"
    (call,) = world.main.calls
    assert call["model"] == MAIN_MODEL
    assert call["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert world.router.calls == []


async def _fast():
    return [e async for e in llm.stream_chat_events(_HISTORY, model_choice="fast", effort="fast")]


def test_the_router_is_still_a_classifier(world):
    """router_chat_completion is internal: it goes to the router with no
    breaker and no queue, one attempt on a person's turn."""
    out = _turn(world, lambda: llm.router_chat_completion([{"role": "user", "content": "route me"}]))
    assert out == "from the router"
    assert len(world.router.calls) == 1 and world.main.calls == []
    assert world.said == []
    world.router.exc_factory = _conn_error
    world.router.failures = 99
    with pytest.raises(resilience.ModelUnavailable):
        _turn(world, lambda: llm.router_chat_completion([{"role": "user", "content": "route me"}]))
    assert len(world.router.calls) == 2 and world.said == []


# ---------------------------------------------------------------------------
# The wait is the controller's, not the port's
# ---------------------------------------------------------------------------


def test_a_queued_caller_never_polls_health_and_wakes_on_ready(world, monkeypatch):
    polls = {"n": 0}

    async def probe(base_url, timeout=None):
        polls["n"] += 1
        return True

    monkeypatch.setattr(resilience, "engine_answers", probe)
    _verdict("WEDGED", world.clock)
    world.clock.on_sleep = _poller(world.clock, "WEDGED", ready_after_s=120.0)
    text, calls = _turn(world, lambda: llm.chat_with_tools(_HISTORY, tools=[]), queue_s=900.0)
    assert text == "from the main model" and calls == []
    assert polls["n"] == 0, "the dead port is never asked"
    assert len(world.main.calls) == 1, "one call: the canary that was this very request"
    # Held for the whole WEDGED phase, then the cooldown, then admitted.
    assert 120.0 <= sum(world.clock.slept) <= 131.0
    assert world.said == [continuity.QUEUED_LINE]


def test_a_connection_refusal_with_a_fresh_verdict_waits_on_the_controller_not_health(world, monkeypatch):
    """The breaker is CLOSED (nothing has failed yet) but the controller
    says RECOVERING: the first refused connection sends the caller to the
    READY event, not to a /health loop."""
    polls = {"n": 0}

    async def probe(base_url, timeout=None):
        polls["n"] += 1
        return False

    monkeypatch.setattr(resilience, "engine_answers", probe)
    # A fresh RECOVERING verdict AFTER the breaker was built CLOSED: the
    # external open takes it at the first refresh, so the call is queued.
    _verdict("RECOVERING", world.clock)
    world.clock.on_sleep = _poller(world.clock, "RECOVERING", ready_after_s=60.0)
    out = _turn(world, lambda: llm.chat_completion(_HISTORY, thinking=False), queue_s=900.0)
    assert out == "from the main model"
    assert polls["n"] == 0
    assert 60.0 <= sum(world.clock.slept) <= 72.0


def test_an_unreachable_controller_falls_back_to_polling_health(world, monkeypatch):
    """No verdict (the controller is down): a refused connection polls
    /health exactly as before — the documented degraded mode — and the
    person still reads the exact sentence, once."""
    polls = {"n": 0}

    async def probe(base_url, timeout=None):
        polls["n"] += 1
        return polls["n"] >= 3

    monkeypatch.setattr(resilience, "engine_answers", probe)
    assert engine_state.unknown()
    world.main.exc_factory = _conn_error
    world.main.failures = 1
    out = _turn(world, lambda: llm.chat_completion(_HISTORY, thinking=False), queue_s=900.0)
    assert out == "from the main model"
    assert polls["n"] == 3 and len(world.main.calls) == 2
    assert world.said == [continuity.QUEUED_LINE]
    assert _counter("llm_resumed_generations_total", outcome="resumed") == 1


def test_the_queue_window_parks_a_turn_that_outran_it(world):
    _verdict("DOWN", world.clock)
    world.clock.on_sleep = _poller(world.clock, "DOWN", ready_after_s=1e9)
    with pytest.raises(continuity.QueuedForRecovery) as info:
        _turn(world, lambda: llm.chat_completion(_HISTORY, thinking=False), queue_s=60.0)
    assert info.value.waited_s >= 60.0
    assert world.main.calls == [] and world.router.calls == []
    assert world.said == [continuity.QUEUED_LINE, continuity.EXPIRED_LINE]
    assert _counter("llm_resumed_generations_total", outcome="expired") == 1
    assert _counter("llm_engine_unavailable_total", what="chat_completion", reason="breaker_open") == 1


def test_without_a_hold_the_window_closes_with_model_unavailable(world):
    """A background job (no chat turn, no hold): the OPEN breaker holds it
    for its declared window and then ModelUnavailable — what video and
    artifact jobs turn into 'deferred'."""
    _verdict("RECOVERING", world.clock)
    world.clock.on_sleep = _poller(world.clock, "RECOVERING", ready_after_s=1e9)

    async def job():
        with resilience.recovery_window(30.0):
            return await llm.chat_completion(_HISTORY, thinking=False)

    with pytest.raises(resilience.ModelUnavailable) as info:
        asyncio.run(job())
    assert info.value.waited_s >= 30.0 and world.main.calls == []
    assert _counter("llm_resumed_generations_total", outcome="expired") == 0


# ---------------------------------------------------------------------------
# HALF_OPEN: one canary; success closes; failure re-opens
# ---------------------------------------------------------------------------


def test_half_open_admits_one_canary_and_its_success_closes(world):
    world.open_main()
    world.clock.now += 10.0
    assert world.breaker.state == breaker.HALF_OPEN
    out = _turn(world, lambda: llm.chat_completion(_HISTORY, thinking=False))
    assert out == "from the main model"
    assert len(world.main.calls) == 1
    assert world.breaker.state == breaker.CLOSED
    assert _counter("llm_breaker_transitions_total", engine="main", to="CLOSED") == 1


def test_a_failed_canary_reopens_and_the_next_caller_queues(world):
    world.open_main()
    world.clock.now += 10.0
    world.main.exc_factory = _conn_error
    world.main.failures = 99
    with pytest.raises(continuity.QueuedForRecovery):
        _turn(world, lambda: llm.chat_completion(_HISTORY, thinking=False), queue_s=5.0)
    assert len(world.main.calls) == 1
    assert world.breaker.state == breaker.OPEN
    assert world.breaker.describe()["last_reason"] == "canary_connection"
    assert world.router.calls == []


def test_queued_callers_are_admitted_when_the_canary_closes_the_breaker(world, monkeypatch):
    world.open_main()
    polls = {"n": 0}

    async def never(base_url, timeout=None):
        polls["n"] += 1
        return True

    monkeypatch.setattr(resilience, "engine_answers", never)
    text, calls = _turn(world, lambda: llm.chat_with_tools(_HISTORY, tools=[]), queue_s=120.0)
    assert text == "from the main model" and calls == []
    assert sum(world.clock.slept) == pytest.approx(10.0), "the cooldown, then it WAS the canary"
    assert polls["n"] == 0
    assert world.breaker.state == breaker.CLOSED
    assert world.said == [continuity.QUEUED_LINE]


# ---------------------------------------------------------------------------
# What never opens, what never retries
# ---------------------------------------------------------------------------


def test_read_timeouts_are_never_retried_and_never_open(world):
    world.main.exc_factory = _read_timeout
    world.main.failures = 99
    for _ in range(5):
        with pytest.raises(openai.APITimeoutError):
            _turn(world, lambda: llm.chat_completion(_HISTORY, thinking=False))
    assert len(world.main.calls) == 5
    assert world.breaker.state == breaker.CLOSED
    assert _counter("llm_breaker_failures_total", engine="main", reason="request_timeout") == 5
    assert world.router.calls == []


def test_a_4xx_is_never_retried_and_never_opens(world):
    world.main.exc_factory = lambda: _status(400)
    world.main.failures = 99
    for _ in range(5):
        with pytest.raises(openai.BadRequestError):
            _turn(world, lambda: llm.chat_completion(_HISTORY, thinking=False))
    assert len(world.main.calls) == 5
    assert world.breaker.state == breaker.CLOSED
    assert _counter("llm_breaker_failures_total", engine="main", reason="malformed") == 5
    assert world.clock.slept == []


def test_three_engine_deaths_open_the_breaker_and_the_next_turn_queues_instead(world):
    world.main.exc_factory = lambda: _status(500, "EngineDeadError: engine core died")
    world.main.failures = 99
    for i in range(3):
        with pytest.raises(resilience.ModelUnavailable):
            _turn(world, lambda: llm.chat_completion(_HISTORY, thinking=False))
        assert len(world.main.calls) == i + 1
    assert world.breaker.state == breaker.OPEN
    assert _counter("llm_breaker_failures_total", engine="main", reason="engine_dead") == 3
    with pytest.raises(continuity.QueuedForRecovery):
        _turn(world, lambda: llm.chat_completion(_HISTORY, thinking=False), queue_s=5.0)
    assert len(world.main.calls) == 3, "the dead engine is not asked while OPEN"
    assert world.router.calls == []


def test_failure_reasons_follow_the_contract(world):
    assert resilience.failure_reason(_conn_error()) == "connection"
    assert resilience.failure_reason(_read_timeout()) == "request_timeout"
    assert resilience.failure_reason(_status(429)) == "capacity"
    assert resilience.failure_reason(_status(503)) == "readiness"
    assert resilience.failure_reason(_status(500, "RuntimeError: Triton Error [CUDA]: misaligned address in worker")) == "worker_lost"
    assert resilience.failure_reason(_status(500, "Internal Server Error")) == "engine_dead"
    assert resilience.failure_reason(_status(400)) == "malformed"
    assert resilience.failure_reason(openai.APIError("EngineDeadError", request=_REQ, body=None)) == "engine_dead"
    assert resilience.failure_reason(asyncio.CancelledError()) == "cancelled"
    assert resilience.failure_reason(ValueError("ours")) is None
    pool = openai.APITimeoutError(request=_REQ)
    pool.__cause__ = httpx.PoolTimeout("pool")
    assert resilience.failure_reason(pool) == "queue_timeout"


# ---------------------------------------------------------------------------
# The controller's verdict
# ---------------------------------------------------------------------------


def test_unknown_engine_state_never_opens(world, monkeypatch):
    monkeypatch.setattr(engine_state.time, "monotonic", world.clock.monotonic)
    engine_state.reset()
    assert engine_state.unknown()
    out = _turn(world, lambda: llm.chat_completion(_HISTORY, thinking=False))
    assert out == "from the main model" and world.breaker.state == breaker.CLOSED
    # A verdict that went stale is unknown too — the breaker stays put.
    _verdict("DOWN", world.clock)
    world.clock.now += 60.0
    out = _turn(world, lambda: llm.chat_completion(_HISTORY, thinking=False))
    assert out == "from the main model" and world.breaker.state == breaker.CLOSED
    # And MONITORING_UNKNOWN is a verdict of "cannot see", never "down".
    _verdict("MONITORING_UNKNOWN", world.clock)
    out = _turn(world, lambda: llm.chat_completion(_HISTORY, thinking=False))
    assert out == "from the main model" and world.breaker.state == breaker.CLOSED


def test_controller_recovering_opens_immediately_and_ready_releases(world, monkeypatch):
    monkeypatch.setattr(engine_state.time, "monotonic", world.clock.monotonic)
    _verdict("RECOVERING", world.clock)
    world.clock.on_sleep = _poller(world.clock, "RECOVERING", ready_after_s=1e9)
    with pytest.raises(continuity.QueuedForRecovery):
        _turn(world, lambda: llm.chat_completion(_HISTORY, thinking=False), queue_s=5.0)
    world.clock.on_sleep = None
    assert world.main.calls == []
    assert world.breaker.state == breaker.OPEN
    assert world.breaker.describe()["held_open_by"] == "RECOVERING"
    assert world.said == [continuity.QUEUED_LINE, continuity.EXPIRED_LINE]
    # Held for the whole reload: the cooldown does not run.
    world.clock.now += 300.0
    _verdict("RECOVERING", world.clock)
    assert world.breaker.state == breaker.OPEN
    # READY: the cooldown runs, the canary goes through, the breaker closes.
    _verdict("READY", world.clock)
    world.clock.now += 10.0
    out = _turn(world, lambda: llm.chat_completion(_HISTORY, thinking=False))
    assert out == "from the main model"
    assert world.breaker.state == breaker.CLOSED


def test_a_controller_restart_that_says_starting_does_not_queue_anyone(world, monkeypatch):
    """STARTING with a head that has run for hours is the controller's own
    cold start (review finding engine_state.py:63): no queue, no line."""
    monkeypatch.setattr(engine_state.time, "monotonic", world.clock.monotonic)
    _verdict("STARTING", world.clock, primary_ready=False,
             signals={"head_container": {"running": True, "started_at": time.time() - 7200.0, "engine_process_alive": True}})
    out = _turn(world, lambda: llm.chat_completion(_HISTORY, thinking=False))
    assert out == "from the main model" and world.said == []
    assert world.breaker.state == breaker.CLOSED


# ---------------------------------------------------------------------------
# Round-2 review findings (docs/availability/REVIEW-FINDINGS-round2.md)
# ---------------------------------------------------------------------------


def test_sizing_never_asks_tokenize_of_an_engine_the_breaker_refuses(world, monkeypatch):
    """llm.py:406 (CONTRACT §8.2 'while OPEN, no caller polls the dead
    port'): the prompt sizing (a /tokenize round trip) waits at the gate
    like the send does, so a queued turn touches the port only once the
    breaker admits it — and the person is told through the same hold."""
    _verdict("RECOVERING", world.clock)
    world.clock.on_sleep = _poller(world.clock, "RECOVERING", ready_after_s=20.0)
    sized_at: list = []
    passthrough = llm.context.fit_request  # the world's stub

    async def fit(messages, **kwargs):
        sized_at.append(world.clock.now)
        return await passthrough(messages, **kwargs)

    monkeypatch.setattr(llm.context, "fit_request", fit)
    out = _turn(world, lambda: llm.chat_completion(_HISTORY, thinking=False), queue_s=900.0)
    assert out == "from the main model"
    assert sized_at and all(t >= 1000.0 + 20.0 for t in sized_at), sized_at
    assert world.said == [continuity.QUEUED_LINE]
    assert _counter("llm_resumed_generations_total", outcome="resumed") == 1


def test_a_sidecar_on_the_main_url_fails_fast_without_the_hold(world, monkeypatch):
    """resilience.py:549: a sidecar call on the MAIN URL (router configured
    onto the main endpoint) has no window: it never enters the turn's
    hold, never speaks, never parks — and never touches /tokenize either."""
    monkeypatch.setattr(settings, "router_base_url", MAIN_URL)
    _verdict("RECOVERING", world.clock)
    world.clock.on_sleep = _poller(world.clock, "RECOVERING", ready_after_s=1e9)
    with pytest.raises(resilience.ModelUnavailable):
        _turn(world, lambda: llm.router_chat_completion(_HISTORY), queue_s=900.0)
    assert world.sized == [] and world.main.calls == []
    assert world.said == []
    assert _counter("llm_resumed_generations_total", outcome="expired") == 0
    assert _counter("llm_engine_unavailable_total", what="router_chat_completion", reason="breaker_open") == 1


def test_wait_admitted_queues_on_the_hold_and_parks_at_the_window(world):
    """resilience.wait_admitted (the Deep Research up-front park): the gate
    without a call — queued on the hold, resumed on READY as attempt 2, or
    parked with the second sentence when the window closes."""
    _verdict("RECOVERING", world.clock)
    world.clock.on_sleep = _poller(world.clock, "RECOVERING", ready_after_s=20.0)
    gens: list = []

    async def wait_then_report():
        await resilience.wait_admitted(what="deep_research", base_url=MAIN_URL)
        gens.append(continuity.current().gen)

    _turn(world, wait_then_report, queue_s=900.0)
    assert world.said == [continuity.QUEUED_LINE]
    assert gens[0].attempt == 2 and gens[0].retry_reason == "recovery"
    assert world.main.calls == []
    world.said.clear()
    _verdict("DOWN", world.clock)
    world.clock.on_sleep = _poller(world.clock, "DOWN", ready_after_s=1e9)
    with pytest.raises(continuity.QueuedForRecovery):
        _turn(world, lambda: resilience.wait_admitted(what="deep_research", base_url=MAIN_URL), queue_s=60.0)
    assert world.said == [continuity.QUEUED_LINE, continuity.EXPIRED_LINE]
    assert _counter("llm_resumed_generations_total", outcome="expired") == 1


def test_best_of_candidates_carry_the_park_out_once(world, monkeypatch):
    """continuity.py:187 (both lenses): three candidates queue on the one
    hold; the window parks the turn ONCE — one EXPIRED_LINE, one expired
    count — and generate_candidates raises QueuedForRecovery instead of
    returning error candidates that fall through to a second wait."""
    from app.core import best_of

    monkeypatch.setattr(settings, "extra_high_samples", 3)
    _verdict("DOWN", world.clock)
    world.clock.on_sleep = _poller(world.clock, "DOWN", ready_after_s=1e9)
    with pytest.raises(continuity.QueuedForRecovery):
        _turn(world, lambda: best_of.generate_candidates(_HISTORY, n=3, temperature=0.3, max_tokens=64),
              queue_s=60.0)
    assert world.said == [continuity.QUEUED_LINE, continuity.EXPIRED_LINE]
    assert _counter("llm_resumed_generations_total", outcome="expired") == 1
    assert world.main.calls == []


def test_a_parked_turns_next_call_raises_at_once_instead_of_waiting_a_second_window(world):
    """continuity.py:187: after the park, a further main-model call of the
    same turn (the single-stream fallthrough after best-of-N) raises
    QueuedForRecovery immediately: no second window, no repeated lines."""
    _verdict("DOWN", world.clock)
    world.clock.on_sleep = _poller(world.clock, "DOWN", ready_after_s=1e9)

    async def two_calls():
        try:
            await llm.chat_completion(_HISTORY, thinking=False)
        except continuity.QueuedForRecovery:
            pass  # an "upgrade, never a gate" handler swallowed it
        before = world.clock.now
        with pytest.raises(continuity.QueuedForRecovery):
            await llm.chat_completion(_HISTORY, thinking=False)
        assert world.clock.now == before, "no second wait"

    _turn(world, two_calls, queue_s=60.0)
    assert world.said == [continuity.QUEUED_LINE, continuity.EXPIRED_LINE]
    assert _counter("llm_resumed_generations_total", outcome="expired") == 1
