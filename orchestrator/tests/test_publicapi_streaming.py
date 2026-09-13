"""The public API's execution path: CONTRACT §10 (the stream) and §11 (how it
reaches the engine).

NO REAL MODEL IS CALLED. `llm.stream_chat_events` is stubbed in every test —
these assertions are about what the orchestrator does AROUND a generation, and
a test that needed a GPU would be a test nobody runs before pushing.

The properties pinned here are the ones whose failure is invisible in
production until it is expensive:

  * the engine's generator is CLOSED even when the caller walks away, because
    an abandoned one holds an admission lane shared with the chat app;
  * usage is reset and read INSIDE the generating task, because it lives in a
    ContextVar and a task holds its own copy of the context — read from the
    outside it is always None, and None rendered as 0 is an under-charge that
    looks like a working meter;
  * exactly one terminal event, whatever went wrong and whenever;
  * a silent generation still heartbeats, so an idle proxy cannot mistake a
    thinking model for a dead socket;
  * an engine that is away is the documented 503 with a `Retry-After`, and the
    retry-safe 503 is told apart from the other one.

This suite has no pytest-asyncio, so it follows the house convention
(tests/test_continuity.py, tests/test_admission.py): a synchronous test that
runs one scenario on its own loop with `asyncio.run`. A failure's traceback is
then the scenario's own rather than a plugin's.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from app import llm
from app.publicapi import events, streaming

_run = asyncio.run


# --------------------------------------------------------------- helpers --


def _spec(**overrides) -> streaming.GenerationSpec:
    base: Dict[str, Any] = dict(
        response_id="resp_test",
        model="techsara-35b",
        messages=[{"role": "user", "content": "Why is the sky blue?"}],
        max_tokens=64,
        temperature=0.2,
        created_at=1789200000,
        item_id="msg_fixed",
    )
    base.update(overrides)
    return streaming.GenerationSpec(**base)


class _FakeEngine:
    """A stand-in for `llm.stream_chat_events` that remembers how it was used.

    It records the arguments it was called with and whether its generator was
    closed — which is how CONTRACT §10's `finally: await stream.aclose()` is
    asserted without reaching into `llm`'s internals.
    """

    def __init__(
        self,
        pieces: List[Any],
        *,
        fail: Optional[BaseException] = None,
        delay: float = 0.0,
        hang: bool = False,
    ) -> None:
        self.pieces = [p if isinstance(p, tuple) else ("token", p) for p in pieces]
        self.fail = fail
        self.delay = delay
        self.hang = hang
        self.closed = False
        self.generator: Any = None
        self.kwargs: Dict[str, Any] = {}
        self.messages: Any = None

    def __call__(self, messages, **kwargs):
        self.messages = messages
        self.kwargs = kwargs
        # A STRONG REFERENCE is kept on purpose. Without one, dropping the last
        # reference to an async generator lets CPython's refcounting finalize
        # it — the loop's async-generator hook runs `aclose()` for us — and the
        # disconnect tests below then pass even with the `finally:
        # await stream.aclose()` deleted, which is precisely the regression
        # they exist to catch (observed while mutation-checking, 2026-09-13).
        self.generator = self._run()
        return self.generator

    async def _run(self):
        try:
            if self.hang:
                await asyncio.sleep(3600)
            for kind, piece in self.pieces:
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield (kind, piece)
            if self.fail is not None:
                raise self.fail
        finally:
            self.closed = True


@pytest.fixture()
def engine(monkeypatch):
    """Install a fake engine; the test decides what it produces."""

    def install(fake: _FakeEngine) -> _FakeEngine:
        monkeypatch.setattr(llm, "stream_chat_events", fake)
        return fake

    return install


@pytest.fixture()
def measured(monkeypatch):
    """Make `llm.get_usage()` answer a fixed value, and record WHICH TASK the
    two ContextVar calls happened in — that is the property, not the number."""
    seen: Dict[str, Any] = {"reset_in": None, "read_in": None}

    def install(value: Optional[Dict[str, int]]):
        def reset() -> None:
            seen["reset_in"] = id(asyncio.current_task())

        def read() -> Optional[Dict[str, int]]:
            seen["read_in"] = id(asyncio.current_task())
            return value

        monkeypatch.setattr(llm, "reset_usage", reset)
        monkeypatch.setattr(llm, "get_usage", read)
        return seen

    return install


async def _drain(frames) -> str:
    return "".join([frame async for frame in frames])


def _frames(spec=None, **kwargs) -> str:
    return _run(_drain(streaming.responses_sse(spec or _spec(), **kwargs)))


# ------------------------------------------------------- §10 lifecycle --


def test_the_stream_follows_the_documented_lifecycle_with_sequence_numbers(
    engine, measured
):
    measured({"prompt_tokens": 37, "completion_tokens": 112})
    engine(_FakeEngine(["Because ", "of ", "Rayleigh scattering."]))

    records = events.parse_frames(_frames())

    assert [r["event"] for r in records] == [
        "response.created",
        "response.in_progress",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.completed",
    ]
    # Starts at 1, increases by exactly 1 — a client detects a dropped or
    # reordered frame with this, and cannot if we ever skip.
    assert [r["data"]["sequence_number"] for r in records] == [1, 2, 3, 4, 5, 6, 7]
    assert [r["data"]["type"] for r in records] == [r["event"] for r in records]
    assert records[-2]["data"]["text"] == "Because of Rayleigh scattering."
    # Usage on the terminal event only, so a client cannot be tempted to sum
    # deltas into a bill.
    assert records[0]["data"]["response"]["usage"] is None
    assert records[-1]["data"]["response"]["usage"] == {
        "input_tokens": 37,
        "output_tokens": 112,
        "total_tokens": 149,
    }
    assert records[-1]["data"]["response"]["status"] == "completed"


def test_a_heartbeat_goes_out_while_the_engine_is_silent(engine, measured):
    measured(None)
    engine(_FakeEngine(["done"], delay=0.2))

    wire = _frames(heartbeat_s=0.02)

    assert ": ping\n\n" in wire
    records = events.parse_frames(wire)
    # The comment consumed no sequence number: numbering a keep-alive would
    # make a client believe it had missed an event.
    assert [r["data"]["sequence_number"] for r in records] == list(
        range(1, len(records) + 1)
    )
    # And the promise is a ceiling, not an inheritance: an operator may set the
    # chat app's interval higher, the public one is capped.
    assert events.HEARTBEAT_SECONDS <= 15.0


def test_usage_that_was_not_measured_is_null_and_never_zero(engine, measured):
    measured(None)
    engine(_FakeEngine(["hi"]))

    records = events.parse_frames(_frames())

    assert records[-1]["event"] == "response.completed"
    # CONTRACT §9: null, never 0. A zero here is both a lie and an under-charge.
    assert records[-1]["data"]["response"]["usage"] is None


def test_usage_is_reset_and_read_inside_the_generating_task(engine, measured):
    seen = measured({"prompt_tokens": 1, "completion_tokens": 2})
    engine(_FakeEngine(["x"]))
    outer: Dict[str, Any] = {}

    async def scenario():
        outer["task"] = id(asyncio.current_task())
        return await _drain(streaming.responses_sse(_spec()))

    _run(scenario())

    # Both calls happened in the SAME task, and it was not the one consuming
    # the stream. `_usage` is a ContextVar and a task gets a COPY of the
    # context it was created in: resetting out there would clear the caller's
    # variable and leave the generating task's untouched, so `get_usage()`
    # would always answer None and every response would bill as unmetered.
    assert seen["reset_in"] is not None
    assert seen["reset_in"] == seen["read_in"]
    assert seen["reset_in"] != outer["task"]


def test_exactly_one_terminal_event_is_emitted_when_the_engine_dies_midway(
    engine, measured
):
    measured({"prompt_tokens": 5, "completion_tokens": 2})
    engine(_FakeEngine(["half an "], fail=RuntimeError("engine went away")))

    records = events.parse_frames(_frames())

    terminals = [r for r in records if r["event"] in events.TERMINAL_EVENTS]
    assert len(terminals) == 1
    assert terminals[0]["event"] == "response.failed"
    failed = terminals[0]["data"]["response"]
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "internal_error"
    # Partial usage belongs on the failure terminal: the tokens were produced
    # and the engine time was spent.
    assert failed["usage"]["total_tokens"] == 7


def test_a_raised_exception_never_puts_its_text_on_the_wire(engine, measured):
    measured(None)
    engine(
        _FakeEngine(
            [],
            fail=RuntimeError(
                'connection to server at "vllm-router" (172.18.0.4), port 30002 '
                "failed; see /app/app/llm.py"
            ),
        )
    )

    wire = _frames()

    # CONTRACT §9: no traceback, no container name, no private IP, no path.
    # `errors.from_unexpected` DISCARDS the text rather than filtering it,
    # which is the only safe treatment of text nobody wrote for a caller.
    for forbidden in ("vllm-router", "172.18.0.4", "/app/", "30002"):
        assert forbidden not in wire
    assert "Something went wrong on our side." in wire


def test_a_reasoning_delta_is_never_streamed_as_answer_text(engine, measured):
    measured(None)
    engine(_FakeEngine([("reasoning", "the user wants a colour"), ("token", "Blue.")]))

    records = events.parse_frames(_frames())
    deltas = [r for r in records if r["event"] == "response.output_text.delta"]

    assert [d["data"]["delta"] for d in deltas] == ["Blue."]
    assert "the user wants a colour" not in str(records)


# --------------------------------------------------- the abandoned client --


def test_a_client_disconnect_closes_the_engine_generator(engine, measured):
    measured(None)
    fake = engine(_FakeEngine(["a", "b", "c", "d"], delay=0.02))

    async def scenario():
        frames = streaming.responses_sse(_spec())
        # Read as far as the FIRST DELTA — the generation is now genuinely
        # running, which is the only state in which "was it closed?" is a
        # question worth asking — and then walk away, exactly as a browser tab
        # that navigates away does to a StreamingResponse.
        assert "response.created" in await frames.__anext__()
        assert "response.in_progress" in await frames.__anext__()
        assert "response.output_text.delta" in await frames.__anext__()
        await frames.aclose()
        await asyncio.sleep(0.05)
        # Read INSIDE the loop: `asyncio.run()` closes every outstanding async
        # generator on its way out, so the same assertion afterwards would be
        # satisfied by the interpreter rather than by our code.
        return fake.closed

    # Without `finally: await stream.aclose()` this is False, and the
    # generation holds an admission lane and an open upstream response until
    # garbage collection.
    assert _run(scenario()) is True


def test_an_abandoned_request_does_not_leave_the_generation_running(
    engine, measured, monkeypatch
):
    """The property the admission lane depends on, tested without relying on GC.

    The two tests around this one assert the OUTCOME — the engine generator was
    closed — and CPython's refcounting reaches that outcome on its own as soon
    as the last reference to an async generator goes away, which makes them
    blind to the mechanism. A real server is not so tidy: Starlette holds the
    response body iterator, and a generation that nobody explicitly stopped
    keeps decoding into a queue no one reads while holding one of the ten
    NORMAL admission lanes shared with the chat application.

    So this test KEEPS A REFERENCE to the pump, defeating refcounting, and
    asserts the thing only `finally: await generation.aclose()` can deliver:
    the generating task is finished the moment the caller has gone.
    """
    measured(None)
    engine(_FakeEngine(["a", "b", "c", "d"], delay=0.02))
    built: List[streaming.Generation] = []

    class _Recording(streaming.Generation):
        def stream(self):
            pump = super().stream()
            self._kept = pump  # the reference GC would otherwise have collected
            built.append(self)
            return pump

    monkeypatch.setattr(streaming, "Generation", _Recording)

    async def scenario():
        frames = streaming.responses_sse(_spec())
        await frames.__anext__()
        await frames.__anext__()
        await frames.__anext__()
        await frames.aclose()
        await asyncio.sleep(0.05)
        return built[0]._task.done()

    assert _run(scenario()) is True


def test_an_abandoned_stream_is_still_recorded(engine, measured):
    measured({"prompt_tokens": 9, "completion_tokens": 1})
    engine(_FakeEngine(["a", "b", "c"], delay=0.02))
    recorded: List[streaming.StreamOutcome] = []

    async def on_finish(outcome: streaming.StreamOutcome) -> None:
        recorded.append(outcome)

    async def scenario():
        frames = streaming.responses_sse(_spec(), on_finish=on_finish)
        await frames.__anext__()  # response.created
        await frames.__anext__()  # response.in_progress
        await frames.__anext__()  # the first delta: the answer has started
        await frames.aclose()
        await asyncio.sleep(0.05)

    _run(scenario())

    # Metering comes from the SERVER-side record, not from what the client
    # received: billing from the wire under-counts every disconnect.
    assert len(recorded) == 1
    assert recorded[0].client_gone is True


def test_a_hanging_engine_does_not_outlive_the_request(engine, measured):
    measured(None)
    fake = engine(_FakeEngine([], hang=True))

    async def scenario():
        frames = streaming.responses_sse(_spec(), heartbeat_s=0.02)
        await frames.__anext__()  # response.created
        await frames.__anext__()  # response.in_progress
        # The engine yields nothing at all, so the next frame out is the
        # keep-alive comment — proof the generation has started and is silent.
        assert (await frames.__anext__()).startswith(":")
        await frames.aclose()
        await asyncio.sleep(0.05)
        return fake.closed

    assert _run(scenario()) is True


# --------------------------------------------------- §11 the engine call --


def test_the_engine_is_called_through_stream_chat_events_and_nothing_lower(
    engine, measured
):
    measured(None)
    fake = engine(_FakeEngine(["ok"]))

    _frames(
        _spec(
            max_tokens=123,
            temperature=0.7,
            messages=[{"role": "user", "content": "hi"}],
        )
    )

    # CONTRACT §11 names the call and its arguments. `model_choice="smart"` is
    # the main model — the only engine allowed to answer anyone.
    assert fake.kwargs["model_choice"] == "smart"
    assert fake.kwargs["max_tokens"] == 123
    assert fake.kwargs["temperature"] == 0.7
    assert fake.messages == [{"role": "user", "content": "hi"}]


def test_the_public_surface_never_asks_the_model_to_think(engine, measured):
    measured(None)
    fake = engine(_FakeEngine(["ok"]))

    _frames(_spec(max_tokens=50))

    # Thinking off, and the reason is BILLING rather than latency: llm.py
    # sizes a thinking call as `max(max_tokens, MAX_OUTPUT_TOKENS)` so the
    # answer still has room after the reasoning pass, which on this surface
    # would silently overrun the ceiling the caller asked for.
    assert llm.wants_thinking("smart", fake.kwargs["effort"]) is False


# ------------------------------------------------- the recovering engine --


class _QueuedForRecovery(RuntimeError):
    """Stands in for app/continuity.py's QueuedForRecovery.

    Matched by NAME rather than by class, which is what `streaming.engine_error`
    does on purpose: importing continuity, breaker and admission into this
    package would drag the controller client and the lanes into the CI lint job
    that builds the OpenAPI document.
    """


class _BreakerOpen(RuntimeError):
    """Stands in for app/breaker.py's BreakerOpen."""


class _AdmissionRejected(RuntimeError):
    """Stands in for app/admission.py's AdmissionRejected."""


_QueuedForRecovery.__name__ = "QueuedForRecovery"
_BreakerOpen.__name__ = "BreakerOpen"
_AdmissionRejected.__name__ = "AdmissionRejected"


def test_a_recovering_engine_is_a_503_model_recovering_with_retry_after(
    engine, measured, monkeypatch
):
    measured(None)
    monkeypatch.setattr(streaming, "_engine_state_name", lambda: "RECOVERING")
    engine(_FakeEngine([], fail=_QueuedForRecovery("still recovering")))

    outcome = _run(streaming.run_to_completion(_spec()))

    assert outcome.status == "failed"
    assert outcome.error is not None
    assert outcome.error.code == "model_recovering"
    assert outcome.error.status == 503
    # CONTRACT §9: Retry-After on every 429 and 503, an integer of at least 1.
    assert outcome.error.headers()["Retry-After"] == str(
        int(streaming.RECOVERING_RETRY_AFTER)
    )
    assert outcome.error.retryable is True


def test_a_wedged_engine_is_model_unavailable_rather_than_recovering(
    engine, measured, monkeypatch
):
    measured(None)
    monkeypatch.setattr(streaming, "_engine_state_name", lambda: "WEDGED")
    engine(_FakeEngine([], fail=_BreakerOpen("breaker for engine main is OPEN")))

    outcome = _run(streaming.run_to_completion(_spec()))

    # The two 503s are separate codes because one of them is retry-safe and the
    # other is not; the controller's verdict is what tells them apart.
    assert outcome.error is not None
    assert outcome.error.code == "model_unavailable"


def test_a_refused_admission_lane_is_a_429_not_a_500(engine, measured):
    measured(None)
    engine(_FakeEngine([], fail=_AdmissionRejected("NORMAL lane refused the request")))

    outcome = _run(streaming.run_to_completion(_spec()))

    assert outcome.error is not None
    assert outcome.error.code == "concurrency_limit_exceeded"
    assert outcome.error.status == 429
    assert "Retry-After" in outcome.error.headers()


def test_the_queued_event_announces_a_recovering_engine(engine, measured, monkeypatch):
    measured(None)
    monkeypatch.setattr(streaming, "_engine_state_name", lambda: "RECOVERING")
    engine(_FakeEngine(["late but here"]))

    records = events.parse_frames(_frames())

    assert [r["event"] for r in records][:3] == [
        "response.created",
        "response.queued",
        "response.in_progress",
    ]


# ----------------------------------------- the compatibility dialect --


def _chat(spec=None, **kwargs) -> str:
    return _run(
        _drain(
            streaming.chat_completions_sse(
                spec or _spec(), completion_id="chatcmpl_x", **kwargs
            )
        )
    )


def test_the_chat_completions_stream_is_anonymous_and_ends_with_done(engine, measured):
    measured({"prompt_tokens": 3, "completion_tokens": 4})
    engine(_FakeEngine(["Hel", "lo"]))

    wire = _chat()

    assert "event:" not in wire  # anonymous chunks, no event names
    assert wire.endswith(events.DONE_SENTINEL)
    # No usage chunk unless the caller asked for one: a client that did not
    # request it does not expect a chunk with no choices, and several crash.
    assert '"prompt_tokens"' not in wire


def test_the_chat_completions_usage_chunk_appears_only_when_asked(engine, measured):
    measured({"prompt_tokens": 3, "completion_tokens": 4})
    engine(_FakeEngine(["Hi"]))

    wire = _chat(include_usage=True)

    assert '"prompt_tokens": 3' in wire
    assert wire.endswith(events.DONE_SENTINEL)


def test_the_compatibility_stream_reports_a_failure_in_the_one_envelope(
    engine, measured
):
    measured(None)
    engine(_FakeEngine(["part"], fail=RuntimeError("gone")))

    wire = _chat()

    # CONTRACT §9: the same envelope everywhere, including mid-stream — and the
    # sentinel still arrives, because a client library waits for it and hangs
    # without it.
    assert '"code": "internal_error"' in wire
    assert wire.endswith(events.DONE_SENTINEL)


# ------------------------------------------------------ the sync path --


def test_the_synchronous_path_returns_the_contract_response_object(engine, measured):
    measured({"prompt_tokens": 37, "completion_tokens": 112})
    engine(_FakeEngine(["Because ", "physics."]))

    outcome = _run(streaming.run_to_completion(_spec()))
    wire = outcome.response().to_wire()

    assert wire["object"] == "response"
    assert wire["status"] == "completed"
    assert wire["output"] == [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Because physics."}],
        }
    ]
    assert wire["usage"] == {
        "input_tokens": 37,
        "output_tokens": 112,
        "total_tokens": 149,
    }
    assert "error" not in wire
    assert outcome.duration_ms is not None and outcome.ttft_ms is not None


def test_the_synchronous_path_closes_the_generator_too(engine, measured):
    measured(None)
    fake = engine(_FakeEngine(["a", "b"]))

    async def scenario():
        await streaming.run_to_completion(_spec())
        return fake.closed

    assert _run(scenario()) is True


# ------------------------------------------------------ finish_reason --


class _TruncatingEngine(_FakeEngine):
    """Reports `length` the way `llm.py` does: by setting the ContextVar from
    inside the generating task, after the last token."""

    async def _run(self):
        try:
            for kind, piece in self.pieces:
                yield (kind, piece)
            llm._set_finish_reason("length")
        finally:
            self.closed = True


def test_a_truncated_answer_says_length_in_the_compatibility_stream(engine, measured):
    """`_finish_reason()` used to read `llm.get_finish_reason()` from the
    CONSUMER task, where the ContextVar the producer set is invisible — so
    every truncated answer went out as `stop` (verifier proof, 2026-09-13).
    The producer now captures it next to `usage`."""
    measured(None)
    engine(_TruncatingEngine(["half an ans"]))

    wire = _chat()

    assert '"finish_reason": "length"' in wire
    assert '"finish_reason": "stop"' not in wire


def test_the_synchronous_outcome_carries_the_engines_finish_reason(engine, measured):
    measured(None)
    engine(_TruncatingEngine(["half"]))

    outcome = _run(streaming.run_to_completion(_spec()))

    assert outcome.finish_reason == "length"
    assert outcome.chat_finish_reason() == "length"


def test_an_unreported_finish_reason_is_stop_and_never_invented(engine, measured):
    measured(None)
    engine(_FakeEngine(["whole answer"]))

    outcome = _run(streaming.run_to_completion(_spec()))

    assert outcome.finish_reason is None
    assert outcome.chat_finish_reason() == "stop"
