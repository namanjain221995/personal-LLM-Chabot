"""Runs that crash the engine, and runs that merely stall (no-timeout design, 2026-09-13).

The design's poison rule: one engine incident implicating a run puts it in
QUARANTINE (dispatched alone, after proven serving, with recovery budget
left); a second fails it with x-should-retry:false. A run killed by somebody
else's crash is quarantined once and then completes. These run the real
durable runner, the real liveness guard and the real Generation pump against
the deterministic fake engine and a fake controller, with the guard's
intervals scaled from minutes to tens of milliseconds.
"""
from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from app.publicapi import blobs, capacity, durable, durable_store, events, liveness
from tests.publicapi_fake_engine import (
    FakeController,
    FakeMainEngine,
    collect,
    expected_text,
    fast_durable_settings,
    make_tenant,
    spec as make_spec,
    word,
)


class RemoteProtocolError(Exception):
    """What httpx raises when the engine process dies under a stream (the
    durable runner matches the class NAME, as streaming.engine_error does)."""


@pytest.fixture(autouse=True)
def _scaled(monkeypatch):
    fast_durable_settings(monkeypatch)
    durable_store.ensure_schema()
    capacity.reset_for_tests()
    monkeypatch.setattr(blobs, "min_free_disk_bytes", lambda: 0)
    monkeypatch.setattr(liveness, "quiet_s", lambda: 0.1)
    monkeypatch.setattr(liveness, "not_serving_s", lambda: 0.0)
    monkeypatch.setattr(liveness, "lost_min_s", lambda: 0.3)
    monkeypatch.setattr(liveness, "quarantine_serving_s", lambda: 0.3)
    monkeypatch.setattr(liveness, "LOST_SAMPLE_SPACING_S", 0.0)
    yield


async def _allow_all(keys):
    return set(keys)


def _runtime(tmp_path, view, owner="A"):
    runtime = durable.Runtime()
    runtime.configure(owner=f"test-{owner}", view=view, authoriser=_allow_all,
                      blob_store=blobs.BlobStore(tmp_path / "blobs"))
    return runtime


def _text(records):
    return "".join(r[2]["delta"] for r in records if r[1] == "response.output_text.delta")


class Cluster:
    """The fake engine's crash model: when the head restarts, every stream
    that was open on the old head dies; the controller's recovery budget
    shrinks by one per restart."""

    def __init__(self, view: FakeController, budget: int = 3) -> None:
        self.view = view
        self.budget = budget
        self.generation = 0

    def restart(self) -> None:
        self.generation += 1
        self.view.restart_head()
        self.view.set(headroom=max(0, self.budget - self.view.head_restarts))


def test_a_poison_prompt_consumes_at_most_two_recoveries_then_fails_and_the_innocent_run_completes(monkeypatch, tmp_path):
    # A budget of 4 in the window: after the two recoveries the poison
    # costs, 2 remain — enough for the innocent run's quarantine rule. (With
    # production's 3 it would wait for the controller's hour window to roll,
    # which the next test proves it does rather than failing.)
    view = FakeController(time.monotonic, state="READY", head_started_at=1.0, headroom=4)
    cluster = Cluster(view, budget=4)
    born = {}

    # THE INNOCENT RUN HAS TO STILL BE RUNNING WHEN THE HEAD RESTARTS
    # (2026-09-28). This test used to arrange that with the clock: 60 tokens at
    # 0.01 s against a poison run launched 0.05 s later that restarts the head
    # two tokens in. On a loaded CI runner the sleeps coalesce, the innocent run
    # reaches its 60th token before the restart, never sees the fault it is
    # supposed to survive, and `engine_fault_attempts` is 0 instead of 1. Seen
    # on shard 2 of run 36416199617; the same shard passes 5,353/5,353 locally
    # in CI's own order, which is what a timing race looks like.
    #
    # So the innocent run now WAITS for the restart instead of racing it. The
    # timeout is a backstop: if the poison run never restarts the head, this
    # test fails on its own assertions rather than hanging.
    restarted = asyncio.Event()

    async def hook(engine, call, index):
        key = id(call)
        born.setdefault(key, cluster.generation)
        if born[key] != cluster.generation:
            raise RemoteProtocolError("peer closed connection")
        prompt = str(call.messages[0]["content"])
        if "POISON" in prompt and index == call.start_index + 2:
            cluster.restart()
            restarted.set()
            raise RemoteProtocolError("peer closed connection")
        if "POISON" not in prompt and index == call.start_index + 3 and not restarted.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(restarted.wait(), timeout=20.0)

    engine = FakeMainEngine(answer_tokens=60, delay_s=0.01, before_token=hook).install(monkeypatch)
    tenant = make_tenant(keys=2)
    poison = make_spec(messages=[{"role": "user", "content": "POISON please"}])
    innocent = make_spec(messages=[{"role": "user", "content": "an ordinary question"}])

    async def scenario():
        runtime = _runtime(tmp_path, view)
        await runtime.start()
        try:
            first = await runtime.launch(innocent, caller=tenant.caller(0), streamed=True)
            await asyncio.sleep(0.05)
            second = await runtime.launch(poison, caller=tenant.caller(1), streamed=True)
            innocent_records = await collect(first, limit_s=30)
            poison_records = await collect(second, limit_s=30)
            return innocent_records, poison_records
        finally:
            await runtime.stop()

    innocent_records, poison_records = asyncio.run(scenario())
    assert view.head_restarts <= 2
    bad = durable_store.get_run(poison.response_id)
    assert bad["status"] == "failed" and bad["error_code"] == "model_unavailable"
    assert bad["engine_fault_attempts"] == 2
    assert (bad["metadata"] or {}).get("should_retry") is False
    assert poison_records[-1][1] == "response.failed"
    assert poison_records[-1][2]["_should_retry"] is False
    assert bad["last_incident_id"]
    good = durable_store.get_run(innocent.response_id)
    assert good["status"] == "completed" and good["engine_fault_attempts"] == 1
    assert _text(innocent_records) == expected_text(60)
    poison_dispatches = [c for c in engine.calls if "POISON" in str(c.messages[0]["content"])]
    assert len(poison_dispatches) == 2  # never a third


def test_a_quarantined_run_waits_for_proven_serving_and_budget_headroom_before_dispatch(monkeypatch, tmp_path):
    view = FakeController(time.monotonic, state="READY", head_started_at=1.0, headroom=3)
    crashed = {"done": False}

    async def hook(engine, call, index):
        if not crashed["done"] and index == 3:
            crashed["done"] = True
            view.restart_head()
            view.set(state="RECOVERING", headroom=1)
            raise RemoteProtocolError("peer closed connection")

    engine = FakeMainEngine(answer_tokens=20, delay_s=0.005, before_token=hook).install(monkeypatch)
    tenant = make_tenant()
    launched = make_spec()
    timeline = {}

    async def scenario():
        runtime = _runtime(tmp_path, view)
        await runtime.start()
        try:
            handle = await runtime.launch(launched, caller=tenant.caller(), streamed=True)
            follower = asyncio.ensure_future(collect(handle, limit_s=30))
            while not crashed["done"]:
                await asyncio.sleep(0.005)
            await asyncio.sleep(0.4)
            timeline["calls_while_recovering"] = len(engine.calls)
            view.set(state="READY")  # serving again, but only 1 recovery left
            await asyncio.sleep(0.6)
            timeline["calls_without_headroom"] = len(engine.calls)
            view.set(headroom=2)
            ready_at = time.monotonic()
            while len(engine.calls) < 2:
                await asyncio.sleep(0.005)
            timeline["dispatched_after"] = time.monotonic() - ready_at
            return await follower
        finally:
            await runtime.stop()

    records = asyncio.run(scenario())
    assert timeline["calls_while_recovering"] == 1
    assert timeline["calls_without_headroom"] == 1
    assert timeline["dispatched_after"] < 2.0
    assert _text(records) == expected_text(20)
    row = durable_store.get_run(launched.response_id)
    assert row["status"] == "completed" and row["engine_fault_attempts"] == 1
    assert engine.calls[1].kwargs["continue_final_message"] is True


def test_a_head_restart_during_the_quarantined_attempts_prefill_fails_it_without_a_third_dispatch(monkeypatch, tmp_path):
    view = FakeController(time.monotonic, state="READY", head_started_at=1.0, headroom=3)
    cluster = Cluster(view)

    async def hook(engine, call, index):
        attempt = len(engine.calls)
        if attempt == 1 and index == 2:
            cluster.restart()
            raise RemoteProtocolError("peer closed connection")
        if attempt == 2 and index == call.start_index:
            # Silent prefill, then the head restarts under it.
            await asyncio.sleep(0.15)
            cluster.restart()
            await asyncio.sleep(3600)

    engine = FakeMainEngine(answer_tokens=30, delay_s=0.005, before_token=hook).install(monkeypatch)
    tenant = make_tenant()
    launched = make_spec()

    async def scenario():
        runtime = _runtime(tmp_path, view)
        await runtime.start()
        try:
            handle = await runtime.launch(launched, caller=tenant.caller(), streamed=True)
            return await collect(handle, limit_s=30)
        finally:
            await runtime.stop()

    records = asyncio.run(scenario())
    assert records[-1][1] == "response.failed"
    row = durable_store.get_run(launched.response_id)
    assert row["engine_fault_attempts"] == 2 and row["status"] == "failed"
    assert (row["metadata"] or {}).get("should_retry") is False
    assert len(engine.calls) == 2
    assert engine.open_streams == 0


def test_three_stalled_attempts_on_a_serving_engine_fail_retryably_with_the_partial_output(monkeypatch, tmp_path):
    """The engine is proven serving and idle, yet our request produces
    nothing: 'lost' three times in a row — the only way silence alone ends a
    run."""
    view = FakeController(time.monotonic, state="READY", requests_running=0, requests_waiting=0)

    async def hook(engine, call, index):
        if index >= 4:
            await asyncio.sleep(3600)

    engine = FakeMainEngine(answer_tokens=50, delay_s=0.002, before_token=hook).install(monkeypatch)
    tenant = make_tenant()
    launched = make_spec()

    async def scenario():
        runtime = _runtime(tmp_path, view)
        await runtime.start()
        try:
            handle = await runtime.launch(launched, caller=tenant.caller(), streamed=True)
            return await collect(handle, limit_s=30)
        finally:
            await runtime.stop()

    records = asyncio.run(scenario())
    row = durable_store.get_run(launched.response_id)
    assert row["status"] == "failed" and row["error_code"] == "model_unavailable"
    assert row["stalled_attempts"] == 3
    assert (row["metadata"] or {}).get("should_retry") is None  # retry-safe
    assert _text(records) == expected_text(4)  # the partial output is kept
    assert len(engine.calls) == 4  # the first attempt made tokens (reset), then three silent ones
    assert engine.open_streams == 0


def test_mutation_guard_without_the_quarantine_rule_a_run_redispatches_with_one_recovery_left(monkeypatch, tmp_path):
    """Mutation check for the quarantine gate: force `quarantine_ready` true
    and the SAME timeline as the headroom test re-dispatches at once with
    only one recovery left — so that test holds because of the rule."""
    monkeypatch.setattr(liveness.ServingTracker, "quarantine_ready", lambda self, now=None: True)
    view = FakeController(time.monotonic, state="READY", head_started_at=1.0, headroom=3)
    crashed = {"done": False}

    async def hook(engine, call, index):
        if not crashed["done"] and index == 3:
            crashed["done"] = True
            view.restart_head()
            view.set(headroom=1)
            raise RemoteProtocolError("peer closed connection")

    engine = FakeMainEngine(answer_tokens=10, delay_s=0.005, before_token=hook).install(monkeypatch)
    tenant = make_tenant()

    async def scenario():
        runtime = _runtime(tmp_path, view)
        await runtime.start()
        try:
            handle = await runtime.launch(make_spec(), caller=tenant.caller(), streamed=True)
            while not crashed["done"]:
                await asyncio.sleep(0.005)
            await asyncio.sleep(0.6)
            calls = len(engine.calls)
            await collect(handle, limit_s=30)
            return calls
        finally:
            await runtime.stop()

    assert asyncio.run(scenario()) == 2


class ConnectError(Exception):
    """httpx's refused connection, matched by name."""


def test_connect_failures_are_retried_with_growing_backoff_and_never_count_as_stalled(monkeypatch, tmp_path):
    monkeypatch.setattr(durable, "CONNECT_BACKOFF_S", 0.05)
    view = FakeController(time.monotonic, state=None)  # the controller is blind too
    starts = []

    class Refusing(FakeMainEngine):
        def __call__(self, messages, **kwargs):
            starts.append(time.monotonic())
            if len(starts) <= 3:
                async def refused():
                    raise ConnectError("connection refused")
                    yield  # pragma: no cover
                return refused()
            kwargs.pop("on_dispatch", None)
            return super().__call__(messages, **kwargs)

    engine = Refusing(answer_tokens=5)
    engine.install(monkeypatch)
    tenant = make_tenant()
    launched = make_spec()

    async def scenario():
        runtime = _runtime(tmp_path, view)
        await runtime.start()
        try:
            return await collect(await runtime.launch(launched, caller=tenant.caller(), streamed=True), limit_s=20)
        finally:
            await runtime.stop()

    records = asyncio.run(scenario())
    assert _text(records) == expected_text(5)
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert gaps[0] >= 0.05 and gaps[1] >= 0.1 and gaps[2] >= 0.2
    row = durable_store.get_run(launched.response_id)
    assert row["status"] == "completed" and row["stalled_attempts"] == 0


def test_a_sidecar_that_refuses_connections_for_the_whole_grace_fails_retryably(monkeypatch, tmp_path):
    """A sidecar that refuses every connection is retried for the WHOLE grace
    and then fails retryably.

    What the grace promises, from `_after_interrupt`: the first refused
    connection stamps `run.sidecar_down_since`, and a later refusal fails the
    run once `now - sidecar_down_since >= engine_down_grace_s()`. Only the
    failure DECISION is guaranteed to be a full grace past the first refusal.
    The dispatch that triggers that decision happens EARLIER than it, by one
    dispatch-to-check latency: the fake engine's generator raises, and only
    then does `_after_interrupt` read the clock and compare. So the last
    dispatch sits BELOW the boundary by that latency, which nothing in the
    backoff bounds -- it is scheduling and GC, and it grows with load.
    Asserting on the last dispatch's time (`calls[-1] - calls[0] >= 0.4`)
    therefore measured something the code never promised, and reddened CI run
    #259 by 0.33 ms -- 0.082% of the grace -- on a tree that passed the same
    shard twice. Re-created here by busy-waiting inside `ConnectError`, that
    old assertion is red 21/50 at 2 ms of latency (shortfall 0.03-3.30 ms) and
    12/12 at 60 ms (shortfall 26.4-49.8 ms, load ~26): the shortfall tracks
    the injected latency, not the ladder. So measure the failure, not the last
    dispatch. Both lower bounds below are one-sided by construction, so load
    can only widen them.

    What they do NOT catch, measured rather than assumed: the lower bounds
    settle only that the decision was not EARLY, and their slack is one
    backoff step of quantisation (the check runs only on a refusal, so the
    decision lands 30.9-44.3 ms past the boundary here) plus 3.6-12.5 ms of
    observation lag. A regression that fires the grace up to ~one
    CONNECT_BACKOFF_MAX_S early therefore still passes. Closing that needs a
    deterministic clock (`durable.Runtime.configure(clock=...)` with
    `tests.publicapi_fake_engine.VirtualClock`), not a tighter constant.
    """
    from app.publicapi import engines

    grace_s = 0.4
    monkeypatch.setattr(durable, "CONNECT_BACKOFF_S", 0.02)
    monkeypatch.setattr(durable, "CONNECT_BACKOFF_MAX_S", 0.05)
    monkeypatch.setattr(liveness, "engine_down_grace_s", lambda: grace_s)
    calls = []

    def refusing(resolved, messages, **kwargs):
        calls.append(time.monotonic())

        async def refused():
            raise ConnectError("connection refused")
            yield  # pragma: no cover

        return refused()

    monkeypatch.setattr(engines, "stream_chat", refusing)
    monkeypatch.setattr(engines, "target", lambda key: engines.EngineTarget(key=key, base_url="http://r:1/v1", model="m"))
    view = FakeController(time.monotonic, state="READY")
    tenant = make_tenant()
    router_spec = make_spec(model="techsara-8b-vision", engine="router", gate_engine="router",
                            max_tokens=64, planned_max_output_tokens=64, context_window=24_576)
    seen = {}

    async def scenario():
        runtime = _runtime(tmp_path, view)
        runtime.witnesses_enabled = False
        await runtime.start()
        try:
            handle = await runtime.launch(router_spec, caller=tenant.caller(), streamed=True)
            # Held for the whole scenario: `_settle` drops the run from
            # `runtime.runs`, and only the Run carries `sidecar_down_since`.
            seen["run"] = runtime.runs[router_spec.response_id]
            records = []
            # Inlined `collect` so the failure can be timestamped where it is
            # seen. `asyncio.timeout`, not `wait_for`: on CI's Python 3.11 a
            # `wait_for` swallows a same-pass cancellation (see the suite's
            # other timeouts).
            async with asyncio.timeout(20):
                async for item in handle.follow(0, heartbeat=0.05):
                    if item is durable.HEARTBEAT:
                        continue
                    records.append(item)
                    if item[1] == events.RESPONSE_FAILED:
                        seen["failed_at"] = time.monotonic()
            return records
        finally:
            await runtime.stop()

    records = asyncio.run(scenario())
    assert records[-1][1] == "response.failed"
    assert records[-1][2]["response"]["error"]["code"] == "model_unavailable"
    # The decision itself, against the instant the sidecar was first seen
    # down: this is exactly the bound `_after_interrupt` enforces.
    assert seen["failed_at"] - seen["run"].sidecar_down_since >= grace_s
    # And from outside, with no reach into the Run: nothing can fail before
    # the first dispatch plus a grace, because `sidecar_down_since` is stamped
    # only after that dispatch has been refused.
    assert seen["failed_at"] - calls[0] >= grace_s
    # Weaker than it reads: the if/elif in `_after_interrupt` stamps on the
    # first refusal and can only decide on a later one, so this cannot fail
    # while the grace is reached at all. Kept as documentation of intent.
    assert len(calls) >= 2  # retried; not failed on the first refusal
    # The OTHER side, which nothing asserted before this test: a grace that
    # regressed LONGER would still satisfy every bound above. Counted, not
    # timed, because the count's error direction under load is the safe one --
    # a stalled process gets FEWER dispatches in before the boundary, never
    # more. Measured on this ladder (0.02 -> 0.05 cap, 0.4 s grace): exactly
    # 10 dispatches in 40/40 runs at load 10.9-14.9, 9-10 with 2 ms of
    # dispatch-to-check latency, 5 with 60 ms. A doubled grace gives 15-17
    # (12/12), so 13 separates them and no wall clock is involved. A time
    # bound here would not: `failed_at - calls[0]` already reaches 0.49 s
    # unmutated against a 0.8 s mutation, and a full GC pass over this suite's
    # heap paused CI for ~0.5 s (see `_no_gc_pause` in test_ocr_classify.py),
    # so any constant that catches the regression also flakes. That same pause
    # is why none of these bounds needs `gc.freeze`: it can only widen the two
    # lower bounds and shrink the count.
    assert len(calls) <= 13
    row = durable_store.get_run(router_spec.response_id)
    assert row["status"] == "failed" and row["error_code"] == "model_unavailable"
    # "retryably", which the name claimed and nothing checked: the sidecar
    # grace settles without `should_retry`, so the caller may try again.
    assert (row["metadata"] or {}).get("should_retry") is None
    assert "_should_retry" not in records[-1][2]


def test_a_sidecar_that_answers_between_two_outages_gets_a_fresh_grace_for_the_second(monkeypatch, tmp_path):
    """The sidecar grace is CONTINUOUS down time, not a cumulative budget.

    `durable.py` clears `sidecar_down_since` on an attempt's first token,
    beside `connect_failures`. Nothing covered that reset, and without it
    refusals either side of a working answer would add up: a run could be
    failed with less than one grace of actual outage behind it. Three refused
    connections, then an attempt that streams two tokens and has its stream
    broken, then refusals for good -- the failure must still be a WHOLE grace
    after the SECOND outage began, and the partial output must survive.
    """
    from app.publicapi import engines

    grace_s = 0.4
    monkeypatch.setattr(durable, "CONNECT_BACKOFF_S", 0.02)
    monkeypatch.setattr(durable, "CONNECT_BACKOFF_MAX_S", 0.05)
    monkeypatch.setattr(liveness, "engine_down_grace_s", lambda: grace_s)
    dispatches = []  # (kind, monotonic) per call the runner made

    def flaky(resolved, messages, *, on_dispatch=None, **kwargs):
        kind = "answers" if len(dispatches) == 3 else "refused"
        dispatches.append((kind, time.monotonic()))

        async def refused():
            raise ConnectError("connection refused")
            yield  # pragma: no cover

        async def broken_after_two_tokens():
            if on_dispatch is not None:
                on_dispatch()
            yield ("token", word(0))
            yield ("token", word(1))
            raise RemoteProtocolError("peer closed connection")

        return broken_after_two_tokens() if kind == "answers" else refused()

    monkeypatch.setattr(engines, "stream_chat", flaky)
    monkeypatch.setattr(engines, "target", lambda key: engines.EngineTarget(key=key, base_url="http://r:1/v1", model="m"))
    view = FakeController(time.monotonic, state="READY")
    tenant = make_tenant()
    router_spec = make_spec(model="techsara-8b-vision", engine="router", gate_engine="router",
                            max_tokens=64, planned_max_output_tokens=64, context_window=24_576)
    seen = {}

    async def scenario():
        runtime = _runtime(tmp_path, view)
        runtime.witnesses_enabled = False
        await runtime.start()
        try:
            handle = await runtime.launch(router_spec, caller=tenant.caller(), streamed=True)
            seen["run"] = runtime.runs[router_spec.response_id]
            records = []
            async with asyncio.timeout(20):
                async for item in handle.follow(0, heartbeat=0.05):
                    if item is durable.HEARTBEAT:
                        continue
                    records.append(item)
                    if item[1] == events.RESPONSE_FAILED:
                        seen["failed_at"] = time.monotonic()
            return records
        finally:
            await runtime.stop()

    records = asyncio.run(scenario())
    kinds = [kind for kind, _ in dispatches]
    # Both of these are fixed by `flaky`'s own `len(dispatches) == 3`, so on
    # correct code they assert only "at least 4" and "at least 5 dispatches
    # happened". They are here to name the shape of the scenario, not to
    # detect a regression in it.
    assert kinds[:4] == ["refused", "refused", "refused", "answers"]
    assert kinds[4:] and set(kinds[4:]) == {"refused"}  # the second outage
    assert records[-1][1] == "response.failed"
    assert records[-1][2]["response"]["error"]["code"] == "model_unavailable"
    assert _text(records) == expected_text(2)  # the two tokens are kept
    # The reset itself, as an ordering and with no clock in it: the stamp that
    # timed the failure was taken AFTER the attempt that answered.
    assert seen["run"].sidecar_down_since > dispatches[3][1]
    # And the second outage got a grace of its own. Were the clock cumulative,
    # the failure would land about (grace - the first outage) after the second
    # one began, which is well short of this.
    assert seen["failed_at"] - dispatches[4][1] >= grace_s
    # And the same counted upper bound as the single-outage test, for the same
    # reason: measured 14 dispatches in 25/25 runs at load 13.1-15.2, against
    # 21 (10/10) with the grace doubled. Timing it would be worse here than
    # there -- the second outage's span already reaches 0.639 s unmutated
    # against a 0.808 s mutation.
    assert len(dispatches) <= 17
    row = durable_store.get_run(router_spec.response_id)
    assert row["status"] == "failed" and row["error_code"] == "model_unavailable"
    assert (row["metadata"] or {}).get("should_retry") is None  # retryable


# ---------------------------------------- adversarial review fixes (2026-09-14) --


def _two_unrelated_wedges(monkeypatch, tmp_path, *, clear_tokens):
    """Wedge 1 at token 10 of the first attempt; the run is quarantined and
    resumes; wedge 2 hits 30 tokens INTO the quarantined attempt. The GDN
    fault is load-triggered, so the second wedge says nothing about this run."""
    from tests.publicapi_fake_engine import set_setting

    set_setting(monkeypatch, "PUBLIC_API_QUARANTINE_CLEAR_TOKENS", str(clear_tokens))
    set_setting(monkeypatch, "PUBLIC_API_QUARANTINE_CLEAR_S", "3600")
    controller = FakeController(time.monotonic, state="READY", headroom=3)
    wedges = {"n": 0}

    async def hook(engine, call, index):
        if (index == 10 and wedges["n"] == 0) or (index == 40 and wedges["n"] == 1):
            wedges["n"] += 1
            controller.set(state="WEDGED", incident_id=f"inc-{wedges['n']}")
            asyncio.get_running_loop().call_later(0.5, lambda: controller.set(state="READY"))
            await asyncio.sleep(30)

    engine = FakeMainEngine(answer_tokens=80, delay_s=0.002, before_token=hook).install(monkeypatch)
    tenant = make_tenant()
    launched = make_spec()

    async def scenario():
        runtime = _runtime(tmp_path, controller)
        await runtime.start()
        try:
            handle = await runtime.launch(launched, caller=tenant.caller(), streamed=True)
            return await collect(handle, limit_s=40)
        finally:
            await runtime.stop()

    records = asyncio.run(scenario())
    return records, durable_store.get_run(launched.response_id), engine


def test_a_quarantined_run_that_decodes_past_the_clear_bound_survives_a_later_unrelated_wedge(monkeypatch, tmp_path):
    """Review P4: the engine-fault counter decays once a quarantined attempt
    has decoded PUBLIC_API_QUARANTINE_CLEAR_TOKENS, so two unrelated wedges
    hours apart no longer fail a multi-hour generation."""
    records, row, engine = _two_unrelated_wedges(monkeypatch, tmp_path, clear_tokens=20)
    assert row["status"] == "completed", row["metadata"]
    assert _text(records) == expected_text(80)
    reasons = [(a["reason"], a["implicated"]) for a in row["metadata"]["attempts"]]
    assert reasons[:2] == [("engine", True), ("engine", True)] and reasons[-1][0] == "completed"
    assert row["metadata"]["quarantine_cleared"] >= 1
    assert len(engine.calls) == 3


def test_a_second_implication_before_the_quarantined_attempt_proves_itself_still_fails_it(monkeypatch, tmp_path):
    """The poison rule is unchanged inside the clear bound: the same two
    wedges with a clear bound the quarantined attempt never reaches fail the
    run with should_retry false after two dispatches."""
    records, row, engine = _two_unrelated_wedges(monkeypatch, tmp_path, clear_tokens=1000)
    assert row["status"] == "failed" and row["engine_fault_attempts"] == 2
    assert records[-1][1] == "response.failed" and records[-1][2]["_should_retry"] is False
    assert len(engine.calls) == 2


class _RouterClient:
    """What `llm._client` returns for the router: every `create` raises what a
    real router failure raises, so the error travels through the REAL
    `engines.stream_chat` and `resilience.resilient`."""

    def __init__(self, error_factory):
        self.error_factory = error_factory
        self.calls = []
        self.chat = self
        self.completions = self

    def client(self, base_url, api_key=None, **_kwargs):
        return self

    async def create(self, **request):
        self.calls.append(time.monotonic())
        raise self.error_factory()


def _router_run(monkeypatch, tmp_path, error_factory, *, limit_s=20):
    import httpx  # noqa: F401 - the error factories use it
    from app import llm
    from app.publicapi import engines

    fake = _RouterClient(error_factory)
    monkeypatch.setattr(llm, "_client", fake.client)
    monkeypatch.setattr(
        engines, "target",
        lambda key: engines.EngineTarget(key=key, base_url=f"http://fake-{key}:8000/v1", model=f"fake-{key}"),
    )
    monkeypatch.setattr(capacity, "chat_is_busy", lambda: False)
    tenant = make_tenant()
    router_spec = make_spec(model="techsara-8b-vision", engine="router", gate_engine="router",
                            max_tokens=64, planned_max_output_tokens=64, context_window=24_576)

    async def scenario():
        runtime = _runtime(tmp_path, FakeController(time.monotonic, state="READY"))
        runtime.witnesses_enabled = False
        await runtime.start()
        try:
            handle = await runtime.launch(router_spec, caller=tenant.caller(), streamed=True)
            return await collect(handle, limit_s=limit_s)
        finally:
            await runtime.stop()

    records = asyncio.run(scenario())
    return records, durable_store.get_run(router_spec.response_id), fake


def test_a_prompt_that_crashes_the_router_mid_prefill_fails_with_should_retry_false_after_two_dispatches(monkeypatch, tmp_path):
    """Review (high) P1a: the router's connection breaks while our call is
    outstanding — the router process died under it. That is a restart
    coinciding with the call: implicated. The second one fails the run with
    should_retry false; there is no third crash."""
    import httpx

    records, row, fake = _router_run(
        monkeypatch, tmp_path,
        lambda: httpx.RemoteProtocolError("peer closed connection without sending complete message body"),
    )
    assert len(fake.calls) == 2
    assert row["status"] == "failed" and row["engine_fault_attempts"] == 2
    assert records[-1][1] == "response.failed" and records[-1][2]["_should_retry"] is False
    assert row["metadata"]["should_retry"] is False
    assert [a["reason"] for a in row["metadata"]["attempts"]] == ["restarted", "restarted"]


def test_a_router_that_answers_500_is_redispatched_once_then_fails_retryably(monkeypatch, tmp_path):
    """Review (high) P1b: an engine 5xx before the stream opens used to count
    as a refused connection and loop on backoff for the whole 1,800 s grace.
    The engine answered, so it is a dispatched attempt: re-dispatched once,
    then failed retryably."""
    import httpx
    import openai

    def server_error():
        request = httpx.Request("POST", "http://fake-router:8000/v1/chat/completions")
        return openai.InternalServerError("Internal Server Error", response=httpx.Response(500, request=request),
                                          body=None)

    started = time.monotonic()
    records, row, fake = _router_run(monkeypatch, tmp_path, server_error)
    assert len(fake.calls) == 2 and time.monotonic() - started < 10
    assert row["status"] == "failed" and row["error_code"] == "model_unavailable"
    assert row["stalled_attempts"] == 2 and row["engine_fault_attempts"] == 0
    assert "_should_retry" not in records[-1][2]
    assert [a["reason"] for a in row["metadata"]["attempts"]] == ["engine_error", "engine_error"]


def test_a_router_that_refuses_connections_is_never_counted_and_waits_out_its_grace(monkeypatch, tmp_path):
    """The other side of P1: a refused connection (or a 503) never reached the
    model — not implicated, not stalled; backoff, then the sidecar grace."""
    import httpx
    import openai

    monkeypatch.setattr(durable, "CONNECT_BACKOFF_S", 0.02)
    monkeypatch.setattr(durable, "CONNECT_BACKOFF_MAX_S", 0.05)
    monkeypatch.setattr(liveness, "engine_down_grace_s", lambda: 0.4)
    request = httpx.Request("POST", "http://fake-router:8000/v1/chat/completions")

    def refused():
        try:
            raise httpx.ConnectError("[Errno 111] Connection refused", request=request)
        except httpx.ConnectError as exc:
            error = openai.APIConnectionError(request=request)
            error.__cause__ = exc
            return error

    records, row, fake = _router_run(monkeypatch, tmp_path, refused)
    assert len(fake.calls) >= 3
    assert row["status"] == "failed" and row["error_code"] == "model_unavailable"
    assert row["stalled_attempts"] == 0 and row["engine_fault_attempts"] == 0
    assert "_should_retry" not in records[-1][2]
