#!/usr/bin/env python3
"""Soak the two-node vLLM cluster with a MIXED workload and watch both nodes.

The GDN spec-decode faults of 2026-09-01/02/10 fired only on MIXED batches —
spec-decode sequences and prefills in the same engine step — under an analysis
pipeline running ten concurrent requests. A benchmark of uniform prompts does
not reproduce that shape; this does. Each worker loops over a prompt mix
(mostly short chat turns, some multi-thousand-token documents, a few 32K
pastes) so the scheduler keeps long prefills and many decodes in flight
together, and a monitor thread watches for exactly the signatures the
incident left behind:

  * CUDA faults / dead workers in BOTH engine containers' logs
  * Xid lines in BOTH kernels (journalctl -k; the worker over ssh)
  * RestartCount / StartedAt of BOTH engine containers
  * vllm:generation_tokens_total frozen while requests run (the wedge)
  * the watchdog's own log

Success is defined at the bottom (`verdict`): zero faults, zero Xid, zero
restarts, no frozen counter, every request answered.

    orchestrator/.venv/bin/python scripts/cluster-soak.py \
        --minutes 60 --concurrency 10 --worker techsphere@10.100.184.2

Read-only apart from the requests it sends. Prints a summary and writes a
JSONL of every request plus the monitor samples under .runtime/logs/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("needs httpx: run with orchestrator/.venv/bin/python")

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / ".runtime" / "logs"

FAULT_RE = re.compile(
    r"CUDA error|illegal memory access|misaligned address|AcceleratorError|"
    r"died unexpectedly|EngineDeadError|Worker proc|CUDA_SUCCESS|EngineCore.*(hit|encountered) an exception",
    re.I,
)
XID_RE = re.compile(r"\bXid\b")

_WORDS = (
    "revenue pipeline candidate interview onboarding quarter forecast region "
    "account opportunity stage contract renewal invoice ticket incident release "
    "cluster latency throughput memory kernel driver network fabric".split()
)


def _text(tokens: int, seed: int) -> str:
    rng = random.Random(seed)
    # ~1.3 tokens per word on this tokenizer; aim a little under the target.
    words = max(8, int(tokens / 1.35))
    return " ".join(rng.choice(_WORDS) for _ in range(words))


def _mix_prompt(seed: int) -> tuple[str, int, str]:
    """(prompt, max_tokens, kind) drawn from the mixed workload."""
    rng = random.Random(seed)
    roll = rng.random()
    if roll < 0.60:
        n = rng.randint(150, 1500)
        kind = "short"
        out = rng.randint(64, 384)
        ask = "Answer briefly and concretely."
    elif roll < 0.90:
        n = rng.randint(3000, 9000)
        kind = "medium"
        out = rng.randint(128, 512)
        ask = "Summarise the document above in five bullet points."
    else:
        n = rng.randint(24000, 32000)
        kind = "long"
        out = 128
        ask = "In two sentences, what is the document above about?"
    body = _text(n, seed)
    return f"Document (id {seed}):\n{body}\n\n{ask}", out, kind


async def _one(client: httpx.AsyncClient, base: str, model: str, seed: int, sink) -> dict:
    prompt, max_tokens, kind = _mix_prompt(seed)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": bool(seed % 3 == 0)},
    }
    t0 = time.perf_counter()
    first = None
    chunks = 0
    err = ""
    status = 0
    try:
        async with client.stream("POST", f"{base}/v1/chat/completions", json=payload) as resp:
            status = resp.status_code
            if status != 200:
                err = (await resp.aread())[:200].decode("utf-8", "replace")
            else:
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    if first is None:
                        first = time.perf_counter() - t0
                    if line.strip() == "data: [DONE]":
                        break
                    chunks += 1
    except Exception as exc:  # noqa: BLE001 — recorded, the soak goes on
        err = f"{type(exc).__name__}: {str(exc)[:160]}"
    total = time.perf_counter() - t0
    row = {
        "at": time.time(), "seed": seed, "kind": kind, "status": status, "ttft_s": first,
        "total_s": round(total, 3), "chunks": chunks, "max_tokens": max_tokens, "error": err,
    }
    sink(row)
    return row


def _sh(cmd: list[str], timeout: float = 20.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception as exc:  # noqa: BLE001
        return f"<{type(exc).__name__}>"


def _ssh(worker: str, remote: str, timeout: float = 25.0) -> str:
    return _sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", worker, remote], timeout)


class Monitor(threading.Thread):
    """Samples both nodes every `every` seconds; keeps the alarming lines."""

    def __init__(self, worker: str, head_ctr: str, worker_ctr: str, metrics_url: str, every: float, sink) -> None:
        super().__init__(daemon=True)
        self.worker, self.head_ctr, self.worker_ctr, self.metrics_url = worker, head_ctr, worker_ctr, metrics_url
        self.every, self.sink = every, sink
        self.stop = threading.Event()
        self.alarms: list[str] = []
        self.samples = 0
        self._last_tokens: float | None = None
        self._frozen = 0
        self.baseline = self._sample(first=True)

    def _count(self, text: str, rx: re.Pattern) -> int:
        return sum(1 for line in text.splitlines() if rx.search(line))

    def _sample(self, first: bool = False) -> dict:
        since = "3m" if not first else "1s"
        head_log = _sh(["docker", "logs", "--since", since, self.head_ctr])
        head_log_err = _sh(["sh", "-c", f"docker logs --since {since} {self.head_ctr} 2>&1 >/dev/null"])
        worker_log = self._ssh_log(since)
        head_xid = _sh(["sh", "-c", "journalctl -k --no-pager -o short-iso 2>/dev/null | grep -c Xid"]).strip()
        worker_xid = _ssh(self.worker, "journalctl -k --no-pager -o short-iso 2>/dev/null | grep -c Xid").strip()
        head_state = _sh(["docker", "inspect", "-f", "{{.RestartCount}} {{.State.StartedAt}} {{.State.Health.Status}}", self.head_ctr]).strip()
        worker_state = _ssh(self.worker, f"docker inspect -f '{{{{.RestartCount}}}} {{{{.State.StartedAt}}}} {{{{.State.Health.Status}}}}' {self.worker_ctr}").strip()
        tokens, running = self._metrics()
        mem = _sh(["sh", "-c", "grep -E 'MemAvailable|SwapFree' /proc/meminfo | tr -s ' ' | paste -sd' '"]).strip()
        wmem = _ssh(self.worker, "grep -E 'MemAvailable|SwapFree' /proc/meminfo | tr -s ' ' | paste -sd' '").strip()
        gpu = _sh(["nvidia-smi", "--query-gpu=utilization.gpu,power.draw,temperature.gpu", "--format=csv,noheader"]).strip()
        wgpu = _ssh(self.worker, "nvidia-smi --query-gpu=utilization.gpu,power.draw,temperature.gpu --format=csv,noheader").strip()
        watchdog = _sh(["docker", "logs", "--since", since, "sf-local-ai-vllm-watchdog-1"]).strip().splitlines()[-2:]
        return {
            "at": time.time(),
            "head_faults": self._count(head_log + head_log_err, FAULT_RE),
            "worker_faults": self._count(worker_log, FAULT_RE),
            "head_xid": head_xid, "worker_xid": worker_xid,
            "head_state": head_state, "worker_state": worker_state,
            "generation_tokens_total": tokens, "running": running,
            "head_mem": mem, "worker_mem": wmem, "head_gpu": gpu, "worker_gpu": wgpu,
            "watchdog": watchdog,
        }

    def _ssh_log(self, since: str) -> str:
        return _ssh(self.worker, f"docker logs --since {since} {self.worker_ctr} 2>&1 | grep -iE 'CUDA error|illegal memory|misaligned|AcceleratorError|died unexpectedly|EngineDeadError|Worker proc|CUDA_SUCCESS' | head -20")

    def _metrics(self) -> tuple[float | None, float | None]:
        try:
            text = httpx.get(self.metrics_url, timeout=5.0).text
        except Exception:  # noqa: BLE001
            return None, None
        tokens = running = None
        for line in text.splitlines():
            if line.startswith("vllm:generation_tokens_total"):
                tokens = float(line.rsplit(" ", 1)[1])
            elif line.startswith("vllm:num_requests_running"):
                running = float(line.rsplit(" ", 1)[1])
        return tokens, running

    def run(self) -> None:
        while not self.stop.wait(self.every):
            s = self._sample()
            self.samples += 1
            self.sink({"monitor": s})
            if s["head_faults"] or s["worker_faults"]:
                self.alarms.append(f"CUDA/engine fault lines: head={s['head_faults']} worker={s['worker_faults']}")
            for node in ("head", "worker"):
                if s[f"{node}_xid"] != self.baseline[f"{node}_xid"]:
                    self.alarms.append(f"{node} Xid count changed {self.baseline[f'{node}_xid']} -> {s[f'{node}_xid']}")
                if s[f"{node}_state"].split(" ")[:2] != self.baseline[f"{node}_state"].split(" ")[:2]:
                    self.alarms.append(f"{node} engine container restarted: {self.baseline[f'{node}_state']} -> {s[f'{node}_state']}")
            # The wedge signature: requests running, tokens not moving.
            if s["running"] and s["generation_tokens_total"] is not None:
                if self._last_tokens is not None and s["generation_tokens_total"] == self._last_tokens:
                    self._frozen += 1
                    if self._frozen >= 3:
                        self.alarms.append("generation_tokens_total frozen across 3 samples with requests running")
                else:
                    self._frozen = 0
            self._last_tokens = s["generation_tokens_total"]
            print(f"[monitor] {time.strftime('%H:%M:%S')} tokens={s['generation_tokens_total']} running={s['running']} "
                  f"head={s['head_gpu']} worker={s['worker_gpu']} faults={s['head_faults']}/{s['worker_faults']} "
                  f"xid={s['head_xid']}/{s['worker_xid']} alarms={len(self.alarms)}", flush=True)


async def _soak(args, sink) -> list[dict]:
    rows: list[dict] = []
    deadline = time.monotonic() + args.minutes * 60
    seed = args.seed
    limits = httpx.Limits(max_connections=args.concurrency + 2)
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=1800, write=60, pool=60), limits=limits) as client:
        model = args.model or (await client.get(f"{args.base}/v1/models")).json()["data"][0]["id"]
        print(f"[soak] model={model} concurrency={args.concurrency} minutes={args.minutes}", flush=True)

        async def worker(wid: int) -> None:
            nonlocal seed
            streak = 0
            while time.monotonic() < deadline:
                seed += 1
                row = await _one(client, args.base, model, seed, sink)
                rows.append(row)
                if row["error"]:
                    streak += 1
                    # Say it once per worker, then back off: a dead port must
                    # not be hammered ten times a second (the first run did,
                    # and drowned the monitor in identical lines). The
                    # failure is already recorded; the monitor thread keeps
                    # sampling both nodes; the verdict counts every error.
                    if streak <= 2:
                        print(f"[w{wid}] {row['kind']} seed={row['seed']} ERROR {row['status']} {row['error']}", flush=True)
                    elif streak == 3:
                        print(f"[w{wid}] engine unreachable; backing off (errors still counted)", flush=True)
                    await asyncio.sleep(min(30.0, 2.0 * streak))
                else:
                    if streak >= 3:
                        print(f"[w{wid}] engine back after {streak} failed attempts", flush=True)
                    streak = 0

        await asyncio.gather(*(worker(i) for i in range(args.concurrency)))
    return rows


def verdict(rows: list[dict], monitor: Monitor) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    errors = [r for r in rows if r["error"] or r["status"] != 200]
    if errors:
        reasons.append(f"{len(errors)} of {len(rows)} requests failed")
    if not rows:
        reasons.append("no requests completed")
    reasons.extend(sorted(set(monitor.alarms)))
    return not reasons, reasons


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--model", default="")
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--seed", type=int, default=1000)
    ap.add_argument("--worker", default=os.environ.get("CLUSTER_WORKER_SSH", "techsphere@10.100.184.2"))
    ap.add_argument("--head-container", default="sf-local-ai-vllm-1")
    ap.add_argument("--worker-container", default="sf-local-ai-worker-vllm-worker-1")
    ap.add_argument("--monitor-every", type=float, default=30.0)
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = LOG_DIR / f"cluster-soak-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    fh = out.open("a", encoding="utf-8")
    lock = threading.Lock()

    def sink(row: dict) -> None:
        with lock:
            fh.write(json.dumps(row) + "\n")
            fh.flush()

    monitor = Monitor(args.worker, args.head_container, args.worker_container, f"{args.base}/metrics", args.monitor_every, sink)
    print(f"[soak] baseline: head={monitor.baseline['head_state']} worker={monitor.baseline['worker_state']} "
          f"xid={monitor.baseline['head_xid']}/{monitor.baseline['worker_xid']} -> {out}", flush=True)
    monitor.start()
    started = time.time()
    try:
        rows = asyncio.run(_soak(args, sink))
    finally:
        monitor.stop.set()
        monitor.join(timeout=args.monitor_every + 30)
    elapsed = time.time() - started

    ok_rows = [r for r in rows if r["status"] == 200 and not r["error"]]
    by_kind: dict[str, list[dict]] = {}
    for r in ok_rows:
        by_kind.setdefault(r["kind"], []).append(r)

    def pct(vals: list[float], p: float) -> float:
        if not vals:
            return float("nan")
        vals = sorted(vals)
        return vals[min(len(vals) - 1, int(round(p * (len(vals) - 1))))]

    print("\n=== soak summary ===")
    print(f"duration {elapsed/60:.1f} min, concurrency {args.concurrency}, requests {len(rows)} (ok {len(ok_rows)}), "
          f"monitor samples {monitor.samples}")
    for kind in ("short", "medium", "long"):
        rs = by_kind.get(kind, [])
        ttft = [r["ttft_s"] for r in rs if r["ttft_s"] is not None]
        print(f"  {kind:6s} n={len(rs):4d}  TTFT p50 {pct(ttft, .5):.2f}s p95 {pct(ttft, .95):.2f}s p99 {pct(ttft, .99):.2f}s  "
              f"e2e p50 {pct([r['total_s'] for r in rs], .5):.1f}s p95 {pct([r['total_s'] for r in rs], .95):.1f}s")
    chunks = sum(r["chunks"] for r in ok_rows)
    print(f"  streamed chunks {chunks} (~{chunks/elapsed:.0f}/s aggregate)")
    good, reasons = verdict(rows, monitor)
    print("VERDICT:", "PASS" if good else "FAIL")
    for r in reasons:
        print("  -", r)
    sink({"summary": {"ok": good, "reasons": reasons, "requests": len(rows), "ok_requests": len(ok_rows), "minutes": elapsed / 60}})
    fh.close()
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
