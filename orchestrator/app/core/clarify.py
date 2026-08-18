"""The deterministic floor under the clarification planner.

The engine's failure mode is not that it refuses — it is that it picks one
reading silently and reports the number with full confidence. Asked "how many
candidates completed the training from slot 128 and how many failed the mock",
three consecutive runs scoped the mock three different ways (through the
training, through the candidate, and through a name LIKE '%Mock%') and returned
7, 20 and 0. Every one of those is a defensible reading of the English. Only one
is what the asker meant, and nothing in the pipeline could know which.

These detectors encode the ambiguities that were MEASURED against this org's own
data, so when the planner is unreachable or produces nothing valid the request
still gets a question worth asking rather than a coin flip. They are not the
clarification system: `core/sf_intel/` is, and it owns the persistence, the
resume, the loop guards and the UI contract. This module is what that system
falls back to (see `sf_intel/planner.deterministic_decision`).

Two rules hold everywhere here:

  * a `label` and a `description` are read by a person, so they contain no
    Salesforce API names — "Interview__c" tells a recruiter nothing and reads as
    a leak;
  * `resolves_to` is read by the query planner, so it names objects and fields
    precisely. It is carried on the option's machine-facing `value` and is never
    displayed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Option:
    label: str
    description: str = ""
    #: The precise, machine-facing reading. Never shown to the user; it becomes
    #: `ClarificationOption.value`, which is what the query planner reads.
    resolves_to: str = ""


@dataclass
class Clarification:
    question: str
    options: List[Option] = field(default_factory=list)
    #: Why we stopped — shown small, so the user can see it was not arbitrary.
    reason: str = ""
    #: Two-to-four word topic label for the card header.
    header: str = "Salesforce"


# ── Detectors ────────────────────────────────────────────────────────────────
# Each returns a Clarification or None. Ordered: the most consequential
# ambiguity wins, because we only ever ask one question.

_MOCK_RE = re.compile(r"\bmocks?\b", re.I)
_SLOT_RE = re.compile(r"\b(slot|cohort|batch)\b", re.I)
_INTERVIEW_RE = re.compile(r"\binterviews?\b", re.I)
#: A capitalised name after "for". Case-SENSITIVE on purpose: the whole signal
#: is the capital letter, and `re.I` made this match "for the", "for each" and
#: "for all", so every question containing those words was treated as already
#: scoped to a person.
_PERSON_HINT_RE = re.compile(r"\bfor\s+([A-Z][a-z]{2,})")
_TREND_RE = re.compile(r"\b(trend|over time|by month|by week|monthly|weekly)\b", re.I)
#: "Interview Readiness Training" is the name of a PROGRAM. Asked for its
#: sessions, the interview-object question fired on the bare word "interview"
#: and interrupted a question that was never about interviews at all.
_PROGRAM_NAME_RE = re.compile(r"\binterview\s+(readiness|skills|rejection)\b", re.I)
_SESSION_RE = re.compile(r"\bsessions?\b", re.I)
_COUNTING_RE = re.compile(r"\b(how many|count of|total number|volume)\b", re.I)
#: A question ABOUT the platform rather than about its records. "How does the
#: interview process work" needs an explanation, not a choice of object.
_CONCEPTUAL_RE = re.compile(
    r"\b(how (does|do|is|are)|what (is|are|does)|explain|why (does|do|is|are)|"
    r"tell me about|describe)\b",
    re.I,
)


def _mock_scope(question: str) -> Optional[Clarification]:
    """"Failed the mock" in a slot: scoped how?

    Measured on Slot 128: through that slot's training = 7 failures; through
    those candidates across every training they ever had = 20. Both are real.
    """
    if not (_MOCK_RE.search(question) and _SLOT_RE.search(question)):
        return None
    return Clarification(
        header="Mock scope",
        question="Which mocks should I count?",
        reason=("Mocks belong to a training, and a candidate in this slot may "
                "have had earlier trainings with their own mocks."),
        options=[
            Option(
                "Only mocks from this slot's training",
                "The mock attached to the training they took in this slot.",
                "Count only Internal_Interview__c whose Candidate_Training__c "
                "belongs to that Cohort__c.",
            ),
            Option(
                "Every mock those candidates ever took",
                "Including mocks from their other trainings.",
                "Count every Internal_Interview__c for the candidates enrolled "
                "in that Cohort__c, across all their trainings.",
            ),
        ],
    )


def _which_interview(question: str) -> Optional[Clarification]:
    """"Interview" names two different objects in this org."""
    if not _INTERVIEW_RE.search(question):
        return None
    if _MOCK_RE.search(question) or re.search(r"\binternal\b", question, re.I):
        return None  # already disambiguated by the asker
    if _PROGRAM_NAME_RE.search(question):
        return None  # the word belongs to a programme name, not the object
    if _SESSION_RE.search(question):
        return None  # asking about sessions, not about interviews
    if _CONCEPTUAL_RE.search(question):
        # "How does the interview process work?" wants an explanation. Offering
        # a choice of Salesforce object in reply to it is a non-sequitur, and it
        # was the most common way this detector fired on a question that had no
        # records behind it at all.
        return None
    return Clarification(
        header="Interview type",
        question="Which interviews do you mean?",
        reason="This org keeps client-facing interviews and internal "
               "assessments on separate records.",
        options=[
            Option("Client-facing interviews",
                   "Interviews candidates sat with employers (the busy one).",
                   "Use Interview__c with record type 'Interview'."),
            Option("Include initial calls too",
                   "Adds the screening calls kept alongside them.",
                   "Use Interview__c across record types 'Interview' and "
                   "'Initial Call', and split the figures."),
            Option("Internal assessments and mocks",
                   "OOT, Intake and the per-week programme mocks.",
                   "Use Internal_Interview__c joined to Interview_Type__c."),
        ],
    )


def _missing_period(question: str) -> Optional[Clarification]:
    """A trend or volume question with no period is a different question per
    reader: this month, this year, or since the org began.

    The period test is `interpret.satisfied_slots`, which is the same one the
    planner policy uses — so a question this detector lets through is exactly a
    question the policy would also let through, and the two can never disagree
    about whether "todau" said today.
    """
    from .sf_intel import interpret

    if _TREND_RE.search(question):
        return None
    if "date_range" in interpret.satisfied_slots(question):
        return None
    # Grouped and bounded on purpose: `\bhow many|count|...\b` alternates
    # across the whole pattern, so the bare `count` branch matched the middle
    # of "ac(count)s" and asked "over what period?" about "top accounts".
    if not _COUNTING_RE.search(question):
        return None
    if _PERSON_HINT_RE.search(question) or _SLOT_RE.search(question):
        return None  # already scoped to a person or a slot
    return Clarification(
        header="Time period",
        question="Over what period?",
        reason="Without a period this counts everything ever recorded.",
        options=[
            Option("This month", "", "Limit to the current calendar month."),
            Option("Last 3 months", "", "Limit to the last three months."),
            Option("All time", "Everything on record.", "Do not filter by date."),
        ],
    )


#: Order matters — the first match is the one question we ask.
_DETECTORS = (_mock_scope, _which_interview, _missing_period)


def needs_clarification(question: str) -> Optional[Clarification]:
    """The one question worth asking before querying, or None.

    Runs against the SPELLING-REPAIRED reading of the request, so "how many
    advance mock scheddule todau" is understood as the request it is rather
    than met with a question about its period.
    """
    from .sf_intel import interpret

    text = interpret.read(question or "").text
    for detector in _DETECTORS:
        found = detector(text)
        if found is not None:
            return found
    return None
