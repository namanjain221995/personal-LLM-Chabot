"""Graders for a document ANSWER — shared by the offline and the live test.

The offline test pins them against the two real answers from 2026-09-17: what
this platform said about the Vertiv SmartRow brochure (a refusal plus a
referral) and what ChatGPT said about the same PDF and the same question (a
recommendation with the brochure's own kW figures). The live test then runs
the graders against a real engine answer, so both tests judge by one standard.

These are deliberately crude — they look for the SHAPE of an answer, not its
quality. A grader that passed the bad answer would be worse than none.
"""
from __future__ import annotations

import re

#: "The short answer is no. The ... document does not mention ..." — an answer
#: whose opening is a statement about the document's coverage rather than an
#: answer to the question. Only the first two sentences are examined: saying
#: the document is silent is REQUIRED, once, after the answer has started.
#:
#: Split in two because the SUBJECT decides which it is. "does not include" is
#: the refusal when its subject is the paper and a verdict when its subject is
#: the product, and this round measured the difference: 1 of 6 live runs of
#: the owner's turn opened "**Recommendation: No ...** While the SmartRow
#: provides the physical infrastructure, it does not include the compute
#: hardware, networking, or software stack required to run DGX Sparks" and was
#: scored a refusal — the exact opposite of what it is.
_REFUSAL_RE = re.compile(
    r"(does not (mention|contain|cover|discuss|provide|include|reference|specify|say)"
    r"|doesn'?t (mention|contain|cover|discuss|provide|include|reference|specify|say)"
    r"|is not mentioned|are not mentioned)",
    re.I,
)

#: Refusal phrases that already name the paper, so they need no subject test.
_ANCHORED_REFUSAL_RE = re.compile(
    r"(no mention of|there is no (mention|reference|information)"
    r"|not covered (in|by) the document|not stated in the document"
    r"|the short answer is no|based on the (provided|attached|uploaded) )",
    re.I,
)

#: The paper, as the subject of a sentence.
_DOC_SUBJECT_RE = re.compile(
    r"\b(document|documents|brochure|datasheet|data sheet|spec sheet|invoice|contract|"
    r"agreement|excerpt|text|paper|file|pdf|page|pages|policy|sheet|attachment|manual|"
    r"provided|attached|uploaded|shared)\b",
    re.I,
)

#: A recommendation: a verdict about the person's own situation. Deliberately
#: NOT "you should" — the answer this round fixes said "What you should do:
#: Check NVIDIA Documentation", which is a referral wearing a verdict's words.
_RECOMMENDATION_RE = re.compile(
    r"(\bi(?: would|'d)? (?:recommend|suggest|advise)\b|\brecommendations?\b"
    r"|\brecommended\b|\byou (?:do not|don'?t|won'?t) need\b"
    r"|\byou would (?:not )?need\b|\byou (?:do not|don'?t) have to\b"
    r"|\bworth (?:it|the)\b|\bnot worth\b|\boverkill\b|\bmakes sense\b"
    r"|\bdoes ?n[o']?t make sense\b|\bunnecessary\b|\bnot necessary\b"
    r"|\bmore than you need\b|\bonly if you\b"
    r"|\bif you (?:plan|expand|grow|scale|move|add|go)\b"
    r"|\bbecomes? (?:useful|relevant|worth|worthwhile)\b"
    r"|\b(?:would|will|can|could) (?:be )?(?:useful|helpful|relevant|beneficial"
    r"|suitable|a good fit|worth)\b|\bwould help\b|\bwould work\b|\bgood fit\b"
    r"|\bfor your (?:current |existing |two |2 )?"
    r"(?:setup|set-up|cluster|pair|sparks?|situation|case|plan|scale)\b"
    # Measured 2026-09-18: a live run opened "**Verdict: No, this is not the
    # right long-term choice.**" and scored FALSE. A verdict the grader
    # cannot see is a live test that fails for being right. None of these
    # matches the answer this round fixes, which is the constraint they are
    # written under -- "the short answer is no" deliberately stays out.
    r"|\bverdict\b|\bbottom line\b"
    r"|\b(?:is|are|was|were)(?: not)? the (?:right|wrong|best) "
    r"(?:choice|fit|option|one|product|unit|solution|tool|move|call)\b"
    r"|\bnot the right\b|\bthe wrong (?:choice|fit|tool|product)\b"
    r"|\bi would(?: not)? (?:sign|renew|buy|use|go|choose|pick|deploy|install)\b"
    r"|\b(?:it|this|that) (?:is|would be)(?: not)? (?:a )?"
    r"(?:good|bad|poor|sensible|wise|sound|solid) "
    r"(?:choice|fit|idea|option|investment|buy|match)\b)",
    re.I,
)

#: An answer that ends by handing the question to someone else and nothing more.
_REFERRAL_RE = re.compile(
    r"\b(check (with )?(nvidia|vertiv|the (vendor|manufacturer))"
    r"|consult (nvidia|vertiv|the (vendor|manufacturer|supplier))"
    r"|contact (nvidia|vertiv|the (vendor|manufacturer))"
    r"|refer to (nvidia|vertiv|the manufacturer))",
    re.I,
)


def opens_with_refusal(answer: str) -> bool:
    """True when the FIRST two sentences are about the DOCUMENT's silence.

    Sentence by sentence, not over the pair joined together: a verdict about
    the product in one sentence and the word "document" in the next must not
    add up to a refusal that neither of them made.
    """
    for sentence in re.split(r"(?<=[.!?])\s+", (answer or "").strip())[:2]:
        if _ANCHORED_REFUSAL_RE.search(sentence):
            return True
        if _REFUSAL_RE.search(sentence) and _DOC_SUBJECT_RE.search(sentence):
            return True
    return False


def gives_recommendation(answer: str) -> bool:
    return bool(_RECOMMENDATION_RE.search(answer or ""))


def cites_document(answer: str, figures: list[str]) -> bool:
    """True when at least one of the document's OWN figures is quoted."""
    return any(f.lower() in (answer or "").lower() for f in figures)


def referral_only(answer: str) -> bool:
    """A referral to the vendor with no recommendation of our own."""
    return bool(_REFERRAL_RE.search(answer or "")) and not gives_recommendation(answer)


#: A line that is a HEADING rather than a sentence: a markdown heading, a
#: numbered bold heading, or a line that is nothing but a bold phrase. A bold
#: LABEL with content after it on the same line ("**Total:** 5,200.00 USD") is
#: not a heading and must not be caught.
_HEADING_LINE_RE = re.compile(
    r"^\s*(?:#{1,6}\s+(?P<hash>.+?)"
    r"|(?P<num>\d+[.)]\s+\*\*[^*]{1,90}\*\*\s*:?)"
    r"|\*\*(?P<bold>[^*]{1,90})\*\*\s*:?)\s*$"
)

#: Numbering and emphasis in front of the heading's actual words.
_HEADING_LEAD_RE = re.compile(r"^[\s*#>_`-]*(?:\d+[.)]\s*)?[\s*_`]*")

#: A heading whose SUBJECT is a source rather than what the section is about.
#: Pinned against what the 2026-09-18 recheck and this round actually saw
#: live: "### 1. The Document's Limitations", "### 2. General Knowledge: DGX
#: Spark Requirements", "### 3. This Conversation: Your Scale", "**THE
#: DOCUMENT**" / "**GENERAL KNOWLEDGE**" / "**THIS CONVERSATION**" verbatim,
#: "**The Document's Limits:**", "### 1. What the Document Says (Constraints)"
#: and "### 1. The Document's Specifications".
#:
#: Anchored at the start on purpose. "### 4. Critical Gaps in the Document" is
#: NOT caught: the subject there is the gaps and the source is a trailing
#: qualifier, which is ordinary English and not the template failure. A grader
#: that flagged it would make the live assertion unfalsifiable.
_SOURCE_HEADING_RE = re.compile(
    r"^(?:the\s+)?(?:"
    r"documents?\s*$|documents?\s*[:\u2013\u2014-]"
    r"|document(?:'s|\u2019s)\s+\w+"
    r"|general\s+knowledge|outside\s+knowledge|my\s+(?:own\s+)?knowledge"
    r"|(?:my|your|our)\s+\w+\s+knowledge"
    r"|(?:this|our|the)\s+conversation"
    r"|from\s+the\s+documents?"
    r"|what\s+(?:the\s+)?documents?\s+(?:says?|does|do|contains?|covers?|states?|"
    r"shows?|provides?|lacks?|misses|leaves?|gives?|tells?)"
    r"|what\s+(?:is|\u2019s|'s)\s+(?:not\s+)?in\s+the\s+documents?"
    # the three-source template rebuilt out of words the ban did not name:
    # "What I Know (General Contract & Business Context)" and "What This
    # Conversation Tells Me About You", live, 2026-09-18
    r"|what\s+(?:i|you|we)\s+(?:already\s+)?know"
    r"|what\s+(?:this|our|the)\s+conversation"
    r"|not\s+in\s+the\s+documents?|missing\s+from\s+the\s+documents?"
    r"|sources?\s*$|sources?\s*[:\u2013\u2014-]"
    r")",
    re.I,
)


def source_named_headings(answer: str) -> list[str]:
    """Headings that name a SOURCE instead of a subject.

    BASE tells the model its three sources are where the answer comes from and
    not how it is laid out. Reading the prompt cannot show whether that held;
    only an answer can. This returns the offending heading lines so a live test
    can assert the list is empty and print it when it is not.
    """
    found = []
    for line in (answer or "").splitlines():
        m = _HEADING_LINE_RE.match(line)
        if not m:
            continue
        text = m.group("hash") or m.group("num") or m.group("bold") or ""
        text = _HEADING_LEAD_RE.sub("", text).strip()
        if _SOURCE_HEADING_RE.match(text):
            found.append(line.strip())
    return found
