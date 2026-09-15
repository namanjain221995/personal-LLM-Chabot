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

STYLE (style guide §4, artifacts/style.py). Every workbook looks
professional by default — TechSara Classic: a #1F3864 header with white
bold 11 pt text, 24 pt tall, a medium rule under it; thin #E5E9F0 grid
lines (never black); banded rows; the header frozen; widths from content;
number, currency, percent and date formats per column; per-VALUE status
colours in status-like columns and a 3-colour scale on score columns; a
styled SUBTOTAL totals row; landscape past six columns, one page wide, the
header repeated, "Page X of Y". `spec.style` (a StyleSpec, written by code
from the person's request) overrides any of it, and the legacy
`spec.SheetStyle` still works: header fill dark/light/none, borders,
wrapping and the highlighted-column pairs exactly as before.

CONDITIONAL-FORMAT PRIORITY. Excel paints a CF fill ABOVE a cell fill, so
banding as a CF would hide a fill a person asked for. `sheet_cf_plan` adds
rules highest priority first: user fills (highlighted columns, a column,
a row, a cell range — stopIfTrue) → user conditions and colour scales →
automatic status/score/due-date colours → banding. Formulas are built by
code: column letters from code, user values only through
style.xlsx_formula_literal (quotes doubled, capped). A cell range is
clipped to the used range before anything is styled, and a range over
10,000 cells is carried by its single CF rule instead of per-cell styles.
A CSV carries none of this; the Word/PDF companions read the same
ResolvedStyle (docx.py, html.py) so the files agree.

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
from typing import Any, List, Optional, Sequence, Tuple

from .. import spec as S
from .. import style as ST
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
#: Header fills by SheetStyle.header_fill on the Classic palette: (fill or None, font colour).
HEADER_FILLS = {
    "dark": (theme.CLASSIC_PRIMARY.lstrip("#").upper(), theme.WHITE.lstrip("#").upper()),
    "light": ("EEF0F3", theme.CLASSIC_INK.lstrip("#").upper()),
    "none": (None, theme.CLASSIC_INK.lstrip("#").upper()),
}
#: The grid line colour (style guide §4: thin #E5E9F0, never black).
BORDER_COLOUR = theme.CLASSIC_GRID.lstrip("#").upper()
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


def _hex6(colour: Optional[str]) -> Optional[str]:
    return colour.lstrip("#").upper() if colour else None


def number_format_for(col: S.Column, values: Sequence[Any] = ()) -> str:
    """The Excel number format of a column (style guide §4): integer
    `#,##0`; number `#,##0.00`, or `#,##0` when every value is integral, with
    a red negative section when a value is negative; currency by symbol
    (INR in lakh/crore grouping); percent `0.0%`; date `dd-mmm-yyyy`
    (`yyyy-mm-dd` when the column asks for ISO)."""
    fmt = col.format
    kind = (fmt.kind if fmt is not None and fmt.kind else None) or {"integer": "integer", "number": "decimal", "currency": "currency", "percent": "percent", "date": "date"}.get(col.type, "text")
    decimals = fmt.decimals if fmt is not None else None
    if kind == "text":
        return "@"
    if kind == "integer":
        return "#,##0"
    if kind == "percent":
        return "0.0%" if decimals is None else ("0%" if decimals == 0 else "0." + "0" * decimals + "%")
    if kind in ("date", "datetime"):
        iso = fmt is not None and fmt.date_style == "iso"
        base = "yyyy-mm-dd" if iso else "dd-mmm-yyyy"
        return base + (" hh:mm" if kind == "datetime" else "")
    places = 2 if decimals is None else decimals
    tail = ("." + "0" * places) if places else ""
    if kind == "currency":
        code = fmt.currency if fmt is not None else None
        if code == "INR":
            return f'[>=10000000]"₹"##\\,##\\,##\\,##0{tail};[>=100000]"₹"##\\,##\\,##0{tail};"₹"##,##0{tail}'
        symbol = {"USD": '"$"', "EUR": '"€"', "GBP": '"£"'}.get(code or "", "")
        return f"{symbol}#,##0{tail}"
    numbers = [n for n in (_as_number(v) for v in values) if n is not None]
    if decimals is None and numbers and all(abs(n - round(n)) < 1e-9 for n in numbers):
        tail = ""
    base = f"#,##0{tail}"
    if col.type == "number" and any(n < 0 for n in numbers):
        return f"{base};[Red]-{base}"
    return base


#: Legacy SheetStyle.header_fill "light" on the Classic palette.
LEGACY_LIGHT_HEADER = ("#EEF0F3", "#1F2937")
#: SUBTOTAL function numbers: they ignore rows a filter hides.
_SUBTOTAL = {"sum": 109, "average": 101, "count": 103, "min": 105, "max": 104}
_ID_COLUMN_RE = re.compile(r"^\s*(id|#|no\.?|key|code|ref|s\.?\s?no\.?|sr\.?\s?no\.?)\b", re.IGNORECASE)


class _StyleCache:
    """openpyxl style objects shared across cells: one Font/Fill/Alignment
    per distinct value, not one per cell (a 10,000-row sheet)."""

    def __init__(self, resolved: ST.ResolvedStyle):
        self.R = resolved
        self._fonts: dict = {}
        self._fills: dict = {}

    def font(self, ts: ST.TextStyle, *, size: Optional[float] = None):
        from openpyxl.styles import Font

        face = self.R.face(ts.font_family)
        key = (face.office_name, size or ts.size_pt, bool(ts.bold), bool(ts.italic), bool(ts.underline), ts.color)
        f = self._fonts.get(key)
        if f is None:
            f = Font(name=face.office_name, size=key[1], bold=key[2], italic=key[3], underline="single" if key[4] else None, color=_hex6(ts.color))
            self._fonts[key] = f
        return f

    def fill(self, colour: Optional[str]):
        from openpyxl.styles import PatternFill

        if not colour:
            return None
        f = self._fills.get(colour)
        if f is None:
            f = PatternFill("solid", fgColor=_hex6(colour))
            self._fills[colour] = f
        return f


def on_fill(rule_style: ST.TextStyle, base: ST.TextStyle) -> ST.TextStyle:
    """`rule_style` over `base`; when the rule set a fill but no text
    colour, the SYSTEM's text colour flips to white or ink so it reads."""
    ts = rule_style.over(base)
    if rule_style.background and rule_style.color is None and ST.contrast_ratio(ts.color or ST.INK, rule_style.background) < 4.5:
        ts = ST.TextStyle.model_construct(**{**ts.model_dump(), "color": ST.readable_on(rule_style.background)})
    return ts


def _cf_font(ts: ST.TextStyle, base: ST.TextStyle):
    """The differential font of a CF rule: only what differs from the
    cell's base (colour, bold, italic, underline) — Excel ignores a dxf
    font's name and size."""
    from openpyxl.styles import Font

    kw = {}
    if ts.color and ts.color != base.color:
        kw["color"] = _hex6(ts.color)
    if ts.bold and not base.bold:
        kw["bold"] = True
    if ts.italic and not base.italic:
        kw["italic"] = True
    if ts.underline and not base.underline:
        kw["underline"] = "single"
    return Font(**kw) if kw else None


def _cf_fill(colour: Optional[str]):
    from openpyxl.styles import PatternFill

    if not colour:
        return None
    return PatternFill(fill_type="solid", start_color=_hex6(colour), end_color=_hex6(colour), bgColor=_hex6(colour))


def cond_formula(rule: ST.CondRule, letter: str, first_row: int) -> str:
    """The CF formula of a user condition, anchored at the first data row.
    Column letters come from code; user values only as escaped literals."""
    ref = f"${letter}{first_row}"
    lit = ST.xlsx_formula_literal
    if rule.op == "blank":
        return f"LEN(TRIM({ref}))=0"
    if rule.op == "date_past":
        return f"AND(ISNUMBER({ref}),{ref}<TODAY())"
    if rule.op == "contains":
        return f"ISNUMBER(SEARCH({ST.xlsx_search_literal(rule.value)},{ref}))"
    if rule.op == "in":
        parts = [(f"TRIM({ref})={lit(v)}" if isinstance(v, str) else f"{ref}={lit(v)}") for v in rule.values[:20]]
        return "OR(" + ",".join(parts) + ")"
    if rule.op == "between":
        return f"AND(ISNUMBER({ref}),{ref}>={lit(rule.value)},{ref}<={lit(rule.value2)})"
    if rule.op in ("gt", "gte", "lt", "lte"):
        sym = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[rule.op]
        return f"AND(ISNUMBER({ref}),{ref}{sym}{lit(rule.value)})"
    sym = "=" if rule.op == "eq" else "<>"
    if isinstance(rule.value, str):
        return f"TRIM({ref}){sym}{lit(rule.value)}"
    return f"{ref}{sym}{lit(rule.value)}"


def status_formula(letter: str, first_row: int, values: Sequence[str]) -> str:
    ref = f"${letter}{first_row}"
    return "OR(" + ",".join(f"TRIM({ref})={ST.xlsx_formula_literal(v)}" for v in values) + ")"


def status_like(col: S.Column) -> bool:
    return col.type == "text" and bool(ST.STATUS_COLUMN_RE.search(col.name))


def score_like(col: S.Column) -> bool:
    return col.type in ("integer", "number", "percent") and bool(ST.SCORE_COLUMN_RE.search(col.name))


def due_like(col: S.Column) -> bool:
    return col.type == "date" and bool(ST.DUE_COLUMN_RE.search(col.name))


def sheet_cf_plan(sheet: S.Sheet, R: ST.ResolvedStyle, *, first_row: int = 2, last_row: Optional[int] = None) -> List[dict]:
    """Every conditional format a sheet gets, HIGHEST priority first (the
    order openpyxl numbers them): user fills on named cells (legacy
    highlight, a row, a cell range) → user conditions and colour scales →
    user fills on a whole column or the table body → automatic
    status/score/due-date colours → banding. A requested fill is therefore
    never hidden by banding or by an automatic colour, and a requested
    condition ("Status red for Blocked") is not hidden by a broader
    requested column fill. Only a rule that paints a FILL carries
    stopIfTrue: a font-only rule ("Owner column bold") merges with the
    lower rules the way Excel merges non-conflicting formats, so the
    column keeps its banding and status colours.
    Each entry: {kind, level, range, formula | scale, fill, font, stop,
    column}. The preview evaluates the same plan in Python."""
    from openpyxl.utils import get_column_letter

    n_cols = len(sheet.columns)
    n_rows = len(sheet.rows)
    last_row = last_row if last_row is not None else n_rows + 1
    if n_rows == 0:
        return []
    last_letter = get_column_letter(n_cols)
    plan: List[dict] = []
    body_base = R.base("table_body")
    by_name = {ST._fold(c.name): j for j, c in enumerate(sheet.columns)}

    def add(kind: str, level: str, rng: str, *, formula: Optional[str] = None, fill: Optional[str] = None, ts: Optional[ST.TextStyle] = None,
            stop: bool = False, scale: Optional[Tuple[str, Optional[str], str]] = None, column: Optional[int] = None) -> None:
        plan.append({"kind": kind, "level": level, "range": rng, "formula": formula, "fill": fill, "style": ts, "stop": stop, "scale": scale, "column": column})

    # 1. user fills on named cells (whole-column and table-body fills are
    # collected in `broad` and placed after the user conditions)
    broad: List[dict] = []
    for j, colour in highlight_for(sheet).items():
        letter = get_column_letter(j + 1)
        _, _, c_fill, c_font = HIGHLIGHT_COLOURS[colour]
        add("highlight", "user_fill", f"{letter}{first_row}:{letter}{last_row}", formula="TRUE", fill="#" + c_fill,
            ts=ST.TextStyle.model_construct(**{**body_base.model_dump(), "color": "#" + c_font}), stop=True, column=j)
    for r in R.rules:
        t = r.target
        if t.sheet is not None and ST._fold(t.sheet) != ST._fold(sheet.name):
            continue
        s = r.style
        if s.background is None and s.color is None and not (s.bold or s.italic or s.underline):
            continue
        if t.kind == "column":
            j = by_name.get(ST._fold(t.name)) if t.name else ((t.index - 1) if t.index and t.index <= n_cols else None)
            if j is None:
                continue
            letter = get_column_letter(j + 1)
            broad.append(dict(kind="column", level="user_fill", rng=f"{letter}{first_row}:{letter}{last_row}", formula="TRUE", fill=s.background,
                              ts=on_fill(s, body_base), stop=bool(s.background), column=j))
        elif t.kind == "row" and t.index is not None:
            if first_row <= t.index <= last_row:
                add("row", "user_fill", f"A{t.index}:{last_letter}{t.index}", formula="TRUE", fill=s.background, ts=on_fill(s, body_base), stop=bool(s.background))
        elif t.kind == "cell_range" and t.a1:
            clipped = ST.clip_a1(t.a1, n_cols, last_row)
            if clipped is None:
                continue
            c1, r1, c2, r2 = clipped
            r1 = max(r1, first_row)
            if r1 > r2:
                continue
            add("cell_range", "user_fill", f"{get_column_letter(c1)}{r1}:{get_column_letter(c2)}{r2}", formula="TRUE", fill=s.background, ts=on_fill(s, body_base), stop=bool(s.background))
        elif t.kind in ("table_body", "table") and s.background:
            broad.append(dict(kind="table_body", level="user_fill", rng=f"A{first_row}:{last_letter}{n_rows + 1}", formula="TRUE", fill=s.background,
                              ts=on_fill(s, body_base), stop=True, column=None))
    # 2. user conditions and scales
    for c in R.conditional:
        if c.sheet is not None and ST._fold(c.sheet) != ST._fold(sheet.name):
            continue
        j = by_name.get(ST._fold(c.column))
        if j is None:
            continue
        letter = get_column_letter(j + 1)
        rng = f"A{first_row}:{last_letter}{n_rows + 1}" if c.whole_row else f"{letter}{first_row}:{letter}{n_rows + 1}"
        ts = c.style.over(body_base)
        if c.style.background and c.style.color is None:
            ts = ST.TextStyle.model_construct(**{**ts.model_dump(), "color": ST.readable_on(c.style.background) if ST.contrast_ratio(body_base.color or ST.INK, c.style.background) < 4.5 else None})
        add("condition", "user_cond", rng, formula=cond_formula(c, letter, first_row), fill=c.style.background, ts=ts, column=j)
    for sc in R.scales:
        if sc.sheet is not None and ST._fold(sc.sheet) != ST._fold(sheet.name):
            continue
        j = by_name.get(ST._fold(sc.column))
        if j is None:
            continue
        letter = get_column_letter(j + 1)
        add("scale", "user_cond", f"{letter}{first_row}:{letter}{n_rows + 1}", scale=(sc.min_color, sc.mid_color, sc.max_color), column=j)
    for b in broad:
        add(b["kind"], b["level"], b["rng"], formula=b["formula"], fill=b["fill"], ts=b["ts"], stop=b["stop"], column=b["column"])
    # 3. automatic colours
    status_cols = [j for j, col in enumerate(sheet.columns) if status_like(col)]
    if R.auto_status_colors:
        for j in status_cols:
            letter = get_column_letter(j + 1)
            for cls in ("success", "warning", "danger", "info"):
                fill, text = ST.STATUS_PAIRS[cls]
                add("status", "auto", f"{letter}{first_row}:{letter}{n_rows + 1}", formula=status_formula(letter, first_row, ST.STATUS_VALUES[cls]),
                    fill=fill, ts=ST.TextStyle.model_construct(**{**body_base.model_dump(), "color": text}), column=j)
        for j, col in enumerate(sheet.columns):
            if due_like(col):
                letter = get_column_letter(j + 1)
                formula = f"AND(ISNUMBER(${letter}{first_row}),${letter}{first_row}<TODAY()"
                if status_cols:
                    sl = get_column_letter(status_cols[0] + 1)
                    formula += ",NOT(" + status_formula(sl, first_row, ST.STATUS_VALUES["success"]) + ")"
                formula += ")"
                add("due", "auto", f"{letter}{first_row}:{letter}{n_rows + 1}", formula=formula, fill=None,
                    ts=ST.TextStyle.model_construct(**{**body_base.model_dump(), "color": ST.STATUS_PAIRS["danger"][1]}), column=j)
    if R.auto_score_scale:
        for j, col in enumerate(sheet.columns):
            if score_like(col) and not any(p["kind"] == "scale" and p["column"] == j for p in plan):
                letter = get_column_letter(j + 1)
                add("scale", "auto", f"{letter}{first_row}:{letter}{n_rows + 1}", scale=ST.SCORE_SCALE, column=j)
    # 4. banding, lowest
    if R.banded:
        add("band", "band", f"A{first_row}:{last_letter}{n_rows + 1}", formula="MOD(ROW(),2)=0", fill=R.tokens.band, ts=None)
    return plan


def _apply_cf(ws, plan: List[dict]) -> None:
    from openpyxl.formatting.rule import ColorScaleRule, FormulaRule

    body_base = None
    for p in plan:
        if p["scale"] is not None:
            lo, mid, hi = p["scale"]
            if mid:
                rule = ColorScaleRule(start_type="min", start_color=_hex6(lo), mid_type="percentile", mid_value=50, mid_color=_hex6(mid), end_type="max", end_color=_hex6(hi))
            else:
                rule = ColorScaleRule(start_type="min", start_color=_hex6(lo), end_type="max", end_color=_hex6(hi))
            ws.conditional_formatting.add(p["range"], rule)
            continue
        ts = p["style"]
        font = None
        if ts is not None:
            font = _cf_font(ts, body_base or ST.TextStyle(color=ST.INK))
        rule = FormulaRule(formula=[p["formula"]], fill=_cf_fill(p["fill"]), font=font, stopIfTrue=True if p["stop"] else None)
        ws.conditional_formatting.add(p["range"], rule)


def _write_sheet(ws, sheet: S.Sheet, warnings: List[str], R: Optional[ST.ResolvedStyle] = None) -> dict:
    """Header, rows, totals, widths, freeze, filter, conditional formats,
    page setup, charts. Returns the layout ({first_row, last_row,
    totals_row, column_letters}) the dashboard sheet points its KPI
    formulas at."""
    from openpyxl.styles import Alignment, Border, Side

    R = R or ST.resolve(None)
    cache = _StyleCache(R)
    n_cols = len(sheet.columns)
    n_rows = len(sheet.rows)
    legacy = sheet.style
    sst = sheet_style(sheet)
    highlight = highlight_for(sheet)
    t = R.tokens
    grid_side = Side(style="thin", color=_hex6(t.grid))
    grid = Border(left=grid_side, right=grid_side, top=grid_side, bottom=grid_side) if sst.borders == "thin" else Border()
    header_rule = Side(style="medium", color=_hex6(t.header_fill if t.header_fill.upper() != "#F3F4F6" else t.primary))
    body_size = R.sizes.body
    base_body = R.base("table_body")

    header_styles = []
    for j, col in enumerate(sheet.columns):
        ts = R.element("table_header", sheet=sheet.name, column=col.name, column_index=j)
        user_header = R.matching_rules("table_header", sheet=sheet.name)
        if legacy is not None and not any(r.style.background for r in user_header):
            if sst.header_fill == "light":
                ts = ST.TextStyle.model_construct(**{**ts.model_dump(), "background": LEGACY_LIGHT_HEADER[0], "color": LEGACY_LIGHT_HEADER[1]})
            elif sst.header_fill == "none":
                ts = ST.TextStyle.model_construct(**{**ts.model_dump(), "background": None, "color": t.ink})
        if legacy is not None and not sst.header_bold and not any(r.style.bold is not None for r in user_header):
            ts = ST.TextStyle.model_construct(**{**ts.model_dump(), "bold": False})
        header_styles.append(ts)
        cell = ws.cell(row=1, column=j + 1)
        _write_text(cell, col.name)
        colour = highlight.get(j)
        if colour and any(r.style.background for r in user_header):
            # AS3 integration (live 2026-09-15): a header colour the person
            # asked for beats the model's legacy column highlight — "a yellow
            # header row" came out with one navy header cell.
            colour = None
        size = ts.size_pt if ts.size_pt != R.base("table_header").size_pt else body_size
        if colour:
            from openpyxl.styles import Font, PatternFill

            h_fill, h_font, _, _ = HIGHLIGHT_COLOURS[colour]
            cell.font = Font(bold=bool(ts.bold), color=h_font, name=R.face(ts.font_family).office_name, size=size)
            cell.fill = PatternFill("solid", fgColor=h_fill)
        else:
            cell.font = cache.font(ts, size=size)
            fill = cache.fill(ts.background)
            if fill is not None:
                cell.fill = fill
        cell.alignment = Alignment(horizontal=ts.align or col.align or ("right" if col.type != "text" else "left"), vertical="center", wrap_text=True)
        if sst.borders == "thin":
            cell.border = Border(left=grid_side, right=grid_side, top=grid_side, bottom=header_rule)
        else:
            cell.border = Border(bottom=header_rule)
    ws.row_dimensions[1].height = 24

    scales = {j: (_percent_scale(sheet, j, warnings) if col.type == "percent" else 1.0) for j, col in enumerate(sheet.columns)}
    formats = {j: number_format_for(col, [r[j] for r in sheet.rows[:2000]]) for j, col in enumerate(sheet.columns)}
    # AS3 integration (live 2026-09-15): a column a requested condition
    # fills ("negative growth in red") must not ALSO carry the [Red] number
    # format — LibreOffice drew -13.33 red on the red fill, invisible.
    filled = {ST._fold(c.column) for c in R.conditional if c.style.background and (c.sheet is None or ST._fold(c.sheet) == ST._fold(sheet.name))}
    for j, col in enumerate(sheet.columns):
        if ST._fold(col.name) in filled and ";[Red]-" in formats[j]:
            formats[j] = formats[j].split(";[Red]-")[0] + ";-" + formats[j].split(";[Red]-")[1]
    long_text = {j for j, col in enumerate(sheet.columns) if col.type == "text" and any(isinstance(r[j], str) and len(r[j]) > 50 for r in sheet.rows[:500])}
    col_styles = [R.element("table_body", sheet=sheet.name, column=col.name, column_index=j) for j, col in enumerate(sheet.columns)]
    # Rows and ranges a rule names: only those cells get a per-cell style.
    row_rules = {r.target.index: r for r in R.rules if r.target.kind == "row" and r.target.index is not None and (r.target.sheet is None or ST._fold(r.target.sheet) == ST._fold(sheet.name))}
    range_rules = []
    for r in R.rules:
        if r.target.kind != "cell_range" or not r.target.a1 or (r.target.sheet is not None and ST._fold(r.target.sheet) != ST._fold(sheet.name)):
            continue
        clipped = ST.clip_a1(r.target.a1, n_cols, n_rows + 1)
        if clipped is None:
            continue
        c1, r1, c2, r2 = clipped
        if (c2 - c1 + 1) * (r2 - r1 + 1) > 10_000:
            continue  # a single CF rule carries it (sheet_cf_plan)
        range_rules.append((clipped, r.style))
    legacy_fills = {j: HIGHLIGHT_COLOURS[c] for j, c in highlight.items()}
    wrap_all = sst.wrap
    alignments: dict = {}
    for i, row in enumerate(sheet.rows):
        excel_row = i + 2
        row_rule = row_rules.get(excel_row)
        for j, value in enumerate(row):
            cell = ws.cell(row=excel_row, column=j + 1)
            _write_value(cell, value, sheet.columns[j].type, percent_scale=scales[j])
            if cell.data_type == "n" or isinstance(cell.value, (_dt.date, _dt.datetime)):
                cell.number_format = formats[j] if formats[j] != "@" else cell.number_format
            ts = col_styles[j]
            if row_rule is not None:
                ts = on_fill(row_rule.style, ts)
            for (c1, r1, c2, r2), rs in range_rules:
                if r1 <= excel_row <= r2 and c1 <= j + 1 <= c2:
                    ts = on_fill(rs, ts)
            size = ts.size_pt if ts.size_pt != base_body.size_pt else body_size
            if j in legacy_fills:
                from openpyxl.styles import Font, PatternFill

                _, _, c_fill, c_font = legacy_fills[j]
                cell.font = Font(color=c_font, name=R.face(ts.font_family).office_name, size=size)
                cell.fill = PatternFill("solid", fgColor=c_fill)
            else:
                cell.font = cache.font(ts, size=size)
                fill = cache.fill(ts.background)
                if fill is not None:
                    cell.fill = fill
            wrap = wrap_all or j in long_text
            key = (ts.align or sheet.columns[j].align or ("right" if sheet.columns[j].type != "text" else None), wrap)
            al = alignments.get(key)
            if al is None:
                # Top-aligned so a wrapped comment reads from the row's top
                # edge, next to the id it belongs to.
                al = alignments[key] = Alignment(horizontal=key[0], vertical="top", wrap_text=True if key[1] else None)
            cell.alignment = al
            cell.border = grid

    first_row, last_row = 2, n_rows + 1
    totals_row = None
    if sheet.totals and n_rows > 0:
        totals_row = last_row + 1
        tts = R.element("table_total", sheet=sheet.name)
        top_side = Side(style="thin", color=_hex6(tts.color or t.primary))
        total_font = cache.font(tts, size=tts.size_pt if tts.size_pt != R.base("table_total").size_pt else body_size)
        total_fill = cache.fill(tts.background)
        for j in range(n_cols):
            c = ws.cell(row=totals_row, column=j + 1)
            c.font = total_font
            if total_fill is not None:
                c.fill = total_fill
            c.border = Border(top=top_side, left=grid_side if sst.borders == "thin" else None, right=grid_side if sst.borders == "thin" else None,
                              bottom=grid_side if sst.borders == "thin" else None)
        label_written = False
        for total in sheet.totals:
            letter = _column_letter(total.column)
            cell = ws.cell(row=totals_row, column=total.column + 1)
            # The formula is written by THIS code from the row count; the spec
            # only named the column and the function. SUBTOTAL ignores rows
            # the autofilter hides, so the total follows the visible rows.
            cell.value = f"=SUBTOTAL({_SUBTOTAL[total.fn]},{letter}{first_row}:{letter}{last_row})"
            col = sheet.columns[total.column]
            cell.number_format = "#,##0" if total.fn == "count" else (formats[total.column] if formats[total.column] != "@" else "#,##0.00")
            cell.alignment = Alignment(horizontal=tts.align or "right")
            if not label_written and total.column > 0:
                label = ws.cell(row=totals_row, column=1)
                if label.value is None:
                    _write_text(label, total.label)
                    label.font = total_font
                    if tts.align:
                        label.alignment = Alignment(horizontal=tts.align)
                    label_written = True

    # Widths: the spec's width, else from content (style guide §4: min 8,
    # max 50; dates 12; currency at least 14), capped at 45 when the sheet
    # wraps so a long comment column is a readable paragraph.
    for j, col in enumerate(sheet.columns):
        if col.width:
            width = col.width
        else:
            longest = len(col.name)
            for row in sheet.rows[:500]:
                v = row[j]
                if v is not None:
                    longest = max(longest, len(str(v)))
            width = min(max(longest + 2, 8), 45 if (sst.wrap or j in long_text) else 50)
            if col.type == "date":
                width = max(12, min(width, 14))
            elif col.type == "currency":
                width = max(14, width)
        ws.column_dimensions[_column_letter(j)].width = width

    if sheet.freeze_header and R.freeze_header:
        ws.freeze_panes = "B2" if (n_cols > 8 and _ID_COLUMN_RE.match(sheet.columns[0].name)) else "A2"
    if sheet.autofilter and n_rows > 0:
        ws.auto_filter.ref = f"A1:{_column_letter(n_cols - 1)}{last_row}"

    _apply_cf(ws, sheet_cf_plan(sheet, R, first_row=first_row, last_row=last_row))
    _page_setup(ws, sheet, R)

    layout = {"first_row": first_row, "last_row": last_row, "totals_row": totals_row, "n_cols": n_cols}
    _draw_charts(ws, sheet, layout, n_cols, R)
    return layout


_PAPER = {"A4": 9, "Letter": 1, "Legal": 5, "A3": 8, "A5": 11}


def _page_setup(ws, sheet: S.Sheet, R: ST.ResolvedStyle) -> None:
    """Print setup (style guide §4): landscape past six columns (or as
    asked), one page wide, the header row repeated, 0.5 in margins, a
    'Page X of Y' footer; user header/footer text with '&' doubled."""
    from openpyxl.worksheet.page import PageMargins

    landscape = R.page.orientation == "landscape" if R.page.orientation_explicit else is_landscape(sheet)
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.page_setup.paperSize = _PAPER.get(R.page.size, 9)
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:1"
    margin = {"narrow": 0.25, "wide": 1.0}.get(R.page.margins, 0.5)
    ws.page_margins = PageMargins(left=margin, right=margin, top=0.5 + 0.25, bottom=0.5 + 0.25, header=0.3, footer=0.3)
    if R.page.page_numbers:
        ws.oddFooter.right.text = "Page &P of &N"
    if R.page.footer_text:
        ws.oddFooter.left.text = ST.xlsx_header_footer_text(R.page.footer_text)
    if R.page.header_text:
        ws.oddHeader.left.text = ST.xlsx_header_footer_text(R.page.header_text)
    ws.sheet_properties.tabColor = _hex6(R.tokens.primary)


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


# --- AS3 integration BEGIN: Chart v2 in the workbook ---
_LEGACY_XLSX_TYPES = ("bar", "horizontal_bar", "line", "pie")


def is_v2_native(chart: Any) -> bool:
    """A chart the charts track draws (render/chart_native.add_xlsx_chart):
    every type beyond the four legacy ones, and any chart with requested
    styling (colours, fonts, labels, axis bounds) the legacy writer ignores.
    Legacy charts keep the writer that references the sheet's own cells."""
    return str(getattr(chart, "type", "")) not in _LEGACY_XLSX_TYPES or getattr(chart, "style", None) is not None
# --- AS3 integration END ---


def _draw_charts(ws, sheet: S.Sheet, layout: dict, n_cols: int, R: Optional[ST.ResolvedStyle] = None) -> None:
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference

    anchor_row = 2
    anchor_col = n_cols + 2
    # Charts whose data is not in the sheet's columns get their numbers in a
    # block to the right, so the chart still references real cells.
    aux_col = n_cols + 12
    layout["chart_blocks"] = {}
    for k, chart in enumerate(sheet.charts):
        if is_v2_native(chart) and chart.series:
            # AS3 integration: the charts track's native writer (values on
            # the "Chart data" sheet; an image where Excel has no such chart).
            from . import chart_native as CN

            CN.add_xlsx_chart(ws, chart, f"{_column_letter(anchor_col - 1)}{anchor_row}", R)
            anchor_row += 18
            continue
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

    return Font(bold=True, name=theme.CLASSIC_BODY_FONT)


def _dashboard(wb, spec: S.WorkbookSpec, layouts: List[Tuple[S.Sheet, str, dict]]) -> None:
    """The first sheet: a title, one KPI cell per total (a live formula into
    the data sheet) and a copy of every chart."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import quote_sheetname

    ws = wb.create_sheet(title="Dashboard", index=0)
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = theme.CLASSIC_ACCENT.lstrip("#").upper()
    # The title and purpose are the model's text like any cell: through
    # _write_text, or a title of `=HYPERLINK(...)` is a live formula (found
    # in review 2026-09-11 — these two were the only bare assignments).
    _write_text(ws["B2"], spec.title)
    ws["B2"].font = Font(bold=True, size=18, color=theme.CLASSIC_PRIMARY.lstrip("#").upper(), name=theme.CLASSIC_HEADING_FONT)
    if spec.purpose:
        _write_text(ws["B3"], spec.purpose)
        ws["B3"].font = Font(color=theme.CLASSIC_MUTED.lstrip("#").upper(), name=theme.CLASSIC_BODY_FONT)
    row = 5
    col = 2
    fill = PatternFill("solid", fgColor=theme.CLASSIC_BAND.lstrip("#").upper())
    for sheet, title, layout in layouts:
        if not layout["totals_row"]:
            continue
        for total in sheet.totals:
            letter = _column_letter(total.column)
            label = ws.cell(row=row, column=col)
            _write_text(label, f"{sheet.columns[total.column].name} ({total.fn})")
            label.font = Font(size=9, color=theme.CLASSIC_MUTED.lstrip("#").upper(), name=theme.CLASSIC_BODY_FONT)
            label.fill = fill
            value = ws.cell(row=row + 1, column=col)
            # quote_sheetname doubles an apostrophe inside the name ("Q1's
            # data" → 'Q1''s data'), which is legal in a sheet name and which
            # a hand-built f"'{title}'" turned into Err:509 in LibreOffice.
            value.value = f"={quote_sheetname(title)}!{letter}{layout['totals_row']}"
            value.font = Font(bold=True, size=16, color=theme.CLASSIC_PRIMARY.lstrip("#").upper(), name=theme.CLASSIC_HEADING_FONT)
            value.fill = fill
            value.alignment = Alignment(horizontal="left")
            value.number_format = "#,##0" if total.fn == "count" else number_format_for(sheet.columns[total.column], [r[total.column] for r in sheet.rows[:2000]])
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
        if is_v2_native(chart):
            continue  # AS3 integration: drawn on its data sheet by chart_native
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


def render_xlsx(spec: S.WorkbookSpec, out_path: str | Path, *, warnings: Optional[List[str]] = None,
                resolved: Optional[ST.ResolvedStyle] = None) -> Path:
    """Write the workbook to `out_path` and return it. `resolved` is the
    ResolvedStyle render/__init__ shares across formats; without it the
    workbook's own style is resolved here."""
    from openpyxl import Workbook

    warnings = warnings if warnings is not None else []
    R = resolved or ST.resolve(spec)
    wb = Workbook()
    wb.remove(wb.active)
    layouts: List[Tuple[S.Sheet, str, dict]] = []
    for sheet, title in zip(spec.sheets[: T.MAX_SHEETS], sheet_titles(spec)):
        if len(sheet.rows) > T.MAX_ROWS_PER_SHEET:
            warnings.append(f"Sheet {sheet.name!r}: {len(sheet.rows):,} rows were cut to the {T.MAX_ROWS_PER_SHEET:,}-row ceiling.")
            sheet = sheet.model_copy(update={"rows": sheet.rows[: T.MAX_ROWS_PER_SHEET]})
        ws = wb.create_sheet(title=title)
        layouts.append((sheet, title, _write_sheet(ws, sheet, warnings, R)))
    if spec.template_id == "dashboard":
        _dashboard(wb, spec, layouts)
    sheet_notes = [(sh.name, sh.notes) for sh in spec.sheets[: T.MAX_SHEETS] if sh.notes]
    if spec.sources or spec.assumptions or sheet_notes:
        ws = wb.create_sheet(title="Notes" if "notes" not in {t.lower() for t in wb.sheetnames} else "Notes-2")
        ws.sheet_properties.tabColor = theme.CLASSIC_MUTED.lstrip("#").upper()
        r = 1
        if sheet_notes:
            ws.cell(row=r, column=1, value="Sheet notes").font = _bold()
            r += 1
            for name, text in sheet_notes:
                _write_text(ws.cell(row=r, column=1), name)
                _write_text(ws.cell(row=r, column=2), text)
                r += 1
            r += 1
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
    from . import chart_native as _CN  # AS3 integration: "Chart data" stays the last sheet

    _CN.keep_chart_data_last(wb)
    wb.save(str(out))
    return out


__all__ = [
    "render_xlsx", "is_formula_like", "safe_sheet_name", "sheet_titles", "sheet_style", "is_landscape",
    "highlight_for", "aggregate_chart", "HIGHLIGHT_COLOURS", "HEADER_FILLS", "BORDER_COLOUR", "CHART_TOP_CATEGORIES",
    "number_format_for", "sheet_cf_plan", "cond_formula", "status_formula", "status_like", "score_like", "due_like",
]
