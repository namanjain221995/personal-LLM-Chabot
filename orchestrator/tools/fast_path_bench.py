"""Measure the Fast path end to end: TTFT, completion, and behaviour under load.

WHAT THIS MEASURES, AND WHAT IT DOES NOT
----------------------------------------
The app is driven IN PROCESS through its real ASGI stack (routing, auth
resolution, engine dispatch, SSE encoding), against an ISOLATED test database
and isolated storage. Two things are deliberately shared with production and
must be read as such:

  * the live inference endpoints (vLLM main/router/embed/rerank). The thing
    being measured is the orchestrator's request path AROUND the model, so the
    model has to be the real one. This adds inference load to a live service;
    it is stated here rather than hidden.
  * nothing else. No production database, no production storage, no deploy,
    no container restart.

TRANSPORT MATTERS, AND IT BIT THIS HARNESS ONCE. `httpx.ASGITransport` buffers
the whole response body, so an in-process run reports every SSE event as
arriving at the same instant: `first_token_ms` then equals `total_ms` and is
NOT time-to-first-token at all, only completion time wearing its name. Measured
2026-09-06, an in-process run showed 22 token events all timestamped together.

So TTFT is measured over a real socket: `--serve` runs the app under uvicorn on
loopback and the client measures it over HTTP, where the stream actually
streams. That also puts real (small) loopback network cost inside the numbers
rather than excluding it. In-process mode is kept for completion-time work,
and refuses to report a TTFT it cannot honestly measure.

THE MEASUREMENT
---------------
`first_token_ms` is time to the first `token` event — the first character of
the ANSWER. It is not time to the first event: a `status`/`step` event is a
spinner, arrives much earlier, and is what makes a slow path feel fast in a
demo while the user still waits. Both are recorded separately so the
difference is visible.

A/B: `--legacy` restores the two shapes this phase replaced (per-candidate
cosine, and an httpx client rebuilt per token count) by monkeypatching them
back, so before/after is measured in one process on one box, minutes apart,
against the same model — rather than across a checkout, where the model's own
load would confound it.

    python tools/fast_path_bench.py --concurrency 1 4 8 --requests 4
    python tools/fast_path_bench.py --concurrency 1 4 8 --requests 4 --legacy
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional


def _bootstrap_env(scratch: Path) -> None:
    """Isolated storage + an isolated database, asserted before anything loads."""
    dsn = os.environ.get("APP_DATABASE_URL", "")
    name = dsn.rsplit("/", 1)[-1].split("?")[0] if dsn else ""
    if not (name == "test" or name.startswith("test_") or name.endswith("_test")):
        raise SystemExit(
            f"refusing to run against database {name!r}: set APP_DATABASE_URL to an "
            "isolated test database (name 'test', 'test_*' or '*_test')"
        )
    scratch.mkdir(parents=True, exist_ok=True)
    for key, sub in (
        ("REPORTS_DIR", "reports"), ("WORKSPACE_DIR", "workspace"),
        ("LANCEDB_DIR", "lancedb"), ("PARQUET_DIR", "parquet"),
        # LANCEDB_WEB_DIR is a SEPARATE variable from LANCEDB_DIR (the
        # Salesforce corpus). Isolating only the latter left the public-web
        # index on its default /data/lancedb-web, which is the deployment's
        # real one -- so "isolated storage" was not true for the index the
        # cached-knowledge and follow-up scenarios actually read, and those
        # scenarios measured an empty index no matter what they seeded.
        ("LANCEDB_WEB_DIR", "lancedb-web"),
    ):
        path = scratch / sub
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)
    os.environ["DUCKDB_PATH"] = str(scratch / "bench.duckdb")
    # Background ingestion would compete with the thing being measured.
    os.environ.setdefault("WEB_KNOWLEDGE_WORKER_ENABLED", "false")



# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
# Goal 2 asks for Fast-mode latency, and "Fast mode" is not one code path. The
# earlier rounds measured only `plain`, which exercises the recall/compaction
# work and nothing else — so the search-path work that ALSO runs on this event
# loop was invisible to the numbers. These four cover what a user actually does.
#
# Each turn is a full request. `expect` is checked against the concatenated
# answer text: answer quality is reported next to latency because a fast wrong
# answer is not an improvement, and every earlier measurement in this phase
# reported speed with no correctness signal at all.

SCENARIOS = {
    # The pure orchestrator path: no search, no stored evidence. Isolates the
    # recall/compaction/tokenize work; comparable with this phase's CPU A/B.
    "plain": {
        "web_search": "off",
        "seed_pages": False,
        "turns": [
            [("In one sentence, what is a vector database?", ["database", "vector"])],
            [("In one sentence, what does an SSE stream do?", ["event", "stream"])],
            [("In one sentence, what is a cross-encoder reranker?", ["rank", "rerank", "score"])],
            [("In one sentence, what is speculative decoding?", ["token", "draft", "decod"])],
        ],
    },
    # Answers from the STORED public corpus with no network: the living-knowledge
    # pre-pass reading pages this harness seeded and indexed.
    "cached": {
        "web_search": "off",
        "seed_pages": True,
        "turns": [
            [("What is GPT-5.2's reasoning score on the BenchLM leaderboard?", ["82.7"])],
            [("What does an H100 cost per GPU-hour on Orbital Compute?", ["2.90"])],
            [("Does Orbital Compute offer a B200 instance, and at what price?", ["6.75"])],
            [("How many models are ranked on the BenchLM reasoning leaderboard?", ["14"])],
        ],
    },
    # A real web search: SearXNG, real fetches, rerank, the whole engine.
    "search": {
        "web_search": "on",
        "seed_pages": False,
        "turns": [
            [("What is the current stable version of PostgreSQL?", ["1"])],
            [("What is the latest released version of Python?", ["3."])],
            [("Who is the current CEO of NVIDIA?", ["Huang"])],
            [("What is the current stable Linux kernel version?", ["."])],
        ],
    },
    # The regression this phase exists for: a terse second turn whose referent
    # is only recoverable from the first. Seeded so the answer is checkable.
    "followup": {
        "web_search": "off",
        "seed_pages": True,
        "turns": [
            [("Which models are ranked on the BenchLM reasoning leaderboard?", ["GPT-5.2"]),
             ("and its score?", ["82.7"])],
            [("How does GPT-5 do on BenchLM reasoning?", ["83.2"]),
             ("what about 5.2?", ["82.7"])],
            [("What does an H100 cost per GPU-hour on Orbital Compute?", ["2.90"]),
             ("and the B200?", ["6.75"])],
            [("Which GPUs does Orbital Compute list?", ["H100"]),
             ("which is cheapest?", ["0.95", "L40S", "oc-l1"])],
        ],
    },
}


class _Sample:
    __slots__ = ("ok", "first_event_ms", "first_token_ms", "total_ms",
                 "events", "tokens", "error", "meta", "answer", "question",
                 "expected", "quality_ok")

    def __init__(self) -> None:
        self.ok = False
        self.first_event_ms: Optional[float] = None
        self.first_token_ms: Optional[float] = None
        self.total_ms: Optional[float] = None
        self.events: Dict[str, int] = {}
        self.tokens = 0
        self.error = ""
        self.meta: dict = {}
        self.answer = ""
        self.question = ""
        self.expected: List[str] = []
        self.quality_ok = False


async def _one(client, question: str, conversation_id: str, *,
               web_search: str = "off", history=None, timeout_note=None) -> _Sample:
    """One Fast-mode turn, timed event by event over a real socket."""
    sample = _Sample()
    messages = list(history or []) + [{"role": "user", "content": question}]
    body = {
        "messages": messages,
        "session_id": conversation_id,
        "conversation_id": conversation_id,
        "mode": "assistant",   # General. Never the Salesforce brain.
        "model": "smart",
        "effort": "fast",
        "web_search": web_search,
    }
    start = time.perf_counter()
    answer: List[str] = []
    try:
        async with client.stream("POST", "/chat", json=body) as resp:
            if resp.status_code != 200:
                sample.error = f"HTTP {resp.status_code}"
                await resp.aread()
                return sample
            event = ""
            async for line in resp.aiter_lines():
                now = (time.perf_counter() - start) * 1000.0
                if sample.first_event_ms is None and line.strip():
                    sample.first_event_ms = now
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                    sample.events[event] = sample.events.get(event, 0) + 1
                elif line.startswith("data:"):
                    payload = line.split(":", 1)[1].strip()
                    if event == "token":
                        if sample.first_token_ms is None:
                            sample.first_token_ms = now
                        sample.tokens += 1
                        # A token frame is {"text": "..."} — not a bare
                        # string. Appending the parsed object stringified the
                        # dict and made every quality check meaningless.
                        try:
                            parsed = json.loads(payload)
                        except (ValueError, TypeError):
                            parsed = payload
                        if isinstance(parsed, dict):
                            answer.append(str(parsed.get("text", "")))
                        else:
                            answer.append(str(parsed))
                    elif event == "meta":
                        try:
                            sample.meta = json.loads(payload)
                        except (ValueError, TypeError):
                            pass
                    elif event == "error":
                        sample.error = payload[:200]
                    elif event == "done":
                        sample.total_ms = now
                        sample.ok = True
    except Exception as exc:  # noqa: BLE001 — a failed sample IS a result
        sample.error = f"{type(exc).__name__}: {exc}"[:200]
    if sample.total_ms is None:
        sample.total_ms = (time.perf_counter() - start) * 1000.0
    sample.answer = "".join(answer)
    return sample


async def _conversation(client, turns, conversation_id: str, web_search: str) -> List[_Sample]:
    """Run a scenario's turns in order, carrying the answers forward as history.

    A follow-up is only a follow-up if the prior turn is actually in the
    prompt; sending the terse question alone would measure a different thing.
    """
    history: List[dict] = []
    out: List[_Sample] = []
    for question, expected in turns:
        sample = await _one(client, question, conversation_id,
                            web_search=web_search, history=history)
        sample.question = question
        sample.expected = list(expected)
        # Quality: substring match against the streamed answer. Crude on
        # purpose — anything cleverer would need a judge model, and a judge
        # would put the thing being measured inside the measurement.
        low = (sample.answer or "").lower()
        sample.quality_ok = bool(sample.ok) and any(
            e.lower() in low for e in expected
        ) if expected else bool(sample.ok)
        out.append(sample)
        history = history + [
            {"role": "user", "content": question},
            {"role": "assistant", "content": sample.answer or ""},
        ]
    return out


def _pct(values: List[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def _proc_cpu_seconds(pid: int) -> Optional[float]:
    """utime+stime for one pid, in seconds. None if it cannot be read."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    try:
        ticks = float(fields[11]) + float(fields[12])
    except (IndexError, ValueError):
        return None
    return ticks / os.sysconf("SC_CLK_TCK")


def _round1(v):
    return None if v is None or v != v else round(v, 1)


def _summarise(label: str, samples: List[_Sample]) -> dict:
    ok = [s for s in samples if s.ok]
    ttft = [s.first_token_ms for s in ok if s.first_token_ms is not None]
    total = [s.total_ms for s in ok if s.total_ms is not None]
    graded = [s for s in ok if s.expected]
    buffered = bool(ok) and all(
        s.first_token_ms is not None and s.total_ms is not None
        and abs(s.first_token_ms - s.total_ms) < 1.0 for s in ok
    )
    seen: Dict[str, int] = {}
    for s in samples:
        for name, n in s.events.items():
            seen[name] = seen.get(name, 0) + n
    return {
        "label": label,
        "samples": len(samples),
        "ok": len(ok),
        "errors": len(samples) - len(ok),
        # True when every sample's first token and its done event landed in the
        # same millisecond: the transport buffered, so this is completion time,
        # not TTFT. Recorded so a reader never has to infer it.
        "stream_buffered": buffered,
        "first_token_ms_p50": _round1(_pct(ttft, 0.50)) if ttft else None,
        "first_token_ms_p95": _round1(_pct(ttft, 0.95)) if ttft else None,
        "total_ms_p50": _round1(_pct(total, 0.50)) if total else None,
        "total_ms_p95": _round1(_pct(total, 0.95)) if total else None,
        "tokens_p50": _round1(statistics.median([s.tokens for s in ok])) if ok else None,
        "graded": len(graded),
        "quality_pass": sum(1 for s in graded if s.quality_ok),
        "quality_failures": [
            {"q": s.question[:70], "expected": s.expected, "got": (s.answer or "")[:160]}
            for s in graded if not s.quality_ok
        ][:5],
        "events": seen,
        "error_kinds": sorted({s.error for s in samples if s.error})[:5],
    }


def _install_legacy_shapes() -> List[str]:
    """Restore the pre-2026-09-06 shapes so before/after is one process apart."""
    restored: List[str] = []
    from app import context, recall

    def legacy_cosine_many(query, blobs):
        return [recall.cosine(query, recall.unpack_vector(b)) for b in blobs]

    recall.cosine_many = legacy_cosine_many  # type: ignore[assignment]
    restored.append("recall.cosine_many -> per-candidate cosine()")

    def legacy_tokenize_client():
        import httpx
        from app.config import settings

        return httpx.AsyncClient(timeout=settings.tokenize_timeout)

    context._tokenize_client = legacy_tokenize_client  # type: ignore[assignment]
    restored.append("context._tokenize_client -> a fresh AsyncClient per call")
    return restored


async def _run(args) -> dict:
    import contextlib

    import httpx

    from app import db

    results = {
        "mode": "legacy" if args.legacy else "current",
        "transport": "http" if args.base_url else "asgi-in-process",
        "rows": [],
    }
    if not args.base_url:
        # ASGITransport buffers the whole body, so every SSE event arrives at
        # once and first_token_ms is completion time wearing TTFT's name.
        results["ttft_valid"] = False

    if args.base_url:
        client_ctx = httpx.AsyncClient(base_url=args.base_url,
                                       timeout=httpx.Timeout(args.timeout))
        app_ctx = contextlib.AsyncExitStack()
    else:
        from app.main import app, lifespan
        client_ctx = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://bench.local", timeout=httpx.Timeout(args.timeout))
        app_ctx = lifespan(app)

    async with app_ctx:
        async with client_ctx as client:
            for name in args.scenario:
                spec = SCENARIOS[name]
                if spec["seed_pages"]:
                    n = await _seed_and_index(db)
                    print(f"  [{name}] seeded and indexed {n} corpus page(s)", flush=True)
                # Warm-up: the first request of a process pays import and pool
                # costs no real user pays. Excluded from every statistic.
                await _one(client, "Say READY.", f"{args.conversation}-warm",
                           web_search="off")
                for concurrency in args.concurrency:
                    convos = [spec["turns"][i % len(spec["turns"])]
                              for i in range(args.requests)]
                    ids = [f"{args.conversation}-{name}-{concurrency}-{i}"
                           for i in range(len(convos))]
                    if args.seed_chunks and not args.base_url:
                        await _seed_recall_chunks(db, ids, args.seed_chunks)
                    elif args.seed_chunks:
                        # Over HTTP the server owns the database; seed through
                        # the same DSN this process is configured with.
                        await _seed_recall_chunks(db, ids, args.seed_chunks)
                    cpu_before = (_proc_cpu_seconds(args.server_pid)
                                  if args.server_pid else None)
                    samples: List[_Sample] = []
                    for begin in range(0, len(convos), concurrency):
                        batch = convos[begin:begin + concurrency]
                        got = await asyncio.gather(*[
                            _conversation(client, turns, ids[begin + i],
                                          spec["web_search"])
                            for i, turns in enumerate(batch)
                        ], return_exceptions=True)
                        for item in got:
                            if isinstance(item, list):
                                samples.extend(item)
                    row = _summarise(f"{name} c={concurrency}", samples)
                    row.update({"scenario": name, "concurrency": concurrency,
                                "conversations": len(convos),
                                "web_search": spec["web_search"]})
                    cpu_after = (_proc_cpu_seconds(args.server_pid)
                                 if args.server_pid else None)
                    if cpu_before is not None and cpu_after is not None:
                        used = cpu_after - cpu_before
                        row["server_cpu_s"] = round(used, 3)
                        row["server_cpu_ms_per_turn"] = round(
                            used * 1000.0 / max(1, len(samples)), 1)
                    results["rows"].append(row)
                    q = (f"{row['quality_pass']}/{row['graded']}"
                         if row["graded"] else "n/a")
                    print(
                        f"  {name:9} c={concurrency:<2} turns={row['samples']:<3} "
                        f"ok={row['ok']:<3} err={row['errors']:<2} "
                        f"ttft p50={row['first_token_ms_p50']} p95={row['first_token_ms_p95']}  "
                        f"total p50={row['total_ms_p50']} p95={row['total_ms_p95']}  "
                        f"quality={q}"
                        + (f"  cpu={row['server_cpu_ms_per_turn']}ms/turn"
                           if row.get("server_cpu_ms_per_turn") is not None else ""),
                        flush=True)
                    for bad in row["quality_failures"]:
                        print(f"      MISS {bad['q']!r} expected {bad['expected']} "
                              f"got {bad['got'][:90]!r}", flush=True)
                    if row["error_kinds"]:
                        print(f"      errors: {row['error_kinds']}", flush=True)
    return results


async def _seed_recall_chunks(db, conversation_ids, count: int) -> None:
    """Give semantic recall real work to score, in EVERY conversation used.

    With an empty conversation `recall.cosine_many` is called with zero
    candidates and its cost is invisible, which would make a CPU A/B look like
    a null result for reasons unrelated to the change. An earlier version of
    this harness seeded only the base conversation id while the requests used
    per-request ids, so recall scored nothing in every measured turn.

    conversation_chunks has a foreign key to conversations, so the rows must
    exist first.
    """
    import array
    import random

    from tests.conftest import _materialize_test_user  # type: ignore

    if count <= 0:
        return
    rng = random.Random(99)
    dim = 1024
    user = await db.run_in_thread(_materialize_test_user, "bench", "member")
    for cid in conversation_ids:
        try:
            await db.run_in_thread(db.create_conversation, int(user["id"]), cid, "bench")
        except Exception:  # already present from an earlier run
            pass
        rows = [
            {
                "ordinal": i,
                "role": "user" if i % 2 else "assistant",
                "text": f"Earlier turn {i} about deployment, budgets and retrieval.",
                "embedding": array.array(
                    "f", [rng.uniform(-1, 1) for _ in range(dim)]).tobytes(),
            }
            for i in range(count)
        ]
        await db.run_in_thread(db.add_conversation_chunks, cid, rows)


async def _seed_and_index(db) -> int:
    """Store the eval fixtures as ordinary public pages AND index them.

    Through the real store and the real indexer, so the `cached` scenario
    measures the retrieval path a user actually gets.
    """
    import hashlib
    from pathlib import Path as _Path

    from app import web_index
    from app.core import extract

    fx = _Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "web_eval"
    pages = [
        ("leaderboard.html", "https://benchlm.test/leaderboard"),
        ("pricing_v2.html", "https://orbital.test/pricing"),
        ("hosting_costs.html", "https://nimbus.test/pricing"),
    ]
    ids = []
    for name, url in pages:
        ext = extract.extract_readable(
            "text/html", fx.joinpath(name).read_bytes(), url)
        row = await db.run_in_thread(
            db.upsert_web_page, url, url, url, ext.title or "", ext.text or "",
            "text/html", 200,
            hashlib.sha256((ext.text or "").encode()).hexdigest(),
        )
        ids.append(int(row["id"]))
    await web_index.index_pending(limit=len(ids) * 4, page_ids=ids)
    return len(ids)


def _serve(args) -> None:
    """Run the app under uvicorn so the SSE stream is measurable over a socket."""
    import uvicorn

    if os.environ.get("FAST_BENCH_LEGACY") == "1":
        for line in _install_legacy_shapes():
            print("  legacy shape:", line)
    from app.authn import principal as principal_module
    from app import auth as auth_module
    from tests.conftest import _principal_for  # type: ignore

    holder: dict = {}

    def resolver(_request):
        if "p" not in holder:
            holder["p"] = _principal_for("bench", "member")
        return holder["p"]

    principal_module.resolve_principal_sync = resolver  # type: ignore[assignment]
    auth_module.resolve_principal_sync = resolver  # type: ignore[assignment]

    print(f"  serving on 127.0.0.1:{args.port} "
          f"({'legacy' if os.environ.get('FAST_BENCH_LEGACY') == '1' else 'current'})",
          flush=True)
    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, log_level="warning")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 8])
    ap.add_argument("--requests", type=int, default=8,
                    help="conversations per concurrency level (a followup "
                         "conversation is 2 turns, so turns = 2x this)")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--legacy", action="store_true",
                    help="restore the pre-fix shapes, for the A/B baseline")
    ap.add_argument("--seed-chunks", type=int, default=300,
                    help="conversation chunks to seed so recall has work to do")
    ap.add_argument("--conversation", default="bench-fast")
    ap.add_argument("--out", default="")
    ap.add_argument("--serve", action="store_true",
                    help="run the app under uvicorn instead of benchmarking; "
                         "set FAST_BENCH_LEGACY=1 to install the pre-fix shapes")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--server-pid", type=int, default=0,
                    help="read this process's CPU time around the run. Wall-clock "
                         "TTFT is dominated by the shared model and varies by 2-3x "
                         "run to run; CPU seconds per request measures the "
                         "event-loop work this phase actually changed, and is "
                         "stable enough to draw a conclusion from")
    ap.add_argument("--base-url", default="",
                    help="measure a server already running at this URL over real "
                         "HTTP. Required for an honest TTFT (see module docstring)")
    ap.add_argument("--scenario", nargs="+", default=["plain"],
                    choices=sorted(SCENARIOS),
                    help="plain | cached | search | followup")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    _bootstrap_env(root / ".bench-scratch")

    if args.serve:
        _serve(args)
        return

    print(f"Fast-path benchmark — mode={'legacy' if args.legacy else 'current'}"
          f" transport={'http' if args.base_url else 'in-process (no TTFT)'}"
          f" scenarios={','.join(args.scenario)}")
    print(f"  database: {os.environ['APP_DATABASE_URL'].rsplit('/', 1)[-1]}")
    results = asyncio.run(_run(args))
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=1))
        print("wrote", args.out)


if __name__ == "__main__":
    main()
