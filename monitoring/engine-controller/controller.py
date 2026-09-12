#!/usr/bin/env python3
"""The engine controller — the head-side state machine of contract §6, and
the single recovery authority of v2 (strict one-model mode).

WHY THIS EXISTS. On 2026-09-11 22:15:50Z the worker rank of the TP=2 pair
died in a Triton kernel. For the next five minutes every existing check said
the engine was fine: /health, /v1/models and /metrics all answered 200,
Prometheus scraped ``up == 1``, GPU utilisation sat at 96 %. What had
actually stopped was the only thing that matters — ``generation_tokens_total``
froze at 2,138,504 with nine requests "running". The shell watchdog needed
two 120 s probe timeouts; vLLM's own 300 s executor timeout fired first and
killed the API, whose process then hung in teardown until the Docker
healthcheck's four misses. Eleven minutes of no inference, of which the
first five looked healthy to everything we had.

So this program holds exactly one belief: **a process being alive proves
nothing; only a real completion does** (contract §2). It reads twenty
signals — the Docker process tables on both nodes (rank 1 through the worker
sentinel), the API's TCP/health/models/metrics, the two token counters, the
two GPU exporters, and the completions it runs itself — folds them into one
of the nine states, and when a failure is confirmed it restarts the PAIR in
the only order that works (worker first, so it is waiting at the rendezvous
when the new head opens it), under a host flock nobody else restarts
through, within a budget of three an hour, after capturing evidence.

The rules that shape the code, each from the contract:

  * READY needs the §5 v2 READINESS SEQUENCE to have passed for the head
    container's current start — a non-streaming completion, a streaming one,
    token-counter progress, both GPUs seen working during TWO concurrent
    256-token completions (which also compile the candidate build's
    multi-sequence GDN prefill kernel before a user can, §6.6), the rank
    alive — and a completion success within the last two probe intervals
    since. STARTING vs DOWN with no proof yet is decided by the head's
    ``started_at`` against ``COLD_START_BUDGET_S``.
  * Before STOP_STALE_PAIR the head's ``MemAvailable`` is checked against
    what a fresh model load needs (``HEAD_MIN_MEM_AVAILABLE_BYTES``, 30 GiB):
    too little is a WARNING with the number and the runbook pointer and ONE
    tick of ``recovery.blocked = head_memory_low`` — then the restart goes
    ahead regardless, because a dead engine must still be restarted.
  * Nothing that happened against a PREVIOUS head incarnation counts: every
    probe records when it started, and a result whose start predates the
    head's ``started_at`` is discarded; every counter resets when
    ``started_at`` moves. A process name is never used as evidence of death
    unless it was seen alive for this same incarnation (a TP=1 engine has no
    ``VLLM::Worker_TP`` process at all).
  * MONITORING_UNKNOWN is what we say when we cannot observe (the Docker
    socket is gone, the API neither answers nor refuses). It is never a
    synonym for DOWN; missing data alone never restarts anything.
  * Detection is DETECT → CONFIRM with the observation counts of §6.3, and
    every recovery step is published in /state and logged with the incident
    id before the next one starts, so the orchestrator queues on RECOVERING
    and an operator can read what happened after the fact. While a step
    blocks (a sentinel restart, a docker restart) a heartbeat re-stamps the
    snapshot so freshness measures this process, not the step.
  * A canary proves nothing without a token: a terminal chunk on empty text
    is a failure (``no tokens``), and the token-progress step expects at
    least the tokens actually received, never zero.
  * DEGRADED is bounded. Three consecutive HTTP errors from a proven engine
    (rule 6), or CANARY_FAIL_DEGRADED_MAX_S of any consecutive failures with
    /health still 200 (rule 7), confirm a wedge — an engine that answers
    /health while no completion gets through is exactly what 2026-09-11
    looked like for five minutes. Errors are not starvation: rule 6 has no
    saturation exemption.
  * Cross-host time is never compared: the sentinel reports how long ago the
    worker started (a duration on its own clock) and the controller sets it
    against how long ago its own last proven completion was.
  * An unexpected exception inside a recovery fails the attempt, releases
    the lock and is logged with its traceback — RECOVERING with the flock
    held and nobody advancing it is worse than a failed recovery.
  * The controller ALONE performs destructive recovery (v2 §6). The sentinel
    acts only on this program's ``POST /restart``; the Docker healthchecks
    are report-only except a last-resort tier minutes later; a manual
    ``POST /recover`` is subject to the same budget and cooldown.
  * An incident ends when the engine proves healthy again by ANY path —
    three consecutive completion successes — not only after a recovery this
    program performed.

Standard library only, like the exporter it is modelled on
(monitoring/exporters/dgx-gpu): it runs on ``python:3.12-slim`` with a
bind-mounted script and must not need a package index to come up during an
incident. Prefix every log line with ``[controller]``.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import random
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Tuple

from common import (
    Budget,
    Clock,
    ConnectFailed,
    DockerClient,
    DockerError,
    DockerUnavailable,
    FAILURE_CATEGORIES,
    MetricsDoc,
    ReadTimeout,
    configure_logging,
    env_bool,
    env_float,
    env_int,
    env_str,
    fetch,
    is_loopback,
    open_http,
    parse_docker_time,
    parse_gpu_utilization,
    parse_vllm_metrics,
    read_json_body,
    read_mem_available,
    run_periodically,
    scan_compile_error_lines,
    send_json,
    send_metrics,
    send_text,
    split_url,
    tcp_connect,
    utc_stamp,
)

log = logging.getLogger("controller")

# ---------------------------------------------------------------------------
# Bounded vocabularies (contract §2, §7.1)
# ---------------------------------------------------------------------------

#: The nine states in code order. QUEUEING (7, v2; was FALLBACK_ACTIVE) is
#: decided by the orchestrator and derived by Grafana; the controller never
#: emits it, but it stays in the one-hot so the label set is the contract's.
STATES: Tuple[str, ...] = (
    "MONITORING_UNKNOWN",  # 0
    "STARTING",            # 1
    "READY",               # 2
    "BUSY",                # 3
    "DEGRADED",            # 4
    "WEDGED",              # 5
    "RECOVERING",          # 6
    "QUEUEING",            # 7
    "DOWN",                # 8
)
STATE_CODE: Dict[str, int] = {name: code for code, name in enumerate(STATES)}

STEPS: Tuple[str, ...] = (
    "idle", "detect", "confirm", "capture", "stop_pair", "wait_load", "canary", "mark_ready", "verify",
)
PROBE_OUTCOMES: Tuple[str, ...] = ("ok", "timeout", "http_error", "connect_error")
ATTEMPT_OUTCOMES: Tuple[str, ...] = ("started", "succeeded", "failed", "budget_exhausted")
#: How the worker half of STOP_STALE_PAIR went, per attempt (a 403 from a
#: token skew used to be swallowed: the head restarted alone and the stale
#: worker could never rejoin it).
WORKER_RESTART_OUTCOMES: Tuple[str, ...] = ("none", "ok", "refused", "unreachable", "failed", "skipped", "dry_run")
#: The verdict of readiness step 4 (contract §5 v2).
PARTICIPATION_VERDICTS: Tuple[str, ...] = ("pending", "ok", "unobserved", "failed", "skipped")
#: What a cold-start timeout looked like: the API never served a completion
#: (``load``), completions worked but the sequence never passed
#: (``readiness``), or a compile-error signature is in the head log
#: (``compile`` — the kernel cache, contract §6.6).
COLD_START_DETAILS: Tuple[str, ...] = ("none", "load", "readiness", "compile")
#: Why a confirmed failure is not being acted on (``recovery.blocked``).
#: ``head_memory_low`` holds for exactly ONE tick (the warning the operator
#: needs); the others hold for as long as their condition does.
BLOCKED_KINDS: Tuple[str, ...] = ("", "cooldown", "lock_held", "lock_unavailable", "budget_exhausted",
                                  "head_memory_low")
READINESS_STEPS: Tuple[str, ...] = ("non_stream", "stream", "progress", "participation", "rank_alive")
#: Readiness step 4 sends this many participation completions AT ONCE (two
#: sockets, started together). One request proves both ranks; two batched
#: in one step also make the engine compile the candidate build's
#: multi-sequence GDN prefill kernel — built in memory at the first step
#: that batches ≥ 2 prefills, ≈ 3 s per rank, covered by no on-disk cache
#: (contract §6.6, CANDIDATE-B §2.6) — BEFORE the first two real users hit
#: it after READY. Not a knob: a single probe would silently lose the
#: warm-up.
PARTICIPATION_PROBES = 2
#: Where the runbook's memory check lives; the WARNING points at it.
HEAD_MEMORY_RUNBOOK = "docs/availability/RUNBOOK.md §7 (Memory check)"
GIB = 1024 ** 3

#: Which state a CONFIRMED failure maps to while the controller cannot act
#: on it yet (cooldown, another actor holds the lock, budget spent). A wedge
#: is a wedge; the rest mean the primary cannot serve.
CONFIRMED_STATE: Dict[str, str] = {
    "wedged_frozen_tokens": "WEDGED",
    "canary_timeout": "WEDGED",
    "canary_http_error": "WEDGED",   # /health answers, completions do not: a wedge, not a dead process
    "worker_rank_dead": "DOWN",
    "head_engine_dead": "DOWN",
    "head_api_dead": "DOWN",
    "manual": "DOWN",
}

#: The process titles vLLM sets (verified in the pinned image,
#: vllm/v1/executor/multiproc_executor.py setup_proc_title_and_log_prefix):
#: ``VLLM::Worker`` while a worker waits at the rendezvous, ``VLLM::Worker_TP<n>``
#: once the parallel groups exist. A TP=1 engine (the ``uni`` executor) has
#: neither — only ``vllm serve`` and ``VLLM::EngineCore``.
RANK_PROCESS_TITLE = "VLLM::Worker"
ENGINE_CORE_TITLE = "VLLM::EngineCore"

#: A head start first observed later than this after it began is one the
#: controller did not watch (it was redeployed beside a running head): its
#: cold start is not measured rather than reported as the head's age.
COLD_START_WATCH_GRACE_S = 120.0

#: The choreography heartbeat stops on its own after this long: every
#: blocking call it covers is bounded by its own timeout (sentinel restart
#: 5 + 120 s, docker restart t + 60 s plus three re-inspects, diagnostics
#: 30 + 20 s — under 300 s in total), so a beat still running past this is
#: a bug, and a stale snapshot (MONITORING_UNKNOWN) is then the truth.
CHOREOGRAPHY_HEARTBEAT_MAX_S = 600.0


def canary_engine_fault_kind(res: "CanaryResult") -> str:
    """Which canary failures are the ENGINE's — ``canary_timeout`` or
    ``canary_http_error`` (rules 6 and 7 count them) — and which are the
    probe's (``""``): a 4xx is a request the API rejected (a changed request
    shape after an upgrade, a 429 under load) — restarting the pair cannot
    fix it and calling it a wedge would queue users on a serving engine; a
    result without an HTTP status is a probe crash (a bug here), not
    evidence about the engine. A connect error is folded into the timeout
    kind for rule 7 only: a completion the API would not take while
    ``/health`` still answers is the same shape as one it never answers."""
    if res.ok:
        return ""
    if res.outcome in ("timeout", "connect_error"):
        return "canary_timeout"
    if res.outcome == "http_error":
        status = res.http_status
        if status is None or 400 <= int(status) < 500:
            return ""
        return "canary_http_error"
    return ""


# ---------------------------------------------------------------------------
# Configuration — every knob is an environment variable with the contract's
# default, so the compose file carries only what differs per node.
# ---------------------------------------------------------------------------


@dataclass
class Config:
    bind: str = "0.0.0.0"
    port: int = 9838
    head_container: str = "sf-local-ai-vllm-1"
    head_api_url: str = "http://127.0.0.1:8000"
    sentinel_url: str = ""            # empty = single-node mode
    sentinel_token: str = ""
    router_health_url: str = ""       # empty = router (internal classifier) not probed
    head_gpu_exporter_url: str = "http://127.0.0.1:9835/metrics"
    worker_gpu_exporter_url: str = "" # empty = the worker GPU is not sampled (skip)
    gpu_util_metric: str = "dgx_gpu_utilization_percent"
    participation_min_util: float = 30.0
    participation_max_tokens: int = 256
    participation_sample_s: float = 0.25
    participation_tail_s: float = 2.0
    readiness_max_tokens: int = 32
    readiness_progress_retry_s: float = 1.0
    canary_max_tokens: int = 4
    canary_interval_s: float = 30.0
    canary_interval_fast_s: float = 10.0
    canary_timeout_s: float = 60.0
    canary_connect_timeout_s: float = 5.0
    canary_model: str = ""            # override; default = first id of /v1/models
    canary_outstanding_s: float = 60.0
    canary_starvation_s: float = 300.0
    #: How long a PROVEN engine may fail every canary (any kind) with
    #: /health still 200 before DEGRADED "awaiting confirmation" becomes a
    #: confirmed WEDGED (rule 7): the bound on the honest in-between.
    canary_fail_degraded_max_s: float = 120.0
    ttft_degraded_s: float = 10.0
    frozen_s: float = 90.0
    cold_start_budget_s: float = 900.0
    recovery_budget: int = 3
    recovery_window_s: float = 3600.0
    recovery_cooldown_s: float = 120.0
    recovery_jitter_s: float = 5.0
    head_api_dead_gap_s: float = 10.0
    worker_rank_grace_s: float = 120.0
    head_restart_timeout_s: int = 10
    poll_s: float = 5.0
    probe_timeout_s: float = 5.0
    router_probe_interval_s: float = 30.0
    #: While the tick thread is inside a blocking choreography call (sentinel
    #: POST /restart up to 125 s, docker restart up to 70 s, diagnostics up
    #: to 55 s) a helper thread re-stamps the published snapshot's
    #: ``generated_at`` this often, so the freshness rules of §7.3 / §8.1
    #: measure the controller's liveness, not the length of a step.
    choreography_heartbeat_s: float = 5.0
    docker_socket: str = "/var/run/docker.sock"
    docker_api_version: str = "1.53"
    lock_path: str = "/run/techsara/locks/engine-recovery.lock"
    incident_dir: str = "/run/techsara/incidents"
    #: The head-memory precondition of STOP_STALE_PAIR: a fresh model load
    #: reads 21.8 GiB of weights and allocates ~25 GiB; on 2026-09-12 07:11
    #: IST a new CUDA context failed with NV_ERR_NO_MEMORY at the memory
    #: edge. Below this the controller WARNS for one tick, then restarts
    #: anyway (a dead engine must still be restarted). 0 disables the check.
    head_min_mem_available_bytes: int = 30 * GIB
    #: The host's /proc/meminfo (host network, no lxcfs: the container's
    #: view is the host's); a test points it at a file.
    meminfo_path: str = "/proc/meminfo"
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        lock_dir = env_str("LOCK_DIR", "/run/techsara/locks")
        return cls(
            bind=env_str("CONTROLLER_BIND", "0.0.0.0"),
            port=env_int("CONTROLLER_PORT", 9838),
            head_container=env_str("HEAD_CONTAINER", "sf-local-ai-vllm-1"),
            head_api_url=env_str("HEAD_API_URL", "http://127.0.0.1:8000").rstrip("/"),
            sentinel_url=env_str("SENTINEL_URL", "").rstrip("/"),
            sentinel_token=env_str("CLUSTER_SENTINEL_TOKEN", ""),
            # v1 spelled it FALLBACK_HEALTH_URL; the router is only an
            # internal classifier in v2, but the compose key may lag a deploy.
            router_health_url=env_str("ROUTER_HEALTH_URL", env_str("FALLBACK_HEALTH_URL", "")),
            head_gpu_exporter_url=env_str("HEAD_GPU_EXPORTER_URL", "http://127.0.0.1:9835/metrics"),
            worker_gpu_exporter_url=env_str("WORKER_GPU_EXPORTER_URL", ""),
            gpu_util_metric=env_str("GPU_UTIL_METRIC", "dgx_gpu_utilization_percent"),
            participation_min_util=env_float("PARTICIPATION_MIN_UTIL", 30.0),
            participation_max_tokens=env_int("PARTICIPATION_MAX_TOKENS", 256),
            participation_sample_s=env_float("PARTICIPATION_SAMPLE_S", 0.25),
            participation_tail_s=env_float("PARTICIPATION_TAIL_S", 2.0),
            readiness_max_tokens=env_int("READINESS_MAX_TOKENS", 32),
            readiness_progress_retry_s=env_float("READINESS_PROGRESS_RETRY_S", 1.0),
            canary_max_tokens=env_int("CANARY_MAX_TOKENS", 4),
            canary_interval_s=env_float("CANARY_INTERVAL_S", 30.0),
            canary_interval_fast_s=env_float("CANARY_INTERVAL_FAST_S", 10.0),
            canary_timeout_s=env_float("CANARY_TIMEOUT_S", 60.0),
            canary_connect_timeout_s=env_float("CANARY_CONNECT_TIMEOUT_S", 5.0),
            canary_model=env_str("CANARY_MODEL", ""),
            canary_outstanding_s=env_float("CANARY_OUTSTANDING_S", 60.0),
            canary_starvation_s=env_float("CANARY_STARVATION_S", 300.0),
            canary_fail_degraded_max_s=env_float("CANARY_FAIL_DEGRADED_MAX_S", 120.0),
            ttft_degraded_s=env_float("TTFT_DEGRADED_S", 10.0),
            frozen_s=env_float("FROZEN_S", 90.0),
            cold_start_budget_s=env_float("COLD_START_BUDGET_S", 900.0),
            recovery_budget=env_int("RECOVERY_BUDGET", 3),
            recovery_window_s=env_float("RECOVERY_WINDOW_S", 3600.0),
            recovery_cooldown_s=env_float("RECOVERY_COOLDOWN_S", 120.0),
            recovery_jitter_s=env_float("RECOVERY_JITTER_S", 5.0),
            head_api_dead_gap_s=env_float("HEAD_API_DEAD_GAP_S", 10.0),
            worker_rank_grace_s=env_float("WORKER_RANK_GRACE_S", 120.0),
            head_restart_timeout_s=env_int("HEAD_RESTART_TIMEOUT_S", 10),
            poll_s=env_float("POLL_S", 5.0),
            probe_timeout_s=env_float("PROBE_TIMEOUT_S", 5.0),
            router_probe_interval_s=env_float("ROUTER_PROBE_INTERVAL_S",
                                              env_float("FALLBACK_PROBE_INTERVAL_S", 30.0)),
            choreography_heartbeat_s=env_float("CHOREOGRAPHY_HEARTBEAT_S", 5.0),
            docker_socket=env_str("DOCKER_SOCKET", "/var/run/docker.sock"),
            docker_api_version=env_str("DOCKER_API_VERSION", "1.53"),
            lock_path=env_str("RECOVERY_LOCK_PATH", os.path.join(lock_dir, "engine-recovery.lock")),
            incident_dir=env_str("INCIDENT_DIR", env_str("INCIDENTS_DIR", "/run/techsara/incidents")),
            head_min_mem_available_bytes=max(0, env_int("HEAD_MIN_MEM_AVAILABLE_BYTES", 30 * GIB)),
            meminfo_path=env_str("MEMINFO_PATH", "/proc/meminfo"),
            dry_run=env_bool("DRY_RUN", False),
        )


# ---------------------------------------------------------------------------
# The synthetic canary and the readiness sequence (contract §5)
# ---------------------------------------------------------------------------


@dataclass
class ReadinessResult:
    """The §5 v2 sequence, step by step. ``None`` = the step did not run
    (an earlier one failed); ``participation`` is one of
    ``PARTICIPATION_VERDICTS``; ``rank_alive`` is filled in by the
    controller from the sentinel when the result is harvested.
    ``participation_probes`` holds one entry per concurrent step-4
    completion (``PARTICIPATION_PROBES`` of them, started together): its
    outcome, TTFT, total latency and token count — the evidence that the
    multi-sequence path ran, and how long each request took."""
    non_stream_ok: Optional[bool] = None
    stream_ok: Optional[bool] = None
    progress_ok: Optional[bool] = None
    participation: str = "pending"
    rank_alive: Optional[bool] = None
    passed: bool = False
    failed_step: str = ""
    detail: str = ""
    tokens_expected: int = 0
    tokens_delta: Optional[float] = None
    gpus: dict = field(default_factory=dict)
    participation_probes: List[dict] = field(default_factory=list)
    started_at: float = 0.0
    at: float = 0.0

    def doc(self) -> dict:
        return {
            "non_stream_ok": self.non_stream_ok, "stream_ok": self.stream_ok, "progress_ok": self.progress_ok,
            "participation": self.participation, "rank_alive": self.rank_alive, "passed": self.passed,
            "failed_step": self.failed_step, "detail": self.detail, "tokens_expected": self.tokens_expected,
            "tokens_delta": self.tokens_delta, "participation_probes": [dict(p) for p in self.participation_probes],
            "started_at": self.started_at, "at": self.at,
        }


@dataclass
class CanaryResult:
    ok: bool
    outcome: str                 # ok | timeout | http_error | connect_error
    error_category: str          # none | canary_timeout | canary_http_error | connect_error
    http_status: Optional[int]
    connect_s: Optional[float]
    ttft_s: Optional[float]
    total_s: float
    tokens: int
    terminal: bool
    started_at: float            # wall — when the probe STARTED (compared with the head's started_at)
    at: float                    # wall, completion
    detail: str = ""
    kind: str = "routine"        # routine | readiness
    health_at_start: Optional[int] = None   # /health as observed in the tick that started the probe
    readiness: Optional[ReadinessResult] = None


def _canary_body(model: str, max_tokens: int, stream: bool, ignore_eos: bool) -> bytes:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "seed": 7,
        "chat_template_kwargs": {"enable_thinking": False},
        "stream": stream,
    }
    if ignore_eos:
        # The readiness steps need their full token count generated (32, then
        # 256 for the participation window); the routine canary keeps the
        # exact §5 body, which lets the model stop after "ok".
        body["ignore_eos"] = True
    return json.dumps(body).encode()


def run_canary(cfg: Config, clock: Clock, model: str, max_tokens: Optional[int] = None,
               ignore_eos: bool = False) -> CanaryResult:
    """One streaming ``/v1/chat/completions`` with the contract's exact body.

    Nothing about the response is kept except counts and timing: the prompt
    is a constant, no user is involved, and the text is discarded as it is
    read. Streaming is what makes TTFT observable — the wedge signature is
    "accepted the request, never sent a token".
    """
    body = _canary_body(model, max_tokens if max_tokens is not None else cfg.canary_max_tokens, True, ignore_eos)
    started_wall = clock.time()
    t0 = clock.mono()

    def done(ok, outcome, category, status, connect_s, ttft, tokens, terminal, detail=""):
        return CanaryResult(
            ok=ok, outcome=outcome, error_category=category, http_status=status,
            connect_s=connect_s, ttft_s=ttft, total_s=clock.mono() - t0, tokens=tokens,
            terminal=terminal, started_at=started_wall, at=clock.time(), detail=detail,
        )

    try:
        resp, connect_s, conn = open_http(
            cfg.head_api_url + "/v1/chat/completions", "POST", body,
            {"Content-Type": "application/json", "Accept": "text/event-stream"},
            connect_timeout=cfg.canary_connect_timeout_s, read_timeout=cfg.canary_timeout_s, clock=clock,
        )
    except ConnectFailed as exc:
        return done(False, "connect_error", "connect_error", None, None, None, 0, False, f"connect {exc.kind}")
    except ReadTimeout:
        return done(False, "timeout", "canary_timeout", None, None, None, 0, False,
                    f"no response within {cfg.canary_timeout_s:g}s")

    tokens = 0
    ttft: Optional[float] = None
    first_chunk: Optional[float] = None
    terminal = False
    try:
        if resp.status != 200:
            # Length only — an error body from the engine can carry a stack
            # trace, which is not ours to store here.
            try:
                length = len(resp.read(65536))
            except (OSError, socket.timeout):
                length = -1
            return done(False, "http_error", "canary_http_error", resp.status, connect_s, None, 0, False,
                        f"HTTP {resp.status} ({length} bytes)")
        while True:
            if clock.mono() - t0 > cfg.canary_timeout_s:
                return done(False, "timeout", "canary_timeout", 200, connect_s, ttft, tokens, False,
                            f"stream still open after {cfg.canary_timeout_s:g}s")
            try:
                line = resp.readline()
            except socket.timeout:
                return done(False, "timeout", "canary_timeout", 200, connect_s, ttft, tokens, False,
                            "no chunk within the read timeout")
            if not line:
                break  # EOF without [DONE]
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if first_chunk is None:
                first_chunk = clock.mono() - t0
            if payload == b"[DONE]":
                terminal = True
                break
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            if isinstance(obj, dict) and "error" in obj:
                # The dying-stream shape: 200, then an error chunk before any
                # token (EngineDeadError mid-stream).
                return done(False, "http_error", "canary_http_error", 200, connect_s, ttft, tokens, False,
                            "error chunk in stream")
            for choice in (obj.get("choices") or []) if isinstance(obj, dict) else []:
                delta = choice.get("delta") or {}
                text = delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning")
                if text:
                    tokens += 1
                    if ttft is None:
                        ttft = clock.mono() - t0
                if choice.get("finish_reason"):
                    terminal = True
    except (OSError, ValueError) as exc:  # a stream that broke mid-body
        return done(False, "http_error", "canary_http_error", 200, connect_s, ttft, tokens, False,
                    f"stream broke: {type(exc).__name__}")
    finally:
        conn.close()

    if ttft is None:
        ttft = first_chunk
    if not terminal:
        return done(False, "http_error", "canary_http_error", 200, connect_s, ttft, tokens, False,
                    "stream ended without a terminal chunk")
    if tokens < 1:
        # A terminal chunk with no content is not a completion: an engine
        # that ends every request without output (finish_reason on empty
        # text, or a bare [DONE]) must never prove itself with it.
        return done(False, "http_error", "canary_http_error", 200, connect_s, ttft, 0, True, "no tokens")
    return done(True, "ok", "none", 200, connect_s, ttft, tokens, True)


def run_non_stream(cfg: Config, clock: Clock, model: str, max_tokens: int) -> CanaryResult:
    """Readiness step 1: the same request without streaming. A finished
    JSON completion with a ``finish_reason`` AND at least one token proves
    the whole path the orchestrator's non-streaming callers use;
    ``usage.completion_tokens`` is what the token-progress step expects to
    see on ``/metrics``. Zero tokens is a failure (detail ``no tokens``)."""
    body = _canary_body(model, max_tokens, False, True)
    started_wall = clock.time()
    t0 = clock.mono()

    def done(ok, outcome, category, status, connect_s, tokens, terminal, detail=""):
        return CanaryResult(
            ok=ok, outcome=outcome, error_category=category, http_status=status, connect_s=connect_s,
            ttft_s=None, total_s=clock.mono() - t0, tokens=tokens, terminal=terminal,
            started_at=started_wall, at=clock.time(), detail=detail, kind="non_stream",
        )

    try:
        status, data = fetch(cfg.head_api_url + "/v1/chat/completions", "POST", body,
                             {"Content-Type": "application/json"}, connect_timeout=cfg.canary_connect_timeout_s,
                             read_timeout=cfg.canary_timeout_s, max_bytes=1024 * 1024, clock=clock)
    except ConnectFailed as exc:
        return done(False, "connect_error", "connect_error", None, None, 0, False, f"connect {exc.kind}")
    except ReadTimeout:
        return done(False, "timeout", "canary_timeout", None, None, 0, False,
                    f"no response within {cfg.canary_timeout_s:g}s")
    if status != 200:
        return done(False, "http_error", "canary_http_error", status, None, 0, False,
                    f"HTTP {status} ({len(data)} bytes)")
    try:
        obj = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return done(False, "http_error", "canary_http_error", 200, None, 0, False, "response is not JSON")
    if not isinstance(obj, dict) or "error" in obj:
        return done(False, "http_error", "canary_http_error", 200, None, 0, False, "error object in response")
    choices = obj.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    terminal = bool(choice.get("finish_reason"))
    usage = obj.get("usage") or {}
    tokens = 0
    try:
        tokens = int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        tokens = 0
    if tokens <= 0 and ((choice.get("message") or {}).get("content")):
        tokens = 1  # no usage block: at least the content we saw
    if not terminal:
        return done(False, "http_error", "canary_http_error", 200, None, tokens, False, "no finish_reason")
    if tokens < 1:
        return done(False, "http_error", "canary_http_error", 200, None, 0, True, "no tokens")
    return done(True, "ok", "none", 200, None, tokens, True)


class GpuUtilSampler:
    """Samples one dgx-gpu exporter every ``interval_s`` on its own thread
    for as long as a participation probe runs (plus a tail, because the
    exporter caches nvidia-smi for 1.5 s and nvidia-smi's utilisation is
    itself a trailing window). Keeps only the maximum, the sample count and
    the error count — never a series."""

    def __init__(self, url: str, metric: str, interval_s: float, timeout_s: float, clock: Clock):
        self.url = url
        self.metric = metric
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self.clock = clock
        self.max_util: Optional[float] = None
        self.samples = 0
        self.errors = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _sample_once(self) -> None:
        try:
            status, body = fetch(self.url, connect_timeout=self.timeout_s, read_timeout=self.timeout_s,
                                 max_bytes=256 * 1024, clock=self.clock)
        except (ConnectFailed, ReadTimeout):
            self.errors += 1
            return
        if status != 200:
            self.errors += 1
            return
        value = parse_gpu_utilization(body.decode("utf-8", "replace"), self.metric)
        if value is None:
            self.errors += 1  # answered, but nvidia-smi did not: no reading
            return
        self.samples += 1
        self.max_util = value if self.max_util is None else max(self.max_util, value)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._sample_once()
            if self._stop.wait(self.interval_s):
                break

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="gpu-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(self.timeout_s * 2 + 1.0)
        return {"max": self.max_util, "samples": self.samples, "errors": self.errors,
                "reachable": self.samples > 0}


def _probe_doc(index: int, res: CanaryResult) -> dict:
    """What ``/state`` keeps of one participation probe: counts and timing,
    never text."""
    return {"probe": index, "ok": res.ok, "outcome": res.outcome, "http_status": res.http_status,
            "connect_s": res.connect_s, "ttft_s": res.ttft_s, "total_s": res.total_s, "tokens": res.tokens,
            "terminal": res.terminal, "detail": res.detail, "started_at": res.started_at, "at": res.at}


def run_participation_probes(cfg: Config, clock: Clock, model: str, count: int = PARTICIPATION_PROBES,
                             runner=run_canary) -> List[CanaryResult]:
    """``count`` streaming ``PARTICIPATION_MAX_TOKENS`` completions, each on
    its own socket and thread, all started before any is awaited, all
    awaited before returning — so the engine sees them in one scheduler
    step and, on the candidate build, compiles the multi-sequence GDN
    prefill kernel here rather than under the first two real users. Every
    thread returns a ``CanaryResult`` (a crash inside one is a result, not
    an exception): the caller requires all of them to be ``ok``."""
    results: List[Optional[CanaryResult]] = [None] * count

    def one(i: int) -> None:
        try:
            results[i] = runner(cfg, clock, model, max_tokens=cfg.participation_max_tokens, ignore_eos=True)
        except Exception as exc:  # noqa: BLE001 — a probe bug must not kill the sequence
            log.exception("participation probe %d crashed", i + 1)
            wall = clock.time()
            results[i] = CanaryResult(False, "http_error", "canary_http_error", None, None, None, 0.0, 0, False,
                                      wall, wall, f"probe crashed: {type(exc).__name__}")

    threads = [threading.Thread(target=one, args=(i,), name=f"participation-{i + 1}", daemon=True)
               for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        # Each probe bounds itself (connect + read timeouts, and the
        # CANARY_TIMEOUT_S wall inside run_canary), so this join ends.
        t.join()
    out: List[CanaryResult] = []
    for r in results:
        if r is None:  # cannot happen (the thread always assigns); a missing probe is a failed one
            wall = clock.time()
            r = CanaryResult(False, "http_error", "canary_http_error", None, None, None, 0.0, 0, False,
                             wall, wall, "no probe result")
        out.append(r)
    return out


def _metrics_generation_total(cfg: Config, clock: Clock) -> Optional[float]:
    try:
        status, body = fetch(cfg.head_api_url + "/metrics", connect_timeout=cfg.probe_timeout_s,
                             read_timeout=cfg.probe_timeout_s, clock=clock)
    except (ConnectFailed, ReadTimeout):
        return None
    if status != 200:
        return None
    return parse_vllm_metrics(body.decode("utf-8", "replace")).get("generation_tokens_total")


def run_readiness_sequence(cfg: Config, clock: Clock, model: str, worker_expected: bool = False,
                           sampler_factory: Optional[Callable[[str], GpuUtilSampler]] = None) -> CanaryResult:
    """Contract §5 v2, steps 1–4 in order (step 5, the sentinel's rank
    verdict, is the controller's at harvest time):

      1. non-streaming completion, ``READINESS_MAX_TOKENS`` tokens — at
         least one token received, or the step fails (``no tokens``);
      2. streaming completion, same length, TTFT measured, terminal chunk,
         at least one token;
      3. ``vllm:generation_tokens_total`` on /metrics grew by at least the
         tokens steps 1+2 actually received, and by at least one (re-read a
         few times: the stat logger records an iteration in the same loop
         turn that streams its chunk, a few milliseconds either way);
      4. ``PARTICIPATION_PROBES`` (two) ``PARTICIPATION_MAX_TOKENS`` streaming
         completions started TOGETHER on two sockets — so one scheduler
         step batches ≥ 2 prefills and the candidate build compiles its
         multi-sequence GDN prefill kernel now, not under the first two
         real users (contract §6.6) — while both GPU exporters are sampled
         every ``PARTICIPATION_SAMPLE_S``. BOTH completions must finish
         (each one's TTFT and total latency is recorded in
         ``participation_probes``) and both GPUs must reach
         ``PARTICIPATION_MIN_UTIL`` at least once. An exporter that never
         answered — or, in cluster mode (``worker_expected``), a worker
         exporter that is not configured at all — makes the verdict
         ``unobserved``: evidence missing, not evidence against, because on
         a TP=2 engine the finished completions have already proved the
         all-reduce ran on both ranks. Both reachable and one GPU flat is
         ``failed``. No exporter configured on a single node is ``skipped``
         — the two completions still run (the kernel warm-up is not about
         the exporters).

    The returned ``CanaryResult`` is the STREAM step's (TTFT, latency, tokens
    — the routine canary's shape) with ``ok`` = every completion succeeded
    and ``readiness`` carrying the per-step verdicts. ``passed`` needs steps
    1–3 true and the participation verdict in {ok, unobserved, skipped}.
    """
    rd = ReadinessResult(started_at=clock.time())
    expected = 0

    def finish(res: CanaryResult, step: str, detail: str) -> CanaryResult:
        rd.failed_step = step
        rd.detail = detail
        rd.at = clock.time()
        res.kind = "readiness"
        res.readiness = rd
        res.started_at = rd.started_at
        res.at = rd.at
        res.detail = f"readiness {step}: {detail}" if detail else f"readiness {step}"
        return res

    before = _metrics_generation_total(cfg, clock)

    # 1. non-streaming
    ns = run_non_stream(cfg, clock, model, cfg.readiness_max_tokens)
    rd.non_stream_ok = ns.ok
    if not ns.ok:
        return finish(ns, "non_stream", ns.detail)
    expected += ns.tokens

    # 2. streaming
    st = run_canary(cfg, clock, model, max_tokens=cfg.readiness_max_tokens, ignore_eos=True)
    rd.stream_ok = st.ok
    if not st.ok:
        return finish(st, "stream", st.detail)
    expected += st.tokens
    rd.tokens_expected = expected

    # 3. token progress on /metrics — the counter must have grown by at
    #    least the tokens steps 1 and 2 actually received, and by at least
    #    one: with both steps requiring a token, ``expected`` is ≥ 2 here;
    #    the floor keeps the step from ever degenerating to ``delta >= 0``.
    if expected < 1:
        return finish(st, "progress", f"no tokens received by steps 1 and 2 ({expected}): nothing to measure")
    expected = max(expected, 1)
    after = _metrics_generation_total(cfg, clock)
    tries = 0
    while after is not None and before is not None and after - before < expected and tries < 3:
        tries += 1
        clock.sleep(cfg.readiness_progress_retry_s)
        after = _metrics_generation_total(cfg, clock)
    if before is None or after is None:
        rd.progress_ok = None
        return finish(st, "progress", "/metrics unavailable: token progress unobservable")
    rd.tokens_delta = after - before
    rd.progress_ok = rd.tokens_delta >= expected
    if not rd.progress_ok:
        return finish(st, "progress",
                      f"generation_tokens_total grew by {rd.tokens_delta:.0f} for {expected} tokens received")

    # 4. two concurrent participation probes — both must finish — while
    #    both GPUs are sampled. The probes run whether or not an exporter is
    #    configured: the multi-sequence kernel warm-up is theirs to do, the
    #    exporters only decide the verdict.
    urls = {"head": cfg.head_gpu_exporter_url, "worker": cfg.worker_gpu_exporter_url}
    factory = sampler_factory or (lambda url: GpuUtilSampler(url, cfg.gpu_util_metric, cfg.participation_sample_s,
                                                              min(1.0, cfg.probe_timeout_s), clock))
    samplers = {node: factory(url) for node, url in urls.items() if url}
    for s in samplers.values():
        s.start()
    probes = run_participation_probes(cfg, clock, model)
    rd.participation_probes = [_probe_doc(i + 1, r) for i, r in enumerate(probes)]
    all_ok = all(r.ok for r in probes)
    if all_ok and samplers:
        # Real time, not the injectable clock: the exporter's 1.5 s cache
        # and nvidia-smi's trailing window are on the wall clock.
        time.sleep(cfg.participation_tail_s)
    results = {node: s.stop() for node, s in samplers.items()}
    rd.gpus = {
        "head_util": (results.get("head") or {}).get("max"),
        "worker_util": (results.get("worker") or {}).get("max"),
        "head_samples": (results.get("head") or {}).get("samples"),
        "worker_samples": (results.get("worker") or {}).get("samples"),
        "sampled_at": clock.time() if samplers else None,
    }
    if not all_ok:
        # The first probe that did not complete carries the result; the
        # detail says how the other went, so "one of two hung" reads as such.
        k, bad = next((i, r) for i, r in enumerate(probes) if not r.ok)
        done = sum(1 for r in probes if r.ok)
        return finish(bad, "participation",
                      f"participation probe {k + 1}/{PARTICIPATION_PROBES} {bad.outcome}: {bad.detail} "
                      f"({done}/{PARTICIPATION_PROBES} completed; both must)")
    if not samplers and not worker_expected:
        rd.participation = "skipped"
    elif not samplers:
        rd.participation = "unobserved"
        rd.detail = "no GPU exporter configured: participation unobserved (the completions finished on a TP=2 engine)"
    else:
        unreachable = [node for node, r in results.items() if not r["reachable"]]
        if worker_expected and "worker" not in results:
            unreachable.append("worker (exporter not configured)")
        low = [node for node, r in results.items()
               if r["reachable"] and (r["max"] is None or r["max"] < cfg.participation_min_util)]
        if unreachable:
            rd.participation = "unobserved"
            rd.detail = (f"GPU exporter unreachable on {', '.join(unreachable)}: participation unobserved "
                         f"(the completion itself finished, which on a TP=2 engine needs both ranks)")
        elif low:
            rd.participation = "failed"
            seen = ", ".join(f"{node} max {results[node]['max'] or 0:.0f}%" for node in low)
            return finish(st, "participation",
                          f"{seen} below PARTICIPATION_MIN_UTIL {cfg.participation_min_util:.0f}% during the probe")
        else:
            rd.participation = "ok"

    rd.passed = True
    rd.at = clock.time()
    st.kind = "readiness"
    st.readiness = rd
    st.started_at = rd.started_at
    st.at = rd.at
    return st


class Canary:
    """Runs a probe on its own thread so a hung stream cannot stall the
    observation loop — the loop must keep noticing /health, the token counters
    and the sentinel WHILE the canary is outstanding, because "canary
    outstanding ≥ 60 s with /health 200" is itself a wedge signal (§2).

    Two kinds: ``routine`` (§5's streaming probe) and ``readiness`` (the
    whole §5 v2 sequence). ``discard()`` drops whatever the thread in flight
    will return — a recovery, or a new head incarnation, must never harvest
    a result from before it."""

    def __init__(self, cfg: Config, clock: Clock, runner=run_canary, readiness_runner=run_readiness_sequence):
        self._cfg = cfg
        self._clock = clock
        self._runner = runner
        self._readiness_runner = readiness_runner
        self._thread: Optional[threading.Thread] = None
        self._result: Optional[CanaryResult] = None
        self._lock = threading.Lock()
        self._generation = 0
        self.started_mono: Optional[float] = None
        self.kind = "routine"
        #: The start of the first probe since the last success — the
        #: "outstanding since" of the wedge rule. Survives a timed-out probe.
        self.outstanding_since_mono: Optional[float] = None
        self.probes_started = 0

    @property
    def in_flight(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def elapsed_s(self, now_mono: float) -> float:
        return (now_mono - self.started_mono) if (self.in_flight and self.started_mono is not None) else 0.0

    def outstanding_s(self, now_mono: float) -> float:
        return (now_mono - self.outstanding_since_mono) if self.outstanding_since_mono is not None else 0.0

    def start(self, model: str, kind: str = "routine", health_at_start: Optional[int] = None,
              worker_expected: bool = False) -> bool:
        if self.in_flight:
            return False
        now = self._clock.mono()
        self.started_mono = now
        self.kind = kind
        if self.outstanding_since_mono is None:
            self.outstanding_since_mono = now
        self.probes_started += 1
        generation = self._generation

        def run() -> None:
            try:
                if kind == "readiness":
                    res = self._readiness_runner(self._cfg, self._clock, model, worker_expected)
                else:
                    res = self._runner(self._cfg, self._clock, model)
            except Exception as exc:  # noqa: BLE001 — a probe bug must not kill the loop
                log.exception("canary crashed")
                wall = self._clock.time()
                res = CanaryResult(False, "http_error", "canary_http_error", None, None, None, 0.0, 0, False,
                                   wall, wall, f"probe crashed: {type(exc).__name__}")
            res.kind = kind if kind == "readiness" else res.kind
            res.health_at_start = health_at_start
            with self._lock:
                if generation == self._generation:
                    self._result = res

        self._thread = threading.Thread(target=run, name="canary", daemon=True)
        self._thread.start()
        return True

    def discard(self) -> None:
        """Forget the probe in flight and any result not yet collected."""
        with self._lock:
            self._generation += 1
            self._result = None
        self.outstanding_since_mono = None

    def collect(self) -> Optional[CanaryResult]:
        with self._lock:
            res, self._result = self._result, None
        if res is not None and res.ok:
            self.outstanding_since_mono = None
        return res

    def join(self, timeout: float) -> bool:
        t = self._thread
        if t is None:
            return True
        t.join(timeout)
        return not t.is_alive()


# ---------------------------------------------------------------------------
# The worker sentinel, as seen from the head (contract §6.4)
# ---------------------------------------------------------------------------


class SentinelClient:
    def __init__(self, base_url: str, token: str, clock: Clock, probe_timeout_s: float = 5.0):
        self.base_url = base_url
        self._token = token
        self._clock = clock
        self._timeout = probe_timeout_s
        self._auth_error_logged = False

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        # The token goes on EVERY call: the sentinel requires it on all
        # endpoints once configured, not only on the mutation.
        hdrs = dict(extra or {})
        if self._token:
            hdrs["X-Sentinel-Token"] = self._token
        return hdrs

    def state(self) -> Tuple[Optional[dict], str]:
        """``(document, error_kind)`` — one of the two is meaningful. The
        kind is bounded (``refused|timeout|unreachable|broken|http_<status>|
        malformed``); the exception text goes to the log only."""
        try:
            status, body = fetch(self.base_url + "/state", headers=self._headers(), read_timeout=self._timeout,
                                 connect_timeout=self._timeout, clock=self._clock)
        except ConnectFailed as exc:
            log.debug("sentinel /state: %s", exc)
            return None, exc.kind
        except ReadTimeout:
            return None, "timeout"
        if status != 200:
            if status in (401, 403) and not self._auth_error_logged:
                self._auth_error_logged = True
                log.error("sentinel refused /state (HTTP %d): token mismatch — run `techsara up` to recreate "
                          "the controller with the token cluster-sync.sh shipped", status)
            return None, f"http_{status}"
        self._auth_error_logged = False
        try:
            doc = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return None, "malformed"
        return (doc if isinstance(doc, dict) else None), ("" if isinstance(doc, dict) else "malformed")

    def diagnostics(self) -> str:
        status, body = fetch(self.base_url + "/diagnostics", headers=self._headers(), read_timeout=20.0,
                             connect_timeout=self._timeout, clock=self._clock)
        if status != 200:
            raise ConnectFailed(f"diagnostics HTTP {status}", refused=False, kind="broken")
        return body.decode("utf-8", "replace")

    def restart(self) -> Tuple[str, dict]:
        """``(outcome, reply)`` with outcome in ``WORKER_RESTART_OUTCOMES``."""
        try:
            # docker restart t=5 plus the container's own start: allow well over it.
            status, body = fetch(self.base_url + "/restart", "POST", b"{}",
                                 self._headers({"Content-Type": "application/json"}),
                                 connect_timeout=self._timeout, read_timeout=120.0, clock=self._clock)
        except (ConnectFailed, ReadTimeout) as exc:
            log.error("sentinel restart unreachable: %s", exc)
            return "unreachable", {}
        if status in (401, 403):
            log.error("sentinel refused POST /restart (HTTP %d): token mismatch — run `techsara up` to recreate "
                      "the controller with the token cluster-sync.sh shipped", status)
            return "refused", {}
        if status not in (200, 202):
            log.error("sentinel restart failed: HTTP %d", status)
            return "failed", {}
        try:
            reply = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            reply = {}
        return "ok", reply if isinstance(reply, dict) else {}


# ---------------------------------------------------------------------------
# The controller
# ---------------------------------------------------------------------------


def _opt_float(value) -> Optional[float]:
    """A number from a peer's JSON, or ``None`` — never an exception and
    never a bool masquerading as one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN is not a reading


@dataclass
class Pending:
    """A trigger condition that has held for ``count`` consecutive ticks."""
    first_at: float
    count: int = 0
    detail: str = ""


@dataclass
class Recovery:
    in_progress: bool = False
    step: str = "idle"
    category: str = "none"
    reason: str = ""
    incident_id: Optional[str] = None
    attempt: int = 0
    started_at: Optional[float] = None      # wall
    detect_at: Optional[float] = None       # wall — first observation of the trigger
    restart_issued_at: Optional[float] = None   # wall — just before the head restart was asked for
    restart_at: Optional[float] = None      # wall — after STOP_STALE_PAIR returned
    deadline_mono: Optional[float] = None
    load_fault: Optional["Pending"] = None  # a worker fault seen during wait_load
    steps: List[dict] = field(default_factory=list)
    verifying: bool = False                 # VERIFY_STABILITY after MARK_READY
    blocked: str = ""                        # one of BLOCKED_KINDS
    blocked_detail: str = ""
    worker_restart: str = "none"             # one of WORKER_RESTART_OUTCOMES
    last: Optional[dict] = None


class Controller:
    """One instance; ``tick()`` is the whole observation → decision → action
    cycle and is the only thing that mutates state, from one thread. The
    HTTP handlers read the snapshot ``tick`` publishes."""

    def __init__(self, cfg: Config, docker: DockerClient, clock: Optional[Clock] = None,
                 canary: Optional[Canary] = None, sentinel: Optional[SentinelClient] = None):
        self.cfg = cfg
        self.docker = docker
        self.clock = clock or Clock()
        self.canary = canary or Canary(cfg, self.clock)
        self.sentinel = sentinel or SentinelClient(cfg.sentinel_url, cfg.sentinel_token, self.clock,
                                                   cfg.probe_timeout_s)
        self.budget = Budget(cfg.recovery_budget, cfg.recovery_window_s, self.clock)

        now = self.clock.time()
        self.state = "MONITORING_UNKNOWN"
        self.reason = "no observation yet"
        self.since = now
        self.transitions: List[dict] = []
        self.primary_ready = False

        # Signals — dicts because they are published as-is in /state.
        self.docker_ok: Optional[bool] = None
        self.docker_error = ""
        #: The Docker error kind last logged at WARNING: each kind is logged
        #: once when it appears (and an INFO when the API answers again),
        #: never once per tick — but never only at DEBUG either: a daemon
        #: that answers 400 to every inspect leaves the controller unable to
        #: observe or act, and that must be said out loud.
        self._docker_error_logged = ""
        self.head: dict = {"exists": None, "running": None, "status": None, "health": None,
                           "restart_count": None, "started_at": None, "finished_at": None,
                           "engine_process_alive": None, "rank_process_alive": None, "observed_at": None}
        self.api: dict = {"tcp": None, "tcp_error": None, "health": None, "models": None, "metrics": None,
                          "observed_at": None}
        self.engine: dict = {"requests_running": None, "requests_waiting": None, "generation_tokens_total": None,
                             "prompt_tokens_total": None, "frozen_seconds": 0.0, "observed_at": None}
        self.worker: dict = {"configured": bool(cfg.sentinel_url), "reachable": None, "container_running": None,
                             "health": None, "rank_process_alive": None, "rank_joined": None,
                             "restart_count": None, "started_at": None, "started_ago_s": None,
                             "clock_skew_s": None, "last_fault": None,
                             "self_restarts_in_window": None, "autonomous": None, "observed_at": None, "error": ""}
        #: Trigger 1(c)'s gate: the sentinel has reported the rank process
        #: alive at least once since the current head start. A rank never
        #: seen alive for this incarnation is "unknown", never "absent"
        #: (the same rule the head-side process table follows).
        self._worker_rank_seen_alive = False
        self.router: dict = {"configured": bool(cfg.router_health_url), "health": None, "observed_at": None}
        self.router_available = False
        self._router_next_mono: Optional[float] = None
        self.gpus: dict = {"head_util": None, "worker_util": None, "sampled_at": None}
        #: The host's MemAvailable (bytes) as of the last tick; ``None`` when
        #: /proc/meminfo could not be read (unobserved, never zero).
        self.head_memory: dict = {"available_bytes": None, "observed_at": None,
                                  "min_bytes": cfg.head_min_mem_available_bytes, "low": None}
        self._meminfo_error_logged = False
        #: The confirmed failure (category, first_at) the head-memory
        #: WARNING was already given for: the block lasts one tick per
        #: confirmation, never longer.
        self._mem_warned_for: Optional[Tuple[str, float]] = None

        self.model_id: str = cfg.canary_model
        self.last_canary: Optional[CanaryResult] = None
        self.last_success_at: Optional[float] = None
        self.consecutive_failures = 0
        self.consecutive_timeouts = 0
        #: Rule 6: consecutive canary HTTP errors the ENGINE answered (5xx,
        #: an error chunk, a stream without a terminal chunk, no tokens) on
        #: a proven engine; a 4xx or a probe crash is not counted.
        self.consecutive_http_errors = 0
        #: Rule 7's clock: when the first probe of the current failure
        #: streak STARTED (wall); ``None`` while the canary passes.
        self.failing_since: Optional[float] = None
        self._next_canary_mono: Optional[float] = None
        self._no_model_logged = False
        self.probes_total: Dict[str, int] = {k: 0 for k in PROBE_OUTCOMES}
        #: The proof that the current head incarnation passed the readiness
        #: sequence: {probe_started_at, passed_at, head_started_at}.
        self.proof: Optional[dict] = None
        self.readiness: Optional[ReadinessResult] = None
        self.participation_ok = False

        self._prev_tokens: Optional[Tuple[float, float]] = None
        self._last_progress_at = now
        self._prev_head_started_at: Optional[float] = None
        self._head_start_seen_at: Optional[float] = None
        self._head_procs_seen: Dict[str, bool] = {"engine": False, "rank": False}
        self._cold_start_flagged = False
        self._cold_start_measured_for: Optional[float] = None
        self._cold_start_detail_for: Optional[float] = None
        self.cold_start_detail = "none"

        self.pending: Dict[str, Pending] = {}
        self.confirmed: Optional[Tuple[str, str, float]] = None  # (category, detail, first_at)
        self.rec = Recovery()
        self.incident: Optional[dict] = None
        self.verify_successes = 0
        self.attempts_total: Dict[str, int] = {k: 0 for k in ATTEMPT_OUTCOMES}
        self.last_failure_category = "none"
        self.budget_exhausted = False
        self.cooldown_until: Optional[float] = None
        self.cold_start_s: Optional[float] = None
        self.recovery_duration_s: Optional[float] = None
        self._lock_fd: Optional[int] = None
        self._lock_error = ""
        self._lock_logged_mono: Optional[float] = None
        self._manual_pending = False
        self._manual_requested_at: Optional[float] = None
        self._confirm_logged: Optional[Tuple[str, float]] = None
        self._rank_wait_logged = False
        self._saturation_note = ""

        self._snap_lock = threading.Lock()
        self._snapshot: dict = {}
        self._heartbeat_stop: Optional[threading.Event] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._publish(now)

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    def _new_head_incarnation(self, now: float, started_at: float, info: dict) -> None:
        """Everything learned about the previous container is void: probe
        counters, the last result, the proof, pending triggers. Timeouts
        accumulated while a head loaded must never confirm a wedge on the
        first tick its /health answers 200 (the 2026-09-12 review blocker).

        The FIRST observation of a start (the controller just came up, or
        the Docker socket was unobservable until now) is bookkeeping only:
        the probe state built meanwhile is about this very container, and
        ``_proven_since_head_start`` compares the proof against the start
        on its own."""
        first = self._prev_head_started_at is None
        if not first:
            log.info("head container restarted: started_at %.0f -> %.0f (restart_count %s)",
                     self._prev_head_started_at, started_at, info.get("RestartCount"))
        self._prev_head_started_at = started_at
        # The cold-start budget counts from when WE first saw this start:
        # a controller (re)deployed next to an hour-old healthy head must
        # not call it DOWN on its first tick for lack of history.
        self._head_start_seen_at = now
        self._prev_tokens = None
        self._cold_start_flagged = False
        self._cold_start_detail_for = None
        self.cold_start_detail = "none"
        self._head_procs_seen = {"engine": False, "rank": False}
        self._worker_rank_seen_alive = False
        if first:
            return
        self.consecutive_failures = 0
        self.consecutive_timeouts = 0
        self.consecutive_http_errors = 0
        self.failing_since = None
        self.last_canary = None
        self.proof = None
        self.readiness = None
        self.participation_ok = False
        self._saturation_note = ""
        self._rank_wait_logged = False
        self.pending.clear()
        self.canary.discard()
        self._next_canary_mono = None

    def _docker_unusable(self, exc: DockerError) -> None:
        """The Docker API cannot be used to observe the head: keep the last
        known head facts (``started_at`` in particular: the readiness rule
        compares against it), publish the bounded kind, and say so at
        WARNING — once per kind, not once per tick. While this holds no
        trigger that needs ``docker_ok`` can fire and no recovery can
        start; a controller that only whispered it at DEBUG sat in
        MONITORING_UNKNOWN for good with ``docker_error`` as the only clue."""
        self.docker_ok = False
        self.docker_error = exc.kind
        if exc.kind != self._docker_error_logged:
            self._docker_error_logged = exc.kind
            hint = ""
            if not isinstance(exc, DockerUnavailable) and 400 <= exc.status < 500:
                hint = (f" (a 4xx from the daemon usually means DOCKER_API_VERSION={self.cfg.docker_api_version} "
                        f"is not accepted by it)")
            log.warning("docker API unusable: %s — %s%s. The head cannot be observed and no recovery can start "
                        "until this clears; the state is MONITORING_UNKNOWN (DEGRADED while a completion is fresh)",
                        exc.kind, exc, hint)

    def _docker_usable(self) -> None:
        self.docker_ok = True
        self.docker_error = ""
        if self._docker_error_logged:
            log.info("docker API answering again (was %s)", self._docker_error_logged)
            self._docker_error_logged = ""

    def _observe_head(self, now: float) -> None:
        try:
            info = self.docker.inspect(self.cfg.head_container)
        except DockerUnavailable as exc:
            self._docker_unusable(exc)
            return
        except DockerError as exc:
            if exc.status == 404:
                self._docker_usable()
                self.head.update({"exists": False, "running": False, "status": "absent", "health": None,
                                  "engine_process_alive": None, "rank_process_alive": None, "observed_at": now})
                return
            self._docker_unusable(exc)
            return
        self._docker_usable()
        state = info.get("State") or {}
        running = bool(state.get("Running"))
        started_at = parse_docker_time(state.get("StartedAt"))
        if started_at is not None and self._prev_head_started_at != started_at:
            self._new_head_incarnation(now, started_at, info)
        engine_alive = rank_alive = None
        if running:
            try:
                procs = self.docker.top(info.get("Id") or self.cfg.head_container)
            except DockerError:
                procs = None  # 409 while the container is between states: unknown, not dead
            if procs is not None:
                # "Seen alive, then vanished" per incarnation. A name never
                # observed for this container is no evidence of anything:
                # a TP=1 engine (the ``uni`` executor) has no
                # ``VLLM::Worker_TP`` process and must never be restarted
                # for lacking one.
                present = {
                    "engine": any("vllm serve" in p for p in procs) and any(ENGINE_CORE_TITLE in p for p in procs),
                    "rank": any(RANK_PROCESS_TITLE in p for p in procs),
                }
                verdict: Dict[str, Optional[bool]] = {}
                for key, seen in present.items():
                    if seen:
                        self._head_procs_seen[key] = True
                        verdict[key] = True
                    elif self._head_procs_seen[key]:
                        verdict[key] = False
                    else:
                        verdict[key] = None
                engine_alive, rank_alive = verdict["engine"], verdict["rank"]
        self.head.update({
            "exists": True,
            "running": running,
            "status": state.get("Status"),
            "health": (state.get("Health") or {}).get("Status"),
            "restart_count": int(info.get("RestartCount") or 0),
            "started_at": started_at,
            "finished_at": parse_docker_time(state.get("FinishedAt")),
            "engine_process_alive": engine_alive,
            "rank_process_alive": rank_alive,
            "observed_at": now,
        })

    def _get_status(self, path: str) -> Tuple[Optional[int], Optional[bytes], str]:
        try:
            status, body = fetch(self.cfg.head_api_url + path, connect_timeout=self.cfg.probe_timeout_s,
                                 read_timeout=self.cfg.probe_timeout_s, clock=self.clock)
            return status, body, ""
        except ReadTimeout:
            return None, None, "timeout"
        except ConnectFailed as exc:
            return None, None, exc.kind

    def _observe_api(self, now: float) -> None:
        host, port, _ = split_url(self.cfg.head_api_url)
        ok, kind = tcp_connect(host, port, self.cfg.probe_timeout_s)
        api = {"tcp": ok, "tcp_error": None if ok else kind, "health": None, "models": None, "metrics": None,
               "observed_at": now}
        sample = None
        if ok:
            api["health"], _, _ = self._get_status("/health")
            status, body, _ = self._get_status("/v1/models")
            api["models"] = status
            if status == 200 and body:
                try:
                    data = json.loads(body.decode("utf-8", "replace")).get("data") or []
                    if data and not self.cfg.canary_model:
                        self.model_id = str(data[0].get("id") or "")
                except (ValueError, AttributeError):
                    pass
            status, body, _ = self._get_status("/metrics")
            api["metrics"] = status
            if status == 200 and body:
                sample = parse_vllm_metrics(body.decode("utf-8", "replace"))
                if any(v is None for v in sample.values()):
                    sample = None
                    api["metrics"] = 0  # answered, but without the engine series: not usable
        self.api = api
        if sample is not None:
            self._engine_sample(now, sample)

    def _engine_sample(self, now: float, s: Dict[str, Optional[float]]) -> None:
        """Signal 12: are the token counters moving while requests are running?

        Either counter moving is progress (a long prefill moves only
        ``prompt_tokens_total``). With nothing running there is nothing to
        freeze, so the clock is reset — a freeze is measured from the moment
        requests appeared and the counters did not follow.
        """
        running = float(s["requests_running"] or 0.0)
        tokens = (float(s["generation_tokens_total"] or 0.0), float(s["prompt_tokens_total"] or 0.0))
        moved = self._prev_tokens is None or tokens != self._prev_tokens
        if moved or running <= 0:
            self._last_progress_at = now
        self._prev_tokens = tokens
        self.engine = {
            "requests_running": running,
            "requests_waiting": float(s["requests_waiting"] or 0.0),
            "generation_tokens_total": tokens[0],
            "prompt_tokens_total": tokens[1],
            "frozen_seconds": max(0.0, now - self._last_progress_at) if running > 0 else 0.0,
            "observed_at": now,
        }

    def _observe_worker(self, now: float) -> None:
        if not self.sentinel.configured:
            return
        doc, err = self.sentinel.state()
        if doc is None:
            self.worker.update({"reachable": False, "container_running": None, "health": None,
                                "rank_process_alive": None, "rank_joined": None, "restart_count": None,
                                "started_at": None, "started_ago_s": None, "clock_skew_s": None,
                                "last_fault": None, "self_restarts_in_window": None,
                                "autonomous": None, "observed_at": now, "error": err})
            return
        container = doc.get("container") or {}
        fault = doc.get("last_fault")
        if not isinstance(fault, dict):
            fault = None
        rank_alive = doc.get("rank_process_alive")
        if rank_alive is True:
            self._worker_rank_seen_alive = True
        # Two clocks (§6.3 trigger 1(a)): the sentinel's ``started_at`` and
        # ``observed_at`` are Node 2's; ``started_ago_s`` (observed_at −
        # started_at, both on Node 2) is a duration, which crosses hosts
        # intact. ``clock_skew_s`` is what an operator reads when NTP fails.
        started_ago = _opt_float(doc.get("started_ago_s"))
        observed = _opt_float(doc.get("observed_at"))
        self.worker.update({
            "reachable": True,
            "container_running": container.get("running"),
            "health": container.get("health"),
            "rank_process_alive": rank_alive,
            "rank_joined": doc.get("rank_joined"),
            "restart_count": container.get("restart_count"),
            "started_at": container.get("started_at"),
            "started_ago_s": started_ago if (started_ago is not None and started_ago >= 0) else None,
            "clock_skew_s": (observed - now) if observed is not None else None,
            "last_fault": fault,
            "self_restarts_in_window": doc.get("self_restarts_in_window"),
            "autonomous": doc.get("autonomous"),
            "observed_at": now,
            "error": "",
        })

    def _observe_router(self, now: float, mono: float) -> None:
        """The router is an internal classifier only (v2 §1): its health is
        a DEGRADED signal for routing, never a serving path."""
        if not self.cfg.router_health_url:
            return
        if self._router_next_mono is not None and mono < self._router_next_mono:
            return
        self._router_next_mono = mono + self.cfg.router_probe_interval_s
        try:
            status, _ = fetch(self.cfg.router_health_url, connect_timeout=self.cfg.probe_timeout_s,
                              read_timeout=self.cfg.probe_timeout_s, clock=self.clock)
        except (ConnectFailed, ReadTimeout):
            status = None
        self.router = {"configured": True, "health": status, "observed_at": now}
        self.router_available = status == 200

    def _observe_head_memory(self, now: float) -> None:
        """The host's ``MemAvailable``, every tick (a 64 KiB read of
        ``/proc/meminfo``): published as ``signals.head_memory`` and the
        ``techsara_vllm_head_mem_available_bytes`` gauge, and consulted
        once per confirmed failure before STOP_STALE_PAIR."""
        available = read_mem_available(self.cfg.meminfo_path)
        if available is None and not self._meminfo_error_logged:
            self._meminfo_error_logged = True
            log.warning("cannot read MemAvailable from %s: the head-memory precondition is unobserved "
                        "(never a block)", self.cfg.meminfo_path)
        elif available is not None:
            self._meminfo_error_logged = False
        minimum = self.cfg.head_min_mem_available_bytes
        self.head_memory = {
            "available_bytes": available,
            "observed_at": now if available is not None else self.head_memory.get("observed_at"),
            "min_bytes": minimum,
            "low": (available < minimum) if (available is not None and minimum > 0) else None,
        }

    def _harvest_canary(self, now: float) -> None:
        res = self.canary.collect()
        if res is None:
            return
        self.probes_total[res.outcome] = self.probes_total.get(res.outcome, 0) + 1
        head_started = self.head.get("started_at")
        if head_started is not None and res.started_at < float(head_started):
            # Started against the previous container: whatever it says is
            # about a process that no longer exists. Neither a success nor
            # a timeout from it may count for this incarnation.
            log.info("canary %s from a probe started before the current head start: ignored", res.outcome)
            return
        self.last_canary = res
        if res.readiness is not None:
            self._record_readiness(res, now, head_started)
        if res.ok:
            self.last_success_at = res.at
            self.consecutive_failures = 0
            self.consecutive_timeouts = 0
            self.consecutive_http_errors = 0
            self.failing_since = None
            proven = self._proven_since_head_start()
            # VERIFY_STABILITY by any path: an open incident ends on three
            # consecutive successes on a PROVEN engine whether the last
            # attempt succeeded, failed, or the budget was spent and the
            # engine healed itself.
            if (proven and self.incident is not None and self.incident.get("ended_at") is None
                    and not self.rec.in_progress):
                self.verify_successes += 1
                if self.verify_successes >= 3:
                    self._end_incident(now)
            if self.budget_exhausted and proven:
                log.info("readiness proven while the recovery budget is exhausted: leaving DOWN on its own")
                self.budget_exhausted = False
        else:
            self.consecutive_failures += 1
            self.verify_successes = 0
            if self.failing_since is None:
                # Rule 7's clock starts when the first probe of the streak
                # started: the engine has not completed a canary since.
                self.failing_since = res.started_at
            h = res.health_at_start
            # Rules 5 and 6 count a failure only on a PROVEN engine whose
            # /health was not 5xx when the probe started: failures while a
            # head loads are STARTING (the cold-start budget's business), a
            # 5xx is trigger 2's. /health silent (None) with TCP up DOES
            # count — that is the hung-API shape.
            countable = self._proven_since_head_start() and not (h is not None and 500 <= int(h) < 600)
            kind = canary_engine_fault_kind(res)
            if res.outcome == "timeout":
                self.consecutive_http_errors = 0
                if countable:
                    self.consecutive_timeouts += 1
                else:
                    log.info("canary timeout not counted toward canary_timeout (proven=%s, health at start=%s)",
                             self._proven_since_head_start(), h)
            elif kind == "canary_http_error":
                self.consecutive_timeouts = 0
                if countable:
                    self.consecutive_http_errors += 1
                else:
                    log.info("canary http_error not counted toward canary_http_error (proven=%s, health at start=%s)",
                             self._proven_since_head_start(), h)
            else:
                # A connect error, a 4xx or a probe crash: neither streak.
                self.consecutive_timeouts = 0
                self.consecutive_http_errors = 0
            # The confirming second probe should not wait out a slow interval
            # scheduled while everything looked fine.
            mono = self.clock.mono()
            if self._next_canary_mono is not None:
                self._next_canary_mono = min(self._next_canary_mono, mono + self.cfg.canary_interval_fast_s)
            log.warning("canary %s: %s (consecutive failures %d)", res.outcome, res.detail,
                        self.consecutive_failures)

    def _record_readiness(self, res: CanaryResult, now: float, head_started: Optional[float]) -> None:
        rd = res.readiness
        assert rd is not None
        # Step 5 — the sentinel's verdict on the rank, as of this tick.
        w = self.worker
        if w["configured"] and w.get("reachable"):
            rd.rank_alive = w.get("rank_process_alive")
        else:
            rd.rank_alive = None
        self.readiness = rd
        self.participation_ok = rd.participation == "ok"
        if rd.gpus:
            self.gpus = {"head_util": rd.gpus.get("head_util"), "worker_util": rd.gpus.get("worker_util"),
                         "sampled_at": rd.gpus.get("sampled_at")}
        if rd.passed and rd.rank_alive is False:
            if not self._rank_wait_logged:
                self._rank_wait_logged = True
                log.info("readiness steps 1-4 passed but the sentinel does not see the rank process (seen alive for "
                         "this head start: %s); waiting", self._worker_rank_seen_alive)
            rd.passed = False
            rd.failed_step = "rank_alive"
            rd.detail = "sentinel reports no VLLM::Worker process on the worker"
            return
        if not rd.passed:
            log.warning("readiness sequence failed at %s: %s", rd.failed_step, rd.detail)
            return
        self._rank_wait_logged = False
        self.proof = {"probe_started_at": res.started_at, "passed_at": res.at, "head_started_at": head_started}
        probes = " ".join(
            f"probe{p.get('probe')}=ttft {p.get('ttft_s') if p.get('ttft_s') is not None else -1:.2f}s/"
            f"total {p.get('total_s') or 0:.2f}s/{p.get('tokens')}tok" for p in rd.participation_probes)
        log.info("readiness sequence passed: non_stream=%s stream=%s progress=%s (delta %s for %d) "
                 "participation=%s (head %s%%, worker %s%%; %d concurrent %s) rank_alive=%s in %.1fs",
                 rd.non_stream_ok, rd.stream_ok, rd.progress_ok, rd.tokens_delta, rd.tokens_expected,
                 rd.participation, self.gpus.get("head_util"), self.gpus.get("worker_util"),
                 len(rd.participation_probes), probes or "(no probes)", rd.rank_alive, res.at - res.started_at)
        if head_started is not None and self._cold_start_measured_for != head_started:
            # cold_start_seconds: head start -> first proof of THAT start,
            # measured once; a DEGRADED->READY flap hours later must not
            # overwrite it with the head's uptime. Only a start this
            # controller watched from the beginning is measured: redeployed
            # next to an hour-old head, its own first proof is not the
            # head's cold start.
            self._cold_start_measured_for = head_started
            seen = self._head_start_seen_at
            measured = res.at - float(head_started)
            if measured >= 0 and seen is not None and seen - float(head_started) <= COLD_START_WATCH_GRACE_S:
                self.cold_start_s = measured
            else:
                log.info("cold start not measured: this head start predates the controller by %.0fs",
                         (seen or res.at) - float(head_started))

    # ------------------------------------------------------------------
    # Readiness helpers
    # ------------------------------------------------------------------

    def _proven_since_head_start(self) -> bool:
        """The readiness sequence passed for the head container's CURRENT
        start — the sequence started after the container did."""
        if self.proof is None:
            return False
        started = self.head.get("started_at")
        return started is None or float(self.proof["probe_started_at"]) >= float(started)

    def _canary_interval(self) -> float:
        # §5: 10 s while STARTING/RECOVERING/WEDGED, 30 s while READY/BUSY.
        # Also fast while the sequence is still owed for this start and
        # while a failing canary awaits its confirming second probe — the
        # road to WEDGED runs through DEGRADED.
        if (self.state in ("STARTING", "RECOVERING", "WEDGED") or not self._proven_since_head_start()
                or self.consecutive_failures > 0):
            return self.cfg.canary_interval_fast_s
        return self.cfg.canary_interval_s

    def _proven_recent(self, now: float) -> bool:
        """READY's freshness rule: proven for this start, and a success
        within the last two probe intervals (the slow one — 60 s by
        default). A canary still in flight does NOT extend it: a probe that
        has not answered is not evidence."""
        if not self._proven_since_head_start():
            return False
        return (now - (self.last_success_at or 0.0)) <= 2 * self.cfg.canary_interval_s

    # ------------------------------------------------------------------
    # Detection (contract §6.3)
    # ------------------------------------------------------------------

    def _worker_fault_after_head_start(self) -> str:
        """Trigger 1's fault-signature half (§6.3): a fatal signature the
        sentinel saw in the worker's CURRENT incarnation, logged after the
        current head started. No ``proven`` gate — this is what catches a
        rank dying while the head is still loading (the review's 900 s
        cold_start_timeout with two attempts left unused)."""
        w = self.worker
        if not (w["configured"] and w.get("reachable") and self.head.get("running")):
            return ""
        fault = w.get("last_fault") or {}
        hs = self.head.get("started_at")
        ws = w.get("started_at")
        at = fault.get("at")
        if not at or hs is None:
            return ""
        if float(at) > float(hs) and (ws is None or float(at) > float(ws)):
            return f"worker fault '{fault.get('signature')}' logged after the current head start"
        return ""

    def _trigger_conditions(self, now: float, mono: float) -> Dict[str, Tuple[bool, str]]:
        cfg = self.cfg
        proven = self._proven_since_head_start()
        head = self.head
        api = self.api
        w = self.worker
        out: Dict[str, Tuple[bool, str]] = {}

        # 1. worker rank dead — one observation: the sentinel already saw it.
        # (a) "worker started_at newer than the head's" (§6.3) is applied as
        # "newer than the last PROVEN completion" — itself newer than the
        # head's start once proven. A completion proves both ranks were
        # participating at that moment, so a worker container that
        # (re)started after it means the rank that participated is gone; a
        # worker that legitimately started seconds after the head (either
        # order happens under the last-resort healthchecks) but then served
        # completions is left alone. The comparison is made on ONE clock:
        # the sentinel reports how long ago the worker started (a duration
        # measured on Node 2 alone) and the controller compares it with
        # how long ago its own last proven completion was — Node 2's wall
        # clock never meets Node 1's. An older sentinel without
        # ``started_ago_s`` falls back to the two-clock comparison.
        # (b) a fatal signature in the worker's current incarnation after
        # the current head start — no proven gate. (c) no VLLM::Worker
        # process at all past the grace, on a proven engine, and only once
        # the sentinel has reported the rank alive for THIS head
        # incarnation (a name never observed alive is unknown, not absent —
        # the head-side process rule, applied to the worker too).
        c1, d1 = False, ""
        if w["configured"] and w["reachable"]:
            ws = w.get("started_at")
            ago = w.get("started_ago_s")
            fault_detail = self._worker_fault_after_head_start()
            restarted_after_proof, d1a = False, ""
            if proven and self.last_success_at is not None:
                since_success = now - float(self.last_success_at)
                if ago is not None:
                    # The sentinel's document is at most one of its polls old,
                    # which can only make ``ago`` SMALLER than the truth; a
                    # false positive would need a TP=2 completion finishing
                    # within that poll of the worker container's start, which
                    # a rank that has to load the model cannot do.
                    if float(ago) < since_success:
                        restarted_after_proof = True
                        d1a = (f"worker container started {since_success - float(ago):.0f}s after the last "
                               f"proven completion (sentinel clock)")
                elif ws and float(ws) > float(self.last_success_at):
                    restarted_after_proof = True
                    d1a = (f"worker container started {float(ws) - float(self.last_success_at):.0f}s after the "
                           f"last proven completion (two clocks: the sentinel sent no started_ago_s)")
            if restarted_after_proof:
                c1, d1 = True, d1a
            elif fault_detail:
                c1, d1 = True, fault_detail
            elif (proven and self._worker_rank_seen_alive and w.get("rank_process_alive") is False
                  and w.get("container_running")
                  and ((ago is not None and float(ago) >= cfg.worker_rank_grace_s)
                       or (ago is None and ws and now - float(ws) >= cfg.worker_rank_grace_s))):
                c1, d1 = True, "sentinel reports the rank process absent (seen alive for this head start)"
        out["worker_rank_dead"] = (c1, d1)

        # 2. head engine dead — /health 5xx, one observation …
        health = api.get("health")
        c2 = health is not None and 500 <= int(health) < 600
        out["head_engine_dead"] = (c2, f"/health {health}" if c2 else "")
        # … and its process-table variant: EngineCore or the head's rank
        # process SEEN ALIVE FOR THIS INCARNATION and now gone, on a proven
        # engine. On 2026-09-11 07:05Z rank 0 died and the head sat in a
        # collective for three minutes before vLLM noticed; the process
        # table knew at once. Two ticks, so one odd `top` sample cannot
        # restart anything. ``None`` (never seen) is not ``False``.
        c2p = bool(proven and head.get("running") and self.docker_ok
                   and (head.get("engine_process_alive") is False or head.get("rank_process_alive") is False))
        out["head_engine_dead:process"] = (c2p, "engine/rank process vanished from the head's process table"
                                           if c2p else "")

        # 3. head API dead — refused/connect error after READY, two observations ≥ 10 s apart.
        c3 = bool(proven and self.docker_ok and head.get("running") and api.get("tcp") is False)
        out["head_api_dead"] = (c3, f"tcp {api.get('tcp_error')}" if c3 else "")

        # 4. wedged — frozen counters with requests running and the canary outstanding, health still 200.
        eng = self.engine
        fresh = eng.get("observed_at") == now
        outstanding = self.canary.outstanding_s(mono)
        c4 = bool(fresh and (eng.get("requests_running") or 0) > 0 and eng.get("frozen_seconds", 0.0) >= cfg.frozen_s
                  and outstanding >= cfg.canary_outstanding_s and health == 200)
        out["wedged_frozen_tokens"] = (
            c4, f"tokens frozen {eng.get('frozen_seconds', 0):.0f}s with {eng.get('requests_running')} running, "
                f"canary outstanding {outstanding:.0f}s" if c4 else "")

        # 5. two consecutive canary timeouts on a PROVEN engine — whatever
        # /health says (a hung API answers nothing, and that is exactly the
        # case; 5xx was excluded when the timeouts were counted) — unless
        # the engine is demonstrably PROGRESSING on a FRESH /metrics sample.
        # vLLM's scheduler is first-come-first-served over an 8,192-token
        # step budget: nine long prefills from the second tenant can starve
        # a new request for a couple of minutes while every counter moves.
        # That is saturation, not a wedge, and restarting it would cost five
        # minutes for nothing. The exemption is bounded: past
        # CANARY_STARVATION_S the engine that will not admit a four-token
        # request is treated as wedged anyway.
        c5 = bool(proven and self.consecutive_timeouts >= 2)
        d5 = f"{self.consecutive_timeouts} consecutive canary timeouts" if c5 else ""
        progressing = bool(fresh and (eng.get("requests_running") or 0) > 0
                           and eng.get("frozen_seconds", 0.0) < cfg.frozen_s)
        starved = progressing and outstanding < cfg.canary_starvation_s
        if c5 and starved:
            c5 = False
            self._saturation_note = (f"canary timed out twice but the engine is progressing "
                                     f"({eng.get('requests_running'):.0f} running, "
                                     f"{eng.get('requests_waiting') or 0:.0f} waiting): saturation, not a wedge")
        else:
            self._saturation_note = ""
        out["canary_timeout"] = (c5, d5)

        # 6. three consecutive canary HTTP errors the ENGINE answered (5xx,
        # an error chunk, a stream without a terminal chunk, no tokens) on a
        # proven engine whose /health was not 5xx when each probe started
        # (a 5xx is trigger 2's). The three probes are the observations.
        # The saturation exemption does NOT apply: an error is an answer,
        # not a request starved of a scheduler slot. Without this rule a
        # head that answers /health, /v1/models and /metrics 200 while
        # every completion fails fast sat in DEGRADED — a SERVING state to
        # the orchestrator — for good.
        res = self.last_canary
        c6 = bool(proven and self.consecutive_http_errors >= 3)
        d6 = ""
        if c6:
            d6 = (f"{self.consecutive_http_errors} consecutive canary HTTP errors"
                  f" (last: HTTP {res.http_status}, {res.detail})" if res is not None else
                  f"{self.consecutive_http_errors} consecutive canary HTTP errors")
        out["canary_http_error"] = (c6, d6)

        # 7. the bound on DEGRADED "awaiting confirmation": a proven engine
        # that has failed every canary for CANARY_FAIL_DEGRADED_MAX_S while
        # /health answers 200 is a wedge (§2: the canary cannot get through
        # while the API says fine), whichever mix of failure kinds got it
        # there — timeouts alternating with errors confirm neither rule 5
        # nor 6. The category follows the LAST failure's kind; the
        # saturation exemption still holds for timeouts (a starved canary
        # on a progressing engine, bounded by CANARY_STARVATION_S).
        kind = canary_engine_fault_kind(res) if res is not None else ""
        failing_for = (now - float(self.failing_since)) if self.failing_since is not None else 0.0
        c7 = bool(proven and kind and health == 200 and failing_for >= cfg.canary_fail_degraded_max_s
                  and not (kind == "canary_timeout" and starved))
        for key in ("canary_failing:timeout", "canary_failing:http_error"):
            out[key] = (False, "")
        if c7:
            out["canary_failing:timeout" if kind == "canary_timeout" else "canary_failing:http_error"] = (
                True, f"every canary failed for {failing_for:.0f}s (≥ {cfg.canary_fail_degraded_max_s:.0f}s) "
                      f"while /health answers 200; last: {res.outcome} ({res.detail})")
        return out

    #: (pending key, category, observations needed, minimum seconds between first and confirming observation)
    _RULES: Tuple[Tuple[str, str, int, float], ...] = (
        ("worker_rank_dead", "worker_rank_dead", 1, 0.0),
        ("head_engine_dead", "head_engine_dead", 1, 0.0),
        ("head_engine_dead:process", "head_engine_dead", 2, 0.0),
        ("head_api_dead", "head_api_dead", 2, -1.0),   # -1: use cfg.head_api_dead_gap_s
        ("wedged_frozen_tokens", "wedged_frozen_tokens", 2, 0.0),
        ("canary_timeout", "canary_timeout", 1, 0.0),  # the two observations are the two probes
        ("canary_http_error", "canary_http_error", 1, 0.0),  # the three observations are the three probes
        ("canary_failing:timeout", "canary_timeout", 1, 0.0),       # rule 7: the streak's length is the confirmation
        ("canary_failing:http_error", "canary_http_error", 1, 0.0),
    )

    def _detect(self, now: float, mono: float) -> None:
        conditions = self._trigger_conditions(now, mono)
        for key, (active, detail) in conditions.items():
            if active:
                p = self.pending.get(key)
                if p is None:
                    p = self.pending[key] = Pending(first_at=now)
                    log.info("DETECT %s: %s", key, detail)
                p.count += 1
                p.detail = detail
            else:
                self.pending.pop(key, None)
        self.confirmed = None
        for key, category, need, gap in self._RULES:
            p = self.pending.get(key)
            if p is None:
                continue
            min_gap = self.cfg.head_api_dead_gap_s if gap < 0 else gap
            if p.count >= need and (now - p.first_at) >= min_gap:
                self.confirmed = (category, p.detail, p.first_at)
                break
        if self.confirmed is not None:
            self.rec.step = "confirm"
        elif self.pending:
            self.rec.step = "detect"
        else:
            # Back to what the post-recovery phase was, not blindly to idle:
            # a pending trigger that flickered during VERIFY_STABILITY must
            # not lose the verification.
            self.rec.step = "verify" if self.rec.verifying else "idle"
            self.rec.blocked = ""
            self.rec.blocked_detail = ""

    # ------------------------------------------------------------------
    # Acting on a confirmed failure
    # ------------------------------------------------------------------

    def _take_manual(self) -> Optional[float]:
        with self._snap_lock:
            pending, self._manual_pending = self._manual_pending, False
            at, self._manual_requested_at = self._manual_requested_at, None
        return at if pending else None

    def _block(self, kind: str, detail: str, category: str) -> None:
        self.rec.blocked = kind
        self.rec.blocked_detail = detail
        self.last_failure_category = category

    def _act_on_confirmed(self, now: float, mono: float) -> None:
        manual_at = self._take_manual()
        manual = manual_at is not None
        if manual:
            category, detail, first_at = "manual", "POST /recover from loopback", float(manual_at)
        elif self.confirmed is not None:
            category, detail, first_at = self.confirmed
        else:
            return
        if not manual:
            if self._confirm_logged != (category, first_at):
                self._confirm_logged = (category, first_at)
                log.warning("CONFIRM %s: %s", category, detail)
            # Publish the failure itself before RECOVERING so the transition
            # history reads DETECT → CONFIRM → RECOVERING, not a jump.
            self._set_state(CONFIRMED_STATE.get(category, "DOWN"), f"{category}: {detail}", now)
        # The budget and the cooldown apply to a manual recovery exactly as
        # to an automatic one (v2 §6): the escalation past them is the
        # runbook's cluster-recover.sh --force under the engine lock, not
        # this endpoint. POST /recover already refused synchronously; this
        # is the race with an automatic recovery between accept and tick.
        if self.cooldown_until is not None and now < self.cooldown_until:
            left = self.cooldown_until - now
            self._block("cooldown", f"{left:.0f}s left", category)
            if manual:
                log.warning("manual recovery dropped: cooldown (%.0fs left)", left)
            return
        if self.budget.exhausted():
            if not self.budget_exhausted:
                self.budget_exhausted = True
                self.attempts_total["budget_exhausted"] += 1
                log.error("recovery budget exhausted (%d in %.0fs): %s confirmed but NO further destructive "
                          "action; evidence preserved, queued requests stay queued, a human is needed",
                          self.budget.used(), self.cfg.recovery_window_s, category)
            self._block("budget_exhausted", f"{self.budget.used()}/{self.cfg.recovery_budget}", "budget_exhausted")
            if manual:
                log.warning("manual recovery dropped: recovery budget exhausted")
            return
        if self._head_memory_low_first_tick(category, first_at, manual, manual_at):
            return
        if not self._try_lock():
            kind = "lock_unavailable" if self._lock_error else "lock_held"
            self._block(kind, self._lock_error or "another actor holds the recovery lock", category)
            if self._lock_logged_mono is None or mono - self._lock_logged_mono >= 60:
                log.warning("%s: standing by (%s confirmed)", self.rec.blocked_detail, category)
                self._lock_logged_mono = mono
            if manual:
                # The operator's request stays queued until the lock frees.
                with self._snap_lock:
                    self._manual_pending = True
                    self._manual_requested_at = manual_at
            return
        self.rec.blocked = ""
        self.rec.blocked_detail = ""
        self._start_recovery(category, detail, first_at, now, mono)

    # -- the head-memory precondition -----------------------------------

    def _head_memory_low_first_tick(self, category: str, first_at: float, manual: bool,
                                    manual_at: Optional[float]) -> bool:
        """The precondition of STOP_STALE_PAIR: is the head short of the
        memory a fresh model load needs? A fresh load reads 21.8 GiB of
        weights through the page cache and the rank allocates ~25 GiB; on
        2026-09-12 07:11 IST a new CUDA context failed with
        ``NV_ERR_NO_MEMORY`` at exactly that edge while ``free`` still
        showed 32 GiB. Below ``HEAD_MIN_MEM_AVAILABLE_BYTES`` the controller
        WARNS with the number and the runbook pointer and holds for ONE
        tick (``recovery.blocked = head_memory_low``); on the next tick the
        same confirmed failure proceeds regardless — a dead engine must
        still be restarted, the warning is what the operator needs — so
        nothing here can block indefinitely. Unobserved memory (no
        ``/proc/meminfo``) never blocks. Returns True when this tick is the
        one being held."""
        minimum = self.cfg.head_min_mem_available_bytes
        available = self.head_memory.get("available_bytes")
        if minimum <= 0 or available is None or available >= minimum:
            return False
        ident = (category, float(first_at))
        if self._mem_warned_for == ident:
            log.info("head MemAvailable still %.1f GiB (< %.0f GiB) at the next tick: proceeding with the %s "
                     "recovery anyway — a dead engine must be restarted", available / GIB, minimum / GIB, category)
            return False
        self._mem_warned_for = ident
        detail = f"MemAvailable {available / GIB:.1f} GiB < {minimum / GIB:.0f} GiB"
        log.warning("head memory low before STOP_STALE_PAIR: MemAvailable %.1f GiB (%d bytes) is below "
                    "HEAD_MIN_MEM_AVAILABLE_BYTES %.0f GiB — a fresh model load reads 21.8 GiB of weights and "
                    "allocates ~25 GiB, and on 2026-09-12 07:11 IST a new CUDA context failed with "
                    "NV_ERR_NO_MEMORY at this edge; see %s. Holding one tick; the %s recovery proceeds at the "
                    "next tick if the failure is still confirmed",
                    available / GIB, available, minimum / GIB, HEAD_MEMORY_RUNBOOK, category)
        self._block("head_memory_low", detail, category)
        if manual:
            # The operator's request is held for the same one tick, not dropped.
            with self._snap_lock:
                self._manual_pending = True
                self._manual_requested_at = manual_at
        return True

    # -- the lock -------------------------------------------------------

    def _try_lock(self) -> bool:
        if self._lock_fd is not None:
            return True
        path = self.cfg.lock_path
        parent = os.path.dirname(path) or "."
        if not os.path.isdir(parent):
            # A missing bind mount must fail SAFE: a lock created inside our
            # own overlay would be one nobody on the host contends for —
            # the two-process-groups failure of 2026-09-11 all over again.
            self._lock_error = "recovery lock directory missing (bind mount?)"
            return False
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
        except OSError as exc:
            self._lock_error = "recovery lock unavailable"
            log.error("cannot open the recovery lock %s: %s", path, exc)
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            self._lock_error = ""
            return False
        except OSError as exc:
            os.close(fd)
            self._lock_error = "recovery lock unavailable"
            log.error("flock failed on %s: %s", path, exc)
            return False
        # It is a lock, not a secret: the checkout owner's shell wrappers
        # (scripts/lib/engine-lock.sh, run as techsphere) open the same file
        # with `exec {fd}>>` and must not be refused by a root-owned 0644
        # created by this container.
        _adopt_owner(fd, parent, log)
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"engine-controller pid={os.getpid()} at={self.clock.time():.0f}\n".encode())
        except OSError:
            pass
        self._lock_fd = fd
        self._lock_error = ""
        return True

    def _release_lock(self) -> None:
        if self._lock_fd is None:
            return
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._lock_fd)
        except OSError:
            pass
        self._lock_fd = None

    # -- the choreography (contract §6.3) --------------------------------

    def _step(self, step: str) -> None:
        # Stamped and published with the CURRENT time, not the tick's start:
        # the previous step may have blocked for a minute, and a snapshot
        # published with a minute-old ``generated_at`` is stale on arrival.
        at = self.clock.time()
        self.rec.step = step
        self.rec.steps.append({"step": step, "at": at})
        log.info("incident=%s attempt=%d step=%s", self.rec.incident_id, self.rec.attempt, step)
        self._publish(at)

    # -- the choreography heartbeat --------------------------------------

    def _touch_snapshot(self, now: float) -> None:
        """Re-stamp the published snapshot's ``generated_at`` without
        touching its contents: "the controller is alive and still in the
        step it last published". Reads nothing the tick thread mutates."""
        with self._snap_lock:
            if self._snapshot:
                self._snapshot = {**self._snapshot, "generated_at": now}

    def _heartbeat_loop(self, stop: threading.Event) -> None:
        started = self.clock.mono()
        while not stop.wait(self.cfg.choreography_heartbeat_s):
            if self.clock.mono() - started > CHOREOGRAPHY_HEARTBEAT_MAX_S:
                log.error("choreography heartbeat stopped after %.0fs: a blocking recovery call has outlived "
                          "every timeout it has (a bug); the snapshot goes stale on purpose",
                          CHOREOGRAPHY_HEARTBEAT_MAX_S)
                return
            self._touch_snapshot(self.clock.time())

    def _start_heartbeat(self) -> None:
        """Armed while the tick thread is inside the blocking choreography
        (jitter, diagnostics, sentinel POST /restart, docker restart); the
        freshness rules (§7.3: 30 s; §8.1: 15 s) then measure the
        controller's liveness rather than a step's length, so a legitimate
        recovery is never rendered MONITORING_UNKNOWN half-way through."""
        self._stop_heartbeat()
        stop = threading.Event()
        thread = threading.Thread(target=self._heartbeat_loop, args=(stop,), name="choreography-heartbeat",
                                  daemon=True)
        self._heartbeat_stop, self._heartbeat_thread = stop, thread
        thread.start()

    def _stop_heartbeat(self) -> None:
        stop, thread = self._heartbeat_stop, self._heartbeat_thread
        self._heartbeat_stop = self._heartbeat_thread = None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(self.cfg.choreography_heartbeat_s + 1.0)

    @property
    def heartbeat_active(self) -> bool:
        t = self._heartbeat_thread
        return t is not None and t.is_alive()

    def _start_recovery(self, category: str, detail: str, first_at: float, now: float, mono: float) -> None:
        stale = (self.incident is not None and self.incident.get("ended_at") is None
                 and first_at - float(self.incident.get("started_at") or first_at) > self.cfg.recovery_window_s)
        if self.incident is None or self.incident.get("ended_at") is not None or stale:
            if stale:
                log.warning("incident=%s left open for longer than the recovery window: closing it before the next",
                            self.incident["id"])
                self.incident["ended_at"] = now
            self.incident = {"id": utc_stamp(first_at), "started_at": first_at, "category": category,
                             "attempts": 0, "ended_at": None}
            log.warning("incident=%s opened: %s (%s)", self.incident["id"], category, detail)
        self.incident["attempts"] += 1
        self.budget.record()
        self.attempts_total["started"] += 1
        self.budget_exhausted = False
        self.pending.clear()
        self.confirmed = None
        self.consecutive_failures = 0
        self.consecutive_timeouts = 0
        self.consecutive_http_errors = 0
        self.failing_since = None
        self.verify_successes = 0
        # Nothing in flight may be harvested as this recovery's proof.
        self.canary.discard()
        self.last_canary = None
        self.proof = None
        self.readiness = None
        # A recovery in progress ALWAYS has a deadline: set here from the
        # confirmation, re-set at wait_load from the restart (the contract's
        # budget), so no step can be held open without one.
        self.rec = Recovery(in_progress=True, step="confirm", category=category, reason=detail,
                            incident_id=self.incident["id"], attempt=self.incident["attempts"],
                            started_at=now, detect_at=first_at, deadline_mono=mono + self.cfg.cold_start_budget_s,
                            last=self.rec.last)
        self.rec.steps.append({"step": "confirm", "at": now})
        self.last_failure_category = category
        self._set_state("RECOVERING", f"{category}: {detail}", now)
        log.warning("incident=%s attempt=%d/%d RECOVERING (%s: %s)", self.rec.incident_id, self.rec.attempt,
                    self.cfg.recovery_budget, category, detail)
        self._publish(now)
        self._start_heartbeat()
        try:
            self._choreograph(category, now)
        except Exception as exc:  # noqa: BLE001 — the lock must never outlive the tick that took it
            self._fail_attempt_on_error(exc, now)
        finally:
            self._stop_heartbeat()

    def _choreograph(self, category: str, now: float) -> None:
        """confirm → capture → stop_pair → wait_load, on the tick thread.
        Anything unexpected raised in here is the caller's to turn into a
        failed attempt: a RECOVERING that nobody advances, with the flock
        held, is the one outcome worse than a failed recovery (the
        orchestrator queues every request forever and cluster-recover.sh
        cannot take the lock)."""
        if self.cfg.recovery_jitter_s > 0:
            delay = random.uniform(0.0, self.cfg.recovery_jitter_s)
            log.info("incident=%s jitter %.1fs before acting", self.rec.incident_id, delay)
            self.clock.sleep(delay)

        self._step("capture")
        self._capture_diagnostics()

        self._step("stop_pair")
        if not self._stop_stale_pair(now):
            self._fail_attempt(category, "head restart failed", now)
            return

        # AFTER the pair was stopped: a probe that completed against the old
        # head during the blocking calls above can never satisfy
        # "started after the restart".
        self.rec.restart_at = self.clock.time()
        self._step("wait_load")
        self.rec.deadline_mono = self.clock.mono() + self.cfg.cold_start_budget_s

    def _fail_attempt_on_error(self, exc: BaseException, now: float) -> None:
        """An unexpected exception inside a recovery tick: logged WITH the
        traceback, the attempt failed (lock released, cooldown armed) so
        the next confirmation can try again, and the exception's class —
        never its text — in ``recovery.last``."""
        rec = self.rec
        log.exception("incident=%s attempt=%d controller error during step %s (%s): failing the attempt and "
                      "releasing the recovery lock", rec.incident_id, rec.attempt, rec.step, type(exc).__name__)
        self._fail_attempt(rec.category, f"controller error during {rec.step}: {type(exc).__name__}", now)

    def _incident_path(self) -> str:
        return os.path.join(self.cfg.incident_dir, self.rec.incident_id or "unknown")

    def _write_evidence(self, name: str, text: str) -> None:
        """One file in the incident directory, owned like the mount so the
        checkout owner can read and clean it. Nothing here may raise: a
        full disk must not stop a recovery."""
        target = self._incident_path()
        try:
            created = not os.path.isdir(target)
            os.makedirs(target, exist_ok=True)
            if created:
                _adopt_owner_path(target, self.cfg.incident_dir, log)
        except OSError as exc:
            log.error("incident=%s cannot create %s: %s", self.rec.incident_id, target, exc)
            return
        try:
            path = os.path.join(target, name)
            with open(path, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(text)
                _adopt_owner(fh.fileno(), self.cfg.incident_dir, log)
        except OSError as exc:
            log.error("incident=%s cannot write %s: %s", self.rec.incident_id, name, exc)

    def _capture_diagnostics(self) -> None:
        """Evidence BEFORE anything is restarted (§6.3)."""
        rec = self.rec
        n = rec.attempt
        try:
            self._write_evidence(f"head-logs-{n}.txt", self.docker.logs(self.cfg.head_container, tail=400, timeout=30.0))
        except DockerError as exc:
            self._write_evidence(f"head-logs-{n}.txt", f"(unavailable: {exc.kind})\n")
        if self.sentinel.configured:
            try:
                self._write_evidence(f"worker-diagnostics-{n}.txt", self.sentinel.diagnostics())
            except (ConnectFailed, ReadTimeout) as exc:
                self._write_evidence(f"worker-diagnostics-{n}.txt", f"(unavailable: {type(exc).__name__})\n")
        with self._snap_lock:
            snap = self._snapshot
        self._write_evidence(f"state-{n}.json", json.dumps(snap, indent=2, sort_keys=True) + "\n")
        log.info("incident=%s diagnostics captured in %s", rec.incident_id, self._incident_path())

    def _restart_head(self) -> bool:
        """``docker restart`` of the head, and the truth about whether it
        happened. The daemon answers only once the container is back —
        SIGTERM→SIGKILL of a 25 GiB process plus re-creating the container
        can take longer than the reply timeout — so a late reply is NOT a
        failed restart: the container is re-inspected, and a moved
        ``StartedAt``/``FinishedAt`` (or ``Restarting``) means it was applied.
        Only a daemon error with an unchanged container is a failure."""
        rec = self.rec
        before_started = self.head.get("started_at")
        rec.restart_issued_at = self.clock.time()
        try:
            self.docker.restart(self.cfg.head_container, t=self.cfg.head_restart_timeout_s)
            return True
        except DockerUnavailable as exc:
            log.warning("incident=%s head restart reply %s; re-inspecting the container", rec.incident_id, exc.kind)
        except DockerError as exc:
            log.error("incident=%s head restart refused by the daemon: %s", rec.incident_id, exc)
        for attempt in range(3):
            if attempt:
                self.clock.sleep(2.0)
            try:
                info = self.docker.inspect(self.cfg.head_container)
            except DockerError as exc:
                log.warning("incident=%s re-inspect failed: %s", rec.incident_id, exc.kind)
                continue
            state = info.get("State") or {}
            started = parse_docker_time(state.get("StartedAt"))
            finished = parse_docker_time(state.get("FinishedAt"))
            issued = rec.restart_issued_at or 0.0
            if (state.get("Restarting") or (started is not None and (before_started is None or started > before_started))
                    or (finished is not None and finished >= issued - 1.0)):
                log.warning("incident=%s head restart was applied (late reply): started_at %s -> %s",
                            rec.incident_id, before_started, started)
                return True
        return False

    def _stop_stale_pair(self, now: float) -> bool:
        """Worker first — it must be waiting at the rendezvous when the new
        head opens it — then the head. A restarted head is a new
        torch.distributed group the old worker can never rejoin (§1). The
        worker half's outcome is recorded per attempt: a refused restart
        (token skew) used to be swallowed, leaving a head-only restart the
        stale worker could never rejoin, with `last` saying "succeeded"."""
        rec = self.rec
        if self.sentinel.configured:
            if self.cfg.dry_run:
                log.warning("incident=%s DRY_RUN: would POST %s/restart", rec.incident_id, self.sentinel.base_url)
                rec.worker_restart = "dry_run"
            else:
                outcome, reply = self.sentinel.restart()
                rec.worker_restart = outcome
                if outcome == "ok":
                    log.warning("incident=%s worker restarted via the sentinel (restarted_at=%s)",
                                rec.incident_id, reply.get("restarted_at"))
                else:
                    # The worker's own healthcheck re-pairs it later; the head
                    # restart is still the right call — but say what happened.
                    log.error("incident=%s worker restart %s — continuing with the head", rec.incident_id, outcome)
        else:
            rec.worker_restart = "skipped"
        for entry in reversed(rec.steps):
            if entry.get("step") == "stop_pair":
                entry["worker"] = rec.worker_restart
                break
        if self.cfg.dry_run:
            log.warning("incident=%s DRY_RUN: would restart %s (t=%d)", rec.incident_id, self.cfg.head_container,
                        self.cfg.head_restart_timeout_s)
            rec.restart_issued_at = self.clock.time()
            return True
        if not self._restart_head():
            log.error("incident=%s head restart failed", rec.incident_id)
            return False
        log.warning("incident=%s head container %s restarted (t=%d)", rec.incident_id, self.cfg.head_container,
                    self.cfg.head_restart_timeout_s)
        return True

    def _advance_recovery(self, now: float, mono: float) -> None:
        rec = self.rec
        deadline_passed = rec.deadline_mono is not None and mono > rec.deadline_mono
        restarted = self.cfg.dry_run or (self.head.get("started_at") or 0) >= (rec.restart_issued_at or 0) - 1.0
        if rec.step == "wait_load":
            # A rank that dies while the new head is loading: fail the attempt
            # with the real category now, not with cold_start_timeout in
            # 900 s. Two observations ≥ HEAD_API_DEAD_GAP_S apart: a worker
            # whose rank died joining the OLD head's store (22:21:10Z on
            # 09-11) is re-created by its restart policy within seconds and
            # then pairs with the new head — its started_at moves past the
            # fault and the condition clears before the second look.
            fault = self._worker_fault_after_head_start() if restarted else ""
            if fault:
                if rec.load_fault is None:
                    rec.load_fault = Pending(first_at=now, detail=fault)
                    log.warning("incident=%s %s during wait_load; confirming", rec.incident_id, fault)
                rec.load_fault.count += 1
                if rec.load_fault.count >= 2 and now - rec.load_fault.first_at >= self.cfg.head_api_dead_gap_s:
                    self._fail_attempt("worker_rank_dead", fault, now)
                    return
            else:
                rec.load_fault = None
            if self.api.get("health") == 200 and restarted:
                self._step("canary")
                self._next_canary_mono = mono  # probe now, not at the next slow interval
            elif deadline_passed:
                self._fail_attempt("cold_start_timeout", self._cold_start_reason(
                    f"/health not 200 within {self.cfg.cold_start_budget_s:.0f}s of the restart", now), now)
        elif rec.step == "canary":
            # MARK_READY takes only a readiness sequence that STARTED after
            # the new container did (and after the pair was stopped): a probe
            # that completed against the old head is never this recovery's proof.
            if (restarted and self.proof is not None
                    and float(self.proof["probe_started_at"]) >= max(float(self.head.get("started_at") or 0),
                                                                     float(rec.restart_at or 0))):
                self._mark_ready(now)
                return
            if deadline_passed:
                self._fail_attempt("cold_start_timeout", self._cold_start_reason(
                    f"readiness sequence not passed within {self.cfg.cold_start_budget_s:.0f}s of the restart", now),
                    now)

    def _mark_ready(self, now: float) -> None:
        rec = self.rec
        res = self.last_canary
        self._step("mark_ready")
        self._release_lock()
        rec.in_progress = False
        self.cooldown_until = now + self.cfg.recovery_cooldown_s
        self.attempts_total["succeeded"] += 1
        if rec.detect_at is not None:
            self.recovery_duration_s = max(0.0, now - rec.detect_at)
        rec.last = {"incident": rec.incident_id, "attempt": rec.attempt, "category": rec.category,
                    "outcome": "succeeded", "worker_restart": rec.worker_restart, "started_at": rec.started_at,
                    "ended_at": now, "duration_s": (now - rec.started_at) if rec.started_at else None}
        log.warning("incident=%s attempt=%d recovered: readiness sequence passed (canary %.2fs, ttft %.2fs); "
                    "%.0fs from detection", rec.incident_id, rec.attempt, res.total_s if res else 0.0,
                    (res.ttft_s if res and res.ttft_s else 0.0), self.recovery_duration_s or 0.0)
        if self.readiness is not None:
            self._write_evidence(f"readiness-{rec.attempt}.json", json.dumps(
                {"readiness": self.readiness.doc(), "gpus": dict(self.gpus)}, indent=2, sort_keys=True) + "\n")
        self.verify_successes = 0
        rec.verifying = True
        rec.step = "verify"
        rec.steps.append({"step": "verify", "at": now})

    def _fail_attempt(self, category: str, detail: str, now: float) -> None:
        rec = self.rec
        self._release_lock()
        rec.in_progress = False
        self.cooldown_until = now + self.cfg.recovery_cooldown_s
        self.attempts_total["failed"] += 1
        self.last_failure_category = category if category in FAILURE_CATEGORIES else "none"
        rec.last = {"incident": rec.incident_id, "attempt": rec.attempt, "category": rec.category,
                    "outcome": "failed", "failure": category, "failure_detail": self.cold_start_detail,
                    "detail": detail[:200], "step": rec.step,
                    "worker_restart": rec.worker_restart, "started_at": rec.started_at, "ended_at": now,
                    "duration_s": (now - rec.started_at) if rec.started_at else None}
        log.error("incident=%s attempt=%d FAILED: %s (%s); cooldown %.0fs, budget remaining %d",
                  rec.incident_id, rec.attempt, category, detail, self.cfg.recovery_cooldown_s,
                  self.budget.remaining())
        rec.step = "idle"
        rec.steps.append({"step": "idle", "at": now})
        self.verify_successes = 0

    def _end_incident(self, now: float) -> None:
        if self.incident is not None and self.incident.get("ended_at") is None:
            self.incident["ended_at"] = now
            log.warning("incident=%s ended after %d attempt(s): three consecutive canary successes",
                        self.incident["id"], self.incident["attempts"])
        self.rec.verifying = False
        self.rec.step = "idle"
        self.rec.steps.append({"step": "idle", "at": now})

    # ------------------------------------------------------------------
    # Cold-start diagnosis (contract §6.6)
    # ------------------------------------------------------------------

    def _cold_start_reason(self, base: str, now: float) -> str:
        """Classify a cold-start timeout once per head start: a compile-error
        signature in the head log means the persistent kernel cache, and
        the reason names the runbook step the operator must run — the
        controller never deletes a cache by itself."""
        started = self.head.get("started_at")
        if self._cold_start_detail_for != started:
            self._cold_start_detail_for = started
            sig = None
            try:
                sig = scan_compile_error_lines(self.docker.logs(self.cfg.head_container, tail=400, timeout=30.0))
            except DockerError as exc:
                log.warning("cannot read the head log for compile signatures: %s", exc.kind)
            if sig is not None:
                self.cold_start_detail = "compile"
                log.error("compile-error signature %r in the head log during a cold-start timeout", sig)
            elif self.readiness is not None or self.last_success_at is not None:
                self.cold_start_detail = "readiness"
            else:
                self.cold_start_detail = "load"
        if self.cold_start_detail == "compile":
            return (f"{base}; compile-error signature in the head log — a stale kernel cache is the likely cause: "
                    f"run scripts/cluster-recover.sh --clear-kernel-cache (runbook)")
        if self.cold_start_detail == "readiness" and self.readiness is not None and not self.readiness.passed:
            return f"{base}; last readiness failure at {self.readiness.failed_step}: {self.readiness.detail}"
        return base

    # ------------------------------------------------------------------
    # The state (contract §2)
    # ------------------------------------------------------------------

    def _set_state(self, name: str, reason: str, now: float) -> None:
        if name != self.state:
            log.warning("state %s -> %s reason=%r incident=%s", self.state, name, reason,
                        self.incident["id"] if self.incident else None)
            self.transitions.append({"from": self.state, "to": name, "reason": reason, "at": now})
            del self.transitions[:-200]
            self.state = name
            self.since = now
        self.reason = reason

    def _blocked_text(self) -> str:
        if not self.rec.blocked:
            return ""
        detail = f" ({self.rec.blocked_detail})" if self.rec.blocked_detail else ""
        return f"; blocked: {self.rec.blocked}{detail}"

    def _evaluate(self, now: float, mono: float) -> None:
        head, api, w = self.head, self.api, self.worker
        proven_recent = self._proven_recent(now)
        self.primary_ready = proven_recent
        health = api.get("health")

        if self.rec.in_progress:
            self._set_state("RECOVERING", f"step {self.rec.step} ({self.rec.category})", now)
            return
        if not self.docker_ok:
            if proven_recent:
                self._set_state("DEGRADED", f"docker socket unobservable ({self.docker_error}); canary ok", now)
            else:
                self._set_state("MONITORING_UNKNOWN", f"docker socket unobservable ({self.docker_error})", now)
            return
        if self.confirmed is not None:
            # A confirmed failure the controller cannot act on yet is still a
            # confirmed failure — published before any "cannot observe"
            # verdict, so the state does not flap DOWN/UNKNOWN every tick.
            category, detail, _ = self.confirmed
            if proven_recent and category == "worker_rank_dead":
                # A passing canary is the tie-breaker the contract promises
                # ("returns to READY by itself"): the one trigger that rests
                # on the sentinel's document alone never pins DOWN over a
                # fresh real completion (a forged or stale sentinel must not
                # hold the orchestrator's breaker open on a healthy engine).
                # /health 5xx, a refused port or a wedge are the head's own
                # word and stand.
                self._set_state("DEGRADED", f"{category} confirmed ({detail}) but the canary passes"
                                            f"{self._blocked_text()}", now)
            else:
                self._set_state(CONFIRMED_STATE.get(category, "DOWN"), f"{category}: {detail}{self._blocked_text()}",
                                now)
            return
        if self.budget_exhausted and not proven_recent:
            self._set_state("DOWN", f"recovery budget exhausted ({self.budget.used()}/{self.cfg.recovery_budget} "
                                    f"in {self.cfg.recovery_window_s:.0f}s); no further restart", now)
            return
        if api.get("tcp") is False and api.get("tcp_error") in ("timeout", "unreachable") and not proven_recent:
            self._set_state("MONITORING_UNKNOWN", f"head API connect {api.get('tcp_error')}; inference unproven", now)
            return
        if head.get("exists") is False:
            self._set_state("DOWN", "head container not found", now)
            return
        if not head.get("running"):
            if head.get("status") in ("restarting", "created"):
                self._set_state("STARTING", f"head container {head.get('status')}", now)
            else:
                self._set_state("DOWN", f"head container {head.get('status') or 'not running'}", now)
            return
        if proven_recent:
            degraded: List[str] = []
            if w["configured"] and not w.get("reachable"):
                degraded.append(f"worker sentinel unreachable ({w.get('error') or 'no answer'})")
            elif w["configured"] and w.get("rank_process_alive") is False:
                degraded.append("sentinel reports the rank process absent" if self._worker_rank_seen_alive else
                                "sentinel does not see the rank process (never seen for this head start: "
                                "unconfirmed, not a trigger)")
            if health != 200:
                degraded.append(f"/health {health if health is not None else 'no answer'}")
            if api.get("metrics") != 200:
                degraded.append("metrics unavailable")
            res = self.last_canary
            if res is not None and res.ok and res.ttft_s is not None and res.ttft_s > self.cfg.ttft_degraded_s:
                degraded.append(f"canary TTFT {res.ttft_s:.1f}s")
            elif res is not None and not res.ok:
                # Still within the freshness window of the last success, but
                # the latest probe failed: not READY, not yet confirmed.
                degraded.append(f"last canary {res.outcome} ({res.detail}); {self.consecutive_failures} consecutive; "
                                f"awaiting confirmation{self._failing_bound_text(now)}")
            if self.router["configured"] and not self.router_available:
                degraded.append(f"router (internal classifier) unhealthy ({self.router.get('health') or 'no answer'})")
            if head.get("engine_process_alive") is False or head.get("rank_process_alive") is False:
                degraded.append("head process vanished from the process table")
            rd = self.readiness
            if rd is not None and rd.participation == "unobserved":
                degraded.append(f"GPU participation unobserved ({rd.detail or 'exporter unreachable'})")
            if degraded:
                self._set_state("DEGRADED", "; ".join(degraded), now)
            elif (self.engine.get("requests_running") or 0) > 0:
                self._set_state("BUSY", f"{int(self.engine['requests_running'])} running; "
                                        f"canary ok in {(res.total_s if res else 0):.2f}s", now)
            else:
                self._set_state("READY", f"canary ok in {(res.total_s if res else 0):.2f}s", now)
            return
        if not self._proven_since_head_start():
            started = head.get("started_at")
            age = (now - float(started)) if started else None
            seen = self._head_start_seen_at
            budget_age = (now - max(float(started), seen if seen is not None else float(started))) if started else None
            rd = self.readiness
            if budget_age is None or budget_age < self.cfg.cold_start_budget_s:
                if rd is not None and not rd.passed:
                    why = f"readiness sequence failed at {rd.failed_step}: {rd.detail}"
                elif self.canary.in_flight and self.canary.kind == "readiness":
                    why = f"readiness sequence running ({self.canary.elapsed_s(mono):.0f}s)"
                else:
                    why = "readiness sequence not passed yet"
                self._set_state("STARTING", f"{why} ({age:.0f}s since container start; tcp {api.get('tcp')}, "
                                            f"health {health})" if age is not None else why, now)
            else:
                if not self._cold_start_flagged:
                    self._cold_start_flagged = True
                    self.last_failure_category = "cold_start_timeout"
                    log.error("no readiness proof within %.0fs of the head start (age %.0fs)",
                              self.cfg.cold_start_budget_s, age)
                reason = self._cold_start_reason(
                    f"cold start timeout: readiness sequence not passed {age:.0f}s after container start", now)
                self._set_state("DOWN", reason, now)
            return
        # Proven once since this head started, but the canary is stale or
        # failing and nothing is confirmed yet: say so without claiming READY.
        res = self.last_canary
        if self._saturation_note:
            why = self._saturation_note
        elif self.canary.in_flight:
            why = f"canary outstanding {self.canary.elapsed_s(mono):.0f}s"
        elif res is not None and not res.ok:
            why = f"last canary {res.outcome} ({res.detail}); {self.consecutive_failures} consecutive"
        else:
            why = "canary result stale"
        self._set_state("DEGRADED", f"{why}; awaiting confirmation{self._failing_bound_text(now)}", now)

    def _failing_bound_text(self, now: float) -> str:
        """How long DEGRADED "awaiting confirmation" can last (rule 7), or
        why it will not end by itself (a failure that is not the engine's)."""
        res = self.last_canary
        if res is None or res.ok or self.failing_since is None:
            return ""
        kind = canary_engine_fault_kind(res)
        if not kind:
            return (f" (HTTP {res.http_status}: not counted as an engine fault — a restart cannot fix a request "
                    f"the API rejects; check the canary body against this build)" if res.http_status is not None
                    else " (probe error, not the engine's)")
        left = self.cfg.canary_fail_degraded_max_s - (now - float(self.failing_since))
        return f" (WEDGED as {kind} in {max(0.0, left):.0f}s unless a canary passes)"

    # ------------------------------------------------------------------
    # Canary scheduling
    # ------------------------------------------------------------------

    def _schedule_canary(self, now: float, mono: float) -> None:
        if self.canary.in_flight or not self.api.get("tcp"):
            return
        if self.rec.in_progress and self.rec.step in ("capture", "stop_pair", "wait_load"):
            # vLLM binds :8000 before the engine exists: a probe now would
            # only hang for its timeout and delay the real readiness run.
            return
        health = self.api.get("health")
        if health is not None and 500 <= int(health) < 600:
            return  # the engine says it is dead; trigger 2 handles it
        model = self.model_id
        if not model:
            if not self._no_model_logged:
                log.warning("no model id yet (/v1/models %s); canary skipped", self.api.get("models"))
                self._no_model_logged = True
            return
        self._no_model_logged = False
        if self._next_canary_mono is None or mono >= self._next_canary_mono:
            # The whole §5 v2 sequence until this head start is proven;
            # the routine streaming probe after that.
            kind = "routine" if self._proven_since_head_start() else "readiness"
            if self.canary.start(model, kind, health, worker_expected=self.sentinel.configured):
                self._next_canary_mono = mono + self._canary_interval()

    # ------------------------------------------------------------------
    # The tick
    # ------------------------------------------------------------------

    def tick(self) -> None:
        now = self.clock.time()
        mono = self.clock.mono()
        self._observe_head(now)
        self._observe_head_memory(now)
        self._observe_api(now)
        self._observe_worker(now)
        self._observe_router(now, mono)
        self._harvest_canary(now)
        if self.rec.in_progress:
            try:
                self._advance_recovery(now, mono)
            except Exception as exc:  # noqa: BLE001 — same rule as the choreography: never a pinned RECOVERING
                self._fail_attempt_on_error(exc, now)
        else:
            self._detect(now, mono)
            self._act_on_confirmed(now, mono)
        self._evaluate(now, mono)
        self._schedule_canary(now, mono)
        self._publish(now)

    # ------------------------------------------------------------------
    # Publishing: /state and /metrics
    # ------------------------------------------------------------------

    def _canary_doc(self) -> dict:
        res = self.last_canary
        doc = {"ok": None, "http_status": None, "connect_s": None, "ttft_s": None, "total_s": None,
               "tokens": None, "terminal": None, "error_category": "none", "at": None, "started_at": None,
               "kind": None, "health_at_start": None,
               "last_success_at": self.last_success_at, "consecutive_failures": self.consecutive_failures,
               "consecutive_timeouts": self.consecutive_timeouts,
               "consecutive_http_errors": self.consecutive_http_errors, "failing_since": self.failing_since,
               "in_flight": self.canary.in_flight, "in_flight_kind": self.canary.kind if self.canary.in_flight else None,
               "outstanding_s": self.canary.outstanding_s(self.clock.mono())}
        if res is not None:
            doc.update({"ok": res.ok, "http_status": res.http_status, "connect_s": res.connect_s,
                        "ttft_s": res.ttft_s, "total_s": res.total_s, "tokens": res.tokens,
                        "terminal": res.terminal, "error_category": res.error_category, "at": res.at,
                        "started_at": res.started_at, "kind": res.kind, "health_at_start": res.health_at_start})
        return doc

    def _readiness_doc(self) -> dict:
        rd = self.readiness
        base = {"non_stream_ok": None, "stream_ok": None, "progress_ok": None, "participation": "pending",
                "rank_alive": None, "passed": False, "failed_step": "", "detail": "", "tokens_expected": 0,
                "tokens_delta": None, "participation_probes": [], "started_at": None, "at": None}
        if rd is not None:
            base.update(rd.doc())
        base["proven_since_head_start"] = self._proven_since_head_start()
        base["proof"] = dict(self.proof) if self.proof else None
        return base

    def _publish(self, now: float) -> None:
        rec = self.rec
        incident = None
        if self.incident is not None:
            ended = self.incident.get("ended_at")
            if ended is None or now - ended < 600:
                incident = dict(self.incident)
        head = dict(self.head)
        head["docker_ok"] = self.docker_ok
        worker = {k: v for k, v in self.worker.items() if k != "error"}
        if self.worker.get("error"):
            worker["error"] = self.worker["error"]
        with self._snap_lock:
            manual_pending = self._manual_pending
        snap = {
            "schema": 1,
            "generated_at": now,
            "state": self.state,
            "state_code": STATE_CODE[self.state],
            "reason": self.reason,
            "since": self.since,
            "primary_ready": self.primary_ready,
            "router_available": self.router_available,
            "incident": incident,
            "readiness": self._readiness_doc(),
            "signals": {
                "head_container": head,
                "worker": worker,
                "api": dict(self.api),
                "engine": dict(self.engine),
                "canary": self._canary_doc(),
                "router": dict(self.router),
                "gpus": dict(self.gpus),
                "head_memory": dict(self.head_memory),
            },
            "recovery": {
                "in_progress": rec.in_progress,
                "step": "confirm" if (manual_pending and rec.step == "idle") else rec.step,
                "category": rec.category,
                "attempt": rec.attempt,
                "incident_id": rec.incident_id,
                "attempts_in_window": self.budget.used(),
                "budget": self.cfg.recovery_budget,
                "window_s": self.cfg.recovery_window_s,
                "cooldown_until": self.cooldown_until,
                "blocked": rec.blocked,
                "blocked_detail": rec.blocked_detail,
                "manual_pending": manual_pending,
                "worker_restart": rec.worker_restart,
                "verify_successes": self.verify_successes,
                "steps": list(rec.steps[-20:]),
                "last": rec.last,
            },
            "last_failure_category": self.last_failure_category,
            "cold_start_detail": self.cold_start_detail,
            "cold_start_seconds": self.cold_start_s,
            "recovery_duration_seconds": self.recovery_duration_s,
            "_counters": {"probes": dict(self.probes_total), "attempts": dict(self.attempts_total)},
            "single_node": not self.sentinel.configured,
            "dry_run": self.cfg.dry_run,
        }
        with self._snap_lock:
            self._snapshot = snap

    def snapshot(self) -> dict:
        with self._snap_lock:
            snap = self._snapshot
        return {k: v for k, v in snap.items() if not k.startswith("_")}

    def request_manual_recovery(self) -> Tuple[int, dict]:
        """``POST /recover``: the same budget and cooldown as an automatic
        recovery (v2 §6), refused synchronously so the operator reads why;
        accepted requests are visible in the very next ``/state`` read
        (``recovery.manual_pending``, step ``confirm``) — a follower must
        never read a pre-request document and call the outcome READY."""
        now = self.clock.time()
        with self._snap_lock:
            snap = self._snapshot
            rec = snap.get("recovery", {})
            if rec.get("in_progress"):
                return 409, {"accepted": False, "reason": "a recovery is already in progress",
                             "incident": snap.get("incident")}
            if self._manual_pending:
                return 202, {"accepted": True, "reason": "manual", "category": "manual",
                             "note": "already queued; poll /state", "incident": self._manual_incident_id()}
            cooldown_until = rec.get("cooldown_until")
            if cooldown_until is not None and now < float(cooldown_until):
                return 429, {"accepted": False, "reason": "cooldown",
                             "retry_after_s": round(float(cooldown_until) - now, 1)}
            if self.budget.exhausted():
                return 429, {"accepted": False, "reason": "recovery budget exhausted",
                             "attempts_in_window": self.budget.used(), "budget": self.cfg.recovery_budget,
                             "window_s": self.cfg.recovery_window_s,
                             "note": "the escalation past the budget is scripts/cluster-recover.sh --force "
                                     "under the engine lock"}
            self._manual_pending = True
            self._manual_requested_at = now
            incident_id = self._manual_incident_id()
            published = dict(snap)
            published["recovery"] = {**rec, "manual_pending": True,
                                     "step": "confirm" if rec.get("step") == "idle" else rec.get("step")}
            self._snapshot = published
        log.warning("manual recovery requested via POST /recover; acting at the next tick (incident %s)", incident_id)
        return 202, {"accepted": True, "reason": "manual", "category": "manual", "incident": incident_id,
                     "note": "the recovery starts at the next tick; poll /state until recovery.in_progress "
                             "has been true"}

    def _manual_incident_id(self) -> Optional[str]:
        # Called under _snap_lock. The id that _start_recovery WILL use.
        inc = self.incident
        if inc is not None and inc.get("ended_at") is None:
            return inc["id"]
        at = self._manual_requested_at
        return utc_stamp(at) if at is not None else None

    def metrics_text(self) -> str:
        with self._snap_lock:
            s = self._snapshot
        sig = s["signals"]
        head, api, worker, eng, can, rec = (sig["head_container"], sig["api"], sig["worker"], sig["engine"],
                                            sig["canary"], s["recovery"])
        rd = s["readiness"]
        d = MetricsDoc()
        d.gauge("techsara_engine_controller_up", 1, "1 while the engine controller is serving.")
        d.gauge("techsara_vllm_state_code", s["state_code"], "Top-level engine state code (contract §2, 0..8).")
        d.one_hot("techsara_vllm_state", "One-hot over the nine state names.", "state", STATES, s["state"])
        d.gauge("techsara_vllm_state_since_timestamp_seconds", s["since"], "When the current state was entered.")
        d.gauge("techsara_vllm_primary_ready", int(bool(s["primary_ready"])),
                "1 when the readiness sequence passed for this head start and a completion succeeded within "
                "the last two probe intervals.")
        d.gauge("techsara_vllm_router_available", int(bool(s["router_available"])),
                "1 when the router's (internal classifier, never a serving path) health endpoint answered 200.")
        d.gauge("techsara_vllm_participation_ok", int(rd.get("participation") == "ok"),
                "1 when both GPUs were seen working during the last readiness sequence.")
        d.gauge("techsara_vllm_generated_at_seconds", s["generated_at"], "The snapshot's own timestamp (freshness).")
        if can["at"] is not None:
            d.gauge("techsara_vllm_synthetic_success", int(bool(can["ok"])), "Outcome of the last canary.")
            if can["connect_s"] is not None:
                d.gauge("techsara_vllm_synthetic_connect_seconds", can["connect_s"], "Last canary TCP connect latency.")
            if can["ttft_s"] is not None:
                d.gauge("techsara_vllm_synthetic_ttft_seconds", can["ttft_s"], "Last canary time to first token.")
            d.gauge("techsara_vllm_synthetic_duration_seconds", can["total_s"], "Last canary total latency.")
            d.gauge("techsara_vllm_synthetic_tokens", can["tokens"], "Content tokens the last canary received.")
        for outcome in PROBE_OUTCOMES:
            d.counter("techsara_vllm_synthetic_probes_total", s["_counters"]["probes"].get(outcome, 0),
                      "Canary probes by outcome.", {"outcome": outcome})
        d.gauge("techsara_vllm_last_success_timestamp_seconds", can["last_success_at"] or 0,
                "Completion time of the last successful real completion; 0 when none since start.")
        d.gauge("techsara_vllm_consecutive_probe_failures", can["consecutive_failures"],
                "Canary failures since the last success.")
        d.gauge("techsara_vllm_generation_frozen_seconds", eng.get("frozen_seconds") or 0,
                "Seconds neither token counter moved while requests were running (0 when idle).")
        mem = sig.get("head_memory") or {}
        if mem.get("available_bytes") is not None:
            d.gauge("techsara_vllm_head_mem_available_bytes", mem["available_bytes"],
                    "The head host's MemAvailable (/proc/meminfo) as of the last tick; the recovery warns below "
                    "HEAD_MIN_MEM_AVAILABLE_BYTES for one tick, then restarts anyway.")
        if head.get("running") is not None:
            d.gauge("techsara_vllm_head_container_running", int(bool(head["running"])), "Head container running.")
        if head.get("engine_process_alive") is not None:
            d.gauge("techsara_vllm_head_engine_process_alive", int(bool(head["engine_process_alive"])),
                    "vllm serve and VLLM::EngineCore present in the head's process table.")
        d.gauge("techsara_vllm_head_api_tcp_up", int(bool(api.get("tcp"))), "TCP connect to the head API succeeded.")
        d.gauge("techsara_vllm_head_health_ok", int(api.get("health") == 200), "/health answered 200.")
        d.gauge("techsara_vllm_head_metrics_ok", int(api.get("metrics") == 200),
                "/metrics answered 200 with the engine series.")
        if worker.get("configured"):
            d.gauge("techsara_vllm_worker_reachable", int(bool(worker.get("reachable"))),
                    "The worker sentinel answered /state.")
            if worker.get("container_running") is not None:
                d.gauge("techsara_vllm_worker_container_running", int(bool(worker["container_running"])),
                        "Worker container running (from the sentinel).")
            if worker.get("rank_process_alive") is not None:
                d.gauge("techsara_vllm_worker_rank_alive", int(bool(worker["rank_process_alive"])),
                        "VLLM::Worker (waiting at the rendezvous or joined as _TP1) present on the worker.")
        both = bool(s["primary_ready"]) and (not worker.get("configured")
                                             or (bool(worker.get("reachable")) and bool(worker.get("rank_process_alive"))))
        d.gauge("techsara_vllm_both_ranks_ok", int(both),
                "A TP=2 completion succeeded recently and the sentinel sees rank 1 (single node: the completion alone).")
        if head.get("restart_count") is not None:
            d.gauge("techsara_vllm_container_restart_count", head["restart_count"],
                    "Docker RestartCount of the rank's container.", {"rank": "0"})
        if worker.get("restart_count") is not None:
            d.gauge("techsara_vllm_container_restart_count", worker["restart_count"],
                    "Docker RestartCount of the rank's container.", {"rank": "1"})
        d.gauge("techsara_vllm_recovery_in_progress", int(bool(rec["in_progress"])), "A recovery holds the lock.")
        d.one_hot("techsara_vllm_recovery_step", "Current recovery step, one-hot.", "step", STEPS, rec["step"])
        for outcome in ATTEMPT_OUTCOMES:
            d.counter("techsara_vllm_recovery_attempts_total", s["_counters"]["attempts"].get(outcome, 0),
                      "Recovery attempts by outcome.", {"outcome": outcome})
        d.one_hot("techsara_vllm_recovery_worker_restart", "Outcome of the worker half of the last STOP_STALE_PAIR.",
                  "outcome", WORKER_RESTART_OUTCOMES, rec["worker_restart"])
        d.gauge("techsara_vllm_restart_budget_remaining", max(0, rec["budget"] - rec["attempts_in_window"]),
                "Recoveries still allowed in the current window.")
        inc = s.get("incident")
        d.gauge("techsara_vllm_incident_start_timestamp_seconds",
                (inc["started_at"] if inc and inc.get("ended_at") is None else 0),
                "Start of the open incident; 0 when none.")
        d.one_hot("techsara_vllm_last_failure_category", "Last failure category, one-hot over the bounded set.",
                  "category", FAILURE_CATEGORIES, s["last_failure_category"])
        if s.get("cold_start_seconds") is not None:
            d.gauge("techsara_vllm_cold_start_seconds", s["cold_start_seconds"],
                    "Last measured head container start -> first readiness proof of that start.")
        if s.get("recovery_duration_seconds") is not None:
            d.gauge("techsara_vllm_recovery_duration_seconds", s["recovery_duration_seconds"],
                    "Last measured detection -> READY.")
        return d.render()

    def shutdown(self) -> None:
        self._stop_heartbeat()
        self._release_lock()


def _adopt_owner(fd: int, ref_dir: str, logger: logging.Logger) -> None:
    """Make a file this (root) container created usable by the owner of the
    bind mount it lives in: mode 0666 (umask stripped it) and, when we are
    allowed to, the mount directory's uid:gid."""
    try:
        os.fchmod(fd, 0o666)
    except OSError:
        pass
    try:
        st = os.stat(ref_dir)
        os.fchown(fd, st.st_uid, st.st_gid)
    except (OSError, PermissionError) as exc:
        logger.debug("cannot chown to the mount owner: %s", exc)


def _adopt_owner_path(path: str, ref_dir: str, logger: logging.Logger) -> None:
    try:
        st = os.stat(ref_dir)
        os.chown(path, st.st_uid, st.st_gid)
        os.chmod(path, 0o777 if os.path.isdir(path) else 0o666)
    except (OSError, PermissionError) as exc:
        logger.debug("cannot chown %s to the mount owner: %s", path, exc)


# ---------------------------------------------------------------------------
# HTTP (contract §6.1)
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    #: Applied to every accepted socket: a peer that connects and never
    #: finishes its request (or announces a body it never sends) frees its
    #: handler thread after this long instead of pinning it forever.
    timeout = 10
    controller: Optional[Controller] = None

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        ctl = self.controller
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if ctl is None:
            send_text(self, 503, "starting\n")
        elif path == "/state":
            send_json(self, 200, ctl.snapshot())
        elif path == "/metrics":
            send_metrics(self, ctl.metrics_text())
        elif path == "/healthz":
            send_text(self, 200, "ok\n")
        else:
            send_text(self, 404, "not found\n")

    def do_POST(self) -> None:  # noqa: N802
        ctl = self.controller
        path = self.path.split("?", 1)[0].rstrip("/")
        peer = self.client_address[0] if self.client_address else ""
        # The path and the peer are checked BEFORE the body is read: a
        # non-loopback peer gets its 403 without being able to hold the
        # thread on a body it never sends. The unread body means the
        # connection cannot be reused, so it is closed.
        if path != "/recover" or ctl is None:
            self.close_connection = True
            send_text(self, 404, "not found\n")
            return
        if not is_loopback(peer):
            self.close_connection = True
            log.warning("POST /recover refused from %s", peer)
            send_json(self, 403, {"accepted": False, "reason": "POST /recover is accepted from 127.0.0.1 only"})
            return
        body = read_json_body(self)
        if not isinstance(body, dict):
            send_json(self, 400, {"accepted": False, "reason": "JSON object body required"})
            return
        if body.get("category", "manual") != "manual":
            send_json(self, 400, {"accepted": False, "reason": "category must be 'manual'"})
            return
        status, reply = ctl.request_manual_recovery()
        send_json(self, status, reply)

    def log_message(self, fmt: str, *args) -> None:
        log.debug(fmt, *args)


def main() -> int:
    global log
    log = configure_logging("controller", "CONTROLLER_LOG_LEVEL")
    cfg = Config.from_env()
    clock = Clock()
    docker = DockerClient(cfg.docker_socket, cfg.docker_api_version)
    ctl = Controller(cfg, docker, clock)
    Handler.controller = ctl
    server = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    log.info(
        "listening on %s:%d; head=%s api=%s sentinel=%s token=%s router=%s gpu_exporters=head:%s worker:%s "
        "lock=%s incidents=%s canary=%.0fs/%.0fs timeout=%.0fs frozen=%.0fs cold_start=%.0fs budget=%d/%.0fs "
        "cooldown=%.0fs poll=%.0fs participation>=%.0f%% x%d head_min_mem=%s dry_run=%s",
        cfg.bind, cfg.port, cfg.head_container, cfg.head_api_url, cfg.sentinel_url or "(single node)",
        "set" if cfg.sentinel_token else "unset", cfg.router_health_url or "(not configured)",
        "set" if cfg.head_gpu_exporter_url else "unset", "set" if cfg.worker_gpu_exporter_url else "unset (skipped)",
        cfg.lock_path, cfg.incident_dir, cfg.canary_interval_s, cfg.canary_interval_fast_s, cfg.canary_timeout_s,
        cfg.frozen_s, cfg.cold_start_budget_s, cfg.recovery_budget, cfg.recovery_window_s, cfg.recovery_cooldown_s,
        cfg.poll_s, cfg.participation_min_util, PARTICIPATION_PROBES,
        (f"{cfg.head_min_mem_available_bytes / GIB:.0f}GiB" if cfg.head_min_mem_available_bytes > 0 else "off"),
        cfg.dry_run,
    )
    if read_mem_available(cfg.meminfo_path) is None:
        log.warning("cannot read MemAvailable from %s: the head-memory precondition will stay unobserved",
                    cfg.meminfo_path)
    if not os.path.isdir(os.path.dirname(cfg.lock_path) or "."):
        log.error("recovery lock directory %s is missing (bind mount?): recoveries will stand by until it exists",
                  os.path.dirname(cfg.lock_path))
    stop = threading.Event()

    def on_signal(signum, _frame) -> None:
        log.info("signal %d: stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        run_periodically(ctl.tick, cfg.poll_s, clock, stop, log, "controller")
    finally:
        ctl.shutdown()
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
