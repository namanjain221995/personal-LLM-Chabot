"""Ask before answering, when the question has more than one honest reading.

The engine's failure mode is not that it refuses — it is that it picks one
reading silently and reports the number with full confidence. Asked "how many
candidates completed the training from slot 128 and how many failed the mock",
three consecutive runs scoped the mock three different ways (through the
training, through the candidate, and through a name LIKE '%Mock%') and returned
7, 20 and 0. Every one of those is a defensible reading of the English. Only one
is what the asker meant, and nothing in the pipeline could know which.

So the ambiguity is put to the user before a query is written, not resolved by
coin flip afterwards. Detection is deterministic — no model call — because a
clarifying step that itself guesses is worse than none.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

#: Free-text escape, mirroring the "Other" row in a Claude Code question.
OTHER = "Something else — let me type it"
#: Always offered: a question the asker considers already clear must have a
#: one-click way past it.
PROCEED = "Proceed without narrowing"


@dataclass
class Option:
    label: str
    description: str = ""
    #: Appended to the original question when chosen, so the follow-up turn
    #: carries the resolved reading rather than the ambiguous one.
    resolves_to: str = ""


@dataclass
class Clarification:
    question: str
    options: List[Option] = field(default_factory=list)
    #: Why we stopped — shown small, so the user can see it was not arbitrary.
    reason: str = ""

    def wire(self, original: str = "") -> dict:
        """The payload the UI renders.

        `original` and each option's `send` are what make this loop-proof: the
        client sends back the FULL resolved question, so nothing downstream has
        to reconstruct which question was being clarified. Relying on
        conversation history for that failed — the assistant turn is not always
        in the history the client posts, so "Yes, run it" arrived as a bare
        question of its own and was clarified again, and again.
        """
        return {
            "question": self.question,
            "reason": self.reason,
            "original": original,
            "options": [
                {
                    "label": o.label,
                    "description": o.description,
                    "send": resolve(original, o.resolves_to or o.label),
                }
                for o in self.options
            ] + [
                {
                    "label": PROCEED,
                    "description": "Answer as I asked, without narrowing it.",
                    "send": resolve(original, "Answer exactly as asked; do not narrow the scope."),
                },
                {"label": OTHER, "description": "", "send": "", "free_text": True},
            ],
        }

    def as_text(self) -> str:
        """The same thing as plain text, so it works before any UI exists."""
        lines = [self.question, ""]
        for i, o in enumerate(self.options, start=1):
            lines.append(f"**{i}.** {o.label}" + (f" — {o.description}" if o.description else ""))
        lines.append(f"**{len(self.options) + 1}.** {PROCEED}")
        lines.append(f"**{len(self.options) + 2}.** {OTHER}")
        if self.reason:
            lines += ["", f"_{self.reason}_"]
        return "\n".join(lines)


# ── Detectors ────────────────────────────────────────────────────────────────
# Each returns a Clarification or None. Ordered: the most consequential
# ambiguity wins, because we only ever ask one question.

_MOCK_RE = re.compile(r"\bmocks?\b", re.I)
_SLOT_RE = re.compile(r"\b(slot|cohort|batch)\b", re.I)
_INTERVIEW_RE = re.compile(r"\binterviews?\b", re.I)
_TRAINING_RE = re.compile(r"\btrainings?\b", re.I)
_PERSON_HINT_RE = re.compile(r"\bfor\s+([A-Z][a-z]{2,})", re.I)
_TIME_RE = re.compile(
    r"\b(today|yesterday|this week|last week|this month|last month|this "
    r"quarter|last quarter|this year|last year|\d{4}|jan|feb|mar|apr|may|jun|"
    r"jul|aug|sep|oct|nov|dec|between|since|right now|now|from \d)\b",
    re.I,
)
_TREND_RE = re.compile(r"\b(trend|over time|by month|by week|monthly|weekly)\b", re.I)
#: "Interview Readiness Training" is the name of a PROGRAM. Asked for its
#: sessions, the interview-object question fired on the bare word "interview"
#: and interrupted a question that was never about interviews at all.
_PROGRAM_NAME_RE = re.compile(
    r"\binterview\s+(readiness|skills|rejection)\b", re.I
)
_SESSION_RE = re.compile(r"\bsessions?\b", re.I)
_COUNTING_RE = re.compile(r"\b(how many|count of|total number|volume)\b", re.I)


def _mock_scope(question: str) -> Optional[Clarification]:
    """"Failed the mock" in a slot: scoped how?

    Measured on Slot 128: through that slot's training = 7 failures; through
    those candidates across every training they ever had = 20. Both are real.
    """
    if not (_MOCK_RE.search(question) and _SLOT_RE.search(question)):
        return None
    return Clarification(
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
    return Clarification(
        question="Which interviews do you mean?",
        reason="This org keeps client-facing interviews and internal "
               "assessments on separate objects.",
        options=[
            Option("Client-facing interviews",
                   "Interviews candidates sat with employers (the busy one).",
                   "Use Interview__c with record type 'Interview'."),
            Option("Include initial calls too",
                   "Adds the screening calls kept on the same object.",
                   "Use Interview__c across record types 'Interview' and "
                   "'Initial Call', and split the figures."),
            Option("Internal assessments / mocks",
                   "OOT, Intake and the per-week programme mocks.",
                   "Use Internal_Interview__c joined to Interview_Type__c."),
        ],
    )


def _missing_period(question: str) -> Optional[Clarification]:
    """A trend or volume question with no period is a different question per
    reader: this month, this year, or since the org began."""
    if _TIME_RE.search(question) or _TREND_RE.search(question):
        return None
    # Grouped and bounded on purpose: `\bhow many|count|...\b` alternates
    # across the whole pattern, so the bare `count` branch matched the middle
    # of "ac(count)s" and asked "over what period?" about "top accounts".
    if not _COUNTING_RE.search(question):
        return None
    if _PERSON_HINT_RE.search(question) or _SLOT_RE.search(question):
        return None  # already scoped to a person or a slot
    return Clarification(
        question="Over what period?",
        reason="Without a period this counts everything ever recorded.",
        options=[
            Option("This month", "", "Limit to the current calendar month."),
            Option("Last 3 months", "", "Limit to the last three months."),
            Option("All time", "Everything in the warehouse.", "Do not filter by date."),
        ],
    )


#: Order matters — the first match is the one question we ask.
_DETECTORS = (_mock_scope, _which_interview, _missing_period)


def _reading(question: str) -> str:
    """A plain-English statement of what we are about to query.

    Derived from the same deterministic matchers the query grounding uses, so
    the confirmation describes what will ACTUALLY run rather than a second,
    prettier guess at it.
    """
    from . import org_brief

    parts = []
    metrics = [m["name"] for m in org_brief.match_metrics(question)]
    if metrics:
        parts.append("measure: " + ", ".join(metrics))
    tables = org_brief.tables_for(question)
    if tables:
        parts.append("from: " + ", ".join(tables[:6]))
    period = _TIME_RE.search(question)
    parts.append(f"period: {period.group(0)}" if period else "period: all time")
    return " · ".join(parts)


def confirmation(question: str) -> Clarification:
    """The always-on check, for a question with no specific ambiguity.

    Cheap to accept — one click — and it puts the reading in front of the user
    BEFORE a number exists to anchor on. Wrong readings are much easier to spot
    here than in a confident paragraph afterwards.
    """
    return Clarification(
        question="Before I run this — have I read it right?",
        reason=_reading(question),
        options=[
            Option("Yes, run it", "", "Proceed exactly as asked."),
            Option("Change the time period", "",
                   "Ask me which period to use before querying."),
            Option("Use a different object", "",
                   "Ask me which Salesforce object to read before querying."),
        ],
    )


def needs_clarification(question: str, always: bool = False) -> Optional[Clarification]:
    """The one question worth asking before querying, or None.

    `always` turns every Salesforce question into a confirmation, which is the
    owner's stated preference: a wrong reading is far cheaper to catch here
    than after a confident number has been produced from it.
    """
    text = question or ""
    for detector in _DETECTORS:
        found = detector(text)
        if found is not None:
            return found
    return confirmation(text) if always else None


def resolve(question: str, choice: str) -> str:
    """Fold a chosen option back into the question for the follow-up turn."""
    return f"{question}\n\n(Clarified: {choice})"


def is_clarification(text: str) -> bool:
    """Was this assistant message one of ours?

    Recognised by structure rather than a stored flag, because the history sent
    back to the model carries only role and content — meta is stripped — so a
    marker outside the text cannot survive the round trip.
    """
    return bool(text) and OTHER in text and "**1.**" in text


def answered(history: Sequence[dict], text: str) -> Optional[str]:
    """The resolved question, when this message answers our last question.

    Without this the clarification loops forever: the user picks "Only mocks
    from this slot's training", that sentence itself contains "mock" and
    "slot", the detector fires again, and the same question comes back. Seen
    three times in a row before this existed.

    Returns None when the previous turn was not a clarification, so an ordinary
    question is unaffected.
    """
    if not history:
        return None
    previous = None
    original = None
    for entry in reversed(history):
        role = entry.get("role")
        content = entry.get("content") or ""
        if previous is None:
            if role != "assistant":
                return None  # the last turn was not our question
            if not is_clarification(content):
                return None
            previous = content
            continue
        if role == "user":
            original = content
            break
    if original is None:
        return None

    # The detector is deterministic, so re-running it on the original question
    # reproduces exactly the options the user was shown.
    asked = needs_clarification(original)
    if asked is None:
        return None

    reply = (text or "").strip()
    chosen: Optional[str] = None
    if reply[:1].isdigit():
        index = int(re.match(r"\d+", reply).group()) - 1
        if 0 <= index < len(asked.options):
            chosen = asked.options[index].resolves_to
        else:
            chosen = reply  # "Other", or a number past the list
    else:
        for option in asked.options:
            # The UI sends the label, sometimes with the description appended.
            if reply.lower().startswith(option.label.lower()[:24]):
                chosen = option.resolves_to
                break
    return resolve(original, chosen or reply)
