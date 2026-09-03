"""The unified evidence pipeline (ADR-0001) — the rules the forensic audit
and the design critique of 2026-09-03 established, pinned.

Fixture pages only; the reranker and the embedding service are stubbed so
every assertion is about the RULES: what counts as relevant, what may retire
what, when the store is sufficient, what may be cached, what is recalled.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import db, llm, rerank, web_index, web_memory
from app.config import settings
from app.freshness import Freshness, classify_offline
from app.web_memory import Evidence, Retrieval, _partition, retrieve


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc)


def _ev(url, text, *, title="", fetched_days=1.0, published_days=None, authority=40,
        lexical=1.0, dense=0.5, answer=-1.0, origin="search"):
    return Evidence(
        url=url, title=title or url, text=text, domain=url.split("/")[2], authority=authority,
        fetched_at=_now() - timedelta(days=fetched_days),
        published_at=(_now() - timedelta(days=published_days)) if published_days is not None else None,
        lexical=lexical, dense=dense, answer=answer, origin=origin,
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with db.connection() as con:
        con.execute("TRUNCATE web_pages RESTART IDENTITY CASCADE")
    web_memory.cache_clear()
    llm.embed_cache_clear()
    rerank.reset_for_tests()
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    yield
    rerank.reset_for_tests()  # a tripped breaker must not leak into other files


# ---------------------------------------------------------------------------
# D3: lexical candidates — the words PostgreSQL stems, and what ranks first
# ---------------------------------------------------------------------------


def _page(key, url, title, text, *, published=None, origin="search", quarantined=False):
    when = _now() - timedelta(days=1)
    with db.connection() as con:
        con.execute(
            """INSERT INTO web_pages
                 (url_key, url, title, text, fetched_at, first_seen_at, last_changed_at,
                  domain, authority, indexed_at, content_hash, published_at, origin, quarantined_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 40, %s, %s, %s, %s, %s)""",
            (key, url, title, text, when, when, when, url.split("/")[2], when, key,
             published, origin, when if quarantined else None),
        )


def test_lexical_query_round_trips_through_postgresql_stemming():
    """A word the Python stemmer would mangle ('business' → 'busines') must
    still match: the query is built from RAW content words and PostgreSQL
    stems once (design critique blocker)."""
    _page("a", "https://a.example/p", "Acme business status", "Acme reports its business status and campus address.")
    rows = web_memory._lexical_candidates("what is the business status of acme", 10)
    assert [r["url"] for r in rows] == ["https://a.example/p"]
    # Raw words go to PostgreSQL; the Python stem ('busines') stays Python-side.
    assert web_memory._content_words("the business status of acme") == ["business", "status", "acme"]
    assert web_memory._terms("business") == ["busines"]


def test_and_ranks_the_page_carrying_every_term_first_and_caps_a_domain():
    for i in range(6):
        _page(f"boiler{i}", f"https://big.example/p{i}", "Big Solutions page", "Big Solutions footer. " * 40)
    _page("ans", "https://org.example/team", "Acme Solutions team", "Jane Roe, Chief Executive Officer of Acme Solutions.")
    rows = web_memory._lexical_candidates("chief executive officer of acme solutions", 10)
    assert rows[0]["url"] == "https://org.example/team"
    assert sum(1 for r in rows if r["domain"] == "big.example") <= 3


def test_quarantined_pages_never_leave_the_store():
    _page("q", "https://q.example/p", "Acme quarantined", "Acme secret leadership page.", quarantined=True)
    assert web_memory._lexical_candidates("acme leadership", 10) == []


def test_best_window_finds_the_answer_deep_in_a_page():
    filler = "Unrelated introduction about the weather. " * 200  # ~8,600 chars
    text = filler + "Jane Roe is the Chief Executive Officer of Acme." + " More filler. " * 50
    window = web_memory._best_window(text, "who is the chief executive officer of acme")
    assert "Jane Roe" in window
    assert len(window) <= web_memory._WINDOW_CHARS


# ---------------------------------------------------------------------------
# D5: supersession — who may retire whom
# ---------------------------------------------------------------------------


def test_an_undated_page_never_retires_a_dated_answering_page():
    """The forensic case: the org chart (published 80 days ago, answers)
    lost to an undated company profile read yesterday (does not answer)."""
    answering = _ev("https://org.example/chart", "Jane Roe — Founder and CEO", published_days=80, answer=0.99)
    profile = _ev("https://social.example/company", "A technology company in Frisco.", answer=0.02)
    kept, superseded, _ = _partition([profile, answering], Freshness.RECENT)
    assert answering in kept and not superseded


def test_two_undated_answering_copies_still_retire_the_old_one():
    """The case the layer was built for: a copy read 200 days ago naming the
    previous holder, a copy read today naming the new one — both answer."""
    old = _ev("https://a.example/old", "The office holder is A.", fetched_days=200, answer=0.9)
    new = _ev("https://b.example/new", "The office holder is B.", fetched_days=1, answer=0.9)
    kept, superseded, _ = _partition([new, old], Freshness.RECENT)
    assert kept == [new] and superseded == [old]


def test_a_non_answering_page_cannot_retire_anything():
    old = _ev("https://a.example/old", "The office holder is A.", fetched_days=200, answer=0.9)
    new = _ev("https://b.example/about", "About us: we are a company.", fetched_days=1, answer=0.01)
    kept, superseded, _ = _partition([new, old], Freshness.RECENT)
    assert old in kept and not superseded


def test_a_member_shared_page_cannot_retire_anything():
    old = _ev("https://a.example/old", "The office holder is A.", fetched_days=200, published_days=200, answer=0.9)
    planted = _ev("https://sites.example/planted", "The office holder is Z.", published_days=0, answer=0.95,
                  authority=70, origin="share")
    kept, superseded, _ = _partition([planted, old], Freshness.RECENT)
    assert old in kept and not superseded


class _V:
    def __init__(self, requirement, reason, volatile=False):
        self.requirement, self.reason, self.volatile = requirement, reason, volatile


def test_supersession_runs_only_for_replaceable_facts():
    """Measured on the eval set: with supersession on for every RECENT
    question, 13 of 60 gold pages (each judged answering at 1.0) were retired
    by newer pages that merely related. It runs for office holders, live
    values, explicit current/latest asks and router-confirmed questions —
    not for the ambiguous default."""
    old = _ev("https://a.example/old", "The holder is A.", fetched_days=200, published_days=200, answer=0.95)
    new = _ev("https://b.example/new", "The holder is B.", fetched_days=1, published_days=1, answer=0.95)
    assert web_memory.supersession_allowed(Freshness.RECENT, _V(Freshness.RECENT, "lexical:office"))
    assert web_memory.supersession_allowed(Freshness.RECENT, _V(Freshness.RECENT, "default", volatile=True))
    assert not web_memory.supersession_allowed(Freshness.RECENT, _V(Freshness.RECENT, "default"))
    assert not web_memory.supersession_allowed(Freshness.STATIC, _V(Freshness.STATIC, "lexical:static"))
    kept, superseded, _ = _partition([new, old], Freshness.RECENT, verdict=_V(Freshness.RECENT, "default"))
    assert old in kept and not superseded
    kept, superseded, _ = _partition([new, old], Freshness.RECENT, verdict=_V(Freshness.RECENT, "lexical:office"))
    assert kept == [new] and superseded == [old]


def test_a_merely_relevant_newer_page_cannot_retire_a_strongly_answering_one():
    old = _ev("https://a.example/old", "Jane Roe is the holder.", fetched_days=200, published_days=200, answer=1.0)
    related = _ev("https://b.example/about", "About the organisation and its offices.", fetched_days=1, published_days=1, answer=0.4)
    kept, superseded, _ = _partition([related, old], Freshness.RECENT, verdict=_V(Freshness.RECENT, "lexical:office"))
    assert old in kept and not superseded


def test_sibling_pages_that_differ_in_the_asked_term_are_not_collapsed():
    """Release notes for 3.14.4 and 3.14.5 share almost every sentence; a
    question about 3.14.4 must keep the 3.14.4 page."""
    # Forty DISTINCT sentences: a repeated sentence would fingerprint to a
    # handful of shingles and the differing tail would dominate them.
    # No digits in the body: the question's version terms ("3", "14", "4")
    # must occur ONLY in the tails, or both pages carry every term.
    body = " ".join(
        f"Section {a}{b} describes a change to the interpreter, the standard library module {a}{b}lib and its tests."
        for a in "abcde"
        for b in "fghijklm"
    )
    a = _ev("https://py.example/3.14.4", body + " Version 3.14.4 was released on 1 June.")
    b = _ev("https://py.example/3.14.5", body + " Version 3.14.5 was released on 1 July.")
    a.score, b.score = 0.5, 0.6
    kept = web_memory._collapse_duplicates([b, a], "what is the release date of python 3.14.4")
    assert a in kept and b in kept
    # With no distinguishing question term they ARE one item.
    assert len(web_memory._collapse_duplicates([b, a], "python release notes")) == 1


def test_the_judged_set_includes_the_top_of_each_half(monkeypatch):
    """Recency weighting pushed a ten-year-old page that dense retrieval
    ranked first below the reranker cut; the top of each half is always
    judged."""
    monkeypatch.setattr(settings, "knowledge_rerank_candidates", 3)
    ranked = [_ev(f"https://x.example/{i}", f"page {i}", dense=0.1, lexical=0.1) for i in range(6)]
    for i, e in enumerate(ranked):
        e.score = 0.9 - i * 0.1
    ranked.append(_ev("https://old.example/gold", "gold", dense=0.95, lexical=0.05))
    ranked[-1].score = 0.05
    judged = {}

    async def fake(query, docs, **kw):
        judged["n"] = len(docs)
        return [0.99 if "gold" in d else 0.01 for d in docs]

    monkeypatch.setattr(rerank, "score", fake)
    out, degraded = run(web_memory._answerability("q", ranked, level=Freshness.RECENT, effort="fast"))
    assert not degraded and judged["n"] >= 4
    assert out[0].url == "https://old.example/gold"


def test_year_only_dates_count_as_undated():
    jan1 = _now().replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    e = Evidence(url="https://x.example/p", title="x", text="x", domain="x.example", authority=40,
                 fetched_at=_now(), published_at=jan1)
    assert web_memory.date_precision(jan1) == "year"
    assert not e.dated


# ---------------------------------------------------------------------------
# D4: sufficiency — answerability decides, staleness is separate
# ---------------------------------------------------------------------------


def test_entity_overlap_alone_is_not_sufficient_once_judged():
    r = Retrieval(query="q", freshness=Freshness.RECENT)
    r.evidence = [_ev("https://a.example/1", "Acme profile", lexical=0.67, dense=0.48, answer=0.03),
                  _ev("https://b.example/2", "Acme register", lexical=0.67, dense=0.27, answer=0.05)]
    r.newest_age = 3600
    assert not r.sufficient(14 * 86400)


def test_one_answering_passage_is_sufficient_and_copy_age_is_what_counts():
    r = Retrieval(query="q", freshness=Freshness.RECENT)
    r.evidence = [_ev("https://a.example/1", "Jane Roe — CEO", published_days=80, fetched_days=1, answer=0.98)]
    r.newest_age = 86400
    assert r.sufficient(14 * 86400)  # published 80 d ago, re-read yesterday: fine


def test_a_stale_answering_passage_is_not_sufficient(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_stale_after_recent_s", 120 * 86400)
    r = Retrieval(query="q", freshness=Freshness.RECENT)
    r.evidence = [_ev("https://a.example/1", "Jane Roe was appointed CEO in 2023", published_days=400, answer=0.98)]
    r.newest_age = 86400
    assert r.stale_answer and not r.sufficient(14 * 86400)


def test_unjudged_evidence_needs_the_strict_gate_when_the_judge_was_busy():
    """A busy/broken reranker must not silently reinstate the entity-overlap
    rule the audit condemned; a deployment with NO reranker keeps it."""
    r = Retrieval(query="q", freshness=Freshness.RECENT)
    weak = _ev("https://a.example/1", "Acme profile", lexical=0.67, dense=0.2)
    weak.score = 0.64
    r.evidence = [weak]
    r.newest_age = 3600
    r.degraded = "rerank_busy"
    assert not r.sufficient(14 * 86400)
    strong = _ev("https://a.example/2", "Acme leadership", lexical=0.9, dense=0.6)
    strong.score = 0.7
    r.evidence = [strong]
    assert r.sufficient(14 * 86400)
    # No reranker configured at all: the pre-ADR hybrid rule stands.
    r.degraded = "rerank_disabled"
    r.evidence = [weak]
    assert r.sufficient(14 * 86400)


# ---------------------------------------------------------------------------
# D6: volatile verdicts
# ---------------------------------------------------------------------------


def test_volatile_questions_get_a_day_not_two_weeks():
    v = classify_offline("what is the latest vllm release", now_year=_now().year)
    assert v.volatile and v.max_age_seconds <= 24 * 3600
    assert not classify_offline("who is the ceo of acme", now_year=_now().year).volatile


# ---------------------------------------------------------------------------
# D10: the evidence cache
# ---------------------------------------------------------------------------


def test_cache_is_invalidated_by_a_corpus_write_and_refuses_private_evidence(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_evidence_cache_ttl_s", 60.0)
    key = web_memory._cache_key("q", Freshness.RECENT, 5)
    r = Retrieval(query="q", freshness=Freshness.RECENT)
    r.evidence = [_ev("https://a.example/1", "x")]
    web_memory._cache_put(key, r)
    assert web_memory._cache_get(key) is not None
    db.bump_web_corpus_generation()
    assert web_memory._cache_key("q", Freshness.RECENT, 5) != key  # a new key: the old entry is unreachable
    private = Retrieval(query="p", freshness=Freshness.RECENT)
    private.evidence = [Evidence(url="u", title="t", text="x", domain="d", authority=1, fetched_at=None, scope="user")]
    k2 = web_memory._cache_key("p", Freshness.RECENT, 5)
    web_memory._cache_put(k2, private)
    assert web_memory._cache_get(k2) is None


def test_cached_items_are_copies(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_evidence_cache_ttl_s", 60.0)
    key = web_memory._cache_key("q", Freshness.RECENT, 5)
    r = Retrieval(query="q", freshness=Freshness.RECENT)
    r.evidence = [_ev("https://a.example/1", "original text")]
    web_memory._cache_put(key, r)
    first = web_memory._cache_get(key)
    first.evidence[0].text = "mutated"
    assert web_memory._cache_get(key).evidence[0].text == "original text"


# ---------------------------------------------------------------------------
# The whole retrieve(): judged, merged by page, degraded when the judge is out
# ---------------------------------------------------------------------------


def test_retrieve_ranks_the_answering_page_first_and_reports_degradation(monkeypatch):
    _page("chart", "https://org.example/chart", "Acme Solutions | The Org",
          "Acme Solutions. Jane Roe, Founder and CEO. Vidhi P, HR lead.", published=_now() - timedelta(days=80))
    _page("prof", "https://social.example/acme", "Acme Solutions | Social",
          "Acme Solutions is a technology company. We see this every day at Acme Solutions.")

    async def no_dense(*a, **k):
        return []

    monkeypatch.setattr(web_index, "retrieve", no_dense)
    monkeypatch.setattr(settings, "knowledge_rerank", True)
    monkeypatch.setattr(settings, "knowledge_evidence_cache_ttl_s", 0.0)

    async def judge(query, docs, **kw):
        return [0.99 if "Founder and CEO" in d else 0.02 for d in docs]

    monkeypatch.setattr(rerank, "score", judge)
    r = run(retrieve("who is the ceo of acme solutions", level=Freshness.RECENT, top_k=5))
    assert r.evidence[0].url == "https://org.example/chart"
    assert r.evidence[0].answer > 0.9 and r.sufficient(14 * 86400) and not r.degraded

    async def busy(query, docs, **kw):
        raise rerank.RerankUnavailable("reranker busy")

    monkeypatch.setattr(rerank, "score", busy)
    r2 = run(retrieve("who is the ceo of acme solutions", level=Freshness.RECENT, top_k=5))
    assert r2.degraded == "rerank_busy"
    assert not r2.sufficient(14 * 86400)  # unjudged + weak: not enough


# ---------------------------------------------------------------------------
# D4 guards on the reranker client
# ---------------------------------------------------------------------------


def test_degenerate_scores_are_refused():
    assert rerank.degenerate([0.5] * 6)
    assert not rerank.degenerate([0.5, 0.51, 0.9, 0.1, 0.5, 0.5])
    assert not rerank.degenerate([0.5, 0.5])


def test_canary_trips_the_breaker_on_inverted_scores(monkeypatch):
    class _Caps:
        enabled = True
        supports_reranking = True
        requires_authentication = False

    monkeypatch.setattr(settings, "rerank_enabled", True)
    monkeypatch.setattr(settings, "rerank_base_url", "http://reranker.test")
    monkeypatch.setattr(settings, "reranker_capabilities", _Caps())
    monkeypatch.setattr(settings, "rerank_canary_enabled", True)

    async def inverted(query, docs, instruction, timeout):
        return [0.01, 0.99]

    monkeypatch.setattr(rerank, "_post", inverted)
    assert run(rerank.canary(force=True)) is False
    assert not rerank.enabled() and "canary" in rerank.breaker_reason()
    with pytest.raises(rerank.RerankUnavailable):
        run(rerank.score("q", ["a", "b"]))

    async def correct(query, docs, instruction, timeout):
        return [0.99, 0.01]

    monkeypatch.setattr(rerank, "_post", correct)
    assert run(rerank.canary(force=True)) is True
    assert rerank.enabled()


# ---------------------------------------------------------------------------
# D13: query embeddings are cached and bounded
# ---------------------------------------------------------------------------


def test_embed_query_caches_and_reports_busy(monkeypatch):
    calls = []

    async def fake_embed(texts, **kw):
        calls.append(list(texts))
        return [[0.1, 0.2]]

    monkeypatch.setattr(llm, "embed_texts", fake_embed)
    monkeypatch.setattr(settings, "embed_max_inflight", 1)
    monkeypatch.setattr(settings, "embed_wait_s", 0.05)

    async def scenario():
        a = await llm.embed_query("who is x", instruction=llm.QUERY_INSTRUCTION)
        b = await llm.embed_query("who is  x ", instruction=llm.QUERY_INSTRUCTION)  # whitespace-normalised hit
        return a, b

    a, b = run(scenario())
    assert a == b == [0.1, 0.2]
    assert len(calls) == 1 and calls[0][0].startswith("Instruct:")

    async def slow(texts, **kw):
        await asyncio.sleep(0.3)
        return [[0.0, 0.0]]

    monkeypatch.setattr(llm, "embed_texts", slow)

    async def busy_scenario():
        first = asyncio.create_task(llm.embed_query("fresh one"))
        await asyncio.sleep(0.01)
        with pytest.raises(llm.EmbedUnavailable):
            await llm.embed_query("fresh two")
        await first

    run(busy_scenario())


# ---------------------------------------------------------------------------
# D9: recall of the assistant's own answers
# ---------------------------------------------------------------------------


def test_recall_excludes_assistant_answers_when_asked(monkeypatch):
    from app import memory_semantic

    async def sem(user_id, query, exclude, limit=3):
        return [
            {"title": "hi", "role": "assistant", "snippet": "The CEO is X.", "conversation_id": "c1", "score": 0.9},
            {"title": "hi", "role": "user", "snippet": "our office is in Frisco", "conversation_id": "c1", "score": 0.8},
        ]

    monkeypatch.setattr(memory_semantic, "semantic_hits", sem)
    monkeypatch.setattr(memory_semantic, "_backfill_in_background", lambda uid: None)
    monkeypatch.setattr(memory_semantic, "keywords", lambda q: [])
    with_assistant = run(memory_semantic.cross_chat_block(1, "who is the ceo", None, include_assistant=True))
    without = run(memory_semantic.cross_chat_block(1, "who is the ceo", None, include_assistant=False))
    assert "The CEO is X." in with_assistant
    assert "The CEO is X." not in without and "Frisco" in without
