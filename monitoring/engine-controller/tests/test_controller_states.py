"""The state machine (contract §2) and the readiness sequence (§5 v2): which
of the nine states the controller publishes for which combination of
signals, without any recovery; what READY requires; and the exact shape of
``/state``."""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

from controller import STATES


def test_starting_vs_down_depends_on_head_started_at_and_cold_start_budget(world):
    # API not listening, container up: young → STARTING; past the budget → DOWN.
    world.head.stop()
    world.head_container.started_at = world.clock.time() - 100
    world.tick()
    assert world.ctl.state == "STARTING", world.ctl.reason
    assert world.ctl.primary_ready is False
    assert world.ctl.last_failure_category == "none"

    world.tick(advance=world.cfg.cold_start_budget_s - 150)   # 750 s under observation
    assert world.ctl.state == "STARTING", world.ctl.reason
    world.tick(advance=151)                                     # 901 s under observation
    assert world.ctl.state == "DOWN", world.ctl.reason
    assert "cold start" in world.ctl.reason
    assert world.ctl.last_failure_category == "cold_start_timeout"
    assert world.ctl.cold_start_detail == "load"
    # nothing was restarted: STARTING/DOWN by age is not a recovery trigger
    assert world.head_restarts == []
    assert world.ctl.rec.in_progress is False


def test_cold_start_budget_counts_from_when_the_controller_first_saw_the_head(world):
    # A controller (re)deployed next to an hour-old, silent head: STARTING,
    # not DOWN, until the budget has elapsed under OUR observation.
    world.head.stop()
    world.head_container.started_at = world.clock.time() - 3600
    world.tick()
    assert world.ctl.state == "STARTING", world.ctl.reason
    world.tick(advance=world.cfg.cold_start_budget_s + 1)
    assert world.ctl.state == "DOWN", world.ctl.reason


def test_cold_start_timeout_with_a_compile_error_signature_names_the_kernel_cache_runbook_step(world):
    """Contract §6.6: a compile-error signature in the head log during a
    cold-start timeout → detail 'compile' and the reason suggests
    scripts/cluster-recover.sh --clear-kernel-cache."""
    world.head.stop()
    world.head_container.log("INFO Using nvcc from /usr/local/cuda/bin/nvcc")   # benign mention: not a signature
    world.head_container.log("ERROR torch._dynamo.exc.BackendCompilerFailed: inductor raised CalledProcessError")
    world.tick()
    world.tick(advance=world.cfg.cold_start_budget_s + 1)
    assert world.ctl.state == "DOWN", world.ctl.reason
    assert world.ctl.cold_start_detail == "compile"
    assert "--clear-kernel-cache" in world.ctl.reason
    assert world.ctl.snapshot()["cold_start_detail"] == "compile"
    # the log was scanned once for this start, not on every tick
    world.head_container.logs.clear()
    world.tick()
    assert world.ctl.cold_start_detail == "compile"


def test_monitoring_unknown_when_the_docker_socket_is_gone(world):
    world.docker.stop()
    world.tick()
    assert world.ctl.state == "MONITORING_UNKNOWN", world.ctl.reason
    assert "docker" in world.ctl.reason
    snap = world.ctl.snapshot()
    assert snap["state_code"] == 0
    assert snap["signals"]["head_container"]["docker_ok"] is False
    # the reason carries a bounded kind, never the socket path
    assert "socket_missing" in world.ctl.reason and world.cfg.docker_socket not in world.ctl.reason
    # never DOWN from missing data alone, and never a restart
    assert all(t["to"] != "DOWN" for t in world.ctl.transitions)


def test_monitoring_unknown_on_an_api_connect_timeout_but_not_on_refused(world):
    """Coverage gap: a connect that times out (backlog full, no route) is
    'cannot observe'; a refused connect is an observation."""
    world.cfg.probe_timeout_s = 0.3
    world.cfg.head_api_url = "http://10.255.255.1:9"     # blackhole: the connect times out
    world.tick()
    assert world.ctl.api["tcp"] is False and world.ctl.api["tcp_error"] in ("timeout", "unreachable")
    assert world.ctl.state == "MONITORING_UNKNOWN", world.ctl.reason
    assert world.head_restarts == []
    world.cfg.head_api_url = world.head.url
    world.head.stop()
    world.tick()
    assert world.ctl.api["tcp_error"] == "refused"
    assert world.ctl.state == "STARTING", world.ctl.reason   # refused, unproven, young: loading


def test_monitoring_unknown_is_not_used_when_the_canary_has_just_proven_inference(world):
    world.make_ready()
    world.docker.stop()
    world.tick()
    # inference is proven by a real completion moments ago: telemetry is
    # partial, so DEGRADED, not UNKNOWN and not DOWN
    assert world.ctl.state == "DEGRADED", world.ctl.reason
    assert "docker" in world.ctl.reason


# ---------------------------------------------------------------------------
# The readiness sequence (§5 v2)
# ---------------------------------------------------------------------------


def _shapes(world):
    """(stream, max_tokens, ignore_eos) of every completion the head saw."""
    return [(bool(r.get("stream")), int(r.get("max_tokens") or 0), bool(r.get("ignore_eos"))) for r in world.head.requests]


def test_ready_only_after_the_readiness_sequence_in_order(world):
    # every HTTP probe answers 200 before any completion has been proven
    world.tick()
    assert world.ctl.api["health"] == 200 and world.ctl.api["models"] == 200 and world.ctl.api["metrics"] == 200
    assert world.ctl.state == "STARTING", world.ctl.reason
    assert world.ctl.primary_ready is False
    assert world.ctl.canary.kind == "readiness"
    world.ctl.canary.join(10.0)
    world.tick()
    assert world.ctl.state == "READY", world.ctl.reason
    assert world.ctl.primary_ready is True
    # the order: non-stream 32 → stream 32 → (token progress) → TWO participation 256s at once
    assert _shapes(world) == [(False, 32, True), (True, 32, True), (True, 256, True), (True, 256, True)]
    snap = world.ctl.snapshot()
    rd = snap["readiness"]
    assert rd["non_stream_ok"] is True and rd["stream_ok"] is True and rd["progress_ok"] is True
    assert rd["participation"] == "ok" and rd["rank_alive"] is True and rd["passed"] is True
    assert rd["tokens_expected"] == 64 and rd["tokens_delta"] >= 64
    assert [p["probe"] for p in rd["participation_probes"]] == [1, 2]
    assert all(p["ok"] and p["tokens"] == 256 and p["total_s"] > 0 for p in rd["participation_probes"])
    assert rd["proven_since_head_start"] is True
    gpus = snap["signals"]["gpus"]
    assert gpus["head_util"] == 93.0 and gpus["worker_util"] == 91.0 and gpus["sampled_at"] is not None
    assert world.head_gpu.scrapes >= 2 and world.worker_gpu.scrapes >= 2
    can = snap["signals"]["canary"]
    assert can["ok"] is True and can["terminal"] is True and can["tokens"] == 32 and can["kind"] == "readiness"
    assert can["health_at_start"] == 200
    # the fixture's head started an hour before the controller: not its cold start to measure
    assert world.ctl.cold_start_s is None
    assert "techsara_vllm_cold_start_seconds" not in world.ctl.metrics_text()
    assert "techsara_vllm_participation_ok 1" in world.ctl.metrics_text()

    # once proven, the routine canary is step 2 alone with the exact §5 body
    world.clock.advance(world.cfg.canary_interval_s + 1)
    world.settle()
    req = world.head.last_request
    assert req["max_tokens"] == 4 and req["temperature"] == 0 and req["seed"] == 7 and req["stream"] is True
    assert req["chat_template_kwargs"] == {"enable_thinking": False}
    assert req["messages"] == [{"role": "user", "content": "Reply with the single word: ok"}]
    assert req["model"] == world.head.model_id
    assert "ignore_eos" not in req
    assert world.ctl.last_canary.kind == "routine" and world.ctl.last_canary.tokens == 1
    assert world.ctl.state == "READY"


def test_cold_start_is_measured_only_for_a_head_start_the_controller_watched(world):
    world.head_container.started_at = world.clock.time() - 20     # started just before the controller
    world.settle()
    assert world.ctl.state == "READY"
    assert world.ctl.cold_start_s is not None and 20 <= world.ctl.cold_start_s < 60
    assert "techsara_vllm_cold_start_seconds" in world.ctl.metrics_text()


def test_readiness_gate_non_stream_failure_blocks_ready(world):
    world.head.completion_status = 500
    world.settle()
    assert world.ctl.state == "STARTING", world.ctl.reason
    rd = world.ctl.readiness
    assert rd.failed_step == "non_stream" and rd.non_stream_ok is False and rd.stream_ok is None
    assert _shapes(world) == [(False, 32, True)]                  # stopped at step 1
    assert world.ctl.last_canary.outcome == "http_error"
    assert world.ctl.snapshot()["signals"]["canary"]["error_category"] == "canary_http_error"
    assert "readiness sequence failed at non_stream" in world.ctl.reason


def test_readiness_gate_stream_failure_blocks_ready_and_an_unproven_timeout_is_not_counted(world):
    world.cfg.canary_timeout_s = 0.5
    world.head.wedged_hold_s = 1.0
    world.head.hang_streams = True                # non-stream answers, the stream hangs
    world.settle()
    world.head.release.set()
    rd = world.ctl.readiness
    assert rd is not None and rd.non_stream_ok is True and rd.stream_ok is False
    assert rd.failed_step == "stream" and rd.progress_ok is None
    assert _shapes(world) == [(False, 32, True), (True, 32, True)]
    assert world.ctl.state == "STARTING", world.ctl.reason
    assert world.ctl.last_canary.outcome == "timeout"
    # a timeout on an UNPROVEN engine never counts toward canary_timeout
    assert world.ctl.consecutive_timeouts == 0 and world.ctl.consecutive_failures == 1


def test_an_error_chunk_in_the_stream_is_a_canary_http_error_not_a_success(world):
    """Coverage gap: the dying-stream shape — 200, then an error chunk
    before any token (EngineDeadError mid-stream) — on the routine canary
    and on readiness step 2."""
    world.make_ready()
    world.head.error_chunk_in_stream = True
    world.clock.advance(world.cfg.canary_interval_s + 1)
    world.settle()
    res = world.ctl.last_canary
    assert res.kind == "routine" and res.ok is False and res.outcome == "http_error"
    assert res.http_status == 200 and res.detail == "error chunk in stream" and res.tokens == 0
    assert world.ctl.consecutive_failures == 1 and world.ctl.consecutive_timeouts == 0
    assert world.ctl.state == "DEGRADED" and "awaiting confirmation" in world.ctl.reason
    world.ctl.proof = None                                 # force the sequence: step 2 sees the same shape
    world.clock.advance(world.cfg.canary_interval_fast_s + 1)
    world.settle()
    rd = world.ctl.readiness
    assert rd.non_stream_ok is True and rd.stream_ok is False and rd.failed_step == "stream"
    assert world.ctl.last_canary.detail == "readiness stream: error chunk in stream"


def test_zero_token_completions_never_pass_the_readiness_sequence_or_the_routine_canary(world):
    """[major] round 2, controller.py:725 — a finish_reason on empty text
    (or a stream carrying only [DONE]) passed every readiness step: expected
    became 0 and step 3 degenerated to delta >= 0, so a head that emits
    nothing was READY and the orchestrator resumed every queued generation
    against it. A completion proves nothing without a token."""
    world.head.short_by = 32                            # 32 asked, 0 generated: the terminal chunk on empty text
    world.settle()
    rd = world.ctl.readiness
    assert rd.failed_step == "non_stream" and rd.non_stream_ok is False and rd.stream_ok is None
    assert world.ctl.last_canary.detail == "readiness non_stream: no tokens"
    assert world.ctl.last_canary.tokens == 0 and world.ctl.last_canary.terminal is True
    assert world.ctl.last_canary.outcome == "http_error" and world.ctl.last_canary.http_status == 200
    assert world.ctl.state == "STARTING" and world.ctl.proof is None, world.ctl.reason
    assert rd.tokens_expected == 0 and rd.progress_ok is None
    # step 3 expects at least the tokens received, and at least one: a
    # single token from each of steps 1 and 2 is enough, zero never is
    world.head.short_by = 31
    world.clock.advance(world.cfg.canary_interval_fast_s + 1)
    world.settle()
    assert world.ctl.state == "READY", world.ctl.reason
    rd = world.ctl.readiness
    assert rd.tokens_expected == 2 and rd.tokens_delta >= 2 and rd.progress_ok is True
    # the routine canary: a terminal chunk with no content is a FAILURE, not a warning
    world.head.short_by = 0
    world.head.empty_completions = True
    world.clock.advance(world.cfg.canary_interval_s + 1)
    world.settle()
    res = world.ctl.last_canary
    assert res.kind == "routine" and res.ok is False and res.detail == "no tokens" and res.tokens == 0
    assert world.ctl.consecutive_failures == 1 and world.ctl.consecutive_http_errors == 1
    assert world.ctl.state == "DEGRADED" and "no tokens" in world.ctl.reason, world.ctl.reason
    assert world.ctl.snapshot()["signals"]["canary"]["ok"] is False
    assert "techsara_vllm_synthetic_success 0" in world.ctl.metrics_text()
    assert "techsara_vllm_synthetic_tokens 0" in world.ctl.metrics_text()
    # …and it counts toward rule 6 like any other error the engine answered
    world.heal_on_restart()
    for _ in range(2):
        world.clock.advance(world.cfg.canary_interval_fast_s + 1)
        world.settle()
    assert world.ctl.rec.in_progress and world.ctl.rec.category == "canary_http_error"


def test_a_stream_without_a_terminal_chunk_is_not_a_success(world):
    world.make_ready()
    world.head.truncate_stream = True
    world.clock.advance(world.cfg.canary_interval_s + 1)
    world.settle()
    res = world.ctl.last_canary
    assert res.ok is False and res.outcome == "http_error" and res.terminal is False and res.tokens == 1
    assert res.detail == "stream ended without a terminal chunk"


def test_readiness_gate_token_progress_blocks_ready(world):
    world.head.freeze_counters_for_completions = True
    world.settle()
    rd = world.ctl.readiness
    assert rd.non_stream_ok and rd.stream_ok and rd.progress_ok is False
    assert rd.failed_step == "progress" and rd.tokens_expected == 64 and rd.tokens_delta == 0
    assert world.ctl.state == "STARTING", world.ctl.reason
    assert "progress" in world.ctl.reason
    # the completions themselves succeeded: they count as successes, not failures
    assert world.ctl.consecutive_failures == 0 and world.ctl.last_success_at is not None
    assert world.ctl.primary_ready is False
    # …and the sequence passes as soon as the counters move again
    world.head.freeze_counters_for_completions = False
    world.clock.advance(world.cfg.canary_interval_fast_s + 1)
    world.settle()
    assert world.ctl.state == "READY", world.ctl.reason


def _participation_spans(world):
    """(start, end) on the real monotonic clock of every 256-token completion."""
    return sorted((s, e) for req, s, e in world.head.spans if int(req.get("max_tokens") or 0) == 256)


def test_participation_probes_are_two_concurrent_requests_started_together(world):
    """Contract §6.6: the candidate build compiles its multi-sequence GDN
    prefill kernel at the first step that batches ≥ 2 prefills, so step 4
    must put two prefills in front of the engine AT ONCE — two sockets,
    both started before either is awaited — and record both."""
    world.head.completion_delay_s = 0.3          # real seconds before the first byte of every completion
    t0 = time.monotonic()
    world.settle()
    elapsed = time.monotonic() - t0
    assert world.ctl.state == "READY", world.ctl.reason
    spans = _participation_spans(world)
    assert len(spans) == 2
    # both were in flight at the same moment: the later start precedes the earlier end
    assert max(s for s, _ in spans) < min(e for _, e in spans), spans
    assert world.head.max_in_flight >= 2
    # …and the step cost one delay, not two (sequential would be ≥ 0.6 s for the pair alone)
    assert elapsed < 0.3 * 3 + 1.5, elapsed
    rd = world.ctl.readiness
    probes = rd.participation_probes
    assert [p["probe"] for p in probes] == [1, 2]
    for p in probes:
        assert p["ok"] is True and p["outcome"] == "ok" and p["tokens"] == 256 and p["terminal"] is True
        assert p["ttft_s"] is not None and p["ttft_s"] >= 0.25          # the delay is before the first byte
        assert p["total_s"] >= p["ttft_s"] and p["http_status"] == 200
    # the evidence reaches /state and survives JSON
    doc = json.loads(json.dumps(world.ctl.snapshot()))["readiness"]
    assert len(doc["participation_probes"]) == 2 and doc["participation_probes"][1]["ttft_s"] >= 0.25
    # the GPU maxima were sampled across both
    assert world.ctl.snapshot()["signals"]["gpus"] == {"head_util": 93.0, "worker_util": 91.0,
                                                       "sampled_at": rd.gpus["sampled_at"]}


def test_participation_requires_both_probes_to_complete(world):
    """One of the two 256-token completions fails (a 500, whichever the
    engine picks up first): the step fails, READY is withheld, the passing
    one is recorded beside it, and the next sequence passes."""
    world.head.fail_once_when = lambda req: int(req.get("max_tokens") or 0) == 256
    world.settle()
    assert world.ctl.state == "STARTING", world.ctl.reason
    rd = world.ctl.readiness
    assert rd.non_stream_ok and rd.stream_ok and rd.progress_ok
    assert rd.failed_step == "participation" and rd.passed is False
    assert rd.participation == "pending"                           # no GPU verdict on an incomplete step
    assert "1/2 completed; both must" in rd.detail and "http_error" in rd.detail
    assert "readiness sequence failed at participation" in world.ctl.reason
    # both were sent, both recorded: exactly one ok, one 500
    assert _shapes(world)[-2:] == [(True, 256, True), (True, 256, True)]
    assert sorted(p["ok"] for p in rd.participation_probes) == [False, True]
    bad = next(p for p in rd.participation_probes if not p["ok"])
    assert bad["outcome"] == "http_error" and bad["http_status"] == 500 and bad["tokens"] == 0
    good = next(p for p in rd.participation_probes if p["ok"])
    assert good["tokens"] == 256 and good["total_s"] > 0
    assert world.ctl.last_canary.outcome == "http_error" and world.ctl.primary_ready is False
    assert world.head.fail_once_when is None                       # the fake fired exactly once
    # the sequence is re-run at the fast interval and passes once both complete
    world.clock.advance(world.cfg.canary_interval_fast_s + 1)
    world.settle()
    assert world.ctl.state == "READY", world.ctl.reason
    assert all(p["ok"] for p in world.ctl.readiness.participation_probes)


def test_readiness_gate_participation_failed_blocks_ready_and_records_the_maxima(world):
    """Both exporters answer; the worker GPU never reaches 30 %: 'failed'."""
    world.worker_gpu.util = 3.0
    world.settle()
    rd = world.ctl.readiness
    assert rd.participation == "failed" and rd.failed_step == "participation" and rd.passed is False
    assert world.ctl.state == "STARTING", world.ctl.reason
    assert "worker max 3%" in world.ctl.reason and "30%" in world.ctl.reason
    assert world.ctl.snapshot()["signals"]["gpus"] == {"head_util": 93.0, "worker_util": 3.0,
                                                       "sampled_at": rd.gpus["sampled_at"]}
    assert "techsara_vllm_participation_ok 0" in world.ctl.metrics_text()
    world.worker_gpu.util = 88.0
    world.clock.advance(world.cfg.canary_interval_fast_s + 1)
    world.settle()
    assert world.ctl.state == "READY", world.ctl.reason


def test_readiness_participation_unobserved_when_an_exporter_is_unreachable_is_degraded_not_a_block(world):
    world.worker_gpu.stop()
    world.settle()
    rd = world.ctl.readiness
    assert rd.participation == "unobserved" and rd.passed is True
    assert world.ctl.primary_ready is True
    assert world.ctl.state == "DEGRADED", world.ctl.reason
    assert "participation unobserved" in world.ctl.reason and "worker" in world.ctl.reason
    assert "TP=2" in world.ctl.reason           # says why READY was not withheld
    assert world.ctl.snapshot()["signals"]["gpus"]["worker_util"] is None
    assert world.head_restarts == [] and world.ctl.rec.in_progress is False
    assert "techsara_vllm_participation_ok 0" in world.ctl.metrics_text()
    # an exporter that answers without the series (nvidia-smi did not) is unobserved too
    world.worker_gpu.start()
    world.worker_gpu.no_reading = True
    world.clock.advance(world.cfg.canary_interval_s + 1)
    world.ctl.proof = None       # force the sequence again
    world.settle()
    assert world.ctl.readiness.participation == "unobserved"


def test_readiness_participation_skipped_when_no_exporter_is_configured_on_a_single_node(world):
    world.cfg.head_gpu_exporter_url = ""
    world.cfg.worker_gpu_exporter_url = ""
    world.cfg.sentinel_url = ""
    world.ctl.sentinel.base_url = ""
    world.ctl.worker["configured"] = False
    world.settle()
    rd = world.ctl.readiness
    assert rd.participation == "skipped" and rd.passed is True
    assert world.ctl.state == "READY", world.ctl.reason
    assert world.head_gpu.scrapes == 0 and world.worker_gpu.scrapes == 0
    # the two concurrent completions still run: the kernel warm-up is theirs, not the exporters'
    assert _shapes(world)[-2:] == [(True, 256, True), (True, 256, True)]
    assert len(rd.participation_probes) == 2 and all(p["ok"] for p in rd.participation_probes)
    assert rd.gpus["sampled_at"] is None


def test_readiness_participation_is_unobserved_in_cluster_mode_without_a_worker_exporter(world):
    """Cluster mode with only the head exporter: one GPU seen is not 'both'."""
    world.cfg.worker_gpu_exporter_url = ""
    world.settle()
    rd = world.ctl.readiness
    assert rd.participation == "unobserved" and rd.passed is True
    assert "not configured" in rd.detail
    assert world.ctl.state == "DEGRADED" and "participation unobserved" in world.ctl.reason
    assert world.ctl.snapshot()["signals"]["gpus"]["head_util"] == 93.0
    assert world.head_gpu.scrapes >= 1 and world.worker_gpu.scrapes == 0
    world.cfg.head_gpu_exporter_url = ""
    world.ctl.proof = None
    world.clock.advance(world.cfg.canary_interval_s + 1)
    world.settle()
    assert world.ctl.readiness.participation == "unobserved" and world.ctl.state == "DEGRADED"


def test_readiness_gate_rank_alive_holds_ready_until_the_sentinel_sees_the_rank(world):
    world.sentinel.rank_process_alive = False
    world.settle()
    rd = world.ctl.readiness
    assert rd.non_stream_ok and rd.stream_ok and rd.progress_ok and rd.participation == "ok"
    assert rd.rank_alive is False and rd.passed is False and rd.failed_step == "rank_alive"
    assert world.ctl.state == "STARTING", world.ctl.reason
    assert world.ctl.proof is None
    world.sentinel.rank_process_alive = True
    world.clock.advance(world.cfg.canary_interval_fast_s + 1)
    world.settle()
    assert world.ctl.state == "READY", world.ctl.reason


def test_readiness_sequence_runs_at_the_fast_interval_until_proven_then_the_routine_canary_at_the_slow_one(world):
    world.head.freeze_counters_for_completions = True
    world.settle()
    assert world.ctl.state == "STARTING"
    n = len(world.head.requests)
    world.clock.advance(world.cfg.canary_interval_fast_s + 1)
    world.settle()
    assert len(world.head.requests) > n                # another sequence ran at the fast interval
    world.head.freeze_counters_for_completions = False
    world.clock.advance(world.cfg.canary_interval_fast_s + 1)
    world.settle()
    assert world.ctl.state == "READY"
    # the probe after the passing sequence was scheduled while STARTING (fast);
    # from the first ROUTINE probe on, the slow interval applies
    world.clock.advance(world.cfg.canary_interval_fast_s + 1)
    world.settle()
    assert world.ctl.last_canary.kind == "routine"
    n = len(world.head.requests)
    world.clock.advance(world.cfg.canary_interval_fast_s + 1)
    world.settle()
    assert len(world.head.requests) == n                # slow interval now: nothing yet
    world.clock.advance(world.cfg.canary_interval_s)
    world.settle()
    assert len(world.head.requests) == n + 1 and world.ctl.last_canary.kind == "routine"


# ---------------------------------------------------------------------------
# READY / BUSY / DEGRADED
# ---------------------------------------------------------------------------


def test_busy_when_requests_are_running(world):
    world.head.running = 3.0
    world.settle()
    assert world.ctl.state == "BUSY", world.ctl.reason
    assert world.ctl.snapshot()["state_code"] == 3
    world.head.running = 0.0
    world.tick()
    assert world.ctl.state == "READY"


def test_degraded_when_the_sentinel_is_unreachable_and_the_canary_is_ok(world):
    world.make_ready()
    world.sentinel.stop()
    world.tick()
    assert world.ctl.state == "DEGRADED", world.ctl.reason
    assert "sentinel" in world.ctl.reason
    assert world.ctl.primary_ready is True
    snap = world.ctl.snapshot()
    assert snap["signals"]["worker"]["reachable"] is False
    # a bounded kind, not an exception string with the sentinel's address
    assert snap["signals"]["worker"]["error"] == "refused"
    assert str(world.sentinel.port) not in world.ctl.reason
    assert world.head_restarts == []


def test_degraded_when_the_canary_ttft_is_slow(world):
    world.make_ready()
    world.ctl.last_canary.ttft_s = 12.0
    world.tick()
    assert world.ctl.state == "DEGRADED" and "TTFT" in world.ctl.reason


def test_degraded_when_the_configured_router_is_unhealthy_never_triggers_recovery(world):
    """v2: the router is an internal classifier; its health is a DEGRADED
    signal, never a serving path and never a recovery trigger."""
    world.cfg.router_health_url = "http://127.0.0.1:1/health"   # nothing listens
    world.ctl.router["configured"] = True
    world.settle()
    assert world.ctl.state == "DEGRADED" and "router" in world.ctl.reason and "classifier" in world.ctl.reason
    assert world.ctl.primary_ready is True
    assert world.ctl.router_available is False
    snap = world.ctl.snapshot()
    assert snap["router_available"] is False and "fallback_available" not in snap
    assert snap["signals"]["router"]["configured"] is True and "fallback" not in snap["signals"]
    world.tick(3)
    assert world.head_restarts == [] and world.ctl.rec.in_progress is False
    text = world.ctl.metrics_text()
    assert "techsara_vllm_router_available 0" in text and "fallback" not in text


def test_ready_lapses_when_the_canary_success_is_older_than_two_intervals(world):
    world.make_ready()
    world.clock.advance(2 * world.cfg.canary_interval_s + 1)
    # a probe starts at this tick (interval elapsed) and is still in flight
    world.head.mode = "wedged"
    world.tick()
    assert world.ctl.primary_ready is False
    assert world.ctl.state == "DEGRADED", world.ctl.reason
    assert "awaiting confirmation" in world.ctl.reason
    world.head.release.set()


# ---------------------------------------------------------------------------
# Single node (TP=1) — the review blocker
# ---------------------------------------------------------------------------


def _single_node(world):
    world.cfg.sentinel_url = ""
    world.ctl.sentinel.base_url = ""
    world.ctl.worker["configured"] = False
    world.cfg.worker_gpu_exporter_url = ""
    # the router's real process table: no VLLM::Worker_TP* at all
    world.head_container.processes = [p for p in world.head_container.processes if "Worker_TP" not in p]


def test_single_node_tp1_engine_without_worker_tp_process_is_ready_and_never_restarted(world):
    """[blocker] a process name never observed alive for this head incarnation
    is no evidence of death: a TP=1 engine (the ``uni`` executor) has no
    VLLM::Worker_TP process and must be READY, not DEGRADED, and never restarted."""
    _single_node(world)
    world.make_ready()
    assert world.ctl.state == "READY", world.ctl.reason
    assert world.ctl.head["rank_process_alive"] is None          # never seen: unknown, not absent
    assert world.ctl.head["engine_process_alive"] is True
    for _ in range(6):
        world.clock.advance(world.cfg.canary_interval_s + 1)
        world.settle()
    assert world.ctl.state == "READY", world.ctl.reason
    assert world.head_restarts == [] and world.ctl.rec.in_progress is False
    assert world.ctl.pending == {}
    assert world.ctl.attempts_total["started"] == 0
    text = world.ctl.metrics_text()
    assert "techsara_vllm_worker_reachable" not in text
    assert "techsara_vllm_both_ranks_ok 1" in text
    assert "techsara_vllm_head_engine_process_alive 1" in text


def test_a_head_rank_process_seen_alive_then_vanished_is_a_trigger_on_a_tp2_head(world):
    """The 07:05Z case still detects: Worker_TP0 was SEEN for this incarnation and vanished."""
    world.make_ready()
    world.heal_on_restart()
    assert world.ctl.head["rank_process_alive"] is True
    world.head_container.processes = [p for p in world.head_container.processes if "Worker_TP" not in p]
    world.tick()
    assert world.ctl.head["rank_process_alive"] is False
    assert world.ctl.rec.in_progress is False and world.ctl.pending["head_engine_dead:process"].count == 1
    world.tick()
    assert world.ctl.rec.in_progress and world.ctl.rec.category == "head_engine_dead"


def test_a_restarted_head_forgets_which_processes_it_had_seen(world):
    """After a restart the incarnation is new: a Worker_TP process that
    never appears in the new one is unknown again, not vanished."""
    world.make_ready()
    world.heal_on_restart()
    world.head.mode = "engine_dead"
    world.tick()
    assert world.ctl.rec.in_progress
    # the NEW container is a TP=1 shape from here on
    world.head_container.processes = [p for p in world.head_container.processes if "Worker_TP" not in p]
    world.drive_recovery_to_ready()
    assert world.ctl.state == "READY"
    assert world.ctl.head["rank_process_alive"] is None
    world.tick(3)
    assert "head_engine_dead:process" not in world.ctl.pending


# ---------------------------------------------------------------------------
# /state
# ---------------------------------------------------------------------------


def test_state_document_shape(world):
    world.make_ready()
    snap = json.loads(json.dumps(world.ctl.snapshot()))
    assert snap["schema"] == 1
    for key in ("generated_at", "state", "state_code", "reason", "since", "primary_ready", "router_available",
                "incident", "readiness", "signals", "recovery", "cold_start_detail"):
        assert key in snap
    assert "fallback_available" not in snap
    sig = snap["signals"]
    for key in ("head_container", "worker", "api", "engine", "canary", "router", "gpus", "head_memory"):
        assert key in sig
    assert "fallback" not in sig
    assert set(("available_bytes", "observed_at", "min_bytes", "low")) <= set(sig["head_memory"])
    assert sig["head_memory"]["available_bytes"] == 60 * 1024 ** 3 and sig["head_memory"]["low"] is False
    assert sig["head_memory"]["min_bytes"] == 30 * 1024 ** 3
    assert set(("running", "health", "restart_count", "started_at", "engine_process_alive", "observed_at")) <= set(sig["head_container"])
    assert set(("reachable", "container_running", "rank_process_alive", "restart_count", "started_at", "last_fault", "observed_at",
                "started_ago_s", "clock_skew_s")) <= set(sig["worker"])
    assert 3500 < sig["worker"]["started_ago_s"] < 3700 and abs(sig["worker"]["clock_skew_s"]) < 5
    assert set(("tcp", "health", "models", "metrics", "observed_at")) <= set(sig["api"])
    assert set(("requests_running", "requests_waiting", "generation_tokens_total", "prompt_tokens_total", "frozen_seconds", "observed_at")) <= set(sig["engine"])
    assert set(("ok", "http_status", "connect_s", "ttft_s", "total_s", "tokens", "terminal", "error_category", "at", "last_success_at", "consecutive_failures",
                "consecutive_timeouts", "consecutive_http_errors", "failing_since")) <= set(sig["canary"])
    assert sig["canary"]["consecutive_http_errors"] == 0 and sig["canary"]["failing_since"] is None
    assert set(("configured", "health", "observed_at")) <= set(sig["router"])
    assert set(("head_util", "worker_util", "sampled_at")) <= set(sig["gpus"])
    assert set(("non_stream_ok", "stream_ok", "progress_ok", "participation", "rank_alive",
                "participation_probes")) <= set(snap["readiness"])
    rec = snap["recovery"]
    assert set(("in_progress", "step", "attempts_in_window", "budget", "window_s", "cooldown_until", "last",
                "manual_pending", "worker_restart", "blocked")) <= set(rec)
    assert rec["in_progress"] is False and rec["step"] == "idle" and rec["manual_pending"] is False
    assert snap["incident"] is None
    assert "_counters" not in snap
    assert STATES[7] == "QUEUEING" and snap["state_code"] == 2


def test_state_document_round_trips_through_the_orchestrators_parser_in_every_state(world):
    """Cross-module: the orchestrator's engine_state.parse_state_document
    (stdlib-only input) must accept every /state document the fakes can
    produce, or it would treat the controller as unreachable (no external
    breaker open) while these tests stay green."""
    orch = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
                        "orchestrator")
    if not os.path.isdir(os.path.join(orch, "app")):
        pytest.skip("orchestrator package not beside this checkout")
    sys.path.insert(0, orch)
    try:
        from app.engine_state import parse_state_document  # type: ignore
    except Exception as exc:  # noqa: BLE001 — the orchestrator's imports are its own business
        pytest.skip(f"orchestrator.app.engine_state not importable here: {exc}")
    finally:
        sys.path.remove(orch)

    seen = set()

    def check():
        doc = json.loads(json.dumps(world.ctl.snapshot()))
        snap = parse_state_document(doc, observed_at=0.0)
        assert snap.state == doc["state"] and snap.state_code == doc["state_code"]
        seen.add(doc["state"])

    world.tick()
    check()                                                 # STARTING
    world.ctl.canary.join(10.0)
    world.tick()
    check()                                                 # READY
    world.head.running = 2.0
    world.tick()
    check()                                                 # BUSY
    world.head.running = 0.0
    world.sentinel.stop()
    world.tick()
    check()                                                 # DEGRADED
    world.sentinel.start()
    world.ctl.sentinel.base_url = world.sentinel.url
    world.heal_on_restart()
    world.head.mode = "engine_dead"
    world.tick()
    check()                                                 # RECOVERING (DOWN published on the way)
    world.drive_recovery_to_ready()
    check()
    world.docker.stop()
    world.clock.advance(3600)
    world.tick()
    check()                                                 # MONITORING_UNKNOWN
    assert {"STARTING", "READY", "BUSY", "DEGRADED", "RECOVERING", "MONITORING_UNKNOWN"} <= seen
    assert any(t["to"] == "DOWN" for t in world.ctl.transitions)
