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

import pytest

from app.engines import source_use
from app.engines.document import run_pdf_engine_multi
from tests.document_answer_grader import (cites_document, gives_recommendation,
                                          opens_with_refusal, referral_only)

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
    system = source_use.system_text(OWNER_QUESTION)
    assert "SOURCE, not a limit" in system
    for source in ("THE DOCUMENT", "GENERAL KNOWLEDGE", "THIS CONVERSATION"):
        assert source in system, f"{source} missing from the document prompt"
    assert "which part is which" in system


def test_the_three_sources_are_not_an_answer_template():
    """Live, the model turned the three source names into three headings and
    wrote a "THIS CONVERSATION: no context was provided" section under them."""
    for question in (OWNER_QUESTION, "extract the invoice total", "summarize this"):
        system = source_use.system_text(question)
        assert "sources, not a template for the answer" in system
        assert "never use THE DOCUMENT, GENERAL KNOWLEDGE or THIS CONVERSATION as headings" in system
        assert "never write a section to say a source is empty" in system


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
    assert "THIS CONVERSATION" in messages[0]["content"]


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
    assert "no next steps, no recommendation" in system
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
    assert "ONE exception" in system
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
