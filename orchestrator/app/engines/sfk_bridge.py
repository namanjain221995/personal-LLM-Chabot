"""The one place the orchestrator touches the new Salesforce pipeline.

    /chat  ->  sfk_bridge.try_answer()  ->  SalesforcePipeline.run()

Nothing else in this application imports `pipeline`, `record_query`, `answer`,
`salesforce` or `graphrag`. One seam means one thing to reason about when the
new path misbehaves, and one thing to remove if it is ever withdrawn.

EXACTLY ONE PIPELINE RUNS PER REQUEST. `try_answer` returns an Outcome when the
new pipeline answered, and None when it did not. On None the caller proceeds to
the existing engine as it always has; on an Outcome the caller skips it
entirely, so no old intent classifier, planner, SQL writer or live-SOQL fallback
executes for a request the new pipeline served.

The new pipeline declines rather than guesses. It declines a conversational
question (route NONE), a concept it could not ground, a stage the deployment
cannot provide, and a record query that failed. Those requests are the old
engine's, and the old engine runs its own extraction for them -- a second model
call this bridge cannot avoid without answering a question it has no facts for.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

from .sf_intel import Outcome

log = logging.getLogger(__name__)

#: Cutover switch. OFF means this module does nothing at all and `/chat`
#: behaves exactly as it did before it existed.
ENABLED = os.environ.get("SFK_PIPELINE_ENABLED", "false").strip().lower() == "true"

#: Where the new pipeline's source is mounted. Compose puts it here; a
#: deployment without the mounts simply has no new pipeline and this stays
#: unavailable. Overridable so the same code can be exercised against a
#: different layout without the paths being faked.
_SOURCE_ROOTS = tuple(
    part for part in (os.environ.get("SFK_SOURCE_ROOTS") or "").split(os.pathsep)
    if part) or ("/app/sfsrc", "/app/vendor")

_lock = threading.Lock()
_state: dict[str, Any] = {"pipeline": None, "error": None, "built": False,
                          "origin": None}


def available() -> bool:
    return ENABLED and _build() is not None


def _knowledge_root() -> str:
    return os.environ.get("SALESFORCE_KNOWLEDGE_ROOT", "/app/salesforce_knowledge")


def _environment() -> str:
    """Canonical first, the old pipeline's variable as an alias.

    `SF_ENVIRONMENT` defaults to the literal string "unknown" in compose, which
    is not an environment -- taking it would write "unknown" into every trace.
    """
    for name in ("SALESFORCE_ENVIRONMENT", "SF_ENVIRONMENT"):
        value = (os.environ.get(name) or "").strip()
        if value and value != "unknown":
            return value
    return "production"


def _record_origin(records: Any) -> dict[str, Any]:
    """Which org the RECORDS came from, read off the records themselves.

    Salesforce stores one `Organization` row per org, and the sync copies it
    into the warehouse like any other object. Its `IsSandbox` is the org's own
    answer, so it cannot drift from the data the way a config value can.

    Only the sandbox NAME comes from configuration: `Organization` does not
    carry it, and the login URL does (`techsara--preprod.sandbox...`). The URL
    is trusted for the name only when the data already says "sandbox" -- it
    can never turn a sandbox into production or the reverse.

    Falls back to configuration, and says so, when the row cannot be read.
    """
    import re

    fallback = {"environment": _environment(), "environment_source": "config",
                "is_sandbox": None, "org_id": None, "instance": None}
    try:
        row = records.executor.connection.execute(
            'SELECT "Id", "IsSandbox", "InstanceName" FROM main."Organization"'
            " LIMIT 1").fetchone()
    except Exception:                                   # noqa: BLE001
        log.warning("cannot read main.Organization; labelling records from "
                    "configuration", exc_info=True)
        return fallback
    if row is None:
        return fallback
    org_id, is_sandbox, instance = row
    sandbox = str(is_sandbox).strip().lower() in ("true", "1")
    if sandbox:
        match = re.search(r"--([a-z0-9]+)\.sandbox\.",
                          os.environ.get("SF_LOGIN_URL", ""), re.I)
        environment = match.group(1).lower() if match else "sandbox"
    else:
        environment = "production"
    return {"environment": environment, "environment_source": "warehouse",
            "is_sandbox": sandbox, "org_id": org_id, "instance": instance}


def _records_environment() -> str:
    origin = _state.get("origin") or {}
    return origin.get("environment") or _environment()


def _build() -> Any:
    """The one pipeline, built once.

    Built lazily rather than at import: the runtime schema opens SQLite and the
    record layer opens DuckDB, and a deployment with the flag off must pay for
    neither. A build failure is remembered so a broken mount does not retry on
    every request.
    """
    if _state["built"]:
        return _state["pipeline"]
    with _lock:
        if _state["built"]:
            return _state["pipeline"]
        _state["built"] = True
        try:
            _state["pipeline"] = _construct()
            log.info("new Salesforce pipeline ready (environment=%s)",
                     _environment())
        except Exception as exc:                        # noqa: BLE001
            # Never raises to the caller. A pipeline that cannot be built means
            # the old engine answers, which is the behaviour without this file.
            _state["error"] = f"{type(exc).__name__}: {exc}"
            _state["pipeline"] = None
            log.warning("new Salesforce pipeline unavailable: %s",
                        _state["error"], exc_info=True)
    return _state["pipeline"]


def _construct() -> Any:
    import sys

    for root in _SOURCE_ROOTS:
        if os.path.isdir(root) and root not in sys.path:
            # The mounts are not installed packages. The repository has no
            # workspace packaging for them, so this is the convention that
            # exists rather than one invented here.
            sys.path.insert(0, root)

    from answer.config import load_answer_config
    from answer.service import AnswerService
    from graphrag.trace_store import TraceStore
    from observability.config import load_tracing_config
    from pipeline import SalesforcePipeline
    from record_query.service import RecordQueryService
    from salesforce.runtime_schema.service import RuntimeSchemaService
    from salesforce.schema_linking.config import load_schema_linking_config
    from salesforce.schema_linking.linker import SchemaLinker

    root = _knowledge_root()
    schema = RuntimeSchemaService(root=root)
    schema.start()

    tracing = load_tracing_config(root)
    store = None
    if tracing.enabled:
        try:
            store = TraceStore(tracing.database_path)
        except Exception:                               # noqa: BLE001
            # Losing traces must not lose answers.
            log.warning("new trace store unavailable at %s",
                        tracing.database_path, exc_info=True)

    _state["models"] = _validate_models()
    records = RecordQueryService(schema, root=root)
    _state["origin"] = _record_origin(records)
    log.info("records come from %s (source=%s, org=%s)",
             _state["origin"]["environment"],
             _state["origin"]["environment_source"], _state["origin"]["org_id"])

    # The linker's config MUST come from the knowledge root. Without it
    # SchemaLinker looked for ./salesforce_knowledge relative to the
    # process's working directory, found nothing, and silently ran on
    # built-in defaults -- including max_tokens=300, which cut the
    # semantic answer off mid-JSON on ~1 question in 3. Found live.
    linker = SchemaLinker(schema, load_schema_linking_config(root),
                          offline=_linker_offline(),
                          business_notes=_business_notes(root))
    ir = _ir_components(schema, records, linker, root) if _ir_path() else None
    _state["ir_path"] = ir is not None

    return SalesforcePipeline(
        # The trace records where the ANSWER's data came from, which is the
        # warehouse's org -- not the schema's, and not whatever config says.
        environment=_records_environment(),
        extract=_extractor(),
        discover=_discovery(),
        linker=linker,
        schema=schema,
        records=records,
        answers=AnswerService(config=load_answer_config(root)),
        trace_store=store,
        ir=ir)


def _ir_path() -> bool:
    """Step 8: the semantic-IR path answers every question (default).

    SFK_IR_PATH=false returns to the step-7 stage path, for comparison.
    """
    return (os.environ.get("SFK_IR_PATH", "true").strip().lower() != "false")


def _ir_components(schema: Any, records: Any, linker: Any, root: str) -> Any:
    """Compiler, grounder, planner and executors for the semantic-IR path.

    Every semantic stage -- intent+routing, discovery, linking, planning,
    interpretation+answer -- runs on the main model (spec: main model in every
    stage). The grounder reuses the linker's retriever and verifier as
    evidence sources and technical checks, never as decision makers.
    """
    from model_roles import stage_model
    from pipeline.compiler import IntentCompiler
    from pipeline.conversation import ConversationStore
    from pipeline.engines.history import HistoryEngine
    from pipeline.engines.operational import OperationalEngine
    from pipeline.engines.records import RecordEngine
    from pipeline.engines.schema import SchemaEngine
    from pipeline.engines.search import SearchEngine
    from pipeline.ir_path import IRComponents
    from pipeline.planner import LogicalPlanner
    from salesforce.schema_linking.ir_grounder import IRGrounder

    intent = stage_model("intent", root)
    linking = stage_model("linking", root) or intent
    planning = stage_model("planning", root) or intent
    grounder = IRGrounder(schema, linker.retriever, linker.verifier,
                          endpoint=linking.endpoint, model=linking.model,
                          business_notes=_business_notes(root))
    conversations = None
    try:
        conversations = ConversationStore(
            os.environ.get("SFK_CONVERSATION_DB",
                           os.path.join(root, "runtime_schema", "db", "conversations.sqlite")))
    except Exception:                                   # noqa: BLE001
        log.warning("conversation state unavailable; follow-ups will not "
                    "inherit", exc_info=True)
    log.info("semantic-IR path on %s (intent+routing) / %s (discovery+linking) / "
             "%s (planning)", intent.model, linking.model, planning.model)
    return IRComponents(
        compiler=IntentCompiler(intent.endpoint, intent.model),
        grounder=grounder,
        planner=LogicalPlanner(planning.endpoint, planning.model, schema, linker.verifier),
        record_engine=RecordEngine(records),
        schema_engine=SchemaEngine(schema, linker.retriever, grounder.graph),
        operational_engine=OperationalEngine(records, linker.retriever),
        history_engine=HistoryEngine(records, linker.retriever),
        search_engine=SearchEngine(records, schema),
        conversations=conversations)


def _linker_offline() -> bool:
    """Offline (retrieval-score only) is no longer the default.

    `pipeline.linking.mode` in schema_config.yaml decides -- `llm_assisted` as
    shipped -- and SFK_LINKER_OFFLINE=true still forces the old behaviour, for
    comparison or when the main model is down for maintenance.
    """
    override = (os.environ.get("SFK_LINKER_OFFLINE") or "").strip().lower()
    if override in ("true", "false"):
        return override == "true"
    try:
        import yaml
        from pathlib import Path
        raw = yaml.safe_load((Path(_knowledge_root()) / "config" /
                              "schema_config.yaml").read_text(encoding="utf-8")) or {}
        mode = ((raw.get("pipeline") or {}).get("linking") or {}).get("mode")
    except Exception:                                   # noqa: BLE001
        mode = None
    return mode == "offline"


def _business_notes(root: str) -> dict:
    """The same hand-reviewed vocabulary the runtime schema loaded."""
    try:
        from salesforce.runtime_schema.business_knowledge import load_business_notes
        from salesforce.runtime_schema.config import load_config
        path = load_config(root).business_knowledge_path
    except Exception:                                   # noqa: BLE001
        return {}
    if path and not os.path.isabs(path):
        for base in (os.getcwd(), os.path.dirname(os.path.abspath(root))):
            candidate = os.path.join(base, path)
            if os.path.isfile(candidate):
                path = candidate
                break
    return load_business_notes(path)


def _validate_models() -> dict:
    """§47: every semantic stage on one main deployment, and it answers.

    Run once when the pipeline is built, not on the first user request. A
    broken main endpoint is reported on /health; it does not stop the build,
    but every question then fails as MAIN_MODEL_UNAVAILABLE: no stage is
    decided without the main model.
    """
    try:
        from model_roles import probe, resolve_role, validate
    except ImportError:
        return {"ok": False, "detail": "model_roles not importable"}
    report = validate(_knowledge_root())
    reachable, detail = probe(resolve_role("main", _knowledge_root()))
    report["main_reachable"] = reachable
    report["main_detail"] = detail
    if report["divergent_semantic_stages"]:
        log.warning("semantic stages NOT on the main model: %s",
                    report["divergent_semantic_stages"])
    if not reachable:
        log.error("main model unreachable at startup: %s", detail)
    return report


def _extractor():
    """Step 1, on the model the `intent` stage resolves to -- the main 35B.

    Measured on the identical task the 35B beat the 8B on every axis (intent
    11/12 vs 9/12, roles 12/12 vs 0/12, p50 2.0 s vs 5.4 s), and roles are
    exactly what went wrong live: "Jayesh Prajapati" read as an object, "May
    2026 month" as a field. SFK_EXTRACT_URL / SFK_EXTRACT_MODEL still override
    it; `model_roles.validate()` then reports the divergence on /health.
    """
    from graphrag.extract import extract
    from model_roles import stage_model

    resolved = stage_model("intent", _knowledge_root())
    endpoint, model = resolved.endpoint, resolved.model
    log.info("stage 1 extraction on %s (%s, source=%s)", model,
             resolved.as_dict()["endpoint"], resolved.source)

    def run(question: str):
        return extract(question, endpoint=endpoint, model=model)

    return run


def _discovery():
    """Step 3, from the knowledge bundle -- when the bundle is mounted.

    Absent, the pipeline reports DISCOVERY as unavailable and a route needing
    it declines to this bridge's caller. That is deliberate: an answer built
    from three of four stages must not read as a complete one.
    """
    directory = os.environ.get("SFK_BUNDLE_DIR", "/data/knowledge")
    if not os.path.isdir(directory):
        log.info("knowledge bundle absent at %s; discovery routes will "
                 "fall through to the existing engine", directory)
        return None
    try:
        from graphrag.resolver import Bundle
    except ImportError:
        return None
    bundle = Bundle(directory)

    # Semantic search needs an embedding endpoint. Without one the resolver
    # falls back to its own default, which in this deployment is not listening
    # -- so the endpoint is read from the same variable the orchestrator
    # already uses, and semantic search is switched OFF when it is blank
    # rather than left to fail per request.
    embed = (os.environ.get("SFK_EMBED_URL")
             or os.environ.get("EMBED_BASE_URL") or "")
    rerank_url = (os.environ.get("SFK_RERANK_URL")
                  or os.environ.get("RERANK_BASE_URL") or "")
    semantic = bool(embed) and bundle.has_vectors
    # Off by default: measured 2026-09-24, the cross-encoder both rescued a
    # rank-12 answer and promoted an unrelated field to rank 1.
    rerank = (os.environ.get("SFK_RERANK", "false").strip().lower() == "true"
              and bool(rerank_url))
    log.info("knowledge bundle at %s (semantic=%s, rerank=%s)",
             directory, semantic, rerank)

    def run(question: str, *, extraction=None, limit=None):
        return bundle.discover_schema(question, limit or 10,
                                      semantic=semantic,
                                      endpoint=embed or None,
                                      rerank=rerank,
                                      rerank_endpoint=rerank_url or None,
                                      extraction=extraction)

    return run


async def _in_thread(function, *args):
    """Run the synchronous pipeline without blocking the event loop.

    Starlette's helper in production, because it honours the concurrency
    limiter FastAPI tunes for this application. `asyncio.to_thread` otherwise,
    so this module can be exercised without FastAPI installed.
    """
    try:
        from starlette.concurrency import run_in_threadpool
    except ImportError:
        import asyncio
        return await asyncio.to_thread(function, *args)
    return await run_in_threadpool(function, *args)


# -- the request gate ------------------------------------------------------
def _declines(request: Any, clarification_response: Any) -> Optional[str]:
    """Why this request is not the new pipeline's, decided before any model call.

    Read from the request alone. Asking the old intent classifier first would
    be the one thing the cutover is meant to stop.
    """
    if clarification_response is not None:
        return "answering a pending clarification"
    if getattr(request, "sf_live", False):
        return "the Live Salesforce toggle is on"
    if getattr(request, "pdf_data", None) or getattr(request, "image_data", None):
        return "the request carries an attachment"
    if not (getattr(request, "text", "") or "").strip():
        return "no question text"
    return None


async def try_answer(text: str, *, emit, request: Any,
                     clarification_response: Any = None,
                     query_trace: Any = None) -> Optional[Outcome]:
    """Answer with the new pipeline, or return None and let the old one run."""
    if not ENABLED:
        return None
    reason = _declines(request, clarification_response)
    if reason:
        log.debug("new pipeline declined: %s", reason)
        return None
    pipeline = _build()
    if pipeline is None:
        return None

    from pipeline import PipelineRequest

    try:
        result = await _in_thread(
            pipeline.run,
            PipelineRequest(question=text,
                            request_id=getattr(query_trace, "request_id", None),
                            test_case_id=getattr(request, "test_case_id", None),
                            conversation_id=(getattr(request, "conversation_id", None)
                                             or getattr(request, "session_id", None))))
    except Exception:                                   # noqa: BLE001
        log.warning("new pipeline raised; the existing engine will answer",
                    exc_info=True)
        return None

    answer = getattr(result, "answer", None)
    written = (getattr(answer, "text", "") or "").strip() if answer else ""
    if not result.success or not written:
        log.info("new pipeline did not answer (route=%s stages_run=%s error=%s)",
                 result.route, result.stages_run, result.error_detail)
        return None

    await emit("token", {"text": written})
    await emit("meta", _meta(result, answer))
    return Outcome(handled=True, answer=written,
                   meta_extras={"sfk_route": result.route,
                                "sfk_trace": True})


def _meta(result: Any, answer: Any) -> dict[str, Any]:
    """The existing /chat meta contract, filled from the new pipeline.

    `route` stays "sql" because that is what the frontend already understands
    for a records answer; the new eight-value route rides alongside as
    `sfk_route` rather than replacing a value a client parses.

    Provenance says SYNCHRONISED, never live. That is the architectural point
    of the new path, and a meta that claimed "live" would undo it in the one
    place a user can see.
    """
    records = getattr(result, "records", None)
    freshness = getattr(records, "freshness", None)
    meta: dict[str, Any] = {
        "route": "sql",
        "sfk_route": result.route,
        "sfk_stages": result.stages_run,
        "provenance": {
            "source": "synchronised_salesforce_records",
            "environment": _records_environment(),
            "is_sandbox": (_state.get("origin") or {}).get("is_sandbox"),
            "org_id": (_state.get("origin") or {}).get("org_id"),
            "environment_source": (_state.get("origin") or {}).get(
                "environment_source", "config"),
            "freshness": getattr(freshness, "status", "unknown"),
            "age_minutes": getattr(freshness, "age_minutes", None),
        },
        "model": getattr(answer, "model", ""),
        "grounded": bool(getattr(answer, "grounded", False)),
    }
    if records is not None and getattr(records, "sql", ""):
        # The statement is built from catalog identifiers and carries no user
        # values -- those are bound parameters and are never in this string.
        meta["sql"] = records.sql
        meta["row_count"] = records.row_count
        if records.total_count is not None:
            meta["total_count"] = records.total_count
        meta["truncated"] = bool(records.truncated)
    return meta


def state() -> dict[str, Any]:
    """For /health and for an operator asking why the new path is quiet."""
    return {"enabled": ENABLED, "built": _state["built"],
            "available": _state["pipeline"] is not None,
            "error": _state["error"], "environment": _records_environment(),
            "records_origin": _state.get("origin"),
            "models": _state.get("models"),
            "linker_mode": "offline" if _linker_offline() else "llm_assisted",
            "ir_path": _state.get("ir_path"),
            "knowledge_root": _knowledge_root()}
