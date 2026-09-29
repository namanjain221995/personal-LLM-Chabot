"""The STATIC topical retrieval stops after the dense half when nothing can
become a topical hit (2026-09-29, track retrieval-does-less, B1).

WHAT WAS MEASURED. A timeless Fast question runs a STATIC retrieval whose only
reader is living_knowledge._topical, and _topical grounds the answer only on a
candidate with dense >= 0.35 (or one the cross-encoder judged, which the STATIC
pre-gate allows only above the same 0.35). On the live store the best dense
score was under 0.35 for 37 of 40 timeless questions, and every one of those
37 still ran the lexical query, the CPU merge of 24 full pages, the meta read
and the rank for a result that was thrown away: prepare p50 263.6 ms against
88.0 ms with a dense-first exit, 0 decision or grounding differences (n=40,
interleaved A/B on the live corpus).

WHAT IS PINNED HERE.
  * `retrieve(..., topical_gate=True)` at STATIC with every dense hit under
    TOPICAL_DENSE_FLOOR: the lexical half is cancelled, nothing is merged,
    read, ranked or reranked, the empty result is not cached, no demand bump,
    and `_judged` is empty for a speculative (cache_store=False) caller.
  * A hit at the floor, or any other level, runs the full retrieval unchanged.
  * The floor, the rerank pre-gate and living_knowledge's hit rule are one
    value, and a seeded property test (2,000 candidate sets) shows that a
    set whose best dense is under the floor never becomes a topical hit
    through the real `_rank_candidates`, `_answerability` and `_topical`.
  * Every web_eval case, with the topical call sites forced to gate, gives
    the decision, sources, grounding and cross-encoder inputs it gives without.
"""
from __future__ import annotations

import asyncio
import math
import random
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import db, freshness, metrics, rerank, web_index, web_memory
from app import living_knowledge as lk
from app.config import settings
from app.freshness import Freshness, Verdict
from app.web_memory import Evidence, Retrieval

QUESTION = "how are badger setts ventilated through their tunnels"
#: Read with a fallback so that, on code without the constant, each test here
#: fails on its own assertion instead of the whole module failing to import.
FLOOR = getattr(web_memory, "TOPICAL_DENSE_FLOOR", 0.35)
BELOW = math.nextafter(FLOOR, 0.0)


#: One instant for the whole module: recency is part of every score, and two
#: runs compared for equality must not differ by the milliseconds between them.
FROZEN = datetime.now(timezone.utc).replace(microsecond=0)


def _now() -> datetime:
    return FROZEN


def _count(name: str, **labels: str) -> float:
    key = metrics._clean(labels, name)
    return float(metrics._counters.get(name, {}).get(key, 0.0))


def _static_verdict() -> Verdict:
    return Verdict(Freshness.STATIC, freshness._MAX_AGE[Freshness.STATIC], "router")


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    web_memory.cache_clear()
    monkeypatch.setattr(web_memory, "_now", _now)
    for name, value in dict(
        web_memory_enabled=True, knowledge_rerank=True, knowledge_evidence_cache_ttl_s=60.0,
        knowledge_rerank_candidates=12, living_knowledge_topical=True,
    ).items():
        monkeypatch.setattr(settings, name, value, raising=False)
    yield
    web_memory.cache_clear()


def _dense_hits(distances):
    return [
        {"url": f"https://dense{i}.example/setts", "title": f"Badger setts {i}",
         "text": f"Badger setts are ventilated through their tunnels, chunk {i}.",
         "page_id": 100 + i, "fetched_at": "", "score": d}
        for i, d in enumerate(distances)
    ]


def _lexical_rows():
    unit = ("Badger setts are ventilated by their many tunnels; air enters low and "
            "leaves through the higher entrances. ")
    return [
        {"id": 200 + i, "url": f"https://lex{i}.example/setts", "title": f"Setts {i}",
         "text": unit * (20 + i) + f" note{i}", "domain": f"lex{i}.example", "authority": 40,
         "fetched_at": _now() - timedelta(days=3 + i), "published_at": None, "modified_at": None,
         "source_type": "", "origin": "search", "content_hash": f"lexhash{i}"}
        for i in range(5)
    ]


def _instrument(monkeypatch, distances, *, lexical_delay: float = 0.0):
    """Stub the two halves and the cross-encoder; count every later stage."""
    calls = {k: 0 for k in ("merge", "meta", "rank", "rerank", "bump", "lexical_started")}
    calls["lexical_cancelled_at"] = None
    calls["sent"] = []

    async def dense(query, top_k=6, site_prefix=""):
        await asyncio.sleep(0.01)  # the embed + scan round trip; the lexical half runs meanwhile
        return _dense_hits(distances)[:top_k]

    rows = _lexical_rows()

    def lexical(query, limit):
        return [dict(r) for r in rows][:limit]

    monkeypatch.setattr(web_index, "retrieve", dense)
    monkeypatch.setattr(web_memory, "_lexical_candidates", lexical)
    real_run_in_thread = db.run_in_thread

    async def run_in_thread(fn, *args, **kwargs):
        if fn is lexical:
            calls["lexical_started"] += 1
            try:
                if lexical_delay:
                    await asyncio.sleep(lexical_delay)
            except asyncio.CancelledError:
                calls["lexical_cancelled_at"] = asyncio.get_running_loop().time()
                raise
            return fn(*args, **kwargs)
        return await real_run_in_thread(fn, *args, **kwargs)

    monkeypatch.setattr(db, "run_in_thread", run_in_thread)

    def counted(name, real):
        def run(*args, **kwargs):
            calls[name] += 1
            return real(*args, **kwargs)
        return run

    monkeypatch.setattr(web_memory, "_merge_candidates", counted("merge", web_memory._merge_candidates))
    monkeypatch.setattr(web_memory, "_rank_candidates", counted("rank", web_memory._rank_candidates))
    monkeypatch.setattr(web_memory, "_page_meta", counted("meta", lambda urls, ids=(): {}))
    monkeypatch.setattr(web_memory, "_bump_retrieval", counted("bump", lambda ids: None))

    async def score(query, docs, **kwargs):
        calls["rerank"] += 1
        calls["sent"].append(list(docs))
        return [0.9 - 0.01 * i for i in range(len(docs))]

    monkeypatch.setattr(rerank, "score", score)
    return calls


# ---------------------------------------------------------------------------
# The exit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cache_store", [True, False])
def test_no_dense_hit_at_the_floor_ends_the_retrieval_after_the_dense_half(monkeypatch, cache_store):
    # Dense scores 0.30, 0.20, 0.05: every one under the floor.
    # The lexical half would take 3 s: long enough that no scheduler or GC
    # pause on a loaded CI runner can pass for "the retrieve waited for it".
    calls = _instrument(monkeypatch, [0.70, 0.80, 0.95], lexical_delay=3.0)
    exits = _count("knowledge_topical_gate_total", outcome="exit")

    async def go():
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await web_memory.retrieve(
            QUESTION, level=Freshness.STATIC, top_k=4, topical_gate=True, cache_store=cache_store,
        )
        returned = loop.time()
        await asyncio.sleep(0.02)  # the cancelled lexical task settles
        cancelled_at = calls["lexical_cancelled_at"]
        await asyncio.sleep(0.05)  # a demand bump would have been scheduled by now
        return result, returned - started, cancelled_at, returned

    result, took, cancelled_at, returned = asyncio.run(go())

    assert result.evidence == [] and result.superseded == [] and result.degraded == ""
    assert calls["lexical_started"] == 1, "both halves still start together"
    # Nothing but the retrieve can cancel it before go() returns.
    assert cancelled_at is not None and cancelled_at <= returned + 1.0, (
        "the lexical half was not cancelled by the retrieve itself"
    )
    assert took < 1.5, f"the retrieve waited for the lexical half ({took * 1000:.0f} ms)"
    assert (calls["merge"], calls["meta"], calls["rank"], calls["rerank"]) == (0, 0, 0, 0)
    assert calls["bump"] == 0
    assert len(web_memory._cache) == 0, "an early-exit result must never be cached"
    if cache_store:
        assert not hasattr(result, "_judged")
    else:
        assert getattr(result, "_judged") == []
    assert _count("knowledge_topical_gate_total", outcome="exit") == exits + 1


def test_the_same_question_without_the_gate_runs_every_stage(monkeypatch):
    """The control for the test above: the stages it counts as skipped do run."""
    calls = _instrument(monkeypatch, [0.70, 0.80, 0.95])
    result = asyncio.run(web_memory.retrieve(QUESTION, level=Freshness.STATIC, top_k=4))
    assert result.evidence, "premise: the lexical half finds the setts pages"
    assert (calls["merge"], calls["meta"], calls["rank"]) == (1, 1, 1)
    # ... and still grounds nothing: the pre-gate keeps the cross-encoder out
    # and no candidate carries the dense agreement a topical hit needs.
    assert calls["rerank"] == 0
    assert max(e.dense for e in result.evidence) < FLOOR


def _evidence_shape(result):
    return (
        [(e.url, e.title, e.text, e.dense, e.lexical, e.score, e.answer, e.page_id) for e in result.evidence],
        [e.url for e in result.superseded],
        result.conflict,
        result.degraded,
    )


@pytest.mark.parametrize("distances", [[0.65, 0.95], [0.30, 0.50, 0.90], [0.95, 0.90, 0.65]])
def test_a_dense_hit_at_the_floor_runs_the_full_retrieval_unchanged(monkeypatch, distances):
    assert any(web_memory._dense_score(d) >= FLOOR for d in distances), "premise: a hit reaches the floor"
    calls = _instrument(monkeypatch, distances)
    passes = _count("knowledge_topical_gate_total", outcome="pass")
    shapes = {}
    for gate in (False, True):
        web_memory.cache_clear()
        calls["sent"].clear()
        result = asyncio.run(web_memory.retrieve(
            QUESTION, level=Freshness.STATIC, top_k=4, topical_gate=gate, use_cache=False,
        ))
        shapes[gate] = (_evidence_shape(result), [list(s) for s in calls["sent"]])
    assert shapes[True] == shapes[False]
    assert shapes[True][0][0], "premise: the full retrieval found evidence"
    assert shapes[True][1], "premise: the cross-encoder judged the head"
    assert _count("knowledge_topical_gate_total", outcome="pass") == passes + 1


@pytest.mark.parametrize("level", [Freshness.RECENT, Freshness.REALTIME])
def test_the_gate_is_ignored_for_a_time_sensitive_level(monkeypatch, level):
    calls = _instrument(monkeypatch, [0.70, 0.80, 0.95])
    shapes = {}
    for gate in (False, True):
        web_memory.cache_clear()
        calls["sent"].clear()
        result = asyncio.run(web_memory.retrieve(QUESTION, level=level, top_k=5, topical_gate=gate, use_cache=False))
        shapes[gate] = (_evidence_shape(result), [list(s) for s in calls["sent"]])
    assert shapes[True] == shapes[False]
    assert calls["merge"] == 2


def test_a_retrieve_cancelled_during_the_dense_half_cancels_the_lexical_half(monkeypatch):
    calls = _instrument(monkeypatch, [0.70], lexical_delay=0.3)
    entered = threading.Event()

    async def slow_dense(query, top_k=6, site_prefix=""):
        entered.set()
        await asyncio.sleep(5)
        return []

    monkeypatch.setattr(web_index, "retrieve", slow_dense)

    async def go():
        task = asyncio.ensure_future(
            web_memory.retrieve(QUESTION, level=Freshness.STATIC, top_k=4, topical_gate=True)
        )
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.01)
        return calls["lexical_cancelled_at"]

    assert asyncio.run(go()) is not None


# ---------------------------------------------------------------------------
# One value, three rules
# ---------------------------------------------------------------------------


def _ev(**fields) -> Evidence:
    base = dict(url="https://docs.example/setts", title="Badger setts",
                text="Badger setts are ventilated through their tunnels and entrances.",
                domain="docs.example", authority=40, fetched_at=_now() - timedelta(days=2))
    base.update(fields)
    return Evidence(**base)


def _topical_decision(monkeypatch, evidence) -> str:
    async def fake_retrieve(question, **kwargs):
        return Retrieval(query=question, freshness=Freshness.STATIC, evidence=list(evidence))

    monkeypatch.setattr(lk, "retrieve", fake_retrieve)
    out = lk.Prepared(verdict=_static_verdict())
    asyncio.run(lk._topical(QUESTION, out, effort="think"))
    return out.decision


def test_the_floor_the_rerank_pregate_and_the_topical_rule_are_one_value(monkeypatch):
    assert web_memory.TOPICAL_DENSE_FLOOR == web_memory._WEAK_DENSE == 0.35

    # The STATIC rerank pre-gate: at the floor the cross-encoder is asked,
    # just under it it is not.
    for dense, asked in ((FLOOR, True), (BELOW, False)):
        sent = []

        async def score(query, docs, _sent=sent, **kwargs):
            _sent.append(docs)
            return [0.1] * len(docs)

        monkeypatch.setattr(rerank, "score", score)
        asyncio.run(web_memory._answerability(
            QUESTION, [_ev(dense=dense, lexical=1.0, score=0.9)], level=Freshness.STATIC, effort="fast",
        ))
        assert bool(sent) is asked, dense

    # living_knowledge's hit rule, unscored (no cross-encoder verdict).
    assert _topical_decision(monkeypatch, [_ev(dense=FLOOR, lexical=0.9, score=0.9)]) == "static_topical"
    assert _topical_decision(monkeypatch, [_ev(dense=BELOW, lexical=0.9, score=0.9)]) == "static_model"


_VOCAB = (
    "badger setts ventilated tunnels entrances airflow burrow soil chamber colony nocturnal "
    "woodland mammal clan bedding spoil heap gallery ventilation den earth"
).split()


def _candidate_set(rng: random.Random, dense_max: float):
    q_terms = rng.sample(_VOCAB, rng.randint(2, 5))
    question = "how are " + " ".join(q_terms)
    cands = []
    for i in range(rng.randint(1, 16)):
        words = [rng.choice(_VOCAB + q_terms * rng.randint(0, 3)) for _ in range(rng.randint(5, 60))]
        dense = 0.0 if rng.random() < 0.3 else min(rng.random() * dense_max, dense_max)
        if dense_max < FLOOR:
            dense = min(dense, BELOW)
        age = timedelta(days=rng.uniform(0, 4000))
        cands.append(Evidence(
            url=f"https://p{i}.example/{rng.randint(0, 10**6)}",
            title=" ".join(rng.sample(_VOCAB + q_terms, rng.randint(0, 4))),
            text=" ".join(words) + f" marker{i}",
            domain=f"p{i}.example",
            authority=rng.choice([15, 40, 70, 80, 100]),
            fetched_at=_now() - age,
            published_at=(_now() - age - timedelta(days=rng.uniform(0, 400))) if rng.random() < 0.5 else None,
            dense=dense,
            origin=rng.choice(["search", "search", "crawl", "share"]),
        ))
    return question, cands


def test_a_candidate_set_whose_best_dense_is_under_the_floor_never_grounds_a_timeless_answer(monkeypatch):
    """The soundness of the exit, through the real rank, the real rerank
    pre-gate and living_knowledge's real hit rule. The cross-encoder stub says
    "answers" to everything it is shown, the most adversarial reranker there
    is, so the only thing keeping a set out is the dense floor."""
    asked = []

    async def score(query, docs, **kwargs):
        asked.append(len(docs))
        return [1.0] * len(docs)

    monkeypatch.setattr(rerank, "score", score)
    current = {}

    async def fake_retrieve(question, **kwargs):
        return current["result"]

    monkeypatch.setattr(lk, "retrieve", fake_retrieve)

    async def decide(n: int, dense_max: float, seed: int):
        rng = random.Random(seed)
        decisions = []
        for _ in range(n):
            question, cands = _candidate_set(rng, dense_max)
            ranked = web_memory._rank_candidates(question, cands, Freshness.STATIC)
            judged, _degraded = await web_memory._answerability(
                question, ranked, level=Freshness.STATIC, effort=rng.choice(["fast", "think", "max"]),
            )
            current["result"] = Retrieval(query=question, freshness=Freshness.STATIC, evidence=judged[:4])
            out = lk.Prepared(verdict=_static_verdict())
            await lk._topical(question, out, effort="think")
            decisions.append(out.decision)
        return decisions

    below = asyncio.run(decide(2000, BELOW, seed=20260929))
    assert len(below) == 2000
    assert set(below) == {"static_model"}
    assert asked == [], "the STATIC pre-gate let the cross-encoder judge a set under the floor"

    # The harness can see a hit when one is possible.
    control = asyncio.run(decide(300, 1.0, seed=20260930))
    assert control.count("static_topical") >= 30, control.count("static_topical")
    assert asked, "control: the cross-encoder ran for sets above the floor"


# ---------------------------------------------------------------------------
# The topical call sites, gated, over every web_eval case
# ---------------------------------------------------------------------------

from tests.test_prepass_rerank_equivalence import CASES, _h, _seed, _src  # noqa: E402


def _force_gate(monkeypatch, real):
    """What track A's call-site flip does: every STATIC retrieval
    living_knowledge makes (the speculative STATIC guess and both _topical
    paths) asks for the gate."""
    async def gated(question, **kwargs):
        if kwargs.get("level") is Freshness.STATIC:
            kwargs["topical_gate"] = True
        return await real(question, **kwargs)

    monkeypatch.setattr(lk, "retrieve", gated)


def _far(monkeypatch):
    """The same dense ranking, every distance moved past the floor."""
    inner = web_index.retrieve

    async def far(query, top_k=6, site_prefix=""):
        hits = await inner(query, top_k=top_k, site_prefix=site_prefix)
        return [dict(h, score=0.66 + 0.33 * (float(h["score"]) - 0.1) / 0.8) for h in hits]

    monkeypatch.setattr(web_index, "retrieve", far)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_every_web_eval_case_decides_and_cites_the_same_with_the_gate(case, monkeypatch):
    lk._page_vocabulary.reset()
    rerank.reset_for_tests()
    for name, value in dict(
        knowledge_evidence_cache_ttl_s=0.0, freshness_router_enabled=True, freshness_fast_skip_router=False,
        knowledge_fast_concurrent_retrieve=True,
    ).items():
        monkeypatch.setattr(settings, name, value, raising=False)
    monkeypatch.setattr(lk, "claims_for", lambda q, limit=3: [])

    async def router_static(question):
        await asyncio.sleep(0)
        return _static_verdict()

    monkeypatch.setattr(freshness, "_ask_router", router_static)
    sent = []
    _seed(case, monkeypatch, sent)
    real = web_memory.retrieve
    q = case["question"]

    def observe():
        record = {}
        for effort in ("fast", "think"):
            sent.clear()
            p = asyncio.run(lk.prepare(q, effort=effort, mode="assistant", web_search_pref="off", allow_network=False))
            record[effort] = {
                "verdict": [p.verdict.requirement.value, p.verdict.reason],
                "decision": p.decision,
                "sources": _src(p.sources or []),
                "grounding": _h(p.grounding),
                "rerank": [list(s) for s in sent],
            }
        return record

    observed = {}
    exits = _count("knowledge_topical_gate_total", outcome="exit")
    passes = _count("knowledge_topical_gate_total", outcome="pass")
    for regime in ("as_seeded", "far"):
        if regime == "far":
            _far(monkeypatch)
        for gate in (False, True):
            monkeypatch.setattr(lk, "retrieve", real)
            if gate:
                _force_gate(monkeypatch, real)
            web_memory.cache_clear()
            observed[(regime, gate)] = observe()
        assert observed[(regime, True)] == observed[(regime, False)], regime
    monkeypatch.setattr(lk, "retrieve", real)

    static = [r for r in observed[("as_seeded", False)].values() if r["verdict"][0] == "static"]
    if static:
        # The gate was consulted: it passed on the seeded corpus (at least one
        # dense hit near every question) and exited on the far one.
        assert _count("knowledge_topical_gate_total", outcome="pass") > passes
        assert _count("knowledge_topical_gate_total", outcome="exit") > exits
        assert all(r["decision"] == "static_model" for r in observed[("far", True)].values()
                   if r["verdict"][0] == "static")


def test_the_web_eval_premise_the_gated_cases_include_real_topical_hits(monkeypatch):
    """The per-case test above compares like with like; this checks that the
    comparison covered grounded answers, not only static_model ones."""
    lk._page_vocabulary.reset()
    rerank.reset_for_tests()
    for name, value in dict(
        knowledge_evidence_cache_ttl_s=0.0, freshness_router_enabled=True, freshness_fast_skip_router=False,
        knowledge_fast_concurrent_retrieve=True,
    ).items():
        monkeypatch.setattr(settings, name, value, raising=False)
    monkeypatch.setattr(lk, "claims_for", lambda q, limit=3: [])

    async def router_static(question):
        return _static_verdict()

    monkeypatch.setattr(freshness, "_ask_router", router_static)
    real = web_memory.retrieve
    decisions = {}
    for case in CASES:
        with db.connection() as con:
            con.execute("TRUNCATE web_pages RESTART IDENTITY CASCADE")
        lk._page_vocabulary.reset()
        web_memory.cache_clear()
        _seed(case, monkeypatch, [])
        _force_gate(monkeypatch, real)
        p = asyncio.run(lk.prepare(case["question"], effort="think", mode="assistant",
                                   web_search_pref="off", allow_network=False))
        decisions[case["id"]] = p.decision
    assert "static_topical" in decisions.values(), decisions
