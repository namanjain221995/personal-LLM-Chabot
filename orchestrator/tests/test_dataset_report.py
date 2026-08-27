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
