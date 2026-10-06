"""What the pipeline is asked, and what it returns.

One result object carrying every stage's output, so a caller reads one thing
and an evaluation scores one thing. A stage that did not run is absent rather
than empty -- "no rows" and "never asked for rows" are different answers and
the difference is the whole point of routing.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from typing import Any


class PipelineError(str, Enum):
    EXTRACTION_UNAVAILABLE = "EXTRACTION_UNAVAILABLE"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    UNKNOWN_ROUTE = "UNKNOWN_ROUTE"
    STAGE_UNAVAILABLE = "STAGE_UNAVAILABLE"
    SCHEMA_LINKING_FAILED = "SCHEMA_LINKING_FAILED"
    RECORD_QUERY_FAILED = "RECORD_QUERY_FAILED"
    METADATA_FAILED = "METADATA_FAILED"
    ANSWER_FAILED = "ANSWER_FAILED"
    # A required model stage could not reach the main model (spec §52). Never
    # replaced by a deterministic decision or a weaker model.
    MAIN_MODEL_UNAVAILABLE = "MAIN_MODEL_UNAVAILABLE"
    PLANNING_FAILED = "PLANNING_FAILED"


@dataclass
class PipelineRequest:
    question: str
    test_case_id: str | None = None
    request_id: str | None = None
    limit: int | None = None
    # None means "read it off the extraction". An explicit value wins, so a
    # caller that knows better than the model can say so; the default used to
    # be the literal "retrieve", which silently answered every COUNT question
    # with a row listing because nothing could tell a default from a choice.
    operation: str | None = None
    group_by_fields: list[str] = dataclass_field(default_factory=list)
    aggregations: list[dict[str, str]] = dataclass_field(default_factory=list)
    # Supplied by a caller that already extracted, so the evaluation runner can
    # replay a fixed extraction and score routing alone.
    extraction: Any = None
    route: Any = None
    # Follow-up questions inherit from the previous turn with the same id.
    conversation_id: str | None = None


@dataclass
class PipelineResult:
    question: str = ""
    route: str | None = None
    stages: list[str] = dataclass_field(default_factory=list)
    stages_run: list[str] = dataclass_field(default_factory=list)

    extraction: Any = None
    discovery: Any = None
    grounded_plan: Any = None
    records: Any = None
    metadata: list[dict[str, Any]] = dataclass_field(default_factory=list)
    answer: Any = None

    success: bool = False
    error: PipelineError | None = None
    error_detail: str = ""
    duration_ms: int = 0
    trace: list[Any] = dataclass_field(default_factory=list)
    # One record per model stage (pipeline.stage_model.ModelCall), in order.
    model_stages: list[Any] = dataclass_field(default_factory=list)
    # The stages that applied to this question's route (model_roles.REQUIRED_MODEL_STAGES).
    applicable_stages: list[str] = dataclass_field(default_factory=list)

    def model_called(self, stage: str) -> bool:
        return any(c.stage == stage and c.model_called for c in self.model_stages)

    def fail(self, error: PipelineError, detail: str) -> "PipelineResult":
        self.success = False
        self.error = error
        self.error_detail = detail
        return self

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "question": self.question,
            "route": self.route,
            "stages": self.stages,
            "stages_run": self.stages_run,
            "success": self.success,
            "duration_ms": self.duration_ms,
        }
        if self.extraction is not None:
            out["extraction"] = _as_dict(self.extraction)
        if self.discovery is not None:
            out["discovery"] = _as_dict(self.discovery)
        if self.grounded_plan is not None:
            out["grounded_plan"] = _as_dict(self.grounded_plan)
        if self.records is not None:
            out["records"] = _as_dict(self.records)
        if self.metadata:
            out["metadata"] = self.metadata
        if self.answer is not None:
            out["answer"] = _as_dict(self.answer)
            out["text"] = getattr(self.answer, "text", "")
        if self.model_stages:
            out["model_stages"] = [c.as_dict() for c in self.model_stages]
            out["applicable_stages"] = self.applicable_stages
        if self.error:
            out["error"] = self.error.value
            out["error_detail"] = self.error_detail
        return out


def _as_dict(value: Any) -> Any:
    if hasattr(value, "as_dict"):
        return value.as_dict()
    if isinstance(value, dict):
        return value
    return {k: v for k, v in getattr(value, "__dict__", {}).items()
            if not k.startswith("_")}
