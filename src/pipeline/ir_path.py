"""The semantic-IR path: the main model decides at every semantic stage.

    question
      -> intent + routing   (main model, one call)  meaning, and the sources
                                                    that can answer it
      -> capabilities.plan  (code)  the handler for each routed source
      -> discovery          (main model)  entity -> object, by candidate ID
      -> linking            (main model)  concept -> field, value -> stored
                                          value, entity -> relationship path
      -> planning           (main model)  the logical query plan
      -> plan compiler, AnalyticPlanner, DuckDB / schema / sync / history (code)
      -> interpretation + answer (main model, one call)  which facts answer the
                                          question, then the answer
      -> grounding validator (code)

Retrieval, the relationship graph and the runtime schema supply evidence and
enforce technical truth; deterministic code executes. No stage skips the main
model because retrieval looks confident (spec §5, §6), and a stage whose model
cannot be reached fails the question as MAIN_MODEL_UNAVAILABLE instead of
being decided by code (§52). Every model stage leaves a `ModelCall` record on
`PipelineResult.model_stages` and a MODEL_STAGE trace event (§44).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from . import tracing
from .capabilities import plan as plan_capabilities
from .conversation import previous_summary
from .dispatch import Stage
from .models import PipelineError, PipelineRequest, PipelineResult
from .results import TypedResult, to_interpreted
from .stage_model import StageLog

log = logging.getLogger(__name__)

_NAME_CONCEPTS = {"name", "record name", "number", "record number", "id"}
_FIELD_ASKS = ("field_for_concept", "field_datatype", "picklist_values",
               "lookup_target", "standard_or_custom")


@dataclass
class IRComponents:
    compiler: Any                       # pipeline.compiler.IntentCompiler
    grounder: Any                       # schema_linking.ir_grounder.IRGrounder
    record_engine: Any = None           # engines.records.RecordEngine
    schema_engine: Any = None           # engines.schema.SchemaEngine
    operational_engine: Any = None      # engines.operational.OperationalEngine
    history_engine: Any = None          # engines.history.HistoryEngine
    search_engine: Any = None           # engines.search.SearchEngine
    conversations: Any = None           # conversation.ConversationStore
    metadata_available: bool = False
    planner: Any = None                 # pipeline.planner.LogicalPlanner


def _ms(mark: float) -> int:
    return int((time.perf_counter() - mark) * 1000)


def applicable_stages(capabilities: list[str], ir: Any, unsupported: dict) -> list[str]:
    """The model stages this question's route needs (model_roles.REQUIRED_MODEL_STAGES)."""
    from model_roles import stage_required
    stages = ["intent", "routing"]
    caps = set(capabilities)
    if not caps or caps == {"conversation"}:
        return [s for s in stages if stage_required(s)]
    if not unsupported:
        if "record_query" in caps:
            stages += ["discovery", "linking", "planning"]
        if "schema_query" in caps and ir.schema:
            if "discovery" not in stages:
                stages.append("discovery")
            if any(a.kind in _FIELD_ASKS for a in ir.schema) and "linking" not in stages:
                stages.append("linking")
        if "operational_context" in caps and ir.entities and "discovery" not in stages:
            stages.append("discovery")
        if "history_query" in caps:
            for stage in ("discovery", "linking"):
                if stage not in stages and (stage == "discovery" or _history_field(ir)):
                    stages.append(stage)
    stages += ["interpretation", "answer"]
    order = ["intent", "routing", "discovery", "linking", "planning", "interpretation",
             "answer"]
    return [s for s in order if s in stages and stage_required(s)]


def _history_field(ir: Any) -> str | None:
    return next((a.get("concept") for a in ir.attributes), None) or next(
        (f.concept for f in ir.filters if f.concept.lower() not in _NAME_CONCEPTS), None)


class _Entities:
    """The minimum of an IR that linking reads, for questions that have none."""

    def __init__(self, entities: dict[str, tuple[str, str]]) -> None:
        self._entities = entities

    def entity(self, ref: str) -> Any:
        if ref not in self._entities:
            return None
        concept, role = self._entities[ref]
        return SimpleNamespace(ref=ref, concept=concept, role=role)


class IRPath:
    def __init__(self, parts: IRComponents, answers: Any) -> None:
        self.parts = parts
        self.answers = answers

    # =====================================================================
    def run(self, request: PipelineRequest, result: PipelineResult) -> None:
        """Fill `result` in place. The caller owns trace open/close."""
        calls = StageLog()
        result.model_stages = calls.calls
        try:
            self._run(request, result, calls)
        finally:
            for call in calls.calls:
                result.trace.append(tracing.model_stage(call))

    def _run(self, request: PipelineRequest, result: PipelineResult,
             calls: StageLog) -> None:
        parts = self.parts

        # -- 1+2. intent and routing (one call) --------------------------------
        previous, _ = (parts.conversations.load(request.conversation_id)
                       if parts.conversations is not None else (None, None))
        mark = time.perf_counter()
        compiled = parts.compiler.compile(
            request.question,
            previous=({"question": previous.question,
                       "ir_summary": previous_summary(previous)} if previous else None),
            log_to=calls)
        ir = compiled.ir
        result.trace.append(tracing.ir_event(
            tracing.IR_COMPILED,
            {"ok": ir is not None, "error": compiled.error,
             "family": getattr(ir, "family", None),
             "output_mode": getattr(ir, "output_mode", None),
             "sources": getattr(ir, "sources", None),
             "follow_up": getattr(ir, "follow_up", None),
             "model": getattr(ir, "model", ""), "retried": getattr(ir, "retried", False),
             "completion_tokens": getattr(ir, "completion_tokens", 0)},
            ok=ir is not None, duration_ms=_ms(mark)))
        result.applicable_stages = applicable_stages([], None, {})
        if ir is None:
            if getattr(compiled, "failure", "") == "unavailable":
                result.fail(PipelineError.MAIN_MODEL_UNAVAILABLE,
                            f"intent: {compiled.error}")
            else:
                result.fail(PipelineError.EXTRACTION_FAILED,
                            f"intent compiler: {compiled.error}")
            return
        result.extraction = ir

        # -- capabilities for the routed sources -------------------------------
        capability = plan_capabilities(ir, metadata_available=parts.metadata_available)
        result.route = capability.route
        result.applicable_stages = applicable_stages(capability.capabilities, ir,
                                                     capability.unsupported)
        result.trace.append(tracing.ir_event(tracing.CAPABILITIES_PLANNED,
                                             capability.as_dict()))
        if ir.family in ("none", "conversation") or not capability.capabilities \
                or capability.capabilities == ["conversation"]:
            # Nothing Salesforce-shaped was asked. Correct, not a failure: the
            # caller's own conversational path answers it.
            result.success = True
            return

        # -- discovery, linking, planning, execution ---------------------------
        if capability.unsupported:
            typed = TypedResult(kind="unsupported", source="capabilities",
                                values={"unavailable": "; ".join(capability.unsupported.values())})
        else:
            typed = self._execute(ir, capability, result, calls)
            if typed is None:
                return                       # a failure is already on `result`
        typed.entity_label = typed.entity_label or (ir.primary.concept if ir.primary else None)
        result.records = typed
        result.trace.append(tracing.ir_event(
            tracing.FACTS_FUSED, {"kind": typed.kind, "source": typed.source,
                                  "returned_count": typed.returned_count,
                                  "total_count": typed.total_count,
                                  "values": sorted(typed.values),
                                  "derived": sorted(typed.derived)}))
        if typed.derived:
            result.trace.append(tracing.ir_event(tracing.DERIVED_METRICS,
                                                 {"derived": typed.derived}))

        # -- interpretation + answer (one call) ---------------------------------
        if self.answers is None:
            result.fail(PipelineError.STAGE_UNAVAILABLE, "no answer service is configured")
            return
        mark = time.perf_counter()
        final = self.answers.answer_interpreted(
            request.question, to_interpreted(typed, request.question, result.grounded_plan),
            log_to=calls, meaning=_meaning(ir))
        result.answer = final
        result.trace.extend(getattr(final, "trace", []) or [])
        answer = getattr(final, "answer", None)
        interpretation = getattr(answer, "interpretation", None)
        result.trace.append(tracing.ir_event(tracing.INTERPRETED,
                                             {"interpretation": interpretation},
                                             ok=interpretation is not None))
        result.trace.append(tracing.stage_completed(Stage.ANSWER, _ms(mark),
                                                    ok=bool(final.text)))
        result.stages_run.append(Stage.ANSWER.value)
        failure = getattr(getattr(final, "failure", None), "value", None)
        if failure == "MODEL_UNAVAILABLE":
            # No answer from the main model. The deterministic fallback is for
            # an answer the validator rejected, never for a missing model (§52).
            result.answer = None
            result.fail(PipelineError.MAIN_MODEL_UNAVAILABLE,
                        f"answer: {final.failure_detail or 'no answer was produced'}")
            return
        if not final.text:
            result.fail(PipelineError.ANSWER_FAILED,
                        final.failure_detail or "no answer was produced")
            return
        result.success = True
        if parts.conversations is not None:
            try:
                parts.conversations.save(request.conversation_id, ir,
                                         {"kind": typed.kind, "rows": typed.returned_count})
            except Exception:                           # noqa: BLE001
                log.warning("could not save conversation state", exc_info=True)

    # =====================================================================
    def _execute(self, ir: Any, capability: Any, result: PipelineResult,
                 calls: StageLog) -> TypedResult | None:
        parts = self.parts
        caps = capability.capabilities
        typed: TypedResult | None = None
        schema_facts: TypedResult | None = None

        if "schema_query" in caps and ir.schema:
            if parts.schema_engine is None:
                result.fail(PipelineError.STAGE_UNAVAILABLE, "no schema engine is configured")
                return None
            resolved = self._resolve_schema(ir, result, calls)
            if resolved is None:
                return None
            mark = time.perf_counter()
            schema_facts = parts.schema_engine.run(ir.schema, resolved)
            self._subplan(result, "schema", schema_facts, mark)

        if "operational_context" in caps:
            if parts.operational_engine is None:
                result.fail(PipelineError.STAGE_UNAVAILABLE, "no operational engine")
                return None
            objects: list[str] = []
            if ir.entities:
                found = self._discover(ir, [(e.ref, e.concept, e.role) for e in ir.entities],
                                       result, calls, need_fact=False)
                if found is None:
                    return None
                objects = [found.objects[e.ref] for e in ir.entities]
            mark = time.perf_counter()
            typed = parts.operational_engine.run(objects)
            self._subplan(result, "operational", typed, mark)

        elif "history_query" in caps:
            typed = self._history(ir, result, calls)
            if typed is None:
                return None

        elif "record_search" in caps:
            typed = self._search(ir, result)
            if typed is None:
                return None

        elif "record_query" in caps:
            typed = self._records(ir, result, calls)
            if typed is None:
                if result.error is PipelineError.MAIN_MODEL_UNAVAILABLE:
                    return None
                # "Who is Naman Jain in our company?": no single kind of record
                # fits "person", but a name was given. Look the name up in
                # every kind of record instead of declining.
                if self._is_name_lookup(ir) and parts.search_engine is not None:
                    typed = self._search(ir, result)
                    if typed is not None:
                        result.success, result.error, result.error_detail = False, None, ""
                if typed is None:
                    return None
            elif not typed.rows and typed.kind in ("records", "single_record") \
                    and self._is_name_lookup(ir) and parts.search_engine is not None:
                # Nothing by that name in the one kind of record grounding
                # picked for a vague word ("person"): the name may be another
                # kind. Widen before answering "not found".
                wider = self._search(ir, result)
                if wider is not None and wider.rows:
                    typed = wider

        if typed is not None and "RUNTIME_SCHEMA" in ir.sources and schema_facts is None \
                and result.grounded_plan is not None:
            # A structure question answered through the records ("which field
            # stores the outcome, and how many have Offer Received"): the
            # fields grounding verified are the structural answer.
            schema_facts = self._grounded_fields(result.grounded_plan)
        if typed is None:
            typed = schema_facts
        elif schema_facts is not None:
            # Mixed: record facts plus schema facts, one answer.
            for i, row in enumerate(schema_facts.rows[:20]):
                typed.values[f"schema_fact_{i + 1}"] = "; ".join(
                    f"{k}={v}" for k, v in row.items() if v not in (None, ""))
            typed.values.update({f"schema_{k}": v for k, v in schema_facts.values.items()})
        if typed is None:
            result.fail(PipelineError.STAGE_UNAVAILABLE,
                        f"no executor for capabilities {caps}")
        return typed

    # -- discovery / linking for non-record questions ---------------------------
    def _discover(self, ir: Any, entities: list[tuple[str, str, str]],
                  result: PipelineResult, calls: StageLog, *, need_fact: bool,
                  extra_terms: dict[str, list[str]] | None = None) -> Any:
        mark = time.perf_counter()
        found = self.parts.grounder.discover(ir.question, entities, log_to=calls,
                                             need_fact=need_fact, extra_terms=extra_terms)
        result.trace.append(tracing.ir_event(
            tracing.IR_GROUNDED, {"discovery": found.objects, "failures": found.failures},
            ok=not (found.unavailable or found.failures), duration_ms=_ms(mark)))
        if found.unavailable:
            result.fail(PipelineError.MAIN_MODEL_UNAVAILABLE, f"discovery: {found.unavailable}")
            return None
        if found.failures:
            result.fail(PipelineError.SCHEMA_LINKING_FAILED, found.failures[0]["detail"])
            return None
        return found

    def _link(self, question: str, shim: Any, objects: dict[str, str], slots: list[Any],
              result: PipelineResult, calls: StageLog) -> dict[str, Any] | None:
        mark = time.perf_counter()
        linked = self.parts.grounder.link(question, shim, objects, slots, {}, log_to=calls)
        result.trace.append(tracing.ir_event(
            tracing.IR_GROUNDED,
            {"linking": {sid: f"{b.object}.{b.field}" for sid, b in linked["bindings"].items()},
             "failures": linked["failures"]},
            ok=not (linked.get("unavailable") or linked["failures"]), duration_ms=_ms(mark)))
        if linked.get("unavailable"):
            result.fail(PipelineError.MAIN_MODEL_UNAVAILABLE, f"linking: {linked['unavailable']}")
            return None
        if linked["failures"]:
            result.fail(PipelineError.SCHEMA_LINKING_FAILED, linked["failures"][0]["detail"])
            return None
        return linked

    def _resolve_schema(self, ir: Any, result: PipelineResult, calls: StageLog
                        ) -> list[dict[str, Any]] | None:
        """Which object (and field) each schema ask means: the main model decides."""
        from salesforce.schema_linking.ir_grounder import Slot
        grounder = self.parts.grounder
        entities: list[tuple[str, str, str]] = []
        extra: dict[str, list[str]] = {}
        for i, ask in enumerate(ir.schema):
            concept = ask.object_concept or ask.field_concept
            if not concept:
                continue
            entities.append((f"a{i}", concept, "primary"))
            if ask.kind in _FIELD_ASKS and ask.field_concept:
                # Evidence: objects with a field labelled exactly like the phrase.
                phrase = " ".join(p for p in (ask.object_concept, ask.field_concept) if p)
                labels = []
                for hit in grounder.schema.search_fields(phrase, None, 10):
                    if hit.get("matched_by") == "exact_label":
                        row = grounder.schema.repo.get_object(hit["object_api_name"]) or {}
                        labels.append(row.get("label") or hit["object_api_name"])
                if labels:
                    extra[f"a{i}"] = list(dict.fromkeys(labels))[:3]
            if ask.kind == "relationship_between" and ask.other_object_concept:
                entities.append((f"a{i}b", ask.other_object_concept, "related"))
        resolved: list[dict[str, Any]] = [{} for _ in ir.schema]
        if not entities:
            return resolved
        found = self._discover(ir, entities, result, calls, need_fact=False,
                               extra_terms=extra)
        if found is None:
            return None
        slots, shim = [], {}
        for i, ask in enumerate(ir.schema):
            resolved[i]["object"] = found.objects.get(f"a{i}")
            resolved[i]["other"] = found.objects.get(f"a{i}b")
            concept = ask.object_concept or ask.field_concept
            shim[f"a{i}"] = (concept or "", "primary")
            if ask.kind in _FIELD_ASKS and ask.field_concept and resolved[i]["object"]:
                slots.append(Slot(id=f"S{i}", entity=f"a{i}", concept=ask.field_concept,
                                  roles=[f"schema question: {ask.kind}"]))
        if slots:
            objects = {f"a{i}": r["object"] for i, r in enumerate(resolved) if r.get("object")}
            linked = self._link(ir.question, _Entities(shim), objects, slots, result, calls)
            if linked is None:
                return None
            for i, _ in enumerate(ir.schema):
                b = linked["bindings"].get(f"S{i}")
                if b is not None:
                    others = [c.api_name for c in grounder.retriever.field_candidates(
                        b.object, ir.schema[i].field_concept or "")[:5] if c.api_name != b.field]
                    resolved[i]["field"] = b.field
                    resolved[i]["field_options"] = [b.field] + others
        result.stages_run.append(Stage.SCHEMA_LINKING.value)
        return resolved

    def _history(self, ir: Any, result: PipelineResult, calls: StageLog
                 ) -> TypedResult | None:
        from salesforce.schema_linking.ir_grounder import Slot
        parts = self.parts
        if parts.history_engine is None or ir.primary is None:
            result.fail(PipelineError.STAGE_UNAVAILABLE, "no history engine")
            return None
        primary = ir.primary
        found = self._discover(ir, [(primary.ref, primary.concept, "primary")], result,
                               calls, need_fact=False)
        if found is None:
            return None
        obj = found.objects[primary.ref]
        field = None
        concept = _history_field(ir)
        if concept:
            linked = self._link(ir.question, _Entities({primary.ref: (primary.concept, "primary")}),
                                {primary.ref: obj},
                                [Slot(id="S0", entity=primary.ref, concept=concept,
                                      roles=["field whose change history is asked"])],
                                result, calls)
            if linked is None:
                return None
            binding = linked["bindings"].get("S0")
            field = binding.field if binding else None
        record_name = next((f.right.value for f in ir.filters
                            if f.concept.lower() in _NAME_CONCEPTS
                            and f.right.type == "literal"), None)
        period = None
        if ir.temporal:
            from salesforce.schema_linking.temporal import parse as parse_period
            period = parse_period(ir.temporal[0].expression)
        mark = time.perf_counter()
        typed = parts.history_engine.run(obj, record_name, field, period=period)
        self._subplan(result, "history", typed, mark)
        return typed

    # -- records ------------------------------------------------------------------
    @classmethod
    def _is_name_lookup(cls, ir: Any) -> bool:
        """"Who is X?": one kind of record, identified only by a name.

        Anything more -- a second entity, a measure, a grouping, another
        condition -- is a different question, and answering it with a name
        search would answer something that was not asked.
        """
        return (len(ir.entities) == 1 and len(ir.filters) == 1
                and cls._named_value(ir) is not None
                and not (ir.measures or ir.dimensions or ir.existence or ir.temporal
                         or ir.comparison or ir.derived))

    @staticmethod
    def _named_value(ir: Any) -> str | None:
        if ir.search_value:
            return str(ir.search_value)
        return next((str(f.right.value) for f in ir.filters
                     if f.concept.lower() in _NAME_CONCEPTS and f.right.type == "literal"
                     and isinstance(f.right.value, str) and f.right.value.strip()), None)

    def _search(self, ir: Any, result: PipelineResult) -> TypedResult | None:
        engine = self.parts.search_engine
        value = self._named_value(ir)
        if engine is None or not value:
            result.fail(PipelineError.STAGE_UNAVAILABLE,
                        "no record search engine or no value to search for")
            return None
        mark = time.perf_counter()
        typed = engine.run(value)
        if not typed.rows:
            typed = engine.run(value, contains=True)       # "naman" -> "Naman Jain"
        typed.entity_label = "records"
        self._subplan(result, "record_search", typed, mark)
        result.stages_run.append(Stage.RECORD_QUERY.value)
        return typed

    def _records(self, ir: Any, result: PipelineResult, calls: StageLog
                 ) -> TypedResult | None:
        parts = self.parts
        if parts.record_engine is None:
            result.fail(PipelineError.STAGE_UNAVAILABLE, "no record engine is configured")
            return None
        if parts.planner is None:
            result.fail(PipelineError.STAGE_UNAVAILABLE, "no planning stage is configured")
            return None

        # -- discovery + linking ------------------------------------------------
        mark = time.perf_counter()
        grounded = parts.grounder.ground(ir, log_to=calls)
        elapsed = _ms(mark)
        result.grounded_plan = grounded
        result.trace.append(tracing.ir_event(
            tracing.IR_GROUNDED,
            {**grounded.summary(),
             "bindings": {sid: f"{b.object}.{b.field}" for sid, b in grounded.bindings.items()},
             "semantic": grounded.semantic},
            ok=grounded.grounded, duration_ms=elapsed))
        result.trace.append(tracing.stage_completed(Stage.SCHEMA_LINKING, elapsed,
                                                    ok=grounded.grounded))
        if not grounded.grounded:
            failure = grounded.failures[0] if grounded.failures else {}
            detail = failure.get("detail") or "no grounded plan"
            if failure.get("code") == "MODEL_UNAVAILABLE":
                result.fail(PipelineError.MAIN_MODEL_UNAVAILABLE, detail)
            else:
                result.fail(PipelineError.SCHEMA_LINKING_FAILED, detail)
            return None
        result.stages_run.append(Stage.SCHEMA_LINKING.value)
        if grounded.semantic.get("linking_skipped") and "linking" in result.applicable_stages:
            result.applicable_stages.remove("linking")      # nothing to link

        # -- planning (main model) and plan compilation (code) -------------------
        from .planner import compile_plan
        mark = time.perf_counter()
        plan, call = parts.planner.plan(ir.question, grounded, log_to=calls)
        if plan is None:
            if call.failure == "unavailable":
                result.fail(PipelineError.MAIN_MODEL_UNAVAILABLE, f"planning: {call.error}")
            else:
                result.fail(PipelineError.PLANNING_FAILED, f"planning: {call.error}")
            return None
        errors = compile_plan(plan, grounded, parts.grounder.schema, parts.grounder.verifier)
        result.trace.append(tracing.ir_event(
            tracing.PLAN_CREATED, {"plan": plan, "compiled": grounded.summary(),
                                   "errors": errors},
            ok=not errors, duration_ms=_ms(mark)))
        if errors:
            result.fail(PipelineError.PLANNING_FAILED, "; ".join(errors[:3]))
            return None
        if grounded.periods:
            result.trace.append(tracing.ir_event(tracing.TEMPORAL_NORMALIZED,
                                                 {"periods": grounded.periods}))

        # -- execution ---------------------------------------------------------------
        mark = time.perf_counter()
        typed = parts.record_engine.run(grounded)
        elapsed = _ms(mark)
        result.trace.append(tracing.ir_event(
            tracing.QUERY_DECOMPOSED,
            {"kind": typed.kind, "subplans": [p["label"] for p in typed.subplans]}))
        for subplan in typed.subplans:
            result.trace.append(tracing.ir_event(
                tracing.SUBPLAN_EXECUTED,
                {"label": subplan["label"], "rows": subplan["rows"], "params": subplan["params"]},
                duration_ms=int(subplan["ms"])))
        result.trace.append(tracing.stage_completed(Stage.RECORD_QUERY, elapsed,
                                                    ok=typed.success))
        if not typed.success:
            result.records = typed
            result.fail(PipelineError.RECORD_QUERY_FAILED,
                        f"{typed.error}: {typed.error_detail}".strip(": "))
            return None
        result.stages_run.append(Stage.RECORD_QUERY.value)
        return typed

    def _grounded_fields(self, g: Any) -> TypedResult:
        rows, seen = [], set()
        refs = [f.left for f in g.filters] + [m.ref for m in g.measures if m.ref] + \
            [d.ref for d in g.dimensions] + [a[0] for a in g.attributes]
        for ref in refs:
            if ref is None or not ref.field or ref.field == "Id":
                continue
            obj = g.objects.get(ref.entity, ref.entity)
            if (obj, ref.field) in seen:
                continue
            seen.add((obj, ref.field))
            rows.append({"object": obj, "field": ref.field, "label": ref.label,
                         "type": ref.data_type})
        return TypedResult(kind="schema_facts", source="runtime_schema", rows=rows,
                           returned_count=len(rows))

    @staticmethod
    def _subplan(result: PipelineResult, label: str, typed: TypedResult,
                 mark: float) -> None:
        result.trace.append(tracing.ir_event(
            tracing.SUBPLAN_EXECUTED, {"label": label, "kind": typed.kind,
                                       "rows": typed.returned_count or len(typed.rows)},
            duration_ms=_ms(mark)))


def _meaning(ir: Any) -> dict[str, Any]:
    """The question's meaning in its own business words, for interpretation."""
    out: dict[str, Any] = {"operation": ir.output_mode,
                           "entities": [e.concept for e in ir.entities]}
    if ir.measures:
        out["measures"] = [f"{m.op} {m.concept or 'records'}" for m in ir.measures]
    if ir.dimensions:
        out["grouped_by"] = [d.concept for d in ir.dimensions]
    if ir.comparison:
        out["sides"] = [s.label for s in ir.comparison]
    return out
