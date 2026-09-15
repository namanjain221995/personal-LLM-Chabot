"""Aggregates in a workbook are computed by code, never typed by the model.

WHY (B1, AS3 fix round 2026-09-15). "plot total Amount by month as a line in
an excel sheet" over a 150-row upload produced a correct, code-computed line
chart next to a sheet the MODEL had typed: seven months, 63,668 in every
one. The chart track already refuses a chart whose numbers were typed; the
table beside it had no such guard, and the version only carried the
warning "figures not in the material". A file is read, not its warnings.

THE RULE. When the material has data tables, every figure on a sheet the
model typed must be either a copy of a source row or computed by code from
the source rows. `enforce` runs after the charts are resolved and, for each
typed sheet whose figures are not copies:

1. rows labelled Total / Grand total beside copied rows are removed and the
   sheet gets a real totals row (a formula the renderer writes);
2. the sheet's columns are read as a binding ("Month" over the one date
   column, "Total Amount" = sum of Amount, "Number of tickets" = count)
   and chart_data.compute makes the rows — every column the model named
   is kept;
3. otherwise a chart on the sheet bound to a material table (already
   computed by chart_data) over what the label column names (the same
   column, or its date bucket: "Month" over Date by month) becomes the
   table: its categories and series ARE the rows; or, for a two-column
   dashboard, each row's label is read as one figure of the table ("Total
   Amount", "Average Units", "Number of orders", "Peak Month Amount"), a
   row no column gives is left out;
4. otherwise the typed table is left out: a sheet with a bound chart keeps
   the chart and a one-line note, a sheet without one is dropped, and a
   workbook left with no sheet gets the source table copied as it is.

Each outcome is a note on the version. A rebuilt sheet carries
`computed_from` (the table id), so the figure check and the "typed rows"
clause of the sentence treat its numbers as code's. Deterministic, no
model call, synchronous and CPU-bound (call it from a worker thread).
"""
from __future__ import annotations

import datetime as _dt
import math
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import spec as S
from . import types as T

try:  # the charts track: the only code that aggregates rows
    from . import chart_data as CD
    from . import chart_spec as CS
except Exception:  # noqa: BLE001 — pragma: no cover, both ship together
    CD = None  # type: ignore[assignment]
    CS = None  # type: ignore[assignment]

#: Rows of a material table scanned to decide whether a typed row is a copy.
SCAN_ROWS = 200_000
#: Typed rows looked up at all; a model-typed sheet longer than this is not a
#: summary, and its unverified figures are treated as derived.
MAX_TYPED_ROWS = 2_000

_AGG_WORDS: Dict[str, Tuple[str, ...]] = {
    "sum": ("grand total", "total", "sum", "overall", "gross"),
    "avg": ("average", "avg", "mean"),
    "count": ("number of", "no of", "no.", "count", "num", "#"),
    "min": ("minimum", "min", "lowest", "smallest"),
    "max": ("maximum", "max", "highest", "largest", "peak"),
    "median": ("median",),
}
_BUCKET_WORDS: Dict[str, Tuple[str, ...]] = {
    "month": ("month", "monthly", "months"),
    "quarter": ("quarter", "quarterly", "qtr"),
    "year": ("year", "yearly", "annual", "years"),
    "week": ("week", "weekly", "weeks"),
    "day": ("day", "daily", "date", "days"),
}
_FILLER = ("of", "the", "in", "per", "by", "(", ")", ":")
_TOTAL_LABEL_RE = re.compile(r"^\s*(?:grand\s+|sub\s*-?\s*)?(?:total|sum|overall)s?\b", re.IGNORECASE)
_NUMERIC_TYPES = ("integer", "number", "currency", "percent")
_NON_AGG_TYPES = ("scatter", "bubble", "histogram", "box", "gantt")


def _fold(text: Any) -> str:
    return re.sub(r"[\s_\-]+", " ", str(text)).strip().casefold()


def _number(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool) or isinstance(v, (_dt.date, _dt.datetime)):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(float(v)) else None
    if isinstance(v, str) and re.search(r"\d", v) and CD is not None:
        return CD.to_number(v)
    return None


def _key(f: float) -> float:
    return round(f, 6)


def _label(v: Any) -> str:
    if isinstance(v, _dt.datetime):
        return v.date().isoformat() if v.time() == _dt.time() else v.isoformat(sep=" ")
    if isinstance(v, _dt.date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _table_attr(t: Any, key: str, default: Any = None) -> Any:
    return t.get(key, default) if isinstance(t, dict) else getattr(t, key, default)


# ------------------------------------------------------------ copies --


def _value_columns(sheet: S.Sheet) -> List[int]:
    """The columns whose numeric cells are figures: all but a leading label
    column (text or date) when the sheet has more than one column, and
    never a date column."""
    out: List[int] = []
    for j, c in enumerate(sheet.columns):
        if c.type == "date":
            continue
        if j == 0 and len(sheet.columns) > 1 and c.type == "text":
            continue
        out.append(j)
    return out


#: An aggregate word anywhere in a row's text ("Total Amount: 1,047,638").
_AGG_TEXT_RE = re.compile(
    r"(?<![\w])(?:" + "|".join(sorted({re.escape(w) for ws in _AGG_WORDS.values() for w in ws if w[-1].isalnum()}, key=len, reverse=True)) + r")(?![\w])",
    re.IGNORECASE,
)


def _row_shape(sheet: S.Sheet, row: Sequence[Any], value_cols: Sequence[int]) -> Tuple[Set[float], Set[str], bool]:
    """(figures, text cells, whether a text cell holds a typed aggregate).

    Verifier 2026-09-15: two ways a typed aggregate used to pass as "no
    figure here" — a number in a column the model declared `date`, and a
    figure inside a text cell beside an aggregate word ("Total Amount:
    1,047,638" on a one-column KPI sheet). The first is a figure; the second
    makes the row need a source row holding that exact text."""
    nums: Set[float] = set()
    texts: Set[str] = set()
    text_figure = False
    for j, cell in enumerate(row):
        if cell is None or (isinstance(cell, str) and not cell.strip()):
            continue
        is_date_col = j < len(sheet.columns) and sheet.columns[j].type == "date"
        n = _number(cell) if j in value_cols or (is_date_col and isinstance(cell, (int, float))) else None
        if n is not None:
            nums.add(_key(n))
        else:
            texts.add(_fold(_label(cell)))
            if isinstance(cell, str) and not text_figure and _AGG_TEXT_RE.search(cell):
                text_figure = any(S._qualifies(m) for m in S._FIGURE_RE.finditer(cell))
    return nums, texts, text_figure


def copied_rows(sheet: S.Sheet, tables: Sequence[Any]) -> Tuple[Set[int], List[int]]:
    """(indices of rows that copy a material row, indices of rows with a
    figure). A typed row is a copy when ONE row of one material table holds
    all its figures and all its text cells."""
    value_cols = _value_columns(sheet)
    shapes: Dict[int, Tuple[Set[float], Set[str]]] = {}
    for i, row in enumerate(sheet.rows):
        nums, texts, text_figure = _row_shape(sheet, row, value_cols)
        if nums or text_figure:
            shapes[i] = (nums, texts)
    figured = sorted(shapes)
    if not shapes or len(sheet.rows) > MAX_TYPED_ROWS:
        return set(), figured
    needed: Set[float] = set().union(*(n for n, _ in shapes.values()))
    text_only = any(not n for n, _ in shapes.values())
    pending = dict(shapes)
    copied: Set[int] = set()
    for table in tables:
        for row in (_table_attr(table, "rows", []) or [])[:SCAN_ROWS]:
            if not pending:
                return copied, figured
            nums: Set[float] = set()
            for cell in row:
                n = _number(cell)
                if n is not None:
                    nums.add(_key(n))
            if not nums & needed and not text_only:
                continue
            texts = {_fold(_label(c)) for c in row if c is not None and not (isinstance(c, str) and not c.strip())}
            for i, (n, t) in list(pending.items()):
                if n <= nums and t <= texts:
                    copied.add(i)
                    del pending[i]
            text_only = any(not n for n, _ in pending.values())
    return copied, figured


# ------------------------------------------------------------ naming --


def _aggs_named(name: str) -> Tuple[Set[str], str]:
    """The aggregate words in a column name and what is left of it:
    "Total Amount" → ({"sum"}, "amount")."""
    rest = f" {_fold(name)} "
    found: Set[str] = set()
    for agg, words in _AGG_WORDS.items():
        for w in words:
            pattern = rf"(?<![\w]){re.escape(w)}(?![\w])" if w[-1].isalnum() else rf"(?<![\w]){re.escape(w)}"
            if re.search(pattern, rest):
                found.add(agg)
                rest = re.sub(pattern, " ", rest)
    words = [w for w in rest.split() if w not in _FILLER]
    return found, " ".join(words)


def _same_measure(rest: str, column: str) -> bool:
    col = _fold(column)
    if not rest:
        return False
    return rest == col or rest.rstrip("s") == col.rstrip("s")


def _value_name(model_name: Optional[str], series_name: str, agg: str, grouped: bool) -> str:
    """The model's header when it says what the computed column holds
    ("Total Amount" over a sum of Amount), else the computed name ("Average
    of Amount")."""
    if model_name:
        found, rest = _aggs_named(model_name)
        if grouped:
            if not found or agg in found:
                return model_name
        elif found and agg in found and len(found) == 1 and (not rest or _same_measure(rest, series_name) or agg == "count"):
            return model_name
        elif not found and agg in ("sum", "none") and _same_measure(_fold(model_name), series_name):
            return model_name
    if grouped or agg == "none" or (agg == "count" and _fold(series_name).startswith("count")):
        return series_name[:80]
    label = CS.AGG_LABELS.get(agg, "Value") if CS is not None else agg.title()
    return f"{label} of {series_name}"[:80]


# ------------------------------------------------------------ rebuild --


def _columns_rows_from(chart: Any, sheet: S.Sheet, agg: str) -> Tuple[List[dict], List[list]]:
    grouped = bool(chart.data is not None and chart.data.group_by)
    model_values = [c.name for c in sheet.columns[1:]]
    names_fit = len(model_values) == len(chart.series)
    first = sheet.columns[0]
    label_name = first.name if first.type in ("text", "date") else (chart.x_label or (chart.data.x if chart.data else "") or "Category")
    columns: List[dict] = [{"name": label_name[:80], "type": "text", **({"width": first.width} if first.width else {})}]
    used = {_fold(label_name)}
    for k, s in enumerate(chart.series):
        model_name = model_values[k] if names_fit else None
        name = _value_name(model_name, s.name, agg, grouped)
        base, n = name, 2
        while _fold(name) in used:
            name = f"{base} {n}"[:80]
            n += 1
        used.add(_fold(name))
        typ = sheet.columns[k + 1].type if names_fit and sheet.columns[k + 1].type in _NUMERIC_TYPES else "number"
        if agg == "count":
            typ = "integer"
        columns.append({"name": name, "type": typ})
    rows = [[cat, *[s.values[i] for s in chart.series]] for i, cat in enumerate(chart.categories)]
    return columns, rows


def _chart_usable(chart: Any, ids: Set[str]) -> bool:
    if CS is None or not isinstance(chart, CS.Chart) or chart.data is None or not chart.series or not chart.categories:
        return False
    if chart.type in _NON_AGG_TYPES or chart.provenance is None:
        return False
    if chart.provenance.table_provenance == "sheet" or chart.provenance.table_id not in ids:
        return False
    return all(len(s.values) == len(chart.categories) and s.x is None for s in chart.series)


def _relates(chart: Any, label: str) -> bool:
    """The chart's categories are what the model's label column names:
    the same column, or its date bucket ("Month" over Date by month)."""
    want = _fold(label)
    if chart.data is None:
        return False
    if want in (_fold(chart.data.x or ""), _fold(chart.x_label or "")):
        return True
    bucket = (chart.provenance.date_bucket if chart.provenance is not None else "") or chart.data.date_bucket or ""
    return bool(bucket) and bool(set(want.split()) & set(_BUCKET_WORDS.get(bucket, ())))


Built = Tuple[List[dict], List[list], Any, str, List[str], Dict[int, str]]


def _with_resolved_charts(sheet: S.Sheet, tables: Sequence[Any]) -> S.Sheet:
    """A chart bound to a material table but not computed yet (a revision's
    draft) is computed first, so it can become the table or be kept."""
    if CS is None or not any(isinstance(c, CS.Chart) and c.data is not None and c.data.table_id and not c.series for c in sheet.charts):
        return sheet
    charts: List[Any] = []
    for chart in sheet.charts:
        if isinstance(chart, CS.Chart) and chart.data is not None and chart.data.table_id and not chart.series:
            resolved, _notes, _msg = CD.resolve_chart(chart, tables)
            chart = resolved if resolved is not None else chart
        charts.append(chart)
    return sheet.model_copy(update={"charts": charts})


def _cell(v: Any) -> Any:
    if v is None or isinstance(v, str) or (isinstance(v, (int, float)) and not isinstance(v, bool)):
        return v
    return _label(v)


def _from_chart(sheet: S.Sheet, tables: Sequence[Any]) -> Optional[Built]:
    ids = {str(_table_attr(t, "id", "")) for t in tables}
    for chart in sheet.charts:
        if not _chart_usable(chart, ids) or not _relates(chart, sheet.columns[0].name):
            continue
        table = next(t for t in tables if str(_table_attr(t, "id", "")) == chart.provenance.table_id)
        agg = chart.provenance.agg or chart.data.agg
        columns, rows = _columns_rows_from(chart, sheet, agg)
        how = _describe(agg, [s.name for s in chart.series] if not chart.data.group_by else list(chart.data.y), chart.data.x, chart.provenance.date_bucket or chart.data.date_bucket, chart.data.group_by)
        return columns, rows, table, how, [], {j: agg for j in range(1, len(columns))}
    return None


def _describe(agg: str, ys: Sequence[str], x: Optional[str], bucket: Optional[str], group_by: Optional[str] = None) -> str:
    label = (CS.AGG_LABELS.get(agg, agg) if CS is not None else agg).lower()
    what = "row count" if agg == "count" and not ys else f"{label} of {', '.join(ys[:3])}"
    by = f"{x} ({bucket})" if bucket and x else (x or "")
    if group_by:
        by = f"{by} and {group_by}"
    return f"{what} by {by}" if by else what


def _infer_x(label: str, table: Any) -> Optional[Tuple[str, Optional[str], bool]]:
    """(x column, date bucket, x is a date) for a model's label column, or
    None when the name does not point at one column of `table`."""
    columns = [str(c) for c in (_table_attr(table, "columns", []) or [])]
    folded = [_fold(c) for c in columns]
    want = _fold(label)
    if want in folded:
        j = folded.index(want)
        info = CD.infer_column(table, j)
        return columns[j], None, info.kind == "date"
    words = set(want.split())
    for bucket, names in _BUCKET_WORDS.items():
        if words & set(names):
            dates = [j for j in range(len(columns)) if CD.infer_column(table, j).kind == "date"]
            if len(dates) == 1:
                return columns[dates[0]], bucket, True
            return None
    return None


def _infer_value(name: str, table: Any) -> Optional[Tuple[Optional[str], str]]:
    """(y column or None for a row count, agg) for a model's value column."""
    columns = [str(c) for c in (_table_attr(table, "columns", []) or [])]
    found, rest = _aggs_named(name)
    if len(found) > 1:
        return None
    for j, c in enumerate(columns):
        if _same_measure(rest, c) and CD.infer_column(table, j).kind == "number":
            return c, (next(iter(found)) if found else "sum")
    if found == {"count"}:
        return None, "count"
    return None


def _from_binding(sheet: S.Sheet, tables: Sequence[Any]) -> Optional[Built]:
    if len(sheet.columns) < 2 or CS is None:
        return None
    for table in tables:
        x = _infer_x(sheet.columns[0].name, table)
        if x is None:
            continue
        xcol, bucket, is_date = x
        values = [_infer_value(c.name, table) for c in sheet.columns[1:]]
        if any(v is None for v in values):
            continue
        computed: List[Any] = []
        notes: List[str] = []
        try:
            for y, agg in values:  # type: ignore[misc]
                chart = CS.Chart.model_validate({"type": "bar", "title": sheet.name[:120], "data": {
                    "table_id": str(_table_attr(table, "id", "")), "x": xcol, "y": [y] if y else [], "agg": agg,
                    "date_bucket": bucket, "sort": "x" if is_date else "none"}})
                result = CD.compute(chart, table)
                notes.extend(result.notes)
                computed.append((y, agg, result))
        except Exception:  # noqa: BLE001 — ChartDataError or a malformed binding: the next table, then give up
            continue
        cats = computed[0][2].categories
        if any(r.categories != cats or len(r.series) != 1 for _, _, r in computed):
            continue
        first = sheet.columns[0]
        columns: List[dict] = [{"name": first.name, "type": "text", **({"width": first.width} if first.width else {})}]
        for c, (y, agg, r) in zip(sheet.columns[1:], computed):
            typ = "integer" if agg == "count" else (c.type if c.type in _NUMERIC_TYPES else "number")
            columns.append({"name": _value_name(c.name, y or r.series[0].name, agg, False), "type": typ})
        seen: Set[str] = set()
        for col in columns:
            base, n = col["name"], 2
            while _fold(col["name"]) in seen:
                col["name"] = f"{base} {n}"[:80]
                n += 1
            seen.add(_fold(col["name"]))
        rows = [[cat, *[r.series[0].values[i] for _, _, r in computed]] for i, cat in enumerate(cats)]
        ys = [y for y, _, _ in computed if y]
        aggs = {agg for _, agg, _ in computed}
        how = _describe(next(iter(aggs)) if len(aggs) == 1 else "value", ys, xcol, bucket)
        return columns, rows, table, how, list(dict.fromkeys(notes)), {j + 1: agg for j, (_, agg, _) in enumerate(computed)}
    return None


_ROWS_WORDS = ("rows", "records", "entries", "transactions", "orders", "items", "sales", "lines", "tickets", "employees", "projects")


def _agg_of(values: Sequence[float], agg: str) -> float:
    import statistics

    if agg == "avg":
        return math.fsum(values) / len(values)
    return float({"sum": math.fsum, "min": min, "max": max, "median": statistics.median}[agg](values))


def _per_dimension(words: List[str], table: Any, columns: List[str]) -> Optional[Tuple[str, str, Optional[str]]]:
    """("amount month" or "month amount") → (y column, x column, date
    bucket) when one part of the words names a numeric column and the rest
    a date bucket (over the table's one date column) or another column."""
    numeric = {c for j, c in enumerate(columns) if CD.infer_column(table, j).kind == "number"}
    splits = [(" ".join(words[k:]), " ".join(words[:k])) for k in range(len(words) - 1, 0, -1)]
    splits += [(" ".join(words[:k]), " ".join(words[k:])) for k in range(1, len(words))]
    for dim, measure in splits:
        y = next((c for c in columns if c in numeric and _same_measure(measure, c)), None)
        if y is None:
            continue
        bucket = next((b for b, names in _BUCKET_WORDS.items() if dim in names), None)
        if bucket is not None:
            dates = [j for j in range(len(columns)) if CD.infer_column(table, j).kind == "date"]
            if len(dates) == 1:
                return y, columns[dates[0]], bucket
            continue
        x = next((c for c in columns if c != y and _same_measure(dim, c)), None)
        if x is not None:
            return y, x, None
    return None


_MONTH_NUMBERS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_UNIT_WORDS = {"inr", "rs", "usd", "eur", "gbp", "units", "pcs", "nos", "count", "k", "lakh", "lakhs", "cr", "crore", "mn", "m"}
_TRAILING_VERBS = {"sold", "made", "earned", "generated", "recorded", "booked", "shipped", "value", "figure", "overall", "all", "time"}


def _date_span(table: Any) -> Optional[Tuple[_dt.date, _dt.date]]:
    columns = list(_table_attr(table, "columns", []) or [])
    infos = [CD.infer_column(table, j) for j in range(len(columns))]
    dates = [i for i in infos if i.kind == "date"]
    if len(dates) != 1:
        return None
    j, order = dates[0].index, dates[0].date_order or "dmy"
    got = [d for d in (CD.to_date(r[j] if j < len(r) else None, order) for r in (_table_attr(table, "rows", []) or [])[:SCAN_ROWS]) if d is not None]
    return (min(got), max(got)) if got else None


def _without_qualifier(label: str, table: Any) -> Optional[str]:
    """The label without a parenthesis that adds nothing the table does not
    already cover: a unit ("(INR)", "(%)") or the table's own period
    ("(Jan-Jul 2026)" over rows from January to July 2026). None when a
    parenthesis narrows the figure (a region, another period): that is a
    filter this reading does not apply."""
    m = re.search(r"\(([^)]*)\)", label)
    if m is None:
        return label
    inner = _fold(m.group(1))
    tokens = re.findall(r"[^\W\d_]+|\d+", inner)
    rest = (label[: m.start()] + " " + label[m.end():]).strip()
    if not tokens or all(t in _UNIT_WORDS for t in tokens):
        return rest
    months = [_MONTH_NUMBERS.get(t[:3]) for t in tokens if not t.isdigit()]
    years = [int(t) for t in tokens if t.isdigit()]
    if None in months or any(y < 1900 or y > 2200 for y in years) or not (months or years):
        return None
    span = _date_span(table)
    if span is None:
        return None
    lo, hi = span
    if years and not set(years) <= {lo.year, hi.year}:
        return None
    if years and not months and not (lo.year == hi.year == years[0] and len(set(years)) == 1):
        return None
    if months and (months[0] != lo.month or months[-1] != hi.month):
        return None
    return rest


def _headline(label: str, table: Any) -> Optional[Tuple[float, str]]:
    """A dashboard row's label read as one figure of the table:
    "Total Amount" → (sum of Amount, "sum of Amount"), "Average Units",
    "Number of orders" → (row count, "row count"), "Number of regions" →
    (distinct Region values, "distinct Region"), "Average Amount per Month"
    → (the mean of the monthly sums of Amount, ...). None when the label
    does not name exactly one aggregate of one column."""
    columns = [str(c) for c in (_table_attr(table, "columns", []) or [])]
    rows = _table_attr(table, "rows", []) or []
    plain = _without_qualifier(label, table)
    if plain is None:
        return None
    found, rest = _aggs_named(plain)
    if len(found) > 1:
        return None
    words = rest.split()
    while len(words) > 1 and words[-1] in _TRAILING_VERBS:
        words = words[:-1]
    rest = " ".join(words)
    agg = next(iter(found)) if found else "sum"
    def label_of(a: str) -> str:
        return (CS.AGG_LABELS.get(a, a) if CS is not None else a).lower()

    for j, c in enumerate(columns):
        if not _same_measure(rest, c):
            continue
        cells = [r[j] if j < len(r) else None for r in rows]
        if agg == "count":
            present = {_fold(_label(v)) for v in cells if v is not None and not (isinstance(v, str) and not v.strip())}
            return float(len(present)), f"distinct {c}"
        if CD.infer_column(table, j).kind != "number":
            return None
        nums = [n for n in (CD.to_number(v) for v in cells) if n is not None]
        return (_agg_of(nums, agg), f"{label_of(agg)} of {c}") if nums else None
    if agg == "count" and (not rest or rest in _ROWS_WORDS or rest.rstrip("s") + "s" in _ROWS_WORDS):
        return float(len(rows)), "row count"
    if agg in ("avg", "min", "max", "median") and CS is not None:
        per = _per_dimension(rest.split(), table, columns)
        if per is None:
            return None
        y, x, bucket = per
        try:
            chart = CS.Chart.model_validate({"type": "bar", "title": label[:120], "data": {
                "table_id": str(_table_attr(table, "id", "")), "x": x, "y": [y], "agg": "sum", "date_bucket": bucket,
                "sort": "x", "other_bucket": False}})
            result = CD.compute(chart, table)
        except Exception:  # noqa: BLE001 — ChartDataError: not computable
            return None
        if any("too many" in n for n in result.notes) or not result.series or not result.series[0].values:
            return None
        per_word = bucket or x
        return _agg_of(result.series[0].values, agg), f"{label_of(agg)} of the {per_word} sums of {y}"
    return None


def _from_headlines(sheet: S.Sheet, tables: Sequence[Any]) -> Optional[Built]:
    """A two-column dashboard ("Metric | Value"): every row whose label names
    an aggregate of one column of ONE table gets the value computed from
    it; a row whose label names none is left out (with a note); a row with
    no figure stays. None when no row could be computed."""
    if len(sheet.columns) != 2 or sheet.columns[0].type != "text" or not sheet.rows:
        return None
    best: Optional[Tuple[int, Any, List[list], List[str], List[str]]] = None
    for table in tables:
        rows: List[list] = []
        hows: List[str] = []
        left: List[str] = []
        computed = 0
        for r in sheet.rows:
            if _number(r[1]) is None:
                if isinstance(r[0], str) and _aggs_named(r[0])[0] and r[1] not in (None, ""):
                    # "Peak Month | June": a figure of the data in words.
                    left.append(r[0])
                else:
                    rows.append(list(r))
                continue
            got = _headline(r[0], table) if isinstance(r[0], str) and r[0].strip() else None
            if got is None:
                left.append(str(r[0] or "a row"))
                continue
            rows.append([r[0], got[0]])
            hows.append(got[1])
            computed += 1
        if computed and (best is None or computed > best[0]):
            best = (computed, table, rows, hows, left)
    if best is None:
        return None
    _, table, rows, hows, left = best
    value = sheet.columns[1]
    columns = [{"name": sheet.columns[0].name, "type": "text", **({"width": sheet.columns[0].width} if sheet.columns[0].width else {})},
               {"name": value.name, "type": value.type if value.type in _NUMERIC_TYPES else "number"}]
    notes = [f"{', '.join(repr(x) for x in left[:4])} {'were' if len(left) != 1 else 'was'} left out: no column of the data gives {'them' if len(left) != 1 else 'it'}"] if left else []
    return columns, rows, table, "; ".join(dict.fromkeys(hows))[:200], notes, {1: "headline"}


def _carry(sheet: S.Sheet, columns: List[dict], rows: List[list], table_id: str, agg_by_col: Dict[int, str], notes: List[str]) -> S.Sheet:
    """The rebuilt sheet: the model's name, notes, style and totals where
    they still name a column; charts over the sheet's own rows re-resolved
    over the computed rows."""
    old_names = [c.name for c in sheet.columns]
    new_folded = [_fold(c["name"]) for c in columns]
    totals: List[dict] = []
    for t in sheet.totals:
        idx = t.column if isinstance(t.column, int) else None
        if idx is None or idx >= len(old_names):
            continue
        name = old_names[idx]
        j = new_folded.index(_fold(name)) if _fold(name) in new_folded else (idx if len(old_names) == len(columns) else None)
        if j is None:
            notes.append(f"sheet {sheet.name!r}: the {t.fn} of {name!r} was left out; the computed table has no such column")
            continue
        if t.fn == "sum" and agg_by_col.get(j) in ("avg", "min", "max", "median", "headline"):
            word = (CS.AGG_LABELS.get(agg_by_col[j], agg_by_col[j]) if CS is not None else agg_by_col[j]).lower()
            notes.append(f"sheet {sheet.name!r}: a sum of {columns[j]['name']!r} would add up {word} values, so that total was left out")
            continue
        if columns[j]["type"] == "text" and t.fn != "count":
            continue
        totals.append({"column": j, "fn": t.fn, "label": t.label})
    style = sheet.style.model_dump() if sheet.style is not None else None
    if style is not None:
        style["highlight"] = [h for h in style.get("highlight") or [] if _fold(h["column"]) in new_folded]
    data = {
        "name": sheet.name, "columns": columns, "rows": rows, "totals": totals,
        "freeze_header": sheet.freeze_header, "autofilter": sheet.autofilter, "notes": sheet.notes,
        "style": style, "computed_from": table_id, "charts": [],
    }
    rebuilt = S.Sheet.model_validate(data)
    own = CD._sheet_table(rebuilt.model_dump(mode="python"))
    charts: List[Any] = []
    for chart in sheet.charts:
        if CS is None or not isinstance(chart, CS.Chart) or chart.data is None:
            notes.append(f"sheet {sheet.name!r}: the chart {getattr(chart, 'title', '') or ''!r} drew the model's typed figures, so it was left out")
            continue
        tid = chart.data.table_id
        over_own = (not tid or tid.startswith("sheet:") or _fold(tid) == _fold(sheet.name)
                    or (chart.provenance is not None and chart.provenance.table_provenance == "sheet"))
        if not over_own:
            # Bound to a material table: its numbers are chart_data's.
            charts.append(chart)
            continue
        fresh, _more, msg = CD.resolve_chart(chart.model_copy(update={"categories": [], "series": [], "provenance": None, "extra": None}), [own], default_table=own)
        if fresh is not None:
            charts.append(fresh)
        else:
            notes.append(f"sheet {sheet.name!r}: {msg or 'a chart over the typed rows could not be drawn from the computed ones'}")
    rebuilt.charts = charts
    return rebuilt


def _chart_only(sheet: S.Sheet, tables: Sequence[Any]) -> Optional[S.Sheet]:
    ids = {str(_table_attr(t, "id", "")) for t in tables}
    kept = [c for c in sheet.charts if CS is not None and isinstance(c, CS.Chart) and c.provenance is not None
            and c.provenance.table_provenance != "sheet" and c.provenance.table_id in ids and c.series]
    if not kept:
        return None
    title = next((str(_table_attr(t, "title", "") or _table_attr(t, "id", "")) for t in tables if str(_table_attr(t, "id", "")) == kept[0].provenance.table_id), "the data")
    return S.Sheet.model_validate({
        "name": sheet.name, "columns": [{"name": "Note", "type": "text", "width": 60}],
        "rows": [[f"The figures are in the chart, computed from {title}."[:500]]],
        "charts": [c.model_dump() for c in kept], "computed_from": kept[0].provenance.table_id,
        "style": sheet.style.model_dump() if sheet.style is not None else None,
    })


def _copy_of(table: Any, notes: List[str]) -> S.Sheet:
    columns = [str(c) for c in (_table_attr(table, "columns", []) or [])][: T.MAX_COLUMNS_PER_SHEET]
    rows = [[_cell(c) for c in list(r)[: len(columns)]] + [None] * (len(columns) - len(r)) for r in (_table_attr(table, "rows", []) or [])[: T.MAX_ROWS_PER_SHEET]]
    title = str(_table_attr(table, "title", "") or _table_attr(table, "id", "") or "Data")
    return S.Sheet.model_validate({
        "name": re.sub(r"\.[A-Za-z0-9]{2,5}$", "", title)[:31] or "Data", "columns": [{"name": c[:80] or f"Column {k + 1}"} for k, c in enumerate(columns)],
        "rows": rows, "rows_from": str(_table_attr(table, "id", "")),
    })


def _edit_fallback(sheet: S.Sheet, parent_sheets: Sequence[S.Sheet], real: Sequence[Any], kept_rows: Sequence[Any], notes: List[str]) -> Optional[S.Sheet]:
    """On an edit, a sheet whose typed figures cannot be computed: the
    columns this edit added are left out when what remains copies the data
    or the previous version; otherwise the previous version's sheet is kept
    as it was. None when the version being edited has no such sheet."""
    twin = next((p for p in parent_sheets if _fold(p.name) == _fold(sheet.name)), None)
    if twin is None:
        return None
    old = {_fold(c.name) for c in twin.columns}
    keep = [j for j, c in enumerate(sheet.columns) if _fold(c.name) in old]
    added = [c.name for j, c in enumerate(sheet.columns) if j not in keep]
    if added and keep:
        try:
            data = sheet.model_dump()
            data["columns"] = [data["columns"][j] for j in keep]
            data["rows"] = [[r[j] for j in keep] for r in sheet.rows]
            remap = {j: k for k, j in enumerate(keep)}
            data["totals"] = [{**t, "column": remap[t["column"]]} for t in data.get("totals") or []
                              if isinstance(t.get("column"), int) and t["column"] in remap]
            data["computed_from"] = None
            trimmed = S.Sheet.model_validate(data)
        except Exception:  # noqa: BLE001 — the previous version's sheet below
            trimmed = None
        if trimmed is not None:
            copied, figured = copied_rows(trimmed, [*real, *kept_rows])
            if all(i in copied for i in figured):
                if twin.computed_from and trimmed.rows == twin.rows:
                    trimmed = trimmed.model_copy(update={"computed_from": twin.computed_from})
                names = ", ".join(repr(n) for n in added[:3])
                notes.append(f"sheet {sheet.name!r}: the {names} column{'s' if len(added) > 1 else ''} this change added "
                             f"{'were' if len(added) > 1 else 'was'} left out: the figures were typed, not computed from the data")
                return trimmed
    notes.append(f"sheet {sheet.name!r} was kept as it was: the figures this change typed were not computed from the data")
    return twin


def _same_sheet(a: S.Sheet, b: S.Sheet) -> bool:
    return _fold(a.name) == _fold(b.name) and [c.name for c in a.columns] == [c.name for c in b.columns] and a.rows == b.rows


def enforce(spec: Any, tables: Sequence[Any], *, parent: Any = None) -> Tuple[Any, List[str], Dict[str, Any]]:
    """(spec, notes, report). See the module docstring. `report` has
    `computed` (sheet names rebuilt from the data), `dropped` (sheet names
    left out), `totals` (sheets whose typed total rows became formulas) and
    `copied_source` (the table copied when nothing else was left). The
    input spec is not mutated; the same object comes back when nothing
    changed."""
    report: Dict[str, Any] = {"computed": [], "dropped": [], "totals": [], "copied_source": ""}
    body = getattr(spec, "body", None)
    real = [t for t in (tables or []) if not str(_table_attr(t, "id", "")).startswith("sheet:") and (_table_attr(t, "rows", None) or [])]
    if CD is None or not isinstance(body, S.WorkbookSpec) or not real:
        return spec, [], report
    parent_body = getattr(parent, "body", None)
    parent_sheets = list(parent_body.sheets) if isinstance(parent_body, S.WorkbookSpec) else []
    notes: List[str] = []
    out: List[S.Sheet] = []
    changed = False
    for sheet in body.sheets:
        if sheet.rows_are_code_made or not sheet.rows:
            out.append(sheet)
            continue
        twin = next((p for p in parent_sheets if _same_sheet(p, sheet)), None)
        if twin is not None:
            # An edit that kept this sheet as it was: not typed in this turn.
            if twin.computed_from and not sheet.computed_from:
                sheet = sheet.model_copy(update={"computed_from": twin.computed_from})
                changed = True
            out.append(sheet)
            continue
        if sheet.computed_from:
            # Only code writes the marker, and only on a sheet it rebuilt;
            # a sheet that arrives with it but differs is checked again.
            sheet = sheet.model_copy(update={"computed_from": None})
            changed = True
        # On an edit, a row the version being edited already had is not
        # typed in this turn (the model echoed it): it counts as a copy.
        kept_rows = [{"id": "parent", "rows": p.rows} for p in parent_sheets if _fold(p.name) == _fold(sheet.name)]
        copied, figured = copied_rows(sheet, [*real, *kept_rows])
        derived = [i for i in figured if i not in copied]
        if not derived:
            out.append(sheet)
            continue
        changed = True
        if copied and all(isinstance(sheet.rows[i][0], str) and _TOTAL_LABEL_RE.match(sheet.rows[i][0]) for i in derived):
            out.append(_totals_as_formulas(sheet, derived, notes))
            report["totals"].append(sheet.name)
            continue
        sheet = _with_resolved_charts(sheet, real)
        built = _from_binding(sheet, real) or _from_chart(sheet, real) or _from_headlines(sheet, real)
        if built is not None:
            columns, rows, table, how, more, agg_by_col = built
            try:
                rebuilt = _carry(sheet, columns, rows, str(_table_attr(table, "id", "")), agg_by_col, notes)
            except Exception:  # noqa: BLE001 — a rebuilt sheet the schema refuses is left out below
                rebuilt = None
            if rebuilt is not None:
                title = str(_table_attr(table, "title", "") or _table_attr(table, "id", ""))
                notes.append(f"the {sheet.name} table was computed from {title} ({how}); the figures the model had typed were not used")
                notes.extend(f"sheet {sheet.name!r}: {m}" for m in more)
                out.append(rebuilt)
                report["computed"].append(sheet.name)
                continue
        kept = _edit_fallback(sheet, parent_sheets, real, kept_rows, notes)
        if kept is not None:
            # An EDIT of a sheet the version already had: the typed figures
            # are left out, never the sheet (verifier 2026-09-15: "add a
            # share % column" to a computed monthly sheet dropped the sheet).
            out.append(kept)
            continue
        chart_only = _chart_only(sheet, real)
        if chart_only is not None:
            notes.append(f"the {sheet.name} table was left out: its figures were typed, not computed from the data; the chart beside it was computed from the data and is kept")
            out.append(chart_only)
            report["dropped"].append(sheet.name)
            continue
        notes.append(f"the {sheet.name} sheet was left out: its figures were typed, not computed from the data")
        report["dropped"].append(sheet.name)
    if not changed:
        return spec, [], report
    if not out:
        table = real[0]
        out.append(_copy_of(table, notes))
        report["copied_source"] = str(_table_attr(table, "id", ""))
        notes.append(f"the workbook has the source table {str(_table_attr(table, 'title', '') or report['copied_source'])} as it is, since no summary could be computed from it")
    new_body = body.model_copy(update={"sheets": out})
    return spec.model_copy(update={spec.kind: new_body}), notes, report


def _totals_as_formulas(sheet: S.Sheet, derived: Sequence[int], notes: List[str]) -> S.Sheet:
    """Typed Total rows under copied rows → removed, and a totals row the
    renderer writes as =SUM over the copied cells."""
    drop = set(derived)
    value_cols = [j for j in _value_columns(sheet) if sheet.columns[j].type in _NUMERIC_TYPES]
    had = {t.column for t in sheet.totals if isinstance(t.column, int)}
    totals = [t.model_dump() for t in sheet.totals]
    for j in value_cols:
        if j not in had and any(_number(sheet.rows[i][j]) is not None for i in derived):
            totals.append({"column": j, "fn": "sum", "label": "Total"})
    data = sheet.model_dump()
    data["rows"] = [r for i, r in enumerate(sheet.rows) if i not in drop]
    data["totals"] = totals[: T.MAX_COLUMNS_PER_SHEET]
    added = len(totals) > len(sheet.totals)
    notes.append(f"sheet {sheet.name!r}: the total row the model typed was " + ("replaced by a totals row the file computes" if added or had else "left out; its figures were not computed from the rows"))
    return S.Sheet.model_validate(data)


__all__ = ["enforce", "copied_rows", "SCAN_ROWS", "MAX_TYPED_ROWS"]
