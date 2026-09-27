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
from graphrag.resolver import Bundle, ResolverError

log = logging.getLogger("knowledge-service")

BUNDLE_DIR = os.environ.get("KNOWLEDGE_BUNDLE_DIR", "/data/knowledge")
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "")
RERANK_BASE_URL = os.environ.get("RERANK_BASE_URL", "")
SEMANTIC_DEFAULT = os.environ.get("KNOWLEDGE_SEMANTIC", "true").lower() == "true"
RERANK_DEFAULT = os.environ.get("KNOWLEDGE_RERANK", "false").lower() == "true"

_state: dict[str, Any] = {"bundle": None, "error": None}


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
    yield
    bundle = _state.get("bundle")
    if bundle is not None:
        bundle.close()


app = FastAPI(title="Salesforce knowledge service", version="1", lifespan=lifespan)


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
    }


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
    started = time.perf_counter()
    semantic = SEMANTIC_DEFAULT if request.semantic is None else request.semantic
    rerank = RERANK_DEFAULT if request.rerank is None else request.rerank
    try:
        result = bundle.discover_schema(
            request.query, request.limit,
            choices=request.choices or None,
            semantic=semantic and bundle.has_vectors,
            endpoint=EMBED_BASE_URL or None,
            rerank=rerank,
            rerank_endpoint=RERANK_BASE_URL or None)
    except ResolverError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    payload = result.as_dict()
    # Rendered questions travel with the result: a caller that has to ask
    # should not need a second round trip to find out how to phrase it.
    questions = questions_for(bundle, result)
    payload["questions"] = [q.as_dict() for q in questions]
    payload["trace"] = payload.get("trace", []) + [
        tracing.clarification_requested(q).as_dict() for q in questions]
    payload["took_ms"] = int((time.perf_counter() - started) * 1000)
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
