#!/usr/bin/env python3
"""api_soak — prove "many users at once" on the real engine, and stop the moment it hurts.

WHY THIS EXISTS (2026-09-13). The owner removed every usage limit from `/v1`,
and one vLLM engine (TP=2 across both Sparks) serves the chat app and the
public API together. What that engine does at 16, 32 or 64 concurrent mixed
streams has never been measured on the current build: the evidence stops at
concurrency 10 with short outputs (the capacity team's measurement of
2026-09-13). The only safe place to find out is the owner-approved planned
window, and later as a post-deploy check — so the tool that does it must be
able to stop itself faster than a person watching Grafana could.

WHAT IT DOES. Ramps closed-loop streaming clients through stages (default
8, 16, 32, 64). Each client sends one request at a time, drawn from a mix of
short and long prompts and short and long outputs, for `--stage-seconds`;
then the stage drains, the engine is read, and the next stage starts. Per
stage it reports TTFT p50/p95/p99, per-stream decode tokens/s, aggregate
tokens/s (client-counted and engine-counted), capacity 503s, errors, and the
engine's running/waiting/KV/preemption figures.

WHAT STOPS IT (the abort guards). A monitor reads the engine's metrics every
`--sample-interval` seconds for the whole run, on its own HTTP client so a
saturated request pool can never blind it:

  * any 5xx that is not the capacity 503 (`model_unavailable`, "at
    capacity"), or an in-stream server error after a 200  -> abort
  * any other failure (4xx, transport error, idle or truncated stream)
    beyond `--max-errors` (default 0)                        -> abort
  * `vllm:kv_cache_usage_perc` above 0.95 for 30 s           -> abort
  * `vllm:num_preemptions_total` rises                       -> abort
  * any engine counter goes BACKWARDS (the engine restarted) -> abort
  * `vllm:generation_tokens_total` flat for 20 s while
    `vllm:num_requests_running` > 0 (the wedge)              -> abort
  * no usable metrics sample for 15 s (the guards are blind) -> abort
  * real traffic before the start                            -> refuse
  * the engine still busy after a stage drained (real users
    arrived, or requests leaked)                             -> abort
  * the guard monitor itself dies, or no sample reaches the
    guards for 15 s while a stage runs (the stage loop checks
    this on its own, so a hung monitor cannot hide)          -> abort
  * the evidence files cannot be written (disk full)         -> abort

On abort every in-flight stream is closed, a final engine sample is taken,
and the report says which guard fired, with the samples behind it. For `/v1`
the disconnect is enough: the server's SSE generator closes the generation,
which releases the admission lane and the engine sequence. THE CHAT APP IS
DIFFERENT (fixed 2026-09-13 after review): its generations are detached
(main.py runs the turn as a task and the stream only follows it), so closing
the stream stops nothing. Every chat turn the tool leaves without a terminal
frame — abort, drain timeout, interrupt, idle or broken stream — gets a
`POST /chat/stop` (bounded, recorded in summary.json under `chat_turns`).
One case cannot be undone from outside: a turn the orchestrator PARKED while
the engine was recovering (terminal code MODEL_RECOVERING) stays `queued`,
and the continuity sweep will run it once the model is back. Those
conversation ids are listed under `chat_turns.parked_conversation_ids` for an
operator to settle; every chat conversation of a run starts with
`soak-<run_id>-`.

WHAT `/v1` CONCURRENCY ACTUALLY MEASURES (corrected 2026-09-13). Streaming
`/v1` work below the long-context threshold takes no public-API gate: its
gate is the orchestrator's shared NORMAL admission lane, which it shares with
the chat app — ADMISSION_NORMAL_MAX 10 at a time, a waiter waits up to
ADMISSION_NORMAL_WAIT_S 600 s, and the line refuses only when
ADMISSION_MAX_WAITING 400 are already waiting. The refusal (depth or wait) is
an IN-STREAM `model_at_capacity` error after a 200, which this tool counts as
capacity. So at stages above 10 through `/v1` the engine should show about
10 running, the rest queue in the orchestrator for up to ten minutes, TTFT
includes that queueing, and capacity refusals will be rare. Two things keep
that from reading as a PASS it is not:
  * the orchestrator's lane is read with the engine (`llm_admission_waiting`,
    `llm_admission_lane_active`, `llm_admission_wait_seconds`), through the
    same Prometheus (`--admission-selector`, default job="orchestrator") or
    `--admission-metrics-url`, and each stage reports its queue depth, how
    many requests waited and their mean wait;
  * optional latency gates, `--ttft-p95-max` and `--decode-p5-min`, turn a
    stage that served everyone too slowly into FAIL. Without them PASS means
    "no errors and no guard fired", and the report says latency was not
    judged.
Size `--stage-seconds` and `--drain-timeout` with the 600 s lane wait in
mind, and to prove the engine beyond 10 either raise the lane for the window
or point `--base-url` at the raw engine with its served `--model`.

THE WEDGE RULE HAS ONE EXEMPTION, and it was bought with an incident: on
2026-09-12 a single ~950K-token prefill kept the token counters flat for
twelve minutes while requests were running — exactly a wedge's shape — and
the engine controller confirmed WEDGED on a working engine. vLLM counts
tokens only when a prefill finishes; the scheduler-step counter
`vllm:iteration_tokens_total_count` advances on every chunk. So a flat
generation counter with a MOVING step counter is a long prefill, not a hang.
When a build does not expose the step counter, the plain rule applies.

SAFETY. Nothing is sent without `--confirm-load` (or `--dry-run`, which only
reads: GET /v1/models, one sign-in for the chat surface, and engine metrics).
The API key comes from an environment variable or a file, never the command
line, and is never printed. Chat-surface requests create conversations for
the signed-in soak account (they are real chat turns); they need
`--chat-base-url`, the ORCHESTRATOR origin that serves /auth/login, /chat and
/chat/stop (the public web origin proxies those under /api/, which this tool
does not speak).

REPEATABILITY. Request shapes, sizes and the mix are fixed by `--seed`, and so
are the prompt bytes: a rerun with the same seed sends byte-identical prompts
in the same order. `--unique-prompts` puts the run id into every prompt
instead, so no two runs share a prefix. Chat conversation ids always carry
the run id — reusing one would append to an earlier run's conversation.

    # read-only rehearsal: config, key, model list, metrics, traffic verdict
    TECHSARA_API_KEY=... python tools/api_soak.py --base-url https://api.example.test \\
        --prometheus-url http://127.0.0.1:9090 --prometheus-selector 'job="vllm-main"' --dry-run

    # the planned window
    TECHSARA_API_KEY=... python tools/api_soak.py --base-url https://api.example.test \\
        --prometheus-url http://127.0.0.1:9090 --prometheus-selector 'job="vllm-main"' \\
        --stages 8,16,32,64 --stage-seconds 180 --confirm-load

Writes `requests.jsonl`, `samples.jsonl` and `summary.json` under
`--out` (default `.runtime/soak/api-soak-<UTC>/`, gitignored).

Exit codes: 0 pass (or dry-run OK), 1 completed with failures (errors, drain
timeouts, a stage with no success, or a latency gate missed), 2 config or
connectivity error, 3 aborted by a guard, 4 refused (traffic present),
5 the tool itself crashed (summary.json is still written), 130 interrupted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import re
import signal
import sys
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("api_soak needs httpx: run it with orchestrator/.venv/bin/python")

ROOT = Path(__file__).resolve().parents[1]

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_CONFIG = 2
EXIT_ABORTED = 3
EXIT_REFUSED = 4
EXIT_CRASH = 5
EXIT_INTERRUPTED = 130

SURFACE_V1_CHAT = "v1-chat"
SURFACE_V1_RESPONSES = "v1-responses"
SURFACE_CHAT_APP = "chat"
V1_SURFACES = (SURFACE_V1_CHAT, SURFACE_V1_RESPONSES)

# Outcomes of one request, and what each one means to the run.
OK = "ok"
CAPACITY = "capacity_503"
SERVER_ERROR = "server_error"  # fatal: aborts at once
CLIENT_ERROR = "client_error"  # counts against --max-errors
TRANSPORT_ERROR = "transport_error"
STREAM_IDLE = "stream_idle"
TRUNCATED = "truncated"
TOOL_ERROR = "tool_error"  # the tool could not read what came back
CANCELLED = "cancelled"  # closed by the tool (abort or drain timeout): neutral
BUDGET_OUTCOMES = frozenset({CLIENT_ERROR, TRANSPORT_ERROR, STREAM_IDLE, TRUNCATED, TOOL_ERROR})

#: The capacity refusal, in both dialects: `/v1` says "The model is at
#: capacity right now" (publicapi/errors.model_at_capacity) for BOTH lane
#: refusals, while the chat app (main.py _failure_sentence, AdmissionRejected)
#: says "The model's queue is full right now" when the line is too deep and
#: "The model is busy and could not start your request in time" when the
#: 600 s lane wait ran out. Both chat sentences are the same condition /v1
#: calls capacity (review 2026-09-13: the second one used to abort the run as
#: a server error). Matched on the message because `/v1` shares the
#: `model_unavailable` code with a real outage, and an outage must abort while
#: a capacity refusal must not.
DEFAULT_CAPACITY_PATTERN = r"at capacity|queue is full|could not start your request in time"

#: The chat app's terminal code for a turn it PARKED while the engine was
#: recovering (app/continuity.py PARKED_CODE). The row stays `queued` and the
#: continuity sweep resumes it later; nothing this tool can call undoes that.
CHAT_PARKED_CODE = "MODEL_RECOVERING"
#: Bound on one `POST /chat/stop`: stopping must never hold the run open.
CHAT_STOP_TIMEOUT_S = 10.0
#: How often a cancellation that has not landed is sent again, and when the
#: HTTP clients are closed under the streams instead (cancel_until_done).
CANCEL_RESEND_S = 0.5
CANCEL_GIVE_UP_S = 30.0

#: `/v1` error codes that are the caller's fault (CONTRACT §9, 4xx rows). An
#: in-stream error with any OTHER code — or an unknown one — is treated as a
#: server error: an unrecognised failure mid-stream is not something to keep
#: loading an engine through.
V1_CLIENT_CODES = frozenset(
    {
        "invalid_request_error",
        "context_length_exceeded",
        "invalid_api_key",
        "insufficient_scope",
        "origin_not_allowed",
        "model_not_found",
        "request_too_large",
        "idempotency_conflict",
    }
)

#: vLLM series the monitor reads, how multiple series of one name fold (an
#: `engine` label per data-parallel rank would give several), and the key
#: they are reported under. Same names the engine controller reads
#: (monitoring/engine-controller/common.py VLLM_SERIES) plus preemptions.
ENGINE_SERIES: Dict[str, Tuple[str, str]] = {
    "vllm:num_requests_running": ("running", "sum"),
    "vllm:num_requests_waiting": ("waiting", "sum"),
    "vllm:kv_cache_usage_perc": ("kv_usage", "max"),
    "vllm:num_preemptions_total": ("preemptions_total", "sum"),
    "vllm:generation_tokens_total": ("generation_tokens_total", "sum"),
    "vllm:prompt_tokens_total": ("prompt_tokens_total", "sum"),
    "vllm:iteration_tokens_total_count": ("iterations_total", "sum"),
}
#: Without these the guards cannot run, so a sample missing one is unusable.
REQUIRED_SERIES = ("running", "waiting", "kv_usage", "preemptions_total", "generation_tokens_total")
#: Counters: any of them going down means the engine process restarted.
COUNTER_KEYS = ("preemptions_total", "generation_tokens_total", "prompt_tokens_total", "iterations_total")

#: The orchestrator's admission series (app/admission.py, app/metrics.py):
#: name -> (key, filtered to the NORMAL lane). Reported, never guarded: they
#: say how much of a stage's TTFT was queueing in front of the engine.
ADMISSION_SERIES: Dict[str, Tuple[str, bool]] = {
    "llm_admission_lane_active": ("adm_active", True),
    "llm_admission_waiting": ("adm_waiting", True),
    "llm_admission_wait_seconds_sum": ("adm_wait_sum", True),
    "llm_admission_wait_seconds_count": ("adm_wait_count", True),
    "llm_admission_rejections_total": ("adm_rejections_total", False),
}
ADMISSION_LANE = "normal"
ADMISSION_COUNTERS = ("adm_wait_sum", "adm_wait_count", "adm_rejections_total")

#: Default request mix: name:weight:prompt_tokens_lo-hi:max_tokens_lo-hi.
#: Mostly chat turns, a fifth long answers (the long-OUTPUT shape nothing has
#: soaked yet), a fifth documents, and a few 32K pastes — so long prefills and
#: many decodes share engine steps, the shape the GDN faults needed.
DEFAULT_MIX = (
    "short:55:100-1500:64-384,"
    "long_answer:20:200-1500:1024-4096,"
    "document:20:3000-9000:128-512,"
    "paste:5:24000-32000:128-1024"
)

_WORDS = (
    "revenue pipeline candidate interview onboarding quarter forecast region account "
    "opportunity stage contract renewal invoice ticket incident release cluster latency "
    "throughput memory kernel driver network fabric schedule budget vendor report"
).split()


# ------------------------------------------------------------------ config --


@dataclass(frozen=True)
class MixEntry:
    name: str
    weight: float
    prompt_tokens: Tuple[int, int]
    max_tokens: Tuple[int, int]


@dataclass
class GuardConfig:
    kv_threshold: float = 0.95
    kv_seconds: float = 30.0
    wedge_seconds: float = 20.0
    blind_seconds: float = 15.0
    abort_on_preemption: bool = True


@dataclass
class TrafficConfig:
    #: running + waiting the engine may already carry before we start. The
    #: engine controller's canary is one 2-token request at a time.
    max_busy: float = 2.0
    #: generation tokens/s the engine may already be producing. One real
    #: stream on this engine is ~70-100 tok/s; a canary is a few tokens.
    max_gen_tps: float = 20.0
    window_s: float = 15.0
    between_stages: bool = True
    settle_s: float = 3.0


@dataclass
class SloConfig:
    """Optional latency gates per stage (None = not judged). A stage that
    misses one is FAIL, not ABORTED: slowness is a finding, not a distress
    signal to stop the engine for."""

    ttft_p95_max_s: Optional[float] = None
    decode_p5_min_tps: Optional[float] = None


@dataclass
class KvLayout:
    """For the pre-run KV ESTIMATE only (no guard uses it). Defaults are the
    production layout measured 2026-09-13: 799 usable blocks, 2,096 attention
    tokens per block, 3 fixed GDN-state blocks per sequence."""

    total_blocks: int = 799
    block_tokens: int = 2096
    fixed_blocks: int = 3


@dataclass
class SoakConfig:
    base_url: str = ""
    surface: str = SURFACE_V1_CHAT
    model: str = "techsara-35b"
    api_key_env: str = "TECHSARA_API_KEY"
    api_key_file: str = ""
    chat_fraction: float = 0.0
    chat_base_url: str = ""
    chat_email: str = ""
    chat_password_file: str = ""
    chat_model: str = "smart"
    chat_effort: str = "fast"
    stages: List[int] = field(default_factory=lambda: [8, 16, 32, 64])
    stage_seconds: float = 120.0
    cooldown_seconds: float = 20.0
    drain_timeout_s: float = 900.0
    mix: List[MixEntry] = field(default_factory=list)
    seed: int = 1
    temperature: float = 0.7
    stream_idle_timeout_s: float = 120.0
    connect_timeout_s: float = 10.0
    capacity_retry_cap_s: float = 5.0
    capacity_pattern: str = DEFAULT_CAPACITY_PATTERN
    max_errors: int = 0
    metrics_url: str = ""
    metrics_model_name: str = ""
    prometheus_url: str = ""
    prometheus_selector: str = 'job="vllm-main"'
    admission_metrics_url: str = ""
    admission_selector: str = 'job="orchestrator"'
    sample_interval_s: float = 2.0
    guards: GuardConfig = field(default_factory=GuardConfig)
    traffic: TrafficConfig = field(default_factory=TrafficConfig)
    slo: SloConfig = field(default_factory=SloConfig)
    kv_layout: KvLayout = field(default_factory=KvLayout)
    unique_prompts: bool = False
    out_dir: str = ""
    dry_run: bool = False
    confirm_load: bool = False


class ConfigError(ValueError):
    pass


def parse_stages(spec: str) -> List[int]:
    try:
        stages = [int(part) for part in str(spec).replace(" ", "").split(",") if part]
    except ValueError as exc:
        raise ConfigError(f"--stages must be comma-separated integers, got {spec!r}") from exc
    if not stages:
        raise ConfigError("--stages is empty")
    for n in stages:
        if n < 1 or n > 4096:
            raise ConfigError(f"a stage's concurrency must be 1..4096, got {n}")
    return stages


def _range(text: str, what: str) -> Tuple[int, int]:
    lo_s, sep, hi_s = text.partition("-")
    try:
        lo = int(lo_s)
        hi = int(hi_s) if sep else lo
    except ValueError as exc:
        raise ConfigError(f"{what} must be N or LO-HI, got {text!r}") from exc
    if lo < 1 or hi < lo:
        raise ConfigError(f"{what} must satisfy 1 <= LO <= HI, got {text!r}")
    return lo, hi


def parse_mix(spec: str) -> List[MixEntry]:
    """`name:weight:prompt_lo-hi:out_lo-hi,...` into entries."""
    entries: List[MixEntry] = []
    for chunk in [c.strip() for c in str(spec).split(",") if c.strip()]:
        parts = chunk.split(":")
        if len(parts) != 4:
            raise ConfigError(f"a mix entry is name:weight:prompt_lo-hi:max_tokens_lo-hi, got {chunk!r}")
        name, weight_s, prompt_s, out_s = parts
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
            raise ConfigError(f"a mix entry name must be 1-32 of [A-Za-z0-9_-], got {name!r}")
        try:
            weight = float(weight_s)
        except ValueError as exc:
            raise ConfigError(f"mix weight must be a number, got {weight_s!r}") from exc
        if not weight > 0:
            raise ConfigError(f"mix weight must be > 0, got {weight_s!r}")
        entries.append(MixEntry(name, weight, _range(prompt_s, f"{name} prompt tokens"), _range(out_s, f"{name} max tokens")))
    if not entries:
        raise ConfigError("--mix is empty")
    if len({e.name for e in entries}) != len(entries):
        raise ConfigError("mix entry names must be unique")
    return entries


def url_problem(flag: str, value: str) -> Optional[str]:
    """Why `value` is not a usable http(s) URL, or None. Checked before the
    run: an unparseable URL used to surface mid-run as an httpx.InvalidURL
    traceback with exit 1, the code of a real FAIL (review 2026-09-13)."""
    if not re.match(r"^https?://[^\s/]+", value or ""):
        return f"{flag} must be an http:// or https:// URL"
    try:
        url = httpx.URL(value)
        port = url.port
    except (httpx.InvalidURL, ValueError, TypeError) as exc:
        return f"{flag} is not a valid URL: {str(exc)[:120]}"
    if not url.host or (port is not None and not 0 < port < 65536):
        return f"{flag} is not a valid URL (host or port)"
    return None


def validate_config(cfg: SoakConfig) -> List[str]:
    """Every reason the run must not start, as sentences. Empty = valid."""
    errs: List[str] = []
    problem = url_problem("--base-url", cfg.base_url)
    if problem:
        errs.append(problem)
    for flag, value in (("--chat-base-url", cfg.chat_base_url), ("--metrics-url", cfg.metrics_url),
                        ("--prometheus-url", cfg.prometheus_url), ("--admission-metrics-url", cfg.admission_metrics_url)):
        problem = url_problem(flag, value) if value else None
        if problem:
            errs.append(problem)
    if cfg.surface not in V1_SURFACES:
        errs.append(f"--surface must be one of {V1_SURFACES}")
    if not 0.0 <= cfg.chat_fraction <= 1.0:
        errs.append("--chat-fraction must be between 0 and 1")
    if cfg.chat_fraction > 0 and not (cfg.chat_email and cfg.chat_password_file):
        errs.append("--chat-fraction > 0 needs --chat-email and --chat-password-file")
    if cfg.chat_fraction > 0 and not cfg.chat_base_url:
        # The public web origin has no /chat, /auth/login or /chat/stop: the
        # browser reaches them through /api/ proxies. Defaulting to
        # --base-url only ever worked when that WAS the orchestrator.
        errs.append("--chat-fraction > 0 needs --chat-base-url, the orchestrator origin serving /auth/login, /chat and /chat/stop")
    if not cfg.stages:
        errs.append("--stages is empty")
    if not cfg.mix:
        errs.append("--mix is empty")
    if not cfg.stage_seconds > 0:
        errs.append("--stage-seconds must be > 0")
    if cfg.drain_timeout_s < 0:
        errs.append("--drain-timeout must be >= 0")
    if cfg.max_errors < 0:
        errs.append("--max-errors must be >= 0")
    for flag, value in (("--ttft-p95-max", cfg.slo.ttft_p95_max_s), ("--decode-p5-min", cfg.slo.decode_p5_min_tps)):
        if value is not None and not value > 0:
            errs.append(f"{flag} must be > 0 when given")
    if bool(cfg.metrics_url) == bool(cfg.prometheus_url):
        errs.append("exactly one engine metrics source is required: --metrics-url or --prometheus-url "
                    "(the abort guards cannot run without one)")
    g = cfg.guards
    if not 0.0 < g.kv_threshold <= 1.0:
        errs.append("--kv-abort must be in (0, 1]")
    for name, value in (("--kv-abort-seconds", g.kv_seconds), ("--wedge-seconds", g.wedge_seconds),
                        ("--metrics-blind-seconds", g.blind_seconds)):
        if not value > 0:
            errs.append(f"{name} must be > 0")
    if not cfg.sample_interval_s > 0:
        errs.append("--sample-interval must be > 0")
    elif cfg.sample_interval_s * 2 > min(g.kv_seconds, g.wedge_seconds, g.blind_seconds):
        # A guard window shorter than two samples can neither confirm nor
        # clear a condition; it would fire on a single noisy reading.
        errs.append("--sample-interval must be at most half of the shortest guard window "
                    f"(kv {g.kv_seconds}s, wedge {g.wedge_seconds}s, blind {g.blind_seconds}s)")
    t = cfg.traffic
    if cfg.sample_interval_s > 0 and t.window_s < 2 * cfg.sample_interval_s:
        errs.append("--traffic-window must cover at least two samples (>= 2 x --sample-interval)")
    if t.between_stages and cfg.sample_interval_s > 0 and cfg.cooldown_seconds < t.settle_s + 2 * cfg.sample_interval_s:
        errs.append("--cooldown-seconds must be >= --traffic-settle-seconds + 2 x --sample-interval "
                    "for the between-stage traffic check (or pass --no-between-stage-traffic-check)")
    try:
        re.compile(cfg.capacity_pattern)
    except re.error as exc:
        errs.append(f"--capacity-pattern is not a valid regular expression: {exc}")
    if not cfg.dry_run and not cfg.confirm_load:
        errs.append("this tool puts real load on an engine: pass --dry-run to only check, or --confirm-load to run")
    return errs


# ------------------------------------------------------------- statistics --


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """Nearest-rank percentile (no interpolation): the smallest value with at
    least p% of the observations at or below it. None for no data — a missing
    figure is never rendered as 0."""
    data = sorted(v for v in values if v is not None)
    if not data:
        return None
    if p <= 0:
        return data[0]
    rank = math.ceil(p / 100.0 * len(data))
    return data[min(len(data), max(1, rank)) - 1]


def _r(value: Optional[float], nd: int = 3) -> Optional[float]:
    return None if value is None else round(float(value), nd)


def kv_estimate(mix: Sequence[MixEntry], concurrency: int, layout: KvLayout) -> Optional[Dict[str, float]]:
    """A planning ESTIMATE of KV use at a concurrency: the weighted mean of
    each entry's midpoint request, and the everyone-sends-the-heaviest bound."""
    if layout.total_blocks <= 0 or layout.block_tokens <= 0 or not mix:
        return None

    def blocks(tokens: float) -> int:
        return layout.fixed_blocks + math.ceil(tokens / layout.block_tokens)

    total_w = sum(e.weight for e in mix)
    mean = sum(e.weight * blocks(sum(e.prompt_tokens) / 2 + sum(e.max_tokens) / 2) for e in mix) / total_w
    heaviest = max(blocks(e.prompt_tokens[1] + e.max_tokens[1]) for e in mix)
    return {
        "expected_usage": round(concurrency * mean / layout.total_blocks, 3),
        "all_heaviest_usage": round(concurrency * heaviest / layout.total_blocks, 3),
    }


# ---------------------------------------------------------------- requests --


@dataclass
class RequestPlan:
    stage: int
    concurrency: int
    worker: int
    seq: int
    surface: str
    mix: str
    prompt_tokens_target: int
    max_tokens: int
    prompt: str
    conversation_id: str


def _ask(max_tokens: int) -> str:
    # The public API has no ignore_eos, so a long OUTPUT has to be asked for.
    if max_tokens >= 768:
        lines = max(10, max_tokens // 24)
        return (f"Write {lines} numbered lines. Each line is one complete sentence that uses two of "
                f"the words above. Do not summarise, and do not stop before line {lines}.")
    words = max(20, int(max_tokens * 0.6))
    return f"In about {words} words, describe what the text above is about."


def build_prompt(tokens: int, max_tokens: int, rng: random.Random, tag: str) -> str:
    # ~1.35 tokens per word on this vocabulary (scripts/cluster-soak.py);
    # the actual prompt size comes back in usage and is recorded.
    words = max(8, int(tokens / 1.35))
    body = " ".join(rng.choice(_WORDS) for _ in range(words))
    return f"Document {tag}:\n{body}\n\n{_ask(max_tokens)}"


def plan_request(cfg: SoakConfig, run_id: str, stage: int, concurrency: int, worker: int, seq: int) -> RequestPlan:
    """Deterministic for (seed, stage, worker, seq): a rerun with the same seed
    sends byte-identical prompts in the same order, unless `unique_prompts`
    puts the run id into each one. The conversation id always carries the
    run id (module docstring, REPEATABILITY)."""
    rng = random.Random(f"{cfg.seed}:{stage}:{worker}:{seq}")
    total_w = sum(e.weight for e in cfg.mix)
    roll = rng.random() * total_w
    entry = cfg.mix[-1]
    for candidate in cfg.mix:
        roll -= candidate.weight
        if roll < 0:
            entry = candidate
            break
    tokens = rng.randint(*entry.prompt_tokens)
    max_tokens = rng.randint(*entry.max_tokens)
    surface = SURFACE_CHAT_APP if (cfg.chat_fraction > 0 and rng.random() < cfg.chat_fraction) else cfg.surface
    conversation_id = f"soak-{run_id}-{stage}-{worker}-{seq}"[:64]
    # WHY the seed, not the run id (review 2026-09-13): the docstring promised
    # a rerun sends the same requests, while every prompt embedded a random
    # run id. The run id is now opt-in, for a run that must share no prefix.
    tag_root = run_id if cfg.unique_prompts else f"s{cfg.seed}"
    prompt = build_prompt(tokens, max_tokens, rng, f"{tag_root}-{stage}-{worker}-{seq}")
    return RequestPlan(stage, concurrency, worker, seq, surface, entry.name, tokens, max_tokens, prompt, conversation_id)


@dataclass
class RequestResult:
    stage: int
    concurrency: int
    worker: int
    seq: int
    surface: str
    mix: str
    prompt_tokens_target: int
    max_tokens: int
    started_at: float
    outcome: str = "pending"
    status: int = 0
    error_code: str = ""
    error: str = ""
    request_id: str = ""
    retry_after_s: Optional[float] = None
    ttft_s: Optional[float] = None
    total_s: Optional[float] = None
    output_tokens: int = 0
    tokens_exact: bool = False
    prompt_tokens: Optional[int] = None
    deltas: int = 0
    heartbeats: int = 0
    finish_reason: Optional[str] = None
    decode_tps: Optional[float] = None
    in_load_window: bool = True
    #: Chat-app turns only: the conversation this turn wrote to, so leftover
    #: rows can be found from requests.jsonl.
    conversation_id: str = ""

    def finish_timing(self, t_start: float, t_first: Optional[float], t_end: float) -> None:
        self.total_s = round(t_end - t_start, 4)
        if t_first is not None:
            self.ttft_s = round(t_first - t_start, 4)
            if self.output_tokens >= 2 and t_end > t_first:
                self.decode_tps = round((self.output_tokens - 1) / (t_end - t_first), 3)


class SSEParser:
    """Incremental Server-Sent Events parser over already-split lines."""

    def __init__(self) -> None:
        self.event: Optional[str] = None
        self.data: List[str] = []
        self.comments = 0

    def feed(self, line: str) -> Optional[Tuple[str, str]]:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            self.comments += 1  # a keep-alive comment frame
            return None
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "event":
            self.event = value
        elif name == "data":
            self.data.append(value)
        return None

    def flush(self) -> Optional[Tuple[str, str]]:
        return self._dispatch()

    def _dispatch(self) -> Optional[Tuple[str, str]]:
        if not self.data and self.event is None:
            return None
        frame = (self.event or "message", "\n".join(self.data))
        self.event, self.data = None, []
        return frame


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def interpret_frame(surface: str, event: str, data: str) -> List[Tuple[str, Any]]:
    """One SSE frame as signals: ("delta", text) ("usage", (prompt, output))
    ("finish", reason) ("error", (code, message)) ("done", None)."""
    out: List[Tuple[str, Any]] = []
    if surface == SURFACE_V1_CHAT:
        if data.strip() == "[DONE]":
            return [("done", None)]
        obj = _json(data)
        if not isinstance(obj, dict):
            return out
        if isinstance(obj.get("error"), dict):
            err = obj["error"]
            out.append(("error", (str(err.get("code") or ""), str(err.get("message") or ""))))
        usage = obj.get("usage")
        if isinstance(usage, dict) and usage.get("completion_tokens") is not None:
            out.append(("usage", (usage.get("prompt_tokens"), int(usage["completion_tokens"]))))
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning")
            if text:
                out.append(("delta", text))
            if choice.get("finish_reason"):
                out.append(("finish", str(choice["finish_reason"])))
        return out
    if surface == SURFACE_V1_RESPONSES:
        obj = _json(data)
        if not isinstance(obj, dict):
            return out
        kind = obj.get("type") or event
        if kind == "response.output_text.delta" and obj.get("delta"):
            out.append(("delta", obj["delta"]))
        elif kind == "response.completed":
            # A truncated answer is still `response.completed` here: the server
            # has no `response.incomplete` event (publicapi/events.py
            # EVENT_NAMES) and says so in `incomplete_details` instead
            # (publicapi/models.py, review 2026-09-13).
            response = obj.get("response") or {}
            usage = response.get("usage") or {}
            if usage.get("output_tokens") is not None:
                out.append(("usage", (usage.get("input_tokens"), int(usage["output_tokens"]))))
            details = response.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            out.append(("finish", "length" if reason == "max_output_tokens" else "stop"))
            out.append(("done", None))
        elif kind == "response.failed":
            err = (obj.get("response") or {}).get("error") or {}
            out.append(("error", (str(err.get("code") or ""), str(err.get("message") or ""))))
        elif kind == "error":
            out.append(("error", (str(obj.get("code") or ""), str(obj.get("message") or ""))))
        return out
    # the chat app (orchestrator/app/sse.py)
    obj = _json(data)
    obj = obj if isinstance(obj, dict) else {}
    if event in ("token", "reasoning") and obj.get("text"):
        out.append(("delta", obj["text"]))
    elif event == "done":
        out.append(("done", None))
    elif event == "error":
        out.append(("error", (str(obj.get("code") or ""), str(obj.get("message") or ""))))
    return out


def classify_stream_error(surface: str, code: str, message: str, capacity_re: "re.Pattern[str]") -> str:
    if capacity_re.search(message or ""):
        return CAPACITY
    if surface in V1_SURFACES and code in V1_CLIENT_CODES:
        return CLIENT_ERROR
    return SERVER_ERROR


def classify_http_failure(status: int, body: bytes, capacity_re: "re.Pattern[str]") -> Tuple[str, str, str]:
    """(outcome, code, message) for a non-200 status line. A 503 is capacity
    only when it is a JSON error whose message says so; a proxy's HTML 503 or
    a `model_recovering` is the engine being gone, and must abort."""
    obj = _json(body.decode("utf-8", "replace"))
    code, message = "", ""
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict):
            code, message = str(err.get("code") or ""), str(err.get("message") or "")
        elif isinstance(obj.get("detail"), str):
            message = obj["detail"]
    if not message:
        message = body[:300].decode("utf-8", "replace")
    if status == 503 and isinstance(obj, dict) and code in ("", "model_unavailable") and capacity_re.search(message):
        return CAPACITY, code, message
    if status >= 500:
        return SERVER_ERROR, code, message
    return CLIENT_ERROR, code, message


def _retry_after(headers: Any) -> Optional[float]:
    try:
        value = headers.get("retry-after")
        return None if value is None else max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def v1_root(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith("/v1") else base + "/v1"


def request_for(plan: RequestPlan, cfg: SoakConfig) -> Tuple[str, Dict[str, Any]]:
    """(url, JSON body) for a plan on its surface."""
    if plan.surface == SURFACE_V1_CHAT:
        return v1_root(cfg.base_url) + "/chat/completions", {
            "model": cfg.model,
            "messages": [{"role": "user", "content": plan.prompt}],
            "stream": True,
            "max_tokens": plan.max_tokens,
            "temperature": cfg.temperature,
            "stream_options": {"include_usage": True},
        }
    if plan.surface == SURFACE_V1_RESPONSES:
        return v1_root(cfg.base_url) + "/responses", {
            "model": cfg.model,
            "input": plan.prompt,
            "stream": True,
            "max_output_tokens": plan.max_tokens,
            "temperature": cfg.temperature,
        }
    base = (cfg.chat_base_url or cfg.base_url).rstrip("/")
    return base + "/chat", {
        "message": plan.prompt,
        "messages": [{"role": "user", "content": plan.prompt}],
        "session_id": plan.conversation_id,
        "conversation_id": plan.conversation_id,
        "mode": "assistant",
        "model": cfg.chat_model,
        "effort": cfg.chat_effort,
        "web_search": "off",
    }


async def execute_request(
    client: httpx.AsyncClient,
    plan: RequestPlan,
    cfg: SoakConfig,
    capacity_re: "re.Pattern[str]",
    clock: Callable[[], float] = time.monotonic,
) -> RequestResult:
    """Send one streaming request and measure it. Every failure becomes an
    outcome on the result. The one exception that escapes is cancellation,
    raised as `_Cancelled` carrying the partly measured result, so the caller
    can still record the stream it closed."""
    url, body = request_for(plan, cfg)
    result = RequestResult(plan.stage, plan.concurrency, plan.worker, plan.seq, plan.surface, plan.mix,
                           plan.prompt_tokens_target, plan.max_tokens, started_at=round(time.time(), 3),
                           conversation_id=plan.conversation_id if plan.surface == SURFACE_CHAT_APP else "")
    t_start = clock()
    t_first: Optional[float] = None
    usage_tokens: Optional[int] = None
    done = False
    parser = SSEParser()
    try:
        async with client.stream("POST", url, json=body) as resp:
            result.status = resp.status_code
            result.request_id = resp.headers.get("x-request-id", "")[:80]
            if resp.status_code != 200:
                raw = b""
                async for chunk in resp.aiter_bytes():
                    raw += chunk
                    if len(raw) > 8192:
                        break
                result.outcome, result.error_code, message = classify_http_failure(resp.status_code, raw, capacity_re)
                result.error = message[:300]
                result.retry_after_s = _retry_after(resp.headers)
                return result
            error: Optional[Tuple[str, str]] = None
            async for line in resp.aiter_lines():
                frame = parser.feed(line)
                if frame is None:
                    continue
                for kind, value in interpret_frame(plan.surface, *frame):
                    if kind == "delta":
                        result.deltas += 1
                        if t_first is None:
                            t_first = clock()
                    elif kind == "usage":
                        result.prompt_tokens = value[0] if isinstance(value[0], int) else None
                        usage_tokens = value[1]
                    elif kind == "finish":
                        result.finish_reason = value
                    elif kind == "error" and error is None:
                        error = value
                    elif kind == "done":
                        done = True
                if done:
                    break
            tail = parser.flush()
            if tail is not None and not done:
                for kind, value in interpret_frame(plan.surface, *tail):
                    if kind == "done":
                        done = True
                    elif kind == "error" and error is None:
                        error = value
            result.heartbeats = parser.comments
            if error is not None:
                result.error_code, message = error
                result.error = message[:300]
                result.outcome = classify_stream_error(plan.surface, error[0], error[1], capacity_re)
            elif not done:
                result.outcome = TRUNCATED
                result.error = "the stream ended without its terminal frame"
            else:
                result.outcome = OK
    except asyncio.CancelledError:
        result.outcome = CANCELLED
        result.heartbeats = parser.comments
        _close_tokens(result, usage_tokens)
        result.finish_timing(t_start, t_first, clock())
        raise _Cancelled(result) from None
    except httpx.ReadTimeout:
        result.outcome = STREAM_IDLE
        result.error = f"no byte for {cfg.stream_idle_timeout_s:.0f}s (not even a keep-alive)"
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        result.outcome = TRANSPORT_ERROR
        result.error = f"{type(exc).__name__}: {str(exc)[:200]}"
    except httpx.HTTPError as exc:
        result.outcome = TRANSPORT_ERROR
        result.error = f"{type(exc).__name__}: {str(exc)[:200]}"
    except Exception as exc:  # noqa: BLE001
        # A frame this tool could not read. Recorded and counted rather than
        # left to kill the worker task, which would silently lower the
        # stage's concurrency while the report still claimed the full number.
        result.outcome = TOOL_ERROR
        result.error = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        if result.outcome != CANCELLED:
            result.heartbeats = max(result.heartbeats, parser.comments)
            _close_tokens(result, usage_tokens)
            result.finish_timing(t_start, t_first, clock())
    return result


async def cancel_until_done(tasks: Sequence["asyncio.Task[Any]"], *, resend_s: float = CANCEL_RESEND_S,
                            give_up_s: float = CANCEL_GIVE_UP_S,
                            on_stuck: Optional[Callable[[], Any]] = None) -> int:
    """Cancel `tasks` and wait until every one has finished. Returns how many
    times a cancellation had to be sent again.

    WHY NOT ONE cancel() AND A gather (2026-09-13, found by this tool's own
    monitor-death test, which hung): a single Task.cancel() on an httpx
    streaming request is sometimes SWALLOWED inside the HTTP stack when it
    lands during connection setup, and the request then streams on. Measured
    on httpx 0.28.1 / httpcore 1.0.9 / anyio 4.14.2 / Python 3.12.3 against a
    wedged fake engine: 48 of 1,500 single cancels swallowed, every second
    cancel landed (scratchpad swallow.py). Against a wedged engine that sends
    keep-alives, a swallowed cancel meant an abort that never finished. So
    the cancellation is re-sent every `resend_s`, and after `give_up_s` the
    `on_stuck` fallback (closing the HTTP clients under the streams) runs.
    """
    for task in tasks:
        task.cancel()
    pending = {task for task in tasks if not task.done()}
    resends, waited, stuck_called = 0, 0.0, False
    while pending:
        _, pending = await asyncio.wait(pending, timeout=resend_s)
        if not pending:
            break
        waited += resend_s
        resends += 1
        for task in pending:
            task.cancel()
        if on_stuck is not None and not stuck_called and waited >= give_up_s:
            stuck_called = True
            result = on_stuck()
            if asyncio.iscoroutine(result):
                await result
    return resends


class _Cancelled(asyncio.CancelledError):
    """A CancelledError that still carries the measured result."""

    def __init__(self, result: RequestResult) -> None:
        super().__init__()
        self.result = result


def _close_tokens(result: RequestResult, usage_tokens: Optional[int]) -> None:
    if usage_tokens is not None:
        result.output_tokens, result.tokens_exact = usage_tokens, True
    else:
        # No usage report (the chat app, or a cut stream): one delta is at
        # least one token, so this UNDER-counts; `tokens_exact` says so.
        result.output_tokens, result.tokens_exact = result.deltas, False


# ------------------------------------------------------------ engine view --


@dataclass
class EngineSample:
    t: float  # monotonic
    wall: float
    values: Dict[str, Optional[float]]
    error: str = ""
    #: Why the orchestrator's admission series were not read this time. Never
    #: makes the sample unusable: the guards do not depend on them.
    admission_error: str = ""

    @property
    def usable(self) -> bool:
        return not self.error and all(self.values.get(k) is not None for k in REQUIRED_SERIES)

    def row(self) -> Dict[str, Any]:
        row = {"wall": round(self.wall, 3), "usable": self.usable, "error": self.error,
               **{k: v for k, v in self.values.items()}}
        if self.admission_error:
            row["admission_error"] = self.admission_error
        return row


def _fold(acc: Dict[str, Optional[float]], name: str, value: float) -> None:
    spec = ENGINE_SERIES.get(name)
    if spec is None or math.isnan(value):
        return
    key, how = spec
    prev = acc.get(key)
    if prev is None:
        acc[key] = value
    elif how == "max":
        acc[key] = max(prev, value)
    else:
        acc[key] = prev + value


def parse_exposition(text: str, model_name: str = "") -> Dict[str, Optional[float]]:
    """Fold a Prometheus text exposition into the ENGINE_SERIES keys. With
    `model_name`, only samples labelled `model_name="<it>"` count — a metrics
    endpoint serving two models must not have their queues added up."""
    acc: Dict[str, Optional[float]] = {key: None for key, _ in ENGINE_SERIES.values()}
    needle = f'model_name="{model_name}"' if model_name else ""
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        brace, space = line.find("{"), line.find(" ")
        if brace != -1 and (space == -1 or brace < space):
            name = line[:brace]
            end = line.find("}", brace)
            if end == -1:
                continue
            labels, rest = line[brace + 1:end], line[end + 1:].strip()
        else:
            if space == -1:
                continue
            name, labels, rest = line[:space], "", line[space + 1:].strip()
        if name not in ENGINE_SERIES or (needle and needle not in labels):
            continue
        try:
            value = float(rest.split(" ", 1)[0])
        except ValueError:
            continue
        _fold(acc, name, value)
    return acc


class ExpositionSource:
    """Reads a vLLM `/metrics` endpoint directly."""

    def __init__(self, client: httpx.AsyncClient, url: str, model_name: str = "", clock: Callable[[], float] = time.monotonic):
        self.client, self.url, self.model_name, self.clock = client, url, model_name, clock

    def describe(self) -> str:
        return f"exposition {self.url}" + (f" (model_name={self.model_name})" if self.model_name else "")

    async def sample(self) -> EngineSample:
        empty = {key: None for key, _ in ENGINE_SERIES.values()}
        try:
            resp = await self.client.get(self.url)
            if resp.status_code != 200:
                return EngineSample(self.clock(), time.time(), empty, f"metrics HTTP {resp.status_code}")
            values = parse_exposition(resp.text, self.model_name)
        except httpx.HTTPError as exc:
            return EngineSample(self.clock(), time.time(), empty, f"{type(exc).__name__}: {str(exc)[:160]}")
        missing = [k for k in REQUIRED_SERIES if values.get(k) is None]
        return EngineSample(self.clock(), time.time(), values, f"missing series: {', '.join(missing)}" if missing else "")


class PrometheusSource:
    """Reads the same series through a Prometheus query API. Samples lag by
    one scrape interval (5 s here), and a target that stopped answering keeps
    its last value for Prometheus' staleness window — so the scrape TIMESTAMP
    is read too, and a sample older than the blind window is unusable."""

    def __init__(self, client: httpx.AsyncClient, base: str, selector: str, max_age_s: float,
                 clock: Callable[[], float] = time.monotonic):
        self.client, self.base, self.selector, self.max_age_s, self.clock = client, base.rstrip("/"), selector, max_age_s, clock

    def describe(self) -> str:
        return f"prometheus {self.base} {{{self.selector}}}"

    def _matchers(self, name_matcher: str) -> str:
        sel = self.selector.strip().strip("{}").strip()
        return "{" + name_matcher + ("," + sel if sel else "") + "}"

    async def _query(self, promql: str) -> List[Dict[str, Any]]:
        resp = await self.client.get(f"{self.base}/api/v1/query", params={"query": promql})
        resp.raise_for_status()
        doc = resp.json()
        if doc.get("status") != "success":
            raise httpx.HTTPError(f"prometheus: {str(doc.get('error'))[:160]}")
        return list((doc.get("data") or {}).get("result") or [])

    async def sample(self) -> EngineSample:
        empty = {key: None for key, _ in ENGINE_SERIES.values()}
        names = "|".join(re.escape(n.split(":", 1)[1]) for n in ENGINE_SERIES)
        try:
            series = await self._query(self._matchers(f'__name__=~"vllm:({names})"'))
            stamps = await self._query("max(timestamp(" + self._matchers('__name__="vllm:generation_tokens_total"') + "))")
        except (httpx.HTTPError, ValueError) as exc:
            return EngineSample(self.clock(), time.time(), empty, f"{type(exc).__name__}: {str(exc)[:160]}")
        values = dict(empty)
        for item in series:
            try:
                _fold(values, str(item["metric"]["__name__"]), float(item["value"][1]))
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        now = time.time()
        error = ""
        try:
            scraped = float(stamps[0]["value"][1]) if stamps else None
        except (KeyError, IndexError, TypeError, ValueError):
            scraped = None
        missing = [k for k in REQUIRED_SERIES if values.get(k) is None]
        if missing:
            error = f"missing series: {', '.join(missing)}"
        elif scraped is None or now - scraped > self.max_age_s:
            error = "stale: the engine's last scrape is " + ("unknown" if scraped is None else f"{now - scraped:.0f}s old")
        return EngineSample(self.clock(), now, values, error)


def _parse_labels(labels: str) -> Dict[str, str]:
    return dict(re.findall(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"', labels))


def fold_admission(acc: Dict[str, Optional[float]], name: str, labels: Dict[str, str], value: float) -> None:
    spec = ADMISSION_SERIES.get(name)
    if spec is None or math.isnan(value):
        return
    key, lane_only = spec
    if lane_only and labels.get("lane") != ADMISSION_LANE:
        return
    acc[key] = value if acc.get(key) is None else float(acc[key]) + value


def finish_admission(acc: Dict[str, Optional[float]]) -> Tuple[Dict[str, Optional[float]], str]:
    """(values, error). WHY counters default to 0 (2026-09-13): app/metrics.py
    creates a counter or histogram series on its FIRST event, so a process
    that exports its lane gauges but no `llm_admission_wait_seconds_count`
    has not had anyone wait yet. Without the gauges nothing is known, and
    every value stays None (null never means zero)."""
    if acc.get("adm_active") is None and acc.get("adm_waiting") is None:
        return {key: None for key, _ in ADMISSION_SERIES.values()}, "no llm_admission lane series for the normal lane"
    for key in ADMISSION_COUNTERS:
        if acc.get(key) is None:
            acc[key] = 0.0
    return acc, ""


class AdmissionReader:
    """The orchestrator's NORMAL admission lane, read from its own `/metrics`
    or through Prometheus. Best effort: a failure is recorded on the sample
    and never blinds the engine guards."""

    def __init__(self, client: httpx.AsyncClient, *, url: str = "", prometheus: str = "", selector: str = ""):
        self.client, self.url, self.prometheus, self.selector = client, url, prometheus.rstrip("/"), selector

    def describe(self) -> str:
        return f"exposition {self.url}" if self.url else f"prometheus {self.prometheus} {{{self.selector}}}"

    async def read(self) -> Tuple[Dict[str, Optional[float]], str]:
        acc: Dict[str, Optional[float]] = {key: None for key, _ in ADMISSION_SERIES.values()}
        try:
            if self.url:
                resp = await self.client.get(self.url)
                if resp.status_code != 200:
                    return acc, f"admission metrics HTTP {resp.status_code}"
                for line in resp.text.splitlines():
                    if not line or line.startswith("#"):
                        continue
                    match = re.match(r"^([A-Za-z_:][A-Za-z0-9_:]*)(\{[^}]*\})?\s+(\S+)", line)
                    if not match or match.group(1) not in ADMISSION_SERIES:
                        continue
                    try:
                        fold_admission(acc, match.group(1), _parse_labels(match.group(2) or ""), float(match.group(3)))
                    except ValueError:
                        continue
            else:
                names = "|".join(re.escape(n[len("llm_admission_"):]) for n in ADMISSION_SERIES)
                sel = self.selector.strip().strip("{}").strip()
                query = '{__name__=~"llm_admission_(' + names + ')"' + ("," + sel if sel else "") + "}"
                resp = await self.client.get(f"{self.prometheus}/api/v1/query", params={"query": query})
                resp.raise_for_status()
                doc = resp.json()
                if doc.get("status") != "success":
                    return acc, f"prometheus: {str(doc.get('error'))[:160]}"
                for item in (doc.get("data") or {}).get("result") or []:
                    try:
                        metric = dict(item["metric"])
                        fold_admission(acc, str(metric.get("__name__")), metric, float(item["value"][1]))
                    except (KeyError, IndexError, TypeError, ValueError):
                        continue
        except (httpx.HTTPError, ValueError) as exc:
            return acc, f"{type(exc).__name__}: {str(exc)[:160]}"
        return finish_admission(acc)


@dataclass
class Abort:
    reason: str
    message: str
    evidence: Dict[str, Any] = field(default_factory=dict)


class EngineGuards:
    """The engine-side abort rules as a state machine fed one sample at a
    time. Pure: no I/O, no clock of its own (the sample carries its time), so
    every rule is testable with synthetic timestamps."""

    def __init__(self, cfg: GuardConfig, now: float, baseline: Optional[EngineSample] = None):
        self.cfg = cfg
        self.last_ok_at = now
        self.prev: Optional[Dict[str, Optional[float]]] = None
        self.kv_high_since: Optional[float] = None
        self.last_progress_at = now
        self.notes: List[str] = []
        self._prefill_note = False
        if baseline is not None and baseline.usable:
            self.prev = dict(baseline.values)
            self.last_ok_at = baseline.t

    def check_blind(self, now: float, why: str = "") -> Optional[Abort]:
        silent = now - self.last_ok_at
        if silent >= self.cfg.blind_seconds:
            return Abort("metrics_blind",
                         f"no usable engine metrics sample for {silent:.1f}s (limit {self.cfg.blind_seconds:.0f}s): "
                         "the guards cannot see the engine",
                         {"last_error": why})
        return None

    def observe(self, sample: EngineSample) -> Optional[Abort]:
        now = sample.t
        if not sample.usable:
            return self.check_blind(now, sample.error)
        v, prev, cfg = sample.values, self.prev, self.cfg
        self.last_ok_at = now
        evidence = {k: v.get(k) for k in ("running", "waiting", "kv_usage", "preemptions_total",
                                          "generation_tokens_total", "iterations_total")}
        if prev is not None:
            for key in COUNTER_KEYS:
                if v.get(key) is not None and prev.get(key) is not None and v[key] < prev[key]:
                    return Abort("engine_restarted",
                                 f"{key} went backwards ({prev[key]:.0f} -> {v[key]:.0f}): the engine process restarted",
                                 evidence)
            if cfg.abort_on_preemption and v["preemptions_total"] > prev["preemptions_total"]:
                return Abort("preemption",
                             f"vllm:num_preemptions_total rose {prev['preemptions_total']:.0f} -> {v['preemptions_total']:.0f}: "
                             "the KV pool ran out and a sequence is being recomputed",
                             evidence)
        kv = float(v["kv_usage"])
        if kv > cfg.kv_threshold:
            if self.kv_high_since is None:
                self.kv_high_since = now
            elif now - self.kv_high_since >= cfg.kv_seconds:
                return Abort("kv_saturated",
                             f"kv_cache_usage_perc {kv:.3f} > {cfg.kv_threshold} for {now - self.kv_high_since:.1f}s "
                             f"(limit {cfg.kv_seconds:.0f}s)",
                             evidence)
        else:
            self.kv_high_since = None
        running = float(v["running"])
        if running <= 0 or prev is None or float(prev.get("running") or 0.0) <= 0:
            # Nothing to freeze, or requests JUST appeared: a freeze is
            # measured from the first sample that shows them running, not
            # from the last idle sample before it.
            self.last_progress_at = now
            self._prefill_note = False
        elif v["generation_tokens_total"] != prev["generation_tokens_total"]:
            self.last_progress_at = now
            self._prefill_note = False
        elif (v.get("iterations_total") is not None and prev.get("iterations_total") is not None
              and v["iterations_total"] != prev["iterations_total"]):
            # The 2026-09-12 exemption (module docstring): steps advancing
            # with flat generation is a long prefill, not a hang.
            self.last_progress_at = now
            if not self._prefill_note:
                self.notes.append(f"generation flat with {running:.0f} running but the scheduler is stepping: a long prefill")
                self._prefill_note = True
        elif now - self.last_progress_at >= cfg.wedge_seconds:
            return Abort("wedge",
                         f"generation_tokens_total flat at {v['generation_tokens_total']:.0f} for "
                         f"{now - self.last_progress_at:.1f}s with {running:.0f} requests running "
                         f"(limit {cfg.wedge_seconds:.0f}s)"
                         + ("" if v.get("iterations_total") is None else "; the scheduler-step counter is flat too"),
                         evidence)
        self.prev = dict(v)
        return None


def evaluate_traffic(samples: Sequence[EngineSample], cfg: TrafficConfig) -> Tuple[bool, Dict[str, Any]]:
    """Is the engine already serving someone? From samples taken while THIS
    tool sends nothing."""
    usable = [s for s in samples if s.usable]
    detail: Dict[str, Any] = {"samples": len(samples), "usable": len(usable),
                              "max_busy_allowed": cfg.max_busy, "max_gen_tps_allowed": cfg.max_gen_tps}
    if len(usable) < 2:
        detail["reason"] = "fewer than two usable engine samples: cannot tell whether real traffic is present"
        return False, detail
    max_busy = max(float(s.values["running"]) + float(s.values["waiting"]) for s in usable)
    dt = usable[-1].t - usable[0].t
    gen_delta = float(usable[-1].values["generation_tokens_total"]) - float(usable[0].values["generation_tokens_total"])
    tps = gen_delta / dt if dt > 0 else 0.0
    detail.update({"max_busy": max_busy, "gen_tps": round(tps, 2), "window_s": round(dt, 2)})
    if gen_delta < 0:
        detail["reason"] = "generation_tokens_total went backwards during the check: the engine restarted"
        return False, detail
    if max_busy > cfg.max_busy:
        detail["reason"] = f"the engine is carrying {max_busy:.0f} running+waiting requests (allowed {cfg.max_busy:.0f})"
        return False, detail
    if tps > cfg.max_gen_tps:
        detail["reason"] = f"the engine is generating {tps:.1f} tokens/s (allowed {cfg.max_gen_tps:.0f})"
        return False, detail
    return True, detail


# -------------------------------------------------------------------- run --


def admission_stats(samples: Sequence[Dict[str, Any]], snaps: Dict[str, Optional[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """The orchestrator's NORMAL lane over one stage: how deep the line got,
    how many requests had to wait for a slot and for how long on average.
    None when the lane was not read."""
    rows = [s for s in samples if s.get("adm_active") is not None or s.get("adm_waiting") is not None]
    start, end = snaps.get("start") or {}, snaps.get("end") or {}
    if not rows and start.get("adm_wait_count") is None:
        return None

    def delta(key: str) -> Optional[float]:
        a, b = start.get(key), end.get(key)
        if a is None or b is None or b < a:  # b < a: the orchestrator restarted
            return None
        return b - a

    waited, wait_sum = delta("adm_wait_count"), delta("adm_wait_sum")
    return {
        "active_max": max((s["adm_active"] for s in rows if s.get("adm_active") is not None), default=None),
        "waiting_max": max((s["adm_waiting"] for s in rows if s.get("adm_waiting") is not None), default=None),
        "queued_requests": waited,
        "queued_wait_mean_s": _r(wait_sum / waited, 2) if waited and wait_sum is not None else None,
        "rejections": delta("adm_rejections_total"),
    }


def slo_failures(stats: Dict[str, Any], slo: SloConfig) -> List[str]:
    """The latency gates a stage missed, as sentences. A gate that was asked
    for but cannot be measured on a stage that served requests is a miss:
    an unjudged stage must not read as a passed one."""
    fails: List[str] = []
    if not stats.get("ok"):
        return fails  # already FAIL as a stage without success
    p95 = stats["ttft_s"]["p95"]
    if slo.ttft_p95_max_s is not None:
        if p95 is None:
            fails.append("TTFT p95 could not be measured")
        elif p95 > slo.ttft_p95_max_s:
            fails.append(f"TTFT p95 {p95}s > {slo.ttft_p95_max_s}s")
    p5 = stats["decode_tps_per_stream"]["p5"]
    if slo.decode_p5_min_tps is not None:
        if p5 is None:
            fails.append("per-stream decode p5 could not be measured")
        elif p5 < slo.decode_p5_min_tps:
            fails.append(f"per-stream decode p5 {p5} tok/s < {slo.decode_p5_min_tps} tok/s")
    return fails


def stage_stats(results: Sequence[RequestResult], samples: Sequence[Dict[str, Any]], snaps: Dict[str, Optional[Dict[str, Any]]],
                load_seconds: float, wall_seconds: float) -> Dict[str, Any]:
    """One stage's figures. TTFT and decode rates are over successful
    requests; token totals include partial streams."""
    counts: Dict[str, int] = {}
    for r in results:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    ok = [r for r in results if r.outcome == OK]
    ttfts = [r.ttft_s for r in ok if r.ttft_s is not None]
    decode = [r.decode_tps for r in ok if r.decode_tps is not None]
    tokens = sum(r.output_tokens for r in results)
    finish: Dict[str, int] = {}
    for r in ok:
        finish[r.finish_reason or "unknown"] = finish.get(r.finish_reason or "unknown", 0) + 1

    def engine_tps(a: str, b: str) -> Optional[float]:
        sa, sb = snaps.get(a), snaps.get(b)
        if not sa or not sb or sa.get("generation_tokens_total") is None or sb.get("generation_tokens_total") is None:
            return None
        dt = sb["_t"] - sa["_t"]
        return None if dt <= 0 else round((sb["generation_tokens_total"] - sa["generation_tokens_total"]) / dt, 2)

    usable = [s for s in samples if s.get("usable")]
    load = [s for s in samples if s.get("phase") == "load"]
    pre_s, pre_e = (snaps.get("start") or {}).get("preemptions_total"), (snaps.get("end") or {}).get("preemptions_total")
    return {
        "requests": len(results),
        "outcomes": counts,
        "ok": len(ok),
        "capacity_503": counts.get(CAPACITY, 0),
        "errors": sum(counts.get(o, 0) for o in BUDGET_OUTCOMES) + counts.get(SERVER_ERROR, 0),
        "ttft_s": {"p50": _r(percentile(ttfts, 50)), "p95": _r(percentile(ttfts, 95)),
                   "p99": _r(percentile(ttfts, 99)), "max": _r(max(ttfts) if ttfts else None)},
        "decode_tps_per_stream": {"p5": _r(percentile(decode, 5), 2), "p50": _r(percentile(decode, 50), 2),
                                  "p95": _r(percentile(decode, 95), 2)},
        "output_tokens": tokens,
        "tokens_exact_share": _r(sum(1 for r in results if r.tokens_exact) / len(results), 3) if results else None,
        "client_tps_aggregate": _r(tokens / wall_seconds, 2) if wall_seconds > 0 else None,
        "engine_tps_load_window": engine_tps("start", "deadline"),
        "engine_tps_stage": engine_tps("start", "end"),
        "finish_reasons": finish,
        "mean_inflight_load": _r(sum(s.get("inflight", 0) for s in load) / len(load), 2) if load else None,
        "engine_max": {
            "running": max((s["running"] for s in usable), default=None),
            "waiting": max((s["waiting"] for s in usable), default=None),
            "kv_usage": _r(max((s["kv_usage"] for s in usable), default=None), 4),
        },
        "preemptions_delta": None if pre_s is None or pre_e is None else pre_e - pre_s,
        "orchestrator_normal_lane": admission_stats(samples, snaps),
        "load_seconds": _r(load_seconds, 2),
        "wall_seconds": _r(wall_seconds, 2),
        "snapshots": {k: ({kk: vv for kk, vv in v.items() if kk != "_t"} if v else None) for k, v in snaps.items()},
    }


def _new_summary(run_id: str) -> Dict[str, Any]:
    return {"tool": "api_soak", "run_id": run_id, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "stages": []}


class Soak:
    def __init__(self, cfg: SoakConfig, *, clock: Callable[[], float] = time.monotonic, echo: Callable[[str], None] = print):
        self.cfg = cfg
        self.clock = clock
        self.echo = echo
        self.run_id = uuid.uuid4().hex[:8]
        #: Filled as the run goes, so a crash still has something to write.
        self.summary: Dict[str, Any] = _new_summary(self.run_id)
        self.capacity_re = re.compile(cfg.capacity_pattern, re.I)
        self.abort: Optional[Abort] = None
        self.abort_event = asyncio.Event()
        self._engine_lock = asyncio.Lock()
        self.inflight = 0
        self.stage_idx: Optional[int] = None
        self.phase = "setup"
        self.budget_errors = 0
        self.samples: List[Dict[str, Any]] = []
        self.results: List[RequestResult] = []
        self.guards: Optional[EngineGuards] = None
        self.out = Path(cfg.out_dir) if cfg.out_dir else ROOT / ".runtime" / "soak" / time.strftime("api-soak-%Y%m%dT%H%M%SZ", time.gmtime())
        self._req_fh: Any = None
        self._sample_fh: Any = None
        self.api_client: Optional[httpx.AsyncClient] = None
        self.chat_client: Optional[httpx.AsyncClient] = None
        #: Its own client for POST /chat/stop: a stop must not queue behind the
        #: streams it is stopping, and must survive their client being closed.
        self.stop_client: Optional[httpx.AsyncClient] = None
        self.metrics_client: Optional[httpx.AsyncClient] = None
        self.source: Any = None
        self.admission: Optional[AdmissionReader] = None
        # Chat turns handed to POST /chat/stop (see _stop_chat_turn).
        self._chat_stop_tasks: Set[asyncio.Task] = set()
        self.chat_stops: List[Dict[str, Any]] = []
        self.parked_chat: List[str] = []
        self.chat_requests = 0
        self._finished = False

    # -- plumbing ----------------------------------------------------------

    def trigger(self, abort: Abort) -> None:
        if self.abort is None:
            self.abort = abort
            self.echo(f"ABORT [{abort.reason}] {abort.message}")
        self.abort_event.set()

    def _write(self, fh: Any, row: Dict[str, Any]) -> None:
        """Append one evidence row. WHY it cannot raise (review 2026-09-13):
        an OSError here (disk full) used to escape into the monitor task and
        kill it silently, leaving every guard blind while the load went on.
        A soak whose evidence cannot be written is aborted instead."""
        if fh is None:
            return
        try:
            fh.write(json.dumps(row, default=str) + "\n")
            fh.flush()
        except (OSError, ValueError) as exc:
            self.trigger(Abort("evidence_unwritable",
                               f"cannot write the run's evidence ({type(exc).__name__}: {str(exc)[:160]})",
                               {"file": str(getattr(fh, "name", ""))}))

    def _record_sample(self, sample: EngineSample, kind: str = "monitor") -> Dict[str, Any]:
        row = {"kind": kind, "stage": self.stage_idx, "phase": self.phase, "inflight": self.inflight, **sample.row()}
        row["_t"] = sample.t
        self.samples.append(row)
        self._write(self._sample_fh, {k: v for k, v in row.items() if k != "_t"})
        return row

    def _record_result(self, result: RequestResult) -> None:
        self.results.append(result)
        self._write(self._req_fh, asdict(result))
        if result.outcome == SERVER_ERROR:
            self.trigger(Abort("server_error",
                               f"{result.surface} request answered {result.status or 'in-stream'} "
                               f"{result.error_code or ''} {result.error[:160]!r}".replace("  ", " "),
                               {"status": result.status, "code": result.error_code, "request_id": result.request_id,
                                "stage": result.stage, "worker": result.worker, "seq": result.seq}))
        elif result.outcome in BUDGET_OUTCOMES:
            self.budget_errors += 1
            if self.budget_errors > self.cfg.max_errors:
                self.trigger(Abort("error_budget",
                                   f"{self.budget_errors} failed request(s) (allowed {self.cfg.max_errors}); latest: "
                                   f"{result.outcome} {result.status or ''} {result.error[:160]!r}",
                                   {"status": result.status, "outcome": result.outcome, "request_id": result.request_id}))

    async def _sample(self) -> EngineSample:
        """One engine sample, plus the admission lane when it is read. Never
        raises anything but cancellation: whatever goes wrong becomes an
        UNUSABLE sample, which feeds the blind guard instead of killing the
        task that reads it."""
        timeout = max(0.5, min(5.0, self.cfg.guards.blind_seconds / 2))
        empty = {k: None for k, _ in ENGINE_SERIES.values()}
        try:
            sample = await asyncio.wait_for(self.source.sample(), timeout)
        except asyncio.TimeoutError:
            return EngineSample(self.clock(), time.time(), empty, f"metrics sample took longer than {timeout:.1f}s")
        except Exception as exc:  # noqa: BLE001
            return EngineSample(self.clock(), time.time(), empty,
                                f"metrics read failed: {type(exc).__name__}: {str(exc)[:160]}")
        if self.admission is not None:
            try:
                values, error = await asyncio.wait_for(self.admission.read(), min(timeout, 3.0))
            except asyncio.TimeoutError:
                values, error = {}, "admission read timed out"
            except Exception as exc:  # noqa: BLE001
                values, error = {}, f"{type(exc).__name__}: {str(exc)[:160]}"
            for key, _ in ADMISSION_SERIES.values():
                sample.values[key] = values.get(key)
            sample.admission_error = error
        return sample

    async def _read_engine(self, kind: str) -> Tuple[EngineSample, Dict[str, Any]]:
        """Take ONE engine sample, record it, and feed it to the guards —
        one reader at a time.

        WHY THE LOCK (2026-09-13, found by this tool's own test on Python
        3.11): the monitor and a stage snapshot used to read /metrics
        concurrently. The earlier reading could finish second, so the guards
        saw `generation_tokens_total` go 1126 -> 1120 and aborted a healthy
        run as "engine restarted". Under the lock samples reach the guards in
        the order they were taken; `_sample` is bounded, so the lock is too.
        """
        async with self._engine_lock:
            sample = await self._sample()
            try:
                row = self._record_sample(sample, kind=kind)
                abort = self.guards.observe(sample) if self.guards is not None else None
            except Exception as exc:  # noqa: BLE001
                # Fail closed: a read the guards could not evaluate is a guard
                # that did not run.
                row = {"kind": kind, "usable": False, "_t": sample.t}
                if isinstance(exc, OSError):
                    abort = Abort("evidence_unwritable", f"cannot record the {kind} engine read "
                                                         f"({type(exc).__name__}: {str(exc)[:160]})")
                else:
                    abort = Abort("guard_error", f"the {kind} engine read could not be evaluated: "
                                                 f"{type(exc).__name__}: {str(exc)[:160]}")
        if abort:
            self.trigger(abort)
        return sample, row

    async def _snapshot(self, label: str) -> Optional[Dict[str, Any]]:
        sample, row = await self._read_engine(f"snapshot:{label}")
        return row if sample.usable and row.get("usable") else None

    async def _wait_abort(self, seconds: float) -> bool:
        """Sleep up to `seconds`; True when an abort cut it short."""
        if seconds <= 0:
            return self.abort_event.is_set()
        try:
            await asyncio.wait_for(self.abort_event.wait(), seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _monitor(self) -> None:
        while not self.abort_event.is_set():
            started = self.clock()
            await self._read_engine("monitor")
            if self.abort_event.is_set():
                return
            if await self._wait_abort(self.cfg.sample_interval_s - (self.clock() - started)):
                return

    def _monitor_done(self, task: "asyncio.Task[None]") -> None:
        """Watch the watchdog (review 2026-09-13): the monitor used to be
        collected only at the end, so an exception in it was swallowed and
        the load ran on with no guard reading the engine. Its end, before the
        run's and without an abort, is itself an abort."""
        if self._finished or task.cancelled():
            return
        exc = task.exception()
        if exc is None and self.abort_event.is_set():
            return  # it stopped because the run is aborting
        self.trigger(Abort("monitor_died",
                           "the guard monitor stopped while the run was going: "
                           + (f"{type(exc).__name__}: {str(exc)[:160]}" if exc else "it returned without an abort"),
                           {}))

    # -- chat turns --------------------------------------------------------

    def _request_chat_stop(self, plan: RequestPlan, why: str) -> None:
        """Hand a chat turn to POST /chat/stop in its own task — not the
        worker's, which is usually being cancelled when this is called."""
        task = asyncio.ensure_future(self._stop_chat_turn(plan, why))
        self._chat_stop_tasks.add(task)
        task.add_done_callback(self._chat_stop_tasks.discard)

    async def _stop_chat_turn(self, plan: RequestPlan, why: str) -> None:
        """WHY (review 2026-09-13): chat-app generations are detached — the
        turn runs as a task and the SSE stream only follows it — so closing
        the stream left the turn generating on a distressed engine for its
        full length. /chat/stop cancels it. Idempotent: a finished turn
        answers {"stopped": false}."""
        entry: Dict[str, Any] = {"conversation_id": plan.conversation_id, "stage": plan.stage,
                                 "worker": plan.worker, "seq": plan.seq, "why": why}
        base = (self.cfg.chat_base_url or self.cfg.base_url).rstrip("/")
        try:
            resp = await asyncio.wait_for(
                self.stop_client.post(f"{base}/chat/stop", timeout=CHAT_STOP_TIMEOUT_S,
                                      json={"conversation_id": plan.conversation_id, "session_id": plan.conversation_id}),
                CHAT_STOP_TIMEOUT_S * 1.5)
            entry["status"] = resp.status_code
            body = _json(resp.text)
            entry["stopped"] = bool(body.get("stopped")) if resp.status_code == 200 and isinstance(body, dict) else None
        except asyncio.TimeoutError:
            entry["error"] = f"no answer in {CHAT_STOP_TIMEOUT_S * 1.5:.0f}s"
        except (httpx.HTTPError, RuntimeError) as exc:  # RuntimeError: the client was already closed
            entry["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        self.chat_stops.append(entry)

    async def _close_request_clients(self) -> None:
        """The last resort of cancel_until_done: streams that ignored their
        cancellation for CANCEL_GIVE_UP_S are cut by closing the clients
        under them. Nothing can be sent afterwards, so the run aborts."""
        self.trigger(Abort("streams_would_not_close",
                           f"open streams ignored cancellation for {CANCEL_GIVE_UP_S:.0f}s; the HTTP clients were closed"))
        for client in (self.api_client, self.chat_client):
            if client is not None:
                await client.aclose()

    async def _settle_chat_stops(self) -> None:
        while self._chat_stop_tasks:
            await asyncio.gather(*list(self._chat_stop_tasks), return_exceptions=True)

    def _chat_summary(self) -> Dict[str, Any]:
        failures = [e for e in self.chat_stops if e.get("error") or e.get("status") != 200]
        return {
            "requests": self.chat_requests,
            "conversation_prefix": f"soak-{self.run_id}-",
            "stops_sent": len(self.chat_stops),
            "stopped_while_generating": sum(1 for e in self.chat_stops if e.get("stopped") is True),
            "stop_failures": failures[:200],
            "parked_conversation_ids": list(self.parked_chat),
        }

    # -- phases ------------------------------------------------------------

    def _api_key(self) -> Tuple[Optional[str], str]:
        if self.cfg.api_key_file:
            try:
                key = Path(self.cfg.api_key_file).read_text().strip()
            except OSError as exc:
                return None, f"cannot read --api-key-file: {type(exc).__name__}"
            return (key or None), "file"
        key = (os.environ.get(self.cfg.api_key_env) or "").strip()
        return (key or None), f"env:{self.cfg.api_key_env}"

    async def _connect(self) -> Tuple[bool, Dict[str, Any]]:
        """Read-only checks. Nothing here generates a token."""
        cfg, report, ok = self.cfg, {}, True
        sample, _ = await self._read_engine("connectivity")
        report["metrics"] = {"source": self.source.describe(), "usable": sample.usable, "error": sample.error,
                             "values": {k: sample.values.get(k) for k, _ in ENGINE_SERIES.values()}}
        if not sample.usable:
            ok = False
        if self.admission is not None:
            # Reported, not required: the lane figures explain TTFT, they
            # guard nothing.
            report["admission"] = {"source": self.admission.describe(), "error": sample.admission_error,
                                   "values": {k: sample.values.get(k) for k, _ in ADMISSION_SERIES.values()}}
        if cfg.chat_fraction < 1.0:
            try:
                resp = await self.api_client.get(v1_root(cfg.base_url) + "/models")
                ids = []
                if resp.status_code == 200:
                    ids = [m.get("id") for m in (resp.json().get("data") or []) if isinstance(m, dict)]
                listed = cfg.model in ids
                report["v1_models"] = {"status": resp.status_code, "model_listed": listed, "models": ids[:20]}
                ok = ok and resp.status_code == 200 and listed
            except (httpx.HTTPError, ValueError) as exc:
                report["v1_models"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
                ok = False
        if cfg.chat_fraction > 0:
            report["chat_sign_in"] = await self._chat_sign_in()
            ok = ok and bool(report["chat_sign_in"].get("ok"))
        return ok, report

    async def _chat_sign_in(self) -> Dict[str, Any]:
        base = (self.cfg.chat_base_url or self.cfg.base_url).rstrip("/")
        try:
            password = Path(self.cfg.chat_password_file).read_text().strip()
        except OSError as exc:
            return {"ok": False, "error": f"cannot read --chat-password-file: {type(exc).__name__}"}
        try:
            resp = await self.chat_client.post(f"{base}/auth/login", json={"email": self.cfg.chat_email, "password": password})
            if resp.status_code != 200:
                return {"ok": False, "login_status": resp.status_code}
            # The session cookie is Secure; httpx will not replay it over
            # plain http, so it is pinned as a header (scripts/devapi_smoke.py).
            pair = resp.headers.get("set-cookie", "").split(";", 1)[0].strip()
            if "=" in pair:
                self.chat_client.headers["Cookie"] = pair
                self.stop_client.headers["Cookie"] = pair
            me = await self.chat_client.get(f"{base}/auth/me")
            return {"ok": me.status_code == 200, "login_status": resp.status_code, "me_status": me.status_code}
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}

    async def _traffic_window(self, window_s: float) -> Tuple[bool, Dict[str, Any]]:
        collected: List[EngineSample] = []
        end = self.clock() + window_s
        while True:
            started = self.clock()
            sample, _ = await self._read_engine("traffic")
            collected.append(sample)
            if self.clock() >= end or self.abort_event.is_set():
                break
            await self._wait_abort(min(self.cfg.sample_interval_s - (self.clock() - started), end - self.clock()))
        ok, detail = evaluate_traffic(collected, self.cfg.traffic)
        detail["_last"] = collected[-1] if collected else None
        return ok, detail

    async def _worker(self, idx: int, conc: int, worker: int, deadline: float) -> None:
        seq = 0
        while not self.abort_event.is_set() and self.clock() < deadline:
            plan = plan_request(self.cfg, self.run_id, idx, conc, worker, seq)
            chat_turn = plan.surface == SURFACE_CHAT_APP
            client = self.chat_client if chat_turn else self.api_client
            self.inflight += 1
            self.chat_requests += 1 if chat_turn else 0
            try:
                result = await execute_request(client, plan, self.cfg, self.capacity_re, self.clock)
            except _Cancelled as cancelled:
                why = "abort" if self.abort_event.is_set() else "drain timeout"
                cancelled.result.in_load_window = False
                cancelled.result.error = f"closed by the tool ({why})"
                self._record_result(cancelled.result)
                if chat_turn:
                    self._request_chat_stop(plan, why)
                raise
            finally:
                self.inflight -= 1
            result.in_load_window = self.clock() <= deadline
            self._record_result(result)
            if chat_turn and result.outcome != OK:
                # Every chat turn that did not end with its answer: an idle or
                # broken stream left the detached turn running, and a stop on
                # a finished one is a no-op. A PARKED turn cannot be stopped
                # from here at all; it is listed for an operator.
                if result.error_code == CHAT_PARKED_CODE:
                    self.parked_chat.append(plan.conversation_id)
                self._request_chat_stop(plan, result.outcome)
            seq += 1
            if result.outcome == CAPACITY:
                pause = min(result.retry_after_s or 1.0, self.cfg.capacity_retry_cap_s, max(0.0, deadline - self.clock()))
                if await self._wait_abort(pause * random.uniform(0.8, 1.0)):
                    return

    async def _run_stage(self, idx: int, conc: int) -> Dict[str, Any]:
        cfg = self.cfg
        self.stage_idx, self.phase = idx, "load"
        first_result, first_sample = len(self.results), len(self.samples)
        snaps: Dict[str, Optional[Dict[str, Any]]] = {"start": await self._snapshot("start")}
        t0 = self.clock()
        deadline = t0 + cfg.stage_seconds
        self.echo(f"stage {idx + 1}/{len(cfg.stages)}: {conc} concurrent streams for {cfg.stage_seconds:.0f}s")
        workers = {asyncio.create_task(self._worker(idx, conc, w, deadline)) for w in range(conc)}
        abort_wait = asyncio.create_task(self.abort_event.wait())
        deadline_seen = False
        drain_cancelled = 0
        pending = set(workers)
        # The stage loop wakes at least this often to check, on its own, that
        # samples still reach the guards: if the monitor hangs rather than
        # dies, nothing else would notice (review 2026-09-13).
        tick = max(0.05, min(cfg.sample_interval_s, cfg.guards.blind_seconds / 3))
        try:
            while pending:
                now = self.clock()
                if not deadline_seen and now >= deadline:
                    deadline_seen = True
                    self.phase = "drain"
                    snaps["deadline"] = await self._snapshot("deadline")
                    continue
                limit = (deadline - now) if not deadline_seen else (deadline + cfg.drain_timeout_s - now)
                if deadline_seen and limit <= 0:
                    drain_cancelled = self.inflight
                    self.echo(f"  drain timeout: closing {drain_cancelled} stream(s) still open {cfg.drain_timeout_s:.0f}s after the stage")
                    break
                done, _ = await asyncio.wait(pending | {abort_wait}, timeout=max(0.0, min(limit, tick)),
                                             return_when=asyncio.FIRST_COMPLETED)
                pending -= done
                if abort_wait in done:
                    break
                if self.guards is not None:
                    blind = self.guards.check_blind(self.clock(), "no engine sample reached the guards (checked by the stage loop)")
                    if blind is not None:
                        self.trigger(blind)
                        break
        finally:
            abort_wait.cancel()
            cancel_resends = await cancel_until_done(list(pending), on_stuck=self._close_request_clients)
            if cancel_resends:
                self.echo(f"  {cancel_resends} cancellation(s) had to be re-sent before every stream closed")
            await asyncio.gather(*workers, return_exceptions=True)
            # Before the end snapshot and the between-stage traffic check: a
            # chat turn left generating would read as someone else's traffic.
            await self._settle_chat_stops()
        if not deadline_seen and not self.abort_event.is_set():
            snaps["deadline"] = await self._snapshot("deadline")
        wall = self.clock() - t0
        self.phase = "stage_end"
        snaps["end"] = await self._snapshot("end")
        stats = stage_stats(self.results[first_result:], self.samples[first_sample:], snaps,
                            min(cfg.stage_seconds, wall), wall)
        stats.update({"stage": idx + 1, "concurrency": conc, "drain_cancelled": drain_cancelled,
                      "cancel_resends": cancel_resends})
        stats["slo_failures"] = slo_failures(stats, cfg.slo)
        self.echo("  " + _stage_line(stats))
        return stats

    async def run(self) -> Tuple[int, Dict[str, Any]]:
        cfg = self.cfg
        summary = self.summary
        summary.update({"mode": "dry-run" if cfg.dry_run else "load", "config": _config_echo(cfg)})
        errs = validate_config(cfg)
        if errs:
            summary.update({"verdict": "CONFIG_ERROR", "errors": errs})
            return EXIT_CONFIG, summary
        headers = {"User-Agent": "techsara-api-soak/1"}
        key, key_source = (None, "not needed")
        if cfg.chat_fraction < 1.0:
            key, key_source = self._api_key()
            if not key:
                summary.update({"verdict": "CONFIG_ERROR",
                                "errors": [f"no API key ({key_source}): set ${cfg.api_key_env} or pass --api-key-file"]})
                return EXIT_CONFIG, summary
        summary["config"]["api_key_source"] = key_source
        peak = max(cfg.stages)
        timeout = httpx.Timeout(connect=cfg.connect_timeout_s, read=cfg.stream_idle_timeout_s, write=60.0, pool=None)
        limits = httpx.Limits(max_connections=peak + 8, max_keepalive_connections=peak + 8)
        self.api_client = httpx.AsyncClient(timeout=timeout, limits=limits,
                                            headers={**headers, **({"Authorization": f"Bearer {key}"} if key else {})})
        self.chat_client = httpx.AsyncClient(timeout=timeout, limits=limits, headers=dict(headers))
        self.stop_client = httpx.AsyncClient(timeout=httpx.Timeout(CHAT_STOP_TIMEOUT_S),
                                             limits=httpx.Limits(max_connections=16), headers=dict(headers))
        # Its own client and pool: 64 open streams must never queue a guard read.
        self.metrics_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0), limits=httpx.Limits(max_connections=4), headers=dict(headers))
        if cfg.metrics_url:
            self.source = ExpositionSource(self.metrics_client, cfg.metrics_url, cfg.metrics_model_name, self.clock)
        else:
            self.source = PrometheusSource(self.metrics_client, cfg.prometheus_url, cfg.prometheus_selector,
                                           cfg.guards.blind_seconds, self.clock)
        if cfg.admission_metrics_url:
            self.admission = AdmissionReader(self.metrics_client, url=cfg.admission_metrics_url)
        elif cfg.prometheus_url:
            self.admission = AdmissionReader(self.metrics_client, prometheus=cfg.prometheus_url, selector=cfg.admission_selector)
        self.out.mkdir(parents=True, exist_ok=True)
        self._req_fh = open(self.out / "requests.jsonl", "a", encoding="utf-8")
        self._sample_fh = open(self.out / "samples.jsonl", "a", encoding="utf-8")
        summary["out_dir"] = str(self.out)
        monitor: Optional[asyncio.Task] = None
        try:
            ok, conn = await self._connect()
            summary["connectivity"] = conn
            summary["plan"] = {"kv_estimate": {str(n): kv_estimate(cfg.mix, n, cfg.kv_layout) for n in cfg.stages}}
            if not ok:
                summary["verdict"] = "CONNECTIVITY_ERROR"
                return EXIT_CONFIG, summary
            self.echo(f"connectivity OK — metrics: {self.source.describe()}")
            self.phase = "traffic_check"
            clear, traffic = await self._traffic_window(cfg.traffic.window_s)
            baseline = traffic.pop("_last", None)
            summary["traffic_pre_start"] = traffic
            if not clear:
                summary["verdict"] = "REFUSED"
                summary["abort"] = asdict(Abort("traffic_present", traffic.get("reason", ""), traffic))
                self.echo(f"REFUSED: {traffic.get('reason')}")
                return EXIT_REFUSED, summary
            if cfg.dry_run:
                summary["verdict"] = "DRY_RUN_OK"
                return EXIT_PASS, summary
            self.guards = EngineGuards(cfg.guards, self.clock(), baseline)
            monitor = asyncio.create_task(self._monitor())
            monitor.add_done_callback(self._monitor_done)
            for idx, conc in enumerate(cfg.stages):
                if self.abort_event.is_set():
                    break
                summary["stages"].append(await self._run_stage(idx, conc))
                if self.abort_event.is_set() or idx == len(cfg.stages) - 1:
                    break
                self.phase = "cooldown"
                if await self._wait_abort(cfg.traffic.settle_s if cfg.traffic.between_stages else cfg.cooldown_seconds):
                    break
                if cfg.traffic.between_stages:
                    clear, traffic = await self._traffic_window(cfg.cooldown_seconds - cfg.traffic.settle_s)
                    traffic.pop("_last", None)
                    summary["stages"][-1]["traffic_after"] = traffic
                    if not clear:
                        self.trigger(Abort("traffic_between_stages",
                                           "after the stage drained: " + str(traffic.get("reason", "")), traffic))
                        break
        finally:
            await self._settle_chat_stops()
            self._finished = True
            if monitor is not None:
                await cancel_until_done([monitor])
                await asyncio.gather(monitor, return_exceptions=True)
            if self.guards is not None:
                self.phase = "final"
                self.guards, guards = None, self.guards  # evidence only: no abort after the end
                final, _ = await self._read_engine("final")
                self.guards = guards
                summary["engine_final"] = final.row()
                summary["guard_notes"] = self.guards.notes
            summary["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for client in (self.api_client, self.chat_client, self.stop_client, self.metrics_client):
                if client is not None:
                    await client.aclose()
            for fh in (self._req_fh, self._sample_fh):
                if fh is not None:
                    fh.close()
        if self.chat_requests or self.chat_stops:
            summary["chat_turns"] = chat = self._chat_summary()
            if chat["parked_conversation_ids"]:
                self.echo(f"WARNING: {len(chat['parked_conversation_ids'])} chat turn(s) were parked by the orchestrator; "
                          "its continuity sweep will run them when the model is back (ids in summary.json chat_turns)")
            if chat["stop_failures"]:
                self.echo(f"WARNING: {len(chat['stop_failures'])} POST /chat/stop call(s) failed; those turns may still "
                          "be generating (summary.json chat_turns.stop_failures)")
        totals = {"requests": len(self.results), "ok": sum(1 for r in self.results if r.outcome == OK),
                  "capacity_503": sum(1 for r in self.results if r.outcome == CAPACITY),
                  "budget_errors": self.budget_errors,
                  "server_errors": sum(1 for r in self.results if r.outcome == SERVER_ERROR),
                  "cancelled": sum(1 for r in self.results if r.outcome == CANCELLED),
                  "drain_cancelled": sum(s.get("drain_cancelled", 0) for s in summary["stages"])}
        summary["totals"] = totals
        first_503 = next((s["stage"] for s in summary["stages"] if s["capacity_503"]), None)
        summary["first_stage_with_capacity_503"] = first_503
        if self.abort is not None:
            summary["abort"] = asdict(self.abort)
            if self.abort.reason == "interrupted":
                summary["verdict"] = "INTERRUPTED"
                return EXIT_INTERRUPTED, summary
            summary["verdict"] = "ABORTED"
            return EXIT_ABORTED, summary
        # A stage in which NOTHING succeeded proved nothing about serving that
        # many people, even if every refusal was a well-formed capacity 503.
        totals["stages_without_success"] = [s["stage"] for s in summary["stages"] if not s["ok"]]
        totals["stages_missing_latency_gates"] = [s["stage"] for s in summary["stages"] if s.get("slo_failures")]
        summary["latency_judged"] = cfg.slo.ttft_p95_max_s is not None or cfg.slo.decode_p5_min_tps is not None
        if (totals["budget_errors"] or totals["drain_cancelled"] or totals["stages_without_success"]
                or totals["stages_missing_latency_gates"]):
            summary["verdict"] = "FAIL"
            return EXIT_FAIL, summary
        summary["verdict"] = "PASS"
        return EXIT_PASS, summary


def _config_echo(cfg: SoakConfig) -> Dict[str, Any]:
    echo = asdict(cfg)
    echo.pop("chat_password_file", None)
    echo["chat_password_file_set"] = bool(cfg.chat_password_file)
    echo["mix"] = [asdict(m) for m in cfg.mix]
    return echo


def _fmt(value: Any, unit: str = "") -> str:
    return "-" if value is None else f"{value}{unit}"


def _engine_rate(s: Dict[str, Any]) -> str:
    # The load-window rate needs the deadline snapshot; a stage cut short by
    # an abort has none, so the whole-stage rate is shown and marked.
    if s.get("engine_tps_load_window") is not None:
        return str(s["engine_tps_load_window"])
    return "-" if s.get("engine_tps_stage") is None else str(s["engine_tps_stage"]) + "(stage)"


def _stage_line(s: Dict[str, Any]) -> str:
    em = s["engine_max"]
    return (f"c={s['concurrency']} req={s['requests']} ok={s['ok']} 503cap={s['capacity_503']} err={s['errors']} "
            f"ttft p50={_fmt(s['ttft_s']['p50'], 's')} p95={_fmt(s['ttft_s']['p95'], 's')} "
            f"decode/stream p50={_fmt(s['decode_tps_per_stream']['p50'])} p5={_fmt(s['decode_tps_per_stream']['p5'])} tok/s "
            f"agg client={_fmt(s['client_tps_aggregate'])} engine={_engine_rate(s)} tok/s "
            f"engine max run={_fmt(em['running'])} wait={_fmt(em['waiting'])} kv={_fmt(em['kv_usage'])} "
            f"preempt+={_fmt(s['preemptions_delta'])}"
            + _lane_text(s.get("orchestrator_normal_lane"))
            + ("" if not s.get("slo_failures") else " LATENCY GATE MISSED: " + "; ".join(s["slo_failures"])))


def _lane_text(lane: Optional[Dict[str, Any]]) -> str:
    if not lane:
        return ""
    return (f" lane(normal) active max={_fmt(lane['active_max'])} queue max={_fmt(lane['waiting_max'])} "
            f"queued={_fmt(lane['queued_requests'])} mean wait={_fmt(lane['queued_wait_mean_s'], 's')} "
            f"refused={_fmt(lane['rejections'])}")


def render_report(summary: Dict[str, Any]) -> str:
    verdict = str(summary.get("verdict"))
    if verdict == "PASS" and not summary.get("latency_judged"):
        verdict += " (no errors and no guard fired; latency was NOT judged: pass --ttft-p95-max / --decode-p5-min)"
    lines = [f"api_soak {summary.get('mode')} run {summary.get('run_id')}: {verdict}"]
    if summary.get("crash"):
        lines.append(f"  crash: {summary['crash']}")
    for err in summary.get("errors") or []:
        lines.append(f"  config: {err}")
    conn = summary.get("connectivity") or {}
    if conn:
        m = conn.get("metrics") or {}
        lines.append(f"  metrics: {m.get('source')} usable={m.get('usable')} {m.get('error') or ''}".rstrip())
        if "v1_models" in conn:
            lines.append(f"  /v1/models: {conn['v1_models']}")
        if "chat_sign_in" in conn:
            lines.append(f"  chat sign-in: {conn['chat_sign_in']}")
        if "admission" in conn:
            a = conn["admission"]
            lines.append(f"  orchestrator lane: {a.get('source')} {a.get('error') or 'read'}")
    plan = (summary.get("plan") or {}).get("kv_estimate") or {}
    if plan:
        lines.append("  KV estimate per stage (planning only): " + ", ".join(
            f"c={n}: ~{v['expected_usage']:.0%} (all-heaviest {v['all_heaviest_usage']:.0%})" for n, v in plan.items() if v))
    traffic = summary.get("traffic_pre_start")
    if traffic:
        lines.append(f"  traffic before start: busy<={traffic.get('max_busy')} gen_tps={traffic.get('gen_tps')} "
                     f"{traffic.get('reason') or 'clear'}")
    for s in summary.get("stages") or []:
        lines.append(f"  stage {s['stage']}: " + _stage_line(s))
    if summary.get("abort"):
        a = summary["abort"]
        lines.append(f"  ABORT reason={a['reason']}: {a['message']}")
    if summary.get("totals"):
        lines.append(f"  totals: {summary['totals']}; first stage with capacity 503: {summary.get('first_stage_with_capacity_503')}")
    if summary.get("chat_turns"):
        c = summary["chat_turns"]
        lines.append(f"  chat turns: {c['requests']} sent, {c['stops_sent']} stop(s) sent, "
                     f"{c['stopped_while_generating']} stopped while generating, {len(c['stop_failures'])} stop failure(s), "
                     f"{len(c['parked_conversation_ids'])} parked; conversations start with {c['conversation_prefix']}")
    for note in summary.get("guard_notes") or []:
        lines.append(f"  note: {note}")
    if summary.get("out_dir"):
        lines.append(f"  evidence: {summary['out_dir']}")
    return "\n".join(lines)


# -------------------------------------------------------------------- CLI --


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    t = p.add_argument_group("target")
    t.add_argument("--base-url", required=True, help="API origin, e.g. https://api.example.test (a trailing /v1 is accepted)")
    t.add_argument("--surface", default=SURFACE_V1_CHAT, choices=V1_SURFACES)
    t.add_argument("--model", default="techsara-35b")
    t.add_argument("--api-key-env", default="TECHSARA_API_KEY", help="environment variable holding the Bearer key")
    t.add_argument("--api-key-file", default="", help="file holding the Bearer key (instead of the environment)")
    t.add_argument("--chat-fraction", type=float, default=0.0, help="share of requests sent as chat-app turns (0..1)")
    t.add_argument("--chat-base-url", default="",
                   help="the ORCHESTRATOR origin serving /auth/login, /chat and /chat/stop; required with --chat-fraction "
                        "(the public web origin proxies those under /api/, which this tool does not speak)")
    t.add_argument("--chat-email", default="")
    t.add_argument("--chat-password-file", default="")
    t.add_argument("--chat-model", default="smart", choices=("smart", "fast"))
    t.add_argument("--chat-effort", default="fast")
    load = p.add_argument_group("load")
    load.add_argument("--stages", default="8,16,32,64")
    load.add_argument("--stage-seconds", type=float, default=120.0)
    load.add_argument("--cooldown-seconds", type=float, default=20.0)
    load.add_argument("--drain-timeout", type=float, default=900.0, help="seconds after a stage's end before open streams are closed")
    load.add_argument("--mix", default=DEFAULT_MIX, help="name:weight:prompt_lo-hi:max_tokens_lo-hi,...")
    load.add_argument("--seed", type=int, default=1)
    load.add_argument("--unique-prompts", action="store_true",
                      help="put the run id into every prompt (default: prompts are byte-identical for a seed)")
    load.add_argument("--temperature", type=float, default=0.7)
    load.add_argument("--stream-idle-timeout", type=float, default=120.0, help="seconds with no byte (not even a keep-alive) before a stream counts as dead")
    load.add_argument("--connect-timeout", type=float, default=10.0)
    load.add_argument("--capacity-retry-cap", type=float, default=5.0, help="longest pause after a capacity 503 (Retry-After is honoured up to this)")
    load.add_argument("--capacity-pattern", default=DEFAULT_CAPACITY_PATTERN)
    m = p.add_argument_group("engine metrics (one required)")
    m.add_argument("--metrics-url", default="", help="a vLLM /metrics URL")
    m.add_argument("--metrics-model-name", default="", help="only count samples labelled model_name=<this>")
    m.add_argument("--prometheus-url", default="")
    m.add_argument("--prometheus-selector", default='job="vllm-main"')
    m.add_argument("--admission-metrics-url", default="",
                   help="the orchestrator's /metrics, for its NORMAL admission lane (default: read through --prometheus-url)")
    m.add_argument("--admission-selector", default='job="orchestrator"',
                   help="Prometheus selector for the orchestrator's llm_admission_* series")
    m.add_argument("--sample-interval", type=float, default=2.0)
    g = p.add_argument_group("abort guards")
    g.add_argument("--kv-abort", type=float, default=0.95)
    g.add_argument("--kv-abort-seconds", type=float, default=30.0)
    g.add_argument("--wedge-seconds", type=float, default=20.0)
    g.add_argument("--metrics-blind-seconds", type=float, default=15.0)
    g.add_argument("--allow-preemptions", action="store_true", help="record preemptions instead of aborting on them")
    g.add_argument("--max-errors", type=int, default=0, help="non-5xx failures tolerated before aborting")
    g.add_argument("--traffic-max-busy", type=float, default=2.0)
    g.add_argument("--traffic-max-gen-tps", type=float, default=20.0)
    g.add_argument("--traffic-window", type=float, default=15.0)
    g.add_argument("--traffic-settle-seconds", type=float, default=3.0)
    g.add_argument("--no-between-stage-traffic-check", action="store_true")
    s = p.add_argument_group("latency gates (a stage that misses one is FAIL; unset = not judged)")
    s.add_argument("--ttft-p95-max", type=float, default=None, help="seconds; TTFT includes orchestrator queueing")
    s.add_argument("--decode-p5-min", type=float, default=None, help="tokens/s per stream at the 5th percentile")
    k = p.add_argument_group("KV planning estimate")
    k.add_argument("--kv-total-blocks", type=int, default=799, help="0 disables the estimate")
    k.add_argument("--kv-block-tokens", type=int, default=2096)
    k.add_argument("--kv-fixed-blocks", type=int, default=3)
    r = p.add_argument_group("run")
    r.add_argument("--out", default="")
    mode = r.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="check config, key, model list, metrics and traffic; send no generation")
    mode.add_argument("--confirm-load", action="store_true", help="required to put load on the target")
    return p


def config_from_args(args: argparse.Namespace) -> SoakConfig:
    return SoakConfig(
        base_url=args.base_url, surface=args.surface, model=args.model, api_key_env=args.api_key_env,
        api_key_file=args.api_key_file, chat_fraction=args.chat_fraction, chat_base_url=args.chat_base_url,
        chat_email=args.chat_email, chat_password_file=args.chat_password_file, chat_model=args.chat_model,
        chat_effort=args.chat_effort, stages=parse_stages(args.stages), stage_seconds=args.stage_seconds,
        cooldown_seconds=args.cooldown_seconds, drain_timeout_s=args.drain_timeout, mix=parse_mix(args.mix),
        seed=args.seed, temperature=args.temperature, stream_idle_timeout_s=args.stream_idle_timeout,
        connect_timeout_s=args.connect_timeout, capacity_retry_cap_s=args.capacity_retry_cap,
        capacity_pattern=args.capacity_pattern, max_errors=args.max_errors, metrics_url=args.metrics_url,
        metrics_model_name=args.metrics_model_name, prometheus_url=args.prometheus_url,
        prometheus_selector=args.prometheus_selector, sample_interval_s=args.sample_interval,
        admission_metrics_url=args.admission_metrics_url, admission_selector=args.admission_selector,
        slo=SloConfig(args.ttft_p95_max, args.decode_p5_min), unique_prompts=args.unique_prompts,
        guards=GuardConfig(args.kv_abort, args.kv_abort_seconds, args.wedge_seconds, args.metrics_blind_seconds,
                           not args.allow_preemptions),
        traffic=TrafficConfig(args.traffic_max_busy, args.traffic_max_gen_tps, args.traffic_window,
                              not args.no_between_stage_traffic_check, args.traffic_settle_seconds),
        kv_layout=KvLayout(args.kv_total_blocks, args.kv_block_tokens, args.kv_fixed_blocks),
        out_dir=args.out, dry_run=args.dry_run, confirm_load=args.confirm_load,
    )


async def run_soak(cfg: SoakConfig, echo: Callable[[str], None] = print, install_signals: bool = False) -> Tuple[int, Dict[str, Any]]:
    soak = Soak(cfg, echo=echo)
    if install_signals:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, soak.trigger, Abort("interrupted", f"received {sig.name}"))
            except (NotImplementedError, RuntimeError):  # pragma: no cover - non-POSIX
                pass
    try:
        code, summary = await soak.run()
    except Exception as exc:  # noqa: BLE001
        # WHY (review 2026-09-13): an unexpected exception used to escape with
        # exit 1 — the code of a completed run with failures — and no
        # summary.json. The run's own `finally` has already closed every
        # stream and sent the chat stops by the time it gets here.
        summary = soak.summary
        summary.update({"verdict": "TOOL_CRASH", "crash": f"{type(exc).__name__}: {str(exc)[:500]}",
                        "crash_traceback": traceback.format_exc()[-4000:]})
        if soak.abort is not None:
            summary["abort"] = asdict(soak.abort)
        if soak.chat_requests or soak.chat_stops:
            summary["chat_turns"] = soak._chat_summary()
        summary.setdefault("out_dir", str(soak.out))
        echo(f"TOOL_CRASH: {type(exc).__name__}: {str(exc)[:200]}")
        code = EXIT_CRASH
    if summary.get("out_dir"):
        try:
            Path(summary["out_dir"]).mkdir(parents=True, exist_ok=True)
            Path(summary["out_dir"], "summary.json").write_text(json.dumps(summary, indent=2, default=str))
        except OSError as exc:  # pragma: no cover
            echo(f"could not write summary.json: {exc}")
    return code, summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = config_from_args(args)
    except ConfigError as exc:
        print(f"api_soak: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    code, summary = asyncio.run(run_soak(cfg, install_signals=True))
    print(render_report(summary))
    return code


if __name__ == "__main__":
    sys.exit(main())
