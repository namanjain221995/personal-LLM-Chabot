"""Structured trace events for the knowledge stages.

The orchestrator already records a stage timeline in PostgreSQL. The stages
this bundle introduces -- resolving a phrase to a component, ranking candidates,
asking a clarifying question -- record nothing today, which is why the
evaluator reports `entities` and `filters` as `not_evaluable`: there is no
structured output to compare against.

These events are EMITTED, not persisted. The bundle does not know whether it is
running inside the orchestrator process or behind an HTTP boundary, and it has
no database handle either way. It produces records in the shape
`query_trace_events` expects; the caller decides where they go. That keeps the
evaluation runner able to score a discovery directly, without booting the app.

Nothing here carries record data. Component API names, scores and ranks are
metadata about the org's schema, not about anybody's candidate or client.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field as dataclass_field
from typing import Any

# Matches the orchestrator's component_version convention so a trace assembled
# from both sources can still be told apart by origin.
COMPONENT_VERSION = "knowledge-bundle-v1"

# Stage names follow the existing SCREAMING_CASE convention in
# query_trace_events (REQUEST_RECEIVED, INTENT_CLASSIFIED, ...).
ENTITY_RESOLVED = "ENTITY_RESOLVED"
SCHEMA_DISCOVERED = "SCHEMA_DISCOVERED"
CLARIFICATION_REQUESTED = "CLARIFICATION_REQUESTED"
COMPONENT_DESCRIBED = "COMPONENT_DESCRIBED"

# A detail blob is a diagnostic, not an archive. Long values are truncated so
# one pathological query cannot bloat the table.
MAX_STRING = 500
MAX_LIST = 50


def _clip(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_STRING]
    if isinstance(value, list):
        return [_clip(v) for v in value[:MAX_LIST]]
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    return value


@dataclass
class TraceEvent:
    """One stage record, shaped for `query_trace_events`."""
    stage: str
    status: str = "success"
    component: str = ""
    details: dict[str, Any] = dataclass_field(default_factory=dict)
    duration_ms: int | None = None
    component_version: str = COMPONENT_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "component": self.component,
            "details": _clip(self.details),
            "duration_ms": self.duration_ms,
            "component_version": self.component_version,
        }


def entity_resolved(resolutions: list[Any], duration_ms: int) -> TraceEvent:
    """What each phrase in the question turned out to mean.

    This is the event the `entities` and `filters` evaluator checks need. It
    records the API names that were chosen AND the phrases that could not be
    resolved, because a silent miss and a confident wrong answer are different
    failures and the old METADATA_RETRIEVED event distinguished neither.
    """
    surfaces = []
    objects: list[str] = []
    fields: list[str] = []
    filters: list[dict[str, str]] = []
    unresolved: list[str] = []
    ambiguous: list[str] = []

    for resolution in resolutions:
        entry: dict[str, Any] = {
            "surface": resolution.surface,
            "status": resolution.status,
        }
        if resolution.chosen is not None:
            chosen = resolution.chosen
            entry["chosen"] = chosen.component_id
            entry["tier"] = chosen.tier
            entry["score"] = chosen.score
            if chosen.kind == "object":
                objects.append(chosen.api_name)
            elif chosen.kind == "field":
                fields.append(chosen.component_id.split(":", 1)[1])
            if chosen.implied_filter:
                entry["implied_filter"] = chosen.implied_filter
                filters.append({"surface": resolution.surface,
                                "expression": chosen.implied_filter})
        else:
            entry["candidates"] = [c.component_id for c in resolution.candidates[:5]]
        if resolution.status == "unresolved":
            unresolved.append(resolution.surface)
        elif resolution.status == "ambiguous":
            ambiguous.append(resolution.surface)
        surfaces.append(entry)

    return TraceEvent(
        stage=ENTITY_RESOLVED,
        status="success" if not unresolved else "info",
        component="graphrag.resolver.resolve",
        duration_ms=duration_ms,
        details={
            "surfaces": surfaces,
            "resolved_objects": sorted(set(objects)),
            "resolved_fields": sorted(set(fields)),
            "implied_filters": filters,
            "ambiguous_surfaces": ambiguous,
            "unresolved_surfaces": unresolved,
        },
    )


def schema_discovered(discovery: Any, *, duration_ms: int, semantic: bool,
                      reranked: bool) -> TraceEvent:
    """The ranked shortlist, with the rank of every candidate.

    Discovery is ranked retrieval, so a pass/fail check misreads it. Recording
    rank and score lets the evaluator ask recall@k and MRR instead -- a query
    that puts the right object second is not the same failure as one that never
    retrieves it at all.
    """
    def rows(items: list[Any]) -> list[dict[str, Any]]:
        return [{"component_id": c.component_id, "rank": i + 1,
                 "score": c.score, "tier": c.tier, "matched": c.matched}
                for i, c in enumerate(items)]

    # Flat API-name lists alongside the ranked rows. The evaluator's `entities`
    # check wants "which objects did this query identify" in one place;
    # ENTITY_RESOLVED answers only "what did each phrase mean", and an object
    # implied by a resolved field belongs to the first question, not the second.
    return TraceEvent(
        stage=SCHEMA_DISCOVERED,
        status="success" if (discovery.objects or discovery.fields
                             or discovery.other) else "info",
        component="graphrag.resolver.discover_schema",
        duration_ms=duration_ms,
        details={
            "signals": {"lexicon": True, "semantic": semantic,
                        "rerank": reranked},
            "object_api_names": [c.api_name for c in discovery.objects],
            "field_api_names": [c.component_id.split(":", 1)[1]
                                for c in discovery.fields],
            "objects": rows(discovery.objects),
            "fields": rows(discovery.fields),
            "other": rows(discovery.other),
            "clarifications_raised": [r.surface for r in discovery.needs_clarification],
        },
    )


def clarification_requested(question: Any) -> TraceEvent:
    """A question put to the user, and the options it offered.

    Recorded so an evaluation can judge the ASK itself. Asking about "placed"
    is correct behaviour -- the placement regression happened because the old
    system did not ask -- so a run that clarifies must be scorable as a success
    rather than counted as a failure to answer.
    """
    return TraceEvent(
        stage=CLARIFICATION_REQUESTED,
        status="info",
        component="graphrag.clarify.clarify",
        details={
            "surface": question.surface,
            "question": question.text,
            "recommended": question.recommended,
            "options": [{"id": o.id, "component_id": o.component_id,
                         "tier": o.tier, "score": o.score,
                         "implied_filter": o.implied_filter}
                        for o in question.options],
        },
    )


def component_described(component_id: str, payload: dict[str, Any],
                        duration_ms: int) -> TraceEvent:
    """Which component was described, and how much of it came back."""
    return TraceEvent(
        stage=COMPONENT_DESCRIBED,
        status="success" if payload.get("found") else "failed",
        component="graphrag.resolver.describe_object",
        duration_ms=duration_ms,
        details={
            "component_id": component_id,
            "found": bool(payload.get("found")),
            "kind": payload.get("kind"),
            "field_count": len(payload.get("fields") or []),
            "picklist_count": len(payload.get("picklist_values") or []),
            "record_types": payload.get("record_types") or [],
            "validation_rules": payload.get("validation_rules") or [],
        },
    )


def to_jsonl(events: list[TraceEvent]) -> str:
    """The events as one JSON object per line, for a file-based runner."""
    return "\n".join(json.dumps(e.as_dict(), sort_keys=True) for e in events)
