"""The job: run the stages against one `video_analyses` row, resumably.

DETACHED FROM THE CHAT TURN, ON PURPOSE. A new message in the same
conversation cancels the running generation (main.py), and so does the Stop
button. A two-hour recording takes longer to transcribe than anyone waits
before asking a second question. So the analysis is a task of its own,
started at upload time, holding a strong reference so the loop cannot
collect it, checkpointing every stage to disk and PostgreSQL. A chat turn
SUBSCRIBES to it — receives its progress, waits for it to finish, answers —
and if that turn is cancelled the job does not notice.

RESUMABLE. Every stage writes one output file and stamps itself `done` on
the row only after the file is durable. On a re-run — a crash, a restart,
the same video uploaded again next week — a stage whose file exists and
whose stamp says done is skipped outright, so a failure in fusion never
re-runs the twenty minutes of transcription that preceded it. `attempt`
counts the runs; a stage that fails is re-tried from scratch on the next
attempt because its file was never written.

ONE JOB AT A TIME (VIDEO_MAX_CONCURRENT_JOBS=1). Measured 2026-09-08: a
saturated speech engine on EITHER node takes the chat model from 71 tok/s to
24, because the chat model is tensor-parallel across both. Two videos at
once would double that. The queue is the database — rows at 'queued' —
and it drains in id order.

PROGRESS is fanned out to every subscriber as small dicts; the chat engine
turns them into `step` events. Percent updates stay in memory; only stage
transitions touch the row, so a two-hour job is a few dozen UPDATEs, not
thousands.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .. import db, metrics
from ..config import settings
from ..asr import ASRBusy, ASRUnavailable
from ..resilience import ModelUnavailable, recovery_window
from . import artifacts as art
from . import loops, store
from .types import STAGES, STAGE_TITLES, OcrSpan, Segment, Understanding

log = logging.getLogger(__name__)

#: This process, as the lease on a run names it (V29). Host and pid say
#: where; the random tail keeps a pid recycled by a container restart from
#: looking like the process that died — its lease must expire, not renew.
_OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _router_enabled() -> bool:
    """The router sidecar is optional on some profiles; its capability record says."""
    caps = getattr(settings, "router_capabilities", None)
    return bool(getattr(caps, "enabled", True)) and bool(settings.router_base_url)

_DONE = "_done"

#: Bump this when a stage's OUTPUT changes meaning — a transcription that
#: used to drop words, a chunker that splits differently — so an analysis
#: finished under the old code is re-run from the source the next time its
#: file is attached, instead of being served from the cache forever. The
#: row records the version it was produced with; stage files on disk are
#: overwritten as each stage re-runs. Bumps are deliberate and rare: every
#: re-attached video pays a full analysis for each one.
#:   1  first cut (2026-09-09)
#:   2  windows go to the engine with its 30-s silence gate off (a 12-s
#:      clip with a quiet lead-in came back empty)
#:   3  repetition loops are collapsed out of the transcript (video/loops.py:
#:      17.9% of a real 2h23m transcript was the decoder repeating itself).
#:      Scoped: see `_RERUN_FOR` — the engines are not asked again.
PIPELINE_VERSION = 3

#: WHAT A BUMP INVALIDATES, when that is known precisely. A stale row re-runs
#: the stages named for every version between its own and this one and keeps
#: the rest from disk; a row from a version with no entry here re-runs
#: everything, which is the safe reading of "I do not know what changed".
#: v3 changed how the transcript is CLEANED, not what the engines heard or
#: saw: whisper, OCR and the captions would produce the same files again, and
#: for a two-hour recording that is forty minutes of two GPUs to fix text.
_RERUN_FOR: Dict[int, Tuple[str, ...]] = {
    3: ("transcript", "fusion", "index", "artifacts"),
}

#: The engine's transcript has meant the same thing since this version: a
#: `transcript.json` written by v2 or later is the engine's own words with
#: the loops still in, and the transcript stage may REPAIR it in place rather
#: than transcribe again. A future bump that changes what the engine is asked
#: (a different model, a different window plan) moves this forward.
_TRANSCRIPT_ENGINE_SINCE = 2


def is_current(row: dict) -> bool:
    """True when this row's results came from the code that is running."""
    return int(row.get("pipeline_version") or 1) >= PIPELINE_VERSION


def stages_to_rerun(old_version: int) -> Optional[set]:
    """The stages a row from `old_version` must run again, or None for all."""
    needed: set = set()
    for version in range(old_version + 1, PIPELINE_VERSION + 1):
        if version not in _RERUN_FOR:
            return None
        needed.update(_RERUN_FOR[version])
    return needed

#: Answers "is somebody chatting right now?" — installed by main.py at
#: startup, because this module must not import main. None = never busy.
_busy_probe: Optional[Callable[[], bool]] = None


def set_busy_probe(fn: Optional[Callable[[], bool]]) -> None:
    global _busy_probe
    _busy_probe = fn


async def pace() -> float:
    """Hold GPU-heavy batch work while a person is waiting for an answer.

    THE POLICY, IN ONE PLACE. Measured 2026-09-09 on the live meeting video:
    chat decode 75 tok/s idle, ~35 during transcription, ~20 during OCR —
    every GPU-heavy unit of this pipeline is felt by whoever is chatting,
    because the chat model is tensor-parallel across both Sparks. So before
    each unit — an ASR window, an OCR batch, a caption — the stage asks
    whether a chat generation is in flight and, if so, waits a second and
    asks again, up to `VIDEO_PACE_MAX_WAIT_S`. A quiet workspace runs the job
    at full speed; a busy one runs it in the gaps. The cap keeps a chatty
    workspace from starving a job forever: past it the unit runs anyway.

    Returns the seconds waited, for the stage's own bookkeeping.
    """
    waited = 0.0
    limit = float(settings.video_pace_max_wait_s)
    while _busy_probe is not None and limit > 0 and waited < limit:
        try:
            busy = bool(_busy_probe())
        except Exception:  # noqa: BLE001 — the probe is advisory
            busy = False
        if not busy:
            break
        await asyncio.sleep(1.0)
        waited += 1.0
    if waited:
        metrics.observe("video_pace_seconds", waited, "seconds a batch unit waited for chat to finish")
    return waited

#: Stage -> whether the pipeline can carry on without it. A required stage
#: that fails fails the video; an optional one is recorded as failed and the
#: video is understood from what remains.
_OPTIONAL = {"ocr", "vision", "index"}
#: The prefix a deferred row's `error` carries; the drain uses it to tell a
#: row that is waiting for an engine from one that is simply new.
DEFERRED_MARK = "waiting for the model"


@dataclass
class _Ctx:
    """Everything a stage may need, loaded lazily from the stage files."""

    row: dict
    content_hash: str
    source: str
    #: The pipeline version the row's files on disk were written by. Equal to
    #: PIPELINE_VERSION on a fresh run; lower on a scoped re-run, where a
    #: stage may decide to repair its old file instead of recomputing it.
    from_version: int = PIPELINE_VERSION
    probe: Optional[dict] = None
    segments: Optional[List[Segment]] = None
    language: Optional[str] = None
    speech_fraction: Optional[float] = None
    frames: Optional[List[dict]] = None
    spans: Optional[List[OcrSpan]] = None
    understanding: Optional[Understanding] = None
    counts: Dict[str, Any] = field(default_factory=dict)

    # -- loaders (resume reads the file a previous run wrote) --------------

    def load_probe(self) -> dict:
        if self.probe is None:
            self.probe = store.read_json(store.stage_path(self.content_hash, "probe.json")) or {}
        return self.probe

    def load_transcript(self) -> List[Segment]:
        if self.segments is None:
            data = store.read_json(store.stage_path(self.content_hash, "transcript.json")) or {}
            self.segments = [Segment.from_json(s) for s in data.get("segments") or []]
            self.language = data.get("language") or None
            report = data.get("report") or {}
            self.speech_fraction = report.get("speech_fraction")
        return self.segments

    def load_frames(self) -> List[dict]:
        if self.frames is None:
            data = store.read_json(store.stage_path(self.content_hash, "frames.json")) or {}
            self.frames = list(data.get("frames") or [])
        return self.frames

    def load_spans(self) -> List[OcrSpan]:
        if self.spans is None:
            data = store.read_json(store.stage_path(self.content_hash, "screen.json")) or {}
            self.spans = [OcrSpan.from_json(s) for s in data.get("spans") or []]
        return self.spans

    def load_understanding(self) -> Understanding:
        if self.understanding is None:
            data = store.read_json(store.stage_path(self.content_hash, "understanding.json")) or {}
            self.understanding = Understanding.from_json(data)
        return self.understanding


# ------------------------------------------------------------- registry --

_tasks: Dict[int, "asyncio.Task[None]"] = {}
_listeners: Dict[int, List["asyncio.Queue[dict]"]] = {}
_latest: Dict[int, dict] = {}
_sem: Optional[asyncio.Semaphore] = None
_sem_loop: Optional[asyncio.AbstractEventLoop] = None
_maintenance: Optional["asyncio.Task[None]"] = None


def _semaphore() -> asyncio.Semaphore:
    global _sem, _sem_loop
    loop = asyncio.get_running_loop()
    if _sem is None or _sem_loop is not loop:
        _sem = asyncio.Semaphore(max(1, settings.video_max_concurrent_jobs))
        _sem_loop = loop
    return _sem


def reset_for_tests() -> None:
    global _sem, _sem_loop
    _tasks.clear()
    _listeners.clear()
    _latest.clear()
    _sem = None
    _sem_loop = None


def subscribe(analysis_id: int) -> "asyncio.Queue[dict]":
    q: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=256)
    _listeners.setdefault(int(analysis_id), []).append(q)
    last = _latest.get(int(analysis_id))
    if last is not None:
        q.put_nowait(last)
    return q


def unsubscribe(analysis_id: int, q: "asyncio.Queue[dict]") -> None:
    lst = _listeners.get(int(analysis_id))
    if lst and q in lst:
        lst.remove(q)
    if lst is not None and not lst:
        _listeners.pop(int(analysis_id), None)


def _publish(analysis_id: int, event: dict) -> None:
    event = {**event, "ts": time.time()}
    _latest[int(analysis_id)] = event
    for q in list(_listeners.get(int(analysis_id), [])):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # A subscriber that stopped reading loses a progress tick, not
            # the job. The terminal event is retried below.
            if event.get("stage") == _DONE:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:  # noqa: BLE001
                    pass


def is_running(analysis_id: int) -> bool:
    task = _tasks.get(int(analysis_id))
    return task is not None and not task.done()


async def ensure_running(analysis_id: int) -> bool:
    """Start the job for this row unless it is running or already done."""
    analysis_id = int(analysis_id)
    if is_running(analysis_id):
        return True
    row = await db.run_in_thread(db.get_video_analysis, analysis_id)
    if row is None:
        return False
    if row["status"] == "done":
        if is_current(row):
            return False
        # Finished under older code: back to the queue BEFORE the task
        # exists, so a reader that looks at the row while another job holds
        # the slot sees 'queued' rather than a stale 'done'.
        log.info("video analysis %d was produced by pipeline v%s; re-running under v%d", analysis_id, row.get("pipeline_version"), PIPELINE_VERSION)
        await db.run_in_thread(db.update_video_analysis, analysis_id, status="queued", error="")
    task = asyncio.get_running_loop().create_task(_run(analysis_id), name=f"video-analysis-{analysis_id}")
    _tasks[analysis_id] = task
    task.add_done_callback(lambda t, aid=analysis_id: _tasks.pop(aid, None) if _tasks.get(aid) is t else None)
    return True


async def wait_for(analysis_id: int) -> Optional[dict]:
    """Block until the job (if any) finishes; return the fresh row."""
    task = _tasks.get(int(analysis_id))
    if task is not None:
        try:
            await asyncio.shield(task)
        except Exception:  # noqa: BLE001 — the row records the failure
            pass
    return await db.run_in_thread(db.get_video_analysis, int(analysis_id))


# ---------------------------------------------------------------- the run --


#: Which stages may run side by side. Speech and screen never read each
#: other's output — fusion is the first stage that needs both — so a video
#: is transcribed on the Sparks' speech engines while its frames are cut,
#: read and captioned on the OCR and router engines. Measured on the
#: 10-minute meeting: the two branches were 96 s and 130 s run one after the
#: other; side by side the job is as long as the longer one.
_BRANCHES: Tuple[Tuple[str, ...], ...] = (("audio", "transcript"), ("frames", "ocr", "vision"))
_PRELUDE: Tuple[str, ...] = ("probe",)
_TAIL: Tuple[str, ...] = ("fusion", "index", "artifacts")


class _Runner:
    """One job's stage machinery: the cache check, the timeout, the row
    update and the progress event, identical for every stage, so the shape
    of the pipeline (what runs after what, and what runs together) is a
    handful of tuples above rather than a loop body."""

    def __init__(self, analysis_id: int, ctx: "_Ctx", stages: Dict[str, dict]) -> None:
        self.analysis_id = analysis_id
        self.ctx = ctx
        self.stages = stages  # shared by every branch; each stage writes only its own key
        #: Set when a stage gave up waiting for the model engine: the job is
        #: to be DEFERRED (requeued with its finished stages), not failed.
        self.deferred: Optional[str] = None

    async def stage(self, stage: str) -> Optional[str]:
        """Run one stage. Returns the failure to report when the failure is
        fatal for the job, None otherwise (done, skipped, or optional)."""
        analysis_id, ctx, stages = self.analysis_id, self.ctx, self.stages
        content_hash = ctx.content_hash
        state = stages.get(stage) or {}
        output = _OUTPUTS.get(stage)
        cached = state.get("status") in ("done", "skipped") and (
            output is None or os.path.exists(store.stage_path(content_hash, output))
        )
        if cached:
            _publish(analysis_id, {"stage": stage, "status": state.get("status"), "percent": 100, "detail": "from an earlier run" if state.get("status") == "done" else str(state.get("detail") or ""), "elapsed_s": 0, "cached": True})
            return None
        stage_started = time.perf_counter()
        _publish(analysis_id, {"stage": stage, "status": "running", "percent": 0, "detail": "", "elapsed_s": 0})
        await db.run_in_thread(db.update_video_analysis, analysis_id, stage=stage)

        async def progress(percent: Optional[float], detail: str, _stage=stage, _t0=stage_started) -> None:
            _publish(analysis_id, {
                "stage": _stage,
                "status": "running",
                "percent": None if percent is None else round(min(100.0, max(0.0, percent)), 1),
                "detail": detail,
                "elapsed_s": round(time.perf_counter() - _t0, 1),
            })

        try:
            outcome = await asyncio.wait_for(
                _STAGE_FNS[stage](ctx, progress),
                timeout=settings.video_stage_timeout_s,
            )
        except asyncio.CancelledError:
            # A shutdown. The row stays 'running' and the startup
            # reconciliation requeues it; the stage file was never
            # written, so this stage re-runs and nothing before it does.
            raise
        except asyncio.TimeoutError:
            outcome = _StageResult(status="failed", detail=f"did not finish within {settings.video_stage_timeout_s:.0f}s")
        except (ModelUnavailable, ASRUnavailable, ASRBusy) as exc:
            # The engine this stage needs was down for the whole recovery
            # window (a TP=2 reload measured 13 min on 2026-09-10; a whisper
            # replica losing its CUDA context). That is not this video's
            # fault. A REQUIRED stage is recorded as deferred — NOT done, so
            # it re-runs — and _run_stages puts the job back in the queue
            # with every finished stage kept. An OPTIONAL stage fails soft
            # exactly as any other failure: the video finishes without it
            # rather than holding the whole job for a sidecar it can do
            # without. Until 2026-09-11 both landed in the branch below,
            # the row read 'failed' and nothing ever re-ran it.
            if stage in _OPTIONAL:
                log.warning("video %s: optional stage %s skipped — %s", content_hash[:12], stage, exc)
                outcome = _StageResult(status="failed", detail=f"{type(exc).__name__}: {str(exc)[:400]}")
            else:
                log.warning("video %s: stage %s deferred — %s", content_hash[:12], stage, exc)
                outcome = _StageResult(status="deferred", detail=f"{DEFERRED_MARK}: {str(exc)[:300]}")
        except Exception as exc:  # noqa: BLE001 — recorded on the row, never a crash
            log.exception("video %s: stage %s failed", content_hash[:12], stage)
            outcome = _StageResult(status="failed", detail=f"{type(exc).__name__}: {str(exc)[:400]}")
        ms = int((time.perf_counter() - stage_started) * 1000)
        stages[stage] = {"status": outcome.status, "ms": ms, "detail": outcome.detail}
        metrics.observe("video_stage_seconds", ms / 1000.0, "wall-clock per pipeline stage", stage=stage)
        metrics.inc(
            "video_stage_total", "pipeline stages finished", stage=stage,
            result={"failed": "fail", "deferred": "deferred"}.get(outcome.status, "ok"),
        )
        # Both branches write the whole `stages` map; each write carries the
        # union as it stands, so the last one to land is also the most
        # complete one.
        fields = {"stages": dict(stages), "counts": dict(ctx.counts), **outcome.row_fields}
        await db.run_in_thread(db.update_video_analysis, analysis_id, **fields)
        _publish(analysis_id, {"stage": stage, "status": outcome.status, "percent": 100, "detail": outcome.detail, "elapsed_s": round(ms / 1000.0, 1)})
        if outcome.status == "deferred":
            self.deferred = f"{STAGE_TITLES.get(stage, stage)}: {outcome.detail}"
            return self.deferred
        if outcome.status == "failed" and stage not in _OPTIONAL:
            return f"{STAGE_TITLES.get(stage, stage)}: {outcome.detail}"
        return None

    async def chain(self, names: Sequence[str]) -> Optional[str]:
        """Stages one after another; stops at the first fatal failure."""
        for name in names:
            failure = await self.stage(name)
            if failure:
                return failure
        return None


def _lease_holder(analysis_id: int) -> str:
    """Who holds the row's lease right now ('' for nobody). In a thread."""
    row = db.get_video_analysis(analysis_id)
    return str((row or {}).get("lease_owner") or "")


async def _heartbeat(analysis_id: int, run: "asyncio.Task[None]") -> None:
    """Renew the lease every third of its TTL while the run lasts.

    A renewal that FAILS means another process took the row over after this
    one's lease lapsed — a stalled loop, a database outage longer than the
    TTL. Two runs of the same video would now be racing for the same stage
    files, so this one stands down: the other owner finishes the job.
    """
    ttl = float(settings.video_lease_ttl_s)
    interval = max(1.0, ttl / 3.0)
    while True:
        await asyncio.sleep(interval)
        try:
            renewed = await db.run_in_thread(db.claim_video_lease, analysis_id, _OWNER, ttl)
        except Exception:  # noqa: BLE001 — the next beat retries
            log.warning("video analysis %d: lease heartbeat failed", analysis_id, exc_info=True)
            continue
        if not renewed:
            log.warning("video analysis %d: the lease was taken by another owner; standing down", analysis_id)
            run.cancel()
            return


async def _run(analysis_id: int) -> None:
    async with _semaphore():
        row = await db.run_in_thread(db.get_video_analysis, analysis_id)
        if row is None:
            return
        if row["status"] == "done" and is_current(row):
            _publish(analysis_id, {"stage": _DONE, "status": "done"})
            return
        # THE LEASE (V29). The row is claimed for this process before any
        # stage runs and renewed while they do. A claim fails only while a
        # DIFFERENT owner's lease is still live — the same video is already
        # being analysed by a process that is alive (a rolling recreate next
        # to a healthy worker, a second orchestrator) — and then this run
        # leaves it to that owner. A claim that takes over an EXPIRED lease
        # is counted: that is a process that died mid-run, which is worth
        # seeing on a dashboard.
        holder = await db.run_in_thread(_lease_holder, analysis_id)
        claimed = await db.run_in_thread(
            db.claim_video_lease, analysis_id, _OWNER, settings.video_lease_ttl_s
        )
        if not claimed:
            log.info("video analysis %d is held by %s; leaving it to that owner", analysis_id, holder or "another process")
            return
        if holder and holder != _OWNER:
            metrics.inc("video_lease_steal_total", "video runs taken over from an owner whose lease had expired")
            log.warning("video analysis %d: took over the run from %s (its lease had expired)", analysis_id, holder)
        run = asyncio.current_task()
        heartbeat = asyncio.get_running_loop().create_task(
            _heartbeat(analysis_id, run), name=f"video-lease-{analysis_id}"
        ) if run is not None else None
        try:
            # A background job waits out a full engine reload (the long
            # window) — nobody is watching a spinner; the lease heartbeat
            # above keeps the row claimed while it waits.
            with recovery_window(settings.llm_recovery_window_s):
                await _run_stages(analysis_id, row)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
            # Released on every exit — done, failed, cancelled — so a restart
            # requeues the row at once instead of after a whole TTL. Owner-
            # scoped in the database: if the lease was taken over meanwhile
            # this is a no-op, never a release of somebody else's run.
            try:
                await asyncio.shield(db.run_in_thread(db.release_video_lease, analysis_id, _OWNER))
            except Exception:  # noqa: BLE001 — the lease expires on its own
                log.debug("video analysis %d: could not release the lease", analysis_id, exc_info=True)


async def _run_stages(analysis_id: int, row: dict) -> None:
    """The stages against one row — under a lease this process holds."""
    old_version = int(row.get("pipeline_version") or 1)
    stale = not is_current(row)
    content_hash = row["content_hash"]
    source = store.source_path(content_hash)
    if not source:
        await _finish(analysis_id, "failed", "the uploaded file is no longer on disk; please attach it again")
        return
    started = time.perf_counter()
    # A stale row keeps its source; what else it keeps is what the version
    # bump said it could. `_RERUN_FOR` names the stages a bump changed, and
    # those (only) are dropped from the row's stage map, so the runner's
    # cache check runs them again and serves the rest from disk. A bump with
    # no entry drops everything: the old outputs are exactly what it said
    # not to trust.
    stages: Dict[str, dict] = dict(row.get("stages") or {})
    counts: Dict[str, Any] = dict(row.get("counts") or {})
    if stale:
        rerun = stages_to_rerun(old_version)
        if rerun is None:
            stages, counts = {}, {}
        else:
            stages = {name: state for name, state in stages.items() if name not in rerun}
            log.info(
                "video analysis %d: v%d -> v%d re-runs %s and keeps %s",
                analysis_id, old_version, PIPELINE_VERSION, ", ".join(sorted(rerun)), ", ".join(sorted(stages)) or "nothing",
            )
    await db.run_in_thread(
        db.update_video_analysis,
        analysis_id,
        status="running",
        error="",
        attempt=int(row.get("attempt") or 0) + 1,
        started_at=db.now(),
        pipeline_version=PIPELINE_VERSION,
        **({"stages": dict(stages), "counts": dict(counts)} if stale else {}),
    )
    ctx = _Ctx(row=row, content_hash=content_hash, source=source, from_version=old_version, counts=counts)
    runner = _Runner(analysis_id, ctx, stages)

    failure = await runner.chain(_PRELUDE)
    if failure is None:
        # The branches run to the end even when one of them fails: the
        # other's stage files are kept, so the retry a re-upload triggers
        # resumes with them instead of paying for them twice.
        failures = await asyncio.gather(*(runner.chain(branch) for branch in _BRANCHES))
        failure = next((f for f in failures if f), None)
    if failure is None:
        failure = await runner.chain(_TAIL)
    if runner.deferred:
        # Deferrals are counted on their own (counts["deferrals"]), not on
        # the row's lifetime `attempt`: a pipeline-version re-run or a
        # re-upload also bumps `attempt`, and must not eat the budget a model
        # outage is allowed.
        deferrals = int((ctx.counts or {}).get("deferrals") or 0) + 1
        if deferrals < max(1, settings.video_max_attempts):
            ctx.counts["deferrals"] = deferrals
            await _defer(analysis_id, runner.deferred, attempt=deferrals, counts=dict(ctx.counts), total_s=time.perf_counter() - started)
            return
        failure = f"{runner.deferred} (gave up after {deferrals} deferrals)"
    if failure:
        await _finish(analysis_id, "failed", failure, total_s=time.perf_counter() - started)
        return
    await _finish(analysis_id, "done", "", total_s=time.perf_counter() - started)


async def _defer(analysis_id: int, why: str, *, attempt: int, counts: Dict[str, Any], total_s: float = 0.0) -> None:
    """Back to the queue with every finished stage kept.

    Idempotent by construction: stage outputs are files keyed by content
    hash and the `stages` map on the row, so the next run re-does only the
    deferred stage; the lease (released by _run's finally) keeps two
    processes from running the same video at once.
    """
    await db.run_in_thread(
        db.update_video_analysis,
        analysis_id,
        status="queued",
        error=f"{DEFERRED_MARK} (attempt {attempt}): {why}"[:1000],
        counts=counts,
        finished_at=None,
    )
    metrics.inc("video_jobs_total", "video analyses finished", result="deferred")
    _publish(analysis_id, {"stage": _DONE, "status": "queued", "detail": f"{DEFERRED_MARK}; will retry", "elapsed_s": round(total_s, 1)})
    # The maintenance tick is 30 minutes apart; VIDEO_RETRY_DELAY_S is the
    # retry delay the operator actually set, so arm a drain for then. The
    # drain itself still honours the delay and the lease, so an early tick
    # or a second timer is harmless.
    try:
        loop = asyncio.get_running_loop()
        loop.call_later(
            max(1.0, float(settings.video_retry_delay_s)),
            lambda: loop.create_task(drain_queue(), name=f"video-redrain-{analysis_id}"),
        )
    except RuntimeError:  # no running loop (tests calling this synchronously)
        pass


async def _finish(analysis_id: int, status: str, error: str, *, total_s: float = 0.0) -> None:
    await db.run_in_thread(
        db.update_video_analysis,
        analysis_id,
        status=status,
        error=error,
        finished_at=db.now(),
        stage="done" if status == "done" else None,
    )
    metrics.inc("video_jobs_total", "video analyses finished", result="ok" if status == "done" else "fail")
    _publish(analysis_id, {"stage": _DONE, "status": status, "detail": error, "elapsed_s": round(total_s, 1)})


@dataclass
class _StageResult:
    status: str  # done | skipped | failed
    detail: str = ""
    row_fields: Dict[str, Any] = field(default_factory=dict)


#: Which file proves a stage's output exists. Stages with no file (index)
#: are trusted from the row alone.
_OUTPUTS = {
    "probe": "probe.json",
    "audio": "audio.wav",
    "transcript": "transcript.json",
    "frames": "frames.json",
    "ocr": "ocr.json",
    "vision": "screen.json",
    "fusion": "understanding.json",
    "index": None,
    "artifacts": "artifacts.json",
}


# ------------------------------------------------------------- stages --


async def _stage_probe(ctx: _Ctx, progress) -> _StageResult:
    from . import media

    probe = await media.probe(ctx.source, timeout_s=60.0)
    if probe.duration_s > settings.video_max_duration_s:
        return _StageResult(
            "failed",
            f"the video is {art.fmt_ts(probe.duration_s)} long; the limit is {art.fmt_ts(settings.video_max_duration_s)}",
        )
    summary = probe.summary()
    store.write_json(store.stage_path(ctx.content_hash, "probe.json"), {**summary, "raw": probe.raw})
    ctx.probe = {**summary, "raw": probe.raw}
    detail = f"{art.fmt_ts(probe.duration_s)}"
    if probe.has_video:
        detail += f" · {probe.width}x{probe.height} {probe.video_codec}"
    detail += f" · {'with' if probe.has_audio else 'NO'} audio"
    return _StageResult(
        "done",
        detail,
        {
            "duration_ms": int(probe.duration_s * 1000),
            "width": probe.width,
            "height": probe.height,
            "has_audio": probe.has_audio,
            "has_video": probe.has_video,
            "media_type": ctx.row.get("media_type") or "",
            "probe": summary,
        },
    )


async def _stage_audio(ctx: _Ctx, progress) -> _StageResult:
    from . import media

    probe = ctx.load_probe()
    if not probe.get("has_audio"):
        return _StageResult("skipped", "the file has no audio track")
    wav = store.stage_path(ctx.content_hash, "audio.wav")
    duration = float(probe.get("duration_s") or 0.0)
    timeout = max(120.0, duration * 0.5 + 60.0)
    samples = await media.extract_audio(ctx.source, wav, timeout_s=timeout, threads=settings.video_ffmpeg_threads)
    ctx.counts["audio_samples"] = int(samples)
    return _StageResult("done", f"{art.fmt_ts(samples / 16000)} of audio")


def _reusable_transcript(ctx: _Ctx) -> Optional[Tuple[List[Segment], Optional[str], dict]]:
    """The stored transcript, when this run may repair it instead of
    transcribing again: this is a version upgrade, the row's files come from
    a version whose engine output means the same thing as today's
    (`_TRANSCRIPT_ENGINE_SINCE`), and the file is there with words in it.
    None means ask the engine — which a current row always does."""
    if not _TRANSCRIPT_ENGINE_SINCE <= ctx.from_version < PIPELINE_VERSION:
        return None
    data = store.read_json(store.stage_path(ctx.content_hash, "transcript.json")) or {}
    raw = data.get("segments") or []
    if not raw:
        return None
    segments = [Segment.from_json(s) for s in raw]
    return segments, (data.get("language") or None), dict(data.get("report") or {})


async def _stage_transcript(ctx: _Ctx, progress) -> _StageResult:
    from . import transcribe as transcribe_mod
    from .transcribe import transcribe_audio

    probe = ctx.load_probe()
    wav = store.stage_path(ctx.content_hash, "audio.wav")
    if not probe.get("has_audio") or not os.path.exists(wav):
        store.write_json(store.stage_path(ctx.content_hash, "transcript.json"), {"language": None, "segments": [], "report": {"reason": "no audio"}})
        ctx.segments, ctx.language, ctx.speech_fraction = [], None, 0.0
        return _StageResult("skipped", "no audio to transcribe")
    if not settings.asr_enabled:
        store.write_json(store.stage_path(ctx.content_hash, "transcript.json"), {"language": None, "segments": [], "report": {"reason": "asr disabled"}})
        ctx.segments, ctx.language, ctx.speech_fraction = [], None, None
        return _StageResult("skipped", "speech-to-text is not enabled on this deployment")
    duration = float(probe.get("duration_s") or 0.0)
    repaired_in_place = _reusable_transcript(ctx)
    if repaired_in_place is not None:
        # A scoped re-run (v2 -> v3): the engine's words are already on disk
        # and only the loops are new. Collapsing them takes milliseconds;
        # asking whisper again would take the recording's length.
        raw_segments, language, old_report = repaired_in_place
        await progress(50.0, "repairing the stored transcript")
        segments, loop_report = loops.collapse(raw_segments)
        language = transcribe_mod.dominant_language(segments) or language
        report = {
            **old_report,
            "segments": len(segments),
            "chars": sum(len(s.text) for s in segments),
            "loops": loop_report,
            "repaired_from_version": ctx.from_version,
        }
    else:
        segments, language, report = await transcribe_audio(
            wav,
            total_s=duration,
            progress=progress,
            max_window_s=min(settings.video_asr_window_s, settings.asr_max_audio_seconds - 5),
            max_gap_s=settings.video_asr_max_gap_s,
            overlap_s=settings.video_asr_overlap_s,
        )
    store.write_json(
        store.stage_path(ctx.content_hash, "transcript.json"),
        {"language": language, "segments": [s.to_json() for s in segments], "report": report},
    )
    ctx.segments, ctx.language = segments, language
    ctx.speech_fraction = report.get("speech_fraction")
    ctx.counts.update({
        "segments": len(segments),
        "transcript_chars": report.get("chars", 0),
        "speech_s": report.get("speech_s"),
        "asr_engine_ms": report.get("engine_ms"),
    })
    if not segments:
        return _StageResult("done", f"no speech detected ({report.get('detector')}, {100 * float(report.get('speech_fraction') or 0):.0f}% speech)", {"language": None})
    detail = f"{len(segments)} segments · {report.get('windows_done')} clip(s) · {language or 'language unknown'}"
    # A transcript repaired in silence is a transcript nobody can audit.
    repaired = loops.describe(report.get("loops") or {})
    if repaired:
        detail += f" · {repaired}"
    return _StageResult("done", detail, {"language": language})


async def _stage_frames(ctx: _Ctx, progress) -> _StageResult:
    from . import frames as fr
    from . import media

    probe = ctx.load_probe()
    if not probe.get("has_video"):
        store.write_json(store.stage_path(ctx.content_hash, "frames.json"), {"frames": [], "report": {"reason": "no video stream"}})
        ctx.frames = []
        return _StageResult("skipped", "the file has no video stream")
    duration = float(probe.get("duration_s") or 0.0)
    cap = fr.frame_cap(duration, per_seconds=settings.video_frame_per_seconds, minimum=settings.video_frame_min, maximum=settings.video_frame_max)
    # The periodic floor is set so that the floor ALONE cannot exceed the cap
    # by more than 3x — the detector adds the rest and dedupe takes it away.
    floor = max(settings.video_frame_floor_min_s, min(settings.video_frame_floor_max_s, duration / max(1, cap * 3)))
    out_dir = store.frames_dir(ctx.content_hash)
    keyframes_only = duration >= settings.video_keyframes_only_after_s
    await progress(5.0, f"scene detection{' (keyframes only)' if keyframes_only else ''} · floor {floor:.0f}s")
    extracted = await media.extract_frames(
        ctx.source,
        out_dir,
        scene_threshold=settings.video_frame_scene_threshold,
        floor_s=floor,
        max_width=settings.video_frame_max_width,
        keyframes_only=keyframes_only,
        timeout_s=max(300.0, duration * 1.5 + 120.0),
        threads=settings.video_ffmpeg_threads,
    )
    await progress(70.0, f"{len(extracted)} candidate frames · hashing")
    kept, report = await asyncio.to_thread(fr.select, extracted, total_s=duration, cap=cap, distance=settings.video_frame_hash_distance)
    keep_paths = {k.path for k in kept}
    for f in extracted:
        if f.path not in keep_paths:
            try:
                os.unlink(f.path)
            except OSError:
                pass
    frames_json = [
        {"file": os.path.basename(k.path), "t": round(k.t_s, 3), "end": round(k.end_s, 3), "phash": int(k.phash), "collapsed": k.collapsed}
        for k in kept
    ]
    store.write_json(store.stage_path(ctx.content_hash, "frames.json"), {"frames": frames_json, "report": {**report, "floor_s": floor, "keyframes_only": keyframes_only}})
    ctx.frames = frames_json
    ctx.counts.update({"frames_extracted": report["extracted"], "frames_distinct": report["distinct"], "frames_kept": report["kept"]})
    return _StageResult("done", f"{report['extracted']} extracted → {report['distinct']} distinct → {report['kept']} kept")


def _kept_frames(ctx: _Ctx):
    from .frames import KeptFrame

    root = store.frames_dir(ctx.content_hash)
    out = []
    for i, f in enumerate(ctx.load_frames()):
        path = os.path.join(root, f["file"])
        if os.path.exists(path):
            out.append(KeptFrame(path=path, t_s=float(f["t"]), end_s=float(f["end"]), index=i, phash=int(f.get("phash") or 0), collapsed=int(f.get("collapsed") or 1)))
    return out


async def _stage_ocr(ctx: _Ctx, progress) -> _StageResult:
    from .screen import read_frames

    kept = _kept_frames(ctx)
    if not kept:
        store.write_json(store.stage_path(ctx.content_hash, "ocr.json"), {"texts": []})
        return _StageResult("skipped", "no frames to read")
    if not settings.ocr_enabled or not settings.video_ocr_enabled:
        store.write_json(store.stage_path(ctx.content_hash, "ocr.json"), {"texts": ["" for _ in kept]})
        return _StageResult("skipped", "OCR is not enabled")
    cap = max(1, settings.video_ocr_max_frames)
    if len(kept) > cap:
        # The longest-held frames are the slides that were actually on
        # screen; flicker and transitions keep their captions only.
        chosen = set(
            k.index for k in sorted(kept, key=lambda k: (-(k.end_s - k.t_s), k.t_s))[:cap]
        )
        subset = [k for k in kept if k.index in chosen]
        read = await read_frames(subset, progress=progress)
        by_index = {k.index: text for k, text in zip(subset, read.texts)}
        texts = [by_index.get(k.index, "") for k in kept]
        note = f" ({cap} of {len(kept)} frames read — the longest-held)"
    else:
        read = await read_frames(kept, progress=progress)
        texts = read.texts
        note = ""
    if read.summary.get("status") == "unavailable":
        # The OCR sidecar answered nothing for any frame. Stamping 'done'
        # here (as this stage did until 2026-09-11) cached the outage as
        # "no readable text" for the life of these bytes; a failed OPTIONAL
        # stage lets the video finish without OCR and re-reads the frames
        # on the next run instead.
        return _StageResult("failed", f"OCR engine unavailable: {str(read.summary.get('detail') or '')[:200]}")
    store.write_json(store.stage_path(ctx.content_hash, "ocr.json"), {"texts": texts})
    legible = sum(1 for t in texts if t.strip())
    ctx.counts.update({"ocr_frames": legible, "ocr_chars": sum(len(t) for t in texts)})
    return _StageResult("done", f"{legible}/{len(kept)} frames had readable text{note}")


async def _stage_vision(ctx: _Ctx, progress) -> _StageResult:
    from .screen import build_spans, caption_frames

    kept = _kept_frames(ctx)
    ocr_data = store.read_json(store.stage_path(ctx.content_hash, "ocr.json")) or {}
    texts = list(ocr_data.get("texts") or [])
    if len(texts) < len(kept):
        texts += ["" for _ in range(len(kept) - len(texts))]
    if not kept:
        store.write_json(store.stage_path(ctx.content_hash, "screen.json"), {"spans": []})
        ctx.spans = []
        return _StageResult("skipped", "no frames to describe")
    captions: List[Optional[str]]
    status, detail = "done", ""
    if settings.video_captions_enabled and _router_enabled():
        captions = await caption_frames(kept, progress=progress)
        described = sum(1 for c in captions if c)
        detail = f"{described}/{len(kept)} frames described by {settings.router_model.split('/')[-1]}"
        if described == 0:
            status = "failed"
            detail = "the vision model described none of the frames"
    else:
        captions = [None for _ in kept]
        status, detail = "skipped", "frame captions are not enabled"
    spans = build_spans(kept, texts, captions)
    store.write_json(store.stage_path(ctx.content_hash, "screen.json"), {"spans": [s.to_json() for s in spans]})
    ctx.spans = spans
    ctx.counts.update({"screen_spans": len(spans), "captions": sum(1 for c in captions if c)})
    return _StageResult(status, detail)


async def _stage_fusion(ctx: _Ctx, progress) -> _StageResult:
    from .fusion import understand

    probe = ctx.load_probe()
    segments = ctx.load_transcript()
    spans = ctx.load_spans()
    u = await understand(
        filename=str(ctx.row.get("filename") or "video"),
        duration_s=float(probe.get("duration_s") or 0.0),
        language=ctx.language,
        has_audio=bool(probe.get("has_audio")),
        speech_fraction=ctx.speech_fraction,
        segments=segments,
        spans=spans,
        progress=progress,
    )
    store.write_json(store.stage_path(ctx.content_hash, "understanding.json"), u.to_json())
    ctx.understanding = u
    return _StageResult(
        "done",
        f"{u.content_type} · {len(u.chapters)} chapters · {len(u.decisions)} decisions · {u.method}",
        {"summary": u.summary, "understanding": u.to_json()},
    )


async def _stage_index(ctx: _Ctx, progress) -> _StageResult:
    from . import index

    segments = ctx.load_transcript()
    spans = ctx.load_spans()
    chunks = index.build_chunks(segments, spans)
    await progress(20.0, f"{len(chunks)} chunks to embed")
    written = await index.index_analysis(int(ctx.row["id"]), chunks)
    ctx.counts["chunks"] = written
    return _StageResult("done", f"{written} evidence chunks indexed", {"indexed_at": db.now(), "chunk_version": index.CHUNKER_VERSION})


async def _stage_artifacts(ctx: _Ctx, progress) -> _StageResult:
    probe = ctx.load_probe()
    segments = ctx.load_transcript()
    spans = ctx.load_spans()
    u = ctx.load_understanding()
    duration = float(probe.get("duration_s") or 0.0)
    title = str(ctx.row.get("filename") or "video")
    out_dir = store.artifacts_dir(ctx.content_hash)
    files = [
        ("transcript_txt", "transcript.txt", "text/plain", art.transcript_txt(segments, title=title)),
        ("transcript_srt", "transcript.srt", "application/x-subrip", art.transcript_srt(segments)),
        ("transcript_vtt", "transcript.vtt", "text/vtt", art.transcript_vtt(segments)),
        ("transcript_json", "transcript.json", "application/json", art.transcript_json(segments, language=ctx.language, duration_s=duration, extra={"source": title})),
        ("screen_text_txt", "screen_text.txt", "text/plain", art.screen_text_txt(spans)),
        ("screen_text_json", "screen_text.json", "application/json", art.screen_text_json(spans)),
        ("summary_md", "summary.md", "text/markdown", art.summary_md(u, title=title, duration_s=duration, language=ctx.language)),
    ]
    written: List[dict] = []
    for kind, name, media_type, body in files:
        if not body.strip() or (kind.startswith("transcript") and not segments) or (kind.startswith("screen") and not any(s.text.strip() for s in spans)):
            continue
        size = await asyncio.to_thread(art.write_text, os.path.join(out_dir, name), body)
        written.append({"kind": kind, "filename": name, "media_type": media_type, "bytes": size})
    store.write_json(store.stage_path(ctx.content_hash, "artifacts.json"), {"artifacts": written})
    return _StageResult("done", f"{len(written)} file(s)", {"artifacts": written})


_STAGE_FNS = {
    "probe": _stage_probe,
    "audio": _stage_audio,
    "transcript": _stage_transcript,
    "frames": _stage_frames,
    "ocr": _stage_ocr,
    "vision": _stage_vision,
    "fusion": _stage_fusion,
    "index": _stage_index,
    "artifacts": _stage_artifacts,
}


# ------------------------------------------------------- lifecycle hooks --


async def start() -> None:
    """Lifespan: put interrupted rows back in the queue, drain it, and keep
    house. Nothing here blocks startup — the drain runs behind the app."""
    global _maintenance
    if not settings.video_analysis_enabled:
        return
    try:
        requeued = await db.run_in_thread(db.requeue_interrupted_video_analyses)
        if requeued:
            log.info("requeued %d video analysis(es) interrupted by a restart", requeued)
    except Exception:  # noqa: BLE001
        log.warning("video: startup reconciliation failed", exc_info=True)
    _maintenance = asyncio.get_running_loop().create_task(_maintenance_loop(), name="video-maintenance")


async def stop() -> None:
    global _maintenance
    if _maintenance is not None:
        _maintenance.cancel()
        _maintenance = None
    pending = [task for task in _tasks.values() if not task.done()]
    for task in pending:
        task.cancel()
    _tasks.clear()
    if pending:
        # Let the runs release their leases before the pool closes behind
        # them (lifespan order): an unreleased lease keeps the next process
        # from requeuing the row for a whole TTL. Bounded — a stage that
        # will not stop within this is abandoned with its lease, which then
        # simply expires.
        await asyncio.wait(pending, timeout=5.0)


async def drain_queue(limit: int = 4) -> int:
    """Start jobs for rows still queued (a restart, or an upload that raced
    the app's startup). Bounded per pass; the semaphore serialises them."""
    started = 0
    try:
        rows = await db.run_in_thread(db.list_video_analyses, "queued", limit)
    except Exception:  # noqa: BLE001
        return 0
    for row in rows:
        if _deferred_too_recently(row):
            continue
        if await ensure_running(int(row["id"])):
            started += 1
    return started


def _deferred_too_recently(row: dict) -> bool:
    """A row the model outage sent back to the queue waits VIDEO_RETRY_DELAY_S
    before its next try, so a still-down engine is not polled by every
    maintenance pass; a new upload is never delayed."""
    if not str(row.get("error") or "").startswith(DEFERRED_MARK):
        return False
    updated = row.get("updated_at")
    if not updated:
        return False
    try:
        from datetime import datetime, timezone

        stamp = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
    except ValueError:
        return False
    return age < float(settings.video_retry_delay_s)


async def reap_orphans() -> int:
    """Delete analyses no conversation refers to any more, after a grace
    period: the same file re-uploaded inside it is a free hit; after it the
    bytes are nobody's."""
    removed = 0
    try:
        rows = await db.run_in_thread(db.orphan_video_analyses, settings.video_orphan_ttl_hours)
    except Exception:  # noqa: BLE001
        return 0
    from . import index

    for row in rows:
        if is_running(int(row["id"])):
            continue
        try:
            await index.delete_analysis(int(row["id"]))
        except Exception:  # noqa: BLE001
            log.warning("video: could not drop index rows for %s", row["id"], exc_info=True)
        await asyncio.to_thread(store.remove_analysis, row["content_hash"])
        await db.run_in_thread(db.delete_video_analysis, int(row["id"]))
        removed += 1
    return removed


async def _maintenance_loop() -> None:
    await asyncio.sleep(5.0)
    while True:
        try:
            await drain_queue()
            reaped = await reap_orphans()
            if reaped:
                log.info("video: reaped %d orphan analysis(es)", reaped)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("video maintenance pass failed", exc_info=True)
        await asyncio.sleep(max(60.0, settings.video_maintenance_interval_s))
