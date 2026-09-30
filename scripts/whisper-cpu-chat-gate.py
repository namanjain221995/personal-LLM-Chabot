#!/usr/bin/env python3
"""Does a busy CPU speech replica slow the chat model? The owner's gate for a CPU whisper copy.

The owner allowed a CPU copy of whisper-large-v3 on the head (2026-09-30) on one condition: chat
decode drops by no more than 5 % while it is busy. This measures exactly that. It streams N tokens
from the main model (one request at a time, thinking off) while the replica under test is idle
("off") and while it decodes clips back to back ("on"), in INTERLEAVED PAIRS whose order alternates.
Everything else the cluster is doing (other tenants, a GPU speech replica, a test run) drifts
during a measurement; pairing puts that drift on both arms, where three medians before, during
and after would read it as the replica's cost. On 2026-09-30 single probes on the shared cluster
ranged from 48 to 104 tok/s, so an unpaired 5 % gate is not resolvable.

A probe counts only when it ran ALONE on the engine: vLLM's generation and prompt token counters
grew by exactly its own tokens and num_requests_running never exceeded 1. Other probes are
recorded and retried.

It starts and stops nothing. Point it at a replica that is already running (a throwaway, or the
production copy before the orchestrator is told about it) and at audio clips to decode.

    python3 scripts/whisper-cpu-chat-gate.py --replica http://127.0.0.1:30208 --clips DIR --pairs 12
    python3 scripts/whisper-cpu-chat-gate.py --probe-only --before 3      # chat baseline, no replica

Exit status: 0 PASS, 1 FAIL (the median paired drop is above --threshold), 2 not measured (too few
clean probes, replica errors, or MemAvailable under --min-available-gib, which also stops the load).
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Iterable, Optional

PROMPT = ("Write a long, detailed essay on the history of the printing press in Europe, "
          "from Gutenberg to the industrial rotary press. Use full paragraphs.")
CLIP_SUFFIXES = (".wav", ".flac")


# ------------------------------------------------------------------ chat probe --

def decode_rate(tokens: int, t_first: Optional[float], t_last: Optional[float]) -> float:
    """Tokens per second between the first and the last streamed token (TTFT excluded)."""
    if tokens < 2 or t_first is None or t_last is None or t_last <= t_first:
        return float("nan")
    return (tokens - 1) / (t_last - t_first)


def read_stream(lines: Iterable[bytes], clock=time.perf_counter) -> dict:
    """Parse an OpenAI-style SSE chat stream: first/last token times, token and chunk counts."""
    t_first = t_last = None
    chunks = 0
    usage = None
    for raw in lines:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        event = json.loads(data)
        if event.get("usage"):
            usage = event["usage"]
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning"):
                now = clock()
                chunks += 1
                if t_first is None:
                    t_first = now
                t_last = now
    tokens = int((usage or {}).get("completion_tokens", chunks))
    prompt_tokens = int((usage or {}).get("prompt_tokens", 0))
    return {"t_first": t_first, "t_last": t_last, "tokens": tokens, "prompt_tokens": prompt_tokens, "chunks": chunks}


class Chat:
    def __init__(self, base: str, model: Optional[str], tokens: int) -> None:
        self.base = base.rstrip("/")
        self.tokens = tokens
        self.model = model or self._first_model()

    def _first_model(self) -> str:
        with urllib.request.urlopen(self.base + "/v1/models", timeout=10) as response:
            return json.load(response)["data"][0]["id"]

    def metrics(self) -> dict:
        with urllib.request.urlopen(self.base + "/metrics", timeout=5) as response:
            text = response.read().decode()

        def total(name: str) -> float:
            values = re.findall(r"^vllm:%s\{[^}]*\} ([0-9.e+]+)$" % name, text, re.M)
            return sum(float(v) for v in values) if values else float("nan")

        return {"running": total("num_requests_running"), "gen": total("generation_tokens_total"),
                "prompt": total("prompt_tokens_total")}

    def probe(self, label: str) -> dict:
        peak = [0.0]
        stop = threading.Event()

        def poll() -> None:
            while not stop.is_set():
                try:
                    peak[0] = max(peak[0], self.metrics()["running"])
                except Exception:
                    pass
                stop.wait(0.25)

        before = self.metrics()
        poller = threading.Thread(target=poll, daemon=True)
        poller.start()
        body = {"model": self.model, "messages": [{"role": "user", "content": PROMPT}],
                "max_tokens": self.tokens, "ignore_eos": True, "temperature": 0, "stream": True,
                "stream_options": {"include_usage": True},
                "chat_template_kwargs": {"enable_thinking": False}}
        request = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=600) as response:
            parsed = read_stream(response)
        stop.set()
        poller.join()
        time.sleep(0.3)  # the counters settle after the stream closes
        after = self.metrics()
        other_gen = after["gen"] - before["gen"] - parsed["tokens"]
        other_prompt = after["prompt"] - before["prompt"] - parsed["prompt_tokens"]
        return {
            "label": label, "ts": time.strftime("%H:%M:%S"), "model": self.model,
            "ttft_ms": round(1000 * (parsed["t_first"] - started), 1) if parsed["t_first"] else None,
            "decode_tps": round(decode_rate(parsed["tokens"], parsed["t_first"], parsed["t_last"]), 2),
            "tokens": parsed["tokens"], "max_running": peak[0],
            "other_gen_tokens": other_gen, "other_prompt_tokens": other_prompt,
            "clean": other_gen == 0 and other_prompt == 0 and peak[0] <= 1,
        }


# ------------------------------------------------------------------ replica load --

class Load:
    """Keeps the replica decoding without a gap while ON: two clients, so one clip always waits
    behind the replica's one-clip lock. OFF lets both finish, so the replica is idle."""

    def __init__(self, replica: str, clips: list[Path], model: str = "openai/whisper-large-v3") -> None:
        self.url = replica.rstrip("/") + "/v1/audio/transcriptions"
        self.health = replica.rstrip("/") + "/health"
        self.clips = clips
        self.model = model
        self.on = threading.Event()
        self.quit = threading.Event()
        self.lock = threading.Lock()
        self.in_flight = 0
        self.done: list[dict] = []
        self.threads = [threading.Thread(target=self._client, args=(i,), daemon=True) for i in range(2)]
        for thread in self.threads:
            thread.start()

    def _post(self, path: Path) -> dict:
        boundary = uuid.uuid4().hex
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{self.model}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n").encode()
        body = head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(self.url, data=body,
                                         headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                response.read()
            ok = True
        except Exception as exc:  # recorded, and the gate refuses to judge on a failing replica
            ok = False
            print(f"load: {path.name}: {exc}", file=sys.stderr, flush=True)
        return {"clip": path.name, "ok": ok, "t0": started, "t1": time.time()}

    def _client(self, offset: int) -> None:
        index = offset
        while not self.quit.is_set():
            if not self.on.wait(0.2):
                continue
            path = self.clips[index % len(self.clips)]
            index += 2
            with self.lock:
                self.in_flight += 1
            result = self._post(path)
            with self.lock:
                self.in_flight -= 1
                self.done.append(result)

    def set(self, on: bool, settle_s: float, idle_timeout_s: float = 900.0) -> None:
        if on:
            self.on.set()
            time.sleep(settle_s)  # the first clip is past its load and encoder when the probe starts
            return
        self.on.clear()
        deadline = time.time() + idle_timeout_s
        while time.time() < deadline:
            with self.lock:
                if self.in_flight == 0:
                    return
            time.sleep(0.2)
        raise RuntimeError("the replica did not go idle")

    def failures(self) -> int:
        with self.lock:
            return sum(1 for r in self.done if not r["ok"])

    def stop(self) -> None:
        self.on.clear()
        self.quit.set()


# ------------------------------------------------------------------ decision --

def paired_drops(pairs: list[tuple[float, float]]) -> list[float]:
    """(off, on) decode rates -> the relative drop of each pair, (off - on) / off (positive = slower)."""
    return [(off - on) / off for off, on in pairs if off > 0]


def bootstrap_ci(values: list[float], stat=statistics.median, n: int = 10000, seed: int = 20260930) -> tuple[float, float]:
    rng = random.Random(seed)
    draws = sorted(stat([rng.choice(values) for _ in values]) for _ in range(n))
    return draws[int(0.025 * n)], draws[int(0.975 * n) - 1]


def verdict(pairs: list[tuple[float, float]], threshold: float, min_pairs: int) -> dict:
    """The gate: PASS when the median paired drop is at most `threshold` (0.05 = 5 %)."""
    drops = paired_drops(pairs)
    if len(drops) < min_pairs:
        return {"verdict": "NOT_MEASURED", "pairs": len(drops), "reason": f"fewer than {min_pairs} clean pairs"}
    median = statistics.median(drops)
    low, high = bootstrap_ci(drops)
    return {
        "verdict": "PASS" if median <= threshold else "FAIL",
        "pairs": len(drops),
        "median_drop_pct": round(100 * median, 2),
        "mean_drop_pct": round(100 * statistics.fmean(drops), 2),
        "ci95_median_drop_pct": [round(100 * low, 2), round(100 * high, 2)],
        "threshold_pct": round(100 * threshold, 2),
        # Whether the data rule out a drop larger than the threshold, not just fail to show one.
        "larger_drop_ruled_out": high <= threshold,
    }


def mem_available_gib(path: str = "/proc/meminfo") -> float:
    for line in Path(path).read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / (1024 * 1024)
    raise RuntimeError("no MemAvailable")


# ------------------------------------------------------------------ main --

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--chat", default="http://127.0.0.1:8000", help="the main model's vLLM API")
    parser.add_argument("--model", default=None, help="served model name (default: the first one listed)")
    parser.add_argument("--tokens", type=int, default=300)
    parser.add_argument("--replica", help="the CPU replica under test, e.g. http://127.0.0.1:30208")
    parser.add_argument("--clips", type=Path, help="directory of .wav/.flac clips to decode")
    parser.add_argument("--pairs", type=int, default=12)
    parser.add_argument("--min-pairs", type=int, default=8)
    parser.add_argument("--before", type=int, default=3, help="idle probes before the pairs")
    parser.add_argument("--after", type=int, default=3, help="idle probes after the pairs")
    parser.add_argument("--threshold", type=float, default=5.0, help="allowed median drop, percent")
    parser.add_argument("--settle", type=float, default=4.0, help="seconds of decoding before an ON probe")
    parser.add_argument("--retries", type=int, default=4, help="unclean probes allowed per slot")
    parser.add_argument("--min-available-gib", type=float, default=20.0)
    parser.add_argument("--meminfo", default="/proc/meminfo")
    parser.add_argument("--out", type=Path, help="append every probe and the verdict here (jsonl)")
    parser.add_argument("--probe-only", action="store_true", help="idle probes only, no replica")
    args = parser.parse_args(argv)
    if not args.probe_only and (not args.replica or not args.clips):
        parser.error("--replica and --clips are required unless --probe-only")

    out = args.out.open("a") if args.out else None

    def emit(row: dict) -> None:
        line = json.dumps(row)
        print(line, flush=True)
        if out:
            out.write(line + "\n")
            out.flush()

    def guard() -> None:
        available = mem_available_gib(args.meminfo)
        if available < args.min_available_gib:
            raise MemoryError(f"MemAvailable {available:.1f} GiB is under {args.min_available_gib} GiB")

    chat = Chat(args.chat, args.model, args.tokens)

    def clean_probe(label: str) -> Optional[dict]:
        for _ in range(1 + args.retries):
            guard()
            row = chat.probe(label)
            emit(row)
            if row["clean"]:
                return row
            time.sleep(1.0)
        return None

    def summary(label: str, rows: list[dict]) -> dict:
        rates = [r["decode_tps"] for r in rows]
        ttfts = [r["ttft_ms"] for r in rows if r["ttft_ms"] is not None]
        return {"label": label, "summary": True, "n": len(rows),
                "median_decode_tps": round(statistics.median(rates), 2) if rates else None,
                "median_ttft_ms": round(statistics.median(ttfts), 1) if ttfts else None,
                "decode_tps": rates}

    load = None
    try:
        guard()
        before = [r for r in (clean_probe("before") for _ in range(args.before)) if r]
        emit(summary("before", before))
        if args.probe_only:
            return 0 if before else 2
        clips = sorted(p for p in args.clips.rglob("*") if p.suffix.lower() in CLIP_SUFFIXES)
        if not clips:
            raise RuntimeError(f"no clips under {args.clips}")
        load = Load(args.replica, clips)
        pairs: list[tuple[float, float]] = []
        on_rows: list[dict] = []
        off_rows: list[dict] = []
        for index in range(args.pairs):
            order = ("off", "on") if index % 2 == 0 else ("on", "off")
            got: dict[str, dict] = {}
            for arm in order:
                load.set(arm == "on", args.settle)
                row = clean_probe(f"pair{index}-{arm}")
                if row:
                    got[arm] = row
            load.set(False, 0)
            if len(got) == 2:
                pairs.append((got["off"]["decode_tps"], got["on"]["decode_tps"]))
                off_rows.append(got["off"])
                on_rows.append(got["on"])
        after = [r for r in (clean_probe("after") for _ in range(args.after)) if r]
        emit(summary("off", off_rows))
        emit(summary("on", on_rows))
        emit(summary("after", after))
        decided = verdict(pairs, args.threshold / 100.0, args.min_pairs)
        failures = load.failures()
        decided.update({"replica_clips_ok": sum(1 for r in load.done if r["ok"]), "replica_clip_failures": failures})
        if failures:
            decided.update({"verdict": "NOT_MEASURED", "reason": f"{failures} replica requests failed"})
        emit(dict(decided, summary=True, label="verdict", model=chat.model))
        return {"PASS": 0, "FAIL": 1}.get(decided["verdict"], 2)
    except MemoryError as exc:
        emit({"label": "abort", "reason": str(exc)})
        return 2
    finally:
        if load:
            load.stop()
        if out:
            out.close()


if __name__ == "__main__":
    sys.exit(main())
