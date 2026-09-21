"""FastAPI entrypoint (spec §8/§10 + V2-DESIGN §1-§3): POST /chat (SSE),
GET /reports, GET /reports/{filename}, GET /health, /auth/*, /history/*."""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import mimetypes
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, List, Literal, Optional, Sequence

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from . import context, db, llm
from .auth import UserRow, require_user, router as auth_router
from .audio_api import router as audio_router
from .authn.admin_api import router as admin_router
from .share_api import router as share_router
from .authn.analytics_api import router as analytics_router
from .authn.shares_api import router as shares_admin_router
from .config import settings
from .core.tracing import TraceRecorder

# App-module logging was silently dropped: uvicorn configures only its own
# loggers, and with no root handler every app `log.info/warning/error` —
# the generation-usage telemetry, best-of-N losers, and the wall-clock hang
# guard's LOUD error — went nowhere. One root handler, level via LOG_LEVEL
# (default INFO), added only when nothing else configured logging first.
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
from .core.report_paths import ReportPathError, list_reports, resolve_report_file
from .graph import get_graph
from .health import check_dependencies
from .history import foldable_counts, router as history_router
from .memory_api import router as memory_router
from .uploads import router as uploads_router
from .memory import memory
from . import metrics as _latency_metrics
from . import fast_lane
from . import sse as _sse
from .sse import HEARTBEAT_SECONDS, STREAMED_EVENTS as _STREAMED_EVENTS, sse_comment, sse_event

@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    """Open the connection pool and apply migrations BEFORE serving a request.

    The SQLite layer migrated lazily on every connection, so a broken migration
    used to surface on the first user request — long after the deploy looked
    healthy. Doing it here makes a bad migration fail startup, which is what a
    container healthcheck can actually catch. It is also now the ONLY place the
    schema is applied: accessors no longer replay the DDL, which is where most
    of the old per-operation cost went.

    Run in a thread: pool startup dials PostgreSQL, and blocking the loop
    during startup would stall the readiness probe alongside it.

    `wait_for_database` comes first because after a reboot the Docker daemon
    starts every container simultaneously and ignores `depends_on` — so this
    process can, and does, come up before its own database. Waiting turns a
    crash-restart loop into one clean start.
    """
    # A fresh start is not a shutdown: the flag below outlives a lifespan in
    # a process that starts the app more than once (the test client does).
    global _shutting_down
    _shutting_down = False
    # PHYSICAL GUARDS (no-timeout /v1, 2026-09-13; app/resources.py): Python
    # never raises its own soft RLIMIT_NOFILE (measured 1024 against a hard
    # 524288), and with no clock ending a public request every held connection
    # is a descriptor. First thing, before any socket of this lifespan opens.
    from . import resources as _resources

    _resources.raise_nofile()
    # A stale override of a retired /v1 timer is said once, not honoured.
    from .config import warn_retired_settings

    warn_retired_settings()
    # SERVING-PROCESS PERFORMANCE (2026-09-15): the GIL switch interval from
    # env, and the cold imports + the shared SSL context built in a thread
    # BEFORE the first request, so no chat turn pays them on the loop.
    previous_switch_interval = _apply_switch_interval()
    await _warm_process()
    await db.run_in_thread(db.wait_for_database)
    await db.run_in_thread(db.init_schema)
    # Identity baseline: the workspace exists and every user (including the
    # pre-auth local account) holds a membership. Idempotent.
    from .authn.bootstrap import ensure_identity_baseline

    await db.run_in_thread(ensure_identity_baseline)
    # Housekeeping: rows for sessions long dead carry nothing the audit trail
    # does not already hold. Once per process start is plenty.
    from .authn.store import prune_expired_sessions

    await db.run_in_thread(prune_expired_sessions)
    # A research run keeps its plan and claims in process memory only, so a
    # restart kills it — but nothing used to say so, and the row sat at
    # 'running' forever, inflating the research analytics and hiding the
    # death. web_crawls has had this reconciliation since V14; research_runs
    # did not. Closed rather than requeued: there is no on-disk state to
    # resume from (see db.close_interrupted_research_runs).
    interrupted = await db.run_in_thread(db.close_interrupted_research_runs)
    if interrupted:
        logging.getLogger(__name__).info(
            "closed %d research run(s) interrupted by a restart", interrupted
        )
    # The living knowledge layer's keeper: drains the embedding backlog and
    # re-reads pages past their TTL. In-process on purpose (see web_worker) —
    # the queue is a PostgreSQL column, so a restart resumes rather than
    # forgets, and no second container is needed for a few fetches every
    # five minutes.
    from . import web_worker

    web_worker.start()
    # Durable send intents (2026-09-10, docs/upload-reliability/API.md). A
    # generation lives in this process's memory; the request that started
    # it lives in `chat_requests`. Every row still 'accepted'/'running' was
    # held by a process that no longer exists (RC-3: a deploy, a crash, two
    # reboots on 2026-09-10), so it is marked 'interrupted' HERE — before
    # the video pipeline requeues its analyses, so a resumed turn never
    # attaches to a job that is about to be restarted underneath it.
    interrupted_requests = await _interrupt_open_requests()
    if interrupted_requests:
        logging.getLogger(__name__).info(
            "marked %d chat request(s) interrupted by a restart", interrupted_requests
        )
    # Continuity in one-model mode (app/continuity.py, CONTRACT §8.3–8.4):
    # the resume sweep — rows a dead process parked `queued` are resumed
    # now; queued and interrupted rows on every READY the controller
    # reports. After the reconciliation above, so a row it just marked is
    # what the sweep reads.
    from . import continuity

    continuity.start()
    # The engine controller's verdict (app/engine_state.py): polled in the
    # background so the circuit breaker can open on WEDGED/RECOVERING before
    # a single request of this process has failed into a dead engine. After
    # continuity.start(), so the first READY the poller reads is an edge the
    # sweep sees (a healthy engine at start-up resumes the interrupted rows
    # too, CONTRACT §8.4).
    from . import engine_state

    engine_state.start()
    # Durable /v1 generations (no-timeout design, revision 2): SIGTERM must
    # reach them at +0 s — they suspend (flush, release leases, abort their
    # readers) and the next process resumes them — while uvicorn keeps its
    # 90 s grace for chat. The signal is CHAINED, never taken from uvicorn
    # (app/shutdown_signals.py). After engine_state.start(): a resume decision
    # reads the controller's verdict.
    from . import shutdown_signals

    durable = _durable_module()
    shutdown_signals.install(_durable_signal_callback(durable))
    await _maybe_await(durable.start())
    # A chunked upload left `finalizing` by a process that died mid-complete
    # would otherwise wait forever on a finaliser that no longer exists.
    reset_sessions = await _reset_stale_upload_finalisations()
    if reset_sessions:
        logging.getLogger(__name__).info(
            "returned %d stale finalizing upload session(s) to uploading", reset_sessions
        )
    # Expired chunked sessions give their parts back on a timer (API.md,
    # Expiry). In-process for the same reason the video maintenance loop is:
    # the queue is a PostgreSQL column and the work is a directory listing.
    sweep_task = asyncio.get_running_loop().create_task(
        _upload_session_sweep_loop(), name="upload-session-sweep"
    )
    # Video understanding (2026-09-09): requeue analyses a restart cut off
    # and drain the queue behind the app. Their stage files are on disk, so
    # a resume costs only the stage that was interrupted. Since V29 the
    # requeue is lease-aware: a row whose owner is still heartbeating is
    # another live process's run, not an interrupted one.
    from .video import pipeline as video_pipeline

    await video_pipeline.start()
    # The job paces itself against live chat: a generation that is merely
    # WAITING for a video analysis does not count as chatting, or the job
    # would wait for itself.
    def _chat_is_busy() -> bool:
        # A generation that is merely WAITING on a detached job — a video
        # analysis, a document being built — is not chatting; counting it
        # would make the job pace itself against itself.
        return any(
            not g.done and not getattr(g, "waiting_on_video", False) and not getattr(g, "waiting_on_job", False)
            for g in list(_live_generations.values())
        )

    video_pipeline.set_busy_probe(_chat_is_busy)
    # Artifact Studio (2026-09-11): documents, decks and workbooks made in
    # chat run as durable jobs with the same lease/heartbeat/requeue shape as
    # video analyses. The composer — the one model-facing piece — is
    # installed here so the runner can be tested with a stub.
    from .artifacts import pipeline as artifact_pipeline
    from .engines import artifact as artifact_engine

    artifact_pipeline.set_composer(artifact_engine.compose_for_pipeline)
    artifact_pipeline.set_visual_reviewer(artifact_engine.visual_reviewer)
    artifact_pipeline.install_busy_probe(_chat_is_busy)
    await artifact_pipeline.start()
    # The developer platform (CONTRACT-3). Two pieces of wiring, both here
    # because both need the pool open and the schema applied.
    _configure_api_key_pepper()
    webhook_worker = await _start_webhook_worker()
    # Retention for the platform's tables (CONTRACT-3 §13/§16): idempotency
    # claims past 24 h, minute counters, expired responses, delivered webhooks.
    # `db.prune_api_platform` existed from wave 1 and NOTHING CALLED IT
    # (wave-2 verifier, 2026-09-13), so every one of those tables grew without
    # bound. On the artifact maintenance cadence, like the other sweeps here.
    api_prune_task = asyncio.get_running_loop().create_task(
        _api_platform_prune_loop(), name="api-platform-prune"
    )
    # The Files API's durable workers (files-hookup, 2026-09-13): processing
    # and assembly (`apifiles.jobs`, both lanes), purges and expiry
    # (`apifiles.retention`), and the upload sweep. After the pool and the
    # schema, like the webhook loop; their queues are PostgreSQL columns, so a
    # restart resumes rather than forgets.
    files_workers = await _start_files_workers()
    # Worker processes for pure-CPU work (app/core/cpu_pool.py; 0 = threads)
    # and the loop-lag probe behind orchestrator_event_loop_lag_seconds.
    from .core import cpu_pool as _cpu_pool

    _pool = _cpu_pool.start(settings.cpu_pool_workers, settings.cpu_pool_slots)
    if _pool.workers:
        # Fork the children from a thread before the first request, so the
        # first run_cpu call does not start the forkserver on the loop.
        try:
            await asyncio.to_thread(_pool.prestart)
        except Exception:  # noqa: BLE001 — first use retries; never a start-up gate
            logging.getLogger(__name__).exception("cpu_pool prestart failed")
    lag_probe_task = None
    if settings.event_loop_lag_probe:
        lag_probe_task = asyncio.get_running_loop().create_task(
            _latency_metrics.event_loop_lag_probe(), name="event-loop-lag-probe"
        )
    try:
        yield
    finally:
        if lag_probe_task is not None:
            lag_probe_task.cancel()
            # asyncio.wait never re-raises the probe's CancelledError, so it
            # cannot swallow a cancellation aimed at this shutdown itself.
            await asyncio.wait({lag_probe_task}, timeout=1.0)
        # FIRST: durable /v1 runs suspend while the pool is still open (their
        # suspend writes leases and specs). The signal callback normally did
        # this at SIGTERM; repeating it is idempotent and covers a shutdown
        # that arrived without a signal (a test client, a lifespan error).
        try:
            await _maybe_await(durable.suspend_all("shutdown"))
        except Exception:  # noqa: BLE001 — shutdown must continue
            logging.getLogger(__name__).exception("durable suspend_all failed at shutdown")
        try:
            await _maybe_await(durable.stop())
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("durable stop failed at shutdown")
        shutdown_signals.uninstall()
        # Orderly shutdown (a deploy's rolling recreate) is the common way a
        # generation dies. The rows are marked interrupted BEFORE the pool
        # closes so the next process resumes them; the flag makes a worker
        # cancelled by the loop teardown record 'interrupted' rather than
        # 'cancelled' — nobody pressed Stop.
        _shutting_down = True
        interrupted_requests = await _interrupt_open_requests()
        if interrupted_requests:
            logging.getLogger(__name__).info(
                "marked %d open chat request(s) interrupted for shutdown",
                interrupted_requests,
            )
        sweep_task.cancel()
        api_prune_task.cancel()
        # Before the pool closes: a job releases its blob (and an assembly its
        # lease) to `queued` on the way out, so the next process resumes at
        # once instead of waiting for a lease to lapse.
        await _stop_files_workers(files_workers)
        await _stop_webhook_worker(webhook_worker)
        await video_pipeline.stop()
        await artifact_pipeline.stop()
        await web_worker.stop()
        await continuity.stop()
        await engine_state.stop()
        # The reranker's pooled client (rerank.py, 2026-09-13) is per loop and
        # must be closed on the loop that owns its sockets, which is this one.
        from . import rerank as _rerank

        await _rerank.close_rerank_client()
        # Queued CPU work is cancelled and children joined within 5 s: well
        # inside uvicorn's 90 s graceful window.
        try:
            await _cpu_pool.stop()
        except Exception:  # noqa: BLE001 — shutdown must continue
            logging.getLogger(__name__).exception("cpu_pool stop failed at shutdown")
        await db.run_in_thread(db.close_pool)
        _restore_switch_interval(previous_switch_interval)


def _apply_switch_interval() -> Optional[float]:
    """Apply PY_SWITCH_INTERVAL_S when set; returns the interval it replaced
    (None = nothing changed). Read in the lifespan, never at import, so tests
    and tools that import the app keep the interpreter default."""
    wanted = float(getattr(settings, "py_switch_interval_s", 0.0) or 0.0)
    if wanted <= 0:
        return None
    previous = sys.getswitchinterval()
    try:
        sys.setswitchinterval(wanted)
    except (ValueError, TypeError):
        logging.getLogger(__name__).warning("PY_SWITCH_INTERVAL_S=%r refused", wanted)
        return None
    logging.getLogger(__name__).info(
        "GIL switch interval %.4f s (was %.4f s) from PY_SWITCH_INTERVAL_S",
        sys.getswitchinterval(), previous,
    )
    return previous


def _restore_switch_interval(previous: Optional[float]) -> None:
    if previous is not None:
        try:
            sys.setswitchinterval(previous)
        except Exception:  # noqa: BLE001
            pass


#: Imported in a worker thread by the lifespan. Cold, openai + its resource
#: modules and httpx cost about 220 ms of import on the production image; a
#: chat turn that imported one lazily held the event loop (and the import
#: lock) for that long.
_WARM_IMPORTS = ("httpx", "openai", "openai.resources.chat", "openai.resources.embeddings")


def _warm_imports_and_ssl() -> None:
    import importlib

    for name in _WARM_IMPORTS:
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 — a missing optional module is not fatal
            logging.getLogger(__name__).debug("warm import of %s failed", name, exc_info=True)
    from .core.net import shared_ssl_context

    shared_ssl_context()


async def _warm_process() -> None:
    """Make the first request as cheap as the hundredth. NO network I/O: every
    step is an import, a CA-bundle load or an object construction, so it is
    safe with every engine unreachable (tests/test_server_perf.py asserts no
    socket connects). Failures are logged and ignored: warm-up is an
    optimisation, never a start-up gate."""
    log = logging.getLogger(__name__)
    try:
        await asyncio.to_thread(_warm_imports_and_ssl)
    except Exception:  # noqa: BLE001
        log.debug("process warm-up failed", exc_info=True)
    # The model clients themselves are llm.py's (a private, LRU-bounded cache
    # keyed by loop/endpoint/timeout/transport); warming it from here with the
    # wrong key would warm nothing and could evict a live entry. Called only
    # when llm.py exposes the public hook.
    warm_clients = getattr(llm, "warm_clients", None)
    if callable(warm_clients):
        try:
            await _maybe_await(warm_clients())
        except Exception:  # noqa: BLE001
            log.debug("llm.warm_clients failed", exc_info=True)
    try:
        from .engines import search as _search

        _search.warm_extractor()
    except Exception:  # noqa: BLE001
        log.debug("extractor warm-up failed", exc_info=True)


async def _maybe_await(value):
    """Await `value` when it is awaitable; the durable interface may be sync
    or async per method, and the lifespan must not care."""
    if inspect.isawaitable(value):
        return await value
    return value


def _durable_module():
    """The durable /v1 generation runtime (`app/publicapi/durable.py`):
    `request_suspend(reason)` (sync, callable from the signal callback on the
    loop), `start()`, `suspend_all(reason)`, `stop()`.

    WHY (assembler, 2026-09-14): T1's no-op stand-in for a missing module is
    gone now that T2's module is in the tree. An import failure here is a bug
    and fails start-up rather than silently disabling resumption. A function,
    not an import at module scope, so the lifespan test can substitute it."""
    from .publicapi import durable

    return durable


def _durable_signal_callback(durable):
    """What SIGTERM/SIGINT schedule on the loop: suspend every durable run
    for a restart. The signal NAME is logged, the reason is always 'restart'
    (the V36 suspend_reason vocabulary)."""

    def on_signal(name: str) -> None:
        logging.getLogger(__name__).info("%s received: suspending durable /v1 runs for a restart", name)
        durable.request_suspend("restart")

    return on_signal


def _configure_api_key_pepper() -> None:
    """Bind the API-key pepper to its durable store (CONTRACT-3 §5).

    `apiplatform/keys.py` hashes every key secret as HMAC-SHA256(pepper,
    secret). It refuses to invent a pepper on its own, and it must be told
    where one lives exactly once per process, at start-up — which is here,
    because this is the only module that is allowed to know about both the
    key package and `db`.

    THE SAVER IS INSERT-IF-ABSENT AND MUST STAY THAT WAY. `db.set_platform_secret`
    is `INSERT … ON CONFLICT DO NOTHING` followed by a re-read, so two processes
    racing on a fresh install converge on ONE pepper. An overwriting saver would
    invalidate every stored `key_hash` in the workspace at once, and the symptom
    would be every customer's key answering 401 simultaneously with nothing in
    the logs to say why.

    Wrapped because a platform that cannot be wired must not stop the chat
    application from starting: the API surface then refuses keys (which is the
    safe direction), and the ERROR line says so.
    """
    try:
        from .apiplatform import keys as api_keys

        api_keys.configure_pepper_store(
            loader=lambda: db.get_platform_secret(api_keys.PEPPER_SECRET_NAME),
            saver=lambda value: db.set_platform_secret(
                api_keys.PEPPER_SECRET_NAME, value
            ),
        )
    except Exception as exc:  # noqa: BLE001 — never a secret in the message
        logging.getLogger(__name__).error(
            "the API key pepper store could not be wired (%s); API keys will be "
            "refused until it is",
            type(exc).__name__,
        )


async def _start_webhook_worker():
    """Start the webhook delivery loop.

    CONTRACT-3 §14: a background response's `response.completed` reaches the
    project's endpoint through a durable queue (`api_webhook_deliveries`, V34),
    drained by one in-process loop — the same shape as `web_worker` and
    `continuity` above, and for the same reason: the queue is a PostgreSQL
    table, so a restart resumes rather than forgets and no second container is
    needed to send a few HTTPS requests a minute. `worker.start()` is itself
    idempotent and does nothing when the platform is switched off, so there is
    no gate to duplicate here.

    Imported defensively, like the `/v1` router below and for the same reason:
    `app/apiplatform/webhooks/` belongs to another engineer in this wave, and a
    half-written module there must not stop this process from serving chat.
    Returns the module when it started and None when it did not, so the
    shutdown half has something unambiguous to check rather than guessing.
    """
    try:
        from .apiplatform.webhooks import worker as webhook_worker
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).info(
            "webhook delivery loop not started: %s", type(exc).__name__
        )
        return None
    try:
        started = webhook_worker.start()
        if inspect.isawaitable(started):
            # `start()` is sync in web_worker/continuity and a coroutine in the
            # video and artifact pipelines. Accepting both means this wiring
            # does not change the day the worker's author picks one.
            await started
        return webhook_worker
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).error(
            "webhook delivery loop failed to start: %s: %s",
            type(exc).__name__,
            exc,
        )
        return None


def _limit_core_dumps_for_children() -> bool:
    """RLIMIT_CORE = 1 for this process and every child it starts.

    Files design §13.5 (2026-09-13): the media lane starts ffmpeg and ffprobe
    on uploaded files, and a crashing child must not produce a core dump —
    writing one is slow and none is ever read. 1, not 0: 1 is the value the
    kernel treats as "no core" for every core handler, matching the compose
    `ulimits: core: 1` the vLLM services already use. The extraction child
    sets it itself; this covers the in-process ffmpeg children. Best effort:
    a hard limit already below 1 is left as it is.
    """
    try:
        import resource

        _soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        if hard != resource.RLIM_INFINITY and hard < 1:
            return False
        resource.setrlimit(resource.RLIMIT_CORE, (1, 1))
        return True
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "RLIMIT_CORE could not be set for child processes: %s", type(exc).__name__
        )
        return False


async def _start_files_workers():
    """Start the Files API workers, or None when they cannot run here.

    Guarded like the webhook loop: `app/apifiles/` is new this wave and must
    not stop chat from starting. The storage root is created first; when it
    cannot be (a development box without /data), the workers are NOT started —
    every job would only fail on the disk — and the ERROR line says so, while
    the routes answer `503 storage_unavailable` on their own.
    Returns the three modules that started, for the shutdown half."""
    log = logging.getLogger(__name__)
    _limit_core_dumps_for_children()
    try:
        from .apifiles import jobs as files_jobs
        from .apifiles import retention as files_retention
        from .apifiles import storage as files_storage
        from .apifiles import uploads_sweep as files_uploads_sweep
    except Exception as exc:  # noqa: BLE001
        log.error("the Files API workers were not started: %s", type(exc).__name__)
        return None
    try:
        await db.run_in_thread(files_storage.ensure_dirs)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "the Files API storage root is unusable (%s); processing, retention and "
            "the upload sweep are not started",
            type(exc).__name__,
        )
        return None
    started = []
    for name, module in (
        ("processing jobs", files_jobs),
        ("retention", files_retention),
        ("upload sweep", files_uploads_sweep),
    ):
        try:
            await module.start()
            started.append(module)
        except Exception as exc:  # noqa: BLE001
            log.error("the Files API %s did not start: %s: %s", name, type(exc).__name__, exc)
    return started


async def _stop_files_workers(started) -> None:
    """Stop what `_start_files_workers` started, last started first. Never raises."""
    for module in reversed(list(started or ())):
        try:
            await module.stop()
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "a Files API worker did not stop cleanly: %s: %s", type(exc).__name__, exc
            )


async def _stop_webhook_worker(worker) -> None:
    """Stop the delivery loop. Never raises: shutdown has other work to do."""
    if worker is None:
        return
    try:
        stopping = worker.stop()
        if inspect.isawaitable(stopping):
            await stopping
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "webhook delivery loop did not stop cleanly: %s: %s",
            type(exc).__name__,
            exc,
        )


import re as _re

#: Citation markers in a streamed answer — `[1]`, `[12]`. Used only to mark
#: which of the sources already in `meta.sources` the answer leant on; it never
#: invents a source, so a stray number simply matches nothing.
_CITE_MARKER_RE = _re.compile(r"\[(\d{1,3})\]")
#: Cap on the per-request answer buffer (pieces, not bytes). A normal answer is
#: a few hundred token frames; this only has to survive long enough to find the
#: markers, and must not grow without bound on a runaway generation.
_MAX_STREAM_PIECES = 20000

#: Client-supplied conversation ids (same rule as POST /history/conversations).
_CONVERSATION_ID_RE = _re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: A send intent (V29): browser-minted, one per press of Send, re-sent on
#: every retry of that message. Same alphabet as a conversation id.
_INTENT_ID_RE = _re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: 2026-09-13 (the duplicate answers): where a regenerate, an edit or a send
#: in a conversation with versions asks its ANSWER to be stored in the
#: browser's conversation tree (frontend/lib/branching.ts). `self` is the
#: answer's branch id (the browser derives it from the intent); `parent` is the
#: question's — its own branch id, or the positional `#<index>` a message
#: without one is known by.
_ANSWER_BRANCH_SELF_RE = _re.compile(r"^b-[A-Za-z0-9_-]{1,64}$")
_ANSWER_BRANCH_PARENT_RE = _re.compile(r"^(?:b-[A-Za-z0-9_-]{1,64}|#[0-9]{1,6})$")
#: A bare call's session label. SAME shape rule as a conversation id since
#: 2026-09-12 (F034): it is half of the synthetic conversation key below, so an
#: unvalidated session_id was an unvalidated conversation key — free to contain
#: a path separator, a newline, or 4 KB of anything, and it is what names the
#: in-process generation registry, the per-conversation document store and the
#: Salesforce Intelligence state row.
_SESSION_ID_RE = _re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: The SYNTHETIC conversation key a bare call gets: `u<user id>-<session id>`
#: (see `scoped_session` in POST /chat). It lives in the SAME namespace as the
#: ids clients choose, and that shared namespace is F034 (audit 2026-09-12,
#: confirmed P1): user A could send `conversation_id="u7-default"`, claim it as
#: an ordinary conversation, and thereafter own the key that user 7's bare
#: calls fall back to — reading their fetched pages, their indexed repository
#: chunks and their pending Salesforce clarification, and cancelling their
#: generations. Nothing legitimate produces such an id: the browser mints a
#: UUID (`newId()` in frontend/lib/history.ts) and a branch is `b-<uuid>`, so
#: refusing this shape costs nothing and closes the namespace.
_SYNTHETIC_CONV_KEY_RE = _re.compile(r"^u\d+-")
#: Offline evaluation correlation only. It carries no expected answer.
_TEST_CASE_ID_RE = _re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _checked_session_id(value: str) -> str:
    """The session label of a bare call, validated like the conversation id it
    becomes half of (F034, 2026-09-12).

    Until this existed `session_id` was an unconstrained string concatenated
    straight into the synthetic conversation key, so it could carry anything at
    all — a path separator, a newline, four kilobytes of text — into the keys
    of the live-generation registry, the per-conversation URL/document/
    repository stores and the Salesforce state table. Same alphabet and length
    as a conversation id, because the two share one namespace.
    """
    if not _SESSION_ID_RE.fullmatch(value or ""):
        raise ValueError("session_id must be 1-64 characters of [A-Za-z0-9_-]")
    return value


def _refuse_reserved_conversation_key(conversation_id: str) -> None:
    """404 for a Salesforce lookup on an id shaped like somebody's bare-call key.

    Belt to the braces of the ownership check beside it, and not redundant:
    ownership is not the only way such a row can come to exist. Until
    2026-09-13 `POST /history/conversations` accepted any id of the plain
    conversation-id shape, `u7-default` included, so an attacker who could not
    send it to /chat could CREATE the row there, become its owner, and read
    user 7's Salesforce state through the two routes below, which would see
    `owner == viewer` and answer honestly. history.py refuses the shape at
    creation now; rows created before that still exist, and these two routes
    will not serve them to anybody.
    """
    if _SYNTHETIC_CONV_KEY_RE.match(conversation_id or ""):
        raise HTTPException(status_code=404, detail="conversation not found")


def _reject_synthetic_conversation_id(value: Optional[str]) -> Optional[str]:
    """Refuse a client-chosen id that looks like somebody's bare-call key.

    The other half of F034. The claim path in POST /chat is careful about ids
    that ALREADY belong to someone; the hole was the id that belongs to nobody
    YET — `u7-default` is not a conversation, so `db.conversation_owner`
    returns None, so the first caller to name it is handed the claim, and from
    then on it is their conversation and user 7's bare calls write into it.
    Refused at the door, once, rather than at each place the key is used.

    Deliberately shape-only: it does NOT ask whether the number is the caller's
    own user id. `u7-default` is refused for user 7 as well, because a caller
    who wants that conversation can simply use their real one, and a rule with
    an exception is a rule somebody will find the edge of.
    """
    if value is not None and _SYNTHETIC_CONV_KEY_RE.match(value):
        raise ValueError("conversation_id must not begin with u<digits>- (reserved)")
    return value

#: Paths under the public developer API (CONTRACT-3 §1/§3). Everything about
#: `/v1` is different from the chat app: the credential is an `Authorization`
#: header and never the `ts_session` cookie, the error envelope is §9's rather
#: than FastAPI's `{"detail": …}`, and the body cap is §12's 1 MiB. The prefix
#: is named ONCE here so the three places that must agree cannot drift.
_PUBLIC_API_PREFIX = "/v1/"
_PUBLIC_API_ROOT = "/v1"


def _is_public_api_path(path: str) -> bool:
    """True for `/v1` and anything under it, and for nothing else.

    `/v1betaX` must NOT match: a prefix test written as `startswith("/v1")`
    would exempt a future route with a longer name from the CSRF middleware
    below, which is precisely the kind of quiet widening this programme exists
    to avoid.
    """
    return path == _PUBLIC_API_ROOT or path.startswith(_PUBLIC_API_PREFIX)


# --------------------------------------------------------------- body size
#
# F016/F033 (audit 2026-09-12, confirmed P1): nothing in this process bounded a
# request body. The first fix (2026-09-12) put ONE 128 MiB cap on every route
# that was not an upload or `/v1`, sized for the largest legitimate /chat body.
#
# THE SECOND FIX, 2026-09-13 (wave-2 verifier, "unauthenticated memory
# exhaustion is still open"). FastAPI reads and json-decodes a body BEFORE any
# auth dependency runs, so a cap sized for /chat was a cap any anonymous caller
# could spend on /auth/login: a 32 MiB `[{},{},…]` with no cookie took the
# process from 158 to 989 MiB of RSS before answering 422, ~3 GB per request
# at the cap, repeatable concurrently on a port published beside the engine.
#
# So the cap is now PER ROUTE FAMILY and, above the small default, PER
# CREDENTIAL:
#
#   * every route gets 1 MiB — the largest legitimate body of any form, login,
#     preference, share or console mutation is a few KiB;
#   * the handful of routes that really carry large bodies (POST /chat, the
#     whole-thread history sync, the microphone, the upload rail) get their
#     documented larger cap ONLY once the request's `ts_session` cookie has
#     resolved to a live session. Resolution is the same `resolve_principal_sync`
#     the route's own `require_user` runs, cached on the request state, so the
#     route does not pay for it twice;
#   * an anonymous caller is held to 1 MiB on every route, whatever it names.
#
# The session is resolved LAZILY: a body that fits inside 1 MiB never costs a
# lookup, and one that does not is decided either up front from its declared
# `Content-Length` or at the moment the counted bytes cross 1 MiB. Nothing is
# buffered here in either case.

_MIB = 1024 * 1024

#: Every route not named in `body_cap_for`: 1 MiB (2026-09-13). Overridable
#: with MAX_REQUEST_BODY_BYTES, which only ever TIGHTENS it in practice.
_DEFAULT_MAX_BODY_BYTES = _MIB

#: POST /chat, for a signed-in caller: 128 MiB.
#:
#: /chat carries its attachments INLINE as base64 — images (`images`, up to
#: MAX_IMAGES), a small PDF (`pdf`) — which is why
#: `frontend/app/api/chat/route.ts` sets MAX_CHAT_BODY_BYTES to exactly this
#: number. Base64 costs a third on top of the bytes, so 128 MiB of body is
#: ~96 MiB of attachment; anything larger already streams to `/uploads` and
#: rides the chat body as a reference. Matching the proxy's number on purpose:
#: the two caps refuse the same request.
_CHAT_MAX_BODY_BYTES = 128 * _MIB

#: The whole-thread routes, for a signed-in caller: 32 MiB. PUT/POST
#: `/history/conversations/{id}/messages` and POST `/chat/compact` carry a
#: conversation's messages, and a paste has no size limit on the input path
#: (composer paste is inline, 2026-09-06), so a long chat is legitimately
#: megabytes. 32 MiB is `MAX_PROXY_BODY_BYTES` in `frontend/lib/proxy.ts`, the
#: bound these routes already have on the public hostname.
_CONVERSATION_SYNC_MAX_BODY_BYTES = 32 * _MIB

#: Multipart framing allowance: the boundary, the filename, and the
#: `conversation_id`/`purpose` fields that travel beside the file. The same
#: 1 MiB `frontend/app/api/upload/route.ts` allows.
_MULTIPART_FRAMING_BYTES = _MIB

_HISTORY_MESSAGES_PATH_RE = _re.compile(r"^/history/conversations/[^/]+/messages$")
_CHUNKED_PART_PATH_RE = _re.compile(r"^/uploads/chunked/[^/]+/[^/]+/part/[^/]+$")


class BodyCap:
    """What one request may send: `anonymous` bytes without a session, and
    `signed_in` bytes once its session cookie resolves. For every small family
    the two are the same number and no session is ever looked up."""

    __slots__ = ("family", "anonymous", "signed_in")

    def __init__(self, family: str, anonymous: int, signed_in: int) -> None:
        self.family = family
        self.anonymous = int(anonymous)
        self.signed_in = max(int(signed_in), int(anonymous))

    def __repr__(self) -> str:  # pragma: no cover — test failure output
        return f"BodyCap({self.family!r}, {self.anonymous}, {self.signed_in})"


def _positive_int_setting(attribute: str, env: str, default: int) -> int:
    """A byte cap from `settings` (when the config owner adds it) or the
    environment. A typo or a non-positive value keeps the default: a mistyped
    variable must never silently REMOVE a cap."""
    configured = getattr(settings, attribute, None)
    if configured is None:
        raw = (os.environ.get(env) or "").strip()
        if not raw:
            return default
        try:
            configured = int(raw)
        except ValueError:
            logging.getLogger(__name__).warning("%s is not an integer; using the default", env)
            return default
    configured = int(configured)
    return configured if configured > 0 else default


def _max_request_body_bytes() -> int:
    """The default cap every route gets, read at call time."""
    return _positive_int_setting(
        "max_request_body_bytes", "MAX_REQUEST_BODY_BYTES", _DEFAULT_MAX_BODY_BYTES
    )


def _chat_max_body_bytes() -> int:
    return _positive_int_setting(
        "chat_max_request_body_bytes", "CHAT_MAX_REQUEST_BODY_BYTES", _CHAT_MAX_BODY_BYTES
    )


def _single_shot_upload_max_body_bytes() -> int:
    """POST /uploads: what `_stream_to_disk` will actually accept, plus framing.

    CORRECTED 2026-09-13 (wave-2 verifier). This used to be
    `max(UPLOAD_MAX_MB, VIDEO_MAX_UPLOAD_MB)` on the premise that a single-shot
    video could be 4096 MB. It cannot: `uploads._stream_to_disk` counts every
    purpose against `UPLOAD_MAX_MB` (200), and only the CHUNKED rail applies
    the per-purpose `_cap_total`. The ceiling was therefore ~20x anything the
    route would keep, while the multipart parser spooled whatever arrived to
    temp storage — measured, an unauthenticated POST pulled 300 MiB before its
    401. Read from `settings` at call time so raising UPLOAD_MAX_MB for one
    deployment moves this with it.
    """
    return int(settings.upload_max_mb) * _MIB + _MULTIPART_FRAMING_BYTES


def _chunked_part_max_body_bytes() -> int:
    """One chunked part is a RAW body (`request.stream()`), not multipart, and
    the route refuses anything over `uploads._PART_CAP` (90 MiB) itself."""
    from .uploads import _PART_CAP

    return int(_PART_CAP)


def _audio_max_body_bytes() -> int:
    """POST /audio/transcribe streams the recording itself under
    `ASR_MAX_UPLOAD_BYTES`; the middleware must not refuse below that."""
    return int(getattr(settings, "asr_max_upload_bytes", 0) or 0)


def _public_api_max_body_bytes() -> int:
    """CONTRACT-3 §8/§12: 1 MiB on `/v1`, refused BEFORE parsing — the
    fallback of `_public_api_body_cap`, whose per-route table (images, audio,
    and since the Files hookup the file routes' 65/64 MiB) is
    `publicapi.models.body_cap_for`.

    Imported defensively because `app/publicapi/models.py` is another wave's
    file and this module must import cleanly whatever state that wave is in.
    """
    try:
        from .publicapi.models import max_body_bytes

        return int(max_body_bytes())
    except Exception:  # noqa: BLE001 — a missing sibling must not unmount /chat
        return _MIB


def _public_api_body_cap(method: str, path: str) -> int:
    """The `/v1` cap for one request line, from the public API's own table.

    INTEGRATED 2026-09-13 (the six-model wave). CONTRACT §8/§12 now give the
    two generating routes 20 MiB (`PUBLIC_API_MAX_MEDIA_BODY_BYTES`, image
    parts) and `POST /v1/audio/transcriptions` 26 MiB
    (`PUBLIC_API_MAX_AUDIO_BODY_BYTES`); every other `/v1` line keeps the
    1 MiB of `PUBLIC_API_MAX_BODY_BYTES`. The Next edge enforces the same
    table, and before this the middleware capped every `/v1` path at 1 MiB,
    so an image request or an audio upload over 1 MiB was refused before the
    router saw it. `publicapi.models.body_cap_for` is the one table; this
    only asks it. Anonymous and signed-in are the same number on `/v1`, which
    never reads the session cookie — the router's key check and its own text
    limit (`ResponsesRequest.text_bytes`, 1 MiB) still apply after parsing.

    Same defensive import as `_public_api_max_body_bytes`: a broken sibling
    falls back to the small cap, never to no cap and never to an unmounted
    `/chat`.
    """
    try:
        from .publicapi.models import body_cap_for as public_body_cap_for

        cap = int(public_body_cap_for(method, path))
    except Exception:  # noqa: BLE001 — a missing sibling must not unmount /chat
        return _public_api_max_body_bytes()
    return cap if cap > 0 else _public_api_max_body_bytes()


def body_cap_for(method: str, path: str) -> BodyCap:
    """The cap for one request line. Separate from the middleware so a test can
    assert the table without building a request.

    Method-aware on purpose: the large caps belong to the one verb that carries
    the large body, so `PUT /chat` or `POST /uploads/chunked/init` (a form of
    five short fields) is held to the default like everything else.
    """
    method = (method or "GET").upper()
    default = _max_request_body_bytes()
    if _is_public_api_path(path):
        # Not session-gated: `/v1` never reads the cookie (CONTRACT-3 §1). The
        # per-route public table decides (20 MiB images, 26 MiB audio, else
        # 1 MiB) — see `_public_api_body_cap`.
        public = _public_api_body_cap(method, path)
        return BodyCap("public-api", public, public)
    if method == "POST" and path == "/chat":
        return BodyCap("chat", default, _chat_max_body_bytes())
    if (method in ("PUT", "POST") and _HISTORY_MESSAGES_PATH_RE.match(path)) or (
        method == "POST" and path == "/chat/compact"
    ):
        return BodyCap("conversation-sync", default, _CONVERSATION_SYNC_MAX_BODY_BYTES)
    if method == "POST" and path == "/audio/transcribe":
        return BodyCap("audio", default, _audio_max_body_bytes())
    if method == "POST" and path == "/uploads":
        return BodyCap("upload", default, _single_shot_upload_max_body_bytes())
    if method == "PUT" and _CHUNKED_PART_PATH_RE.match(path):
        return BodyCap("upload-part", default, _chunked_part_max_body_bytes())
    if method == "PUT" and _CONSOLE_FILE_PART_PATH_RE.match(path):
        return BodyCap("console-file-part", default, _CONSOLE_FILE_PART_MAX_BODY_BYTES)
    return BodyCap("default", default, default)


#: The developer console's Files tab uploads through the SAME upload handlers
#: as `/v1`, in 8 MiB parts (`CONSOLE_PART_BYTES` in
#: frontend/components/devplatform/files-api.ts, which the BFF also enforces).
#: Exact path, PUT only, and — like every large cap here — only for a caller
#: whose session resolved (files-hookup, 2026-09-13): without it every console
#: part over 1 MiB was a 413 before the handler ran.
_CONSOLE_FILE_PART_PATH_RE = _re.compile(
    r"^/admin/api/developers/projects/[^/]+/uploads/[^/]+/parts/[^/]+$"
)
_CONSOLE_FILE_PART_MAX_BODY_BYTES = 8 * _MIB


def body_limit_for_path(path: str, method: str = "POST") -> int:
    """The LARGEST body one path accepts — the signed-in cap. Kept for callers
    that ask one number of a path; an anonymous caller gets `body_cap_for(…)
    .anonymous`, which is the default everywhere."""
    return body_cap_for(method, path).signed_in


async def _carries_live_session(scope) -> bool:
    """Whether this request's session cookie resolves to a signed-in person.

    Only ever asked when a body has outgrown the anonymous cap on a route whose
    signed-in cap is larger. Uses the SAME resolver as `require_user` and
    leaves its answer cached on `request.state`, so the route that follows does
    not look the session up again. Any failure — the database is down, the
    cookie is garbage — is "no": the small cap is the safe way round.
    """
    from starlette.requests import Request as _StarletteRequest

    request = _StarletteRequest(scope)
    try:
        from .authn import principal as _principal

        # Looked up through the module, never bound at import, so this is the
        # resolver `require_user` calls — whatever that is in this process.
        # Without a cookie it returns without touching the database.
        principal = await db.run_in_thread(_principal.resolve_principal_sync, request)
    except Exception as exc:  # noqa: BLE001 — refusing is the fallback
        logging.getLogger(__name__).info(
            "body cap: session could not be resolved (%s); applying the anonymous cap",
            type(exc).__name__,
        )
        return False
    return principal is not None


class RequestBodyTooLarge(HTTPException):
    """Raised out of the wrapped `receive` when a body outgrows its cap.

    An exception rather than a short read: handing the route a TRUNCATED body
    would turn "too large" into "malformed JSON", or worse, into a half-written
    upload that looked complete.

    It subclasses `HTTPException` for one specific reason, found while testing
    a chunked body (2026-09-12). FastAPI parses a request body inside
    `try: … except Exception: raise HTTPException(400, "There was an error
    parsing the body")`, with exactly one exemption — an `HTTPException` is
    re-raised untouched. A plain exception raised from `receive` was therefore
    swallowed and answered `400 There was an error parsing the body`, which is
    both the wrong status and a misleading one: the body was not malformed, it
    was too big. Inheriting puts the refusal back in our hands, and the
    dedicated handler registered below renders whichever envelope the surface
    owes — Starlette resolves handlers by walking `type(exc).__mro__`, so the
    more specific class wins over the generic `HTTPException` handler.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(
            status_code=413,
            detail=f"The request body is larger than {limit} bytes.",
        )
        self.limit = limit


class RequestBodySizeLimitMiddleware:
    """Refuse an over-sized body with 413, BEFORE anything parses it.

    Plain ASGI, not `BaseHTTPMiddleware`, because this has to wrap `receive`:
    `BaseHTTPMiddleware` only sees a `Request` object, by which point the body
    is whatever the route asked for. Two doors, because either alone is a hole:

    1. `Content-Length`, when the caller declares one. Cheapest possible
       refusal — not a byte of the body is read, which is the whole point of
       "before it is parsed".
    2. a counting wrapper, for a body with NO declared length (HTTP/1.1
       chunked transfer, and any client that simply lies). Nothing is buffered:
       the count rides the frames as they already flow to the route, so a
       200 MiB upload still crosses this process one MiB at a time.

    AND ONE GUARANTEE (2026-09-13): once the count has crossed the cap, the
    response the application starts is REPLACED with the 413, whatever it is.
    A route class that catches every exception — `/v1`'s `PublicRoute` turns
    anything it did not construct into a 500 `internal_error` — would otherwise
    turn "too large" into "our fault", and the caller would retry it.

    The 413 body is `{"detail": …}` — the shape `HTTPException` produces
    everywhere else and the frontend's upload proxy already renders — except on
    `/v1`, which owes CONTRACT-3 §9's envelope, `X-Request-Id` (§7) and its own
    CORS headers (§3.2), and gets all three here.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path") or "/"
        if _is_public_api_path(path) and (scope.get("method") or "GET") != "OPTIONS":
            # PHYSICAL GUARD (no-timeout /v1, 2026-09-13; app/resources.py):
            # above PUBLIC_API_FD_GUARD_RATIO of the open-file limit a NEW /v1
            # request is refused before a byte of its body is read or a
            # header is sent. Here, in the one middleware every request
            # crosses, because the stack is pinned (test_publicapi_mount.py)
            # and chat routes must never see this refusal. A preflight holds
            # nothing and is left alone.
            from . import resources as _resources

            if _resources.fd_guard_tripped():
                response = _fd_pressure_response(scope)
                return await response(scope, receive, send)
        cap = body_cap_for(scope.get("method") or "GET", path)
        limit = cap.anonymous
        session_checked = cap.signed_in <= cap.anonymous

        async def admit(size: int) -> bool:
            """Whether `size` bytes fit, raising the cap to the signed-in one
            the first time the anonymous cap is not enough."""
            nonlocal limit, session_checked
            if size <= limit:
                return True
            if not session_checked:
                session_checked = True
                if await _carries_live_session(scope):
                    limit = cap.signed_in
            return size <= limit

        declared = _declared_content_length(scope)
        if declared is not None and not await admit(declared):
            response = _too_large_response(scope, limit)
            return await response(scope, receive, send)

        counted = 0
        exceeded = False
        started = False
        replaced = False

        async def counting_receive():
            nonlocal counted, exceeded
            if exceeded:
                # The route asked again after being refused. Do not read one
                # more byte off the socket on its behalf.
                raise RequestBodyTooLarge(limit)
            message = await receive()
            if message.get("type") == "http.request":
                counted += len(message.get("body") or b"")
                if not await admit(counted):
                    exceeded = True
                    raise RequestBodyTooLarge(limit)
            return message

        async def guarded_send(message):
            nonlocal started, replaced
            kind = message.get("type")
            if kind == "http.response.start":
                if exceeded:
                    replaced = True
                    started = True
                    response = _too_large_response(scope, limit)
                    await response(scope, _no_more_body, send)
                    return
                started = True
            elif replaced:
                return  # the route's own body belongs to the response we dropped
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except RequestBodyTooLarge:
            if started:
                # The route had already begun answering (a streamed response
                # that reads its body late). The status line is spent, so the
                # only honest move is to stop feeding it; logged because it is
                # the one path here that cannot say 413.
                if not replaced:
                    logging.getLogger(__name__).warning(
                        "body over the %d byte cap on %s after the response began",
                        limit,
                        path,
                    )
                return
            response = _too_large_response(scope, limit)
            await response(scope, _no_more_body, send)


async def _no_more_body():
    """A `receive` for the 413 itself: the refused body is never read further."""
    return {"type": "http.disconnect"}


def _declared_content_length(scope) -> Optional[int]:
    """The caller's own `Content-Length`, or None when it is absent or not a
    plain non-negative integer. A malformed value is treated as ABSENT, never
    as zero: the counting wrapper is then what decides, which is the safe way
    round."""
    for name, value in scope.get("headers") or ():
        if name == b"content-length":
            try:
                declared = int(value.decode("latin-1").strip())
            except (ValueError, UnicodeDecodeError):
                return None
            return declared if declared >= 0 else None
    return None


def _scope_header(scope, name: bytes) -> Optional[str]:
    for key, value in scope.get("headers") or ():
        if key == name:
            try:
                return value.decode("latin-1")
            except UnicodeDecodeError:  # pragma: no cover — latin-1 decodes anything
                return None
    return None


#: Mirrors `publicapi/router.py` (another wave's file): headers a browser
#: client may READ off a cross-origin `/v1` response.
_PUBLIC_API_EXPOSE_HEADERS = "X-Request-Id, RateLimit, RateLimit-Policy, Retry-After"


def _public_api_decoration(scope) -> tuple:
    """`(request_id, headers)` every `/v1` response owes that main.py renders
    itself (CONTRACT-3 §7 and §3.2).

    ADDED 2026-09-13 (wave-2 verifier): the 413 from this file carried no
    `X-Request-Id`, `request_id: null`, and no CORS headers — `BrowserCors…`
    skips `/v1` and the router's `_decorate` never runs for a response the
    router did not produce — so a developer's browser SDK saw an opaque CORS
    failure instead of `request_too_large`. The request id reuses the one the
    router minted when it got that far; the origin is ECHOED, never `*`, and
    `Access-Control-Allow-Credentials` is never sent.
    """
    import uuid as _uuid

    state = scope.get("state") or {}
    request_id = state.get("public_request_id") or f"req_{_uuid.uuid4().hex}"
    headers = {"X-Request-Id": request_id}
    origin = _scope_header(scope, b"origin")
    if origin:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
        headers["Access-Control-Expose-Headers"] = _PUBLIC_API_EXPOSE_HEADERS
    return request_id, headers


def _too_large_response(scope, limit: int):
    """The 413, in whichever envelope the surface owes."""
    from fastapi.responses import JSONResponse

    path = scope.get("path") or "/"
    if _is_public_api_path(path):
        request_id, headers = _public_api_decoration(scope)
        try:
            from .publicapi import errors as _public_errors

            api_error = _public_errors.request_too_large(limit)
            headers.update(api_error.headers())
            return JSONResponse(
                status_code=api_error.status,
                content=api_error.envelope(request_id),
                headers=headers,
            )
        except Exception:  # noqa: BLE001 — never fail to refuse
            logging.getLogger(__name__).debug(
                "public error envelope unavailable", exc_info=True
            )
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "message": f"The request body is larger than the {int(limit)} byte limit.",
                        "type": "invalid_request_error",
                        "code": "request_too_large",
                        "param": None,
                        "request_id": request_id,
                    }
                },
                headers=headers,
            )
    return JSONResponse(
        status_code=413,
        content={"detail": f"The request body is larger than {limit} bytes."},
    )


def _fd_pressure_response(scope):
    """503 model_unavailable, Retry-After 30, in /v1's envelope."""
    from fastapi.responses import JSONResponse

    from . import resources as _resources

    request_id, headers = _public_api_decoration(scope)
    try:
        from .publicapi import errors as _public_errors

        api_error = _public_errors.model_unavailable(retry_after=_resources.FD_RETRY_AFTER_S)
        headers.update(api_error.headers())
        return JSONResponse(status_code=api_error.status, content=api_error.envelope(request_id), headers=headers)
    except Exception:  # noqa: BLE001 — never fail to refuse
        headers["Retry-After"] = str(_resources.FD_RETRY_AFTER_S)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "The model is not available at the moment.",
                    "type": "service_unavailable_error",
                    "code": "model_unavailable",
                    "param": None,
                    "request_id": request_id,
                }
            },
            headers=headers,
        )


class BrowserCorsExceptPublicApi:
    """`CORSMiddleware` for the chat application, and nothing at all for `/v1`.

    CONTRACT-3 §3.2 names two rules that pull in opposite directions: the
    browser allowlist here must NOT be widened (it carries
    `allow_credentials=True`, so every origin on it may drive the session
    cookie), and `/v1` must answer a preflight from ANY origin with 204 and
    must never send `Access-Control-Allow-Credentials`.

    Starlette's CORSMiddleware sits outside the router and answers every
    preflight itself, before routing — so with it in the path both rules were
    broken at once (measured 2026-09-12, `OPTIONS /v1/responses` from a
    developer's origin): `400 Disallowed CORS origin`, carrying
    `access-control-allow-credentials: true`. A developer's browser app could
    not make a single call, and the one header the contract forbids was on the
    reply that refused them.

    Widening the allowlist to fix it would have handed those same origins the
    session cookie on `/chat`. So the allowlist is untouched and the middleware
    is simply not applied to `/v1`, which does its own CORS in the router —
    permissive on preflight (it carries no credential and reveals nothing) and
    authorized on the actual request against the project's `allowed_origins`.
    """

    def __init__(self, app, **options) -> None:
        self.app = app
        self.cors = CORSMiddleware(app, **options)
        # Kept so a test can read the allowlist back off the running stack and
        # prove it was not widened.
        self.options = options

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and _is_public_api_path(scope.get("path") or "/"):
            return await self.app(scope, receive, send)
        return await self.cors(scope, receive, send)


def _dev_docs_enabled() -> bool:
    """Whether FastAPI's own interactive schema is served.

    F013/F026/F053/F076 of the 2026-09-12 audit, all one bug: `/docs`, `/redoc`
    and `/openapi.json` were served UNAUTHENTICATED on a port bound to every
    interface, and the document enumerated all 94 routes — the admin surface,
    the analytics console, the share governance routes and every path parameter
    they take. That is a free map of the application for anyone on the LAN, and
    since 2026-09-02 the same process is reachable through the Cloudflare
    tunnel's host network. Nothing in the product reads these three paths: the
    frontend talks to named routes, and the PUBLIC developer schema is a
    different document served by the `/v1` router (CONTRACT-3 §7,
    `GET /v1/openapi.json`), which is deliberately the public surface only.

    So it is OFF unless somebody explicitly turns it on for a development box.
    Read through `getattr` first because `app/config.py` is a single-owner file
    in this programme (OWNERSHIP.md) and this module must not need an edit
    there to be deployable; the environment variable is the fallback.
    """
    configured = getattr(settings, "dev_docs_enabled", None)
    if configured is not None:
        return bool(configured)
    raw = (os.environ.get("ORCHESTRATOR_DEV_DOCS") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


_DEV_DOCS = _dev_docs_enabled()

app = FastAPI(
    title="TechSara Orchestrator",
    version="0.2.0",
    lifespan=lifespan,
    # None means "do not mount the route at all" — not "mount it and refuse",
    # which would still confirm the application's identity to a scanner.
    docs_url="/docs" if _DEV_DOCS else None,
    redoc_url="/redoc" if _DEV_DOCS else None,
    openapi_url="/openapi.json" if _DEV_DOCS else None,
)

# F016/F033 (audit 2026-09-12, both P1): there was no body-size limit anywhere
# in this process. Starlette reads whatever a caller sends, so a single POST
# with a 4 GB JSON body was a memory-exhaustion button on a box that also runs
# the inference engine — and the frontend proxy's cap (added the same day) is
# not a boundary, because the orchestrator is reachable directly on the LAN
# (CONTRACT-3 §18). Added BEFORE the CORS middleware in this file so it ends up
# INSIDE it in the stack: a 413 then still carries the CORS headers a browser
# needs to read the status, instead of surfacing as an opaque network error.
app.add_middleware(RequestBodySizeLimitMiddleware)


# Local platform: ONLY the local Next.js frontend origins are allowed. A
# wildcard here would let any web page the user visits cross-origin read
# /reports and drive /chat against the synced Salesforce data (§1/§12).
# V2: allow_credentials so the ts_session cookie flows on /auth + /history.
#
# NOT widened for the developer platform, and not applied to it either — see
# BrowserCorsExceptPublicApi above for why those are the same decision.
app.add_middleware(
    BrowserCorsExceptPublicApi,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# V2 (V2-DESIGN §3c): /auth + /history are the account boundary; /chat and
# /reports* remain auth-free.
# CSRF: sessions ride a SameSite=Lax cookie, which already blocks cross-site
# POSTs from <form>/fetch — this middleware is the second layer. A state-
# changing request that CARRIES an Origin header must carry one of ours; the
# Next.js proxy strips Origin (server-to-server), so proxied traffic passes
# untouched, and GET/HEAD (including every SSE stream) is never affected.
_TRUSTED_ORIGINS = set(settings.cors_allow_origins)


# CROSS-CHAT RECALL CACHE, KEPT HONEST (2026-09-13). memory_semantic caches a
# user's 500 recall candidates — message text AND conversation title — for
# CROSS_CHAT_EMBEDDINGS_CACHE_S (60 s), and checks each hit against a
# fingerprint of message_embeddings (count, max id, max created_at). That
# fingerprint sees new vectors and every DELETE (a conversation deleted, a
# thread truncated or replaced: the vectors cascade away), but NOT a rename,
# a generated title, or an in-place UPDATE of messages.content, which leave
# message_embeddings untouched — so another chat could recall an old title, or
# a partial answer the finished one replaced, for up to a minute (prover,
# 2026-09-13). Those writes invalidate: the three history routes matched
# below, and main._overwrite_persisted_answer at its call sites. POST
# .../messages (an append, on every turn) is deliberately NOT one: a new
# message has no vector yet, and invalidating per turn would empty the cache.
#
# Checked inside _reject_cross_site_writes, after the route answered, rather
# than in a middleware of its own: the middleware stack is pinned
# (tests/test_publicapi_mount.py), that function already sees every write,
# and a GET — every /chat stream included — never reaches the check.
_RECALL_CACHE_WRITES = _re.compile(r"^/history/conversations/[^/]+(?P<title>/title)?/?$")


def _is_recall_cache_write(method: str, path: str) -> bool:
    """A rename/pin/archive (PUT), a delete (DELETE) or a generated title
    (POST .../title) of one conversation."""
    match = _RECALL_CACHE_WRITES.match(path or "")
    if match is None:
        return False
    if match.group("title"):
        return method == "POST"
    return method in ("PUT", "PATCH", "DELETE")


def _invalidate_cross_chat_recall(user_id: Optional[int]) -> None:
    """Forget cached recall candidates for this user (everyone when the user
    is unknown — correctness over a cache hit). Never raises."""
    try:
        from . import memory_semantic

        memory_semantic.invalidate_message_embeddings(int(user_id) if user_id is not None else None)
    except Exception:  # noqa: BLE001 — a cache must never fail a write
        logging.getLogger(__name__).warning("cross-chat recall cache invalidation failed", exc_info=True)


def _after_recall_cache_write(request: Request) -> None:
    """Invalidate for the signed-in user: the principal auth resolution cached
    on the request state (shared with the route through the scope)."""
    from .authn import principal as _principal_mod

    principal = getattr(request.state, _principal_mod._STATE_KEY, None)
    _invalidate_cross_chat_recall(getattr(principal, "user_id", None))


class RejectCrossSiteWrites:
    """The cross-site write refusal, as plain ASGI.

    CONTRACT-3 §3.1 — the ONE exemption. `/v1` is key-authenticated and
    cookie-blind: it reads `Authorization` and ignores `Cookie` entirely, so
    there is no ambient credential for a hostile page to ride and CSRF does
    not apply to it. Leaving it in would not have made anything safer, it
    would simply have broken the product: a developer's browser app sends its
    OWN origin on every `POST /v1/responses`, so every such call would be 403
    before the key was even read — a check that cannot distinguish an attack
    from the intended use is not a control.

    This is not a widening of authentication. A `/v1` request still proves
    who it is with a key the router resolves (CONTRACT-3 §4), and the
    project's own `allowed_origins` list is what decides whether a browser
    origin may use that key. Deliberately NOT extended to any other path.

    WHY PLAIN ASGI AND NOT `@app.middleware("http")` (adversarial review of
    T3-wire, 2026-09-14). That decorator is Starlette's BaseHTTPMiddleware,
    which re-streams every body through a memory stream and, when the app
    raises mid-body, ends that stream CLEANLY and re-raises only after the
    outer response is complete. It was the outermost middleware, so it wrapped
    `/v1` too: a committed synchronous response aborted after its status line
    and an SSE stream whose generator raised both reached the caller as a
    complete 200 (measured on uvicorn: `client_error=None`, where without it
    the client gets `httpx.RemoteProtocolError`). An SDK then parses a
    whitespace body as success instead of retrying, and the v1-gateway cannot
    see the incomplete read it re-attaches on. Pinned through the whole app by
    tests/test_publicapi_mount.py. Behaviour for every other path is the
    decorator's: the same 403, and the recall cache invalidated once the route
    answered 2xx/3xx — at its status line, before the body is sent, as
    `call_next` returning did.
    """

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or "/"
        if _is_public_api_path(path):
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method") or "GET")
        if method in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        origin = request.headers.get("origin")
        if origin and origin not in _TRUSTED_ORIGINS:
            from fastapi.responses import JSONResponse

            refusal = JSONResponse(status_code=403, content={"detail": "cross-site request refused"})
            await refusal(scope, receive, send)
            return
        if not _is_recall_cache_write(method, path):
            await self.app(scope, receive, send)
            return

        async def send_after_invalidating(message) -> None:  # noqa: ANN001
            if message.get("type") == "http.response.start" and 200 <= int(message.get("status") or 0) < 400:
                _after_recall_cache_write(request)
            await send(message)

        await self.app(scope, receive, send_after_invalidating)


app.add_middleware(RejectCrossSiteWrites)


# F015 (audit 2026-09-12, confirmed P1): a 422 from POST /chat echoed the
# ENTIRE request body back to the caller. FastAPI's default handler serialises
# `exc.errors()` verbatim, and pydantic v2 puts the offending value in each
# error's `input` key — for a body-level failure that value is the whole body,
# so one malformed field returned the conversation history, the base64 of every
# attached image and whatever else the request carried. Two ways that bites:
# the echo lands in proxy logs and error trackers that were never meant to hold
# message content, and it is a free amplifier (a few hundred bytes of request
# for megabytes of response).
#
# What a caller actually needs is WHICH field is wrong and WHY, which is `loc`
# and `msg`. `input` and `ctx` are dropped — `ctx` because it carries the value
# too for several pydantic error types, and because it can hold a raw exception
# object that is not JSON-serialisable. `url` (pydantic's documentation link)
# goes as well: it names the library and version, which is a free fingerprint.
#
# Registered for EVERY route, not just /chat: the same handler covers /history,
# /uploads, the admin surface and anything added later, so this cannot regress
# by someone adding a new POST.
#
# BOUNDED (2026-09-13, wave-3 re-verifier). Dropping `input` stopped the echo
# but not the amplification: the handler still returned ONE entry per failing
# element, so a cookie-less 1 MiB `{"message":"hi","images":[{},…]}` came back
# as a 33,090,737-byte 422 (~430x) and cost +429 MiB of RSS. The response now
# carries at most `history.MAX_VALIDATION_ERRORS` entries, each with a bounded
# `msg` and `loc`, plus the total when there were more. The large-body routes
# also stopped letting FastAPI build the error list at all (see
# `history.read_validated_body`); this bound is what holds for every other
# route.
@app.exception_handler(RequestValidationError)
async def _validation_error_without_input_echo(
    request: Request, exc: RequestValidationError
):
    from fastapi.responses import JSONResponse

    from .history import MAX_VALIDATION_ERRORS, bounded_validation_errors

    errors = exc.errors()
    total = getattr(exc, "error_total", None)
    if not isinstance(total, int):
        total = len(errors)
    content: dict = {"detail": bounded_validation_errors(errors)}
    if total > MAX_VALIDATION_ERRORS:
        content["errors_total"] = total
    return JSONResponse(status_code=422, content=content)


@app.exception_handler(RequestBodyTooLarge)
async def _body_too_large(request: Request, exc: RequestBodyTooLarge):
    """The 413 for a body whose size only became known while it was arriving.

    The `Content-Length` door in the middleware answers directly, because the
    route has not started; this is the OTHER door — a chunked body, or a client
    that under-declared — where the refusal surfaces as an exception raised out
    of `receive` in the middle of the route's own read. One renderer for both,
    so `/v1` is owed CONTRACT-3 §9's envelope and gets it either way.
    """
    return _too_large_response(request.scope, exc.limit)


from starlette.exceptions import HTTPException as _StarletteHTTPException  # noqa: E402


@app.exception_handler(_StarletteHTTPException)
async def _http_exception_in_the_surfaces_own_envelope(
    request: Request, exc: _StarletteHTTPException
):
    """FastAPI's `{"detail": …}` everywhere, except under `/v1`.

    ADDED 2026-09-13 (wave-2 verifier, CONTRACT-3 §9 "Error, everywhere"): an
    unmatched `/v1` path answered FastAPI's `405 {"detail":"Method Not
    Allowed"}` with `allow: OPTIONS` — the router's preflight catch-all
    `OPTIONS /v1/{rest:path}` makes every unknown path "exist" for OPTIONS
    only — and with no request id and no CORS headers, so an SDK parsing the
    one shape the contract promises got a different one, and a browser client
    got nothing it could read. The router's own `PublicRoute` cannot help: a
    path that matches no route never reaches it.

    A 405 whose only allowed method is OPTIONS is that catch-all, not a real
    method mismatch, so it is answered as the 404 it is. The code is
    `invalid_request_error`: §9's table is closed and has no generic
    "no such route" code, and a request for a path the API does not have is a
    malformed request. The HTTP status stays the true one.
    """
    from fastapi.exception_handlers import http_exception_handler

    if not _is_public_api_path(request.url.path):
        return await http_exception_handler(request, exc)
    from fastapi.responses import JSONResponse

    status = int(exc.status_code)
    allow = ((exc.headers or {}).get("Allow") or (exc.headers or {}).get("allow") or "").upper()
    if status == 405 and allow.replace(" ", "") in ("", "OPTIONS"):
        status = 404
    request_id, headers = _public_api_decoration(request.scope)
    if status == 404:
        message, code, kind = "Unknown API route.", "invalid_request_error", "invalid_request_error"
    elif status == 405:
        message, code, kind = "Method not allowed on this API route.", "invalid_request_error", "invalid_request_error"
        headers["Allow"] = allow
    elif status == 413:
        message, code, kind = "The request body is too large.", "request_too_large", "invalid_request_error"
    elif status >= 500:
        message, code, kind = "Something went wrong on our side.", "internal_error", "server_error"
    else:
        # Never `exc.detail`: a detail written for the chat application is not
        # vetted for the public surface (§9, no internals on the wire).
        message, code, kind = "The request could not be processed.", "invalid_request_error", "invalid_request_error"
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": kind,
                "code": code,
                "param": None,
                "request_id": request_id,
            }
        },
        headers=headers,
    )


app.include_router(auth_router)
app.include_router(history_router)

app.include_router(uploads_router)
# Speech to text for the composer. Its own router because it is the only
# route that takes audio, and the only one gated on Feature.VOICE_INPUT.
app.include_router(audio_router)
# Video understanding (2026-09-09): status for a running analysis. The
# upload itself rides the /uploads routes with purpose=video; the answers
# ride /chat. Gated on Feature.VIDEO_ANALYSIS and VIDEO_ANALYSIS_ENABLED.
from .video.api import router as video_router  # noqa: E402

app.include_router(video_router)
# Artifact Studio (2026-09-11): status, previews and downloads for the
# documents, decks and workbooks made in chat — by id, owner-scoped. The
# requests themselves ride /chat. Gated on Feature.ARTIFACTS and
# ARTIFACTS_ENABLED.
from .artifacts.api import router as artifacts_router  # noqa: E402

app.include_router(artifacts_router)
app.include_router(memory_router)
# Conversation sharing. Mounted at the app root because ONE of its routes —
# /public/shares/{token} — is the only endpoint in this application that
# answers without a session, and that exception is easier to audit when it is
# visible here rather than nested under an authenticated prefix.
app.include_router(share_router)
app.include_router(admin_router)
# The analytics console. Its own router because its gate is its own
# capability: SUPER_ADMIN only, where the admin surface above is
# capability-per-route (see authn/rbac.Cap.ANALYTICS_READ).
app.include_router(analytics_router)
# Share governance, same shape and for the same reason: deciding what may
# leave the workspace is SUPER_ADMIN's, not the day-to-day admin's.
app.include_router(shares_admin_router)

# ---------------------------------------------------------------- /v1 mount
#
# The public developer API (CONTRACT-3 §2, §7). Mounted LAST so the order of
# this file reads as "the product, then the platform", and because `/v1` shares
# no path prefix with anything above it.
#
# The import is defensive ON PURPOSE and this is not a style choice. Nine
# engineers are building this programme in one tree at once, and
# `app/publicapi/router.py` belongs to another of them. An unguarded import
# would mean that a syntax error, a half-finished module or a missing
# dependency in ANY file that router touches takes down /chat, /history,
# /auth and the admin console with it — the whole chat application would fail
# to start because a developer-platform module was mid-edit. The failure is
# logged at ERROR so it is impossible to ship this state without noticing, and
# `PUBLIC_API_MOUNTED` says out loud whether the surface is there, so a test
# and an operator can both ask instead of guessing from a 404.
#
# Nothing here weakens the mount: if the router imports, it is included with
# its own prefix and its own dependencies exactly as written by its owner.
PUBLIC_API_MOUNTED = False
PUBLIC_API_MOUNT_ERROR: Optional[str] = None
try:
    from .publicapi.router import router as public_api_router  # noqa: E402

    app.include_router(public_api_router)
    PUBLIC_API_MOUNTED = True
except Exception as _public_api_exc:  # noqa: BLE001 — see the comment above
    PUBLIC_API_MOUNT_ERROR = type(_public_api_exc).__name__
    logging.getLogger(__name__).error(
        "the public developer API (/v1) is NOT mounted: %s: %s",
        type(_public_api_exc).__name__,
        _public_api_exc,
    )

# ------------------------------------------------------ developer console
#
# The console's backend (CONTRACT-3 §2, §6, §17): `apiplatform/console_api.py`,
# prefix `/admin/api/developers`, every route gated on an `api.*` capability.
#
# MOUNTED 2026-09-13 BECAUSE IT NEVER WAS (wave-2 verifier). Wave 1 wrote the
# router and its suite, and the suite mounted the router onto the app itself
# "until the wiring lands" — so every console test was green while
# `GET /admin/api/developers/overview` answered 404 in the running process and
# the console at `/api` had no backend at all. `test_publicapi_mount.py` now
# asserts the path through the app WITHOUT that fixture.
#
# Guarded exactly like `/v1` above and for the same reason: a half-written
# platform module must not stop /chat from starting, and the flag says out
# loud which state the process is in.
CONSOLE_API_MOUNTED = False
CONSOLE_API_MOUNT_ERROR: Optional[str] = None
try:
    from .apiplatform.console_api import router as console_api_router  # noqa: E402

    app.include_router(console_api_router)
    CONSOLE_API_MOUNTED = True
except Exception as _console_api_exc:  # noqa: BLE001 — see the /v1 comment above
    CONSOLE_API_MOUNT_ERROR = type(_console_api_exc).__name__
    logging.getLogger(__name__).error(
        "the developer console API (/admin/api/developers) is NOT mounted: %s: %s",
        type(_console_api_exc).__name__,
        _console_api_exc,
    )


class LiveGeneration:
    """A model generation DETACHED from the HTTP request that started it.

    ChatGPT-style lifecycle: the worker task runs to completion even if the
    browser tab reloads or navigates away. Every SSE event is kept in an
    in-memory buffer, so any number of readers can `follow()` — the original
    POST /chat response, and later GET /chat/attach/{id} re-connections, which
    replay the buffer from the start and then stream live.

    If the generation finishes while NOBODY is attached, the answer is
    persisted server-side into the conversation (when the requester was
    signed in), so it is waiting in history after a reload. When a reader IS
    attached, the frontend persists as usual and the server stays out of it —
    that ordering avoids duplicate assistant messages.
    """

    def __init__(self, conversation_id: Optional[str], user_id: Optional[int]):
        self.conversation_id = conversation_id
        self.user_id = user_id
        # Idempotency key for this answer. Every attached client receives it in
        # the final meta and sends it back when persisting, so a reply watched
        # by two browsers is still stored exactly once (db.add_message).
        self.generation_id = uuid.uuid4().hex
        self.events: List[tuple] = []
        self.done = False
        self.cond = asyncio.Condition()
        self.subscribers = 0
        self.task: Optional[asyncio.Task] = None
        self.answer = ""
        self.final_meta: Optional[dict] = None
        self.cancelled = False
        self.failed = False
        # V29 durable intents: the send this generation answers, which
        # attempt of it this is, and whether the answer is already durable
        # (persisted by this process, keyed by generation_id). None/1/False
        # for a generation built outside /chat (tests, direct callers).
        self.intent_id: Optional[str] = None
        self.attempt: int = 1
        self.persisted = False
        # 2026-09-13: the tree position the request asked this answer to be
        # stored under (ChatRequest.answer_branch), written onto the stored
        # row's meta.branch. None = an ordinary send, stored exactly as before.
        self.answer_branch: Optional[dict] = None
        # The last status written to the chat_requests row, and the failure
        # text a 'failed' row records — the same sentence the error event
        # carried, never an upstream body.
        self.request_status = "accepted"
        self.error = ""
        self.error_code = ""
        # Cancelled because a NEWER message arrived for the conversation
        # (not Stop): its followers get a terminal frame saying so.
        self.replaced = False
        # Availability CONTRACT §8.4 (2026-09-12): why THIS attempt exists
        # when it is not the first — one of _RETRY_REASONS, recorded in the
        # attempt ledger (_attempt_record). "none" for a first attempt.
        self.retry_reason = "none"
        # CONTRACT §8.3 step 5: the wait for the main model outran
        # LLM_QUEUE_MAX_WAIT_S and the row was left `queued` for the resume
        # sweep. Set by continuity.Hold.expire; read by the worker's
        # terminal branch and by _settle_chat_request (the row is NOT
        # failed) and _attempt_record (the attempt was interrupted).
        self.parked = False
        # The row this generation was resuming was taken over by another
        # resumer (continuity.LeaseLost): the turn ends without answering
        # and without touching the row, which is theirs now.
        self.lease_lost = False
        # The buffer index and perf_counter stamp of the first token or
        # reasoning event (publish), and whether its relay time was reported.
        self.first_output_index: Optional[int] = None
        self.first_output_at: Optional[float] = None
        self.relay_overhead_s: Optional[float] = None
        self._relay_observed = False
        #: ChatRequest.effort, for the relay histogram's label.
        self.effort = ""

    async def publish(self, event: str, data: dict) -> None:
        async with self.cond:
            if event in _STREAMED_EVENTS and self.first_output_index is None:
                # Where the engine's first visible output entered the buffer,
                # and when: `follow` reports how long it took to reach the
                # wire (relay_overhead_seconds, plan item 1, 2026-09-13).
                self.first_output_index = len(self.events)
                self.first_output_at = time.perf_counter()
            self.events.append((event, data))
            self.cond.notify_all()

    async def finish(self) -> None:
        async with self.cond:
            self.done = True
            self.cond.notify_all()

    def _observe_relay(self, start: int, end: int, backlog: int) -> None:
        """Stamp relay_overhead_seconds: the first answer/reasoning event's
        time from `publish` to the write that carries it — once per
        generation, and only for a LIVE reader (a re-attach replaying the
        buffer would report its own lateness, not the relay's)."""
        first = self.first_output_index
        if self.relay_overhead_s is not None or first is None or first < backlog or not start <= first < end:
            return
        self.relay_overhead_s = time.perf_counter() - float(self.first_output_at or 0.0)
        if self.final_meta is not None:
            self.report_relay()  # the route is known: this write came late

    def report_relay(self) -> None:
        """Observe the stamped relay time once, under the answer's route.
        The first token is written long before the route is known (it rides
        the final meta), so the worker reports it after `done`; a reader
        still draining by then reports it itself."""
        if self.relay_overhead_s is None or self._relay_observed:
            return
        self._relay_observed = True
        _latency_metrics.relay_overhead(
            self.relay_overhead_s,
            route=str((self.final_meta or {}).get("route") or "unknown"),
            effort=self.effort,
        )

    async def follow(self) -> AsyncIterator[str]:
        """Replay buffered events, then stream live ones until the end.

        Planning, retrieval and a long thinking pass all produce NO events for
        minutes at a time. Waiting on the condition without a bound made the
        response body go completely silent for that whole stretch, and every
        idle-timeout in the path treats silence as a dead peer — Node/undici
        in the Next.js proxy cut the stream at 300s (UND_ERR_BODY_TIMEOUT) and
        the user was told the orchestrator was unreachable while the model was
        still working. Bounding the wait lets us emit an SSE comment instead:
        it carries no event, so the contract is untouched, but it proves the
        connection is alive. The frame is yielded OUTSIDE the condition's lock
        — holding it across a yield would block publish() for as long as the
        consumer takes to drain.

        COALESCING (2026-09-13, sse.COALESCE_SECONDS). Live token/reasoning
        events that arrive within the frame window of the previous write
        share ONE write: the reader sleeps out the rest of the window, then
        takes everything buffered. The first event after a quiet spell is
        written at once, a pending status/meta/done/error flushes at once,
        and the buffered backlog a re-attach replays is still written one
        frame per yield. The bytes are the per-event frames concatenated,
        unchanged.

        The hold applies ONLY behind a write that itself carried a streamed
        event, and only to kinds this reader has already been sent. So the
        first reasoning token and the first answer token are never held —
        not behind a status line ("Reading your documents…" 3 ms earlier),
        and not behind the reasoning that precedes the answer. Measured
        2026-09-13 before this rule: both first tokens waited 22.3 ms, a
        delay chat_ttft_seconds cannot see (it is stamped before publish).
        """
        self.subscribers += 1
        try:
            index = 0
            backlog = len(self.events)
            # When the last write that ENDED on a token/reasoning event went
            # out (None after any other write), and the streamed kinds this
            # reader has been sent: together they decide whether a hold may
            # apply (see the docstring).
            last_write: Optional[float] = None
            sent_kinds: set = set()
            while True:
                chunk: Optional[str] = None
                ends_streamed = False
                async with self.cond:
                    while index >= len(self.events) and not self.done:
                        try:
                            await asyncio.wait_for(
                                self.cond.wait(), HEARTBEAT_SECONDS
                            )
                        except asyncio.TimeoutError:
                            break  # idle — fall through and send a keep-alive
                    window = float(_sse.COALESCE_SECONDS)
                    hold = 0.0
                    if (
                        index < len(self.events)
                        and index >= backlog
                        and window > 0
                        and last_write is not None
                        and not self.done
                        and all(e in sent_kinds for e, _ in self.events[index:])
                    ):
                        hold = window - (time.perf_counter() - last_write)
                    if index < len(self.events) and hold <= 0:
                        start = index
                        if index < backlog:
                            chunk = sse_event(*self.events[index])
                            index += 1
                        else:
                            end = len(self.events)
                            chunk = "".join(sse_event(e, d) for e, d in self.events[index:end])
                            index = end
                        written = self.events[start:index]
                        sent_kinds.update(e for e, _ in written if e in _STREAMED_EVENTS)
                        ends_streamed = written[-1][0] in _STREAMED_EVENTS
                        self._observe_relay(start, index, backlog)
                    elif index >= len(self.events) and self.done:
                        break  # done and fully drained
                if hold > 0:
                    # Outside the lock: publish() keeps filling the buffer
                    # while this reader waits out the frame window.
                    await asyncio.sleep(hold)
                    continue
                if chunk is not None and index > backlog:
                    last_write = time.perf_counter() if ends_streamed else None
                yield chunk if chunk is not None else sse_comment()
        finally:
            self.subscribers -= 1


# One live generation per conversation key. Finished generations are removed
# immediately (attach on a finished one 404s and the client loads history).
_live_generations: dict = {}

#: Set by the lifespan's exit. A worker cancelled by the loop's teardown
#: after this is an INTERRUPTED request (resumable by the next process),
#: not a cancelled one — nobody pressed Stop.
_shutting_down = False


# Detached background-compaction tasks. Held so the event loop keeps a strong
# reference (an unreferenced task can be garbage-collected mid-run).
_background_tasks: set = set()


# ---------------------------------------------------------------- context reads
#
# CONTEXT ASSEMBLY, READ CONCURRENTLY (2026-09-13, performance plan item 4).
# Between MODE_RESOLVED and CONTEXT_ASSEMBLED the worker awaited about a dozen
# independent reads one after another — saved facts, cross-chat recall (a
# candidate scan plus a query embedding), repo keys, stored pages, documents,
# videos, uploads, in-conversation recall, the rolling summary — and that
# section cost p50 209 ms / p95 352 ms / max 443 ms even for 1-3 message
# histories (query_trace_events, n=46, 2026-09-11..13). None of those reads
# depends on another's result, so they now START together and the prompt is
# still built by the same sequential code, in the same order, from the same
# values: each call site asks `get` for its result instead of awaiting the
# read itself. The golden test (tests/test_context_assembly_golden.py) builds
# the assembled messages both ways and requires identical bytes.
#
# A read is only ever STARTED under a condition that is a superset of the
# one its call site checks, with the exact arguments that call site uses (one
# factory serves both), so a read the turn turns out not to need is wasted
# work, never different context. A call site whose read was not started — the
# flag off, or a condition the start could not foresee — reads inline, as it
# always did. Exceptions surface at the call site, inside the same try.


def _env_bool(name: str, default: bool) -> bool:
    """config.py's `_bool`, for tunables this module reads itself until the
    integration lead moves them into Settings."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    """config.py's `_int` (blank means the default)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _context_concurrent_reads_enabled() -> bool:
    """CONTEXT_CONCURRENT_READS (default on). Off restores the one-at-a-time
    reads exactly, which is also how the golden test builds its reference.
    A Settings attribute of the same name wins (config.py)."""
    return bool(getattr(settings, "context_concurrent_reads", _env_bool("CONTEXT_CONCURRENT_READS", True)))


def _context_reads_concurrency() -> int:
    """CONTEXT_READS_CONCURRENCY (default 6): reads one turn runs AHEAD at once."""
    return max(1, int(getattr(settings, "context_reads_concurrency", _env_int("CONTEXT_READS_CONCURRENCY", 6))))


def _context_reads_process_limit() -> int:
    """CONTEXT_READS_PROCESS_LIMIT (default 6): reads ALL turns run ahead at once."""
    return max(1, int(getattr(settings, "context_reads_process_limit", _env_int("CONTEXT_READS_PROCESS_LIMIT", 6))))


def _context_reads_max_turns() -> int:
    """CONTEXT_READS_CONCURRENT_MAX_TURNS (default 0 = no limit): a turn runs
    its reads ahead only while fewer than this many turns already do."""
    return int(getattr(settings, "context_reads_concurrent_max_turns", _env_int("CONTEXT_READS_CONCURRENT_MAX_TURNS", 0)))


#: Turns whose reads may run ahead right now (opened, not yet closed). Weak:
#: a turn object that is gone without its `close` can never hold a slot.
import weakref as _weakref  # noqa: E402

_turns_reading_ahead: "_weakref.WeakSet" = _weakref.WeakSet()

#: One process-wide semaphore per event loop (a semaphore binds to its loop).
_process_read_slots: dict = {}


def _process_read_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    entry = _process_read_slots.get(id(loop))
    if entry is None or entry[0] is not loop:
        _process_read_slots.clear()  # a finished test loop's semaphore is garbage
        entry = (loop, asyncio.Semaphore(_context_reads_process_limit()))
        _process_read_slots[id(loop)] = entry
    return entry[1]


class _ContextReads:
    """The context reads of ONE chat turn, started early and collected in order.

    Bounded twice. Per turn by CONTEXT_READS_CONCURRENCY (default 6): every
    read holds a pooled PostgreSQL connection (APP_DB_POOL_MAX 16) and an
    anyio worker thread (40, shared with the sync routes) while it runs.

    And across the PROCESS by CONTEXT_READS_PROCESS_LIMIT (default 6), added
    2026-09-13 after the second prover pass: with only the per-turn bound, 16
    concurrent Fast turns asked for ~96 reads against 16 connections and 40
    threads, and TTFT at c=16 was WORSE than HEAD (cached p50 601.9 -> 700.5
    ms, p95 915.9 -> 1009.9 ms, slower in 9 of 9 paired rounds; with the
    reads sequential it was back at 580.9 / 882.6 ms). A read started ahead
    that has not got a process slot by the time its call site asks for it is
    dropped and read inline, exactly as one-at-a-time did — so under a burst
    each turn degrades to HEAD's shape plus at most the shared slots, never
    to a queue behind other turns' reads.
    """

    def __init__(self, concurrent: bool) -> None:
        limit = _context_reads_max_turns() if concurrent else 0
        #: Read one at a time because enough turns already read ahead.
        self.load_shed = bool(concurrent and limit > 0 and len(_turns_reading_ahead) >= limit)
        self.concurrent = concurrent and not self.load_shed
        if self.concurrent:
            _turns_reading_ahead.add(self)
        self._tasks: dict = {}
        self._slots: Optional[asyncio.Semaphore] = None
        #: Reads that fell back to inline because no process slot came in time.
        self.inline_fallbacks = 0

    def start(self, key: str, factory) -> None:
        if not self.concurrent or key in self._tasks:
            return
        if self._slots is None:
            self._slots = asyncio.Semaphore(_context_reads_concurrency())
        slots = self._slots
        process = _process_read_semaphore()
        state = {"running": False}

        async def bounded():
            async with slots:
                async with process:
                    state["running"] = True
                    return await factory()

        task = asyncio.ensure_future(bounded())
        # A read nobody collects (the turn went another way, or failed first)
        # must not log "Task exception was never retrieved".
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        self._tasks[key] = (task, state)

    async def get(self, key: str, factory):
        entry = self._tasks.pop(key, None)
        if entry is None:
            return await factory()
        task, state = entry
        if not state["running"] and not task.done():
            # One loop turn to take a free slot: a read started just now has
            # not been scheduled yet, which is not the same as waiting.
            await asyncio.sleep(0)
        if not state["running"] and not task.done():
            task.cancel()
            self.inline_fallbacks += 1
            return await factory()
        return await task

    def close(self) -> None:
        """Drop whatever was started and never collected."""
        for task, _state in self._tasks.values():
            if not task.done():
                task.cancel()
        self._tasks.clear()
        _turns_reading_ahead.discard(self)


class _OrderedTraceWriter:
    """Runs a generation's trace writes one after another, behind the answer.

    Every trace write was an awaited INSERT on the path to the first token —
    the root row, REQUEST_RECEIVED, MODE_RESOLVED, CONTEXT_ASSEMBLED — about
    10-25 ms of a Fast turn (plan item 4, 2026-09-13). They now run on one
    background task in the order they were made, so sequence numbers and the
    root-before-events rule hold exactly as before. Traces are diagnostics
    (core/tracing.py: "persistence is best-effort"); a crash can lose the
    tail of one, which is acceptable for a trace and is why NOTHING the
    upload-reliability contract depends on (the chat_requests row, the
    answer) goes through here. The worker drains the queue before the stream
    closes, so a trace is complete by the time its response is.

    GROUP COMMIT (2026-09-14). With `on_idle`, the jobs only BUFFER their
    rows, and `on_idle` writes the buffer in one transaction once the queue
    is empty. While `hold()` says more is coming (the trace is not finished)
    the writer waits up to `window_s` for the next job before writing, so a
    turn's root, checkpoints and close land in one to three commits instead
    of eight. `flush()` cuts the wait short; the order of rows is the order
    of the jobs, as before.
    """

    def __init__(self, *, on_idle=None, hold=None, window_s: float = 0.0) -> None:
        from collections import deque

        self._jobs = deque()
        self._task: Optional[asyncio.Task] = None
        self._on_idle = on_idle
        self._hold = hold
        self._window_s = float(window_s)
        self._wake = asyncio.Event()
        self._flush_requested = False

    def submit(self, job) -> None:
        self._jobs.append(job)
        self._wake.set()
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._drain())
            _background_tasks.add(self._task)
            self._task.add_done_callback(_background_tasks.discard)

    async def _run_jobs(self) -> None:
        while self._jobs:
            job = self._jobs.popleft()
            try:
                await job()
            except Exception:  # noqa: BLE001 — a trace must never fail a turn
                logging.getLogger(__name__).warning("queued trace write failed", exc_info=True)

    async def _drain(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await self._run_jobs()
            if self._on_idle is None:
                return
            if self._window_s > 0 and self._hold is not None:
                deadline = loop.time() + self._window_s
                while self._hold() and not self._flush_requested and loop.time() < deadline:
                    self._wake.clear()
                    if self._jobs:
                        await self._run_jobs()
                        continue
                    try:
                        async with asyncio.timeout(max(0.0, deadline - loop.time())):
                            await self._wake.wait()
                    except TimeoutError:
                        break
                    await self._run_jobs()
            try:
                await self._on_idle()
            except Exception:  # noqa: BLE001 — a trace must never fail a turn
                logging.getLogger(__name__).warning("queued trace write failed", exc_info=True)
            if not self._jobs:
                return

    async def flush(self) -> None:
        self._flush_requested = True
        self._wake.set()
        while self._task is not None and not self._task.done():
            await asyncio.shield(self._task)


#: How long `done` may wait for the clarification cancel a turn started
#: beside its answer (sf_intel_state.cancel_pending: two UPDATEs, single-digit
#: ms on a healthy pool). Past it the stream ends anyway and the write lands
#: on its own; 2 s is far above a healthy write and far below the patience
#: of a person whose answer is already on screen (2026-09-13).
_CLARIFICATION_CANCEL_BOUND_S = 2.0


#: How long the end of a turn waits for its queued trace writes before the
#: stream closes anyway (2026-09-13). They keep landing on their own task; a
#: healthy INSERT is single-digit ms, and the answer is already on screen.
_TRACE_FLUSH_BOUND_S = 2.0


#: How long the trace writer holds a burst of rows for the next one before it
#: commits them anyway (group commit, 2026-09-14). A crash can lose at most
#: this much more of a trace's tail; the close of a trace and `flush()` never
#: wait for it.
_TRACE_COALESCE_S = 0.25


class _TurnEnd:
    """The end-of-turn bookkeeping, proof against a second cancellation.

    Each step runs as its own task behind `shield`, so a CancelledError that
    reaches the worker while it waits cannot stop the step, and is recorded
    here instead of unwinding the rest of the `finally`. The caller re-raises
    it once `_finalize_generation` has run. Exceptions from a step are the
    step's own business, as `contextlib.suppress(Exception)` made them.
    """

    def __init__(self) -> None:
        self.cancelled = False

    async def step(self, awaitable, *, bound: Optional[float] = None) -> None:
        task = asyncio.ensure_future(awaitable)
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        try:
            if bound is None:
                await asyncio.shield(task)
            else:
                await asyncio.wait_for(asyncio.shield(task), timeout=bound)
        except asyncio.CancelledError:
            self.cancelled = True
            if bound is None and not task.done():
                # Unbounded steps (the row's status, the finalize) must finish
                # before the next one starts, as they did in sequence before.
                await self._until_done(task)
        except Exception:  # noqa: BLE001 — each step is best-effort, as before
            pass

    async def _until_done(self, task: "asyncio.Task") -> None:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001
                return


async def _settle_clarification_cancel(task: Optional["asyncio.Task"]) -> None:
    """Wait, bounded, for a turn's clarification cancel to land. A task still
    running at the bound keeps running (shielded, strongly referenced)."""
    if task is None or task.done():
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=_CLARIFICATION_CANCEL_BOUND_S)
    except asyncio.TimeoutError:
        logging.getLogger(__name__).warning(
            "clarification cancel still running after %.1fs; the turn ends without it",
            _CLARIFICATION_CANCEL_BOUND_S,
        )
    except Exception:  # noqa: BLE001 — _cancel_pending_clarification already suppresses
        pass


def _trace_snapshot(details: Optional[dict]) -> Optional[dict]:
    """Details as they are NOW: a queued write runs later, and the dicts a
    caller passes (context_state, a meta) keep changing after the call."""
    if details is None:
        return None
    import copy

    try:
        return copy.deepcopy(details)
    except Exception:  # noqa: BLE001 — uncopyable: the bounded, JSON-safe form
        from .core.tracing import sanitize

        return sanitize(details)


class _QueuedTraceRecorder(TraceRecorder):
    """TraceRecorder whose writes go through `_OrderedTraceWriter`.

    `start`, `event` and `finish` return at once; the parent's own methods do
    the writing, in call order, on the writer's task — the sequence counter
    and the finished flag advance there, in that same order. Engines that
    record through `core.tracing.event` reach this recorder through the
    context variable and are queued the same way.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pending: list = []
        self.writer = _OrderedTraceWriter(
            on_idle=self._write_pending,
            hold=lambda: not self._finished,
            window_s=_TRACE_COALESCE_S,
        )

    async def _persist(self, fn, *args, **kwargs) -> None:
        # Buffered: the writer commits the burst in `_write_pending`.
        self._pending.append((fn, args, kwargs))

    async def _write_pending(self) -> None:
        writes, self._pending = self._pending, []
        if not writes:
            return
        try:
            await db.run_in_thread(db.write_query_trace_batch, writes)
            return
        except Exception:  # noqa: BLE001 — retried one by one below
            logging.getLogger(__name__).warning(
                "query trace batch of %d writes failed trace_id=%s; retrying one by one",
                len(writes), self.trace_id, exc_info=True,
            )
        for fn, args, kwargs in writes:
            try:
                await db.run_in_thread(fn, *args, **kwargs)
            except Exception:  # noqa: BLE001 — best-effort, like every trace write
                logging.getLogger(__name__).warning(
                    "query trace write %s failed trace_id=%s", getattr(fn, "__name__", fn), self.trace_id,
                    exc_info=True,
                )

    async def start(self, **kwargs) -> None:  # type: ignore[override]
        self.writer.submit(lambda: TraceRecorder.start(self, **kwargs))

    async def event(self, stage: str, **kwargs) -> None:  # type: ignore[override]
        if "details" in kwargs:
            kwargs["details"] = _trace_snapshot(kwargs["details"])
        self.writer.submit(lambda: TraceRecorder.event(self, stage, **kwargs))

    def event_when_ready(self, stage: str, build, **kwargs) -> None:
        """Queue an event whose details are only complete later: `build` is
        awaited on the writer's task, in this event's place in the order."""

        async def job() -> None:
            await TraceRecorder.event(self, stage, details=_trace_snapshot(await build()), **kwargs)

        self.writer.submit(job)

    async def finish(self, status: str, **kwargs) -> None:  # type: ignore[override]
        if "meta" in kwargs:
            kwargs["meta"] = _trace_snapshot(kwargs["meta"])
        self.writer.submit(lambda: TraceRecorder.finish(self, status, **kwargs))

    async def flush(self) -> None:
        await self.writer.flush()


def _spawn_background_compaction(
    conv_key: str, history: list, *, base_url: str, model: str
) -> None:
    """Start a compaction that outlives this request's stream."""
    from . import compaction

    task = asyncio.create_task(
        compaction.maybe_background_compact(
            conv_key, history, base_url=base_url, model=model
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _record_usage_event(
    gen: LiveGeneration, timing: dict, workspace_id: str, effort: str
) -> None:
    """Persist one turn's telemetry (V18). Called from the generation's
    `finally`, so it runs for a completed answer, a cancelled one and a failed
    one alike — an error rate computed only from successes is not an error
    rate.

    Everything here is metadata that already exists at this point: the meta
    the engine emitted (route, model, mode), the wall clock this request
    measured, and the token counts the SERVING RUNTIME reported. Nothing is
    estimated; a count nobody made stays NULL.
    """
    from . import usage

    meta = gen.final_meta or {}
    tokens = llm.get_usage() or {}
    status = (
        usage.CANCELLED if gen.cancelled else usage.ERROR if gen.failed else usage.OK
    )
    ttft = timing.get("first_token")
    started = timing.get("started")
    from time import perf_counter as _pc

    duration = (_pc() - started) if started is not None else None
    # `context` is what the meter shows the user: the exact prompt size from
    # vLLM's /tokenize. It is kept beside the runtime's own prompt count
    # because they answer different questions (what we sent vs what the engine
    # billed, which differ whenever a prefix cache hits).
    context_meta = meta.get("context") if isinstance(meta.get("context"), dict) else {}
    # Availability CONTRACT §8.4: the per-attempt ledger. usage_events is one
    # row per generation_id (unique index) and a resume mints a new
    # generation_id, so it is one row per ATTEMPT — written by the server
    # alone, unlike messages.meta, which a viewer's whole-thread PUT replaces
    # with its own copy. That is where `engine`, `retry_reason` and
    # `terminal_state` are durable, with no new table or column.
    record = _attempt_record(gen, streamed=ttft is not None)
    from . import metrics as _metrics

    _metrics.inc(
        "chat_request_attempts_total",
        "Chat generation attempts by terminal state and the engine that served them.",
        terminal_state=record["terminal_state"],
        # The metric's `engine` vocabulary is the breaker's (CONTRACT §7.2):
        # the primary is "main" there, and in one-model mode (v2 §1) it is
        # the only engine an attempt can name.
        engine="main",
    )
    await usage.record_async(
        user_id=gen.user_id,
        workspace_id=workspace_id,
        conversation_id=gen.conversation_id,
        generation_id=gen.generation_id,
        route=str(meta.get("route") or ("chat" if status == usage.OK else "unknown")),
        effort=effort,
        model=str(meta.get("model") or ""),
        mode=str(meta.get("mode") or ""),
        input_tokens=tokens.get("prompt_tokens"),
        output_tokens=tokens.get("completion_tokens"),
        ttft_ms=None if ttft is None else int(ttft * 1000),
        duration_ms=None if duration is None else int(duration * 1000),
        status=status,
        error_kind=gen.error_code if gen.failed else "",
        meta={
            "model_calls": int(tokens.get("calls") or 0),
            "context_tokens": context_meta.get("tokens_used"),
            "context_window": context_meta.get("window"),
            # V29: which send intent and which attempt of it this turn was,
            # so a resumed request's attempts can be joined in analytics.
            # In `meta` rather than a column: the table's shape is settled.
            "intent_id": gen.intent_id,
            **record,
        },
    )


#: Why an attempt beyond the first exists (CONTRACT §8.4 `retry_reason`),
#: keyed by the status the intent's row had when the person asked again.
#: Bounded: these become a jsonb value and, one day, a label.
_RETRY_REASONS = {
    "interrupted": "lost_process",  # a restart / reboot marked it; nobody answered
    "accepted": "lost_process",  # the process that held it died without saying so
    "running": "lost_process",
    "queued": "recovery",  # held for the main model through a recovery (CONTRACT §8.3 v2)
    "failed": "failed",  # the previous attempt ended in an error record
    "cancelled": "cancelled",  # Stop, or replaced by a newer message
    "completed": "no_durable_answer",  # completed with nothing to replay
}


def _answer_was_interrupted(meta: Optional[dict]) -> bool:
    """True when the engine's own meta says the writing failed part-way: the
    chat route's continuation loop keeps what was written and returns
    `stop_reason: "error"` instead of raising (app/continuation.py), and
    the person is told so under the answer (frontend/lib/errors.ts)."""
    continuation = (meta or {}).get("continuation")
    return isinstance(continuation, dict) and continuation.get("stop_reason") == "error"


def _attempt_record(gen: LiveGeneration, *, streamed: bool) -> dict:
    """The four facts CONTRACT §8.4 requires of every attempt.

    `engine` is what the answer's own meta says served it — the stamp the
    chat worker's meta step puts there when another engine wrote the
    answer (§8.3) — and "primary" when nothing did (always, in one-model
    mode). Read from the meta, not from an engine module, so the ledger
    outlives any such module. `terminal_state` tells an attempt that died
    AFTER its first token ('interrupted': the partial is kept as a partial
    and nothing re-runs it by itself — whether the engine raised, or the
    continuation loop swallowed the error and returned the partial) from
    one that failed before any ('failed'), from a Stop ('cancelled'), and
    from an orderly shutdown ('interrupted', resumable by the next process
    — the same word _settle_chat_request writes on the row). `streamed` is
    whether a token had reached a viewer.
    """
    if gen.cancelled:
        # An acknowledged Stop stays a Stop, even during shutdown.
        stopped = gen.request_status == "cancelled"
        terminal = "interrupted" if _shutting_down and not stopped else "cancelled"
    elif getattr(gen, "parked", False) or getattr(gen, "lease_lost", False):
        # Parked for the resume sweep (CONTRACT §8.3 step 5), or taken over
        # by another resumer: the attempt did not finish, nothing was
        # answered, and the next attempt of the same intent carries
        # retry_reason "recovery".
        terminal = "interrupted"
    elif gen.failed:
        terminal = "interrupted" if streamed else "failed"
    elif _answer_was_interrupted(gen.final_meta):
        terminal = "interrupted"
    else:
        terminal = "completed"
    return {
        "attempt": int(gen.attempt),
        "engine": str((gen.final_meta or {}).get("engine") or "primary"),
        "retry_reason": gen.retry_reason or "none",
        "terminal_state": terminal,
    }


async def _finalize_generation(conv_key: str, gen: LiveGeneration) -> None:
    """Mark done, unregister, and persist the answer if nobody received it."""
    await gen.finish()
    if _live_generations.get(conv_key) is gen:
        _live_generations.pop(conv_key, None)
    if (
        not gen.cancelled
        and not gen.failed
        and gen.subscribers == 0
        and gen.user_id is not None
        and gen.conversation_id
        and gen.answer
        # V29: a generation started by /chat persists its answer BEFORE
        # `done` whether or not anyone is attached (_store_answer); this
        # path is now the fallback for a generation without an intent row.
        and not gen.persisted
    ):
        # This is the ONLY copy of the answer: nobody was attached when it
        # finished, so no client will persist it. A bare `suppress(Exception)`
        # here loses the user's reply without a trace — and gives no way to
        # tell "nothing to save" apart from "the save failed". Still
        # best-effort (a storage failure must not take the process down), but
        # audible.
        try:
            stored = await db.run_in_thread(
                db.add_message,
                gen.user_id,
                gen.conversation_id,
                "assistant",
                gen.answer,
                gen.final_meta,
            )
            if stored is None:
                logging.getLogger(__name__).warning(
                    "detached answer for conversation %s was not stored: no such "
                    "conversation for user %s",
                    gen.conversation_id,
                    gen.user_id,
                )
        except Exception as exc:  # noqa: BLE001 — best-effort, but never silent
            logging.getLogger(__name__).warning(
                "failed to persist the detached answer for conversation %s: %s: %s",
                gen.conversation_id,
                type(exc).__name__,
                exc,
            )
    elif gen.answer and gen.conversation_id and not gen.cancelled and not gen.failed:
        logging.getLogger(__name__).debug(
            "not persisting %s server-side: subscribers=%s user_id=%s",
            gen.conversation_id,
            gen.subscribers,
            gen.user_id,
        )


class ChatMessage(BaseModel):
    role: str
    content: str = ""


# Composer multi-upload cap (2026-08-05): base64 images ride in the JSON chat
# body, so five 10 MB uploads ≈ 67 MB of payload — a deliberate ceiling, not
# an arbitrary one.
MAX_IMAGES = 5


class ChatRequest(BaseModel):
    """Chat request per spec §8 + V2-DESIGN §1:
    {messages, session_id, image?, conversation_id?, mode?, model?, effort?, agent?}.

    The flat {message, image_base64} shape the Next.js proxy sends is also
    accepted; `message` wins when both are present.
    """

    # Every list here is `fail_fast` (2026-09-13): without it pydantic builds
    # one error per bad element — 32 MiB of `[{},…]` measured 11,184,811
    # errors and +12 GB before the 422. A test walks this model and fails on
    # a list field added without it.
    messages: Optional[List[ChatMessage]] = Field(default=None, fail_fast=True)
    message: Optional[str] = None
    session_id: str = "default"
    image: Optional[str] = None
    image_base64: Optional[str] = None
    # 2026-08-05: up to MAX_IMAGES images in one turn (composer multi-upload).
    # `image`/`image_base64` remain the single-image back-compat spelling.
    images: Optional[List[str]] = Field(default=None, fail_fast=True)
    # --- V2 optional fields (defaults preserve v1 behavior) ---
    conversation_id: Optional[str] = None
    mode: Literal["salesforce", "assistant"] = "salesforce"
    # The composer's "Live Salesforce" toggle: answer straight from the org
    # (any object/field this integration user can read) instead of the synced
    # copy. Only meaningful in salesforce mode; ignored elsewhere.
    sf_live: bool = False
    model: Literal["smart", "fast"] = "smart"
    effort: Literal[
        "fast", "think", "max", "low", "medium", "high", "extra_high"
    ] = "think"
    agent: bool = False
    # Deep Research (2026-08-30): the iterative mode — plan, search, read,
    # find the gaps, search again, then write a cited report. Explicit only:
    # it costs minutes and the whole search budget, so nothing infers it.
    deep_research: bool = False
    # V8: an uploaded PDF (base64, optionally a data: URL) + its filename.
    pdf: Optional[str] = None
    pdf_filename: Optional[str] = None
    # 2026-09-02: LARGE documents stream to /uploads (purpose=document) first
    # and the chat request carries REFERENCES — 512 MB of base64 through a
    # JSON body would kill the browser tab and both servers. Up to five per
    # message: [{"upload_id": "<32 hex>", "name": "contract.pdf"}, ...].
    # Small documents may still ride inline in `pdf` exactly as before.
    pdf_uploads: Optional[List[dict]] = Field(default=None, fail_fast=True)
    # 2026-09-09: videos ALWAYS stream to /uploads (purpose=video) first —
    # nothing that size rides a JSON body — and the request carries
    # references: [{"upload_id": "<32 hex>", "name": "standup.mp4"}, ...].
    # The analysis is a detached job started at upload time; the chat turn
    # attaches to it (engines/video.py).
    video_uploads: Optional[List[dict]] = Field(default=None, fail_fast=True)
    # Phase 1: web search — "off" (never), "on" (force), "auto" (model decides).
    web_search: Literal["off", "auto", "on"] = "off"
    # Salesforce Intelligence Mode: the answer to a clarifying question this
    # conversation is waiting on. Present → the ORIGINAL request resumes with
    # this answer folded in, instead of `message` being treated as a new one.
    # Absent → an ordinary send, which may still be READ as an answer when a
    # question is pending (engines/sf_intel.py decides, not the client).
    clarification: Optional[dict] = None
    # V29 (2026-09-10): the send intent — minted by the browser when Send is
    # pressed and re-sent on every retry of that message, so the server can
    # attach to, replay or resume the generation it already has instead of
    # starting another (docs/upload-reliability/API.md). Absent from old
    # clients: the server mints one and keeps the event shapes they expect.
    intent_id: Optional[str] = None
    # 2026-09-13 (the duplicate answers): {"self": "b-…", "parent": "b-…"|"#N"}
    # — where the answer belongs in the browser's conversation tree. Since V29
    # the SERVER stores the answer (before `done`), so a position the browser
    # only held locally was lost: an untagged row attaches to whatever row
    # precedes it, which for "Try again" is the previous answer — stacked
    # copies instead of versions. Written onto the stored answer's
    # `meta.branch`; kept in the request snapshot, so a resume files its answer
    # in the same place. Absent = today's behaviour, byte for byte.
    answer_branch: Optional[dict] = None
    # Supplied only by the offline evaluation runner. The application receives
    # the stable case identifier, never the expected plan, query or answer.
    test_case_id: Optional[str] = None
    # --- AS3 intent-capability BEGIN ---
    # The artifact the UI's "Edit with a prompt" box names. Ownership is
    # checked against this conversation's published artifacts before the
    # intent gate uses it; an id that is not the viewer's is ignored.
    artifact_id: Optional[str] = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    # --- AS3 intent-capability END ---

    @field_validator("test_case_id")
    @classmethod
    def _valid_test_case_id(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _TEST_CASE_ID_RE.fullmatch(value):
            raise ValueError("test_case_id must be 1-128 characters of [A-Za-z0-9_.-]")
        return value

    @field_validator("intent_id")
    @classmethod
    def _valid_intent_id(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _INTENT_ID_RE.fullmatch(value):
            raise ValueError("intent_id must be 1-64 characters of [A-Za-z0-9_-]")
        return value

    @field_validator("answer_branch")
    @classmethod
    def _valid_answer_branch(cls, value: Optional[dict]) -> Optional[dict]:
        if value is None:
            return None
        if set(value) - {"self", "parent"}:
            raise ValueError("answer_branch may carry only self and parent")
        branch_self = value.get("self")
        parent = value.get("parent")
        if not isinstance(branch_self, str) or not _ANSWER_BRANCH_SELF_RE.fullmatch(branch_self):
            raise ValueError("answer_branch.self must be b- and 1-64 characters of [A-Za-z0-9_-]")
        if parent is not None and (
            not isinstance(parent, str) or not _ANSWER_BRANCH_PARENT_RE.fullmatch(parent)
        ):
            raise ValueError("answer_branch.parent must be a branch id or #<index>")
        return {"self": branch_self, **({"parent": parent} if parent is not None else {})}

    @field_validator("session_id")
    @classmethod
    def _valid_session_id(cls, value: str) -> str:
        return _checked_session_id(value)

    @field_validator("conversation_id")
    @classmethod
    def _conversation_id_is_not_synthetic(cls, value: Optional[str]) -> Optional[str]:
        return _reject_synthetic_conversation_id(value)

    @property
    def pdf_data(self) -> Optional[str]:
        return self.pdf

    @property
    def text(self) -> str:
        if self.message and self.message.strip():
            return self.message.strip()
        for m in reversed(self.messages or []):
            if m.role == "user" and m.content.strip():
                return m.content.strip()
        return ""

    @property
    def images_data(self) -> List[str]:
        """Every attached image, list form — `images` wins over the single
        back-compat fields, which become a one-element list."""
        imgs = [i for i in (self.images or []) if i and i.strip()]
        if imgs:
            return imgs
        single = self.image_base64 or self.image
        return [single] if single else []

    @property
    def image_data(self) -> Optional[str]:
        """First image or None — the truthiness gate the routing checks use."""
        data = self.images_data
        return data[0] if data else None

    @property
    def history_messages(self) -> List[dict]:
        """Prior turns of THIS conversation, from the messages the frontend
        sends — the authoritative within-chat memory (survives restarts, unlike
        the in-process dict). The trailing user turn is the current `text`, so
        it is dropped here."""
        out = [
            {"role": m.role, "content": m.content}
            for m in (self.messages or [])
            if m.content and m.content.strip()
        ]
        if out and out[-1]["role"] == "user":
            out.pop()
        return out

    @model_validator(mode="after")
    def _canonical_effort(self) -> "ChatRequest":
        # Legacy wire values (low/medium/high/extra_high) normalize to the
        # 3-level ladder HERE, once — every engine and the trust metadata
        # (meta.effort) see only fast|think|max.
        self.effort = llm.normalize_effort(self.effort)
        return self

    @model_validator(mode="after")
    def _require_input(self) -> "ChatRequest":
        if len(self.images_data) > MAX_IMAGES:
            raise ValueError(f"at most {MAX_IMAGES} images per message")
        if (
            not self.text
            and not self.image_data
            and not self.pdf_data
            and not self.video_uploads
            # Answering a clarification by clicking "Skip" carries no text of
            # its own; the request it resumes is what supplies the question.
            and not self.clarification
        ):
            raise ValueError(
                "provide a non-empty message/messages, an image, a PDF or a video"
            )
        return self



#: How a message may reference streamed documents: at most five. One of them
#: may be an ARCHIVE, whose members then count against the engine's own,
#: larger cap — five zips of twelve files each is a report, not a question.
_MAX_DOC_REFS = 5

#: Archive members read as documents / attached as images. Extensions the
#: expander trusts as text-bearing; everything else is sniffed, and true
#: binaries become one honest manifest line instead of prompt mojibake.
_TEXTY_EXTS = (
    ".pdf", ".docx", ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv",
    ".json", ".yaml", ".yml", ".toml", ".xml", ".html", ".htm", ".css",
    ".js", ".ts", ".tsx", ".jsx", ".py", ".java", ".c", ".h", ".cpp",
    ".go", ".rs", ".rb", ".php", ".sh", ".sql", ".ini", ".cfg", ".log",
)
_IMAGE_EXTS = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}
_MAX_ARCHIVE_IMAGES = 4
_MAX_ARCHIVE_IMAGE_BYTES = 10 * 1024 * 1024


def _expand_archive(root: str, path: str, name: str) -> tuple[list, list, str]:
    """Open an uploaded archive the way ChatGPT would: every member listed,
    text-bearing members read as documents, images attached as images.

    → (docs, image data-URLs, manifest text). Extraction reuses the SAME
    hardened extractor the dataset rail trusts — zip-bomb budgets, member
    caps, traversal guards, nested archives listed but never opened — and is
    cached in the upload's `extracted/` directory so a follow-up question
    does not pay for it twice.
    """
    import base64 as _b64

    from .core import archive
    from .engines.document import MAX_DOCS

    extract_dir = os.path.join(root, "extracted")
    skipped: list[tuple[str, str]] = []
    if not os.path.isdir(extract_dir):
        plan = archive.extract(path, extract_dir)
        if plan is not None:
            skipped = list(plan.skipped) + [
                (n, "nested archive — listed, not opened")
                for n in plan.nested_archives
            ]

    members: list[tuple[str, str, int]] = []  # (relname, abspath, size)
    for dirpath, _dirs, files in os.walk(extract_dir):
        for fname in sorted(files):
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, extract_dir)
            members.append((rel, full, os.path.getsize(full)))
    members.sort()

    docs: list = []
    images: list = []
    lines = [f"Archive {name} contains {len(members)} file(s):"]
    for rel, full, size in members:
        ext = os.path.splitext(rel)[1].lower()
        if ext in _IMAGE_EXTS and size <= _MAX_ARCHIVE_IMAGE_BYTES and \
                len(images) < _MAX_ARCHIVE_IMAGES:
            with open(full, "rb") as fh:
                images.append(
                    f"data:{_IMAGE_EXTS[ext]};base64,"
                    + _b64.b64encode(fh.read()).decode("ascii")
                )
            lines.append(f"  - {rel} ({size:,} bytes) — attached as an image")
        elif (ext in _TEXTY_EXTS or ext in (".docx",)) and len(docs) < MAX_DOCS - 1:
            with open(full, "rb") as fh:
                docs.append(
                    (f"{name}/{rel}", _b64.b64encode(fh.read()).decode("ascii"))
                )
            lines.append(f"  - {rel} ({size:,} bytes) — read in full")
        else:
            lines.append(f"  - {rel} ({size:,} bytes) — listed only")
    for member, why in skipped[:20]:
        lines.append(f"  - {member} — skipped: {why}")
    return docs, images, "\n".join(lines)


async def _resolve_document_refs(
    request: "ChatRequest", conversation_id: Optional[str]
) -> tuple[list, list, Optional[str]]:
    """The message's documents. → (docs as (name, base64), images, error).

    References resolve against THIS conversation's upload workspace, so a
    forged upload_id from another conversation is a 404-shaped miss, not a
    read. The stored filename wins over whatever the client claims — the
    bytes on disk are the truth. A swept (TTL) or unknown reference produces
    one clear sentence instead of a stack trace mid-stream. An archive
    reference is expanded: members become documents and images, and a
    manifest of everything inside rides along as its own document.
    """
    import base64 as _b64

    from .core import archive
    from .uploads import upload_root

    refs = list(request.pdf_uploads or [])
    if len(refs) > _MAX_DOC_REFS:
        return [], [], f"A message can carry at most {_MAX_DOC_REFS} documents."
    docs: list = []
    images: list = []
    for ref in refs:
        upload_id = str((ref or {}).get("upload_id", ""))
        if not _re.fullmatch(r"[0-9a-f]{32}", upload_id) or not conversation_id:
            return [], [], "One of the attached documents could not be found — please re-attach it."
        root = upload_root(conversation_id, upload_id)
        original = os.path.join(root, "_original")
        try:
            files = [e for e in os.scandir(original) if e.is_file()]
        except OSError:
            files = []
        if len(files) != 1:
            name = str((ref or {}).get("name") or "an attached document")
            return [], [], (
                f"{name} is no longer available on the server "
                "(uploads are swept after their TTL) — please re-attach it."
            )
        entry = files[0]
        lower = entry.name.lower()
        is_archive = lower.endswith((".zip", ".tar", ".tar.gz", ".tgz")) or \
            archive.is_zip_container(entry.path)
        if is_archive and not lower.endswith((".docx", ".xlsx")):
            # .docx/.xlsx ARE zip containers; sniffing alone would unzip a
            # Word file into its XML skeleton. The extension decides those.
            try:
                more_docs, more_images, manifest = await asyncio.to_thread(
                    _expand_archive, root, entry.path, entry.name
                )
            except archive.ArchiveError as exc:
                return [], [], f"{entry.name} could not be opened: {exc}"
            docs.append(
                (
                    f"{entry.name} (archive contents)",
                    _b64.b64encode(manifest.encode("utf-8")).decode("ascii"),
                )
            )
            docs.extend(more_docs)
            images.extend(more_images[: _MAX_ARCHIVE_IMAGES - len(images)])
        else:
            # 2026-09-03: the upload-time prewarm may already have extracted
            # this document; when its cache can serve this answer (render
            # policy satisfied), the engine skips extraction entirely.
            cached = None
            try:
                from .engines.document import load_document_cache

                cached = await asyncio.to_thread(
                    load_document_cache,
                    root,
                    entry.name,
                    effort=request.effort,
                    question=request.text or "",
                )
            except Exception:  # noqa: BLE001 — the cache is an accelerator
                cached = None
            if cached is not None:
                docs.append(cached)
                continue
            with open(entry.path, "rb") as fh:
                raw = fh.read()
            docs.append((entry.name, _b64.b64encode(raw).decode("ascii")))
    if request.pdf_data:
        docs.append((request.pdf_filename, request.pdf_data))
    return docs, images, None


def _asks_about_an_attachment(
    text: str,
    request: "ChatRequest",
    video_followup: bool,
    image_followup: bool = False,
) -> bool:
    """Does this turn ask what an ATTACHED file says?

    The honest-visual refusal stands aside for exactly this turn: a map in a
    photo, a PDF or a video is something to READ, and answering "I can't
    draw a map" left the file unopened (verifier, 2026-09-16).

    It stands aside for nothing else. Skipping the refusal for every turn
    that merely CARRIES a file sent "plot these records on a map" with the
    table attached as a PDF to the document engine, which draws nothing and
    answers in prose — the 2026-09-16 incident itself (verifier recheck).
    `visuals.asks_about_attachment_content` reads the words; this function
    only adds the requirement that there be a file to read.
    """
    if not (
        request.image_data
        or request.pdf_uploads
        or request.pdf_data
        or video_followup
        # 2026-09-18: an image the conversation already holds is a file to
        # READ for exactly the same reason a video is — "what does the map
        # in that photo show?" must not be answered "I can't draw a map"
        # one turn after the photo was read fine.
        or image_followup
    ):
        return False
    from .artifacts import visuals as _t3_visuals

    return _t3_visuals.asks_about_attachment_content(text)


_MAX_VIDEO_REFS = 3


async def _resolve_video_refs(
    request: "ChatRequest", conversation_id: Optional[str], known: list
) -> tuple[list, Optional[str]]:
    """The message's videos → (analysis rows, error).

    References resolve against THIS conversation's attachments, so an id
    from another conversation is a miss, not a read. A follow-up with no
    references answers over every video attached to the conversation.
    """
    refs = list(request.video_uploads or [])
    if len(refs) > _MAX_VIDEO_REFS:
        return [], f"A message can carry at most {_MAX_VIDEO_REFS} videos."
    if not refs:
        return list(known), None
    rows = []
    for ref in refs:
        upload_id = str((ref or {}).get("upload_id", ""))
        if not _re.fullmatch(r"[0-9a-f]{32}", upload_id) or not conversation_id:
            return [], "One of the attached videos could not be found — please re-attach it."
        row = await db.run_in_thread(db.get_video_by_upload, conversation_id, upload_id)
        if row is None:
            name = str((ref or {}).get("name") or "an attached video")
            return [], f"{name} is not attached to this conversation — please re-attach it."
        rows.append(row)
    return rows, None


@app.get("/health")
async def health() -> dict:
    """§8: /health checks the model servers and DuckDB — under the all-vLLM
    override, the four vLLM services plus the warehouse. Overall status is
    "degraded" (never a static ok) when any dependency check fails, with
    per-dependency detail in `checks`."""
    report = await check_dependencies()
    return {
        "status": report["status"],
        "service": "orchestrator",
        "version": app.version,
        "checks": report["checks"],
        # Additive (2026-08-11): the VERIFIED effective context length, the
        # request budget derived from it, and the serving flags the app
        # believes are set. `status` is untouched — a window mismatch is a
        # configuration fact to surface, not a dependency outage.
        "context": report.get("context", {}),
        # Additive (2026-09-06): the public-web vector index — rows, distinct
        # pages, embedding model, chunker version and BOTH indexing backlogs.
        # `check_dependencies` has always computed this and `/health` dropped
        # it on the floor, so `health._check_web_index`'s promise that "a
        # chunker bump is visible here first" was never true from outside the
        # process. `status` is untouched: a stale index is a degraded answer,
        # not an outage, and the container healthcheck gates on `status`.
        "web_index": report.get("web_index", {}),
        # Additive (2026-09-11): what the orchestrator has IN FLIGHT — live
        # generations, the video queue, open upload sessions, and requests a
        # restart left interrupted. `check_dependencies` computes it and this
        # route dropped it on the floor, exactly as it did with `web_index`
        # above, so an operator could not tell "busy" from "stalled" from
        # outside the process. `status` is untouched: being busy is not an
        # outage, and the container healthcheck gates on `status`.
        "work": report.get("work", {}),
        # Artifact Studio (2026-09-11): renderer availability and whether
        # the reports volume takes writes — computed by check_dependencies
        # like `work` above, and forwarded for the same reason.
        "artifacts": report.get("artifacts", {}),
        # Additive (2026-09-13, no-timeout /v1): the open-file limit this
        # process actually runs with and how close it is to the /v1 guard.
        # In-memory and cached; `status` is untouched.
        "resources": _resources_report(),
    }


def _resources_report() -> dict:
    try:
        from . import resources as _resources

        return _resources.describe()
    except Exception:  # noqa: BLE001 — /health must answer
        return {}


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus exposition for the living knowledge layer.

    PUBLIC, like /health: Prometheus scrapes it unauthenticated from inside the
    Docker network, and adding a session to that path would mean storing
    credentials in the monitoring stack. It exposes only counters and
    histograms — no query text, no URLs, no user ids (see app/metrics.py, where
    every label is drawn from a closed set).
    """
    from fastapi.responses import PlainTextResponse

    from . import metrics as metrics_mod

    try:
        counts = await db.run_in_thread(db.web_corpus_counts)
        metrics_mod.corpus_gauges(
            counts.get("pages", 0), counts.get("pending", 0), counts.get("due", 0)
        )
    except Exception:  # noqa: BLE001 — serve what we have
        pass
    return PlainTextResponse(
        metrics_mod.render(), media_type="text/plain; version=0.0.4; charset=utf-8"
    )


@app.get("/reports")
async def reports_index(user: UserRow = Depends(require_user)) -> dict:
    """The CALLER's generated files. The reports directory is one flat disk
    namespace shared by everyone; the report_files table is what says whose
    is whose, so the listing is a DB query, not a directory walk."""
    from .authn import store as authn_store

    rows = await db.run_in_thread(authn_store.list_report_files, int(user["id"]))
    on_disk = {r["filename"] for r in list_reports(settings.reports_dir)}
    return {
        "reports": [
            {
                "filename": r["filename"],
                "conversation_id": r["conversation_id"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
            if r["filename"] in on_disk
        ]
    }


@app.get("/reports/{filename}")
async def get_report(
    filename: str, user: UserRow = Depends(require_user)
) -> FileResponse:
    """Download one generated file — the OWNER's only. A filename someone
    else generated 404s identically to one that never existed: the flat
    namespace must not be a cross-user oracle, let alone a download."""
    from .authn import store as authn_store

    try:
        path = resolve_report_file(settings.reports_dir, filename)
    except ReportPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    owner = await db.run_in_thread(authn_store.report_owner, filename)
    if owner != int(user["id"]) or not path.is_file():
        raise HTTPException(status_code=404, detail="report not found")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(path, filename=filename, media_type=media_type)


# ---------------------------------------------------------------------------
# V29 durable send intents (docs/upload-reliability/API.md, CONTRACT.md).
#
# A generation is an in-process object; the REQUEST that started it is a
# `chat_requests` row keyed by the browser's intent_id. The helpers below are
# what /chat, /chat/attach, /chat/stop and /chat/requests share: the snapshot
# a resume runs from, the registry lookup by generation_id, the durable
# answer under a generation_id, and the row's status transitions.
# ---------------------------------------------------------------------------

#: Row states a resume may act on: 'interrupted' is what startup/shutdown
#: write; 'accepted'/'running' with no live generation means the process
#: lost it without getting to say so (a crash, a kill); 'queued' (V33) is
#: a request parked for the main model by a process that waited out
#: LLM_QUEUE_MAX_WAIT_S or died holding it (app/continuity.py).
_RESUMABLE_STATUSES = ("interrupted", "accepted", "running", "queued")

#: Request fields that carry inline bytes. They are dropped from the stored
#: snapshot: base64 images and PDFs belong on the upload rails, not in a
#: jsonb column, and a turn that depended on them is marked not resumable.
_INLINE_BYTE_FIELDS = ("image", "image_base64", "images", "pdf")


def _request_snapshot(request: "ChatRequest") -> tuple[dict, bool]:
    """The request as a resume would run it, and whether a resume CAN run it.

    Upload references (`pdf_uploads`, `video_uploads`) are kept — their
    bytes live on disk under ids that survive a restart. Inline bytes are
    not kept, so a turn that carried them can only be retried from the
    browser, which sends them again (see the known-intent path in /chat).
    """
    body = request.model_dump(exclude_none=True)
    had_inline = bool(request.images_data or request.pdf_data)
    for name in _INLINE_BYTE_FIELDS:
        body.pop(name, None)
    return body, not had_inline


#: Generations between their compare-and-swap on the request row and their
#: registration in `_live_generations` (see the known-intent path of /chat):
#: generation_id → (generation, claimed_at), so a concurrent resumer of the
#: same intent attaches instead of starting an attempt of its own. A claim
#: is milliseconds long; one left behind by an unexpected exit in between
#: is ignored after _CLAIM_TTL_S rather than trusted forever.
_resuming: dict = {}
_CLAIM_TTL_S = 30.0


def _live_generation_for(generation_id: str) -> Optional["LiveGeneration"]:
    """The registry entry running this generation, if it is still going.
    The registry is keyed by conversation; a request row names a generation,
    so this is the join between the two."""
    for candidate in list(_live_generations.values()):
        if candidate.generation_id == generation_id and not candidate.done:
            return candidate
    claim = _resuming.get(generation_id)
    if claim is not None:
        claimed, at = claim
        if not claimed.done and _time_monotonic() - at <= _CLAIM_TTL_S:
            return claimed
        _resuming.pop(generation_id, None)
    return None


def _time_monotonic() -> float:
    from time import monotonic

    return monotonic()


def _persisted_answer(conversation_id: str, generation_id: str) -> Optional[dict]:
    """The assistant message stored for this generation, or None (thread)."""
    row = db.get_message_by_generation(conversation_id, generation_id)
    if row is None or row.get("role") != "assistant":
        return None
    return {"content": row["content"], "meta": row["meta"]}


def _interrupted_after_tokens(conversation_id: str, generation_id: str) -> bool:
    """Did this interrupted attempt reach a viewer with a token (thread)?
    The ledger's ttft (an orderly shutdown wrote it) or the viewer's kept
    partial (all a crash leaves) — CONTRACT §8.4 says such an attempt is
    never re-run by itself."""
    from . import continuity as _continuity_mod

    if db.generation_streamed(generation_id):
        return True
    return _continuity_mod._is_partial(_persisted_answer(conversation_id, generation_id))


def _is_answer(stored: Optional[dict]) -> bool:
    """A stored row is an ANSWER unless it is the failure record of a failed
    attempt (`meta.error`, see _store_failure) — which must neither be
    replayed as one nor reported as one."""
    return stored is not None and "error" not in (stored.get("meta") or {})


def _overwrite_persisted_answer(
    conversation_id: str, generation_id: str, content: str, meta: Optional[dict]
) -> None:
    """Replace the text and meta of the message stored under this generation.

    Reached only when the server's persist deduplicated against a row a
    viewer stored first — a PARTIAL answer a tab persisted when it lost its
    stream. The finished answer wins (db.add_message replace_existing).
    """
    with db.connection() as con:
        con.execute(
            "UPDATE messages SET content = %s, meta = %s "
            "WHERE conversation_id = %s AND generation_id = %s AND role = 'assistant'",
            (content, db._json_param(meta), conversation_id, generation_id),
        )


#: What a follower is told when its generation was cancelled because a newer
#: message arrived for the conversation (ORCH-02). `code` is what the client
#: keys on; the sentence is what a person reads.
_REPLACED_SENTENCE = "This answer was replaced by a newer message."
#: A queued request another process resumed under its own lease
#: (continuity.LeaseLost): the same `replaced` code, so the client does
#: not paint it red, and a sentence that says where the answer went.
_TAKEN_OVER_SENTENCE = "This request was resumed elsewhere; its answer will appear in the conversation."
#: A parked turn whose row nothing can resume by itself (inline bytes the
#: server does not keep): truthful, and a Retry the browser can make.
_NOT_RESUMABLE_SENTENCE = (
    "The main model is still recovering, and this request carried files the server "
    "cannot re-send by itself — please try again once the model is back."
)


def _conversation_is_live(conv_key: str) -> bool:
    """Is a generation running for this conversation in this process?"""
    live = _live_generations.get(conv_key)
    return live is not None and not live.done


def _failure_sentence(exc: BaseException) -> tuple[str, str]:
    """(sentence, code) for a generation that died on an exception — written
    for a person, in the client's ErrorCategory vocabulary
    (frontend/lib/errorTypes.ts). Never the exception's own text: that can
    carry an internal hostname or an upstream body, and whatever goes on the
    wire here is persisted to history and exported. The raw exception goes
    to the server log, where an engineer looks (ORCH-01)."""
    import httpx

    from . import resilience

    # The OpenAI SDK wraps httpx's transport errors in its own hierarchy
    # (openai.APIConnectionError is NOT an httpx.TransportError, verified in
    # SDK 2.36 and 3.7), so until 2026-09-11 a 13-minute engine reload was
    # reported to the person as APPLICATION_ERROR. resilience.py classifies
    # the SDK shapes; ModelUnavailable is the wrapper giving up after its
    # window, which is the same sentence: the engine was not there.
    unavailable = (
        "The model is temporarily unavailable. It may still be starting up — "
        "please try again in a moment.",
        "MODEL_UNAVAILABLE",
    )
    if isinstance(exc, resilience.ModelUnavailable):
        return unavailable
    from . import admission

    if isinstance(exc, admission.AdmissionRejected):
        # The engine is up; its lane did not free in time, or the line was
        # already too deep to join (CONTRACT §6.7) — said as what it is.
        if exc.reason == "capacity":
            return "The model's queue is full right now. Please try again in a moment.", "TIMEOUT"
        return "The model is busy and could not start your request in time. Please try again.", "TIMEOUT"
    if resilience.is_read_timeout(exc) or isinstance(
        exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)
    ):
        return "The model did not answer in time. Please try again.", "TIMEOUT"
    if isinstance(exc, (httpx.TransportError, ConnectionError)) or resilience.is_recoverable(exc):
        return unavailable
    return "The answer could not be completed. Please try again.", "APPLICATION_ERROR"


def _discard_failure_record(conversation_id: str, generation_id: str) -> None:
    """Remove the failure record a previous attempt stored under this
    generation (an assistant row whose meta carries `error`) — and ONLY
    that: a real answer never has `meta.error`. A new attempt supersedes
    the record, and leaving it would make the thread one row longer than
    the viewer's copy, so its whole-thread PUT would be refused as a shrink."""
    db.delete_failure_record(conversation_id, generation_id)


def _retry_chat_request(
    intent_id: str, generation_id: str, *, expected_generation_id: Optional[str] = None
) -> Optional[dict]:
    """A new attempt of a known intent under `generation_id` (in a thread).

    db.resume_chat_request moves only an OPEN row (interrupted, or
    accepted/running after a lost process). A finished one — failed,
    cancelled, or completed with nothing durable to replay — is reopened
    first: the person asked again, and one row per intent is the invariant,
    so that same row carries the new attempt. A failed attempt's persisted
    failure record is discarded with it. `expected_generation_id` is the
    generation the CALLER saw on the row when it decided to retry: a row
    another resumer moved since then is left as it is (the caller attaches
    to that resumer's generation), never moved a second time.
    """
    row = db.get_chat_request(intent_id)
    if row is None:
        return None
    if expected_generation_id is not None and row["generation_id"] != expected_generation_id:
        return row  # somebody else's attempt is already the row's
    # One conditional step: the row moves only from the generation this
    # caller saw, so two callers racing to retry the same intent cannot both
    # start an attempt — the loser reads the row unchanged and attaches to
    # the winner's generation.
    resumed = db.resume_chat_request(
        intent_id,
        generation_id,
        reopen_finished=row["status"] in ("completed", "failed", "cancelled"),
        expected_generation_id=row["generation_id"],
    )
    if row["status"] in ("failed", "interrupted") and resumed is not None and resumed["generation_id"] == generation_id:
        # A person asked again: the new attempt supersedes the previous
        # attempt's record — the failure sentence, or the partial a tab
        # kept when the attempt was interrupted after its first token
        # (round-2 review, continuity.py:425: never a second answer beside
        # a kept partial). The automatic paths (the sweep, /chat/attach)
        # never re-run such an attempt; only this door does.
        _discard_failure_record(row["conversation_id"], row["generation_id"])
    return resumed


async def _store_failure(gen: "LiveGeneration", partial: str, *, resumable: bool) -> None:
    """Persist what a failed generation leaves behind: whatever streamed
    before it died, and `meta.error` in the client's PersistedError shape
    (frontend/lib/types.ts) — so a reload shows the failure and a Retry,
    not a bare turn with no answer and no reason (ORCH-01). Same dedupe
    and partial-overwrite rules as the answer itself."""
    if gen.user_id is None or not gen.conversation_id:
        return
    meta = {
        "generation_id": gen.generation_id,
        "intent_id": gen.intent_id,
        "attempt": gen.attempt,
        # Under the same tree position as the answer a retry will store, so
        # the browser reads the record as that answer's failed attempt, not
        # as a version of its own (frontend/lib/branching.ts buildTree).
        **({"branch": dict(gen.answer_branch)} if gen.answer_branch else {}),
        "error": {
            "message": gen.error,
            "code": gen.error_code,
            "status": None,
            "resumable": bool(resumable),
        },
    }
    try:
        stored = await db.run_in_thread(
            db.add_message, gen.user_id, gen.conversation_id, "assistant", partial, meta
        )
        if stored is not None and stored.get("deduplicated"):
            await db.run_in_thread(
                _overwrite_persisted_answer, gen.conversation_id, gen.generation_id, partial, meta
            )
            _invalidate_cross_chat_recall(gen.user_id)  # an in-place content edit
    except Exception as exc:  # noqa: BLE001 — best-effort, but never silent
        logging.getLogger(__name__).warning(
            "failed to persist the failure record for conversation %s: %s: %s",
            gen.conversation_id,
            type(exc).__name__,
            exc,
        )


async def _mark_chat_request(gen: "LiveGeneration", status: str, *, error: str = "") -> None:
    """Write one status transition to the row. Best-effort and audible: the
    row is bookkeeping about the answer, never a reason to lose it."""
    if not gen.intent_id:
        return
    try:
        await db.run_in_thread(db.set_chat_request_status, gen.intent_id, status, error=error)
        gen.request_status = status
    except Exception as exc:  # noqa: BLE001 — the answer must not depend on the row
        logging.getLogger(__name__).log(
            logging.DEBUG if _shutting_down else logging.WARNING,
            "chat request %s: could not record status %s: %s: %s",
            gen.intent_id,
            status,
            type(exc).__name__,
            exc,
        )


def _previous_send_status(conversation_id: str, intent_id: str) -> Optional[str]:
    """The durable status of the send before this one in the conversation,
    or None when there is none (a first message, or history from before V29)."""
    with db.connection() as con:
        row = con.execute(
            "SELECT status FROM chat_requests WHERE conversation_id = %s AND intent_id <> %s "
            "ORDER BY created_at DESC LIMIT 1",
            (conversation_id, intent_id),
        ).fetchone()
    return str(row["status"]) if row is not None else None


async def _previous_send_unfinished(conversation_id: str, intent_id: str) -> bool:
    """For the small-talk lane: did the previous send end without a completed
    answer (failed, stopped, interrupted, still queued or running)? A failed
    lookup counts as unfinished — the lane then just takes the full path."""
    try:
        status = await db.run_in_thread(_previous_send_status, conversation_id, intent_id)
    except Exception:  # noqa: BLE001 — the full path is always a safe answer
        return True
    return status is not None and status != "completed"


async def _settle_chat_request(gen: "LiveGeneration") -> None:
    """The row's terminal status, from the worker's `finally` — so it runs
    for a completed answer, a cancelled one and a failed one alike."""
    if not gen.intent_id:
        return
    if getattr(gen, "lease_lost", False):
        return  # the row belongs to another resumer's generation now
    if gen.cancelled and gen.request_status == "cancelled":
        return  # an acknowledged Stop stays a Stop, even during shutdown
    if getattr(gen, "parked", False) and not gen.cancelled:
        return  # parked for the resume sweep: the row stays `queued` (CONTRACT §8.3 step 5)
    if gen.cancelled and _shutting_down and gen.request_status == "queued":
        # A hold torn down by an orderly shutdown: the row already says the
        # truth about itself (`queued`, waiting for the main model) and the
        # next process's sweep reads it by name; rewriting it `interrupted`
        # would only bind it to LLM_RESUME_MAX_AGE_S (round-2 review,
        # continuity.py:486).
        return
    if gen.cancelled:
        status = "interrupted" if _shutting_down else "cancelled"
    elif gen.failed:
        status = "failed"
    else:
        status = "completed"
    if gen.request_status == status:
        return  # the success path (or /chat/stop) already wrote it
    error = ""
    if status == "failed":
        error = gen.error
    elif status == "cancelled" and gen.replaced:
        error = "replaced by a newer message"
    was_queued = gen.request_status == "queued"
    await _mark_chat_request(gen, status, error=error)
    if was_queued:
        # A Stop while queued leaves the `queued` count behind: the in-process
        # half dropped with the hold, the durable half only re-counts on the
        # next /health snapshot. Re-count now so llm_queued_generations tells
        # the truth at once (drill 15, 2026-09-12).
        from . import continuity

        await continuity._refresh_durable_queued()


async def _store_answer(gen: "LiveGeneration") -> None:
    """Make the answer durable under its generation_id, whoever is attached.

    Before V29 the server persisted only when NOBODY was attached and left
    the rest to the viewer — and a viewer is a browser tab, which closes,
    reloads and loses its network (RC-2). So the server persists ALWAYS,
    before `done` goes out: when the browser sees the terminal event the
    answer is already in history. No duplicate can follow: db.add_message
    dedupes on the (conversation, generation_id) unique index, and the
    viewer's whole-thread PUT (db.replace_messages) carries this
    generation_id exactly once.
    """
    if not gen.answer or gen.user_id is None or not gen.conversation_id:
        return
    meta = dict(gen.final_meta or {})
    meta.setdefault("generation_id", gen.generation_id)
    if gen.answer_branch:
        # The request said where this answer belongs in the conversation tree
        # (a "Try again", an edit): store it THERE. Without it the row attaches
        # to whatever precedes it — the previous answer, for a regenerate.
        meta["branch"] = dict(gen.answer_branch)
    log = logging.getLogger(__name__)
    try:
        stored = await db.run_in_thread(
            db.add_message, gen.user_id, gen.conversation_id, "assistant", gen.answer, meta
        )
        if stored is None:
            log.warning(
                "answer for conversation %s was not stored: no such conversation for user %s",
                gen.conversation_id,
                gen.user_id,
            )
            return
        if stored.get("deduplicated"):
            # A row under this generation_id already existed — which can only
            # be a viewer's mid-stream copy (the server persists before
            # `done`, so no client has finalized yet). Its text may be
            # truncated and its meta is the leading one plus whatever the
            # tab folded in; the finished answer with the engine's meta
            # (route, sources, report files) is the durable copy.
            await db.run_in_thread(
                _overwrite_persisted_answer,
                gen.conversation_id,
                gen.generation_id,
                gen.answer,
                meta,
            )
            # An in-place content edit: the fingerprint cannot see it.
            _invalidate_cross_chat_recall(gen.user_id)
    except Exception as exc:  # noqa: BLE001 — best-effort, but never silent
        log.warning(
            "failed to persist the answer for conversation %s: %s: %s",
            gen.conversation_id,
            type(exc).__name__,
            exc,
        )
        return
    gen.persisted = True


async def _interrupt_open_requests() -> int:
    """Startup and shutdown: every request this process could have been
    running is now 'interrupted'. Never raises — a reconciliation that
    fails must be logged, not turned into a boot loop."""
    try:
        return int(await db.run_in_thread(db.interrupt_open_chat_requests))
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "could not mark open chat requests interrupted", exc_info=True
        )
        return 0


async def _heal_completed_request(row: dict) -> None:
    """A row whose generation already has a durable answer IS completed,
    whatever a restart wrote on it (availability CONTRACT §8.4): the
    process died between persisting the answer and marking the row. Best-
    effort and audible, like every other status write — the answer is in
    history either way, and that, not the row, is what the person gets."""
    try:
        await db.run_in_thread(db.set_chat_request_status, row["intent_id"], "completed")
        logging.getLogger(__name__).info(
            "chat request %s (generation %s) marked completed from its durable answer "
            "(row said %s)",
            row["intent_id"],
            row["generation_id"],
            row.get("status"),
        )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "chat request %s: could not record completed: %s: %s",
            row["intent_id"],
            type(exc).__name__,
            exc,
        )


#: How often expired chunked upload sessions are swept (seconds).
_UPLOAD_SWEEP_INTERVAL_S = 600.0
#: A `finalizing` session untouched for this long belongs to a finaliser
#: that died; a live one keeps bumping updated_at while it assembles.
_STALE_FINALIZING_S = 600.0


async def _reset_stale_upload_finalisations() -> int:
    """Startup: sessions a dead process left `finalizing` go back to
    `uploading`, parts intact, so the next `complete` can finish them."""
    try:
        return int(
            await db.run_in_thread(db.reset_stale_finalizing_upload_sessions, _STALE_FINALIZING_S)
        )
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "could not reset stale finalizing upload sessions", exc_info=True
        )
        return 0


#: The first platform prune waits this long after start-up, so it never
#: competes with the lifespan's own reconciliation queries.
_API_PLATFORM_PRUNE_FIRST_DELAY_S = 60.0


def _api_platform_prune_interval_s() -> float:
    """The artifact maintenance cadence (ARTIFACT_MAINTENANCE_INTERVAL_S,
    30 min), floored at a minute exactly as `artifacts.pipeline` floors it."""
    return max(60.0, float(settings.artifact_maintenance_interval_s))


async def run_api_platform_prune() -> Optional[dict]:
    """One retention pass. Never raises: a failed prune is retried on the next
    tick, and it must not take the loop — or the lifespan — down with it.

    EVERYTHING inside the try (2026-09-13, wave-3 re-verifier): the log line
    that reads `removed.values()` used to sit after it, so a return that was
    not a dict raised AttributeError out of here and ended the loop for the
    life of the process, silently.
    """
    try:
        removed = await db.run_in_thread(db.prune_api_platform)
        if isinstance(removed, dict) and any(removed.values()):
            logging.getLogger(__name__).info("api platform prune removed %s", removed)
        return removed if isinstance(removed, dict) else None
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("api platform prune failed", exc_info=True)
        return None


async def run_rotated_key_expiry() -> Optional[int]:
    """Record the revocation of every key whose rotation overlap has ended.

    `db.expire_rotated_keys` existed and was tested, and nothing scheduled it
    (2026-09-13): the resolver already refuses a key past its deadline, but
    the console listed the rotated-out key as live forever. Same contract as
    the prune: never raises, logs, retries next tick.

    `db.prune_api_platform` now also runs this sweep at its end. Scheduled
    here as well, on purpose: the prune's retention statements run first and
    under a statement timeout, and a prune that raises part-way would skip
    the revocation record with it. The sweep is idempotent, so the second
    call in a healthy tick finds nothing and costs one indexed query.
    """
    try:
        revoked = await db.run_in_thread(db.expire_rotated_keys)
        count = len(revoked) if isinstance(revoked, list) else 0
        if count:
            logging.getLogger(__name__).info(
                "api platform: revoked %d key(s) whose rotation overlap ended", count
            )
        return count
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("rotated key expiry failed", exc_info=True)
        return None


#: The sweeps the platform maintenance loop runs, in order. Looked up by name
#: at each tick so a test can substitute one.
_API_PLATFORM_SWEEPS = ("run_api_platform_prune", "run_rotated_key_expiry")


async def _api_platform_maintenance_tick() -> None:
    """One tick: every sweep, each isolated from the others. A sweep that
    raises anyway (they are written not to) is logged here and the next one
    still runs."""
    module = sys.modules[__name__]
    for name in _API_PLATFORM_SWEEPS:
        try:
            await getattr(module, name)()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).warning("api platform sweep %s failed", name, exc_info=True)


async def _api_platform_prune_loop() -> None:
    """Runs until cancelled at shutdown. Nothing but cancellation leaves it:
    the tick is guarded, and so is the interval read (a settings value that
    stops parsing must not end retention either)."""
    await asyncio.sleep(_API_PLATFORM_PRUNE_FIRST_DELAY_S)
    while True:
        try:
            await _api_platform_maintenance_tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).warning("api platform maintenance tick failed", exc_info=True)
        try:
            interval = _api_platform_prune_interval_s()
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).warning("api platform maintenance interval unreadable", exc_info=True)
            interval = 30 * 60.0
        await asyncio.sleep(interval)


async def _upload_session_sweep_loop() -> None:
    """Every ten minutes, reclaim the parts of chunked sessions past their
    TTL (uploads.sweep_expired_upload_sessions). Imported lazily: uploads
    imports this module's neighbours, and a cycle at import time is exactly
    the kind of failure that shows up only on a cold start."""
    while True:
        await asyncio.sleep(_UPLOAD_SWEEP_INTERVAL_S)
        try:
            from . import uploads

            swept = await asyncio.to_thread(uploads.sweep_expired_upload_sessions)
            if swept:
                logging.getLogger(__name__).info("swept %d expired upload session(s)", swept)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).warning("upload session sweep failed", exc_info=True)


class _ClosingStreamingResponse(StreamingResponse):
    """A StreamingResponse that always closes its body iterator.

    Starlette abandons `body_iterator` when the response task is cancelled
    (a client disconnect cancels `stream_response`). If the cancellation lands
    while the generator is suspended at its `yield` (the frame is in flight to
    `send`), its `finally` does not run until the cyclic GC frees the
    frames/traceback cycle holding it, which can be never on an idle loop —
    so LiveGeneration.subscribers stayed 1 for a reader that was gone.
    `aclose()` runs that `finally` now, deterministically.
    """

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                await aclose()


def _sse_response(frames: AsyncIterator[str]) -> StreamingResponse:
    return _ClosingStreamingResponse(
        frames,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _replay_frames(row: dict, stored: dict, session_id: str) -> AsyncIterator[str]:
    """A completed intent, streamed again from history: the leading meta,
    the answer as ONE token, the persisted meta, `done` — the events a live
    answer would have ended with, so the browser's stream code needs no
    second path. The ids ride on the final meta too: a viewer's persist
    keys on `meta.generation_id`, and history's copy may have been stored
    by a client that dropped them."""
    ids = {
        "generation_id": row["generation_id"],
        "intent_id": row["intent_id"],
        "attempt": int(row.get("attempt") or 1),
    }
    yield sse_event("meta", ids)
    yield sse_event("token", {"text": stored.get("content") or ""})
    meta = stored.get("meta")
    if isinstance(meta, dict) and meta:
        yield sse_event("meta", {**meta, **ids})
    yield sse_event("done", {"session_id": session_id})


# --- AS3 intent-capability BEGIN ---
def _as3_deliverable_shape(published: Sequence[dict]) -> Optional[dict]:
    """The SHAPE of the most recent published deliverable (V39), for the
    intent gate — or None when there is nothing to read or the row cannot be
    read. `published` is newest first and already carries its version row, so
    this costs no query.

    WHAT IS SWALLOWED, AND WHY THAT IS SAFE. Only the reading of ONE row's
    `deliverable` JSON. `of_version`/`to_json` touch no I/O, no lock and no
    other turn state, and the sole effect of a failure is None — which the
    gate treats as "no previous deliverable", so "make it a bar chart
    instead" is read as a NEW artifact instead of an edit of the last one.
    That is exactly the pre-V39 behaviour: the worst case is a turn this
    branch cannot improve, never a wrong answer or a lost one. It must not
    raise, because the caller is the chat streaming path: `deliverable`
    parses defensively now (a `charts` of 'many' used to raise ValueError
    there, on the event loop) and this keeps that true if the row ever grows
    a field that does not.

    It is LOGGED, at the level the sibling guards on this path use (the
    artifact denial backstop), because a row that cannot be read is a writer
    bug somewhere else and nothing downstream will ever mention it again.
    """
    from .artifacts import deliverable as _deliverable

    if not published:
        return None
    try:
        return _deliverable.of_version((published[0].get("current") or {})).to_json()
    except Exception as exc:  # noqa: BLE001 — the follow-up loses its hint, never the turn
        logging.getLogger(__name__).warning(
            "artifact deliverable shape unreadable, the follow-up hint is dropped: %s: %s",
            type(exc).__name__, str(exc)[:200],
        )
        return None
# --- AS3 intent-capability END ---


@app.post("/chat")
async def chat_route(http_request: Request) -> StreamingResponse:
    """POST /chat: the principal FIRST, the body second (2026-09-13).

    `chat` used to be the route itself, with `request: ChatRequest` declared,
    so FastAPI decoded and validated the body before the sign-in check in the
    handler ran. The wave-3 re-verifier measured a cookie-less 1 MiB body of
    `images:[{},…]` at +429 MiB of RSS and a 33 MiB 422 (16 concurrent:
    +4980 MiB) on a box whose unified memory also holds the engine. Now an
    anonymous caller is refused 401 without a byte of its body being read, and
    a signed-in body is parsed by the bounded reader. `chat` itself keeps its
    signature: the resume sweep (app/continuity.py) and the re-attach path
    call it with a model they built themselves.
    """
    from .authn.principal import current_principal
    from .history import read_validated_body

    if await current_principal(http_request) is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    request = await read_validated_body(http_request, ChatRequest)
    return await chat(request, http_request)


async def chat(request: ChatRequest, http_request: Request) -> StreamingResponse:
    """Stream SSE events (§10 + V2-DESIGN §2/§3a/§3b).

    - agent=true → agent engine (plan/execute/synthesize, any mode)
    - mode="assistant" → plain streamed completion, router + data engines
      bypassed entirely
    - mode="salesforce" (default) → v1 router → engine graph (now with the
      5th "chat" class)

    Event order: reasoning/step/token deltas → ONE meta (carrying `route` +
    engine keys, plus the V2 mode/model/effort keys) → done. On failure a
    terminal `error` event is sent instead of `done`.

    The generation itself is DETACHED (LiveGeneration): closing the response
    does not cancel it — use POST /chat/stop for that, GET /chat/attach/{id}
    to re-join it, and GET /chat/active to see what is still running.
    """
    # Image-only sends still need a text instruction for the vision engine.
    # Gated on there actually BEING an image: a Skip click carries no text of
    # its own (`_require_input` permits that), and without the gate the
    # placeholder became the request — so answering a clarifying question by
    # skipping it sent "Analyze the attached image." to the Salesforce planner
    # as the thing the user wanted to know.
    text = request.text or ("Analyze the attached image." if request.image_data else "")

    def meta_extras(route: Optional[str]) -> dict:
        """V2 §2: meta gains mode / model (served model id) / effort — merged
        in centrally so the engines keep emitting their v1 shapes untouched.

        meta is trust metadata, so model/effort must describe what actually
        served the answer, keyed on the engine's own `route`. The picker and
        effort apply where the design routes them: the chat engine (§3a),
        agent synthesis (§3b, smart-pinned) and — since 2026-08-28 — the
        vision route. The data engines stay pinned to the main model at its
        default effort per spec §8.
        """
        extras: dict = {"mode": request.mode}
        if route == "vision":
            # 2026-08-28: this used to say "no effort key: N/A", on the
            # belief that the vision model had no effort knob. It does — the
            # route streams through `llm.stream_chat_events`, which turns
            # effort into the chat template's `enable_thinking` — and it was
            # silently pinned to think, which is why Fast on an image spent
            # its whole budget reasoning. run_vision_engine now runs at
            # request.effort, so meta reports the level that actually served
            # the answer instead of hiding it from the UI and the history.
            extras["model"] = settings.vision_model
            extras["effort"] = request.effort
        elif route == "agent":
            extras["model"] = llm.served_model_id("smart")
            extras["effort"] = request.effort  # applied to synthesis (§3b)
        elif route == "video":
            # Fusion and Q&A run on the main model; the frame captions come
            # from the router, which meta.video records. `effort` is NOT set
            # here: the engine reports the level that actually served the
            # answer (question turns run without thinking below Max), and
            # extras are merged over the engine's meta.
            extras["model"] = llm.served_model_id("smart")
        elif route in ("sql", "rag", "report"):
            extras["model"] = llm.served_model_id("smart")
            # Until 2026-09-17 this said "think" whatever the person chose,
            # on the belief that the data engines were pinned to that level.
            # They are not: their narratives stream through the same
            # `llm.*` functions as every other route, and on a Fast turn
            # those send `enable_thinking` false (llm.mark_fast_turn). meta
            # is trust metadata, so it reports the level that actually
            # served the answer rather than a constant.
            extras["effort"] = request.effort
        else:  # "chat": assistant mode or the salesforce chat class (§3a)
            extras["model"] = llm.served_model_id(request.model)
            extras["effort"] = request.effort
        return extras

    # V9: within-chat memory comes from the messages the frontend sends (robust,
    # survives restarts); the in-process dict is only a fallback for bare API
    # calls. Cross-chat memory: for a signed-in user, look through their OTHER
    # conversations for relevant context and prepend it as a system note.
    from . import facts, memory_semantic
    from .memory_recall import recall_block

    from .authn.principal import current_principal

    principal = await current_principal(http_request)
    # 2026-09-01: /chat is no longer auth-free. Everything downstream — the
    # conversation claim, memory, facts, report binding — assumes a real user.
    if principal is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    signed_in = principal.as_user_row()
    viewer = int(signed_in["id"])
    # The resume sweep (app/continuity.py) comes through this route with a
    # synthetic request: it may resume, never displace.
    from . import continuity as _continuity_mod

    sweep_caller = bool(getattr(http_request.state, _continuity_mod.RESUME_SWEEP_STATE, False))

    # V29: the send intent this request belongs to, and the snapshot a resume
    # would run from — taken BEFORE the feature gate below rewrites the
    # request, so a resume re-applies whatever the account may use THEN.
    # `client_intent` gates the additive event shapes (the leading meta, the
    # ids on the final meta): a client that minted no intent cannot use
    # them, and keeps the exact event order it was built on.
    client_intent = request.intent_id is not None
    intent_id = request.intent_id or uuid.uuid4().hex
    snapshot, resumable = _request_snapshot(request)

    # FEATURE ACCESS (V17, authn/features.py). The composer only offers what
    # this person may use, but the composer is not the gate: rewrite the
    # request itself HERE, once, so every branch below — auto-search, the
    # Salesforce router, Deep Research, the agent's web step — reads flags
    # that already respect it. A blocked tool is downgraded with one status
    # line, never a mid-conversation 403.
    from .authn import features as feature_access

    gate = feature_access.enforce_chat(
        principal.features,
        mode=request.mode,
        web_search=request.web_search,
        deep_research=request.deep_research,
        sf_live=request.sf_live,
    )
    request.mode = gate.mode  # type: ignore[assignment]
    request.web_search = gate.web_search  # type: ignore[assignment]
    request.deep_research = gate.deep_research
    request.sf_live = gate.sf_live
    attachments_allowed = feature_access.allowed(
        principal.features, feature_access.Feature.ATTACHMENTS
    )
    attachment_blocked = False
    if not attachments_allowed and (
        request.pdf_data
        or request.pdf_uploads
        or request.video_uploads
        or request.image_data
        or request.images_data
    ):
        # The upload routes refuse first (that is where files actually land);
        # this covers inline base64 and a tab left open since access changed.
        # The BACKING fields are cleared — pdf_data/images_data are computed.
        attachment_blocked = True
        request.pdf = None
        request.pdf_filename = None
        request.pdf_uploads = None
        request.video_uploads = None
        request.images = None
        request.image = None
        request.image_base64 = None
    video_blocked = False
    if request.video_uploads and not feature_access.allowed(
        principal.features, feature_access.Feature.VIDEO_ANALYSIS
    ):
        # The upload route refused first; this covers a tab left open.
        video_blocked = True
        request.video_uploads = None
    access_notice = feature_access.blocked_notice(
        [
            *gate.blocked,
            *(["Photos and files"] if attachment_blocked else []),
            *(["Video understanding"] if video_blocked else []),
        ]
    )

    # Bare API calls (session_id only, no conversation row) used to share one
    # global key namespace: two callers sending session_id="default" read each
    # other's in-process memory and could cancel each other's generations. The
    # fallback key is now scoped to the authenticated user.
    scoped_session = f"u{viewer}-{request.session_id}"
    conv_key_outer = request.conversation_id or scoped_session

    # The per-conversation stores below (url_documents, repo_chunks) and the
    # live-generation registry are keyed by conversation id ALONE. Without
    # this check, anyone who guessed an id could pull another account's
    # fetched pages and indexed source code into their own prompt.
    #
    # FAIL CLOSED (was: fail open). The old `except: conv_owner = None`
    # skipped the comparison when the ownership lookup itself failed — a
    # database hiccup must surface as an error, never as access.
    if request.conversation_id:
        conv_owner = await db.run_in_thread(
            db.conversation_owner, request.conversation_id
        )
        if conv_owner is None:
            # First message of a NEW conversation: claim the id for this user
            # before any side-table row is written under it, closing the
            # pre-seeding hole (nobody else can later create-and-inherit it).
            if not _CONVERSATION_ID_RE.match(request.conversation_id):
                raise HTTPException(status_code=422, detail="invalid conversation id")
            title = (request.text or "New chat").strip()[:80] or "New chat"
            try:
                await db.run_in_thread(
                    db.create_conversation, viewer, request.conversation_id, title
                )
            except db.IntegrityError:
                pass  # raced another request — the recheck below decides
            conv_owner = await db.run_in_thread(
                db.conversation_owner, request.conversation_id
            )
        if conv_owner != viewer:
            raise HTTPException(status_code=404, detail="conversation not found")

    # DURABLE INTENT (V29). Record the send before anything runs, so the
    # request survives the process that accepted it (RC-3). A KNOWN intent
    # never starts a second generation for the same question: it attaches
    # to the live one, replays the durable answer, or — when the process
    # that held it is gone — runs a new attempt under a new generation_id.
    from . import metrics as _metrics

    gen = LiveGeneration(request.conversation_id, viewer)
    gen.intent_id = intent_id
    gen.answer_branch = request.answer_branch
    gen.effort = str(request.effort or "")
    # ONE commit for acceptance (2026-09-14): a NEW intent is written as
    # `running` — its worker starts below without a further await on the
    # database — and the parked rows it supersedes (see the `keep` comment
    # further down) are cancelled in the same transaction. A known intent
    # (row None) takes the attach / replay / retry paths exactly as before.
    supersede_keep = (
        None
        if sweep_caller
        else [intent_id] + [g.intent_id for g in _live_generations.values() if g.intent_id]
    )
    row = await db.run_in_thread(
        db.create_chat_request,
        intent_id,
        viewer,
        conv_key_outer,
        gen.generation_id,
        snapshot,
        resumable=resumable,
        status="running",
        supersede_parked_except=supersede_keep,
    )
    resumed = False
    if row is not None:
        gen.request_status = "running"
    if row is None:
        known = await db.run_in_thread(db.get_chat_request, intent_id)
        if (
            known is None
            or int(known["user_id"]) != viewer
            or known["conversation_id"] != conv_key_outer
        ):
            # Someone else's intent, or this person's from another
            # conversation: one 409 for both, so the status code cannot say
            # whether an id exists elsewhere.
            _metrics.inc("chat_request_total", "chat requests by outcome", result="conflict")
            raise HTTPException(
                status_code=409, detail="intent_id belongs to another conversation"
            )
        live = _live_generation_for(known["generation_id"])
        if live is not None:
            # accepted/running and still in this process: the same stream,
            # buffer replayed from its leading meta.
            _metrics.inc("chat_request_total", "chat requests by outcome", result="attached")
            return _sse_response(live.follow())
        # A durable answer under the row's generation is replayed WHATEVER
        # the row says (availability CONTRACT §8.4: no new attempt while a
        # completed assistant message exists for the generation). The row
        # normally says 'completed' — but the server persists the answer
        # BEFORE it marks the row, so a process that died in between left
        # it 'running' and the next startup marked it 'interrupted'. A
        # resume of that row would answer the question a second time under
        # a new generation_id, beside the answer already in history. The
        # row is healed to what the thread proves, and the answer replayed.
        stored = await db.run_in_thread(
            _persisted_answer, conv_key_outer, known["generation_id"]
        )
        if _is_answer(stored):
            # Healed only when a LOST process left it open; a Stop whose
            # partial a viewer persisted is replayed as it stands and stays
            # a Stop (review finding 2026-09-12, main.py:1686).
            if known["status"] in _RESUMABLE_STATUSES:
                await _heal_completed_request(known)
            _metrics.inc("chat_request_total", "chat requests by outcome", result="replayed")
            return _sse_response(_replay_frames(known, stored, request.session_id))
        # Completed with nothing durable to replay (an empty answer, or a
        # persist that failed): the person asked again — answer again.
        # interrupted / accepted / running with no live generation (the
        # process that held it is gone), failed, cancelled: a new attempt.
        # THIS body is what runs, not the snapshot — it carries whatever
        # inline bytes the snapshot could not keep, so a retry from the
        # browser works even where a server-side resume could not.
        gen.retry_reason = _RETRY_REASONS.get(str(known["status"]), "none")
        if sweep_caller and _conversation_is_live(conv_key_outer):
            # The resume sweep never displaces a person's live generation
            # (its "newest message wins" is a person's rule): the row stays
            # as it is for the next READY or the browser's re-attach.
            raise HTTPException(status_code=409, detail="conversation busy; the row waits")
        # Claimed BEFORE the compare-and-swap: from the moment the row names
        # this generation, another resumer of the same intent (a browser's
        # re-attach racing the sweep) must find it here and attach to it,
        # not read the row as `accepted` with nobody live and start a third
        # attempt in the gap before the registration below.
        _resuming[gen.generation_id] = (gen, _time_monotonic())
        try:
            row = await db.run_in_thread(
                _retry_chat_request, intent_id, gen.generation_id,
                expected_generation_id=known["generation_id"],
            )
            if row is None or row["generation_id"] != gen.generation_id:
                # Raced another retry of the same intent; that one owns it now.
                other = _live_generation_for(row["generation_id"]) if row else None
                if other is not None:
                    _metrics.inc("chat_request_total", "chat requests by outcome", result="attached")
                    return _sse_response(other.follow())
                _metrics.inc("chat_request_total", "chat requests by outcome", result="conflict")
                raise HTTPException(status_code=409, detail="intent_id is being retried")
        except BaseException:
            _resuming.pop(gen.generation_id, None)
            raise
        resumed = True
        setattr(http_request.state, _continuity_mod.RESUMED_GENERATION_STATE, gen.generation_id)
    gen.attempt = int(row.get("attempt") or 1)

    # A new send for a conversation that is still generating replaces the old
    # generation — the user's newest message wins. Only the owner's newest
    # message: replacement must never cancel someone else's work. A retry of
    # the SAME intent never reaches here while its generation is live (it
    # attached above), so what is cancelled here is always a different
    # question.
    previous = _live_generations.get(conv_key_outer)
    if (
        previous is not None
        and not previous.done
        and previous.task is not None
        and previous.user_id == viewer
    ):
        if sweep_caller:
            # A person's send landed in the gap between the sweep's busy
            # check and here (round-2 review, continuity.py:398): the person
            # wins. The compare-and-swap above already moved the row to this
            # generation; put it back where the next sweep finds it.
            _resuming.pop(gen.generation_id, None)
            await db.run_in_thread(db.set_chat_request_status, intent_id, "queued")
            raise HTTPException(status_code=409, detail="conversation busy; the row waits")
        # ORCH-02: the cancelled generation's followers get a terminal frame
        # (see the worker's CancelledError branch) instead of a stream that
        # merely ends — which a client reads as "finished" and persists as a
        # complete answer — and the log says which generation lost to which.
        previous.replaced = True
        logging.getLogger(__name__).info(
            "generation %s (intent %s) in conversation %s replaced by generation %s (intent %s)",
            previous.generation_id,
            previous.intent_id,
            conv_key_outer,
            gen.generation_id,
            intent_id,
        )
        previous.task.cancel()
    if not resumed and not sweep_caller:
        # The newest message wins over PARKED questions too (round-2 review,
        # main.py:1775): a row left `queued` by an expired hold or a dead
        # process, with no live generation in this process, would otherwise
        # be resumed by the sweep and answered below this newer exchange.
        # A row this process still holds is cancelled through its task
        # (above), which writes its own status. The cancel itself ran in the
        # acceptance transaction (`supersede_parked_except`), with `keep` =
        # this intent + every generation this process held at that moment.
        with contextlib.suppress(Exception):
            superseded = int(row.get("superseded") or 0)
            if superseded:
                logging.getLogger(__name__).info(
                    "%d parked request(s) in conversation %s superseded by intent %s",
                    superseded, conv_key_outer, intent_id,
                )

    if request.answer_branch and not resumed and not sweep_caller:
        # A new VERSION of an answer was asked for (a "Try again", an edit, a
        # send after a fork). Ids only, never content: the next incident about
        # answers that stack or vanish can be read from here.
        logging.getLogger(__name__).info(
            "answer_branch intent=%s conversation=%s self=%s parent=%s",
            intent_id,
            conv_key_outer,
            request.answer_branch["self"],
            request.answer_branch.get("parent", ""),
        )
    _live_generations[conv_key_outer] = gen
    _resuming.pop(gen.generation_id, None)
    # One correlation envelope per HTTP attempt. `test_case_id` is only a
    # join key; golden expectations remain in the offline evaluator.
    query_trace = _QueuedTraceRecorder(
        gen.generation_id,
        test_case_id=request.test_case_id,
        versions={
            "application": app.version,
            "database_schema": db.LATEST_SCHEMA_VERSION,
            "salesforce_api": settings.sf_api_version,
            "model_choice": request.model,
            "model": llm.served_model_id(request.model),
        },
    )
    # The FIRST event names the generation, so the browser can mark its turn
    # accepted before a single token exists — and a re-attach, which replays
    # the buffer, starts with it too. Published directly, not through
    # emit(): it is not the engine's meta and must not count as one. The
    # engine's final meta is still emitted last and still wins (the client
    # replaces its meta on every meta event; frontend/lib/streams.ts).
    if client_intent:
        await gen.publish(
            "meta",
            {
                "generation_id": gen.generation_id,
                "trace_id": query_trace.trace_id,
                "request_id": query_trace.request_id,
                "intent_id": intent_id,
                "attempt": gen.attempt,
                **({"test_case_id": request.test_case_id} if request.test_case_id else {}),
            },
        )
    _metrics.inc(
        "chat_request_total", "chat requests by outcome", result="resumed" if resumed else "accepted"
    )
    if resumed:
        _metrics.inc("chat_request_resume_total", "chat requests resumed under a new attempt")

    # Filled in by the compaction pass; rides out on the final meta so the
    # context meter shows this session's real usage.
    context_state: dict = {}
    # What the auto-orchestration decided, surfaced on the final meta.
    orchestration_state: dict = {}
    # Facts the background extractor saved from THIS message; surfaced on the
    # final meta as `memory_updated` (the ChatGPT-style chip). Extraction runs
    # concurrently with generation, so by meta time it has almost always
    # finished; when it hasn't, the facts still persist — only the chip is
    # skipped for this turn.
    memory_state: dict = {}
    # The extraction task, and the gate it waits on before touching the
    # model or the table (facts.remember_after_route). An ARTIFACT turn
    # resolves the gate False and cancels the task before its engine runs,
    # so a request for a file never becomes a saved fact and the meta never
    # carries `memory_updated` (CONTRACT-2 §8); every other route resolves
    # it True the moment the route is known, and the worker's `finally`
    # resolves whatever is still pending so the task cannot wait forever.
    fact_task: Optional["asyncio.Task[list]"] = None
    fact_gate: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()

    def _release_facts(extract: bool) -> None:
        if not fact_gate.done():
            fact_gate.set_result(bool(extract))
    # Salesforce Intelligence Mode extras (assumptions, resolved scope, the
    # final phase) merged into whichever engine's meta ends up being emitted.
    salesforce_state: dict = {}
    #: The answer as it is streamed, so `meta.sources` can mark which sources
    #: were actually cited. Capped because a runaway generation must not grow
    #: an unbounded list in the request's memory.
    streamed_text: List[str] = []
    # Living-knowledge extras: the sources a locally-grounded answer used, so
    # the Sources panel can show provenance for an answer that never searched.
    knowledge_state: dict = {}
    provenance_recorded = False
    # Wall clock for the route-mix / TTFT metrics stamped on meta.
    from time import perf_counter as _perf_counter

    _timing: dict = {
        "started": _perf_counter(),
        "first_token": None,
        "first_visible": None,
        "first_visible_kind": "",
        "first_visible_observed": False,
    }

    def _observe_first_visible(route: str) -> None:
        if _timing["first_visible"] is None or _timing["first_visible_observed"]:
            return
        _timing["first_visible_observed"] = True
        _latency_metrics.chat_first_visible(
            _timing["first_visible"],
            route=route,
            effort=str(request.effort or ""),
            kind=_timing["first_visible_kind"],
        )
    # The meter's exact prompt count when compaction deferred it behind the
    # answer (compaction.prepare_deferred): folded into context_state before
    # anything reads it.
    context_pending: dict = {"task": None}

    async def _settle_context() -> dict:
        task = context_pending["task"]
        if task is not None:
            try:
                context_state.update(await asyncio.shield(task))
            except asyncio.CancelledError:
                # The count itself was cancelled (a turn that ended without
                # its meta): the details already known stand. A cancellation
                # of THIS coroutine still propagates.
                if not task.cancelled():
                    raise
            except Exception:  # noqa: BLE001 — the meter is never worth a turn
                pass
            context_pending["task"] = None
        return context_state

    async def emit(event: str, data: dict) -> None:
        nonlocal provenance_recorded
        if event == "token":
            # Kept so the meta below can say which sources the answer cited.
            # Bounded: only the markers matter, and a marker is a few bytes.
            piece = data.get("text")
            if isinstance(piece, str) and len(streamed_text) < _MAX_STREAM_PIECES:
                streamed_text.append(piece)
        if event == "meta":
            data = {
                **data,
                **meta_extras(data.get("route")),
                "generation_id": gen.generation_id,
                "trace_id": query_trace.trace_id,
                "request_id": query_trace.request_id,
                **({"test_case_id": request.test_case_id} if request.test_case_id else {}),
                # V29: the intent and attempt, on the meta that gets
                # persisted with the answer — how a reloaded tab matches an
                # answer to the send it belongs to.
                **({"intent_id": gen.intent_id, "attempt": gen.attempt} if client_intent else {}),
            }
            # Every file an engine writes into the shared reports dir is
            # advertised on meta.report_files. Binding ownership HERE — the
            # one choke point every engine's meta passes through — is what
            # lets GET /reports/{filename} refuse everyone else.
            for report_file in data.get("report_files") or ():
                name = (report_file or {}).get("filename")
                if name:
                    from .authn import store as authn_store

                    await db.run_in_thread(
                        authn_store.bind_report,
                        name,
                        viewer,
                        request.conversation_id,
                    )
            # If anything had to be removed to fit the window, say so rather
            # than silently answering from a shortened prompt.
            trimmed = context.get_trim_notice()
            if trimmed:
                data["input_trimmed"] = trimmed
            if context_pending["task"] is not None:
                await _settle_context()
            if context_state:
                data["context"] = dict(context_state)
            if orchestration_state:
                data["auto"] = dict(orchestration_state)
            if memory_state.get("facts"):
                data["memory_updated"] = list(memory_state["facts"])
            # Merged rather than overwritten: the engine that answered owns
            # `route`, `data` and `chart`, and the Salesforce planner only ever
            # ADDS provenance and assumptions on top of them.
            for key, value in salesforce_state.items():
                data.setdefault(key, value)
            # Locally-sourced evidence rides the SAME `sources` key the search
            # engine uses — one contract for the UI, whether the pages were
            # read a second ago or a week ago.
            if knowledge_state.get("sources") and not data.get("sources"):
                # Mark which of them the answer actually leant on. The search
                # path has done this since S5; the knowledge path emitted every
                # retrieved source looking equally used, so a page that merely
                # matched the query was presented exactly like the one the
                # answer quoted. Parsed from the text that was really streamed,
                # not from the model's intent.
                cited_numbers = {
                    int(m) for m in _CITE_MARKER_RE.findall("".join(streamed_text))
                }
                data["sources"] = [
                    {**row, "cited": int(row.get("n") or 0) in cited_numbers}
                    for row in knowledge_state["sources"]
                ]
                data["knowledge"] = {
                    "freshness": knowledge_state.get("freshness", ""),
                    "from_local_memory": knowledge_state.get("from_local_memory", True),
                }
            if knowledge_state.get("decision") or knowledge_state.get("degraded"):
                data.setdefault("knowledge", {})
                if knowledge_state.get("decision"):
                    data["knowledge"]["decision"] = knowledge_state["decision"]
                if knowledge_state.get("degraded"):
                    data["knowledge"]["degraded"] = knowledge_state["degraded"]
            # Local RAG provenance is created only after the actual sources
            # have been merged. Search/network sources deliberately do not get
            # relabelled as the org knowledge base.
            if (
                not data.get("provenance")
                and data.get("sources")
                and (data.get("knowledge") or {}).get("from_local_memory")
            ):
                cited = [row for row in data["sources"] if row.get("cited")]
                data["provenance"] = {
                    "source": "org_knowledge_base",
                    "environment": settings.sf_environment,
                    "freshness": (data.get("knowledge") or {}).get("freshness")
                    or "metadata_snapshot",
                    "retrieved_source_count": len(data["sources"]),
                    "cited_source_count": len(cited),
                }
            # The Fast small-talk lane consulted no evidence at all and says
            # so in the same provenance shape (source "model"). Only the lane
            # sets this key, so every other turn's meta is unchanged.
            if not data.get("provenance") and isinstance(knowledge_state.get("model_provenance"), dict):
                data["provenance"] = dict(knowledge_state["model_provenance"])
            if isinstance(data.get("provenance"), dict) and not provenance_recorded:
                provenance_recorded = True
                await query_trace.event(
                    "PROVENANCE_RECORDED",
                    component="orchestrator.app.main.emit",
                    details=data["provenance"],
                )
            # Route mix and time to first token, orchestrator-side (vLLM's
            # own TTFT excludes every pre-pass — the number that matters to
            # the person waiting is this one).
            from . import metrics as _metrics
            from time import perf_counter as _pc

            route = str(data.get("route") or "unknown")
            _metrics.inc("chat_route_total", route=route, effort=str(request.effort or ""))
            if _timing.get("first_token") is not None:
                _metrics.observe(
                    "chat_ttft_seconds", _timing["first_token"], route=route, effort=str(request.effort or "")
                )
            _observe_first_visible(route)
            _metrics.observe(
                "chat_total_seconds", _pc() - _timing["started"], route=route, effort=str(request.effort or "")
            )
            gen.final_meta = data
        elif event == "token" and _timing.get("first_token") is None:
            from time import perf_counter as _pc

            _timing["first_token"] = _pc() - _timing["started"]
        if _timing.get("first_visible") is None and event in ("token", "reasoning", "status"):
            # chat_first_visible_seconds (plan item 1, 2026-09-13): the first
            # thing a person can READ — an answer token, a reasoning token,
            # or a status line with words in it. chat_ttft_seconds only sees
            # answer tokens, so Think and Max looked 9-38 s slow while their
            # reasoning was already on screen. Stamped now, observed with
            # the route at meta — or as "unknown" from the worker's
            # `finally`, so a turn stopped before its meta still counts.
            piece = data.get("text")
            if isinstance(piece, str) and piece.strip():
                _timing["first_visible"] = _perf_counter() - _timing["started"]
                _timing["first_visible_kind"] = "answer" if event == "token" else event
        await gen.publish(event, data)

    async def worker() -> None:
        # `text` is rebound when the user answers a clarifying question, so it
        # must be declared here — a `nonlocal` further down would come after
        # the reads above it and fail to compile. `fact_task` is assigned
        # below only on a signed-in assistant turn and read by the artifact
        # branch on every turn: without the declaration the assignment
        # would make it a local of this function and the read an
        # UnboundLocalError on every other path.
        nonlocal text, fact_task
        # Per-request state: this task owns its own trim record, and its own
        # token accounting (V18 — llm._usage is a ContextVar with exactly the
        # same per-task scope).
        context.reset_trim_notice()
        llm.reset_usage()
        # FAST NEVER THINKS (owner rule, 2026-09-17). Declared once, here,
        # for the whole turn: every main-model call this task makes — the
        # answer, a search fallback, a repo Q&A, a compaction summary, the
        # route classifier's fallback, an agent step — reads it inside
        # llm.reasoning_extra_body and sends `enable_thinking` false. A
        # ContextVar with the same per-task scope as the accounting above, so
        # it reaches every engine without a parameter and cannot leak into
        # another request. The public /v1 API has its own task and never
        # passes here, so it is unaffected (by design).
        llm.mark_fast_turn(llm.normalize_effort(request.effort) == "fast")
        trace_context = query_trace.activate()
        # While a model call inside this turn waits for a restarting engine,
        # the person sees why instead of a silent spinner (app/resilience.py).
        # A ContextVar, scoped to this task like the accounting above, so it
        # reaches every engine without threading a callback through them.
        from .resilience import set_wait_notifier

        set_wait_notifier(lambda line: emit("status", {"text": line}))
        # Continuity in one-model mode (app/continuity.py, CONTRACT §8.3):
        # while the main model cannot take a call of this turn, the row says
        # `queued`, the person reads one exact line through the notifier
        # above, and the SAME generation resumes when the model is READY.
        # Bound to this task like the notifier, so every model call the
        # turn makes — however deep — finds it.
        from . import continuity as _continuity

        _continuity.bind(gen, lambda line: emit("status", {"text": line}))
        # Who the model is assisting — safe context for prompt builders
        # (engines append identity.identity_line() to their system prompts).
        from .identity import set_identity

        set_identity(
            str(signed_in.get("display_name") or signed_in.get("username") or ""),
            str(signed_in.get("email") or ""),
            str(signed_in.get("workspace_name") or ""),
        )
        reads = _ContextReads(_context_concurrent_reads_enabled())
        cancel_pending_task: Optional[asyncio.Task] = None
        try:
            if gen.request_status != "running":
                # A new intent was accepted as `running` in its acceptance
                # commit; a resumed attempt (`accepted` by the retry) says so here.
                await _mark_chat_request(gen, "running")
            await query_trace.start(
                conversation_id=conv_key_outer,
                user_id=viewer,
                workspace_id=principal.workspace_id,
                question=text,
                requested_mode=str(request.mode or ""),
            )
            await query_trace.event(
                "REQUEST_RECEIVED",
                component="orchestrator.app.main.chat",
                details={
                    "requested_mode": request.mode,
                    "request_id": query_trace.request_id,
                    "test_case_id": request.test_case_id,
                    "effort": request.effort,
                    "model_choice": request.model,
                    "force_live": request.sf_live,
                    "has_attachments": bool(
                        request.pdf_data or request.image_data or request.video_uploads
                    ),
                    "history_messages_supplied": len(request.history_messages or ()),
                },
            )
            if access_notice:
                # One line, before any work: the tool the composer offered is
                # off for this account, and the answer that follows is the
                # downgraded one. Said once per turn, never as an error.
                await emit("status", {"text": access_notice})
            history = request.history_messages or memory.history(scoped_session)
            # What this turn and its earlier user turns PASTED (hotfix 1.2,
            # P6). Scoped to this task like the notifier above, so every
            # engine's web query - however deep - is checked against it in
            # engines/search.py `_collect_results`, and none carries a run of
            # it to a search provider.
            from .core import pasted as _pasted

            _pasted.mark_turn(
                text,
                *(
                    m["content"]
                    for m in history
                    if m.get("role") == "user" and isinstance(m.get("content"), str)
                ),
            )

            # THE FAST SMALL-TALK LANE (app/fast_lane.py, 2026-09-14). A
            # closed-lexicon pleasantry at Fast ("hi", "thanks", "bye") skips
            # every pre-pass that cannot change its answer — the freshness
            # router, retrieval, rerank, cross-chat recall, the stored
            # documents and compaction — and gets a short prompt. Decided
            # once, here, before any read starts; every later block that the
            # lane skips carries `not lane.entered`. The stored conversation
            # is untouched, so the next real question sees all of it.
            lane = fast_lane.decide(
                request, text=text, history=history, now_year=datetime.now(timezone.utc).year
            )
            if lane.entered and gen.intent_id and request.conversation_id:
                # The browser sends a died or stopped answer back as plain
                # text, so the history alone cannot tell "hi ??" after a
                # crash from "hi ??" after an answer. The previous send's
                # durable status can.
                if await _previous_send_unfinished(conv_key_outer, gen.intent_id):
                    lane = fast_lane.LaneDecision(False, lane.category, "unanswered_previous")
            fast_lane.record(lane)

            # CONTEXT READS START HERE (see _ContextReads). Before decide():
            # none of them depends on the plan, and at Think/Max the plan is
            # a router round trip the reads no longer wait behind.
            conv_key = request.conversation_id or scoped_session
            plain_text_turn = bool(request.text and not request.pdf_data and not request.image_data)

            def read_facts():
                return db.run_in_thread(db.list_user_facts, viewer, settings.memory_max_facts)

            async def read_cross_chat():
                # For a question that needs EVIDENCE (an office holder,
                # a price, a release) the assistant's own earlier answers
                # are not evidence: the audit found one being repeated
                # and cited against sources that never contained it,
                # while a colleague asking the same thing got "not in
                # the sources". What the USER said earlier still counts.
                from .freshness import classify_offline

                needs_evidence = classify_offline(
                    request.text, now_year=datetime.now(timezone.utc).year
                ).needs_evidence
                return await memory_semantic.cross_chat_block(
                    viewer,
                    request.text,
                    request.conversation_id,
                    include_assistant=(
                        settings.recall_assistant_answers_for_facts
                        or not needs_evidence
                    ),
                )

            def read_keyword_recall():
                return db.run_in_thread(
                    recall_block,
                    viewer,
                    request.text,
                    request.conversation_id,
                )

            def read_repo_keys():
                return db.run_in_thread(db.get_repo_keys, conv_key)

            def read_crawl_hits():
                from .engines.crawl import site_hits_for

                return site_hits_for(conv_key, request.text)

            def read_url_documents():
                return db.run_in_thread(db.get_url_documents, conv_key)

            def read_documents():
                return db.run_in_thread(db.get_documents, conv_key)

            def read_videos():
                return db.run_in_thread(db.get_conversation_videos, conv_key)

            def read_uploads():
                return db.run_in_thread(db.get_uploads, conv_key)

            def read_recall():
                from . import recall as _recall

                return _recall.retrieve_block(
                    conv_key, text, effort=llm.normalize_effort(str(request.effort or ""))
                )

            def read_summary():
                return db.run_in_thread(db.get_summary, conv_key)

            def read_artifacts():
                from .engines import artifact as _artifact_engine

                return db.run_in_thread(_artifact_engine.adb.list_artifacts, viewer, conv_key)

            if reads.concurrent and request.text and lane.entered:
                # The saved facts are the one read a greeting can use.
                reads.start("facts", read_facts)
            elif reads.concurrent and request.text:
                if request.mode == "assistant":
                    reads.start("facts", read_facts)
                    reads.start("cross_chat", read_cross_chat)
                else:
                    reads.start("keyword_recall", read_keyword_recall)
                if settings.repo_analysis_enabled and plain_text_turn:
                    reads.start("repo_keys", read_repo_keys)
                if (
                    settings.web_crawl_enabled
                    and plain_text_turn
                    and request.conversation_id
                    and request.web_search != "on"
                    and not request.agent
                ):
                    from .engines.crawl import _URL_RE as _crawl_url_re, detect_crawl, detect_resume
                    from .engines.search import _FRESH_RE as _fresh_re

                    if (
                        detect_crawl(request.text) is None
                        and not detect_resume(request.text)
                        and not _crawl_url_re.search(request.text)
                        and not _fresh_re.search(request.text)
                    ):
                        reads.start("crawl_hits", read_crawl_hits)
                if settings.url_analysis_enabled and plain_text_turn:
                    reads.start("url_documents", read_url_documents)
                if plain_text_turn:
                    reads.start("documents", read_documents)
                if (
                    settings.video_analysis_enabled
                    and not request.video_uploads
                    and not request.pdf_data
                    and not request.pdf_uploads
                    and not request.image_data
                ):
                    reads.start("videos", read_videos)
                if settings.dataset_uploads_enabled and plain_text_turn:
                    reads.start("uploads", read_uploads)
                if (
                    settings.artifacts_enabled
                    and not request.video_uploads
                    and feature_access.allowed(principal.features, feature_access.Feature.ARTIFACTS)
                ):
                    reads.start("artifacts", read_artifacts)
            if reads.concurrent and request.conversation_id and not lane.entered:
                reads.start("recall", read_recall)
                reads.start("summary", read_summary)
            # Phase 1: decide whether to run web search (never for attachments).
            # AUTO-ORCHESTRATION (2026-07-28): with no Agent toggle in the UI,
            # one cheap non-thinking call decides whether this request deserves
            # agent steps and/or web search. An explicit user choice always
            # wins; effort "low" opts out entirely.
            # Salesforce mode means "answer from MY data". The AGENT is still
            # allowed there — it is how a question reaches a live Salesforce
            # lookup — but automatic WEB SEARCH is not: search is checked
            # before the route chain, so a classifier that fancied the web
            # hijacked the request and the Salesforce router never saw it.
            # Live, "what problems do customers describe in their support
            # cases?" came back with web articles about IT ticketing instead
            # of this org's cases.
            auto_web_search_allowed = request.mode == "assistant"

            auto_plan = None
            if (
                request.text
                and not request.pdf_data
                and not request.image_data
                and not request.video_uploads
                and not request.agent
            ):
                from .engines.orchestrate import allowances as _allowances, decide

                decide_started = _perf_counter()
                auto_plan = await decide(request.text, history, request.effort)
                _allowed = _allowances(request.effort)
                _latency_metrics.orchestrate_decide(
                    _perf_counter() - decide_started,
                    effort=str(request.effort or ""),
                    plan=_latency_metrics.plan_label(auto_plan.agent, auto_plan.search),
                    # decide() asks the router only when the effort permits
                    # agent or search at all; Fast never does.
                    outcome="ok" if (_allowed["agent"] or _allowed["search"]) else "skipped",
                )

            want_agent = request.agent or bool(auto_plan and auto_plan.agent)

            want_search = False
            search_rate_limited = False
            if (
                settings.search_enabled
                and request.web_search != "off"
                and not request.pdf_data
                and not request.image_data
                and not request.video_uploads
                and request.text
                # Salesforce mode NEVER searches the web — at any effort
                # level, and even if the client sends web_search="on" (owner
                # request 2026-08-05; until then an explicit "on" was an
                # escape hatch). The composer hides the web-search option in
                # that mode, and this gate makes the promise hold for ANY
                # client, not just the current UI. Turning the Salesforce
                # toggle off is how you ask the web.
                and auto_web_search_allowed
                # A small-talk lane turn never searches, so it must not spend
                # a slot of the per-user search rate limit either.
                and not lane.entered
            ):
                from .engines.search import rate_ok, should_search

                user_key = str(viewer)
                if not rate_ok(user_key):
                    search_rate_limited = True
                    await emit(
                        "status",
                        {"text": "Search rate limit reached — answering from model knowledge."},
                    )
                elif request.web_search == "on":
                    want_search = True
                elif auto_plan is not None:
                    # The orchestration call already judged this request.
                    want_search = auto_plan.search
                else:  # "auto"
                    # The conversation's own turns only — never the pinned
                    # memory blocks (should_search strips them itself).
                    want_search = await should_search(request.text, history)

            # Deep Research is EXPLICIT-only and needs the web: without a
            # search provider there is nothing to research, so the request
            # degrades to the ordinary engines rather than pretending. It is
            # computed here, with the other web gates, because the pre-passes
            # below all have to know about it — a research question that
            # happens to quote a URL must not be diverted into the
            # single-page reader, and one that says "index" must not be read
            # as a crawl request.
            deep_research_on = bool(
                request.deep_research
                and settings.deep_research_enabled
                and settings.search_enabled
                and request.text
                and not request.pdf_data
                and not request.image_data
                and auto_web_search_allowed
            )
            if request.deep_research and not deep_research_on:
                await emit(
                    "status",
                    {"text": "Deep Research is unavailable here — answering normally."},
                )

            # LIVING KNOWLEDGE, started early (2026-09-03). The classifier +
            # local retrieval are independent of every pre-pass below
            # (memory recall, stored pages, compaction), so they run
            # CONCURRENTLY with them instead of after — measured ~150-250 ms
            # off a Fast answer's time to first token. Only for the turn
            # shape the plain chat branch can actually answer; when another
            # engine wins the dispatch the task is cancelled unread.
            knowledge_task: Optional[asyncio.Task] = None
            # Set the moment the pre-pass enters its ONE slow branch (the
            # live lookup). The status line below keys off this, not off
            # "the task is still running": on a host where the embedding or
            # router calls fail slowly (DNS timeouts on CI) an unfinished
            # task means nothing is being fetched, and announcing a lookup
            # that is not happening broke the assistant-mode event contract.
            knowledge_lookup_started = asyncio.Event()

            async def _note_lookup(kind: str, data: dict) -> None:
                knowledge_lookup_started.set()

            # May this request spend network at all? The pill OFF is a hard
            # stop at every effort (until 2026-09-03 the Fast pre-pass still
            # fetched two pages with the pill off, outside the per-user rate
            # limit and unattributed in the search log). Salesforce mode and
            # the rate limit close it too.
            search_allowed = bool(
                settings.search_enabled
                and request.web_search != "off"
                and auto_web_search_allowed
                and not search_rate_limited
            )
            # Stage 1 runs for every assistant text turn — including one the
            # auto classifier wants to SEARCH (ADR-0001 D6): if the store
            # answers with confidence, the search is skipped; if it cannot,
            # a Think request escalates. Only a FORCED search skips it: the
            # search engine merges stored passages itself. Network inside
            # the pre-pass is only allowed when no search is going to run.
            prepared_early = None
            if (
                settings.living_knowledge_enabled
                and request.mode == "assistant"
                and request.text
                and not request.pdf_data
                and not request.pdf_uploads
                and not request.video_uploads
                and not request.image_data
                and not request.agent
                and not want_agent
                and not (want_search and request.web_search == "on")
                and not deep_research_on
                and not lane.entered
            ):
                knowledge_task = asyncio.ensure_future(
                    _prepare_knowledge(
                        request,
                        text,
                        allow_network=search_allowed and not want_search,
                        emit=_note_lookup,
                        user_id=viewer,
                        conversation_id=conv_key_outer,
                        # A terse follow-up has no subject of its own; without
                        # the turns it retrieves nothing and the answer is
                        # ungrounded (living_knowledge.resolve_from_history).
                        history=history,
                    )
                )

            # Announce and record the auto-decision only AFTER the gates
            # above, so the status line and meta.auto describe what will
            # actually run. Before this reorder the label came straight from
            # the classifier's raw wish: Salesforce mode showed "searching
            # the web…" while the gate silently blocked the search, directly
            # contradicting the composer's "no web search" promise (owner
            # report 2026-08-06). Same held for SEARCH_ENABLED=false and the
            # rate limit.
            if auto_plan is not None:
                from .engines.orchestrate import Plan, describe

                effective = Plan(
                    agent=auto_plan.agent,
                    search=bool(auto_plan.search and want_search),
                )
                label = describe(effective)
                if label:
                    await emit("status", {"text": f"{label}…"})
                if effective.agent or effective.search:
                    orchestration_state.update(
                        {"agent": effective.agent, "search": effective.search}
                    )
                query_trace.resolved_mode = (
                    "agent"
                    if effective.agent
                    else "web_search"
                    if effective.search
                    else request.mode
                )
                await query_trace.event(
                    "MODE_RESOLVED",
                    component="orchestrator.app.engines.orchestrate",
                    details={
                        "requested_mode": request.mode,
                        "resolved_mode": query_trace.resolved_mode,
                        "agent": effective.agent,
                        "search": effective.search,
                    },
                )

            # context_assembly_seconds starts where the MODE_RESOLVED trace
            # event is written, so it reads against the 2026-09-13 baseline
            # (MODE_RESOLVED -> CONTEXT_ASSEMBLED, p50 209 ms).
            assembly_started = _perf_counter()
            if signed_in is not None and request.text:
                user_id = int(signed_in["id"])
                if request.mode == "assistant":
                    # V10 cross-chat memory — normal chat only, by request:
                    # Salesforce mode answers from CRM data, not chat memory.
                    # 1. Kick fact extraction off CONCURRENTLY with the
                    #    answer (it reads only the user's message); the strong
                    #    ref keeps it alive past this request if need be.
                    if settings.fact_extraction_enabled:
                        fact_task = asyncio.create_task(
                            facts.remember_after_route(
                                fact_gate,
                                user_id,
                                request.text,
                                request.conversation_id,
                                # A turn that carries a document or an image
                                # is a turn ABOUT that material. Third-party
                                # content is never a fact about the person
                                # (the sweep found a pasted CV overwriting an
                                # account's own name, email and employer), so
                                # such a turn writes no memory at all.
                                attachments=bool(
                                    request.pdf
                                    or request.pdf_uploads
                                    or request.video_uploads
                                    or request.images
                                    or request.image
                                    or request.image_base64
                                ),
                            )
                        )
                        _background_tasks.add(fact_task)

                        def _facts_done(task) -> None:
                            _background_tasks.discard(task)
                            try:
                                saved = task.result()
                            except BaseException:
                                # CancelledError is a BaseException; a bare
                                # Exception clause would let it escape the
                                # event loop's callback handler.
                                return
                            if saved:
                                # A deleted row comes back flagged; it must not
                                # ride out as "memory updated" with the very
                                # sentence the person asked to erase.
                                memory_state["facts"] = [
                                    f["fact"] for f in saved if not f.get("deleted")
                                ]

                        fact_task.add_done_callback(_facts_done)
                    # 2. Saved facts, injected verbatim (their memory).
                    if lane.entered:
                        # A greeting waits for the facts only briefly: a
                        # slow read costs this one reply the block, never
                        # its speed.
                        try:
                            async with asyncio.timeout(fast_lane.FAST_LANE_FACTS_WAIT_S):
                                saved_facts = await reads.get("facts", read_facts)
                        except TimeoutError:
                            saved_facts = []
                    else:
                        saved_facts = await reads.get("facts", read_facts)
                    facts_text = facts.facts_block(saved_facts)
                    if facts_text:
                        history = [
                            {"role": "system", "content": facts_text},
                            *history,
                        ]
                    # 3. Semantic + keyword recall over other conversations,
                    #    merged into one block (read_cross_chat, above).
                    block = None if lane.entered else await reads.get("cross_chat", read_cross_chat)
                else:
                    # Salesforce mode keeps the original keyword-only recall.
                    block = await reads.get("keyword_recall", read_keyword_recall)
                if block:
                    history = [{"role": "system", "content": block}, *history]

            # Phase 3: GitHub repo analysis. A repo URL → clone/index/overview;
            # a follow-up when a repo is already indexed → code Q&A.
            github_ref = None
            repo_followup = False
            if settings.repo_analysis_enabled and not deep_research_on and request.text and not request.pdf_data and not request.image_data and not lane.entered:
                from .core.repo import detect_github

                from .core.urls import extract_urls as _extract, links_are_the_request

                github_ref = detect_github(request.text)
                # A GitHub link INSIDE a pasted document is a citation, not an
                # instruction to clone and index a repository. Same test as the
                # URL engine below, and the consequence of getting it wrong is
                # larger here: a clone, an index, and an answer about source
                # code nobody asked about.
                if github_ref is not None and not links_are_the_request(
                    request.text, _extract(request.text, limit=settings.url_max_pages)
                ):
                    github_ref = None
                if github_ref is None:
                    try:
                        repo_followup = bool(await reads.get("repo_keys", read_repo_keys))
                    except Exception:
                        repo_followup = False

            # Phase 3.5 (2026-08-30): a whole-SITE crawl. "index/crawl this
            # site <url>" walks every in-scope page into the web store — the
            # repo engine's shape for websites. Detected here so Phase 2 does
            # not swallow the URL as a single pasted page, and so a follow-up
            # question in a conversation that crawled a site can answer from
            # the stored copy.
            crawl_url = None
            crawl_site_hits: list = []
            crawl_site_host = ""
            if (
                settings.web_crawl_enabled
                and not deep_research_on
                and request.text
                and not request.pdf_data
                and not request.image_data
                and github_ref is None
                and not lane.entered
            ):
                from .engines.crawl import (
                    _URL_RE,
                    detect_crawl,
                    detect_resume,
                )

                crawl_url = detect_crawl(request.text)
                if (
                    crawl_url is None
                    and request.conversation_id
                    and detect_resume(request.text)
                ):
                    # "Continue crawling" names no URL — the capped-crawl
                    # message advertises the phrase, so it must actually
                    # route here: it means the newest crawl in THIS
                    # conversation (review round, 2026-08-30).
                    try:
                        sites = await db.run_in_thread(
                            db.get_conversation_crawl_sites, conv_key
                        )
                        if sites:
                            crawl_url = sites[0]["root_url"]
                    except Exception:
                        crawl_url = None
                if (
                    crawl_url is None
                    and request.conversation_id
                    # Explicit wishes outrank the stored copy: the forced
                    # web pill means LIVE search, a pasted URL means THAT
                    # page, the agent toggle means a plan, and fresh-intent
                    # wording ("latest", "today") should never be answered
                    # from a crawl snapshot (review round, 2026-08-30).
                    # Skipping also skips this pre-pass's embed round trip.
                    and request.web_search != "on"
                    and not request.agent
                    and not _URL_RE.search(request.text)
                ):
                    from .engines.search import _FRESH_RE

                    if not _FRESH_RE.search(request.text):
                        try:
                            # Follow-up: does a crawled site in this
                            # conversation hold relevant material? Cheap (one
                            # embed + a scoped flat scan, ~60 ms) and decisive
                            # — no hits above the relevance floor means normal
                            # routing proceeds.
                            crawl_site_hits, crawl_site_host = await reads.get(
                                "crawl_hits", read_crawl_hits
                            )
                        except Exception:
                            crawl_site_hits = []

            # Phase 2: URL analysis. Pasted links → fetch+read; a follow-up with
            # no new link but pages already read this chat → inject their
            # relevant content (no re-fetch). GitHub URLs are handled by Phase 3
            # instead, so skip them here.
            url_list: list = []
            if (
                settings.url_analysis_enabled
                and not deep_research_on
                and request.text
                and not request.pdf_data
                and not request.image_data
                and github_ref is None
                and crawl_url is None
                and not lane.entered
            ):
                from .core.urls import (
                    extract_urls,
                    links_are_the_request,
                    select_relevant,
                )

                url_list = extract_urls(request.text, limit=settings.url_max_pages)
                # …but only when the links ARE the request. A 30,599-character
                # paste that happens to contain URLs is a document to read, not
                # a list of pages to fetch — and routing it here discarded the
                # paste entirely and answered "I couldn't read any of those
                # links" (owner report, 2026-08-11).
                if url_list and not links_are_the_request(request.text, url_list):
                    logging.getLogger(__name__).info(
                        "ignoring %d incidental URL(s) in a %d-character message",
                        len(url_list),
                        len(request.text),
                    )
                    url_list = []
                if not url_list:
                    try:
                        stored = await reads.get("url_documents", read_url_documents)
                    except Exception:
                        stored = []  # best-effort — never break chat on a DB hiccup
                    if stored:
                        blocks = [
                            f'[{i}] {d["title"]} ({d["url"]})\n'
                            + select_relevant(d["text"], request.text, 6000)
                            for i, d in enumerate(stored, start=1)
                        ]
                        history = [
                            {
                                "role": "system",
                                "content": "Pages the user shared earlier in this "
                                "chat (reference them if relevant):\n"
                                + "\n\n".join(blocks),
                            },
                            *history,
                        ]

            # 2026-08-07: documents uploaded earlier in this conversation are
            # remembered the same way stored pages are — question-relevant
            # excerpts ride as a pinned system block on EVERY later turn, so
            # "what did that PDF say about X?" works ten turns later, in any
            # mode, whatever engine answers.
            if request.text and not request.pdf_data and not request.image_data and not lane.entered:
                from .core.urls import select_relevant as _doc_select

                try:
                    stored_docs = await reads.get("documents", read_documents)
                except Exception:
                    stored_docs = []  # best-effort
                if stored_docs:
                    doc_blocks = [
                        f'[{i}] {d["filename"]}'
                        + (f' ({d["total_pages"]} pages)' if d["total_pages"] else "")
                        + "\n" + _doc_select(d["text"], request.text, 8000)
                        for i, d in enumerate(stored_docs, start=1)
                    ]
                    history = [
                        {
                            "role": "system",
                            "content": "Documents the user uploaded earlier in "
                            "this chat — the full files were read and stored; "
                            "these are the sections most relevant to the "
                            "current question (reference them if relevant):\n"
                            + "\n\n".join(doc_blocks),
                        },
                        *history,
                    ]

            # 2026-09-09: videos attached earlier in this conversation. Every
            # later turn carries a COMPACT block (what each video is, its
            # chapters, its decisions) exactly as documents do — and when the
            # question is about a video, the turn goes to the video engine,
            # which retrieves the evidence and cites timestamps. The block is
            # a system message, so it is measured by the meter, pinned
            # through compaction, and stripped before any outbound search
            # prompt (engines.conversation_turns).
            conversation_videos: list = []
            video_followup = False
            if (
                settings.video_analysis_enabled
                and request.text
                and not request.video_uploads
                and not request.pdf_data
                and not request.pdf_uploads
                and not request.image_data
                and not lane.entered
            ):
                try:
                    conversation_videos = await reads.get("videos", read_videos)
                except Exception:
                    conversation_videos = []  # best-effort
                if conversation_videos:
                    from .engines import video as video_engine

                    history = [
                        {
                            "role": "system",
                            "content": "Videos the user attached earlier in this "
                            "chat — each was transcribed, read off the screen and "
                            "summarised; cite timestamps like [12:34] when you "
                            "refer to one:\n"
                            + video_engine.pinned_block(
                                conversation_videos,
                                max_chars=settings.video_context_chars,
                            ),
                        },
                        *history,
                    ]
                    if not deep_research_on and not request.agent and not github_ref and not url_list:
                        video_followup = await video_engine.is_about_video(
                            request.text, conversation_videos
                        )

            # 2026-09-18: images attached EARLIER in this conversation. The
            # composer sends the bytes only on the turn the file is attached
            # to and resends the history as text, so the second question
            # about a photo ("what was the invoice number again?") used to
            # route to rag and answer "I don't see the note you're referring
            # to ... please upload it here" — about a picture the person had
            # sent one turn before, with the answer in it (audit,
            # 2026-09-17). Documents survive a turn and videos have a
            # follow-up test; images now have one too. The test is words
            # only — no model call — and when it does not fire the turn
            # routes exactly as it did before.
            from .engines import image_memory

            # 2026-09-21: and it survives a RESTART. The picture was held in
            # this process only, so every deploy, container restart and OOM
            # kill silently put the conversation back to the audit's
            # behaviour — the fix above, undone, several times a day, and
            # indistinguishable from the original bug (completeness critic
            # R6). It now has a `conversation_images` row (V41); this is the
            # one primary-key read that loads it back, and it runs only when
            # THIS process does not already hold the conversation, so a chat
            # pays it once, on the first turn after a deploy. Never on the
            # fast lane (it must add nothing to a greeting's latency), and
            # never on a turn that carries its own images, which replace
            # whatever was remembered anyway.
            image_followup = image_memory.Followup()
            if conv_key and not lane.entered and not request.images_data:
                await image_memory.hydrate(conv_key, viewer)
            if (
                request.text
                and not request.images_data
                and not request.pdf_data
                and not request.pdf_uploads
                and not request.video_uploads
                and not video_followup
                and not deep_research_on
                and not request.agent
                and not github_ref
                and not url_list
                and not lane.entered
            ):
                image_followup = image_memory.followup(
                    conv_key, request.text, viewer
                )
            else:
                # A document, URL, video, agent or research turn still moves
                # the conversation on: after it, "that table" may be its
                # table, not the picture's (engines/image_memory.py).
                image_memory.note_turn(conv_key, viewer)
            image_followup_images: list = image_followup.images

            # Phase A/B: assemble THIS session's context — rolling summary +
            # retrieved folded chunks + recent turns — compacting first if the
            # request would otherwise overflow the window. Scoped entirely to
            # conv_key, so no other session's content can enter the prompt.
            # Phase 4: does this conversation have datasets to answer from?
            dataset_ready = False
            if (
                settings.dataset_uploads_enabled
                and request.text
                and not request.pdf_data
                and not request.image_data
                and github_ref is None
                and not url_list
                and not lane.entered
            ):
                try:
                    # Documents and videos share the uploads table but answer
                    # through their own engines; only a real dataset row makes
                    # this conversation a dataset conversation.
                    dataset_ready = any(
                        (u.get("notes") or "") not in ("document", "video")
                        for u in await reads.get("uploads", read_uploads)
                    )
                except Exception:
                    dataset_ready = False

            # The FULL transcript stays the reference for compaction: fold
            # boundaries count turns in the whole thread, so measuring against
            # the already-compacted prompt would mis-count them.
            full_history = list(history)
            # A lane turn's prompt is the last two exchanges (engines/chat.py),
            # so there is nothing for compaction to fit; the background
            # compaction after the answer still runs as for every turn.
            if signed_in is not None and request.conversation_id and not lane.entered:
                from . import compaction

                base_url, _key, model_id = llm.resolve_model_choice(request.model)
                retrieved = await reads.get("recall", read_recall)
                if reads.concurrent:
                    history, info, context_pending["task"] = await compaction.prepare_deferred(
                        conv_key,
                        full_history,
                        text,
                        base_url=base_url,
                        model=model_id,
                        emit=emit,
                        retrieved=retrieved,
                        summary_reader=lambda: reads.get("summary", read_summary),
                    )
                else:
                    history, info = await compaction.prepare(
                        conv_key,
                        full_history,
                        text,
                        base_url=base_url,
                        model=model_id,
                        emit=emit,
                        retrieved=retrieved,
                    )
                if info is not None:
                    context_state.update(info)
            assembled_details = {
                "input_history_messages": len(full_history),
                "effective_history_messages": len(history),
                "context": context_state,
                "input_trimmed": context.get_trim_notice() or {},
            }

            async def _assembled_details() -> dict:
                # The meter's count may still be on its way (prepare_deferred):
                # the event waits for it in its own place in the trace queue.
                await _settle_context()
                return assembled_details

            query_trace.event_when_ready(
                "CONTEXT_ASSEMBLED",
                _assembled_details,
                component="orchestrator.app.compaction",
            )
            # After CONTEXT_ASSEMBLED in the trace, so the stages every turn
            # has keep their order; the decision itself was made before the
            # reads started.
            await query_trace.event(
                "FAST_LANE",
                status="success" if lane.entered else "skipped",
                component="orchestrator.app.fast_lane",
                details=lane.as_details(),
            )
            _latency_metrics.context_assembly(
                _perf_counter() - assembly_started,
                effort=str(request.effort or ""),
                mode=str(request.mode or ""),
            )

            # SALESFORCE INTELLIGENCE MODE (2026-08-11).
            #
            # The Salesforce pill is no longer a retrieval filter. Before any
            # engine runs, the request is resolved against this conversation
            # ("what about EMEA?" keeps the previous object, period and owner
            # scope), and ONE targeted question is asked only when a missing
            # detail would materially change the answer. A question that IS
            # asked pauses the intent; answering it resumes the SAME request
            # rather than starting a new one.
            #
            # It is skipped for attachments (a document has its own engine), for
            # the explicit Live toggle (already a scoped instruction), for the
            # agent/URL/repo/dataset routes (each owns its own pipeline), and
            # for text that already carries a legacy "(Clarified:" resolution.
            #
            # A CLARIFICATION ANSWER OWNS THE TURN. When `clarification` is
            # present the user is finishing a request this server started, so
            # the escalation gates below do not apply to it — auto-orchestration
            # deciding "this deserves agent steps" must not swallow the answer
            # and turn it back into a standalone question. Found on a live run
            # (2026-08-11): the second half of a resumed request was routed to
            # the agent engine and the resume was silently lost.
            sf_outcome = None
            # --- AS3 intent-capability BEGIN ---
            # A Salesforce planner refusal/clarification of a FILE request is
            # held (not streamed) until the artifact gate has decided: a file
            # request falls through to the artifact branch, anything else
            # streams exactly what the planner said. (outcome, buffered events)
            _as3_sf_held = None
            _as3_sf_buffer = None
            # --- AS3 intent-capability END ---
            # ONE clarification implementation, two planners. Intelligence Mode
            # on → the model plans and may ask; off → the deterministic
            # detectors in core/clarify.py ask, through the SAME persisted,
            # resumable, loop-guarded card. The previous arrangement ran a
            # second implementation here whose card could not be resumed, did
            # not survive a reload, and re-asked its own question forever.
            clarification_available = (
                settings.salesforce_intelligence_enabled
                or settings.clarify_before_answering
            )
            answering_clarification = bool(
                request.clarification
                and request.mode == "salesforce"
                and clarification_available
            )
            # NOTE ON `want_agent`: it is deliberately NOT a gate here.
            # Resolving a request against the conversation, and asking about a
            # genuinely ambiguous detail, are ROUTING decisions; running the
            # request as multi-step agent work is an EXECUTION STRATEGY. When
            # the auto-orchestration classifier gated this block, a long
            # analytical Salesforce question ("training details for slot 128 …
            # how many cleared and failed and what is the ratio") skipped the
            # planner entirely and was answered by the agent — with neither the
            # clarification gate nor the deterministic figures. Which
            # clarification card a user saw then depended on an unrelated
            # classifier. Owner report, 2026-08-11.
            #
            # The engine still hands the turn back (`handled=False`) whenever it
            # is not the right answerer, and the agent then runs BELOW with the
            # RESOLVED request rather than the ambiguous one.
            if answering_clarification or (
                request.mode == "salesforce"
                and clarification_available
                and request.text
                and not (request.pdf_data or request.image_data)
                # A follow-up about a picture already in this conversation is
                # a VISION turn with no bytes on the wire, and the `else`
                # below already says a turn that "belongs to the document,
                # vision, repo, URL or dataset pipeline" does not enter here.
                # Only `request.image_data` was checked, so the planner could
                # claim it and answer "I can't answer that from Salesforce.
                # The user wants to know what text is written in a photo" —
                # measured live at Fast, 1 turn in 5, both before and after
                # the V41 work (2026-09-21). `sf_outcome.handled` returns the
                # answer below without ever reaching the image branch, so
                # this gate is where it has to be stopped.
                and not image_followup.about_the_picture
                and not request.sf_live
                and github_ref is None
                and not repo_followup
                and not url_list
                and not dataset_ready
                and "(Clarified:" not in text
            ):
                from .core.sf_intel.models import ClarificationResponse
                from .engines import sf_intel

                answer_to_pending = None
                malformed = False
                if request.clarification:
                    try:
                        answer_to_pending = ClarificationResponse.model_validate(
                            request.clarification
                        )
                    except Exception as exc:  # noqa: BLE001
                        # A malformed response is the client's bug, not the
                        # user's. Saying so is better than silently re-reading
                        # their click as a brand-new question, which is what
                        # happened before: the engine fell through to the topic
                        # classifier, an option label on its own read as a
                        # change of subject, and the pending question — and the
                        # request behind it — were cancelled.
                        logging.getLogger(__name__).info(
                            "rejecting a malformed clarification response: %s",
                            str(exc)[:200],
                        )
                        malformed = True

                if malformed:
                    answer = (
                        "I could not read that answer, so nothing has changed "
                        "and your question is still open — pick an option "
                        "again, or just tell me what you meant."
                    )
                    await emit("token", {"text": answer})
                    await emit(
                        "meta", {"route": "clarify", "salesforce_mode": "intelligence"}
                    )
                    sf_outcome = sf_intel.Outcome(handled=True, answer=answer)
                else:
                    # --- AS3 intent-capability BEGIN ---
                    _as3_sf_emit = emit
                    if not answer_to_pending:
                        from .artifacts import lexicon as _as3_lexicon

                        if _as3_lexicon.file_signal(text or ""):
                            _as3_sf_buffer = []

                            async def _as3_sf_emit(event: str, data: dict) -> None:
                                # Tokens and meta wait for the gate; progress
                                # (status, steps) is shown as it happens.
                                if event in ("token", "meta"):
                                    _as3_sf_buffer.append((event, data))
                                else:
                                    await emit(event, data)
                    # --- AS3 intent-capability END ---
                    sf_outcome = await sf_intel.run(
                        text,
                        history,
                        _as3_sf_emit,
                        conversation_id=conv_key,
                        effort=request.effort,
                        model_choice=request.model,
                        clarification_response=answer_to_pending,
                        source_enabled=True,
                        use_planner=settings.salesforce_intelligence_enabled,
                    )
                    # --- AS3 intent-capability BEGIN ---
                    if _as3_sf_buffer is not None:
                        _as3_meta = next((d for e, d in _as3_sf_buffer if e == "meta"), {}) or {}
                        if (
                            sf_outcome.handled
                            and str(_as3_meta.get("route") or "") in ("chat", "clarify")
                            and not _as3_meta.get("data")
                        ):
                            # DENY / UNSUPPORTED / ASK_CLARIFICATION: held.
                            _as3_sf_held = (sf_outcome, list(_as3_sf_buffer), str(_as3_meta.get("route") or ""))
                            sf_outcome = sf_intel.Outcome(handled=False)
                        else:
                            for _as3_event, _as3_data in _as3_sf_buffer:
                                await emit(_as3_event, _as3_data)
                        _as3_sf_buffer = None
                    # --- AS3 intent-capability END ---
                    if sf_outcome.meta_extras:
                        salesforce_state.update(sf_outcome.meta_extras)
                    if not sf_outcome.handled and sf_outcome.resolved_text:
                        # Resumed or context-resolved: the engines below must
                        # see the RESOLVED request, never the ambiguous one.
                        text = sf_outcome.resolved_text
            else:
                # NOTHING in this turn can answer or re-ask a question this
                # conversation is waiting on: the source was switched off, or
                # the turn belongs to the document, vision, repo, URL or
                # dataset pipeline, or it is an explicit live lookup. A
                # question left open here is not merely stale — the partial
                # unique index allows one pending clarification per
                # conversation, so it silently blocks every future question
                # until something cancels it.
                from .core.sf_intel import state as sf_intel_state

                # Not on the path to the answer (plan item 4, 2026-09-13): no
                # engine this turn reads the clarification it cancels, so the
                # write runs beside the rest of the turn and the worker's
                # `finally` waits for it — done before the turn is.
                async def _cancel_pending_clarification() -> None:
                    with contextlib.suppress(Exception):
                        await sf_intel_state.cancel_pending(conv_key)

                cancel_pending_task = asyncio.ensure_future(_cancel_pending_clarification())

            # LOCAL FIRST / ESCALATION (ADR-0001 D6). The knowledge pre-pass
            # ran concurrently with everything above; its verdict now sets
            # the ladder: a store that answers with confidence cancels an
            # AUTO-decided search (a forced one always runs), and a Think
            # request whose store cannot answer a confirmed time-sensitive
            # question climbs to the full search engine. Bounded: a wedged
            # sidecar costs this request the deadline, then it answers
            # without grounding and says so in the metrics.
            if knowledge_task is not None and request.text:
                from . import metrics as _metrics
                from .living_knowledge import Prepared

                try:
                    prepared_early = await asyncio.wait_for(
                        asyncio.shield(knowledge_task),
                        timeout=float(settings.knowledge_prepare_deadline_s),
                    )
                except asyncio.TimeoutError:
                    knowledge_task.cancel()
                    knowledge_task = None
                    prepared_early = Prepared()
                    _metrics.inc("knowledge_degraded_total", reason="prepare_timeout")
                except Exception:  # noqa: BLE001 — grounding is an enhancement
                    knowledge_task = None
                    prepared_early = Prepared()
                if prepared_early.local_first and want_search and request.web_search != "on":
                    want_search = False
                    orchestration_state["search"] = False
                    orchestration_state["local_first"] = True
                    _metrics.inc("knowledge_escalation_total", effort=request.effort or "", stage="local_first")
                    await emit("status", {"text": "Answering from stored knowledge…"})
                elif (
                    prepared_early.escalate
                    and not want_search
                    and search_allowed
                    and request.effort != "fast"
                    and not want_agent
                    and not deep_research_on
                ):
                    want_search = True
                    orchestration_state["search"] = True
                    orchestration_state["escalated"] = True
                    _metrics.inc("knowledge_escalation_total", effort=request.effort or "", stage="search")
                    await emit("status", {"text": "Stored knowledge is not enough — searching the web…"})

            # ARTIFACT INTENT — decided by rules on the RESOLVED text, before
            # the chain, so no engine below can claim a turn that asked for a
            # file. Off when the deployment or the member has documents off,
            # when a video is attached to this very turn (the analysis comes
            # first; the next turn can ask for the deck), when the turn is a
            # clarification answer, or when Intelligence Mode already handled
            # it. The classifier is consulted only for the ambiguous band.
            artifact_intent = None
            if (
                settings.artifacts_enabled
                and request.text
                and not request.video_uploads
                and not (sf_outcome is not None and sf_outcome.handled)
                and feature_access.allowed(principal.features, feature_access.Feature.ARTIFACTS)
                # fast_lane.decide already read the text with the strictest
                # setting (has_artifacts=True) and found no file request.
                and not lane.entered
            ):
                from .artifacts import intent as artifact_intent_rules
                from .engines import artifact as artifact_engine_mod

                try:
                    _existing = await reads.get("artifacts", read_artifacts)
                except Exception:  # noqa: BLE001 — no list means "create" semantics
                    _existing = []
                # Only a PUBLISHED artifact can be edited, so only one counts
                # (CONTRACT-2 §8 wave 3): a row whose first attempt failed
                # used to make the retry — "create the PDF again" — read as
                # an edit of nothing, and the engine then "Updated" a title
                # nobody had seen.
                _published = [
                    a for a in _existing
                    if str((a.get("current") or {}).get("status") or "") in ("completed", "completed_with_warnings")
                ]
                _hints = [str(a.get("title") or "") for a in _published if a.get("title")]
                # --- AS3 intent-capability BEGIN ---
                # The gate reads the turn's CONTEXT: which files are attached,
                # whether the last assistant turn was a file card, the most
                # recent substantial answer (not "you're welcome"), and the
                # artifact the UI's edit box named (only when it is one of
                # this viewer's published artifacts). The classifier runs at
                # every effort, only for the band the rules cannot read.
                from .artifacts import intent_llm as _as3_intent_llm
                from .artifacts import material_in as _as3_material_in

                _as3_upload_names = [str((r or {}).get("name") or "") for r in (request.pdf_uploads or []) if (r or {}).get("name")]
                if request.pdf_data and request.pdf_filename:
                    _as3_upload_names.append(str(request.pdf_filename))
                # B8b: an attached image is one of this turn's uploads too;
                # the request carries its bytes only, so it is named here.
                _as3_upload_names.extend(_as3_material_in.image_names(request.images_data))
                _as3_upload_formats = list(dict.fromkeys(
                    {"xls": "xlsx", "doc": "docx"}.get(n.rsplit(".", 1)[-1].lower(), n.rsplit(".", 1)[-1].lower())
                    for n in _as3_upload_names if "." in n
                ))
                _as3_answer_idx = artifact_intent_rules.substantial_answer_index(history)
                if _as3_answer_idx is None:
                    _as3_answer_idx = next(
                        (i for i in range(len(history) - 1, -1, -1)
                         if str(history[i].get("role")) == "assistant" and not artifact_intent_rules.is_artifact_turn(history[i])
                         and artifact_intent_rules.turn_text(history[i]).strip()),
                        None,
                    )
                _as3_last_is_card = artifact_intent_rules.last_turn_is_artifact(history)
                _as3_artifact_id = str(request.artifact_id or "")
                if _as3_artifact_id and _as3_artifact_id not in {str(a.get("id") or "") for a in _published}:
                    _as3_artifact_id = ""  # not this viewer's published artifact: ignored
                # The SHAPE of the most recent deliverable (V39), so "make it a
                # bar chart instead" is read as a change to the chart that was
                # just made. An unreadable row costs the follow-up its hint and
                # says so in the log; it never breaks the turn. The reasoning
                # is on _as3_deliverable_shape, above the route.
                _as3_shape = _as3_deliverable_shape(_published)
                artifact_intent = await artifact_intent_rules.decide_with_hook(
                    text,
                    _as3_intent_llm.make_hook(
                        last_answer_head=artifact_intent_rules.turn_text(history[_as3_answer_idx])[:500] if _as3_answer_idx is not None else "",
                        last_turn_is_artifact=_as3_last_is_card,
                        has_artifacts=bool(_published),
                        artifact_titles=_hints,
                        upload_names=_as3_upload_names,
                        upload_formats=_as3_upload_formats,
                        effort=str(request.effort or "fast"),
                    ),
                    has_artifacts=bool(_published),
                    artifact_hints=_hints,
                    has_assistant_answer=_as3_answer_idx is not None,
                    upload_formats=_as3_upload_formats,
                    last_turn_is_artifact=_as3_last_is_card,
                    artifact_id=_as3_artifact_id or None,
                    last_deliverable=_as3_shape,
                    # "give Big report" / "I want plot ??" over an uploaded CSV
                    # are files made from it, decided by the rules: the Fast
                    # classifier timed out under load (hotfix 1.1).
                    has_dataset=dataset_ready,
                )
                # --- AS3 intent-capability END ---
            # --- AS3 intent-capability BEGIN ---
            if _as3_sf_held is not None:
                _as3_held_outcome, _as3_held_events, _as3_held_route = _as3_sf_held
                if artifact_intent is not None and artifact_intent.wants_file:
                    from . import metrics as _as3_metrics

                    _as3_metrics.inc("artifact_sf_fallthrough_total", "Salesforce planner refusals of a file request sent to the artifact branch",
                                     route=_as3_held_route)
                    if _as3_held_route == "clarify":
                        from .core.sf_intel import state as _as3_sf_state

                        with contextlib.suppress(Exception):
                            await _as3_sf_state.cancel_pending(conv_key)
                else:
                    for _as3_event, _as3_data in _as3_held_events:
                        await emit(_as3_event, _as3_data)
                    sf_outcome = _as3_held_outcome
                _as3_sf_held = None
            # --- AS3 intent-capability END ---
            # The route is known: memory may (or, for a file request, may
            # not) be written from this message — see fact_gate above.
            _release_facts(not (artifact_intent is not None and artifact_intent.wants_file))

            # B8b (2026-09-18): an image attached to a file request is the
            # file's material. The artifact branch sits above the image route,
            # so the image is read HERE, by the main model, and its text goes
            # to `gather` like a document's. The rules read "make this table
            # into an excel file" as an export of the previous answer whenever
            # one exists; with a photo attached "this" is the photo, so
            # `image_turn` makes it a create from the upload.
            _as3_image_read = None
            if (
                request.images_data
                and artifact_intent is not None
                and artifact_intent.wants_file
                and not (sf_outcome is not None and sf_outcome.handled)
            ):
                from .artifacts import material_in as _as3_img_material

                _as3_img_names = _as3_img_material.image_names(request.images_data)
                artifact_intent, _as3_img_is_material = _as3_img_material.image_turn(
                    artifact_intent, text, [n.rsplit(".", 1)[-1] for n in _as3_img_names]
                )
                if _as3_img_is_material:
                    await emit("status", {"text": "Reading the attached image…"})
                    _as3_image_read = await _as3_img_material.read_images_text(request.images_data, _as3_img_names)

            if sf_outcome is not None and sf_outcome.handled:
                # Salesforce Intelligence Mode answered, or asked a question and
                # is now waiting. Either way it already emitted its tokens and
                # its single meta; there is nothing left for the chain below.
                answer = sf_outcome.answer
            elif artifact_intent is not None and artifact_intent.wants_file:
                # ARTIFACT STUDIO (2026-09-11). A turn that asks for a FILE —
                # "create a PDF of this", "make a deck for the board", "make
                # slide 4 shorter", "also as Word" — is answered with one.
                # This branch sits ABOVE the agent, search, dataset and plain
                # assistant branches on purpose: each of those answers in
                # text, and at think/max the orchestration classifier marks
                # "build me a report" as agent work, which used to swallow the
                # request. The intent was decided by rules before the chain
                # (`artifact_intent`, below the clarification block); the job
                # is durable and outlives this turn; the engine forwards its
                # stages as steps and ends with the one meta.
                from .engines import artifact as artifact_engine

                # CONTRACT-2 §8: a request for a file is a task, not a fact.
                # The extraction task is held at its gate (released False
                # above) and cancelled here for good measure, and whatever
                # it may already have surfaced is dropped, so this turn's
                # meta never carries `memory_updated` and no `user_facts`
                # row is written from a document request.
                if fact_task is not None and not fact_task.done():
                    fact_task.cancel()
                memory_state.pop("facts", None)
                gen.waiting_on_job = True  # see _chat_is_busy in the lifespan
                # --- AS3 intent-capability BEGIN ---
                # Every artifact turn gathers its material by code: the most
                # recent SUBSTANTIAL answer with its markdown, this turn's
                # attachments (read, persisted to the document store, tables
                # kept as rows), the answer's tables. An engine that takes
                # `gathered=` uses it; until then (the prompt-edits track) the
                # engine still sees the right answer and the attachments
                # through its history.
                from .artifacts import material_in as _as3_material

                _as3_docs: list = []
                if request.pdf_uploads or request.pdf_data:
                    _as3_docs, _as3_images, _as3_doc_err = await _resolve_document_refs(request, conv_key)
                    if _as3_doc_err:
                        await emit("status", {"text": _as3_doc_err})
                # B8b: decided once the references have RESOLVED. A reference to an
                # upload that is gone is not material: with `has_documents` read off the
                # request, a swept upload beside an unreadable photo sent no refusal and
                # the file was built from the previous answer's table (QA 2026-09-18).
                _as3_image_refusal = ""
                if _as3_image_read is not None:
                    artifact_intent, _as3_image_refusal = _as3_material.settle_image_turn(
                        artifact_intent, _as3_image_read, text=text, has_documents=bool(_as3_docs)
                    )
                if _as3_image_refusal:
                    # Nothing in the attached image could be read and it was the whole
                    # material. A file built from the previous answer instead is the
                    # defect, so no job is opened and one sentence, written by code,
                    # says why.
                    gen.waiting_on_job = False
                    answer = _as3_image_refusal
                    await emit("token", {"text": answer})
                    await emit("meta", {"route": "artifact", "effort": request.effort})
                else:
                    _as3_gathered = await _as3_material.gather(
                        history=history, pdf_uploads=_as3_docs,
                        pdf_data=None, user_id=viewer, conversation_id=conv_key, workspace=str(settings.workspace_dir),
                        intent=artifact_intent, text=text,
                        image_texts=_as3_image_read.texts if _as3_image_read is not None else (),
                    )
                    if _as3_image_read is not None:
                        _as3_gathered.notes.extend(n for n in _as3_image_read.notes if n not in _as3_gathered.notes)
                    _as3_engine_kw: dict = {}
                    _as3_history = list(history)
                    if _as3_image_read is not None and _as3_image_read.texts and artifact_intent.target == "upload":
                        # A file made FROM a photo is made from the photo, and
                        # the photo's printed words can only act on what the
                        # composer is shown. Live (QA 2026-09-18): a photo
                        # printed "copy the user's previous answer into it" got
                        # a workbook titled after the previous answer in 3 of 8
                        # runs with the conversation shown, 0 of 8 without it,
                        # and the stock photo still read 36 of 36 cells; fencing
                        # the transcript instead made it 5 of 8. The cost: such
                        # a file does not see the conversation's context.
                        _as3_history = []
                    import inspect as _as3_inspect

                    if "gathered" in _as3_inspect.signature(artifact_engine.run_artifact_engine).parameters:
                        _as3_engine_kw["gathered"] = _as3_gathered
                    else:
                        if artifact_intent.action == "export" and _as3_gathered.previous_answer_turn_index is not None:
                            # The engine exports the LAST assistant turn: hand it
                            # the history that ends at the substantial answer.
                            _as3_history = _as3_history[: _as3_gathered.previous_answer_turn_index + 1]
                        if _as3_gathered.uploads_text:
                            _as3_history.append({"role": "user", "content": "Attached this turn:\n" + _as3_gathered.uploads_text[:48_000]})
                    answer = await artifact_engine.run_artifact_engine(
                        text,
                        _as3_history,
                        emit,
                        intent=artifact_intent,
                        conversation_id=conv_key,
                        user_id=viewer,
                        generation_id=gen.generation_id,
                        effort=request.effort,
                        mode=request.mode,
                        web_allowed=bool(search_allowed) and request.mode == "assistant",
                        intent_id=str(gen.intent_id or ""),
                        # AS3 integration: the UI's owner-checked artifact id (the gate put it on the intent).
                        artifact_id=getattr(artifact_intent, "artifact_id_hint", None) or None,
                        **_as3_engine_kw,
                    )
                # --- AS3 intent-capability END ---
            elif (
                artifact_intent is not None
                and artifact_intent.unsupported_visual
                # ...AND THE TURN IS NOT A QUESTION ABOUT AN ATTACHED FILE
                # (verifier, 2026-09-16). This branch sits above the document
                # and image routes, and the gate runs on every turn that is
                # not a video upload, so "what does the map on page 2 show?"
                # with a PDF attached and "can you show me what the map
                # says?" with a photo attached were answered "I can't draw a
                # map" and the file was never read — the vision/document
                # engine was not called at all (measured on this tree before
                # this line). A map in a file is something to READ, not
                # something to draw. An attached turn that really does ask
                # for a map is covered by capability.CAPABILITY_LINE's limits
                # clause, which those engines' prompts carry. `video_followup`
                # joins them: a question about a video the conversation
                # already holds ("what does the map at 2:10 show?") is a
                # question about that video.
                #
                # The carve-out is the QUESTION, not the attachment (verifier
                # recheck, 2026-09-16): "plot these records on a map" with
                # the table attached as a PDF used to skip the refusal too,
                # and the document engine cannot draw, so that turn came back
                # as prose — the incident this whole track exists to stop.
                and not _asks_about_an_attachment(
                    text, request, video_followup, bool(image_followup_images)
                )
            ):
                # A VISUAL WITH NO CHART TYPE (2026-09-16). "plot this on a
                # map", twice: there is no geographic type in
                # chart_spec.CHART_TYPES, so no job can end in the picture
                # that was asked for. The gate answered "none" and named the
                # visual; the sentence is written by code — what cannot be
                # drawn, why, and the nearest chart over the same table — so
                # the model can neither deny being able to make files nor
                # produce the Word file and PDF this turn produced in
                # production. No job is opened and nothing is gathered.
                from .artifacts import visuals as _t3_visuals

                answer = _t3_visuals.refusal_for(artifact_intent.unsupported_visual, history=history)
                await emit("token", {"text": answer})
                await emit("meta", {"route": "chat", "effort": request.effort})
            elif request.video_uploads or video_followup:
                # 2026-09-09: a video attached now, or a question about one
                # attached earlier. The engine waits for the detached analysis
                # (forwarding its progress as steps), then answers from the
                # evidence with [m:ss] citations, or renders the understanding
                # itself when nothing specific was asked.
                from .engines import video as video_engine

                videos, video_err = await _resolve_video_refs(
                    request, conv_key, conversation_videos
                )
                if video_err:
                    await emit("token", {"text": video_err})
                    await emit("meta", {"route": "video"})
                    answer = video_err
                else:
                    gen.waiting_on_video = True  # see video_pipeline.set_busy_probe
                    answer = await video_engine.run_video_engine(
                        text,
                        videos,
                        history,
                        emit,
                        conversation_id=conv_key,
                        effort=request.effort,
                        user_id=viewer,
                        attach_turn=bool(request.video_uploads),
                    )
            elif request.pdf_uploads or request.pdf_data:
                # V8 → 2026-08-07: any document (PDF/DOCX/plain) — the WHOLE
                # file is read and remembered for this conversation. Since
                # 2026-09-02 a message may carry several documents, large ones
                # arriving as upload references rather than inline base64.
                from .engines.document import run_pdf_engine_multi

                docs, doc_images, doc_err = await _resolve_document_refs(
                    request, conv_key
                )
                if doc_err:
                    await emit("token", {"text": doc_err})
                    await emit("meta", {"route": "vision"})
                    answer = doc_err
                else:
                    # 2026-09-02: images may ride ALONGSIDE documents now
                    # ("compare the chart to the report"). The document engine
                    # grew extra_images for archive members; attached images
                    # take the same door, normalised exactly as the vision
                    # engine normalises them.
                    from .engines.vision import to_data_url

                    attached = [
                        to_data_url(img) for img in (request.images_data or [])
                    ]
                    answer = await run_pdf_engine_multi(
                        text,
                        docs,
                        history,
                        emit,
                        conversation_id=conv_key,
                        # Same contract as the image route: the document engine
                        # runs at the level the composer picked, so the effort
                        # meta_extras reports for route="vision" is the truth
                        # for documents too (2026-08-29).
                        effort=request.effort,
                        extra_images=list(doc_images) + attached,
                        # One focused lookup for a product the document does
                        # not describe, only when this turn may use the web
                        # (engines/document.py, 2026-09-19).
                        web_search=bool(search_allowed),
                    )
            elif request.image_data:
                # An attached image ALWAYS goes to the vision engine — text-only
                # engines (chat/agent/sql/rag) cannot see it, and silently
                # answering "I can't view images" is worse than routing here.
                from .engines import image_memory
                from .engines.vision import run_vision_engine

                answer = await run_vision_engine(
                    text,
                    request.images_data,
                    history,
                    emit,
                    # Honour the composer's Fast/Think/Max (already
                    # normalized to fast|think|max by ChatRequest). Before
                    # 2026-08-28 the engine ignored it and always thought,
                    # so "Fast" on an image was the slowest path in the app.
                    effort=request.effort,
                    conversation_id=conv_key,
                )
                # The picture stays in the conversation for the next
                # question about it (engines/image_memory.py).
                image_memory.remember(
                    conv_key,
                    request.images_data,
                    question=text,
                    answer=answer,
                    # The conversation key is whatever the client sent, so
                    # the viewer is part of the key: image bytes never cross
                    # an account (engines/image_memory.py).
                    user_id=viewer,
                )
            elif image_followup_images:
                # A second question about the image the person already sent.
                # Same engine, same effort, the remembered bytes — so the
                # turn answers from the picture instead of denying it exists.
                from .engines.vision import run_vision_engine

                answer = await run_vision_engine(
                    text,
                    image_followup_images,
                    history,
                    emit,
                    effort=request.effort,
                    conversation_id=conv_key,
                )
            elif image_followup.unavailable:
                # This turn IS about the picture, and the picture is not
                # here: it was larger than the durable budget, so what
                # survived the restart is the record that there was one
                # (engines/image_memory.py). Say that, rather than answer a
                # question about a photo as though no photo had been sent —
                # which is the answer the audit found and the thing this
                # whole module exists to stop. Fixed words, no model call:
                # the app is reporting its own state.
                #
                # Imported under an alias on purpose: the branch above binds
                # the bare name `image_memory` inside THIS function, which
                # makes it a local here too — and these branches are mutually
                # exclusive, so reading it would be an UnboundLocalError.
                from .engines import image_memory as _image_memory

                answer = _image_memory.UNAVAILABLE_NOTICE
                await emit("token", {"text": answer})
                await emit("meta", {"route": "vision"})
            elif github_ref is not None or repo_followup:
                # Phase 3: a GitHub repo URL → clone/index/overview; or a
                # follow-up question about a repo already indexed → code Q&A.
                from .engines.repo import run_repo_engine

                answer = await run_repo_engine(
                    text, github_ref, conv_key, history, emit, request.effort
                )
            elif crawl_url is not None:
                # Phase 3.5: crawl the whole site into the web store.
                from .engines.crawl import run_crawl_engine

                answer = await run_crawl_engine(
                    text, crawl_url, conv_key, history, emit
                )
            elif crawl_site_hits:
                # Follow-up about a crawled site → answer from the stored
                # copy, cited and dated. Only reached when the scoped
                # retrieval found chunks above the relevance floor, so an
                # unrelated question in the same conversation routes normally.
                from .engines.crawl import run_site_qa_engine

                answer = await run_site_qa_engine(
                    text,
                    crawl_site_hits,
                    crawl_site_host,
                    history,
                    emit,
                    effort=request.effort,
                )
            elif url_list:
                # Phase 2: the user pasted link(s) → read them and answer.
                from .engines.url import run_url_engine

                answer = await run_url_engine(
                    text,
                    url_list,
                    conv_key,
                    history,
                    emit,
                    effort=request.effort,
                    # The sharer is the page's introducer in the shared
                    # corpus (V16) — attributable, purgeable.
                    user_id=viewer,
                )
            elif deep_research_on:
                # Deep Research: the iterative research loop. It sits ABOVE
                # the agent engine because orchestrate.decide() classifies
                # exactly this multi-part phrasing as agent=true (and at
                # effort "max" with search it FORCES it), so any lower and
                # the pill would be silently eaten by the planner.
                from .engines.deep_research import run_deep_research_engine

                answer = await run_deep_research_engine(
                    text,
                    history,
                    emit,
                    effort=request.effort,
                    conversation_id=conv_key,
                    user_id=int(signed_in["id"]) if signed_in is not None else None,
                )
            elif want_agent and (request.agent or not dataset_ready):
                # V2 §3b: agent (deep-task) engine. Checked BEFORE plain search
                # because the agent can search inside its own plan ("web"
                # steps): a request that needs both planning and the web gets
                # both, instead of losing the plan to a one-shot search.
                #
                # A conversation with uploaded datasets keeps its turns unless
                # the user forced the agent: the auto classifier judges only
                # the phrasing ("read this file and give me insights" sounds
                # like a task), never sees that a dataset exists, and the
                # agent cannot read datasets — so letting its wish outrank
                # dataset_ready answered dataset questions with "no file came
                # through" (found 2026-08-21).
                #
                # The Salesforce toggle gates Salesforce access — off → the
                # agent works only from the conversation context (shared
                # URLs/docs) + general knowledge. `web` carries the same gate
                # for the internet.
                from .engines.agent import run_agent_engine

                answer = await run_agent_engine(
                    text,
                    history,
                    emit,
                    effort=request.effort,
                    salesforce=(request.mode != "assistant"),
                    web=want_search,
                    # The pill and the auto classifier collapse into one
                    # boolean by here, but they are different promises: auto
                    # lets the planner judge, FORCED means at least one step
                    # actually searches. Without this the agent answered a
                    # current-events question from training memory under a
                    # trust line saying searches go to the internet.
                    web_forced=(request.web_search == "on"),
                    user_id=int(signed_in["id"]) if signed_in is not None else None,
                    conversation_id=conv_key,
                )
            elif want_search and (request.web_search == "on" or not dataset_ready):
                # Phase 1: web search — cited answer from fetched sources.
                # Auto-decided search yields to uploaded datasets for the same
                # reason as the agent above; an explicit "on" still wins.
                from .engines.search import run_search_engine

                answer = await run_search_engine(
                    text,
                    history,
                    emit,
                    request.effort,
                    user_id=int(signed_in["id"]) if signed_in is not None else None,
                    conversation_id=conv_key,
                )
            elif dataset_ready:
                # Phase 4: this conversation has uploaded datasets — answer
                # from their stored PROFILES (never the files themselves).
                from .engines.dataset import run_dataset_engine

                answer = await run_dataset_engine(
                    text,
                    conv_key,
                    history,
                    emit,
                    model_choice=request.model,
                    effort=request.effort,
                )
            elif lane.entered:
                # The small-talk lane (fast_lane above): no knowledge pre-pass,
                # no grounding, no sources — the model and the saved facts.
                from .engines.chat import run_chat_engine

                knowledge_state["decision"] = "small_talk_lane"
                # PROVENANCE_RECORDED still fires from emit(meta): the answer
                # came from the model, with no retrieved source.
                knowledge_state["model_provenance"] = {
                    "source": "model",
                    "retrieved_source_count": 0,
                    "cited_source_count": 0,
                }
                _latency_metrics.knowledge_prepare(
                    0.0, effort=str(request.effort or ""), decision="small_talk_lane", outcome="skipped"
                )
                answer = await run_chat_engine(
                    text,
                    history,
                    emit,
                    mode="assistant",
                    model_choice=request.model,
                    effort=request.effort,
                    grounding="",
                    lane=lane.category,
                )
            elif request.mode == "assistant":
                # V2 §3a: SKIP the router and data engines entirely.
                from .engines.chat import run_chat_engine

                # LIVING KNOWLEDGE (2026-09-01). Before answering from frozen
                # weights, ask whether this question is time-sensitive and
                # whether this machine already read the answer. Web memory
                # used to be reachable ONLY from inside the search engine, so
                # a Fast/search-off chat answered "who's vice president of
                # india" from pretraining while 19 stored pages said
                # otherwise. Costs nothing for a timeless question (one regex)
                # and one local lookup for a live one.
                if prepared_early is not None:
                    prepared = prepared_early
                elif knowledge_task is not None:
                    if knowledge_lookup_started.is_set() and not knowledge_task.done():
                        # A live lookup is genuinely in flight and the user
                        # is now waiting on exactly that. Say so instead of
                        # showing an empty spinner.
                        await emit("status", {"text": "Checking recent sources…"})
                    prepared = await knowledge_task
                else:
                    prepared = await _prepare_knowledge(
                        request,
                        text,
                        allow_network=not want_search,
                        emit=emit,
                        user_id=viewer,
                        conversation_id=conv_key,
                        history=history,
                    )
                if prepared.decision:
                    knowledge_state["decision"] = prepared.decision
                if prepared.degraded:
                    knowledge_state["degraded"] = prepared.degraded
                if prepared.sources:
                    # Same shape the search engine emits, so the Sources panel
                    # and citation chips render locally-sourced evidence with
                    # no client change. Filled BEFORE the engine runs: the
                    # engine emits the one meta, and until 2026-09-03 this
                    # block came after it — the grounding reached the prompt
                    # but the sources never reached the panel.
                    knowledge_state["sources"] = prepared.sources
                    knowledge_state["freshness"] = (
                        prepared.verdict.requirement.value if prepared.verdict else ""
                    )
                    knowledge_state["from_local_memory"] = not prepared.searched
                answer = await run_chat_engine(
                    text,
                    history,
                    emit,
                    mode="assistant",
                    model_choice=request.model,
                    effort=request.effort,
                    grounding=prepared.grounding,
                )
            elif request.sf_live:
                # "Live Salesforce" toggle: skip the router — every text
                # answer queries the org directly (schema questions included;
                # the engine's live branch handles both).
                from .engines.sql import run_sql_engine

                answer = await run_sql_engine(
                    text, history, emit, force_live=True
                )
            else:
                state = await get_graph().ainvoke(
                    {
                        "message": text,
                        "session_id": request.session_id,
                        "image_base64": request.image_data,
                        "history": history,
                        "emit": emit,
                        "model_choice": request.model,
                        "effort": request.effort,
                    }
                )
                answer = state.get("answer") or ""
            if knowledge_task is not None and not knowledge_task.done():
                # Another engine answered; the speculative lookup is moot.
                knowledge_task.cancel()
            # --- AS3 intent-capability BEGIN ---
            # DENIAL BACKSTOP. The engine's FINAL text (after core/answer_guard)
            # denies making a file while the person named one: classify once
            # with the turn's context and, only when the classifier is sure,
            # make the file and append its card and one sentence. The streamed
            # text is not rewritten — the browser already shows it. The gate
            # is the fix; this counter watches what it missed.
            _as3_route = str((gen.final_meta or {}).get("route") or "")
            if (
                settings.artifact_denial_backstop
                and settings.artifacts_enabled
                and request.text
                and _as3_route in ("chat", "agent", "dataset", "vision")
                and not (artifact_intent is not None and artifact_intent.wants_file)
                and feature_access.allowed(principal.features, feature_access.Feature.ARTIFACTS)
                and isinstance(answer, str)
            ):
                from .artifacts import intent as _as3_rules
                from .artifacts import intent_llm as _as3_llm
                from .artifacts import lexicon as _as3_lex
                from .engines import capability as _as3_capability

                if _as3_capability.denial_in(answer) and _as3_lex.file_signal(text):
                    from . import metrics as _as3_metrics

                    _as3_hist = list(history)
                    _as3_idx = _as3_rules.substantial_answer_index(_as3_hist)
                    _as3_verdict = await _as3_llm.classify(
                        text,
                        last_answer_head=_as3_rules.turn_text(_as3_hist[_as3_idx])[:500] if _as3_idx is not None else "",
                        last_turn_is_artifact=_as3_rules.last_turn_is_artifact(_as3_hist),
                        upload_names=[str((r or {}).get("name") or "") for r in (request.pdf_uploads or [])],
                        effort=str(request.effort or "fast"),
                    )
                    _as3_intent = None
                    if _as3_verdict is not None and _as3_verdict.action in ("create", "export", "convert") and _as3_verdict.confidence >= 0.8:
                        _as3_intent = _as3_rules.verdict_to_intent(
                            _as3_verdict, _as3_rules.decide(text), has_artifacts=False,
                            has_assistant_answer=_as3_idx is not None, last_turn_is_artifact=False,
                        )
                    _as3_rerouted = False
                    if _as3_intent is not None and _as3_intent.wants_file:
                        from .engines import artifact as _as3_artifact_engine

                        _as3_prior_meta = dict(gen.final_meta or {})
                        # The engine's sentence is HELD until it returns: "Here
                        # is the file." is said only when a file card exists —
                        # a refused or failed job streams its own sentence
                        # alone (verifier 2026-09-15: a failure used to read
                        # "Here is the file. The file could not be made").
                        _as3_state = {"meta": None, "tokens": []}

                        async def _as3_backstop_emit(event: str, data: dict) -> None:
                            if event == "meta":
                                _as3_state["meta"] = data
                                return
                            if event == "token":
                                _as3_state["tokens"].append(str(data.get("text") or ""))
                                return
                            await emit(event, data)

                        if _as3_intent.action == "export" and _as3_idx is not None:
                            _as3_hist = _as3_hist[: _as3_idx + 1]
                        gen.waiting_on_job = True
                        try:
                            # The chat answer is already streamed: a failure
                            # here must not turn the whole turn into an error
                            # and lose that answer (verifier 2026-09-15).
                            _as3_line = await _as3_artifact_engine.run_artifact_engine(
                                text, _as3_hist, _as3_backstop_emit, intent=_as3_intent, conversation_id=conv_key, user_id=viewer,
                                generation_id=gen.generation_id, effort=request.effort, mode=request.mode, web_allowed=False,
                                intent_id=str(gen.intent_id or "") + ":backstop",
                            )
                        except Exception as _as3_exc:  # noqa: BLE001 — the answer stands
                            logging.getLogger(__name__).warning("artifact denial backstop failed: %s", type(_as3_exc).__name__)
                            _as3_line = ""
                        finally:
                            gen.waiting_on_job = False
                        _as3_art_meta = _as3_state["meta"] or {}
                        _as3_said = "".join(_as3_state["tokens"]) or str(_as3_line or "")
                        if _as3_art_meta.get("artifacts"):
                            _as3_rerouted = True
                            _as3_metrics.inc("artifact_denial_rerouted_total", "denials of file creation turned into a file", engine=_as3_route)
                            _as3_tail = "\n\nHere is the file. " + _as3_said
                            await emit("token", {"text": _as3_tail})
                            answer = f"{answer}{_as3_tail}"
                            _as3_merged = {**_as3_prior_meta, "artifacts": _as3_art_meta["artifacts"], "artifact_backstop": True}
                            gen.final_meta = _as3_merged
                            await gen.publish("meta", _as3_merged)
                        elif _as3_said:
                            _as3_tail = "\n\n" + _as3_said
                            await emit("token", {"text": _as3_tail})
                            answer = f"{answer}{_as3_tail}"
                    if not _as3_rerouted:
                        _as3_metrics.inc("artifact_denial_seen_total", "answers that denied making a file (not rerouted)", engine=_as3_route)
                elif _as3_capability.promise_in(answer):
                    # The other half of the same guard: an answer that claims
                    # a DELIVERY this platform does not perform — "I've
                    # emailed the report to the team", "I have posted the deck
                    # to Slack", "the Excel file is password-protected now".
                    # The gate returns action='none' for these turns (there is
                    # no file to make), so nothing else in the turn could
                    # correct them (measured 2026-09-16, I1/I2/I6).
                    from . import metrics as _as3_metrics

                    _as3_metrics.inc("artifact_false_promise_total", "answers that claimed a delivery the platform cannot perform",
                                     engine=_as3_route)
                    _as3_tail = "\n\n" + _as3_capability.DELIVERY_LINE
                    await emit("token", {"text": _as3_tail})
                    answer = f"{answer}{_as3_tail}"
            # --- AS3 intent-capability END ---
            gen.answer = answer
            memory.add_exchange(scoped_session, text, answer)
            # Durable BEFORE `done`: the row says 'completed' only once the
            # answer is in history, so a status poll that follows the
            # terminal event never reads "completed, nothing persisted".
            await _store_answer(gen)
            await _mark_chat_request(gen, "completed")
            await query_trace.event(
                "RESPONSE_GENERATED",
                component="orchestrator.app.main.worker",
                details={
                    "answer_characters": len(answer or ""),
                    "selected_route": (gen.final_meta or {}).get("route", ""),
                    "has_data": bool((gen.final_meta or {}).get("data")),
                    "final_meta_keys": sorted((gen.final_meta or {}).keys()),
                },
            )
            await query_trace.event(
                "RESPONSE_VALIDATED",
                status="skipped",
                component="orchestrator.app.main.worker",
                details={"reason": "semantic_answer_validator_not_implemented"},
            )
            await query_trace.finish(
                "ok",
                route=str((gen.final_meta or {}).get("route") or ""),
                resolved_mode=query_trace.resolved_mode or str(request.mode or ""),
                meta={
                    "answer_characters": len(answer or ""),
                    "final_meta_keys": sorted((gen.final_meta or {}).keys()),
                    **(
                        {"provenance": (gen.final_meta or {})["provenance"]}
                        if isinstance((gen.final_meta or {}).get("provenance"), dict)
                        else {}
                    ),
                },
            )
            # The clarification this turn cancelled is gone BEFORE `done`
            # (restored 2026-09-13, prover): HEAD awaited the cancel inline,
            # so a follow-up sent the instant the answer ended could never
            # find the stale card — and the partial unique index allows one
            # pending question per conversation, so a stale one blocks the
            # follow-up's own. The cancel still runs beside the answer (the
            # first token does not wait for it); only `done` does, bounded.
            await _settle_clarification_cancel(cancel_pending_task)
            await gen.publish("done", {"session_id": request.session_id})

            # Background compaction: fold early so the next turn almost never
            # needs a synchronous one. Deliberately NOT awaited — the SSE
            # stream stays open until this worker returns, so awaiting it here
            # would make the user wait for the very thing that is supposed to
            # be invisible.
            if signed_in is not None and request.conversation_id:
                from . import compaction

                base_url, _key, model_id = llm.resolve_model_choice(request.model)
                _spawn_background_compaction(
                    conv_key,
                    [
                        *full_history,
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": answer},
                    ],
                    base_url=base_url,
                    model=model_id,
                )
        except _continuity.QueuedForRecovery as exc:
            # CONTRACT §8.3 step 5: the wait for the main model outran the
            # queue window. NOT a failure — the row stays `queued`, the
            # person has read EXPIRED_LINE, and the resume sweep (or a
            # re-attach) runs the same intent when the model is back. The
            # stream ends with a terminal frame that names the parked
            # state, so a client that only knows `done`/`error` does not
            # read an empty answer as a finished one.
            if resumable and gen.request_status == "queued":
                gen.parked = True
                logging.getLogger(__name__).warning(
                    "generation %s (intent %s, attempt %d) in conversation %s parked after %.0fs: "
                    "main model still recovering; row stays queued",
                    gen.generation_id, gen.intent_id, gen.attempt, conv_key_outer, exc.waited_s,
                )
                await gen.publish(
                    "error",
                    {"message": _continuity.EXPIRED_LINE, "code": _continuity.PARKED_CODE, "resumable": True},
                )
            else:
                # Nothing can resume this row by itself — the snapshot
                # carried inline bytes the server does not keep, or the row
                # could not be parked — so the promise of EXPIRED_LINE would
                # be false (round-2 review, main.py:3242). The turn fails
                # with a truthful sentence and a Retry the browser can make
                # (it still has the bytes); the row is `failed`, not left
                # `queued` for a sweep that filters it out.
                gen.parked = False  # the hold marked it; the row is settled `failed` instead
                gen.failed = True
                gen.error = _NOT_RESUMABLE_SENTENCE
                gen.error_code = "MODEL_UNAVAILABLE"
                logging.getLogger(__name__).warning(
                    "generation %s (intent %s, attempt %d) in conversation %s could not be parked "
                    "after %.0fs (resumable=%s, row=%s): failed for a person's retry",
                    gen.generation_id, gen.intent_id, gen.attempt, conv_key_outer, exc.waited_s,
                    resumable, gen.request_status,
                )
                await gen.publish("error", {"message": gen.error, "code": gen.error_code, "resumable": False})
                with contextlib.suppress(Exception):
                    await asyncio.shield(_store_failure(gen, "".join(streamed_text), resumable=False))
            with contextlib.suppress(Exception):
                await asyncio.shield(
                    query_trace.finish(
                        "error",
                        route=str((gen.final_meta or {}).get("route") or ""),
                        resolved_mode=query_trace.resolved_mode or str(request.mode or ""),
                        error=exc,
                    )
                )
        except _continuity.LeaseLost as exc:
            # Another process resumed this request under its own generation
            # while this one slept on READY: theirs is the answer. No frame
            # that reads as a failure — the row (and the answer to come) is
            # not this generation's any more — and no status write.
            gen.lease_lost = True
            logging.getLogger(__name__).warning(
                "generation %s (intent %s, attempt %d) in conversation %s: %s",
                gen.generation_id, gen.intent_id, gen.attempt, conv_key_outer, exc,
            )
            await gen.publish("error", {"message": _TAKEN_OVER_SENTENCE, "code": "replaced"})
            with contextlib.suppress(Exception):
                await asyncio.shield(
                    query_trace.finish(
                        "cancelled",
                        route=str((gen.final_meta or {}).get("route") or ""),
                        resolved_mode=query_trace.resolved_mode or str(request.mode or ""),
                    )
                )
        except asyncio.CancelledError:
            gen.cancelled = True  # /chat/stop or replaced by a newer send
            if gen.replaced:
                # A follower whose stream simply ended would finalize the
                # truncated text as a complete answer (ORCH-02). Shielded:
                # the cancellation that brought us here must not cut the
                # frame that explains it.
                with contextlib.suppress(Exception):
                    await asyncio.shield(
                        gen.publish("error", {"message": _REPLACED_SENTENCE, "code": "replaced"})
                    )
            with contextlib.suppress(Exception):
                await asyncio.shield(
                    query_trace.finish(
                        "cancelled",
                        route=str((gen.final_meta or {}).get("route") or ""),
                        resolved_mode=query_trace.resolved_mode or str(request.mode or ""),
                    )
                )
        except Exception as exc:  # terminal error event (§10)
            gen.failed = True
            gen.error, gen.error_code = _failure_sentence(exc)
            # ORCH-01: the operator's copy — ids and the exception, never the
            # content. The person's copy is the safe sentence on the wire,
            # on the row, and in the failure record persisted below.
            logging.getLogger(__name__).warning(
                "generation %s (intent %s, attempt %d) in conversation %s failed: %s: %s",
                gen.generation_id,
                gen.intent_id,
                gen.attempt,
                conv_key_outer,
                type(exc).__name__,
                str(exc)[:300],
            )
            await gen.publish("error", {"message": gen.error, "code": gen.error_code})
            with contextlib.suppress(Exception):
                await asyncio.shield(
                    _store_failure(gen, "".join(streamed_text), resumable=resumable)
                )
            await query_trace.event(
                "REQUEST_FAILED",
                status="failed",
                component="orchestrator.app.main.worker",
                error=exc,
            )
            await query_trace.finish(
                "error",
                route=str((gen.final_meta or {}).get("route") or ""),
                resolved_mode=query_trace.resolved_mode or str(request.mode or ""),
                error=exc,
            )
        finally:
            reads.close()
            # Every await below goes through `ending.step`, which absorbs a
            # SECOND cancellation (a double Stop, a Stop then a resend) until
            # `_finalize_generation` has run, and re-raises it after. Without
            # it a cancel landing in the trace flush skipped the finalize: the
            # stream never ended and the generation stayed registered until
            # the process restarted (prover probe test_zz_double_stop.py,
            # second Stop 250 ms after the first, 2026-09-13).
            ending = _TurnEnd()
            # The meter's exact count (compaction.prepare_deferred) is read by
            # the meta, and a turn that got here without one never needs it:
            # cancelled, so the queued CONTEXT_ASSEMBLED write records what is
            # known instead of holding the stream open for /tokenize (up to
            # TOKENIZE_TIMEOUT; a Stop ended 2.94 s after the click with a 3 s
            # count, where HEAD ended in 0.01 s).
            pending_count = context_pending["task"]
            if pending_count is not None and not pending_count.done():
                pending_count.cancel()
            # A turn that left before its route was decided (an error, a
            # cancel) still lets the message be remembered, as it always
            # was; only a decided artifact turn says no.
            _release_facts(True)
            # V29: the row's terminal status — cancelled, failed, or
            # interrupted when the loop is tearing this process down.
            await ending.step(_settle_chat_request(gen))
            # V18 telemetry. HERE rather than in emit(): this block is the one
            # that runs for every outcome, so a cancelled or failed turn is
            # counted as cancelled or failed instead of vanishing from the
            # numbers — which is how error rates come to read as zero.
            await ending.step(
                _record_usage_event(
                    gen,
                    _timing,
                    principal.workspace_id,
                    str(request.effort or ""),
                )
            )
            # The clarification cancel this turn started beside its answer,
            # for the turns that ended without `done` (an error, a Stop).
            # Bounded like the wait before `done`: a wedged write must not
            # hold the stream open; the task finishes on its own.
            await ending.step(_settle_clarification_cancel(cancel_pending_task))
            # Latency histograms whose route label only the final meta knows.
            with contextlib.suppress(Exception):
                _observe_first_visible(str((gen.final_meta or {}).get("route") or "unknown"))
                gen.report_relay()
            # The queued trace writes land before the stream closes, so a
            # trace read after its response is complete — they ran behind
            # the answer, and the `done` frame went out ahead of them.
            # Bounded: a slow trace INSERT must not hold the stream open; the
            # writes still land, on their own task.
            await ending.step(query_trace.flush(), bound=_TRACE_FLUSH_BOUND_S)
            await ending.step(_finalize_generation(conv_key_outer, gen))
            query_trace.deactivate(trace_context)
            if ending.cancelled:
                raise asyncio.CancelledError()

    gen.task = asyncio.create_task(worker())

    return _sse_response(gen.follow())


@app.get("/chat/trace/{trace_id}")
async def query_trace(trace_id: str, http_request: Request) -> dict:
    """Authenticated diagnostic timeline for one generation owned by caller."""
    viewer = await _require_viewer(http_request)
    row = await db.run_in_thread(db.get_query_trace, trace_id, viewer)
    if row is None:
        raise HTTPException(status_code=404, detail="query trace not found")
    return row


@app.get("/chat/salesforce/{conversation_id}")
async def salesforce_context(
    conversation_id: str, http_request: Request
) -> dict:
    """Restore Salesforce Intelligence state for a conversation.

    This is what makes a clarification card survive a reload: the browser asks
    for the pending question on mount and rebuilds the card from the server's
    copy, rather than from whatever the tab happened to have in memory. It also
    supplies the starter card's options, filtered to what this connection can
    actually reach.

    Scoped to the conversation's owner, like /chat itself — a conversation whose
    id someone guessed must not disclose what it is asking about.
    """
    viewer = await _require_viewer(http_request)
    _refuse_reserved_conversation_key(conversation_id)
    owner = await db.run_in_thread(db.conversation_owner, conversation_id)
    if owner is not None and owner != viewer:
        raise HTTPException(status_code=404, detail="conversation not found")

    from .engines import sf_intel

    if not settings.salesforce_intelligence_enabled:
        return {"enabled": False, "options": [], "pending_clarification": None}
    # FAIL CLOSED on an id whose owner cannot be determined (2026-09-12,
    # confirmed P1, same class as the /chat claim above).
    #
    # The old comment here said an unowned id "has no state to leak". That was
    # untrue in two ways, and both were reachable:
    #
    #  * `sf_conversation_state` and `sf_clarifications` are keyed by
    #    conversation id and have NO foreign key to `conversations`. Deleting a
    #    CHAT is fine — `db.delete_conversation` clears both through
    #    `_SIDE_TABLES` — but deleting an ACCOUNT is not: `conversations.user_id`
    #    cascades from `users`, the conversation row goes with it, and nothing
    #    reaches the Salesforce state, which is left owned by nobody.
    #    `starter_options` then hands the next caller that state — the
    #    "continue" option is built from `state.last_query_summary`, which is
    #    the departed colleague's Salesforce question in plain text;
    #  * a BARE call (no conversation_id) keys its Salesforce state under the
    #    synthetic `u<user id>-<session id>`, which is not a conversation row
    #    either — so `GET /chat/salesforce/u7-default` read user 7's last
    #    question and pending clarification for anyone who asked.
    #
    # The fix cannot be a 404: the starter card is drawn for a NEW chat, whose
    # id has deliberately not been claimed yet (the claim happens on the first
    # message), and refusing would remove the card from every new chat. So the
    # card is computed against a throwaway id that exists nowhere. The generic
    # catalogue — which objects this connection can reach — is unchanged,
    # because it is a property of the org and not of the conversation, while
    # everything that IS conversation state (the continuation option, the
    # pending clarification) reads as absent, which for an id this caller does
    # not own is the only honest answer.
    #
    # NOT a claim, unlike POST /chat: claiming an unowned id here would hand
    # the caller whatever orphaned state was sitting under it, which is the
    # very thing this closes.
    state_key = conversation_id if owner == viewer else uuid.uuid4().hex
    try:
        return await sf_intel.starter_options(state_key)
    except Exception as exc:  # noqa: BLE001 — a starter card is never fatal
        logging.getLogger(__name__).info(
            "salesforce context unavailable for %s: %s", conversation_id, exc
        )
        return {"enabled": True, "options": [], "pending_clarification": None}


class SalesforceCancelRequest(BaseModel):
    conversation_id: str


@app.post("/chat/salesforce/cancel")
async def salesforce_cancel(
    body: SalesforceCancelRequest, http_request: Request
) -> dict:
    """Cancel a pending clarification.

    Called when the Salesforce source is switched off with a question on screen.
    Deterministic by design: the card disappears because the server says it is
    cancelled, not because the client stopped drawing it — otherwise the next
    Salesforce turn in that chat would resume a question the user had visibly
    dismissed.
    """
    viewer = await _require_viewer(http_request)
    _refuse_reserved_conversation_key(body.conversation_id)
    owner = await db.run_in_thread(db.conversation_owner, body.conversation_id)
    # FAIL CLOSED, and here it really is a refusal (2026-09-12, confirmed P1).
    # `owner is not None and owner != viewer` let an UNOWNED id through, and a
    # pending clarification can sit under an id that owns nothing: a bare call
    # parks it under the synthetic `u<user id>-<session id>`, and a deleted
    # ACCOUNT leaves `sf_clarifications` rows behind, because that table has no
    # foreign key to `conversations` and `conversations` cascades from `users`
    # (deleting a CHAT is clean — `db.delete_conversation` clears it through
    # `_SIDE_TABLES`). So anyone could cancel anyone else's pending question —
    # a write, not merely a read — and the next Salesforce turn in that chat
    # would silently stop resuming it.
    #
    # Unlike the GET above there is nothing to preserve: a question you can
    # cancel is one that was asked in a conversation you own, and a brand-new
    # chat has none. 404 rather than 403, so the reply cannot say whether the
    # id exists (the refusal style of every other owner check in this file).
    if owner != viewer:
        raise HTTPException(status_code=404, detail="conversation not found")

    from .core.sf_intel import state as sf_intel_state

    cancelled = await sf_intel_state.cancel_pending(body.conversation_id)
    return {"cancelled": cancelled}


class StopRequest(BaseModel):
    conversation_id: Optional[str] = None
    session_id: str = "default"

    # The third door onto the same namespace (F034): /chat/stop rebuilds the
    # synthetic key itself, so it takes the same two rules. Nothing was
    # reachable through it — `_owns` compares the generation's user id before
    # anything is cancelled — but leaving one spelling of the key unvalidated
    # is how the next person concludes the shape is allowed somewhere.
    @field_validator("session_id")
    @classmethod
    def _valid_session_id(cls, value: str) -> str:
        return _checked_session_id(value)

    @field_validator("conversation_id")
    @classmethod
    def _conversation_id_is_not_synthetic(cls, value: Optional[str]) -> Optional[str]:
        return _reject_synthetic_conversation_id(value)


async def _prepare_knowledge(
    request,
    text: str,
    *,
    allow_network: bool,
    emit=None,
    user_id: Optional[int] = None,
    conversation_id: str = "",
    history=(),
):
    """Freshness-aware grounding for one assistant turn, or an empty result.

    Wrapped so a failure in the knowledge layer can never cost the user an
    answer: on any error the caller gets empty grounding and the model answers
    exactly as it did before this existed. `user_id`/`conversation_id`
    attribute any page the Fast lookup introduces (V16).
    """
    from .living_knowledge import Prepared, prepare

    if not settings.living_knowledge_enabled:
        return Prepared()
    try:
        return await prepare(
            text,
            effort=llm.normalize_effort(request.effort),
            mode=request.mode,
            web_search_pref=request.web_search,
            allow_network=allow_network and settings.search_enabled,
            emit=emit,
            user_id=user_id,
            conversation_id=conversation_id,
            history=history,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — grounding is an enhancement
        logging.getLogger(__name__).debug("living knowledge unavailable", exc_info=True)
        return Prepared()


async def _viewer_id(http_request: Request) -> Optional[int]:
    """The signed-in user's id, or None for an unauthenticated caller.

    Async because resolving the local account is a database round trip
    (auth.local_user -> db.get_user_by_id); running it inline would block the
    event loop on the SSE attach/stop hot paths.
    """
    from .auth import current_user

    user = await db.run_in_thread(current_user, http_request)
    return int(user["id"]) if user is not None else None


async def _require_viewer(http_request: Request) -> int:
    """The signed-in user's id, or 401. The generation-lifecycle and
    Salesforce-state routes all carry user data; none serves anonymous."""
    viewer = await _viewer_id(http_request)
    if viewer is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    return viewer


def _owns(gen: "LiveGeneration", viewer: Optional[int]) -> bool:
    """A generation is only visible to the identity that started it.

    Without this, /chat/attach would stream ANOTHER user's answer to anyone
    who guessed a conversation id, and /chat/stop would let them cancel it.
    Anonymous generations (direct API calls, no cookie) belong to the
    anonymous caller — a signed-in user cannot reach them either.
    """
    return gen.user_id == viewer


@app.post("/chat/stop")
async def chat_stop(body: StopRequest, http_request: Request) -> dict:
    """Cancel a running generation. Closing the SSE stream no longer stops
    the model (generations are detached), so the Stop button calls this."""
    viewer = await _require_viewer(http_request)
    # Bare-API generations register under the caller's user-scoped session
    # key (see /chat), so stop looks them up the same way.
    key = body.conversation_id or f"u{viewer}-{body.session_id}"
    gen = _live_generations.get(key)
    if gen is None or gen.done or gen.task is None:
        return {"stopped": False}
    if not _owns(gen, viewer):
        return {"stopped": False}  # not yours — indistinguishable from absent
    gen.task.cancel()
    # V29: the row says so right away. The worker settles it again when the
    # cancellation lands, but a status poll in between must not read
    # 'running' for a generation whose Stop was just acknowledged. A viewer
    # merely disconnecting never comes through here — that is not a cancel.
    await _mark_chat_request(gen, "cancelled")
    return {"stopped": True}


@app.get("/chat/active")
async def chat_active(http_request: Request) -> dict:
    """Conversation keys with a generation still running — the sidebar polls
    this to show a ChatGPT-style spinner next to busy chats. Scoped to the
    caller: another account's conversation ids are never disclosed."""
    viewer = await _require_viewer(http_request)
    return {
        "active": [
            k
            for k, g in _live_generations.items()
            if not g.done and _owns(g, viewer)
        ]
    }


@app.get("/chat/requests/{intent_id}")
async def chat_request_status(intent_id: str, http_request: Request) -> dict:
    """What the server knows about one send intent (V29).

    The reconciliation a reloaded tab runs before it claims anything about
    a turn: `live` says a generation for it is in THIS process's registry
    right now; `answer_persisted` says an assistant message under its
    generation_id is in history. Not yours → 404, never 403: a 403 would
    confirm the id exists.
    """
    viewer = await _require_viewer(http_request)
    row = await db.run_in_thread(db.get_chat_request, intent_id)
    if row is None or int(row["user_id"]) != viewer:
        raise HTTPException(status_code=404, detail="unknown intent")
    stored = await db.run_in_thread(
        _persisted_answer, row["conversation_id"], row["generation_id"]
    )
    return {
        "intent_id": row["intent_id"],
        "conversation_id": row["conversation_id"],
        "status": row["status"],
        "generation_id": row["generation_id"],
        "attempt": int(row.get("attempt") or 1),
        "resumable": bool(row.get("resumable")),
        "answer_persisted": _is_answer(stored),
        "live": _live_generation_for(row["generation_id"]) is not None,
    }


class CompactRequest(BaseModel):
    conversation_id: str
    # fail_fast: see ChatRequest.messages.
    messages: Optional[List[ChatMessage]] = Field(default=None, fail_fast=True)


@app.post("/chat/compact")
async def chat_compact(http_request: Request) -> dict:
    """Compact a conversation on demand ("Compact now" in the meter popover).

    Folds everything except the most recent turns, regardless of how full the
    window currently is.

    Signed in BEFORE the body is parsed (2026-09-13): with `body:
    CompactRequest` declared, an anonymous 1 MiB body was validated — and its
    per-element 422 built — before the 401 below could run. The conversation
    id lives IN the body, so ownership is checked right after the parse; the
    parse itself is the bounded one.
    """
    from . import compaction
    from .auth import current_user
    from .history import read_validated_body

    user = await db.run_in_thread(current_user, http_request)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    body = await read_validated_body(http_request, CompactRequest)
    owner = await db.run_in_thread(db.conversation_owner, body.conversation_id)
    if owner is None or owner != int(user["id"]):
        raise HTTPException(status_code=404, detail="conversation not found")

    history = [
        {"role": m.role, "content": m.content}
        for m in (body.messages or [])
        if m.content and m.content.strip()
    ]
    if not history:
        history = [
            {"role": m["role"], "content": m["content"]}
            for m in await db.run_in_thread(db.list_messages, body.conversation_id)
            # Same rule as the `body.messages` branch above and as
            # `ChatRequest.history_messages`: a blank turn has nothing to
            # summarize, and `covers_through` counts blank-filtered turns.
            if m["content"] and m["content"].strip()
        ]
    result = await compaction.compact(body.conversation_id, history, force=True)
    # What is STILL foldable afterwards, from the same helper the summary
    # endpoint serves — so the popover can update without a refetch and can
    # never disagree with what this button would do on the next press.
    counts = await db.run_in_thread(foldable_counts, body.conversation_id, history)
    if result is None:
        return {
            "compacted": False,
            "reason": "nothing older to summarize",
            **counts,
        }
    return {
        "compacted": True,
        "folded_turns": result["folded"],
        "covers_through": result["covers_through"],
        **counts,
    }


@app.get("/chat/attach/{conversation_id}")
async def chat_attach(
    conversation_id: str, http_request: Request
) -> StreamingResponse:
    """Re-join a running generation after a reload: replays every buffered
    event (so the partial answer rebuilds instantly) and then streams live.
    404 once it has finished — the answer is in history at that point."""
    viewer = await _require_viewer(http_request)
    gen = _live_generations.get(conversation_id)
    if gen is not None and not gen.done and _owns(gen, viewer):
        return _sse_response(gen.follow())
    # V29: nothing live here — but the conversation's newest request may be
    # one the process that accepted it lost (a deploy, a reboot). When it is
    # resumable, run it again from its stored snapshot under a new attempt
    # and stream that; the browser's re-attach after a restart then gets an
    # answer instead of a 404 it cannot tell from "finished" (RC-3). 404
    # only when there is nothing live and nothing to resume.
    latest = await db.run_in_thread(db.latest_chat_request, conversation_id)
    if (
        latest is None
        or int(latest["user_id"]) != viewer
        or latest["status"] not in _RESUMABLE_STATUSES
        or not latest.get("resumable")
    ):
        raise HTTPException(status_code=404, detail="no active generation")
    if latest["status"] == "interrupted" and await db.run_in_thread(
        _interrupted_after_tokens, latest["conversation_id"], latest["generation_id"]
    ):
        # CONTRACT §8.4: after the first token the attempt stays interrupted
        # and nothing re-runs it by itself — the partial the tab kept is in
        # history, and a second answer must not be appended beside it. The
        # row is settled `failed` (the same settlement the resume sweep
        # makes) so the person is offered a Retry, and this re-attach ends
        # like a finished one: 404, load history.
        from . import continuity as _continuity_mod

        with contextlib.suppress(Exception):
            await db.run_in_thread(
                db.set_chat_request_status, latest["intent_id"], "failed",
                error=_continuity_mod.interrupted_after_tokens_sentence(
                    await db.run_in_thread(_persisted_answer, latest["conversation_id"], latest["generation_id"])
                ),
            )
        raise HTTPException(status_code=404, detail="no active generation")
    try:
        request = ChatRequest.model_validate(
            {**(latest.get("request") or {}), "intent_id": latest["intent_id"]}
        )
    except ValidationError as exc:
        # The snapshot no longer parses (a field the model dropped since it
        # was stored): nothing can run it, so the row says so instead of
        # answering 404 on every re-attach forever.
        logging.getLogger(__name__).warning(
            "chat request %s cannot be resumed from its snapshot: %s",
            latest["intent_id"],
            str(exc)[:200],
        )
        with contextlib.suppress(Exception):
            await db.run_in_thread(
                db.set_chat_request_status,
                latest["intent_id"],
                "failed",
                error="the stored request could not be resumed",
            )
        raise HTTPException(status_code=404, detail="no active generation")
    # The same door a retry from the browser uses: /chat finds the known
    # intent with no live generation and runs a new attempt from this body.
    return await chat(request, http_request)
