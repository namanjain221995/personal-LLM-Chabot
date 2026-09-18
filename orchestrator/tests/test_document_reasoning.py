"""A document is a SOURCE, not a cage (2026-09-17).

The owner attached a 4-page Vertiv SmartRow brochure and asked, in his own
words, "as I have dgx spark ?? is help Full ??". The platform answered that the
document does not mention DGX Spark and told him to check with NVIDIA and
Vertiv. ChatGPT, given the same PDF and the same question, answered the
question: useful, but overkill for two Sparks and relevant at ten or twenty —
with the brochure's own kW figures quoted.

The cause was the document engine's system prompt: a pure extractor persona
("a careful document analyst ... Answer using what is actually in the
document"). These tests hold the new prompt (app/engines/source_use.py) and
both directions of its switch: a decision question gets a recommendation, an
invoice question stays strictly inside the document.
"""
import asyncio
import base64
import itertools

import pytest

from app.engines import source_use
from app.engines.document import run_pdf_engine_multi
from tests.document_answer_grader import (cites_document, gives_recommendation,
                                          opens_with_refusal, referral_only,
                                          source_named_headings,
                                          source_named_labels)

#: The owner's question, exactly as he typed it (conversation
#: b8b9202e-9966-40c5-8e3c-e3a8c8c881eb, 2026-09-17, Fast mode).
OWNER_QUESTION = "as I have dgx spark ?? is help Full ??"

#: His follow-up, one turn later.
OWNER_FOLLOWUP = "I want to place dgx spark on their rack for cooling"

#: Brochure-shaped text: the SmartRow product, its kW figures, and — like the
#: real document — not one word about DGX, Spark or NVIDIA.
BROCHURE = """\
Vertiv SmartRow Infrastructure Solution

A self-contained, row-based data centre in a single enclosure. Configurations
from 2 to 12 racks, with integrated row cooling, UPS power and monitoring.

Cooling: 2 x 10 kW row-based cooling units for the compact configuration;
larger rows are served by 4 x 45 kW units.
Power: integrated UPS from 10 kVA to 135 kVA, with maintenance bypass.
Environment: sealed aisle containment, fire suppression, access control and
environmental monitoring built into the enclosure.
"""

#: Figures a grounded answer can only have got from the brochure.
BROCHURE_FIGURES = ["10 kW", "45 kW", "135 kVA", "12 racks"]

#: What this platform actually answered (abridged to its load-bearing lines).
THE_BAD_ANSWER = (
    "The short answer is no. The Vertiv SmartRow document you shared does not "
    "mention NVIDIA DGX Spark or provide specific support for it.\n\n"
    "No DGX Mention: I searched the provided text, and there is no mention of "
    "DGX, Spark, or NVIDIA in the document.\n\n"
    "What you should do: Check NVIDIA Documentation for the DGX Spark's rack "
    "requirements. Consult Vertiv about compatibility."
)

#: What ChatGPT answered, same PDF, same question (abridged the same way).
THE_GOOD_ANSWER = (
    "Yes — this Vertiv SmartRow can be useful for your DGX Spark setup, but it "
    "is mainly data-centre infrastructure around the DGX Sparks, not something "
    "that makes the DGX Spark itself faster. For your current 2 x DGX Spark "
    "setup it would be somewhat overkill unless you are planning to expand. "
    "The brochure's cooling is sized at 2 x 10 kW and 4 x 45 kW, far above what "
    "two Sparks draw. Where it becomes relevant is your planned 10-20 DGX Spark "
    "cluster. The brochure does not mention DGX Spark, so the rack fit is my "
    "own estimate; the DGX Spark datasheet would settle the depth."
)


# ---------------------------------------------------------------------------
# The prompt itself
# ---------------------------------------------------------------------------


def test_the_extractor_cage_is_gone():
    system = source_use.system_text(OWNER_QUESTION)
    assert "careful document analyst" not in system
    assert "Answer using what is actually in the document" not in system


def test_the_prompt_names_all_three_sources_and_labels_them():
    """All three sources are still described — in prose, not as three labels.

    The names used to be three shouty headings in the prompt, and the model
    mirrored them into the answer (see the test below). They are described
    instead, so the answer has no template to copy.
    """
    system = source_use.system_text(OWNER_QUESTION)
    assert "SOURCE, not a limit" in system
    assert "what the document itself says" in system
    assert "where the document is silent, answer from your own knowledge" in system
    assert "what this conversation already tells you about the person" in system
    assert "which part is which" in system


def test_the_three_sources_are_not_an_answer_template():
    """The model copies the prompt's SHAPE, so the shape had to go.

    The first cut named the three sources in capitals and then banned those
    exact strings as headings. Live it did not hold: one graded run of the
    owner's turn came back as "### 1. The Document's Limitations", "### 2.
    General Knowledge: DGX Spark Requirements", "### 3. This Conversation:
    Your Scale", and another printed the three names in bold verbatim. Now the
    prompt carries no such labels at all, plus a positive rule and a ban that
    does not depend on capitalisation. tests/test_live_document_reasoning.py
    checks the ANSWER; this checks the instrument.
    """
    for question in (OWNER_QUESTION, "extract the invoice total", "summarize this"):
        system = source_use.system_text(question)
        assert "WHERE THE ANSWER COMES FROM, NOT HOW IT IS LAID OUT" in system
        assert "Lay the answer out by the QUESTION, never by source" in system
        assert "Every heading and every bold label names the SUBJECT underneath it" in system
        # A flat ban did not hold live; a check the model can run on each
        # heading as it writes it did.
        assert "CHECK EVERY HEADING AND EVERY BOLD LABEL" in system
        # The model routed around a heading-only rule: with the ### headings
        # clean it split each section into "**The Document's Figures:**" and
        # "**My Knowledge (DGX Spark Power):**" instead.
        assert "including a label inside a section" in system
        assert "in any form or capitalisation" in system
        assert "No section exists to report what a source holds or lacks" in system
        # And the rule quotes no BAD heading. Naming one seeded it: the prompt
        # that spelled out the rewrite for "The Document's Limits (Capacity)"
        # got "**The Document's Limits:**" back twice in the next live answer.
        # Every example in the prompt is of a good heading.
        assert "The Document" not in system
        assert "Document's" not in system
        assert "Where a fact came from is said in the sentence that uses it" in system
        assert "never as a heading" in system
        # Nothing left in the prompt for the model to mirror. What mirrored was
        # the LABEL form the old prompt used — "- THE DOCUMENT: its own figures
        # ..." — and the answers copied it as "### 1. The Document's
        # Limitations". Two of the three names are gone from the prompt
        # outright; "the document" survives only as ordinary words inside the
        # no-fabrication rule, never as a label.
        for label in ("GENERAL KNOWLEDGE", "THIS CONVERSATION"):
            assert label not in system, f"{label} is still a template in the prompt"
        for label in ("THE DOCUMENT", "GENERAL KNOWLEDGE", "THIS CONVERSATION"):
            assert f"- {label}" not in system, f"{label} is still a bullet label"
            assert f"{label}:" not in system, f"{label} is still a heading label"
            assert f"**{label}**" not in system


def test_a_silent_document_is_one_line_AFTER_the_answer():
    """The line is required; its POSITION is the owner's whole complaint.

    The first cut said "say so in ONE opening line, then answer anyway", and
    live the model did exactly that: three graded runs of the owner's turn all
    opened "**The document does not mention NVIDIA DGX Spark.**", which is
    opens_with_refusal() in tests/document_answer_grader.py and the shape this
    round exists to kill. The answer comes first; the line comes after it.
    """
    system = source_use.system_text(OWNER_QUESTION)
    assert "DO NOT OPEN WITH THAT" in system
    assert "Give your answer first" in system
    assert "in ONE line after it" in system
    assert "never as the opening sentence" in system
    assert "answer anyway" in system
    assert "never the whole answer" in system
    assert "never the first thing you say" in system
    assert "what would settle it" in system
    # and the old instruction that produced the refusal opening is gone
    assert "ONE opening line" not in system


def test_the_prompt_still_forbids_inventing_document_values():
    """The fix must not buy advice with fabrication."""
    for question in (OWNER_QUESTION, "extract the invoice total", "summarize this"):
        system = source_use.system_text(question)
        assert "never invent a value" in system
        assert 'Say "the document says" only for what it really says' in system


def test_structure_rules_ride_on_every_document_answer():
    for question in (OWNER_QUESTION, "extract the invoice total", "summarize this"):
        system = source_use.system_text(question)
        assert "markdown headings" in system
        assert "**bold labels**" in system
        assert "Do not restate or summarize the document before answering" in system
        # Said again in the FORMAT block, which is the last thing the model
        # reads and where a layout rule actually gets applied.
        assert "never the source the fact came from" in system
        # Said last as well as in BASE: a layout rule is obeyed best where the
        # layout rules are, and this one had to be measured four times.
        assert "rewrite it as the thing it is about before you write it" in system


# ---------------------------------------------------------------------------
# The switch, both directions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        OWNER_QUESTION,
        OWNER_FOLLOWUP,
        "is this helpful for me?",
        "should I use this for my cluster?",
        "can I place my two servers in it?",
        "do I need this?",
        "is it worth it for 10 nodes?",
        "what do you recommend for my setup?",
    ],
)
def test_decision_questions_get_the_advisory_block(question):
    assert source_use.question_mode(question) == "advise", question
    system = source_use.system_text(question)
    assert "DECISION QUESTION" in system
    assert "clear yes / no / it depends" in system
    assert "never the answer itself" in system  # the vendor referral
    assert "EXTRACTION QUESTION" not in system
    # Three graded live runs came back "the brochure does not provide full help":
    # a verdict about the PAPER, which is the refusal in a verdict's clothes.
    assert "ABOUT THE THING, NOT THE PAPERWORK" in system
    assert "they mean the thing the document describes" in system
    assert "Judge the thing." in system
    # A ban alone kept losing to the model's instinct to tour its sources, so
    # the decision answer gets a skeleton of its own. Live, one run came back
    # as "### 1. What the Document Says", "### 2. What I Know (General
    # Contract & Business Context)", "### 3. What This Conversation Tells Me
    # About You" — the three sources rebuilt out of words no ban had named.
    assert "SHAPE OF THIS ANSWER" in system
    assert "each under a heading naming THAT THING" in system
    assert "never a tour of your sources" in system
    assert "the answer laid out backwards" in system


@pytest.mark.parametrize(
    "question",
    [
        "extract the line items from this invoice",
        "what is the invoice total?",
        "what is the due date and the PO number?",
        "fill in the form fields",
        "list all the parties named in the contract",
        "transcribe the table verbatim",
    ],
)
def test_field_questions_stay_strictly_inside_the_document(question):
    assert source_use.question_mode(question) == "extract", question
    system = source_use.system_text(question)
    assert "EXTRACTION QUESTION" in system
    assert "Return ONLY what is actually in the document" in system
    assert "do not add advice" in system
    assert "not stated in the document" in system
    assert "DECISION QUESTION" not in system


def test_an_explicit_judgement_rides_AFTER_the_fields():
    """"...and tell me whether I should renew" asks for both.

    The first cut answered it as a pure decision question, which loses the
    fields. The rule is: extraction wins for the fields, advice may follow
    after them, clearly separated — so both blocks ride, extraction first.
    """
    q = "extract the payment terms and tell me whether I should renew"
    assert source_use.question_mode(q) == "extract+advise"
    system = source_use.system_text(q)
    assert "EXTRACTION QUESTION" in system
    assert "ALSO ASKED FOR A JUDGEMENT" in system
    assert system.index("EXTRACTION QUESTION") < system.index("ALSO ASKED FOR A JUDGEMENT")
    assert "Answer in two parts, in this order" in system
    # The advice half may not reach back into the fields.
    assert "may change, fill in or round a field in the first part" in system


@pytest.mark.parametrize(
    "question",
    ["summarize this document", "what does section 3 say?", "how many pages is this?"],
)
def test_neutral_questions_get_neither_block(question):
    assert source_use.question_mode(question) == ""
    system = source_use.system_text(question)
    assert "DECISION QUESTION" not in system
    assert "EXTRACTION QUESTION" not in system


# ---------------------------------------------------------------------------
# The owner's turn, end to end through the engine
# ---------------------------------------------------------------------------


class Rec:
    def __init__(self):
        self.events = []

    async def emit(self, e, d):
        self.events.append((e, d))


@pytest.fixture()
def prompt_recorder(monkeypatch):
    """Captures the messages the engine builds; answers nothing of substance."""
    from app import llm

    seen: dict = {}

    async def fake_stream(msgs, **kw):
        seen["messages"] = msgs
        seen["kwargs"] = kw
        yield "token", "ok"

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    return seen


def _run_owner_turn(recorder, question=OWNER_QUESTION, history=()):
    b64 = base64.b64encode(BROCHURE.encode()).decode()
    asyncio.run(
        run_pdf_engine_multi(
            question,
            [("vertiv-smartrow.pdf", b64)],
            list(history),
            Rec().emit,
            effort="fast",
        )
    )
    return recorder["messages"]


def test_the_owner_turn_builds_an_advisory_prompt(prompt_recorder):
    messages = _run_owner_turn(prompt_recorder)
    system = messages[0]["content"]
    assert messages[0]["role"] == "system"
    assert "DECISION QUESTION" in system
    assert "SOURCE, not a limit" in system
    assert "careful document analyst" not in system
    # The brochure still reaches the prompt, in full, as before.
    user = messages[-1]["content"]
    text = " ".join(p["text"] for p in user if p.get("type") == "text")
    assert "45 kW" in text and "135 kVA" in text
    assert OWNER_QUESTION in text


def test_what_the_person_already_said_is_in_the_prompt(prompt_recorder):
    """(c) of the rule: the conversation's own context. It was always passed;
    the prompt now says to USE it."""
    history = [
        {"role": "user", "content": "I have 2 DGX Sparks and plan to grow to 20."},
        {"role": "assistant", "content": "Noted — a 20-node Spark cluster."},
    ]
    messages = _run_owner_turn(prompt_recorder, history=history)
    turns = " ".join(
        str(m.get("content")) for m in messages if m.get("role") != "system"
    )
    assert "2 DGX Sparks" in turns
    assert "what this conversation already tells you about the person" in messages[0]["content"]
    assert "context you must use" in messages[0]["content"]


def test_the_follow_up_turn_keeps_the_rule_once_the_document_is_history():
    """The next turn routed to chat, where the document survives only as the
    pinned block main.py builds. The same rule is exported for it."""
    block = source_use.HISTORY_DOC_GUIDANCE
    assert "SOURCE, not a limit" in block
    assert "real recommendation" in block
    assert "not a referral to the vendor" in block
    assert "never claim they say something they do not" in block


def test_an_invoice_upload_never_carries_the_advisory_block(prompt_recorder):
    b64 = base64.b64encode(
        b"INVOICE 42\nDate: 2026-08-01\nTotal: 1,250.00 USD\nDue: 2026-09-01"
    ).decode()
    asyncio.run(
        run_pdf_engine_multi(
            "what is the invoice total and the due date?",
            [("invoice.pdf", b64)],
            [],
            Rec().emit,
            effort="fast",
        )
    )
    system = prompt_recorder["messages"][0]["content"]
    assert "EXTRACTION QUESTION" in system
    assert "DECISION QUESTION" not in system


# ---------------------------------------------------------------------------
# The graders, pinned against the two real answers
# ---------------------------------------------------------------------------


def test_the_graders_reject_the_answer_this_round_fixes():
    assert opens_with_refusal(THE_BAD_ANSWER)
    assert not gives_recommendation(THE_BAD_ANSWER)
    assert not cites_document(THE_BAD_ANSWER, BROCHURE_FIGURES)
    assert referral_only(THE_BAD_ANSWER)


def test_the_graders_accept_an_answer_that_answers_the_question():
    assert not opens_with_refusal(THE_GOOD_ANSWER)
    assert gives_recommendation(THE_GOOD_ANSWER)
    assert cites_document(THE_GOOD_ANSWER, BROCHURE_FIGURES)
    assert not referral_only(THE_GOOD_ANSWER)


#: Verbatim opening of A_owner_vertiv run 5, 2026-09-18, live. The verdict is
#: right, the sentence is about the PRODUCT, and the grader scored it a
#: refusal — which would have failed the live test for being correct.
_A_VERDICT_THAT_READS_LIKE_A_REFUSAL = (
    "**Recommendation: No, the Vertiv SmartRow is not a full solution for your "
    "20-node DGX Spark cluster.**\n\n"
    "While the SmartRow provides the physical infrastructure (power, cooling, "
    "containment), it does not include the compute hardware, networking, or "
    "software stack required to run DGX Sparks. Furthermore, based on the power "
    "specifications provided in the document, the standard SmartRow configurations "
    "are likely insufficient for a full 20-node cluster."
)


def test_a_verdict_about_the_product_is_not_a_refusal_about_the_paper():
    """The subject decides. "does not include" is the refusal when its subject
    is the paper and a verdict when its subject is the product."""
    assert not opens_with_refusal(_A_VERDICT_THAT_READS_LIKE_A_REFUSAL)
    assert gives_recommendation(_A_VERDICT_THAT_READS_LIKE_A_REFUSAL)
    # ... and the refusals it must still catch, including the one the second
    # recheck reproduced live on the delivered patch.
    assert opens_with_refusal(
        "**Not stated in the document.**\n\nThe provided Master Services "
        "Agreement excerpt contains specific commercial and legal terms, but it "
        "does not contain any information regarding the long-term strategic fit."
    )
    assert opens_with_refusal(
        "The document does not mention NVIDIA DGX Spark. Check with the vendor."
    )
    assert opens_with_refusal(
        "The brochure you shared does not cover DGX Spark compatibility."
    )


@pytest.mark.parametrize(
    "opening",
    [
        # Verbatim first lines of live runs from 2026-09-18 that the grader
        # scored as giving no recommendation, although each opens with one.
        "**Verdict: No, this is not the right long-term choice.**",
        "**Short Answer: No, this is not the right choice for the long term.**",
        "**Recommendation: No, the Vertiv SmartRow is not a full solution.**",
        "Bottom line: this is the wrong product for two nodes.",
        "I would not renew on these terms.",
        "This would be a poor choice at your scale.",
    ],
)
def test_the_grader_can_see_a_verdict_that_is_not_the_word_recommend(opening):
    """A verdict the grader cannot see is a live test that fails for being
    right. None of these forms may rescue the answer this round fixes."""
    assert gives_recommendation(opening), opening


def test_widening_the_recommendation_grader_did_not_rescue_the_bad_answer():
    """The constraint every addition above is written under."""
    assert not gives_recommendation(THE_BAD_ANSWER)
    assert referral_only(THE_BAD_ANSWER)
    # "The short answer is no" is the bad answer's own opening and stays out.
    assert not gives_recommendation("The short answer is no.")


def test_a_refusal_is_not_assembled_out_of_two_innocent_sentences():
    """Sentence by sentence: a verdict in one and the word "document" in the
    next must not add up to a refusal neither of them made."""
    assert not opens_with_refusal(
        "The SmartRow does not include the compute hardware. "
        "The document lists 2 to 12 racks, which covers your 20 nodes."
    )


def test_saying_the_document_is_silent_is_allowed_after_the_answer():
    """Rule 2: the sentence is required, as a line, not as the answer."""
    assert "does not mention DGX Spark" in THE_GOOD_ANSWER
    assert not opens_with_refusal(THE_GOOD_ANSWER)


# ---------------------------------------------------------------------------
# What QA found on the first cut (2026-09-18), case by case
#
# Every question below was measured wrong by QA against the delivered patch
# and is now pinned. The two BLOCKERs cost real answers: the invoice question
# routed to ADVISE and the live answer invented "**Tax Amount:** 0.00 USD" for
# an invoice with no tax line, and the contract question routed to NEUTRAL, so
# BASE's general-knowledge permission produced speculation about New York and
# England & Wales as the governing law.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        # BLOCKER 1, QA's exact wording.
        "I need the total from this invoice and the tax amount",
        "I need to know the total",
        "what is the total on this invoice?",
        "give me the vendor name and the amount",
        "how much do we owe and when is it due?",
        "pull the numbers out of this invoice for our books",
        "copy the table on page 3",
        "what is the effective date and the annual fee?",
    ],
)
def test_a_polite_field_ask_is_extraction_not_advice(question):
    """"I need the total" names a FIELD. First person does not make it advice."""
    assert source_use.question_mode(question) == "extract", question
    system = source_use.system_text(question)
    assert "EXTRACTION QUESTION" in system
    assert "DECISION QUESTION" not in system


@pytest.mark.parametrize(
    "question",
    [
        # BLOCKER 2, QA's exact wording.
        "Who are the parties to this contract, what is the term, and what is the governing law?",
        "what's the termination notice period in this contract?",
        "what are the parties and the fees?",
    ],
)
def test_contract_fields_are_extraction_not_neutral(question):
    """A contract field question must not fall through to BASE, whose
    general-knowledge permission then guesses the governing law."""
    assert source_use.question_mode(question) == "extract", question
    system = source_use.system_text(question)
    assert "EXTRACTION QUESTION" in system
    assert "DECISION QUESTION" not in system


def test_the_extraction_block_overrides_the_general_knowledge_permission():
    """BASE grants general knowledge where the document is silent; extraction
    must take that grant away, not merely disagree with it."""
    system = source_use.system_text("what is the governing law and the term?")
    assert "OVERRIDES the general-knowledge permission above" in system
    assert "the document is the only source" in system
    assert "do not infer it" in system
    assert "no thinking out loud, no self-correction in the answer" in system


@pytest.mark.parametrize(
    "question",
    [
        OWNER_QUESTION,                                  # advise
        "what is the invoice total?",                    # extract
        "extract the total and tell me if I should renew",  # extract+advise
        "summarize this document",                       # neutral
    ],
)
def test_no_mode_may_invent_a_document_value(question):
    """The 0.00 USD tax line QA caught: a field the document does not state is
    "not stated in the document", in EVERY mode."""
    system = source_use.system_text(question)
    assert "NEVER PRESENT A VALUE AS THE DOCUMENT'S WHEN THE DOCUMENT DOES NOT GIVE IT" in system
    assert 'is "not stated in the document"' in system
    assert 'never 0, 0.00, "none" and never a typical value' in system
    assert "This binds every mode" in system
    # ... and it must not gag the advisory half, which is the whole round.
    assert "a figure from your own knowledge is welcome" in system


@pytest.mark.parametrize(
    "question",
    [
        "what cooling capacity does this product offer?",
        "what does clause 11 say?",
        "summarize this document",
    ],
)
def test_a_fully_answered_question_gets_no_unasked_section(question):
    """MEDIUM: "what cooling capacity does this product offer?" was answered
    correctly and then padded with a "Context for Your Setup" section and a
    heat-load calculation nobody asked for (303 chars on dev, 1,030 here)."""
    assert source_use.question_mode(question) == ""
    system = source_use.system_text(question)
    assert "ANSWER THE QUESTION AND STOP" in system
    assert '"context for your setup"' in system
    assert "no next steps, no offer of further analysis" in system
    assert "do not pad it with general knowledge" in system
    assert "DECISION QUESTION" not in system
    assert "EXTRACTION QUESTION" not in system


@pytest.mark.parametrize(
    "question",
    ["what supply water temperature should I run for this unit?", "summarize this"],
)
def test_stopping_does_not_gag_a_contradiction(question):
    """A commissioning sheet that says 45 C supply water contradicts the 7-18 C
    everyone expects. The document still wins, and the gap is said out loud —
    that is part of the answer, not the padding the rule above bans."""
    assert source_use.question_mode(question) == ""
    system = source_use.system_text(question)
    assert "ONE more exception" in system
    assert "contradicts what is normally true" in system
    assert "what the usual figure is" in system


@pytest.mark.parametrize(
    "question",
    ["is this worth buying", "is that worth the money", "is it worth buying"],
)
def test_worth_buying_is_a_decision_question(question):
    """LOW: _EXPLICIT_ADVICE_RE covered "is it worth" and "worth it" but not
    "is this worth buying" — and there is no pronoun for the fallback."""
    assert source_use.question_mode(question) == "advise", question


@pytest.mark.parametrize(
    "question",
    [
        "what supply water temperature should I run for this unit?",
        "what pressure should we hold the loop at?",
    ],
)
def test_a_value_question_that_contains_should_i_is_not_a_decision(question):
    """"What X should I run" wants the number the document gives. The
    advisory block's "clear yes / no / it depends" is the wrong shape for it."""
    assert source_use.question_mode(question) == "", question


@pytest.mark.parametrize(
    "question",
    ["should I use this for my cluster?", "what model should we buy?"],
)
def test_should_i_is_still_a_decision_when_it_asks_for_one(question):
    assert source_use.question_mode(question) == "advise", question


@pytest.mark.parametrize(
    "question",
    ["give me a summary of the key points I need", "I need a summary"],
)
def test_the_word_need_alone_is_not_a_decision(question):
    """The fallback used to carry need/use/run/plan/good/right, so almost any
    first-person sentence became advice."""
    assert source_use.question_mode(question) == "", question


# ---------------------------------------------------------------------------
# The battery: every question QA measured, in one table.
# Delivered patch: 14 of these 30 landed in the wrong mode. Now: 0.
# ---------------------------------------------------------------------------

BATTERY = [
    ("extract", "extract the invoice number, the total, the tax amount, the PO number and the payment terms"),
    ("extract", "I need the total from this invoice and the tax amount"),
    ("extract", "Who are the parties to this contract, what is the term, and what is the governing law?"),
    ("extract", "fill in the form fields: vendor, issue date, due date, currency, discount"),
    ("extract", "what is the total on this invoice?"),
    ("extract", "give me the vendor name and the amount"),
    ("extract", "I want the line items"),
    ("extract", "what's the termination notice period in this contract?"),
    ("extract", "how much do we owe and when is it due?"),
    ("extract", "what is the effective date and the annual fee?"),
    ("extract", "I need to know the total"),
    ("extract", "pull the numbers out of this invoice for our books"),
    ("extract", "what are the parties and the fees?"),
    ("extract", "copy the table on page 3"),
    ("advise", "as I have dgx spark ?? is help Full ??"),
    ("advise", "I want to place dgx spark on their rack for cooling"),
    ("advise", "will this work for my setup? I run 12 servers with 25 GbE NICs"),
    ("advise", "am I allowed to put our customer database on a US cloud region?"),
    ("advise", "should we use this method? we serve a 35B MoE at 1M context"),
    ("advise", "is this worth buying"),
    ("advise", "do I need this for 20 nodes"),
    ("advise", "would you recommend this"),
    ("advise", "can I place my servers in this?"),
    ("", "summarize this document"),
    ("", "what is this document about?"),
    ("", "translate page 2 to English"),
    ("", "what cooling capacity does this product offer?"),
    ("", "what supply water temperature should I run for this unit?"),
    ("", "give me a summary of the key points I need"),
    ("", "what does clause 11 say?"),
]


def test_the_thirty_question_battery_has_no_mismatch():
    wrong = [
        (q, want, source_use.question_mode(q))
        for want, q in BATTERY
        if source_use.question_mode(q) != want
    ]
    assert not wrong, "\n".join(
        f"{got or 'neutral'!r} want {want or 'neutral'!r}: {q}" for q, want, got in wrong
    )


# ---------------------------------------------------------------------------
# The two blockers, end to end through the engine
# ---------------------------------------------------------------------------

#: An invoice with NO tax line, NO PO and NO payment terms — the fixture that
#: made the "**Tax Amount:** 0.00 USD" fabrication visible.
INVOICE_WITHOUT_TAX = """\
INVOICE

Invoice number: INV-2026-0912
Vendor: Acme Racks Ltd, 14 Mill Road, Leeds
Date of issue: 2026-08-01
Line 1: Rack enclosure 42U, qty 2, 1,000.00 USD
Line 2: Row cooling unit 10 kW, qty 1, 4,200.00 USD
Total: 5,200.00 USD
Due: 2026-09-01
"""

CONTRACT = """\
MASTER SERVICES AGREEMENT

This Agreement is made between Northwind Group Ltd ("Customer") and
Acme Racks Ltd ("Supplier").
Clause 2. Term. The initial term is 24 months from the Effective Date of
    2026-03-15.
Clause 5. Fees. 18,000 USD per year, invoiced annually in advance.
Clause 11. Termination for convenience. Either party may terminate on
    90 days written notice.
"""


def _system_for_upload(recorder, question, body, name="doc.pdf"):
    b64 = base64.b64encode(body.encode()).decode()
    asyncio.run(
        run_pdf_engine_multi(question, [(name, b64)], [], Rec().emit, effort="fast")
    )
    return recorder["messages"][0]["content"]


def test_the_misrouted_invoice_turn_builds_a_strict_prompt(prompt_recorder):
    """BLOCKER 1 end to end: QA's exact question against QA's exact invoice."""
    system = _system_for_upload(
        prompt_recorder,
        "I need the total from this invoice and the tax amount",
        INVOICE_WITHOUT_TAX,
        "invoice.pdf",
    )
    assert "EXTRACTION QUESTION" in system
    assert "DECISION QUESTION" not in system
    assert "do not add advice" in system
    assert 'write "not stated in the document"' in system


def test_the_contract_turn_builds_a_strict_prompt(prompt_recorder):
    """BLOCKER 2 end to end: no general-knowledge permission on a contract."""
    system = _system_for_upload(
        prompt_recorder,
        "Who are the parties to this contract, what is the term, and what is the governing law?",
        CONTRACT,
        "msa.pdf",
    )
    assert "EXTRACTION QUESTION" in system
    assert "DECISION QUESTION" not in system
    assert "OVERRIDES the general-knowledge permission above" in system


# ---------------------------------------------------------------------------
# THE MODE BATTERY (2026-09-18)
#
# 204 questions, each labelled with the mode a careful person would choose,
# pinned so the switch cannot drift. It exists because the second recheck
# found the classifier reproducing the original incident on a phrasing nobody
# had written down: "is this the right choice for us in the long term?" took
# the strict extraction block, because the word "term" inside "long term"
# matched the contract field. Every trap that found is a row here.
#
# The labels follow the rule the round is built on: strict extraction only on
# high-precision evidence, and ANY ask for a judgement makes the answer
# advisory (extract+advise when fields are named too), never extraction alone.
#
# Covered on purpose: contract and invoice field asks, blunt and polite;
# decision questions with and without a first-person pronoun; "long term",
# "short term", "in terms of", "the whole party", "the total picture", "the
# amount of heat"; mixed asks; plain factual questions the document answers;
# summaries.
# ---------------------------------------------------------------------------

MODE_BATTERY = [
    ('extract', 'extract the invoice number, the total, the tax amount, the PO number and the payment terms', 'invoice-blunt'),
    ('extract', 'what is the invoice total?', 'invoice-blunt'),
    ('extract', 'invoice total?', 'invoice-blunt'),
    ('extract', 'total and due date', 'invoice-blunt'),
    ('extract', 'what is the grand total?', 'invoice-blunt'),
    ('extract', "what's the subtotal?", 'invoice-blunt'),
    ('extract', 'what is the tax amount?', 'invoice-blunt'),
    ('extract', 'list the line items', 'invoice-blunt'),
    ('extract', 'list all the line items with quantities', 'invoice-blunt'),
    ('extract', 'what is the due date?', 'invoice-blunt'),
    ('extract', 'who is the vendor?', 'invoice-blunt'),
    ('extract', 'what currency is the invoice in?', 'invoice-blunt'),
    ('extract', 'pull the numbers out of this invoice for our books', 'invoice-blunt'),
    ('extract', 'copy the table on page 3', 'invoice-blunt'),
    ('extract', 'give me the vendor name and the amount', 'invoice-blunt'),
    ('extract', 'what is the PO number?', 'invoice-blunt'),
    ('extract', "what's the payment terms on this invoice?", 'invoice-blunt'),
    ('extract', 'transcribe the address block at the top', 'invoice-blunt'),
    ('extract', 'read out the serial number on page 2', 'invoice-blunt'),
    ('extract', 'what is the amount due?', 'invoice-blunt'),
    ('extract', 'balance due?', 'invoice-blunt'),
    ('extract', 'how much do we owe and when is it due?', 'invoice-blunt'),
    ('extract', 'what is the unit price of the cooling unit?', 'invoice-blunt'),
    ('extract', 'quantity on line 2?', 'invoice-blunt'),
    ('extract', 'what is the discount?', 'invoice-blunt'),
    ('extract', 'is there a PO number on this invoice?', 'invoice-blunt'),
    ('extract', 'date of issue?', 'invoice-blunt'),
    ('extract', 'what is the date on this invoice?', 'invoice-blunt'),
    ('extract', 'what does the invoice say the total is?', 'invoice-blunt'),
    ('extract', 'extract the line items from this invoice', 'invoice-blunt'),
    ('extract', 'could you please tell me the grand total and the due date?', 'invoice-polite'),
    ('extract', 'I need the total from this invoice and the tax amount', 'invoice-polite'),
    ('extract', 'I need to know the total', 'invoice-polite'),
    ('extract', 'would you mind giving me the invoice number and the vendor name?', 'invoice-polite'),
    ('extract', 'can you give me the totals for each line?', 'invoice-polite'),
    ('extract', 'I want the line items', 'invoice-polite'),
    ('extract', 'if you could pull the payment terms that would be great', 'invoice-polite'),
    ('extract', 'just the total please', 'invoice-polite'),
    ('extract', 'send me the totals for both invoices', 'invoice-polite'),
    ('extract', 'confirm the due date for me', 'invoice-polite'),
    ('extract', 'kindly provide the effective date and the annual fee', 'invoice-polite'),
    ('extract', 'fees and dates please', 'invoice-polite'),
    ('extract', 'total?', 'invoice-polite'),
    ('extract', 'does the datasheet give a price?', 'invoice-polite'),
    ('extract', 'what is the effective date and the annual fee?', 'invoice-polite'),
    ('extract', 'Who are the parties to this contract, what is the term, and what is the governing law?', 'contract'),
    ('extract', 'what is the governing law?', 'contract'),
    ('extract', 'what is the term of this contract?', 'contract'),
    ('extract', "what's the contract term?", 'contract'),
    ('extract', 'how long is the initial term?', 'contract'),
    ('extract', "what's the termination notice period in this contract?", 'contract'),
    ('extract', 'what are the parties and the fees?', 'contract'),
    ('extract', 'who signed this agreement and on what date?', 'contract'),
    ('extract', 'who signed it?', 'contract'),
    ('extract', 'what is the notice period?', 'contract'),
    ('extract', "what's the notice to terminate?", 'contract'),
    ('extract', 'when does this agreement expire?', 'contract'),
    ('extract', 'what is the contract value?', 'contract'),
    ('extract', 'what is the billing address?', 'contract'),
    ('extract', 'what is the interest rate on late payment?', 'contract'),
    ('extract', 'please list the parties to this agreement', 'contract'),
    ('extract', 'list all the parties named in the contract', 'contract'),
    ('extract', 'what is the jurisdiction?', 'contract'),
    ('extract', 'what is the commencement date?', 'contract'),
    ('extract', 'what is the renewal date?', 'contract'),
    ('extract', 'fill in the form fields: vendor, issue date, due date, currency, discount', 'contract'),
    ('extract', 'transcribe the table verbatim', 'contract'),
    ('extract', 'quote the termination clause word for word', 'contract'),
    ('extract', 'what is the annual fee under this agreement?', 'contract'),
    ('extract', 'copy the wording of clause 5', 'contract'),
    ('extract', 'quote the section on data residency', 'contract'),
    ('extract', 'quote clause 11 exactly', 'contract'),
    ('extract', 'i need the po number and the vendor address', 'contract'),
    ('extract', 'can you extract everything?', 'contract'),
    ('extract', 'any chance you can pull the totals?', 'contract'),
    ('extract', "what's the effective date?", 'contract'),
    ('extract', 'what is the policy number?', 'contract'),
    ('extract', 'list the benefits of this product', 'contract'),
    ('advise', 'as I have dgx spark ?? is help Full ??', 'decision-first-person'),
    ('advise', 'I want to place dgx spark on their rack for cooling', 'decision-first-person'),
    ('advise', 'will this work for my setup? I run 12 servers with 25 GbE NICs', 'decision-first-person'),
    ('advise', 'am I allowed to put our customer database on a US cloud region?', 'decision-first-person'),
    ('advise', 'should we use this method? we serve a 35B MoE at 1M context', 'decision-first-person'),
    ('advise', 'do I need this for 20 nodes', 'decision-first-person'),
    ('advise', 'can I place my servers in this?', 'decision-first-person'),
    ('advise', 'is this a good fit for us?', 'decision-first-person'),
    ('advise', 'which model should we buy for a 40 kW row?', 'decision-first-person'),
    ('advise', 'we have 20 DGX Sparks - is this overkill?', 'decision-first-person'),
    ('advise', 'should I renew this contract?', 'decision-first-person'),
    ('advise', 'can we host our GPUs in this enclosure?', 'decision-first-person'),
    ('advise', 'is it worth the money for a small deployment?', 'decision-first-person'),
    ('advise', 'what do you recommend for my setup?', 'decision-first-person'),
    ('advise', 'should I use this for my cluster?', 'decision-first-person'),
    ('advise', 'is this helpful for me?', 'decision-first-person'),
    ('advise', 'is it worth it for 10 nodes?', 'decision-first-person'),
    ('advise', 'do we really need the larger unit?', 'decision-first-person'),
    ('advise', 'would you recommend this', 'decision-first-person'),
    ('advise', "I'm torn between the 10 kW and the 45 kW unit - which should I pick?", 'decision-first-person'),
    ('advise', "i'm thinking of racking these in it", 'decision-first-person'),
    ('advise', "we'd be installing two of them here", 'decision-first-person'),
    ('advise', 'my plan is to host the cluster in one of these', 'decision-first-person'),
    ('advise', 'i was going to buy two', 'decision-first-person'),
    ('advise', 'our options are this or a standard rack', 'decision-first-person'),
    ('advise', 'is this worth buying', 'decision-no-pronoun'),
    ('advise', 'is that worth the money', 'decision-no-pronoun'),
    ('advise', 'does this make sense for a two-node cluster?', 'decision-no-pronoun'),
    ('advise', 'is this suitable for a small office?', 'decision-no-pronoun'),
    ('advise', 'is this overkill for two nodes?', 'decision-no-pronoun'),
    ('advise', 'is this enough cooling for 20 nodes?', 'decision-no-pronoun'),
    ('advise', 'would this be helpful for a 40 kW row?', 'decision-no-pronoun'),
    ('advise', 'does this fit a 600 mm rack?', 'decision-no-pronoun'),
    ('advise', 'is it any good?', 'decision-no-pronoun'),
    ('advise', 'which one is better for a small server room?', 'decision-no-pronoun'),
    ('advise', 'should this be deployed in an office?', 'decision-no-pronoun'),
    ('advise', 'is a SmartRow overkill here?', 'decision-no-pronoun'),
    ('advise', 'can this be used for GPU racks?', 'decision-no-pronoun'),
    ('advise', 'pros and cons?', 'decision-no-pronoun'),
    ('advise', 'is it better than a standard rack?', 'decision-no-pronoun'),
    ('advise', 'is a third party allowed to access the data?', 'decision-no-pronoun'),
    ('advise', 'would this be suitable for an office server room?', 'decision-no-pronoun'),
    ('advise', 'is the price fair for two racks?', 'decision-no-pronoun'),
    ('advise', 'what would you do in my position?', 'decision-first-person'),
    ('advise', 'should we sign this or walk away?', 'decision-first-person'),
    ('advise', 'how does this compare to a standard rack?', 'decision-no-pronoun'),
    ('advise', 'is there anything I should worry about?', 'decision-first-person'),
    ('advise', 'does this cover 20 nodes?', 'decision-no-pronoun'),
    ('advise', 'can it handle 20 nodes?', 'decision-no-pronoun'),
    ('advise', 'is this the right choice for us in the long term?', 'trap-long-term'),
    ('advise', 'will this work for my setup in the long term? I run 12 servers with 25 GbE NICs in a 600 mm deep rack, rear-to-front airflow', 'trap-long-term'),
    ('advise', 'we plan to grow - does this help us in the short term?', 'trap-short-term'),
    ('advise', 'long term, is this a sensible investment?', 'trap-long-term'),
    ('advise', 'in terms of cooling, will this work for my rack?', 'trap-in-terms-of'),
    ('', 'in terms of power, what does this unit draw?', 'trap-in-terms-of'),
    ('', 'what is the total picture here?', 'trap-total-picture'),
    ('advise', 'does the total picture make sense for a 20-node build?', 'trap-total-picture'),
    ('', 'who was at the whole party?', 'trap-whole-party'),
    ('advise', 'the whole party is coming to see the rack - will it fit in the room?', 'trap-whole-party'),
    ('advise', 'the party was a total disaster, does this help?', 'trap-whole-party'),
    ('advise', 'the whole party wants to know: is it worth it?', 'trap-whole-party'),
    ('', 'how many parties attended the launch party?', 'trap-whole-party'),
    ('', 'what rate of airflow does it need over a long period?', 'trap-long-term'),
    ('', 'over the short term the value of the team matters more', 'trap-short-term'),
    ('', 'in the long term, what is the total cost of ownership?', 'trap-long-term'),
    ('', 'up to date figures please', 'trap-total-picture'),
    ('', "in the short run it's fine - long term?", 'trap-short-term'),
    ('advise', 'over a long period, does this hold up?', 'trap-long-term'),
    ('advise', 'the total picture: should we sign?', 'trap-total-picture'),
    ('advise', 'third party access - is it permitted?', 'trap-whole-party'),
    ('advise', 'will this handle the amount of heat two racks produce?', 'trap-amount-of'),
    ('advise', 'is the amount of cooling enough for 20 DGX Sparks?', 'trap-amount-of'),
    ('extract', 'what is the total, in terms of the line items?', 'trap-in-terms-of'),
    ('extract', 'short term this looks fine, but what is the term of the contract?', 'trap-short-term'),
    ('extract+advise', 'extract the payment terms and tell me whether I should renew', 'mixed'),
    ('extract+advise', 'what is the annual fee, and do you recommend we sign?', 'mixed'),
    ('extract+advise', 'give me the total and tell me if it is worth it', 'mixed'),
    ('extract+advise', 'what is the term and the notice period - should we renew?', 'mixed'),
    ('extract+advise', 'list the line items and tell me if we are being overcharged', 'mixed'),
    ('extract+advise', 'what is the total and is it worth it?', 'mixed'),
    ('extract+advise', 'pull the fees out and tell me whether this is good value', 'mixed'),
    ('extract+advise', 'who are the parties, and should we sign with them?', 'mixed'),
    ('extract+advise', 'what is the due date, and do I need to pay early?', 'mixed'),
    ('extract+advise', 'invoice total please, and is that reasonable for two racks?', 'mixed'),
    ('extract+advise', 'list the reasons why I should buy this', 'mixed'),
    ('', 'what cooling capacity does this product offer?', 'factual'),
    ('', 'what does clause 11 say?', 'factual'),
    ('', 'what supply water temperature should I run for this unit?', 'factual'),
    ('', 'what pressure should we hold the loop at?', 'factual'),
    ('', 'what does this document say about fire suppression?', 'factual'),
    ('', 'explain section 3 in plain English', 'factual'),
    ('', 'how many pages is this?', 'factual'),
    ('', 'what is the product name?', 'factual'),
    ('', 'what problem does this paper solve?', 'factual'),
    ('', 'what is this document about?', 'factual'),
    ('', 'translate page 2 to English', 'factual'),
    ('', 'translate the whole thing', 'factual'),
    ('', 'what happens in the event of termination?', 'factual'),
    ('', 'how many racks does the enclosure hold?', 'factual'),
    ('', 'what airflow direction does it use?', 'factual'),
    ('', 'what is the operating temperature range?', 'factual'),
    ('', 'does the document mention DGX Spark?', 'factual'),
    ('', 'what sections does this contract have?', 'factual'),
    ('', 'what does the paper measure at 128k context?', 'factual'),
    ('', 'what UPS capacity is included?', 'factual'),
    ('', 'does it come with monitoring?', 'factual'),
    ('', 'how much will this cost us?', 'factual'),
    ('', 'any hidden fees?', 'factual'),
    ('', "what's the catch?", 'factual'),
    ('', 'what language is this in?', 'factual'),
    ('', 'does the contract auto-renew?', 'factual'),
    ('', 'what is my liability under this agreement?', 'factual'),
    ('', 'read the first paragraph back to me', 'factual'),
    ('', 'what is the total number of racks?', 'factual'),
    ('', 'explain the termination process', 'factual'),
    ('', 'long-term support: is it included?', 'factual'),
    ('', 'summarize this document', 'summary'),
    ('', 'give me a summary of the key points I need', 'summary'),
    ('', 'summarise this in three bullets', 'summary'),
    ('', 'I need a summary', 'summary'),
    ('', 'tl;dr?', 'summary'),
    ('', 'summarize the contract', 'summary'),
    ('', 'summarize the invoice', 'summary'),
    ('', 'give me an executive summary', 'summary'),
    ('', 'what are the main points?', 'summary'),
    ('', 'what is the key takeaway?', 'summary'),
]


@pytest.mark.parametrize("want,question,group", MODE_BATTERY)
def test_the_mode_battery_has_no_mismatch(want, question, group):
    signals = source_use.classify(question)
    assert signals.mode == want, (
        f"[{group}] {question!r}\n"
        f"  want {want or 'neutral'!r}, got {signals.mode or 'neutral'!r}\n"
        f"  field={signals.field_score} decision={signals.decision_score} "
        f"evidence={signals.evidence}"
    )


def test_the_battery_still_covers_every_group_it_was_built_for():
    """The battery may grow. It may not quietly lose the cases that caught the
    regressions — a shrunken battery passes for the wrong reason."""
    from collections import Counter

    groups = Counter(group for _, _, group in MODE_BATTERY)
    modes = Counter(want for want, _, _ in MODE_BATTERY)
    assert len(MODE_BATTERY) >= 120, len(MODE_BATTERY)
    assert len({q for _, q, _ in MODE_BATTERY}) == len(MODE_BATTERY), "duplicate question"
    for group, least in (
        ("invoice-blunt", 20),
        ("invoice-polite", 10),
        ("contract", 20),
        ("decision-first-person", 15),
        ("decision-no-pronoun", 15),
        ("mixed", 8),
        ("factual", 15),
        ("summary", 8),
    ):
        assert groups[group] >= least, f"{group}: {groups[group]} < {least}"
    for trap in ("trap-long-term", "trap-short-term", "trap-in-terms-of",
                 "trap-total-picture", "trap-whole-party", "trap-amount-of"):
        assert groups[trap] >= 1, f"{trap} dropped out of the battery"
    for mode in ("extract", "advise", "extract+advise", ""):
        assert modes[mode] >= 10, f"{mode or 'neutral'}: only {modes[mode]} rows"


# ---------------------------------------------------------------------------
# The second recheck's findings, one test each (2026-09-18)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        # BLOCKER: reproduced live, twice out of two runs, on the CONTRACT
        # fixture. mode was extract, the answer opened "**Not stated in the
        # document.**" and gave no verdict — the owner's complaint verbatim.
        "is this the right choice for us in the long term?",
        "will this work for my setup in the long term?",
        "we plan to grow - does this help us in the short term?",
        "long term, is this a sensible investment?",
        "in terms of cooling, will this work for my rack?",
        "will this handle the amount of heat our racks produce?",
        "is the amount of cooling enough for 20 DGX Sparks?",
    ],
)
def test_ordinary_english_never_routes_a_decision_to_extraction(question):
    """"long term" is not the contract's term, and the cost of confusing them
    is the whole incident. The words are masked out before any field pattern
    runs, so no lookbehind can be missing one."""
    signals = source_use.classify(question)
    assert signals.mode == "advise", (question, signals)
    assert signals.field_score < 2, signals.evidence
    assert "EXTRACTION QUESTION" not in source_use.system_text(question)


@pytest.mark.parametrize(
    "question,expected",
    [
        ("what is the term of this contract?", "extract"),
        ("what's the contract term?", "extract"),
        ("how long is the initial term?", "extract"),
        ("Who are the parties to this contract, what is the term, and what is the governing law?", "extract"),
        ("what is the total on this invoice?", "extract"),
    ],
)
def test_the_mask_is_not_over_applied(question, expected):
    """The fix may not buy safety by losing the real field asks: a term OF A
    CONTRACT and a total ON AN INVOICE are still extraction."""
    assert source_use.question_mode(question) == expected, question


@pytest.mark.parametrize(
    "question",
    [
        # MEDIUM: these were NEUTRAL, and NEUTRAL told the model not to
        # recommend — so a person asking for a recommendation got none.
        "does this make sense for a two-node cluster?",
        "is this suitable for a small office?",
        "is this overkill for two nodes?",
        "is this enough cooling for 20 nodes?",
        "would this be helpful for a 40 kW row?",
        "does this fit a 600 mm rack?",
        "is this worth buying",
        "is it any good?",
        "can this be used for GPU racks?",
        "should this be deployed in an office?",
    ],
)
def test_a_decision_without_a_pronoun_is_still_a_decision(question):
    """A judgement does not stop being a judgement because the person left
    themselves out of the sentence."""
    assert source_use.question_mode(question) == "advise", question
    assert "DECISION QUESTION" in source_use.system_text(question)


def test_neutral_never_forbids_a_recommendation():
    """Rule 3 of the review, made mechanical. NEUTRAL means "answer exactly
    what was asked and stop", never "refuse to judge" — the first cut said
    "no next steps, no recommendation" in so many words, and every question
    the switch failed to recognise as a decision inherited that refusal."""
    system = source_use.system_text("summarize this document")
    assert "no recommendation" not in system
    assert "STOPPING IS NOT REFUSING TO JUDGE" in system
    assert "if the question does turn on one, give it plainly" in system
    assert "Never answer a question about whether something suits this person" in system
    # ... and it still stops the padding it was added for.
    assert "Do not append a section the person did not ask for" in system
    assert "Do not volunteer a verdict nobody asked for" in system


@pytest.mark.parametrize(
    "question",
    [
        # LOW: field asks that used to fall to NEUTRAL, where BASE's
        # general-knowledge permission applies to an extraction task.
        "who signed this agreement and on what date?",
        "who signed it?",
        "what is the contract value?",
        "what is the billing address?",
        "when does this agreement expire?",
        "what is the interest rate on late payment?",
    ],
)
def test_the_remaining_field_asks_reach_the_strict_block(question):
    assert source_use.question_mode(question) == "extract", question
    assert "EXTRACTION QUESTION" in source_use.system_text(question)


@pytest.mark.parametrize(
    "question",
    [
        "extract the payment terms and tell me whether I should renew",
        "what is the annual fee, and do you recommend we sign?",
        "give me the total and tell me if it is worth it",
        "who are the parties, and should we sign with them?",
        "what is the total and is it worth it?",
        "invoice total please, and is that reasonable for two racks?",
    ],
)
def test_a_decision_signal_beats_a_field_match(question):
    """Rule 2 of the review: a judgement asked for ANYWHERE in the question
    makes the answer extract+advise, never extraction alone. The fields are
    still answered first, under the strict rules."""
    signals = source_use.classify(question)
    assert signals.mode == "extract+advise", (question, signals)
    system = source_use.system_text(question)
    assert system.index("EXTRACTION QUESTION") < system.index("ALSO ASKED FOR A JUDGEMENT")


@pytest.mark.parametrize(
    "question",
    [
        "i'm thinking of racking these in it",
        "we'd be installing two of them here",
        "my plan is to host the cluster in one of these",
        "our options are this or a standard rack",
        "I want to place dgx spark on their rack for cooling",
    ],
)
def test_the_weak_decision_tier_catches_what_is_not_even_a_question(question):
    """No question mark, no decision word — a person saying what they mean to
    do with the thing. First person plus a word about DOING something with it
    is worth one point, and one point is enough. The owner's own follow-up
    turn, "I want to place dgx spark on their rack for cooling", is this
    shape."""
    signals = source_use.classify(question)
    assert signals.mode == "advise", (question, signals)
    assert signals.decision_score == source_use._WEAK or any(
        kind == "decision-marker" for kind, _ in signals.evidence
    ), signals.evidence


def test_the_thresholds_are_asymmetric_on_purpose():
    """The safe-failure rule, in the code rather than in a comment: one
    decision signal is enough to make a question advisory, strict extraction
    needs two points of high-precision evidence, and a single bare word that
    is also ordinary English is worth less than that."""
    assert source_use._DECISION_THRESHOLD < source_use._FIELD_THRESHOLD
    assert source_use._WEAK < source_use._FIELD_THRESHOLD
    bare = source_use.classify("what happens in the event of termination?")
    assert bare.field_score < source_use._FIELD_THRESHOLD, bare.evidence
    assert bare.mode == ""


def test_the_classifier_says_why():
    """A misroute has to be readable in one line. The 2026-09-18 blocker was
    one lookbehind that excluded "long-term" and not "long term", and it took
    two live runs against the engine to find it."""
    named = source_use.classify("what is the due date and the PO number?")
    assert named.mode == "extract"
    assert any(kind == "field-noun" for kind, _ in named.evidence), named.evidence

    framed = source_use.classify("what is the term of this contract?")
    assert framed.mode == "extract"
    assert any(kind.startswith("frame-") for kind, _ in framed.evidence), framed.evidence

    decided = source_use.classify("is this the right choice for us in the long term?")
    assert decided.mode == "advise"
    assert any(kind == "decision-marker" for kind, _ in decided.evidence), decided.evidence
    assert not any(kind.startswith("frame-") for kind, _ in decided.evidence), decided.evidence


def test_help_is_only_a_decision_signal_in_a_frame():
    """"can you help me extract the total" is an extraction ask with the word
    help in it; "does this help us" is a decision."""
    assert source_use.question_mode("can you help me extract the total?") == "extract"
    assert source_use.question_mode("does this help us at 20 nodes?") == "advise"


# ---------------------------------------------------------------------------
# "A decision signal ANYWHERE beats a field frame ANYWHERE" — as a property,
# and then as a generated search over the class the blocker belonged to.
#
# The 2026-09-18 QA round closed the headline incident and left this open:
# _VALUE_WH_RE is anchored to the START of the whole question, and it was used
# to DISCARD a "should" clause found anywhere later in it. Six hand-written
# rows of test_a_decision_signal_beats_a_field_match were all green because
# not one of them opened with a value-wh word. Examples cannot find a hole
# they were written without knowing about, so this section asserts the
# property and then searches the space instead of sampling it.
# ---------------------------------------------------------------------------


def test_the_precedence_gives_a_held_signal_back_when_the_fields_win():
    """The rule, read straight off _apply_precedence with no regex involved.

    A narrowing rule may send a question to NEUTRAL, which answers what was
    asked and still permits a verdict. It may never send one to strict
    EXTRACTION, whose block says "do not add advice, a recommendation, a next
    step, a caution or an offer of further help".
    """
    def held_only():
        return source_use._Decision(
            held=source_use._STRONG,
            held_why=[("should-inside-a-value-question", "should we")],
        )

    below = source_use._apply_precedence(0, held_only())
    assert below.mode == "", below
    assert below.held_decision_score == source_use._STRONG
    assert below.decision_signal_anywhere

    at = source_use._apply_precedence(source_use._FIELD_THRESHOLD, held_only())
    assert at.mode == "extract+advise", at
    assert at.held_decision_score == 0, "the held signal is spent, not kept"
    assert any(
        kind == "should-restored-beside-a-field-ask" for kind, _ in at.evidence
    ), at.evidence


def test_extraction_alone_is_unreachable_from_any_score_that_carries_a_judgement():
    """The whole score space, not a sample: 1,000 combinations of the three
    numbers the precedence sees. Every one that leaves as "extract" carries no
    judgement evidence at all, held or counted."""
    seen = {"extract": 0, "extract+advise": 0, "advise": 0, "": 0}
    for field, score, held in itertools.product(range(10), repeat=3):
        sig = source_use._apply_precedence(
            field, source_use._Decision(score=score, held=held)
        )
        seen[sig.mode] += 1
        if sig.mode == "extract":
            assert not sig.decision_signal_anywhere, (field, score, held, sig)
    assert seen["extract"], "the search never reached extraction, so it proves nothing"
    assert seen["extract+advise"], seen


#: One opener for every word _VALUE_WH_RE names — what / what's / whats / when
#: / who / where / how much / how many / how long / how often — because the
#: anchor was what made the first word of a question decide whether its second
#: half was answered.
_WH_OPENERS = (
    "what is the {field}",
    "what's the {field}",
    "whats the {field}",
    "what does this document give as the {field}",
    "when was the {field} last changed",
    "who set the {field}",
    "where does it state the {field}",
    "how much of a difference does the {field} make",
    "how many times does it repeat the {field}",
    "how long has the {field} been in force",
    "how often does the {field} change",
)

#: Real fields of a real document, each of which reaches the strict threshold
#: on its own (asserted below, so a combination cannot pass by the field half
#: quietly failing to register).
_FIELD_HEADS = (
    "invoice total",
    "tax amount",
    "due date",
    "notice period",
    "payment terms",
    "governing law",
    "PO number",
    "termination clause",
    "annual fee",
    "billing address",
    "interest rate",
    "line items",
)

#: Judgement asks in the words people use. The last five are the 2026-09-18
#: MEDIUM: they scored ZERO decision evidence, so beside a named field they
#: produced strict extraction — a missing decision word next to a present
#: field word is the unsafe direction.
_DECISION_TAILS = (
    "should we accept it?",
    "should I be worried about that?",
    "should that worry me?",
    "should we renegotiate it?",
    "should I be happy with that?",
    "should we be paying late fees?",
    "should that be a concern at our size?",
    "should we sign?",
    "is that reasonable?",
    "do you recommend we sign?",
    "is it worth it?",
    "would you recommend this?",
    "is that too expensive?",
    "is that a red flag?",
    "is that a problem for us?",
    "is that normal?",
    "how bad is that for us?",
    "what do you make of it?",
)

#: How the two halves are joined. "and" was the shape QA reproduced; a dash, a
#: full stop and "but" are the same question typed by someone else, and a
#: clause-scoped pattern would have had to get all four right.
_JOINS = (", and ", " - ", ". ", " but ")


def _generated_questions():
    for opener, field, tail, join in itertools.product(
        _WH_OPENERS, _FIELD_HEADS, _DECISION_TAILS, _JOINS
    ):
        yield opener.format(field=field) + join + tail


def test_every_generated_field_head_reaches_extraction_on_its_own():
    """Control 1 for the search. Without this, a combination could pass
    because the field never registered, and the search would prove nothing."""
    wrong = [
        (q, source_use.classify(q))
        for opener, field in itertools.product(_WH_OPENERS, _FIELD_HEADS)
        for q in [opener.format(field=field) + "?"]
        if source_use.question_mode(q) != "extract"
    ]
    assert not wrong, wrong[:10]


def test_every_generated_decision_tail_carries_a_signal_on_its_own():
    """Control 2 for the search: each tail is a judgement ask by itself, so a
    combination that routes to extraction is a signal being DISCARDED and not
    a signal that was never there."""
    missing = [t for t in _DECISION_TAILS if not source_use.classify(t).decision_signal_anywhere]
    assert not missing, missing


def test_no_generated_combination_routes_to_extraction_alone():
    """THE SEARCH. 9,504 questions: every value-wh opener x every field x
    every decision tail x every join.

    Measured on the classifier this test ships with: 0 violations. Measured on
    the same search against the classifier as it stood before this change:
    6,336 of 9,504 (66.7%) routed to strict "extract", which is the block that
    says "do not add advice, a recommendation, a next step, a caution or an
    offer of further help" — the owner's complaint, reproduced live by QA 3 of
    3 on a controlled invoice pair.

    It is pinned as a search rather than as rows so that a future edit cannot
    reintroduce the class by finding a wording nobody thought to write down.
    """
    total = 0
    bad = []
    for question in _generated_questions():
        total += 1
        if source_use.question_mode(question) == "extract":
            bad.append((question, source_use.classify(question).evidence))
    assert total == 9504, total
    assert not bad, f"{len(bad)} of {total} routed to extraction alone, e.g. {bad[:5]}"


def test_the_generated_search_covers_both_halves_it_was_built_for():
    """A search is only worth its coverage. If a later edit trims a table,
    this fails before the search quietly gets easier."""
    assert len(_WH_OPENERS) == 11 and len(_FIELD_HEADS) == 12
    assert len(_DECISION_TAILS) == 18 and len(_JOINS) == 4
    openers = " ".join(_WH_OPENERS)
    for word in ("what is", "what's", "whats", "when", "who", "where",
                 "how much", "how many", "how long", "how often"):
        assert word in openers, word
    # every opener really is a value-wh opener as the narrowing rule reads it
    for opener in _WH_OPENERS:
        assert source_use._VALUE_WH_RE.match(opener.format(field="total")), opener


def test_the_first_word_of_a_question_no_longer_decides_if_it_is_answered():
    """QA's minimal pair, live 6 runs on one invoice fixture: the misrouted
    phrasing refused the judgement half 3 of 3 ("The document does not provide
    enough information to determine if the invoice should be accepted"), the
    correctly routed one gave a verdict 3 of 3 ("**Yes, accept it.**")."""
    tell = source_use.classify("tell me the invoice total, and should we accept it?")
    what = source_use.classify("what is the invoice total, and should we accept it?")
    assert tell.mode == "extract+advise", tell
    assert what.mode == "extract+advise", what
    assert tell.mode == what.mode


@pytest.mark.parametrize(
    "question",
    [
        "what is the notice period, and should we renegotiate it?",
        "what are the payment terms, and should I be happy with them?",
        "when is payment due, and should we be paying late fees?",
        "what is the termination clause, and should that worry me?",
        "what does it cost, and should that be a concern at our size?",
        "what notice period should I be giving?",
        "who signed this, and should I be worried about that?",
    ],
)
def test_the_wordings_qa_reproduced_are_answered_in_both_halves(question):
    """Every one of these was verified routing to strict extraction before
    this change; each is a field ask AND a judgement ask."""
    system = source_use.system_text(question)
    assert source_use.question_mode(question) == "extract+advise", source_use.classify(question)
    assert system.index("EXTRACTION QUESTION") < system.index("ALSO ASKED FOR A JUDGEMENT")


@pytest.mark.parametrize(
    "tail",
    [
        "is that a problem for us?",
        "is that a red flag?",
        "is that normal?",
        "how bad is that for us?",
        "what do you make of it?",
        "is this a deal-breaker?",
        "is that cause for concern?",
        "should I be concerned?",
    ],
)
def test_asking_for_an_assessment_is_a_decision_signal(tail):
    """The 2026-09-18 MEDIUM. Alone these are safe — they fall to neutral,
    which permits a judgement — but beside a named field they produced strict
    extraction, and that is the direction this module may not be wrong in."""
    assert source_use.classify(tail).decision_signal_anywhere, source_use.classify(tail)
    assert source_use.question_mode(f"what is the invoice total, and {tail}") == "extract+advise"


@pytest.mark.parametrize(
    "question",
    [
        "copy the addresses into a table",
        "copy out the payment schedule",
        "copy this clause",
        "copy the table on page 3",
        "copy every line item",
    ],
)
def test_copy_is_an_extraction_verb_whatever_noun_follows(question):
    """LOW: the verb took a fixed noun list, so "copy the addresses into a
    table" fell to neutral. The brief lists copy as an extraction verb."""
    assert source_use.question_mode(question) == "extract", source_use.classify(question)


def test_copy_still_needs_something_to_copy():
    """The widened verb is a determiner plus a noun, not the bare word: an
    acknowledgement is not an extraction request."""
    assert source_use.question_mode("copy that, thanks") != "extract"


# ---------------------------------------------------------------------------
# The heading grader — the LOW the recheck could only check by eye
# ---------------------------------------------------------------------------


def test_the_heading_grader_catches_the_headings_the_recheck_saw():
    """Pinned against the real lines from the 2026-09-18 live runs. Reading
    the prompt cannot show whether the rule held; only an answer can, so the
    live test asserts this list is empty."""
    seen_live = (
        "### 1. The Document\u2019s Limitations (Why it\u2019s not \"Full\")\n"
        "### 2. General Knowledge: DGX Spark Requirements\n"
        "### 3. This Conversation: Your Scale (2 \u2192 20 Nodes)\n"
        "**THE DOCUMENT**\n**GENERAL KNOWLEDGE**\n**THIS CONVERSATION**\n"
    )
    assert len(source_named_headings(seen_live)) == 6, source_named_headings(seen_live)


def test_the_heading_grader_catches_the_forms_this_round_measured():
    """Every tightening of BASE moved the failure, and the grader had to move
    with it. These are real heading lines from this round's live runs."""
    seen_live = (
        "**The Document\u2019s Limits:**\n"
        "### 1. What the Document Says (Constraints)\n"
        "### 1. The Document\u2019s Specifications\n"
        "### What the Document Does Not Say\n"
        "### 3. This Conversation: Your Scale\n"
        # with the ### headings clean, the split moved to the bold labels
        "**The Document\u2019s Figures:**\n"
        "**My Knowledge (DGX Spark Power):**\n"
        # and rebuilt again out of words no ban had named
        "### 1. What the Document Says\n"
        "### 2. What I Know (General Contract & Business Context)\n"
        "### 3. What This Conversation Tells Me About You\n"
    )
    assert len(source_named_headings(seen_live)) == 10, source_named_headings(seen_live)


def test_the_heading_grader_is_anchored_so_it_can_be_disproved():
    """A grader that flagged every heading containing the word "document"
    could never be satisfied, and the live assertion would be theatre. The
    subject has to BE the source: in "Critical Gaps in the Document" the
    subject is the gaps, and a numbered list item with a bold lead-in is not a
    heading at all."""
    ordinary = (
        "### 4. Critical Gaps in the Document\n"
        "1.  **Rack Count vs. Server Count:** The document says \"up to 12 racks.\"\n"
        "3.  **No Specific DGX Spark Mention:** The document does not mention DGX.\n"
        "**Verdict on Power:**\n"
        "### Summary Table\n"
        "### Recommendation & Next Steps\n"
        "**What Would Change This Answer?**\n"
        "### The 24-month term\n"
        "### Getting out\n"
    )
    assert source_named_headings(ordinary) == []


def test_the_heading_grader_leaves_a_good_answer_alone():
    good = (
        "### Recommendation: No, not for two Sparks\n"
        "### Power draw at 20 nodes\n"
        "**Total:** 5,200.00 USD\n"
        "**Due Date:** 2026-09-01\n"
        "## Cooling headroom\n"
        "The document says 4 x 45 kW units, which is far above what two Sparks draw.\n"
        "### What would settle it\n"
        # real headings from this round's live answers, all of them fine
        "### 4. \"Full Help\" Assessment\n"
        "### Comparison: SmartRow vs. Standard Rack for Your Scale\n"
        "### Why This Fits Your Long-Term Needs\n"
        "### What\u2019s Missing (and Why It Matters)\n"
        "### Capacity\n"
        "### Contract terms\n"
    )
    assert source_named_headings(good) == []


# ---------------------------------------------------------------------------
# The bold LABEL grader — the half of BASE's rule that had no instrument
# ---------------------------------------------------------------------------


def test_the_label_grader_catches_the_bullet_lead_ins_the_round_measured():
    """BASE says "CHECK EVERY HEADING AND EVERY BOLD LABEL BEFORE YOU WRITE
    IT, including a label inside a section". 4 of the 24 answers the
    2026-09-18 live round graded carried 10 such labels and
    source_named_headings() returned [] for every one of them: it matches a
    bold phrase only when the phrase is the WHOLE line, and these are bullet
    lead-ins with the line's content after them. These are the real lines."""
    seen_live = (
        "**The Document Says:** The compact configuration uses 2 x 10 kW units.\n"
        "- **Document says:** 4 x 45 kW row cooling.\n"
        "* **General Knowledge:** a DGX Spark draws about 240 W.\n"
        "1. **Document Silence:** nothing about DGX, Spark or NVIDIA.\n"
        "**Document Says:** up to 12 racks.\n"
    )
    assert len(source_named_labels(seen_live)) == 5, source_named_labels(seen_live)


def test_the_label_grader_leaves_an_ordinary_bold_label_alone():
    """The same disprovability the heading grader was built with. A bold label
    with content after it is the NORMAL way to print a field, and a grader
    that flagged those could never be satisfied."""
    ordinary = (
        "**Total:** 5,200.00 USD\n"
        "**Due Date:** 2026-09-01\n"
        "- **Power headroom:** 10 kW per row.\n"
        "1.  **Rack Count vs. Server Count:** The document says \"up to 12 racks.\"\n"
        "3.  **No Specific DGX Spark Mention:** The document does not mention DGX.\n"
        "* **Cooling capacity:** 4 x 45 kW.\n"
        "**Verdict:** Yes, but it is overkill for two Sparks.\n"
    )
    assert source_named_labels(ordinary) == []


def test_a_line_is_never_both_a_heading_and_a_label():
    """The two graders are separate because they measure different behaviours
    at different rates, and a line counted twice would inflate either one."""
    both = "**THE DOCUMENT**\n**The Document Says:**\n### 1. What the Document Says\n"
    assert len(source_named_headings(both)) == 3, source_named_headings(both)
    assert source_named_labels(both) == []


def test_the_heading_grader_now_sees_the_source_as_a_bare_subject():
    """"**The Document Says:**" matched no alternative of the heading pattern
    before this round: every "document says" form it had required a "what" in
    front of it. It was invisible even as a standalone heading."""
    assert source_named_headings("**The Document Says:**\n") == ["**The Document Says:**"]
    assert source_named_headings("### Document Silence\n") == ["### Document Silence"]
    # ... and still anchored, so the live assertion can still come out clean.
    assert source_named_headings("### 4. Critical Gaps in the Document\n") == []
    assert source_named_headings("### Document Retention\n") == []



# ---------------------------------------------------------------------------
# ROUND 5: a judgement ask with NO decision word beside a field ask (2026-09-18)
#
# Round 4's verifier generated 11,475 "field ask + judgement ask" questions and
# 8,676 took strict extraction alone. Live, "what is the annual fee in this
# contract, and is that in line with the market?" came back "I cannot assess
# its competitiveness based on the provided text", while "... and is that
# reasonable?" — one word different, and that word in _DECISION_RE — got a
# verdict. The round-4 search could not find this by construction: its control
# (test_every_generated_decision_tail_carries_a_signal_on_its_own, above)
# asserted that every tail already registered as a decision signal.
#
# The corpus these tests read, tests/document_judgement_corpus.py, was
# committed BEFORE the detector (see its docstring and git history), and it is
# not filtered through source_use. The controls below are the opposite of the
# round-4 one: the tails are judgement asks by their OWN wording (a hand
# label anchored to a fragment of the tail), and the search runs with the
# decision vocabulary SWITCHED OFF, so it can only pass on structure.
#
# Measured on the full search (30 field heads x 114 tails x 10 joins x both
# orders), strict extraction alone:
#                          4810da0 (before)    this change
#   dev tails (84)         39,600 / 50,400      1,140 / 50,400
#   held-out tails (30)    16,200 / 18,000        600 / 18,000
# and 66 of the 84 dev tails carry no decision signal at all.
# ---------------------------------------------------------------------------

from tests import document_judgement_corpus as corpus  # noqa: E402

_ALL_TAILS = corpus.JUDGEMENT_TAILS + corpus.HELD_OUT_TAILS

#: The two residual classes the clause test leaves, both documented in
#: source_use (step 4, "MEASURED WITH THIS STEP"). The search below may fail
#: ONLY inside these; a failure anywhere else is a new hole.
#:
#: R1: REVERSED order, where the generator removed the tail's question mark
#: and put a bare fragment first ("in line with the market, and what is the
#: annual fee?"). With no "?" and no question shape, nothing marks it as an
#: ask, and treating every leading fragment as one would cost real field asks
#: ("for the Leeds office, what is the billing address?").
_R1_FRAGMENT_TAILS = frozenset({
    "in line with the market?",
    "on the high side?",
    "good or bad?",
    "anything we ought to be careful about there?",
    "thumbs up or thumbs down?",
    "any thoughts on that?",
    "too high?",
})
#: R2: FORWARD order, an adjective-led fragment glued on with a bare "and" or
#: "but" ("what is the invoice total and good or bad?"). Without a
#: part-of-speech tagger "and good or bad" looks like "and cooling unit
#: prices", which is another field.
_R2_ADJECTIVE_TAILS = frozenset({"good or bad?", "thumbs up or thumbs down?", "too high?"})


def _documented_residual(direction, tail, join):
    if direction == "rev":
        return tail in _R1_FRAGMENT_TAILS and join != "? "
    return tail in _R2_ADJECTIVE_TAILS and join in (" and ", " but ")


def test_the_corpus_tails_are_judgement_asks_by_their_own_wording():
    """The control that REPLACES round 4's. It does not ask whether any
    pattern recognises a tail; it checks the hand label against the tail's own
    words, so a label cannot drift away from what it describes."""
    assert len(corpus.JUDGEMENT_TAILS) >= 80 and len(corpus.HELD_OUT_TAILS) >= 30
    assert len({t for t, _, _ in _ALL_TAILS}) == len(_ALL_TAILS), "duplicate tail"
    from collections import Counter

    kinds = Counter(kind for _, kind, _ in _ALL_TAILS)
    for tail, kind, because in _ALL_TAILS:
        assert kind in corpus.KINDS, (tail, kind)
        assert because.lower() in tail.lower(), (tail, because)
    for kind in corpus.KINDS:
        assert kinds[kind] >= 3, (kind, kinds[kind])


def test_the_search_is_not_built_from_the_vocabulary_it_tests():
    """The round-4 search could only return zero because every tail it used
    was already a decision signal. Most of these are not — measured at this
    commit, 66 of the 84 dev tails and 27 of the 30 held-out tails carry no
    decision signal at all — and the search below runs with the decision
    vocabulary switched off, so it keeps searching where the vocabulary is
    blind however that vocabulary grows."""
    blind = [t for t, _, _ in _ALL_TAILS if not source_use.classify(t).decision_signal_anywhere]
    assert len(blind) >= len(_ALL_TAILS) // 2, (len(blind), len(_ALL_TAILS))
    # the verifier's own tail is among the blind ones
    assert "is that in line with the market?" in blind


def test_every_corpus_field_head_reaches_extraction_on_its_own():
    """Control 1: a combination cannot pass because its field half quietly
    failed to register."""
    wrong = [h for h in corpus.FIELD_HEADS if source_use.question_mode(h + "?") != "extract"]
    assert not wrong, wrong
    assert len(corpus.FIELD_HEADS) >= 25 and len(corpus.JOINS) >= 6


def test_no_judgement_tail_rides_a_field_ask_into_strict_extraction_on_structure_alone(monkeypatch):
    """THE SEARCH, with the decision vocabulary OFF: 30 field heads x 114
    judgement tails x 10 joins x both orders = 68,400 questions. Every
    question that still leaves as strict "extract" must be one of the two
    documented residual classes above.

    Measured at this commit, vocabulary off: 2,070 of 68,400 in those two
    classes and 0 outside them. On 4810da0, vocabulary ON, 55,800 of 68,400
    took strict extraction alone. Decision evidence can only move a question
    AWAY from "extract" (see _apply_precedence), so passing with it off is the
    stronger statement.
    """
    monkeypatch.setattr(source_use, "_decision_evidence", lambda q: source_use._Decision())
    total = 0
    outside = []
    residual = 0
    for head, (tail, _, _), join in itertools.product(corpus.FIELD_HEADS, _ALL_TAILS, corpus.JOINS):
        for direction, question in (
            ("fwd", head + join + tail),
            ("rev", tail.rstrip("?") + join + head + "?"),
        ):
            total += 1
            if source_use.question_mode(question) != "extract":
                continue
            if _documented_residual(direction, tail, join):
                residual += 1
            else:
                outside.append(question)
    assert total == 68_400, total
    assert not outside, f"{len(outside)} new holes, e.g. {outside[:5]}"
    assert residual <= 2_070, residual


@pytest.mark.parametrize(
    "question",
    [
        # the verifier's live reproduction, and the same ask in other shapes
        "what is the annual fee in this contract, and is that in line with the market?",
        "what is the annual fee in this contract and is that in line with the market?",
        "is that in line with the market? what is the annual fee in this contract",
        "what is the annual fee in this contract - how does that stack up against the market?",
        "give me the grand total and tell me if that's in line with the market",
        "what's the due date, and can we actually meet that?",
        "what is the notice period; would a lawyer be happy with that?",
        "list the line items and flag it if that looks unusual",
        "what is the invoice total, and your thoughts?",
        "what are the payment terms. is that one-sided?",
    ],
)
def test_a_judgement_ask_with_no_decision_word_beside_a_field_is_not_extraction_alone(question):
    """Every one of these took strict extraction alone on 4810da0: the field
    half scored, the judgement half carried no decision word. Now the clause
    test sees a second ask that no field can answer."""
    signals = source_use.classify(question)
    assert signals.mode == "extract+advise", (question, signals)
    assert signals.asks_beyond_fields, signals
    system = source_use.system_text(question)
    assert system.index("EXTRACTION QUESTION") < system.index("ALSO ASKED FOR A JUDGEMENT")


def test_the_verifiers_pair_now_takes_one_route_and_structure_is_what_caught_it():
    """The minimal pair: one word different, one routed to strict extraction
    and refused, the other answered 3 of 3. Both are extract+advise now — and
    the market one is caught by the clause test, not by a decision word."""
    market = source_use.classify(
        "what is the annual fee in this contract, and is that in line with the market?")
    reasonable = source_use.classify(
        "what is the annual fee in this contract, and is that reasonable?")
    assert market.mode == reasonable.mode == "extract+advise"
    assert not market.decision_signal_anywhere, market.evidence
    assert any(kind.startswith("open-ask-") for kind, _ in market.evidence), market.evidence


@pytest.mark.parametrize(
    "question",
    [
        "is the annual fee in this contract in line with the market?",
        "is the notice period long?",
        "is the interest rate on late payment steep?",
        "are the payment terms one-sided?",
    ],
)
def test_a_one_clause_judgement_about_a_named_field_is_not_extraction_alone(question):
    """The same class without a join: a polar question whose subject is a
    field and whose predicate is not something the page can state."""
    assert source_use.question_mode(question) == "extract+advise", source_use.classify(question)


@pytest.mark.parametrize(
    "question",
    [
        "what is the annual fee, and is it payable in advance?",
        "what is the invoice total, and does it include VAT?",
        "what is the due date, and is it a business day?",
        "what is the notice period, and does it apply to both parties?",
        "what is the late fee and how is it calculated?",
        "what is the total, and which clause is it in?",
        "extract the line items and put them in a table",
        "what is the total, and can you list the line items?",
        "is there a PO number on this invoice?",
        "does the datasheet give a price?",
        "I need the total from this invoice?",
        "what is the fee in this contract and in USD?",
    ],
)
def test_a_fact_about_a_field_is_still_a_field_ask(question):
    """The other direction. A follow-up the page CAN answer — when, how, in
    what currency, in which clause — leaves nothing once field words, value
    words and function words are taken away, and stays strict."""
    signals = source_use.classify(question)
    assert signals.mode == "extract", (question, signals.evidence, signals.open_asks)


def test_strict_extraction_survives_for_pure_field_asks():
    """Bar: at least 300 pure field asks, at least 95% strict. The corpus
    builds 4,460 (field heads alone, a field head plus a fact follow-up, two
    field heads joined), including ten follow-ups held out until the detector
    was finished. Measured at this commit: 4,460 of 4,460 strict. On the
    frozen detector the held-out follow-ups first measured 1,210 of 1,330
    (90.98%) — all 120 losses were "is it quoted per unit?" — which is what
    added pricing bases to _VALUE_WORDS."""
    asks = [h + "?" for h in corpus.FIELD_HEADS]
    for head, follow, join in itertools.product(
        corpus.FIELD_HEADS,
        corpus.FACT_FOLLOW_UPS + corpus.HELD_OUT_FACT_FOLLOW_UPS,
        (", and ", "? ", " - ", ". "),
    ):
        asks.append(head + join + follow)
    for (a, b), join in itertools.product(corpus.FIELD_PAIRS, corpus.JOINS):
        asks.append(a + join + b)
    assert len(asks) >= 300, len(asks)
    lost = [q for q in asks if source_use.question_mode(q) != "extract"]
    kept = len(asks) - len(lost)
    assert kept >= 0.95 * len(asks), f"{kept}/{len(asks)} strict; lost e.g. {lost[:8]}"


def test_the_clause_test_never_sends_a_question_to_strict_extraction():
    """It can only take a question OUT of "extract". Over the precedence's
    whole score space, with and without an open ask: an open ask beside
    fields is extract+advise, and without fields it changes nothing."""
    ask = [("polar", "is that in line with the market?")]
    for field, score, held in itertools.product(range(6), repeat=3):
        without = source_use._apply_precedence(field, source_use._Decision(score=score, held=held))
        with_ask = source_use._apply_precedence(
            field, source_use._Decision(score=score, held=held), open_asks=ask)
        assert with_ask.mode != "extract", (field, score, held)
        if without.mode != "extract":
            assert with_ask.mode == without.mode, (field, score, held)
        else:
            assert with_ask.mode == "extract+advise"


def test_the_clause_test_only_runs_beside_a_field_ask():
    """A question with no field in it has no strict block to be saved from,
    so its mode is exactly what the two vocabularies made it."""
    for question in ("is that in line with the market?", "summarize this document",
                     "what does clause 11 say?", OWNER_QUESTION):
        signals = source_use.classify(question)
        assert signals.open_asks == [], (question, signals.open_asks)


# --- the strict block no longer turns a misroute into a refusal --------------


def test_the_strict_block_forbids_only_what_was_not_asked():
    """The old block said "For this answer do not add advice, a
    recommendation, a next step, a caution or an offer of further help" with
    no qualifier. Forced into it live, the verifier's market question opened
    its judgement with "The document does not provide market benchmarks ..."
    0 of 8 times answered; the rewritten block answered 7 of 15, and an
    actual residual ("what is the annual fee in this contract and good or
    bad?", still routed extract) went from 0 of 5 answered to 7 of 8."""
    system = source_use.system_text("what is the invoice total?")
    assert source_use.question_mode("what is the invoice total?") == "extract"
    assert "For this answer do not add advice" not in system
    assert "that the person did not ask for" in system
    assert "NOTHING THE PERSON ASKED IS FORBIDDEN" in system
    assert "that part was asked, so answer it" in system
    assert "The strict rules above bind the FIELDS only" in system
    assert "Its FIRST sentence is the verdict" in system
    assert "never a list of things to go and check" in system


def test_the_strict_block_still_never_invents_a_value():
    """What the block exists for, unchanged in force: the "**Tax Amount:**
    0.00 USD" fabrication. Live on the tax-free invoice, 16 runs each: the
    rewritten block kept the total, wrote "not stated" for the tax and printed
    no tax figure 16 of 16, at median 76 and 86.5 chars on the two fixtures;
    the old block was clean 16 of 16 too, at medians of 193 and 126.5."""
    q = "I need the total from this invoice and the tax amount"
    system = source_use.system_text(q)
    assert source_use.question_mode(q) == "extract"
    assert "for a field, the document is the only source" in system
    assert 'write "not stated in the document"' in system
    assert "that line is the whole answer for that field" in system
    assert "with no sentence about why it is missing" in system
    assert "A field the document does not give is never such a part" in system
    assert "Answer what was asked and stop" in system


def test_both_judgement_halves_share_one_set_of_rules():
    """A judgement that lands in extract+advise and one that lands in strict
    extraction get the same instruction for how to answer it, and the
    extract+advise block does not carry the safety net as well."""
    both = source_use.system_text("what is the annual fee, and is that reasonable?")
    strict = source_use.system_text("what is the annual fee?")
    assert source_use._JUDGEMENT_RULES in both
    assert source_use._JUDGEMENT_RULES in strict
    assert "NOTHING THE PERSON ASKED IS FORBIDDEN" not in both
    assert both.index("ALSO ASKED FOR A JUDGEMENT") < both.index(source_use._JUDGEMENT_RULES)
    # the rules quote no failing wording: the model copies what a prompt quotes
    for phrase in ("in line with the market", "reasonable?", "competitive"):
        assert phrase not in source_use._JUDGEMENT_RULES
        assert phrase not in source_use._ASKED_IS_ANSWERED
