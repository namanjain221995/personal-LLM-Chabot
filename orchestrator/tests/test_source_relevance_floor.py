"""A greeting cites nothing (pre-pass latency round, 2026-09-14).

What production showed: a Fast "hi ??" took 5.97 s and its answer listed two
"sources", a Wikipedia disambiguation page titled HI and a film trailer. The
freshness router was asked whether a greeting is time-sensitive, a retrieval
ran for the word "hi" with the previous question's words appended by
resolve_from_history, and the cross-encoder called both pages relevant.

Pinned here:
  - Rule 1: a pleasantry (closed token set: greeting, thanks, farewell,
    laughter) is served static_model with no router, no retrieval, no live
    lookup and no sources, at every effort, with or without a router timeout;
  - the unanswered-previous-turn veto: "hi ??" after a question that got no
    answer keeps that question's retrieval (a crashed live-value question
    must not lose its evidence);
  - resolve_from_history leaves a pleasantry alone but still resolves
    acknowledgements ("ok", "yes", "👍") and terse follow-ups;
  - acknowledgements, "again", "a lot", and every live-value string of the
    freshness suites (alone and wrapped in a greeting or thanks) are never
    pleasantries: the labelled guard set is a zero-false-positive gate;
  - Rule 2 (the short-question source floor) only COUNTS by default.

Self-written examples only; no production message appears here.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db, freshness, metrics, rerank, web_index, web_memory
from app import living_knowledge as lk
from app.config import settings
from app.freshness import Freshness, Verdict
from app.web_memory import Evidence, Retrieval
from tests import test_freshness, test_freshness_time_signal
from tests import test_living_knowledge_fast_budget as fast_budget

GUARD = json.loads(
    (Path(__file__).parent / "fixtures" / "prepass_pleasantry_guard_set.json").read_text(encoding="utf-8")
)["items"]
FULL_PATH = [i for i in GUARD if i["expected"] == "FULL_PATH"]
LANE = [i for i in GUARD if i["expected"] == "FAST_LANE"]

#: The guard items Rule 1 serves without a lookup. Pinned so a lexicon change
#: shows up as a diff here, not as a silent widening.
RULE_1_MATCHES = {
    "hi", "hello", "hey there", "hi ??", "good morning", "hiya!", "hello!!", "hey 👋", "hello?",
    "thanks", "thank you so much!", "thx", "thanks a lot 🙏", "lol", "haha", "lmao", "bye",
    "good night!", "take care", "thanks, bye!", "namaste", "dhanyavaad", "shukriya", "alvida",
}

ANSWERED = [
    {"role": "user", "content": "what is the RBI repo rate?"},
    {"role": "assistant", "content": "The RBI repo rate is 5.5 percent, per the policy statement."},
]
OFFER = [
    {"role": "user", "content": "help me plan the quarterly review"},
    {"role": "assistant", "content": "Here is a plan. Shall I check today's gold rate for the appendix?"},
]
UNANSWERED = [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "Hello! How can I help?"},
    {"role": "user", "content": "what is the gold price today?"},
]
EMPTY_ANSWER = UNANSWERED + [{"role": "assistant", "content": "   "}]
ERRORED_ANSWER = UNANSWERED + [
    {"role": "assistant", "content": "The gold price today is", "meta": {"interrupted": True}}
]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # No TRUNCATE here: most tests below are pure and parametrised over ~200
    # items; the one that writes pages empties the table itself.
    web_memory.cache_clear()
    lk._page_vocabulary.reset()
    rerank.reset_for_tests()
    monkeypatch.setattr(settings, "knowledge_evidence_cache_ttl_s", 0.0)
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(settings, "living_knowledge_topical", True)
    monkeypatch.setattr(settings, "freshness_router_enabled", True)
    monkeypatch.setattr(settings, "freshness_fast_lookup", True)
    monkeypatch.setattr(settings, "knowledge_rerank", True)
    monkeypatch.setattr(settings, "knowledge_pleasantry_rule", True)
    monkeypatch.setattr(settings, "knowledge_source_floor", False)
    monkeypatch.setattr(settings, "freshness_fast_skip_router", False)
    monkeypatch.setattr(lk, "claims_for", lambda q, limit=3: [])
    yield
    rerank.reset_for_tests()


# ── the owner's turn, reproduced ─────────────────────────────────────────────

_HI_PAGE = ("https://en.wiki.example/wiki/HI", "HI - Wikipedia",
            "HI or Hi may refer to: HI, the postal abbreviation for Hawaii; Hi, a greeting; "
            "HI, the chemical formula of hydrogen iodide; Hi, a song title.")
_TRAILER = ("https://video.example/watch/hi-papa-trailer", "Hi Papa - Official Trailer",
            "Watch the official trailer of Hi Papa, a family drama. Say hi to the cast in the comments.")


def _seed_greeting_junk(monkeypatch):
    """Two pages a greeting can match, judged 0.82 and 0.61 as in the owner's
    turn; the dense half and the cross-encoder are stubbed."""
    with db.connection() as con:
        con.execute("TRUNCATE web_pages RESTART IDENTITY CASCADE")
    now = datetime.now(timezone.utc) - timedelta(days=1)
    for key, (url, title, text) in (("hi", _HI_PAGE), ("trailer", _TRAILER)):
        with db.connection() as con:
            con.execute(
                """INSERT INTO web_pages (url_key, url, title, text, fetched_at, first_seen_at,
                       last_changed_at, domain, authority, indexed_at, content_hash)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (key, url, title, text, now, now, now, url.split("/")[2], 40, now, key),
            )

    async def dense(query, top_k=6, site_prefix=""):
        return [
            {"url": _HI_PAGE[0], "title": _HI_PAGE[1], "text": _HI_PAGE[2], "fetched_at": "", "score": 0.9},
            {"url": _TRAILER[0], "title": _TRAILER[1], "text": _TRAILER[2], "fetched_at": "", "score": 0.95},
        ]

    async def score(query, docs, **kw):
        return [0.82 if d.startswith(_HI_PAGE[1]) else 0.61 for d in docs]

    monkeypatch.setattr(web_index, "retrieve", dense)
    monkeypatch.setattr(rerank, "score", score)


def _spies(monkeypatch, router: str):
    seen = {"router": [], "retrieve": [], "lookup": []}

    async def ask(question):
        seen["router"].append(question)
        if router == "timeout":
            await asyncio.sleep(0.02)
            raise asyncio.TimeoutError()
        await asyncio.sleep(0)
        return Verdict(Freshness.RECENT, freshness._MAX_AGE[Freshness.RECENT], "router")

    real_retrieve = lk.retrieve

    async def retrieve(q, **kw):
        seen["retrieve"].append(q)
        return await real_retrieve(q, **kw)

    async def lookup(q, verdict, **kw):
        seen["lookup"].append(q)
        return None

    monkeypatch.setattr(freshness, "_ask_router", ask)
    monkeypatch.setattr(lk, "retrieve", retrieve)
    monkeypatch.setattr(lk, "_fast_lookup", lookup)
    return seen


def _prepare(question, *, effort="fast", history=(), allow_network=True):
    return lk.prepare(
        question, effort=effort, mode="assistant", web_search_pref="auto",
        allow_network=allow_network, history=history,
    )


def test_premise_with_the_rule_off_hi_retrieves_and_cites_the_junk_pages(monkeypatch):
    """What HEAD did (the rule is the only difference): the router is asked,
    the retrieval runs, and both junk pages are cited."""
    monkeypatch.setattr(settings, "knowledge_pleasantry_rule", False)
    _seed_greeting_junk(monkeypatch)
    seen = _spies(monkeypatch, "ok")
    prepared = run(_prepare("hi ??", history=ANSWERED))
    assert seen["router"] and seen["retrieve"]
    assert {s["title"] for s in prepared.sources} == {_HI_PAGE[1], _TRAILER[1]}


@pytest.mark.parametrize("router", ["ok", "timeout"])
@pytest.mark.parametrize("history", [(), ANSWERED, OFFER], ids=["no_history", "answered", "offer"])
def test_hi_gives_zero_sources_no_grounding_and_no_lookup(monkeypatch, router, history):
    _seed_greeting_junk(monkeypatch)
    seen = _spies(monkeypatch, router)
    before = metrics._counters.get("knowledge_pleasantry_total", {}).get((("effort", "fast"),), 0.0)
    prepared = run(_prepare("hi ??", history=history))
    assert prepared.sources == [] and prepared.grounding == ""
    assert prepared.decision == "static_model"
    assert prepared.verdict.requirement is Freshness.STATIC and prepared.verdict.reason == "pleasantry"
    assert seen == {"router": [], "retrieve": [], "lookup": []}
    assert not prepared.escalate and not prepared.searched
    after = metrics._counters["knowledge_pleasantry_total"][(("effort", "fast"),)]
    assert after == before + 1, "_clean kept the open label and counted the turn"


@pytest.mark.parametrize("effort", ["think", "max"])
def test_the_rule_applies_at_think_and_max_too(monkeypatch, effort):
    _seed_greeting_junk(monkeypatch)
    seen = _spies(monkeypatch, "ok")
    prepared = run(_prepare("thank you so much!", effort=effort, history=ANSWERED))
    assert prepared.decision == "static_model" and prepared.sources == []
    assert seen == {"router": [], "retrieve": [], "lookup": []}


@pytest.mark.parametrize("history", [UNANSWERED, EMPTY_ANSWER, ERRORED_ANSWER],
                         ids=["unanswered", "blank_answer", "interrupted_answer"])
def test_hi_after_an_unanswered_live_value_question_still_resolves_and_retrieves(monkeypatch, history):
    seen = _spies(monkeypatch, "ok")

    async def fake_retrieve(q, **kw):
        seen["retrieve"].append(q)
        return Retrieval(query=q, freshness=kw["level"])

    monkeypatch.setattr(lk, "retrieve", fake_retrieve)
    prepared = run(_prepare("hi ??", history=history))
    assert seen["retrieve"], "the unanswered question lost its retrieval"
    assert all("gold" in q for q in seen["retrieve"]), seen["retrieve"]
    assert prepared.verdict.reason != "pleasantry" and prepared.verdict.needs_evidence
    assert seen["lookup"], "the live value lost its Fast lookup"


# ── resolve_from_history ─────────────────────────────────────────────────────


@pytest.mark.parametrize("message", ["hi ??", "thanks", "namaste", "lol"])
def test_resolve_leaves_a_pleasantry_unchanged(message):
    assert lk.resolve_from_history(message, ANSWERED) == message


@pytest.mark.parametrize("message", ["ok", "yes", "and the B200?", "👍"])
def test_resolve_still_resolves_acks_and_follow_ups(message):
    resolved = lk.resolve_from_history(message, ANSWERED)
    assert resolved != message and resolved.startswith(message)
    assert "rbi" in resolved.lower()


def test_resolve_still_resolves_a_pleasantry_after_an_unanswered_question():
    assert "gold" in lk.resolve_from_history("hi ??", UNANSWERED)


def test_resolve_is_unchanged_with_the_rule_off(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_pleasantry_rule", False)
    assert "rbi" in lk.resolve_from_history("hi ??", ANSWERED).lower()


# ── near misses keep the full path ───────────────────────────────────────────


@pytest.mark.parametrize("question", ["HI sales tax", "आज सोने का भाव क्या है?"])
def test_a_near_miss_keeps_its_evidence_and_lookup_path(monkeypatch, question):
    seen = _spies(monkeypatch, "ok")

    async def fake_retrieve(q, **kw):
        seen["retrieve"].append(q)
        return Retrieval(query=q, freshness=kw["level"])

    monkeypatch.setattr(lk, "retrieve", fake_retrieve)
    prepared = run(_prepare(question, history=ANSWERED))
    assert seen["router"] and seen["retrieve"] and seen["lookup"], seen
    assert prepared.decision == "fast_lookup_failed"


@pytest.mark.parametrize("text", [
    "again", "a lot", "so much", "there", "you there", "good", "ok", "okay", "cool", "yes", "sure",
    "fine", "why", "why?", "👍", "🙏", "👌", "lol?", "thanks?", "bye?", "ok thanks bye 2", "hi 👍",
    "hi 5", "héllo", "नमस्ते", "hi there, one question",
])
def test_these_are_not_pleasantries(text):
    assert not lk.is_pleasantry(text, ANSWERED)


# ── the labelled guard set: zero false positives ─────────────────────────────


# Each test walks the whole set and reports every failing item at once (the
# conftest's per-test TRUNCATE makes one test per item cost minutes).

HISTORIES_ANSWERED = {"no_history": (), "answered": ANSWERED, "offer": OFFER}
HISTORIES_UNANSWERED = {"unanswered": UNANSWERED, "blank_answer": EMPTY_ANSWER, "interrupted_answer": ERRORED_ANSWER}


@pytest.mark.parametrize("history_id", list(HISTORIES_ANSWERED))
def test_no_full_path_item_is_a_pleasantry(history_id):
    history = HISTORIES_ANSWERED[history_id]
    wrong = [(i["id"], i["text"]) for i in FULL_PATH if lk.is_pleasantry(i["text"], history)]
    assert len(FULL_PATH) >= 130
    assert wrong == [], f"false positives: {wrong}"


@pytest.mark.parametrize("history_id", list(HISTORIES_ANSWERED))
def test_lane_items_match_exactly_the_pinned_set(history_id):
    history = HISTORIES_ANSWERED[history_id]
    matched = {i["text"] for i in LANE if lk.is_pleasantry(i["text"], history)}
    assert matched == RULE_1_MATCHES


@pytest.mark.parametrize("history_id", list(HISTORIES_UNANSWERED))
def test_no_item_is_a_pleasantry_after_an_unanswered_turn(history_id):
    history = HISTORIES_UNANSWERED[history_id]
    wrong = [(i["id"], i["text"]) for i in GUARD if lk.is_pleasantry(i["text"], history)]
    assert wrong == []


def test_the_pinned_set_is_all_lane_labelled_and_no_ack():
    by_text = {i["text"]: i for i in GUARD}
    for text in RULE_1_MATCHES:
        assert by_text[text]["expected"] == "FAST_LANE", text
        assert by_text[text]["category"] != "ack_full_path_this_round", text


_LIVE = sorted(
    set(test_freshness_time_signal.LIVE_VALUE)
    | set(fast_budget.LIVE_WITHOUT_A_RECENCY_WORD)
    | set(fast_budget.LIVE_IN_A_TIMELESS_SHAPE)
    | {q for q, _ in test_freshness.CASES}
)


def test_no_live_value_string_is_a_pleasantry_alone_or_wrapped():
    assert len(_LIVE) > 80
    wrong = []
    for question in _LIVE:
        for text in (question, "hi, " + question, question + " thanks", "namaste " + question):
            for history in (ANSWERED, ()):
                if lk.is_pleasantry(text, history):
                    wrong.append(text)
    assert wrong == []


def test_the_kill_switch_restores_the_full_path(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_pleasantry_rule", False)
    seen = _spies(monkeypatch, "ok")

    async def fake_retrieve(q, **kw):
        seen["retrieve"].append(q)
        return Retrieval(query=q, freshness=kw["level"])

    monkeypatch.setattr(lk, "retrieve", fake_retrieve)
    prepared = run(_prepare("thanks", history=()))
    assert seen["router"] == ["thanks"]
    assert prepared.verdict.reason != "pleasantry"


# ── Rule 2: the short-question source floor, shadow only ─────────────────────


def _floor_count():
    return metrics._counters.get("knowledge_source_floor_total", {}).get((("would", "drop"),), 0.0)


def _weak_but_judged_relevant(monkeypatch):
    async def fake_retrieve(q, **kw):
        ev = Evidence(
            url="https://junk.example/page", title="Junk", text="unrelated text", domain="junk.example",
            authority=40, fetched_at=datetime.now(timezone.utc), dense=0.2, lexical=0.1, answer=0.82, score=0.7,
        )
        out = Retrieval(query=q, freshness=kw["level"], evidence=[ev])
        out.newest_age = 60.0
        return out

    monkeypatch.setattr(lk, "retrieve", fake_retrieve)


@pytest.mark.parametrize("enabled", [False, True])
def test_the_source_floor_counts_and_only_drops_when_switched_on(monkeypatch, enabled):
    monkeypatch.setattr(settings, "knowledge_source_floor", enabled)
    _spies(monkeypatch, "ok")
    _weak_but_judged_relevant(monkeypatch)
    before = _floor_count()
    prepared = run(_prepare("sup", history=()))
    assert _floor_count() == before + 1
    assert prepared.decision == "local"
    assert len(prepared.sources) == (0 if enabled else 1)


@pytest.mark.parametrize("question", ["भाव?", "what is the price of the used bicycle today"])
def test_the_source_floor_ignores_indic_script_and_longer_questions(monkeypatch, question):
    monkeypatch.setattr(settings, "knowledge_source_floor", True)
    _spies(monkeypatch, "ok")
    _weak_but_judged_relevant(monkeypatch)
    before = _floor_count()
    prepared = run(_prepare(question, history=()))
    assert _floor_count() == before
    assert len(prepared.sources) == 1
