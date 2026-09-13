"""Shared world for the `/v1/embeddings`, `/v1/rerank` and
`/v1/audio/transcriptions` suites (2026-09-13).

WHAT IS REAL: PostgreSQL and the V34 tables, keys minted through
`projects.create_key` and verified by the real resolver, `quotas` writing real
ledgers, `usage.record` writing `usage_events`, the public router with its
`PublicRoute` envelope, the capacity gate, and the OpenAI SDK parsing a
vLLM-shaped reply.

WHAT IS STUBBED: the engines, and only at the HTTP transport. The embedding
engine is an `httpx.MockTransport` behind a real `AsyncOpenAI` client (so the
SDK's own parsing is exercised); the reranker and the speech replicas are the
same kind of transport installed in `sidecars._transport`. No test reaches a
network, and no test needs a GPU.

Not a test module (no `test_` prefix), so pytest does not collect it; the
three suites import its fixtures by name.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import asr, db, llm
from app.apiplatform import keys as key_tools, projects, quotas
from app.apiplatform.scopes import Scope
from app.config import settings
from app.model_capabilities import RerankerBackend
from app.publicapi import capacity, endpoints, router as public_router, sidecars

WORKSPACE = "ws-sidecars"
OTHER_WORKSPACE = "ws-sidecars-other"
TEST_PEPPER = "sidecar-pepper-0123456789abcdefgh"

#: Engine addresses the fixtures configure. Deliberately recognisable, so a
#: test can assert that none of them — nor the served model names — ever
#: reaches a response body or header.
EMBED_URL = "http://embed-engine.internal:30003/v1"
RERANK_URL = "http://rerank-engine.internal:30005"
ASR_URLS = ("http://asr-head.internal:30007/v1", "http://asr-worker.internal:30007/v1")
EMBED_MODEL = "Qwen/Qwen3-Embedding-0.6B"
RERANK_MODEL = "Qwen/Qwen3-Reranker-0.6B"
ASR_MODEL = "openai/whisper-large-v3"
INTERNAL_STRINGS = (
    "embed-engine",
    "rerank-engine",
    "asr-head",
    "asr-worker",
    ".internal",
    "Qwen",
    "whisper-large",
)

TOKENS: Dict[str, str] = {}


@pytest.fixture(autouse=True)
def _pepper(monkeypatch):
    monkeypatch.setattr(settings, "api_key_pepper", TEST_PEPPER)
    key_tools.reset_pepper_cache()
    yield
    key_tools.reset_pepper_cache()


@pytest.fixture()
def engines_configured(monkeypatch):
    """Every sidecar engine configured, at addresses no test can reach."""
    monkeypatch.setattr(settings, "embed_base_url", EMBED_URL)
    monkeypatch.setattr(settings, "embed_model", EMBED_MODEL)
    monkeypatch.setattr(settings, "rerank_backend", RerankerBackend.REMOTE)
    monkeypatch.setattr(settings, "rerank_base_url", RERANK_URL)
    monkeypatch.setattr(settings, "rerank_model", RERANK_MODEL)
    monkeypatch.setattr(settings, "asr_enabled", True)
    monkeypatch.setattr(settings, "asr_base_urls", ASR_URLS)
    monkeypatch.setattr(settings, "asr_base_url", ASR_URLS[0])
    monkeypatch.setattr(settings, "asr_model", ASR_MODEL)
    monkeypatch.setattr(settings, "public_api_enforce_limits", False)
    # No probe of a real file: the tests that care install their own.
    monkeypatch.setattr(sidecars, "probe_seconds", _no_probe)
    asr.set_provider(None)
    capacity.reset_for_tests()
    yield
    asr.set_provider(None)
    capacity.reset_for_tests()
    sidecars._transport = None


async def _no_probe(audio, **_kwargs):
    return None


@pytest.fixture()
def platform(engines_configured):
    """Two workspaces, two projects and four real keys:

    * `live`  — every scope;
    * `narrow` — every scope EXCEPT the three new write scopes;
    * `chatonly` — every scope, allowlisted to techsara-35b only;
    * `other` — another tenant's key with every scope.
    """
    with db.connection() as con:
        con.execute("INSERT INTO workspaces (id, name) VALUES (%s, %s)", (WORKSPACE, "Acme"))
        con.execute("INSERT INTO workspaces (id, name) VALUES (%s, %s)", (OTHER_WORKSPACE, "Rival"))
    project = projects.create_project(WORKSPACE, "Integration")
    other = projects.create_project(OTHER_WORKSPACE, "Theirs")
    new_writes = {Scope.EMBEDDINGS_WRITE, Scope.RERANK_WRITE, Scope.AUDIO_WRITE}
    live = projects.create_key(project["id"], WORKSPACE, "server", scopes=list(Scope))
    narrow = projects.create_key(
        project["id"], WORKSPACE, "narrow", scopes=sorted(s.value for s in set(Scope) - new_writes)
    )
    chatonly = projects.create_key(
        project["id"], WORKSPACE, "chat", scopes=list(Scope), allowed_models=["techsara-35b"]
    )
    theirs = projects.create_key(other["id"], OTHER_WORKSPACE, "theirs", scopes=list(Scope))
    TOKENS.clear()
    TOKENS.update(
        {"live": live.token, "narrow": narrow.token, "chatonly": chatonly.token, "other": theirs.token}
    )
    quotas.reset_concurrency()
    yield {"project": project, "other": other}
    quotas.reset_concurrency()
    TOKENS.clear()


@pytest.fixture()
def api(platform):
    """A bare app with ONLY the public router — the three routes registered
    through `endpoints.register`, which is idempotent, so this works whether
    or not router.py's own hook has already added them."""
    endpoints.register(public_router.router)
    app = FastAPI()
    app.include_router(public_router.router)
    with TestClient(app) as client:
        yield client


def auth(which: str = "live") -> Dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[which]}"}


# ------------------------------------------------------------- engines --


@dataclass
class Recorded:
    """What a stub engine was sent."""

    requests: List[httpx.Request] = field(default_factory=list)
    bodies: List[bytes] = field(default_factory=list)

    @property
    def calls(self) -> int:
        return len(self.requests)

    def json_bodies(self) -> List[Any]:
        return [json.loads(body) for body in self.bodies]


Handler = Callable[[httpx.Request, bytes], Any]


def install_transport(handler: Handler) -> Recorded:
    """`sidecars._transport` = a MockTransport calling `handler(request, body)`.

    `handler` may return an `httpx.Response` or raise an httpx error. The body
    is read here (an async stream for the speech upload), once.
    """
    recorded = Recorded()

    async def respond(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        recorded.requests.append(request)
        recorded.bodies.append(body)
        result = handler(request, body)
        if hasattr(result, "__await__"):
            result = await result
        return result

    sidecars._transport = httpx.MockTransport(respond)
    return recorded


def install_embed_engine(monkeypatch, handler: Handler) -> Recorded:
    """The embedding engine: a real AsyncOpenAI client over a MockTransport,
    handed out where `sidecars` asks `llm._client` for one."""
    from openai import AsyncOpenAI

    recorded = Recorded()
    seen: List[Optional[float]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        recorded.requests.append(request)
        recorded.bodies.append(body)
        result = handler(request, body)
        if hasattr(result, "__await__"):
            result = await result
        return result

    def client(base_url, api_key=None, *, read_timeout=None):
        seen.append(read_timeout)
        assert base_url == settings.embed_base_url
        return AsyncOpenAI(
            base_url=base_url,
            api_key="local",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )

    monkeypatch.setattr(llm, "_client", client)
    recorded.read_timeouts = seen  # type: ignore[attr-defined]
    return recorded


def vllm_embeddings(body: bytes, *, dims: int = 4, usage: bool = True) -> httpx.Response:
    """A vLLM `/v1/embeddings` reply: vector i is [i+1, i+1.5, …] so order is
    checkable."""
    payload = json.loads(body)
    inputs = payload["input"] if isinstance(payload["input"], list) else [payload["input"]]
    data = [
        {"object": "embedding", "index": i, "embedding": [float(i + 1) + 0.5 * k for k in range(dims)]}
        for i in range(len(inputs))
    ]
    reply: Dict[str, Any] = {"object": "list", "data": data, "model": payload["model"]}
    if usage:
        tokens = sum(len(text.split()) + 1 for text in inputs)
        reply["usage"] = {"prompt_tokens": tokens, "total_tokens": tokens}
    return httpx.Response(200, json=reply)


def usage_events(route: str) -> List[Dict[str, Any]]:
    with db.connection() as con:
        return list(
            con.execute(
                "SELECT route, model, mode, status, error_kind, input_tokens, output_tokens, "
                "generation_id, meta FROM usage_events WHERE route = %s ORDER BY created_at",
                (route,),
            ).fetchall()
        )


def daily(project_id: str) -> Dict[str, int]:
    with db.connection() as con:
        row = con.execute(
            "SELECT COALESCE(SUM(requests),0) AS requests, COALESCE(SUM(input_tokens),0) AS input_tokens, "
            "COALESCE(SUM(output_tokens),0) AS output_tokens, COALESCE(SUM(errors),0) AS errors "
            "FROM api_usage_daily WHERE project_id = %s",
            (project_id,),
        ).fetchone()
    return {key: int(row[key]) for key in ("requests", "input_tokens", "output_tokens", "errors")}


def assert_nothing_internal(response: httpx.Response) -> None:
    text = response.text + json.dumps(dict(response.headers))
    for internal in INTERNAL_STRINGS:
        assert internal not in text, internal
