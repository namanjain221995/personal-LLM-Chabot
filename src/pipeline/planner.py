"""Stage 5: the main model writes the logical query plan; code compiles it.

Grounding has already chosen, with the main model, every object, relationship
path and field (the bindings). This stage decides how the question is
COMPUTED from them -- which bindings filter, which are measured, grouped,
ordered or shown, the operation, the limit, existence, the parts of a derived
metric, the sides of a comparison, and the periods as structured temporal
semantics (spec §13, §15, §16).

Then deterministic code turns the plan into the `GroundedQuery` the analytic
SQL planner reads: binding ids become verified field references, stored
values are checked against the runtime schema, aggregate/type compatibility
is enforced, periods become half-open ranges, a filter on an entity reached
through a child relationship becomes an EXISTS. The model never writes SQL,
never names an API identifier (only binding ids and entity refs), and never
does calendar arithmetic.

A plan that fails a technical check is returned to the model once, told
exactly what failed; a second failure is PLANNING_FAILED, never a plan code
invented instead.
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Any

from record_query.grounded import (GDimension, GExists, GFilter, GMeasure, GOrder,
                                   GRef, GroundedQuery, GSegment)
from salesforce.schema_linking.ir_grounder import (CHECKBOX, NUMERIC, PICKLIST, TEMPORAL,
                                                   alias, boolean, comparable)
from salesforce.schema_linking.retriever import normalise
from salesforce.schema_linking.temporal import from_semantics

from .ir import FILTER_OPERATORS, GRAINS, MEASURE_OPS, OUTPUT_MODES

log = logging.getLogger(__name__)

OPERATIONS = ("records", "single_record", "count", "value", "grouped", "ranking",
              "comparison", "percentage", "ratio", "trend", "exists", "duplicate_groups",
              "summary")

SYSTEM = """You write the LOGICAL QUERY PLAN for ONE Salesforce question. The objects,
relationship paths and fields are already chosen and verified. You decide how
the question is COMPUTED from them. Use ONLY the binding ids and entity refs
given; never write SQL, API names, or dates you calculated.

You receive: the question; its meaning in business words (intent); entities
(ref, object, whether it is the fact entity whose records are scanned, and how
it is reached from the fact: "one (parent)" or "many (child)"); bindings (id,
entity, field label, data_type, picklist_values, stored_values, used_as).

Return minified JSON, omitting empty keys:
{"operation":"records|single_record|count|value|grouped|ranking|comparison|percentage|ratio|trend|exists|duplicate_groups",
 "filters":[{"binding":"S0","operator":"equals","value":"Completed","group":0}],
 "periods":[{"binding":"S2","period":{"kind":"relative","unit":"month","offset":0}}],
 "measures":[{"op":"count","binding":null,"entity":"e0"}],
 "dimensions":[{"binding":"S1","grain":null}],
 "attributes":["S3"],
 "ordering":[{"measure":0,"direction":"desc"}],
 "limit":5,"distinct":false,"duplicate_threshold":1,
 "existence":[{"mode":"not_exists","entity":"e1","filters":[]}],
 "derived":{"kind":"percentage","numerator":[filters],"denominator":[filters]},
 "comparison":[{"label":"this month","filters":[],"period":{"binding":"S2","period":{...}}}]}

filters: operator one of equals not_equals greater_than greater_than_or_equal
 less_than less_than_or_equal contains not_contains starts_with in not_in
 is_null is_not_null. value: a stored value copied exactly from stored_values or
 picklist_values; true/false for a checkbox; a list for in/not_in. To compare two
 fields use "other_binding":"S4" instead of value. Same group = AND, different
 groups = OR. "negate":true negates one filter.
A date binding is restricted to a time range ONLY through "periods" (or a
comparison side's "period"), never through a filter value like "2026".
period (structured; code computes the dates):
 {"kind":"relative","unit":"day|week|month|quarter|year","offset":0}  this
   month offset 0, last month -1, next week 1, today = day 0, yesterday = day -1
 {"kind":"last_n"|"next_n","unit":"month","n":6}         last six months
 {"kind":"older_than"|"within_last","unit":"day","n":60}
 {"kind":"calendar","year":2026,"month":5}  (or "quarter":2, or month + "day")
 {"kind":"between","start":<period>,"end":<period>}
 {"kind":"before"|"after"|"since"|"until","anchor":<period>}
 {"kind":"upcoming"} | {"kind":"past"}
Rules:
- How many fact records: measure {"op":"count","binding":null,"entity":<fact>}.
- Unique records of another entity: {"op":"count_distinct","binding":null,"entity":<it>};
  distinct values of a field: {"op":"count_distinct","binding":"S1"}.
- sum/avg need a numeric binding; grain only on a date binding (trend).
- Ranking ("top 5 X by Y"): dimension + measure + ordering {"measure":0} + limit.
- Percentage: derived.numerator = filters selecting the part; derived.denominator =
  filters for the whole ([] = every fact record); never repeat numerator filters in
  top-level filters.
- Comparison: one entry per side, each with its own filters and/or period.
- Duplicates: operation duplicate_groups, dimensions = the keys, count measure,
  duplicate_threshold (1 = occurs more than once).
- "X with / without Y" where Y is reached as many (child): existence exists /
  not_exists on Y, with Y's own conditions as its filters.
- When Y IS the fact entity ("does candidate X have interviews" with fact
  interview): no existence entry; filter the fact records (X's name binding)
  and use operation exists.
- Latest / earliest: ordering {"binding":<date>,"direction":"desc|asc"} + limit 1.
- A binding used as "shown attribute" goes in attributes for record lists.
- To show or group by a related record itself (its name), use {"entity":"e1"}
  in attributes or dimensions; no binding is needed for that.
- A binding whose concept the operation does not need may be left unused.
- Concepts in implied_by_object are already true of every fact record (the
  object itself means them): write no filter for them.
- "What is the <field> of <record X>": operation single_record, filter X's
  name binding, attributes [the field's binding]; no measure."""


class LogicalPlanner:
    def __init__(self, endpoint: str, model: str, schema: Any, verifier: Any, *,
                 timeout: float = 60.0, max_tokens: int = 1200, client: Any = None) -> None:
        from .stage_model import StageModel
        self.schema, self.verifier = schema, verifier
        self.max_tokens = max_tokens
        self.stage = StageModel(endpoint, model, timeout=timeout, client=client)

    @property
    def client(self) -> Any:
        return self.stage.client

    @client.setter
    def client(self, value: Any) -> None:
        self.stage.client = value

    def plan(self, question: str, g: GroundedQuery, *, log_to: Any = None
             ) -> tuple[dict[str, Any] | None, Any]:
        """The model's plan, already proven to compile, and its call record."""
        payload = {"question": question,
                   "intent": _intent(g.ir, (g.semantic or {}).get("implied")),
                   "entities": g.entities,
                   "bindings": [b.for_model() for b in g.bindings.values()]}
        implied = (g.semantic or {}).get("implied") or []
        if implied:
            # Concepts the chosen object already expresses ("internal" when the
            # object is Internal Interview): no binding, so no filter. Without
            # this the plan filtered on a binding that did not exist.
            payload["implied_by_object"] = implied
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False,
                                                           default=str)}]

        def validate(plan: Any) -> str:
            trial = copy.deepcopy(g)
            errors = compile_plan(plan, trial, self.schema, self.verifier)
            return "; ".join(errors[:3])

        plan, call = self.stage.call("planning", messages, log_to=log_to,
                                     validate=validate, max_tokens=self.max_tokens,
                                     candidate_count=len(g.bindings),
                                     retry_hint="Return the corrected plan as minified "
                                                "JSON, same shape.")
        if plan is not None:
            call.selected = {k: plan.get(k) for k in ("operation", "measures", "limit")
                             if plan.get(k) is not None}
        return plan, call


# =====================================================================
def compile_plan(plan: Any, g: GroundedQuery, schema: Any, verifier: Any) -> list[str]:
    """Logical plan -> the GroundedQuery fields. Returns the technical errors.

    `g` arrives with objects, paths, bindings and the base already verified;
    this fills filters, measures, dimensions, ordering, attributes, periods,
    existence, segments, derived parts, limit and output mode.
    """
    errors: list[str] = []
    if not isinstance(plan, dict):
        return ["the plan is not a JSON object"]
    bindings = g.bindings
    entity_refs = set(g.objects)

    def entity_name(raw: Any, where: str):
        """{"entity":"e1"} -> that related record's Name, verified on its object."""
        ref = raw.get("entity") if isinstance(raw, dict) and not raw.get("binding") else None
        if ref is None:
            return None
        if ref not in entity_refs or not schema.repo.get_field(g.objects[ref], "Name"):
            errors.append(f"{where}: {ref!r} is not an entity with a name")
            return False
        concept = next((e["concept"] for e in g.entities if e["ref"] == ref), ref)
        return GRef(ref, "Name", "Text", "Name"), alias(f"{concept} name")

    def binding(bid: Any, where: str):
        b = bindings.get(str(bid)) if bid is not None else None
        if b is None:
            errors.append(f"{where}: {bid!r} is not a binding id ({sorted(bindings)}); "
                          'to show a related record itself use {"entity":"<ref>"}')
        return b

    def build_filter(raw: Any, where: str) -> GFilter | None:
        if not isinstance(raw, dict):
            errors.append(f"{where}: a filter must be an object")
            return None
        b = binding(raw.get("binding"), where)
        if b is None:
            return None
        operator = raw.get("operator") or "equals"
        if operator not in FILTER_OPERATORS:
            errors.append(f"{where}: unknown operator {operator!r}")
            return None
        left = b.ref()
        out = GFilter(left=left, operator=operator, group=int(raw.get("group") or 0)
                      if isinstance(raw.get("group"), (int, float)) else 0,
                      negate=bool(raw.get("negate")))
        dtype = normalise(b.data_type)
        if raw.get("other_binding") is not None:
            other = binding(raw.get("other_binding"), where)
            if other is None:
                return None
            out.kind, out.right = "field", other.ref()
            out.compare_as = comparable(schema, g.objects, left, out.right)
            if out.compare_as is None:
                errors.append(f"{where}: {b.label} ({b.data_type}) and {other.label} "
                              f"({other.data_type}) cannot be compared")
                return None
            return out
        if operator in ("is_null", "is_not_null"):
            out.kind = "null"
            return out
        value = raw.get("value")
        if value is None:
            errors.append(f"{where}: {b.id} {operator} needs a value")
            return None
        if dtype in CHECKBOX:
            flag = boolean(value)
            if flag is None:
                errors.append(f"{where}: {b.label} is a checkbox; value must be true/false")
                return None
            out.kind, out.value = "boolean", flag
            return out
        if dtype in PICKLIST and operator in ("equals", "not_equals", "in", "not_in"):
            values = value if isinstance(value, list) else [value]
            stored = []
            for v in values:
                if isinstance(v, bool):
                    errors.append(f"{where}: {b.label} is a picklist; use one of its "
                                  f"values {b.picklist_values[:15]}, not {v}")
                    return None
                s = verifier.verify_picklist_value(g.objects[b.entity], b.field, v)
                if s is None:
                    errors.append(f"{where}: {v!r} is not a value of {b.label}; values: "
                                  f"{b.picklist_values[:15]}")
                    return None
                stored.append(s)
            out.kind = "set" if isinstance(value, list) else "literal"
            out.value = stored if isinstance(value, list) else stored[0]
            return out
        if dtype in TEMPORAL:
            # A date filter takes a calendar date, never a period or a word.
            # "2026", "September 2026", "true" reached DuckDB as literals and
            # failed there: 23 of the first 220 benchmark questions, live.
            import re as _re
            items = value if isinstance(value, list) else [value]
            if not all(isinstance(v, str) and _re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?Z?)?", v.strip())
                    for v in items):
                errors.append(f"{where}: {b.label} is a date; {value!r} is not a date. "
                              'For a time range use "periods":[{"binding":"' + b.id +
                              '","period":{...}}] instead of a filter')
                return None
        if isinstance(value, bool):
            out.kind, out.value = "literal", value
            return out
        out.kind = "set" if isinstance(value, list) else "literal"
        out.value = value
        return out

    def build_period(raw: Any, where: str) -> list[GFilter]:
        if not isinstance(raw, dict):
            errors.append(f"{where}: a period entry must be an object")
            return []
        b = binding(raw.get("binding"), where)
        if b is None:
            return []
        if normalise(b.data_type) not in TEMPORAL:
            errors.append(f"{where}: {b.label} is {b.data_type}, not a date")
            return []
        found = from_semantics(raw.get("period"))
        if found is None:
            errors.append(f"{where}: unreadable period {raw.get('period')!r}")
            return []
        g.periods.append({**found.as_dict(), "field": b.field,
                          **({"segment": raw["_segment"]} if raw.get("_segment") else {})})
        return _range(b.ref(), found)

    operation = plan.get("operation")
    if operation not in OPERATIONS and operation not in OUTPUT_MODES:
        errors.append(f"operation must be one of {list(OPERATIONS)}")
        operation = "records"
    g.output_mode = operation

    for i, raw in enumerate(plan.get("filters") or []):
        built = build_filter(raw, f"filters[{i}]")
        if built:
            g.filters.append(built)
    for i, raw in enumerate(plan.get("periods") or []):
        g.filters.extend(build_period(raw, f"periods[{i}]"))

    for i, raw in enumerate(plan.get("measures") or []):
        if not isinstance(raw, dict) or raw.get("op") not in MEASURE_OPS:
            errors.append(f"measures[{i}]: op must be one of {list(MEASURE_OPS)}")
            continue
        op = raw["op"]
        ref: GRef | None = None
        if raw.get("binding") is not None:
            b = binding(raw.get("binding"), f"measures[{i}]")
            if b is None:
                continue
            ref = b.ref()
            if op in ("sum", "avg") and normalise(b.data_type) not in NUMERIC:
                errors.append(f"measures[{i}]: {op} needs a number; {b.label} is {b.data_type}")
                continue
        elif op == "count_distinct":
            entity = raw.get("entity")
            if entity not in entity_refs:
                errors.append(f"measures[{i}]: count_distinct needs a binding or an entity ref")
                continue
            ref = GRef(entity, "Id")
        elif op != "count":
            errors.append(f"measures[{i}]: {op} needs a binding")
            continue
        name = (f"{op}_{ref.field}" if ref is not None and ref.field and ref.field != "Id" else
                ("record_count" if op == "count" else f"{op}_records"))
        g.measures.append(GMeasure(op, ref, name))

    for i, raw in enumerate(plan.get("dimensions") or []):
        named = entity_name(raw, f"dimensions[{i}]")
        if named is False:
            continue
        if named:
            g.dimensions.append(GDimension(named[0], None, named[1]))
            continue
        b = binding(raw.get("binding") if isinstance(raw, dict) else raw, f"dimensions[{i}]")
        if b is None:
            continue
        grain = raw.get("grain") if isinstance(raw, dict) else None
        if grain is not None and grain not in GRAINS:
            errors.append(f"dimensions[{i}]: grain must be one of {list(GRAINS)}")
            continue
        if grain and normalise(b.data_type) not in TEMPORAL:
            errors.append(f"dimensions[{i}]: {b.label} is not a date; no grain")
            continue
        g.dimensions.append(GDimension(b.ref(), grain, alias(b.label or b.field)))

    for i, raw in enumerate(plan.get("ordering") or []):
        if not isinstance(raw, dict):
            continue
        descending = str(raw.get("direction", "desc")).lower() != "asc"
        if raw.get("measure") is not None:
            index = raw.get("measure")
            if not isinstance(index, int) or not 0 <= index < len(g.measures):
                errors.append(f"ordering[{i}]: measure {index!r} does not exist")
                continue
            g.ordering.append(GOrder(measure=index, descending=descending))
        else:
            b = binding(raw.get("binding"), f"ordering[{i}]")
            if b is not None:
                g.ordering.append(GOrder(ref=b.ref(), descending=descending))

    base = g.base
    for i, raw in enumerate(plan.get("attributes") or []):
        named = entity_name(raw, f"attributes[{i}]")
        if named is False:
            continue
        if named:
            g.attributes.append(named)
            continue
        b = binding(raw.get("binding") if isinstance(raw, dict) else raw, f"attributes[{i}]")
        if b is None:
            continue
        label = b.concept or b.label or b.field
        owner = next((e["concept"] for e in g.entities if e["ref"] == b.entity), "")
        if b.entity != base and owner and owner.lower() not in str(label).lower():
            label = f"{owner} {label}"
        g.attributes.append((b.ref(), alias(label)))

    limit = plan.get("limit")
    if limit is not None:
        if isinstance(limit, (int, float)) and not isinstance(limit, bool) and 0 < limit <= 1000:
            g.limit = int(limit)
        else:
            errors.append("limit must be a whole number from 1 to 1000")
    g.distinct = bool(plan.get("distinct"))
    threshold = plan.get("duplicate_threshold")
    if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
        g.duplicate_threshold = max(1, min(int(threshold), 1_000_000))

    for i, raw in enumerate(plan.get("comparison") or []):
        if not isinstance(raw, dict) or not raw.get("label"):
            errors.append(f"comparison[{i}]: each side needs a label")
            continue
        segment = GSegment(label=str(raw["label"])[:60])
        for j, f in enumerate(raw.get("filters") or []):
            built = build_filter(f, f"comparison[{i}].filters[{j}]")
            if built:
                segment.filters.append(built)
        if raw.get("period"):
            period = dict(raw["period"]) if isinstance(raw["period"], dict) else raw["period"]
            if isinstance(period, dict):
                period["_segment"] = segment.label
            segment.filters.extend(build_period(period, f"comparison[{i}].period"))
        g.segments.append(segment)

    derived = plan.get("derived")
    if isinstance(derived, dict) and derived:
        kind = derived.get("kind")
        if kind not in ("percentage", "ratio", "difference", "growth"):
            errors.append("derived.kind must be percentage, ratio, difference or growth")
        else:
            g.derived = kind
            for part in ("numerator", "denominator"):
                for j, f in enumerate(derived.get(part) or []):
                    built = build_filter(f, f"derived.{part}[{j}]")
                    if built:
                        getattr(g, part).append(built)
            if kind in ("percentage", "ratio") and not g.numerator:
                errors.append("a percentage needs derived.numerator: the filters that "
                              "select the part")

    for i, raw in enumerate(plan.get("existence") or []):
        if isinstance(raw, dict) and raw.get("entity") == base:
            errors.append(f"existence[{i}]: {base} is the fact entity, so its records are "
                          "the ones scanned; drop this existence entry, filter the fact "
                          "records (including through related entities' bindings) and use "
                          "operation exists or count")
            continue
        if not isinstance(raw, dict) or raw.get("mode") not in ("exists", "not_exists") \
                or raw.get("entity") not in entity_refs:
            errors.append(f"existence[{i}]: needs mode exists|not_exists and a non-fact entity ref")
            continue
        ref = raw["entity"]
        hops = g.paths.get(ref) or []
        conditions = [x for x in (build_filter(f, f"existence[{i}].filters[{j}]")
                                  for j, f in enumerate(raw.get("filters") or [])) if x]
        if len(hops) == 1 and hops[0].direction == "child":
            g.existence.append(GExists(mode=raw["mode"], child_object=g.objects[ref],
                                       child_field=hops[0].field, parent_entity=base,
                                       filters=conditions, child_entity=ref))
            g.paths.pop(ref, None)            # checked by EXISTS, not joined
        elif len(hops) == 1 and hops[0].direction == "parent" and not conditions:
            # A related entity reached as a PARENT "exists" when its lookup is set.
            g.filters.append(GFilter(GRef(base, hops[0].field),
                                     "is_null" if raw["mode"] == "not_exists" else "is_not_null",
                                     kind="null"))
            g.paths.pop(ref, None)
        else:
            errors.append(f"existence[{i}]: {g.objects[ref]} is not one direct relationship "
                          f"away from {g.objects[base]}")

    _check_shape(g, errors)
    if not errors:
        _child_filters_to_exists(g, errors)
        _show_returned_names(g, schema)
    return errors


def _check_shape(g: GroundedQuery, errors: list[str]) -> None:
    mode = g.output_mode
    if mode in ("count", "value", "grouped", "ranking", "trend", "duplicate_groups") \
            and not g.measures:
        errors.append(f"operation {mode} needs a measure")
    if mode in ("grouped", "ranking", "trend", "duplicate_groups") and not g.dimensions:
        errors.append(f"operation {mode} needs a dimension")
    if mode == "trend" and not any(d.grain for d in g.dimensions):
        errors.append("a trend needs a date dimension with a grain")
    if mode == "comparison" and len(g.segments) < 2:
        errors.append("a comparison needs two sides")
    if mode in ("percentage", "ratio") and not g.derived:
        errors.append(f"operation {mode} needs derived")


def _child_filters_to_exists(g: GroundedQuery, errors: list[str]) -> None:
    """A filter owned by an entity reached through a CHILD hop cannot be a join
    (it would multiply rows): it becomes an EXISTS semi-join."""
    for ref, hops in list(g.paths.items()):
        if not hops or not any(h.direction == "child" for h in hops):
            continue
        owned = [f for f in g.filters if f.left.entity == ref]
        used_elsewhere = any(m.ref and m.ref.entity == ref for m in g.measures) or \
            any(d.ref.entity == ref for d in g.dimensions) or \
            any(a[0].entity == ref for a in g.attributes)
        if len(hops) == 1 and not used_elsewhere:
            if owned:
                g.filters = [f for f in g.filters if f.left.entity != ref]
                g.existence.append(GExists("exists", g.objects[ref], hops[0].field,
                                           g.base, owned, child_entity=ref))
                g.paths.pop(ref)
        elif used_elsewhere:
            errors.append(f"{g.objects[ref]} is reached through a child relationship and "
                          "cannot be shown or grouped per row; make it the fact entity's "
                          "measure or an existence condition")


def _show_returned_names(g: GroundedQuery, schema: Any) -> None:
    """"Who is the candidate of BCN-00028": a returned entity is named by its Name."""
    for e in g.entities:
        ref = e["ref"]
        if e.get("role") == "returned" and ref in g.paths and \
                not any(a[0].entity == ref for a in g.attributes) and \
                not any(d.ref.entity == ref for d in g.dimensions) and \
                not g.measures and schema.repo.get_field(g.objects[ref], "Name"):
            g.attributes.append((GRef(ref, "Name", "Text", "Name"),
                                 alias(f"{e['concept']} name")))


def _range(ref: GRef, period: Any) -> list[GFilter]:
    out = []
    if period.start is not None:
        out.append(GFilter(ref, "on_or_after", value=period.start.isoformat()))
    if period.end_exclusive is not None:
        out.append(GFilter(ref, "before", value=period.end_exclusive.isoformat()))
    return out


def _intent(ir: Any, implied: list[str] | None = None) -> dict[str, Any] | None:
    """The question's meaning, compact: what the plan must compute.

    Filters the linking model declared implied by the chosen object are left
    out: they are already true of every record, and shown here they were
    written into the plan against a binding that does not exist.
    """
    if ir is None:
        return None
    data = ir.as_dict()
    gone = {i.split(": ", 1)[1].strip().lower() if ": " in i else i.lower()
            for i in implied or []}
    if gone:
        data["filters"] = [f for f in data.get("filters") or []
                           if str(f.get("concept") or "").lower() not in gone]
    keep = ("output_mode", "entities", "filters", "measures", "dimensions", "ordering",
            "limit", "distinct", "temporal", "comparison", "derived", "existence",
            "attributes", "duplicate_threshold")
    out = {k: data[k] for k in keep if data.get(k) not in (None, [], {}, False)}
    return out
