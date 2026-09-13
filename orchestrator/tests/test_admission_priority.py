"""app/admission.py — chat first, /v1 never starved (PRIORITY, 2026-09-13).

Offline: fake engine calls that hold until released, no network, no engine
sample. What is pinned: a waiting chat turn is served before an earlier /v1
waiter; after ADMISSION_CHAT_WEIGHT chat grants /v1 could have taken, the
oldest /v1 waiter is served; /v1 at its seat share gets no freed seat while
chat waits (seats, not grants — adversarial review 2026-09-13); a chat turn
that times out, or one that takes a seat /v1 could not, moves no /v1 turn;
the reserved seats stay FREE while /v1 is above its share, whoever holds the
rest, and are not left idle when only chat waits; the depth bound is per
origin class; a waiter cancelled in the tick its grant landed gives the seat
back; the origin is task-local; with chat-only traffic the "ahead" count is
the old one.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import List

import pytest

from app import admission, engine_state, kv_budget, metrics, resilience
from app.config import settings

MAIN_URL = "http://vllm-main.test:8000/v1"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    metrics.reset()
    admission.reset()
    engine_state.reset()
    monkeypatch.setattr(settings, "admission_long_threshold_tokens", 131_072)
    monkeypatch.setattr(settings, "admission_normal_max", 2)
    monkeypatch.setattr(settings, "admission_long_max", 1)
    monkeypatch.setattr(settings, "admission_long_idle_max", 0)
    monkeypatch.setattr(settings, "admission_long_wait_s", 5.0)
    monkeypatch.setattr(settings, "admission_normal_wait_s", 5.0)
    monkeypatch.setattr(settings, "admission_max_waiting", 100)
    monkeypatch.setattr(settings, "admission_chat_weight", 3, raising=False)
    monkeypatch.setattr(settings, "admission_chat_reserved_normal_slots", 0, raising=False)
    monkeypatch.setattr(admission, "_POLL_S", 0.02)

    async def no_network(url):
        raise AssertionError(f"unexpected network read of {url}")

    monkeypatch.setattr(kv_budget, "_fetch_text", no_network)
    yield
    admission.reset()
    engine_state.reset()
    metrics.reset()


def _counter(name: str, **labels) -> float:
    key = tuple(sorted(labels.items()))
    return metrics._counters.get(name, {}).get(key, 0.0)


class _Call:
    def __init__(self, name: str = "", log: "List[str] | None" = None) -> None:
        self.name = name
        self.log = log
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self):
        if self.log is not None:
            self.log.append(self.name)
        self.entered.set()
        # Lets go by itself after 5 s: no test may hang, even against a mutant.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.release.wait(), 5.0)
        return "answer"


async def _run(call, *, origin=admission.ORIGIN_CHAT, chars: int = 10, max_tokens=None):
    with admission.origin(origin):
        return await admission.run(call, messages=[{"role": "user", "content": "x" * chars}],
                                   base_url=MAIN_URL, model="m", max_tokens=max_tokens)


def _start(call, **kw) -> asyncio.Task:
    # A task copies the context at creation: the origin is set inside _run.
    return asyncio.ensure_future(_run(call, **kw))


async def _settle(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def test_a_waiting_chat_turn_is_admitted_before_an_earlier_waiting_v1_request():
    async def run():
        order: List[str] = []
        holders = [_Call("h1", order), _Call("h2", order)]
        tasks = [_start(c) for c in holders]
        for c in holders:
            await asyncio.wait_for(c.entered.wait(), 1.0)
        v1 = _Call("v1", order)
        tv = _start(v1, origin=admission.ORIGIN_V1)
        await asyncio.sleep(0.05)  # the /v1 request is waiting FIRST
        chat = _Call("chat", order)
        tc = _start(chat)
        await asyncio.sleep(0.05)
        holders[0].release.set()
        await asyncio.wait_for(chat.entered.wait(), 1.0)
        assert not v1.entered.is_set(), "the chat turn took the freed seat, the /v1 request waits on"
        holders[1].release.set()
        await asyncio.wait_for(v1.entered.wait(), 1.0)
        chat.release.set()
        v1.release.set()
        await asyncio.gather(*tasks, tv, tc)
        assert order == ["h1", "h2", "chat", "v1"]
        assert _counter("llm_admission_grants_total", origin="chat", contended="yes") == 1

    asyncio.run(run())


def test_after_three_chat_grants_the_oldest_v1_waiter_is_served_next(monkeypatch):
    monkeypatch.setattr(settings, "admission_normal_max", 1)

    async def run():
        lane = admission.lanes().normal
        await lane.acquire(origin=admission.ORIGIN_CHAT, timeout=5, on_wait=None)  # the holder
        granted: List[str] = []

        async def wait(name, cls):
            await lane.acquire(origin=cls, timeout=5, on_wait=None)
            granted.append(name)

        tasks = [asyncio.ensure_future(wait("v1-a", admission.ORIGIN_V1)),
                 asyncio.ensure_future(wait("v1-b", admission.ORIGIN_V1))]
        await _settle()
        tasks += [asyncio.ensure_future(wait(f"chat-{i}", admission.ORIGIN_CHAT)) for i in range(5)]
        await _settle()
        # Each release hands the one seat to the next waiter in the weighted order.
        for _ in range(7):
            lane.release_nowait(granted[-1].split("-")[0] if granted else admission.ORIGIN_CHAT)
            await _settle()
        lane.release_nowait(admission.ORIGIN_CHAT)
        await asyncio.gather(*tasks)
        assert granted == ["chat-0", "chat-1", "chat-2", "v1-a", "chat-3", "chat-4", "v1-b"]

    asyncio.run(run())


def test_v1_is_never_starved_by_a_continuous_stream_of_chat_turns(monkeypatch):
    monkeypatch.setattr(settings, "admission_normal_max", 1)

    async def run():
        lane = admission.lanes().normal
        await lane.acquire(origin=admission.ORIGIN_CHAT, timeout=5, on_wait=None)
        granted: List[str] = []

        async def wait(name, cls):
            await lane.acquire(origin=cls, timeout=30, on_wait=None)
            granted.append(name)

        v1 = asyncio.ensure_future(wait("v1", admission.ORIGIN_V1))
        await _settle()
        chats = []
        for i in range(40):
            # Chat keeps arriving: there are always two chat turns waiting.
            while sum(1 for t in chats if not t.done()) < 2:
                chats.append(asyncio.ensure_future(wait(f"chat-{len(chats)}", admission.ORIGIN_CHAT)))
                await _settle()
            lane.release_nowait(admission.ORIGIN_CHAT)
            await _settle()
            if v1.done():
                break
        assert v1.done(), "the /v1 request was starved"
        assert granted.index("v1") <= 3, granted
        for t in chats:
            t.cancel()
        await asyncio.gather(*chats, return_exceptions=True)

    asyncio.run(run())


def test_v1_never_holds_the_last_normal_slot_reserved_for_chat(monkeypatch):
    """Headroom, not a /v1 cap (adversarial review 2026-09-13): above its seat
    share a /v1 request takes a seat only if the reserved one stays free after
    it — so a seat /v1 frees while chat holds the reserved one stays free."""
    monkeypatch.setattr(settings, "admission_normal_max", 4)
    monkeypatch.setattr(settings, "admission_chat_reserved_normal_slots", 1, raising=False)

    async def run():
        lane = admission.lanes().normal
        assert lane.v1_share() == 1  # 4 // (3 + 1)
        v1_calls = [_Call(f"v1-{i}") for i in range(4)]
        v1_tasks = [_start(c, origin=admission.ORIGIN_V1) for c in v1_calls]
        await asyncio.sleep(0.1)
        assert [c.entered.is_set() for c in v1_calls] == [True, True, True, False]
        assert lane.active == 3 and lane.active_by_origin[admission.ORIGIN_V1] == 3
        # The reserved seat is free, and a chat turn takes it at once.
        chat = _Call("chat")
        started = time.monotonic()
        tc = _start(chat)
        await asyncio.wait_for(chat.entered.wait(), 1.0)
        assert time.monotonic() - started < 0.2
        # A /v1 answer ends while chat holds the reserved seat: the freed seat
        # is now the one free seat, and the waiting /v1 request may not take it.
        v1_calls[0].release.set()
        await asyncio.sleep(0.1)
        assert not v1_calls[3].entered.is_set(), "the freed seat stays free for chat"
        assert lane.active == 3
        second = _Call("chat-2")
        started = time.monotonic()
        t2 = _start(second)
        await asyncio.wait_for(second.entered.wait(), 1.0)
        assert time.monotonic() - started < 0.2, "the second chat turn found it free"
        # Two free seats: now /v1 may take one and leave one.
        second.release.set()
        v1_calls[1].release.set()
        await asyncio.wait_for(v1_calls[3].entered.wait(), 1.0)
        for c in (chat, *v1_calls):
            c.release.set()
        await asyncio.gather(tc, t2, *v1_tasks)
        # The clamp: with one seat, /v1 keeps it — a reserve never shuts /v1 out.
        monkeypatch.setattr(settings, "admission_normal_max", 1)
        assert lane.reserved_for_chat() == 0

    asyncio.run(run())


def test_while_chat_waits_v1_at_its_seat_share_gets_no_freed_seat(monkeypatch):
    """Seats, not grants (adversarial review 2026-09-13): a /v1 answer holds its
    seat far longer than a chat turn, so '1 grant in 4' gave /v1 ~70 % of seat
    time. /v1 holding its share (10 // 4 = 2) or more gets no contended grant,
    and the grants chat takes meanwhile do not count toward /v1's turn."""
    monkeypatch.setattr(settings, "admission_normal_max", 10)

    async def run():
        lane = admission.lanes().normal
        for _ in range(4):
            await lane.acquire(origin=admission.ORIGIN_V1, timeout=5, on_wait=None)
        for _ in range(6):
            await lane.acquire(origin=admission.ORIGIN_CHAT, timeout=5, on_wait=None)
        granted: List[str] = []

        async def wait(name, cls):
            await lane.acquire(origin=cls, timeout=5, on_wait=None)
            granted.append(name)

        tasks = [asyncio.ensure_future(wait(f"v1-{i}", admission.ORIGIN_V1)) for i in range(3)]
        await _settle()
        tasks += [asyncio.ensure_future(wait(f"chat-{i}", admission.ORIGIN_CHAT)) for i in range(5)]
        await _settle()
        seen = []
        for origin in (admission.ORIGIN_V1,) * 4 + (admission.ORIGIN_CHAT,) * 2:
            lane.release_nowait(origin)
            await _settle()
            seen.append((lane.active_by_origin[admission.ORIGIN_V1], list(granted)))
        # Four /v1 seats end: /v1 at 3 and 2 is at its share (no turn counted),
        # at 1 and 0 below it (two turns counted); one more chat grant makes
        # three, and the next freed seat is /v1's.
        assert granted == ["chat-0", "chat-1", "chat-2", "chat-3", "chat-4", "v1-0"], seen
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


def test_a_chat_turn_that_leaves_by_timeout_moves_no_v1_turn(monkeypatch):
    monkeypatch.setattr(settings, "admission_normal_max", 1)

    async def run():
        lane = admission.lanes().normal
        await lane.acquire(origin=admission.ORIGIN_CHAT, timeout=5, on_wait=None)
        v1 = asyncio.ensure_future(lane.acquire(origin=admission.ORIGIN_V1, timeout=5, on_wait=None))
        await _settle()
        # Three chat turns give up while /v1 waits (the build counted each as a
        # chat resolution and handed /v1 the next seat).
        for _ in range(3):
            with pytest.raises(admission.AdmissionRejected):
                await lane.acquire(origin=admission.ORIGIN_CHAT, timeout=0.05, on_wait=None)
        assert lane.queue.chat_streak == 0
        chat = asyncio.ensure_future(lane.acquire(origin=admission.ORIGIN_CHAT, timeout=5, on_wait=None))
        await _settle()
        lane.release_nowait(admission.ORIGIN_CHAT)
        await _settle()
        assert chat.done() and not v1.done(), "the waiting chat turn goes first"
        lane.release_nowait(admission.ORIGIN_CHAT)
        await asyncio.wait_for(v1, 1.0)

    asyncio.run(run())


def test_a_chat_grant_that_v1_could_not_have_taken_moves_no_v1_turn(monkeypatch):
    monkeypatch.setattr(settings, "admission_normal_max", 2)
    monkeypatch.setattr(settings, "admission_chat_reserved_normal_slots", 1, raising=False)
    monkeypatch.setattr(settings, "admission_chat_weight", 1, raising=False)

    async def run():
        lane = admission.lanes().normal
        assert lane.v1_share() == 1
        await lane.acquire(origin=admission.ORIGIN_V1, timeout=5, on_wait=None)
        await lane.acquire(origin=admission.ORIGIN_CHAT, timeout=5, on_wait=None)
        granted: List[str] = []

        async def wait(name, cls):
            await lane.acquire(origin=cls, timeout=5, on_wait=None)
            granted.append(name)

        tasks = [asyncio.ensure_future(wait("v1", admission.ORIGIN_V1))]
        await _settle()
        tasks += [asyncio.ensure_future(wait(f"chat-{i}", admission.ORIGIN_CHAT)) for i in range(3)]
        await _settle()
        # /v1 holds its share and the one free seat is reserved: chat-0 takes a
        # seat /v1 could not have — no turn for /v1.
        lane.release_nowait(admission.ORIGIN_CHAT)
        await _settle()
        # /v1 below its share now: chat-1 is the one grant weight 1 allows.
        lane.release_nowait(admission.ORIGIN_V1)
        await _settle()
        lane.release_nowait(admission.ORIGIN_CHAT)
        await _settle()
        lane.release_nowait(admission.ORIGIN_CHAT)
        await _settle()
        assert granted == ["chat-0", "chat-1", "v1", "chat-2"]
        await asyncio.gather(*tasks)

    asyncio.run(run())


def test_a_reserved_seat_is_not_left_idle_when_only_chat_waits(monkeypatch):
    monkeypatch.setattr(settings, "admission_normal_max", 2)
    monkeypatch.setattr(settings, "admission_chat_reserved_normal_slots", 1, raising=False)
    # Weight 0: /v1 is offered the seat first whenever both classes wait, so
    # the chat turn gets it ONLY through the try-the-other-class rule.
    monkeypatch.setattr(settings, "admission_chat_weight", 0, raising=False)

    async def run():
        lane = admission.lanes().normal
        await lane.acquire(origin=admission.ORIGIN_V1, timeout=5, on_wait=None)
        await lane.acquire(origin=admission.ORIGIN_CHAT, timeout=5, on_wait=None)
        v1_wait = asyncio.ensure_future(lane.acquire(origin=admission.ORIGIN_V1, timeout=5, on_wait=None))
        await _settle()
        chat_wait = asyncio.ensure_future(lane.acquire(origin=admission.ORIGIN_CHAT, timeout=5, on_wait=None))
        await _settle()
        lane.release_nowait(admission.ORIGIN_CHAT)
        await _settle()
        assert chat_wait.done(), "the /v1 head may not take the reserved seat, so the chat head gets it"
        assert not v1_wait.done()
        assert lane.active == 2
        lane.release_nowait(admission.ORIGIN_V1)
        await asyncio.wait_for(v1_wait, 1.0)

    asyncio.run(run())


def test_a_flood_of_v1_waiters_cannot_refuse_a_chat_turn_at_the_door(monkeypatch):
    monkeypatch.setattr(settings, "admission_max_waiting", 3)
    monkeypatch.setattr(settings, "admission_normal_max", 1)

    async def run():
        lane = admission.lanes().normal
        await lane.acquire(origin=admission.ORIGIN_CHAT, timeout=5, on_wait=None)
        flood = [asyncio.ensure_future(lane.acquire(origin=admission.ORIGIN_V1, timeout=5, on_wait=None))
                 for _ in range(3)]
        await _settle()
        with pytest.raises(admission.AdmissionRejected) as refused:
            await lane.acquire(origin=admission.ORIGIN_V1, timeout=5, on_wait=None)
        assert refused.value.reason == "capacity"
        chat = asyncio.ensure_future(lane.acquire(origin=admission.ORIGIN_CHAT, timeout=5, on_wait=None))
        await _settle()
        assert not chat.done(), "the chat turn waits in its own line instead of being refused"
        lane.release_nowait(admission.ORIGIN_CHAT)
        await asyncio.wait_for(chat, 1.0)
        for task in flood:
            task.cancel()
        await asyncio.gather(*flood, return_exceptions=True)
        assert _counter("llm_admission_rejections_total", reason="capacity") == 1

    asyncio.run(run())


def test_a_waiter_cancelled_as_it_is_granted_gives_the_slot_back(monkeypatch):
    monkeypatch.setattr(settings, "admission_normal_max", 1)

    async def run():
        lane = admission.lanes().normal
        await lane.acquire(origin=admission.ORIGIN_CHAT, timeout=5, on_wait=None)
        waiter = asyncio.ensure_future(lane.acquire(origin=admission.ORIGIN_CHAT, timeout=5, on_wait=None))
        await _settle()
        assert lane.waiting == 1
        # Same tick: the release grants the waiter's future, and the waiter's
        # task is cancelled before it ever runs again.
        lane.release_nowait(admission.ORIGIN_CHAT)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert lane.active == 0 and lane.waiting == 0, "the seat came back"
        started = time.monotonic()
        assert await lane.acquire(origin=admission.ORIGIN_CHAT, timeout=5, on_wait=None) == 0.0
        assert time.monotonic() - started < 0.05

    asyncio.run(run())


def test_origin_set_inside_a_task_does_not_leak_to_the_caller():
    async def run():
        seen = {}

        async def producer():
            admission.set_origin(admission.ORIGIN_V1)
            seen["inside"] = admission.current_origin()

        await asyncio.ensure_future(producer())
        seen["caller"] = admission.current_origin()
        with admission.origin("something-else"):
            seen["unknown"] = admission.current_origin()
        return seen

    assert asyncio.run(run()) == {"inside": "v1", "caller": "chat", "unknown": "chat"}


def test_chat_only_traffic_reports_the_same_ahead_count_as_before():
    """Before 2026-09-13: ahead = active + waiting + (1 if closed)."""
    said: List[str] = []

    async def notify(line):
        said.append(line)

    async def run():
        resilience.set_wait_notifier(notify)
        a, b, c, d = _Call(), _Call(), _Call(), _Call()
        ta, tb = _start(a), _start(b)
        await asyncio.wait_for(a.entered.wait(), 1.0)
        await asyncio.wait_for(b.entered.wait(), 1.0)
        tc = _start(c)
        await asyncio.sleep(0.05)
        td = _start(d)
        await asyncio.sleep(0.05)
        assert said == ["Waiting for a free slot on the main model (2 ahead).",
                        "Waiting for a free slot on the main model (3 ahead)."]
        for call in (a, b, c, d):
            call.release.set()
        await asyncio.gather(ta, tb, tc, td)
        # Behind a closure with nothing active: the one LONG request that holds it.
        await admission.lanes().normal.set_closed(True)
        e = _Call()
        te = _start(e)
        await asyncio.sleep(0.05)
        assert said[-1] == "Waiting for a free slot on the main model (1 ahead)."
        await admission.lanes().normal.set_closed(False)
        e.release.set()
        await te

    asyncio.run(run())


def test_a_v1_waiter_counts_the_chat_turns_that_go_before_it():
    said: List[str] = []

    async def notify(line):
        said.append(line)

    async def run():
        resilience.set_wait_notifier(notify)
        a, b = _Call(), _Call()
        ta, tb = _start(a), _start(b)
        await asyncio.wait_for(b.entered.wait(), 1.0)
        c = _Call()
        tc = _start(c)
        await asyncio.sleep(0.05)
        v = _Call()
        tv = _start(v, origin=admission.ORIGIN_V1)
        await asyncio.sleep(0.05)
        assert said[-1] == "Waiting for a free slot on the main model (3 ahead)."
        for call in (a, b, c, v):
            call.release.set()
        await asyncio.gather(ta, tb, tc, tv)
        assert admission.describe()["origin_waiting"] == {"chat": 0, "v1": 0}

    asyncio.run(run())
