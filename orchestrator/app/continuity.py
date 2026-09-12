"""Continuity in one-model mode (CONTRACT v2 §8.3–8.4) — what happens to a
person's request while the main model cannot take it.

WHY THIS EXISTS. Only `nvidia/Qwen3.6-35B-A3B-NVFP4` may answer a person.
There is no smaller model to stand in for it and there never will be: an
answer from another model is a different answer, and the person asked this
one. A reload of the TP=2 pair measured 3 m 32 s warm and 5 m 20 s cold
(2026-09-11); the old shape for that window was a spinner, then the
MODEL_UNAVAILABLE sentence, then a Retry the person had to press. The new
shape is the one a queue has: the request is KEPT, the person is TOLD, and
the same generation goes on by itself when the model is back.

WHAT "KEPT" MEANS. The V29 `chat_requests` row — the durable record of the
send, keyed by the browser's intent_id — moves to `queued` (V33) the moment
a model call of the turn cannot be admitted: the breaker is OPEN, or the
controller reports the engine STARTING / WEDGED / RECOVERING / DOWN. The
generation itself stays alive in this process, sleeping on the engine-state
client's READY event (app/engine_state.py) — never polling the dead port —
for up to LLM_QUEUE_MAX_WAIT_S. When READY arrives the SAME generation
resumes: same generation_id, `attempt` + 1, `retry_reason = "recovery"`,
and the V29 guards (one row per intent, one assistant message per
generation) make a second answer impossible. If the wait outruns the
window the worker parks the row — it stays `queued` — and says so; the
resume sweep below picks it up on the next READY, and a browser that comes
back attaches to whatever is running for its intent.

WHAT "TOLD" MEANS. Exactly two sentences exist, said once each:

    QUEUED_LINE   when the request is first held
    EXPIRED_LINE  when the wait outran the window and the row was parked

The status line rides the chat worker's notifier (app/resilience.py), so
sibling calls of one turn share one announcement, and the SSE stream keeps
its heartbeats for the whole wait: the client renders a queued state, not
a dead spinner.

THE HOLD. `bind()` attaches a `Hold` to the chat worker's task — the same
ContextVar idiom as resilience._Notifier and llm._usage — carrying the
generation, the notifier and the queue bookkeeping. app/resilience.py asks
for it at its two wait points (the breaker gate and the engine wait) and
app/admission.py at its lanes; a task with no hold (a video stage, an
artifact job, a title job) waits exactly as before and gets
ModelUnavailable at its window, which those jobs already turn into
"deferred".

THE RESUME SWEEP (§8.4 v2). When the controller's verdict turns to READY —
and, for rows a dead process parked, at start-up — every `queued` (and, on
READY, `interrupted`) row with a resumable snapshot and no live generation
in this process is resumed exactly once: the V29 compare-and-swap on the
row's generation_id is the lease (only one resumer can move it), and a row
whose generation already has a durable answer is healed and counted
`duplicate_suppressed` instead of run again.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar
from typing import Awaitable, Callable, Optional

from . import db, engine_state, metrics
from .config import settings

log = logging.getLogger(__name__)

#: The exact sentences (CONTRACT §8.3). Never paraphrased anywhere else.
QUEUED_LINE = "Main model is recovering—your request is safely queued."
EXPIRED_LINE = "The main model is still recovering. Your request is kept and will resume automatically."

#: The client-facing code on the terminal frame of a parked turn. Not a
#: failure code from frontend/lib/errorTypes.ts on purpose: the row is
#: `queued`, not failed, and the sentence beside it says so.
PARKED_CODE = "MODEL_RECOVERING"

#: The two kinds of wait a hold records. `recovery` is the engine being
#: away (the resume bumps `attempt`); `admission` is a lane wait in front
#: of a healthy engine (app/admission.py) — same durable state, no new
#: attempt.
RECOVERY = "recovery"
ADMISSION = "admission"


def resume_max_age_s() -> float:
    """A parked or interrupted row older than this is not resumed by the
    sweep (LLM_RESUME_MAX_AGE_S): nobody is waiting for it any more, and a
    thread would only be surprised by an answer to a day-old question. The
    browser's own re-attach is not bound by it."""
    return max(0.0, float(settings.llm_resume_max_age_s))


#: How many rows one sweep may resume. A larger backlog is drained by the
#: next READY edge (and by the browsers that come back).
_SWEEP_LIMIT = 200


class QueuedForRecovery(RuntimeError):
    """The wait for the main model outran LLM_QUEUE_MAX_WAIT_S.

    Raised by app/resilience.py on a chat turn INSTEAD of ModelUnavailable:
    the row stays `queued`, the person has read EXPIRED_LINE, and the chat
    worker ends the stream without failing the request (main.py). Never
    raised on a task without a hold.
    """

    def __init__(self, waited_s: float) -> None:
        self.waited_s = waited_s
        super().__init__(f"main model still recovering after {waited_s:.0f}s; request kept queued")


# ---------------------------------------------------------------------------
# The hold: one per chat turn
# ---------------------------------------------------------------------------

#: In-memory count of generations currently held for the primary in this
#: process (CONTRACT §7.2 `llm_queued_generations`; signal 16).
_queued = 0


def _publish_queued() -> None:
    metrics.set_gauge(
        "llm_queued_generations", float(_queued),
        "Accepted generations waiting for the primary model in this process.",
    )


def queued_count() -> int:
    return _queued


class Hold:
    """The chat worker's handle on its generation while it waits.

    `gen` is duck-typed (main.LiveGeneration): `intent_id`, `generation_id`,
    `attempt`, `retry_reason`, `request_status`, `parked`. `notify` sends
    one status line to the person; it is the same callable the wait
    notifier holds, so the two never interleave.
    """

    __slots__ = ("gen", "notify", "kind", "queued_at", "announced", "row_parked", "expired", "resumed_once")

    def __init__(self, gen, notify: Optional[Callable[[str], Awaitable[None]]]) -> None:
        self.gen = gen
        self.notify = notify
        #: The kind of the wait in progress, or None when not waiting.
        self.kind: Optional[str] = None
        self.queued_at: Optional[float] = None
        #: Whether the person has read the line for THIS wait.
        self.announced = False
        #: Whether the row was moved to `queued` for this wait.
        self.row_parked = False
        self.expired = False
        #: The turn resumes as a new attempt ONCE (CONTRACT §8.3 step 4): a
        #: second recovery wait inside the same turn — the engine came back,
        #: served a sidecar call, went away again — is the same attempt
        #: still going.
        self.resumed_once = False

    @property
    def waiting(self) -> bool:
        return self.kind is not None

    @property
    def active(self) -> bool:
        """Is the generation still the one a wait would be about? A model
        call made AFTER the answer — the background compaction the chat
        worker spawns, a title job — inherits the task's context and with
        it this hold, but its wait is nobody's queued request: the row is
        finished and the person has the answer."""
        gen = self.gen
        if getattr(gen, "done", False):
            return False
        return str(getattr(gen, "request_status", "running")) in ("accepted", "running", "queued")

    async def _say(self, line: str) -> None:
        if self.notify is None:
            return
        try:
            await self.notify(line)
        except Exception:  # noqa: BLE001 — a status line must never break the wait
            log.debug("continuity: notifier failed", exc_info=True)

    async def enter(self, kind: str, line: str = QUEUED_LINE) -> None:
        """The wait begins (idempotent within one wait): the row goes
        `queued`, the gauge counts it, the person reads `line` once."""
        global _queued
        if self.kind is not None:
            return
        self.kind = kind
        self.queued_at = time.monotonic()
        self.announced = False
        self.row_parked = False
        _queued += 1
        _publish_queued()
        gen = self.gen
        if getattr(gen, "intent_id", None):
            try:
                row = await db.run_in_thread(db.park_chat_request, gen.intent_id, gen.generation_id)
            except Exception as exc:  # noqa: BLE001 — bookkeeping, never a reason to lose the turn
                log.warning("continuity: could not park request %s: %s: %s",
                            gen.intent_id, type(exc).__name__, exc)
                row = None
            if row is not None:
                self.row_parked = True
                gen.request_status = "queued"
        log.info("continuity generation=%s intent=%s queued kind=%s",
                 getattr(gen, "generation_id", "?"), getattr(gen, "intent_id", "?"), kind)
        if not self.announced:
            self.announced = True
            await self._say(line)

    def _observe_wait(self) -> float:
        waited = max(0.0, time.monotonic() - (self.queued_at or time.monotonic()))
        metrics.observe("llm_queue_wait_seconds", waited,
                        "Seconds a generation waited for the primary model.")
        return waited

    async def resume(self) -> None:
        """The wait is over and the call goes through: the row is `running`
        again under the SAME generation; a recovery wait is a new attempt
        (`attempt` + 1, `retry_reason = recovery`), counted once."""
        global _queued
        if self.kind is None:
            return
        kind = self.kind
        waited = self._observe_wait()
        recovery = kind == RECOVERY
        new_attempt = recovery and not self.resumed_once
        gen = self.gen
        if self.row_parked and getattr(gen, "intent_id", None):
            try:
                row = await db.run_in_thread(
                    db.resume_queued_chat_request, gen.intent_id, gen.generation_id, new_attempt=new_attempt,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("continuity: could not resume request %s: %s: %s",
                            gen.intent_id, type(exc).__name__, exc)
                row = None
            if row is not None:
                gen.request_status = "running"
                gen.attempt = int(row.get("attempt") or gen.attempt)
            elif new_attempt:
                gen.attempt = int(getattr(gen, "attempt", 1)) + 1
        elif new_attempt:
            gen.attempt = int(getattr(gen, "attempt", 1)) + 1
        if recovery:
            gen.retry_reason = RECOVERY
        if new_attempt:
            self.resumed_once = True
            metrics.inc("llm_resumed_generations_total",
                        "Generations resumed after waiting for the primary, by outcome.", outcome="resumed")
        log.info("continuity generation=%s intent=%s resumed kind=%s after %.0fs attempt=%s",
                 getattr(gen, "generation_id", "?"), getattr(gen, "intent_id", "?"), kind, waited,
                 getattr(gen, "attempt", "?"))
        self._clear()

    def abandon(self) -> None:
        """The wait ended without the call going through — a Stop, a
        cancellation, a parent task torn down. Only the gauge moves: the
        row's terminal status is whoever ended the turn's to write."""
        if self.kind is None:
            return
        self._observe_wait()
        self._clear()

    async def expire(self) -> None:
        """The wait outran the window: the row stays `queued` (parked for
        the sweep), the person reads EXPIRED_LINE, and the turn ends."""
        if self.kind is None:
            return
        waited = self._observe_wait()
        self.expired = True
        gen = self.gen
        setattr(gen, "parked", True)
        metrics.inc("llm_resumed_generations_total",
                    "Generations resumed after waiting for the primary, by outcome.", outcome="expired")
        log.warning("continuity generation=%s intent=%s parked after %.0fs: %s",
                    getattr(gen, "generation_id", "?"), getattr(gen, "intent_id", "?"), waited,
                    "row stays queued for the resume sweep")
        self._clear()
        await self._say(EXPIRED_LINE)
        raise QueuedForRecovery(waited)

    def _clear(self) -> None:
        global _queued
        _queued = max(0, _queued - 1)
        _publish_queued()
        self.kind = None
        self.queued_at = None
        self.announced = False
        self.row_parked = False


_HOLD: ContextVar[Optional[Hold]] = ContextVar("_llm_continuity_hold", default=None)


def bind(gen, notify: Optional[Callable[[str], Awaitable[None]]]) -> Hold:
    """Attach a hold to the CURRENT task for the rest of its life — the chat
    worker calls it beside `resilience.set_wait_notifier`. Child tasks (the
    route classification and the answer of one turn run as siblings) get a
    copy of the context but the same object, so the line is said once."""
    hold = Hold(gen, notify)
    _HOLD.set(hold)
    return hold


def current() -> Optional[Hold]:
    """The task's hold, or None outside a chat turn (or after its answer)."""
    hold = _HOLD.get()
    if hold is None or not hold.active:
        return None
    return hold


def held() -> bool:
    return current() is not None


def queue_window_s() -> float:
    """How long a held turn waits for the primary (CONTRACT §8.3 step 3)."""
    return max(0.0, float(settings.llm_queue_max_wait_s))


# ---------------------------------------------------------------------------
# The resume sweep
# ---------------------------------------------------------------------------

_sweep_task: Optional[asyncio.Task] = None
_sweep_lock: Optional[asyncio.Lock] = None
_installed = False


def _count(outcome: str) -> None:
    metrics.inc("llm_resumed_generations_total",
                "Generations resumed after waiting for the primary, by outcome.", outcome=outcome)


def _principal_for(user_id: int):
    """A Principal for the row's owner without a session: the same rows
    authn/principal._build reads, minus the last-active stamp (a resume the
    server runs on someone's behalf is not that person being active)."""
    from .authn import features as features_mod
    from .authn import store
    from .authn.principal import Principal
    from .authn.rbac import Role, capabilities

    user = store.get_user(int(user_id))
    if user is None or user["status"] != "active":
        return None
    member = store.membership(int(user["id"]))
    if member is None:
        return None
    role = Role(member["role"])
    return Principal(
        user_id=int(user["id"]),
        username=user["username"],
        email=user.get("email") or "",
        display_name=user.get("display_name") or user["username"],
        role=role,
        workspace_id=member["workspace_id"],
        workspace_name=member["workspace_name"],
        session_id="resume-sweep",
        caps=capabilities(role),
        features=features_mod.resolve(
            role=role.value,
            workspace_defaults=member.get("feature_defaults"),
            member_overrides=member.get("member_features"),
        ),
    )


def _synthetic_request(principal):
    """A Starlette Request carrying a resolved principal, so the /chat route
    (which resolves its caller once, from request.state) runs a resume
    exactly as it runs a browser's re-attach."""
    from starlette.requests import Request

    from .authn import principal as principal_mod

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/chat",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 0),
    }
    request = Request(scope)
    setattr(request.state, principal_mod._STATE_KEY, principal)
    return request


async def _resume_row(row: dict, main) -> str:
    """One row: 'resumed', 'duplicate_suppressed' or 'skipped'."""
    intent_id = row["intent_id"]
    if main._live_generation_for(row["generation_id"]) is not None:
        return "skipped"  # this process is already running (or holding) it
    busy = main._live_generations.get(row["conversation_id"])
    if busy is not None and not busy.done:
        # The conversation has moved on to a newer generation. /chat would
        # replace it ("the newest message wins") — right for a person's
        # send, wrong for a sweep: the parked row waits for the browser's
        # re-attach or the next READY.
        return "skipped"
    stored = await db.run_in_thread(main._persisted_answer, row["conversation_id"], row["generation_id"])
    if main._is_answer(stored):
        # The answer exists (the process died between persisting it and
        # marking the row): heal, never answer again.
        await main._heal_completed_request(row)
        _count("duplicate_suppressed")
        return "duplicate_suppressed"
    try:
        chat_request = main.ChatRequest.model_validate({**(row.get("request") or {}), "intent_id": intent_id})
    except Exception as exc:  # noqa: BLE001 — a snapshot that no longer parses
        log.warning("continuity sweep: request %s cannot be resumed from its snapshot: %s",
                    intent_id, str(exc)[:200])
        return "skipped"
    principal = await db.run_in_thread(_principal_for, int(row["user_id"]))
    if principal is None:
        log.info("continuity sweep: request %s belongs to a disabled or removed account; left as is", intent_id)
        return "skipped"
    # A parked attempt's failure record (the viewer's copy of EXPIRED_LINE)
    # would sit beside the answer the resume writes: discard it, as a
    # person's Retry of a failed attempt does.
    await db.run_in_thread(db.delete_failure_record, row["conversation_id"], row["generation_id"])
    before = row["generation_id"]
    # The same door a re-attach uses: /chat finds the known intent with no
    # live generation and runs a new attempt from the snapshot. The
    # response is a stream nobody reads; the generation is detached and
    # persists its own answer.
    from fastapi import HTTPException

    try:
        await main.chat(chat_request, _synthetic_request(principal))
    except HTTPException as exc:
        # 409: the compare-and-swap lost to another resumer (a browser's
        # re-attach) — theirs, not a second answer.
        log.info("continuity sweep: request %s taken by another resumer (%s)", intent_id, exc.status_code)
        _count("duplicate_suppressed")
        return "duplicate_suppressed"
    after = await db.run_in_thread(db.get_chat_request, intent_id)
    if after is None or after["generation_id"] == before:
        # The compare-and-swap moved nothing: somebody else (a browser, a
        # sweep in another process) owns the resume.
        _count("duplicate_suppressed")
        return "duplicate_suppressed"
    _count("resumed")
    return "resumed"


async def resume_sweep(trigger: str) -> dict:
    """Resume every row the trigger allows (module docstring), once each.
    Returns the outcome counts; never raises."""
    global _sweep_lock
    from . import main  # lazy: main imports llm imports resilience imports this module

    statuses = ("queued", "interrupted") if trigger == "ready" else ("queued",)
    if _sweep_lock is None:
        _sweep_lock = asyncio.Lock()
    counts = {"resumed": 0, "duplicate_suppressed": 0, "skipped": 0}
    async with _sweep_lock:
        try:
            rows = await db.run_in_thread(
                db.list_resumable_chat_requests, statuses, max_age_s=resume_max_age_s(), limit=_SWEEP_LIMIT,
            )
        except Exception:  # noqa: BLE001 — a sweep that cannot read must not take the process down
            log.warning("continuity sweep (%s): could not list rows", trigger, exc_info=True)
            return counts
        for row in rows:
            try:
                outcome = await _resume_row(row, main)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad row must not stop the others
                log.warning("continuity sweep (%s): request %s not resumed: %s: %s",
                            trigger, row.get("intent_id"), type(exc).__name__, exc)
                outcome = "skipped"
            counts[outcome] = counts.get(outcome, 0) + 1
    if rows:
        log.info("continuity sweep (%s): %d row(s): %s", trigger, len(rows), counts)
    return counts


def _schedule_sweep(trigger: str) -> None:
    global _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        # One in flight: the READY edge it will see is the same READY.
        return
    _sweep_task = asyncio.create_task(resume_sweep(trigger), name=f"continuity-sweep-{trigger}")


def _on_ready() -> None:
    """The engine-state client's not-serving → serving edge."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    _schedule_sweep("ready")


def start() -> None:
    """Called from the FastAPI lifespan after the open rows have been
    reconciled: register for READY and sweep the rows a dead process
    parked. Idempotent."""
    global _installed
    engine_state.on_ready(_on_ready)
    _publish_queued()
    _installed = True
    _schedule_sweep("startup")


async def stop() -> None:
    global _sweep_task, _installed
    _installed = False
    if _sweep_task is not None:
        _sweep_task.cancel()
        try:
            await _sweep_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _sweep_task = None


def describe() -> dict:
    """What /health shows."""
    return {
        "queued_generations": _queued,
        "max_wait_s": queue_window_s(),
        "resume_max_age_s": resume_max_age_s(),
        "sweep_installed": _installed,
    }


def reset() -> None:
    """Tests only."""
    global _queued, _sweep_task, _sweep_lock, _installed
    _queued = 0
    _sweep_task = None
    _sweep_lock = None
    _installed = False
    _publish_queued()
