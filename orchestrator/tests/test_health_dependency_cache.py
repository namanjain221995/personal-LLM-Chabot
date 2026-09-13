"""The /health dependency fan-out is cached for a few seconds (2026-09-13).

Every call opened the DuckDB warehouse, the app database, both LanceDB
indexes and the reports volume in threads and probed every model server —
for the container healthcheck (30 s), the blackbox probe (15 s) and every
deploy poll. py-spy put it at 8 % of the orchestrator's CPU under 8
concurrent Fast requests (2026-09-05). What is pinned: probes are not repeated
inside HEALTH_DEPENDENCY_CACHE_S, concurrent callers share one probe, a
changed probe or setting is never answered from the cache, and the in-memory
engine verdict is never stale.
"""
from __future__ import annotations

import asyncio

import pytest

from app import health
from app.config import settings


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.fixture()
def probes(monkeypatch):
    calls = {"vllm": 0, "duckdb": 0, "app_db": 0, "web_index": 0}

    async def endpoint_ok(_client, _url):
        calls["vllm"] += 1
        await asyncio.sleep(0.01)  # a real probe takes time; concurrency must share it
        return {"status": "ok"}

    async def optional_disabled(_client):
        return {"status": "disabled"}

    def duckdb_ok(_path):
        calls["duckdb"] += 1
        return {"status": "ok"}

    def app_db_ok():
        calls["app_db"] += 1
        return {"status": "ok"}

    def web_index_ok():
        calls["web_index"] += 1
        return {"status": "ok", "rows": 1}

    async def context_ok(_client):
        return {"status": "ok"}

    monkeypatch.setattr(health, "_probe_vllm", endpoint_ok)
    monkeypatch.setattr(health, "_probe_ocr", optional_disabled)
    monkeypatch.setattr(health, "_probe_reranker", optional_disabled)
    monkeypatch.setattr(health, "probe_context_window", context_ok)
    monkeypatch.setattr(health, "_check_duckdb", duckdb_ok)
    monkeypatch.setattr(health, "_check_app_db", app_db_ok)
    monkeypatch.setattr(health, "_check_embedding_index", lambda: {"status": "empty"})
    monkeypatch.setattr(health, "_check_web_index", web_index_ok)
    monkeypatch.setattr(health, "_check_work", lambda: {"live_generations": 0})
    monkeypatch.setattr(health, "_check_artifacts", lambda: {"status": "ok"})
    monkeypatch.setattr(health, "HEALTH_DEPENDENCY_CACHE_S", 4.0)
    # health.py prefers the Settings attribute since 2026-09-13; pin both.
    monkeypatch.setattr(health.settings, "health_dependency_cache_s", 4.0, raising=False)
    clock = _Clock()
    monkeypatch.setattr(health, "time", clock)
    health.reset_dependency_cache()
    yield calls, clock
    health.reset_dependency_cache()


def test_probes_are_not_repeated_within_four_seconds(probes):
    calls, clock = probes

    async def scenario():
        first = await health.check_dependencies()
        clock.now += 3.9
        second = await health.check_dependencies()
        return first, second

    first, second = asyncio.run(scenario())
    assert calls["duckdb"] == 1 and calls["app_db"] == 1 and calls["web_index"] == 1
    assert first == second


def test_a_probe_runs_again_once_the_window_has_passed(probes):
    calls, clock = probes

    async def scenario():
        await health.check_dependencies()
        clock.now += 4.0
        await health.check_dependencies()

    asyncio.run(scenario())
    assert calls["duckdb"] == 2 and calls["app_db"] == 2


def test_concurrent_callers_share_the_probe_in_flight(probes):
    calls, _clock = probes

    async def scenario():
        return await asyncio.gather(*(health.check_dependencies() for _ in range(8)))

    reports = asyncio.run(scenario())
    assert calls["duckdb"] == 1
    assert all(r == reports[0] for r in reports)


def test_each_caller_gets_its_own_copy_of_the_checks(probes):
    async def scenario():
        first = await health.check_dependencies()
        first["checks"]["duckdb"]["status"] = "scribbled"
        return await health.check_dependencies()

    second = asyncio.run(scenario())
    assert second["checks"]["duckdb"]["status"] == "ok"


def test_the_engine_verdict_is_never_served_from_the_cache(probes, monkeypatch):
    """The breaker and the controller verdict are in-memory and change in
    milliseconds; /health must show them on the very next call."""
    calls, _clock = probes
    reason = {"text": ""}
    monkeypatch.setattr(health, "answer_engine_not_serving", lambda: reason["text"])

    async def scenario():
        before = await health.check_dependencies()
        reason["text"] = "breaker OPEN (held by controller)"
        after = await health.check_dependencies()
        return before, after

    before, after = asyncio.run(scenario())
    assert calls["duckdb"] == 1
    assert before["checks"]["vllm"]["status"] == "ok"
    assert after["checks"]["vllm"]["status"] == "degraded"
    assert "breaker OPEN" in after["checks"]["vllm"]["detail"]


def test_a_replaced_probe_or_a_changed_setting_is_never_answered_from_the_cache(probes, monkeypatch):
    calls, _clock = probes

    async def scenario():
        await health.check_dependencies()
        monkeypatch.setattr(health, "_check_duckdb", lambda _p: {"status": "error", "detail": "gone"})
        degraded = await health.check_dependencies()
        monkeypatch.setattr(settings, "duckdb_path", "/nonexistent/other.duckdb")
        again = await health.check_dependencies()
        return degraded, again

    degraded, again = asyncio.run(scenario())
    assert degraded["checks"]["duckdb"]["status"] == "error"
    assert calls["app_db"] == 3  # each change re-ran the whole fan-out


def test_a_new_event_loop_probes_afresh(probes):
    calls, _clock = probes
    asyncio.run(health.check_dependencies())
    asyncio.run(health.check_dependencies())
    assert calls["duckdb"] == 2


def test_zero_disables_the_cache(probes, monkeypatch):
    calls, _clock = probes
    monkeypatch.setattr(health, "HEALTH_DEPENDENCY_CACHE_S", 0.0)
    monkeypatch.setattr(health.settings, "health_dependency_cache_s", 0.0, raising=False)

    async def scenario():
        await health.check_dependencies()
        await health.check_dependencies()

    asyncio.run(scenario())
    assert calls["duckdb"] == 2


@pytest.mark.parametrize(
    "raw, expected",
    [(None, 4.0), ("", 4.0), ("   ", 4.0), ("0", 0.0), ("2.5", 2.5)],
)
def test_the_tunable_parses_like_config_float(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("HEALTH_DEPENDENCY_CACHE_S", raising=False)
    else:
        monkeypatch.setenv("HEALTH_DEPENDENCY_CACHE_S", raw)
    assert health._env_float("HEALTH_DEPENDENCY_CACHE_S", 4.0) == expected
