"""Dependency checks behind GET /health (spec §8).

§8 requires /health to check the model-serving backends and DuckDB. Under the
owner's all-vLLM override that means the configured chat/embedding services
plus the DuckDB warehouse. Each vLLM service exposes GET
/health at its server root (the OpenAI base URLs end in /v1, so the /v1
suffix is stripped first); the warehouse is opened read-only, exactly like
the sql engine does.

Probes are short-timeout and run concurrently, so /health stays fast even
when every dependency is down. Nothing here performs network I/O at import
time, and the offline test suite mocks the probe functions.
"""
from __future__ import annotations

import time

import asyncio
import importlib.util
from typing import Dict, List, Tuple

import httpx

from .config import settings
from .model_capabilities import ModelCapabilities, RerankerBackend


def service_root(base_url: str) -> str:
    """Root URL of a vLLM service given its OpenAI-compatible base URL.

    vLLM serves GET /health at the server root while clients are configured
    with the /v1 API base: http://vllm:30000/v1 → http://vllm:30000.
    """
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return root


async def _probe_vllm(client: httpx.AsyncClient, base_url: str) -> dict:
    """GET {root}/health on one vLLM service; never raises."""
    url = f"{service_root(base_url)}/health"
    try:
        resp = await client.get(url)
    except Exception as exc:  # connect/timeout/DNS — report, don't crash /health
        return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
    if resp.status_code == 200:
        return {"status": "ok"}
    return {"status": "error", "detail": f"HTTP {resp.status_code} from {url}"}


def _capability_result(
    capabilities: ModelCapabilities, status: str, detail: str = ""
) -> dict:
    result = capabilities.as_dict()
    result["status"] = status
    if detail:
        result["detail"] = detail
    return result


async def _probe_ocr(client: httpx.AsyncClient) -> dict:
    """Probe optional OCR without turning its pixels-only fallback fatal."""
    capabilities = settings.ocr_capabilities
    if not settings.ocr_enabled or not capabilities.enabled:
        return {"status": "disabled", "detail": "disabled by configuration"}
    if not capabilities.supports_ocr:
        return {"status": "degraded", "detail": "configured model does not advertise OCR"}
    if not settings.ocr_base_url:
        return {"status": "degraded", "detail": "OCR_BASE_URL is not configured"}
    try:
        result = await _probe_vllm(client, settings.ocr_base_url)
    except Exception as exc:  # a probe must never make /health itself fail
        return {"status": "degraded", "detail": f"{type(exc).__name__}: {exc}"}
    if result.get("status") == "ok":
        return {"status": "ok"}
    return {"status": "degraded", "detail": result.get("detail", "OCR probe failed")}


def _probe_inprocess_reranker() -> dict:
    """Check lazy dependencies without importing torch or loading weights."""
    missing = []
    for module in ("torch", "transformers"):
        try:
            available = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            available = False
        if not available:
            missing.append(module)
    if missing:
        return {
            "status": "degraded",
            "detail": f"missing lazy in-process dependencies: {', '.join(missing)}",
        }
    return {"status": "ok", "detail": "lazy in-process dependencies available"}


def _reranker_score_url(base_url: str) -> str:
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return f"{root}/score"


async def _probe_remote_reranker(client: httpx.AsyncClient) -> dict:
    if not settings.rerank_base_url:
        return {"status": "degraded", "detail": "RERANK_BASE_URL is not configured"}
    if (
        settings.reranker_capabilities.requires_authentication
        and not settings.rerank_api_key
    ):
        return {
            "status": "degraded",
            "detail": "RERANK_API_KEY is required by the configured reranker",
        }
    headers = {}
    if settings.rerank_api_key:
        headers["Authorization"] = f"Bearer {settings.rerank_api_key}"
    url = _reranker_score_url(settings.rerank_base_url)
    try:
        response = await client.post(
            url,
            json={
                "model": settings.rerank_model,
                "text_1": "health check",
                "text_2": "health check",
            },
            headers=headers,
        )
        if response.status_code != 200:
            return {"status": "degraded", "detail": f"HTTP {response.status_code} from {url}"}
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data or "score" not in data[0]:
            return {"status": "degraded", "detail": "invalid /score response"}
    except Exception as exc:
        return {"status": "degraded", "detail": f"{type(exc).__name__}: {exc}"}
    return {"status": "ok"}


async def _probe_reranker(client: httpx.AsyncClient) -> dict:
    capabilities = settings.reranker_capabilities
    try:
        backend = RerankerBackend.parse(settings.rerank_backend)
    except ValueError as exc:
        return {"status": "degraded", "detail": str(exc)}
    if (
        not settings.rerank_enabled
        or not capabilities.enabled
        or backend is RerankerBackend.DISABLED
    ):
        return {"status": "disabled", "detail": "disabled by configuration"}
    if not capabilities.supports_reranking:
        return {
            "status": "degraded",
            "detail": "configured model does not advertise reranking",
        }
    if backend is RerankerBackend.REMOTE:
        return await _probe_remote_reranker(client)
    return await asyncio.to_thread(_probe_inprocess_reranker)


#: A transient sync-worker write lock is expected, not a fault.
_HEALTH_LOCK_WAIT_SECONDS = 3.0


def _check_duckdb(path: str) -> dict:
    """Open the warehouse read-only (same posture as the sql engine, §8/§12);
    never raises. Blocking — callers run it in a thread."""
    import duckdb  # lazy

    # The config MUST match engines/sql.py exactly. DuckDB refuses a second
    # connection to one file whose configuration differs from the connection
    # already open ("Can't open a connection to same database file with a
    # different configuration"), so a one-key config here turned a perfectly
    # healthy warehouse into a "degraded" report whenever the SQL engine had
    # it open — a different failure from a lock, and one no retry recognises.
    #
    # The lock retry matters just as much: the sync worker holds the write
    # lock a large fraction of the time, and both real readers wait for it
    # (engines/sql.py, core/schema_cache.py). Opening once and reporting
    # "error" on a transient lock made /health flap for a warehouse that was
    # about to be readable (2026-08-29).
    deadline = time.monotonic() + _HEALTH_LOCK_WAIT_SECONDS
    last: Exception | None = None
    while True:
        try:
            con = duckdb.connect(
                path,
                read_only=True,
                config={
                    "enable_external_access": False,
                    "autoinstall_known_extensions": False,
                    "autoload_known_extensions": False,
                },
            )
            try:
                con.execute("SELECT 1")
            finally:
                con.close()
            return {"status": "ok"}
        except Exception as exc:  # noqa: BLE001 — never raises, by contract
            last = exc
            if time.monotonic() >= deadline:
                return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
            time.sleep(0.25)


def _check_embedding_index() -> dict:
    """Inspect model/dimension metadata without creating or repairing an index."""
    from .embedding_index import inspect_embedding_index  # lazy

    return inspect_embedding_index(
        settings.lancedb_dir,
        settings.lancedb_table,
        settings.embed_model,
    )


def _expected_schema_version() -> int:
    """Every migration in `db._MIGRATIONS` must be applied before we are healthy.

    Derived rather than hardcoded. As a literal it went stale the moment
    migration v5 — the entire clarification schema: `sf_intents`,
    `sf_clarifications`, `sf_conversation_state`, and the two indexes that
    enforce one-pending-question-per-conversation and first-response-wins —
    was appended: the constant still said 4, so a database missing every one of
    those tables reported healthy, and the first clarification would have failed
    at runtime instead of at startup.
    """
    from . import db

    return db.LATEST_SCHEMA_VERSION


def _check_app_db(_path: str = "") -> dict:
    """Confirm PostgreSQL answers and the schema is at the expected version.

    /health used to probe only DuckDB, so a schema problem stayed invisible
    until the first user request touched it. Now that migrations run once at
    startup rather than on every connection, this is also the only continuous
    signal that the database is still reachable — a pooled connection can go
    away under us when the container restarts.

    `_path` is vestigial (the SQLite file path) and ignored; the parameter
    stays so the existing call site and its tests keep working.
    """
    from . import db  # lazy: importing db must not open anything at import time

    try:
        version = db.schema_version()
    except Exception as exc:  # noqa: BLE001 — /health reports, never raises
        return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
    expected = _expected_schema_version()
    if version < expected:
        return {
            "status": "error",
            "detail": (
                f"schema version {version} < expected {expected} "
                "— migrations have not been applied"
            ),
        }
    return {"status": "ok", "schema_version": version}


async def probe_context_window(client: httpx.AsyncClient) -> dict:
    """What the MAIN model is actually serving, versus what we configured.

    `MODEL_MAX_CONTEXT`/`MAIN_MODEL_MAX_LEN` is the app's belief; vLLM's
    `/v1/models` reports `max_model_len`, which is the truth. Reporting the two
    side by side is the difference between "262144 is set in .env" and "262144
    is what the server will accept" — and a mismatch is silent otherwise: the
    app simply starts getting 400s on long requests it thought were legal.
    """
    configured = int(settings.model_max_context)
    out: dict = {
        "configured_max_model_len": configured,
        "served_max_model_len": None,
        "status": "degraded",
        "budget": {
            "reserved_output_default": int(settings.model_max_output),
            "reserved_output_high": int(settings.model_high_max_output),
            "safety_margin": int(settings.main_model_context_safety_margin),
            "max_input_tokens": max(
                0,
                configured
                - int(settings.model_high_max_output)
                - int(settings.main_model_context_safety_margin),
            ),
        },
        "serving_flags": {
            "kv_cache_dtype": settings.main_model_kv_cache_dtype,
            "prefix_caching": settings.main_model_enable_prefix_caching,
            "chunked_prefill": settings.main_model_enable_chunked_prefill,
            "auto_tool_choice": settings.main_model_enable_auto_tool_choice,
            "max_num_batched_tokens": settings.main_model_max_batched_tokens,
        },
    }
    try:
        resp = await client.get(f"{service_root(settings.openai_base_url)}/v1/models")
        resp.raise_for_status()
        for item in resp.json().get("data", []):
            served = item.get("max_model_len")
            if isinstance(served, int):
                out["served_max_model_len"] = served
                break
    except Exception as exc:  # noqa: BLE001 — /health never raises
        out["detail"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return out

    served = out["served_max_model_len"]
    if served is None:
        out["detail"] = "the model server did not report max_model_len"
    elif served == configured:
        out["status"] = "ok"
    else:
        # Not an error: a deliberately smaller app-side window is a valid
        # deployment. Saying WHICH way it differs is what makes it actionable.
        out["status"] = "degraded"
        out["detail"] = (
            f"the app is configured for {configured} tokens but the server "
            f"serves {served}"
            + (
                " — requests sized to the configured value will be rejected"
                if configured > served
                else " — the extra context is being left unused"
            )
        )
    return out


async def check_dependencies() -> dict:
    """Probe every §8 dependency concurrently.

    Returns {"status": "ok"|"degraded", "checks": {name: {"status": ...}}}
    with one entry per vLLM service plus "duckdb"; overall status is "ok"
    only when every check passed.
    """
    # Profiles may colocate several roles on one model server (the DGX default
    # shares main/vision and router/agent; Mac commonly shares even more).
    # Probe each process once while retaining a capability record per role.
    configured: List[Tuple[str, str, ModelCapabilities]] = [
        ("vllm", settings.openai_base_url, settings.main_capabilities),
        ("vllm-router", settings.router_base_url, settings.router_capabilities),
        ("vllm-agent", settings.agent_base_url, settings.agent_capabilities),
        ("vllm-vision", settings.vision_base_url, settings.vision_capabilities),
        ("vllm-embed", settings.embed_base_url, settings.embed_capabilities),
    ]
    vllm_targets: List[Tuple[str, str]] = []
    seen: Dict[str, str] = {}
    for name, url, capabilities in configured:
        if not capabilities.enabled:
            continue
        if url in seen:
            continue  # already probed under seen[url] — same process
        seen[url] = name
        vllm_targets.append((name, url))

    async with httpx.AsyncClient(timeout=settings.health_probe_timeout) as client:
        results = await asyncio.gather(
            *(_probe_vllm(client, url) for _, url in vllm_targets),
            asyncio.to_thread(_check_duckdb, settings.duckdb_path),
            asyncio.to_thread(_check_app_db),
            asyncio.to_thread(_check_embedding_index),
            _probe_ocr(client),
            _probe_reranker(client),
            probe_context_window(client),
        )
    required_count = len(vllm_targets)
    checks: Dict[str, dict] = {
        name: result
        for (name, _), result in zip(vllm_targets, results[:required_count])
    }
    checks["duckdb"] = results[required_count]
    checks["app_db"] = results[required_count + 1]
    embedding_index_result = results[required_count + 2]
    ocr_result = results[required_count + 3]
    reranker_result = results[required_count + 4]
    context_result = results[required_count + 5]

    endpoint_results = {
        url: checks[name] for name, url in vllm_targets
    }

    def endpoint_capability(
        capabilities: ModelCapabilities, base_url: str, required_feature: str
    ) -> dict:
        if not capabilities.enabled:
            return _capability_result(
                capabilities, "disabled", "disabled by configuration"
            )
        if not getattr(capabilities, required_feature):
            return _capability_result(
                capabilities,
                "degraded",
                f"configured model does not advertise {required_feature}",
            )
        result = endpoint_results.get(base_url)
        if result and result.get("status") == "ok":
            return _capability_result(capabilities, "ok")
        detail = (result or {}).get("detail", "model endpoint was not probed")
        return _capability_result(capabilities, "degraded", detail)

    embed_capability = endpoint_capability(
        settings.embed_capabilities,
        settings.embed_base_url,
        "supports_embeddings",
    )
    embed_capability["index"] = embedding_index_result
    if embedding_index_result.get("status") == "error":
        embed_capability["status"] = "degraded"
        embed_capability["detail"] = embedding_index_result.get(
            "detail", "embedding index compatibility check failed"
        )

    capabilities = {
        "main": endpoint_capability(
            settings.main_capabilities, settings.openai_base_url, "supports_chat"
        ),
        "router": endpoint_capability(
            settings.router_capabilities, settings.router_base_url, "supports_chat"
        ),
        "agent": endpoint_capability(
            settings.agent_capabilities, settings.agent_base_url, "supports_chat"
        ),
        "vision": endpoint_capability(
            settings.vision_capabilities, settings.vision_base_url, "supports_vision"
        ),
        "embed": embed_capability,
        "ocr": _capability_result(
            settings.ocr_capabilities,
            ocr_result["status"],
            ocr_result.get("detail", ""),
        ),
        "reranker": _capability_result(
            settings.reranker_capabilities,
            reranker_result["status"],
            reranker_result.get("detail", ""),
        ),
    }
    overall = "ok" if all(c["status"] == "ok" for c in checks.values()) else "degraded"
    critical_roles = {"main", "router", "embed"}
    capability_status = (
        "degraded"
        if any(
            item["status"] == "degraded"
            or (name in critical_roles and item["status"] != "ok")
            for name, item in capabilities.items()
        )
        else "ok"
    )
    # `status` and `checks` retain their established required-dependency
    # contract. Optional features are additive and cannot make the service
    # unavailable; launchers can gate them through `capability_status`.
    # `context` is additive and never changes `status`: an app configured for a
    # smaller window than the server serves is a valid deployment, not an
    # outage. It is here so "is 262144 real?" has an answer that does not
    # involve reading a .env file and trusting it.
    return {
        "status": overall,
        "checks": checks,
        "capability_status": capability_status,
        "capabilities": capabilities,
        "context": context_result,
    }
