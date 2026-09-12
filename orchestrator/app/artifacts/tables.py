"""Pasted and generated tables: the rows the model must never retype.

WHY THIS MODULE EXISTS. Before it, a 30-row audit table pasted into the
chat reached the composer only through `intent.instruction`, which collapses
every tab and newline into one space and cuts at 4,000 characters (discovery
of 2026-09-12, C5): row boundaries and blank cells were gone before the model
saw them, and the model retyped what it could remember. A "500 sample
customers" request had no generator at all (C6): the model typed the rows at
~53 tokens each, which the Fast budget cuts at about 225 rows.

Three things live here, all pure — no I/O, no model call, no app import
beyond `types` — so the composer, the engine and the renderers share them:

  * `parse_table` — a TSV / markdown / space-aligned / CSV block, found inside
    a longer message, becomes a `ParsedTable` in which every source line is
    one row and every blank cell stays blank (None). Nothing is dropped or
    merged: a short row is padded and the padding recorded, an over-long row
    keeps its extra cells in the last column. `forward_fill` is the one
    transformation, and it is gated by evidence (CONTRACT-2 §6).
  * `generate_rows` — exactly N rows from a column-rule mapping, seeded, with
    built-in name pools, so a dataset request costs the model a schema and
    not 25,000 tokens of rows (CONTRACT-2 §4).
  * the rewrite helpers — batches of (row, text) for the composer, and a
    validator that accepts a rewritten comment only when every timestamp and
    every quoted span of the original survived verbatim (CONTRACT-2 §4).

Row numbers in warnings and transformations are SOURCE line numbers —
1-based, in the text as pasted — so the header of a plain paste is line 1
and the numbers coincide with the spreadsheet rows a person sees.
"""
from __future__ import annotations

import csv
import io
import re
import string
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from random import Random
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from . import types as T

#: A paste beyond either cap comes back with `truncated=True` and the
#: counts; the caller refuses with a clear sentence instead of quietly
#: trimming what the person pasted. The byte cap is measured in UTF-8
#: bytes, as the name says.
MAX_PASTE_ROWS = 10_000
MAX_PASTE_BYTES = 5 * 1024 * 1024
#: Stripped from a paste before a line is split (security review of
#: 2026-09-12, #5 and #7): NUL, which no output format can carry (spec.py
#: scrubs it from every string) and which is the sentinel `_try_pipe` uses
#: for an escaped pipe — a NUL in a cell came out as a literal `|`; and
#: U+FEFF, the byte-order mark Excel's "CSV UTF-8" export writes, which
#: `str.strip` does not remove and which made the first header `\ufeffId`
#: (every lookup by column name broke, and a formula lead hidden behind it
#: reached the CSV writer unneutralised).
_PASTE_STRIP_RE = re.compile("[\x00\ufeff]")

#: The stdlib reader refuses a field over 131,072 characters with
#: csv.Error; a pasted comment column can be longer than that and the
#: process-wide limit is raised once so a CSV paste never fails on it
#: (render/csv.py raises it for the same reason).
if csv.field_size_limit() < (1 << 20):
    csv.field_size_limit(1 << 20)

Cell = Optional[str]
Row = List[Cell]

#: A line inside a block needs at least this share of the block's lines to
#: carry the delimiter (the contract's "≥ 60% of non-empty lines").
_MIN_DENSITY = 0.6


# ---------------------------------------------------------- parsed table --


@dataclass
class ParsedTable:
    """One pasted table: a header, its rows, and where each row came from.

    `rows` are lists of str | None — None IS a blank source cell and must
    travel as one to the renderers ("K blank source fields stay blank").
    `source_rows[i]` is the 1-based line of `rows[i]` in the pasted text."""

    columns: List[str]
    rows: List[Row]
    source_rows: List[int]
    #: tab | pipe | spaces | comma | semicolon
    delimiter: str = "tab"
    #: 1-based line of the header in the pasted text.
    header_row: int = 1
    #: Width mismatches and lines without a separator, one entry per row.
    parse_warnings: List[str] = field(default_factory=list)
    #: What code changed after parsing, one entry per cell ("row 3: host
    #: forward-filled from row 2"). Empty until `forward_fill` runs.
    transformations: List[str] = field(default_factory=list)
    forward_filled: int = 0
    #: True when the paste went past MAX_PASTE_ROWS or MAX_PASTE_BYTES;
    #: `rows` then holds the first MAX_PASTE_ROWS and `total_rows` the count.
    truncated: bool = False
    total_rows: int = 0
    #: The message text around the block, so the instruction survives.
    prose_before: str = ""
    prose_after: str = ""

    @property
    def blanks(self) -> int:
        return sum(1 for r in self.rows for c in r if c is None)

    @property
    def blanks_by_column(self) -> Dict[str, int]:
        counts = {name: 0 for name in self.columns}
        for r in self.rows:
            for name, c in zip(self.columns, r):
                if c is None:
                    counts[name] += 1
        return counts

    @property
    def report(self) -> Dict[str, Any]:
        """What the completion sentence and the render report need."""
        return {
            "rows": len(self.rows),
            "columns": len(self.columns),
            "blanks": self.blanks,
            "blanks_by_column": self.blanks_by_column,
            "forward_filled": self.forward_filled,
            "warnings": list(self.parse_warnings),
            "transformations": list(self.transformations),
            "delimiter": self.delimiter,
            "truncated": self.truncated,
            "total_rows": self.total_rows,
        }

    def to_material(self, id: str, title: str = "") -> Dict[str, Any]:
        """The material-table shape (compose.DataTable's fields plus
        `source_rows` and `warnings`); rows stay str | None."""
        return {
            "id": id,
            "title": title or "Pasted table",
            "columns": list(self.columns),
            "rows": [list(r) for r in self.rows],
            "source_rows": list(self.source_rows),
            "warnings": list(self.parse_warnings),
        }

    def to_material_table(self, id: str, title: str = "") -> Any:
        """A `compose.DataTable` (CONTRACT-2 §6). Imported lazily: the
        composer imports this module, not the other way round."""
        from .compose import DataTable  # noqa: PLC0415 — avoids an import cycle

        d = self.to_material(id, title)
        return DataTable(**{k: v for k, v in d.items() if k in DataTable.__dataclass_fields__})

    def copy(self) -> "ParsedTable":
        return ParsedTable(
            columns=list(self.columns),
            rows=[list(r) for r in self.rows],
            source_rows=list(self.source_rows),
            delimiter=self.delimiter,
            header_row=self.header_row,
            parse_warnings=list(self.parse_warnings),
            transformations=list(self.transformations),
            forward_filled=self.forward_filled,
            truncated=self.truncated,
            total_rows=self.total_rows,
            prose_before=self.prose_before,
            prose_after=self.prose_after,
        )


# --------------------------------------------------------------- parsing --

_SPACE_RUN_SPLIT = re.compile(r"( {2,})")
_SPACE_RUN_RE = re.compile(r"\S {2,}\S")
#: A markdown rule line: `|---|:---:|`, `---|---`, `| ---- |`.
_PIPE_RULE_RE = re.compile(r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)*\s*:?-{2,}:?\s*\|?\s*$")
#: The sentinel for an escaped `\|` in a markdown cell. NUL cannot collide
#: with pasted text because parse_table strips every NUL first (#7).
_ESCAPED_PIPE = "\x00"
#: How many lines a quoted field may span before the quote is taken for a
#: stray one (prose with an unbalanced `"` must not swallow the message).
_JOIN_MAX = 20


@dataclass
class _Record:
    """One unit the block finder works over: a line, or — for the quoted
    delimiters — a csv record that may span several lines."""

    start: int          # 1-based first line in the pasted text
    end: int            # 1-based last line
    raw: str            # the text of those lines, joined with \n
    cells: Optional[List[str]] = None   # already split (tab/csv), else None


def parse_table(text: Optional[str]) -> Optional[ParsedTable]:
    """The table inside `text`, or None when there is none.

    Delimiter, in this order of trust: tab, markdown `|`, runs of 2+ spaces,
    then a comma/semicolon CSV (`csv.Sniffer` picks between the two). The
    block is the run of lines carrying the delimiter with the most such
    lines; lines around it are prose (`prose_before` / `prose_after`).
    Inside the block every non-empty line is one row: a tab table's line
    written with runs of spaces is split on those (a run twice the width of
    the others is two separators, i.e. a blank cell between), and a line
    with no separator at all is one cell in the first column, with a
    warning. Rows are never dropped or merged."""
    if not text or not text.strip():
        return None
    text = _PASTE_STRIP_RE.sub("", text)
    if not text.strip():
        return None
    truncated = False
    original_nonempty = 0
    if len(text.encode("utf-8")) > MAX_PASTE_BYTES:
        # The cap is in BYTES (a 5 MB paste of Devanagari is 5 MB, not 5 M
        # characters), the cut lands on a line boundary so no half row is
        # parsed, and the ORIGINAL paste's lines are counted first so the
        # report can say how many rows there really were.
        original_nonempty = sum(1 for line in text.splitlines() if line.strip())
        cut = text.encode("utf-8")[:MAX_PASTE_BYTES].decode("utf-8", errors="ignore")
        text = cut[: cut.rfind("\n")] if "\n" in cut else cut
        truncated = True
    lines = text.splitlines()
    if sum(1 for line in lines if line.strip()) < 2:
        return None
    for attempt in (_try_tab, _try_pipe, _try_spaces, _try_csv):
        table = attempt(lines)
        if table is not None:
            if truncated:
                table.truncated = True
                before_header = sum(1 for line in lines[: table.header_row - 1] if line.strip())
                table.total_rows = max(table.total_rows, original_nonempty - before_header - 1)
            return table
    return None


def _whole_text_is_table(records: Sequence[_Record], has_delimiter: Callable[[str], bool]) -> bool:
    """Every non-empty line carries the delimiter: the message IS the table
    (a header and one row is then a table; inside prose it takes three
    delimited lines, so a sentence with one tab or one pipe in it never
    becomes a two-line table)."""
    return bool(records) and all(has_delimiter(r.raw) for r in records)


def _tab_records(lines: Sequence[str]) -> List[_Record]:
    """One record per non-empty line, split on tabs with QUOTE_NONE
    semantics: a tab is a cell boundary whatever quotes are open, and a
    line boundary is a row boundary whatever quotes are open. A cell
    wrapped in a matching pair of straight quotes is unwrapped (Excel
    quotes a cell that holds a tab or a newline when it copies to the
    clipboard); an unmatched quote — the inch mark in `27" monitor` — is
    text and stays. The previous reader let that inch mark open a csv
    quoted field that swallowed up to twenty following lines into one
    record: a 31-row paste came back as 11 rows with no warning."""
    out: List[_Record] = []
    for i, line in enumerate(lines, 1):
        if not line.strip():
            continue
        out.append(_Record(i, i, line, [_unquote(c) for c in line.split("\t")]))
    return out


def _opens_quote(cells: Optional[Sequence[str]]) -> bool:
    """Does the row's last filled cell begin a quote it never closes — the
    first line of an Excel-style multi-line cell? The lines after it are
    kept as their own rows, with a warning that says why they look odd."""
    if not cells:
        return False
    for cell in reversed(list(cells)):
        if cell.strip():
            return cell.strip().startswith('"') and cell.count('"') % 2 == 1
    return False


def _line_records(lines: Sequence[str]) -> List[_Record]:
    return [_Record(i, i, line) for i, line in enumerate(lines, 1) if line.strip()]


def _quoted_records(lines: Sequence[str], delimiter: str) -> List[_Record]:
    """Records for the comma/semicolon CSV path, whose cells may be quoted
    as RFC 4180 quotes them. A quoted field may span lines (joined while the
    quotes are unbalanced, up to _JOIN_MAX); a join that does not come back
    as ONE record was a stray quote, and the line stands alone. The tab
    path never comes here (`_tab_records`): a clipboard table is not a
    CSV, and a quote in it must never cross a line."""
    out: List[_Record] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        buf = [line]
        j = i
        while sum(t.count('"') for t in buf) % 2 == 1 and j + 1 < n and j - i < _JOIN_MAX:
            j += 1
            buf.append(lines[j])
        cells: Optional[List[str]] = None
        if j > i:
            records = _read_csv("\n".join(buf), delimiter)
            if len(records) == 1:
                cells = records[0]
            else:
                j = i
        if cells is None:
            records = _read_csv(line, delimiter)
            cells = records[0] if len(records) == 1 else [line]
        out.append(_Record(i + 1, j + 1, "\n".join(lines[i:j + 1]), cells))
        i = j + 1
    return out


def _read_csv(text: str, delimiter: str) -> List[List[str]]:
    try:
        return [r for r in csv.reader(io.StringIO(text), delimiter=delimiter, skipinitialspace=True) if r]
    except csv.Error:
        return []


def _unquote(cell: str) -> str:
    c = cell.strip()
    if len(c) >= 2 and c[0] == '"' and c[-1] == '"':
        return c[1:-1].replace('""', '"')
    return cell


def _pick_block(
    flags: Sequence[bool], *, min_lines: int, max_gap: int, weak: Optional[Sequence[bool]] = None,
) -> Optional[Tuple[int, int]]:
    """(start, end) into the record list: the run with the most flagged
    records, trimmed to flagged ones at both ends. A run is broken by more
    than `max_gap` consecutive records that are neither flagged nor `weak`
    (a weak record — a tab table's line written with runs of spaces — is
    row-like: it neither breaks a run nor may start or end one, so a prose
    line with a double space can never become the header). None below
    `min_lines` flagged records or below the density the contract asks for."""
    best: Optional[Tuple[int, int, int]] = None
    start = None
    last_flagged = None
    count = 0
    gap = 0
    for i, f in enumerate(flags):
        if f:
            if start is None or gap > max_gap:
                if start is not None and (best is None or count > best[2]):
                    best = (start, last_flagged, count)
                start, count = i, 0
            last_flagged = i
            count += 1
            gap = 0
        elif not (weak is not None and weak[i]):
            gap += 1
    if start is not None and (best is None or count > best[2]):
        best = (start, last_flagged, count)
    if best is None:
        return None
    s, e, n = best
    row_like = n + (sum(1 for i in range(s, e + 1) if weak[i] and not flags[i]) if weak is not None else 0)
    if n < min_lines or row_like / (e - s + 1) < _MIN_DENSITY:
        return None
    return s, e


def _align_header(widths: Sequence[int], block: Tuple[int, int]) -> Tuple[int, int]:
    """For the weak delimiters (spaces, commas) a prose line just before the
    table can carry the delimiter too ("Here is the data,  please:"). The
    header is the first block line as wide as most of the block; narrower
    leading lines (≤ 2 cells) are prose, and trailing ones likewise.
    `widths[k]` is the cell count of block line `block[0] + k`."""
    start, end = block
    mode = max(set(widths), key=lambda w: (widths.count(w), w))
    if mode < 2:
        return block
    while start < end and widths[start - block[0]] != mode and widths[start - block[0]] <= 2:
        start += 1
    while end > start and widths[end - block[0]] != mode and widths[end - block[0]] <= 2:
        end -= 1
    return start, end


def _looks_like_prose(columns: Sequence[str], rows: Sequence[Row]) -> bool:
    """For the weak delimiters: sentences split at their commas or double
    spaces are not a table. A header cell that is a sentence (ends in `?`
    or `!`, or in `.` with three or more words), or a block in which most
    cells are wordy, is prose."""
    for c in columns:
        c = c.rstrip()
        if c.endswith(("?", "!")) or (c.endswith(".") and len(c.split()) >= 3) or len(c.split()) >= 6:
            return True
    cells = [c for r in rows for c in r if c]
    wordy = sum(1 for c in cells if len(c.split()) >= 4)
    return bool(cells) and wordy / len(cells) > 0.6


def _common_indent(texts: Sequence[str]) -> int:
    return min((len(t) - len(t.lstrip(" ")) for t in texts if t.strip()), default=0)


def _cell(value: Optional[str]) -> Cell:
    if value is None:
        return None
    value = value.strip()
    return value if value else None


def _header(cells: Sequence[str], warnings: List[str], line_no: int) -> List[str]:
    """Header cells stripped, trailing blanks dropped, blanks and
    duplicates named so the model can refer to every column."""
    names = [c.strip() for c in cells]
    while names and not names[-1]:
        names.pop()
    out: List[str] = []
    seen: Dict[str, int] = {}
    for j, name in enumerate(names):
        if not name:
            name = f"Column {j + 1}"
            warnings.append(f"row {line_no}: header cell {j + 1} is blank; named {name!r}")
        base = name
        if base in seen:
            seen[base] += 1
            name = f"{base} ({seen[base]})"
            warnings.append(f"row {line_no}: header {base!r} repeats; renamed {name!r}")
        else:
            seen[base] = 1
        out.append(name)
    return out


def _fit(cells: Sequence[str], expected: int, line_no: int, warnings: List[str]) -> Row:
    """Exactly `expected` cells from a row's split: trailing separators are
    ignored, a short row is padded with None (recorded), an over-long row
    keeps its extra cells joined into the last column (recorded)."""
    row: Row = [_cell(c) for c in cells]
    while len(row) > expected and row[-1] is None:
        row.pop()
    n = len(row)
    if n > expected:
        extra = [c for c in row[expected - 1:] if c is not None]
        row = row[: expected - 1] + [" ".join(extra) if extra else None]
        warnings.append(f"row {line_no} has {n} cells for {expected} columns; the extra cells were joined into the last column")
    elif n < expected:
        warnings.append(f"row {line_no} has {n} cells for {expected} columns")
        row.extend([None] * (expected - n))
    return row


def _split_aligned(line: str, expected: Optional[int]) -> List[str]:
    """Cells of a line aligned with runs of spaces. Leading spaces are a
    blank first cell. When the count falls short of `expected`, a run that
    is a multiple of the narrowest run stands for that many separators —
    the 8-space gap in `2026-08-04        MTG-77821` is a blank Session ID,
    not a wide one."""
    parts = _SPACE_RUN_SPLIT.split(line.rstrip())
    cells = parts[0::2]
    seps = parts[1::2]
    if expected is None or len(cells) >= expected or not seps:
        return cells
    unit = min(len(s) for s in seps)
    widened = [cells[0]]
    for sep, cell in zip(seps, cells[1:]):
        widened.extend([""] * (len(sep) // unit - 1))
        widened.append(cell)
    return widened if len(widened) == expected else cells


def _has_space_runs(text: str) -> bool:
    return bool(_SPACE_RUN_RE.search(text)) or text.startswith("  ")


def _assemble(
    delimiter: str,
    lines: Sequence[str],
    records: Sequence[_Record],
    block: Tuple[int, int],
    split_header: Callable[[_Record], List[str]],
    split_row: Callable[[_Record, str, int], Optional[List[str]]],
    *,
    skip: Callable[[str], bool] = lambda _text: False,
    prose_guard: bool = False,
    continuation: Callable[[int], bool] = lambda _index: False,
) -> Optional[ParsedTable]:
    """A ParsedTable from the records of one block. `split_row` gets the
    record, its de-indented text and the expected width; None from it means
    the line has no separator and stands as one cell (recorded once).
    `continuation(index)` says whether record `index` looks like the rest
    of a quoted cell the row above began — the warning then says so, and
    the line is still its own row: rows are never merged."""
    start, end = block
    indent = _common_indent([r.raw for r in records[start:end + 1]])
    warnings: List[str] = []
    header = records[start]
    columns = _header(split_header(header), warnings, header.start)
    if len(columns) < 2:
        return None
    expected = len(columns)
    rows: List[Row] = []
    source_rows: List[int] = []
    total = 0
    for index in range(start + 1, end + 1):
        rec = records[index]
        text = rec.raw[indent:] if rec.raw[:indent].strip() == "" else rec.raw
        if skip(text):
            continue
        total += 1
        if total > MAX_PASTE_ROWS:
            continue
        cells = split_row(rec, text, expected)
        if cells is None:
            if continuation(index):
                warnings.append(f"row {rec.start} looks like a continuation of the comment above; kept as its own row")
            else:
                warnings.append(f"row {rec.start} has no separator; kept as one cell in the first column")
            rows.append([_cell(text)] + [None] * (expected - 1))
        else:
            rows.append(_fit(cells, expected, rec.start, warnings))
        source_rows.append(rec.start)
    if not rows or (prose_guard and _looks_like_prose(columns, rows)):
        return None
    return ParsedTable(
        columns=columns,
        rows=rows,
        source_rows=source_rows,
        delimiter=delimiter,
        header_row=header.start,
        parse_warnings=warnings,
        truncated=total > MAX_PASTE_ROWS,
        total_rows=total,
        prose_before="\n".join(lines[: header.start - 1]).strip(),
        prose_after="\n".join(lines[records[end].end:]).strip(),
    )


def _try_tab(lines: Sequence[str]) -> Optional[ParsedTable]:
    records = _tab_records(lines)
    min_lines = 2 if _whole_text_is_table(records, lambda t: "\t" in t) else 3
    block = _pick_block(
        ["\t" in r.raw for r in records], min_lines=min_lines, max_gap=2,
        weak=[_has_space_runs(r.raw) for r in records],
    )
    if block is None:
        return None

    def split_row(rec: _Record, text: str, expected: int) -> Optional[List[str]]:
        if "\t" in rec.raw:
            return list(rec.cells or [])
        if _has_space_runs(text):
            return _split_aligned(text, expected)
        return None

    def continuation(index: int) -> bool:
        return index > 0 and "\t" in records[index - 1].raw and _opens_quote(records[index - 1].cells)

    return _assemble("tab", lines, records, block, lambda r: list(r.cells or []), split_row, continuation=continuation)


def _try_pipe(lines: Sequence[str]) -> Optional[ParsedTable]:
    records = _line_records(lines)
    min_lines = 2 if _whole_text_is_table(records, lambda t: "|" in t) else 3
    block = _pick_block(["|" in r.raw for r in records], min_lines=min_lines, max_gap=1)
    if block is None:
        return None

    def cells_of(text: str) -> List[str]:
        text = text.replace("\\|", _ESCAPED_PIPE).strip()
        if text.startswith("|"):
            text = text[1:]
        if text.endswith("|"):
            text = text[:-1]
        return [c.replace(_ESCAPED_PIPE, "|") for c in text.split("|")]

    def split_row(rec: _Record, text: str, expected: int) -> Optional[List[str]]:
        return cells_of(text) if "|" in text else None

    return _assemble(
        "pipe", lines, records, block, lambda r: cells_of(r.raw), split_row,
        skip=lambda t: bool(_PIPE_RULE_RE.match(t)),
    )


def _try_spaces(lines: Sequence[str]) -> Optional[ParsedTable]:
    records = _line_records(lines)
    block = _pick_block([bool(_SPACE_RUN_RE.search(r.raw.strip())) for r in records], min_lines=3, max_gap=1)
    if block is None:
        return None
    widths = [len(_split_aligned(records[i].raw, None)) for i in range(block[0], block[1] + 1)]
    block = _align_header(widths, block)
    if block[1] - block[0] < 2:
        return None

    def split_row(rec: _Record, text: str, expected: int) -> Optional[List[str]]:
        return _split_aligned(text, expected) if _has_space_runs(text) else None

    return _assemble(
        "spaces", lines, records, block, lambda r: _split_aligned(r.raw, None), split_row, prose_guard=True,
    )


def _try_csv(lines: Sequence[str]) -> Optional[ParsedTable]:
    sample = "\n".join(line for line in lines[:60] if line.strip())[:4096]
    try:
        sniffed = csv.Sniffer().sniff(sample, delimiters=",;").delimiter
    except csv.Error:
        sniffed = ","
    for delimiter in (sniffed, ";" if sniffed == "," else ","):
        table = _try_csv_with(lines, delimiter)
        if table is not None:
            return table
    return None


def _try_csv_with(lines: Sequence[str], delimiter: str) -> Optional[ParsedTable]:
    # A comma table needs three records of two or more cells, so at least
    # three lines carry the delimiter. Counted before the reader is opened:
    # the quoted-record reader costs two csv.reader calls per line, and a
    # 5 MB paste of unbalanced quotes with no comma in it spent most of
    # its ten seconds here, twice — once per delimiter (security review
    # 2026-09-12, #10).
    if sum(1 for line in lines if delimiter in line) < 3:
        return None
    records = _quoted_records(lines, delimiter)
    block = _pick_block([len(r.cells or []) >= 2 for r in records], min_lines=3, max_gap=1)
    if block is None:
        return None
    widths = [len(records[i].cells or []) for i in range(block[0], block[1] + 1)]
    block = _align_header(widths, block)
    if block[1] - block[0] < 2:
        return None
    # Sentences carry commas too. A comma table narrower than three columns
    # is trusted only when it has at least four rows.
    mode = max(set(widths), key=lambda w: (widths.count(w), w))
    if mode < 3 and block[1] - block[0] < 4:
        return None

    def split_row(rec: _Record, text: str, expected: int) -> Optional[List[str]]:
        return list(rec.cells or []) if delimiter in rec.raw else None

    return _assemble(
        "comma" if delimiter == "," else "semicolon", lines, records, block,
        lambda r: list(r.cells or []), split_row, prose_guard=True,
    )


# ---------------------------------------------------------- forward fill --

#: A leading column whose header names a grouping key — the one place a
#: blank cell conventionally means "same as above".
_FILL_HEADER_RE = re.compile(
    r"\b(host|candidate|owner|group|name|team|manager|reviewer|interviewer|evaluator)\b", re.I,
)
_FILL_MIN_BLANK_SHARE = 0.3


def forward_fill(table: ParsedTable, column: Union[int, str] = 0, *, evidence: bool = True) -> ParsedTable:
    """A NEW table with the blank cells of `column` filled from the row
    above — only when the column is text, at least 30% of its cells are
    blank, every blank run follows a filled cell, and (with `evidence`) the
    header names a grouping key; `evidence=False` forces the fill on any
    header. Every filled cell is recorded in `transformations`. Any
    condition that fails returns an unchanged copy: a blank that might be
    real data is never invented."""
    out = table.copy()
    j = _column_index(table, column)
    if j is None or not table.rows:
        return out
    cells = [r[j] if j < len(r) else None for r in table.rows]
    filled = [c for c in cells if c is not None]
    if not filled or len(filled) == len(cells):
        return out
    if evidence and not _FILL_HEADER_RE.search(table.columns[j]):
        return out
    if not _is_text(filled):
        return out
    if (len(cells) - len(filled)) / len(cells) < _FILL_MIN_BLANK_SHARE:
        return out
    if cells[0] is None:
        return out
    label = table.columns[j].strip().lower()
    last_value: Cell = None
    last_row = 0
    for row, src, cell in zip(out.rows, out.source_rows, cells):
        if cell is None:
            row[j] = last_value
            out.transformations.append(f"row {src}: {label} forward-filled from row {last_row}")
            out.forward_filled += 1
        else:
            last_value, last_row = cell, src
    return out


def _column_index(table: ParsedTable, column: Union[int, str]) -> Optional[int]:
    if isinstance(column, int):
        return column if 0 <= column < len(table.columns) else None
    wanted = str(column).strip().lower()
    for j, name in enumerate(table.columns):
        if name.strip().lower() == wanted:
            return j
    return None


def _is_text(values: Sequence[str]) -> bool:
    """Fewer than half the filled cells read as a number or a date."""
    typed = sum(1 for v in values if parse_number(v) is not None or normalise_date(v) != v)
    return typed * 2 < len(values)


# --------------------------------------------------------------- helpers --

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_YMD_RE = re.compile(r"^(\d{4})[/.](\d{1,2})[/.](\d{1,2})$")
_DMY_NUM_RE = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$")
_DMY_TEXT_RE = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?[\s\-]+([A-Za-z]{3,9})\.?,?[\s\-]+(\d{4})$")
_MDY_TEXT_RE = re.compile(r"^([A-Za-z]{3,9})\.?[\s\-]+(\d{1,2})(?:st|nd|rd|th)?,?[\s\-]+(\d{4})$")


def normalise_date(value: Any, *, day_first: bool = True) -> Any:
    """An ISO `YYYY-MM-DD` string for a date written any common way, or the
    value untouched. Numeric `a/b/yyyy` is read day-first (the local
    convention) unless the day-first reading is impossible; a datetime
    string is left alone so its time is not lost."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        return value
    s = value.strip()
    m = _ISO_DATE_RE.match(s) or _YMD_RE.match(s)
    if m:
        return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)), value)
    m = _DMY_NUM_RE.match(s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a > 12:
            d, mo = a, b
        elif b > 12:
            d, mo = b, a
        else:
            d, mo = (a, b) if day_first else (b, a)
        return _iso(y, mo, d, value)
    m = _DMY_TEXT_RE.match(s)
    if m and m.group(2).lower() in _MONTHS:
        return _iso(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)), value)
    m = _MDY_TEXT_RE.match(s)
    if m and m.group(1).lower() in _MONTHS:
        return _iso(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)), value)
    return value


def _iso(y: int, mo: int, d: int, original: Any) -> Any:
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return original


_NA = frozenset({"", "-", "—", "–", "n/a", "na", "n.a.", "none", "null", "nil", "tbd", "?"})
_CURRENCY_RE = re.compile(r"^(?:rs\.?|inr|usd|eur|gbp|[₹$€£¥])\s*|\s*(?:rs\.?|inr|usd|eur|gbp|[₹$€£¥])$", re.I)
_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_INT_RE = re.compile(r"^[+-]?\d+$")
#: A space between digit groups of three is a thousands separator
#: ("1 234 567"); any other space keeps the cell a label ("3 4").
_THOUSANDS_SPACE_RE = re.compile(r"(?<=\d) (?=\d{3}(?:\D|$))")


def parse_number(value: Any) -> Optional[Union[int, float]]:
    """The number a cell holds — thousands commas, a `%`, a currency sign or
    accounting parentheses stripped — or None. A blank, `n/a` or a dash is
    None, never 0: a missing value must not become a value."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    s = str(value).strip()
    if s.lower() in _NA:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        s, negative = s[1:-1].strip(), True
    s = _CURRENCY_RE.sub("", s).strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    s = _THOUSANDS_SPACE_RE.sub("", s.replace(",", "").replace("_", ""))
    if not _NUMBER_RE.match(s):
        return None
    n: Union[int, float] = int(s) if _INT_RE.match(s) else float(s)
    return -n if negative else n


_TS_RE = re.compile(r"(?<![\d:])(\d{1,2}:\d{2}(?::\d{2})?)(?![\d:])")
_DQ_RE = re.compile(r'"([^"\n]{1,500})"|“([^”\n]{1,500})”')
_SQ_RE = re.compile(r"(?<!\w)'([^'\n]{1,500}?)'(?!\w)|‘([^’\n]{1,500})’")


def timestamps_in(text: Optional[str]) -> List[str]:
    """Every `hh:mm` / `hh:mm:ss` in order of appearance (duplicates kept)."""
    return _TS_RE.findall(text or "")


def quoted_spans(text: Optional[str]) -> List[str]:
    """The text inside every pair of quotes, in order. Straight and curly
    double quotes always; single quotes only when they open and close at
    word boundaries, so `q's` is an apostrophe and `'good'` a quote."""
    found: List[Tuple[int, str]] = []
    for rx in (_DQ_RE, _SQ_RE):
        for m in rx.finditer(text or ""):
            inner = (m.group(1) or m.group(2) or "").strip()
            if inner:
                found.append((m.start(), inner))
    return [s for _, s in sorted(found)]


# ------------------------------------------------------------- generator --

GEN_KINDS: Tuple[str, ...] = ("id", "name", "email", "choice", "int", "float", "date", "datetime", "text", "derived")
#: What one generator may build, checked BEFORE it is built (security
#: review of 2026-09-12, #11). The generator runs in the orchestrator
#: process — the render sandbox's memory limit does not cover it — and a
#: schema-valid recipe could ask for a gigabyte per cell: an id pattern
#: `{n:0999999999d}` formats every row to a billion characters (the
#: schema's own `format(n=1)` probe allocated it), and seven derived
#: `concat` columns of twenty copies each are 20^7 copies of the first
#: cell. So: rows × columns at most GEN_MAX_CELLS (half the product of
#: the sheet ceilings — a 10,000-row sheet may have 30 generated columns,
#: a 60-column sheet 5,000 rows); a generated cell at most
#: GEN_MAX_CELL_CHARS; a generated sheet at most GEN_MAX_CHARS in all
#: (10,000 rows × 60 columns of thirteen-character cells fit); an id
#: pattern's width or precision at most GEN_MAX_ID_WIDTH, and its `start`
#: within GEN_MAX_ID_START, so an id is never longer than the pattern plus
#: a few dozen digits.
GEN_MAX_CELLS = (T.MAX_ROWS_PER_SHEET * T.MAX_COLUMNS_PER_SHEET) // 2
GEN_MAX_CELL_CHARS = 2_000
GEN_MAX_CHARS = 8_000_000
GEN_MAX_ID_WIDTH = 40
GEN_MAX_ID_START = 10 ** 15
GEN_MAX_ID_PATTERN_CHARS = 200
DERIVED_OPS: Tuple[str, ...] = ("sum", "mean", "min", "max", "diff", "concat")
#: RFC 2606 reserved domains — a synthetic address must never be deliverable.
EMAIL_DOMAINS: Tuple[str, ...] = ("example.com", "example.org", "example.net")
#: When the model gives no window, a fixed one keeps the output stable
#: across days (today's date would change every run).
_DEFAULT_DATE_START = date(2025, 1, 1)
_DEFAULT_DATE_END = date(2025, 12, 31)
_UNIQUE_FLOAT_TRIES = 64

INDIAN_FIRST_NAMES: Tuple[str, ...] = (
    "Aarav", "Aditi", "Aditya", "Akash", "Amit", "Ananya", "Anjali", "Ankit", "Anushka", "Arjun",
    "Arnav", "Ayesha", "Bhavna", "Chirag", "Deepak", "Devika", "Dhruv", "Divya", "Farah", "Gauri",
    "Gaurav", "Harsh", "Isha", "Ishaan", "Jaya", "Kabir", "Karan", "Kavita", "Kiran", "Kritika",
    "Lakshmi", "Manish", "Meera", "Mohit", "Nandini", "Neha", "Nikhil", "Nisha", "Neeraj", "Om",
    "Pallavi", "Pooja", "Pranav", "Priya", "Rahul", "Rajesh", "Ravi", "Rekha", "Riya", "Rohan",
    "Ritu", "Sakshi", "Sanjay", "Shreya", "Simran", "Sneha", "Sunita", "Suresh", "Tanvi", "Tarun",
    "Uday", "Varun", "Vikram", "Vishal", "Yash", "Zoya",
)
INDIAN_LAST_NAMES: Tuple[str, ...] = (
    "Sharma", "Verma", "Gupta", "Mehta", "Nair", "Iyer", "Rao", "Reddy", "Joshi", "Desai",
    "Kumar", "Singh", "Malhotra", "Patel", "Menon", "Bhat", "Krishnan", "Jain", "Saxena", "Das",
    "Tiwari", "Kulkarni", "Chopra", "Agarwal", "Mishra", "Shinde", "Thakur", "Sood", "Nambiar", "Kaur",
    "Khan", "Bose", "Sen", "Prakash", "Pillai", "Chauhan", "Yadav", "Banerjee", "Mukherjee", "Chatterjee",
    "Deshpande", "Kapoor", "Bhatia", "Sethi", "Ahuja", "Naidu",
)
INTERNATIONAL_FIRST_NAMES: Tuple[str, ...] = (
    "James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael", "Linda", "William", "Elizabeth",
    "David", "Susan", "Richard", "Jessica", "Joseph", "Sarah", "Thomas", "Karen", "Daniel", "Lisa",
    "Matthew", "Nancy", "Anthony", "Emily", "Mark", "Anna", "Paul", "Laura", "Steven", "Sophia",
    "Andrew", "Olivia", "Kevin", "Emma", "Brian", "Grace", "George", "Hannah", "Edward", "Chloe",
    "Liam", "Noah", "Lucas", "Mia", "Ethan", "Isabella",
)
INTERNATIONAL_LAST_NAMES: Tuple[str, ...] = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
    "Hernandez", "Lopez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee",
    "Thompson", "White", "Harris", "Clark", "Lewis", "Robinson", "Walker", "Young", "Allen", "King",
    "Wright", "Scott", "Green", "Baker", "Adams", "Nelson", "Hill", "Campbell", "Mitchell", "Carter",
    "Roberts", "Turner", "Phillips", "Evans", "Collins", "Stewart",
)
_NAME_POOLS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "indian": (INDIAN_FIRST_NAMES, INDIAN_LAST_NAMES),
    "international": (INTERNATIONAL_FIRST_NAMES, INTERNATIONAL_LAST_NAMES),
}
_MIXED_INDIAN_SHARE = 0.7

_Values = Dict[str, List[Any]]


def generate_rows(generator: Dict[str, Any]) -> Tuple[List[List[Any]], Dict[str, Any]]:
    """Exactly `generator["rows"]` rows from its column rules (CONTRACT-2
    §4: `rows`, `seed`, `columns: [{name, kind, …}]`), and a report
    `{rows, seed, blanks_by_rule, unique_checked}`. Deterministic: one
    `random.Random(seed)`, columns generated in dependency order. Every
    rule violation is a ValueError with the column named — the caller turns
    it into a repair prompt, never into a silently wrong file."""
    if not isinstance(generator, dict):
        raise ValueError("generator must be a mapping")
    n = generator.get("rows")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1 or n > T.MAX_ROWS_PER_SHEET:
        raise ValueError(f"generator rows must be an integer between 1 and {T.MAX_ROWS_PER_SHEET:,}, got {n!r}")
    seed = generator.get("seed", 42)
    if seed is None:
        seed = 42
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(f"generator seed must be an integer, got {seed!r}")
    columns = generator.get("columns")
    if not isinstance(columns, list) or not columns:
        raise ValueError("generator needs at least one column")
    names: List[str] = []
    for col in columns:
        if not isinstance(col, dict) or not isinstance(col.get("name"), str) or not col["name"].strip():
            raise ValueError("every generator column needs a name")
        if col["name"] in names:
            raise ValueError(f"generator column {col['name']!r} repeats")
        if col.get("kind") not in GEN_KINDS:
            raise ValueError(f"column {col['name']!r}: kind must be one of {', '.join(GEN_KINDS)}, got {col.get('kind')!r}")
        names.append(col["name"])
    if n * len(columns) > GEN_MAX_CELLS:
        raise ValueError(f"{n:,} rows × {len(columns)} columns is {n * len(columns):,} cells; the most one sheet may generate is {GEN_MAX_CELLS:,}")

    rng = Random(seed)
    values: _Values = {}
    blanks_by_rule: Dict[str, int] = {}
    unique_checked: List[str] = []
    chars = 0
    for col in _dependency_order(columns):
        name, kind = col["name"], col["kind"]
        unique = bool(col.get("unique")) or kind in ("id", "email")
        cells = _COLUMN_GENERATORS[kind](col, n, rng, values, unique)
        chars += sum(len(c) if isinstance(c, str) else 8 for c in cells if c is not None)
        if chars > GEN_MAX_CHARS:
            raise ValueError(f"the generated sheet would be more than {GEN_MAX_CHARS:,} characters in all (at column {name!r}); fewer rows or shorter cells are needed")
        if kind == "derived":
            blanks = sum(1 for c in cells if c is None)
            if blanks:
                blanks_by_rule[name] = blanks
        rule = col.get("only_when")
        if rule is not None:
            blanked = _apply_only_when(name, rule, cells, values)
            blanks_by_rule[name] = blanks_by_rule.get(name, 0) + blanked
        if unique:
            _assert_unique(name, cells)
            unique_checked.append(name)
        values[name] = cells
    rows = [[values[c["name"]][i] for c in columns] for i in range(n)]
    assert len(rows) == n
    return rows, {"rows": n, "seed": seed, "blanks_by_rule": blanks_by_rule, "unique_checked": unique_checked}


def _dependency_order(columns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Columns sorted so every `only_when`, `derived` and `from` reference
    is generated first; an unknown or circular reference is a ValueError."""
    by_name = {c["name"]: c for c in columns}
    deps: Dict[str, List[str]] = {}
    for c in columns:
        refs: List[str] = []
        rule = c.get("only_when")
        if rule is not None:
            if not isinstance(rule, dict) or not isinstance(rule.get("column"), str) or not isinstance(rule.get("in"), list):
                raise ValueError(f"column {c['name']!r}: only_when must be {{\"column\": <name>, \"in\": [...]}}")
            refs.append(rule["column"])
        if c["kind"] == "derived":
            spec = c.get("derived")
            if not isinstance(spec, dict) or spec.get("op") not in DERIVED_OPS or not isinstance(spec.get("columns"), list) or not spec["columns"]:
                raise ValueError(f"column {c['name']!r}: derived must be {{\"op\": one of {', '.join(DERIVED_OPS)}, \"columns\": [...]}}")
            refs.extend(spec["columns"])
        source = c.get("from")
        if source is not None:
            if not isinstance(source, str):
                raise ValueError(f"column {c['name']!r}: 'from' must name a column")
            refs.append(source)
        for r in refs:
            if r not in by_name:
                raise ValueError(f"column {c['name']!r} refers to unknown column {r!r}")
            if r == c["name"]:
                raise ValueError(f"column {c['name']!r} refers to itself")
        deps[c["name"]] = refs
    ordered: List[Dict[str, Any]] = []
    done: set = set()
    pending = [c["name"] for c in columns]
    while pending:
        progressed = False
        for name in list(pending):
            if all(d in done for d in deps[name]):
                ordered.append(by_name[name])
                done.add(name)
                pending.remove(name)
                progressed = True
        if not progressed:
            raise ValueError(f"generator columns depend on each other in a cycle: {', '.join(pending)}")
    return ordered


def _apply_only_when(name: str, rule: Dict[str, Any], cells: List[Any], values: _Values) -> int:
    ref = values[rule["column"]]
    allowed = rule["in"]
    allowed_text = {str(a) for a in allowed}
    blanked = 0
    for i, v in enumerate(ref):
        if v is None or not (v in allowed or str(v) in allowed_text):
            if cells[i] is not None:
                blanked += 1
            cells[i] = None
    return blanked


def _assert_unique(name: str, cells: Sequence[Any]) -> None:
    seen: set = set()
    for c in cells:
        if c is None:
            continue
        if c in seen:
            raise ValueError(f"column {name!r} is not unique: {c!r} repeats")
        seen.add(c)


_FORMAT_SPEC_DIGITS_RE = re.compile(r"\d+")


def id_pattern_problem(pattern: Any) -> Optional[str]:
    """Why `pattern` is not an id pattern the generator will format, or
    None. Read with string.Formatter — NEVER formatted: `{n:0999999999d}`
    is a valid format that allocates a gigabyte per call, and the schema's
    own `format(n=1)` probe was the first allocation (security review
    2026-09-12, #11). The pattern may hold only the field `n`, with a
    conversion and a format spec whose width and precision are at most
    GEN_MAX_ID_WIDTH and which nests no field."""
    if not isinstance(pattern, str) or "{n" not in pattern:
        return 'id pattern must contain {n}, e.g. "CAND-{n:04d}"'
    if len(pattern) > GEN_MAX_ID_PATTERN_CHARS:
        return f"id pattern is {len(pattern)} characters long; the most is {GEN_MAX_ID_PATTERN_CHARS}"
    try:
        parts = list(string.Formatter().parse(pattern))
    except ValueError as exc:
        return f"bad id pattern ({exc})"
    for _literal, field_name, format_spec, _conversion in parts:
        if field_name is None:
            continue
        if field_name != "n":
            return f"id pattern must use only {{n}} (found {{{field_name}}})"
        spec = format_spec or ""
        if "{" in spec or "}" in spec:
            return "id pattern must not have a nested field in its format spec"
        for digits in _FORMAT_SPEC_DIGITS_RE.findall(spec):
            if int(digits) > GEN_MAX_ID_WIDTH:
                return f"id pattern width {digits} is more than {GEN_MAX_ID_WIDTH} characters"
    return None


def _gen_id(col: Dict[str, Any], n: int, rng: Random, values: _Values, unique: bool) -> List[Any]:
    pattern = col.get("pattern") or "{n}"
    problem = id_pattern_problem(pattern)
    if problem:
        raise ValueError(f"column {col['name']!r}: {problem}")
    start = col.get("start", 1)
    if isinstance(start, bool) or not isinstance(start, int):
        raise ValueError(f"column {col['name']!r}: id start must be an integer")
    if abs(start) > GEN_MAX_ID_START:
        raise ValueError(f"column {col['name']!r}: id start must be within ±{GEN_MAX_ID_START:,}")
    try:
        return [pattern.format(n=start + i) for i in range(n)]
    except (KeyError, ValueError, IndexError) as exc:
        raise ValueError(f"column {col['name']!r}: bad id pattern {pattern!r} ({exc})") from exc


def _name_pairs(region: str) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for key in (("indian", "international") if region == "mixed" else (region,)):
        first, last = _NAME_POOLS[key]
        pairs.extend((f, l) for f in first for l in last)  # noqa: E741
    return pairs


def _gen_name(col: Dict[str, Any], n: int, rng: Random, values: _Values, unique: bool) -> List[Any]:
    region = col.get("region") or "mixed"
    if region not in ("mixed", *_NAME_POOLS):
        raise ValueError(f"column {col['name']!r}: region must be indian, international or mixed")
    if unique:
        pairs = _name_pairs(region)
        if n > len(pairs):
            raise ValueError(f"cannot make {n} unique names from the pool ({len(pairs)} distinct {region} names)")
        rng.shuffle(pairs)
        return [f"{f} {l}" for f, l in pairs[:n]]  # noqa: E741
    out: List[Any] = []
    for _ in range(n):
        key = region if region != "mixed" else ("indian" if rng.random() < _MIXED_INDIAN_SHARE else "international")
        first, last = _NAME_POOLS[key]
        out.append(f"{rng.choice(first)} {rng.choice(last)}")
    return out


_LOCAL_PART_RE = re.compile(r"[^a-z0-9]+")


def _gen_email(col: Dict[str, Any], n: int, rng: Random, values: _Values, unique: bool) -> List[Any]:
    domains = col.get("domains") or list(EMAIL_DOMAINS)
    if not isinstance(domains, list) or not all(isinstance(d, str) and d for d in domains):
        raise ValueError(f"column {col['name']!r}: domains must be a list of strings")
    source = col.get("from")
    names = values[source] if source is not None else _gen_name({"name": col["name"]}, n, rng, values, False)
    seen: Dict[str, int] = {}
    out: List[Any] = []
    for i in range(n):
        raw = names[i] if names[i] is not None else f"user{i + 1}"
        local = _LOCAL_PART_RE.sub(".", str(raw).lower()).strip(".") or f"user{i + 1}"
        address = f"{local}@{rng.choice(domains)}"
        if address in seen:
            seen[address] += 1
            address = f"{local}{seen[address]}@{address.rsplit('@', 1)[1]}"
        else:
            seen[address] = 1
        out.append(address)
    return out


def _gen_choice(col: Dict[str, Any], n: int, rng: Random, values: _Values, unique: bool) -> List[Any]:
    options = col.get("values")
    if not isinstance(options, list) or not options:
        raise ValueError(f"column {col['name']!r}: choice needs a non-empty values list")
    weights = col.get("weights")
    if weights is not None:
        bad = not isinstance(weights, list) or len(weights) != len(options)
        if bad or any(isinstance(w, bool) or not isinstance(w, (int, float)) or w < 0 for w in weights):
            raise ValueError(f"column {col['name']!r}: weights must be non-negative numbers, one per value")
        if not any(weights):
            raise ValueError(f"column {col['name']!r}: weights are all zero")
    if unique:
        if n > len(options):
            raise ValueError(f"column {col['name']!r}: cannot pick {n} unique values from {len(options)}")
        return rng.sample(options, n)
    return rng.choices(options, weights=weights, k=n)


def _bounds(col: Dict[str, Any], default_lo: float, default_hi: float) -> Tuple[Any, Any]:
    lo = col.get("min", default_lo)
    hi = col.get("max", default_hi)
    for v in (lo, hi):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"column {col['name']!r}: min and max must be numbers")
    if lo > hi:
        raise ValueError(f"column {col['name']!r}: min {lo} is above max {hi}")
    return lo, hi


def _gen_int(col: Dict[str, Any], n: int, rng: Random, values: _Values, unique: bool) -> List[Any]:
    lo, hi = _bounds(col, 0, 100)
    lo, hi = int(lo), int(hi)
    if unique:
        if n > hi - lo + 1:
            raise ValueError(f"column {col['name']!r}: cannot pick {n} unique integers between {lo} and {hi}")
        return rng.sample(range(lo, hi + 1), n)
    return [rng.randint(lo, hi) for _ in range(n)]


def _gen_float(col: Dict[str, Any], n: int, rng: Random, values: _Values, unique: bool) -> List[Any]:
    lo, hi = _bounds(col, 0.0, 1.0)
    decimals = col.get("decimals", 2)
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 10:
        raise ValueError(f"column {col['name']!r}: decimals must be an integer from 0 to 10")
    out: List[Any] = []
    seen: set = set()
    for _ in range(n):
        for _try in range(_UNIQUE_FLOAT_TRIES if unique else 1):
            v = round(rng.uniform(lo, hi), decimals)
            if not unique or v not in seen:
                break
        else:
            raise ValueError(f"column {col['name']!r}: cannot make {n} unique values between {lo} and {hi} at {decimals} decimals")
        seen.add(v)
        out.append(v)
    return out


def _date_of(col: Dict[str, Any], key: str, default: date) -> date:
    raw = col.get(key)
    if raw is None:
        return default
    text = normalise_date(str(raw).strip()[:10]) if isinstance(raw, (str, date)) else raw
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"column {col['name']!r}: {key} must be an ISO date, got {raw!r}") from exc


def _gen_date(col: Dict[str, Any], n: int, rng: Random, values: _Values, unique: bool) -> List[Any]:
    start = _date_of(col, "start", _DEFAULT_DATE_START)
    end = _date_of(col, "end", _DEFAULT_DATE_END)
    if start > end:
        raise ValueError(f"column {col['name']!r}: start {start} is after end {end}")
    span = (end - start).days
    if unique:
        if n > span + 1:
            raise ValueError(f"column {col['name']!r}: cannot pick {n} unique dates in {span + 1} days")
        offsets = rng.sample(range(span + 1), n)
    else:
        offsets = [rng.randint(0, span) for _ in range(n)]
    return [(start + timedelta(days=o)).isoformat() for o in offsets]


def _datetime_of(col: Dict[str, Any], key: str, default: datetime) -> datetime:
    raw = col.get(key)
    if raw is None:
        return default
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None)
    text = str(raw).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d" and key == "end":
            parsed = parsed.replace(hour=23, minute=59, second=59)
        return parsed
    raise ValueError(f"column {col['name']!r}: {key} must be an ISO datetime, got {raw!r}")


def _gen_datetime(col: Dict[str, Any], n: int, rng: Random, values: _Values, unique: bool) -> List[Any]:
    start = _datetime_of(col, "start", datetime.combine(_DEFAULT_DATE_START, datetime.min.time()))
    end = _datetime_of(col, "end", datetime.combine(_DEFAULT_DATE_END, datetime.max.time()).replace(microsecond=0))
    if start > end:
        raise ValueError(f"column {col['name']!r}: start {start} is after end {end}")
    span = int((end - start).total_seconds())
    if unique:
        if n > span + 1:
            raise ValueError(f"column {col['name']!r}: cannot pick {n} unique moments in {span + 1} seconds")
        offsets = rng.sample(range(span + 1), n)
    else:
        offsets = [rng.randint(0, span) for _ in range(n)]
    return [(start + timedelta(seconds=o)).strftime("%Y-%m-%d %H:%M:%S") for o in offsets]


def _gen_text(col: Dict[str, Any], n: int, rng: Random, values: _Values, unique: bool) -> List[Any]:
    spec = col.get("text")
    pool = spec.get("pool") if isinstance(spec, dict) else (col.get("pool") or col.get("values"))
    if not isinstance(pool, list) or not pool or not all(isinstance(p, str) for p in pool):
        raise ValueError(f"column {col['name']!r}: text needs {{\"pool\": [\"…\", …]}}")
    if unique:
        if n > len(pool):
            raise ValueError(f"column {col['name']!r}: cannot pick {n} unique texts from a pool of {len(pool)}")
        return rng.sample(pool, n)
    return [rng.choice(pool) for _ in range(n)]


def _gen_derived(col: Dict[str, Any], n: int, rng: Random, values: _Values, unique: bool) -> List[Any]:
    spec = col["derived"]
    op, refs = spec["op"], spec["columns"]
    decimals = spec.get("decimals", col.get("decimals", 2))
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 10:
        raise ValueError(f"column {col['name']!r}: decimals must be an integer from 0 to 10")
    sep = spec.get("sep", " ")
    if not isinstance(sep, str):
        raise ValueError(f"column {col['name']!r}: sep must be a string")
    out: List[Any] = []
    for i in range(n):
        inputs = [values[r][i] for r in refs]
        if any(v is None or (isinstance(v, str) and not v.strip()) for v in inputs):
            out.append(None)
            continue
        if op == "concat":
            joined = sep.join(str(v) for v in inputs)
            if len(joined) > GEN_MAX_CELL_CHARS:
                # Checked per cell, before the column is built up: a
                # concat over a concat doubles every level (#11).
                raise ValueError(f"column {col['name']!r}: a concat cell would be {len(joined):,} characters; the most a generated cell holds is {GEN_MAX_CELL_CHARS:,}")
            out.append(joined)
            continue
        nums = [parse_number(v) for v in inputs]
        if any(x is None for x in nums):
            out.append(None)
            continue
        if op == "sum":
            result: Any = sum(nums)
        elif op == "mean":
            result = round(sum(nums) / len(nums), decimals)
        elif op == "min":
            result = min(nums)
        elif op == "max":
            result = max(nums)
        else:  # diff
            result = nums[0] - sum(nums[1:])
        if op != "mean" and all(isinstance(x, int) for x in nums):
            result = int(result)
        elif isinstance(result, float):
            result = round(result, decimals)
        out.append(result)
    return out


_COLUMN_GENERATORS: Dict[str, Callable[[Dict[str, Any], int, Random, _Values, bool], List[Any]]] = {
    "id": _gen_id,
    "name": _gen_name,
    "email": _gen_email,
    "choice": _gen_choice,
    "int": _gen_int,
    "float": _gen_float,
    "date": _gen_date,
    "datetime": _gen_datetime,
    "text": _gen_text,
    "derived": _gen_derived,
}


# --------------------------------------------------------------- rewrite --


def rewrite_batches(rows: Sequence[Sequence[Any]], column_index: int, batch_size: int = 40) -> List[List[Tuple[int, str]]]:
    """(row_index, original_text) pairs for every non-blank cell of the
    column, in batches the composer sends one at a time. Blank cells are
    not sent — blank stays blank. `row_index` is 0-based into `rows`."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    items = [
        (i, str(row[column_index]).strip())
        for i, row in enumerate(rows)
        if column_index < len(row) and row[column_index] is not None and str(row[column_index]).strip()
    ]
    return [items[k:k + batch_size] for k in range(0, len(items), batch_size)]


def apply_rewrites(
    rows: Sequence[Sequence[Any]],
    column_index: int,
    replies: Dict[Any, Any],
    *,
    max_ratio: float = 3.0,
    floor: int = 60,
) -> Tuple[List[List[Any]], List[int], List[str]]:
    """The rows with the column rewritten where the reply is acceptable:
    non-empty, no longer than `max_ratio` × the original (a `floor` of
    characters keeps a one-word comment rewritable), and every timestamp and
    quoted span of the original present verbatim. Otherwise the original is
    kept and a warning names the row (1-based). Returns (rows, kept,
    warnings) where `kept` lists the 0-based rows whose ORIGINAL stayed —
    rejected and missing replies alike. One row in, one row out, always."""
    out = [list(r) for r in rows]
    answers: Dict[int, Any] = {}
    for k, v in replies.items():
        try:
            answers[int(k)] = v
        except (TypeError, ValueError):
            continue
    kept: List[int] = []
    warnings: List[str] = []
    missing: List[int] = []
    for i, row in enumerate(out):
        original = row[column_index] if column_index < len(row) else None
        if original is None or not str(original).strip():
            continue
        if i not in answers:
            kept.append(i)
            missing.append(i)
            continue
        problem = _rewrite_problem(str(original), answers[i], max_ratio, floor)
        if problem:
            kept.append(i)
            warnings.append(f"row {i + 1}: kept the original ({problem})")
            continue
        row[column_index] = str(answers[i]).strip()
    if missing:
        shown = ", ".join(str(i + 1) for i in missing[:10]) + (", …" if len(missing) > 10 else "")
        warnings.append(f"{len(missing)} row(s) had no rewrite and keep the original: rows {shown}")
    assert len(out) == len(rows)
    return out, sorted(kept), warnings


#: The words that turn a finding around. A rewrite must carry as many of
#: them as the original: "did not pass" → "passed" and "no follow up" →
#: "follow-up was done" invert the audit (security review 2026-09-12, #2),
#: while "did not pass" → "didn't pass" keeps the count. Contractions are
#: matched on their `n't`; the bare words at word boundaries, so "knot",
#: "note" and "nothing"-vs-"no" are counted right.
_NEGATION_RE = re.compile(r"\b(?:not|no|never|none|nothing|nobody|neither|nor|without|cannot)\b|(?<=\w)n['’]t\b", re.IGNORECASE)


def negations_in(text: Optional[str]) -> int:
    """How many negation words `text` carries (see _NEGATION_RE)."""
    return len(_NEGATION_RE.findall(text or ""))


def _as_whole_words(span: str, text: str) -> bool:
    """Is `span` in `text` as whole words — not "no" inside "not"? (#2: the
    quoted "no" of `said "no" to a retry` was found inside "did not want a
    retry", and the guarantee that quoted text survives was hollow.)"""
    return re.search(rf"(?<!\w){re.escape(span)}(?!\w)", text) is not None


def _rewrite_problem(original: str, reply: Any, max_ratio: float, floor: int) -> Optional[str]:
    if not isinstance(reply, str) or not reply.strip():
        return "empty rewrite"
    text = reply.strip()
    limit = max(int(max_ratio * len(original)), floor)
    if len(text) > limit:
        return f"rewrite is {len(text)} characters for an original of {len(original)}"
    for ts in timestamps_in(original):
        if ts not in text:
            return f"timestamp {ts} missing"
    for span in quoted_spans(original):
        if not _as_whole_words(span, text):
            return f"quoted text {span!r} missing"
    before, after = negations_in(original), negations_in(text)
    if before != after:
        return f"a negation was changed: {before} in the original, {after} in the rewrite"
    return None


__all__ = [
    "MAX_PASTE_ROWS", "MAX_PASTE_BYTES", "ParsedTable", "parse_table", "forward_fill",
    "normalise_date", "parse_number", "timestamps_in", "quoted_spans", "negations_in",
    "GEN_KINDS", "DERIVED_OPS", "EMAIL_DOMAINS", "GEN_MAX_CELLS", "GEN_MAX_CELL_CHARS", "GEN_MAX_CHARS",
    "GEN_MAX_ID_WIDTH", "generate_rows", "id_pattern_problem",
    "rewrite_batches", "apply_rewrites",
]
