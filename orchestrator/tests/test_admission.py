"""app/admission.py — the two lanes in front of the main model (CONTRACT §6.7).

Offline: fake engine calls (async functions and async iterators), the
controller's engine sample planted directly, no network. What is pinned:
the NORMAL lane is a semaphore of ADMISSION_NORMAL_MAX; the LONG lane runs
one at a time, drains the NORMAL lane, waits for the engine to be idle
(bounded) and holds the NORMAL lane closed until its first token; every
wait says its sentence once with how many are ahead; the bounded waits
reject with `timeout` and a too-deep line with `capacity`; the metrics
carry the lane and the reason; a stream releases its lane when it ends.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app import admission, continuity, engine_state, metrics, resilience
from app.config import settings

MAIN_URL = "http://vllm-main.test:8000/v1"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    metrics.reset()
    admission.reset()
    engine_state.reset()
    continuity.reset()
    monkeypatch.setattr(settings, "admission_long_threshold_tokens", 1000)
    monkeypatch.setattr(settings, "admission_normal_max", 2)
    monkeypatch.setattr(settings, "admission_long_max", 1)
    monkeypatch.setattr(settings, "admission_long_idle_max", 0)
    monkeypatch.setattr(settings, "admission_long_wait_s", 5.0)
    monkeypatch.setattr(settings, "admission_normal_wait_s", 5.0)
    monkeypatch.setattr(settings, "admission_max_waiting", 100)
    monkeypatch.setattr(admission, "_POLL_S", 0.02)
    yield
    admission.reset()
    engine_state.reset()
    continuity.reset()
    metrics.reset()


def _msgs(chars: int) -> list:
    return [{"role": "user", "content": "x" * chars}]


def _counter(name: str, **labels) -> float:
    key = tuple(sorted(labels.items()))
    return metrics._counters.get(name, {}).get(key, 0.0)


def _gauge(name: str, **labels) -> float:
    key = tuple(sorted(labels.items()))
    return metrics._gauges.get(name, {}).get(key, 0.0)


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
    """A fake engine call that holds until released, so lane occupancy can
    be observed while it is in flight."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.done = False

    async def __call__(self):
        self.entered.set()
        await self.release.wait()
        self.done = True
        return "answer"


async def _run(call, chars: int = 10, *, stream: bool = False):
    return await admission.run(call, messages=_msgs(chars), base_url=MAIN_URL, model="m", stream=stream)


# ---------------------------------------------------------------------------
# Choosing the lane
# ---------------------------------------------------------------------------


def test_lane_choice_uses_the_estimate_and_asks_tokenize_only_near_the_threshold(monkeypatch):
    asked: list = []

    async def count(base_url, model, messages):
        asked.append(len(messages[0]["content"]))
        return 1500, None

    monkeypatch.setattr(admission.context, "count_tokens", count)

    async def run():
        # Far below: the estimate decides, no round trip.
        assert admission.lane_for(await admission.prompt_tokens(_msgs(30), base_url=MAIN_URL, model="m")) == admission.NORMAL
        # Far above (3 chars ≈ 1 token): long, no round trip either.
        assert admission.lane_for(await admission.prompt_tokens(_msgs(30_000), base_url=MAIN_URL, model="m")) == admission.LONG
        assert asked == []
        # Within a factor of two of the threshold: the exact count decides.
        tokens = await admission.prompt_tokens(_msgs(2_000), base_url=MAIN_URL, model="m")
        assert tokens == 1500 and admission.lane_for(tokens) == admission.LONG
        assert asked == [2_000]

    asyncio.run(run())


# ---------------------------------------------------------------------------
# NORMAL: a semaphore, a sentence, a bounded wait
# ---------------------------------------------------------------------------


def test_the_normal_lane_admits_up_to_its_capacity_and_the_next_waits_with_the_line():
    said: list = []

    async def notify(line):
        said.append(line)

    async def run():
        resilience.set_wait_notifier(notify)
        a, b, c = _Call(), _Call(), _Call()
        ta = asyncio.create_task(_run(a))
        tb = asyncio.create_task(_run(b))
        await asyncio.wait_for(a.entered.wait(), 1.0)
        await asyncio.wait_for(b.entered.wait(), 1.0)
        assert _gauge("llm_admission_lane_active", lane="normal") == 2.0
        tc = asyncio.create_task(_run(c))
        await asyncio.sleep(0.1)
        assert not c.entered.is_set(), "the third waits"
        assert _gauge("llm_admission_waiting", lane="normal") == 1.0
        assert said == ["Waiting for the model to finish current work (2 ahead)."]
        a.release.set()
        await ta
        await asyncio.wait_for(c.entered.wait(), 1.0)
        assert said == ["Waiting for the model to finish current work (2 ahead)."], "said once"
        b.release.set()
        c.release.set()
        assert await tb == "answer" and await tc == "answer"
        assert _gauge("llm_admission_lane_active", lane="normal") == 0.0
        assert _gauge("llm_admission_waiting", lane="normal") == 0.0
        hist = metrics._hists["llm_admission_wait_seconds"][(("lane", "normal"),)]
        assert hist[2] == 1, "one wait observed (the two admitted at once are not waits)"

    asyncio.run(run())


def test_a_normal_wait_past_its_bound_is_rejected_as_timeout(monkeypatch):
    monkeypatch.setattr(settings, "admission_normal_wait_s", 0.15)

    async def run():
        a, b, c = _Call(), _Call(), _Call()
        ta = asyncio.create_task(_run(a))
        tb = asyncio.create_task(_run(b))
        await asyncio.wait_for(a.entered.wait(), 1.0)
        await asyncio.wait_for(b.entered.wait(), 1.0)
        with pytest.raises(admission.AdmissionRejected) as info:
            await _run(c)
        assert info.value.reason == "timeout" and info.value.lane == "normal"
        assert not c.entered.is_set()
        a.release.set()
        b.release.set()
        await asyncio.gather(ta, tb)

    asyncio.run(run())
    assert _counter("llm_admission_rejections_total", reason="timeout") == 1
    assert _gauge("llm_admission_waiting", lane="normal") == 0.0


def test_a_line_that_is_too_deep_refuses_newcomers_at_once(monkeypatch):
    monkeypatch.setattr(settings, "admission_max_waiting", 1)

    async def run():
        a, b, c, d = _Call(), _Call(), _Call(), _Call()
        ta = asyncio.create_task(_run(a))
        tb = asyncio.create_task(_run(b))
        await asyncio.wait_for(a.entered.wait(), 1.0)
        await asyncio.wait_for(b.entered.wait(), 1.0)
        tc = asyncio.create_task(_run(c))  # the one allowed waiter
        await asyncio.sleep(0.05)
        started = time.monotonic()
        with pytest.raises(admission.AdmissionRejected) as info:
            await _run(d)
        assert info.value.reason == "capacity" and time.monotonic() - started < 0.5
        for call in (a, b, c):
            call.release.set()
        await asyncio.gather(ta, tb, tc)

    asyncio.run(run())
    assert _counter("llm_admission_rejections_total", reason="capacity") == 1


# ---------------------------------------------------------------------------
# LONG: exclusive, drains NORMAL, waits for idle, reopens at the first token
# ---------------------------------------------------------------------------


class _Chunks:
    def __init__(self, chunks) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        self._i = 0
        return self

    async def __anext__(self):
        if self._i >= len(self._chunks):
            raise StopAsyncIteration
        self._i += 1
        return self._chunks[self._i - 1]

    async def close(self):
        self.closed = True


def test_a_long_request_drains_the_normal_lane_waits_for_idle_and_reopens_at_its_first_token():
    said: list = []

    async def notify(line):
        said.append(line)

    async def run():
        resilience.set_wait_notifier(notify)
        _sample(running=1)
        a = _Call()
        ta = asyncio.create_task(_run(a))
        await asyncio.wait_for(a.entered.wait(), 1.0)
        opened = asyncio.Event()

        async def open_long():
            opened.set()
            return _Chunks(["first", "second"])

        long_task = asyncio.create_task(_run(open_long, chars=30_000, stream=True))
        await asyncio.sleep(0.1)
        # Holds the LONG slot, has closed NORMAL, and waits for `a` to finish.
        lanes = admission.lanes()
        assert lanes.long.active == 1 and lanes.normal.closed is True
        assert not opened.is_set()
        # A newcomer to NORMAL waits behind the closed lane.
        b = _Call()
        tb = asyncio.create_task(_run(b))
        await asyncio.sleep(0.05)
        assert not b.entered.is_set()
        # `a` finishes: NORMAL is drained, but the engine still reports work.
        a.release.set()
        await ta
        await asyncio.sleep(0.1)
        assert not opened.is_set(), "waits for the engine to be idle"
        _sample(running=0)
        stream = await asyncio.wait_for(long_task, 2.0)
        assert opened.is_set()
        # Opened, no token yet: NORMAL stays closed.
        await asyncio.sleep(0.05)
        assert lanes.normal.closed is True and not b.entered.is_set()
        chunks = []
        async for chunk in stream:
            chunks.append(chunk)
            if len(chunks) == 1:
                # The first token: the prefill is over, NORMAL reopens.
                assert lanes.normal.closed is False
                await asyncio.wait_for(b.entered.wait(), 1.0)
        assert chunks == ["first", "second"]
        assert lanes.long.active == 0, "released when the stream ended"
        b.release.set()
        await tb
        assert said == [
            # The large document, behind the one request that was running…
            "Waiting for the model to finish current work before your large document (1 ahead).",
            # …and the newcomer, behind the closed lane it found.
            "Waiting for the model to finish current work (1 ahead).",
        ]
        assert _gauge("llm_admission_lane_active", lane="long") == 0.0
        assert metrics._hists["llm_admission_wait_seconds"][(("lane", "long"),)][2] == 1

    asyncio.run(run())


def test_the_long_lane_is_exclusive_and_a_second_long_waits_with_the_count_ahead():
    said: list = []

    async def notify(line):
        said.append(line)

    async def run():
        resilience.set_wait_notifier(notify)
        _sample(running=0)
        first, second = _Call(), _Call()
        t1 = asyncio.create_task(_run(first, chars=30_000))
        await asyncio.wait_for(first.entered.wait(), 1.0)
        t2 = asyncio.create_task(_run(second, chars=30_000))
        await asyncio.sleep(0.1)
        assert not second.entered.is_set()
        assert said == ["Waiting for the model to finish current work before your large document (1 ahead)."]
        first.release.set()
        await t1
        await asyncio.wait_for(second.entered.wait(), 1.0)
        second.release.set()
        await t2

    asyncio.run(run())


def test_the_idle_wait_is_bounded_and_the_request_proceeds_after_it(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(settings, "admission_long_wait_s", 0.3)

    async def run():
        _sample(running=4)  # never idle
        call = _Call()
        call.release.set()
        with caplog.at_level(logging.WARNING, logger="app.admission"):
            started = time.monotonic()
            assert await _run(call, chars=30_000) == "answer"
        assert 0.25 <= time.monotonic() - started < 2.0
        assert "engine not idle" in caplog.text
        assert admission.lanes().normal.closed is False

    asyncio.run(run())


def test_an_unknown_controller_means_no_idle_wait():
    async def run():
        assert engine_state.engine_load() is None
        call = _Call()
        call.release.set()
        started = time.monotonic()
        assert await _run(call, chars=30_000) == "answer"
        assert time.monotonic() - started < 0.5

    asyncio.run(run())


def test_a_failing_long_open_reopens_the_normal_lane():
    async def run():
        _sample(running=0)

        async def boom():
            raise RuntimeError("engine refused")

        with pytest.raises(RuntimeError):
            await _run(boom, chars=30_000, stream=True)
        lanes = admission.lanes()
        assert lanes.normal.closed is False and lanes.long.active == 0

        async def open_then_die():
            class _Dies(_Chunks):
                async def __anext__(self):
                    raise ConnectionError("died before a token")

            return _Dies([])

        stream = await _run(open_then_die, chars=30_000, stream=True)
        assert lanes.normal.closed is True
        with pytest.raises(ConnectionError):
            async for _ in stream:
                pass
        assert lanes.normal.closed is False and lanes.long.active == 0

    asyncio.run(run())


def test_closing_a_stream_early_releases_its_lane():
    async def run():
        inner = _Chunks(["a", "b", "c"])

        async def open_it():
            return inner

        stream = await _run(open_it, stream=True)
        assert admission.lanes().normal.active == 1
        assert await stream.__anext__() == "a"
        await stream.close()
        assert inner.closed is True and admission.lanes().normal.active == 0
        # A stream read to the end is not closed by the safety close.
        inner2 = _Chunks(["a"])

        async def open_2():
            return inner2

        stream = await _run(open_2, stream=True)
        assert [c async for c in stream] == ["a"]
        await stream.close()
        assert inner2.closed is False and admission.lanes().normal.active == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# The durable side: a chat turn's hold parks the row while it waits
# ---------------------------------------------------------------------------


def test_a_held_turn_parks_its_row_for_an_admission_wait_and_resumes_without_a_new_attempt(monkeypatch):
    from types import SimpleNamespace

    parked: list = []
    resumed: list = []
    monkeypatch.setattr(continuity.db, "park_chat_request",
                        lambda intent, gen: parked.append(intent) or {"status": "queued"})
    monkeypatch.setattr(continuity.db, "resume_queued_chat_request",
                        lambda intent, gen, *, new_attempt: resumed.append(new_attempt) or {"status": "running", "attempt": 1})

    async def to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(continuity.db, "run_in_thread", to_thread)
    said: list = []

    async def notify(line):
        said.append(line)

    async def run():
        gen = SimpleNamespace(intent_id="i", generation_id="g", attempt=1, retry_reason="none",
                              request_status="running", parked=False)
        continuity.bind(gen, notify)
        a, b, c = _Call(), _Call(), _Call()
        ta = asyncio.create_task(_run(a))
        tb = asyncio.create_task(_run(b))
        await asyncio.wait_for(a.entered.wait(), 1.0)
        await asyncio.wait_for(b.entered.wait(), 1.0)
        tc = asyncio.create_task(_run(c))
        await asyncio.sleep(0.1)
        assert parked == ["i"] and gen.request_status == "queued"
        assert _gauge("llm_queued_generations") == 1.0
        a.release.set()
        await ta
        await asyncio.wait_for(c.entered.wait(), 1.0)
        assert resumed == [False], "an admission wait is not a new attempt"
        assert gen.attempt == 1 and gen.retry_reason == "none" and gen.request_status == "running"
        assert _gauge("llm_queued_generations") == 0.0
        b.release.set()
        c.release.set()
        await asyncio.gather(tb, tc)

    asyncio.run(run())
    assert said == ["Waiting for the model to finish current work (2 ahead)."]


def test_the_lane_label_is_bounded_and_health_describes_the_lanes():
    assert metrics._ALLOWED["lane"] == {"normal", "long"}
    assert metrics._ALLOWED_BY_METRIC["llm_admission_rejections_total"]["reason"] == {"capacity", "timeout"}

    async def run():
        shown = admission.describe()
        assert shown["normal"] == {"capacity": 2, "active": 0, "waiting": 0, "closed": False}
        assert shown["long"] == {"capacity": 1, "active": 0, "waiting": 0, "closed": False}
        assert shown["long_threshold_tokens"] == 1000

    asyncio.run(run())
    # Outside a loop: the limits, no occupancy.
    assert admission.describe()["normal"]["active"] == 0
