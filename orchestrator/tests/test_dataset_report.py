"""H-03: generated reports for UPLOADED datasets.

Covers the two halves that are pure: WHEN a document is generated (the intent
test, whose false positives would replace ordinary dataset answers with PDFs)
and WHAT goes in it (facts computed from the profile, never asked of a model).
"""
import pytest

from app.engines.dataset_report import (
    build_report_markdown,
    wants_document_report,
)


# --- intent -----------------------------------------------------------------

@pytest.mark.parametrize(
    "message",
    [
        # The exact wording H-03 was reported with, and its acceptance test.
        "generate a very short pdf file in one page about this csv file",
        "Generate a very short one-page PDF report about this CSV and give me "
        "the downloadable file.",
        "create a PDF summary of this data",
        "make me a docx report",
        "can you generate a report file from this?",
        "I need a document I can download about this dataset",
    ],
)
def test_document_requests_are_detected(message):
    assert wants_document_report(message) is True


@pytest.mark.parametrize(
    "message",
    [
        # Ordinary dataset questions: these must keep their prose answer.
        "summarise this csv file",
        "what columns does this file have?",
        "how many rows are in this csv?",
        "give me insights about this data",
        "which stage has the most opportunities?",
        "what is the total amount?",
        # "pdf"/"report" with no creation verb is a question, not a request.
        "what does this report say?",
        "is this a pdf?",
        # EXPORT_RE (sql.py) matches a bare "csv" — the reason this module does
        # not reuse it. A csv export is not a generated document.
        "export this to csv",
        "download the spreadsheet",
        "",
    ],
)
def test_ordinary_questions_are_not_document_requests(message):
    assert wants_document_report(message) is False


# --- content ----------------------------------------------------------------

PROFILE = {
    "file": "sample_opportunities.csv",
    "bytes": 1234,
    "kind": "table",
    "rows": 42,
    "columns_total": 3,
    "columns": [
        {
            "name": "Amount",
            "dtype": "DOUBLE",
            "null_pct": 4.76,
            "distinct": 40,
            "min": 1000,
            "max": 90000,
        },
        {
            "name": "StageName",
            "dtype": "VARCHAR",
            "null_pct": 0.0,
            "distinct": 3,
            "min_length": 4,
            "max_length": 11,
            "top_values": [
                {"value": "Prospecting", "count": 20},
                {"value": "Closed Won", "count": 15},
            ],
        },
    ],
}

UPLOAD = {"filename": "sample_opportunities.csv", "bytes": 1234, "profile": [PROFILE]}


def test_report_states_real_shape_from_the_profile():
    md = build_report_markdown("Data Report", [UPLOAD], "Some prose.", "2026-08-24 10:00")
    assert "sample_opportunities.csv" in md
    assert "**42** rows" in md
    assert "**3** columns" in md


def test_report_lists_columns_with_their_statistics():
    md = build_report_markdown("Data Report", [UPLOAD], "", "2026-08-24 10:00")
    assert "| Amount | DOUBLE | 4.76% | 40 | 1000 … 90000 |" in md
    # A string column reports length, never a raw min/max VALUE (profile.py
    # withholds those on purpose — the alphabetically first cell can be a
    # secret from anywhere in the file).
    assert "4–11 chars" in md
    # The renderer writes "**Column** - Value (count), Value (count)"; the value
    # and its count are never separated by a dash. This assertion and the code
    # it tests landed in the same commit, so it had never passed.
    assert "**StageName** \u2014 Prospecting (20), Closed Won (15)" in md


def test_identifier_columns_are_not_broken_down():
    """An Id column has top_values but every count is 1 — it says nothing."""
    ids = {
        "file": "x.csv",
        "rows": 3,
        "columns_total": 2,
        "columns": [
            {
                "name": "Id",
                "dtype": "VARCHAR",
                "null_pct": 0.0,
                "distinct": 3,
                "top_values": [
                    {"value": "a", "count": 1},
                    {"value": "b", "count": 1},
                ],
            },
            {
                "name": "Stage",
                "dtype": "VARCHAR",
                "null_pct": 0.0,
                "distinct": 2,
                "top_values": [
                    {"value": "Won", "count": 2},
                    {"value": "Lost", "count": 1},
                ],
            },
        ],
    }
    md = build_report_markdown("T", [{"filename": "x.csv", "profile": [ids]}], "", "now")
    assert "**Stage** — Won (2), Lost (1)" in md
    assert "**Id** —" not in md


def test_boolean_flags_rank_below_categorical_columns():
    """is_closed/is_won have distinct=2 and would otherwise win on count."""
    prof = {
        "file": "x.csv",
        "rows": 10,
        "columns_total": 2,
        "columns": [
            {
                "name": "is_won",
                "dtype": "BOOLEAN",
                "null_pct": 0.0,
                "distinct": 2,
                "top_values": [
                    {"value": "False", "count": 7},
                    {"value": "True", "count": 3},
                ],
            },
            {
                "name": "stage",
                "dtype": "VARCHAR",
                "null_pct": 0.0,
                "distinct": 3,
                "top_values": [
                    {"value": "Won", "count": 5},
                    {"value": "Lost", "count": 3},
                ],
            },
        ],
    }
    md = build_report_markdown(
        "T", [{"filename": "x.csv", "profile": [prof]}], "", "now"
    )
    assert md.index("**stage**") < md.index("**is_won**")


def test_narrative_is_included_when_present():
    md = build_report_markdown("T", [UPLOAD], "Pipeline is concentrated.", "now")
    assert "## Summary" in md
    assert "Pipeline is concentrated." in md


def test_pipes_in_user_data_cannot_break_the_table():
    hostile = {
        "file": "x.csv",
        "rows": 1,
        "columns_total": 1,
        "columns": [{"name": "a|b", "dtype": "VARCHAR", "null_pct": 0.0, "distinct": 1}],
    }
    md = build_report_markdown("T", [{"filename": "x.csv", "profile": [hostile]}], "", "now")
    assert "a\\|b" in md


def test_unreadable_upload_does_not_crash_the_report():
    bad = {"filename": "x.bin", "profile": [{"file": "x.bin", "error": "not a table"}]}
    md = build_report_markdown("T", [bad], "", "now")
    assert "No readable tabular data" in md


# --- the totals reconcile (self-consistency, 2026-09-21) ---------------------
#
# "Figures computed from every row" prints the column total; the breakdown
# under it lists only the group values the profile kept. Before this block the
# two figures could disagree on the same page with nothing but an occasional
# "_Further values are not listed._" between them — the summary line saying one
# total and the table below summing to another, which is the shape the
# answer-quality sweep recorded. Both numbers are ours, so the gap is
# arithmetic, not a sentence we hope a model will write.

def _sales_profile(rows_by_region, *, column_sum, file_rows, omitted=(), months=None, truncated=True):
    return {
        "file": "sales-2025.csv",
        "bytes": 90000,
        "kind": "table",
        "rows": file_rows,
        "columns_total": 3,
        "columns": [
            {"name": "region", "dtype": "VARCHAR", "null_pct": 0.0, "distinct": 5},
            {"name": "order_date", "dtype": "DATE", "null_pct": 0.0, "distinct": 90},
            {"name": "revenue", "dtype": "DOUBLE", "null_pct": 0.0, "distinct": 198,
             "min": 120.5, "max": 19000.0, "sum": column_sum, "avg": 5110.49, "median": 4980.0},
        ],
        "aggregates": {
            "computed": "exact",
            "measures": ["revenue"],
            "by_group": [{"group": "region", "measure": "revenue", "truncated": truncated,
                          "rows": list(rows_by_region)}],
            "by_month": list(months or []),
            "omitted": list(omitted),
        },
    }


def _report(prof, message="Create a PDF report of revenue by region"):
    return build_report_markdown(
        "Sales report", [{"filename": "sales-2025.csv", "bytes": 90000, "profile": [prof]}],
        "", "2026-09-21 10:00", message=message,
    )


SHORT_ROWS = [
    {"value": "North", "count": 70, "sum": 380120.00, "avg": 5430.29},
    {"value": "South", "count": 68, "sum": 341900.55, "avg": 5027.95},
    {"value": "East", "count": 60, "sum": 287000.47, "avg": 4783.34},
]


def test_a_breakdown_that_does_not_add_up_to_the_total_says_so_with_both_figures():
    """The measures table says 1,022,098.02 and these rows add up to
    1,009,021.02. The report must state the gap, in figures, under the rows."""
    md = _report(_sales_profile(SHORT_ROWS, column_sum=1022098.02, file_rows=200))
    assert "| revenue | 1,022,098.02 |" in md
    assert "These rows add up to 1,009,021.02, 13,077.00 less than the 1,022,098.02 total revenue above" in md
    assert "cover 198 of its 200 rows" in md
    # The vague line it replaces must not also be printed.
    assert "_Further values are not listed._" not in md


def test_the_profile_s_own_reason_for_the_gap_is_carried_into_the_report():
    """The dataset chat engine relays `omitted`; the report dropped it. The
    figure says how much is missing, the reason says why."""
    md = _report(_sales_profile(
        SHORT_ROWS, column_sum=1022098.02, file_rows=200,
        omitted=["by_group region: 2 value(s) found in only one row are not listed, so its "
                 "listed groups add up to less than the column totals"],
    ))
    assert "2 value(s) found in only one row are not listed" in md


def test_a_breakdown_that_adds_up_is_left_exactly_as_it_was():
    """No note on a report whose figures agree: the mechanism must be silent
    when there is nothing to reconcile."""
    rows = [
        {"value": "North", "count": 70, "sum": 380120.00, "avg": 5430.29},
        {"value": "South", "count": 68, "sum": 341900.55, "avg": 5027.95},
        {"value": "East", "count": 62, "sum": 300077.47, "avg": 4839.96},
    ]
    md = _report(_sales_profile(rows, column_sum=1022098.02, file_rows=200, truncated=False))
    assert "| revenue | 1,022,098.02 |" in md
    assert "These rows add up to" not in md
    assert "These rows cover" not in md


def test_float_noise_in_a_double_sum_is_not_reported_as_a_gap():
    """A DOUBLE column's sum comes back as 1,022,098.0200000001. That is the
    last bits of a double, not a missing row."""
    rows = [
        {"value": "North", "count": 100, "sum": 511049.01, "avg": 5110.49},
        {"value": "South", "count": 100, "sum": 511049.01, "avg": 5110.49},
    ]
    md = _report(_sales_profile(rows, column_sum=1022098.0200000001, file_rows=200, truncated=False))
    assert "These rows add up to" not in md


def test_rows_that_are_short_while_the_figures_agree_are_still_said():
    """A dropped group whose revenue is zero leaves the totals equal and the
    row counts apart — a reader counting 190 rows under a heading that says
    200 is the same contradiction in the other column."""
    rows = [
        {"value": "North", "count": 95, "sum": 511049.01, "avg": 5379.46},
        {"value": "South", "count": 95, "sum": 511049.01, "avg": 5379.46},
    ]
    md = _report(_sales_profile(rows, column_sum=1022098.02, file_rows=200))
    assert "These rows cover 190 of the 200 rows above" in md
    assert "still adds up to the total" in md


def test_a_breakdown_row_with_no_computed_sum_claims_nothing():
    """Nothing is asserted about figures that were never computed."""
    rows = [
        {"value": "North", "count": 70, "sum": 380120.00, "avg": 5430.29},
        {"value": "South", "count": 68, "sum": None, "avg": None},
    ]
    md = _report(_sales_profile(rows, column_sum=1022098.02, file_rows=200))
    assert "These rows add up to" not in md
    # the old line is still the honest thing to say for a truncated list
    assert "_Further values are not listed._" in md


def test_monthly_rows_are_reconciled_against_the_column_total():
    """Undated rows are in no month and AGG_MAX_MONTHS caps the list, so the
    months add up to less than the Total above them."""
    months = [{"date": "order_date", "measure": "revenue", "truncated": True, "rows": [
        {"month": "2025-01", "count": 60, "sum": 300000.00},
        {"month": "2025-02", "count": 60, "sum": 320000.00},
        {"month": "2025-03", "count": 60, "sum": 340000.00},
    ]}]
    prof = _sales_profile(SHORT_ROWS, column_sum=1022098.02, file_rows=200, months=months)
    md = _report(prof, message="Create a PDF report of monthly revenue")
    assert "### Total revenue by month" in md
    assert "These rows add up to 960,000.00, 62,098.02 less than the 1,022,098.02 total revenue above" in md
    assert "_Further months are not listed._" not in md


def test_a_breakdown_larger_than_the_total_is_reported_in_its_own_direction():
    """A lossy DOUBLE sum can round the other way. The sentence must not
    claim rows are missing when there are more, not fewer."""
    rows = [
        {"value": "North", "count": 100, "sum": 600000.00, "avg": 6000.0},
        {"value": "South", "count": 100, "sum": 500000.00, "avg": 5000.0},
    ]
    md = _report(_sales_profile(rows, column_sum=1022098.02, file_rows=200, truncated=False))
    assert "77,901.98 more than the 1,022,098.02 total revenue above" in md


def test_a_measure_name_with_an_underscore_cannot_close_the_italics():
    """The sentence is an italic note and the measure name is the file's own
    text: `order_total` would otherwise open an emphasis run inside it."""
    prof = _sales_profile(SHORT_ROWS, column_sum=1022098.02, file_rows=200)
    prof["columns"][2]["name"] = "order_total"
    prof["aggregates"]["measures"] = ["order_total"]
    prof["aggregates"]["by_group"][0]["measure"] = "order_total"
    md = _report(prof, message="Create a PDF report of order_total by region")
    assert "total order\\_total above" in md
