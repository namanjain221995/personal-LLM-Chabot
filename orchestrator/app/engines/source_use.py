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
     of in a comment nobody can test;
  4. and ONE function, `_apply_precedence`, turns the two scores into a mode.
     Neither scorer knows where in the sentence the other one fired, and no
     pattern gets to decide the mode by where it is anchored.

WHY STEP 4 EXISTS (2026-09-18). The cut before this one narrowed "should"
with `_VALUE_WH_RE`, anchored `^` to the start of the WHOLE question, and used
it to DISCARD a "should" clause found anywhere later in it: "what supply water
temperature should I run for this unit?" wants a number, not a verdict. The
narrowing is right and the discarding was not. "what is the invoice total, and
should we accept it?" lost its judgement half and took the strict EXTRACTION
block, which says "do not add advice, a recommendation, a next step, a caution
or an offer of further help" — while "tell me the invoice total, and should we
accept it?", differing in its FIRST WORD ONLY, was answered in full. Live on
one invoice fixture, three runs of each wording, the misrouted phrasing
refused the judgement 2 of 3 ("The document does not provide enough
information to determine if the prices are fair or if the order is valid, so I
cannot advise on whether you should accept it", then a list of things to go
and check) while the correctly routed one answered it every time. With the
precedence below, the same misrouted wording answers both halves 3 of 3:
"**Total** ... **5,200.00 USD**" then "**Should we accept it?** **Yes.**"

A clause-scoped anchor would have been the same instrument again. So a
narrowed signal is now HELD rather than dropped, and the precedence — not a
regex — decides what holding it may cost: it may send a question to NEUTRAL,
which answers what was asked and still permits a verdict, and it may never
send one to strict EXTRACTION. A generated cross-product of value-wh openers x
field names x judgement asks pins the whole class in the suite: 9,504
questions, 6,336 of which took strict extraction before this change.

The evidence is returned with the mode (`classify`), so a misroute can be read
off in one line instead of bisected through a lookbehind chain.

WHY A CLAUSE TEST ON TOP OF THE WORDS (2026-09-18, round 5). Both scores above
are vocabularies, and the decision one can never be finished: round 4's
verifier found 8,676 of 11,475 generated "field ask + judgement ask" questions
taking strict extraction alone, because the judgement half used an adjective
no pattern listed ("... and is that in line with the market?"). So a question
that names a field is now also split into clauses, and a clause that asks
something no field can answer — what is left of it once field words, value
words and function words are removed is not empty — keeps the question out of
strict extraction alone. The lexicon that test needs is the FIELD side's,
where a gap costs a sentence; an adjective nobody listed counts towards a
judgement. The instrument, the alternatives it beat (a shape regex, a
question count, more decision words) and what each measured are in the
"step 4" section below; the strict block was rewritten at the same time so
that a misroute the clause test still misses costs less (see EXTRACTION).

Consumed by app/engines/document.py. `HISTORY_DOC_GUIDANCE` is the same rule
in one paragraph, for the pinned "documents the user uploaded earlier" block
that main.py puts in front of LATER turns — those turns route to chat, where
the document survives only as history, and the refusal reappeared there
("Based on the provided Vertiv SmartRow document ...") on the very next turn.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as _dataclass_field
from typing import List, Sequence, Tuple

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
#:
#: WHAT THE STRICT RULES ARE FOR, AND WHAT THEY ARE NOT (2026-09-18, round 5).
#: The block exists for three things: never invent a value (the "**Tax
#: Amount:** 0.00 USD" QA caught on an invoice with no tax line), answer only
#: what was asked, and no padding. Until this round it ALSO said "For this
#: answer do not add advice, a recommendation, a next step, a caution or an
#: offer of further help" with no qualifier, so every judgement ask the switch
#: failed to see was forbidden outright: live, "what is the annual fee in this
#: contract, and is that in line with the market?" (mode extract) answered
#: "Therefore, I cannot assess its competitiveness based on the provided
#: text." The switch now sees that wording (the clause test, "step 4" below),
#: but no classifier sees every wording — it still leaves 1,140 of 50,400
#: generated judgement asks in this block. So a misroute must cost a
#: sentence, not a refusal: the ban now covers what the person did NOT ask
#: for, and _ASKED_IS_ANSWERED says a question they did ask is answered. The
#: field rules are untouched and still bind the fields, and the last line of
#: _ASKED_IS_ANSWERED keeps the missing-field rule out of its reach.
#:
#: Measured live, Fast, graded on the first sentence of the judgement half.
#: A real residual ("what is the annual fee in this contract and good or
#: bad?", still routed extract): old block 0 of 5 answered, this block 7 of 8
#: over two batches ("**Verdict: Good** / This is a good rate."); the miss
#: opened "This is **not stated in the document**" and listed what it would
#: need to know. The verifier's market
#: question FORCED into this block: old 0 of 8, this block 7 of 15 — a
#: question that needs outside figures is the hard case, and the strict
#: framing still wins about half the time, which is why the switch and not
#: this block is the fix. The tax-free invoice ("I need the total from this
#: invoice and the tax amount"), 16 runs per block over two fixtures: no tax
#: figure in any answer from either block, total and "not stated" in every
#: one, median length 76 / 86.5 chars here against 193 / 126.5 on the old
#: block.
_STRICT_FIELDS = (
    "\nTHIS IS AN EXTRACTION QUESTION. Return ONLY what is actually in the document for "
    "each field asked: the fields, values and figures as written, with page numbers where "
    "that helps. For those fields this OVERRIDES the general-knowledge permission above — "
    "for a field, the document is the only source. Do not fill a missing field from general "
    "knowledge, do not infer it from the rest of the document, do not estimate it, and do "
    "not write 0, 0.00 or \"none\" for a field the document never gives; write \"not "
    "stated in the document\" — that line is the whole answer for that field, with no "
    "sentence about why it is missing. Answer what was asked and stop: do not add advice, "
    "a recommendation, a next step, a caution or an offer of further help that the person "
    "did not ask for, and do not raise a discrepancy the person did not ask about. Give the "
    "fields, not the reasoning that found them: no thinking out loud, no self-correction "
    "in the answer."
)

#: How a judgement is answered once the fields are given — shared by the
#: strict block's safety net and by EXTRACT_THEN_ADVISE, so a question that
#: lands in either one gets the same instruction.
#:
#: Measured 2026-09-18, live, the verifier's pair on the MSA fixture, both
#: routed extract+advise, five runs of each wording per text, graded on the
#: first sentence of the judgement half. With the extract+advise text of
#: 4810da0 ("THEN ... the judgement: a clear yes / no / it depends, the
#: reasoning, and the numbers it turns on ..."): "is that in line with the
#: market?" 1 of 5, "is that reasonable?" 3 of 5. The failures open with what
#: the document lacks — "The document does not provide market benchmarks or
#: industry averages for comparison.", "It is not possible to determine if
#: this fee is reasonable based on the provided text alone." — and end with a
#: list of things to go and check: the strict field rules leak into the half
#: they do not govern. With the rules below: 4 of 5 and 5 of 5, then 3 of 3
#: and 3 of 3 again on the committed code; the one miss opens "Whether
#: 18,000 USD is in line with the market depends entirely on the scope of
#: services provided, which is not detailed in the excerpt."
#: So the rules say which half the strict rules bind, where the verdict goes,
#: and what a closing referral may be.
#:
#: They name no failing wording on purpose: the model copies what a prompt
#: quotes (see BASE), and a list of example wordings is a vocabulary again.
_JUDGEMENT_RULES = (
    "Its FIRST sentence is the verdict: yes, no, or it depends on the one thing it turns "
    "on \u2014 never a sentence about what the document lacks. Then the reasoning and the "
    "numbers it turns on: the document's figures where it has them and, where it has none, "
    "typical figures from general knowledge, said to be yours. The strict rules above bind "
    "the FIELDS only. For the judgement, general knowledge is a source, and the document "
    "being silent is the reason to use it, not a reason to withhold the judgement. Naming "
    "what would settle it is one closing line at most, never a list of things to go and "
    "check."
)

#: The strict block's safety net for a judgement ask the switch did not see.
_ASKED_IS_ANSWERED = (
    "\nNOTHING THE PERSON ASKED IS FORBIDDEN. If the question also asks for a judgement "
    "\u2014 whether a value is fair or usual, high or low, how it compares with what others "
    "pay, whether it is a risk, what to do about it \u2014 that part was asked, so answer "
    "it after the fields, under its own heading. " + _JUDGEMENT_RULES + " A field the "
    "document does not give is never such a part: it stays \"not stated in the document\", "
    "with nothing added."
)

EXTRACTION = _STRICT_FIELDS + _ASKED_IS_ANSWERED

#: Added when the question names fields AND asks for a judgement.
#: Extraction wins for the fields; the advice follows them, clearly separated.
#: Built on the field rules alone: the judgement half below replaces the
#: safety net, it does not sit beside it.
EXTRACT_THEN_ADVISE = _STRICT_FIELDS + (
    "\nTHE PERSON ALSO ASKED FOR A JUDGEMENT. Answer in two parts, in this order. FIRST "
    "the fields, under their own heading, under the strict rules above. THEN, under a "
    "separate heading, the judgement: a clear yes / no / it depends, the reasoning, and "
    "the numbers it turns on. " + _JUDGEMENT_RULES + " Nothing in the second part may "
    "change, fill in or round a field in the first part, and a field that is not stated "
    "stays not stated even if the advice would be easier with a number there."
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
    # "copy" took a fixed noun list (table/text/list/wording/clause/paragraph/
    # section/block), so "copy the addresses into a table" fell to neutral --
    # the brief lists copy as an extraction verb whatever follows it. A
    # DETERMINER plus any noun replaces the list. "copy that, thanks" still
    # does not match: there is no word after the determiner.
    r"|copy\s+(?:out\s+)?(?:the|this|that|these|those|all|every|each)\s+\w+"
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
    r"|\bworks?\s+(?:for|with)\s+(?:my|our|us|me|a|an|the|this|that)\b"
    # ASKING FOR AN ASSESSMENT OF A FIGURE (2026-09-18 MEDIUM). "what is the
    # invoice total, and is that a red flag?" scored ZERO decision evidence
    # and, because the field half scored 2, took the strict extraction block.
    # A missing DECISION word beside a present FIELD word is the unsafe
    # direction, and the previous cut had only measured the field list's
    # residuals. These are the copula-plus-evaluation shapes QA measured:
    # "is that a problem for us?", "is that a red flag?", "is that normal?",
    # "how bad is that for us?", "what do you make of it?".
    r"|\b(?:is|are|was|were|isn'?t|aren'?t|does|do|seems?|sounds?|looks?|feels?)\s+"
    r"(?:it|this|that|these|those|they|there)\s+(?:really\s+|actually\s+|even\s+)?"
    r"(?:an?\s+|any\s+)?(?:problems?|issues?|concerns?|concerning|risky|risks?|"
    r"red\s+flags?|warning\s+signs?|deal[- ]?breakers?|blockers?|worr(?:y|ying|ies)|"
    r"worrisome|normal|typical|standard|usual|unusual|odd|strange|common|"
    r"acceptable|unacceptable|bad|dangerous|serious|significant|excessive|"
    r"unreasonable|unfair|out\s+of\s+line|high|low|a\s+lot|much)\b"
    r"|\bhow\s+(?:bad|risky|serious|concerning|worrying|normal|unusual|common|safe|"
    r"significant|exposed|much\s+of\s+(?:an?\s+)?(?:problem|risk|concern|issue))\b"
    r"|\bred\s+flags?\b|\bdeal[- ]?breakers?\b|\bcause\s+for\s+concern\b"
    r"|\bwhat\s+do\s+you\s+(?:make\s+of|think\s+(?:of|about))\b"
    r"|\bworr(?:y|ied|ies|ying)\b|\bconcerned\b"
    r"|\banything\s+(?:i|we)\s+should\b|\banything\s+(?:odd|wrong|unusual)\b",
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
#:
#: THIS PATTERN DOES NOT DECIDE THE MODE, AND MUST NOT (2026-09-18). It is
#: anchored to the start of the whole question while the "should" it speaks
#: about can sit anywhere in it, so on its own it read "what is the invoice
#: total, and should we accept it?" as a pure value ask and threw the
#: judgement away. What it produces is HELD evidence, not discarded evidence,
#: and _apply_precedence() gives it back the moment a field ask is also
#: proven. See that function for the rule; lengthening this alternation is
#: not the fix and never was.
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


# --- step 4: asks a field cannot answer (STRUCTURE, not vocabulary) ---------
#
# WHY THIS STEP EXISTS (2026-09-18, round 5). Steps 1-3 decide the mode from
# two vocabularies, and the decision vocabulary is the one that cannot be
# finished: it has to know every adjective a person may use to ask for a
# judgement. Round 4's verifier generated 11,475 "field ask + judgement ask"
# questions and 8,676 took strict extraction alone; live, "what is the annual
# fee in this contract, and is that in line with the market?" came back "I
# cannot assess its competitiveness based on the provided text" while "...and
# is that reasonable?" — one word different, and that word in _DECISION_RE —
# got a verdict 3 of 3. This round's own corpus (tests/document_judgement_
# corpus.py, committed before this code) measured the same thing: 66 of 84
# judgement tails carry no decision signal at all, and 39,600 of 50,400
# generated questions took strict extraction alone.
#
# The instrument is a CLAUSE test. The failing questions are two asks joined
# together, and whether the second one can be answered by the strict block is
# a question about what it asks, not about which adjective it uses:
#
#   1. split the question into clauses (punctuation, spaced dashes, commas,
#      and "and/but/so/or" when a new question starts after it);
#   2. find the clauses that ASK something — a polar question ("is that ...",
#      "would a lawyer ..."), a wh-question, an embedded one ("tell me if
#      ...", "whether ..."), an imperative aimed at a thing ("flag it if
#      ..."), or a fragment typed with a question mark ("on the high side?");
#   3. an ask is answerable from the document's values only when, once its
#      field words, document words, words for what a page states about a
#      field (payable, signed, included, per year), closed-class words
#      (articles, pronouns, auxiliaries, prepositions) and value tokens
#      (numbers, currencies, dates, units) are taken away, NOTHING is left.
#      "is it payable in advance?" leaves nothing; "is that in line with the
#      market?" leaves "line market"; "is that competitive?" leaves
#      "competitive". A wh-question
#      that names a field outright ("what is the unit price of the cooling
#      unit") asks for that field's value, whatever else it names.
#
# The lexicon in step 3 is the FIELD side's — words that a value-answerable
# clause may contain — and it is closed-ish (function words, units, what a
# document states about a field). An adjective nobody listed is NOT in it, so
# an unknown word counts towards a judgement, never away from one. That is the
# safe-failure rule of the module docstring applied to the vocabulary itself:
# the lexicon that must be complete is the one whose gaps cost a sentence.
#
# REJECTED, each measured on the same 50,400-question dev search and the same
# 3,130 pure field asks (tests/document_judgement_corpus.py), by swapping this
# step for the alternative and leaving everything else alone:
#
#   * a SHAPE regex for "<join> is/does/would ... that/it": it keys on the
#     join and on a pronoun, which is the _VALUE_WH_RE mistake again (a
#     pattern anchored where one wording happens to put the ask). It left
#     29,100 of 50,400 in strict extraction — embedded asks ("tell me if
#     ..."), fragments ("on the high side?"), wh-asks ("how does that stack up
#     ...") and every reversed order walk past it — and it cost 1,320 of 3,130
#     pure field asks their strict block, because "is it payable in advance?"
#     has exactly the shape "is that in line with the market?" has.
#   * a QUESTION COUNT (two or more asks, by "?" or by interrogative clause):
#     the verifier's own failing wording has ONE question mark, so 12,954 of
#     50,400 stayed in strict extraction; and "how much do we owe and when is
#     it due?" has two interrogative clauses that are both field asks, so it
#     kept only 734 of 3,130 pure field asks strict and flipped a pinned
#     battery row. How many things were asked cannot tell a second field ask
#     from a judgement; what each one asks can.
#   * more DECISION WORDS: every round so far added the adjective the last
#     round's search found ("red flag", "normal", "a problem for us") and the
#     next search found another. _DECISION_RE is kept, unchanged, as a
#     supplement — it still decides "advise" on its own for questions with no
#     field in them, where this step has nothing to say.
#
# MEASURED WITH THIS STEP: 1,140 of 50,400 dev questions still take strict
# extraction alone (39,600 before), and 600 of 18,000 on the held-out tails,
# which were written with the corpus and first measured on the finished
# detector (16,200 before). 1,080 and 540 of those are REVERSED orders whose
# first clause is a bare fragment with its question mark removed by the
# generator ("in line with the market, and what is the annual fee?"); the rest
# are "... and good or bad?" / "... and thumbs up or thumbs down?", an
# adjective conjunct this step deliberately does not guess at (see
# _PREP_CONJUNCT_RE). Those residuals are why the EXTRACTION block itself no
# longer forbids answering a question the person asked (see EXTRACTION).
#
# What it costs: a field ask followed by a follow-up that names something
# outside the lexicon ("how is it split across the line items?") goes to
# extract+advise — the cheap direction, a sentence of context. All 3,130 dev
# and 1,330 held-out pure field asks in the corpus stay strict.

_AUX_WORDS = (
    r"(?:is|are|was|were|am|isn'?t|aren'?t|wasn'?t|weren'?t|do|does|did|don'?t|doesn'?t|"
    r"didn'?t|can|could|will|would|shall|should|may|might|must|has|have|had|can'?t|"
    r"cannot|couldn'?t|won'?t|wouldn'?t|shouldn'?t|hasn'?t|haven'?t|ought)"
)
#: What may follow the auxiliary in a polar question: its subject. Closed
#: class — pronouns, determiners, quantifiers, a number.
_SUBJECT_WORDS = (
    r"(?:it|that|this|these|those|they|there|we|i|you|he|she|the|a|an|our|my|your|their|"
    r"its|any|such|anyone|anything|someone|something|most|many|all|every|each|some|\d)"
)
_POLAR_START = _AUX_WORDS + r"\s+" + _SUBJECT_WORDS + r"\b"
_WH_START = (
    r"(?:(?:on|in|at|for|by|from|to|under|of|with|until|since)\s+)?"
    r"(?:what|what'?s|whats|which|who|who'?s|whom|whose|when|where|why|how)\b"
)
#: "how much / many / long ..." asks for a value; bare "how" asks for a manner
#: ("how does that stack up against the market?") and is treated as open.
_VALUE_HOW_RE = re.compile(
    r"^(?:(?:on|in|at|for|by|from|to|under|of|with|until|since)\s+)?how\s+(?:much|many|long|"
    r"often|far|soon|old|big|large|early|late|frequently|quickly)\b",
    re.I,
)
#: An ask inside a request: "tell me IF ...", "let me know WHETHER ...", "I
#: wonder if ...", "flag it if ...". Up to four words may lead in; a bare
#: clause-initial "if" is a condition ("if you could pull the terms that
#: would be great"), not a question, so "if" needs at least one word before it.
_EMBEDDED_RE = re.compile(
    r"^(?:(?:[\w'-]+\s+){0,4}?whether|(?:[\w'-]+\s+){1,4}?if)\b\s*(?P<body>.*)$", re.I
)
#: An imperative aimed at a thing: a verb and then an object pronoun ("flag
#: it", "sanity-check it", "send me"). A noun fragment is almost never
#: followed by an object pronoun, which is what makes this a shape and not a
#: verb list.
_IMPERATIVE_RE = re.compile(r"^[a-z][\w-]*\s+(?:it|them|me|us|that|this|these|those)\b", re.I)
#: Where one clause ends and the next begins. After "and / but / so / or" a
#: new clause starts with a question, a request ("... and list the line
#: items") or a first-person statement ("... but I need the total"); a noun
#: after "and" ("the total and the due date") is still the same clause.
_ASK_START = (
    r"(?:" + _POLAR_START + r"|" + _WH_START + r"|whether\b"
    r"|[a-z][\w-]*\s+(?:it|them|me|us)\b"
    r"|(?:tell|give|show|send|list|pull|extract|provide|confirm|share|read|copy|quote|fetch|"
    r"find|transcribe|itemi[sz]e|fill)\b"
    r"|(?:i|we)\s+(?:need|want|would\s+like|'d\s+like|require)\b)"
)
_CLAUSE_SPLIT_RE = re.compile(
    r"(?P<q>\?)+|[!;]+|\.(?=\s|$)|\s[-–—]+\s|[–—]|,\s"
    r"|\s+(?=(?:and|but|so|or|plus|also)\s+" + _ASK_START + r")",
    re.I,
)
_LEADING_FILLER_RE = re.compile(
    r"^(?:(?:and|but|so|or|plus|also|then|oh|well|ok|okay|please|just|now)\b[\s,]*)+", re.I
)
#: Verbs and frames that ask to be HANDED something. A clause with one of these
#: and a field named outright is a field ask however it is phrased — "can you
#: give me the totals", "any chance you can pull the totals?", "I need the
#: total from this invoice?" — and is checked for this before its shape is,
#: so "would you mind giving me the invoice number" is not read as asking OUR
#: view the way "would you accept that?" is.
_REQUEST_VERB_RE = re.compile(
    r"\b(?:tell|telling|give|giving|show|showing|send|sending|list|listing|pull|pulling|"
    r"extract|extracting|provide|providing|confirm|confirming|share|sharing|read|reading|"
    r"copy|copying|quote|quoting|fetch|find|get|getting|transcribe|itemi[sz]e|fill|help)\b"
    r"|\b(?:i|we)\s+(?:need|want|would\s+like|'d\s+like|require)\b",
    re.I,
)
_TOKEN_RE = re.compile(r"[$€£₹]|\d[\w,.:/%]*|[a-z]+(?:[-'][a-z]+)*", re.I)

#: Words that carry no question of their own: articles, determiners,
#: pronouns (not "you"), auxiliaries, non-comparative prepositions,
#: conjunctions, wh-words, politeness. Comparatives (more, less, above,
#: below, over, under, than) and degree words (too, very) are deliberately
#: NOT here: "is that above the going rate?" is a judgement.
_CLOSED_CLASS = frozenset("""
a an the this that these those its their our my his her any some each every both either
neither all no another other such same whole entire full own only just
it they them we us i me he she him one ones there here anything something everything
nothing anywhere somewhere elsewhere else
is are was were be been being am do does did done has have had having will would shall
should can could may might must ought cannot isn aren wasn weren don doesn didn hasn
haven hadn won wouldn shouldn couldn s re d ll ve t m
in on at of for to from by with within per about into onto across between after before
during until till via as up out off down through throughout upon regarding against
and or but nor so if whether then also plus
what whats which who whom whose when where how
please kindly thanks thank exactly precisely again now currently
""".split())

#: What a document states ABOUT a field: when, how, by whom, in what form it
#: is paid, stated, signed or counted. A follow-up made only of these ("is it
#: payable in advance?", "is it listed as a separate line?") is a fact on the
#: page, not a judgement.
_FACT_WORDS = frozenset("""
payable paid pay pays owed owe owes stated state states say says said list listed lists
show shows shown give gives given specify specifies specified mention mentions mentioned
include includes included including inclusive exclusive excluding excluded contain
contains contained cover covers covered signed sign signs dated issued issue quoted
quote charged billed invoiced apply applies applied fixed variable refundable
non-refundable renewable renew renews auto-renew auto-renews net gross separately
separate annually monthly yearly quarterly weekly daily annual upfront advance arrears
written printed attached named called start starts begin begins end ends expire expires
terminate terminates calculated based set effective valid located found appear appears
itemised itemized broken payee
""".split())

#: A value, not a claim about one: numbers, currencies, units of time, months,
#: and the basis a price is quoted on ("per unit", "per seat"). The pricing
#: bases were added after the held-out follow-ups were first measured: "is it
#: quoted per unit?" was the only one of the ten that lost strict extraction
#: (120 of 1,330 held-out pure field asks, 90.98% kept), and a price's basis is
#: a value printed on the page like its currency.
_VALUE_WORDS = frozenset("""
usd eur gbp inr aud cad chf jpy cny sgd aed dollar dollars euro euros pound pounds rupee
rupees yen day days week weeks month months year years quarter quarters hour hours annum
calendar business working january february march april may june july august september
october november december jan feb mar apr jun jul aug sep sept oct nov dec two three four
five six seven eight nine ten twelve fifteen twenty thirty sixty ninety hundred thousand
million unit units seat seats user users licence licences license licenses node nodes
device devices piece pieces
""".split())

#: Parts of a document a follow-up may point at ("which clause is it in?").
_DOC_PART_WORDS = frozenset("""
line lines row rows item items column columns entry entries paragraph paragraphs heading
headings footnote footnotes header footer box field fields part
""".split())

#: How a request asks for the answer to be LAID OUT, which an imperative may
#: add without asking anything new ("... and put them in a table").
_FORMAT_WORDS = frozenset("""
put format show keep make present arrange sort sorted order table list bullet bullets
csv json markdown brief briefly short simple plain english copy paste group grouped
""".split())


def _clauses(question: str) -> List[Tuple[str, bool]]:
    """The question's clauses, each with whether a "?" closed it."""
    out: List[Tuple[str, bool]] = []
    pos = 0
    text = question or ""
    for m in _CLAUSE_SPLIT_RE.finditer(text):
        piece = text[pos:m.start()]
        if piece.strip():
            out.append((piece, bool(m.group("q"))))
        pos = m.end()
    tail = text[pos:]
    if tail.strip():
        out.append((tail, False))
    return out


def _residual(text: str, *, imperative: bool = False) -> List[str]:
    """The words of a clause left once everything a VALUE could contain is
    taken away. Empty means the clause is answerable from the document."""
    blank = text
    for rx in (_UNAMBIGUOUS_FIELD_RE, _AMBIGUOUS_FIELD_RE, _DOC_WORD_RE, _EXTRACT_VERB_RE):
        blank = rx.sub(lambda m: " " * (m.end() - m.start()), blank)
    left = []
    for tok in _TOKEN_RE.findall(blank.replace("’", "'")):
        word = tok.lower()
        if word[0].isdigit() or word in ("$", "€", "£", "₹"):
            continue
        if "'" in word:
            word = word.split("'", 1)[0]
        if (word in _CLOSED_CLASS or word in _FACT_WORDS or word in _VALUE_WORDS
                or word in _DOC_PART_WORDS or (imperative and word in _FORMAT_WORDS)):
            continue
        left.append(word)
    return left


def _addresses_the_assistant(text: str, *, possessive_only: bool = False) -> bool:
    """"you" / "your" outside "thank you": the person wants OUR view.

    `possessive_only` is for clauses that are not questions: "your thoughts"
    and "your view on whether that's fair" ask for a view, while "if you can
    find it" is a condition on a field ask.
    """
    rx = r"\byours?\b" if possessive_only else r"\byou(?:r|rs|'d|'re|'ll)?\b"
    return bool(re.search(rx, re.sub(r"\bthank\s+you\b", "", text, flags=re.I), re.I))


#: Inside a field clause, a conjunct that opens with a preposition or an
#: indefinite pronoun is a predicate about the field, not another field:
#: "what is the annual fee and in line with the market?", "... and anything
#: we ought to be careful about there?". A conjunct that opens with a noun or
#: a determiner ("... and the due date", "... and cooling unit prices") is
#: another field and is never tested: without a part-of-speech tagger a bare
#: noun and a bare adjective look alike, and guessing there would cost field
#: asks, not judgement asks.
_PREP_CONJUNCT_RE = re.compile(
    r"\s(?:and|but)\s+(?P<c>(?:in|on|at|with|against|compared|relative|versus|vs|above|"
    r"below|anything|something|any)\b.*)$",
    re.I,
)


def _asks_riding_on_a_field_clause(clause: str) -> List[Tuple[str, str]]:
    """A clause that names a field outright still asks for more when it also
    asks for OUR view ("give me the grand total and your thoughts?") or tacks
    a predicate on with "and in ..." ("what is the annual fee and in line
    with the market?")."""
    if _addresses_the_assistant(clause, possessive_only=True):
        return [("field-clause-to-assistant", clause)]
    conj = _PREP_CONJUNCT_RE.search(clause)
    if conj and not re.match(_WH_START, conj.group("c").lower()) and _residual(conj.group("c")):
        return [("predicate-conjunct", conj.group("c"))]
    return []


def _open_asks(question: str) -> List[Tuple[str, str]]:
    """Clauses that ask something the document's values cannot answer.

    Returns (kind, clause) pairs; empty means every ask in the question is a
    field ask, a fact about a field, or not an ask at all. See step 4 above.
    """
    found: List[Tuple[str, str]] = []
    for raw, closed_by_q in _clauses(question):
        clause = _LEADING_FILLER_RE.sub("", raw.strip()).strip(" ,.")
        if not clause:
            continue
        lower = clause.lower()
        masked = _mask_ordinary_english(clause)
        strong = _field_evidence(masked)[0] >= _FIELD_THRESHOLD
        # A request to hand a field over is a field ask however it is phrased:
        # "can you give me the totals", "any chance you can pull the totals?"
        if strong and _REQUEST_VERB_RE.search(clause):
            found.extend(_asks_riding_on_a_field_clause(clause))
            continue
        body, kind, imperative = clause, "", False
        emb = _EMBEDDED_RE.match(clause)
        if re.match(_WH_START, lower):
            if re.match(r"(?:(?:on|in|at|for|by|from|to|under|of|with)\s+)?why\b", lower):
                found.append(("why", clause))
                continue
            value_wh = not re.match(
                r"(?:(?:on|in|at|for|by|from|to|under|of|with)\s+)?how\b", lower
            ) or bool(_VALUE_HOW_RE.match(lower))
            if value_wh and strong:
                # "what is the unit price of the cooling unit" asks for the
                # field's value whatever else it names.
                found.extend(_asks_riding_on_a_field_clause(clause))
                continue
            kind = "wh-value" if value_wh else "how"
        elif re.match(_POLAR_START, lower):
            kind = "polar"
            # "is there a late fee?", "does the contract state the fee?"
            if strong and re.match(r"(?:is|are|was|were)\s+there\b", lower):
                found.extend(_asks_riding_on_a_field_clause(clause))
                continue
        elif emb:
            kind, body = "embedded", emb.group("body")
            if re.match(_WH_START, body.lower()) and strong:
                found.extend(_asks_riding_on_a_field_clause(clause))
                continue
        elif _IMPERATIVE_RE.match(clause):
            # No field skip here: a request verb with a field was skipped
            # above, and "sanity-check it against what the market charges"
            # carries a wh-frame ("what the market charges") without asking
            # for any field.
            kind, imperative = "imperative", True
        elif closed_by_q:
            kind = "fragment"
        elif _addresses_the_assistant(clause, possessive_only=True):
            found.append(("statement-to-assistant", clause))
            continue
        else:
            continue  # a statement or a noun phrase: nothing is asked
        if _addresses_the_assistant(body):
            found.append((kind + "-to-assistant", clause))
            continue
        left = _residual(body, imperative=imperative)
        if left:
            found.append((kind, clause))
    return found


@dataclass
class Signals:
    """What the classifier saw, and what it concluded.

    The evidence is kept because a misroute is otherwise invisible: the
    2026-09-18 blocker was one lookbehind that excluded "long-term" and not
    "long term", and it took two live runs to find. `evidence` says which
    pattern fired on which words.

    `held_decision_score` is decision evidence that WAS found and then set
    aside by a narrowing rule (today only the value-wh reading of "should").
    It is carried separately instead of being dropped, because dropping it is
    what produced the 2026-09-18 blocker: the classifier recorded
    ("should-inside-a-value-question", "should we") as evidence and routed the
    question to strict extraction anyway. `decision_score` is the score AFTER
    the precedence has run, so `wants_advice` already accounts for it.

    `open_asks` are clauses that ask something no field of the document can
    answer (step 4). They carry no decision WORD, which is the point: they
    are the judgement asks the vocabulary is blind to.
    """

    mode: str
    field_score: int = 0
    decision_score: int = 0
    held_decision_score: int = 0
    evidence: List[Tuple[str, str]] = _dataclass_field(default_factory=list)
    open_asks: List[Tuple[str, str]] = _dataclass_field(default_factory=list)

    @property
    def wants_fields(self) -> bool:
        return self.field_score >= _FIELD_THRESHOLD

    @property
    def wants_advice(self) -> bool:
        return self.decision_score >= _DECISION_THRESHOLD

    @property
    def decision_signal_anywhere(self) -> bool:
        """True when the question carries a judgement ask at all — including
        one a narrowing rule set aside. The module's rule is that this can
        never be answered by strict extraction, and `mode` is asserted against
        it directly in the suite."""
        return (self.decision_score + self.held_decision_score) >= _DECISION_THRESHOLD

    @property
    def asks_beyond_fields(self) -> bool:
        """True when some clause asks what a field cannot answer. Like a
        decision signal, it can never be answered by strict extraction alone."""
        return bool(self.open_asks)


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


@dataclass
class _Decision:
    """The judgement evidence in a question, split by how firmly it counts.

    `held` is evidence a narrowing rule set aside. It is returned rather than
    dropped so that _apply_precedence() — not a regex — decides what a set
    aside signal is allowed to cost.
    """

    score: int = 0
    held: int = 0
    why: List[Tuple[str, str]] = _dataclass_field(default_factory=list)
    held_why: List[Tuple[str, str]] = _dataclass_field(default_factory=list)


def _decision_evidence(question: str) -> _Decision:
    """Points for "the person asked for a judgement", and why.

    Reads the question UNMASKED. The mask exists to stop ordinary English
    being mistaken for a field, and the same caution does not apply here: a
    decision signal read out of "best practice" costs a sentence of context,
    which is the direction this module is allowed to be wrong in.
    """
    out = _Decision()

    hit = _DECISION_RE.search(question)
    if hit:
        out.score += _STRONG
        out.why.append(("decision-marker", hit.group(0).strip()))

    should = _SHOULD_RE.search(question)
    if should:
        decides = bool(_DECIDE_AFTER_SHOULD_RE.search(question))
        if decides or not _VALUE_WH_RE.match(question):
            out.score += _STRONG
            out.why.append(("should", should.group(0)))
        else:
            # HELD, not discarded. _VALUE_WH_RE is anchored to the start of
            # the whole question and this "should" may be two clauses later.
            out.held += _STRONG
            out.held_why.append(("should-inside-a-value-question", should.group(0)))

    if out.score < _DECISION_THRESHOLD:
        person = _FIRST_PERSON_RE.search(question)
        suits = _SUITABILITY_RE.search(question)
        if person and suits:
            out.score += _WEAK
            out.why.append(("first-person-plus-suitability", suits.group(0)))

    return out


def _apply_precedence(
    field_score: int,
    decision: _Decision,
    field_why: Sequence[Tuple[str, str]] = (),
    open_asks: Sequence[Tuple[str, str]] = (),
) -> Signals:
    """Turn two scores over the WHOLE question into a mode.

    THE ASYMMETRY, as one statement instead of as regex anchoring. Both scores
    are computed over the entire question, neither pattern knows where in the
    sentence the other one fired, and this function alone decides what wins.

    The one rule that needs saying out loud, because breaking it IS the
    incident this module exists to prevent:

        SETTING A DECISION SIGNAL ASIDE MAY SEND A QUESTION TO NEUTRAL. IT MAY
        NEVER SEND ONE TO STRICT EXTRACTION.

    A narrowing rule (today only _VALUE_WH_RE) says "this 'should' is asking
    for a value, not for a verdict". That reading is safe while nothing else
    in the question asks for a value — the question falls to NEUTRAL, which
    answers what was asked and still permits a judgement. It stops being safe
    the moment the FIELD score reaches its threshold, because the question has
    then proved twice over that it contains a value ask, so the "should" is a
    SECOND and separate thing being asked, and the strict EXTRACTION block
    then said "do not add advice, a recommendation, a next step, a caution or
    an offer of further help" (qualified since round 5 to what the person did
    not ask for — see EXTRACTION).

    Measured 2026-09-18, live, twelve runs on one invoice fixture, only the
    first word of the question differing. BEFORE: "what is the total, and
    should we accept it?" (mode extract) refused the judgement half 2 of 3 --
    "I cannot provide a definitive \"yes\" or \"no\" because the document does
    not contain your purchase order, budget approval, or contract terms" --
    while "tell me the total, and should we accept it?" (mode extract+advise)
    answered it 3 of 3. AFTER: both wordings are extract+advise and both give
    the figure and then "**Should we accept it?** **Yes.**"

    So held evidence is restored exactly when the fields would otherwise win,
    and the result is extract+advise. The post-condition is checked below and
    pinned by a generated cross-product in the suite.
    """
    restored = bool(decision.held) and field_score >= _FIELD_THRESHOLD

    why = list(field_why) + list(decision.why)
    if restored:
        why += [("should-restored-beside-a-field-ask", t) for _, t in decision.held_why]
    else:
        why += list(decision.held_why)

    sig = Signals(
        mode="",
        field_score=field_score,
        decision_score=decision.score + (decision.held if restored else 0),
        held_decision_score=0 if restored else decision.held,
        evidence=why,
        open_asks=list(open_asks),
    )
    if sig.wants_fields and sig.wants_advice:
        sig.mode = "extract+advise"
    elif sig.wants_advice:
        sig.mode = "advise"
    elif sig.wants_fields:
        sig.mode = "extract"

    # Step 4 (round 5): a clause that asks what no field can answer is a
    # second ask with no decision WORD in it. Beside a field ask it gets the
    # fields strictly and then an answer, exactly as a decision word would —
    # "what is the annual fee in this contract, and is that in line with the
    # market?" is the same question as "... and is that reasonable?".
    if sig.mode == "extract" and sig.asks_beyond_fields:
        sig.mode = "extract+advise"
        sig.evidence.extend(("open-ask-" + kind, text) for kind, text in sig.open_asks)

    # The rule above as a NET rather than as a hope. Nothing reaches it while
    # the scores keep their documented values — restoring held evidence above
    # already rules the combination out — but the two directions cost very
    # different things (a sentence of context against the owner's complaint),
    # so the cheap direction is the one a future edit falls into.
    if sig.mode == "extract" and (sig.decision_signal_anywhere or sig.asks_beyond_fields):
        sig.mode = "extract+advise"
    return sig


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

    Both scores are taken over the whole question; _apply_precedence() holds
    the whole of rule 1, including what happens to a decision signal a
    narrowing rule set aside — and, since round 5, to a clause that asks what
    no field can answer (step 4). That clause test runs only when the fields
    would otherwise win: it exists to stop strict extraction ALONE, and a
    question with no field in it has no strict block to be saved from.
    """
    q = question or ""
    masked = _mask_ordinary_english(q)

    field_score, field_why = _field_evidence(masked)
    open_asks = _open_asks(q) if field_score >= _FIELD_THRESHOLD else []
    return _apply_precedence(field_score, _decision_evidence(q), field_why, open_asks)


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
