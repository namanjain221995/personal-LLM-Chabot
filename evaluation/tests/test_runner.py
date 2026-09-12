import json
from pathlib import Path

import httpx
import pytest

from evaluation.loader import EvaluationCase, EvaluationDataset
from evaluation.runners.evaluation_runner import (
    RunnerError,
    build_report,
    iter_sse,
    run_case,
    select_cases,
    summarize,
    write_report,
)


def _case(case_id="SF-RUN-001", status="approved"):
    return EvaluationCase(
        {
            "id": case_id,
            "question": "How many candidates were placed last month?",
            "review_status": status,
            "request": {"selected_mode": "salesforce", "conversation_history": []},
            "expected": {
                "mode": "salesforce",
                "intent": "record_count",
                "entities": {
                    "objects": {"required": [], "forbidden": []},
                    "fields": {"required": [], "forbidden": []},
                },
                "filters": [],
                "source": {"type": "live_salesforce_records", "environment": "preprod"},
                "plan": {
                    "operation": "count",
                    "query_language": "SOQL_or_multi_query_plan",
                    "requires_record_query": True,
                },
                "answer": {"type": "oracle_result"},
                "provenance": {
                    "required": True,
                    "expected_source_label": "live_salesforce_records",
                    "expected_environment": "preprod",
                    "must_indicate_freshness": True,
                },
            },
            "grading": {"critical_checks": ["mode", "provenance"]},
        }
    )


def _dataset(*cases):
    return EvaluationDataset(
        path=Path("dataset.yaml"),
        version=1,
        metadata={"id": "test"},
        comparison_policy={},
        cases=list(cases),
    )


def test_select_cases_refuses_drafts_by_default():
    dataset = _dataset(_case(status="draft"))
    with pytest.raises(RunnerError, match="no runnable cases"):
        select_cases(dataset)
    with pytest.raises(RunnerError, match="refusing unapproved"):
        select_cases(dataset, case_ids=["SF-RUN-001"])
    assert select_cases(dataset, allow_draft=True)[0].id == "SF-RUN-001"


def test_sse_parser_handles_comments_and_multiline_json():
    events = list(
        iter_sse(
            [
                ": keepalive",
                "event: meta",
                'data: {"trace_id":',
                'data: "trace-1"}',
                "",
            ]
        )
    )
    assert events == [("meta", {"trace_id": "trace-1"})]


def test_run_case_sends_no_golden_data_and_joins_trace_offline():
    case = _case()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/chat":
            body = json.loads(request.content)
            assert set(body) == {"message", "mode", "messages", "test_case_id"}
            assert "expected" not in body
            assert body["test_case_id"] == case.id
            stream = (
                'event: meta\ndata: {"trace_id":"trace-1","request_id":"req-1"}\n\n'
                'event: token\ndata: {"text":"There were 4."}\n\n'
                'event: meta\ndata: {"trace_id":"trace-1","route":"sql"}\n\n'
                'event: done\ndata: {"session_id":"default"}\n\n'
            )
            return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})
        if request.method == "GET" and request.url.path == "/chat/trace/trace-1":
            return httpx.Response(
                200,
                json={
                    "trace_id": "trace-1",
                    "test_case_id": case.id,
                    "resolved_mode": "salesforce",
                    "final_status": "ok",
                    "provenance": {
                        "source": "live_salesforce_records",
                        "environment": "preprod",
                        "freshness": "live",
                    },
                    "events": [],
                },
            )
        return httpx.Response(404)

    with httpx.Client(
        base_url="http://testserver", transport=httpx.MockTransport(handler)
    ) as client:
        result = run_case(client, case)

    assert result["response"]["answer"] == "There were 4."
    assert result["evaluation"]["passed"] is True
    assert result["trace"]["test_case_id"] == case.id


def test_trace_join_mismatch_is_rejected():
    case = _case()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                text=(
                    'event: meta\ndata: {"trace_id":"trace-1"}\n\n'
                    'event: done\ndata: {}\n\n'
                ),
            )
        return httpx.Response(
            200,
            json={"test_case_id": "another-case", "final_status": "ok"},
        )

    with httpx.Client(
        base_url="http://testserver", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(RunnerError, match="trace join mismatch"):
            run_case(client, case)


def test_summary_and_report_writer(tmp_path):
    run = {
        "evaluation": {
            "passed": False,
            "first_incorrect_stage": "intent",
            "checks": [{"stage": "intent", "status": "not_evaluable"}],
        }
    }
    summary = summarize([run])
    assert summary["strict_accuracy"] == 0.0
    assert summary["stage_statuses"] == {"intent:not_evaluable": 1}
    report = build_report(
        _dataset(_case()),
        [run],
        started_at="2026-09-12T10:00:00+00:00",
        completed_at="2026-09-12T10:01:00+00:00",
        base_url="http://testserver",
        allow_draft=False,
    )
    path = write_report(report, tmp_path)
    assert json.loads(path.read_text(encoding="utf-8"))["summary"]["failed"] == 1
