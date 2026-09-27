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
import contextlib
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
    """The premise of the test above, asserted rather than assumed.

    The EXACT figures are pinned because `_may_send_first`'s condition-5
    docstring quotes them: it said "bounds at 55" until 2026-09-28, which is
    not what the code returns for this shape.
    """
    msgs = SHAPES["tool-call-with-null-content"]
    assert len(_TOOL_ARGS) > 100_000
    assert context.upper_bound_messages(msgs) == 26, (
        "the figure condition 5's docstring quotes has moved; update the docstring too"
    )
    assert context.estimate_messages(msgs) == 12
    assert context.upper_bound_messages(msgs[1:]) == 16, "the single-message form, for the record"


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


# ---------------------------------------------------------------------------
# A send-first request the engine REFUSES on size
# ---------------------------------------------------------------------------
#
# QA 2026-09-27. `settle_pending_count` withdraws the window mark when the
# served window turns out not to be the one the request was sized against —
# but it only ever runs when the send SUCCEEDED. When the engine refuses the
# oversized request instead, `cancel_pending_count` kills the count that would
# have reported the real window, nothing is written back, and the endpoint
# keeps its mark: every later turn is sized send-first against the same stale
# window and is refused again. Measured before the fix below, with the
# refusal arriving before the /tokenize answer: 4 of 4 `llm.chat_completion`
# turns refused, and `window_is_server_reported` still True, where the
# blocking path served all four at max_tokens=2680.


def _size_refusal(window: int, wanted: int):
    """The 400 vLLM answers when prompt + max_tokens exceeds max_model_len."""
    import openai

    message = (
        f"This model's maximum context length is {window} tokens. "
        f"However, you requested {wanted} tokens"
    )
    return openai.BadRequestError(
        message,
        response=httpx.Response(
            400, request=httpx.Request("POST", MAIN_URL), json={"error": {"message": message}}
        ),
        body={"error": {"message": message}},
    )


class _SizeStrictEngine:
    """An engine that refuses exactly as vLLM does when the request does not
    fit the window it is really serving."""

    def __init__(self, served_window: int, prompt_tokens: int, base_url: str = MAIN_URL) -> None:
        self.base_url = base_url
        self.served_window = served_window
        self.prompt_tokens = prompt_tokens
        self.calls: list = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        wanted = self.prompt_tokens + int(kwargs.get("max_tokens") or 0)
        if wanted > self.served_window:
            raise _size_refusal(self.served_window, wanted)
        if kwargs.get("stream"):
            # The streamed sites dispatch through the same engine.
            return _StreamDouble()
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="ok", reasoning_content=None, tool_calls=None),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=self.prompt_tokens, completion_tokens=2),
        )




async def _drive_dispatch_site(site: str, msgs, max_tokens: int = 8000):
    """One turn through whichever entry point dispatches it."""
    if site == "chat_completion":
        return await llm.chat_completion(list(msgs), max_tokens=max_tokens)
    if site == "stream_chat_completion":
        async for _ in llm.stream_chat_completion(list(msgs), max_tokens=max_tokens):
            pass
        return None
    if site == "stream_chat_events":
        async for _ in llm.stream_chat_events(messages=list(msgs), max_tokens=max_tokens):
            pass
        return None
    raise AssertionError(f"unknown dispatch site {site!r}")


#: EVERY site that can dispatch a send-first request. `_settling` covers the
#: non-streaming ones; the two streaming ones call
#: `llm._withdraw_window_on_size_refusal` by hand, and until 2026-09-28 both of
#: those calls could be deleted with this whole file still green (measured: 36
#: passed with either one removed, while four consecutive turns went
#: BadRequestError x4 — the exact defect the withdrawal exists to fix).
_DISPATCH_SITES = ["chat_completion", "stream_chat_completion", "stream_chat_events"]


@pytest.mark.parametrize("site", _DISPATCH_SITES)
def test_a_send_first_request_refused_on_size_stops_vouching_for_the_window(site):
    """The refusal must cost ONE turn, not every turn — at EVERY dispatch site.

    The engine came back serving 8,192 where a real count had once reported
    1,000,000. The first turn was sized send-first against the stale window
    and is refused — that is the documented cost of having already sent. The
    SECOND turn must count first and be served, which is only possible if the
    refusal withdrew the mark. The /tokenize answer is held until after the
    refusal on purpose: that is the ordering in which the count cannot
    withdraw anything itself.

    BOUNDED, and the gate is released in a `finally`. The r3 version awaited
    the turn with the gate still held and released it only afterwards, so on
    any tree where the request waits for its count the turn never finished and
    the test HUNG instead of failing (measured 2026-09-28 with the fast path
    forced off on this branch: `timeout -s KILL 90 pytest -k refused_on_size`
    -> EXIT=137). A red test is worth a shard; a wedged one costs the 90-minute
    CI ceiling.
    """

    async def run(monkeypatch):
        gate = asyncio.Event()
        counter = _Tokenize(count=5000, window=8_192, gate=gate)
        engine = _SizeStrictEngine(served_window=8_192, prompt_tokens=5000)
        _tokenize(monkeypatch, counter)
        _engine(monkeypatch, engine)
        # A window a real count once vouched for, and no longer the served one.
        context._window_cache[MAIN_URL] = 1_000_000
        getattr(context, "_window_from_server", set()).add(MAIN_URL)

        msgs = [{"role": "user", "content": "word " * 3000}]
        first = None
        turn = asyncio.get_running_loop().create_task(_drive_dispatch_site(site, msgs))
        try:
            for _ in range(200):
                if engine.calls:
                    break
                await asyncio.sleep(0)
            sent_before_its_count = bool(engine.calls) and not gate.is_set()
            try:
                # `asyncio.timeout`, not `wait_for`: CI runs Python 3.11, where
                # `wait_for` can swallow a cancellation delivered in the same
                # pass and hang (it wedged PR #65's CI at 45 minutes).
                async with asyncio.timeout(5.0):
                    await turn
            except asyncio.TimeoutError:
                first = "TimeoutError (the turn waited for its count)"
            except BaseException as exc:  # noqa: BLE001 - the refusal is the point
                first = type(exc).__name__
        finally:
            gate.set()
            if not turn.done():
                turn.cancel()
            with contextlib.suppress(BaseException):
                await turn
        await asyncio.sleep(0)
        vouched_after_the_refusal = context.window_is_server_reported(MAIN_URL)
        refusals = _counter("llm_send_first_refused_on_size_total")

        second = None
        try:
            async with asyncio.timeout(5.0):
                await _drive_dispatch_site(site, msgs)
            second = "served"
        except BaseException as exc:  # noqa: BLE001
            second = type(exc).__name__
        return (sent_before_its_count, first, vouched_after_the_refusal, second,
                [c["max_tokens"] for c in engine.calls], len(_still_counting()), refusals)

    with pytest.MonkeyPatch.context() as mp:
        sent_first, first, vouched, second, max_tokens, counting, refusals = asyncio.run(run(mp))
    assert sent_first, "the first turn waited for its count, so this proves nothing"
    assert first == "BadRequestError", f"[{site}] the oversized request was not refused: {first}"
    assert vouched is False, (
        f"[{site}] the endpoint still vouches for a window the engine refused a request "
        "against, so every later turn is sized send-first against it and refused again"
    )
    assert second == "served", f"[{site}] the turn after the refusal was refused too: {second}"
    assert max_tokens[0] >= 8000 and max_tokens[-1] == 2680, (
        f"[{site}] the turn after the refusal was not sized from a fresh count: {max_tokens}"
    )
    assert counting == 0, "a count outlived the request that started it"
    # The one new user-visible failure this change introduces is COUNTED. Until
    # 2026-09-28 nothing recorded it: `context_window_changed_under_send_total`
    # is incremented by `settle_pending_count`, i.e. only on a send that
    # SUCCEEDED, so the shrink that cost nobody a turn was counted and the
    # shrink that cost one was invisible in Prometheus and in the log.
    assert refusals == 1.0, (
        f"[{site}] a turn was refused because the window shrank under it and nothing counted it"
    )


def test_the_refused_turn_says_so_in_the_log(caplog):
    """A refused turn that is only a counter is still hard to act on: the log
    line names the endpoint and the engine's own message."""

    async def run(monkeypatch):
        counter = _Tokenize(count=5000, window=8_192, gate=asyncio.Event())
        engine = _SizeStrictEngine(served_window=8_192, prompt_tokens=5000)
        _tokenize(monkeypatch, counter)
        _engine(monkeypatch, engine)
        context._window_cache[MAIN_URL] = 1_000_000
        getattr(context, "_window_from_server", set()).add(MAIN_URL)
        with contextlib.suppress(BaseException):
            async with asyncio.timeout(5.0):
                await _drive_dispatch_site(
                    "chat_completion", [{"role": "user", "content": "word " * 3000}]
                )

    with caplog.at_level("WARNING", logger="app.llm"):
        with pytest.MonkeyPatch.context() as mp:
            asyncio.run(run(mp))
    lines = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("refused this turn on size" in line and MAIN_URL in line for line in lines), lines


def test_a_size_refusal_of_a_request_that_counted_first_keeps_the_window_mark():
    """The narrowing to `has_pending_count` is the difference between one
    endpoint user paying a round trip and ALL of them paying one after any
    size refusal — a refusal of a request that counted first says nothing
    about the window's provenance (a /v1 request, a continuation, a shape the
    bound does not cover)."""

    async def run(monkeypatch):
        counter = _Tokenize(count=5000, window=1_000_000)
        engine = _SizeStrictEngine(served_window=8_192, prompt_tokens=5000)
        _tokenize(monkeypatch, counter)
        _engine(monkeypatch, engine)
        _warm(counter)
        # An image part: outside what the bound covers, so this turn COUNTS
        # first — and the engine refuses it on size all the same.
        msgs = [{"role": "user", "content": [{"type": "text", "text": "what is this"},
                                             _png_part(1024, 1024)]}]
        refused = None
        try:
            async with asyncio.timeout(5.0):
                await llm.chat_completion(msgs, max_tokens=8000)
        except BaseException as exc:  # noqa: BLE001
            refused = type(exc).__name__
        return refused, context.window_is_server_reported(MAIN_URL), _counter(
            "llm_send_first_refused_on_size_total")

    with pytest.MonkeyPatch.context() as mp:
        refused, vouched, refusals = asyncio.run(run(mp))
    assert refused == "BadRequestError", "the count-first request was not refused on size"
    assert vouched is True, (
        "a size refusal of a request that COUNTED first withdrew the window mark, so every "
        "later turn on this endpoint pays a round trip it did not have to"
    )
    assert refusals == 0.0, "a count-first refusal was counted as a send-first one"


# ---------------------------------------------------------------------------
# EVERY settlement site, not just the two that happened to be covered
# ---------------------------------------------------------------------------
#
# Measured 2026-09-28: removing `async with _settling(...)` from
# `chat_with_tools`, from `router_chat_completion` or from
# `chat_completion_with_reasoning` each left this file at 36 passed, although
# each loses the exact count for `_measured`, `_last_count_exact` and the meter
# at that entry point. (`chat_completion` went 3 failed / 33 passed and
# `json_completion` 1 failed / 35 passed.) This parametrised test is the guard
# for all five.


_NON_STREAMING_SITES = ["chat_completion", "chat_completion_with_reasoning",
                        "chat_with_tools", "json_completion", "router_chat_completion"]


async def _drive_non_streaming(site: str, msgs):
    if site == "chat_completion":
        return await llm.chat_completion(list(msgs), max_tokens=8000)
    if site == "chat_completion_with_reasoning":
        return await llm.chat_completion_with_reasoning(list(msgs), max_tokens=8000)
    if site == "chat_with_tools":
        return await llm.chat_with_tools(list(msgs), tools=[{"type": "function", "function": {
            "name": "decide", "parameters": {"type": "object"}}}], max_tokens=8000)
    if site == "json_completion":
        return await llm.json_completion(list(msgs), json_schema={"type": "object"},
                                         max_tokens=800)
    if site == "router_chat_completion":
        return await llm.router_chat_completion(list(msgs), max_tokens=200)
    raise AssertionError(f"unknown site {site!r}")


@pytest.mark.parametrize("site", _NON_STREAMING_SITES)
def test_every_non_streaming_entry_point_settles_its_count(site):
    """One /tokenize per turn, and the count reaches `_measured` — at every
    non-streaming entry point, not only the two whose other assertions
    happened to fail without it."""
    url = ROUTER_URL if site == "router_chat_completion" else MAIN_URL

    async def run(monkeypatch):
        counter = _Tokenize(count=4242, window=1_000_000)
        engine = _Engine(base_url=url)
        _tokenize(monkeypatch, counter)
        _engine(monkeypatch, engine)
        context._window_cache[url] = counter.window
        getattr(context, "_window_from_server", set()).add(url)

        await _drive_non_streaming(site, [{"role": "user", "content": "hello"}])
        sized = engine.calls[-1]["messages"]
        # Read in the SAME task the turn ran in: `_measured` is a ContextVar.
        return (context.measured_prompt_tokens(sized, url), len(counter.calls),
                context._last_count_exact.get(), _still_counting())

    with pytest.MonkeyPatch.context() as mp:
        measured, counts, exact, left = asyncio.run(run(mp))
    assert measured == 4242, f"[{site}] the count never reached the meter"
    assert exact is True, f"[{site}] the turn reports 'not measured' after settling"
    assert counts == 1, f"[{site}] the prompt was counted {counts} times"
    assert left == [], f"[{site}] the turn ended with its count still running"


@pytest.mark.parametrize("site", ["stream_chat_completion", "stream_chat_events"])
def test_a_streamed_dispatch_that_never_happened_drops_its_count(site):
    """`_open_stream` refused, so nothing was sent: the count owes nothing and
    must be dropped by the `finally`, not left for a settlement that will
    never come. BOTH streaming sites: `stream_chat_completion`'s guard could
    be deleted with this file green until 2026-09-28."""

    async def run(monkeypatch):
        gate = asyncio.Event()
        counter = _Tokenize(count=40, window=1_000_000, gate=gate)
        engine = _StreamEngine(raise_on_send=RuntimeError("engine down"))
        _tokenize(monkeypatch, counter)
        _engine(monkeypatch, engine)
        _warm(counter)

        # BOUNDED on purpose. A tree that blocks on its /tokenize before the
        # dispatch never reaches the engine at all, so it must FAIL here with
        # a timeout rather than hang the suite.
        with pytest.raises(BaseException) as refused:
            async with asyncio.timeout(5.0):
                await _drive_dispatch_site(site, [{"role": "user", "content": "hello"}])
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
    assert pending is None, f"[{site}] a refused dispatch left a count owed to nobody"
    assert left == [], f"[{site}] a refused dispatch left its count running"


# ---------------------------------------------------------------------------
# The gate's own safety, tested instead of argued
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ceiling,fast", [
    # `lane_for` returns LONG_OUTPUT on max_tokens ABOVE the threshold, so the
    # boundary itself must stay on the fast path: the unbounded-thinking entry
    # points (`chat_with_tools`, `json_completion`,
    # `chat_completion_with_reasoning`, `stream_chat_events`) floor their
    # ceiling at `settings.max_output_tokens`, which IS 65,536.
    (8_000, True),
    (65_536, True),
    (65_537, False),
    (200_000, False),
])
def test_a_ceiling_that_could_take_the_long_output_lane_is_counted_first(ceiling, fast):
    """Condition 4's argument is "a chat-origin NORMAL ticket charges no KV".
    `admission.lane_for` returns LONG_OUTPUT above the chat threshold and
    `_admit`'s LONG_OUTPUT branch charges KV from `tokens` — the ESTIMATE on
    this path, which under-counts the real tokenizer by up to 2.3x. Nothing
    reaches that branch from chat today, so condition 6 costs no live call;
    it makes the argument true by construction rather than by a property of a
    call site two modules away."""

    async def run(monkeypatch):
        counter = _Tokenize(count=40, window=1_000_000)
        _tokenize(monkeypatch, counter)
        _warm(counter)
        with context.settles_pending_count():
            await context.fit_request(
                [{"role": "user", "content": "hello"}],
                base_url=MAIN_URL, model="m", requested_max_tokens=ceiling,
            )
        fired = context._pending_count.get() is not None
        context.cancel_pending_count()
        return fired, len(counter.calls)

    with pytest.MonkeyPatch.context() as mp:
        fired, counts = asyncio.run(run(mp))
    assert fired is fast, (
        f"ceiling {ceiling}: fast path fired={fired}, expected {fast} "
        f"(chat LONG_OUTPUT threshold {admission.chat_long_output_threshold_tokens()})"
    )
    if not fast:
        assert counts == 1, "the slow path did not count the prompt before sizing"


def test_the_settlement_refuses_a_count_that_describes_another_request():
    """`settle_pending_count` writes back only for the list it was started
    for. Without the identity check a caller that settled the wrong list would
    publish one request's count as another's."""

    async def run(monkeypatch):
        counter = _Tokenize(count=4242, window=1_000_000)
        _tokenize(monkeypatch, counter)
        _warm(counter)
        with context.settles_pending_count():
            sized, _ = await context.fit_request(
                [{"role": "user", "content": "hello"}],
                base_url=MAIN_URL, model="m", requested_max_tokens=8000,
            )
        other = [{"role": "user", "content": "a different request"}]
        await context.settle_pending_count(other, MAIN_URL)
        # A cancelled task is not `done()` until the loop has run again.
        for _ in range(20):
            await asyncio.sleep(0)
        return (context.measured_prompt_tokens(sized, MAIN_URL),
                context.measured_prompt_tokens(other, MAIN_URL),
                context._pending_count.get(), _still_counting())

    with pytest.MonkeyPatch.context() as mp:
        for_sized, for_other, pending, left = asyncio.run(run(mp))
    assert for_other is None, "one request's count was published as another's"
    assert for_sized is None, "a count settled against the wrong list was written back anyway"
    assert pending is None and left == [], "the mis-settled count was left running"


def test_a_send_first_turn_reports_not_measured_until_it_settles():
    """Between the dispatch and the settlement the turn must not report a
    PREVIOUS turn's exactness or count: admission runs in that gap."""

    seen: dict = {}

    class _Sampling(_Engine):
        async def _create(self, **kwargs):
            seen["exact"] = context._last_count_exact.get()
            seen["measured"] = context._measured.get()
            return await super()._create(**kwargs)

    async def run(monkeypatch):
        counter = _Tokenize(count=4242, window=1_000_000)
        engine = _Sampling()
        _tokenize(monkeypatch, counter)
        _engine(monkeypatch, engine)
        _warm(counter)
        # A first turn that settles an exact count, so the ContextVars are
        # True/not-None when the second turn is sized.
        await llm.chat_completion([{"role": "user", "content": "first"}], max_tokens=8000)
        assert context._last_count_exact.get() is True
        await llm.chat_completion([{"role": "user", "content": "second"}], max_tokens=8000)
        return seen, context._last_count_exact.get()

    with pytest.MonkeyPatch.context() as mp:
        at_dispatch, after = asyncio.run(run(mp))
    assert at_dispatch["exact"] is False, (
        "a send-first turn carried the previous turn's exactness into its own dispatch"
    )
    assert at_dispatch["measured"] is None, (
        "a send-first turn carried the previous turn's count into its own dispatch"
    )
    assert after is True, "the settlement did not write the exact count back"


def test_the_count_task_reports_its_own_exactness_not_the_callers():
    """`_count_beside_the_send` runs in a COPY of the caller's context, so the
    verdict has to be RETURNED. It sets `_last_count_exact` False first: a
    failed count inside the task must not report the caller's stale True."""

    async def run(monkeypatch):
        counter = _Tokenize(count=40, window=1_000_000, fail=True)
        _tokenize(monkeypatch, counter)
        context._last_count_exact.set(True)
        count, window, exact = await context._count_beside_the_send(MAIN_URL, "m",
                                                                   [{"role": "user", "content": "hi"}])
        return count, window, exact

    with pytest.MonkeyPatch.context() as mp:
        count, window, exact = asyncio.run(run(mp))
    assert exact is False, "a failed count reported the caller's stale exactness"
    assert count > 0 and window is None


def test_a_failed_count_is_retrieved_so_the_loop_never_logs_it():
    """`_swallow_count_result` retrieves the exception of a count nobody
    awaits. Without it the loop logs "Task exception was never retrieved" for
    every abandoned count — noise that hides real ones."""

    async def run():
        async def boom():
            raise RuntimeError("count broke")

        task = asyncio.get_running_loop().create_task(boom())
        context._INFLIGHT_COUNTS.add(task)
        task.add_done_callback(context._swallow_count_result)
        for _ in range(10):
            if task.done():
                break
            await asyncio.sleep(0)
        # The done callback is scheduled with `call_soon`, so it has not run
        # yet at the moment the task finishes.
        for _ in range(10):
            await asyncio.sleep(0)
        # NOT awaited: awaiting would retrieve the exception itself and prove
        # nothing. `_log_traceback` is the flag `Task.__del__` reads before it
        # complains, and it is cleared only by a retrieval.
        assert hasattr(task, "_log_traceback"), "this Python names the flag differently"
        return task.done(), task in context._INFLIGHT_COUNTS, task._log_traceback

    done, still_registered, will_log = asyncio.run(run())
    assert done and not still_registered, "the finished count stayed in the registry"
    assert will_log is False, "an abandoned count will be logged by the event loop"


def test_the_fast_path_cancels_a_count_it_is_about_to_replace():
    """One pending count per context, by construction. Nothing in llm.py
    awaits between a fit and its send, so no caller can reach here with a
    count already pending — the guard is what makes that structural instead
    of a promise every future caller has to keep."""

    async def run(monkeypatch):
        counter = _Tokenize(count=40, window=1_000_000, gate=asyncio.Event())
        _tokenize(monkeypatch, counter)
        _warm(counter)
        with context.settles_pending_count():
            await context.fit_request(
                [{"role": "user", "content": "first"}],
                base_url=MAIN_URL, model="m", requested_max_tokens=8000,
            )
            stale = context._pending_count.get().task
            await context.fit_request(
                [{"role": "user", "content": "second"}],
                base_url=MAIN_URL, model="m", requested_max_tokens=8000,
            )
        fresh = context._pending_count.get().task
        for _ in range(20):
            await asyncio.sleep(0)
        cancelled = stale.cancelled()
        context.cancel_pending_count()
        for _ in range(20):
            await asyncio.sleep(0)
        return stale is not fresh, cancelled, _still_counting()

    with pytest.MonkeyPatch.context() as mp:
        replaced, cancelled, left = asyncio.run(run(mp))
    assert replaced, "the second fit reused the first fit's count"
    assert cancelled, "the count the second fit replaced was left running"
    assert left == [], "a count outlived the request that started it"


def test_a_broken_count_beside_the_send_is_logged_not_swallowed(caplog):
    """`count_tokens` swallows its own failures, so `settle_pending_count`'s
    `except Exception` can only fire on a genuine bug in
    `_count_beside_the_send`. Returning silently left `_measured` None and
    `_last_count_exact` False for the rest of the turn with nothing to notice
    it by."""

    async def run(monkeypatch):
        counter = _Tokenize(count=4242, window=1_000_000)
        engine = _Engine()
        _tokenize(monkeypatch, counter)
        _engine(monkeypatch, engine)
        _warm(counter)

        async def broken(base_url, model, msgs):
            raise RuntimeError("the count task itself broke")

        monkeypatch.setattr(context, "_count_beside_the_send", broken)
        await llm.chat_completion([{"role": "user", "content": "hello"}], max_tokens=8000)
        sized = engine.calls[-1]["messages"]
        return context.measured_prompt_tokens(sized, MAIN_URL), _still_counting()

    with caplog.at_level("WARNING", logger="app.context"):
        with pytest.MonkeyPatch.context() as mp:
            measured, left = asyncio.run(run(mp))
    assert measured is None, "a turn whose count broke reported a number anyway"
    assert left == []
    lines = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("count running beside the send failed" in line for line in lines), lines
