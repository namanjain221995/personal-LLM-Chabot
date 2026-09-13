"""Context reads started ahead are bounded across the PROCESS, not only per
turn (second prover pass, 2026-09-13).

fast_path_bench --serve at c=16, medians of 6 interleaved runs: cached TTFT
p50 601.9 -> 700.5 ms and p95 915.9 -> 1009.9 ms against HEAD, slower in 9 of
9 paired rounds; with CONTEXT_CONCURRENT_READS=false it was back to 580.9 /
882.6 ms. Each turn started ~10 reads, 6 at once, with no process-wide cap:
16 turns asked for ~96 thread-plus-connection reads against APP_DB_POOL_MAX=16
and anyio's 40 threads, and every other turn's thread work queued behind them.
"""
from __future__ import annotations

import asyncio

from app import main
from app.config import settings


def _tracked():
    state = {"live": 0, "top": 0, "inline": 0}

    def factory(inline_marker=None):
        async def read():
            state["live"] += 1
            state["top"] = max(state["top"], state["live"])
            try:
                await asyncio.sleep(0.03)
            finally:
                state["live"] -= 1
            return "value"

        return read

    return state, factory


def test_sixteen_turns_never_run_more_reads_ahead_than_the_process_limit(monkeypatch):
    monkeypatch.setattr(settings, "context_reads_process_limit", 6)
    monkeypatch.setattr(settings, "context_reads_concurrency", 6)
    state, factory = _tracked()

    async def scenario():
        turns = [main._ContextReads(True) for _ in range(16)]
        for n, reads in enumerate(turns):
            for i in range(10):
                reads.start(f"r{i}", factory())
        await asyncio.sleep(0.01)
        ahead_top = state["top"]
        for reads in turns:
            reads.close()
        return ahead_top

    assert asyncio.run(scenario()) <= 6


def test_a_read_that_has_no_slot_when_its_call_site_asks_is_read_inline_with_the_same_value(monkeypatch):
    monkeypatch.setattr(settings, "context_reads_process_limit", 2)
    state, factory = _tracked()

    async def scenario():
        reads = main._ContextReads(True)
        for i in range(8):
            reads.start(f"r{i}", factory())
        values = [await reads.get(f"r{i}", factory()) for i in (7, 6, 5)]
        reads.close()
        return values, reads.inline_fallbacks

    values, inline = asyncio.run(scenario())
    assert values == ["value"] * 3
    assert inline >= 1


def test_one_turn_alone_still_runs_its_reads_ahead_together(monkeypatch):
    monkeypatch.setattr(settings, "context_reads_process_limit", 6)
    monkeypatch.setattr(settings, "context_reads_concurrency", 6)
    state, factory = _tracked()

    async def scenario():
        reads = main._ContextReads(True)
        for i in range(10):
            reads.start(f"r{i}", factory())
        for i in range(10):
            assert await reads.get(f"r{i}", factory()) == "value"
        return reads.inline_fallbacks

    assert asyncio.run(scenario()) == 0
    assert state["top"] == 6


def test_past_the_turn_limit_a_turn_reads_one_at_a_time_and_the_slot_returns_when_a_turn_ends(monkeypatch):
    monkeypatch.setattr(settings, "context_reads_concurrent_max_turns", 2)
    for leftover in list(main._turns_reading_ahead):
        leftover.close()
    first, second = main._ContextReads(True), main._ContextReads(True)
    third = main._ContextReads(True)
    assert first.concurrent and second.concurrent
    assert not third.concurrent and third.load_shed
    first.close()
    first.close()  # a second close never gives back a slot twice
    fourth = main._ContextReads(True)
    assert fourth.concurrent
    for reads in (second, third, fourth):
        reads.close()
    assert len(main._turns_reading_ahead) == 0


def test_a_turn_with_concurrent_reads_off_never_takes_a_turn_slot(monkeypatch):
    monkeypatch.setattr(settings, "context_reads_concurrent_max_turns", 1)
    for leftover in list(main._turns_reading_ahead):
        leftover.close()
    off = main._ContextReads(False)
    assert not off.concurrent and not off.load_shed
    assert len(main._turns_reading_ahead) == 0
    on = main._ContextReads(True)
    assert on.concurrent
    on.close()
    off.close()
    assert len(main._turns_reading_ahead) == 0
