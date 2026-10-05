"""One semantic decision per question, over a compact candidate package.

    deterministic retrieval  ->  conclusive?  ->  yes: accept, no model call
                                              ->  no:  ONE main-model call
                             ->  closed-set check  ->  runtime-schema verification

Retrieval turns 7,586 fields into a few dozen candidates. The model never sees
the schema, only those candidates, and may only choose among them: any name it
returns that was not offered is rejected here, before the verifier ever runs.

One call per question, not one per concept. The object, every filter's field,
every shown attribute and the date field are decided together, because they
are not independent: "offer received" is Interview_Outcome__c only once the
object is Interview__c, and "status" means something different on Account.
Deciding them in isolation is how "status" got linked as a token instead of as
part of a question.

The fast path exists because most questions do not need a model at all. An
exact label, a hand-reviewed business alias, or a picklist value that exists
on exactly one field is a fact; asking a model to confirm a fact costs ~1 s
and adds a way to be wrong.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field as dataclass_field
from typing import Any

from .models import Evidence, FieldCandidate, ObjectCandidate, Selection
from .retriever import normalise

log = logging.getLogger(__name__)

# Evidence that is a fact rather than a similarity.
STRONG = {Evidence.EXACT_API.value, Evidence.EXACT_LABEL.value,
          Evidence.MANUAL_ALIAS.value}
TEMPORAL_TYPES = {"date", "datetime"}

SYSTEM = """You map ONE Salesforce question onto the org's schema.

Choose ONLY from the candidates given. Never invent an object or field name;
if nothing fits, answer null with confidence 0. The whole question is the
context for every choice: a word like "status" means different fields on
different objects, and a value the user gave ("Offer Received") is strong
evidence for the field whose picklist contains it.

`business_knowledge` on a candidate is hand-reviewed org vocabulary: it states
what the team means by a term. It OUTRANKS a literal label match. When a
candidate's business_knowledge terms or note cover the question's words, choose
it, even if another candidate's label repeats a word from the question
("status") more literally. Evidence `manual_alias_match` means the same thing.

primary_object is the object whose records are counted, listed or filtered --
the object the filters' fields live on. It may differ from the entity the user
named: "candidates who got an offer" filters interviews by outcome, so the
records are interviews, reached from the candidate.

For a picklist field, echo the stored value EXACTLY as spelled in
picklist_values. For a checkbox field, value is true or false.

Return only minified JSON (no indentation, no line breaks):
{"entities": [{"index": <n>, "name": "<entity>", "object": "<api or null>", "confidence": 0-1,
               "evidence": [<labels>]}],
 "primary_object": "<api>",
 "filters": [{"index": <n>, "object": "<api>", "field": "<api or null>",
              "value": <exact value>, "confidence": 0-1, "evidence": [<labels>]}],
 "attributes": [{"index": <n>, "object": "<api>", "field": "<api or null>",
                 "confidence": 0-1}],
 "date_field": {"object": "<api>", "field": "<api or null>", "confidence": 0-1} or null}

Evidence labels: picklist_value_match, business_knowledge, label_match,
semantic_match, relationship_match, data_type_match, name_field, no_fit."""


@dataclass
class Package:
    """Everything the decision may choose from. Built deterministically."""

    question: str
    entities: list[dict[str, str]]
    objects: dict[str, list[ObjectCandidate]]            # entity -> candidates
    anchored: list[ObjectCandidate]                      # from filter values
    filters: list[dict[str, Any]]
    attributes: list[dict[str, Any]]
    temporal: dict[str, Any] | None
    fields: dict[tuple[str, str], list[FieldCandidate]]  # (object, slot) -> candidates

    def object_set(self) -> list[str]:
        seen: list[str] = []
        for candidates in self.objects.values():
            for c in candidates[:3]:
                if c.api_name not in seen:
                    seen.append(c.api_name)
        for c in self.anchored:
            if c.api_name not in seen:
                seen.append(c.api_name)
        return seen

    def counts(self) -> dict[str, int]:
        return {"object_candidates": sum(len(v) for v in self.objects.values()),
                "anchored_objects": len(self.anchored),
                "field_candidates": sum(len(v) for v in self.fields.values())}


@dataclass
class Decision:
    objects: dict[str, Selection] = dataclass_field(default_factory=dict)
    primary_object: str | None = None
    filters: dict[int, tuple[str, Selection]] = dataclass_field(default_factory=dict)
    attributes: dict[int, tuple[str, Selection]] = dataclass_field(default_factory=dict)
    date_field: tuple[str, Selection] | None = None
    mode: str = "fast_path"             # fast_path | model | model_unavailable
    model: str = ""
    duration_ms: int = 0
    rejected: list[str] = dataclass_field(default_factory=list)

    @property
    def invoked(self) -> bool:
        return self.mode != "fast_path"

    def summary(self, package: Package | None = None) -> dict[str, Any]:
        out = {"mode": self.mode, "model_invoked": self.invoked,
               "model": self.model if self.invoked else None,
               "model_calls": 1 if self.invoked else 0,
               "duration_ms": self.duration_ms,
               "primary_object": self.primary_object,
               "objects": {k: [v.selected, v.confidence] for k, v in self.objects.items()},
               "filters": {i: [o, s.selected, s.resolved_value, s.confidence]
                           for i, (o, s) in self.filters.items()},
               "attributes": {i: [o, s.selected, s.confidence]
                              for i, (o, s) in self.attributes.items()},
               "date_field": (list(self.date_field[:1]) + [self.date_field[1].selected]
                              if self.date_field else None),
               "rejected": self.rejected}
        if package is not None:
            out.update(package.counts())
        return out


def _strong(candidate: Any) -> bool:
    return bool(STRONG & set(candidate.evidence))


def _decisive(candidates: list[Any], margin: float) -> bool:
    """The top candidate is a fact and nothing else comes close."""
    if not candidates or not _strong(candidates[0]):
        return False
    if len(candidates) == 1:
        return True
    second = candidates[1]
    return (candidates[0].retrieval_score - second.retrieval_score >= margin
            or not _strong(second))


def _unique_picklist(candidates: list[FieldCandidate]) -> FieldCandidate | None:
    hits = [c for c in candidates if c.matched_picklist_value]
    return hits[0] if len(hits) == 1 else None


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


class SemanticLinker:
    def __init__(self, retriever: Any, schema: Any, *, endpoint: str, model: str,
                 timeout: float = 60.0, max_tokens: int = 900,
                 fast_path_margin: float = 0.25,
                 business_notes: dict[str, dict[str, Any]] | None = None,
                 client: Any = None) -> None:
        self.retriever = retriever
        self.schema = schema
        self.endpoint = endpoint
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.margin = fast_path_margin
        self.notes = business_notes or {}
        self.last_error = ""
        # Injectable for tests: anything with post(url, json=, timeout=).
        self.client = client

    # -- 1. deterministic retrieval --------------------------------------
    def gather(self, intent: Any, question: str) -> Package:
        get = (lambda k, d=None: intent.get(k, d)) if isinstance(intent, dict) \
            else (lambda k, d=None: getattr(intent, k, d))
        entities = [e for e in (get("business_entities", []) or [])
                    if isinstance(e, dict) and e.get("name")]
        names = [e["name"] for e in entities]
        objects = {e["name"]: self.retriever.object_candidates(
                       e["name"], related_entities=[n for n in names if n != e["name"]])[:5]
                   for e in entities}
        filters = [f for f in (get("filters", []) or []) if f.get("concept")]
        attributes = [a for a in (get("requested_attributes", []) or [])
                      if a.get("attribute")]
        temporal = get("temporal")

        package = Package(question=question or "", entities=entities,
                          objects=objects, anchored=[], filters=filters,
                          attributes=attributes, temporal=temporal, fields={})
        package.anchored = self._anchored(filters, package)
        for api_name in package.object_set()[:6]:
            for index, spec in enumerate(filters):
                package.fields[(api_name, f"filter:{index}")] = \
                    self.retriever.field_candidates(
                        api_name, spec["concept"], value=spec.get("value"),
                        operator=spec.get("operator"))[:6]
            for index, spec in enumerate(attributes):
                package.fields[(api_name, f"attribute:{index}")] = \
                    self.retriever.field_candidates(api_name, spec["attribute"])[:6]
            if temporal and temporal.get("concept"):
                dated = [c for c in self.retriever.field_candidates(
                             api_name, temporal["concept"], limit=20)
                         if normalise(c.data_type) in TEMPORAL_TYPES]
                package.fields[(api_name, "date")] = dated[:6]
        return package

    def _anchored(self, filters: list[dict[str, Any]], package: Package
                  ) -> list[ObjectCandidate]:
        """Objects the FILTER VALUES point at, not the entity words.

        "Candidates who got an offer" names no interview, but "Offer Received"
        exists as a picklist value on Interview__c -- and without that object
        in the candidate set no model could choose it. Only distinctive values
        anchor: "Active" is a picklist value on dozens of objects and would
        flood the package.
        """
        already = {c.api_name for cs in package.objects.values() for c in cs[:3]}
        found: list[str] = []
        connection = self.schema.db.connection
        for spec in filters:
            value = spec.get("value")
            if isinstance(value, str) and value.strip() and normalise(spec["concept"]) != "name":
                owners = [r[0] for r in connection.execute(
                    "SELECT DISTINCT object_api_name FROM picklist_values"
                    " WHERE value = ? COLLATE NOCASE", (value.strip(),))]
                if 0 < len(owners) <= 3:
                    found += owners
            concept_tokens = set(normalise(spec["concept"]).split())
            for row in connection.execute(
                    "SELECT DISTINCT object_api_name, alias FROM field_aliases"
                    " WHERE is_manual = 1"):
                alias_tokens = set(normalise(row[1]).split())
                # Exact, or a reviewed multi-word alias contained in the concept.
                if alias_tokens == concept_tokens or (
                        len(alias_tokens) >= 2 and alias_tokens <= concept_tokens):
                    found.append(row[0])
        out: list[ObjectCandidate] = []
        for api_name in dict.fromkeys(found):
            if api_name in already:
                continue
            candidates = self.retriever.object_candidates(api_name, limit=1)
            if candidates and candidates[0].api_name == api_name:
                candidate = candidates[0]
                candidate.evidence = sorted(set(candidate.evidence) |
                                            {Evidence.PICKLIST_VALUE.value})
                out.append(candidate)
        return out[:3]

    # -- 2. fast path ------------------------------------------------------
    def fast_path(self, package: Package) -> Decision | None:
        if not package.entities or package.anchored:
            return None
        decision = Decision(mode="fast_path")
        for entity in package.entities:
            candidates = package.objects.get(entity["name"]) or []
            if not _decisive(candidates, self.margin):
                return None
            top = candidates[0]
            decision.objects[entity["name"]] = Selection(
                selected=top.api_name, confidence=0.99,
                reason_codes=list(top.evidence)[:6], model="retrieval")
        primary_entity = next((e for e in package.entities
                               if e.get("role") == "primary_entity"),
                              package.entities[0])
        primary = decision.objects[primary_entity["name"]].selected
        decision.primary_object = primary

        for index, spec in enumerate(package.filters):
            candidates = package.fields.get((primary, f"filter:{index}")) or []
            chosen = _unique_picklist(candidates) if spec.get("value") is not None else None
            if chosen is None and _decisive(candidates, self.margin):
                chosen = candidates[0]
            if chosen is None:
                return None
            decision.filters[index] = (primary, Selection(
                selected=chosen.api_name, confidence=0.99,
                resolved_value=chosen.matched_picklist_value,
                reason_codes=list(chosen.evidence)[:6], model="retrieval"))
        for index, _ in enumerate(package.attributes):
            candidates = package.fields.get((primary, f"attribute:{index}")) or []
            if not _decisive(candidates, self.margin):
                return None
            decision.attributes[index] = (primary, Selection(
                selected=candidates[0].api_name, confidence=0.99,
                reason_codes=list(candidates[0].evidence)[:6], model="retrieval"))
        if package.temporal and package.temporal.get("concept"):
            candidates = package.fields.get((primary, "date")) or []
            if not _decisive(candidates, self.margin):
                return None
            decision.date_field = (primary, Selection(
                selected=candidates[0].api_name, confidence=0.99, model="retrieval"))
        return decision

    # -- 3. one model call ---------------------------------------------------
    def _knowledge(self, kind: str, target: str) -> dict[str, Any] | None:
        entry = self.notes.get(f"{kind}:{target}")
        if not entry:
            return None
        out = {"terms": entry["terms"][:6]}
        if entry.get("notes"):
            out["note"] = entry["notes"][0]
        return out

    def prompt(self, package: Package) -> str:
        def obj(c: ObjectCandidate) -> dict[str, Any]:
            item = c.for_model()
            item.pop("description", None)
            knowledge = self._knowledge("object", c.api_name)
            if knowledge:
                item["business_knowledge"] = knowledge
            return item

        def fld(c: FieldCandidate) -> dict[str, Any]:
            item = {"api_name": c.api_name, "label": c.label,
                    "data_type": c.data_type, "evidence": c.evidence}
            if c.picklist_values:
                item["picklist_values"] = c.picklist_values[:15]
            if c.matched_picklist_value:
                item["matched_picklist_value"] = c.matched_picklist_value
            knowledge = self._knowledge("field", f"{c.object_api_name}.{c.api_name}")
            if knowledge:
                item["business_knowledge"] = knowledge
            return item

        fields: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for (api_name, slot), candidates in package.fields.items():
            if candidates:
                fields.setdefault(slot, {})[api_name] = [fld(c) for c in candidates]
        payload = {
            "question": package.question,
            "entities": [{"index": i, "name": e["name"], "role": e.get("role"),
                          "candidates": [obj(c) for c in package.objects.get(e["name"], [])]}
                         for i, e in enumerate(package.entities)],
            "objects_the_values_point_at": [obj(c) for c in package.anchored],
            "filters": [{"index": i, "concept": f["concept"],
                         "operator": f.get("operator"), "value": f.get("value"),
                         "candidate_fields": fields.get(f"filter:{i}", {})}
                        for i, f in enumerate(package.filters)],
            "attributes": [{"index": i, "attribute": a["attribute"],
                            "entity": a.get("entity"),
                            "candidate_fields": fields.get(f"attribute:{i}", {})}
                           for i, a in enumerate(package.attributes)],
        }
        if package.temporal:
            payload["date_restriction"] = {
                "expression": package.temporal.get("expression"),
                "concept": package.temporal.get("concept"),
                "candidate_fields": fields.get("date", {})}
        return json.dumps(payload, ensure_ascii=False)

    def _call(self, user: str) -> tuple[dict[str, Any] | None, int]:
        payload, duration, self.last_error = self._post(user)
        return payload, duration

    def _post(self, user: str) -> tuple[dict[str, Any] | None, int, str]:
        """(answer, ms, why-it-failed). The reason is traced, never guessed."""
        started = time.perf_counter()
        body = {"model": self.model,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": user}],
                "max_tokens": self.max_tokens, "temperature": 0,
                "response_format": {"type": "json_object"},
                "chat_template_kwargs": {"enable_thinking": False}}
        url = f"{self.endpoint.rstrip('/')}/chat/completions"
        try:
            if self.client is not None:
                response = self.client.post(url, json=body, timeout=self.timeout)
            else:
                import httpx
                with httpx.Client() as client:
                    response = client.post(url, json=body, timeout=self.timeout)
        except Exception as exc:                        # noqa: BLE001
            log.warning("semantic linker: main model unreachable: %s", exc)
            return (None, int((time.perf_counter() - started) * 1000),
                    f"unreachable: {type(exc).__name__}")
        duration = int((time.perf_counter() - started) * 1000)
        status = getattr(response, "status_code", 200)
        if status != 200:
            log.warning("semantic linker: HTTP %s", status)
            return None, duration, f"http_{status}"
        try:
            choice = response.json()["choices"][0]
            content = choice["message"]["content"]
        except Exception as exc:                        # noqa: BLE001
            return None, duration, f"malformed_envelope: {type(exc).__name__}"
        if not content:
            return None, duration, f"empty_content: finish={choice.get('finish_reason')}"
        try:
            return json.loads(content), duration, ""
        except json.JSONDecodeError:
            # The tail goes to the application log only, never the trace: it
            # is model output and may echo a value the user typed.
            log.warning("semantic linker: answer is not JSON (finish=%s, %d chars)"
                        " tail=%r", choice.get("finish_reason"), len(content),
                        content[-240:])
            return None, duration, f"not_json: finish={choice.get('finish_reason')}"

    # -- 4. closed-set enforcement ----------------------------------------
    def _parse(self, payload: dict[str, Any], package: Package,
               decision: Decision) -> None:
        offered_objects = set(package.object_set())

        by_name = {normalise(e["name"]): e["name"] for e in package.entities}
        for item in payload.get("entities") or []:
            # By index first: the model may write "interviews" for the entity
            # the extraction called "interview", and a name lookup then threw
            # away a correct decision. Seen live.
            index = item.get("index")
            if isinstance(index, int) and 0 <= index < len(package.entities):
                name = package.entities[index]["name"]
            else:
                name = by_name.get(normalise(item.get("name")))
            if name is None or name not in package.objects:
                continue
            chosen = item.get("object")
            allowed = {c.api_name for c in package.objects[name]} | \
                {c.api_name for c in package.anchored}
            if chosen and chosen not in allowed:
                decision.rejected.append(f"entity {name!r} -> {chosen} (not offered)")
                chosen = None
            decision.objects[name] = Selection(
                selected=chosen, confidence=_clamp(item.get("confidence")),
                reason_codes=[str(r) for r in (item.get("evidence") or [])][:6],
                model=self.model)

        primary = payload.get("primary_object")
        if primary and primary not in offered_objects:
            decision.rejected.append(f"primary_object {primary} (not offered)")
            primary = None
        decision.primary_object = primary
        # An entity the model skipped, but whose own candidate list contains
        # the primary object the model named: that IS the model's choice for
        # it, stated once instead of twice.
        if primary:
            for entity in package.entities:
                name = entity["name"]
                current = decision.objects.get(name)
                # Missing OR null: seen live both ways -- the model states the
                # object once, as primary_object, and leaves the entity entry
                # empty. It is the same choice, from the entity's own list.
                if (current is None or not current.selected) and primary in {
                        c.api_name for c in package.objects.get(name, [])}:
                    decision.objects[name] = Selection(
                        selected=primary, confidence=0.9,
                        reason_codes=["named_as_primary_object"], model=self.model)

        def slot_choice(item: dict[str, Any], slot: str
                        ) -> tuple[str, Selection] | None:
            owner, chosen = item.get("object"), item.get("field")
            offered = {c.api_name: c for c in package.fields.get((owner, slot), [])}
            if chosen and chosen not in offered:
                decision.rejected.append(f"{slot} -> {owner}.{chosen} (not offered)")
                chosen = None
            value = item.get("value")
            return owner, Selection(
                selected=chosen, confidence=_clamp(item.get("confidence")),
                reason_codes=[str(r) for r in (item.get("evidence") or [])][:6],
                resolved_value=value if isinstance(value, str) else None,
                model=self.model)

        for item in payload.get("filters") or []:
            if isinstance(item.get("index"), int) and 0 <= item["index"] < len(package.filters):
                choice = slot_choice(item, f"filter:{item['index']}")
                if choice:
                    decision.filters[item["index"]] = choice
        for item in payload.get("attributes") or []:
            if isinstance(item.get("index"), int) and 0 <= item["index"] < len(package.attributes):
                choice = slot_choice(item, f"attribute:{item['index']}")
                if choice:
                    decision.attributes[item["index"]] = choice
        date = payload.get("date_field")
        if isinstance(date, dict) and date.get("field"):
            decision.date_field = slot_choice(date, "date")

    def decide(self, package: Package) -> Decision:
        fast = self.fast_path(package)
        if fast is not None:
            return fast
        decision = Decision(mode="model", model=self.model)
        payload, decision.duration_ms = self._call(self.prompt(package))
        if payload is None:
            # No 8B fallback: a semantic decision nobody made is AMBIGUOUS,
            # and the request declines rather than guesses.
            decision.mode = "model_unavailable"
            decision.rejected.append(f"model_failed: {getattr(self, 'last_error', '')}")
            return decision
        self._parse(payload, package, decision)
        return decision
