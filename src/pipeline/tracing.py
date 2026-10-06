"""Pipeline-level trace events.

The stages emit their own events -- graphrag, schema_linking and record_query
each have a tracing module and each produces events in this same shape. These
add only what no single stage can see: which route was taken, which stages it
implied, and which of them actually ran.

That difference is the one thing worth tracing at this level. A stage that was
planned and did not run is the failure this layer exists to make visible.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any

COMPONENT_VERSION = "pipeline-v1"

ROUTE_DISPATCHED = "ROUTE_DISPATCHED"
STAGE_STARTED = "STAGE_STARTED"
STAGE_COMPLETED = "STAGE_COMPLETED"
STAGE_SKIPPED = "STAGE_SKIPPED"
PIPELINE_COMPLETED = "PIPELINE_COMPLETED"


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


def route_dispatched(route: Any, stages: Any) -> TraceEvent:
    return TraceEvent(
        stage=ROUTE_DISPATCHED, component="pipeline.dispatch.stages_for",
        details={"route": getattr(route, "value", str(route)),
                 "stages": [s.value for s in stages]})


def stage_started(stage: Any) -> TraceEvent:
    return TraceEvent(stage=STAGE_STARTED, component="pipeline.run",
                      details={"pipeline_stage": stage.value})


def stage_completed(stage: Any, duration_ms: int, *, ok: bool = True,
                    detail: str = "") -> TraceEvent:
    return TraceEvent(
        stage=STAGE_COMPLETED, status="success" if ok else "failed",
        component="pipeline.run", duration_ms=duration_ms,
        details={"pipeline_stage": stage.value, "ok": ok, "detail": detail})


def stage_skipped(stage: Any, reason: str) -> TraceEvent:
    """A stage the route asked for and the deployment could not provide.

    Logged as `failed`, not `info`. A missing port is a configuration gap that
    changed the answer, and an answer built from three of four stages must not
    read as a complete one.
    """
    return TraceEvent(
        stage=STAGE_SKIPPED, status="failed", component="pipeline.run",
        details={"pipeline_stage": stage.value, "reason": reason})


def pipeline_completed(result: Any) -> TraceEvent:
    return TraceEvent(
        stage=PIPELINE_COMPLETED,
        status="success" if result.success else "failed",
        component="pipeline.SalesforcePipeline.run",
        duration_ms=result.duration_ms,
        details={"route": result.route, "stages": result.stages,
                 "stages_run": result.stages_run,
                 "error": result.error.value if result.error else None,
                 "error_detail": result.error_detail})


# -- the semantic-IR path (step 8) -------------------------------------------
IR_COMPILED = "IR_COMPILED"
CAPABILITIES_PLANNED = "CAPABILITIES_PLANNED"
CONVERSATION_INHERITED = "CONVERSATION_INHERITED"
IR_GROUNDED = "IR_GROUNDED"
TEMPORAL_NORMALIZED = "TEMPORAL_NORMALIZED"
QUERY_DECOMPOSED = "QUERY_DECOMPOSED"
SUBPLAN_EXECUTED = "SUBPLAN_EXECUTED"
DERIVED_METRICS = "DERIVED_METRICS"
FACTS_FUSED = "FACTS_FUSED"
MODEL_STAGE = "MODEL_STAGE"
PLAN_CREATED = "PLAN_CREATED"
INTERPRETED = "RESULT_INTERPRETED"


def ir_event(stage: str, details: dict[str, Any], *, ok: bool = True,
             duration_ms: int | None = None) -> TraceEvent:
    """One event on the IR path. Details carry decisions, never rows or secrets."""
    return TraceEvent(stage=stage, status="success" if ok else "failed",
                      component="pipeline.ir_path", details=details,
                      duration_ms=duration_ms, component_version="pipeline-ir-v1")


def model_stage(call: Any) -> TraceEvent:
    """One model stage: proof the main model made this stage's decision (§44)."""
    details = call.as_dict()
    return TraceEvent(stage=MODEL_STAGE, status="success" if call.ok else "failed",
                      component=f"pipeline.stage.{call.stage}", details=details,
                      duration_ms=call.duration_ms, component_version="pipeline-ir-v2")
