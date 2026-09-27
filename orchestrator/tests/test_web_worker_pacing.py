"""The background web-index drain paces against live chat.

THE DEFECT (2026-09-21). The video pipeline and Artifact Studio have paced
their GPU-heavy batches against a live chat generation since 2026-09-09; the
web knowledge worker never did. Its embedding drain went to the shared
embedding sidecar at an arbitrary moment relative to a chat turn, and the
query embedding on that turn's retrieval path — which has no priority lane —
queued behind it.

Measured against the live sidecar (Qwen/Qwen3-Embedding-0.6B on
127.0.0.1:8003, 2026-09-21, load average 0.6-1.3): a one-text query embedding
costs 15.3 ms mean on an idle sidecar and 756.7 ms mean / 875.5 ms median /
1131.3 ms max with one index batch of 64 page chunks in flight ahead of it.
The live orchestrator's own meter agreed: `embed_seconds{kind="index"}`
31.9209 s over 40 calls (798 ms mean) against `embed_seconds{kind="query"}`
0.579072 s over 14 calls (41 ms mean).

These tests pin the mechanism, not the milliseconds: the drain must not start
while somebody is chatting, it must cost a quiet box nothing, and it must give
up waiting rather than starve the backlog for ever.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from app import db, web_index, web_worker
from app.config import settings

#: Captured before any test patches `asyncio.sleep`, so the fake can still
#: yield to the loop without recursing into itself.
_REAL_SLEEP = asyncio.sleep


@pytest.fixture(autouse=True)
def _no_probe():
    """Every test installs its own probe and leaves none behind."""
    yield
    web_worker.set_busy_probe(None)


@pytest.fixture
def virtual_sleep(monkeypatch):
    """Replace the pace loop's 1 s step with a recorded, instant one."""
    slept: list = []

    async def _fake(seconds, *args, **kwargs):
        slept.append(seconds)
        return await _REAL_SLEEP(0, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", _fake)
    return slept


# --------------------------------------------------------------------------
# pace() itself
# --------------------------------------------------------------------------


def test_a_quiet_box_is_not_made_to_wait(virtual_sleep):
    """Nobody chatting -> the drain starts immediately. Pacing must never be
    a tax on the case it is not for."""
    web_worker.set_busy_probe(lambda: False)
    assert asyncio.run(web_worker.pace()) == 0.0
    assert virtual_sleep == []


def test_no_probe_installed_means_never_busy(virtual_sleep):
    """A process that never wired the probe (a tool, a test) drains at once
    rather than blocking on a mechanism it does not have."""
    web_worker.set_busy_probe(None)
    assert asyncio.run(web_worker.pace()) == 0.0
    assert virtual_sleep == []


def test_the_drain_waits_while_a_generation_is_in_flight(virtual_sleep):
    """Busy for three polls, then quiet: the drain waits exactly those three
    seconds and no longer."""
    remaining = {"busy": 3}

    def probe() -> bool:
        if remaining["busy"] <= 0:
            return False
        remaining["busy"] -= 1
        return True

    web_worker.set_busy_probe(probe)
    assert asyncio.run(web_worker.pace()) == 3.0
    assert virtual_sleep == [1.0, 1.0, 1.0]


def test_a_permanently_busy_box_never_starves_the_backlog(virtual_sleep, monkeypatch):
    """The cap is the escape valve: past it the drain runs anyway, exactly as
    `video.pipeline.pace` behaves."""
    monkeypatch.setattr(settings, "web_index_pace_max_wait_s", 4.0)
    web_worker.set_busy_probe(lambda: True)
    assert asyncio.run(web_worker.pace()) == 4.0
    assert virtual_sleep == [1.0, 1.0, 1.0, 1.0]


def test_pacing_can_be_switched_off(virtual_sleep, monkeypatch):
    monkeypatch.setattr(settings, "web_index_pace_max_wait_s", 0.0)
    web_worker.set_busy_probe(lambda: True)
    assert asyncio.run(web_worker.pace()) == 0.0
    assert virtual_sleep == []


def test_a_probe_that_raises_is_advisory_not_fatal(virtual_sleep):
    """The probe reaches into main's live-generation registry. If that ever
    throws, the worker carries on — it must not take the drain down."""

    def probe() -> bool:
        raise RuntimeError("registry changed shape")

    web_worker.set_busy_probe(probe)
    assert asyncio.run(web_worker.pace()) == 0.0
    assert virtual_sleep == []


# --------------------------------------------------------------------------
# run_once: the drain really is behind the pace
# --------------------------------------------------------------------------


@pytest.fixture
def quiet_cycle(monkeypatch):
    """`run_once` with everything but the index drain stubbed out, recording
    the order in which the cycle's steps happen."""
    events: list = []

    async def _index_pending(*args, **kwargs):
        events.append("index")
        return 0

    async def _maintain(*args, **kwargs):
        return {}

    async def _run_in_thread(fn, *args, **kwargs):
        return []

    monkeypatch.setattr(web_index, "index_pending", _index_pending)
    monkeypatch.setattr(web_index, "maintain", _maintain)
    monkeypatch.setattr(db, "run_in_thread", _run_in_thread)
    monkeypatch.setattr(settings, "web_background_crawl_enabled", False)
    monkeypatch.setattr(settings, "web_knowledge_worker_enabled", True)
    return events


def test_run_once_does_not_embed_while_somebody_is_chatting(quiet_cycle, virtual_sleep):
    """THE REGRESSION. Before the fix `run_once` called `index_pending`
    straight away, so the batch reached the embedding sidecar with a chat
    turn's query embedding already queued behind it."""
    polls = {"n": 0}

    def probe() -> bool:
        polls["n"] += 1
        # Busy for the first two polls; the cycle must not have embedded yet.
        if polls["n"] <= 2:
            assert quiet_cycle == [], "the index drain ran while a generation was in flight"
            return True
        return False

    web_worker.set_busy_probe(probe)
    asyncio.run(web_worker.run_once())

    assert quiet_cycle == ["index"]
    assert virtual_sleep == [1.0, 1.0]


def test_the_old_behaviour_is_exactly_what_the_knob_at_zero_reproduces(
    quiet_cycle, virtual_sleep, monkeypatch
):
    """WEB_INDEX_PACE_MAX_WAIT_S=0 restores what this worker did before
    2026-09-21: the drain goes to the embedding sidecar with a generation in
    flight. Kept as a test so the assertion above is provably load-bearing
    and so an operator who turns pacing off knows what they are getting."""
    monkeypatch.setattr(settings, "web_index_pace_max_wait_s", 0.0)
    web_worker.set_busy_probe(lambda: True)
    asyncio.run(web_worker.run_once())

    assert quiet_cycle == ["index"]
    assert virtual_sleep == []


def test_run_once_on_a_quiet_box_is_unchanged(quiet_cycle, virtual_sleep):
    """Same work, same counters, no added wait — the fix must be invisible
    when nobody is chatting."""
    web_worker.set_busy_probe(lambda: False)
    done = asyncio.run(web_worker.run_once())

    assert quiet_cycle == ["index"]
    assert virtual_sleep == []
    assert done["indexed"] == 0
    assert done["refreshed"] == 0
    assert done["failed"] == 0


def test_the_post_refresh_drain_is_paced_too(monkeypatch, virtual_sleep):
    """A refresh cycle can take minutes, so the box's state at the top of the
    cycle says nothing about its state by the time the changed pages are
    re-embedded. That second drain paces on its own."""
    events: list = []

    async def _index_pending(*args, **kwargs):
        events.append("index")
        return 0

    async def _maintain(*args, **kwargs):
        return {}

    row = {"id": 1, "url": "https://example.invalid/a", "title": ""}

    async def _run_in_thread(fn, *args, **kwargs):
        # The refresh queue read returns one row; _schedule_next does nothing.
        return [row] if fn is web_worker._due_pages else None

    async def _refresh_one(_row):
        events.append("refresh")
        return "not_modified"

    monkeypatch.setattr(web_index, "index_pending", _index_pending)
    monkeypatch.setattr(web_index, "maintain", _maintain)
    monkeypatch.setattr(db, "run_in_thread", _run_in_thread)
    monkeypatch.setattr(web_worker, "_refresh_one", _refresh_one)
    monkeypatch.setattr(settings, "web_background_crawl_enabled", False)
    monkeypatch.setattr(settings, "web_knowledge_worker_enabled", True)

    paced = {"drains": 0}

    def probe() -> bool:
        # Busy for exactly one poll before each of the two drains.
        if events.count("index") == paced["drains"]:
            paced["drains"] += 1
            return True
        return False

    web_worker.set_busy_probe(probe)
    done = asyncio.run(web_worker.run_once())

    assert events == ["index", "refresh", "index"]
    assert virtual_sleep == [1.0, 1.0]
    assert done["not_modified"] == 1


# --------------------------------------------------------------------------
# The wiring: main must install the probe, or the mechanism is dead code.
# --------------------------------------------------------------------------


def test_main_installs_the_busy_probe_on_the_worker():
    """The video pipeline's probe and this one come from the same place in
    main's lifespan. A mechanism nobody wires is the defect this fixes."""
    from app import main

    source = inspect.getsource(main.lifespan)
    assert "web_worker.set_busy_probe(_chat_is_busy)" in source
    assert "video_pipeline.set_busy_probe(_chat_is_busy)" in source
