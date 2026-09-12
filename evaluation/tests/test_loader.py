from pathlib import Path

import pytest
import yaml

from evaluation.loader import DatasetError, load_dataset


DATASET = Path(__file__).resolve().parents[1] / "datasets" / "salesforce_eval_v1.yaml"


def test_loads_all_cases_and_keeps_drafts_disabled():
    dataset = load_dataset(DATASET)
    assert dataset.metadata["id"] == "salesforce_eval_v1"
    assert len(dataset.cases) == 78
    assert len({case.id for case in dataset.cases}) == 78
    assert list(dataset.runnable_cases()) == []


def test_application_request_never_contains_golden_expectations():
    case = load_dataset(DATASET).by_id("SF-DATA-006")
    request = case.application_request()
    assert request["test_case_id"] == "SF-DATA-006"
    assert request["mode"] == "salesforce"
    assert request["message"] == case.question
    assert "expected" not in request
    assert "reference_response" not in request


def test_approved_or_explicitly_enabled_cases_are_runnable(tmp_path):
    payload = yaml.safe_load(DATASET.read_text(encoding="utf-8"))
    payload["cases"] = payload["cases"][:2]
    payload["dataset"]["case_count"] = 2
    payload["cases"][0]["review_status"] = "approved"
    payload["cases"][1]["enabled"] = True
    path = tmp_path / "approved.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    assert [case.id for case in load_dataset(path).runnable_cases()] == [
        payload["cases"][0]["id"], payload["cases"][1]["id"]
    ]


def test_duplicate_ids_are_rejected(tmp_path):
    payload = yaml.safe_load(DATASET.read_text(encoding="utf-8"))
    payload["cases"] = payload["cases"][:2]
    payload["cases"][1]["id"] = payload["cases"][0]["id"]
    payload["dataset"]["case_count"] = 2
    path = tmp_path / "duplicate.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(DatasetError, match="duplicate case ids"):
        load_dataset(path)


def test_declared_case_count_must_match(tmp_path):
    payload = yaml.safe_load(DATASET.read_text(encoding="utf-8"))
    payload["cases"] = payload["cases"][:1]
    path = tmp_path / "bad-count.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(DatasetError, match="case_count"):
        load_dataset(path)
