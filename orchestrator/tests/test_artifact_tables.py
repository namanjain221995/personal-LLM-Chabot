"""The table module (app/artifacts/tables.py): pasted tables, the row
generator and the rewrite validator — CONTRACT-2 §4 and §6.

Offline and pure: no database, no model. The fixture `audit_paste.txt` is
a real-shaped paste — 34 audit rows written with tabs, three of them with
runs of spaces, two with a trailing tab, 25 continuation rows without a
host, blank dates and session ids, timestamps and typos in the comments —
and the first three tests pin that NOTHING in it is dropped, merged, shifted
or invented.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.artifacts import tables as X
from app.artifacts import types as T

FIXTURE = Path(__file__).parent / "fixtures" / "audit_paste.txt"
COLUMNS = [
    "Host", "Candidate", "Date", "Session ID", "Meeting ID",
    "Interview Duration (min)", "Ratio of Interview Post-Session", "Outcome", "Audit Comments",
]


@pytest.fixture(scope="module")
def audit() -> X.ParsedTable:
    table = X.parse_table(FIXTURE.read_text(encoding="utf-8"))
    assert table is not None
    return table


# ------------------------------------------------------- the audit paste --


def test_audit_paste_is_34_rows_by_9_columns_with_nothing_dropped(audit):
    assert audit.delimiter == "tab"
    assert audit.columns == COLUMNS
    assert len(audit.rows) == 34
    assert all(len(r) == 9 for r in audit.rows)
    # Header on line 1, one row per following line, in order.
    assert audit.header_row == 1
    assert audit.source_rows == list(range(2, 36))
    # No width mismatch anywhere: the space-run rows and the trailing-tab
    # rows all came out at nine cells without help.
    assert audit.parse_warnings == []
    assert audit.truncated is False and audit.total_rows == 34
    assert audit.prose_before == "" and audit.prose_after == ""


def test_audit_paste_blanks_are_preserved_and_counted(audit):
    by_col = audit.blanks_by_column
    assert by_col["Host"] == 25            # continuation rows
    assert by_col["Date"] == 5
    assert by_col["Session ID"] == 4
    assert by_col["Candidate"] == 0 and by_col["Audit Comments"] == 0
    assert by_col["Meeting ID"] == 2 and by_col["Interview Duration (min)"] == 3
    assert by_col["Ratio of Interview Post-Session"] == 4 and by_col["Outcome"] == 1
    assert audit.blanks == sum(by_col.values()) == 44
    report = audit.report
    assert report["rows"] == 34 and report["columns"] == 9 and report["blanks"] == 44
    assert report["forward_filled"] == 0 and report["warnings"] == [] and report["delimiter"] == "tab"


def test_audit_paste_cells_land_in_their_columns(audit):
    rows = audit.rows
    assert rows[0] == [
        "Ravi Sharma", "Priya Nair", "2026-08-03", "S-1041", "MTG-77812", "42", "0.81", "Selected",
        "candiate was confident, ansered all q's. at 00:12:30 host asked abt system design, good explanation",
    ]
    # Written with runs of spaces; the 8-space gap is the blank Session ID.
    assert rows[4] == [None, "Meera Pillai", "2026-08-04", None, "MTG-77821", "45", "0.72", "Selected",
                       "solid. minor grammer issues but technically strong"]
    assert rows[11] == [None, "Amit Patel", "2026-08-06", "S-1052", "MTG-77841", "31", "0.47", "Rejected",
                        "kept switching topics; host had difficulty to keep on track"]
    assert rows[20] == [None, "Shreya Kulkarni", "2026-08-08", "S-1061", "MTG-77862", "43", "0.79", "Selected",
                        "good all round. recomended"]
    # Trailing tab after the comment: ignored, not a tenth column.
    assert rows[8] == [None, "Neha Gupta", None, None, "MTG-77832", "40", "0.78", "Selected",
                       "good analitical skills, clear about expectations"]
    assert rows[26] == ["Kavita Rao", "Bhavna Sood", "2026-08-11", "S-1067", "MTG-77880", "42", "0.77", "Selected",
                        "steady and reliable answers. host noted good attitude"]
    # Four blanks in one row (no show) stay four blanks.
    assert rows[17] == [None, "Harsh Vardhan", "2026-08-07", None, "MTG-77853", None, None, None,
                        "no show. host waited 15 min"]
    assert rows[33] == [None, "Om Prakash", "2026-08-12", "S-1074", "MTG-77893", "39", "0.68", "On hold",
                        "decent. recheck refrences before final"]


def test_forward_fill_of_host_fills_25_rows_and_records_each(audit):
    filled = X.forward_fill(audit, "Host")
    assert filled is not audit
    assert filled.forward_filled == 25
    assert len(filled.transformations) == 25
    assert filled.transformations[0] == "row 3: host forward-filled from row 2"
    assert all("host forward-filled from row" in t for t in filled.transformations)
    assert filled.blanks_by_column["Host"] == 0
    assert filled.rows[1][0] == "Ravi Sharma"      # Arjun, from Ravi's row
    assert filled.rows[4][0] == "Kavita Rao"       # Meera (space-run row), from Kavita's
    assert filled.rows[33][0] == "Suresh Kumar"    # the last continuation row
    # Everything else is untouched, and the source table is not mutated.
    assert [r[1:] for r in filled.rows] == [r[1:] for r in audit.rows]
    assert audit.rows[1][0] is None and audit.forward_filled == 0 and audit.transformations == []
    assert filled.report["forward_filled"] == 25
    # A column index works too.
    assert X.forward_fill(audit, 0).forward_filled == 25


def test_forward_fill_refuses_without_evidence_or_below_the_threshold(audit):
    # "Date" is not a grouping header: refused by evidence, and even when
    # forced, 5 blanks in 34 rows is under the 30% threshold.
    assert X.forward_fill(audit, "Date").forward_filled == 0
    assert X.forward_fill(audit, "Date", evidence=False).forward_filled == 0
    assert X.forward_fill(audit, "Session ID", evidence=False).forward_filled == 0
    # An unknown column is a no-op copy, never an exception.
    assert X.forward_fill(audit, "Nope").rows == audit.rows
    # A blank run that does not follow a filled cell (blank first row) is
    # refused: there is nothing above to fill from.
    t = X.parse_table("Host\tCandidate\n\tA\n\tB\nRavi\tC\n\tD")
    assert t is not None and t.blanks_by_column["Host"] == 3
    assert X.forward_fill(t, "Host").forward_filled == 0
    # Numbers are not a grouping column even when the header says so.
    t = X.parse_table("Group\tv\n1\ta\n\tb\n2\tc\n\td")
    assert X.forward_fill(t, "Group", evidence=False).forward_filled == 0
    # `evidence=False` forces the fill on an unnamed text column.
    t = X.parse_table("Col\tv\nx\ta\n\tb\ny\tc\n\td")
    assert X.forward_fill(t, "Col").forward_filled == 0
    assert X.forward_fill(t, "Col", evidence=False).rows == [["x", "a"], ["x", "b"], ["y", "c"], ["y", "d"]]


def test_to_material_and_material_table(audit):
    m = audit.to_material("paste1", "IR session audit")
    assert set(m) == {"id", "title", "columns", "rows", "source_rows", "warnings"}
    assert m["id"] == "paste1" and m["title"] == "IR session audit"
    assert m["columns"] == COLUMNS and len(m["rows"]) == 34 and m["rows"][4][3] is None
    assert m["source_rows"] == audit.source_rows and m["warnings"] == []
    m["rows"][0][0] = "changed"
    assert audit.rows[0][0] == "Ravi Sharma"
    dt = audit.to_material_table("paste1")
    assert dt.id == "paste1" and dt.columns == COLUMNS and dt.rows[17][5] is None and dt.source_id == ""


# ---------------------------------------------------- other delimiters --


def test_markdown_table_inside_a_message():
    text = (
        "Here is the summary table from the review, please make it a PDF.\n\n"
        "| Host | Candidate | Outcome |\n"
        "|:-----|-----------|--------:|\n"
        "| Ravi Sharma | Priya Nair | Selected |\n"
        "|  | Arjun Mehta | Rejected |\n"
        "| Kavita Rao | a \\| b | On hold |\n\n"
        "Thanks, keep the blanks."
    )
    t = X.parse_table(text)
    assert t is not None and t.delimiter == "pipe"
    assert t.columns == ["Host", "Candidate", "Outcome"]
    assert t.rows == [["Ravi Sharma", "Priya Nair", "Selected"], [None, "Arjun Mehta", "Rejected"], ["Kavita Rao", "a | b", "On hold"]]
    assert t.source_rows == [5, 6, 7]          # the rule line is not a row
    assert t.prose_before == "Here is the summary table from the review, please make it a PDF."
    assert t.prose_after == "Thanks, keep the blanks."
    assert t.parse_warnings == []
    # Without outer pipes too.
    t = X.parse_table("a | b\n--|--\n1 | 2\n3 |")
    assert t.rows == [["1", "2"], ["3", None]]


def test_comma_csv_with_quotes_and_embedded_newlines():
    text = (
        "Please turn this into a spreadsheet, thanks\n"
        "id,name,comment,score\n"
        '1,"Smith, John","line one\nline two",42\n'
        "2,Priya,,0.5\n"
        '3,"Neha ""N"" Gupta",fine,\n'
        "Regards, Dev"
    )
    t = X.parse_table(text)
    assert t is not None and t.delimiter == "comma"
    assert t.columns == ["id", "name", "comment", "score"]
    assert t.rows == [
        ["1", "Smith, John", "line one\nline two", "42"],
        ["2", "Priya", None, "0.5"],
        ["3", 'Neha "N" Gupta', "fine", None],
    ]
    assert t.source_rows == [3, 5, 6]          # the two-line record starts on line 3
    assert t.prose_before == "Please turn this into a spreadsheet, thanks"
    assert t.prose_after == "Regards, Dev"
    assert t.parse_warnings == []
    # Semicolons, via the sniffer.
    t = X.parse_table("a;b;c\n1;2;3\n4;5;6\n7;;9")
    assert t.delimiter == "semicolon" and t.rows == [["1", "2", "3"], ["4", "5", "6"], ["7", None, "9"]]


def test_two_space_aligned_table_keeps_the_blank_first_cell():
    text = (
        "Host          Candidate      Date\n"
        "Ravi Sharma   Priya Nair     2026-08-03\n"
        "              Arjun Mehta    2026-08-03\n"
        "Kavita Rao    Deepak Joshi   2026-08-04\n"
    )
    t = X.parse_table(text)
    assert t is not None and t.delimiter == "spaces"
    assert t.columns == ["Host", "Candidate", "Date"]
    assert t.rows == [
        ["Ravi Sharma", "Priya Nair", "2026-08-03"],
        [None, "Arjun Mehta", "2026-08-03"],
        ["Kavita Rao", "Deepak Joshi", "2026-08-04"],
    ]
    assert t.parse_warnings == []


def test_prose_before_and_after_a_tab_table_survives():
    text = (
        "Make a PDF of this:\n"
        "Host\tCandidate\n"
        "Ravi\tPriya\n"
        "\tArjun\n"
        "lonely line\n"
        "Kavita\tDeepak\n"
        "Keep the blanks please."
    )
    t = X.parse_table(text)
    assert t is not None
    assert t.prose_before == "Make a PDF of this:"
    assert t.prose_after == "Keep the blanks please."
    assert t.source_rows == [3, 4, 5, 6]
    # The line without any separator is a row in the first column, once
    # warned, never dropped.
    assert t.rows == [["Ravi", "Priya"], [None, "Arjun"], ["lonely line", None], ["Kavita", "Deepak"]]
    assert t.parse_warnings == ["row 5 has no separator; kept as one cell in the first column"]


def test_space_run_lines_are_rows_of_a_tab_table_but_never_its_header():
    # Four consecutive rows typed with spaces inside a tab table do not
    # split the block (they are row-like), and each lands by position.
    text = (
        "Host\tCandidate\tDate\n"
        "Ravi\tPriya\t2026-08-03\n"
        "    Arjun    2026-08-03\n"
        "    Sneha    2026-08-03\n"
        "    Deepak    2026-08-04\n"
        "    Meera    2026-08-04\n"
        "Kavita\tRohan\t2026-08-04"
    )
    t = X.parse_table(text)
    assert len(t.rows) == 6 and t.parse_warnings == []
    assert t.rows[2] == [None, "Sneha", "2026-08-03"] and t.rows[5] == ["Kavita", "Rohan", "2026-08-04"]
    # A prose line with a double space next to the table stays prose: only
    # a tab line can open or close the block.
    text = "Here is the audit.  Please make a PDF:\nHost\tCandidate\nRavi\tPriya\n\tArjun\nThanks.  Bye."
    t = X.parse_table(text)
    assert t.columns == ["Host", "Candidate"] and t.rows == [["Ravi", "Priya"], [None, "Arjun"]]
    assert t.prose_before == "Here is the audit.  Please make a PDF:" and t.prose_after == "Thanks.  Bye."


def test_width_mismatches_are_padded_or_joined_and_recorded():
    t = X.parse_table("a\tb\tc\n1\t2\t3\t4\t\n5\t6\n")
    assert t.rows == [["1", "2", "3 4"], ["5", "6", None]]
    assert t.parse_warnings == [
        "row 2 has 4 cells for 3 columns; the extra cells were joined into the last column",
        "row 3 has 2 cells for 3 columns",
    ]
    # A blank LAST cell with the right width is a blank, not a mismatch.
    t = X.parse_table("a\tb\tc\n1\t2\t\n")
    assert t.rows == [["1", "2", None]] and t.parse_warnings == []
    # Blank and repeated headers are named so every column can be referred to.
    t = X.parse_table("a\t\ta\n1\t2\t3")
    assert t.columns == ["a", "Column 2", "a (2)"] and len(t.parse_warnings) == 2


def test_excel_style_quoted_multiline_cell_in_a_tab_paste_is_kept_as_rows():
    """CONTRACT-2 §6 ("every line → one row … never dropped") and the wave
    2c addendum: in a TAB paste a quote never crosses a line. The first
    version of this test pinned the join (two lines merged into one cell);
    the same reader merged 31 rows into 11 on an inch mark, so the pin is
    now the other way: each line is a row, the continuation is warned."""
    text = 'Host\tComment\nRavi\t"multi\nline ""quoted"" cell"\nKavita\t5" screen\n'
    t = X.parse_table(text)
    assert t.rows == [["Ravi", '"multi'], ['line ""quoted"" cell"', None], ["Kavita", '5" screen']]
    assert t.source_rows == [2, 3, 4]
    assert t.parse_warnings == ["row 3 looks like a continuation of the comment above; kept as its own row"]
    # A cell wrapped in a MATCHING pair of quotes is unwrapped; an unmatched
    # one is text; and a tab is a cell boundary even inside quotes
    # (QUOTE_NONE semantics — the clipboard is not a CSV).
    t = X.parse_table('a\tb\n"x y"\t"z"\n"1\t2\n')
    assert t.rows == [["x y", "z"], ['"1', "2"]] and t.parse_warnings == []


def test_an_inch_mark_never_swallows_the_rows_after_it():
    """The verifier's finding on wave 2a: `Ravi\t"27 monitor requested` —
    a straight double quote that is an inch mark, not a quote — made the
    csv-style reader join up to twenty following lines into one record,
    and a 31-row paste came back as 11 rows with no warning."""
    lines = ["Name\tRequest"]
    for i in range(1, 32):
        lines.append(f"Person {i}\t" + ('"27 monitor requested' if i == 9 else f"item {i}"))
    t = X.parse_table("\n".join(lines))
    assert t is not None and len(t.rows) == 31 and t.parse_warnings == []
    assert t.rows[8] == ["Person 9", '"27 monitor requested'] and t.rows[30] == ["Person 31", "item 31"]
    assert t.source_rows == list(range(2, 33))
    # Several inch marks, one per line: still one row each.
    text = "Item\tSize\n" + "\n".join(f'Monitor {i}\t{20 + i}" wide' for i in range(1, 25))
    t = X.parse_table(text)
    assert len(t.rows) == 24 and t.rows[3] == ["Monitor 4", '24" wide'] and t.parse_warnings == []


def test_a_tab_or_pipe_table_inside_prose_needs_three_lines_but_a_bare_one_needs_two():
    """Wave 2c addendum: inside a message the tab and pipe blocks need
    three delimited lines like the other delimiters (a sentence with one
    tab in it and a line after it is not a table); when the WHOLE text is
    the table, a header and one row is a table."""
    assert X.parse_table("a\tb\n1\t2").rows == [["1", "2"]]
    assert X.parse_table("a | b\n1 | 2").rows == [["1", "2"]]
    assert X.parse_table("Here is the data:\na\tb\n1\t2") is None
    assert X.parse_table("Here is the data:\na | b\n1 | 2") is None
    assert X.parse_table("Here is the data:\na\tb\n1\t2\n3\t4").rows == [["1", "2"], ["3", "4"]]
    assert X.parse_table("Here is the data:\na | b\n1 | 2\n3 | 4").rows == [["1", "2"], ["3", "4"]]


def test_a_200k_character_cell_does_not_crash_the_reader():
    """csv.reader's default field limit (131,072) is raised process-wide
    (wave 2c addendum): a pasted comment longer than that is a cell."""
    big = "y" * 200_000
    t = X.parse_table("a,b,c\n1,2,3\n4,5,6\n7,8," + big)
    assert t is not None and t.rows[2][2] == big


def test_prose_and_too_little_are_not_tables():
    assert X.parse_table("") is None
    assert X.parse_table(None) is None
    assert X.parse_table("just one line") is None
    assert X.parse_table("a\tb") is None                          # header, no rows
    assert X.parse_table("Hello, how are you?\nI am fine, thanks.\nPlease, help me.") is None
    assert X.parse_table("Dear team, here is the update\nWe met, discussed and agreed\nMore later, thanks") is None
    assert X.parse_table("First sentence here.  Second one follows.\nAnother line here.  And more text after.\nThird line, yes.  Done now.") is None
    assert X.parse_table("only\tone\tline\nno tab here") is None      # one delimited line is not a table
    # A narrow comma table is trusted only with a few rows behind it.
    assert X.parse_table("id,value\n1,2\n3,4") is None
    assert X.parse_table("id,value\n1,2\n3,4\n5,6\n7,8").rows == [["1", "2"], ["3", "4"], ["5", "6"], ["7", "8"]]


def test_the_10k_row_cap_is_reported_not_silently_applied():
    text = "id\tv\n" + "\n".join(f"{i}\t{i * 2}" for i in range(X.MAX_PASTE_ROWS + 1))
    t = X.parse_table(text)
    assert t is not None and t.truncated is True
    assert len(t.rows) == X.MAX_PASTE_ROWS and t.total_rows == X.MAX_PASTE_ROWS + 1
    assert t.rows[-1] == [str(X.MAX_PASTE_ROWS - 1), str((X.MAX_PASTE_ROWS - 1) * 2)]
    assert t.report["truncated"] is True and t.report["total_rows"] == X.MAX_PASTE_ROWS + 1
    # Exactly at the cap is fine.
    text = "id\tv\n" + "\n".join(f"{i}\t{i}" for i in range(X.MAX_PASTE_ROWS))
    t = X.parse_table(text)
    assert t.truncated is False and len(t.rows) == X.MAX_PASTE_ROWS
    # The byte cap marks the table truncated too, and total_rows counts the
    # ORIGINAL paste's rows, not the rows that survived the cut.
    text = "id\tv\n" + "\n".join(f"{i}\t{'x' * 600}" for i in range(9_500))
    assert len(text) > X.MAX_PASTE_BYTES
    t = X.parse_table(text)
    assert t is not None and t.truncated is True and len(t.rows) < 9_500
    assert t.total_rows == 9_500 and t.report["total_rows"] == 9_500
    assert all(len(r) == 2 and r[1] == "x" * 600 for r in t.rows), "the cut lands on a line boundary: no half row"
    # The cap is in UTF-8 BYTES (wave 2c addendum): a paste under the cap
    # in characters and over it in bytes is truncated too.
    text = "id\tv\n" + "\n".join(f"{i}\t{'€' * 600}" for i in range(3_000))
    assert len(text) < X.MAX_PASTE_BYTES < len(text.encode("utf-8"))
    t = X.parse_table(text)
    assert t is not None and t.truncated is True and len(t.rows) < 3_000 and t.total_rows == 3_000
    assert all(r[1] == "€" * 600 for r in t.rows)


# --------------------------------------------------------------- helpers --


@pytest.mark.parametrize("raw, iso", [
    ("2026-08-03", "2026-08-03"),
    ("2026/08/03", "2026-08-03"),
    ("03/08/2026", "2026-08-03"),      # day-first (the local convention)
    ("13/08/2026", "2026-08-13"),      # only day-first is possible
    ("08/13/2026", "2026-08-13"),      # only month-first is possible
    ("3 Aug 2026", "2026-08-03"),
    ("3rd August 2026", "2026-08-03"),
    ("Aug 3, 2026", "2026-08-03"),
    ("August 3 2026", "2026-08-03"),
    ("3-Aug-2026", "2026-08-03"),
])
def test_normalise_date_accepts_the_common_spellings(raw, iso):
    assert X.normalise_date(raw) == iso


@pytest.mark.parametrize("raw", ["31/02/2026", "2026-08-03 10:30", "n/a", "", "S-1041", "yesterday", "00:12:30"])
def test_normalise_date_leaves_the_rest_alone(raw):
    assert X.normalise_date(raw) == raw


def test_normalise_date_month_first_on_request():
    assert X.normalise_date("03/08/2026", day_first=False) == "2026-03-08"
    assert X.normalise_date(None) is None and X.normalise_date(42) == 42


@pytest.mark.parametrize("raw, number", [
    ("42", 42), ("0.81", 0.81), (" 1,234.50 ", 1234.5), ("12%", 12), ("₹ 12,00,000", 1200000),
    ("$ 99.99", 99.99), ("Rs. 500", 500), ("(1,200)", -1200), ("+7", 7), ("-3.5", -3.5),
    ("1e5", 100000.0), (".5", 0.5), ("1 234", 1234), ("1 234 567.5", 1234567.5), (7, 7), (2.5, 2.5),
])
def test_parse_number_reads_formatted_numbers(raw, number):
    got = X.parse_number(raw)
    assert got == number and type(got) is type(number)


@pytest.mark.parametrize("raw", ["", "  ", "-", "—", "n/a", "NA", "none", None, "S-1041", "00:12:30", "2026-08-03", "3.14.15", "1.5k", "3 4", "12 Aug", True])
def test_parse_number_never_turns_a_blank_or_a_label_into_zero(raw):
    assert X.parse_number(raw) is None


def test_timestamps_in():
    text = "at 00:12:30 host asked; (00:41:00 - 00:46:00) and 9:05 but not 1:5 or 12:345 or 0.81, again 00:12:30"
    assert X.timestamps_in(text) == ["00:12:30", "00:41:00", "00:46:00", "9:05", "00:12:30"]
    assert X.timestamps_in("") == [] and X.timestamps_in(None) == []


def test_quoted_spans_ignores_apostrophes():
    text = """said "I am ready", ansered all q's. host said 'good' and “fine” and ‘ok’ then the candidate's turn"""
    assert X.quoted_spans(text) == ["I am ready", "good", "fine", "ok"]
    assert X.quoted_spans("nothing quoted, the host's note") == []


# ------------------------------------------------------------- generator --

GENERATOR = {
    "rows": 500,
    "seed": 42,
    "columns": [
        {"name": "Candidate ID", "kind": "id", "pattern": "CAND-{n:04d}"},
        {"name": "Name", "kind": "name", "unique": True},
        {"name": "Email", "kind": "email", "from": "Name"},
        {"name": "Outcome", "kind": "choice", "values": ["Selected", "Rejected", "On hold"], "weights": [5, 3, 2]},
        {"name": "Score", "kind": "int", "min": 40, "max": 100},
        {"name": "Ratio", "kind": "float", "min": 0.3, "max": 0.95, "decimals": 2},
        {"name": "Interview Date", "kind": "date", "start": "2026-08-01", "end": "2026-08-31"},
        {"name": "Logged At", "kind": "datetime", "start": "2026-08-01", "end": "2026-08-31"},
        {"name": "Start Date", "kind": "date", "start": "2026-09-01", "end": "2026-12-31",
         "only_when": {"column": "Outcome", "in": ["Selected"]}},
        {"name": "Comment", "kind": "text", "text": {"pool": ["good", "weak on basics", "strong"]}},
        {"name": "Bonus", "kind": "int", "min": 1, "max": 5, "only_when": {"column": "Outcome", "in": ["Selected"]}},
        {"name": "Total", "kind": "derived", "derived": {"op": "sum", "columns": ["Score", "Bonus"]}},
        {"name": "Label", "kind": "derived", "derived": {"op": "concat", "columns": ["Candidate ID", "Name"], "sep": " / "}},
    ],
}


def test_generate_rows_makes_exactly_500_validated_rows():
    rows, report = X.generate_rows(GENERATOR)
    assert len(rows) == 500 and all(len(r) == 13 for r in rows)
    assert report["rows"] == 500 and report["seed"] == 42
    assert report["unique_checked"] == ["Candidate ID", "Name", "Email"]
    ids = [r[0] for r in rows]
    assert ids[0] == "CAND-0001" and ids[-1] == "CAND-0500" and len(set(ids)) == 500
    assert len({r[1] for r in rows}) == 500 and all(" " in r[1] for r in rows)
    assert len({r[2] for r in rows}) == 500
    assert all(r[2].endswith(("@example.com", "@example.org", "@example.net")) for r in rows)
    assert rows[0][2].split("@")[0] == rows[0][1].lower().replace(" ", ".")
    assert {r[3] for r in rows} <= {"Selected", "Rejected", "On hold"}
    assert all(40 <= r[4] <= 100 and isinstance(r[4], int) for r in rows)
    assert all(0.3 <= r[5] <= 0.95 and round(r[5], 2) == r[5] for r in rows)
    assert all("2026-08-01" <= r[6] <= "2026-08-31" for r in rows)
    assert all("2026-08-01 00:00:00" <= r[7] <= "2026-08-31 23:59:59" and len(r[7]) == 19 for r in rows)
    assert {r[9] for r in rows} <= {"good", "weak on basics", "strong"}
    assert all(r[12] == f"{r[0]} / {r[1]}" for r in rows)


def test_generate_rows_only_when_blanks_and_derived_follow_them():
    rows, report = X.generate_rows(GENERATOR)
    selected = [r for r in rows if r[3] == "Selected"]
    others = [r for r in rows if r[3] != "Selected"]
    assert selected and others
    assert all("2026-09-01" <= r[8] <= "2026-12-31" for r in selected)
    assert all(1 <= r[10] <= 5 and r[11] == r[4] + r[10] for r in selected)
    assert all(r[8] is None and r[10] is None and r[11] is None for r in others)
    assert report["blanks_by_rule"] == {"Start Date": len(others), "Bonus": len(others), "Total": len(others)}


def test_generate_rows_is_deterministic_per_seed():
    a, _ = X.generate_rows(GENERATOR)
    b, _ = X.generate_rows(GENERATOR)
    c, _ = X.generate_rows({**GENERATOR, "seed": 7})
    d, report = X.generate_rows({k: v for k, v in GENERATOR.items() if k != "seed"})
    assert a == b
    assert a != c
    assert d == a and report["seed"] == 42        # the default seed is 42


def test_generate_rows_derived_ops_and_blank_inputs():
    gen = {
        "rows": 4, "seed": 1,
        "columns": [
            {"name": "a", "kind": "choice", "values": [10]},
            {"name": "b", "kind": "choice", "values": [4]},
            {"name": "c", "kind": "int", "min": 1, "max": 1, "only_when": {"column": "a", "in": [99]}},
            {"name": "mean", "kind": "derived", "derived": {"op": "mean", "columns": ["a", "b"]}},
            {"name": "min", "kind": "derived", "derived": {"op": "min", "columns": ["a", "b"]}},
            {"name": "max", "kind": "derived", "derived": {"op": "max", "columns": ["a", "b"]}},
            {"name": "diff", "kind": "derived", "derived": {"op": "diff", "columns": ["a", "b"]}},
            {"name": "sum_blank", "kind": "derived", "derived": {"op": "sum", "columns": ["a", "c"]}},
            {"name": "cat", "kind": "derived", "derived": {"op": "concat", "columns": ["a", "b"], "sep": "-"}},
        ],
    }
    rows, report = X.generate_rows(gen)
    assert rows == [[10, 4, None, 7.0, 4, 10, 6, None, "10-4"]] * 4
    assert report["blanks_by_rule"] == {"c": 4, "sum_blank": 4}


def test_generate_rows_unique_and_range_rules():
    rows, report = X.generate_rows({"rows": 50, "seed": 3, "columns": [
        {"name": "n", "kind": "int", "min": 1, "max": 50, "unique": True},
        {"name": "d", "kind": "date", "start": "2026-01-01", "end": "2026-03-01", "unique": True},
        {"name": "f", "kind": "float", "min": 0, "max": 1, "decimals": 3, "unique": True},
        {"name": "t", "kind": "text", "pool": [f"t{i}" for i in range(50)], "unique": True},
        {"name": "c", "kind": "choice", "values": list(range(60)), "unique": True},
        {"name": "e", "kind": "email"},
    ]})
    assert sorted(r[0] for r in rows) == list(range(1, 51))
    for j in range(6):
        assert len({r[j] for r in rows}) == 50
    assert report["unique_checked"] == ["n", "d", "f", "t", "c", "e"]
    # An email column with no `from` makes its own names, still unique.
    assert all("@" in r[5] for r in rows)
    # Duplicate names get distinct addresses.
    rows, _ = X.generate_rows({"rows": 3, "columns": [
        {"name": "n", "kind": "choice", "values": ["Ravi Sharma"]},
        {"name": "e", "kind": "email", "from": "n", "domains": ["example.com"]},
    ]})
    assert [r[1] for r in rows] == ["ravi.sharma@example.com", "ravi.sharma2@example.com", "ravi.sharma3@example.com"]


@pytest.mark.parametrize("generator, message", [
    ({"rows": 0, "columns": [{"name": "a", "kind": "int"}]}, "between 1 and"),
    ({"rows": T.MAX_ROWS_PER_SHEET + 1, "columns": [{"name": "a", "kind": "int"}]}, "between 1 and"),
    ({"rows": "5", "columns": [{"name": "a", "kind": "int"}]}, "between 1 and"),
    ({"rows": 6000, "columns": [{"name": "n", "kind": "name", "unique": True}]}, "cannot make 6000 unique names from the pool"),
    ({"rows": 3, "columns": [{"name": "a", "kind": "wat"}]}, "kind must be one of"),
    ({"rows": 3, "columns": []}, "at least one column"),
    ({"rows": 3, "columns": [{"name": "a", "kind": "int"}, {"name": "a", "kind": "int"}]}, "repeats"),
    ({"rows": 5, "columns": [{"name": "a", "kind": "choice", "values": ["x", "y"], "unique": True}]}, "cannot pick 5 unique values"),
    ({"rows": 3, "columns": [{"name": "a", "kind": "choice", "values": []}]}, "non-empty values"),
    ({"rows": 3, "columns": [{"name": "a", "kind": "id", "pattern": "X"}]}, "must contain {n}"),
    ({"rows": 3, "columns": [{"name": "a", "kind": "int", "min": 5, "max": 1}]}, "above max"),
    ({"rows": 3, "columns": [{"name": "a", "kind": "date", "start": "2026-02-01", "end": "2026-01-01"}]}, "after end"),
    ({"rows": 3, "columns": [{"name": "a", "kind": "int", "only_when": {"column": "zz", "in": [1]}}]}, "unknown column"),
    ({"rows": 3, "columns": [
        {"name": "a", "kind": "derived", "derived": {"op": "sum", "columns": ["b"]}},
        {"name": "b", "kind": "derived", "derived": {"op": "sum", "columns": ["a"]}}]}, "cycle"),
    ({"rows": 3, "columns": [{"name": "a", "kind": "derived", "derived": {"op": "pow", "columns": ["a"]}}]}, "derived must be"),
    ({"rows": 3, "columns": [{"name": "a", "kind": "text"}]}, "pool"),
])
def test_generate_rows_refuses_bad_rules_by_name(generator, message):
    with pytest.raises(ValueError, match=message):
        X.generate_rows(generator)


def test_name_pools_are_large_enough():
    assert len(X.INDIAN_FIRST_NAMES) >= 60 and len(set(X.INDIAN_FIRST_NAMES)) == len(X.INDIAN_FIRST_NAMES)
    assert len(X.INDIAN_LAST_NAMES) >= 40 and len(set(X.INDIAN_LAST_NAMES)) == len(X.INDIAN_LAST_NAMES)
    assert len(X.INTERNATIONAL_FIRST_NAMES) >= 40 and len(set(X.INTERNATIONAL_FIRST_NAMES)) == len(X.INTERNATIONAL_FIRST_NAMES)
    assert len(X.INTERNATIONAL_LAST_NAMES) >= 40 and len(set(X.INTERNATIONAL_LAST_NAMES)) == len(X.INTERNATIONAL_LAST_NAMES)
    rows, _ = X.generate_rows({"rows": 2000, "columns": [{"name": "n", "kind": "name", "region": "indian", "unique": True}]})
    assert len({r[0] for r in rows}) == 2000


# --------------------------------------------------------------- rewrite --

ROWS = [
    ["a", 'candiate was confident at 00:12:30, said "I am ready"'],
    ["b", None],
    ["c", "good"],
    ["d", "  "],
    ["e", "host waited 15 min (00:27:00)"],
]


def test_rewrite_batches_skip_blanks_and_respect_the_batch_size():
    batches = X.rewrite_batches(ROWS, 1, batch_size=2)
    assert batches == [
        [(0, 'candiate was confident at 00:12:30, said "I am ready"'), (2, "good")],
        [(4, "host waited 15 min (00:27:00)")],
    ]
    assert X.rewrite_batches(ROWS, 1) == [batches[0] + batches[1]]
    assert X.rewrite_batches([], 1) == []
    with pytest.raises(ValueError):
        X.rewrite_batches(ROWS, 1, batch_size=0)


def test_apply_rewrites_accepts_only_faithful_replies():
    out, kept, warnings = X.apply_rewrites(ROWS, 1, {
        0: 'The candidate was confident at 00:12:30 and said "I am ready".',
        2: "Performed well overall.",
        3: "must not land on a blank row",
        4: "The host waited 15 minutes (00:27:00).",
    })
    assert out == [
        ["a", 'The candidate was confident at 00:12:30 and said "I am ready".'],
        ["b", None],
        ["c", "Performed well overall."],
        ["d", "  "],                      # blank stays blank
        ["e", "The host waited 15 minutes (00:27:00)."],
    ]
    assert kept == [] and warnings == []
    assert len(out) == len(ROWS)
    assert ROWS[0][1].startswith("candiate")   # the input is not mutated


def test_apply_rewrites_keeps_the_original_when_a_reply_breaks_a_rule():
    out, kept, warnings = X.apply_rewrites(ROWS, 1, {
        "0": 'The candidate was confident and said "I am ready".',   # timestamp lost (string key accepted)
        2: "",                                                        # empty
        4: "x" * 500,                                                 # 3x the original, well over
    })
    assert out == ROWS and kept == [0, 2, 4]
    assert warnings == [
        "row 1: kept the original (timestamp 00:12:30 missing)",
        "row 3: kept the original (empty rewrite)",
        "row 5: kept the original (rewrite is 500 characters for an original of 29)",
    ]
    # A lost quoted span is a rejection too; a missing reply keeps the
    # original and is summarised once, not once per row.
    out, kept, warnings = X.apply_rewrites(ROWS, 1, {0: "Confident at 00:12:30, said they were ready."})
    assert out == ROWS and kept == [0, 2, 4]
    assert warnings == [
        "row 1: kept the original (quoted text 'I am ready' missing)",
        "2 row(s) had no rewrite and keep the original: rows 3, 5",
    ]
    # The length rule is 3x the original with a floor, so a one-word
    # comment can still become a sentence.
    out, kept, warnings = X.apply_rewrites(ROWS, 1, {2: "Candidate performed well across the interview."})
    assert out[2][1] == "Candidate performed well across the interview." and 2 not in kept
    out, kept, warnings = X.apply_rewrites(ROWS, 1, {2: "x" * 61})
    assert 2 in kept
    out, kept, warnings = X.apply_rewrites(ROWS, 1, {2: "x" * 12}, floor=0)
    assert 2 not in kept
    out, kept, warnings = X.apply_rewrites(ROWS, 1, {2: "x" * 13}, floor=0)
    assert 2 in kept
