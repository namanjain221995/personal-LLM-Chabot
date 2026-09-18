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
from tests.document_answer_grader import (cites_document, explains_a_missing_field,
                                          gives_recommendation, opens_with_refusal,
                                          referral_only, source_named_headings,
                                          source_named_labels, states_a_computation)

#: The ONE block every field-bearing question gets since round 7 (2026-09-19),
#: and its two rule sets. Rounds 1-6 had a strict EXTRACTION block that
#: forbade judgement and an extract+advise block beside it; the router chose
#: between them and missed 10-25% of judgement asks. The assertions that used
#: to read "EXTRACTION QUESTION" and "ALSO ASKED FOR A JUDGEMENT" read these.
FIELD_BLOCK = "THE QUESTION NAMES FIELDS OF THE DOCUMENT"
FIELD_RULES = "FIELD RULES, for each field asked"
JUDGEMENT_RULES = "JUDGEMENT RULES, for anything else the person asked"


def _fields_then_judgement(question):
    """Round 7's replacement for "mode == extract+advise": the question gets
    the field block, whose judgement rules answer anything else it asks,
    after the fields and never refused."""
    signals = source_use.classify(question)
    assert signals.mode == "extract", (question, signals.evidence)
    system = source_use.system_text(question)
    assert system.index(FIELD_RULES) < system.index(JUDGEMENT_RULES), question
    assert "every such ask is answered" in system and "never refused" in system
    return signals

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
    assert FIELD_BLOCK not in system
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
    assert FIELD_BLOCK in system
    assert "return ONLY what is actually in the document" in system
    assert "do not add advice" in system
    assert "not stated in the document" in system
    assert "DECISION QUESTION" not in system


def test_an_explicit_judgement_rides_AFTER_the_fields():
    """"...and tell me whether I should renew" asks for both.

    The first cut answered it as a pure decision question, which loses the
    fields. The rule is: the field rules win for the fields, advice follows
    after them, clearly separated. Since round 7 that is ONE block with two
    rule sets, fields first, instead of two blocks the router chose between.
    """
    q = "extract the payment terms and tell me whether I should renew"
    _fields_then_judgement(q)
    system = source_use.system_text(q)
    assert "after the fields, under its own heading" in system
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
    assert FIELD_BLOCK not in system


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
    assert FIELD_BLOCK in system
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
    assert FIELD_BLOCK in system
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
    assert FIELD_BLOCK in system
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
        "extract the total and tell me if I should renew",  # extract, with a judgement
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
    assert FIELD_BLOCK not in system


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
    assert FIELD_BLOCK in system
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
    assert FIELD_BLOCK in system
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
# Round 7 (2026-09-19) folded extract+advise into "extract": the field block
# now answers any judgement asked beside its fields, so the eleven "mixed"
# rows are labelled "extract" and _fields_then_judgement() pins what they get.
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
    ('extract', 'extract the payment terms and tell me whether I should renew', 'mixed'),
    ('extract', 'what is the annual fee, and do you recommend we sign?', 'mixed'),
    ('extract', 'give me the total and tell me if it is worth it', 'mixed'),
    ('extract', 'what is the term and the notice period - should we renew?', 'mixed'),
    ('extract', 'list the line items and tell me if we are being overcharged', 'mixed'),
    ('extract', 'what is the total and is it worth it?', 'mixed'),
    ('extract', 'pull the fees out and tell me whether this is good value', 'mixed'),
    ('extract', 'who are the parties, and should we sign with them?', 'mixed'),
    ('extract', 'what is the due date, and do I need to pay early?', 'mixed'),
    ('extract', 'invoice total please, and is that reasonable for two racks?', 'mixed'),
    ('extract', 'list the reasons why I should buy this', 'mixed'),
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
    if group == "mixed":
        _fields_then_judgement(question)
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
    for mode in ("extract", "advise", ""):
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
    assert FIELD_BLOCK not in source_use.system_text(question)


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
    assert FIELD_BLOCK in source_use.system_text(question)


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
    is answered, never refused. The fields are still answered first, under
    the field rules."""
    signals = _fields_then_judgement(question)
    assert signals.wants_advice, signals.evidence


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


def test_no_block_the_router_can_choose_forbids_answering_an_ask():
    """ROUND 7's invariant, and why the router's misses stopped mattering.

    Rounds 1-6 pinned this through the router: "a judgement ask may never
    reach strict EXTRACTION", with a precedence function, held evidence and a
    clause test to enforce it -- and each blind verifier still found 10-25%
    of judgement asks that reached it. The strict block is gone. What is
    asserted now is a property of the three blocks themselves: every one
    answers a judgement it is asked for, and none bans advice without
    "that the person did not ask for" in the same sentence. Proved: with the
    JUDGEMENT RULES sentence removed from FIELDS, the first assertion fails;
    with the old strict block's unqualified ban put back, the second does."""
    for mode, block in source_use._BLOCKS.items():
        system = source_use.system_for_mode(mode)
        if mode == "extract":
            assert "every such ask is answered" in block and "never refused" in block
        elif mode == "advise":
            assert "Give a real recommendation" in block
        else:
            assert "STOPPING IS NOT REFUSING TO JUDGE" in block
        for sentence in system.split(". "):
            if "do not add advice" in sentence.lower():
                assert "that the person did not ask for" in sentence, (mode, sentence)
        assert "For this answer do not add advice" not in system
    assert set(source_use._BLOCKS) == {"extract", "advise", ""}


def test_a_value_question_with_should_beside_a_field_gets_the_field_block():
    """What the held-evidence machinery existed for, as behaviour. "should"
    inside a value question is read as asking for a value ("what supply
    water temperature should I run?" is neutral), and until round 7 that
    reading, beside a field, sent "what is the invoice total, and should we
    accept it?" to the strict block (refused 2 of 3 live). The field block
    now answers the judgement whatever the "should" is taken to mean."""
    held = source_use.classify("what is the invoice total, and should we accept it?")
    assert any(k == "should-inside-a-value-question" for k, _ in held.evidence), held.evidence
    _fields_then_judgement("what is the invoice total, and should we accept it?")
    assert source_use.question_mode("what supply water temperature should I run?") == ""


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
    missing = [t for t in _DECISION_TAILS
               if not source_use.classify(t).wants_advice
               and not any(k == "should-inside-a-value-question"
                           for k, _ in source_use.classify(t).evidence)]
    assert not missing, missing


def test_every_generated_combination_gets_the_fields_and_its_judgement():
    """THE SEARCH. 9,504 questions: every value-wh opener x every field x
    every decision tail x every join.

    Before round 5, 6,336 of 9,504 (66.7%) routed to the strict block that
    said "do not add advice, a recommendation, a next step, a caution or an
    offer of further help" -- the owner's complaint, reproduced live by QA 3
    of 3 on a controlled invoice pair. Since round 7 every one of them gets
    the ONE field block, whose judgement rules answer the tail: measured,
    9,504 of 9,504 "extract" (the no-refusal half is the block invariant,
    test_no_block_the_router_can_choose_forbids_answering_an_ask)."""
    total = 0
    bad = []
    for question in _generated_questions():
        total += 1
        if source_use.question_mode(question) != "extract":
            bad.append((question, source_use.classify(question).evidence))
    assert total == 9504, total
    assert not bad, f"{len(bad)} of {total} lost the field block, e.g. {bad[:5]}"


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
    tell = _fields_then_judgement("tell me the invoice total, and should we accept it?")
    what = _fields_then_judgement("what is the invoice total, and should we accept it?")
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
    _fields_then_judgement(question)


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
    assert source_use.classify(tail).wants_advice, source_use.classify(tail)
    _fields_then_judgement(f"what is the invoice total, and {tail}")


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
# ROUNDS 5 AND 6: judgement asks with NO decision word beside a field ask
#
# Round 4's verifier generated 11,475 "field ask + judgement ask" questions and
# 8,676 took the strict extraction block, which refused the judgement ("I
# cannot assess its competitiveness based on the provided text"). Rounds 5 and
# 6 answered with a clause test and then with shapes (an imperative with a noun
# object, "Okay for us?", "Would we sign it?", "that seems high to me"); each
# closed what its verifier found, and the next blind verifier found 10-25%
# more. Round 7 (2026-09-19) removed the strict block instead: every question
# that names a field gets ONE block whose judgement rules answer whatever else
# it asks. The clause test, its lexicons and the tests that pinned them are
# deleted with it; what these tests keep is the BEHAVIOUR they were written
# for -- every wording below gets the fields AND an answer to its judgement --
# and the corpora, committed before any detector, still drive it.
#
# The vocabulary_off fixture switches the decision vocabulary off: these
# wordings get the judgement rules because they name a field, not because a
# decision word was found.
# ---------------------------------------------------------------------------

from tests import document_judgement_corpus as corpus  # noqa: E402

_ALL_TAILS = corpus.JUDGEMENT_TAILS + corpus.HELD_OUT_TAILS
_R6_ALL_TAILS = corpus.ROUND6_TAILS + corpus.ROUND6_HELD_OUT_TAILS


@pytest.fixture()
def vocabulary_off(monkeypatch):
    monkeypatch.setattr(source_use, "_decision_evidence", lambda q: (0, []))


def test_the_corpus_tails_are_judgement_asks_by_their_own_wording():
    """The corpus control: each hand label is checked against the tail's own
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


def test_the_round6_rows_are_judgement_asks_by_their_own_wording():
    assert len(corpus.ROUND6_TAILS) >= 30 and len(corpus.ROUND6_HELD_OUT_TAILS) >= 25
    every = [t for t, _, _ in _ALL_TAILS + _R6_ALL_TAILS]
    assert len(set(every)) == len(every), "duplicate tail"
    for tail, kind, because in _R6_ALL_TAILS:
        assert kind in corpus.KINDS, (tail, kind)
        assert because.lower() in tail.lower(), (tail, because)


def test_the_decision_vocabulary_is_still_blind_to_most_of_the_corpus():
    """Why round 7 stopped relying on it. Measured at this commit: most of
    the hand-written judgement tails carry no decision signal at all, the
    verifier's own among them -- which is why a router that must SEE the
    judgement to allow it could never be finished."""
    tails = _ALL_TAILS + _R6_ALL_TAILS
    blind = [t for t, _, _ in tails if not source_use.classify(t).wants_advice]
    assert len(blind) >= len(tails) // 2, (len(blind), len(tails))
    assert "is that in line with the market?" in blind


def test_every_corpus_field_head_reaches_the_field_block_on_its_own():
    """Control: a combination below cannot pass because its field half quietly
    failed to register."""
    wrong = [h for h in corpus.FIELD_HEADS if source_use.question_mode(h + "?") != "extract"]
    assert not wrong, wrong
    assert len(corpus.FIELD_HEADS) >= 25 and len(corpus.JOINS) >= 6


def test_no_judgement_tail_beside_a_field_loses_the_judgement_rules(vocabulary_off):
    """THE SEARCH, decision vocabulary OFF: 30 field heads x 175 judgement
    tails (rounds 5 and 6, dev and held out) x 10 joins x both orders =
    105,000 questions. Each must get the field block, whose judgement rules
    answer the tail.

    Before round 7, with its clause test and every shape rule in place, 2,070
    of the round-5 68,400 and 1,380 of the round-6 36,600 still took the
    strict block alone; on 4810da0, 55,800 of 68,400. Measured at this
    commit: 0 of 105,000."""
    total, lost = 0, []
    for head, (tail, _, _), join in itertools.product(
        corpus.FIELD_HEADS, _ALL_TAILS + _R6_ALL_TAILS, corpus.JOINS
    ):
        for question in (head + join + tail, tail.rstrip("?") + join + head + "?"):
            total += 1
            if source_use.question_mode(question) != "extract":
                lost.append(question)
    assert total == 105_000, total
    assert not lost, f"{len(lost)} lost the field block, e.g. {lost[:5]}"


@pytest.mark.parametrize(
    "question",
    [
        # round 5: the verifier's live reproduction, and the same ask in other shapes
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
        # round 5: one clause, a field as subject
        "is the annual fee in this contract in line with the market?",
        "is the notice period long?",
        "is the interest rate on late payment steep?",
        "are the payment terms one-sided?",
        # round 6, F1: an imperative with any object
        "what is the annual fee in this contract, and assess the risk",
        "Extract the payment terms and assess the risk.",
        "what is the annual fee in this contract but flag any risks",
        "Highlight any concerns. What is the annual fee in this contract?",
        "what is the annual fee in this contract, and comment on the pricing",
        "what is the annual fee in this contract, and rate it out of 10",
        # round 6, F2: "okay" is the question
        "what is the annual fee in this contract? Okay for us?",
        "what is the annual fee in this contract? Ok to sign?",
        "okay for us, and what is the annual fee in this contract?",
        # round 6, F3: a modal about the person's own action
        "what is the annual fee in this contract? Would we sign it?",
        "what is the invoice total? would I pay that?",
        # round 6, F7: a statement of view
        "what is the annual fee in this contract, that seems high to me",
        "Our CFO thinks it's excessive. What is the annual fee in this contract?",
        "what is the invoice total - feels like a lot",
        # round 6: existential, run-on, embedded, why
        "any red flags, and what is the invoice total?",
        "Tell me if that's steep What is the annual fee in this contract?",
        "what is the invoice total, and I wonder if that's steep",
        "what is the invoice total, and why is the tax amount so high?",
        # the round-6 verifier's blind residuals (v6-1r-meas), all strict on 899da3d
        "what is the annual fee in this contract? poke holes in the offer",
        "what's the notice period, I'm not sure that's fair",
        "what is the total on this invoice, and help me decide whether to sign",
        "what's the notice period. our finance lead reckons it's a rip-off",
        "what is the total on this invoice and deal or no deal",
        "what is the annual fee in this contract and shall we go ahead",
    ],
)
def test_a_judgement_beside_a_field_gets_the_fields_and_an_answer(vocabulary_off, question):
    """Every one of these took the strict block alone on some round's code,
    and the 2026-09-18 live runs show what that cost: "I cannot determine if
    this is okay" (8 of 8 on "... Okay for us?"). Now each gets the fields and
    then its judgement, with the decision vocabulary switched off."""
    _fields_then_judgement(question)


def test_the_verifiers_pair_takes_one_route_without_a_decision_word():
    """The minimal pair, one word different: before round 5 one was refused
    and the other answered 3 of 3. Both get the same block now, and the
    market one still carries no decision word -- it does not need one."""
    market = _fields_then_judgement(
        "what is the annual fee in this contract, and is that in line with the market?")
    reasonable = _fields_then_judgement(
        "what is the annual fee in this contract, and is that reasonable?")
    assert market.mode == reasonable.mode == "extract"
    assert not market.wants_advice, market.evidence


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
        "summarise the payment terms",
        "identify the parties to this agreement",
        "print the line items",
        "name the parties to the contract",
        "describe the termination clause",
        "what is the invoice total, and summarise the line items",
        "list the line items and put them in a table",
        "sort the line items by price",
        "Okay, what is the invoice total?",
        "ok so what's the due date?",
        "what is the invoice total? ok",
        "short term this looks fine, but what is the term of the contract?",
        "it looks standard, but what is the invoice total?",
        "what is the notice period, and can we terminate early?",
    ],
)
def test_a_fact_about_a_field_is_still_a_field_ask(question):
    """The other direction: a follow-up the page CAN answer, filler, a
    settled view, a handover verb -- the field block, with nothing that
    invites a judgement nobody asked for (see NOTHING UNASKED)."""
    signals = source_use.classify(question)
    assert signals.mode == "extract", (question, signals.evidence)
    assert "NOTHING UNASKED" in source_use.system_text(question)


@pytest.mark.parametrize("follow", ["is it quoted per unit?", "is it per seat?",
                                    "is that per user?", "is it charged per device?"])
def test_a_pricing_basis_is_a_field_follow_up_for_every_head(follow):
    lost = [h for h in corpus.FIELD_HEADS
            if source_use.question_mode(h + "? " + follow) != "extract"]
    assert not lost, (follow, lost[:5])


@pytest.mark.parametrize("follow", corpus.ROUND6_FACT_FOLLOW_UPS)
def test_a_fact_follow_up_or_a_courtesy_keeps_every_field_head(follow):
    lost = []
    for head, join in itertools.product(corpus.FIELD_HEADS, (", and ", "? ", " - ", ". ")):
        question = head + join + follow
        if source_use.question_mode(question) != "extract":
            lost.append(question)
    assert not lost, lost[:5]


@pytest.mark.parametrize("head", corpus.ROUND6_UNREACHED_FIELD_HEADS)
def test_the_natural_contract_heads_reach_the_field_block(head):
    """Five contract fields the lexicon lacked before round 6 ("what's the
    liability cap", "how long is the non-compete")."""
    assert source_use.question_mode(head + "?") == "extract", source_use.classify(head + "?")


def _pure_field_asks():
    asks = [h + "?" for h in corpus.FIELD_HEADS + corpus.ROUND6_UNREACHED_FIELD_HEADS]
    follows = (corpus.FACT_FOLLOW_UPS + corpus.HELD_OUT_FACT_FOLLOW_UPS
               + corpus.ROUND6_FACT_FOLLOW_UPS + corpus.ROUND6_HELD_OUT_FACT_FOLLOW_UPS)
    for head, follow, join in itertools.product(corpus.FIELD_HEADS, follows,
                                                (", and ", "? ", " - ", ". ")):
        asks.append(head + join + follow)
    for (a, b), join in itertools.product(corpus.FIELD_PAIRS, corpus.JOINS):
        asks.append(a + join + b)
    contexts = (corpus.ROUND6_CONTEXT + corpus.ROUND6_HELD_OUT_CONTEXT
                + corpus.ROUND6_FRESH_CONTEXT + corpus.ROUND6_FRESH_CONTEXT_2)
    for head, context, join in itertools.product(corpus.FIELD_HEADS, contexts,
                                                 (". ", ", ", " - ")):
        said = context[0].upper() + context[1:]
        asks += [said + join + head + "?", head + "? " + said + "."]
    return asks


def test_pure_field_asks_keep_the_field_block():
    """The bar: at least 97% of 12,375 pure field asks (the round-5 set, the
    round-6 fact follow-ups and courtesies, four sets of context statements)
    get the field block. Measured at this commit: 12,375 of 12,375."""
    asks = _pure_field_asks()
    assert len(asks) == 12_375, len(asks)
    lost = [q for q in asks if source_use.question_mode(q) != "extract"]
    kept = len(asks) - len(lost)
    assert kept >= 0.97 * len(asks), f"{kept}/{len(asks)}; lost e.g. {lost[:8]}"


# --- a calculation is not a field -------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "what is the sum of the line items?",
        "add up the line items",
        "total the line items",
        "how much do the line items add up to?",
        "what is the difference between the subtotal and the total?",
    ],
)
def test_a_calculation_beside_a_field_goes_to_neutral(question):
    """Round 6: the field rules forbid arithmetic in the field lines, so a
    calculation the person ASKED for goes to NEUTRAL, where BASE says a
    worked-out figure is yours, with the arithmetic shown. Proved: with
    _COMPUTE_RE made to match nothing, every row routes "extract"."""
    signals = source_use.classify(question)
    assert signals.mode == "", (question, signals.evidence)
    system = source_use.system_text(question)
    assert FIELD_BLOCK not in system and "ANSWER THE QUESTION AND STOP" in system


def test_how_it_is_calculated_is_still_a_fact_about_the_field():
    """These ask what the page SAYS about a field. Proved: with the
    lookarounds on "sum" removed from _COMPUTE_RE, the lump-sum row routes
    neutral."""
    for question in ("what is the late fee and how is it calculated?",
                     "what is the annual fee? is VAT added?",
                     "what is the annual fee, and is it paid as a lump sum?",
                     "what are the subtotal and total the invoice shows?"):
        assert source_use.question_mode(question) == "extract", source_use.classify(question)


def test_a_calculation_with_a_judgement_keeps_the_field_block():
    """A decision word beside a calculation and a field keeps the field
    block, whose judgement rules cover the calculation ("A calculation you
    are asked for is yours: show the working and say so")."""
    q = "add up the line items and tell me if it is worth it"
    _fields_then_judgement(q)
    assert "A calculation you are asked for is yours" in source_use.system_text(q)
    assert source_use.question_mode("add up the line items") == ""


# --- the field block no longer turns a misroute into a refusal ---------------


def test_the_field_block_forbids_only_what_was_not_asked():
    """The old strict block said "For this answer do not add advice, a
    recommendation, a next step, a caution or an offer of further help" with
    no qualifier; forced into it live, the verifier's market question was
    answered 0 of 8 times. The field block bans only what the person did not
    ask for, and says every other ask is answered."""
    system = source_use.system_text("what is the invoice total?")
    assert source_use.question_mode("what is the invoice total?") == "extract"
    assert "For this answer do not add advice" not in system
    assert "that the person did not ask for" in system
    assert "every such ask is answered" in system and "never refused" in system
    assert "Its FIRST sentence is the verdict" in system
    assert "never a list of things to go and check" in system


def test_the_field_block_still_never_invents_a_value():
    """What the strict rules exist for, unchanged in force: the "**Tax
    Amount:** 0.00 USD" fabrication, the governing law filled in from general
    knowledge."""
    q = "I need the total from this invoice and the tax amount"
    system = source_use.system_text(q)
    assert source_use.question_mode(q) == "extract"
    assert "for a field, the document is the only source" in system
    assert 'write "not stated in the document"' in system
    assert "that line is the whole answer for that" in system
    assert "with nothing about why it is missing" in system
    assert "a field that is not stated stays not stated" in system
    assert "Answer what was asked and stop" in system


def test_both_judgement_blocks_share_one_set_of_rules():
    """A judgement gets the same instruction whichever block it lands in, and
    the rules quote no failing wording: the model copies what a prompt quotes."""
    both = source_use.system_text("what is the annual fee, and is that reasonable?")
    advise = source_use.system_text("is this worth it for us?")
    assert source_use._JUDGEMENT_RULES in both and source_use._JUDGEMENT_RULES in advise
    assert both.index(JUDGEMENT_RULES) < both.index(source_use._JUDGEMENT_RULES)
    for phrase in ("in line with the market", "reasonable?", "competitive",
                   "does not help", "full help"):
        assert phrase not in source_use._JUDGEMENT_RULES


# --- F6: a strict answer shows no working and explains no missing field -----

#: Real answers from the round-5 verifier's live runs on the itemised tax-free
#: invoice (INVOICE_WITHOUT_TAX), strict extraction, Fast. The first states a
#: sum that is false on its own terms (2 x 1,000.00 + 1 x 4,200.00 is
#: 6,200.00, not 5,200.00).
_PADDED_WITH_A_FALSE_SUM = (
    "**Total:** 5,200.00 USD (from the invoice)\n\n**Tax Amount:** Not stated in the "
    "document\n\nThe invoice lists a total of 5,200.00 USD but does not provide a separate "
    "line item for tax, nor does it specify a tax rate or amount. The total appears to be "
    "the sum of the line items (2 x 1,000.00 + 1 x 4,200.00 = 5,200.00), suggesting either "
    "no tax was applied, tax is included in the unit prices, or the tax amount is zero. "
    "Without a specific tax line or rate mentioned, the exact tax amount cannot be "
    "determined from the document."
)
_PADDED = (
    "**Total:** 5,200.00 USD (from the invoice)\n\n**Tax Amount:** Not stated in the "
    "document. The invoice lists a total of 5,200.00 USD but does not provide a separate "
    "line item for tax, nor does it specify if tax is included in the line items or the "
    "total."
)
_CLEAN = "**Total:** 5,200.00 USD\n**Tax Amount:** not stated in the document"


def test_the_computation_grader_sees_the_false_sum_and_nothing_in_a_clean_answer():
    assert states_a_computation(_PADDED_WITH_A_FALSE_SUM) == ["2 x 1", "000.00 + 1", "= 5"]
    assert states_a_computation(_PADDED) == []
    assert states_a_computation(_CLEAN) == []
    # a date is not arithmetic, and neither is a field quoted with its unit
    assert states_a_computation("**Due:** 2026-09-01\n**Rack:** 42U, qty 2") == []


def test_the_explanation_grader_sees_padding_and_not_the_field_line():
    assert len(explains_a_missing_field(_PADDED_WITH_A_FALSE_SUM)) == 3
    assert explains_a_missing_field(_PADDED) == [
        "The invoice lists a total of 5,200.00 USD but does not provide a separate line "
        "item for tax, nor does it specify if tax is included in the line items or the total."
    ]
    assert explains_a_missing_field(_CLEAN) == []
    # a clean contract answer that quotes a clause is not an explanation
    assert explains_a_missing_field(
        "**Parties:** Northwind Group Ltd and Acme Racks Ltd\n**Notice:** 90 days written "
        "notice (Clause 11)") == []


def test_the_field_rules_forbid_working_and_talk_about_a_missing_field():
    """F6. BASE tells every answer to "show the arithmetic" for a figure it
    worked out and to say the document is silent "in ONE line after" the
    answer; on the itemised tax-free invoice the strict block obeyed both,
    live: 4 of 8 answers on 57dcede added a sentence about the missing tax,
    and a verifier run stated a false sum. The field rules override both for
    the field lines (round 6: 0 computations, 0 explanations in 16 of 16),
    and round 7 kept them in the ONE field block, scoped to the field lines,
    so a judgement or a calculation the person asks for may still work with
    the numbers. The live test re-measures the behaviour."""
    q = "I need the total from this invoice and the tax amount"
    assert source_use.question_mode(q) == "extract"
    system = source_use.system_text(q)
    assert "NO ARITHMETIC IN THE FIELD LINES" in system
    assert "copy every figure exactly as printed" in system
    assert "no working beside it, not even to show where a total comes from" in system
    assert "with nothing about why it is missing, what the document shows instead" in system
    # ... and they bind the field lines only: the judgement may use numbers
    assert "each binds only its own part of the answer" in system
    assert "A calculation you are asked for is yours: show the working" in system
    # the rule names no failing wording: the model copies what a prompt quotes
    assert "=" not in source_use.FIELDS and " x " not in source_use.FIELDS
