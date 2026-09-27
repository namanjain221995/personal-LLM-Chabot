"""The engine call starts the instant the prompt is final.

THE DEFECT. Between a chat turn entering `run_chat_engine` and its body
reaching the engine there were three `llm._fit` calls and four
`context.count_tokens` calls — 56.3 ms of work on the harness's plain shape
and 103.6 ms on its docs shape, 28.0 ms and 41.5 ms of it on the critical
path. Every one is a /tokenize round trip that had to COMPLETE before a byte
went on the wire, in front of an engine whose own first token costs 49.8 ms.

THE FIX. `context.fit_request` still counts, once, per call — but when a
bound the prompt cannot game already proves the request fits and already
pins `max_tokens` to the caller's ceiling, the count runs BESIDE the send
instead of in front of it, and `llm._settling` writes it back the moment the
request has been dispatched.

WHAT THESE TESTS ARE FOR. The previous attempt at this was refused because
it lost the invariant that the window must be one a real /tokenize reported.
So the gate is tested condition by condition, and every one of them is a
test that the SLOW path still runs:

  * provenance  — a window resolved from configuration never fires it;
  * the bound   — `upper_bound_messages`, never `estimate_messages`;
  * the shapes  — tool-call arguments, dict content, image parts and tool
                  turns are outside what the bound covers;
  * the ceiling — a bounded classifier call asking for 4 tokens is not proof
                  that the trim loop breaks at 256;
  * continuations, which are never sized here at all;
  * the window shrinking under a request that has already been sent.

Offline, like tests/test_inference_hardening.py: a faked /tokenize client, a
fake engine client, no network and no GPU.
"""
from __future__ import annotations

import asyncio
import base64
import struct
from types import SimpleNamespace

import httpx
import pytest

from app import admission, breaker, context, engine_state, llm, metrics
from app.config import settings

MAIN_URL = settings.openai_base_url
ROUTER_URL = settings.router_base_url


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    metrics.reset()
    admission.reset()
    engine_state.reset()
    breaker.reset()
    # A window and a threshold big enough that an ordinary turn is NORMAL and
    # far inside the window, exactly as the live container is configured
    # (MAIN_MODEL_MAX_LEN=1000000, CONTEXT_SAFETY_MARGIN=512).
    monkeypatch.setattr(settings, "context_safety_margin", 512)
    monkeypatch.setattr(settings, "admission_long_threshold_tokens", 131072)
    monkeypatch.setattr(settings, "admission_normal_max", 4)
    monkeypatch.setattr(settings, "admission_normal_wait_s", 5.0)
    monkeypatch.setattr(admission, "_POLL_S", 0.02)
    monkeypatch.setattr(context, "_window_cache", {})
    # `getattr` guards, not laziness: the two names below do not exist on the
    # revision before this one, and the first test in this file has to reach
    # its ASSERTION there — "the request waited for its /tokenize" — rather
    # than die in setup, or "it fails before and passes after" means nothing.
    for name in ("_window_from_server", "_INFLIGHT_COUNTS"):
        if hasattr(context, name):
            monkeypatch.setattr(context, name, set())
    yield
    admission.reset()
    engine_state.reset()
    breaker.reset()
    metrics.reset()


def _counter(name: str, **labels) -> float:
    key = tuple(sorted(labels.items()))
    return metrics._counters.get(name, {}).get(key, 0.0)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Tokenize:
    """A /tokenize endpoint. `gate`, when set, holds every answer until it is
    released, which is how "the request went first" is observed."""

    is_closed = False

    def __init__(self, count: int = 40, window: int = 1_000_000, gate=None,
                 fail: bool = False, windows=None) -> None:
        self.count = count
        self.window = window
        self.gate = gate
        self.fail = fail
        self.windows = list(windows) if windows else None
        self.calls: list = []

    async def post(self, url, json):  # noqa: A002 - httpx's own parameter name
        self.calls.append(json)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise httpx.ConnectError("refused")
        window = self.window
        if self.windows:
            window = self.windows.pop(0)
        return httpx.Response(
            200,
            json={"count": self.count, "max_model_len": window},
            request=httpx.Request("POST", url),
        )


class _Engine:
    """A non-streaming engine client that records when each send happened."""

    def __init__(self, base_url: str = MAIN_URL, hold=None) -> None:
        self.base_url = base_url
        self.calls: list = []
        self.hold = hold
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.hold is not None:
            await self.hold.wait()
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="ok", reasoning_content=None, tool_calls=None),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=40, completion_tokens=2),
        )


def _engine(monkeypatch, client) -> None:
    monkeypatch.setattr(llm, "_client", lambda *a, **k: client)


def _tokenize(monkeypatch, double) -> None:
    monkeypatch.setattr(context, "_tokenize_client", lambda: double)


def _warm(double) -> None:
    """The first /tokenize of the process is `model_window`'s own probe; it is
    what puts the endpoint in `_window_from_server`. Do it up front so a test
    measures the TURN, not the cold cache."""
    context._window_cache[MAIN_URL] = double.window
    getattr(context, "_window_from_server", set()).add(MAIN_URL)


# ---------------------------------------------------------------------------
# 1 — the request goes first
# ---------------------------------------------------------------------------


def test_the_body_reaches_the_engine_before_its_tokenize_answers():
    """The defect, stated as a race: on HEAD the POST cannot be made until
    /tokenize has answered, so holding /tokenize holds the request."""

    async def run(monkeypatch):
        gate = asyncio.Event()
        counter = _Tokenize(count=40, window=1_000_000, gate=gate)
        engine = _Engine()
        _tokenize(monkeypatch, counter)
        _engine(monkeypatch, engine)
        _warm(counter)

        turn = asyncio.get_running_loop().create_task(
            llm.chat_completion([{"role": "user", "content": "hello"}], max_tokens=8000)
        )
        # Let the turn run as far as it can with /tokenize still unanswered.
        for _ in range(60):
            if engine.calls:
                break
            await asyncio.sleep(0)
        sent_while_uncounted = bool(engine.calls) and not gate.is_set()
        gate.set()
        await turn
        return sent_while_uncounted, len(counter.calls)

    with pytest.MonkeyPatch.context() as mp:
        sent_first, counted = asyncio.run(run(mp))
    assert sent_first, "the request waited for its /tokenize before going on the wire"
    # ...and the count still happened, exactly once.
    assert counted == 1


def test_a_send_first_turn_sizes_the_request_exactly_as_the_blocking_path_would():
    """The answer invariant in one process: same messages, same max_tokens."""

    async def run(monkeypatch):
        counter = _Tokenize(count=40, window=1_000_000)
        _tokenize(monkeypatch, counter)
        _warm(counter)
        msgs = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hello"}]

        slow = await context.fit_request(msgs, base_url=MAIN_URL, model="m", requested_max_tokens=8000)
        with context.settles_pending_count():
            fast = await context.fit_request(msgs, base_url=MAIN_URL, model="m", requested_max_tokens=8000)
        pending = context._pending_count.get()
        await context.settle_pending_count(fast[0], MAIN_URL)
        return slow, fast, pending is not None

    with pytest.MonkeyPatch.context() as mp:
        slow, fast, was_pending = asyncio.run(run(mp))
    assert was_pending, "the fast path did not fire"
    assert fast[0] == slow[0]
    assert fast[1] == slow[1]


# ---------------------------------------------------------------------------
# 2 — provenance: a window from configuration is not a window
# ---------------------------------------------------------------------------


def test_a_window_the_server_never_reported_does_not_fire_the_fast_path():
    """`model_window` falls back to a CONFIGURED constant when the count
    fails and caches it for the life of the process. That value is not
    evidence about the engine, and a request may not be sent against it."""

    async def run(monkeypatch):
        counter = _Tokenize(fail=True)
        _tokenize(monkeypatch, counter)
        with context.settles_pending_count():
            await context.fit_request(
                [{"role": "user", "content": "hello"}],
                base_url=MAIN_URL, model="m", requested_max_tokens=8000,
            )
        return (
            context._window_cache.get(MAIN_URL),
            context.window_is_server_reported(MAIN_URL),
            context._pending_count.get(),
        )

    with pytest.MonkeyPatch.context() as mp:
        cached, vouched, pending = asyncio.run(run(mp))
    # The cache IS populated — from configuration, which is the trap.
    assert cached == settings.model_max_context
    assert vouched is False
    assert pending is None, "a configured window fired the send-first path"


def test_a_failed_count_withdraws_a_window_an_earlier_count_had_vouched_for():
    async def run(monkeypatch):
        good = _Tokenize(count=40, window=1_000_000)
        _tokenize(monkeypatch, good)
        await context.count_tokens(MAIN_URL, "m", [{"role": "user", "content": "x"}])
        vouched_before = context.window_is_server_reported(MAIN_URL)
        _tokenize(monkeypatch, _Tokenize(fail=True))
        await context.count_tokens(MAIN_URL, "m", [{"role": "user", "content": "x"}])
        return vouched_before, context.window_is_server_reported(MAIN_URL)

    with pytest.MonkeyPatch.context() as mp:
        before, after = asyncio.run(run(mp))
    assert before is True and after is False


# ---------------------------------------------------------------------------
# 3 — the BOUND decides, never the estimate
# ---------------------------------------------------------------------------


#: Text the character estimate under-counts: a dense script, a base64 paste,
#: minified JSON. Kept out of the test id — a 10,000-character parameter
#: makes an unreadable one.
_UNDERCOUNTED = {
    "devanagari": "पहचानने वाली प्रणाली " * 400,
    "cjk": "这是一个非常密集的中文提示词" * 400,
    "base64": base64.b64encode(bytes(range(256)) * 40).decode(),
    "minified-json": '{"k":"v","n":12345,"b":true},' * 600,
}


@pytest.mark.parametrize("label", sorted(_UNDERCOUNTED))
def test_a_prompt_the_character_estimate_clears_but_the_bound_does_not_takes_the_slow_path(label):
    text = _UNDERCOUNTED[label]
    """N013, again. `estimate_messages` is len/3 for ASCII and a
    bytes-per-token AVERAGE otherwise; a dense script, a base64 paste or
    minified JSON all tokenize far above it. The gate reads
    `upper_bound_messages`, which the text cannot game down."""

    async def run(monkeypatch):
        counter = _Tokenize(count=40, window=1_000_000)
        _tokenize(monkeypatch, counter)
        _warm(counter)
        msgs = [{"role": "user", "content": text}]
        with context.settles_pending_count():
            await context.fit_request(msgs, base_url=MAIN_URL, model="m", requested_max_tokens=8000)
        return context._pending_count.get()

    msgs = [{"role": "user", "content": text}]
    estimate = context.estimate_messages(msgs)
    bound = context.upper_bound_messages(msgs)
    # A threshold the ESTIMATE is safely under and the BOUND is safely over.
    threshold = 2 * (estimate + bound) // 2
    threshold = max(2 * estimate + 2, min(threshold, 2 * bound - 2))
    assert estimate < threshold // 2 <= bound, (label, estimate, bound, threshold)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "admission_long_threshold_tokens", threshold)
        pending = asyncio.run(run(mp))
    assert pending is None, f"{label}: the estimate was allowed to decide"


# ---------------------------------------------------------------------------
# 4 — shapes the bound does not cover
# ---------------------------------------------------------------------------


def _png_part(width: int, height: int) -> dict:
    header = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
              + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00")
    return {"type": "image_url",
            "image_url": {"url": "data:image/png;base64," + base64.b64encode(header).decode()}}


_TOOL_ARGS = '{"q": "' + ("x" * 120_000) + '"}'

SHAPES = {
    # 120,000 characters of tool arguments bound at 55 tokens: NEITHER
    # `estimate_messages` NOR `upper_bound_messages` looks at tool_calls.
    "tool-call-with-null-content": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "search", "arguments": _TOOL_ARGS}}]},
    ],
    "tool-result-turn": [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "1", "content": "result"},
    ],
    "dict-content": [{"role": "user", "content": {"text": "hello"}}],
    "image-part": [{"role": "user", "content": [{"type": "text", "text": "what is this"},
                                                _png_part(1024, 1024)]}],
    "named-turn": [{"role": "user", "name": "a" * 50_000, "content": "hello"}],
}


@pytest.mark.parametrize("label", sorted(SHAPES))
def test_a_message_the_bound_does_not_cover_takes_the_slow_path(label):
    async def run(monkeypatch):
        counter = _Tokenize(count=40, window=1_000_000)
        _tokenize(monkeypatch, counter)
        _warm(counter)
        with context.settles_pending_count():
            await context.fit_request(
                SHAPES[label], base_url=MAIN_URL, model="m", requested_max_tokens=8000,
            )
        return context._pending_count.get()

    with pytest.MonkeyPatch.context() as mp:
        pending = asyncio.run(run(mp))
    assert pending is None, f"{label} was sized before it was counted"


def test_the_tool_argument_shape_really_does_defeat_both_counters():
    """The premise of the test above, asserted rather than assumed."""
    msgs = SHAPES["tool-call-with-null-content"]
    assert context.estimate_messages(msgs) < 100
    assert context.upper_bound_messages(msgs) < 100
    assert len(_TOOL_ARGS) > 100_000


# ---------------------------------------------------------------------------
# 5 — a small ceiling is not proof that the loop breaks
# ---------------------------------------------------------------------------


def test_a_bounded_classifier_call_with_no_room_for_a_minimum_answer_takes_the_slow_path():
    """`ceiling = 4` and a budget of 4 does not prove the trim loop breaks:
    its first break is `budget >= MIN_OUTPUT_TOKENS` (256). The gate asks for
    `max(ceiling, MIN_OUTPUT_TOKENS)`, so this one trims exactly as HEAD."""

    async def run(monkeypatch):
        # window 700, margin 0, a ~510-character prompt the engine counts at
        # 480 tokens: the budget is 220 — above the ceiling of 4 and below
        # MIN_OUTPUT_TOKENS, which is the whole point.
        counter = _Tokenize(count=480, window=700)
        _tokenize(monkeypatch, counter)
        context._window_cache[MAIN_URL] = 700
        context._window_from_server.add(MAIN_URL)
        msgs = [{"role": "user", "content": "q" * 500}, {"role": "user", "content": "now answer"}]
        # The double must not claim more tokens than the bound allows.
        assert counter.count <= context.upper_bound_messages(msgs)
        slow = await context.fit_request(msgs, base_url=MAIN_URL, model="m", requested_max_tokens=4)
        with context.settles_pending_count():
            fast = await context.fit_request(msgs, base_url=MAIN_URL, model="m", requested_max_tokens=4)
        return slow, fast, context._pending_count.get()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "context_safety_margin", 0)
        slow, fast, pending = asyncio.run(run(mp))
    assert pending is None, "a ceiling of 4 was taken as proof of 256 tokens of room"
    assert fast == slow


# ---------------------------------------------------------------------------
# 6 — continuations are never sized here
# ---------------------------------------------------------------------------


def test_a_continuation_never_takes_the_fast_path():
    async def run(monkeypatch):
        counter = _Tokenize(count=40, window=1_000_000)
        _tokenize(monkeypatch, counter)
        _warm(counter)
        sized, budget = await llm._fit(
            [{"role": "user", "content": "hello"},
             {"role": "assistant", "content": "an answer so far"}],
            base_url=MAIN_URL, model="m", requested_max_tokens=8000,
            what="stream", continuation=True,
        )
        return context._pending_count.get(), budget

    with pytest.MonkeyPatch.context() as mp:
        pending, budget = asyncio.run(run(mp))
    assert pending is None
    assert budget > 0


# ---------------------------------------------------------------------------
# 7 — the count still reaches the lanes and the meter
# ---------------------------------------------------------------------------


def test_the_settled_count_is_the_one_the_lanes_read_and_they_ask_for_no_other():
    async def run(monkeypatch):
        counter = _Tokenize(count=4242, window=1_000_000)
        engine = _Engine()
        _tokenize(monkeypatch, counter)
        _engine(monkeypatch, engine)
        _warm(counter)

        await llm.chat_completion([{"role": "user", "content": "hello"}], max_tokens=8000)
        sized = engine.calls[0]["messages"]
        # Read in the SAME task the turn ran in: `_measured` is a ContextVar.
        return context.measured_prompt_tokens(sized, MAIN_URL), len(counter.calls)

    with pytest.MonkeyPatch.context() as mp:
        measured, counts = asyncio.run(run(mp))
    assert measured == 4242, "the count never reached the meter"
    # ONE /tokenize for the turn: the lanes issued none of their own.
    assert counts == 1


def test_the_lane_a_send_first_turn_takes_is_the_lane_the_exact_count_would_have_chosen():
    """Admission runs INSIDE the send, i.e. before settlement, so it reads
    the estimate. Under the gate both the estimate and the exact count are
    below half the LONG threshold, so the lane cannot differ."""
    msgs = [{"role": "user", "content": "a short question"}]
    threshold = settings.admission_long_threshold_tokens
    estimate = context.estimate_messages(msgs)
    bound = context.upper_bound_messages(msgs)
    assert bound < threshold // 2
    assert admission.lane_for(estimate) == admission.lane_for(bound) == admission.NORMAL


# ---------------------------------------------------------------------------
# 8 — a Stop owes nothing
# ---------------------------------------------------------------------------


def test_a_stopped_turn_leaves_no_count_running():
    async def run(monkeypatch):
        gate = asyncio.Event()      # /tokenize never answers
        hold = asyncio.Event()      # the engine never answers either
        counter = _Tokenize(count=40, window=1_000_000, gate=gate)
        engine = _Engine(hold=hold)
        _tokenize(monkeypatch, counter)
        _engine(monkeypatch, engine)
        _warm(counter)

        turn = asyncio.get_running_loop().create_task(
            llm.chat_completion([{"role": "user", "content": "hello"}], max_tokens=8000)
        )
        for _ in range(60):
            if engine.calls:
                break
            await asyncio.sleep(0)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        # Let the cancellation land on the count task too.
        for _ in range(10):
            await asyncio.sleep(0)
        gate.set()
        hold.set()
        await asyncio.sleep(0)
        return [t for t in context._INFLIGHT_COUNTS if not t.done()]

    with pytest.MonkeyPatch.context() as mp:
        still_running = asyncio.run(run(mp))
    assert still_running == [], "a stopped turn left its /tokenize running"


# ---------------------------------------------------------------------------
# 9 — the window shrank under a request that had already gone
# ---------------------------------------------------------------------------


def test_a_window_that_shrank_under_the_send_withdraws_the_fast_path():
    """On the blocking path a shrunken window self-heals on the very call
    that would otherwise overflow, because the fit re-reads it from the
    /tokenize it is about to use. A request that has already been sent cannot
    self-heal, so the mark is withdrawn and every later call waits again."""

    async def run(monkeypatch):
        counter = _Tokenize(count=40, window=1_000_000, windows=[8_192, 8_192])
        _tokenize(monkeypatch, counter)
        _warm(counter)

        with context.settles_pending_count():
            sized, _ = await context.fit_request(
                [{"role": "user", "content": "hello"}],
                base_url=MAIN_URL, model="m", requested_max_tokens=8000,
            )
        fired = context._pending_count.get() is not None
        await context.settle_pending_count(sized, MAIN_URL)
        vouched = context.window_is_server_reported(MAIN_URL)
        cached = context._window_cache.get(MAIN_URL)

        with context.settles_pending_count():
            await context.fit_request(
                [{"role": "user", "content": "hello again"}],
                base_url=MAIN_URL, model="m", requested_max_tokens=8000,
            )
        return fired, vouched, cached, context._pending_count.get()

    with pytest.MonkeyPatch.context() as mp:
        fired, vouched, cached, pending_after = asyncio.run(run(mp))
    assert fired, "the first call did not take the fast path"
    assert vouched is False, "the mark survived a window that had changed"
    assert cached == 8_192, "the cache kept a window the engine no longer serves"
    assert _counter("context_window_changed_under_send_total") == 1.0
    assert pending_after is None, "the next call sent first against a withdrawn window"


# ---------------------------------------------------------------------------
# The /v1 surface keeps its exact count
# ---------------------------------------------------------------------------


def test_the_public_api_surface_is_never_sized_before_it_is_counted():
    """A chat-origin NORMAL ticket charges no KV and arms no closure timer,
    so the count is the only thing the lane would have used. A /v1 NORMAL
    ticket DOES charge KV from the count — and an estimate a base64 paste can
    defeat threefold is not a count."""

    async def run(monkeypatch):
        counter = _Tokenize(count=40, window=1_000_000)
        _tokenize(monkeypatch, counter)
        _warm(counter)
        with admission.origin(admission.ORIGIN_V1):
            with context.settles_pending_count():
                await context.fit_request(
                    [{"role": "user", "content": "hello"}],
                    base_url=MAIN_URL, model="m", requested_max_tokens=8000,
                )
            return context._pending_count.get()

    with pytest.MonkeyPatch.context() as mp:
        assert asyncio.run(run(mp)) is None


# ---------------------------------------------------------------------------
# Only a caller that settles may send first
# ---------------------------------------------------------------------------


def test_a_caller_that_does_not_settle_gets_todays_blocking_behaviour():
    async def run(monkeypatch):
        counter = _Tokenize(count=40, window=1_000_000)
        _tokenize(monkeypatch, counter)
        _warm(counter)
        sized, _ = await context.fit_request(
            [{"role": "user", "content": "hello"}],
            base_url=MAIN_URL, model="m", requested_max_tokens=8000,
        )
        # The blocking path counted before it returned, so the count is
        # already the lanes' to read — with no settlement anywhere.
        return context._pending_count.get(), context.measured_prompt_tokens(sized, MAIN_URL)

    with pytest.MonkeyPatch.context() as mp:
        pending, measured = asyncio.run(run(mp))
    assert pending is None
    assert measured == 40


# ---------------------------------------------------------------------------
# THE STREAMED SITES (verification 2026-09-27)
# ---------------------------------------------------------------------------
#
# WHY THESE EXIST. Everything above settles through `llm._settling`, which the
# non-streaming calls use. The two STREAMING sites do NOT use it: they hold a
# `dispatched` flag, cancel the count in a `finally` when nothing was
# dispatched, and settle INSIDE `_consume` with the stream already held. That
# hand-rolled pair is the most fragile code in this change and the file it was
# added by tested none of it — the proof lived in scripts that were never
# committed. A Stop landing on the settlement must close the stream and
# release its admission lane; a dispatch that never happened must drop the
# count; and a settlement that happens must reach the lanes. Each is asserted
# here against the real `llm` functions.


class _StreamDouble:
    """A streamed response that records its own close."""

    def __init__(self, hold=None) -> None:
        self.closed = 0
        self.hold = hold

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        if self.hold is not None:
            await self.hold.wait()
        yield SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content="ok", reasoning_content=None),
                finish_reason=None,
            )],
            usage=None,
        )

    async def close(self):
        self.closed += 1


class _StreamEngine:
    """A streaming engine client. `raise_on_send` refuses the dispatch."""

    def __init__(self, raise_on_send=None, hold=None, base_url: str = MAIN_URL) -> None:
        self.base_url = base_url
        self.calls: list = []
        self.streams: list = []
        self.raise_on_send = raise_on_send
        self.hold = hold
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_on_send is not None:
            raise self.raise_on_send
        stream = _StreamDouble(self.hold)
        self.streams.append(stream)
        return stream


def _still_counting() -> list:
    return [t for t in getattr(context, "_INFLIGHT_COUNTS", ()) if not t.done()]


@pytest.mark.parametrize("site", ["stream_chat_completion", "stream_chat_events"])
def test_a_streamed_turn_sends_first_and_settles_with_the_stream_already_open(site):
    """The body goes on the wire before the count answers, and the count still
    reaches the lanes — on BOTH streaming sites."""

    async def run(monkeypatch):
        gate = asyncio.Event()
        counter = _Tokenize(count=4242, window=1_000_000, gate=gate)
        engine = _StreamEngine()
        _tokenize(monkeypatch, counter)
        _engine(monkeypatch, engine)
        _warm(counter)

        async def drive():
            if site == "stream_chat_completion":
                async for _ in llm.stream_chat_completion(
                    [{"role": "user", "content": "hello"}], max_tokens=8000
                ):
                    pass
            else:
                async for _ in llm.stream_chat_events(
                    messages=[{"role": "user", "content": "hello"}], max_tokens=8000
                ):
                    pass

        turn = asyncio.get_running_loop().create_task(drive())
        for _ in range(200):
            if engine.calls:
                break
            await asyncio.sleep(0)
        sent_while_uncounted = bool(engine.calls) and not gate.is_set()
        gate.set()
        await turn
        # `_measured` is a ContextVar: read it in the task that settled it.
        return sent_while_uncounted, len(counter.calls), _still_counting()

    with pytest.MonkeyPatch.context() as mp:
        sent_first, counts, left = asyncio.run(run(mp))
    assert sent_first, "the streamed request waited for its /tokenize before going on the wire"
    assert counts == 1, "the count was duplicated or skipped"
    assert left == [], "the turn ended with its count still running"


def test_a_stop_on_the_settlement_closes_the_stream_and_releases_its_lane():
    """The settlement is awaited with the stream ALREADY held, so a Stop
    landing on it runs `_consume`'s close — the admission lane and the
    breaker permit the stream holds are released instead of being stranded."""

    async def run(monkeypatch):
        gate = asyncio.Event()          # /tokenize never answers
        counter = _Tokenize(count=40, window=1_000_000, gate=gate)
        engine = _StreamEngine()
        _tokenize(monkeypatch, counter)
        _engine(monkeypatch, engine)
        _warm(counter)

        async def drive():
            async for _ in llm.stream_chat_events(
                messages=[{"role": "user", "content": "hello"}], max_tokens=8000
            ):
                pass

        turn = asyncio.get_running_loop().create_task(drive())
        for _ in range(200):
            if engine.streams:
                break
            await asyncio.sleep(0)
        # Park the turn on the settlement, then Stop it there.
        for _ in range(20):
            await asyncio.sleep(0)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        for _ in range(20):
            await asyncio.sleep(0)
        left = _still_counting()
        gate.set()
        for _ in range(50):
            await asyncio.sleep(0)
        return (sum(s.closed for s in engine.streams),
                len(engine.streams),
                admission.lanes().normal.active,
                left)

    with pytest.MonkeyPatch.context() as mp:
        closed, opened, lane_active, left = asyncio.run(run(mp))
    assert opened == 1
    assert closed == 1, "a Stop on the settlement stranded an open stream"
    assert lane_active == 0, "a Stop on the settlement stranded the admission lane"
    assert left == [], "a Stop on the settlement left the count running"


def test_a_streamed_dispatch_that_never_happened_drops_its_count():
    """`_open_stream` refused, so nothing was sent: the count owes nothing and
    must be dropped by the `finally`, not left for a settlement that will
    never come."""

    async def run(monkeypatch):
        gate = asyncio.Event()
        counter = _Tokenize(count=40, window=1_000_000, gate=gate)
        engine = _StreamEngine(raise_on_send=RuntimeError("engine down"))
        _tokenize(monkeypatch, counter)
        _engine(monkeypatch, engine)
        _warm(counter)

        async def drive():
            async for _ in llm.stream_chat_events(
                messages=[{"role": "user", "content": "hello"}], max_tokens=8000
            ):
                pass

        # BOUNDED on purpose. A tree that blocks on its /tokenize before the
        # dispatch never reaches the engine at all, so it must FAIL here with
        # a timeout rather than hang the suite.
        with pytest.raises(BaseException) as refused:
            await asyncio.wait_for(drive(), timeout=5.0)
        assert isinstance(refused.value, RuntimeError) and not isinstance(
            refused.value, asyncio.TimeoutError
        ), "the request never reached the engine: it was still waiting for its /tokenize"
        for _ in range(20):
            await asyncio.sleep(0)
        pending = context._pending_count.get()
        left = _still_counting()
        gate.set()
        for _ in range(50):
            await asyncio.sleep(0)
        return pending, left

    with pytest.MonkeyPatch.context() as mp:
        pending, left = asyncio.run(run(mp))
    assert pending is None, "a refused dispatch left a count owed to nobody"
    assert left == [], "a refused dispatch left its count running"


# ---------------------------------------------------------------------------
# THE GUIDED-JSON DOWNGRADE (verification 2026-09-27)
# ---------------------------------------------------------------------------


def test_the_guided_json_downgrade_still_settles_its_count():
    """The 400 that turns a guided JSON call into an unconstrained one is a
    caught exception INSIDE the settlement, not a failed turn: the request did
    reach the engine, so the count is still owed to the lanes and the meter.
    Settling inside the `try` would cancel it on the way to the re-send."""

    class _Downgrading:
        def __init__(self) -> None:
            self.base_url = MAIN_URL
            self.calls: list = []
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        async def _create(self, **kwargs):
            self.calls.append(kwargs)
            if "response_format" in kwargs:
                raise llm._bad_request_error()(
                    "guided decoding unavailable",
                    response=httpx.Response(
                        400, request=httpx.Request("POST", MAIN_URL + "/chat/completions")
                    ),
                    body=None,
                )
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content='{"ok": true}', reasoning_content=None,
                                            tool_calls=None),
                    finish_reason="stop",
                )],
                usage=SimpleNamespace(prompt_tokens=40, completion_tokens=2),
            )

    async def run(monkeypatch):
        counter = _Tokenize(count=4242, window=1_000_000)
        engine = _Downgrading()
        _tokenize(monkeypatch, counter)
        _engine(monkeypatch, engine)
        _warm(counter)
        text = await llm.json_completion(
            [{"role": "user", "content": "hello"}],
            json_schema={"type": "object"}, max_tokens=800,
        )
        sized = engine.calls[-1]["messages"]
        return (text, len(engine.calls), len(counter.calls),
                context.measured_prompt_tokens(sized, MAIN_URL), _still_counting())

    with pytest.MonkeyPatch.context() as mp:
        text, sends, counts, measured, left = asyncio.run(run(mp))
    assert text == '{"ok": true}'
    assert sends == 2, "the downgrade did not re-send"
    assert counts == 1, "the downgrade counted the prompt twice"
    assert measured == 4242, "the downgrade lost the count the lanes and the meter are owed"
    assert left == []


# ---------------------------------------------------------------------------
# THE ANSWER INVARIANT, IN THE SUITE (verification 2026-09-27)
# ---------------------------------------------------------------------------
#
# The change's central claim is that the body on the wire is byte-identical.
# It was proved by scripts that were never committed, so nothing in the suite
# would notice a later edit to `_may_send_first` that changed `max_tokens` —
# dropping `max(ceiling, MIN_OUTPUT_TOKENS)`, or admitting a shape the bound
# does not cover. These two tests are that guard.


_SIZING_CASES = [
    # (label, window, ceiling, messages)
    ("greeting", 1_000_000, 8_000, [{"role": "user", "content": "hi"}]),
    ("system+user", 1_000_000, 8_000,
     [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hello " * 400}]),
    ("assistant history", 1_000_000, 8_000,
     [{"role": "system", "content": "s"}]
     + [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"} for i in range(40)]),
    ("classifier ceiling 4", 1_000_000, 4, [{"role": "user", "content": "yes or no"}]),
    ("ceiling above the window", 8_192, 100_000, [{"role": "user", "content": "hi"}]),
    ("a prompt near the window", 8_192, 256, [{"role": "user", "content": "x" * 6_000}]),
    ("empty content", 1_000_000, 8_000, [{"role": "user", "content": ""}]),
]


@pytest.mark.parametrize("label,window,ceiling,msgs", _SIZING_CASES,
                         ids=[c[0] for c in _SIZING_CASES])
def test_the_fast_path_sizes_every_shape_exactly_as_the_blocking_path(label, window, ceiling, msgs):
    """Whatever the gate decides, the two paths must agree to the token.

    A shape the gate REFUSES is covered too: then both runs are the blocking
    path and the test only says so, which is what keeps this table honest as
    the gate changes."""

    async def run(monkeypatch):
        counter = _Tokenize(count=max(1, context.estimate_messages(msgs)), window=window)
        _tokenize(monkeypatch, counter)
        _warm(counter)

        slow = await context.fit_request(msgs, base_url=MAIN_URL, model="m",
                                         requested_max_tokens=ceiling)
        with context.settles_pending_count():
            fast = await context.fit_request(msgs, base_url=MAIN_URL, model="m",
                                             requested_max_tokens=ceiling)
        fired = context._pending_count.get() is not None
        await context.settle_pending_count(fast[0], MAIN_URL)
        return slow, fast, fired

    with pytest.MonkeyPatch.context() as mp:
        slow, fast, fired = asyncio.run(run(mp))
    assert fast[0] == slow[0], f"{label}: the fast path changed the messages"
    assert fast[1] == slow[1], f"{label}: the fast path changed max_tokens"
    if fired:
        # The gate's own arithmetic: it only fires where max_tokens is the
        # caller's ceiling exactly, never a trimmed budget.
        assert fast[1] == ceiling, f"{label}: fired without pinning max_tokens to the ceiling"


def test_a_send_first_turn_puts_the_SAME_request_on_the_wire_as_a_blocking_one():
    """One process, one engine double, two turns of the same shape: the first
    with the fast path available, the second with the window mark withdrawn so
    it blocks. The recorded request objects must be equal — and the first must
    really have gone before its count, or this proves nothing."""

    async def run(monkeypatch):
        gate = asyncio.Event()
        counter = _Tokenize(count=40, window=1_000_000, gate=gate)
        engine = _Engine()
        _tokenize(monkeypatch, counter)
        _engine(monkeypatch, engine)
        _warm(counter)
        msgs = [{"role": "system", "content": "be brief"},
                {"role": "user", "content": "how does the deploy chain work"}]

        turn = asyncio.get_running_loop().create_task(
            llm.chat_completion(list(msgs), max_tokens=8000)
        )
        for _ in range(200):
            if engine.calls:
                break
            await asyncio.sleep(0)
        sent_first = bool(engine.calls) and not gate.is_set()
        gate.set()
        await turn

        # Withdraw the mark: the next turn must count before it sends.
        getattr(context, "_window_from_server", set()).discard(MAIN_URL)
        await llm.chat_completion(list(msgs), max_tokens=8000)
        return sent_first, engine.calls

    with pytest.MonkeyPatch.context() as mp:
        sent_first, calls = asyncio.run(run(mp))
    assert sent_first, "the first turn waited for its /tokenize, so this compares nothing"
    assert len(calls) == 2
    assert calls[0] == calls[1], "the send-first turn put a different request on the wire"
