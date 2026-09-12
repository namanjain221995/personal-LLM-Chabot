"""Approved-only smoke runner for the Salesforce evaluation dataset.

The candidate application receives only the user-facing request plus a stable
case id. Golden expectations are joined offline after the completed trace is
retrieved through the authenticated diagnostics endpoint.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Sequence, Tuple

import httpx

from evaluation.evaluators import evaluate_case
from evaluation.loader import EvaluationCase, EvaluationDataset, load_dataset


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = ROOT / "evaluation" / "datasets" / "salesforce_eval_v1.yaml"
DEFAULT_OUTPUT_DIR = ROOT / "evaluation" / "reports"


class RunnerError(RuntimeError):
    pass


def select_cases(
    dataset: EvaluationDataset,
    *,
    case_ids: Sequence[str] = (),
    allow_draft: bool = False,
    limit: int = 10,
) -> list[EvaluationCase]:
    """Select cases without accidentally turning a draft bank into live traffic."""
    if limit < 1:
        raise RunnerError("--limit must be at least 1")
    if case_ids:
        requested: list[EvaluationCase] = []
        for case_id in case_ids:
            try:
                requested.append(dataset.by_id(case_id))
            except KeyError as exc:
                raise RunnerError(f"unknown case id: {case_id}") from exc
        drafts = [case.id for case in requested if not case.runnable]
        if drafts and not allow_draft:
            raise RunnerError(
                "refusing unapproved case(s): "
                + ", ".join(drafts)
                + "; review them or pass --allow-draft explicitly"
            )
        candidates = requested
    else:
        candidates = dataset.cases if allow_draft else list(dataset.runnable_cases())
    selected = candidates[:limit]
    if not selected:
        raise RunnerError(
            "no runnable cases: the dataset is still draft; approve individual "
            "cases before smoke testing (or deliberately pass --allow-draft)"
        )
    return selected


def iter_sse(lines: Iterable[str]) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Parse the small SSE subset used by /chat, including multiline data."""
    event_name = "message"
    data_lines: list[str] = []

    def dispatch() -> Tuple[str, Dict[str, Any]] | None:
        nonlocal event_name, data_lines
        if not data_lines:
            event_name = "message"
            return None
        raw = "\n".join(data_lines)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RunnerError(f"invalid JSON in SSE {event_name!r} event") from exc
        if not isinstance(payload, dict):
            raise RunnerError(f"SSE {event_name!r} data must be a JSON object")
        item = (event_name, payload)
        event_name = "message"
        data_lines = []
        return item

    for raw_line in lines:
        line = raw_line.rstrip("\r")
        if not line:
            item = dispatch()
            if item is not None:
                yield item
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    item = dispatch()
    if item is not None:
        yield item


def run_case(client: httpx.Client, case: EvaluationCase) -> Dict[str, Any]:
    request_payload = case.application_request()
    answer_parts: list[str] = []
    response_meta: Dict[str, Any] = {}
    terminal = ""

    with client.stream("POST", "/chat", json=request_payload) as response:
        response.raise_for_status()
        for event_name, payload in iter_sse(response.iter_lines()):
            if event_name == "token" and isinstance(payload.get("text"), str):
                answer_parts.append(payload["text"])
            elif event_name == "meta":
                response_meta.update(payload)
            elif event_name == "error":
                raise RunnerError(
                    f"{case.id}: application error: "
                    f"{payload.get('message') or payload.get('code') or 'unknown error'}"
                )
            elif event_name == "done":
                terminal = "done"

    if terminal != "done":
        raise RunnerError(f"{case.id}: SSE stream ended without a done event")
    trace_id = str(response_meta.get("trace_id") or "")
    if not trace_id:
        raise RunnerError(f"{case.id}: final SSE metadata did not include trace_id")

    response = client.get(f"/chat/trace/{trace_id}")
    response.raise_for_status()
    trace = response.json()
    if not isinstance(trace, dict):
        raise RunnerError(f"{case.id}: trace endpoint did not return an object")
    if trace.get("test_case_id") != case.id:
        raise RunnerError(
            f"{case.id}: trace join mismatch (got {trace.get('test_case_id')!r})"
        )
    if trace.get("final_status") == "running":
        raise RunnerError(f"{case.id}: trace is still running after the done event")

    result = evaluate_case(case, trace)
    return {
        "case_id": case.id,
        "request": request_payload,
        "response": {
            "answer": "".join(answer_parts),
            "meta": response_meta,
        },
        "trace": trace,
        "evaluation": result.wire(),
    }


def summarize(cases: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    passed = sum(bool(item["evaluation"]["passed"]) for item in cases)
    stage_statuses: Counter[str] = Counter()
    first_incorrect: Counter[str] = Counter()
    for item in cases:
        evaluation = item["evaluation"]
        first = evaluation.get("first_incorrect_stage")
        if first:
            first_incorrect[str(first)] += 1
        for check in evaluation.get("checks") or []:
            stage_statuses[f"{check['stage']}:{check['status']}"] += 1
    total = len(cases)
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "strict_accuracy": passed / total if total else 0.0,
        "stage_statuses": dict(sorted(stage_statuses.items())),
        "first_incorrect_stage": dict(sorted(first_incorrect.items())),
    }


def build_report(
    dataset: EvaluationDataset,
    case_runs: Sequence[Dict[str, Any]],
    *,
    started_at: str,
    completed_at: str,
    base_url: str,
    allow_draft: bool,
) -> Dict[str, Any]:
    return {
        "run_id": "salesforce_eval_" + completed_at.replace(":", "").replace("-", ""),
        "dataset": {
            "id": dataset.metadata.get("id"),
            "version": dataset.version,
            "path": str(dataset.path),
        },
        "started_at": started_at,
        "completed_at": completed_at,
        "base_url": base_url,
        "selection_policy": "draft_override" if allow_draft else "approved_only",
        "summary": summarize(case_runs),
        "cases": list(case_runs),
    }


def write_report(report: Dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = str(report["completed_at"]).replace(":", "").replace("-", "")
    path = output_dir / f"salesforce_eval_{timestamp}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("EVALUATION_BASE_URL", "http://127.0.0.1:8080"),
    )
    parser.add_argument(
        "--cookie",
        default=os.environ.get("EVALUATION_SESSION_COOKIE", ""),
        help="raw authenticated Cookie header; prefer EVALUATION_SESSION_COOKIE",
    )
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--allow-draft",
        action="store_true",
        help="deliberately permit unreviewed cases (off by default)",
    )
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not args.cookie:
            raise RunnerError(
                "authentication is required to retrieve traces; set "
                "EVALUATION_SESSION_COOKIE to the browser's raw Cookie header"
            )
        dataset = load_dataset(args.dataset)
        selected = select_cases(
            dataset,
            case_ids=args.case_id,
            allow_draft=args.allow_draft,
            limit=args.limit,
        )
        started_at = _utc_now()
        headers = {"Cookie": args.cookie, "Accept": "text/event-stream"}
        case_runs: list[Dict[str, Any]] = []
        with httpx.Client(
            base_url=args.base_url.rstrip("/"),
            headers=headers,
            timeout=args.timeout,
        ) as client:
            for case in selected:
                print(f"running {case.id}", file=sys.stderr)
                case_runs.append(run_case(client, case))
        completed_at = _utc_now()
        report = build_report(
            dataset,
            case_runs,
            started_at=started_at,
            completed_at=completed_at,
            base_url=args.base_url,
            allow_draft=args.allow_draft,
        )
        path = write_report(report, args.output_dir)
        print(path)
        return 0 if report["summary"]["failed"] == 0 else 1
    except (RunnerError, httpx.HTTPError, OSError, ValueError) as exc:
        print(f"evaluation runner: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
