"""The per-engine capacity gates of `/v1` (capacity rule, 2026-09-13).

Not a usage limit: one gate per SHARED engine, shared by every caller,
first come first served, a bounded wait, and the retry-safe 503
`model_unavailable` "at capacity" when the wait runs out — never a 429 and
never a number attached to a caller. These tests pin the properties whose
failure would be invisible until a public burst slowed the chat app: the
count and the token budget hold, the line is FIFO, a slot is never stranded
by a cancellation, and public work yields to a person waiting for an answer.

No engine and no database: the gate is process state.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from app.config import settings
from app.publicapi import capacity, errors
from tests.publicapi_fake_engine import set_setting


@pytest.fixture(autouse=True)
def _fresh_gates(monkeypatch):
    capacity.reset_for_tests()
    monkeypatch.setattr(capacity, "YIELD_STEP_S", 0.01)
    yield
    capacity.reset_for_tests()


def _run(coro):
    return asyncio.run(coro)


def test_the_documented_defaults_are_the_argued_numbers():
    snap = capacity.snapshot()
    assert snap["main.long"]["max_concurrent"] == 1
    assert (snap["router"]["max_concurrent"], snap["router"]["budget_tokens"]) == (4, 24_576)
    assert snap["ocr"]["max_concurrent"] == 2 and snap["ocr"]["budget_tokens"] == 0
    assert (snap["embed"]["max_concurrent"], snap["embed"]["budget_tokens"]) == (2, 8192)
    assert (snap["rerank"]["max_concurrent"], snap["rerank"]["budget_tokens"]) == (2, 8192)
    assert snap["asr"]["max_concurrent"] == 1
    assert capacity.sync_wait_s() == 30.0
    assert capacity.background_wait_s() == 3600.0


def test_the_concurrency_cap_holds_and_the_next_caller_is_a_retryable_503_not_a_429():
    async def scenario():
        async with capacity.hold("ocr", wait_s=1), capacity.hold("ocr", wait_s=1):
            with pytest.raises(errors.ApiError) as refused:
                async with capacity.hold("ocr", wait_s=0.05):
                    pass
            return refused.value

    refusal = _run(scenario())
    assert refusal.status == 503 and refusal.code == "model_unavailable"
    assert refusal.headers()["Retry-After"] == "5"
    assert "capacity" in refusal.message


def test_a_waiter_is_admitted_the_moment_a_slot_is_given_back():
    async def scenario():
        order = []
        release = asyncio.Event()

        async def holder(name):
            async with capacity.hold("asr", wait_s=5):
                order.append(f"{name} in")
                await release.wait()
                order.append(f"{name} out")

        first = asyncio.ensure_future(holder("a"))
        await asyncio.sleep(0.01)
        second = asyncio.ensure_future(holder("b"))
        await asyncio.sleep(0.01)
        assert capacity.snapshot()["asr"]["waiting"] == 1
        release.set()
        await asyncio.gather(first, second)
        return order

    assert _run(scenario()) == ["a in", "a out", "b in", "b out"]


def test_the_token_budget_holds_while_a_request_heavier_than_it_still_runs_alone(monkeypatch):
    async def scenario():
        async with capacity.hold("router", weight_tokens=20_000, wait_s=1):
            # 20,000 + 8,000 > 24,576: refused although only 1 of 4 slots is used.
            with pytest.raises(errors.ApiError):
                async with capacity.hold("router", weight_tokens=8_000, wait_s=0.05):
                    pass
            async with capacity.hold("router", weight_tokens=4_000, wait_s=0.05):
                assert capacity.snapshot()["router"]["used_tokens"] == 24_000
        # Nothing held: a 100,000-token request is charged the whole budget and
        # admitted on its own rather than never.
        async with capacity.hold("router", weight_tokens=100_000, wait_s=0.05):
            with pytest.raises(errors.ApiError):
                async with capacity.hold("router", weight_tokens=1, wait_s=0.05):
                    pass
        return capacity.snapshot()["router"]

    after = _run(scenario())
    assert after["in_flight"] == 0 and after["used_tokens"] == 0


def test_the_line_is_first_come_first_served_so_a_heavy_request_is_not_starved():
    async def scenario():
        admitted = []
        release_first = asyncio.Event()

        async def run(name, weight, release=None):
            async with capacity.hold("embed", weight_tokens=weight, wait_s=5):
                admitted.append(name)
                if release is not None:
                    await release.wait()

        first = asyncio.ensure_future(run("small-1", 6000, release_first))
        await asyncio.sleep(0.01)
        heavy = asyncio.ensure_future(run("heavy", 8192))
        await asyncio.sleep(0.01)
        # Would fit beside small-1 (6000 + 1000 <= 8192) but must queue behind
        # the heavy request that arrived first.
        light = asyncio.ensure_future(run("light", 1000))
        await asyncio.sleep(0.05)
        assert admitted == ["small-1"]
        release_first.set()
        await asyncio.gather(first, heavy, light)
        return admitted

    assert _run(scenario()) == ["small-1", "heavy", "light"]


def test_a_cancelled_waiter_leaves_the_line_and_strands_nothing():
    async def scenario():
        async with capacity.hold("main.long", wait_s=1):
            waiter = asyncio.ensure_future(
                capacity.hold("main.long", wait_s=10).__aenter__()
            )
            await asyncio.sleep(0.02)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert capacity.snapshot()["main.long"]["waiting"] == 0
        async with capacity.hold("main.long", wait_s=0.05):
            return capacity.snapshot()["main.long"]["in_flight"]

    assert _run(scenario()) == 1


def test_a_holder_cancelled_mid_request_gives_its_slot_back():
    async def scenario():
        entered = asyncio.Event()

        async def holder():
            async with capacity.hold("asr", wait_s=1):
                entered.set()
                await asyncio.sleep(3600)

        task = asyncio.ensure_future(holder())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return capacity.snapshot()["asr"]["in_flight"]

    assert _run(scenario()) == 0


def test_an_abandoned_wait_raises_abandoned_rather_than_a_capacity_error():
    async def scenario():
        gone = asyncio.Event()
        async with capacity.hold("main.long", wait_s=1):
            async def cancel_soon():
                await asyncio.sleep(0.02)
                gone.set()

            asyncio.ensure_future(cancel_soon())
            with pytest.raises(capacity.Abandoned):
                async with capacity.hold("main.long", wait_s=10, abandon=gone):
                    pass
        return capacity.snapshot()["main.long"]

    snap = _run(scenario())
    assert snap["in_flight"] == 0 and snap["waiting"] == 0


def test_the_wait_bound_is_read_at_call_time_from_the_setting(monkeypatch):
    set_setting(monkeypatch, "PUBLIC_API_GATE_WAIT_S", "2.5")
    assert capacity.sync_wait_s() == 2.5
    monkeypatch.setattr(settings, "public_api_router_max_concurrent", 1, raising=False)
    assert capacity.snapshot()["router"]["max_concurrent"] == 1


def _install_video_probe(monkeypatch, busy):
    module = types.ModuleType("app.video.pipeline")
    module._busy_probe = busy
    monkeypatch.setitem(sys.modules, "app.video.pipeline", module)


def test_public_ocr_work_waits_while_a_chat_generation_is_running_and_then_goes_anyway(monkeypatch):
    set_setting(monkeypatch, "PUBLIC_API_YIELD_TO_CHAT_MAX_WAIT_S", "0.2")
    probes = []

    def busy():
        probes.append(1)
        return True

    _install_video_probe(monkeypatch, busy)

    async def scenario():
        loop = asyncio.get_running_loop()
        started = loop.time()
        async with capacity.hold("ocr", wait_s=5, yield_to_chat=True):
            return loop.time() - started

    waited = _run(scenario())
    # It yielded (asked repeatedly) and then ran anyway after the bound —
    # a chatty workspace must not starve an API caller for ever.
    assert len(probes) > 3
    assert 0.15 <= waited < 2.0


def test_public_work_that_does_not_yield_never_asks_the_chat_probe(monkeypatch):
    calls = []
    _install_video_probe(monkeypatch, lambda: calls.append(1) or True)

    async def scenario():
        async with capacity.hold("router", wait_s=1):
            pass

    _run(scenario())
    assert calls == []


def test_a_public_transcription_never_takes_the_replica_a_dictation_needs(monkeypatch):
    """Two replicas, one dictation running: a public clip would leave NO free
    replica for the next dictation, so it waits (adversarial review
    2026-09-13 — the old rule admitted it and the next dictation waited
    behind it inside whisper's lock). With no dictation running it starts."""
    monkeypatch.setattr(settings, "asr_base_urls", ("http://a/v1", "http://b/v1"))
    pool = types.SimpleNamespace(waiting=0, active=1)
    module = types.ModuleType("app.asr")
    module.POOL = pool
    monkeypatch.setitem(sys.modules, "app.asr", module)

    async def scenario():
        with pytest.raises(errors.ApiError) as refused:
            async with capacity.hold("asr", wait_s=0.1, yield_to_chat=True):
                pass
        pool.active = 0

        async with capacity.hold("asr", wait_s=0.1, yield_to_chat=True):
            return refused.value

    refusal = _run(scenario())
    assert refusal.status == 503 and refusal.code == "model_unavailable"


@pytest.mark.parametrize(
    "replicas, active, waiting, busy",
    [
        (2, 0, 0, False),
        (2, 1, 0, True),  # the case the review measured: the old rule said False
        (3, 1, 0, False),
        (3, 2, 0, True),
        (1, 0, 0, False),  # one replica: only while no dictation runs at all
        (1, 1, 0, True),
        (2, 0, 1, True),  # anyone queued for dictation
    ],
)
def test_a_replica_stays_free_for_dictation_after_a_public_clip_starts(
    monkeypatch, replicas, active, waiting, busy
):
    monkeypatch.setattr(settings, "asr_base_urls", tuple(f"http://r{i}/v1" for i in range(replicas)))
    module = types.ModuleType("app.asr")
    module.POOL = types.SimpleNamespace(waiting=waiting, active=active)
    monkeypatch.setitem(sys.modules, "app.asr", module)
    assert capacity.dictation_is_busy() is busy


def test_the_gate_publishes_its_depth_for_a_deploy_guard(monkeypatch):
    from app import metrics

    seen = {}

    def gauge(name, value, help_text="", **labels):
        seen[(name, labels.get("engine"))] = value

    monkeypatch.setattr(metrics, "set_gauge", gauge)

    async def scenario():
        async with capacity.hold("main.long", wait_s=1):
            return dict(seen)

    during = _run(scenario())
    assert during[("public_api_engine_in_flight", "main.long")] == 1
    assert seen[("public_api_engine_in_flight", "main.long")] == 0


# ------------------------------------------ no clock ends a /v1 wait (T2) --
#
# No-timeout design (2026-09-13): `hold(wait_s=None)` — the default, and
# what every durable /v1 caller passes — waits until admitted or abandoned.


def test_the_new_main_normal_gate_caps_public_work_at_six_of_chats_ten_normal_slots():
    async def scenario():
        release = asyncio.Event()
        admitted = []

        async def public(n):
            async with capacity.hold("main.normal"):
                admitted.append(n)
                await release.wait()

        tasks = [asyncio.ensure_future(public(n)) for n in range(10)]
        await asyncio.sleep(0.05)
        snap = capacity.snapshot()["main.normal"]
        release.set()
        await asyncio.gather(*tasks)
        return snap, admitted

    snap, admitted = _run(scenario())
    assert snap["max_concurrent"] == 6 and snap["in_flight"] == 6 and snap["waiting"] == 4
    assert sorted(admitted) == list(range(10))


def test_a_wait_with_no_limit_outlasts_the_old_sync_bound_and_is_still_first_come_first_served(monkeypatch):
    set_setting(monkeypatch, "PUBLIC_API_GATE_WAIT_S", "0.01")  # the legacy bound, which must not apply
    order = []

    async def scenario():
        release = asyncio.Event()

        async def holder():
            async with capacity.hold("asr"):
                order.append("holder")
                await release.wait()

        async def waiter(name):
            async with capacity.hold("asr"):
                order.append(name)

        first = asyncio.ensure_future(holder())
        await asyncio.sleep(0.01)
        waiters = [asyncio.ensure_future(waiter(f"w{n}")) for n in range(3)]
        await asyncio.sleep(0.4)  # 40x the legacy bound: nobody was refused
        assert all(not w.done() for w in waiters)
        release.set()
        await asyncio.gather(first, *waiters)

    _run(scenario())
    assert order == ["holder", "w0", "w1", "w2"]


def test_on_wait_reports_the_position_on_entry_on_every_move_and_at_least_every_interval(monkeypatch):
    monkeypatch.setattr(capacity, "ON_WAIT_EVERY_S", 0.05)
    reports = []

    async def scenario():
        release_first = asyncio.Event()
        release_second = asyncio.Event()

        async def holder(event):
            async with capacity.hold("asr"):
                await event.wait()

        first = asyncio.ensure_future(holder(release_first))
        await asyncio.sleep(0.01)
        second = asyncio.ensure_future(holder(release_second))
        await asyncio.sleep(0.01)

        async def reporter():
            async with capacity.hold("asr", on_wait=lambda position, waited: reports.append((position, waited))):
                pass

        third = asyncio.ensure_future(reporter())
        await asyncio.sleep(0.18)
        release_first.set()
        await asyncio.sleep(0.02)
        release_second.set()
        await asyncio.gather(first, second, third)

    _run(scenario())
    positions = [p for p, _ in reports]
    assert positions[0] == 2  # on entry: one waiter ahead
    assert positions.count(2) >= 3  # the periodic reports while nothing moved
    assert 1 in positions  # the move to the head of the line was reported


def test_a_run_that_yielded_goes_back_to_the_head_of_the_line():
    order = []

    async def scenario():
        release = asyncio.Event()

        async def holder():
            async with capacity.hold("main.long"):
                await release.wait()

        async def waiter(name, front=False):
            async with capacity.hold("main.long", front=front):
                order.append(name)

        first = asyncio.ensure_future(holder())
        await asyncio.sleep(0.01)
        late = asyncio.ensure_future(waiter("queued-first"))
        await asyncio.sleep(0.01)
        yielded = asyncio.ensure_future(waiter("yielded", front=True))
        await asyncio.sleep(0.01)
        release.set()
        await asyncio.gather(first, late, yielded)

    _run(scenario())
    assert order == ["yielded", "queued-first"]


def test_an_unlimited_wait_ends_on_abandon():
    async def scenario():
        abandon = asyncio.Event()
        async with capacity.hold("asr"):
            waiting = asyncio.ensure_future(capacity.hold("asr", abandon=abandon).__aenter__())
            await asyncio.sleep(0.05)
            abandon.set()
            with pytest.raises(capacity.Abandoned):
                await waiting
        return capacity.snapshot()["asr"]

    snap = _run(scenario())
    assert snap["in_flight"] == 0 and snap["waiting"] == 0


def test_a_dictation_busy_fleet_makes_public_speech_wait_instead_of_refusing(monkeypatch):
    busy = {"on": True}
    monkeypatch.setattr(capacity, "dictation_is_busy", lambda: busy["on"])
    monkeypatch.setattr(capacity, "chat_is_busy", lambda: False)
    monkeypatch.setattr(capacity, "ON_WAIT_EVERY_S", 0.05)
    notices = []

    async def scenario():
        entered = asyncio.Event()

        async def public():
            async with capacity.hold("asr", yield_to_chat=True, on_wait=lambda p, w: notices.append((p, w))):
                entered.set()

        task = asyncio.ensure_future(public())
        await asyncio.sleep(0.2)
        was_waiting = not entered.is_set() and not task.done()
        busy["on"] = False
        await asyncio.wait_for(task, 2)
        return was_waiting

    assert _run(scenario()) is True
    # The wait was reported while it happened (position 0: not in line yet),
    # so a stream can say `queued` instead of going silent.
    assert len(notices) >= 3 and all(position == 0 for position, _ in notices)


def test_the_gates_a_generation_holds_are_taken_in_one_fixed_order():
    assert capacity.gates_for("main", None) == ["main.normal"]
    assert capacity.gates_for("main", "main.extended") == ["main.extended", "main.normal"]
    assert capacity.gates_for("main", "main.long") == ["main.long"]
    assert capacity.gates_for("router", "router") == ["router"]
    assert capacity.gates_for("ocr", None) == []


def test_has_room_is_false_while_anyone_waits_so_the_dispatcher_never_jumps_the_line():
    async def scenario():
        async with capacity.hold("asr"):
            assert capacity.has_room("asr") is False
            waiter = asyncio.ensure_future(capacity.hold("asr").__aenter__())
            await asyncio.sleep(0.01)
        # The holder left and the waiter was granted in the same release.
        await waiter
        busy = capacity.has_room("asr")
        capacity._release("asr", capacity._gate("asr"), 0)
        return busy, capacity.has_room("asr")

    busy, free = _run(scenario())
    assert busy is False and free is True


def test_the_oldest_wait_is_published(monkeypatch):
    gauges = {}
    from app import metrics

    monkeypatch.setattr(metrics, "set_gauge", lambda name, value, *_a, **labels: gauges.__setitem__((name, labels.get("engine")), value))

    async def scenario():
        async with capacity.hold("asr"):
            waiter = asyncio.ensure_future(capacity.hold("asr").__aenter__())
            await asyncio.sleep(0.05)
            capacity._publish("asr", capacity._gate("asr"))
            oldest = gauges[("public_api_engine_oldest_wait_seconds", "asr")]
        await waiter
        capacity._release("asr", capacity._gate("asr"), 0)
        return oldest

    assert _run(scenario()) >= 0.04


def test_the_chat_long_probe_keeps_the_before_first_token_rule_beside_admissions_own_answer():
    """Merge of 2026-09-14. T1's admission exposes `chat_long_admission_present()`
    (a chat LONG request holds the seat or waits for it) and T1's capacity
    delegated to it. PR #65's capacity answers a narrower question — only a
    large prefill still AHEAD counts (rereview P1: a chat document already
    decoding, with the lanes reopened, must not hold public long work for its
    whole generation) — so the gate keeps reading the lanes itself."""
    from app import admission

    async def scenario():
        admission.reset()
        lanes = admission.lanes()
        # A chat document past its first token: it still holds the LONG seat,
        # the lanes are open again, nothing waits for idle.
        lanes.long.active += 1
        lanes.long.active_by_origin[admission.ORIGIN_CHAT] = lanes.long.active_by_origin.get(admission.ORIGIN_CHAT, 0) + 1
        try:
            return capacity.chat_long_admission_present(), admission.chat_long_admission_present()
        finally:
            admission.reset()

    assert _run(scenario()) == (False, True)
