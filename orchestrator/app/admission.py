"""Long-context admission — three lanes in front of the main model (CONTRACT §6.7).

WHY THIS EXISTS. The fault that took the engine down on 2026-09-11 fired
inside vLLM's GDN prefill kernel under nine concurrent mixed
prefill+decode requests, and the second tenant reproduces that load shape
at will (docs/availability/INCIDENT-2026-09-11-vllm.md, hypothesis H). The
orchestrator cannot fix the kernel, but it can stop feeding it the worst
case: a very large prefill arriving while nine other requests are being
scheduled. So every orchestrator request passes a lane before it reaches
vLLM:

    NORMAL   prompt ≤ ADMISSION_LONG_THRESHOLD_TOKENS and an ordinary
             planned output: at most ADMISSION_NORMAL_MAX generations at
             once, of which /v1 may hold all but
             ADMISSION_CHAT_RESERVED_NORMAL_SLOTS (PRIORITY below);
    LONG     prompt above the threshold: ONE at a time. Before it starts, the
             engine is asked — through the controller's engine sample,
             never /metrics itself — to report `requests_running` ≤
             ADMISSION_LONG_IDLE_MAX, for at most ADMISSION_LONG_WAIT_S.
             The NORMAL lane keeps admitting during that wait: the second
             tenant's raw-port traffic can keep the engine busy for the
             whole bound, and closing NORMAL for it would turn one large
             document into ten minutes of no answers for everyone else
             (round-2 review, admission.py:296). From the moment the long
             request is ADMITTED — idle reached, or the wait expired and
             it proceeds — the NORMAL (and LONG_OUTPUT) lane is closed until
             the long request's FIRST TOKEN: no new prefill mixes with the
             large one. Then the lanes reopen and the long generation decodes
             beside ordinary traffic like any other. Time a waiter spends
             behind that closure does not count against its own bound: it is
             never refused with `timeout` for a wait it did not choose.
             It does not always proceed after the bound (LONG BESIDE LONG
             ANSWERS), and a /v1 one closes the lanes only within a budget
             (THE /v1 CLOSURE BUDGET).
    LONG_OUTPUT  an ordinary prompt with a planned output above its origin's
             threshold (THE LONG-OUTPUT LANE below): at most
             ADMISSION_LONG_OUTPUT_MAX_SEQS at once AND within the KV budget.

THE CLOSURE IS BOUNDED (F044, 2026-09-13). A non-streaming call has no first
token to observe — it returns when the whole generation is done — so until
this date a LONG json_completion (an Artifact Studio compose over a large
upload, a Deep Research step) kept NORMAL closed for its entire generation,
up to the 4200 s wall clock, and the waiters behind it, whose bound is
deferred during a closure, waited all of it. Now every LONG admission arms a
prefill grace sized from the prompt (`closure_grace_s`: a floor plus the
tokens at a prefill rate below the slowest one measured, capped at
LONG_CLOSURE_MAX_S) and reopens the lanes when it expires, first token or not.
And a waiter defers its bound for at most LONG_CLOSURE_MAX_S of closure in
total — so no wait, however many LONG requests close the lane in turn, is
longer than its own bound plus that ceiling. A per-request bound set with
`set_wait_bound_s` (publicapi: 30 s for a synchronous response, which must
answer before Cloudflare's 100 s) is never deferred at all.

The second tenant's raw-port traffic is outside these lanes (it does not
pass through this process); that is documented, not solved, here.

THE LONG-OUTPUT LANE (2026-09-13). The owner's goal of that date lets a /v1
answer run to 1,000,000 output tokens: with a small prompt that is a NORMAL
request by size, ~3 h on one engine sequence, growing to 481 of the 799 KV
blocks. In NORMAL it would hold one of the ten slots for the whole three hours
and could be preempted — a recompute of up to ~800 s with prefix caching off —
by the engine when a second one arrived. So the lane is chosen from the
planned output as well as the prompt (`lane_for`):

- LONG if the prompt is above ADMISSION_LONG_THRESHOLD_TOKENS (any origin);
- else LONG_OUTPUT if `max_tokens` is above the origin's threshold —
  /v1: ADMISSION_V1_LONG_OUTPUT_THRESHOLD_TOKENS = 8,192 (the public default
  and pre-2026-09-13 ceiling; /v1 runs thinking-off, so max_tokens is what the
  caller asked); chat: ADMISSION_CHAT_LONG_OUTPUT_THRESHOLD_TOKENS = 65,536,
  strictly above MAX_OUTPUT_TOKENS, because every Smart chat turn is SENT
  65,536 though its measured output p99 is ~2K — ordinary chat never leaves
  NORMAL;
- else NORMAL. `max_tokens=None` (a caller that does not pass it) is NORMAL,
  exactly the behaviour before this lane existed.

A LONG_OUTPUT request waits until a seat is free (2) AND its projected KV fits
AND no LONG prefill holds the lane closed AND no LONG request is waiting for
the engine to go idle, for ADMISSION_LONG_OUTPUT_WAIT_S (600 s) or the
per-request bound publicapi sets through `set_wait_bound_s` (30 s synchronous,
3600 s background). Past the bound it is AdmissionRejected(lane="long_output",
reason="timeout", retry_after_s=ADMISSION_LONG_OUTPUT_RETRY_AFTER_S = 60): a
lane held by a 3-hour job must not be retried every 5 s.
ADMISSION_LONG_OUTPUT_MAX_SEQS=0 closes the lane: every long answer is refused
`capacity` at once (a kill switch that needs an orchestrator restart only).

LONG BESIDE LONG ANSWERS (adversarial review 2026-09-13). The GDN fault needs
a prefill and a decode in one step (split_non_spec requires num_prefills > 0
and num_decodes > 0), and a decode-only sequence supplies the decode half —
so a LONG_OUTPUT decoder is NOT subtracted from the idle test: a 950K prefill
beside two decoders would be ~116 chunked steps of exactly the fault shape,
and production has no evidence for it (the only large-KV run on candidate B,
2026-09-12 09:04-09:15Z, ran alone). Before the build a LONG request that
never saw an idle engine proceeded after its bound, a rare fallback; with
3-hour decoders it would become the normal path. So: while a LONG request
waits for idle, new LONG_OUTPUT admissions are held (they would only add
running work), and when its bound runs out with this process's own
LONG_OUTPUT sequences still on the engine it is refused `timeout`
(Retry-After 60) instead of proceeding. It still proceeds after the bound for
work this process cannot hold back — NORMAL decodes that end in minutes, the
second tenant — except a /v1 request, which is refused rather than closing
the lanes on a chat turn already waiting for NORMAL. With no controller
sample the fallback counts NORMAL and LONG_OUTPUT occupancy together.

THE /v1 CLOSURE BUDGET (adversarial review 2026-09-13). A closure costs chat
its NORMAL lane for the whole prefill (~800 s for 950K tokens), and with no
usage limit one /v1 client sending a 950K prompt every 1,000 s kept chat's
median wait at ~400 s. So /v1-origin LONG requests may only close the lanes
for ADMISSION_V1_LONG_CLOSURE_DUTY (0.10) of any ADMISSION_V1_LONG_CLOSURE_WINDOW_S
(7200 s): a /v1 LONG waiter is seat-blocked unless the closure seconds of
/v1 LONG requests in the window, plus its own projected `closure_grace_s`, fit
720 s — or the window holds no /v1 closure at all (a prompt too big for the
budget runs alone rather than never: a 950K one, 1,010 s projected and ~800 s
real, then closes the lanes at most 800 of every ~8,000 s, 10 %). A /v1
waiter that runs out of its bound this way is refused `timeout` with
Retry-After 60. Chat LONG requests are not budgeted: the closure is the price
of a person's own document.

KV BUDGET (app/kv_budget.py has the arithmetic and the 2026-09-13 numbers).
LONG and LONG_OUTPUT requests commit a projected, block-exact charge —
(3 + ceil(min(prompt + max_tokens, window) / 2096)) × 2096; 1,008,176 tokens
for a 1M request — against floor(pool × (1 − 0.35)) = 1,081,080 tokens, so
one full window fits and two never do. /v1 NORMAL requests commit their
charge too, and everything committed must fit the managed limit
floor(pool × (1 − 0.15)) = 1,413,720 (kv_budget, THE MANAGED LIMIT): nine
131K-prompt /v1 answers beside a 1M job would otherwise be 1,111 of 799
blocks. Chat NORMAL is never charged. A charge is released synchronously (no
await) when the ticket releases — from the stream's end, close, the op
raising, or `_LaneStream.__del__` as a garbage-collection backstop — because a
lost release pins up to 60 % of the budget for hours. There is no auto-expiry
(expiring a live generation's charge would over-commit the engine);
llm_admission_kv_oldest_charge_age_seconds and a warning after 4 h make a
leak alertable instead. No long grant is made while the controller's
kv_cache_usage says the reserve is already used up.

ONE ARBITER grants a LONG request its seat and its KV together, and a
LONG_OUTPUT request its seat and its KV together — a document waiting for the
one LONG seat is in the KV order from the moment it arrives, so a /v1 1M
request that comes later can never take the KV the document is about to need
(adversarial review 2026-09-13: back-to-back chat documents, 38 of 40 refused
behind a later 1M request). The order is chat first, FIFO within a class. A
waiter that cannot be granted yet — seat taken, lane closed, KV short — is
not skipped outright: the head of each class keeps its place in KV, and a
later waiter passes it only if the charges admitted since the head arrived,
plus the later one, plus the head's own charge still fit (kv_budget,
BACKFILL). So a big request is never starved by small ones; small ones are
not held for a big one's whole bound when they cannot delay it (a 950K chat
document that cannot fit beside a 400K job no longer holds a 16K /v1 answer
for 600 s); and a chat document that fits beside a running job is never held
behind a /v1 request that does not. A /v1 head is protected against chat only
once it has waited ADMISSION_V1_KV_PROMOTE_S (300 s) AND chat charges are what
keep it out (it would fit without them): then it goes ahead of every chat
waiter that arrived after it. /v1 is never starved by chat in KV, and never
promoted when a /v1 job is what it waits for (a 400K /v1 job and a waiting 1M
/v1 request no longer refuse fitting chat documents for an hour).

PRIORITY (2026-09-13, revised by the adversarial review of that date). Every
lane serves two origin classes — `chat` (the default: chat turns, Artifact
Studio, Deep Research, video, the resume sweep — product work for a person)
and `v1` (set by publicapi inside its producer task with
`set_origin(ORIGIN_V1)`) — from one FIFO deque each, with a synchronous grant
rule (`_pump`, no await). Grants happen at release, so a newcomer cannot take
a freed slot ahead of the waiters (the asyncio.Condition + notify_all lanes
this replaced were not even FIFO). The NORMAL rule when both classes have an
admissible head:

- /v1 goes first only if it holds fewer than its SEAT SHARE,
  max(1, (capacity − reserved) // (ADMISSION_CHAT_WEIGHT + 1)) = 2 of 10, AND chat has had
  ADMISSION_CHAT_WEIGHT (3) grants in a row that /v1 could have taken;
- otherwise chat goes first;
- and at any time a /v1 request above its share takes a seat only if
  ADMISSION_CHAT_RESERVED_NORMAL_SLOTS (2) seats stay free after it.

WHY SEATS, NOT GRANTS. The first build counted grants ("/v1 gets one in
four"), but a /v1 answer of up to 8,192 tokens holds its seat many times
longer than a chat turn (output p50 130): on the stub a /v1 flood took ~70 %
of NORMAL seat time and refused 370 of 911 chat turns at 0.5/s that the
engine served alone. Counting the seats /v1 HOLDS bounds its share of seat
time while chat waits; the streak still hands /v1 a turn below that share, so
/v1 is never starved even at capacity 1. Only a chat grant /v1 could have
taken counts: a chat turn that timed out, or one that took the reserved seat
/v1 may not hold, does not. When only one class can be admitted it goes, so a
seat never sits idle because of the order. The depth bound
ADMISSION_MAX_WAITING is PER CLASS: a flood of /v1 waiters cannot refuse a
chat turn at the door. RESERVED SEATS ARE HEADROOM, NOT A /v1 CAP: the first
build let /v1 hold capacity − R seats, so with chat already in the R seats the
next chat turn queued behind /v1 answers of up to 8,192 tokens (400 of them
at once: chat wait p95 76.3 s with R=1, 26.1 s with R=2). Counting every
holder instead, a seat /v1 frees stays free for chat while fewer than R are
free, so chat waits only when its own demand fills the lane. The cost is R
idle seats while /v1 saturates and chat is silent (8 of 10 streams: 279 → 272
tok/s on the stub curve for R 1 → 2). What this buys, measured on the stub
(not promised): under two-class contention chat is served nearly strictly
first and /v1 absorbs the backlog; under sustained overload of both classes
/v1 keeps its seat share.

WHY NORMAL STAYS 10 (capacity decision 2026-09-13; evidence corrected by the
adversarial review of that date). Candidate B, the production build since
2026-09-12 08:39Z, has run mixed load at ≥ 8 running for 2.19 h, ≥ 10 for
1.86 h and ≥ 12 for 0.014 h (max 16), nearly all of it the 120-min
cluster-soak (8,851 of 8,851 ok), with 0 faults. Against the pinned no-MTP
build's rate (2 faults in 1.44 h at ≥ 8) that is P ≈ e^-3.04 ≈ 0.048: a weak
rejection of the old rate, and the WHOLE evidence for c=10 on B. Since the
soak the engine has been idle (max 3 running; ~120 arrivals/h are the
controller's synthetic probes), so days of uptime add essentially no mixed
load: the acceptance bar for a larger lane is load-hours — hours at ≥ 10
running with mixed prefill/decode and 0 Xid — not days. The ceiling these
lanes allow is 13 sequences (10 NORMAL + 2 LONG_OUTPUT + 1 LONG); the second
tenant's raw-port concurrency of 10 on top makes 23, which B has never run.
The stub engine curve saturates (NORMAL 10 → 281 tok/s, 16 → 302), real chat
peaked at 1 active and 0 waiting over the 21 h of admission metrics, and the
orchestrator used ~0.2 cores at every N. So raising the lane buys ≤ 9 %
throughput nobody needs yet against a 3-minute-downtime crash risk the
evidence cannot bound. A larger lane is earned by the staged soak (NORMAL =
highest passed stage − 2), with the tenant-shaped workload beside it, and set
by env: an orchestrator restart, never an engine restart.

WHAT THE PERSON SEES. A wait is durable and truthful: on a chat turn the
V29 row says `queued` (app/continuity.py) and one line is said,
LONG_LINE for a large document, LONG_OUTPUT_LINE for a long answer and
NORMAL_LINE for a slot behind other work, each with how many are ahead. A
wait that outruns its bound is a rejection with a reason the metrics name —
`timeout` — and a line the chat worker turns into the TIMEOUT sentence; a
line that is already too deep refuses newcomers at once — `capacity` —
rather than promising a wait it cannot keep.

SIZING. The lane is chosen from the prompt as it will be sent (the sized
messages `context.fit_request` returns). When fit_request measured those very
messages exactly, that count decides — no second /tokenize round trip, which
was the CPU-bound pre-pass finding of 2026-09-05. Otherwise a prompt is
NORMAL at once only when `context.upper_bound_messages` (bytes, which no
script can game) is under half the threshold, LONG at once when the
estimate is over twice it, and /tokenize decides the band between. Until
2026-09-13 the "certainly small" verdict came from the three-characters-per-
token estimate, and a non-Latin prompt of ~190,000 real tokens passed it
into the NORMAL lane (N013).

Per event loop, like the breaker registry: a lane is asyncio state and
the test suite runs a loop per test. So is the KV ledger — a multi-process
orchestrator would multiply every cap and the budget by its worker count.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import weakref
from collections import deque
from contextvars import ContextVar, Token
from typing import Awaitable, Callable, Deque, Dict, Iterator, List, Optional, Sequence, Tuple, TypeVar

from . import context, continuity, engine_state, kv_budget, metrics
from .config import settings
from .kv_budget import _s_float, _s_int

log = logging.getLogger(__name__)

T = TypeVar("T")

NORMAL = "normal"
LONG = "long"
LONG_OUTPUT = "long_output"
LANES = (NORMAL, LONG, LONG_OUTPUT)

#: Origin classes (module docstring, PRIORITY). Anything else folds to chat.
ORIGIN_CHAT = "chat"
ORIGIN_V1 = "v1"
ORIGINS = (ORIGIN_CHAT, ORIGIN_V1)

#: The exact sentence for a large document waiting for an idle engine
#: (CONTRACT §6.7), its sibling for an ordinary request waiting for a
#: slot, and the one for a long answer waiting for engine room. `{n}` is how
#: many requests are ahead in the lane.
LONG_LINE = "Waiting for the model to finish current work before your large document ({n} ahead)."
NORMAL_LINE = "Waiting for a free slot on the main model ({n} ahead)."
LONG_OUTPUT_LINE = "Waiting for room on the main model for a long answer ({n} ahead)."

#: How often a waiter re-reads the lane and the engine sample while it
#: waits. In-memory; a grant wakes it earlier.
_POLL_S = 1.0

#: How many /v1 NORMAL waiters one grant looks at for one that fits KV. The
#: pump runs on every release; a bounded scan keeps it O(1) whatever the depth
#: of the line (ADMISSION_MAX_WAITING is 400 per class) — the event loop is
#: the orchestrator's CPU bound (fast-mode pre-pass finding, 2026-09-05).
_V1_KV_SCAN = 32

#: The LONG closure's prefill grace (module docstring, THE CLOSURE IS
#: BOUNDED). Sized from the A/B run of 2026-09-12 (docs/availability/ab/
#: B-20260912T0859Z-SUMMARY.md): a 128k prompt prefilled at ~5,200 tok/s
#: (~25 s) and the 950k needle at ~1,190 tok/s (~800 s). A fixed 30-60 s
#: grace would reopen NORMAL half-way through any prefill above ~250k tokens
#: and rebuild the very mix the closure prevents; charging every token at
#: 1,000 tok/s — slower than the slowest measured — covers the 950k prefill
#: with ~30 % to spare. The ceiling is the most any closure, and any NORMAL
#: waiter's deferral, can last. Module constants rather than settings only
#: because config.py is held by another workstream in this programme.
_CLOSURE_FLOOR_S = 60.0
_CLOSURE_PREFILL_TOKENS_PER_S = 1000.0
LONG_CLOSURE_MAX_S = 1200.0

#: A KV charge older than this is logged once (module docstring, KV BUDGET):
#: a 1M-token answer at the slowest measured decode (~71 tok/s at c=10) is
#: ~3.9 h, so an entry past 4 h is more likely a leaked ticket than a job.
KV_CHARGE_WARN_AGE_S = 14_400.0

#: Retry-After for a NORMAL refusal, as publicapi has always sent it.
_DEFAULT_RETRY_AFTER_S = 5.0


# ---------------------------------------------------------------------------
# Settings of this build (config.py is another workstream's file: read through
# kv_budget's helpers, exactly config.py's parsing, at call time)
# ---------------------------------------------------------------------------


def long_output_max_seqs() -> int:
    # 2: decode-only partners add no prefill steps of their own, and candidate
    # B's mixed-load proof stops at c=10 — small until the long-output soak
    # passes. 0 closes the lane (module docstring, THE LONG-OUTPUT LANE).
    return max(0, _s_int("admission_long_output_max_seqs", "ADMISSION_LONG_OUTPUT_MAX_SEQS", 2))


def v1_long_output_threshold_tokens() -> int:
    return _s_int("admission_v1_long_output_threshold_tokens", "ADMISSION_V1_LONG_OUTPUT_THRESHOLD_TOKENS", 8192)


def chat_long_output_threshold_tokens() -> int:
    return _s_int("admission_chat_long_output_threshold_tokens", "ADMISSION_CHAT_LONG_OUTPUT_THRESHOLD_TOKENS", 65536)


def long_output_wait_s() -> float:
    return _s_float("admission_long_output_wait_s", "ADMISSION_LONG_OUTPUT_WAIT_S", 600.0)


def long_output_retry_after_s() -> float:
    return _s_float("admission_long_output_retry_after_s", "ADMISSION_LONG_OUTPUT_RETRY_AFTER_S", 60.0)


def chat_weight() -> int:
    return max(0, _s_int("admission_chat_weight", "ADMISSION_CHAT_WEIGHT", 3))


def chat_reserved_normal_slots() -> int:
    # 2 (adversarial review 2026-09-13): 400 /v1 answers of 4-8K tokens at
    # t=0 and chat at 0.1/s on the stub gave chat wait p95 76.3 s with 1
    # reserved seat, 26.1 s with 2, 2.3 s with 3 (the grant-count rule); the
    # seat rule's own figures are in the build's bench report. Each reserved
    # seat costs /v1 one stream while chat is silent: 9 → 8 streams is 279 →
    # 272 tok/s on the stub curve (−2.5 %).
    return _s_int("admission_chat_reserved_normal_slots", "ADMISSION_CHAT_RESERVED_NORMAL_SLOTS", 2)


def v1_kv_promote_s() -> float:
    # 300 s (module docstring, ONE ARBITER): half a streaming /v1 long answer's
    # 600 s bound, so a /v1 request that chat is keeping out of KV still gets in
    # before it is refused; a chat document's own wait is 600 s too.
    return max(0.0, _s_float("admission_v1_kv_promote_s", "ADMISSION_V1_KV_PROMOTE_S", 300.0))


def v1_long_closure_duty() -> float:
    # 0.10 of the window (module docstring, THE /v1 CLOSURE BUDGET): a chat turn
    # that arrives during a closure waits up to the closure's length, so the
    # duty is the share of chat turns a /v1 document loop can make wait.
    return min(1.0, max(0.0, _s_float("admission_v1_long_closure_duty", "ADMISSION_V1_LONG_CLOSURE_DUTY", 0.10)))


def v1_long_closure_window_s() -> float:
    # 7200 s: long enough that the run-alone exception keeps a 950K prompt
    # (~800 s closure) at 800 / (7200 + 800) = 10 %. 0 turns the budget off.
    return max(0.0, _s_float("admission_v1_long_closure_window_s", "ADMISSION_V1_LONG_CLOSURE_WINDOW_S", 7200.0))


# A malformed value fails at import — at boot, like config.py — not per request.
for _check in (long_output_max_seqs, v1_long_output_threshold_tokens, chat_long_output_threshold_tokens,
               long_output_wait_s, long_output_retry_after_s, chat_weight, chat_reserved_normal_slots,
               v1_kv_promote_s, v1_long_closure_duty, v1_long_closure_window_s):
    _check()
del _check


# ---------------------------------------------------------------------------
# Origin and the per-request wait bound (ContextVars: a task sees the copy it
# was created with, so publicapi sets them INSIDE its producer task)
# ---------------------------------------------------------------------------

_origin: ContextVar[str] = ContextVar("admission_origin", default=ORIGIN_CHAT)
_wait_bound: ContextVar[Optional[float]] = ContextVar("admission_wait_bound_s", default=None)


def _fold(value: object) -> str:
    return ORIGIN_V1 if value == ORIGIN_V1 else ORIGIN_CHAT


def set_origin(o: str) -> Token:
    return _origin.set(_fold(o))


def current_origin() -> str:
    return _fold(_origin.get())


@contextlib.contextmanager
def origin(o: str) -> Iterator[None]:
    token = set_origin(o)
    try:
        yield
    finally:
        _origin.reset(token)


def set_wait_bound_s(seconds: Optional[float]) -> Token:
    """Bound this context's LONG_OUTPUT wait (None: the lane's own bound). The
    bound is a hard wall-clock one: time behind a LONG closure is not deferred."""
    return _wait_bound.set(None if seconds is None else max(0.0, float(seconds)))


def closure_grace_s(tokens: int) -> float:
    """How long a LONG request may keep NORMAL closed without a first token."""
    grace = _CLOSURE_FLOOR_S + max(0, int(tokens)) / _CLOSURE_PREFILL_TOKENS_PER_S
    return min(float(LONG_CLOSURE_MAX_S), grace)


def _retry_after_for(lane: str) -> float:
    # LONG and LONG_OUTPUT waits are bounded at 600 s and blocked by work that
    # runs for minutes to hours: a 5 s retry only hammers the lane.
    return _DEFAULT_RETRY_AFTER_S if lane == NORMAL else float(long_output_retry_after_s())


class AdmissionRejected(RuntimeError):
    """The lane refused the request: `reason` is `timeout` (the bounded wait
    ran out) or `capacity` (the line was already too deep to join).
    `retry_after_s` is what a caller should wait before trying again."""

    def __init__(self, lane: str, reason: str, waited_s: float, retry_after_s: float = _DEFAULT_RETRY_AFTER_S) -> None:
        self.lane = lane
        self.reason = reason
        self.waited_s = waited_s
        self.retry_after_s = float(retry_after_s)
        super().__init__(f"{lane} lane refused the request: {reason} after {waited_s:.0f}s")


def _reject(lane: str, reason: str, waited_s: float) -> AdmissionRejected:
    metrics.inc("llm_admission_rejections_total", "Requests the admission lanes refused, by reason.", reason=reason)
    return AdmissionRejected(lane, reason, waited_s, retry_after_s=_retry_after_for(lane))


# ---------------------------------------------------------------------------
# Waiters
# ---------------------------------------------------------------------------


class _Waiter:
    __slots__ = ("fut", "origin", "charge", "enqueued_at", "lane", "key", "tokens")

    def __init__(self, fut: asyncio.Future, origin_: str, charge: int, lane: str, key: object = None,
                 tokens: int = 0) -> None:
        self.fut = fut
        self.origin = origin_
        self.charge = int(charge)
        self.enqueued_at = time.monotonic()
        self.lane = lane
        self.key = key
        self.tokens = int(tokens)


def _other(cls: str) -> str:
    return ORIGIN_CHAT if cls == ORIGIN_V1 else ORIGIN_V1


class _ClassQueue:
    """One FIFO deque per origin class, and NORMAL's chat streak (module
    docstring, PRIORITY)."""

    def __init__(self) -> None:
        self.q: Dict[str, Deque[_Waiter]] = {ORIGIN_CHAT: deque(), ORIGIN_V1: deque()}
        self.chat_streak = 0

    def add(self, w: _Waiter) -> None:
        self.q[w.origin].append(w)

    def remove(self, w: _Waiter) -> bool:
        try:
            self.q[w.origin].remove(w)
        except ValueError:
            return False
        return True

    def head(self, cls: str) -> Optional[_Waiter]:
        dq = self.q[cls]
        return dq[0] if dq else None

    def depth(self, cls: str, lane: Optional[str] = None) -> int:
        if lane is None:
            return len(self.q[cls])
        return sum(1 for w in self.q[cls] if w.lane == lane)

    def total(self, lane: Optional[str] = None) -> int:
        return self.depth(ORIGIN_CHAT, lane) + self.depth(ORIGIN_V1, lane)

    def position(self, w: _Waiter, lane: Optional[str] = None) -> int:
        """How many waiters are ahead of `w`: same-class waiters in front of
        it, plus every chat waiter for a v1 waiter (chat goes first)."""
        ahead = 0
        for other in self.q[w.origin]:
            if other is w:
                break
            if lane is None or other.lane == lane:
                ahead += 1
        if w.origin == ORIGIN_V1:
            ahead += self.depth(ORIGIN_CHAT, lane)
        return ahead


def _grant_metric(cls: str, contended: bool) -> None:
    metrics.inc("llm_admission_grants_total",
                "Admission grants, by origin; contended = the other origin class was waiting at the grant.",
                origin=cls, contended="yes" if contended else "no")


async def _await_grant(
    w: _Waiter,
    *,
    lane_name: str,
    closed: Callable[[], bool],
    timeout: float,
    on_wait: Optional[Callable[[int], Awaitable[None]]],
    ahead: int,
    pump: Callable[[], None],
    leave: Callable[[_Waiter], None],
    give_back: Callable[[], None],
    max_deferral_s: Optional[float] = None,
) -> float:
    """Wait for `w`'s future to be granted, up to `timeout` seconds of wait
    that was the caller's own (time behind a LONG closure is deferred, for at
    most `max_deferral_s`, LONG_CLOSURE_MAX_S unless the caller set its own
    bound). `on_wait` is called once, with `ahead`, the first time the caller
    actually has to wait. A waiter that leaves — timeout, cancellation, a
    failing `on_wait` — is taken out of its deque; one whose grant landed in
    the same tick gives the seat straight back."""
    # Read at call time, never bound as a default: the ceiling is a module
    # constant tests (and an operator's patch) may change.
    ceiling = float(LONG_CLOSURE_MAX_S if max_deferral_s is None else max_deferral_s)
    started = w.enqueued_at
    told = False
    last = started
    deferred = 0.0
    try:
        while not w.fut.done():
            if not told and on_wait is not None:
                told = True
                await on_wait(ahead)
                continue
            now = time.monotonic()
            if closed():
                # A LONG request holds the lane: that time is its wait, not
                # this caller's — the bound is deferred. For at most
                # LONG_CLOSURE_MAX_S in total: an unbounded deferral let a
                # waiter behind a closure that never lifted wait forever
                # (F044, 2026-09-13).
                step = min(now - last, max(0.0, ceiling - deferred))
                deferred += step
                started += step
            last = now
            remaining = timeout - (now - started)
            if remaining <= 0:
                raise _reject(lane_name, "timeout", now - started)
            await asyncio.wait({w.fut}, timeout=min(_POLL_S, remaining))
            if not w.fut.done():
                # Every tick: time-based conditions (a /v1 promotion, the /v1
                # closure window sliding) and the engine sample (the reserve
                # guard) change without an event of ours.
                pump()
    except BaseException:
        if w.fut.done() and not w.fut.cancelled():
            give_back()
        else:
            leave(w)
        raise
    return time.monotonic() - started


# ---------------------------------------------------------------------------
# The lanes
# ---------------------------------------------------------------------------


class Lane:
    """One lane: a capacity, the requests in it (by origin), the requests
    waiting for it, and a `closed` flag the LONG lane raises over the NORMAL
    and LONG_OUTPUT lanes. The LONG and LONG_OUTPUT lanes' waiters live in the
    KV arbiter's deques (seat and KV are one wait); their `waiting` counts
    them."""

    def __init__(
        self,
        name: str,
        capacity: Callable[[], int],
        *,
        owner: "Optional[Lanes]" = None,
        reserved: Optional[Callable[[], int]] = None,
    ) -> None:
        self.name = name
        self._capacity = capacity
        self._reserved = reserved
        self._owner = owner
        self.active = 0
        self.active_by_origin: Dict[str, int] = {ORIGIN_CHAT: 0, ORIGIN_V1: 0}
        self.closed = False
        #: LONG_OUTPUT only: tickets past their first chunk.
        self.decoding = 0
        self.queue = _ClassQueue()

    @property
    def capacity(self) -> int:
        # LONG_OUTPUT honours 0 (the kill switch); the others keep one seat.
        floor = 0 if self.name == LONG_OUTPUT else 1
        return max(floor, int(self._capacity()))

    def _arbitrated(self) -> bool:
        return self.name in (LONG, LONG_OUTPUT) and self._owner is not None

    @property
    def waiting(self) -> int:
        if self._arbitrated():
            assert self._owner is not None
            return self.queue.total() + self._owner.kv.queue.total(self.name)
        return self.queue.total()

    def waiting_by_origin(self, cls: str) -> int:
        if self._arbitrated():
            assert self._owner is not None
            return self.queue.depth(cls) + self._owner.kv.queue.depth(cls, self.name)
        return self.queue.depth(cls)

    def reserved_for_chat(self) -> int:
        """Seats /v1 may never hold, clamped to [0, capacity − 1]: /v1 always
        keeps at least one."""
        if self._reserved is None:
            return 0
        return min(max(0, int(self._reserved())), max(0, self.capacity - 1))

    def v1_share(self) -> int:
        """The seats /v1 may hold and still be offered a contended grant, and
        may take whatever is reserved (module docstring, PRIORITY): its share
        of the unreserved seats, (capacity − reserved) // (weight + 1), at
        least 1 — never the reserved seats themselves, even at weight 0."""
        return max(1, (self.capacity - self.reserved_for_chat()) // (chat_weight() + 1))

    def free(self) -> bool:
        return not self.closed and self.active < self.capacity

    def seat_free(self, cls: str) -> bool:
        """A /v1 request takes a seat only if the reserved seats stay FREE
        after it — whoever holds the rest — unless it holds fewer than its
        seat share, which is never reserved away (module docstring, PRIORITY)."""
        if not self.free():
            return False
        if cls == ORIGIN_V1 and self.active_by_origin[ORIGIN_V1] >= self.v1_share():
            if self.active + self.reserved_for_chat() >= self.capacity:
                return False
        return True

    def _publish(self) -> None:
        metrics.set_gauge("llm_admission_lane_active", float(self.active),
                          "Generations admitted to the engine, by lane.", lane=self.name)
        metrics.set_gauge("llm_admission_waiting", float(self.waiting),
                          "Generations waiting for a lane.", lane=self.name)
        if self._owner is not None:
            self._owner.publish_origins()

    def take(self, cls: str) -> None:
        self.active += 1
        self.active_by_origin[cls] = self.active_by_origin.get(cls, 0) + 1

    def _v1_candidate(self) -> Optional[_Waiter]:
        """The first /v1 waiter whose KV charge fits without delaying a waiter
        ahead of it (the arbiter's heads, and the /v1 head of this lane)."""
        dq = self.queue.q[ORIGIN_V1]
        if not dq:
            return None
        if self._owner is None:
            return dq[0]
        protected: List[_Waiter] = []
        for i, w in enumerate(dq):
            if i >= _V1_KV_SCAN:
                break
            if self._owner.normal_kv_ok(w, protected):
                return w
            if i == 0 and w.charge > 0:
                protected.append(w)
        return None

    def _pump(self) -> None:
        """Grant free seats to waiters, synchronously (module docstring,
        PRIORITY). No await: called from releases in `finally` blocks and
        from garbage collection."""
        q = self.queue
        granted = False
        while q.total():
            if not q.q[ORIGIN_V1]:
                q.chat_streak = 0
            chat_w = q.head(ORIGIN_CHAT) if self.free() else None
            v1_w = self._v1_candidate() if self.seat_free(ORIGIN_V1) else None
            if chat_w is None and v1_w is None:
                break
            if chat_w is not None and v1_w is not None:
                below_share = self.active_by_origin[ORIGIN_V1] < self.v1_share()
                if below_share and q.chat_streak >= chat_weight():
                    w = v1_w
                else:
                    w = chat_w
                    # Only a grant /v1 could have taken counts toward its turn.
                    if below_share:
                        q.chat_streak += 1
            else:
                w = chat_w if chat_w is not None else v1_w
            assert w is not None
            q.remove(w)
            if w.fut.done():  # cannot happen (waiters leave synchronously); never grant twice
                continue
            self.take(w.origin)
            if w.key is not None and w.charge > 0 and self._owner is not None:
                self._owner.ledger.commit(w.key, w.charge, self.name, w.origin, kv_budget.NORMAL_V1,
                                          at=time.monotonic())
            w.fut.set_result(None)
            if w.origin == ORIGIN_V1:
                q.chat_streak = 0
            _grant_metric(w.origin, bool(q.q[_other(w.origin)]))
            granted = True
        if granted:
            self._publish()

    def _leave(self, w: _Waiter) -> None:
        # A waiter that leaves (timeout, cancel) is not a grant: it moves no
        # streak (adversarial review 2026-09-13: chat timeouts handed /v1 turns).
        self.queue.remove(w)
        self._pump_all()
        self._publish()

    def _pump_all(self) -> None:
        if self._owner is not None:
            self._owner.pump()
        else:
            self._pump()

    def _tick(self) -> None:
        if self._owner is not None:
            self._owner.tick()
        else:
            self._pump()

    async def acquire(
        self,
        *,
        origin: str = ORIGIN_CHAT,
        timeout: float,
        on_wait: Optional[Callable[[int], Awaitable[None]]],
        charge: int = 0,
        key: object = None,
    ) -> float:
        """Take one seat, waiting up to `timeout`. Returns the seconds
        waited; raises AdmissionRejected on capacity or timeout. `on_wait`
        is called once, with how many are ahead, the first time the caller
        actually has to wait. A /v1 NORMAL request passes its projected KV
        `charge` and a ledger `key`: the grant commits it."""
        cls = _fold(origin)
        w = _Waiter(asyncio.get_running_loop().create_future(), cls, charge, self.name, key)
        self.queue.add(w)
        self._pump_all()
        if w.fut.done():
            self._publish()
            return 0.0
        # The depth bound is per class: a flood of one class never refuses
        # the other at the door.
        if self.queue.depth(cls) - 1 >= max(0, int(settings.admission_max_waiting)):
            self.queue.remove(w)
            self._publish()
            raise _reject(self.name, "capacity", 0.0)
        # How many are ahead: the requests in this lane and those waiting in
        # front — and, behind a closure, the one LONG request that holds it.
        ahead = self.active + self.queue.position(w) + (1 if self.closed else 0)
        self._publish()

        def give_back() -> None:
            if key is not None and self._owner is not None:
                self._owner.ledger.release(key)
            self.release_nowait(cls)

        return await _await_grant(
            w, lane_name=self.name, closed=lambda: self.closed, timeout=timeout, on_wait=on_wait,
            ahead=ahead, pump=self._tick, leave=self._leave, give_back=give_back,
        )

    def release_nowait(self, origin: str = ORIGIN_CHAT, *, pump: bool = True) -> None:
        cls = _fold(origin)
        self.active = max(0, self.active - 1)
        self.active_by_origin[cls] = max(0, self.active_by_origin.get(cls, 0) - 1)
        if pump:
            self._pump_all()
        self._publish()

    async def release(self, origin: str = ORIGIN_CHAT) -> None:
        self.release_nowait(origin)

    def set_closed_nowait(self, closed: bool, *, pump: bool = True) -> None:
        self.closed = closed
        if pump:
            self._pump_all()

    async def set_closed(self, closed: bool) -> None:
        self.set_closed_nowait(closed)


class _KvArbiter:
    """One grant order for everything that commits long KV: a LONG request
    gets its LONG seat and its KV in one grant, a LONG_OUTPUT request its seat
    and its KV in one grant (module docstring, ONE ARBITER)."""

    def __init__(self, ls: "Lanes") -> None:
        self.ls = ls
        self.queue = _ClassQueue()

    def head(self, cls: str) -> Optional[_Waiter]:
        return self.queue.head(cls)

    def _gate_blocked(self, w: _Waiter) -> bool:
        """Blocked by something other than KV: a seat, a closure, a LONG idle
        wait (LONG_OUTPUT), the /v1 closure budget (LONG)."""
        ls = self.ls
        if w.lane == LONG_OUTPUT:
            lo = ls.long_output
            return lo.closed or lo.active >= lo.capacity or ls.long_idle_waiting > 0
        lg = ls.long
        if lg.active >= lg.capacity:
            return True
        return w.origin == ORIGIN_V1 and not ls.v1_closure_allows(w.tokens)

    def budget(self) -> int:
        return kv_budget.budget_tokens(kv_budget.cached())

    def _promoted(self, h: _Waiter, budget: int, limit: int) -> bool:
        """A /v1 head that has waited ADMISSION_V1_KV_PROMOTE_S and that chat
        charges are what keep out goes ahead of later chat waiters."""
        if h.origin != ORIGIN_V1 or time.monotonic() - h.enqueued_at < v1_kv_promote_s():
            return False
        if self._gate_blocked(h):
            return False
        led = self.ls.ledger
        long_rest = led.committed - led.by_origin(ORIGIN_CHAT, kv_budget.LONG_WORK)
        managed_rest = led.managed - led.by_origin(ORIGIN_CHAT)
        return ((long_rest == 0 or long_rest + h.charge <= budget)
                and (managed_rest == 0 or managed_rest + h.charge <= limit))

    def _choose(self) -> Optional[_Waiter]:
        if kv_budget.reserve_exhausted(engine_state.engine_load()):
            return None
        led = self.ls.ledger
        pool = kv_budget.cached()
        budget = kv_budget.budget_tokens(pool)
        limit = kv_budget.managed_limit_tokens(pool)
        chat = list(self.queue.q[ORIGIN_CHAT])
        v1 = list(self.queue.q[ORIGIN_V1])
        chat_head = chat[0] if chat else None
        v1_head = v1[0] if v1 else None
        if v1_head is not None and self._promoted(v1_head, budget, limit):
            order = ([w for w in chat if w.enqueued_at < v1_head.enqueued_at] + v1
                     + [w for w in chat if w.enqueued_at >= v1_head.enqueued_at])
        else:
            order = chat + v1
        protected: List[Tuple[_Waiter, int, int]] = []
        for w in order:
            if (not self._gate_blocked(w)
                    and led.fits(w.charge, budget) and led.fits_managed(w.charge, limit)
                    and all(since_long + w.charge + h.charge <= budget and since_all + w.charge + h.charge <= limit
                            for h, since_long, since_all in protected)):
                return w
            if w is chat_head or w is v1_head:
                # It keeps its place in KV: what is admitted past it from here
                # on may never push its wait further (kv_budget, BACKFILL).
                protected.append((w, led.since(w.enqueued_at, kv_budget.LONG_WORK), led.since(w.enqueued_at)))
        return None

    def _pump(self) -> None:
        q = self.queue
        granted = False
        while q.total():
            w = self._choose()
            if w is None:
                break
            q.remove(w)
            if w.fut.done():
                continue
            self.ls.ledger.commit(w.key, w.charge, w.lane, w.origin, kv_budget.LONG_WORK, at=time.monotonic())
            self.ls.get(w.lane).take(w.origin)
            w.fut.set_result(None)
            _grant_metric(w.origin, bool(q.q[_other(w.origin)]))
            granted = True
        if granted:
            self._publish()

    def _publish(self) -> None:
        self.ls.long._publish()
        self.ls.long_output._publish()
        self.ls.publish_kv()

    def _leave(self, w: _Waiter) -> None:
        self.queue.remove(w)
        self.ls.pump()
        self._publish()

    def _give_back(self, w: _Waiter) -> None:
        self.ls.ledger.release(w.key)
        self.ls.get(w.lane).release_nowait(w.origin, pump=False)
        self.ls.pump()
        self._publish()

    async def acquire(
        self,
        *,
        lane: str,
        origin: str,
        charge: int,
        key: object,
        timeout: float,
        on_wait: Optional[Callable[[int], Awaitable[None]]],
        tokens: int = 0,
        max_deferral_s: Optional[float] = None,
    ) -> float:
        cls = _fold(origin)
        if lane == LONG_OUTPUT and self.ls.long_output.capacity == 0:
            raise _reject(lane, "capacity", 0.0)
        w = _Waiter(asyncio.get_running_loop().create_future(), cls, charge, lane, key, tokens)
        self.queue.add(w)
        self.ls.pump()
        if w.fut.done():
            self._publish()
            return 0.0
        if self.queue.depth(cls, lane) - 1 >= max(0, int(settings.admission_max_waiting)):
            self.queue.remove(w)
            self._publish()
            raise _reject(lane, "capacity", 0.0)
        if lane == LONG_OUTPUT:
            lo = self.ls.long_output
            ahead = lo.active + self.queue.position(w, LONG_OUTPUT) + (1 if lo.closed else 0)
            closed: Callable[[], bool] = lambda: self.ls.long_output.closed  # noqa: E731
        else:
            ahead = self.ls.long.active + self.queue.position(w, LONG)
            closed = lambda: False  # noqa: E731
        self._publish()
        return await _await_grant(
            w, lane_name=lane, closed=closed, timeout=timeout, on_wait=on_wait, ahead=ahead,
            pump=self.ls.pump, leave=self._leave, give_back=lambda: self._give_back(w),
            max_deferral_s=max_deferral_s,
        )


class Lanes:
    def __init__(self) -> None:
        self.ledger = kv_budget.Ledger()
        self.normal = Lane(NORMAL, lambda: settings.admission_normal_max, owner=self,
                           reserved=chat_reserved_normal_slots)
        self.long = Lane(LONG, lambda: settings.admission_long_max, owner=self)
        self.long_output = Lane(LONG_OUTPUT, long_output_max_seqs, owner=self)
        self.kv = _KvArbiter(self)
        #: LONG requests between their grant and the end of their idle wait:
        #: LONG_OUTPUT admissions are held meanwhile (LONG BESIDE LONG ANSWERS).
        self.long_idle_waiting = 0
        #: [start, end or None] of /v1-origin LONG closures (THE /v1 CLOSURE BUDGET).
        self.v1_closures: Deque[List[Optional[float]]] = deque()

    def get(self, name: str) -> Lane:
        if name == LONG:
            return self.long
        if name == LONG_OUTPUT:
            return self.long_output
        return self.normal

    def pump(self) -> None:
        """Called on release, on reopen after a closure, when a waiter leaves,
        when the ledger changes, and on each long waiter's poll tick. Long work
        first: a /v1 NORMAL grant must never take KV a long head could have
        had now."""
        self.kv._pump()
        self.normal._pump()
        self.long._pump()

    def tick(self) -> None:
        """A NORMAL waiter's poll tick: NORMAL grants follow events (a release,
        a reopen); only the arbiter has conditions that move with time."""
        if self.kv.queue.total():
            self.pump()

    def normal_kv_ok(self, w: _Waiter, protected: Sequence[_Waiter] = ()) -> bool:
        """Does a /v1 NORMAL waiter's charge fit the managed limit without
        delaying the arbiter's heads or `protected` (kv_budget, BACKFILL)?"""
        if w.charge <= 0:
            return True
        led = self.ledger
        limit = kv_budget.managed_limit_tokens(kv_budget.cached())
        if not led.fits_managed(w.charge, limit):
            return False
        for h in (self.kv.head(ORIGIN_CHAT), self.kv.head(ORIGIN_V1), *protected):
            if h is None or h is w or h.charge <= 0:
                continue
            if led.since(h.enqueued_at) + w.charge + h.charge > limit:
                return False
        return True

    def v1_closure_used_s(self, now: Optional[float] = None) -> float:
        at = time.monotonic() if now is None else now
        window = v1_long_closure_window_s()
        start = at - window
        while self.v1_closures and self.v1_closures[0][1] is not None and self.v1_closures[0][1] < start:
            self.v1_closures.popleft()
        used = 0.0
        for began, ended in self.v1_closures:
            assert began is not None
            used += max(0.0, (at if ended is None else ended) - max(began, start))
        return used

    def v1_closure_allows(self, tokens: int, now: Optional[float] = None) -> bool:
        window = v1_long_closure_window_s()
        if window <= 0:
            return True
        used = self.v1_closure_used_s(now)
        # A prompt too big for the budget runs alone rather than never.
        return used <= 0.0 or used + closure_grace_s(tokens) <= v1_long_closure_duty() * window

    def publish_origins(self) -> None:
        for cls in ORIGINS:
            waiting = self.normal.queue.depth(cls) + self.long.queue.depth(cls) + self.kv.queue.depth(cls)
            metrics.set_gauge("llm_admission_waiting_by_origin", float(waiting),
                              "Generations waiting for any admission lane or KV grant, by origin class.",
                              origin=cls)

    def publish_kv(self, now: Optional[float] = None) -> None:
        p = kv_budget.cached()
        age = self.ledger.oldest_age_s(now)
        metrics.set_gauge("llm_admission_kv_pool_tokens", float(p.tokens),
                          "The engine KV pool the admission budget is computed from, in tokens.")
        metrics.set_gauge("llm_admission_kv_budget_tokens", float(kv_budget.budget_tokens(p)),
                          "KV tokens long work may commit: the pool less the reserve.")
        metrics.set_gauge("llm_admission_kv_committed_tokens", float(self.ledger.committed),
                          "Projected KV tokens committed by admitted LONG and LONG_OUTPUT requests.")
        metrics.set_gauge("llm_admission_kv_normal_committed_tokens", float(self.ledger.normal_committed),
                          "Projected KV tokens committed by admitted /v1 NORMAL requests.")
        metrics.set_gauge("llm_admission_kv_managed_limit_tokens", float(kv_budget.managed_limit_tokens(p)),
                          "KV tokens long work and /v1 NORMAL together may commit: the pool less the headroom.")
        metrics.set_gauge("llm_admission_kv_oldest_charge_age_seconds", float(age),
                          "Age of the oldest KV charge; a charge older than a 1M-token answer is a leak.")
        metrics.set_gauge("llm_admission_kv_pool_live", 1.0 if p.source == "live" else 0.0,
                          "1 when the KV pool was read from the engine, 0 when it comes from the settings.")
        metrics.set_gauge("llm_admission_long_output_decoding", float(self.long_output.decoding),
                          "LONG_OUTPUT requests past their first token (decode-only).")
        metrics.set_gauge("llm_admission_v1_long_closure_seconds", float(self.v1_closure_used_s(now)),
                          "Seconds /v1 LONG requests kept the lanes closed in the budget window.")
        for entry in self.ledger.stale_entries(KV_CHARGE_WARN_AGE_S, now):
            log.warning("admission: a %s KV charge of %d tokens (%s) has been held for more than %.0f s; "
                        "a leaked ticket pins the budget until the process restarts",
                        entry.lane, entry.charge, entry.origin, KV_CHARGE_WARN_AGE_S)


_by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Lanes]" = weakref.WeakKeyDictionary()


def lanes() -> Lanes:
    loop = asyncio.get_running_loop()
    found = _by_loop.get(loop)
    if found is None:
        found = _by_loop[loop] = Lanes()
    return found


# ---------------------------------------------------------------------------
# Choosing the lane
# ---------------------------------------------------------------------------


async def prompt_tokens(messages: Sequence[dict], *, base_url: str, model: str) -> int:
    """The prompt's size for the lane decision (module docstring, SIZING)."""
    measured = context.measured_prompt_tokens(messages, base_url)
    if measured is not None:
        return int(measured)
    threshold = max(1, int(settings.admission_long_threshold_tokens))
    estimate = context.estimate_messages(messages)
    # "Certainly small" only from a bound the text cannot game (N013); an
    # over-estimate may skip the round trip, it only errs toward LONG.
    if context.upper_bound_messages(messages) < threshold // 2 or estimate > threshold * 2:
        return estimate
    exact, _window = await context.count_tokens(base_url, model, messages)
    return int(exact)


def lane_for(tokens: int, max_tokens: Optional[int] = None, origin: str = ORIGIN_CHAT) -> str:
    """LONG by prompt; else LONG_OUTPUT by the origin's output threshold; else
    NORMAL (module docstring, THE LONG-OUTPUT LANE)."""
    if tokens > max(1, int(settings.admission_long_threshold_tokens)):
        return LONG
    if max_tokens is not None:
        if _fold(origin) == ORIGIN_V1:
            threshold = v1_long_output_threshold_tokens()
        else:
            threshold = chat_long_output_threshold_tokens()
        if int(max_tokens) > int(threshold):
            return LONG_OUTPUT
    return NORMAL


# ---------------------------------------------------------------------------
# Running a call through its lane
# ---------------------------------------------------------------------------


async def _say(line: str) -> None:
    """One status line: through the chat turn's hold when there is one
    (which also parks the row), else the wait notifier."""
    hold = continuity.current()
    if hold is not None:
        await hold.enter(continuity.ADMISSION, line)
        return
    from . import resilience  # lazy: resilience imports continuity, not this module

    await resilience.notify(line)


def _ahead(ls: "Lanes") -> Optional[int]:
    """How much work is in front of a long request: the engine's own
    `requests_running` from the controller's sample — every sequence,
    LONG_OUTPUT decoders included (a decode is half of the GDN fault shape;
    module docstring, LONG BESIDE LONG ANSWERS) — or, with no sample, this
    process's NORMAL and LONG_OUTPUT occupancy. None when idle by that
    measure."""
    idle_max = max(0, int(settings.admission_long_idle_max))
    sample = engine_state.engine_load()
    if sample is not None:
        running = int(sample["requests_running"])
    else:
        running = int(ls.normal.active) + int(ls.long_output.active)
    return running if running > idle_max else None


async def _wait_for_idle(deadline: float, ls: "Lanes") -> bool:
    """Wait until the engine is idle — the controller's engine sample says
    `requests_running` ≤ ADMISSION_LONG_IDLE_MAX, or, with no sample (an
    unknown controller), this process's own lanes are that empty — or
    `deadline` passes. The NORMAL lane is NOT closed meanwhile (module
    docstring); the log says once when the sample is unknown."""
    said_unknown = False
    while True:
        if engine_state.engine_load() is None and not said_unknown:
            said_unknown = True
            log.info("admission: controller engine sample unknown; the long request waits for this "
                     "process's own lane to be idle")
        if _ahead(ls) is None:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(_POLL_S, remaining))


class _Ticket:
    """What one admitted call holds, and how it lets go. Release is
    synchronous and idempotent (module docstring, KV BUDGET)."""

    __slots__ = ("lane", "lanes", "origin", "long_holds_normal", "released", "waited_s", "_closure_timer",
                 "kv_key", "decoding", "loop", "_closure_interval")

    def __init__(self, lane: str, lanes_: Lanes, origin_: str = ORIGIN_CHAT) -> None:
        self.lane = lane
        self.lanes = lanes_
        self.origin = _fold(origin_)
        self.long_holds_normal = False
        self.released = False
        self.waited_s = 0.0
        self._closure_timer: Optional[asyncio.Task] = None
        self.kv_key: object = None
        self.decoding = False
        #: The loop the lanes belong to: the garbage-collection backstop must
        #: release on this loop's thread, never on the collector's.
        try:
            self.loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None
        self._closure_interval: Optional[List[Optional[float]]] = None

    def close_lanes(self) -> None:
        """ADMITTED: nothing new into NORMAL or LONG_OUTPUT until the first
        token; a /v1 closure is timed against its budget."""
        ls = self.lanes
        ls.normal.set_closed_nowait(True, pump=False)
        ls.long_output.set_closed_nowait(True, pump=False)
        self.long_holds_normal = True
        if self.origin == ORIGIN_V1:
            interval: List[Optional[float]] = [time.monotonic(), None]
            ls.v1_closures.append(interval)
            self._closure_interval = interval

    def bound_closure(self, seconds: float) -> None:
        """Reopen the lanes after `seconds` if no first token has (F044)."""
        if not self.long_holds_normal or self._closure_timer is not None:
            return

        async def expire() -> None:
            await asyncio.sleep(max(0.0, seconds))
            if not self.long_holds_normal:
                return
            metrics.inc("llm_admission_closure_expired_total",
                        "LONG requests whose prefill grace ran out before a first token reopened the NORMAL lane.")
            log.warning("admission: long request held the normal lane %.0fs without a first token; reopening it",
                        seconds)
            self._reopen_closure()

        self._closure_timer = asyncio.get_running_loop().create_task(expire())

    def _cancel_timer(self) -> None:
        timer = self._closure_timer
        if timer is None or timer.done():
            return
        try:
            current = asyncio.current_task()
        except RuntimeError:  # no running loop (garbage collection)
            current = None
        if timer is not current:
            with contextlib.suppress(Exception):
                timer.cancel()

    def _reopen_closure(self, *, pump: bool = True) -> None:
        if self.long_holds_normal:
            self.long_holds_normal = False
            interval = self._closure_interval
            if interval is not None and interval[1] is None:
                interval[1] = time.monotonic()
            self.lanes.normal.set_closed_nowait(False, pump=False)
            self.lanes.long_output.set_closed_nowait(False, pump=False)
            if pump:
                self.lanes.pump()

    def first_token_nowait(self) -> None:
        """The first chunk: a LONG request's large prefill is done (the lanes
        may take new work again); a LONG_OUTPUT request is decode-only now."""
        self._cancel_timer()
        if self.lane == LONG_OUTPUT and not self.decoding and not self.released:
            self.decoding = True
            self.lanes.long_output.decoding += 1
            self.lanes.publish_kv()
        self._reopen_closure()

    async def first_token(self) -> None:
        self.first_token_nowait()

    def release_nowait(self) -> None:
        if self.released:
            return
        self.released = True
        self._cancel_timer()
        ls = self.lanes
        if self.decoding:
            self.decoding = False
            ls.long_output.decoding = max(0, ls.long_output.decoding - 1)
        self._reopen_closure(pump=False)
        if self.kv_key is not None:
            ls.ledger.release(self.kv_key)
            self.kv_key = None
        ls.get(self.lane).release_nowait(self.origin, pump=False)
        ls.pump()
        ls.publish_kv()

    async def release(self) -> None:
        self.release_nowait()


def _charge_pool_needed(ls: Lanes) -> bool:
    """WHY NOT ALWAYS READ THE POOL: an empty ledger with nobody waiting admits
    any charge (`Ledger.fits`), so a read could not change the decision. Only
    a request that lands beside committed or waiting work reads it —
    fewer GETs against the engine than one per long request, the same grants."""
    return ls.ledger.managed > 0 or ls.kv.queue.total() > 0


#: Pool reads in flight, held so the loop cannot drop them half-way.
_refreshes: "set[asyncio.Future]" = set()


def _charge(ls: Lanes, tokens: int, max_tokens: Optional[int], base_url: str) -> int:
    """The projected charge, from the pool as last read — synchronously, so a
    request takes its place in the KV order the moment it arrives (a 2 s
    /metrics read before joining let a later request in ahead of it). When the
    decision can depend on the pool, a read refreshes it in the background:
    the arbiter re-reads `kv_budget.cached()` at every grant and tick, and the
    settings it starts from are the 2026-09-13 live values."""
    if _charge_pool_needed(ls):
        refresh = asyncio.ensure_future(kv_budget.pool(base_url))
        _refreshes.add(refresh)
        refresh.add_done_callback(_refreshes.discard)
    p = kv_budget.cached()
    window = getattr(settings, "model_max_context", None)
    return kv_budget.charge_tokens(tokens, int(max_tokens or 0), block_size=p.block_size, window=window)


async def _admit(
    lane: str,
    on_wait,
    *,
    origin_: str = ORIGIN_CHAT,
    tokens: int = 0,
    max_tokens: Optional[int] = None,
    base_url: str = "",
) -> _Ticket:
    ls = lanes()
    ticket = _Ticket(lane, ls, origin_)
    if lane == NORMAL:
        charge, key = 0, None
        if _fold(origin_) == ORIGIN_V1:
            # The NORMAL path never reads the pool: the cached block size is
            # exact while the engine's config cannot change (zero downtime).
            p = kv_budget.cached()
            charge = kv_budget.charge_tokens(tokens, int(max_tokens or 0), block_size=p.block_size,
                                             window=getattr(settings, "model_max_context", None))
            key = object()
        ticket.waited_s = await ls.normal.acquire(origin=origin_, timeout=float(settings.admission_normal_wait_s),
                                                  on_wait=on_wait, charge=charge, key=key)
        ticket.kv_key = key
        return ticket
    if lane == LONG_OUTPUT:
        charge = _charge(ls, tokens, max_tokens, base_url)
        bound = _wait_bound.get()
        key = object()
        # Seat and KV in one wait; a refusal holds nothing (a same-tick grant
        # was given back inside the wait). A caller's own bound is a wall
        # clock: publicapi's 30 s synchronous bound must not become 1,040 s
        # behind a 950K document's closure (adversarial review 2026-09-13).
        ticket.waited_s = await ls.kv.acquire(
            lane=LONG_OUTPUT, origin=origin_, charge=charge, key=key, tokens=tokens,
            timeout=float(long_output_wait_s() if bound is None else bound), on_wait=on_wait,
            max_deferral_s=None if bound is None else 0.0,
        )
        ticket.kv_key = key
        return ticket
    budget = float(settings.admission_long_wait_s)
    started = time.monotonic()
    told = False

    async def tell(ahead: int) -> None:
        # Once for the whole admission — the seat, the KV grant and the idle
        # wait are one wait to the person.
        nonlocal told
        if told or on_wait is None:
            return
        told = True
        await on_wait(ahead)

    # Seat and KV in one grant (module docstring, ONE ARBITER): a document
    # waiting for the seat is already in the KV order.
    charge = _charge(ls, tokens, max_tokens, base_url)
    key = object()
    ticket.waited_s = await ls.kv.acquire(lane=LONG, origin=origin_, charge=charge, key=key, tokens=tokens,
                                          timeout=budget, on_wait=tell)
    ticket.kv_key = key
    try:
        deadline = started + budget
        # Wait for the engine to be idle — the NORMAL lane keeps admitting
        # meanwhile (module docstring); new LONG_OUTPUT admissions do not.
        ls.long_idle_waiting += 1
        try:
            ahead = _ahead(ls)
            if ahead is not None:
                await tell(ahead)
            idle = await _wait_for_idle(deadline, ls)
        finally:
            ls.long_idle_waiting = max(0, ls.long_idle_waiting - 1)
        if not idle:
            waited = time.monotonic() - started
            if ls.long_output.active > 0:
                # Our own long answers decode for hours: proceeding would make
                # the fault shape routine (LONG BESIDE LONG ANSWERS).
                log.warning("admission: engine not idle after %.0fs with %d long answer(s) on it; "
                            "long request refused", budget, ls.long_output.active)
                raise _reject(LONG, "timeout", waited)
            if _fold(origin_) == ORIGIN_V1 and ls.normal.queue.depth(ORIGIN_CHAT) > 0:
                log.warning("admission: engine not idle after %.0fs and a chat turn waits for NORMAL; "
                            "/v1 long request refused", budget)
                raise _reject(LONG, "timeout", waited)
            # The bound is "at most": the engine never went idle (the other
            # tenant, most likely). The closed lane below is the protection
            # the orchestrator can give; the request goes in and the log says.
            log.warning("admission: engine not idle after %.0fs; long request proceeds", budget)
        # ADMITTED: from here to the first token nothing new is admitted to
        # NORMAL or LONG_OUTPUT, so no other prefill of ours mixes with it.
        ticket.close_lanes()
        ticket.waited_s = time.monotonic() - started
        return ticket
    except BaseException:
        ticket.release_nowait()
        raise


class _LaneStream:
    """A stream that releases its lane when it ends and marks its first chunk
    (a LONG request's prefill is over; a LONG_OUTPUT request is decoding).
    Forwards iteration and close() to the wrapped stream; sits INSIDE the
    breaker's GuardedStream, which sees it as the stream."""

    __slots__ = ("_stream", "_ticket", "_iter", "_first", "_exhausted")

    def __init__(self, stream, ticket: _Ticket) -> None:
        self._stream = stream
        self._ticket = ticket
        self._iter = None
        self._first = True
        self._exhausted = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._iter is None:
            self._iter = self._stream.__aiter__()
        try:
            chunk = await self._iter.__anext__()
        except StopAsyncIteration:
            self._exhausted = True
            self._ticket.release_nowait()
            raise
        except BaseException:
            self._ticket.release_nowait()
            raise
        if self._first:
            self._first = False
            self._ticket.first_token_nowait()
        return chunk

    async def close(self) -> None:
        self._ticket.release_nowait()
        if self._exhausted:
            return
        closer = getattr(self._stream, "close", None) or getattr(self._stream, "aclose", None)
        if closer is not None:
            await closer()

    async def aclose(self) -> None:
        await self.close()

    def __del__(self) -> None:
        # The backstop for a consumer that dropped the stream without closing
        # it: the seat and, above all, a KV charge of up to 60 % of the budget
        # would otherwise be held until the process restarts.
        ticket = getattr(self, "_ticket", None)
        if ticket is None or ticket.released:
            return
        try:
            log.warning("admission: a %s stream was dropped without close(); released at garbage collection",
                        ticket.lane)
            loop = ticket.loop
            if loop is not None and loop.is_closed():
                # Its lanes died with the loop: nothing waits on them, and no
                # thread may safely touch them.
                return
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if loop is None or running is loop:
                ticket.release_nowait()
            else:
                # The cyclic collector runs on whichever thread allocates —
                # a db.run_in_thread worker included — and futures, deques and
                # the ledger are loop-thread state (adversarial review
                # 2026-09-13: Lane._pump ran on a worker thread).
                loop.call_soon_threadsafe(ticket.release_nowait)
        except Exception:  # noqa: BLE001 — a finalizer must never raise (a closed loop, interpreter exit)
            pass


async def run(
    op: Callable[[], Awaitable[T]],
    *,
    messages: Sequence[dict],
    base_url: str,
    model: str,
    stream: bool = False,
    max_tokens: Optional[int] = None,
) -> T:
    """Run one main-model call through its lane.

    `op` opens the call (the resilient wrapper's retry loop calls this per
    attempt, AFTER the breaker admitted it — a request queued for a
    recovering engine must not hold a lane slot for the whole reload). A
    non-streaming call holds its slot until it returns; a streaming call
    returns a stream that holds the slot until it ends and, for the LONG
    lane, keeps the NORMAL and LONG_OUTPUT lanes closed until its first chunk.
    Either way the closure lasts at most `closure_grace_s(tokens)` (module
    docstring). `max_tokens` is the planned output: above the origin's
    threshold the call takes the LONG_OUTPUT lane; None means NORMAL by output.
    """
    tokens = await prompt_tokens(messages, base_url=base_url, model=model)
    origin_ = current_origin()
    lane = lane_for(tokens, max_tokens, origin_)
    line = LONG_LINE if lane == LONG else LONG_OUTPUT_LINE if lane == LONG_OUTPUT else NORMAL_LINE

    async def on_wait(ahead: int) -> None:
        await _say(line.format(n=ahead))

    ticket = await _admit(lane, on_wait, origin_=origin_, tokens=tokens, max_tokens=max_tokens, base_url=base_url)
    # Everything between the grant and the stream's hand-off releases the
    # ticket if it raises: `hold.resume()` raises LeaseLost on purpose and
    # awaits the database, where a Stop can cancel it — a ticket lost there
    # pinned its seat, a KV charge with no expiry and a LONG closure
    # (adversarial review 2026-09-13).
    try:
        ticket.bound_closure(closure_grace_s(tokens))
        if ticket.waited_s > 0:
            metrics.observe("llm_admission_wait_seconds", ticket.waited_s,
                            "Seconds a request waited for its admission lane.", lane=lane)
            hold = continuity.current()
            if hold is not None and hold.waiting and hold.kind == continuity.ADMISSION:
                await hold.resume()
        result = await op()
    except BaseException:
        ticket.release_nowait()
        raise
    if stream:
        return _LaneStream(result, ticket)  # type: ignore[return-value]
    ticket.release_nowait()
    return result


def describe() -> dict:
    """What /health shows: lane occupancy for the current loop (the app's
    one loop in production); the configured limits either way. /health is
    polled every 15-30 s, so it also refreshes the KV age gauge and the
    stale-charge warning."""
    out: Dict[str, dict] = {}
    try:
        ls: Optional[Lanes] = lanes()
    except RuntimeError:
        ls = None
    for name in (NORMAL, LONG):
        lane = ls.get(name) if ls is not None else None
        out[name] = {
            "capacity": max(1, int(settings.admission_normal_max if name == NORMAL else settings.admission_long_max)),
            "active": lane.active if lane else 0,
            "waiting": lane.waiting if lane else 0,
            "closed": bool(lane.closed) if lane else False,
        }
    lo = ls.long_output if ls is not None else None
    out[LONG_OUTPUT] = {
        "capacity": long_output_max_seqs(),
        "active": lo.active if lo else 0,
        "waiting": lo.waiting if lo else 0,
        "closed": bool(lo.closed) if lo else False,
        "decoding": lo.decoding if lo else 0,
    }
    p = kv_budget.cached()
    out["kv"] = {
        "pool_tokens": p.tokens,
        "block_size": p.block_size,
        "source": p.source,
        "budget_tokens": kv_budget.budget_tokens(p),
        "committed_tokens": ls.ledger.committed if ls else 0,
        "reserve_fraction": kv_budget.reserve_fraction(),
        "oldest_charge_age_s": round(ls.ledger.oldest_age_s(), 1) if ls else 0.0,
        "normal_v1_committed_tokens": ls.ledger.normal_committed if ls else 0,
        "managed_limit_tokens": kv_budget.managed_limit_tokens(p),
        "v1_long_closure_s_in_window": round(ls.v1_closure_used_s(), 1) if ls else 0.0,
    }
    out["origin_waiting"] = {
        cls: (ls.normal.queue.depth(cls) + ls.long.queue.depth(cls) + ls.kv.queue.depth(cls)) if ls else 0
        for cls in ORIGINS
    }
    if ls is not None:
        ls.publish_kv()
    out["long_threshold_tokens"] = int(settings.admission_long_threshold_tokens)  # type: ignore[assignment]
    return out


def reset() -> None:
    """Tests only."""
    _by_loop.clear()
    kv_budget.reset()
