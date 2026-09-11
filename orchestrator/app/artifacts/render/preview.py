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
"""
from __future__ import annotations

import datetime as _dt
import io
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import types as T


def page_count(pdf_path: str | Path) -> int:
    import pypdfium2 as pdfium  # lazy: arm64 wheel, no system deps

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
    if isinstance(value, (_dt.datetime, _dt.date)):
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
        columns: List[str] = []
        rows: List[List[Any]] = []
        formulas: Dict[str, str] = {}
        truncated = False
        for r_index, row in enumerate(chosen.iter_rows(min_row=1, max_col=max_cols, values_only=False), start=1):
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


__all__ = ["page_count", "rasterise_page", "sheet_grid"]
