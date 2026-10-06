"""The grounded query: the IR with every concept replaced by verified schema.

Planning reads ONLY this. Every object and field in it has been checked against
the runtime schema; every value that must be a picklist value is the stored
spelling; every relationship is a path the graph found. Nothing here was named
by a model without being verified.

`entity` refs are the IR's ("e0", "e1"). Each entity has an object and, unless
it is the base, the path that reaches it from the base.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any


@dataclass
class GRef:
    entity: str
    field: str | None                 # None: the record itself (COUNT(*))
    data_type: str | None = None
    label: str | None = None


@dataclass
class GFilter:
    left: GRef
    operator: str
    kind: str = "literal"             # literal | field | null | set | boolean
    value: Any = None
    right: GRef | None = None
    group: int = 0
    negate: bool = False
    compare_as: str | None = None     # "time" when both sides store "08:30 AM"


@dataclass
class GMeasure:
    op: str                           # count | count_distinct | sum | avg | min | max
    ref: GRef | None
    alias: str


@dataclass
class GDimension:
    ref: GRef
    grain: str | None
    alias: str


@dataclass
class GOrder:
    measure: int | None = None
    ref: GRef | None = None
    descending: bool = True


@dataclass
class GExists:
    mode: str                         # exists | not_exists
    child_object: str
    child_field: str                  # the lookup ON the child to the parent
    parent_entity: str
    filters: list[GFilter] = dataclass_field(default_factory=list)
    child_entity: str = ""


@dataclass
class GSegment:
    label: str
    filters: list[GFilter] = dataclass_field(default_factory=list)


@dataclass
class GroundedQuery:
    base: str = ""                                    # entity ref the query runs over
    objects: dict[str, str] = dataclass_field(default_factory=dict)
    paths: dict[str, list[Any]] = dataclass_field(default_factory=dict)  # ref -> [Hop]
    filters: list[GFilter] = dataclass_field(default_factory=list)
    measures: list[GMeasure] = dataclass_field(default_factory=list)
    dimensions: list[GDimension] = dataclass_field(default_factory=list)
    ordering: list[GOrder] = dataclass_field(default_factory=list)
    attributes: list[tuple[GRef, str]] = dataclass_field(default_factory=list)
    existence: list[GExists] = dataclass_field(default_factory=list)
    segments: list[GSegment] = dataclass_field(default_factory=list)
    numerator: list[GFilter] = dataclass_field(default_factory=list)
    denominator: list[GFilter] = dataclass_field(default_factory=list)
    derived: str | None = None                        # percentage | ratio | ...
    limit: int | None = None
    distinct: bool = False
    output_mode: str = "records"
    duplicate_threshold: int = 1
    periods: list[dict[str, Any]] = dataclass_field(default_factory=list)
    failures: list[dict[str, Any]] = dataclass_field(default_factory=list)
    semantic: dict[str, Any] = dataclass_field(default_factory=dict)
    # Linking output the planning stage writes the plan from: slot id ->
    # schema_linking.ir_grounder.Binding, and the entities with their objects.
    bindings: dict[str, Any] = dataclass_field(default_factory=dict)
    entities: list[dict[str, Any]] = dataclass_field(default_factory=list)
    ir: Any = None                                    # the IR grounding read

    @property
    def base_object(self) -> str:
        return self.objects.get(self.base, "")

    @property
    def grounded(self) -> bool:
        return bool(self.base_object) and not self.failures

    def fail(self, code: str, detail: str, **context: Any) -> None:
        self.failures.append({"code": code, "detail": detail, **context})

    def as_dict(self) -> dict[str, Any]:
        """The trace store's grounding contract, from the grounded IR."""
        return {**self.summary(), "schema_grounded": self.grounded,
                "primary_object": self.base_object,
                "objects": sorted(set(self.objects.values())),
                "semantic": self.semantic}

    def summary(self) -> dict[str, Any]:
        """Compact, for tracing: what was decided, not the rows."""
        def ref(r: GRef | None) -> str | None:
            return f"{self.objects.get(r.entity, r.entity)}.{r.field or '*'}" if r else None
        return {
            "base": self.base_object, "objects": self.objects,
            "paths": {k: [h.describe() for h in v] for k, v in self.paths.items()},
            "filters": [f"{ref(f.left)} {f.operator} "
                        f"{ref(f.right) if f.kind == 'field' else repr(f.value)}"
                        for f in self.filters],
            "measures": [f"{m.op}({ref(m.ref) or '*'})" for m in self.measures],
            "dimensions": [f"{ref(d.ref)}{'/' + d.grain if d.grain else ''}"
                           for d in self.dimensions],
            "ordering": [f"{'measure:' + str(o.measure) if o.measure is not None else ref(o.ref)}"
                         f" {'desc' if o.descending else 'asc'}" for o in self.ordering],
            "existence": [f"{e.mode} {e.child_object}.{e.child_field}" for e in self.existence],
            "segments": [s.label for s in self.segments],
            "numerator": [f"{ref(f.left)} {f.operator} {f.value!r}" for f in self.numerator],
            "denominator": [f"{ref(f.left)} {f.operator} {f.value!r}" for f in self.denominator],
            "derived": self.derived, "limit": self.limit,
            "duplicate_threshold": self.duplicate_threshold,
            "periods": self.periods, "failures": self.failures,
        }
