"""The main-engine gates after the integration of 2026-09-13: ONE accounting of
long public work, sized by what cannot be gamed, and chat first.

Pinned against the REAL planner, capacity gates, admission lanes, KV ledger,
`streaming.Generation` and `llm.stream_chat_events`. Only the OpenAI client of
the main engine (`llm._client`) and `/tokenize` (`context.count_tokens`) are
stubbed; time is scaled (one fake token every 10 ms, waits of seconds).

What each group proves:

* THE REGRESSION (six-model rereview, NEW high): a gate sized by the prompt's
  UTF-8 byte bound sent every ~123 KB document (35-47k real tokens) through the
  one-at-a-time `main.long` gate, and one 1M-output job refused them all. Now
  no main gate is sized by the prompt; documents run beside a 1M job.
* ONE ACCOUNTING: a gated answer is admitted once, into admission's
  LONG_OUTPUT lane (seat + KV charge), before its status line; the
  generation's own admission call uses that ticket instead of queueing again;
  every exit gives the seat and the charge back.
* THE YIELD (rereview P1, medium): a chat LONG request past its first token no
  longer refuses public work; one still before it does.
* THE KV RULE: a public answer planned above ~800,000 tokens runs one at a
  time whatever ADMISSION_KV_RESERVE_FRACTION says (pool 1,663,201 tokens).
"""
from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Dict, List

import pytest

from app import admission, context, engine_state, kv_budget, llm
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
        self.engine.peak = max(self.engine.peak, self.engine.running)
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
    def __init__(self, tick: float = 0.01, cap: int = 10**9) -> None:
        self.tick = tick
        self.cap = cap
        self.running = 0
        self.peak = 0
        self.requests: List[Dict[str, Any]] = []
        self.chat = self
        self.completions = self

    def client(self, base_url, api_key=None, *, read_timeout=None, **transport):
        # `transport`: the no-timeout /v1 path asks for `unbounded_read=True`
        # (llm._client's keep-alive client); the fake serves every shape.
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
    monkeypatch.setattr(settings, "admission_normal_wait_s", 5.0)
    monkeypatch.setattr(settings, "admission_long_wait_s", 5.0)
    monkeypatch.setattr(admission, "_POLL_S", 0.02)
    monkeypatch.setattr(capacity, "YIELD_STEP_S", 0.02)
    for name in (
        "PUBLIC_API_MAIN_EXTENDED_OUTPUT_TOKENS",
        "PUBLIC_API_MAIN_EXTENDED_MAX_CONCURRENT",
        "PUBLIC_API_MAIN_LONG_MAX_CONCURRENT",
        "PUBLIC_API_MAIN_SOLO_OUTPUT_TOKENS",
        "ADMISSION_KV_RESERVE_FRACTION",
        "ADMISSION_LONG_OUTPUT_MAX_SEQS",
    ):
        monkeypatch.delenv(name, raising=False)
    # The KV pool is the 2026-09-13 setting (1,663,201 tokens in 2,096-token
    # blocks); the suite never reads an engine's /metrics.
    monkeypatch.setenv("ADMISSION_KV_METRICS_URL", "off")
    # The public gate bound: 30 s in production, 0.5 s here. Both spellings,
    # because config.py may or may not declare the attribute yet.
    monkeypatch.setenv("PUBLIC_API_GATE_WAIT_S", "0.5")
    monkeypatch.setattr(settings, "public_api_gate_wait_s", 0.5, raising=False)

    async def estimate_count(base_url, model, messages):
        return context.estimate_messages(messages), 1_000_000

    monkeypatch.setattr(context, "count_tokens", estimate_count)
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


def _plan(max_output_tokens=None, text="Write a very long essay about rivers."):
    body: Dict[str, Any] = {"model": "techsara-35b", "input": text}
    if max_output_tokens is not None:
        body["max_output_tokens"] = max_output_tokens
    return planning.plan_generation(models.parse_responses_request(body), _flagship())


def _spec(plan, rid: str, **overrides):
    spec = streaming.spec_from_plan(plan, response_id=rid, created_at=int(time.time()))
    return streaming.GenerationSpec(**{**spec.__dict__, **overrides})


def _document(kilobytes: int) -> str:
    """Ordinary prose of `kilobytes` KB: ~3-4 bytes a token, so 105-141 KB is
    the rereview's 35-47k-token document."""
    sentence = "The river bends past the old mill and the town gathers at its banks. "
    return (sentence * (kilobytes * 1000 // len(sentence) + 1))[: kilobytes * 1000]


def _bounded_gate(plan):
    """The plan's main gate held with the SYNCHRONOUS bound — what
    `router._capacity_gate(plan)` was when these tests were written (PR #65).

    The router itself no longer holds a bounded gate: since the no-timeout
    release it waits through `router._patient_gate` (every gate of
    `capacity.gates_for`, no time limit), which passes the same `work=plan`,
    so the one accounting pinned here is the one the router uses
    (`test_the_routers_patient_gate_admits_a_long_answer_once_with_no_bound`).
    A FINITE caller still exists — the legacy background path and the sidecar
    routes — and these tests pin the capacity/admission front door for it."""
    import contextlib

    if not plan.gate_engine:
        return contextlib.nullcontext()
    return capacity.hold(
        plan.gate_engine,
        weight_tokens=plan.gate_weight_tokens,
        wait_s=capacity.sync_wait_s(),
        yield_to_chat=plan.yield_to_chat,
        work=plan,
    )


async def _public_sync(plan, rid: str, *, engine_tokens: int):
    """router._generate's synchronous shape: the plan's gate, then the
    generation inside it."""
    async with _bounded_gate(plan):
        return await streaming.run_to_completion(_spec(plan, rid, max_tokens=engine_tokens))


async def _chat_first_token(messages, *, max_tokens=5) -> float:
    started = time.monotonic()
    async for _kind, _delta in llm.stream_chat_events(
        messages, model_choice="smart", effort="fast", max_tokens=max_tokens
    ):
        return time.monotonic() - started
    return time.monotonic() - started


def _block_charge(prompt: int, output: int) -> int:
    return kv_budget.charge_tokens(prompt, output, block_size=2096, window=1_000_000)


# ----------------------------------------------------- the regression --


@pytest.mark.parametrize("kilobytes", [105, 123, 140, 141])
def test_an_ordinary_document_with_the_default_output_takes_no_main_gate(kilobytes):
    plan = _plan(text=_document(kilobytes))
    assert plan.bounded_input_tokens > 100_000  # the byte bound that used to pick main.long
    assert plan.gate_engine is None


@pytest.mark.parametrize(
    "planned, gate",
    [(8192, None), (8193, "main.extended"), (800_000, "main.extended"), (800_001, "main.long"), (1_000_000, "main.long")],
)
def test_the_main_gate_follows_the_planned_output_only(planned, gate):
    assert planning.main_gate_for(planned) == gate


def test_a_digit_prompt_is_still_a_long_prompt_by_the_exact_count_that_admission_takes(monkeypatch):
    """140,000 digits are 140,000 real tokens (one per digit) but estimate at
    ~46,674. The planner no longer gates on the prompt; admission's exact
    count is what puts it in the one-at-a-time LONG lane."""
    digits = "7" * 140_000
    plan = _plan(text=digits)
    assert plan.gate_engine is None

    async def exact(base_url, model, messages):
        return sum(len(str(m.get("content") or "")) for m in messages), 1_000_000

    monkeypatch.setattr(context, "count_tokens", exact)

    async def scenario():
        tokens = await admission.prompt_tokens(plan.messages, base_url="http://engine/v1", model="m")
        return tokens, admission.lane_for(tokens, None, admission.ORIGIN_V1)

    tokens, lane = asyncio.run(scenario())
    assert tokens >= 140_000 and lane == admission.LONG


def test_documents_of_35_to_47k_tokens_run_beside_a_1m_job_and_chat_keeps_its_first_token(fake_main):
    """Rereview P9, scaled: one 1M-output job holds main.long; twenty default-
    output documents of 105-141 KB arrive. Before: every one 503 after 0.5 s.
    Now: none refused, none waits for the job, and a chat turn's first token
    is as fast as on an idle engine."""
    job_plan = _plan(1_000_000, text="Write everything you know.")
    assert job_plan.gate_engine == "main.long"

    async def scenario():
        idle_first = await _chat_first_token([{"role": "user", "content": "hello"}])
        job = asyncio.ensure_future(_public_sync(job_plan, "resp_1m", engine_tokens=600))
        await asyncio.sleep(0.2)
        assert capacity.snapshot()["main.long"]["in_flight"] == 1
        assert admission.lanes().long_output.active == 1
        sizes = [105 + (i * 36) // 19 for i in range(20)]

        async def document(i: int, kb: int):
            started = time.monotonic()
            plan = _plan(text=_document(kb))
            try:
                await _public_sync(plan, f"resp_doc_{i}", engine_tokens=20)
                return ("ok", time.monotonic() - started)
            except errors.ApiError as exc:
                return (exc.code, time.monotonic() - started)

        docs = [asyncio.ensure_future(document(i, kb)) for i, kb in enumerate(sizes)]
        await asyncio.sleep(0.05)
        busy_first = await _chat_first_token([{"role": "user", "content": "hello"}])
        results = await asyncio.gather(*docs)
        still_running = not job.done()
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        return idle_first, busy_first, results, still_running

    idle_first, busy_first, results, still_running = asyncio.run(scenario())
    assert still_running, "the 1M job must still be running while the documents finish"
    assert [code for code, _ in results] == ["ok"] * 20
    # 20 x 20 tokens at 10 ms each, nine at a time for /v1 (one NORMAL seat is
    # chat's): ~0.5 s total. Queued behind the job would be the job's 6 s.
    assert max(elapsed for _, elapsed in results) < 3.0
    assert busy_first < 0.5 and busy_first < idle_first + 0.3


# ----------------------------------------------------- one accounting --


def test_a_gated_answer_is_admitted_once_into_the_long_output_lane_and_given_back(fake_main):
    plan = _plan(100_000)
    assert plan.gate_engine == "main.extended"
    seen: List[tuple] = []

    async def scenario():

        lanes = admission.lanes()
        async with _bounded_gate(plan):
            before_call = (lanes.long_output.active, lanes.ledger.committed, len(lanes.ledger))
            generation = streaming.Generation(_spec(plan, "resp_once", max_tokens=30))
            async for chunk in generation.stream():
                if chunk.kind == streaming.TOKEN_KIND:
                    seen.append((lanes.long_output.active, lanes.long_output.decoding,
                                 lanes.normal.active, len(lanes.ledger)))
            await generation.aclose()
            in_block_after = (lanes.long_output.active, lanes.ledger.committed)
        return before_call, in_block_after, (lanes.long_output.active, lanes.ledger.committed,
                                             capacity.snapshot()["main.extended"]["in_flight"])

    before_call, in_block_after, after = asyncio.run(scenario())
    prompt = context.estimate_messages(plan.messages)
    assert before_call == (1, _block_charge(prompt, 100_000), 1)
    # During the generation: still ONE seat, ONE charge, nothing in NORMAL.
    # (The last chunk may be read after the producer, and so the ticket, has
    # already finished.)
    assert seen[0] == (1, 1, 0, 1) and set(seen[:-1]) == {(1, 1, 0, 1)}
    # The stream's end released the ticket; the gate's exit found nothing left.
    assert in_block_after == (0, 0)
    assert after == (0, 0, 0)


def test_a_gated_answer_whose_generation_never_runs_gives_its_seat_and_charge_back():
    plan = _plan(100_000)

    async def scenario():

        lanes = admission.lanes()
        with pytest.raises(RuntimeError):
            async with _bounded_gate(plan):
                assert lanes.long_output.active == 1
                raise RuntimeError("the row could not be written")
        return lanes.long_output.active, lanes.ledger.committed, capacity.snapshot()["main.extended"]["in_flight"]

    assert asyncio.run(scenario()) == (0, 0, 0)


def test_the_third_long_answer_is_a_real_503_before_the_status_line_after_the_seats_fill(fake_main):
    """ADMISSION_LONG_OUTPUT_MAX_SEQS (2) is the one cap: two 100k answers
    hold the seats, the third waits its gate bound and is refused with the
    long answer's Retry-After — no public counter of its own in front."""
    plan = _plan(100_000)

    async def scenario():

        holders = []
        for _ in range(2):
            gate = _bounded_gate(plan)
            await gate.__aenter__()
            holders.append(gate)
        started = time.monotonic()
        with pytest.raises(errors.ApiError) as refused:
            async with _bounded_gate(plan):
                pass
        waited = time.monotonic() - started
        snap = capacity.snapshot()["main.extended"]
        for gate in holders:
            await gate.__aexit__(None, None, None)
        return refused.value, waited, snap, admission.lanes().long_output.active

    refusal, waited, snap, after = asyncio.run(scenario())
    assert refusal.status == 503 and refusal.code == "model_unavailable"
    assert refusal.headers()["Retry-After"] == "60"
    assert 0.4 <= waited < 1.5
    assert snap["in_flight"] == 2 and snap["max_concurrent"] == 2
    assert after == 0


def test_the_routers_patient_gate_admits_a_long_answer_once_with_no_bound(fake_main):
    """The router's own gate since the no-timeout release, merged with PR #65's
    one accounting (2026-09-14): `router._patient_gate` takes `main.extended`
    — which pre-admits the answer into LONG_OUTPUT, patiently, because the
    router passes no wait_s — then `main.normal`. Two answers fill the
    LONG_OUTPUT seats; a third WAITS past the synchronous bound instead of the
    503 above, and is admitted into the seat the first one gives back."""
    from app.publicapi import router as public_router

    plan = _plan(100_000)

    async def scenario():
        lanes = admission.lanes()
        holders = []
        for _ in range(2):
            gate = public_router._patient_gate(plan)
            await gate.__aenter__()
            holders.append(gate)
        inside = (lanes.long_output.active, capacity.snapshot()["main.normal"]["in_flight"])

        async def third():
            async with public_router._patient_gate(plan):
                return lanes.long_output.active, capacity.snapshot()["main.normal"]["in_flight"]

        task = asyncio.create_task(third())
        await asyncio.sleep(1.2)  # more than twice the 0.5 s synchronous bound
        still_waiting = not task.done()
        await holders[0].__aexit__(None, None, None)
        async with asyncio.timeout(5):
            third_inside = await task
        await holders[1].__aexit__(None, None, None)
        return inside, still_waiting, third_inside, (
            lanes.long_output.active, lanes.ledger.committed, capacity.snapshot()["main.normal"]["in_flight"],
        )

    inside, still_waiting, third_inside, after = asyncio.run(scenario())
    assert inside == (2, 2)
    assert still_waiting is True
    assert third_inside == (2, 2)
    assert after == (0, 0, 0)


def test_a_background_cancel_while_waiting_for_the_lane_leaves_no_ticket_behind():
    plan = _plan(100_000)

    async def scenario():

        holders = []
        for _ in range(2):
            gate = _bounded_gate(plan)
            await gate.__aenter__()
            holders.append(gate)
        gone = asyncio.Event()

        async def cancel_soon():
            await asyncio.sleep(0.1)
            gone.set()

        asyncio.ensure_future(cancel_soon())
        with pytest.raises(capacity.Abandoned):
            async with capacity.hold("main.extended", wait_s=10, abandon=gone, work=plan):
                pass
        lanes = admission.lanes()
        waiting = lanes.long_output.waiting
        for gate in holders:
            await gate.__aexit__(None, None, None)
        return waiting, lanes.long_output.active, lanes.ledger.committed

    assert asyncio.run(scenario()) == (0, 0, 0)


def test_a_long_prompt_is_not_pre_admitted_the_long_lane_accounts_it_at_the_call(fake_main, monkeypatch):
    """A 450 KB prompt counted at 150k tokens with 100k of output: LONG by
    prompt. The gate takes no LONG_OUTPUT seat for it; the call takes the LONG
    lane once, with the whole KV charge."""
    text = "x" * 450_000
    plan = _plan(100_000, text=text)
    assert plan.gate_engine == "main.extended"
    seen: List[tuple] = []

    async def scenario():

        lanes = admission.lanes()
        async with _bounded_gate(plan):
            in_gate = (lanes.long_output.active, lanes.ledger.committed)
            generation = streaming.Generation(_spec(plan, "resp_longprompt", max_tokens=5))
            async for chunk in generation.stream():
                if chunk.kind == streaming.TOKEN_KIND:
                    seen.append((lanes.long.active, lanes.long_output.active, len(lanes.ledger)))
            await generation.aclose()
        return in_gate, (lanes.long.active, lanes.long_output.active, lanes.ledger.committed)

    in_gate, after = asyncio.run(scenario())
    assert in_gate == (0, 0)
    assert seen[0] == (1, 0, 1) and set(seen[:-1]) == {(1, 0, 1)}
    assert after == (0, 0, 0)


class _Held:
    """A stream `admission.run` wraps: holds its ticket until closed."""

    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return "chunk"

    async def close(self) -> None:
        self.closed = True


def test_a_pre_admitted_ticket_serves_one_call_and_never_a_retry_or_a_long_call():
    """admission.run takes the pre-admitted ticket once: a retry in the same
    task after the first stream ended is admitted on its own (never on a
    released ticket) — as the long answer it is, because the planned output
    travels with the ticket — and a call whose prompt turns out LONG releases
    the ticket and is accounted by the LONG lane alone, output included."""
    small = [{"role": "user", "content": "Write a long essay."}]
    large = [{"role": "user", "content": "x" * 450_000}]

    async def opener():
        return _Held()

    async def scenario():
        lanes = admission.lanes()
        admission.set_origin(admission.ORIGIN_V1)
        ticket = await admission.preadmit(small, base_url="http://engine/v1", model="m", max_tokens=100_000, wait_s=1)
        assert ticket is not None and ticket.lane == admission.LONG_OUTPUT
        with admission.use_preadmitted(ticket, max_tokens=100_000):
            # Each attempt in its own task, as a retry wrapper may run it: the
            # first attempt's "consumed" mark stays in ITS context, so only the
            # released flag keeps the retry off the ticket.
            async def attempt():
                return await admission.run(opener, messages=small, base_url="http://engine/v1", model="m", stream=True)

            first = await asyncio.get_running_loop().create_task(attempt())
            during_first = (lanes.long_output.active, lanes.normal.active, len(lanes.ledger))
            await first.close()
            retry = await asyncio.get_running_loop().create_task(attempt())
            during_retry = (ticket.released, lanes.long_output.active, lanes.normal.active, lanes.ledger.committed)
            await retry.close()

        second = await admission.preadmit(small, base_url="http://engine/v1", model="m", max_tokens=100_000, wait_s=1)
        with admission.use_preadmitted(second, max_tokens=100_000):
            long_call = await admission.run(opener, messages=large, base_url="http://engine/v1", model="m", stream=True)
            await long_call.__anext__()  # the first chunk reopens the lanes
            during_long = (second.released, lanes.long_output.active, lanes.long.active, len(lanes.ledger),
                           lanes.ledger.committed)
            await long_call.close()
        return during_first, during_retry, during_long, (lanes.long_output.active, lanes.long.active,
                                                         lanes.normal.active, lanes.ledger.committed)

    during_first, during_retry, during_long, after = asyncio.run(scenario())
    small_tokens = context.estimate_messages([{"role": "user", "content": "Write a long essay."}])
    large_tokens = context.estimate_messages([{"role": "user", "content": "x" * 450_000}])
    assert during_first == (1, 0, 1)
    assert during_retry == (True, 1, 0, _block_charge(small_tokens, 100_000))
    assert during_long == (True, 0, 1, 1, _block_charge(large_tokens, 100_000))
    assert after == (0, 0, 0, 0)


def test_a_synchronous_bound_is_a_wall_clock_even_behind_a_long_closure():
    """A LONG prefill holds the lanes closed; admission defers a lane's own
    bound behind a closure. The public 30 s (here 0.5 s) bound must not grow."""
    plan = _plan(100_000)

    async def scenario():
        lanes = admission.lanes()
        lanes.normal.closed = True
        lanes.long_output.closed = True
        started = time.monotonic()
        try:
            with pytest.raises(errors.ApiError):
                # yield disabled for this probe: the closure is what is tested
                async with capacity._hold_main("main.extended", wait_s=0.5, abandon=None, work=plan):
                    pass
        finally:
            lanes.normal.closed = False
            lanes.long_output.closed = False
        return time.monotonic() - started, lanes.long_output.waiting, lanes.ledger.committed

    capacity_probe = capacity.chat_long_admission_present
    try:
        capacity.chat_long_admission_present = lambda: False  # type: ignore[assignment]
        waited, waiting, committed = asyncio.run(scenario())
    finally:
        capacity.chat_long_admission_present = capacity_probe  # type: ignore[assignment]
    assert waited < 1.5
    assert waiting == 0 and committed == 0


def test_public_generations_are_v1_work_for_the_admission_lanes(fake_main):
    """The producer task marks its admission calls as /v1: chat keeps the
    NORMAL seat reserved for it, and grants go chat first."""
    plan = _plan()
    origins: List[str] = []
    real_run = admission.run

    async def spy(op, **kwargs):
        origins.append(admission.current_origin())
        return await real_run(op, **kwargs)

    async def scenario():
        admission.run = spy  # type: ignore[assignment]
        try:
            await streaming.run_to_completion(_spec(plan, "resp_origin", max_tokens=3))
            await _chat_first_token([{"role": "user", "content": "hi"}])
        finally:
            admission.run = real_run  # type: ignore[assignment]

    asyncio.run(scenario())
    assert origins == [admission.ORIGIN_V1, admission.ORIGIN_CHAT]


# -------------------------------------------------------------- the yield --


DOC = [{"role": "user", "content": "x" * 450_000}]  # ~150k tokens: the LONG lane


def test_a_chat_large_document_past_its_first_token_does_not_refuse_public_long_work(fake_main):
    """Rereview P1: a chat LONG turn that is already decoding needs nothing
    from public work; main.extended must be admitted beside it."""
    fake_main.cap = 400

    async def scenario():
        async def chat_document():
            async for _ in llm.stream_chat_events(DOC, model_choice="smart", effort="fast", max_tokens=400):
                pass

        turn = asyncio.ensure_future(chat_document())
        await asyncio.sleep(0.3)
        lanes = admission.lanes()
        state = (lanes.long.active, lanes.long_idle_waiting, lanes.normal.closed)
        present = capacity.chat_long_admission_present()
        async with capacity.hold("main.extended", wait_s=0.5, work=_plan(100_000)):
            admitted = admission.lanes().long_output.active
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        return state, present, admitted

    state, present, admitted = asyncio.run(scenario())
    assert state == (1, 0, False)
    assert present is False
    assert admitted == 1


def test_a_chat_large_document_still_waiting_for_the_idle_engine_holds_new_public_long_work(fake_main):
    fake_main.cap = 400

    async def scenario():
        async def busy():
            async for _ in llm.stream_chat_events(
                [{"role": "user", "content": "busy"}], model_choice="smart", effort="fast", max_tokens=400
            ):
                pass

        runner = asyncio.ensure_future(busy())
        await asyncio.sleep(0.2)
        turn = asyncio.ensure_future(_chat_first_token(DOC))
        await asyncio.sleep(0.3)
        present = capacity.chat_long_admission_present()
        started = time.monotonic()
        with pytest.raises(errors.ApiError) as refused:
            async with capacity.hold("main.extended", wait_s=0.4, work=_plan(100_000)):
                pass
        waited = time.monotonic() - started
        seats = admission.lanes().long_output.active
        for task in (runner, turn):
            task.cancel()
        await asyncio.gather(runner, turn, return_exceptions=True)
        return present, refused.value, waited, seats

    present, refusal, waited, seats = asyncio.run(scenario())
    assert present is True
    assert refusal.status == 503 and refusal.headers()["Retry-After"] == "60"
    assert waited >= 0.35 and seats == 0


def test_a_public_long_prompt_waiting_for_its_seat_does_not_make_public_work_yield():
    async def scenario():
        lanes = admission.lanes()
        loop = asyncio.get_running_loop()
        waiter = admission._Waiter(loop.create_future(), admission.ORIGIN_V1, 0, admission.LONG)
        lanes.long.queue.add(waiter)
        try:
            v1_only = capacity.chat_long_admission_present()
            chat = admission._Waiter(loop.create_future(), admission.ORIGIN_CHAT, 0, admission.LONG)
            lanes.long.queue.add(chat)
            with_chat = capacity.chat_long_admission_present()
            lanes.long.queue.remove(chat)
        finally:
            lanes.long.queue.remove(waiter)
        return v1_only, with_chat

    assert asyncio.run(scenario()) == (False, True)


# ------------------------------------------------------------ the KV rule --


@pytest.mark.parametrize("reserve", ["0.35", "0"])
def test_two_answers_planned_above_800k_never_run_together_whatever_the_reserve(fake_main, monkeypatch, reserve):
    """Pool 1,663,201 tokens. At the default reserve admission's budget
    (1,081,080) already refuses the second 1M charge (1,008,176); at reserve
    0 the budget IS the pool, and two 800,001-token answers (806,960 each,
    1,613,920 together) would fit it — the one-at-a-time gate refuses the
    second anyway, before the status line."""
    monkeypatch.setenv("ADMISSION_KV_RESERVE_FRACTION", reserve)
    plan = _plan(800_001)
    assert plan.gate_engine == "main.long"

    async def scenario():
        first = asyncio.ensure_future(_public_sync(plan, "resp_a", engine_tokens=300))
        await asyncio.sleep(0.2)
        started = time.monotonic()
        with pytest.raises(errors.ApiError) as refused:
            await _public_sync(plan, "resp_b", engine_tokens=5)
        waited = time.monotonic() - started
        lanes = admission.lanes()
        state = (lanes.long_output.active, lanes.ledger.committed, capacity.snapshot()["main.long"]["in_flight"])
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        return refused.value, waited, state

    refusal, waited, state = asyncio.run(scenario())
    assert refusal.status == 503 and refusal.headers()["Retry-After"] == "60"
    assert waited >= 0.4
    prompt = context.estimate_messages(plan.messages)
    assert state == (1, _block_charge(prompt, 800_001), 1)


def test_at_800k_and_below_the_second_answer_is_admissions_kv_decision_not_a_public_counter(fake_main, monkeypatch):
    """The boundary of the rule: 800,000 planned is `main.extended`, which has
    no count of its own. With the reserve at 0, whether a second one runs is
    exactly what admission's ledger says — two (806,960 KV tokens each) fit a
    budget equal to the pool; an admission with a managed limit below the pool
    (the capacity team's later version: 0.85 of it) refuses the second. Either
    way the public gauge mirrors admission's seats, and no public waiter queues."""
    monkeypatch.setenv("ADMISSION_KV_RESERVE_FRACTION", "0")
    plan = _plan(800_000)
    assert plan.gate_engine == "main.extended"

    async def scenario():

        lanes = admission.lanes()
        first = _bounded_gate(plan)
        await first.__aenter__()
        charge = lanes.ledger.committed
        pool = kv_budget.cached()
        limits = [kv_budget.budget_tokens(pool)]
        if "managed_limit_tokens" in inspect.getsource(admission):  # does THIS admission bound all commits?
            limits.append(kv_budget.managed_limit_tokens(pool))
        expected = 2 if 2 * charge <= min(limits) else 1
        second = _bounded_gate(plan)
        try:
            await second.__aenter__()
            admitted = True
        except errors.ApiError:
            admitted = False
        state = (lanes.long_output.active, capacity.snapshot()["main.extended"])
        if admitted:
            await second.__aexit__(None, None, None)
        await first.__aexit__(None, None, None)
        return charge, expected, admitted, state

    charge, expected, admitted, (seats, snap) = asyncio.run(scenario())
    assert charge == _block_charge(context.estimate_messages(plan.messages), 800_000) == 806_960
    assert admitted is (expected == 2)
    assert seats == snap["in_flight"] == expected
    assert snap["waiting"] == 0


def test_at_the_default_reserve_a_second_800k_answer_waits_for_kv_not_for_a_public_counter(fake_main):
    plan = _plan(800_000)

    async def scenario():

        first = _bounded_gate(plan)
        await first.__aenter__()
        with pytest.raises(errors.ApiError):
            async with _bounded_gate(plan):
                pass
        lanes = admission.lanes()
        state = (lanes.long_output.active, lanes.long_output.waiting)
        # A 100k answer still fits beside it (806,960 + 213,792 <= 1,081,080).
        async with _bounded_gate(_plan(100_000)):
            beside = lanes.long_output.active
        await first.__aexit__(None, None, None)
        return state, beside

    state, beside = asyncio.run(scenario())
    assert state == (1, 0)
    assert beside == 2


# ------------------------------------ integration review 2026-09-13 fixes --
#
# The single-accounting claim after a retry and for a LONG prompt (high), a
# pre-admitted call sent into a later LONG closure (medium), main.extended
# uncapped when admission does not take the answer (medium), a claimed ticket
# released from another context (low), and the chat-document policy (high).


class _FlakyMain(_FakeMain):
    """The engine refuses the first `fail_first` opens with a 503 before any
    chunk (a recoverable engine error), then serves."""

    def __init__(self, fail_first: int = 0, **kw: Any) -> None:
        super().__init__(**kw)
        self.fail_left = fail_first

    async def create(self, **request):
        import httpx
        import openai

        self.requests.append(request)
        if self.fail_left > 0:
            self.fail_left -= 1
            raise openai.InternalServerError(
                "engine died",
                response=httpx.Response(503, request=httpx.Request("POST", "http://main/v1/chat/completions")),
                body=None,
            )
        return _SlowStream(self, request)


@pytest.fixture()
def flaky_main(monkeypatch):
    from app import resilience

    fake = _FlakyMain(fail_first=1)
    monkeypatch.setattr(llm, "_client", fake.client)
    monkeypatch.setattr(resilience, "_backoff_s", lambda attempt: 0.01)
    # Production: LLM_INTERACTIVE_RECOVERY_S=120 (the suite's conftest sets 0).
    monkeypatch.setattr(settings, "llm_interactive_recovery_s", 5.0)
    return fake


def test_a_retry_after_a_recoverable_engine_error_is_admitted_into_long_output_again(flaky_main):
    """The first open fails before any chunk and releases the pre-admitted
    ticket; the retry used to be admitted NORMAL with a prompt-only charge."""
    plan = _plan(200_000)
    assert plan.gate_engine == "main.extended"

    async def scenario():

        lanes = admission.lanes()
        async with _bounded_gate(plan):
            task = asyncio.ensure_future(streaming.run_to_completion(_spec(plan, "resp_retry", max_tokens=400)))
            deadline = time.monotonic() + 3
            while not flaky_main.running and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            state = (len(flaky_main.requests), flaky_main.running, lanes.long_output.active,
                     lanes.normal.active, lanes.ledger.committed, lanes.ledger.normal_committed)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return state, (lanes.long_output.active, lanes.normal.active, lanes.ledger.managed)

    state, after = asyncio.run(scenario())
    prompt = context.estimate_messages(plan.messages)
    assert state == (2, 1, 1, 0, _block_charge(prompt, 200_000), 0)
    assert after == (0, 0, 0)


def test_retried_long_answers_never_run_beyond_the_long_output_seats(flaky_main):
    """Three 300k answers, two LONG_OUTPUT seats: the third is a 503 before its
    status line even when the first open of each answer fails once."""
    plan = _plan(300_000)

    async def scenario():

        gates, tasks, refused = [], [], 0
        for i in range(3):
            flaky_main.fail_left = 1
            gate = _bounded_gate(plan)
            try:
                await gate.__aenter__()
            except errors.ApiError:
                refused += 1
                continue
            gates.append(gate)
            tasks.append(asyncio.ensure_future(
                streaming.run_to_completion(_spec(plan, f"resp_seat_{i}", max_tokens=400))))
            deadline = time.monotonic() + 3
            while flaky_main.running < len(tasks) and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        lanes = admission.lanes()
        state = (flaky_main.running, refused, lanes.long_output.active, lanes.normal.active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for gate in gates:
            await gate.__aexit__(None, None, None)
        return state, flaky_main.peak

    state, peak = asyncio.run(scenario())
    assert state == (2, 1, 2, 0)
    assert peak <= admission.long_output_max_seqs()


def test_a_gated_answer_with_a_long_prompt_is_kv_charged_for_its_prompt_and_its_output(fake_main):
    plan = _plan(300_000, text=_document(700))
    assert plan.gate_engine == "main.extended"

    async def scenario():

        lanes = admission.lanes()
        async with _bounded_gate(plan):
            task = asyncio.ensure_future(streaming.run_to_completion(_spec(plan, "resp_longkv", max_tokens=400)))
            deadline = time.monotonic() + 3
            while not fake_main.running and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            state = (lanes.long.active, lanes.long_output.active, lanes.ledger.committed)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return state, lanes.ledger.committed

    state, after = asyncio.run(scenario())
    prompt = context.estimate_messages(plan.messages)
    assert state == (1, 0, _block_charge(prompt, 300_000))
    assert after == 0


def test_a_pre_admitted_call_waits_out_a_long_closure_raised_after_its_grant():
    """The ticket is granted before the status line and the call is sent later.
    A chat document admitted in between closes the lanes for its prefill; the
    pre-admitted call used to go straight in. It now waits for the document's
    first chunk — and, with no controller sample, its parked ticket does not
    count as work in front of the document either."""
    small = [{"role": "user", "content": "Write a very long essay about rivers."}]
    entered: List[float] = []

    async def scenario():
        lanes = admission.lanes()
        with admission.origin(admission.ORIGIN_V1):
            ticket = await admission.preadmit(small, base_url="http://engine/v1", model="m", max_tokens=200_000,
                                              wait_s=1)
        assert ticket is not None

        doc_stream = _Held()

        async def open_doc():
            return doc_stream

        async def open_public():
            entered.append(time.monotonic())
            return _Held()

        async def public_call():
            with admission.origin(admission.ORIGIN_V1), admission.use_preadmitted(ticket, max_tokens=200_000):
                return await admission.run(open_public, messages=small, base_url="http://engine/v1", model="m",
                                           stream=True)

        doc_task = asyncio.ensure_future(
            admission.run(open_doc, messages=DOC, base_url="http://engine/v1", model="m", stream=True))
        await asyncio.sleep(0.05)
        public_task = asyncio.ensure_future(public_call())
        doc = await asyncio.wait_for(doc_task, 3.0)  # admitted: the parked call is not on the engine
        await asyncio.sleep(0.2)
        during_prefill = (lanes.long_output.closed, bool(entered), lanes.long_output_parked, ticket.released)
        first_chunk_at = time.monotonic()
        await doc.__anext__()  # the document's first chunk reopens the lanes
        public = await asyncio.wait_for(public_task, 2.0)
        state = (lanes.long_output.active, lanes.long.active, entered[0] >= first_chunk_at)
        await public.close()
        await doc.close()
        return during_prefill, state, (lanes.long_output.active, lanes.long.active, lanes.ledger.committed)

    during_prefill, state, after = asyncio.run(scenario())
    assert during_prefill == (True, False, 1, False)
    assert state == (1, 1, True)
    assert after == (0, 0, 0)


def test_a_pre_admitted_call_behind_a_closure_is_refused_after_the_deferral_ceiling_and_releases(monkeypatch):
    monkeypatch.setattr(admission, "LONG_CLOSURE_MAX_S", 0.3)
    small = [{"role": "user", "content": "Write a long essay."}]

    async def opener():
        return _Held()

    async def scenario():
        lanes = admission.lanes()
        with admission.origin(admission.ORIGIN_V1):
            ticket = await admission.preadmit(small, base_url="http://engine/v1", model="m", max_tokens=100_000,
                                              wait_s=1)
        lanes.long_output.closed = True
        lanes.normal.closed = True
        started = time.monotonic()
        try:
            with admission.origin(admission.ORIGIN_V1), admission.use_preadmitted(ticket, max_tokens=100_000):
                with pytest.raises(admission.AdmissionRejected) as refused:
                    await asyncio.wait_for(
                        admission.run(opener, messages=small, base_url="http://engine/v1", model="m", stream=True),
                        3.0)
        finally:
            lanes.long_output.closed = False
            lanes.normal.closed = False
        return (refused.value.lane, refused.value.reason, time.monotonic() - started, ticket.released,
                lanes.long_output.active, lanes.ledger.committed, lanes.long_output_parked)

    lane, reason, waited, released, active, committed, parked = asyncio.run(scenario())
    assert (lane, reason) == ("long_output", "timeout")
    assert 0.25 <= waited < 1.5
    assert (released, active, committed, parked) == (True, 0, 0, 0)


def test_a_call_in_another_context_never_uses_or_releases_a_ticket_a_generation_claimed(fake_main, monkeypatch):
    """`_take_preadmitted` clears the ContextVar in the taker's context only;
    a LONG call in the gate's own context used to find the same ticket and
    release it while the generation still decoded on it."""
    monkeypatch.setattr(settings, "admission_long_wait_s", 0.3)
    fake_main.cap = 10_000
    plan = _plan(200_000)

    async def opener():
        return _Held()

    async def scenario():

        lanes = admission.lanes()
        async with _bounded_gate(plan):
            ticket = admission._preadmitted.get()
            gen = asyncio.ensure_future(streaming.run_to_completion(_spec(plan, "resp_claim", max_tokens=5_000)))
            deadline = time.monotonic() + 3
            while not fake_main.running and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            claimed = ticket.claimed
            with pytest.raises(admission.AdmissionRejected):
                # A LONG call beside a decoding long answer: refused (the rule),
                # and the generation's ticket untouched.
                await admission.run(opener, messages=DOC, base_url="http://engine/v1", model="m", stream=True)
            state = (claimed, ticket.released, lanes.long_output.active, fake_main.running,
                     lanes.ledger.committed)
            gen.cancel()
            await asyncio.gather(gen, return_exceptions=True)
        return state

    claimed, released, active, running, committed = asyncio.run(scenario())
    prompt = context.estimate_messages(plan.messages)
    assert (claimed, released, active, running) == (True, False, 1, 1)
    assert committed == _block_charge(prompt, 200_000)


def test_main_extended_keeps_its_own_count_when_admission_does_not_take_the_answer(monkeypatch):
    """ADMISSION_V1_LONG_OUTPUT_THRESHOLD_TOKENS raised above the planner's
    extended threshold: the answer is main.extended by the plan, NORMAL by
    admission, and nothing capped it — six 32k answers held the gate at once."""
    monkeypatch.setenv("ADMISSION_V1_LONG_OUTPUT_THRESHOLD_TOKENS", "65536")
    plan = _plan(32_000)
    assert plan.gate_engine == "main.extended"

    async def scenario():
        gates, refused = [], []
        for _ in range(3):
            gate = capacity.hold(plan.gate_engine, wait_s=0.2, work=plan)
            try:
                await gate.__aenter__()
                gates.append(gate)
            except errors.ApiError as exc:
                refused.append(exc)
        state = (len(gates), capacity.snapshot()["main.extended"]["in_flight"], admission.lanes().long_output.active)
        for gate in gates:
            await gate.__aexit__(None, None, None)
        return state, refused, capacity.snapshot()["main.extended"]["in_flight"]

    state, refused, after = asyncio.run(scenario())
    assert state == (2, 2, 0)
    assert len(refused) == 1 and refused[0].status == 503
    assert after == 0


def test_long_prompt_answers_are_capped_by_the_extended_count_too():
    """A LONG prompt is never pre-admitted, so no LONG_OUTPUT seat caps its
    gate: the gate's own count does (two), and the third is a 503."""
    plan = _plan(100_000, text="x" * 450_000)
    assert plan.gate_engine == "main.extended"

    async def scenario():
        gates, refused = [], 0
        for _ in range(3):
            gate = capacity.hold(plan.gate_engine, wait_s=0.2, work=plan)
            try:
                await gate.__aenter__()
                gates.append(gate)
            except errors.ApiError:
                refused += 1
        state = (len(gates), refused, capacity.snapshot()["main.extended"]["in_flight"],
                 admission.lanes().long_output.active)
        for gate in gates:
            await gate.__aexit__(None, None, None)
        return state, capacity.snapshot()["main.extended"]["in_flight"]

    assert asyncio.run(scenario()) == ((2, 1, 2, 0), 0)


# ------------------------------- chat documents beside public long answers --


def _policy(monkeypatch, value=None, hold=None):
    for name in ("ADMISSION_CHAT_LONG_BESIDE_V1_ANSWERS", "ADMISSION_V1_LONG_OUTPUT_CHAT_HOLD_S"):
        monkeypatch.delenv(name, raising=False)
    if value is not None:
        monkeypatch.setenv("ADMISSION_CHAT_LONG_BESIDE_V1_ANSWERS", value)
    if hold is not None:
        monkeypatch.setenv("ADMISSION_V1_LONG_OUTPUT_CHAT_HOLD_S", str(hold))


async def _chat_document_outcome(messages, *, max_tokens=5):
    import contextvars

    outcome: Dict[str, Any] = {}

    async def turn():
        started = time.monotonic()
        events = llm.stream_chat_events(messages, model_choice="smart", effort="fast", max_tokens=max_tokens)
        try:
            async for _kind, _delta in events:
                outcome["result"] = "first token"
                outcome["after_s"] = time.monotonic() - started
                break
        except admission.AdmissionRejected as exc:
            outcome["result"] = f"refused:{exc.lane}:{exc.reason}"
            outcome["after_s"] = time.monotonic() - started
        finally:
            await events.aclose()  # the document's ticket goes back now, not at garbage collection

    # A chat turn: its own context, not the public gate's.
    await asyncio.get_running_loop().create_task(turn(), context=contextvars.Context())
    return outcome


@pytest.mark.parametrize("policy", [None, "refuse", "proceed"])
def test_a_chat_document_beside_a_decoding_public_1m_answer_follows_the_owner_policy(fake_main, monkeypatch, policy):
    """The integration review's high finding: with the hand-off a public 1M
    answer holds 1,008,176 of the 1,081,080-token budget and is a decoding
    long answer, so a 143k-token chat document is refused for the answer's
    whole life. `refuse` (the default) keeps that — the GDN rule — and holds
    the next public long answer instead; `proceed` lets the document in at
    once, within the managed limit."""
    _policy(monkeypatch, policy)
    monkeypatch.setattr(settings, "admission_long_wait_s", 0.6)
    fake_main.cap = 100_000
    plan = _plan(1_000_000)
    assert plan.gate_engine == "main.long"

    async def scenario():

        lanes = admission.lanes()
        async with _bounded_gate(plan):
            job = asyncio.ensure_future(streaming.run_to_completion(_spec(plan, "resp_1m", max_tokens=100_000)))
            deadline = time.monotonic() + 3
            while not lanes.long_output.decoding and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            outcome = await _chat_document_outcome([{"role": "user", "content": _document(430)}])
            state = (lanes.long_output.active, lanes.long_output.decoding, lanes.ledger.committed,
                     lanes.v1_long_output_held())
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        return outcome, state

    outcome, (active, decoding, committed, held) = asyncio.run(scenario())
    prompt = context.estimate_messages(plan.messages)
    assert (active, decoding) == (1, 1)
    if policy == "proceed":
        assert outcome["result"] == "first token" and outcome["after_s"] < 0.4
        assert held is False
    else:
        assert outcome["result"] == "refused:long:timeout" and outcome["after_s"] >= 0.55
        assert committed == _block_charge(prompt, 1_000_000) and held is True


def test_after_a_refused_chat_document_new_public_long_answers_are_held_until_a_document_runs(fake_main, monkeypatch):
    """BACK TO BACK: one key's consecutive 1M jobs used to keep chat documents
    out indefinitely. After a refusal, the next public long answer is a 503
    before its status line; once a chat document is admitted it runs again."""
    _policy(monkeypatch)
    monkeypatch.setattr(settings, "admission_long_wait_s", 0.4)
    fake_main.cap = 100_000
    answer = _plan(100_000)
    document = [{"role": "user", "content": _document(430)}]

    async def scenario():

        lanes = admission.lanes()
        gate = _bounded_gate(answer)
        await gate.__aenter__()
        job = asyncio.ensure_future(streaming.run_to_completion(_spec(answer, "resp_job", max_tokens=100_000)))
        deadline = time.monotonic() + 3
        while not lanes.long_output.decoding and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        refused = await _chat_document_outcome(document)
        with pytest.raises(errors.ApiError) as next_job:
            async with _bounded_gate(answer):
                pass
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        await gate.__aexit__(None, None, None)
        held_after_job = lanes.v1_long_output_held()
        retried = await _chat_document_outcome(document)
        held_after_document = lanes.v1_long_output_held()
        async with _bounded_gate(answer):
            admitted = lanes.long_output.active
        return refused, next_job.value, held_after_job, retried, held_after_document, admitted

    refused, next_job, held_after_job, retried, held_after_document, admitted = asyncio.run(scenario())
    assert refused["result"] == "refused:long:timeout"
    assert next_job.status == 503 and next_job.headers()["Retry-After"] == "60"
    assert held_after_job is True
    assert retried["result"] == "first token"
    assert held_after_document is False and admitted == 1


@pytest.mark.parametrize("hold", ["0", "0.3"])
def test_the_hold_is_bounded_and_zero_turns_it_off(fake_main, monkeypatch, hold):
    _policy(monkeypatch, hold=hold)
    monkeypatch.setattr(settings, "admission_long_wait_s", 0.3)
    fake_main.cap = 100_000
    answer = _plan(100_000)

    async def scenario():

        lanes = admission.lanes()
        async with _bounded_gate(answer):
            job = asyncio.ensure_future(streaming.run_to_completion(_spec(answer, "resp_h", max_tokens=100_000)))
            deadline = time.monotonic() + 3
            while not lanes.long_output.decoding and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            refused = await _chat_document_outcome([{"role": "user", "content": _document(430)}])
            held = lanes.v1_long_output_held()
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        await asyncio.sleep(0.35)
        return refused, held, lanes.v1_long_output_held()

    refused, held, later = asyncio.run(scenario())
    assert refused["result"] == "refused:long:timeout"
    assert held is (hold != "0")
    assert later is False


def test_proceed_still_refuses_a_document_the_managed_limit_cannot_hold(fake_main, monkeypatch):
    """950k tokens beside a 1M answer is 1.95M KV tokens against a 1.41M
    managed limit: `proceed` never overcommits the engine."""
    _policy(monkeypatch, "proceed")
    monkeypatch.setattr(settings, "admission_long_wait_s", 0.3)
    fake_main.cap = 100_000
    plan = _plan(1_000_000)
    small = [{"role": "user", "content": "Write a long essay."}]

    async def scenario():

        lanes = admission.lanes()
        async with _bounded_gate(plan):
            job = asyncio.ensure_future(streaming.run_to_completion(_spec(plan, "resp_limit", max_tokens=100_000)))
            deadline = time.monotonic() + 3
            while not lanes.long_output.decoding and time.monotonic() < deadline:
                await asyncio.sleep(0.01)

            async def opener():
                return _Held()

            with pytest.raises(admission.AdmissionRejected) as refused:
                # A 950k-token CHAT document (the prompt count planted below);
                # the gate's own context is /v1 work, so name the origin.
                with admission.origin(admission.ORIGIN_CHAT):
                    await admission.run(opener, messages=small, base_url="http://engine/v1", model="m",
                                        stream=True, max_tokens=65_536)
            state = (lanes.long.active, lanes.ledger.committed)
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        return refused.value, state

    async def planted(messages, *, base_url, model):
        return 950_000 if messages[0]["content"] == "Write a long essay." else context.estimate_messages(messages)

    monkeypatch.setattr(admission, "prompt_tokens", planted)
    refusal, (long_active, committed) = asyncio.run(scenario())
    prompt = context.estimate_messages(plan.messages)
    assert (refusal.lane, refusal.reason) == ("long", "timeout")
    assert long_active == 0 and committed == _block_charge(prompt, 1_000_000)


def test_proceed_keeps_the_rule_for_public_documents_and_for_chats_own_long_answers(monkeypatch):
    """`proceed` is about chat documents beside PUBLIC long answers only."""
    _policy(monkeypatch, "proceed")
    monkeypatch.setattr(settings, "admission_long_wait_s", 0.3)
    small = [{"role": "user", "content": "Write a long essay."}]

    async def opener():
        return _Held()

    async def one(answer_origin, document_origin):
        lanes = admission.lanes()
        with admission.origin(answer_origin):
            answer = await admission.run(opener, messages=small, base_url="http://engine/v1", model="m", stream=True,
                                         max_tokens=70_000)
        await answer.__anext__()  # decoding
        try:
            with admission.origin(document_origin):
                doc = await asyncio.wait_for(
                    admission.run(opener, messages=DOC, base_url="http://engine/v1", model="m", stream=True), 3.0)
            await doc.close()
            result = "admitted"
        except admission.AdmissionRejected as exc:
            result = exc.reason
        await answer.close()
        return result, lanes.long_output.decoding

    async def scenario():
        return (await one(admission.ORIGIN_CHAT, admission.ORIGIN_CHAT),
                await one(admission.ORIGIN_V1, admission.ORIGIN_V1),
                await one(admission.ORIGIN_V1, admission.ORIGIN_CHAT))

    chat_answer, v1_document, chat_document = asyncio.run(scenario())
    assert chat_answer == ("timeout", 0)
    assert v1_document == ("timeout", 0)
    assert chat_document == ("admitted", 0)


def test_a_malformed_policy_fails_loudly_and_describe_shows_the_policy(monkeypatch):
    _policy(monkeypatch, "sometimes")
    with pytest.raises(ValueError):
        admission.chat_long_beside_v1_answers()
    _policy(monkeypatch, "Proceed")
    assert admission.chat_long_beside_v1_answers() == "proceed"

    async def scenario():
        admission.lanes()
        return admission.describe()["chat_documents_beside_v1_answers"]

    shown = asyncio.run(scenario())
    assert shown == {"policy": "proceed", "v1_long_answers_held": False}
