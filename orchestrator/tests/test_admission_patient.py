"""app/admission.py — PATIENT waiters (no-timeout /v1 design, revision 2, 2026-09-13).

Offline: the real lanes, fake engine calls, the prompt size planted, the
controller's engine sample planted. Waits scaled to fractions of a second.
What is pinned:

- 3,000 patient waiters ahead of a chat waiter: chat is admitted on the next
  release, and each release grants exactly one waiter;
- patient tails do not poll: 3,000 of them cost a handful of loop wake-ups a
  second, not 3,000;
- a patient waiter outlives ADMISSION_NORMAL_WAIT_S (and a per-request
  set_wait_bound_s) where a chat waiter times out;
- ADMISSION_MAX_WAITING counts chat waiters only: at 0 it refuses chat at the
  door and queues patient work;
- a cancelled patient waiter leaves the queue and holds nothing;
- a patient LONG request never refuses on the idle bound — it keeps waiting
  where a bounded one is refused — and gives its seat back to a chat document
  that arrives while it waits for idle;
- PATIENT IDLE WAITERS NEVER HOLD CHAT (T1 review, 2026-09-14): a patient LONG
  request waiting for idle past its bound holds /v1 long answers but admits a
  chat long answer beside it at once (a chat LONG idle waiter still holds
  both); it gives its KV back to a chat long answer only its charge keeps out,
  and never gives way to a chat request something else holds — no requeue
  churn, no spin.
"""
from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from app import admission, continuity, engine_state, kv_budget, metrics
from app.config import settings

MAIN_URL = "http://vllm-main.test:8000/v1"
V1 = admission.ORIGIN_V1
CHAT = admission.ORIGIN_CHAT
HOLD_MAX_S = 5.0


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    metrics.reset()
    admission.reset()
    engine_state.reset()
    continuity.reset()
    monkeypatch.setattr(settings, "model_max_context", 1_000_000)
    monkeypatch.setattr(settings, "admission_long_threshold_tokens", 131_072)
    monkeypatch.setattr(settings, "admission_normal_max", 1)
    monkeypatch.setattr(settings, "admission_long_max", 1)
    monkeypatch.setattr(settings, "admission_long_idle_max", 0)
    monkeypatch.setattr(settings, "admission_long_wait_s", 0.2)
    monkeypatch.setattr(settings, "admission_normal_wait_s", 0.2)
    monkeypatch.setattr(settings, "admission_max_waiting", 100)
    monkeypatch.setattr(settings, "admission_long_output_wait_s", 0.2, raising=False)
    monkeypatch.setattr(settings, "admission_kv_metrics_url", "off", raising=False)
    monkeypatch.setattr(admission, "_POLL_S", 0.02)

    async def planted_prompt_tokens(messages, *, base_url, model):
        return int(messages[0]["content"])

    monkeypatch.setattr(admission, "prompt_tokens", planted_prompt_tokens)

    async def no_fetch(url):
        raise RuntimeError("no engine in this test")

    monkeypatch.setattr(kv_budget, "_fetch_text", no_fetch)
    yield
    admission.reset()
    engine_state.reset()
    continuity.reset()
    metrics.reset()


def _sample(running: int) -> None:
    engine_state._record(
        engine_state.parse_state_document(
            {"schema": 1, "generated_at": time.time(), "state": "BUSY" if running else "READY",
             "state_code": 3 if running else 2, "reason": "t",
             "signals": {"engine": {"requests_running": running, "requests_waiting": 0}}},
            observed_at=time.monotonic(),
        ),
        "",
    )


class _Call:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self):
        self.entered.set()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.release.wait(), HOLD_MAX_S)
        return "answer"


async def _run(call, *, origin=CHAT, prompt: int = 100, max_tokens=None, patient=None, bound=None):
    with admission.origin(origin):
        token = admission.set_wait_bound_s(bound) if bound is not None else None
        try:
            return await admission.run(call, messages=[{"role": "user", "content": str(prompt)}],
                                       base_url=MAIN_URL, model="m", max_tokens=max_tokens, patient=patient)
        finally:
            if token is not None:
                admission._wait_bound.reset(token)


def test_a_chat_waiter_behind_3000_patient_waiters_is_admitted_on_the_next_release():
    async def run():
        ls = admission.lanes()
        lane = ls.normal
        lane.take(CHAT)  # the one seat is busy
        waiters = [asyncio.ensure_future(lane.acquire(origin=V1, timeout=0.2, on_wait=None, patient_=True))
                   for _ in range(3000)]
        await asyncio.sleep(0.05)
        chat = asyncio.ensure_future(lane.acquire(origin=CHAT, timeout=5.0, on_wait=None))
        await asyncio.sleep(0.05)
        assert lane.queue.depth(V1) == 3000 and lane.queue.depth(CHAT) == 1
        lane.release_nowait(CHAT)
        await asyncio.sleep(0.05)
        assert chat.done() and not chat.cancelled(), "chat first on the release"
        assert sum(1 for w in waiters if w.done()) == 0, "exactly one grant for one release"
        # Each further release grants exactly one patient waiter, in order.
        lane.release_nowait(CHAT)
        await asyncio.sleep(0.02)
        assert [i for i, w in enumerate(waiters) if w.done()] == [0]
        lane.release_nowait(V1)
        await asyncio.sleep(0.02)
        assert [i for i, w in enumerate(waiters) if w.done()] == [0, 1]
        assert lane.active == 1
        for w in waiters:
            w.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        assert lane.queue.total() == 0

    asyncio.run(run())


def test_patient_tails_do_not_wake_the_loop_every_poll(monkeypatch):
    """The per-waiter poll is for the heads; 3,000 tails polling every tick
    would be the thundering herd the synchronous grant replaced."""
    ticks = {"n": 0}
    real_tick = admission.Lanes.tick

    def counting_tick(self):
        ticks["n"] += 1
        return real_tick(self)

    monkeypatch.setattr(admission.Lanes, "tick", counting_tick)

    async def run():
        lane = admission.lanes().normal
        lane.take(CHAT)
        waiters = [asyncio.ensure_future(lane.acquire(origin=V1, timeout=1.0, on_wait=None, patient_=True))
                   for _ in range(3000)]
        await asyncio.sleep(0.1)
        ticks["n"] = 0
        await asyncio.sleep(0.5)  # 25 poll periods of 0.02 s
        observed = ticks["n"]
        for w in waiters:
            w.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        return observed

    observed = asyncio.run(run())
    # Only the head polls: ~25 ticks. 3,000 polling tails would be ~75,000.
    assert observed <= 60, observed


def test_a_patient_waiter_outlives_the_normal_wait_bound_where_chat_times_out():
    async def run():
        holder = _Call()
        first = asyncio.ensure_future(_run(holder))
        await asyncio.wait_for(holder.entered.wait(), 1.0)
        chat = asyncio.ensure_future(_run(_Call()))
        patient_call = _Call()
        patient_call.release.set()
        patient = asyncio.ensure_future(_run(patient_call, origin=V1, patient=True))
        with pytest.raises(admission.AdmissionRejected) as refused:
            await asyncio.wait_for(chat, 2.0)
        assert refused.value.reason == "timeout"
        await asyncio.sleep(0.4)  # twice the 0.2 s bound
        assert not patient.done(), "patient: still waiting, never refused"
        holder.release.set()
        assert await asyncio.wait_for(patient, 2.0) == "answer"
        await first

    asyncio.run(run())


def test_a_patient_context_waits_through_a_per_request_bound():
    async def run():
        holder = _Call()
        first = asyncio.ensure_future(_run(holder))
        await asyncio.wait_for(holder.entered.wait(), 1.0)
        call = _Call()
        call.release.set()
        with admission.as_patient():
            assert admission.patient() is True
            task = asyncio.ensure_future(_run(call, origin=V1, bound=0.05))
        assert admission.patient() is False
        await asyncio.sleep(0.3)
        assert not task.done()
        holder.release.set()
        assert await asyncio.wait_for(task, 2.0) == "answer"
        await first

    asyncio.run(run())


def test_max_waiting_zero_refuses_chat_at_the_door_and_queues_patient_work(monkeypatch):
    monkeypatch.setattr(settings, "admission_max_waiting", 0)

    async def run():
        lane = admission.lanes().normal
        lane.take(CHAT)
        patient = [asyncio.ensure_future(lane.acquire(origin=V1, timeout=1.0, on_wait=None, patient_=True))
                   for _ in range(50)]
        await asyncio.sleep(0.02)
        assert all(not w.done() for w in patient)
        with pytest.raises(admission.AdmissionRejected) as refused:
            await lane.acquire(origin=CHAT, timeout=1.0, on_wait=None)
        assert refused.value.reason == "capacity"
        # A bounded /v1 waiter is refused too: only patience is exempt.
        with pytest.raises(admission.AdmissionRejected):
            await lane.acquire(origin=V1, timeout=1.0, on_wait=None)
        for w in patient:
            w.cancel()
        await asyncio.gather(*patient, return_exceptions=True)

    asyncio.run(run())


def test_a_cancelled_patient_waiter_leaves_the_queue_and_holds_nothing():
    async def run():
        lane = admission.lanes().normal
        lane.take(CHAT)
        a = asyncio.ensure_future(lane.acquire(origin=V1, timeout=1.0, on_wait=None, patient_=True))
        b = asyncio.ensure_future(lane.acquire(origin=V1, timeout=1.0, on_wait=None, patient_=True))
        await asyncio.sleep(0.02)
        a.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await a
        assert lane.queue.depth(V1) == 1
        lane.release_nowait(CHAT)
        await asyncio.wait_for(b, 1.0)
        assert lane.active == 1 and lane.active_by_origin[V1] == 1
        lane.release_nowait(V1)
        assert lane.active == 0

    asyncio.run(run())


def test_a_patient_long_request_keeps_waiting_where_a_bounded_one_is_refused():
    """The bound runs out with our own long answer on the engine: a bounded
    LONG request is refused `timeout`; a patient one waits for idle."""

    async def run():
        ls = admission.lanes()
        decoder = _Call()
        decode_task = asyncio.ensure_future(_run(decoder, prompt=100, max_tokens=100_000))
        await asyncio.wait_for(decoder.entered.wait(), 1.0)
        assert ls.long_output.active == 1
        _sample(1)
        with pytest.raises(admission.AdmissionRejected) as refused:
            await asyncio.wait_for(_run(_Call(), prompt=200_000), 3.0)
        assert refused.value.reason == "timeout"
        call = _Call()
        call.release.set()
        patient = asyncio.ensure_future(_run(call, origin=V1, prompt=200_000, patient=True))
        for _ in range(30):  # 0.6 s: three idle bounds
            _sample(1)
            await asyncio.sleep(0.02)
        assert not patient.done(), "patient: waiting for idle, never refused"
        decoder.release.set()
        await decode_task
        _sample(0)
        assert await asyncio.wait_for(patient, 2.0) == "answer"
        assert ls.long.active == 0 and ls.ledger.committed == 0

    asyncio.run(run())


def test_a_patient_long_request_waiting_for_idle_gives_its_seat_to_a_chat_document():
    async def run():
        ls = admission.lanes()
        _sample(3)  # the engine is busy: the patient request holds the seat, waiting for idle
        public = _Call()
        public.release.set()
        public_task = asyncio.ensure_future(_run(public, origin=V1, prompt=200_000, patient=True))
        for _ in range(20):
            await asyncio.sleep(0.01)
            if ls.long.active == 1:
                break
        assert ls.long.active == 1 and ls.long_idle_waiting == 1
        document = _Call()
        chat_task = asyncio.ensure_future(_run(document, prompt=300_000))
        await asyncio.sleep(0.1)
        # The patient request gave seat and KV back; the chat document has them.
        assert ls.long.active_by_origin[CHAT] == 1 and ls.long.active_by_origin[V1] == 0
        assert metrics._counters["llm_admission_patient_requeues_total"][()] == 1.0
        _sample(0)
        await asyncio.wait_for(document.entered.wait(), 2.0)
        assert not public.entered.is_set(), "the public request queued behind the document"
        document.release.set()
        await chat_task
        assert await asyncio.wait_for(public_task, 2.0) == "answer"

    asyncio.run(run())


def test_a_patient_long_output_request_ignores_a_per_request_bound(monkeypatch):
    monkeypatch.setattr(settings, "admission_long_output_max_seqs", 1, raising=False)

    async def run():
        ls = admission.lanes()
        holder = _Call()
        first = asyncio.ensure_future(_run(holder, origin=V1, max_tokens=100_000))
        await asyncio.wait_for(holder.entered.wait(), 1.0)
        with pytest.raises(admission.AdmissionRejected):
            await asyncio.wait_for(_run(_Call(), origin=V1, max_tokens=100_000, bound=0.05), 2.0)
        call = _Call()
        call.release.set()
        task = asyncio.ensure_future(_run(call, origin=V1, max_tokens=100_000, bound=0.05, patient=True))
        await asyncio.sleep(0.3)
        assert not task.done()
        holder.release.set()
        await first
        assert await asyncio.wait_for(task, 2.0) == "answer"
        assert ls.long_output.active == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# PATIENT IDLE WAITERS NEVER HOLD CHAT (T1 review, 2026-09-14)
# ---------------------------------------------------------------------------


def test_a_chat_long_answer_is_admitted_beside_a_patient_long_request_waiting_past_its_idle_bound(monkeypatch):
    """The review's proof, inverted: before the fix the chat answer was refused
    `timeout` after its whole 0.5 s bound while the patient request held
    LONG_OUTPUT with no limit. A /v1 long answer is still held (public FIFO)."""
    monkeypatch.setattr(settings, "admission_long_output_max_seqs", 3, raising=False)
    monkeypatch.setattr(settings, "admission_long_output_wait_s", 0.5, raising=False)

    async def run():
        ls = admission.lanes()
        decoder = _Call()  # a chat long answer, decoding
        decode_task = asyncio.ensure_future(_run(decoder, prompt=100, max_tokens=100_000))
        await asyncio.wait_for(decoder.entered.wait(), 1.0)
        _sample(2)
        public = _Call()
        public.release.set()
        public_task = asyncio.ensure_future(_run(public, origin=V1, prompt=200_000, patient=True))
        await asyncio.sleep(0.3)  # past ADMISSION_LONG_WAIT_S (0.2 s here): it waits again
        assert ls.long_idle_waiting == 1 and ls.long_idle_waiting_patient == 1
        public_answer = _Call()
        public_answer.release.set()
        public_answer_task = asyncio.ensure_future(
            _run(public_answer, origin=V1, prompt=100, max_tokens=100_000, patient=True))
        await asyncio.sleep(0.1)
        assert not public_answer.entered.is_set(), "a /v1 long answer is still held by the idle waiter"
        started = time.monotonic()
        answer = _Call()
        answer.release.set()
        assert await asyncio.wait_for(_run(answer, prompt=100, max_tokens=100_000), 2.0) == "answer"
        assert time.monotonic() - started < 0.2, "admitted at once, not after the chat bound"
        assert not public_task.done(), "the patient request still waits for idle"
        assert not public_answer.entered.is_set()
        decoder.release.set()
        await decode_task
        _sample(0)
        assert await asyncio.wait_for(public_task, 2.0) == "answer"
        assert await asyncio.wait_for(public_answer_task, 2.0) == "answer"
        assert ls.long_idle_waiting == 0 and ls.long_idle_waiting_patient == 0
        assert ls.long_output.active == 0 and ls.ledger.committed == 0

    asyncio.run(run())


def test_a_chat_long_request_waiting_for_idle_still_holds_every_long_answer(monkeypatch):
    """LONG BESIDE LONG ANSWERS is unchanged for a chat document."""
    monkeypatch.setattr(settings, "admission_long_wait_s", 1.0)
    monkeypatch.setattr(settings, "admission_long_output_wait_s", 2.0, raising=False)

    async def run():
        ls = admission.lanes()
        _sample(3)
        document = _Call()
        document.release.set()
        document_task = asyncio.ensure_future(_run(document, prompt=300_000))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if ls.long_idle_waiting == 1:
                break
        assert ls.long_idle_waiting == 1 and ls.long_idle_waiting_patient == 0
        answer = _Call()
        answer.release.set()
        answer_task = asyncio.ensure_future(_run(answer, prompt=100, max_tokens=100_000))
        await asyncio.sleep(0.2)
        assert not answer.entered.is_set(), "a chat idle waiter holds chat long answers"
        _sample(0)
        assert await asyncio.wait_for(document_task, 2.0) == "answer"
        assert await asyncio.wait_for(answer_task, 2.0) == "answer"

    asyncio.run(run())


def test_a_patient_long_request_waiting_for_idle_gives_its_kv_to_a_chat_long_answer_only_its_charge_keeps_out(
    monkeypatch,
):
    monkeypatch.setattr(settings, "admission_long_output_wait_s", 2.0, raising=False)

    async def run():
        ls = admission.lanes()
        budget = kv_budget.budget_tokens(kv_budget.cached())
        _sample(3)  # busy: the patient request holds seat and KV, waiting for idle
        public = _Call()
        public.release.set()
        public_task = asyncio.ensure_future(_run(public, origin=V1, prompt=900_000, patient=True))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if ls.long.active == 1:
                break
        public_charge = ls.ledger.committed
        answer_charge = kv_budget.charge_tokens(100, 300_000, block_size=kv_budget.cached().block_size,
                                                window=1_000_000)
        assert public_charge + answer_charge > budget >= answer_charge
        answer = _Call()
        answer_task = asyncio.ensure_future(_run(answer, prompt=100, max_tokens=300_000))
        await asyncio.wait_for(answer.entered.wait(), 1.0)
        assert metrics._counters["llm_admission_patient_requeues_total"][()] == 1.0
        assert ls.long.active_by_origin[V1] == 0 and ls.ledger.committed == answer_charge
        assert not public.entered.is_set(), "the public request queued behind the chat answer"
        await asyncio.sleep(0.2)
        assert metrics._counters["llm_admission_patient_requeues_total"][()] == 1.0, "gave way once"
        answer.release.set()
        await answer_task
        _sample(0)
        assert await asyncio.wait_for(public_task, 2.0) == "answer"
        assert ls.ledger.committed == 0 and ls.long.active == 0

    asyncio.run(run())


def test_a_patient_long_request_never_gives_way_to_a_chat_request_something_else_holds(monkeypatch):
    """A chat long answer waits for its seat (a chat decoder has it) and is the
    protected head; a chat document behind it could take the LONG seat only by
    breaking that protection. The patient request's seat and KV are not what
    keeps either out, so it gives nothing back — the "any chat waiter" rule
    re-granted it at once and spun the loop without an await."""
    monkeypatch.setattr(settings, "admission_long_output_max_seqs", 1, raising=False)
    monkeypatch.setattr(settings, "admission_long_output_wait_s", 3.0, raising=False)
    monkeypatch.setattr(settings, "admission_long_wait_s", 3.0)

    async def run():
        ls = admission.lanes()
        decoder = _Call()
        decode_task = asyncio.ensure_future(_run(decoder, prompt=100, max_tokens=70_000))
        await asyncio.wait_for(decoder.entered.wait(), 1.0)
        _sample(1)
        public = _Call()
        public.release.set()
        public_task = asyncio.ensure_future(_run(public, origin=V1, prompt=200_000, patient=True))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if ls.long.active == 1:
                break
        assert ls.long.active_by_origin[V1] == 1
        seatless = _Call()
        seatless.release.set()
        seatless_task = asyncio.ensure_future(_run(seatless, prompt=100, max_tokens=700_000))
        document = _Call()
        document.release.set()
        document_task = asyncio.ensure_future(_run(document, prompt=500_000))
        started = time.monotonic()
        await asyncio.sleep(0.4)  # 20 polls
        assert time.monotonic() - started < 1.0, "the loop is not spinning"
        assert ("llm_admission_patient_requeues_total" not in metrics._counters
                or metrics._counters["llm_admission_patient_requeues_total"].get((), 0.0) == 0.0)
        assert ls.long.active_by_origin[V1] == 1, "the patient request kept its seat"
        assert not seatless.entered.is_set() and not document.entered.is_set()
        for task in (decode_task, public_task, seatless_task, document_task):
            task.cancel()
        await asyncio.gather(decode_task, public_task, seatless_task, document_task, return_exceptions=True)
        assert ls.ledger.committed == 0 and ls.long.active == 0 and ls.long_output.active == 0

    asyncio.run(run())


def test_however_the_give_way_rule_errs_a_patient_request_gives_way_at_most_once_per_poll(monkeypatch):
    """Belt and braces for the rule above: were `_patient_idle_gives_way` ever
    wrong, a give-back that is re-granted at once needs no await, and without
    the once-per-poll guard it spun the event loop."""
    monkeypatch.setattr(admission, "_patient_idle_gives_way", lambda ls, ticket: True)

    async def run():
        ls = admission.lanes()
        _sample(3)
        public = _Call()
        public.release.set()
        public_task = asyncio.ensure_future(_run(public, origin=V1, prompt=200_000, patient=True))
        started = time.monotonic()
        await asyncio.sleep(0.2)  # 10 polls of 0.02 s
        elapsed = time.monotonic() - started
        requeues = metrics._counters.get("llm_admission_patient_requeues_total", {}).get((), 0.0)
        assert elapsed < 0.5, f"the loop kept running ({elapsed:.3f}s)"
        assert 1 <= requeues <= elapsed / admission._POLL_S + 2, requeues
        _sample(0)
        assert await asyncio.wait_for(public_task, 2.0) == "answer"
        assert ls.long.active == 0 and ls.ledger.committed == 0

    asyncio.run(run())
