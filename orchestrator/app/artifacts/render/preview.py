"""Page images and workbook grids for the in-app viewer — the `preview` stage.

Pages come from `preview.pdf` through pypdfium2, the same rasteriser
core/pdf.py uses for uploaded PDFs (RENDER_SCALE there is a fixed 2.0; here
the scale is derived from the WIDTH the API asked for — 240 px for a card
thumbnail, 1400 px for a page — so a thumbnail is not a downscaled 1400 px
render). PNG bytes are returned; the API caches them under `previews/`.

The workbook preview is a GRID, not an image: `sheet_grid()` opens the .xlsx
read-only with `data_only=False`, so a formula cell is returned as its TEXT
(`=SUM(B2:B41)`) and is never evaluated by this code — the browser shows the
formula, exactly as the API contract says. Hidden sheets are excluded; bounds
are enforced so a 10,000-row sheet does not become a 10,000-row JSON.

`grid_for()` is the one shape the `GET …/grid` route serves for an xlsx AND
a csv (CONTRACT-2 §11): `sheets`, `sheet`, `columns`, `rows`, `total_rows`,
`total_columns`, `truncated`, `formulas_as_text: true` — an xlsx through
`sheet_grid` (trimmed to the sheet's real last column, paged by offset), a
csv through render/csv.read_csv_grid (one pass, one page in memory).
"""
from __future__ import annotations

import datetime as _dt
import io
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import types as T


def _lock():
    """The ONE lock every pypdfium2 call in the process takes — owned by
    core/pdf.py, whose uploaded-PDF readers run in the same worker threads.
    PDFium is not thread-safe; two threads inside it crash the process
    (reproduced 2026-09-11: SIGSEGV at 80 concurrent page renders, SIGABRT
    at 16). The worker subprocess has its own process and its own lock."""
    from ...core.pdf import PDFIUM_LOCK

    return PDFIUM_LOCK


def page_count(pdf_path: str | Path) -> int:
    import pypdfium2 as pdfium  # lazy: arm64 wheel, no system deps

    with _lock():
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            return len(pdf)
        finally:
            pdf.close()


def rasterise_page(pdf_path: str | Path, page: int, width: int) -> bytes:
    """PNG bytes of 1-based `page`, `width` pixels wide (height follows the
    page's aspect). Raises IndexError past the last page, ValueError for a
    width outside PREVIEW_WIDTHS' range."""
    import pypdfium2 as pdfium  # lazy

    if width < 32 or width > max(T.PREVIEW_WIDTHS) * 2:
        raise ValueError(f"preview width {width} is out of range")
    with _lock():
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            n = len(pdf)
            if page < 1 or page > n:
                raise IndexError(f"page {page} of {n}")
            pg = pdf[page - 1]
            try:
                page_w, _ = pg.get_size()  # points
                scale = width / page_w if page_w > 0 else 1.0
                bitmap = pg.render(scale=scale)
                pil = bitmap.to_pil().convert("RGB")
            finally:
                pg.close()
        finally:
            pdf.close()
    if pil.width != width:  # rounding: make the promised width exact
        from PIL import Image

        pil = pil.resize((width, max(1, round(pil.height * width / pil.width))), Image.LANCZOS)
    buf = io.BytesIO()
    pil.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return str(value)
        return value
    if isinstance(value, _dt.datetime):
        # openpyxl reads a date cell back as a datetime at midnight; the
        # grid shows the date a person typed, not "2026-08-03T00:00:00".
        if value.hour == value.minute == value.second == value.microsecond == 0 and value.tzinfo is None:
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    return str(value)


def sheet_grid(xlsx_path: str | Path, sheet: Optional[str], max_rows: int, max_cols: int) -> Dict[str, Any]:
    """The viewer's grid: `{sheets: [{name, rows, cols}], sheet: {name,
    columns, rows, truncated, formulas}}`. Formulas as text, never
    evaluated; hidden sheets excluded; `sheet` None means the first."""
    from openpyxl import load_workbook  # lazy

    max_rows = max(1, min(int(max_rows), T.MAX_ROWS_PER_SHEET))
    max_cols = max(1, min(int(max_cols), T.MAX_COLUMNS_PER_SHEET))
    wb = load_workbook(str(xlsx_path), read_only=True, data_only=False)
    try:
        visible = [ws for ws in wb.worksheets if ws.sheet_state == "visible"]
        listing: List[Dict[str, Any]] = [
            {"name": ws.title, "rows": int(ws.max_row or 0), "cols": int(ws.max_column or 0)} for ws in visible
        ]
        if not visible:
            return {"sheets": [], "sheet": None}
        chosen = visible[0]
        if sheet:
            for ws in visible:
                if ws.title == sheet:
                    chosen = ws
                    break
            else:
                raise KeyError(sheet)
        # Trimmed to the sheet's real last column: iter_rows pads every row
        # to max_col, and a three-column sheet used to come back as sixty
        # columns, fifty-seven of them blank.
        real_cols = int(chosen.max_column or 0)
        width = max(1, min(max_cols, real_cols)) if real_cols else max_cols
        columns: List[str] = []
        rows: List[List[Any]] = []
        formulas: Dict[str, str] = {}
        truncated = False
        for r_index, row in enumerate(chosen.iter_rows(min_row=1, max_col=width, values_only=False), start=1):
            if r_index == 1:
                columns = ["" if c.value is None else str(c.value) for c in row]
                continue
            if len(rows) >= max_rows:
                truncated = True
                break
            out_row: List[Any] = []
            for c in row:
                v = c.value
                if isinstance(v, str) and v.startswith("=") and c.data_type == "f":
                    formulas[c.coordinate] = v
                    out_row.append(v)
                else:
                    out_row.append(_json_value(v))
            rows.append(out_row)
        cols_truncated = int(chosen.max_column or 0) > max_cols
        return {
            "sheets": listing,
            "sheet": {
                "name": chosen.title,
                "columns": columns,
                "rows": rows,
                "truncated": bool(truncated or cols_truncated),
                "formulas": formulas,
            },
        }
    finally:
        wb.close()


def grid_for(path: str | Path, fmt: str, *, sheet: Optional[str] = "", offset: int = 0, limit: int = 500,
             max_cols: int = 60, title: str = "") -> Dict[str, Any]:
    """CONTRACT-2 §11: the grid dict for one file. `sheets` lists the
    workbook's visible sheet names (for a csv, the one name the file has:
    `title`, else its stem); `sheet` is the one shown; `rows` is the page
    from `offset` (0-based data rows) of at most `limit`; `total_rows` and
    `total_columns` are the whole sheet's; `truncated` says a row or a
    column lies outside the page. Formulas are text, never evaluated.
    Raises KeyError for a sheet the workbook does not have."""
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), T.MAX_ROWS_PER_SHEET))
    max_cols = max(1, min(int(max_cols), T.MAX_COLUMNS_PER_SHEET))
    if fmt == "csv":
        from .csv import read_csv_grid  # lazy: keeps this module's imports light

        page = read_csv_grid(path, offset=offset, limit=limit, max_cols=max_cols)
        name = (title or "").strip() or Path(path).stem
        return {
            "sheets": [name], "sheet": name, "columns": list(page["columns"]), "rows": list(page["rows"]),
            "total_rows": int(page["total_rows"]), "total_columns": int(page["total_columns"]),
            "truncated": bool(page["truncated"]), "formulas_as_text": True,
        }
    if fmt != "xlsx":
        raise ValueError(f"no grid for {fmt}")
    # sheet_grid reads from the top: the page is sliced out of offset+limit
    # rows, which is one read and at most the page plus its offset in
    # memory (a 10,000-row sheet paged at 500 costs 10,000 rows once, on
    # the last page — acceptable for a viewer that pages forward).
    raw = sheet_grid(path, sheet or None, max_rows=offset + limit, max_cols=max_cols)
    chosen = raw.get("sheet") or {}
    listing = raw.get("sheets") or []
    names = [str(s.get("name")) for s in listing]
    all_rows = list(chosen.get("rows") or [])
    page_rows = all_rows[offset: offset + limit]
    facts = next((s for s in listing if str(s.get("name")) == str(chosen.get("name"))), {})
    total_rows = max(0, int(facts.get("rows") or 0) - 1) if facts else len(all_rows)
    total_columns = int(facts.get("cols") or len(chosen.get("columns") or []))
    return {
        "sheets": names, "sheet": str(chosen.get("name") or ""), "columns": list(chosen.get("columns") or []),
        "rows": page_rows, "total_rows": total_rows, "total_columns": total_columns,
        "truncated": total_rows > offset + len(page_rows) or total_columns > max_cols,
        "formulas_as_text": True,
    }


__all__ = ["page_count", "rasterise_page", "sheet_grid", "grid_for"]
