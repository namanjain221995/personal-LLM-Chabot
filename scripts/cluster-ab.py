#!/usr/bin/env python3
"""A/B harness for the two-node vLLM engine: the same workload matrix against
candidate A (the pinned production image) and candidate B (a trial image or
flag set), with both nodes watched the whole time.

Built on scripts/cluster-soak.py's design (monitor thread, fault/Xid/restart
signatures) but for a DIFFERENT question: not "does it survive an hour of
mixed load" but "what changed, by how much, and did anything fault". Every
phase is a function that writes its own requests.jsonl + summary.json +
summary.md, and the run as a whole writes monitor.jsonl (both nodes every 5 s),
meta.json (what was measured: image digests, engine argv, git rev) and a
SUMMARY.md. Two runs compare with `--compare`.

    # baseline on the running production engine (no restart, nothing changed):
    orchestrator/.venv/bin/python scripts/cluster-ab.py --label A --phase all \\
        --worker techsphere@10.100.184.2

    # candidate, after the SRE switch under the engine lock:
    orchestrator/.venv/bin/python scripts/cluster-ab.py --label B --phase all

    # one phase, or a tiny smoke (2 requests per phase, needle at 8K):
    orchestrator/.venv/bin/python scripts/cluster-ab.py --label A --phase c10_short
    orchestrator/.venv/bin/python scripts/cluster-ab.py --label A --smoke

    # side-by-side table with deltas:
    orchestrator/.venv/bin/python scripts/cluster-ab.py --compare .runtime/ab/A-<stamp> .runtime/ab/B-<stamp>

Phases (each writes <out>/<phase>/{requests.jsonl,summary.json,summary.md}):

  c1_short        40 short prompts, c=1            latency floor, decode tok/s
  c10_short       200 short prompts, c=10          the second tenant's shape
  c16_short       320 short prompts, c=16          over the tenant's shape
  prefill_32k     5 x 32K-token prompts, c=1       TTFT = prefill time
  prefill_128k    3 x 128K-token prompts, c=1      the long-prefill regression check
  needle_950k     1 x ~950K needle-in-haystack     correctness = 3/3 needles found
  decode_burst    16 simultaneous 256-token decodes  aggregate decode tok/s
  mixed           10 min of the soak's mixed workload at c=10 (the fault trigger)
  cancellations   20 streams cancelled after the first token; the engine must keep serving
  json_structured 30 response_format=json_object requests; json.loads must succeed
  streaming       20 streams; TTFT and inter-token gaps

Every request records: ok, http_status, error_category (bounded), ttft_s,
total_s, prompt_tokens, completion_tokens, tok_s, correct. Prompts are
synthetic (seeded word soup + the needle builder from
orchestrator/scripts/validate_long_context.py); nothing private is sent and
no answer text is written to disk beyond its length.

Read-only apart from the requests it sends: it never restarts, stops or
reconfigures anything. Seeds are deterministic (`--seed`), so A and B see the
same prompts in the same order.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import random
import re
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("needs httpx: run with orchestrator/.venv/bin/python")

ROOT = Path(__file__).resolve().parents[1]
AB_DIR = ROOT / ".runtime" / "ab"
NEEDLE_MODULE = ROOT / "orchestrator" / "scripts" / "validate_long_context.py"

PHASES = (
    "c1_short", "c10_short", "c16_short", "prefill_32k", "prefill_128k", "needle_950k",
    "decode_burst", "mixed", "cancellations", "json_structured", "streaming",
)

# The same signatures the soak and the controller watch for. Kept identical on
# purpose: a fault this harness counts is a fault the controller would act on.
FAULT_RE = re.compile(
    r"CUDA error|illegal memory access|misaligned address|AcceleratorError|"
    r"died unexpectedly|EngineDeadError|Worker proc|CUDA_SUCCESS|EngineCore.*(hit|encountered) an exception",
    re.I,
)

# Bounded error categories: nothing free-form may appear in a summary.
ERROR_CATEGORIES = ("none", "connect_error", "timeout", "http_4xx", "http_5xx", "stream_error", "malformed", "other")

_WORDS = (
    "revenue pipeline candidate interview onboarding quarter forecast region "
    "account opportunity stage contract renewal invoice ticket incident release "
    "cluster latency throughput memory kernel driver network fabric".split()
)

_SHORT_ASKS = (
    "Answer briefly and concretely.",
    "Reply in three short sentences.",
    "Summarise the list above in one sentence.",
    "Which word appears most often above? Answer with the word only.",
)


# ------------------------------------------------------------------ prompts --
def _text(tokens: int, seed: int) -> str:
    """~`tokens` tokens of seeded word soup (≈1.3 tokens per word here)."""
    rng = random.Random(seed)
    words = max(8, int(tokens / 1.35))
    return " ".join(rng.choice(_WORDS) for _ in range(words))


def _short_prompt(seed: int) -> tuple[str, int]:
    rng = random.Random(seed)
    n = rng.randint(150, 400)
    return f"Notes (id {seed}):\n{_text(n, seed)}\n\n{rng.choice(_SHORT_ASKS)}", rng.randint(96, 256)


def _mix_prompt(seed: int) -> tuple[str, int, str]:
    """The soak's mixed workload, verbatim: mostly short turns, some
    multi-thousand-token documents, a few 32K pastes, thinking on one in three.
    This is the batch shape the GDN faults fired under."""
    rng = random.Random(seed)
    roll = rng.random()
    if roll < 0.60:
        n, kind, out, ask = rng.randint(150, 1500), "short", rng.randint(64, 384), "Answer briefly and concretely."
    elif roll < 0.90:
        n, kind, out, ask = rng.randint(3000, 9000), "medium", rng.randint(128, 512), "Summarise the document above in five bullet points."
    else:
        n, kind, out, ask = rng.randint(24000, 32000), "long", 128, "In two sentences, what is the document above about?"
    return f"Document (id {seed}):\n{_text(n, seed)}\n\n{ask}", out, kind


def _load_needle_module():
    """orchestrator/scripts/validate_long_context.py's prompt builder, if importable."""
    try:
        spec = importlib.util.spec_from_file_location("validate_long_context", NEEDLE_MODULE)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        for name in ("_build_prompt", "_messages", "_resize", "NEEDLE_POSITIONS"):
            getattr(mod, name)
        return mod
    except Exception as exc:  # noqa: BLE001 — fall back to the same builder inline
        print(f"[ab] needle builder not importable ({type(exc).__name__}); using the inline copy", flush=True)
        return None


# The inline copy of the validator's builder, used only when the import fails
# (a moved file, a missing dependency). Same filler, same positions, same
# system prompt, so a run on either path is comparable.
_FILLER = (
    "The quarterly operations review noted steady throughput across the region.",
    "Scheduling remained unchanged for the reporting period under discussion.",
    "No material deviation was recorded against the published baseline plan.",
    "Coordination between the delivery and enablement teams continued as usual.",
    "Documentation was refreshed to reflect the current process description.",
)
_NEEDLE_POSITIONS = (0.02, 0.5, 0.97)


def _inline_build_prompt(target_tokens: int, chars_per_token: float, codes: list[str]) -> str:
    rng = random.Random(1337)
    parts, length, target = [], 0, int(target_tokens * chars_per_token)
    while length < target:
        line = rng.choice(_FILLER)
        parts.append(line)
        length += len(line) + 1
    text = "\n".join(parts)
    for i, (pos, code) in enumerate(zip(_NEEDLE_POSITIONS, codes), start=1):
        needle = f"IMPORTANT RECORD {i}: the verification code for checkpoint {i} is {code}."
        at = min(len(text), max(0, int(len(text) * pos)))
        boundary = text.rfind("\n", 0, at)
        at = boundary + 1 if boundary != -1 else at
        text = text[:at] + needle + "\n" + text[at:]
    return text


def _inline_messages(document: str) -> list[dict]:
    return [
        {"role": "system", "content": (
            "You answer strictly from the document provided. Reply with the three verification "
            "codes only, comma separated, in checkpoint order. No explanation.")},
        {"role": "user", "content": f"{document}\n\nList the verification codes for checkpoints 1, 2 and 3."},
    ]


# --------------------------------------------------------------- tokenizer --
async def _tokenize(client: httpx.AsyncClient, base: str, model: str, messages: list[dict]) -> int:
    resp = await client.post(f"{base}/tokenize", json={"model": model, "messages": messages})
    resp.raise_for_status()
    return int(resp.json()["count"])


async def _calibrated_document(client: httpx.AsyncClient, base: str, model: str, target: int, seed: int,
                               ask: str) -> tuple[str, int]:
    """Word soup whose EXACT token count (per /tokenize) lands within 1 % of
    `target`. Character estimates drift 20-30 % between content types, which
    at 128K is tens of thousands of tokens — enough to make A and B measure
    different prefills. Six rescales are plenty; the last count is recorded."""
    per_word = 1.35
    count = 0
    for _ in range(6):
        body = _text(int(target * per_word / 1.35), seed)
        prompt = f"Document (id {seed}):\n{body}\n\n{ask}"
        count = await _tokenize(client, base, model, [{"role": "user", "content": prompt}])
        if abs(count - target) <= max(64, target // 100):
            return prompt, count
        per_word *= target / max(1, count)
    return prompt, count


# ---------------------------------------------------------------- requests --
def _pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(round(p * (len(vals) - 1))))]


def _mean(vals: list[float]) -> float | None:
    return statistics.fmean(vals) if vals else None


def _category(exc: BaseException) -> str:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError)):
        return "connect_error" if not isinstance(exc, httpx.RemoteProtocolError) else "stream_error"
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(exc, (httpx.ReadError, httpx.WriteError, httpx.CloseError)):
        return "stream_error"
    if isinstance(exc, (json.JSONDecodeError, KeyError, ValueError)):
        return "malformed"
    return "other"


class Request:
    """One chat completion, streamed or not, timed and tokenised."""

    def __init__(self, client: httpx.AsyncClient, base: str, model: str, read_timeout: float) -> None:
        self.client, self.base, self.model, self.read_timeout = client, base, model, read_timeout

    async def run(self, *, messages: list[dict], max_tokens: int, seed: int, stream: bool = True,
                  temperature: float = 0.0, thinking: bool = False, extra: dict | None = None,
                  cancel_after_first_token: bool = False, keep_text: bool = False) -> dict:
        payload: dict[str, Any] = {
            "model": self.model, "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "seed": seed, "stream": stream,
            "chat_template_kwargs": {"enable_thinking": thinking},
        }
        if stream:
            # The final chunk then carries usage, so prompt/completion tokens
            # come from the engine, not from counting chunks.
            payload["stream_options"] = {"include_usage": True}
        if extra:
            payload.update(extra)
        rec: dict[str, Any] = {
            "at": time.time(), "seed": seed, "ok": False, "http_status": 0, "error_category": "none",
            "ttft_s": None, "total_s": None, "prompt_tokens": None, "completion_tokens": None,
            "tok_s": None, "correct": None, "cancelled": False, "chunks": 0, "answer_len": 0,
        }
        text_parts: list[str] = []
        chunk_times: list[float] = []
        t0 = time.perf_counter()
        timeout = httpx.Timeout(connect=10.0, read=self.read_timeout, write=60.0, pool=60.0)
        try:
            if stream:
                async with self.client.stream("POST", f"{self.base}/v1/chat/completions", json=payload, timeout=timeout) as resp:
                    rec["http_status"] = resp.status_code
                    if resp.status_code != 200:
                        await resp.aread()
                        rec["error_category"] = "http_5xx" if resp.status_code >= 500 else "http_4xx"
                    else:
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            obj = json.loads(data)
                            if obj.get("usage"):
                                rec["prompt_tokens"] = obj["usage"].get("prompt_tokens")
                                rec["completion_tokens"] = obj["usage"].get("completion_tokens")
                            for ch in obj.get("choices") or []:
                                delta = ch.get("delta") or {}
                                piece = delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning") or ""
                                if piece:
                                    now = time.perf_counter()
                                    if rec["ttft_s"] is None:
                                        rec["ttft_s"] = now - t0
                                    chunk_times.append(now)
                                    rec["chunks"] += 1
                                    if delta.get("content"):
                                        text_parts.append(delta["content"])
                            if cancel_after_first_token and rec["ttft_s"] is not None:
                                # Closing the response mid-stream is the client
                                # disconnect vLLM must turn into an abort.
                                rec["cancelled"] = True
                                break
                        rec["ok"] = True
            else:
                resp = await self.client.post(f"{self.base}/v1/chat/completions", json=payload, timeout=timeout)
                rec["http_status"] = resp.status_code
                if resp.status_code != 200:
                    rec["error_category"] = "http_5xx" if resp.status_code >= 500 else "http_4xx"
                else:
                    obj = resp.json()
                    usage = obj.get("usage") or {}
                    rec["prompt_tokens"] = usage.get("prompt_tokens")
                    rec["completion_tokens"] = usage.get("completion_tokens")
                    text_parts.append((obj["choices"][0].get("message") or {}).get("content") or "")
                    rec["ok"] = True
        except Exception as exc:  # noqa: BLE001 — categorised, the run goes on
            rec["error_category"] = _category(exc)
            rec["error_type"] = type(exc).__name__
        rec["total_s"] = round(time.perf_counter() - t0, 4)
        if rec["ttft_s"] is not None:
            rec["ttft_s"] = round(rec["ttft_s"], 4)
        if rec["completion_tokens"] is None and rec["ok"] and stream:
            rec["completion_tokens"] = rec["chunks"]  # no usage chunk: chunks ≈ tokens
        # Decode tok/s needs a measured TTFT (streamed), a finished stream and
        # enough tokens for the ratio to mean anything; a 2-token answer or a
        # cancelled stream would otherwise print thousands of tok/s.
        if rec["ok"] and stream and not rec["cancelled"] and rec["ttft_s"] is not None and (rec["completion_tokens"] or 0) >= 8:
            decode = rec["total_s"] - rec["ttft_s"]
            rec["tok_s"] = round(rec["completion_tokens"] / decode, 2) if decode > 0 else None
        if len(chunk_times) >= 2:
            gaps = [b - a for a, b in zip(chunk_times, chunk_times[1:])]
            rec["gap_p50_s"] = round(_pct(gaps, .5) or 0, 4)
            rec["gap_p95_s"] = round(_pct(gaps, .95) or 0, 4)
            rec["gap_max_s"] = round(max(gaps), 4)
        text = "".join(text_parts)
        rec["answer_len"] = len(text)
        if keep_text:
            rec["_text"] = text  # stripped before it reaches disk
        return rec


# ----------------------------------------------------------------- monitor --
def _sh(cmd: list[str], timeout: float = 25.0, stdin: str | None = None) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin).stdout
    except Exception as exc:  # noqa: BLE001
        return f"##error {type(exc).__name__}\n"


# One POSIX-sh snippet samples a node; it runs locally through `sh -s` and on
# the worker through `ssh ... sh -s`, so both nodes are measured the same way
# and the worker costs one round trip per sample. Raw counters only: the
# deltas (CPU %, swap pages, RoCE bytes) are computed here between samples.
_NODE_SNIPPET = r"""
CTR="$1"; SINCE="$2"
echo "##ctr"; docker inspect -f '{{.RestartCount}} {{.State.StartedAt}} {{.State.Health.Status}} {{.State.Status}}' "$CTR" 2>/dev/null
echo "##gpu"; nvidia-smi --query-gpu=utilization.gpu,power.draw,temperature.gpu --format=csv,noheader,nounits 2>/dev/null
echo "##mem"; grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo
echo "##vmstat"; grep -E '^(pswpin|pswpout) ' /proc/vmstat
echo "##loadavg"; cat /proc/loadavg
echo "##roce"; for p in /sys/class/infiniband/*/ports/1/counters; do h=$(basename "$(dirname "$(dirname "$(dirname "$p")")")"); echo "$h $(cat "$p/port_xmit_data" 2>/dev/null || echo 0) $(cat "$p/port_rcv_data" 2>/dev/null || echo 0)"; done
echo "##clk"; getconf CLK_TCK 2>/dev/null || echo 100
echo "##procs"
# One awk per process reads every task's stat at once. The comm sits in
# parentheses (field 2) and may contain spaces, so fields are re-split after
# the closing parenthesis: utime/stime are stat fields 14/15, rss 24, the
# last CPU 39.
for pid in $(docker top "$CTR" -eo pid 2>/dev/null | tail -n +2); do
  [ -r "/proc/$pid/stat" ] || continue
  awk -v pid="$pid" '{
    o = index($0, "("); c = index($0, ")"); comm = substr($0, o + 1, c - o - 1); gsub(/ /, "_", comm)
    n = split(substr($0, c + 2), f, " "); tid = $1
    if (tid == pid && FILENAME ~ /task/) next
    if (tid == pid) print "P", pid, comm, f[12] + f[13], f[22]
    else print "T", pid, tid, comm, f[12] + f[13], f[37]
  }' "/proc/$pid/stat" /proc/"$pid"/task/*/stat 2>/dev/null
done
echo "##xid"; journalctl -k -q --no-pager --since "@$SINCE" 2>/dev/null | grep -c Xid
echo "##xid_boot"; journalctl -k -q --no-pager 2>/dev/null | grep -c Xid
echo "##faults"; docker logs --since "$SINCE" "$CTR" 2>&1 | grep -E 'CUDA error|illegal memory access|misaligned address|AcceleratorError|died unexpectedly|EngineDeadError|Worker proc|CUDA_SUCCESS|EngineCore.*(hit|encountered) an exception' | tail -n 3
echo "##end"
"""


def _parse_node(raw: str) -> dict:
    sections: dict[str, list[str]] = {}
    cur = None
    for line in raw.splitlines():
        if line.startswith("##"):
            cur = line[2:].strip().split(" ", 1)[0]
            sections.setdefault(cur, [])
            if line.startswith("##error"):
                sections["error"] = [line[8:]]
            continue
        if cur is not None and line.strip():
            sections[cur].append(line)
    out: dict[str, Any] = {"error": sections.get("error", [None])[0]}
    ctr = (sections.get("ctr") or [""])[0].split()
    out["container"] = {"restart_count": int(ctr[0]) if ctr and ctr[0].isdigit() else None,
                        "started_at": ctr[1] if len(ctr) > 1 else None,
                        "health": ctr[2] if len(ctr) > 2 else None,
                        "status": ctr[3] if len(ctr) > 3 else None}
    gpu = (sections.get("gpu") or [""])[0].split(",")
    try:
        out["gpu"] = {"util": float(gpu[0]), "power_w": float(gpu[1]), "temp_c": float(gpu[2])}
    except (ValueError, IndexError):
        out["gpu"] = {"util": None, "power_w": None, "temp_c": None}
    mem = {}
    for line in sections.get("mem", []):
        k, v = line.split(":", 1)
        mem[k] = int(v.split()[0])
    out["mem_kb"] = mem
    vm = {}
    for line in sections.get("vmstat", []):
        k, v = line.split()
        vm[k] = int(v)
    out["vmstat"] = vm
    la = (sections.get("loadavg") or [""])[0].split()
    out["loadavg"] = [float(x) for x in la[:3]] if len(la) >= 3 else None
    roce = {}
    for line in sections.get("roce", []):
        parts = line.split()
        if len(parts) == 3:
            # sysfs port_*_data counts 32-bit words (octets / 4).
            roce[parts[0]] = {"xmit_bytes": int(parts[1]) * 4, "rcv_bytes": int(parts[2]) * 4}
    out["roce"] = roce
    clk = (sections.get("clk") or ["100"])[0].strip()
    out["clk_tck"] = int(clk) if clk.isdigit() else 100
    procs, threads = {}, {}
    for line in sections.get("procs", []):
        parts = line.split()
        if parts[0] == "P" and len(parts) >= 5:
            procs[int(parts[1])] = {"comm": parts[2], "jiffies": int(parts[3]), "rss_pages": int(parts[4])}
        elif parts[0] == "T" and len(parts) >= 6:
            threads[int(parts[2])] = {"pid": int(parts[1]), "comm": parts[3], "jiffies": int(parts[4]), "cpu": int(parts[5])}
    out["procs"], out["threads"] = procs, threads
    xid = (sections.get("xid") or ["0"])[0].strip()
    out["xid_since_start"] = int(xid) if xid.isdigit() else None
    xb = (sections.get("xid_boot") or ["0"])[0].strip()
    out["xid_since_boot"] = int(xb) if xb.isdigit() else None
    faults = sections.get("faults", [])
    out["fault_lines_tail"] = [ln[:200] for ln in faults]
    return out


class Monitor(threading.Thread):
    """Samples head + worker every `every` seconds into monitor.jsonl.

    CPU per process/thread is (Δjiffies / CLK_TCK) / Δt × 100 between two
    samples of the same pid — a window figure, unlike `ps pcpu` which is a
    lifetime average and never moves on a process that has been up for days.
    """

    def __init__(self, *, worker: str, head_ctr: str, worker_ctr: str, base: str, controller: str,
                 every: float, sink: Callable[[dict], None], since_epoch: int) -> None:
        super().__init__(daemon=True)
        self.worker, self.head_ctr, self.worker_ctr = worker, head_ctr, worker_ctr
        self.base, self.controller, self.every, self.sink = base, controller, every, sink
        self.since = since_epoch
        self.stop = threading.Event()
        self.phase = "idle"
        self.samples: list[dict] = []
        self._prev: dict[str, tuple[float, dict]] = {}
        self.baseline: dict[str, dict] = {}
        self.alarms: list[str] = []
        self._lock = threading.Lock()

    def _raw(self, node: str) -> dict:
        if node == "head":
            raw = _sh(["sh", "-s", self.head_ctr, str(self.since)], stdin=_NODE_SNIPPET)
        else:
            raw = _sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", self.worker, "sh", "-s",
                       self.worker_ctr, str(self.since)], stdin=_NODE_SNIPPET)
        return _parse_node(raw)

    def _engine(self) -> dict:
        out: dict[str, Any] = {"generation_tokens_total": None, "prompt_tokens_total": None, "running": None, "waiting": None}
        try:
            text = httpx.get(f"{self.base}/metrics", timeout=5.0).text
        except Exception:  # noqa: BLE001
            return out
        for line in text.splitlines():
            for key, name in (("generation_tokens_total", "vllm:generation_tokens_total"),
                              ("prompt_tokens_total", "vllm:prompt_tokens_total"),
                              ("running", "vllm:num_requests_running"),
                              ("waiting", "vllm:num_requests_waiting")):
                if line.startswith(name + "{") or line.startswith(name + " "):
                    try:
                        out[key] = float(line.rsplit(" ", 1)[1])
                    except ValueError:
                        pass
        return out

    def _controller(self) -> dict | None:
        if not self.controller:
            return None
        try:
            doc = httpx.get(f"{self.controller}/state", timeout=3.0).json()
        except Exception:  # noqa: BLE001
            return {"reachable": False}
        rec = (doc.get("recovery") or {})
        return {"reachable": True, "state": doc.get("state"), "state_code": doc.get("state_code"),
                "primary_ready": doc.get("primary_ready"), "recovery_in_progress": rec.get("in_progress"),
                "recovery_step": rec.get("step")}

    def _with_rates(self, node: str, cur: dict, now: float) -> dict:
        prev = self._prev.get(node)
        rates: dict[str, Any] = {"cpu_pct": {}, "thread_cpu_pct": {}, "pswpout_delta": None, "pswpin_delta": None, "roce_delta_bytes": {}}
        if prev is not None:
            dt = max(1e-3, now - prev[0])
            clk = cur.get("clk_tck") or 100
            for pid, p in cur["procs"].items():
                q = prev[1]["procs"].get(pid)
                if q and q["comm"] == p["comm"]:
                    rates["cpu_pct"][f"{p['comm']}:{pid}"] = round((p["jiffies"] - q["jiffies"]) / clk / dt * 100, 1)
            for tid, t in cur["threads"].items():
                q = prev[1]["threads"].get(tid)
                if q:
                    pct = (t["jiffies"] - q["jiffies"]) / clk / dt * 100
                    if pct >= 1.0:
                        rates["thread_cpu_pct"][f"{t['comm']}:{t['pid']}:{tid}"] = {"pct": round(pct, 1), "cpu": t["cpu"]}
            for k in ("pswpout", "pswpin"):
                if k in cur["vmstat"] and k in prev[1]["vmstat"]:
                    rates[f"{k}_delta"] = cur["vmstat"][k] - prev[1]["vmstat"][k]
            for hca, c in cur["roce"].items():
                q = prev[1]["roce"].get(hca)
                if q:
                    rates["roce_delta_bytes"][hca] = {"xmit": c["xmit_bytes"] - q["xmit_bytes"], "rcv": c["rcv_bytes"] - q["rcv_bytes"]}
        self._prev[node] = (now, cur)
        cur = dict(cur)
        cur.update(rates)
        # Raw per-process tables are large; keep only what the summary uses.
        cur["procs"] = {f"{p['comm']}:{pid}": {"rss_mb": round(p["rss_pages"] * 4096 / 2**20, 1)} for pid, p in cur["procs"].items()}
        cur.pop("threads", None)
        return cur

    def sample(self) -> dict:
        with self._lock:
            return self._sample_locked()

    def sample_now(self) -> None:
        """One extra sample from the caller's thread, tagged with the current
        phase, so a phase shorter than the sampling interval still has a
        GPU/CPU/RoCE reading of its own."""
        row = self.sample()
        self.samples.append(row)
        self.sink(row)

    def _sample_locked(self) -> dict:
        now = time.time()
        head = self._with_rates("head", self._raw("head"), now)
        worker = self._with_rates("worker", self._raw("worker"), now)
        row = {"at": now, "phase": self.phase, "head": head, "worker": worker, "engine": self._engine(),
               "controller": self._controller()}
        for node in ("head", "worker"):
            base = self.baseline.get(node)
            cur = row[node]["container"]
            if base and cur["started_at"] and (cur["started_at"] != base["started_at"] or cur["restart_count"] != base["restart_count"]):
                self.alarms.append(f"{node} engine container restarted ({base['started_at']} -> {cur['started_at']})")
            if row[node]["fault_lines_tail"]:
                self.alarms.append(f"{node}: CUDA/engine fault line in the container log")
            if row[node]["xid_since_start"]:
                self.alarms.append(f"{node}: {row[node]['xid_since_start']} Xid line(s) in the kernel log since the run started")
        return row

    def run(self) -> None:
        first = self.sample()
        self.baseline = {"head": first["head"]["container"], "worker": first["worker"]["container"]}
        self.samples.append(first)
        self.sink(first)
        while not self.stop.wait(self.every):
            row = self.sample()
            self.samples.append(row)
            self.sink(row)
            h, w = row["head"]["gpu"], row["worker"]["gpu"]
            print(f"[monitor] {time.strftime('%H:%M:%S')} {self.phase:15s} gpu head={h['util']}%/{h['power_w']}W "
                  f"worker={w['util']}%/{w['power_w']}W running={row['engine']['running']} "
                  f"xid={row['head']['xid_since_start']}/{row['worker']['xid_since_start']} alarms={len(set(self.alarms))}", flush=True)


# ------------------------------------------------------------------ phases --
class Run:
    def __init__(self, args: argparse.Namespace, out: Path, monitor: Monitor) -> None:
        self.args, self.out, self.monitor = args, out, monitor
        self.model = ""
        self.needle_mod = _load_needle_module()

    # -- helpers --------------------------------------------------------
    def n(self, full: int) -> int:
        return 2 if self.args.smoke else full

    def seed(self, phase: str, i: int) -> int:
        return self.args.seed + PHASES.index(phase) * 10_000 + i

    async def gather(self, tasks: list, concurrency: int) -> list[dict]:
        sem = asyncio.Semaphore(concurrency)

        async def guarded(coro):
            async with sem:
                return await coro
        return list(await asyncio.gather(*(guarded(t) for t in tasks)))

    def write_phase(self, phase: str, rows: list[dict], wall_s: float, extra: dict | None = None) -> dict:
        d = self.out / phase
        d.mkdir(parents=True, exist_ok=True)
        with (d / "requests.jsonl").open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({k: v for k, v in r.items() if not k.startswith("_")}) + "\n")
        summary = summarise(phase, rows, wall_s, [s for s in self.monitor.samples if s["phase"] == phase])
        if extra:
            summary.update(extra)
        (d / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        (d / "summary.md").write_text(phase_markdown(summary), encoding="utf-8")
        print(phase_markdown(summary), flush=True)
        return summary

    async def with_phase(self, phase: str, fn) -> dict:
        self.monitor.phase = phase
        print(f"\n=== phase {phase} ===", flush=True)
        t0 = time.time()
        try:
            rows, extra = await fn()
        finally:
            # One more monitor sample lands inside the phase window so a short
            # phase still has a GPU/CPU/RoCE reading of its own.
            await asyncio.to_thread(self.monitor.sample_now)
        wall = time.time() - t0
        self.monitor.phase = "idle"
        return self.write_phase(phase, rows, wall, extra)

    # -- the phases -----------------------------------------------------
    async def short(self, phase: str, count: int, concurrency: int, req: Request) -> tuple[list[dict], dict]:
        tasks = []
        for i in range(self.n(count)):
            prompt, out = _short_prompt(self.seed(phase, i))
            tasks.append(req.run(messages=[{"role": "user", "content": prompt}], max_tokens=out, seed=self.seed(phase, i)))
        return await self.gather(tasks, concurrency), {"concurrency": concurrency}

    async def prefill(self, phase: str, tokens: int, count: int, req: Request, client: httpx.AsyncClient) -> tuple[list[dict], dict]:
        rows = []
        for i in range(self.n(count)):
            seed = self.seed(phase, i)
            prompt, exact = await _calibrated_document(client, self.args.base, self.model, tokens, seed,
                                                       "In one sentence, what is the document above about?")
            r = await req.run(messages=[{"role": "user", "content": prompt}], max_tokens=16, seed=seed)
            r["target_tokens"], r["tokenized_tokens"] = tokens, exact
            # TTFT is the prefill: report prefill tokens/s per request.
            if r["ok"] and r["ttft_s"]:
                r["prefill_tok_s"] = round((r["prompt_tokens"] or exact) / r["ttft_s"], 1)
            rows.append(r)
        return rows, {"target_tokens": tokens}

    async def needle(self, phase: str, req: Request, client: httpx.AsyncClient) -> tuple[list[dict], dict]:
        target = 8_192 if self.args.smoke else self.args.needle_tokens
        codes = [f"TS-{target}-{i}{i}{i}{i}" for i in (7, 4, 9)]
        base_v1 = f"{self.args.base}/v1"
        if self.needle_mod is not None:
            document, exact = await self.needle_mod._resize(client, base_v1, self.model, target, codes)
            messages = self.needle_mod._messages(document)
        else:
            cpt = 4.0
            document = _inline_build_prompt(target, cpt, codes)
            exact = await _tokenize(client, self.args.base, self.model, _inline_messages(document))
            for _ in range(6):
                if abs(exact - target) <= max(512, target // 100):
                    break
                cpt *= target / max(1, exact)
                document = _inline_build_prompt(target, cpt, codes)
                exact = await _tokenize(client, self.args.base, self.model, _inline_messages(document))
            messages = _inline_messages(document)
        r = await req.run(messages=messages, max_tokens=128, seed=self.seed(phase, 0), stream=False, keep_text=True)
        found = [c for c in codes if c in r.get("_text", "")]
        r["target_tokens"], r["tokenized_tokens"] = target, exact
        r["needles_found"], r["needles_total"] = len(found), len(codes)
        r["correct"] = r["ok"] and len(found) == len(codes)
        if r["ok"] and r["total_s"]:
            r["prefill_tok_s"] = round((r["prompt_tokens"] or exact) / r["total_s"], 1)
        return [r], {"target_tokens": target}

    async def decode_burst(self, phase: str, req: Request) -> tuple[list[dict], dict]:
        n = self.n(16)
        tasks = []
        for i in range(n):
            seed = self.seed(phase, i)
            prompt = f"Write a long, detailed essay about {random.Random(seed).choice(_WORDS)} management. Seed {seed}."
            # min_tokens + ignore_eos pin every request at exactly 256 decode
            # steps, so the 16 streams overlap for their whole life.
            tasks.append(req.run(messages=[{"role": "user", "content": prompt}], max_tokens=256, seed=seed,
                                 extra={"min_tokens": 256, "ignore_eos": True}))
        return await self.gather(tasks, n), {"concurrency": n}

    async def mixed(self, phase: str, req: Request) -> tuple[list[dict], dict]:
        minutes = self.args.mixed_minutes
        concurrency = 10
        if self.args.smoke:
            minutes, concurrency = 0.0, 2
        rows: list[dict] = []
        counter = 0
        lock = asyncio.Lock()
        deadline = time.monotonic() + minutes * 60

        async def worker() -> None:
            nonlocal counter
            while True:
                async with lock:
                    if (minutes and time.monotonic() >= deadline) or (not minutes and counter >= 2):
                        return
                    counter += 1
                    i = counter
                seed = self.seed(phase, i)
                prompt, out, kind = _mix_prompt(seed)
                r = await req.run(messages=[{"role": "user", "content": prompt}], max_tokens=out, seed=seed,
                                  temperature=0.7, thinking=bool(seed % 3 == 0))
                r["kind"] = kind
                rows.append(r)
                if r["error_category"] != "none":
                    # Back off like the soak: a dead port must not be hammered.
                    await asyncio.sleep(2.0)
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        return rows, {"concurrency": concurrency, "minutes": minutes}

    async def cancellations(self, phase: str, req: Request, client: httpx.AsyncClient) -> tuple[list[dict], dict]:
        running_before = self.monitor._engine().get("running")
        n = self.n(20)
        tasks = []
        for i in range(n):
            seed = self.seed(phase, i)
            prompt = f"Write a long, detailed essay about {random.Random(seed).choice(_WORDS)} management. Seed {seed}."
            tasks.append(req.run(messages=[{"role": "user", "content": prompt}], max_tokens=512, seed=seed,
                                 cancel_after_first_token=True))
        rows = await self.gather(tasks, min(n, 8))
        # The assertion: the engine drains the aborted requests and still
        # answers. Wait up to 30 s for num_requests_running to return to where
        # it was, then run one plain completion.
        drained = False
        for _ in range(30):
            running = self.monitor._engine().get("running")
            if running is not None and running_before is not None and running <= running_before:
                drained = True
                break
            await asyncio.sleep(1.0)
        probe = await req.run(messages=[{"role": "user", "content": "Reply with the single word: ok"}], max_tokens=4,
                              seed=self.seed(phase, 999))
        probe["kind"] = "post_cancel_probe"
        probe["correct"] = probe["ok"]
        for r in rows:
            r["correct"] = r["ok"] and r["cancelled"]
        rows.append(probe)
        return rows, {"engine_kept_serving": probe["ok"], "running_drained": drained,
                      "running_before": running_before, "concurrency": min(n, 8)}

    async def json_structured(self, phase: str, req: Request) -> tuple[list[dict], dict]:
        tasks = []
        for i in range(self.n(30)):
            seed = self.seed(phase, i)
            rng = random.Random(seed)
            items = rng.sample(_WORDS, 3)
            prompt = (f"Return a JSON object with a key \"items\": a list of three objects, each with keys "
                      f"\"name\" (string) and \"count\" (integer), for {', '.join(items)}. JSON only.")
            tasks.append(req.run(messages=[{"role": "user", "content": prompt}], max_tokens=160, seed=seed, stream=False,
                                 extra={"response_format": {"type": "json_object"}}, keep_text=True))
        rows = await self.gather(tasks, 4)
        for r in rows:
            if r["ok"]:
                try:
                    json.loads(r.get("_text", ""))
                    r["correct"] = True
                except (json.JSONDecodeError, TypeError):
                    r["correct"] = False
        return rows, {"concurrency": 4}

    async def streaming(self, phase: str, req: Request) -> tuple[list[dict], dict]:
        tasks = []
        for i in range(self.n(20)):
            seed = self.seed(phase, i)
            prompt, _ = _short_prompt(seed)
            tasks.append(req.run(messages=[{"role": "user", "content": prompt + " Use about 150 words."}], max_tokens=200, seed=seed))
        return await self.gather(tasks, 4), {"concurrency": 4}

    # -- driver ---------------------------------------------------------
    async def run_phases(self, phases: list[str]) -> dict[str, dict]:
        limits = httpx.Limits(max_connections=40, max_keepalive_connections=20)
        results: dict[str, dict] = {}
        async with httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(connect=10, read=1800, write=60, pool=60)) as client:
            self.model = self.args.model or (await client.get(f"{self.args.base}/v1/models")).json()["data"][0]["id"]
            print(f"[ab] label={self.args.label} model={self.model} phases={','.join(phases)} out={self.out}", flush=True)
            short = Request(client, self.args.base, self.model, read_timeout=600.0)
            long = Request(client, self.args.base, self.model, read_timeout=1800.0)
            huge = Request(client, self.args.base, self.model, read_timeout=3600.0)
            table = {
                "c1_short": lambda: self.short("c1_short", 40, 1, short),
                "c10_short": lambda: self.short("c10_short", 200, 10, short),
                "c16_short": lambda: self.short("c16_short", 320, 16, short),
                "prefill_32k": lambda: self.prefill("prefill_32k", 32_768, 5, long, client),
                "prefill_128k": lambda: self.prefill("prefill_128k", 131_072, 3, long, client),
                "needle_950k": lambda: self.needle("needle_950k", huge, client),
                "decode_burst": lambda: self.decode_burst("decode_burst", short),
                "mixed": lambda: self.mixed("mixed", long),
                "cancellations": lambda: self.cancellations("cancellations", short, client),
                "json_structured": lambda: self.json_structured("json_structured", short),
                "streaming": lambda: self.streaming("streaming", short),
            }
            for phase in phases:
                results[phase] = await self.with_phase(phase, table[phase])
        return results


# ----------------------------------------------------------------- summary --
def _node_stats(samples: list[dict], node: str) -> dict:
    gpu = [s[node]["gpu"]["util"] for s in samples if s[node]["gpu"]["util"] is not None]
    power = [s[node]["gpu"]["power_w"] for s in samples if s[node]["gpu"]["power_w"] is not None]
    temp = [s[node]["gpu"]["temp_c"] for s in samples if s[node]["gpu"]["temp_c"] is not None]
    cpu: dict[str, list[float]] = {}
    threads: dict[str, list[float]] = {}
    for s in samples:
        for k, v in (s[node].get("cpu_pct") or {}).items():
            cpu.setdefault(k, []).append(v)
        for k, v in (s[node].get("thread_cpu_pct") or {}).items():
            threads.setdefault(k, []).append(v["pct"])
    roce: dict[str, dict[str, float]] = {}
    for s in samples:
        for hca, d in (s[node].get("roce_delta_bytes") or {}).items():
            r = roce.setdefault(hca, {"xmit_gb": 0.0, "rcv_gb": 0.0})
            r["xmit_gb"] += d["xmit"] / 1e9
            r["rcv_gb"] += d["rcv"] / 1e9
    swap_out = sum((s[node].get("pswpout_delta") or 0) for s in samples)
    swap_in = sum((s[node].get("pswpin_delta") or 0) for s in samples)
    xid = [s[node]["xid_since_start"] for s in samples if s[node]["xid_since_start"] is not None]
    starts = {s[node]["container"]["started_at"] for s in samples if s[node]["container"]["started_at"]}
    rc = [s[node]["container"]["restart_count"] for s in samples if s[node]["container"]["restart_count"] is not None]
    faults = sum(1 for s in samples if s[node]["fault_lines_tail"])
    mem = [s[node]["mem_kb"].get("MemAvailable") for s in samples if s[node]["mem_kb"].get("MemAvailable")]
    return {
        "samples": len(samples),
        "gpu_util_mean": round(_mean(gpu), 1) if gpu else None, "gpu_util_max": max(gpu) if gpu else None,
        "gpu_power_mean_w": round(_mean(power), 1) if power else None, "gpu_power_max_w": max(power) if power else None,
        "gpu_temp_max_c": max(temp) if temp else None,
        "cpu_pct": {k: {"mean": round(_mean(v), 1), "max": max(v)} for k, v in sorted(cpu.items())},
        "busy_threads_pct": {k: {"mean": round(_mean(v), 1), "max": max(v)} for k, v in sorted(threads.items(), key=lambda kv: -max(kv[1]))[:12]},
        "swap_out_pages": swap_out, "swap_in_pages": swap_in,
        "roce_gb": {k: {kk: round(vv, 3) for kk, vv in v.items()} for k, v in roce.items()},
        "xid_max": max(xid) if xid else None,
        "restarts": (max(rc) - min(rc)) if rc else None, "distinct_starts": len(starts),
        "samples_with_fault_lines": faults,
        "mem_available_min_gb": round(min(mem) / 2**20, 1) if mem else None,
    }


def summarise(phase: str, rows: list[dict], wall_s: float, samples: list[dict]) -> dict:
    ok = [r for r in rows if r["ok"]]
    cats = {c: 0 for c in ERROR_CATEGORIES}
    for r in rows:
        cats[r.get("error_category", "other") if r.get("error_category") in cats else "other"] += 1
    ttft = [r["ttft_s"] for r in ok if r.get("ttft_s") is not None]
    total = [r["total_s"] for r in ok if r.get("total_s") is not None]
    toks = [r["tok_s"] for r in ok if r.get("tok_s")]
    comp = sum((r.get("completion_tokens") or 0) for r in ok)
    prompt = sum((r.get("prompt_tokens") or 0) for r in ok)
    judged = [r for r in rows if r.get("correct") is not None]
    gaps95 = [r["gap_p95_s"] for r in ok if r.get("gap_p95_s") is not None]
    gaps50 = [r["gap_p50_s"] for r in ok if r.get("gap_p50_s") is not None]
    prefill = [r["prefill_tok_s"] for r in ok if r.get("prefill_tok_s")]
    ctl_states = sorted({(s.get("controller") or {}).get("state") for s in samples if (s.get("controller") or {}).get("state")})
    out = {
        "phase": phase, "requests": len(rows), "ok": len(ok), "failed": len(rows) - len(ok),
        "failures_by_category": {k: v for k, v in cats.items() if k != "none" and v},
        "wall_s": round(wall_s, 2),
        "aggregate_completion_tok_s": round(comp / wall_s, 2) if wall_s > 0 else None,
        "aggregate_prompt_tok_s": round(prompt / wall_s, 1) if wall_s > 0 else None,
        "completion_tokens": comp, "prompt_tokens": prompt,
        "ttft_p50_s": round(_pct(ttft, .5), 4) if ttft else None,
        "ttft_p95_s": round(_pct(ttft, .95), 4) if ttft else None,
        "ttft_max_s": round(max(ttft), 4) if ttft else None,
        "total_p50_s": round(_pct(total, .5), 3) if total else None,
        "total_p95_s": round(_pct(total, .95), 3) if total else None,
        "tok_s_p50": round(_pct(toks, .5), 2) if toks else None,
        "tok_s_p95": round(_pct(toks, .95), 2) if toks else None,
        "prefill_tok_s_p50": round(_pct(prefill, .5), 1) if prefill else None,
        "gap_p50_s_median": round(_pct(gaps50, .5), 4) if gaps50 else None,
        "gap_p95_s_median": round(_pct(gaps95, .5), 4) if gaps95 else None,
        "gap_max_s": round(max(r.get("gap_max_s") or 0 for r in ok), 4) if ok else None,
        "correctness_rate": round(sum(1 for r in judged if r["correct"]) / len(judged), 4) if judged else None,
        "correctness_judged": len(judged),
        "controller_states_seen": ctl_states,
        "nodes": {"head": _node_stats(samples, "head"), "worker": _node_stats(samples, "worker")} if samples else {},
    }
    return out


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.3f}" if abs(v) < 10 else f"{v:.1f}"
    return str(v)


def phase_markdown(s: dict) -> str:
    lines = [f"### {s['phase']}", "",
             "| metric | value |", "|---|---|",
             f"| requests ok / failed | {s['ok']} / {s['failed']} {s['failures_by_category'] or ''} |",
             f"| wall | {_fmt(s['wall_s'])} s |",
             f"| aggregate completion tok/s | {_fmt(s['aggregate_completion_tok_s'])} |",
             f"| aggregate prompt tok/s | {_fmt(s['aggregate_prompt_tok_s'])} |",
             f"| TTFT p50 / p95 / max | {_fmt(s['ttft_p50_s'])} / {_fmt(s['ttft_p95_s'])} / {_fmt(s['ttft_max_s'])} s |",
             f"| total p50 / p95 | {_fmt(s['total_p50_s'])} / {_fmt(s['total_p95_s'])} s |",
             f"| per-request decode tok/s p50 / p95 | {_fmt(s['tok_s_p50'])} / {_fmt(s['tok_s_p95'])} |",
             f"| prefill tok/s p50 | {_fmt(s['prefill_tok_s_p50'])} |",
             f"| inter-token gap p50 / p95 (median over streams) / max | {_fmt(s['gap_p50_s_median'])} / {_fmt(s['gap_p95_s_median'])} / {_fmt(s['gap_max_s'])} s |",
             f"| correctness | {_fmt(s['correctness_rate'])} over {s['correctness_judged']} judged |",
             f"| controller states seen | {', '.join(s['controller_states_seen']) or '—'} |"]
    for node in ("head", "worker"):
        n = (s.get("nodes") or {}).get(node)
        if not n:
            continue
        cpu = ", ".join(f"{k} {v['mean']}/{v['max']}%" for k, v in n["cpu_pct"].items() if v["max"] >= 1.0) or "—"
        roce = ", ".join(f"{k} tx {v['xmit_gb']} / rx {v['rcv_gb']} GB" for k, v in n["roce_gb"].items()) or "—"
        lines.append(f"| {node} GPU util mean/max, power mean/max | {_fmt(n['gpu_util_mean'])} / {_fmt(n['gpu_util_max'])} %, "
                     f"{_fmt(n['gpu_power_mean_w'])} / {_fmt(n['gpu_power_max_w'])} W |")
        lines.append(f"| {node} CPU per process mean/max | {cpu} |")
        lines.append(f"| {node} swap out / in pages | {n['swap_out_pages']} / {n['swap_in_pages']} |")
        lines.append(f"| {node} RoCE moved | {roce} |")
        lines.append(f"| {node} Xid (run) / restarts / fault samples | {_fmt(n['xid_max'])} / {_fmt(n['restarts'])} / {n['samples_with_fault_lines']} |")
    for k in ("engine_kept_serving", "running_drained", "target_tokens", "concurrency", "minutes"):
        if k in s:
            lines.append(f"| {k} | {s[k]} |")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- compare --
COMPARE_METRICS = (
    ("ok", "requests ok", False), ("failed", "requests failed", True),
    ("aggregate_completion_tok_s", "aggregate completion tok/s", False),
    ("aggregate_prompt_tok_s", "aggregate prompt tok/s", False),
    ("ttft_p50_s", "TTFT p50 s", True), ("ttft_p95_s", "TTFT p95 s", True),
    ("total_p50_s", "total p50 s", True), ("total_p95_s", "total p95 s", True),
    ("tok_s_p50", "decode tok/s p50", False), ("prefill_tok_s_p50", "prefill tok/s p50", False),
    ("gap_p95_s_median", "inter-token gap p95 s", True), ("gap_max_s", "inter-token gap max s", True),
    ("correctness_rate", "correctness", False),
)
NODE_METRICS = (
    ("gpu_util_mean", "GPU util mean %"), ("gpu_util_max", "GPU util max %"),
    ("gpu_power_mean_w", "GPU power mean W"), ("swap_out_pages", "swap-out pages"),
    ("xid_max", "Xid (run)"), ("restarts", "restarts"), ("samples_with_fault_lines", "fault samples"),
)


def _delta(a: Any, b: Any, lower_is_better: bool) -> str:
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return "—"
    d = b - a
    pct = f" ({d / a * 100:+.1f} %)" if a else ""
    arrow = ""
    if d != 0:
        better = (d < 0) == lower_is_better
        arrow = " ✓" if better else " ✗"
    return f"{d:+.3f}{pct}{arrow}" if isinstance(d, float) else f"{d:+d}{pct}{arrow}"


def compare(dir_a: Path, dir_b: Path) -> str:
    sa = json.loads((dir_a / "summary.json").read_text())
    sb = json.loads((dir_b / "summary.json").read_text())
    la, lb = sa.get("label", "A"), sb.get("label", "B")
    lines = [f"# A/B comparison — {la} ({dir_a.name}) vs {lb} ({dir_b.name})", "",
             f"- {la}: image `{sa.get('meta', {}).get('head_image', '?')}`, engine argv hash `{sa.get('meta', {}).get('argv_sha256', '?')[:12]}`",
             f"- {lb}: image `{sb.get('meta', {}).get('head_image', '?')}`, engine argv hash `{sb.get('meta', {}).get('argv_sha256', '?')[:12]}`",
             "", "Delta is B − A; ✓ = better, ✗ = worse (lower is better for latency, failures, swap, Xid, restarts).", ""]
    for phase in PHASES:
        pa, pb = sa.get("phases", {}).get(phase), sb.get("phases", {}).get(phase)
        if not pa and not pb:
            continue
        lines += [f"## {phase}", "", f"| metric | {la} | {lb} | delta |", "|---|---|---|---|"]
        for key, name, lower in COMPARE_METRICS:
            va, vb = (pa or {}).get(key), (pb or {}).get(key)
            if va is None and vb is None:
                continue
            lines.append(f"| {name} | {_fmt(va)} | {_fmt(vb)} | {_delta(va, vb, lower)} |")
        for node in ("head", "worker"):
            na, nb = ((pa or {}).get("nodes") or {}).get(node) or {}, ((pb or {}).get("nodes") or {}).get(node) or {}
            for key, name in NODE_METRICS:
                va, vb = na.get(key), nb.get(key)
                if va is None and vb is None:
                    continue
                lines.append(f"| {node} {name} | {_fmt(va)} | {_fmt(vb)} | {_delta(va, vb, key != 'gpu_util_mean' and key != 'gpu_util_max')} |")
            # CPU per process, matched by name (the pid differs between runs;
            # names carry "::" so only the trailing ":pid" is stripped).
            def by_name(stats: dict) -> dict[str, float]:
                return {k.rsplit(":", 1)[0]: v["mean"] for k, v in (stats.get("cpu_pct") or {}).items()}
            ca, cb = by_name(na), by_name(nb)
            for pname in sorted(set(ca) | set(cb)):
                va, vb = ca.get(pname), cb.get(pname)
                if not (va or vb):
                    continue
                lines.append(f"| {node} CPU {pname} mean % | {_fmt(va)} | {_fmt(vb)} | {_delta(va, vb, True)} |")
            for hca in sorted(set((na.get("roce_gb") or {}).keys()) | set((nb.get("roce_gb") or {}).keys())):
                va = (na.get("roce_gb") or {}).get(hca, {}).get("xmit_gb")
                vb = (nb.get("roce_gb") or {}).get(hca, {}).get("xmit_gb")
                lines.append(f"| {node} RoCE {hca} tx GB | {_fmt(va)} | {_fmt(vb)} | {_delta(va, vb, False)} |")
        lines.append("")
    return "\n".join(lines)


# -------------------------------------------------------------------- meta --
def _meta(args: argparse.Namespace) -> dict:
    import hashlib
    head_image = _sh(["docker", "inspect", "-f", "{{.Config.Image}} {{.Image}}", args.head_container]).strip()
    argv = _sh(["docker", "inspect", "-f", "{{json .Config.Cmd}}", args.head_container]).strip()
    env = _sh(["docker", "inspect", "-f", "{{json .Config.Env}}", args.head_container]).strip()
    engine_env = {}
    try:
        for e in json.loads(env or "[]"):
            k, _, v = e.partition("=")
            # Only the engine knobs the candidate record cares about; never
            # the whole environment (it may carry tokens).
            if k.startswith(("VLLM_", "NCCL_", "FLASHINFER_", "CUDA_LAUNCH", "CUTE_DSL", "TORCH_CUDA")) and not k.endswith(("_IP", "_ADDR")):
                engine_env[k] = v
    except json.JSONDecodeError:
        pass
    worker_image = _sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", args.worker, "docker", "inspect", "-f",
                        "'{{.Config.Image}} {{.Image}}'", args.worker_container]).strip().strip("'")
    git_rev = _sh(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"]).strip()
    return {
        "label": args.label, "started_at": time.time(), "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base": args.base, "smoke": args.smoke, "seed": args.seed, "mixed_minutes": args.mixed_minutes,
        "head_image": head_image, "worker_image": worker_image,
        "engine_argv": json.loads(argv) if argv.startswith("[") else argv,
        "argv_sha256": hashlib.sha256(argv.encode()).hexdigest(),
        "engine_env": engine_env, "git_rev": git_rev, "harness": "scripts/cluster-ab.py",
    }


# -------------------------------------------------------------------- main --
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", choices=("A", "B"), help="which candidate this run measures")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--model", default="")
    ap.add_argument("--worker", default=os.environ.get("CLUSTER_WORKER_SSH", "techsphere@10.100.184.2"))
    ap.add_argument("--phase", default="all", help="phase name, comma-separated names, or all")
    ap.add_argument("--out", default="", help="default .runtime/ab/<label>-<utcstamp>/")
    ap.add_argument("--smoke", action="store_true", help="2 requests per phase, needle at 8K, mixed at c=2")
    ap.add_argument("--seed", type=int, default=1000)
    ap.add_argument("--mixed-minutes", type=float, default=10.0)
    ap.add_argument("--needle-tokens", type=int, default=950_000)
    ap.add_argument("--head-container", default="sf-local-ai-vllm-1")
    ap.add_argument("--worker-container", default="sf-local-ai-worker-vllm-worker-1")
    ap.add_argument("--controller", default="http://127.0.0.1:9838", help="engine controller; '' to skip")
    ap.add_argument("--monitor-every", type=float, default=5.0)
    ap.add_argument("--compare", nargs=2, metavar=("DIR_A", "DIR_B"), help="render a side-by-side table and exit")
    args = ap.parse_args()

    if args.compare:
        dir_a, dir_b = Path(args.compare[0]), Path(args.compare[1])
        text = compare(dir_a, dir_b)
        out = dir_b / f"compare-{dir_a.name}-vs-{dir_b.name}.md"
        out.write_text(text, encoding="utf-8")
        print(text)
        print(f"[ab] written {out}")
        return 0
    if not args.label:
        ap.error("--label A|B is required (or --compare)")

    phases = list(PHASES) if args.phase == "all" else [p.strip() for p in args.phase.split(",") if p.strip()]
    unknown = [p for p in phases if p not in PHASES]
    if unknown:
        ap.error(f"unknown phase(s) {unknown}; choose from {', '.join(PHASES)}")

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(args.out) if args.out else AB_DIR / f"{args.label}-{stamp}"
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # .runtime/ can be root-owned after a docker-run promtool/compose
        # render; say so instead of dying in pathlib.
        sys.exit(f"cannot create {out} ({exc.strerror}); pass --out <writable dir>")

    meta = _meta(args)
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    mon_fh = (out / "monitor.jsonl").open("a", encoding="utf-8")
    lock = threading.Lock()

    def sink(row: dict) -> None:
        with lock:
            mon_fh.write(json.dumps(row) + "\n")
            mon_fh.flush()

    monitor = Monitor(worker=args.worker, head_ctr=args.head_container, worker_ctr=args.worker_container, base=args.base,
                      controller=args.controller, every=args.monitor_every, sink=sink, since_epoch=int(time.time()))
    monitor.start()
    run = Run(args, out, monitor)
    started = time.time()
    try:
        results = asyncio.run(run.run_phases(phases))
    finally:
        monitor.stop.set()
        monitor.join(timeout=args.monitor_every + 40)
        mon_fh.close()

    alarms = sorted(set(monitor.alarms))
    summary = {"label": args.label, "meta": meta, "phases": results, "elapsed_s": round(time.time() - started, 1),
               "alarms": alarms, "monitor_samples": len(monitor.samples)}
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    md = [f"# A/B run {args.label} — {stamp}", "",
          f"- image: `{meta['head_image']}` (worker `{meta['worker_image']}`)",
          f"- engine argv sha256 `{meta['argv_sha256'][:12]}`, git `{meta['git_rev']}`, smoke={args.smoke}, seed={args.seed}",
          f"- elapsed {summary['elapsed_s']} s, monitor samples {len(monitor.samples)}",
          f"- alarms: {'; '.join(alarms) if alarms else 'none'}", ""]
    for phase in phases:
        md.append(phase_markdown(results[phase]))
    (out / "SUMMARY.md").write_text("\n".join(md), encoding="utf-8")
    failed = sum(r["failed"] for r in results.values())
    wrong = [p for p, r in results.items() if r["correctness_rate"] is not None and r["correctness_rate"] < 1.0]
    print(f"\n[ab] done: {out}  failed requests={failed}  correctness<1 in {wrong or 'none'}  alarms={alarms or 'none'}")
    return 0 if not failed and not wrong and not alarms else 1


if __name__ == "__main__":
    sys.exit(main())
