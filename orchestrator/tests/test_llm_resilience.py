"""app/resilience.py — surviving a model-engine outage.

The measured outage this guards against: 2026-09-10 21:31:42Z a worker
CUDA fault took the TP=2 engine down; the head answered "connection refused"
until 21:45Z. Every model call in llm.py died in 0.1 s with
openai.APIConnectionError and nothing waited for /health.

These tests are offline: the clock is patched, sleeps are recorded instead of
slept, and the engine probe is a fake. They pin the contract, not the SDK:
what is retried, what is never retried, how long is waited, and that a
stream is re-opened only before its first token.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import openai
import pytest

from app import llm, main, resilience
from app.config import settings

_REQ = httpx.Request("POST", "http://vllm:8000/v1/chat/completions")


def _conn_error() -> openai.APIConnectionError:
    exc = openai.APIConnectionError(request=_REQ)
    exc.__cause__ = httpx.ConnectError("connection refused")
    return exc


def _timeout(cause: Exception) -> openai.APITimeoutError:
    exc = openai.APITimeoutError(request=_REQ)
    exc.__cause__ = cause
    return exc


def _status(code: int) -> openai.APIStatusError:
    response = httpx.Response(code, request=_REQ)
    if code == 400:
        return openai.BadRequestError("bad", response=response, body=None)
    if code == 404:
        return openai.NotFoundError("gone", response=response, body=None)
    if code == 429:
        return openai.RateLimitError("slow down", response=response, body=None)
    return openai.InternalServerError("engine dead", response=response, body=None)


class _Clock:
    """A monotonic clock the tests advance by hand; sleeps advance it too."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(resilience.time, "monotonic", c.monotonic)
    monkeypatch.setattr(resilience.asyncio, "sleep", c.sleep)
    # Deterministic jitter: always the top of the 0.5-1.0 band.
    monkeypatch.setattr(resilience.random, "random", lambda: 1.0)
    return c


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_classification_retries_only_what_a_restart_can_fix():
    assert resilience.is_recoverable(_conn_error())
    assert resilience.is_connection_error(_conn_error())
    assert resilience.is_recoverable(_timeout(httpx.ConnectTimeout("connect")))
    assert resilience.is_recoverable(_status(500))
    assert resilience.is_recoverable(_status(429))
    # A dying stream sends {"error": ...} on a 200 — a bare APIError.
    assert resilience.is_recoverable(openai.APIError("engine dead", request=_REQ, body=None))
    # Raw httpx transport errors (a stream that died mid-body) count as well.
    assert resilience.is_recoverable(httpx.RemoteProtocolError("peer closed"))
    assert resilience.is_recoverable(httpx.ConnectError("refused"))


def test_classification_never_retries_client_errors_read_timeouts_or_cancellation():
    assert not resilience.is_recoverable(_status(400))
    assert not resilience.is_recoverable(_status(404))
    # A read timeout is a generation that ran the whole wall clock — re-running
    # it would burn the GPU for minutes again.
    assert not resilience.is_recoverable(_timeout(httpx.ReadTimeout("read")))
    assert resilience.is_read_timeout(_timeout(httpx.ReadTimeout("read")))
    assert not resilience.is_connection_error(_timeout(httpx.ReadTimeout("read")))
    assert not resilience.is_recoverable(asyncio.CancelledError())
    assert not resilience.is_recoverable(ValueError("not a model error"))


# ---------------------------------------------------------------------------
# resilient()
# ---------------------------------------------------------------------------


def test_resilient_waits_for_the_engine_then_retries_and_succeeds(clock, monkeypatch):
    calls = {"op": 0, "probe": 0}

    async def probe(base_url, timeout=None):
        calls["probe"] += 1
        # Down for the first two polls, then back.
        return calls["probe"] >= 3

    monkeypatch.setattr(resilience, "engine_answers", probe)
    monkeypatch.setattr(settings, "llm_health_poll_s", 5.0)

    async def op():
        calls["op"] += 1
        if calls["op"] == 1:
            raise _conn_error()
        return "answer"

    out = asyncio.run(resilience.resilient(op, what="t", base_url="http://vllm:8000/v1", recovery_s=600))
    assert out == "answer"
    assert calls["op"] == 2
    assert calls["probe"] == 3
    # Two health polls of 5 s, then one backoff pause (2 s * jitter 1.0).
    assert clock.slept == [5.0, 5.0, 2.0]


def test_resilient_gives_up_with_model_unavailable_after_the_window(clock, monkeypatch):
    async def down(base_url, timeout=None):
        return False

    monkeypatch.setattr(resilience, "engine_answers", down)
    monkeypatch.setattr(settings, "llm_health_poll_s", 30.0)
    attempts = {"n": 0}

    async def op():
        attempts["n"] += 1
        raise _conn_error()

    with pytest.raises(resilience.ModelUnavailable) as info:
        asyncio.run(resilience.resilient(op, what="t", base_url="http://vllm:8000/v1", recovery_s=120))
    exc = info.value
    assert exc.base_url == "http://vllm:8000/v1"
    assert exc.waited_s >= 120
    assert exc.attempts == 1
    assert isinstance(exc.last, openai.APIConnectionError)
    assert isinstance(exc.__cause__, openai.APIConnectionError)
    # The wait never overshoots the window: polls stop at the deadline.
    assert sum(clock.slept) == pytest.approx(120.0)


def test_resilient_does_not_retry_a_bad_request_or_a_read_timeout(clock, monkeypatch):
    async def never(base_url, timeout=None):  # pragma: no cover — must not be called
        raise AssertionError("no health poll for a non-recoverable error")

    monkeypatch.setattr(resilience, "engine_answers", never)
    for exc in (_status(400), _timeout(httpx.ReadTimeout("read")), ValueError("x")):
        attempts = {"n": 0}

        async def op(_exc=exc):
            attempts["n"] += 1
            raise _exc

        with pytest.raises(type(exc)):
            asyncio.run(resilience.resilient(op, what="t", base_url="http://vllm:8000/v1", recovery_s=600))
        assert attempts["n"] == 1
    assert clock.slept == []


def test_resilient_backs_off_between_engine_errors_without_polling_health(clock, monkeypatch):
    """A 5xx means the API answered: no /health wait, just bounded backoff."""

    async def never(base_url, timeout=None):  # pragma: no cover
        raise AssertionError("a 5xx does not poll /health")

    monkeypatch.setattr(resilience, "engine_answers", never)
    monkeypatch.setattr(settings, "llm_retry_base_s", 2.0)
    monkeypatch.setattr(settings, "llm_retry_cap_s", 30.0)
    attempts = {"n": 0}

    async def op():
        attempts["n"] += 1
        if attempts["n"] <= 6:
            raise _status(500)
        return "ok"

    assert asyncio.run(resilience.resilient(op, what="t", base_url="http://vllm:8000/v1", recovery_s=3600)) == "ok"
    # 2, 4, 8, 16, then capped at 30 (jitter factor 1.0).
    assert clock.slept == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_zero_window_means_one_attempt(clock, monkeypatch):
    async def never(base_url, timeout=None):  # pragma: no cover
        raise AssertionError("no polling with a zero window")

    monkeypatch.setattr(resilience, "engine_answers", never)

    async def op():
        raise _conn_error()

    with pytest.raises(resilience.ModelUnavailable):
        asyncio.run(resilience.resilient(op, what="embed_query", base_url="http://embed/v1", recovery_s=0))
    assert clock.slept == []


def test_recovery_window_context_sets_the_task_default(monkeypatch):
    monkeypatch.setattr(settings, "llm_interactive_recovery_s", 120.0)
    assert resilience.effective_recovery_s() == 120.0
    with resilience.recovery_window(1200):
        assert resilience.effective_recovery_s() == 1200.0
        with resilience.recovery_window(0):
            assert resilience.effective_recovery_s() == 0.0
        assert resilience.effective_recovery_s() == 1200.0
    assert resilience.effective_recovery_s() == 120.0


def test_wait_notifier_is_told_once_while_waiting(clock, monkeypatch):
    lines: list[str] = []
    calls = {"probe": 0}

    async def probe(base_url, timeout=None):
        calls["probe"] += 1
        return calls["probe"] >= 4

    async def notify(line: str) -> None:
        lines.append(line)

    monkeypatch.setattr(resilience, "engine_answers", probe)
    monkeypatch.setattr(settings, "llm_health_poll_s", 5.0)

    async def run():
        with resilience.wait_notifier(notify):
            return await resilience.wait_for_engine("http://vllm:8000/v1", deadline_s=600, what="t")

    assert asyncio.run(run()) is True
    assert len(lines) == 1
    assert "restarting" in lines[0] and "up to 10 min" in lines[0]

    # A chat turn is several model calls waiting on the SAME outage: one line.
    lines.clear()
    calls["probe"] = 0

    async def probe_two_calls(base_url, timeout=None):
        calls["probe"] += 1
        return calls["probe"] in (3, 6)

    monkeypatch.setattr(resilience, "engine_answers", probe_two_calls)

    async def two_waits():
        with resilience.wait_notifier(notify):
            first = await resilience.wait_for_engine("http://vllm:8000/v1", deadline_s=600, what="route")
            # The engine answered (and the flag reset), then died again before
            # the answer call: that is a NEW outage and earns a new line.
            second = await resilience.wait_for_engine("http://vllm:8000/v1", deadline_s=600, what="answer")
            return first, second

    assert asyncio.run(two_waits()) == (True, True)
    assert len(lines) == 2

    # Sibling tasks (the route classification and the answer of one turn)
    # wait on the same outage at the same time: still one line.
    lines.clear()
    calls["probe"] = 0

    async def probe_both(base_url, timeout=None):
        calls["probe"] += 1
        return calls["probe"] >= 6

    monkeypatch.setattr(resilience, "engine_answers", probe_both)

    async def concurrent():
        with resilience.wait_notifier(notify):
            return await asyncio.gather(
                resilience.wait_for_engine("http://vllm:8000/v1", deadline_s=600, what="route"),
                resilience.wait_for_engine("http://vllm:8000/v1", deadline_s=600, what="answer"),
            )

    assert asyncio.run(concurrent()) == [True, True]
    assert len(lines) == 1


def test_engine_answers_requires_health_and_models_to_be_200():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    class Patched(real):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    import app.resilience as mod

    original = httpx.AsyncClient
    httpx.AsyncClient = Patched  # type: ignore[misc]
    try:
        assert asyncio.run(mod.engine_answers("http://vllm:8000/v1")) is False
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]
    # /v1 is stripped from the base URL: the probe hits the server root.
    assert seen == ["/health", "/v1/models"]


# ---------------------------------------------------------------------------
# llm.py wiring
# ---------------------------------------------------------------------------


class _FlakyClient:
    """Refuses `failures` times with the given exception, then answers."""

    def __init__(self, exc_factory, failures: int, response) -> None:
        self.exc_factory = exc_factory
        self.failures = failures
        self.calls: list[dict] = []
        self.base_url = "http://vllm:8000/v1"
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.failures:
            raise self.exc_factory()
        return self._response

    @property
    def _response(self):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="hello", reasoning_content=None), finish_reason="stop")],
            usage=None,
        )


@pytest.fixture()
def instant_engine(monkeypatch, clock):
    async def up(base_url, timeout=None):
        return True

    monkeypatch.setattr(resilience, "engine_answers", up)
    monkeypatch.setattr(settings, "llm_interactive_recovery_s", 120.0)


async def _sized(messages, **kwargs):
    return list(messages), 64


def test_chat_completion_survives_a_connection_refusal(monkeypatch, instant_engine):
    client = _FlakyClient(_conn_error, failures=2, response=None)
    monkeypatch.setattr(llm, "_client", lambda *a, **k: client)
    monkeypatch.setattr(llm.context, "fit_request", _sized)
    out = asyncio.run(llm.chat_completion([{"role": "user", "content": "hi"}], thinking=False))
    assert out == "hello"
    assert len(client.calls) == 3


def test_open_stream_keeps_usage_telemetry_on_through_an_outage(monkeypatch, instant_engine):
    """Until 2026-09-11 the first streamed call during an outage flipped
    token telemetry off for the process: the catch-all read a connection
    error as 'the server refused stream_options'."""
    llm._ASK_FOR_USAGE["enabled"] = True
    client = _FlakyClient(_conn_error, failures=1, response=None)
    stream = asyncio.run(llm._open_stream(client, {"model": "m", "messages": [], "stream": True}))
    assert stream is not None
    assert llm._ASK_FOR_USAGE["enabled"] is True
    assert all("stream_options" in call for call in client.calls)


def test_open_stream_drops_usage_option_only_on_a_400(monkeypatch, instant_engine):
    llm._ASK_FOR_USAGE["enabled"] = True
    try:
        client = _FlakyClient(lambda: _status(400), failures=1, response=None)
        asyncio.run(llm._open_stream(client, {"model": "m", "messages": [], "stream": True}))
        assert llm._ASK_FOR_USAGE["enabled"] is False
        assert "stream_options" in client.calls[0]
        assert "stream_options" not in client.calls[1]
    finally:
        llm._ASK_FOR_USAGE["enabled"] = True


def test_json_completion_downgrades_only_on_a_400(monkeypatch, instant_engine):
    monkeypatch.setattr(llm.context, "fit_request", _sized)
    # 400 on the guided request: fall back to unconstrained, once.
    client = _FlakyClient(lambda: _status(400), failures=1, response=None)
    monkeypatch.setattr(llm, "_client", lambda *a, **k: client)
    out = asyncio.run(llm.json_completion([{"role": "user", "content": "x"}], json_schema={"type": "object"}))
    assert out == "hello"
    assert "response_format" in client.calls[0] and "response_format" not in client.calls[1]
    # A connection error on the guided request is NOT a downgrade: the same
    # guided request is retried after the engine is back.
    client = _FlakyClient(_conn_error, failures=1, response=None)
    monkeypatch.setattr(llm, "_client", lambda *a, **k: client)
    out = asyncio.run(llm.json_completion([{"role": "user", "content": "x"}], json_schema={"type": "object"}))
    assert out == "hello"
    assert all("response_format" in call for call in client.calls)


def test_chat_gives_up_after_the_interactive_window(monkeypatch, clock):
    async def down(base_url, timeout=None):
        return False

    monkeypatch.setattr(resilience, "engine_answers", down)
    monkeypatch.setattr(settings, "llm_interactive_recovery_s", 60.0)
    monkeypatch.setattr(settings, "llm_health_poll_s", 10.0)
    monkeypatch.setattr(llm.context, "fit_request", _sized)
    client = _FlakyClient(_conn_error, failures=99, response=None)
    monkeypatch.setattr(llm, "_client", lambda *a, **k: client)
    with pytest.raises(resilience.ModelUnavailable):
        asyncio.run(llm.chat_completion([{"role": "user", "content": "hi"}], thinking=False))
    assert sum(clock.slept) == pytest.approx(60.0)


def test_query_embeddings_get_one_attempt_batches_wait(monkeypatch, clock):
    polls = {"n": 0}

    async def probe(base_url, timeout=None):
        polls["n"] += 1
        return True

    monkeypatch.setattr(resilience, "engine_answers", probe)

    class Embed:
        def __init__(self, failures):
            self.failures = failures
            self.calls = 0
            self.embeddings = SimpleNamespace(create=self._create)

        async def _create(self, **kwargs):
            self.calls += 1
            if self.calls <= self.failures:
                raise _conn_error()
            return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[0.1])])

    query = Embed(failures=1)
    monkeypatch.setattr(llm, "_client", lambda *a, **k: query)
    with pytest.raises(resilience.ModelUnavailable):
        asyncio.run(llm.embed_texts(["q"], kind="query"))
    assert query.calls == 1 and polls["n"] == 0

    batch = Embed(failures=1)
    monkeypatch.setattr(llm, "_client", lambda *a, **k: batch)
    with resilience.recovery_window(600):
        vectors = asyncio.run(llm.embed_texts(["a"], kind="index"))
    assert vectors == [[0.1]] and batch.calls == 2 and polls["n"] == 1


def _embed_engine(failures: int):
    class Embed:
        def __init__(self):
            self.calls = 0
            self.embeddings = SimpleNamespace(create=self._create)

        async def _create(self, **kwargs):
            self.calls += 1
            if self.calls <= failures:
                raise _conn_error()
            return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[0.1])])

    return Embed()


def test_sidecars_fail_fast_on_an_interactive_turn(monkeypatch, clock):
    """The review finding of 2026-09-11: the interactive window is per model
    CALL, and a chat turn makes several sidecar calls before the main model
    is asked — recall's embedding, the route classifier. With the embedding
    engine down and the main model healthy, a Smart turn waited the whole
    window on the embedder (and then again on the router) before falling
    back to what it would have done in under a second — and told the person
    "the model is restarting" about an engine that was not the model.

    On an interactive turn — the chat worker binds a wait notifier to its
    task — a sidecar gets ONE attempt and its caller's fallback."""
    polls = {"n": 0}

    async def probe(base_url, timeout=None):
        polls["n"] += 1
        return True

    monkeypatch.setattr(resilience, "engine_answers", probe)
    monkeypatch.setattr(settings, "llm_interactive_recovery_s", 120.0)
    monkeypatch.setattr(llm.context, "fit_request", _sized)
    said: list[str] = []

    async def notify(text):
        said.append(text)

    async def interactive_turn():
        resilience.set_wait_notifier(notify)
        # recall.retrieve_block's call shape: the default kind, on the chat path.
        embed = _embed_engine(failures=1)
        monkeypatch.setattr(llm, "_client", lambda *a, **k: embed)
        with pytest.raises(resilience.ModelUnavailable):
            await llm.embed_texts(["what did we decide?"])
        assert embed.calls == 1
        # orchestrate.decide's call shape.
        router = _FlakyClient(_conn_error, failures=1, response=None)
        monkeypatch.setattr(llm, "_client", lambda *a, **k: router)
        with pytest.raises(resilience.ModelUnavailable):
            await llm.router_chat_completion([{"role": "user", "content": "route me"}])
        assert len(router.calls) == 1

    asyncio.run(interactive_turn())
    assert polls["n"] == 0, "no /health polling on a sidecar during a person's turn"
    assert clock.slept == [], "and no waiting"
    assert said == [], "and nobody is told the model is restarting"


def test_sidecars_still_wait_where_nobody_is_watching(monkeypatch, clock):
    """The other half of the same rule: an indexer, a title job, a video
    stage inside its recovery window — no notifier is bound, so a sidecar
    waits out the restart like any other call. This is the author's
    original intent for background work and it is kept."""
    polls = {"n": 0}

    async def probe(base_url, timeout=None):
        polls["n"] += 1
        return True

    monkeypatch.setattr(resilience, "engine_answers", probe)
    monkeypatch.setattr(settings, "llm_interactive_recovery_s", 120.0)
    monkeypatch.setattr(llm.context, "fit_request", _sized)

    async def background_job():
        assert resilience._NOTIFY.get() is None
        embed = _embed_engine(failures=1)
        monkeypatch.setattr(llm, "_client", lambda *a, **k: embed)
        assert await llm.embed_texts(["chunk"]) == [[0.1]]
        assert embed.calls == 2
        router = _FlakyClient(_conn_error, failures=1, response=None)
        monkeypatch.setattr(llm, "_client", lambda *a, **k: router)
        assert await llm.router_chat_completion([{"role": "user", "content": "title this"}]) == "hello"
        assert len(router.calls) == 2

    asyncio.run(background_job())
    assert polls["n"] == 2


def test_an_infinite_window_does_not_crash_the_wait(monkeypatch, clock):
    """`LLM_INTERACTIVE_RECOVERY_S=inf` is accepted by config; the notifier's
    "(up to N min)" used to convert it to an integer and raise OverflowError
    out of the wait — turning a patient chat into an application error."""
    async def up(base_url, timeout=None):
        return True

    monkeypatch.setattr(resilience, "engine_answers", up)
    said = []

    async def notify(text):
        said.append(text)

    async def run():
        resilience.set_wait_notifier(notify)
        calls = {"n": 0}

        async def op():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _conn_error()
            return "ok"

        return await resilience.resilient(op, what="t", base_url="http://x", recovery_s=float("inf"))

    assert asyncio.run(run()) == "ok"


def test_the_main_model_still_waits_on_an_interactive_turn(monkeypatch, instant_engine):
    """The rule is about sidecars. The main model — the answer itself, with
    nothing to stand in for it — keeps the interactive window on a person's
    turn; this is what the branch was for."""
    monkeypatch.setattr(llm.context, "fit_request", _sized)
    client = _FlakyClient(_conn_error, failures=1, response=None)
    monkeypatch.setattr(llm, "_client", lambda *a, **k: client)

    async def turn():
        resilience.set_wait_notifier(lambda text: asyncio.sleep(0))
        return await llm.chat_completion([{"role": "user", "content": "hi"}], thinking=False)

    assert asyncio.run(turn()) == "hello"
    assert len(client.calls) == 2


# ---------------------------------------------------------------------------
# What the person is told
# ---------------------------------------------------------------------------


def test_failure_sentence_classifies_sdk_errors_and_the_wrapper():
    unavailable = "MODEL_UNAVAILABLE"
    assert main._failure_sentence(_conn_error())[1] == unavailable
    assert main._failure_sentence(_status(500))[1] == unavailable
    assert main._failure_sentence(openai.APIError("dead", request=_REQ, body=None))[1] == unavailable
    assert main._failure_sentence(resilience.ModelUnavailable("http://vllm:8000/v1", 120, 3, _conn_error()))[1] == unavailable
    assert main._failure_sentence(_timeout(httpx.ReadTimeout("read")))[1] == "TIMEOUT"
    assert main._failure_sentence(_status(400))[1] == "APPLICATION_ERROR"
    assert main._failure_sentence(ValueError("x"))[1] == "APPLICATION_ERROR"
    # Never the exception's own text on the wire.
    sentence, _ = main._failure_sentence(_conn_error())
    assert "connection refused" not in sentence.lower()
