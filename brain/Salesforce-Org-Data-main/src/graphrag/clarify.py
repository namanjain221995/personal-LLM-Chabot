"""Turn an unresolved ambiguity into a question a person can actually answer.

The resolver deliberately refuses to guess when a term has more than one
meaning. That refusal is only useful if the caller can act on it, and a caller
cannot put `field:Interview__c.Interview_Outcome__c` in front of a user and
expect a sensible choice. This module renders each candidate as a line that
says what picking it would mean, in the org's own vocabulary.

Three steps, and the third is what stops the same question being asked twice:

    question = clarify(bundle, resolution)          ask
    chosen   = apply(bundle, question, ["a"])       act
    snippet  = propose_curated(question, "a")       remember

`propose_curated` emits YAML rather than editing `curated.yaml` itself. A
lexicon entry is a human decision about what the business means, and a decision
made once in a chat window is not the same as one a Salesforce owner has
signed. The snippet goes in front of that person; the file stays reviewed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Iterable, Sequence

from .resolver import Bundle, Candidate, Resolution

# More than this and a question stops being a choice and becomes a list.
MAX_OPTIONS = 5

# A gap this wide means one candidate is genuinely ahead; it is offered as the
# recommendation so a user who does not care can accept and move on.
RECOMMEND_MARGIN = 0.15

# When a curated entry is one of the options, the question is about a real
# business distinction. Weak single-token matches alongside it are not rival
# readings, they are noise, and listing them makes the question look arbitrary:
# "candidate" offering Candidate_Owner_Name__c invites a wrong answer.
CURATED_OPTION_FLOOR = 0.6

_LETTERS = "abcdefghij"


class ClarifyError(RuntimeError):
    """Raised when a choice does not match the question it answers."""


@dataclass
class Option:
    id: str
    component_id: str
    headline: str
    detail: str
    score: float
    tier: str
    implied_filter: str | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Question:
    surface: str
    text: str
    options: list[Option] = dataclass_field(default_factory=list)
    recommended: str | None = None
    allow_multiple: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "text": self.text,
            "options": [o.as_dict() for o in self.options],
            "recommended": self.recommended,
            "allow_multiple": self.allow_multiple,
        }

    def render(self) -> str:
        """Plain text, for a terminal or a prompt."""
        lines = [self.text]
        for option in self.options:
            mark = " (suggested)" if option.id == self.recommended else ""
            lines.append(f"  {option.id}) {option.headline}{mark}")
            if option.detail:
                lines.append(f"     {option.detail}")
        return "\n".join(lines)


def _describe_option(bundle: Bundle, candidate: Candidate) -> tuple[str, str]:
    """One headline and one explanatory line for a candidate.

    The explanation is what makes a choice possible: two options reading
    "Invoice" and "Invoice" are not a question. Field counts, custom-vs-standard
    and picklist values are what actually separate them.
    """
    row = bundle.db.execute(
        "SELECT kind, api_name, qualified_name, label, description, is_custom,"
        " is_stub, parent_id FROM component WHERE id = ?",
        (candidate.component_id,)).fetchone()
    if row is None:
        return candidate.component_id, ""

    name = row["qualified_name"]
    headline = f'{name}' + (f' — "{row["label"]}"' if row["label"] else "")

    parts: list[str] = []
    if row["kind"] == "object":
        parts.append("custom object" if row["is_custom"] else "standard object")
        if row["is_stub"] and not row["is_custom"]:
            parts.append("not customized in this org")
        else:
            count = bundle.db.execute(
                "SELECT count(*) FROM component WHERE parent_id=? AND kind='field'",
                (candidate.component_id,)).fetchone()[0]
            if count:
                parts.append(f"{count} fields")
    elif row["kind"] == "field":
        props = bundle.db.execute(
            "SELECT properties FROM component WHERE id=?", (candidate.component_id,)
        ).fetchone()[0]
        field_type = json.loads(props).get("type")
        parent = (row["parent_id"] or "object:?")[len("object:"):]
        parts.append(f"{field_type or 'field'} on {parent}")
        values = [r[0] for r in bundle.db.execute(
            "SELECT value FROM picklist_value WHERE field_id=? ORDER BY value LIMIT 6",
            (candidate.component_id,))]
        if values:
            parts.append("values: " + ", ".join(values))
    else:
        parts.append(row["kind"].replace("_", " "))

    if candidate.implied_filter:
        parts.append(f"filter: {candidate.implied_filter}")
    if row["description"]:
        parts.append(str(row["description"])[:120])

    return headline, " · ".join(parts)


def clarify(bundle: Bundle, resolution: Resolution) -> Question:
    """Render one ambiguous resolution as an answerable question."""
    if resolution.status != "ambiguous":
        raise ClarifyError(
            f"{resolution.surface!r} is {resolution.status}, not ambiguous; nothing to ask")

    candidates = list(resolution.candidates)
    if any(c.tier == "curated" for c in candidates):
        candidates = [c for c in candidates
                      if c.tier == "curated" or c.score >= CURATED_OPTION_FLOOR]

    options: list[Option] = []
    for letter, candidate in zip(_LETTERS, candidates[:MAX_OPTIONS]):
        headline, detail = _describe_option(bundle, candidate)
        options.append(Option(
            id=letter,
            component_id=candidate.component_id,
            headline=headline,
            detail=detail,
            score=candidate.score,
            tier=candidate.tier,
            implied_filter=candidate.implied_filter,
            note=candidate.note,
        ))

    recommended = None
    if len(options) > 1 and options[0].score - options[1].score >= RECOMMEND_MARGIN:
        recommended = options[0].id
    # A term the lexicon holds under review has no recommendation by
    # construction: the whole reason it is held is that nobody has decided.
    if any(o.tier == "curated" for o in options) and resolution.question:
        recommended = None

    text = resolution.question or (
        f'"{resolution.surface}" could mean more than one thing. Which did you mean?')
    if len(options) == 1:
        # Not a choice but an open question: the lexicon has a target and is
        # missing the predicate that makes it usable. Say so plainly rather
        # than offering a single-item menu.
        text = (f'"{resolution.surface}" needs a decision before it can be used. '
                f"{resolution.question or ''}").strip()

    return Question(
        surface=resolution.surface,
        text=text,
        options=options,
        recommended=recommended,
    )


def questions_for(bundle: Bundle, discovery: Any) -> list[Question]:
    """Every question a discovery raised, in the order it raised them."""
    return [clarify(bundle, r) for r in discovery.needs_clarification]


def apply(bundle: Bundle, question: Question, choice_ids: Sequence[str]) -> list[Candidate]:
    """Turn the user's picks back into resolved candidates."""
    by_id = {o.id: o for o in question.options}
    unknown = [c for c in choice_ids if c not in by_id]
    if unknown:
        raise ClarifyError(
            f"{question.surface!r}: no option {', '.join(unknown)}; "
            f"expected one of {', '.join(sorted(by_id))}")
    out: list[Candidate] = []
    for choice in choice_ids:
        option = by_id[choice]
        row = bundle.db.execute(
            "SELECT kind, api_name, label FROM component WHERE id=?",
            (option.component_id,)).fetchone()
        out.append(Candidate(
            component_id=option.component_id,
            kind=row["kind"] if row else "unknown",
            api_name=row["api_name"] if row else "",
            label=row["label"] if row else None,
            score=1.0,
            tier="user_choice",
            matched=question.surface,
            implied_filter=option.implied_filter,
            source=f"user answered {question.surface!r}",
        ))
    return out


def propose_curated(question: Question, choice_id: str, *,
                    answered_by: str | None = None) -> str:
    """The curated.yaml entry this answer implies, for a human to review.

    Deliberately not written to the file. One person answering one question in
    one conversation is weaker evidence than a Salesforce owner signing off a
    mapping that will steer every future query, and silently promoting the
    former to the latter is how a plausible guess becomes permanent.
    """
    by_id = {o.id: o for o in question.options}
    if choice_id not in by_id:
        raise ClarifyError(f"no option {choice_id!r} on this question")
    option = by_id[choice_id]
    lines = [
        f"  {question.surface}:",
        f"    target: {option.component_id}",
    ]
    if option.implied_filter:
        lines.append(f"    filter: {option.implied_filter!r}")
    lines.append("    confidence: high")
    source = answered_by or "answered in conversation; confirm with a Salesforce owner"
    lines.append(f"    source: {source}")
    lines.append("    # REVIEW BEFORE MERGING: a chat answer is not a sign-off.")
    return "\n".join(lines)
