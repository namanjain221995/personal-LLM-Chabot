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
INTENT_EXTRACTED = "INTENT_EXTRACTED"
ROUTE_DECIDED = "ROUTE_DECIDED"
SCHEMA_SEARCHED = "SCHEMA_SEARCHED"
SCHEMA_RETRIEVED = "SCHEMA_RETRIEVED"
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


def intent_extracted(extraction: Any, question: str) -> TraceEvent:
    """What the model read the question as, before anything was grounded.

    Recorded separately from ENTITY_RESOLVED so the two are independently
    scorable: a wrong answer caused by misreading the question is a different
    failure from one caused by mapping a correctly-read concept to the wrong
    component, and the old single METADATA_RETRIEVED event could tell neither.
    """
    if extraction is None:
        return TraceEvent(
            stage=INTENT_EXTRACTED, status="skipped",
            component="graphrag.extract.extract",
            details={"reason": "no extraction; fell back to the lexical path"})
    return TraceEvent(
        stage=INTENT_EXTRACTED, status="success",
        component="graphrag.extract.extract",
        duration_ms=extraction.duration_ms,
        details={
            "request_type": extraction.request_type,
            "intent": extraction.intent,
            "action": extraction.action,
            "business_entities": extraction.business_entities,
            "filters": extraction.filters,
            "requested_attributes": extraction.requested_attributes,
            "metadata_types": extraction.metadata_types,
            "decoding_mode": extraction.mode,
            "completion_tokens": extraction.completion_tokens,
        })


def route_decided(route: Any, extraction: Any) -> TraceEvent:
    """Which of the eight paths the question took, and the flags that chose it.

    The three booleans travel with the route because the route alone cannot be
    argued with: NONE could mean the question needed nothing, or that the model
    failed to set a flag. Recording both makes a wrong route traceable to
    whichever stage actually produced it.
    """
    flags = {
        "requires_schema_discovery": getattr(extraction, "requires_schema_discovery", None),
        "requires_record_query": getattr(extraction, "requires_record_query", None),
        "requires_metadata_context": getattr(extraction, "requires_metadata_context", None),
    } if extraction is not None else {}
    return TraceEvent(
        stage=ROUTE_DECIDED, status="success",
        component="graphrag.routing.derive_route",
        details={"route": getattr(route, "value", str(route)), "flags": flags})


def schema_searched(query: str, objects: list[Any], fields: list[Any],
                    duration_ms: int) -> TraceEvent:
    """Candidates the runtime schema catalog returned, with how each matched.

    `matched_by` is the useful column: an exact API-name hit and an FTS guess
    are both "found", and only one of them is evidence.
    """
    def rows(items: list[Any], key: str) -> list[dict[str, Any]]:
        out = []
        for index, item in enumerate(items):
            name = item.get(key, "")
            if key == "api_name" and item.get("object_api_name"):
                name = f"{item['object_api_name']}.{name}"
            out.append({"name": name, "rank": index + 1,
                        "score": item.get("score"),
                        "matched_by": item.get("matched_by")})
        return out

    return TraceEvent(
        stage=SCHEMA_SEARCHED,
        status="success" if (objects or fields) else "info",
        component="salesforce.runtime_schema.service.search",
        duration_ms=duration_ms,
        details={"query": query,
                 "objects": rows(objects, "api_name"),
                 "fields": rows(fields, "api_name")})


def schema_retrieved(object_api_name: str, detail: Any,
                     duration_ms: int) -> TraceEvent:
    """Detailed schema for one object, and which cache level served it.

    `served_from` says whether the answer came from memory or from SQLite,
    which is the only way to tell a hot-cache miss from a slow query after
    the fact.
    """
    if detail is None:
        return TraceEvent(
            stage=SCHEMA_RETRIEVED, status="failed",
            component="salesforce.runtime_schema.service.get_object_schema",
            duration_ms=duration_ms,
            details={"object": object_api_name,
                     "reason": "no such object in the runtime schema"})
    return TraceEvent(
        stage=SCHEMA_RETRIEVED, status="success",
        component="salesforce.runtime_schema.service.get_object_schema",
        duration_ms=duration_ms,
        details={
            "object": object_api_name,
            "served_from": detail.get("served_from"),
            "field_count": len(detail.get("fields") or []),
            "relationship_count": len(detail.get("relationships") or []),
            "child_relationship_count": len(detail.get("child_relationships") or []),
            "picklist_field_count": len(detail.get("picklists") or {}),
            "record_type_count": len(detail.get("record_types") or []),
            # The mirror cannot supply these; a planner reading the trace should
            # know the gap is the source's, not a retrieval failure.
            "source": (detail.get("object") or {}).get("source"),
        })


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
