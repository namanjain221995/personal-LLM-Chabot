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


def _check_app_db(path: str) -> dict:
    """Open the app database and confirm the schema is migrated; never raises.

    /health used to probe only DuckDB, so a schema problem in app.sqlite3
    stayed invisible until the first user request touched it — connect() runs
    the migration lazily. Reading a migrated column makes a bad migration show
    up here instead.
    """
    from . import db  # lazy: importing db must not open anything at import time

    try:
        with db.closing(db.connect()) as con:
            columns = {
                row["name"] for row in con.execute("PRAGMA table_info(messages)")
            }
        missing = {"generation_id"} - columns
        if missing:
            return {
                "status": "error",
                "detail": f"messages table missing column(s): {sorted(missing)}",
            }
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001 — /health reports, never raises
        return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}


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
            asyncio.to_thread(_check_app_db, settings.app_db_path),
        )
    checks: Dict[str, dict] = {
        name: result for (name, _), result in zip(vllm_targets, results[:-2])
    }
    checks["duckdb"] = results[-2]
    checks["app_db"] = results[-1]
    overall = "ok" if all(c["status"] == "ok" for c in checks.values()) else "degraded"
    return {"status": overall, "checks": checks}
