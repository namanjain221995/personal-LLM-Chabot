"""The one public entry point: intent in, grounded schema plan out.

    RETRIEVAL -> RERANKING -> VERIFICATION -> GROUNDING

Retrieval narrows 419 objects and 4,592 fields to a handful using the runtime
schema alone. A model chooses among those. Verification confirms every chosen
identifier exists and was actually offered. Only then does anything enter the
plan -- which is the only schema a later SOQL generator is permitted to read.

The intent is never mutated. What the user said and what Salesforce calls it
stay side by side, so a wrong answer can be traced to whichever of the two was
at fault.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Sequence

from .config import SchemaLinkingConfig, load_schema_linking_config
from .confidence import resolve
from .models import (AttributeMapping, EntityMapping, FailureCode, FilterMapping,
                     GroundedSchemaPlan, Selection)
from . import tracing
from .reranker import SchemaLinkingModel, build_models
from .retriever import CandidateRetriever, normalise
from .verifier import SchemaVerifier

log = logging.getLogger(__name__)


def _entities(intent: Any) -> list[dict[str, str]]:
    raw = intent.get("business_entities") if isinstance(intent, dict) else getattr(
        intent, "business_entities", None)
    return [e for e in (raw or []) if isinstance(e, dict) and e.get("name")]


def _get(intent: Any, key: str, default: Any = None) -> Any:
    if isinstance(intent, dict):
        return intent.get(key, default)
    return getattr(intent, key, default)


class SchemaLinker:
    def __init__(self, schema_service: Any,
                 config: SchemaLinkingConfig | None = None, *,
                 primary_model: SchemaLinkingModel | None = None,
                 fallback_model: SchemaLinkingModel | None = None,
                 offline: bool = False,
                 semantic: Any = None,
                 business_notes: dict[str, dict[str, Any]] | None = None) -> None:
        self.schema = schema_service
        self.config = config or load_schema_linking_config()
        self.retriever = CandidateRetriever(schema_service, self.config)
        self.verifier = SchemaVerifier(schema_service)
        built_primary, built_fallback = build_models(self.config, offline=offline)
        self.primary = primary_model or built_primary
        self.fallback = fallback_model or built_fallback
        # offline      -> per-item selection by retrieval score, no model
        # llm_assisted -> one semantic decision per question on the main model
        #   per_item     -> a caller supplied per-item models explicitly; honour
        #                   them (the pre-step-7 online path, kept for tests
        #                   and for comparing the two approaches)
        self.semantic = semantic
        if offline:
            self.mode = "offline"
        elif semantic is None and (primary_model or fallback_model):
            self.mode = "per_item"
        else:
            self.mode = "llm_assisted"
        if self.semantic is None and self.mode == "llm_assisted":
            from .semantic import SemanticLinker
            self.semantic = SemanticLinker(
                self.retriever, schema_service,
                endpoint=self.config.models.primary.endpoint,
                model=self.config.models.primary.name,
                timeout=self.config.models.timeout_seconds,
                max_tokens=self.config.models.max_tokens,
                fast_path_margin=self.config.fast_path_margin,
                business_notes=business_notes)

    # -- objects ----------------------------------------------------------
    def _link_object(self, entity: dict[str, str], intent: Any,
                     plan: GroundedSchemaPlan) -> tuple[str | None, Selection | None]:
        name = entity["name"]
        role = entity.get("role", "primary_entity")
        others = [e["name"] for e in _entities(intent) if e["name"] != name]
        started = time.perf_counter()
        candidates = self.retriever.object_candidates(name, related_entities=others)
        plan.trace.append(tracing.candidates_retrieved(
            tracing.OBJECT_CANDIDATES_RETRIEVED, name, candidates,
            int((time.perf_counter() - started) * 1000)))
        if not candidates:
            plan.fail(FailureCode.NO_OBJECT_CANDIDATES,
                      f"nothing in the runtime schema matched {name!r}",
                      business_entity=name)
            return None, None

        decision = resolve(
            self.primary.select_object(
                name, role, candidates,
                filters=_get(intent, "filters", []),
                requested_attributes=[a.get("attribute") for a in
                                      _get(intent, "requested_attributes", []) or []]),
            lambda: self.fallback.select_object(name, role, candidates),
            self.config.confidence,
            ambiguous_code=FailureCode.AMBIGUOUS_OBJECT)

        plan.trace.append(tracing.selected(
            tracing.OBJECT_SELECTED, name, decision.selection,
            accepted=decision.accepted))
        if decision.escalated:
            plan.trace.append(tracing.escalated(name, decision.selection,
                                                decision.selection))
        if not decision.accepted:
            plan.fail(decision.code or FailureCode.AMBIGUOUS_OBJECT,
                      f"no confident object for {name!r}",
                      business_entity=name,
                      confidence=decision.selection.confidence,
                      candidates=[c.api_name for c in candidates[:5]])
            plan.requires_fallback = True
            return None, decision.selection

        verified = self.verifier.verify_object(decision.selection.selected, candidates)
        plan.trace.append(tracing.verified(
            tracing.OBJECT_VERIFIED, decision.selection.selected or "", verified))
        if not verified:
            plan.fail(verified.code or FailureCode.OBJECT_VERIFICATION_FAILED,
                      verified.detail, business_entity=name)
            return None, decision.selection

        # Feeds the hot-object cache. Not duplicated here -- the runtime schema
        # service owns that policy.
        self.schema.record_object_access(verified.value)
        return verified.value, decision.selection

    # -- fields -----------------------------------------------------------
    def _link_field(self, object_api_name: str, concept: str, value: Any,
                    operator: str | None, plan: GroundedSchemaPlan
                    ) -> tuple[str | None, Selection | None, str | None]:
        started = time.perf_counter()
        candidates = self.retriever.field_candidates(
            object_api_name, concept, value=value, operator=operator)
        plan.trace.append(tracing.candidates_retrieved(
            tracing.FIELD_CANDIDATES_RETRIEVED, concept, candidates,
            int((time.perf_counter() - started) * 1000), scope=object_api_name))
        if not candidates:
            plan.fail(FailureCode.NO_FIELD_CANDIDATES,
                      f"no field on {object_api_name} matched {concept!r}",
                      concept=concept)
            return None, None, None

        decision = resolve(
            self.primary.select_field(concept, value, operator,
                                      object_api_name, candidates),
            lambda: self.fallback.select_field(concept, value, operator,
                                               object_api_name, candidates),
            self.config.confidence,
            ambiguous_code=FailureCode.AMBIGUOUS_FIELD)

        plan.trace.append(tracing.selected(
            tracing.FIELD_SELECTED, concept, decision.selection,
            accepted=decision.accepted, scope=object_api_name))
        if not decision.accepted:
            plan.fail(decision.code or FailureCode.AMBIGUOUS_FIELD,
                      f"no confident field for {concept!r} on {object_api_name}",
                      concept=concept, confidence=decision.selection.confidence,
                      candidates=[c.api_name for c in candidates[:5]])
            plan.requires_fallback = True
            return None, decision.selection, None

        verified = self.verifier.verify_field(object_api_name,
                                              decision.selection.selected, candidates)
        plan.trace.append(tracing.verified(
            tracing.FIELD_VERIFIED, decision.selection.selected or "", verified,
            scope=object_api_name))
        if not verified:
            plan.fail(verified.code or FailureCode.FIELD_VERIFICATION_FAILED,
                      verified.detail, concept=concept)
            return None, decision.selection, None

        # A picklist filter must carry Salesforce's spelling, not the user's:
        # "unassigned" matches nothing where the org stores "Unassigned".
        resolved = self.verifier.verify_picklist_value(
            object_api_name, verified.value,
            decision.selection.resolved_value or value)
        return verified.value, decision.selection, resolved

    # -- relationships ----------------------------------------------------
    def _link_relationship(self, object_api_name: str, entity_name: str,
                           plan: GroundedSchemaPlan
                           ) -> tuple[Any | None, Selection | None]:
        started = time.perf_counter()
        candidates = self.retriever.relationship_candidates(
            object_api_name, entity_name)
        plan.trace.append(tracing.candidates_retrieved(
            tracing.RELATIONSHIP_CANDIDATES_RETRIEVED, entity_name, candidates,
            int((time.perf_counter() - started) * 1000), scope=object_api_name))
        if not candidates:
            plan.fail(FailureCode.NO_RELATIONSHIP_PATH,
                      f"no relationship from {object_api_name} matched {entity_name!r}",
                      business_entity=entity_name)
            return None, None

        decision = resolve(
            self.primary.select_relationship(entity_name, object_api_name, candidates),
            lambda: self.fallback.select_relationship(entity_name, object_api_name,
                                                      candidates),
            self.config.confidence,
            ambiguous_code=FailureCode.AMBIGUOUS_RELATIONSHIP)

        plan.trace.append(tracing.selected(
            tracing.RELATIONSHIP_SELECTED, entity_name, decision.selection,
            accepted=decision.accepted, scope=object_api_name))
        if not decision.accepted:
            plan.fail(decision.code or FailureCode.AMBIGUOUS_RELATIONSHIP,
                      f"no confident relationship for {entity_name!r}",
                      business_entity=entity_name,
                      confidence=decision.selection.confidence)
            plan.requires_fallback = True
            return None, decision.selection

        verified = self.verifier.verify_relationship(
            object_api_name, decision.selection.selected, candidates)
        plan.trace.append(tracing.verified(
            tracing.RELATIONSHIP_VERIFIED, decision.selection.selected or "",
            verified, scope=object_api_name))
        if not verified:
            plan.fail(verified.code or FailureCode.RELATIONSHIP_VERIFICATION_FAILED,
                      verified.detail, business_entity=entity_name)
            return None, decision.selection

        chosen = next(c for c in candidates if c.source_field == verified.value)
        self.schema.record_object_access(chosen.target_object)
        return chosen, decision.selection

    # -- entry point ------------------------------------------------------
    def link(self, intent: Any, question: str = "") -> GroundedSchemaPlan:
        if self.mode == "llm_assisted" and self.semantic is not None:
            return self._link_semantic(intent, question)
        return self._link_offline(intent)

    def _link_offline(self, intent: Any) -> GroundedSchemaPlan:
        started = time.perf_counter()
        plan = GroundedSchemaPlan()
        plan.trace.append(tracing.started(intent))
        entities = _entities(intent)
        if not entities:
            plan.fail(FailureCode.NO_OBJECT_CANDIDATES,
                      "the intent named no business entities")
            plan.duration_ms = int((time.perf_counter() - started) * 1000)
            return plan

        primary_entity = next(
            (e for e in entities if e.get("role") == "primary_entity"), entities[0])
        primary_object, selection = self._link_object(primary_entity, intent, plan)
        if primary_object is None:
            plan.duration_ms = int((time.perf_counter() - started) * 1000)
            return plan

        plan.primary_object = primary_object
        plan.objects = [primary_object]
        plan.entity_mappings.append(EntityMapping(
            business_entity=primary_entity["name"],
            salesforce_object=primary_object,
            confidence=selection.confidence if selection else 0.0,
            role=primary_entity.get("role", "primary_entity"),
            reason_codes=selection.reason_codes if selection else []))

        # Related entities resolve as RELATIONSHIPS from the primary object,
        # not as free-standing objects: "candidate" in this question means the
        # Account that Internal_Interview__c.Candidate__c points at, and which
        # of several Account lookups it is depends on business meaning.
        related_paths: dict[str, Any] = {}
        for entity in entities:
            if entity is primary_entity:
                continue
            chosen, rel_selection = self._link_relationship(
                primary_object, entity["name"], plan)
            if chosen is None:
                continue
            related_paths[normalise(entity["name"])] = chosen
            if chosen.target_object not in plan.objects:
                plan.objects.append(chosen.target_object)
            plan.entity_mappings.append(EntityMapping(
                business_entity=entity["name"],
                salesforce_object=chosen.target_object,
                confidence=rel_selection.confidence if rel_selection else 0.0,
                role=entity.get("role", "related_entity"),
                source_field=chosen.source_field,
                relationship_name=chosen.traversal_name,
                target_object=chosen.target_object,
                reason_codes=rel_selection.reason_codes if rel_selection else []))

        for spec in _get(intent, "filters", []) or []:
            concept = spec.get("concept")
            if not concept:
                continue
            field, field_selection, resolved = self._link_field(
                primary_object, concept, spec.get("value"),
                spec.get("operator"), plan)
            if field is None:
                continue
            row = self.schema.repo.get_field(primary_object, field)
            plan.filter_mappings.append(FilterMapping(
                business_concept=concept, field=field,
                operator=spec.get("operator", "equals"),
                value=resolved if resolved is not None else spec.get("value"),
                confidence=field_selection.confidence if field_selection else 0.0,
                object_api_name=primary_object,
                data_type=(row or {}).get("data_type"),
                reason_codes=field_selection.reason_codes if field_selection else []))

        for spec in _get(intent, "requested_attributes", []) or []:
            attribute = spec.get("attribute")
            if not attribute:
                continue
            owner = normalise(spec.get("entity"))
            path = related_paths.get(owner)
            if path is not None:
                # Attribute lives on a related object: search THERE, and write
                # the path with the relationship name the schema supplies.
                target_field, selection_, _ = self._link_field(
                    path.target_object, attribute, None, None, plan)
                if target_field is None:
                    continue
                verified = self.verifier.verify_field_path(
                    primary_object, path.traversal_name, path.target_object,
                    target_field)
                if not verified:
                    plan.fail(verified.code or FailureCode.FIELD_VERIFICATION_FAILED,
                              verified.detail, business_attribute=attribute)
                    continue
                plan.requested_attribute_mappings.append(AttributeMapping(
                    business_attribute=f"{spec.get('entity')} {attribute}".strip(),
                    field_path=verified.value, target_object=path.target_object,
                    target_field=target_field,
                    confidence=selection_.confidence if selection_ else 0.0,
                    reason_codes=selection_.reason_codes if selection_ else []))
            else:
                field, selection_, _ = self._link_field(
                    primary_object, attribute, None, None, plan)
                if field is None:
                    continue
                plan.requested_attribute_mappings.append(AttributeMapping(
                    business_attribute=attribute, field_path=field,
                    target_object=primary_object, target_field=field,
                    confidence=selection_.confidence if selection_ else 0.0,
                    reason_codes=selection_.reason_codes if selection_ else []))

        plan.schema_grounded = bool(plan.primary_object) and not plan.failures
        plan.duration_ms = int((time.perf_counter() - started) * 1000)
        plan.trace.append(tracing.plan_created(plan))
        plan.trace.append(tracing.completed(plan))
        return plan


# ===========================================================================
# llm_assisted: one semantic decision, then the same verification as always
# ===========================================================================
_CHECKBOX_TRUE = {"true", "yes", "y", "1", "available", "active", "checked"}
_CHECKBOX_FALSE = {"false", "no", "n", "0", "unavailable", "inactive", "unchecked"}


def _checkbox(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = normalise(value)
    if text in _CHECKBOX_TRUE:
        return True
    if text in _CHECKBOX_FALSE:
        return False
    return None


def _link_semantic(self, intent: Any, question: str) -> GroundedSchemaPlan:
    """The step-7 path. Same plan shape, same verifier, one model decision.

    The model chooses; this function refuses anything the schema cannot back:
    an object or field that was not offered, a picklist value the field does
    not have, a non-boolean for a checkbox, a non-date field for a date range.
    """
    from .semantic import TEMPORAL_TYPES
    from .temporal import parse as parse_period

    started = time.perf_counter()
    plan = GroundedSchemaPlan()
    plan.trace.append(tracing.started(intent))
    entities = _entities(intent)
    if not entities:
        plan.fail(FailureCode.NO_OBJECT_CANDIDATES,
                  "the intent named no business entities")
        plan.duration_ms = int((time.perf_counter() - started) * 1000)
        return plan

    retrieval_started = time.perf_counter()
    package = self.semantic.gather(intent, question)
    retrieval_ms = int((time.perf_counter() - retrieval_started) * 1000)
    for entity in entities:
        plan.trace.append(tracing.candidates_retrieved(
            tracing.OBJECT_CANDIDATES_RETRIEVED, entity["name"],
            package.objects.get(entity["name"], []), retrieval_ms))

    decision = self.semantic.decide(package)
    plan.trace.append(tracing.semantic_rerank(decision, package))
    plan.trace.append(tracing.semantic_linking(decision, package))
    plan.semantic = {**decision.summary(package), "linker_mode": "llm_assisted",
                     "retrieval_ms": retrieval_ms}
    threshold = self.config.confidence.fallback_to_main_model

    def finish() -> GroundedSchemaPlan:
        plan.schema_grounded = bool(plan.primary_object) and not plan.failures
        plan.duration_ms = int((time.perf_counter() - started) * 1000)
        plan.trace.append(tracing.plan_created(plan))
        plan.trace.append(tracing.completed(plan))
        return plan

    if decision.mode == "model_unavailable":
        plan.fail(FailureCode.MAIN_MODEL_LOW_CONFIDENCE,
                  "the main model could not make a decision retrieval could "
                  "not make alone", reason=", ".join(decision.rejected)
                  or "model_unavailable")
        return finish()

    # -- objects ------------------------------------------------------------
    chosen: dict[str, str] = {}
    for entity in entities:
        name = entity["name"]
        candidates = package.objects.get(name) or []
        selection = decision.objects.get(name)
        if not candidates and not package.anchored:
            plan.fail(FailureCode.NO_OBJECT_CANDIDATES,
                      f"nothing in the runtime schema matched {name!r}",
                      business_entity=name)
            continue
        if selection is None or not selection.selected or selection.confidence < threshold:
            plan.fail(FailureCode.AMBIGUOUS_OBJECT, f"no confident object for {name!r}",
                      business_entity=name,
                      confidence=selection.confidence if selection else 0.0,
                      candidates=[c.api_name for c in candidates[:5]])
            plan.requires_fallback = True
            continue
        plan.trace.append(tracing.selected(tracing.OBJECT_SELECTED, name, selection,
                                           accepted=True))
        verified = self.verifier.verify_object(selection.selected,
                                               list(candidates) + list(package.anchored))
        plan.trace.append(tracing.verified(tracing.OBJECT_VERIFIED,
                                           selection.selected or "", verified))
        if not verified:
            plan.fail(verified.code or FailureCode.OBJECT_VERIFICATION_FAILED,
                      verified.detail, business_entity=name)
            continue
        chosen[name] = verified.value
    if plan.failures:
        return finish()

    primary_entity = next((e for e in entities if e.get("role") == "primary_entity"),
                          entities[0])
    primary = decision.primary_object or chosen.get(primary_entity["name"])
    if primary not in set(chosen.values()) | {c.api_name for c in package.anchored} \
            or self.schema.repo.get_object(primary) is None:
        primary = chosen.get(primary_entity["name"])
    plan.primary_object = primary
    plan.objects = [primary]
    self.schema.record_object_access(primary)

    # The entity whose object IS the primary; if the model chose an object the
    # values point at (a "fact" object no entity named), no entity maps to it.
    anchor_entity = next((e for e in entities if chosen.get(e["name"]) == primary), None)
    if anchor_entity is not None:
        selection = decision.objects[anchor_entity["name"]]
        plan.entity_mappings.append(EntityMapping(
            business_entity=anchor_entity["name"], salesforce_object=primary,
            confidence=selection.confidence,
            role=anchor_entity.get("role", "primary_entity"),
            reason_codes=selection.reason_codes))
    else:
        row = self.schema.repo.get_object(primary) or {}
        plan.entity_mappings.append(EntityMapping(
            business_entity=str(row.get("label") or primary), salesforce_object=primary,
            confidence=1.0, role="fact_object",
            reason_codes=["values_point_here"]))

    # -- relationships: every other entity is reached FROM the primary ------
    reached: dict[str, Any] = {}
    for entity in entities:
        if entity is anchor_entity:
            continue
        target = chosen.get(entity["name"])
        rels = [r for r in self.retriever.relationship_candidates(
                    primary, entity["name"], limit=10) if r.target_object == target]
        plan.trace.append(tracing.candidates_retrieved(
            tracing.RELATIONSHIP_CANDIDATES_RETRIEVED, entity["name"], rels, 0,
            scope=primary))
        if not rels:
            plan.fail(FailureCode.NO_RELATIONSHIP_PATH,
                      f"no relationship from {primary} to {target} for {entity['name']!r}",
                      business_entity=entity["name"])
            continue
        if len(rels) > 1 and rels[0].retrieval_score - rels[1].retrieval_score < 0.1:
            plan.fail(FailureCode.AMBIGUOUS_RELATIONSHIP,
                      f"{len(rels)} relationships from {primary} reach {target}",
                      business_entity=entity["name"],
                      candidates=[r.source_field for r in rels[:5]])
            continue
        rel = rels[0]
        verified = self.verifier.verify_relationship(primary, rel.source_field, rels)
        plan.trace.append(tracing.verified(tracing.RELATIONSHIP_VERIFIED,
                                           rel.source_field, verified, scope=primary))
        if not verified:
            plan.fail(verified.code or FailureCode.RELATIONSHIP_VERIFICATION_FAILED,
                      verified.detail, business_entity=entity["name"])
            continue
        reached[target] = rel
        if target not in plan.objects:
            plan.objects.append(target)
        self.schema.record_object_access(target)
        selection = decision.objects.get(entity["name"])
        plan.entity_mappings.append(EntityMapping(
            business_entity=entity["name"], salesforce_object=target,
            confidence=selection.confidence if selection else 0.0,
            role=entity.get("role", "related_entity"),
            source_field=rel.source_field, relationship_name=rel.traversal_name,
            target_object=target,
            reason_codes=selection.reason_codes if selection else []))

    def owner_of(owner: str | None) -> str | None:
        if not owner or owner == primary:
            return primary
        return owner if owner in reached else None

    # -- filters -------------------------------------------------------------
    for index, spec in enumerate(package.filters):
        pick = decision.filters.get(index)
        owner = owner_of(pick[0]) if pick else None
        selection = pick[1] if pick else None
        if owner is None or selection is None or not selection.selected \
                or selection.confidence < threshold:
            plan.fail(FailureCode.AMBIGUOUS_FIELD,
                      f"no confident field for {spec['concept']!r}",
                      concept=spec["concept"],
                      confidence=selection.confidence if selection else 0.0,
                      candidates=[c.api_name for c in
                                  package.fields.get((primary, f"filter:{index}"), [])[:5]])
            plan.requires_fallback = True
            continue
        offered = package.fields.get((owner, f"filter:{index}"), [])
        plan.trace.append(tracing.selected(tracing.FIELD_SELECTED, spec["concept"],
                                           selection, accepted=True, scope=owner))
        verified = self.verifier.verify_field(owner, selection.selected, offered)
        plan.trace.append(tracing.verified(tracing.FIELD_VERIFIED, selection.selected,
                                           verified, scope=owner))
        if not verified:
            plan.fail(verified.code or FailureCode.FIELD_VERIFICATION_FAILED,
                      verified.detail, concept=spec["concept"])
            continue
        row = self.schema.repo.get_field(owner, verified.value) or {}
        data_type = normalise(row.get("data_type"))
        operator = spec.get("operator") or "equals"
        value = spec.get("value")
        if data_type in ("picklist", "multiselectpicklist") and operator in ("equals", "not_equals"):
            stored = self.verifier.verify_picklist_value(
                owner, verified.value, selection.resolved_value or value)
            if stored is None:
                plan.fail(FailureCode.FIELD_VERIFICATION_FAILED,
                          f"{value!r} is not a value of {owner}.{verified.value}",
                          concept=spec["concept"])
                continue
            value = stored
        elif data_type == "checkbox":
            flag = _checkbox(selection.resolved_value if selection.resolved_value is not None
                             else value)
            if flag is None:
                plan.fail(FailureCode.FIELD_VERIFICATION_FAILED,
                          f"{value!r} is not true/false for checkbox "
                          f"{owner}.{verified.value}", concept=spec["concept"])
                continue
            value = flag
        plan.filter_mappings.append(FilterMapping(
            business_concept=spec["concept"], field=verified.value, operator=operator,
            value=value, confidence=selection.confidence, object_api_name=owner,
            data_type=row.get("data_type"), reason_codes=selection.reason_codes))

    # -- attributes ------------------------------------------------------------
    for index, spec in enumerate(package.attributes):
        pick = decision.attributes.get(index)
        owner = owner_of(pick[0]) if pick else None
        selection = pick[1] if pick else None
        if owner is None or selection is None or not selection.selected \
                or selection.confidence < threshold:
            plan.fail(FailureCode.AMBIGUOUS_FIELD,
                      f"no confident field for {spec['attribute']!r}",
                      concept=spec["attribute"])
            plan.requires_fallback = True
            continue
        verified = self.verifier.verify_field(
            owner, selection.selected, package.fields.get((owner, f"attribute:{index}"), []))
        plan.trace.append(tracing.verified(tracing.FIELD_VERIFIED, selection.selected,
                                           verified, scope=owner))
        if not verified:
            plan.fail(verified.code or FailureCode.FIELD_VERIFICATION_FAILED,
                      verified.detail, business_attribute=spec["attribute"])
            continue
        if owner == primary:
            path, target_field = verified.value, verified.value
        else:
            rel = reached[owner]
            walk = self.verifier.verify_field_path(primary, rel.traversal_name, owner,
                                                   verified.value)
            if not walk:
                plan.fail(walk.code or FailureCode.FIELD_VERIFICATION_FAILED,
                          walk.detail, business_attribute=spec["attribute"])
                continue
            path, target_field = walk.value, verified.value
        plan.requested_attribute_mappings.append(AttributeMapping(
            business_attribute=spec["attribute"], field_path=path, target_object=owner,
            target_field=target_field, confidence=selection.confidence,
            reason_codes=selection.reason_codes))

    # The user asked about candidates but the records are interviews: show
    # which candidate each row belongs to, or the answer lists interviews the
    # user never asked for by name.
    result_object = chosen.get(primary_entity["name"])
    if result_object and result_object != primary and result_object in reached \
            and not any(m.target_object == result_object
                        for m in plan.requested_attribute_mappings) \
            and self.schema.repo.get_field(result_object, "Name") is not None:
        rel = reached[result_object]
        walk = self.verifier.verify_field_path(primary, rel.traversal_name,
                                               result_object, "Name")
        if walk:
            plan.requested_attribute_mappings.append(AttributeMapping(
                business_attribute=f"{primary_entity['name']} name",
                field_path=walk.value, target_object=result_object,
                target_field="Name", confidence=1.0,
                reason_codes=["result_entity_name"]))

    # -- date restriction ------------------------------------------------------
    if package.temporal:
        period = parse_period(package.temporal.get("expression"))
        if period is None:
            plan.fail(FailureCode.SCHEMA_LINKING_FAILED,
                      f"could not read the date {package.temporal.get('expression')!r}",
                      concept="date")
            return finish()
        field_name = None
        if decision.date_field and decision.date_field[1].selected \
                and owner_of(decision.date_field[0]) == primary:
            verified = self.verifier.verify_field(
                primary, decision.date_field[1].selected,
                package.fields.get((primary, "date"), []))
            field_name = verified.value if verified else None
        if field_name is None and not package.temporal.get("concept"):
            field_name = self.config.default_date_field
        row = self.schema.repo.get_field(primary, field_name) if field_name else None
        if row is None or normalise(row.get("data_type")) not in TEMPORAL_TYPES:
            plan.fail(FailureCode.AMBIGUOUS_FIELD,
                      f"no date field on {primary} for "
                      f"{package.temporal.get('concept') or 'the date restriction'!r}",
                      concept=package.temporal.get("concept") or "date")
            return finish()
        for operator, bound in (("on_or_after", period.start),
                                ("before", period.end_exclusive)):
            if bound is None:          # open-ended: "upcoming", "before May"
                continue
            plan.filter_mappings.append(FilterMapping(
                business_concept=period.expression, field=field_name,
                operator=operator, value=bound.isoformat(), confidence=1.0,
                object_api_name=primary, data_type=row.get("data_type"),
                reason_codes=[f"period:{period.kind}", "code_resolved_range"]))
        plan.semantic["period"] = period.as_dict()
        plan.semantic["date_field"] = field_name

    return finish()


SchemaLinker._link_semantic = _link_semantic


def link_salesforce_schema(intent: Any, schema_service: Any, *,
                           config: SchemaLinkingConfig | None = None,
                           offline: bool = False) -> GroundedSchemaPlan:
    """Business concepts in, verified Salesforce identifiers out."""
    return SchemaLinker(schema_service, config, offline=offline).link(intent)
