"""Typed results and the arithmetic that must never be the model's (§23, §44).

Every executor returns a TypedResult: rows AND the scalar facts derived from
them -- counts, the numerator and denominator of a percentage, the difference
between two comparison segments, a trend's first and last values. All of it
is computed here, by code. The answer model receives verified facts and only
explains them.

`to_interpreted` is the adapter to the existing answer layer, so the grounding
validator, the regeneration and the safety fallback apply unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any


@dataclass
class TypedResult:
    kind: str                       # see KIND_TO_TYPE
    source: str = "record_data"     # record_data | runtime_schema | operational | history
    rows: list[dict[str, Any]] = dataclass_field(default_factory=list)
    values: dict[str, Any] = dataclass_field(default_factory=dict)
    derived: dict[str, Any] = dataclass_field(default_factory=dict)
    returned_count: int = 0
    total_count: int | None = None
    truncated: bool = False
    freshness: Any = None
    subplans: list[dict[str, Any]] = dataclass_field(default_factory=list)
    entity_label: str | None = None
    error: str | None = None
    error_detail: str = ""

    @property
    def success(self) -> bool:
        return self.error is None

    @property
    def row_count(self) -> int:
        return self.returned_count

    @property
    def param_count(self) -> int | None:
        counts = [p.get("params") for p in self.subplans if p.get("params") is not None]
        return sum(counts) if counts else None

    @property
    def execution_ms(self) -> float:
        return sum(float(p.get("ms") or 0) for p in self.subplans)

    @property
    def sql(self) -> str:
        return "\n;\n".join(p["sql"] for p in self.subplans if p.get("sql"))

    def summary(self) -> dict[str, Any]:
        return {"kind": self.kind, "source": self.source,
                "returned_count": self.returned_count, "total_count": self.total_count,
                "truncated": self.truncated, "values": self.values,
                "derived": self.derived, "subplans": len(self.subplans),
                "error": self.error}


def _round(value: float, places: int = 2) -> float | int:
    value = round(float(value), places)
    return int(value) if value.is_integer() else value


def percentage(numerator: float, denominator: float) -> dict[str, Any]:
    """Deterministic, and honest about an empty denominator."""
    if not denominator:
        return {"numerator": _round(numerator), "denominator": 0, "percentage": None,
                "note": "the denominator is zero, so no percentage exists"}
    return {"numerator": _round(numerator), "denominator": _round(denominator),
            "percentage": _round(numerator * 100.0 / denominator)}


def ratio(numerator: float, denominator: float) -> dict[str, Any]:
    if not denominator:
        return {"numerator": _round(numerator), "denominator": 0, "ratio": None}
    return {"numerator": _round(numerator), "denominator": _round(denominator),
            "ratio": _round(numerator / denominator, 4)}


def compare(segments: list[tuple[str, float]]) -> dict[str, Any]:
    """Two or more segments: every figure the answer may state, computed."""
    out: dict[str, Any] = {"segments": [{"segment": label, "value": _round(v)}
                                        for label, v in segments]}
    if len(segments) >= 2:
        (first, a), (second, b) = segments[0], segments[1]
        out["difference"] = _round(a - b)
        out["difference_absolute"] = _round(abs(a - b))
        out["higher"] = first if a > b else (second if b > a else "equal")
        if b:
            out["change_percent"] = _round((a - b) * 100.0 / b)
    return out


def trend(points: list[tuple[str, float]]) -> dict[str, Any]:
    if not points:
        return {}
    values = [v for _, v in points]
    out = {"periods": len(points), "first": {"period": points[0][0], "value": _round(values[0])},
           "last": {"period": points[-1][0], "value": _round(values[-1])},
           "total": _round(sum(values)),
           "highest": {"period": points[values.index(max(values))][0], "value": _round(max(values))},
           "lowest": {"period": points[values.index(min(values))][0], "value": _round(min(values))}}
    out["change_first_to_last"] = _round(values[-1] - values[0])
    return out


KIND_TO_TYPE = {
    "records": "MULTI_RECORD", "single_record": "SINGLE_RECORD", "empty": "EMPTY_RESULT",
    "count": "COUNT", "aggregate": "SINGLE_AGGREGATE", "grouped": "GROUPED_AGGREGATE",
    "duplicate_groups": "DUPLICATE_GROUPS",
    "ranking": "RANKING", "trend": "TREND", "comparison": "COMPARISON",
    "percentage": "PERCENTAGE", "ratio": "PERCENTAGE", "exists": "EXISTS",
    "schema_facts": "SCHEMA_FACTS", "operational_facts": "OPERATIONAL_FACTS",
    "history_timeline": "HISTORY_TIMELINE", "unsupported": "UNSUPPORTED",
    "truncated": "TRUNCATED_RESULT",
}


def applied_filters(g: Any) -> list[dict[str, Any]]:
    """Every condition the executed queries carried, in business words.

    Facts about the query that ran -- so an answer may say "with outcome Offer
    Received" or "found no interviews for John Smith" -- never the question's
    wishes.
    """
    from answer import display
    from answer.facts import _OPERATOR_WORDS
    if g is None:
        return []
    conditions = list(g.filters) + list(g.numerator) + list(g.denominator)
    for segment in g.segments:
        conditions += segment.filters
    for x in g.existence:
        conditions += x.filters
    out = []
    for f in conditions:
        field = f.left.field or ""
        item = {"attribute": f.left.label or display.label_for(field),
                "condition": _OPERATOR_WORDS.get(f.operator, f.operator)}
        if f.kind == "field" and f.right is not None:
            item["value"] = f.right.label or display.label_for(f.right.field or "")
        elif f.kind != "null":
            item["value"] = display.normalise(f.value)
        out.append(item)
    return out


def to_interpreted(result: TypedResult, question: str, grounded: Any = None) -> Any:
    """TypedResult -> the answer layer's InterpretedResult, facts included."""
    from answer import display
    from answer.facts import _collect_supported
    from answer.models import Freshness, InterpretedResult, ResultType

    kind = result.kind
    if kind == "records" and result.truncated:
        kind = "truncated"
    elif kind == "records" and result.returned_count == 0:
        kind = "empty"
    elif kind == "records" and result.returned_count == 1:
        kind = "single_record"
    interpreted = InterpretedResult(
        result_type=ResultType(KIND_TO_TYPE.get(kind, "MULTI_RECORD")),
        user_question=question, primary_entity=result.entity_label,
        returned_row_count=result.returned_count, total_count=result.total_count,
        truncated=result.truncated)
    if result.source in ("runtime_schema", "operational", "capabilities"):
        interpreted.freshness = Freshness(status="not_applicable")
    elif result.freshness is not None:
        f = result.freshness
        interpreted.freshness = Freshness(status=getattr(f, "status", "unknown"),
                                          age_minutes=getattr(f, "age_minutes", None),
                                          last_successful_sync=getattr(f, "last_sync_at", None))
    rows = [display.normalise_row(r) for r in result.rows]
    if kind in ("grouped", "duplicate_groups", "ranking", "trend", "comparison"):
        interpreted.groups = rows
    else:
        interpreted.records = rows
    interpreted.aggregates = {k: display.normalise(v) for k, v in result.values.items()}
    interpreted.derived_facts = result.derived
    interpreted.applied_filters = applied_filters(grounded)
    _collect_supported(interpreted)
    # Column names of the returned rows are the schema's own labels
    # ("Candidate Status"): naming one is not inventing it.
    interpreted.supported_strings |= {str(k).lower() for r in result.rows for k in r}
    if result.entity_label:
        # "Background Check ID", "Interview Name": the record's own columns,
        # named after the record.
        base = result.entity_label.lower()
        for name in {base, base.rstrip("s")}:
            interpreted.supported_strings |= {f"{name} id", f"{name} name",
                                              f"{name} number"}
    if grounded is not None:
        # Every field grounding verified may be named ("the Estimated Margin
        # total"): it is the schema's own label for what was computed.
        refs = [m.ref for m in grounded.measures] + [d.ref for d in grounded.dimensions] + \
            [a[0] for a in grounded.attributes] + [f.left for f in grounded.filters]
        interpreted.supported_strings |= {r.label.lower() for r in refs
                                          if r is not None and r.label}
        # The periods the query ran over: "Q2 2026", "the last 8 weeks", and
        # their resolved bounds are facts about the query, like its filters.
        import re
        for period in grounded.periods:
            text = " ".join(str(period.get(k) or "") for k in
                            ("expression", "start", "end_exclusive"))
            interpreted.supported_numbers |= {float(n) for n in re.findall(r"\d+", text)}
            if period.get("expression"):
                interpreted.supported_strings.add(str(period["expression"]).lower())
    return interpreted
