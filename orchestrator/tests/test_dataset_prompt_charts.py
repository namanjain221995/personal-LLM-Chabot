"""The dataset route must not draw a chart out of numbers it guessed.

2026-09-17, QA case P06. "Compare revenue by region in a chart" went to the
dataset route and was answered with a mermaid pie whose four values the model
typed from the profile: East 138,653 against a true 133,668, North 133,968
against 167,382, South 113,436 against 106,662, West 166,356 against 197,874.
Even the ranking was wrong — the answer put East above North, and North is the
larger. A number in prose can be hedged; a number in a chart cannot, so the
prompt refuses the chart and points at the deliverable that computes one.

Routing such a request to the code-computed chart is a separate change (it
needs a dataset signal in the AS3 gate). This file pins the prompt half only.
"""
from __future__ import annotations

from app.engines import dataset
from app.engines.capability import CAPABILITY_LINE


def test_the_dataset_prompt_forbids_charts_of_computed_numbers():
    system = dataset._SYSTEM
    assert "CHARTS:" in system
    # The rule names what it forbids, in the words of the formats the model
    # actually reached for.
    assert "never draw a chart or a diagram" in system
    for named in ("mermaid pie", "xychart", "ASCII plot"):
        assert named in system
    assert "computed or estimated from the data" in system
    # Exact figures stay allowed exactly where they are real. CONTRACT MOVED
    # 2026-09-18: that was "where the file's full_rows let you compute them";
    # the model no longer computes anything (a sum over 200 visible rows was
    # 165% too high), so the chart rule defers to the NUMBERS rule, which
    # allows only cells and figures computed by code.
    assert "only as the NUMBERS rule allows" in system.split("CHARTS:")[1]
    # And the person is told how to get a real chart instead.
    assert "make a bar chart of revenue by region" in system


def test_the_rule_reaches_the_prompt_the_engine_actually_sends():
    uploads = [{"filename": "sales.csv", "bytes": 1024, "status": "ready", "profile": {"rows": 3}}]
    messages = dataset.build_messages("Compare revenue by region in a chart", uploads, [])
    system = messages[0]["content"]
    assert "CHARTS:" in system
    # The numbers rule it qualifies is still there, and so is the file
    # capability line (tests/test_capability_guard.py owns that promise).
    # CONTRACT MOVED 2026-09-18: HONESTY and FULL CONTENT were replaced by the
    # one NUMBERS rule (quote computed figures, never calculate).
    assert "NUMBERS:" in system
    assert "HONESTY:" not in system and "FULL CONTENT:" not in system
    assert CAPABILITY_LINE in system
    # The rule sits after the numbers rule and before the diagram rules, so
    # "you may draw a mermaid diagram" is never the last word on charts.
    assert system.index("NUMBERS:") < system.index("CHARTS:")
    assert system.index("CHARTS:") < system.index("DIAGRAMS:")
