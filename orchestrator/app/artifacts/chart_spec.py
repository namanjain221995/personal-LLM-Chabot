"""Chart v2 — what the model may say about a chart, and what code fills in.

WHY A BINDING AND NOT NUMBERS. A chart in a new spec names a TABLE and the
columns to plot (`data: Binding`); the categories and the series values are
OUTPUT fields that chart_data.resolve_spec computes from that table. The model
never writes a number that ends up on an axis: a literal-series chart in a new
spec is blocked (replaced by a note), and typed figures in the request become
a `prompt<N>` DataTable first (chart_data.parse_prompt_data) so the model can
bind to them like any upload. Literal `categories/series` survive only in old
spec.json files, which still load and render (back-compat with spec.Chart).

A SUPERSET OF spec.Chart. Every field spec.Chart has keeps its name, type and
meaning, so `Chart.model_validate(old_chart.model_dump())` always works and
`to_legacy` gives the old shape back for the four legacy types.

NO IMPORT OF spec.py AT MODULE LEVEL. spec.py will import this module when the
Chart swap lands (one reviewed integration commit); importing spec here would
be a cycle. `from_legacy`/`to_legacy` import it lazily.

THE GUIDED SCHEMA. `guided_schema()` is the JSON schema a constrained decoder
sees: `categories`, `series`, `extra` and `provenance` are removed from it
(they are marked readOnly on the model too), so guided decoding never spends a
token on a number.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterator, List, Literal, Optional, Sequence, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import types as T

# ------------------------------------------------------------- vocabulary --

CHART_TYPES: Tuple[str, ...] = (
    # tier 1 — native in XLSX and PPTX wherever the library allows
    "bar", "horizontal_bar", "stacked_bar", "stacked_horizontal_bar", "percent_stacked_bar",
    "line", "area", "stacked_area", "pie", "donut", "scatter", "histogram", "combo",
    # tier 2 — images where a container has no native form
    "box", "heatmap", "waterfall", "funnel", "gantt", "radar", "bubble",
)
TIER1_TYPES: Tuple[str, ...] = CHART_TYPES[:13]
TIER2_TYPES: Tuple[str, ...] = CHART_TYPES[13:]
LEGACY_TYPES: Tuple[str, ...] = ("bar", "horizontal_bar", "line", "pie")

ChartType = Literal[
    "bar", "horizontal_bar", "stacked_bar", "stacked_horizontal_bar", "percent_stacked_bar",
    "line", "area", "stacked_area", "pie", "donut", "scatter", "histogram", "combo",
    "box", "heatmap", "waterfall", "funnel", "gantt", "radar", "bubble",
]

#: Types whose categories come from grouping rows by `x` and aggregating `y`.
AGGREGATING_TYPES: Tuple[str, ...] = (
    "bar", "horizontal_bar", "stacked_bar", "stacked_horizontal_bar", "percent_stacked_bar",
    "line", "area", "stacked_area", "pie", "donut", "combo", "heatmap", "waterfall", "funnel", "radar",
)
BAR_FAMILY: Tuple[str, ...] = ("bar", "horizontal_bar", "stacked_bar", "stacked_horizontal_bar", "percent_stacked_bar")
STACKED_TYPES: Tuple[str, ...] = ("stacked_bar", "stacked_horizontal_bar", "percent_stacked_bar", "stacked_area")
PART_OF_WHOLE_TYPES: Tuple[str, ...] = ("pie", "donut")
XY_TYPES: Tuple[str, ...] = ("scatter", "bubble")

Agg = Literal["sum", "avg", "count", "min", "max", "median", "none"]
AGGS: Tuple[str, ...] = ("sum", "avg", "count", "min", "max", "median", "none")
AGG_LABELS: Dict[str, str] = {
    "sum": "Sum", "avg": "Average", "count": "Count", "min": "Minimum", "max": "Maximum",
    "median": "Median", "none": "Value",
}
DateBucket = Literal["day", "week", "month", "quarter", "year"]
DATE_BUCKETS: Tuple[str, ...] = ("day", "week", "month", "quarter", "year")
DateOrder = Literal["auto", "dmy", "mdy", "ymd"]

NumberFormat = Literal[
    "integer", "decimal1", "decimal2", "percent",
    "currency_INR", "currency_USD", "currency_EUR", "currency_GBP", "compact",
]
NUMBER_FORMATS: Tuple[str, ...] = (
    "integer", "decimal1", "decimal2", "percent",
    "currency_INR", "currency_USD", "currency_EUR", "currency_GBP", "compact",
)
_NUMBER_FORMAT_ALIASES = {
    "inr": "currency_INR", "usd": "currency_USD", "eur": "currency_EUR", "gbp": "currency_GBP",
    "rupee": "currency_INR", "rupees": "currency_INR", "dollar": "currency_USD", "euro": "currency_EUR",
    "pound": "currency_GBP", "percentage": "percent", "pct": "percent", "int": "integer",
    "decimal": "decimal1",
}

LegendPosition = Literal["bottom", "top", "left", "right", "none"]

#: The default series order from the style guide (§1): reordered for colour
#: vision deficiency — the first five are pairwise >= 25 CIELAB apart under
#: simulated deuteranopia (tests/test_artifact_render_charts.py measures it).
DEFAULT_PALETTE: Tuple[str, ...] = ("#2F6FB2", "#E07B00", "#0E9D9A", "#C0566B", "#6D5AE6", "#8A5A44", "#3F8F4F", "#5F6B7A")

#: Fonts a chart may ask for, until style.FONT_ALLOWLIST lands. Keys are
#: case-folded; the value is the display name matplotlib and Office get.
_LOCAL_FONT_ALLOWLIST: Dict[str, str] = {
    name.casefold(): name for name in (
        "Calibri", "Carlito", "Cambria", "Caladea", "Arial", "Helvetica", "Liberation Sans", "Times New Roman",
        "Liberation Serif", "Georgia", "Verdana", "Segoe UI", "Roboto", "Open Sans", "Lato", "Noto Sans",
        "Noto Serif", "DejaVu Sans", "DejaVu Serif", "Courier New", "Consolas", "Garamond", "Tahoma",
        "Trebuchet MS", "Aptos", "Inter", "Source Sans Pro", "Nirmala UI", "Lohit Devanagari", "Lohit Gujarati",
        "Noto Sans Devanagari", "Noto Sans Gujarati", "Mangal", "Shruti",
    )
}

#: Named colours until style.COLOR_NAMES lands — the style guide's values.
_LOCAL_COLOR_NAMES: Dict[str, str] = {
    "dark blue": "#1F3864", "navy": "#1F3864", "navy blue": "#1F3864", "blue": "#2F6FB2", "light blue": "#DCE6F2",
    "dark green": "#1E6B34", "green": "#3F8F4F", "light green": "#E3F2E6", "red": "#C62828", "dark red": "#9B1C1C",
    "light red": "#FDE4E4", "orange": "#E07B00", "amber": "#B7791F", "yellow": "#FFD54F", "light yellow": "#FFF1C7",
    "purple": "#6D5AE6", "violet": "#6D5AE6", "pink": "#D63384", "grey": "#6B7280", "gray": "#6B7280",
    "light grey": "#EEF0F3", "light gray": "#EEF0F3", "black": "#000000", "white": "#FFFFFF", "brown": "#8A5A44",
    "teal": "#0E9D9A", "gold": "#B7791F", "maroon": "#7B1E1E",
    # Hinglish / Hindi / Gujarati
    "neela": "#2F6FB2", "nila": "#2F6FB2", "नीला": "#2F6FB2", "વાદળી": "#2F6FB2", "gehra neela": "#1F3864",
    "गहरा नीला": "#1F3864", "ઘેરો વાદળી": "#1F3864", "hara": "#3F8F4F", "हरा": "#3F8F4F", "લીલો": "#3F8F4F",
    "lal": "#C62828", "laal": "#C62828", "लाल": "#C62828", "લાલ": "#C62828", "peela": "#FFD54F", "पीला": "#FFD54F",
    "પીળો": "#FFD54F", "narangi": "#E07B00", "नारंगी": "#E07B00", "નારંગી": "#E07B00", "kala": "#000000",
    "काला": "#000000", "કાળો": "#000000", "safed": "#FFFFFF", "सफ़ेद": "#FFFFFF", "सफेद": "#FFFFFF", "સફેદ": "#FFFFFF",
    "gulabi": "#D63384", "गुलाबी": "#D63384", "ગુલાબી": "#D63384", "baingani": "#6D5AE6", "बैंगनी": "#6D5AE6",
    "જાંબલી": "#6D5AE6", "bhura": "#8A5A44", "भूरा": "#8A5A44", "ભૂરો": "#8A5A44", "sleti": "#6B7280",
    "स्लेटी": "#6B7280", "રાખોડી": "#6B7280",
}

_HEX_RE = re.compile(r"^(?:#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})|([0-9a-fA-F]{6}))$")
_TABLE_ID_RE = re.compile(r"^[A-Za-z0-9_.:\- ]{1,80}$")
_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def resolve_color(value: Any) -> Optional[str]:
    """A hex code (#abc, abc, #aabbcc) or a colour name in EN/Hinglish/hi/gu
    → '#RRGGBB'; None when it is neither. style.resolve_color wins when the
    styling track is merged, so both tracks agree on every name."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.strip().split())
    if not text:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{3}", text):
        # AS3 integration: a bare three-letter word ("bed", "add", "bad") is
        # not a colour in a chart style; a short hex needs its '#'.
        return _LOCAL_COLOR_NAMES.get(text.casefold())
    try:  # pragma: no cover - exercised once styling merges
        from . import style as _style  # type: ignore

        got = _style.resolve_color(text)
        if got:
            return str(got).upper() if str(got).startswith("#") else "#" + str(got).upper()
    except Exception:
        pass
    m = _HEX_RE.match(text)
    if m:
        h = m.group(1) or m.group(2)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return "#" + h.upper()
    return _LOCAL_COLOR_NAMES.get(text.casefold())


def allowed_font(name: Any) -> Optional[str]:
    """The canonical font name when `name` is on the allowlist, else None."""
    if not isinstance(name, str) or not name.strip():
        return None
    key = " ".join(name.split()).casefold()
    try:  # pragma: no cover - exercised once styling merges
        from . import style as _style  # type: ignore

        allow = getattr(_style, "FONT_ALLOWLIST", None)
        if allow:
            # AS3 integration: the styling track keys the allowlist by the
            # casefolded name; the canonical spelling is the FontFace's name.
            for k, face in allow.items() if isinstance(allow, dict) else ((a, None) for a in allow):
                if str(k).casefold() == key:
                    return str(getattr(face, "name", None) or k)
            return None
    except Exception:
        pass
    return _LOCAL_FONT_ALLOWLIST.get(key)


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return _XML_ILLEGAL.sub("", value)
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


_TYPE_ALIASES = {
    "column": "bar", "col": "bar", "barh": "horizontal_bar", "hbar": "horizontal_bar",
    "stacked": "stacked_bar", "stacked_column": "stacked_bar", "doughnut": "donut",
    "scatter_plot": "scatter", "hist": "histogram", "dual_axis": "combo", "bar_line": "combo",
    "box_plot": "box", "boxplot": "box", "timeline": "gantt", "spider": "radar",
    "line_chart": "line", "area_chart": "area", "pie_chart": "pie", "bar_chart": "bar",
    "percent_stacked": "percent_stacked_bar", "100_stacked_bar": "percent_stacked_bar",
}


def chart_type_alias(v: Any) -> Any:
    if isinstance(v, str):
        k = v.strip().lower().replace("-", "_").replace(" ", "_")
        return _TYPE_ALIASES.get(k, k)
    return v


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def _no_illegal_characters(cls, data: Any) -> Any:
        return _scrub(data) if isinstance(data, (dict, list)) else data


_READ_ONLY = {"readOnly": True}

# ---------------------------------------------------------------- binding --


class Filter(_Strict):
    """One row filter, applied by code before any aggregation."""

    column: str = Field(min_length=1, max_length=80)
    op: Literal["eq", "ne", "in", "not_in", "contains", "gt", "gte", "lt", "lte", "between", "blank", "not_blank"] = "eq"
    value: Union[str, float, int, bool, None, List[Union[str, float, int]]] = None

    @field_validator("value")
    @classmethod
    def _bounded(cls, v: Any) -> Any:
        def one(x: Any) -> Any:
            if isinstance(x, bool) or x is None:
                return x
            if isinstance(x, (int, float)):
                if not math.isfinite(float(x)):
                    raise ValueError("a filter number must be finite")
                return x
            if isinstance(x, str):
                if len(x) > 100:
                    raise ValueError("a filter value is at most 100 characters")
                return x
            raise ValueError("a filter value is text or a number")

        if isinstance(v, list):
            if len(v) > 20:
                raise ValueError("a filter list has at most 20 values")
            return [one(x) for x in v]
        return one(v)


class Binding(_Strict):
    """Which table and which columns a chart is drawn from. Column names are
    matched case- and space-insensitively, then fuzzily (with a note)."""

    table_id: str = Field(default="", max_length=80, description="The id of a table listed in the material (upload1, paste1, prompt1, answer1, or a sheet name). Empty on a sheet chart means the sheet's own rows.")
    x: Optional[str] = Field(default=None, max_length=80, description="The category / time column (or the numeric x of a scatter).")
    y: List[str] = Field(default_factory=list, max_length=8, description="Numeric columns to plot. Leave empty with agg='count' to count rows.")
    agg: Agg = Field(default="sum", description="How rows that share an x are combined.")
    group_by: Optional[str] = Field(default=None, max_length=80, description="Split into one series per value of this column (stacked bar by region, heatmap rows).")
    date_bucket: Optional[DateBucket] = None
    date_order: DateOrder = "auto"
    bins: Optional[int] = Field(default=None, ge=2, le=100)
    filters: List[Filter] = Field(default_factory=list, max_length=5)
    sort: Literal["auto", "none", "x", "value_desc", "value_asc"] = "auto"
    top_n: Optional[int] = Field(default=None, ge=1, le=50)
    other_bucket: bool = True
    y2: List[str] = Field(default_factory=list, max_length=4, description="Columns drawn as lines on a secondary axis (combo / dual axis).")
    start: Optional[str] = Field(default=None, max_length=80, description="Gantt: start date column.")
    end: Optional[str] = Field(default=None, max_length=80, description="Gantt: end date column.")
    label: Optional[str] = Field(default=None, max_length=80, description="Gantt: task label column.")
    size: Optional[str] = Field(default=None, max_length=80, description="Bubble: size column.")
    trendline: bool = False

    @field_validator("table_id")
    @classmethod
    def _table_id(cls, v: str) -> str:
        if v and not _TABLE_ID_RE.match(v):
            raise ValueError("table_id must be a table id from the material")
        return v

    @field_validator("agg", mode="before")
    @classmethod
    def _agg_alias(cls, v: Any) -> Any:
        if isinstance(v, str):
            k = v.strip().lower()
            return {"mean": "avg", "average": "avg", "total": "sum", "cnt": "count", "count_rows": "count", "raw": "none"}.get(k, k)
        return v

    @field_validator("date_bucket", mode="before")
    @classmethod
    def _bucket_alias(cls, v: Any) -> Any:
        if isinstance(v, str):
            k = v.strip().lower()
            return {"daily": "day", "weekly": "week", "monthly": "month", "quarterly": "quarter", "yearly": "year", "annual": "year", "": None}.get(k, k)
        return v


# ------------------------------------------------------------------ style --


class ChartTextStyle(_Strict):
    """The subset of style.TextStyle a chart element can carry."""

    font_family: Optional[str] = Field(default=None, max_length=60)
    size_pt: Optional[float] = Field(default=None, ge=6, le=72)
    bold: Optional[bool] = None
    italic: Optional[bool] = None
    underline: Optional[bool] = None
    color: Optional[str] = Field(default=None, max_length=40)

    @field_validator("font_family")
    @classmethod
    def _font(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        got = allowed_font(v)
        if got is None:
            raise ValueError(f"font {v[:40]!r} is not on the allowlist")
        return got

    @field_validator("color")
    @classmethod
    def _color(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        got = resolve_color(v)
        if got is None:
            raise ValueError(f"colour {v[:40]!r} is not a colour name or hex code")
        return got


def _colour_list(v: Optional[List[str]]) -> Optional[List[str]]:
    if v is None:
        return None
    out = []
    for c in v:
        got = resolve_color(c)
        if got is None:
            raise ValueError(f"colour {str(c)[:40]!r} is not a colour name or hex code")
        out.append(got)
    return out


def _colour_map(v: Optional[Dict[str, str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, c in (v or {}).items():
        got = resolve_color(c)
        if got is None:
            raise ValueError(f"colour {str(c)[:40]!r} is not a colour name or hex code")
        out[_clip(str(k), 80)] = got
    return out


class ChartStyle(_Strict):
    palette: Optional[List[str]] = Field(default=None, max_length=12)
    color: Optional[str] = Field(default=None, max_length=40, description="One colour for a single-series chart (\"blue bars\").")
    series_colors: Dict[str, str] = Field(default_factory=dict, description="Series name → colour.")
    category_colors: Dict[str, str] = Field(default_factory=dict, description="Category (pie slice, bar) → colour.")
    font_family: Optional[str] = Field(default=None, max_length=60)
    title: Optional[ChartTextStyle] = None
    axis: Optional[ChartTextStyle] = None
    legend: Optional[ChartTextStyle] = None
    data_label: Optional[ChartTextStyle] = None
    legend_position: LegendPosition = "bottom"
    data_labels: Literal["auto", "on", "off"] = "auto"
    number_format: Optional[NumberFormat] = None
    y_min: Optional[float] = None
    y_max: Optional[float] = None
    x_min: Optional[float] = None
    x_max: Optional[float] = None
    log_y: bool = False
    gridlines: bool = True
    background: Optional[str] = Field(default=None, max_length=40)
    width_in: Optional[float] = Field(default=None, ge=2, le=14)
    height_in: Optional[float] = Field(default=None, ge=2, le=10)
    dpi: Optional[int] = Field(default=None, ge=72, le=300)

    @field_validator("palette")
    @classmethod
    def _palette(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return _colour_list(v)

    @field_validator("series_colors", "category_colors", mode="before")
    @classmethod
    def _maps(cls, v: Any) -> Any:
        if v is None:
            return {}
        if isinstance(v, dict) and len(v) > 50:
            raise ValueError("at most 50 colour assignments")
        return _colour_map(v) if isinstance(v, dict) else v

    @field_validator("color", "background")
    @classmethod
    def _one_colour(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        got = resolve_color(v)
        if got is None:
            raise ValueError(f"colour {v[:40]!r} is not a colour name or hex code")
        return got

    @field_validator("font_family")
    @classmethod
    def _font(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        got = allowed_font(v)
        if got is None:
            raise ValueError(f"font {v[:40]!r} is not on the allowlist")
        return got

    @field_validator("number_format", mode="before")
    @classmethod
    def _nf_alias(cls, v: Any) -> Any:
        if isinstance(v, str):
            k = v.strip()
            if k in NUMBER_FORMATS:
                return k
            return _NUMBER_FORMAT_ALIASES.get(k.lower(), "currency_" + k.upper() if k.upper() in ("INR", "USD", "EUR", "GBP") else k)
        return v

    @field_validator("y_min", "y_max", "x_min", "x_max")
    @classmethod
    def _finite(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not math.isfinite(v):
            raise ValueError("axis bounds must be finite")
        return v


# ----------------------------------------------------------------- output --


class Series(_Strict):
    """A computed series (or a legacy literal one). `x` and `sizes` are the
    scatter/bubble coordinates; `axis`/`kind` place a combo series."""

    name: str = Field(min_length=1, max_length=80)
    values: List[float] = Field(default_factory=list, max_length=5000)
    x: Optional[List[float]] = Field(default=None, max_length=5000)
    sizes: Optional[List[float]] = Field(default=None, max_length=5000)
    color: Optional[str] = Field(default=None, max_length=40)
    axis: Literal["primary", "secondary"] = "primary"
    kind: Optional[Literal["bar", "line"]] = None

    @field_validator("values", "x", "sizes")
    @classmethod
    def _finite(cls, v: Optional[List[float]]) -> Optional[List[float]]:
        if v is not None and any(not math.isfinite(x) for x in v):
            raise ValueError("chart values must be finite numbers")
        return v

    @field_validator("color")
    @classmethod
    def _colour(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else resolve_color(v)


class BoxStats(_Strict):
    name: str = Field(max_length=80)
    n: int = 0
    min: float = 0.0
    q1: float = 0.0
    median: float = 0.0
    q3: float = 0.0
    max: float = 0.0
    mean: float = 0.0
    whisker_low: float = 0.0
    whisker_high: float = 0.0
    outliers: List[float] = Field(default_factory=list, max_length=200)


class Span(_Strict):
    label: str = Field(max_length=120)
    start: str = Field(max_length=10)   # ISO date
    end: str = Field(max_length=10)
    days: float = 0.0


class Trend(_Strict):
    series: str = Field(max_length=80)
    slope: float
    intercept: float
    r2: float


class ChartExtra(_Strict):
    bin_edges: List[float] = Field(default_factory=list, max_length=101)
    box: List[BoxStats] = Field(default_factory=list, max_length=60)
    spans: List[Span] = Field(default_factory=list, max_length=200)
    trendlines: List[Trend] = Field(default_factory=list, max_length=8)
    heatmap_rows: List[str] = Field(default_factory=list, max_length=60)


class Provenance(_Strict):
    table_id: str = Field(max_length=80)
    table_title: str = Field(default="", max_length=200)
    table_provenance: Literal["upload", "paste", "prompt", "answer", "sheet", "data"] = "data"
    rows_total: int = 0
    rows_used: int = 0
    agg: str = Field(default="", max_length=20)
    filters: List[str] = Field(default_factory=list, max_length=5)
    date_order: str = Field(default="", max_length=8)
    date_bucket: str = Field(default="", max_length=8)
    sampled: bool = False
    engine: Literal["python", "pandas", "duckdb", ""] = ""


class Chart(_Strict):
    """Chart v2. A NEW chart carries `data`; code fills `categories`,
    `series`, `extra` and `provenance`. An OLD chart (spec.json written before
    v2) carries literal `categories/series` and no `data`."""

    type: ChartType = "bar"
    title: str = Field(default="", max_length=120)
    subtitle: str = Field(default="", max_length=200)
    x_label: str = Field(default="", max_length=60)
    y_label: str = Field(default="", max_length=60)
    y2_label: str = Field(default="", max_length=60)
    caption: str = Field(default="", max_length=300)
    sources: List[str] = Field(default_factory=list, max_length=8)
    data: Optional[Binding] = None
    style: Optional[ChartStyle] = None
    categories: List[str] = Field(default_factory=list, max_length=T.MAX_CHART_POINTS, json_schema_extra=_READ_ONLY)
    series: List[Series] = Field(default_factory=list, max_length=60, json_schema_extra=_READ_ONLY)
    extra: Optional[ChartExtra] = Field(default=None, json_schema_extra=_READ_ONLY)
    provenance: Optional[Provenance] = Field(default=None, json_schema_extra=_READ_ONLY)

    @field_validator("categories")
    @classmethod
    def _category_text(cls, v: List[str]) -> List[str]:
        return [_clip(str(c), 80) or f"#{i + 1}" for i, c in enumerate(v)]

    @field_validator("type", mode="before")
    @classmethod
    def _type_alias(cls, v: Any) -> Any:
        return chart_type_alias(v)

    @property
    def is_literal(self) -> bool:
        """An old-style chart: numbers in the spec, no binding."""
        return self.data is None

    @property
    def is_resolved(self) -> bool:
        return self.data is not None and self.provenance is not None

    @model_validator(mode="after")
    def _shape(self) -> "Chart":
        if self.data is None:
            # Legacy literal chart: the same rules spec.Chart enforces.
            if not self.series:
                raise ValueError("a chart needs a data binding (data.table_id and columns)")
            if self.type not in XY_TYPES and not self.categories:
                raise ValueError("a literal chart needs categories")
            self._lengths()
            if self.type in PART_OF_WHOLE_TYPES and len(self.series) != 1:
                raise ValueError("a pie chart has exactly one series")
            if all(v == 0 for s in self.series for v in s.values):
                raise ValueError("the chart has no data: every value is 0 — fill it from the material or leave the chart out")
        elif self.series:
            self._lengths()
        return self

    def _lengths(self) -> None:
        n = len(self.categories)
        for s in self.series:
            if self.type in XY_TYPES:
                if s.x is None or len(s.x) != len(s.values):
                    raise ValueError(f"series {s.name!r} needs one x per value")
                if s.sizes is not None and len(s.sizes) != len(s.values):
                    raise ValueError(f"series {s.name!r} needs one size per value")
            elif len(s.values) != n:
                raise ValueError(f"series {s.name!r} has {len(s.values)} values for {n} categories")


#: A chart over a workbook sheet: the same model; an empty `data.table_id`
#: means the sheet's own rows (chart_data resolves it to `sheet:<name>`).
SheetChart = Chart


# ------------------------------------------------------------------ edits --


class BindingPatch(_Strict):
    table_id: Optional[str] = Field(default=None, max_length=80)
    x: Optional[str] = Field(default=None, max_length=80)
    y: Optional[List[str]] = Field(default=None, max_length=8)
    agg: Optional[Agg] = None
    group_by: Optional[str] = Field(default=None, max_length=80)
    date_bucket: Optional[DateBucket] = None
    date_order: Optional[DateOrder] = None
    bins: Optional[int] = Field(default=None, ge=2, le=100)
    filters: Optional[List[Filter]] = Field(default=None, max_length=5)
    sort: Optional[Literal["auto", "none", "x", "value_desc", "value_asc"]] = None
    top_n: Optional[int] = Field(default=None, ge=1, le=50)
    other_bucket: Optional[bool] = None
    y2: Optional[List[str]] = Field(default=None, max_length=4)
    start: Optional[str] = Field(default=None, max_length=80)
    end: Optional[str] = Field(default=None, max_length=80)
    label: Optional[str] = Field(default=None, max_length=80)
    size: Optional[str] = Field(default=None, max_length=80)
    trendline: Optional[bool] = None


class ChartStylePatch(ChartStyle):
    """ChartStyle with every field optional (`None` = leave as is)."""

    legend_position: Optional[LegendPosition] = None  # type: ignore[assignment]
    data_labels: Optional[Literal["auto", "on", "off"]] = None  # type: ignore[assignment]
    log_y: Optional[bool] = None  # type: ignore[assignment]
    gridlines: Optional[bool] = None  # type: ignore[assignment]
    series_colors: Optional[Dict[str, str]] = None  # type: ignore[assignment]
    category_colors: Optional[Dict[str, str]] = None  # type: ignore[assignment]


class ChartPatch(_Strict):
    type: Optional[ChartType] = None
    title: Optional[str] = Field(default=None, max_length=120)
    subtitle: Optional[str] = Field(default=None, max_length=200)
    x_label: Optional[str] = Field(default=None, max_length=60)
    y_label: Optional[str] = Field(default=None, max_length=60)
    y2_label: Optional[str] = Field(default=None, max_length=60)
    caption: Optional[str] = Field(default=None, max_length=300)
    data: Optional[BindingPatch] = None
    style: Optional[ChartStylePatch] = None

    @field_validator("type", mode="before")
    @classmethod
    def _type_alias(cls, v: Any) -> Any:
        return chart_type_alias(v)


class ChartRef(_Strict):
    """Which chart an edit means: its 1-based position in document order, its
    title (fuzzy), its type, or the slide/sheet that holds it."""

    index: Optional[int] = Field(default=None, ge=1, le=400)
    title: Optional[str] = Field(default=None, max_length=120)
    type: Optional[str] = Field(default=None, max_length=40)
    sheet: Optional[str] = Field(default=None, max_length=31)
    slide: Optional[int] = Field(default=None, ge=1, le=T.MAX_SLIDES + 1)


def apply_patch(chart: Chart, patch: ChartPatch) -> Chart:
    """A new Chart with `patch` merged in. A change to the binding or the type
    clears the computed fields, so the caller re-runs chart_data.resolve_spec
    (0 model calls); a style or label change keeps them."""
    data = chart.model_dump()
    recompute = False
    for key in ("type", "title", "subtitle", "x_label", "y_label", "y2_label", "caption"):
        value = getattr(patch, key)
        if value is not None:
            if key == "type" and value != chart.type:
                recompute = True
            data[key] = value
    if patch.data is not None:
        binding = dict(data.get("data") or {})
        for key, value in patch.data.model_dump(exclude_none=True).items():
            if binding.get(key) != value:
                recompute = True
            binding[key] = value
        data["data"] = binding
    if patch.style is not None:
        style = dict(data.get("style") or {})
        for key, value in patch.style.model_dump(exclude_none=True).items():
            if key in ("series_colors", "category_colors"):
                merged = dict(style.get(key) or {})
                merged.update(value)
                style[key] = merged
            elif key in ("title", "axis", "legend", "data_label") and isinstance(value, dict):
                merged = dict(style.get(key) or {})
                merged.update({k: v for k, v in value.items() if v is not None})
                style[key] = merged
            else:
                style[key] = value
        data["style"] = style
    if recompute and data.get("data"):
        data["categories"], data["series"], data["extra"], data["provenance"] = [], [], None, None
    return Chart.model_validate(data)


# -------------------------------------------------------------- legacy map --


def from_legacy(chart: Any) -> Chart:
    """spec.Chart (or its dict) → Chart v2, literal. Always succeeds for a
    valid spec.Chart."""
    if isinstance(chart, Chart):
        return chart
    data = chart.model_dump() if hasattr(chart, "model_dump") else dict(chart)
    return Chart.model_validate(data)


_LEGACY_KEYS = ("type", "title", "categories", "series", "y_label", "caption", "sources")


def to_legacy(chart: Chart) -> Any:
    """Chart v2 → spec.Chart for the four legacy types, with computed values;
    None when the type or shape has no legacy form."""
    from . import spec as S  # lazy: spec will import this module

    if chart.type not in LEGACY_TYPES or not chart.series or not chart.categories:
        return None
    data = {k: v for k, v in chart.model_dump().items() if k in _LEGACY_KEYS}
    data["series"] = [{"name": s["name"], "values": s["values"]} for s in data["series"][:8]]
    try:
        return S.Chart.model_validate(data)
    except Exception:
        return None


# ------------------------------------------------------------- guided schema --

_OUTPUT_FIELDS = ("categories", "series", "extra", "provenance")


#: Style fields a constrained decoder is offered. The rest of ChartStyle
#: (sizes in inches, dpi, background, x bounds, per-element fonts) is set by
#: code or by the styling track's parser, never guessed by the model: in the
#: first live pilot (2026-09-15) the model filled width/height/dpi/background
#: and fonts nobody asked for when the whole ChartStyle was in the schema.
GUIDED_STYLE_FIELDS: Tuple[str, ...] = (
    "color", "series_colors", "category_colors", "legend_position", "data_labels", "number_format", "y_min", "y_max", "log_y",
)


#: Binding fields in the order a decoder should consider them. Live run
#: 2026-09-15 (54 requests): with the pydantic order the model filled y2,
#: label, size and trendline on bar/line charts and skipped group_by in 5 of
#: 15 misses; the grouping column now comes right after x and the
#: type-specific fields last, each saying which type it is for.
_GUIDED_BINDING_ORDER: Tuple[str, ...] = (
    "table_id", "x", "group_by", "y", "agg", "date_bucket", "filters", "sort", "top_n", "bins", "trendline", "y2", "label", "start", "end", "size",
)
_BINDING_HINTS: Dict[str, str] = {
    "group_by": "The second dimension ('by region', 'for each region', 'broken down by priority'): one series per value — stacked bars, one line per group, heatmap rows. Leave out when there is one dimension.",
    "y": "Numeric column(s) to plot. Leave empty with agg='count' to count rows.",
    "y2": "ONLY for a combo / dual-axis chart: columns drawn as lines on the second axis.",
    "label": "ONLY for gantt: the task name column.",
    "start": "ONLY for gantt: the start date column.",
    "end": "ONLY for gantt: the end date column.",
    "size": "ONLY for bubble: the bubble size column.",
    "trendline": "ONLY for scatter: true when a trend line is asked for.",
    "bins": "ONLY for histogram.",
}


def _column_kinds(tables: Sequence[Any]) -> Tuple[List[str], List[str], List[str], List[str]]:
    from . import chart_data  # lazy: chart_data imports this module

    ids: List[str] = []
    every: List[str] = []
    numeric: List[str] = []
    dates: List[str] = []
    for t in tables:
        tid = str(t.get("id") if isinstance(t, dict) else getattr(t, "id", ""))
        if tid and tid not in ids:
            ids.append(tid)
        cols = list((t.get("columns") if isinstance(t, dict) else getattr(t, "columns", [])) or [])
        for i, name in enumerate(cols[:60]):
            name = str(name)
            info = chart_data.infer_column(t, i)
            if name not in every:
                every.append(name)
            if info.kind == "number" and name not in numeric:
                numeric.append(name)
            if info.kind == "date" and name not in dates:
                dates.append(name)
    return ids, every, numeric, dates


def guided_schema(model: type = Chart, tables: Sequence[Any] = ()) -> dict:
    """The JSON schema a constrained decoder sees for a chart: the output
    fields are removed (and their now-unused definitions dropped); `type`,
    `title` and `data` are required; `style` offers only GUIDED_STYLE_FIELDS
    and defaults nothing. With `tables`, column fields are ENUMS of the real
    column names (numeric columns for y/y2/size, date columns for gantt
    start/end) and table_id is an enum of the table ids, so the decoder
    cannot name a column that does not exist."""
    schema = model.model_json_schema()
    props = schema.get("properties", {})
    for key in _OUTPUT_FIELDS:
        props.pop(key, None)
    req = [r for r in schema.get("required", []) if r not in _OUTPUT_FIELDS]
    for key in ("type", "title", "data"):
        if key in props and key not in req:
            req.append(key)
    schema["required"] = req
    defs = schema.get("$defs", {})
    for name in ("Series", "ChartExtra", "Provenance", "BoxStats", "Span", "Trend"):
        defs.pop(name, None)
    style = defs.get("ChartStyle")
    if style:
        style["properties"] = {k: v for k, v in style.get("properties", {}).items() if k in GUIDED_STYLE_FIELDS}
        for v in style["properties"].values():
            v.pop("default", None)
        style["description"] = "Only what the person asked for; omit style entirely otherwise."
        defs.pop("ChartTextStyle", None)
    binding = defs.get("Binding")
    if binding:
        bprops = binding.get("properties", {})
        ordered = {k: bprops[k] for k in _GUIDED_BINDING_ORDER if k in bprops}
        for k, v in ordered.items():
            v.pop("default", None)
            if k in _BINDING_HINTS:
                v["description"] = _BINDING_HINTS[k]
        binding["properties"] = ordered
        binding["required"] = ["table_id"]
        if tables:
            ids, every, numeric, dates = _column_kinds(tables)

            def one_of(names: List[str]) -> dict:
                return {"type": "string", "enum": list(names)} if names else {"type": "string"}

            if ids:
                ordered["table_id"] = {**one_of(ids), "description": "The id of the table to chart."}
            for key, names in (("x", every), ("group_by", every), ("label", every), ("start", dates or every), ("end", dates or every), ("size", numeric or every)):
                if key in ordered:
                    ordered[key] = {**one_of(names), "description": ordered[key].get("description", "")}
            for key in ("y", "y2"):
                if key in ordered:
                    ordered[key] = {"type": "array", "items": one_of(numeric or every), "maxItems": 8 if key == "y" else 4,
                                    "description": ordered[key].get("description", "")}
            flt = defs.get("Filter")
            if flt and "column" in flt.get("properties", {}):
                flt["properties"]["column"] = one_of(every)
    return schema


def chart_from_model(raw: Any) -> Tuple[Chart, List[str]]:
    """A chart the model wrote → a validated Chart. A style value that does
    not validate (a colour word that is not a colour, a font off the
    allowlist) is dropped with a note instead of losing the whole chart;
    anything wrong outside `style` still raises."""
    from pydantic import ValidationError

    notes: List[str] = []
    data = dict(raw) if isinstance(raw, dict) else raw
    for _ in range(12):
        try:
            return Chart.model_validate(data), notes
        except ValidationError as exc:
            if not isinstance(data, dict) or not isinstance(data.get("style"), dict):
                raise
            dropped = False
            style = dict(data["style"])
            for err in exc.errors():
                loc = err.get("loc", ())
                if len(loc) >= 2 and loc[0] == "style" and loc[1] in style:
                    notes.append(f"the chart style {loc[1]} was not applied: {err.get('msg', '')[:80]}")
                    style.pop(loc[1])
                    dropped = True
            if not dropped:
                raise
            data = {**data, "style": style}
    return Chart.model_validate(data), notes


def strip_output_fields(schema: Any) -> Any:
    """Recursively remove readOnly properties from any schema that embeds a
    Chart (for spec.schema_for after the swap)."""
    if isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            for key in [k for k, v in props.items() if isinstance(v, dict) and v.get("readOnly")]:
                props.pop(key)
                if isinstance(schema.get("required"), list):
                    schema["required"] = [r for r in schema["required"] if r != key]
        for v in list(schema.values()):
            strip_output_fields(v)
    elif isinstance(schema, list):
        for v in schema:
            strip_output_fields(v)
    return schema


# ----------------------------------------------------------- container map --

_ALL_IMAGE = {t: "image" for t in CHART_TYPES}

#: Per output format, how each type is drawn. `image` means a PNG (or SVG in
#: PDF/HTML) drawn by matplotlib from the same computed values.
CONTAINER_SUPPORT: Dict[str, Dict[str, str]] = {
    "xlsx": {
        **_ALL_IMAGE,
        **{t: "native" for t in (*TIER1_TYPES, "radar", "bubble")},
    },
    "pptx": {
        **_ALL_IMAGE,
        **{t: "native" for t in (*TIER1_TYPES, "radar", "bubble")},
    },
    "docx": dict(_ALL_IMAGE),
    "pdf": dict(_ALL_IMAGE),
    "html": dict(_ALL_IMAGE),
    "png": dict(_ALL_IMAGE),
    "svg": dict(_ALL_IMAGE),
    "csv": {t: "unsupported" for t in CHART_TYPES},
}


def support_for(fmt: str, chart_type: str) -> str:
    return CONTAINER_SUPPORT.get(fmt, {}).get(chart_type, "unsupported")


# ------------------------------------------------------------ chart walking --


def iter_chart_slots(spec: Any) -> Iterator[Tuple[Tuple[Any, ...], Any]]:
    """Every chart in a spec (model or dict; envelope or body), in document
    order, as (path, chart). Paths: ('blocks', i, 'chart'), ('slides', i,
    'chart'), ('sheets', i, 'charts', j) — relative to the body."""
    body = _body_of(spec)
    blocks = _get(body, "blocks")
    if isinstance(blocks, list):
        for i, b in enumerate(blocks):
            if _get(b, "type") == "chart" and _get(b, "chart") is not None:
                yield ("blocks", i, "chart"), _get(b, "chart")
    slides = _get(body, "slides")
    if isinstance(slides, list):
        for i, s in enumerate(slides):
            if _get(s, "chart") is not None:
                yield ("slides", i, "chart"), _get(s, "chart")
    sheets = _get(body, "sheets")
    if isinstance(sheets, list):
        for i, sh in enumerate(sheets):
            for j, c in enumerate(_get(sh, "charts") or []):
                yield ("sheets", i, "charts", j), c


def find_charts(spec: Any, ref: ChartRef) -> List[Tuple[Tuple[Any, ...], Any]]:
    """The chart slots `ref` names. Title matching folds case/space and falls
    back to a close match; several matches are returned for the caller to
    ask about."""
    import difflib

    slots = list(iter_chart_slots(spec))
    if ref.index is not None:
        slots = slots[ref.index - 1: ref.index] if ref.index <= len(slots) else []
    if ref.slide is not None:
        slots = [s for s in slots if s[0][0] == "slides" and s[0][1] == ref.slide - 1]
    if ref.sheet:
        body = _body_of(spec)
        names = [str(_get(sh, "name") or "").casefold() for sh in (_get(body, "sheets") or [])]
        slots = [s for s in slots if s[0][0] == "sheets" and names[s[0][1]] == ref.sheet.casefold()]
    if ref.type:
        slots = [s for s in slots if str(_get(s[1], "type")) == ref.type]
    if ref.title:
        want = " ".join(ref.title.split()).casefold()
        exact = [s for s in slots if " ".join(str(_get(s[1], "title") or "").split()).casefold() == want]
        if exact:
            return exact
        titles = [" ".join(str(_get(s[1], "title") or "").split()).casefold() for s in slots]
        close = set(difflib.get_close_matches(want, titles, n=3, cutoff=0.75))
        slots = [s for s, t in zip(slots, titles) if t in close or (want and want in t)]
    return slots


def _body_of(spec: Any) -> Any:
    for key in ("document", "presentation", "workbook"):
        inner = _get(spec, key)
        if inner is not None:
            return inner
    body = getattr(spec, "body", None) if not isinstance(spec, dict) else spec.get("body")
    return body if body is not None else spec


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


# ------------------------------------------------------------ prompt guide --


def prompt_guide(kind: str, tables: Sequence[Any]) -> str:
    """What the composer is told about charts: the tables it may bind to
    (ids, columns with types, a few distinct values for low-cardinality
    columns) and the rules. The model writes `data`, never numbers."""
    from . import chart_data  # lazy: chart_data imports this module

    lines: List[str] = []
    lines.append("CHARTS. A chart is drawn by code from a TABLE. Write `data` (the binding) and presentation fields only; never write `categories`, `series` or any number — code computes them.")
    if tables:
        lines.append("Tables you may chart (bind with data.table_id):")
        for t in list(tables)[:12]:
            lines.append("- " + chart_data.describe_table(t))
    else:
        lines.append("There is no table in the material, so do not add a chart: say in the text which figures are needed as a table.")
    lines.append(
        "Binding: x = category/date column; y = numeric column(s); agg = sum|avg|count|min|max|median|none (count needs no y); "
        "group_by = one series per value (stacked bars, heatmap rows); date_bucket = day|week|month|quarter|year; "
        "filters = [{column, op: eq|ne|in|contains|gt|gte|lt|lte|between, value}]; top_n; y2 = columns on a secondary axis (combo); "
        "gantt needs label, start, end; bubble needs x, y, size; scatter needs numeric x and y (trendline: true for a trend line); histogram needs one numeric y (bins optional)."
    )
    lines.append(
        "Filters compare real cell values: a date range is two filters with ISO dates "
        "({\"column\": \"Date\", \"op\": \"gte\", \"value\": \"2026-01-01\"}); \"Q1 and Q2\" is date_bucket quarter plus a date filter, never a filter on the text Q1. "
        "Stacked or one-line-per-group charts put the grouping column in group_by."
    )
    lines.append(
        'Example: {"type": "stacked_bar", "title": "Sales by quarter and region", "data": {"table_id": "upload1", "x": "Date", "date_bucket": "quarter", '
        '"group_by": "Region", "y": ["Amount"], "agg": "sum"}}. Example: {"type": "pie", "title": "Tickets by status", "data": {"table_id": "upload2", "x": "Status", "agg": "count"}, '
        '"style": {"category_colors": {"Open": "red"}}}.'
    )
    # The SAME rule table chart_choice.recommend applies after the model
    # answers: a type the guidance and the chooser disagree on is a type the
    # person is told was changed, so the two lists are kept in one place.
    from . import chart_choice  # lazy: chart_choice imports this module

    lines.append(chart_choice.rules_text())
    lines.append("Types: " + ", ".join(CHART_TYPES) + ".")
    if kind == "workbook":
        lines.append("On a sheet chart, an empty data.table_id means the sheet's own rows.")
    lines.append("Style only what the person asked for (style.color for a one-series chart, series_colors, category_colors, legend_position, data_labels, number_format, y_min/y_max); leave style out otherwise.")
    return "\n".join(lines)


__all__ = [
    "CHART_TYPES", "TIER1_TYPES", "TIER2_TYPES", "LEGACY_TYPES", "AGGREGATING_TYPES", "BAR_FAMILY", "STACKED_TYPES",
    "PART_OF_WHOLE_TYPES", "XY_TYPES", "AGGS", "AGG_LABELS", "DATE_BUCKETS", "NUMBER_FORMATS", "DEFAULT_PALETTE",
    "Filter", "Binding", "ChartTextStyle", "ChartStyle", "Series", "BoxStats", "Span", "Trend", "ChartExtra", "Provenance",
    "Chart", "SheetChart", "BindingPatch", "ChartStylePatch", "ChartPatch", "ChartRef", "apply_patch", "from_legacy",
    "to_legacy", "guided_schema", "strip_output_fields", "CONTAINER_SUPPORT", "support_for", "iter_chart_slots",
    "find_charts", "prompt_guide", "resolve_color", "allowed_font", "chart_from_model", "GUIDED_STYLE_FIELDS",
]
