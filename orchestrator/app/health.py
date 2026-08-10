"""Dependency checks behind GET /health (spec §8).

§8 requires /health to check the model-serving backends and DuckDB. Under the
owner's all-vLLM override that means the four vLLM services (main, router,
vision, embed) plus the DuckDB warehouse. Each vLLM service exposes GET
/health at its server root (the OpenAI base URLs end in /v1, so the /v1
suffix is stripped first); the warehouse is opened read-only, exactly like
the sql engine does.

Probes are short-timeout and run concurrently, so /health stays fast even
when every dependency is down. Nothing here performs network I/O at import
time, and the offline test suite mocks the probe functions.
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Tuple

import httpx

from .config import settings


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


def _check_duckdb(path: str) -> dict:
    """Open the warehouse read-only (same posture as the sql engine, §8/§12);
    never raises. Blocking — callers run it in a thread."""
    import duckdb  # lazy

    try:
        con = duckdb.connect(
            path,
            read_only=True,
            config={"enable_external_access": False},
        )
        try:
            con.execute("SELECT 1")
        finally:
            con.close()
    except Exception as exc:
        return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
    return {"status": "ok"}


#: Every migration in db._MIGRATIONS must have been applied before the app is
#: healthy. Bumped alongside a new migration; a container running old code
#: against a newer database, or new code against an un-migrated one, is exactly
#: what this catches.
EXPECTED_SCHEMA_VERSION = 3


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
    if version < EXPECTED_SCHEMA_VERSION:
        return {
            "status": "error",
            "detail": (
                f"schema version {version} < expected {EXPECTED_SCHEMA_VERSION} "
                "— migrations have not been applied"
            ),
        }
    return {"status": "ok", "schema_version": version}


async def check_dependencies() -> dict:
    """Probe every §8 dependency concurrently.

    Returns {"status": "ok"|"degraded", "checks": {name: {"status": ...}}}
    with one entry per vLLM service plus "duckdb"; overall status is "ok"
    only when every check passed.
    """
    # One model now serves chat, routing and vision, so those three settings
    # point at the SAME endpoint. Probing it three times would report three
    # "services" that can only ever fail together — dedupe by URL so /health
    # lists what actually exists.
    configured: List[Tuple[str, str]] = [
        ("vllm", settings.openai_base_url),
        ("vllm-router", settings.router_base_url),
        ("vllm-vision", settings.vision_base_url),
        ("vllm-embed", settings.embed_base_url),
    ]
    vllm_targets: List[Tuple[str, str]] = []
    seen: Dict[str, str] = {}
    for name, url in configured:
        if url in seen:
            continue  # already probed under seen[url] — same process
        seen[url] = name
        vllm_targets.append((name, url))

    async with httpx.AsyncClient(timeout=settings.health_probe_timeout) as client:
        results = await asyncio.gather(
            *(_probe_vllm(client, url) for _, url in vllm_targets),
            asyncio.to_thread(_check_duckdb, settings.duckdb_path),
            asyncio.to_thread(_check_app_db),
        )
    checks: Dict[str, dict] = {
        name: result for (name, _), result in zip(vllm_targets, results[:-2])
    }
    checks["duckdb"] = results[-2]
    checks["app_db"] = results[-1]
    overall = "ok" if all(c["status"] == "ok" for c in checks.values()) else "degraded"
    return {"status": overall, "checks": checks}
