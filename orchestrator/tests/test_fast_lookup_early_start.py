"""The Fast lookup starts as soon as the candidate dates make it certain.

MEASURED 2026-09-29 (Fast, component harness on the live stack): on a turn
that went to the web, the local judgement ran in SERIES before the lookup —
router, dense + lexical, merge, rank, and a rerank of 12-17 passages (571 ms
p50) — 430-990 ms one turn at a time, and the "Checking recent sources…"
status arrived at 441-1,004 ms. For a REALTIME question (max age 300 s) that
judgement almost never passes.

`Retrieval.sufficient` needs `newest_age <= max_age`, and `newest_age` is the
smallest read-age among evidence drawn from the candidates, so once
`retrieve` reports candidates none of which is new enough (its
`on_candidates` hook, after the dates and before rank and rerank) and no fresh
claim exists, the lookup is certain. It starts then; the judgement finishes
beside it, and every decision is the one the old sequence makes.

Every test runs under asyncio.timeout: a lookup that does not start early
fails the test, it never hangs it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import living_knowledge as lk
from app import web_memory
from app.config import settings
from app.freshness import Freshness
from app.web_memory import Evidence, Retrieval

QUESTION = "usd to inr exchange rate right now"  # lexical:realtime, max age 300 s


def _ev(age_s: float, *, answer: float = -1.0, url: str = "https://rates.example.org/usd-inr") -> Evidence:
    return Evidence(
        url=url, title="USD INR", text="1 USD = 83.2 INR", domain="rates.example.org",
        authority=60, fetched_at=datetime.now(timezone.utc) - timedelta(seconds=age_s),
        dense=0.6, lexical=0.8, score=0.8, answer=answer,
    )


class _Stage:
    """A retrieval whose rerank is blocked until `release` is set, and a
    lookup that records when it started."""

    def __init__(self, monkeypatch, candidates, *, result=None, lookup_result="fresh",
                 lookup_releases=True, claims=()):
        self.events = []
        self.release = asyncio.Event()
        self.lookup_cancelled = False
        self.statuses = []
        self.hooked = []
        self._candidates = candidates
        self._result = result
        self._lookup_result = lookup_result
        self._lookup_releases = lookup_releases

        async def fake_retrieve(q, *, level=Freshness.RECENT, top_k=5, on_candidates=None, **kw):
            self.hooked.append(on_candidates is not None)
            self.events.append("candidates")
            if on_candidates is not None:
                on_candidates(list(self._candidates))
            self.events.append("rerank_waiting")
            await self.release.wait()  # the cross-encoder, blocked
            self.events.append("rerank_done")
            if self._result is not None:
                return self._result(q, level)
            return Retrieval(query=q, freshness=level, evidence=list(self._candidates),
                             newest_age=min((c.age_seconds for c in self._candidates), default=float("inf")))

        async def fake_lookup(question, verdict, **kw):
            self.events.append("lookup_started")
            if self._lookup_releases:
                self.release.set()
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                self.lookup_cancelled = True
                raise
            self.events.append("lookup_done")
            if self._lookup_result is None:
                return None
            return Retrieval(query=question, freshness=verdict.requirement,
                             evidence=[_ev(5, url="https://fresh.example.org/now")], newest_age=5.0)

        monkeypatch.setattr(web_memory, "retrieve", fake_retrieve)
        monkeypatch.setattr(lk, "retrieve", fake_retrieve)
        monkeypatch.setattr(lk, "_fast_lookup", fake_lookup)
        monkeypatch.setattr(lk, "claims_for", lambda q, limit=3: list(claims))
        for name, value in dict(
            web_memory_enabled=True, freshness_fast_lookup=True, freshness_router_enabled=True,
            living_knowledge_topical=True, knowledge_evidence_cache_ttl_s=0.0,
            knowledge_fast_early_lookup=True,
        ).items():
            monkeypatch.setattr(settings, name, value, raising=False)

    async def emit(self, kind, data):
        self.statuses.append((kind, data.get("text")))
        self.events.append("status")

    async def prepare(self, *, effort="fast", allow_network=True, pref="auto", question=QUESTION):
        async with asyncio.timeout(2.0):
            return await lk.prepare(question, effort=effort, mode="assistant", web_search_pref=pref,
                                    allow_network=allow_network, emit=self.emit)


def test_all_candidates_stale_starts_the_lookup_while_the_rerank_is_still_running(monkeypatch):
    """dev: the lookup waits for the rerank, which waits for the lookup — the
    asyncio.timeout above fails the test."""
    stage = _Stage(monkeypatch, [_ev(3600), _ev(86400, url="https://b.example.org/x")])
    p = asyncio.run(stage.prepare())
    assert stage.events.index("lookup_started") < stage.events.index("rerank_done")
    assert stage.events.index("status") < stage.events.index("rerank_done")
    assert p.decision == "fast_lookup" and p.searched
    assert [s["url"] for s in p.sources] == ["https://fresh.example.org/now"]
    assert stage.statuses == [("status", "Checking recent sources…")], "exactly one status"


def test_one_fresh_candidate_means_no_early_start(monkeypatch):
    """Guard: a candidate inside the max age can still make the result
    sufficient, so nothing starts before the judgement."""
    stage = _Stage(monkeypatch, [_ev(60, answer=0.99), _ev(86400, url="https://b.example.org/x")],
                   lookup_releases=False)

    async def run():
        task = asyncio.ensure_future(stage.prepare())
        await asyncio.sleep(0.05)
        assert "lookup_started" not in stage.events
        stage.release.set()
        return await task

    p = asyncio.run(run())
    assert p.decision == "local"
    assert "lookup_started" not in stage.events and stage.statuses == []


def test_a_fresh_but_insufficient_candidate_runs_the_old_sequence(monkeypatch):
    stage = _Stage(monkeypatch, [_ev(60, answer=0.01)], lookup_releases=False)

    async def run():
        task = asyncio.ensure_future(stage.prepare())
        await asyncio.sleep(0.05)
        stage.release.set()
        return await task

    p = asyncio.run(run())
    assert stage.events.index("rerank_done") < stage.events.index("status") < stage.events.index("lookup_started")
    assert p.decision == "fast_lookup"


def test_rerank_busy_cancels_the_early_lookup_and_answers_from_the_floor(monkeypatch):
    def busy(q, level):
        r = Retrieval(query=q, freshness=level, evidence=[_ev(3600)], newest_age=3600.0)
        r.degraded = "rerank_busy"
        return r

    stage = _Stage(monkeypatch, [_ev(3600)], result=busy)

    async def slow_lookup(question, verdict, **kw):
        stage.events.append("lookup_started")
        stage.release.set()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            stage.lookup_cancelled = True
            raise
        return None

    monkeypatch.setattr(lk, "_fast_lookup", slow_lookup)
    p = asyncio.run(stage.prepare())
    assert p.decision == "degraded_busy"
    assert stage.lookup_cancelled, "the lookup must not outlive the turn's decision"
    assert "verification was skipped" in p.grounding


def test_a_failed_lookup_falls_back_to_the_stale_local_grounding(monkeypatch):
    stage = _Stage(monkeypatch, [_ev(3600, answer=0.9)], lookup_result=None)
    p = asyncio.run(stage.prepare())
    assert stage.events.index("lookup_started") < stage.events.index("rerank_done")
    assert p.decision == "fast_lookup_failed"
    assert [s["url"] for s in p.sources] == ["https://rates.example.org/usd-inr"]
    assert p.grounding, "stale evidence with an honest date"


def test_a_fresh_resolved_claim_keeps_the_answer_local(monkeypatch):
    claim = {"claim": "USD/INR", "value": "83.2", "created_at": datetime.now(timezone.utc),
             "as_of": None, "url": "https://rates.example.org/usd-inr"}
    stage = _Stage(monkeypatch, [_ev(3600, answer=0.9)], lookup_releases=False, claims=[claim])

    async def run():
        task = asyncio.ensure_future(stage.prepare())
        await asyncio.sleep(0.05)
        stage.release.set()
        return await task

    p = asyncio.run(run())
    assert "lookup_started" not in stage.events
    assert p.decision == "local"


def test_no_candidates_reported_means_the_old_sequence(monkeypatch):
    """The hook never fires (a cache hit, an empty store): nothing early."""
    stage = _Stage(monkeypatch, [], lookup_releases=False)

    async def fake_retrieve(q, *, level=Freshness.RECENT, top_k=5, on_candidates=None, **kw):
        stage.events.append("rerank_done")
        return Retrieval(query=q, freshness=level)

    monkeypatch.setattr(web_memory, "retrieve", fake_retrieve)
    monkeypatch.setattr(lk, "retrieve", fake_retrieve)
    p = asyncio.run(stage.prepare())
    assert stage.events == ["rerank_done", "status", "lookup_started", "lookup_done"]
    assert p.decision == "fast_lookup"


@pytest.mark.parametrize("effort, allow, pref", [
    ("think", True, "auto"), ("max", True, "auto"), ("fast", False, "auto"), ("fast", True, "off"),
])
def test_it_is_armed_only_where_the_fast_lookup_may_run(monkeypatch, effort, allow, pref):
    stage = _Stage(monkeypatch, [_ev(3600)])
    stage.release.set()
    asyncio.run(stage.prepare(effort=effort, allow_network=allow, pref=pref))
    assert stage.hooked == [False]


def test_the_setting_off_restores_the_old_sequence(monkeypatch):
    stage = _Stage(monkeypatch, [_ev(3600)])
    monkeypatch.setattr(settings, "knowledge_fast_early_lookup", False, raising=False)
    stage.release.set()
    p = asyncio.run(stage.prepare())
    assert stage.hooked == [False]
    assert stage.events.index("rerank_done") < stage.events.index("lookup_started")
    assert p.decision == "fast_lookup"


def test_a_retrieval_without_the_hook_is_called_as_before(monkeypatch):
    """Before web_memory offers `on_candidates`, nothing extra is passed."""
    stage = _Stage(monkeypatch, [_ev(3600)])
    seen = []

    async def old(q, *, level=Freshness.RECENT, top_k=5, use_cache=True, effort="fast",
                  verdict=None, cache_store=True):
        seen.append(level)
        return Retrieval(query=q, freshness=level, evidence=[_ev(3600)], newest_age=3600.0)

    monkeypatch.setattr(web_memory, "retrieve", old)
    monkeypatch.setattr(lk, "retrieve", old)
    p = asyncio.run(stage.prepare())
    assert seen == [Freshness.REALTIME] and p.decision == "fast_lookup"


def test_the_speculative_static_run_never_carries_the_hook(monkeypatch):
    stage = _Stage(monkeypatch, [_ev(3600)])
    stage.release.set()
    calls = []

    async def spy(q, *, level=Freshness.RECENT, top_k=5, on_candidates=None, cache_store=True, **kw):
        calls.append((level, cache_store, on_candidates is not None))
        return Retrieval(query=q, freshness=level)

    async def ask(question):
        from app.freshness import Verdict, _MAX_AGE
        await asyncio.sleep(0.01)  # the speculative run starts meanwhile
        return Verdict(Freshness.RECENT, _MAX_AGE[Freshness.RECENT], "router")

    from app import freshness

    monkeypatch.setattr(freshness, "_ask_router", ask)
    monkeypatch.setattr(web_memory, "retrieve", spy)
    monkeypatch.setattr(lk, "retrieve", spy)
    asyncio.run(stage.prepare(question="tell me about the rupee"))
    assert (Freshness.STATIC, False, False) in calls, "the speculative STATIC guess, unhooked"
    assert (Freshness.RECENT, True, True) in calls, "the full, level-matching retrieval, hooked"
