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

THE SWITCH, and what QA found wrong with its first cut (2026-09-18). Four
modes, decided by the SHAPE of the question:

  extract          a FIELD of the document is named — total, tax, due date,
                   parties, term, governing law, invoice number, PO, account,
                   policy number, effective date, signatories, amounts — even
                   when the ask is polite ("I need the total from this
                   invoice"). Strict: only what the document says.
  advise           whether the thing suits this person, or which option to
                   pick ("is help Full ??", "should I use this", "is this
                   worth buying").
  extract+advise   both, EXPLICITLY. The fields come first under the strict
                   rules, the judgement after, clearly separated.
  "" (neutral)     anything else. Answer the question and stop.

QA's first-cut findings, all closed here. (1) "I need the total from this
invoice and the tax amount" routed to ADVISE — _EXTRACT_RE wanted the adjacent
bigram "invoice total" and never saw "total", while a bare "I" plus the very
generic verb "need" tripped the advisory fallback. The live answer then
invented "Tax Amount: 0.00 USD" for an invoice with no tax line. (2) "Who are
the parties to this contract, what is the term, and what is the governing
law?" routed to NEUTRAL, so BASE's general-knowledge permission applied to an
extraction task and the answer speculated about the governing law. (3) A
question the document fully answers collected an unasked "Context for Your
Setup" section. (4) 14 of 30 battery questions landed in the wrong mode.

So: the field vocabulary is named nouns, not bigrams; the advisory fallback
needs a genuine suitability word (need, use, run, plan, good and right are
gone from it); a naked "should I" inside a value question ("what supply water
temperature should I run?") is a value question, not a decision; a loose
advisory signal never beats a named field; every mode is forbidden to invent a
document value; and neutral now says, in words, to stop when the question is
answered.

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
    "specification that is not in it. A figure you worked out yourself from the "
    "document's figures is YOURS, not the document's — show the arithmetic and say so.\n"
    "- GENERAL KNOWLEDGE: where the document is silent, answer from what you know, and say "
    "that this part is not from the document.\n"
    "- THIS CONVERSATION: what the person has already told you about their situation — "
    "their hardware, their scale, their plans — is context you must use.\n"
    "Those three are sources, not a template for the answer: never use THE DOCUMENT, "
    "GENERAL KNOWLEDGE or THIS CONVERSATION as headings, never announce that you are "
    "answering from three sources, and never write a section to say a source is empty. "
    "Label a part inline, in a few words, where it belongs.\n"
    "NEVER PRESENT A VALUE AS THE DOCUMENT'S WHEN THE DOCUMENT DOES NOT GIVE IT. A field "
    "the document does not state is \"not stated in the document\" — never 0, 0.00, "
    "\"none\" and never a typical value put in its place. This binds every mode. It does "
    "not silence you: a figure from your own knowledge is welcome wherever the question "
    "calls for one, labelled as yours.\n"
    "When the document does not cover what was asked, DO NOT OPEN WITH THAT. Give your "
    "answer first, then say the document is silent in ONE line after it — never as the "
    "opening sentence, and never twice — and answer anyway from general knowledge and "
    "their situation, closing by naming what would settle it (a specific spec sheet, a "
    "named page, a measurement). \"The document does not mention it\" is never the whole "
    "answer, never the first thing you say, and never a reason to send the person away."
)

#: Added when the question is a decision ("is it helpful for me?").
ADVISORY = (
    "\nTHIS IS A DECISION QUESTION, AND IT IS ABOUT THE THING, NOT THE PAPERWORK. When "
    "the person asks whether \"this\" helps them, they mean the thing the document "
    "describes — the product, the method, the policy — not the document as a text. Judge "
    "the thing. \"The brochure does not provide full help\" is the same refusal as \"the "
    "document does not mention it\" wearing a verdict's clothes; \"the unit would cover "
    "your load, but it is more than you need at two nodes\" is an answer.\n"
    "Give a real recommendation: a clear yes / no / it depends in the first lines, "
    "the reasoning behind it, and the numbers it turns on — "
    "the document's own figures where it has them, typical figures from general knowledge "
    "where it does not, each labelled as such. Compare the options when there are options, "
    "and say at what point the answer changes (what scale, load, budget). Telling the "
    "person to check the manufacturer's documentation or ask the vendor is a closing line "
    "for the one thing you genuinely cannot settle — never the answer itself."
)

#: Added when the question asks for fields out of an invoice, contract or form.
#: "This overrides" is load-bearing: BASE grants general knowledge where the
#: document is silent, and QA watched that permission fill in a contract's
#: governing law with speculation about New York and England & Wales.
EXTRACTION = (
    "\nTHIS IS AN EXTRACTION QUESTION. Return ONLY what is actually in the document: the "
    "fields, values and figures as written, with page numbers where that helps. This "
    "OVERRIDES the general-knowledge permission above — for this answer the document is "
    "the only source. Do not fill a missing field from general knowledge, do not infer it "
    "from the rest of the document, do not estimate it, and do not write 0, 0.00 or "
    "\"none\" for a field the document never gives; write \"not stated in the document\". "
    "For this answer do not add advice, a recommendation, a next step, a caution or an "
    "offer of further help, and do not raise a discrepancy the person did not ask "
    "about. Give the fields, not the reasoning that found them: no thinking out loud, "
    "no self-correction in the answer."
)

#: Added when the question names fields AND explicitly asks for a judgement.
#: Extraction wins for the fields; the advice follows them, clearly separated.
EXTRACT_THEN_ADVISE = EXTRACTION + (
    "\nTHE PERSON ALSO ASKED FOR A JUDGEMENT. Answer in two parts, in this order. FIRST "
    "the fields, under their own heading, under the strict rules above. THEN, under a "
    "separate heading, the judgement: a clear yes / no / it depends, the reasoning, and "
    "the numbers it turns on — the document's figures where it has them, general "
    "knowledge where it does not, each labelled. Nothing in the second part may change, "
    "fill in or round a field in the first part, and a field that is not stated stays "
    "not stated even if the advice would be easier with a number there."
)

#: Added when the question is neither — it asks what the document says.
#: QA measured the padding this closes: "what cooling capacity does this
#: product offer?" was answered correctly in 303 characters by the old prompt
#: and in 1,030 by the new one, the difference being an unasked "Context for
#: Your Setup" section and a heat-load calculation nobody requested.
NEUTRAL = (
    "\nANSWER THE QUESTION AND STOP. Answer exactly what was asked, at the length the "
    "question deserves, and finish there. Do not append a section the person did not ask "
    "for — no \"context for your setup\", no next steps, no recommendation, no offer of "
    "further analysis, no questions back. When the document already answers the question, "
    "the document's answer IS the answer: do not pad it with general knowledge. The "
    "general-knowledge permission above is for what the document does NOT answer.\n"
    "ONE exception, because it is part of answering and not padding: when the document's "
    "own answer contradicts what is normally true, give the document's answer and say in "
    "ONE line that it differs from the usual figure, and what the usual figure is."
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
    "situation. A value the documents do not state is \"not stated in the document\", never "
    "a zero and never a typical value. A question about whether something suits them gets a "
    "real recommendation with reasoning and numbers, not a referral to the vendor. Do not "
    "open by restating the document."
)

# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

#: The ACT of extracting: "extract", "transcribe", "copy the table", "fill in".
_EXTRACT_VERB_RE = re.compile(
    r"\b(extract|transcribe|itemi[sz]e|verbatim|word[- ]for[- ]word|"
    r"fill (in|out)|key[- ]values?|line items?|form fields?|fields?|"
    r"copy (the|this|that) (table|text|list|wording|clause|paragraph)|"
    r"pull (the )?(numbers?|figures?|values?|fields?|data|amounts?)|"
    r"read (out|off) the|quote the|"
    r"list (all|every|each|the) )",
    re.I,
)

#: A NAMED FIELD of a document. The first cut asked for adjacent bigrams
#: ("invoice total"), so "the total from this invoice" fell through to the
#: advisory fallback and the answer invented a tax figure; these are the nouns
#: themselves. Deliberately generic: a question that names a document field is
#: an extraction question however politely it is phrased ("I need the total").
#: "amount" is left broad — "what amount of heat does it produce" then takes
#: the strict block, which answers from the document and says "not stated" for
#: the rest. That is the safe direction to be wrong in.
_FIELD_NOUN_RE = re.compile(
    r"\b("
    r"totals?|subtotals?|grand total|amounts?|amount due|balance due|"
    r"tax(es)?|vat|gst(in)?|sales tax|"
    r"fees?|unit price|line price|"
    r"invoice (number|no|id|total|amount|date|value)|bill (number|amount)|"
    r"(po|purchase order|reference|policy|account|customer|vendor|supplier|"
    r"vat|gst|iban|serial|model|part|order|tracking) (number|no|id)|"
    r"(vendor|supplier|customer|payee|counterparty|bill to|ship to) "
    r"(name|address|details)|"
    r"due date|payment terms|terms of payment|"
    r"(effective|issue|start|end|expiry|expiration|renewal|commencement|"
    r"delivery|invoice) date|date of issue|"
    r"parties|governing law|jurisdiction|termination|notice period|"
    r"signator(y|ies)|signature|signed by|"
    r"quantit(y|ies)|\bqty\b|currency|discount|"
    r"owes?|owed|"
    r"list (all|every|each) )\b"
    r"|\bdue\b(?! to)"
    r"|(?<!in )(?<!long-)(?<!short-)(?<!mid-)\bterms?\b",
    re.I,
)

#: An explicit ask for a judgement, whatever else the sentence contains.
_EXPLICIT_ADVICE_RE = re.compile(
    r"(\b(i|we) should\b|do (i|we) (need|have to)\b|"
    r"is (it|this|that) worth\b|worth (it|buying|having|the (money|cost|price))\b|"
    r"do you recommend\b|what do you (recommend|suggest|think)\b|"
    r"is (it|this|that) (help|use)ful\b|any (advice|recommendation)\b|"
    r"is (it|this|that) (a )?good\b|would you (recommend|suggest|buy)\b|"
    r"which (one|option|model|version|configuration|config|product) should\b)",
    re.I,
)

#: "should I / should we" — a judgement ask MOST of the time.
_SHOULD_RE = re.compile(r"\bshould (i|we)\b", re.I)

#: ... except inside a question that asks for a VALUE. "What supply water
#: temperature should I run for this unit?" wants the number the document
#: gives, and the advisory block's "clear yes / no / it depends" is the wrong
#: shape for it. A decision VERB after "should I" overrides the exception.
_VALUE_WH_RE = re.compile(
    r"^\W*(what|when|who|where|how (much|many|long|often))\b", re.I
)
_DECIDE_AFTER_SHOULD_RE = re.compile(
    r"\bshould (i|we) (\w+ ){0,2}(buy|purchase|get|use|choose|pick|go with|order|"
    r"renew|sign|upgrade|replace|switch|invest|adopt|keep|cancel|do)\b",
    re.I,
)

#: The person is in the question ("as I have dgx spark ...").
_FIRST_PERSON_RE = re.compile(
    r"\b(i|i'm|im|i've|ive|my|mine|me|we|we're|our|ours|us)\b", re.I
)

#: ... and the question is about a choice, a fit or a benefit. The first cut
#: also carried need, needs, use, using, run, plan, good and right, which made
#: almost any first-person sentence a decision — "I need to know the total" and
#: "give me a summary of the key points I need" both became advice. A word
#: stays here only if it asks whether the thing SUITS the person.
_SUITABILITY_RE = re.compile(
    r"\b(help|helps|helping|helpful|useful|usable|worth|overkill|enough|"
    r"sufficient|benefit|benefits|beneficial|suit|suits|suitable|suited|"
    r"fits?|compatible|compatibility|works? (for|with|in)|"
    r"better|best|choose|choice|options?|compare|comparison|versus|vs|"
    r"recommend|recommends|recommended|recommendation|advice|advise|"
    r"allowed|permitted|may (i|we)|can (i|we)|"
    r"place|placing|install|mount|deploy|rack|host|migrate|"
    r"put|store|buy|buying|purchase|invest|upgrade|"
    r"makes? sense)\b",
    re.I,
)


def _wants_fields(q: str) -> bool:
    """True when the question names a document field or asks to extract."""
    return bool(_EXTRACT_VERB_RE.search(q) or _FIELD_NOUN_RE.search(q))


def _explicit_advice(q: str) -> bool:
    """True when the person asked for a judgement in so many words."""
    if _EXPLICIT_ADVICE_RE.search(q):
        return True
    if _SHOULD_RE.search(q):
        return bool(_DECIDE_AFTER_SHOULD_RE.search(q)) or not _VALUE_WH_RE.match(q)
    return False


def question_mode(question: str) -> str:
    """The shape of the question: "advise", "extract", "extract+advise", "".

    A named field wins over a merely IMPLIED judgement, so "I need the total
    from this invoice" is extraction however first-person it sounds. When the
    judgement is EXPLICIT ("extract the payment terms and tell me whether I
    should renew") the person asked for both, and both are answered — fields
    first, under the strict rules, advice after and clearly separated.
    """
    q = question or ""
    fields = _wants_fields(q)
    explicit = _explicit_advice(q)
    if fields:
        return "extract+advise" if explicit else "extract"
    if explicit:
        return "advise"
    if _FIRST_PERSON_RE.search(q) and _SUITABILITY_RE.search(q):
        return "advise"
    return ""


_BLOCKS = {
    "advise": ADVISORY,
    "extract": EXTRACTION,
    "extract+advise": EXTRACT_THEN_ADVISE,
    "": NEUTRAL,
}


def system_text(question: str = "") -> str:
    """The document route's system prompt for THIS question."""
    return BASE + _BLOCKS[question_mode(question)] + STRUCTURE
