"""Verified facts, derived values, and the set of things an answer may say.

Everything arithmetic happens here. The model is never asked to subtract two
group counts or work out a percentage -- not because it cannot, but because a
wrong number in a business answer is indistinguishable from a right one until
someone checks, and nobody checks.

Extraction also builds the supported sets the validator later enforces. Those
are produced here rather than in the validator so that one pass over the result
defines both what the model is told and what it is allowed to repeat.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from . import display
from .models import Freshness, InterpretedResult, ResultType

# Aliases the planner gives a bare COUNT(*).
COUNT_ALIASES = ("record_count", "count", "total")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    return None


def _freshness(query_result: Any) -> Freshness:
    raw = _get(query_result, "freshness")
    if raw is None:
        return Freshness()
    return Freshness(status=str(_get(raw, "status", "unknown")),
                     age_minutes=_get(raw, "age_minutes"),
                     last_successful_sync=_get(raw, "last_sync_at"))


def _group_columns(query_plan: Any) -> list[str]:
    return [str(_get(item, "output_alias", "")) for item in
            (_get(query_plan, "group_by", []) or [])]


def _aggregate_columns(query_plan: Any) -> list[str]:
    return [str(_get(item, "output_alias", "")) for item in
            (_get(query_plan, "select", []) or []) if _get(item, "aggregate")]


def extract_facts(query_result: Any, result_type: ResultType, *,
                  question: str = "", grounded_plan: Any = None,
                  query_plan: Any = None) -> InterpretedResult:
    rows = list(_get(query_result, "rows", []) or [])
    interpreted = InterpretedResult(
        result_type=result_type,
        user_question=question,
        primary_entity=_primary_entity(grounded_plan),
        returned_row_count=int(_get(query_result, "row_count", 0) or 0),
        total_count=_get(query_result, "total_count"),
        truncated=bool(_get(query_result, "truncated", False)),
        freshness=_freshness(query_result))

    interpreted.applied_filters = _applied_filters(grounded_plan)
    group_columns = _group_columns(query_plan)
    aggregate_columns = _aggregate_columns(query_plan)

    if result_type is ResultType.COUNT:
        interpreted.aggregates = {"matching_record_count":
                                  _count_value(rows, aggregate_columns)}
        interpreted.total_count = interpreted.aggregates["matching_record_count"]
    elif result_type is ResultType.SINGLE_AGGREGATE:
        interpreted.aggregates = _aggregates(rows, aggregate_columns)
    elif result_type in (ResultType.GROUPED_AGGREGATE, ResultType.COMPARISON):
        interpreted.groups = _groups(rows, group_columns, aggregate_columns)
        interpreted.derived_facts = derive_group_facts(
            interpreted.groups, group_columns, aggregate_columns)
    else:
        interpreted.records = [display.normalise_row(row) for row in rows]

    _collect_supported(interpreted)
    return interpreted


# Operators as a person would say them, so a restated filter reads as English.
_OPERATOR_WORDS = {
    "equals": "is", "not_equals": "is not", "greater_than": "is more than",
    "greater_than_or_equal": "is at least", "less_than": "is less than",
    "less_than_or_equal": "is at most", "contains": "contains",
    "not_contains": "does not contain", "starts_with": "starts with",
    "ends_with": "ends with", "in": "is one of", "not_in": "is not one of",
    "is_null": "is empty", "is_not_null": "is not empty",
    "between": "is between", "before": "is before", "after": "is after",
    "on_or_before": "is on or before", "on_or_after": "is on or after",
}


def _applied_filters(grounded_plan: Any) -> list[dict[str, Any]]:
    """The filters the executed query carried, in business words.

    Established by the plan, so they are evidence. The question is not: what a
    user asked for does not make it true, but what the query filtered on is a
    fact about the query that ran.
    """
    out: list[dict[str, Any]] = []
    for mapping in _get(grounded_plan, "filter_mappings", []) or []:
        item = (mapping if isinstance(mapping, dict)
                else dict(getattr(mapping, "__dict__", {})))
        field = item.get("field")
        if not field:
            continue
        operator = str(item.get("operator", "equals")).lower()
        out.append({
            "attribute": str(item.get("business_concept")
                             or display.label_for(field)),
            "condition": _OPERATOR_WORDS.get(operator, operator),
            "value": display.normalise(item.get("value")),
        })
    return out


def _primary_entity(grounded_plan: Any) -> str | None:
    name = _get(grounded_plan, "primary_object")
    return display.label_for(name) if name else None


def _count_value(rows: list[dict[str, Any]],
                 aggregate_columns: list[str]) -> int:
    """The single number a COUNT produced.

    Zero when the statement returned no row at all: a COUNT that matched
    nothing is the answer "none", not a missing answer.
    """
    if not rows:
        return 0
    row = rows[0]
    for key in [*aggregate_columns, *COUNT_ALIASES]:
        if key in row:
            return int(_number(row[key]) or 0)
    first = next(iter(row.values()), 0)
    return int(_number(first) or 0)


def _aggregates(rows: list[dict[str, Any]],
                aggregate_columns: list[str]) -> dict[str, Any]:
    if not rows:
        return {}
    row = rows[0]
    keys = aggregate_columns or list(row)
    return {display.label_for(key): display.normalise(row.get(key))
            for key in keys if key in row}


def _groups(rows: list[dict[str, Any]], group_columns: list[str],
            aggregate_columns: list[str]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for row in rows:
        entry: dict[str, Any] = {}
        for column in group_columns or [k for k in row
                                        if k not in aggregate_columns]:
            entry[display.label_for(column)] = display.normalise(row.get(column))
        for column in aggregate_columns or []:
            if column in row:
                entry[display.label_for(column)] = display.normalise(row[column])
        groups.append(entry)
    return groups


def derive_group_facts(groups: list[dict[str, Any]], group_columns: list[str],
                       aggregate_columns: list[str]) -> dict[str, Any]:
    """Sum, extremes, the gap between the top two, and each group's share.

    Computed over one measure only. With two measures there is no single
    "difference" a sentence could mean, and inventing one would be worse than
    leaving the model with the group values alone.
    """
    if not groups:
        return {}
    measures = [display.label_for(c) for c in aggregate_columns]
    if len(measures) != 1:
        return {}
    measure = measures[0]
    labels = [display.label_for(c) for c in group_columns]
    label = labels[0] if labels else None

    values: list[tuple[str, float]] = []
    for group in groups:
        number = _number(group.get(measure))
        if number is None:
            return {}
        name = display.display_of(group.get(label)) if label else ""
        values.append((name, number))

    total = sum(v for _, v in values)
    ordered = sorted(values, key=lambda pair: pair[1], reverse=True)
    facts: dict[str, Any] = {
        "measure": measure,
        "total": _round(total),
        "group_count": len(values),
        "largest": {"group": ordered[0][0], "value": _round(ordered[0][1])},
        "smallest": {"group": ordered[-1][0], "value": _round(ordered[-1][1])},
    }
    if len(ordered) >= 2:
        facts["difference"] = _round(ordered[0][1] - ordered[1][1])
    if total:
        facts["shares"] = [{"group": name,
                            "percentage": _round(value * 100.0 / total, 1)}
                           for name, value in values]
    return facts


def _round(value: float, places: int = 2) -> float | int:
    rounded = round(float(value), places)
    return int(rounded) if float(rounded).is_integer() else rounded


def _collect_supported(interpreted: InterpretedResult) -> None:
    """Every number and every identifier an answer is allowed to contain.

    Row counts go in because a summary states them. Freshness age goes in
    because a freshness note states it. Anything else the model writes and this
    set does not hold is, by construction, unsupported.
    """
    numbers: set[float] = {float(interpreted.returned_row_count)}
    strings: set[str] = set()

    if interpreted.total_count is not None:
        numbers.add(float(interpreted.total_count))
    if interpreted.freshness.age_minutes is not None:
        numbers.add(float(interpreted.freshness.age_minutes))
        # A note may reasonably round "17.4 minutes" to "17".
        numbers.add(float(round(interpreted.freshness.age_minutes)))
    if interpreted.freshness.status:
        strings.add(interpreted.freshness.status)
    # The business name of what was queried. Every summary says it -- "8
    # matching internal interviews" -- and without it a correct answer fails
    # for naming the thing the question was about.
    if interpreted.primary_entity:
        strings.add(interpreted.primary_entity)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "available":
                    continue
                walk(item)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
            return
        number = _number(value)
        if number is not None:
            numbers.add(number)
            return
        if isinstance(value, (date, datetime)):
            strings.add(value.isoformat())
            # "the week of September 14, 2026" restates a returned date.
            numbers.update({float(value.year), float(value.month), float(value.day)})
            return
        if isinstance(value, str) and value.strip():
            strings.add(value.strip())
            # A returned date (kept as ISO text once normalised) may be
            # restated in words: "the week of September 14, 2026".
            import re as _re
            stamp = _re.match(r"(\d{4})-(\d{2})-(\d{2})", value.strip())
            if stamp:
                numbers.update(float(part) for part in stamp.groups())

    # The filter values the query ran with. An answer must be able to say what
    # it searched for, especially when the search found nothing.
    walk(interpreted.applied_filters)
    walk(interpreted.records)
    walk(interpreted.groups)
    walk(interpreted.aggregates)
    walk(interpreted.derived_facts)

    # "a loss of 1,739" states -1739 without its sign. The magnitude of an
    # established figure is itself established.
    numbers |= {abs(n) for n in numbers}
    interpreted.supported_numbers = numbers
    interpreted.supported_strings = {s.lower() for s in strings}
