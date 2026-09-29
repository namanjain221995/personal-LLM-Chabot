"""The retrieval CPU slot admits a queued rank job before queued merges
(2026-09-29, track retrieval-does-less, B3).

Measured on production (e8eaf293): with 8 Fast turns released at once, the
knowledge pre-pass grew from 0.27 s to 1.9-2.0 s mean and one non-loop thread
sat at ~100% CPU for 1.4-1.6 s. Every retrieve takes the single CPU slot twice,
merge (p50 158 ms) then rank (p50 22 ms), first come first served, so each
turn's short rank waited behind every other turn's merge. Rank-first inside
the same single slot: p50 853 -> 674 ms, mean 815 -> 682 ms per retrieve at 8
concurrent (n=40 per arm, same questions, order alternated). Still exactly one
CPU job at a time: two slots measured slower on 2026-09-13 (the GIL).
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app import web_index, web_memory
from app.config import settings
from app.freshness import Freshness


async def _until(flag: threading.Event, seconds: float = 5.0) -> None:
    async with asyncio.timeout(seconds):
        while not flag.is_set():
            await asyncio.sleep(0.005)


async def _queued(n: int, seconds: float = 5.0) -> None:
    """Until `n` jobs wait for this loop's CPU slot. Polled, not slept for, so
    a slow CI runner cannot reorder the scenario. (On code without the
    prioritised slot there is no queue to read: a fixed pause instead.)"""
    pair = web_memory._cpu_limiters.get(asyncio.get_running_loop())
    if not isinstance(pair, tuple):
        await asyncio.sleep(0.2)
        return
    async with asyncio.timeout(seconds):
        while len(pair[0]._queue) < n:
            await asyncio.sleep(0.005)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    web_memory.cache_clear()
    for name, value in dict(
        web_memory_enabled=True, knowledge_rerank=False, knowledge_evidence_cache_ttl_s=0.0,
        knowledge_cpu_rank_first=True,
    ).items():
        monkeypatch.setattr(settings, name, value, raising=False)
    yield


def _row(word: str) -> dict:
    return {
        "id": {"alpha": 1, "bravo": 2, "charlie": 3}[word], "url": f"https://{word}.example/p",
        "title": word.title(), "text": f"{word} question answer text " * 30, "domain": f"{word}.example",
        "authority": 40, "fetched_at": None, "published_at": None, "modified_at": None,
        "source_type": "", "origin": "search", "content_hash": "",
    }


@pytest.mark.parametrize("rank_first", [True, False])
def test_a_queued_rank_job_finishes_before_a_merge_queued_ahead_of_it(monkeypatch, rank_first):
    """Three retrieves. A has merged and is reading page metadata; B's merge
    holds the slot; C's merge queues; then A's rank queues. When B's merge
    ends, A's rank goes first (with KNOWLEDGE_CPU_RANK_FIRST=false: C's merge,
    which arrived first)."""
    monkeypatch.setattr(settings, "knowledge_cpu_rank_first", rank_first)
    lock = threading.Lock()
    state = {"active": 0, "top": 0}
    finished = []
    a_meta_entered, a_meta_release = threading.Event(), threading.Event()
    b_merge_entered, b_merge_release = threading.Event(), threading.Event()

    async def no_dense(query, top_k=6, site_prefix=""):
        return []

    monkeypatch.setattr(web_index, "retrieve", no_dense)
    monkeypatch.setattr(web_memory, "_lexical_candidates", lambda q, limit: [_row(q.split()[0])])

    def meta(urls, ids=()):
        if any("alpha" in u for u in urls):
            a_meta_entered.set()
            a_meta_release.wait(5)
        return {}

    monkeypatch.setattr(web_memory, "_page_meta", meta)

    def tracked(kind, real):
        def run(query, *args):
            with lock:
                state["active"] += 1
                state["top"] = max(state["top"], state["active"])
            try:
                if kind == "merge" and query.startswith("bravo"):
                    b_merge_entered.set()
                    b_merge_release.wait(5)
                return real(query, *args)
            finally:
                with lock:
                    state["active"] -= 1
                    finished.append((kind, query.split()[0]))
        return run

    monkeypatch.setattr(web_memory, "_merge_candidates", tracked("merge", web_memory._merge_candidates))
    monkeypatch.setattr(web_memory, "_rank_candidates", tracked("rank", web_memory._rank_candidates))

    def ask(word):
        return asyncio.ensure_future(web_memory.retrieve(
            f"{word} question", level=Freshness.RECENT, top_k=3, use_cache=False,
        ))

    async def scenario():
        a = ask("alpha")
        await _until(a_meta_entered)          # A merged; its meta read is blocked
        b = ask("bravo")
        await _until(b_merge_entered)         # B's merge holds the slot
        c = ask("charlie")
        await _queued(1)                      # C's merge queues behind it
        a_meta_release.set()
        await _queued(2)                      # A's rank queues behind C's merge
        b_merge_release.set()
        async with asyncio.timeout(10):
            return await asyncio.gather(a, b, c)

    results = asyncio.run(scenario())
    assert all(r.evidence for r in results)
    assert state["top"] == 1, f"{state['top']} CPU jobs ran at once"
    rank_a, merge_c = finished.index(("rank", "alpha")), finished.index(("merge", "charlie"))
    if rank_first:
        assert rank_a < merge_c, f"A's rank waited behind C's merge: {finished}"
    else:
        assert merge_c < rank_a, f"KNOWLEDGE_CPU_RANK_FIRST=false is not FIFO: {finished}"


def test_run_cpu_admits_rank_jobs_ahead_of_merges_and_runs_one_at_a_time():
    order = []
    lock = threading.Lock()
    state = {"active": 0, "top": 0}
    hold = threading.Event()

    def job(name, block=False):
        with lock:
            state["active"] += 1
            state["top"] = max(state["top"], state["active"])
        try:
            if block:
                hold.wait(5)
            time.sleep(0.002)
            order.append(name)
        finally:
            with lock:
                state["active"] -= 1

    async def scenario():
        first = asyncio.ensure_future(web_memory._run_cpu(job, "merge1", True))
        await asyncio.sleep(0.05)
        queued = [
            asyncio.ensure_future(web_memory._run_cpu(job, "merge2")),
            asyncio.ensure_future(web_memory._run_cpu(job, "rank1", job=web_memory._CPU_RANK)),
            asyncio.ensure_future(web_memory._run_cpu(job, "merge3")),
            asyncio.ensure_future(web_memory._run_cpu(job, "rank2", job=web_memory._CPU_RANK)),
        ]
        await asyncio.sleep(0.05)
        hold.set()
        async with asyncio.timeout(5):
            await asyncio.gather(first, *queued)

    asyncio.run(scenario())
    assert order == ["merge1", "rank1", "rank2", "merge2", "merge3"]
    assert state["top"] == 1


def test_a_waiter_cancelled_in_the_queue_never_leaks_the_slot():
    ran = []
    hold = threading.Event()

    def blocker():
        hold.wait(5)
        ran.append("blocker")

    async def scenario():
        first = asyncio.ensure_future(web_memory._run_cpu(blocker))
        await asyncio.sleep(0.05)
        doomed = asyncio.ensure_future(web_memory._run_cpu(ran.append, "cancelled"))
        after = asyncio.ensure_future(web_memory._run_cpu(ran.append, "after", job=web_memory._CPU_RANK))
        await asyncio.sleep(0.02)
        doomed.cancel()
        hold.set()
        async with asyncio.timeout(5):
            await first
            await after
            with pytest.raises(asyncio.CancelledError):
                await doomed
            await web_memory._run_cpu(ran.append, "fresh")  # the slot is free again

    asyncio.run(scenario())
    assert ran == ["blocker", "after", "fresh"]


def test_a_waiter_cancelled_after_it_was_admitted_hands_the_slot_on():
    async def scenario():
        slot = web_memory._CpuSlot(1)
        await slot.acquire(web_memory._CPU_MERGE)                       # held
        second = asyncio.ensure_future(slot.acquire(web_memory._CPU_MERGE))
        third = asyncio.ensure_future(slot.acquire(web_memory._CPU_MERGE))
        await asyncio.sleep(0)
        slot.release()          # admits `second` ...
        second.cancel()         # ... which is cancelled before it ever runs
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert second.cancelled()
        assert third.done() and not third.cancelled(), "the slot was not handed on"
        slot.release()          # third's job ends
        async with asyncio.timeout(1):
            await slot.acquire(web_memory._CPU_MERGE)                   # free: immediate
        assert slot._free == 0 and not slot._queue
        slot.release()
        assert slot._free == 1

    asyncio.run(scenario())
