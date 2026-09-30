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

WHO MAY EXPIRE IT (round-2 review, resilience.py:549). One turn is many
model calls, and some of them are SIDECARS on the main URL — the fact
extractor or the route classifier when the router is configured onto the
main endpoint. A sidecar makes ONE attempt with no window
(resilience.sidecar_recovery_s), and a call with no window never enters
the hold and never expires it: app/resilience.py hands the hold only to a
call that has a window to wait, so a sidecar that gives up at once cannot
park the turn while the answer is still legitimately waiting.

THE PARK IS FINAL (round-2 review, continuity.py:187). Once a hold has
expired — the person has read EXPIRED_LINE, the row is parked — it is not
re-enterable: every further main-model call of the turn raises
QueuedForRecovery at once, whatever "upgrade, never a gate" handler sat
between the first raise and the caller. Without that a best-of-N turn
waited a SECOND full window and read both lines twice. A held wait that
the caller's OWN budget cancelled (a Deep Research stage's wait_for) is a
"cut": the next call of that turn parks instead of queueing again, and
the stage that was cut reports the park, never a stage timeout.

THE RESUME SWEEP (§8.4 v2). When the controller's verdict turns to READY —
and, for rows a dead process parked or a restart interrupted, at start-up —
every `queued` and `interrupted` row with a resumable snapshot and no live
generation in this process is resumed exactly once: the V29 compare-and-swap
on the row's generation_id is the lease (only one resumer can move it), a
row whose generation already has a durable answer is healed and counted
`duplicate_suppressed` instead of run again, and an `interrupted` row whose
attempt had already streamed tokens is NOT re-run — the partial the person
read stays (§8.4), the row is settled `failed` so a person may retry it.
`queued` rows are resumed whatever their age (someone parked them on
purpose); LLM_RESUME_MAX_AGE_S bounds only `interrupted` rows, measured
from the row's `created_at` — the moment the send was accepted. Two sweep
triggers never drop each other: a trigger that lands while a sweep is in
flight runs as soon as that sweep finishes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar
from typing import Awaitable, Callable, Optional

from . import breaker as _breaker
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

#: What an `interrupted` row that had already streamed tokens is settled
#: with when the sweep declines to re-run it (§8.4: the partial stays) —
#: one sentence when a partial was kept, another when the tab lost it.
INTERRUPTED_AFTER_TOKENS = (
    "The answer was interrupted after it had started; what was written is kept above. "
    "Retry to ask again."
)
INTERRUPTED_NOTHING_KEPT = (
    "The answer was interrupted after it had started and could not be resumed. "
    "Retry to ask again."
)


def interrupted_after_tokens_sentence(stored: Optional[dict]) -> str:
    """The truthful settlement sentence for an attempt interrupted after its
    first token: names the kept partial only when one exists."""
    return INTERRUPTED_AFTER_TOKENS if _is_partial(stored) else INTERRUPTED_NOTHING_KEPT


#: The two kinds of wait a hold records. `recovery` is the engine being
#: away (the resume bumps `attempt`; the wait is a queued generation for
#: CONTRACT §7.2); `admission` is a lane wait in front of a healthy engine
#: (app/admission.py) — same durable state, no new attempt, and counted
#: under llm_admission_* only.
RECOVERY = "recovery"
ADMISSION = "admission"


def resume_max_age_s() -> float:
    """An `interrupted` row accepted more than this long ago is not resumed
    by the sweep (LLM_RESUME_MAX_AGE_S): nobody is waiting for it any more,
    and a thread would only be surprised by an answer to a day-old
    question. Measured from the row's `created_at`. `queued` rows are not
    bound by it — a parked request is an explicit promise (CONTRACT §2
    DOWN: "never lost") — and neither is the browser's own re-attach."""
    return max(0.0, float(settings.llm_resume_max_age_s))


#: How many rows one sweep may resume. A larger backlog is drained by the
#: next READY edge (and by the browsers that come back).
_SWEEP_LIMIT = 200


class QueuedForRecovery(RuntimeError):
    """The wait for the main model outran LLM_QUEUE_MAX_WAIT_S (or the
    turn's own budget cut it while the engine was still away).

    Raised by app/resilience.py on a chat turn INSTEAD of ModelUnavailable:
    the row stays `queued`, the person has read EXPIRED_LINE, and the chat
    worker ends the stream without failing the request (main.py). Never
    raised on a task without a hold. Any handler between the raise and the
    worker must let it through: `except Exception` blocks on the chat path
    re-raise it explicitly.
    """

    def __init__(self, waited_s: float) -> None:
        self.waited_s = waited_s
        super().__init__(f"main model still recovering after {waited_s:.0f}s; request kept queued")


class LeaseLost(RuntimeError):
    """The row this generation was resuming now names ANOTHER generation:
    a resumer in a different process took it over under its own lease
    while this one slept on READY. Continuing would produce a second
    answer under a second generation_id, which the V29 dedupe cannot
    catch; the worker ends the turn instead (main.py)."""

    def __init__(self, intent_id: str, owner: str) -> None:
        self.intent_id = intent_id
        self.owner = owner
        super().__init__(f"request {intent_id} is now held by generation {owner}")


# ---------------------------------------------------------------------------
# The hold: one per chat turn
# ---------------------------------------------------------------------------

#: In-memory count of generations currently held for the primary in this
#: process (CONTRACT §7.2 `llm_queued_generations`; signal 16). Recovery
#: waits only: a lane wait in front of a healthy engine is not a generation
#: waiting for the primary (it has llm_admission_waiting).
_queued = 0
#: Rows durably `queued` in the database as of the last count — parked by
#: an expired hold, or by a process that is gone. The published gauge is
#: the larger of the two (see _publish_queued).
_durable_queued = 0
#: When (time.monotonic) the observation `_durable_queued` came from. Two
#: observers deliver counts out of turn — a /health snapshot read in a
#: thread, a resume's own re-count, a sweep's — and the LATER observation
#: is the truth: a count taken before this stamp is dropped, never
#: published over a fresher one.
_durable_observed_at = 0.0


def _publish_queued() -> None:
    """`llm_queued_generations` = max(holds in this process, rows `queued`
    in the database). The in-memory count is exact for what THIS process
    holds and moves at once; the durable count catches what an expired
    hold or a dead process left `queued` (it is refreshed by the /health
    work snapshot, by every sweep, by every park AND by every resume that
    moves a row out of `queued` — drill 5 of 2026-09-12 read "still 1"
    after the queued turn had resumed and completed, because the /health
    snapshot taken during the outage was the last word on the durable
    half), so a long DOWN never reads as "nothing queued" while thirty
    rows wait. max(), not the sum: a held generation's row is one of the
    `queued` rows."""
    metrics.set_gauge(
        "llm_queued_generations", float(max(_queued, _durable_queued)),
        "Accepted generations waiting for the primary model (held in this process or durably queued).",
    )


def queued_count() -> int:
    return _queued


def note_durable_queued(count: int, *, observed_at: Optional[float] = None) -> None:
    """The durable `queued` row count, as an observer read it — the
    /health work snapshot (app/health.py), a sweep, a resume — folded
    into the gauge. `observed_at` (time.monotonic) is when the count was
    taken; None means "just now". An observation older than the one
    already published is dropped: a /health snapshot that read the row
    as `queued` a moment before the resume moved it must not land AFTER
    the resume's own re-count and pin the gauge at the stale value."""
    global _durable_queued, _durable_observed_at
    stamp = time.monotonic() if observed_at is None else float(observed_at)
    if stamp < _durable_observed_at:
        return
    _durable_observed_at = stamp
    _durable_queued = max(0, int(count))
    _publish_queued()


async def _refresh_durable_queued() -> None:
    """Re-count the `queued` rows now (a thread hop; best-effort). Stamped
    from BEFORE the read, so a slower observer that started earlier can
    never overwrite this one with what it saw."""
    observed_at = time.monotonic()
    try:
        count = await db.run_in_thread(db.count_chat_requests, "queued")
    except Exception:  # noqa: BLE001 — a gauge refresh never breaks a turn
        log.debug("continuity: could not count queued rows", exc_info=True)
        return
    note_durable_queued(count, observed_at=observed_at)


class Hold:
    """The chat worker's handle on its generation while it waits.

    `gen` is duck-typed (main.LiveGeneration): `intent_id`, `generation_id`,
    `attempt`, `retry_reason`, `request_status`, `parked`. `notify` sends
    one status line to the person; it is the same callable the wait
    notifier holds, so the two never interleave.
    """

    __slots__ = (
        "gen", "notify", "kind", "queued_at", "announced", "row_parked", "expired",
        "resumed_once", "cut", "waited_s", "lost", "announced_outage",
    )

    def __init__(self, gen, notify: Optional[Callable[[str], Awaitable[None]]]) -> None:
        self.gen = gen
        self.notify = notify
        #: The kind of the wait in progress, or None when not waiting.
        self.kind: Optional[str] = None
        self.queued_at: Optional[float] = None
        #: Whether the person has read the line for THIS wait.
        self.announced = False
        #: Whether the person has read QUEUED_LINE for THIS outage: sticky
        #: until a call of the turn is actually SERVED (`served()`), so the
        #: siblings of one turn — best-of-N candidates, a route+answer pair
        #: — that re-enter the wait while one of them is the HALF_OPEN
        #: canary do not say the line again (round-2 review,
        #: continuity.py:208). A new outage after a served call earns a
        #: new line.
        self.announced_outage = False
        #: Whether the row was moved to `queued` for this wait (or an earlier
        #: one of this turn that was cut: the row is still `queued` then).
        self.row_parked = False
        #: Sticky: the turn is parked. No further wait is entered — every
        #: entry raises QueuedForRecovery (module docstring, THE PARK IS FINAL).
        self.expired = False
        #: The turn resumes as a new attempt ONCE (CONTRACT §8.3 step 4): a
        #: second recovery wait inside the same turn — the engine came back,
        #: served a sidecar call, went away again — is the same attempt
        #: still going.
        self.resumed_once = False
        #: A recovery wait of this turn was cancelled while the engine was
        #: still away — by the caller's own budget, not by the engine coming
        #: back. The next entry parks instead of queueing again.
        self.cut = False
        #: Seconds spent in recovery waits so far (what the park reports).
        self.waited_s = 0.0
        #: Sticky: the row was taken over by another resumer (LeaseLost).
        #: Every further main-model call of the turn raises it again, so
        #: no handler between the first raise and the worker can let this
        #: generation go on to answer.
        self.lost: Optional[LeaseLost] = None

    @property
    def waiting(self) -> bool:
        return self.kind is not None

    @property
    def parked(self) -> bool:
        return self.expired

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

    async def enter(self, kind: str, line: Optional[str] = QUEUED_LINE) -> None:
        """The wait begins (idempotent within one wait): the row goes
        `queued`, a recovery wait is counted in the gauge, the person reads
        `line` once (`None`: say nothing — the caller speaks). A parked
        turn, or one whose earlier recovery wait was cut, does not wait
        again: it parks (raises QueuedForRecovery)."""
        global _queued
        if self.lost is not None:
            raise self.lost
        if self.expired:
            raise QueuedForRecovery(self.waited_s)
        if self.kind is not None:
            return
        if kind == RECOVERY and self.cut:
            await self.park()  # raises
        self.kind = kind
        self.queued_at = time.monotonic()
        self.announced = False
        if kind == RECOVERY:
            _queued += 1
            _publish_queued()
            # Only a RECOVERY wait parks the row: `queued` means "resume when
            # the main model is READY", and the sweep acts on it. A lane wait
            # happens while the engine IS serving — its row keeps the ordinary
            # V29 accepted/running durability and must neither be counted as
            # waiting for the primary nor be picked up by the READY sweep
            # (review round 2: lane parks inflated the durable queued count).
            await self._park_row()
        log.info("continuity generation=%s intent=%s queued kind=%s",
                 getattr(self.gen, "generation_id", "?"), getattr(self.gen, "intent_id", "?"), kind)
        if line is not None and not self.announced:
            self.announced = True
            if kind == RECOVERY:
                if self.announced_outage:
                    return  # a sibling already said it for this outage
                self.announced_outage = True
            await self._say(line)

    def served(self) -> None:
        """A call of the turn was served (a response, a first chunk): the
        outage, if there was one, is over — the next earns a new line."""
        self.announced_outage = False

    async def _park_row(self) -> None:
        """The row goes `queued` under this generation (a no-op when it
        already is, or when the turn has no row)."""
        gen = self.gen
        if self.row_parked or not getattr(gen, "intent_id", None):
            return
        try:
            row = await db.run_in_thread(db.park_chat_request, gen.intent_id, gen.generation_id)
        except Exception as exc:  # noqa: BLE001 — bookkeeping, never a reason to lose the turn
            log.warning("continuity: could not park request %s: %s: %s",
                        gen.intent_id, type(exc).__name__, exc)
            row = None
        if row is not None:
            self.row_parked = True
            gen.request_status = "queued"

    def _observe_wait(self) -> float:
        """Seconds of the wait in progress; a RECOVERY wait is observed in
        llm_queue_wait_seconds (CONTRACT §7.2 — lane waits have their own
        histogram in app/admission.py)."""
        waited = max(0.0, time.monotonic() - (self.queued_at or time.monotonic()))
        if self.kind == RECOVERY:
            self.waited_s += waited
            metrics.observe("llm_queue_wait_seconds", waited,
                            "Seconds a generation waited for the primary model.")
        return waited

    async def resume(self) -> None:
        """The wait is over and the call goes through: the row is `running`
        again under the SAME generation; a recovery wait is a new attempt
        (`attempt` + 1, `retry_reason = recovery`), counted once. Raises
        LeaseLost when the row now belongs to another generation.

        The gauge moves HERE, both halves: `_clear()` publishes the
        in-process drop, and when this call moved the durable row out of
        `queued` (or found it taken, or Stopped), the durable half is
        re-counted at once. Before drill 5 of 2026-09-12 the durable half
        was refreshed only by /health, a sweep and a park — never by the
        resume — so the /health snapshot that had counted this very row
        during the outage kept `llm_queued_generations` at 1 after the
        turn had resumed and completed on the primary (max(0, stale 1)),
        until the next /health happened to run."""
        if self.kind is None:
            return
        kind = self.kind
        waited = self._observe_wait()
        recovery = kind == RECOVERY
        new_attempt = recovery and not self.resumed_once
        gen = self.gen
        #: Whether the durable row left `queued` under this call — it was
        #: moved to `running` here, or a Stop had already moved it.
        row_left_queued = False
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
                self.row_parked = False
                row_left_queued = True
            else:
                owner = await self._row_owner()
                if owner is not None and owner != gen.generation_id:
                    # Taken over under another lease (a sweep in another
                    # process): this generation must not answer.
                    self._clear()
                    self.cut = False
                    log.warning("continuity generation=%s intent=%s lease lost to generation %s",
                                gen.generation_id, gen.intent_id, owner)
                    self.lost = LeaseLost(str(gen.intent_id), owner)
                    # Theirs now, and `running` under their lease: the
                    # durable half must not keep counting it as queued.
                    await _refresh_durable_queued()
                    raise self.lost
                # A Stop landed meanwhile (the row is `cancelled`): the
                # cancellation reaches this task on its own; the attempt
                # still moves so the ledger is truthful about the wait.
                if new_attempt:
                    gen.attempt = int(getattr(gen, "attempt", 1)) + 1
                row_left_queued = owner is not None  # the row exists and is not `queued` any more
        elif new_attempt:
            gen.attempt = int(getattr(gen, "attempt", 1)) + 1
        if recovery:
            gen.retry_reason = RECOVERY
            self.cut = False
        if new_attempt:
            self.resumed_once = True
            metrics.inc("llm_resumed_generations_total",
                        "Generations resumed after waiting for the primary, by outcome.", outcome="resumed")
        log.info("continuity generation=%s intent=%s resumed kind=%s after %.0fs attempt=%s",
                 getattr(gen, "generation_id", "?"), getattr(gen, "intent_id", "?"), kind, waited,
                 getattr(gen, "attempt", "?"))
        self._clear()  # the in-process half drops and is published
        if row_left_queued:
            await _refresh_durable_queued()  # …and the durable half is re-counted now

    async def _row_owner(self) -> Optional[str]:
        """The generation the row names now, or None when it cannot be read."""
        try:
            row = await db.run_in_thread(db.get_chat_request, self.gen.intent_id)
        except Exception:  # noqa: BLE001
            return None
        return str(row["generation_id"]) if row else None

    def abandon(self) -> None:
        """The wait ended without the call going through — a Stop, a
        cancellation, a parent task torn down. The gauge moves and, for a
        recovery wait, the turn is marked CUT: if the turn goes on to
        another main-model call while the engine is still away, that call
        parks instead of waiting a second window (module docstring). The
        row's terminal status is whoever ended the turn's to write."""
        if self.kind is None:
            return
        if self.kind == RECOVERY:
            self.cut = True
        self._observe_wait()
        self._clear()

    async def expire(self) -> None:
        """The wait outran the window: the row stays `queued` (parked for
        the sweep), the person reads EXPIRED_LINE, and the turn ends."""
        if self.kind is None:
            return
        self._observe_wait()
        self._clear()
        await self.park()

    async def park(self) -> None:
        """Park the turn now — the wait is over (expired, or cut by the
        caller's budget while the engine was still away): the row stays
        `queued` for the resume sweep, the person reads EXPIRED_LINE once,
        and QueuedForRecovery ends the turn. Final: a parked hold is never
        re-entered."""
        gen = self.gen
        if self.expired:
            raise QueuedForRecovery(self.waited_s)
        if self.kind is not None:
            self._observe_wait()
            self._clear()
        await self._park_row()  # a cut turn's row is queued already; a give-up's may not be
        self.expired = True
        self.cut = False
        setattr(gen, "parked", True)
        metrics.inc("llm_resumed_generations_total",
                    "Generations resumed after waiting for the primary, by outcome.", outcome="expired")
        log.warning("continuity generation=%s intent=%s parked after %.0fs: %s",
                    getattr(gen, "generation_id", "?"), getattr(gen, "intent_id", "?"), self.waited_s,
                    "row stays queued for the resume sweep")
        await self._say(EXPIRED_LINE)
        await _refresh_durable_queued()
        raise QueuedForRecovery(self.waited_s)

    def _clear(self) -> None:
        global _queued
        if self.kind == RECOVERY:
            _queued = max(0, _queued - 1)
            _publish_queued()
        self.kind = None
        self.queued_at = None
        self.announced = False


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
#: A trigger that landed while a sweep was in flight; run when it finishes.
_pending_trigger: Optional[str] = None
_installed = False

#: What the sweep lists, whatever the trigger: rows a process parked and
#: rows a restart (or an orderly shutdown) interrupted.
_SWEEP_STATUSES = ("queued", "interrupted")


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


#: The marks the sweep leaves on its synthetic request's state: the /chat
#: route reads `RESUME_SWEEP_STATE` to know a sweep is calling (it must
#: never replace a person's live generation) and writes the generation it
#: started under `RESUMED_GENERATION_STATE`, so the outcome is counted from
#: what actually happened, not from a before/after guess.
RESUME_SWEEP_STATE = "resume_sweep"
RESUMED_GENERATION_STATE = "resumed_generation_id"


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
    setattr(request.state, RESUME_SWEEP_STATE, True)
    return request


def _is_partial(stored: Optional[dict]) -> bool:
    """A failure record that carries text: the viewer's copy of what had
    streamed before the attempt died (frontend markInterrupted, or
    main._store_failure). Not an answer, but not nothing either."""
    if stored is None:
        return False
    if "error" not in (stored.get("meta") or {}):
        return False
    return bool(str(stored.get("content") or "").strip())


async def _resume_row(row: dict, main) -> str:
    """One row: 'resumed', 'duplicate_suppressed', 'kept_partial' or 'skipped'."""
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
    if str(row.get("status")) == "interrupted":
        # CONTRACT §8.4: an attempt that died AFTER its first token keeps
        # its partial and is never re-run by itself. The evidence is the
        # ledger (usage_events.ttft_ms, written by an orderly shutdown) or
        # the viewer's persisted partial (a crash leaves only that).
        streamed = await db.run_in_thread(db.generation_streamed, row["generation_id"])
        if streamed or _is_partial(stored):
            await db.run_in_thread(
                db.set_chat_request_status, intent_id, "failed",
                error=interrupted_after_tokens_sentence(stored),
            )
            log.info("continuity sweep: request %s streamed before it was interrupted; partial kept, "
                     "row settled failed for a person's retry", intent_id)
            return "kept_partial"
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
    # A parked attempt's EMPTY failure record (the viewer's copy of
    # EXPIRED_LINE, no text) would sit beside the answer the resume writes:
    # discard it, as a person's Retry of a failed attempt does. A record
    # with text was handled above.
    await db.run_in_thread(db.delete_failure_record, row["conversation_id"], row["generation_id"])
    # The same door a re-attach uses: /chat finds the known intent with no
    # live generation and runs a new attempt from the snapshot. The
    # response is a stream nobody reads; the generation is detached and
    # persists its own answer.
    from fastapi import HTTPException

    request = _synthetic_request(principal)
    try:
        await main.chat(chat_request, request)
    except HTTPException as exc:
        if exc.status_code == 409 and "busy" in str(exc.detail):
            # A person's newer send in the same conversation won (the race
            # the round-2 review named): the row waits for the next READY.
            log.info("continuity sweep: request %s yields to a newer message in its conversation", intent_id)
            return "skipped"
        # 409: the compare-and-swap lost to another resumer (a browser's
        # re-attach) — theirs, not a second answer.
        log.info("continuity sweep: request %s taken by another resumer (%s)", intent_id, exc.status_code)
        _count("duplicate_suppressed")
        return "duplicate_suppressed"
    started = getattr(request.state, RESUMED_GENERATION_STATE, None)
    after = await db.run_in_thread(db.get_chat_request, intent_id)
    if not started or after is None or after["generation_id"] != started:
        # /chat attached to a generation somebody else started (a browser's
        # re-attach won the compare-and-swap) or replayed a durable answer:
        # theirs, not a resume of ours.
        _count("duplicate_suppressed")
        return "duplicate_suppressed"
    _count("resumed")
    return "resumed"


async def resume_sweep(trigger: str) -> dict:
    """Resume every `queued` and `interrupted` row the rules allow (module
    docstring), once each. Returns the outcome counts; never raises."""
    global _sweep_lock
    from . import main  # lazy: main imports llm imports resilience imports this module

    if _sweep_lock is None:
        _sweep_lock = asyncio.Lock()
    counts = {"resumed": 0, "duplicate_suppressed": 0, "skipped": 0}
    async with _sweep_lock:
        try:
            rows = await db.run_in_thread(
                db.list_resumable_chat_requests, _SWEEP_STATUSES,
                max_age_s=resume_max_age_s(), limit=_SWEEP_LIMIT,
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
        await _refresh_durable_queued()
    if rows:
        log.info("continuity sweep (%s): %d row(s): %s", trigger, len(rows), counts)
    return counts


async def _sweep_loop(trigger: str) -> None:
    """One sweep, then every trigger that landed while it ran."""
    global _pending_trigger
    while True:
        await resume_sweep(trigger)
        if _pending_trigger is None:
            return
        trigger, _pending_trigger = _pending_trigger, None


def _schedule_sweep(trigger: str) -> None:
    """Run a sweep now, or — while one is in flight — right after it. A
    trigger is never dropped: the READY edge that lands during the
    start-up sweep would otherwise be lost, and with it the rows that
    sweep had not listed yet (round-2 review, continuity.py:486)."""
    global _sweep_task, _pending_trigger
    if _sweep_task is not None and not _sweep_task.done():
        _pending_trigger = trigger
        return
    _pending_trigger = None
    _sweep_task = asyncio.create_task(_sweep_loop(trigger), name=f"continuity-sweep-{trigger}")


def _on_ready() -> None:
    """The engine-state client's not-serving → serving edge."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    _schedule_sweep("ready")


def _on_breaker_transition(engine: str, frm: str, to: str, reason: str) -> None:
    """The main breaker closing again (a canary served) is a READY edge of
    its own — the only one there is while the controller is unreachable
    and the breaker works from observed failures alone."""
    if not _installed or engine != _breaker.MAIN or to != _breaker.CLOSED or frm == _breaker.CLOSED:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    _schedule_sweep("breaker_closed")


def start() -> None:
    """Called from the FastAPI lifespan after the open rows have been
    reconciled: register for READY (the controller's edge and the
    breaker's), then sweep the rows a dead process parked and a restart
    interrupted. Idempotent."""
    global _installed
    engine_state.on_ready(_on_ready)
    _breaker.on_transition(_on_breaker_transition)
    _publish_queued()
    _installed = True
    _schedule_sweep("startup")


async def stop() -> None:
    global _sweep_task, _installed, _pending_trigger
    _installed = False
    _pending_trigger = None
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
        "queued_rows": _durable_queued,
        "max_wait_s": queue_window_s(),
        "resume_max_age_s": resume_max_age_s(),
        "sweep_installed": _installed,
    }


def reset() -> None:
    """Tests only."""
    global _queued, _durable_queued, _durable_observed_at, _sweep_task, _sweep_lock, _installed, _pending_trigger
    _queued = 0
    _durable_queued = 0
    _durable_observed_at = 0.0
    _sweep_task = None
    _sweep_lock = None
    _pending_trigger = None
    _installed = False
    _publish_queued()
