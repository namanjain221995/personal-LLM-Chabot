"""A search rate slot is spent by a search, not by asking whether one may run.

MEASURED 2026-09-29 on the live process: `rate_ok` spends a slot on every
assistant turn before the plan is read, so from the 11th Fast turn in a minute
the Fast live lookup was silently switched off and a 3 ms "Search rate limit
reached" status was counted as the turn's first visible text (16 such
observations since one restart). `rate_peek` reads the window and records
nothing; `rate_take` records the one search that runs. `rate_ok` is unchanged
(tests/test_search_engine.py pins it).

Also here, because the same track owns the call sites: the topical gate's
dense floor comes from web_memory (TOPICAL_DENSE_FLOOR once it exists there),
and the three retrievals whose only use is the topical gate ask for the
dense-first exit when the retrieval offers it.
"""
from __future__ import annotations

import asyncio

import pytest

from app import living_knowledge as lk
from app import web_memory
from app.config import settings
from app.engines import search
from app.freshness import Freshness, Verdict, _MAX_AGE
from app.web_memory import Evidence, Retrieval


@pytest.fixture(autouse=True)
def _window(monkeypatch):
    monkeypatch.setattr(search, "_rate", {})
    monkeypatch.setattr(settings, "search_rate_per_min", 3)


def test_peek_does_not_spend_a_slot():
    for _ in range(10):
        assert search.rate_peek("u1") is True
    assert search._rate.get("u1", []) == []


def test_take_spends_exactly_one_slot_and_peek_sees_it():
    search.rate_take("u1")
    assert len(search._rate["u1"]) == 1
    search.rate_take("u1")
    search.rate_take("u1")
    assert len(search._rate["u1"]) == 3
    assert search.rate_peek("u1") is False
    assert search.rate_peek("u2") is True, "the window is per user"


def test_peek_and_take_share_rate_ok_s_window():
    assert search.rate_ok("u1") and search.rate_ok("u1")
    assert search.rate_peek("u1") is True
    search.rate_take("u1")
    assert search.rate_peek("u1") is False
    assert search.rate_ok("u1") is False


def test_old_entries_leave_the_window(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(search.time, "monotonic", lambda: now[0])
    for _ in range(3):
        search.rate_take("u1")
    assert search.rate_peek("u1") is False
    now[0] += 61.0
    assert search.rate_peek("u1") is True
    search.rate_take("u1")
    assert len(search._rate["u1"]) == 1, "take prunes what peek only ignores"


# --- the topical gate's floor and call sites --------------------------------


def _ev(dense: float) -> Evidence:
    return Evidence(
        url="https://docs.example.org/a", title="Enable the widget", text="enable the widget in settings",
        domain="docs.example.org", authority=60, fetched_at=None,
        dense=dense, lexical=0.9, score=0.9,
    )


def _topical(monkeypatch, dense: float) -> str:
    async def fake(q, *, level=Freshness.STATIC, **kw):
        return Retrieval(query=q, freshness=level, evidence=[_ev(dense)])

    monkeypatch.setattr(lk, "retrieve", fake)
    monkeypatch.setattr(settings, "living_knowledge_topical_min_score", 0.4, raising=False)
    out = lk.Prepared(verdict=Verdict(Freshness.STATIC, _MAX_AGE[Freshness.STATIC], "router"))
    return asyncio.run(lk._topical("how do I enable the widget", out, effort="think")).decision


def test_the_topical_dense_floor_is_web_memory_s(monkeypatch):
    assert _topical(monkeypatch, 0.40) == "static_topical"
    monkeypatch.setattr(web_memory, "TOPICAL_DENSE_FLOOR", 0.45, raising=False)
    assert lk._topical_dense_floor() == 0.45
    assert _topical(monkeypatch, 0.40) == "static_model"


def _gate_spy(monkeypatch):
    calls = []

    async def fake(q, *, level=Freshness.RECENT, top_k=5, topical_gate=False, **kw):
        calls.append((level, top_k, topical_gate, kw.get("cache_store", True)))
        return Retrieval(query=q, freshness=level)

    # Both the real retrieval and the name this module calls take the keyword,
    # as they will once the dense-first exit has landed in web_memory.
    monkeypatch.setattr(web_memory, "retrieve", fake)
    monkeypatch.setattr(lk, "retrieve", fake)
    monkeypatch.setattr(lk, "claims_for", lambda q, limit=3: [])
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(settings, "living_knowledge_topical", True)
    monkeypatch.setattr(settings, "knowledge_evidence_cache_ttl_s", 0.0, raising=False)
    monkeypatch.setattr(settings, "knowledge_fast_topical_precheck", False, raising=False)
    return calls


def _router(monkeypatch, level: Freshness):
    async def ask(question):
        return Verdict(level, _MAX_AGE[level], "router")

    from app import freshness

    monkeypatch.setattr(freshness, "_ask_router", ask)
    monkeypatch.setattr(settings, "freshness_router_enabled", True)


def _prepare(question: str, effort: str = "fast"):
    return asyncio.run(lk.prepare(question, effort=effort, mode="assistant", web_search_pref="off",
                                  allow_network=False))


def test_the_speculative_static_run_asks_for_the_topical_gate(monkeypatch):
    calls = _gate_spy(monkeypatch)
    _router(monkeypatch, Freshness.STATIC)
    _prepare("tell me about the widget settings page")
    assert calls == [(Freshness.STATIC, 4, True, False)], calls


def test_the_timeless_think_retrieval_asks_for_the_topical_gate(monkeypatch):
    calls = _gate_spy(monkeypatch)
    _prepare("how does the widget cache work", effort="think")
    assert calls == [(Freshness.STATIC, 4, True, True)], calls


def test_the_fast_topical_retrieval_asks_for_the_topical_gate(monkeypatch):
    calls = _gate_spy(monkeypatch)
    # Settled STATIC by the regex pass: no speculative run, `_fast_topical_retrieval` retrieves.
    _prepare("how does the widget cache work")
    assert calls == [(Freshness.STATIC, 4, True, True)], calls


def test_a_time_sensitive_retrieval_never_asks_for_the_topical_gate(monkeypatch):
    calls = _gate_spy(monkeypatch)
    _router(monkeypatch, Freshness.RECENT)
    _prepare("tell me about the widget settings page")
    # The speculative STATIC guess (gated) is cancelled; the RECENT retrieval is not gated.
    assert [c for c in calls if c[0] is Freshness.RECENT] == [(Freshness.RECENT, 5, False, True)], calls


def test_without_the_keyword_nothing_extra_is_passed(monkeypatch):
    """Before the dense-first exit lands, `retrieve` is called as it was."""
    seen = []

    async def old(q, *, level=Freshness.RECENT, top_k=5, use_cache=True, effort="fast",
                  verdict=None, cache_store=True):
        seen.append(level)
        return Retrieval(query=q, freshness=level)

    monkeypatch.setattr(web_memory, "retrieve", old)
    monkeypatch.setattr(lk, "retrieve", old)
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(settings, "living_knowledge_topical", True)
    monkeypatch.setattr(settings, "knowledge_fast_topical_precheck", False, raising=False)
    assert _prepare("how does the widget cache work", effort="think").decision == "static_model"
    assert seen == [Freshness.STATIC]
