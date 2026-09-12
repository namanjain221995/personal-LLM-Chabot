"""Long-context admission — two lanes in front of the main model (CONTRACT §6.7).

WHY THIS EXISTS. The fault that took the engine down on 2026-09-11 fired
inside vLLM's GDN prefill kernel under nine concurrent mixed
prefill+decode requests, and the second tenant reproduces that load shape
at will (docs/availability/INCIDENT-2026-09-11-vllm.md, hypothesis H). The
orchestrator cannot fix the kernel, but it can stop feeding it the worst
case: a very large prefill arriving while nine other requests are being
scheduled. So every orchestrator request passes two lanes before it
reaches vLLM:

    NORMAL   prompt ≤ ADMISSION_LONG_THRESHOLD_TOKENS: at most
             ADMISSION_NORMAL_MAX generations at once (a semaphore);
    LONG     above the threshold: ONE at a time. Before it starts, the
             NORMAL lane is closed and drained (nothing new is admitted,
             what is running finishes), the engine is asked — through the
             controller's engine sample, never /metrics itself — to report
             `requests_running` ≤ ADMISSION_LONG_IDLE_MAX, for at most
             ADMISSION_LONG_WAIT_S, and the NORMAL lane stays closed until
             the long request's FIRST TOKEN: no new prefill mixes with the
             large one. Then the lane reopens and the long generation
             decodes beside ordinary traffic like any other.

The second tenant's raw-port traffic is outside these lanes (it does not
pass through this process); that is documented, not solved, here.

WHAT THE PERSON SEES. A wait is durable and truthful: on a chat turn the
V29 row says `queued` (app/continuity.py) and one line is said,
LONG_LINE for a large document and NORMAL_LINE for a slot behind other
work, each with how many are ahead. A wait that outruns its bound is a
rejection with a reason the metrics name — `timeout` — and a line the
chat worker turns into the TIMEOUT sentence; a line that is already too
deep refuses newcomers at once — `capacity` — rather than promising a
wait it cannot keep.

SIZING. The lane is chosen from the prompt as it will be sent (the sized
messages `context.fit_request` returns). The count is `context`'s own
character estimate — no second /tokenize round trip for the ordinary
prompt, which was the CPU-bound pre-pass finding of 2026-09-05 — and the
exact count from /tokenize only for a prompt whose estimate is within a
factor of two of the threshold, where the estimate could pick the wrong
lane.

Per event loop, like the breaker registry: a lane is asyncio state and
the test suite runs a loop per test.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import weakref
from typing import Awaitable, Callable, Dict, Optional, Sequence, TypeVar

from . import context, continuity, engine_state, metrics
from .config import settings

log = logging.getLogger(__name__)

T = TypeVar("T")

NORMAL = "normal"
LONG = "long"
LANES = (NORMAL, LONG)

#: The exact sentence for a large document waiting for an idle engine
#: (CONTRACT §6.7), and its sibling for an ordinary request waiting for a
#: slot. `{n}` is how many requests are ahead in the lane.
LONG_LINE = "Waiting for the model to finish current work before your large document ({n} ahead)."
NORMAL_LINE = "Waiting for the model to finish current work ({n} ahead)."

#: How often a waiter re-reads the lane and the engine sample while it
#: waits. In-memory; the lane's condition wakes it earlier.
_POLL_S = 1.0


class AdmissionRejected(RuntimeError):
    """The lane refused the request: `reason` is `timeout` (the bounded wait
    ran out) or `capacity` (the line was already too deep to join)."""

    def __init__(self, lane: str, reason: str, waited_s: float) -> None:
        self.lane = lane
        self.reason = reason
        self.waited_s = waited_s
        super().__init__(f"{lane} lane refused the request: {reason} after {waited_s:.0f}s")


# ---------------------------------------------------------------------------
# The lanes
# ---------------------------------------------------------------------------


class Lane:
    """One lane: a capacity, the requests in it, the requests waiting for
    it, and a `closed` flag the LONG lane raises over the NORMAL one."""

    def __init__(self, name: str, capacity: Callable[[], int]) -> None:
        self.name = name
        self._capacity = capacity
        self.active = 0
        self.waiting = 0
        self.closed = False
        self.cond = asyncio.Condition()

    @property
    def capacity(self) -> int:
        return max(1, int(self._capacity()))

    def _publish(self) -> None:
        metrics.set_gauge("llm_admission_lane_active", float(self.active),
                          "Generations admitted to the engine, by lane.", lane=self.name)
        metrics.set_gauge("llm_admission_waiting", float(self.waiting),
                          "Generations waiting for a lane.", lane=self.name)

    def free(self) -> bool:
        return not self.closed and self.active < self.capacity

    async def acquire(self, *, timeout: float, on_wait: Optional[Callable[[int], Awaitable[None]]]) -> float:
        """Take one slot, waiting up to `timeout`. Returns the seconds
        waited; raises AdmissionRejected on capacity or timeout. `on_wait`
        is called once, with how many are ahead, the first time the caller
        actually has to wait."""
        started = time.monotonic()
        if self.free():
            self.active += 1
            self._publish()
            return 0.0
        if self.waiting >= max(0, int(settings.admission_max_waiting)):
            metrics.inc("llm_admission_rejections_total", "Requests the admission lanes refused, by reason.",
                        reason="capacity")
            raise AdmissionRejected(self.name, "capacity", 0.0)
        ahead = self.active + self.waiting
        self.waiting += 1
        self._publish()
        told = False
        try:
            async with self.cond:
                while not self.free():
                    if not told and on_wait is not None:
                        told = True
                        await on_wait(ahead)
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        metrics.inc("llm_admission_rejections_total",
                                    "Requests the admission lanes refused, by reason.", reason="timeout")
                        raise AdmissionRejected(self.name, "timeout", time.monotonic() - started)
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.cond.wait(), min(_POLL_S, remaining))
                self.active += 1
        finally:
            self.waiting -= 1
            self._publish()
        return time.monotonic() - started

    async def release(self) -> None:
        self.active = max(0, self.active - 1)
        self._publish()
        async with self.cond:
            self.cond.notify_all()

    async def set_closed(self, closed: bool) -> None:
        self.closed = closed
        async with self.cond:
            self.cond.notify_all()


class Lanes:
    def __init__(self) -> None:
        self.normal = Lane(NORMAL, lambda: settings.admission_normal_max)
        self.long = Lane(LONG, lambda: settings.admission_long_max)

    def get(self, name: str) -> Lane:
        return self.long if name == LONG else self.normal


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
    estimate = context.estimate_messages(messages)
    threshold = max(1, int(settings.admission_long_threshold_tokens))
    if estimate < threshold // 2 or estimate > threshold * 2:
        return estimate
    exact, _window = await context.count_tokens(base_url, model, messages)
    return int(exact)


def lane_for(tokens: int) -> str:
    return LONG if tokens > max(1, int(settings.admission_long_threshold_tokens)) else NORMAL


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


async def _wait_for_idle(deadline: float) -> bool:
    """Wait until the controller's engine sample says the engine is idle
    (`requests_running` ≤ ADMISSION_LONG_IDLE_MAX) or `deadline` passes.
    A controller that is unknown cannot be waited on — the drained NORMAL
    lane is then the whole protection, and the log says so once."""
    idle_max = max(0, int(settings.admission_long_idle_max))
    said_unknown = False
    while True:
        sample = engine_state.engine_load()
        if sample is None:
            if not said_unknown:
                said_unknown = True
                log.info("admission: controller engine sample unknown; long request proceeds after the lane drains")
            return True
        if sample["requests_running"] <= idle_max:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(_POLL_S, remaining))


class _Ticket:
    """What one admitted call holds, and how it lets go."""

    __slots__ = ("lane", "lanes", "long_holds_normal", "released", "waited_s")

    def __init__(self, lane: str, lanes_: Lanes) -> None:
        self.lane = lane
        self.lanes = lanes_
        self.long_holds_normal = False
        self.released = False
        self.waited_s = 0.0

    async def first_token(self) -> None:
        """A LONG request's first token: the large prefill is done, the
        NORMAL lane may take new work again."""
        if self.long_holds_normal:
            self.long_holds_normal = False
            await self.lanes.normal.set_closed(False)

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        await self.first_token()
        await self.lanes.get(self.lane).release()


async def _admit(lane: str, on_wait) -> _Ticket:
    ls = lanes()
    ticket = _Ticket(lane, ls)
    if lane == NORMAL:
        ticket.waited_s = await ls.normal.acquire(timeout=float(settings.admission_normal_wait_s), on_wait=on_wait)
        return ticket
    budget = float(settings.admission_long_wait_s)
    started = time.monotonic()
    told = False

    async def tell(ahead: int) -> None:
        # Once for the whole admission — the lane, the drain and the idle
        # wait are one wait to the person.
        nonlocal told
        if told or on_wait is None:
            return
        told = True
        await on_wait(ahead)

    ticket.waited_s = await ls.long.acquire(timeout=budget, on_wait=tell)
    try:
        # Close the NORMAL lane and let it drain; then wait for the engine
        # to be idle. Both against the one budget.
        await ls.normal.set_closed(True)
        ticket.long_holds_normal = True
        deadline = started + budget
        sample = engine_state.engine_load()
        busy = sample is not None and sample["requests_running"] > max(0, int(settings.admission_long_idle_max))
        if ls.normal.active > 0 or busy:
            await tell(ls.normal.active)
        async with ls.normal.cond:
            while ls.normal.active > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    metrics.inc("llm_admission_rejections_total",
                                "Requests the admission lanes refused, by reason.", reason="timeout")
                    raise AdmissionRejected(LONG, "timeout", time.monotonic() - started)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(ls.normal.cond.wait(), min(_POLL_S, remaining))
        if not await _wait_for_idle(deadline):
            # The bound is "at most": the engine never went idle (the other
            # tenant, most likely). The drained lane is the protection the
            # orchestrator can give; the request goes in and the log says.
            log.warning("admission: engine not idle after %.0fs; long request proceeds", budget)
        ticket.waited_s = time.monotonic() - started
        return ticket
    except BaseException:
        await ticket.release()
        raise


class _LaneStream:
    """A stream that releases its lane when it ends and reopens the NORMAL
    lane at its first chunk (a LONG request's prefill is over). Forwards
    iteration and close() to the wrapped stream; sits INSIDE the breaker's
    GuardedStream, which sees it as the stream."""

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
            await self._ticket.release()
            raise
        except BaseException:
            await self._ticket.release()
            raise
        if self._first:
            self._first = False
            await self._ticket.first_token()
        return chunk

    async def close(self) -> None:
        await self._ticket.release()
        if self._exhausted:
            return
        closer = getattr(self._stream, "close", None) or getattr(self._stream, "aclose", None)
        if closer is not None:
            await closer()

    async def aclose(self) -> None:
        await self.close()


async def run(
    op: Callable[[], Awaitable[T]],
    *,
    messages: Sequence[dict],
    base_url: str,
    model: str,
    stream: bool = False,
) -> T:
    """Run one main-model call through its lane.

    `op` opens the call (the resilient wrapper's retry loop calls this per
    attempt, AFTER the breaker admitted it — a request queued for a
    recovering engine must not hold a lane slot for the whole reload). A
    non-streaming call holds its slot until it returns; a streaming call
    returns a stream that holds the slot until it ends and, for the LONG
    lane, keeps the NORMAL lane closed until its first chunk.
    """
    tokens = await prompt_tokens(messages, base_url=base_url, model=model)
    lane = lane_for(tokens)
    line = LONG_LINE if lane == LONG else NORMAL_LINE

    async def on_wait(ahead: int) -> None:
        await _say(line.format(n=ahead))

    ticket = await _admit(lane, on_wait)
    if ticket.waited_s > 0:
        metrics.observe("llm_admission_wait_seconds", ticket.waited_s,
                        "Seconds a request waited for its admission lane.", lane=lane)
        hold = continuity.current()
        if hold is not None and hold.waiting and hold.kind == continuity.ADMISSION:
            await hold.resume()
    try:
        result = await op()
    except BaseException:
        await ticket.release()
        raise
    if stream:
        return _LaneStream(result, ticket)  # type: ignore[return-value]
    await ticket.release()
    return result


def describe() -> dict:
    """What /health shows: lane occupancy for the current loop (the app's
    one loop in production); the configured limits either way."""
    out: Dict[str, dict] = {}
    try:
        ls = lanes()
    except RuntimeError:
        ls = None
    for name in LANES:
        lane = ls.get(name) if ls is not None else None
        out[name] = {
            "capacity": max(1, int(settings.admission_normal_max if name == NORMAL else settings.admission_long_max)),
            "active": lane.active if lane else 0,
            "waiting": lane.waiting if lane else 0,
            "closed": bool(lane.closed) if lane else False,
        }
    out["long_threshold_tokens"] = int(settings.admission_long_threshold_tokens)  # type: ignore[assignment]
    return out


def reset() -> None:
    """Tests only."""
    _by_loop.clear()
