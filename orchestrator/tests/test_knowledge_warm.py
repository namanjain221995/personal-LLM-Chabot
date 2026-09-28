"""The knowledge path is warmed behind the app after start (2026-09-29).

WHAT WAS MEASURED. In a fresh process the first Fast turn took 2.5-3.7 s to
its first answer token against about 0.7 s warm: the first dense query opened
the LanceDB table and the embedding client (2.0-3.1 s), the topical page
vocabulary was built on the first turn that asked (3.9-6.6 s), and the
reranker canary ran before the first real scoring. The owner saw the same
thing as 2.6 s averaged over the first turns after the e8eaf293 deploy.

Pinned here:
  * the lifespan schedules the warm-up LAST and does not await it: start-up
    has finished and the app is serving before the warm-up's first line runs,
    and shutdown cancels one that is still running;
  * KNOWLEDGE_WARM_ON_START=false starts nothing;
  * the warm-up runs the dense query, the vocabulary build (in a thread, off
    the event loop) and the canary, and a failing step is logged, not raised;
  * `_warm_process` is untouched and stays socket-free (test_server_perf).
"""
from __future__ import annotations

import asyncio
import threading

from app import db, living_knowledge, rerank, web_index
from app.config import settings
from app.core import knowledge_warm


def test_the_lifespan_starts_the_warm_up_after_startup_and_never_awaits_it(monkeypatch):
    from app import main as app_main, web_worker

    monkeypatch.setattr(settings, "knowledge_warm_on_start", True)
    order: list = []

    async def fake_warm():
        order.append("warm.started")
        await asyncio.Event().wait()  # never finishes on its own

    async def _noop():
        return None

    monkeypatch.setattr(knowledge_warm, "warm", fake_warm)
    monkeypatch.setattr(web_worker, "start", lambda: None)
    monkeypatch.setattr(web_worker, "stop", _noop)

    async def drive():
        async with app_main.lifespan(app_main.app):
            order.append("serving")
            task = knowledge_warm._task
            assert task is not None, "the warm-up was not scheduled"
            for _ in range(5):
                await asyncio.sleep(0)
            assert not task.done(), "start-up waited for the warm-up"
            order.append("still serving")
        return task

    task = asyncio.run(drive())
    assert order == ["serving", "warm.started", "still serving"], order
    assert task.cancelled(), "shutdown left the warm-up running"
    assert knowledge_warm._task is None


def test_with_the_setting_off_nothing_is_started(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_warm_on_start", False)

    async def body():
        return knowledge_warm.start()

    assert asyncio.run(body()) is None


def test_the_setting_is_off_by_default_under_pytest():
    assert settings.knowledge_warm_on_start is False


def test_the_warm_up_runs_each_step_and_the_vocabulary_off_the_loop(monkeypatch):
    ran: dict = {}

    async def retrieve(question, top_k=6, **kwargs):
        ran["dense"] = (question, top_k)
        return []

    def precheck(question):
        ran["vocabulary_thread"] = threading.current_thread() is not threading.main_thread()
        return None

    async def canary(*, force=False):
        ran["canary"] = True
        return True

    monkeypatch.setattr(settings, "living_knowledge_enabled", True)
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(living_knowledge, "fast_topical_precheck", lambda: True)
    monkeypatch.setattr(web_index, "retrieve", retrieve)
    monkeypatch.setattr(living_knowledge, "_topical_precheck", precheck)
    monkeypatch.setattr(rerank, "canary", canary)

    result = asyncio.run(knowledge_warm.warm())

    assert ran["dense"] == (knowledge_warm.WARM_QUERY, 1)
    assert ran["vocabulary_thread"] is True, "the vocabulary was built on the event loop"
    assert ran["canary"] is True
    assert set(result) == {"dense", "vocabulary", "rerank_canary"}
    assert all(isinstance(v, float) for v in result.values()), result


def test_a_failing_step_is_logged_not_raised_and_the_others_still_run(monkeypatch, caplog):
    ran: dict = {}

    async def retrieve(question, top_k=6, **kwargs):
        raise ConnectionError("embedder unreachable")

    def precheck(question):
        ran["vocabulary"] = True
        return None

    async def canary(*, force=False):
        ran["canary"] = True
        return True

    monkeypatch.setattr(settings, "living_knowledge_enabled", True)
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(living_knowledge, "fast_topical_precheck", lambda: True)
    monkeypatch.setattr(web_index, "retrieve", retrieve)
    monkeypatch.setattr(living_knowledge, "_topical_precheck", precheck)
    monkeypatch.setattr(rerank, "canary", canary)

    with caplog.at_level("WARNING", logger="app.core.knowledge_warm"):
        result = asyncio.run(knowledge_warm.warm())

    assert result["dense"] == "error:ConnectionError"
    assert ran == {"vocabulary": True, "canary": True}
    assert "dense failed: ConnectionError" in caplog.text


def test_with_the_knowledge_layer_off_only_the_canary_runs(monkeypatch):
    ran: list = []

    async def retrieve(question, top_k=6, **kwargs):  # pragma: no cover - must not run
        ran.append("dense")
        return []

    async def canary(*, force=False):
        ran.append("canary")
        return True

    monkeypatch.setattr(settings, "web_memory_enabled", False)
    monkeypatch.setattr(web_index, "retrieve", retrieve)
    monkeypatch.setattr(rerank, "canary", canary)

    assert set(asyncio.run(knowledge_warm.warm())) == {"rerank_canary"}
    assert ran == ["canary"]


def test_the_vocabulary_step_reads_through_the_database_thread_helper(monkeypatch):
    """The build goes through db.run_in_thread, as the Fast pre-pass runs it,
    not a bare thread of its own."""
    used: list = []
    real = db.run_in_thread

    async def run_in_thread(fn, *args, **kwargs):
        used.append(getattr(fn, "__name__", ""))
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(db, "run_in_thread", run_in_thread)
    monkeypatch.setattr(living_knowledge, "_topical_precheck", lambda question: None)
    asyncio.run(knowledge_warm._vocabulary())
    assert used == ["<lambda>"]
