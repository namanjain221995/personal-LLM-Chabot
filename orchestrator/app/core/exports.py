"""Tabular exports (spec §8): xlsx via openpyxl (bold header + auto column
widths) and csv. Filenames are `<slug>-<timestamp>.<ext>`. Exports are capped
at 100k rows; SQL result previews at 500 rows.

Pure module: openpyxl is imported lazily inside export_xlsx only.
"""
from __future__ import annotations

import csv
import re
import time
from pathlib import Path
from typing import List, Sequence, Tuple

PREVIEW_ROW_CAP = 500
EXPORT_ROW_CAP = 100_000

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 40, fallback: str = "export") -> str:
    slug = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    slug = slug[:max_len].strip("-")
    return slug or fallback


def timestamped_filename(slug: str, ext: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{slugify(slug)}-{stamp}.{ext.lstrip('.')}"


def cap_rows(rows: Sequence, cap: int) -> Tuple[list, bool]:
    """Return (rows capped to `cap`, truncated flag)."""
    if cap < 0:
        raise ValueError("cap must be >= 0")
    if len(rows) > cap:
        return list(rows[:cap]), True
    return list(rows), False


def apply_export_cap(rows: Sequence, cap: int = EXPORT_ROW_CAP) -> Tuple[list, bool]:
    """Export-specific cap (100k rows by default)."""
    return cap_rows(rows, cap)


def _cell_value(value: object) -> object:
    """Coerce values openpyxl cannot store natively (dict/list/...) to str."""
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


def export_xlsx(
    columns: Sequence[str],
    rows: Sequence[Sequence],
    directory: str | Path,
    slug: str,
    cap: int = EXPORT_ROW_CAP,
) -> Tuple[Path, bool]:
    """Write an .xlsx file with a bold header row and auto-sized columns.

    Returns (path, truncated). Rows beyond `cap` (default 100k) are dropped.
    """
    from openpyxl import Workbook  # lazy: keep core imports light
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    rows, truncated = apply_export_cap(rows, cap)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / timestamped_filename(slug, "xlsx")

    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append([str(c) for c in columns])
    bold = Font(bold=True)
    for cell in ws[1]:
        cell.font = bold
    for row in rows:
        ws.append([_cell_value(v) for v in row])

    # Auto column widths: longest of header/values (sampled), padded, capped.
    sample = rows[:1000]
    for idx, col in enumerate(columns):
        longest = len(str(col))
        for row in sample:
            if idx < len(row) and row[idx] is not None:
                longest = max(longest, len(str(row[idx])))
        ws.column_dimensions[get_column_letter(idx + 1)].width = min(longest + 2, 60)

    wb.save(path)
    return path, truncated


def export_csv(
    columns: Sequence[str],
    rows: Sequence[Sequence],
    directory: str | Path,
    slug: str,
    cap: int = EXPORT_ROW_CAP,
) -> Tuple[Path, bool]:
    """Write a .csv file. Returns (path, truncated); same 100k row cap."""
    rows, truncated = apply_export_cap(rows, cap)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / timestamped_filename(slug, "csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([str(c) for c in columns])
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])
    return path, truncated


__all__: List[str] = [
    "PREVIEW_ROW_CAP",
    "EXPORT_ROW_CAP",
    "slugify",
    "timestamped_filename",
    "cap_rows",
    "apply_export_cap",
    "export_xlsx",
    "export_csv",
]
