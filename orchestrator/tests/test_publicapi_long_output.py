"""One million output tokens on techsara-35b (owner decision, 2026-09-13).

What the owner decided, and what each test below pins:

* the public ceiling is PUBLIC_API_MAX_OUTPUT_TOKENS (1,000,000) — the chat
  app's MODEL_MAX_OUTPUT is not read; the default stays 8,192;
* input and output share the window, so an overflowing request is CLAMPED to
  what is left, never refused — and the response says what was applied;
* a long generation is not cut at the chat app's 70-minute wall clock: each
  request gets its own, sized from its planned output, and the in-band
  "[generation stopped …]" marker never reaches `/v1` output;
* the reservation of a 1M job does not inflate today's ledger while limits
  are off; the idempotency lease outlives the longest generation.

The engine is stubbed (`llm.stream_chat_events`); PostgreSQL is real for the
route tests, as in tests/test_publicapi_routes.py.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Dict, List, Optional

import pytest

from app import db, llm
from app.apiplatform import quotas
from app.config import settings
from app.publicapi import capacity, errors, events, models, planning, registry, streaming
from tests.test_publicapi_routes import TOKENS, _auth, _pepper, api, platform  # noqa: F401
from tests.publicapi_fake_engine import set_setting

_run = asyncio.run


@pytest.fixture(autouse=True)
def _one_million_window(monkeypatch):
    """The production shape: MAIN_MODEL_MAX_LEN=1000000, GEN_WALL_CLOCK_S=4200."""
    monkeypatch.setattr(settings, "model_max_context", 1_000_000)
    monkeypatch.setattr(settings, "gen_wall_clock_s", 4200.0)
    monkeypatch.setattr(settings, "context_safety_margin", 512)
    for name in (
        "PUBLIC_API_MAX_OUTPUT_TOKENS",
        "PUBLIC_API_GEN_WALL_CLOCK_S",
        "PUBLIC_API_MAIN_PREFILL_ALLOWANCE_S",
        "PUBLIC_API_MAIN_MIN_DECODE_TOKENS_PER_S",
    ):
        monkeypatch.delenv(name, raising=False)
    capacity.reset_for_tests()


def _request(**body) -> models.ResponsesRequest:
    return models.parse_responses_request({"model": "techsara-35b", "input": "hi", **body})


def _flagship() -> registry.PublicModel:
    return registry.resolve_public_model("techsara-35b")


# ------------------------------------------------------------ the plan --


def test_one_million_is_accepted_and_one_more_is_a_400_naming_max_output_tokens():
    plan = planning.plan_generation(_request(max_output_tokens=1_000_000), _flagship())
    assert plan.requested_max_output_tokens == 1_000_000

    with pytest.raises(errors.ApiError) as refused:
        planning.plan_generation(_request(max_output_tokens=1_000_001), _flagship())
    assert refused.value.status == 400 and refused.value.param == "max_output_tokens"


def test_an_omitted_max_output_tokens_keeps_the_eight_thousand_default():
    plan = planning.plan_generation(_request(), _flagship())
    assert plan.requested_max_output_tokens == 8192
    assert plan.planned_max_output_tokens == 8192
    assert plan.clamped is False


def test_a_request_that_overflows_the_window_is_clamped_to_what_is_left_and_not_refused():
    prompt = "x" * 300_000  # 300,000 bytes: inside the input ceiling on the byte bound
    plan = planning.plan_generation(
        _request(input=prompt, max_output_tokens=1_000_000), _flagship()
    )

    estimate = plan.estimated_input_tokens
    assert plan.planned_max_output_tokens == 1_000_000 - estimate - 512
    assert plan.clamped is True
    # The ENGINE is sent the request: llm._fit re-clamps with the exact
    # /tokenize count, so the window is used to the token, never overrun.
    assert plan.max_tokens_for_engine == 1_000_000


def test_the_project_ceiling_still_narrows_the_model_ceiling_and_zero_means_zero():
    with pytest.raises(errors.ApiError):
        planning.plan_generation(
            _request(max_output_tokens=5000), _flagship(), project_max_output_tokens=4096
        )
    with pytest.raises(errors.ApiError) as zero:
        planning.plan_generation(_request(), _flagship(), project_max_output_tokens=0)
    assert zero.value.param == "max_output_tokens"


def test_input_over_the_ceiling_counted_on_the_byte_bound_is_the_only_size_refusal():
    # A digit string estimates at a third of its real tokens (the Qwen
    # pre-tokenizer isolates digits): refused on its byte length.
    with pytest.raises(errors.ApiError) as refused:
        planning.plan_generation(
            _request(input="7" * 2900), _flagship(), project_max_input_tokens=1000
        )
    assert refused.value.code == "context_length_exceeded"


@pytest.mark.parametrize("max_output_tokens", [None, 100_000, 1_000_000])
def test_a_plan_carries_no_wall_clock_at_any_output_size(max_output_tokens):
    """No-timeout design (2026-09-13): the per-request wall clock is deleted.
    `wall_clock_s` survives on the plan only as 0.0 — "none" — for readers
    that still log it."""
    plan = planning.plan_generation(_request(max_output_tokens=max_output_tokens), _flagship())
    assert plan.wall_clock_s == 0.0
    assert not hasattr(planning, "wall_clock_for")


def test_the_retired_wall_clock_settings_change_nothing(monkeypatch):
    set_setting(monkeypatch, "PUBLIC_API_GEN_WALL_CLOCK_S", "10")
    set_setting(monkeypatch, "PUBLIC_API_MAIN_PREFILL_ALLOWANCE_S", "1")
    set_setting(monkeypatch, "PUBLIC_API_MAIN_MIN_DECODE_TOKENS_PER_S", "1")
    router = registry.resolve_public_model("techsara-8b-vision")
    for model in (_flagship(), router):
        if model is None:
            continue
        assert planning.plan_generation(_request(), model).wall_clock_s == 0.0


def test_an_answer_planned_above_800k_takes_the_one_at_a_time_gate_and_a_normal_one_takes_none():
    # Integration 2026-09-13: by the PLANNED OUTPUT, never the prompt; above
    # PUBLIC_API_MAIN_SOLO_OUTPUT_TOKENS (800,000) two answers are the whole KV pool.
    short = planning.plan_generation(_request(), _flagship())
    extended = planning.plan_generation(_request(max_output_tokens=200_000), _flagship())
    long = planning.plan_generation(_request(max_output_tokens=900_000), _flagship())
    assert short.gate_engine is None
    assert extended.gate_engine == "main.extended"
    assert long.gate_engine == "main.long"


def test_the_applied_value_prefers_llms_own_then_the_reported_prompt_then_the_plan():
    common = dict(engine="main", requested=1_000_000, planned=600_000, context_window=1_000_000, reserve=512)
    assert planning.applied_max_output_tokens(**common, llm_applied=700_000) == 700_000
    assert (
        planning.applied_max_output_tokens(**common, usage={"prompt_tokens": 250_000})
        == 1_000_000 - 250_000 - 512
    )
    assert planning.applied_max_output_tokens(**common, usage=None) == 600_000
    sidecar = dict(common, engine="router")
    assert planning.applied_max_output_tokens(**sidecar, usage={"prompt_tokens": 1}) == 600_000


def test_with_limits_off_a_one_million_job_reserves_only_the_default_output():
    plan = planning.plan_generation(_request(max_output_tokens=1_000_000), _flagship())
    assert planning.reservation_output_tokens(plan, limits_enforced=False) == 8192
    # Enforced: the PLANNED value — what the window leaves after "hi".
    assert planning.reservation_output_tokens(plan, limits_enforced=True) == plan.planned_max_output_tokens
    assert 999_000 < plan.planned_max_output_tokens < 1_000_000
    default = planning.plan_generation(_request(), _flagship())
    assert planning.reservation_output_tokens(default, limits_enforced=False) is None


# ------------------------------------------------------- the generation --


def _spec(**overrides) -> streaming.GenerationSpec:
    base: Dict[str, Any] = dict(
        response_id="resp_long",
        model="techsara-35b",
        messages=[{"role": "user", "content": "write"}],
        max_tokens=1_000_000,
        temperature=0.2,
        created_at=1789200000,
        item_id="msg_fixed",
        engine="main",
        requested_max_output_tokens=1_000_000,
        planned_max_output_tokens=999_000,
        context_window=1_000_000,
        context_reserve=512,
    )
    base.update(overrides)
    return streaming.GenerationSpec(**base)


def _usage(monkeypatch, value):
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: value)


def test_the_in_band_wall_clock_marker_never_reaches_the_answer_and_the_response_fails_as_timeout(monkeypatch):
    # NOT stubbed (adversarial review 2026-09-13): vLLM's usage chunk is the
    # stream's last, and a wall-clock stop never sees it — a stubbed usage
    # here was a false green for "the partial output is still measured".
    _usage(monkeypatch, None)

    def engine(messages, **kwargs):
        async def run():
            yield ("token", "partial ")
            yield ("token", "answer")
            # Exactly what llm.stream_chat_events does at its wall clock.
            llm._finish_reason.set(llm.WALL_CLOCK_FINISH)
            yield ("token", "\n\n[generation stopped after 4200s — wall-clock guard; the text above is what was produced]")

        return run()

    monkeypatch.setattr(llm, "stream_chat_events", engine)
    outcome = _run(streaming.run_to_completion(_spec()))

    assert outcome.text == "partial answer"
    assert outcome.status == "failed"
    assert outcome.error.code == "timeout" and outcome.error.status == 504
    # The partial output is still counted and charged: the two deltas the
    # server received (never the marker), and the prompt by the estimate
    # because this stub took no /tokenize count.
    from app import context

    assert outcome.usage == {
        "prompt_tokens": context.estimate_messages(_spec().messages),
        "completion_tokens": 2,
        "calls": 1,
        "source": streaming.USAGE_COUNTED_AT_STOP,
    }
    assert outcome.max_output_tokens == _spec().planned


class _InterruptAfter:
    """A liveness guard that proves the engine lost the request after `ticks`
    quiet heartbeats — the only thing that ends a silent attempt now."""

    def __init__(self, ticks: int) -> None:
        self.ticks = ticks
        self.seen = 0
        self.dispatched_at = None
        self.chunks = 0

    def dispatched(self, now=None):
        self.dispatched_at = 0.0

    def chunk(self, now=None, *, token=True):
        self.chunks += 1

    def tick(self, now=None):
        from app.publicapi import liveness

        self.seen += 1
        if self.seen >= self.ticks:
            return liveness.Verdict(True, liveness.REASON_LOST, False, "test")
        return liveness.OK


def test_an_interrupted_generation_is_counted_too(monkeypatch):
    _usage(monkeypatch, None)

    def engine(messages, **kwargs):
        async def run():
            yield ("token", "one ")
            yield ("reasoning", "thinking")
            yield ("token", "two")
            await asyncio.sleep(3600)

        return run()

    monkeypatch.setattr(llm, "stream_chat_events", engine)
    guard = _InterruptAfter(ticks=3)

    async def scenario():
        generation = streaming.Generation(_spec(estimated_input_tokens=41), heartbeat_s=0.02, guard=guard)
        async for _ in generation.stream():
            pass
        await generation.aclose()
        return generation

    generation = _run(asyncio.wait_for(scenario(), 10))
    assert generation.interrupt is not None and generation.interrupt.reason == "lost"
    assert generation.error is None
    assert generation.usage["completion_tokens"] == 3  # the reasoning delta was generated too
    assert generation.usage["prompt_tokens"] == 41
    assert generation.usage["source"] == streaming.USAGE_COUNTED_AT_STOP
    assert guard.chunks == 3


def test_an_engine_that_accepts_the_no_timeout_kwargs_gets_no_wall_clock_and_no_read_timeout(monkeypatch):
    _usage(monkeypatch, None)
    seen: Dict[str, Any] = {}

    def engine(
        messages, *, model_choice, effort, temperature, max_tokens, wall_clock_s=None,
        wall_clock_marker=True, read_timeout_s=600.0, admission_patient=False, on_dispatch=None,
        continue_final_message=False,
    ):
        seen.update(
            wall_clock_s=wall_clock_s, wall_clock_marker=wall_clock_marker, read_timeout_s=read_timeout_s,
            admission_patient=admission_patient, continue_final_message=continue_final_message,
            messages=list(messages), max_tokens=max_tokens,
        )

        async def run():
            if on_dispatch is not None:
                on_dispatch()
            yield ("token", "ok")

        return run()

    monkeypatch.setattr(llm, "stream_chat_events", engine)
    _run(streaming.run_to_completion(_spec()))
    assert seen["wall_clock_s"] == 0 and seen["wall_clock_marker"] is False
    assert seen["read_timeout_s"] is None
    assert seen["continue_final_message"] is False

    # A continuation: the partial answer as the final assistant message, the
    # remaining budget, and continue_final_message — never a fresh turn.
    guard = _InterruptAfter(ticks=10**9)
    prefix = [{"role": "user", "content": "write"}, {"role": "assistant", "content": "partial"}]

    async def resume():
        generation = streaming.Generation(
            _spec(), guard=guard, messages=prefix, max_tokens=900, continue_final_message=True,
            admission_patient=True,
        )
        texts = [c.text async for c in generation.stream() if c.kind == "token"]
        await generation.aclose()
        return texts

    assert _run(resume()) == ["ok"]
    assert seen["continue_final_message"] is True and seen["admission_patient"] is True
    assert seen["messages"] == prefix and seen["max_tokens"] == 900
    assert guard.dispatched_at is not None  # on_dispatch reached the guard


def test_a_continuation_on_an_engine_path_without_continue_final_message_is_refused_not_regenerated(monkeypatch):
    _usage(monkeypatch, None)
    calls: List[Any] = []

    def engine(messages, *, model_choice, effort, temperature, max_tokens):
        calls.append(messages)

        async def run():
            yield ("token", "fresh answer")

        return run()

    monkeypatch.setattr(llm, "stream_chat_events", engine)

    async def scenario():
        generation = streaming.Generation(
            _spec(), messages=[{"role": "assistant", "content": "x"}], continue_final_message=True
        )
        async for _ in generation.stream():
            pass
        await generation.aclose()
        return generation

    generation = _run(scenario())
    assert type(generation.error).__name__ == "ContinuationUnsupported"
    assert calls == []


def test_an_engine_without_a_per_call_clock_is_not_handed_one_and_its_own_clock_is_named(monkeypatch):
    _usage(monkeypatch, None)
    seen: Dict[str, Any] = {}

    def engine(messages, *, model_choice, effort, temperature, max_tokens):
        seen["called"] = True

        async def run():
            llm._finish_reason.set(llm.WALL_CLOCK_FINISH)
            if False:
                yield ("token", "")

        return run()

    monkeypatch.setattr(llm, "stream_chat_events", engine)
    outcome = _run(streaming.run_to_completion(_spec()))

    assert seen == {"called": True}
    assert "4200 second" in outcome.error.message


def test_a_hung_engine_is_waited_on_until_its_liveness_guard_interrupts_then_closed(monkeypatch):
    """No clock cuts a silent engine (a 30-minute prefill is legitimate); the
    guard's proof does. Non-durable callers report the interrupt as the
    retry-safe `model_unavailable`, with the partial text, and the engine
    stream is closed."""
    _usage(monkeypatch, None)
    closed: List[bool] = []

    def engine(messages, **kwargs):
        async def run():
            try:
                yield ("token", "started")
                await asyncio.sleep(3600)
            finally:
                closed.append(True)

        return run()

    monkeypatch.setattr(llm, "stream_chat_events", engine)
    guard = _InterruptAfter(ticks=5)
    real = streaming.Generation

    def guarded(spec, **kwargs):
        kwargs.setdefault("guard", guard)
        return real(spec, **kwargs)

    monkeypatch.setattr(streaming, "Generation", guarded)
    started = time.monotonic()
    outcome = _run(
        asyncio.wait_for(streaming.run_to_completion(_spec(wall_clock_s=0.1), heartbeat_s=0.02), 10)
    )

    assert time.monotonic() - started < 5
    assert guard.seen >= 5  # it waited through every quiet heartbeat first
    assert outcome.status == "failed" and outcome.error.code == "model_unavailable"
    assert outcome.text == "started"
    assert closed == [True]


def test_the_stream_announces_the_planned_ceiling_and_ends_with_the_applied_one(monkeypatch):
    _usage(monkeypatch, {"prompt_tokens": 400, "completion_tokens": 2})
    monkeypatch.setattr(llm, "get_applied_max_tokens", lambda: 999_088, raising=False)

    def engine(messages, **kwargs):
        async def run():
            yield ("token", "hi")
            llm._set_finish_reason("length")

        return run()

    monkeypatch.setattr(llm, "stream_chat_events", engine)

    async def drain():
        return "".join([f async for f in streaming.responses_sse(_spec())])

    records = events.parse_frames(_run(drain()))
    created = records[0]["data"]["response"]
    completed = records[-1]["data"]["response"]
    assert created["max_output_tokens"] == 999_000
    assert completed["max_output_tokens"] == 999_088
    assert completed["incomplete_details"] == {"reason": "max_output_tokens"}
    assert completed["status"] == "completed"


def test_the_compatibility_stream_carries_the_applied_ceiling_on_its_finish_chunk(monkeypatch):
    _usage(monkeypatch, {"prompt_tokens": 400, "completion_tokens": 2})

    def engine(messages, **kwargs):
        async def run():
            yield ("token", "hi")

        return run()

    monkeypatch.setattr(llm, "stream_chat_events", engine)

    async def drain():
        return "".join(
            [f async for f in streaming.chat_completions_sse(_spec(), completion_id="chatcmpl_x")]
        )

    import json

    chunks = [json.loads(line[6:]) for line in _run(drain()).splitlines() if line.startswith("data: {")]
    finish = [c for c in chunks if c["choices"] and c["choices"][0]["finish_reason"]]
    assert finish[0]["max_output_tokens"] == 1_000_000 - 400 - 512
    assert all("max_output_tokens" not in c for c in chunks if c not in finish)


# ------------------------------------------------------------ the routes --


@pytest.fixture()
def engine(monkeypatch):
    calls: List[Dict[str, Any]] = []

    def install(pieces=("ok",), usage=None, finish: Optional[str] = None):
        def fake(messages, **kwargs):
            calls.append(kwargs)

            async def run():
                for piece in pieces:
                    await asyncio.sleep(0)
                    yield ("token", piece)
                if finish:
                    llm._set_finish_reason(finish)

            return run()

        monkeypatch.setattr(llm, "stream_chat_events", fake)
        monkeypatch.setattr(llm, "reset_usage", lambda: None)
        monkeypatch.setattr(llm, "get_usage", lambda: usage)
        return calls

    return install


def test_a_one_million_token_request_is_answered_and_the_applied_ceiling_is_stored(api, platform, engine):
    calls = engine(["done"], usage={"prompt_tokens": 20, "completion_tokens": 1}, finish="length")
    response = api.post(
        "/v1/responses",
        json={"model": "techsara-35b", "input": "Write a very long book.", "max_output_tokens": 1_000_000},
        headers=_auth(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["max_output_tokens"] == 1_000_000 - 20 - 512
    assert body["incomplete_details"] == {"reason": "max_output_tokens"}
    assert calls[0]["max_tokens"] == 1_000_000

    stored = api.get(f"/v1/responses/{body['id']}", headers=_auth()).json()
    assert stored["max_output_tokens"] == body["max_output_tokens"]
    assert stored["incomplete_details"] == {"reason": "max_output_tokens"}


def test_a_background_202_already_names_the_planned_ceiling(api, platform, engine):
    engine(["x"])
    response = api.post(
        "/v1/responses",
        json={"model": "techsara-35b", "input": "hi", "background": True, "max_output_tokens": 50_000},
        headers=_auth(),
    )

    assert response.status_code == 202, response.text
    assert response.json()["max_output_tokens"] == 50_000


def test_the_chat_completion_body_reports_the_applied_ceiling(api, platform, engine):
    engine(["hi"], usage={"prompt_tokens": 5, "completion_tokens": 1})
    response = api.post(
        "/v1/chat/completions",
        json={"model": "techsara-35b", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 900_000},
        headers=_auth(),
    )

    assert response.status_code == 200, response.text
    assert response.json()["max_output_tokens"] == 900_000
    assert response.json()["choices"][0]["finish_reason"] == "stop"


def test_the_usage_ledger_records_what_was_asked_for_what_was_applied_and_the_clock(
    api, platform, engine, monkeypatch
):
    from app import usage as usage_ledger

    rows = []

    async def spy(**kwargs):
        rows.append(kwargs)

    monkeypatch.setattr(usage_ledger, "record_async", spy)
    engine(["hi"], usage={"prompt_tokens": 100_000, "completion_tokens": 1})
    api.post(
        "/v1/responses",
        json={"model": "techsara-35b", "input": "hi", "max_output_tokens": 1_000_000},
        headers=_auth(),
    )

    meta = rows[-1]["meta"]
    assert meta["max_output_tokens_requested"] == 1_000_000
    assert meta["max_output_tokens_applied"] == 1_000_000 - 100_000 - 512
    assert meta["clamped"] is True
    # RETIRED: the plan carries 0.0 — no wall clock (no-timeout design). The
    # router (T3) may drop the field; until then it must never be a limit.
    assert meta.get("wall_clock_s", 0.0) == 0.0
    assert rows[-1]["model"] == "techsara-35b"


def test_with_limits_off_a_one_million_request_reserves_the_default_output_at_admission(
    api, platform, engine, monkeypatch
):
    seen = []
    real = quotas.reserve

    def spy(caller, **kwargs):
        seen.append(kwargs.get("max_output_tokens"))
        return real(caller, **kwargs)

    monkeypatch.setattr(quotas, "reserve", spy)
    engine(["ok"])
    api.post(
        "/v1/responses",
        json={"model": "techsara-35b", "input": "hi", "max_output_tokens": 1_000_000},
        headers=_auth(),
    )
    assert seen == [8192]


def test_an_answer_planned_above_800k_holds_the_main_long_gate_while_it_runs(api, platform, engine, monkeypatch):
    seen = []
    real_hold = capacity.hold

    def spy(gate, **kwargs):
        seen.append((gate, capacity.snapshot()["main.long"]["in_flight"]))
        return real_hold(gate, **kwargs)

    monkeypatch.setattr(capacity, "hold", spy)
    engine(["ok"])

    long = api.post(
        "/v1/responses",
        json={"model": "techsara-35b", "input": "hi", "max_output_tokens": 900_000},
        headers=_auth(),
    )
    short = api.post("/v1/responses", json={"model": "techsara-35b", "input": "hi"}, headers=_auth())

    assert long.status_code == short.status_code == 200
    # The long request holds main.long, alone. The short one never touches
    # main.long; T3's router takes `capacity.gates_for` (design
    # capacity_waits), so it holds main.normal (assembler, 2026-09-14: the
    # pre-T3 alternative is no longer accepted).
    assert seen[0] == ("main.long", 0)
    assert [gate for gate, _ in seen[1:]] == ["main.normal"]


@pytest.mark.parametrize("mode", ["sync", "stream", "background"])
def test_a_long_answer_admitted_before_its_status_line_hands_that_ticket_to_its_generation(
    api, platform, monkeypatch, mode
):
    """One accounting (integration 2026-09-13), through the mounted routes: the
    main gate admits the answer into admission's LONG_OUTPUT lane before the
    status line — in the handler for sync and stream, inside the job for
    background — and the producer task that calls the engine sees exactly that
    ticket (a ContextVar, carried into the tasks Starlette and the generation
    create) as /v1 work; the ticket is released when the request ends."""
    from app import admission

    monkeypatch.setenv("ADMISSION_KV_METRICS_URL", "off")
    tickets: List[Any] = []
    seen: List[tuple] = []
    real_preadmit = admission.preadmit

    async def spy(*args, **kwargs):
        ticket = await real_preadmit(*args, **kwargs)
        tickets.append(ticket)
        return ticket

    def fake(messages, **kwargs):
        async def run():
            pre = admission._preadmitted.get()
            seen.append((pre is not None and pre is tickets[-1], getattr(pre, "lane", None),
                         admission.current_origin()))
            yield ("token", "ok")

        return run()

    monkeypatch.setattr(admission, "preadmit", spy)
    monkeypatch.setattr(llm, "stream_chat_events", fake)
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: None)

    body: Dict[str, Any] = {"model": "techsara-35b", "input": "Write a long essay.", "max_output_tokens": 100_000}
    if mode == "stream":
        body["stream"] = True
    if mode == "background":
        body["background"] = True
    response = api.post("/v1/responses", json=body, headers=_auth())
    assert response.status_code == (202 if mode == "background" else 200), response.text
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (tickets and tickets[0].released and seen):
        time.sleep(0.05)
    if mode == "background":
        rid = response.json()["id"]
        while time.monotonic() < deadline:
            if api.get(f"/v1/responses/{rid}", headers=_auth()).json()["status"] == "completed":
                break
            time.sleep(0.05)

    assert len(tickets) == 1 and tickets[0] is not None
    assert tickets[0].lane == admission.LONG_OUTPUT and tickets[0].origin == admission.ORIGIN_V1
    assert seen == [(True, admission.LONG_OUTPUT, admission.ORIGIN_V1)]
    assert tickets[0].released


def test_a_background_job_waits_queued_for_the_long_gate_and_a_cancel_there_never_asks_the_model(
    api, platform, engine, monkeypatch
):
    calls = engine(["never"])
    set_setting(monkeypatch, "PUBLIC_API_BACKGROUND_GATE_WAIT_S", "30")

    with api.portal.wrap_async_context_manager(capacity.hold("main.long", wait_s=1)):
        created = api.post(
            "/v1/responses",
            json={"model": "techsara-35b", "input": "hi", "background": True, "max_output_tokens": 900_000},
            headers=_auth(),
        ).json()
        time.sleep(0.3)
        polled = api.get(f"/v1/responses/{created['id']}", headers=_auth()).json()
        assert polled["status"] == "queued"
        api.post(f"/v1/responses/{created['id']}/cancel", headers=_auth())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            final = api.get(f"/v1/responses/{created['id']}", headers=_auth()).json()
            if final["status"] != "queued":
                break
            time.sleep(0.05)

    assert final["status"] == "cancelled"
    assert calls == []


def test_a_background_job_that_never_gets_capacity_fails_retry_safe(api, platform, engine, monkeypatch):
    calls = engine(["never"])
    set_setting(monkeypatch, "PUBLIC_API_BACKGROUND_GATE_WAIT_S", "0.2")

    with api.portal.wrap_async_context_manager(capacity.hold("main.long", wait_s=1)):
        created = api.post(
            "/v1/responses",
            json={"model": "techsara-35b", "input": "hi", "background": True, "max_output_tokens": 900_000},
            headers=_auth(),
        ).json()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            final = api.get(f"/v1/responses/{created['id']}", headers=_auth()).json()
            if final["status"] not in ("queued", "in_progress"):
                break
            time.sleep(0.05)

    assert final["status"] == "failed"
    assert final["error"]["code"] == "model_unavailable"
    assert calls == []


def test_a_read_timeout_shorter_than_a_full_window_prefill_is_logged_as_an_error(monkeypatch, caplog):
    # The LLM_REQUEST_TIMEOUT invariant for /v1: the read timeout bounds the
    # silence of an 878 s full-window prefill, so it must be at least the
    # prefill allowance — or a 1M-context request dies inside httpx.
    monkeypatch.setattr(planning, "_TRANSPORT_CHECKED", False)
    monkeypatch.setattr(settings, "llm_request_timeout", 300.0)
    with caplog.at_level("ERROR", logger="app.publicapi.planning"):
        assert planning.transport_timeout_is_sufficient() is False
    assert "LLM_REQUEST_TIMEOUT" in caplog.text

    monkeypatch.setattr(settings, "llm_request_timeout", 4200.0)
    assert planning.transport_timeout_is_sufficient() is True
