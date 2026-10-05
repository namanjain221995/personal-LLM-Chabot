"""Canonical IR -> objects, relationship paths and field bindings, decided by
the main model.

    discovery (main model)    every entity -> one object, chosen by candidate
                              ID from high-recall retrieval; and the fact
                              entity (the records scanned)
    paths     (graph)         relationship paths from the fact entity, enumerated
    linking   (main model)    every concept -> one field (candidate ID), every
                              user value -> its stored value, every non-fact
                              entity -> one relationship path (candidate ID)
    verify    (runtime schema) the chosen IDs exist, belong to the object, the
                              stored values are real

Retrieval, the graph and the runtime schema supply EVIDENCE and enforce
technical truth; they never make the choice. There is no fast path, no
confidence threshold that skips the model, and nothing the model left out is
filled in from retrieval (spec §5, §6, §9, §11). The logical plan is written
afterwards by the planning stage (`pipeline.planner`) from these bindings.

A model that cannot be reached fails the question with MODEL_UNAVAILABLE; a
model that finds no fitting candidate may name other words to look for, which
buys one more retrieval and one more call, never a guess.
"""
from __future__ import annotations

import copy
import json
import logging
import re
import time
from dataclasses import dataclass, field as dataclass_field
from typing import Any

from record_query.grounded import GRef, GroundedQuery

from .graph import Hop, RelationshipGraph
from .retriever import normalise
from .temporal import parse as parse_period

log = logging.getLogger(__name__)

TEMPORAL = {"date", "datetime"}
NUMERIC = {"number", "currency", "percent", "double", "int", "integer", "long"}
PICKLIST = {"picklist", "multiselectpicklist"}
CHECKBOX = {"checkbox", "boolean"}
NAME_CONCEPTS = {"name", "id", "number", "record", "record name", "code", "title"}
CREATED = {"created", "created date", "creation date", "created on", "creation",
           "created at", "date created"}
MODIFIED = {"modified", "last modified", "updated", "last updated", "modified date",
            "last modified date", "updated date"}
TRUE_WORDS = {"true", "yes", "y", "1", "checked", "enabled", "active", "available", "on"}
FALSE_WORDS = {"false", "no", "n", "0", "unchecked", "disabled", "inactive",
               "unavailable", "not available", "off"}
TIME_VALUE = re.compile(r"^\s*\d{1,2}:\d{2}\s*(AM|PM)\s*$", re.I)

DISCOVERY_SYSTEM = """You decide which Salesforce object each business entity in ONE
question means. Each entity lists candidate objects with an id. Choose by
MEANING in the whole question, not by the closest spelling. A qualifier the
user put on an entity ("internal interviews", "contract positions") may name a
more specific object; choose that object when one is offered.
`business_knowledge` is hand-reviewed org vocabulary and outranks a label match.

Also choose fact_entity: the entity whose records are scanned, counted or
filtered (the FACT). It can differ from the entity the answer is about:
"how many unique candidates had interviews" -> fact interview;
"top recruiters by number of interviews" -> fact interview;
"candidates with no interviews" -> fact candidate;
"does candidate X have interviews" -> fact candidate.
An entity listed under intent.existence is checked for, never the fact.

Each candidate may list `holds`: fields on it that match a concept or value the
question gives for that entity ("Offer Received" lives in Interview Outcome).
An object that holds the question's values is strong evidence.

If no candidate fits an entity, give object_id null and search_terms: other
words the object could be called. Choose ONLY ids offered.

Return minified JSON only:
{"entities":[{"ref":"e0","object_id":"O1","confidence":0.9,"search_terms":[]}],
 "fact_entity":"e0"}"""

LINKING_SYSTEM = """You map ONE Salesforce question's business concepts onto fields
of objects that were already chosen. Every slot is one concept; choose the
field_id whose MEANING matches how the question uses it. Choose ONLY ids
offered for that slot.

A value the user gave ("Offer Received") is strong evidence for the field whose
picklist_values contain it, even when the user called it "status".
`business_knowledge` is hand-reviewed org vocabulary and outranks a label match.

For every user value listed in a slot's `values`, give the stored value under
"values": the exact picklist value for a picklist; true or false for a checkbox
("checked", "yes", "active", "enabled" -> true; "unchecked", "no" -> false);
otherwise the value unchanged.

Never choose a candidate marked deprecated when another fits. Two slots
"compared with" each other must be fields stored the same way (both dates,
both numbers, or both the same picklist format), so they can be compared.

If the concept is already expressed by the chosen object itself (qualifier
"internal" when the object is Internal Interview), set implied_by_object true
and field_id null. If no offered field fits, field_id null and search_terms:
other words the field could be called.

For every entry in `relations` choose the path_id whose lookups match how the
question relates that entity ("the recruiter who MANAGES the requirement" ->
the Account Manager lookup). When the question names the record the entity
belongs to ("the recruiter of THE JOB REQUIREMENT"), prefer a lookup ON that
record over a path that detours through objects the question never mentions.
The chosen path decides the entity's object.

Return minified JSON only:
{"slots":[{"id":"S0","field_id":"F1","values":{"<user value>":"<stored>"},
           "implied_by_object":false,"search_terms":[]}],
 "relations":[{"ref":"e1","path_id":"R1"}]}"""


def _norm(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


@dataclass
class Binding:
    """One concept bound to one verified field (a slot after linking)."""
    id: str
    entity: str
    concept: str | None
    object: str
    field: str
    data_type: str | None = None
    label: str | None = None
    values: dict[str, Any] = dataclass_field(default_factory=dict)
    roles: list[str] = dataclass_field(default_factory=list)
    picklist_values: list[str] = dataclass_field(default_factory=list)

    def ref(self) -> GRef:
        return GRef(self.entity, self.field, self.data_type, self.label)

    def for_model(self) -> dict[str, Any]:
        item = {"binding": self.id, "entity": self.entity, "concept": self.concept,
                "field": f"{self.object}.{self.field}", "label": self.label,
                "data_type": self.data_type, "used_as": self.roles}
        if self.values:
            item["stored_values"] = self.values
        if self.picklist_values and normalise(self.data_type) in PICKLIST:
            item["picklist_values"] = self.picklist_values[:20]
        return item


@dataclass
class Slot:
    id: str
    entity: str
    concept: str | None
    date_only: bool = False
    numeric_only: bool = False
    values: list[Any] = dataclass_field(default_factory=list)
    operators: list[str] = dataclass_field(default_factory=list)
    roles: list[str] = dataclass_field(default_factory=list)


@dataclass
class Discovery:
    objects: dict[str, str] = dataclass_field(default_factory=dict)
    fact: str = ""
    candidates: dict[str, list[Any]] = dataclass_field(default_factory=dict)
    failures: list[dict[str, Any]] = dataclass_field(default_factory=list)
    unavailable: str = ""


class IRGrounder:
    def __init__(self, schema: Any, retriever: Any, verifier: Any, *,
                 endpoint: str, model: str, default_date_field: str = "CreatedDate",
                 timeout: float = 60.0, max_tokens: int = 1200,
                 business_notes: dict[str, dict[str, Any]] | None = None,
                 client: Any = None, object_limit: int = 8, field_limit: int = 12,
                 path_limit: int = 8, **_ignored: Any) -> None:
        from pipeline.stage_model import StageModel
        self.schema = schema
        self.retriever = retriever
        self.verifier = verifier
        self.graph = RelationshipGraph(schema)
        self.endpoint, self.model = endpoint, model
        self.default_date_field = default_date_field
        self.max_tokens = max_tokens
        self.notes = business_notes or {}
        self.object_limit, self.field_limit, self.path_limit = \
            object_limit, field_limit, path_limit
        self.stage = StageModel(endpoint, model, timeout=timeout, client=client)
        self.last_error = ""

    @property
    def client(self) -> Any:
        return self.stage.client

    @client.setter
    def client(self, value: Any) -> None:
        self.stage.client = value

    # =====================================================================
    def ground(self, ir: Any, *, log_to: Any = None) -> GroundedQuery:
        """Discovery then linking. The plan itself is the planning stage's."""
        from pipeline.stage_model import StageLog
        log_to = log_to if log_to is not None else StageLog()
        started = time.perf_counter()
        g = GroundedQuery(limit=ir.limit, distinct=ir.distinct,
                          output_mode=ir.output_mode,
                          duplicate_threshold=ir.duplicate_threshold)
        question = ir.question or ""
        if not ir.entities:
            g.fail("NO_OBJECT_CANDIDATES", "the question named no kind of record")
            return g
        ir = self._normalise(ir)
        g.ir = ir

        # -- discovery ----------------------------------------------------------
        concepts: dict[str, list[tuple[str, Any]]] = {}
        for f in ir.filters:
            if f.right.type in ("literal", "set") and f.concept.lower() not in NAME_CONCEPTS:
                concepts.setdefault(f.entity, []).append((f.concept, f.right.value))
        found = self.discover(
            question, [(e.ref, e.concept, e.role) for e in ir.entities], log_to=log_to,
            qualifiers=self._qualifiers(ir), intent=_intent_summary(ir), held=concepts)
        if found.unavailable:
            g.fail("MODEL_UNAVAILABLE", f"discovery: {found.unavailable}")
            return g
        if found.failures:
            g.failures.extend(found.failures)
            return g
        g.objects = dict(found.objects)
        g.base = found.fact

        # -- relationship options (graph) ---------------------------------------
        options = {e.ref: self._path_options(g.objects[g.base], e, ir, g.objects, question)
                   for e in ir.entities if e.ref != g.base}
        for ref, opts in options.items():
            if not opts:
                concept = ir.entity(ref).concept
                g.fail("NO_RELATIONSHIP_PATH",
                       f"no relationship from {g.objects[g.base]} to {g.objects[ref]} "
                       f"for {concept!r}", entity=concept)
        if g.failures:
            return g

        # -- linking --------------------------------------------------------------
        slots = self._slots(ir)
        linked = self.link(question, ir, g.objects, slots, options, log_to=log_to)
        if linked.get("unavailable"):
            g.fail("MODEL_UNAVAILABLE", f"linking: {linked['unavailable']}")
            return g
        g.failures.extend(linked["failures"])
        if g.failures:
            return g
        g.paths = linked["paths"]
        g.objects.update(linked["objects"])
        g.bindings = linked["bindings"]
        g.entities = [{"ref": e.ref, "concept": e.concept, "role": e.role,
                       "object": g.objects[e.ref], "fact": e.ref == g.base,
                       "path": " ".join(h.describe() for h in g.paths.get(e.ref, [])) or None,
                       "reached_as": _reach(g.paths.get(e.ref))}
                      for e in ir.entities]
        stage_calls = [c for c in log_to.calls if c.stage in ("discovery", "linking")]
        g.semantic = {"linker_mode": "ir_grounder_v2", "mode": "model",
                      "model": self.model, "fact_entity": g.base,
                      "model_calls": sum(1 + c.retries for c in stage_calls),
                      "duration_ms": sum(c.duration_ms for c in stage_calls),
                      "object_candidates": sum(len(v) for v in found.candidates.values()),
                      "slots": len(slots), "implied": linked["implied"],
                      "linking_skipped": linked["skipped"],
                      "rejected": linked["rejected"],
                      "duration_total_ms": int((time.perf_counter() - started) * 1000)}
        return g

    # =====================================================================
    # discovery
    def discover(self, question: str, entities: list[tuple[str, str, str]], *,
                 log_to: Any, qualifiers: dict[str, list[str]] | None = None,
                 intent: dict[str, Any] | None = None, need_fact: bool = True,
                 extra_terms: dict[str, list[str]] | None = None,
                 held: dict[str, list[tuple[str, Any]]] | None = None) -> Discovery:
        """Each entity -> one object, by the main model, from candidate IDs.

        `extra_terms` widens retrieval for an entity (more evidence, never a
        choice): e.g. the label of an object one of whose fields is labelled
        exactly as the question's field phrase.
        """
        out = Discovery()
        concepts = [c for _, c, _ in entities]
        for ref, concept, _ in entities:
            pool = self._object_pool(concept, [c for c in concepts if c != concept],
                                     (qualifiers or {}).get(ref, []),
                                     (extra_terms or {}).get(ref, []))
            out.candidates[ref] = pool
            if not pool:
                out.failures.append({"code": "NO_OBJECT_CANDIDATES", "entity": concept,
                                     "detail": f"nothing in the schema matched {concept!r}"})
        if out.failures:
            return out

        self._held_concepts = held or {}
        answer, ids = self._ask_discovery(question, entities, out.candidates, log_to,
                                          qualifiers, intent, need_fact, decided={})
        if answer is None:
            out.unavailable = self.last_error
            return out
        decided = {ref: obj for ref, obj in answer["objects"].items() if obj}
        retry = {ref: terms for ref, terms in answer["search_terms"].items()
                 if ref not in decided and terms}
        if retry:
            # Second pass (§39): the model said what else to look for.
            for ref, terms in retry.items():
                concept = next(c for r, c, _ in entities if r == ref)
                pool = list(out.candidates[ref])
                for term in terms[:4]:
                    for c in self.retriever.object_candidates(term, limit=4):
                        if c.api_name not in {x.api_name for x in pool}:
                            pool.append(c)
                out.candidates[ref] = pool[:self.object_limit + 8]
            again, _ = self._ask_discovery(
                question, [e for e in entities if e[0] in retry], out.candidates, log_to,
                qualifiers, intent, False, decided=decided)
            if again is None:
                out.unavailable = self.last_error
                return out
            decided.update({ref: obj for ref, obj in again["objects"].items() if obj})
        for ref, concept, _ in entities:
            chosen = decided.get(ref)
            if not chosen:
                out.failures.append({"code": "AMBIGUOUS_OBJECT", "entity": concept,
                                     "detail": f"no candidate object fits {concept!r}",
                                     "candidates": [c.api_name for c in out.candidates[ref][:5]]})
                continue
            verified = self.verifier.verify_object(chosen, out.candidates[ref])
            if not verified:
                out.failures.append({"code": "OBJECT_VERIFICATION_FAILED",
                                     "entity": concept, "detail": verified.detail})
                continue
            out.objects[ref] = verified.value
        out.fact = answer.get("fact") or (entities[0][0] if entities else "")
        return out

    def _holds(self, obj: str, concepts: list[tuple[str, Any]]) -> list[str]:
        """Evidence only: fields on `obj` that carry a concept's value or exact label."""
        out: list[str] = []
        for concept, value in concepts[:4]:
            try:
                found = self.retriever.field_candidates(
                    obj, concept, value=value if isinstance(value, (str, list)) else None)[:3]
            except Exception:                           # noqa: BLE001
                continue
            for c in found:
                if c.matched_picklist_value or _norm(c.label) == _norm(concept):
                    tag = f"{c.label}" + (f" (has {c.matched_picklist_value!r})"
                                          if c.matched_picklist_value else "")
                    if tag not in out:
                        out.append(tag)
        return out[:3]

    def _object_pool(self, concept: str, others: list[str], qualifiers: list[str],
                     extra_terms: list[str] = ()) -> list[Any]:
        """High recall: the concept's candidates, plus objects a qualifier names."""
        pool: list[Any] = []
        seen: set[str] = set()
        for term in [f"{word} {concept}" for word in qualifiers] + list(extra_terms):
            for c in self.retriever.object_candidates(term, limit=3):
                if c.api_name not in seen:
                    seen.add(c.api_name)
                    pool.append(c)
        for c in self.retriever.object_candidates(concept, related_entities=others,
                                                  limit=self.object_limit):
            if c.api_name not in seen:
                seen.add(c.api_name)
                pool.append(c)
        return pool[:self.object_limit + 3]

    def _ask_discovery(self, question: str, entities: list[tuple[str, str, str]],
                       candidates: dict[str, list[Any]], log_to: Any,
                       qualifiers: dict[str, list[str]] | None,
                       intent: dict[str, Any] | None, need_fact: bool,
                       decided: dict[str, str]) -> tuple[dict[str, Any] | None, dict]:
        ids: dict[str, dict[str, str]] = {}
        payload_entities = []
        counter = 0
        for ref, concept, role in entities:
            ids[ref] = {}
            listed = []
            for c in candidates[ref]:
                counter += 1
                oid = f"O{counter}"
                ids[ref][oid] = c.api_name
                item = {"id": oid, "api_name": c.api_name, "label": c.label,
                        "evidence": c.evidence[:4]}
                if c.aliases:
                    item["aliases"] = c.aliases[:4]
                if getattr(c, "description", None):
                    item["description"] = str(c.description)[:160]
                k = self._knowledge("object", c.api_name)
                if k:
                    item["business_knowledge"] = k
                holds = self._holds(c.api_name, (getattr(self, "_held_concepts", None) or {}).get(ref, []))
                if holds:
                    item["holds"] = holds
                listed.append(item)
            entry = {"ref": ref, "concept": concept, "role": role, "candidates": listed}
            if qualifiers and qualifiers.get(ref):
                entry["qualifiers"] = qualifiers[ref]
            payload_entities.append(entry)
        payload: dict[str, Any] = {"question": question, "entities": payload_entities}
        if intent:
            payload["intent"] = intent
        if decided:
            payload["already_decided"] = decided
        refs = [ref for ref, _, _ in entities]

        def validate(answer: Any) -> str:
            got = {i.get("ref"): i for i in answer.get("entities") or [] if isinstance(i, dict)}
            missing = [r for r in refs if r not in got]
            if missing:
                return f"answer every entity ref; missing {missing}"
            for ref, item in got.items():
                oid = item.get("object_id")
                if ref in ids and oid is not None and oid not in ids[ref]:
                    return f"{ref}: {oid!r} is not one of its candidate ids {sorted(ids[ref])}"
            if need_fact and answer.get("fact_entity") not in refs:
                return f"fact_entity must be one of {refs}"
            checked = {x.split()[-1] for x in (intent or {}).get("existence", [])}
            if need_fact and answer.get("fact_entity") in checked:
                return (f"{answer.get('fact_entity')} is checked for existence in the "
                        "intent, so it cannot be the fact entity; the fact is the entity "
                        "that has (or lacks) it")
            return ""

        roles = {ref: role for ref, _, role in entities}

        def soft(answer: Any) -> str:
            # Worth one retry, never a failure: the model decides again.
            fact = answer.get("fact_entity")
            if need_fact and (intent or {}).get("output_mode") == "exists" \
                    and roles.get(fact) == "related":
                return (f"output_mode is exists and {fact} is a related entity: in "
                        "'does X have Y' the fact is usually X, the entity asked about, "
                        "with Y checked for existence. Re-check fact_entity")
            return ""

        answer, call = self.stage.call(
            "discovery", [{"role": "system", "content": DISCOVERY_SYSTEM},
                          {"role": "user", "content": json.dumps(payload, ensure_ascii=False,
                                                                 default=str)}],
            log_to=log_to, validate=validate, soft=soft, max_tokens=self.max_tokens,
            candidate_count=counter)
        if answer is None:
            self.last_error = call.error
            if call.failure == "invalid":
                # The model answered but never usably: no object is decided.
                return {"objects": {}, "search_terms": {}, "fact": None}, ids
            return None, ids
        objects, terms = {}, {}
        for item in answer.get("entities") or []:
            ref = item.get("ref")
            if ref not in ids:
                continue
            objects[ref] = ids[ref].get(item.get("object_id"))
            terms[ref] = [str(t) for t in item.get("search_terms") or [] if str(t).strip()]
        call.selected = {"objects": objects, "fact_entity": answer.get("fact_entity")}
        return {"objects": objects, "search_terms": terms,
                "fact": answer.get("fact_entity")}, ids

    # =====================================================================
    # relationship options
    def _path_options(self, base_obj: str, e: Any, ir: Any, objects: dict[str, str],
                      question: str) -> list[list[Hop]]:
        """Every acceptable way from the fact object to this entity's object.

        All of them go to the model; none is chosen here. A returned entity is
        shown per row, so only parent paths count for it; when the question
        ties it to an intermediate entity, that entity's own lookups are
        offered too, whatever object they reach ("the recruiter who MANAGES the
        requirement" may be a User lookup).
        """
        target = objects[e.ref]
        returned = e.role == "returned"
        via = [objects[x.ref] for x in ir.entities
               if x.role == "related" and x.ref != e.ref and objects.get(x.ref)
               and objects[x.ref] not in (target, base_obj)] if returned else None
        paths = self.graph.paths(base_obj, target, context=question, via=via or None,
                                 allow_child=not returned, limit=self.path_limit)
        if not paths and via:
            paths = self.graph.paths(base_obj, target, context=question,
                                     allow_child=not returned, limit=self.path_limit)
        options = [p.hops for p in paths]
        own: list[list[Hop]] = []
        if returned and (via or not paths):
            start = via[-1] if via else base_obj
            to_start = self.graph.paths(base_obj, start) if start != base_obj else []
            prefix = to_start[0].hops if to_start else []
            if start == base_obj or prefix:
                for hop in self.graph.lookups_from(start):
                    own.append(prefix + [hop])
        # The named intermediate's own lookups come first: listed after
        # multi-object detours they lost to them (JS-00014). Order only.
        options = own + options
        unique: list[list[Hop]] = []
        seen: set[str] = set()
        for hops in options:
            key = " ".join(h.describe() for h in hops)
            if key not in seen:
                seen.add(key)
                unique.append(hops)
        return unique[:15]

    # =====================================================================
    # slots
    def _slots(self, ir: Any) -> list[Slot]:
        """Every concept that must become a field, once per (entity, concept, kind)."""
        slots: dict[tuple[str, str, bool], Slot] = {}

        def slot(entity: str, concept: str | None, role: str, *, value: Any = None,
                 operator: str | None = None, date_only: bool = False,
                 numeric_only: bool = False) -> None:
            key = (entity, _norm(concept), date_only)
            s = slots.get(key)
            if s is None:
                s = slots[key] = Slot(id=f"S{len(slots)}", entity=entity, concept=concept,
                                      date_only=date_only)
            s.numeric_only = s.numeric_only or numeric_only
            if role not in s.roles:
                s.roles.append(role)
            if operator and operator not in s.operators:
                s.operators.append(operator)
            for v in (value if isinstance(value, list) else [value]):
                if v is not None and not isinstance(v, (dict,)) and v not in s.values:
                    s.values.append(v)

        def filters(items: list[Any], where: str) -> None:
            for f in items:
                value = f.right.value if f.right.type in ("literal", "boolean", "set") else None
                compared = (f" compared with {f.right.concept!r}"
                            if f.right.type == "field_reference" and f.right.concept else "")
                slot(f.entity, f.concept, f"{where}: {f.operator}"
                     + (f" {value!r}" if value is not None else "") + compared,
                     value=value, operator=f.operator)
                if f.right.type == "field_reference" and f.right.concept:
                    slot(f.right.entity or f.entity, f.right.concept,
                         f"{where}: compared with {f.concept!r}")

        filters(ir.filters, "filter")
        for m in ir.measures:
            if m.concept:
                slot(m.entity, m.concept, f"measure {m.op}",
                     numeric_only=m.op in ("sum", "avg"))
        for d in ir.dimensions:
            slot(d.entity, d.concept, "dimension" + (f" by {d.grain}" if d.grain else ""),
                 date_only=bool(d.grain))
        for o in ir.ordering:
            if o.target == "date":
                slot(o.entity or ir.primary.ref, o.concept, "ordering by date", date_only=True)
            elif o.target == "attribute" and o.concept:
                slot(o.entity or ir.primary.ref, o.concept, "ordering")
        for a in ir.attributes:
            slot(a["entity"], a["concept"], "shown attribute")
        for t in ir.temporal:
            slot(t.entity, t.concept, f"period {t.expression!r}", date_only=True)
        for seg in ir.comparison:
            filters(seg.filters, f"segment {seg.label!r}")
            if seg.temporal:
                slot(seg.temporal.entity, seg.temporal.concept,
                     f"segment {seg.label!r} period {seg.temporal.expression!r}",
                     date_only=True)
        for d in ir.derived:
            filters(d.numerator, f"{d.kind} numerator")
            filters(d.denominator, f"{d.kind} denominator")
        for x in ir.existence:
            filters(x.filters, f"{x.mode} condition")
        return list(slots.values())

    def _field_pool(self, obj: str, s: Slot, ir: Any, extra_terms: list[str] = ()
                    ) -> list[Any]:
        """High-recall field candidates for one slot on one object.

        Platform fields the concept names (Name, CreatedDate, LastModifiedDate)
        come first as evidence; retrieval's candidates follow, so the model
        always has the alternatives. Only types that cannot serve the slot are
        removed: a period needs a date, an average needs a number (§10).
        """
        concept = s.concept
        pool: list[Any] = []
        seen: set[str] = set()

        def add(items: list[Any]) -> None:
            for c in items:
                if c.api_name in seen:
                    continue
                dtype = normalise(c.data_type)
                if s.date_only and dtype not in TEMPORAL:
                    continue
                if s.numeric_only and dtype not in NUMERIC:
                    continue
                seen.add(c.api_name)
                pool.append(c)

        entity_concept = _norm(ir.entity(s.entity).concept) if ir and ir.entity(s.entity) else ""
        if s.date_only and (concept is None or _norm(concept) in CREATED):
            add(self._named(obj, "CreatedDate"))
        if s.date_only and _norm(concept) in MODIFIED:
            add(self._named(obj, "LastModifiedDate"))
        if concept and (_norm(concept) in NAME_CONCEPTS or _norm(concept) == entity_concept):
            add(self._named(obj, "Name"))
        value = s.values[0] if s.values else None
        operator = s.operators[0] if s.operators else None
        terms = [concept or "date"] + list(extra_terms)
        for term in terms:
            add(self.retriever.field_candidates(obj, term, value=value, operator=operator,
                                                limit=20 if s.date_only or s.numeric_only
                                                else self.field_limit))
        if s.date_only:
            phrases = ([f"{concept} date"] if concept else []) + \
                      ([f"{entity_concept} date"] if entity_concept else [])
            for phrase in phrases:
                add(self.retriever.field_candidates(obj, phrase, limit=20))
            add(self._named(obj, "CreatedDate"))
            add(self._named(obj, "LastModifiedDate"))
        # Every other value the slot carries: its own picklist evidence.
        for other in s.values[1:4]:
            add([c for c in self.retriever.field_candidates(obj, concept or "", value=other,
                                                            operator=operator, limit=6)
                 if c.matched_picklist_value])
        return pool[:self.field_limit + 4]

    # =====================================================================
    # linking
    def link(self, question: str, ir: Any, objects: dict[str, str], slots: list[Slot],
             options: dict[str, list[list[Hop]]], *, log_to: Any) -> dict[str, Any]:
        """Each slot -> a field, each non-fact entity -> a path. Main model, by ID."""
        result: dict[str, Any] = {"bindings": {}, "paths": {}, "objects": {},
                                  "failures": [], "implied": [], "rejected": [],
                                  "skipped": False}
        if not slots and not options:
            # Nothing to link: no concept names a field and no other entity
            # needs a path ("how many interviews"). The stage does not apply;
            # it is reported as not applicable, never as decided by code.
            result["skipped"] = True
            return result
        # Fields are offered on the entity's object and on every object a
        # relationship option could make it (a chosen lookup may reach User).
        pools: dict[str, dict[str, list[Any]]] = {}
        for s in slots:
            owners = [objects[s.entity]] + [hops[-1].target for hops in options.get(s.entity, [])]
            pools[s.id] = {}
            for obj in dict.fromkeys(owners):
                found = self._field_pool(obj, s, ir)
                if found:
                    pools[s.id][obj] = found
        answer = self._ask_linking(question, ir, objects, slots, pools, options, log_to,
                                   result)
        if answer is None:
            result["unavailable"] = self.last_error
            return result
        if result["failures"]:
            return result
        chosen, relations = answer
        terms = chosen.pop("__terms__", {})

        # Second pass for slots the model said were named otherwise (§39).
        retry = [s for s in slots if s.id not in chosen and terms.get(s.id)]
        if retry:
            for s in retry:
                owner = relations[s.entity][-1].target if relations.get(s.entity)                     else objects[s.entity]
                more = self._field_pool(owner, s, ir, extra_terms=terms[s.id][:4])
                pools[s.id] = {owner: more} if more else {}
            again = self._ask_linking(question, ir, objects, retry, pools,
                                      {}, log_to, result, decided_relations=relations)
            if again is None:
                result["unavailable"] = self.last_error
                return result
            if result["failures"]:
                return result
            again[0].pop("__terms__", None)
            chosen.update(again[0])

        result["paths"] = {ref: hops for ref, hops in relations.items()}
        for ref, hops in relations.items():
            if hops and hops[-1].target != objects[ref]:
                result["objects"][ref] = hops[-1].target
        final = {**objects, **result["objects"]}
        for s in slots:
            pick = chosen.get(s.id)
            if pick == "implied":
                result["implied"].append(f"{s.entity}: {s.concept}")
                continue
            owner = final[s.entity]
            if not pick:
                result["failures"].append({
                    "code": "AMBIGUOUS_FIELD", "concept": s.concept,
                    "detail": f"no field on {owner} fits {s.concept or 'the date'!r}",
                    "candidates": [c.api_name for c in pools[s.id].get(owner, [])[:5]]})
                continue
            obj, field, values = pick
            offered = pools[s.id].get(obj, [])
            verified = self.verifier.verify_field(obj, field, offered)
            if not verified or obj != owner:
                result["failures"].append({
                    "code": "FIELD_VERIFICATION_FAILED", "concept": s.concept,
                    "detail": verified.detail if not verified else
                    f"{obj}.{field} is not on {owner}, the object {s.entity} resolved to"})
                continue
            row = self.schema.repo.get_field(obj, verified.value) or {}
            candidate = next((c for c in offered if c.api_name == verified.value), None)
            result["bindings"][s.id] = Binding(
                id=s.id, entity=s.entity, concept=s.concept, object=obj,
                field=verified.value, data_type=row.get("data_type"),
                label=row.get("label"), values=values, roles=list(s.roles),
                picklist_values=list(getattr(candidate, "picklist_values", []) or []))
        return result

    def _ask_linking(self, question: str, ir: Any, objects: dict[str, str],
                     slots: list[Slot], pools: dict[str, dict[str, list[Any]]],
                     options: dict[str, list[list[Hop]]], log_to: Any,
                     result: dict[str, Any], decided_relations: dict | None = None
                     ) -> tuple[dict[str, Any], dict[str, list[Hop]]] | None:
        field_ids: dict[str, dict[str, tuple[str, Any]]] = {}
        path_ids: dict[str, dict[str, list[Hop]]] = {}
        counter = 0
        payload_slots = []
        for s in slots:
            field_ids[s.id] = {}
            listed = []
            for obj, found in pools[s.id].items():
                for c in found:
                    counter += 1
                    fid = f"F{counter}"
                    field_ids[s.id][fid] = (obj, c)
                    item = {"id": fid, "object": obj, "api_name": c.api_name,
                            "label": c.label, "data_type": c.data_type,
                            "evidence": c.evidence[:4]}
                    if c.picklist_values:
                        item["picklist_values"] = c.picklist_values[:15]
                    if c.matched_picklist_value:
                        item["matched_value"] = c.matched_picklist_value
                    if re.search(r"do not use|deprecated|obsolete|\(old\)|legacy",
                                 str(c.label or ""), re.I):
                        item["deprecated"] = True
                    k = self._knowledge("field", f"{obj}.{c.api_name}")
                    if k:
                        item["business_knowledge"] = k
                    listed.append(item)
            entity = ir.entity(s.entity) if ir is not None else None
            entry = {"id": s.id, "entity": s.entity,
                     "entity_concept": entity.concept if entity else s.entity,
                     "object": objects.get(s.entity), "concept": s.concept,
                     "used_as": s.roles, "candidates": listed}
            if s.values:
                entry["values"] = s.values
            payload_slots.append(entry)
        relations_payload = []
        rcount = 0
        for ref, opts in options.items():
            path_ids[ref] = {}
            listed = []
            for hops in opts:
                rcount += 1
                rid = f"R{rcount}"
                path_ids[ref][rid] = hops
                listed.append({"id": rid, "path": " ".join(h.describe() for h in hops),
                               "field_label": hops[-1].label if hops else None,
                               "reaches": hops[-1].target if hops else objects.get(ref),
                               "joins_many": any(h.direction == "child" for h in hops)})
            entity = ir.entity(ref)
            relations_payload.append({"ref": ref, "entity": entity.concept if entity else ref,
                                      "role": entity.role if entity else None,
                                      "options": listed})
        payload: dict[str, Any] = {"question": question,
                                   "objects": {ref: obj for ref, obj in objects.items()},
                                   "slots": payload_slots}
        if relations_payload:
            payload["relations"] = relations_payload
        slot_ids = [s.id for s in slots]

        def owner_after(ref: str, rel_answer: dict[str, str]) -> str:
            rid = rel_answer.get(ref)
            hops = path_ids.get(ref, {}).get(rid) if rid else None
            if hops:
                return hops[-1].target
            if decided_relations and decided_relations.get(ref):
                return decided_relations[ref][-1].target
            return objects[ref]

        def validate(answer: Any) -> str:
            got = {i.get("id"): i for i in answer.get("slots") or [] if isinstance(i, dict)}
            missing = [sid for sid in slot_ids if sid not in got]
            if missing:
                return f"answer every slot; missing {missing}"
            rel = {i.get("ref"): i.get("path_id") for i in answer.get("relations") or []
                   if isinstance(i, dict)}
            for ref in path_ids:
                if rel.get(ref) not in path_ids[ref]:
                    return (f"relations: choose a path_id for {ref} from "
                            f"{sorted(path_ids[ref])}")
            for s in slots:
                item = got[s.id]
                fid = item.get("field_id")
                if fid is None:
                    continue
                if fid not in field_ids[s.id]:
                    return f"{s.id}: {fid!r} is not one of its candidate ids"
                obj, c = field_ids[s.id][fid]
                owner = owner_after(s.entity, rel)
                if obj != owner:
                    return (f"{s.id}: {fid} is a field of {obj}, but entity {s.entity} is "
                            f"{owner}; choose a field of {owner}")
                dtype = normalise(c.data_type)
                values = item.get("values") if isinstance(item.get("values"), dict) else {}
                for raw in s.values:
                    stored = values.get(str(raw), values.get(raw) if isinstance(raw, str) else None)
                    if stored is None and isinstance(raw, bool):
                        stored = raw
                    if dtype in PICKLIST and stored is not None and not isinstance(stored, bool) \
                            and self.verifier.verify_picklist_value(obj, c.api_name, stored) is None:
                        return (f"{s.id}: {stored!r} is not a value of {c.api_name}; "
                                f"its values are {c.picklist_values[:15]}")
            return ""

        pairs = [(a.id, b.id) for a in slots for b in slots if a is not b
                 and any(r.endswith(f"compared with {b.concept!r}") for r in a.roles)]

        def soft(answer: Any) -> str:
            got = {i.get("id"): i.get("field_id") for i in answer.get("slots") or []
                   if isinstance(i, dict)}
            for left, right in pairs:
                lf, rf = field_ids[left].get(got.get(left)), field_ids[right].get(got.get(right))
                if lf and rf:
                    lt, rt = normalise(lf[1].data_type), normalise(rf[1].data_type)
                    same_family = (lt == rt or {lt, rt} <= TEMPORAL or {lt, rt} <= NUMERIC)
                    if not same_family:
                        return (f"{left} ({lf[1].label}, {lf[1].data_type}) and {right} "
                                f"({rf[1].label}, {rf[1].data_type}) are compared but stored "
                                "differently; choose a matching pair")
            return ""

        answer, call = self.stage.call(
            "linking", [{"role": "system", "content": LINKING_SYSTEM},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False,
                                                               default=str)}],
            log_to=log_to, validate=validate, soft=soft, max_tokens=self.max_tokens,
            candidate_count=counter + rcount)
        if answer is None:
            self.last_error = call.error
            if call.failure == "invalid":
                result["failures"].append({"code": "LINKING_INVALID",
                                           "detail": f"linking output unusable: {call.error}"})
                return {}, {}
            return None
        chosen: dict[str, Any] = {}
        terms: dict[str, list[str]] = {}
        for item in answer.get("slots") or []:
            sid = item.get("id")
            if sid not in field_ids:
                continue
            if item.get("implied_by_object") and not item.get("field_id"):
                chosen[sid] = "implied"
                continue
            fid = item.get("field_id")
            if fid in field_ids[sid]:
                obj, c = field_ids[sid][fid]
                values = item.get("values") if isinstance(item.get("values"), dict) else {}
                chosen[sid] = (obj, c.api_name, {str(k): v for k, v in values.items()})
            elif item.get("search_terms"):
                terms[sid] = [str(t) for t in item["search_terms"] if str(t).strip()]
        relations: dict[str, list[Hop]] = dict(decided_relations or {})
        for item in answer.get("relations") or []:
            ref, rid = item.get("ref"), item.get("path_id")
            if ref in path_ids and rid in path_ids[ref]:
                relations[ref] = path_ids[ref][rid]
        call.selected = {"fields": {sid: (f"{v[0]}.{v[1]}" if isinstance(v, tuple) else v)
                                    for sid, v in chosen.items()},
                         "relations": {ref: " ".join(h.describe() for h in hops)
                                       for ref, hops in relations.items()}}
        chosen["__terms__"] = terms
        return chosen, relations

    # =====================================================================
    # helpers
    def _qualifiers(self, ir: Any) -> dict[str, list[str]]:
        """Words that may name a more specific object ("internal" interviews).

        Evidence for discovery only: the filter stays in the IR, and linking
        says whether the chosen object already implies it.
        """
        out: dict[str, list[str]] = {}
        for f in ir.filters:
            if f.right.type == "field_reference":
                continue
            word = None
            if f.right.type == "boolean" and f.right.value is True and \
                    len(_norm(f.concept).split()) == 1:
                word = f.concept
            elif f.right.type == "literal" and isinstance(f.right.value, str) and \
                    _norm(f.concept).split()[-1:] in (["type"], ["kind"], ["category"]):
                word = f.right.value
            if word:
                out.setdefault(f.entity, []).append(_norm(word))
        return out

    def _knowledge(self, kind: str, target: str) -> dict[str, Any] | None:
        entry = self.notes.get(f"{kind}:{target}")
        if not entry:
            return None
        out = {"terms": entry["terms"][:6]}
        if entry.get("notes"):
            out["note"] = entry["notes"][0]
        return out

    def _named(self, obj: str, field: str) -> list[Any]:
        """A platform field, packaged as a candidate with exact evidence."""
        row = self.schema.repo.get_field(obj, field)
        if row is None:
            return []
        from .models import Evidence, FieldCandidate
        return [FieldCandidate(object_api_name=obj, api_name=field, label=row.get("label"),
                               data_type=row.get("data_type"), retrieval_score=1.0,
                               evidence=[Evidence.EXACT_API.value])]

    # =====================================================================
    # IR normalisation
    def _normalise(self, ir: Any) -> Any:
        """Readings of the IR that the SCHEMA settles, applied before grounding.

        Each is a rule about how business words meet this org's objects, not a
        rule about any one question. The IR is copied; the traced one is not
        changed.
        """
        import copy
        ir = copy.deepcopy(ir)
        primary = ir.primary

        # 1. (A qualifier that names an object together with its entity is no
        #    longer rewritten here: discovery is offered that object and the
        #    main model decides; see _qualifiers.)

        # 1b. A time bucket named only by its unit ("quarterly", "by month")
        #     buckets the same date the period restricts -- CreatedDate unless
        #     the question named another. Two different dates in one trend
        #     answered neither question. Seen live.
        generic = {"date", "time", "period", "day", "week", "month", "quarter", "year",
                   "days", "weeks", "months", "quarters", "years", "daily", "weekly",
                   "monthly", "quarterly", "yearly", "annual"}
        for d in ir.dimensions:
            if d.grain and (not d.concept or _norm(d.concept) in generic):
                same = next((t.concept for t in ir.temporal
                             if t.entity == d.entity and t.concept), None)
                d.concept = same or "created"

        question = _norm(ir.question)
        concept_of = {e.ref: _norm(e.concept) for e in ir.entities}

        # 1c. A bare attribute word the question qualified with its entity
        #     ("by interview status" -> concept "status"): the whole phrase is
        #     the concept, so "Interview Status" wins over "Client Feedback
        #     Status". Seen live.
        def qualify(entity: str, concept: str | None) -> str | None:
            if not concept or len(_norm(concept).split()) != 1:
                return concept
            phrase = f"{concept_of.get(entity, '')} {_norm(concept)}".strip()
            return phrase if phrase in question else concept
        for d in ir.dimensions:
            d.concept = qualify(d.entity, d.concept)
        for f in ir.filters:
            f.concept = qualify(f.entity, f.concept)
        for a in ir.attributes:
            a["concept"] = qualify(a["entity"], a["concept"])

        # 1d. "created this month vs last month": "created" is WHICH date the
        #     period applies to, not a yes/no fact about the record.
        # 1d'. A date filter whose value is a period ("created = this month")
        #      IS the period.
        from pipeline.ir import Temporal
        # "joining date >= this year" -> "since this year".
        bound_words = {"greater_than_or_equal": "since", "on_or_after": "since",
                       "after": "after", "greater_than": "after",
                       "less_than": "before", "before": "before",
                       "less_than_or_equal": "until", "on_or_before": "until"}

        def period_filter(f: Any) -> bool:
            text = f.right.value or f.right.temporal
            if f.right.type not in ("literal", "temporal") or not isinstance(text, str) \
                    or parse_period(text) is None:
                return False
            if f.operator in bound_words:
                f.right.value, f.right.temporal = f"{bound_words[f.operator]} {text}", None
                f.operator = "equals"
                return parse_period(f.right.value) is not None
            return f.operator in ("equals", "between")
        for seg in ir.comparison:
            for f in [f for f in seg.filters if period_filter(f)]:
                seg.filters.remove(f)
                if seg.temporal is None:
                    seg.temporal = Temporal(f.right.value or f.right.temporal, f.entity,
                                            f.concept, "segment")
                elif not seg.temporal.concept:
                    seg.temporal.concept = f.concept
        for f in [f for f in ir.filters if period_filter(f)]:
            ir.filters.remove(f)
            ir.temporal.append(Temporal(f.right.value or f.right.temporal, f.entity, f.concept))
        for seg in ir.comparison:
            for f in list(seg.filters):
                if f.right.type == "boolean" and _norm(f.concept) in CREATED | MODIFIED:
                    seg.filters.remove(f)
                    if seg.temporal and not seg.temporal.concept:
                        seg.temporal.concept = f.concept
        # A comparison's periods belong to its segments. Repeated at top level
        # they AND together ("this month" AND "last month") and match nothing.
        segment_periods = {_norm(seg.temporal.expression) for seg in ir.comparison
                           if seg.temporal}
        ir.temporal = [t for t in ir.temporal if _norm(t.expression) not in segment_periods]
        for f in list(ir.filters):
            if f.right.type == "boolean" and _norm(f.concept) in CREATED | MODIFIED:
                ir.filters.remove(f)
                for t in ir.temporal:
                    if t.entity == f.entity and not t.concept:
                        t.concept = f.concept
                for seg in ir.comparison:
                    if seg.temporal and not seg.temporal.concept:
                        seg.temporal.concept = f.concept

        # 1e. A join condition written as a filter ("interview.candidate =
        #     candidate.name"): how two entities relate comes from the schema's
        #     relationships, never from a field comparison across entities.
        def is_join(f: Any) -> bool:
            return (f.right.type == "field_reference" and f.right.entity
                    and f.right.entity != f.entity
                    and (_norm(f.concept) in concept_of.values()
                         or _norm(f.right.concept or "") in concept_of.values()
                         or _norm(f.right.concept or "") in NAME_CONCEPTS))
        ir.filters = [f for f in ir.filters if not is_join(f)]
        for x in ir.existence:
            x.filters = [f for f in x.filters if not is_join(f)]

        # 1f. "recruiters with marketing records" written as a filter
        #     "exists"/"is not null" on the related entity: an existence.
        from pipeline.ir import Entity, Existence
        for f in list(ir.filters):
            if _norm(f.concept) in ("exists", "exist", "any", "present", "existence") \
                    and primary and f.entity != primary.ref:
                ir.filters.remove(f)
                negative = (f.operator == "is_null") != bool(f.negate)
                ir.existence.append(Existence("not_exists" if negative else "exists", f.entity))
        # 1g. An existence on the record itself ("interviews with no recruiter"
        #     as "not exists interview where recruiter is empty"): its filters
        #     ARE the condition.
        for x in list(ir.existence):
            if primary and x.entity == primary.ref:
                ir.existence.remove(x)
                ir.filters += x.filters
        # 1h. "Top candidates by number of job submissions": a count of
        #     another KIND of record, named where a field would be. When the
        #     org has an object by that name and it is not yet an entity, the
        #     counted records are the base and the ranked kind is returned.
        for m in ir.measures:
            if m.op == "count" and m.concept and primary and m.entity == primary.ref \
                    and _norm(m.concept) not in {_norm(e.concept) for e in ir.entities} \
                    and self._object_named(_norm(m.concept)):
                ref = f"e{len(ir.entities)}"
                ir.entities.append(Entity(ref, _norm(m.concept).rstrip("s"), "primary"))
                primary.role = "returned"
                m.entity, m.concept = ref, None
                for o in ir.ordering:
                    if o.target.startswith("measure") and o.entity == primary.ref:
                        o.entity = ref
                primary = ir.primary
                break

        concepts = {_norm(e.concept): e.ref for e in ir.entities}
        for m in ir.measures:
            # 2. count_distinct of a concept that IS an entity ("unique
            #    candidates", "different recruiters"): distinct records of it.
            if m.op == "count_distinct" and m.concept and _norm(m.concept) in concepts:
                m.entity, m.concept = concepts[_norm(m.concept)], None
            # 3. An attribute named with another entity's word, on the primary
            #    ("candidate rate on job submissions"): the primary's own field
            #    when one is labelled exactly that.
            elif m.op in ("sum", "avg", "min", "max") and m.concept and primary \
                    and m.entity != primary.ref:
                owner = ir.entity(m.entity)
                phrase = _norm(f"{owner.concept} {m.concept}") if owner else ""
                if phrase and self._field_named(primary.concept, phrase):
                    m.entity, m.concept = primary.ref, phrase

        # 4. A related entity nothing refers to, whose word only restates part
        #    of another concept ("average number of openings" + entity
        #    "opening"): not an entity.
        used = {f.entity for f in ir.filters} | {m.entity for m in ir.measures} | \
            {d.entity for d in ir.dimensions} | {a["entity"] for a in ir.attributes} | \
            {t.entity for t in ir.temporal} | {x.entity for x in ir.existence} | \
            {o.entity for o in ir.ordering if o.entity}
        words = " ".join([_norm(m.concept) for m in ir.measures if m.concept] +
                         [_norm(a["concept"]) for a in ir.attributes] +
                         [_norm(f.concept) for f in ir.filters])
        if not any(e.role == "returned" for e in ir.entities):
            ir.entities = [e for e in ir.entities if not (
                e.role == "related" and e.ref not in used
                and _norm(e.concept).rstrip("s") and _norm(e.concept).rstrip("s") in words)]
        return ir

    def _object_named(self, phrase: str) -> bool:
        """An object whose label or plural label IS the phrase."""
        return any(self.retriever.object_candidates_by_name(p)
                   for p in {phrase, phrase + "s", phrase.rstrip("s")})

    def _field_named(self, entity_concept: str, phrase: str) -> bool:
        objects = self.retriever.object_candidates(entity_concept)[:1]
        if not objects:
            return False
        fields = self.retriever.field_candidates(objects[0].api_name, phrase)[:1]
        return bool(fields) and _norm(fields[0].label) == phrase


def _intent_summary(ir: Any) -> dict[str, Any]:
    """What discovery needs of the intent to choose the fact entity."""
    out: dict[str, Any] = {"output_mode": ir.output_mode}
    if ir.measures:
        out["measures"] = [f"{m.op}({m.entity}{'.' + m.concept if m.concept else ''})"
                           for m in ir.measures]
    if ir.dimensions:
        out["dimensions"] = [f"{d.entity}.{d.concept}" for d in ir.dimensions]
    if ir.existence:
        out["existence"] = [f"{x.mode} {x.entity}" for x in ir.existence]
    return out


def _reach(hops: list[Hop] | None) -> str | None:
    if not hops:
        return None
    return "many (child)" if any(h.direction == "child" for h in hops) else "one (parent)"


def boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = _norm(value)
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    return None


def comparable(schema: Any, objects: dict[str, str], left: GRef, right: GRef) -> str | None:
    """How two fields compare, or None if they cannot.

    Same family compares directly. Two picklist/text fields whose stored
    values are clock times ("08:30 AM") compare AS TIMES -- as text,
    "10:00 AM" sorts before "09:00 AM" and every answer is wrong.
    """
    a, b = normalise(left.data_type), normalise(right.data_type)
    if a in TEMPORAL and b in TEMPORAL:
        return "direct"
    if a in NUMERIC and b in NUMERIC:
        return "direct"
    if a == b == "time":
        return "direct"

    def time_like(ref: GRef) -> bool:
        values = [row["value"] for row in schema.repo.get_picklist_values(
            objects[ref.entity], ref.field)]
        return bool(values) and all(TIME_VALUE.match(str(v)) for v in values)

    if a in PICKLIST | {"text"} and b in PICKLIST | {"text"}:
        if time_like(left) and time_like(right):
            return "time"
        return "direct" if a == b else None
    return None


def alias(text: str | None) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in str(text or "value").lower())
    return re.sub(r"_+", "_", cleaned).strip("_") or "value"
