"""Chat keeps priority on the engines `/v1` shares — the capacity findings of
the adversarial review of 2026-09-13, each pinned against the REAL admission
lanes, planner, capacity gates and `llm.stream_chat_events`.

What is stubbed is only the OpenAI client the main engine would be reached
with (`llm._client`) and `/tokenize` (`context.count_tokens`); time is scaled
(one fake token every 10 ms, admission waits of seconds rather than minutes).

The findings:

* public answers below the 131,072-token long footprint took NO gate, and ten
  130,000-token answers held all ten NORMAL admission slots — a chat turn was
  refused (`main.extended`, 2 at a time, for planned output above 8,192);
* gate footprints were charged from the 3-chars-per-token estimate, which a
  digit prompt pushes to a third of the truth (byte bound now);
* a chat large-document turn waited the whole long-lane idle bound behind a
  running public long job (the main gates step aside for it; the rest needs
  admission.py — pinned below by a strict xfail);
* a public clip made the next dictation wait behind it (see
  test_publicapi_capacity.py and the routing test at the end).
"""
from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Dict, List

import pytest

from app import admission, context, llm
from app.config import settings
from app.publicapi import capacity, errors, models, planning, registry, streaming


class _Obj:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


def _chunk(content=None, finish=None):
    choices = [] if content is None and finish is None else [
        _Obj(
            delta=_Obj(content=content, model_extra={}, reasoning=None, reasoning_content=None),
            finish_reason=finish,
        )
    ]
    return _Obj(choices=choices, usage=None)


class _SlowStream:
    def __init__(self, engine: "_FakeMain", request: Dict[str, Any]) -> None:
        self.engine = engine
        self.request = request
        self.closed = False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        count = min(int(self.request.get("max_tokens") or 16), self.engine.cap)
        self.engine.running += 1
        try:
            for _ in range(count):
                if self.closed:
                    return
                await asyncio.sleep(self.engine.tick)
                yield _chunk("x")
            yield _chunk(finish="length")
        finally:
            self.engine.running -= 1

    async def close(self):
        self.closed = True


class _FakeMain:
    """The OpenAI client `llm._client` hands back for the main engine."""

    def __init__(self, tick: float = 0.01, cap: int = 10**9) -> None:
        self.tick = tick
        self.cap = cap
        self.running = 0
        self.requests: List[Dict[str, Any]] = []
        self.chat = self
        self.completions = self

    def client(self, base_url, api_key=None, *, read_timeout=None):
        return self

    async def create(self, **request):
        self.requests.append(request)
        return _SlowStream(self, request)


@pytest.fixture(autouse=True)
def _scaled(monkeypatch):
    monkeypatch.setattr(settings, "model_max_context", 1_000_000)
    monkeypatch.setattr(settings, "gen_wall_clock_s", 4200.0)
    monkeypatch.setattr(settings, "context_safety_margin", 512)
    monkeypatch.setattr(settings, "admission_normal_max", 10)
    monkeypatch.setattr(settings, "admission_long_max", 1)
    monkeypatch.setattr(settings, "admission_long_idle_max", 0)
    monkeypatch.setattr(settings, "admission_long_threshold_tokens", 131_072)
    monkeypatch.setattr(admission, "_POLL_S", 0.02)
    monkeypatch.setattr(capacity, "YIELD_STEP_S", 0.02)
    for name in (
        "PUBLIC_API_MAIN_EXTENDED_OUTPUT_TOKENS",
        "PUBLIC_API_MAIN_EXTENDED_MAX_CONCURRENT",
        "PUBLIC_API_MAIN_LONG_MAX_CONCURRENT",
        "PUBLIC_API_MAIN_LONG_FOOTPRINT_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)

    async def estimate_count(base_url, model, messages):
        return context.estimate_messages(messages), 1_000_000

    monkeypatch.setattr(context, "count_tokens", estimate_count)
    # No engine sample: the idle test reads this process's NORMAL occupancy.
    from app import engine_state

    monkeypatch.setattr(engine_state, "engine_load", lambda: None)
    admission.reset()
    capacity.reset_for_tests()
    yield
    admission.reset()
    capacity.reset_for_tests()


@pytest.fixture()
def fake_main(monkeypatch):
    fake = _FakeMain()
    monkeypatch.setattr(llm, "_client", fake.client)
    return fake


def _flagship() -> registry.PublicModel:
    return registry.resolve_public_model("techsara-35b")


def _plan(max_output_tokens=None, text="Write a very long essay about rivers.", **extra):
    body: Dict[str, Any] = {"model": "techsara-35b", "input": text, **extra}
    if max_output_tokens is not None:
        body["max_output_tokens"] = max_output_tokens
    return planning.plan_generation(models.parse_responses_request(body), _flagship())


def _spec(plan, rid: str, **overrides):
    spec = streaming.spec_from_plan(plan, response_id=rid, created_at=int(time.time()))
    return streaming.GenerationSpec(**{**spec.__dict__, **overrides})


async def _public_sync(plan, rid: str, *, engine_tokens: int):
    """The synchronous shape of router._generate: the plan's gate (waiting up
    to PUBLIC_API_GATE_WAIT_S) around the generation."""
    from app.publicapi import router as public_router

    async with public_router._capacity_gate(plan):
        return await streaming.run_to_completion(_spec(plan, rid, max_tokens=engine_tokens))


async def _chat_first_token(messages, *, max_tokens=5) -> float:
    started = time.monotonic()
    async for _kind, _delta in llm.stream_chat_events(
        messages, model_choice="smart", effort="fast", max_tokens=max_tokens
    ):
        return time.monotonic() - started
    return time.monotonic() - started


# ---------------------------------------------------------------- the plan --


def test_the_extended_gate_has_the_argued_defaults():
    snap = capacity.snapshot()["main.extended"]
    assert snap["max_concurrent"] == 2 and snap["budget_tokens"] == 0
    assert planning.main_extended_output_tokens() == 8192


@pytest.mark.parametrize(
    "max_output_tokens, gate",
    [
        (None, None),  # the 8,192 default: exactly what public work could do before
        (8192, None),
        (8193, "main.extended"),
        (130_000, "main.extended"),  # the review's case: no gate at all before
        (200_000, "main.long"),
    ],
)
def test_a_flagship_answer_planned_above_the_old_public_ceiling_takes_a_main_gate(max_output_tokens, gate):
    assert _plan(max_output_tokens).gate_engine == gate


def test_a_digit_prompt_is_sized_for_the_long_gate_at_its_byte_bound_not_its_estimate():
    """120,000 digits are 120,000 real tokens (the Qwen pre-tokenizer isolates
    digits) and estimate at ~40,008: with 90,000 tokens of output the estimate
    put the footprint at 130,008 — under the 131,072 threshold."""
    plan = _plan(90_000, text="7" * 120_000)
    assert plan.estimated_input_tokens + plan.planned_max_output_tokens <= 131_072
    assert plan.footprint_tokens >= 120_000 + plan.planned_max_output_tokens
    assert plan.gate_engine == "main.long"


def test_the_router_gate_charges_a_digit_prompt_at_least_its_digits_so_four_cannot_fill_the_pool(monkeypatch):
    from app.publicapi import engines

    async def no_probe(engine):
        return None

    monkeypatch.setattr(engines, "served_window", no_probe)
    router = registry.resolve_public_model("techsara-8b-vision")
    if router is None:  # pragma: no cover - the suite's settings configure a router
        pytest.skip("no router configured")
    body = models.parse_responses_request(
        {"model": "techsara-8b-vision", "input": "7" * 12_000, "max_output_tokens": 2_000}
    )
    plan = planning.plan_generation(body, router)
    assert plan.gate_engine == "router"
    assert plan.gate_weight_tokens >= 12_000 + plan.planned_max_output_tokens

    async def scenario():
        async with capacity.hold("router", weight_tokens=plan.gate_weight_tokens, wait_s=1):
            with pytest.raises(errors.ApiError) as refused:
                async with capacity.hold("router", weight_tokens=plan.gate_weight_tokens, wait_s=0.05):
                    pass
            return refused.value

    assert asyncio.run(scenario()).code == "model_unavailable"


# ------------------------------------------------- the NORMAL admission lane --


def test_ten_public_long_answers_cannot_take_the_chat_apps_normal_slots(fake_main, monkeypatch):
    """The review's M1, scaled: ten public requests with 130,000-token
    ceilings (each a ~22-minute hold in production; 20 s here), then a chat
    turn. Before the fix all ten took NORMAL slots and the chat turn was
    refused after the (scaled) admission wait. Now two run, eight are refused
    at the public gate, and the chat turn gets its first token at once."""
    monkeypatch.setattr(settings, "admission_normal_wait_s", 2.0)
    monkeypatch.setenv("PUBLIC_API_GATE_WAIT_S", "0.2")
    plan = _plan(130_000)

    async def scenario():
        public = [
            asyncio.ensure_future(_public_sync(plan, f"resp_{i}", engine_tokens=2000)) for i in range(10)
        ]
        try:
            await asyncio.sleep(0.6)
            normal = admission.describe()["normal"]
            first = await asyncio.wait_for(
                _chat_first_token([{"role": "user", "content": "hello"}]), 10
            )
            refused = [t for t in public if t.done() and isinstance(t.exception(), errors.ApiError)]
            return normal, first, len(refused)
        finally:
            for task in public:
                task.cancel()
            await asyncio.gather(*public, return_exceptions=True)

    normal, first, refused = asyncio.run(scenario())
    assert normal["active"] <= 2, normal
    assert refused == 8
    assert first < 1.0


# -------------------------------------------------------- the LONG lane --


DOC = [{"role": "user", "content": "x" * 450_000}]  # ~150k tokens: the LONG lane


def test_a_main_gate_steps_aside_while_a_chat_large_document_waits_for_an_idle_engine(fake_main, monkeypatch):
    """A chat LONG request waits for the engine to be idle (600 s in
    production). A new multi-hour public generation starting meanwhile would
    keep it waiting all of it, so the main gates do not start one — for the
    whole gate wait, ending in the retry-safe 503."""
    monkeypatch.setattr(settings, "admission_long_wait_s", 5.0)
    monkeypatch.setattr(settings, "admission_normal_wait_s", 5.0)
    fake_main.cap = 400  # a chat NORMAL turn that keeps the engine busy for ~4 s

    async def scenario():
        async def drain_busy():
            async for _ in llm.stream_chat_events(
                [{"role": "user", "content": "busy"}], model_choice="smart", effort="fast", max_tokens=400
            ):
                pass

        runner = asyncio.ensure_future(drain_busy())
        await asyncio.sleep(0.2)
        long_turn = asyncio.ensure_future(_chat_first_token(DOC))
        await asyncio.sleep(0.3)
        present = capacity.chat_long_admission_present()
        started = time.monotonic()
        with pytest.raises(errors.ApiError) as refused:
            async with capacity.hold("main.extended", wait_s=0.3):
                pass
        waited = time.monotonic() - started
        for task in (runner, long_turn):
            task.cancel()
        await asyncio.gather(runner, long_turn, return_exceptions=True)
        await asyncio.sleep(0.05)
        async with capacity.hold("main.extended", wait_s=0.3):
            after = capacity.chat_long_admission_present()
        return present, refused.value, waited, after

    present, refusal, waited, after = asyncio.run(scenario())
    assert present is True
    assert refusal.status == 503 and refusal.code == "model_unavailable"
    assert refusal.headers()["Retry-After"] == "60"
    assert waited >= 0.25
    assert after is False


def test_a_public_long_prompt_in_the_long_lane_does_not_make_other_public_work_wait_for_it(monkeypatch):
    async def scenario():
        lanes = admission.lanes()
        lanes.long.active = 1  # the public request's own LONG ticket
        with capacity.PublicMainGeneration(possibly_long_prompt=True, long_lived=True):
            mine = capacity.chat_long_admission_present()
        lanes.long.active = 0
        return mine

    assert asyncio.run(scenario()) is False


def test_a_long_lived_public_generation_is_counted_as_decoding_only_after_its_first_token(fake_main):
    plan = _plan(200_000)
    assert plan.gate_engine == "main.long"
    fake_main.tick = 0.02
    seen: List[int] = []

    async def scenario():
        generation = streaming.Generation(_spec(plan, "resp_track", max_tokens=30))
        before = capacity.public_long_lived_decoding()
        async for chunk in generation.stream():
            if chunk.kind == streaming.TOKEN_KIND:
                seen.append(capacity.public_long_lived_decoding())
        await generation.aclose()
        return before, capacity.public_long_lived_decoding()

    before, after = asyncio.run(scenario())
    assert before == 0 and after == 0
    # Counted while decoding; the last chunk may be read after the producer
    # (and so the count) has already finished.
    assert seen[0] == 1 and max(seen) == 1


def test_a_default_sized_public_generation_is_never_counted_as_long_lived(fake_main):
    plan = _plan()
    assert plan.gate_engine is None

    async def scenario():
        generation = streaming.Generation(_spec(plan, "resp_short", max_tokens=5))
        counts = [capacity.public_long_lived_decoding() async for _ in generation.stream()]
        await generation.aclose()
        return counts

    assert set(asyncio.run(scenario())) == {0}


def _admission_leaves_out_public_decodes() -> bool:
    return "public_long_lived_decoding" in inspect.getsource(admission)


@pytest.mark.xfail(
    not _admission_leaves_out_public_decodes(),
    strict=True,
    reason=(
        "needs integration in app/admission.py (not this wave's file): _ahead must subtract "
        "publicapi.capacity.public_long_lived_decoding() from requests_running (or from the "
        "NORMAL occupancy without a sample). Strict: when it lands this XPASSes and the marker goes."
    ),
)
def test_a_chat_large_document_is_not_held_for_the_whole_idle_wait_by_a_running_public_long_job(
    fake_main, monkeypatch
):
    """The review's M3, scaled: 600 s → 3 s. A public 1M-token job (small
    prompt) is decoding; a chat large-document turn must start without
    waiting the whole long-lane idle bound."""
    monkeypatch.setattr(settings, "admission_long_wait_s", 3.0)
    plan = _plan(1_000_000, text="Write everything you know.")
    assert plan.gate_engine == "main.long"

    async def scenario():
        async def job():
            async with capacity.hold(plan.gate_engine, wait_s=1):
                return await streaming.run_to_completion(_spec(plan, "resp_bg", max_tokens=100_000))

        running = asyncio.ensure_future(job())
        try:
            await asyncio.sleep(0.3)
            assert capacity.public_long_lived_decoding() == 1
            return await asyncio.wait_for(_chat_first_token(DOC), 10)
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    assert asyncio.run(scenario()) < 1.5


# ------------------------------------------------------ dictation routing --


class _Replica:
    def __init__(self, url: str) -> None:
        self.base_url = url
        self.name = "whisper"
        self.model = "whisper"
        self.calls = 0

    async def transcribe(self, audio, **kwargs):
        self.calls += 1
        return _Obj(text="ok")


def test_a_public_clip_counts_on_its_replica_in_dictations_least_active_routing(monkeypatch):
    """Review experiment W: dictation A on replica 0, a public clip decoding
    on replica 1 — dictation B went to replica 1 and waited behind the public
    clip. Counted, B's routing sees replica 1 as busy."""
    from app import asr
    from app.publicapi import sidecars

    replicas = [_Replica("http://r0/v1"), _Replica("http://r1/v1")]
    provider = asr.RoutedProvider(replicas)
    monkeypatch.setattr(asr, "_provider", provider)

    assert provider._order()[0] == 0
    with sidecars.counted_in_dictation_routing("http://r1/v1"):
        provider._active[0] += 1  # dictation A decoding on replica 0
        assert [row["active"] for row in provider.stats()] == [1, 1]
        # Tie: B goes to index 0, behind A's short dictation, not behind the
        # public clip.
        assert provider._order()[0] == 0
        provider._active[0] -= 1
        assert provider._order() == [0, 1]
    assert [row["active"] for row in provider.stats()] == [0, 0]


def test_the_routing_count_is_given_back_when_the_clip_fails(monkeypatch):
    from app import asr
    from app.publicapi import sidecars

    provider = asr.RoutedProvider([_Replica("http://r0/v1"), _Replica("http://r1/v1")])
    monkeypatch.setattr(asr, "_provider", provider)
    with pytest.raises(RuntimeError):
        with sidecars.counted_in_dictation_routing("http://r1/v1"):
            raise RuntimeError("engine gone")
    assert [row["active"] for row in provider.stats()] == [0, 0]
