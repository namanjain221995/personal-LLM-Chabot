"""One question in, one routed result out.

This is the wiring, not the work. Every stage is a port supplied by whoever
builds the pipeline: the knowledge bundle owns discovery, the runtime schema
owns linking, DuckDB owns records. Holding them behind ports is what lets the
same dispatch table run in the orchestrator, in the knowledge service, or in a
test with none of them present.

A stage the route asked for and the deployment cannot provide is reported as
STAGE_UNAVAILABLE. It is never quietly skipped -- an answer assembled from
three of four stages would otherwise be indistinguishable from a complete one.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from . import tracing
from .dispatch import Stage, UnknownRoute, stages_for
from .models import PipelineError, PipelineRequest, PipelineResult

log = logging.getLogger(__name__)

# Route names that mean a record query. Used only to explain a missing port.
_RECORD_STAGE_OWNER = "record_query.RecordQueryService"


# What the extraction's own words mean for the record layer. A question that
# asks "how many" must run a COUNT, not a listing whose length the answer layer
# would then have to describe.
_ACTIONS = {
    "count": "count", "how_many": "count",
    "aggregate": "aggregate", "sum": "aggregate", "average": "aggregate",
    "compare": "compare", "comparison": "compare",
    "search": "search", "find": "search",
    "list": "retrieve", "retrieve": "retrieve", "show": "retrieve",
}


def _accepts(function: Any, name: str) -> bool:
    import inspect
    try:
        return name in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


_RETURN_MODES = {"count": "count", "aggregate": "aggregate",
                 "records": "retrieve", "single_record": "retrieve"}


def _operation_for(request: Any, extraction: Any) -> str:
    """The record operation this question needs.

    An explicit request wins. Otherwise the extraction decides, because it is
    the only thing that read the question -- and falling back to "retrieve"
    turns every count into a list.
    """
    if getattr(request, "operation", None):
        return str(request.operation)
    # return_mode is the extraction's direct statement of the answer's shape.
    mode = getattr(extraction, "return_mode", None)
    if mode in _RETURN_MODES:
        return _RETURN_MODES[mode]
    for key in ("action", "intent"):
        value = str(getattr(extraction, key, "") or "").strip().lower()
        if value in _ACTIONS:
            return _ACTIONS[value]
        for token, operation in _ACTIONS.items():
            if token in value:
                return operation
    return "retrieve"


def _route_of(extraction: Any) -> Any:
    """`graphrag.routing.route_for`, imported only if it is reachable.

    The knowledge bundle and this package live in different source trees and
    are mounted into different containers. A hard import would make the
    pipeline unusable in a process that has records but no bundle, which is
    exactly the process that needs it most.
    """
    try:
        from graphrag.routing import route_for
    except ImportError:
        from .dispatch import UnknownRoute as _Unknown
        raise _Unknown(
            "no route was supplied and graphrag.routing is not importable")
    return route_for(extraction)


class SalesforcePipeline:
    def __init__(self, *,
                 environment: str = "",
                 extract: Callable[[str], Any] | None = None,
                 discover: Callable[..., Any] | None = None,
                 linker: Any = None,
                 schema: Any = None,
                 records: Any = None,
                 describe: Callable[[str], Any] | None = None,
                 answers: Any = None,
                 trace_store: Any = None,
                 ir: Any = None) -> None:
        self.environment = environment
        self.extract = extract
        self.discover = discover
        self.linker = linker
        # The runtime schema service, for provenance only. The linker owns the
        # lookups; this answers "which tier served them" in one place rather
        # than threading a cache level through every retrieval call.
        self.schema = schema if schema is not None else getattr(linker, "schema", None)
        self.records = records
        self.describe = describe
        self.answers = answers
        self.trace_store = trace_store
        # Step 8: the semantic-IR path (pipeline.ir_path.IRComponents). When
        # set, every question takes it; the stage-dispatch path below stays for
        # callers that supply their own extraction or route (evaluation).
        self.ir = ir

    # -- entry ------------------------------------------------------------
    def run(self, request: PipelineRequest | str) -> PipelineResult:
        if isinstance(request, str):
            request = PipelineRequest(question=request)
        started = time.perf_counter()
        result = PipelineResult(question=request.question)

        trace_id = self._begin_trace(request)

        if self.ir is not None and request.extraction is None and request.route is None:
            from .ir_path import IRPath
            IRPath(self.ir, self.answers).run(request, result)
            self._record_extraction(trace_id, result.extraction)
            if result.route:
                self._record_route(trace_id, result.route)
            result.duration_ms = self._elapsed(started)
            result.trace.append(tracing.pipeline_completed(result))
            self._finish_trace(trace_id, result)
            return result

        extraction = request.extraction
        if extraction is None and self.extract is not None:
            extraction = self.extract(request.question)
        result.extraction = extraction
        self._record_extraction(trace_id, extraction)

        if extraction is None and request.route is None:
            result.duration_ms = self._elapsed(started)
            result.trace.append(tracing.pipeline_completed(result))
            self._finish_trace(trace_id, result)
            return result.fail(PipelineError.EXTRACTION_UNAVAILABLE,
                               "no extraction and no route was supplied")

        try:
            route = request.route if request.route is not None else _route_of(extraction)
            stages = stages_for(route)
        except UnknownRoute as exc:
            result.duration_ms = self._elapsed(started)
            result.trace.append(tracing.pipeline_completed(result))
            self._finish_trace(trace_id, result)
            return result.fail(PipelineError.UNKNOWN_ROUTE, str(exc))

        result.route = getattr(route, "value", str(route))
        result.stages = [s.value for s in stages]
        result.trace.append(tracing.route_dispatched(route, stages))
        self._record_route(trace_id, result.route)

        for stage in stages:
            if not self._run_stage(stage, request, result, trace_id):
                break
        else:
            result.success = True

        # NONE runs nothing and that is a correct outcome, not a failure: the
        # extraction said the question needs no Salesforce work at all.
        if not stages:
            result.success = True

        result.duration_ms = self._elapsed(started)
        result.trace.append(tracing.pipeline_completed(result))
        self._finish_trace(trace_id, result)
        return result

    # -- stages -------------------------------------------------------------
    def _run_stage(self, stage: Stage, request: PipelineRequest,
                   result: PipelineResult, trace_id: str | None) -> bool:
        handler = {
            Stage.DISCOVERY: self._stage_discovery,
            Stage.SCHEMA_LINKING: self._stage_linking,
            Stage.RECORD_QUERY: self._stage_records,
            Stage.METADATA: self._stage_metadata,
            Stage.ANSWER: self._stage_answer,
        }[stage]
        result.trace.append(tracing.stage_started(stage))
        mark = time.perf_counter()
        try:
            ok, detail = handler(request, result, trace_id)
        except Exception as exc:                       # noqa: BLE001
            log.exception("pipeline stage %s raised", stage.value)
            ok, detail = False, f"{type(exc).__name__}: {exc}"
            result.fail(PipelineError.STAGE_UNAVAILABLE, detail)
        elapsed = self._elapsed(mark)
        result.trace.append(tracing.stage_completed(stage, elapsed, ok=ok,
                                                    detail=detail))
        if ok:
            result.stages_run.append(stage.value)
        return ok

    def _stage_discovery(self, request: PipelineRequest, result: PipelineResult,
                         trace_id: str | None) -> tuple[bool, str]:
        if self.discover is None:
            return self._unavailable(result, Stage.DISCOVERY,
                                     "no discovery port is configured")
        discovery = self.discover(request.question, extraction=result.extraction,
                                  limit=request.limit)
        result.discovery = discovery
        result.trace.extend(getattr(discovery, "trace", []) or [])
        self._record_schema(trace_id, discovery)
        return True, ""

    def _stage_linking(self, request: PipelineRequest, result: PipelineResult,
                       trace_id: str | None) -> tuple[bool, str]:
        if self.linker is None:
            return self._unavailable(result, Stage.SCHEMA_LINKING,
                                     "no schema linker is configured")
        # Discovery's candidates do not reach the linker: it runs its own
        # retrieval against the runtime schema. So DATA_WITH_DISCOVERY differs
        # from DATA_DIRECT only in what the trace records, until the two
        # retrievers are joined. Stated rather than hidden -- an evaluation
        # comparing the two routes would otherwise read the tie as a result.
        # The whole question, not just the extracted concepts: "status" is
        # linked in the context of what was asked (§14). Older linkers that do
        # not take it still work.
        plan = (self.linker.link(result.extraction, question=request.question)
                if _accepts(self.linker.link, "question")
                else self.linker.link(result.extraction))
        result.grounded_plan = plan
        result.trace.extend(getattr(plan, "trace", []) or [])
        self._record_served_from(trace_id, plan)
        if not getattr(plan, "schema_grounded", False):
            failures = getattr(plan, "failures", []) or []
            detail = failures[0].get("detail") if failures else "no grounded plan"
            result.fail(PipelineError.SCHEMA_LINKING_FAILED, detail)
            return False, detail
        return True, ""

    def _stage_records(self, request: PipelineRequest, result: PipelineResult,
                       trace_id: str | None) -> tuple[bool, str]:
        if self.records is None:
            return self._unavailable(
                result, Stage.RECORD_QUERY,
                f"no record query service is configured ({_RECORD_STAGE_OWNER})")
        if result.grounded_plan is None:
            detail = "the record stage ran without a grounded plan"
            result.fail(PipelineError.RECORD_QUERY_FAILED, detail)
            return False, detail
        query = self.records.query_records(
            result.grounded_plan.as_dict(),
            operation=_operation_for(request, result.extraction),
            limit=request.limit,
            group_by_fields=request.group_by_fields or None,
            aggregations=request.aggregations or None)
        result.records = query
        result.trace.extend(getattr(query, "trace", []) or [])
        if not query.success:
            detail = f"{query.error.value if query.error else ''}: {query.error_detail}"
            result.fail(PipelineError.RECORD_QUERY_FAILED, detail.strip(": "))
            return False, detail
        return True, ""

    def _stage_metadata(self, request: PipelineRequest, result: PipelineResult,
                        trace_id: str | None) -> tuple[bool, str]:
        if self.describe is None:
            return self._unavailable(result, Stage.METADATA,
                                     "no describe port is configured")
        described: list[dict[str, Any]] = []
        for component_id in self._components_to_describe(result):
            payload = self.describe(component_id)
            if payload is not None:
                described.append({"component_id": component_id,
                                  "detail": payload})
        result.metadata = described
        if not described:
            detail = "nothing was identified to describe"
            result.fail(PipelineError.METADATA_FAILED, detail)
            return False, detail
        return True, ""

    def _stage_answer(self, request: PipelineRequest, result: PipelineResult,
                      trace_id: str | None) -> tuple[bool, str]:
        if self.answers is None:
            return self._unavailable(result, Stage.ANSWER,
                                     "no answer service is configured")
        if result.records is None:
            detail = "the answer stage ran with no query result"
            result.fail(PipelineError.ANSWER_FAILED, detail)
            return False, detail
        final = self.answers.answer(
            request.question, result.records,
            grounded_plan=result.grounded_plan,
            # The plan the record layer attached. A COUNT and a one-row
            # SELECT are the same shape without it.
            query_plan=getattr(result.records, "plan", None))
        result.answer = final
        result.trace.extend(getattr(final, "trace", []) or [])
        # A fallback answer is still an answer: the user gets verified facts.
        # The stage fails only when there is no text at all.
        if not final.text:
            detail = final.failure_detail or "no answer was produced"
            result.fail(PipelineError.ANSWER_FAILED, detail)
            return False, detail
        return True, ""

    @staticmethod
    def _components_to_describe(result: PipelineResult) -> list[str]:
        """Which components the metadata stage explains.

        Discovery first when it ran, because it ranked them against the
        question. The grounded plan otherwise: its objects are verified, just
        not ranked.
        """
        seen: list[str] = []
        for candidate in getattr(result.discovery, "objects", []) or []:
            component_id = getattr(candidate, "component_id", None) or \
                f"object:{getattr(candidate, 'api_name', '')}"
            if component_id not in seen:
                seen.append(component_id)
        if not seen and result.grounded_plan is not None:
            seen = [f"object:{name}"
                    for name in getattr(result.grounded_plan, "objects", [])]
        return seen

    @staticmethod
    def _unavailable(result: PipelineResult, stage: Stage,
                     reason: str) -> tuple[bool, str]:
        result.trace.append(tracing.stage_skipped(stage, reason))
        result.fail(PipelineError.STAGE_UNAVAILABLE, reason)
        return False, reason

    # -- trace store ---------------------------------------------------------
    # Every call is best-effort. Tracing that can fail a request would make the
    # observability the reason the pipeline stopped working.
    def _begin_trace(self, request: PipelineRequest) -> str | None:
        if self.trace_store is None:
            return None
        try:
            return self.trace_store.begin(request.question,
                                          test_case_id=request.test_case_id,
                                          request_id=request.request_id,
                                          environment=self.environment or None)
        except Exception:                               # noqa: BLE001
            log.warning("could not open a trace", exc_info=True)
            return None

    def _record_extraction(self, trace_id: str | None, extraction: Any) -> None:
        if not trace_id:
            return
        try:
            self.trace_store.record_extraction(
                trace_id, extraction,
                model=getattr(extraction, "model", "") or "",
                endpoint=getattr(extraction, "endpoint", "") or "")
        except Exception:                               # noqa: BLE001
            log.warning("could not record extraction", exc_info=True)

    def _record_route(self, trace_id: str | None, route: str) -> None:
        if not trace_id:
            return
        try:
            self.trace_store.record_route(trace_id, route)
        except Exception:                               # noqa: BLE001
            log.warning("could not record route", exc_info=True)

    def _record_served_from(self, trace_id: str | None, plan: Any) -> None:
        """Which runtime-schema tier answered, recorded once per request.

        Asked of the primary object because that is the object every later
        stage reads. Tracing must not change what it observes, so this reports
        the tier rather than warming it.
        """
        if not trace_id or self.schema is None:
            return
        primary = getattr(plan, "primary_object", None)
        if not primary:
            return
        try:
            self.trace_store.record_schema_source(trace_id,
                                                  self.schema.served_from(primary))
        except Exception:                               # noqa: BLE001
            log.warning("could not record the schema tier", exc_info=True)

    def _record_schema(self, trace_id: str | None, discovery: Any) -> None:
        if not trace_id or discovery is None:
            return
        try:
            objects = [getattr(c, "api_name", "") for c in
                       getattr(discovery, "objects", []) or []]
            fields = [getattr(c, "component_id", "").split(":", 1)[-1]
                      for c in getattr(discovery, "fields", []) or []]
            self.trace_store.record_schema(
                trace_id, objects=objects, fields=fields,
                served_from=getattr(discovery, "served_from", None))
        except Exception:                               # noqa: BLE001
            log.warning("could not record schema candidates", exc_info=True)

    def _finish_trace(self, trace_id: str | None,
                      result: PipelineResult) -> None:
        if not trace_id:
            return
        try:
            self.trace_store.add_events(trace_id, result.trace)
            self.trace_store.finish(
                trace_id, status="ok" if result.success else "error",
                discovery=result.discovery,
                total_duration_ms=result.duration_ms,
                grounded_plan=result.grounded_plan,
                records=result.records,
                answer=result.answer,
                signals={"route": result.route, "stages": result.stages,
                         "stages_run": result.stages_run,
                         "applicable_model_stages": result.applicable_stages,
                         "model_called": {c.stage: c.model_called
                                          for c in result.model_stages}})
        except TypeError:
            # An older store without the step 4/5 keywords. Record what it can
            # take rather than losing the whole trace.
            try:
                self.trace_store.finish(
                    trace_id, status="ok" if result.success else "error",
                    discovery=result.discovery,
                    total_duration_ms=result.duration_ms,
                    signals={"route": result.route})
            except Exception:                           # noqa: BLE001
                log.warning("could not close trace %s", trace_id, exc_info=True)
        except Exception:                               # noqa: BLE001
            log.warning("could not close trace %s", trace_id, exc_info=True)

    @staticmethod
    def _elapsed(mark: float) -> int:
        return int((time.perf_counter() - mark) * 1000)
