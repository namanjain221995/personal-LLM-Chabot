"""The CSV writer, its reopen (validate_csv) and the grid reader: RFC 4180
bytes (CRLF, minimal quoting, UTF-8 with NO byte-order mark), the formula
neutralisation policy shared with the XLSX writer, the exactly-N-rows
guarantee, and a page of a 1,200-row file read without loading it. Every
assertion on a file reopened — with the stdlib csv module, or on the bytes.
"""
from __future__ import annotations

import csv
import datetime as dt
import random

import pytest

from app.artifacts import types as T
from app.artifacts.render import csv as C
from app.artifacts.render import validate, xlsx

HEADER = ["Id", "Name", "Amount", "Note"]

# Indian names, a rupee amount with a thousands separator, an emoji, an
# em-dash, and a Tamil string: the characters a BOM-less UTF-8 file must
# carry unchanged.
UNICODE_ROWS = [
    ["CAND-0001", "Ramaswamy Iyer", "₹1,20,000", "Cleared — round 2 😀"],
    ["CAND-0002", "Priyanka Deshmukh", "₹95,500", "தமிழ் text in a cell"],
    ["CAND-0003", "José Muñoz-Álvarez", "€1.234,56", "naïve café"],
]


def _read(path):
    """Every record as the stdlib reads it — newline='' so a quoted newline
    is one field, not two records."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.reader(fh))


def _generated(n: int, seed: int = 42):
    """n deterministic rows in the shape a generator sheet produces: an id
    pattern, a name from a pool, an integer, a float, a choice, a date."""
    rng = random.Random(seed)
    first = ["Aarav", "Diya", "Ishaan", "Meera", "Rohan", "Sneha", "Kabir", "Ananya", "Liam", "Olivia"]
    last = ["Sharma", "Patel", "Iyer", "Reddy", "Khan", "Mehta", "Nair", "Bose", "Smith", "García"]
    rows = []
    for i in range(1, n + 1):
        rows.append([
            f"CAND-{i:04d}",
            f"{rng.choice(first)} {rng.choice(last)}",
            rng.randint(18, 65),
            round(rng.uniform(1_000, 99_999), 2),
            rng.choice(["Selected", "On hold", "Rejected"]),
            (dt.date(2026, 1, 1) + dt.timedelta(days=rng.randint(0, 240))).isoformat(),
        ])
    return rows


GEN_HEADER = ["Id", "Name", "Age", "Salary", "Status", "Joined"]


# ------------------------------------------------------------- the bytes --


def test_round_trip_commas_quotes_newlines_unicode_blanks(tmp_path):
    rows = [
        ["1", 'He said "hello"', "a,b,c", "line one\nline two"],
        ["2", "", None, "trailing space "],
        ["3", "crlf\r\ninside", "tab\there", "plain"],
    ] + UNICODE_ROWS
    path = tmp_path / "rt.csv"
    report = C.write_csv(HEADER, rows, path)
    assert report["rows"] == 6 and report["columns"] == 4
    assert report["neutralised"] == 0 and report["neutralised_cells"] == []
    assert report["size"] == path.stat().st_size and len(report["sha256"]) == 64

    records = _read(path)
    assert records[0] == HEADER
    # None and "" both come back as the empty field; everything else is
    # exactly what went in, including the embedded newlines and CRLF.
    assert records[1] == ["1", 'He said "hello"', "a,b,c", "line one\nline two"]
    assert records[2] == ["2", "", "", "trailing space "]
    assert records[3] == ["3", "crlf\r\ninside", "tab\there", "plain"]
    assert records[4:] == UNICODE_ROWS
    assert len(records) == 7  # a quoted newline never becomes a record


def test_bytes_are_crlf_minimal_quoting_and_no_bom(tmp_path):
    path = tmp_path / "bytes.csv"
    C.write_csv(["A", "B"], [["plain", "with,comma"], ["say \"hi\"", "two\nlines"], ["₹", "😀"]], path)
    raw = path.read_bytes()
    # The trade-off the module docstring states: no BOM, and pinned here.
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.startswith(b"A,B\r\n")
    # CRLF between records only; the one LF inside a field is quoted.
    body = raw.split(b"\r\n")
    assert body[-1] == b""  # the file ends with CRLF
    assert body[1] == b"plain,\"with,comma\""
    assert body[2] == b"\"say \"\"hi\"\"\",\"two\nlines\""
    assert body[3] == "₹,😀".encode("utf-8")
    # Bare LF never separates records.
    assert raw.count(b"\n") == raw.count(b"\r\n") + 1
    assert raw.decode("utf-8")  # strict decode


def test_written_file_reads_back_identically_cell_for_cell(tmp_path):
    """500 generated rows through write_csv and the stdlib reader: every
    cell equals the text of the value that went in."""
    rows = _generated(500)
    path = tmp_path / "same.csv"
    C.write_csv(GEN_HEADER, rows, path)
    records = _read(path)
    assert records[0] == GEN_HEADER
    assert len(records) == 501
    for original, read_back in zip(rows, records[1:]):
        assert read_back == [C.cell_text(v) for v in original]
        assert read_back[0] == original[0] and read_back[2] == str(original[2])
        assert float(read_back[3]) == original[3]


# --------------------------------------------------------- number policy --


def test_integral_floats_are_integers_and_others_are_positional(tmp_path):
    path = tmp_path / "nums.csv"
    rows = [[7080.0, 7080.5, -2.0, 0.1 + 0.2], [1e21, -1e-05, 12, 1234567.0]]
    C.write_csv(["a", "b", "c", "d"], rows, path)
    records = _read(path)
    assert records[1] == ["7080", "7080.5", "-2", "0.30000000000000004"]
    # No exponent notation: a reader that guesses types keeps the column numeric.
    assert records[2] == ["1000000000000000000000", "-0.00001", "12", "1234567"]


def test_none_and_non_finite_are_empty_fields_and_bools_dates_are_text(tmp_path):
    path = tmp_path / "special.csv"
    rows = [[None, float("nan"), float("inf"), float("-inf")], [True, False, dt.date(2026, 9, 12), dt.datetime(2026, 9, 12, 14, 30)]]
    C.write_csv(["a", "b", "c", "d"], rows, path)
    records = _read(path)
    assert records[1] == ["", "", "", ""]
    assert records[2] == ["True", "False", "2026-09-12", "2026-09-12T14:30:00"]
    # "-inf" would have been a formula lead; it never reaches the file.
    assert C.validate_csv(path)["rows"] == 2


def test_column_types_strip_thousands_separators_only_for_numeric_columns(tmp_path):
    path = tmp_path / "typed.csv"
    rows = [["1,000", "1,000", "7,080.50", "12%", "1,000"]]
    C.write_csv(["Text", "Int", "Cur", "Pct", "Date"], rows, path,
                column_types=["text", "integer", "currency", "percent", "date"])
    assert _read(path)[1] == ["1,000", "1000", "7080.5", "12%", "1,000"]
    with pytest.raises(ValueError, match="column types"):
        C.write_csv(["a", "b"], [["1", "2"]], tmp_path / "bad.csv", column_types=["text"])


# --------------------------------------------------- formula neutralisation --


def test_formula_leads_are_neutralised_and_numbers_are_not(tmp_path):
    hostile = ["=SUM(A1)", "+cmd|' /C calc'!A0", "-2+3", "@SUM(A1:A9)", "\tTab lead", "\rCR lead", '=HYPERLINK("http://evil.example/x")']
    benign = ["-2", "+3.5", "1,000", "12%", "-12%", "+1,234.50", "plain", "'already quoted", "a=b"]
    rows = [[h, b] for h, b in zip(hostile, benign)]
    path = tmp_path / "formulas.csv"
    report = C.write_csv(["Hostile", "Benign"], rows, path)
    assert report["neutralised"] == len(hostile)
    # (row, column) as a spreadsheet numbers them: header row 1, data from 2.
    assert report["neutralised_cells"] == [(i + 2, 1) for i in range(len(hostile))]
    records = _read(path)
    for i, (h, b) in enumerate(zip(hostile, benign), start=1):
        assert records[i][0] == "'" + h, h
        assert records[i][1] == b, b
    # The bytes: the apostrophe is IN the field, visible to every consumer.
    assert b"\r\n'=SUM(A1),-2\r\n" in path.read_bytes()
    facts = C.validate_csv(path)
    assert facts["ok"] and facts["rows"] == len(hostile)


def test_neutralisation_policy_matches_the_xlsx_writer():
    """One policy, two writers: the leads are the XLSX writer's tuple, and a
    plain number that starts with a sign is a number in both."""
    assert C.FORMULA_LEADS == xlsx._FORMULA_LEADS
    for text in ("=1", "+cmd", "-x", "@y", "\tz", "\rw"):
        assert xlsx.is_formula_like(text) and C.is_formula_lead(text), text
    for text in ("-2", "+3.5", "-1,000", "+12%", "-0.5"):
        assert C.is_formula_lead(text) is False, text
    assert C.neutralise("-") == "'-"  # a lone dash is not a number by the pinned regex
    assert C.neutralise("plain") == "plain"


def test_header_cells_follow_the_same_policy_and_report_row_one(tmp_path):
    path = tmp_path / "header.csv"
    report = C.write_csv(["=cmd", "Name"], [["1", "2"]], path)
    assert report["neutralised"] == 1 and report["neutralised_cells"] == [(1, 1)]
    assert _read(path)[0] == ["'=cmd", "Name"]
    assert C.validate_csv(path)["columns"] == 2


def test_reported_cells_stop_at_twenty_but_the_count_is_exact(tmp_path):
    rows = [["=x"] for _ in range(35)]
    report = C.write_csv(["A"], rows, tmp_path / "many.csv")
    assert report["neutralised"] == 35
    assert len(report["neutralised_cells"]) == 20 and report["neutralised_cells"][-1] == (21, 1)


def test_a_cell_over_excels_limit_is_cut_and_counted_and_a_longer_file_still_reads(tmp_path):
    """Wave 2c addendum: a 200,000-character cell made validate_csv refuse
    and read_csv_grid crash on a well-formed file (csv.Error from the
    131,072 field limit). The writer cuts at Excel's 32,767 and says so; the
    reader's limit is raised so a file we did not write still reads."""
    path = tmp_path / "long.csv"
    long = "c" * 200_000
    report = C.write_csv(["A", "B"], [[long, "short"], ["x", "y" * 40_000]], path)
    assert report["cut_cells"] == 2
    assert report["warnings"] == ["2 cells were cut to 32,767 characters, the most a spreadsheet cell holds"]
    records = _read(path)
    assert len(records[1][0]) == C.MAX_CELL_CHARS == 32_767 and len(records[2][1]) == 32_767
    facts = C.validate_csv(path, expected_rows=2)
    assert facts["rows"] == 2
    assert C.read_csv_grid(path)["rows"][0][0] == "c" * 32_767
    # A short cell is not touched and not counted.
    assert C.write_csv(["A"], [["fine"]], tmp_path / "ok.csv")["cut_cells"] == 0
    # A file with a 200,000-character field that this writer did NOT make
    # (a pasted export) is read, not crashed on.
    raw = tmp_path / "foreign.csv"
    raw.write_bytes(b"A,B\r\n" + long.encode() + b",1\r\n")
    assert C.validate_csv(raw)["rows"] == 1
    assert len(C.read_csv_grid(raw)["rows"][0][0]) == 200_000


def test_control_characters_are_stripped_but_tab_and_newlines_stay(tmp_path):
    path = tmp_path / "ctl.csv"
    C.write_csv(["A"], [["a\x00b\x01c\x0bd\x1fe\ufffef"], ["keep\ttab\nand\r\nnewline"]], path)
    records = _read(path)
    assert records[1] == ["abcdef"]
    assert records[2] == ["keep\ttab\nand\r\nnewline"]
    assert C.validate_csv(path)["rows"] == 2


# ------------------------------------------------------------ validation --


def test_exactly_n_rows_validates_with_500_and_fails_with_499(tmp_path):
    path = tmp_path / "gen.csv"
    report = C.write_csv(GEN_HEADER, _generated(500), path)
    assert report["rows"] == 500
    facts = C.validate_csv(path, expected_rows=500, expected_columns=6)
    assert facts == {"ok": True, "rows": 500, "columns": 6, "size": report["size"], "sha256": report["sha256"], "encoding": "utf-8"}
    # "were required", as the wave 2c brief words the exactly-N sentence
    # (CONTRACT-2 §11: a mismatch names both numbers); it was "expected".
    with pytest.raises(ValueError, match="500 data rows; 499 were required"):
        C.validate_csv(path, expected_rows=499)
    with pytest.raises(ValueError, match="header has 6 columns; 5 were required"):
        C.validate_csv(path, expected_columns=5)


def test_validation_failure_is_the_validate_stage_exception(tmp_path):
    """Drop-in for validate.py's _VALIDATORS: the exception is the one
    validate_all already catches, and it is a ValueError as the brief says."""
    path = tmp_path / "short.csv"
    C.write_csv(["A"], [["1"]], path)
    with pytest.raises(validate.ValidationFailed) as excinfo:
        C.validate_csv(path, expected_rows=2)
    assert isinstance(excinfo.value, ValueError)


def test_ragged_row_is_refused_by_the_writer_and_by_the_validator(tmp_path):
    path = tmp_path / "ragged.csv"
    with pytest.raises(ValueError, match="data row 2 has 3 cells; the header has 2"):
        C.write_csv(["A", "B"], [["1", "2"], ["1", "2", "3"]], path)
    assert not path.exists()  # no half-written file is left behind
    # A file that arrived ragged (not ours) is refused on reopen.
    path.write_bytes(b"A,B\r\n1,2\r\n1,2,3\r\n")
    with pytest.raises(ValueError, match="row 3 of the CSV has 3 cells; the header has 2"):
        C.validate_csv(path)
    path.write_bytes(b"A,B\r\n1,2\r\n1\r\n")
    with pytest.raises(ValueError, match="row 3 of the CSV has 1 cells"):
        C.validate_csv(path)


def test_zero_bytes_missing_file_and_oversize_are_refused(tmp_path, monkeypatch):
    path = tmp_path / "empty.csv"
    path.write_bytes(b"")
    with pytest.raises(ValueError, match="zero bytes"):
        C.validate_csv(path)
    with pytest.raises(ValueError, match="not written"):
        C.validate_csv(tmp_path / "missing.csv")
    C.write_csv(["A"], [["1"] for _ in range(50)], path)
    monkeypatch.setattr(T, "MAX_FILE_BYTES", 64)
    with pytest.raises(ValueError, match="larger than"):
        C.validate_csv(path)


def test_nul_non_utf8_and_bom_are_refused(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_bytes(b"A,B\r\n1,x\x00y\r\n")
    with pytest.raises(ValueError, match="row 2, column 2 of the CSV holds a NUL byte"):
        C.validate_csv(path)
    path.write_bytes(b"A,B\r\n1,caf\xe9\r\n")  # latin-1 é
    with pytest.raises(ValueError, match="not valid UTF-8"):
        C.validate_csv(path)
    path.write_bytes(b"\xef\xbb\xbfA,B\r\n1,2\r\n")
    with pytest.raises(ValueError, match="byte-order mark"):
        C.validate_csv(path)


def test_unneutralised_formula_lead_is_refused(tmp_path):
    path = tmp_path / "live.csv"
    path.write_bytes(b"A,B\r\n=HYPERLINK(\"http://evil/\"),-2\r\n")
    with pytest.raises(ValueError, match="row 2, column 1 of the CSV begins with '=' and was not neutralised"):
        C.validate_csv(path)
    path.write_bytes(b"A,B\r\n'=HYPERLINK(\"http://evil/\"),-2\r\n")
    assert C.validate_csv(path)["rows"] == 1  # the apostrophe makes it text; -2 is a number


def test_empty_header_cell_is_refused(tmp_path):
    path = tmp_path / "hdr.csv"
    with pytest.raises(ValueError, match="every column needs a name"):
        C.write_csv(["A", ""], [["1", "2"]], path)
    path.write_bytes(b"A,\r\n1,2\r\n")
    with pytest.raises(ValueError, match="empty header cell"):
        C.validate_csv(path)
    path.write_bytes(b"\r\n1,2\r\n")
    with pytest.raises(ValueError, match="empty header cell"):
        C.validate_csv(path)


def test_writer_refuses_the_row_and_column_ceilings(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "MAX_ROWS_PER_SHEET", 5)
    with pytest.raises(ValueError, match="more than 5 rows"):
        C.write_csv(["A"], [["1"] for _ in range(6)], tmp_path / "rows.csv")
    C.write_csv(["A"], [["1"] for _ in range(5)], tmp_path / "rows.csv")  # exactly the ceiling is fine
    monkeypatch.setattr(T, "MAX_COLUMNS_PER_SHEET", 3)
    with pytest.raises(ValueError, match="4 columns; the ceiling is 3"):
        C.write_csv(["A", "B", "C", "D"], [], tmp_path / "cols.csv")


# ------------------------------------------------------------- the grid --


def test_grid_reader_pages_a_1200_row_file(tmp_path):
    path = tmp_path / "big.csv"
    rows = _generated(1200, seed=7)
    C.write_csv(GEN_HEADER, rows, path)

    first = C.read_csv_grid(path)  # defaults: offset 0, limit 500, max_cols 60
    assert first["columns"] == GEN_HEADER
    assert first["total_rows"] == 1200 and first["total_columns"] == 6
    assert len(first["rows"]) == 500 and first["truncated"] is True
    assert first["rows"][0][0] == "CAND-0001" and first["rows"][499][0] == "CAND-0500"

    last = C.read_csv_grid(path, offset=1000, limit=500)
    assert len(last["rows"]) == 200 and last["truncated"] is False
    assert last["rows"][0][0] == "CAND-1001" and last["rows"][-1][0] == "CAND-1200"
    assert last["total_rows"] == 1200

    middle = C.read_csv_grid(path, offset=700, limit=1)
    assert middle["rows"] == [[C.cell_text(v) for v in rows[700]]] and middle["truncated"] is True

    beyond = C.read_csv_grid(path, offset=5000, limit=10)
    assert beyond["rows"] == [] and beyond["truncated"] is False and beyond["total_rows"] == 1200

    narrow = C.read_csv_grid(path, offset=1000, limit=500, max_cols=2)
    assert narrow["columns"] == ["Id", "Name"] and narrow["total_columns"] == 6
    assert all(len(r) == 2 for r in narrow["rows"]) and narrow["truncated"] is True

    # Bounds: a negative offset is 0, a zero limit is 1, a huge limit is the sheet ceiling.
    assert len(C.read_csv_grid(path, offset=-5, limit=0)["rows"]) == 1
    assert len(C.read_csv_grid(path, limit=10 ** 9)["rows"]) == 1200


def test_grid_reader_returns_formulas_as_text_and_keeps_newlines(tmp_path):
    path = tmp_path / "grid.csv"
    C.write_csv(["A", "B"], [["=SUM(A1)", "two\nlines"], ["-2", None]], path)
    grid = C.read_csv_grid(path)
    assert grid["rows"] == [["'=SUM(A1)", "two\nlines"], ["-2", ""]]
    assert grid["total_rows"] == 2 and grid["truncated"] is False


# ------------------------------------------ security review 2026-09-12 --


def test_a_byte_order_mark_in_a_cell_is_stripped_so_the_lead_is_neutralised_and_the_file_validates(tmp_path):
    """#5: a header copied from a UTF-8-BOM CSV kept U+FEFF, so the CSV
    started with the three BOM bytes (validate_csv refused the whole
    render) and a formula lead behind the mark was not neutralised. The
    invisible zero-width characters (U+FEFF, U+200B, U+2060) are stripped
    from every cell before the lead is looked at."""
    path = tmp_path / "bom.csv"
    report = C.write_csv(["﻿Id", "Name"], [["﻿=cmd|' /C calc'!A0", "​Alice⁠"], ["2", "Bob"]], path)
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
    records = _read(path)
    assert records[0] == ["Id", "Name"]
    assert records[1] == ["'=cmd|' /C calc'!A0", "Alice"], "the lead behind the mark is neutralised, the marks are gone"
    assert report["neutralised"] == 1 and report["neutralised_cells"] == [(2, 1)]
    assert C.validate_csv(path)["ok"]
    assert C.cell_text("﻿=1") == "=1" and C.is_formula_lead(C.cell_text("﻿=1"))


def test_a_neutralised_cell_is_cut_so_the_apostrophe_fits_in_excels_limit(tmp_path):
    """#6: the cut came before the apostrophe, so every cut formula-led
    cell was 32,768 characters — one over the limit the module promises."""
    path = tmp_path / "cut.csv"
    report = C.write_csv(["Cell"], [["=" + "A" * 200_000], ["B" * 200_000], ["=" + "C" * 32_766]], path)
    records = _read(path)
    assert len(records[1][0]) == C.MAX_CELL_CHARS and records[1][0].startswith("'=A")
    assert len(records[2][0]) == C.MAX_CELL_CHARS
    assert records[3][0] == "'=" + "C" * 32_765 and len(records[3][0]) == C.MAX_CELL_CHARS, "exactly at the limit before the apostrophe: cut by one"
    assert report["cut_cells"] == 3 and report["neutralised"] == 2
    assert all(len(cell) <= C.MAX_CELL_CHARS for record in records for cell in record)
    assert C.validate_csv(path, expected_rows=3)["ok"]
