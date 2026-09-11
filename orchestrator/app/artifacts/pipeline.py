"""The job: turn one `artifact_jobs` row into one published version, resumably.

DETACHED FROM THE CHAT TURN, ON PURPOSE — the video pipeline's reasoning
(video/pipeline.py) holds here word for word. A new message cancels the
running generation, so does the Stop button, so does a reload; a document
that takes three model calls and a render takes longer than people wait
before asking something else. The job is therefore a task of its own,
started at acceptance, holding a strong reference, checkpointing every stage
to disk and PostgreSQL. The chat turn SUBSCRIBES to it, forwards its progress
as `step` events, waits, and answers with the card; if that turn dies the
job does not notice.

KEYED BY OWNER + ARTIFACT + VERSION, NOT BY CONTENT HASH. Two people asking
for "a Q3 report" get two artifacts; the same person asking twice in one
turn (a retried POST after a lost acknowledgement) gets the same job through
the idempotency key. There is no upsert-by-hash and no reap-by-attachment:
a version is published once and never removed by this module.

RESUMABLE. Each stage the runner owns writes one output file into the
version's working directory (`v<N>.tmp/`) and stamps itself done on the row
only after the file is durable. A requeued job — a restart, a lapsed lease,
an explicit retry of a failed job — skips every stage whose file exists and
whose stamp says done, so a failure in render never re-asks the model for
the content. `attempt` counts the runs.

THE STAGES. `intent`, `gather` and `outline` happen INSIDE the composer for
v1 (the chat integration supplies it through `set_composer`; it publishes
their step events through `ctx.progress_stage`). The runner itself runs
`compose` (the composer → an ArtifactSpec → spec.json), `render` (a
SUBPROCESS: argument array, scrubbed environment, RLIMIT_AS/RLIMIT_CPU,
wall-clock timeout that kills the process group), `validate` (the render
report reread; every file present, non-empty, under MAX_FILE_BYTES, its
sha256 recomputed), `preview` (page 1 rasterised at both PREVIEW_WIDTHS so
the card has a thumbnail; the rest on demand), then publishes: manifest,
atomic rename, and the version/artifact/job rows in one transaction.

ONE JOB AT A TIME (ARTIFACT_MAX_CONCURRENT_JOBS=1). Rendering is CPU-bound
(python-docx, python-pptx, WeasyPrint, matplotlib) and runs in a child
process, but the compose stage is a model call on the same TP=2 engine chat
uses — so compose paces itself against live generations exactly as a video
stage does (`pace()`), and only compose: a render costs the chat model
nothing.

PROGRESS is fanned out to every subscriber as small dicts `{stage, status,
percent, detail, elapsed_s}` — the video shape, so engines forward them with
STEP_IDS unchanged. Percent ticks stay in memory; only stage transitions
touch the row.

A CANCEL ALWAYS WINS. The API cancels the row and the heartbeat notices
within a third of the lease TTL; in between, every write the runner makes
to the row's status is conditional on the row still being queued/running
(artifacts/db.py), so a cancel that lands before mark_running, before the
publish, or before a failure/deferral is never overwritten — the person who
pressed Cancel is not handed the file. A directory already renamed when the
cancel is noticed stays on disk (a published version is never deleted) but
no row points at it.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import signal
import socket
import sys
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .. import metrics
from .. import db as core_db
from ..asr import ASRBusy, ASRUnavailable
from ..config import settings
from ..resilience import ModelUnavailable, recovery_window
from . import db, store
from . import types as T
from .spec import ArtifactSpec

log = logging.getLogger(__name__)

#: This process, as the lease on a job names it (V29 shape). Host and pid say
#: where; the random tail keeps a pid recycled by a container restart from
#: looking like the process that died — its lease must expire, not renew.
_OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

_DONE = "_done"
#: The prefix a deferred row's `error` carries; the drain uses it to tell a
#: row that is waiting for an engine from one that is simply new.
DEFERRED_MARK = "waiting for the model"
#: How many times a model outage may send one job back to the queue before it
#: is failed as dependency_unavailable, and how long a deferred row waits
#: before its next try. Constants, not settings: the Artifact Studio's
#: operator knobs are the ones listed under "Artifact Studio" in config.py,
#: and a retry policy for an engine reload (13 min measured on 2026-09-10)
#: is not something a deployment tunes.
_MAX_DEFERRALS = 3
_RETRY_DELAY_S = 300.0

#: The stages this module runs itself, in order, and the file that proves
#: each one's output exists — the resume check reads both the stamp on the
#: row and the file on disk.
RUNNER_STAGES: Tuple[str, ...] = ("compose", "render", "validate", "preview")
_OUTPUTS: Dict[str, str] = {
    "compose": T.SPEC_NAME,
    "render": store.RENDER_REPORT_NAME,
    "validate": T.VALIDATION_NAME,
    "preview": store.PREVIEW_META_NAME,
}
#: Which category a stage's UNCATEGORISED failure lands in.
_DEFAULT_CATEGORY: Dict[str, str] = {
    "compose": "model_failure",
    "render": "renderer_failure",
    "validate": "validation_failure",
    "preview": "renderer_failure",
    "publish": "storage_failure",
}
#: What the row says for each category — a sentence for a person. Nothing
#: from an exception ever reaches the row; the log line carries it with the
#: diagnostic_ref.
_SAFE_ERROR: Dict[str, str] = {
    "invalid_request": "The request could not be turned into a document.",
    "source_unavailable": "A source this document needs could not be read.",
    "model_failure": "The content could not be written. Please try again.",
    "renderer_failure": "The file could not be built.",
    "validation_failure": "The file was built but did not pass its checks, so it was not released.",
    "storage_failure": "The file could not be saved to the reports volume.",
    "quota_exceeded": "Your artifact storage is full. Delete an older artifact and try again.",
    "permission_denied": "That artifact is not yours to change.",
    "cancelled": "Cancelled.",
    "dependency_unavailable": "A service this needs is not available right now. Please try again later.",
}


def safe_error(category: str, detail: str = "") -> str:
    base = _SAFE_ERROR.get(category, _SAFE_ERROR["renderer_failure"])
    return f"{base} {detail}".strip() if detail else base


# ---------------------------------------------------------- exceptions --


class ArtifactRefused(Exception):
    """Acceptance refused before any row was written: quota, disk, an
    artifact that is not the caller's. `category` is one of
    types.FAILURE_CATEGORIES; `message` is for a person."""

    def __init__(self, category: str, message: str) -> None:
        self.category = category if category in T.FAILURE_CATEGORIES else "invalid_request"
        self.message = message
        super().__init__(message)


class StageFailure(Exception):
    """A stage failed for a reason it can name. The composer raises this to
    put a category on its failure (`source_unavailable`, `invalid_request`,
    `model_failure`); the runner maps anything else to the stage's default."""

    def __init__(self, category: str, message: str = "") -> None:
        self.category = category if category in T.FAILURE_CATEGORIES else "renderer_failure"
        self.message = message or safe_error(self.category)
        super().__init__(self.message)


class RenderFailed(StageFailure):
    """The render subprocess reported `{error: {category, message}}`, did not
    finish in time, or died. The renderer's own RenderError never crosses
    the process boundary as an object — only as that JSON."""


# ------------------------------------------------------------- hooks --

#: (ctx) -> ArtifactSpec. Installed by the chat integration at startup; the
#: runner never imports the engine (the engine imports the runner).
Composer = Callable[["ComposeContext"], Awaitable[ArtifactSpec]]
_composer: Optional[Composer] = None

#: Max effort only: shown a few rendered pages, returns a REVISED spec when
#: it found layout defects worth a correction, else None. Installed by the
#: engine (compose.visual_review + compose.revise); absent in tests.
VisualReviewer = Callable[["ComposeContext", ArtifactSpec, List[bytes]], Awaitable[Optional[ArtifactSpec]]]
_visual_reviewer: Optional[VisualReviewer] = None

#: How many times the visual loop may re-render one version. One: the
#: brief asks for a bounded loop, and a second pass rarely fixes what the
#: first could not — the corrections are logged and counted either way.
_MAX_VISUAL_PASSES = 1


def set_composer(fn: Optional[Composer]) -> None:
    global _composer
    _composer = fn


def set_visual_reviewer(fn: Optional[VisualReviewer]) -> None:
    global _visual_reviewer
    _visual_reviewer = fn


#: Answers "is somebody chatting right now?" — installed by main.py, because
#: this module must not import main. None = never busy.
_busy_probe: Optional[Callable[[], bool]] = None


def install_busy_probe(fn: Optional[Callable[[], bool]]) -> None:
    global _busy_probe
    _busy_probe = fn


async def pace() -> float:
    """Hold the compose stage while a person is waiting for an answer.

    The video pipeline's policy, applied to the one stage here that touches
    the chat model: before the compose call the job asks whether a chat
    generation is in flight and, if so, waits a second and asks again, up to
    VIDEO_PACE_MAX_WAIT_S (the same cap — it is the same engine and the same
    person waiting). Past the cap the stage runs anyway, so a chatty
    workspace cannot starve a job forever. Returns the seconds waited.
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
        metrics.observe("artifact_pace_seconds", waited, "seconds the compose stage waited for chat to finish")
    return waited


# ------------------------------------------------------------ context --


@dataclass
class _Ctx:
    """Everything a stage may need, loaded lazily from the stage files."""

    job: dict
    work_dir: str
    spec: Optional[ArtifactSpec] = None
    report: Optional[dict] = None
    material: Optional[dict] = None
    parent_spec: Optional[ArtifactSpec] = None
    warnings: List[str] = field(default_factory=list)

    def load_spec(self) -> ArtifactSpec:
        if self.spec is None:
            spec = store.read_spec(self.work_dir)
            if spec is None:
                raise StageFailure("model_failure", "the content of this version is missing")
            self.spec = spec
        return self.spec

    def load_report(self) -> dict:
        if self.report is None:
            data = store.read_json(os.path.join(self.work_dir, store.RENDER_REPORT_NAME))
            if not isinstance(data, dict):
                raise StageFailure("renderer_failure", "the render report is missing")
            self.report = data
        return self.report

    def load_material(self) -> dict:
        if self.material is None:
            data = store.read_json(os.path.join(self.work_dir, store.MATERIAL_NAME))
            self.material = _material(data if isinstance(data, dict) else {})
        return self.material


def _material(data: Optional[dict]) -> dict:
    """The conversation-derived material, with every key present."""
    data = dict(data or {})
    return {
        "history_text": str(data.get("history_text") or ""),
        "sources": list(data.get("sources") or []),
        "tables": list(data.get("tables") or []),
        "uploads_text": str(data.get("uploads_text") or ""),
        "salesforce": data.get("salesforce") if isinstance(data.get("salesforce"), dict) else {},
    }


class ComposeContext:
    """What the composer sees: the job row's fields, the instruction, the
    material, the effort budget, the parent spec for an edit, and two ways
    to report progress. Stages intent/gather/outline are the composer's to
    announce through `progress_stage`; `progress` is a percent tick inside
    compose itself."""

    def __init__(self, ctx: _Ctx, progress: Callable[[Optional[float], str], Awaitable[None]],
                 progress_stage: Callable[[str, str, str], Awaitable[None]]) -> None:
        self._ctx = ctx
        self._progress = progress
        self._progress_stage = progress_stage

    @property
    def job(self) -> dict:
        return self._ctx.job

    @property
    def job_id(self) -> str:
        return str(self._ctx.job["id"])

    @property
    def user_id(self) -> int:
        return int(self._ctx.job["user_id"])

    @property
    def conversation_id(self) -> str:
        return str(self._ctx.job.get("conversation_id") or "")

    @property
    def artifact_id(self) -> str:
        return str(self._ctx.job["artifact_id"])

    @property
    def version(self) -> int:
        return int(self._ctx.job["version"])

    @property
    def operation(self) -> str:
        return str(self._ctx.job.get("operation") or "create")

    @property
    def instruction(self) -> str:
        return str(self._ctx.job.get("instruction") or "")

    @property
    def effort(self) -> str:
        return str(self._ctx.job.get("effort") or "fast")

    @property
    def budget(self) -> T.EffortBudget:
        return T.EFFORT_BUDGETS.get(self.effort, T.EFFORT_BUDGETS["fast"])

    @property
    def mode(self) -> str:
        return str(self._ctx.job.get("mode") or "assistant")

    @property
    def kind(self) -> str:
        return str(self._ctx.job.get("kind") or "document")

    @property
    def formats(self) -> List[str]:
        return list(self._ctx.job.get("selected_formats") or [])

    @property
    def template_id(self) -> str:
        return str(self._ctx.job.get("template_id") or "generic")

    @property
    def title(self) -> str:
        return str(self._ctx.job.get("title") or "")

    @property
    def material(self) -> dict:
        return self._ctx.load_material()

    @property
    def parent_spec(self) -> Optional[ArtifactSpec]:
        return self._ctx.parent_spec

    @property
    def work_dir(self) -> str:
        return self._ctx.work_dir

    def warn(self, text: str) -> None:
        """A correction or caveat worth showing on the card."""
        if text and text not in self._ctx.warnings:
            self._ctx.warnings.append(str(text)[:300])

    async def progress(self, percent: Optional[float], detail: str) -> None:
        await self._progress(percent, detail)

    async def progress_stage(self, stage: str, status: str, detail: str = "") -> None:
        await self._progress_stage(stage, status, detail)


# ------------------------------------------------------------ registry --

_tasks: Dict[str, "asyncio.Task[None]"] = {}
_listeners: Dict[str, List["asyncio.Queue[dict]"]] = {}
#: The last event per job, so a late subscriber (the engine re-subscribing
#: after its 30 s idle check, a status poll) sees where the job is. The
#: video precedent keeps the same map for the life of the process, keyed by
#: a small integer set; artifact job ids are uuids that never repeat, so
#: this one is PRUNED: an entry goes when its last listener leaves after
#: the terminal event, and the map is capped at _LATEST_CAP with the oldest
#: terminal entries evicted first — a job nobody is watching answers from
#: its row.
_latest: "OrderedDict[str, dict]" = OrderedDict()
_LATEST_CAP = 512
_workdirs: Dict[str, str] = {}
_sem: Optional[asyncio.Semaphore] = None
_sem_loop: Optional[asyncio.AbstractEventLoop] = None
_maintenance: Optional["asyncio.Task[None]"] = None


def _semaphore() -> asyncio.Semaphore:
    global _sem, _sem_loop
    loop = asyncio.get_running_loop()
    if _sem is None or _sem_loop is not loop:
        _sem = asyncio.Semaphore(max(1, settings.artifact_max_concurrent_jobs))
        _sem_loop = loop
    return _sem


def reset_for_tests() -> None:
    global _sem, _sem_loop
    _tasks.clear()
    _listeners.clear()
    _latest.clear()
    _workdirs.clear()
    _sem = None
    _sem_loop = None


def subscribe(job_id: str) -> "asyncio.Queue[dict]":
    q: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=256)
    _listeners.setdefault(str(job_id), []).append(q)
    last = _latest.get(str(job_id))
    if last is not None:
        q.put_nowait(last)
    return q


def unsubscribe(job_id: str, q: "asyncio.Queue[dict]") -> None:
    lst = _listeners.get(str(job_id))
    if lst and q in lst:
        lst.remove(q)
    if lst is not None and not lst:
        _listeners.pop(str(job_id), None)
        last = _latest.get(str(job_id))
        if last is not None and last.get("stage") == _DONE:
            _latest.pop(str(job_id), None)


def _remember(job_id: str, event: dict) -> None:
    _latest[job_id] = event
    _latest.move_to_end(job_id)
    if len(_latest) <= _LATEST_CAP:
        return
    for jid in [j for j, e in _latest.items() if e.get("stage") == _DONE and j not in _listeners]:
        if len(_latest) <= _LATEST_CAP:
            break
        _latest.pop(jid, None)
    while len(_latest) > _LATEST_CAP:
        _latest.popitem(last=False)


def _publish(job_id: str, event: dict) -> None:
    event = {**event, "ts": time.time()}
    _remember(str(job_id), event)
    for q in list(_listeners.get(str(job_id), [])):
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


def is_running(job_id: str) -> bool:
    task = _tasks.get(str(job_id))
    return task is not None and not task.done()


async def ensure_running(job_id: str) -> bool:
    """Start the job for this row unless it is running or already terminal.

    One process runs one job once. The row is loaded in a thread, and two
    callers — the maintenance drain and the chat turn, or retry() and the
    engine's 30 s re-kick — can both pass the first `is_running` check and
    both be parked on that await; so it is asked AGAIN after the await,
    before the task is created. With ARTIFACT_MAX_CONCURRENT_JOBS=2 the
    unchecked version claimed the same lease twice (same _OWNER) and ran the
    same stages on the same v<N>.tmp concurrently.
    """
    job_id = str(job_id)
    if is_running(job_id):
        return True
    row = await core_db.run_in_thread(db.load_job, job_id)
    if row is None or row["status"] in T.TERMINAL_STATUSES:
        return False
    if is_running(job_id):
        return True
    task = asyncio.get_running_loop().create_task(_run(job_id), name=f"artifact-job-{job_id[:8]}")
    _tasks[job_id] = task
    task.add_done_callback(lambda t, jid=job_id: _tasks.pop(jid, None) if _tasks.get(jid) is t else None)
    return True


async def wait_for(job_id: str) -> Optional[dict]:
    """Block until the job (if any) finishes; return the fresh row.

    Observes the task without inheriting its outcome: `asyncio.wait` never
    re-raises what the task raised, and in particular never re-raises its
    CancelledError. `await asyncio.shield(task)` did — when the person
    pressed Cancel on the card the job task was cancelled, the shielded
    future was cancelled with it, and the CancelledError went OUT of here
    into the chat turn, which died as if Stop had been pressed and emitted
    no final meta. The caller's OWN cancellation still propagates, because
    `asyncio.wait` raises it in the caller, not the task.
    """
    task = _tasks.get(str(job_id))
    if task is not None:
        await asyncio.wait({task})
    return await core_db.run_in_thread(db.load_job, str(job_id))


# ---------------------------------------------------------- acceptance --


def idempotency_key(user_id: int, conversation_id: str, generation_id: str, operation: str, instruction: str) -> str:
    """CONTRACT §1: sha256(user_id, conversation_id, generation_id or
    intent_id, operation, normalised instruction)."""
    normalised = " ".join((instruction or "").split()).lower()
    raw = "\x1f".join([str(int(user_id)), conversation_id or "", generation_id or "", operation or "", normalised])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_make_key = idempotency_key


def _input_hash(instruction: str, formats: Sequence[str], effort: str, template_id: str, parent: Optional[Tuple[str, int]]) -> str:
    raw = "\x1f".join([" ".join((instruction or "").split()).lower(), ",".join(formats), effort or "", template_id or "", f"{parent[0]}:{parent[1]}" if parent else ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def accept(
    *,
    user_id: int,
    conversation_id: str,
    generation_id: str,
    operation: str,
    instruction: str,
    kind: str,
    formats: Sequence[str],
    format_reason: str = "",
    effort: str = "fast",
    mode: str = "assistant",
    template_id: str = "generic",
    parent: Optional[Tuple[str, int]] = None,
    idempotency_key: str = "",
    requested_formats: Optional[Sequence[str]] = None,
    material: Optional[dict] = None,
    title: str = "",
) -> dict:
    """Persist the request as a job BEFORE any model call. Blocking (call
    through db.run_in_thread from the loop).

    `create` mints a new artifact; `edit` and `convert` add a version to
    `parent`'s artifact, which must be the caller's. The same
    `idempotency_key` answers with the same job — looked up BEFORE the
    quota and free-space checks, so a retried acceptance for a turn whose
    job exists (or already published and pushed the person over quota)
    gets that job back instead of a refusal. Refuses — with no row written
    — when the person is over quota, the volume is short of space, or the
    parent is not theirs (`ArtifactRefused`).

    THE KEY NEEDS A TURN IDENTITY (CONTRACT §1: generation_id or
    intent_id). A caller that supplies neither an explicit key nor a
    generation_id gets a key minted from a uuid: without one, two different
    turns saying "make a pdf" in the same conversation collapsed onto ONE
    job and the second person's request silently returned the first. The
    minted key is recorded on the row like any other; it simply dedupes
    nothing, which is the truth of an acceptance with no turn to retry.

    `material` (history_text, sources, tables, uploads_text, salesforce) is
    written into the version's working directory as material.json so a
    requeued job composes from the same evidence the turn gathered.
    """
    if operation not in ("create", "edit", "convert"):
        raise ArtifactRefused("invalid_request", "unknown operation")
    if kind not in T.KINDS:
        raise ArtifactRefused("invalid_request", "unknown artifact kind")
    formats = [f for f in formats if f in T.FORMATS_FOR_KIND[kind]]
    if not formats:
        raise ArtifactRefused("invalid_request", f"none of the requested formats can be made for a {kind}")
    if operation != "create" and parent is None:
        raise ArtifactRefused("invalid_request", "an edit needs the artifact it edits")
    if parent is not None and not T.is_artifact_id(parent[0]):
        raise ArtifactRefused("permission_denied", safe_error("permission_denied"))

    if idempotency_key:
        key = idempotency_key
    elif generation_id:
        key = _make_key(user_id, conversation_id, generation_id, operation, instruction)
    else:
        key = _make_key(user_id, conversation_id, f"minted-{uuid.uuid4().hex}", operation, instruction)
    existing = db.get_job_by_key(key, int(user_id))
    if existing is not None:
        existing["created"] = False
        return existing

    if not store.free_space_ok():
        raise ArtifactRefused("storage_failure", "The reports volume is out of space.")
    if not store.quota_ok(user_id):
        raise ArtifactRefused("quota_exceeded", safe_error("quota_exceeded"))
    # Work in flight counts too: the quota above sees published bytes only,
    # and a person who could queue fifty Max-effort jobs would hold the one
    # render slot and the shared engine against everyone else (review,
    # 2026-09-11). A small per-person ceiling on queued + running jobs.
    open_jobs = db.count_open_jobs(int(user_id))
    if open_jobs >= max(1, int(settings.artifact_max_open_jobs_per_user)):
        raise ArtifactRefused(
            "quota_exceeded",
            f"You already have {open_jobs} document(s) being built. Wait for them to finish, or cancel one, and try again.",
        )

    job_id = uuid.uuid4().hex
    artifact_id = parent[0] if parent is not None else uuid.uuid4().hex
    row = db.create_artifact_job(
        job_id=job_id,
        artifact_id=artifact_id,
        user_id=int(user_id),
        conversation_id=conversation_id or "",
        generation_id=generation_id or "",
        operation=operation,
        instruction=instruction or "",
        kind=kind,
        requested_formats=list(requested_formats if requested_formats is not None else formats),
        selected_formats=list(formats),
        format_reason=format_reason or "",
        effort=effort or "fast",
        mode=mode or "assistant",
        template_id=template_id or "generic",
        idempotency_key=key,
        parent_version=int(parent[1]) if parent is not None else None,
        title=title or "",
        input_hash=_input_hash(instruction, formats, effort, template_id, parent),
    )
    if row is None or int(row.get("user_id") or 0) != int(user_id):
        # None: the parent is not theirs. A row that is somebody else's: an
        # explicit key another person's acceptance already carries — the
        # owner-scoped lookup above found nothing, and the unscoped insert
        # path answered with theirs. Neither is this caller's job.
        raise ArtifactRefused("permission_denied", safe_error("permission_denied"))
    if row.get("created"):
        try:
            work_dir = store.ensure_workdir(int(user_id), str(row["artifact_id"]), int(row["version"]))
            store.write_json(os.path.join(work_dir, store.MATERIAL_NAME), _material(material))
        except OSError as exc:
            log.warning("artifact job %s: could not write material.json: %s", job_id[:8], type(exc).__name__)
    out = db.load_job(str(row["id"])) or row
    out["created"] = bool(row.get("created"))
    return out


async def cancel(job_id: str, user_id: int) -> Optional[dict]:
    """Owner-scoped, idempotent. A running task is cancelled; a published
    version is never touched. None for a job that is not the caller's."""
    row = await core_db.run_in_thread(db.cancel_job, str(job_id), int(user_id))
    if row is None:
        return None
    task = _tasks.get(str(job_id))
    if row.get("status") == "cancelled" and task is not None and not task.done():
        task.cancel()
    if row.get("status") == "cancelled":
        # Nothing will retry a cancelled job, so its working directory —
        # material.json holds the conversation's text — is not kept.
        work_dir = store.version_workdir(int(user_id), str(row["artifact_id"]), int(row["version"]))
        if str(job_id) not in _workdirs:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(store.remove_workdir, work_dir)
    return row


async def retry(job_id: str, user_id: int) -> Optional[dict]:
    """A failed job, back in the queue for the SAME version; started at once."""
    row = await core_db.run_in_thread(db.retry_job, str(job_id), int(user_id))
    if row is None:
        return None
    if row.get("status") == "queued":
        await ensure_running(str(job_id))
    return row


def ref_for(job: dict, version: Optional[dict] = None) -> T.ArtifactRef:
    """The ArtifactRef for a job row (with `kind`/`title` joined by load_job)
    and, when published, its version row."""
    version = version or {}
    files = [
        T.FileRef(
            format=str(f.get("format")), filename=str(f.get("filename")), mime_type=str(f.get("mime_type") or T.MIME_TYPES.get(str(f.get("format")), "application/octet-stream")),
            size=int(f.get("size") or 0), sha256=str(f.get("sha256") or ""),
            pages=f.get("pages"), slides=f.get("slides"), sheets=f.get("sheets"),
        )
        for f in (version.get("files") or [])
    ]
    return T.ArtifactRef(
        artifact_id=str(job["artifact_id"]),
        version=int(job["version"]),
        job_id=str(job["id"]),
        title=str(version.get("title") or job.get("title") or ""),
        kind=str(version.get("kind") or job.get("kind") or "document"),
        status=str(job.get("status") or "queued"),
        files=files,
        preview_kind=str(version.get("preview_kind") or "none"),
        preview_pages=int(version.get("preview_pages") or 0),
        warnings=list(version.get("warnings") or []),
        created_at=str(job.get("created_at") or ""),
        operation=str(job.get("operation") or "create"),
        parent_version=version.get("parent_version"),
    )


# ---------------------------------------------------------------- the run --


@dataclass
class _StageResult:
    status: str  # done | skipped | failed | deferred
    detail: str = ""
    category: str = ""


#: Margin the compose stage's timeout keeps past the recovery window, so the
#: window's own ModelUnavailable is what ends a waiting stage.
_RECOVERY_MARGIN_S = 60.0


def stage_timeout(stage: str) -> float:
    """The wall-clock a stage gets. ARTIFACT_STAGE_TIMEOUT_S for every stage
    but compose; compose gets AT LEAST the recovery window plus a margin.

    The stages run inside recovery_window(LLM_RECOVERY_WINDOW_S) on purpose:
    a background job waits out a full engine reload (13 min measured on
    2026-09-10) and DEFERS when the window ends without the engine — the
    llm client raises ModelUnavailable only after the WHOLE window. With the
    defaults (600 s stage timeout, 1200 s window) the timeout always won:
    the job was failed as model_failure 'did not finish within 600s' and the
    contracted requeue never fired in production. So the window wins for
    compose: timeout = max(ARTIFACT_STAGE_TIMEOUT_S, LLM_RECOVERY_WINDOW_S +
    60). Render, validate and preview touch no engine and keep the setting.
    """
    base = float(settings.artifact_stage_timeout_s)
    if stage != "compose":
        return base
    return max(base, float(settings.llm_recovery_window_s) + _RECOVERY_MARGIN_S)


class _Runner:
    """One job's stage machinery: the cache check, the timeout, the row
    update and the progress event, identical for every stage."""

    def __init__(self, job_id: str, ctx: _Ctx, stages: Dict[str, dict]) -> None:
        self.job_id = job_id
        self.ctx = ctx
        self.stages = stages
        self.deferred: Optional[str] = None
        self.failure: Optional[Tuple[str, str]] = None
        self.diagnostic_ref = uuid.uuid4().hex[:12]

    async def announce(self, stage: str, status: str, detail: str = "") -> None:
        """A stage the COMPOSER performs (intent/gather/outline): recorded on
        the row and fanned out like the runner's own."""
        if stage not in T.STAGES:
            return
        if status == "running":
            _publish(self.job_id, {"stage": stage, "status": "running", "percent": None, "detail": detail, "elapsed_s": 0})
            await core_db.run_in_thread(db.update_job, self.job_id, stage=stage)
            return
        self.stages[stage] = {"status": status, "ms": 0, "detail": detail}
        progress_row = {**self.ctx.job.get("progress", {}), "stages": dict(self.stages), "detail": detail}
        self.ctx.job["progress"] = progress_row
        await core_db.run_in_thread(db.set_job_progress, self.job_id, progress_row)
        _publish(self.job_id, {"stage": stage, "status": status, "percent": 100, "detail": detail, "elapsed_s": 0})

    async def stage(self, stage: str) -> Optional[str]:
        """Run one stage. Returns the failure to report when the failure is
        fatal for the job, None otherwise (done or cached)."""
        job_id, ctx, stages = self.job_id, self.ctx, self.stages
        state = stages.get(stage) or {}
        output = _OUTPUTS.get(stage)
        cached = state.get("status") == "done" and (
            output is None or os.path.exists(os.path.join(ctx.work_dir, output))
        )
        if cached:
            _publish(job_id, {"stage": stage, "status": "done", "percent": 100, "detail": "from an earlier attempt", "elapsed_s": 0, "cached": True})
            return None
        stage_started = time.perf_counter()
        _publish(job_id, {"stage": stage, "status": "running", "percent": 0, "detail": "", "elapsed_s": 0})
        await core_db.run_in_thread(db.update_job, job_id, stage=stage)

        async def progress(percent: Optional[float], detail: str, _stage=stage, _t0=stage_started) -> None:
            _publish(job_id, {
                "stage": _stage,
                "status": "running",
                "percent": None if percent is None else round(min(100.0, max(0.0, float(percent))), 1),
                "detail": str(detail or ""),
                "elapsed_s": round(time.perf_counter() - _t0, 1),
            })

        timeout = stage_timeout(stage)
        try:
            outcome = await asyncio.wait_for(
                _STAGE_FNS[stage](self, ctx, progress),
                timeout=timeout,
            )
        except asyncio.CancelledError:
            # A shutdown or an explicit cancel. The stage file was never
            # written, so this stage re-runs on the next attempt and nothing
            # before it does.
            raise
        except asyncio.TimeoutError:
            outcome = _StageResult("failed", f"did not finish within {timeout:.0f}s", _DEFAULT_CATEGORY[stage])
        except (ModelUnavailable, ASRUnavailable, ASRBusy) as exc:
            # The engine this stage needs was down for the whole recovery
            # window (the chat model; or whisper, for a composer that
            # transcribes an attached clip — the video precedent's trio).
            # Not this job's fault: DEFER — back to the queue with every
            # finished stage kept — rather than fail.
            log.warning("artifact job %s: stage %s deferred — %s", job_id[:8], stage, exc)
            outcome = _StageResult("deferred", f"{DEFERRED_MARK}: {type(exc).__name__}")
        except StageFailure as exc:
            log.warning("artifact job %s [%s]: stage %s failed: %s: %s", job_id[:8], self.diagnostic_ref, stage, exc.category, exc.message)
            outcome = _StageResult("failed", exc.message, exc.category)
        except Exception:  # noqa: BLE001 — recorded on the row, never a crash
            log.exception("artifact job %s [%s]: stage %s failed", job_id[:8], self.diagnostic_ref, stage)
            category = _DEFAULT_CATEGORY[stage]
            outcome = _StageResult("failed", safe_error(category), category)
        ms = int((time.perf_counter() - stage_started) * 1000)
        stages[stage] = {"status": outcome.status, "ms": ms, "detail": outcome.detail}
        metrics.observe("artifact_stage_seconds", ms / 1000.0, "wall-clock per artifact pipeline stage", stage=stage)
        progress_row = {**ctx.job.get("progress", {}), "stages": dict(stages), "detail": outcome.detail, "elapsed_s": round(ms / 1000.0, 1)}
        ctx.job["progress"] = progress_row
        await core_db.run_in_thread(db.set_job_progress, job_id, progress_row)
        _publish(job_id, {"stage": stage, "status": outcome.status, "percent": 100, "detail": outcome.detail, "elapsed_s": round(ms / 1000.0, 1)})
        if outcome.status == "deferred":
            self.deferred = f"{T.STAGE_TITLES.get(stage, stage)}: {outcome.detail}"
            return self.deferred
        if outcome.status == "failed":
            self.failure = (outcome.category or _DEFAULT_CATEGORY[stage], outcome.detail)
            return f"{T.STAGE_TITLES.get(stage, stage)}: {outcome.detail}"
        return None

    async def chain(self, names: Sequence[str]) -> Optional[str]:
        for name in names:
            failure = await self.stage(name)
            if failure:
                return failure
        return None


async def _heartbeat(job_id: str, run: "asyncio.Task[None]") -> None:
    """Renew the lease every third of its TTL while the run lasts.

    A renewal that FAILS means another process took the row over after this
    one's lease lapsed; two runs would now race for the same working
    directory, so this one stands down. A row that turned `cancelled` under
    us (the API in another process) stops the run the same way.
    """
    ttl = float(settings.artifact_lease_ttl_s)
    interval = max(1.0, ttl / 3.0)
    while True:
        await asyncio.sleep(interval)
        try:
            renewed = await core_db.run_in_thread(db.claim_lease, job_id, _OWNER, ttl)
            row = await core_db.run_in_thread(db.load_job, job_id) if renewed else None
        except Exception:  # noqa: BLE001 — the next beat retries
            log.warning("artifact job %s: lease heartbeat failed", job_id[:8], exc_info=True)
            continue
        if not renewed:
            log.warning("artifact job %s: the lease was taken by another owner; standing down", job_id[:8])
            run.cancel()
            return
        if row is not None and row.get("status") == "cancelled":
            log.info("artifact job %s: cancelled by its owner; stopping", job_id[:8])
            run.cancel()
            return


async def _run(job_id: str) -> None:
    job_id = str(job_id)
    async with _semaphore():
        row = await core_db.run_in_thread(db.load_job, job_id)
        if row is None:
            return
        if row["status"] in T.TERMINAL_STATUSES:
            _publish(job_id, {"stage": _DONE, "status": row["status"], "detail": row.get("error") or ""})
            return
        # THE LEASE (V29 shape). Claimed before any stage runs and renewed
        # while they do. A claim fails only while a DIFFERENT owner's lease
        # is live — that process is alive and has the job — and then this
        # run leaves it. A claim that takes over an EXPIRED lease is counted:
        # that is a process that died mid-run.
        holder = await core_db.run_in_thread(db.lease_holder, job_id)
        claimed = await core_db.run_in_thread(db.claim_lease, job_id, _OWNER, settings.artifact_lease_ttl_s)
        if not claimed:
            log.info("artifact job %s is held by %s; leaving it to that owner", job_id[:8], holder or "another process")
            return
        if holder and holder != _OWNER:
            metrics.inc("artifact_lease_steal_total", "artifact runs taken over from an owner whose lease had expired")
            log.warning("artifact job %s: took over the run from %s (its lease had expired)", job_id[:8], holder)
        run = asyncio.current_task()
        heartbeat = asyncio.get_running_loop().create_task(
            _heartbeat(job_id, run), name=f"artifact-lease-{job_id[:8]}"
        ) if run is not None else None
        if (_latest.get(job_id) or {}).get("stage") == _DONE:
            # A requeued job's last event was its deferral or failure; this
            # attempt starts fresh so a late subscriber is not told "done".
            _latest.pop(job_id, None)
        try:
            # A background job waits out a full engine reload (the long
            # window); the heartbeat keeps the row claimed while it waits.
            with recovery_window(settings.llm_recovery_window_s):
                await _run_stages(job_id, row)
        except Exception:  # noqa: BLE001 — a failure OUTSIDE a stage (a stage-boundary row write, publication)
            # Without this the task died, the lease was released, the row
            # stayed 'running' with no error and nobody was told (review,
            # 2026-09-11). Record it if the database will let us, and ALWAYS
            # end the subscribers' wait — the chat turn and the card must
            # not spin on a job that no longer exists.
            ref = uuid.uuid4().hex[:12]
            log.exception("artifact job %s [%s]: the runner failed outside a stage", job_id[:8], ref)
            try:
                await _finish(job_id, "failed", "storage_failure", safe_error("storage_failure"), diagnostic_ref=ref)
            except Exception:  # noqa: BLE001 — the row write is what failed; the event still goes out
                metrics.inc("artifact_jobs_total", "artifact jobs finished", result="failed")
                _publish(job_id, {"stage": _DONE, "status": "failed", "detail": safe_error("storage_failure")})
        except asyncio.CancelledError:
            fresh = None
            try:
                fresh = await core_db.run_in_thread(db.load_job, job_id)
            except Exception:  # noqa: BLE001
                pass
            already_done = (_latest.get(job_id) or {}).get("stage") == _DONE
            if fresh is not None and fresh.get("status") == "cancelled" and not already_done:
                metrics.inc("artifact_jobs_total", "artifact jobs finished", result="cancelled")
                _publish(job_id, {"stage": _DONE, "status": "cancelled", "detail": safe_error("cancelled")})
            raise
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
            _workdirs.pop(job_id, None)
            # Released on every exit so a restart requeues the row at once
            # instead of after a whole TTL. Owner-scoped: never somebody
            # else's lease.
            try:
                await asyncio.shield(core_db.run_in_thread(db.release_lease, job_id, _OWNER))
            except Exception:  # noqa: BLE001 — the lease expires on its own
                log.debug("artifact job %s: could not release the lease", job_id[:8], exc_info=True)


async def _visual_qa(runner: "_Runner", ctx: "_Ctx", stages: Dict[str, dict], progress: Dict[str, Any]) -> Optional[str]:
    """Max effort: look at the rendered pages before releasing them.

    A few pages of the preview PDF (ARTIFACT_QA_PAGES, downscaled to
    ARTIFACT_QA_WIDTH) go to the vision-capable model through the installed
    reviewer; when it returns a revised spec, the spec is rewritten and the
    render / validate / preview stages run again — once. Bounded by
    `_MAX_VISUAL_PASSES`; every pass is counted; a reviewer that fails is a
    warning, never a failed job (the file it looked at is a good file).
    Returns a failure string only when the RE-RENDER fails.
    """
    job = ctx.job
    budget = T.EFFORT_BUDGETS.get(str(job.get("effort") or "fast"), T.EFFORT_BUDGETS["fast"])
    if not budget.visual_qa or _visual_reviewer is None:
        return None
    passes = int(progress.get("visual_passes") or 0)
    if passes >= _MAX_VISUAL_PASSES:
        return None
    report = ctx.load_report()
    if str(report.get("preview_kind") or "none") != "pages":
        return None
    pdf = _preview_pdf_path(ctx.work_dir, report.get("preview_pdf"))
    if pdf is None or not os.path.isfile(pdf):
        return None
    try:
        count = min(int(await asyncio.to_thread(_page_count, pdf)), int(settings.artifact_qa_pages))
        pages = [await asyncio.to_thread(_rasterise_page, pdf, i, int(settings.artifact_qa_width)) for i in range(1, count + 1)]
    except Exception as exc:  # noqa: BLE001
        log.warning("artifact job %s: visual QA could not rasterise: %s", runner.job_id[:8], type(exc).__name__)
        return None
    if not pages:
        return None
    _publish(runner.job_id, {"stage": "validate", "status": "running", "percent": None, "detail": f"looking at {len(pages)} page(s)", "elapsed_s": 0})
    spec = ctx.load_spec()
    ccx = ComposeContext(ctx, progress=lambda pct, detail: asyncio.sleep(0), progress_stage=lambda stage, status, detail="": asyncio.sleep(0))
    try:
        revised = await asyncio.wait_for(_visual_reviewer(ccx, spec, pages), timeout=stage_timeout("compose"))
    except Exception as exc:  # noqa: BLE001 — the file is fine; the second look is what failed
        log.warning("artifact job %s: visual QA failed: %s", runner.job_id[:8], type(exc).__name__)
        ctx.warnings.append("the visual check could not run")
        return None
    progress["visual_passes"] = passes + 1
    await core_db.run_in_thread(db.set_job_progress, runner.job_id, {**progress, "warnings": ctx.warnings})
    if revised is None:
        _publish(runner.job_id, {"stage": "validate", "status": "done", "percent": 100, "detail": "layout checked", "elapsed_s": 0})
        return None
    metrics.inc("artifact_corrections_total", "artifact correction passes", stage="visual")
    # The files on disk are a GOOD version. The revision is rendered next to
    # them, and only replaces them once it has rendered, validated and
    # previewed; if it cannot, the good version is published with a note —
    # a correction must never turn a finished document into a failed job
    # (review, 2026-09-11).
    kept = os.path.join(ctx.work_dir, ".before-visual-qa")
    saved: List[str] = []
    try:
        os.makedirs(kept, exist_ok=True)
        for name in os.listdir(ctx.work_dir):
            src = os.path.join(ctx.work_dir, name)
            if os.path.isfile(src) and name != store.MATERIAL_NAME:
                os.replace(src, os.path.join(kept, name))
                saved.append(name)
        previews = os.path.join(ctx.work_dir, T.PREVIEWS_DIR)
        if os.path.isdir(previews):
            os.replace(previews, os.path.join(kept, T.PREVIEWS_DIR))
    except OSError as exc:
        log.warning("artifact job %s: could not set the visual QA aside: %s", runner.job_id[:8], type(exc).__name__)
        with contextlib.suppress(OSError):
            for name in saved:
                os.replace(os.path.join(kept, name), os.path.join(ctx.work_dir, name))
        return None
    await asyncio.to_thread(store.write_spec, ctx.work_dir, revised)
    original_spec, original_report = ctx.spec, ctx.report
    ctx.spec, ctx.report = revised, None
    before = {name: dict(state) for name, state in stages.items()}
    for name in ("render", "validate", "preview"):
        stages.pop(name, None)
    failure = await runner.chain(("render", "validate", "preview"))
    if failure is None and not runner.deferred:
        ctx.warnings.append("the layout was corrected after a visual check")
        with contextlib.suppress(OSError):
            import shutil

            shutil.rmtree(kept, ignore_errors=True)
        return None
    # The revision could not be built: put the good version back, exactly
    # as it was, and say what happened. Nothing about the job fails.
    log.warning("artifact job %s: the visual correction could not be rendered (%s); keeping the reviewed version", runner.job_id[:8], failure or "deferred")
    with contextlib.suppress(OSError):
        for name in os.listdir(ctx.work_dir):
            path = os.path.join(ctx.work_dir, name)
            if os.path.isfile(path) and name != store.MATERIAL_NAME:
                os.unlink(path)
        stale_previews = os.path.join(ctx.work_dir, T.PREVIEWS_DIR)
        if os.path.isdir(stale_previews):
            import shutil

            shutil.rmtree(stale_previews, ignore_errors=True)
        for name in os.listdir(kept):
            os.replace(os.path.join(kept, name), os.path.join(ctx.work_dir, name))
        os.rmdir(kept)
    ctx.spec, ctx.report = original_spec, original_report
    stages.clear()
    stages.update(before)
    runner.deferred = None
    runner.failure = None
    ctx.warnings.append("a visual correction was attempted but could not be applied; the reviewed version is what was published")
    return None


async def _run_stages(job_id: str, row: dict) -> None:
    """The stages against one row — under a lease this process holds."""
    started = time.perf_counter()
    user_id, artifact_id, version = int(row["user_id"]), str(row["artifact_id"]), int(row["version"])
    progress: Dict[str, Any] = dict(row.get("progress") or {})
    stages: Dict[str, dict] = dict(progress.get("stages") or {})
    moved = await core_db.run_in_thread(db.mark_running, job_id, int(row.get("attempt") or 0) + 1)
    if moved is None:
        # Cancelled (or completed elsewhere) between the load and this
        # write: the API already answered for it, and the row says so.
        fresh = await core_db.run_in_thread(db.load_job, job_id)
        status = str((fresh or {}).get("status") or "cancelled")
        if status == "cancelled":
            metrics.inc("artifact_jobs_total", "artifact jobs finished", result="cancelled")
        _publish(job_id, {"stage": _DONE, "status": status, "detail": (fresh or {}).get("error") or ""})
        return
    # A previous attempt renamed the directory and died before the rows
    # were marked: the version IS published, and only the bookkeeping is
    # owed. Nothing is rendered twice.
    if store.is_published(user_id, artifact_id, version):
        try:
            status = await _complete_from_dir(job_id, row, store.version_dir(user_id, artifact_id, version), [])
        except Exception:  # noqa: BLE001
            log.exception("artifact job %s: could not record an already-published version", job_id[:8])
            await _finish(job_id, "failed", "storage_failure", safe_error("storage_failure"), total_s=time.perf_counter() - started)
            return
        await _finish(job_id, status, "", "", total_s=time.perf_counter() - started)
        return
    if not store.free_space_ok():
        await _finish(job_id, "failed", "storage_failure", "The reports volume is out of space.", total_s=time.perf_counter() - started)
        return
    try:
        work_dir = await asyncio.to_thread(store.ensure_workdir, user_id, artifact_id, version)
    except OSError:
        log.exception("artifact job %s: could not create the working directory", job_id[:8])
        await _finish(job_id, "failed", "storage_failure", safe_error("storage_failure"), total_s=time.perf_counter() - started)
        return
    _workdirs[job_id] = work_dir
    ctx = _Ctx(job=row, work_dir=work_dir)
    ctx.warnings = list(progress.get("warnings") or [])
    if row.get("operation") in ("edit", "convert"):
        ctx.parent_spec = await asyncio.to_thread(_parent_spec, row)
    runner = _Runner(job_id, ctx, stages)

    failure = await runner.chain(RUNNER_STAGES)
    if failure is None and not runner.deferred:
        failure = await _visual_qa(runner, ctx, stages, progress)
    if runner.deferred:
        deferrals = int(progress.get("deferrals") or 0) + 1
        if deferrals < _MAX_DEFERRALS:
            await _defer(job_id, runner.deferred, deferrals=deferrals, progress={**ctx.job.get("progress", {}), "deferrals": deferrals, "warnings": ctx.warnings}, total_s=time.perf_counter() - started)
            return
        failure = f"{runner.deferred} (gave up after {deferrals} deferrals)"
        runner.failure = ("dependency_unavailable", safe_error("dependency_unavailable"))
    if failure:
        category, detail = runner.failure or ("renderer_failure", failure)
        await core_db.run_in_thread(db.set_job_progress, job_id, {**ctx.job.get("progress", {}), "warnings": ctx.warnings})
        await _finish(job_id, "failed", category, detail, diagnostic_ref=runner.diagnostic_ref, total_s=time.perf_counter() - started)
        return

    # PUBLISH. Manifest, atomic rename, rows — the only stage that touches
    # the published tree, and the one after which nothing is undone.
    publish_started = time.perf_counter()
    try:
        validation = store.read_json(os.path.join(work_dir, T.VALIDATION_NAME)) or {}
        preview_meta = store.read_json(os.path.join(work_dir, store.PREVIEW_META_NAME)) or {}
        report = ctx.load_report()
        spec = ctx.load_spec()
        manifest = store.build_manifest(
            artifact_id=artifact_id, version=version, files=list(validation.get("files") or []),
            spec_version=int(spec.spec_version), template_version=T.TEMPLATE_VERSION, renderer_version=T.RENDERER_VERSION,
            preview=preview_meta if isinstance(preview_meta, dict) else {},
            warnings=_merge_warnings(report.get("warnings"), validation.get("warnings"), ctx.warnings),
        )
        # The renderer's chart PNGs are embedded in the files; the scratch
        # JSON is this process's (store docstring). Neither is published.
        scratch = [str(name) for name in (report.get("chart_files") or []) if name]
        final = await asyncio.to_thread(store.publish, work_dir, manifest, scratch=scratch)
        status = await _complete_from_dir(job_id, row, final, ctx.warnings)
    except store.StorageError as exc:
        log.error("artifact job %s [%s]: publish failed: %s", job_id[:8], runner.diagnostic_ref, exc)
        await _finish(job_id, "failed", "storage_failure", safe_error("storage_failure"), diagnostic_ref=runner.diagnostic_ref, total_s=time.perf_counter() - started)
        return
    except Exception:  # noqa: BLE001
        log.exception("artifact job %s [%s]: publish failed", job_id[:8], runner.diagnostic_ref)
        await _finish(job_id, "failed", "storage_failure", safe_error("storage_failure"), diagnostic_ref=runner.diagnostic_ref, total_s=time.perf_counter() - started)
        return
    metrics.observe("artifact_stage_seconds", time.perf_counter() - publish_started, "wall-clock per artifact pipeline stage", stage="publish")
    await _finish(job_id, status, "", "", total_s=time.perf_counter() - started)


def _parent_spec(row: dict) -> Optional[ArtifactSpec]:
    """The spec of the version an edit starts from: the named parent, else
    the artifact's current version. None when there is none on disk."""
    user_id, artifact_id = int(row["user_id"]), str(row["artifact_id"])
    version_row = db.get_version(artifact_id, int(row["version"]), user_id) or {}
    parent = version_row.get("parent_version")
    if parent is None:
        art = db.get_artifact(artifact_id, user_id) or {}
        parent = int(art.get("current_version") or 0)
    if not parent:
        return None
    try:
        return store.read_spec(store.version_dir(user_id, artifact_id, int(parent)))
    except ValueError:
        return None


def _merge_warnings(*lists: Any) -> List[str]:
    """Every warning once, in first-seen order, each cut to a card-sized line."""
    out: List[str] = []
    for lst in lists:
        for text in list(lst or []):
            line = str(text or "")[:300]
            if line and line not in out:
                out.append(line)
    return out


async def _complete_from_dir(job_id: str, row: dict, directory: str, warnings: List[str]) -> str:
    """Record a published directory on the rows. Returns the status —
    'completed', 'completed_with_warnings', or 'cancelled' when the job was
    cancelled meanwhile and the rows were left alone (db.publish_version).

    Reads only what a published directory holds: the manifest carries the
    preview meta and the warnings the scratch files had before publication,
    so the crash window between the rename and the row update is closed
    from the directory alone."""
    validation = store.read_json(os.path.join(directory, T.VALIDATION_NAME)) or {}
    manifest = store.read_json(os.path.join(directory, T.MANIFEST_NAME)) or {}
    preview = manifest.get("preview") if isinstance(manifest.get("preview"), dict) else {}
    spec = store.read_spec(directory)
    files = list(validation.get("files") or [])
    all_warnings = _merge_warnings(manifest.get("warnings"), validation.get("warnings"), warnings)
    assumptions = list(getattr(spec.body, "assumptions", []) or []) if spec is not None else []
    status = await core_db.run_in_thread(
        db.publish_version,
        str(row["artifact_id"]), int(row["version"]), job_id,
        files=files,
        validation={k: v for k, v in validation.items() if k != "files"},
        warnings=all_warnings,
        assumptions=assumptions,
        preview_kind=str(preview.get("preview_kind") or "none"),
        preview_pages=int(preview.get("preview_pages") or 0),
        title=spec.title if spec is not None else "",
        spec_version=int(spec.spec_version) if spec is not None else 1,
        template_id=str(getattr(spec.body, "template_id", "") or "") if spec is not None else None,
        template_version=T.TEMPLATE_VERSION,
        renderer_version=T.RENDERER_VERSION,
    )
    return str(status)


#: The states the runner may move a row OUT of. A row that is anything else
#: (cancelled by the API while a stage ran) keeps what the API wrote.
_RUNNER_MAY_LEAVE = ("queued", "running")


async def _defer(job_id: str, why: str, *, deferrals: int, progress: dict, total_s: float = 0.0) -> None:
    """Back to the queue with every finished stage kept."""
    moved = await core_db.run_in_thread(
        db.set_job_status, job_id, "queued", error=f"{DEFERRED_MARK} (attempt {deferrals}): {why}"[:1000],
        only_from=_RUNNER_MAY_LEAVE,
    )
    if not moved:
        await _publish_row_state(job_id, total_s)
        return
    await core_db.run_in_thread(db.update_job, job_id, progress=progress)
    metrics.inc("artifact_jobs_total", "artifact jobs finished", result="deferred")
    _publish(job_id, {"stage": _DONE, "status": "queued", "detail": f"{DEFERRED_MARK}; will retry", "elapsed_s": round(total_s, 1)})
    try:
        loop = asyncio.get_running_loop()
        loop.call_later(
            _RETRY_DELAY_S,
            lambda: loop.create_task(drain_queue(), name=f"artifact-redrain-{job_id[:8]}"),
        )
    except RuntimeError:  # no running loop (tests calling this synchronously)
        pass


async def _publish_row_state(job_id: str, total_s: float) -> None:
    """The runner lost a write to a state the API set meanwhile (a cancel):
    tell the listeners what the row says, count it, and stop."""
    fresh = await core_db.run_in_thread(db.load_job, job_id)
    status = str((fresh or {}).get("status") or "cancelled")
    if status == "cancelled":
        metrics.inc("artifact_jobs_total", "artifact jobs finished", result="cancelled")
    _publish(job_id, {"stage": _DONE, "status": status, "detail": (fresh or {}).get("error") or "", "elapsed_s": round(total_s, 1)})


async def _finish(job_id: str, status: str, category: str, error: str, *, diagnostic_ref: str = "", total_s: float = 0.0) -> None:
    if status == "failed":
        moved = await core_db.run_in_thread(
            db.set_job_status, job_id, "failed",
            error=error, failure_category=category or "renderer_failure", diagnostic_ref=diagnostic_ref,
            only_from=_RUNNER_MAY_LEAVE,
        )
        if not moved:
            await _publish_row_state(job_id, total_s)
            return
        metrics.inc("artifact_jobs_total", "artifact jobs finished", result="fail")
    elif status == "cancelled":
        # publish_version found the job cancelled inside the last heartbeat
        # interval and wrote nothing; the directory stays (never deleted),
        # the rows say what the API answered.
        metrics.inc("artifact_jobs_total", "artifact jobs finished", result="cancelled")
        error = error or safe_error("cancelled")
    else:
        # publish_version already wrote the terminal status in its transaction.
        metrics.inc("artifact_jobs_total", "artifact jobs finished", result="ok")
    _publish(job_id, {"stage": _DONE, "status": status, "detail": error, "elapsed_s": round(total_s, 1)})


# ------------------------------------------------------------- stages --


async def _stage_compose(runner: _Runner, ctx: _Ctx, progress) -> _StageResult:
    if _composer is None:
        raise StageFailure("dependency_unavailable", "no composer is installed")
    waited = await pace()
    if waited:
        await progress(None, f"waited {waited:.0f}s for chat to finish")
    compose_ctx = ComposeContext(ctx, progress, runner.announce)
    spec = await _composer(compose_ctx)
    if not isinstance(spec, ArtifactSpec):
        raise StageFailure("model_failure", "the composer did not return a document")
    if spec.kind != str(ctx.job.get("kind") or spec.kind):
        raise StageFailure("model_failure", f"the composer wrote a {spec.kind}, not a {ctx.job.get('kind')}")
    await asyncio.to_thread(store.write_spec, ctx.work_dir, spec)
    ctx.spec = spec
    body = spec.body
    count = len(getattr(body, "blocks", None) or getattr(body, "slides", None) or getattr(body, "sheets", None) or [])
    unit = {"document": "block", "presentation": "slide", "workbook": "sheet"}[spec.kind]
    return _StageResult("done", f"{spec.title!r} · {count} {unit}{'s' if count != 1 else ''}")


async def _stage_render(runner: _Runner, ctx: _Ctx, progress) -> _StageResult:
    spec = ctx.load_spec()
    formats = [f for f in (ctx.job.get("selected_formats") or []) if f in T.FORMATS_FOR_KIND[spec.kind]]
    if not formats:
        raise StageFailure("invalid_request", f"no format this {spec.kind} can be rendered to was selected")
    title_slug = T.slug_for(spec.title)
    await progress(5.0, f"{', '.join(formats)} · {settings.artifact_render_timeout_s:.0f}s budget")
    render_started = time.perf_counter()
    report = await _render_in_subprocess(ctx.work_dir, spec, formats, title_slug, int(ctx.job["version"]), str(ctx.job.get("effort") or "fast"))
    if not isinstance(report, dict):
        raise RenderFailed("renderer_failure", safe_error("renderer_failure"))
    report = dict(report)
    report.setdefault("title_slug", title_slug)
    report.setdefault("formats", list(formats))
    await asyncio.to_thread(store.write_json, os.path.join(ctx.work_dir, store.RENDER_REPORT_NAME), report)
    ctx.report = report
    elapsed = time.perf_counter() - render_started
    timings = report.get("timings") if isinstance(report.get("timings"), dict) else {}
    for fmt in formats:
        metrics.observe("artifact_render_seconds", float(timings.get(fmt, elapsed)), "seconds to render one artifact format", format=fmt)
    for text in report.get("warnings") or []:
        if text and text not in ctx.warnings:
            ctx.warnings.append(str(text)[:300])
    return _StageResult("done", f"{len(report.get('files') or [])} file(s) in {elapsed:.1f}s")


async def _stage_validate(runner: _Runner, ctx: _Ctx, progress) -> _StageResult:
    report = ctx.load_report()
    spec = ctx.load_spec()
    selected = [f for f in (ctx.job.get("selected_formats") or []) if f in T.FORMATS_FOR_KIND[spec.kind]]
    checked = await asyncio.to_thread(_validate_files, ctx.work_dir, report, selected, spec.title, int(ctx.job["version"]))
    problems: List[str] = checked["problems"]
    if problems:
        raise StageFailure("validation_failure", safe_error("validation_failure", problems[0]))
    # The render report is scratch (store.SCRATCH_NAMES): what it says
    # about the files lives on in validation.json, which is published.
    validation = {
        **{k: v for k, v in (report.get("validation") or {}).items()},
        "files": checked["files"],
        "warnings": _merge_warnings(report.get("warnings"), checked["warnings"]),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    await asyncio.to_thread(store.write_json, os.path.join(ctx.work_dir, T.VALIDATION_NAME), validation)
    for text in checked["warnings"]:
        if text and text not in ctx.warnings:
            ctx.warnings.append(str(text)[:300])
    detail = " · ".join(f"{f['format']} {f['size'] // 1024} KB" for f in checked["files"])
    return _StageResult("done", detail)


def _validate_files(work_dir: str, report: dict, selected: Sequence[str], title: str, version: int) -> dict:
    """Blocking: reopen-by-stat every file the render report names. The
    renderer already reopened them structurally (page counts, zip parts);
    this is the runner's independent check that what it is about to publish
    is there, non-empty, within the ceiling, hashes to what was claimed —
    and is NAMED what the contract names it, types.download_name(title,
    version, fmt). The version row records the filename verbatim and
    store.resolve_version_file refuses a name whose extension is not the
    format, so a report naming e.g. 'spec.json.bak' for pdf used to publish
    a COMPLETED version whose download and inline URLs 404'd."""
    files: List[dict] = []
    problems: List[str] = []
    warnings: List[str] = []
    seen_formats: List[str] = []
    real_root = os.path.realpath(work_dir)
    for entry in report.get("files") or []:
        if not isinstance(entry, dict):
            continue
        fmt = str(entry.get("format") or "")
        filename = os.path.basename(str(entry.get("filename") or ""))
        if fmt not in T.FORMATS or not filename:
            problems.append(f"the renderer named a file it may not write ({fmt or 'unknown format'})")
            continue
        expected = T.download_name(title, version, fmt)
        if filename != expected:
            problems.append(f"the {fmt} file is not named {expected}")
            continue
        path = os.path.realpath(os.path.join(work_dir, filename))
        if not path.startswith(real_root + os.sep):
            problems.append(f"the {fmt} file resolves outside the working directory")
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            problems.append(f"the {fmt} file is missing")
            continue
        if size <= 0:
            problems.append(f"the {fmt} file is empty")
            continue
        if size > T.MAX_FILE_BYTES:
            problems.append(f"the {fmt} file is {size // (1024 * 1024)} MB; the ceiling is {T.MAX_FILE_BYTES // (1024 * 1024)} MB")
            continue
        digest = store.sha256_file(path)
        claimed = str(entry.get("sha256") or "")
        if claimed and claimed != digest:
            problems.append(f"the {fmt} file changed after it was rendered")
            continue
        ref = T.FileRef(
            format=fmt, filename=filename, mime_type=T.MIME_TYPES.get(fmt, "application/octet-stream"),
            size=int(size), sha256=digest,
            pages=entry.get("pages"), slides=entry.get("slides"), sheets=entry.get("sheets"),
        )
        files.append(ref.to_json())
        seen_formats.append(fmt)
    for fmt in selected:
        if fmt not in seen_formats:
            problems.append(f"the {fmt} file was not produced")
    preview_kind = str(report.get("preview_kind") or "none")
    preview_pdf = report.get("preview_pdf")
    if preview_kind == "pages":
        pdf = _preview_pdf_path(work_dir, preview_pdf)
        if pdf is None or not os.path.isfile(pdf) or os.path.getsize(pdf) <= 0:
            warnings.append("no page preview could be made for this version")
    return {"files": files, "problems": problems, "warnings": warnings}


def _preview_pdf_path(work_dir: str, preview_pdf: Any) -> Optional[str]:
    """The render report's preview.pdf, verified inside the working
    directory. The renderer writes it under its fixed name; a report that
    points elsewhere is refused, not followed."""
    real_root = os.path.realpath(work_dir)
    candidate = os.path.join(work_dir, T.PREVIEW_PDF_NAME)
    if preview_pdf:
        given = str(preview_pdf)
        candidate = given if os.path.isabs(given) else os.path.join(work_dir, given)
    real = os.path.realpath(candidate)
    if not real.startswith(real_root + os.sep):
        return None
    return real


async def _stage_preview(runner: _Runner, ctx: _Ctx, progress) -> _StageResult:
    report = ctx.load_report()
    preview_kind = str(report.get("preview_kind") or "none")
    preview_pages = 0
    thumbnails: List[str] = []
    if preview_kind == "pages":
        pdf = _preview_pdf_path(ctx.work_dir, report.get("preview_pdf"))
        if pdf is not None and os.path.isfile(pdf) and os.path.getsize(pdf) > 0:
            try:
                preview_pages = min(int(await asyncio.to_thread(_page_count, pdf)), T.MAX_PREVIEW_PAGES)
            except Exception as exc:  # noqa: BLE001 — a preview problem is a warning, not a failure
                log.warning("artifact job %s: preview page count failed: %s", runner.job_id[:8], type(exc).__name__)
                preview_pages = 0
            if preview_pages:
                for width in T.PREVIEW_WIDTHS:
                    try:
                        png = await asyncio.to_thread(_rasterise_page, pdf, 1, int(width))
                        dest = os.path.join(ctx.work_dir, T.PREVIEWS_DIR, f"1-{int(width)}.png")
                        await asyncio.to_thread(store.write_bytes, dest, png)
                        thumbnails.append(os.path.basename(dest))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("artifact job %s: thumbnail at %d px failed: %s", runner.job_id[:8], width, type(exc).__name__)
                        ctx.warnings.append("the card's thumbnail could not be made")
                        break
        else:
            preview_kind = "none"
            ctx.warnings.append("no page preview could be made for this version")
    elif preview_kind == "grid":
        preview_pages = int(report.get("preview_pages") or 0)
    else:
        preview_kind = "none"
    meta = {"preview_kind": preview_kind, "preview_pages": preview_pages, "thumbnails": thumbnails}
    await asyncio.to_thread(store.write_json, os.path.join(ctx.work_dir, store.PREVIEW_META_NAME), meta)
    if preview_kind == "pages":
        return _StageResult("done", f"{preview_pages} page(s) · thumbnail {'ready' if thumbnails else 'missing'}")
    return _StageResult("done", "grid preview" if preview_kind == "grid" else "no preview for this kind")


_STAGE_FNS = {
    "compose": _stage_compose,
    "render": _stage_render,
    "validate": _stage_validate,
    "preview": _stage_preview,
}


# --------------------------------------------------------- the renderer --


def _page_count(pdf_path: str) -> int:
    from .render.preview import page_count  # lazy: pypdfium2

    return int(page_count(pdf_path))


def _rasterise_page(pdf_path: str, page: int, width: int) -> bytes:
    from .render.preview import rasterise_page  # lazy: pypdfium2 + PIL

    return rasterise_page(pdf_path, page, width)


#: The orchestrator root — what `python -m app.artifacts.render.worker`
#: needs on PYTHONPATH when its cwd is the working directory.
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ENV_KEYS = ("PATH", "HOME", "LANG", "PYTHONPATH", "MPLCONFIGDIR", "FONTCONFIG_FILE")


def render_env(work_dir: str) -> Dict[str, str]:
    """The child's environment: PATH/HOME/LANG/PYTHONPATH/MPLCONFIGDIR/
    FONTCONFIG_FILE and nothing else — no DSN, no API key, no token reaches
    a process that parses model-written content. matplotlib's cache goes in
    the working directory so a read-only HOME cannot fail a render."""
    env: Dict[str, str] = {}
    for key in _ENV_KEYS:
        value = os.environ.get(key)
        if value:
            env[key] = value
    env["PYTHONPATH"] = _APP_ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("HOME", work_dir)
    env["MPLCONFIGDIR"] = os.path.join(work_dir, ".mpl")
    return env


def _limits(memory_mb: int, cpu_s: float) -> Callable[[], None]:
    """preexec_fn: RLIMIT_AS and RLIMIT_CPU for the child. A limit the
    process may not raise (a hard cap below ours) is left where it is."""
    def apply() -> None:
        import resource

        try:
            cap = int(memory_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
        except (ValueError, OSError):
            pass
        try:
            secs = max(1, int(cpu_s))
            resource.setrlimit(resource.RLIMIT_CPU, (secs, secs))
        except (ValueError, OSError):
            pass

    return apply


def _kill_group(proc: "asyncio.subprocess.Process") -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def _render_in_subprocess(work_dir: str, spec: ArtifactSpec, formats: Sequence[str], title_slug: str, version: int, effort: str) -> dict:
    """Run `python -m app.artifacts.render.worker <job.json>` in a child
    process and return its render report as a dict.

    Argument array, cwd = the working directory, scrubbed environment,
    RLIMIT_AS = ARTIFACT_RENDER_MEMORY_MB, RLIMIT_CPU and a wall-clock
    timeout of ARTIFACT_RENDER_TIMEOUT_S that kills the whole process group
    (start_new_session so the children of a renderer die with it). Tests
    replace this function; nothing else in the module knows how a render
    happens.
    """
    job_path = os.path.join(work_dir, store.JOB_NAME)
    report_path = os.path.join(work_dir, store.RENDER_REPORT_NAME)
    await asyncio.to_thread(store.write_json, job_path, {
        "spec": spec.model_dump(mode="json", exclude_none=True),
        "formats": list(formats),
        "out_dir": work_dir,
        "title_slug": title_slug,
        "version": int(version),
        "effort": effort,
    })
    try:
        os.unlink(report_path)
    except OSError:
        pass
    timeout = float(settings.artifact_render_timeout_s)
    argv = [sys.executable, "-m", "app.artifacts.render.worker", job_path]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=work_dir,
        env=render_env(work_dir),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=_limits(settings.artifact_render_memory_mb, timeout),
        start_new_session=True,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        _kill_group(proc)
        await proc.wait()
        raise RenderFailed("renderer_failure", f"the files were not built within {timeout:.0f}s")
    except asyncio.CancelledError:
        _kill_group(proc)
        raise
    report = store.read_json(report_path)
    error = report.get("error") if isinstance(report, dict) else None
    if isinstance(error, dict):
        raise RenderFailed(str(error.get("category") or "renderer_failure"), str(error.get("message") or safe_error("renderer_failure")))
    if proc.returncode != 0 or not isinstance(report, dict):
        tail = (stderr or b"")[-2000:].decode("utf-8", "replace")
        if proc.returncode is not None and proc.returncode < 0:
            log.error("artifact render worker killed by signal %d (rlimit?): %s", -proc.returncode, tail)
            raise RenderFailed("renderer_failure", "the renderer ran out of memory or time")
        log.error("artifact render worker exited %s: %s", proc.returncode, tail)
        raise RenderFailed("renderer_failure", safe_error("renderer_failure"))
    return report


# ------------------------------------------------------- lifecycle hooks --


async def start() -> None:
    """Lifespan: put interrupted rows back in the queue, drain it, and keep
    house. Nothing here blocks startup — the drain runs behind the app."""
    global _maintenance
    if not settings.artifacts_enabled:
        # Off: no jobs run, but what an enabled deployment left in working
        # directories is still swept on the same schedule.
        _maintenance = asyncio.get_running_loop().create_task(_sweep_only_loop(), name="artifact-sweep")
        return
    try:
        requeued = await core_db.run_in_thread(db.requeue_lapsed)
        if requeued:
            log.info("requeued %d artifact job(s) interrupted by a restart", requeued)
    except Exception:  # noqa: BLE001
        log.warning("artifacts: startup reconciliation failed", exc_info=True)
    _maintenance = asyncio.get_running_loop().create_task(_maintenance_loop(), name="artifact-maintenance")


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
        # them. Bounded — a stage that will not stop within this is
        # abandoned with its lease, which then simply expires.
        await asyncio.wait(pending, timeout=5.0)


async def drain_queue(limit: int = 4) -> int:
    """Start jobs for rows still queued (a restart, an acceptance that raced
    startup, a deferral whose delay has passed). Bounded per pass; the
    semaphore serialises them."""
    started = 0
    try:
        rows = await core_db.run_in_thread(db.list_jobs, "queued", limit)
    except Exception:  # noqa: BLE001
        return 0
    for row in rows:
        if _deferred_too_recently(row):
            continue
        if await ensure_running(str(row["id"])):
            started += 1
    return started


def _deferred_too_recently(row: dict) -> bool:
    """A row a model outage sent back waits _RETRY_DELAY_S before its next
    try, so a still-down engine is not polled by every maintenance pass; a
    new acceptance is never delayed."""
    if not str(row.get("error") or "").startswith(DEFERRED_MARK):
        return False
    updated = row.get("updated_at")
    if not updated:
        return False
    try:
        stamp = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
    except ValueError:
        return False
    return age < _RETRY_DELAY_S


async def sweep() -> int:
    """Remove abandoned working directories older than the TTL — never one
    a job in this process is writing, never one a QUEUED job still needs
    (its material.json is what it will compose from), never a published
    version."""
    keep = set(_workdirs.values())
    ttl_s = float(settings.artifact_tmp_ttl_hours) * 3600.0
    try:
        for job in await core_db.run_in_thread(db.list_jobs, "queued", 500):
            created = job.get("created_at")  # ISO text from _job_row
            try:
                created_dt = datetime.fromisoformat(str(created).replace("Z", "+00:00")) if created else None
            except ValueError:
                created_dt = None
            if created_dt is not None and created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - created_dt).total_seconds() if created_dt else 0.0
            if age > ttl_s:
                # Queued for longer than the TTL: nothing is going to run it
                # (the drain would have). Say so on the row, and let its
                # working directory go with the others.
                await core_db.run_in_thread(
                    db.set_job_status, str(job["id"]), "failed",
                    error="This document was never built. Please ask for it again.",
                    failure_category="dependency_unavailable", completed=True,
                )
                continue
            keep.add(store.version_workdir(int(job["user_id"]), str(job["artifact_id"]), int(job["version"])))
    except Exception:  # noqa: BLE001 — a listing that fails keeps the sweep conservative
        return 0
    return await asyncio.to_thread(store.sweep_abandoned, settings.artifact_tmp_ttl_hours, skip=keep)


async def _sweep_only_loop() -> None:
    await asyncio.sleep(5.0)
    while True:
        try:
            await sweep()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("artifact sweep failed", exc_info=True)
        await asyncio.sleep(max(60.0, settings.artifact_maintenance_interval_s))


async def _maintenance_loop() -> None:
    await asyncio.sleep(5.0)
    while True:
        try:
            # A row left 'running' by a process that died (or a runner that
            # failed before it could write) comes back to the queue here,
            # every pass — not only at the next restart.
            lapsed = await core_db.run_in_thread(db.requeue_lapsed)
            if lapsed:
                log.warning("artifacts: requeued %d job(s) whose lease had lapsed", lapsed)
            await drain_queue()
            removed = await sweep()
            if removed:
                log.info("artifacts: swept %d abandoned working directory(ies)", removed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("artifact maintenance pass failed", exc_info=True)
        await asyncio.sleep(max(60.0, settings.artifact_maintenance_interval_s))


__all__ = [
    "ArtifactRefused", "StageFailure", "RenderFailed", "ComposeContext", "DEFERRED_MARK", "RUNNER_STAGES",
    "set_composer", "install_busy_probe", "pace", "stage_timeout", "idempotency_key", "accept", "cancel", "retry", "ref_for",
    "ensure_running", "is_running", "wait_for", "subscribe", "unsubscribe",
    "start", "stop", "drain_queue", "sweep", "reset_for_tests", "render_env", "safe_error",
]
