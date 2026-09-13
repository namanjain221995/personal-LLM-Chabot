"""app/admission.py — the LONG_OUTPUT lane and the KV budget (2026-09-13).

Offline: fake engine calls and streams, the prompt size planted per request,
the engine's /metrics answered from the 2026-09-13 label line, the
controller's engine sample planted directly. Real numbers throughout: a
1,663,201-token pool in 2096-token blocks, a 0.35 reserve (budget 1,081,080 =
515 whole blocks), a 0.15 headroom (managed limit 1,413,720), a 1,000,000-token
window, thresholds 8,192 (/v1) and 65,536 (chat), two LONG_OUTPUT seats. Only
the waits are scaled (seconds, not minutes). The second half pins the fixes of
the adversarial review of the same day, one block per finding.
"""
from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import time
from typing import List

import pytest

from app import admission, continuity, engine_state, kv_budget, metrics, resilience
from app.config import settings

MAIN_URL = "http://vllm-main.test:8000/v1"
V1 = admission.ORIGIN_V1
CHAT = admission.ORIGIN_CHAT

LIVE_METRICS = (
    '# TYPE vllm:cache_config_info gauge\n'
    'vllm:cache_config_info{block_size="2096",cache_dtype="fp8",enable_prefix_caching="False",engine="0",'
    'kv_cache_max_concurrency="1.6632016632016633",kv_cache_memory_bytes="8589934592",'
    'kv_cache_size_tokens="1663201",mamba_block_size="1000000",mamba_cache_mode="none",'
    'num_gpu_blocks="800"} 1.0\n'
)

_NEW = (
    "admission_long_output_max_seqs", "admission_v1_long_output_threshold_tokens",
    "admission_chat_long_output_threshold_tokens", "admission_long_output_wait_s",
    "admission_long_output_retry_after_s", "admission_kv_reserve_fraction", "admission_kv_pool_tokens",
    "admission_kv_block_size", "admission_kv_fixed_blocks_per_seq", "admission_kv_pool_refresh_s",
    "admission_kv_metrics_url", "admission_chat_weight", "admission_chat_reserved_normal_slots",
    "admission_kv_unmanaged_headroom_fraction", "admission_v1_kv_promote_s", "admission_v1_long_closure_duty",
    "admission_v1_long_closure_window_s",
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    metrics.reset()
    admission.reset()
    engine_state.reset()
    for name in _NEW:
        monkeypatch.delattr(settings, name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setattr(settings, "model_max_context", 1_000_000)
    monkeypatch.setattr(settings, "admission_long_threshold_tokens", 131_072)
    monkeypatch.setattr(settings, "admission_normal_max", 10)
    monkeypatch.setattr(settings, "admission_long_max", 1)
    monkeypatch.setattr(settings, "admission_long_idle_max", 0)
    monkeypatch.setattr(settings, "admission_long_wait_s", 5.0)
    monkeypatch.setattr(settings, "admission_normal_wait_s", 5.0)
    monkeypatch.setattr(settings, "admission_max_waiting", 100)
    monkeypatch.setattr(settings, "admission_long_output_wait_s", 5.0, raising=False)
    monkeypatch.setattr(admission, "_POLL_S", 0.02)
    reads: List[str] = []

    async def fetch(url):
        reads.append(url)
        return LIVE_METRICS

    monkeypatch.setattr(kv_budget, "_fetch_text", fetch)

    async def planted_prompt_tokens(messages, *, base_url, model):
        return int(messages[0]["content"])

    monkeypatch.setattr(admission, "prompt_tokens", planted_prompt_tokens)
    yield reads
    admission.reset()
    engine_state.reset()
    metrics.reset()


def _counter(name: str, **labels) -> float:
    key = tuple(sorted(labels.items()))
    return metrics._counters.get(name, {}).get(key, 0.0)


def _sample(running: int) -> None:
    doc_engine = {"requests_running": running, "requests_waiting": 0}
    engine_state._record(
        engine_state.parse_state_document(
            {"schema": 1, "generated_at": time.time(), "state": "BUSY" if running else "READY",
             "state_code": 3 if running else 2, "reason": "t", "signals": {"engine": doc_engine}},
            observed_at=time.monotonic(),
        ),
        "",
    )


#: No test may hang, even against a mutant that admits what it should not: a
#: held call lets go by itself after this long, and an expected refusal is
#: awaited with a bound.
HOLD_MAX_S = 5.0


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
    """An engine stream that yields its chunks only when told to."""

    def __init__(self) -> None:
        self.go = asyncio.Queue()
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


async def _run(call, *, origin=CHAT, prompt: int = 100, max_tokens=None, stream: bool = False, bound=None):
    with admission.origin(origin):
        token = admission.set_wait_bound_s(bound) if bound is not None else None
        try:
            return await admission.run(call, messages=[{"role": "user", "content": str(prompt)}],
                                       base_url=MAIN_URL, model="m", stream=stream, max_tokens=max_tokens)
        finally:
            if token is not None:
                admission._wait_bound.reset(token)


def _start(call, **kw) -> asyncio.Task:
    return asyncio.ensure_future(_run(call, **kw))


FULL = 1_008_176  # (3 + 478) × 2096


# ---------------------------------------------------------------------------
# Choosing the lane
# ---------------------------------------------------------------------------


def test_a_v1_request_above_8192_output_tokens_takes_the_long_output_lane_not_normal():
    assert admission.lane_for(100, 8_193, V1) == admission.LONG_OUTPUT
    assert admission.lane_for(100, 8_192, V1) == admission.NORMAL
    assert admission.lane_for(200_000, 8_193, V1) == admission.LONG, "a long prompt is LONG whatever the output"

    async def run():
        call = _Call()
        task = _start(call, origin=V1, max_tokens=9_000)
        await asyncio.wait_for(call.entered.wait(), 1.0)
        ls = admission.lanes()
        assert (ls.long_output.active, ls.normal.active) == (1, 0)
        assert ls.ledger.committed == (3 + 5) * 2096
        call.release.set()
        await task
        assert ls.ledger.committed == 0 and ls.long_output.active == 0

    asyncio.run(run())


def test_a_smart_chat_turn_sent_65536_max_tokens_stays_in_normal():
    assert admission.lane_for(2_000, 65_536, CHAT) == admission.NORMAL
    assert admission.lane_for(2_000, 65_537, CHAT) == admission.LONG_OUTPUT
    # A /v1 request asking the same is a long answer.
    assert admission.lane_for(2_000, 65_536, V1) == admission.LONG_OUTPUT

    async def run():
        call = _Call()
        task = _start(call, prompt=2_000, max_tokens=65_536)
        await asyncio.wait_for(call.entered.wait(), 1.0)
        ls = admission.lanes()
        assert (ls.normal.active, ls.long_output.active, ls.ledger.committed) == (1, 0, 0)
        call.release.set()
        await task

    asyncio.run(run())


def test_no_max_tokens_means_normal_as_before():
    assert admission.lane_for(100) == admission.NORMAL
    assert admission.lane_for(100, None, V1) == admission.NORMAL

    async def run():
        call = _Call()
        task = _start(call, origin=V1)
        await asyncio.wait_for(call.entered.wait(), 1.0)
        assert admission.lanes().normal.active == 1
        call.release.set()
        await task

    asyncio.run(run())


# ---------------------------------------------------------------------------
# The KV budget
# ---------------------------------------------------------------------------


def test_a_second_one_million_token_request_waits_while_the_first_holds_its_kv(_fresh):
    reads = _fresh

    async def run():
        first, second = _Call(), _Call()
        t1 = _start(first, origin=V1, max_tokens=1_000_000)
        await asyncio.wait_for(first.entered.wait(), 1.0)
        ls = admission.lanes()
        assert ls.ledger.committed == FULL
        assert reads == [], "an empty ledger admits any charge: no pool read was needed"
        t2 = _start(second, origin=V1, max_tokens=1_000_000)
        await asyncio.sleep(0.2)
        assert not second.entered.is_set(), "two full windows never fit"
        assert reads == ["http://vllm-main.test:8000/metrics"], "beside committed work the live pool decides"
        assert ls.long_output.active == 1 and ls.long_output.waiting == 1
        assert admission.describe()["kv"]["source"] == "live"
        first.release.set()
        await t1
        await asyncio.wait_for(second.entered.wait(), 1.0)
        assert ls.ledger.committed == FULL
        second.release.set()
        await t2
        assert ls.ledger.committed == 0

    asyncio.run(run())


def test_a_small_long_output_request_runs_beside_a_one_million_token_request():
    async def run():
        big, fits, too_big = _Call(), _Call(), _Call()
        tb = _start(big, origin=V1, max_tokens=1_000_000, prompt=4)
        await asyncio.wait_for(big.entered.wait(), 1.0)
        # 72,904 tokens are left: 34 blocks, 3 of them fixed.
        tf = _start(fits, origin=V1, max_tokens=64_000, prompt=4)
        await asyncio.wait_for(fits.entered.wait(), 1.0)
        assert admission.lanes().ledger.committed == FULL + 34 * 2096
        fits.release.set()
        await tf
        tt = _start(too_big, origin=V1, max_tokens=66_000, prompt=4)
        await asyncio.sleep(0.2)
        assert not too_big.entered.is_set(), "35 blocks do not fit beside 481"
        big.release.set()
        await tb
        await asyncio.wait_for(too_big.entered.wait(), 1.0)
        too_big.release.set()
        await tt

    asyncio.run(run())


def test_a_long_output_wait_past_its_bound_is_a_timeout_rejection_with_a_sixty_second_retry_after(monkeypatch):
    monkeypatch.setattr(settings, "admission_long_output_wait_s", 0.2, raising=False)
    said: List[str] = []

    async def notify(line):
        said.append(line)

    async def run():
        resilience.set_wait_notifier(notify)
        first = _Call()
        t1 = _start(first, origin=V1, max_tokens=1_000_000)
        await asyncio.wait_for(first.entered.wait(), 1.0)
        started = time.monotonic()
        with pytest.raises(admission.AdmissionRejected) as refused:
            await asyncio.wait_for(_run(_Call(), origin=V1, max_tokens=1_000_000), 3.0)
        waited = time.monotonic() - started
        assert (refused.value.lane, refused.value.reason, refused.value.retry_after_s) == ("long_output", "timeout", 60.0)
        assert 0.15 <= waited < 1.0
        ls = admission.lanes()
        assert ls.long_output.waiting == 0 and ls.ledger.committed == FULL
        assert said == ["Waiting for room on the main model for a long answer (1 ahead)."]
        first.release.set()
        await t1

    asyncio.run(run())
    assert _counter("llm_admission_rejections_total", reason="timeout") == 1
    # A NORMAL refusal keeps the Retry-After it always had.
    assert admission.AdmissionRejected("normal", "timeout", 1.0).retry_after_s == 5.0


def test_the_long_output_lane_admits_at_most_two_sequences():
    async def run():
        calls = [_Call() for _ in range(3)]
        tasks = [_start(c, origin=V1, max_tokens=10_000) for c in calls]
        await asyncio.sleep(0.2)
        assert [c.entered.is_set() for c in calls] == [True, True, False]
        ls = admission.lanes()
        assert ls.long_output.active == 2 and ls.normal.active == 0
        calls[0].release.set()
        await asyncio.wait_for(calls[2].entered.wait(), 1.0)
        for c in calls:
            c.release.set()
        await asyncio.gather(*tasks)
        assert admission.describe()["long_output"] == {
            "capacity": 2, "active": 0, "waiting": 0, "closed": False, "decoding": 0,
        }

    asyncio.run(run())


def test_a_request_heavier_than_the_budget_runs_alone_rather_never(monkeypatch):
    # A budget smaller than one full window: 5 % of the pool.
    monkeypatch.setattr(settings, "admission_kv_reserve_fraction", 0.95, raising=False)

    async def run():
        heavy, small = _Call(), _Call()
        th = _start(heavy, origin=V1, max_tokens=1_000_000)
        await asyncio.wait_for(heavy.entered.wait(), 1.0)
        ts = _start(small, origin=V1, max_tokens=10_000)
        await asyncio.sleep(0.2)
        assert not small.entered.is_set(), "nothing runs beside a request that is already over budget"
        heavy.release.set()
        await th
        await asyncio.wait_for(small.entered.wait(), 1.0)
        small.release.set()
        await ts

    asyncio.run(run())


def test_a_long_prompt_that_cannot_fit_beside_committed_long_output_is_refused_not_admitted(monkeypatch):
    monkeypatch.setattr(settings, "admission_long_wait_s", 0.3)

    async def run():
        job = _Call()
        tj = _start(job, origin=V1, max_tokens=1_000_000)
        await asyncio.wait_for(job.entered.wait(), 1.0)
        document = _Call()
        with pytest.raises(admission.AdmissionRejected) as refused:
            await asyncio.wait_for(_run(document, prompt=950_000, max_tokens=65_536), 3.0)
        assert (refused.value.lane, refused.value.reason) == ("long", "timeout")
        assert not document.entered.is_set()
        ls = admission.lanes()
        assert ls.long.active == 0, "the LONG seat was given back"
        assert ls.ledger.committed == FULL and len(ls.ledger) == 1
        assert ls.normal.closed is False and ls.long_output.closed is False
        job.release.set()
        await tj

    asyncio.run(run())


def test_a_long_prompt_closure_also_holds_new_long_output_admissions():
    async def run():
        _sample(running=0)
        inner = _Chunks()

        async def open_doc():
            return inner

        doc = await _run(open_doc, prompt=200_000, stream=True)
        ls = admission.lanes()
        assert ls.normal.closed is True and ls.long_output.closed is True
        answer = _Call()
        ta = _start(answer, origin=V1, max_tokens=20_000)
        await asyncio.sleep(0.2)
        assert not answer.entered.is_set(), "a long answer starts with a prefill: it waits for the first token"
        await inner.go.put("first")
        assert await doc.__anext__() == "first"
        await asyncio.wait_for(answer.entered.wait(), 1.0)
        assert ls.long_output.closed is False
        answer.release.set()
        await ta
        await doc.aclose()
        assert ls.ledger.committed == 0 and ls.long.active == 0

    asyncio.run(run())


def test_closing_or_abandoning_a_long_output_stream_releases_its_kv_charge():
    async def run():
        ls = admission.lanes()

        async def open_it():
            return _Chunks()

        closed = await _run(open_it, origin=V1, max_tokens=1_000_000, stream=True)
        assert ls.ledger.committed == FULL
        await closed.aclose()
        assert ls.ledger.committed == 0 and ls.long_output.active == 0
        await closed.aclose()  # idempotent
        assert ls.long_output.active == 0

        abandoned = await _run(open_it, origin=V1, max_tokens=1_000_000, stream=True)
        assert ls.ledger.committed == FULL and ls.long_output.active == 1
        del abandoned
        gc.collect()
        assert ls.ledger.committed == 0 and ls.long_output.active == 0, "garbage collection is the backstop"

    asyncio.run(run())


def test_a_client_that_disconnects_while_waiting_for_long_output_room_leaves_no_waiter_and_no_charge():
    async def run():
        ls = admission.lanes()
        holder = _Call()
        th = _start(holder, origin=V1, max_tokens=1_000_000)
        await asyncio.wait_for(holder.entered.wait(), 1.0)
        waiter = _start(_Call(), origin=V1, max_tokens=1_000_000)
        await asyncio.sleep(0.1)
        assert ls.long_output.waiting == 1
        waiter.cancel()  # the client went away mid-wait
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert ls.long_output.waiting == 0 and ls.ledger.committed == FULL and len(ls.ledger) == 1
        holder.release.set()
        await th
        assert ls.ledger.committed == 0 and ls.long_output.active == 0
        assert admission.describe()["origin_waiting"] == {"chat": 0, "v1": 0}

    asyncio.run(run())


def test_a_client_that_disconnects_mid_generation_releases_the_seat_and_the_charge():
    async def run():
        ls = admission.lanes()
        inner = _Chunks()

        async def open_it():
            return inner

        async def consume():
            stream = await _run(open_it, origin=V1, max_tokens=1_000_000, stream=True)
            try:
                async for _ in stream:
                    pass
            finally:
                await stream.aclose()  # what llm._consume does on the way out

        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0.05)
        await inner.go.put("tok")
        await asyncio.sleep(0.05)
        assert ls.long_output.decoding == 1 and ls.ledger.committed == FULL
        task.cancel()  # the HTTP client disconnected
        with pytest.raises(asyncio.CancelledError):
            await task
        assert inner.closed is True
        assert (ls.long_output.active, ls.long_output.decoding, ls.ledger.committed) == (0, 0, 0)

    asyncio.run(run())


def test_no_kv_grant_while_the_controller_reports_the_reserve_used_up(monkeypatch):
    usage = {"value": 0.70}

    def load():
        return {"requests_running": 0.0, "requests_waiting": 0.0, "age_s": 0.0, "kv_cache_usage": usage["value"]}

    monkeypatch.setattr(engine_state, "engine_load", load)

    async def run():
        call = _Call()
        task = _start(call, origin=V1, max_tokens=20_000)
        await asyncio.sleep(0.2)
        assert not call.entered.is_set(), "unmanaged traffic already holds the reserve"
        usage["value"] = 0.30
        await asyncio.wait_for(call.entered.wait(), 1.0)  # the poll tick re-reads the sample
        call.release.set()
        await task

    asyncio.run(run())


def test_the_wait_bound_override_shortens_a_synchronous_v1_wait(monkeypatch):
    monkeypatch.setattr(settings, "admission_long_output_wait_s", 5.0, raising=False)

    async def run():
        job = _Call()
        tj = _start(job, origin=V1, max_tokens=1_000_000)
        await asyncio.wait_for(job.entered.wait(), 1.0)
        started = time.monotonic()
        with pytest.raises(admission.AdmissionRejected) as refused:
            await asyncio.wait_for(_run(_Call(), origin=V1, max_tokens=1_000_000, bound=0.2), 3.0)
        assert refused.value.reason == "timeout" and time.monotonic() - started < 1.0
        # The override is this context's alone: the next caller has the lane's bound.
        late = asyncio.ensure_future(_run(_Call(), origin=V1, max_tokens=1_000_000))
        await asyncio.sleep(0.4)
        assert not late.done()
        late.cancel()
        await asyncio.gather(late, return_exceptions=True)
        job.release.set()
        await tj

    asyncio.run(run())


def test_a_charge_older_than_four_hours_is_logged_once_and_shown_on_the_gauge(caplog):
    async def run():
        ls = admission.lanes()
        job = _Call()
        tj = _start(job, origin=V1, max_tokens=1_000_000)
        await asyncio.wait_for(job.entered.wait(), 1.0)
        later = time.monotonic() + 14_401
        with caplog.at_level(logging.WARNING, logger="app.admission"):
            ls.publish_kv(now=later)
            ls.publish_kv(now=later + 60)
        assert caplog.text.count("has been held for more than") == 1
        key = ()
        assert metrics._gauges["llm_admission_kv_oldest_charge_age_seconds"][key] >= 14_401
        assert metrics._gauges["llm_admission_kv_committed_tokens"][key] == FULL
        job.release.set()
        await tj

    asyncio.run(run())


# ===========================================================================
# The adversarial review of 2026-09-13, one block per finding
# ===========================================================================

# Whole blocks of 2096 tokens, 3 of them fixed; budget 515 blocks (1,081,080).
B = 2096


def _blocks(prompt: int, max_tokens: int) -> int:
    return kv_budget.charge_tokens(prompt, max_tokens, block_size=B, window=1_000_000) // B


async def _open_stream(inner, **kw):
    async def open_it():
        return inner

    return await _run(open_it, stream=True, **kw)


# --- LONG beside long answers: a decoder is half of the fault shape --------


def test_a_decoding_long_answer_is_work_in_front_of_a_large_document_which_is_refused_not_mixed(monkeypatch):
    """High finding: the build subtracted decoding LONG_OUTPUT sequences from
    the idle test, so a 200K-950K prefill started at once beside them — the
    split_non_spec shape (num_prefills > 0 and num_decodes > 0) B has no
    evidence for. Now they count, and past the bound the document is refused
    (Retry-After 60) rather than proceeding beside a decode that runs for hours."""
    monkeypatch.setattr(settings, "admission_long_wait_s", 0.3)

    async def run():
        ls = admission.lanes()
        inner = _Chunks()
        job = await _open_stream(inner, origin=V1, max_tokens=100_000)
        await inner.go.put("first")
        assert await job.__anext__() == "first"
        assert ls.long_output.decoding == 1
        _sample(running=1)  # exactly that decode
        document = _Call()
        document.release.set()
        started = time.monotonic()
        with pytest.raises(admission.AdmissionRejected) as refused:
            await asyncio.wait_for(_run(document, prompt=200_000, max_tokens=65_536), 3.0)
        assert (refused.value.lane, refused.value.reason, refused.value.retry_after_s) == ("long", "timeout", 60.0)
        assert time.monotonic() - started >= 0.25, "it waited for the decoder to finish first"
        assert not document.entered.is_set()
        assert ls.long.active == 0 and ls.normal.closed is False and ls.long_output.closed is False
        assert ls.ledger.committed == _blocks(100, 100_000) * B
        await job.aclose()
        # Nothing of ours on the engine: the document goes in at once.
        _sample(running=0)
        started = time.monotonic()
        assert await _run(document, prompt=200_000) == "answer"
        assert time.monotonic() - started < 0.2

    asyncio.run(run())


def test_a_long_answer_still_in_prefill_is_work_in_front_of_a_large_document_too(monkeypatch):
    monkeypatch.setattr(settings, "admission_long_wait_s", 0.3)

    async def run():
        job = await _open_stream(_Chunks(), origin=V1, max_tokens=100_000)
        assert admission.lanes().long_output.decoding == 0
        _sample(running=1)
        with pytest.raises(admission.AdmissionRejected) as refused:
            await asyncio.wait_for(_run(_Call(), prompt=200_000), 3.0)
        assert refused.value.reason == "timeout"
        await job.aclose()

    asyncio.run(run())


def test_with_no_controller_sample_long_answers_count_as_work_in_front_of_a_large_document(monkeypatch):
    monkeypatch.setattr(settings, "admission_long_wait_s", 0.3)

    async def run():
        assert engine_state.engine_load() is None
        job = _Call()
        tj = _start(job, origin=V1, max_tokens=100_000)
        await asyncio.wait_for(job.entered.wait(), 1.0)
        document = _Call()
        started = time.monotonic()
        with pytest.raises(admission.AdmissionRejected):
            await asyncio.wait_for(_run(document, prompt=200_000), 3.0)
        assert time.monotonic() - started >= 0.25 and not document.entered.is_set()
        job.release.set()
        await tj

    asyncio.run(run())


def test_while_a_large_document_waits_for_idle_no_new_long_answer_is_admitted(monkeypatch):
    monkeypatch.setattr(settings, "admission_long_wait_s", 3.0)

    async def run():
        ls = admission.lanes()
        _sample(running=1)  # something the orchestrator does not manage
        inner = _Chunks()
        doc_task = asyncio.ensure_future(_open_stream(inner, prompt=200_000))
        await asyncio.sleep(0.1)
        assert ls.long.active == 1 and ls.long_idle_waiting == 1
        answer = _Call()
        ta = _start(answer, origin=V1, max_tokens=20_000)
        await asyncio.sleep(0.2)
        assert not answer.entered.is_set(), "a new long answer would only add running work"
        _sample(running=0)
        doc = await asyncio.wait_for(doc_task, 2.0)
        assert ls.long_idle_waiting == 0 and ls.long_output.closed is True
        await asyncio.sleep(0.1)
        assert not answer.entered.is_set(), "then the closure holds it until the first token"
        await inner.go.put("first")
        await doc.__anext__()
        await asyncio.wait_for(answer.entered.wait(), 1.0)
        answer.release.set()
        await ta
        await doc.aclose()

    asyncio.run(run())


def test_a_v1_large_prompt_is_refused_past_its_idle_bound_while_a_chat_turn_waits_for_normal(monkeypatch):
    monkeypatch.setattr(settings, "admission_long_wait_s", 0.3)
    monkeypatch.setattr(settings, "admission_normal_max", 1)

    async def run():
        ls = admission.lanes()
        _sample(running=1)
        holder, queued = _Call(), _Call()
        th = _start(holder)
        await asyncio.wait_for(holder.entered.wait(), 1.0)
        tq = _start(queued)
        await asyncio.sleep(0.05)
        assert ls.normal.queue.depth(CHAT) == 1
        with pytest.raises(admission.AdmissionRejected) as refused:
            await asyncio.wait_for(_run(_Call(), origin=V1, prompt=200_000, max_tokens=2_000), 3.0)
        assert (refused.value.lane, refused.value.reason) == ("long", "timeout")
        assert ls.normal.closed is False
        # A chat document in the same place proceeds after its bound, as before.
        chat_doc = _Call()
        chat_doc.release.set()
        assert await asyncio.wait_for(_run(chat_doc, prompt=200_000), 3.0) == "answer"
        holder.release.set()
        queued.release.set()
        await asyncio.gather(th, tq)

    asyncio.run(run())


# --- One arbiter: a document waiting for the LONG seat keeps its KV place ----


def test_a_chat_document_waiting_for_the_long_seat_keeps_its_kv_place_against_a_later_v1_request():
    """High finding: the document queued for the LONG seat was invisible to
    the KV arbiter; at the seat's release a later /v1 request took the KV the
    document needed, and 38 of 40 documents were refused behind it."""
    assert (_blocks(140_000, 65_536), _blocks(900_000, 65_536), _blocks(100, 300_000)) == (102, 464, 147)

    async def run():
        ls = admission.lanes()
        _sample(running=0)
        first = _Chunks()
        doc1 = await _open_stream(first, prompt=140_000, max_tokens=65_536)
        await first.go.put("first")
        await doc1.__anext__()  # the lanes reopen; doc1 holds the LONG seat and 102 blocks
        second = _Chunks()
        doc2_task = asyncio.ensure_future(_open_stream(second, prompt=900_000, max_tokens=65_536))
        await asyncio.sleep(0.05)
        assert ls.long.waiting == 1
        # 147 blocks fit beside doc1 now (249 of 515), but beside doc2 (611) they never would.
        answer = _Call()
        ta = _start(answer, origin=V1, max_tokens=300_000)
        await asyncio.sleep(0.2)
        assert not answer.entered.is_set(), "admitted past the document, it would keep it out"
        await doc1.aclose()
        doc2 = await asyncio.wait_for(doc2_task, 1.0)
        assert ls.ledger.committed == 464 * B
        await second.go.put("first")
        await doc2.__anext__()
        await asyncio.sleep(0.1)
        assert not answer.entered.is_set()
        await doc2.aclose()
        await asyncio.wait_for(answer.entered.wait(), 1.0)
        answer.release.set()
        await ta

    asyncio.run(run())


def test_a_waiting_chat_head_that_cannot_fit_does_not_hold_a_v1_answer_that_cannot_delay_it(monkeypatch):
    """Medium finding: a chat head that could not fit held every /v1 answer
    behind it for its whole bound, though 643K tokens were free."""
    monkeypatch.setattr(settings, "admission_long_output_wait_s", 0.6, raising=False)

    async def run():
        job = _Call()
        tj = _start(job, origin=V1, max_tokens=1_000_000)
        await asyncio.wait_for(job.entered.wait(), 1.0)
        chat = _start(_Call(), origin=CHAT, max_tokens=200_000)  # 99 blocks: 580 > 515
        await asyncio.sleep(0.05)
        small = _Call()
        started = time.monotonic()
        ts = _start(small, origin=V1, max_tokens=20_000, bound=5.0)  # 13 blocks; 13 + 99 cannot delay the head
        await asyncio.wait_for(small.entered.wait(), 1.0)
        assert time.monotonic() - started < 0.3 and not chat.done()
        assert admission.lanes().ledger.committed == FULL + 13 * B
        with pytest.raises(admission.AdmissionRejected):
            await asyncio.wait_for(chat, 3.0)
        small.release.set()
        job.release.set()
        await asyncio.gather(tj, ts)

    asyncio.run(run())


def test_a_v1_answer_that_would_delay_a_waiting_chat_head_waits_behind_it():
    assert (_blocks(100, 400_000), _blocks(100, 900_000), _blocks(100, 250_000)) == (194, 433, 123)

    async def run():
        ls = admission.lanes()
        job = _Call()
        tj = _start(job, origin=V1, max_tokens=400_000)
        await asyncio.wait_for(job.entered.wait(), 1.0)
        chat = _Call()
        tc = _start(chat, origin=CHAT, max_tokens=900_000)  # 194 + 433 > 515
        await asyncio.sleep(0.05)
        answer = _Call()
        ta = _start(answer, origin=V1, max_tokens=250_000)  # fits now (317), but 123 + 433 > 515
        await asyncio.sleep(0.2)
        assert not answer.entered.is_set()
        job.release.set()
        await tj
        await asyncio.wait_for(chat.entered.wait(), 1.0)
        assert not answer.entered.is_set() and ls.ledger.committed == 433 * B
        chat.release.set()
        await tc
        await asyncio.wait_for(answer.entered.wait(), 1.0)
        answer.release.set()
        await ta

    asyncio.run(run())


def test_a_fitting_chat_long_answer_is_admitted_past_a_v1_head_that_cannot_fit(monkeypatch):
    """Medium findings (two reviewers): the chat streak handed the KV turn to a
    /v1 head that could not fit, and the arbiter then stopped, refusing chat
    work that fitted beside the running job. A /v1 head is promoted over chat
    only when chat is what keeps it out — here a /v1 job is — so even with no
    promotion wait the chat answers go in, one after another."""
    monkeypatch.setattr(settings, "admission_v1_kv_promote_s", 0.0, raising=False)

    async def run():
        ls = admission.lanes()
        job = _Call()
        tj = _start(job, origin=V1, max_tokens=400_000)  # 194 blocks
        await asyncio.wait_for(job.entered.wait(), 1.0)
        big_call = _Call()
        big = asyncio.ensure_future(_run(big_call, origin=V1, max_tokens=1_000_000, bound=5.0))
        await asyncio.sleep(0.05)
        assert ls.kv.queue.depth(V1) == 1
        for _ in range(4):
            answer = _Call()
            started = time.monotonic()
            ta = _start(answer, origin=CHAT, max_tokens=200_000)  # 99 blocks beside 194
            await asyncio.wait_for(answer.entered.wait(), 1.0)
            assert time.monotonic() - started < 0.3
            answer.release.set()
            await ta
        assert not big_call.entered.is_set()
        job.release.set()
        await tj
        await asyncio.wait_for(big_call.entered.wait(), 1.0)
        big_call.release.set()
        assert await asyncio.wait_for(big, 2.0) == "answer"

    asyncio.run(run())


def test_a_v1_head_that_chat_keeps_out_is_promoted_after_its_wait_and_is_not_starved(monkeypatch):
    monkeypatch.setattr(settings, "admission_v1_kv_promote_s", 0.3, raising=False)

    async def run():
        a, b, c = _Call(), _Call(), _Call()
        ta = _start(a, origin=CHAT, max_tokens=200_000)
        await asyncio.wait_for(a.entered.wait(), 1.0)
        v1 = _Call()
        tv = _start(v1, origin=V1, max_tokens=1_000_000)  # 481 + 99 > 515
        await asyncio.sleep(0.05)
        tb = _start(b, origin=CHAT, max_tokens=200_000)  # before promotion: chat first
        await asyncio.wait_for(b.entered.wait(), 1.0)
        a.release.set()
        await ta
        await asyncio.sleep(0.35)  # the /v1 head has waited its 0.3 s, and only chat keeps it out
        tc = _start(c, origin=CHAT, max_tokens=200_000)
        await asyncio.sleep(0.2)
        assert not c.entered.is_set(), "admitted past the promoted /v1 head, it would keep it out again"
        b.release.set()
        await tb
        await asyncio.wait_for(v1.entered.wait(), 1.0)
        assert not c.entered.is_set()
        v1.release.set()
        await tv
        await asyncio.wait_for(c.entered.wait(), 1.0)
        c.release.set()
        await tc

    asyncio.run(run())


# --- A raise between the grant and the call releases everything ------------


class _FakeHold:
    """A chat turn's continuity hold as run() sees it: enter() parks it
    (waiting, kind ADMISSION); resume() is what raises or is cancelled."""

    def __init__(self, resume) -> None:
        self.kind = None
        self._resume = resume

    @property
    def waiting(self) -> bool:
        return self.kind is not None

    async def enter(self, kind, line=None):
        self.kind = kind

    async def resume(self):
        await self._resume()


@pytest.mark.parametrize("lane", ["normal", "long", "long_output"])
@pytest.mark.parametrize("how", ["lease_lost", "stop"])
def test_a_lease_lost_or_a_stop_between_the_grant_and_the_call_releases_the_seat_the_kv_and_the_closure(
    lane, how, monkeypatch
):
    """High finding: `hold.resume()` ran between the grant and the try that
    releases the ticket; LeaseLost (raised on purpose) or a Stop landing in its
    database hop pinned the seat, a KV charge with no expiry and a closure."""
    monkeypatch.setattr(settings, "admission_normal_max", 1)
    monkeypatch.setattr(settings, "admission_long_output_max_seqs", 1, raising=False)
    kw = {"normal": dict(origin=V1, max_tokens=4_000),
          "long": dict(prompt=200_000, max_tokens=65_536),
          "long_output": dict(origin=V1, max_tokens=400_000)}[lane]

    async def lease_lost():
        raise continuity.LeaseLost("intent-1", "another-generation")

    async def slow():
        await asyncio.sleep(HOLD_MAX_S)

    async def run():
        ls = admission.lanes()
        _sample(running=0)
        holder = _Call()
        th = _start(holder, **kw)
        await asyncio.wait_for(holder.entered.wait(), 1.0)
        hold = _FakeHold(lease_lost if how == "lease_lost" else slow)
        monkeypatch.setattr(continuity, "current", lambda: hold)
        call = _Call()
        task = _start(call, **kw)
        await asyncio.sleep(0.1)
        assert hold.waiting
        holder.release.set()
        await th
        if how == "lease_lost":
            with pytest.raises(continuity.LeaseLost):
                await asyncio.wait_for(task, 2.0)
        else:
            await asyncio.sleep(0.1)  # granted, now inside resume()
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not call.entered.is_set()
        assert ls.get(lane).active == 0
        assert (ls.ledger.committed, ls.ledger.normal_committed, len(ls.ledger)) == (0, 0, 0)
        assert ls.normal.closed is False and ls.long_output.closed is False

    asyncio.run(run())


# --- A caller's own bound is a wall clock ----------------------------------


def test_a_caller_set_wait_bound_is_not_deferred_behind_a_long_closure():
    """Medium finding: publicapi's 30 s synchronous bound (under Cloudflare's
    100 s) was deferred behind a 950K document's closure for up to 1,010 s."""

    async def run():
        ls = admission.lanes()
        _sample(running=0)
        inner = _Chunks()
        doc = await _open_stream(inner, prompt=950_000)
        assert ls.long_output.closed is True
        started = time.monotonic()
        with pytest.raises(admission.AdmissionRejected) as refused:
            await asyncio.wait_for(_run(_Call(), origin=V1, max_tokens=20_000, bound=0.2), 3.0)
        assert refused.value.reason == "timeout" and time.monotonic() - started < 0.6
        # Without a bound of its own a waiter still does not pay for the closure.
        late_call = _Call()
        late_call.release.set()
        late = _start(late_call, origin=V1, max_tokens=20_000)
        await asyncio.sleep(0.3)
        assert not late.done()
        await inner.go.put("first")
        await doc.__anext__()
        assert await asyncio.wait_for(late, 6.0) == "answer"
        await doc.aclose()

    asyncio.run(run())


# --- The /v1 closure budget --------------------------------------------------


def test_the_v1_closure_seconds_count_only_what_overlaps_the_window(monkeypatch):
    monkeypatch.setattr(settings, "admission_v1_long_closure_window_s", 25.0, raising=False)
    monkeypatch.setattr(settings, "admission_v1_long_closure_duty", 0.8, raising=False)

    async def run():
        ls = admission.lanes()
        ls.v1_closures.extend([[-10.0, -6.0], [0.0, 10.0], [20.0, None]])
        assert ls.v1_closure_used_s(now=30.0) == pytest.approx(15.0)  # [5, 10] + [20, 30]
        assert len(ls.v1_closures) == 2, "an interval wholly out of the window is dropped"
        # 20 s budget: 15 used + a 200K prompt's 260 s grace does not fit...
        assert not ls.v1_closure_allows(200_000, now=30.0)
        # ...and an empty window admits it alone rather than never.
        ls.v1_closures.clear()
        assert ls.v1_closure_allows(200_000, now=30.0)
        monkeypatch.setattr(settings, "admission_v1_long_closure_window_s", 0.0, raising=False)
        ls.v1_closures.append([29.0, None])
        assert ls.v1_closure_allows(950_000, now=30.0), "window 0 turns the budget off"

    asyncio.run(run())


def test_v1_large_prompts_close_the_lanes_only_within_their_budget_and_chat_ones_are_not_budgeted(monkeypatch):
    """High finding: one /v1 client sending a 950K prompt every 1,000 s kept
    NORMAL closed for most of every hour; chat's median wait was ~400 s."""
    monkeypatch.setattr(settings, "admission_v1_long_closure_window_s", 0.6, raising=False)
    monkeypatch.setattr(settings, "admission_v1_long_closure_duty", 0.1, raising=False)
    monkeypatch.setattr(settings, "admission_long_wait_s", 3.0)

    async def run():
        ls = admission.lanes()
        _sample(running=0)
        first = _Chunks()
        doc = await _open_stream(first, origin=V1, prompt=200_000)
        await asyncio.sleep(0.05)
        await first.go.put("first")
        await doc.__anext__()
        await doc.aclose()
        assert ls.v1_closure_used_s() > 0.0
        started = time.monotonic()
        second = _Call()
        second.release.set()
        tv = _start(second, origin=V1, prompt=200_000)
        await asyncio.sleep(0.2)
        assert not second.entered.is_set(), "a 260 s grace does not fit a 0.06 s budget with a closure in the window"
        # A chat document is not budgeted.
        chat_doc = _Call()
        chat_doc.release.set()
        assert await asyncio.wait_for(_run(chat_doc, prompt=200_000), 1.0) == "answer"
        assert await asyncio.wait_for(tv, 3.0) == "answer"
        assert time.monotonic() - started >= 0.5, "only once the first closure left the window"

    asyncio.run(run())


# --- The managed limit: /v1 NORMAL is charged too ---------------------------


def test_a_v1_normal_request_that_would_overfill_the_managed_limit_beside_a_long_answer_waits():
    """Medium finding: nine /v1 NORMAL requests with 131K prompts beside a 1M
    answer project 1,111 of 799 blocks — a preemption. Beside 1,008,176
    committed tokens the managed limit (1,413,720) leaves 405,544: two
    130K-prompt /v1 answers (69 blocks each), not three. Chat is never charged."""
    each = _blocks(130_000, 8_192) * B
    assert each == 69 * B

    async def run():
        ls = admission.lanes()
        job = _Call()
        tj = _start(job, origin=V1, max_tokens=1_000_000)
        await asyncio.wait_for(job.entered.wait(), 1.0)
        calls = [_Call() for _ in range(3)]
        tasks = [_start(c, origin=V1, prompt=130_000, max_tokens=8_192) for c in calls]
        await asyncio.sleep(0.2)
        assert [c.entered.is_set() for c in calls] == [True, True, False]
        assert ls.ledger.normal_committed == 2 * each and ls.normal.active == 2
        chat = _Call()
        tc = _start(chat, prompt=130_000, max_tokens=65_536)
        await asyncio.wait_for(chat.entered.wait(), 1.0)
        assert ls.ledger.normal_committed == 2 * each, "chat NORMAL is never charged"
        calls[0].release.set()
        await asyncio.wait_for(calls[2].entered.wait(), 1.0)
        for c in (*calls, chat, job):
            c.release.set()
        await asyncio.gather(tj, tc, *tasks)
        assert (ls.ledger.committed, ls.ledger.normal_committed) == (0, 0)

    asyncio.run(run())


def test_a_long_answer_waits_while_v1_normal_charges_would_overfill_the_managed_limit(monkeypatch):
    """The other direction: /v1 NORMAL requests admitted first (3 × 69 blocks,
    433,872 tokens) leave 979,848 of the managed limit — a 1M answer (1,008,176)
    fits the long budget but not beside them, so it waits for one to end."""
    monkeypatch.setattr(settings, "admission_normal_max", 10)

    async def run():
        ls = admission.lanes()
        calls = [_Call() for _ in range(3)]
        tasks = [_start(c, origin=V1, prompt=130_000, max_tokens=8_192) for c in calls]
        for c in calls:
            await asyncio.wait_for(c.entered.wait(), 1.0)
        assert ls.ledger.normal_committed == 3 * 69 * B
        job = _Call()
        tj = _start(job, origin=V1, max_tokens=1_000_000)
        await asyncio.sleep(0.2)
        assert not job.entered.is_set()
        calls[0].release.set()
        await asyncio.wait_for(job.entered.wait(), 1.0)
        for c in (*calls, job):
            c.release.set()
        await asyncio.gather(tj, *tasks)

    asyncio.run(run())


# --- Low findings, fixed because they were cheap -----------------------------


def test_long_output_max_seqs_zero_refuses_every_long_answer_at_once(monkeypatch):
    monkeypatch.setattr(settings, "admission_long_output_max_seqs", 0, raising=False)

    async def run():
        started = time.monotonic()
        with pytest.raises(admission.AdmissionRejected) as refused:
            await asyncio.wait_for(_run(_Call(), origin=V1, max_tokens=1_000_000), 2.0)
        assert (refused.value.lane, refused.value.reason) == ("long_output", "capacity")
        assert time.monotonic() - started < 0.2
        assert admission.describe()["long_output"]["capacity"] == 0
        # An ordinary answer is not affected.
        ok = _Call()
        ok.release.set()
        assert await _run(ok, origin=V1, max_tokens=2_000) == "answer"

    asyncio.run(run())


def test_the_gc_backstop_releases_on_the_loops_thread_when_collected_on_another(monkeypatch):
    import threading

    monkeypatch.setattr(settings, "admission_normal_max", 1)
    threads = []
    original = admission.Lane._pump

    def spy(self):
        threads.append(threading.get_ident())
        return original(self)

    monkeypatch.setattr(admission.Lane, "_pump", spy)

    async def run():
        loop_thread = threading.get_ident()
        ls = admission.lanes()
        inner = _Chunks()
        stream = await _open_stream(inner)
        waiter = _Call()
        tw = _start(waiter)
        await asyncio.sleep(0.05)
        assert ls.normal.active == 1 and ls.normal.waiting == 1
        inner.back = stream  # a reference cycle: only the cyclic collector frees it
        gc.collect()
        gc.disable()
        try:
            del stream, inner
            threads.clear()
            await asyncio.get_running_loop().run_in_executor(None, gc.collect)
        finally:
            gc.enable()
        await asyncio.wait_for(waiter.entered.wait(), 1.0)
        assert threads and set(threads) == {loop_thread}
        waiter.release.set()
        await tw

    asyncio.run(run())
