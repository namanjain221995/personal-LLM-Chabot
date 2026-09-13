"""Fast effort's knowledge pre-pass budget (performance plan item 2, 2026-09-13).

What the plan measured before this change: Fast/chat time to first token p50
1.46 s against an engine TTFT of 0.05-0.2 s. The static-question retrieval
cost p50 0.72 s / p90 2.09 s and found nothing on 92% of timeless turns; the
freshness router added a mean 0.216 s on 72% of turns, BEFORE retrieval began.

Pinned here, each by a test that fails without its change:
  - (opt-in since 2026-09-13, default off) a slow topical retrieval cannot hold
    a Fast answer past the short deadline while the pre-check has not cleared
    the question, and the miss is recorded as degraded;
  - once the pre-check says a page CAN pass the gate, the wait runs on to the
    longer hit budget, so a real topical hit slower than the short deadline
    still grounds the answer (revised 2026-09-13: a flat 0.3 s deadline took
    prepare_fast answer@5 from 0.900 to 0.300 at a 0.6 s retrieval);
  - a pre-check that cannot tell, fails, or is switched off falls back to the
    pre-change wait, not to the short deadline;
  - a corpus with no page that could pass the gate is not waited on at all;
  - a Fast timeless TASK never asks the router; a live-value question with no
    recency word ("euro to dollar", "is AWS down") still does, and still gets
    the Fast live lookup (revised 2026-09-13: the first skip made it 0 of 4);
  - Think and Max keep today's order and wait for grounding however slow.

Time is driven with small real deadlines (tens of milliseconds) and events,
never with long sleeps.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import db, freshness, metrics, web_index, web_memory
from app import living_knowledge as lk
from app.config import settings
from app.freshness import Freshness, Verdict
from app.web_memory import Retrieval


def run(coro):
    return asyncio.run(coro)


#: Module default -> the Settings attribute that, once config.py defines it,
#: wins over that default (living_knowledge.fast_topical_deadline_s and
#: friends). A test must set BOTH, or a Settings attribute silently overrides
#: what the test thinks it pinned — which is how an env flip could not reach
#: these tests before 2026-09-13.
_SETTING_FOR = {
    "_FAST_TOPICAL_DEADLINE_S": "knowledge_fast_topical_deadline_s",
    "_FAST_TOPICAL_HIT_BUDGET_S": "knowledge_fast_topical_hit_budget_s",
    "_FAST_TOPICAL_PRECHECK": "knowledge_fast_topical_precheck",
    "_FAST_SKIP_ROUTER": "freshness_fast_skip_router",
    "_FAST_CONCURRENT_RETRIEVE": "knowledge_fast_concurrent_retrieve",
}


def _tune(monkeypatch, const, value):
    monkeypatch.setattr(lk, const, value)
    monkeypatch.setattr(settings, _SETTING_FOR[const], value, raising=False)


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
    # The defaults this change ships, set explicitly so an operator's
    # environment cannot change what these tests prove. Both wall-clock
    # bounds are OFF by default since the second prover pass (2026-09-13);
    # the tests of the opt-in bounds set their own values.
    _tune(monkeypatch, "_FAST_TOPICAL_DEADLINE_S", 0.0)
    _tune(monkeypatch, "_FAST_TOPICAL_HIT_BUDGET_S", 0.0)
    _tune(monkeypatch, "_FAST_TOPICAL_PRECHECK", True)
    _tune(monkeypatch, "_FAST_SKIP_ROUTER", True)
    _tune(monkeypatch, "_FAST_CONCURRENT_RETRIEVE", True)
    monkeypatch.setattr(lk, "claims_for", lambda q, limit=3: [])
    yield


def _page(key, url, title, text, *, age_days=30.0, authority=web_memory.AUTHORITY_REFERENCE):
    when = datetime.now(timezone.utc) - timedelta(days=age_days)
    with db.connection() as con:
        con.execute(
            """INSERT INTO web_pages
                 (url_key, url, title, text, fetched_at, first_seen_at,
                  last_changed_at, domain, authority, indexed_at, content_hash)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (key, url, title, text, when, when, when, url.split("/")[2], authority, when, key),
        )


DOCS_URL = "https://docs.example.org/guide/photosynthesis-simulator-configuration"
DOCS_TITLE = "Photosynthesis simulator configuration"
DOCS_TEXT = (
    "To configure the photosynthesis simulator set PHOTO_RATE in config.yaml. "
    "The photosynthesis simulator reads config.yaml at startup."
)
STRONG = "explain how the photosynthesis simulator is configured"


def _seed_docs(monkeypatch, *, dense_delay: float = 0.0):
    """The strong-match page of test_living_knowledge, dense half stubbed;
    `dense_delay` makes the whole retrieval that much slower."""

    async def dense_hit(query, top_k=6, site_prefix=""):
        if dense_delay:
            await asyncio.sleep(dense_delay)
        return [{"url": DOCS_URL, "title": DOCS_TITLE, "text": DOCS_TEXT, "fetched_at": "", "score": 0.2}]

    monkeypatch.setattr(web_index, "retrieve", dense_hit)
    _page("docs/config", DOCS_URL, DOCS_TITLE, DOCS_TEXT)


def _router(monkeypatch, answer=Freshness.RECENT, *, gate: asyncio.Event | None = None, calls=None):
    """A router stub that counts its calls; optionally waits on `gate` so a
    test can prove what had started before it answered."""

    async def ask(question):
        if calls is not None:
            calls.append(question)
        await asyncio.sleep(0)  # a real router round trip yields to the loop
        if gate is not None:
            await asyncio.wait_for(gate.wait(), timeout=0.5)
        return Verdict(answer, freshness._MAX_AGE[answer], "router")

    monkeypatch.setattr(freshness, "_ask_router", ask)


def _prepare(question, effort="fast", **kw):
    call = dict(effort=effort, mode="assistant", web_search_pref="off", allow_network=False)
    call.update(kw)
    return lk.prepare(question, **call)


def _degraded_count(reason):
    return metrics._counters.get("knowledge_degraded_total", {}).get((("reason", reason),), 0.0)


# ── the deadline ─────────────────────────────────────────────────────────────


def _unanswered_precheck(monkeypatch):
    """A pre-check stuck in its thread (a slow database) until the test ends."""
    import threading

    release = threading.Event()

    def stuck(q):
        release.wait(timeout=2.0)
        return True

    monkeypatch.setattr(lk, "_topical_precheck", stuck)
    return release


def test_a_fast_timeless_question_answers_within_the_deadline_when_retrieval_is_slow(monkeypatch):
    _tune(monkeypatch, "_FAST_TOPICAL_DEADLINE_S", 0.05)
    # The pre-check has not answered, so only the short deadline can end the wait.
    release = _unanswered_precheck(monkeypatch)
    state = {"cancelled": False}

    async def never(q, **kw):
        try:
            # Finite, so a build without the deadline fails instead of hanging.
            await asyncio.sleep(3.0)
            return Retrieval(query=q, freshness=kw["level"])
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    monkeypatch.setattr(lk, "retrieve", never)
    before = _degraded_count(lk.TOPICAL_DEADLINE)

    started = time.perf_counter()
    try:
        prepared = run(_prepare("What is photosynthesis?"))
    finally:
        release.set()
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5, f"Fast waited {elapsed:.3f} s on a 0.05 s deadline"
    assert prepared.verdict.requirement is Freshness.STATIC
    assert prepared.decision == "static_model"
    assert prepared.degraded == lk.TOPICAL_DEADLINE
    assert prepared.grounding == "" and prepared.retrieval is None
    assert state["cancelled"], "the abandoned retrieval must not keep running"
    assert _degraded_count(lk.TOPICAL_DEADLINE) == before + 1


def test_a_topical_hit_within_the_deadline_is_still_used(monkeypatch):
    _seed_docs(monkeypatch)
    prepared = run(_prepare(STRONG))
    assert prepared.decision == "static_topical"
    assert "config.yaml" in prepared.grounding
    assert prepared.sources and "docs.example.org" in prepared.sources[0]["url"]
    assert prepared.degraded == ""


def test_a_hit_that_beats_the_deadline_is_used_even_when_the_precheck_is_slower(monkeypatch):
    """The pre-check runs BESIDE the retrieval: it may never hold a hit back."""
    _seed_docs(monkeypatch)
    real = lk._topical_precheck

    def slow_precheck(q):
        time.sleep(0.2)
        return real(q)

    monkeypatch.setattr(lk, "_topical_precheck", slow_precheck)
    started = time.perf_counter()
    prepared = run(_prepare(STRONG))
    assert prepared.decision == "static_topical"
    assert time.perf_counter() - started < 0.2 + 0.25


def test_a_topical_hit_slower_than_the_short_deadline_is_kept_once_the_precheck_clears_it(monkeypatch):
    """The prover's blocker: a real hit arriving after 0.3 s was dropped. The
    pre-check finds the page at once, so the wait runs on to the hit budget."""
    _tune(monkeypatch, "_FAST_TOPICAL_DEADLINE_S", 0.05)
    _tune(monkeypatch, "_FAST_TOPICAL_HIT_BUDGET_S", 2.0)
    _seed_docs(monkeypatch, dense_delay=0.3)
    metrics.reset()
    started = time.perf_counter()
    prepared = run(_prepare(STRONG))
    elapsed = time.perf_counter() - started
    assert prepared.decision == "static_topical", prepared.degraded
    assert "config.yaml" in prepared.grounding
    assert prepared.degraded == ""
    assert 0.3 <= elapsed < 2.0
    assert metrics._counters["knowledge_topical_precheck_total"] == {(("result", "hit"),): 1.0}


def test_a_cleared_question_is_still_bounded_by_the_hit_budget(monkeypatch):
    _tune(monkeypatch, "_FAST_TOPICAL_DEADLINE_S", 0.02)
    _tune(monkeypatch, "_FAST_TOPICAL_HIT_BUDGET_S", 0.15)
    monkeypatch.setattr(lk, "_topical_precheck", lambda q: True)

    async def never(q, **kw):
        # Finite, so a build without the hit budget fails instead of hanging.
        await asyncio.sleep(3.0)
        return Retrieval(query=q, freshness=kw["level"])

    monkeypatch.setattr(lk, "retrieve", never)
    before = _degraded_count(lk.TOPICAL_HIT_BUDGET)
    started = time.perf_counter()
    prepared = run(_prepare("What is photosynthesis?"))
    elapsed = time.perf_counter() - started
    assert 0.15 <= elapsed < 0.6, elapsed
    assert prepared.decision == "static_model"
    assert prepared.degraded == lk.TOPICAL_HIT_BUDGET
    assert _degraded_count(lk.TOPICAL_HIT_BUDGET) == before + 1


def test_a_hit_budget_shorter_than_the_short_deadline_never_cuts_a_cleared_question_shorter(monkeypatch):
    _tune(monkeypatch, "_FAST_TOPICAL_DEADLINE_S", 0.2)
    _tune(monkeypatch, "_FAST_TOPICAL_HIT_BUDGET_S", 0.01)
    monkeypatch.setattr(lk, "_topical_precheck", lambda q: True)

    async def slow(q, **kw):
        await asyncio.sleep(0.1)
        return Retrieval(query=q, freshness=kw["level"])

    monkeypatch.setattr(lk, "retrieve", slow)
    prepared = run(_prepare("What is photosynthesis?"))
    assert prepared.retrieval is not None and prepared.degraded == ""


@pytest.mark.parametrize("precheck", ["cannot_tell", "raises"])
def test_a_precheck_that_cannot_answer_falls_back_to_the_pre_change_wait(monkeypatch, precheck):
    """Not to the short deadline: a failed pre-check says nothing about a hit."""
    _tune(monkeypatch, "_FAST_TOPICAL_DEADLINE_S", 0.05)

    def broken(q):
        if precheck == "raises":
            raise RuntimeError("pool exhausted")
        return None

    monkeypatch.setattr(lk, "_topical_precheck", broken)

    async def slow(q, **kw):
        await asyncio.sleep(0.25)
        return Retrieval(query=q, freshness=kw["level"])

    monkeypatch.setattr(lk, "retrieve", slow)
    metrics.reset()
    prepared = run(_prepare("What is photosynthesis?"))
    assert prepared.retrieval is not None, "the retrieval must be waited for"
    assert prepared.degraded == ""
    assert metrics._counters["knowledge_topical_precheck_total"] == {(("result", "fail"),): 1.0}


def test_with_the_precheck_switched_off_a_slow_topical_hit_is_still_waited_for(monkeypatch):
    _tune(monkeypatch, "_FAST_TOPICAL_PRECHECK", False)
    _tune(monkeypatch, "_FAST_TOPICAL_DEADLINE_S", 0.05)
    _seed_docs(monkeypatch, dense_delay=0.25)
    prepared = run(_prepare(STRONG))
    assert prepared.decision == "static_topical"
    assert prepared.degraded == ""


def test_the_hit_budget_tunable_parses_like_config_py_and_defers_to_settings(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_FAST_TOPICAL_HIT_BUDGET_S", "2.5")
    assert lk._env_float("KNOWLEDGE_FAST_TOPICAL_HIT_BUDGET_S", 1.5) == 2.5
    assert lk.fast_topical_hit_budget_s() == 0.0  # the fixture's pinned default
    monkeypatch.setattr(settings, "knowledge_fast_topical_hit_budget_s", 0.9, raising=False)
    assert lk.fast_topical_hit_budget_s() == 0.9


def test_a_deadline_of_zero_also_lifts_the_hit_budget_so_the_rollback_is_the_whole_pre_change_wait(monkeypatch):
    _tune(monkeypatch, "_FAST_TOPICAL_DEADLINE_S", 0.0)
    _tune(monkeypatch, "_FAST_TOPICAL_HIT_BUDGET_S", 0.02)
    monkeypatch.setattr(lk, "_topical_precheck", lambda q: True)

    async def slow(q, **kw):
        await asyncio.sleep(0.15)
        return Retrieval(query=q, freshness=kw["level"])

    monkeypatch.setattr(lk, "retrieve", slow)
    prepared = run(_prepare("What is photosynthesis?"))
    assert prepared.retrieval is not None and prepared.degraded == ""


def test_a_deadline_of_zero_restores_the_unbounded_wait(monkeypatch):
    _tune(monkeypatch, "_FAST_TOPICAL_DEADLINE_S", 0.0)
    monkeypatch.setattr(lk, "_topical_precheck", lambda q: None)

    async def slow(q, **kw):
        await asyncio.sleep(0.08)
        return Retrieval(query=q, freshness=kw["level"])

    monkeypatch.setattr(lk, "retrieve", slow)
    prepared = run(_prepare("What is photosynthesis?"))
    assert prepared.retrieval is not None and prepared.degraded == ""


def test_a_settings_attribute_of_the_same_name_wins_over_the_module_default(monkeypatch):
    """So the integration lead can move the tunable into config.py unchanged."""
    monkeypatch.setattr(settings, "knowledge_fast_topical_deadline_s", 1.25, raising=False)
    assert lk.fast_topical_deadline_s() == 1.25


def test_the_tunables_parse_like_config_py(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_FAST_TOPICAL_DEADLINE_S", " ")
    assert lk._env_float("KNOWLEDGE_FAST_TOPICAL_DEADLINE_S", 0.3) == 0.3
    monkeypatch.setenv("KNOWLEDGE_FAST_TOPICAL_DEADLINE_S", "0.25")
    assert lk._env_float("KNOWLEDGE_FAST_TOPICAL_DEADLINE_S", 0.3) == 0.25
    monkeypatch.setenv("FRESHNESS_FAST_SKIP_ROUTER", "off")
    assert lk._env_bool("FRESHNESS_FAST_SKIP_ROUTER", True) is False
    monkeypatch.setenv("FRESHNESS_FAST_SKIP_ROUTER", "")
    assert lk._env_bool("FRESHNESS_FAST_SKIP_ROUTER", True) is True


# ── the pre-check ────────────────────────────────────────────────────────────


def test_a_corpus_no_page_of_which_can_pass_the_gate_is_not_waited_on(monkeypatch):
    _tune(monkeypatch, "_FAST_TOPICAL_DEADLINE_S", 5.0)
    _page("other", "https://cooking.example/rice", "Cooking rice", "Rinse the rice and simmer it for twelve minutes.")

    async def never(q, **kw):
        await asyncio.Event().wait()

    monkeypatch.setattr(lk, "retrieve", never)
    started = time.perf_counter()
    prepared = run(_prepare("what is the boiling point of water?"))
    assert time.perf_counter() - started < 1.0
    assert prepared.decision == "static_model"
    assert prepared.degraded == "", "a proven miss is not a degraded answer"


def test_the_precheck_needs_the_share_of_terms_the_lexical_floor_needs():
    _page("docs/config", DOCS_URL, DOCS_TITLE, DOCS_TEXT)
    # photosynthesis, simulator, configured: 3 terms, 0.34 * 3 -> 2 must be on a page.
    assert lk._topical_precheck(STRONG) is True
    # One of three terms on the page ("simulator") is not enough.
    assert lk._topical_precheck("simulator boiling water") is False
    assert lk._topical_precheck("what is the boiling point of water?") is False


def test_a_question_with_no_content_words_cannot_be_topical():
    assert lk._topical_precheck("what is it?") is False


def test_the_precheck_floor_matches_both_topical_gates():
    """The pre-check is sound only while it asks for the same lexical floor as
    `_topical_hit` and `_answerability`'s STATIC pre-gate."""
    assert lk._TOPICAL_LEXICAL_FLOOR == 0.34
    assert "e.lexical >= 0.34" in inspect.getsource(web_memory._answerability)
    assert "e.lexical >= _TOPICAL_LEXICAL_FLOOR" in inspect.getsource(lk._topical)


def test_the_precheck_never_rejects_a_question_the_full_topical_gate_accepts(monkeypatch):
    """Soundness on the web_eval fixtures: for every question the unbounded
    (Think) topical path grounds, the pre-check must not say 'no page can'."""
    import hashlib
    from pathlib import Path
    import json

    from app.core import extract

    fx = Path(__file__).parent / "fixtures" / "web_eval"
    urls = {
        "leaderboard.html": "https://benchlm.test/leaderboard",
        "leaderboard_cards.html": "https://benchlm.test/cards",
        "no_answer.html": "https://benchlm.test/methodology",
        "pricing_v2.html": "https://orbital.test/pricing",
        "hosting_costs.html": "https://nimbus.test/pricing",
    }
    for name, url in urls.items():
        ext = extract.extract_readable("text/html", fx.joinpath(name).read_bytes(), url)
        db.upsert_web_page(url, url, url, ext.title or "", ext.text or "", "text/html", 200,
                           hashlib.sha256((ext.text or "").encode()).hexdigest())

    def dense_rows(query):
        q = set(web_memory._terms(query))
        with db.connection() as con:
            rows = con.execute("SELECT id, url, title, text FROM web_pages").fetchall()
        hits = []
        for r in rows:
            ratio = len(q & set(web_memory._terms(f"{r['title']} {r['text']}"))) / max(len(q), 1)
            if ratio >= 0.5:
                hits.append({"url": r["url"], "title": r["title"], "page_id": r["id"], "fetched_at": "",
                             "text": web_memory._best_window(r["text"], query), "score": 0.2 + (1 - ratio) * 0.6})
        return sorted(hits, key=lambda h: h["score"])

    async def dense(query, top_k=6, site_prefix=""):
        return (await db.run_in_thread(dense_rows, query))[:top_k]

    monkeypatch.setattr(web_index, "retrieve", dense)
    questions = [c["question"] for c in json.loads(fx.joinpath("cases.json").read_text())["cases"] if c.get("question")]
    questions += ["Explain the BenchLM reasoning leaderboard methodology", "what is the boiling point of water?"]
    grounded = 0
    for q in questions:
        out = lk.Prepared(verdict=freshness.static_timeless_task())
        run(lk._topical(q, out, effort="think"))
        if out.decision == "static_topical":
            grounded += 1
            assert lk._topical_precheck(q) is not False, q
    assert grounded >= 4, "the fixture set must exercise real topical hits"


# ── the router ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("question", ["hello, how are you?", "write me a haiku about autumn", "translate this sentence"])
def test_a_fast_timeless_task_never_asks_the_router(monkeypatch, question):
    calls = []
    _router(monkeypatch, Freshness.RECENT, calls=calls)
    monkeypatch.setattr(lk, "_topical_precheck", lambda q: False)
    prepared = run(_prepare(question))
    assert calls == []
    assert prepared.verdict.requirement is Freshness.STATIC
    assert prepared.verdict.reason == freshness.TIMELESS_TASK_REASON


#: The prover's probe (scratchpad/prove/lk/fast_lookup_probe.py) plus the rest
#: of its list: live values asked with no recency word.
LIVE_WITHOUT_A_RECENCY_WORD = [
    "euro to dollar", "is AWS down", "Tesla share value", "score of india vs australia",
    "upcoming iPhone release date", "is the vLLM 0.12 out", "GPT-5.2 benchmark score",
    "what did Sam Altman say", "tell me about OpenAI o5",
]


@pytest.mark.parametrize("question", LIVE_WITHOUT_A_RECENCY_WORD)
def test_a_fast_live_value_question_asks_the_router_and_makes_the_live_lookup(monkeypatch, question):
    calls, lookups = [], []
    _router(monkeypatch, Freshness.RECENT, calls=calls)

    async def empty(q, **kw):
        return Retrieval(query=q, freshness=kw["level"])

    async def lookup(q, verdict, **kw):
        lookups.append(q)
        return None

    monkeypatch.setattr(lk, "retrieve", empty)
    monkeypatch.setattr(lk, "_fast_lookup", lookup)
    prepared = run(_prepare(question, web_search_pref="auto", allow_network=True))
    assert calls == [question], "the router must decide a live-value question"
    assert prepared.verdict.reason == "router" and prepared.verdict.needs_evidence
    assert lookups == [question], "HEAD made the Fast live lookup here; so must this"
    assert prepared.decision == "fast_lookup_failed"


def test_a_time_sensitive_fast_question_still_consults_the_router(monkeypatch):
    calls = []
    _router(monkeypatch, Freshness.RECENT, calls=calls)
    prepared = run(_prepare("what is the price of a used bicycle"))
    assert len(calls) == 1
    assert prepared.verdict.reason == "router"
    assert prepared.verdict.requirement is Freshness.RECENT


def test_fast_starts_the_time_sensitive_retrieval_while_the_router_is_deciding(monkeypatch):
    seen = []

    async def body():
        gate = asyncio.Event()
        calls = []
        _router(monkeypatch, Freshness.RECENT, gate=gate, calls=calls)

        async def fake_retrieve(q, **kw):
            seen.append(kw)
            gate.set()  # the router can only answer once retrieval has started
            return Retrieval(query=q, freshness=kw["level"])

        monkeypatch.setattr(lk, "retrieve", fake_retrieve)
        return await _prepare("what is the price of a used bicycle")

    prepared = run(body())
    # The router answered (it did not hit its 0.6 s deadline), so retrieval was
    # already running while it decided — and that one run was used.
    assert prepared.verdict.reason == "router"
    assert len(seen) == 1
    assert seen[0]["level"] is Freshness.RECENT and seen[0]["top_k"] == 5
    assert prepared.retrieval is not None


def test_a_router_verdict_that_changes_the_level_discards_the_speculative_retrieval(monkeypatch):
    _router(monkeypatch, Freshness.STATIC)
    monkeypatch.setattr(lk, "_topical_precheck", lambda q: None)
    levels, cancelled = [], []

    async def fake_retrieve(q, **kw):
        levels.append(kw["level"])
        if kw["level"] is Freshness.RECENT:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        return Retrieval(query=q, freshness=kw["level"])

    monkeypatch.setattr(lk, "retrieve", fake_retrieve)
    prepared = run(_prepare("what is the price of a used bicycle"))
    assert prepared.verdict.requirement is Freshness.STATIC
    assert levels == [Freshness.RECENT, Freshness.STATIC]
    assert cancelled == [True]
    assert prepared.retrieval is not None and prepared.retrieval.freshness is Freshness.STATIC


def test_a_router_timeout_that_changes_supersession_recomputes_uncached(monkeypatch):
    """Router down -> the 'default' verdict, which (for a non-volatile question)
    forbids supersession the router-labelled speculative run allowed. Its
    result must not be used. Revised 2026-09-13 (second prover pass): the
    speculative run never writes the level-keyed cache, so the recomputation
    is exactly HEAD's call, cache read included."""

    async def down(question):
        await asyncio.sleep(0)  # the connection attempt yields before it fails
        raise RuntimeError("router unavailable")

    monkeypatch.setattr(freshness, "_ask_router", down)
    calls = []

    async def fake_retrieve(q, **kw):
        calls.append((kw["verdict"].reason, kw.get("use_cache", True), kw.get("cache_store", True)))
        return Retrieval(query=q, freshness=kw["level"])

    monkeypatch.setattr(lk, "retrieve", fake_retrieve)
    question = "what is the cost of a used bicycle"
    prepared = run(_prepare(question))
    assert prepared.verdict.reason == "default" and not prepared.verdict.volatile
    assert calls[-1] == ("default", True, True)
    assert calls[0] == ("router", True, False)


# ── Think and Max are unchanged ──────────────────────────────────────────────


@pytest.mark.parametrize("effort", ["think", "max"])
def test_a_time_sensitive_think_or_max_question_retrieves_after_the_router_with_todays_arguments(monkeypatch, effort):
    order, kwargs = [], []

    async def ask(question):
        order.append("router")
        await asyncio.sleep(0)
        return Verdict(Freshness.RECENT, freshness._MAX_AGE[Freshness.RECENT], "router")

    async def fake_retrieve(q, **kw):
        order.append("retrieve")
        kwargs.append(kw)
        return Retrieval(query=q, freshness=kw["level"])

    monkeypatch.setattr(freshness, "_ask_router", ask)
    monkeypatch.setattr(lk, "retrieve", fake_retrieve)
    prepared = run(_prepare("what is the price of a used bicycle", effort=effort))
    assert order == ["router", "retrieve"]
    assert kwargs == [{"level": Freshness.RECENT, "top_k": 5, "effort": effort, "verdict": prepared.verdict}]


@pytest.mark.parametrize("effort", ["think", "max"])
def test_think_and_max_still_ask_the_router_first_and_wait_for_slow_grounding(monkeypatch, effort):
    _tune(monkeypatch, "_FAST_TOPICAL_DEADLINE_S", 0.01)
    order = []

    async def ask(question):
        order.append("router")
        return Verdict(Freshness.STATIC, freshness._MAX_AGE[Freshness.STATIC], "router")

    async def slow(q, **kw):
        order.append("retrieve")
        await asyncio.sleep(0.08)
        return Retrieval(query=q, freshness=kw["level"])

    def precheck(q):
        order.append("precheck")
        return False

    monkeypatch.setattr(freshness, "_ask_router", ask)
    monkeypatch.setattr(lk, "retrieve", slow)
    monkeypatch.setattr(lk, "_topical_precheck", precheck)
    kwargs = []

    async def slow_recording(q, **kw):
        kwargs.append(kw)
        return await slow(q, **kw)

    monkeypatch.setattr(lk, "retrieve", slow_recording)
    prepared = run(_prepare("hello, how are you?", effort=effort))
    assert order == ["router", "retrieve"], "no pre-check, no speculation, router first"
    assert kwargs == [{"level": Freshness.STATIC, "top_k": 4}], "the exact call Think/Max always made"
    assert prepared.verdict.reason == "router"
    assert prepared.retrieval is not None and prepared.degraded == ""
    assert prepared.decision == "static_model"


# ── the speculative retrieval and the evidence cache (2026-09-13) ────────────


def test_a_speculative_retrieval_the_router_overrules_leaves_nothing_in_the_evidence_cache(monkeypatch):
    """The prover's low finding: turn 1's router says STATIC, but the RECENT
    run started beside it finishes first and cached ITS partition (reason
    'router', supersession allowed) under the RECENT key, which carries no
    verdict. Turn 2's router times out ('default', no supersession) and would
    have been served that partition. HEAD never ran the RECENT retrieval."""
    monkeypatch.setattr(settings, "knowledge_evidence_cache_ttl_s", 300.0)
    monkeypatch.setattr(settings, "knowledge_rerank", False)
    _seed_docs(monkeypatch)
    question = "what is the price of the photosynthesis simulator configuration"
    assert freshness.router_would_be_asked(question, now_year=datetime.now(timezone.utc).year)
    finished = []
    real_retrieve = lk.retrieve

    async def watched(q, **kw):
        result = await real_retrieve(q, **kw)
        finished.append((kw["level"], bool(result.evidence)))
        return result

    monkeypatch.setattr(lk, "retrieve", watched)

    async def static_after_the_retrieval(q):
        for _ in range(200):  # the speculative run has finished, evidence and all
            if finished:
                break
            await asyncio.sleep(0.01)
        return Verdict(Freshness.STATIC, freshness._MAX_AGE[Freshness.STATIC], "router")

    monkeypatch.setattr(freshness, "_ask_router", static_after_the_retrieval)
    prepared = run(_prepare(question))
    assert prepared.verdict.requirement is Freshness.STATIC
    assert finished and finished[0] == (Freshness.RECENT, True), finished
    recent_keys = [k for k in web_memory._cache if ":recent:" in k]
    assert recent_keys == [], recent_keys


def test_a_speculative_retrieval_the_router_confirms_is_cached_under_the_real_verdict(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_evidence_cache_ttl_s", 300.0)
    monkeypatch.setattr(settings, "knowledge_rerank", False)
    _seed_docs(monkeypatch)
    question = "what is the price of the photosynthesis simulator configuration"
    _router(monkeypatch, Freshness.RECENT)
    prepared = run(_prepare(question))
    assert prepared.verdict.requirement is Freshness.RECENT and prepared.verdict.reason == "router"
    assert prepared.retrieval is not None and prepared.retrieval.evidence, "the fixture must produce evidence"
    assert [k for k in web_memory._cache if ":recent:" in k], "a reused speculative result is cached"
