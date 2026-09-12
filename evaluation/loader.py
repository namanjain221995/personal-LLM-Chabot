"""Strict loader for the Salesforce golden evaluation dataset."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List

import yaml


class DatasetError(ValueError):
    pass


_EXPECTED_KEYS = {
    "mode", "intent", "entities", "filters", "source", "plan", "answer", "provenance"
}
_ANSWER_TYPES = {"fixed_reference", "oracle_result", "policy_reference"}
_APPROVED = {"approved", "reviewed", "enabled"}


@dataclass(frozen=True)
class EvaluationCase:
    raw: Dict[str, Any]

    @property
    def id(self) -> str:
        return self.raw["id"]

    @property
    def question(self) -> str:
        return self.raw["question"]

    @property
    def expected(self) -> Dict[str, Any]:
        return self.raw["expected"]

    @property
    def review_status(self) -> str:
        return str(self.raw.get("review_status") or "draft").strip().lower()

    @property
    def runnable(self) -> bool:
        if self.raw.get("enabled") is False:
            return False
        return self.raw.get("enabled") is True or self.review_status in _APPROVED

    def application_request(self) -> Dict[str, Any]:
        """Only inputs the candidate application may see—never expectations."""
        request = self.raw.get("request") or {}
        return {
            "message": self.question,
            "mode": request.get("selected_mode", self.expected["mode"]),
            "test_case_id": self.id,
            "messages": list(request.get("conversation_history") or []),
        }


@dataclass(frozen=True)
class EvaluationDataset:
    path: Path
    version: int
    metadata: Dict[str, Any]
    comparison_policy: Dict[str, Any]
    cases: List[EvaluationCase]

    def by_id(self, case_id: str) -> EvaluationCase:
        for case in self.cases:
            if case.id == case_id:
                return case
        raise KeyError(case_id)

    def runnable_cases(self) -> Iterator[EvaluationCase]:
        return (case for case in self.cases if case.runnable)


def _mapping(value: Any, where: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise DatasetError(f"{where} must be a mapping")
    return value


def _validate_case(raw: Any, index: int) -> EvaluationCase:
    case = _mapping(raw, f"cases[{index}]")
    for key in ("id", "category", "question", "request", "expected", "grading"):
        if key not in case:
            raise DatasetError(f"cases[{index}] is missing {key}")
    if not isinstance(case["id"], str) or not case["id"].strip():
        raise DatasetError(f"cases[{index}].id must be a non-empty string")
    if not isinstance(case["question"], str) or not case["question"].strip():
        raise DatasetError(f"{case['id']}.question must be a non-empty string")
    expected = _mapping(case["expected"], f"{case['id']}.expected")
    missing = _EXPECTED_KEYS - set(expected)
    if missing:
        raise DatasetError(f"{case['id']}.expected is missing {sorted(missing)}")
    answer = _mapping(expected["answer"], f"{case['id']}.expected.answer")
    if answer.get("type") not in _ANSWER_TYPES:
        raise DatasetError(f"{case['id']} has unsupported answer type {answer.get('type')!r}")
    critical = _mapping(case["grading"], f"{case['id']}.grading").get("critical_checks")
    if not isinstance(critical, list) or not critical:
        raise DatasetError(f"{case['id']}.grading.critical_checks must be a non-empty list")
    return EvaluationCase(case)


def load_dataset(path: str | Path) -> EvaluationDataset:
    source = Path(path)
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DatasetError(f"could not load {source}: {exc}") from exc
    root = _mapping(payload, str(source))
    metadata = _mapping(root.get("dataset"), "dataset")
    raw_cases = root.get("cases")
    if not isinstance(raw_cases, list):
        raise DatasetError("cases must be a list")
    cases = [_validate_case(raw, index) for index, raw in enumerate(raw_cases)]
    ids = [case.id for case in cases]
    duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicates:
        raise DatasetError(f"duplicate case ids: {duplicates}")
    declared = metadata.get("case_count")
    if declared is not None and int(declared) != len(cases):
        raise DatasetError(f"dataset.case_count={declared}, but found {len(cases)} cases")
    return EvaluationDataset(
        path=source,
        version=int(root.get("version") or 1),
        metadata=metadata,
        comparison_policy=_mapping(root.get("comparison_policy") or {}, "comparison_policy"),
        cases=cases,
    )
