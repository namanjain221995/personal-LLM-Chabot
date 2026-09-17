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
_REFUSAL_RE = re.compile(
    r"(does not (mention|contain|cover|discuss|provide|include|reference|specify|say)"
    r"|doesn'?t (mention|contain|cover|discuss|provide|include|reference|specify|say)"
    r"|no mention of|there is no (mention|reference|information)"
    r"|is not mentioned|are not mentioned|not covered (in|by) the document"
    r"|the short answer is no|based on the (provided|attached|uploaded) )",
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
    r"(?:setup|set-up|cluster|pair|sparks?|situation|case|plan|scale)\b)",
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
    """True when the FIRST two sentences are about the document's silence."""
    head = " ".join(re.split(r"(?<=[.!?])\s+", (answer or "").strip())[:2])
    return bool(_REFUSAL_RE.search(head))


def gives_recommendation(answer: str) -> bool:
    return bool(_RECOMMENDATION_RE.search(answer or ""))


def cites_document(answer: str, figures: list[str]) -> bool:
    """True when at least one of the document's OWN figures is quoted."""
    return any(f.lower() in (answer or "").lower() for f in figures)


def referral_only(answer: str) -> bool:
    """A referral to the vendor with no recommendation of our own."""
    return bool(_REFERRAL_RE.search(answer or "")) and not gives_recommendation(answer)
