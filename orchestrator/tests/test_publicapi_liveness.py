"""The liveness guard: engine evidence, never a clock (no-timeout design, 2026-09-13).

Every scenario of the design's liveness_guard ACCEPTANCE list, run at the
guard under a virtual clock and a fake controller, so an hour of silence costs
a loop iteration. The guard is ticked every 15 s exactly as
`streaming.Generation` ticks it on a quiet heartbeat.
"""
from __future__ import annotations

import asyncio

import pytest

from app.publicapi import liveness
from tests.publicapi_fake_engine import FakeController, VirtualClock

TICK = 15.0


def _guard(**controller):
    clock = VirtualClock()
    view = FakeController(clock, **controller)
    guard = liveness.MainGuard(view, clock=clock)
    return clock, view, guard


def _run_silent(clock, guard, seconds, *, on_tick=None):
    """Tick every 15 s for `seconds`; return (verdict or None, ticks, t)."""
    ticks = 0
    end = clock.now + seconds
    while clock.now < end:
        clock.advance(TICK)
        if on_tick is not None:
            on_tick(clock.now)
        verdict = guard.tick()
        ticks += 1
        if verdict.interrupt:
            return verdict, ticks, clock.now
    return None, ticks, clock.now


# ------------------------------------------------------- the acceptance --


def test_a_thirty_minute_silent_prefill_on_a_busy_engine_is_never_interrupted():
    clock, view, guard = _guard(state="BUSY", requests_running=1)
    guard.dispatched()
    verdict, ticks, _ = _run_silent(clock, guard, 1800)
    assert verdict is None
    assert ticks >= 119  # one heartbeat each, and none of them an interrupt


def test_a_controller_restart_during_a_long_prefill_is_unknown_never_down():
    """STARTING with a 10-hour-old head for 900 s, then the controller's own
    'cold start timeout' DOWN: the head is alive and old, so neither is
    proof of anything (controller.py:2846-2853)."""
    clock, view, guard = _guard(state="BUSY", head_started_at=1000.0 - 36_000)
    guard.dispatched()
    phases = []

    def on_tick(now):
        elapsed = now - 1000.0
        if elapsed < 900:
            view.set(state="STARTING", reason="cold start")
        else:
            view.set(state="DOWN", reason="cold start timeout: readiness not proven")
        phases.append(view.doc.state)

    verdict, ticks, _ = _run_silent(clock, guard, 1800, on_tick=on_tick)
    assert verdict is None
    assert {"STARTING", "DOWN"} <= set(phases)


def test_monitoring_unknown_never_interrupts_until_an_hour_of_silence():
    clock, view, guard = _guard(state="MONITORING_UNKNOWN")
    guard.dispatched()
    verdict, _, _ = _run_silent(clock, guard, 20 * 60)
    assert verdict is None
    verdict, _, at = _run_silent(clock, guard, 3600)
    assert verdict is not None and verdict.reason == liveness.REASON_BLIND
    assert 3600 <= at - 1000.0 <= 3600 + TICK


def test_a_stale_controller_is_unknown_too():
    clock, view, guard = _guard(state=None)
    guard.dispatched()
    verdict, _, at = _run_silent(clock, guard, 3700)
    assert verdict is not None and verdict.reason == liveness.REASON_BLIND
    assert at - 1000.0 >= 3600


def test_a_wedged_engine_interrupts_within_45_seconds_and_implicates_the_attempt():
    clock, view, guard = _guard(state="BUSY")
    guard.dispatched()

    def on_tick(now):
        if now - 1000.0 >= 200:
            view.set(state="WEDGED", incident_id="inc-7")

    verdict, _, at = _run_silent(clock, guard, 600, on_tick=on_tick)
    assert verdict is not None and verdict.reason == liveness.REASON_ENGINE
    assert at - 1000.0 <= 245
    assert verdict.implicated is True


def test_a_head_restart_interrupts_and_implicates_an_attempt_dispatched_before_it():
    clock, view, guard = _guard(state="BUSY", head_started_at=500.0)
    guard.dispatched()

    def on_tick(now):
        if now - 1000.0 >= 150 and view.doc.head_started_at == 500.0:
            view.restart_head(now)
            view.set(state="READY")

    verdict, _, _ = _run_silent(clock, guard, 600, on_tick=on_tick)
    assert verdict is not None and verdict.reason == liveness.REASON_ENGINE
    assert verdict.implicated is True


def test_an_attempt_dispatched_after_the_incident_began_is_not_implicated():
    clock, view, guard = _guard(state="WEDGED", incident_id="inc-1")
    clock.advance(60)
    guard.tick()
    clock.advance(TICK)
    guard.dispatched()
    verdict, _, _ = _run_silent(clock, guard, 600)
    assert verdict is not None and verdict.reason == liveness.REASON_ENGINE
    assert verdict.implicated is False


def test_an_idle_engine_after_300_seconds_has_lost_the_request():
    clock, view, guard = _guard(state="READY", requests_running=0, requests_waiting=0)
    guard.dispatched()
    verdict, _, at = _run_silent(clock, guard, 900)
    assert verdict is not None and verdict.reason == liveness.REASON_LOST
    assert at - 1000.0 >= 300


def test_a_busy_engine_is_never_declared_lost_even_after_two_hours_of_silence():
    clock, view, guard = _guard(state="BUSY", requests_running=1)
    guard.dispatched()
    verdict, _, _ = _run_silent(clock, guard, 2 * 3600)
    assert verdict is None


def test_one_busy_sample_resets_the_lost_count():
    clock, view, guard = _guard(state="READY", requests_running=0)
    guard.dispatched()
    flips = {"n": 0}

    def on_tick(now):
        flips["n"] += 1
        view.set(requests_running=1 if flips["n"] % 3 == 0 else 0)

    verdict, _, _ = _run_silent(clock, guard, 1800, on_tick=on_tick)
    assert verdict is None


def test_a_decoding_stream_never_gets_past_g0_even_on_a_wedged_verdict():
    clock, view, guard = _guard(state="WEDGED", incident_id="x")
    guard.dispatched()
    for _ in range(4 * 60):
        clock.advance(5)
        guard.chunk()
        if int(clock.now) % 15 == 0:
            assert guard.tick().interrupt is False


def test_a_four_hour_decode_is_never_interrupted():
    clock, view, guard = _guard(state="BUSY")
    guard.dispatched()
    for second in range(4 * 3600):
        clock.advance(1)
        guard.chunk()
        if second % 15 == 0:
            assert guard.tick().interrupt is False
    assert guard.new_tokens == 4 * 3600


def test_an_undispatched_attempt_is_never_judged():
    """Waits before dispatch (admission, a gate) are not silence."""
    clock, view, guard = _guard(state="WEDGED", incident_id="x")
    verdict, _, _ = _run_silent(clock, guard, 7200)
    assert verdict is None


# ------------------------------------------------------------ counters --


@pytest.mark.parametrize("reason", ["restart", "shutdown", "store", "yield", "lease_lost"])
def test_suspends_and_yields_never_count_as_stalled_or_faulted(reason):
    change = liveness.attempt_counts(
        liveness.AttemptEnd(reason=reason, dispatched=True, new_tokens=0, evidence_covered=True, implicated=True)
    )
    assert change.stalled == 0 and change.engine_fault == 0


def test_five_restart_suspends_during_a_reprefill_leave_stalled_at_zero():
    stalled = 0
    for _ in range(5):
        change = liveness.attempt_counts(
            liveness.AttemptEnd("restart", dispatched=True, new_tokens=0, evidence_covered=True, implicated=False)
        )
        stalled = 0 if change.stalled is None else stalled + change.stalled
    assert stalled == 0


def test_a_silent_end_on_a_proven_serving_engine_counts_as_stalled():
    change = liveness.attempt_counts(
        liveness.AttemptEnd("lost", dispatched=True, new_tokens=0, evidence_covered=True, implicated=False)
    )
    assert change.stalled == 1


def test_a_silent_end_with_a_gap_in_serving_evidence_is_not_stalled():
    change = liveness.attempt_counts(
        liveness.AttemptEnd("connection", dispatched=True, new_tokens=0, evidence_covered=False, implicated=False)
    )
    assert change.stalled == 0


def test_any_new_token_resets_stalled():
    change = liveness.attempt_counts(
        liveness.AttemptEnd("lost", dispatched=True, new_tokens=3, evidence_covered=True, implicated=False)
    )
    assert change.stalled is None


def test_an_undispatched_attempt_counts_nothing():
    change = liveness.attempt_counts(
        liveness.AttemptEnd("engine", dispatched=False, new_tokens=0, evidence_covered=True, implicated=True)
    )
    assert change == liveness.CounterChange(stalled=0, engine_fault=0)


def test_an_implicated_engine_incident_counts_one_fault():
    change = liveness.attempt_counts(
        liveness.AttemptEnd("engine", dispatched=True, new_tokens=0, evidence_covered=True, implicated=True)
    )
    assert change.engine_fault == 1


def test_evidence_gaps_are_measured_during_the_attempt():
    clock, view, guard = _guard(state="READY")
    guard.dispatched()
    clock.advance(15)
    guard.tick()
    view.set(state="MONITORING_UNKNOWN")
    for _ in range(4):
        clock.advance(15)
        guard.tick()
    view.set(state="READY")
    clock.advance(15)
    guard.tick()
    assert guard.max_evidence_gap_s >= 60
    assert guard.evidence_covered is False


def test_a_connection_error_without_proof_is_a_connection_interrupt():
    clock, view, guard = _guard(state="READY")
    guard.dispatched()
    assert guard.classify_error().reason == liveness.REASON_CONNECTION


def test_a_connection_error_during_a_proven_incident_is_an_engine_interrupt():
    clock, view, guard = _guard(state="READY")
    guard.dispatched()
    view.set(state="DOWN", reason="head_engine_dead", incident_id="inc-9")
    clock.advance(1)
    verdict = guard.classify_error()
    assert verdict.reason == liveness.REASON_ENGINE and verdict.implicated is True


# ------------------------------------------------ quarantine and the grace --


def test_quarantine_needs_300_seconds_of_proven_serving_and_two_recoveries_left():
    clock = VirtualClock()
    view = FakeController(clock, state="READY", headroom=3)
    tracker = liveness.ServingTracker(view, clock=clock)
    assert tracker.quarantine_ready() is False
    clock.advance(299)
    assert tracker.quarantine_ready() is False
    clock.advance(2)
    assert tracker.quarantine_ready() is True
    view.set(headroom=1)
    assert tracker.quarantine_ready() is False
    view.set(headroom=None)
    assert tracker.quarantine_ready() is False  # unknown budget waits, never gambles


def test_a_serving_gap_restarts_the_quarantine_clock():
    clock = VirtualClock()
    view = FakeController(clock, state="READY", headroom=3)
    tracker = liveness.ServingTracker(view, clock=clock)
    tracker.observe()
    clock.advance(250)
    view.set(state="RECOVERING")
    tracker.observe()
    view.set(state="READY")
    clock.advance(100)
    assert tracker.quarantine_ready() is False
    clock.advance(301)
    assert tracker.quarantine_ready() is True


def _wait(view, clock, grace, seconds_bad, then):
    """Drive wait_not_bad under the virtual clock: proven bad for
    `seconds_bad`, then `then` state."""
    start = clock.now

    async def sleep(seconds):
        clock.advance(seconds)
        if clock.now - start >= seconds_bad:
            view.set(**then)

    return asyncio.run(liveness.wait_not_bad(view, grace=grace, clock=clock, sleep=sleep, poll_s=15))


def test_an_engine_proven_down_for_1799_seconds_then_ready_resumes():
    clock = VirtualClock()
    view = FakeController(clock, state="DOWN", reason="worker_rank_dead", incident_id="i")
    grace = liveness.EngineDownGrace(view, clock=clock)
    assert _wait(view, clock, grace, 1785, {"state": "READY", "incident_id": None}) is True


def test_an_engine_proven_down_for_1800_seconds_fails_the_wait():
    clock = VirtualClock()
    view = FakeController(clock, state="DOWN", reason="worker_rank_dead", incident_id="i")
    grace = liveness.EngineDownGrace(view, clock=clock)
    with pytest.raises(liveness.EngineDownExpired):
        _wait(view, clock, grace, 10_000, {"state": "READY"})


def test_two_hours_of_unknown_while_waiting_never_expires_the_grace():
    clock = VirtualClock()
    view = FakeController(clock, state="MONITORING_UNKNOWN")
    grace = liveness.EngineDownGrace(view, clock=clock)
    for _ in range(int(2 * 3600 / 15)):
        clock.advance(15)
        assert grace.expired() is False


def test_connect_failures_with_no_serving_evidence_expire_the_grace():
    clock = VirtualClock()
    view = FakeController(clock, state=None)
    grace = liveness.EngineDownGrace(view, clock=clock)
    grace.connect_failed()
    clock.advance(1799)
    assert grace.expired() is False
    clock.advance(2)
    assert grace.expired() is True


# ----------------------------------------------------- sidecar witnesses --


def _metrics(running=1, waiting=0, gen=100.0, prompt=50.0, iterations=10.0, start=1_700_000_000.0):
    return (
        "# HELP vllm:num_requests_running x\n"
        f'vllm:num_requests_running{{model_name="m"}} {running}\n'
        f'vllm:num_requests_waiting{{model_name="m"}} {waiting}\n'
        f'vllm:generation_tokens_total{{model_name="m"}} {gen}\n'
        f'vllm:prompt_tokens_total{{model_name="m"}} {prompt}\n'
        f'vllm:iteration_tokens_total_count{{model_name="m"}} {iterations}\n'
        f'vllm:kv_cache_usage_perc{{model_name="m"}} 0.2\n'
        f"process_start_time_seconds {start}\n"
    )


def test_metrics_text_is_parsed_into_witness_values():
    sample = liveness.parse_metrics(_metrics(running=2, gen=7) + 'vllm:num_requests_running{model_name="n"} 3\n', 5.0)
    assert sample.get("running") == 5.0
    assert sample.get("generation_tokens") == 7.0
    assert sample.get("process_start") == 1_700_000_000.0


def test_a_sidecar_with_nothing_running_while_our_call_is_outstanding_has_lost_it():
    clock = VirtualClock()
    witness = liveness.SidecarWitness(clock=clock)
    started = clock.now
    for _ in range(3):
        clock.advance(15)
        witness.observe(liveness.parse_metrics(_metrics(running=0), clock.now))
    assert witness.verdict(started) == liveness.REASON_LOST


def test_a_changed_process_start_is_a_restart():
    clock = VirtualClock()
    witness = liveness.SidecarWitness(clock=clock)
    started = clock.now
    witness.observe(liveness.parse_metrics(_metrics(), clock.now))
    clock.advance(15)
    witness.observe(liveness.parse_metrics(_metrics(start=1_800_000_000.0), clock.now))
    assert witness.verdict(started) == liveness.REASON_RESTARTED


def test_an_iteration_counter_moving_through_2000_seconds_of_silence_keeps_it_progressing():
    clock = VirtualClock()
    witness = liveness.SidecarWitness(clock=clock)
    guard = liveness.SidecarGuard(witness, clock=clock)
    guard.dispatched()
    iterations = 10.0
    for _ in range(int(2000 / 15)):
        clock.advance(15)
        iterations += 1
        witness.observe(liveness.parse_metrics(_metrics(iterations=iterations), clock.now))
        assert guard.tick().interrupt is False
    assert witness.verdict(guard.dispatched_at) == "progressing"


def test_frozen_witnesses_with_work_running_are_stalled():
    clock = VirtualClock()
    witness = liveness.SidecarWitness(clock=clock)
    guard = liveness.SidecarGuard(witness, clock=clock)
    guard.dispatched()
    verdict = None
    for _ in range(20):
        clock.advance(15)
        witness.observe(liveness.parse_metrics(_metrics(), clock.now))
        verdict = guard.tick()
        if verdict.interrupt:
            break
    assert verdict.interrupt and verdict.reason == liveness.REASON_STALLED


def test_an_unknown_witness_falls_back_to_1800_seconds_of_silence():
    clock = VirtualClock()
    guard = liveness.SidecarGuard(liveness.SidecarWitness(clock=clock), clock=clock)
    guard.dispatched()
    clock.advance(1799)
    assert guard.tick().interrupt is False
    clock.advance(2)
    verdict = guard.tick()
    assert verdict.interrupt and verdict.reason == liveness.REASON_BLIND


def test_the_sampler_is_reference_counted_per_engine():
    fetched = []

    async def fetch(root):
        fetched.append(root)
        return _metrics()

    async def scenario():
        sampler = liveness.WitnessSampler(fetch=fetch)
        a = sampler.acquire("router", "http://r:1")
        b = sampler.acquire("router", "http://r:1")
        assert a is b
        await asyncio.sleep(0.01)
        sampler.release("router")
        assert "router" in sampler._entries
        sampler.release("router")
        assert "router" not in sampler._entries

    asyncio.run(scenario())
    assert fetched == ["http://r:1"]


def test_mutation_guard_the_cold_start_down_exception_is_what_keeps_scenario_two_alive(monkeypatch):
    """Mutation check: without the cold-start carve-out the SAME timeline
    interrupts — proving scenario two passes because of the rule, not by
    accident."""
    clock, view, guard = _guard(state="BUSY", head_started_at=1000.0 - 36_000)
    guard.dispatched()
    real = FakeController.proven_not_serving

    def naive(self, now):
        state = self.doc.state
        return state if state in ("WEDGED", "RECOVERING", "STARTING", "DOWN") else None

    monkeypatch.setattr(FakeController, "proven_not_serving", naive)

    def on_tick(now):
        view.set(state="DOWN", reason="cold start timeout: readiness not proven")

    verdict, _, _ = _run_silent(clock, guard, 600, on_tick=on_tick)
    assert verdict is not None and verdict.reason == liveness.REASON_ENGINE
    monkeypatch.setattr(FakeController, "proven_not_serving", real)


# ---------------------------------------- adversarial review fixes (2026-09-14) --


def _state_doc(state, reason, running, generated_at):
    from app import engine_state

    return {
        "state": state, "state_code": engine_state.STATE_CODES[state], "reason": reason,
        "primary_ready": True, "generated_at": generated_at,
        "signals": {"head_container": {"running": True, "engine_process_alive": True, "started_at": 1.0},
                    "engine": {"requests_running": running, "requests_waiting": 0}},
    }


def _drive_real_adapter(monkeypatch, state, reason, seconds=900):
    """The REAL EngineStateView over engine_state snapshots built by the real
    parser, one controller document per 15 s tick."""
    from app import engine_state

    base, wall = 50_000.0, 1_800_000_000.0
    view = liveness.EngineStateView()
    monkeypatch.setattr(engine_state, "_snapshot", engine_state.parse_state_document(
        _state_doc("READY", "canary ok", 1, wall), observed_at=base, read_wall=wall))
    guard = liveness.MainGuard(view, clock=lambda: base)
    guard.dispatched(base)
    for step in range(15, seconds + 1, 15):
        monkeypatch.setattr(engine_state, "_snapshot", engine_state.parse_state_document(
            _state_doc(state, reason, 0, wall + step), observed_at=base + step, read_wall=wall + step))
        verdict = guard.tick(base + step)
        if verdict.interrupt:
            return verdict, step
    return None, seconds


def test_a_stale_idle_sample_under_degraded_metrics_unavailable_never_declares_the_request_lost(monkeypatch):
    """Review P3: when /metrics fails the controller keeps its last good
    sample (running=0, taken before this request reached vLLM) and publishes
    DEGRADED 'metrics unavailable' under a fresh generated_at. That is not
    evidence the engine lost the request: a 15-minute prefill runs on."""
    verdict, _ = _drive_real_adapter(monkeypatch, "DEGRADED", "metrics unavailable")
    assert verdict is None


def test_control_the_same_idle_samples_from_a_ready_controller_do_declare_the_request_lost(monkeypatch):
    verdict, at = _drive_real_adapter(monkeypatch, "READY", "canary ok")
    assert verdict is not None and verdict.reason == liveness.REASON_LOST and at >= 300


def test_an_idle_sample_older_than_45_seconds_is_not_evidence_for_lost():
    clock, view, guard = _guard(state="READY", requests_running=0, sample_age_s=120.0)
    guard.dispatched()
    verdict, _, _ = _run_silent(clock, guard, 2 * 3600)
    assert verdict is None


def test_a_frozen_idle_sample_taken_before_dispatch_never_declares_the_request_lost():
    clock, view, guard = _guard(state="READY", requests_running=0)
    dispatched_at = clock.now
    guard.dispatched()
    taken = dispatched_at - 10.0

    verdict, _, _ = _run_silent(clock, guard, 3600, on_tick=lambda now: view.set(sample_age_s=now - taken))
    assert verdict is None


def test_idle_samples_taken_before_dispatch_are_never_counted_even_with_a_short_lost_floor(monkeypatch):
    """Three idle samples 10 s apart, all fresh (≤ 45 s old), all taken
    BEFORE this attempt was dispatched: they say nothing about whether the
    engine has the request. (The 300 s floor hides this by default; an
    operator's shorter PUBLIC_API_LIVENESS_LOST_MIN_S would not.)"""
    monkeypatch.setattr(liveness, "quiet_s", lambda: 0.5)
    monkeypatch.setattr(liveness, "lost_min_s", lambda: 0.0)
    clock, view, guard = _guard(state="READY", requests_running=0)
    dispatched_at = clock.now
    guard.dispatched()
    verdicts = []
    for step, taken in ((1.0, dispatched_at - 30.0), (2.0, dispatched_at - 20.0), (3.0, dispatched_at - 10.0)):
        clock.now = dispatched_at + step
        view.set(sample_age_s=clock.now - taken)
        verdicts.append(guard.tick())
    assert [v.interrupt for v in verdicts] == [False, False, False]


def test_fresh_idle_samples_taken_after_dispatch_still_declare_the_request_lost():
    clock, view, guard = _guard(state="READY", requests_running=0, sample_age_s=5.0)
    guard.dispatched()
    verdict, _, at = _run_silent(clock, guard, 900)
    assert verdict is not None and verdict.reason == liveness.REASON_LOST and at - 1000.0 >= 300


def test_the_adapter_derives_the_samples_age_when_engine_state_exposes_its_timestamp(monkeypatch):
    from app import engine_state

    snap = engine_state.parse_state_document(
        _state_doc("READY", "canary ok", 0, 1_800_000_100.0), observed_at=10.0, read_wall=1_800_000_100.0)
    monkeypatch.setattr(engine_state, "_snapshot", snap)
    monkeypatch.setattr(engine_state, "engine_load", lambda now=None: {
        "requests_running": 0.0, "requests_waiting": 0.0, "age_s": 4.0,
        "generated_at": 1_800_000_100.0, "sample_observed_at": 1_800_000_070.0,
    })
    load = liveness.EngineStateView().engine_load(14.0)
    assert load["sample_age_s"] == 34.0


def test_the_real_engine_state_gives_the_adapter_a_samples_age_and_g2_ignores_an_old_sample(monkeypatch):
    """Assembler, 2026-09-14: end to end through the REAL parser and the REAL
    engine_state.engine_load (no stub): a document generated now whose engine
    sample was taken 90 s ago yields sample_age_s >= 90, past the 45 s G2
    bound, so it is not a current idle sample."""
    from app import engine_state

    wall = 1_800_000_100.0
    doc = _state_doc("READY", "canary ok", 0, wall)
    doc["signals"]["engine"]["observed_at"] = wall - 90.0
    snap = engine_state.parse_state_document(doc, observed_at=10.0, read_wall=wall)
    monkeypatch.setattr(engine_state, "_snapshot", snap)
    load = liveness.EngineStateView().engine_load(10.0)
    assert load is not None
    assert load["sample_age_s"] >= 90.0 > liveness.LOST_SAMPLE_MAX_AGE_S


@pytest.mark.parametrize(
    "build, kind",
    [
        ("connect_refused", "connect"),
        ("connect_timeout", "connect"),
        ("status_503", "connect"),
        ("status_502_wrapped", "connect"),
        ("status_500", "status"),
        ("status_500_wrapped", "status"),
        ("read_timeout", "timeout"),
        ("protocol_wrapped", "broken"),
        ("protocol", "broken"),
    ],
)
def test_a_sidecar_error_is_classified_through_its_whole_cause_chain(build, kind):
    """Review (high): the router's failures arrive wrapped — resilience's
    ModelUnavailable carries the engine error in `.last`, and the OpenAI client
    raises APIConnectionError/APITimeoutError FROM the httpx error."""
    import httpx
    import openai

    from app.resilience import ModelUnavailable

    request = httpx.Request("POST", "http://fake-router:8000/v1/chat/completions")

    def chained(outer, inner):
        outer.__cause__ = inner
        return outer

    def status(code):
        return openai.APIStatusError("server error", response=httpx.Response(code, request=request), body=None)

    errors_by_name = {
        "connect_refused": lambda: chained(openai.APIConnectionError(request=request),
                                           httpx.ConnectError("refused", request=request)),
        "connect_timeout": lambda: chained(openai.APITimeoutError(request=request),
                                           httpx.ConnectTimeout("timed out", request=request)),
        "status_503": lambda: status(503),
        "status_502_wrapped": lambda: ModelUnavailable("http://fake-router:8000/v1", 0.0, 1, status(502)),
        "status_500": lambda: status(500),
        "status_500_wrapped": lambda: ModelUnavailable("http://fake-router:8000/v1", 0.0, 1, status(500)),
        "read_timeout": lambda: chained(openai.APITimeoutError(request=request),
                                        httpx.ReadTimeout("timed out", request=request)),
        "protocol_wrapped": lambda: ModelUnavailable(
            "http://fake-router:8000/v1", 0.0, 1,
            chained(openai.APIConnectionError(request=request), httpx.RemoteProtocolError("peer closed"))),
        "protocol": lambda: httpx.RemoteProtocolError("peer closed connection without sending complete message body"),
    }
    assert liveness.sidecar_error_kind(errors_by_name[build]()) == kind


def test_a_sidecar_restart_counts_as_an_engine_fault_and_a_refused_connection_counts_nothing():
    restarted = liveness.attempt_counts(liveness.AttemptEnd(
        reason=liveness.REASON_RESTARTED, dispatched=True, new_tokens=0, evidence_covered=True, implicated=True))
    refused = liveness.attempt_counts(liveness.AttemptEnd(
        reason=liveness.REASON_CONNECT, dispatched=False, new_tokens=0, evidence_covered=True, implicated=False))
    answered = liveness.attempt_counts(liveness.AttemptEnd(
        reason=liveness.REASON_ENGINE_ERROR, dispatched=True, new_tokens=0, evidence_covered=True, implicated=False))
    assert (restarted.engine_fault, refused.engine_fault, refused.stalled) == (1, 0, 0)
    assert answered == liveness.CounterChange(stalled=1, engine_fault=0)
