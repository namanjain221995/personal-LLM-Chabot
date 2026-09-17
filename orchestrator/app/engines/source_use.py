"""How an answering model may USE an uploaded document (2026-09-17).

THE INCIDENT. The owner attached a 4-page Vertiv SmartRow brochure (rack
enclosures, row cooling, UPS, 2-12 racks, 10-135 kVA) and asked, in his own
words, "as I have dgx spark ?? is help Full ??". The platform answered:

    "The short answer is no. The Vertiv SmartRow document you shared does not
     mention NVIDIA DGX Spark or provide specific support for it. ... No DGX
     Mention: I searched the provided text, and there is no mention of DGX,
     Spark, or NVIDIA in the document. ... What you should do: Check NVIDIA
     Documentation ... Consult Vertiv"

Every sentence of that is true and the answer is still wrong: he did not ask
whether the brochure mentions DGX Spark, he asked whether the product is
useful to HIM. The cause was the document engine's system prompt — "You are a
careful document analyst ... Answer using what is actually in the document" —
a pure extractor persona. It answers ABOUT the document and refuses to advise,
so a decision question came back as a literal search report plus a referral to
the vendor.

THE RULE THIS MODULE ENCODES: a document is a SOURCE, not a cage. The answer
is built from the document, from general knowledge where the document is
silent, and from what the conversation already says about this person's
situation — with each part labelled, and nothing attributed to the document
that the document does not say.

THE SWITCH. Extraction must stay strict: an invoice, a contract or a form is
asked for field by field, and general knowledge has no business filling a
blank there. `question_mode` reads the SHAPE of the question — a decision
question ("is it helpful for me", "should I use it", "can I place X in it")
gets the advisory block, a field question ("extract the line items", "what is
the invoice total") gets the strict block, and anything else gets the base
rules alone, which already answer from the document first.

Consumed by app/engines/document.py. `HISTORY_DOC_GUIDANCE` is the same rule
in one paragraph, for the pinned "documents the user uploaded earlier" block
that main.py puts in front of LATER turns — those turns route to chat, where
the document survives only as history, and the refusal reappeared there
("Based on the provided Vertiv SmartRow document ...") on the very next turn.
"""
from __future__ import annotations

import re

#: The persona and the three sources, said to every document answer.
BASE = (
    "You are an expert advisor reading a document WITH the person. You are given the "
    "document's extracted text (the ENTIRE document was read; the excerpt shown is the "
    "part most relevant to the question) and, for PDFs, images of its pages. The document "
    "is a SOURCE, not a limit on what you may say.\n"
    "Answer the question the person actually asked — not the question \"what does this "
    "document say\". Build the answer from three sources and make clear which part is "
    "which:\n"
    "- THE DOCUMENT: its own figures, names, models and page numbers. Say \"the document "
    "says\" only for what it really says, and never invent a value, a model name or a "
    "specification that is not in it.\n"
    "- GENERAL KNOWLEDGE: where the document is silent, answer from what you know, and say "
    "that this part is not from the document.\n"
    "- THIS CONVERSATION: what the person has already told you about their situation — "
    "their hardware, their scale, their plans — is context you must use.\n"
    "When the document does not cover what was asked, say so in ONE opening line, then "
    "answer anyway from general knowledge and their situation, and close by naming what "
    "would settle it (a specific spec sheet, a named page, a measurement). \"The document "
    "does not mention it\" is never the whole answer and never a reason to send the person "
    "away."
)

#: Added when the question is a decision ("is it helpful for me?").
ADVISORY = (
    "\nTHIS IS A DECISION QUESTION. Give a real recommendation: a clear yes / no / it "
    "depends in the first lines, the reasoning behind it, and the numbers it turns on — "
    "the document's own figures where it has them, typical figures from general knowledge "
    "where it does not, each labelled as such. Compare the options when there are options, "
    "and say at what point the answer changes (what scale, load, budget). Telling the "
    "person to check the manufacturer's documentation or ask the vendor is a closing line "
    "for the one thing you genuinely cannot settle — never the answer itself."
)

#: Added when the question asks for fields out of an invoice, contract or form.
EXTRACTION = (
    "\nTHIS IS AN EXTRACTION QUESTION. Return ONLY what is actually in the document: the "
    "fields, values and figures as written, with page numbers where that helps. Do not "
    "fill a missing field from general knowledge, do not estimate it, and do not add "
    "advice — write \"not stated in the document\" for anything the document does not give."
)

#: How the answer is laid out (the round's shared structure rules).
STRUCTURE = (
    "\nFORMAT: lead with the answer. Use short markdown headings, **bold labels** and "
    "bullets, and a small markdown table when you compare options or scales. Do not "
    "restate or summarize the document before answering the question."
)

#: One paragraph of the same rule for the pinned document block main.py puts in
#: front of LATER turns, which route to chat (see the module docstring).
HISTORY_DOC_GUIDANCE = (
    "These documents are a SOURCE, not a limit on what you may say: answer the question "
    "the person actually asked, using the documents for what they contain (never claim "
    "they say something they do not), your general knowledge where they are silent — say "
    "which part is which — and what this conversation already tells you about the person's "
    "situation. A question about whether something suits them gets a real recommendation "
    "with reasoning and numbers, not a referral to the vendor. Do not open by restating "
    "the document."
)

#: Asking for fields out of a document: invoice, contract, form, table.
_EXTRACT_RE = re.compile(
    r"\b(extract|transcribe|itemi[sz]e|verbatim|word[- ]for[- ]word|"
    r"line items?|fields?|form fields?|fill (in|out)|key[- ]values?|"
    r"invoice (number|no|total|amount|date|value)|bill (number|amount)|"
    r"(po|purchase order|reference|policy|account|customer|vat|tax|gst(in)?|iban) "
    r"(number|id|no)|due date|payment terms|list (all|every|each|the) )\b",
    re.I,
)

#: An explicit ask for a judgement, whatever else the sentence contains.
_EXPLICIT_ADVICE_RE = re.compile(
    r"(should (i|we)\b|\b(i|we) should\b|do (i|we) (need|have to)\b|is it worth\b|worth it\b|"
    r"do you recommend\b|what do you (recommend|suggest|think)\b|"
    r"is (it|this|that) (help|use)ful\b|any (advice|recommendation)\b|"
    r"is (it|this|that) (a )?good\b|would you (recommend|suggest|buy)\b)",
    re.I,
)

#: The person is in the question ("as I have dgx spark ...").
_FIRST_PERSON_RE = re.compile(
    r"\b(i|i'm|im|i've|ive|my|mine|me|we|we're|our|ours|us)\b", re.I
)

#: ... and the question is about a choice, a fit or a benefit.
_DECISION_RE = re.compile(
    r"\b(help|helps|helpful|useful|usable|use|using|worth|need|needs|buy|buying|"
    r"purchase|should|suit|suits|suitable|suited|fit|fits|recommend|recommends|"
    r"recommended|recommendation|advice|advise|overkill|enough|sufficient|benefit|"
    r"benefits|good|better|best|right|choose|choice|option|options|compare|"
    r"comparison|place|placing|put|install|mount|deploy|setup|set up|run|plan|"
    r"planning|upgrade|scale|expand)\b",
    re.I,
)


def question_mode(question: str) -> str:
    """The shape of the question: "advise", "extract", or "" for neither.

    An explicit ask for a judgement wins over extraction wording, so "extract
    the totals and tell me whether I should renew" stays a decision question.
    """
    q = question or ""
    if _EXPLICIT_ADVICE_RE.search(q):
        return "advise"
    if _EXTRACT_RE.search(q):
        return "extract"
    if _FIRST_PERSON_RE.search(q) and _DECISION_RE.search(q):
        return "advise"
    return ""


def system_text(question: str = "") -> str:
    """The document route's system prompt for THIS question."""
    mode = question_mode(question)
    extra = ADVISORY if mode == "advise" else EXTRACTION if mode == "extract" else ""
    return BASE + extra + STRUCTURE
