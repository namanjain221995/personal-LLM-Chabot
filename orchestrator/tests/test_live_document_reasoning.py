"""LIVE: the owner's document turn against a real vLLM endpoint — opt-in.

Skipped unless LIVE_VLLM_BASE_URL is set (the suite stays offline by default,
per conftest's contract). On the DGX box the main model is published at
http://127.0.0.1:8000/v1 by the published overlay:

    LIVE_VLLM_BASE_URL=http://127.0.0.1:8000/v1 .venv/bin/python -m pytest \
        tests/test_live_document_reasoning.py -q

It calls the ENGINE, never the production orchestrator: run_pdf_engine_multi
with its own emitter and no conversation id, so nothing is stored and no
running container is touched. Fast effort, thinking off — the mode the owner
was in when the refusal happened.

The assertion is the shape of the answer, by the same graders the offline test
pins against the two real 2026-09-17 answers: a recommendation, the brochure's
own figures quoted, and no refusal in the opening lines.
"""
import asyncio
import base64
import os
import re

import pytest

from app.config import settings
from app.engines.document import run_pdf_engine_multi
from tests.document_answer_grader import (cites_document, explains_a_missing_field,
                                          gives_recommendation, opens_with_refusal,
                                          referral_only, source_named_headings,
                                          source_named_labels, states_a_computation)
from tests.test_document_reasoning import (BROCHURE, BROCHURE_FIGURES,
                                           OWNER_QUESTION)
from tests.test_document_reasoning import \
    INVOICE_WITHOUT_TAX as ITEMISED_INVOICE_WITHOUT_TAX

LIVE = os.environ.get("LIVE_VLLM_BASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not LIVE, reason="LIVE_VLLM_BASE_URL not set — live vLLM test is opt-in"
)


class Rec:
    def __init__(self):
        self.events = []

    async def emit(self, e, d):
        self.events.append((e, d))


@pytest.fixture()
def live(monkeypatch):
    monkeypatch.setattr(settings, "openai_base_url", LIVE)
    return LIVE


def _answer(question: str, history=()) -> str:
    b64 = base64.b64encode(BROCHURE.encode()).decode()
    return asyncio.run(
        run_pdf_engine_multi(
            question,
            [("vertiv-smartrow.pdf", b64)],
            list(history),
            Rec().emit,
            effort="fast",  # llm.wants_thinking("smart", "fast") is False
        )
    )


def test_the_owner_question_gets_an_answer_not_a_search_report(live):
    history = [
        {"role": "user", "content": "I have 2 DGX Sparks and plan to grow to 20."},
        {"role": "assistant", "content": "Noted — a 20-node DGX Spark cluster."},
    ]
    answer = _answer(OWNER_QUESTION, history)
    assert answer.strip(), "the live engine returned nothing"
    assert not opens_with_refusal(answer), f"opened with a refusal:\n{answer}"
    assert gives_recommendation(answer), f"no recommendation:\n{answer}"
    assert cites_document(answer, BROCHURE_FIGURES), f"no document figure:\n{answer}"
    assert not referral_only(answer), f"referral instead of an answer:\n{answer}"


def test_an_invoice_extraction_stays_inside_the_document(live):
    invoice = (
        "INVOICE 42\nVendor: Acme Racks Ltd\nDate: 2026-08-01\n"
        "Line 1: Rack enclosure, 1,000.00 USD\nTax: 250.00 USD\n"
        "Total: 1,250.00 USD\nDue: 2026-09-01"
    )
    b64 = base64.b64encode(invoice.encode()).decode()
    answer = asyncio.run(
        run_pdf_engine_multi(
            "extract the invoice total, the due date and the vendor",
            [("invoice.pdf", b64)],
            [],
            Rec().emit,
            effort="fast",
        )
    )
    assert "1,250.00" in answer or "1250.00" in answer
    assert "2026-09-01" in answer
    assert "Acme Racks" in answer


#: The contract the 2026-09-18 recheck reproduced the blocker against.
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

#: The phrasing that reproduced the original incident on the delivered patch.
LONG_TERM_QUESTION = "is this the right choice for us in the long term?"


def _answer_about(question: str, body: str, name: str = "doc.pdf", history=()) -> str:
    b64 = base64.b64encode(body.encode()).decode()
    return asyncio.run(
        run_pdf_engine_multi(
            question, [(name, b64)], list(history), Rec().emit, effort="fast"
        )
    )


@pytest.mark.parametrize("run", [0, 1, 2])
def test_a_long_term_decision_question_gets_a_verdict_not_a_refusal(live, run):
    """The 2026-09-18 blocker, live, three times.

    On the delivered patch the word "term" inside "long term" matched the
    contract field, the question took the strict extraction block, and both
    runs opened "**Not stated in the document.** The provided Master Services
    Agreement excerpt ... does not contain any information regarding the
    long-term strategic fit ... Therefore, I cannot determine if this is the
    'right choice' based on the document alone." That is the owner's original
    complaint, reproduced by the round that exists to prevent it.
    """
    from app.engines import source_use

    assert source_use.question_mode(LONG_TERM_QUESTION) == "advise"
    answer = _answer_about(LONG_TERM_QUESTION, CONTRACT, "msa.pdf")
    assert answer.strip(), "the live engine returned nothing"
    assert not opens_with_refusal(answer), f"opened with a refusal:\n{answer}"
    assert gives_recommendation(answer), f"no recommendation:\n{answer}"


INVOICE_WITHOUT_TAX = (
    "INVOICE\n\nInvoice number: INV-2026-0912\nVendor: Acme Racks Ltd\n"
    "Date of issue: 2026-08-01\nTotal: 5,200.00 USD\nDue: 2026-09-01\n"
)


@pytest.mark.parametrize(
    "question,body,name",
    [
        ("I need the total from this invoice and the tax amount",
         INVOICE_WITHOUT_TAX, "invoice.pdf"),
        ("Who are the parties to this contract, what is the term, and what is "
         "the governing law?", CONTRACT, "msa.pdf"),
        ("what does clause 11 say?", CONTRACT, "msa.pdf"),
    ],
)
def test_a_short_answer_never_names_a_source_in_a_heading(live, question, body, name):
    """Extraction and neutral answers: 0 of 27 across five live passes on
    2026-09-18 — the short modes do not do this at all, so a flat zero here
    is a real assertion and not an aspiration."""
    answer = _answer_about(question, body, name)
    bad = source_named_headings(answer)
    assert not bad, f"headings named a source, not a subject: {bad}\n{answer}"
    # ... and the BOLD LABEL half of the same rule, which had no instrument
    # until source_named_labels() (2026-09-18). Zero here is a measurement:
    # 0 of 6 short answers across two passes, the same result the heading
    # assertion above rests on.
    labels = source_named_labels(answer)
    assert not labels, f"a bold label named a source, not a subject: {labels}\n{answer}"


def test_a_long_answer_hardly_ever_names_a_source_in_a_heading(live):
    """The rule BASE could only be READ to hold, until this.

    A graded run of the owner's turn came back structured as "### 1. The
    Document's Limitations", "### 2. General Knowledge: DGX Spark
    Requirements", "### 3. This Conversation: Your Scale"; another printed the
    three names in bold verbatim; a later one rebuilt the same tour as "### 1.
    What the Document Says", "### 2. What I Know", "### 3. What This
    Conversation Tells Me About You".

    The threshold is 1, not 0, and that is a measurement, not a hedge. Five
    live passes on 2026-09-18, 3 to 12 long answers each: the rate fell from 2
    of 3 on the delivered patch to 1 of 12, and stayed at 1 of 12 through
    three further tightenings of BASE, ADVISORY and the FORMAT block. The last
    survivor was "**What This Conversation Tells Me**". It did not reach a
    reliable zero, and a prompt rule never gives one — the strict extraction
    block has the same property (an answer sometimes adds advice it was told
    not to). A flat `== 0` here would be a flaky test claiming a guarantee the
    mechanism cannot make. Two or more of six is the regression this catches,
    and the delivered patch would fail it.
    """
    history = [
        {"role": "user", "content": "I have 2 DGX Sparks and plan to grow to 20."},
        {"role": "assistant", "content": "Noted — a 20-node DGX Spark cluster."},
    ]
    answers = [_answer(OWNER_QUESTION, history) for _ in range(4)]
    answers += [_answer_about(LONG_TERM_QUESTION, CONTRACT, "msa.pdf") for _ in range(2)]
    named = {i: source_named_headings(a) for i, a in enumerate(answers)}
    offending = [i for i, bad in named.items() if bad]
    assert len(offending) <= 1, (
        "more than one of six long answers laid itself out by source: "
        + repr({i: named[i] for i in offending})
    )


def test_a_long_answer_does_not_do_the_same_thing_in_its_bold_labels(live):
    """NOT an assertion — a MEASUREMENT printed where the next round can read
    it, and deliberately not a threshold.

    BASE's rule covers "every heading AND every bold label ... including a
    label inside a section". The heading half is measured and held at 1 of 6
    by the test above. The label half had no instrument at all until
    source_named_labels(), and the first two passes with one say it is a
    commoner failure than the heading, not a rarer one: 1 of 6 long answers in
    pass one ("*   **Document Says:** ..." three times in one answer), 3 of 6
    in pass two ("*   **What the document says:** ...", "*   **The Document
    Says:** ...", "*   **Document Limit:** ..."), so 4 of 12. Short answers
    were clean both times, 0 of 6, and that IS asserted above.

    A threshold pinned at that rate would be a test that passes while the rule
    is broken a third of the time, and one pinned lower would fail on the
    prompt as it stands. Closing it needs a BASE change measured over many
    runs, which is a piece of work and not a line in this one. So this records
    the number and fails only if the instrument itself stops working.
    """
    history = [
        {"role": "user", "content": "I have 2 DGX Sparks and plan to grow to 20."},
        {"role": "assistant", "content": "Noted — a 20-node DGX Spark cluster."},
    ]
    answers = [_answer(OWNER_QUESTION, history) for _ in range(2)]
    answers += [_answer_about(LONG_TERM_QUESTION, CONTRACT, "msa.pdf")]
    labelled = {i: source_named_labels(a) for i, a in enumerate(answers)}
    assert all(a.strip() for a in answers), "the live engine returned nothing"
    print("source-named bold labels per long answer:", labelled)


@pytest.mark.parametrize("run", [0, 1, 2])
def test_a_strict_answer_shows_no_working_and_explains_no_missing_field(live, run):
    """F6 (round 6), live. The ITEMISED tax-free invoice -- two line items, a
    total, no tax line -- is the one that drew the padding: on 57dcede 4 of 8
    strict answers added a sentence about the missing tax ("The invoice lists
    a total of 5,200.00 USD but does not provide a separate line item or
    breakdown for tax"), and a round-5 verifier run stated a FALSE sum, "2 x
    1,000.00 + 1 x 4,200.00 = 5,200.00". A strict answer copies fields; it
    never computes and never explains an absence. Zero is a measurement, not
    a hope: with the round-6 strict block, 16 of 16 runs in two batches had no
    computation and no explanation, the total, and the tax "not stated"."""
    from app.engines import source_use

    question = "I need the total from this invoice and the tax amount"
    assert source_use.question_mode(question) == "extract"
    answer = _answer_about(question, ITEMISED_INVOICE_WITHOUT_TAX, "invoice.pdf")
    assert "5,200.00" in answer or "5200.00" in answer, answer
    assert re.search(r"tax[^\n]{0,60}not stated", answer, re.I), answer
    assert not states_a_computation(answer), f"stated a computation:\n{answer}"
    assert not explains_a_missing_field(answer), f"explained a missing field:\n{answer}"
