"""The one place the orchestrator touches the new Salesforce pipeline.

    /chat  ->  sfk_bridge.try_answer()  ->  SalesforcePipeline.run()

Nothing else in this application imports `pipeline`, `record_query`, `answer`,
`salesforce` or `graphrag`. One seam means one thing to reason about when the
new path misbehaves, and one thing to remove if it is ever withdrawn.

It is also the one place the orchestrator asks the sf_assistant service
(`SF_ASSISTANT_ENABLED`, off by default; see `_try_assistant`). With that switch
on, `try_answer` takes only that path, so the pipeline above cannot run.

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

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any, Optional

from .sf_intel import Outcome

log = logging.getLogger(__name__)

#: Cutover switch. OFF means this module does nothing at all and `/chat`
#: behaves exactly as it did before it existed.
ENABLED = os.environ.get("SFK_PIPELINE_ENABLED", "false").strip().lower() == "true"

#: The sf_assistant switch. OFF means none of its code runs and `/chat` behaves
#: as it did without it. It reads the environment here, as `ENABLED` does, so
#: this module still imports without the application around it.
ASSISTANT_ENABLED = (
    os.environ.get("SF_ASSISTANT_ENABLED", "false").strip().lower() == "true")

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
                     query_trace: Any = None,
                     principal: Any = None,
                     conv_key: Optional[str] = None,
                     mode_sent: bool = False,
                     stop_line: Optional[str] = None,
                     wants_a_file: bool = False) -> Optional[Outcome]:
    """Answer with the new pipeline, or return None and let the old one run.

    `mode_sent` is whether the client named a mode itself. main.py reads it,
    because `request.model_fields_set` always holds "mode" by the time a turn
    gets here: the feature gate assigns the field.

    `stop_line` is `stop_line_for()` of this turn and `wants_a_file` is the file
    rule's verdict on it: main.py needs both before it can decide to come here.
    """
    if ASSISTANT_ENABLED:
        return await _try_assistant(
            text, emit=emit, request=request,
            clarification_response=clarification_response,
            query_trace=query_trace, principal=principal, conv_key=conv_key,
            mode_sent=mode_sent, stop_line=stop_line,
            wants_a_file=wants_a_file)
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


# -- sf_assistant ----------------------------------------------------------
#: The service has no TCP port. This URL only names it: the transport goes to
#: the socket, and the service accepts exactly this Host.
_ASK_URL = "http://sf-assistant-ask/mcp"
#: `read` is the service's worst silence, 120 s in its line + 240 s of running
#: + 10 s. LiveGeneration.follow keeps the stream warm for that long.
_ASK_TIMEOUTS = {"connect": 3, "read": 370, "write": 10, "pool": 3}
_ASK_MAX_QUESTION = 1000
_ASK_MAX_TURNS = 4
_ASK_MAX_TURN_CHARS = 2000
#: The statuses the service sends. Only these reach the log: any other value is
#: logged as "unknown", because a log line must never carry the reply's text.
_ASK_STATUSES = ("answered", "refused", "unclear", "failed", "busy", "timeout",
                 "no_answer")
#: The same lines the service sends, for a reply that came without its text.
_ASK_FIXED_LINES = {
    "refused": "This field is blocked, so it cannot be shown here.",
    "unclear": "I could not tell what this refers to. Please ask the full "
               "question, with the name or object.",
}
#: The service's closing line for a refused answer that keeps its text. It is no
#: reply of its own, so it is not above, but it marks a refusal all the same.
_ASK_REFUSED_PART_LINE = "One blocked field was left out."
#: What the bridge itself says on a turn one of those lines stops: to a polite word,
#: when the service has no answer, and to a turn the service would not take or a
#: file, because no retry changes those. See `stop_line_for`.
_ASK_NOTED_LINE = ("Okay. Please ask the full question, with the name or "
                   "object, when you are ready.")
_ASK_RETRY_LINE = ("I could not answer that just now. Please try again in a "
                   "moment, with the full question.")
_ASK_NEW_CHAT_LINE = ("This chat holds a blocked or unclear answer, so I cannot "
                      "do that here. Please start a new chat for it.")
#: The same place after an unclear line alone. It stops only the very next turn,
#: so asking again works, and a new chat is not needed for that.
_ASK_AGAIN_LINE = ("I could not tell what the last question referred to, so I "
                   "cannot do that yet. Please ask again with the full question.")
#: The same place when the stored answers could not be read: this chat may hold
#: neither, so the line says only what happened.
_ASK_READ_FAILED_LINE = ("I could not check this chat just now, so I cannot do "
                         "that yet; please try again in a moment.")
#: How long the status stays cleared before a chart's meta (see `_say`). In a real
#: browser 20 ms was enough and 10 ms was not; the rest is for the network between.
_ASK_CHART_PAUSE_S = 0.1
#: Set once a missing token is in the log. No call is made without one, so
#: nothing else would show this lockout.
_ask_no_token_logged = False
#: What the frontend's chart code does to a chart, which `_chart_left_out` checks
#: the service's chart against: `truncateLabel` in lib/chartFormat.ts, and
#: `showSymbol` in lib/chartOption.ts.
_AXIS_LABEL_CUT = 24
_LINE_MARKS_UP_TO = 60
_SAME_LABELS_NOTE = ("No chart: two labels read the same once cleaned or "
                     "shortened, so the bars cannot be told apart.")
_LONE_VALUE_NOTE = ("No chart: a line this long would not show a value with no "
                    "value on either side of it. The rows are in the Data tab.")


async def _assistant_declines(text: str, request: Any,
                              clarification_response: Any, principal: Any,
                              conv_key: Optional[str],
                              mode_sent: bool) -> Optional[str]:
    """Why this turn is not the service's, decided before it is called."""
    global _ask_no_token_logged
    from ..authn.rbac import Cap
    from ..config import settings
    from ..core.sf_intel import state as sf_state
    from .sf_intel import is_conversational

    if not settings.sf_assistant_socket or not settings.sf_assistant_token:
        if not _ask_no_token_logged:
            _ask_no_token_logged = True
            log.warning("sf_assistant is on but has no socket or no token: "
                        "every Salesforce question goes to the older route")
        return "no socket or no token"
    if principal is None or not principal.can(Cap.SF_ASSISTANT_USE):
        return "the capability is missing"
    # The request model defaults to "salesforce", so a client that names no mode
    # has not turned the toggle on. The frontend always names one.
    if not mode_sent or getattr(request, "mode", None) != "salesforce":
        return "the Salesforce toggle is not on"
    # An answer with no conversation has no stored row, and the row is what the
    # web block and the workspace-share block read. The frontend always sends one.
    if not getattr(request, "conversation_id", None):
        return "no conversation to store the answer in"
    reason = _declines(request, clarification_response)
    if reason:
        return reason
    if (getattr(request, "pdf_uploads", None)
            or getattr(request, "video_uploads", None)):
        return "the request carries an upload"
    if len(text.strip()) > _ASK_MAX_QUESTION:
        return "the question is too long"
    if is_conversational(text):
        return "a conversational turn"
    # An open question belongs to the engine that asked it. A read that fails
    # may be hiding one, so it counts as open.
    try:
        if await sf_state.get_pending(conv_key) is not None:
            return "a clarification is open"
    except Exception as exc:                            # noqa: BLE001
        return f"the open clarification could not be read: {type(exc).__name__}"
    return None


def _refused_lines() -> tuple[str, ...]:
    """The lines that mark a refusal: the service's fixed one and its closing one."""
    return (_ASK_FIXED_LINES["refused"], _ASK_REFUSED_PART_LINE)


def _client_answers(request: Any) -> list[str]:
    """The answers in the turns the client sent, oldest first. It reads the client's
    history, as `_recent_turns` does: a forged history changes only the admin's own
    turn.
    """
    return [
        turn["content"] for turn in getattr(request, "history_messages", None) or ()
        if turn.get("role") == "assistant" and isinstance(turn.get("content"), str)]


def _follows_a_refused_line(request: Any) -> bool:
    """`in`, because the service closes the real text of a refused answer with its own
    shorter line, and the frontend folds extra text into a turn."""
    return any(line in answer for answer in _client_answers(request) for line in _refused_lines())


def follows_a_stop_line(request: Any) -> bool:
    """Do the answers the client sent stop this turn: a refused line in any of them,
    or an unclear line in the last?
    """
    answers = _client_answers(request)
    return (_follows_a_refused_line(request)
            or bool(answers) and _ASK_FIXED_LINES["unclear"] in answers[-1])


async def stop_line_for(request: Any, conv_key: Optional[str], principal: Any) -> Optional[str]:
    """What this turn says in place of anything that could read records, or None when
    nothing stops it.

    A REFUSED LINE, in a turn the client sent or in a stored answer, stops every later
    turn of the chat, for anyone, whatever it says. The older route has no field
    block, and its router gives a short follow-up the earlier question as its subject,
    so even an "ok" would hand it the refused question. The stored rows are read
    because the client's turns are not enough: a regenerate or an edit sends them only
    up to its question, so the older route would get the refused question as its text.

    AN UNCLEAR LINE holds nothing back, so it stops the very next turn and no other,
    read from the client's turns alone: only they give the turn order, because stored
    rows can hold sibling branches. A client that sends no turns needs no stop here,
    since the memory it then reads holds no sf_assistant exchange. Asking again works
    after one, so its line says that and not "new chat", unless a stored refusal is
    there too: the stored rows are read first for that.

    A read that fails counts as a stop only for those who may ask the service, as it
    does for the web block in main.py: for everyone it would cost every member the
    older route during a database fault, to guard the former admin who asks in such
    a chat then.
    """
    from .. import db
    from ..authn.rbac import Cap

    if _follows_a_refused_line(request):
        return _ASK_NEW_CHAT_LINE
    # No refused line in the client's turns, so a stop here is the unclear line.
    unclear = _ASK_AGAIN_LINE if follows_a_stop_line(request) else None
    try:
        refused = await db.run_in_thread(
            db.conversation_has_engine_line, conv_key, "sf_assistant", _refused_lines())
    except Exception as exc:                            # noqa: BLE001
        log.warning("cannot read whether this chat holds a refused line: %s",
                    type(exc).__name__)
        if unclear:
            return unclear
        return _ASK_READ_FAILED_LINE if principal.can(Cap.SF_ASSISTANT_USE) else None
    return _ASK_NEW_CHAT_LINE if refused else unclear


async def say_stop_line(line: str, emit) -> Outcome:
    """`line` as this turn's answer, for main.py on a turn that skipped the gate."""
    return await _say(line, {}, emit)


def _recent_turns(request: Any) -> list[dict[str, str]]:
    """The turns the service may read a follow-up from: the frontend's messages,
    because stored rows can hold sibling branches. A turn with no string for its
    content is dropped here: one such value costs the whole call an `ok: false`.
    """
    turns = [
        {"role": turn["role"], "content": turn["content"]}
        for turn in getattr(request, "history_messages", None) or ()
        if turn.get("role") in ("user", "assistant")
        and isinstance(turn.get("content"), str)
    ]
    return [{**turn, "content": turn["content"][:_ASK_MAX_TURN_CHARS]}
            for turn in turns[-_ASK_MAX_TURNS:]]


def _strict_json(raw: Any) -> Any:
    """JSON with no NaN or Infinity, which the jsonb column behind meta refuses
    and which would then cost the stored answer."""
    def refuse(name: str) -> None:
        raise ValueError(f"{name} is not JSON")

    return json.loads(raw, parse_constant=refuse)


def _tool_result(raw: bytes) -> Optional[dict[str, Any]]:
    """The `result` of one JSON-RPC reply, or None when the reply says it has
    none. Raises on a reply of another shape; the caller treats both alike."""
    reply = _strict_json(raw)["result"]
    if reply.get("isError"):
        return None
    envelope = _strict_json(reply["content"][0]["text"])
    if envelope.get("ok") is not True or not isinstance(envelope.get("result"), dict):
        return None
    return envelope["result"]


async def _ask_service(arguments: dict[str, Any]) -> Optional[dict[str, Any]]:
    """One `tools/call` of ask_salesforce over the socket: its `result`, or None.

    Plain JSON-RPC, with no initialize and no MCP library: the service answers
    that on one connection. Every log line here holds a status and seconds only,
    never the question, the answer or the token, and an exception's text is left
    out for the same reason. A non-200 is a warning, so a lockout shows.
    CancelledError is no Exception, so Stop passes straight through.
    """
    import httpx

    from ..config import settings

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=settings.sf_assistant_socket),
            trust_env=False,
            timeout=httpx.Timeout(**_ASK_TIMEOUTS),
        ) as client:
            response = await client.post(
                _ASK_URL,
                headers={"Authorization": f"Bearer {settings.sf_assistant_token}",
                         "Accept": "application/json"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "ask_salesforce",
                                 "arguments": arguments}})
    except Exception as exc:                            # noqa: BLE001
        log.warning("sf_assistant call failed after %.1f s: %s",
                    time.perf_counter() - started, type(exc).__name__)
        return None
    seconds = time.perf_counter() - started
    if response.status_code != 200:
        log.warning("sf_assistant replied %d after %.1f s",
                    response.status_code, seconds)
        return None
    try:
        result = _tool_result(response.content)
    except Exception as exc:                            # noqa: BLE001
        log.warning("sf_assistant reply unreadable after %.1f s: %s",
                    seconds, type(exc).__name__)
        return None
    if result is None:
        log.warning("sf_assistant refused the call after %.1f s", seconds)
        return None
    status = result.get("status")
    log.info("sf_assistant status %s after %.1f s",
             status if status in _ASK_STATUSES else "unknown", seconds)
    return result


async def _try_assistant(text: str, *, emit, request: Any,
                         clarification_response: Any, query_trace: Any,
                         principal: Any, conv_key: Optional[str],
                         mode_sent: bool, stop_line: Optional[str],
                         wants_a_file: bool) -> Optional[Outcome]:
    """Ask the sf_assistant service, or return None and let the older route run.

    NOTHING IS EMITTED BUT THE STATUS UNTIL THE REPLY IS JUDGED, so a reply that
    is no answer leaves the turn clean for the older route.

    REFUSED AND UNCLEAR ARE ANSWERS. The older route has no field block, so
    falling back after a refusal could show exactly what was refused, and it
    would read an unclear follow-up as a question of its own. Every other reply
    means the service could not answer, and only then does the older route run,
    unless one of those lines stops this turn (`stop_line_for`). Such a turn never
    returns None, whoever asks, so only a switched-off bridge leaves it to the
    older route. A file is one more turn the service cannot take.
    """
    from .sf_intel import is_conversational

    if stop_line and is_conversational(text):
        return await _say(_ASK_NOTED_LINE, {}, emit)
    reason = ("a file" if wants_a_file else
              await _assistant_declines(text, request, clarification_response,
                                        principal, conv_key, mode_sent))
    if reason:
        log.debug("sf_assistant declined: %s", reason)
        return await _say(stop_line, {}, emit) if stop_line else None
    await emit("status", {"text": "Reading Salesforce records…"})

    arguments: dict[str, Any] = {"question": text.strip(),
                                 "recent_turns": _recent_turns(request)}
    # A request_id of any other type would cost the whole call an `ok: false`.
    request_id = getattr(query_trace, "request_id", None)
    if isinstance(request_id, str):
        arguments["request_id"] = request_id
    result = await _ask_service(arguments) or {}

    status = result.get("status")
    written = result.get("answer")
    written = written.strip() if isinstance(written, str) else ""
    # A status of another type is simply no status, and a list cannot be looked up.
    fixed = _ASK_FIXED_LINES.get(status) if isinstance(status, str) else None
    if fixed and written in ("", fixed):
        # The fixed line stands alone: charts or sources under it would claim
        # an answer that is not there. The service sends it with kind "records"
        # when a refused record answer ends with no text.
        written, result = fixed, {}
    if status not in ("answered", "refused", "unclear") or not written:
        return await _say(_ASK_RETRY_LINE, {}, emit) if stop_line else None
    return await _say(written, result, emit)


async def _say(written: str, result: dict[str, Any], emit) -> Outcome:
    """The status cleared, the meta, then the answer.

    In this order so a chart is open the first time it is seen, as it is after a
    reload. ProofDrawer decides once, when it first draws, and MessageRow draws it
    as soon as a status line or text stands in the row, which was before this meta
    arrived. An empty status takes the row back to its waiting state, so it first
    draws on the text, with the chart already in its meta.

    THE PAUSE IS PART OF THAT, for a chart only. `LiveGeneration.follow` writes all
    it has as one chunk and the browser draws once a frame, so without it the row
    never shows the cleared status and the chart stays closed. Where no frame comes
    in time (a hidden tab), the chart is closed until a reload or a click: no harm.
    """
    meta = _assistant_meta(result)
    await emit("status", {"text": ""})
    if "chart" in meta:
        await asyncio.sleep(_ASK_CHART_PAUSE_S)
    await emit("meta", meta)
    await emit("token", {"text": written})
    # All of it is in that meta: main.py applies meta_extras after the event.
    return Outcome(handled=True, answer=written)


def _text_list(value: Any) -> list[str]:
    """The strings of a list, for a key the frontend renders as text."""
    lines = [value] if isinstance(value, str) else value
    return ([line for line in lines if isinstance(line, str) and line.strip()]
            if isinstance(lines, list) else [])


def _chart_left_out(spec: Any, rows: list[dict[str, Any]]) -> Optional[str]:
    """Why the frontend's drawing of this chart would mislead, or None.

    The service checks its own drawing, with a cut at 28 characters and a marker
    on every point. Two bars that read the same at the frontend's cut cannot be
    told apart, and on a long line a value with empty rows on both sides has no
    mark and no segment: it is simply not there. The rows stay in either case.
    """
    if spec.type in ("bar", "horizontal_bar", "line", "area"):
        labels = [str(row.get(spec.x_key)) for row in rows]
        cut = [label if len(label) <= _AXIS_LABEL_CUT
               else f"{label[:_AXIS_LABEL_CUT - 1]}…" for label in labels]
        if len(set(cut)) < len(cut):
            return _SAME_LABELS_NOTE
    if spec.type in ("line", "area") and len(rows) > _LINE_MARKS_UP_TO:
        for key in spec.y_keys:
            drawn = [isinstance(row.get(key), (int, float))
                     and not isinstance(row.get(key), bool) for row in rows]
            for before, value, after in zip([False, *drawn], drawn, [*drawn[1:], False]):
                if value and not before and not after:
                    return _LONE_VALUE_NOTE
    return None


def _assistant_meta(result: dict[str, Any]) -> dict[str, Any]:
    """The /chat meta contract, filled from the service's `result`.

    `route` stays "sql", on fixed lines too: that is what the frontend reads as
    a records answer, what keeps the turn out of the public-link routes, and
    what stops the file gate holding and replacing a line said on a turn with a
    file word. `engine` reaches the attempt ledger and is what the web block and
    the workspace-share block read from the stored message.

    `sql` is never set: the learned-examples loop feeds a thumbed-up `meta.sql`
    into other people's prompts. The chart goes through the same parser as any
    model's and must name columns the rows hold, or only the chart is lost.

    `data_ts` is not used: it is the newest sync of ANY object, so the source line
    could then read fresher than the object this answer read. The answer's own
    text says when that object was synced.
    """
    from ..core.chart_spec import parse_chart_spec

    meta: dict[str, Any] = {"route": "sql", "engine": "sf_assistant"}
    sourced = result.get("kind") in ("merged", "records")
    objects = _text_list(result.get("objects"))
    if sourced and objects:
        # Synced, never live, and no `truncated` here: the source line would
        # then say "capped, totals cover what was read" about a chart's rows. No
        # objects means no line: a bare "synced copy" would name nothing, which
        # is what a refused answer with no query that worked would show.
        meta["salesforce_sources"] = {"source": "synced", "objects": objects}

    charts = result.get("charts")
    items = ([item for item in charts if isinstance(item, dict)]
             if isinstance(charts, list) else [])
    item = next((i for i in items if i.get("chart")),
                next((i for i in items if i.get("data")), None))
    if item is None:
        return meta
    rows = item.get("data")
    rows = ([row for row in rows if isinstance(row, dict)]
            if isinstance(rows, list) else [])
    if rows:
        meta["data"] = rows
        meta["truncated"] = bool(item.get("truncated"))
    # A row of a stacked or split chart lacks the series it has no value for, so
    # the columns are the union of all rows' keys.
    columns = list(dict.fromkeys(key for row in rows for key in row))
    spec = parse_chart_spec(item.get("chart"), columns)
    left_out = _chart_left_out(spec, rows) if spec is not None else None
    if spec is not None and not left_out:
        meta["chart"] = spec.wire_dump()
    if sourced:
        # The service sends its scope as lines; the frontend renders one string.
        scope = _text_list(item.get("subtitle"))
        if scope:
            meta["salesforce_scope"] = " · ".join(scope)
        notes = _text_list(item.get("notes")) + ([left_out] if left_out else [])
        if notes:
            meta["assumptions"] = notes
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
