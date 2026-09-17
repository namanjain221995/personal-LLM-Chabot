r"""How an answering model may USE an uploaded document (2026-09-17).

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

THE SWITCH. Four modes, decided by the SHAPE of the question:

  extract          a FIELD of the document is named — total, tax, due date,
                   parties, the term of the contract, governing law, invoice
                   number, PO, signatories — or the person asked for the ACT
                   of extracting. Strict: only what the document says.
  advise           whether the thing suits this person, or which option to
                   pick ("is help Full ??", "should I use this", "is this
                   worth buying", "does this make sense for two nodes").
  extract+advise   both. The fields come first under the strict rules, the
                   judgement after, clearly separated.
  "" (neutral)     anything else. Answer what was asked and stop — which is
                   NOT the same as refusing to judge.

THE FAILURE DIRECTION IS NOT SYMMETRIC, AND THAT IS THE WHOLE DESIGN. Calling
a question advisory when it was really a field ask costs a sentence of
context. Calling a decision question an extraction reproduces the incident
above: the strict block takes away the general-knowledge permission and the
answer becomes a report on the paperwork. So strict extraction is chosen only
on HIGH-PRECISION evidence, and ANY decision signal in the question beats a
field match — the result is extract+advise, never extraction alone.

WHY A SCORED CLASSIFIER AND NOT MORE REGEX (2026-09-18). The first two cuts
were one alternation per signal, and the second cut tried to carve ordinary
English out of the field list with lookbehinds:

    r"|(?<!in )(?<!long-)(?<!short-)(?<!mid-)\bterms?\b"

Those exclude the HYPHENATED forms only. "is this the right choice for us in
the long term?" — unhyphenated, the ordinary English noun phrase — matched
\bterms?\b, took the strict extraction block, and answered, live, twice out of
two runs: "**Not stated in the document.** The provided Master Services
Agreement excerpt ... does not contain any information regarding the long-term
strategic fit ... I cannot determine if this is the 'right choice' based on
the document alone." That is the owner's complaint word for word. Every new
alternation had the same shape of hole ("in terms of", "the total picture",
"the amount of heat", "the whole party"), and each patch was invisible until
something reproduced it live.

The instrument is wrong for the job, not the alternations. What decides the
mode is not "does the word 'term' appear" but "is a field of the document
being NAMED", and that is a question about the word's CONTEXT. So:

  1. ordinary-English phrases are MASKED out of the question first, by name,
     so no later pattern can see them at all ("long term", "in terms of",
     "the whole party", "the total picture", "the amount of heat");
  2. field words are split in two. An UNAMBIGUOUS one ("invoice number", "due
     date", "governing law", "notice period", "who signed") is worth full
     marks wherever it appears. An AMBIGUOUS one ("term", "total", "amount",
     "party", "value", "rate", "date", "fee", "price") is worth full marks
     only inside a FIELD FRAME — beside a document word, "the term OF THE
     contract", "WHAT IS the total", "GIVE ME the total" — and worth almost
     nothing on its own;
  3. the two thresholds differ on purpose: one decision signal is enough to
     make a question advisory, strict extraction needs two points of field
     evidence. That asymmetry IS the safe-failure rule, in the code, instead
     of in a comment nobody can test.

The evidence is returned with the mode (`classify`), so a misroute can be read
off in one line instead of bisected through a lookbehind chain.

Consumed by app/engines/document.py. `HISTORY_DOC_GUIDANCE` is the same rule
in one paragraph, for the pinned "documents the user uploaded earlier" block
that main.py puts in front of LATER turns — those turns route to chat, where
the document survives only as history, and the refusal reappeared there
("Based on the provided Vertiv SmartRow document ...") on the very next turn.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as _dataclass_field
from typing import List, Tuple

#: The persona and the three sources, said to every document answer.
#:
#: The three sources are described in lower case and NEVER as three shouty
#: labels. The 2026-09-18 recheck watched the model mirror the prompt's own
#: layout: told about "THE DOCUMENT / GENERAL KNOWLEDGE / THIS CONVERSATION",
#: one graded run of the owner's turn came back structured as "### 1. The
#: Document's Limitations", "### 2. General Knowledge: DGX Spark
#: Requirements", "### 3. This Conversation: Your Scale", and another printed
#: the three names in bold, verbatim, as its headings. A ban on the exact
#: uppercase strings did not stop either one — the model was copying the
#: SHAPE. So the shape is gone from the prompt, and two weaker forms of the
#: ban were measured live before this one. A positive rule plus a ban on the
#: three names let 2 of 6 long answers head a section "### 1. The Document's
#: Limits (Capacity)" or "### The Document's Terms" — the model reads "names a
#: subject" as satisfied by a possessive. Spelling the rewrite out with the
#: offending heading QUOTED was worse: the next run came back with "**The
#: Document's Limits:**" twice in one answer, the exact phrase the prompt had
#: just quoted. So the rule names no bad heading at all. It is a containment
#: test the model can apply to each heading as it writes it, and every example
#: in it is of a GOOD heading.
BASE = (
    "You are an expert advisor reading a document WITH the person. You are given the "
    "document's extracted text (the ENTIRE document was read; the excerpt shown is the "
    "part most relevant to the question) and, for PDFs, images of its pages. The document "
    "is a SOURCE, not a limit on what you may say.\n"
    "Answer the question the person actually asked — not the question \"what does this "
    "document say\". Three things go into the answer, and the person must be able to see "
    "which part is which:\n"
    "- what the document itself says: its own figures, names, models and page numbers. Say "
    "\"the document says\" only for what it really says, and never invent a value, a model "
    "name or a specification that is not in it. A figure you worked out yourself from the "
    "document's figures is YOURS, not the document's — show the arithmetic and say so.\n"
    "- what you know: where the document is silent, answer from your own knowledge, and say "
    "that this part is not from the document.\n"
    "- what this conversation already tells you about the person — their hardware, their "
    "scale, their plans — which is context you must use.\n"
    "THOSE THREE ARE WHERE THE ANSWER COMES FROM, NOT HOW IT IS LAID OUT. Lay the answer "
    "out by the QUESTION, never by source. Every heading and every bold label names the "
    "SUBJECT underneath it \u2014 \"Power draw\", \"Cooling capacity\", \"Cost at 20 "
    "nodes\", \"What would change this\". CHECK EVERY HEADING AND EVERY BOLD LABEL "
    "BEFORE YOU WRITE IT, including a label inside a section: if it "
    "contains the word \"document\", \"knowledge\" or \"conversation\" in any form or "
    "capitalisation, it is naming a source, and you rewrite it as the subject it is "
    "really about. No section exists to report what a source holds or lacks; those facts "
    "belong inside the subject sections that use them. Where a fact came from is said in "
    "the sentence that uses it, in a few words \u2014 \"the brochure lists 4 x 45 kW\", "
    "\"from general knowledge, a Spark draws about 240 W\" \u2014 never as a heading. "
    "Never announce that you are answering from more than one source.\n"
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
    "for the one thing you genuinely cannot settle \u2014 never the answer itself.\n"
    "SHAPE OF THIS ANSWER: the verdict, then the two or three things it turns on \u2014 "
    "each under a heading naming THAT THING (\"The 24-month term\", \"Cost at 20 "
    "nodes\", \"Getting out\") \u2014 then, in one short section, what would change the "
    "answer. It is never a tour of your sources: \"what the paper says\", \"what I "
    "know\" and \"what this conversation tells me\" are not sections, and a numbered "
    "list of them is the answer laid out backwards."
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

#: Added when the question names fields AND asks for a judgement.
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
#:
#: QA measured the padding this closes: "what cooling capacity does this
#: product offer?" was answered correctly in 303 characters by the old prompt
#: and in 1,030 by the new one, the difference being an unasked "Context for
#: Your Setup" section and a heat-load calculation nobody requested.
#:
#: The second half exists because the first cut of this block said "no next
#: steps, no recommendation", which turned every question the switch failed to
#: recognise as a decision into a refusal to judge. Stopping early and refusing
#: to judge are different instructions, and only one of them was wanted.
NEUTRAL = (
    "\nANSWER THE QUESTION AND STOP. Answer exactly what was asked, at the length the "
    "question deserves, and finish there. Do not append a section the person did not ask "
    "for — no \"context for your setup\", no next steps, no offer of further analysis, no "
    "questions back. When the document already answers the question, the document's answer "
    "IS the answer: do not pad it with general knowledge. The general-knowledge permission "
    "above is for what the document does NOT answer.\n"
    "STOPPING IS NOT REFUSING TO JUDGE. Do not volunteer a verdict nobody asked for — but "
    "if the question does turn on one, give it plainly in a line or two. Never answer a "
    "question about whether something suits this person by describing the document "
    "instead.\n"
    "ONE more exception, because it is part of answering and not padding: when the "
    "document's own answer contradicts what is normally true, give the document's answer "
    "and say in ONE line that it differs from the usual figure, and what the usual figure "
    "is."
)

#: How the answer is laid out (the round's shared structure rules).
STRUCTURE = (
    "\nFORMAT: lead with the answer. Use short markdown headings, **bold labels** and "
    "bullets, and a small markdown table when you compare options or scales. Do not "
    "restate or summarize the document before answering the question. A heading or a bold "
    "label names the value or the subject it introduces — **Total:**, **Verdict:**, "
    "**Power draw:** \u2014 never the source the fact came from: if a heading would "
    "contain the word \"document\", \"knowledge\" or \"conversation\", rewrite it as "
    "the thing it is about before you write it."
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
# The switch: a scored classifier over explicit signals.
#
# Points, and why they are not symmetric (see the module docstring):
#   a field signal must reach 2 before the strict extraction block is chosen;
#   one decision signal, worth 1, is enough to make the question advisory.
# Getting a question wrong towards advice costs a sentence of context. Getting
# it wrong towards extraction is the incident this module exists to prevent.
# ---------------------------------------------------------------------------

#: Full marks: this evidence names a field of the document, or the act of
#: extracting, and needs no help from context.
_STRONG = 2
#: Nearly nothing: a word that is a field name in a document and an ordinary
#: English word everywhere else. One of these alone never chooses extraction;
#: two independent ones in the same question do.
_WEAK = 1

_FIELD_THRESHOLD = 2
_DECISION_THRESHOLD = 1

# --- step 1: ordinary English, masked out before anything looks for fields ---

#: Phrases in which a field word is NOT a field. Every one of these was
#: measured routing a question into strict extraction: "long term" (the
#: 2026-09-18 blocker, reproduced live), "in terms of", "the amount of heat",
#: "the total picture", "the whole party". They are blanked — with spaces, so
#: the offsets of everything else survive — before a single field pattern runs,
#: which is why no later pattern needs a lookbehind.
_ORDINARY_ENGLISH = re.compile(
    r"\b(?:long|longer|short|shorter|medium|mid|near|immediate)[ -]term(?:s)?\b"
    r"|\bin\s+the\s+(?:long|short|medium|mid|near)\s+run\b"
    r"|\bin\s+terms?\s+of\b"
    r"|\bterms?\s+of\s+reference\b"
    r"|\btotal\s+(?:picture|number|mess|disaster|failure|waste|nonsense|rubbish|"
    r"recall|silence|eclipse)\b"
    r"|\bin\s+total\b|\btotal\s+cost\s+of\s+ownership\b"
    r"|\b(?:the\s+)?(?:whole|entire|office|dinner|christmas|birthday|launch)\s+part(?:y|ies)\b"
    r"|\bpart(?:y|ies)\s+(?:to|at)\s+the\s+(?:discussion|conversation|call|meeting|"
    r"table|office|house|weekend|party)\b"
    r"|\bthird[ -]part(?:y|ies)\b"
    r"|\bamounts?\s+of\s+(?!(?:the\s+|this\s+|that\s+)?(?:invoice|bill|money|payment|"
    r"tax|vat|gst|fees?|discount|deposit|credit|debt|cash|refund|charge))"
    r"|\b(?:a\s+)?numbers?\s+of\s+(?!(?:the\s+|this\s+|that\s+)?(?:invoice|contract|"
    r"agreement|order|purchase|policy|account|customer|vendor|supplier|part|serial))"
    r"|\bperiods?\s+of\s+(?:time|instability|growth|silence|notice\s+given)\b"
    r"|\bup[ -]to[ -]date\b|\bto\s+date\b|\bdates?\s+back\b"
    r"|\bvalue\s+(?:for|of)\s+money\b|\b(?:good|best|great|poor|better)\s+value\b"
    r"|\bvalue\s+proposition\b|\badded\s+value\b|\bvalues?\s+of\s+(?:the\s+)?"
    r"(?:this\s+)?(?:company|team|business|organisation|organization)\b"
    r"|\b(?:flow|air\s?flow|data|bit|clock|refresh|sample|sampling|failure|success|"
    r"transfer|heat|cooling|discharge|charge|error|churn|frame|growth|hit)[ -]rates?\b"
    r"|\bat\s+any\s+rate\b|\bfirst[ -]rate\b"
    r"|\bbest\s+practices?\b"
    r"|\bbalance\s+of\s+(?:power|probabilit(?:y|ies)|the\s+\w+)\b|\bload[ -]?balanc\w*\b"
    r"|\bquantit(?:y|ies)\s+of\s+(?:heat|air|water|noise|work|light|data|traffic)\b"
    r"|\bprices?\s+(?:of|for)\s+(?:electricity|power|energy|fuel|land)\b"
    r"|\bdue\s+to\b|\bin\s+due\s+course\b|\bdue\s+diligence\b",
    re.I,
)


def _mask_ordinary_english(question: str) -> str:
    """Blank the phrases in which a field word is ordinary English.

    Same length out as in, so every later offset still means what it meant.
    """
    return _ORDINARY_ENGLISH.sub(lambda m: " " * (m.end() - m.start()), question or "")


# --- step 2: field evidence -------------------------------------------------

#: The ACT of extracting: the verb the person used says what they want done to
#: the document, whatever nouns follow it.
_EXTRACT_VERB_RE = re.compile(
    r"\b(?:extract|transcribe|itemi[sz]e|verbatim|word[- ]for[- ]word"
    r"|fill\s+(?:in|out)"
    r"|copy\s+(?:the|this|that)\s+(?:table|text|list|wording|clause|paragraph|section|block)"
    r"|pull\s+(?:the\s+|out\s+the\s+)?(?:numbers?|figures?|values?|fields?|data|amounts?"
    r"|totals?|fees?|line\s+items?|details?|terms?|dates?|prices?|quantit(?:y|ies))"
    r"|read\s+(?:out|off)\s+the"
    r"|quote\s+(?:the|this|that|clause|section)"
    r"|(?:form|key|all|which|these|the)\s+fields?\b"
    r"|key[- ]values?"
    r"|list\s+(?:all|every|each|the|out)\b)",
    re.I,
)

#: A field of a document that ordinary English does not also use. Multi-word or
#: domain-specific enough to be worth full marks anywhere in the sentence.
_UNAMBIGUOUS_FIELD_RE = re.compile(
    r"\b(?:"
    r"grand\s+totals?|sub[- ]?totals?|amounts?\s+(?:due|payable|outstanding)|balance\s+due"
    r"|totals?\s+due|line\s+items?|unit\s+price|line\s+price|list\s+price"
    r"|tax(?:es)?|vat|gst(?:in)?|sales\s+tax|tax\s+(?:amount|rate|point)"
    r"|invoice\s+(?:number|no|id|total|amount|date|value|reference)"
    r"|bill\s+(?:number|amount|date)"
    r"|(?:po|purchase\s+order|reference|policy|account|customer|vendor|supplier|"
    r"vat|gst|iban|serial|model|part|order|tracking|claim|case|registration)"
    r"\s+(?:number|no\.?|id)"
    r"|(?:vendor|supplier|customer|payee|counterparty|company|client)\s+"
    r"(?:name|address|details|registration)"
    r"|(?:billing|invoice|shipping|delivery|registered|business|postal)\s+address"
    r"|bill\s+to|ship\s+to|address\s+block|remit\s+to"
    r"|due\s+date|payment\s+terms|terms\s+of\s+payment|terms\s+and\s+conditions"
    r"|(?:effective|issue|start|end|expiry|expiration|renewal|commencement|delivery|"
    r"invoice|signature|signing|execution|completion|payment)\s+dates?"
    r"|dates?\s+of\s+(?:issue|signature|signing|execution|completion)"
    r"|governing\s+law|jurisdiction|choice\s+of\s+law"
    r"|notice\s+period|period\s+of\s+notice"
    r"|termination\s+(?:clause|date|notice|provision|fee|charge|right|for\s+convenience)"
    r"|(?:notice|right)\s+of\s+termination|notice\s+to\s+terminate"
    r"|signator(?:y|ies)|signatures?|signed\s+(?:by|this|that|the|it|on)"
    r"|who\s+(?:signed|has\s+signed|signs)"
    r"|(?:annual|monthly|yearly|quarterly|one[- ]time|set[- ]?up|late|service|licen[cs]e|"
    r"subscription|management|transaction|renewal|termination|cancellation)\s+fees?"
    r"|interest\s+rate|late\s+payment|penalt(?:y|ies)"
    r"|quantit(?:y|ies)|qty|currency|discounts?|purchase\s+order"
    r"|owes?|owed|outstanding\s+balance"
    r"|expir(?:e|es|ed|y|ation|ing)"
    r"|effective\s+from|valid\s+(?:from|until|through)"
    r")\b"
    r"|\bdue\b",
    re.I,
)

#: A field of a document that is ALSO an ordinary English word. Worth full
#: marks only inside a field frame (below), and almost nothing on its own.
_AMBIGUOUS_FIELD_WORDS = (
    "terms?",
    "totals?",
    "amounts?",
    "part(?:y|ies)",
    "values?",
    "rates?",
    "dates?",
    "prices?",
    "fees?",
    "costs?",
    "balances?",
    "numbers?",
    "periods?",
    "vendors?",
    "suppliers?",
    "payees?",
    "counterpart(?:y|ies)",
    "terminations?",
    "deposits?",
    "charges?",
)
_AMBIGUOUS_ALT = "(?:" + "|".join(_AMBIGUOUS_FIELD_WORDS) + ")"
_AMBIGUOUS_FIELD_RE = re.compile(r"\b" + _AMBIGUOUS_ALT + r"\b", re.I)

#: Words that name a document or a part of one. An ambiguous field word beside
#: one of these is being used as a field OF that document.
_DOC_WORD_ALT = (
    r"(?:invoices?|bills?|contracts?|agreements?|msa|sow|receipts?|quotes?|quotation|"
    r"estimate|statements?|polic(?:y|ies)|forms?|lease|orders?|po|purchase\s+order|"
    r"documents?|docs?|paperwork|pages?|clauses?|sections?|annex|schedule|exhibit|"
    r"appendix|attachment|file|pdf|sheet|datasheet|brochure|report|letter|certificate|"
    r"credit\s+note|remittance|payslip|ledger|table)"
)
_DOC_WORD_RE = re.compile(r"\b" + _DOC_WORD_ALT + r"\b", re.I)

#: How close a document word has to be to count as naming the field. Six words
#: covers "what does the invoice say the total is" and stops well short of the
#: two clauses of "does this contract make sense for us in the long term".
_DOC_WORD_WINDOW = 6

#: "the term OF THIS contract", "the total ON THIS invoice".
_FRAME_OF_DOC_RE = re.compile(
    r"\b" + _AMBIGUOUS_ALT + r"\s+(?:of|on|in|for|under|from|within|per)\s+"
    r"(?:the|this|that|our|your|their|each|both|either)\s+(?:\w+\s+){0,2}?" +
    _DOC_WORD_ALT + r"\b",
    re.I,
)

#: "WHAT IS the total", "WHO ARE the parties", "HOW LONG IS the initial term".
#: Exactly ONE word may sit between the determiner and the field word. Two let
#: a preposition in, and "what happens in the event OF TERMINATION" — a plain
#: question about what the contract says — was read as a field ask.
#: "this" and "that" are NOT determiners here: in "how much will THIS COST us"
#: they are the subject pronoun, and reading them as "this <field>" sent a
#: question a brochure cannot answer into the strict block.
_FRAME_WH_VALUE_RE = re.compile(
    r"\b(?:what|what's|whats|how\s+much|how\s+many|how\s+long|when|who|whom|which)\b"
    r"(?:\s+\w+){0,4}?\s+(?:the|its|their|our|your)\s+(?:\w+\s+){0,1}?"
    + _AMBIGUOUS_ALT + r"\b"
    r"|\b(?:what|what's|whats|how\s+much|how\s+many|how\s+long|when|who|which)\s+"
    r"(?:is|are|was|were|does|do|did)\s+(?:it|this|that|they)?\s*" + _AMBIGUOUS_ALT + r"\b",
    re.I,
)

#: "GIVE ME the total", "I NEED the total", and the elliptical "just the total".
_FRAME_REQUEST_RE = re.compile(
    r"\b(?:give|show|tell|send|list|get|find|pull|extract|provide|state|confirm|fetch|"
    r"print|share|paste|quote)\s+(?:me\s+|us\s+)?"
    r"(?:the|this|that|its|their|all|each|every|both)\s+(?:\w+\s+){0,1}?"
    + _AMBIGUOUS_ALT + r"\b"
    r"|\b(?:i|we)\s+(?:need|want|require|would\s+like)(?:\s+to\s+know)?\s+"
    r"(?:the|this|that|its|their)\s+(?:\w+\s+){0,1}?" + _AMBIGUOUS_ALT + r"\b"
    r"|^\W*(?:just\s+|only\s+)?(?:the|this)\s+(?:\w+\s+){0,1}?"
    + _AMBIGUOUS_ALT + r"(?:\s+please)?\W*$",
    re.I,
)

#: The elliptical ask, which is the whole question: "total?", "fees and dates
#: please". Anchored at BOTH ends — anchored only at the start, it read "the
#: party was a total disaster, does this help?" as a request for the parties.
_FRAME_ELLIPTICAL_RE = re.compile(
    r"^\W*(?:just\s+|only\s+)?(?:the\s+)?" + _AMBIGUOUS_ALT +
    r"(?:\s*(?:,|and)\s+(?:the\s+)?" + _AMBIGUOUS_ALT + r")*"
    r"(?:\s+please)?\W*$",
    re.I,
)

# --- step 3: decision evidence ---------------------------------------------

#: The person asked for a judgement. One of these is enough — see the
#: thresholds above. Deliberately wider than the field list: an over-eager
#: decision signal costs a sentence, a missed one costs the incident.
#:
#: "help" is only a signal in a frame ("does this HELP us"), never bare: "can
#: you help me extract the total" is an extraction ask with the word help in it.
_DECISION_RE = re.compile(
    r"\bworth\b"
    r"|\brecommend(?:s|ed|ation|ations)?\b|\bsuggest(?:s|ed|ion|ions)?\b"
    r"|\badvis(?:e|es|able|ability)\b|\badvice\b"
    r"|\bsuitab(?:le|ility)\b|\bsuited\b|\bsuits\b"
    r"|\boverkill\b|\benough\b|\bsufficient\b|\bmakes?\s+sense\b"
    r"|\bhelp\s?full?\b|\bhelpful\b|\buseful\b|\busable\b|\bbeneficial\b"
    r"|\b(?:does|do|will|would|can|could|is|are)\s+(?:it|this|that|these|those|they)"
    r"\s+(?:really\s+)?helps?\b"
    r"|\bany\s+good\b|\bgood\s+(?:for|enough|idea|choice|fit|value|option|buy|deal)\b"
    r"|\bis\s+(?:it|this|that)\s+(?:a\s+)?good\b"
    r"|\bright\s+(?:choice|fit|option|one|product|unit|solution|call|move|thing|"
    r"approach|size|model|kit)\b"
    r"|\bwrong\s+(?:choice|fit|option|one|product|unit|solution|size)\b"
    r"|\bwhich\s+(?:one|option|model|version|configuration|config|product|unit|size|"
    r"approach|route|is|should|would|do)\b"
    r"|\bbetter\b|\bbest\b|\bpros\s+and\s+cons\b|\btrade[- ]?offs?\b"
    r"|\bcompare\b|\bcomparison\b|\bversus\b|\bvs\.?\b"
    r"|\bdo\s+(?:i|we)\s+(?:really\s+)?(?:need|have\s+to|want)\b"
    r"|\bdo\s+you\s+(?:recommend|suggest|think)\b"
    r"|\bwould\s+you\s+(?:recommend|suggest|buy|go\s+with|use|pick|choose)\b"
    r"|\b(?:can|may|could)\s+(?:i|we)\s+(?:\w+\s+){0,2}?(?:place|put|install|mount|"
    r"deploy|rack|host|migrate|store|use|run|buy|fit|connect|plug|power|cool|stack|"
    r"combine|order|purchase|rely|trust|apply|claim|expect)\b"
    r"|\b(?:can|could)\s+(?:it|this|that|these|they)\s+(?:be\s+used|handle|support|"
    r"cope|cover|take|run|host|fit|power|cool|do)\b"
    # "does this cover 20 nodes?" is the same suitability question as "can it
    # cover 20 nodes?", and fell to neutral until this line.
    r"|\b(?:does|do)\s+(?:it|this|that|these|they)\s+(?:cover|support|handle|fit|suit|"
    r"scale|work|manage|stack|hold)\b"
    r"|\b(?:will|would)\s+(?:it|this|that|these|they)\s+(?:work|fit|handle|cope|"
    r"support|cover|be\s+enough|do|help|suit)\b"
    r"|\ballowed\b|\bpermitted\b|\bdeductible\b"
    r"|\bovercharg\w*\b|\btoo\s+(?:much|many|high|expensive|low|small|big|large|costly)\b"
    r"|\bviable\b|\bfeasible\b|\bsensible\b|\breasonable\b|\bfair\s+(?:price|deal|rate)\b"
    r"|\b(?:is|are|was|were|seems?|looks?)\s+(?:it|this|that|the\s+\w+)\s+"
    r"(?:fair|competitive|expensive|cheap|steep|excessive)\b"
    r"|\bwhat\s+would\s+you\s+do\b"
    r"|\bis\s+(?:it|this|that)\s+(?:ok|okay|fine|safe|smart|wise)\b"
    r"|\b(?:i|we)\s+(?:want|plan|intend|am\s+looking|are\s+looking)\s+to\s+"
    r"(?:\w+\s+){0,2}?(?:place|put|install|mount|deploy|use|buy|host|rack|migrate|"
    r"store|run|order|purchase|grow|expand)\b"
    r"|\bfits?\b|\bcompatib(?:le|ility)\b"
    r"|\bworks?\s+(?:for|with)\s+(?:my|our|us|me|a|an|the|this|that)\b",
    re.I,
)

#: "should I / should we / should this", and the same words the other way
#: round — "tell me whether I SHOULD renew" is the mixed question's whole
#: second half, and a pattern that only looked for "should i" missed it.
_SHOULD_RE = re.compile(
    r"\bshould\s+(?:i|we|it|this|that|they|these|those|one)\b"
    r"|\b(?:i|we|it|this|that|they)\s+should\b",
    re.I,
)

#: ... except inside a question that asks for a VALUE. "What supply water
#: temperature should I run for this unit?" wants the number the document
#: gives, and the advisory block's "clear yes / no / it depends" is the wrong
#: shape for it. A decision VERB after "should" overrides the exception.
_VALUE_WH_RE = re.compile(
    r"^\W*(?:what|what's|whats|when|who|where|how\s+(?:much|many|long|often))\b", re.I
)
_DECIDE_VERBS = (
    r"(?:buy|purchase|get|use|choose|pick|go\s+with|order|renew|sign|upgrade|replace|"
    r"switch|invest|adopt|keep|cancel|deploy|install|place|host|rack|migrate|do)"
)
_DECIDE_AFTER_SHOULD_RE = re.compile(
    r"\bshould\s+(?:i|we|it|this|that|they|these|those|one)\s+(?:\w+\s+){0,2}?"
    + _DECIDE_VERBS + r"\b"
    r"|\b(?:i|we|it|this|that|they)\s+should\s+(?:\w+\s+){0,2}?" + _DECIDE_VERBS + r"\b",
    re.I,
)

#: The person is in the question ("as I have dgx spark ...").
_FIRST_PERSON_RE = re.compile(
    r"\b(?:i|i'm|im|i've|ive|my|mine|me|we|we're|our|ours|us)\b", re.I
)

#: ... and the question is about placing, buying or fitting the thing. This is
#: the WEAK decision tier: on its own, first person plus one of these is worth
#: a single point, which is exactly enough. An earlier cut also carried need,
#: use, run, plan, good and right here, which made "I need to know the total"
#: a decision; a word stays only if it asks what to DO with the thing.
_SUITABILITY_RE = re.compile(
    r"\b(?:place|placing|install|installing|mount|mounting|deploy|deploying|rack|racking|"
    r"host|hosting|migrate|migrating|store|storing|put|buy|buying|purchase|purchasing|"
    r"invest|upgrade|upgrading|choose|choosing|choice|options?|alternatives?|"
    r"compatible|benefit|benefits)\b",
    re.I,
)


@dataclass
class Signals:
    """What the classifier saw, and what it concluded.

    The evidence is kept because a misroute is otherwise invisible: the
    2026-09-18 blocker was one lookbehind that excluded "long-term" and not
    "long term", and it took two live runs to find. `evidence` says which
    pattern fired on which words.
    """

    mode: str
    field_score: int = 0
    decision_score: int = 0
    evidence: List[Tuple[str, str]] = _dataclass_field(default_factory=list)

    @property
    def wants_fields(self) -> bool:
        return self.field_score >= _FIELD_THRESHOLD

    @property
    def wants_advice(self) -> bool:
        return self.decision_score >= _DECISION_THRESHOLD


def _field_evidence(masked: str) -> Tuple[int, List[Tuple[str, str]]]:
    """Points for "a field of this document is being named", and why."""
    score = 0
    why: List[Tuple[str, str]] = []

    verb = _EXTRACT_VERB_RE.search(masked)
    if verb:
        score += _STRONG
        why.append(("extract-verb", verb.group(0)))

    named = _UNAMBIGUOUS_FIELD_RE.search(masked)
    if named:
        score += _STRONG
        why.append(("field-noun", named.group(0)))

    if score >= _FIELD_THRESHOLD:
        return score, why

    # An ambiguous word only counts when something in the sentence says it is
    # a field OF a document.
    framed = False
    for name, rx in (
        ("frame-of-document", _FRAME_OF_DOC_RE),
        ("frame-what-is", _FRAME_WH_VALUE_RE),
        ("frame-request", _FRAME_REQUEST_RE),
        ("frame-elliptical", _FRAME_ELLIPTICAL_RE),
    ):
        hit = rx.search(masked)
        if hit:
            score += _STRONG
            why.append((name, hit.group(0).strip()))
            framed = True
            break

    if not framed:
        doc_words = [m.start() for m in _DOC_WORD_RE.finditer(masked)]
        for hit in _AMBIGUOUS_FIELD_RE.finditer(masked):
            near = any(
                _words_between(masked, min(pos, hit.start()), max(pos, hit.start()))
                <= _DOC_WORD_WINDOW
                for pos in doc_words
            )
            if near:
                score += _STRONG
                why.append(("frame-near-document", hit.group(0)))
                framed = True
                break

    if not framed:
        # Bare ambiguous words, and they do NOT add up: the total is capped
        # below the threshold however many there are. An earlier cut let two
        # of them reach it, and "what rate of airflow does it need over a long
        # period?" — rate + period, both ordinary English — took the strict
        # extraction block. A word that could be ordinary English is evidence
        # only in a frame; counting two of them is counting the same weakness
        # twice.
        bare = [hit.group(0) for hit in _AMBIGUOUS_FIELD_RE.finditer(masked)]
        if bare:
            score += _WEAK
            why.extend(("bare-field-word", word) for word in bare)

    return score, why


def _words_between(text: str, start: int, end: int) -> int:
    return len(text[start:end].split())


def _decision_evidence(question: str) -> Tuple[int, List[Tuple[str, str]]]:
    """Points for "the person asked for a judgement", and why.

    Reads the question UNMASKED. The mask exists to stop ordinary English
    being mistaken for a field, and the same caution does not apply here: a
    decision signal read out of "best practice" costs a sentence of context,
    which is the direction this module is allowed to be wrong in.
    """
    score = 0
    why: List[Tuple[str, str]] = []

    hit = _DECISION_RE.search(question)
    if hit:
        score += _STRONG
        why.append(("decision-marker", hit.group(0).strip()))

    should = _SHOULD_RE.search(question)
    if should:
        decides = bool(_DECIDE_AFTER_SHOULD_RE.search(question))
        if decides or not _VALUE_WH_RE.match(question):
            score += _STRONG
            why.append(("should", should.group(0)))
        else:
            why.append(("should-inside-a-value-question", should.group(0)))

    if score < _DECISION_THRESHOLD:
        person = _FIRST_PERSON_RE.search(question)
        suits = _SUITABILITY_RE.search(question)
        if person and suits:
            score += _WEAK
            why.append(("first-person-plus-suitability", suits.group(0)))

    return score, why


def classify(question: str) -> Signals:
    """Score a question, then apply the precedence, and keep the evidence.

    The precedence, in order, and each line is a rule from the 2026-09-18
    review:

      1. fields AND a judgement  -> extract+advise. A decision signal anywhere
         beats a field match; the fields are still answered first, strictly.
      2. a judgement only        -> advise.
      3. fields only             -> extract. Reached only on >= 2 points of
         high-precision field evidence.
      4. neither                 -> neutral, which answers what was asked and
         stops. It does not forbid a verdict (see NEUTRAL).
    """
    q = question or ""
    masked = _mask_ordinary_english(q)

    field_score, field_why = _field_evidence(masked)
    decision_score, decision_why = _decision_evidence(q)

    sig = Signals(
        mode="",
        field_score=field_score,
        decision_score=decision_score,
        evidence=field_why + decision_why,
    )
    if sig.wants_fields and sig.wants_advice:
        sig.mode = "extract+advise"
    elif sig.wants_advice:
        sig.mode = "advise"
    elif sig.wants_fields:
        sig.mode = "extract"
    return sig


def question_mode(question: str) -> str:
    """The shape of the question: "advise", "extract", "extract+advise", "".

    A named field wins over a merely IMPLIED judgement, so "I need the total
    from this invoice" is extraction however first-person it sounds. When a
    judgement IS asked for ("extract the payment terms and tell me whether I
    should renew", "is this the right choice for us in the long term?") the
    judgement is answered — with the fields first when fields were named, and
    never as extraction alone.
    """
    return classify(question).mode


_BLOCKS = {
    "advise": ADVISORY,
    "extract": EXTRACTION,
    "extract+advise": EXTRACT_THEN_ADVISE,
    "": NEUTRAL,
}


def system_text(question: str = "") -> str:
    """The document route's system prompt for THIS question."""
    return BASE + _BLOCKS[question_mode(question)] + STRUCTURE
