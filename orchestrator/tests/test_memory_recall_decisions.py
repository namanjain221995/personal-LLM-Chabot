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


# --- QA round 1, 2026-09-18 ---------------------------------------------------
# "last time we/you/I" matched the ordinary idiom of a world-fact question,
# and the regex read the WHOLE message, pasted material included. Either way
# a closed evidence gate was opened and a stale assistant answer recalled
# (live, real embedder: 2/2 world-fact leaks at f449785, 0/2 at 4810da0).


@pytest.mark.parametrize(
    "question",
    [
        "When was the last time we landed on the moon?",
        "When was the last time we had a recession in the US?",
        "When was the last time I could see Halley's comet from Earth?",
        "Last time I checked bitcoin was 60k, what is the bitcoin price now?",
        "Meeting notes: we agreed to ship on Friday.\nWhat is the bitcoin price now?",
        "Transcript:\nA: did we decide on the vendor?\nB: yes\n\nSummarise this transcript.",
    ],
)
def test_a_last_time_idiom_or_a_paste_does_not_refer_to_the_past(question):
    assert not memory_semantic.refers_to_past_conversation(question)


@pytest.mark.parametrize(
    "question",
    [
        "last time we talked you recommended a book, which one?",
        "what did I tell you about my allergy?",
        "in our last chat we picked a colour palette, remind me",
        "Remind me what you suggested for the logo in the other chat",
        "Hi again.\n\nwhat did we decide last time about the database?",
    ],
)
def test_more_questions_about_our_own_history(question):
    assert memory_semantic.refers_to_past_conversation(question)


@pytest.mark.parametrize(
    "question",
    [
        "What did we agree to in the Paris climate agreement?",
        "Who did we pick in the first round of the NBA draft?",
        "Why did we go with the metric system?",
        "When did we decide to leave the gold standard?",
    ],
)
def test_a_national_we_is_a_known_false_positive(question):
    """KNOWN TRADE-OFF, pinned so a change to it is deliberate. "What did we
    decide about the pricing tiers?" and "What did we agree to in the Paris
    Agreement?" have the same shape; only meaning separates them, and the
    first is the question this module exists for. The cost is bounded: these
    ask about a settled past, where a recalled earlier answer is rarely stale
    — the gate's measured failure (2026-09-03) was a CHANGING fact."""
    assert memory_semantic.refers_to_past_conversation(question)


def test_only_the_typed_question_is_read_on_a_huge_paste(monkeypatch):
    """The regex ran over the whole request text on the event loop — 1.0 to
    1.9 s on a 10 MB paste (QA). It now reads at most 2,000 characters of the
    last line."""
    seen = []
    real = memory_semantic._PAST_REFERENCE_RE

    class Spy:
        def search(self, text):
            seen.append(len(text))
            return real.search(text)

    monkeypatch.setattr(memory_semantic, "_PAST_REFERENCE_RE", Spy())
    paste = ("we agreed to ship " * 600_000) + "\n" + "x" * 50_000 + " what did we decide?"
    assert memory_semantic.refers_to_past_conversation(paste)
    assert seen and max(seen) <= 2_000


def test_a_world_fact_question_about_a_recession_does_not_recall_a_stale_answer(owner):
    uid = owner
    db.create_conversation(uid, "conv-econ", "Economy")
    db.add_message(uid, "conv-econ", "user", "When did the US last have a recession?")
    db.add_message(uid, "conv-econ", "assistant", "The last US recession was in 2020, brief and sharp.")
    asyncio.run(memory_semantic.ensure_message_embeddings(uid))
    question = "When was the last time we had a recession in the US?"
    include_assistant = _include_assistant_as_main_py_passes_it(question)
    assert include_assistant is False
    block = _block(uid, question, include_assistant)
    assert "2020" not in block
    assert "(you answered)" not in block


def test_a_user_row_after_the_question_is_never_paired_as_its_answer(owner):
    """The pair is the ASSISTANT's turn; the person's own next message (here
    unrelated, scoring 0) is not an answer and must not ride in on the pair."""
    uid = owner
    db.create_conversation(uid, "conv-cache", "Cache choice")
    db.add_message(uid, "conv-cache", "user", "Which database is right for the session cache?")
    db.add_message(uid, "conv-cache", "user", "Unrelated: book me a table for lunch on Friday.")
    db.add_message(uid, "conv-cache", "assistant", "Redis, with a one-hour expiry on sessions.")
    asyncio.run(memory_semantic.ensure_message_embeddings(uid))
    hits = asyncio.run(
        memory_semantic.semantic_hits(uid, QUESTION, "conv-new", limit=5, pair_answers=True)
    )
    snippets = [h["snippet"] for h in hits]
    assert "Which database is right for the session cache?" in snippets
    assert not any("book me a table" in s for s in snippets)


def test_two_pairs_fit_a_limit_of_two(owner):
    """Each question with its answer is ONE unit: limit=2 returns both
    questions and both answers, not one question and its answer."""
    uid = owner
    db.create_conversation(uid, "conv-wh", "Warehouse")
    db.add_message(uid, "conv-wh", "user", "Which database should hold the analytics warehouse?")
    db.add_message(uid, "conv-wh", "assistant", "We'll use ClickHouse for analytics.")
    asyncio.run(memory_semantic.ensure_message_embeddings(uid))
    hits = asyncio.run(
        memory_semantic.semantic_hits(uid, QUESTION, "conv-new", limit=2, pair_answers=True)
    )
    assert sorted((h["role"], h["snippet"]) for h in hits) == sorted(
        [
            ("user", "Which database should we use for the billing service?"),
            ("assistant", DECISION),
            ("user", "Which database should hold the analytics warehouse?"),
            ("assistant", "We'll use ClickHouse for analytics."),
        ]
    )


def test_a_decision_after_a_clarifying_question_is_recalled(owner):
    """Q -> the assistant asks a clarifying question -> the person answers ->
    the assistant decides. Paired with the clarifying question alone, the
    decision was recalled 0/3 live (QA, 2026-09-18)."""
    uid = owner
    db.create_conversation(uid, "conv-an", "Analytics store")
    db.add_message(uid, "conv-an", "user", "Postgres or MySQL for the analytics database?")
    db.add_message(uid, "conv-an", "assistant", "What query patterns do you expect, mostly large aggregates?")
    db.add_message(uid, "conv-an", "user", "Yes, mostly big aggregates over billions of events.")
    db.add_message(uid, "conv-an", "assistant", "Then go with ClickHouse instead of either of those.")
    asyncio.run(memory_semantic.ensure_message_embeddings(uid))
    block = _block(uid, QUESTION, _include_assistant_as_main_py_passes_it(QUESTION))
    assert "Then go with ClickHouse instead of either of those." in block
    assert DECISION in block


def test_a_full_answer_ending_in_an_offer_does_not_pull_in_the_next_exchange(owner):
    uid = owner
    db.create_conversation(uid, "conv-audit", "Audit log")
    db.add_message(uid, "conv-audit", "user", "Which database should keep the audit log?")
    db.add_message(
        uid,
        "conv-audit",
        "assistant",
        "Keep it in Postgres: an append-only table with a monotonically increasing id, "
        "row-level security so only the auditor role can read it, a nightly job that "
        "hashes each day's rows into a chain so tampering shows, and partitions by "
        "month so old months can be detached to cold storage without locking the "
        "table. Want me to sketch the schema?",
    )
    db.add_message(uid, "conv-audit", "user", "Unrelated: suggest a lunch spot near the office.")
    db.add_message(uid, "conv-audit", "assistant", "Try the thali place on the corner, it opens at noon.")
    asyncio.run(memory_semantic.ensure_message_embeddings(uid))
    hits = asyncio.run(
        memory_semantic.semantic_hits(uid, QUESTION, "conv-new", limit=5, pair_answers=True)
    )
    snippets = " ".join(h["snippet"] for h in hits)
    assert "Keep it in Postgres" in snippets
    assert "thali" not in snippets
    assert "lunch spot" not in snippets


# --- Review round 2, 2026-09-18 -----------------------------------------------


def test_a_decision_stated_after_the_first_240_characters_is_recalled(owner):
    """Verifier: the paired answer was cut to the 240-character snippet, and
    72.7% of production assistant messages are longer (median 744)."""
    uid = owner
    analysis = (
        "Both options are mature relational databases with strong tooling, "
        "replication and a large hiring pool. For a billing workload the things "
        "that matter are exact decimal arithmetic, transactional DDL for safe "
        "migrations, row-level locking behaviour under concurrent invoice runs "
        "and the maturity of the reporting ecosystem. "
    )
    answer = analysis * 2 + "Decision: we go with Postgres for the ledger service."
    assert len(answer) > 600
    db.create_conversation(uid, "conv-ledger", "Ledger database")
    db.add_message(uid, "conv-ledger", "user", "Which database should we use for the ledger service?")
    db.add_message(uid, "conv-ledger", "assistant", answer)
    asyncio.run(memory_semantic.ensure_message_embeddings(uid))
    block = _block(uid, QUESTION, _include_assistant_as_main_py_passes_it(QUESTION))
    assert "we go with Postgres for the ledger service" in block


def test_a_decision_in_the_middle_of_a_long_answer_is_recalled(owner):
    """Live, the main model put its conclusion 2,000-4,500 characters into a
    longer answer and closed with an offer; the opening and the close alone
    miss it. The body's decisive sentence rides along, bounded."""
    uid = owner
    filler = (
        "Consider how each option handles replication, backups, failover, "
        "schema migrations and the hiring pool in your region. "
    )
    answer = (
        filler * 6
        + "My recommendation is to go with CockroachDB for the ledger. "
        + filler * 6
        + "Want me to sketch the migration plan?"
    )
    db.create_conversation(uid, "conv-ledger", "Ledger database")
    db.add_message(uid, "conv-ledger", "user", "Which database should we use for the ledger service?")
    db.add_message(uid, "conv-ledger", "assistant", answer)
    asyncio.run(memory_semantic.ensure_message_embeddings(uid))
    block = _block(uid, QUESTION, _include_assistant_as_main_py_passes_it(QUESTION))
    assert "My recommendation is to go with CockroachDB for the ledger." in block
    line = next(ln for ln in block.splitlines() if "CockroachDB" in ln)
    assert len(line) < 4 * memory_semantic._SNIPPET_CHARS + 200


def test_a_short_decision_ending_in_an_offer_does_not_drag_in_the_next_exchange(owner):
    """Verifier: "Go with Postgres. Want me to sketch the schema?" is short
    and ends in "?", and was taken for a clarifying question."""
    uid = owner
    db.create_conversation(uid, "conv-ledger", "Ledger database")
    db.add_message(uid, "conv-ledger", "user", "Which database should we use for the ledger service?")
    db.add_message(uid, "conv-ledger", "assistant", "Go with Postgres. Want me to sketch the schema?")
    db.add_message(uid, "conv-ledger", "user", "Not now. What is the bitcoin price today?")
    db.add_message(uid, "conv-ledger", "assistant", "Bitcoin trades at 60,000 dollars today.")
    asyncio.run(memory_semantic.ensure_message_embeddings(uid))
    block = _block(uid, QUESTION, _include_assistant_as_main_py_passes_it(QUESTION))
    assert "Go with Postgres" in block
    assert "60,000" not in block


def test_the_newest_of_two_identical_questions_brings_its_own_answer(owner):
    uid = owner
    for cid, answer in (
        ("conv-old", "Use MySQL for now, postgres later."),
        ("conv-late", "Switch to Postgres now."),
    ):
        db.create_conversation(uid, cid, cid)
        db.add_message(uid, cid, "user", "Which database should we use for the payroll service?")
        db.add_message(uid, cid, "assistant", answer)
    asyncio.run(memory_semantic.ensure_message_embeddings(uid))
    block = _block(uid, QUESTION, _include_assistant_as_main_py_passes_it(QUESTION))
    assert ("Switch to Postgres now." in block) or ("Use MySQL for now" in block)


def test_decision_recall_pairs_only_the_callers_own_rows(owner):
    """Security review: another user's decision and the current chat's own
    answer never reach the block."""
    uid = owner
    bob = db.create_user("bob", "hash")
    db.create_conversation(bob, "b-db", "Bob database")
    db.add_message(bob, "b-db", "user", "Which database should we use for the ledger?")
    db.add_message(bob, "b-db", "assistant", "BOB-SECRET: go with Mongo for the ledger.")
    db.add_message(uid, "conv-new", "user", "Which database for the billing service, again?")
    db.add_message(uid, "conv-new", "assistant", "CURRENT-CHAT: the current conversation's answer about Postgres.")
    asyncio.run(memory_semantic.ensure_message_embeddings(uid))
    asyncio.run(memory_semantic.ensure_message_embeddings(bob))
    block = _block(uid, QUESTION, include_assistant=False)
    assert DECISION in block
    assert "BOB-SECRET" not in block
    assert "CURRENT-CHAT" not in block


def test_a_pasted_block_above_a_world_question_does_not_open_the_gate(owner):
    text = "Notes from the call:\nwe agreed to ship on Friday\n\nWhich database is fastest for analytics?"
    block = _block(owner, text, include_assistant=False)
    assert "(you answered)" not in block


@pytest.mark.parametrize(
    "question",
    [
        "Where did we land on the database question?",
        "What database did we land on?",
        "Which database are we going with?",
        "Remind me of the database decision.",
        "what did we decide about the database?\nThanks!",
        "what did we decide about the database?\n\nthank you so much",
    ],
)
def test_more_decision_phrasings_refer_to_the_past(question):
    """Verifier: 3 of 12 natural decision-recall phrasings matched. These
    four, and a question with a courtesy typed under it, now do; "what was
    decided", "the final call on" and "the verdict on" are as often about the
    world and stay closed."""
    assert memory_semantic.refers_to_past_conversation(question)


@pytest.mark.parametrize(
    "question",
    [
        "What was the verdict in the trial?",
        "What was decided at the G20 summit?",
        "Remind me what the Fed decided.",
        "Thanks!\nWhat is the bitcoin price now?",
    ],
)
def test_the_new_phrasings_do_not_open_world_questions(question):
    assert not memory_semantic.refers_to_past_conversation(question)


_COMPARISON = (
    "## Comparison\n\n"
    "| Criteria | Option A | Option B |\n|---|---|---|\n"
    "| **Cost** | Low | High (we recommend budgeting for it) |\n"
    "| **Hiring** | Easy | Hard |\n\n"
    + "- Consider replication, backups, failover and the hiring pool (e.g. in Pune). " * 12
    + "\n\n"
)
_CLOSE = "\n\n" + "Next steps: plan the migration, set up monitoring and train the team. " * 5


@pytest.mark.parametrize(
    "conclusion,decision",
    [
        # the choice on the line after a labelled heading
        ("### Final Verdict\n**Use SQS FIFO.** It is the simplest option to run.", "**Use SQS FIFO."),
        # the choice in the heading itself; a dotted name is not a sentence end
        ("### Final Recommendation: **Next.js (React)**\n\n**Why Next.js?**", "Final Recommendation: **Next.js (React)**"),
        # a bold imperative straight after a table
        ("| **Fairness** | Low | **High** |\n\n**Adopt the Hybrid Model.** It fits the cost curve.", "**Adopt the Hybrid Model."),
        # plain prose
        ("All things considered, my recommendation is to go with Postgres for the ledger.", "my recommendation is to go with Postgres for the ledger."),
        # the alternatives listed after the choice are not the choice
        (
            "### Recommendation: **GitHub Actions**\n\nIt is closest to the code.\n\n"
            "- **Choose GitLab CI** if you need built-in compliance reports.\n"
            "- **Choose CircleCI** if you have more than fifty developers.",
            "Recommendation: **GitHub Actions**",
        ),
        # a numbered bold imperative
        ("1. **Use SQS FIFO.** It is the simplest option to run.", "**Use SQS FIFO."),
        # a labelled heading whose next line states the choice in prose
        (
            "### Our Recommendation\nPostgres is the right fit for the ledger, given your team. "
            "It also keeps the reporting stack unchanged.",
            "Our Recommendation Postgres is the right fit for the ledger, given your team.",
        ),
        # a dotted name inside the statement is not where it ends
        ("**Use Next.js for the console.** It reuses the design system.", "**Use Next.js for the console."),
    ],
)
def test_a_long_markdown_answer_keeps_the_statement_of_its_choice(conclusion, decision):
    """The main model's six live decision answers (6,770-9,664 characters)
    put the choice 5,434-9,413 characters in, in these shapes; cut to the
    opening and the close, 0/6 were recalled, and 6/6 with the statements.
    Table rows compare options and are never taken for the choice."""
    answer = "Here is a detailed comparison of the options for your team. " * 5 + _COMPARISON + conclusion + _CLOSE
    snippet = memory_semantic._answer_snippet(answer)
    assert decision in snippet
    assert "we recommend budgeting" not in snippet
    assert len(snippet) <= 4 * memory_semantic._SNIPPET_CHARS + 3 * len(" … ")
