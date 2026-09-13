"""Capture tests/test_context_assembly_golden.py's fixtures from a tree whose
prompt is KNOWN right. Not collected by the suite (no test_ prefix); run it on
purpose, by path, against a `git archive` of that tree — never against the
working tree whose change is under test, or the golden compares a change with
itself (the gap this directory closes, 2026-09-13).

Recipe used for the checked-in fixtures (2026-09-13; orchestrator tree aefd714,
commits 02b509f and 82265d5 carry the same tree):

    BASE=$(mktemp -d)
    git archive 82265d5 orchestrator | tar -x -C "$BASE"
    cp orchestrator/tests/test_context_assembly_golden.py "$BASE/orchestrator/tests/"
    mkdir -p "$BASE/orchestrator/tests/fixtures/context_assembly_golden"
    cp orchestrator/tests/fixtures/context_assembly_golden/capture_from_baseline.py \
       "$BASE/orchestrator/tests/fixtures/context_assembly_golden/"
    cd "$BASE/orchestrator"
    CONTEXT_GOLDEN_CAPTURE_DIR=/some/scratch/dir \
    TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/test_golden_capture \
      python -m pytest -q -p no:cacheprovider \
      tests/fixtures/context_assembly_golden/capture_from_baseline.py

Capture twice into two directories and `diff -r` them before copying the
files here: a scenario that is not deterministic cannot be a golden.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.test_context_assembly_golden import (  # noqa: F401 — offline_turn is a fixture
    GOLDEN_DIR,
    SCENARIOS,
    assert_markers,
    golden_turn,
    meta_bytes,
    offline_turn,
)


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_capture_the_scenario_from_this_tree(scenario, offline_turn, monkeypatch):  # noqa: F811
    out = (os.environ.get("CONTEXT_GOLDEN_CAPTURE_DIR") or "").strip()
    if not out:
        pytest.skip("set CONTEXT_GOLDEN_CAPTURE_DIR to capture")
    target = Path(out).resolve()
    # Straight into the checked-in directory would skip the review step.
    assert target != GOLDEN_DIR.resolve(), "capture into a scratch dir, then review and copy"
    target.mkdir(parents=True, exist_ok=True)
    # A pre-change tree ignores the flag; a later reference tree reads the
    # default. Either way the capture is of the tree's own default path.
    monkeypatch.delenv("CONTEXT_CONCURRENT_READS", raising=False)
    prompt, meta = golden_turn(monkeypatch, offline_turn, scenario, "capture")
    assert_markers(scenario, prompt)
    (target / f"{scenario}.messages.json").write_text(prompt + "\n", encoding="utf-8")
    (target / f"{scenario}.meta.json").write_text(meta_bytes(meta), encoding="utf-8")
