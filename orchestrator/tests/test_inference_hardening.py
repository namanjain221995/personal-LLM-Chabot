"""The inference-path defects the developer-platform audit confirmed (2026-09-13).

docs/developer-platform/AUDIT.md, one test group per finding:

- F045  json_completion and chat_completion_with_reasoning never recorded
        token usage, so usage_events — and the per-key quotas built on it —
        under-counted every Deep Research step, compose and best-of-N call;
- N014  one user-triggerable 400 on a streamed call switched token telemetry
        off for the whole process until a restart;
- F044  a non-streaming LONG request kept the NORMAL lane closed for its whole
        generation, and the NORMAL waiters behind it never timed out;
- N013  a non-Latin prompt of ~190k real tokens was sized at a third of that
        and admitted to the NORMAL lane beside nine other prefills;
- F046  image parts were sized at zero tokens;
- F048  the client caches dropped evicted clients without closing them;
- F049  the docs still named a retired model and a window it no longer runs.

Offline, like tests/test_admission.py and tests/test_llm_resilience.py: fake
engine clients, the /tokenize client faked, the controller sample absent. Each
test fails on the code as it was before its fix.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import struct
import time
from types import SimpleNamespace

import httpx
import openai
import pytest

from app import admission, breaker, context, continuity, engine_state, llm, metrics
from app.config import settings

MAIN_URL = settings.openai_base_url
_REQ = httpx.Request("POST", "http://vllm:8000/v1/chat/completions")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    metrics.reset()
    admission.reset()
    engine_state.reset()
    continuity.reset()
    breaker.reset()
    monkeypatch.setattr(settings, "admission_long_threshold_tokens", 1000)
    monkeypatch.setattr(settings, "admission_normal_max", 2)
    monkeypatch.setattr(settings, "admission_long_max", 1)
    monkeypatch.setattr(settings, "admission_long_idle_max", 0)
    monkeypatch.setattr(settings, "admission_long_wait_s", 5.0)
    monkeypatch.setattr(settings, "admission_normal_wait_s", 5.0)
    monkeypatch.setattr(settings, "admission_max_waiting", 100)
    monkeypatch.setattr(admission, "_POLL_S", 0.02)
    monkeypatch.setitem(llm._ASK_FOR_USAGE, "enabled", True)
    monkeypatch.setitem(llm._ASK_FOR_USAGE, "disabled_at", None)
    yield
    admission.reset()
    engine_state.reset()
    continuity.reset()
    breaker.reset()
    metrics.reset()


def _counter(name: str, **labels) -> float:
    key = tuple(sorted(labels.items()))
    return metrics._counters.get(name, {}).get(key, 0.0)


def _bad_request() -> openai.BadRequestError:
    return openai.BadRequestError("bad", response=httpx.Response(400, request=_REQ), body=None)


async def _passthrough_fit(messages, **kwargs):
    return list(messages), 256


# ---------------------------------------------------------------------------
# F045 — the non-streaming paths record what the engine reported
# ---------------------------------------------------------------------------


class _Completion:
    """A non-streaming engine client: records each request, answers with the
    given usage (None: the engine reported none)."""

    def __init__(self, content: str, usage, *, refuse_guided: bool = False) -> None:
        self.calls: list = []
        self.base_url = MAIN_URL
        self._content = content
        self._usage = usage
        self._refuse_guided = refuse_guided
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._refuse_guided and "response_format" in kwargs:
            raise _bad_request()
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=self._content, reasoning_content=None),
                finish_reason="stop",
            )],
            usage=self._usage,
        )


def _use(monkeypatch, client) -> None:
    monkeypatch.setattr(llm, "_client", lambda *a, **k: client)
    monkeypatch.setattr(llm.context, "fit_request", _passthrough_fit)


def test_a_guided_json_completion_records_the_tokens_the_engine_reported(monkeypatch):
    client = _Completion('{"ok": true}', SimpleNamespace(prompt_tokens=120, completion_tokens=30))
    _use(monkeypatch, client)

    async def turn():
        # Reset INSIDE the task and read in the same task: `_usage` is a
        # ContextVar, and a read from another task would see its own copy.
        llm.reset_usage()
        try:
            out = await llm.json_completion([{"role": "user", "content": "x"}], json_schema={"type": "object"})
        finally:
            usage = llm.get_usage()
        return out, usage

    out, usage = asyncio.run(turn())
    assert out == '{"ok": true}'
    assert "response_format" in client.calls[0]
    assert usage == {"prompt_tokens": 120, "completion_tokens": 30, "calls": 1}


def test_an_unguided_json_completion_after_a_guided_refusal_records_the_call_that_answered(monkeypatch):
    client = _Completion("{}", SimpleNamespace(prompt_tokens=50, completion_tokens=5), refuse_guided=True)
    _use(monkeypatch, client)

    async def turn():
        llm.reset_usage()
        try:
            await llm.json_completion([{"role": "user", "content": "x"}], json_schema={"type": "object"})
        finally:
            usage = llm.get_usage()
        return usage

    usage = asyncio.run(turn())
    assert len(client.calls) == 2 and "response_format" not in client.calls[1]
    # The refused guided call reported nothing and is not counted.
    assert usage == {"prompt_tokens": 50, "completion_tokens": 5, "calls": 1}


def test_a_best_of_n_candidate_records_the_tokens_the_engine_reported(monkeypatch):
    client = _Completion("the answer", SimpleNamespace(prompt_tokens=900, completion_tokens=77))
    _use(monkeypatch, client)

    async def turn():
        llm.reset_usage()
        try:
            out = await llm.chat_completion_with_reasoning([{"role": "user", "content": "x"}], effort="fast")
        finally:
            usage = llm.get_usage()
        return out, usage

    (reasoning, text), usage = asyncio.run(turn())
    assert text == "the answer"
    assert usage == {"prompt_tokens": 900, "completion_tokens": 77, "calls": 1}


def test_a_response_without_usage_leaves_the_turn_not_measured_rather_than_zero(monkeypatch):
    for reported in (None, SimpleNamespace(prompt_tokens=None, completion_tokens=None)):
        _use(monkeypatch, _Completion("{}", reported))

        async def turn():
            llm.reset_usage()
            try:
                await llm.json_completion([{"role": "user", "content": "x"}])
            finally:
                usage = llm.get_usage()
            return usage

        assert asyncio.run(turn()) is None, reported

    # The same rule on the streaming path: a usage chunk with no counts in it
    # is not a call of zero tokens.
    async def empty_chunk():
        llm.reset_usage()
        llm._capture_usage(SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=None, completion_tokens=None)))
        return llm.get_usage()

    assert asyncio.run(empty_chunk()) is None


# ---------------------------------------------------------------------------
# N014 — one bad request does not switch measurement off for everyone
# ---------------------------------------------------------------------------


class _StreamClient:
    """A streaming engine client that refuses requests with a 400 by rule."""

    def __init__(self, refuse) -> None:
        self.calls: list = []
        self.base_url = MAIN_URL
        self._refuse = refuse
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._refuse(kwargs):
            raise _bad_request()

        async def chunks():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"), finish_reason="stop")],
                                  usage=None)
            yield SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=7, completion_tokens=1))

        return chunks()


def test_a_bad_request_on_a_stream_propagates_and_leaves_token_telemetry_on_for_the_next_caller():
    # A corrupt image: the engine refuses the request with AND without the option.
    bad = _StreamClient(lambda kwargs: True)
    with pytest.raises(openai.BadRequestError):
        asyncio.run(llm._open_stream(bad, {"model": "m", "messages": [], "stream": True}))
    assert llm._ASK_FOR_USAGE["enabled"] is True
    assert _counter("llm_usage_option_refused_total") == 0

    good = _StreamClient(lambda kwargs: False)
    asyncio.run(llm._open_stream(good, {"model": "m", "messages": [], "stream": True}))
    assert good.calls[0]["stream_options"] == {"include_usage": True}


def test_a_runtime_that_refuses_the_usage_option_is_asked_again_after_the_retry_interval():
    unsupporting = _StreamClient(lambda kwargs: "stream_options" in kwargs)
    asyncio.run(llm._open_stream(unsupporting, {"model": "m", "messages": [], "stream": True}))
    assert llm._ASK_FOR_USAGE["enabled"] is False
    assert _counter("llm_usage_option_refused_total") == 1

    # Within the interval the option is not asked for...
    unsupporting.calls.clear()
    asyncio.run(llm._open_stream(unsupporting, {"model": "m", "messages": [], "stream": True}))
    assert "stream_options" not in unsupporting.calls[0]

    # ...and once it has passed, it is — not "until restart".
    llm._ASK_FOR_USAGE["disabled_at"] = time.monotonic() - llm._USAGE_RETRY_S - 1
    supporting = _StreamClient(lambda kwargs: False)
    asyncio.run(llm._open_stream(supporting, {"model": "m", "messages": [], "stream": True}))
    assert supporting.calls[0]["stream_options"] == {"include_usage": True}
    assert llm._ASK_FOR_USAGE["enabled"] is True


# ---------------------------------------------------------------------------
# F044 — the LONG closure is bounded, and so is a NORMAL wait behind it
# ---------------------------------------------------------------------------


class _Call:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self):
        self.entered.set()
        await self.release.wait()
        return "answer"


async def _run(call, chars: int = 10, *, stream: bool = False):
    return await admission.run(call, messages=[{"role": "user", "content": "x" * chars}],
                               base_url=MAIN_URL, model="m", stream=stream)


def test_a_non_streaming_long_request_reopens_the_normal_lane_when_its_prefill_grace_runs_out(monkeypatch):
    monkeypatch.setattr(admission, "_CLOSURE_FLOOR_S", 0.2, raising=False)
    monkeypatch.setattr(admission, "_CLOSURE_PREFILL_TOKENS_PER_S", 1e9, raising=False)

    async def run():
        long_call = _Call()
        long_task = asyncio.create_task(_run(long_call, chars=30_000))
        await asyncio.wait_for(long_call.entered.wait(), 1.0)
        lanes = admission.lanes()
        assert lanes.normal.closed is True, "closed from admission, as before"
        newcomer = _Call()
        newcomer_task = asyncio.create_task(_run(newcomer))
        await asyncio.sleep(0.05)
        assert not newcomer.entered.is_set()
        # The long generation is still running — no first token will ever be
        # observed for a non-streaming call — but its grace has run out.
        await asyncio.wait_for(newcomer.entered.wait(), 2.0)
        assert lanes.normal.closed is False
        assert lanes.long.active == 1
        assert _counter("llm_admission_closure_expired_total") == 1
        newcomer.release.set()
        long_call.release.set()
        assert await long_task == "answer" and await newcomer_task == "answer"
        assert lanes.long.active == 0 and lanes.normal.active == 0

    asyncio.run(run())


def test_a_long_request_that_returns_inside_its_grace_reopens_normal_at_once_and_leaves_no_timer_behind():
    async def run():
        call = _Call()
        call.release.set()
        assert await _run(call, chars=30_000) == "answer"
        lanes = admission.lanes()
        assert lanes.normal.closed is False and lanes.long.active == 0
        await asyncio.sleep(0)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert pending == [], "the grace timer is cancelled on release"
        assert _counter("llm_admission_closure_expired_total") == 0

    asyncio.run(run())


def test_a_normal_waiter_behind_a_closure_that_never_lifts_is_refused_with_timeout_after_its_bound_plus_the_ceiling(monkeypatch):
    monkeypatch.setattr(settings, "admission_normal_wait_s", 0.2)
    monkeypatch.setattr(admission, "LONG_CLOSURE_MAX_S", 0.3, raising=False)

    async def run():
        await admission.lanes().normal.set_closed(True)
        started = time.monotonic()
        with pytest.raises(admission.AdmissionRejected) as refused:
            await asyncio.wait_for(_run(_Call()), 3.0)
        waited = time.monotonic() - started
        assert refused.value.reason == "timeout"
        assert 0.45 <= waited < 1.5

    asyncio.run(run())
    assert _counter("llm_admission_rejections_total", reason="timeout") == 1


def test_the_prefill_grace_covers_the_slowest_prefill_measured_and_never_exceeds_the_ceiling():
    # docs/availability/ab/B-20260912T0859Z-SUMMARY.md: the 950k needle
    # prefilled at ~1,190 tok/s — about 800 s — and 128k in about 25 s.
    assert admission.closure_grace_s(950_000) >= 800 * 1.25
    assert admission.closure_grace_s(131_072) >= 25 * 2
    assert admission.closure_grace_s(10_000_000) == admission.LONG_CLOSURE_MAX_S


# ---------------------------------------------------------------------------
# N013 — a non-Latin prompt cannot slip under the LONG threshold
# ---------------------------------------------------------------------------


def test_a_gujarati_prompt_whose_character_estimate_is_under_half_the_threshold_takes_the_long_lane(monkeypatch):
    # 1,400 Gujarati characters is ~1,400 real tokens against a threshold of
    # 1,000; at three characters a token it estimated 474 — under half.
    text = "ગ" * 1400
    assert int(len(text) / 3.0) + 1 + 7 < 500, "the shape that used to pass as NORMAL"
    asked: list = []

    async def count(base_url, model, messages):
        asked.append(True)
        return 1400, None

    monkeypatch.setattr(admission.context, "count_tokens", count)

    async def run():
        return await admission.prompt_tokens([{"role": "user", "content": text}], base_url=MAIN_URL, model="m")

    tokens = asyncio.run(run())
    assert admission.lane_for(tokens) == admission.LONG


def test_the_lanes_reuse_the_exact_count_fit_request_measured_instead_of_asking_or_estimating_again(monkeypatch):
    class _Tokenize:
        is_closed = False

        async def post(self, url, json):
            return httpx.Response(200, json={"count": 5000, "max_model_len": 1_000_000},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(context, "_tokenize_client", lambda: _Tokenize())
    monkeypatch.setattr(context, "_window_cache", {})

    async def second_count(*args, **kwargs):
        raise AssertionError("the lanes asked /tokenize for a count fit_request already had")

    async def run():
        sized, _ = await context.fit_request([{"role": "user", "content": "short"}], base_url=MAIN_URL, model="m")
        monkeypatch.setattr(admission.context, "count_tokens", second_count)
        tokens = await admission.prompt_tokens(sized, base_url=MAIN_URL, model="m")
        # A different list (a copy) is not the one that was counted.
        copied = await admission.prompt_tokens(list(sized), base_url=MAIN_URL, model="m")
        return tokens, copied

    tokens, copied = asyncio.run(run())
    assert tokens == 5000 and admission.lane_for(tokens) == admission.LONG
    assert copied < 500


def test_an_estimated_count_from_a_failed_tokenize_is_not_handed_to_the_lanes_as_exact(monkeypatch):
    class _Down:
        is_closed = False

        async def post(self, url, json):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(context, "_tokenize_client", lambda: _Down())
    monkeypatch.setattr(context, "_window_cache", {MAIN_URL: 1_000_000})

    async def run():
        sized, _ = await context.fit_request([{"role": "user", "content": "short"}], base_url=MAIN_URL, model="m")
        return context.measured_prompt_tokens(sized, MAIN_URL)

    assert asyncio.run(run()) is None


# ---------------------------------------------------------------------------
# F046 — images are sized by their pixels
# ---------------------------------------------------------------------------


def _png_part(width: int, height: int) -> dict:
    header = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
              + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00")
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(header).decode()}}


def test_an_image_is_sized_from_its_pixels_and_an_unreadable_one_at_the_processors_ceiling():
    # Measured 2026-09-09 on the main model: 1280x720 ≈ 957 tokens with the prompt.
    assert 900 <= context.estimate_image_tokens(_png_part(1280, 720)) <= 960
    assert context.estimate_image_tokens(
        {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}
    ) >= context._IMAGE_MAX_TOKENS
    huge = context.estimate_image_tokens(_png_part(20_000, 20_000))
    assert huge == context._IMAGE_MAX_TOKENS + context._IMAGE_OVERHEAD_TOKENS


def test_a_prompt_of_large_images_and_a_short_question_takes_the_long_lane():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "what is in these?"},
        _png_part(1600, 1600),
        _png_part(1600, 1600),
    ]}]
    # 2 x 2,500 patch tokens against a threshold of 1,000.
    assert context.estimate_messages(messages) > 5000

    async def run():
        return await admission.prompt_tokens(messages, base_url=MAIN_URL, model="m")

    assert admission.lane_for(asyncio.run(run())) == admission.LONG


# ---------------------------------------------------------------------------
# F048 — evicted clients are closed, least recently used first
# ---------------------------------------------------------------------------


def test_the_model_client_cache_closes_what_it_evicts_and_keeps_the_client_in_use(monkeypatch):
    closed: list = []

    class _FakeOpenAI:
        def __init__(self, base_url, **kwargs):
            self.base_url = base_url

        async def close(self):
            closed.append(self.base_url)

    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeOpenAI)
    monkeypatch.setattr(llm, "_CLIENTS", type(llm._CLIENTS)())
    monkeypatch.setattr(llm, "_CLIENT_CACHE_MAX", 3, raising=False)

    async def run():
        first = llm._client("http://a/v1")
        llm._client("http://b/v1")
        llm._client("http://c/v1")
        assert llm._client("http://a/v1") is first  # used again: now the most recent
        llm._client("http://d/v1")  # evicts b, the least recently used
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return first

    first = asyncio.run(run())
    assert closed == ["http://b/v1"]
    assert first in llm._CLIENTS.values() and len(llm._CLIENTS) == 3


def test_the_tokenize_client_cache_closes_what_it_evicts(monkeypatch):
    closed: list = []

    class _FakeAsyncClient:
        is_closed = False

        def __init__(self, timeout=None):
            pass

        async def aclose(self):
            closed.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(context, "_TOKENIZE_CLIENTS", type(context._TOKENIZE_CLIENTS)())
    monkeypatch.setattr(context, "_TOKENIZE_CACHE_MAX", 1, raising=False)

    async def run():
        monkeypatch.setattr(settings, "tokenize_timeout", 1.0)
        old = context._tokenize_client()
        monkeypatch.setattr(settings, "tokenize_timeout", 2.0)
        new = context._tokenize_client()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return old, new

    old, new = asyncio.run(run())
    assert closed == [old]
    assert [client for _, client in context._TOKENIZE_CLIENTS.values()] == [new]


# ---------------------------------------------------------------------------
# F049 — no retired model name or stale window in the docs
# ---------------------------------------------------------------------------


def test_the_inference_docs_name_no_retired_model_and_no_fixed_context_window():
    llm_source = inspect.getsource(llm)
    assert "gpt-oss-120b" not in llm_source
    assert "262k window" not in llm_source
    assert "131072" not in (context.__doc__ or "")
    assert "settings.llm_model" in (llm._openai_client.__doc__ or "")
