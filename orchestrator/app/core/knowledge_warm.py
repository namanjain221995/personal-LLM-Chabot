"""Warm the knowledge path once, in the background, after the app has started.

WHY (2026-09-29). The first Fast turns after every deploy took 2.5-3.7 s to
their first answer token against about 0.7 s once the process was warm. The
difference is state that only the first user turn used to build:

  * the LanceDB web table and the query-embedding client — the first dense
    query measured 2.0-3.1 s cold (embed 310-653 ms of it), 85 ms warm;
  * the topical page vocabulary behind living_knowledge._topical_precheck —
    3.9-6.6 s to build cold, a 9-10 ms incremental re-sync afterwards;
  * the reranker canary, which runs lazily before the first real scoring.

`main._warm_process` cannot do this work: it runs BEFORE the lifespan serves
and must stay socket-free (tests/test_server_perf.py asserts no connect). So
this is a separate task, started as the lifespan hands over to serving, never
awaited by start-up, cancelled at shutdown, and it uses only the functions a
turn already calls. Every failure is logged and swallowed: a warm-up that
cannot reach a sidecar leaves the first turn exactly as cold as before, never
worse.

What it costs: one dense query (one embedding call and one table scan), one
full vocabulary build in a database thread (Python tokenising, so it competes
for the GIL for a few seconds after start), and one three-document rerank.
Its observations land in the usual stage histograms like any other query.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Optional, Union

log = logging.getLogger(__name__)

#: The dense query. Any short text opens the table and the embed client; the
#: rows it returns are thrown away.
WARM_QUERY = "knowledge warm-up"

#: The running warm-up, if any (the lifespan cancels it at shutdown; tests
#: read it).
_task: Optional["asyncio.Task"] = None


async def _dense() -> None:
    from .. import web_index

    await web_index.retrieve(WARM_QUERY, top_k=1)


async def _vocabulary() -> None:
    from .. import db, living_knowledge

    # In a database thread, exactly as the Fast pre-pass runs it: the build
    # reads web_pages over pooled connections and tokenises in Python.
    await db.run_in_thread(living_knowledge._topical_precheck, WARM_QUERY)


async def _rerank_canary() -> None:
    from .. import rerank

    await rerank.canary()


async def _timed(name: str, step) -> Union[float, str]:
    started = time.perf_counter()
    try:
        await step()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a warm-up is never worth an error
        log.warning("knowledge warm-up: %s failed: %s", name, type(exc).__name__)
        return f"error:{type(exc).__name__}"
    return round(time.perf_counter() - started, 3)


async def warm() -> Dict[str, Union[float, str]]:
    """Run every applicable step once, concurrently. Never raises (except a
    cancellation aimed at it). Returns {step: seconds, or "error:<Type>"}."""
    from .. import living_knowledge
    from ..config import settings

    steps = []
    if settings.living_knowledge_enabled and settings.web_memory_enabled:
        steps.append(("dense", _dense))
        if living_knowledge.fast_topical_precheck():
            steps.append(("vocabulary", _vocabulary))
    steps.append(("rerank_canary", _rerank_canary))
    started = time.perf_counter()
    timings = await asyncio.gather(*(_timed(name, step) for name, step in steps))
    result = {name: value for (name, _), value in zip(steps, timings)}
    log.info(
        "knowledge path warmed in %.2f s: %s", time.perf_counter() - started, result
    )
    return result


def start() -> Optional["asyncio.Task"]:
    """Schedule the warm-up on the running loop and return at once; None when
    KNOWLEDGE_WARM_ON_START is off. The caller must not await the task."""
    global _task
    from ..config import settings

    if not settings.knowledge_warm_on_start:
        return None
    _task = asyncio.get_running_loop().create_task(warm(), name="knowledge-warm")
    return _task


async def stop() -> None:
    """Cancel a warm-up that is still running. Never raises."""
    global _task
    task, _task = _task, None
    if task is None or task.done():
        return
    task.cancel()
    # asyncio.wait never re-raises the task's CancelledError, so it cannot
    # swallow a cancellation aimed at the shutdown itself.
    await asyncio.wait({task}, timeout=1.0)
