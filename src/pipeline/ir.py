"""The canonical semantic IR: what a question MEANS, before any Salesforce name.

The IR speaks business concepts ("interview", "recruiter", "client bill rate").
Grounding later maps each concept to a verified object or field, and planning
turns the grounded IR into deterministic queries. Nothing here is executable,
and nothing here names a Salesforce API identifier the model invented.

Every question is a composition of the same primitives -- measures, dimensions,
filters, ordering, existence, temporal constraints, comparisons, derived
metrics -- so a new kind of question is a new combination, not a new pipeline.

Kept as plain dataclasses, like the rest of src/, and serialisable both ways:
the IR is traced, stored as conversation state, and replayed by evaluation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field as dataclass_field
from typing import Any

OPERATION_FAMILIES = ("record", "schema", "metadata", "operational", "history",
                      "search", "text", "prediction", "business_definition",
                      "conversation", "none")
OUTPUT_MODES = ("records", "single_record", "count", "value", "grouped", "ranking",
                "comparison", "percentage", "ratio", "trend", "exists", "summary",
                "duplicate_groups",
                "schema_facts", "metadata_facts", "operational_facts",
                "history_timeline", "search_results", "answer")
MEASURE_OPS = ("count", "count_distinct", "sum", "avg", "min", "max")
FILTER_OPERATORS = ("equals", "not_equals", "greater_than", "greater_than_or_equal",
                    "less_than", "less_than_or_equal", "contains", "not_contains",
                    "starts_with", "in", "not_in", "is_null", "is_not_null",
                    "between", "before", "after", "on_or_before", "on_or_after")
OPERAND_TYPES = ("literal", "field_reference", "temporal", "boolean", "null", "set")
EXISTENCE_MODES = ("exists", "not_exists")
GRAINS = ("day", "week", "month", "quarter", "year")
SCHEMA_ASKS = ("object_for_concept", "fields_of_object", "field_for_concept",
               "field_datatype", "picklist_values", "lookup_target",
               "objects_referencing", "record_types", "required_fields",
               "standard_or_custom", "relationship_between", "describe_object")
DERIVED_KINDS = ("percentage", "ratio", "difference", "growth")
# Information sources the routing stage (the main model) can name (spec §8).
SOURCES = ("RECORD_DATA", "RUNTIME_SCHEMA", "METADATA_CONTEXT", "BUSINESS_KNOWLEDGE",
           "OPERATIONAL_CONTEXT", "HISTORY_CONTEXT", "TEXT_ANALYSIS",
           "CONVERSATION_CONTEXT")


@dataclass
class Entity:
    """A KIND of record the question is about. Never a specific record."""
    ref: str                        # stable id inside this IR: "e0", "e1"
    concept: str                    # the user's words: "interview", "candidate"
    role: str = "primary"           # primary | related | returned | context


@dataclass
class Operand:
    type: str = "literal"           # see OPERAND_TYPES
    value: Any = None               # literal / boolean / set members
    concept: str | None = None      # field_reference: the other attribute
    entity: str | None = None       # field_reference: whose attribute
    temporal: str | None = None     # temporal: the expression, resolved by code


@dataclass
class Filter:
    """`entity.concept operator right`. The owner is explicit (§19)."""
    entity: str                     # which entity's attribute: "e1"
    concept: str                    # "name", "status", "end time"
    operator: str = "equals"
    right: Operand = dataclass_field(default_factory=Operand)
    group: int = 0                  # filters in the same group are ANDed;
                                    # groups are ORed (AND/OR/NOT support)
    negate: bool = False


@dataclass
class Measure:
    op: str                         # see MEASURE_OPS
    entity: str                     # what is counted / whose attribute
    concept: str | None = None      # None for count(*) of the entity
    alias: str = ""


@dataclass
class Dimension:
    entity: str
    concept: str                    # "status", "recruiter", "created date"
    grain: str | None = None        # time bucket when the dimension is a date


@dataclass
class Ordering:
    target: str                     # "measure:0" | "attribute" | "date"
    entity: str | None = None
    concept: str | None = None      # attribute / date concept when not a measure
    descending: bool = True


@dataclass
class Temporal:
    """WHEN. The expression is resolved to a range by code, never by the model.

    `concept` names WHICH date ("created", "interview date"); schema linking
    picks the field. `purpose` says what the range does: restrict the records,
    define a comparison segment, or bound a trend window.
    """
    expression: str
    entity: str = "e0"
    concept: str | None = None
    purpose: str = "filter"         # filter | segment | window


@dataclass
class Segment:
    """One side of a comparison: a label and the extra filters that define it."""
    label: str
    filters: list[Filter] = dataclass_field(default_factory=list)
    temporal: Temporal | None = None


@dataclass
class Derived:
    kind: str                       # see DERIVED_KINDS
    numerator: list[Filter] = dataclass_field(default_factory=list)
    denominator: list[Filter] = dataclass_field(default_factory=list)


@dataclass
class Existence:
    mode: str                       # exists | not_exists
    entity: str                     # the related entity that must (not) exist
    filters: list[Filter] = dataclass_field(default_factory=list)


@dataclass
class SchemaAsk:
    kind: str                       # see SCHEMA_ASKS
    object_concept: str | None = None
    field_concept: str | None = None
    other_object_concept: str | None = None


@dataclass
class SemanticIR:
    question: str = ""
    family: str = "record"
    output_mode: str = "records"
    entities: list[Entity] = dataclass_field(default_factory=list)
    attributes: list[dict[str, str]] = dataclass_field(default_factory=list)  # {entity, concept}
    filters: list[Filter] = dataclass_field(default_factory=list)
    measures: list[Measure] = dataclass_field(default_factory=list)
    dimensions: list[Dimension] = dataclass_field(default_factory=list)
    ordering: list[Ordering] = dataclass_field(default_factory=list)
    limit: int | None = None
    distinct: bool = False
    # Duplicate detection is GROUP BY the dimensions plus HAVING COUNT(*) >
    # this threshold. One means "appears more than once".
    duplicate_threshold: int = 1
    temporal: list[Temporal] = dataclass_field(default_factory=list)
    comparison: list[Segment] = dataclass_field(default_factory=list)
    derived: list[Derived] = dataclass_field(default_factory=list)
    existence: list[Existence] = dataclass_field(default_factory=list)
    schema: list[SchemaAsk] = dataclass_field(default_factory=list)
    search_value: str | None = None
    follow_up: bool = False         # "the interviews you just showed"
    # Routing (§8): the sources are the main model's decision; the capabilities
    # are the handlers code found for them.
    sources: list[str] = dataclass_field(default_factory=list)
    capabilities: list[str] = dataclass_field(default_factory=list)
    # Stage-1 provenance, for tracing.
    model: str = ""
    endpoint: str = ""
    mode: str = ""
    duration_ms: int = 0
    completion_tokens: int = 0
    retried: bool = False

    # -- helpers ----------------------------------------------------------
    def entity(self, ref: str | None) -> Entity | None:
        return next((e for e in self.entities if e.ref == ref), None)

    @property
    def primary(self) -> Entity | None:
        return next((e for e in self.entities if e.role == "primary"),
                    self.entities[0] if self.entities else None)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    # -- the old Extraction contract, so routing and tracing keep working ----
    @property
    def request_type(self) -> str:
        if self.family == "record":
            return "DATA"
        if self.family in ("schema", "metadata", "business_definition"):
            return "MIXED" if self.measures or self.filters else "METADATA"
        return "DATA"

    @property
    def intent(self) -> str:
        return {"count": "record_count", "grouped": "grouped_record_count",
                "schema_facts": "field_metadata_lookup"}.get(self.output_mode,
                                                              "record_list")

    @property
    def action(self) -> str:
        if self.output_mode == "count":
            return "count"
        if self.measures or self.dimensions:
            return "aggregate"
        if self.comparison:
            return "compare"
        return "retrieve"

    @property
    def business_entities(self) -> list[dict[str, str]]:
        role = {"primary": "primary_entity", "related": "related_entity",
                "returned": "target_entity", "context": "context_entity"}
        return [{"name": e.concept, "role": role.get(e.role, "related_entity")}
                for e in self.entities]

    @property
    def requested_attributes(self) -> list[dict[str, str]]:
        return [{"entity": (self.entity(a.get("entity")) or Entity("", "")).concept,
                 "attribute": a.get("concept", "")} for a in self.attributes]

    @property
    def return_mode(self) -> str:
        return {"count": "count", "single_record": "single_record"}.get(
            self.output_mode, "records")

    @property
    def requires_record_query(self) -> bool:
        return "record_query" in self.capabilities

    @property
    def requires_schema_discovery(self) -> bool:
        return self.family in ("record", "schema", "history") and bool(self.entities)

    @property
    def requires_metadata_context(self) -> bool:
        return "METADATA_CONTEXT" in self.sources

    @property
    def metadata_types(self) -> list[str]:
        return []


def _operand(raw: Any) -> Operand:
    if isinstance(raw, dict):
        kind = raw.get("type") if raw.get("type") in OPERAND_TYPES else "literal"
        return Operand(type=kind, value=raw.get("value"),
                       concept=raw.get("concept"), entity=raw.get("entity"),
                       temporal=raw.get("temporal") or raw.get("expression"))
    if isinstance(raw, bool):
        return Operand(type="boolean", value=raw)
    if raw is None:
        return Operand(type="null")
    if isinstance(raw, list):
        return Operand(type="set", value=raw)
    return Operand(type="literal", value=raw)


def _filter(raw: dict[str, Any], default_entity: str) -> Filter | None:
    concept = str(raw.get("concept") or "").strip()
    if not concept:
        return None
    operator = raw.get("operator") if raw.get("operator") in FILTER_OPERATORS else "equals"
    right = _operand(raw.get("right", raw.get("value")))
    if operator in ("is_null", "is_not_null"):
        right = Operand(type="null")
    return Filter(entity=str(raw.get("entity") or default_entity), concept=concept[:80],
                  operator=operator, right=right, group=int(raw.get("group") or 0),
                  negate=bool(raw.get("negate")))


def _temporal(raw: Any, default_entity: str, purpose: str = "filter") -> Temporal | None:
    if not isinstance(raw, dict) or not str(raw.get("expression") or "").strip():
        return None
    return Temporal(expression=str(raw["expression"]).strip()[:80],
                    entity=str(raw.get("entity") or default_entity),
                    concept=(str(raw["concept"]).strip()[:80] if raw.get("concept") else None),
                    purpose=raw.get("purpose") if raw.get("purpose") in
                    ("filter", "segment", "window") else purpose)


def from_dict(payload: dict[str, Any], question: str = "") -> SemanticIR:
    """Build an IR from the model's JSON, dropping anything out of vocabulary.

    Lenient on shape, strict on vocabulary: an unknown operator or output mode
    is replaced by its safe default rather than trusted, and every reference to
    an entity must name one that exists.
    """
    ir = SemanticIR(question=question)
    family = payload.get("family")
    ir.family = family if family in OPERATION_FAMILIES else "record"
    mode = payload.get("output_mode")
    ir.output_mode = mode if mode in OUTPUT_MODES else "records"

    for index, item in enumerate(payload.get("entities") or []):
        if isinstance(item, dict) and str(item.get("concept") or "").strip():
            ir.entities.append(Entity(
                ref=str(item.get("ref") or f"e{index}"),
                concept=str(item["concept"]).strip()[:80],
                role=item.get("role") if item.get("role") in
                ("primary", "related", "returned", "context") else
                ("primary" if index == 0 else "related")))
    if ir.entities and not any(e.role == "primary" for e in ir.entities):
        ir.entities[0].role = "primary"
    refs = {e.ref for e in ir.entities}
    default = ir.primary.ref if ir.primary else "e0"

    def own(ref: Any) -> str:
        return ref if ref in refs else default

    for item in payload.get("attributes") or []:
        if isinstance(item, dict) and item.get("concept"):
            ir.attributes.append({"entity": own(item.get("entity")),
                                  "concept": str(item["concept"])[:80]})
    for item in payload.get("filters") or []:
        if isinstance(item, dict):
            f = _filter(item, default)
            if f:
                f.entity = own(f.entity)
                if f.right.type == "field_reference":
                    f.right.entity = own(f.right.entity)
                ir.filters.append(f)
    for item in payload.get("measures") or []:
        if isinstance(item, dict) and item.get("op") in MEASURE_OPS:
            ir.measures.append(Measure(op=item["op"], entity=own(item.get("entity")),
                                       concept=(str(item["concept"])[:80]
                                                if item.get("concept") else None),
                                       alias=str(item.get("alias") or "")[:40]))
    for item in payload.get("dimensions") or []:
        if isinstance(item, dict) and item.get("concept"):
            ir.dimensions.append(Dimension(
                entity=own(item.get("entity")), concept=str(item["concept"])[:80],
                grain=item.get("grain") if item.get("grain") in GRAINS else None))
    for item in payload.get("ordering") or []:
        if isinstance(item, dict) and item.get("target"):
            ir.ordering.append(Ordering(
                target=str(item["target"]), entity=own(item.get("entity")),
                concept=item.get("concept"),
                descending=item.get("direction", "desc") != "asc"))
    limit = payload.get("limit")
    ir.limit = int(limit) if isinstance(limit, (int, float)) and 0 < limit <= 1000 else None
    ir.distinct = bool(payload.get("distinct"))
    threshold = payload.get("duplicate_threshold", 1)
    if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
        ir.duplicate_threshold = max(1, min(int(threshold), 1_000_000))
    for item in payload.get("temporal") or []:
        t = _temporal(item, default)
        if t:
            t.entity = own(t.entity)
            ir.temporal.append(t)
    for item in payload.get("comparison") or []:
        if isinstance(item, dict) and item.get("label"):
            segment = Segment(label=str(item["label"])[:60])
            segment.filters = [f for f in (_filter(x, default) for x in
                               item.get("filters") or [] if isinstance(x, dict)) if f]
            segment.temporal = _temporal(item.get("temporal"), default, "segment")
            ir.comparison.append(segment)
    for item in payload.get("derived") or []:
        if isinstance(item, dict) and item.get("kind") in DERIVED_KINDS:
            ir.derived.append(Derived(
                kind=item["kind"],
                numerator=[f for f in (_filter(x, default) for x in
                           item.get("numerator") or [] if isinstance(x, dict)) if f],
                denominator=[f for f in (_filter(x, default) for x in
                             item.get("denominator") or [] if isinstance(x, dict)) if f]))
    for item in payload.get("existence") or []:
        if isinstance(item, dict) and item.get("mode") in EXISTENCE_MODES \
                and item.get("entity") in refs:
            ir.existence.append(Existence(
                mode=item["mode"], entity=item["entity"],
                filters=[f for f in (_filter(x, item["entity"]) for x in
                         item.get("filters") or [] if isinstance(x, dict)) if f]))
    for item in payload.get("schema") or []:
        if isinstance(item, dict) and item.get("kind") in SCHEMA_ASKS:
            ir.schema.append(SchemaAsk(kind=item["kind"],
                                       object_concept=item.get("object_concept"),
                                       field_concept=item.get("field_concept"),
                                       other_object_concept=item.get("other_object_concept")))
    value = payload.get("search_value")
    ir.search_value = str(value)[:120] if value else None
    ir.follow_up = bool(payload.get("follow_up"))
    seen: list[str] = []
    for item in payload.get("sources") or []:
        name = str(item or "").strip().upper()
        if name in SOURCES and name not in seen:
            seen.append(name)
    ir.sources = seen
    return ir


def ir_from_dict(data: dict[str, Any]) -> SemanticIR:
    """Rebuild a stored IR exactly (conversation state), including provenance."""
    ir = from_dict(data, data.get("question", ""))
    for key in ("capabilities", "sources", "model", "endpoint", "mode"):
        if key in data:
            setattr(ir, key, data[key])
    return ir
