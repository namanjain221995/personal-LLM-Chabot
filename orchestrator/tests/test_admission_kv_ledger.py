"""app/admission.py — patient LONG holders, the patient KV ledger and YIELD
(no-timeout /v1 design, revision 2, 2026-09-13).

Offline: the real lanes and KV arbiter on the 2026-09-13 numbers (a
1,663,201-token pool in 2096-token blocks, budget 1,081,080), fake engine
streams that yield only when told, the controller's engine sample planted.
What is pinned:

- a patient LONG ticket gives its LONG seat back at its first token and keeps
  its KV charge and its ledger entry until the stream ends (and holds the seat
  when PUBLIC_API_LONG_RELEASE_AT_FIRST_TOKEN is off);
- a chat document that fits beside a public decode is admitted without a yield
  when the decode may not be suspended (PUBLIC_API_DECODE_YIELDS_PER_RUN=0),
  at the production ADMISSION_LONG_IDLE_MAX of 0;
- a chat document that does not fit (KV, or PUBLIC_API_SHARED_KV_BUDGET_TOKENS)
  asks the patient holder to yield, and is admitted once it did;
- a chat document arriving during a public PREFILL yields the holder and is
  admitted within 2 s of the release;
- a chat document whose idle wait is blocked by a public decode yields the
  decoder (LONG BESIDE LONG ANSWERS kept);
- a holder is asked once; an async callback runs; a raising callback breaks
  nothing; a bounded /v1 request never makes a public run yield;
- DECODE YIELDS ARE CAPPED (T1 review, 2026-09-14): one run's decode is
  suspended at most PUBLIC_API_DECODE_YIELDS_PER_RUN times across its tickets;
  a chat document then prefills beside it, but never beside a decoder it has
  just asked to yield; the count is remembered per run, bounded.
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


def _charge(prompt: int, max_tokens: int) -> int:
    return kv_budget.charge_tokens(prompt, max_tokens, block_size=2096, window=1_000_000)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    metrics.reset()
    admission.reset()
    engine_state.reset()
    continuity.reset()
    monkeypatch.setattr(settings, "model_max_context", 1_000_000)
    monkeypatch.setattr(settings, "admission_long_threshold_tokens", 131_072)
    monkeypatch.setattr(settings, "admission_normal_max", 10)
    monkeypatch.setattr(settings, "admission_long_max", 1)
    monkeypatch.setattr(settings, "admission_long_idle_max", 1)
    monkeypatch.setattr(settings, "admission_long_wait_s", 3.0)
    monkeypatch.setattr(settings, "admission_normal_wait_s", 3.0)
    monkeypatch.setattr(settings, "admission_max_waiting", 100)
    monkeypatch.setattr(settings, "admission_kv_metrics_url", "off", raising=False)
    monkeypatch.setattr(settings, "public_api_shared_kv_budget_tokens", 1_400_000)
    monkeypatch.setattr(settings, "public_api_long_release_at_first_token", True)
    monkeypatch.setattr(settings, "public_api_decode_yields_per_run", 1)
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


class _Chunks:
    def __init__(self) -> None:
        self.go: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await asyncio.wait_for(self.go.get(), HOLD_MAX_S)
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self):
        self.closed = True


async def _patient_stream(run_id: str, *, prompt: int, max_tokens: int, opened: asyncio.Event = None):
    chunks = _Chunks()

    async def op():
        if opened is not None:
            opened.set()
        return chunks

    with admission.origin(V1):
        token = admission.set_run_id(run_id)
        try:
            stream = await admission.run(op, messages=[{"role": "user", "content": str(prompt)}],
                                         base_url=MAIN_URL, model="m", stream=True, max_tokens=max_tokens,
                                         patient=True)
        finally:
            admission._run_id.reset(token)
    return stream, chunks


async def _run(call, *, origin=CHAT, prompt: int = 100, max_tokens=None):
    with admission.origin(origin):
        return await admission.run(call, messages=[{"role": "user", "content": str(prompt)}],
                                   base_url=MAIN_URL, model="m", max_tokens=max_tokens)


async def _first_token(stream, chunks) -> None:
    chunks.go.put_nowait("tok")
    await stream.__anext__()


def test_a_patient_long_ticket_gives_its_seat_back_at_first_token_and_keeps_its_kv():
    async def run():
        ls = admission.lanes()
        stream, chunks = await _patient_stream("resp_a", prompt=200_000, max_tokens=100)
        charge = _charge(200_000, 100)
        assert ls.long.active == 1 and ls.ledger.committed == charge
        assert admission.kv_ledger().total() == charge
        assert ls.normal.closed, "the closure until first token is unchanged"
        await _first_token(stream, chunks)
        assert ls.long.active == 0, "seat given back at first token"
        assert not ls.normal.closed
        assert ls.ledger.committed == charge and admission.kv_ledger().total() == charge, "KV kept"
        assert [h.seated for h in ls.patient.holders()] == [False]
        chunks.go.put_nowait(None)
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
        assert ls.ledger.committed == 0 and admission.kv_ledger().total() == 0 and ls.long.active == 0

    asyncio.run(run())


def test_with_release_at_first_token_off_the_patient_ticket_holds_its_seat_to_the_end(monkeypatch):
    monkeypatch.setattr(settings, "public_api_long_release_at_first_token", False)

    async def run():
        ls = admission.lanes()
        stream, chunks = await _patient_stream("resp_a", prompt=200_000, max_tokens=100)
        await _first_token(stream, chunks)
        assert ls.long.active == 1
        await stream.aclose()
        assert ls.long.active == 0 and ls.ledger.committed == 0 and admission.kv_ledger().total() == 0

    asyncio.run(run())


def test_a_fitting_chat_document_prefills_beside_a_public_decode_that_may_not_be_suspended(monkeypatch):
    """At the PRODUCTION idle max (0: the review found the old version of this
    test passed only because it set 1) and no decode yields allowed — the
    design's own shape."""
    monkeypatch.setattr(settings, "admission_long_idle_max", 0)
    monkeypatch.setattr(settings, "public_api_decode_yields_per_run", 0)
    asked: list = []

    async def run():
        admission.register_yield("resp_a", lambda: asked.append("resp_a"))
        stream, chunks = await _patient_stream("resp_a", prompt=200_000, max_tokens=100)
        await _first_token(stream, chunks)
        _sample(1)  # the public decode is the one running request; idle max is 0
        document = _Call()
        document.release.set()
        assert await asyncio.wait_for(_run(document, prompt=300_000), 2.0) == "answer"
        assert asked == []
        assert not chunks.closed, "the public stream kept decoding"
        await stream.aclose()

    asyncio.run(run())


def test_a_chat_document_that_does_not_fit_kv_asks_the_patient_holder_to_yield():
    async def run():
        ls = admission.lanes()
        holder: dict = {}

        async def yield_now():
            await holder["stream"].aclose()

        admission.register_yield("resp_big", yield_now)
        stream, chunks = await _patient_stream("resp_big", prompt=900_000, max_tokens=100)
        holder["stream"] = stream
        await _first_token(stream, chunks)
        _sample(1)
        assert ls.ledger.committed == _charge(900_000, 100)
        assert _charge(900_000, 100) + _charge(300_000, 100) > kv_budget.budget_tokens(kv_budget.cached())
        document = _Call()
        document.release.set()
        started = time.monotonic()
        assert await asyncio.wait_for(_run(document, prompt=300_000, max_tokens=100), 3.0) == "answer"
        assert time.monotonic() - started < 2.0
        assert metrics._counters["llm_admission_patient_yields_total"][(("reason", "kv"),)] == 1.0
        assert ls.ledger.committed == 0 and admission.kv_ledger().total() == 0

    asyncio.run(run())


def test_the_shared_budget_alone_can_make_a_public_decode_yield(monkeypatch):
    monkeypatch.setattr(settings, "public_api_shared_kv_budget_tokens", 400_000)
    asked: list = []

    async def run():
        holder: dict = {}

        def yield_now():
            asked.append("resp_a")
            holder["stream"]._ticket.release_nowait()

        admission.register_yield("resp_a", yield_now)
        stream, chunks = await _patient_stream("resp_a", prompt=200_000, max_tokens=100)
        holder["stream"] = stream
        await _first_token(stream, chunks)
        _sample(1)
        # 207,504 + 308,112 fits the 1,081,080 budget but not a 400,000 shared one.
        document = _Call()
        document.release.set()
        assert await asyncio.wait_for(_run(document, prompt=300_000, max_tokens=100), 3.0) == "answer"
        assert asked == ["resp_a"]
        await stream.aclose()

    asyncio.run(run())


def test_a_chat_document_during_a_public_prefill_yields_the_holder_and_is_admitted_within_2s():
    async def run():
        ls = admission.lanes()
        opened = asyncio.Event()
        holder: dict = {}
        released_at: dict = {}

        async def yield_now():
            await asyncio.sleep(0.3)  # the durable runner's suspend latency
            released_at["t"] = time.monotonic()
            await holder["stream"].aclose()

        admission.register_yield("resp_pre", yield_now)
        stream, chunks = await _patient_stream("resp_pre", prompt=200_000, max_tokens=100, opened=opened)
        holder["stream"] = stream
        assert opened.is_set() and ls.long.active == 1 and ls.long.active_by_origin[V1] == 1
        _sample(0)
        document = _Call()
        task = asyncio.ensure_future(_run(document, prompt=300_000, max_tokens=100))
        await asyncio.wait_for(document.entered.wait(), 3.0)
        assert time.monotonic() - released_at["t"] <= 2.0
        assert metrics._counters["llm_admission_patient_yields_total"][(("reason", "prefill"),)] == 1.0
        document.release.set()
        await task
        assert ls.long.active == 0 and ls.ledger.committed == 0

    asyncio.run(run())


def test_a_chat_document_blocked_in_its_idle_wait_by_a_public_decode_yields_the_decoder(monkeypatch):
    monkeypatch.setattr(settings, "admission_long_idle_max", 0)

    async def run():
        holder: dict = {}

        def yield_now():
            holder["asked"] = True
            holder["stream"]._ticket.release_nowait()
            _sample(0)  # the decoder left the engine

        admission.register_yield("resp_dec", yield_now)
        stream, chunks = await _patient_stream("resp_dec", prompt=200_000, max_tokens=100)
        holder["stream"] = stream
        await _first_token(stream, chunks)
        _sample(1)
        document = _Call()
        document.release.set()
        assert await asyncio.wait_for(_run(document, prompt=300_000, max_tokens=100), 2.0) == "answer"
        assert holder.get("asked") is True
        assert metrics._counters["llm_admission_patient_yields_total"][(("reason", "decode"),)] == 1.0
        await stream.aclose()

    asyncio.run(run())


def test_a_holder_is_asked_once_and_a_raising_callback_breaks_nothing(caplog):
    calls: list = []

    async def run():
        ls = admission.lanes()

        def boom():
            calls.append(1)
            raise RuntimeError("callback bug")

        admission.register_yield("resp_x", boom)
        stream, chunks = await _patient_stream("resp_x", prompt=900_000, max_tokens=100)
        await _first_token(stream, chunks)
        _sample(1)
        document = _Call()
        document.release.set()
        task = asyncio.ensure_future(_run(document, prompt=300_000, max_tokens=100))
        await asyncio.sleep(0.3)  # ~15 arbiter ticks
        assert calls == [1], "asked once, not per tick"
        assert not task.done(), "still blocked: the callback did not yield"
        await stream.aclose()
        assert await asyncio.wait_for(task, 2.0) == "answer"
        assert ls.ledger.committed == 0

    asyncio.run(run())


def test_a_bounded_v1_long_request_never_makes_a_public_run_yield():
    asked: list = []

    async def run():
        admission.register_yield("resp_a", lambda: asked.append(1))
        stream, chunks = await _patient_stream("resp_a", prompt=900_000, max_tokens=100)
        await _first_token(stream, chunks)
        _sample(1)
        with pytest.raises((admission.AdmissionRejected, asyncio.TimeoutError)):
            await asyncio.wait_for(_run(_Call(), origin=V1, prompt=300_000, max_tokens=100), 0.5)
        assert asked == []
        admission.unregister_yield("resp_a")
        await stream.aclose()

    asyncio.run(run())


def test_the_ledger_surface_register_unregister_total():
    async def run():
        ledger = admission.kv_ledger()
        ledger.register("resp_1", 1000)
        ledger.register("resp_2", 2500)
        assert ledger.total() == 3500
        ledger.register("resp_1", 500)  # re-register replaces
        assert ledger.total() == 3000
        ledger.unregister("resp_2")
        ledger.unregister("resp_missing")
        assert ledger.total() == 500

    asyncio.run(run())


def test_the_ledger_is_also_usable_as_an_object_and_says_whether_a_footprint_fits():
    async def run():
        ls = admission.lanes()
        assert admission.kv_ledger.fits(900_000) is True, "an empty ledger admits anything"
        stream, chunks = await _patient_stream("resp_a", prompt=900_000, max_tokens=100)
        await _first_token(stream, chunks)
        assert admission.kv_ledger.total() == _charge(900_000, 100)
        assert admission.kv_ledger.fits(100_000) is True
        assert admission.kv_ledger.fits(300_000) is False, "beside a 900K job a 300K footprint does not fit"
        admission.kv_ledger.register("resp_b", 10)
        assert admission.kv_ledger().total() == _charge(900_000, 100) + 10
        admission.kv_ledger.unregister("resp_b")
        await stream.aclose()
        assert admission.kv_ledger.fits(300_000) is True
        assert ls.ledger.committed == 0

    asyncio.run(run())


def test_chat_long_admission_present_sees_a_holder_and_a_waiter_but_not_patient_work():
    async def run():
        assert admission.chat_long_admission_present() is False
        stream, chunks = await _patient_stream("resp_a", prompt=200_000, max_tokens=100)
        assert admission.chat_long_admission_present() is False, "a patient holder is not chat"
        await stream.aclose()
        _sample(0)
        document = _Call()
        task = asyncio.ensure_future(_run(document, prompt=300_000))
        await asyncio.wait_for(document.entered.wait(), 2.0)
        assert admission.chat_long_admission_present() is True
        document.release.set()
        await task
        assert admission.chat_long_admission_present() is False

    asyncio.run(run())


def test_a_run_id_passed_to_run_names_the_patient_ticket_without_a_context_variable():
    asked: list = []

    async def run():
        admission.register_yield("resp_kw", lambda: asked.append("resp_kw"))
        chunks = _Chunks()

        async def op():
            return chunks

        with admission.origin(V1):
            stream = await admission.run(op, messages=[{"role": "user", "content": "200000"}], base_url=MAIN_URL,
                                         model="m", stream=True, max_tokens=100, patient=True, run_id="resp_kw")
        assert [h.run_id for h in admission.lanes().patient.holders()] == ["resp_kw"]
        assert admission.current_run_id() is None, "nothing leaked into the context"
        _sample(0)
        document = _Call()
        task = asyncio.ensure_future(_run(document, prompt=300_000))
        await asyncio.sleep(0.1)
        assert asked == ["resp_kw"], "the chat document found the run by the id it was given"
        await stream.aclose()
        document.release.set()
        await asyncio.wait_for(task, 2.0)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# DECODE YIELDS ARE CAPPED (T1 review, 2026-09-14)
# ---------------------------------------------------------------------------


def test_a_runs_decode_is_suspended_once_and_the_next_chat_document_prefills_beside_it(monkeypatch):
    monkeypatch.setattr(settings, "admission_long_idle_max", 0)

    async def run():
        ls = admission.lanes()
        asked: list = []
        holder: dict = {}

        def yield_now():
            asked.append(len(asked) + 1)
            holder["stream"]._ticket.release_nowait()
            _sample(0)

        admission.register_yield("resp_job", yield_now)
        stream, chunks = await _patient_stream("resp_job", prompt=200_000, max_tokens=100)
        holder["stream"] = stream
        await _first_token(stream, chunks)
        _sample(1)
        first = _Call()
        first.release.set()
        assert await asyncio.wait_for(_run(first, prompt=300_000, max_tokens=100), 2.0) == "answer"
        assert asked == [1], "the first collision suspends the decode"
        await stream.aclose()
        admission.unregister_yield("resp_job")

        # The run resumes: a new ticket under the same run id, decoding again.
        admission.register_yield("resp_job", yield_now)
        stream, chunks = await _patient_stream("resp_job", prompt=200_000, max_tokens=100)
        holder["stream"] = stream
        await _first_token(stream, chunks)
        _sample(1)
        assert ls.patient.decode_yields_left("resp_job") == 0
        second = _Call()
        second.release.set()
        started = time.monotonic()
        assert await asyncio.wait_for(_run(second, prompt=300_000, max_tokens=100), 2.0) == "answer"
        assert time.monotonic() - started < 0.5, "beside the decode, not after the idle bound"
        assert asked == [1], "never suspended a second time"
        assert not chunks.closed, "the public decode kept going"
        assert metrics._counters["llm_admission_patient_yields_total"][(("reason", "decode"),)] == 1.0
        await stream.aclose()
        assert ls.ledger.committed == 0

    asyncio.run(run())


def test_a_spent_decoder_is_never_asked_again_even_when_other_work_keeps_the_document_waiting(monkeypatch):
    """Other running work (a NORMAL decode) keeps the engine busy beside the
    capped public decoder: the document waits its idle bound for THAT work,
    and the public run is not suspended a second time for it."""
    monkeypatch.setattr(settings, "admission_long_idle_max", 0)
    monkeypatch.setattr(settings, "admission_long_wait_s", 0.4)

    async def run():
        ls = admission.lanes()
        asked: list = []
        admission.register_yield("resp_spent", lambda: asked.append(1))
        ls.patient._count_decode_yield("resp_spent")  # its one decode yield was spent on an earlier ticket
        stream, chunks = await _patient_stream("resp_spent", prompt=200_000, max_tokens=100)
        await _first_token(stream, chunks)
        _sample(2)  # the public decode and one other request
        document = _Call()
        document.release.set()
        started = time.monotonic()
        assert await asyncio.wait_for(_run(document, prompt=300_000, max_tokens=100), 2.0) == "answer"
        assert time.monotonic() - started >= 0.35, "waited its bound for the other request"
        assert asked == [], "the spent decoder was not asked again"
        assert not chunks.closed
        await stream.aclose()

    asyncio.run(run())


def test_a_chat_document_never_prefills_beside_a_decoder_it_just_asked_to_yield(monkeypatch):
    """The decoder is suspending: the document waits for it (here the callback
    never releases, so it waits its idle bound out and then proceeds)."""
    monkeypatch.setattr(settings, "admission_long_idle_max", 0)
    monkeypatch.setattr(settings, "admission_long_wait_s", 0.6)

    async def run():
        asked: list = []
        admission.register_yield("resp_slow", lambda: asked.append(1))
        stream, chunks = await _patient_stream("resp_slow", prompt=200_000, max_tokens=100)
        await _first_token(stream, chunks)
        _sample(1)
        document = _Call()
        document.release.set()
        task = asyncio.ensure_future(_run(document, prompt=300_000, max_tokens=100))
        await asyncio.sleep(0.3)
        assert asked == [1]
        assert not document.entered.is_set(), "not beside a decoder that is being suspended"
        assert await asyncio.wait_for(task, 2.0) == "answer"
        await stream.aclose()

    asyncio.run(run())


def test_the_decode_yield_count_outlives_the_ticket_and_is_bounded(monkeypatch):
    monkeypatch.setattr(admission, "_DECODE_YIELD_MEMORY", 3)
    monkeypatch.setattr(settings, "public_api_decode_yields_per_run", 2)

    async def run():
        ledger = admission.kv_ledger()
        ledger.register_yield("resp_1", lambda: None)
        for attempt in range(3):
            ledger.register("resp_1", 1000)
            entry = ledger.entries["resp_1"]
            scheduled = ledger.request_yield([entry], "decode")
            assert scheduled == (1 if attempt < 2 else 0), attempt
            ledger.unregister("resp_1")
        assert ledger.decode_yields_left("resp_1") == 0
        # Prefill and KV yields are not capped.
        ledger.register("resp_1", 1000)
        assert ledger.request_yield([ledger.entries["resp_1"]], "kv") == 1
        for run_id in ("resp_2", "resp_3", "resp_4"):
            ledger._count_decode_yield(run_id)
        assert "resp_1" not in ledger.decode_yields and len(ledger.decode_yields) == 3
        await asyncio.sleep(0)

    asyncio.run(run())
