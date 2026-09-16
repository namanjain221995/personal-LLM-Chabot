"""Chart numbers, computed by code from a bound table — never by the model.

resolve_spec(spec, tables) walks every chart of a spec. A chart with a
`data` binding gets its categories, series, extras (bins, box stats, spans,
trend lines) and provenance computed here from the named DataTable; whatever
the model typed into those fields is overwritten. A chart with no binding and
literal numbers is BLOCKED in a new spec (replaced by a note) — a figure the
model typed is not a figure from the data. A binding that names a missing
table or column becomes a callout that says so, never a guessed chart.

CPU-BOUND AND SYNCHRONOUS. Parsing and aggregating up to 2,000,000 rows is
work an event loop must never do inline (memory: fast-mode-cpu-bound-prepass —
the orchestrator's Fast TTFT went 0.7 → 11.7 s from exactly this). Call it
with `await resolve_spec_async(...)`, which runs it in a worker thread under a
deadline, or from the render worker process.

ENGINES. Up to PANDAS_ROW_CAP rows are aggregated with pandas; above that,
up to DUCKDB_ROW_CAP, with duckdb over the same frame; beyond that the first
DUCKDB_ROW_CAP rows are used and a note says so (never silently).

DATES. Day-month order is decided per column: any first field > 12 means
dd-mm, any second field > 12 means mm-dd, all ambiguous means dd-mm with a
note (Indian data is day-first). An explicit `date_order` wins.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import difflib
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import chart_choice, chart_spec as CS

PANDAS_ROW_CAP = 200_000
DUCKDB_ROW_CAP = 2_000_000
MAX_CATEGORIES = 200
PIE_MAX_SLICES = 7
MAX_GROUP_SERIES = 8
MAX_XY_POINTS = 5_000
MAX_GANTT_ROWS = 60
MAX_BOXES = 20
HEATMAP_MAX = 50
#: A treemap stops being readable long before a pie does: a rectangle under
#: ~1% of the area cannot hold its own label, so the tail goes to "Other".
TREEMAP_MAX = 24
#: A pareto is read left to right until the cumulative line passes 80%; past
#: twenty bars the labels collide and the point is lost.
PARETO_MAX = 20
#: Two rings: at most this many inner slices, each split by MAX_GROUP_SERIES.
SUNBURST_MAX_INNER = 10
#: A violin needs enough points per group to have a shape, and the sample is
#: stored in the spec, so both the group count and the sample are bounded.
MAX_VIOLINS = 12
MAX_VIOLIN_POINTS = 600
MIN_VIOLIN_POINTS = 5
MAX_CANDLES = 300
MAX_BULLETS = 12
OTHER = "Other"
BLANK = "(blank)"


class ChartDataError(ValueError):
    """The chart cannot be computed; the message is shown in its place."""


# ------------------------------------------------------------------ tables --


def _table_attr(t: Any, key: str, default: Any = None) -> Any:
    if isinstance(t, dict):
        return t.get(key, default)
    return getattr(t, key, default)


def provenance_of(table: Any) -> str:
    tid = str(_table_attr(table, "id", "") or "")
    explicit = _table_attr(table, "provenance", None)
    if explicit in ("upload", "paste", "prompt", "answer", "sheet", "data"):
        return explicit
    for prefix in ("upload", "paste", "prompt", "answer", "sheet"):
        if tid.startswith(prefix):
            return prefix
    return "data"


def _make_table(id: str, title: str, columns: List[str], rows: List[List[Any]], source_id: str = "") -> Any:
    try:
        from .compose import DataTable  # lazy: compose imports the LLM client
    except Exception:  # pragma: no cover - compose always importable in the app
        @dataclass
        class DataTable:  # type: ignore[no-redef]
            id: str
            title: str
            columns: List[str]
            rows: List[List[Any]]
            source_id: str = ""
    return DataTable(id=id, title=title, columns=columns, rows=rows, source_id=source_id)


# ------------------------------------------------------------------ numbers --

_INDIC_DIGITS = str.maketrans("०१२३४५६७८९૦૧૨૩૪૫૬૭૮૯", "01234567890123456789")
_SUFFIXES: Dict[str, float] = {
    "k": 1e3, "thousand": 1e3, "hazar": 1e3, "hazaar": 1e3, "हजार": 1e3, "हज़ार": 1e3, "હજાર": 1e3,
    "l": 1e5, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "लाख": 1e5, "લાખ": 1e5,
    "cr": 1e7, "crore": 1e7, "crores": 1e7, "करोड़": 1e7, "करोड": 1e7, "કરોડ": 1e7,
    "m": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9,
}
_CURRENCY_RE = re.compile(r"^(?:₹|rs\.?|inr|\$|usd|€|eur|£|gbp)\s*|\s*(?:₹|rs\.?|inr|\$|usd|€|eur|£|gbp)$", re.IGNORECASE)
_GROUPED = r"(?:\d{1,3}(?:,\d{2})*,\d{3}|\d{1,3}(?:,\d{3})+)"
_NUMBER_BODY_RE = re.compile(rf"^(?P<n>{_GROUPED}(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)\s*(?P<suf>[^\d\s.,]+\.?)?$")


_APPROX_PREFIX_RE = re.compile(r"^(?:~|≈|∼|≅|about|approx\.?|approximately|circa|ca\.?|c\.)\s*", re.I)


def to_number(value: Any) -> Optional[float]:
    """A cell or a typed figure → float; None when it is not a number.
    Handles ₹/$/€/£, Western and Indian digit grouping (1,20,000), Devanagari
    and Gujarati digits, %, (negative), and k/lakh/crore/million suffixes."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, (_dt.date, _dt.datetime)):
        return None
    s = str(value).translate(_INDIC_DIGITS).strip().replace("−", "-").replace(" ", " ").replace(" ", " ")
    if not s or len(s) > 40:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1].strip()
    # "~67", "≈ 67", "approx. 67": an estimate is still a number. A column
    # with one estimated cell was typed text (6 of 7 numeric, under the 0.9
    # bar), its sum was refused, and the pie fell back to counting rows
    # (production 2026-09-16).
    s = _APPROX_PREFIX_RE.sub("", s).strip()
    # `s[:1] in "+-"` is True for an EMPTY string ("" is in every string):
    # a lone "-" cell (a blank in many exports) raised IndexError here, and
    # describe_table/prompt_guide crashed on it. Verifier 2026-09-15.
    if s and s[0] in "+-":
        neg, s = (s[0] == "-") != neg, s[1:].strip()
    s = _CURRENCY_RE.sub("", s).strip()
    if s and s[0] in "+-":  # "₹ -5"
        neg, s = (s[0] == "-") != neg, s[1:].strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    if not s:
        return None
    m = _NUMBER_BODY_RE.match(s)
    if not m:
        return None
    num = float(m.group("n").replace(",", ""))
    suf = (m.group("suf") or "").rstrip(".").casefold()
    if suf:
        if suf not in _SUFFIXES:
            return None
        num *= _SUFFIXES[suf]
    num = -num if neg else num
    return num if math.isfinite(num) else None


# -------------------------------------------------------------------- dates --

_MONTHS: Dict[str, int] = {}
for _i, _names in enumerate((
    ("jan", "january", "जनवरी", "જાન્યુઆરી"), ("feb", "february", "फरवरी", "ફેબ્રુઆરી"), ("mar", "march", "मार्च", "માર્ચ"),
    ("apr", "april", "अप्रैल", "એપ્રિલ"), ("may", "मई", "મે"), ("jun", "june", "जून", "જૂન"), ("jul", "july", "जुलाई", "જુલાઈ"),
    ("aug", "august", "अगस्त", "ઑગસ્ટ", "ઓગસ્ટ"), ("sep", "sept", "september", "सितंबर", "સપ્ટેમ્બર"),
    ("oct", "october", "अक्टूबर", "ઑક્ટોબર", "ઓક્ટોબર"), ("nov", "november", "नवंबर", "નવેમ્બર"),
    ("dec", "december", "दिसंबर", "ડિસેમ્બર"),
), start=1):
    for _n in _names:
        _MONTHS[_n] = _i
_MONTH_ABBR = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WEEKDAYS = {n: i for i, ns in enumerate((("mon", "monday"), ("tue", "tues", "tuesday"), ("wed", "wednesday"), ("thu", "thur", "thurs", "thursday"), ("fri", "friday"), ("sat", "saturday"), ("sun", "sunday"))) for n in ns}

_TIME = r"(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s*[AaPp][Mm])?(?:Z|[+-]\d{2}:?\d{2})?)?"
_ISO_RE = re.compile(rf"^(\d{{4}})[-/.](\d{{1,2}})[-/.](\d{{1,2}}){_TIME}$")
_AMBIG_RE = re.compile(rf"^(\d{{1,2}})[-/.](\d{{1,2}})[-/.](\d{{4}}|\d{{2}}){_TIME}$")
_DMY_NAMED_RE = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?[\s\-/.]*([^\W\d_]+)\.?[\s\-/.,]*(\d{4}|\d{2})$")
_MDY_NAMED_RE = re.compile(r"^([^\W\d_]+)\.?[\s\-/.]*(\d{1,2})(?:st|nd|rd|th)?,?[\s\-/.]+(\d{4})$")
_MY_RE = re.compile(r"^([^\W\d_]+)\.?[\s\-/.,']*(\d{4}|'?\d{2})$")
_YM_RE = re.compile(r"^(\d{4})[-/](\d{1,2})$")


def _year(y: str) -> int:
    y = y.lstrip("'")
    v = int(y)
    return v + 2000 if len(y) == 2 and v < 70 else (v + 1900 if len(y) == 2 else v)


def _safe_date(y: int, m: int, d: int) -> Optional[_dt.date]:
    try:
        return _dt.date(y, m, d)
    except ValueError:
        return None


def _date_shape(value: Any) -> Optional[Tuple[str, Any]]:
    """('date', date) for unambiguous forms, ('ambig', (a, b, year)) for
    a-b-yyyy, None when not a date."""
    if isinstance(value, _dt.datetime):
        return ("date", value.date())
    if isinstance(value, _dt.date):
        return ("date", value)
    if not isinstance(value, str):
        return None
    s = value.translate(_INDIC_DIGITS).strip()
    if not s or len(s) > 40:
        return None
    m = _ISO_RE.match(s)
    if m:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return ("date", d) if d else None
    m = _AMBIG_RE.match(s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), _year(m.group(3))
        if a > 31 or b > 31 or (a > 12 and b > 12):
            return None
        return ("ambig", (a, b, y))
    m = _DMY_NAMED_RE.match(s)
    if m and m.group(2).casefold() in _MONTHS:
        d = _safe_date(_year(m.group(3)), _MONTHS[m.group(2).casefold()], int(m.group(1)))
        return ("date", d) if d else None
    m = _MDY_NAMED_RE.match(s)
    if m and m.group(1).casefold() in _MONTHS:
        d = _safe_date(int(m.group(3)), _MONTHS[m.group(1).casefold()], int(m.group(2)))
        return ("date", d) if d else None
    m = _MY_RE.match(s)
    if m and m.group(1).casefold() in _MONTHS:
        return ("date", _dt.date(_year(m.group(2)), _MONTHS[m.group(1).casefold()], 1))
    m = _YM_RE.match(s)
    if m and 1 <= int(m.group(2)) <= 12:
        return ("date", _dt.date(int(m.group(1)), int(m.group(2)), 1))
    return None


def detect_date_order(values: Iterable[Any]) -> Tuple[str, str]:
    """(order, note) for a column's a-b-yyyy strings: 'dmy' | 'mdy'."""
    first_big = second_big = False
    seen = False
    for v in values:
        shape = _date_shape(v)
        if shape and shape[0] == "ambig":
            seen = True
            a, b, _ = shape[1]
            first_big = first_big or a > 12
            second_big = second_big or b > 12
    if not seen:
        return "dmy", ""
    if first_big and not second_big:
        return "dmy", ""
    if second_big and not first_big:
        return "mdy", ""
    if first_big and second_big:
        return "dmy", "the dates mix day-first and month-first forms; they were read day-first (dd-mm)"
    return "dmy", "every date could be read either way; they were read day-first (dd-mm)"


def to_date(value: Any, order: str = "dmy") -> Optional[_dt.date]:
    shape = _date_shape(value)
    if shape is None:
        return None
    if shape[0] == "date":
        return shape[1]
    a, b, y = shape[1]
    if order == "mdy":
        return _safe_date(y, a, b)
    return _safe_date(y, b, a)


# ------------------------------------------------------------------ columns --


@dataclass
class ColumnInfo:
    index: int
    name: str
    kind: str                  # number | date | text | empty
    date_order: str = ""
    note: str = ""
    distinct: List[str] = field(default_factory=list)
    n_distinct: int = 0


#: Cells that mean "no value" in real exports. Without this a numeric column
#: with one "N/A" in seven cells fell under the 90% number share, was read as
#: text, and the whole chart was refused.
_MISSING_TOKENS = frozenset({"n/a", "na", "n.a.", "#n/a", "null", "none", "nil", "nan", "-", "--", "—", "–", "?", "#value!", "#div/0!"})


def _is_blank(v: Any) -> bool:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return True
    if isinstance(v, str):
        t = v.strip()
        return not t or (len(t) <= 7 and t.casefold() in _MISSING_TOKENS)
    return False


#: Rows read to decide dd-mm vs mm-dd when a column is only DESCRIBED
#: (prompt_guide, guided_schema, repair_binding run on the event loop): an
#: evenly spaced sample, so a 2M-row table costs milliseconds, not a second.
#: compute() reads every row (full_scan=True) in its worker thread.
DATE_ORDER_SAMPLE = 20_000


def _spread(rows: Sequence[Any], limit: int) -> Sequence[Any]:
    if len(rows) <= limit:
        return rows
    stride = math.ceil(len(rows) / limit)
    return rows[::stride]


def infer_column(table: Any, index: int, sample: int = 2000, *, full_scan: bool = False) -> ColumnInfo:
    columns = list(_table_attr(table, "columns", []) or [])
    rows = _table_attr(table, "rows", []) or []
    name = str(columns[index])
    vals = [r[index] if index < len(r) else None for r in rows[:sample]]
    present = [v for v in vals if not _is_blank(v)]
    info = ColumnInfo(index=index, name=name, kind="empty")
    if not present:
        return info
    n_num = sum(1 for v in present if to_number(v) is not None)
    n_date = sum(1 for v in present if _date_shape(v) is not None)
    if n_date >= 0.9 * len(present) and n_date >= n_num:
        info.kind = "date"
        scan = rows if full_scan else _spread(rows, DATE_ORDER_SAMPLE)
        info.date_order, info.note = detect_date_order(r[index] if index < len(r) else None for r in scan)
    elif n_num >= 0.9 * len(present):
        info.kind = "number"
    else:
        info.kind = "text"
    distinct: Dict[str, None] = {}
    for v in present:
        distinct.setdefault(_label_of(v), None)
        if len(distinct) > 31:
            break
    info.n_distinct = len(distinct)
    info.distinct = list(distinct)[:8]
    return info


def describe_table(table: Any) -> str:
    """One line for the composer's prompt: id, title, rows, typed columns and
    up to 8 values of the low-cardinality text columns."""
    tid = _table_attr(table, "id", "")
    title = _table_attr(table, "title", "") or tid
    rows = _table_attr(table, "rows", []) or []
    parts = []
    for i, _ in enumerate(list(_table_attr(table, "columns", []) or [])[:40]):
        info = infer_column(table, i)
        if info.kind == "text" and info.n_distinct <= 30:
            parts.append(f"{info.name} (text: {', '.join(info.distinct)}{', …' if info.n_distinct > len(info.distinct) else ''})")
        elif info.kind == "date":
            order = info.date_order or "dmy"
            sample = _spread(rows, 5000)
            if len(sample) < len(rows):
                # An even sample plus both ends (data is usually in date order).
                sample = list(rows[:500]) + list(sample) + list(rows[-500:])
            dates = [d for d in (to_date(r[i] if i < len(r) else None, order) for r in sample) if d is not None]
            about = "about " if len(sample) < len(rows) else ""
            span = f", {about}{min(dates).isoformat()} to {max(dates).isoformat()}" if dates else ""
            parts.append(f"{info.name} (date{span})")
        else:
            parts.append(f"{info.name} ({info.kind})")
    return f'{tid} "{title}" ({len(rows):,} rows): ' + "; ".join(parts)


def _fold(name: str) -> str:
    return re.sub(r"[\s_\-]+", " ", str(name)).strip().casefold()


def match_column(name: Optional[str], columns: Sequence[str]) -> Tuple[Optional[int], str]:
    """(index, note). Exact, then case/space/underscore-insensitive, then a
    close match or a unique containment (with a note)."""
    if not name:
        return None, ""
    cols = [str(c) for c in columns]
    if name in cols:
        return cols.index(name), ""
    folded = [_fold(c) for c in cols]
    want = _fold(name)
    if want in folded:
        return folded.index(want), ""
    close = difflib.get_close_matches(want, folded, n=2, cutoff=0.8)
    if close:
        i = folded.index(close[0])
        return i, f"the column {name!r} was read as {cols[i]!r}"
    contains = [i for i, f in enumerate(folded) if want and (want in f.split() or want in f or f in want) and len(f) >= 2]
    if len(contains) == 1:
        i = contains[0]
        return i, f"the column {name!r} was read as {cols[i]!r}"
    return None, ""


# ----------------------------------------------------------------- labels --


def _label_of(v: Any) -> str:
    if _is_blank(v):
        return BLANK
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, _dt.datetime):
        return v.date().isoformat() if v.time() == _dt.time() else v.isoformat(sep=" ")
    if isinstance(v, _dt.date):
        return v.isoformat()
    return " ".join(str(v).split())[:80]


def fmt_compact(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def _bucket(d: _dt.date, bucket: str) -> Tuple[Tuple[int, ...], str]:
    if bucket == "day":
        return (d.year, d.month, d.day), f"{d.day:02d} {_MONTH_ABBR[d.month]} {d.year}"
    if bucket == "week":
        y, w, _ = d.isocalendar()
        return (y, w), f"W{w:02d} {y}"
    if bucket == "month":
        return (d.year, d.month), f"{_MONTH_ABBR[d.month]} {d.year}"
    if bucket == "quarter":
        q = (d.month - 1) // 3 + 1
        return (d.year, q), f"Q{q} {d.year}"
    return (d.year,), str(d.year)


def _auto_bucket(dates: Sequence[_dt.date]) -> str:
    if not dates:
        return "month"
    lo, hi = min(dates), max(dates)
    span = (hi - lo).days
    if span <= 62 and len(set(dates)) <= 62:
        return "day"
    if span <= 3 * 366:
        return "month"
    return "year"


_COARSER = {"day": "week", "week": "month", "month": "quarter", "quarter": "year"}


# ------------------------------------------------------------------ compute --


@dataclass
class ComputedChart:
    categories: List[str]
    series: List[CS.Series]
    extra: Optional[CS.ChartExtra]
    provenance: CS.Provenance
    notes: List[str] = field(default_factory=list)
    x_label: str = ""
    y_label: str = ""
    caption: str = ""


@dataclass
class _Frame:
    """The columns a chart needs, parsed once."""

    rows_total: int
    rows_used: int
    sampled: bool
    cols: Dict[str, List[Any]]
    names: Dict[str, str]
    kinds: Dict[str, ColumnInfo]
    notes: List[str]


def _resolve_columns(binding: CS.Binding, table: Any, chart_type: str) -> Tuple[Dict[str, int], List[str]]:
    columns = list(_table_attr(table, "columns", []) or [])
    title = _table_attr(table, "title", "") or _table_attr(table, "id", "")
    wanted: Dict[str, Optional[str]] = {"x": binding.x, "group_by": binding.group_by, "start": binding.start,
                                       "end": binding.end, "label": binding.label, "size": binding.size,
                                       "target": binding.target}
    for i, y in enumerate(binding.y):
        wanted[f"y{i}"] = y
    for i, y in enumerate(binding.y2):
        wanted[f"y2_{i}"] = y
    for i, f in enumerate(binding.filters):
        wanted[f"f{i}"] = f.column
    out: Dict[str, int] = {}
    notes: List[str] = []
    for role, name in wanted.items():
        if not name:
            continue
        idx, note = match_column(name, columns)
        if idx is None:
            raise ChartDataError(f"The column {name!r} was not found in {title}.")
        out[role] = idx
        if note:
            notes.append(note)
    return out, notes


#: A row whose category cell says it is a total (a markdown answer table's
#: "**Total**" line, a spreadsheet's "Grand total" row) is the sum of the
#: other rows: aggregated with them it counts every figure twice (a pie of
#: Rent/Food/Travel plus a "Total" slice of 50%). Verifier case 2026-09-15.
_TOTAL_LABEL_RE = re.compile(
    r"^(?:grand\s*total|sub\s*-?\s*total|total(?:s)?(?:\s*\(.*\))?|overall(?:\s*total)?|कुल(?:\s*योग)?|योग|કુલ(?:\s*સરવાળો)?|સરવાળો)\s*:?$",
    re.IGNORECASE,
)
_TOTAL_ROW_TYPES = CS.AGGREGATING_TYPES + ("box", "violin")


def _is_total_label(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = re.sub(r"[*_`]", "", value).strip()
    return bool(text) and len(text) <= 40 and _TOTAL_LABEL_RE.match(text) is not None


def _is_total_row(row: Sequence[Any]) -> bool:
    """A summary row, wherever it carries its marker.

    The x cell alone is not enough: production 2026-09-16 plotted a row whose
    RANK cell read "Total" and whose state cell read "Distinct States", so a
    count of 30 states joined five states on a pie of per-state counts. Any
    short label cell of the row marks the whole row.
    """
    return any(_is_total_label(cell) for cell in row)


def _build_frame(binding: CS.Binding, table: Any, roles: Dict[str, int], deadline: Optional[float], chart_type: str = "") -> _Frame:
    rows = list(_table_attr(table, "rows", []) or [])
    total = len(rows)
    notes: List[str] = []
    sampled = False
    if total > DUCKDB_ROW_CAP:
        rows = rows[:DUCKDB_ROW_CAP]
        sampled = True
        notes.append(f"the table has {total:,} rows; the chart uses the first {DUCKDB_ROW_CAP:,}")
    columns = list(_table_attr(table, "columns", []) or [])
    kinds: Dict[int, ColumnInfo] = {}
    for idx in set(roles.values()):
        kinds[idx] = infer_column(table, idx, full_scan=True)
    # Filters first, on raw cells.
    keep = list(range(len(rows)))
    for i, flt in enumerate(binding.filters):
        idx = roles[f"f{i}"]
        info = kinds[idx]
        order = binding.date_order if binding.date_order in ("dmy", "mdy") else (info.date_order or "dmy")
        keep = [r for r in keep if _passes(rows[r][idx] if idx < len(rows[r]) else None, flt, info.kind, order)]
        _check_deadline(deadline)
    x_idx = roles.get("x")
    filters_on_x = any(roles.get(f"f{i}") == x_idx for i in range(len(binding.filters)))
    if chart_type in _TOTAL_ROW_TYPES and x_idx is not None and kinds[x_idx].kind == "text" and not filters_on_x:
        before = len(keep)
        dropped = [r for r in keep if _is_total_row(rows[r])]
        if dropped:
            labels = sorted({_label_of(rows[r][x_idx]) for r in dropped})[:3]
            keep = [r for r in keep if not _is_total_row(rows[r])]
            notes.append(f"{before - len(keep)} total row{'s' if before - len(keep) != 1 else ''} ({', '.join(labels)}) left out so no figure is counted twice")
    cols: Dict[str, List[Any]] = {}
    for role, idx in roles.items():
        if role.startswith("f"):
            continue
        cols[role] = [rows[r][idx] if idx < len(rows[r]) else None for r in keep]
    names = {role: str(columns[idx]) for role, idx in roles.items()}
    by_role = {role: kinds[idx] for role, idx in roles.items()}
    for info in kinds.values():
        if info.note and info.kind == "date" and binding.date_order == "auto":
            notes.append(f"{info.name}: {info.note}")
    return _Frame(rows_total=total, rows_used=len(keep), sampled=sampled, cols=cols, names=names, kinds=by_role, notes=notes)


def _check_deadline(deadline: Optional[float]) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise ChartDataError("The chart took too long to compute and was left out.")


def _passes(value: Any, flt: CS.Filter, kind: str, order: str) -> bool:
    op, target = flt.op, flt.value
    if op == "blank":
        return _is_blank(value)
    if op == "not_blank":
        return not _is_blank(value)
    targets = target if isinstance(target, list) else [target]

    def coerce(v: Any) -> Any:
        if kind == "number":
            return to_number(v)
        if kind == "date":
            return to_date(v, order) if not isinstance(v, (_dt.date,)) else v
        return None if _is_blank(v) else " ".join(str(v).split()).casefold()

    left = coerce(value)
    rights = [coerce(t) if not (kind == "text" and isinstance(t, (int, float))) else _label_of(t).casefold() for t in targets]
    if op in ("eq", "ne", "in", "not_in"):
        hit = left is not None and left in rights
        return hit if op in ("eq", "in") else not hit
    if op == "contains":
        text = " ".join(str(value or "").split()).casefold()
        return any(str(t).casefold() in text for t in targets if t is not None)
    if left is None or not rights or rights[0] is None:
        return False
    try:
        if op == "gt":
            return left > rights[0]
        if op == "gte":
            return left >= rights[0]
        if op == "lt":
            return left < rights[0]
        if op == "lte":
            return left <= rights[0]
        if op == "between" and len(rights) >= 2 and rights[1] is not None:
            return rights[0] <= left <= rights[1]
    except TypeError:
        return False
    return False


def _keys_for(frame: _Frame, role: str, binding: CS.Binding, chart_type: str) -> Tuple[List[Any], Dict[Any, str], str, List[str]]:
    """(sort keys per row, key → label, bucket, notes) for the x or group
    column. Dates are bucketed; numbers keep numeric order; text keeps
    the cell."""
    info = frame.kinds[role]
    values = frame.cols[role]
    notes: List[str] = []
    labels: Dict[Any, str] = {}
    if info.kind == "date" and role == "x":
        order = binding.date_order if binding.date_order in ("dmy", "mdy") else (info.date_order or "dmy")
        dates = [to_date(v, order) for v in values]
        present = [d for d in dates if d is not None]
        bucket = binding.date_bucket or _auto_bucket(present)
        while True:
            keys: List[Any] = []
            labels = {}
            for d in dates:
                if d is None:
                    keys.append(("~blank",))
                    labels[("~blank",)] = BLANK
                else:
                    k, lab = _bucket(d, bucket)
                    keys.append(k)
                    labels[k] = lab
            if len(labels) <= MAX_CATEGORIES or bucket == "year":
                break
            notes.append(f"{len(labels):,} {bucket}s are too many to draw; the dates were grouped by {_COARSER[bucket]}")
            bucket = _COARSER[bucket]
        return keys, labels, bucket, notes
    if info.kind == "number" and role == "x" and chart_type not in ("pie", "donut", "funnel"):
        nums = [to_number(v) for v in values]
        present = [n for n in nums if n is not None]
        # A column of years (2022, 2023, …) is labelled 2024, not "2,024".
        yearish = bool(present) and all(n.is_integer() and 1800 <= n <= 2200 for n in present)
        keys = []
        for n in nums:
            k = (0, n) if n is not None else (1, 0.0)
            keys.append(k)
            labels[k] = BLANK if n is None else (str(int(n)) if yearish else fmt_compact(n))
        return keys, labels, "", notes
    keys = []
    for v in values:
        lab = _label_of(v)
        k = ("t", lab.casefold())
        keys.append(k)
        labels.setdefault(k, lab)
    return keys, labels, "", notes


_ARRAY_CHUNK = 16_384


def _chunked_array(values: Iterable[Any], n: int, dtype: str) -> Any:
    """A numpy array built in chunks, yielding the GIL between them.

    One np.array()/pd.array()/DataFrame() call over a 2,000,000-item Python
    list holds the GIL for over a second, and a worker thread that holds the
    GIL stalls the event loop exactly as if the work ran inline (measured
    2026-09-15: 3.8 s heartbeat gaps at 2.1M rows). np.fromiter over 64k
    items holds it for a few milliseconds; time.sleep(0) lets the loop run."""
    import numpy as np  # lazy

    it = iter(values)
    parts = []
    left = n
    while left > 0:
        take = min(_ARRAY_CHUNK, left)
        parts.append(np.fromiter(it, dtype=dtype, count=take))
        left -= take
        time.sleep(0)
    if not parts:
        return np.empty(0, dtype=dtype)
    return parts[0] if len(parts) == 1 else np.concatenate(parts)


def _aggregate(keys: List[Any], groups: List[Any], ycols: List[List[Optional[float]]], agg: str) -> Tuple[Dict[Tuple[Any, Any], List[float]], str]:
    """(key, group) → [aggregate per y]. pandas up to PANDAS_ROW_CAP rows,
    duckdb above."""
    n = len(keys)
    if n == 0:
        return {}, "python"
    import pandas as pd  # lazy

    codes_k: Dict[Any, int] = {}
    codes_g: Dict[Any, int] = {}
    kc = _chunked_array((codes_k.setdefault(k, len(codes_k)) for k in keys), n, "int64")
    gc = _chunked_array((codes_g.setdefault(g, len(codes_g)) for g in groups), n, "int64")
    inv_k = {v: k for k, v in codes_k.items()}
    inv_g = {v: g for g, v in codes_g.items()}
    data: Dict[str, Any] = {"k": kc, "g": gc}
    width = max(1, len(ycols))
    nan = float("nan")
    for i in range(width):
        col = ycols[i] if i < len(ycols) else [None] * n
        data[f"y{i}"] = _chunked_array((nan if v is None else v for v in col), n, "float64")
    df = pd.DataFrame(data, copy=False)
    out: Dict[Tuple[Any, Any], List[float]] = {}
    if n <= PANDAS_ROW_CAP:
        engine = "pandas"
        grouped = df.groupby(["k", "g"], sort=False)
        if agg == "count":
            res = grouped.size().to_frame("y0")
        else:
            fn = {"sum": "sum", "avg": "mean", "min": "min", "max": "max", "median": "median", "none": "sum"}[agg]
            res = grouped[[f"y{i}" for i in range(width)]].agg(fn)
        flat = res.reset_index()
        ks = flat["k"].to_numpy()
        gs = flat["g"].to_numpy()
        cols = [flat[f"y{i}"].to_numpy(dtype="float64") for i in range(width if agg != "count" else 1)]
        for row_i in range(len(flat)):
            vals = [float(c[row_i]) for c in cols]
            out[(inv_k[int(ks[row_i])], inv_g[int(gs[row_i])])] = [0.0 if math.isnan(v) else v for v in vals]
    else:
        engine = "duckdb"
        import duckdb  # lazy

        con = duckdb.connect(database=":memory:")
        try:
            con.register("t", df)
            if agg == "count":
                sel = "COUNT(*) AS y0"
            else:
                fn = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX", "median": "MEDIAN", "none": "SUM"}[agg]
                sel = ", ".join(f"{fn}(y{i}) AS y{i}" for i in range(width))
            for rec in con.execute(f"SELECT k, g, {sel} FROM t GROUP BY k, g").fetchall():
                vals = [0.0 if v is None or (isinstance(v, float) and math.isnan(v)) else float(v) for v in rec[2:]]
                out[(inv_k[int(rec[0])], inv_g[int(rec[1])])] = vals
        finally:
            con.close()
    return out, engine


def _month_or_weekday_order(labels: Sequence[str]) -> Optional[Dict[str, int]]:
    folded = [l.casefold().rstrip(".") for l in labels]
    if folded and all(f in _MONTHS for f in folded):
        return {l: _MONTHS[f] for l, f in zip(labels, folded)}
    if folded and all(f in _WEEKDAYS for f in folded):
        return {l: _WEEKDAYS[f] for l, f in zip(labels, folded)}
    return None


def _numbers(values: List[Any]) -> List[Optional[float]]:
    return [to_number(v) for v in values]


#: Columns that number or index the rows of a derived table rather than
#: measure anything: plotting them says nothing about the subject.
_NON_MEASURE_RE = re.compile(r"^(?:rank|ranking|sr\.?|s\.?\s*no\.?|no\.?|#|index|position|serial|row|year|month|day|date|id|code|pin|zip|phone|percent(?:age)?|%.*|.*%.*|share.*|.*\bpct\b.*)$", re.I)


def _measure_column(table: Any, binding: CS.Binding, roles: Dict[str, int]) -> Optional[str]:
    """The one numeric column a per-row table is really about, or None.

    A table with one row per category ("State | Count | % of total") cannot be
    summarised by COUNTING its rows: every category weighs 1 and the chart says
    nothing. Production 2026-09-16 drew exactly that — six states, six equal
    slices — because the measure column carried one estimated cell ("~67") and
    the binding fell back to a row count. When the table offers exactly one
    plottable measure, that is the chart's subject; when it offers none or
    several, the count stands and the request is answered as written.
    """
    columns = [str(c) for c in (_table_attr(table, "columns", []) or [])]
    taken = {roles.get("x"), roles.get("group_by")}
    candidates: List[str] = []
    for idx, name in enumerate(columns):
        if idx in taken or _NON_MEASURE_RE.match(name.strip()):
            continue
        if infer_column(table, idx, full_scan=True).kind == "number":
            candidates.append(name)
    return candidates[0] if len(candidates) == 1 else None


def _one_row_per_category(table: Any, roles: Dict[str, int]) -> bool:
    """True when the x column holds a different label on every row."""
    idx = roles.get("x")
    rows = list(_table_attr(table, "rows", []) or [])
    if idx is None or not rows:
        return False
    labels = [_label_of(r[idx]) for r in rows if idx < len(r) and not _is_total_row(r)]
    return len(labels) > 1 and len(set(labels)) == len(labels)


def frame_label(roles: Dict[str, int], table: Any) -> str:
    """The x column's name, for a note about how the rows were read."""
    columns = [str(c) for c in (_table_attr(table, "columns", []) or [])]
    idx = roles.get("x")
    return columns[idx] if idx is not None and idx < len(columns) else "category"


def compute(chart: CS.Chart, table: Any, *, deadline: Optional[float] = None) -> ComputedChart:
    """The chart's numbers from `table`. Raises ChartDataError with the
    sentence to show instead of the chart."""
    if chart.data is None:
        raise ChartDataError("This chart has no data binding.")
    b = chart.data
    t = chart.type
    roles, notes = _resolve_columns(b, table, t)
    if (
        b.agg == "count" and not b.y and not b.y2
        and t in CS.AGGREGATING_TYPES
        and _one_row_per_category(table, roles)
    ):
        measure = _measure_column(table, b, roles)
        if measure is not None:
            b = b.model_copy(update={"y": [measure], "agg": "sum"})
            chart = chart.model_copy(update={"data": b})
            roles, extra_notes = _resolve_columns(b, table, t)
            notes = notes + extra_notes + [
                f"each row of this table is one {frame_label(roles, table)}, so counting rows would weigh them equally; "
                f"{measure} was summed instead"
            ]
    frame = _build_frame(b, table, roles, deadline, t)
    notes = notes + frame.notes
    title = str(_table_attr(table, "title", "") or _table_attr(table, "id", ""))
    prov = CS.Provenance(
        table_id=str(_table_attr(table, "id", "")), table_title=title[:200], table_provenance=provenance_of(table),  # type: ignore[arg-type]
        rows_total=frame.rows_total, rows_used=frame.rows_used, agg=b.agg,
        filters=[_filter_text(f, frame) for f in b.filters][:5], sampled=frame.sampled,
    )
    if frame.rows_used == 0:
        raise ChartDataError(f"No rows of {title} matched the chart's filters." if b.filters else f"{title} has no rows to chart.")
    if t in CS.XY_TYPES:
        result = _compute_xy(chart, frame, prov, deadline)
    elif t == "histogram":
        result = _compute_histogram(chart, frame, prov)
    elif t in ("box", "violin"):
        result = _compute_box(chart, frame, prov)
    elif t == "gantt":
        result = _compute_gantt(chart, frame, prov)
    elif t == "candlestick":
        result = _compute_candlestick(chart, frame, prov)
    elif t == "bullet":
        result = _compute_bullet(chart, frame, prov, deadline)
    elif t == "pareto":
        result = _compute_pareto(chart, frame, prov, deadline)
    else:
        result = _compute_grouped(chart, frame, prov, deadline)
    result.notes = notes + result.notes
    if frame.kinds.get("x") is not None and frame.kinds["x"].kind == "date":
        order = b.date_order if b.date_order != "auto" else (frame.kinds["x"].date_order or "dmy")
        result.provenance.date_order = order
    result.caption = _caption(chart, result, frame)
    return result


def _filter_text(f: CS.Filter, frame: _Frame) -> str:
    sym = {"eq": "=", "ne": "≠", "in": "in", "not_in": "not in", "contains": "contains", "gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "between": "between", "blank": "is blank", "not_blank": "is not blank"}[f.op]
    val = "" if f.op in ("blank", "not_blank") else (", ".join(_label_of(v) for v in f.value) if isinstance(f.value, list) else _label_of(f.value))
    return f"{f.column} {sym} {val}".strip()[:120]


def _compute_grouped(chart: CS.Chart, frame: _Frame, prov: CS.Provenance, deadline: Optional[float]) -> ComputedChart:
    b = chart.data
    assert b is not None
    t = chart.type
    notes: List[str] = []
    agg = b.agg
    y_roles = [f"y{i}" for i in range(len(b.y))]
    y2_roles = [f"y2_{i}" for i in range(len(b.y2))]
    all_y = y_roles + y2_roles
    if not all_y and agg != "count":
        agg = "count"
        notes.append("no numeric column was named, so rows were counted")
    for role in all_y:
        if frame.kinds[role].kind not in ("number", "empty") and agg != "count":
            raise ChartDataError(f"The column {frame.names[role]!r} is not numeric, so it cannot be plotted as a {CS.AGG_LABELS[agg].lower()}.")
        if agg != "count" and frame.kinds[role].kind == "empty" and all(_is_blank(v) for v in frame.cols[role]):
            # "empty" is on the whitelist above because infer_column reads the
            # first 2,000 rows only: a number column that starts blank must
            # still be plottable. A column blank in every row the chart would
            # USE is a different thing — production 2026-09-16 asked for a map
            # of PersonMailingLatitude / PersonMailingLongitude, which are null
            # in 100% of that file, and a sum over nothing drew a row of zeros
            # that looked like real data.
            raise ChartDataError(f"The column {frame.names[role]!r} is empty in every row, so there is nothing to plot.")
    if t == "heatmap" and not b.group_by:
        raise ChartDataError("A heatmap needs a column for its rows (group_by).")
    if t == "sunburst" and not b.group_by:
        raise ChartDataError("A sunburst needs a second level: the column its outer ring splits by (group_by).")
    if "x" not in frame.cols:
        raise ChartDataError("The chart needs a category column (x).")
    prov.agg = agg
    keys, klabels, bucket, knotes = _keys_for(frame, "x", b, t)
    notes.extend(knotes)
    prov.date_bucket = bucket
    grouped = bool(b.group_by) and t in CS.SPLIT_TYPES
    if b.group_by and not grouped:
        notes.append(f"a {t.replace('_', ' ')} has one series, so the split by {b.group_by} was not used")
    if grouped:
        gkeys, glabels, _, _ = _keys_for(frame, "group_by", b, t)
        if len(all_y) > 1:
            notes.append(f"with a split by {frame.names['group_by']}, only {frame.names[all_y[0]]} is plotted")
            all_y = all_y[:1]
            y_roles = [r for r in y_roles if r in all_y]
            y2_roles = [r for r in y2_roles if r in all_y]
    else:
        gkeys, glabels = [("all",)] * len(keys), {("all",): ""}
    ycols = [_numbers(frame.cols[r]) for r in all_y] if agg != "count" else []
    _check_deadline(deadline)
    cells, engine = _aggregate(keys, gkeys, ycols, agg)
    prov.engine = engine  # type: ignore[assignment]

    # Category order.
    totals_k: Dict[Any, float] = {}
    first_seen: Dict[Any, int] = {}
    for i, k in enumerate(keys):
        first_seen.setdefault(k, i)
    for (k, g), vals in cells.items():
        totals_k[k] = totals_k.get(k, 0.0) + (vals[0] if vals else 0.0)
    order_keys = list(first_seen)
    info_x = frame.kinds["x"]
    sort = b.sort
    if sort == "auto":
        calendar_text = info_x.kind == "text" and _month_or_weekday_order([klabels[k] for k in order_keys]) is not None
        if info_x.kind in ("date",) or calendar_text or (info_x.kind == "number" and t not in ("pie", "donut", "funnel")):
            sort = "x"
        elif t in ("pie", "donut", "funnel", "bar", "horizontal_bar", "stacked_bar", "stacked_horizontal_bar",
                   "percent_stacked_bar", "treemap", "sunburst", "pareto"):
            sort = "value_desc"
        else:
            sort = "none"
    if sort == "x":
        month_order = _month_or_weekday_order([klabels[k] for k in order_keys]) if info_x.kind == "text" else None
        if month_order:
            order_keys.sort(key=lambda k: month_order[klabels[k]])
        else:
            order_keys.sort(key=lambda k: (k[0] == "~blank", k))
    elif sort == "value_desc":
        order_keys.sort(key=lambda k: (-totals_k.get(k, 0.0), first_seen[k]))
    elif sort == "value_asc":
        order_keys.sort(key=lambda k: (totals_k.get(k, 0.0), first_seen[k]))
    elif info_x.kind == "text":
        month_order = _month_or_weekday_order([klabels[k] for k in order_keys])
        if month_order:
            order_keys.sort(key=lambda k: month_order[klabels[k]])

    # top_n / pie slices / category cap: fold the rest into Other by
    # re-aggregating the folded rows (correct for every agg, not only sum).
    limit = b.top_n
    if t in ("pie", "donut"):
        limit = min(limit or PIE_MAX_SLICES, PIE_MAX_SLICES)
    if t == "heatmap":
        limit = min(limit or HEATMAP_MAX, HEATMAP_MAX)
    if t == "treemap":
        limit = min(limit or TREEMAP_MAX, TREEMAP_MAX)
    if t == "pareto":
        limit = min(limit or PARETO_MAX, PARETO_MAX)
    if t == "sunburst":
        limit = min(limit or SUNBURST_MAX_INNER, SUNBURST_MAX_INNER)
    if t == "bullet":
        limit = min(limit or MAX_BULLETS, MAX_BULLETS)
    if limit is None and len(order_keys) > MAX_CATEGORIES:
        limit = MAX_CATEGORIES
        notes.append(f"{len(order_keys):,} categories are too many to draw; the largest {MAX_CATEGORIES - 1} are shown and the rest are in {OTHER}")
    fold_other = False
    if limit is not None and len(order_keys) > limit:
        use_other = b.other_bucket
        keep_n = limit - 1 if use_other else limit
        ranked = sorted(order_keys, key=lambda k: (-totals_k.get(k, 0.0), first_seen[k]))
        kept = set(ranked[:keep_n])
        dropped = len(order_keys) - keep_n
        if t in ("pie", "donut") and b.top_n is None:
            notes.append(f"a pie shows at most {PIE_MAX_SLICES} slices; {dropped} smaller categories are combined in {OTHER}")
        elif t in ("treemap", "pareto", "sunburst") and b.top_n is None:
            notes.append(f"a {t} shows at most {limit} categories; the {dropped} smallest are combined in {OTHER}")
        order_keys = [k for k in order_keys if k in kept]
        if use_other:
            fold_other = True
            other_key = ("~other",)
            klabels[other_key] = OTHER
            keys = [k if k in kept else other_key for k in keys]
            cells, _ = _aggregate(keys, gkeys, ycols, agg)
            order_keys.append(other_key)

    # Group (series) order and cap.
    group_order: List[Any] = []
    if grouped:
        totals_g: Dict[Any, float] = {}
        for (k, g), vals in cells.items():
            totals_g[g] = totals_g.get(g, 0.0) + (vals[0] if vals else 0.0)
        group_order = sorted(totals_g, key=lambda g: -totals_g[g])
        if t != "heatmap":
            gl = [glabels[g] for g in group_order]
            mo = _month_or_weekday_order(gl)
            if mo:
                group_order.sort(key=lambda g: mo[glabels[g]])
        cap = HEATMAP_MAX if t == "heatmap" else MAX_GROUP_SERIES
        if len(group_order) > cap:
            kept_g = set(group_order[: cap - 1])
            notes.append(f"{len(group_order)} values of {frame.names['group_by']} are too many series; the largest {cap - 1} are shown and the rest are in {OTHER}")
            other_g = ("~other",)
            glabels[other_g] = OTHER
            gkeys = [g if g in kept_g else other_g for g in gkeys]
            cells, _ = _aggregate(keys, gkeys, ycols, agg)
            group_order = [g for g in group_order if g in kept_g] + [other_g]
    categories = [klabels[k] for k in order_keys]
    if t in CS.NON_NEGATIVE_TYPES and agg != "count":
        negative = [(f"{klabels[k]}{'' if g == ('all',) else ' / ' + (glabels[g] or BLANK)}", cells[(k, g)][0])
                    for k in order_keys for g in (group_order or [("all",)])
                    if cells.get((k, g)) and cells[(k, g)][0] < 0]
        if negative:
            # matplotlib draws a negative wedge as nothing and the shares of
            # the rest come out wrong (verifier sample: "55%" of a cash flow
            # whose outflows vanished, their labels piled on one spot).
            name, value = negative[0]
            raise ChartDataError(
                f"A {t} cannot show negative values ({name} is {fmt_compact(value)}); a bar or waterfall chart can."
            )
    series: List[CS.Series] = []
    extra: Optional[CS.ChartExtra] = None
    if grouped:
        empty_cells: List[str] = []
        for g in group_order:
            vals = [cells.get((k, g), [0.0])[0] for k in order_keys]
            if agg not in ("sum", "count", "none"):
                empty_cells.extend(f"{glabels[g] or BLANK} in {klabels[k]}" for k in order_keys if (k, g) not in cells)
            series.append(CS.Series(name=(glabels[g] or BLANK)[:80], values=vals))
        if empty_cells:
            # A missing (category, group) is a true 0 for a sum or a count,
            # but an average/min/max/median of no rows is not 0: say so.
            shown = "; ".join(empty_cells[:3]) + (f" and {len(empty_cells) - 3} more" if len(empty_cells) > 3 else "")
            notes.append(f"no rows for {shown}: the {CS.AGG_LABELS[agg].lower()} there is drawn as 0, which is not a measured value")
        if t == "heatmap":
            extra = CS.ChartExtra(heatmap_rows=[s.name for s in series][:60])
        elif t == "sunburst":
            # The outer ring is exactly the (category, group) cells that hold
            # something; a zero cell would be a zero-width wedge, so it is not
            # drawn and is not counted.
            extra = CS.ChartExtra(ring_counts=[sum(1 for sr in series if sr.values[j] > 0) for j in range(len(order_keys))])
    else:
        if agg == "count":
            names = ["Count"]
        else:
            names = [frame.names[r] for r in all_y]
        for i, name in enumerate(names):
            vals = [cells.get((k, ("all",)), [0.0] * max(1, len(names)))[i] for k in order_keys]
            role = all_y[i] if all_y else ""
            kw: Dict[str, Any] = {}
            if t == "combo":
                if role in y2_roles:
                    kw = {"axis": "secondary", "kind": "line"}
                elif not y2_roles and i > 0:
                    kw = {"kind": "line"}
                else:
                    kw = {"kind": "bar"}
            series.append(CS.Series(name=_unique_name(name, series), values=vals, **kw))
    if fold_other and t in ("pie", "donut") and series and len(series) == 1 and categories and categories[-1] == OTHER and series[0].values[-1] == 0:
        categories, series[0] = categories[:-1], series[0].model_copy(update={"values": series[0].values[:-1]})
    x_label = frame.names.get("x", "")
    if bucket:
        x_label = f"{x_label} ({bucket})"
    if agg == "count":
        y_label = "Count"
    elif len(all_y) == 1 or grouped:
        base = frame.names[all_y[0]]
        y_label = base if agg in ("sum", "none") else f"{CS.AGG_LABELS[agg]} {base}"
    else:
        y_label = CS.AGG_LABELS[agg] if agg not in ("sum", "none") else ""
    return ComputedChart(categories=categories, series=series, extra=extra, provenance=prov, notes=notes, x_label=x_label, y_label=y_label[:60])


def _unique_name(name: str, existing: Sequence[CS.Series]) -> str:
    base = (name or "Series")[:76]
    names = {s.name for s in existing}
    if base not in names:
        return base
    i = 2
    while f"{base} {i}" in names:
        i += 1
    return f"{base} {i}"


def _compute_xy(chart: CS.Chart, frame: _Frame, prov: CS.Provenance, deadline: Optional[float]) -> ComputedChart:
    import numpy as np  # lazy

    b = chart.data
    assert b is not None
    notes: List[str] = []
    if "x" not in frame.cols or not b.y:
        raise ChartDataError(f"A {chart.type} chart needs a numeric x column and a numeric y column.")
    if chart.type == "bubble" and "size" not in frame.cols:
        raise ChartDataError("A bubble chart needs a size column.")
    xs_all = _numbers(frame.cols["x"])
    if frame.kinds["x"].kind == "date":
        order = b.date_order if b.date_order in ("dmy", "mdy") else (frame.kinds["x"].date_order or "dmy")
        xs_all = [float(d.toordinal()) if d else None for d in (to_date(v, order) for v in frame.cols["x"])]
        notes.append(f"{frame.names['x']} is a date; it is plotted as a day number")
    y_roles = [f"y{i}" for i in range(len(b.y))]
    sizes_all = _numbers(frame.cols["size"]) if chart.type == "bubble" else None
    groups: List[Tuple[str, List[int]]] = []
    if b.group_by and "group_by" in frame.cols:
        idx_by: Dict[str, List[int]] = {}
        for i, v in enumerate(frame.cols["group_by"]):
            idx_by.setdefault(_label_of(v), []).append(i)
        ordered = sorted(idx_by.items(), key=lambda kv: -len(kv[1]))[:MAX_GROUP_SERIES]
        groups = [(k, v) for k, v in ordered]
        y_roles = y_roles[:1]
    else:
        groups = [("", list(range(len(xs_all))))]
    series: List[CS.Series] = []
    trends: List[CS.Trend] = []
    dropped = 0
    for role in y_roles:
        ys_all = _numbers(frame.cols[role])
        for gname, idxs in groups:
            pts = []
            for i in idxs:
                x, y = xs_all[i], ys_all[i]
                s = sizes_all[i] if sizes_all is not None else 0.0
                if x is None or y is None or s is None:
                    dropped += 1
                    continue
                pts.append((x, y, s))
            if not pts:
                continue
            name = gname if gname else frame.names[role]
            if gname and len(y_roles) > 1:
                name = f"{frame.names[role]} · {gname}"
            if b.trendline and len(pts) >= 2:
                xa = np.array([p[0] for p in pts], dtype="float64")
                ya = np.array([p[1] for p in pts], dtype="float64")
                if float(np.ptp(xa)) > 0:
                    slope, intercept = np.polyfit(xa, ya, 1)
                    pred = slope * xa + intercept
                    ss_res = float(np.sum((ya - pred) ** 2))
                    ss_tot = float(np.sum((ya - ya.mean()) ** 2))
                    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
                    trends.append(CS.Trend(series=name[:80], slope=float(slope), intercept=float(intercept), r2=float(r2)))
            if len(pts) > MAX_XY_POINTS:
                stride = math.ceil(len(pts) / MAX_XY_POINTS)
                notes.append(f"{len(pts):,} points are too many to draw; every {stride}th point is shown (the trend line uses all of them)")
                pts = pts[::stride]
            kw: Dict[str, Any] = {"x": [p[0] for p in pts]}
            if sizes_all is not None:
                kw["sizes"] = [p[2] for p in pts]
            series.append(CS.Series(name=_unique_name(name, series), values=[p[1] for p in pts], **kw))
            _check_deadline(deadline)
    if dropped:
        notes.append(f"{dropped:,} rows without a number in every plotted column were left out")
    if not series:
        raise ChartDataError("No row has numbers in both plotted columns.")
    extra = CS.ChartExtra(trendlines=trends) if trends else None
    return ComputedChart(categories=[], series=series[:60], extra=extra, provenance=prov, notes=notes,
                         x_label=frame.names["x"], y_label=frame.names[y_roles[0]] if len(y_roles) == 1 else "")


def _compute_histogram(chart: CS.Chart, frame: _Frame, prov: CS.Provenance) -> ComputedChart:
    import numpy as np  # lazy

    b = chart.data
    assert b is not None
    role = "y0" if b.y else ("x" if "x" in frame.cols else None)
    if role is None:
        raise ChartDataError("A histogram needs one numeric column.")
    values_all = _numbers(frame.cols[role])
    clean = [v for v in values_all if v is not None]
    if not clean:
        raise ChartDataError(f"The column {frame.names[role]!r} has no numbers.")
    arr = np.array(clean, dtype="float64")
    if b.bins:
        edges = np.histogram_bin_edges(arr, bins=b.bins)
    else:
        edges = np.histogram_bin_edges(arr, bins="auto")
        if len(edges) - 1 > 50:
            edges = np.histogram_bin_edges(arr, bins=50)
        elif len(edges) - 1 < 5 and len(set(clean)) > 5:
            edges = np.histogram_bin_edges(arr, bins=min(10, len(set(clean))))
    labels = [f"{fmt_compact(float(edges[i]))}–{fmt_compact(float(edges[i + 1]))}" for i in range(len(edges) - 1)]
    series: List[CS.Series] = []
    if b.group_by and "group_by" in frame.cols:
        by: Dict[str, List[float]] = {}
        for v, g in zip(values_all, frame.cols["group_by"]):
            if v is not None:
                by.setdefault(_label_of(g), []).append(v)
        for g, vals in sorted(by.items(), key=lambda kv: -len(kv[1]))[:MAX_GROUP_SERIES]:
            counts, _ = np.histogram(np.array(vals, dtype="float64"), bins=edges)
            series.append(CS.Series(name=g[:80], values=[float(c) for c in counts]))
    else:
        counts, _ = np.histogram(arr, bins=edges)
        series.append(CS.Series(name="Count", values=[float(c) for c in counts]))
    prov.agg = "count"
    notes = []
    if len(clean) < len(values_all):
        notes.append(f"{len(values_all) - len(clean):,} rows without a number in {frame.names[role]} were left out")
    return ComputedChart(categories=labels, series=series, extra=CS.ChartExtra(bin_edges=[float(e) for e in edges]), provenance=prov,
                         notes=notes, x_label=frame.names[role], y_label="Count")


def _box_stats(name: str, vals: List[float]) -> CS.BoxStats:
    import numpy as np  # lazy

    a = np.array(vals, dtype="float64")
    q1, med, q3 = (float(x) for x in np.percentile(a, [25, 50, 75]))
    iqr = q3 - q1
    lo_f, hi_f = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    inside = a[(a >= lo_f) & (a <= hi_f)]
    wl = float(inside.min()) if inside.size else float(a.min())
    wh = float(inside.max()) if inside.size else float(a.max())
    outliers = sorted(float(x) for x in a[(a < lo_f) | (a > hi_f)])[:200]
    return CS.BoxStats(name=name[:80], n=int(a.size), min=float(a.min()), q1=q1, median=med, q3=q3, max=float(a.max()),
                       mean=float(a.mean()), whisker_low=wl, whisker_high=wh, outliers=outliers)


def _compute_box(chart: CS.Chart, frame: _Frame, prov: CS.Provenance) -> ComputedChart:
    """Box and violin: the same grouping, the same five numbers. A violin
    also carries each group's SAMPLE, because its width at a height is a
    density estimated from those numbers and nothing else."""
    b = chart.data
    assert b is not None
    violin = chart.type == "violin"
    what = "violin plot" if violin else "box plot"
    boxes: List[CS.BoxStats] = []
    samples: List[Tuple[str, List[float]]] = []
    notes: List[str] = []
    if not b.y:
        raise ChartDataError(f"A {what} needs a numeric column (y).")
    cap = MAX_VIOLINS if violin else MAX_BOXES
    split = "group_by" if "group_by" in frame.cols else ("x" if "x" in frame.cols else None)
    if split:
        by: Dict[str, List[float]] = {}
        ys = _numbers(frame.cols["y0"])
        for v, g in zip(ys, frame.cols[split]):
            if v is not None:
                by.setdefault(_label_of(g), []).append(v)
        ordered = list(by.items())
        mo = _month_or_weekday_order([k for k, _ in ordered])
        ordered.sort(key=(lambda kv: mo[kv[0]]) if mo else (lambda kv: -len(kv[1])))
        if len(ordered) > cap:
            notes.append(f"{len(ordered)} groups are too many to draw; the {cap} with the most rows are shown")
            ordered = sorted(ordered, key=lambda kv: -len(kv[1]))[:cap]
        if violin:
            # A density curve drawn through two or three points is a shape
            # the data does not support; such a group is left out by name.
            thin = [k for k, v in ordered if len(v) < MIN_VIOLIN_POINTS]
            if thin:
                notes.append(
                    f"{', '.join(thin[:3])} ha{'s' if len(thin) == 1 else 've'} fewer than {MIN_VIOLIN_POINTS} values, "
                    "too few for a distribution, so no violin is drawn for them"
                )
                ordered = [(k, v) for k, v in ordered if len(v) >= MIN_VIOLIN_POINTS]
        boxes = [_box_stats(k, v) for k, v in ordered if v]
        samples = [(k, v) for k, v in ordered if v]
    else:
        for i in range(len(b.y)):
            vals = [v for v in _numbers(frame.cols[f"y{i}"]) if v is not None]
            if violin and len(vals) < MIN_VIOLIN_POINTS:
                continue
            if vals:
                boxes.append(_box_stats(frame.names[f"y{i}"], vals))
                samples.append((frame.names[f"y{i}"], vals))
    if not boxes:
        raise ChartDataError(f"There are no numbers to draw a {what} from.")
    extra = CS.ChartExtra(box=boxes)
    if violin:
        dists = []
        trimmed = 0
        for name, vals in samples[:cap]:
            kept = sorted(vals)
            if len(kept) > MAX_VIOLIN_POINTS:
                stride = math.ceil(len(kept) / MAX_VIOLIN_POINTS)
                kept = kept[::stride][:MAX_VIOLIN_POINTS]
                trimmed += 1
            dists.append(CS.Distribution(name=name[:80], n=len(vals), values=kept))
        if trimmed:
            notes.append(f"the curve of {trimmed} group{'s' if trimmed != 1 else ''} is drawn from an even sample of "
                         f"{MAX_VIOLIN_POINTS} of its values; the box inside it uses every value")
        extra = CS.ChartExtra(box=boxes, violin=dists)
    prov.agg = "none"
    return ComputedChart(categories=[bx.name for bx in boxes], series=[CS.Series(name="Median", values=[bx.median for bx in boxes])],
                         extra=extra, provenance=prov, notes=notes,
                         x_label=frame.names.get(split, "") if split else "", y_label=frame.names["y0"])


def _compute_gantt(chart: CS.Chart, frame: _Frame, prov: CS.Provenance) -> ComputedChart:
    b = chart.data
    assert b is not None
    for role in ("label", "start", "end"):
        if role not in frame.cols:
            raise ChartDataError("A timeline needs a task column, a start date column and an end date column.")
    notes: List[str] = []
    spans: List[Tuple[_dt.date, _dt.date, str]] = []
    bad = 0
    for lab, s, e in zip(frame.cols["label"], frame.cols["start"], frame.cols["end"]):
        so = b.date_order if b.date_order in ("dmy", "mdy") else (frame.kinds["start"].date_order or "dmy")
        eo = b.date_order if b.date_order in ("dmy", "mdy") else (frame.kinds["end"].date_order or "dmy")
        ds, de = to_date(s, so), to_date(e, eo)
        if ds is None or de is None or de < ds:
            bad += 1
            continue
        spans.append((ds, de, _label_of(lab)))
    if bad:
        notes.append(f"{bad} rows without a valid start and end date were left out")
    if not spans:
        raise ChartDataError("No row has a valid start and end date.")
    spans.sort(key=lambda x: (x[0], x[1]))
    if len(spans) > MAX_GANTT_ROWS:
        notes.append(f"{len(spans)} tasks are too many to draw; the first {MAX_GANTT_ROWS} by start date are shown")
        spans = spans[:MAX_GANTT_ROWS]
    extra = CS.ChartExtra(spans=[CS.Span(label=l[:120], start=s.isoformat(), end=e.isoformat(), days=float((e - s).days)) for s, e, l in spans])
    prov.agg = "none"
    return ComputedChart(categories=[l for _, _, l in spans], series=[CS.Series(name="Duration (days)", values=[float((e - s).days) for s, e, _ in spans])],
                         extra=extra, provenance=prov, notes=notes, x_label="Date", y_label=frame.names["label"])


def _compute_pareto(chart: CS.Chart, frame: _Frame, prov: CS.Provenance, deadline: Optional[float]) -> ComputedChart:
    """Bars from largest to smallest, plus the running share of the total.

    A pareto ASSERTS an order: the reader stops where the line crosses 80%
    and calls everything left of it the vital few. Drawn in any other order
    that sentence is false, so the descending sort is not a default here —
    it is forced, and a binding that asked for another order is told so.
    The cumulative percentage is of the bars that are DRAWN (the "Other"
    bucket is one of them), so the line always ends at 100%.
    """
    b = chart.data
    assert b is not None
    notes: List[str] = []
    if b.sort not in ("auto", "value_desc"):
        notes.append("a pareto chart is read from the largest bar down, so the categories were sorted by value")
    inner = chart.model_copy(update={"data": b.model_copy(update={"sort": "value_desc"})})
    result = _compute_grouped(inner, frame, prov, deadline)
    if not result.series:
        raise ChartDataError("There is nothing to rank in this table.")
    values = list(result.series[0].values)
    total = sum(values)
    if total <= 0:
        raise ChartDataError("A pareto chart needs a positive total; every value here is zero.")
    running = 0.0
    cumulative: List[float] = []
    for v in values:
        running += v
        cumulative.append(running / total * 100.0)
    result.series = [
        result.series[0].model_copy(update={"kind": "bar"}),
        CS.Series(name="Cumulative %", values=cumulative, axis="secondary", kind="line"),
    ]
    result.notes = result.notes + notes
    return result


def _compute_candlestick(chart: CS.Chart, frame: _Frame, prov: CS.Provenance) -> ComputedChart:
    """Open-high-low-close per period, aggregated the way a candle is: the
    OPEN of a period is its first row, the CLOSE its last, the HIGH the
    largest high and the LOW the smallest low. Averaging any of the four
    would draw a candle no trade ever made."""
    b = chart.data
    assert b is not None
    if len(b.y) != 4:
        raise ChartDataError(
            "A candlestick chart needs exactly four numeric columns, in the order open, high, low, close"
            f" ({len(b.y)} {'was' if len(b.y) == 1 else 'were'} given)."
        )
    if "x" not in frame.cols:
        raise ChartDataError("A candlestick chart needs a period column (x): the date or label of each candle.")
    keys, klabels, bucket, notes = _keys_for(frame, "x", b, chart.type)
    prov.date_bucket = bucket
    prov.agg = "none"
    ohlc = [_numbers(frame.cols[f"y{i}"]) for i in range(4)]
    order: List[Any] = []
    acc: Dict[Any, List[float]] = {}
    skipped = 0
    for row, key in enumerate(keys):
        o, h, l, c = (col[row] for col in ohlc)
        if o is None or h is None or l is None or c is None:
            skipped += 1
            continue
        if key not in acc:
            acc[key] = [o, h, l, c]
            order.append(key)
        else:
            cell = acc[key]
            cell[1] = max(cell[1], h)
            cell[2] = min(cell[2], l)
            cell[3] = c
    if skipped:
        notes.append(f"{skipped:,} rows without all four numbers were left out")
    if not order:
        raise ChartDataError("No row has an open, a high, a low and a close.")
    order.sort(key=lambda k: (k[0] == "~blank", k))
    wrong = [klabels[k] for k in order if acc[k][1] < max(acc[k][0], acc[k][3]) or acc[k][2] > min(acc[k][0], acc[k][3])]
    if wrong:
        # high < max(open, close) is not a market that happened; it is the
        # four columns bound in the wrong order, which a silent chart would
        # draw as upside-down wicks.
        raise ChartDataError(
            f"{len(wrong)} period{'s' if len(wrong) != 1 else ''} ({', '.join(wrong[:3])}) "
            "have a high below, or a low above, the open and close: the four columns look out of order — "
            "bind them as open, high, low, close."
        )
    if len(order) > MAX_CANDLES:
        notes.append(f"{len(order):,} periods are too many to draw; the last {MAX_CANDLES} are shown")
        order = order[-MAX_CANDLES:]
    candles = [CS.Candle(label=klabels[k][:80], open=acc[k][0], high=acc[k][1], low=acc[k][2], close=acc[k][3]) for k in order]
    names = [frame.names[f"y{i}"] for i in range(4)]
    series = [CS.Series(name=_clip_name(names[i]), values=[getattr(cd, f) for cd in candles])
              for i, f in enumerate(("open", "high", "low", "close"))]
    x_label = frame.names.get("x", "")
    if bucket:
        x_label = f"{x_label} ({bucket})"
    return ComputedChart(categories=[cd.label for cd in candles], series=series, extra=CS.ChartExtra(candles=candles),
                         provenance=prov, notes=notes, x_label=x_label, y_label="")


def _clip_name(name: str) -> str:
    return (str(name) or "Series")[:80]


def _compute_bullet(chart: CS.Chart, frame: _Frame, prov: CS.Provenance, deadline: Optional[float]) -> ComputedChart:
    """One actual against one target per label, with the qualitative bands
    the table supplied.

    NO BAND IS INVENTED. The usual bullet chart shades "poor / fair / good"
    behind the measure; those thresholds are a judgement, not a measurement,
    so they are drawn only from columns the binding names (y[1:]) and are
    absent otherwise. A band the code made up (60% and 80% of target, say)
    would read as the organisation's own thresholds.
    """
    b = chart.data
    assert b is not None
    if not b.y:
        raise ChartDataError("A bullet chart needs the actual value column (y).")
    if "target" not in frame.cols:
        raise ChartDataError("A bullet chart needs the target column it compares the actual with (target).")
    if "x" not in frame.cols:
        raise ChartDataError("A bullet chart needs a label column (x).")
    notes: List[str] = []
    agg = b.agg if b.agg != "count" else "sum"
    keys, klabels, bucket, knotes = _keys_for(frame, "x", b, chart.type)
    notes.extend(knotes)
    prov.agg = agg
    prov.date_bucket = bucket
    band_roles = [f"y{i}" for i in range(1, len(b.y))][:4]
    roles = ["y0", "target"] + band_roles
    for role in roles:
        if frame.kinds[role].kind not in ("number", "empty"):
            raise ChartDataError(f"The column {frame.names[role]!r} is not numeric, so it cannot be a bullet chart's measure.")
    ycols = [_numbers(frame.cols[r]) for r in roles]
    _check_deadline(deadline)
    cells, engine = _aggregate(keys, [("all",)] * len(keys), ycols, agg)
    prov.engine = engine  # type: ignore[assignment]
    first_seen: Dict[Any, int] = {}
    for i, k in enumerate(keys):
        first_seen.setdefault(k, i)
    order = list(first_seen)
    if b.sort == "value_desc":
        order.sort(key=lambda k: (-cells.get((k, ("all",)), [0.0])[0], first_seen[k]))
    elif b.sort == "value_asc":
        order.sort(key=lambda k: (cells.get((k, ("all",)), [0.0])[0], first_seen[k]))
    elif b.sort == "x":
        order.sort(key=lambda k: (k[0] == "~blank", k))
    if len(order) > MAX_BULLETS:
        notes.append(f"{len(order)} rows are too many for one bullet chart; the first {MAX_BULLETS} are shown")
        order = order[:MAX_BULLETS]
    bullets: List[CS.Bullet] = []
    for k in order:
        vals = cells.get((k, ("all",)), [0.0] * len(roles))
        bands = sorted(vals[2:2 + len(band_roles)])
        bullets.append(CS.Bullet(label=klabels[k][:80], actual=vals[0], target=vals[1], bands=bands))
    if not any(bl.target for bl in bullets):
        raise ChartDataError(f"Every {frame.names['target']} is zero, so there is no target to measure against.")
    if band_roles:
        notes.append("the shaded bands are " + ", ".join(frame.names[r] for r in band_roles))
    series = [CS.Series(name=_clip_name(frame.names["y0"]), values=[bl.actual for bl in bullets]),
              CS.Series(name=_clip_name(frame.names["target"]), values=[bl.target for bl in bullets])]
    y_label = frame.names["y0"] if agg in ("sum", "none") else f"{CS.AGG_LABELS[agg]} {frame.names['y0']}"
    return ComputedChart(categories=[bl.label for bl in bullets], series=series, extra=CS.ChartExtra(bullets=bullets),
                         provenance=prov, notes=notes, x_label=frame.names.get("x", ""), y_label=y_label[:60])


_PROVENANCE_PHRASE = {
    "answer": "from the assistant's earlier answer",
    "prompt": "figures typed in the request",
    "paste": "pasted table",
}


def _caption(chart: CS.Chart, result: ComputedChart, frame: _Frame) -> str:
    b = chart.data
    assert b is not None
    prov = result.provenance
    t = chart.type
    parts: List[str] = []
    if t in CS.XY_TYPES:
        ys = " and ".join(frame.names[f"y{i}"] for i in range(len(b.y)))
        parts.append(f"{ys} against {frame.names.get('x', '')}")
    elif t == "histogram":
        role = "y0" if b.y else "x"
        parts.append(f"Distribution of {frame.names.get(role, '')} ({len(result.categories)} bins)")
    elif t in ("box", "violin"):
        split = frame.names.get("group_by") or frame.names.get("x")
        parts.append(f"Distribution of {frame.names.get('y0', '')}" + (f" by {split}" if split else ""))
    elif t == "candlestick":
        cols = ", ".join(frame.names[f"y{i}"] for i in range(min(4, len(b.y))))
        parts.append(f"{cols} per {prov.date_bucket or frame.names.get('x', '')} ({len(result.categories)} periods)")
    elif t == "bullet":
        parts.append(f"{frame.names.get('y0', '')} against {frame.names.get('target', '')} by {frame.names.get('x', '')}")
    elif t == "gantt":
        parts.append(f"{frame.names['label']} from {frame.names['start']} to {frame.names['end']}")
    else:
        by = frame.names.get("x", "")
        if prov.date_bucket:
            by = prov.date_bucket
        if b.group_by and t in CS.SPLIT_TYPES:
            by = f"{by} and {frame.names.get('group_by', b.group_by)}"
        if prov.agg == "count":
            parts.append(f"Count of rows by {by}")
        else:
            ys = [frame.names[f"y{i}"] for i in range(len(b.y))] + [frame.names[f"y2_{i}"] for i in range(len(b.y2))]
            if b.group_by and t in CS.SPLIT_TYPES:
                ys = ys[:1]
            label = CS.AGG_LABELS[prov.agg] if prov.agg != "none" else "Value"
            parts.append(f"{label} of {' and '.join(ys)} by {by}")
    if t == "pareto" and result.series and len(result.series) > 1 and result.series[1].values:
        crossing = next((i for i, v in enumerate(result.series[1].values) if v >= 80.0), None)
        if crossing is not None:
            n = crossing + 1
            parts.append(f"the top {n} of {len(result.categories)} make up "
                         f"{result.series[1].values[crossing]:.0f}% of the total")
    parts.append(prov.table_title or prov.table_id)
    parts.append(f"{prov.rows_used:,} rows")
    if prov.filters:
        parts.append("filtered: " + "; ".join(prov.filters))
    if prov.sampled:
        parts.append(f"first {DUCKDB_ROW_CAP:,} of {prov.rows_total:,} rows")
    phrase = _PROVENANCE_PHRASE.get(prov.table_provenance)
    if phrase:
        parts.append(phrase)
    auto = " · ".join(p for p in parts if p)
    given = (chart.caption or "").strip()
    if given:
        if phrase and prov.table_provenance == "answer" and phrase not in given:
            given = f"{given} ({phrase})"
        return given[:300]
    return auto[:300]


# --------------------------------------------------------------- the spec --


def _chart_of(value: Any) -> CS.Chart:
    if isinstance(value, CS.Chart):
        return value
    if hasattr(value, "model_dump"):
        return CS.Chart.model_validate(value.model_dump())
    return CS.Chart.model_validate(value)


def _find_table(table_id: str, tables: Sequence[Any], default: Any = None) -> Tuple[Any, str]:
    if not table_id:
        if default is not None:
            return default, ""
        if len(tables) == 1:
            return tables[0], ""
        return None, ""
    for t in tables:
        if str(_table_attr(t, "id", "")) == table_id:
            return t, ""
    want = table_id.strip().casefold()
    for t in tables:
        if str(_table_attr(t, "id", "")).casefold() == want or str(_table_attr(t, "title", "")).strip().casefold() == want:
            return t, ""
    ids = [str(_table_attr(t, "id", "")) for t in tables]
    close = difflib.get_close_matches(table_id, ids, n=1, cutoff=0.85)
    if close:
        return tables[ids.index(close[0])], f"the table {table_id!r} was read as {close[0]!r}"
    return None, ""


def resolve_chart(chart: Any, tables: Sequence[Any], *, default_table: Any = None, allow_literal: bool = False,
                  deadline: Optional[float] = None) -> Tuple[Optional[CS.Chart], List[str], str]:
    """(chart or None, notes, message). None means "draw `message` instead"."""
    c = _chart_of(chart)
    if c.data is None:
        if allow_literal:
            return c, [], ""
        name = c.title or "The chart"
        return None, [f"{name} was not drawn: its numbers were not bound to a table"], (
            f"{name} was not drawn because its numbers did not come from a table. Give the figures as a table (or upload the file) and ask again."
        )
    table, note = _find_table(c.data.table_id, tables, default_table)
    if table is None:
        what = c.data.table_id or "no table"
        return None, [f"{c.title or 'a chart'}: the table {what!r} is not available"], f"The chart could not be drawn: the table {what!r} is not available."
    try:
        result = compute(c, table, deadline=deadline)
    except ChartDataError as exc:
        return None, [str(exc)], str(exc)
    update: Dict[str, Any] = {
        "categories": result.categories, "series": result.series, "extra": result.extra,
        "provenance": result.provenance, "caption": result.caption,
    }
    if not c.x_label and result.x_label and c.type not in CS.NO_AXIS_TYPES:
        update["x_label"] = result.x_label[:60]
    if not c.y_label and result.y_label and c.type not in CS.NO_AXIS_TYPES:
        update["y_label"] = result.y_label[:60]
    colour_notes: List[str] = []
    if c.style is not None and (c.style.category_colors or c.style.series_colors):
        names = list(result.categories) + [ex.name for ex in ((result.extra.box if result.extra else None) or [])] + [sp.label for sp in ((result.extra.spans if result.extra else None) or [])]
        cat_map, miss_c = _align_colour_keys(c.style.category_colors, names)
        ser_map, miss_s = _align_colour_keys(c.style.series_colors, [s.name for s in result.series] + ["Increase", "Decrease", "Total"])
        update["style"] = c.style.model_copy(update={"category_colors": cat_map, "series_colors": ser_map})
        if miss_c or miss_s:
            colour_notes.append(f"no category or series is called {', '.join(repr(m) for m in (miss_c + miss_s)[:3])}, so that colour was not used")
    resolved = CS.Chart.model_validate({**c.model_dump(), **{k: (v.model_dump() if hasattr(v, "model_dump") else ([s.model_dump() for s in v] if k == "series" else v)) for k, v in update.items()}})
    notes = ([note] if note else []) + result.notes + colour_notes
    return resolved, notes, ""


def _align_colour_keys(mapping: Dict[str, str], names: Sequence[str]) -> Tuple[Dict[str, str], List[str]]:
    """Colour keys the model wrote ("open", "north ") → the exact computed
    names ("Open", "North"): case- and space-insensitive, so a requested
    colour is not silently lost in one renderer and applied in another."""
    exact = set(names)
    folded = {}
    for n in names:
        folded.setdefault(_fold(n), n)
    out: Dict[str, str] = {}
    missing: List[str] = []
    for key, colour in (mapping or {}).items():
        if key in exact:
            out[key] = colour
        elif _fold(key) in folded:
            out[folded[_fold(key)]] = colour
        else:
            out[key] = colour
            missing.append(key)
    return out, missing


_GROUP_TYPES = ("stacked_bar", "stacked_horizontal_bar", "percent_stacked_bar", "stacked_area", "heatmap", "sunburst")
_SECOND_DIMENSION_TYPES = ("line", "area", "bar", "horizontal_bar", "radar")


def _mentions(text: str, name: str) -> bool:
    if not text or not name:
        return False
    return re.search(rf"(?<![\w]){re.escape(name.casefold())}(?![\w])", text.casefold()) is not None


def repair_binding(chart: CS.Chart, tables: Sequence[Any], instruction: str = "") -> Tuple[CS.Chart, List[str]]:
    """Deterministic clean-up of a binding the model wrote, BEFORE compute:

    - fields that belong to other chart types are dropped (y2 off a combo,
      label/start/end off a gantt, size off a bubble, trendline off a
      scatter); a y2 on a non-combo chart with no y becomes its y;
    - a grouped type (stacked, heatmap) with no group_by, or a line/bar over
      dates whose request names a second text column, gets that column as
      group_by — only when exactly one such column is named in the request
      or the title, with a note;
    - the TYPE is checked against the table's shape by chart_choice: a type
      the person named in `instruction` wins whenever the shape carries it,
      otherwise the chooser's type is used and the note names both and why.

    Live run 2026-09-15: 7 of 20 misses were a skipped group_by and 3 were
    stray y2/label/size fields. Nothing here invents a column or a number."""
    if chart.data is None:
        return chart, []
    b = chart.data
    t = chart.type
    notes: List[str] = []
    upd: Dict[str, Any] = {}
    if t != "combo" and b.y2:
        if not b.y:
            upd["y"] = list(b.y2)
        upd["y2"] = []
    if t != "gantt" and (b.label or b.start or b.end):
        upd.update(label=None, start=None, end=None)
    if t != "bubble" and b.size:
        upd["size"] = None
    if t != "bullet" and b.target:
        upd["target"] = None
    if t != "scatter" and b.trendline:
        upd["trendline"] = False
    table, _ = _find_table(b.table_id, list(tables))
    if table is not None and not b.group_by and t in _GROUP_TYPES and len(b.y) == 1:
        columns = [str(c) for c in (_table_attr(table, "columns", []) or [])]
        yi, _ = match_column(b.y[0], columns)
        if yi is not None and infer_column(table, yi).kind == "text":
            # A text column named as the value of a grouped chart is its
            # second dimension: count the rows per (x, that column).
            upd.update(group_by=columns[yi], y=[], agg="count")
            notes.append(f"{columns[yi]} is text, so the chart counts rows by {b.x} and {columns[yi]}")
    if table is not None and not (upd.get("group_by") or b.group_by) and (t in _GROUP_TYPES or t in _SECOND_DIMENSION_TYPES):
        columns = [str(c) for c in (_table_attr(table, "columns", []) or [])]
        x_idx, _ = match_column(b.x, columns)
        used = {x_idx} | {match_column(y, columns)[0] for y in list(upd.get("y", b.y))}
        text = f"{instruction} {chart.title}"
        candidates = []
        for i, name in enumerate(columns):
            if i in used or not _mentions(text, name):
                continue
            info = infer_column(table, i)
            if info.kind == "text" and 1 < info.n_distinct <= 12:
                candidates.append(name)
        x_is_date = x_idx is not None and infer_column(table, x_idx).kind == "date"
        if len(candidates) == 1 and (t in _GROUP_TYPES or x_is_date):
            upd["group_by"] = candidates[0]
            notes.append(f"the chart is split by {candidates[0]}, as the request names it")

    # The type, LAST: the chooser reads the binding as repaired above (a
    # group_by just filled in changes the shape), and the person's own words
    # outrank both the model and the chooser.
    type_upd: Dict[str, Any] = {}
    if table is not None:
        probe = chart if not upd else chart.model_copy(update={"data": CS.Binding.model_validate({**b.model_dump(), **upd})})
        choice = chart_choice.choose(probe, table, instruction)
        if choice is not None:
            if choice.type != t:
                type_upd["type"] = choice.type
            if choice.trendline != probe.data.trendline and choice.type == "scatter":
                upd["trendline"] = choice.trendline
            if choice.note:
                notes.append(choice.note)
    if not upd and not type_upd:
        return chart, notes
    data = {**b.model_dump(), **upd}
    return chart.model_copy(update={"data": CS.Binding.model_validate(data), **type_upd,
                                    "categories": [], "series": [], "extra": None, "provenance": None}), notes


def _sheet_table(sheet: Dict[str, Any]) -> Any:
    name = str(sheet.get("name") or "Sheet")
    cols = [str(c.get("name") if isinstance(c, dict) else c) for c in (sheet.get("columns") or [])]
    return _make_table(id=f"sheet:{name}", title=name, columns=cols, rows=list(sheet.get("rows") or []))


def resolve_spec(spec: Any, tables: Sequence[Any], *, allow_literal: bool = False, timeout_s: Optional[float] = None) -> Tuple[Any, List[str]]:
    """Every chart in `spec` resolved against `tables`. Returns (spec, notes).

    SYNCHRONOUS AND CPU-BOUND: from async code call resolve_spec_async (a
    worker thread with a deadline). `spec` may be a pydantic spec (envelope
    or body) or its dict; the same type comes back. A chart that cannot be
    computed is replaced: a document block by a note callout, a slide chart
    by a bullet saying why, a sheet chart is dropped — each with a note."""
    deadline = time.monotonic() + timeout_s if timeout_s else None
    model_cls = type(spec) if hasattr(spec, "model_dump") else None
    data = spec.model_dump(mode="python") if model_cls else _deep_copy(spec)
    body = _dict_body(data)
    notes: List[str] = []
    tables = list(tables or [])
    changed = False

    blocks = body.get("blocks")
    if isinstance(blocks, list):
        for i, blk in enumerate(list(blocks)):
            if isinstance(blk, dict) and blk.get("type") == "chart" and blk.get("chart") is not None:
                chart, n, msg = _resolve_guarded(blk["chart"], tables, None, allow_literal, deadline)
                notes.extend(n)
                changed = True
                if chart is None:
                    blocks[i] = {"type": "callout", "kind": "note", "title": (_title_of(blk["chart"]) or "Chart not drawn")[:120], "text": msg[:2000]}
                else:
                    blocks[i] = {**blk, "chart": chart.model_dump()}
    slides = body.get("slides")
    if isinstance(slides, list):
        for s in slides:
            if isinstance(s, dict) and s.get("chart") is not None:
                chart, n, msg = _resolve_guarded(s["chart"], tables, None, allow_literal, deadline)
                notes.extend(n)
                changed = True
                if chart is None:
                    s["chart"] = None
                    if s.get("layout") == "chart":
                        s["layout"] = "bullets"
                    s["bullets"] = (list(s.get("bullets") or []) + [msg[:220]])[:8]
                else:
                    s["chart"] = chart.model_dump()
    sheets = body.get("sheets")
    if isinstance(sheets, list):
        for sh in sheets:
            if not isinstance(sh, dict) or not sh.get("charts"):
                continue
            own = _sheet_table(sh)
            kept = []
            for ch in sh["charts"]:
                chart, n, msg = _resolve_guarded(ch, tables + [own], own, allow_literal, deadline)
                notes.extend(n)
                changed = True
                if chart is not None:
                    kept.append(chart.model_dump())
            sh["charts"] = kept
    if not changed or model_cls is None:
        return (spec if not changed else data), notes
    try:
        return model_cls.model_validate(data), notes
    except Exception:
        # Before the spec.Chart → chart_spec.Chart swap lands, the spec
        # model only knows the legacy chart shape: keep what it can carry.
        legacy_notes = _downgrade_to_legacy(body)
        return model_cls.model_validate(data), notes + legacy_notes


def _resolve_guarded(chart: Any, tables: Sequence[Any], default: Any, allow_literal: bool, deadline: Optional[float]) -> Tuple[Optional[CS.Chart], List[str], str]:
    try:
        _check_deadline(deadline)
        return resolve_chart(chart, tables, default_table=default, allow_literal=allow_literal, deadline=deadline)
    except ChartDataError as exc:
        return None, [str(exc)], str(exc)
    except Exception as exc:  # a malformed chart dict must not fail the spec
        title = _title_of(chart) or "A chart"
        return None, [f"{title} could not be computed ({type(exc).__name__})"], f"{title} could not be drawn from the data."


def _title_of(chart: Any) -> str:
    if isinstance(chart, dict):
        return str(chart.get("title") or "")
    return str(getattr(chart, "title", "") or "")


_LEGACY_KEYS = ("type", "title", "categories", "series", "y_label", "caption", "sources")


def _legacy_dict(chart: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if chart.get("type") not in CS.LEGACY_TYPES or not chart.get("series") or not chart.get("categories"):
        return None
    if len(chart["series"]) > 8 or all(v == 0 for s in chart["series"] for v in s.get("values", [])):
        return None
    out = {k: chart[k] for k in _LEGACY_KEYS if k in chart}
    out["series"] = [{"name": s["name"], "values": s["values"]} for s in chart["series"]]
    return out


def _downgrade_to_legacy(body: Dict[str, Any]) -> List[str]:
    notes: List[str] = []
    for i, blk in enumerate(body.get("blocks") or []):
        if isinstance(blk, dict) and blk.get("type") == "chart":
            legacy = _legacy_dict(blk["chart"])
            if legacy is None:
                notes.append(f"{blk['chart'].get('title') or 'a chart'}: this chart type is drawn once the chart renderer integration lands")
                body["blocks"][i] = {"type": "callout", "kind": "note", "title": (blk["chart"].get("title") or "Chart")[:120],
                                     "text": "This chart type cannot be drawn in this version yet."}
            else:
                blk["chart"] = legacy
    for s in body.get("slides") or []:
        if isinstance(s, dict) and s.get("chart"):
            legacy = _legacy_dict(s["chart"])
            if legacy is None:
                notes.append(f"{s['chart'].get('title') or 'a chart'}: this chart type is drawn once the chart renderer integration lands")
                s["chart"] = None
                if s.get("layout") == "chart":
                    s["layout"] = "bullets"
                    s["bullets"] = (list(s.get("bullets") or []) + ["This chart type cannot be drawn in this version yet."])[:8]
            else:
                s["chart"] = legacy
    for sh in body.get("sheets") or []:
        if isinstance(sh, dict) and sh.get("charts"):
            kept = []
            for ch in sh["charts"]:
                legacy = _legacy_dict(ch)
                if legacy is not None:
                    kept.append(legacy)
                else:
                    notes.append(f"{ch.get('title') or 'a chart'}: this chart type is drawn once the chart renderer integration lands")
            sh["charts"] = kept
    return notes


def _dict_body(data: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(data, dict):
        kind = data.get("kind")
        if kind in ("document", "presentation", "workbook") and isinstance(data.get(kind), dict):
            return data[kind]
        if isinstance(data.get("body"), dict):
            return data["body"]
    return data


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


async def resolve_spec_async(spec: Any, tables: Sequence[Any], *, allow_literal: bool = False, timeout_s: float = 15.0) -> Tuple[Any, List[str]]:
    """resolve_spec in a worker thread, bounded by `timeout_s`. The thread is
    given the same deadline, so a timed-out computation stops at its next
    check instead of burning a core in the background. asyncio.timeout, not
    wait_for (memory: ci-python311-waitfor-hang)."""
    async with asyncio.timeout(timeout_s + 1.0):
        return await asyncio.to_thread(resolve_spec, spec, tables, allow_literal=allow_literal, timeout_s=timeout_s)


def recompute_matches(chart: Any, tables: Sequence[Any], rel_tol: float = 1e-9, *, default_table: Any = None) -> Tuple[bool, List[str]]:
    """Re-run the computation and compare with the numbers the chart carries
    (for selfcheck). A literal chart cannot be verified: (False, [reason])."""
    c = _chart_of(chart)
    if c.data is None:
        return False, ["the chart has no data binding, so its numbers cannot be recomputed"]
    fresh, notes, msg = resolve_chart(c, tables, default_table=default_table)
    if fresh is None:
        return False, [msg or "the chart could not be recomputed"]
    diffs: List[str] = []
    if list(fresh.categories) != list(c.categories):
        diffs.append(f"categories differ: {c.categories[:5]} vs {fresh.categories[:5]}")
    if len(fresh.series) != len(c.series):
        diffs.append(f"{len(c.series)} series in the chart, {len(fresh.series)} computed")
    for a, b in zip(c.series, fresh.series):
        if a.name != b.name:
            diffs.append(f"series name {a.name!r} vs {b.name!r}")
        for label, xs, ys in (("values", a.values, b.values), ("x", a.x or [], b.x or [])):
            if len(xs) != len(ys):
                diffs.append(f"series {a.name!r} has {len(xs)} {label}, {len(ys)} computed")
                continue
            for i, (u, v) in enumerate(zip(xs, ys)):
                if not math.isclose(u, v, rel_tol=rel_tol, abs_tol=1e-9):
                    diffs.append(f"series {a.name!r} {label}[{i}] is {u!r}; the data gives {v!r}")
                    break
    return (not diffs), diffs[:20]


# ------------------------------------------------------------ typed figures --

_NUM_TOKEN = rf"[-−]?\s*(?:₹|rs\.?\s*|\$|€|£)?\s*(?:{_GROUPED}(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:%|(?:k|lakh|lakhs|lac|lacs|crore|crores|cr|mn|million|bn|billion|thousand|हजार|हज़ार|લાખ|लाख|करोड़|करोड|કરોડ|હજાર)\b\.?|m\b|b\b)?"
_LCHAR = r"[\w&'’/().\u0900-\u097F\u0A80-\u0AFF]"
#: A label is words that each START with a letter ("Team A", "week1", "Q1"),
#: so a number never hides inside a label ("apples 5 and oranges").
#: Bounded (at most 8 words of at most 40 characters): unbounded, every start
#: position in a run of words with no figure after it re-read the rest of
#: the run — 11 s of event loop on a 20 KB prose paste (verifier
#: 2026-09-15). A label is cut to 4 words downstream (_clean_label).
_LABEL = rf"[^\W\d_]{_LCHAR}{{0,40}}(?:[ \-][^\W\d_]{_LCHAR}{{0,40}}){{0,7}}"
_ITEM_RE = re.compile(rf"(?P<label>{_LABEL})(?:\s*(?:[:=]|[–—\-](?=\s))\s*|\s+(?:is|was|hai|che|chhe|છે|है|=)?\s*)(?P<num>{_NUM_TOKEN})(?![\w.])", re.UNICODE | re.IGNORECASE)
_SEP_RE = re.compile(r"^\s*(?:[,;|\n]|\band\b|\baur\b|\bane\b|&|और|અને)?\s*(?:[,;|\n]|\band\b|\baur\b|\bane\b|&|और|અને)?\s*$", re.IGNORECASE)
_PAIR_RE = re.compile(r"\(\s*(-?\d+(?:\.\d+)?)\s*[,;]\s*(-?\d+(?:\.\d+)?)\s*\)")
_LIST_RE = re.compile(r"(?P<name>[^\W\d_][\w ]{0,24}?)\s*(?:=|:|\bis\b|\bhai\b)\s*\[?\s*(?P<vals>[^\s,;\[\]]+(?:\s*[,;]\s*[^\s,;\[\]]+){1,199})\s*\]?", re.IGNORECASE)
_STOP_LABEL_WORDS = {
    "plot", "chart", "graph", "draw", "make", "show", "create", "pie", "bar", "line", "of", "for", "with", "as", "a", "an",
    "the", "data", "values", "figures", "numbers", "is", "are", "me", "please", "pls", "banao", "bana", "do", "karo",
    "chahiye", "and", "aur", "ane", "in", "by", "to", "from", "here", "these", "this", "using", "use",
}
_TRAILING_POSTPOSITIONS = {"me", "mein", "mai", "ma", "maa", "ka", "ki", "ke", "ko", "no", "ni", "nu", "na", "में", "का", "की", "के", "માં", "નો", "ની", "નું", "ના", "in", "of", "for", "at"}
_BY_RE = re.compile(r"\b([A-Za-z][A-Za-z ]{1,20}?)\s+(?:by|per|across|over)\s+([A-Za-z]{2,20})\b", re.IGNORECASE)


_PROSE_LABEL_END = frozenset({"have", "has", "had", "got", "get", "vs", "versus", "v", "and", "or", "at", "around", "about", "till", "until", "version", "ver", "after", "before", "every", "than"})
_PROSE_LABEL_START = frozenset({"vs", "versus", "v", "v/s"})


def _clean_label(label: str, max_words: int) -> str:
    label = label.split(":")[-1]
    words = [w for w in re.split(r"\s+", label.strip()) if w]
    while len(words) > 1 and words[0].casefold().strip("()") in _STOP_LABEL_WORDS:
        words = words[1:]
    while len(words) > 1 and words[-1].casefold() in _TRAILING_POSTPOSITIONS:
        words = words[:-1]
    words = words[-max_words:]
    return " ".join(words).strip(" -–—()").strip()


def _num_value(token: str) -> Tuple[Optional[float], bool]:
    tok = token.strip()
    pct = tok.endswith("%")
    compact = re.sub(r"\s+", "", tok) if not re.search(r"[A-Za-zऀ-૿]{2,}", tok) else re.sub(r"(?<=\d)\s+(?=[A-Za-zऀ-૿])", "", tok)
    return to_number(compact), pct


def parse_prompt_data(text: str, index: int = 1) -> Optional[Any]:
    """Figures a person typed in the request → a DataTable `prompt<index>`, or
    None when the text carries no series of figures. The model then binds a
    chart to that table like any upload; it never retypes the numbers."""
    if not text or not isinstance(text, str):
        return None
    src = text.translate(_INDIC_DIGITS)
    if len(src) > 20_000:
        src = src[:20_000]
    title = "Figures typed in the request"
    tid = f"prompt{int(index)}"

    # 1. (x, y) pairs.
    pairs = _PAIR_RE.findall(src)
    if len(pairs) >= 2:
        return _make_table(tid, title, ["x", "y"], [[float(a), float(b)] for a, b in pairs])

    # 2. named lists: x = 1,2,3 and y = 4,5,6 / months = Jan, Feb and sales = 10, 12
    lists: List[Tuple[str, List[str]]] = []
    for m in _LIST_RE.finditer(src):
        vals = [v.strip() for v in re.split(r"\s*[,;]\s*", m.group("vals")) if v.strip()]
        name = _clean_label(m.group("name"), 3)
        if len(vals) >= 2 and name:
            lists.append((name, vals))
    if len(lists) >= 2:
        lengths = {len(v) for _, v in lists}
        numeric = [(n, v) for n, v in lists if all(to_number(x) is not None for x in v)]
        if len(lengths) == 1 and numeric:
            cols = [n for n, _ in lists]
            rows = []
            for i in range(lists[0][1].__len__()):
                row = []
                for n, v in lists:
                    num = to_number(v[i])
                    row.append(num if (n, v) in numeric else v[i])
                rows.append(row)
            return _make_table(tid, title, _dedupe(cols), rows)

    # 3. label–value runs: "Jan 10, Feb 12", "north 120; south 95", "Rent 40%".
    items = list(_ITEM_RE.finditer(src))
    best: List[re.Match] = []
    run: List[re.Match] = []
    for m in items:
        if run and not _SEP_RE.match(src[run[-1].end():m.start()]):
            if len(run) > len(best):
                best = run
            run = []
        run.append(m)
    if len(run) > len(best):
        best = run
    if len(best) < 2:
        return None
    word_counts = [len(_clean_label(m.group("label"), 4).split()) for m in best[1:]]
    max_words = max(1, max(word_counts) if word_counts else 1)
    for m in best:
        raw_words = [w.casefold() for w in m.group("label").split()]
        if raw_words and (raw_words[-1] in _PROSE_LABEL_END or raw_words[0] in _PROSE_LABEL_START):
            # "I have 3 kids and 2 dogs", "meeting at 10 and lunch at 1",
            # "iPhone 15 vs iPhone 16": counts, times and model numbers in
            # a sentence, not a series of figures.
            return None
    rows = []
    any_pct = False
    for m in best:
        label = _clean_label(m.group("label"), max_words)
        value, pct = _num_value(m.group("num"))
        if not label or value is None:
            return None
        any_pct = any_pct or pct
        rows.append([label, value])
    labels = [r[0] for r in rows]
    if len({l.casefold() for l in labels}) != len(labels):
        return None
    label_col = "Category"
    if _month_or_weekday_order(labels) is not None:
        label_col = "Month" if all(l.casefold().rstrip(".") in _MONTHS for l in labels) else "Day"
    value_col = "Share (%)" if any_pct else "Value"
    by = _BY_RE.search(src[: best[0].start()])
    if by:
        measure = _clean_label(by.group(1), 2)
        dim = by.group(2)
        if measure and measure.casefold() not in _STOP_LABEL_WORDS:
            value_col = measure[:1].upper() + measure[1:] + (" (%)" if any_pct else "")
        if dim and dim.casefold() not in _STOP_LABEL_WORDS and label_col == "Category":
            label_col = dim[:1].upper() + dim[1:]
    return _make_table(tid, title, [label_col, value_col], rows)


def _dedupe(names: List[str]) -> List[str]:
    out: List[str] = []
    for n in names:
        base, i = n, 2
        while n in out:
            n = f"{base} {i}"
            i += 1
        out.append(n)
    return out


# --------------------------------------------------------- markdown tables --

_MD_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def tables_from_markdown(md: str, *, prefix: str = "answer", start: int = 1, title: str = "Table from the assistant's earlier answer") -> List[Any]:
    """GFM pipe tables in `md` → DataTables `<prefix><n>`; cells that are
    numbers become floats (so a chart binds to real numbers)."""
    out: List[Any] = []
    lines = (md or "").splitlines()
    i = 0
    n = start
    while i < len(lines) - 1:
        head, sep = lines[i], lines[i + 1]
        if "|" in head and _MD_SEP_RE.match(sep):
            cols = [c.strip() for c in head.strip().strip("|").split("|")]
            rows = []
            j = i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                cells = (cells + [""] * len(cols))[: len(cols)]
                rows.append([_md_cell(c) for c in cells])
                j += 1
            if cols and rows:
                out.append(_make_table(f"{prefix}{n}", f"{title} ({n})" if n > 1 else title, [re.sub(r"[*_`]", "", c) or f"Column {k + 1}" for k, c in enumerate(cols)], rows))
                n += 1
            i = j
        else:
            i += 1
    return out


def _md_cell(c: str) -> Any:
    c = re.sub(r"^\*\*(.*)\*\*$", r"\1", c.strip())
    num = to_number(c)
    return num if num is not None and re.search(r"\d", c) else c


__all__ = [
    "PANDAS_ROW_CAP", "DUCKDB_ROW_CAP", "ChartDataError", "ComputedChart", "ColumnInfo", "to_number", "to_date",
    "detect_date_order", "infer_column", "describe_table", "match_column", "compute", "resolve_chart", "resolve_spec",
    "resolve_spec_async", "recompute_matches", "parse_prompt_data", "tables_from_markdown", "provenance_of", "repair_binding",
]
