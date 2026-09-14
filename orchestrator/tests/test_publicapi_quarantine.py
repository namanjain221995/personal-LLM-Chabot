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
import time

import pytest

from app.publicapi import blobs, capacity, durable, durable_store, liveness
from tests.publicapi_fake_engine import (
    FakeController,
    FakeMainEngine,
    collect,
    expected_text,
    fast_durable_settings,
    make_tenant,
    spec as make_spec,
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

    async def hook(engine, call, index):
        key = id(call)
        born.setdefault(key, cluster.generation)
        if born[key] != cluster.generation:
            raise RemoteProtocolError("peer closed connection")
        prompt = str(call.messages[0]["content"])
        if "POISON" in prompt and index == call.start_index + 2:
            cluster.restart()
            raise RemoteProtocolError("peer closed connection")

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
    from app.publicapi import engines

    monkeypatch.setattr(durable, "CONNECT_BACKOFF_S", 0.02)
    monkeypatch.setattr(durable, "CONNECT_BACKOFF_MAX_S", 0.05)
    monkeypatch.setattr(liveness, "engine_down_grace_s", lambda: 0.4)
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

    async def scenario():
        runtime = _runtime(tmp_path, view)
        runtime.witnesses_enabled = False
        await runtime.start()
        try:
            return await collect(await runtime.launch(router_spec, caller=tenant.caller(), streamed=True), limit_s=20)
        finally:
            await runtime.stop()

    records = asyncio.run(scenario())
    assert records[-1][1] == "response.failed"
    assert records[-1][2]["response"]["error"]["code"] == "model_unavailable"
    assert calls[-1] - calls[0] >= 0.4


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
