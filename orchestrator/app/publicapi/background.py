"""Background responses: `background: true` (CONTRACT-3 §14).

A synchronous `POST /v1/responses` is only useful while the caller is still
holding the socket. A batch job asking for a thousand summaries, a lambda with
a thirty-second ceiling, a phone on a train — all need the same thing: hand
the work over, get an id, and come back for the answer. That is what this
module runs.

THE ORDER OF OPERATIONS IS THE CONTRACT, and it is the part that is easy to
get subtly wrong:

    1. the project's concurrency ceiling is checked FIRST, so a caller
       hammering the endpoint cannot fill `api_responses` with rows for work
       that is never going to start. It is THE SAME per-project counter the
       synchronous and streaming requests take (`quotas.concurrency_slot`,
       kind `background`), held for the whole life of the job. Until
       2026-09-13 this module kept its own dict, so a project could run its
       limit in streams AND its limit again in background jobs — 8 of the 10
       NORMAL lanes the chat app shares, at the default of 4 (verifier
       finding, reproduced);
    2. the row is DURABLE BEFORE ANY EXPENSIVE WORK — `db.create_api_response`
       with `background=True`. If this process dies in the next millisecond,
       `GET /v1/responses/{id}` still answers, and the id the route is about
       to return in its 202 is not a promise about something that was never
       recorded;
    3. the route returns 202 with that id;
    4. the generation runs in a DETACHED task. Detached is the whole feature:
       the task holds no reference to the request, the request holds no
       reference to the task, and the client hanging up changes nothing.

THE EXECUTION PATH IS THE SYNCHRONOUS ROUTE'S, NOT A SECOND ONE. CONTRACT-3
§11 says `/v1` calls `llm.stream_chat_events` and nothing lower, through the
shared admission lanes and the breaker. This module therefore drives
`streaming.Generation` — the same class `streaming.run_to_completion` drives
for `stream: false` and `streaming.responses_sse` drives for `stream: true`.
It is not a copy of that loop with a cancel check bolted on: the producer
task, the ContextVar usage boundary, the `aclose()` rule of CONTRACT-3 §10 and
the engine-failure mapping (`streaming.engine_error`) are all inherited, so a
change to any of them reaches background jobs on the same commit.

The one thing this module adds to the pump is the cancel check between
chunks, which is why it iterates `Generation.stream()` itself instead of
calling `run_to_completion`.

WHY THE TASK IS HELD IN A MODULE-LEVEL SET. `asyncio.create_task` returns the
only strong reference to a task; the event loop keeps a weak one. A task whose
last reference is dropped can be collected mid-await — a documented CPython
behaviour and, on 2026-09-10, the shape of the upload-reliability bug where a
finaliser "sometimes" did not run. `_tasks` holds each job until its own done
callback removes it.

CANCELLATION IS A FLAG, NOT A KILL. `POST /v1/responses/{id}/cancel` sets
`api_responses.cancel_requested` and, in this process, an in-memory event too.
The running job checks both between chunks and stops at the next one, so a
cancelled job RECORDS THE TEXT IT ALREADY PRODUCED and its usage instead of
losing both — which is what cancelling the task outright would do, since the
usage ContextVar is read in the producer's `finally`. Cancelling twice, or
cancelling something that already finished, is a no-op that returns the same
row; the column is what makes that idempotence survive a restart, since the
event does not.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

from .. import db
from ..apiplatform import quotas
from ..apiplatform.resolver import ApiCaller
from . import errors, streaming

log = logging.getLogger(__name__)

#: The statuses from which nothing further happens (they match the V34 CHECK).
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

#: The statuses a background row can be left in by a process that died.
OPEN_STATUSES = ("queued", "in_progress")

#: The kind this module declares to `quotas.concurrency_slot`.
SLOT_KIND = "background"

#: How often a running job re-reads its own `cancel_requested` column. The
#: in-memory event is the fast path and covers every cancel this orchestrator
#: serves; the poll is what makes a cancel issued by ANOTHER process (a
#: blue/green overlap during a deploy) take effect within a few seconds
#: instead of never.
CANCEL_POLL_SECONDS = 3.0

#: What a restart-orphaned row is failed with. Retry-safe on purpose: the
#: request never ran to completion, so asking again is the right advice.
INTERRUPTED_CODE = "model_unavailable"
INTERRUPTED_MESSAGE = "The service restarted while this response was running."

#: Builds the generation for one spec. `streaming.Generation` in production;
#: a stub in the tests, which is the only way to exercise this module's
#: ORCHESTRATION — what is written, when, and in what order — on a box with
#: no engine (see `tests/conftest.py`).
GenerationFactory = Callable[[streaming.GenerationSpec], Any]


@dataclass
class _Job:
    """One running background response, as this process sees it."""

    response_id: str
    project_id: str
    workspace_id: str
    cancel: asyncio.Event = field(default_factory=asyncio.Event)


#: Live jobs by response id, and the strong references that keep their tasks
#: from being collected mid-await (see the module docstring).
_jobs: Dict[str, _Job] = {}
_tasks: Set[asyncio.Task] = set()

_factory: Optional[GenerationFactory] = None

#: When this process started serving background jobs. A background row that
#: is still open, was created BEFORE this instant and is not in `_jobs` was
#: started by a process that no longer exists — nobody will ever finish it.
#: A row created after it may belong to a sibling process (a blue/green
#: overlap), so it is never touched on this evidence alone.
PROCESS_STARTED_AT: datetime = datetime.now(timezone.utc)


def set_generation_factory(factory: Optional[GenerationFactory]) -> None:
    """Swap in what builds a generation. `None` restores `streaming.Generation`."""
    global _factory
    _factory = factory


def _generation_factory() -> GenerationFactory:
    return _factory if _factory is not None else streaming.Generation


# ------------------------------------------------------- the concurrency --


def in_flight(project_id: Optional[str] = None) -> int:
    """Background jobs this process is running, for one project or for all.

    A count of live JOBS, for a health payload and the tests. It is NOT the
    concurrency gate: that is `quotas.concurrency_slot`, one counter per
    project across sync, stream and background (2026-09-13).
    """
    if project_id is None:
        return len(_jobs)
    return sum(1 for job in _jobs.values() if job.project_id == project_id)


def active_ids() -> Tuple[str, ...]:
    """The response ids this process is running. For a health payload and for
    the tests; never returned to a caller."""
    return tuple(_jobs)


# ------------------------------------------------------------- the entry --


async def start(
    spec: streaming.GenerationSpec,
    *,
    caller: ApiCaller,
    on_finish: Optional[streaming.OnFinish] = None,
    request_id: str = "",
    metadata: Optional[Mapping[str, Any]] = None,
    instructions_present: bool = False,
    fingerprint: str = "",
) -> dict:
    """Persist (if needed), start the detached job, and return the row.

    Returns the `api_responses` row the route renders as its 202 body. Raises
    `errors.ApiError` (429 `concurrency_limit_exceeded`) when the project is
    already at its ceiling — the ceiling of `caller`'s PROJECT, shared with
    every synchronous and streaming request of that project, never a number
    from the request (CONTRACT-3 §8, §12).

    THE SLOT IS HELD FOR THE WHOLE JOB and released by a done-callback on the
    task, not by a `finally` inside it: a task cancelled before its first step
    never runs its own `finally`, and a slot released only there would be
    stranded until a restart — the same shape as the streaming leak the
    verifier found on 2026-09-13.

    TWO CALL SHAPES, ONE FUNCTION. The router claims a row for EVERY mode
    before it dispatches (`_claim_request`), because `GET /v1/responses/{id}`
    is documented for all of them — so by the time this is called the row for
    `spec.response_id` usually exists already and must not be written twice.
    A caller that has not claimed one (a console action, a test) gets it
    written here. The difference is one read, and it is worth it: a second
    entry point would be a second place for the ordering above to drift.

    When the ceiling refuses a row that was ALREADY claimed, that row is
    closed as `failed` with the same code the caller is about to receive. A
    row left `queued` with nobody running it is exactly what
    `reconcile_interrupted` exists to clean up, and manufacturing one on a
    path that knows better would be sloppy.
    """
    project_id = caller.project_id
    workspace_id = caller.workspace_id
    existing = await db.run_in_thread(db.get_api_response, spec.response_id, project_id)
    slot = contextlib.ExitStack()
    try:
        slot.enter_context(quotas.concurrency_slot(caller, SLOT_KIND))
    except errors.ApiError as refusal:
        if existing is not None and str(existing.get("status")) not in TERMINAL_STATUSES:
            with contextlib.suppress(Exception):
                await db.run_in_thread(
                    db.update_api_response,
                    spec.response_id,
                    project_id,
                    status="failed",
                    error_code=refusal.code,
                    error_message=refusal.message,
                )
        raise

    if existing is None:
        try:
            row = await db.run_in_thread(
                db.create_api_response,
                project_id,
                workspace_id,
                spec.model,
                request_id or spec.response_id,
                response_id=spec.response_id,
                key_id=(caller.key_id or None),
                status="queued",
                background=True,
                streamed=False,
                fingerprint=fingerprint,
                instructions_present=instructions_present,
                metadata=dict(metadata or {}),
            )
        except BaseException:
            # The slot is a promise about work that is about to start. If the
            # row could not be written there is no work, and holding the slot
            # would leak one unit of the project's concurrency per failure
            # until a restart.
            slot.close()
            raise
    else:
        row = existing

    job = _Job(
        response_id=spec.response_id, project_id=project_id, workspace_id=workspace_id
    )
    _jobs[spec.response_id] = job
    try:
        task = asyncio.ensure_future(_run(job, spec, on_finish))
    except BaseException:
        _jobs.pop(spec.response_id, None)
        slot.close()
        raise
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    # Released when the task is DONE, however it got there — finished, failed,
    # or cancelled before it ever ran. `ExitStack.close()` is idempotent.
    task.add_done_callback(lambda _task: slot.close())
    return row


# ------------------------------------------------------------ the runner --


async def _cancelled_in_db(job: _Job) -> bool:
    row = await db.run_in_thread(db.get_api_response, job.response_id, job.project_id)
    return bool(row and row.get("cancel_requested"))


async def _run(
    job: _Job,
    spec: streaming.GenerationSpec,
    on_finish: Optional[streaming.OnFinish],
) -> None:
    """The detached task. Never raises — every exit writes a terminal row."""
    started = time.monotonic()
    outcome = streaming.StreamOutcome(
        response_id=spec.response_id, model=spec.model, created_at=spec.created_at
    )
    pieces: List[str] = []
    generation: Any = None
    last_poll = time.monotonic()

    try:
        await db.run_in_thread(
            db.update_api_response, job.response_id, job.project_id, status="in_progress"
        )
        # A cancel that arrived while the row was still `queued` must be
        # honoured before the model is ever asked — otherwise the cheapest
        # thing to cancel is the one thing a cancel cannot stop.
        if job.cancel.is_set() or await _cancelled_in_db(job):
            outcome.status = "cancelled"
        else:
            generation = _generation_factory()(spec)
            async for chunk in generation.stream():
                # THE CANCEL IS CHECKED BEFORE THE CHUNK IS KEPT, not after.
                # The rule that falls out of it is one a caller can state:
                # nothing the model produced after the cancel was requested is
                # recorded. Checking afterwards would keep one more chunk than
                # the caller asked for, and "it stopped, but there is an extra
                # sentence" is the bug report that follows.
                #
                # And it is checked BETWEEN chunks, never by cancelling the
                # task: `Generation` reads the usage ContextVar in its
                # producer's `finally`, so a hard cancel would throw away both
                # the partial answer and the tokens it cost.
                #
                # THE LIMIT, STATED PLAINLY: a job whose engine has gone
                # silent still receives `Generation`'s heartbeat chunk, which
                # `events.HEARTBEAT_SECONDS` caps at 15 s on this surface. So
                # a cancel is noticed within one heartbeat even when no token
                # has arrived for minutes — bounded, not open-ended, and the
                # reason the heartbeat is consumed here rather than filtered
                # out before this check.
                if job.cancel.is_set():
                    outcome.status = "cancelled"
                    break
                now = time.monotonic()
                if now - last_poll >= CANCEL_POLL_SECONDS:
                    last_poll = now
                    if await _cancelled_in_db(job):
                        outcome.status = "cancelled"
                        break
                if getattr(chunk, "kind", "") == streaming.TOKEN_KIND:
                    text = str(getattr(chunk, "text", "") or "")
                    if text:
                        pieces.append(text)
    except asyncio.CancelledError:
        # The loop is being torn down (a deploy). Marked `failed` with a
        # RETRYABLE code rather than `cancelled`: nobody pressed cancel, and a
        # caller polling this id deserves to know it may safely ask again.
        #
        # Best-effort, and honestly so: the task is already cancelled, so the
        # very next await can raise `CancelledError` again and the row may
        # never be written. `reconcile_interrupted()` is the repair, but NO
        # PRODUCTION CALLER RUNS IT YET (verifier finding 2026-09-13: the
        # lifespan belongs to `app/main.py`, another owner) — until it is
        # wired, a row cut off here can stay open.
        outcome.status = "failed"
        outcome.error = errors.model_unavailable()
        _finalise(outcome, generation, pieces, started)
        with contextlib.suppress(BaseException):
            await _settle(job, outcome, on_finish)
        raise
    except errors.ApiError as exc:
        outcome.status = "failed"
        outcome.error = exc
    except Exception as exc:  # noqa: BLE001 — a job never dies unrecorded
        # `from_unexpected` THROWS THE ORIGINAL TEXT AWAY (CONTRACT-3 §9): a
        # psycopg error carrying a container name and a private IP must not
        # reach the row that `GET /v1/responses/{id}` renders.
        outcome.status = "failed"
        outcome.error = errors.from_unexpected(exc)
        log.warning("background response %s failed", job.response_id, exc_info=True)
    finally:
        if generation is not None:
            # ALWAYS closed (CONTRACT-3 §10). `Generation.stream()`'s own
            # `finally` does not run promptly when the consumer is abandoned —
            # measured 2026-09-13 — so the consumer asks.
            with contextlib.suppress(BaseException):
                await generation.aclose()
        _jobs.pop(job.response_id, None)

    _finalise(outcome, generation, pieces, started)
    if outcome.status != "cancelled" and getattr(generation, "error", None) is not None:
        # The engine failed inside the producer task, which carries the
        # exception rather than raising it into this loop. `engine_error` maps
        # it to the retry-safe CONTRACT §9 code — `model_recovering` when the
        # controller says a reload is in progress, `timeout` for a wall-clock
        # kill — which a flat `internal_error` would have hidden.
        outcome.status = "failed"
        outcome.error = streaming.engine_error(generation.error)
    await _settle(job, outcome, on_finish)


def _finalise(
    outcome: streaming.StreamOutcome, generation: Any, pieces: List[str], started: float
) -> None:
    """Fill in what the pump produced: text, usage and timings."""
    outcome.text = "".join(pieces)
    # CONTRACT-3 §9: usage is null — never 0 — when nothing was measured, and
    # it is read from the GENERATION, not from `llm.get_usage()`: the
    # ContextVar was set inside the producer's own task and this one would
    # read its own empty copy.
    outcome.usage = getattr(generation, "usage", None)
    outcome.duration_ms = int((time.monotonic() - started) * 1000)
    first_token_at = getattr(generation, "first_token_at", None)
    if first_token_at is not None:
        outcome.ttft_ms = int((first_token_at - started) * 1000)


async def _settle(
    job: _Job, outcome: streaming.StreamOutcome, on_finish: Optional[streaming.OnFinish]
) -> None:
    """Record the result, then tell the ledger's hook and the webhooks."""
    if on_finish is not None:
        # The router's recorder: it writes the status, usage and timings to
        # the row AND the one usage-ledger row CONTRACT-3 §16 allows. A
        # failure there must never turn a completed response into a failed
        # one, so it is swallowed and logged.
        try:
            await on_finish(outcome)
        except Exception:  # noqa: BLE001
            log.warning(
                "the completion hook raised for %s", job.response_id, exc_info=True
            )
    else:
        await _record_outcome(job, outcome)

    if outcome.text:
        # The ONE place output text is stored, and only for a background
        # response: its caller has no stream to read it from (SCHEMA-V34, and
        # pruned by the project's retention window). Written even for a
        # cancelled job — throwing away half an answer because somebody
        # pressed stop is what people report as data loss.
        with contextlib.suppress(Exception):
            await db.run_in_thread(
                db.update_api_response,
                job.response_id,
                job.project_id,
                output_text=outcome.text,
            )

    row = await db.run_in_thread(db.get_api_response, job.response_id, job.project_id)
    if row is not None:
        await _notify(row, job.workspace_id)


async def _record_outcome(job: _Job, outcome: streaming.StreamOutcome) -> None:
    """The row write for a caller that supplied no `on_finish`."""
    fields: Dict[str, Any] = {
        "status": outcome.status,
        "duration_ms": outcome.duration_ms,
    }
    if outcome.ttft_ms is not None:
        fields["ttft_ms"] = outcome.ttft_ms
    usage = outcome.usage_model()
    if usage is not None:
        # Touched only when the engine reported: NULL means NOT MEASURED
        # (SCHEMA-V34) and a zero would be both a lie and an under-charge.
        fields["input_tokens"] = usage.input_tokens
        fields["output_tokens"] = usage.output_tokens
    if outcome.error is not None:
        fields["error_code"] = outcome.error.code
        fields["error_message"] = errors.redact(outcome.error.message)
    try:
        await db.run_in_thread(
            db.update_api_response, job.response_id, job.project_id, **fields
        )
    except Exception:  # noqa: BLE001
        log.warning(
            "background response %s could not record its result",
            job.response_id, exc_info=True,
        )


async def _notify(row: Mapping[str, Any], workspace_id: str) -> None:
    """Queue `response.completed` / `.failed` / `.cancelled` to the project's
    endpoints. Best-effort and last: a webhook that could not be queued must
    never change what the response row says happened."""
    from ..apiplatform.webhooks import sender

    event = {
        "completed": sender.RESPONSE_COMPLETED,
        "failed": sender.RESPONSE_FAILED,
        "cancelled": sender.RESPONSE_CANCELLED,
    }.get(str(row.get("status") or ""))
    if event is None:
        return
    try:
        await sender.emit_response_event(row, event, workspace_id=workspace_id)
    except Exception:  # noqa: BLE001 — see the docstring
        log.warning("could not queue %s for %s", event, row.get("id"), exc_info=True)


# ------------------------------------------------------- cancel and sweep --


async def request_cancel(response_id: str, project_id: str) -> Optional[dict]:
    """`POST /v1/responses/{id}/cancel` — idempotent, by construction.

    Returns the row (which the route renders) or None when this project has no
    such response, which is the 404 `response_not_found` the contract owes —
    another project's id reads as missing, never as forbidden.

    Cancelling a response that has already finished is NOT an error and does
    not write: the row comes back exactly as it was, so a client that retries
    a cancel after a timeout sees the same answer rather than a 409, and a
    completed response can never be walked back into `cancelled`.

    WHEN A JOB IS RUNNING HERE, THE STATUS IS NOT SET FROM OUTSIDE. Only the
    flag is, and the job writes its own terminal row once it has stopped —
    which is what lets it keep the text and the token counts it had already
    accumulated. Setting `cancelled` from here would race the job's own write
    and could leave a response marked cancelled with no usage against a
    request that really did cost tokens.

    WHEN NO JOB IS RUNNING HERE and the row is still open, the row is a
    restart orphan: its task died with the process that started it (the flag
    alone would then do nothing at all, and the caller would poll `queued`
    until the sweep got to it). So it is closed as `cancelled` immediately,
    which is also the honest answer — nobody is generating anything. The flag
    is written too, so that a second orchestrator during a blue/green overlap
    stops at its next chunk rather than finishing work the caller has
    abandoned.
    """
    row = await db.run_in_thread(db.get_api_response, response_id, project_id)
    if row is None:
        return None
    if str(row.get("status") or "") in TERMINAL_STATUSES:
        return row
    job = _jobs.get(response_id)
    fields: Dict[str, Any] = {"cancel_requested": True}
    if job is None:
        fields["status"] = "cancelled"
    updated = await db.run_in_thread(
        db.update_api_response, response_id, project_id, **fields
    )
    if job is not None:
        # The fast path: the running task sees this before its next chunk.
        job.cancel.set()
    return updated or row


def as_datetime(value: Any) -> Optional[datetime]:
    """A row timestamp as an aware datetime, or None.

    `db` renders temporal columns as ISO strings (`db._iso`), not datetimes —
    which is why, until 2026-09-13, every `created_at` this surface rendered
    from a stored row came out as 0: the router asked the string for
    `.timestamp()`. Accepts both shapes so neither caller has to know.
    """
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


async def repair_if_orphaned(row: Optional[dict]) -> Optional[dict]:
    """Close ONE restart-orphaned background row at the moment it is read.

    WHY HERE (2026-09-13). The sweep below has no production caller — the
    lifespan that would run it belongs to `app/main.py`, another owner — so a
    background response cut off by a deploy stayed `in_progress` and a client
    polling `GET /v1/responses/{id}` polled forever (verifier finding; the
    `research_runs` bug of 2026-08-28 again). The read path is the one place
    every such poller passes through, so the repair happens there, lazily,
    for exactly the row being asked about.

    Only a row that is background, still open, not running in this process,
    and created before this process started. Anything else is returned as is.
    """
    if not row or not row.get("background"):
        return row
    if str(row.get("status") or "") not in OPEN_STATUSES:
        return row
    if str(row.get("id")) in _jobs:
        return row
    created = as_datetime(row.get("created_at"))
    if created is None or created >= PROCESS_STARTED_AT:
        return row
    updated = await db.run_in_thread(
        db.update_api_response,
        row["id"],
        row["project_id"],
        status="failed",
        error_code=INTERRUPTED_CODE,
        error_message=INTERRUPTED_MESSAGE,
    )
    return updated or row


async def reconcile_interrupted(project_id: str) -> int:
    """Close background rows a dead process left open. Returns how many.

    A response that was `queued` or `in_progress` when the orchestrator went
    away is being run by nobody: its task died with the process and, unlike a
    video analysis or an artifact job, there is no on-disk state to resume
    from. Left alone the row says `in_progress` forever and a caller polls it
    forever — which is exactly what `research_runs` did until 2026-08-28.

    Called per project because the accessors are tenant-scoped by design. A
    process-wide sweep at start-up needs a `db` accessor that does not exist
    yet (see `notes[]`); it belongs to the database team's file, not this one.
    """
    closed = 0
    for status in OPEN_STATUSES:
        rows = await db.run_in_thread(
            db.list_api_responses, project_id, status=status, limit=200
        )
        for row in rows:
            if not row.get("background"):
                # A synchronous response in this state died with the socket
                # that was waiting on it; nobody is polling for it.
                continue
            if str(row["id"]) in _jobs:
                # Still running in THIS process. A restart is the only thing
                # this function repairs, and a live job is not one.
                continue
            await db.run_in_thread(
                db.update_api_response,
                row["id"],
                project_id,
                status="failed",
                error_code=INTERRUPTED_CODE,
                error_message=INTERRUPTED_MESSAGE,
            )
            closed += 1
    return closed


async def drain(timeout: float = 30.0) -> int:
    """Wait for the jobs this process is running, up to `timeout` seconds.

    For an orderly shutdown and for the tests. Returns how many were still
    running when the wait gave up — those are rows for `reconcile_interrupted`
    to repair, not results to invent. NEITHER FUNCTION HAS A PRODUCTION CALLER
    as of 2026-09-13: the lifespan that should call `drain` on shutdown and a
    process-wide sweep on start-up lives in `app/main.py`, which another owner
    holds (verifier finding, recorded as open).
    """
    pending = [task for task in _tasks if not task.done()]
    if not pending:
        return 0
    _done, still_running = await asyncio.wait(pending, timeout=timeout)
    return len(still_running)


__all__ = [
    "CANCEL_POLL_SECONDS",
    "SLOT_KIND",
    "GenerationFactory",
    "INTERRUPTED_CODE",
    "OPEN_STATUSES",
    "TERMINAL_STATUSES",
    "active_ids",
    "as_datetime",
    "drain",
    "in_flight",
    "reconcile_interrupted",
    "repair_if_orphaned",
    "request_cancel",
    "set_generation_factory",
    "start",
]
