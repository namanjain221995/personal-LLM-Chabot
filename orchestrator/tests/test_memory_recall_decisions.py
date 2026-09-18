"""'What did we decide last time?' recalls the decision (QA-mem-5, 2026-09-18).

The freshness classifier marks "what did we decide last time about the
database?" as needing evidence (reason 'default'), so main.py asked
cross-chat recall for the USER's turns only — and a decision is almost always
stated in the assistant's answer. Measured at 4810da0 with the real embedder
over five synthetic decision conversations: 0 of 5 recall blocks carried the
decision. Separately, when assistant turns were allowed, the relative floor
(0.75 x best) dropped an answer scoring 0.599 against a floor of 0.603
because the question that led to it scored 0.804.

Real SQL, the real cache and the real ranking; only the embedding sidecar is
a deterministic fake (test_memory_semantic.py style).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app import db, llm, memory_semantic
from app.config import settings
from app.freshness import classify_offline

DECISION = "We'll go with Postgres and revisit in Q3."
QUESTION = "what did we decide last time about the database?"


def _fake_embedder(mapping, default=(0.0, 0.0, 1.0)):
    """3-dim vectors keyed by the first substring that matches."""

    async def embed(texts, **kwargs):
        out = []
        for text in texts:
            vec = list(default)
            for key, v in mapping.items():
                if key in (text or "").lower():
                    vec = list(v)
                    break
            out.append(vec)
        return out

    return embed


#: The question and the query share a direction; the answer sits at cosine
#: 0.6 to both — under the 0.75 relative floor, as the real embedder put the
#: pricing decision (0.599 vs 0.603).
_MAPPING = {
    "database": (1.0, 0.0, 0.0),
    "pricing": (1.0, 0.0, 0.0),
    "postgres": (0.6, 0.8, 0.0),
    "three tiers": (0.6, 0.8, 0.0),
    "interest rates": (0.0, 1.0, 0.0),
    "held rates": (0.0, 1.0, 0.0),
}


@pytest.fixture(autouse=True)
def fresh_cache():
    memory_semantic.invalidate_message_embeddings()
    llm.embed_cache_clear()
    yield
    memory_semantic.invalidate_message_embeddings()
    llm.embed_cache_clear()


@pytest.fixture()
def owner(monkeypatch):
    monkeypatch.setattr(llm, "embed_texts", _fake_embedder(_MAPPING))
    uid = db.create_user("alice", "hash")
    db.create_conversation(uid, "conv-db", "Billing service database")
    db.add_message(uid, "conv-db", "user", "Which database should we use for the billing service?")
    db.add_message(uid, "conv-db", "assistant", DECISION)
    db.add_message(uid, "conv-db", "user", "And how often should we take backups of it?")
    db.add_message(uid, "conv-db", "assistant", "Nightly full backups, kept for thirty days.")
    db.create_conversation(uid, "conv-news", "Interest rates")
    db.add_message(uid, "conv-news", "user", "What did the central bank decide about interest rates this month?")
    db.add_message(uid, "conv-news", "assistant", "The central bank held rates at 5.25% this month.")
    db.create_conversation(uid, "conv-new", "New chat")
    assert asyncio.run(memory_semantic.ensure_message_embeddings(uid)) == 6
    return uid


def _include_assistant_as_main_py_passes_it(question: str) -> bool:
    """main.read_cross_chat's argument, computed the same way."""
    needs_evidence = classify_offline(
        question, now_year=datetime.now(timezone.utc).year
    ).needs_evidence
    return settings.recall_assistant_answers_for_facts or not needs_evidence


def _block(uid: int, question: str, include_assistant: bool) -> str:
    return asyncio.run(
        memory_semantic.cross_chat_block(
            uid, question, "conv-new", include_assistant=include_assistant
        )
    ) or ""


def test_what_did_we_decide_recalls_the_decision_the_assistant_stated(owner):
    include_assistant = _include_assistant_as_main_py_passes_it(QUESTION)
    # The premise of the defect: the evidence gate is closed for this question.
    assert include_assistant is False
    block = _block(owner, QUESTION, include_assistant)
    assert DECISION in block
    assert "(you answered)" in block
    # The answer is the one that followed the matching question, not a later
    # turn of the same conversation.
    assert "Nightly full backups" not in block


def test_a_world_fact_question_still_excludes_assistant_turns(owner):
    question = "What did the central bank decide about interest rates?"
    include_assistant = _include_assistant_as_main_py_passes_it(question)
    assert include_assistant is False
    block = _block(owner, question, include_assistant)
    assert "interest rates this month" in block  # the user's own turn is kept
    assert "5.25" not in block
    assert "(you answered)" not in block
    assert DECISION not in block


def test_the_relative_floor_keeps_the_answer_paired_with_its_question(owner):
    """With assistant turns allowed, the answer still fell under 0.75 x the
    question's score and was dropped; paired with its question it stays."""
    uid = owner
    db.create_conversation(uid, "conv-pricing", "Launch pricing")
    db.add_message(uid, "conv-pricing", "user", "Should the launch pricing have three or two options?")
    db.add_message(uid, "conv-pricing", "assistant", "Launch with three tiers: Starter, Team and Enterprise.")
    asyncio.run(memory_semantic.ensure_message_embeddings(uid))
    block = _block(uid, "What did we decide about the pricing?", include_assistant=True)
    assert "Launch with three tiers" in block


def test_pairs_count_once_against_the_semantic_limit(owner):
    """A question and its answer are one recalled unit, so a decision query
    returns at most `semantic_limit` questions, each with its answer."""
    hits = asyncio.run(
        memory_semantic.semantic_hits(owner, QUESTION, "conv-new", limit=1, pair_answers=True)
    )
    assert [(h["role"], h["snippet"]) for h in hits] == [
        ("user", "Which database should we use for the billing service?"),
        ("assistant", DECISION),
    ]


def test_another_users_answer_is_never_paired(owner, monkeypatch):
    bob = db.create_user("bob", "hash")
    db.create_conversation(bob, "conv-bob", "Bob database")
    db.add_message(bob, "conv-bob", "user", "Which database for bob's shop?")
    db.add_message(bob, "conv-bob", "assistant", "Bob goes with Postgres too, secretly.")
    asyncio.run(memory_semantic.ensure_message_embeddings(bob))
    block = _block(owner, QUESTION, include_assistant=False)
    assert "secretly" not in block
    assert DECISION in block


@pytest.mark.parametrize(
    "question",
    [
        QUESTION,
        "What did we decide about the pricing tiers?",
        "remind me what we agreed on the vendor",
        "which framework did we settle on for the dashboard?",
        "what did you tell me about how often to run retros?",
        "We discussed the budget in another chat, what was the number?",
        "What was our decision on the hiring plan?",
        "you said something about backups last time, what was it?",
    ],
)
def test_questions_about_our_own_history_refer_to_the_past(question):
    assert memory_semantic.refers_to_past_conversation(question)


@pytest.mark.parametrize(
    "question",
    [
        "Who is the CEO of Microsoft?",
        "What did the central bank decide about interest rates?",
        "When was the last time India won the World Cup?",
        "What is the current price of bitcoin?",
        "What did the EU agree on AI regulation this year?",
        "Did we land on the moon in 1969?",
        "Which database is best for a billing service?",
        "The committee decided to postpone the vote; why?",
        "What happened in the last session of Parliament?",
        "What if I asked you to write a poem about rates?",
    ],
)
def test_world_fact_questions_do_not_refer_to_the_past(question):
    assert not memory_semantic.refers_to_past_conversation(question)
