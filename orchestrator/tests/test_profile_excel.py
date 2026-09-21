"""A spreadsheet is profiled like a CSV (B15, 2026-09-18).

Before this, profile_excel reported only each sheet's name, row count, column
NAMES and five sample rows: no types, nulls, ranges, top values, full rows or
computed totals. The same 50 rows were answerable uploaded as .csv and refused
uploaded as .xlsx. Each sheet is now streamed to a scratch CSV and handed to
profile_tabular, AFTER the zip-bomb caps, and the scratch is removed.
"""
from __future__ import annotations

import csv
import json
import os
import zipfile
from datetime import date, datetime

import pytest

from app.core import archive
from app.core import profile as profiler

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "dataset_truth")


def _first_rows(n: int):
    with open(os.path.join(FIXTURES, "sales_200.csv"), newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        return header, [row for _, row in zip(range(n), reader)]


def _typed(row):
    """The cells as a spreadsheet holds them: numbers, real dates, text."""
    order_id, order_date, region, channel, quantity, revenue = row
    return [int(order_id), date.fromisoformat(order_date), region, channel, int(quantity),
            float(revenue) if revenue else None]


def _write_xlsx(path, sheets):
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets:
        ws = wb.create_sheet(title)
        for row in rows:
            ws.append(row)
    wb.save(path)
    wb.close()


@pytest.fixture()
def same_rows(tmp_path):
    header, rows = _first_rows(50)
    csv_path = tmp_path / "sales.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    xlsx_path = tmp_path / "sales.xlsx"
    _write_xlsx(xlsx_path, [("Orders", [header] + [_typed(r) for r in rows])])
    return str(csv_path), str(xlsx_path)


def test_the_same_50_rows_profile_identically_as_csv_and_xlsx(same_rows):
    csv_path, xlsx_path = same_rows
    as_csv = profiler.profile_tabular(csv_path)
    book = profiler.profile_excel(xlsx_path)
    assert book["kind"] == "spreadsheet" and len(book["sheets"]) == 1
    sheet = book["sheets"][0]
    assert sheet["name"] == "Orders" and sheet["rows"] == 50

    assert sheet["columns"] == as_csv["columns"], "types, nulls, ranges, top values, sums"
    assert sheet["aggregates"] == as_csv["aggregates"]
    assert sheet["aggregates"]["measures"] == ["quantity", "revenue"]
    assert sheet["full_rows"] == as_csv["full_rows"] and sheet["full_content"] is True
    assert sheet["sample_rows"] == as_csv["sample_rows"]
    assert {c["name"]: c["dtype"] for c in sheet["columns"]}["order_date"] == "DATE"


def test_every_sheet_is_profiled_and_a_timestamp_column_stays_a_timestamp(tmp_path):
    path = tmp_path / "two.xlsx"
    stamps = [datetime(2024, 1, 1, 0, 0), datetime(2024, 1, 2, 9, 30), datetime(2024, 2, 3, 0, 0)]
    _write_xlsx(
        path,
        [
            ("Visits", [["at", "day", "minutes"]]
             + [[stamps[i % 3], date(2024, 1, 1 + i % 28), i % 7] for i in range(90)]),
            ("Costs", [["item", "cost"]] + [[f"item {i % 4}", i * 1.25] for i in range(12)]),
        ],
    )
    book = profiler.profile_excel(str(path))
    visits, costs = book["sheets"]
    types = {c["name"]: c["dtype"] for c in visits["columns"]}
    # Midnight and non-midnight datetimes in one column: written as they
    # print, the CSV reader would sniff that column as text.
    assert types == {"at": "TIMESTAMP", "day": "DATE", "minutes": "BIGINT"}
    assert [r["month"] for r in visits["aggregates"]["by_month"][0]["rows"]] == ["2024-01", "2024-02"]
    assert costs["rows"] == 12 and costs["aggregates"]["measures"] == ["cost"]
    assert costs["columns"][1]["sum"] == sum(i * 1.25 for i in range(12))


def test_the_scratch_dir_is_gone_afterwards(same_rows, monkeypatch):
    _csv, xlsx_path = same_rows
    made = []
    real_mkdtemp = profiler.tempfile.mkdtemp
    monkeypatch.setattr(profiler.tempfile, "mkdtemp", lambda **kw: made.append(real_mkdtemp(**kw)) or made[-1])
    book = profiler.profile_excel(xlsx_path)
    assert "aggregates" in book["sheets"][0]
    assert made, "each sheet went through a scratch CSV"
    assert not any(os.path.exists(d) for d in made)


def test_a_zip_bomb_xlsx_raises_before_openpyxl_and_leaves_no_scratch(tmp_path, monkeypatch):
    bomb = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", b"<Types/>")
        zf.writestr("xl/worksheets/sheet1.xml", b"\0" * (40 * 1024 * 1024))
    opened = []
    import openpyxl

    monkeypatch.setattr(openpyxl, "load_workbook", lambda *a, **k: opened.append(a) or pytest.fail("opened"))
    made = []
    real_mkdtemp = profiler.tempfile.mkdtemp
    monkeypatch.setattr(profiler.tempfile, "mkdtemp", lambda **kw: made.append(real_mkdtemp(**kw)) or made[-1])
    with pytest.raises(archive.ArchiveError):
        profiler.profile_excel(str(bomb))
    assert opened == [], "the caps run before any reader opens the package"
    assert not any(os.path.exists(d) for d in made)


def test_without_a_scratch_dir_a_sheet_keeps_its_old_minimal_profile(same_rows, monkeypatch):
    _csv, xlsx_path = same_rows
    monkeypatch.setattr(profiler, "_scratch_dir", lambda: None)
    sheet = profiler.profile_excel(xlsx_path)["sheets"][0]
    assert sheet["name"] == "Orders" and sheet["rows"] == 50
    assert [c["name"] for c in sheet["columns"]][:2] == ["order_id", "order_date"]
    assert len(sheet["sample_rows"]) == 5
    json.dumps(sheet, default=str)


# ---------------------------------------------------------------------------
# QA round 1 fixes (2026-09-18): one budget per workbook, and a width cap
# ---------------------------------------------------------------------------


def _rendered(agg) -> int:
    """The block's length as dataset.format_profile prints a sheet's."""
    wrapped = [{"sheets": [{"aggregates": agg}]}]
    shell = [{"sheets": [{"aggregates": 0}]}]
    return len(json.dumps(wrapped, ensure_ascii=False, indent=1, default=str)) - len(
        json.dumps(shell, indent=1)
    ) + 1


def test_a_wide_sheet_is_described_like_a_wide_csv(tmp_path):
    """Only the first PROFILE_MAX_COLUMNS columns are written to the scratch
    CSV (DuckDB never parses 16,384 columns), and the sheet says it was cut,
    exactly as a CSV of the same 70 columns does."""
    header = [f"c{i:02d}" for i in range(70)]
    rows = [[f"{'NS'[r % 2]}{i}" if i % 3 == 0 else r * 10 + i + 0.25 for i in range(70)] for r in range(40)]
    csv_path = tmp_path / "wide.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows([[repr(v) if isinstance(v, float) else v for v in row] for row in rows])
    xlsx_path = tmp_path / "wide.xlsx"
    _write_xlsx(xlsx_path, [("Wide", [header] + rows)])
    as_csv = profiler.profile_tabular(str(csv_path))
    sheet = profiler.profile_excel(str(xlsx_path))["sheets"][0]
    assert as_csv["columns_total"] == sheet["columns_total"] == 70
    assert as_csv["columns_truncated"] is sheet["columns_truncated"] is True
    assert sheet["columns"] == as_csv["columns"] and len(sheet["columns"]) == 60
    assert sheet["aggregates"] == as_csv["aggregates"]
    assert "full_rows" not in sheet and "full_rows" not in as_csv


def _gap_xlsx(path, width_ref: str, gap_row: int) -> None:
    """QA's HIGH repro: one header cell, a <dimension> of A1:<width_ref><gap_row>,
    and one data row at gap_row. openpyxl yields gap_row - 2 empty rows of the
    claimed width."""
    import re

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws["A1"] = "h"
    ws["A2"] = 1
    tmp = str(path) + ".tmp.xlsx"
    wb.save(tmp)
    with zipfile.ZipFile(tmp) as zin, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                x = data.decode()
                x = re.sub(r'<dimension ref="[^"]*"/>', f'<dimension ref="A1:{width_ref}{gap_row}"/>', x)
                x = x.replace('<row r="2"', f'<row r="{gap_row}"').replace('r="A2"', f'r="A{gap_row}"')
                data = x.encode()
            zout.writestr(info, data)
    os.remove(tmp)


def _spy_scratch(monkeypatch):
    written = []
    real = profiler.profile_tabular

    def spy(p, *a, **k):
        written.append(os.path.getsize(p))
        return real(p, *a, **k)

    monkeypatch.setattr(profiler, "profile_tabular", spy)
    return written


def test_a_gap_to_the_last_excel_column_is_cheap(tmp_path, monkeypatch):
    """Measured by QA at 9045dbf on the worker: this 4,843-byte file ran past
    1,200 s at 18 GB RSS and wrote an 819 MB scratch CSV (0.0 s on 4810da0)."""
    import time

    book = tmp_path / "xfd.xlsx"
    _gap_xlsx(book, "XFD", 50_000)
    archive.check_zip_container(str(book), label="spreadsheet")  # every cap passes
    written = _spy_scratch(monkeypatch)
    t = time.perf_counter()
    sheet = profiler.profile_excel(str(book))["sheets"][0]
    assert time.perf_counter() - t < 5.0
    assert max(written) <= 4 * 1024 * 1024
    assert sheet["columns_total"] == 16_384 and len(sheet["columns"]) == 60
    assert sheet["rows"] == 49_999


def test_a_gap_past_the_byte_budget_falls_back_with_a_note(tmp_path, monkeypatch):
    """openpyxl does not stop at Excel's 1,048,576 rows: a gap to row
    3,000,000 is read. The byte budget stops the writer; the row count and
    sample are kept, as on 4810da0."""
    book = tmp_path / "far.xlsx"
    _gap_xlsx(book, "BH", 3_000_000)
    written = _spy_scratch(monkeypatch)
    sheet = profiler.profile_excel(str(book))["sheets"][0]
    assert written == []
    assert sheet["rows"] == 2_999_999 and len(sheet["sample_rows"]) == 5
    assert sheet["note"].startswith("not profiled: the workbook expands to more text than")


def test_the_cell_budget_is_one_per_workbook(tmp_path, monkeypatch):
    rows = [["region", "amount"]] + [["NS"[i % 2], i + 0.5] for i in range(100)]
    path = tmp_path / "two.xlsx"
    _write_xlsx(path, [("A", rows), ("B", rows)])
    # 101 rows x 2 cells fit once, not twice.
    monkeypatch.setattr(profiler, "SHEET_MAX_CELLS", 300)
    first, second = profiler.profile_excel(str(path))["sheets"]
    assert "aggregates" in first and "note" not in first
    assert "aggregates" not in second
    assert second["note"] == "not profiled: the workbook holds more than 300 cells"
    assert second["rows"] == 100 and len(second["sample_rows"]) == 5


def test_a_workbook_shares_one_aggregates_budget(tmp_path, monkeypatch):
    """QA: the cap applied per sheet, so a 3-sheet x 1,000-row workbook's chat
    prompt block grew from 7,037 characters to 163,562."""
    import random

    rng = random.Random(4)

    def sheet_rows(k):
        out = [["region", "product", "day", "quantity", "revenue"]]
        for i in range(600):
            out.append([f"R{i % 6}", f"p{i % (10 + 10 * k):02d}", date(2020 + i % 5, 1 + i % 12, 1),
                        rng.randint(1, 9), rng.randrange(100, 10**6) / 100])
        return out

    tiny = [["region", "amount"]] + [["NS"[i % 2], i + 0.5] for i in range(20)]
    path = tmp_path / "three.xlsx"
    _write_xlsx(path, [("Tiny", tiny)] + [(f"S{k}", sheet_rows(k)) for k in range(3)])
    alone = [profiler.profile_excel(str(path))["sheets"][i]["aggregates"] for i in range(4)]
    budget = sum(map(_rendered, alone)) // 2
    monkeypatch.setattr(profiler, "AGG_MAX_CHARS", budget)
    shared = [s["aggregates"] for s in profiler.profile_excel(str(path))["sheets"]]
    assert sum(map(_rendered, shared)) <= budget
    assert shared[0] == alone[0], "a sheet needing less than an equal share keeps all of it"
    assert all(s["by_group"] for s in shared[1:]), "every sheet keeps some breakdowns"
    assert any("dropped to keep the aggregates" in w for s in shared[1:] for w in s["omitted"])


def test_a_workbook_shares_one_full_rows_budget(tmp_path, monkeypatch):
    header, rows = _first_rows(30)
    typed = [header] + [_typed(r) for r in rows]
    path = tmp_path / "twice.xlsx"
    _write_xlsx(path, [("One", typed), ("Two", typed)])
    one = profiler.profile_excel(str(path))["sheets"][0]
    need = len(json.dumps(one["full_rows"], default=str))
    monkeypatch.setattr(profiler.settings, "profile_full_chars", need + need // 2)
    first, second = profiler.profile_excel(str(path))["sheets"]
    assert first["full_content"] is True and first["full_rows"] == one["full_rows"]
    assert "full_rows" not in second, "the workbook is one file: one full-rows budget"
    assert second["aggregates"] == first["aggregates"], "its totals are still computed"
