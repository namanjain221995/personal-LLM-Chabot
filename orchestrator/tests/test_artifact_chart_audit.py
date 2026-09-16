"""artifacts/chart_audit.py — the SECOND reading of a computed chart.

The production incident of 2026-09-16 is the first test: "visualise this
table on pie chart" over a table the assistant had just written
(Rank | State | Count (Approx) | % of Total Records, with an "Other 25
States" row reading "~67" and a summary row "Total | Distinct States | 30")
drew seven equal 14 % slices, and selfcheck.chart_values_check reported the
values as matching because chart_data.recompute_matches re-ran the same
binding and agreed with itself.
"""
from __future__ import annotations

import pytest

from app.artifacts import chart_audit as CA
from app.artifacts import chart_spec as CS

INCIDENT_TABLE = {
    "id": "answer1",
    "title": "Table from the assistant's earlier answer",
    "columns": ["Rank", "State", "Count (Approx)", "% of Total Records"],
    "rows": [
        [1, "Texas", 21, "2.57%"],
        [2, "Missouri", 11, "1.35%"],
        [3, "Illinois", 9, "1.10%"],
        [4, "California", 8, "0.98%"],
        [5, "New Jersey", 7, "0.86%"],
        ["—", "Other 25 States", "~67", "8.20%"],
        ["Total", "Distinct States", 30, "3.67%"],
    ],
}

_STATES = ["Texas", "Missouri", "Illinois", "California", "New Jersey", "Other 25 States", "Distinct States"]


def _pie(categories, values, *, agg="count", y=(), provenance_agg="", series_name="Count", chart_type="pie"):
    return CS.Chart(
        type=chart_type, title="Records by state",
        data=CS.Binding(table_id="answer1", x="State", y=list(y), agg=agg),
        categories=list(categories),
        series=[CS.Series(name=series_name, values=[float(v) for v in values])],
        provenance=CS.Provenance(table_id="answer1", agg=provenance_agg or agg),
    )


# ------------------------------------------------- the production incident --


def test_the_seven_equal_slices_of_2026_09_16_are_a_binding_bug_not_data():
    chart = _pie(_STATES, [1] * 7)
    findings, _state = CA.audit_chart(chart, INCIDENT_TABLE)
    binding = [f for f in findings if f.code == "binding"]
    assert len(binding) == 1, findings
    assert "all 7 slices are 1" in binding[0].message
    assert "Count (Approx)" in binding[0].message, "the message names the column the chart should have used"
    assert "bound to the wrong column" in binding[0].message
    assert binding[0].mechanical is True, "chart_data.resolve_spec re-binds it: selfcheck may repair by code"


def test_the_summary_row_must_not_be_a_slice():
    chart = _pie(_STATES, [1] * 7)
    findings, _state = CA.audit_chart(chart, INCIDENT_TABLE)
    summary = [f for f in findings if f.code == "summary_category"]
    assert len(summary) == 1, findings
    assert "Distinct States" in summary[0].message and "counted twice" in summary[0].message


def test_the_chart_the_platform_should_have_drawn_is_clean():
    chart = _pie(["Other 25 States", "Texas", "Missouri", "Illinois", "California", "New Jersey"],
                 [67, 21, 11, 9, 8, 7], agg="count", provenance_agg="sum", series_name="Count (Approx)")
    findings, state = CA.audit_chart(chart, INCIDENT_TABLE)
    assert findings == [], findings
    assert state == "match", "the regrouping reads ~67 as 67, like the cell parser does"


# ------------------------------------------------------- (a) the binding --


def test_equal_slices_over_a_genuinely_flat_table_are_not_a_finding():
    table = {"id": "t", "columns": ["Team", "Tickets"], "rows": [["A", 5], ["B", 5], ["C", 5], ["D", 5]]}
    chart = CS.Chart(type="pie", title="Tickets", data=CS.Binding(table_id="t", x="Team", y=["Tickets"], agg="sum"),
                     categories=["A", "B", "C", "D"], series=[CS.Series(name="Tickets", values=[5, 5, 5, 5])],
                     provenance=CS.Provenance(table_id="t", agg="sum"))
    findings, state = CA.audit_chart(chart, table)
    assert [f.code for f in findings] == [], "four equal teams is the data, not the binding"
    assert state == "match"


def test_two_equal_slices_are_never_called_a_binding_bug():
    table = {"id": "t", "columns": ["Team", "Tickets"], "rows": [["A", 4], ["B", 9]]}
    chart = _pie(["A", "B"], [1, 1])
    chart = chart.model_copy(update={"data": CS.Binding(table_id="t", x="Team", agg="count")})
    findings, _state = CA.audit_chart(chart, table)
    assert [f.code for f in findings] == [], "with two slices there is nothing to tell apart"


def test_a_rank_column_alone_is_not_evidence_that_the_slices_differ():
    table = {"id": "t", "columns": ["Rank", "Team"], "rows": [[1, "A"], [2, "B"], [3, "C"], [4, "D"]]}
    chart = CS.Chart(type="pie", title="Teams", data=CS.Binding(table_id="t", x="Team", agg="count"),
                     categories=["A", "B", "C", "D"], series=[CS.Series(name="Count", values=[1, 1, 1, 1])],
                     provenance=CS.Provenance(table_id="t", agg="count"))
    findings, _state = CA.audit_chart(chart, table)
    assert [f.code for f in findings] == [], "a column that only numbers the rows measures nothing"


def test_a_bar_chart_of_equal_values_is_left_alone():
    chart = _pie(_STATES[:6], [1] * 6, chart_type="bar")
    findings, _state = CA.audit_chart(chart, INCIDENT_TABLE)
    assert [f.code for f in findings] == [], "only a part-of-whole chart claims a share of a whole"


# ------------------------------------------------ (b) the summary labels --


@pytest.mark.parametrize("label", [
    "Total", "total", "**Total**", "Grand Total", "Sub-total", "Subtotal", "Sum", "Overall",
    "All regions", "Distinct States", "Unique customers", "Total Records", "कुल", "योग", "કુલ", "સરવાળો",
])
def test_summary_words_are_recognised(label):
    assert CA.is_summary_label(label), label


@pytest.mark.parametrize("label", ["Texas", "Alabama", "Allentown", "Totality", "Q1", "Summer", "Distinctive design"])
def test_ordinary_categories_are_not_summaries(label):
    assert not CA.is_summary_label(label), label


def test_a_totals_bar_is_named_as_a_bar_not_a_slice():
    table = {"id": "t", "columns": ["Region", "Sales"], "rows": [["North", 10], ["South", 15], ["Total", 25]]}
    chart = CS.Chart(type="bar", title="Sales", data=CS.Binding(table_id="t", x="Region", y=["Sales"], agg="sum"),
                     categories=["North", "South", "Total"], series=[CS.Series(name="Sales", values=[10, 15, 25])],
                     provenance=CS.Provenance(table_id="t", agg="sum"))
    findings, _state = CA.audit_chart(chart, table)
    assert [f.code for f in findings] == ["summary_category"]
    assert "not a bar of its own" in findings[0].message


# ------------------------------------------------- (c) the recomputation --


def test_a_wrong_number_in_one_category_fails_the_regrouping():
    table = {"id": "t", "columns": ["Region", "Sales"], "rows": [["North", 10], ["North", 5], ["South", 15]]}
    chart = CS.Chart(type="bar", title="Sales", data=CS.Binding(table_id="t", x="Region", y=["Sales"], agg="sum"),
                     categories=["North", "South"], series=[CS.Series(name="Sales", values=[15, 99])],
                     provenance=CS.Provenance(table_id="t", agg="sum"))
    state, diffs = CA.recompute(chart, table)
    assert state == "mismatch"
    assert diffs == ["Sales: South is drawn as 99; the sum of Sales over the table's rows is 15"]


def test_the_regrouping_drops_the_summary_row_before_it_compares():
    table = {"id": "t", "columns": ["Region", "Sales"], "rows": [["North", 10], ["South", 15], ["Total", 25]]}
    chart = CS.Chart(type="bar", title="Sales", data=CS.Binding(table_id="t", x="Region", y=["Sales"], agg="sum"),
                     categories=["North", "South"], series=[CS.Series(name="Sales", values=[10, 15])],
                     provenance=CS.Provenance(table_id="t", agg="sum"))
    assert CA.recompute(chart, table) == ("match", [])


def test_an_average_is_regrouped_as_an_average():
    table = {"id": "t", "columns": ["Region", "Score"], "rows": [["North", 10], ["North", 20], ["South", 15]]}
    chart = CS.Chart(type="bar", title="Score", data=CS.Binding(table_id="t", x="Region", y=["Score"], agg="avg"),
                     categories=["North", "South"], series=[CS.Series(name="Score", values=[15, 15])],
                     provenance=CS.Provenance(table_id="t", agg="avg"))
    assert CA.recompute(chart, table) == ("match", [])
    wrong = chart.model_copy(update={"series": [CS.Series(name="Score", values=[30, 15])]})
    state, diffs = CA.recompute(wrong, table)
    assert state == "mismatch" and "the avg of Score" in diffs[0]


def test_a_binding_this_reading_cannot_reproduce_says_unverifiable_not_mismatch():
    table = {"id": "t", "columns": ["Region", "Quarter", "Sales"], "rows": [["North", "Q1", 10], ["South", "Q1", 15]]}
    grouped = CS.Chart(type="bar", title="Sales", data=CS.Binding(table_id="t", x="Region", y=["Sales"], agg="sum", group_by="Quarter"),
                       categories=["North", "South"], series=[CS.Series(name="Q1", values=[10, 15])],
                       provenance=CS.Provenance(table_id="t", agg="sum"))
    assert CA.recompute(grouped, table) == ("unverifiable", []), "a split by another column is chart_data's business"
    filtered = CS.Chart(type="bar", title="Sales", data=CS.Binding(table_id="t", x="Region", y=["Sales"], agg="sum",
                                                                  filters=[CS.Filter(column="Quarter", op="eq", value="Q1")]),
                        categories=["North", "South"], series=[CS.Series(name="Sales", values=[10, 15])],
                        provenance=CS.Provenance(table_id="t", agg="sum"))
    assert CA.recompute(filtered, table) == ("unverifiable", [])


def test_a_date_or_numeric_x_column_is_left_to_chart_data():
    table = {"id": "t", "columns": ["Day", "Sales"], "rows": [["2026-01-05", 10], ["2026-01-06", 15]]}
    chart = CS.Chart(type="line", title="Sales", data=CS.Binding(table_id="t", x="Day", y=["Sales"], agg="sum"),
                     categories=["05 Jan", "06 Jan"], series=[CS.Series(name="Sales", values=[10, 15])],
                     provenance=CS.Provenance(table_id="t", agg="sum"))
    assert CA.recompute(chart, table) == ("unverifiable", []), "chart_data buckets dates; this reading does not"


def test_an_other_bucket_is_skipped_rather_than_failed():
    rows = [[f"C{i}", i + 1] for i in range(10)]
    table = {"id": "t", "columns": ["Name", "N"], "rows": rows}
    chart = CS.Chart(type="pie", title="N", data=CS.Binding(table_id="t", x="Name", y=["N"], agg="sum"),
                     categories=["C9", "C8", "C7", "C6", "C5", "C4", "Other"],
                     series=[CS.Series(name="N", values=[10, 9, 8, 7, 6, 5, 10])],
                     provenance=CS.Provenance(table_id="t", agg="sum"))
    state, diffs = CA.recompute(chart, table)
    assert (state, diffs) == ("match", []), "a folded Other bucket is not this reading's business"


def test_a_cell_this_reading_cannot_parse_makes_it_stand_aside():
    table = {"id": "t", "columns": ["Region", "Sales"], "rows": [["North", "n/a"], ["South", 15]]}
    chart = CS.Chart(type="bar", title="Sales", data=CS.Binding(table_id="t", x="Region", y=["Sales"], agg="sum"),
                     categories=["North", "South"], series=[CS.Series(name="Sales", values=[0, 15])],
                     provenance=CS.Provenance(table_id="t", agg="sum"))
    assert CA.recompute(chart, table) == ("unverifiable", [])


# --------------------------------------------------------- over the spec --


def test_audit_spec_walks_a_document_and_never_raises_on_a_broken_chart():
    from app.artifacts import spec as S

    doc = S.parse_body("document", {
        "title": "States",
        "blocks": [{"type": "chart", "chart": _pie(_STATES, [1] * 7).model_dump(mode="python")}],
    })
    audit = CA.audit_spec(doc, [INCIDENT_TABLE])
    assert audit.charts == 1 and audit.has_part_of_whole and audit.has_summary_sensitive
    assert sorted(set(audit.codes())) == ["binding", "summary_category"]
    assert audit.mechanical("binding") is True
    # No table for the chart: nothing that needs one is claimed. The summary
    # label is read off the FINAL categories, so it still stands.
    without = CA.audit_spec(doc, [])
    assert sorted(set(without.codes())) == ["summary_category"]
    assert without.recomputed == "unverifiable"
