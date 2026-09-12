"""Detection → confirmation → the choreography (contract §6.3), the lock, the
budget, the cooldown, the loopback-only POST /recover (subject to the same
budget), incident closure by any path, and the /metrics document of §7.1 v2.
Tests named after a review finding reproduce that finding's scenario."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import socket
import time

from common import FAILURE_CATEGORIES
from controller import ATTEMPT_OUTCOMES, Handler, PROBE_OUTCOMES, STATES, STEPS, WORKER_RESTART_OUTCOMES


def _steps(world):
    return [s["step"] for s in world.ctl.rec.steps]


# ---------------------------------------------------------------------------
# Trigger 1: worker rank dead — one observation, and the choreography order
# ---------------------------------------------------------------------------


def test_worker_rank_dead_triggers_recovery_in_one_observation_with_the_choreography_order(world):
    world.make_ready()
    world.heal_on_restart()
    # The worker container was restarted (the last-resort healthcheck) after
    # a fatal signature: its started_at is now newer than the last proven
    # completion.
    fault_at = world.clock.time() + 1
    world.clock.advance(2)
    world.sentinel.last_fault = {"at": fault_at, "signature": "misaligned address"}
    world.sentinel.container["started_at"] = world.clock.time()
    world.sentinel.container["restart_count"] += 1

    world.tick()   # ONE observation
    assert world.ctl.rec.incident_id is not None
    assert world.ctl.incident["category"] == "worker_rank_dead"
    assert world.ctl.state == "RECOVERING", world.ctl.reason
    # the failure state was published before RECOVERING
    names = [t["to"] for t in world.ctl.transitions]
    assert names[-2:] == ["DOWN", "RECOVERING"]
    # choreography: sentinel restart BEFORE the head's docker restart
    assert world.order == ["sentinel_restart", "docker_restart"]
    assert len(world.head_restarts) == 1 and world.head_restarts[0][3] == world.cfg.head_restart_timeout_s
    assert _steps(world) == ["confirm", "capture", "stop_pair", "wait_load"]
    assert world.ctl.rec.worker_restart == "ok"
    assert [s for s in world.ctl.rec.steps if s["step"] == "stop_pair"][0]["worker"] == "ok"

    # diagnostics were captured BEFORE the restarts, into the incident dir
    inc = os.path.join(world.cfg.incident_dir, world.ctl.rec.incident_id)
    assert sorted(os.listdir(inc)) == ["head-logs-1.txt", "state-1.json", "worker-diagnostics-1.txt"]
    state_doc = json.load(open(os.path.join(inc, "state-1.json")))
    assert state_doc["recovery"]["step"] == "capture"
    assert "worker log line 1" in open(os.path.join(inc, "worker-diagnostics-1.txt")).read()

    world.drive_recovery_to_ready()
    assert _steps(world) == ["confirm", "capture", "stop_pair", "wait_load", "canary", "mark_ready", "verify"]
    assert world.ctl.state == "READY"
    assert world.ctl.rec.in_progress is False and world.ctl.rec.step == "verify"
    assert world.ctl.attempts_total == {"started": 1, "succeeded": 1, "failed": 0, "budget_exhausted": 0}
    assert world.ctl.recovery_duration_s is not None
    assert world.ctl.incident["ended_at"] is None
    # the readiness evidence (GPU maxima) is in the incident dir
    rd_doc = json.load(open(os.path.join(inc, "readiness-1.json")))
    assert rd_doc["readiness"]["passed"] is True and rd_doc["gpus"]["head_util"] == 93.0
    # VERIFY_STABILITY: three consecutive canary successes close the incident
    for _ in range(3):
        world.clock.advance(world.cfg.canary_interval_s + 1)
        world.settle()
    assert world.ctl.incident["ended_at"] is not None
    assert world.ctl.rec.step == "idle"
    # nothing else was restarted along the way
    assert world.order == ["sentinel_restart", "docker_restart"]


def test_worker_fault_signature_alone_after_the_head_start_triggers_without_a_worker_restart(world):
    """Coverage gap: the last_fault-only branch of trigger 1 (the worker
    container did NOT restart; the sentinel is non-autonomous in v2)."""
    world.make_ready()
    world.heal_on_restart()
    world.clock.advance(2)
    world.sentinel.last_fault = {"at": world.clock.time(), "signature": "NCCL error"}
    world.tick()
    assert world.ctl.rec.in_progress and world.ctl.rec.category == "worker_rank_dead"
    assert "worker fault 'NCCL error'" in world.ctl.rec.reason
    assert world.order == ["sentinel_restart", "docker_restart"]
    world.drive_recovery_to_ready()
    # after the pair restart the fault predates both containers: no re-trigger
    world.tick(3)
    assert "worker_rank_dead" not in world.ctl.pending


def test_a_fault_from_the_workers_previous_incarnation_is_not_a_trigger(world):
    world.make_ready()
    # a fault, then the worker restarted (healthcheck) BEFORE the last proven completion
    world.sentinel.last_fault = {"at": world.clock.time() - 30, "signature": "misaligned address"}
    world.sentinel.container["started_at"] = world.clock.time() - 20
    world.ctl.last_success_at = world.clock.time()
    world.tick()
    assert world.ctl.rec.in_progress is False and world.ctl.pending == {}


def test_worker_rank_process_absent_after_grace_triggers_and_a_starting_worker_does_not(world):
    world.make_ready()
    world.heal_on_restart()
    # a worker that just (re)started before the last completion is not a fault …
    world.sentinel.rank_process_alive = False
    world.sentinel.container["started_at"] = world.clock.time() - 10
    world.ctl.last_success_at = world.clock.time()   # proven after that start
    world.tick()
    assert world.ctl.rec.in_progress is False
    # … until the rank has been absent past the grace
    world.clock.advance(world.cfg.worker_rank_grace_s)
    world.ctl.last_success_at = world.clock.time()
    world.tick()
    assert world.ctl.rec.in_progress is True and world.ctl.rec.category == "worker_rank_dead"


def test_worker_fault_during_wait_load_fails_the_attempt_early_and_the_next_attempt_follows_the_cooldown(world):
    """[major] a rank dying while the new head loads used to wait out the
    900 s cold-start budget with two attempts left unused; now the attempt
    fails with the real category (two looks ≥ 10 s apart) and the next one
    starts after the cooldown."""
    world.cfg.recovery_cooldown_s = 120.0
    world.make_ready()
    world.head.mode = "engine_dead"        # the restarted head never reaches /health 200 by itself
    world.tick()
    assert world.ctl.rec.in_progress and world.ctl.rec.step == "wait_load"
    # the worker (restarted by us at stop_pair) logs a fatal signature after the new head started
    world.clock.advance(30)
    world.sentinel.last_fault = {"at": world.clock.time(), "signature": "NCCL error"}
    world.tick()
    assert world.ctl.rec.in_progress and world.ctl.rec.load_fault is not None   # seen once: confirming
    world.tick(advance=world.cfg.head_api_dead_gap_s + 1)
    assert world.ctl.rec.in_progress is False
    assert world.ctl.attempts_total["failed"] == 1
    assert world.ctl.last_failure_category == "worker_rank_dead"
    assert world.ctl.rec.last["failure"] == "worker_rank_dead"
    # the same fault is re-confirmed at once, blocked only by the cooldown
    world.tick(advance=5)
    assert world.ctl.state == "DOWN" and world.ctl.rec.blocked == "cooldown", world.ctl.reason
    assert len(world.head_restarts) == 1
    world.heal_on_restart()
    world.tick(advance=120)
    assert world.ctl.rec.in_progress and world.ctl.rec.attempt == 2
    assert len(world.head_restarts) == 2
    world.drive_recovery_to_ready()
    assert world.ctl.state == "READY"


def test_a_worker_re_created_by_its_restart_policy_during_wait_load_does_not_fail_the_attempt(world):
    """The 22:21:10Z shape: the new rank joined the OLD head's store and died;
    Docker re-creates the worker within seconds and it pairs with the new
    head. One fault observation must not cost an attempt."""
    world.make_ready()
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress and world.ctl.rec.step == "wait_load"
    world.clock.advance(5)
    world.sentinel.last_fault = {"at": world.clock.time(), "signature": "died unexpectedly"}
    world.tick()
    assert world.ctl.rec.load_fault is not None and world.ctl.rec.in_progress
    world.clock.advance(3)
    world.sentinel.container["started_at"] = world.clock.time()      # the restart policy re-created it
    world.tick(advance=10)
    assert world.ctl.rec.in_progress and world.ctl.rec.load_fault is None
    assert world.ctl.attempts_total["failed"] == 0
    world.head.mode = "healthy"
    world.drive_recovery_to_ready()
    assert world.ctl.attempts_total["succeeded"] == 1


# ---------------------------------------------------------------------------
# Trigger 2: head engine dead — one observation on /health 5xx
# ---------------------------------------------------------------------------


def test_head_engine_dead_on_health_5xx_needs_one_observation(world):
    world.make_ready()
    world.heal_on_restart()
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.category == "head_engine_dead" and world.ctl.rec.in_progress
    assert world.order == ["sentinel_restart", "docker_restart"]
    world.drive_recovery_to_ready()
    assert world.ctl.state == "READY"


def test_head_process_table_missing_engine_needs_two_observations(world):
    world.make_ready()
    world.heal_on_restart()
    world.head_container.processes = [p for p in world.head_container.processes if "EngineCore" not in p]
    world.tick()
    assert world.ctl.rec.in_progress is False and world.ctl.pending["head_engine_dead:process"].count == 1
    world.tick()
    assert world.ctl.rec.in_progress and world.ctl.rec.category == "head_engine_dead"


# ---------------------------------------------------------------------------
# Trigger 3: head API dead — two observations at least 10 s apart
# ---------------------------------------------------------------------------


def test_head_api_dead_needs_two_observations_ten_seconds_apart(world):
    world.make_ready()
    world.heal_on_restart(start_api=True)
    world.head.stop()                      # listener closed → connection refused

    world.tick()
    assert world.ctl.api["tcp"] is False and world.ctl.api["tcp_error"] == "refused"
    assert world.ctl.rec.in_progress is False
    assert world.ctl.state == "DEGRADED", world.ctl.reason   # unconfirmed: not yet DOWN

    world.tick(advance=5)                  # 5 s apart: still not confirmed
    assert world.ctl.rec.in_progress is False and world.head_restarts == []

    world.tick(advance=5)                  # 10 s apart: confirmed
    assert world.ctl.rec.in_progress and world.ctl.rec.category == "head_api_dead"
    assert [t["to"] for t in world.ctl.transitions][-2:] == ["DOWN", "RECOVERING"]
    assert len(world.head_restarts) == 1
    world.drive_recovery_to_ready()
    assert world.ctl.state == "READY"


def test_head_api_dead_before_ready_is_starting_not_a_trigger(world):
    world.head.stop()
    world.tick(3, advance=10)
    assert world.ctl.state == "STARTING" and world.head_restarts == []


def test_a_confirmed_but_blocked_failure_is_not_published_as_monitoring_unknown(world):
    """[minor] head_api_dead confirmed via a connect TIMEOUT during the
    cooldown used to flap DOWN → MONITORING_UNKNOWN → DOWN every tick."""
    world.cfg.recovery_cooldown_s = 300.0
    world.cfg.probe_timeout_s = 0.3
    world.make_ready()
    world.heal_on_restart()
    world.head.mode = "engine_dead"
    world.tick()
    world.drive_recovery_to_ready()
    assert world.ctl.cooldown_until is not None
    world.cfg.head_api_url = "http://10.255.255.1:9"      # blackhole: connects time out
    world.tick()
    world.tick(advance=world.cfg.head_api_dead_gap_s + 1)
    assert world.ctl.confirmed is not None and world.ctl.confirmed[0] == "head_api_dead"
    assert world.ctl.state == "DOWN" and world.ctl.rec.blocked == "cooldown", world.ctl.reason
    before = len(world.ctl.transitions)
    world.tick(3, advance=5)
    assert world.ctl.state == "DOWN"
    assert all(t["to"] != "MONITORING_UNKNOWN" for t in world.ctl.transitions[before:])
    assert len(world.ctl.transitions) == before


# ---------------------------------------------------------------------------
# Trigger 4: wedged — frozen tokens + outstanding canary, two observations
# ---------------------------------------------------------------------------


def test_wedged_from_frozen_tokens_and_outstanding_canary_with_two_observation_confirmation(world):
    world.cfg.canary_timeout_s = 300.0     # the probe stays in flight for this test
    world.make_ready()
    world.heal_on_restart()
    # the engine has requests but the counters stop moving
    world.head.mode = "wedged"
    world.head.running = 9.0
    world.tick()                            # first sample with requests running
    world.clock.advance(world.cfg.canary_interval_s + 1)
    world.tick()                            # the canary starts and hangs
    assert world.ctl.canary.in_flight
    world.clock.advance(95)                 # frozen ≥ 90, canary outstanding ≥ 60
    world.tick()                            # observation 1 → detect only
    assert world.ctl.engine["frozen_seconds"] >= world.cfg.frozen_s
    assert "wedged_frozen_tokens" in world.ctl.pending
    assert world.ctl.pending["wedged_frozen_tokens"].count == 1
    assert world.ctl.rec.in_progress is False and world.ctl.state != "WEDGED"
    assert world.ctl.rec.step == "detect"
    world.tick()                            # observation 2 → confirm
    names = [t["to"] for t in world.ctl.transitions]
    assert names[-2:] == ["WEDGED", "RECOVERING"], names
    assert world.ctl.rec.category == "wedged_frozen_tokens"
    assert world.ctl.last_failure_category == "wedged_frozen_tokens"
    assert world.order == ["sentinel_restart", "docker_restart"]
    world.head.release.set()
    world.drive_recovery_to_ready()
    assert world.ctl.state == "READY"


def test_frozen_counters_without_requests_is_not_a_wedge(world):
    world.make_ready()
    world.head.running = 0.0
    for _ in range(4):
        world.tick(advance=60)
    assert world.ctl.engine["frozen_seconds"] == 0.0
    assert world.ctl.pending == {}


# ---------------------------------------------------------------------------
# Trigger 5: two consecutive canary timeouts
# ---------------------------------------------------------------------------


def test_two_consecutive_canary_timeouts_trigger_canary_timeout(world):
    world.cfg.canary_timeout_s = 0.5
    world.make_ready()
    world.heal_on_restart()
    world.head.mode = "wedged"
    world.head.wedged_hold_s = 2.0
    for _ in range(2):
        world.clock.advance(world.cfg.canary_interval_s + 1)
        world.ctl.tick()                    # starts a probe
        world.ctl.canary.join(3.0)
    world.ctl.tick()                        # harvests the second timeout → confirmed
    assert world.ctl.probes_total["timeout"] == 2
    assert world.ctl.rec.in_progress and world.ctl.rec.category == "canary_timeout"
    assert [t["to"] for t in world.ctl.transitions][-2:] == ["WEDGED", "RECOVERING"]


def test_hung_api_is_confirmed_by_two_canary_timeouts_regardless_of_health(world):
    """[major] TCP accepts, /health never answers, completions never answer:
    a proven engine is confirmed canary_timeout after two probes, without
    /health == 200."""
    world.cfg.canary_timeout_s = 0.5
    world.cfg.probe_timeout_s = 0.3
    world.make_ready()
    world.heal_on_restart()
    world.head.mode = "hung"
    world.head.wedged_hold_s = 3.0
    world.clock.advance(world.cfg.canary_interval_s + 1)
    world.ctl.tick()
    assert world.ctl.api["tcp"] is True and world.ctl.api["health"] is None
    world.ctl.canary.join(3.0)
    world.ctl.tick()                                    # harvests timeout 1
    assert world.ctl.consecutive_timeouts == 1 and world.ctl.last_canary.health_at_start is None
    assert world.ctl.state == "DEGRADED" and world.ctl.rec.in_progress is False
    world.clock.advance(world.cfg.canary_interval_fast_s + 1)
    world.ctl.tick()                                    # the confirming probe comes at the FAST interval
    assert world.ctl.canary.in_flight
    world.ctl.canary.join(3.0)
    world.ctl.tick()                                    # harvests timeout 2 → confirmed
    assert world.ctl.probes_total["timeout"] == 2
    assert world.ctl.rec.in_progress and world.ctl.rec.category == "canary_timeout", world.ctl.reason
    world.head.release.set()
    world.drive_recovery_to_ready()
    assert world.ctl.state == "READY"


def test_timeouts_during_model_load_after_an_external_restart_never_confirm_canary_timeout(world):
    """[blocker] vLLM binds :8000 before the engine is built: during the
    5-minute load TCP accepts, /health times out and every probe times out.
    Those timeouts must not confirm a wedge on the first tick /health
    answers 200 — the counters reset with the head's started_at, and a
    timeout on an unproven engine is STARTING, not evidence."""
    world.cfg.canary_timeout_s = 0.5
    world.cfg.probe_timeout_s = 0.3
    world.make_ready()
    world.heal_on_restart()
    # one timeout on the PROVEN engine first: consecutive_timeouts = 1
    world.head.mode = "hung"
    world.head.wedged_hold_s = 3.0
    world.clock.advance(world.cfg.canary_interval_s + 1)
    world.ctl.tick()
    world.ctl.canary.join(3.0)
    world.ctl.tick()
    assert world.ctl.consecutive_timeouts == 1 and world.ctl.state == "DEGRADED"
    # an EXTERNAL restart (healthcheck / docker restart): new incarnation, still loading
    world.head.release.set()
    world.head.release = __import__("threading").Event()
    world.clock.advance(5)
    world.head_container.started_at = world.clock.time()
    world.head_container.restart_count += 1
    for _ in range(3):
        world.clock.advance(world.cfg.canary_interval_fast_s + 1)
        world.ctl.tick()
        world.ctl.canary.join(3.0)
    world.ctl.tick()
    assert world.ctl.consecutive_timeouts == 0                # reset on the new start, unproven: never counted
    assert world.ctl.probes_total["timeout"] >= 2
    assert world.ctl.state == "STARTING", world.ctl.reason
    # the load finishes: /health 200 in the same tick the old probes are behind us
    world.head.release.set()
    world.head.mode = "healthy"
    world.clock.advance(world.cfg.canary_interval_fast_s + 1)
    world.settle()
    assert world.ctl.rec.in_progress is False and world.head_restarts == []
    assert all(t["to"] not in ("WEDGED", "RECOVERING") for t in world.ctl.transitions)
    assert world.ctl.state == "READY", world.ctl.reason


def test_canary_timeouts_on_a_progressing_engine_are_saturation_not_a_wedge(world):
    world.cfg.canary_timeout_s = 0.5
    world.make_ready()
    world.heal_on_restart()
    # nine requests running, counters moving on every scrape, canary starved
    world.head.mode = "wedged"
    world.head.wedged_hold_s = 2.0
    world.head.running = 9.0
    world.head.progress_per_scrape = 500.0
    for _ in range(3):
        world.clock.advance(world.cfg.canary_interval_s + 1)
        world.ctl.tick()
        world.ctl.canary.join(3.0)
    world.ctl.tick()
    assert world.ctl.probes_total["timeout"] >= 2
    assert world.ctl.rec.in_progress is False and world.head_restarts == []
    assert world.ctl.state == "DEGRADED" and "saturation" in world.ctl.reason, world.ctl.reason
    assert world.ctl.engine["frozen_seconds"] == 0.0
    # …but not forever: past the starvation ceiling it is a wedge after all
    world.clock.advance(world.cfg.canary_starvation_s)
    world.ctl.tick()
    world.ctl.canary.join(3.0)
    world.ctl.tick()
    assert world.ctl.rec.in_progress and world.ctl.rec.category == "canary_timeout"
    world.head.release.set()


# ---------------------------------------------------------------------------
# MARK_READY takes only the new head's own proof
# ---------------------------------------------------------------------------


def test_recovery_is_not_marked_succeeded_on_a_completion_from_the_old_head(world):
    """[major] a probe in flight against the still-serving head at CONFIRM
    completes during the blocking recovery calls; it must never be the
    recovery's proof."""
    world.make_ready()
    world.head.completion_delay_s = 1.0                 # the next routine probe is slow
    world.clock.advance(world.cfg.canary_interval_s + 1)
    world.tick()
    assert world.ctl.canary.in_flight and world.ctl.canary.kind == "routine"
    # the operator asks for a recovery while that probe is in flight; the
    # NEW container never answers /health (it is loading, forever, here)
    world.head_container.on_restart = lambda c: setattr(world.head, "mode", "hung")
    status, _ = world.ctl.request_manual_recovery()
    assert status == 202
    world.tick()
    assert world.ctl.rec.in_progress and world.ctl.rec.step == "wait_load"
    assert world.ctl.rec.restart_at >= world.ctl.rec.restart_issued_at
    world.ctl.canary.join(5.0)                          # the OLD head's completion arrives now
    world.head.release.set()
    world.head.completion_delay_s = 0.0
    for _ in range(3):
        world.tick(advance=5)
    assert world.ctl.rec.in_progress and world.ctl.rec.step == "wait_load"
    assert world.ctl.attempts_total["succeeded"] == 0
    assert world.ctl.proof is None and world.ctl.last_success_at is not None
    # the new head loads: its OWN readiness sequence is the proof
    world.head.mode = "healthy"
    world.drive_recovery_to_ready()
    assert world.ctl.attempts_total["succeeded"] == 1
    assert world.ctl.proof["probe_started_at"] >= world.ctl.rec.restart_at


# ---------------------------------------------------------------------------
# The lock, the budget, the cooldown
# ---------------------------------------------------------------------------


def test_the_lock_prevents_a_second_concurrent_recovery(world):
    world.make_ready()
    world.heal_on_restart()
    holder = os.open(world.cfg.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)   # cluster-up.sh, say, is restarting the pair
    try:
        world.head.mode = "engine_dead"
        world.tick(3)
        assert world.ctl.rec.in_progress is False
        assert world.head_restarts == [] and world.sentinel.restart_calls == []
        assert world.ctl.state == "DOWN" and "lock_held" in world.ctl.reason, world.ctl.reason
        assert world.ctl.rec.step == "confirm" and world.ctl.rec.blocked == "lock_held"
        assert world.cfg.lock_path not in world.ctl.reason
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    world.tick()
    assert world.ctl.rec.in_progress and len(world.head_restarts) == 1
    # while the controller recovers, nobody else can take the lock
    probe = os.open(world.cfg.lock_path, os.O_RDWR)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = False
        except BlockingIOError:
            held = True
        assert held
    finally:
        os.close(probe)
    world.drive_recovery_to_ready()
    # released at MARK_READY
    probe = os.open(world.cfg.lock_path, os.O_RDWR)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(probe, fcntl.LOCK_UN)
    finally:
        os.close(probe)


def test_lock_file_and_incident_files_are_world_writable_and_owned_like_the_mount(world):
    """[major] the controller runs as root; a 0644 root-owned lock made the
    runbook's shell wrappers (engine-lock.sh as techsphere) refuse to run."""
    world.make_ready()
    world.heal_on_restart()
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress
    lock_dir = os.path.dirname(world.cfg.lock_path)
    st = os.stat(world.cfg.lock_path)
    assert st.st_mode & 0o666 == 0o666
    assert (st.st_uid, st.st_gid) == (os.stat(lock_dir).st_uid, os.stat(lock_dir).st_gid)
    inc = os.path.join(world.cfg.incident_dir, world.ctl.rec.incident_id)
    for name in os.listdir(inc):
        fst = os.stat(os.path.join(inc, name))
        assert fst.st_mode & 0o666 == 0o666, name
        assert fst.st_uid == os.stat(world.cfg.incident_dir).st_uid
    assert os.stat(inc).st_mode & 0o777 == 0o777


def test_a_missing_lock_directory_fails_safe_and_is_never_created(world):
    """[minor] a lock created inside the container's own overlay would be one
    nobody on the host contends for: refuse, loudly, and do not makedirs."""
    world.cfg.lock_path = os.path.join(world.tmp, "not-mounted", "engine-recovery.lock")
    world.make_ready()
    world.head.mode = "engine_dead"
    world.tick(3)
    assert world.ctl.rec.in_progress is False and world.head_restarts == []
    assert not os.path.exists(os.path.dirname(world.cfg.lock_path))
    assert world.ctl.rec.blocked == "lock_unavailable"
    assert "bind mount" in world.ctl.rec.blocked_detail
    assert world.ctl.state == "DOWN" and "lock_unavailable" in world.ctl.reason
    assert world.tmp not in world.ctl.reason


def test_the_budget_exhausts_to_down_with_budget_exhausted_and_no_further_restart(world):
    world.cfg.recovery_budget = 2
    world.ctl.budget.limit = 2
    world.cfg.cold_start_budget_s = 100.0
    world.make_ready()
    world.head.mode = "engine_dead"          # and it stays dead after every restart
    for attempt in (1, 2):
        world.tick()                          # confirm → restart
        assert world.ctl.rec.in_progress and world.ctl.rec.attempt == attempt
        assert len(world.head_restarts) == attempt
        world.tick(advance=world.cfg.cold_start_budget_s + 1)   # wait_load times out
        assert world.ctl.rec.in_progress is False
        assert world.ctl.last_failure_category == "cold_start_timeout"
        assert world.ctl.attempts_total["failed"] == attempt
    world.tick()                              # a third confirmation: no budget left
    assert world.ctl.state == "DOWN", world.ctl.reason
    assert "budget" in world.ctl.reason
    assert world.ctl.last_failure_category == "budget_exhausted"
    assert world.ctl.attempts_total["budget_exhausted"] == 1
    world.tick(5, advance=30)
    assert len(world.head_restarts) == 2                      # no further destructive action
    assert world.ctl.rec.in_progress is False
    text = world.ctl.metrics_text()
    assert 'techsara_vllm_last_failure_category{category="budget_exhausted"} 1' in text
    assert "techsara_vllm_restart_budget_remaining 0" in text
    # evidence preserved
    first_incident = world.ctl.incident["id"]
    inc = os.path.join(world.cfg.incident_dir, first_incident)
    assert "head-logs-2.txt" in os.listdir(inc)
    # …and it returns to READY by itself when the readiness sequence passes
    world.head.mode = "healthy"
    world.settle()
    assert world.ctl.state == "READY", world.ctl.reason
    # [major] the incident ends by this path too, and the next failure is a NEW incident
    assert world.ctl.incident["ended_at"] is None
    for _ in range(3):
        world.clock.advance(world.cfg.canary_interval_s + 1)
        world.settle()
    assert world.ctl.incident["ended_at"] is not None
    assert "techsara_vllm_incident_start_timestamp_seconds 0" in world.ctl.metrics_text()
    world.clock.advance(world.cfg.recovery_window_s + 5)      # the budget window slides
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress and world.ctl.incident["id"] != first_incident
    assert world.ctl.incident["attempts"] == 1


def test_incident_ends_after_a_failed_attempt_when_the_engine_heals_itself(world):
    """[major] after _fail_attempt the incident stayed open forever and the
    next unrelated failure days later was filed as attempt N+1 of it."""
    world.cfg.cold_start_budget_s = 100.0
    world.cfg.recovery_cooldown_s = 0.0
    world.make_ready()
    world.head.mode = "engine_dead"
    world.tick()
    first = world.ctl.incident["id"]
    world.tick(advance=world.cfg.cold_start_budget_s + 1)     # cold_start_timeout: attempt 1 failed
    assert world.ctl.attempts_total["failed"] == 1 and world.ctl.rec.in_progress is False
    # the head heals by itself (its own healthcheck, an operator, a late daemon reply)
    world.head.mode = "healthy"
    world.settle()
    assert world.ctl.state == "READY", world.ctl.reason
    for _ in range(3):
        world.clock.advance(world.cfg.canary_interval_s + 1)
        world.settle()
    assert world.ctl.incident["ended_at"] is not None
    assert world.ctl.snapshot()["incident"]["ended_at"] is not None
    # a week later: a NEW incident id, attempt 1, evidence in its own directory
    world.clock.advance(7 * 86400)
    world.tick()
    world.heal_on_restart()
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress
    assert world.ctl.incident["id"] != first and world.ctl.incident["attempts"] == 1
    assert sorted(os.listdir(os.path.join(world.cfg.incident_dir, world.ctl.incident["id"]))) == [
        "head-logs-1.txt", "state-1.json", "worker-diagnostics-1.txt"]


def test_cooldown_blocks_a_new_recovery_until_it_elapses(world):
    world.cfg.recovery_cooldown_s = 120.0
    world.make_ready()
    world.heal_on_restart()
    world.head.mode = "engine_dead"
    world.tick()
    world.drive_recovery_to_ready()
    assert len(world.head_restarts) == 1
    world.head.mode = "engine_dead"
    world.tick(2, advance=5)
    assert world.ctl.state == "DOWN" and "cooldown" in world.ctl.reason, world.ctl.reason
    assert world.ctl.rec.blocked == "cooldown"
    assert len(world.head_restarts) == 1
    world.tick(advance=115)
    assert len(world.head_restarts) == 2 and world.ctl.rec.in_progress


def test_budget_exhausted_with_a_sentinel_only_trigger_and_a_passing_canary_is_degraded_not_down(world):
    """[major] a forged or stale sentinel document must never pin DOWN (and
    the orchestrator's breaker open) over a fresh real completion once the
    budget is spent: the passing canary is the tie-breaker."""
    world.cfg.recovery_budget = 1
    world.ctl.budget.limit = 1
    world.make_ready()
    world.heal_on_restart()
    world.clock.advance(2)
    world.sentinel.last_fault = {"at": world.clock.time(), "signature": "misaligned address"}
    world.tick()
    assert world.ctl.rec.in_progress
    world.drive_recovery_to_ready()
    assert world.ctl.state == "READY"
    # the "sentinel" keeps claiming a fault newer than everything, while completions succeed
    world.clock.advance(2)
    world.sentinel.last_fault = {"at": world.clock.time(), "signature": "misaligned address"}
    world.sentinel.container["started_at"] = world.clock.time() - 3600
    world.tick()
    assert world.ctl.confirmed is not None and world.ctl.rec.blocked == "budget_exhausted"
    assert world.ctl.state == "DEGRADED", world.ctl.reason
    assert "canary passes" in world.ctl.reason and "budget_exhausted" in world.ctl.reason
    assert world.ctl.primary_ready is True and len(world.head_restarts) == 1
    for _ in range(3):
        world.clock.advance(world.cfg.canary_interval_s + 1)
        world.settle()
    assert world.ctl.state == "DEGRADED" and len(world.head_restarts) == 1


# ---------------------------------------------------------------------------
# The head-memory precondition of STOP_STALE_PAIR
# ---------------------------------------------------------------------------

GIB = 1024 ** 3


def _memory_warnings(caplog):
    return [r for r in caplog.records if r.levelname == "WARNING" and "head memory low" in r.message]


def test_low_head_memory_warns_once_and_delays_the_restart_by_exactly_one_tick(world, caplog):
    """A fresh model load reads 21.8 GiB of weights and allocates ~25 GiB;
    on 2026-09-12 07:11 IST a new CUDA context failed with NV_ERR_NO_MEMORY
    at that edge. Below HEAD_MIN_MEM_AVAILABLE_BYTES the controller warns
    with the number and the runbook pointer and holds ONE tick — then
    restarts anyway: a dead engine must still be restarted."""
    caplog.set_level(logging.INFO, logger="controller")
    world.make_ready()
    world.heal_on_restart()
    world.set_mem_available(17.9)                    # what the head had at 22:10Z on 09-11
    world.head.mode = "engine_dead"

    world.tick()                                     # tick 1: confirmed, held, warned
    assert world.ctl.confirmed is not None and world.ctl.confirmed[0] == "head_engine_dead"
    assert world.ctl.rec.in_progress is False
    assert world.ctl.rec.blocked == "head_memory_low"
    assert world.ctl.rec.blocked_detail == "MemAvailable 17.9 GiB < 30 GiB"
    assert world.ctl.state == "DOWN" and "blocked: head_memory_low" in world.ctl.reason, world.ctl.reason
    assert world.head_restarts == [] and world.sentinel.restart_calls == []
    warnings = _memory_warnings(caplog)
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "17.9 GiB" in msg and str(int(17.9 * 1024 * 1024) * 1024) in msg     # the number, twice
    assert "30 GiB" in msg and "RUNBOOK.md §7" in msg and "NV_ERR_NO_MEMORY" in msg
    assert "next tick" in msg
    snap = world.ctl.snapshot()
    mem = snap["signals"]["head_memory"]
    assert mem["available_bytes"] == int(17.9 * 1024 * 1024) * 1024 and mem["low"] is True
    assert mem["observed_at"] == snap["generated_at"]
    assert snap["recovery"]["blocked"] == "head_memory_low"
    assert f"techsara_vllm_head_mem_available_bytes {mem['available_bytes']}" in world.ctl.metrics_text()

    world.tick()                                     # tick 2: still confirmed, still low → proceeds
    assert world.ctl.rec.in_progress and world.ctl.rec.category == "head_engine_dead"
    assert world.ctl.rec.blocked == ""
    assert world.order == ["sentinel_restart", "docker_restart"]
    assert len(_memory_warnings(caplog)) == 1                      # warned once, not again
    assert any("proceeding" in r.message and "dead engine" in r.message for r in caplog.records
               if r.levelname == "INFO")
    world.drive_recovery_to_ready()
    assert world.ctl.state == "READY"

    # a NEW confirmed failure gets its own warning and its own one-tick hold
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress is False and world.ctl.rec.blocked == "head_memory_low"
    assert len(_memory_warnings(caplog)) == 2
    world.tick()
    assert world.ctl.rec.in_progress and len(world.head_restarts) == 2


def test_head_memory_ok_unobserved_or_disabled_never_delays_the_restart(world, caplog):
    # enough memory (the fixture's 60 GiB): the restart is on the confirming tick, no warning
    world.make_ready()
    world.heal_on_restart()
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress and len(world.head_restarts) == 1
    assert _memory_warnings(caplog) == []
    world.drive_recovery_to_ready()

    # unobserved (no /proc/meminfo): the signal is null, the metric is omitted, nothing is held
    world.cfg.meminfo_path = os.path.join(world.tmp, "no-such-meminfo")
    world.tick()
    mem = world.ctl.snapshot()["signals"]["head_memory"]
    assert mem["available_bytes"] is None and mem["low"] is None
    assert "techsara_vllm_head_mem_available_bytes" not in world.ctl.metrics_text()
    assert sum(1 for r in caplog.records if "cannot read MemAvailable" in r.message) == 1
    world.tick(2)
    assert sum(1 for r in caplog.records if "cannot read MemAvailable" in r.message) == 1   # logged once
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress and len(world.head_restarts) == 2
    assert _memory_warnings(caplog) == []
    world.drive_recovery_to_ready()

    # disabled (HEAD_MIN_MEM_AVAILABLE_BYTES=0): low memory is published but never acted on
    world.cfg.meminfo_path = os.path.join(world.tmp, "meminfo")
    world.cfg.head_min_mem_available_bytes = 0
    world.set_mem_available(5.0)
    world.head.mode = "engine_dead"
    world.tick()
    mem = world.ctl.snapshot()["signals"]["head_memory"]
    assert mem["available_bytes"] == 5 * GIB and mem["low"] is None and mem["min_bytes"] == 0
    assert world.ctl.rec.in_progress and len(world.head_restarts) == 3
    assert _memory_warnings(caplog) == []


def test_a_manual_recovery_is_held_one_tick_by_low_head_memory_not_dropped(world, caplog):
    world.make_ready()
    world.heal_on_restart()
    world.set_mem_available(20.0)
    status, _ = world.ctl.request_manual_recovery()
    assert status == 202
    world.tick()
    assert world.ctl.rec.in_progress is False and world.ctl.rec.blocked == "head_memory_low"
    assert world.ctl.snapshot()["recovery"]["manual_pending"] is True      # kept, not dropped
    assert world.head_restarts == [] and len(_memory_warnings(caplog)) == 1
    world.tick()
    assert world.ctl.rec.in_progress and world.ctl.rec.category == "manual"
    assert world.order == ["sentinel_restart", "docker_restart"]
    assert len(_memory_warnings(caplog)) == 1
    world.drive_recovery_to_ready()
    assert world.ctl.state == "READY"


def test_jitter_is_applied_before_acting(world):
    world.cfg.recovery_jitter_s = 5.0
    world.make_ready()
    world.heal_on_restart()
    world.head.mode = "engine_dead"
    before = world.clock.time()
    world.tick()
    assert world.ctl.rec.in_progress
    # the fake clock's sleep advances it: the restart happened after the jitter
    assert 0 <= world.head_restarts[0][2] - before <= 5.0 + 1.0


def test_dry_run_logs_instead_of_restarting(world, caplog):
    world.cfg.dry_run = True
    world.make_ready()
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress
    assert world.head_restarts == [] and world.sentinel.restart_calls == []
    assert world.ctl.rec.worker_restart == "dry_run"
    assert any("DRY_RUN" in r.message for r in caplog.records)


def test_a_failed_head_restart_fails_the_attempt(world):
    world.make_ready()
    world.docker.fail_restart = True
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress is False
    assert world.ctl.attempts_total["failed"] == 1
    assert world.ctl.rec.last["outcome"] == "failed"


def test_a_head_restart_answered_late_by_docker_is_re_inspected_not_failed(world):
    """[minor] the daemon applied the restart but answered after the client's
    timeout: the container's moved started_at says it happened."""
    world.cfg.head_restart_timeout_s = 0
    world.ctl.docker.restart_reply_extra_s = 0.3          # the client waits 0.3 s for the reply
    world.docker.restart_reply_delay_s = 1.0              # the daemon answers after 1 s
    world.make_ready()
    world.heal_on_restart()
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress and world.ctl.rec.step == "wait_load"
    assert world.ctl.attempts_total["failed"] == 0
    assert len(world.head_restarts) == 1
    time.sleep(1.0)                                       # let the fake daemon's late reply finish
    world.drive_recovery_to_ready()
    assert world.ctl.attempts_total == {"started": 1, "succeeded": 1, "failed": 0, "budget_exhausted": 0}


def test_worker_restart_outcome_is_recorded_per_attempt_and_a_refusal_is_visible(world):
    """[minor] a 403 from a token skew used to be swallowed; the head was
    restarted alone and `last` said succeeded."""
    world.make_ready()
    world.heal_on_restart()
    world.sentinel.restart_status = 403
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress
    assert world.ctl.rec.worker_restart == "refused"
    assert [s for s in world.ctl.rec.steps if s["step"] == "stop_pair"][0]["worker"] == "refused"
    assert len(world.head_restarts) == 1               # the head restart still happens
    assert 'techsara_vllm_recovery_worker_restart{outcome="refused"} 1' in world.ctl.metrics_text()
    world.drive_recovery_to_ready()
    assert world.ctl.rec.last["worker_restart"] == "refused"
    world.sentinel.stop()
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.worker_restart == "unreachable"


def test_cold_start_seconds_is_measured_once_per_head_start(world):
    """[minor] a DEGRADED→READY flap hours later must not overwrite the gauge
    with the head's uptime."""
    world.head_container.started_at = world.clock.time() - 20
    world.make_ready()
    measured = world.ctl.cold_start_s
    assert measured is not None
    world.clock.advance(8 * 3600)
    world.sentinel.stop()
    world.settle()
    assert world.ctl.state == "DEGRADED"
    world.sentinel.start()
    world.ctl.sentinel.base_url = world.sentinel.url
    world.settle()
    assert world.ctl.state == "READY"
    assert world.ctl.cold_start_s == measured
    # a real restart measures again
    world.heal_on_restart()
    world.head.mode = "engine_dead"
    world.tick()
    world.drive_recovery_to_ready()
    assert world.ctl.cold_start_s != measured and world.ctl.cold_start_s < 60


def test_state_publishes_bounded_error_kinds_not_exception_strings(world):
    """[minor] worker.error, docker_error and recovery.blocked are bounded
    kinds; socket paths, addresses and exception text stay in the log."""
    world.make_ready()
    world.sentinel.stop()
    world.tick()
    snap = world.ctl.snapshot()
    assert snap["signals"]["worker"]["error"] == "refused"
    world.sentinel.start()
    world.sentinel.required_token = "s3cret"          # the controller has none: 403
    world.ctl.sentinel.base_url = world.sentinel.url
    world.tick()
    assert world.ctl.snapshot()["signals"]["worker"]["error"] == "http_403"
    world.docker.stop()
    world.tick()
    assert world.ctl.docker_error == "socket_missing"
    assert world.cfg.docker_socket not in json.dumps(world.ctl.snapshot())


def test_sentinel_token_is_sent_on_every_call(world):
    from controller import SentinelClient

    world.sentinel.required_token = "s3cret"
    world.ctl.sentinel = SentinelClient(world.sentinel.url, "s3cret", world.clock, world.cfg.probe_timeout_s)
    world.make_ready()
    world.heal_on_restart()
    assert world.ctl.worker["reachable"] is True
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.worker_restart == "ok"
    paths = {c["path"] for c in world.sentinel.get_calls}
    assert {"/state", "/diagnostics"} <= paths
    assert all(c["token"] == "s3cret" for c in world.sentinel.get_calls)
    assert all(c["token"] == "s3cret" for c in world.sentinel.restart_calls)


# ---------------------------------------------------------------------------
# POST /recover
# ---------------------------------------------------------------------------


def test_post_recover_is_refused_from_a_non_loopback_peer_and_accepted_from_127_0_0_1(world, peer_tools):
    connect_from, http_status, serve = peer_tools
    world.make_ready()
    world.heal_on_restart()
    server, port = serve(Handler, "controller", world.ctl)
    try:
        body = b'{"reason": "manual", "category": "manual"}'
        req = (b"POST /recover HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
               b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        raw = connect_from("127.0.0.2", port, req)
        assert http_status(raw) == 403, raw
        raw = connect_from("127.0.0.1", port, req)
        assert http_status(raw) == 202, raw
        reply = json.loads(raw.split(b"\r\n\r\n", 1)[1])
        assert reply["incident"] is not None
        # GET /state and /metrics are open from anywhere
        raw = connect_from("127.0.0.2", port, b"GET /state HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        assert http_status(raw) == 200
        raw = connect_from("127.0.0.2", port, b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        assert http_status(raw) == 200 and raw.endswith(b"ok\n")
        raw = connect_from("127.0.0.2", port, b"POST /state HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        assert http_status(raw) == 404
    finally:
        server.shutdown()
        server.server_close()
    # the accepted request is acted on at the next tick, as category manual
    world.tick()
    assert world.ctl.rec.in_progress and world.ctl.rec.category == "manual"
    assert world.ctl.rec.incident_id == reply["incident"]
    assert world.ctl.last_failure_category == "manual"
    assert world.order == ["sentinel_restart", "docker_restart"]


def test_post_recover_publishes_manual_pending_in_the_snapshot_before_the_next_tick(world):
    """[major] cluster-recover.sh polls /state right after the POST; it must
    never read a pre-request document and call the outcome READY."""
    world.make_ready()
    world.heal_on_restart()
    snap_before = world.ctl.snapshot()
    assert snap_before["recovery"]["manual_pending"] is False and snap_before["recovery"]["step"] == "idle"
    status, reply = world.ctl.request_manual_recovery()
    assert status == 202
    snap = world.ctl.snapshot()
    assert snap["recovery"]["manual_pending"] is True and snap["recovery"]["step"] == "confirm"
    assert snap["recovery"]["in_progress"] is False
    # a second POST before the tick is idempotent
    status2, reply2 = world.ctl.request_manual_recovery()
    assert status2 == 202 and reply2["incident"] == reply["incident"]
    world.tick()
    assert world.ctl.rec.in_progress and world.ctl.rec.incident_id == reply["incident"]
    assert world.ctl.snapshot()["recovery"]["manual_pending"] is False
    assert len(world.head_restarts) == 1


def test_post_recover_while_recovering_is_409(world):
    world.make_ready()
    world.heal_on_restart()
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress
    status, reply = world.ctl.request_manual_recovery()
    assert status == 409 and reply["accepted"] is False


def test_manual_recovery_honours_the_budget_and_the_cooldown(world):
    """[major] POST /recover used to bypass both gates: unbounded pair
    restarts from anything that can reach 127.0.0.1:9838."""
    world.cfg.recovery_budget = 2
    world.ctl.budget.limit = 2
    world.cfg.recovery_cooldown_s = 120.0
    world.make_ready()
    world.heal_on_restart()
    status, _ = world.ctl.request_manual_recovery()
    assert status == 202
    world.tick()
    world.drive_recovery_to_ready()
    assert len(world.head_restarts) == 1
    # cooldown: refused synchronously, nothing queued
    status, reply = world.ctl.request_manual_recovery()
    assert status == 429 and reply["reason"] == "cooldown" and reply["retry_after_s"] > 0
    world.tick(2)
    assert len(world.head_restarts) == 1 and world.ctl.snapshot()["recovery"]["manual_pending"] is False
    world.clock.advance(121)
    status, _ = world.ctl.request_manual_recovery()
    assert status == 202
    world.tick()
    world.drive_recovery_to_ready()
    assert len(world.head_restarts) == 2
    world.clock.advance(121)
    # budget: refused synchronously with the escalation path named
    status, reply = world.ctl.request_manual_recovery()
    assert status == 429 and reply["reason"] == "recovery budget exhausted"
    assert "--force" in reply["note"]
    world.tick(3)
    assert len(world.head_restarts) == 2 and world.ctl.rec.in_progress is False
    assert world.ctl.state == "READY"            # a refused manual request is not a failure
    # the window slides: allowed again
    world.clock.advance(world.cfg.recovery_window_s)
    status, _ = world.ctl.request_manual_recovery()
    assert status == 202


def test_handler_timeout_frees_a_pinned_thread_and_the_peer_is_checked_before_the_body(world, peer_tools):
    """[minor] a peer that announces a body and never sends it used to pin a
    handler thread forever; a non-loopback peer used to be able to do so
    before its 403."""
    connect_from, http_status, serve = peer_tools
    world.make_ready()
    old_timeout = Handler.timeout
    Handler.timeout = 1
    server, port = serve(Handler, "controller", world.ctl)
    try:
        hdr = b"POST /recover HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n"
        # non-loopback: 403 at once, without waiting for the body
        s = socket.socket()
        s.settimeout(3.0)
        s.bind(("127.0.0.2", 0))
        s.connect(("127.0.0.1", port))
        t0 = time.monotonic()
        s.sendall(hdr)
        raw = s.recv(65536)
        assert http_status(raw) == 403 and time.monotonic() - t0 < 0.9
        s.close()
        # loopback: the thread waits for the body only up to Handler.timeout, then closes
        s = socket.socket()
        s.settimeout(3.0)
        s.connect(("127.0.0.1", port))
        t0 = time.monotonic()
        s.sendall(hdr)
        try:
            data = s.recv(65536)
        except socket.timeout:
            data = b"(no close)"
        assert data == b"" and 0.8 <= time.monotonic() - t0 <= 2.5, data
        s.close()
    finally:
        Handler.timeout = old_timeout
        server.shutdown()
        server.server_close()
    assert world.ctl.snapshot()["recovery"]["manual_pending"] is False


# ---------------------------------------------------------------------------
# /metrics — every name of §7.1 v2, bounded labels only
# ---------------------------------------------------------------------------

CONTRACT_METRICS = [
    "techsara_engine_controller_up",
    "techsara_vllm_state_code",
    "techsara_vllm_state",
    "techsara_vllm_state_since_timestamp_seconds",
    "techsara_vllm_primary_ready",
    "techsara_vllm_router_available",
    "techsara_vllm_participation_ok",
    "techsara_vllm_generated_at_seconds",
    "techsara_vllm_synthetic_success",
    "techsara_vllm_synthetic_connect_seconds",
    "techsara_vllm_synthetic_ttft_seconds",
    "techsara_vllm_synthetic_duration_seconds",
    "techsara_vllm_synthetic_tokens",
    "techsara_vllm_synthetic_probes_total",
    "techsara_vllm_last_success_timestamp_seconds",
    "techsara_vllm_consecutive_probe_failures",
    "techsara_vllm_generation_frozen_seconds",
    "techsara_vllm_head_container_running",
    "techsara_vllm_head_engine_process_alive",
    "techsara_vllm_head_api_tcp_up",
    "techsara_vllm_head_health_ok",
    "techsara_vllm_head_metrics_ok",
    "techsara_vllm_worker_reachable",
    "techsara_vllm_worker_container_running",
    "techsara_vllm_worker_rank_alive",
    "techsara_vllm_both_ranks_ok",
    "techsara_vllm_container_restart_count",
    "techsara_vllm_recovery_in_progress",
    "techsara_vllm_recovery_step",
    "techsara_vllm_recovery_attempts_total",
    "techsara_vllm_restart_budget_remaining",
    "techsara_vllm_incident_start_timestamp_seconds",
    "techsara_vllm_last_failure_category",
    "techsara_vllm_cold_start_seconds",
    "techsara_vllm_recovery_duration_seconds",
]
EXTRA_METRICS = ["techsara_vllm_recovery_worker_restart", "techsara_vllm_head_mem_available_bytes"]

ALLOWED_LABELS = {
    "state": set(STATES),
    "step": set(STEPS),
    "outcome": set(PROBE_OUTCOMES) | set(ATTEMPT_OUTCOMES) | set(WORKER_RESTART_OUTCOMES),
    "category": set(FAILURE_CATEGORIES),
    "rank": {"0", "1"},
}

SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{([^}]*)\})? (-?[0-9.e+-]+|NaN)$')


def _parse(text):
    samples = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = SAMPLE.match(line)
        assert m, f"malformed exposition line: {line!r}"
        labels = {}
        if m.group(3):
            for pair in m.group(3).split(","):
                k, v = pair.split("=", 1)
                labels[k] = v.strip('"')
        samples.append((m.group(1), labels, m.group(4)))
    return samples


def test_metrics_render_every_contract_metric_with_bounded_labels_only(world):
    world.make_ready()
    world.heal_on_restart()
    world.head.mode = "engine_dead"
    world.tick()
    world.drive_recovery_to_ready()          # so cold_start / recovery_duration are measured
    text = world.ctl.metrics_text()
    samples = _parse(text)
    names = {s[0] for s in samples}
    missing = [m for m in CONTRACT_METRICS + EXTRA_METRICS if m not in names]
    assert not missing, missing
    assert "techsara_vllm_fallback_available" not in names
    for name, labels, _value in samples:
        for key, value in labels.items():
            assert key in ALLOWED_LABELS, (name, key)
            assert value in ALLOWED_LABELS[key], (name, key, value)
    # one-hots sum to exactly one
    for name in ("techsara_vllm_state", "techsara_vllm_recovery_step", "techsara_vllm_last_failure_category",
                 "techsara_vllm_recovery_worker_restart"):
        assert sum(float(v) for n, _l, v in samples if n == name) == 1.0, name
    # every declared name has exactly one HELP and one TYPE
    for name in CONTRACT_METRICS + EXTRA_METRICS:
        assert text.count(f"# HELP {name} ") == 1 and text.count(f"# TYPE {name} ") == 1
    # the counters carry every outcome, even at zero
    outcomes = {lb["outcome"] for n, lb, _v in samples if n == "techsara_vllm_synthetic_probes_total"}
    assert outcomes == set(PROBE_OUTCOMES)
    outcomes = {lb["outcome"] for n, lb, _v in samples if n == "techsara_vllm_recovery_attempts_total"}
    assert outcomes == set(ATTEMPT_OUTCOMES)
    assert "techsara_vllm_state_code 2" in text
    assert 'techsara_vllm_state{state="READY"} 1' in text
    assert 'techsara_vllm_state{state="QUEUEING"} 0' in text
    assert 'techsara_vllm_container_restart_count{rank="0"} 1' in text
    assert 'techsara_vllm_container_restart_count{rank="1"} 1' in text
    assert "techsara_vllm_both_ranks_ok 1" in text
    assert "techsara_vllm_participation_ok 1" in text
    assert 'techsara_vllm_recovery_worker_restart{outcome="ok"} 1' in text
    gen = [v for n, _l, v in samples if n == "techsara_vllm_generated_at_seconds"][0]
    assert abs(float(gen) - world.ctl.snapshot()["generated_at"]) < 1e-3


def test_metrics_before_any_canary_omit_unmeasured_series_rather_than_lying(world):
    world.docker.stop()
    world.tick()
    text = world.ctl.metrics_text()
    assert "techsara_vllm_synthetic_success" not in text
    assert "techsara_vllm_head_container_running" not in text     # unobservable, not 0
    assert "techsara_vllm_last_success_timestamp_seconds 0" in text
    assert 'techsara_vllm_state{state="MONITORING_UNKNOWN"} 1' in text
    assert "techsara_engine_controller_up 1" in text
    assert "techsara_vllm_participation_ok 0" in text
    assert "techsara_vllm_generated_at_seconds" in text


def test_metrics_endpoint_serves_the_document(world, peer_tools):
    connect_from, http_status, serve = peer_tools
    world.make_ready()
    server, port = serve(Handler, "controller", world.ctl)
    try:
        raw = connect_from("127.0.0.1", port, b"GET /metrics HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        assert http_status(raw) == 200
        assert b"text/plain; version=0.0.4" in raw
        assert b"techsara_vllm_state_code 2" in raw
    finally:
        server.shutdown()
        server.server_close()
