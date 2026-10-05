"""Which handlers serve the sources the main model routed a question to (§8).

The routing DECISION is the model's: the compiler returns, with the intent,
the information sources the answer needs (`ir.sources`). This module only
finds the handler for each source and says which ones this deployment cannot
serve. It never adds a source the model did not name and never drops one it
did; the legacy route name is derived from the result so existing traces and
dashboards keep one vocabulary.

A capability this deployment cannot serve is named in `unsupported` with the
reason the answer should give -- it is never silently downgraded to a record
query that answers a different question.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field

from .ir import SemanticIR

# Honest refusals for what this read-only pipeline does not do.
_UNSUPPORTED = {
    "metadata_context": "automation and configuration metadata (flows, triggers, "
                        "validation rules) is not ingested into this pipeline yet",
    "text_analysis": "themes and sentiment across free-text fields are not analysed "
                     "by this pipeline",
    "prediction": "the pipeline reports what the data shows; it does not forecast",
    "business_definition": "no business definition for this term has been recorded",
}


@dataclass
class CapabilityPlan:
    capabilities: list[str] = dataclass_field(default_factory=list)
    sources: list[str] = dataclass_field(default_factory=list)
    route: str = "NONE"
    unsupported: dict[str, str] = dataclass_field(default_factory=dict)
    # Sources the model named that no handler serves here (traced, never hidden).
    unserved: list[str] = dataclass_field(default_factory=list)

    def as_dict(self) -> dict:
        return {"capabilities": self.capabilities, "sources": self.sources,
                "route": self.route, "unsupported": self.unsupported,
                "unserved": self.unserved}


def plan(ir: SemanticIR, *, metadata_available: bool = False,
         definitions: dict[str, str] | None = None,
         text_analysis_available: bool = False) -> CapabilityPlan:
    sources = list(ir.sources)
    capabilities: list[str] = []

    def add(*names: str) -> None:
        for name in names:
            if name not in capabilities:
                capabilities.append(name)

    if ir.family == "none":
        sources = []
    for source in sources:
        if source == "RECORD_DATA":
            if ir.family == "search":          # a value looked up across kinds of record
                add("record_search")
            else:
                add("schema_grounding", "record_query")
        elif source == "RUNTIME_SCHEMA":
            add("schema_query")
        elif source == "OPERATIONAL_CONTEXT":
            add("operational_context")
        elif source == "HISTORY_CONTEXT":
            add("schema_grounding", "history_query")
        elif source == "METADATA_CONTEXT":
            add("metadata_context")
        elif source == "BUSINESS_KNOWLEDGE":
            add("business_definition")
        elif source == "TEXT_ANALYSIS":
            add("schema_grounding", "text_analysis")
    if ir.family == "prediction":
        add("prediction")
    if sources == ["CONVERSATION_CONTEXT"]:
        add("conversation")
    if ir.comparison or ir.derived:
        add("derived_metrics")

    unsupported = {}
    for capability in capabilities:
        if capability == "metadata_context" and metadata_available:
            continue
        if capability == "business_definition" and definitions:
            continue
        if capability == "text_analysis" and text_analysis_available:
            continue
        if capability in _UNSUPPORTED:
            unsupported[capability] = _UNSUPPORTED[capability]

    if not capabilities or capabilities == ["conversation"]:
        route = "NONE"
    elif "record_query" in capabilities and "schema_query" in capabilities:
        route = "MIXED_DIRECT"
    elif any(c in capabilities for c in ("record_query", "record_search",
                                         "history_query", "text_analysis")):
        route = "DATA_DIRECT"
    elif any(c in capabilities for c in ("schema_query", "operational_context")):
        route = "SCHEMA_ONLY"
    elif any(c in capabilities for c in ("metadata_context", "business_definition")):
        route = "METADATA_ONLY"
    else:
        route = "NONE"
    ir.capabilities = capabilities
    return CapabilityPlan(capabilities=capabilities, sources=sources,
                          route=route, unsupported=unsupported)
