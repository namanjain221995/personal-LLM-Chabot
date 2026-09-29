"""A question about a paste is classified and looked up by the person's words.

MEASURED 2026-09-29 (Fast, component harness on the live stack, 8 pastes of
~5,000 words each with a question of the person's own at the end):
  - 3 of 8 were ruled lexical:office because of "head of" / "president"
    INSIDE the pasted material. Each spent a 6.1-9.1 s web lookup and put
    ~7.2k characters of unrelated pages into the prompt (first token 7.8-10.7 s).
  - The other 5 were settled STATIC only because the paste held a year, and
    spent 1.4-2.8 s windowing 24 stored pages against a ~730-term query.
  classify_offline on the person's own words gave `default` or `lexical:static`
  for all 8.

The contract: when the message is a paste (and not a transform ask, which is
settled before this), the freshness verdict, the router question and every
retrieval read `pasted.search_words(message)` — the same words the web query
already uses — resolved against history as usual. No words of the person's
own: STATIC `pasted_no_ask`, `static_model`, nothing retrieved. The model
still receives the whole message; this changes only what is looked up.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from app import freshness, rerank, web_memory
from app import living_knowledge as lk
from app.config import settings
from app.core import pasted
from app.freshness import Freshness, Verdict, _MAX_AGE
from app.web_memory import Retrieval

from tests.test_pasted_regress_review import TYPED
from tests.test_prepass_rerank_equivalence import _distinct, _seed, _src

#: A synthetic services agreement: an office-holder phrase the freshness rule
#: fires on ("President", "head of"), paragraphs, well over 300 characters.
AGREEMENT = "\n\n".join([
    "SERVICES AGREEMENT",
    "1. Parties. This agreement is made between the Client and the Provider, signed by the\n"
    "President of the Provider and the head of procurement of the Client.",
    "2. Term. The agreement runs for twenty four months from the effective date and renews\n"
    "automatically for further twelve month periods unless either party gives notice.",
    "3. Fees. The Client pays the fees in Schedule A within thirty days of each invoice; late\n"
    "amounts carry interest at one percent a month until paid in full.",
    "4. Confidentiality. Each party keeps the other's confidential information secret.",
])
#: The same material as one block of lines: indistinguishable from a person
#: typing a long message, so it is read whole, as before.
AGREEMENT_ONE_BLOCK = AGREEMENT.replace("\n\n", "\n")
OWN = "what does clause 3 mean?"
ASKED = f"{AGREEMENT}\n\n{OWN}"


class _Seen:
    def __init__(self):
        self.retrieved = []
        self.lookups = []
        self.router = []
        self.claims = []


def _stage(monkeypatch, router_level=Freshness.STATIC) -> _Seen:
    seen = _Seen()

    async def empty(q, *, level=Freshness.RECENT, **kw):
        seen.retrieved.append((q, level))
        return Retrieval(query=q, freshness=level)

    async def lookup(question, verdict, **kw):
        seen.lookups.append(question)
        return None

    async def ask(question):
        seen.router.append(question)
        return Verdict(router_level, _MAX_AGE[router_level], "router")

    def claims(q, limit=3):
        seen.claims.append(q)
        return []

    monkeypatch.setattr(lk, "retrieve", empty)
    monkeypatch.setattr(lk, "_fast_lookup", lookup)
    monkeypatch.setattr(lk, "claims_for", claims)
    monkeypatch.setattr(freshness, "_ask_router", ask)
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(settings, "freshness_router_enabled", True)
    monkeypatch.setattr(settings, "freshness_fast_lookup", True)
    monkeypatch.setattr(settings, "living_knowledge_topical", True)
    monkeypatch.setattr(settings, "knowledge_fast_topical_precheck", False, raising=False)
    monkeypatch.setattr(settings, "knowledge_evidence_cache_ttl_s", 0.0, raising=False)
    return seen


def _prepare(message, history=(), effort="fast"):
    return asyncio.run(lk.prepare(message, effort=effort, mode="assistant", web_search_pref="auto",
                                  allow_network=True, history=history))


def test_the_premise_the_paste_holds_an_office_word_and_a_question_of_the_person_s_own():
    assert pasted.is_paste(ASKED) and not pasted.is_transform_ask(ASKED)
    assert freshness.classify_offline(ASKED, now_year=2026).reason == "lexical:office"
    assert pasted.search_words(ASKED) == OWN


def test_words_inside_a_paste_do_not_buy_a_lookup(monkeypatch):
    """dev: lexical:office, a RECENT retrieval of the whole paste, and a lookup."""
    seen = _stage(monkeypatch)
    p = _prepare(ASKED)
    assert p.verdict.reason != "lexical:office"
    assert p.verdict.requirement is Freshness.STATIC
    assert [q for q, _ in seen.retrieved] == [OWN], "retrieval reads the person's words only"
    assert seen.lookups == []
    assert p.decision == "static_model"


def test_a_paste_with_no_words_of_the_person_s_own_retrieves_nothing(monkeypatch):
    seen = _stage(monkeypatch)
    p = _prepare(AGREEMENT)
    assert seen.retrieved == [] and seen.lookups == [] and seen.router == [] and seen.claims == []
    assert (p.verdict.requirement, p.verdict.reason) == (Freshness.STATIC, "pasted_no_ask")
    assert p.decision == "static_model"


def test_a_live_question_about_a_paste_still_looks_up_with_its_own_words(monkeypatch):
    """Guard (passes on dev as well): the price question still reaches the web."""
    seen = _stage(monkeypatch)
    own = "is this price still current?"
    p = _prepare(f"{AGREEMENT}\n\n{own}")
    assert p.verdict.requirement is Freshness.RECENT
    assert seen.lookups and pasted.web_query(seen.lookups[0]) == own


def test_an_undecided_question_about_a_paste_asks_the_router_its_own_words(monkeypatch):
    seen = _stage(monkeypatch, router_level=Freshness.RECENT)
    own = "is the vendor named here any good?"
    p = _prepare(f"{AGREEMENT}\n\n{own}")
    assert seen.router == [own]
    assert p.verdict.reason == "router"
    assert all(q == own for q, _ in seen.retrieved)


def test_a_typed_multi_line_question_is_read_whole_as_before(monkeypatch):
    """Guard (passes on dev as well): a person typing a long question in one
    block is not a paste with no ask. It is classified and retrieved whole,
    and its web query is still its last line (review 2026-09-19, R2)."""
    seen = _stage(monkeypatch)
    p = _prepare(TYPED)
    assert (p.verdict.requirement, p.verdict.reason) == (Freshness.RECENT, "year:2026")
    assert [q for q, _ in seen.retrieved] == [TYPED]
    assert [pasted.web_query(q) for q in seen.lookups] == [TYPED.splitlines()[-1]]


def test_one_block_of_material_is_read_whole_as_before(monkeypatch):
    """The limit of the rule, pinned: material with no blank line in it and no
    question set apart cannot be told from typed text, so it keeps dev's
    reading (the measured pastes all had paragraphs and a question)."""
    _stage(monkeypatch)
    assert pasted.is_paste(AGREEMENT_ONE_BLOCK) and pasted.own_words(AGREEMENT_ONE_BLOCK) == ""
    assert _prepare(AGREEMENT_ONE_BLOCK).verdict.reason == "lexical:office"


def test_the_paste_question_is_resolved_against_history_as_usual(monkeypatch):
    seen = _stage(monkeypatch)
    history = [
        {"role": "user", "content": "I am reviewing a supplier contract for our clinic"},
        {"role": "assistant", "content": "Happy to help with the supplier contract."},
    ]
    _prepare(ASKED, history)
    (q, _), = seen.retrieved
    assert q.startswith(OWN) and "supplier" in q, q
    assert "Provider" not in q and "procurement" not in q


def test_a_transform_ask_is_still_settled_first(monkeypatch):
    seen = _stage(monkeypatch)
    sample = "\n".join(["Job Title: Data Engineer", "Company: Example", "Location: Pune", "Skills: SQL"])
    message = f"{AGREEMENT}\n\nrewrite this in the same format as the sample below\n\n{sample}"
    assert pasted.is_transform_ask(message)
    p = _prepare(message)
    assert p.verdict.reason == "pasted_transform" and seen.retrieved == []


def test_the_setting_off_restores_reading_the_whole_message(monkeypatch):
    _stage(monkeypatch)
    monkeypatch.setattr(settings, "knowledge_paste_own_words", False, raising=False)
    p = _prepare(ASKED)
    assert p.verdict.reason == "lexical:office"


@pytest.mark.parametrize("effort", ["think", "max"])
def test_think_and_max_read_the_person_s_words_too(monkeypatch, effort):
    seen = _stage(monkeypatch)
    p = _prepare(ASKED, effort=effort)
    assert p.verdict.requirement is Freshness.STATIC
    assert not p.escalate
    assert all(q == OWN for q, _ in seen.retrieved)


# ---------------------------------------------------------------------------
# The golden: decision, sources, what was retrieved and looked up, and what
# the cross-encoder was asked, for 6 paste turns and 6 follow-ups, over the
# web_eval corpus with the real PostgreSQL lexical half, merge, rank,
# answerability and partition (only the dense half, the cross-encoder and the
# router are stubbed, deterministically — the harness of
# tests/test_prepass_rerank_equivalence.py). Written once from this change
# with PREPASS_PASTE_GOLDEN_WRITE=1; dev differs on every paste case that
# carries material (the verdict read the paste) and on the weak-time
# follow-ups (no router call, a lookup of the bare sentence).
# ---------------------------------------------------------------------------

GOLDEN = Path(__file__).parent / "fixtures" / "prepass_paste_followup_golden.json"

#: A pasted e-mail with office words in it, and a question of the person's own.
EMAIL = "\n".join([
    "From: procurement desk",
    "To: platform team",
    "Subject: GPU rental quotes for the next quarter",
    "Hi all, the head of infrastructure and our president asked for three quotes for H100",
    "capacity. Orbital Compute and Nimbus Cloud both replied last week; please compare them",
    "and send a one page summary before the budget review on Friday.",
    "Regards, the procurement desk",
])
NOTES = "\n".join([
    "Meeting notes, model evaluation sync",
    "Attendees: the president of the research guild, the head of evaluation, two engineers",
    "1. BenchLM reasoning leaderboard reviewed; our shortlist is GPT-5.2 and Aurora-Max.",
    "2. Cost per GPU-hour matters as much as the score for the pilot.",
    "3. Next sync in two weeks; owners to bring updated numbers.",
])
SAMPLE = "\n".join(["Job Title: Data Engineer", "Company: Example", "Location: Pune", "Skills: SQL"])
LEADERBOARD_Q = "Which models are ranked on the BenchLM reasoning leaderboard?"
H100_Q = "What does an H100 cost per GPU-hour on Orbital Compute?"


def _turns(user, assistant):
    return [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]


GOLDEN_CASES = [
    # ── pastes ──
    {"id": "paste-email-own-price-question", "fixtures": ["pricing_v1.html", "pricing_v2.html"],
     "message": f"{EMAIL}\n\n{H100_Q}"},
    {"id": "paste-agreement-clause-question", "fixtures": ["leaderboard.html"], "message": ASKED},
    {"id": "paste-agreement-no-ask", "fixtures": ["leaderboard.html"], "message": AGREEMENT},
    {"id": "paste-notes-still-current", "fixtures": ["leaderboard.html"],
     "message": f"{NOTES}\n\nis this price still current?"},
    {"id": "paste-transform-ask", "fixtures": ["leaderboard.html"],
     "message": f"{AGREEMENT}\n\nrewrite this in the same format as the sample below\n\n{SAMPLE}"},
    {"id": "typed-multi-line-question", "fixtures": ["hosting_costs.html"], "message": TYPED},
    # ── follow-ups ──
    {"id": "followup-bare-pronoun", "fixtures": ["leaderboard.html"], "message": "and its score?",
     "history": _turns(LEADERBOARD_Q, "The top entries are Aurora-Max, Meridian-Pro and Solaris-9; "
                                      "GPT-5.2 also appears in the ranking.")},
    {"id": "followup-exact-variant", "fixtures": ["leaderboard.html"], "message": "what about 5.2?",
     "history": _turns("How does GPT-5 do on BenchLM reasoning?", "GPT-5 scores 83.2, ranked 11th.")},
    {"id": "followup-weak-today-rome", "fixtures": ["leaderboard.html"],
     "message": "which of those three is most accepted today?",
     "history": _turns("what are the main theories about why the Roman empire fell?",
                       "Three are most discussed: economic decline, military overreach and "
                       "political instability.")},
    {"id": "followup-price-today", "fixtures": ["pricing_v1.html", "pricing_v2.html"],
     "message": "and the price today?", "history": _turns(H100_Q, "It was 2.90 dollars an hour.")},
    {"id": "followup-what-about-now", "fixtures": ["pricing_v1.html", "pricing_v2.html"],
     "message": "what about now?", "history": _turns(H100_Q, "It was 2.90 dollars an hour.")},
    {"id": "followup-weak-these-days", "fixtures": ["leaderboard.html"],
     "message": "is it still the best one these days?",
     "history": _turns(LEADERBOARD_Q, "Aurora-Max leads the ranking.")},
]


@pytest.fixture
def _golden_env(monkeypatch):
    web_memory.cache_clear()
    lk._page_vocabulary.reset()
    rerank.reset_for_tests()
    for name, value in dict(
        knowledge_evidence_cache_ttl_s=0.0, web_memory_enabled=True, living_knowledge_topical=True,
        freshness_router_enabled=True, freshness_fast_skip_router=False, knowledge_rerank=True,
        knowledge_rerank_candidates=12, knowledge_fast_concurrent_retrieve=True,
        freshness_fast_lookup=True,
    ).items():
        monkeypatch.setattr(settings, name, value, raising=False)
    monkeypatch.setattr(lk, "claims_for", lambda q, limit=3: [])
    yield
    rerank.reset_for_tests()


def _observe_golden(case, monkeypatch):
    sent, retrieved, lookups, asked = [], [], [], []
    _seed({"fixtures": case["fixtures"]}, monkeypatch, sent)
    real = web_memory.retrieve

    async def spy(q, **kw):
        retrieved.append([q, kw["level"].value, kw.get("top_k")])
        return await real(q, **kw)

    async def lookup(question, verdict, **kw):
        lookups.append(question)
        return None

    async def router(question):
        asked.append(question)
        await asyncio.sleep(0)
        low = question.lower()
        level = (
            Freshness.RECENT if any(w in low for w in ("price", "cost", "cheaper", "rate"))
            else Freshness.STATIC
        )
        return Verdict(level, _MAX_AGE[level], "router")

    monkeypatch.setattr(lk, "retrieve", spy)
    monkeypatch.setattr(lk, "_fast_lookup", lookup)
    monkeypatch.setattr(freshness, "_ask_router", router)
    p = asyncio.run(lk.prepare(case["message"], effort="fast", mode="assistant", web_search_pref="auto",
                               allow_network=True, history=case.get("history", ())))
    return json.loads(json.dumps({
        "verdict": [p.verdict.requirement.value, p.verdict.reason] if p.verdict else None,
        "decision": p.decision,
        "router_asked": asked,
        "retrieved": sorted(retrieved),
        # What reaches the search provider (`_fast_lookup` sends web_query).
        "lookups": [pasted.web_query(q) for q in lookups],
        "sources": _src(p.sources),
        "rerank_distinct": _distinct(sent),
    }))


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=[c["id"] for c in GOLDEN_CASES])
def test_paste_and_follow_up_turns_match_the_golden(case, monkeypatch, _golden_env):
    observed = _observe_golden(case, monkeypatch)
    if os.environ.get("PREPASS_PASTE_GOLDEN_WRITE"):
        data = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
        data[case["id"]] = observed
        GOLDEN.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        return
    golden = json.loads(GOLDEN.read_text())[case["id"]]
    for key in ("verdict", "decision", "lookups", "sources", "retrieved"):
        assert observed[key] == golden[key], f"{case['id']}: {key} moved"
    assert observed == golden


def test_no_golden_retrieval_or_lookup_carries_pasted_material():
    golden = json.loads(GOLDEN.read_text())
    assert len(golden) == len(GOLDEN_CASES) == 12
    for case in GOLDEN_CASES:
        if case["id"].startswith("typed-"):
            continue  # typed text: all of it is the person's own
        material = pasted.pasted_material(case["message"])
        got = golden[case["id"]]
        for q in [q for q, _, _ in got["retrieved"]] + got["lookups"] + got["router_asked"]:
            assert not pasted.carries_paste(q, material), (case["id"], q[:80])
