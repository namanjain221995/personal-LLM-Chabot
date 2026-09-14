"""The processing runner: durable jobs per blob, under leases (design §4.2).

A BLOB, NOT A FILE, IS THE UNIT OF WORK. Two files of one project over the
same bytes are processed once. The database row is the queue (`queue.
claim_due_blobs`: `queued`, or `processing` with a lapsed lease, at most one
per project per sweep); nothing lives only in memory, so a restart loses at
most the unit in flight.

LANES. `cpu` runs PUBLIC_API_FILES_CPU_JOBS (2) at once: text kinds, images,
and the `assemble` stage of uploads (`queue.assemble_claimed`, claimed in the
same slots so an assembly and an extraction never both take a slot's memory).
`media` runs PUBLIC_API_FILES_MEDIA_JOBS (1): audio and video through the
video pipeline's `api` lane (`apifiles/media.py`). Each child may take 8 GiB
of address space on memory the model engines share, which is why the lanes
are small (design §8, R5).

STAGES ARE DURABLE FILES. Every stage writes its outputs under `derived/`
atomically, THEN its marker `derived/stages/<stage>.json` (status, ms, facts),
THEN the row. On resume a stage whose marker exists is not run again — its
facts come from the marker — so a restart resumes at the first stage without
a file (A-6: "completed stages are not re-run, stage ms unchanged"). A crash
between an output and its marker re-runs that one stage: the cheap direction.

UNIT BOUNDARIES (`JobContext.checkpoint`). Between pages, OCR units, embed
calls and pipeline progress ticks the job checks, at most once a second: the
blob is not `deleting` (a DELETE stops the job, no error), the lease is still
ours (another process took over), the process is not shutting down. Every row
write the job makes is guarded by `lease_owner` — a job that lost its lease
cannot overwrite the new holder's stages.

WHAT ENDS A RUN.
* success → `processed`, facts, derived names, `derived_bytes`, one usage row,
  `file.processed` webhooks;
* a verdict about the FILE (`extractors.ExtractError`: unsupported, corrupt,
  too complex) → `failed` with that code, `file.failed` webhooks;
* the EMBEDDING ENGINE is down, or its state is unknown → deferred WITHOUT
  spending an attempt, retried with exponential backoff (15 s doubling, capped
  at the 300 s retry delay) for as long as the outage lasts (2026-09-14; the
  rule, its exceptions and the one trade-off — an engine that dies under a
  blob's first batch spends that blob an attempt — are `apifiles/outage.py`);
* the engine REFUSES US (401/403/404 and other 4xx: a wrong model name, a
  rotated key) or fails in a way that is not a proven transport failure →
  a counted deferral, below: misconfiguration must end in a visible failure,
  not a queue that never drains;
* the engine is proven SERVING while this blob's call failed, or the ENGINE
  or the DISK is not there in a way that can be about the blob (an OCR batch
  with no success, disk under the watermark, ENOSPC in the child, a child
  killed from outside, a sandbox that cannot be applied) → deferred: back to
  `queued`, finished stages kept, `attempt + 1`, not before now + 300 s; the
  fifth counted deferral fails `processing_unavailable` (`queue.defer_blob`)
  — except that OCR on the last attempt completes with the text layers
  instead, so an OCR outage never fails a file either;
* shutdown → released to `queued` at once (no attempt counted), so the next
  process resumes without waiting 90 s for the lease to lapse;
* a DELETE or a lost lease → the job stops and writes nothing more.

CRASH LOOPS ARE BOUNDED. A run records `progress.running_owner` when it
starts and clears it on every graceful exit. A claim that finds it still set
means the previous run died with its process (an OOM kill, a parser bug that
took the orchestrator down); after PUBLIC_API_FILES_PROCESSING_MAX_ATTEMPTS
such takeovers the blob fails `internal_error` instead of being re-claimed
forever — a key that uploads a crash file costs at most five runs.

WHAT THE WIRE SEES (`processing_view`). Stage names from the fixed step table
(`sniff.STAGES_BY_KIND`), their status and percent, allow-listed numeric
facts, and one fixed sentence on failure. Never an engine name, a URL, a path
or exception text: `processing_view` never reads a free-text field, so a
progress `detail` written anywhere upstream cannot reach a caller.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import db
from . import events, limits, outage, queue, schema, sniff, storage
from .extractors import (
    ERROR_CODES,
    ExtractError,
    FileCorrupt,
    FileTooComplex,
    Spec,
    Unsupported,
    atomic_write_json,
    find_source,
    sentence_for,
)

log = logging.getLogger(__name__)

APIFILES_PIPELINE_VERSION = 1

STAGES_DIR = "stages"
TEXT_KINDS = ("pdf", "document", "presentation", "text", "html")
TABLE_KINDS = ("spreadsheet", "tabular")
MEDIA_KINDS = ("audio", "video")
#: The pipeline stages one media driver run covers (design §6.2).
MEDIA_PIPELINE_STEPS = ("probe", "audio", "transcript", "frames", "ocr", "vision", "fusion", "artifacts")

#: Stages that write derived data: the watermark is checked before each
#: (finding #17). `assemble` checks it inside `queue.assemble_claimed`.
DERIVED_WRITING = frozenset({"text", "sheets", "ocr", "variants", "chunk", "index", "probe"})

#: How often a job re-reads its row's status at a unit boundary.
STATUS_CHECK_S = 1.0
#: Lease renewal cadence (design §4.2: every 30 s against a 90 s lease).
RENEW_S = 30.0
#: Retry cadence after a failed renewal (a database blip).
RENEW_RETRY_S = 5.0
#: The claim sweep when nothing wakes the runner.
POLL_S = 5.0
#: How often a media job polls its analysis row (design §6.2.4).
MEDIA_POLL_S = 5.0


# ================================================================ errors ==


class StopJob(Exception):
    """Stop at this unit boundary, write nothing: `deleting`, `lease_lost`
    or `shutdown`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Deferred(Exception):
    """The engine or the disk is unavailable: defer, keep finished stages.

    `counted=False` (an engine outage, `apifiles/outage.py`) re-queues the
    blob with backoff and does not spend one of its attempts."""

    def __init__(self, message: str = "", *, counted: bool = True, kind: Optional[str] = None) -> None:
        super().__init__(message)
        self.counted = bool(counted)
        self.kind = kind


class ProcessingUnavailable(ExtractError):
    """A service stayed unavailable past its own retries (the video pipeline
    already spent its deferrals): terminal, not a verdict on the bytes."""

    code = "processing_unavailable"


# ============================================================ the stages ==


@dataclass
class StageResult:
    status: str = "done"  # done | skipped
    facts: Dict[str, Any] = field(default_factory=dict)
    #: Further steps this stage completed (the media driver covers probe…artifacts).
    covers: Tuple[str, ...] = ()


StageFn = Callable[["JobContext"], Awaitable[StageResult]]


class JobContext:
    """What a stage function gets: the blob, its paths, and the runner's
    boundary, progress and child-process helpers."""

    def __init__(self, runner: "JobRunner", blob: Dict[str, Any]) -> None:
        self.runner = runner
        self.blob = blob
        self.blob_id = str(blob["id"])
        self.project_id = str(blob["project_id"])
        self.sha256 = str(blob["sha256"])
        self.kind = str(blob.get("kind") or "unknown")
        self.mime_type = str(blob.get("mime_type") or "application/octet-stream")
        self.facts: Dict[str, Any] = dict(_json_field(blob.get("facts")))
        self.stages: Dict[str, Any] = dict(_json_field(blob.get("stages")))
        self.progress_state: Dict[str, Any] = dict(_json_field(blob.get("progress")))
        self.lost = False
        #: Set when the job must stop while it may be waiting at a capacity
        #: gate (lease lost, blob being deleted): the patient gates have no
        #: clock, so this is what ends their wait (`capacity.Abandoned`).
        self.abandon = asyncio.Event()
        self.stop_reason: Optional[str] = None
        self._last_status_check = 0.0
        self.waited_for_capacity_s = float(self.progress_state.get("waited_for_capacity_s") or 0.0)
        self.current_stage: Optional[str] = None
        self.started = time.monotonic()

    # -- paths ------------------------------------------------------------

    @property
    def blob_dir(self) -> str:
        return storage.blob_dir(self.project_id, self.sha256)

    @property
    def derived_dir(self) -> str:
        return storage.derived_dir(self.project_id, self.sha256)

    @property
    def original_path(self) -> str:
        return storage.original_path(self.project_id, self.sha256)

    @property
    def source(self) -> str:
        found = find_source(self.derived_dir)
        if not found:
            raise FileCorrupt()
        return found

    @property
    def steps(self) -> Tuple[str, ...]:
        return tuple(sniff.STAGES_BY_KIND.get(self.kind) or sniff.STAGES_BY_KIND["unknown"])

    # -- the unit boundary ------------------------------------------------

    async def checkpoint(self) -> None:
        if self.runner.stopping:
            self.stop_reason = "shutdown"
            raise StopJob("shutdown")
        if self.lost:
            self.stop_reason = "lease_lost"
            raise StopJob("lease_lost")
        now = time.monotonic()
        if now - self._last_status_check < STATUS_CHECK_S:
            return
        self._last_status_check = now
        row = await db.run_in_thread(_lease_state, self.blob_id)
        if row is None or row.get("status") == "deleting":
            self.stop_reason = "deleting"
            raise StopJob("deleting")
        if row.get("lease_owner") != self.runner.owner or row.get("status") != "processing":
            self.lost = True
            self.stop_reason = "lease_lost"
            raise StopJob("lease_lost")

    async def should_continue(self) -> bool:
        """The boolean form `vectors.build_index` takes."""
        try:
            await self.checkpoint()
        except StopJob:
            return False
        return True

    # -- progress ---------------------------------------------------------

    async def progress(self, stage: str, percent: Optional[float], *, force: bool = False) -> None:
        pct = None if percent is None else int(max(0, min(100, round(float(percent)))))
        entry = dict(self.stages.get(stage) or {})
        entry.update({"status": "running"})
        if pct is not None:
            entry["percent"] = pct
        self.stages[stage] = entry
        event = {"stage": stage, "percent": pct, "kind": self.kind}
        events.publish(self.blob_id, event)
        if self.runner.throttle.should_write(self.blob_id, force=force):
            await db.run_in_thread(
                _update_held,
                self.blob_id,
                self.runner.owner,
                stage=stage,
                stages=self.stages,
                progress=self._progress_payload(stage, pct),
            )

    def _progress_payload(self, stage: str, pct: Optional[int]) -> Dict[str, Any]:
        steps = self.steps
        payload = dict(self.progress_state)
        payload.update(
            {
                "stage": stage,
                "step": steps.index(stage) + 1 if stage in steps else 0,
                "total_steps": len(steps),
                "percent": pct,
                "waited_for_capacity_s": round(self.waited_for_capacity_s, 1),
                "running_owner": self.runner.owner,
            }
        )
        self.progress_state = payload
        return payload

    # -- the extraction child ---------------------------------------------

    def caps(self) -> Dict[str, Any]:
        kind_caps = limits.kind_caps(self.kind)
        return {
            "bytes": kind_caps.bytes,
            "pages": kind_caps.pages,
            "ocr_pages": kind_caps.ocr_pages,
            "pixels": kind_caps.pixels,
            "cpu_s": kind_caps.cpu_s,
            "rlimit_as_bytes": limits.extract_rlimit_as_bytes(),
            "fsize_bytes": _fsize_for(self.blob),
            "thin_page_chars": _thin_page_chars(),
            "html_readable_bytes": limits.html_readable_max_bytes(),
        }

    async def worker(self, op: str, *, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from . import extract_worker

        await self.checkpoint()
        spec = Spec(op=op, kind=self.kind, derived_dir=self.derived_dir, source=self.source, caps=self.caps(), args=dict(args or {}))
        try:
            return await extract_worker.run(spec, wall_s=limits.kind_caps(self.kind).wall_s)
        except extract_worker.RetryableWorkerError as exc:
            raise Deferred("the extraction child could not write (disk full)") from exc


def _fsize_for(blob: Mapping[str, Any]) -> int:
    from . import extract_worker

    return extract_worker.fsize_limit(int(blob.get("bytes") or 0))


def _thin_page_chars() -> int:
    try:
        from ..engines.document import TEXT_OK_CHARS

        return int(TEXT_OK_CHARS)
    except Exception:  # noqa: BLE001
        return 200


# ------------------------------------------------------------ stage fns --


async def stage_sniff(ctx: JobContext) -> StageResult:
    """Magic bytes decide the kind (`sniff.detect_path`), then `derived/src.<ext>`
    is hard-linked to `original` so every extractor opens a suffixed path."""
    original = ctx.original_path
    if not os.path.exists(original):
        raise FileCorrupt()
    detection = await asyncio.to_thread(sniff.detect_path, original, mime_hint=ctx.mime_type)
    await asyncio.to_thread(storage.makedirs, ctx.derived_dir)
    if detection.ext:
        await asyncio.to_thread(sniff.link_source, original, ctx.derived_dir, detection.ext)
    ctx.kind = detection.kind if detection.kind in sniff.KINDS else "unknown"
    ctx.mime_type = detection.mime_type
    return StageResult(facts={"ext": detection.ext})


async def stage_text(ctx: JobContext) -> StageResult:
    return StageResult(facts=await ctx.worker("extract"))


async def stage_ocr(ctx: JobContext) -> StageResult:
    from . import ocr_pages, render
    from ..publicapi import capacity
    from ..publicapi.errors import ApiError

    caps = ctx.caps()

    async def do_render(pages: Sequence[int]) -> Dict[int, str]:
        await ctx.checkpoint()
        return await render.render_pages(ctx.derived_dir, ctx.source, pages, caps=caps, wall_s=limits.kind_caps("pdf").wall_s)

    async def progress(pct: int) -> None:
        await ctx.progress("ocr", pct)

    budget = limits.kind_caps("pdf").ocr_pages or 0
    # `queue.defer_blob` fails the blob when attempt + 1 reaches the ceiling:
    # on that attempt a deferral would throw away every text layer, so the
    # stage completes with what it could read (review finding, 2026-09-13).
    final_attempt = int(ctx.blob.get("attempt") or 0) + 1 >= ctx.runner.max_attempts
    try:
        facts = await ocr_pages.run_stage(
            ctx.derived_dir,
            budget=int(budget),
            render=do_render,
            read=ctx.runner.ocr_reader,
            gate=ctx.runner.ocr_gate or (lambda: ocr_pages.default_gate(abandon=ctx.abandon)),
            concurrency=2,
            checkpoint=ctx.checkpoint,
            progress=progress,
            final_attempt=final_attempt,
        )
    except capacity.Abandoned:
        raise StopJob(await ctx.runner._why_not_held(ctx)) from None
    except ocr_pages.EngineUnavailable as exc:
        # Counted on purpose: on the last attempt the stage completes with the
        # text layers, so the count bounds how long a PDF waits for a missing
        # OCR engine (~25 min) and never fails the file (ocr_pages docstring).
        raise Deferred("OCR unavailable") from exc
    except ApiError as exc:
        raise Deferred("OCR gate refused") from exc
    return StageResult(facts=facts)


async def stage_sheets(ctx: JobContext) -> StageResult:
    return StageResult(facts=await ctx.worker("sheets"))


async def stage_profile(ctx: JobContext) -> StageResult:
    return StageResult(facts=await ctx.worker("profile"))


async def stage_decode(ctx: JobContext) -> StageResult:
    facts = await ctx.worker("decode")
    facts.pop("alpha", None)
    return StageResult(facts=facts)


async def stage_variants(ctx: JobContext) -> StageResult:
    facts = await ctx.worker("variants")
    return StageResult(facts={"variants": list(facts.get("variants") or [])})


async def stage_chunk(ctx: JobContext) -> StageResult:
    from . import chunks

    count = await asyncio.to_thread(chunks.build_text_chunks, ctx.derived_dir)
    return StageResult(facts={"chunks": int(count)})


async def stage_index(ctx: JobContext) -> StageResult:
    """Vectors over chunks.jsonl (team INDEX's `vectors.build_index`). For
    audio/video the chunks are built here first, from the analysis's
    transcript and screen text (`chunks.chunk_media`, design §6.3)."""
    from . import chunks, vectors
    from ..publicapi import capacity

    if ctx.kind in MEDIA_KINDS and not os.path.exists(os.path.join(ctx.derived_dir, chunks.CHUNKS_NAME)):
        content_hash = storage.api_video_hash(ctx.project_id, ctx.sha256)
        from ..video import store as video_store

        transcript = await asyncio.to_thread(video_store.read_json, video_store.stage_path(content_hash, "transcript.json"))
        screen = await asyncio.to_thread(video_store.read_json, video_store.stage_path(content_hash, "screen.json"))
        rows = chunks.chunk_media(transcript, screen)
        await asyncio.to_thread(chunks.write_chunks, rows, ctx.derived_dir)

    progressed = False

    async def on_progress(done: int, total: int) -> None:
        nonlocal progressed
        progressed = True  # called only after a batch of vectors was written
        await ctx.progress("index", 100.0 * done / max(1, total))

    embed = ctx.runner.embed_documents or _engine_embedder(ctx)
    try:
        info = await vectors.build_index(
            ctx.derived_dir,
            embed_documents=embed,
            should_continue=ctx.should_continue,
            on_progress=on_progress,
        )
    except asyncio.CancelledError:
        if ctx.stop_reason:
            raise StopJob(ctx.stop_reason) from None
        raise
    except capacity.Abandoned:
        raise StopJob(await ctx.runner._why_not_held(ctx)) from None
    except Exception as exc:  # noqa: BLE001 — classified below; our own bugs re-raise
        kind = outage.classify(exc)
        if kind == outage.KIND_NOT_ENGINE:
            if isinstance(exc, (NotImplementedError, RecursionError, FileNotFoundError)):
                # Our bug, not an outage: retrying it for 25 minutes and then
                # calling it `processing_unavailable` would hide it (review,
                # 2026-09-13).
                raise
            if isinstance(exc, RuntimeError):
                raise Deferred("the embedding engine answered wrongly") from exc
            if isinstance(exc, (ConnectionError, TimeoutError)):
                # Bare builtins: the SDK wraps every engine transport failure
                # in its own error, so this came from our side of the call.
                # Counted, so it ends instead of retrying for ever.
                raise Deferred(f"a connection outside the engine call failed ({type(exc).__name__})") from exc
            if isinstance(exc, OSError):
                raise Deferred("the vector file could not be written") from exc
            raise
        try:
            probe = await _probe_embed(embed) if outage.needs_probe(kind) else None
        except capacity.Abandoned:
            # Raised inside this handler, so the `except capacity.Abandoned`
            # above cannot see it: without this a job abandoned while its
            # probe waited at the gate failed `internal_error`.
            raise StopJob(await ctx.runner._why_not_held(ctx)) from None
        counted = outage.counts_attempt(kind, probe=probe, progressed=progressed)
        raise Deferred(f"the embedding engine is unavailable ({kind}, probe {probe})", counted=counted, kind=kind) from exc
    facts: Dict[str, Any] = {"chunks_indexed": int(info.rows), "index_truncated": bool(info.truncated)}
    if ctx.kind in MEDIA_KINDS:
        facts["chunks"] = int(info.chunks_total)
    if info.embed_input_tokens is not None:
        facts["embed_input_tokens"] = int(info.embed_input_tokens)
    return StageResult(facts=facts)


#: The one input a probe embeds (`apifiles/outage.py`): tiny, fixed, and not
#: any tenant's text.
PROBE_TEXT = "probe"


def _engine_embedder(ctx: JobContext) -> Callable[[Sequence[str]], Awaitable[Any]]:
    """The production document embedder for one job: the patient gate, ended
    by the job's `abandon`, adding its wait to `waited_for_capacity_s`."""
    from . import vectors

    async def embed(texts: Sequence[str]) -> Any:
        base = ctx.waited_for_capacity_s

        def on_wait(_position: int, waited_s: float) -> None:
            ctx.waited_for_capacity_s = base + max(0.0, float(waited_s))

        return await vectors.engine_embed_documents(
            texts, abandon=ctx.abandon, on_wait=on_wait, on_admitted=lambda waited_s: on_wait(0, waited_s)
        )

    return embed


async def _probe_embed(embed: Callable[[Sequence[str]], Awaitable[Any]]) -> str:
    """One one-input call through the same embedder: does the engine answer
    right now? (`outage.PROBE_*`)."""
    from ..publicapi import capacity

    try:
        await embed([PROBE_TEXT])
    except (asyncio.CancelledError, capacity.Abandoned):
        raise
    except Exception as exc:  # noqa: BLE001 — the verdict is the point
        return outage.probe_verdict(exc)
    return outage.PROBE_SERVING


async def stage_media(ctx: JobContext) -> StageResult:
    """probe → audio → transcript → frames → ocr → vision → fusion → artifacts
    through the video pipeline on the api lane (team MEDIA's `media.py`); this
    stage only waits, relays bare stage names and percents, and maps the
    outcome onto the closed vocabulary."""
    from . import media

    await ctx.checkpoint()
    ext = str(ctx.facts.get("ext") or "bin")
    try:
        started = await media.start_media_analysis(
            project_id=ctx.project_id,
            sha256=ctx.sha256,
            blob_id=ctx.blob_id,
            original_path=ctx.original_path,
            filename=f"media.{ext}",
            media_type=ctx.mime_type,
        )
    except media.MediaTooLong:
        raise FileTooComplex(_hours_ceiling()) from None
    except media.MediaTimeout as exc:
        raise Deferred("probe did not finish") from exc
    except media.MediaError:
        raise FileCorrupt() from None
    ctx.kind = started.kind
    # `start_media_analysis` wrote `video_analysis_id` to the ROW; the job's
    # copy must carry it too, or `_terminal` lists no derived names and counts
    # no analysis bytes for a file processed in one run (review finding,
    # 2026-09-13: `processing.derived` was [] while transcript.txt existed).
    ctx.blob["video_analysis_id"] = int(started.analysis_id)
    probe = started.probe
    facts: Dict[str, Any] = {
        "duration_s": round(float(probe.duration_s or 0.0), 1),
        "has_audio": bool(probe.has_audio),
        "has_video": bool(probe.has_video),
    }
    if getattr(probe, "width", None):
        facts["width"] = int(probe.width)
    if getattr(probe, "height", None):
        facts["height"] = int(probe.height)
    row = await _follow_pipeline(ctx, started.analysis_id)
    status, code = media.classify_outcome(row)
    if status != "processed":
        if code == "file_corrupt":
            raise FileCorrupt()
        raise ProcessingUnavailable()
    language = row.get("language")
    if isinstance(language, str) and re.fullmatch(r"[a-z]{2,3}(-[A-Za-z]{2,4})?", language):
        facts["language"] = language
    covered = tuple(s for s in MEDIA_PIPELINE_STEPS if s in ctx.steps and s != "probe")
    return StageResult(facts=facts, covers=covered)


async def _follow_pipeline(ctx: JobContext, analysis_id: int) -> Dict[str, Any]:
    from . import media
    from ..video import pipeline

    subscription = pipeline.subscribe(analysis_id)
    try:
        last_poll = 0.0
        while True:
            await ctx.checkpoint()
            now = time.monotonic()
            if now - last_poll >= MEDIA_POLL_S:
                last_poll = now
                row = await db.run_in_thread(db.get_video_analysis, analysis_id)
                if row is None:
                    raise ProcessingUnavailable()
                if row.get("status") in ("done", "failed"):
                    return row
                # A restart lost the pipeline task: start it again — but never
                # inside the pipeline's own retry delay after a model outage
                # (`ensure_running` does not check it; `drain_queue` does), or
                # this 5 s poll would re-try a down engine every 5 s.
                if (
                    row.get("status") == "queued"
                    and not pipeline.is_running(analysis_id)
                    and not pipeline._deferred_too_recently(row)
                ):
                    await pipeline.ensure_running(analysis_id)
            try:
                async with asyncio.timeout(MEDIA_POLL_S):
                    event = await subscription.get()
            except asyncio.TimeoutError:
                continue
            relayed = media.map_progress(ctx.kind, event)
            if relayed and relayed.get("stage") not in (None, "finalize"):
                stage = str(relayed["stage"])
                if relayed.get("status") == "done":
                    # A finished pipeline stage shows as done at once; its row
                    # write rides the next throttled progress write.
                    ctx.stages[stage] = {"status": "done"}
                    events.publish(ctx.blob_id, {"stage": stage, "percent": 100, "kind": ctx.kind, "status": "done"})
                else:
                    await ctx.progress(stage, relayed.get("percent"))
            if event.get("stage") == "_done":
                last_poll = 0.0
    finally:
        pipeline.unsubscribe(analysis_id, subscription)


def _hours_ceiling() -> str:
    seconds = limits.kind_caps("video").seconds or 14_400.0
    hours = seconds / 3600.0
    return f"longer than {hours:g} hours"


async def stage_finalize(ctx: JobContext) -> StageResult:
    """Page-marked `text.txt` (+ `pages.json`) from pages.jsonl."""
    from . import derived

    if ctx.kind in TEXT_KINDS or ctx.kind in TABLE_KINDS:
        await asyncio.to_thread(
            derived.build_text_artifacts, ctx.derived_dir, kind=ctx.kind, unit=str(ctx.facts.get("unit") or "page")
        )
    return StageResult()


def default_stages() -> Dict[str, StageFn]:
    return {
        "sniff": stage_sniff,
        "text": stage_text,
        "ocr": stage_ocr,
        "sheets": stage_sheets,
        "profile": stage_profile,
        "decode": stage_decode,
        "variants": stage_variants,
        "chunk": stage_chunk,
        "index": stage_index,
        "probe": stage_media,
        "finalize": stage_finalize,
    }


# ============================================================= row writes ==


def _json_field(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, Mapping) else {}


def _lease_state(blob_id: str) -> Optional[dict]:
    with db.connection() as con:
        row = con.execute("SELECT status, lease_owner FROM api_file_blobs WHERE id = %s", (blob_id,)).fetchone()
    return dict(row) if row else None


def _update_held(blob_id: str, owner: str, **fields: Any) -> Optional[dict]:
    """`schema.update_api_file_blob`'s allow-list, guarded by the lease: a job
    that lost its lease (or whose blob went to `deleting`) writes nothing."""
    unknown = sorted(set(fields) - schema._BLOB_UPDATABLE)
    if unknown:
        raise ValueError(f"not updatable on api_file_blobs: {unknown[0]}")
    names = sorted(fields)
    assignments = ", ".join(f"{name} = %s" for name in names)
    values = [schema.Jsonb(fields[n]) if n in schema._JSON_FIELDS else fields[n] for n in names]
    with db.connection() as con:
        row = con.execute(
            f"UPDATE api_file_blobs SET {assignments}, updated_at = now() "
            " WHERE id = %s AND lease_owner = %s AND status = 'processing' RETURNING *",
            (*values, blob_id, owner),
        ).fetchone()
    return dict(row) if row else None


def queue_position(blob_id: str) -> Optional[int]:
    """How many due blobs of the same lane are ahead of this one (0 when it is
    running). Server-internal: the route passes a blob id it read itself."""
    with db.connection() as con:
        row = con.execute(
            "SELECT b.status, b.lane, "
            "  (SELECT count(*) FROM api_file_blobs o WHERE o.lane = b.lane AND o.status = 'queued' "
            "     AND (o.updated_at, o.id) < (b.updated_at, b.id)) AS ahead "
            "  FROM api_file_blobs b WHERE b.id = %s",
            (blob_id,),
        ).fetchone()
    if row is None:
        return None
    return 0 if row["status"] != "queued" else int(row["ahead"])


# ================================================================ runner ==


@dataclass
class RunOutcome:
    blob_id: str
    outcome: str  # processed | failed | deferred | stopped | moved
    error_code: Optional[str] = None


class JobRunner:
    """One lane's claim loop, lease renewal and job tasks."""

    def __init__(
        self,
        lane: str,
        *,
        concurrency: Optional[int] = None,
        stages: Optional[Dict[str, StageFn]] = None,
        owner: Optional[str] = None,
        lease_s: Optional[float] = None,
        renew_s: float = RENEW_S,
        poll_s: float = POLL_S,
        retry_delay_s: Optional[float] = None,
        max_attempts: Optional[int] = None,
        run_assemblies: Optional[bool] = None,
        ocr_reader: Optional[Callable[..., Awaitable[Any]]] = None,
        ocr_gate: Optional[Callable[[], Any]] = None,
        embed_documents: Optional[Callable[..., Awaitable[Any]]] = None,
        free_check: Optional[Callable[[str, int], None]] = None,
        emit_webhooks: bool = True,
        record_usage: bool = True,
    ) -> None:
        if lane not in ("cpu", "media"):
            raise ValueError("lane must be cpu or media")
        self.lane = lane
        default_jobs = limits.cpu_jobs() if lane == "cpu" else limits.media_jobs()
        self.concurrency = max(1, int(concurrency if concurrency is not None else default_jobs))
        self.stages = dict(default_stages())
        self.stages.update(stages or {})
        self.owner = owner or queue.OWNER
        self.lease_s = float(lease_s if lease_s is not None else limits.processing_lease_s())
        self.renew_s = float(renew_s)
        self.poll_s = float(poll_s)
        self.retry_delay_s = retry_delay_s
        self.max_attempts = int(max_attempts if max_attempts is not None else limits.processing_max_attempts())
        self.run_assemblies = (lane == "cpu") if run_assemblies is None else bool(run_assemblies)
        self.ocr_reader = ocr_reader
        self.ocr_gate = ocr_gate
        self.embed_documents = embed_documents
        self.free_check = free_check or _default_free_check
        self.emit_webhooks = emit_webhooks
        self.record_usage = record_usage
        self.throttle = events.ProgressThrottle()
        self.stopping = False
        self._wake = asyncio.Event()
        self._loop_task: Optional[asyncio.Task] = None
        self._renew_task: Optional[asyncio.Task] = None
        self._jobs: Dict[str, Tuple[asyncio.Task, JobContext]] = {}
        self._assemblies: Dict[str, asyncio.Task] = {}
        self._cancelled: set = set()
        self._last_renewal = time.monotonic()
        self.outcomes: List[RunOutcome] = []

    # -- control ----------------------------------------------------------

    def kick(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        if self._loop_task is not None:
            return
        self.stopping = False
        self._loop_task = asyncio.create_task(self._loop(), name=f"apifiles-jobs-{self.lane}")
        self._renew_task = asyncio.create_task(self._renew_loop(), name=f"apifiles-leases-{self.lane}")

    async def stop(self) -> None:
        """Cancel every job; each releases its blob to `queued` (no attempt
        counted) so the next process resumes it without waiting for a lease."""
        self.stopping = True
        self._wake.set()
        tasks = [t for t in (self._loop_task, self._renew_task) if t is not None]
        tasks += [t for t, _ctx in self._jobs.values()] + list(self._assemblies.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._loop_task = self._renew_task = None
        self._jobs.clear()
        self._assemblies.clear()

    async def cancel(self, blob_id: str) -> None:
        """Cancel a local job (a DELETE): its child is killed, nothing is written."""
        entry = self._jobs.get(blob_id)
        if entry is None:
            return
        task, ctx = entry
        ctx.stop_reason = "deleting"
        self._cancelled.add(blob_id)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    def running(self) -> List[str]:
        return list(self._jobs)

    # -- the loop ---------------------------------------------------------

    async def run_once(self) -> List[RunOutcome]:
        """Claim what is due now and run it to completion (tests; also the
        body of one sweep when awaited)."""
        spawned = await self._claim_and_spawn()
        results: List[RunOutcome] = []
        for task in spawned:
            try:
                result = await task
            except asyncio.CancelledError:
                continue
            if isinstance(result, RunOutcome):
                results.append(result)
        return results

    async def _claim_and_spawn(self) -> List[asyncio.Task]:
        spawned: List[asyncio.Task] = []
        free = self.concurrency - len(self._jobs) - len(self._assemblies)
        if free <= 0 or self.stopping:
            return spawned
        if self.run_assemblies:
            upload = await db.run_in_thread(queue.claim_assembly, owner=self.owner)
            if upload is not None:
                task = asyncio.create_task(self._run_assembly(upload))
                self._assemblies[str(upload["id"])] = task
                spawned.append(task)
                free -= 1
        if free > 0:
            if self._jobs and time.monotonic() - self._last_renewal >= self.renew_s:
                # A renewal is overdue (the database was away): renew BEFORE
                # sweeping, so the sweep does not find our own leases lapsed.
                await self._renew_once()
            rows = await db.run_in_thread(queue.claim_due_blobs, self.lane, free, owner=self.owner, lease_s=self.lease_s)
            for row in rows:
                blob_id = str(row["id"])
                if blob_id in self._jobs:
                    # Our own job's lease lapsed and the sweep took it back
                    # under the same owner: that claim only renewed the lease.
                    # A second task here ran two children on one derived dir
                    # and left one untracked, so a DELETE's cancel missed it
                    # (review finding, 2026-09-13, reproduced with a 1 s lease).
                    continue
                ctx = JobContext(self, row)
                task = asyncio.create_task(self._run_job(ctx), name=f"apifiles-job-{ctx.blob_id}")
                self._jobs[ctx.blob_id] = (task, ctx)
                spawned.append(task)
        return spawned

    async def _loop(self) -> None:
        while not self.stopping:
            try:
                spawned = await self._claim_and_spawn()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a database blip must not end the lane
                log.warning("files %s lane: claim failed", self.lane, exc_info=True)
                spawned = []
            if spawned and len(self._jobs) + len(self._assemblies) < self.concurrency:
                continue
            self._wake.clear()
            waiters = [asyncio.ensure_future(self._wake.wait())]
            waiters += [t for t, _ in self._jobs.values()] + list(self._assemblies.values())
            await asyncio.wait(waiters, timeout=self.poll_s, return_when=asyncio.FIRST_COMPLETED)
            waiters[0].cancel()

    async def _renew_loop(self) -> None:
        delay = self.renew_s
        while not self.stopping:
            await asyncio.sleep(delay)
            # After a failed renewal, retry soon rather than a full interval
            # later: the lease has 60 s of slack, not 90.
            delay = self.renew_s if await self._renew_once() else min(self.renew_s, RENEW_RETRY_S)

    async def _renew_once(self) -> bool:
        held_ids = list(self._jobs)
        if not held_ids:
            self._last_renewal = time.monotonic()
            return True
        try:
            kept = set(await db.run_in_thread(queue.renew_blob_leases, held_ids, owner=self.owner, lease_s=self.lease_s))
        except Exception:  # noqa: BLE001 — retried by the loop and before the next claim sweep
            log.warning("files %s lane: lease renewal failed", self.lane, exc_info=True)
            return False
        self._last_renewal = time.monotonic()
        for blob_id in held_ids:
            if blob_id not in kept and blob_id in self._jobs:
                job = self._jobs[blob_id][1]
                job.lost = True
                job.abandon.set()  # a job waiting at a patient gate stops now
        return True

    # -- one assembly -----------------------------------------------------

    async def _run_assembly(self, upload: Dict[str, Any]) -> Optional[RunOutcome]:
        upload_id = str(upload["id"])
        try:
            result = await asyncio.to_thread(queue.assemble_claimed, upload, owner=self.owner)
            if result.outcome in ("checksum_mismatch", "failed") and result.file and self.emit_webhooks:
                await _emit_file_events([result.file], None, "file.failed")
            if result.blob is not None:
                enqueue(result.blob)
            return None
        except Exception:  # noqa: BLE001
            log.warning("files assembly %s failed a unit", upload_id, exc_info=True)
            return None
        finally:
            self._assemblies.pop(upload_id, None)
            self._wake.set()

    # -- one job ----------------------------------------------------------

    async def _run_job(self, ctx: JobContext) -> RunOutcome:
        try:
            return await self._run_job_inner(ctx)
        finally:
            entry = self._jobs.get(ctx.blob_id)
            if entry is not None and entry[1] is ctx:
                # Only this job's own entry: never another task's for the same blob.
                self._jobs.pop(ctx.blob_id, None)
                self.throttle.forget(ctx.blob_id)
            self._wake.set()

    async def _run_job_inner(self, ctx: JobContext) -> RunOutcome:
        blob_id = ctx.blob_id
        try:
            crashed = ctx.progress_state.get("running_owner")
            if crashed and crashed != self.owner:
                crashes = int(ctx.progress_state.get("crashes") or 0) + 1
                ctx.progress_state["crashes"] = crashes
                log.warning("files blob %s: previous run ended without releasing (%d)", blob_id, crashes)
                if crashes >= self.max_attempts:
                    raise ExtractError()
            ctx.progress_state["running_owner"] = self.owner
            held = await db.run_in_thread(
                _update_held, blob_id, self.owner, progress=ctx.progress_state, pipeline_version=APIFILES_PIPELINE_VERSION
            )
            if held is None:
                raise StopJob("lease_lost")
            return await self._run_steps(ctx)
        except StopJob as stop:
            return await self._stopped(ctx, stop.reason)
        except asyncio.CancelledError:
            reason = "shutdown" if self.stopping else (ctx.stop_reason or ("deleting" if blob_id in self._cancelled else "shutdown"))
            self._cancelled.discard(blob_id)
            await asyncio.shield(self._stopped(ctx, reason))
            raise
        except (Deferred, storage.StorageUnavailable) as exc:
            return await self._defer(ctx, exc)
        except ExtractError as exc:
            return await self._terminal(ctx, "failed", exc)
        except Exception:  # noqa: BLE001
            log.exception("files blob %s: stage %s failed unexpectedly", blob_id, ctx.current_stage)
            return await self._terminal(ctx, "failed", ExtractError())

    async def _run_steps(self, ctx: JobContext) -> RunOutcome:
        done_markers = await asyncio.to_thread(_read_markers, ctx.derived_dir)
        covered: set = set()
        index = 0
        while True:
            steps = ctx.steps  # the table can change after sniff / probe decide the kind
            if index >= len(steps):
                break
            step = steps[index]
            index += 1
            if step in covered:
                continue
            marker = done_markers.get(step)
            if marker is not None:
                ctx.facts.update(marker.get("facts") or {})
                if marker.get("kind") in sniff.KINDS:
                    ctx.kind = str(marker["kind"])
                    ctx.mime_type = str(marker.get("mime_type") or ctx.mime_type)
                ctx.stages[step] = {"status": marker.get("status", "done"), "ms": marker.get("ms")}
                covered.update(marker.get("covers") or ())
                for extra in marker.get("covers") or ():
                    ctx.stages[extra] = {"status": "done"}
                if step == "sniff":
                    moved = await self._maybe_move_lane(ctx)
                    if moved is not None:
                        return moved
                continue
            if step == "finalize" and ctx.kind in ("unsupported", "unknown"):
                raise Unsupported()
            fn = self.stages.get(step)
            if fn is None:
                log.error("files blob %s: no implementation for stage %r", ctx.blob_id, step)
                raise ExtractError()
            if step in DERIVED_WRITING:
                await asyncio.to_thread(self.free_check, ctx.kind, int(ctx.blob.get("bytes") or 0))
            await ctx.checkpoint()
            ctx.current_stage = step
            await ctx.progress(step, 0, force=True)
            started = time.monotonic()
            result = await fn(ctx)
            ms = int((time.monotonic() - started) * 1000)
            ctx.facts.update(result.facts or {})
            # A stage finished: the engine outage that deferred this blob is
            # over, so the next one backs off from the start again.
            ctx.progress_state.pop("outage_retries", None)
            ctx.progress_state.pop("outage_since", None)
            marker = {
                "status": result.status,
                "ms": ms,
                "facts": result.facts or {},
                "covers": list(result.covers or ()),
                # The kind as this stage left it: a resumed run must not fall
                # back to the row's pre-sniff guess when the crash came between
                # this marker and the row write.
                "kind": ctx.kind,
                "mime_type": ctx.mime_type,
                "pipeline_version": APIFILES_PIPELINE_VERSION,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
            await ctx.checkpoint()
            await asyncio.to_thread(_write_marker, ctx.derived_dir, step, marker)
            ctx.stages[step] = {"status": result.status, "ms": ms}
            for extra in result.covers or ():
                ctx.stages[extra] = {"status": "done"}
                covered.add(extra)
            fields: Dict[str, Any] = {"stages": ctx.stages, "facts": ctx.facts, "stage": step}
            if step == "sniff":
                moved = await self._maybe_move_lane(ctx)
                if moved is not None:
                    return moved
                fields.update(kind=ctx.kind, mime_type=ctx.mime_type)
            if step == "probe":
                fields.update(kind=ctx.kind)
            fields["progress"] = ctx._progress_payload(step, 100)
            if await db.run_in_thread(_update_held, ctx.blob_id, self.owner, **fields) is None:
                raise StopJob(await self._why_not_held(ctx))
            events.publish(ctx.blob_id, {"stage": step, "percent": 100, "kind": ctx.kind, "status": "done"})
        return await self._terminal(ctx, "processed", None)

    async def _maybe_move_lane(self, ctx: JobContext) -> Optional[RunOutcome]:
        wanted = "media" if ctx.kind in MEDIA_KINDS else "cpu"
        if wanted == self.lane:
            return None
        ctx.progress_state.pop("running_owner", None)
        await db.run_in_thread(
            queue.release_blob,
            ctx.blob_id,
            owner=self.owner,
            status="queued",
            lane=wanted,
            kind=ctx.kind,
            mime_type=ctx.mime_type,
            stages=ctx.stages,
            facts=ctx.facts,
            progress=ctx.progress_state,
        )
        _kick_lane(wanted)
        outcome = RunOutcome(ctx.blob_id, "moved")
        self.outcomes.append(outcome)
        return outcome

    async def _why_not_held(self, ctx: JobContext) -> str:
        row = await db.run_in_thread(_lease_state, ctx.blob_id)
        return "deleting" if row is None or row.get("status") == "deleting" else "lease_lost"

    # -- endings ----------------------------------------------------------

    async def _stopped(self, ctx: JobContext, reason: str) -> RunOutcome:
        if reason == "shutdown":
            ctx.progress_state.pop("running_owner", None)
            try:
                await db.run_in_thread(
                    queue.release_blob,
                    ctx.blob_id,
                    owner=self.owner,
                    status="queued",
                    stages=ctx.stages,
                    facts=ctx.facts,
                    progress=ctx.progress_state,
                )
            except Exception:  # noqa: BLE001 — the lease lapses in 90 s anyway
                log.warning("files blob %s: release on shutdown failed", ctx.blob_id, exc_info=True)
        outcome = RunOutcome(ctx.blob_id, "stopped", reason)
        self.outcomes.append(outcome)
        return outcome

    async def _defer(self, ctx: JobContext, exc: BaseException) -> RunOutcome:
        counted = bool(getattr(exc, "counted", True))
        ctx.progress_state.pop("running_owner", None)
        if counted:
            delay = self.retry_delay_s
        else:
            # An engine outage (apifiles/outage.py): no attempt is spent, and
            # the retry backs off from 15 s up to the counted retry delay.
            retries = int(ctx.progress_state.get("outage_retries") or 0)
            cap = self.retry_delay_s if self.retry_delay_s is not None else limits.processing_retry_delay_s()
            delay = outage.backoff_s(retries, cap)
            ctx.progress_state["outage_retries"] = retries + 1
            ctx.progress_state.setdefault("outage_since", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        # An uncounted deferral never fails the blob, so a long run of them is
        # the only sign of an engine that stays down: past OUTAGE_WARN_AFTER in
        # a row each one is a WARNING naming when the streak began.
        streak = 0 if counted else int(ctx.progress_state.get("outage_retries") or 0)
        log.log(
            logging.WARNING if streak >= outage.OUTAGE_WARN_AFTER else logging.INFO,
            "files blob %s deferred at %s (%s, retry in %s s): %s",
            ctx.blob_id, ctx.current_stage,
            "counted" if counted else (
                f"outage, not counted, {streak} in a row since {ctx.progress_state.get('outage_since')}"
            ),
            "default" if delay is None else round(float(delay), 1), exc,
        )
        await db.run_in_thread(_update_held, ctx.blob_id, self.owner, progress=ctx.progress_state, stages=ctx.stages, facts=ctx.facts)
        row = await db.run_in_thread(
            queue.defer_blob, ctx.blob_id, owner=self.owner, delay_s=delay, max_attempts=self.max_attempts,
            count_attempt=counted,
        )
        if row is not None and row.get("status") == "failed":
            events.publish(ctx.blob_id, {"status": "failed"})
            await self._after_terminal(row, "failed", ctx)
            outcome = RunOutcome(ctx.blob_id, "failed", "processing_unavailable")
        else:
            events.publish(ctx.blob_id, {"status": "queued"})
            outcome = RunOutcome(ctx.blob_id, "deferred", None if counted else "outage")
        self.outcomes.append(outcome)
        return outcome

    async def _terminal(self, ctx: JobContext, status: str, error: Optional[ExtractError]) -> RunOutcome:
        from . import derived

        ctx.progress_state.pop("running_owner", None)
        code = None
        if error is not None:
            code = error.code if error.code in ERROR_CODES else "internal_error"
            if ctx.current_stage and ctx.current_stage in ctx.stages:
                ctx.stages[ctx.current_stage] = {**ctx.stages[ctx.current_stage], "status": "failed"}
            ceiling = _safe_ceiling(error.ceiling)
            if ceiling:
                ctx.progress_state["error_ceiling"] = ceiling
        view_row = {**ctx.blob, "kind": ctx.kind, "status": "processed"}
        names: List[str] = []
        if status == "processed":
            try:
                names = [n["name"] for n in await asyncio.to_thread(derived.list_names, view_row)]
            except Exception:  # noqa: BLE001
                names = []
        ctx.progress_state["derived"] = names
        try:
            derived_total = await asyncio.to_thread(derived.derived_bytes, view_row)
        except (OSError, ValueError):
            derived_total = 0
        ctx.progress_state.update({"stage": "finalize", "percent": 100 if status == "processed" else None})
        row = await db.run_in_thread(
            queue.release_blob,
            ctx.blob_id,
            owner=self.owner,
            status=status,
            kind=ctx.kind if ctx.kind in sniff.KINDS else "unknown",
            mime_type=ctx.mime_type,
            facts=ctx.facts,
            stages=ctx.stages,
            progress=ctx.progress_state,
            error_code=code,
            derived_bytes=int(derived_total),
            stage="finalize",
        )
        if row is None:
            return await self._stopped(ctx, await self._why_not_held(ctx))
        events.publish(ctx.blob_id, {"status": status})
        await self._after_terminal(row, status, ctx)
        outcome = RunOutcome(ctx.blob_id, status, code)
        self.outcomes.append(outcome)
        return outcome

    async def _after_terminal(self, row: Dict[str, Any], status: str, ctx: JobContext) -> None:
        files: List[dict] = []
        try:
            files = await db.run_in_thread(schema.live_files_for_blob, str(row["id"]))
        except Exception:  # noqa: BLE001
            log.warning("files blob %s: could not list its files", row.get("id"), exc_info=True)
        if self.record_usage:
            try:
                from . import accounting

                await accounting.record_processing(
                    row,
                    file_ids=[str(f["id"]) for f in files],
                    status=status,
                    duration_ms=int((time.monotonic() - ctx.started) * 1000),
                    meta={"waited_for_capacity_s": round(ctx.waited_for_capacity_s, 1)},
                )
            except Exception:  # noqa: BLE001 — metering never fails processing
                log.warning("files blob %s: usage row failed", row.get("id"), exc_info=True)
        if self.emit_webhooks and files:
            await _emit_file_events(files, row, "file.processed" if status == "processed" else "file.failed")


def _read_markers(derived_dir: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    directory = os.path.join(derived_dir, STAGES_DIR)
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return out
    for entry in entries:
        if not entry.name.endswith(".json"):
            continue
        try:
            with open(entry.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and int(data.get("pipeline_version") or 0) == APIFILES_PIPELINE_VERSION:
            out[entry.name[:-5]] = data
    return out


def _write_marker(derived_dir: str, step: str, marker: Dict[str, Any]) -> None:
    directory = os.path.join(derived_dir, STAGES_DIR)
    if not os.path.isdir(derived_dir):
        # Purged under us (a DELETE raced the stage): writing would resurrect it.
        raise StopJob("deleting")
    try:
        os.mkdir(directory, storage.DIR_MODE)
    except FileExistsError:
        pass
    atomic_write_json(os.path.join(directory, f"{step}.json"), marker)


def _default_free_check(kind: str, blob_bytes: int) -> None:
    storage.require_free(storage.projected_derived_bytes(kind, blob_bytes))


_CEILING_RE = re.compile(r"^[A-Za-z0-9 ,.'()-]{1,120}$")


def _safe_ceiling(text: str) -> str:
    """Ceiling phrases are written by this package; anything else is dropped."""
    text = str(text or "").strip()
    return text if _CEILING_RE.fullmatch(text) and "/" not in text and "http" not in text.lower() else ""


async def _emit_file_events(files: Sequence[Mapping[str, Any]], blob: Optional[Mapping[str, Any]], event_type: str) -> None:
    """`file.processed` / `file.failed` through the webhook sender's
    `emit_file_event` (design §13.2, team WEBHOOKS). Absent until that lands:
    then nothing is emitted, and that is logged once per call."""
    try:
        from ..apiplatform.webhooks import sender
    except Exception:  # noqa: BLE001
        return
    emit = getattr(sender, "emit_file_event", None)
    if emit is None:
        log.debug("files webhooks: sender.emit_file_event is not installed yet")
        return
    for file_row in files:
        try:
            await emit(file_row, blob or {}, event_type, workspace_id=str(file_row.get("workspace_id") or ""))
        except Exception:  # noqa: BLE001 — a webhook never fails processing
            log.warning("files webhook %s for %s failed", event_type, file_row.get("id"), exc_info=True)


# ======================================================== module surface ==

_runners: Dict[str, JobRunner] = {}


def _kick_lane(lane: str) -> None:
    runner = _runners.get(lane)
    if runner is not None:
        runner.kick()


def enqueue(blob: Any) -> None:
    """Wake the lane that will run this blob (the row is already durable).
    Takes a blob id or a blob row (`queue.set_enqueue_hook` passes the row)."""
    if isinstance(blob, Mapping):
        lane = str(blob.get("lane") or "cpu")
        _kick_lane(lane)
        return
    for runner in _runners.values():
        runner.kick()


async def start(**runner_kwargs: Any) -> None:
    """Start both lanes and install the enqueue hook (integration: main.py)."""
    for lane in ("cpu", "media"):
        if lane not in _runners:
            _runners[lane] = JobRunner(lane, **runner_kwargs)
        await _runners[lane].start()
    queue.set_enqueue_hook(enqueue)


async def stop() -> None:
    queue.set_enqueue_hook(None)
    runners = list(_runners.values())
    _runners.clear()
    for runner in runners:
        await runner.stop()


async def cancel_blob(blob_id: str) -> None:
    for runner in list(_runners.values()):
        await runner.cancel(blob_id)


# ========================================================== the wire view ==

#: The facts a File object may show, per kind (design §2.2). Only these keys,
#: and only numbers, booleans or the validated strings below, ever reach the
#: wire — a fact a stage recorded for its own use (`ext`, `variants`,
#: `embed_input_tokens`, `thin_pages`) stays server-side.
_FACT_KEYS: Dict[str, Tuple[str, ...]] = {
    "pdf": ("pages", "text_pages", "ocr_pages", "ocr_skipped_pages", "chars", "estimated_tokens"),
    "document": ("sections", "chars", "estimated_tokens"),
    "presentation": ("slides", "chars", "estimated_tokens"),
    "text": ("sections", "chars", "estimated_tokens"),
    "html": ("sections", "chars", "estimated_tokens"),
    "spreadsheet": ("sheets", "rows", "columns", "chars", "estimated_tokens"),
    "tabular": ("rows", "columns", "chars", "estimated_tokens"),
    "image": ("width", "height", "format"),
    "audio": ("duration_s", "has_audio", "has_video", "language", "speech_fraction"),
    "video": ("duration_s", "has_audio", "has_video", "width", "height", "language", "speech_fraction"),
}
_INDEXED_KEYS = ("chunks", "chunks_indexed", "index_truncated")
_FORMAT_RE = re.compile(r"^[A-Z0-9]{2,12}$")
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")

_STATE = {"queued": "queued", "processing": "processing", "processed": "processed", "failed": "failed", "deleting": "processing"}
_WIRE_STAGE_STATUS = {"done": "done", "skipped": "done", "running": "running", "failed": "failed"}


def _epoch(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _clean_facts(kind: str, facts: Mapping[str, Any]) -> Dict[str, Any]:
    keys = _FACT_KEYS.get(kind, ())
    if kind in TEXT_KINDS or kind in TABLE_KINDS or kind in MEDIA_KINDS:
        keys = keys + _INDEXED_KEYS
    out: Dict[str, Any] = {}
    for key in keys:
        if key not in facts:
            continue
        value = facts[key]
        if isinstance(value, bool):
            out[key] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = value
        elif key == "format" and isinstance(value, str) and _FORMAT_RE.fullmatch(value):
            out[key] = value
        elif key == "language" and isinstance(value, str) and _LANGUAGE_RE.fullmatch(value):
            out[key] = value
    return out


def blob_from_file_row(file_row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The blob columns of a `schema._FILE_SELECT` row (prefixed `blob_`)."""
    if not file_row.get("blob_id"):
        return None
    blob: Dict[str, Any] = {"id": file_row["blob_id"], "project_id": file_row.get("project_id")}
    for key, value in file_row.items():
        if key.startswith("blob_") and key != "blob_id":
            blob[key[5:]] = value
    return blob


def processing_view(blob_row: Optional[Mapping[str, Any]], *, file_row: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """`{"status", "status_details", "processing"}` for a File object.

    `blob_row` may be None for a file still assembling from an upload (then
    `file_row` supplies the assembly progress) or one whose assembly failed."""
    if blob_row is None and file_row is not None:
        blob_row = blob_from_file_row(file_row)
    if blob_row is None:
        return _assembling_view(file_row or {})
    kind = str(blob_row.get("kind") or "unknown")
    status = str(blob_row.get("status") or "queued")
    state = _STATE.get(status, "queued")
    stages = _json_field(blob_row.get("stages"))
    progress = _json_field(blob_row.get("progress"))
    facts = _json_field(blob_row.get("facts"))
    steps = tuple(sniff.STAGES_BY_KIND.get(kind) or sniff.STAGES_BY_KIND["unknown"])
    listed: List[Dict[str, Any]] = []
    done_steps = 0.0
    current = None
    for name in steps:
        entry = stages.get(name) if isinstance(stages.get(name), Mapping) else {}
        raw = str((entry or {}).get("status") or "pending")
        wire = _WIRE_STAGE_STATUS.get(raw, "pending")
        if state == "processed":
            wire = "done"
        item: Dict[str, Any] = {"name": name, "status": wire}
        if wire == "running":
            pct = (entry or {}).get("percent")
            if isinstance(pct, (int, float)) and not isinstance(pct, bool):
                item["percent"] = int(max(0, min(100, pct)))
                done_steps += item["percent"] / 100.0
            current = current or name
        elif wire == "done":
            done_steps += 1
        elif wire == "failed":
            current = current or name
        listed.append(item)
    if current is None and state != "processed":
        # Nothing running: the next step is the first one not done. A blob a
        # re-upload re-queued still carries `finalize` from its failed run in
        # `stage` and `progress.stage` (schema._requeue_recoverable_blob).
        current = next((item["name"] for item in listed if item["status"] != "done"), None)
    if state == "processed":
        stage_name, step, percent = "finalize", len(steps), 100
    else:
        stage_name = current or str(progress.get("stage") or blob_row.get("stage") or steps[0])
        if stage_name not in steps:
            stage_name = steps[0]
        step = steps.index(stage_name) + 1
        percent = int(max(0, min(99, round(100.0 * done_steps / max(1, len(steps))))))
    error = None
    status_wire = "uploaded"
    details = None
    code = blob_row.get("error_code")
    if state == "processed":
        status_wire = "processed"
    elif state == "failed":
        status_wire = "error"
        code = code if code in ERROR_CODES else "internal_error"
        details = _redacted(sentence_for(code, _safe_ceiling(str(progress.get("error_ceiling") or ""))))
        error = {"code": code, "message": details}
    waited = progress.get("waited_for_capacity_s")
    derived_names = progress.get("derived") if state == "processed" else []
    view = {
        "state": state,
        "kind": kind,
        "stage": stage_name,
        "step": step,
        "total_steps": len(steps),
        "percent": percent,
        "stages": listed,
        "queue_position": 0 if state == "processing" else None,
        "waited_for_capacity_s": float(waited) if isinstance(waited, (int, float)) and not isinstance(waited, bool) else 0.0,
        "started_at": _epoch(blob_row.get("started_at")),
        "finished_at": _epoch(blob_row.get("processed_at")) if state in ("processed", "failed") else None,
        "error": error,
        "facts": _clean_facts(kind, facts),
        "derived": [str(n) for n in (derived_names or []) if isinstance(n, str) and re.fullmatch(r"[a-z0-9_.-]{1,40}", n)],
    }
    return {"status": status_wire, "status_details": details, "processing": view}


def file_processing_view(file_row: Mapping[str, Any]) -> Dict[str, Any]:
    """The same view from a joined file row (`schema._FILE_SELECT`: blob
    columns prefixed `blob_`) — the shape `publicapi.files.routes.
    FilesDependencies.processing_view` passes."""
    return processing_view(None, file_row=file_row)


def _assembling_view(file_row: Mapping[str, Any]) -> Dict[str, Any]:
    code = file_row.get("error_code")
    total = int(file_row.get("bytes") or 0)
    done = int(file_row.get("assembly_bytes_done") or 0)
    if code:
        message = (
            "The assembled bytes did not match the checksum you supplied."
            if code == "checksum_mismatch"
            else sentence_for("internal_error")
        )
        return {
            "status": "error",
            "status_details": message,
            "processing": {
                "state": "failed", "kind": "unknown", "stage": "assemble", "step": 0, "total_steps": 0,
                "percent": None, "stages": [{"name": "assemble", "status": "failed"}], "queue_position": None,
                "waited_for_capacity_s": 0.0, "started_at": None, "finished_at": None,
                "error": {"code": str(code), "message": message}, "facts": {}, "derived": [],
            },
        }
    percent = int(min(99, 100 * done / total)) if total else 0
    return {
        "status": "uploaded",
        "status_details": None,
        "processing": {
            "state": "processing", "kind": "unknown", "stage": "assemble", "step": 0, "total_steps": 0,
            "percent": percent, "stages": [{"name": "assemble", "status": "running", "percent": percent}],
            "queue_position": None, "waited_for_capacity_s": 0.0,
            "started_at": _epoch(file_row.get("assembly_started_at")), "finished_at": None,
            "error": None, "facts": {}, "derived": [],
        },
    }


def _redacted(text: str) -> str:
    try:
        from ..publicapi.errors import redact

        return redact(text)
    except Exception:  # noqa: BLE001
        return text


# ============================================================ readiness ==


async def wait_until_terminal(
    blob_id: str,
    *,
    on_progress: Optional[Callable[[dict], Awaitable[None]]] = None,
    timeout_s: Optional[float] = None,
) -> Optional[dict]:
    """Wait for a blob to reach `processed` / `failed` (or `deleting`, or to
    disappear: None). `timeout_s` None = no deadline (streams, background);
    on a timeout the current, non-terminal row is returned. Local progress
    wakes it at once; a 5 s poll covers a job in another process."""
    loop = asyncio.get_running_loop()
    deadline = None if timeout_s is None else loop.time() + max(0.0, float(timeout_s))
    last_signature = None
    with events.subscription(blob_id) as updates:
        while True:
            row = await db.run_in_thread(schema.get_api_file_blob, blob_id)
            if row is None:
                return None
            if row.get("status") in ("processed", "failed", "deleting"):
                return row
            if on_progress is not None:
                view = processing_view(row)["processing"]
                signature = (view["stage"], view["percent"], view["state"])
                if signature != last_signature:
                    last_signature = signature
                    await on_progress(view)
            wait = events.POLL_S
            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return row
                wait = min(wait, remaining)
            loaded_at = loop.time()
            try:
                async with asyncio.timeout(wait):
                    await updates.get()
            except asyncio.TimeoutError:
                continue
            # A local event is a doorbell: ring it at most once per second
            # into a database read, however fast the job publishes progress.
            pause = events.EVENT_THROTTLE_S - (loop.time() - loaded_at)
            if deadline is not None:
                pause = min(pause, deadline - loop.time())
            if pause > 0:
                await asyncio.sleep(pause)
            while not updates.empty():
                updates.get_nowait()
