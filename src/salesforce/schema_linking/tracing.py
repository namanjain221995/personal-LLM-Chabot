"""Trace events for schema linking.

Emitted, never persisted here: this package has no database handle and does not
know whether it runs in-process or behind HTTP. The caller decides where the
records go. Shapes match `query_trace_events` so both pipelines land in one
timeline.

Each stage is separate on purpose. "Wrong object" and "right object, wrong
field" are different failures with different fixes, and one SCHEMA_LINKING
event could distinguish neither.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Sequence

COMPONENT_VERSION = "schema-linking-v1"

SCHEMA_LINKING_STARTED = "SCHEMA_LINKING_STARTED"
OBJECT_CANDIDATES_RETRIEVED = "OBJECT_CANDIDATES_RETRIEVED"
OBJECT_SELECTED = "OBJECT_SELECTED"
OBJECT_VERIFIED = "OBJECT_VERIFIED"
FIELD_CANDIDATES_RETRIEVED = "FIELD_CANDIDATES_RETRIEVED"
FIELD_SELECTED = "FIELD_SELECTED"
FIELD_VERIFIED = "FIELD_VERIFIED"
RELATIONSHIP_CANDIDATES_RETRIEVED = "RELATIONSHIP_CANDIDATES_RETRIEVED"
RELATIONSHIP_SELECTED = "RELATIONSHIP_SELECTED"
RELATIONSHIP_VERIFIED = "RELATIONSHIP_VERIFIED"
MODEL_ESCALATION = "MODEL_ESCALATION"
GROUNDED_SCHEMA_PLAN_CREATED = "GROUNDED_SCHEMA_PLAN_CREATED"
SCHEMA_LINKING_COMPLETED = "SCHEMA_LINKING_COMPLETED"
SCHEMA_LINKING_FAILED = "SCHEMA_LINKING_FAILED"
# Step 7. Recorded apart even though one model call can make both decisions:
# which object (a rerank of retrieval's candidates) and which fields (the
# linking proper). Merged, a trace could not say which half was needed.
SEMANTIC_RERANK = "SEMANTIC_RERANK"
SEMANTIC_LINKING = "SEMANTIC_LINKING"

MAX_CANDIDATES_LOGGED = 10


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


def started(intent: Any) -> TraceEvent:
    entities = (intent.get("business_entities") if isinstance(intent, dict)
                else getattr(intent, "business_entities", None)) or []
    return TraceEvent(
        stage=SCHEMA_LINKING_STARTED, component="schema_linking.linker.link",
        details={"route": (intent.get("route") if isinstance(intent, dict)
                           else getattr(intent, "route", None)),
                 "business_entities": [e.get("name") for e in entities],
                 "filter_concepts": [f.get("concept") for f in
                                     (intent.get("filters") if isinstance(intent, dict)
                                      else getattr(intent, "filters", [])) or []]})


def candidates_retrieved(stage: str, term: str, candidates: Sequence[Any],
                         duration_ms: int, *, scope: str | None = None) -> TraceEvent:
    """What retrieval offered, with the evidence for each.

    `evidence` is the useful column: an exact API-name hit and an FTS guess are
    both "retrieved", and only one is a fact. Recording it lets a later
    evaluation separate a retrieval miss from a reranking mistake.
    """
    rows = []
    for index, candidate in enumerate(candidates[:MAX_CANDIDATES_LOGGED]):
        rows.append({
            "name": getattr(candidate, "api_name", None)
                    or getattr(candidate, "source_field", ""),
            "rank": index + 1,
            "score": getattr(candidate, "retrieval_score", None),
            "evidence": list(getattr(candidate, "evidence", []))})
    details: dict[str, Any] = {"term": term, "count": len(candidates),
                               "candidates": rows}
    if scope:
        details["scope"] = scope
    return TraceEvent(stage=stage, status="success" if candidates else "info",
                      component="schema_linking.retriever", duration_ms=duration_ms,
                      details=details)


def selected(stage: str, term: str, selection: Any, *,
             accepted: bool, scope: str | None = None) -> TraceEvent:
    details: dict[str, Any] = {
        "term": term,
        "selected": getattr(selection, "selected", None),
        "confidence": getattr(selection, "confidence", None),
        "reason_codes": list(getattr(selection, "reason_codes", [])),
        "model": getattr(selection, "model", ""),
        "escalated": bool(getattr(selection, "escalated", False)),
        "accepted": accepted,
    }
    resolved = getattr(selection, "resolved_value", None)
    if resolved is not None:
        details["resolved_value"] = resolved
    if scope:
        details["scope"] = scope
    return TraceEvent(stage=stage, status="success" if accepted else "info",
                      component="schema_linking.reranker",
                      duration_ms=getattr(selection, "duration_ms", None),
                      details=details)


def escalated(term: str, primary: Any, fallback: Any) -> TraceEvent:
    """The primary tier was not confident enough. Both answers are recorded.

    Keeping both is what makes §30's fallback-accuracy measurable: whether
    escalation actually helps can only be judged by comparing what each tier
    said on the same candidates.
    """
    return TraceEvent(
        stage=MODEL_ESCALATION, status="info",
        component="schema_linking.confidence.resolve",
        details={"term": term,
                 "primary": {"model": getattr(primary, "model", ""),
                             "selected": getattr(primary, "selected", None),
                             "confidence": getattr(primary, "confidence", None)},
                 "fallback": {"model": getattr(fallback, "model", ""),
                              "selected": getattr(fallback, "selected", None),
                              "confidence": getattr(fallback, "confidence", None)}})


def verified(stage: str, name: str, result: Any, *, scope: str | None = None
             ) -> TraceEvent:
    details: dict[str, Any] = {"name": name, "ok": bool(result)}
    if not result:
        details["code"] = getattr(getattr(result, "code", None), "value", None)
        details["detail"] = getattr(result, "detail", "")
    if scope:
        details["scope"] = scope
    return TraceEvent(stage=stage, status="success" if result else "failed",
                      component="schema_linking.verifier", details=details)


def plan_created(plan: Any) -> TraceEvent:
    return TraceEvent(
        stage=GROUNDED_SCHEMA_PLAN_CREATED,
        status="success" if plan.schema_grounded else "info",
        component="schema_linking.linker.build_grounded_plan",
        duration_ms=plan.duration_ms,
        details={"primary_object": plan.primary_object,
                 "objects": plan.objects,
                 "entity_mappings": len(plan.entity_mappings),
                 "filter_mappings": len(plan.filter_mappings),
                 "attribute_mappings": len(plan.requested_attribute_mappings),
                 "schema_grounded": plan.schema_grounded,
                 "requires_fallback": plan.requires_fallback})


def completed(plan: Any) -> TraceEvent:
    stage = SCHEMA_LINKING_COMPLETED if plan.schema_grounded else SCHEMA_LINKING_FAILED
    return TraceEvent(
        stage=stage, status="success" if plan.schema_grounded else "failed",
        component="schema_linking.linker.link", duration_ms=plan.duration_ms,
        details={"schema_grounded": plan.schema_grounded,
                 "failure_codes": [f["code"] for f in plan.failures],
                 "failures": plan.failures[:5]})


def semantic_rerank(decision: Any, package: Any) -> TraceEvent:
    """Stage-3 view: did retrieval settle the object, or did the model?"""
    return TraceEvent(
        stage=SEMANTIC_RERANK,
        status="success" if decision.primary_object else "failed",
        component="schema_linking.semantic",
        duration_ms=decision.duration_ms if decision.invoked else 0,
        details={"reranker_invoked": decision.invoked,
                 "mode": decision.mode,
                 "model_role": "main" if decision.invoked else None,
                 "model": decision.model if decision.invoked else None,
                 "candidate_count": package.counts()["object_candidates"],
                 "anchored_objects": [c.api_name for c in package.anchored],
                 "selected": {k: v.selected for k, v in decision.objects.items()},
                 "primary_object": decision.primary_object,
                 "confidence": {k: v.confidence for k, v in decision.objects.items()}})


def semantic_linking(decision: Any, package: Any) -> TraceEvent:
    """Stage-4 view: which fields, chosen how, and anything the model named
    that was never offered (those are rejected, and listed here)."""
    return TraceEvent(
        stage=SEMANTIC_LINKING,
        status="success" if not decision.rejected else "info",
        component="schema_linking.semantic",
        duration_ms=decision.duration_ms if decision.invoked else 0,
        details={"linker_mode": "llm_assisted",
                 "decided_by": decision.mode,
                 "model_role": "main" if decision.invoked else None,
                 "model": decision.model if decision.invoked else None,
                 "field_candidate_count": package.counts()["field_candidates"],
                 "filters": {str(i): {"object": o, "field": s.selected,
                                      "value": s.resolved_value,
                                      "confidence": s.confidence}
                             for i, (o, s) in decision.filters.items()},
                 "attributes": {str(i): {"object": o, "field": s.selected,
                                         "confidence": s.confidence}
                                for i, (o, s) in decision.attributes.items()},
                 "date_field": (decision.date_field[1].selected
                                if decision.date_field else None),
                 "rejected": decision.rejected})
