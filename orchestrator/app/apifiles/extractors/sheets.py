"""Spreadsheets (xlsx) and tabular files (csv / tsv / ndjson / json / parquet).

TWO STAGES, TWO OPS (design §4.3: `sheets`, then `profile`).

`sheets` writes the ROWS:
* xlsx — openpyxl `read_only=True, data_only=True` (streams rows, evaluates
  nothing) on `derived/src.xlsx`, one `sheet-<n>.csv` per worksheet plus text
  blocks of 200 rows with the header repeated. Measured for the design:
  300,000 rows × 5 columns iterated at 26,929 rows/s, +26 MiB RSS.
* csv / tsv — Python's csv module over the file as a stream; the block text is
  the ORIGINAL cells, tab-joined, so a citation `rows 201-400` quotes what the
  file says rather than a type-coerced copy.
* ndjson / jsonl / a JSON array of objects — DuckDB `read_json_auto` through
  the chat app's hardened connection (`core.profile._duck`: no extension
  downloads, HTTP/S3 filesystems disabled), fetched 200 rows at a time.
* parquet — no text blocks (design §4.4: profile only).

`profile` writes `profile.json` through `core.profile.profile_excel` /
`profile_tabular` — the exact profile the chat app shows its model, which by
construction never reports min/max VALUES of string columns (a raw cell can be
a secret). Both read the SUFFIXED hard link: DuckDB picks its reader by suffix
and openpyxl refuses a suffixless path (design finding #2, measured).

A ROW BLOCK'S `page` is its block number (1-based) and `rows` is the inclusive
data-row range, header excluded, so `rows [1, 200]` is the first 200 data rows.
"""
from __future__ import annotations

import csv
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import (
    PROFILE_NAME,
    ROWS_PER_BLOCK,
    AtomicTextWriter,
    FileCorrupt,
    FileTooComplex,
    PagesWriter,
    Spec,
    atomic_write_json,
    check_source_size,
    source_suffix,
)
from .office import check_container

#: One CSV field larger than this is a hostile file, not a spreadsheet. The
#: csv module's default is 131,072; 16 MiB still admits a long text cell.
CSV_FIELD_LIMIT = 16 * 1024 * 1024

#: A text block holds at most this many characters even when 200 rows are
#: wide: a 200-row block of 5,000-char cells would otherwise be a 1 MiB "page".
BLOCK_MAX_CHARS = 60_000


def _cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ")


class _Blocks:
    """Accumulates rows into 200-row text blocks with the header repeated."""

    def __init__(self, pages: PagesWriter, *, sheet: Optional[str], rows_per_block: int) -> None:
        self.pages = pages
        self.sheet = sheet
        self.rows_per_block = rows_per_block
        self.header: List[str] = []
        self._lines: List[str] = []
        self._chars = 0
        self._first = 1
        self.rows = 0

    def set_header(self, header: Sequence[Any]) -> None:
        self.header = [_cell(h) for h in header]

    def add(self, row: Sequence[Any]) -> None:
        line = "\t".join(_cell(v) for v in row)
        self.rows += 1
        self._lines.append(line)
        self._chars += len(line) + 1
        if len(self._lines) >= self.rows_per_block or self._chars >= BLOCK_MAX_CHARS:
            self.flush()

    def flush(self) -> None:
        if not self._lines:
            return
        last = self._first + len(self._lines) - 1
        head = "\t".join(self.header)
        text = (head + "\n" if head else "") + "\n".join(self._lines)
        self.pages.add(self.pages.pages + 1, text, sheet=self.sheet, rows=(self._first, last))
        self._first = last + 1
        self._lines = []
        self._chars = 0


def _xlsx_sheets(spec: Spec) -> Dict[str, Any]:
    check_container(spec, "spreadsheet")
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(spec.source, read_only=True, data_only=True)
    except Exception:  # noqa: BLE001 — openpyxl raises many types for a damaged package
        raise FileCorrupt() from None
    sheets = 0
    rows_total = 0
    columns = 0
    try:
        with PagesWriter(spec.derived_dir) as pages:
            for number, worksheet in enumerate(workbook.worksheets, start=1):
                sheets += 1
                blocks = _Blocks(pages, sheet=str(worksheet.title), rows_per_block=ROWS_PER_BLOCK)
                out = AtomicTextWriter(os.path.join(spec.derived_dir, f"sheet-{number}.csv"))
                try:
                    writer = csv.writer(out, lineterminator="\n")
                    header_seen = False
                    for row in worksheet.iter_rows(values_only=True):
                        values = ["" if v is None else v for v in row]
                        writer.writerow(values)
                        if not header_seen:
                            blocks.set_header(values)
                            columns = max(columns, len(values))
                            header_seen = True
                            continue
                        blocks.add(values)
                    blocks.flush()
                    out.commit()
                except BaseException:
                    out.abort()
                    raise
                rows_total += blocks.rows
    finally:
        workbook.close()
    return {
        "sheets": sheets,
        "rows": rows_total,
        "columns": columns,
        "chars": pages.chars,
        "estimated_tokens": pages.estimated_tokens,
        "unit": "rows",
    }


def _csv_rows(path: str, delimiter: str) -> Iterable[List[str]]:
    from .text import iter_lines

    csv.field_size_limit(CSV_FIELD_LIMIT)
    # The line end goes back on: without it a quoted cell that spans lines is
    # joined with nothing between its halves ("multi" + "line").
    return csv.reader((line + "\n" for line in iter_lines(path)), delimiter=delimiter)


def _delimiter_for(path: str) -> str:
    if source_suffix(path) == ".tsv":
        return "\t"
    from .text import iter_lines

    sample: List[str] = []
    for line in iter_lines(path):
        sample.append(line)
        if len(sample) >= 50:
            break
    try:
        return csv.Sniffer().sniff("\n".join(sample), delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _duck_rows(path: str):
    """(column names, cursor) for a json/ndjson source, through the chat app's
    network-closed DuckDB connection."""
    from ...core.profile import _duck, _reader_sql

    con = _duck()
    cursor = con.execute(f"SELECT * FROM {_reader_sql(path)}")
    names = [d[0] for d in (cursor.description or [])]
    return con, names, cursor


def _tabular_sheets(spec: Spec) -> Dict[str, Any]:
    check_source_size(spec, 1 << 40)
    suffix = source_suffix(spec.source)
    if suffix == ".parquet":
        # Profile only (design §4.4): a columnar file has no row text a reader
        # would cite, and the profile carries its shape.
        with PagesWriter(spec.derived_dir):
            pass
        return {"rows": None, "columns": None, "chars": 0, "estimated_tokens": 0, "unit": "rows"}
    columns = 0
    with PagesWriter(spec.derived_dir) as pages:
        blocks = _Blocks(pages, sheet=None, rows_per_block=ROWS_PER_BLOCK)
        if suffix in (".json", ".jsonl", ".ndjson"):
            try:
                con, names, cursor = _duck_rows(spec.source)
            except Exception:  # noqa: BLE001 — duckdb.Error subclasses; never echoed
                raise FileCorrupt() from None
            try:
                blocks.set_header(names)
                columns = len(names)
                while True:
                    batch = cursor.fetchmany(ROWS_PER_BLOCK)
                    if not batch:
                        break
                    for row in batch:
                        blocks.add(row)
            finally:
                con.close()
        else:
            try:
                header_seen = False
                for row in _csv_rows(spec.source, _delimiter_for(spec.source)):
                    if not header_seen:
                        blocks.set_header(row)
                        columns = len(row)
                        header_seen = True
                        continue
                    blocks.add(row)
            except csv.Error:
                raise FileCorrupt() from None
        blocks.flush()
    return {
        "rows": blocks.rows,
        "columns": columns,
        "chars": pages.chars,
        "estimated_tokens": pages.estimated_tokens,
        "unit": "rows",
    }


def sheets(spec: Spec) -> Dict[str, Any]:
    if spec.kind == "spreadsheet":
        return _xlsx_sheets(spec)
    return _tabular_sheets(spec)


def profile(spec: Spec) -> Dict[str, Any]:
    from ...core import archive
    from ...core.profile import profile_excel, profile_tabular

    try:
        if spec.kind == "spreadsheet":
            check_container(spec, "spreadsheet")
            result = profile_excel(spec.source, name="spreadsheet.xlsx")
            facts: Dict[str, Any] = {"sheets": len(result.get("sheets") or [])}
        else:
            result = profile_tabular(spec.source, name="table" + source_suffix(spec.source))
            facts = {"rows": result.get("rows"), "columns": result.get("columns_total")}
    except archive.ArchiveError:
        raise FileTooComplex("it expands beyond the decompression ceiling") from None
    except (FileCorrupt, FileTooComplex):
        raise
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 — duckdb / openpyxl parse errors
        raise FileCorrupt() from None
    # The profile's `file` is the name we gave it, never the client's filename
    # or a path on disk.
    atomic_write_json(os.path.join(spec.derived_dir, PROFILE_NAME), result)
    return facts
