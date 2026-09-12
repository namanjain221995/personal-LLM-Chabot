"""app/breaker.py — the circuit breaker in front of each model engine.

The incident this guards against: 2026-09-11 22:15:50Z, worker rank dead,
head answering /health 200 for five minutes while every completion hung.
These tests pin CONTRACT §8.2 with an injected clock and no engine at all:
what opens the breaker, what never does, that HALF_OPEN admits exactly one
canary, how the controller's verdict holds it open, and that every
transition is one metric and one log line in the documented format.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from app import breaker, metrics
from app.breaker import CLOSED, HALF_OPEN, OPEN, Breaker


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def clock():
    return _Clock()


@pytest.fixture(autouse=True)
def _fresh_registry():
    metrics.reset()
    breaker.reset()
    yield
    breaker.reset()
    metrics.reset()


def _breaker(clock, **kw) -> Breaker:
    kw.setdefault("failures", 3)
    kw.setdefault("window_s", 30.0)
    kw.setdefault("cooldown_s", 10.0)
    return Breaker("main", clock=clock, **kw)


def _fail(brk: Breaker, reason: str = "connection", times: int = 1) -> None:
    for _ in range(times):
        permit = brk.acquire()
        brk.record_failure(reason, permit=permit)


def _series(name: str) -> dict:
    """{label-tuple: value} for one metric, straight from the registry."""
    return dict(metrics._counters.get(name, {})) | dict(metrics._gauges.get(name, {}))


# ---------------------------------------------------------------------------
# CLOSED → OPEN
# ---------------------------------------------------------------------------


def test_starts_closed_and_admits_everyone(clock):
    brk = _breaker(clock)
    assert brk.state == CLOSED
    assert brk.allows()
    permit = brk.acquire()
    assert permit is not None and permit.canary is False
    assert _series("llm_breaker_state")[(("engine", "main"),)] == 0


def test_opens_after_three_counted_failures_inside_the_window(clock, caplog):
    brk = _breaker(clock)
    with caplog.at_level(logging.INFO, logger="app.breaker"):
        _fail(brk, "connection", 2)
        assert brk.state == CLOSED
        _fail(brk, "engine_dead", 1)
    assert brk.state == OPEN
    assert not brk.allows()
    assert brk.acquire() is None
    # Every transition: one metric and one log line in the contract format.
    assert _series("llm_breaker_state")[(("engine", "main"),)] == 1
    assert _series("llm_breaker_transitions_total")[(("engine", "main"), ("to", "OPEN"))] == 1
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("llm.breaker ")]
    assert lines == ["llm.breaker engine=main from=CLOSED to=OPEN reason=3xengine_dead_in_30s"]
    # Failures are counted by their bounded reason.
    failures = _series("llm_breaker_failures_total")
    assert failures[(("engine", "main"), ("reason", "connection"))] == 2
    assert failures[(("engine", "main"), ("reason", "engine_dead"))] == 1


def test_failures_outside_the_window_do_not_add_up(clock):
    brk = _breaker(clock)
    _fail(brk, "connection", 2)
    clock.now += 31.0
    _fail(brk, "connection", 1)
    assert brk.state == CLOSED
    assert brk.describe()["failures_in_window"] == 1


def test_a_success_between_failures_restarts_the_window(clock):
    brk = _breaker(clock)
    _fail(brk, "connection", 2)
    brk.record_success(brk.acquire())
    _fail(brk, "connection", 2)
    assert brk.state == CLOSED
    _fail(brk, "connection", 1)
    assert brk.state == OPEN


@pytest.mark.parametrize("reason", ["request_timeout", "queue_timeout", "malformed"])
def test_non_opening_reasons_are_counted_but_never_open(clock, reason):
    brk = _breaker(clock)
    _fail(brk, reason, 5)
    assert brk.state == CLOSED
    assert _series("llm_breaker_failures_total")[(("engine", "main"), ("reason", reason))] == 5


def test_cancelled_and_not_our_fault_are_never_counted(clock):
    brk = _breaker(clock)
    _fail(brk, "cancelled", 5)
    for _ in range(5):
        brk.record_failure(None, permit=brk.acquire())
    assert brk.state == CLOSED
    assert "llm_breaker_failures_total" not in metrics._counters


def test_an_unknown_reason_is_ignored_not_minted(clock):
    brk = _breaker(clock)
    _fail(brk, "kaboom", 5)
    assert brk.state == CLOSED
    assert "llm_breaker_failures_total" not in metrics._counters


# ---------------------------------------------------------------------------
# OPEN → HALF_OPEN → CLOSED / OPEN
# ---------------------------------------------------------------------------


def test_half_open_after_the_cooldown_admits_exactly_one_canary(clock):
    brk = _breaker(clock)
    _fail(brk, "connection", 3)
    clock.now += 9.9
    assert brk.state == OPEN and brk.acquire() is None
    clock.now += 0.1
    assert brk.state == HALF_OPEN
    assert brk.allows()
    canary = brk.acquire()
    assert canary is not None and canary.canary is True
    # Nobody else gets through while the canary is out.
    assert not brk.allows()
    assert brk.acquire() is None
    assert brk.describe()["canary_in_flight"] is True


def test_canary_success_closes_and_clears_the_window(clock):
    brk = _breaker(clock)
    _fail(brk, "connection", 3)
    clock.now += 10.0
    canary = brk.acquire()
    brk.record_success(canary)
    assert brk.state == CLOSED
    assert brk.describe()["failures_in_window"] == 0
    assert _series("llm_breaker_transitions_total")[(("engine", "main"), ("to", "CLOSED"))] == 1
    assert _series("llm_breaker_transitions_total")[(("engine", "main"), ("to", "HALF_OPEN"))] == 1


def test_canary_failure_reopens_with_the_reason_in_the_log(clock, caplog):
    brk = _breaker(clock)
    _fail(brk, "connection", 3)
    clock.now += 10.0
    canary = brk.acquire()
    with caplog.at_level(logging.INFO, logger="app.breaker"):
        brk.record_failure("worker_lost", permit=canary)
    assert brk.state == OPEN
    assert [r.getMessage() for r in caplog.records if "from=HALF_OPEN" in r.getMessage()] == [
        "llm.breaker engine=main from=HALF_OPEN to=OPEN reason=canary_worker_lost"
    ]
    # And the cooldown starts again from the canary's failure.
    clock.now += 9.0
    assert brk.state == OPEN
    clock.now += 1.0
    assert brk.state == HALF_OPEN


def test_a_cancelled_or_released_canary_hands_the_probe_to_the_next_caller(clock):
    brk = _breaker(clock)
    _fail(brk, "connection", 3)
    clock.now += 10.0
    canary = brk.acquire()
    brk.record_failure("cancelled", permit=canary)
    assert brk.state == HALF_OPEN
    assert brk.allows()
    second = brk.acquire()
    assert second is not None and second.canary
    brk.release(second)
    assert brk.acquire() is not None


def test_a_canary_that_proved_nothing_keeps_the_breaker_half_open(clock):
    """A 4xx or a read timeout on the canary: the engine answered (or ran),
    so it is neither proof of health nor proof of death."""
    brk = _breaker(clock)
    _fail(brk, "connection", 3)
    clock.now += 10.0
    brk.record_failure("malformed", permit=brk.acquire())
    assert brk.state == HALF_OPEN
    brk.record_failure("request_timeout", permit=brk.acquire())
    assert brk.state == HALF_OPEN
    assert brk.acquire() is not None


def test_a_late_outcome_from_an_ordinary_permit_is_not_the_canary(clock):
    brk = _breaker(clock)
    early = brk.acquire()  # admitted while CLOSED
    _fail(brk, "connection", 3)
    clock.now += 10.0
    assert brk.state == HALF_OPEN
    brk.record_success(early)  # its answer arrives now: proves nothing about NOW
    assert brk.state == HALF_OPEN
    canary = brk.acquire()
    assert canary is not None and canary.canary


# ---------------------------------------------------------------------------
# The controller's verdict (external open)
# ---------------------------------------------------------------------------


def test_controller_state_opens_immediately_and_holds(clock, caplog):
    verdict = {"state": None}
    brk = _breaker(clock, external_open=lambda: verdict["state"])
    assert brk.state == CLOSED
    verdict["state"] = "RECOVERING"
    with caplog.at_level(logging.INFO, logger="app.breaker"):
        assert brk.state == OPEN
    assert brk.describe()["held_open_by"] == "RECOVERING"
    assert [r.getMessage() for r in caplog.records][-1] == (
        "llm.breaker engine=main from=CLOSED to=OPEN reason=controller_RECOVERING"
    )
    # Held: the cooldown does not run while the controller says so.
    clock.now += 600.0
    assert brk.state == OPEN and brk.acquire() is None
    # The moment it stops saying so, the cooldown starts.
    verdict["state"] = None
    assert brk.state == OPEN
    clock.now += 10.0
    assert brk.state == HALF_OPEN
    assert brk.describe()["held_open_by"] is None


def test_unknown_verdict_never_opens(clock):
    brk = _breaker(clock, external_open=lambda: None)
    for _ in range(50):
        clock.now += 5.0
        assert brk.state == CLOSED
    assert brk.allows()


def test_controller_verdict_overrides_a_half_open_canary(clock):
    verdict = {"state": None}
    brk = _breaker(clock, external_open=lambda: verdict["state"])
    _fail(brk, "connection", 3)
    clock.now += 10.0
    canary = brk.acquire()
    verdict["state"] = "WEDGED"
    assert brk.state == OPEN
    # The canary's success arrives while the controller still says WEDGED:
    # the controller ran a real completion and lost; one lucky answer does
    # not overrule it.
    brk.record_success(canary)
    assert brk.state == OPEN


# ---------------------------------------------------------------------------
# Thresholds from settings, and the registry
# ---------------------------------------------------------------------------


def test_thresholds_follow_settings_when_not_pinned(clock, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "llm_breaker_failures", 2)
    monkeypatch.setattr(settings, "llm_breaker_window_s", 5.0)
    monkeypatch.setattr(settings, "llm_breaker_cooldown_s", 1.0)
    brk = Breaker("main", clock=clock)
    _fail(brk, "capacity", 2)
    assert brk.state == OPEN
    clock.now += 1.0
    assert brk.state == HALF_OPEN


def test_registry_maps_only_the_main_engine(monkeypatch):
    """One-model mode (CONTRACT v2 §1): the router, the embedder and OCR
    have no breaker — nothing a person reads comes from them — and no
    second engine exists to stand in for the main one."""
    from app.config import settings

    monkeypatch.setattr(settings, "openai_base_url", "http://vllm:8000/v1")
    monkeypatch.setattr(settings, "router_base_url", "http://vllm-router:30002/v1")
    assert breaker.engine_for_base_url("http://vllm:8000/v1/") == "main"
    assert breaker.engine_for_base_url("http://vllm:8000/v1") == "main"
    assert breaker.engine_for_base_url("http://vllm-router:30002/v1") is None
    assert breaker.engine_for_base_url("http://vllm-embed:30003/v1") is None
    assert breaker.engine_for_base_url("") is None
    assert breaker.for_base_url("http://vllm-embed:30003/v1") is None
    main = breaker.for_base_url("http://vllm:8000/v1")
    assert main is breaker.get("main")
    assert set(breaker.all_breakers()) == {"main"}
    assert breaker.ENGINES == ("main",)
    assert not hasattr(breaker, "FALLBACK")


def test_breaker_reasons_are_the_metric_vocabulary():
    """metrics.py bounds llm_breaker_failures_total{reason} per metric;
    the two sets must not drift (CONTRACT §4)."""
    assert set(breaker.REASONS) == metrics._ALLOWED_BY_METRIC["llm_breaker_failures_total"]["reason"]
    assert breaker.OPENING_REASONS < breaker.REASONS


def test_breakers_are_per_event_loop():
    """Hundreds of tests hand the wrapper a dead client on purpose; one
    loop's three refused connections must not open the breaker for the
    next loop (see the registry's comment)."""

    async def trip_it():
        main = breaker.get("main")
        for _ in range(3):
            main.record_failure("connection", permit=main.acquire())
        return main.state

    async def look():
        return breaker.get("main").state

    assert asyncio.run(trip_it()) == OPEN
    assert asyncio.run(look()) == CLOSED
    # An installed breaker (a fixture's, with a fake clock) is handed to
    # every loop instead.
    pinned = Breaker("main", clock=lambda: 0.0)
    breaker.install("main", pinned)

    async def same():
        return breaker.get("main") is pinned

    assert asyncio.run(same()) and asyncio.run(same())
    assert breaker.get("main") is pinned  # and outside any loop


def test_main_breaker_takes_the_controller_as_its_external_input(monkeypatch):
    from app import engine_state

    import time as _time

    engine_state.reset()
    main = breaker.get("main")
    assert main.state == CLOSED
    engine_state._snapshot = engine_state.parse_state_document(
        {"schema": 1, "state": "DOWN", "state_code": 8, "reason": "budget", "generated_at": _time.time()},
        observed_at=main._now(),
    )
    try:
        assert main.state == OPEN
        assert breaker.main_allows() is False
    finally:
        engine_state.reset()


# ---------------------------------------------------------------------------
# The canary hold is bounded (review finding 2026-09-12, breaker.py:183)
# ---------------------------------------------------------------------------


def test_a_canary_outstanding_past_the_hold_lets_the_next_caller_probe(clock, monkeypatch):
    """A non-streaming canary can run for minutes (a Max candidate thinking).
    After LLM_BREAKER_CANARY_HOLD_S it stops blocking everyone else; its
    late outcome is then an ordinary one, not the canary's."""
    from app.config import settings

    monkeypatch.setattr(settings, "llm_breaker_canary_hold_s", 60.0)
    brk = _breaker(clock)
    _fail(brk, "connection", 3)
    clock.now += 10.0
    slow = brk.acquire()
    assert slow is not None and slow.canary
    clock.now += 59.0
    assert not brk.allows()
    clock.now += 2.0
    assert brk.allows()
    probe = brk.acquire()
    assert probe is not None and probe.canary
    # The slow one answers now: proves nothing about the new probe.
    brk.record_success(slow)
    assert brk.state == HALF_OPEN
    brk.record_success(probe)
    assert brk.state == CLOSED


# ---------------------------------------------------------------------------
# A streaming canary settles on the FIRST CHUNK, never on the headers
# ---------------------------------------------------------------------------


class _Chunks:
    """A fake SDK stream: chunks, then StopAsyncIteration — or an error at
    `die_at` (0 = before any chunk)."""

    def __init__(self, chunks, die_at=None, exc=None):
        self._chunks = list(chunks)
        self._die_at = die_at
        self._exc = exc
        self.closed = False

    def __aiter__(self):
        self._i = 0
        return self

    async def __anext__(self):
        if self._die_at is not None and self._i == self._die_at:
            raise self._exc
        if self._i >= len(self._chunks):
            raise StopAsyncIteration
        self._i += 1
        return self._chunks[self._i - 1]

    async def close(self):
        self.closed = True


def _main_url(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "openai_base_url", "http://vllm:8000/v1")
    return "http://vllm:8000/v1"


def _half_open(clock):
    brk = _breaker(clock)
    _fail(brk, "connection", 3)
    clock.now += 10.0
    assert brk.state == HALF_OPEN
    breaker.install("main", brk)
    return brk


class _HungThenEmpty:
    """A fake SDK stream whose first chunk never comes until `release` is
    set — and then there is none (the server closed the body)."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.release.wait()
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


def test_a_half_open_canary_that_only_opened_a_stream_does_not_close_the_breaker(clock, monkeypatch):
    """The 2026-09-11 shape for this process's own traffic: vLLM sends the
    headers before the engine has scheduled anything, so `create()` returns
    and no chunk ever comes. Headers prove nothing: the breaker stays
    HALF_OPEN with the canary out until a chunk (or an error) arrives —
    and since the round-2 review the first chunk is pulled INSIDE the
    resilient attempt, so `resilient()` itself does not return until the
    body says something. An empty body proves nothing either: the permit
    is handed back, not counted."""
    from app import resilience

    url = _main_url(monkeypatch)
    brk = _half_open(clock)
    hung = _HungThenEmpty()

    async def run():
        opening = asyncio.create_task(
            resilience.resilient(lambda: _coro(hung), what="stream", base_url=url, stream=True)
        )
        await asyncio.sleep(0.05)
        assert not opening.done(), "headers alone do not return the stream"
        assert brk.state == HALF_OPEN and brk.describe()["canary_in_flight"] is True
        # Nobody else is admitted while the probe has produced nothing.
        assert not brk.allows()
        hung.release.set()
        stream = await opening
        assert isinstance(stream, resilience.GuardedStream) and stream.exhausted
        return stream

    stream = asyncio.run(run())
    # The body was empty: the permit is handed back, not counted.
    assert brk.state == HALF_OPEN and brk.allows()
    asyncio.run(stream.close())
    assert brk.state == HALF_OPEN and brk.allows()


async def _coro(value):
    return value


def test_the_first_chunk_closes_the_breaker_and_a_body_death_before_it_reopens(clock, monkeypatch):
    import httpx
    import openai

    from app import resilience

    url = _main_url(monkeypatch)
    brk = _half_open(clock)

    async def dies_before_a_token():
        # The death before the first token is the OPEN's failure (round-2
        # review, resilience.py:663): it never reaches a consumer. With no
        # window to retry in (this is not a chat turn) the wrapper gives up.
        exc = openai.APIError("EngineDeadError: engine core died", request=httpx.Request("POST", url), body=None)
        with pytest.raises(resilience.ModelUnavailable) as info:
            await resilience.resilient(
                lambda: _coro(_Chunks([], die_at=0, exc=exc)), what="stream", base_url=url, stream=True,
                recovery_s=0.0,
            )
        assert info.value.last is exc

    asyncio.run(dies_before_a_token())
    assert brk.state == OPEN
    assert brk.describe()["last_reason"] == "canary_engine_dead"
    assert _series("llm_breaker_failures_total")[(("engine", "main"), ("reason", "engine_dead"))] == 1

    clock.now += 10.0
    assert brk.state == HALF_OPEN

    async def serves():
        stream = await resilience.resilient(lambda: _coro(_Chunks(["a", "b"])), what="stream", base_url=url, stream=True)
        seen = []
        async for chunk in stream:
            seen.append(chunk)
            # Settled by the FIRST chunk, before the stream ends.
            assert brk.state == CLOSED
        return seen

    assert asyncio.run(serves()) == ["a", "b"]
    assert brk.state == CLOSED


def test_a_stream_that_dies_after_a_token_is_counted_but_never_re_opened(clock, monkeypatch):
    """CONTRACT §8.4: after the first token nothing is retried — but the
    death IS a failure the breaker hears about (review finding llm.py:935).
    It does not open the breaker by itself: the token before it was a
    success, and a success restarts the window (the engine WAS serving);
    the controller's frozen-token detection covers an engine that keeps
    dying mid-answer. A death BEFORE any token has no such success and
    opens it (the previous test)."""
    import httpx

    from app import resilience

    url = _main_url(monkeypatch)
    brk = _breaker(clock)
    breaker.install("main", brk)
    opens = {"n": 0}

    async def one_dying_stream():
        async def open_it():
            opens["n"] += 1
            return _Chunks(["tok"], die_at=1, exc=httpx.ReadError("stream died"))

        stream = await resilience.resilient(open_it, what="stream", base_url=url, stream=True)
        got = []
        with pytest.raises(httpx.ReadError):
            async for chunk in stream:
                got.append(chunk)
        assert got == ["tok"]

    for i in range(3):
        asyncio.run(one_dying_stream())
        assert opens["n"] == i + 1, "no re-open after a token"
    assert brk.state == CLOSED
    assert _series("llm_breaker_failures_total")[(("engine", "main"), ("reason", "connection"))] == 3
    assert brk.describe()["failures_in_window"] == 1
