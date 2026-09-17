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

import pytest

from app.config import settings
from app.engines.document import run_pdf_engine_multi
from tests.document_answer_grader import (cites_document, gives_recommendation,
                                          opens_with_refusal, referral_only)
from tests.test_document_reasoning import (BROCHURE, BROCHURE_FIGURES,
                                           OWNER_QUESTION)

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
