"""Load bench for the admission lanes, against a STUB engine only (2026-09-13).

WHAT THIS IS. An in-process discrete simulation that drives the REAL
`app.admission.run` — the NORMAL, LONG and LONG_OUTPUT lanes, the KV ledger,
the chat-first weighted order — with thousands of chat and /v1 requests
against a stub engine, in VIRTUAL time: the event loop's clock jumps to the
next timer instead of sleeping, so hours of 1M-token answers take seconds of
CPU. It exists because the capacity decision of 2026-09-13 forbids load
against the production engine (a GDN fault costs ~3 minutes of downtime),
and a lane change still has to be shown to hold before it ships.

WHAT IT DOES NOT TOUCH. No production URL is ever read: the engine is an
object in this process, the KV pool read (`kv_budget._fetch_text`) is answered
from the stub, the controller sample (`engine_state.engine_load`) comes from
the stub, and HTTP(S)_PROXY is forced to a dead 127.0.0.1:9 for the run so a
stray client call dies instead of reaching anything. `--serve-stub` exposes
the same stub over HTTP for a PRIVATE orchestrator, on loopback only, and
refuses the production ports.

THE STUB ENGINE (fluid model, exact event times).
- Decode step time t(n) = 6.68 + 2.843·n ms for n decoding sequences: the
  two-point fit to 105 tok/s at one stream and 306.7 tok/s at c=16 measured
  on the real engine. Above n=16 it is an EXTRAPOLATION, and every report says
  so.
- Prefill s(p) = 0.05 + 94.6·P + 786.9·P² seconds for P = p / 1e6 tokens: the
  two-point fit to the A/B run of 2026-09-12 (128K in ~25 s, the 950K needle
  in 803.7 s) plus a 50 ms floor. Prefill does not slow the decoders here (a
  limitation: the real engine mixes prefill chunks into decode steps).
- KV: 799 usable blocks of 2096 tokens, 3 fixed GDN-state blocks per
  sequence plus ceil(tokens / 2096), allocated as tokens grow. When the
  blocks run out the most recently scheduled sequence is preempted (FCFS
  `running.pop()`, vllm/v1/core/sched/scheduler.py:776-778 on the pinned
  build) and recomputes its whole context on re-admission. Preemptions are
  counted; max_num_seqs is the build default 256.
- `--token-scale` (default 0.001): the stream emits one chunk per 1/scale
  real tokens (a 1M answer is 1,000 chunks). Admission always sees the
  UNSCALED prompt_tokens and max_tokens.

WHAT TTFT MEANS HERE. Arrival at `admission.run` to the first chunk: the
admission wait plus the stub prefill. The orchestrator's own pre-pass (14 d
chat p95 ≥ 30 s in production) is not modelled; this bench measures what the
lanes add or remove.

POPULATIONS (Poisson each; quantiles are the 14-day engine histograms with
canaries removed, log-linear between them).
- chat: prompt p50 1.8K / p90 7.8K / p95 9.3K / p99 32K; output p50 130 /
  p95 1.5K / p99 2K; max_tokens 65,536 (what every Smart turn is sent).
- v1 short: the same prompt; max_tokens from {1024, 2048, 4096, 8192}; actual
  output = max × U(0.05, 1).
- v1 long: prompt U(2K, 8K); max_tokens from {100K, 400K, 1M}; actual
  output = max × U(0.05, 1) (capped at the window).
- chat LONG: a 200K-950K document, max_tokens 65,536.

    python tools/api_load_bench.py --scenario mixed long_burst v1_flood two_1m chat_doc_behind_1m
    python tools/api_load_bench.py --scenario mixed --assert-no-preemption --json-out /tmp/b.json
    python tools/api_load_bench.py --serve-stub 127.0.0.1:29100
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import logging
import math
import os
import random
import sys
import time
import types
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Deque, Dict, Iterator, List, Optional, Sequence, Tuple
from unittest import mock

_ORCH = Path(__file__).resolve().parents[1]
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))

DEAD_PROXY = "http://127.0.0.1:9"

#: Ports the stub server must never take: the engines, the orchestrator,
#: Prometheus, the controller and the sidecars of the production host.
REFUSED_PORTS = frozenset({8000, 8002, 8003, 8004, 8005, 8080, 9090, 9838, 30002, 30003, 30004, 30005, 30006})

STUB_URL = "http://stub-engine.invalid/v1"

ENGINE_CURVE_LABEL = ("t(n) = 6.68 + 2.843*n ms: two-point fit to 105 tok/s at 1 stream and 306.7 tok/s "
                      "at c=16; above n=16 an EXTRAPOLATION")


# ---------------------------------------------------------------------------
# Virtual time
# ---------------------------------------------------------------------------


class VirtualClock:
    """The loop's clock jumps to the next timer instead of sleeping."""

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        # Every read moves the clock by 1 ns, as a real clock moves between two
        # reads. Without it a waiter left with less than the loop's 1e-9 s clock
        # resolution of bound fires its timer without the clock moving, and with
        # other handles always ready the loop spins at one instant forever (seen
        # at t = 1040.6 s of v1_flood, 400 waiters).
        self.now += 1e-9
        return self.now

    @contextlib.contextmanager
    def patch_loop(self, loop: asyncio.AbstractEventLoop) -> Iterator[None]:
        selector = loop._selector  # type: ignore[attr-defined]
        real_select = selector.select

        def select(timeout=None):
            if timeout is not None and timeout > 0:
                # At least 1 us: at t ~ 1000 s a timeout below ~1e-13 s does not
                # change a float `now`, and a waiter with that much bound left
                # would then spin forever without the clock moving (seen at
                # t = 1100 s of v1_flood before this floor).
                self.now += max(timeout, 1e-6)
            elif timeout is None:
                # Nothing ready and nothing scheduled: advance a little so a
                # bug shows up as a runaway clock, not a hung process.
                self.now += 1.0
            return real_select(0)

        with mock.patch.object(selector, "select", select), mock.patch.object(loop, "time", self.time):
            yield


# ---------------------------------------------------------------------------
# The stub engine
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class Seq:
    rid: int
    prompt: int
    target: int
    generated: float = 0.0
    state: str = "new"  # new | queued | prefill | decode | done
    prefill_end: float = 0.0
    first_token_emitted: bool = False
    chunks_emitted: int = 0
    aborted: bool = False
    preempted: int = 0
    progress: Optional[asyncio.Event] = None


class StubEngine:
    def __init__(
        self,
        *,
        now: Callable[[], float],
        blocks: int = 799,
        block_size: int = 2096,
        fixed_blocks: int = 3,
        base_ms: float = 6.68,
        per_seq_ms: float = 2.843,
        max_num_seqs: int = 256,
        token_scale: float = 0.001,
        pool_tokens: int = 1_663_201,
    ) -> None:
        self.now = now
        self.blocks = blocks
        self.block_size = block_size
        self.fixed = fixed_blocks
        self.base_ms = base_ms
        self.per_seq_ms = per_seq_ms
        self.max_num_seqs = max_num_seqs
        self.chunk_tokens = max(1.0, 1.0 / max(1e-9, token_scale))
        self.pool_tokens = pool_tokens
        self.running: List[Seq] = []
        self.waiting: Deque[Seq] = deque()
        self.preemptions = 0
        self.max_running = 0
        self.peak_blocks = 0
        self.generated_total = 0.0
        self._last = now()
        self._wake: Optional[asyncio.Event] = None
        self._stopped = False

    # -- the model ----------------------------------------------------------
    def step_s(self, n: int) -> float:
        return (self.base_ms + self.per_seq_ms * max(1, n)) / 1000.0

    @staticmethod
    def prefill_s(tokens: int) -> float:
        p = max(0, tokens) / 1e6
        return 0.05 + 94.6 * p + 786.9 * p * p

    def seq_blocks(self, s: Seq) -> int:
        return self.fixed + math.ceil((s.prompt + int(s.generated)) / self.block_size)

    def used_blocks(self) -> int:
        return sum(self.seq_blocks(s) for s in self.running)

    def sample(self) -> dict:
        return {"requests_running": float(len(self.running)), "requests_waiting": float(len(self.waiting)),
                "age_s": 0.0, "kv_cache_usage": self.used_blocks() / float(self.blocks)}

    def metrics_text(self) -> str:
        return "\n".join([
            "# TYPE vllm:cache_config_info gauge",
            f'vllm:cache_config_info{{block_size="{self.block_size}",cache_dtype="fp8",enable_prefix_caching="False",'
            f'engine="0",kv_cache_size_tokens="{self.pool_tokens}",num_gpu_blocks="{self.blocks + 1}"}} 1.0',
            f'vllm:num_requests_running{{engine="0",model_name="stub"}} {len(self.running)}',
            f'vllm:num_requests_waiting{{engine="0",model_name="stub"}} {len(self.waiting)}',
            f'vllm:kv_cache_usage_perc{{engine="0",model_name="stub"}} {self.used_blocks() / float(self.blocks)}',
            f'vllm:num_preemptions_total{{engine="0",model_name="stub"}} {self.preemptions}',
            "",
        ])

    # -- requests -----------------------------------------------------------
    def _event(self) -> asyncio.Event:
        if self._wake is None:
            self._wake = asyncio.Event()
        return self._wake

    def submit(self, rid: int, prompt: int, target: int) -> Seq:
        s = Seq(rid=rid, prompt=max(1, int(prompt)), target=max(1, int(target)), progress=asyncio.Event())
        s.state = "queued"
        self.waiting.append(s)
        self._event().set()
        return s

    def abort(self, s: Seq) -> None:
        if s.state == "done":
            return
        s.state = "done"
        s.aborted = True
        if s in self.running:
            self.running.remove(s)
        else:
            with contextlib.suppress(ValueError):
                self.waiting.remove(s)
        assert s.progress is not None
        s.progress.set()
        self._event().set()

    def stop(self) -> None:
        self._stopped = True
        self._event().set()

    # -- the scheduler ------------------------------------------------------
    def _advance(self, now: float) -> None:
        dt = now - self._last
        self._last = now
        if dt <= 0:
            return
        decs = [s for s in self.running if s.state == "decode"]
        if not decs:
            return
        rate = 1.0 / self.step_s(len(decs))
        for s in decs:
            before = s.generated
            s.generated = min(float(s.target), s.generated + dt * rate)
            self.generated_total += s.generated - before
            chunks = int(s.generated // self.chunk_tokens)
            if chunks > s.chunks_emitted:
                s.chunks_emitted = chunks
                s.progress.set()  # type: ignore[union-attr]

    def _transitions(self, now: float) -> None:
        for s in list(self.running):
            if s.state == "prefill" and s.prefill_end <= now + 1e-9:
                s.state = "decode"
                if not s.first_token_emitted:
                    # The prefill step samples the first token, as vLLM's does.
                    s.first_token_emitted = True
                    s.generated = max(s.generated, 1.0)
                    self.generated_total += 1.0
                    s.chunks_emitted = int(s.generated // self.chunk_tokens)
                s.progress.set()  # type: ignore[union-attr]
            if s.state == "decode" and s.generated >= s.target - 1e-6:
                s.generated = float(s.target)
                s.state = "done"
                self.running.remove(s)
                s.progress.set()  # type: ignore[union-attr]

    def _preempt(self) -> None:
        while len(self.running) > 1 and self.used_blocks() > self.blocks:
            victim = self.running.pop()  # FCFS: the most recently scheduled
            victim.state = "queued"
            victim.preempted += 1
            self.preemptions += 1
            self.waiting.appendleft(victim)

    def _schedule(self, now: float) -> None:
        used = self.used_blocks()
        while self.waiting and len(self.running) < self.max_num_seqs:
            s = self.waiting[0]
            need = self.seq_blocks(s)
            if self.running and used + need > self.blocks:
                break
            self.waiting.popleft()
            s.state = "prefill"
            s.prefill_end = now + self.prefill_s(s.prompt + int(s.generated))
            self.running.append(s)
            used += need
        self.max_running = max(self.max_running, len(self.running))
        self.peak_blocks = max(self.peak_blocks, used)

    def _next_dt(self, now: float) -> Optional[float]:
        cands: List[float] = []
        decs = [s for s in self.running if s.state == "decode"]
        rate = 1.0 / self.step_s(len(decs)) if decs else 0.0
        for s in self.running:
            if s.state == "prefill":
                cands.append(s.prefill_end - now)
        for s in decs:
            cands.append((s.target - s.generated) / rate)
            nxt_chunk = (s.chunks_emitted + 1) * self.chunk_tokens
            if nxt_chunk < s.target:
                cands.append((nxt_chunk - s.generated) / rate)
            tokens = s.prompt + s.generated
            nxt_block = (math.floor(tokens / self.block_size) + 1) * self.block_size
            cands.append((nxt_block - tokens) / rate)
        if not cands:
            return None
        return max(1e-6, min(cands) + 1e-7)

    async def run(self) -> None:
        wake = self._event()
        while not self._stopped:
            now = self.now()
            self._advance(now)
            self._transitions(now)
            self._preempt()
            self._schedule(now)
            dt = self._next_dt(now)
            wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=dt if dt is not None else 3600.0)


class StubStream:
    """What `op()` returns: iterates chunks as the engine produces them and
    aborts the sequence on close, as a closed HTTP stream does in vLLM."""

    def __init__(self, engine: StubEngine, seq: Seq) -> None:
        self.engine = engine
        self.seq = seq
        self.first_sent = False
        self.consumed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        s = self.seq
        while True:
            if s.aborted:
                raise StopAsyncIteration
            if not self.first_sent and s.first_token_emitted:
                self.first_sent = True
                self.consumed = s.chunks_emitted if s.chunks_emitted <= 1 else 1
                return "first"
            if self.first_sent and s.chunks_emitted > self.consumed:
                self.consumed += 1
                return "chunk"
            if s.state == "done":
                raise StopAsyncIteration
            s.progress.clear()  # type: ignore[union-attr]
            await s.progress.wait()  # type: ignore[union-attr]

    async def close(self) -> None:
        self.engine.abort(self.seq)


# ---------------------------------------------------------------------------
# Populations and scenarios
# ---------------------------------------------------------------------------

CHAT_PROMPT_Q = ((0.0, 20), (0.5, 1_800), (0.9, 7_800), (0.95, 9_300), (0.99, 32_000), (1.0, 48_000))
CHAT_OUTPUT_Q = ((0.0, 8), (0.5, 130), (0.95, 1_500), (0.99, 2_000), (1.0, 7_600))


def quantile_sample(points: Sequence[Tuple[float, int]], rng: random.Random) -> int:
    u = rng.random()
    for (q0, v0), (q1, v1) in zip(points, points[1:]):
        if u <= q1:
            f = (u - q0) / (q1 - q0) if q1 > q0 else 0.0
            return int(round(math.exp(math.log(v0) + f * (math.log(v1) - math.log(v0)))))
    return int(points[-1][1])


@dataclass(eq=False)
class Req:
    rid: int
    pop: str
    origin: str
    arrival: float
    prompt: int
    max_tokens: int
    output: int
    admitted_at: Optional[float] = None
    first_at: Optional[float] = None
    done_at: Optional[float] = None
    rejected: Optional[Tuple[str, str]] = None
    rejected_at: Optional[float] = None
    lane: Optional[str] = None


WINDOW = 1_000_000


def _poisson(rng: random.Random, rate: float, start: float, end: float) -> Iterator[float]:
    if rate <= 0:
        return
    t = start
    while True:
        t += rng.expovariate(rate)
        if t >= end:
            return
        yield t


class Builder:
    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.reqs: List[Req] = []

    def rng(self, name: str) -> random.Random:
        # One stream per population: removing a population never moves another's arrivals.
        return random.Random(f"{self.seed}:{name}")

    def add(self, pop: str, origin: str, arrival: float, prompt: int, max_tokens: int, output: int) -> None:
        output = max(1, min(int(output), WINDOW - int(prompt)))
        self.reqs.append(Req(len(self.reqs), pop, origin, arrival, int(prompt), int(max_tokens), output))

    def chat(self, rate: float, start: float, end: float) -> None:
        rng = self.rng("chat")
        for t in _poisson(rng, rate, start, end):
            self.add("chat", "chat", t, quantile_sample(CHAT_PROMPT_Q, rng), 65_536, quantile_sample(CHAT_OUTPUT_Q, rng))

    def v1_short(self, rate: float, start: float, end: float) -> None:
        rng = self.rng("v1_short")
        for t in _poisson(rng, rate, start, end):
            mx = rng.choice((1024, 2048, 4096, 8192))
            self.add("v1_short", "v1", t, quantile_sample(CHAT_PROMPT_Q, rng), mx, mx * rng.uniform(0.05, 1.0))

    def v1_long_at(self, t: float, max_tokens: int, rng: random.Random, *, full: bool = False) -> None:
        prompt = rng.randint(2_000, 8_000)
        out = max_tokens if full else max_tokens * rng.uniform(0.05, 1.0)
        self.add("v1_long", "v1", t, prompt, max_tokens, out)

    def v1_long(self, rate: float, start: float, end: float) -> None:
        rng = self.rng("v1_long")
        for t in _poisson(rng, rate, start, end):
            self.v1_long_at(t, rng.choice((100_000, 400_000, 1_000_000)), rng)

    def chat_long_at(self, t: float, prompt: int, rng: random.Random) -> None:
        self.add("chat_long", "chat", t, prompt, 65_536, quantile_sample(CHAT_OUTPUT_Q, rng))

    def chat_long(self, rate: float, start: float, end: float) -> None:
        rng = self.rng("chat_long")
        for t in _poisson(rng, rate, start, end):
            self.chat_long_at(t, rng.randint(200_000, 950_000), rng)


@dataclass
class Scenario:
    name: str
    about: str
    duration: float
    build: Callable[[Builder, bool], None]  # (builder, with_the_load_under_test)
    compare_baseline: bool = False
    drain_s: float = 1800.0


def _scenarios(chat_rate: Optional[float], duration: Optional[float]) -> Dict[str, Scenario]:
    def d(default: float) -> float:
        return float(duration) if duration else default

    def cr(default: float) -> float:
        return float(chat_rate) if chat_rate is not None else default

    def mixed(b: Builder, with_long: bool) -> None:
        end = d(3600.0)
        b.chat(cr(0.1), 0.0, end)
        b.v1_short(0.05, 0.0, end)
        b.chat_long(1 / 3600.0, 0.0, end)
        if with_long:
            b.v1_long(4 / 3600.0, 0.0, end)

    def long_burst(b: Builder, with_burst: bool) -> None:
        end = d(3600.0)
        b.chat(cr(0.15), 0.0, end)
        b.v1_short(0.05, 0.0, end)
        if with_burst:
            rng = b.rng("burst")
            # Two 400K answers first: both LONG_OUTPUT seats decode at once, the worst
            # case for chat (two 1M answers never fit together, so they would run one).
            for i, mx in enumerate((400_000, 400_000, 100_000, 100_000, 1_000_000, 1_000_000)):
                b.v1_long_at(600.0 + 2.0 * i, mx, rng)

    def v1_flood(b: Builder, _with: bool) -> None:
        end = d(1800.0)
        b.v1_short(2.0, 0.0, end)
        b.chat(cr(0.1), 0.0, end)

    def two_1m(b: Builder, _with: bool) -> None:
        rng = b.rng("two_1m")
        b.v1_long_at(10.0, 1_000_000, rng, full=True)
        b.v1_long_at(11.0, 1_000_000, rng, full=True)

    def chat_doc_behind_1m(b: Builder, _with: bool) -> None:
        rng = b.rng("doc")
        b.v1_long_at(10.0, 1_000_000, rng, full=True)
        b.chat_long_at(60.0, 950_000, rng)
        b.chat_long_at(900.0, 200_000, rng)
        b.chat(cr(0.05), 0.0, d(1800.0))

    # --- The adversarial review of 2026-09-13, one scenario per finding. ---

    def chat_vs_v1_flood(b: Builder, with_flood: bool) -> None:
        # Finding 1: a grant-count weight gave /v1 ~70 % of NORMAL seat time and
        # refused chat turns the engine served alone.
        end = d(1800.0)
        b.chat(cr(0.5), 0.0, end)
        if with_flood:
            b.v1_short(2.0, 0.0, end)

    def v1_burst_then_chat(b: Builder, _with: bool) -> None:
        # Finding 6: 400 /v1 answers of 4-8K tokens at once, chat behind them.
        rng = b.rng("v1_burst")
        for i in range(400):
            mx = rng.choice((4096, 8192))
            b.add("v1_short", "v1", i * 0.01, 2_000, mx, mx)
        b.chat(cr(0.1), 1.0, d(1200.0))

    def v1_doc_loop(b: Builder, with_docs: bool) -> None:
        # Finding 2: one /v1 client, a 950K prompt with a 2K answer every 1,000 s.
        end = d(14_400.0)
        b.chat(cr(0.1), 0.0, end)
        if with_docs:
            t = 30.0
            while t < end:
                b.add("v1_doc", "v1", t, 950_000, 2_000, 2_000)
                t += 1000.0

    def chat_docs_back_to_back_1m(b: Builder, _with: bool) -> None:
        # Finding 3: chat documents faster than the one LONG seat serves them,
        # and a 1M /v1 request arriving behind the first of them.
        rng = b.rng("back_to_back")
        t = 0.0
        while t < d(1800.0):
            b.chat_long_at(t, 200_000, rng)
            t += 45.0
        b.v1_long_at(100.0, 1_000_000, rng)

    def v1_drain_blocks_chat_docs(b: Builder, _with: bool) -> None:
        # Finding 4: a 400K /v1 job, a 1M /v1 request every 300 s (never fits
        # beside it), a 200K chat document every 120 s (always fits beside it).
        rng = b.rng("drain")
        end = d(3600.0)
        b.v1_long_at(5.0, 400_000, rng, full=True)
        t = 20.0
        while t < end:
            b.v1_long_at(t, 1_000_000, rng, full=True)
            t += 300.0
        t = 30.0
        while t < end:
            b.chat_long_at(t, 200_000, rng)
            t += 120.0

    def one_background_1m(b: Builder, _with: bool) -> None:
        # Finding 4, background form (run with a 3,600 s long-output wait).
        rng = b.rng("background")
        end = d(3600.0)
        b.v1_long_at(5.0, 400_000, rng, full=True)
        b.v1_long_at(20.0, 1_000_000, rng, full=True)
        t = 30.0
        while t < end:
            b.chat_long_at(t, 200_000, rng)
            t += 120.0

    def chat_long_answer_at(b: Builder, t: float, rng: random.Random) -> None:
        # A chat long answer (Artifact Studio, Deep Research): max_tokens above
        # 65,536 takes LONG_OUTPUT; ~100 blocks projected, a few K actual.
        b.add("chat_long_answer", "chat", t, rng.randint(2_000, 8_000), 200_000, rng.randint(2_000, 20_000))

    def v1_drain_blocks_chat_long_answers(b: Builder, _with: bool) -> None:
        # Finding 4 with chat LONG_OUTPUT work in place of documents: documents
        # cannot start beside a running long answer at all (LONG BESIDE LONG
        # ANSWERS), so this is the form in which the KV order still decides.
        rng = b.rng("drain_answers")
        end = d(3600.0)
        b.v1_long_at(5.0, 400_000, rng, full=True)
        t = 20.0
        while t < end:
            b.v1_long_at(t, 1_000_000, rng, full=True)
            t += 300.0
        t = 30.0
        while t < end:
            chat_long_answer_at(b, t, rng)
            t += 120.0

    def one_background_1m_chat_long_answers(b: Builder, _with: bool) -> None:
        rng = b.rng("background_answers")
        end = d(3600.0)
        b.v1_long_at(5.0, 400_000, rng, full=True)
        b.v1_long_at(20.0, 1_000_000, rng, full=True)
        t = 30.0
        while t < end:
            chat_long_answer_at(b, t, rng)
            t += 120.0

    def doomed_chat_doc_blocks_v1(b: Builder, with_docs: bool) -> None:
        # Finding 5: 950K chat documents that can never fit beside a 400K job,
        # and small /v1 long answers that always do.
        rng = b.rng("doomed")
        end = d(3600.0)
        b.v1_long_at(5.0, 400_000, rng, full=True)
        t = 30.0
        while t < end:
            b.add("v1_small_long", "v1", t, 5_000, 16_000, 16_000)
            t += 400.0
        if with_docs:
            t = 60.0
            while t < end:
                b.chat_long_at(t, 950_000, rng)
                t += 600.0

    def v1_big_prompts_beside_1m(b: Builder, _with: bool) -> None:
        # Finding 15: /v1 NORMAL requests with prompts just under the LONG
        # threshold beside a 1M answer that grows to 481 blocks.
        rng = b.rng("big_prompts")
        end = d(10_800.0)
        b.v1_long_at(5.0, 1_000_000, rng, full=True)
        # From 2 h in, when the answer holds ~350 blocks and grows to 481.
        for t in _poisson(rng, 0.1, 7200.0, end):
            b.add("v1_big_prompt", "v1", t, rng.randint(100_000, 131_000), 8_192, rng.randint(2_000, 8_192))
        b.chat(cr(0.05), 7200.0, end)

    return {
        "mixed": Scenario("mixed", "chat + v1 short + chat LONG, with and without v1 long-output (Poisson 4/h)",
                          d(3600.0), mixed, compare_baseline=True),
        "long_burst": Scenario("long_burst", "chat + v1 short, with and without a burst of 6 long-output /v1 at t=600 s",
                               d(3600.0), long_burst, compare_baseline=True),
        "v1_flood": Scenario("v1_flood", "v1 short at 2/s (NORMAL saturated) beside chat", d(1800.0), v1_flood),
        "two_1m": Scenario("two_1m", "two 1M-output /v1 requests a second apart", 60.0, two_1m, drain_s=3600.0),
        "chat_doc_behind_1m": Scenario("chat_doc_behind_1m", "chat documents (950K, 200K) behind a running 1M job",
                                       d(1800.0), chat_doc_behind_1m),
        "chat_vs_v1_flood": Scenario("chat_vs_v1_flood", "chat (0.5/s) with and without a /v1 short flood at 2/s",
                                     d(1800.0), chat_vs_v1_flood, compare_baseline=True, drain_s=1200.0),
        "v1_burst_then_chat": Scenario("v1_burst_then_chat", "400 /v1 answers of 4-8K at t=0, chat at 0.1/s",
                                       d(1200.0), v1_burst_then_chat, drain_s=1200.0),
        "v1_doc_loop": Scenario("v1_doc_loop", "chat 0.1/s with and without one /v1 client sending a 950K prompt "
                                "every 1,000 s", d(14_400.0), v1_doc_loop, compare_baseline=True, drain_s=1800.0),
        "chat_docs_back_to_back_1m": Scenario("chat_docs_back_to_back_1m", "200K chat documents every 45 s; one 1M "
                                              "/v1 request at t=100 s", d(1800.0), chat_docs_back_to_back_1m,
                                              drain_s=600.0),
        "v1_drain_blocks_chat_docs": Scenario("v1_drain_blocks_chat_docs", "400K /v1 job; a 1M /v1 request every "
                                              "300 s; a 200K chat document every 120 s", d(3600.0),
                                              v1_drain_blocks_chat_docs, drain_s=600.0),
        "one_background_1m": Scenario("one_background_1m", "400K /v1 job; ONE 1M /v1 waiter; a 200K chat document "
                                      "every 120 s (run with --long-output-wait-s 3600)", d(3600.0),
                                      one_background_1m, drain_s=600.0),
        "doomed_chat_doc_blocks_v1": Scenario("doomed_chat_doc_blocks_v1", "400K /v1 job; a 16K /v1 long answer "
                                              "every 400 s; with and without a 950K chat document every 600 s",
                                              d(3600.0), doomed_chat_doc_blocks_v1, compare_baseline=True,
                                              drain_s=600.0),
        "v1_big_prompts_beside_1m": Scenario("v1_big_prompts_beside_1m", "a 1M /v1 answer; from 2 h in, /v1 NORMAL "
                                             "requests with 100-131K prompts at 0.1/s and chat 0.05/s", d(10_800.0),
                                             v1_big_prompts_beside_1m, drain_s=3_600.0),
        "v1_drain_blocks_chat_long_answers": Scenario(
            "v1_drain_blocks_chat_long_answers", "400K /v1 job; a 1M /v1 request every 300 s; a chat long answer "
            "(max_tokens 200K) every 120 s", d(3600.0), v1_drain_blocks_chat_long_answers, drain_s=600.0),
        "one_background_1m_chat_long_answers": Scenario(
            "one_background_1m_chat_long_answers", "400K /v1 job; ONE 1M /v1 waiter; a chat long answer every 120 s "
            "(run with --long-output-wait-s 3600)", d(3600.0), one_background_1m_chat_long_answers, drain_s=600.0),
    }


SCENARIOS = ("mixed", "long_burst", "v1_flood", "two_1m", "chat_doc_behind_1m", "chat_vs_v1_flood",
             "v1_burst_then_chat", "v1_doc_loop", "chat_docs_back_to_back_1m", "v1_drain_blocks_chat_docs",
             "one_background_1m", "doomed_chat_doc_blocks_v1", "v1_big_prompts_beside_1m",
             "v1_drain_blocks_chat_long_answers", "one_background_1m_chat_long_answers")


# ---------------------------------------------------------------------------
# Running one simulation through the REAL admission lanes
# ---------------------------------------------------------------------------


@dataclass
class Config:
    seed: int = 20260913
    token_scale: float = 0.001
    normal_max: int = 10
    long_output_max: int = 2
    reserve: float = 0.35
    chat_weight: int = 3
    reserved_chat_slots: int = 2
    max_waiting: int = 400
    normal_wait_s: float = 600.0
    long_wait_s: float = 600.0
    long_output_wait_s: float = 600.0
    chat_rate: Optional[float] = None
    duration: Optional[float] = None
    kv_sample: bool = True
    #: 8192 is the build. A huge value puts every /v1 answer in NORMAL: the
    #: counterfactual of no LONG_OUTPUT lane (long answers holding chat's seats).
    v1_long_output_threshold: int = 8192
    #: Keep every chat TTFT in the report (for pooling several seeds).
    keep_samples: bool = False
    #: Keep (population, arrival, admitted_at, refusal) per request in the report.
    keep_timeline: bool = False
    #: False: /v1 NORMAL requests are never held for KV (the counterfactual of
    #: the managed limit, adversarial review 2026-09-13).
    v1_normal_kv: bool = True
    headroom: float = 0.15
    v1_kv_promote_s: float = 300.0
    v1_closure_duty: float = 0.10
    v1_closure_window_s: float = 7200.0


@dataclass
class Observed:
    normal_active_max: int = 0
    normal_v1_max: int = 0
    long_active_max: int = 0
    long_output_active_max: int = 0
    kv_committed_max: int = 0
    kv_over_budget: int = 0
    budget: int = 0
    engine_running_max: int = 0


def _pct(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _summary(values: Sequence[float]) -> dict:
    return {"n": len(values), "p50": _r(_pct(values, 0.5)), "p95": _r(_pct(values, 0.95)),
            "max": _r(max(values) if values else None)}


def _r(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(float(v), 3)


def _patches(cfg: Config, clock: VirtualClock, engine_ref: dict, reqs_by_id: Dict[int, Req]) -> contextlib.ExitStack:
    from app import admission, engine_state, kv_budget
    from app.config import settings

    stack = contextlib.ExitStack()
    proxies = {k: DEAD_PROXY for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")}
    proxies.update({"NO_PROXY": "", "no_proxy": ""})
    stack.enter_context(mock.patch.dict(os.environ, proxies))
    for name, value in (
        ("admission_normal_max", cfg.normal_max), ("admission_long_max", 1), ("admission_long_idle_max", 0),
        ("admission_long_threshold_tokens", 131_072), ("admission_normal_wait_s", cfg.normal_wait_s),
        ("admission_long_wait_s", cfg.long_wait_s), ("admission_max_waiting", cfg.max_waiting),
        ("model_max_context", WINDOW),
        ("admission_long_output_max_seqs", cfg.long_output_max),
        ("admission_v1_long_output_threshold_tokens", cfg.v1_long_output_threshold),
        ("admission_chat_long_output_threshold_tokens", 65536),
        ("admission_long_output_wait_s", cfg.long_output_wait_s),
        ("admission_long_output_retry_after_s", 60.0),
        ("admission_kv_reserve_fraction", cfg.reserve), ("admission_kv_pool_tokens", 1_663_201),
        ("admission_kv_block_size", 2096), ("admission_kv_fixed_blocks_per_seq", 3),
        ("admission_kv_pool_refresh_s", 300.0), ("admission_kv_metrics_url", ""),
        ("admission_chat_weight", cfg.chat_weight), ("admission_chat_reserved_normal_slots", cfg.reserved_chat_slots),
        ("admission_kv_unmanaged_headroom_fraction", cfg.headroom), ("admission_v1_kv_promote_s", cfg.v1_kv_promote_s),
        ("admission_v1_long_closure_duty", cfg.v1_closure_duty),
        ("admission_v1_long_closure_window_s", cfg.v1_closure_window_s),
    ):
        stack.enter_context(mock.patch.object(settings, name, value, create=True))
    shim = types.SimpleNamespace(monotonic=clock.time, time=lambda: 1.8e9 + clock.now, sleep=time.sleep,
                                 perf_counter=clock.time)
    stack.enter_context(mock.patch.object(admission, "time", shim))
    stack.enter_context(mock.patch.object(kv_budget, "time", shim))

    def load(now=None):
        engine = engine_ref.get("engine")
        if engine is None:
            return None
        sample = engine.sample()
        if not cfg.kv_sample:
            sample.pop("kv_cache_usage", None)
        return sample

    stack.enter_context(mock.patch.object(engine_state, "engine_load", load))
    if not cfg.v1_normal_kv:
        stack.enter_context(mock.patch.object(admission.Lanes, "normal_kv_ok", lambda self, w, protected=(): True))

    async def fetch(url):
        assert url.startswith("http://stub-engine.invalid"), url
        return engine_ref["engine"].metrics_text()

    stack.enter_context(mock.patch.object(kv_budget, "_fetch_text", fetch))

    async def planted_prompt_tokens(messages, *, base_url, model):
        return reqs_by_id[int(messages[0]["rid"])].prompt

    stack.enter_context(mock.patch.object(admission, "prompt_tokens", planted_prompt_tokens))
    return stack


async def _simulate(scn: Scenario, cfg: Config, reqs: List[Req], clock: VirtualClock, engine_ref: dict) -> dict:
    from app import admission, kv_budget

    engine = StubEngine(now=clock.time, token_scale=cfg.token_scale)
    engine_ref["engine"] = engine
    seen = Observed()
    seen.budget = kv_budget.budget_tokens(kv_budget.setting_pool())
    engine_task = asyncio.ensure_future(engine.run())

    def observe_lanes() -> None:
        ls = admission.lanes()
        seen.normal_active_max = max(seen.normal_active_max, ls.normal.active)
        seen.normal_v1_max = max(seen.normal_v1_max, ls.normal.active_by_origin.get("v1", 0))
        seen.long_active_max = max(seen.long_active_max, ls.long.active)
        seen.long_output_active_max = max(seen.long_output_active_max, ls.long_output.active)
        seen.kv_committed_max = max(seen.kv_committed_max, ls.ledger.committed)
        if len(ls.ledger) > 1 and ls.ledger.committed > seen.budget:
            seen.kv_over_budget += 1
        seen.engine_running_max = max(seen.engine_running_max, len(engine.running) + 1)

    async def client(req: Req) -> None:
        async def op():
            req.admitted_at = clock.now
            observe_lanes()
            seq = engine.submit(req.rid, req.prompt, req.output)
            return StubStream(engine, seq)

        with admission.origin(req.origin):
            req.lane = admission.lane_for(req.prompt, req.max_tokens, req.origin)
            try:
                stream = await admission.run(op, messages=[{"role": "user", "content": "", "rid": req.rid}],
                                             base_url=STUB_URL, model="stub", stream=True,
                                             max_tokens=req.max_tokens)
            except admission.AdmissionRejected as exc:
                req.rejected = (exc.lane, exc.reason)
                req.rejected_at = clock.now
                return
        try:
            async for chunk in stream:
                if req.first_at is None:
                    req.first_at = clock.now
            req.done_at = clock.now
        finally:
            await stream.aclose()

    clients: List[asyncio.Task] = []
    for req in sorted(reqs, key=lambda r: r.arrival):
        delay = req.arrival - clock.now
        if delay > 0:
            await asyncio.sleep(delay)
        clients.append(asyncio.ensure_future(client(req)))
    deadline = scn.duration + scn.drain_s
    pending = {c for c in clients if not c.done()}
    if pending:
        _done, pending = await asyncio.wait(pending, timeout=max(0.0, deadline - clock.now))
    for task in pending:
        task.cancel()
    await asyncio.gather(*clients, return_exceptions=True)
    ended = clock.now
    engine.stop()
    engine_task.cancel()
    await asyncio.gather(engine_task, return_exceptions=True)
    return _report(reqs, seen, engine, cfg, ended, unfinished=len(pending))


def _report(reqs: List[Req], seen: Observed, engine: StubEngine, cfg: Config, ended: float, *, unfinished: int) -> dict:
    pops = sorted({r.pop for r in reqs})
    bounds = {"normal": cfg.normal_wait_s, "long": cfg.long_wait_s, "long_output": cfg.long_output_wait_s}
    waits: Dict[str, List[float]] = {"chat": [], "v1": []}
    normal_waits: Dict[str, List[float]] = {"chat": [], "v1": []}
    lane_waits: Dict[str, List[float]] = {}
    ttft: Dict[str, List[float]] = {p: [] for p in pops}
    rejected: Dict[str, int] = {}
    longest_v1 = 0.0
    for r in reqs:
        if r.admitted_at is not None:
            wait = r.admitted_at - r.arrival
        elif r.rejected_at is not None:
            wait = r.rejected_at - r.arrival
        else:
            continue
        if r.rejected is not None:
            key = f"{r.rejected[0]}/{r.rejected[1]}"
            rejected[key] = rejected.get(key, 0) + 1
        waits[r.origin].append(wait)
        if r.lane == "normal":
            normal_waits[r.origin].append(wait)
        lane_waits.setdefault(r.lane or "?", []).append(wait)
        if r.origin == "v1":
            longest_v1 = max(longest_v1, wait)
        if r.first_at is not None:
            ttft[r.pop].append(r.first_at - r.arrival)
    from app import admission

    ceiling = float(admission.LONG_CLOSURE_MAX_S)
    out = {
        "requests": {p: sum(1 for r in reqs if r.pop == p) for p in pops},
        "admitted": {p: sum(1 for r in reqs if r.pop == p and r.admitted_at is not None) for p in pops},
        "rejected": rejected,
        "rejected_by_population": {p: sum(1 for r in reqs if r.pop == p and r.rejected is not None) for p in pops},
        "wait_s_by_origin": {o: _summary(v) for o, v in waits.items()},
        "wait_s_by_lane": {lane: _summary(v) for lane, v in sorted(lane_waits.items())},
        "normal_wait_s_by_origin": {o: dict(_summary(v), mean=_r(sum(v) / len(v)) if v else None)
                                    for o, v in normal_waits.items()},
        "ttft_s_by_population": {p: _summary(v) for p, v in ttft.items()},
        "lane_active_max": {"normal": seen.normal_active_max, "normal_v1": seen.normal_v1_max,
                            "long": seen.long_active_max, "long_output": seen.long_output_active_max},
        "kv": {"budget_tokens": seen.budget, "committed_max_tokens": seen.kv_committed_max,
               "over_budget_with_more_than_one_charge": seen.kv_over_budget},
        "stub": {"kv_peak_blocks": engine.peak_blocks, "blocks": engine.blocks, "preemptions": engine.preemptions,
                 "max_engine_running": engine.max_running},
        "longest_v1_wait_s": _r(longest_v1),
        "waits_within_bound": all(
            (max(v) if v else 0.0) <= bounds.get(lane, max(bounds.values())) + ceiling + 1.0
            for lane, v in lane_waits.items()),
        "output_tok_s": _r(engine.generated_total / ended) if ended > 0 else None,
        # Seat-seconds held in NORMAL over the arrival window: how close the lane runs to full.
        # Seat-seconds each origin held in NORMAL: a grant is not a share of the lane.
        "normal_seat_s_by_origin": {o: _r(sum(
            (min(r.done_at if r.done_at is not None else ended, ended) - r.admitted_at)
            for r in reqs if r.lane == "normal" and r.origin == o and r.admitted_at is not None))
            for o in ("chat", "v1")},
        "normal_mean_occupancy": _r(sum(
            (min(r.done_at if r.done_at is not None else ended, ended) - r.admitted_at)
            for r in reqs if r.lane == "normal" and r.admitted_at is not None) / ended) if ended > 0 else None,
        "virtual_s": _r(ended),
        "unfinished_at_drain": unfinished,
    }
    if cfg.keep_samples:
        out["chat_ttft_samples"] = [round(v, 4) for v in ttft.get("chat", [])]
    if cfg.keep_timeline:
        out["timeline"] = [(r.pop, _r(r.arrival), _r(r.admitted_at), "/".join(r.rejected) if r.rejected else None)
                           for r in sorted(reqs, key=lambda r: r.arrival)]
    return out


def run_scenario(name: str, cfg: Config) -> dict:
    """Run one scenario (and its baseline, when it has one) and return the report."""
    scn = _scenarios(cfg.chat_rate, cfg.duration)[name]
    labels = ("with", "baseline") if scn.compare_baseline else ("with",)
    # The lanes log every refusal and every "engine not idle"; in a simulation
    # of thousands of requests those are the expected output, not news.
    quiet = logging.getLogger("app.admission")
    level = quiet.level
    quiet.setLevel(logging.ERROR)
    try:
        return _run_scenario(name, cfg, labels, scn)
    finally:
        quiet.setLevel(level)


def _run_scenario(name: str, cfg: Config, labels: Tuple[str, ...], scn: Scenario) -> dict:
    from app import admission, kv_budget, metrics

    runs: Dict[str, dict] = {}
    for label in labels:
        builder = Builder(cfg.seed)
        scn.build(builder, label == "with")
        reqs = builder.reqs
        by_id = {r.rid: r for r in reqs}
        clock = VirtualClock()
        loop = asyncio.new_event_loop()
        engine_ref: dict = {}
        started = time.perf_counter()
        try:
            with _patches(cfg, clock, engine_ref, by_id), clock.patch_loop(loop):
                admission.reset()
                kv_budget.reset()
                metrics.reset()
                runs[label] = loop.run_until_complete(_simulate(scn, cfg, reqs, clock, engine_ref))
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            admission.reset()
            kv_budget.reset()
        runs[label]["cpu_wall_s"] = round(time.perf_counter() - started, 2)
    report = {
        "scenario": name, "about": scn.about, "engine_curve": ENGINE_CURVE_LABEL,
        "config": {k: getattr(cfg, k) for k in cfg.__dataclass_fields__},
        "runs": runs,
    }
    if scn.compare_baseline:
        with_p95 = runs["with"]["ttft_s_by_population"].get("chat", {}).get("p95")
        base_p95 = runs["baseline"]["ttft_s_by_population"].get("chat", {}).get("p95")
        report["chat_ttft_p95_ratio_with_over_baseline"] = (
            round(with_p95 / base_p95, 4) if with_p95 is not None and base_p95 else None)
    return report


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def table(report: dict) -> str:
    lines = [f"== {report['scenario']}: {report['about']}", f"   engine: {report['engine_curve']}"]
    head = ("run", "reqs", "rejected", "chat wait p50/p95/max s", "v1 wait p50/p95/max s", "chat TTFT p50/p95 s",
            "lane max n/v1/L/LO", "KV commit max/budget", "stub peak blk", "preempt", "eng run max",
            "v1 longest wait s", "tok/s", "NORMAL seat-s chat/v1", "unfinished")
    lines.append(" | ".join(head))
    for label, run in report["runs"].items():
        cw, vw = run["wait_s_by_origin"]["chat"], run["wait_s_by_origin"]["v1"]
        ct = run["ttft_s_by_population"].get("chat", {"p50": None, "p95": None})
        la = run["lane_active_max"]
        lines.append(" | ".join(str(x) for x in (
            label, sum(run["requests"].values()), run["rejected"] or 0,
            f"{cw['p50']}/{cw['p95']}/{cw['max']}", f"{vw['p50']}/{vw['p95']}/{vw['max']}",
            f"{ct['p50']}/{ct['p95']}", f"{la['normal']}/{la['normal_v1']}/{la['long']}/{la['long_output']}",
            f"{run['kv']['committed_max_tokens']}/{run['kv']['budget_tokens']}", run["stub"]["kv_peak_blocks"],
            run["stub"]["preemptions"], run["stub"]["max_engine_running"], run["longest_v1_wait_s"],
            run["output_tok_s"], f"{run['normal_seat_s_by_origin']['chat']}/{run['normal_seat_s_by_origin']['v1']}",
            run["unfinished_at_drain"])))
        lines.append(f"   {label}: admitted {run['admitted']} refused {run['rejected_by_population']}")
    if "chat_ttft_p95_ratio_with_over_baseline" in report:
        lines.append(f"   chat TTFT p95 with / baseline = {report['chat_ttft_p95_ratio_with_over_baseline']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# --serve-stub: the same engine over HTTP, for a PRIVATE orchestrator
# ---------------------------------------------------------------------------


def parse_serve_target(value: str) -> Tuple[str, int]:
    """HOST:PORT on loopback, never a production port. SystemExit(2) otherwise."""
    host, sep, port_s = (value or "").rpartition(":")
    host = host.strip("[]")
    if not sep or not host or not port_s.isdigit():
        print(f"--serve-stub wants HOST:PORT, got {value!r}", file=sys.stderr)
        raise SystemExit(2)
    port = int(port_s)
    loopback = host == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        print(f"--serve-stub refuses non-loopback host {host!r}: the stub is for a private orchestrator only",
              file=sys.stderr)
        raise SystemExit(2)
    if port in REFUSED_PORTS or not (1 <= port <= 65535):
        print(f"--serve-stub refuses port {port}: a production service port (or invalid)", file=sys.stderr)
        raise SystemExit(2)
    return host, port


async def serve_stub(host: str, port: int, *, answer_tokens: int, token_scale: float) -> None:
    loop = asyncio.get_running_loop()
    engine = StubEngine(now=loop.time, token_scale=token_scale)
    engine_task = asyncio.ensure_future(engine.run())
    counter = {"n": 0}

    async def respond(writer, status: str, body: bytes, ctype: str = "application/json") -> None:
        writer.write(f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                     "Connection: close\r\n\r\n".encode() + body)
        await writer.drain()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            request_line, *header_lines = head.decode("latin-1").split("\r\n")
            method, path, _ = request_line.split(" ", 2)
            headers = {h.split(":", 1)[0].strip().lower(): h.split(":", 1)[1].strip() for h in header_lines if ":" in h}
            body = await reader.readexactly(int(headers.get("content-length", "0") or 0))
            path = path.split("?", 1)[0]
            if method == "GET" and path == "/metrics":
                await respond(writer, "200 OK", engine.metrics_text().encode(), "text/plain; version=0.0.4")
            elif method == "GET" and path in ("/health", "/ping"):
                await respond(writer, "200 OK", b"")
            elif method == "GET" and path == "/v1/models":
                await respond(writer, "200 OK", json.dumps({"object": "list", "data": [
                    {"id": "stub", "object": "model", "max_model_len": WINDOW}]}).encode())
            elif method == "POST" and path == "/tokenize":
                doc = json.loads(body or b"{}")
                text = json.dumps(doc.get("messages") or doc.get("prompt") or "")
                await respond(writer, "200 OK", json.dumps({"count": max(1, len(text) // 3), "max_model_len": WINDOW,
                                                            "tokens": []}).encode())
            elif method == "POST" and path == "/v1/chat/completions":
                doc = json.loads(body or b"{}")
                prompt = max(1, len(json.dumps(doc.get("messages") or [])) // 3)
                target = min(int(doc.get("max_tokens") or answer_tokens), answer_tokens)
                counter["n"] += 1
                seq = engine.submit(counter["n"], prompt, target)
                stream = StubStream(engine, seq)
                rid = f"chatcmpl-stub-{counter['n']}"
                if not doc.get("stream"):
                    async for _ in stream:
                        pass
                    await respond(writer, "200 OK", json.dumps({
                        "id": rid, "object": "chat.completion", "model": doc.get("model", "stub"),
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "x" * target},
                                     "finish_reason": "length" if target >= int(doc.get("max_tokens") or 1e18) else "stop"}],
                        "usage": {"prompt_tokens": prompt, "completion_tokens": target,
                                  "total_tokens": prompt + target}}).encode())
                    return
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\n"
                             b"Connection: close\r\n\r\n")
                sent = 0
                try:
                    async for _ in stream:
                        n = 1 if sent == 0 else int(stream.engine.chunk_tokens)
                        sent += n
                        frame = {"id": rid, "object": "chat.completion.chunk", "model": doc.get("model", "stub"),
                                 "choices": [{"index": 0, "delta": {"content": "x" * n}, "finish_reason": None}]}
                        writer.write(b"data: " + json.dumps(frame).encode() + b"\n\n")
                        await writer.drain()
                    final = {"id": rid, "object": "chat.completion.chunk", "model": doc.get("model", "stub"),
                             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                    usage = {"id": rid, "object": "chat.completion.chunk", "choices": [],
                             "usage": {"prompt_tokens": prompt, "completion_tokens": target,
                                       "total_tokens": prompt + target}}
                    writer.write(b"data: " + json.dumps(final).encode() + b"\n\n")
                    writer.write(b"data: " + json.dumps(usage).encode() + b"\n\ndata: [DONE]\n\n")
                    await writer.drain()
                finally:
                    await stream.close()
            else:
                await respond(writer, "404 Not Found", b'{"error":"not found"}')
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    server = await asyncio.start_server(handle, host, port)
    print(f"stub engine on http://{host}:{port} (engine curve: {ENGINE_CURVE_LABEL})", flush=True)
    try:
        async with server:
            await server.serve_forever()
    finally:
        engine.stop()
        engine_task.cancel()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", nargs="+", default=["mixed"], choices=list(SCENARIOS) + ["all"])
    ap.add_argument("--seed", type=int, default=20260913)
    ap.add_argument("--duration", type=float, default=None, help="virtual seconds of arrivals (scenario default)")
    ap.add_argument("--chat-rate", type=float, default=None, help="chat turns per second (scenario default)")
    ap.add_argument("--token-scale", type=float, default=None,
                    help="one stub chunk per 1/scale real tokens (default 0.001; 1.0 with --serve-stub)")
    ap.add_argument("--normal-max", type=int, default=10)
    ap.add_argument("--long-output-max", type=int, default=2)
    ap.add_argument("--reserve", type=float, default=0.35)
    ap.add_argument("--chat-weight", type=int, default=3)
    ap.add_argument("--reserved-chat-slots", type=int, default=2)
    ap.add_argument("--long-output-wait-s", type=float, default=600.0,
                    help="the lane's bound; 3600 = publicapi's background bound (one_background_1m)")
    ap.add_argument("--no-v1-normal-kv", action="store_true",
                    help="counterfactual: /v1 NORMAL requests are never held for KV")
    ap.add_argument("--v1-long-output-threshold", type=int, default=8192,
                    help="8192 = the build; 1000000000 = the counterfactual with no LONG_OUTPUT lane")
    ap.add_argument("--no-kv-sample", action="store_true",
                    help="the controller sample carries no kv_cache_usage (today's engine_state, before integration)")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--assert-no-preemption", action="store_true")
    ap.add_argument("--serve-stub", default="", metavar="HOST:PORT")
    ap.add_argument("--serve-answer-tokens", type=int, default=128)
    args = ap.parse_args(argv)

    if args.serve_stub:
        # Refuse first: a refused target must leave the caller's environment alone.
        host, port = parse_serve_target(args.serve_stub)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
            os.environ[key] = DEAD_PROXY
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = ""
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(serve_stub(host, port, answer_tokens=args.serve_answer_tokens,
                                   token_scale=args.token_scale if args.token_scale else 1.0))
        return 0

    cfg = Config(seed=args.seed, token_scale=args.token_scale or 0.001, normal_max=args.normal_max,
                 long_output_max=args.long_output_max, reserve=args.reserve, chat_weight=args.chat_weight,
                 reserved_chat_slots=args.reserved_chat_slots, chat_rate=args.chat_rate, duration=args.duration,
                 kv_sample=not args.no_kv_sample, v1_long_output_threshold=args.v1_long_output_threshold,
                 long_output_wait_s=args.long_output_wait_s, v1_normal_kv=not args.no_v1_normal_kv)
    names = list(SCENARIOS) if "all" in args.scenario else args.scenario
    reports = []
    preempted = 0
    for name in names:
        report = run_scenario(name, cfg)
        reports.append(report)
        print(table(report), flush=True)
        preempted += sum(run["stub"]["preemptions"] for run in report["runs"].values())
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(reports, indent=2))
    if args.assert_no_preemption and preempted:
        print(f"FAIL: {preempted} stub preemptions", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
