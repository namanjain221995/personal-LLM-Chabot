"""Retrieval's CPU work runs off the event loop AND one job at a time.

2026-09-13: plan item 3b moved _merge_candidates and _rank_candidates into
anyio's shared thread pool. The loop stopped stalling, but at 8-16 concurrent
retrieves the per-request wall p95 rose 55-60% over running them in a row
(GIL contention between pure-Python threads; scratchpad relay/stall_p95.py).
One dedicated slot (web_memory._run_cpu) keeps both wins.
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone

from app import web_memory
from app.freshness import Freshness


def _rows(n: int):
    unit = "the reasoning model has a score on the leaderboard and pricing in tokens. "
    return [
        {
            "id": i + 1,
            "url": f"https://slot{i}.example/p",
            "title": f"Slot {i}",
            "text": unit * 40 + f" uniq{i}",
            "domain": f"slot{i}.example",
            "authority": 40,
            "fetched_at": datetime.now(timezone.utc),
            "published_at": None,
            "modified_at": None,
            "source_type": "",
            "origin": "search",
        }
        for i in range(n)
    ]


def test_retrieval_cpu_work_runs_one_job_at_a_time_and_never_on_the_event_loop(monkeypatch):
    rows = _rows(6)
    monkeypatch.setattr(web_memory, "_lexical_candidates", lambda query, limit: rows)
    lock = threading.Lock()
    state = {"active": 0, "top": 0, "threads": set(), "jobs": 0}

    def tracked(real):
        def run(*args, **kwargs):
            with lock:
                state["active"] += 1
                state["top"] = max(state["top"], state["active"])
                state["threads"].add(threading.get_ident())
                state["jobs"] += 1
            try:
                time.sleep(0.02)  # long enough for a second job to overlap
                return real(*args, **kwargs)
            finally:
                with lock:
                    state["active"] -= 1

        return run

    monkeypatch.setattr(web_memory, "_merge_candidates", tracked(web_memory._merge_candidates))
    monkeypatch.setattr(web_memory, "_rank_candidates", tracked(web_memory._rank_candidates))

    async def scenario():
        loop_thread = threading.get_ident()
        results = await asyncio.gather(
            *(
                web_memory.retrieve("reasoning score leaderboard", level=Freshness.RECENT, top_k=3, use_cache=False)
                for _ in range(8)
            )
        )
        return loop_thread, results

    loop_thread, results = asyncio.run(scenario())
    assert all(r.evidence for r in results)
    assert state["jobs"] == 16
    assert loop_thread not in state["threads"], "retrieval CPU ran on the event loop"
    assert state["top"] == 1, f"{state['top']} retrieval CPU jobs overlapped"
