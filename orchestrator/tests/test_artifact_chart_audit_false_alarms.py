"""chart_audit must never warn about a chart that is RIGHT.

Every case here was measured on the merged chart-intent branch before the
fix (adversarial verification, 2026-09-16) and published a warning — twice
over, since a `no_summary_category` or `values_recomputed` failure is a
must, and a failed must sends the job to the self-check's repair.
"""
from __future__ import annotations

import pytest

from app.artifacts import chart_audit as CA
from app.artifacts import chart_data as CD
from app.artifacts import chart_spec as CS


# ------------------------------------- a summary bar that IS the chart --


def test_a_waterfalls_closing_total_bar_is_not_a_double_count():
    """Measured before: this literal waterfall failed no_summary_category,
    and the repair then replaced the whole chart with a callout."""
    chart = CS.Chart(type="waterfall", title="Cash bridge",
                     categories=["Opening", "Sales", "Costs", "Total"],
                     series=[CS.Series(name="GBP", values=[100.0, 50.0, -30.0, 120.0])])
    assert CA.check_summary_categories(chart) == []


def test_a_funnels_top_stage_is_a_stage_not_a_summary():
    chart = CS.Chart(type="funnel", title="Signup funnel",
                     categories=["Total Visits", "Unique Visitors", "Signups"],
                     series=[CS.Series(name="People", values=[1000.0, 700.0, 90.0])])
    assert CA.check_summary_categories(chart) == []


def test_a_pie_still_cannot_carry_a_totals_slice():
    """The exclusion is two chart types wide, not a retreat."""
    chart = CS.Chart(type="pie", title="Spend", categories=["Rent", "Food", "Total"],
                     series=[CS.Series(name="Spend", values=[30.0, 20.0, 50.0])])
    findings = CA.check_summary_categories(chart)
    assert [f.code for f in findings] == ["summary_category"] and "Total" in findings[0].message


# ------------------------- the qualified summary words are also names --


def _pie_of(labels, values, *, x="Hospital", y="Beds"):
    return CS.Chart(type="pie", title="Beds", data=CS.Binding(table_id="t", x=x, y=[y], agg="sum"),
                    categories=list(labels), series=[CS.Series(name=y, values=[float(v) for v in values])],
                    provenance=CS.Provenance(table_id="t", agg="sum"))


@pytest.mark.parametrize("label", ["All Saints Hospital", "Total Rewards", "Distinct Designs Ltd", "Unique Fitness"])
def test_an_ordinary_name_that_matches_the_vocabulary_is_not_flagged(label):
    """The widened vocabulary reads these as summaries (is_summary_label is
    the vocabulary and still says so). A DRAWN category is only a summary
    when the qualifier names the chart's own dimension or its value really
    is the sum of the others; neither holds here."""
    assert CA.is_summary_label(label) is True, "the vocabulary is unchanged"
    chart = _pie_of([label, "Mercy", "St Jude"], [10, 20, 30])
    assert CA.check_summary_categories(chart) == []


def test_the_2026_09_16_distinct_states_slice_is_still_flagged():
    """The incident: a pie over x="State" whose last slice was the table's
    summary row, "Distinct States". The qualifier names the dimension."""
    chart = _pie_of(["Texas", "Missouri", "Distinct States"], [1, 1, 1], x="State", y="Count")
    findings = CA.check_summary_categories(chart)
    assert [f.code for f in findings] == ["summary_category"]
    assert "Distinct States" in findings[0].message


def test_a_qualified_summary_whose_value_is_the_sum_of_the_others_is_flagged():
    """No dimension to match ("Rewards" is not the x column), but the
    arithmetic is the double count itself."""
    chart = _pie_of(["Cashback", "Points", "Total Rewards"], [30, 20, 50], x="Programme", y="Spend")
    findings = CA.check_summary_categories(chart)
    assert [f.code for f in findings] == ["summary_category"] and "Total Rewards" in findings[0].message


# ----------------------------- rows: the audit drops what chart_data drops --


def test_a_metric_cell_reading_total_revenue_does_not_drop_the_row():
    """chart_data._is_total_label does not know "total <x>", so it KEEPS
    these rows and sums them. Measured before the fix, this correct bar was
    reported as "Jan is drawn as 110; the sum of Value over the table's rows
    is 10" — the audit's own regrouping had dropped four of the six rows."""
    table = {"id": "answer1", "columns": ["Month", "Metric", "Value"],
             "rows": [["Jan", "Total Revenue", 100], ["Jan", "Refunds", 10],
                      ["Feb", "Total Revenue", 80], ["Feb", "Refunds", 5],
                      ["Mar", "Total Revenue", 60], ["Mar", "Refunds", 4]]}
    chart = CS.Chart(type="bar", title="Value by month", data=CS.Binding(table_id="answer1", x="Month", y=["Value"], agg="sum"))
    resolved, _notes, msg = CD.resolve_chart(chart, [table])
    assert msg == "" and list(resolved.series[0].values) == [110.0, 85.0, 64.0]
    assert CA.recompute(resolved, table) == ("match", [])


def test_a_real_totals_row_is_still_dropped_before_the_comparison():
    table = {"id": "t", "columns": ["Region", "Sales"], "rows": [["North", 10], ["South", 15], ["Total", 25]]}
    chart = CS.Chart(type="bar", title="Sales", data=CS.Binding(table_id="t", x="Region", y=["Sales"], agg="sum"),
                     categories=["North", "South"], series=[CS.Series(name="Sales", values=[10, 15])],
                     provenance=CS.Provenance(table_id="t", agg="sum"))
    assert CA.recompute(chart, table) == ("match", [])


# --------------------------------- the Other bucket is chart_data's own --


def test_the_pie_other_fold_is_not_compared_with_a_row_called_other():
    """PIE_MAX_SLICES = 7: chart_data folds the tail into a bucket it names
    "Other". Measured before the fix against a table whose last row is
    literally "Other": "Other is drawn as 282; the sum of Spend over the
    table's rows is 3"."""
    rows = [[f"C{i}", 100 - i] for i in range(9)] + [["Other", 3]]
    table = {"id": "answer1", "columns": ["Category", "Spend"], "rows": rows}
    chart = CS.Chart(type="pie", title="Spend by category", data=CS.Binding(table_id="answer1", x="Category", y=["Spend"], agg="sum"))
    resolved, _notes, msg = CD.resolve_chart(chart, [table])
    assert msg == "" and resolved.categories[-1] == CD.OTHER
    state, diffs = CA.recompute(resolved, table)
    assert (state, diffs) == ("match", []), diffs


def test_a_wrong_number_in_a_real_category_still_fails_beside_an_other_fold():
    """Skipping "Other" costs the check nothing else."""
    rows = [[f"C{i}", 100 - i] for i in range(9)] + [["Other", 3]]
    table = {"id": "answer1", "columns": ["Category", "Spend"], "rows": rows}
    chart = CS.Chart(type="pie", title="Spend by category", data=CS.Binding(table_id="answer1", x="Category", y=["Spend"], agg="sum"))
    resolved, _n, _m = CD.resolve_chart(chart, [table])
    wrong = resolved.model_copy(update={"series": [CS.Series(name=resolved.series[0].name,
                                                            values=[999.0] + list(resolved.series[0].values)[1:])]})
    state, diffs = CA.recompute(wrong, table)
    assert state == "mismatch" and "999" in diffs[0]


# -------------------------------- equal slices over a genuinely flat column --


def test_three_teams_of_five_are_not_a_binding_bug_because_a_year_column_differs():
    """Measured before the fix: "all 3 slices are 5 … while Founded in the
    table it is drawn from differs row by row (2,011, 2,015, 2,019) — it is
    bound to the wrong column". The chart is right and the sentence names a
    YEAR column as the one it should have used."""
    table = {"id": "t", "columns": ["Team", "Members", "Founded"],
             "rows": [["Alpha", 5, 2011], ["Beta", 5, 2015], ["Gamma", 5, 2019]]}
    chart = CS.Chart(type="pie", title="Teams", data=CS.Binding(table_id="t", x="Team", y=["Members"], agg="sum"),
                     categories=["Alpha", "Beta", "Gamma"], series=[CS.Series(name="Members", values=[5.0, 5.0, 5.0])],
                     provenance=CS.Provenance(table_id="t", agg="sum"))
    findings, state = CA.audit_chart(chart, table)
    assert [f.code for f in findings] == [], [f.message for f in findings]
    assert state == "match"


def test_the_incident_pie_is_still_a_binding_bug():
    """The guard stands aside only for a chart that NAMES its own measure
    column; the incident's binding is agg="count" with no y at all."""
    from tests.test_artifact_chart_audit import INCIDENT_TABLE, _STATES, _pie as incident_pie

    findings, _state = CA.audit_chart(incident_pie(_STATES, [1] * 7), INCIDENT_TABLE)
    assert [f.code for f in findings if f.code == "binding"] == ["binding"]
