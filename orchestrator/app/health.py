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
import threading
from typing import Dict, List, Optional, Tuple

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


def _check_web_index() -> dict:
    """The web vector index (web_index.py): compatibility plus what it holds.

    Additive and NEVER fatal. The index is derived state — PostgreSQL rebuilds
    it (`tools.reindex_web`, or the worker's self-heal) — so a missing or
    mismatched directory is "empty" / "degraded", not an outage; it must not
    flip `status`, which the container healthcheck gates on. What an operator
    needs from this entry is the two numbers the manifest of a rebuild is
    validated against: `rows` (chunks) and `distinct_pages`, plus
    `pending_pages` (the watermark backlog) so "is the index behind?" has an
    answer without a database session.

    Cost: the distinct-page count reads ONE int64 column, not the vectors —
    measured 43-49 ms for 9,017 rows on the live index, linear in rows, so a
    90k-row table stays under half a second at the 30 s healthcheck interval.
    """
    import json

    from .embedding_index import inspect_embedding_index, metadata_path  # lazy
    from .web_index import TABLE as web_table  # lazy: web_index imports llm/db

    directory = settings.lancedb_web_dir
    out: dict = {
        "status": "empty",
        "directory": directory,
        "table": web_table,
        "rows": 0,
        "distinct_pages": 0,
    }
    # This check opens LanceDB itself rather than through `web_index._open`, so
    # it does not inherit that module's CRM guard. A `LANCEDB_WEB_DIR` that
    # overlapped the Salesforce corpus would therefore be refused by every
    # WRITE path and stay invisible here — /health would report a healthy web
    # index while the configuration was one that no write could ever use. It is
    # reported as misconfigured, not as an outage: `status` gates the container
    # healthcheck, and this is a configuration fact, not a dependency failure.
    try:
        from .web_index import assert_not_salesforce  # lazy, same as above

        assert_not_salesforce(directory)
    except Exception as exc:  # noqa: BLE001 — a check never raises
        out["status"] = "misconfigured"
        out["detail"] = str(exc)[:200]
        return out
    try:
        inspected = inspect_embedding_index(directory, web_table, settings.embed_model)
    except Exception as exc:  # noqa: BLE001 — /health reports, never raises
        inspected = {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
    for key, value in inspected.items():
        if key != "status":
            out[key] = value
    if inspected.get("status") == "ok":
        try:
            import lancedb  # lazy
            import pyarrow.compute as pc

            table = lancedb.connect(directory).open_table(web_table)
            rows = int(table.count_rows())
            out["rows"] = rows
            if rows:
                ids = table.search().select(["page_id"]).limit(rows).to_arrow()
                out["distinct_pages"] = int(len(pc.unique(ids["page_id"])))
            # Additive sidecar keys (chunker_version, query_instruction) that
            # the typed loader ignores; a chunker bump is visible here first.
            try:
                with open(metadata_path(directory), encoding="utf-8") as fh:
                    raw = json.load(fh)
                for key in ("chunker_version", "query_instruction"):
                    if key in raw:
                        out[key] = raw[key]
            except (OSError, ValueError):
                pass
            out["status"] = "ok"
        except Exception as exc:  # noqa: BLE001
            out["status"] = "degraded"
            out["detail"] = f"{type(exc).__name__}: {exc}"
    elif inspected.get("status") == "empty":
        out["status"] = "empty"
    else:
        # A model/dimension mismatch is the same class of fault the Salesforce
        # index reports as "error"; here it is degraded because the fix is a
        # rebuild, not a restart, and the platform answers without the index.
        out["status"] = "degraded"
    try:
        from . import db  # lazy

        out["pending_pages"] = int(db.count_unindexed_web_pages())
        # `pending_pages` counts only `indexed_at IS NULL`. Since V24 a page
        # can also be waiting on a RE-CHUNK (its vectors exist but were built
        # by an older chunker), and during a chunker migration that is the
        # larger queue by far — so "is the index behind?" answered with the
        # first number alone under-reports the backlog by the whole second one.
        from .web_index import CHUNKER_VERSION

        out["rechunk_pending"] = int(db.count_stale_chunk_pages(CHUNKER_VERSION))
    except Exception:  # noqa: BLE001 — the database has its own check
        pass
    return out


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


# ---------------------------------------------------------------------------
# What this box is actually WORKING on (2026-09-11, INF-15 / the observability
# gap in REVIEW-MANIFEST). Every existing check answers "can I still reach my
# dependencies"; none of them answers "is anything in flight, and has it been
# in flight too long". That question is the one an operator has during a
# deploy, after a reboot, and while a person says "it has been spinning for
# half an hour", and until now it could only be answered with a psql session.
#
# Rules this section keeps:
#   * READ ONLY, and counts only. No user id, conversation id, intent id or
#     filename appears here — /health is public inside the Docker network
#     (Prometheus scrapes it unauthenticated), so it holds the same line
#     app/metrics.py holds about labels.
#   * CHEAP. Three grouped counts over partial indexes that exist for exactly
#     these predicates (idx_video_analyses_queue, idx_upload_sessions_open,
#     idx_chat_requests_open), behind a few seconds of cache — /health is
#     called by the container healthcheck every 30 s AND by the blackbox probe
#     every 15 s, and a deploy polls it in a loop.
#   * NEVER FATAL. It is additive: it does not appear in `checks` and cannot
#     move `status`, because "two videos are queued" is not an outage.
_WORK_TTL_SECONDS = 5.0
#: (monotonic time it was taken, the snapshot)
_work_cache: Tuple[float, dict] = (0.0, {})
_work_lock = threading.Lock()


def _live_generation_count() -> int:
    """Generations this PROCESS is streaming right now.

    Read out of `sys.modules` rather than imported: app.main imports this
    module, so importing it back would be a cycle at import time. By the time
    anything calls /health, main is loaded and the lookup succeeds; when it is
    not (a test importing health alone), the honest answer is zero, which is
    also the true one — there is no registry to hold a generation.
    """
    import sys

    main = sys.modules.get(f"{__package__}.main")
    registry = getattr(main, "_live_generations", None)
    if not isinstance(registry, dict):
        return 0
    return sum(1 for gen in list(registry.values()) if not getattr(gen, "done", False))


def _publish_work_gauges(work: dict, *, observed_at: Optional[float] = None) -> None:
    """Mirror the snapshot into the Prometheus registry. `observed_at`
    (time.monotonic, taken BEFORE `_read_work` ran) dates the durable
    queued count for app/continuity.py, which keeps the later of two
    observations; None means "just now".

    WHY HERE. app/metrics.py is a passive registry: a gauge only exists once
    something sets it, and nothing on the request path knows these numbers —
    they are database aggregates, not events. /health is the one code path
    that already computes them and is already called on a fixed cadence by
    two independent callers (the container healthcheck every 30 s, the
    blackbox probe every 15 s), so publishing from here makes the gauges no
    more than a scrape interval stale without adding a second polling loop.
    The alert rules in monitoring/prometheus/rules/alerts.yml read exactly
    these names.
    """
    from . import metrics

    video = work.get("video") or {}
    uploads = work.get("uploads") or {}
    metrics.set_gauge(
        "live_generations", work.get("live_generations", 0),
        "Chat generations streaming in this process.",
    )
    for state in ("queued", "running"):
        metrics.set_gauge(
            "video_queue_depth", video.get(state, 0),
            "Video analyses by pipeline state.", state=state,
        )
    metrics.set_gauge(
        "video_running_stage_age_seconds", video.get("oldest_running_age_s", 0),
        "Seconds since the oldest RUNNING video analysis last changed stage.",
    )
    for state in ("uploading", "finalizing"):
        metrics.set_gauge(
            "upload_sessions_open", uploads.get(state, 0),
            "Chunked upload sessions still open, by state.", state=state,
        )
    metrics.set_gauge(
        "chat_requests_interrupted", work.get("chat_requests_interrupted", 0),
        "Chat requests a restart left interrupted and unresumed.",
    )
    # V32: rows parked for the main model — in THIS process (the in-memory
    # `llm_queued_generations`) or by one that is gone — that the resume
    # sweep will pick up on the next READY. The durable count, as opposed
    # to the live one.
    metrics.set_gauge(
        "chat_requests_queued", work.get("chat_requests_queued", 0),
        "Chat requests durably queued for the main model (V32 status queued).",
    )
    # …and folded into `llm_queued_generations` (CONTRACT §7.2) as the
    # durable half of that gauge (app/continuity._publish_queued). Dated
    # from before the read: this runs in a thread, and a resume that moved
    # the row out of `queued` while the read was in flight has re-counted
    # already — its fresher count wins, this one is dropped (drill 5,
    # 2026-09-12: the durable half pinned the gauge at 1 after the resume).
    from . import continuity

    continuity.note_durable_queued(
        int(work.get("chat_requests_queued", 0) or 0), observed_at=observed_at,
    )
    artifacts = work.get("artifacts") or {}
    for state in ("queued", "running"):
        metrics.set_gauge(
            "artifact_queue_depth", artifacts.get(state, 0),
            "Artifact jobs by pipeline state.", state=state,
        )
    metrics.set_gauge(
        "artifact_oldest_queued_age_seconds", artifacts.get("oldest_queued_age_s", 0),
        "Seconds the oldest QUEUED artifact job has waited for a worker.",
    )


def _read_work() -> dict:
    """The counts, in one pooled connection. Blocking; run in a thread.

    Written against `db.connection()` rather than accessors because none of
    these aggregates has one: `list_video_analyses` and `list_upload_sessions`
    return ROWS (and the latter needs a conversation and a user), and counting
    by fetching would be both slower and a way to pull user data into a public
    endpoint. The accessor this wants is named in the SRE report.
    """
    from . import db  # lazy: importing db must not open anything at import time

    video = {"queued": 0, "running": 0, "oldest_running_age_s": 0}
    uploads = {"uploading": 0, "finalizing": 0}
    interrupted = 0
    queued = 0
    with db.connection() as con:
        for row in con.execute(
            "SELECT status, count(*) AS n FROM video_analyses "
            "WHERE status IN ('queued', 'running') GROUP BY status"
        ).fetchall():
            video[row["status"]] = int(row["n"])
        # `updated_at` moves on every stage transition and on every heartbeat
        # of a running stage, so the oldest one is "how long has the most
        # stuck job been silent" — the number the stalled-analysis alert is
        # about. 0 when nothing is running.
        row = con.execute(
            "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - min(updated_at))), 0) AS age "
            "FROM video_analyses WHERE status = 'running'"
        ).fetchone()
        video["oldest_running_age_s"] = int(float(row["age"] or 0))
        for row in con.execute(
            "SELECT status, count(*) AS n FROM upload_sessions "
            "WHERE status IN ('uploading', 'finalizing') GROUP BY status"
        ).fetchall():
            uploads[row["status"]] = int(row["n"])
        for row in con.execute(
            "SELECT status, count(*) AS n FROM chat_requests "
            "WHERE status IN ('interrupted', 'queued') GROUP BY status"
        ).fetchall():
            if row["status"] == "interrupted":
                interrupted = int(row["n"])
            else:
                queued = int(row["n"])
    # V31 artifact jobs: the one aggregate here that HAS an accessor
    # (artifacts.db.work_snapshot — counts only, same idx_artifact_jobs_open
    # partial index). `oldest_queued_age_s` is the number an operator wants
    # when a person says "it has been 'queued' for ten minutes": the queue
    # drains one job at a time, so an old queued row means the worker is
    # busy or gone, not that the job is slow.
    from .artifacts import db as artifacts_db  # lazy, same reason as db

    artifacts = artifacts_db.work_snapshot()
    return {
        "status": "ok",
        "live_generations": _live_generation_count(),
        "video": video,
        "uploads": uploads,
        "chat_requests_interrupted": interrupted,
        "chat_requests_queued": queued,
        "artifacts": artifacts,
    }


def _check_work() -> dict:
    """The cached snapshot. Never raises; a failure is reported as `unknown`
    so a reader can tell "nothing in flight" from "I could not look"."""
    import time as _time

    global _work_cache
    now = _time.monotonic()
    taken_at, cached = _work_cache
    if cached and now - taken_at < _WORK_TTL_SECONDS:
        return cached
    with _work_lock:
        taken_at, cached = _work_cache
        if cached and _time.monotonic() - taken_at < _WORK_TTL_SECONDS:
            return cached
        observed_at = _time.monotonic()  # before the read: dates the counts (see _publish_work_gauges)
        try:
            work = _read_work()
        except Exception as exc:  # noqa: BLE001 — additive, never fatal
            work = {"status": "unknown", "detail": f"{type(exc).__name__}: {exc}"[:200]}
        else:
            try:
                _publish_work_gauges(work, observed_at=observed_at)
            except Exception:  # noqa: BLE001 — a metric never breaks a probe
                pass
        _work_cache = (_time.monotonic(), work)
        return work


def _check_artifacts() -> dict:
    """The Artifact Studio's dependencies: can this process write under the
    reports volume, and which renderers the render package says it can run.
    Blocking (a mkstemp on the volume); callers run it in a thread.

    ADDITIVE, never `status`: a missing PPTX library or a read-only volume
    means "no decks today", not an unavailable chat service — and the
    container healthcheck gates on `status`. The render package reports its
    own availability through `capabilities()` — pdf, docx, pptx, xlsx and,
    since CONTRACT-2 (2026-09-12), csv, whose writer is the standard
    library and is therefore always true; it is imported lazily and
    tolerated absent (the package ships separately from the job runner and
    tests import this module without it).
    """
    if not settings.artifacts_enabled:
        return {"status": "disabled", "detail": "disabled by configuration", "renderers": {}, "volume_writable": False}
    from .artifacts import store  # lazy: settings-bound paths

    try:
        writable = bool(store.volume_writable())
    except Exception as exc:  # noqa: BLE001 — a check never raises
        writable = False
        volume_detail = f"{type(exc).__name__}: {str(exc)[:120]}"
    else:
        volume_detail = "" if writable else "the artifacts directory under REPORTS_DIR is not writable"
    renderers: dict = {}
    render_detail = ""
    try:
        from .artifacts.render import capabilities  # lazy: python-docx/pptx/weasyprint live behind it

        caps = capabilities()
        renderers = dict(caps) if isinstance(caps, dict) else {}
    except ImportError as exc:
        render_detail = f"render package unavailable: {str(exc)[:120]}"
    except Exception as exc:  # noqa: BLE001
        render_detail = f"{type(exc).__name__}: {str(exc)[:120]}"
    ok_renderers = all(
        (isinstance(v, dict) and v.get("status", "ok") == "ok") or v is True or v == "ok"
        for v in renderers.values()
    ) if renderers else False
    out: dict = {
        "status": "ok" if writable and ok_renderers else "degraded",
        "renderers": renderers,
        "volume_writable": writable,
        "free_mb": store.free_space_mb(),
    }
    detail = "; ".join(d for d in (volume_detail, render_detail) if d)
    if detail:
        out["detail"] = detail
    elif not ok_renderers:
        out["detail"] = "one or more renderers are unavailable"
    return out


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


def answer_engine_not_serving() -> str:
    """Why the answer engine is not taking calls right now, or "" when it
    is: the controller's fresh verdict says not serving, or the main
    breaker is OPEN (held by the controller, or opened by observed
    failures). A HALF_OPEN breaker is probing and an unknown verdict is
    not a verdict (CONTRACT §8.1), so neither is reported here. In-memory
    only; never raises."""
    from . import breaker, engine_state  # lazy, like engine_availability

    try:
        reasons = []
        snap = engine_state.snapshot()
        if engine_state.serving() is False and snap is not None:
            reasons.append(f"controller says {snap.state}")
        brk = breaker.get(breaker.MAIN).describe()
        if brk["state"] == breaker.OPEN:
            held = brk.get("held_open_by")
            reasons.append(f"breaker OPEN ({'held by ' + str(held) if held else brk.get('last_reason', '')})")
        return "; ".join(reasons)
    except Exception:  # noqa: BLE001 — additive, never fatal
        return ""


def engine_availability() -> dict:
    """The orchestrator's own view of the main engine (CONTRACT §8): the
    controller's last verdict, the circuit breaker, the generations held
    for the engine (app/continuity.py) and the admission lanes
    (app/admission.py).

    In-memory only, by design — the poller, the breaker and the lanes
    already hold the answer, and /health is called every 30 s by the
    container healthcheck and every 15 s by the blackbox probe. It rides
    INSIDE the main model's `checks` entry so the existing readers see it
    next to the probe it qualifies, and it never changes that entry's
    `status`: the breaker being OPEN means the orchestrator is protecting
    itself, not that this process is unhealthy — the container healthcheck
    gates on `status`, and restarting the orchestrator would not bring the
    engine back (it would only park every queued generation). Never raises.
    """
    from . import admission, breaker, continuity, engine_state  # lazy: keep import cost off the probe path

    try:
        return {
            "controller": engine_state.describe(),
            "breakers": {name: brk.describe() for name, brk in breaker.all_breakers().items()},
            "queue": continuity.describe(),
            "admission": admission.describe(),
            # One-model mode (CONTRACT v2 §1): stated, so a reader of an
            # older report is not left looking for the fallback block.
            "answer_engine": "main",
        }
    except Exception as exc:  # noqa: BLE001 — additive, never fatal
        return {"status": "unknown", "detail": f"{type(exc).__name__}: {exc}"[:200]}


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
            asyncio.to_thread(_check_web_index),
            asyncio.to_thread(_check_work),
            asyncio.to_thread(_check_artifacts),
        )
    required_count = len(vllm_targets)
    checks: Dict[str, dict] = {
        name: result
        for (name, _), result in zip(vllm_targets, results[:required_count])
    }
    checks["duckdb"] = results[required_count]
    checks["app_db"] = results[required_count + 1]
    # The engine-availability view (2026-09-12) rides on the main model's
    # entry — see engine_availability for why there and why it cannot move
    # `status`. `seen` maps the URL to the name it was probed under, so a
    # profile that shares the main endpoint with other roles still finds it.
    main_name = seen.get(settings.openai_base_url, "")
    if isinstance(checks.get(main_name), dict):
        # A copy: a probe stub may hand the same dict to every service.
        checks[main_name] = {**checks[main_name], "engine": engine_availability()}
        # `/health` 200 proves nothing (CONTRACT §2): the entry's `status`
        # follows the controller's verdict and the breaker, so a reader of
        # checks.vllm.status alone never reads a wedge as healthy (round-2
        # review, health.py:764). `reachable` keeps the probe's own answer.
        not_serving = answer_engine_not_serving()
        if not_serving and checks[main_name].get("status") == "ok":
            checks[main_name] = {
                **checks[main_name],
                "status": "degraded",
                "reachable": True,
                "detail": f"answer engine not serving: {not_serving}",
            }
    embedding_index_result = results[required_count + 2]
    ocr_result = results[required_count + 3]
    reranker_result = results[required_count + 4]
    context_result = results[required_count + 5]
    web_index_result = results[required_count + 6]
    work_result = results[required_count + 7]
    artifacts_result = results[required_count + 8]

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
    # `web_index` is additive too (never `status`, never `capability_status`):
    # the web vector index is derived state that PostgreSQL rebuilds, so its
    # worst case is a slower, dense-less answer, not an unavailable service.
    # `work` is additive for a different reason: it is not a dependency at
    # all. It is what this box is currently doing — live generations, the
    # video queue, open upload sessions, chat requests a restart interrupted —
    # and being busy is never an outage. See `_check_work`.
    # `artifacts` (V31) is additive like `web_index`: the document renderers
    # and the reports volume are a feature's dependencies, not the chat
    # service's, and a missing deck library is "no decks", not an outage.
    return {
        "status": overall,
        "checks": checks,
        "capability_status": capability_status,
        "capabilities": capabilities,
        "context": context_result,
        "web_index": web_index_result,
        "work": work_result,
        "artifacts": artifacts_result,
    }
