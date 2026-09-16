"""The self-check may not take a chart away, and may not warn about a
document with nothing wrong with it.

End to end, through the real renderer: every case was measured on the merged
chart-intent branch (adversarial verification, 2026-09-16) as
`completed_with_warnings`, and the first one published a Word file with the
chart REPLACED by a callout.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from app.artifacts import chart_spec as CS, pipeline, selfcheck as SC, store
from app.artifacts import spec as S
from app.artifacts.render import render_version
from app.config import settings
from tests.test_artifact_jobs import _accept, _composer, _run, isolated, owner  # noqa: F401

pytest.importorskip("weasyprint")


@pytest.fixture(autouse=True)
def real_render(monkeypatch):
    async def render(work_dir, spec, formats, title_slug, version, effort, **kw):
        report = await asyncio.to_thread(render_version, spec, formats, work_dir, title_slug=title_slug,
                                         version=version, effort=effort)
        return report.to_json()

    monkeypatch.setattr(pipeline, "_render_in_subprocess", render)
    monkeypatch.setattr(settings, "artifact_selfcheck", True)
    monkeypatch.setattr(settings, "artifact_selfcheck_repair", True)
    monkeypatch.setattr(settings, "artifact_selfcheck_budget_fast_s", 20.0)
    monkeypatch.setattr(settings, "artifact_selfcheck_budget_think_s", 60.0)
    yield


def _report(owner_id, row, version=1):
    return store.read_json(os.path.join(store.version_dir(owner_id, row["artifact_id"], version), SC.SELFCHECK_NAME))


def _blocks(owner_id, row, version=1):
    published = json.loads(Path(store.version_dir(owner_id, row["artifact_id"], version), "spec.json").read_text())
    return [b["type"] for b in published["document"]["blocks"]]


def _doc_with(chart):
    return S.parse_body("document", {"title": "Report", "blocks": [
        {"type": "paragraph", "text": "A short note about the figures."},
        {"type": "chart", "chart": chart.model_dump(mode="python")}]})


WATERFALL = CS.Chart(type="waterfall", title="Cash bridge",
                     categories=["Opening", "Sales", "Costs", "Total"],
                     series=[CS.Series(name="GBP", values=[100.0, 50.0, -30.0, 120.0])])


def test_the_repair_may_not_delete_the_chart_it_cannot_re_bind(owner):
    """Measured before the fix: outcome "repaired", repair accepted, and the
    published blocks were ['paragraph', 'callout'] — "Cash bridge was not
    drawn because its numbers did not come from a table"."""
    pipeline.set_composer(_composer(_doc_with(WATERFALL)))
    row = _accept(owner, instruction="draw the cash bridge", formats=["docx"], format_reason="explicit: word")
    fresh = _run(row["id"])
    report = _report(owner, row)
    assert _blocks(owner, row) == ["paragraph", "chart"], "the person asked for a chart"
    assert fresh["status"] == "completed", report["unmet"]
    assert report["repair"].get("attempted") is not True


SCATTER = CS.Chart(type="scatter", title="Spend against headcount",
                   series=[CS.Series(name="Teams", values=[4.0, 7.0, 2.0], x=[10.0, 20.0, 30.0])])


def test_a_literal_scatter_in_a_word_file_is_not_a_warning(owner):
    """A chart in a .docx is a picture: neither its type nor its series can
    be read back, and there is no table to regroup. Measured before the fix:
    "not confirmed: a scatter chart" and "not confirmed: chart values from
    the data — no native chart values to compare"."""
    pipeline.set_composer(_composer(_doc_with(SCATTER)))
    row = _accept(owner, instruction="a short report with a scatter of spend against headcount",
                  formats=["docx"], format_reason="explicit: word")
    fresh = _run(row["id"])
    report = _report(owner, row)
    assert report["unmet"] == [] and report["outcome"] == "clean"
    assert fresh["status"] == "completed"
    assert "a scatter chart" not in report["false_claim_guard"]["claimable"], (
        "silence about an unreadable requirement is not a claim that it was met")


MIXED_METRIC_TABLE = {
    "id": "answer1", "title": "Monthly revenue", "columns": ["Month", "Metric", "Value"],
    "rows": [["Jan", "Total Revenue", 100], ["Jan", "Refunds", 10],
             ["Feb", "Total Revenue", 80], ["Feb", "Refunds", 5],
             ["Mar", "Total Revenue", 60], ["Mar", "Refunds", 4]],
}


def test_a_correct_bar_over_rows_the_audit_used_to_drop_publishes_clean(owner):
    chart = CS.Chart(type="bar", title="Value by month",
                     data=CS.Binding(table_id="answer1", x="Month", y=["Value"], agg="sum"),
                     categories=["Jan", "Feb", "Mar"],
                     series=[CS.Series(name="Value", values=[110.0, 85.0, 64.0])],
                     provenance=CS.Provenance(table_id="answer1", agg="sum"))
    pipeline.set_composer(_composer(_doc_with(chart)))
    row = _accept(owner, instruction="chart the monthly value", formats=["docx"], format_reason="explicit: word",
                  material={"tables": [MIXED_METRIC_TABLE]})
    fresh = _run(row["id"])
    report = _report(owner, row)
    assert fresh["status"] == "completed", report["unmet"]
    assert _blocks(owner, row) == ["paragraph", "chart"]


OTHER_TABLE = {"id": "answer1", "title": "Spend", "columns": ["Category", "Spend"],
               "rows": [[f"C{i}", 100 - i] for i in range(9)] + [["Other", 3]]}


def test_a_pie_whose_other_fold_meets_a_row_called_other_publishes_clean(owner):
    from app.artifacts import chart_data as CD

    chart = CS.Chart(type="pie", title="Spend by category",
                     data=CS.Binding(table_id="answer1", x="Category", y=["Spend"], agg="sum"))
    resolved, _n, _m = CD.resolve_chart(chart, [OTHER_TABLE])
    pipeline.set_composer(_composer(_doc_with(resolved)))
    row = _accept(owner, instruction="pie of spend by category", formats=["docx"], format_reason="explicit: word",
                  material={"tables": [OTHER_TABLE]})
    fresh = _run(row["id"])
    report = _report(owner, row)
    assert fresh["status"] == "completed", report["unmet"]


def test_a_wrong_number_in_a_word_file_still_fails(owner):
    """The three silences above are not a retreat: a bound chart whose
    numbers do not match the table is still a failed must, in a .docx, with
    no native chart to read."""
    chart = CS.Chart(type="bar", title="Value by month",
                     data=CS.Binding(table_id="answer1", x="Month", y=["Value"], agg="sum"),
                     categories=["Jan", "Feb", "Mar"],
                     series=[CS.Series(name="Value", values=[999.0, 85.0, 64.0])],
                     provenance=CS.Provenance(table_id="answer1", agg="sum"))
    pipeline.set_composer(_composer(_doc_with(chart)))
    row = _accept(owner, instruction="chart the monthly value", formats=["docx"], format_reason="explicit: word",
                  material={"tables": [MIXED_METRIC_TABLE]})
    _run(row["id"])
    report = _report(owner, row)
    assert report["outcome"] in ("unmet", "repaired"), report
    assert report["repair"].get("attempted") is True, "a bound chart is still re-bindable by code"
