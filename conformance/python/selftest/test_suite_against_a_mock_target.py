"""The real suite, run as a subprocess against a local contract-shaped mock.

    python -m pytest selftest -p no:techsara_conformance.plugin

WHY (2026-09-13): each scenario here is a defect the adversarial review
proved by pointing the suite at a mock, kept as a regression test. The mock
(selftest/mock_target.py) needs no server, key or engine; the whole file runs
in a few seconds. The key below is a placeholder no server accepts.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from mock_target import Behaviour, MockTarget

SUITE = Path(__file__).resolve().parents[1]
PLACEHOLDER_KEY = "selftest-placeholder-key"


def run_suite(
    tmp_path: Path, target: MockTarget, tests: List[str], *, extra: Optional[List[str]] = None, keys: Optional[Dict[str, Any]] = None
) -> Tuple[int, Dict[str, Any], str]:
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps({"base_url": target.base_url, "api_key": PLACEHOLDER_KEY, **(keys or {})}))
    report = tmp_path / "report.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("TECHSARA_")}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, f"--keys-file={keys_file}", f"--conformance-report={report}", *(extra or [])],
        cwd=SUITE, env=env, capture_output=True, text=True, timeout=120,
    )
    data = json.loads(report.read_text()) if report.exists() else {}
    return proc.returncode, data, proc.stdout + proc.stderr


def rows(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {r["test"].split("::")[-1]: r for r in report.get("results", [])}


def test_a_limits_off_schema_with_a_hidden_429_fails_the_run_instead_of_waiting_it_out(tmp_path):
    with MockTarget(Behaviour(documents_429=False, models_429_first=1)) as target:
        code, report, out = run_suite(tmp_path, target, ["tests/test_models.py::test_no_ratelimit_headers_are_sent_when_limits_are_off"])
    results = rows(report)
    assert code == 1, out
    assert results["test_no_ratelimit_headers_are_sent_when_limits_are_off"]["result"] == "FAIL", results
    session = results["limits_off_held_for_the_whole_run"]
    assert session["result"] == "FAIL" and "429" in session["detail"], session
    assert report["limits_off_expected"] is True
    assert target.behaviour.calls["models"] == 1, "the 429 was not retried"


def test_a_ratelimit_header_on_any_route_fails_a_limits_off_run_even_when_every_test_passes(tmp_path):
    with MockTarget(Behaviour(documents_429=False, ratelimit_on_errors=True)) as target:
        code, report, out = run_suite(tmp_path, target, ["tests/test_models.py::test_retrieving_an_unknown_model_is_a_404_model_not_found_envelope"])
    results = rows(report)
    assert results["test_retrieving_an_unknown_model_is_a_404_model_not_found_envelope"]["result"] == "PASS", results
    assert results["limits_off_held_for_the_whole_run"]["result"] == "FAIL", results
    assert report["totals"].get("FAIL") == 1
    assert code == 1, out


def test_a_paced_heartbeating_stream_passes_the_silence_check_and_its_pings_are_counted(tmp_path):
    behaviour = Behaviour(documents_429=True, stream_prefill_s=3.0, ping_every_s=1.0)
    with MockTarget(behaviour) as target:
        code, report, out = run_suite(
            tmp_path, target,
            ["tests/test_long_request.py::test_a_long_stream_with_the_sdk_timeout_disabled_runs_to_its_budget_and_is_never_silent_on_the_wire"],
            extra=["--long-output-tokens=16"],
        )
    result = next(iter(rows(report).values()))
    assert code == 0 and result["result"] == "PASS", out
    note = result["notes"][0]
    assert "heartbeat comment line(s)" in note and not note.split(" heartbeat comment")[0].endswith(" 0"), note


def test_a_contract_shaped_rerank_answer_passes(tmp_path):
    with MockTarget(Behaviour()) as target:
        code, report, out = run_suite(
            tmp_path, target,
            ["tests/test_rerank.py::test_rerank_orders_documents_by_relevance_cut_to_top_n_with_documents"],
            extra=["--feature", "rerank=built"],
        )
    assert code == 0, out
    assert [r["result"] for r in report["results"]] == ["PASS"], report["results"]


def test_a_key_minted_without_a_built_features_scope_skips_with_the_reason_instead_of_failing(tmp_path):
    old_scopes = {"scopes": ["models.read", "responses.read", "responses.write"]}
    with MockTarget(Behaviour()) as target:
        code, report, out = run_suite(tmp_path, target, ["tests/test_rerank.py"], extra=["--feature", "rerank=built"], keys=old_scopes)
        strict_code, _, strict_out = run_suite(
            tmp_path, target, ["tests/test_rerank.py"], extra=["--feature", "rerank=built", "--strict-scopes"], keys=old_scopes
        )
    results = rows(report)
    assert code == 0, out
    ordered = results["test_rerank_orders_documents_by_relevance_cut_to_top_n_with_documents"]
    assert ordered["result"] == "SKIP" and "rerank.write" in ordered["detail"] and "provision_key" in ordered["detail"], ordered
    limited = results["test_rerank_without_the_scope_is_403"]
    assert limited["result"] == "SKIP" and "no limited key" in limited["detail"], "the limited-key test is not skipped for the MAIN key's scopes"
    assert strict_code == pytest.ExitCode.USAGE_ERROR and "rerank.write" in strict_out, strict_out


def test_a_ceiling_refusal_from_a_server_that_does_not_advertise_the_ceiling_proves_nothing(tmp_path):
    # The e2e build: no advertised ceiling, everything above 8,192 refused.
    with MockTarget(Behaviour(declares_output_ceiling=False)) as target:
        test = "tests/test_responses.py::test_max_output_tokens_one_above_the_advertised_ceiling_is_a_400_naming_it"
        planned_code, planned, _ = run_suite(tmp_path, target, [test])
        forced_code, forced, out = run_suite(tmp_path, target, [test], extra=["--feature", "output_ceiling=built"])
    assert planned_code == 0 and planned["results"][0]["result"] == "XFAIL", planned["results"]
    assert forced_code == 1 and forced["results"][0]["result"] == "FAIL", out
    assert "would prove nothing" in forced["results"][0]["detail"], forced["results"][0]


def test_the_one_million_ceiling_test_skips_when_the_main_long_gate_is_at_capacity(tmp_path):
    behaviour = Behaviour(declares_output_ceiling=True, refuse_above=1_000_000, at_capacity_from=131_072)
    with MockTarget(behaviour) as target:
        code, report, out = run_suite(
            tmp_path, target, ["tests/test_responses.py::test_max_output_tokens_of_one_million_is_accepted_and_clamped_not_refused"]
        )
    result = report["results"][0]
    assert code == 0 and result["result"] == "SKIP", out
    assert "main.long" in result["detail"] and "60" in result["detail"], result
