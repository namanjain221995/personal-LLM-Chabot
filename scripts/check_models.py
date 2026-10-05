#!/usr/bin/env python3
"""Are the models this pipeline depends on up, fast, and answering correctly?

Three checks per role, because "the model is fine" can mean three different
things and they fail independently:

  reachable   the endpoint serves, and serves the model the config names
  latency     a real call, timed, not a /models ping
  contract    the answer is the shape the pipeline needs

    python scripts/check_models.py
    python scripts/check_models.py --quick          # skip the contract checks
    python scripts/check_models.py --repeat 3       # latency over several calls

Run it from inside the orchestrator container, or anywhere that resolves the
vllm service names.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "brain/Salesforce-Org-Data-main/src"))

# Role -> (env var for the endpoint, fallback, env var for the model, fallback).
# The fallbacks are what the running stack actually serves, verified; the env
# vars let a different deployment answer for itself.
ROLES = {
    "main": ("OPENAI_BASE_URL", "http://vllm:8000/v1",
             "MAIN_MODEL", "Qwen/Qwen3.6-35B-A3B-NVFP4"),
    "router": ("ROUTER_BASE_URL", "http://vllm-router:30002/v1",
               "ROUTER_MODEL", "Qwen/Qwen3-VL-8B-Instruct-FP8"),
    "embed": ("EMBED_BASE_URL", "http://vllm-embed:30003/v1",
              "EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B"),
    "rerank": ("RERANK_BASE_URL", "http://vllm-reranker:30005/v1",
               "RERANKER_MODEL", "Qwen/Qwen3-Reranker-0.6B"),
}

# Which pipeline stage each role serves, so a failure names the consequence.
SERVES = {
    "main": "step 6 answer generation - MANDATORY, no fallback path",
    "router": "step 1 intent extraction - optional, lexical path degrades",
    "embed": "step 3 semantic discovery - optional, lexical path degrades",
    "rerank": "step 3 reranking - off by default, measured as a regression",
}

EXTRACTION_CASES = [
    ("How many internal interviews are scheduled?", "DATA", True),
    ("What fields does the Recruiter object have?", "METADATA", False),
    ("List the candidates placed last month", "DATA", True),
]


@dataclass
class RoleReport:
    role: str
    endpoint: str = ""
    expected_model: str = ""
    served: list[str] = dataclass_field(default_factory=list)
    roots: list[str] = dataclass_field(default_factory=list)
    reachable: bool = False
    model_matches: bool = False
    latencies_ms: list[float] = dataclass_field(default_factory=list)
    contract_ok: int = 0
    contract_total: int = 0
    notes: list[str] = dataclass_field(default_factory=list)

    @property
    def median_ms(self) -> float | None:
        return round(statistics.median(self.latencies_ms), 1) if self.latencies_ms else None

    @property
    def fatal(self) -> bool:
        """Only the main model has no fallback, so only it can fail the run."""
        return self.role == "main" and not (self.reachable and self.model_matches)


def _env(name: str, fallback: str) -> str:
    import os
    return os.environ.get(name) or fallback


def _api_root(url: str) -> str:
    """The OpenAI-compatible API root, whether or not /v1 was configured.

    RERANK_BASE_URL is set without it in this deployment while the others carry
    it, so a checker that trusts the variable reports a healthy service as
    down.
    """
    trimmed = url.rstrip("/")
    return trimmed if trimmed.endswith("/v1") else f"{trimmed}/v1"


def probe(report: RoleReport, client) -> None:
    try:
        response = client.get(f"{report.endpoint}/models", timeout=8)
        entries = response.json().get("data") or []
        report.served = [m["id"] for m in entries]
        report.roots = [m.get("root") for m in entries if m.get("root")]
        report.reachable = True
    except Exception as exc:                            # noqa: BLE001
        report.notes.append(f"unreachable: {type(exc).__name__}")
        return
    # A model may be configured by repository id or by the local path it was
    # loaded from; vLLM reports the first as `id` and the second as `root`.
    # Either naming is the same model, and calling that a mismatch would send
    # somebody looking for a fault that is not there.
    report.model_matches = (report.expected_model in report.served
                            or report.expected_model in report.roots)
    if report.model_matches and report.expected_model not in report.served:
        report.expected_model = report.served[0]
        report.notes.append("configured by local path; serving id used instead")
    elif not report.model_matches:
        report.notes.append(
            f"serves {report.served} but the config names "
            f"{report.expected_model!r}")


def time_chat(report: RoleReport, client, repeat: int) -> None:
    """A real completion, thinking disabled, so the number means something.

    A /models ping measures the HTTP stack. This measures the thing a request
    waits for.
    """
    body = {"model": report.expected_model,
            "messages": [{"role": "user", "content": "Reply with the word ok."}],
            "max_tokens": 8, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False}}
    for _ in range(repeat):
        started = time.perf_counter()
        try:
            response = client.post(
                f"{report.endpoint}/chat/completions",
                json=body, timeout=120)
            if response.status_code != 200:
                report.notes.append(f"chat HTTP {response.status_code}")
                return
        except Exception as exc:                        # noqa: BLE001
            report.notes.append(f"chat failed: {type(exc).__name__}")
            return
        report.latencies_ms.append((time.perf_counter() - started) * 1000)


def time_embed(report: RoleReport, client, repeat: int) -> None:
    body = {"model": report.expected_model, "input": "internal interview"}
    for _ in range(repeat):
        started = time.perf_counter()
        try:
            response = client.post(f"{report.endpoint}/embeddings",
                                   json=body, timeout=60)
            if response.status_code != 200:
                report.notes.append(f"embeddings HTTP {response.status_code}")
                return
            vector = response.json()["data"][0]["embedding"]
        except Exception as exc:                        # noqa: BLE001
            report.notes.append(f"embeddings failed: {type(exc).__name__}")
            return
        report.latencies_ms.append((time.perf_counter() - started) * 1000)
    if report.latencies_ms:
        report.notes.append(f"{len(vector)} dimensions")


def time_rerank(report: RoleReport, client, repeat: int) -> None:
    """A reranker has no /chat/completions. It scores documents against a query.

    `graphrag.rerank` posts to /rerank, so that is what gets timed -- checking
    a path the pipeline never calls would report a healthy service as broken,
    or the reverse.
    """
    body = {"model": report.expected_model, "query": "internal interview",
            "documents": ["Internal_Interview__c - interview records",
                          "Account - billing address and phone"]}
    for _ in range(repeat):
        started = time.perf_counter()
        try:
            response = client.post(f"{report.endpoint}/rerank", json=body,
                                   timeout=60)
            if response.status_code != 200:
                report.notes.append(f"rerank HTTP {response.status_code}")
                return
            results = response.json().get("results") or []
        except Exception as exc:                        # noqa: BLE001
            report.notes.append(f"rerank failed: {type(exc).__name__}")
            return
        report.latencies_ms.append((time.perf_counter() - started) * 1000)
    if results:
        top = max(results, key=lambda r: r.get("relevance_score", 0))
        report.notes.append(
            f"ranked the interview document at index {top.get('index')} "
            f"(score {top.get('relevance_score')})")


def check_extraction(report: RoleReport) -> None:
    """Does the router return the JSON contract step 2 routes on?

    Not an evaluation. It asks whether the three routing booleans and the
    entity list come back at all -- a model that returns prose here does not
    fail loudly, it silently sends every question down the lexical path.
    """
    from graphrag.extract import extract
    from graphrag.routing import route_for
    for question, expected_type, expects_records in EXTRACTION_CASES:
        report.contract_total += 1
        result = extract(question, endpoint=report.endpoint,
                         model=report.expected_model)
        if result is None:
            report.notes.append(f"no extraction for {question!r}")
            continue
        problems = []
        if result.request_type != expected_type:
            problems.append(f"request_type={result.request_type} "
                            f"(expected {expected_type})")
        if bool(result.requires_record_query) is not expects_records:
            problems.append(f"requires_record_query="
                            f"{result.requires_record_query}")
        if not result.business_entities:
            problems.append("named no business entities")
        if not result.model:
            problems.append("did not record its own model")
        if problems:
            report.notes.append(f"{question[:36]!r}: " + "; ".join(problems))
        else:
            report.contract_ok += 1
            report.notes.append(
                f"{question[:36]!r} -> {route_for(result).value} "
                f"({result.duration_ms} ms, mode={result.mode})")


def check_answer(report: RoleReport) -> None:
    """Does the main model produce a GROUNDED answer, not just an answer?

    A reachable model that hallucinates is a worse failure than one that is
    down, because the pipeline returns it. This runs the real generator and the
    real grounding validator over a fixed verified result.
    """
    from answer.config import AnswerConfig
    from answer.service import AnswerService

    class Freshness:
        status, age_minutes, last_sync_at = "fresh", 17.0, None

    class Result:
        success, rows = True, [{"record_count": 491}]
        row_count, total_count, truncated = 1, 1, False
        execution_ms, error, error_detail, sql = 0.7, None, "", ""
        freshness = Freshness()
        trace: list = []

    class SelectItem:
        output_alias, aggregate = "record_count", "COUNT"

    class Plan:
        operation = type("O", (), {"value": "count"})()
        select, group_by = [SelectItem()], []

    class Grounded:
        primary_object = "Internal_Interview__c"

    config = AnswerConfig(endpoint=report.endpoint,
                          model=report.expected_model)
    report.contract_total += 1
    started = time.perf_counter()
    final = AnswerService(config=config).answer(
        "How many internal interviews are scheduled?", Result(),
        grounded_plan=Grounded(), query_plan=Plan())
    elapsed = (time.perf_counter() - started) * 1000

    if final.fallback_used:
        report.notes.append(
            f"FELL BACK to the deterministic renderer: {final.failure_detail}")
    elif not final.grounded:
        report.notes.append("answer was not grounded")
    elif "491" not in final.text:
        report.notes.append(f"lost the verified number: {final.text[:80]!r}")
    else:
        report.contract_ok += 1
        report.notes.append(
            f"grounded in {final.attempts} attempt(s), {int(elapsed)} ms, "
            f"regenerated={final.regenerated}")
        report.notes.append(f"answer: {final.text.splitlines()[0][:72]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true",
                        help="reachability and latency only")
    parser.add_argument("--repeat", type=int, default=1,
                        help="latency samples per role")
    parser.add_argument("--role", action="append", dest="roles", default=None,
                        choices=sorted(ROLES))
    args = parser.parse_args(argv)

    try:
        import httpx
    except ImportError:
        print("error: httpx is not installed", file=sys.stderr)
        return 1

    wanted = args.roles or list(ROLES)
    reports: list[RoleReport] = []
    with httpx.Client() as client:
        for role in wanted:
            endpoint_var, endpoint_default, model_var, model_default = ROLES[role]
            report = RoleReport(role=role,
                                endpoint=_api_root(_env(endpoint_var,
                                                        endpoint_default)),
                                expected_model=_env(model_var, model_default))
            probe(report, client)
            if report.reachable and report.model_matches:
                timer = {"embed": time_embed, "rerank": time_rerank}.get(
                    role, time_chat)
                timer(report, client, max(1, args.repeat))
            reports.append(report)

    if not args.quick:
        for report in reports:
            if not (report.reachable and report.model_matches):
                continue
            try:
                if report.role == "router":
                    check_extraction(report)
                elif report.role == "main":
                    check_answer(report)
            except Exception as exc:                    # noqa: BLE001
                report.notes.append(f"contract check errored: "
                                    f"{type(exc).__name__}: {exc}")

    print(f"{'role':8} {'status':9} {'median ms':>10}  model")
    print("-" * 78)
    for report in reports:
        if not report.reachable:
            status = "DOWN"
        elif not report.model_matches:
            status = "MISMATCH"
        elif report.contract_total and report.contract_ok < report.contract_total:
            status = "DEGRADED"
        else:
            status = "ok"
        median = "" if report.median_ms is None else f"{report.median_ms:,.1f}"
        print(f"{report.role:8} {status:9} {median:>10}  "
              f"{report.expected_model}")

    for report in reports:
        print(f"\n{report.role}  {report.endpoint}")
        print(f"  serves : {SERVES[report.role]}")
        if report.contract_total:
            print(f"  contract : {report.contract_ok}/{report.contract_total}")
        if report.latencies_ms and len(report.latencies_ms) > 1:
            print(f"  latency : " + ", ".join(
                f"{v:,.0f}" for v in report.latencies_ms) + " ms")
        for note in report.notes:
            print(f"  - {note}")

    fatal = [r.role for r in reports if r.fatal]
    if fatal:
        print(f"\nFAIL: {', '.join(fatal)} unavailable and has no fallback")
        return 1
    degraded = [r.role for r in reports
                if not (r.reachable and r.model_matches) or
                (r.contract_total and r.contract_ok < r.contract_total)]
    if degraded:
        print(f"\nDEGRADED: {', '.join(degraded)} - the pipeline still answers, "
              f"with less precision")
        return 0
    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
