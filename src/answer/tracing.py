"""Trace events for answer generation.

Same shape as every other stage's events, so one timeline holds the whole
question. The model and its role are recorded on every generation event: an
evaluation that cannot tell which model wrote an answer cannot attribute a
hallucination to one.

Grounding failures are traced as reason codes and the offending values -- the
number that was wrong, the name that was invented. Not the supported records,
which are already in the result and are somebody's personal data.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any

COMPONENT_VERSION = "answer-v1"

RESULT_INTERPRETATION_STARTED = "RESULT_INTERPRETATION_STARTED"
RESULT_CLASSIFIED = "RESULT_CLASSIFIED"
RESULT_FACTS_EXTRACTED = "RESULT_FACTS_EXTRACTED"
ANSWER_CONTEXT_CREATED = "ANSWER_CONTEXT_CREATED"
MAIN_ANSWER_MODEL_STARTED = "MAIN_ANSWER_MODEL_STARTED"
MAIN_ANSWER_MODEL_COMPLETED = "MAIN_ANSWER_MODEL_COMPLETED"
ANSWER_GROUNDING_VALIDATION_STARTED = "ANSWER_GROUNDING_VALIDATION_STARTED"
ANSWER_GROUNDING_VALIDATION_COMPLETED = "ANSWER_GROUNDING_VALIDATION_COMPLETED"
ANSWER_REGENERATION_STARTED = "ANSWER_REGENERATION_STARTED"
ANSWER_REGENERATION_COMPLETED = "ANSWER_REGENERATION_COMPLETED"
DETERMINISTIC_SAFETY_FALLBACK_USED = "DETERMINISTIC_SAFETY_FALLBACK_USED"
FINAL_RESPONSE_CREATED = "FINAL_RESPONSE_CREATED"


@dataclass
class TraceEvent:
    stage: str
    status: str = "success"
    component: str = ""
    details: dict[str, Any] = dataclass_field(default_factory=dict)
    duration_ms: int | None = None
    component_version: str = COMPONENT_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "status": self.status,
                "component": self.component, "details": self.details,
                "duration_ms": self.duration_ms,
                "component_version": self.component_version}


def interpretation_started(question: str) -> TraceEvent:
    return TraceEvent(stage=RESULT_INTERPRETATION_STARTED,
                      component="answer.service.answer",
                      details={"question_characters": len(question or "")})


def result_classified(result_type: Any) -> TraceEvent:
    return TraceEvent(stage=RESULT_CLASSIFIED,
                      component="answer.classifier.classify_result",
                      details={"result_type": getattr(result_type, "value",
                                                      str(result_type))})


def facts_extracted(interpreted: Any, duration_ms: int) -> TraceEvent:
    return TraceEvent(
        stage=RESULT_FACTS_EXTRACTED, component="answer.facts.extract_facts",
        duration_ms=duration_ms,
        details={"returned_row_count": interpreted.returned_row_count,
                 "total_count": interpreted.total_count,
                 "truncated": interpreted.truncated,
                 "groups": len(interpreted.groups),
                 "aggregates": sorted(interpreted.aggregates),
                 "derived_facts": sorted(interpreted.derived_facts),
                 "supported_numbers": len(interpreted.supported_numbers),
                 "supported_strings": len(interpreted.supported_strings)})


def context_created(context: Any) -> TraceEvent:
    return TraceEvent(stage=ANSWER_CONTEXT_CREATED,
                      component="answer.context.build_context",
                      details=context.metrics())


def model_started(model: str, role: str, attempt: int) -> TraceEvent:
    return TraceEvent(stage=MAIN_ANSWER_MODEL_STARTED,
                      component="answer.client.MainModelClient",
                      details={"model": model, "model_role": role,
                               "attempt": attempt})


def model_completed(meta: dict[str, Any], role: str, *, ok: bool,
                    detail: str = "") -> TraceEvent:
    return TraceEvent(
        stage=MAIN_ANSWER_MODEL_COMPLETED,
        status="success" if ok else "failed",
        component="answer.client.MainModelClient",
        duration_ms=int(meta.get("duration_ms") or 0),
        details={"model": meta.get("model"), "model_role": role,
                 "completion_tokens": meta.get("completion_tokens"),
                 "finish_reason": meta.get("finish_reason"),
                 "ok": ok, "detail": detail})


def validation_started(attempt: int) -> TraceEvent:
    return TraceEvent(stage=ANSWER_GROUNDING_VALIDATION_STARTED,
                      component="answer.validator.validate_grounded_answer",
                      details={"attempt": attempt})


def validation_completed(report: Any, duration_ms: int) -> TraceEvent:
    return TraceEvent(
        stage=ANSWER_GROUNDING_VALIDATION_COMPLETED,
        status="success" if report.ok else "failed",
        component="answer.validator.validate_grounded_answer",
        duration_ms=duration_ms,
        details={"ok": report.ok, "codes": report.codes(),
                 "violations": [v.as_dict() for v in report.violations]})


def regeneration_started(report: Any) -> TraceEvent:
    return TraceEvent(stage=ANSWER_REGENERATION_STARTED,
                      component="answer.service.answer",
                      details={"codes": report.codes()})


def regeneration_completed(*, ok: bool, detail: str = "") -> TraceEvent:
    return TraceEvent(stage=ANSWER_REGENERATION_COMPLETED,
                      status="success" if ok else "failed",
                      component="answer.service.answer",
                      details={"ok": ok, "detail": detail})


def fallback_used(reason: str, codes: list[str]) -> TraceEvent:
    """Logged as failed. The user got an answer; the model did not produce it."""
    return TraceEvent(stage=DETERMINISTIC_SAFETY_FALLBACK_USED, status="failed",
                      component="answer.fallback.build_fallback",
                      details={"reason": reason, "codes": codes})


def final_response(final: Any) -> TraceEvent:
    return TraceEvent(
        stage=FINAL_RESPONSE_CREATED,
        status="success" if final.grounded else "failed",
        component="answer.service.answer",
        duration_ms=final.duration_ms,
        details={"result_type": final.result_type,
                 "model": final.model, "model_role": final.model_role,
                 "attempts": final.attempts,
                 "regenerated": final.regenerated,
                 "fallback_used": final.fallback_used,
                 "grounded": final.grounded,
                 "characters": len(final.text)})
