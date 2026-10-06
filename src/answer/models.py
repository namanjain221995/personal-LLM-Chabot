"""Records the answer layer passes between its stages.

Three structures, deliberately separate. `InterpretedResult` is everything
deterministic code established. `AnswerContext` is the compact subset the model
is shown. `GroundedAnswer` is what the model returned. Keeping them apart is
what makes grounding checkable: the validator compares the third against the
first, and a fact that never reached the second cannot be in either.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from typing import Any


class ResultType(str, Enum):
    EMPTY_RESULT = "EMPTY_RESULT"
    SINGLE_RECORD = "SINGLE_RECORD"
    MULTI_RECORD = "MULTI_RECORD"
    COUNT = "COUNT"
    SINGLE_AGGREGATE = "SINGLE_AGGREGATE"
    GROUPED_AGGREGATE = "GROUPED_AGGREGATE"
    DUPLICATE_GROUPS = "DUPLICATE_GROUPS"
    COMPARISON = "COMPARISON"
    TRUNCATED_RESULT = "TRUNCATED_RESULT"
    ERROR_RESULT = "ERROR_RESULT"
    # Typed results from the analytical path (step 8).
    PERCENTAGE = "PERCENTAGE"
    RANKING = "RANKING"
    TREND = "TREND"
    EXISTS = "EXISTS"
    SCHEMA_FACTS = "SCHEMA_FACTS"
    OPERATIONAL_FACTS = "OPERATIONAL_FACTS"
    HISTORY_TIMELINE = "HISTORY_TIMELINE"
    UNSUPPORTED = "UNSUPPORTED"


class AnswerFailure(str, Enum):
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_RETURNED_NOTHING = "MODEL_RETURNED_NOTHING"
    MODEL_OUTPUT_MALFORMED = "MODEL_OUTPUT_MALFORMED"
    GROUNDING_REJECTED = "GROUNDING_REJECTED"


class GroundingCode(str, Enum):
    """Why an answer was refused. One code per kind of unsupported claim."""

    UNSUPPORTED_NUMBER = "UNSUPPORTED_NUMBER"
    UNSUPPORTED_RECORD = "UNSUPPORTED_RECORD"
    UNSUPPORTED_EMAIL = "UNSUPPORTED_EMAIL"
    UNSUPPORTED_DATE = "UNSUPPORTED_DATE"
    INVENTED_RECORD_COUNT = "INVENTED_RECORD_COUNT"
    TRUNCATION_MISSTATED = "TRUNCATION_MISSTATED"
    FRESHNESS_MISSTATED = "FRESHNESS_MISSTATED"
    LIVE_DATA_CLAIMED = "LIVE_DATA_CLAIMED"
    EMPTY_RESULT_EXPLAINED = "EMPTY_RESULT_EXPLAINED"


@dataclass
class Freshness:
    """The answer layer's own view of sync state, decoupled from DuckDB's."""

    status: str = "unknown"
    age_minutes: float | None = None
    last_successful_sync: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class InterpretedResult:
    """Everything deterministic code established, before any model runs."""

    result_type: ResultType = ResultType.EMPTY_RESULT
    user_question: str = ""
    primary_entity: str | None = None

    returned_row_count: int = 0
    # None means "not established". Never a guess, and never silently equal to
    # returned_row_count when rows were cut.
    total_count: int | None = None
    truncated: bool = False

    # What the query actually filtered on. Established by the plan that ran,
    # not by the question -- which is why it is evidence and the question is
    # not. Without it an empty-result answer cannot name what was searched
    # for, and "no records matched Round 99" fails for saying 99.
    applied_filters: list[dict[str, Any]] = dataclass_field(default_factory=list)
    records: list[dict[str, Any]] = dataclass_field(default_factory=list)
    groups: list[dict[str, Any]] = dataclass_field(default_factory=list)
    aggregates: dict[str, Any] = dataclass_field(default_factory=dict)
    derived_facts: dict[str, Any] = dataclass_field(default_factory=dict)

    freshness: Freshness = dataclass_field(default_factory=Freshness)
    # Every value a grounded answer is allowed to contain, flattened once so
    # the validator never has to walk the structure again.
    supported_numbers: set[float] = dataclass_field(default_factory=set)
    supported_strings: set[str] = dataclass_field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        return {
            "result_type": self.result_type.value,
            "user_question": self.user_question,
            "primary_entity": self.primary_entity,
            "returned_row_count": self.returned_row_count,
            "total_count": self.total_count,
            "truncated": self.truncated,
            "applied_filters": self.applied_filters,
            "records": self.records,
            "groups": self.groups,
            "aggregates": self.aggregates,
            "derived_facts": self.derived_facts,
            "freshness": self.freshness.as_dict(),
        }


@dataclass
class AnswerContext:
    """The compact structure the main model is shown, and nothing more."""

    payload: dict[str, Any] = dataclass_field(default_factory=dict)
    rows_sent_to_model: int = 0
    database_returned_rows: int = 0
    model_context_truncated: bool = False
    database_result_truncated: bool = False
    characters: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.payload)

    def metrics(self) -> dict[str, Any]:
        return {"rows_sent_to_model": self.rows_sent_to_model,
                "database_returned_rows": self.database_returned_rows,
                "model_context_truncated": self.model_context_truncated,
                "database_result_truncated": self.database_result_truncated,
                "characters": self.characters}


@dataclass
class AnswerDetail:
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text}


@dataclass
class GroundedAnswer:
    answer_type: str = ""
    summary: str = ""
    details: list[AnswerDetail] = dataclass_field(default_factory=list)
    notes: list[str] = dataclass_field(default_factory=list)
    freshness_note: str | None = None
    # Result interpretation (spec §31), written by the same call before the
    # answer: which facts answer the question and how sub-results relate.
    interpretation: dict[str, Any] | None = None

    def texts(self) -> list[str]:
        """Every piece of prose the model wrote, for the validator to read."""
        out = [self.summary, *(d.text for d in self.details), *self.notes]
        if self.freshness_note:
            out.append(self.freshness_note)
        return [t for t in out if t]

    def as_dict(self) -> dict[str, Any]:
        return {"answer_type": self.answer_type, "summary": self.summary,
                "details": [d.as_dict() for d in self.details],
                "notes": list(self.notes),
                "freshness_note": self.freshness_note,
                "interpretation": self.interpretation}


@dataclass
class GroundingViolation:
    code: GroundingCode
    detail: str
    value: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "detail": self.detail,
                "value": self.value}


@dataclass
class GroundingReport:
    ok: bool = True
    violations: list[GroundingViolation] = dataclass_field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok

    def add(self, code: GroundingCode, detail: str, value: Any = None) -> None:
        self.violations.append(GroundingViolation(code, detail, value))
        self.ok = False

    def codes(self) -> list[str]:
        return [v.code.value for v in self.violations]

    def as_dict(self) -> dict[str, Any]:
        """Reason codes and the offending values, never the whole answer.

        A violation names the number or the identifier that was not supported
        because that is what a fix needs. It does not copy the record that was
        supported -- a trace is not a second copy of the result.
        """
        return {"ok": self.ok,
                "violations": [v.as_dict() for v in self.violations]}


@dataclass
class FinalAnswer:
    text: str = ""
    answer: GroundedAnswer | None = None
    interpreted: InterpretedResult | None = None
    result_type: str = ""
    model: str = ""
    model_role: str = "main"
    attempts: int = 0
    regenerated: bool = False
    fallback_used: bool = False
    grounded: bool = False
    grounding: GroundingReport | None = None
    failure: AnswerFailure | None = None
    failure_detail: str = ""
    duration_ms: int = 0
    trace: list[Any] = dataclass_field(default_factory=list)
    # One entry per model call: duration, tokens, finish reason, ok.
    model_calls: list[dict[str, Any]] = dataclass_field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "text": self.text,
            "result_type": self.result_type,
            "model": self.model,
            "model_role": self.model_role,
            "attempts": self.attempts,
            "regenerated": self.regenerated,
            "fallback_used": self.fallback_used,
            "grounded": self.grounded,
            "duration_ms": self.duration_ms,
        }
        if self.answer:
            out["answer"] = self.answer.as_dict()
        if self.grounding:
            out["grounding"] = self.grounding.as_dict()
        if self.failure:
            out["failure"] = self.failure.value
            out["failure_detail"] = self.failure_detail
        return out
