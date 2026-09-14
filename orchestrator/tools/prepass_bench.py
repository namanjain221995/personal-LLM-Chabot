"""Time `living_knowledge.prepare` at Fast with every engine stubbed at the
latency production measured (pre-pass latency round, 2026-09-14).

WHAT THIS MEASURES. The knowledge pre-pass on its own, one question at a time
(concurrency 1): the freshness router, the speculative and real retrievals
(embed + dense scan, lexical SQL, merge/rank CPU, _page_meta), the rerank and
the partition. Every network and database boundary is a stub that sleeps for
the measured time, so the numbers are the SHAPE of the path (which stages run,
how often, in what order), not engine noise. No database, no network, no
engine is touched.

The stage latencies, seconds (production, 2026-09-14):
    router ok 0.15; router timeout 1.108 (cut at freshness.ROUTER_DEADLINE_S,
    0.6); embed 0.035; dense scan 0.29; lexical 0.088; merge/rank CPU 0.13;
    rerank 0.42 per call warm, 1.47 cold.

OLD vs NEW. Run it once per tree: `app` is imported from the working
directory, so pointing the interpreter at an unmodified checkout gives the old
numbers and at this one the new. In a tree that has
KNOWLEDGE_FAST_SPECULATIVE_SALVAGE the new tree is timed with salvage on and
off; KNOWLEDGE_PLEASANTRY_RULE likewise.

    cd orchestrator && python tools/prepass_bench.py --runs 5 --label new
    cd <old checkout>/orchestrator && python tools/prepass_bench.py --runs 5 --label old

Self-written questions only.
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
from typing import Dict, List

os.environ.setdefault("APP_DATABASE_URL", "postgresql://bench:bench@127.0.0.1:1/bench_unused")
sys.path.insert(0, os.getcwd())

#: (question, the router's word when it answers in time, with a prior exchange)
QUESTIONS = [
    ("hi ??", "RECENT", True),
    ("hi ??", "RECENT", False),
    ("thanks a lot", "RECENT", True),
    ("what is the cost of a used bicycle", "RECENT", False),
    ("write a haiku about autumn", "STATIC", False),
    ("explain how a bicycle gear ratio works", "STATIC", False),
]
_WORD = {q: w for q, w, _h in QUESTIONS}

LATENCY = {
    "router_ok": 0.15,
    "router_timeout": 1.108,
    "embed": 0.035,
    "dense": 0.29,
    "lexical": 0.088,
    "cpu": 0.13,
    "rerank_warm": 0.42,
    "rerank_cold": 1.47,
}


def _install(stages: Dict[str, List[float]], *, router: str, rerank_s: float) -> None:
    from app import db, freshness, rerank, web_index, web_memory
    from app import living_knowledge as lk
    from app.config import settings
    from app.freshness import Freshness, Verdict

    settings.web_memory_enabled = True
    settings.living_knowledge_topical = True
    settings.freshness_router_enabled = True
    settings.knowledge_rerank = True
    settings.knowledge_evidence_cache_ttl_s = 0.0  # every run pays the full path

    def span(name, started):
        stages.setdefault(name, []).append(time.perf_counter() - started)

    async def ask(question):
        started = time.perf_counter()
        try:
            await asyncio.sleep(LATENCY["router_ok"] if router == "ok" else LATENCY["router_timeout"])
            word = _WORD.get(question, "RECENT")
            level = Freshness.STATIC if word == "STATIC" else Freshness.RECENT
            return Verdict(level, freshness._MAX_AGE[level], "router")
        finally:
            span("router", started)

    async def dense(query, top_k=6, site_prefix=""):
        started = time.perf_counter()
        await asyncio.sleep(LATENCY["embed"] + LATENCY["dense"])
        span("embed+dense", started)
        return [
            {"url": f"https://bench{i}.example.org/p", "title": f"Bench page {i}",
             "text": f"A used bicycle page {i} about gears, prices and frames. " * 8,
             "fetched_at": "", "score": 0.2 + 0.03 * i}
            for i in range(top_k)
        ]

    def lexical(query, limit):
        started = time.perf_counter()
        time.sleep(LATENCY["lexical"])
        span("lexical", started)
        return []

    real_rank = web_memory._rank_candidates

    def rank(query, candidates, level):
        started = time.perf_counter()
        out = real_rank(query, candidates, level)
        left = LATENCY["cpu"] - (time.perf_counter() - started)
        if left > 0:
            time.sleep(left)
        span("merge+rank_cpu", started)
        return out

    async def score(query, docs, **kw):
        started = time.perf_counter()
        await asyncio.sleep(rerank_s)
        span("rerank", started)
        stages.setdefault("rerank_docs", []).append(float(len(docs)))
        return [0.9 if i % 3 == 0 else 0.1 for i, _ in enumerate(docs)]

    freshness._ask_router = ask
    web_index.retrieve = dense
    web_memory._lexical_candidates = lexical
    web_memory._page_meta = lambda urls, ids=(): {}
    web_memory._rank_candidates = rank
    web_memory._bump_retrieval = lambda ids: None
    rerank.score = score
    lk.claims_for = lambda q, limit=3: []
    lk._topical_precheck = lambda q: False  # the 92% of timeless turns with no page

    async def in_thread(fn, *a, **k):
        return await asyncio.to_thread(fn, *a, **k)

    db.run_in_thread = in_thread


def _time_one(question: str, *, router: str, rerank_s: float, with_history: bool) -> dict:
    from app import living_knowledge as lk

    stages: Dict[str, List[float]] = {}
    _install(stages, router=router, rerank_s=rerank_s)
    history = [
        {"role": "user", "content": "what does a used road bicycle cost?"},
        {"role": "assistant", "content": "Usually a few hundred dollars, depending on the frame."},
    ] if with_history else []

    async def go():
        started = time.perf_counter()
        prepared = await lk.prepare(
            question, effort="fast", mode="assistant", web_search_pref="off",
            allow_network=False, history=history,
        )
        return time.perf_counter() - started, prepared

    seconds, prepared = asyncio.run(go())
    return {
        "prepare_s": seconds,
        "decision": prepared.decision,
        "reason": prepared.verdict.reason if prepared.verdict else "",
        "sources": len(prepared.sources),
        "router_calls": len(stages.get("router", [])),
        "retrievals": len(stages.get("embed+dense", [])),
        "reranks": len(stages.get("rerank", [])),
        "rerank_docs": sum(stages.get("rerank_docs", [])),
    }


def _pct(values: List[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--label", default="tree")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    from app.config import settings

    variants = [("default", {})]
    if hasattr(settings, "knowledge_fast_speculative_salvage"):
        variants = [("salvage_on", {"knowledge_fast_speculative_salvage": True}),
                    ("salvage_off", {"knowledge_fast_speculative_salvage": False})]
    rows = []
    for variant, overrides in variants:
        for name, value in overrides.items():
            setattr(settings, name, value)
        for profile, router, rerank_s in (
            ("router_ok_warm", "ok", LATENCY["rerank_warm"]),
            ("router_timeout_warm", "timeout", LATENCY["rerank_warm"]),
            ("router_timeout_cold", "timeout", LATENCY["rerank_cold"]),
        ):
            for question, _word, with_history in QUESTIONS:
                runs = [_time_one(question, router=router, rerank_s=rerank_s, with_history=with_history)
                        for _ in range(args.runs)]
                times = [r["prepare_s"] for r in runs]
                last = runs[-1]
                row = {
                    "tree": args.label, "variant": variant, "profile": profile, "question": question,
                    "history": with_history,
                    "p50_s": round(statistics.median(times), 3), "p95_s": round(_pct(times, 0.95), 3),
                    **{k: last[k] for k in ("decision", "reason", "sources", "router_calls", "retrievals", "reranks", "rerank_docs")},
                }
                rows.append(row)
                print(
                    f"{args.label:5} {variant:11} {profile:20} {question[:38]:38} {'hist' if with_history else '-':4} p50 {row['p50_s']:6.3f} "
                    f"p95 {row['p95_s']:6.3f}  router {row['router_calls']} retrieve {row['retrievals']} "
                    f"rerank {row['reranks']} ({int(row['rerank_docs'])} docs)  {row['decision']}/{row['reason']} "
                    f"sources {row['sources']}",
                    flush=True,
                )
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
