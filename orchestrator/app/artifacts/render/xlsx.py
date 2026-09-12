"""WorkbookSpec → .xlsx with openpyxl: typed columns, real formulas, native charts.

WHAT THE MODEL NEVER WRITES. A `Total` in the spec names a column and a
function; THIS module writes `=SUM(B2:B41)` from the row count. No string in
the spec is ever placed as a formula: any text cell that begins with
`= + - @ TAB CR` is stored as an inline string with the quotePrefix style
(the apostrophe Excel would add if a person typed it), so `=HYPERLINK(...)`
from a CRM field is text in every spreadsheet application, never a formula.
`test_artifact_render_xlsx.py` reopens the file and checks the cell type.
This is the neutralisation core/exports.py lacks (CURRENT_STATE.md: "formula
injection is unguarded").

FORMATS. Number formats follow the column type — integer `#,##0`, number
`#,##0.00`, currency `#,##0.00`, percent `0.0%`, date `yyyy-mm-dd` with a
real date cell when the text parses as an ISO date. A percent column's values
are taken as FRACTIONS (0.125 → 12.5 %) when every value is within [-1, 1]
and as percentages otherwise, and the choice is recorded as a warning when it
was the latter, because a column of 12.5 meaning 1250 % is the classic
silent spreadsheet error.

DASHBOARD. The `dashboard` template puts a first sheet named Dashboard in
front: one KPI cell per total (a formula that points at the data sheet's
totals row) and every chart, so the workbook opens on the summary. Data
sheets follow. openpyxl is imported lazily; the file is reopened by
validate.py.

STYLE (CONTRACT-2 §4, `spec.SheetStyle`). A sheet without a style gets the
defaults — thin black borders on every cell, a bold header on a navy fill —
and a sheet with one gets what it says: header fill dark/light/none with a
font that reads on it, a highlighted column in the red/amber/green/blue
pairs Excel's own conditional formats use (dark text on a light fill for
the cells, white on the dark tone for the header), wrapped text, top
alignment. A CSV carries none of this; the Word/PDF companions carry the
same pairs (docx.py, html.py) so the four files agree.

LONG SHEETS AND CHARTS. A chart over a 500-row sheet is not 500 bars: past
types.MAX_CHART_POINTS rows the chart is drawn over an aggregate — the
series summed per distinct category, the top CHART_TOP_CATEGORIES by the
first series and an "Other" bucket — written as a data block beside the
sheet and referenced from there, so the chart still points at real cells.
`sheet_titles` is the one place the workbook's sheet names are derived, so
validate.py can find each spec sheet in the file it reopens.
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .. import spec as S
from .. import types as T
from . import theme

_NUMBER_FORMATS = {
    "text": "@",
    "integer": "#,##0",
    "number": "#,##0.00",
    "currency": "#,##0.00",
    "percent": "0.0%",
    "date": "yyyy-mm-dd",
}
_FN_NAMES = {"sum": "SUM", "average": "AVERAGE", "count": "COUNTA", "min": "MIN", "max": "MAX"}
_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$")
#: "007", "00123": an identifier written with leading zeros, which a number
#: would lose. Never coerced, whatever the column type says.
_LEADING_ZERO_RE = re.compile(r"^\s*[-+]?0\d")

#: The highlight pairs (header fill, header font, cell fill, cell font),
#: the tones Excel's built-in conditional formats use — dark text on a
#: light fill reads at better than 7:1; white on the dark tone above 4.5:1.
HIGHLIGHT_COLOURS = {
    "red": ("9C0006", "FFFFFF", "FFC7CE", "9C0006"),
    "amber": ("9C5700", "FFFFFF", "FFEB9C", "9C5700"),
    "green": ("006100", "FFFFFF", "C6EFCE", "006100"),
    "blue": ("1F4E78", "FFFFFF", "DDEBF7", "1F4E78"),
}
#: Header fills by SheetStyle.header_fill: (fill or None, font colour).
HEADER_FILLS = {
    "dark": (theme.NAVY.lstrip("#").upper(), theme.WHITE.lstrip("#").upper()),
    "light": (theme.SURFACE_2.lstrip("#").upper(), theme.INK.lstrip("#").upper()),
    "none": (None, theme.INK.lstrip("#").upper()),
}
BORDER_COLOUR = "000000"
#: How many categories an aggregated chart keeps before "Other".
CHART_TOP_CATEGORIES = 20

#: Sheet names Excel refuses: over 31 chars or with []:*?/\ — the spec
#: already strips those (spec.Sheet._sheet_name); this is the last guard.
_SHEET_BAD_RE = re.compile(r"[\[\]:*?/\\]")


def safe_sheet_name(name: str, fallback: str) -> str:
    cleaned = _SHEET_BAD_RE.sub(" ", name or "").strip()[:31].strip()
    return cleaned or fallback


def sheet_titles(spec: S.WorkbookSpec) -> List[str]:
    """The worksheet title each spec sheet gets, in spec order — the one
    derivation the writer and the validator share, so a sheet renamed to
    stay legal ("Notes: [draft]" → "Notes   draft", a second "Data" →
    "Data-2", "Dashboard" on a dashboard workbook → "Dashboard data") is
    found again when the file is reopened."""
    used: set = set()
    out: List[str] = []
    for index, sheet in enumerate(spec.sheets[: T.MAX_SHEETS]):
        title = safe_sheet_name(sheet.name, f"Sheet{index + 1}")
        if title.lower() == "dashboard" and spec.template_id == "dashboard":
            title = safe_sheet_name(f"{title} data", f"Sheet{index + 1}")
        while title.lower() in used:  # Excel compares titles case-insensitively
            title = safe_sheet_name(f"{title[:28]}-{index + 1}", f"Sheet{index + 1}")
        used.add(title.lower())
        out.append(title)
    return out


def sheet_style(sheet: S.Sheet) -> S.SheetStyle:
    """The sheet's style, or the defaults when the model gave none."""
    return sheet.style if sheet.style is not None else S.SheetStyle()


def is_landscape(sheet: S.Sheet) -> bool:
    """CONTRACT-2 §4: `auto` is landscape when the sheet has more than six
    columns; the tabular Word/PDF documents read this too."""
    style = sheet_style(sheet)
    if style.orientation == "auto":
        return len(sheet.columns) > 6
    return style.orientation == "landscape"


def highlight_for(sheet: S.Sheet) -> dict:
    """Column index → highlight colour name, for the columns the style
    names (matched by folded header text, as the spec validated them)."""
    style = sheet_style(sheet)
    if not style.highlight:
        return {}
    by_name = {}
    for j, c in enumerate(sheet.columns):
        by_name.setdefault(" ".join(c.name.split()).casefold(), j)
    out = {}
    for h in style.highlight:
        j = by_name.get(" ".join(h.column.split()).casefold())
        if j is not None:
            out[j] = h.color
    return out


def is_formula_like(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_FORMULA_LEADS)


def _parse_date(value: Any) -> Optional[_dt.date]:
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value if isinstance(value, _dt.date) else value.date()
    if isinstance(value, str):
        m = _ISO_DATE_RE.match(value.strip())
        if m:
            try:
                return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
    return None


def _write_value(cell, value: Any, column_type: str, *, percent_scale: float) -> None:
    """Store `value` in `cell` as the column type asks, or as text when it
    cannot be read that way. Text is NEVER a formula."""
    if value is None or (isinstance(value, str) and not value.strip()):
        cell.value = None
        return
    if column_type == "date":
        parsed = _parse_date(value)
        if parsed is not None:
            cell.value = parsed
            cell.number_format = _NUMBER_FORMATS["date"]
            return
        _write_text(cell, str(value))
        return
    if column_type in ("integer", "number", "currency", "percent"):
        number = _as_number(value)
        if number is not None:
            if column_type == "integer" and abs(number - round(number)) < 1e-9:
                number = int(round(number))
            if column_type == "percent":
                number = number * percent_scale
            cell.value = number
            cell.number_format = _NUMBER_FORMATS[column_type]
            return
        _write_text(cell, str(value))
        return
    _write_text(cell, value if isinstance(value, str) else str(value))


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        if _LEADING_ZERO_RE.match(value):
            # "007" is an identifier: as a number it is 7, and the file
            # would have silently lost two characters of a key.
            return None
        cleaned = value.strip().replace(",", "")
        if cleaned.endswith("%"):
            cleaned = cleaned[:-1]
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _write_text(cell, text: str) -> None:
    """A string cell that Excel treats as text whatever it starts with.
    openpyxl classifies a leading `=` as a formula (`data_type == 'f'`); the
    override below makes it a string and the quotePrefix style records the
    apostrophe a person would have typed."""
    cell.value = text
    cell.data_type = "s"
    if is_formula_like(text):
        cell.quotePrefix = True
    cell.number_format = "@"


def _column_letter(index: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(index + 1)


def _percent_scale(sheet: S.Sheet, col_index: int, warnings: List[str]) -> float:
    """1.0 when the column's values are fractions (all within [-1, 1]),
    0.01 when they are whole percentages."""
    numbers = [_as_number(r[col_index]) for r in sheet.rows]
    numbers = [n for n in numbers if n is not None]
    if not numbers:
        return 1.0
    if all(-1.0 <= n <= 1.0 for n in numbers):
        return 1.0
    warnings.append(
        f"Sheet {sheet.name!r}: the percent column {sheet.columns[col_index].name!r} holds whole numbers "
        "(for example 12.5), so they were stored as 12.5% rather than 1250%."
    )
    return 0.01


def _write_sheet(ws, sheet: S.Sheet, warnings: List[str]) -> dict:
    """Header, rows, totals, widths, freeze, filter, charts. Returns the
    layout ({first_row, last_row, totals_row, column_letters}) the dashboard
    sheet points its KPI formulas at."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    n_cols = len(sheet.columns)
    n_rows = len(sheet.rows)
    style = sheet_style(sheet)
    highlight = highlight_for(sheet)
    fill_hex, header_colour = HEADER_FILLS.get(style.header_fill, HEADER_FILLS["dark"])
    header_font = Font(bold=style.header_bold, color=header_colour, name=theme.OFFICE_SANS)
    header_fill = PatternFill("solid", fgColor=fill_hex) if fill_hex else None
    body_font = Font(name=theme.OFFICE_SANS)
    thin = Side(style="thin", color=BORDER_COLOUR)
    grid = Border(left=thin, right=thin, top=thin, bottom=thin) if style.borders == "thin" else Border()
    header_border = grid if style.borders == "thin" else Border(bottom=Side(style="thin", color=theme.BORDER.lstrip("#").upper()))

    for j, col in enumerate(sheet.columns):
        cell = ws.cell(row=1, column=j + 1)
        _write_text(cell, col.name)
        colour = highlight.get(j)
        if colour:
            h_fill, h_font, _, _ = HIGHLIGHT_COLOURS[colour]
            cell.font = Font(bold=style.header_bold, color=h_font, name=theme.OFFICE_SANS)
            cell.fill = PatternFill("solid", fgColor=h_fill)
        else:
            cell.font = header_font
            if header_fill is not None:
                cell.fill = header_fill
        cell.alignment = Alignment(horizontal="right" if col.type != "text" else "left", vertical="center", wrap_text=style.wrap)
        cell.border = header_border

    scales = {j: (_percent_scale(sheet, j, warnings) if col.type == "percent" else 1.0) for j, col in enumerate(sheet.columns)}
    cell_fills = {}
    cell_fonts = {}
    for j, colour in highlight.items():
        _, _, c_fill, c_font = HIGHLIGHT_COLOURS[colour]
        cell_fills[j] = PatternFill("solid", fgColor=c_fill)
        cell_fonts[j] = Font(color=c_font, name=theme.OFFICE_SANS)
    for i, row in enumerate(sheet.rows):
        for j, value in enumerate(row):
            cell = ws.cell(row=i + 2, column=j + 1)
            _write_value(cell, value, sheet.columns[j].type, percent_scale=scales[j])
            cell.font = cell_fonts.get(j, body_font)
            if j in cell_fills:
                cell.fill = cell_fills[j]
            # Top-aligned so a wrapped comment reads from the row's top edge,
            # next to the id it belongs to.
            cell.alignment = Alignment(horizontal="right" if sheet.columns[j].type != "text" else None, vertical="top", wrap_text=style.wrap)
            cell.border = grid

    first_row, last_row = 2, n_rows + 1
    totals_row = None
    if sheet.totals and n_rows > 0:
        totals_row = last_row + 1
        label_written = False
        for total in sheet.totals:
            letter = _column_letter(total.column)
            cell = ws.cell(row=totals_row, column=total.column + 1)
            # The formula is written by THIS code from the row count; the spec
            # only named the column and the function.
            cell.value = f"={_FN_NAMES[total.fn]}({letter}{first_row}:{letter}{last_row})"
            col_type = sheet.columns[total.column].type
            cell.number_format = _NUMBER_FORMATS["integer"] if total.fn == "count" else _NUMBER_FORMATS.get(col_type, "#,##0.00")
            cell.font = Font(bold=True, name=theme.OFFICE_SANS)
            cell.alignment = Alignment(horizontal="right")
            cell.border = Border(top=thin)
            if not label_written and total.column > 0:
                label = ws.cell(row=totals_row, column=1)
                if label.value is None:
                    _write_text(label, total.label)
                    label.font = Font(bold=True, name=theme.OFFICE_SANS)
                    label_written = True

    # Widths: the spec's width, or the longest of header/sampled values —
    # capped lower when text wraps, so a long comment column is a readable
    # paragraph rather than a 60-character strip.
    for j, col in enumerate(sheet.columns):
        if col.width:
            width = col.width
        else:
            longest = len(col.name)
            for row in sheet.rows[:500]:
                v = row[j]
                if v is not None:
                    longest = max(longest, len(str(v)))
            width = min(max(longest + 2, 8), 45 if style.wrap else 60)
        ws.column_dimensions[_column_letter(j)].width = width
    ws.row_dimensions[1].height = 20

    if sheet.freeze_header:
        ws.freeze_panes = "A2"
    if sheet.autofilter and n_rows > 0:
        ws.auto_filter.ref = f"A1:{_column_letter(n_cols - 1)}{last_row}"

    layout = {"first_row": first_row, "last_row": last_row, "totals_row": totals_row, "n_cols": n_cols}
    _draw_charts(ws, sheet, layout, n_cols)
    return layout


def _chart_columns(sheet: S.Sheet, chart: S.Chart) -> Tuple[Optional[int], List[int]]:
    """Which sheet columns a chart's categories and series refer to, by
    NAME: the category column is the first text column whose values equal the
    chart's categories, each series the column whose name equals the series'
    name. None/empty when the chart is not over this sheet's data — then the
    chart is drawn from its own numbers on a small hidden data block."""
    cat_col = None
    cats = list(chart.categories)
    for j, col in enumerate(sheet.columns):
        values = [str(r[j]) if r[j] is not None else "" for r in sheet.rows]
        if values[: len(cats)] == cats and len(values) >= len(cats):
            cat_col = j
            break
    series_cols: List[int] = []
    names = {c.name: j for j, c in enumerate(sheet.columns)}
    for s in chart.series:
        j = names.get(s.name)
        if j is None:
            return None, []
        series_cols.append(j)
    return cat_col, series_cols


def aggregate_chart(sheet: S.Sheet, chart: S.Chart, cat_col: int, series_cols: List[int]) -> Tuple[List[str], List[List[float]]]:
    """The chart's data over EVERY row of a long sheet, summed per distinct
    category: the top CHART_TOP_CATEGORIES categories by the first series'
    total, in that order, then "Other" for the rest when there is a rest.
    Returns (categories, one value list per series)."""
    totals: dict = {}
    order: List[str] = []
    for row in sheet.rows:
        raw = row[cat_col] if cat_col < len(row) else None
        key = "" if raw is None else str(raw).strip()
        if not key:
            key = "(blank)"
        if key not in totals:
            totals[key] = [0.0] * len(series_cols)
            order.append(key)
        for s_index, j in enumerate(series_cols):
            n = _as_number(row[j] if j < len(row) else None)
            if n is not None:
                totals[key][s_index] += n
    ranked = sorted(order, key=lambda k: (-totals[k][0], order.index(k)))
    top = ranked[:CHART_TOP_CATEGORIES]
    rest = ranked[CHART_TOP_CATEGORIES:]
    categories = list(top)
    values = [[totals[k][s] for k in top] for s in range(len(series_cols))]
    if rest:
        categories.append("Other")
        for s in range(len(series_cols)):
            values[s].append(sum(totals[k][s] for k in rest))
    return categories, values


def _draw_charts(ws, sheet: S.Sheet, layout: dict, n_cols: int) -> None:
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference

    anchor_row = 2
    anchor_col = n_cols + 2
    # Charts whose data is not in the sheet's columns get their numbers in a
    # block to the right, so the chart still references real cells.
    aux_col = n_cols + 12
    layout["chart_blocks"] = {}
    for k, chart in enumerate(sheet.charts):
        cat_col, series_cols = _chart_columns(sheet, chart)
        n_points = len(chart.categories)
        over_columns = cat_col is not None and series_cols and n_points <= len(sheet.rows)
        if over_columns and len(sheet.rows) > T.MAX_CHART_POINTS:
            # A long sheet: the chart is the aggregate, not the first 200
            # rows — written beside the sheet like any other data block.
            categories, values = aggregate_chart(sheet, chart, cat_col, series_cols)
            names = [s.name for s in chart.series]
            over_columns = False
        else:
            categories, values, names = list(chart.categories), [list(s.values) for s in chart.series], [s.name for s in chart.series]
        n_points = len(categories)
        if over_columns:
            cats_ref = Reference(ws, min_col=cat_col + 1, min_row=2, max_row=1 + n_points)
            data_refs = [(Reference(ws, min_col=j + 1, min_row=1, max_row=1 + n_points), True) for j in series_cols]
        else:
            top = 1 + sum(layout["chart_blocks"][b]["rows"] + 3 for b in layout["chart_blocks"])
            head = ws.cell(row=top, column=aux_col)
            _write_text(head, chart.title or "Chart data")
            head.font = _bold()
            for i, cat in enumerate(categories):
                _write_text(ws.cell(row=top + 1 + i, column=aux_col), cat)
            for s_index, name in enumerate(names):
                _write_text(ws.cell(row=top, column=aux_col + 1 + s_index), name)
                for i, v in enumerate(values[s_index]):
                    ws.cell(row=top + 1 + i, column=aux_col + 1 + s_index, value=float(v))
            cats_ref = Reference(ws, min_col=aux_col, min_row=top + 1, max_row=top + n_points)
            data_refs = [(Reference(ws, min_col=aux_col + 1 + s, min_row=top, max_row=top + n_points), True) for s in range(len(names))]
            layout["chart_blocks"][k] = {"top": top, "col": aux_col, "rows": n_points, "series": len(names)}

        if chart.type == "pie":
            c = PieChart()
        elif chart.type == "line":
            c = LineChart()
        else:
            c = BarChart()
            c.type = "bar" if chart.type == "horizontal_bar" else "col"
            c.grouping = "clustered"
        c.title = chart.title or None
        c.height, c.width = 8.5, 16
        for ref, titles_from_data in data_refs:
            c.add_data(ref, titles_from_data=titles_from_data)
        c.set_categories(cats_ref)
        for i, series in enumerate(c.series):
            colour = theme.series_colour(i).lstrip("#").upper()
            if chart.type == "line":
                series.graphicalProperties.line.solidFill = colour
                series.graphicalProperties.line.width = 28575  # 2.25 pt in EMU
                series.smooth = False
            elif chart.type != "pie":
                series.graphicalProperties.solidFill = colour
                series.graphicalProperties.line.solidFill = colour
        if chart.type != "pie":
            c.y_axis.title = chart.y_label or None
            c.legend.position = "b"
            if len(chart.series) == 1:
                c.legend = None
        ws.add_chart(c, f"{_column_letter(anchor_col - 1)}{anchor_row}")
        anchor_row += 18


def _bold():
    from openpyxl.styles import Font

    return Font(bold=True, name=theme.OFFICE_SANS)


def _dashboard(wb, spec: S.WorkbookSpec, layouts: List[Tuple[S.Sheet, str, dict]]) -> None:
    """The first sheet: a title, one KPI cell per total (a live formula into
    the data sheet) and a copy of every chart."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import quote_sheetname

    ws = wb.create_sheet(title="Dashboard", index=0)
    ws.sheet_view.showGridLines = False
    # The title and purpose are the model's text like any cell: through
    # _write_text, or a title of `=HYPERLINK(...)` is a live formula (found
    # in review 2026-09-11 — these two were the only bare assignments).
    _write_text(ws["B2"], spec.title)
    ws["B2"].font = Font(bold=True, size=18, color=theme.NAVY.lstrip("#").upper(), name=theme.OFFICE_SANS)
    if spec.purpose:
        _write_text(ws["B3"], spec.purpose)
        ws["B3"].font = Font(color=theme.INK_MUTED.lstrip("#").upper(), name=theme.OFFICE_SANS)
    row = 5
    col = 2
    fill = PatternFill("solid", fgColor=theme.SURFACE.lstrip("#").upper())
    for sheet, title, layout in layouts:
        if not layout["totals_row"]:
            continue
        for total in sheet.totals:
            letter = _column_letter(total.column)
            label = ws.cell(row=row, column=col)
            _write_text(label, f"{sheet.columns[total.column].name} ({total.fn})")
            label.font = Font(size=9, color=theme.INK_MUTED.lstrip("#").upper(), name=theme.OFFICE_SANS)
            label.fill = fill
            value = ws.cell(row=row + 1, column=col)
            # quote_sheetname doubles an apostrophe inside the name ("Q1's
            # data" → 'Q1''s data'), which is legal in a sheet name and which
            # a hand-built f"'{title}'" turned into Err:509 in LibreOffice.
            value.value = f"={quote_sheetname(title)}!{letter}{layout['totals_row']}"
            value.font = Font(bold=True, size=16, color=theme.NAVY.lstrip("#").upper(), name=theme.OFFICE_SANS)
            value.fill = fill
            value.alignment = Alignment(horizontal="left")
            col_type = sheet.columns[total.column].type
            value.number_format = _NUMBER_FORMATS["integer"] if total.fn == "count" else _NUMBER_FORMATS.get(col_type, "#,##0.00")
            ws.column_dimensions[_column_letter(col - 1)].width = 22
            col += 2
            if col > 12:
                col = 2
                row += 3
    row += 3
    # Charts: re-drawn on the dashboard from the data sheets' cells.
    chart_row = row
    for sheet, title, layout in layouts:
        data_ws = wb[title]
        # openpyxl charts are bound to one worksheet; rebuilding them here
        # against the data sheet keeps the dashboard chart live.
        _draw_dashboard_charts(ws, data_ws, sheet, layout, chart_row)
        chart_row += 18 * len(sheet.charts)


def _draw_dashboard_charts(ws, data_ws, sheet: S.Sheet, layout: dict, start_row: int) -> None:
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference

    row = start_row
    blocks = layout.get("chart_blocks") or {}
    for k, chart in enumerate(sheet.charts):
        cat_col, series_cols = _chart_columns(sheet, chart)
        n_points = len(chart.categories)
        block = blocks.get(k)
        over_columns = cat_col is not None and series_cols and n_points <= len(sheet.rows) and block is None
        if not over_columns and block is None:
            continue  # its data block lives on the data sheet; the chart is there
        if chart.type == "pie":
            c = PieChart()
        elif chart.type == "line":
            c = LineChart()
        else:
            c = BarChart()
            c.type = "bar" if chart.type == "horizontal_bar" else "col"
        c.title = chart.title or None
        c.height, c.width = 8.5, 16
        if block is not None:
            top, col, n_points = block["top"], block["col"], block["rows"]
            for s_index in range(block["series"]):
                c.add_data(Reference(data_ws, min_col=col + 1 + s_index, min_row=top, max_row=top + n_points), titles_from_data=True)
            c.set_categories(Reference(data_ws, min_col=col, min_row=top + 1, max_row=top + n_points))
        else:
            for j in series_cols:
                c.add_data(Reference(data_ws, min_col=j + 1, min_row=1, max_row=1 + n_points), titles_from_data=True)
            c.set_categories(Reference(data_ws, min_col=cat_col + 1, min_row=2, max_row=1 + n_points))
        for i, series in enumerate(c.series):
            colour = theme.series_colour(i).lstrip("#").upper()
            if chart.type == "line":
                series.graphicalProperties.line.solidFill = colour
            elif chart.type != "pie":
                series.graphicalProperties.solidFill = colour
        ws.add_chart(c, f"B{row}")
        row += 18


def render_xlsx(spec: S.WorkbookSpec, out_path: str | Path, *, warnings: Optional[List[str]] = None) -> Path:
    """Write the workbook to `out_path` and return it."""
    from openpyxl import Workbook

    warnings = warnings if warnings is not None else []
    wb = Workbook()
    wb.remove(wb.active)
    layouts: List[Tuple[S.Sheet, str, dict]] = []
    for sheet, title in zip(spec.sheets[: T.MAX_SHEETS], sheet_titles(spec)):
        if len(sheet.rows) > T.MAX_ROWS_PER_SHEET:
            warnings.append(f"Sheet {sheet.name!r}: {len(sheet.rows):,} rows were cut to the {T.MAX_ROWS_PER_SHEET:,}-row ceiling.")
            sheet = sheet.model_copy(update={"rows": sheet.rows[: T.MAX_ROWS_PER_SHEET]})
        ws = wb.create_sheet(title=title)
        layouts.append((sheet, title, _write_sheet(ws, sheet, warnings)))
    if spec.template_id == "dashboard":
        _dashboard(wb, spec, layouts)
    if spec.sources or spec.assumptions:
        ws = wb.create_sheet(title="Notes")
        r = 1
        if spec.sources:
            ws.cell(row=r, column=1, value="Sources").font = _bold()
            r += 1
            for n, c in enumerate(spec.sources, start=1):
                _write_text(ws.cell(row=r, column=1), f"[{n}]")
                _write_text(ws.cell(row=r, column=2), c.title)
                if c.url:
                    _write_text(ws.cell(row=r, column=3), c.url)
                r += 1
            r += 1
        if spec.assumptions:
            ws.cell(row=r, column=1, value="Assumptions").font = _bold()
            r += 1
            for a in spec.assumptions:
                _write_text(ws.cell(row=r, column=2), a)
                r += 1
        ws.column_dimensions["B"].width = 60
        ws.column_dimensions["C"].width = 50
    props = wb.properties
    props.title = spec.title
    props.creator = "TechSara Local AI"
    props.description = spec.purpose or "Generated by TechSara Local AI Artifact Studio"
    out = Path(out_path)
    wb.save(str(out))
    return out


__all__ = [
    "render_xlsx", "is_formula_like", "safe_sheet_name", "sheet_titles", "sheet_style", "is_landscape",
    "highlight_for", "aggregate_chart", "HIGHLIGHT_COLOURS", "HEADER_FILLS", "BORDER_COLOUR", "CHART_TOP_CATEGORIES",
]
