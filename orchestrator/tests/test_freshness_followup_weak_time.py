"""A weak time word inside a conversation is a question for the router.

MEASURED 2026-09-29 (Fast, component harness on the live stack): in a thread
about the fall of Rome, "which of those three is most accepted today?" matched
lexical:recent on "today" alone and set off a 4.3 s web lookup with the
subject-less sentence as its query, which put 7.1k characters of unrelated
pages (vivianvoss.net, drwho.virtadpt.net, lmika.org) into the prompt. First
token 4,865 ms against 689-861 ms for the other follow-ups.

`resolve_from_history` leaves that message as it is (it has four content
words, so it "stands on its own"), so the rule keys on the turn having earlier
turns, not on the resolver having changed it. Inside a conversation, a verdict
whose ONLY signal is today / now / nowadays / these days goes to the router;
if the router cannot answer, the regex verdict stands as before. Every strong
signal keeps its verdict with no router call, and a question with no earlier
turn is never affected.
"""
from __future__ import annotations

import asyncio

import pytest

from app import freshness
from app import living_knowledge as lk
from app.config import settings
from app.freshness import Freshness, Verdict, _MAX_AGE, classify
from app.web_memory import Retrieval

from tests.test_freshness import CASES as FRESHNESS_CASES
from tests.test_freshness_time_signal import LIVE_VALUE, TIMELESS
from tests.test_living_knowledge_fast_budget import LIVE_IN_A_TIMELESS_SHAPE, LIVE_WITHOUT_A_RECENCY_WORD

ROME = [
    {"role": "user", "content": "what are the main theories about why the Roman empire fell?"},
    {"role": "assistant", "content": "Three are most discussed: economic decline, military "
                                     "overreach and political instability."},
]
GOLD = [
    {"role": "user", "content": "what is the gold price in Surat?"},
    {"role": "assistant", "content": "Gold was 7,100 rupees a gram yesterday."},
]


@pytest.mark.parametrize("q", [
    "which of those three is most accepted today?",
    "is that still true now?",
    "what do historians think nowadays?",
    "is it the same these days?",
])
def test_a_weak_time_word_alone_is_weak(q):
    assert freshness.weak_time_only(q, now_year=2026)


@pytest.mark.parametrize("q", [
    "what is the latest one?",
    "which is the current one?",
    "and right now?",
    "today's weather",
    "the exchange rate today",
    "who is the president today?",
    "is it still true in 2026?",
    "and the price today?",
    "which version today?",
    "what is the rate now?",
    "and the score now?",
    "who won today?",
    "what happened this week and today?",
])
def test_every_strong_signal_keeps_its_verdict(q):
    assert not freshness.weak_time_only(q, now_year=2026)


def test_a_long_message_is_not_a_terse_follow_up():
    """It states its own subject, and the regex pass is not run on it twice."""
    long = "tell me more about the migration plan and the rollout " * 10 + "today?"
    assert len(long) > 500
    assert freshness._deterministic(long, 2026).reason == "lexical:recent"
    assert not freshness.weak_time_only(long, now_year=2026)


class _Router:
    def __init__(self, level=Freshness.STATIC, fail=False):
        self.asked = []
        self.level = level
        self.fail = fail

    async def __call__(self, question):
        self.asked.append(question)
        if self.fail:
            raise RuntimeError("router unavailable")
        return Verdict(self.level, _MAX_AGE[self.level], "router")


def _stage(monkeypatch, router: _Router):
    """An empty store, the lookup recorded instead of run."""
    lookups, retrieved = [], []

    async def empty(q, *, level=Freshness.RECENT, **kw):
        retrieved.append((q, level))
        return Retrieval(query=q, freshness=level)

    async def lookup(question, verdict, **kw):
        lookups.append(question)
        return None

    monkeypatch.setattr(freshness, "_ask_router", router)
    monkeypatch.setattr(lk, "retrieve", empty)
    monkeypatch.setattr(lk, "_fast_lookup", lookup)
    monkeypatch.setattr(lk, "claims_for", lambda q, limit=3: [])
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(settings, "freshness_router_enabled", True)
    monkeypatch.setattr(settings, "freshness_fast_lookup", True)
    monkeypatch.setattr(settings, "living_knowledge_topical", True)
    monkeypatch.setattr(settings, "knowledge_fast_topical_precheck", False, raising=False)
    monkeypatch.setattr(settings, "knowledge_evidence_cache_ttl_s", 0.0, raising=False)
    return lookups, retrieved


def _prepare(q, history=(), effort="fast"):
    return asyncio.run(lk.prepare(q, effort=effort, mode="assistant", web_search_pref="auto",
                                  allow_network=True, history=history))


def test_the_measured_follow_up_asks_the_router_and_makes_no_lookup(monkeypatch):
    """dev: lexical:recent, no router call, and a web lookup of the bare sentence."""
    router = _Router(Freshness.STATIC)
    lookups, _ = _stage(monkeypatch, router)
    p = _prepare("which of those three is most accepted today?", ROME)
    assert router.asked == ["which of those three is most accepted today?"]
    assert (p.verdict.requirement, p.verdict.reason) == (Freshness.STATIC, "router")
    assert p.decision == "static_model"
    assert lookups == []


def test_the_router_saying_recent_still_looks_it_up(monkeypatch):
    router = _Router(Freshness.RECENT)
    lookups, _ = _stage(monkeypatch, router)
    p = _prepare("which of those three is most accepted today?", ROME)
    assert (p.verdict.requirement, p.verdict.reason) == (Freshness.RECENT, "router")
    assert lookups == ["which of those three is most accepted today?"]


def test_a_router_that_cannot_answer_leaves_the_regex_verdict(monkeypatch):
    router = _Router(fail=True)
    lookups, _ = _stage(monkeypatch, router)
    p = _prepare("which of those three is most accepted today?", ROME)
    assert router.asked, "it was asked"
    assert (p.verdict.requirement, p.verdict.reason) == (Freshness.RECENT, "lexical:recent")
    assert lookups == ["which of those three is most accepted today?"], "exactly what dev did"


def test_a_resolved_follow_up_with_a_price_keeps_lexical_recent_and_no_router(monkeypatch):
    router = _Router()
    lookups, _ = _stage(monkeypatch, router)
    p = _prepare("and the price today?", GOLD)
    assert router.asked == []
    assert (p.verdict.requirement, p.verdict.reason) == (Freshness.RECENT, "lexical:recent")
    assert lookups and lookups[0].startswith("and the price today?"), lookups


def test_a_follow_up_the_regex_pass_could_not_settle_still_asks_as_before(monkeypatch):
    """"what is" (timeless shape) and "price" (recent) both fire: undecided on
    dev too, so the router is asked exactly as before."""
    router = _Router(Freshness.RECENT)
    _stage(monkeypatch, router)
    p = _prepare("and what is the price today?", GOLD)
    assert len(router.asked) == 1
    assert p.verdict.reason == "router"


@pytest.mark.parametrize("q", ["today's weather", "and right now?", "what is the latest one?"])
def test_strong_signals_in_a_conversation_never_ask(monkeypatch, q):
    router = _Router()
    _stage(monkeypatch, router)
    p = _prepare(q, ROME)
    assert router.asked == []
    assert p.verdict.requirement in (Freshness.RECENT, Freshness.REALTIME)


def test_a_standalone_weak_time_question_is_unchanged(monkeypatch):
    router = _Router()
    lookups, _ = _stage(monkeypatch, router)
    p = _prepare("which of those three is most accepted today?", ())
    assert router.asked == []
    assert (p.verdict.requirement, p.verdict.reason) == (Freshness.RECENT, "lexical:recent")
    assert lookups


def test_the_setting_off_restores_the_regex_verdict(monkeypatch):
    router = _Router()
    _stage(monkeypatch, router)
    monkeypatch.setattr(settings, "freshness_followup_weak_time_router", False, raising=False)
    p = _prepare("which of those three is most accepted today?", ROME)
    assert router.asked == [] and p.verdict.reason == "lexical:recent"


def test_think_asks_the_router_too(monkeypatch):
    router = _Router(Freshness.STATIC)
    _stage(monkeypatch, router)
    p = _prepare("which of those three is most accepted today?", ROME, effort="think")
    assert router.asked and p.verdict.requirement is Freshness.STATIC
    assert not p.escalate


def test_the_fast_speculative_guess_for_it_is_static(monkeypatch):
    """Undecided like the 'default' population, so the retrieval started beside
    the router guesses STATIC (the router's usual answer) and is reused."""
    router = _Router(Freshness.STATIC)
    _, retrieved = _stage(monkeypatch, router)
    _prepare("which of those three is most accepted today?", ROME)
    assert retrieved == [("which of those three is most accepted today?", Freshness.STATIC)]


STANDALONE = sorted(
    {q for q, _ in FRESHNESS_CASES}
    | set(LIVE_VALUE) | set(TIMELESS)
    | set(LIVE_WITHOUT_A_RECENCY_WORD) | set(LIVE_IN_A_TIMELESS_SHAPE)
)


def test_no_standalone_verdict_moves(monkeypatch):
    """Every graded corpus question, asked with no earlier turn: the verdict
    `prepare` reaches is the one `classify` gave before this change, with the
    same deterministic router."""

    async def deterministic_router(question):
        level = (Freshness.STATIC, Freshness.RECENT, Freshness.REALTIME)[len(question) % 3]
        return Verdict(level, _MAX_AGE[level], "router")

    _stage(monkeypatch, _Router())
    monkeypatch.setattr(freshness, "_ask_router", deterministic_router)
    checked = 0
    for q in STANDALONE:
        if lk.is_pleasantry(q, ()):
            continue  # settled before any classification, as on dev
        checked += 1
        want = asyncio.run(classify(q, now_year=lk.datetime.now(lk.timezone.utc).year, allow_router=True))
        got = _prepare(q, ()).verdict
        want = lk.realtime_clamped(want)
        assert (got.requirement, got.reason, got.max_age_seconds) == (
            want.requirement, want.reason, want.max_age_seconds
        ), q
    assert checked >= 100, checked
