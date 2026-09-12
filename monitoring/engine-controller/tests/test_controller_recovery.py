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

from common import FAILURE_CATEGORIES, utc_stamp
from controller import (
    ATTEMPT_OUTCOMES, EXTERNAL_REPAIR_OUTCOMES, Handler, PROBE_OUTCOMES, STATES, STEPS, WORKER_RESTART_OUTCOMES,
)
from fakes import HEAD_PROCESSES


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
    assert world.ctl.attempts_total == {"started": 1, "succeeded": 1, "failed": 0, "budget_exhausted": 0,
                                        "repaired_worker": 0}
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
# The worker re-pair after an EXTERNAL head start (drill 5, 2026-09-12)
# ---------------------------------------------------------------------------

NO_ATTEMPTS = {"started": 0, "succeeded": 0, "failed": 0, "budget_exhausted": 0, "repaired_worker": 0}


def _head_api_killed_then_docker_restarts_it(world):
    """Drill 5's first ten seconds: ``kill -9`` of the head's ``vllm serve``
    (connection refused, the process table empty of it), one controller tick
    (DETECT head_api_dead 1 of 2, DEGRADED), then Docker's restart policy
    brings the container back 5 s later — started_at moves, restart_count
    +1, nothing listens on :8000 while the new head loads. The controller
    performed none of it."""
    world.head.stop()
    world.head_container.processes = [p for p in world.head_container.processes
                                      if "vllm serve" not in p and "VLLM::" not in p]
    world.tick()
    assert world.ctl.state == "DEGRADED" and world.ctl.rec.in_progress is False, world.ctl.reason
    world.clock.advance(5)
    world.head_container.started_at = world.clock.time()
    world.head_container.restart_count += 1


def test_an_external_head_restart_with_a_stale_worker_re_pairs_the_worker_once_without_a_pair_restart(world, caplog):
    """Drill 5 (2026-09-12 09:55:49Z): the head's API process was killed,
    Docker restarted the container within 5 s, the controller correctly went
    STARTING — and nothing re-paired the worker. Rank 1 stayed in the dead
    head's process group, the new head waited out its 300 s rendezvous and
    exited, Docker restarted it again, and only the worker's last-resort
    healthcheck tier (900 s / 8 misses) restarted the worker: READY 767 s
    after the break instead of ~180 s. v1's worker healthcheck did the
    re-pair on a 5xx; in v2 the controller is the single authority and must
    own it: ONE sentinel restart, no head restart, no budget, no cooldown,
    an incident with its evidence, the STARTING reason saying so."""
    caplog.set_level(logging.INFO, logger="controller")
    world.make_ready()
    assert world.ctl.worker["rank_joined"] is True             # rank 1 joined to the head that is about to die
    cooldown_until = world.clock.time() + 600.0                # a cooldown from an earlier recovery is no bar
    world.ctl.cooldown_until = cooldown_until
    _head_api_killed_then_docker_restarts_it(world)

    world.tick()
    # the worker was restarted through the sentinel; the head was NOT restarted; no recovery ran
    assert world.order == ["sentinel_restart"]
    assert world.head_restarts == [] and world.ctl.rec.in_progress is False and world.ctl.rec.step == "idle"
    assert world.ctl.budget.used() == 0 and world.ctl.cooldown_until == cooldown_until
    assert world.ctl.attempts_total == {**NO_ATTEMPTS, "repaired_worker": 1}
    assert world.ctl.state == "STARTING", world.ctl.reason
    assert world.ctl.reason.startswith("head restarted outside the controller; worker re-paired; "), world.ctl.reason
    assert world.ctl.last_failure_category == "head_restarted_externally"
    inc = world.ctl.incident
    assert inc is not None and inc["category"] == "head_restarted_externally"
    assert inc["ended_at"] is None and inc["attempts"] == 0      # no pair-restart attempt was made
    snap = world.ctl.snapshot()
    rep = snap["recovery"]["external_repair"]
    assert rep["outcome"] == "ok" and rep["incident_id"] == inc["id"] and rep["ended_at"] >= rep["at"]
    assert set(EXTERNAL_REPAIR_OUTCOMES) >= {"ok", "refused", "unreachable", "failed", "dry_run", "lock_held",
                                            "lock_unavailable", "not_needed", "no_evidence"}
    assert set(WORKER_RESTART_OUTCOMES) - {"none", "skipped"} <= set(EXTERNAL_REPAIR_OUTCOMES)
    assert "rank 1 joined" in rep["detail"] and "before the new head" in rep["detail"]
    assert rep["head_started_at"] == world.ctl.head["started_at"]
    assert snap["recovery"]["head_start_origin"] == "external"
    assert snap["incident"]["id"] == inc["id"]
    # the worker sentinel's restart was the choreography's POST /restart
    assert len(world.sentinel.restart_calls) == 1 and world.sentinel.restart_calls[0]["peer"] == "127.0.0.1"
    # evidence captured into the incident dir, like a recovery, named by the head start
    tag = f"repair-{utc_stamp(world.ctl.head['started_at'])}"
    inc_dir = os.path.join(world.cfg.incident_dir, inc["id"])
    assert sorted(os.listdir(inc_dir)) == sorted(
        [f"head-logs-{tag}.txt", f"worker-diagnostics-{tag}.txt", f"state-{tag}.json", f"{tag}.json"])
    assert "worker log line 1" in open(os.path.join(inc_dir, f"worker-diagnostics-{tag}.txt")).read()
    captured = json.load(open(os.path.join(inc_dir, f"state-{tag}.json")))
    assert captured["state"] == "STARTING" and captured["reason"].startswith(
        "head restarted outside the controller; re-pairing the worker")
    assert json.load(open(os.path.join(inc_dir, f"{tag}.json")))["outcome"] == "ok"
    # metrics: the bounded category one-hot, the bounded outcome counter, the budget untouched
    text = world.ctl.metrics_text()
    assert 'techsara_vllm_last_failure_category{category="head_restarted_externally"} 1' in text
    assert 'techsara_vllm_recovery_attempts_total{outcome="repaired_worker"} 1' in text
    assert 'techsara_vllm_recovery_attempts_total{outcome="started"} 0' in text
    assert "techsara_vllm_restart_budget_remaining 3" in text
    assert "techsara_vllm_recovery_in_progress 0" in text
    assert 'techsara_vllm_state{state="STARTING"} 1' in text
    # the log carries the incident id and the reason
    msgs = [r.getMessage() for r in caplog.records]
    assert any("NOT performed by the controller" in m for m in msgs)
    assert any(inc["id"] in m and "head restarted outside the controller" in m for m in msgs)
    assert any(inc["id"] in m and "worker restarted via the sentinel" in m and "budget untouched" in m for m in msgs)
    assert not any("RECOVERING" in m for m in msgs)

    # once per head incarnation: the head loads for a while, nothing else is restarted
    world.tick(3, advance=10)
    assert world.order == ["sentinel_restart"]
    assert world.ctl.state == "STARTING" and "worker re-paired" in world.ctl.reason

    # the new head opens its API, the controller proves the pair, the incident ends by the usual path
    world.head_container.processes = list(HEAD_PROCESSES)
    world.head.start()
    world.clock.advance(world.cfg.canary_interval_fast_s + 1)
    world.settle()
    assert world.ctl.state == "READY", world.ctl.reason
    assert "head restarted outside" not in world.ctl.reason
    for _ in range(3):
        world.clock.advance(world.cfg.canary_interval_s + 1)
        world.settle()
    assert world.ctl.incident["ended_at"] is not None
    assert world.order == ["sentinel_restart"] and world.head_restarts == []
    assert world.ctl.attempts_total == {**NO_ATTEMPTS, "repaired_worker": 1}


def test_a_spent_recovery_budget_does_not_block_the_re_pair(world):
    """The budget bounds PAIR restarts (destructive, a head restart each);
    a worker re-pair after a head start the controller did not perform is
    what makes that external restart succeed, and costs no head restart."""
    world.make_ready()
    for _ in range(world.cfg.recovery_budget):
        world.ctl.budget.record()
    assert world.ctl.budget.exhausted()
    _head_api_killed_then_docker_restarts_it(world)
    world.tick()
    assert world.order == ["sentinel_restart"] and world.head_restarts == []
    assert world.ctl.attempts_total == {**NO_ATTEMPTS, "repaired_worker": 1}
    assert world.ctl.budget.used() == world.cfg.recovery_budget          # untouched by the re-pair
    assert "techsara_vllm_restart_budget_remaining 0" in world.ctl.metrics_text()
    assert world.ctl.snapshot()["recovery"]["external_repair"]["outcome"] == "ok"


def test_the_controllers_own_pair_restart_never_triggers_the_external_re_pair(world):
    """The choreography restarts the worker FIRST and then the head: the head
    start it observes next is its own (rec.restart_issued_at set, the attempt
    in progress) and must not be answered with a second worker restart."""
    world.make_ready()
    world.heal_on_restart()
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress and world.ctl.rec.restart_issued_at is not None
    world.drive_recovery_to_ready()
    assert world.order == ["sentinel_restart", "docker_restart"]
    assert world.ctl.snapshot()["recovery"]["head_start_origin"] == "controller"
    assert world.ctl.snapshot()["recovery"]["external_repair"] is None
    assert world.ctl.attempts_total == {**NO_ATTEMPTS, "started": 1, "succeeded": 1}
    assert world.ctl.last_failure_category == "head_engine_dead"
    for _ in range(3):
        world.clock.advance(world.cfg.canary_interval_s + 1)
        world.settle()
    assert world.order == ["sentinel_restart", "docker_restart"]
    assert world.ctl.state == "READY"


def test_a_head_restart_the_controller_issued_is_its_own_even_when_the_attempt_ended_before_it_was_observed(world):
    """The attempt fails at stop_pair on a daemon error, but the restart had
    been applied (a late reply); the moved started_at seen on the next tick
    is still the controller's own — no re-pair of a worker the choreography
    just restarted."""
    world.make_ready()
    world.heal_on_restart()
    world.docker.restart_reply_delay_s = 0.0
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress
    # the attempt fails right after the head restart was applied
    world.ctl._fail_attempt("cold_start_timeout", "forced by the test", world.clock.time())
    assert world.ctl.rec.in_progress is False
    world.clock.advance(5)
    world.tick()
    assert world.ctl.snapshot()["recovery"]["head_start_origin"] == "controller"
    assert world.order == ["sentinel_restart", "docker_restart"]
    assert world.ctl.attempts_total["repaired_worker"] == 0


def test_a_head_restart_applied_by_the_daemon_after_a_failed_attempt_is_still_the_controllers_own(world):
    """stop_pair failed (the daemon answered 500 and three re-inspects showed
    no change), the attempt ended — and then the daemon applied the restart
    anyway. The moved started_at within OWN_RESTART_WINDOW_S of
    rec.restart_issued_at is the controller's restart: the worker the
    choreography restarted seconds earlier is not restarted again."""
    world.make_ready()
    world.heal_on_restart()
    world.docker.fail_restart = True
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress is False and world.ctl.attempts_total["failed"] == 1
    assert world.ctl.rec.restart_issued_at is not None and world.order == ["sentinel_restart"]
    world.clock.advance(30)
    world.head_container.started_at = world.clock.time()      # applied late by the daemon
    world.head_container.restart_count += 1
    world.head.mode = "healthy"
    world.tick()
    assert world.ctl.snapshot()["recovery"]["head_start_origin"] == "controller"
    assert world.ctl.snapshot()["recovery"]["external_repair"] is None
    assert world.order == ["sentinel_restart"] and world.ctl.attempts_total["repaired_worker"] == 0


def test_a_worker_that_already_restarted_after_the_external_head_start_is_left_alone(world):
    """The worker's own restart policy (or its last-resort tier, or an
    operator) re-created it after the new head started: it is waiting at the
    right rendezvous, and restarting it again would only delay the pair."""
    world.make_ready()
    _head_api_killed_then_docker_restarts_it(world)
    world.clock.advance(2)
    world.sentinel.container["started_at"] = world.clock.time()          # newer than the head's
    world.sentinel.container["restart_count"] += 1
    world.sentinel.rank_joined = False                                   # VLLM::Worker, waiting
    world.tick(3, advance=5)
    assert world.order == [] and world.sentinel.restart_calls == []
    assert world.ctl.rec.in_progress is False and world.ctl.attempts_total == NO_ATTEMPTS
    assert world.ctl.state == "STARTING", world.ctl.reason
    assert world.ctl.reason.startswith("head restarted outside the controller; worker already re-paired; ")
    rep = world.ctl.snapshot()["recovery"]["external_repair"]
    assert rep["outcome"] == "not_needed" and rep["incident_id"] is None
    assert world.ctl.incident is None and world.ctl.last_failure_category == "none"


def test_an_external_head_restart_whose_pair_is_already_up_is_not_re_paired(world):
    """A head start observed late (the Docker socket was unobservable while
    it happened) with /health already 200: rank 1 joined, whatever the
    timestamps say."""
    world.make_ready()
    world.heal_on_restart(start_api=True)
    world.docker.stop()
    world.tick()
    assert world.ctl.state == "DEGRADED" and world.ctl.docker_ok is False
    world.clock.advance(5)
    world.head_container.started_at = world.clock.time()
    world.head_container.restart_count += 1
    world.docker.start()
    world.clock.advance(200)
    world.tick()
    assert world.ctl.snapshot()["recovery"]["head_start_origin"] == "external"
    assert world.sentinel.restart_calls == [] and world.order == []
    assert world.ctl.snapshot()["recovery"]["external_repair"]["outcome"] == "not_needed"
    assert world.ctl.attempts_total == NO_ATTEMPTS


def test_the_external_re_pair_is_skipped_and_logged_while_another_actor_holds_the_lock(world, caplog):
    """cluster-up.sh / cluster-recover.sh hold the engine lock across a pair
    start: the head start they cause is theirs, and so is the worker — the
    controller stands aside once, says so, and never comes back for it when
    the lock frees."""
    caplog.set_level(logging.INFO, logger="controller")
    world.make_ready()
    holder = os.open(world.cfg.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        _head_api_killed_then_docker_restarts_it(world)
        world.tick()
        assert world.sentinel.restart_calls == [] and world.order == []
        assert world.ctl.rec.in_progress is False and world.ctl.attempts_total == NO_ATTEMPTS
        assert world.ctl.state == "STARTING", world.ctl.reason
        assert world.ctl.reason.startswith(
            "head restarted outside the controller; worker re-pair skipped (lock held by another actor); ")
        rep = world.ctl.snapshot()["recovery"]["external_repair"]
        assert rep["outcome"] == "lock_held" and rep["incident_id"] is None
        assert world.ctl.incident is None and world.ctl.last_failure_category == "none"
        msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("another actor holds the recovery lock" in m and "worker re-pair skipped" in m for m in msgs)
        assert world.cfg.lock_path not in world.ctl.reason
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    # the lock frees: still nothing for THIS head start (once per incarnation)
    world.tick(3, advance=10)
    assert world.sentinel.restart_calls == [] and world.order == []
    # the controller's OWN lock is released after a re-pair: the next external start can take it
    world.clock.advance(5)
    world.head_container.started_at = world.clock.time()
    world.head_container.restart_count += 1
    world.tick()
    assert world.order == ["sentinel_restart"]
    probe = os.open(world.cfg.lock_path, os.O_RDWR)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(probe, fcntl.LOCK_UN)
    finally:
        os.close(probe)


def test_single_node_mode_never_re_pairs_after_an_external_head_restart(world):
    """No sentinel, no rank 1: an external head restart is a cold start to
    prove, nothing more."""
    world.cfg.sentinel_url = ""
    world.ctl.sentinel.base_url = ""
    world.ctl.worker["configured"] = False
    world.cfg.worker_gpu_exporter_url = ""
    world.head_container.processes = [p for p in world.head_container.processes if "Worker_TP" not in p]
    world.make_ready()
    _head_api_killed_then_docker_restarts_it(world)
    world.tick(3, advance=5)
    assert world.sentinel.restart_calls == [] and world.order == []
    assert world.ctl.state == "STARTING", world.ctl.reason
    assert "head restarted outside" not in world.ctl.reason
    assert world.ctl.snapshot()["recovery"]["external_repair"] is None
    assert world.ctl.attempts_total == NO_ATTEMPTS and world.ctl.incident is None
    assert "techsara_vllm_worker_reachable" not in world.ctl.metrics_text()


def test_the_re_pair_waits_for_a_reachable_sentinel_and_uses_the_workers_age_when_the_rank_was_never_seen(world):
    """The sentinel was down for the whole previous incarnation (rank never
    reported joined or alive) and is down at the tick the head start is
    observed: nothing is decided until it answers, and then the worker's age
    against the new head's is evidence enough on its own."""
    world.sentinel.stop()
    world.settle()                                        # proven while the sentinel was unreachable
    assert world.ctl.proof is not None and world.ctl.worker["rank_joined"] is None
    assert world.ctl.state == "DEGRADED" and world.ctl._worker_rank_seen_alive is False
    _head_api_killed_then_docker_restarts_it(world)
    world.tick(2, advance=5)
    assert world.ctl.snapshot()["recovery"]["external_repair"] is None     # undecided: nobody to ask
    world.sentinel.start()
    world.ctl.sentinel.base_url = world.sentinel.url
    world.tick(advance=5)
    assert world.order == ["sentinel_restart"]
    rep = world.ctl.snapshot()["recovery"]["external_repair"]
    assert rep["outcome"] == "ok" and "before the new head" in rep["detail"] and "joined" not in rep["detail"]
    assert world.ctl.attempts_total == {**NO_ATTEMPTS, "repaired_worker": 1}


def test_a_refused_re_pair_is_logged_once_and_not_retried_for_that_head_start(world, caplog):
    """A token mismatch (403) is the operator's to fix (`techsara up`); a
    second POST could only restart a worker that has just come back."""
    caplog.set_level(logging.INFO, logger="controller")
    world.make_ready()
    world.sentinel.restart_status = 403
    _head_api_killed_then_docker_restarts_it(world)
    world.tick(3, advance=5)
    assert len(world.sentinel.restart_calls) == 1 and world.order == []
    rep = world.ctl.snapshot()["recovery"]["external_repair"]
    assert rep["outcome"] == "refused" and rep["incident_id"] == world.ctl.incident["id"]
    assert world.ctl.attempts_total["repaired_worker"] == 0
    assert world.ctl.last_failure_category == "head_restarted_externally"
    assert world.ctl.reason.startswith("head restarted outside the controller; worker re-pair refused; ")
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("worker re-pair refused" in m and "last-resort healthcheck tier" in m for m in msgs)
    assert 'techsara_vllm_recovery_attempts_total{outcome="repaired_worker"} 0' in world.ctl.metrics_text()


def test_dry_run_logs_the_re_pair_instead_of_restarting_the_worker(world, caplog):
    caplog.set_level(logging.INFO, logger="controller")
    world.cfg.dry_run = True
    world.make_ready()
    _head_api_killed_then_docker_restarts_it(world)
    world.tick()
    assert world.sentinel.restart_calls == [] and world.order == []
    assert world.ctl.snapshot()["recovery"]["external_repair"]["outcome"] == "dry_run"
    assert world.ctl.attempts_total["repaired_worker"] == 0
    assert any("DRY_RUN: would POST" in r.getMessage() and "worker re-pair" in r.getMessage() for r in caplog.records)


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


def test_a_single_huge_prefill_with_flat_token_counters_is_saturation_not_a_wedge(world):
    """2026-09-12 09:02–09:16Z, candidate B, the ~950K needle: vLLM counts a
    prompt's tokens only when its prefill FINISHES, so both token counters sat
    flat for 12 minutes with one request running while the scheduler stepped
    through the chunks and the KV usage rose 0.09 → 0.56; the canary starved
    behind it. The controller confirmed WEDGED at 09:04:15Z (the restart was
    refused only by the budget). With the step counter and the KV usage as
    progress witnesses the engine is DEGRADED (saturated), never restarted."""
    world.cfg.canary_timeout_s = 300.0
    world.make_ready()
    world.heal_on_restart()
    world.head.mode = "wedged"             # completions never answer: the canary starves
    world.head.running = 1.0
    world.head.progress_per_scrape = 0      # both token counters flat …
    world.head.iterations = 1000.0          # … but the scheduler steps every scrape
    world.head.iterations_per_scrape = 3
    world.head.kv_per_scrape = 0.004        # … and the KV usage climbs chunk by chunk
    world.tick()
    world.clock.advance(world.cfg.canary_interval_s + 1)
    world.tick()                            # the canary starts and hangs
    assert world.ctl.canary.in_flight
    for _ in range(8):                      # 13+ minutes of a starved canary on a stepping engine
        world.clock.advance(100)
        world.tick()
    assert world.ctl.engine["frozen_seconds"] < world.cfg.frozen_s, "the witnesses moved: not frozen"
    assert "wedged_frozen_tokens" not in world.ctl.pending
    assert world.ctl.rec.in_progress is False
    assert "WEDGED" not in [tr["to"] for tr in world.ctl.transitions]
    assert "RECOVERING" not in [tr["to"] for tr in world.ctl.transitions]
    assert world.order == []
    assert "saturation" in (world.ctl._saturation_note or "") or world.ctl.state in ("DEGRADED", "BUSY", "READY")


def test_a_build_without_the_step_counter_still_detects_a_true_wedge(world):
    """A head whose /metrics lacks the step/KV series (older builds) keeps the
    token-counter rule: flat counters with requests running and a starved
    canary is still a wedge."""
    world.cfg.canary_timeout_s = 300.0
    world.make_ready()
    world.heal_on_restart()
    world.head.mode = "wedged"
    world.head.running = 9.0
    world.head.iterations = None
    world.tick()
    world.clock.advance(world.cfg.canary_interval_s + 1)
    world.tick()
    world.clock.advance(95)
    world.tick()
    world.tick()
    assert [tr["to"] for tr in world.ctl.transitions][-2:] == ["WEDGED", "RECOVERING"]
    world.head.release.set()
    world.drive_recovery_to_ready()


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
    # …and STILL not a wedge past the starvation ceiling: a progressing engine
    # is never restarted (2026-09-12: one ~950K prefill held the canary for
    # 13 minutes while every progress witness moved). Past the ceiling the
    # reason only says so.
    world.clock.advance(world.cfg.canary_starvation_s)
    world.ctl.tick()
    world.ctl.canary.join(3.0)
    world.ctl.tick()
    assert world.ctl.rec.in_progress is False and world.head_restarts == []
    assert "large prefill" in (world.ctl._saturation_note or ""), world.ctl._saturation_note
    world.head.release.set()


# ---------------------------------------------------------------------------
# Trigger 6: three consecutive canary HTTP errors (round-2 [major] :1590)
# ---------------------------------------------------------------------------


def _probe_once(world, advance=None):
    """Advance past the canary interval, start a probe, let it finish, harvest."""
    world.clock.advance(world.cfg.canary_interval_fast_s + 1 if advance is None else advance)
    world.ctl.tick()
    world.ctl.canary.join(5.0)
    world.ctl.tick()


def test_three_consecutive_canary_http_errors_on_a_proven_engine_confirm_canary_http_error(world):
    """[major] round 2, controller.py:1590 — a proven engine whose every
    completion fails fast while /health, /v1/models and /metrics answer 200
    used to sit in DEGRADED 'awaiting confirmation' forever (a SERVING state
    to the orchestrator). Three consecutive HTTP errors are the three
    observations; the saturation exemption does not apply."""
    world.make_ready()
    world.heal_on_restart()
    world.head.completion_status = 500
    # nine requests "running" and the counters moving: saturation would
    # exempt a TIMEOUT, never an error — an error is an answer
    world.head.running = 9.0
    world.head.progress_per_scrape = 500.0
    world.clock.advance(world.cfg.canary_interval_s + 1)
    world.ctl.tick()
    world.ctl.canary.join(5.0)
    world.ctl.tick()                                    # error 1 → DEGRADED, awaiting confirmation
    assert world.ctl.consecutive_http_errors == 1 and world.ctl.rec.in_progress is False
    assert world.ctl.state == "DEGRADED" and "awaiting confirmation" in world.ctl.reason, world.ctl.reason
    assert "WEDGED as canary_http_error in" in world.ctl.reason
    assert world.ctl.snapshot()["signals"]["canary"]["consecutive_http_errors"] == 1
    _probe_once(world)                                  # error 2, at the FAST interval
    assert world.ctl.consecutive_http_errors == 2 and world.ctl.rec.in_progress is False
    _probe_once(world)                                  # error 3 → confirmed
    assert world.ctl.probes_total["http_error"] == 3
    assert world.ctl.rec.in_progress and world.ctl.rec.category == "canary_http_error", world.ctl.reason
    assert "3 consecutive canary HTTP errors" in world.ctl.rec.reason and "HTTP 500" in world.ctl.rec.reason
    assert [t["to"] for t in world.ctl.transitions][-2:] == ["WEDGED", "RECOVERING"]
    assert world.ctl.last_failure_category == "canary_http_error"
    assert world.order == ["sentinel_restart", "docker_restart"]
    world.head.progress_per_scrape = 0.0
    world.drive_recovery_to_ready()
    assert world.ctl.state == "READY" and world.ctl.consecutive_http_errors == 0
    assert 'techsara_vllm_last_failure_category{category="canary_http_error"} 1' in world.ctl.metrics_text()


def test_the_dying_stream_shapes_count_as_canary_http_errors_and_a_4xx_does_not(world):
    """Rule 6 counts what the ENGINE answered: an error chunk in a 200
    stream, a stream that ends without a terminal chunk, a 5xx. A 4xx is a
    request the API rejected — a restart cannot fix it, and calling it a
    wedge would queue users on a serving engine — so it never counts and
    the DEGRADED reason says why."""
    world.make_ready()
    world.heal_on_restart()
    world.head.error_chunk_in_stream = True
    world.clock.advance(world.cfg.canary_interval_s + 1)
    world.ctl.tick()
    world.ctl.canary.join(5.0)
    world.ctl.tick()
    assert world.ctl.last_canary.detail == "error chunk in stream" and world.ctl.consecutive_http_errors == 1
    world.head.error_chunk_in_stream = False
    world.head.truncate_stream = True
    _probe_once(world)
    assert world.ctl.last_canary.detail == "stream ended without a terminal chunk"
    assert world.ctl.consecutive_http_errors == 2 and world.ctl.rec.in_progress is False
    world.head.truncate_stream = False
    world.head.completion_status = 503
    _probe_once(world)
    assert world.ctl.rec.in_progress and world.ctl.rec.category == "canary_http_error"
    world.drive_recovery_to_ready()
    # a 4xx: not the engine's fault, never confirmed, said so
    world.head.completion_status = 400
    for _ in range(4):
        _probe_once(world)
    assert world.ctl.probes_total["http_error"] >= 7
    assert world.ctl.consecutive_http_errors == 0 and world.ctl.consecutive_failures == 4
    assert world.ctl.rec.in_progress is False and len(world.head_restarts) == 1
    assert world.ctl.state == "DEGRADED", world.ctl.reason
    assert "HTTP 400: not counted as an engine fault" in world.ctl.reason
    assert "canary_http_error" not in world.ctl.pending


# ---------------------------------------------------------------------------
# Rule 7: DEGRADED with a failing canary is bounded (round-2 correctness :2247)
# ---------------------------------------------------------------------------


def test_degraded_with_a_failing_canary_is_bounded_and_becomes_wedged_per_the_failure_kind(world):
    """[contract] round 2 — DEGRADED is 'the canary still succeeds'; a proven
    engine failing every canary for CANARY_FAIL_DEGRADED_MAX_S with /health
    200 is WEDGED (canary_timeout or canary_http_error by the last failure's
    kind). Timeouts alternating with errors confirm neither rule 5 nor 6 —
    this is the bound that catches the mix."""
    world.cfg.canary_timeout_s = 0.5
    world.cfg.canary_fail_degraded_max_s = 120.0
    world.make_ready()
    world.heal_on_restart()
    world.head.wedged_hold_s = 1.5
    kinds = []                                          # outcome of every probe that actually ran

    def probes_run():
        return world.ctl.probes_total["http_error"] + world.ctl.probes_total["timeout"]

    for i in range(20):
        if i % 2 == 0:
            world.head.mode = "healthy"
            world.head.completion_status = 500
        else:
            world.head.completion_status = 200
            world.head.mode = "wedged"                  # /health still 200; only completions hang
        before = probes_run()
        _probe_once(world, advance=(world.cfg.canary_interval_s + 1) if i == 0 else None)
        world.head.release.set()
        world.head.release = __import__("threading").Event()
        if probes_run() > before:
            kinds.append("timeout" if i % 2 else "http_error")
        if world.ctl.rec.in_progress:
            break                                       # the bound fires on the first tick past it, before a new probe
        assert world.ctl.last_canary.outcome == kinds[-1]
        assert world.ctl.consecutive_timeouts <= 1 and world.ctl.consecutive_http_errors <= 1
        assert world.ctl.state == "DEGRADED", world.ctl.reason
    assert world.ctl.rec.in_progress, (kinds, world.ctl.reason)
    assert set(kinds) == {"http_error", "timeout"} and len(kinds) >= 10
    expected = "canary_timeout" if kinds[-1] == "timeout" else "canary_http_error"
    assert world.ctl.rec.category == expected, (kinds, world.ctl.rec.reason)
    assert f"last: {kinds[-1]}" in world.ctl.rec.reason
    assert "every canary failed for" in world.ctl.rec.reason and "/health answers 200" in world.ctl.rec.reason
    assert [t["to"] for t in world.ctl.transitions][-2:] == ["WEDGED", "RECOVERING"]
    # it took the bound, not fewer probes
    assert world.ctl.probes_total["http_error"] + world.ctl.probes_total["timeout"] >= 10
    world.head.mode = "healthy"
    world.head.completion_status = 200
    world.drive_recovery_to_ready()
    assert world.ctl.failing_since is None


def test_the_degraded_bound_keeps_the_saturation_exemption_for_timeouts(world):
    """Rule 7 with the last failure a TIMEOUT on a progressing engine
    (counters moving, requests running) stays exempt until
    CANARY_STARVATION_S, exactly like rule 5: a starved canary on a busy
    scheduler is not a wedge."""
    world.cfg.canary_timeout_s = 0.5
    world.cfg.canary_fail_degraded_max_s = 60.0
    world.cfg.canary_starvation_s = 300.0
    world.make_ready()
    world.heal_on_restart()
    world.head.mode = "wedged"
    world.head.wedged_hold_s = 1.5
    world.head.running = 9.0
    world.head.progress_per_scrape = 500.0
    for i in range(8):                                  # ~90 s of consecutive timeouts, engine progressing
        _probe_once(world, advance=(world.cfg.canary_interval_s + 1) if i == 0 else None)
        world.head.release.set()
        world.head.release = __import__("threading").Event()
    assert world.ctl.failing_since is not None
    assert world.clock.time() - world.ctl.failing_since >= world.cfg.canary_fail_degraded_max_s
    assert world.ctl.rec.in_progress is False and world.head_restarts == []
    assert world.ctl.state == "DEGRADED" and "saturation" in world.ctl.reason, world.ctl.reason
    # past the starvation ceiling the same streak is STILL saturation while the
    # engine progresses: neither rule 5 nor rule 7 restarts a working engine
    world.clock.advance(world.cfg.canary_starvation_s)
    world.ctl.tick()
    assert world.ctl.rec.in_progress is False and world.head_restarts == []
    world.head.release.set()


# ---------------------------------------------------------------------------
# Trigger 1(c): the seen-alive gate (round-2 [minor] :1530)
# ---------------------------------------------------------------------------


def test_worker_rank_absent_is_not_a_trigger_until_the_sentinel_has_seen_the_rank_alive_for_this_head_start(world):
    """[minor] round 2, controller.py:1530 — the pair proven while the
    sentinel was unreachable, then a sentinel that comes up on a build whose
    worker title it does not recognise reports rank_process_alive:false: a
    name never observed alive for this head start is unknown, not absent,
    and must not restart a healthy pair. Seen alive then gone still does."""
    world.sentinel.stop()
    world.settle()
    assert world.ctl.proof is not None and world.ctl.state == "DEGRADED" and "sentinel" in world.ctl.reason
    world.sentinel.start()
    world.ctl.sentinel.base_url = world.sentinel.url
    world.sentinel.rank_process_alive = False           # never seen alive for this head start
    world.sentinel.container["started_at"] = world.clock.time() - 3600
    for _ in range(4):
        world.clock.advance(world.cfg.worker_rank_grace_s)
        world.settle()
    assert world.ctl.worker["rank_process_alive"] is False and world.ctl.worker["reachable"] is True
    assert "worker_rank_dead" not in world.ctl.pending
    assert world.ctl.rec.in_progress is False and world.head_restarts == [] and world.sentinel.restart_calls == []
    assert world.ctl.state == "DEGRADED" and "never seen for this head start" in world.ctl.reason, world.ctl.reason
    # the sentinel sees the rank once: from now on its absence past the grace counts
    world.heal_on_restart()
    world.sentinel.rank_process_alive = True
    world.settle()
    assert world.ctl.state == "READY", world.ctl.reason
    world.sentinel.rank_process_alive = False
    world.clock.advance(world.cfg.worker_rank_grace_s + 1)
    world.tick()                                        # one observation: confirmed, restarted
    assert world.ctl.rec.in_progress and world.ctl.rec.category == "worker_rank_dead"
    assert "seen alive for this head start" in world.ctl.rec.reason
    assert world.order == ["sentinel_restart", "docker_restart"]
    # a restarted head is a new incarnation: the gate closes again until the rank is seen
    world.sentinel.rank_process_alive = False           # the fake's restart said True; override before the next look
    world.tick()                                        # the new started_at is observed here
    assert world.ctl._worker_rank_seen_alive is False
    world.sentinel.rank_process_alive = True
    world.drive_recovery_to_ready()
    assert world.ctl._worker_rank_seen_alive is True


# ---------------------------------------------------------------------------
# Trigger 1(a): one clock, never two (round-2 [minor] :1525)
# ---------------------------------------------------------------------------


def test_trigger_1a_uses_the_sentinels_own_clock_so_node_2_skew_never_confirms_a_recovered_pair(world):
    """[minor] round 2, controller.py:1525 — with Node 2's clock 10 minutes
    ahead, the worker's started_at (Node 2) landed after the readiness
    proof (Node 1) of every recovery: RECOVERING again right after
    MARK_READY, three times, budget_exhausted on a healthy pair. The
    sentinel now reports started_ago_s (a duration on its own clock) and
    the controller compares it with its own time since the last proof."""
    world.sentinel.clock_skew_s = 600.0                 # Node 2 ten minutes AHEAD
    world.make_ready()
    world.heal_on_restart()
    assert world.ctl.worker["clock_skew_s"] is not None and 595 < world.ctl.worker["clock_skew_s"] < 605
    assert world.ctl.worker["started_ago_s"] is not None and 3590 < world.ctl.worker["started_ago_s"] < 3700
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress and world.ctl.rec.category == "head_engine_dead"
    world.drive_recovery_to_ready()
    assert world.ctl.state == "READY"
    # the sentinel's started_at on the wire IS newer than the proof (two clocks) …
    assert world.ctl.worker["started_at"] > world.ctl.last_success_at
    # … and yet the pair is left alone: on one clock the worker started BEFORE the proof
    for _ in range(4):
        world.clock.advance(world.cfg.canary_interval_s + 1)
        world.settle()
    assert "worker_rank_dead" not in world.ctl.pending
    assert len(world.head_restarts) == 1 and world.ctl.attempts_total["started"] == 1
    assert world.ctl.state == "READY", world.ctl.reason

    # Node 2 ten minutes BEHIND: a real worker restart right after a canary
    # success used to be missed by (a); on one clock it is caught at once
    world.sentinel.clock_skew_s = -600.0
    world.clock.advance(world.cfg.canary_interval_s + 1)
    world.settle()
    assert world.ctl.state == "READY"
    world.clock.advance(5)
    world.sentinel.container["started_at"] = world.clock.time()     # restarted 5 s after the last proof
    world.tick()
    assert world.ctl.rec.in_progress and world.ctl.rec.category == "worker_rank_dead"
    assert "sentinel clock" in world.ctl.rec.reason and "after the last proven completion" in world.ctl.rec.reason
    world.drive_recovery_to_ready()


def test_trigger_1a_falls_back_to_the_two_clock_comparison_for_a_sentinel_without_started_ago_s(world):
    world.sentinel.report_started_ago = False           # an older sentinel
    world.make_ready()
    world.heal_on_restart()
    assert world.ctl.worker["started_ago_s"] is None
    world.clock.advance(5)
    world.sentinel.container["started_at"] = world.clock.time()
    world.tick()
    assert world.ctl.rec.in_progress and world.ctl.rec.category == "worker_rank_dead"
    assert "two clocks" in world.ctl.rec.reason
    world.drive_recovery_to_ready()
    world.tick(3)
    assert "worker_rank_dead" not in world.ctl.pending


# ---------------------------------------------------------------------------
# The snapshot stays fresh through a blocking choreography (round-2 [minor] :2394)
# ---------------------------------------------------------------------------


def test_generated_at_keeps_advancing_while_a_choreography_call_blocks(world):
    """[minor] round 2, controller.py:2394 — generated_at was frozen for the
    whole sentinel POST /restart (up to 125 s) and docker restart (up to
    70 s), so §7.3's 30 s freshness rule derived MONITORING_UNKNOWN in the
    middle of a recovery the controller was actively running."""
    import threading

    world.cfg.choreography_heartbeat_s = 0.05
    world.sentinel.restart_delay_s = 1.0                # real seconds inside STOP_STALE_PAIR
    world.make_ready()
    world.heal_on_restart()
    world.head.mode = "engine_dead"
    samples = []
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            snap = world.ctl.snapshot()
            samples.append((snap["recovery"]["step"], snap["generated_at"], snap["state"]))
            time.sleep(0.02)

    t = threading.Thread(target=sample, daemon=True)
    t.start()
    world.tick()                                        # confirm → capture → stop_pair (blocks 1 s) → wait_load
    stop.set()
    t.join(2.0)
    assert world.ctl.rec.in_progress and world.ctl.rec.step == "wait_load"
    assert world.ctl.heartbeat_active is False          # armed only for the blocking region
    stamps = [g for step, g, _ in samples if step == "stop_pair"]
    assert len(stamps) >= 5, samples
    assert max(stamps) - min(stamps) >= 0.5, (min(stamps), max(stamps))
    assert all(state == "RECOVERING" for step, _, state in samples if step in ("capture", "stop_pair"))
    # the steps are stamped when they begin, not with the tick's start time
    steps = {s["step"]: s["at"] for s in world.ctl.rec.steps}
    assert steps["wait_load"] - steps["stop_pair"] >= 0.9
    # once the tick thread is out of the choreography nothing re-stamps the snapshot on its own
    before = world.ctl.snapshot()["generated_at"]
    time.sleep(0.3)
    assert world.ctl.snapshot()["generated_at"] == before
    world.drive_recovery_to_ready()
    assert world.ctl.heartbeat_active is False


# ---------------------------------------------------------------------------
# Docker API errors are said out loud (round-2 [minor] :1164)
# ---------------------------------------------------------------------------


def _docker_warnings(caplog):
    return [r for r in caplog.records if r.levelname == "WARNING" and "docker API unusable" in r.message]


def test_docker_api_errors_are_logged_at_warning_once_per_kind(world, caplog):
    """[minor] round 2, controller.py:1164 — a 400 ('client version 1.53 is
    too new') or 500 from the daemon was logged at DEBUG only; the
    controller sat in MONITORING_UNKNOWN for good with docker_error as the
    only clue."""
    caplog.set_level(logging.INFO, logger="controller")
    world.make_ready()
    world.docker.inspect_status = 400
    world.tick(3)
    assert world.ctl.docker_ok is False and world.ctl.docker_error == "api_400"
    warnings = _docker_warnings(caplog)
    assert len(warnings) == 1, [r.message for r in warnings]
    msg = warnings[0].getMessage()
    assert "api_400" in msg and "DOCKER_API_VERSION=1.53" in msg and "no recovery can start" in msg
    assert world.ctl.state == "DEGRADED"                # a fresh completion: not UNKNOWN
    world.clock.advance(2 * world.cfg.canary_interval_s + 1)
    world.tick()
    assert world.ctl.state == "MONITORING_UNKNOWN", world.ctl.reason
    assert len(_docker_warnings(caplog)) == 1           # still once
    world.docker.inspect_status = None
    world.tick()
    assert world.ctl.docker_ok is True
    assert any(r.levelname == "INFO" and "docker API answering again (was api_400)" in r.message
               for r in caplog.records)
    world.docker.inspect_status = 500
    world.tick(2)
    assert len(_docker_warnings(caplog)) == 2 and "api_500" in _docker_warnings(caplog)[1].getMessage()
    world.docker.stop()
    world.tick(2)
    assert len(_docker_warnings(caplog)) == 3 and "socket_missing" in _docker_warnings(caplog)[2].getMessage()
    assert world.cfg.docker_socket not in json.dumps(world.ctl.snapshot())


# ---------------------------------------------------------------------------
# An exception inside a recovery never pins RECOVERING (round-2 [minor] :1983)
# ---------------------------------------------------------------------------


def _lock_is_free(path: str) -> bool:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return True
        except BlockingIOError:
            return False
    finally:
        os.close(fd)


def test_an_exception_inside_the_choreography_fails_the_attempt_and_releases_the_lock_with_a_traceback(
        world, caplog, monkeypatch):
    """[minor] round 2, controller.py:1983 — an unexpected exception between
    the lock and wait_load was swallowed by run_periodically and left
    rec.in_progress True with the flock held: RECOVERING forever, every
    request queued, cluster-recover.sh unable to take the lock."""
    caplog.set_level(logging.INFO, logger="controller")
    world.make_ready()
    world.heal_on_restart()
    original = world.ctl._capture_diagnostics

    def boom():
        raise RuntimeError("a snapshot value that is not JSON-serialisable")

    monkeypatch.setattr(world.ctl, "_capture_diagnostics", boom)
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress is False
    assert _lock_is_free(world.cfg.lock_path)
    assert world.ctl.attempts_total == {"started": 1, "succeeded": 0, "failed": 1, "budget_exhausted": 0,
                                        "repaired_worker": 0}
    assert world.ctl.rec.last["outcome"] == "failed" and world.ctl.rec.last["step"] == "capture"
    assert world.ctl.rec.last["detail"] == "controller error during capture: RuntimeError"
    assert "not JSON-serialisable" not in json.dumps(world.ctl.snapshot())    # the class, never the text
    errors = [r for r in caplog.records if r.levelname == "ERROR" and "controller error during step capture" in r.message]
    assert len(errors) == 1 and errors[0].exc_info is not None
    assert "Traceback" in caplog.text and "RuntimeError: a snapshot value" in caplog.text
    assert world.head_restarts == [] and world.sentinel.restart_calls == []
    assert world.ctl.state != "RECOVERING", world.ctl.reason
    assert world.ctl.heartbeat_active is False
    # the next confirmation (cooldown 0 here) is attempt 2 and, with the bug gone, succeeds
    monkeypatch.setattr(world.ctl, "_capture_diagnostics", original)
    world.tick()
    assert world.ctl.rec.in_progress and world.ctl.rec.attempt == 2 and len(world.head_restarts) == 1
    world.drive_recovery_to_ready()
    assert world.ctl.attempts_total["succeeded"] == 1


def test_an_exception_while_advancing_a_recovery_fails_the_attempt_too(world, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="controller")
    world.make_ready()
    world.heal_on_restart()
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress and world.ctl.rec.step == "wait_load"
    assert world.ctl.rec.deadline_mono is not None

    def boom():
        raise KeyError("started_at")

    monkeypatch.setattr(world.ctl, "_worker_fault_after_head_start", boom)
    world.tick()
    assert world.ctl.rec.in_progress is False and _lock_is_free(world.cfg.lock_path)
    assert world.ctl.rec.last["detail"] == "controller error during wait_load: KeyError"
    assert any(r.exc_info is not None and "controller error during step wait_load" in r.message
               for r in caplog.records)


def test_a_recovery_in_progress_always_has_a_deadline(world):
    """The deadline is set at confirmation and re-set from the restart at
    wait_load, so no step can be held open without one."""
    world.make_ready()
    world.head.mode = "engine_dead"
    seen = []
    original_step = world.ctl._step

    def spy(step):
        seen.append((step, world.ctl.rec.deadline_mono is not None))
        original_step(step)

    world.ctl._step = spy
    world.tick()
    assert seen == [("capture", True), ("stop_pair", True), ("wait_load", True)]


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
    assert world.ctl.attempts_total == {"started": 1, "succeeded": 1, "failed": 0, "budget_exhausted": 0,
                                        "repaired_worker": 0}


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
