"""Score schema discovery against the golden dataset, offline.

The SSE runner measures the whole application: it signs in, opens a stream,
waits for a model to finish writing, then reads the trace back out of
PostgreSQL. One case cost 132 seconds. That is the right tool for judging an
end-to-end answer and the wrong one for judging whether "placed" resolves to
the correct field, which is a question about retrieval alone.

This runner posts to the knowledge service instead. No login, no stream, no
model, no database. Every case in the dataset, in seconds, so a lexicon or
ranking change can be measured immediately rather than at the end of an hour.

It scores retrieval ONLY. Mode, plan, answer and provenance still belong to
the SSE runner; nothing here replaces it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import httpx

from evaluation.evaluators.discovery import DEFAULT_K, evaluate_discovery, summarise
from evaluation.loader import EvaluationCase, load_dataset

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = ROOT / "evaluation" / "datasets" / "salesforce_eval_v1.yaml"
DEFAULT_OUTPUT_DIR = ROOT / "evaluation" / "reports"
DEFAULT_SERVICE = os.environ.get("KNOWLEDGE_SERVICE_URL", "http://127.0.0.1:8006")


class RunnerError(RuntimeError):
    pass


def _select(dataset: Any, case_ids: List[str], limit: int,
            allow_draft: bool) -> List[EvaluationCase]:
    if case_ids:
        cases = [dataset.by_id(cid) for cid in case_ids]
    else:
        cases = list(dataset.cases if allow_draft else dataset.runnable_cases())
    if not cases:
        raise RunnerError(
            "no runnable cases. Every case is still a draft; mark a batch "
            "`review_status: approved`, or pass --allow-draft to score "
            "retrieval during harness development.")
    drafts = [c for c in cases if not c.runnable]
    if drafts and not allow_draft:
        raise RunnerError(
            f"{len(drafts)} selected case(s) are drafts; pass --allow-draft "
            f"to include them: {', '.join(c.id for c in drafts[:5])}")
    return cases[:limit] if limit else cases


def _discover(client: httpx.Client, service: str, case: EvaluationCase,
              k: int, semantic: bool, rerank: bool) -> Dict[str, Any]:
    response = client.post(
        f"{service.rstrip('/')}/discover",
        json={
            # Only the question travels. The expected objects, fields and
            # filters stay on this side of the boundary.
            "query": case.question,
            "limit": max(k, 10),
            "semantic": semantic,
            "rerank": rerank,
        },
        timeout=120.0)
    if response.status_code != 200:
        raise RunnerError(
            f"{service}/discover returned {response.status_code}: {response.text[:200]}")
    return response.json()


def run(dataset_path: str, *, service: str, case_ids: List[str], limit: int,
        allow_draft: bool, k: int, semantic: bool, rerank: bool,
        output_dir: str) -> Dict[str, Any]:
    dataset = load_dataset(dataset_path)
    cases = _select(dataset, case_ids, limit, allow_draft)

    results: List[Dict[str, Any]] = []
    started = time.perf_counter()
    with httpx.Client() as client:
        health = client.get(f"{service.rstrip('/')}/health", timeout=15.0)
        if health.status_code != 200:
            raise RunnerError(f"{service}/health returned {health.status_code}")
        service_health = health.json()

        for case in cases:
            case_started = time.perf_counter()
            discovery = _discover(client, service, case, k, semantic, rerank)
            checks = evaluate_discovery(case.raw, discovery, k)
            critical = [c for c in checks if c.status == "failed"]
            results.append({
                "case_id": case.id,
                "question": case.question,
                "passed": not critical,
                "first_incorrect_stage": critical[0].stage if critical else None,
                "checks": [c.wire() for c in checks],
                "asked": [q.get("surface") for q in discovery.get("questions") or []],
                "took_ms": int((time.perf_counter() - case_started) * 1000),
            })
            status = "ok  " if not critical else "FAIL"
            print(f"  {status} {case.id:16s} {case.question[:56]}")

    summary = summarise(results, k)
    summary["service_health"] = service_health
    summary["signals"] = {"semantic": semantic, "rerank": rerank}
    summary["strict_passed"] = sum(1 for r in results if r["passed"])
    summary["total_seconds"] = round(time.perf_counter() - started, 1)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%z")
    report = {
        "run_id": f"discovery_{stamp}",
        "dataset": str(Path(dataset_path).resolve()),
        "service": service,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "cases": results,
    }
    target = Path(output_dir) / f"discovery_{stamp}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    report["report_path"] = str(target)
    return report


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="discovery_runner",
        description="Score schema discovery against the golden dataset.")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--case-id", action="append", default=[], dest="case_ids")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--allow-draft", action="store_true")
    parser.add_argument("-k", type=int, default=DEFAULT_K,
                        help="how many ranked results count as retrieved")
    parser.add_argument("--no-semantic", action="store_true")
    parser.add_argument("--rerank", action="store_true")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args(argv)

    try:
        report = run(args.dataset, service=args.service, case_ids=args.case_ids,
                     limit=args.limit, allow_draft=args.allow_draft, k=args.k,
                     semantic=not args.no_semantic, rerank=args.rerank,
                     output_dir=args.output_dir)
    except (RunnerError, httpx.HTTPError, OSError, ValueError) as exc:
        print(f"discovery runner: {exc}", file=sys.stderr)
        return 2

    s = report["summary"]
    print(f"\n  {s['cases']} cases in {s['total_seconds']}s  "
          f"(semantic={s['signals']['semantic']}, rerank={s['signals']['rerank']})")
    for stage in ("objects", "fields", "clarification"):
        block = s[stage]
        line = (f"  {stage:14s} {block['passed']}/{block['evaluated']} passed"
                f"   not_evaluable {block['not_evaluable']}")
        recall = block.get(f"mean_recall_at_{s['k']}")
        if recall is not None:
            line += f"   recall@{s['k']} {recall}   MRR {block['mrr']}"
        print(line)
    print(f"  strict (all checks pass): {s['strict_passed']}/{s['cases']}")
    print(f"  report: {report['report_path']}")
    return 0 if s["strict_passed"] == s["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
