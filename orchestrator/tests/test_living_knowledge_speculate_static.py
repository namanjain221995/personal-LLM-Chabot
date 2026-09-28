"""The timeless branch uses the retrieval that is already running (2026-09-28).

WHAT WAS MEASURED. `prepare` starts a speculative retrieval before it asks the
freshness router, so the router's round trip (118-265 ms through the live
router, 15 of 20 live Fast turns ask it) overlaps with real work. But:

  * the router is asked ONLY when the deterministic pass cannot decide
    (`freshness.router_would_be_asked` is `_deterministic(...) is None`), and
    for exactly that population `classify_offline` is the bare
    RECENT/'default' — so the guess was RECENT on every single turn;
  * the router answers STATIC for most of that population: 13 of 15 on a
    graded corpus through the live router, and 15 of 19 pre-passes on the live
    process (knowledge_decision_total static_model 10 + static_topical 5,
    against fast_lookup 2 + local 2);
  * the timeless branch (`_topical`) then cancelled the RECENT run and ran a
    STATIC retrieval of its own, IN SERIES with the round trip. In the live
    container that was `retrieve` twice, 134 ms cancelled + 386 ms fresh, on
    top of a 136 ms router call.

So the guess is STATIC (KNOWLEDGE_FAST_SPECULATE_STATIC, default on) and the
timeless branch reuses the run instead of starting a second one. Measured in
the live container over 8 questions, load 5.7-7.1: pre-pass p50 394 -> 328 ms,
and on the questions that actually ask the router 400 -> 282, 383 -> 227,
407 -> 294 ms — with the decision, the sources and the grounding bytes
identical on all 8, including three `static_topical` hits carrying 6.4-6.7 KB
of real grounding.

WHY THE EVIDENCE CANNOT CHANGE. Everything in `web_memory.retrieve` up to and
including the rerank ignores the verdict; `_partition` is the only step that
reads it, and at STATIC `web_memory.supersession_allowed` returns False
whatever the verdict, so `_partition` is the identity there. The speculative
run is issued at top_k=4 — the number `_topical` asks for — so `retrieve`'s
over-fetch (`want = max(top_k * 3, 12)`) matches too. A verdict that is NOT
STATIC has a level the run did not use, and the full retrieval runs exactly as
it does today.
"""
from __future__ import annotations

import asyncio

import pytest

from app import db, freshness, web_memory
from app import living_knowledge as lk
from app.config import settings
from app.freshness import Freshness, Verdict
from app.web_memory import Retrieval


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with db.connection() as con:
        con.execute("TRUNCATE web_pages RESTART IDENTITY CASCADE")
    web_memory.cache_clear()
    lk._page_vocabulary.reset()
    monkeypatch.setattr(settings, "knowledge_evidence_cache_ttl_s", 0.0)
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(settings, "living_knowledge_topical", True)
    monkeypatch.setattr(settings, "freshness_router_enabled", True)
    monkeypatch.setattr(settings, "knowledge_fast_concurrent_retrieve", True, raising=False)
    monkeypatch.setattr(settings, "knowledge_fast_topical_deadline_s", 0.0, raising=False)
    monkeypatch.setattr(settings, "knowledge_fast_topical_hit_budget_s", 0.0, raising=False)
    monkeypatch.setattr(settings, "freshness_fast_skip_router", False, raising=False)
    monkeypatch.setattr(lk, "claims_for", lambda q, limit=3: [])
    yield


#: A question the deterministic pass cannot classify, which is the only
#: population that reaches the router at all — and one that matches the strong
#: page test_living_knowledge_fast_budget seeds, so the equivalence test below
#: compares real evidence and not two empty lists.
UNDECIDED = "photosynthesis simulator PHOTO_RATE config.yaml"


def _router(monkeypatch, answer, *, calls=None):
    async def ask(question):
        if calls is not None:
            calls.append(question)
        await asyncio.sleep(0)
        return Verdict(answer, freshness._MAX_AGE[answer], "router")

    monkeypatch.setattr(freshness, "_ask_router", ask)


def _prepare(question=UNDECIDED, effort="fast", **kw):
    call = dict(effort=effort, mode="assistant", web_search_pref="off", allow_network=False)
    call.update(kw)
    return lk.prepare(question, **call)


def test_the_router_is_only_asked_for_questions_offline_calls_recent_default():
    """The premise of the change: for every question that reaches the router,
    `classify_offline` is the bare RECENT/'default', so a guess taken from it
    is RECENT every time."""
    year = 2026
    assert freshness.router_would_be_asked(UNDECIDED, now_year=year)
    offline = freshness.classify_offline(UNDECIDED, now_year=year)
    assert (offline.requirement, offline.reason) == (Freshness.RECENT, "default")


def test_a_timeless_verdict_reuses_the_speculative_run_instead_of_retrieving_twice(monkeypatch):
    _router(monkeypatch, Freshness.STATIC)
    monkeypatch.setattr(lk, "_topical_precheck", lambda q: None)
    seen = []

    async def fake_retrieve(q, **kw):
        seen.append((kw["level"], kw["top_k"], kw.get("cache_store", True)))
        out = Retrieval(query=q, freshness=kw["level"])
        out._judged = []
        return out

    monkeypatch.setattr(lk, "retrieve", fake_retrieve)
    prepared = run(_prepare())
    assert prepared.verdict.requirement is Freshness.STATIC
    # ONE retrieval, at the level and top_k the timeless branch needs.
    # ONE call, at STATIC/top_k=4, and it is the SPECULATIVE one
    # (cache_store=False, so `_reuse_static` caches it under the real verdict
    # rather than `retrieve` caching it under a guessed one).
    assert seen == [(Freshness.STATIC, 4, False)], seen
    assert prepared.decision == "static_model"


def test_the_reused_run_is_not_cancelled_by_prepares_own_cleanup(monkeypatch):
    """`prepare`'s `finally` cancels the speculative future. Once it has been
    handed to `_topical` it is being awaited there, so the handoff has to clear
    it — otherwise the branch awaits a future its caller cancels."""
    _router(monkeypatch, Freshness.STATIC)
    monkeypatch.setattr(lk, "_topical_precheck", lambda q: None)
    started = asyncio.Event()

    async def slow_retrieve(q, **kw):
        started.set()
        await asyncio.sleep(0.05)
        out = Retrieval(query=q, freshness=kw["level"])
        out._judged = []
        return out

    monkeypatch.setattr(lk, "retrieve", slow_retrieve)
    prepared = run(_prepare())
    assert prepared.retrieval is not None
    assert prepared.retrieval.freshness is Freshness.STATIC
    assert prepared.degraded == ""


@pytest.mark.parametrize("answer", [Freshness.RECENT, Freshness.REALTIME])
def test_a_time_sensitive_verdict_still_runs_the_full_retrieval(monkeypatch, answer):
    """The guess was wrong: the level the run used is not the level the answer
    needs, so the retrieval runs again exactly as it does today. This is the
    case the change trades against, and it must not silently keep the STATIC
    result."""
    _router(monkeypatch, answer)
    levels, cancelled = [], []

    async def fake_retrieve(q, **kw):
        levels.append(kw["level"])
        if kw["level"] is Freshness.STATIC:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        out = Retrieval(query=q, freshness=kw["level"])
        out._judged = []
        return out

    monkeypatch.setattr(lk, "retrieve", fake_retrieve)
    prepared = run(_prepare())
    assert prepared.verdict.requirement is answer
    assert levels == [Freshness.STATIC, answer], levels
    assert cancelled == [True]
    assert prepared.retrieval is not None and prepared.retrieval.freshness is answer


def test_the_reuse_is_the_same_evidence_as_a_second_retrieval(monkeypatch):
    """The equivalence claim, on a real retrieval against the store: the same
    question through the reuse and through a fresh STATIC retrieval must give
    the same evidence, the same order and the same scores."""
    from tests.test_living_knowledge_fast_budget import DOCS_TEXT, DOCS_TITLE, DOCS_URL, _page, _seed_docs

    _seed_docs(monkeypatch)
    _page("spec1", DOCS_URL, DOCS_TITLE, DOCS_TEXT)
    _router(monkeypatch, Freshness.STATIC)

    async def body():
        prepared = await _prepare()
        fresh = await web_memory.retrieve(UNDECIDED, level=Freshness.STATIC, top_k=4)
        return prepared, fresh

    prepared, fresh = run(body())
    assert prepared.retrieval is not None
    got = [(e.url, round(e.score, 6), round(e.answer, 6)) for e in prepared.retrieval.evidence]
    want = [(e.url, round(e.score, 6), round(e.answer, 6)) for e in fresh.evidence]
    assert got == want, (got, want)
    assert prepared.retrieval.superseded == [] and prepared.retrieval.conflict is False


def test_the_knob_off_restores_the_recent_guess(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_fast_speculate_static", False, raising=False)
    _router(monkeypatch, Freshness.STATIC)
    monkeypatch.setattr(lk, "_topical_precheck", lambda q: None)
    levels = []

    async def fake_retrieve(q, **kw):
        levels.append((kw["level"], kw["top_k"]))
        out = Retrieval(query=q, freshness=kw["level"])
        out._judged = []
        return out

    monkeypatch.setattr(lk, "retrieve", fake_retrieve)
    run(_prepare())
    assert levels == [(Freshness.RECENT, 5), (Freshness.STATIC, 4)], levels
