"""The answer of last resort, built from verified facts alone.

Reached only when the main model has failed grounding twice, or could not be
called at all. It is not a fast path and not an optimisation -- every normal
answer goes through the model. This exists so that a model which cannot stay
grounded produces a plainer answer rather than an invented one.
"""
from __future__ import annotations

from . import display
from .models import AnswerDetail, GroundedAnswer, InterpretedResult, ResultType


def _entity(interpreted: InterpretedResult) -> str:
    return interpreted.primary_entity or "records"


def _record_line(record: dict[str, object]) -> str:
    parts = [f"{label}: {display.display_of(value)}"
             for label, value in record.items()]
    return " — ".join(parts)


def build_fallback(interpreted: InterpretedResult) -> GroundedAnswer:
    entity = _entity(interpreted)
    result_type = interpreted.result_type

    if result_type is ResultType.COUNT:
        count = interpreted.aggregates.get("matching_record_count", 0)
        return GroundedAnswer(
            answer_type="count",
            summary=f"{count} matching {entity} were found.",
            freshness_note=_freshness_note(interpreted))

    if result_type is ResultType.EMPTY_RESULT:
        return GroundedAnswer(
            answer_type="empty",
            summary=f"No matching {entity} were found.",
            freshness_note=_freshness_note(interpreted))

    if result_type in (ResultType.GROUPED_AGGREGATE, ResultType.DUPLICATE_GROUPS,
                       ResultType.COMPARISON,
                       ResultType.RANKING, ResultType.TREND):
        return GroundedAnswer(
            answer_type="comparison" if result_type is ResultType.COMPARISON
            else "aggregate",
            summary=f"{len(interpreted.groups)} groups were returned.",
            details=[AnswerDetail(_record_line(group))
                     for group in interpreted.groups] + _fact_lines(interpreted),
            freshness_note=_freshness_note(interpreted))

    if result_type in (ResultType.PERCENTAGE, ResultType.EXISTS,
                       ResultType.SCHEMA_FACTS, ResultType.OPERATIONAL_FACTS,
                       ResultType.HISTORY_TIMELINE, ResultType.UNSUPPORTED):
        # Facts computed by code: state them as they are, nothing more.
        summary = {ResultType.PERCENTAGE: "The requested figures were computed.",
                   ResultType.EXISTS: "The existence check was run.",
                   ResultType.UNSUPPORTED: "This question cannot be answered from the "
                                           "available sources."}.get(
            result_type, "The following facts were found.")
        return GroundedAnswer(
            answer_type="summary", summary=summary,
            details=_fact_lines(interpreted) + [AnswerDetail(_record_line(record))
                                                for record in interpreted.records],
            freshness_note=_freshness_note(interpreted))

    if result_type is ResultType.SINGLE_AGGREGATE:
        return GroundedAnswer(
            answer_type="aggregate",
            summary="The requested value was computed.",
            details=[AnswerDetail(f"{label}: {display.display_of(value)}")
                     for label, value in interpreted.aggregates.items()],
            freshness_note=_freshness_note(interpreted))

    # Record listings. The summary never asserts a total the result did not
    # establish, which is the same rule the validator holds the model to.
    if interpreted.truncated and interpreted.total_count is None:
        summary = (f"Showing {interpreted.returned_row_count} matching "
                   f"{entity}. Further records may exist.")
    elif interpreted.truncated:
        summary = (f"{interpreted.total_count} matching {entity} were found. "
                   f"Showing the first {interpreted.returned_row_count}.")
    else:
        summary = f"{interpreted.returned_row_count} matching {entity} were found."

    return GroundedAnswer(
        answer_type="record_list" if len(interpreted.records) != 1 else "summary",
        summary=summary,
        details=[AnswerDetail(_record_line(record))
                 for record in interpreted.records],
        freshness_note=_freshness_note(interpreted))


def _fact_lines(interpreted: InterpretedResult) -> list[AnswerDetail]:
    lines = [AnswerDetail(f"{label}: {display.display_of(value)}")
             for label, value in interpreted.aggregates.items()]
    for label, value in (interpreted.derived_facts or {}).items():
        if not isinstance(value, (dict, list)):
            lines.append(AnswerDetail(f"{label}: {display.display_of(value)}"))
    return lines


def _freshness_note(interpreted: InterpretedResult) -> str | None:
    age = interpreted.freshness.age_minutes
    if age is None:
        return None
    minutes = int(round(age))
    return f"Data was last synchronised {minutes} minutes ago."
