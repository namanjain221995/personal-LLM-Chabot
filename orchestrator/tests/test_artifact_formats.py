"""The format-selection policy (app/artifacts/formats.py), as a table.

Offline. CONTRACT-2 §1 and §5 (2026-09-12): csv is a format, a workbook
carries xlsx/csv/docx/pdf from one spec, a list of named formats is
honoured in order, aliases and typos name their format, a format named
as the SOURCE of a conversion is not a target, "X or Y" and "best format"
are decided from the task words, and a CSV asked for with styling words
carries a note.
"""
from __future__ import annotations

import pytest

from app.artifacts import formats as F, types as T


# ------------------------------------------------------------- aliases --


@pytest.mark.parametrize("text,formats", [
    ("Create a professional PDF about this.", ["pdf"]),
    ("Make a Word document from this conversation.", ["docx"]),
    ("Give me a CSV of the leads", ["csv"]),
    ("a comma-separated file of the leads", ["csv"]),
    ("comma separated values please", ["csv"]),
    ("Create a dataset of 500 customers", ["csv"]),
    ("Give me a data file of the orders", ["csv"]),
    # Typos and spellings (discovery C4).
    ("Give me the audit as a cvs file", ["csv"]),
    ("Create a spread sheet of the customers", ["xlsx"]),
    ("Export this as xlxs", ["xlsx"]),
    ("as an xls please", ["xlsx"]),
    ("make an exel of it", ["xlsx"]),
    ("an excell workbook", ["xlsx"]),
    ("a work book for the budget", ["xlsx"]),
    ("Give me a ppt on onboarding", ["pptx"]),
    ("a powerpint about Q3", ["pptx"]),
    ("a power point for the board", ["pptx"]),
    ("a slide deck on pricing", ["pptx"]),
    ("Make me slides for the offsite", ["pptx"]),
    # A bare "word" names Word only inside a list of formats.
    ("Share XLSX, Word, PDF, and CSV of this audit.", ["xlsx", "docx", "pdf", "csv"]),
    ("Give me xlsx, word, pdf and csv of this", ["xlsx", "docx", "pdf", "csv"]),
    ("pdf or word of this", ["pdf", "docx"]),
    ("Write a report in plain words about the launch", []),
    ("in a word, no", []),
    # A number after "sheet" is a part, and "cheat sheet" is a document.
    ("Change sheet 2", []),
    ("Make a cheat sheet for the sales team", []),
    ("Make a balance sheet report", []),
    # "cvs" without a creation context is not a format.
    ("The cvs pipeline failed again", []),
    # "slide 3" is a part of a deck, not a request for one.
    ("what do you think of slide 3?", []),
])
def test_named_formats(text, formats):
    assert F.explicit_formats(text) == formats, text


def test_order_of_mention_is_kept_and_repeats_are_one():
    assert F.explicit_formats("CSV first, then the Excel, then a PDF and the csv again") == ["csv", "xlsx", "pdf"]
    assert F.explicit_formats("PDF and pdf and .pdf") == ["pdf"]


@pytest.mark.parametrize("text,formats", [
    # The source of a conversion is not a deliverable (pinned since the
    # first release: test_artifact_intent.py:26 / test_artifact_spec.py).
    ("Turn this CSV into an Excel dashboard.", ["xlsx"]),
    ("Convert the xlsx to csv", ["csv"]),
    ("Create a PDF from the Excel data", ["pdf"]),
    ("Make a Word document from the attached csv", ["docx"]),
    ("Export this CSV as xlsx", ["xlsx"]),
    ("Read the pasted csv and give me a PDF", ["pdf"]),
    # …but a format that is both read and asked for is asked for.
    ("Clean the CSV and give me the CSV back", ["csv"]),
])
def test_a_source_format_is_not_a_target(text, formats):
    assert F.explicit_formats(text) == formats, text


# ---------------------------------------------------------------- kinds --


@pytest.mark.parametrize("text,kind,fmts,explicit", [
    ("Create a professional PDF about this.", "document", ["pdf"], True),
    ("Make a Word document from this conversation.", "document", ["docx"], True),
    ("Generate a PowerPoint presentation for the CEO.", "presentation", ["pptx", "pdf"], True),
    ("Turn this CSV into an Excel dashboard.", "workbook", ["xlsx"], True),
    ("Create an SOP document.", "document", ["docx", "pdf"], False),
    ("Build a deck for the board.", "presentation", ["pptx", "pdf"], False),
    ("Make a budget tracker.", "workbook", ["xlsx"], False),
    # csv → workbook; a multi-format list is honoured in order.
    ("Give me a CSV of the leads", "workbook", ["csv"], True),
    ("Share XLSX, Word, PDF, and CSV of this audit.", "workbook", ["xlsx", "docx", "pdf", "csv"], True),
    ("Give me a spread sheet and a cvs file of these rows", "workbook", ["xlsx", "csv"], True),
    ("Give me a PDF and an Excel of this audit table", "workbook", ["pdf", "xlsx"], True),
    # A CSV with the styling it cannot carry gets the Excel next to it.
    ("Create a CSV with red highlights for failed rows", "workbook", ["csv", "xlsx"], True),
])
def test_kind_and_formats(text, kind, fmts, explicit):
    d = F.decide(text)
    assert (d.kind, d.formats, d.explicit) == (kind, fmts, explicit), d


def test_a_workbook_carries_word_and_pdf_but_a_document_carries_no_grid():
    """CONTRACT-2 §1/§5: cross-kind formats are no longer dropped silently
    for a workbook; a document or a deck still cannot be a csv/xlsx."""
    d = F.decide("Share XLSX, Word, PDF, and CSV of this audit.")
    assert d.kind == "workbook" and d.formats == ["xlsx", "docx", "pdf", "csv"] and d.warnings == []
    assert T.FORMATS_FOR_KIND["workbook"] == ("xlsx", "csv", "docx", "pdf")
    d = F.decide("Create a PDF report on AI in business, plus an Excel of the raw numbers")
    assert d.kind == "document" and d.formats == ["pdf"]
    assert d.warnings == ["xlsx cannot be produced for a document; it was not made"]
    d = F.decide("Make a PowerPoint deck and a csv of the figures")
    assert d.kind == "presentation" and d.formats == ["pptx", "pdf"] and "csv cannot be produced" in d.warnings[0]
    ok, bad = F.formats_for_conversion("document", ["csv", "pdf"])
    assert (ok, bad) == (["pdf"], ["csv"])
    ok, bad = F.formats_for_conversion("workbook", ["csv", "pdf", "docx", "pptx"])
    assert (ok, bad) == (["csv", "pdf", "docx"], ["pptx"])


# -------------------------------------------------------------- X or Y --


@pytest.mark.parametrize("text,kind,fmts,note", [
    # Both, when the kind carries both: two cheap files from one spec.
    ("Create XLSX or CSV of the table above.", "workbook", ["xlsx", "csv"], "or: both xlsx and csv"),
    ("Give me this as a PDF or a Word doc", "document", ["pdf", "docx"], "or: both pdf and docx"),
    ("PDF or Excel of this table please", "workbook", ["pdf", "xlsx"], "or: both pdf and xlsx"),
    # Otherwise the more useful one, without a "cannot be produced" warning.
    ("Give me a PDF or an Excel", "document", ["pdf"], "or: pdf over xlsx"),
    ("Excel or PowerPoint for the pitch", "presentation", ["pptx", "pdf"], "or: pptx over xlsx"),
])
def test_x_or_y(text, kind, fmts, note):
    d = F.decide(text)
    assert (d.kind, d.formats) == (kind, fmts), d
    assert note in d.reason and d.warnings == [], d


def test_x_or_y_applies_to_formats_the_intent_gate_handed_over():
    # The engine passes the intent's formats as explicit_only with the same text.
    d = F.decide("Create XLSX or CSV of the table above.", explicit_only=["xlsx", "csv"])
    assert d.formats == ["xlsx", "csv"] and d.kind == "workbook"
    d = F.decide("Give me a PDF or an Excel", explicit_only=["pdf", "xlsx"])
    assert d.formats == ["pdf"] and d.warnings == []


# ---------------------------------------------------------- best format --


@pytest.mark.parametrize("text,kind,fmts", [
    ("Make the best output for this data.", "workbook", ["xlsx", "csv"]),
    ("Give me the best format for this raw data export", "workbook", ["csv"]),
    ("the best format for this table, with the failures highlighted", "workbook", ["xlsx"]),
    ("the best format for these meeting notes as a report", "document", ["docx", "pdf"]),
    ("Make the best format for this.", "document", ["docx", "pdf"]),
    ("the best deliverable for the board pitch deck", "presentation", ["pptx", "pdf"]),
])
def test_best_format_is_decided_from_the_task_words(text, kind, fmts):
    d = F.decide(text)
    assert (d.kind, d.formats, d.explicit) == (kind, fmts, False), d
    assert d.reason.startswith("best format")


# ------------------------------------------------------ data-only note --


def test_a_csv_with_styling_words_carries_the_data_only_note():
    d = F.decide("Create a CSV with red highlights for failed rows")
    assert d.formats == ["csv", "xlsx"]
    assert d.data_only_note == "the CSV carries the data only; the formatting is in the Excel file"
    assert "+xlsx for the styling" in d.reason
    # The styled format already named: nothing is added, the note names it.
    d = F.decide("Share XLSX and CSV of this audit with bold headers and borders")
    assert d.formats == ["xlsx", "csv"] and d.data_only_note.endswith("in the Excel file")
    d = F.decide("Give me a CSV and a PDF of this table with the failures in red")
    assert d.formats == ["csv", "pdf"] and d.data_only_note.endswith("in the PDF file")
    # No styling words: no note. A forward-fill is data, not styling.
    assert F.decide("Give me a CSV of the leads").data_only_note == ""
    assert F.decide("CSV of this audit; fill blank hosts from the row above").data_only_note == ""
    assert F.decide("CSV of this audit; fill blank hosts from the row above").formats == ["csv"]


# ------------------------------------------------------------ defaults --


@pytest.mark.parametrize("text,kind,fmts,template", [
    # A dataset is data before it is anything else.
    ("Create a CSV dataset containing 500 realistic sample records for an AI project evaluation system.", "workbook", ["csv"], "data"),
    ("Generate 500 sample records for the evaluation", "workbook", ["csv"], "data"),
    ("Create a dataset of 500 customers", "workbook", ["csv"], "data"),
    ("Give me some sample data for the demo", "workbook", ["csv"], "data"),
    # A spreadsheet, tracker or dashboard is the editable file.
    ("Make a KPI dashboard spreadsheet", "workbook", ["xlsx"], "dashboard"),
    ("Make a budget tracker.", "workbook", ["xlsx"], "tracker"),
    ("Build a dashboard from the dataset", "workbook", ["xlsx"], "data"),
    # A dataset asked for WITH styling is a spreadsheet: the styling has to live somewhere.
    ("Generate 200 sample records with the failures highlighted in red", "workbook", ["xlsx"], "data"),
    # A table transform with styling words and no named format.
    ("Turn this audit table into something with bold headers and the failures in red", "workbook", ["xlsx"], "data"),
    # Prose stays prose.
    ("Create an SOP document.", "document", ["docx", "pdf"], "sop"),
    ("Prepare a one-page executive brief.", "document", ["pdf", "docx"], "brief"),
    ("Make a printable handout.", "document", ["pdf"], "generic"),
])
def test_defaults(text, kind, fmts, template):
    d = F.decide(text)
    assert (d.kind, d.formats, d.template_id) == (kind, fmts, template), d


def test_the_reason_names_the_rule():
    assert F.decide("Generate 500 sample records for the evaluation").reason == "dataset words"
    assert F.decide("Share XLSX, Word, PDF, and CSV of this audit.").reason == "explicit: xlsx, docx, pdf, csv"
    assert F.kind_for("Give me a PDF and an Excel of this audit table", ["pdf", "xlsx"]) == ("workbook", "explicit: xlsx (a table, pdf carried)")
    assert F.kind_for("a PDF report, plus an Excel", ["pdf", "xlsx"]) == ("document", "explicit: pdf")
    assert F.kind_for("a PDF report on AI, plus an Excel with the data", ["pdf", "xlsx"]) == ("document", "explicit: pdf")
    # A kind word alone does not make "cvs" a format: only a creation context does.
    assert F.kind_for("the cvs pipeline failed, write up a report", []) == ("document", "document words")
    assert F.kind_for("Turn this audit table into something with bold headers", []) == ("workbook", "table transform")
    assert F.decide("Make a budget tracker.").reason == "spreadsheet words"
