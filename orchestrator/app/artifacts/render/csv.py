"""Sheet rows → .csv through the stdlib csv module, reopened afterwards.

THE FILE. RFC 4180 as Excel, pandas, DuckDB and our own grid reader all
read it: CRLF between records; a field is quoted only when it holds a
comma, a double quote, CR or LF (QUOTE_MINIMAL), with an inner quote
doubled; an embedded newline survives inside its quotes. `None` and a
non-finite float are the empty field. An integral float is written as an
integer (`7080`, never `7080.0` — the value the spec meant); every other
number in positional notation (`-0.00001`, not `-1e-05`, so a numeric
column stays a numeric column to a reader that guesses types). NUL and the
control range XML cannot carry are stripped, as spec.py strips them from
every string before a renderer sees it.

NO BYTE-ORDER MARK — a deliberate trade-off. A UTF-8 BOM is what makes
Excel on Windows decode a CSV as UTF-8 instead of the ANSI code page; without
it a name like "Ramaswamy Iyer" survives but "₹" and "—" render as mojibake
there. WITH a BOM, csv.reader, pandas' default reader, DuckDB's read_csv and
`read_csv_grid` below all see the three bytes INSIDE the first header cell
("\\ufeffId" instead of "Id"), and every lookup by column name breaks. The
CSV is the data file (CONTRACT-2 §1: the XLSX/DOCX/PDF carry the styling),
and the data consumers win. `test_artifact_render_csv.py` pins the bytes.

FORMULA INJECTION. What this writer shares with the XLSX writer is the
LEADS tuple: a str cell that starts with one of `= + - @ TAB CR` is a
formula to Excel and LibreOffice (xlsx.py `is_formula_like` tests exactly
that and nothing more). This writer adds one exemption the XLSX writer does
not need — its numeric columns are numbers, not text — a plain number with
a sign (`^[-+]?\\d[\\d,]*(\\.\\d+)?%?$`: "-2", "+3.5", "-12%") is a value and is
never neutralised. openpyxl neutralises with the invisible quotePrefix
style; a CSV has no styles, so the apostrophe is written INTO the field —
`'=HYPERLINK(...)` — where every consumer can see it. That is the honest
trade-off against Excel and LibreOffice executing `=HYPERLINK("http://evil/")`
out of a CRM field the moment the file opens. The count and the first cells
are in the report so the completion sentence can say so.

CELL LENGTH. Excel refuses a cell over 32,767 characters and the stdlib
reader refuses a field over 131,072 (csv.Error) — a well-formed file with a
200,000-character comment made validate_csv refuse and read_csv_grid crash.
write_csv cuts a cell at 32,767 characters (Excel's limit) and counts the
cuts in the report; and the process-wide field limit is raised to 1 MiB
below, so a file this module did not write is read without a crash and
refused, if at all, by a rule that names the row.

`validate_csv` is the `validate` stage's reopen for this format (the same
shape as validate.py's validators: a function of the path returning the
facts, raising on failure). It streams the file once through csv.reader and
refuses what write_csv never produces: zero bytes, a BOM, bytes that are
not UTF-8, a NUL, a ragged row, an unneutralised formula lead — and, when
the caller says how many rows the spec had, a different count, which is the
"exactly 500 rows" guarantee CONTRACT-2 §11 asks the worker to enforce.
"""
from __future__ import annotations

import csv as _csv  # the stdlib module; this file shares its name, so bind it explicitly
import datetime as _dt
import math
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .. import types as T

#: The leads Excel and LibreOffice read as the start of a formula — the
#: XLSX writer's tuple, kept in step by test_artifact_render_csv.py.
FORMULA_LEADS: Tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")
#: A signed number, with thousands separators, a decimal part, a percent —
#: text that starts with + or - and reads as one of these is a value, not a
#: formula, and is never neutralised (CONTRACT-2 / wave 2b brief).
PLAIN_NUMBER_RE = re.compile(r"^[-+]?\d[\d,]*(\.\d+)?%?$")
#: spec.py's _XML_ILLEGAL: NUL, the C0 controls XML refuses, the two
#: non-characters. Tab, LF and CR stay (LF/CR are legal inside a quoted field).
_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_BOM = b"\xef\xbb\xbf"
#: Column types (spec.ColumnType) whose text values are written as bare
#: numbers when they parse as one: "1,000" → 1000. A CSV has no number
#: format, so a thousands separator would make the column text to every
#: reader; percent and date columns are written as the spec holds them.
_NUMERIC_TYPES = frozenset({"integer", "number", "currency"})
#: How many neutralised cells the write report lists (the count is exact).
_REPORTED_CELLS = 20
#: Excel's cell limit; a longer cell is cut here and the cut is counted.
MAX_CELL_CHARS = 32_767
#: The stdlib reader's field limit, raised once for the process so a cell
#: between Excel's limit and this one (a file we did not write) is read,
#: never a csv.Error out of the validator or the grid.
if _csv.field_size_limit() < (1 << 20):
    _csv.field_size_limit(1 << 20)


def is_formula_lead(text: str) -> bool:
    """The neutralisation predicate: a str that a spreadsheet would try to
    evaluate. Numbers with a sign are not formulas."""
    return isinstance(text, str) and text.startswith(FORMULA_LEADS) and PLAIN_NUMBER_RE.match(text) is None


def neutralise(text: str) -> str:
    """`text` as a CSV field that no spreadsheet runs: an apostrophe in front
    of a formula lead, the text unchanged otherwise."""
    return "'" + text if is_formula_lead(text) else text


def _number_text(value: float | int) -> str:
    """The shortest exact text of a number: integers (and integral floats)
    without a decimal part, everything else in positional notation. A
    non-finite float has no CSV representation and is the empty field."""
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        return ""
    if value == int(value):
        return str(int(value))
    text = repr(value)
    if "e" in text or "E" in text:
        # repr(-1e-05) is "-1e-05": exact, but neither a plain number to the
        # neutralisation regex nor a number to every reader. Decimal keeps
        # repr's digits and prints them positionally.
        text = format(Decimal(text), "f")
    return text


def _as_number(text: str) -> Optional[float]:
    """"1,000" → 1000.0, " 7080.50 " → 7080.5; None when the text is not
    a number. Thousands separators only — a percent sign is meaning, not
    formatting, and stays in the cell."""
    cleaned = text.strip().replace(",", "")
    if not cleaned or cleaned.endswith("%"):
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def cell_text(value: Any, column_type: str = "text") -> str:
    """One spec value as the field's text, before neutralisation: None and
    non-finite → "", numbers by `_number_text`, dates ISO, everything else
    `str()` with the illegal control range stripped."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return _number_text(value)
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    text = _ILLEGAL_RE.sub("", value if isinstance(value, str) else str(value))
    if column_type in _NUMERIC_TYPES:
        number = _as_number(text)
        if number is not None:
            return _number_text(number)
    return text


def _sha256_of(path: Path) -> str:
    # validate.sha256_of, imported lazily: validate.py will import this module
    # for its `csv` validator, and a top-level import in both directions is a
    # cycle whichever module Python loads first.
    from .validate import sha256_of

    return sha256_of(path)


def write_csv(columns: Sequence[str], rows: Iterable[Sequence[Any]], path: str | Path, *,
              column_types: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Write `columns` as the header and every row of `rows` under it; return
    {rows, columns, size, sha256, neutralised, neutralised_cells, cut_cells,
    warnings}.

    `rows` counts DATA rows (the header excluded); `columns` is the header
    width. `neutralised` is the number of cells given an apostrophe;
    `neutralised_cells` the first 20 as (row, column), both 1-based as a
    spreadsheet numbers them — the header is row 1, the first data row 2 —
    so a person can find the cell in whatever opens the file. `cut_cells`
    counts the cells cut to MAX_CELL_CHARS and `warnings` says so in one
    sentence ("N cells were cut to 32,767 characters").

    A ragged row is a ValueError, never padded or cut: the sheet was shaped
    by spec.Sheet before it got here, and a width that differs now is a bug
    upstream that a silently reshaped file would hide. The row and column
    ceilings are the spec's (types.MAX_ROWS_PER_SHEET, MAX_COLUMNS_PER_SHEET)
    and are refused for the same reason — the caller trims, and says so.
    """
    header = [str(c) for c in columns]
    if not header:
        raise ValueError("a CSV needs at least one column")
    if any(not h.strip() for h in header):
        raise ValueError("every column needs a name (spec.Column requires one; a blank header is a bug upstream)")
    if len(header) > T.MAX_COLUMNS_PER_SHEET:
        raise ValueError(f"the sheet has {len(header)} columns; the ceiling is {T.MAX_COLUMNS_PER_SHEET}")
    types: List[str] = list(column_types) if column_types is not None else ["text"] * len(header)
    if len(types) != len(header):
        raise ValueError(f"{len(types)} column types were given for {len(header)} columns")

    target = Path(path)
    width = len(header)
    n_rows = 0
    neutralised = 0
    cut = 0
    cells: List[Tuple[int, int]] = []

    def _record(values: Sequence[Any], line: int) -> List[str]:
        nonlocal neutralised, cut
        out: List[str] = []
        for j, value in enumerate(values):
            text = cell_text(value, types[j])
            if len(text) > MAX_CELL_CHARS:
                text = text[:MAX_CELL_CHARS]
                cut += 1
            field = neutralise(text)
            if field != text:
                neutralised += 1
                if len(cells) < _REPORTED_CELLS:
                    cells.append((line, j + 1))
            out.append(field)
        return out

    try:
        with open(target, "w", encoding="utf-8", newline="") as fh:
            writer = _csv.writer(fh, lineterminator="\r\n", quoting=_csv.QUOTE_MINIMAL, doublequote=True)
            writer.writerow(_record(header, 1))
            for index, row in enumerate(rows):
                if len(row) != width:
                    raise ValueError(f"data row {index + 1} has {len(row)} cells; the header has {width}")
                if n_rows >= T.MAX_ROWS_PER_SHEET:
                    raise ValueError(f"the sheet has more than {T.MAX_ROWS_PER_SHEET:,} rows, the ceiling")
                writer.writerow(_record(row, index + 2))
                n_rows += 1
    except BaseException:
        # Never leave half a file where a whole one was promised: a partial
        # CSV on disk is exactly the "download opens nothing" the validate
        # stage exists to prevent.
        target.unlink(missing_ok=True)
        raise

    warnings: List[str] = []
    if cut:
        warnings.append(f"{cut} cell{'s were' if cut != 1 else ' was'} cut to {MAX_CELL_CHARS:,} characters, the most a spreadsheet cell holds")
    return {
        "rows": n_rows,
        "columns": width,
        "size": target.stat().st_size,
        "sha256": _sha256_of(target),
        "neutralised": neutralised,
        "neutralised_cells": cells,
        "cut_cells": cut,
        "warnings": warnings,
    }


def read_csv_grid(path: str | Path, *, offset: int = 0, limit: int = 500, max_cols: int = 60) -> Dict[str, Any]:
    """The viewer's page of a CSV: {columns, rows, total_rows, total_columns,
    truncated}. One pass through csv.reader that keeps only the `limit` rows
    from `offset` (0-based data rows, the header excluded) and counts the
    rest, so a 10,000-row file costs one file read and 500 rows of memory.
    Every cell is text exactly as the file holds it — an apostrophe-led
    formula stays an apostrophe-led string; nothing is evaluated.
    `truncated` says rows exist after this page or columns beyond max_cols."""
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), T.MAX_ROWS_PER_SHEET))
    max_cols = max(1, min(int(max_cols), T.MAX_COLUMNS_PER_SHEET))
    columns: List[str] = []
    rows: List[List[str]] = []
    total_rows = 0
    total_columns = 0
    end = offset + limit
    with open(path, "r", encoding="utf-8", newline="") as fh:
        for index, record in enumerate(_csv.reader(fh)):
            if index == 0:
                total_columns = len(record)
                columns = record[:max_cols]
                continue
            data_index = index - 1
            total_rows += 1
            if offset <= data_index < end:
                rows.append(record[:max_cols])
    return {
        "columns": columns,
        "rows": rows,
        "total_rows": total_rows,
        "total_columns": total_columns,
        "truncated": bool(total_rows > offset + len(rows) or total_columns > max_cols),
    }


def _failure(sentence: str) -> ValueError:
    # validate.ValidationFailed IS a ValueError, so this validator's refusal
    # reaches render_version's `except V.ValidationFailed` (render/__init__.py)
    # like every other validator's; imported lazily for the same cycle
    # reason as _sha256_of.
    from .validate import ValidationFailed

    return ValidationFailed(sentence)


def validate_csv(path: str | Path, *, expected_rows: Optional[int] = None,
                 expected_columns: Optional[int] = None) -> Dict[str, Any]:
    """Reopen `path` as the CSV write_csv wrote; return {ok, rows, columns,
    size, sha256, encoding} or raise ValueError (validate.ValidationFailed)
    with one sentence that is safe to show. `expected_rows` is the spec's
    data-row count (the exactly-N guarantee); `expected_columns` its header
    width."""
    p = Path(path)
    if not p.is_file():
        raise _failure("the CSV file was not written")
    size = p.stat().st_size
    if size == 0:
        raise _failure("the CSV file is empty (zero bytes)")
    if size > T.MAX_FILE_BYTES:
        raise _failure(f"the CSV file is larger than {T.MAX_FILE_BYTES // (1024 * 1024)} MB")
    with open(p, "rb") as fh:
        if fh.read(len(_BOM)) == _BOM:
            raise _failure("the CSV starts with a byte-order mark, which this writer never emits")

    rows = 0
    columns: Optional[int] = None
    try:
        with open(p, "r", encoding="utf-8", errors="strict", newline="") as fh:
            for index, record in enumerate(_csv.reader(fh)):
                line = index + 1
                if index == 0:
                    columns = len(record)
                    if columns == 0 or any(not c.strip() for c in record):
                        raise _failure("the CSV has an empty header cell")
                    if expected_columns is not None and columns != expected_columns:
                        raise _failure(f"the CSV header has {columns} columns; {expected_columns} were required")
                else:
                    if len(record) != columns:
                        raise _failure(f"row {line} of the CSV has {len(record)} cells; the header has {columns}")
                    rows += 1
                for j, cell in enumerate(record):
                    if "\x00" in cell:
                        raise _failure(f"row {line}, column {j + 1} of the CSV holds a NUL byte")
                    if is_formula_lead(cell):
                        raise _failure(
                            f"row {line}, column {j + 1} of the CSV begins with {cell[0]!r} and was not neutralised"
                        )
    except UnicodeDecodeError as exc:
        raise _failure("the CSV is not valid UTF-8") from exc
    except _csv.Error as exc:
        raise _failure(f"the CSV could not be parsed: {exc}") from exc
    if columns is None:
        raise _failure("the CSV has no header row")
    if expected_rows is not None and rows != expected_rows:
        # The exactly-N sentence of the wave 2c brief: both numbers, and the
        # word "required" — the count is the request's, not a guess.
        raise _failure(f"the CSV has {rows:,} data rows; {expected_rows:,} were required")
    return {"ok": True, "rows": rows, "columns": columns, "size": size, "sha256": _sha256_of(p), "encoding": "utf-8"}


__all__ = ["FORMULA_LEADS", "PLAIN_NUMBER_RE", "MAX_CELL_CHARS", "is_formula_lead", "neutralise", "cell_text", "write_csv", "read_csv_grid", "validate_csv"]
