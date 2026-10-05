"""Read-only HTTP surface over the Salesforce knowledge bundle.

The bundle answers "which object and field does this question mean?" from the
production metadata mirror. It runs as its own service rather than inside the
orchestrator for one reason above the others: the evaluation runner can then
score discovery directly, without booting the app, authenticating, opening an
SSE stream and reading traces back out of PostgreSQL. That round trip is what
made a single evaluation case cost two minutes.

Read-only throughout. Nothing here writes to Salesforce, to the bundle, or to
the application database. Trace events are RETURNED to the caller, which
decides whether to persist them -- this service has no database handle.
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from graphrag import tracing
from graphrag.clarify import ClarifyError, apply as apply_choice, clarify, questions_for
from graphrag.extract import DEFAULT_MODEL as EXTRACT_MODEL_DEFAULT, extract
from graphrag.resolver import Bundle, ResolverError
from graphrag.trace_store import TraceStore

log = logging.getLogger("knowledge-service")


def _env(name: str, default: str = "") -> str:
    """Read SFK_<name>, falling back to the deprecated KNOWLEDGE_<name>.

    KNOWLEDGE_* collided with the orchestrator's own document-RAG settings --
    KNOWLEDGE_RERANK in particular, which defaults to TRUE there and FALSE
    here. Somebody turning the schema reranker off in .env would have silently
    turned the document reranker off too, in a feature they were not touching.
    The old names still work so an existing deployment does not break on this
    rename; SFK_* wins when both are set.
    """
    return os.environ.get(f"SFK_{name}") or os.environ.get(
        f"KNOWLEDGE_{name}") or default


def _flag(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").strip().lower() == "true"


BUNDLE_DIR = _env("BUNDLE_DIR", "/data/knowledge")
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "")
RERANK_BASE_URL = os.environ.get("RERANK_BASE_URL", "")
SEMANTIC_DEFAULT = _flag("SEMANTIC", True)
RERANK_DEFAULT = _flag("RERANK", False)

# Step 1: the model that reads the question. Blank endpoint disables
# extraction entirely and the lexical path runs alone.
EXTRACT_BASE_URL = os.environ.get("EXTRACT_BASE_URL", "")
EXTRACT_MODEL = os.environ.get("EXTRACT_MODEL", EXTRACT_MODEL_DEFAULT)
EXTRACT_DEFAULT = _flag("EXTRACT", True)

# Traces live beside the bundle but in their OWN file: the bundle is read-only
# and rebuilt from the mirror, traces are append-only and must survive that.
TRACE_DB = _env("TRACE_DB", "/data/traces/db/traces.sqlite")
TRACE_ENABLED = _flag("TRACING", True)

_state: dict[str, Any] = {"bundle": None, "error": None, "traces": None}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Opened once. SQLite connections are cheap but the vector table is 20 MB
    # and loading it per request would cost more than every query combined.
    try:
        _state["bundle"] = Bundle(BUNDLE_DIR)
        log.info("knowledge bundle opened at %s (vectors=%s, graph=%s)",
                 BUNDLE_DIR, _state["bundle"].has_vectors, _state["bundle"].has_graph)
    except (ResolverError, OSError) as exc:
        # Serving a clear 503 beats refusing to start: an operator can then
        # see WHICH artifact is missing from /health instead of a crash loop.
        _state["error"] = str(exc)
        log.error("knowledge bundle unavailable: %s", exc)
    if TRACE_ENABLED:
        try:
            _state["traces"] = TraceStore(TRACE_DB)
            log.info("trace store open at %s", TRACE_DB)
        except Exception as exc:
            # Losing traces must not lose answers.
            log.error("trace store unavailable (%s); serving without it", exc)
    yield
    for key in ("bundle", "traces"):
        obj = _state.get(key)
        if obj is not None:
            obj.close()


app = FastAPI(title="Salesforce knowledge service", version="1", lifespan=lifespan)


def _versions() -> dict[str, Any]:
    """Bundle identity, stored with every trace so a run is reproducible."""
    bundle = _state.get("bundle")
    if bundle is None:
        return {}
    out: dict[str, Any] = {"extraction_model": EXTRACT_MODEL if EXTRACT_BASE_URL else None}
    try:
        for key, value in bundle.db.execute("SELECT key, value FROM manifest"):
            if key in ("built_at", "catalog_version"):
                out[f"catalog_{key}"] = value
    except Exception:
        pass
    return out


def _bundle() -> Bundle:
    bundle = _state.get("bundle")
    if bundle is None:
        raise HTTPException(status_code=503, detail=_state.get("error") or "bundle not loaded")
    return bundle


class DiscoverRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=10, ge=1, le=50)
    semantic: bool | None = None
    rerank: bool | None = None
    extract: bool | None = None
    # Set by the evaluation runner so a trace can be joined to its golden case.
    test_case_id: str | None = Field(default=None, max_length=64)
    # Terms the user already disambiguated, so the same question is not asked
    # twice in one conversation: {"placed": "field:Interview__c.Interview_Outcome__c"}
    choices: dict[str, str] = Field(default_factory=dict)


class DescribeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=300)


class ApplyRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    surface: str = Field(min_length=1, max_length=200)
    choice_ids: list[str] = Field(min_length=1, max_length=5)


@app.get("/health")
def health() -> dict[str, Any]:
    """Is this process serving? Not "is every artifact perfect?"."""
    bundle = _state.get("bundle")
    if bundle is None:
        return {"status": "degraded", "bundle_dir": BUNDLE_DIR,
                "error": _state.get("error")}
    return {
        "status": "ok",
        "bundle_dir": BUNDLE_DIR,
        "vectors": bundle.has_vectors,
        "graph": bundle.has_graph,
        "semantic_default": SEMANTIC_DEFAULT,
        "rerank_default": RERANK_DEFAULT,
        "extraction": {"enabled": bool(EXTRACT_BASE_URL) and EXTRACT_DEFAULT,
                       "model": EXTRACT_MODEL if EXTRACT_BASE_URL else None,
                       "endpoint": EXTRACT_BASE_URL or None},
        "tracing": {"enabled": _state.get("traces") is not None, "db": TRACE_DB},
    }


@app.get("/traces")
def traces(limit: int = 20) -> dict[str, Any]:
    """Recent traces, newest first."""
    store = _state.get("traces")
    if store is None:
        raise HTTPException(status_code=503, detail="tracing is disabled")
    return {"traces": store.recent(limit)}


@app.get("/traces/stats")
def trace_stats() -> dict[str, Any]:
    store = _state.get("traces")
    if store is None:
        raise HTTPException(status_code=503, detail="tracing is disabled")
    return store.stats()


@app.get("/traces/{trace_id}")
def trace_detail(trace_id: str) -> dict[str, Any]:
    store = _state.get("traces")
    if store is None:
        raise HTTPException(status_code=503, detail="tracing is disabled")
    found = store.get(trace_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no trace {trace_id}")
    return found


@app.get("/manifest")
def manifest() -> dict[str, Any]:
    """What this bundle was built from, for trace reproducibility."""
    bundle = _bundle()
    out: dict[str, Any] = {}
    for db_name in ("catalog", "lexicon", "cards"):
        try:
            rows = bundle.db.execute(
                f"SELECT key, value FROM {db_name}.manifest"
                if db_name != "catalog" else "SELECT key, value FROM manifest")
            out[db_name] = {k: v for k, v in rows}
        except Exception:
            out[db_name] = {}
    return out


@app.post("/discover")
def discover(request: DiscoverRequest) -> dict[str, Any]:
    """Rank the components a question is about, and say when it must ask."""
    bundle = _bundle()
    store = _state.get("traces")
    started = time.perf_counter()
    semantic = SEMANTIC_DEFAULT if request.semantic is None else request.semantic
    rerank = RERANK_DEFAULT if request.rerank is None else request.rerank
    want_extract = EXTRACT_DEFAULT if request.extract is None else request.extract

    trace_id = None
    if store is not None:
        try:
            trace_id = store.begin(request.query,
                                   test_case_id=request.test_case_id,
                                   versions=_versions())
        except Exception:
            log.warning("could not open a trace", exc_info=True)

    # Step 1 -- read the question. Optional by construction: a model that is
    # down, slow or confused costs precision, never an answer.
    extraction = None
    if want_extract and EXTRACT_BASE_URL:
        extraction = extract(request.query, endpoint=EXTRACT_BASE_URL,
                             model=EXTRACT_MODEL)
    if store is not None and trace_id:
        try:
            store.record_extraction(trace_id, extraction,
                                    model=EXTRACT_MODEL if EXTRACT_BASE_URL else "",
                                    endpoint=EXTRACT_BASE_URL)
        except Exception:
            log.warning("could not record extraction", exc_info=True)

    try:
        result = bundle.discover_schema(
            request.query, request.limit,
            choices=request.choices or None,
            semantic=semantic and bundle.has_vectors,
            endpoint=EMBED_BASE_URL or None,
            rerank=rerank,
            rerank_endpoint=RERANK_BASE_URL or None,
            extraction=extraction)
    except ResolverError as exc:
        if store is not None and trace_id:
            store.finish(trace_id, status="error")
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    payload = result.as_dict()
    questions = questions_for(bundle, result)
    payload["questions"] = [q.as_dict() for q in questions]
    events = list(result.trace) + [tracing.clarification_requested(q) for q in questions]
    payload["trace"] = [e.as_dict() for e in events]
    payload["took_ms"] = int((time.perf_counter() - started) * 1000)

    if store is not None and trace_id:
        try:
            store.add_events(trace_id, events)
            store.finish(trace_id, status="ok", discovery=result,
                         total_duration_ms=payload["took_ms"],
                         signals={"semantic": semantic, "rerank": rerank,
                                  "extract": bool(extraction)})
        except Exception:
            log.warning("could not persist trace %s", trace_id, exc_info=True)
        payload["trace_id"] = trace_id
    return payload


@app.post("/describe")
def describe(request: DescribeRequest) -> dict[str, Any]:
    """Everything the org records about one component."""
    bundle = _bundle()
    trace: list[Any] = []
    payload = bundle.describe_object(request.name, trace=trace)
    payload["trace"] = [e.as_dict() for e in trace]
    if not payload.get("found"):
        # 404, not 500: "this org has no such component" is an answer.
        raise HTTPException(status_code=404, detail=payload)
    return payload


@app.post("/clarify/apply")
def clarify_apply(request: ApplyRequest) -> dict[str, Any]:
    """Turn a user's pick back into a resolved component."""
    bundle = _bundle()
    result = bundle.discover_schema(request.query, semantic=False)
    target = next((r for r in result.needs_clarification
                   if r.surface == request.surface), None)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"{request.surface!r} raised no question for this query")
    try:
        question = clarify(bundle, target)
        chosen = apply_choice(bundle, question, request.choice_ids)
    except ClarifyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "surface": request.surface,
        "chosen": [c.as_dict() for c in chosen],
        # Feed this back as `choices` on the next /discover call.
        "choices": {request.surface: chosen[0].component_id},
    }
