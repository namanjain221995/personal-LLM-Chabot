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


def test_a_silent_document_is_one_line_then_an_answer():
    system = source_use.system_text(OWNER_QUESTION)
    assert "ONE opening line" in system
    assert "answer anyway" in system
    assert "never the whole answer" in system
    assert "what would settle it" in system


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


def test_an_explicit_judgement_beats_extraction_wording():
    """"...and tell me whether I should renew" is a decision, not a form."""
    q = "extract the payment terms and tell me whether I should renew"
    assert source_use.question_mode(q) == "advise"


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
