"""Timing of the app-state database, from the orchestrator's side.

WHY THIS EXISTS. Until 2026-09-14 the orchestrator exported no database timing
of any kind. The 14-day Prometheus study (dbperf2 measure-metrics.json) could
only attribute database time to a chat turn by correlating backend active time
with the knowledge stage histograms, and it could not tell a slow query from a
pool checkout wait or a thread-pool queue. The latency targets set that day
("a hot-path query <= 5 ms", "the chat-turn sequence <= 30 ms", "idle polling
<= 1 query/s per process") were measurable only in throwaway containers. These
series make them measurable in production.

WHAT IS MEASURED, AND WHERE:

- ``techsara_db_pool_wait_seconds`` — ``db.connection()`` or
  ``db.read_connection()`` asked the pool for a connection until it had one.
  Includes the pool's liveness check (``db._check_pooled_connection``: a
  socket probe, plus a round trip only for a connection idle for a while or
  one whose socket shows input).
- ``techsara_db_thread_wait_seconds`` — ``db.run_in_thread`` was awaited until
  the worker thread started the call (anyio's 40-token limiter is shared with
  every other sync route and ``to_thread`` user). Only callers that go through
  ``run_in_thread`` are seen; FastAPI's own threadpool dispatch of sync routes
  is not.
- ``techsara_db_transaction_seconds{site}`` — connection checked out until the
  commit (or rollback) returned: every statement, the Python work between
  them, and the COMMIT round trip.
- ``techsara_db_statement_seconds{site}`` — one ``execute`` / ``executemany``
  round trip, results received. Its ``_count`` is statements per call site, so
  ``sum(rate(techsara_db_statement_seconds_count[5m]))`` is the idle polling
  rate of the process.
- ``techsara_db_transaction_errors_total{site}`` — transactions that left
  ``db.connection()`` with an exception (rolled back).
- ``techsara_db_checkout_errors_total{site}`` — ``db.connection()`` never got a
  connection (pool timeout, server refused); its wait is still observed.
- ``techsara_db_pool_{size,available,max,requests_waiting}`` — psycopg_pool's
  own counters, sampled at scrape time; nothing is sampled when no pool exists.

CARDINALITY. ``site`` is a closed vocabulary (``SITES``). It is derived from
the function that entered ``db.connection()``: an exact (module, function)
entry in ``_SITE_BY_FUNCTION``, else the module's entry in
``_SITE_BY_MODULE``, else ``"other"``. The result is cached per code object,
so a call site costs one frame walk and one dict lookup. No SQL text, id or
user value can reach a label.

EVERYTHING HERE MUST NEVER RAISE into a database call. Every metric operation
is guarded; a failure loses a sample, never a query.
"""
from __future__ import annotations

import contextlib
import sys
import time
from typing import Any, Callable, Dict, Optional, Tuple

import psycopg

from . import metrics

#: Seconds. The owner's targets (2026-09-14) sit ON bucket edges, so the share
#: of calls meeting each target is a bucket ratio, not an interpolation:
#: 2 ms durable flush, 5 ms hot query / key+usage, 20 ms hot query at 10x,
#: 30 ms chat-turn sequence, 50 ms lexical, 100 ms lexical at 10x.
DB_BUCKETS: Tuple[float, ...] = (
    0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.03, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 10.0,
)

POOL_WAIT = "techsara_db_pool_wait_seconds"
THREAD_WAIT = "techsara_db_thread_wait_seconds"
TRANSACTION = "techsara_db_transaction_seconds"
STATEMENT = "techsara_db_statement_seconds"
TRANSACTION_ERRORS = "techsara_db_transaction_errors_total"
CHECKOUT_ERRORS = "techsara_db_checkout_errors_total"

# ---------------------------------------------------------------------------
# The call-site vocabulary
# ---------------------------------------------------------------------------

_F: Dict[Tuple[str, str], str] = {}


def _sites(site: str, module: str, *names: str) -> None:
    for name in names:
        _F[(module, name)] = site


_DB = "app.db"

# --- chat turn -------------------------------------------------------------
_sites("conversation", _DB,
       "get_conversation", "conversation_owner", "create_conversation",
       "update_conversation", "list_conversations", "conversation_title_state",
       "set_generated_title", "delete_conversation", "oldest_user")
_sites("history", _DB, "list_messages", "get_summary", "save_summary", "clear_summary")
_sites("message_write", _DB,
       "add_message", "replace_messages", "truncate_messages", "set_message_feedback")
_sites("message_write", "app.main", "_overwrite_persisted_answer")
_sites("chat_request", _DB,
       "create_chat_request", "get_chat_request", "latest_chat_request",
       "set_chat_request_status", "park_chat_request", "resume_chat_request",
       "resume_queued_chat_request", "cancel_parked_chat_requests",
       "interrupt_open_chat_requests", "list_resumable_chat_requests",
       "count_chat_requests", "generation_streamed", "get_message_by_generation")
_sites("context", _DB,
       "get_documents", "save_document", "get_uploads", "get_upload", "save_upload",
       "get_url_documents", "get_url_document_urls", "save_url_document",
       "get_conversation_chunks", "add_conversation_chunks",
       "get_conversation_videos", "get_conversation_crawl_sites",
       "get_repo", "get_repo_keys", "save_repo", "replace_repo_chunks",
       "search_repo_chunks")
_sites("facts", _DB, "list_user_facts", "trusted_user_facts", "add_user_fact",
       "update_user_fact", "delete_user_fact")
_sites("recall", _DB,
       "recall_conversations", "search_conversations", "fetch_message_embeddings",
       "messages_missing_embeddings", "store_message_embeddings")
_sites("recall", "app.memory_semantic", "_embeddings_fingerprint")
_sites("salesforce", _DB,
       "get_sf_conversation_state", "save_sf_conversation_state", "get_sf_intent",
       "save_sf_intent", "latest_open_sf_intent", "close_sf_intents",
       "create_sf_clarification", "get_sf_clarification", "pending_sf_clarification",
       "resolve_sf_clarification", "cancel_sf_clarifications",
       "list_confirmed_sql_examples")
_sites("query_trace", _DB,
       "start_query_trace", "append_query_trace_event", "finish_query_trace",
       "write_query_trace_batch", "get_query_trace")
_sites("usage", "app.usage", "record")
_sites("usage", _DB, "record_voice_transcription")

# --- living knowledge (pre-pass) --------------------------------------------
_sites("web_lexical", "app.web_memory", "_lexical_candidates")
_sites("web_meta", "app.web_memory", "_page_meta")
_sites("web_meta", _DB, "web_page_ids_for_urls", "get_web_pages", "get_web_page_versions")
_sites("web_servable", _DB, "servable_web_page_ids")
_sites("web_servable", "app.web_index", "_servable_page_ids")
_sites("web_knowledge", "app.web_memory", "_bump_retrieval")
_sites("web_knowledge", _DB, "search_web_claims", "insert_web_claims", "log_web_search")

# --- background web corpus ----------------------------------------------------
_sites("web_worker", _DB,
       "upsert_web_page", "touch_web_page_unchanged", "set_web_page_quarantine",
       "get_unindexed_web_pages", "count_unindexed_web_pages",
       "mark_web_pages_indexed", "count_stale_chunk_pages",
       "reset_web_index_watermark", "create_web_crawl", "enqueue_web_crawl",
       "get_web_crawl", "finish_web_crawl", "next_queued_web_crawl",
       "requeue_interrupted_web_crawls", "web_crawl_queue_counts",
       "add_crawl_frontier", "take_crawl_frontier", "mark_crawl_frontier",
       "defer_crawl_frontier", "clear_crawl_frontier", "crawl_frontier_counts",
       "create_research_run", "finish_research_run", "get_research_runs",
       "close_interrupted_research_runs")

# --- scrapes and probes ---------------------------------------------------------
_sites("metrics_scrape", _DB, "web_corpus_counts")
_sites("health", _DB, "schema_version")
_sites("health", "app.health", "_read_work")

# --- /v1 public API ----------------------------------------------------------------
_sites("api_key", _DB,
       "api_key_by_public_id", "touch_api_key", "get_api_project",
       "get_service_account", "public_model_overrides", "get_playground_project",
       "get_platform_secret")
_sites("api_usage", _DB, "api_project_usage_transaction", "lock_api_project_usage",
       "read_usage_daily")
_sites("api_usage", "app.apiplatform.quotas",
       "reserve", "record_usage", "read_window", "_record_unenforced")
_sites("api_idempotency", _DB, "finish_idempotency", "release_idempotency")
_sites("api_idempotency", "app.apiplatform.idempotency", "claim")
_sites("api_response", _DB,
       "create_api_response", "update_api_response", "get_api_response",
       "list_api_responses")

# --- durable run event log (V36) ------------------------------------------------------
_DS = "app.publicapi.durable_store"
_sites("durable_flush", _DS, "append")
_sites("durable_dispatch", _DS, "due_for_resume", "claim", "queued_head")
_sites("durable_lease", _DS, "renew_leases", "still_authorised_shim")
_sites("maintenance", _DS, "purge_events")

# --- Files API (V37) -------------------------------------------------------------------
_sites("files_claim", "app.apifiles.queue", "claim_due_blobs", "claim_assembly")

# --- maintenance and schema ---------------------------------------------------------------
_sites("maintenance", _DB,
       "prune_api_platform", "_prune_api_durable", "purge_api_content_batched",
       "_delete_response_events_batched", "expire_rotated_keys",
       "orphan_video_analyses", "expired_upload_sessions",
       "reset_stale_finalizing_upload_sessions")
_sites("migrations", _DB, "_apply_migrations")
_sites("migrations", _DS, "ensure_schema", "schema_present")
_sites("migrations", "app.apifiles.schema", "ensure_schema")

#: Module fallbacks: a function not named above is attributed to its module's
#: area rather than to "other".
_SITE_BY_MODULE: Dict[str, str] = {
    "app.authn.store": "auth",
    "app.authn.bootstrap": "auth",
    "app.analytics": "analytics",
    "app.artifacts.db": "artifacts",
    "app.apifiles.schema": "files",
    "app.apifiles.queue": "files",
    "app.apifiles.jobs": "files",
    "app.apifiles.retention": "files",
    "app.apifiles.uploads_sweep": "files",
    "app.apifiles.accounting": "files",
    "app.apifiles.service": "files",  # SqlFileStore reaches db.connection indirectly
    "app.publicapi.durable_store": "durable",
    "app.apiplatform.webhooks.queue": "webhooks",
    "app.apiplatform.console_api": "api_console",
    "app.apiplatform.projects": "api_console",
    "app.apiplatform.quotas": "api_usage",
    "app.apiplatform.idempotency": "api_idempotency",
    "app.living_knowledge": "web_knowledge",
    "app.web_memory": "web_knowledge",
    "app.web_worker": "web_worker",
    "app.web_index": "web_servable",
    "app.health": "health",
    "app.usage": "usage",
}

_SITE_BY_FUNCTION = _F

#: Every value `site` can take. Anything else is a bug in this module, and the
#: metrics registry folds it to "other" regardless.
SITES = frozenset(set(_F.values()) | set(_SITE_BY_MODULE.values()) | {
    "other", "sharing", "video", "uploads", "webhooks", "api_console",
})

# A few db.py areas that are cheap to name by prefix rather than one by one:
# they are not on the chat path and each owns a table family.
_DB_PREFIX_SITES: Tuple[Tuple[str, str], ...] = (
    ("share", "sharing"), ("touch_share", "sharing"), ("add_share", "sharing"),
    ("create_share", "sharing"), ("revoke_share", "sharing"),
    ("update_share", "sharing"), ("list_workspace_shares", "sharing"),
    ("revoke_workspace_public_shares", "sharing"), ("workspace_shar", "sharing"),
    ("set_workspace_sharing", "sharing"),
    ("video", "video"), ("get_video", "video"), ("upsert_video", "video"),
    ("update_video", "video"), ("delete_video", "video"), ("list_video", "video"),
    ("claim_video", "video"), ("release_video", "video"),
    ("link_video", "video"), ("requeue_interrupted_video", "video"),
    ("create_upload_session", "uploads"), ("get_upload_session", "uploads"),
    ("list_upload_sessions", "uploads"), ("record_upload_part", "uploads"),
    ("try_begin_upload_finalize", "uploads"), ("set_upload_session_status", "uploads"),
    ("webhook", "webhooks"), ("due_webhook", "webhooks"), ("enqueue_webhook", "webhooks"),
    ("record_webhook", "webhooks"), ("create_webhook", "webhooks"),
    ("list_webhook", "webhooks"), ("update_webhook", "webhooks"),
    ("delete_webhook", "webhooks"), ("get_webhook", "webhooks"),
    ("api_", "api_console"), ("create_api", "api_console"), ("list_api", "api_console"),
    ("revoke_api", "api_console"), ("update_api", "api_console"),
    ("create_service_account", "api_console"), ("list_service_accounts", "api_console"),
    ("set_service_account", "api_console"), ("set_public_model", "api_console"),
    ("set_platform_secret", "api_console"),
)

metrics._ALLOWED_BY_METRIC[TRANSACTION] = {"site": set(SITES)}
metrics._ALLOWED_BY_METRIC[STATEMENT] = {"site": set(SITES)}
metrics._ALLOWED_BY_METRIC[TRANSACTION_ERRORS] = {"site": set(SITES)}
metrics._ALLOWED_BY_METRIC[CHECKOUT_ERRORS] = {"site": set(SITES)}
for _name in (POOL_WAIT, THREAD_WAIT, TRANSACTION, STATEMENT):
    metrics._BUCKETS_BY_METRIC[_name] = DB_BUCKETS


def site_for(module: str, function: str) -> str:
    """The `site` label of a function that opens a transaction."""
    site = _F.get((module, function))
    if site is not None:
        return site
    if module == _DB:
        for prefix, candidate in _DB_PREFIX_SITES:
            if function.startswith(prefix):
                return candidate
        return "other"
    return _SITE_BY_MODULE.get(module, "other")


#: code object -> site. Bounded by the number of functions in the program.
_site_cache: Dict[Any, str] = {}
_CONTEXTLIB_FILE = contextlib.__file__
#: app.db context managers that only hand out a pooled connection: the site is
#: the function that called them. `read_connection` is the single-statement
#: autocommit checkout; `_on` is "the caller's transaction or a pooled one"
#: (the trace writers and the usage ledgers).
_DB_PLUMBING = frozenset({"connection", "read_connection", "_on"})
_THIS_FILE = __file__


def _caller_site(depth: int) -> str:
    """Site of the first frame above ``depth`` that is not plumbing.

    Plumbing is contextlib (``@contextmanager``'s ``__enter__``), this module,
    and ``db.connection`` itself.
    """
    try:
        frame = sys._getframe(depth + 1)
        while frame is not None:
            code = frame.f_code
            filename = code.co_filename
            if filename == _CONTEXTLIB_FILE or filename == _THIS_FILE or (
                code.co_name in _DB_PLUMBING and frame.f_globals.get("__name__") == _DB
            ):
                frame = frame.f_back
                continue
            site = _site_cache.get(code)
            if site is None:
                site = site_for(str(frame.f_globals.get("__name__", "")), code.co_name)
                _site_cache[code] = site
            return site
    except Exception:  # noqa: BLE001 — a label must never break a query
        pass
    return "other"


# ---------------------------------------------------------------------------
# The seams
# ---------------------------------------------------------------------------


_NO_LABELS: Tuple[Tuple[str, str], ...] = ()
#: site -> the registry key ``metrics._clean({"site": site}, name)`` produces.
#: The histograms share one vocabulary, so one key serves all of them.
_key_cache: Dict[str, Tuple[Tuple[str, str], ...]] = {}


def _site_key(site: str) -> Tuple[Tuple[str, str], ...]:
    key = _key_cache.get(site)
    if key is None:
        key = metrics._clean({"site": site}, TRANSACTION)
        _key_cache[site] = key
    return key


#: Statement durations kept per checkout before they are recorded early. A
#: transaction that loops over thousands of executes (a backfill, a per-row
#: insert loop) would otherwise grow the list without bound and then hold the
#: registry lock for the whole list at commit: 100k statements measured 59 ms
#: under the global metrics lock (verifier, 2026-09-14). Chunks keep both the
#: memory and the lock hold bounded; the recorded counts are identical.
_PENDING_FLUSH_AT = 256


def _flush_pending(conn: Any, pending: list) -> None:
    try:
        batch = pending[:]
        del pending[:]
        key = _site_key(getattr(conn, "_techsara_site", None) or "other")
        metrics.observe_many([(STATEMENT, key, s) for s in batch])
    except Exception:  # noqa: BLE001 — a metric must never break a query
        pass


class TimedCursor(psycopg.Cursor):
    """``psycopg.Cursor`` that times each round trip of a checked-out connection.

    Installed as the connection's ``cursor_factory`` by ``checkout``. The
    durations go to the list ``checkout`` binds on the connection and are
    recorded when the transaction ends, under one registry lock together with
    the pool wait and the transaction time. Outside a checkout (the pool's own
    liveness check, a reset) nothing is bound and nothing is recorded.
    """

    __slots__ = ()

    def execute(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        pending = getattr(self._conn, "_techsara_pending", None)
        if pending is None:
            return super().execute(*args, **kwargs)
        started = time.perf_counter()
        try:
            return super().execute(*args, **kwargs)
        finally:
            pending.append(time.perf_counter() - started)
            if len(pending) >= _PENDING_FLUSH_AT:
                _flush_pending(self._conn, pending)

    def executemany(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        pending = getattr(self._conn, "_techsara_pending", None)
        if pending is None:
            return super().executemany(*args, **kwargs)
        started = time.perf_counter()
        try:
            return super().executemany(*args, **kwargs)
        finally:
            pending.append(time.perf_counter() - started)
            if len(pending) >= _PENDING_FLUSH_AT:
                _flush_pending(self._conn, pending)


class checkout:
    """``with checkout(pool) as con`` — ``pool.connection()`` plus timing.

    Used by ``db.connection()`` only. The site is resolved from the caller's
    frame when the checkout is entered; everything is recorded at exit.
    """

    __slots__ = ("_pool", "_cm", "_con", "_site", "_asked", "_got", "_pending")

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._cm = None
        self._con = None
        self._site = "other"
        self._asked = 0.0
        self._got = 0.0
        self._pending: list = []

    def __enter__(self) -> psycopg.Connection:
        try:
            self._site = _caller_site(1)
        except Exception:  # noqa: BLE001
            self._site = "other"
        self._asked = time.perf_counter()
        self._cm = self._pool.connection()
        try:
            con = self._cm.__enter__()
        except BaseException:
            # PoolTimeout (every connection busy for APP_DB_POOL_TIMEOUT) or a
            # server that refuses: the wait still happened and is the most
            # important one to see.
            self._record_checkout_failure(time.perf_counter())
            raise
        self._got = time.perf_counter()
        try:
            if con.cursor_factory is psycopg.Cursor:
                con.cursor_factory = TimedCursor
            con._techsara_site = self._site
            con._techsara_pending = self._pending
        except Exception:  # noqa: BLE001
            pass
        self._con = con
        return con

    def __exit__(self, exc_type, exc, tb) -> Optional[bool]:
        con = self._con
        try:
            # Unbound BEFORE the connection goes back: once returned, another
            # thread may check it out and bind its own.
            if con is not None:
                con._techsara_site = None
                con._techsara_pending = None
        except Exception:  # noqa: BLE001
            pass
        try:
            return self._cm.__exit__(exc_type, exc, tb)
        finally:
            self._record(time.perf_counter(), exc_type is not None)
            self._con = None
            self._cm = None

    def _record_checkout_failure(self, done: float) -> None:
        try:
            metrics.observe_many(((POOL_WAIT, _NO_LABELS, done - self._asked),))
            metrics.inc(CHECKOUT_ERRORS, _CHECKOUT_ERRORS_HELP, site=self._site)
        except Exception:  # noqa: BLE001
            pass

    def _record(self, done: float, failed: bool) -> None:
        try:
            key = _site_key(self._site)
            observations = [
                (POOL_WAIT, _NO_LABELS, self._got - self._asked),
                (TRANSACTION, key, done - self._got),
            ]
            observations.extend((STATEMENT, key, s) for s in self._pending)
            metrics.observe_many(observations)
            if failed:
                metrics.inc(TRANSACTION_ERRORS, _ERRORS_HELP, site=self._site)
        except Exception:  # noqa: BLE001 — a metric must never break a query
            pass


def wrap_thread_call(fn: Callable[..., Any]) -> Callable[[], Any]:
    """``fn`` wrapped so the thread records how long it waited to start."""
    queued = time.perf_counter()

    def call() -> Any:
        try:
            metrics.observe_many(((THREAD_WAIT, _NO_LABELS, time.perf_counter() - queued),))
        except Exception:  # noqa: BLE001
            pass
        return fn()

    return call


# ---------------------------------------------------------------------------
# Scrape-time pool gauges
# ---------------------------------------------------------------------------


def publish_pool_gauges() -> None:
    """psycopg_pool's counters as gauges. Never opens a pool."""
    try:
        from . import db  # lazy: db imports this module

        pool = getattr(db, "_pool", None)
        if pool is None or getattr(pool, "closed", True):
            return
        stats = pool.get_stats()
        metrics.set_gauge("techsara_db_pool_size", stats.get("pool_size", 0),
                          "Connections the app-state pool currently holds (in use + idle).")
        metrics.set_gauge("techsara_db_pool_available", stats.get("pool_available", 0),
                          "Idle connections ready in the app-state pool.")
        metrics.set_gauge("techsara_db_pool_max", stats.get("pool_max", 0),
                          "The app-state pool's max_size (APP_DB_POOL_MAX).")
        metrics.set_gauge("techsara_db_pool_requests_waiting", stats.get("requests_waiting", 0),
                          "Threads waiting for an app-state pool connection right now.")
    except Exception:  # noqa: BLE001
        pass


_ERRORS_HELP = "App-state transactions that ended in an exception (rolled back), by call site."
_CHECKOUT_ERRORS_HELP = (
    "db.connection() calls that never got a connection (pool timeout, server refused), by call site."
)

metrics._declare(POOL_WAIT, "histogram",
                 "db.connection(): pool checkout wait, including the liveness check.")
metrics._declare(THREAD_WAIT, "histogram",
                 "db.run_in_thread(): await until the worker thread started the call.")
metrics._declare(TRANSACTION, "histogram",
                 "db.connection(): checkout to commit/rollback, by call site.")
metrics._declare(STATEMENT, "histogram",
                 "One execute()/executemany() round trip on the app-state pool, by call site.")
metrics._declare(TRANSACTION_ERRORS, "counter", _ERRORS_HELP)
metrics._declare(CHECKOUT_ERRORS, "counter", _CHECKOUT_ERRORS_HELP)
metrics.register_collector(publish_pool_gauges)
