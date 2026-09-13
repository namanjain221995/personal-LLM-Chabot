"""`llm.stream_chat_events(wall_clock_s=…, wall_clock_marker=…)` and
`get_applied_max_tokens()` — the llm.py half of the one-million-token output
on `/v1` (integration, 2026-09-13).

Before this, every techsara-35b generation stopped at GEN_WALL_CLOCK_S
(4,200 s on this deployment) whatever `/v1` planned for it: ~300-420k tokens.
What is pinned here:

* a per-call clock replaces GEN_WALL_CLOCK_S for that call only, in both
  directions, and a nonsense value never removes the guard;
* a stubbed generation planned for five hours runs past 4,200 s and is cut at
  ITS clock — driven through `publicapi.streaming` exactly as `/v1` drives it,
  on an injected clock (`llm._generation_clock`), in milliseconds;
* `wall_clock_marker=False` keeps the "[generation stopped …]" sentence out of
  the answer while the finish reason still says WALL_CLOCK_FINISH;
* THE TRANSPORT INVARIANT: the HTTP read timeout is never shorter than the
  clock the call runs to — raised per request, never for chat-app calls;
* the applied `max_tokens` is the value `_fit` produced (or the forced-closure
  retry's), cleared at the start of every call.
"""
from __future__ import annotations

import asyncio
import inspect
import math
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from app import llm
from app.config import settings
from app.publicapi import capacity, models, planning, registry, streaming

_run = asyncio.run


class FakeClock:
    """What `llm._generation_clock` returns: seconds that pass only when the
    fake engine says a chunk took them."""

    def __init__(self) -> None:
        self.now = 1_000.0


def _chunk(content=None, reasoning=None, finish_reason=None):
    delta = SimpleNamespace(content=content, reasoning=reasoning, model_extra=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)], usage=None
    )


class TimedStream:
    """An engine stream where every chunk costs `step_s` of the fake clock."""

    def __init__(self, chunks, clock: FakeClock, step_s: float) -> None:
        self._chunks = list(chunks)
        self._clock = clock
        self._step = step_s
        self.closed = False
        self.delivered = 0

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        try:
            chunk = next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None
        self._clock.now += self._step
        self.delivered += 1
        await asyncio.sleep(0)
        return chunk

    async def close(self):
        self.closed = True


class Completions:
    def __init__(self, streams) -> None:
        self.requests: List[Dict[str, Any]] = []
        self._streams = list(streams)

    async def create(self, **request):
        self.requests.append(request)
        return self._streams.pop(0)


@pytest.fixture()
def engine(monkeypatch):
    """The main-engine path of llm.py with only the network replaced:
    `_fit` answers `budget` (what /tokenize-based sizing would), the client is
    a fake, and the guard reads a fake clock."""
    clock = FakeClock()
    state: Dict[str, Any] = {"clock": clock, "budget": None, "fit_calls": []}

    async def fit(messages, *, base_url, model, requested_max_tokens=None):
        state["fit_calls"].append(requested_max_tokens)
        if state.get("fit_raises"):
            raise RuntimeError("tokenize unavailable")
        budget = state["budget"] if state["budget"] is not None else requested_max_tokens or 8192
        return list(messages), budget

    monkeypatch.setattr(llm.context, "fit_request", fit)
    monkeypatch.setattr(llm, "_generation_clock", lambda: clock.now)
    monkeypatch.setattr(llm, "_client", lambda base_url, api_key=None, **_: state["client"])
    # The production shape: GEN_WALL_CLOCK_S=4200 and LLM_REQUEST_TIMEOUT
    # defaulting to it.
    monkeypatch.setattr(settings, "gen_wall_clock_s", 4200.0)
    monkeypatch.setattr(settings, "llm_request_timeout", 4200.0)
    monkeypatch.setattr(settings, "model_max_context", 1_000_000)
    monkeypatch.setattr(settings, "context_safety_margin", 512)
    for name in (
        "PUBLIC_API_MAX_OUTPUT_TOKENS",
        "PUBLIC_API_GEN_WALL_CLOCK_S",
        "PUBLIC_API_MAIN_PREFILL_ALLOWANCE_S",
        "PUBLIC_API_MAIN_MIN_DECODE_TOKENS_PER_S",
    ):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delattr(settings, name.lower(), raising=False)
    capacity.reset_for_tests()

    def install(*streams):
        completions = Completions(streams)
        state["client"] = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        state["completions"] = completions
        return completions

    state["install"] = install
    return state


async def _collect(gen):
    return [item async for item in gen]


def _tokens(count: int, finish: str = "stop"):
    chunks = [_chunk(content=f"t{i} ") for i in range(count)]
    chunks.append(_chunk(finish_reason=finish))
    return chunks


# ----------------------------------------------------------- the signature --


def test_the_signature_is_what_publicapi_feature_detects_and_the_liveness_answer_flips():
    parameters = inspect.signature(llm.stream_chat_events).parameters
    assert parameters["wall_clock_s"].default is None
    assert parameters["wall_clock_marker"].default is True
    assert parameters["wall_clock_s"].kind is inspect.Parameter.KEYWORD_ONLY
    assert streaming._accepted_keywords(
        llm.stream_chat_events, ("wall_clock_s", "wall_clock_marker")
    ) == frozenset({"wall_clock_s", "wall_clock_marker"})
    assert registry.per_request_wall_clock_live() is True
    assert registry._source_accepts_wall_clock() is True
    assert callable(llm.get_applied_max_tokens) and callable(llm.reset_applied_max_tokens)


# ---------------------------------------------- the five-hour generation --


def _five_hour_spec() -> streaming.GenerationSpec:
    request = models.parse_responses_request(
        {"model": "techsara-35b", "input": "Write the long report.", "max_output_tokens": 855_000}
    )
    plan = planning.plan_generation(request, registry.resolve_public_model("techsara-35b"))
    return streaming.spec_from_plan(plan, response_id="resp_fivehours", created_at=1789200000)


def test_a_generation_planned_for_five_hours_is_not_cut_at_4200_s_and_completes(engine):
    spec = _five_hour_spec()
    # 900 s prefill allowance + 855,000 tokens / 50 tok/s = 18,000 s: five hours.
    assert spec.wall_clock_s == 18_000.0
    engine["budget"] = 855_000
    # 35 chunks of 500 s each: the answer ends at 17,500 s of engine time,
    # four times the chat application's 4,200 s clock.
    stream = TimedStream(_tokens(35), engine["clock"], step_s=500.0)
    completions = engine["install"](stream)

    outcome = _run(streaming.run_to_completion(spec, heartbeat_s=0.5))

    assert outcome.error is None and outcome.status == "completed"
    assert outcome.text == "".join(f"t{i} " for i in range(35))
    assert "wall-clock guard" not in outcome.text
    assert outcome.finish_reason == "stop"
    assert stream.delivered == 36  # every chunk, the finish chunk included
    # Sent with its own transport timeout: the read timeout is the call's own
    # clock, never the 4,200 s LLM_REQUEST_TIMEOUT that would give up first.
    sent = completions.requests[0]
    assert sent["timeout"].read == 18_000.0
    assert sent["timeout"].connect == settings.llm_connect_timeout
    assert sent["max_tokens"] == 855_000
    # The ceiling reported is the one llm.py applied, not a re-derivation.
    assert outcome.max_output_tokens == 855_000


def test_the_same_generation_is_still_cut_at_its_own_clock_and_fails_as_timeout_without_the_marker(engine):
    spec = _five_hour_spec()
    engine["budget"] = 855_000
    stream = TimedStream(_tokens(60), engine["clock"], step_s=500.0)
    engine["install"](stream)

    outcome = _run(streaming.run_to_completion(spec, heartbeat_s=0.5))

    # 36 x 500 = 18,000 s is not past the clock; the 37th chunk (18,500 s) is.
    assert outcome.text == "".join(f"t{i} " for i in range(36))
    assert "generation stopped" not in outcome.text
    assert outcome.status == "failed" and outcome.error.code == "timeout"
    assert "18000 second" in outcome.error.message
    assert outcome.finish_reason == llm.WALL_CLOCK_FINISH
    assert stream.closed is True
    assert outcome.usage["completion_tokens"] == 36
    assert outcome.usage["source"] == streaming.USAGE_COUNTED_AT_STOP


# --------------------------------------------------- the llm.py contract --


def test_a_chat_app_call_keeps_gen_wall_clock_s_the_marker_and_the_shared_transport(engine, caplog):
    stream = TimedStream(_tokens(20), engine["clock"], step_s=500.0)
    completions = engine["install"](stream)

    async def scenario():
        # Read inside the loop: asyncio.run gives the coroutine a copy of the
        # context, so the ContextVar is not visible out here afterwards.
        events = await _collect(llm.stream_chat_events(
            [{"role": "user", "content": "q"}], effort="fast", max_tokens=8192,
        ))
        return events, llm.get_finish_reason()

    with caplog.at_level("ERROR"):
        events, finish = _run(scenario())

    text = "".join(delta for kind, delta in events if kind == "token")
    # 8 x 500 = 4,000 s passes; the 9th chunk at 4,500 s is past 4,200 s.
    assert text.startswith("".join(f"t{i} " for i in range(8)))
    assert "t8 " not in text
    assert "wall-clock guard" in text  # the chat app still gets its sentence
    assert finish == llm.WALL_CLOCK_FINISH
    assert "timeout" not in completions.requests[0]
    assert any("4200s" in record.getMessage() for record in caplog.records)


def test_wall_clock_marker_false_stops_the_sentence_but_not_the_finish_reason(engine):
    engine["install"](TimedStream(_tokens(20), engine["clock"], step_s=500.0))

    async def scenario():
        events = await _collect(llm.stream_chat_events(
            [{"role": "user", "content": "q"}], effort="fast", max_tokens=8192,
            wall_clock_s=1_000.0, wall_clock_marker=False,
        ))
        return events, llm.get_finish_reason()

    events, finish = _run(scenario())
    assert [delta for _, delta in events] == ["t0 ", "t1 "]
    assert finish == llm.WALL_CLOCK_FINISH


def test_a_shorter_per_call_clock_also_wins_and_needs_no_transport_override(engine):
    completions = engine["install"](TimedStream(_tokens(20), engine["clock"], step_s=500.0))

    events = _run(_collect(llm.stream_chat_events(
        [{"role": "user", "content": "q"}], effort="fast", max_tokens=100, wall_clock_s=1_200.0,
    )))
    assert [delta for kind, delta in events if kind == "token"][:2] == ["t0 ", "t1 "]
    assert "t2 " not in "".join(delta for _, delta in events)
    assert "timeout" not in completions.requests[0]


@pytest.mark.parametrize("bad", [0, -5, math.nan, math.inf, "soon"])
def test_a_nonsense_per_call_clock_never_removes_the_guard(engine, bad):
    engine["install"](TimedStream(_tokens(20), engine["clock"], step_s=500.0))
    events = _run(_collect(llm.stream_chat_events(
        [{"role": "user", "content": "q"}], effort="fast", max_tokens=100, wall_clock_s=bad,
    )))
    text = "".join(delta for _, delta in events)
    assert "t8 " not in text and "wall-clock guard" in text


def test_the_transport_timeout_is_raised_only_past_llm_request_timeout(monkeypatch):
    monkeypatch.setattr(settings, "llm_request_timeout", 4200.0)
    assert llm._transport_timeout_for(4200.0) is None
    assert llm._transport_timeout_for(600.0) is None
    raised = llm._transport_timeout_for(20_900.0)
    assert raised.read == 20_900.0
    assert raised.write == settings.llm_write_timeout
    assert raised.pool == settings.llm_write_timeout
    # An operator who set LLM_REQUEST_TIMEOUT above the public ceiling gets it.
    monkeypatch.setattr(settings, "llm_request_timeout", 30_000.0)
    assert llm._transport_timeout_for(21_600.0) is None


def test_the_applied_max_tokens_is_the_fitted_budget_and_is_cleared_by_the_next_call(engine):
    engine["budget"] = 998_720
    engine["install"](TimedStream(_tokens(2), engine["clock"], step_s=1.0))

    async def scenario():
        await _collect(llm.stream_chat_events(
            [{"role": "user", "content": "q"}], effort="fast", max_tokens=1_000_000,
        ))
        after_success = llm.get_applied_max_tokens()
        engine["fit_raises"] = True
        with pytest.raises(RuntimeError):
            await _collect(llm.stream_chat_events(
                [{"role": "user", "content": "q"}], effort="fast", max_tokens=1_000_000,
            ))
        return after_success, llm.get_applied_max_tokens()

    after_success, after_failure = _run(scenario())
    assert after_success == 998_720
    assert after_failure is None


def test_the_forced_closure_retry_reports_its_own_ceiling(engine, monkeypatch):
    monkeypatch.setattr(settings, "thinking_budget_mode", "client")
    monkeypatch.setattr(settings, "thinking_budget_high", 10)
    monkeypatch.setattr(settings, "thinking_budget_grace", 1.0)
    engine["budget"] = 4_000
    looping = TimedStream([_chunk(reasoning="r")] * 50, engine["clock"], step_s=1.0)
    answer = TimedStream(_tokens(1), engine["clock"], step_s=1.0)
    completions = engine["install"](looping, answer)

    async def scenario():
        events = await _collect(llm.stream_chat_events(
            [{"role": "user", "content": "q"}], effort="think", max_tokens=300,
        ))
        return events, llm.get_applied_max_tokens()

    events, applied = _run(scenario())
    assert len(completions.requests) == 2
    assert completions.requests[1]["max_tokens"] == 300
    assert applied == 300
    assert ("token", "t0 ") in events
