"""Fast pre-pass latency round (2026-09-14): salvage, shadow rerank limits,
and the delayed message-embedding backfill.

Measured before this change (production trace of a Fast "hi ??", 5,997 ms):
the freshness router timed out, so the verdict was 'default' where the
speculative retrieval had guessed 'router'. Same level, different
supersession rule, so HEAD threw the finished speculative run away and ran
the whole retrieval again: a second embed, dense scan, lexical query, merge,
rank and a second 16-passage rerank (the reranker processed 15,439 prompt
tokens that minute).

Pinned here:
  - salvage: one retrieval, not two, when only the partition differs; a
    router STATIC answer still cancels; a failed, degraded, unjudged (cache
    hit) or out-of-time speculative run falls back to the full retrieve;
  - the Fast rerank limits are SHADOW ONLY by default: rerank.score receives
    exactly today's head and knowledge_rerank_shadow_total counts what the
    limits would have done;
  - switched on, a gated or capped result is never cached, so a Think turn
    with the same question afterwards gets a fully judged result;
  - the backfill task is registered at once but embeds only after its delay.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import freshness, memory_semantic, metrics, rerank, web_index, web_memory
from app import living_knowledge as lk
from app.config import settings
from app.freshness import Freshness, Verdict
from app.web_memory import Evidence, Retrieval

QUESTION = "what is the cost of a used bicycle"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    web_memory.cache_clear()
    rerank.reset_for_tests()
    monkeypatch.setattr(settings, "knowledge_evidence_cache_ttl_s", 0.0)
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(settings, "living_knowledge_topical", True)
    monkeypatch.setattr(settings, "freshness_router_enabled", True)
    monkeypatch.setattr(settings, "freshness_fast_skip_router", False)
    monkeypatch.setattr(settings, "knowledge_fast_concurrent_retrieve", True)
    monkeypatch.setattr(settings, "knowledge_fast_speculative_salvage", True)
    monkeypatch.setattr(settings, "knowledge_fast_rerank_weak_gate", False)
    monkeypatch.setattr(settings, "knowledge_fast_rerank_max_docs", 0)
    monkeypatch.setattr(settings, "knowledge_rerank", True)
    monkeypatch.setattr(settings, "knowledge_rerank_candidates", 12)
    monkeypatch.setattr(lk, "claims_for", lambda q, limit=3: [])
    yield
    web_memory.cache_clear()
    rerank.reset_for_tests()


def _counter(name, **labels):
    return metrics._counters.get(name, {}).get(tuple(sorted(labels.items())), 0.0)


def _ev(i, *, days=1.0, dense=0.5, lexical=0.5, answer=-1.0, score=None):
    return Evidence(
        url=f"https://site{i}.example.org/page", title=f"Page {i}", text=f"passage number {i}",
        domain=f"site{i}.example.org", authority=40,
        fetched_at=datetime.now(timezone.utc) - timedelta(days=days),
        dense=dense, lexical=lexical, answer=answer, score=0.5 if score is None else score, page_id=i,
    )


def _router(monkeypatch, outcome, delay=0.0):
    async def ask(question):
        await asyncio.sleep(delay)
        if outcome == "down":
            raise RuntimeError("router unavailable")
        return Verdict(outcome, freshness._MAX_AGE[outcome], "router")

    monkeypatch.setattr(freshness, "_ask_router", ask)


def _prepare(question=QUESTION, effort="fast"):
    return lk.prepare(question, effort=effort, mode="assistant", web_search_pref="off", allow_network=False)


# ── salvage ──────────────────────────────────────────────────────────────────


def _judged_retrieval(q, level, *, degraded="", judged=True):
    items = [_ev(1, days=400.0, answer=0.95, lexical=0.9), _ev(2, days=2.0, answer=0.95, lexical=0.9)]
    for e in items:
        e.authority = web_memory.AUTHORITY_REFERENCE
    out = Retrieval(query=q, freshness=level, evidence=list(items), degraded=degraded)
    if judged:
        out._judged = items
    return out


def test_salvage_calls_retrieve_once_while_the_speculative_run_is_still_running(monkeypatch):
    _router(monkeypatch, "down")  # fails at once: the speculative run is mid-flight
    calls = []

    async def fake_retrieve(q, **kw):
        calls.append(kw["verdict"].reason)
        await asyncio.sleep(0.05)
        return _judged_retrieval(q, kw["level"])

    monkeypatch.setattr(lk, "retrieve", fake_retrieve)
    before = _counter("knowledge_salvage_total", how="salvaged")
    prepared = run(_prepare())
    assert prepared.verdict.reason == "default"
    assert calls == ["router"]
    assert _counter("knowledge_salvage_total", how="salvaged") == before + 1
    # Built from copies: prepare's filtering cannot edit the judged list.
    assert [e.page_id for e in prepared.retrieval.evidence] == [1, 2]
    assert not prepared.retrieval.superseded, "'default' forbids supersession"


def test_a_router_static_answer_still_cancels_the_speculative_run(monkeypatch):
    _router(monkeypatch, Freshness.STATIC)
    monkeypatch.setattr(lk, "_topical_precheck", lambda q: None)
    levels, cancelled = [], []

    async def fake_retrieve(q, **kw):
        levels.append(kw["level"])
        if kw["level"] is not Freshness.STATIC:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        return Retrieval(query=q, freshness=kw["level"])

    monkeypatch.setattr(lk, "retrieve", fake_retrieve)
    prepared = run(_prepare())
    assert prepared.verdict.requirement is Freshness.STATIC
    assert levels[1:] == [Freshness.STATIC] and cancelled == [True]


@pytest.mark.parametrize("shape", ["raises", "degraded", "cache_hit_without_judged_list"])
def test_a_speculative_run_salvage_cannot_use_falls_back_to_the_full_retrieve(monkeypatch, shape):
    _router(monkeypatch, "down", delay=0.02)
    calls = []

    async def fake_retrieve(q, **kw):
        calls.append(kw["verdict"].reason)
        if kw["verdict"].reason == "router":
            if shape == "raises":
                raise RuntimeError("embedding sidecar down")
            if shape == "degraded":
                return _judged_retrieval(q, kw["level"], degraded="rerank_busy")
            return _judged_retrieval(q, kw["level"], judged=False)
        return Retrieval(query=q, freshness=kw["level"])

    monkeypatch.setattr(lk, "retrieve", fake_retrieve)
    prepared = run(_prepare())
    assert calls == ["router", "default"]
    assert prepared.retrieval is not None and prepared.retrieval.evidence == []


def test_a_speculative_run_past_the_prepare_deadline_falls_back(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_prepare_deadline_s", 0.08)
    _router(monkeypatch, "down")
    calls, cancelled = [], []

    async def fake_retrieve(q, **kw):
        calls.append(kw["verdict"].reason)
        if kw["verdict"].reason == "router":
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        return Retrieval(query=q, freshness=kw["level"])

    monkeypatch.setattr(lk, "retrieve", fake_retrieve)
    started = time.perf_counter()
    run(_prepare())
    assert time.perf_counter() - started < 1.0
    assert calls == ["router", "default"] and cancelled == [True]


def test_with_salvage_off_the_full_retrieve_runs_as_before(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_fast_speculative_salvage", False)
    _router(monkeypatch, "down", delay=0.02)
    calls = []

    async def fake_retrieve(q, **kw):
        calls.append(kw["verdict"].reason)
        return _judged_retrieval(q, kw["level"])

    monkeypatch.setattr(lk, "retrieve", fake_retrieve)
    run(_prepare())
    assert calls == ["router", "default"]


def test_repartition_copies_and_recomputes_under_the_given_verdict():
    spec = _judged_retrieval(QUESTION, Freshness.RECENT)
    router = Verdict(Freshness.RECENT, freshness._MAX_AGE[Freshness.RECENT], "router")
    default = Verdict(Freshness.RECENT, freshness._MAX_AGE[Freshness.RECENT], "default")
    under_router = web_memory.repartition(spec, Freshness.RECENT, router, top_k=5)
    under_default = web_memory.repartition(spec, Freshness.RECENT, default, top_k=5)
    assert [e.page_id for e in under_router.evidence] == [2]
    assert [e.page_id for e in under_router.superseded] == [1]
    assert [e.page_id for e in under_default.evidence] == [1, 2] and not under_default.superseded
    assert all(a is not b for a in under_default.evidence for b in spec._judged)
    assert not hasattr(under_default, "_judged")
    assert under_default.newest_age == pytest.approx(min(e.age_seconds for e in spec._judged), abs=5)


# ── shadow rerank limits ─────────────────────────────────────────────────────


def _ranked(weak: bool):
    """24 candidates in blend order. Weak: nothing reaches dense 0.35 or
    lexical 0.34. Otherwise the best-dense and best-lexical candidates sit
    deep in the blend order, as a recency-weighted blend puts them."""
    out = []
    for i in range(24):
        dense = 0.1 + i * 0.005 if weak else 0.2 + (0.6 if i in (17, 18) else 0.0)
        lexical = 0.05 if weak else 0.1 + (0.5 if i in (19, 20) else 0.0)
        out.append(_ev(i, dense=dense, lexical=lexical, score=0.9 - i * 0.01))
    return out


def _todays_head(ranked, n=12):
    """_answerability's rule on dev@ca6b6f3, written out: the top n of the
    blend plus the top 4 of each half."""
    head = list(ranked[:n])
    seen = {id(e) for e in head}
    for key in ("dense", "lexical"):
        for e in sorted(ranked, key=lambda x: getattr(x, key), reverse=True)[:4]:
            if id(e) not in seen and getattr(e, key) > 0:
                head.append(e)
                seen.add(id(e))
    return [web_memory._rerank_text(e) for e in head]


def _recording_score(monkeypatch, sent, relevant_ids=()):
    async def score(query, docs, **kw):
        sent.append(list(docs))
        return [0.9 if any(d.startswith(f"Page {i}\n") for i in relevant_ids) else 0.05 for d in docs]

    monkeypatch.setattr(rerank, "score", score)


@pytest.mark.parametrize("weak", [True, False])
def test_with_the_knobs_off_rerank_receives_todays_head_and_the_shadow_counters_fire(monkeypatch, weak):
    ranked = _ranked(weak)
    expected = _todays_head(ranked)
    sent = []
    # A relevant page in today's head but outside an 8-passage cap: page 18 is
    # a top-4 dense pick (the cap keeps only page 17), page 20 a top-4 dense
    # pick of the weak list (the cap keeps only page 23).
    _recording_score(monkeypatch, sent, relevant_ids=(18,) if not weak else (20,))
    skip = _counter("knowledge_rerank_shadow_total", would="skip_weak")
    cap = _counter("knowledge_rerank_shadow_total", would="cap_drop")
    out, degraded = run(web_memory._answerability(QUESTION, ranked, level=Freshness.RECENT, effort="fast"))
    assert degraded == ""
    assert sent == [expected] and len(expected) == 16
    assert _counter("knowledge_rerank_shadow_total", would="skip_weak") == skip + (1 if weak else 0)
    assert _counter("knowledge_rerank_shadow_total", would="cap_drop") == cap + 1
    assert len(out) == 24


def test_think_counts_no_shadow_and_a_static_question_keeps_its_own_gate(monkeypatch):
    sent = []
    _recording_score(monkeypatch, sent)
    skip = _counter("knowledge_rerank_shadow_total", would="skip_weak")
    cap = _counter("knowledge_rerank_shadow_total", would="cap_drop")
    run(web_memory._answerability(QUESTION, _ranked(True), level=Freshness.RECENT, effort="think"))
    run(web_memory._answerability(QUESTION, _ranked(True), level=Freshness.STATIC, effort="fast"))
    assert len(sent) == 1 and len(sent[0]) == 16
    assert _counter("knowledge_rerank_shadow_total", would="skip_weak") == skip
    assert _counter("knowledge_rerank_shadow_total", would="cap_drop") == cap


def _stub_retrieval_stages(monkeypatch, weak: bool):
    """retrieve() with no database or engine: the dense half returns 24 hits,
    the lexical half nothing, _page_meta nothing."""

    async def dense(query, top_k=6, site_prefix=""):
        hits = []
        for i in range(24):
            distance = 0.95 if weak else (0.1 if i < 6 else 0.8)
            hits.append({"url": f"https://site{i}.example.org/page", "title": f"Page {i}",
                         "text": f"unrelated words {i} " * 3, "fetched_at": "", "score": distance})
        return hits

    monkeypatch.setattr(web_index, "retrieve", dense)
    monkeypatch.setattr(web_memory, "_lexical_candidates", lambda q, limit: [])
    monkeypatch.setattr(web_memory, "_page_meta", lambda urls, ids=(): {})
    monkeypatch.setattr(web_memory, "_collapse_duplicates", lambda ranked, query="": ranked)
    monkeypatch.setattr(settings, "knowledge_evidence_cache_ttl_s", 60.0)
    monkeypatch.setattr(rerank, "enabled", lambda: True)


@pytest.mark.parametrize("knob", ["weak_gate", "max_docs"])
def test_with_a_knob_on_the_limited_result_is_never_cached_and_think_gets_a_judged_one(monkeypatch, knob):
    weak = knob == "weak_gate"
    _stub_retrieval_stages(monkeypatch, weak=weak)
    if weak:
        monkeypatch.setattr(settings, "knowledge_fast_rerank_weak_gate", True)
    else:
        monkeypatch.setattr(settings, "knowledge_fast_rerank_max_docs", 8)
    sent = []
    _recording_score(monkeypatch, sent, relevant_ids=range(24))
    verdict = Verdict(Freshness.RECENT, freshness._MAX_AGE[Freshness.RECENT], "router")

    fast = run(web_memory.retrieve(QUESTION, level=Freshness.RECENT, top_k=5, effort="fast", verdict=verdict))
    assert fast.evidence, "the premise: a non-empty, non-degraded result HEAD would cache"
    assert not fast.degraded
    if weak:
        assert sent == [], "the weak-candidate gate skipped the cross-encoder"
    else:
        assert len(sent) == 1 and len(sent[0]) <= 8, [len(d) for d in sent]
    assert len(web_memory._cache) == 0, "a Fast-limited result was cached"
    web_memory.cache_result(QUESTION, level=Freshness.RECENT, top_k=5, result=fast)
    assert len(web_memory._cache) == 0, "cache_result stored a Fast-limited result"

    sent.clear()
    think = run(web_memory.retrieve(QUESTION, level=Freshness.RECENT, top_k=5, effort="think", verdict=verdict))
    assert len(sent) == 1 and len(sent[0]) == 12, "Think judged today's full head (12 blend, no extra half)"
    assert all(e.scored for e in think.evidence)
    assert len(web_memory._cache) == 1


def test_a_salvaged_fast_limited_result_is_not_cached_either(monkeypatch):
    _stub_retrieval_stages(monkeypatch, weak=False)
    monkeypatch.setattr(settings, "knowledge_fast_rerank_max_docs", 8)
    sent = []
    _recording_score(monkeypatch, sent, relevant_ids=range(24))
    _router(monkeypatch, "down", delay=0.05)
    prepared = run(_prepare())
    assert prepared.verdict.reason == "default"
    assert len(sent) == 1, "salvaged: one rerank"
    assert prepared.retrieval.evidence and getattr(prepared.retrieval, "_no_cache", False)
    assert len(web_memory._cache) == 0


# ── the delayed backfill ─────────────────────────────────────────────────────


def test_the_backfill_task_starts_at_once_but_embeds_after_the_delay(monkeypatch):
    monkeypatch.setattr(settings, "message_backfill_delay_s", 0.15)
    calls = []

    async def ensure(user_id):
        calls.append((user_id, time.perf_counter()))
        return 0

    monkeypatch.setattr(memory_semantic, "ensure_message_embeddings", ensure)

    async def body():
        started = time.perf_counter()
        memory_semantic._backfill_in_background(7)
        task = memory_semantic._backfills.get(7)
        assert task is not None and not task.done(), "registered at once"
        await asyncio.sleep(0.05)
        assert calls == [], "embedded before the delay"
        memory_semantic._backfill_in_background(7)
        assert memory_semantic._backfills.get(7) is task, "one in flight per user"
        await asyncio.wait_for(task, timeout=2.0)
        return started

    started = run(body())
    assert len(calls) == 1 and calls[0][0] == 7
    assert calls[0][1] - started >= 0.14
    assert 7 not in memory_semantic._backfills


def test_a_zero_delay_embeds_on_the_next_loop_turn(monkeypatch):
    monkeypatch.setattr(settings, "message_backfill_delay_s", 0.0)
    calls = []

    async def ensure(user_id):
        calls.append(user_id)
        return 0

    monkeypatch.setattr(memory_semantic, "ensure_message_embeddings", ensure)

    async def body():
        memory_semantic._backfill_in_background(9)
        await asyncio.sleep(0.01)

    run(body())
    assert calls == [9]
