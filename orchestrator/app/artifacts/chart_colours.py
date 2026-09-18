"""What colour a chart's marks are when NOBODY SAID — the automatic scheme.

WHY THIS EXISTS. Until now the renderers varied colour per SERIES and by
nothing else, so every single-series chart in the product came out of palette
slot 0: one blue, for a revenue bar chart, a headcount bar chart and a
churn bar chart alike, in the same document. Nothing chose a colour from what
the colour MEANT, and the composer is told not to style charts (chart_spec's
guidance), so no colour was ever chosen at all. This module is the missing
step: a pure function from a chart (and its document) to the colours its
marks get, consulted at render time by every renderer.

WHAT IT NEVER DOES. It never writes `chart.style`. `chart.style` belongs to
the person: it is what an edit turn reads, what `selfcheck` compares against
the request, and what `inspect_files` reads back out of the produced file. An
automatic colour that wrote itself into the spec would be indistinguishable
from a colour somebody asked for. It also never changes a chart's TYPE;
`chart_choice` owns types (the PR #77 rule).

PRECEDENCE. Every explicit field wins, in this order, and the scheme is
consulted only when all of them are silent:

    series.color → style.series_colors → style.category_colors →
    style.color → style.palette → THE SCHEME → the palette

THE RULES (`rule_for`), first match wins:

  signed      a waterfall, or a single-series bar with a negative value:
              gain #2F6FB2, loss #C0566B, neutral grey totals, and a legend.
              Green against red is never used — about 1 man in 12 cannot
              read that pair, and it is the one pairing a signed chart
              tempts you into.
  ordinal     a funnel: one hue, monotone lightness, light end >= 2:1 on
              white, so the stages read as an order rather than as five
              unrelated things.
  time        a single series over a date x, or a line or an area: ONE
              colour. A time series is one thing measured repeatedly, and
              colouring its points differently says the opposite.
  status      the chart SAYS it is about status (style.STATUS_COLUMN_RE
              matches its x column, group_by, axis label, title or
              subtitle) AND every category (or, on a stack, every series)
              is a status VALUE (style.STATUS_VALUES): the reserved status
              tokens, plus mandatory labels — status colour is never the
              only carrier of the meaning. Both halves are required: a
              High/Medium/Low spend band is an ordinary nominal column.
  categorical pie, donut, treemap, sunburst, stacked and multi-series: a
              categorical slot per category or series NAME, assigned across
              the whole document, so "North" is one colour in every chart.
  ranking     a chart the person asked for as a ranking ("top", "most",
              "largest") and that is sorted by value: the leader in the
              subject's colour, the rest neutral grey. The chart's job is
              to point at one row.
  subject     everything else, which is most bar charts: ONE colour for the
              whole series — the palette slot of the chart's SUBJECT (its x
              column plus its measure), assigned in first-appearance order
              across the document. Two charts of different subjects get
              different colours; the same subject charted twice keeps one.

THE DOCUMENT PLAN. First-appearance order is a property of the DOCUMENT, not
of a chart, so it is computed once by `plan_for(spec)` (from
`style.resolve`) and handed to every renderer on `ResolvedStyle.chart_plan`.
Without a plan each chart still gets a scheme; it just starts counting from
slot 1, which is what a standalone chart render wants anyway.

PURE. No matplotlib, no Office library, no I/O: this module is imported by
`style`, `render/charts`, `render/chart_native`, `render/pptx` and
`render/xlsx`, and it must stay cheap for all of them.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import chart_spec as CS

# ================================================================ palette ==

#: The eight categorical slots, in the order they are handed out.
#:
#: Slots 1-5 are the style guide's colour-vision order and do not move.
#: Slots 6-8 were re-stepped on 2026-09-17: the old #8A5A44 and #5F6B7A had
#: OKLCH chroma 0.072 and 0.028, under the 0.10 floor at which a hue starts
#: reading as grey, and #5F6B7A against #3F8F4F was only 14.8 OKLab ΔE apart
#: under NORMAL vision — a pair full-colour readers cannot separate either.
#: `tests/test_artifact_chart_colours.py` recomputes all three checks.
CHART_PALETTE: Tuple[str, ...] = CS.DEFAULT_PALETTE

#: How many slots a chart's SUBJECT may be drawn from. This was 5 — the five
#: that are pairwise separated under simulated deuteranopia — but that floor
#: governs marks that sit SIDE BY SIDE inside one chart, and two subjects
#: live in two different charts and are never adjacent. At 5, the sixth
#: subject of a document wrapped back to slot 1: measured on a seven-chart
#: report, "Revenue by Region" and "Margin by Region" both came out #2F6FB2,
#: which is the reported complaint again inside exactly the long report this
#: round is meant to produce.
SUBJECT_SLOTS = len(CHART_PALETTE)

#: A signed measure. Blue rises, rose falls, grey is a total or a starting
#: balance. Never green against red.
GAIN = "#2F6FB2"
LOSS = "#C0566B"
NEUTRAL = "#5F6B7A"

#: Chart gridlines. Lighter than the table hairline (style.Tokens.grid,
#: #E5E9F0): a gridline is a reading aid behind the data, not a border.
GRID = "#E8ECF1"

#: Status class → the mark colour: a RESERVED palette, never reused as
#: "series 4". Green / amber / red is what a status chart is read as, and it
#: is also the one triple a red-green reader cannot separate by hue — so the
#: four are stepped apart in LIGHTNESS as well (OKLab ΔE >= 8.7 under both
#: simulated protanopia and deuteranopia over ALL pairs, >= 17.4 under normal
#: vision, every one at 3:1 or better on white, and every one carrying a
#: label colour at 4.4:1 or better). `force_labels` makes the labels
#: mandatory, because colour must never be the only carrier of "critical".
#: tests/test_artifact_chart_colours.py recomputes all of it.
STATUS_MARKS: Dict[str, str] = {
    "success": "#2F8247",
    "warning": "#B28D2A",
    "danger": "#902731",
    "info": "#005591",
    "neutral": NEUTRAL,
}

WHITE = "#FFFFFF"

#: The words that make a bar chart a ranking rather than a comparison.
_RANK_RE = re.compile(r"\b(top|most|largest|highest|biggest)\b", re.IGNORECASE)
#: A category that is a total rather than a part of the movement.
_TOTAL_RE = re.compile(r"^(total|net|net total|overall|sum|grand total|closing|opening)$", re.IGNORECASE)


# ============================================================== colour maths ==


def _channels(hex_colour: str) -> Tuple[float, float, float]:
    h = str(hex_colour).lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def _to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _to_srgb(c: float) -> float:
    c = min(1.0, max(0.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def luminance(hex_colour: str) -> float:
    """WCAG relative luminance of a `#RRGGBB` colour."""
    r, g, b = (_to_linear(c) for c in _channels(hex_colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    """WCAG 2.x contrast ratio of two `#RRGGBB` colours (1..21)."""
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def to_oklch(hex_colour: str) -> Tuple[float, float, float]:
    """(L, C, H degrees) in OKLCH. Perceptual lightness is what an ordinal
    ramp has to be monotone in; sRGB channels are not."""
    r, g, b = (_to_linear(c) for c in _channels(hex_colour))
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    L = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s
    a_ = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
    b_ = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    return L, math.hypot(a_, b_), math.degrees(math.atan2(b_, a_)) % 360.0


def _oklch_rgb(L: float, C: float, H: float) -> Tuple[float, float, float]:
    h = math.radians(H)
    a_, b_ = C * math.cos(h), C * math.sin(h)
    l = (L + 0.3963377774 * a_ + 0.2158037573 * b_) ** 3
    m = (L - 0.1055613458 * a_ - 0.0638541728 * b_) ** 3
    s = (L - 0.0894841775 * a_ - 1.2914855480 * b_) ** 3
    return (
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )


def from_oklch(L: float, C: float, H: float) -> str:
    """`#RRGGBB` for an OKLCH triple, with chroma reduced until the colour
    is inside sRGB (lightness and hue are kept: the ramp depends on them)."""
    lo, hi = 0.0, max(0.0, C)
    if all(-0.001 <= v <= 1.001 for v in _oklch_rgb(L, hi, H)):
        lo = hi
    else:
        for _ in range(24):
            mid = (lo + hi) / 2
            if all(-0.001 <= v <= 1.001 for v in _oklch_rgb(L, mid, H)):
                lo = mid
            else:
                hi = mid
    r, g, b = _oklch_rgb(L, lo, H)
    return "#" + "".join(f"{round(_to_srgb(v) * 255):02X}" for v in (r, g, b))


def ordinal_ramp(base: str, n: int, *, light_min_contrast: float = 2.0) -> List[str]:
    """`n` steps of ONE hue, dark to light, strictly increasing in
    lightness, whose lightest step still reaches `light_min_contrast` on
    white (a stage nobody can see on the page is not a stage)."""
    if n <= 0:
        return []
    L0, C0, H = to_oklch(base)
    if n == 1:
        return [base]
    dark = min(max(L0, 0.40), 0.56)

    def step(L: float, span: float) -> str:
        # Chroma eases off towards the light end, the way a real tint ramp
        # does; the hue never moves.
        share = 0.0 if span <= 1e-6 else (L - dark) / span
        return from_oklch(L, max(0.02, C0 * (1.0 - 0.45 * share)), H)

    lo, hi = dark + 0.04, 0.94
    if contrast_ratio(step(hi, hi - dark), WHITE) < light_min_contrast:
        for _ in range(24):
            mid = (lo + hi) / 2
            if contrast_ratio(step(mid, mid - dark), WHITE) >= light_min_contrast:
                lo = mid
            else:
                hi = mid
        light = lo
    else:
        light = hi
    span = light - dark
    return [step(dark + span * i / (n - 1), span) for i in range(n)]


# ================================================================== reading ==


def _fold(text: Any) -> str:
    return " ".join(unicodedata.normalize("NFC", str(text or "")).split()).casefold()


def as_chart(chart: Any) -> Optional[CS.Chart]:
    """`chart` as a Chart v2, or None when it is not a chart at all. A
    legacy `spec.Chart` and a plain dict are both accepted, because the
    legacy deck and workbook writers hand this module their own objects."""
    if isinstance(chart, CS.Chart):
        return chart
    if chart is None:
        return None
    try:
        if isinstance(chart, dict):
            return CS.Chart.model_validate(chart)
        from .spec import Chart as LegacyChart
    except Exception:  # noqa: BLE001 - colour never fails a render
        return None
    try:
        if isinstance(chart, LegacyChart):
            return CS.from_legacy(chart)
    except Exception:  # noqa: BLE001
        return None
    return None


def subject_key(chart: CS.Chart) -> str:
    """What the chart is ABOUT: its x column plus its measure. Two charts
    that share this share a colour; two that do not, do not."""
    b = chart.data
    if b is not None:
        measure = ",".join(sorted(_fold(c) for c in b.y)) or f"{_fold(b.agg)}:rows"
        return "|".join((_fold(b.table_id), _fold(b.x or ""), _fold(b.agg), measure, _fold(b.group_by or "")))
    measure = _fold(chart.y_label) or ",".join(_fold(s.name) for s in chart.series)
    return "|".join(("", _fold(chart.x_label), "", measure, ""))


def _status_class(value: str) -> Optional[str]:
    from . import style as ST  # local: style imports this module's plan builder

    folded = _fold(value)
    for cls, values in ST.STATUS_VALUES.items():
        if folded in values:
            return cls
    return None


def status_classes(categories: Sequence[str]) -> Optional[List[str]]:
    """One status class per category, or None unless EVERY category is a
    status value. A chart where three of five categories are statuses is a
    chart of something else that happens to contain the word 'open'."""
    cats = [str(c) for c in categories]
    if len(cats) < 2:
        return None
    out = []
    for cat in cats:
        cls = _status_class(cat)
        if cls is None:
            return None
        out.append(cls)
    return out


def _is_time(chart: CS.Chart) -> bool:
    b = chart.data
    if b is not None and b.date_bucket:
        return True
    p = chart.provenance
    return bool(p is not None and p.date_bucket)


def _signed_single_bar(chart: CS.Chart) -> bool:
    return (
        chart.type in ("bar", "horizontal_bar")
        and len(chart.series) == 1
        and any(v < 0 for v in chart.series[0].values)
    )


def _sorted_by_value(values: Sequence[float]) -> bool:
    vals = list(values)
    if len(vals) < 3:
        return False
    return all(a >= b for a, b in zip(vals, vals[1:])) or all(a <= b for a, b in zip(vals, vals[1:]))


def _is_ranking(chart: CS.Chart) -> bool:
    if chart.type not in ("bar", "horizontal_bar") or len(chart.series) != 1:
        return False
    if not _RANK_RE.search(f"{chart.title} {chart.subtitle}"):
        return False
    b = chart.data
    if b is not None:
        return b.sort in ("value_desc", "value_asc") or b.top_n is not None
    return _sorted_by_value(chart.series[0].values)


def _names_of(chart: CS.Chart) -> List[str]:
    """What a chart of parts is keyed by: its categories when one series is
    split into parts, its series names when several are stacked or plotted
    together."""
    if len(chart.series) <= 1:
        return [str(c) for c in chart.categories]
    return [s.name for s in chart.series]


def _status_named(chart: CS.Chart) -> bool:
    """Does this chart SAY it is about status? `status_classes` reads the
    VALUES, and values alone are not enough. Measured on this tree before
    the gate: x='Spend band' with High/Medium/Low came back
    ('#902731', '#B28D2A', '#2F8247') — the biggest spend band painted
    CRITICAL RED and the smallest SUCCESS GREEN — and x='Churned' with
    Yes/No came back ('#2F8247', '#902731'), so "Churned: Yes" was green.
    Everywhere else in the product this colouring is gated on the COLUMN
    NAME (style._status_column, render/xlsx._status_col, both via
    style.STATUS_COLUMN_RE); the chart rule uses the same gate, over the
    binding's x column and group_by, the axis label, the title and the
    subtitle. A chart that is really about status says so in one of them.
    """
    from . import style as ST  # local: style imports this module's plan builder

    parts = [chart.x_label, chart.title, chart.subtitle]
    b = chart.data
    if b is not None:
        parts += [b.x or "", b.group_by or ""]
    return bool(ST.STATUS_COLUMN_RE.search(" ".join(str(p or "") for p in parts)))


def rule_for(chart: CS.Chart) -> str:
    """Which colour rule this chart falls under. Pure, and independent of
    the document plan, so the plan can be built from it."""
    t = str(chart.type)
    n = len(chart.series)
    if t == "waterfall" or _signed_single_bar(chart):
        return "signed"
    if t == "funnel":
        return "ordinal"
    if n <= 1 and (t in ("line", "area") or _is_time(chart)):
        return "time"
    # Statuses can be the CATEGORIES of one series ("Tickets by status") or
    # the SERIES of a stack ("Status by team"). Either way the colour means
    # the status, not "slot 4".
    if status_classes(_names_of(chart)) and _status_named(chart):
        return "status"
    if t in CS.PART_OF_WHOLE_TYPES or t == "sunburst" or t in CS.STACKED_TYPES or n > 1:
        return "categorical"
    if _is_ranking(chart):
        return "ranking"
    return "subject"


def colour_keys(chart: CS.Chart) -> List[str]:
    """The NAMES a categorical chart keys its document-wide slots by. Empty
    for every other rule: a subject, a ramp and the RESERVED status palette
    never take a categorical slot."""
    return _names_of(chart) if rule_for(chart) == "categorical" else []


# =================================================================== plan ==


@dataclass(frozen=True)
class Plan:
    """One document's colour plan, in document order."""

    subjects: Dict[str, str] = field(default_factory=dict)
    names: Dict[str, str] = field(default_factory=dict)
    charts: int = 0

    @property
    def single_chart(self) -> bool:
        return self.charts <= 1


def plan_for(spec: Any, palette: Optional[Sequence[str]] = None) -> Plan:
    """The colour plan for every chart of `spec`, read in document order:
    subject → slot (1-5) and category-or-series name → slot (1-8), each
    assigned when it is first seen. A chart that repeats a subject or a name
    keeps its colour; a new one takes the next slot."""
    pal = tuple(palette or CHART_PALETTE) or CHART_PALETTE
    subjects: Dict[str, str] = {}
    names: Dict[str, str] = {}
    n = 0
    try:
        slots = list(CS.iter_chart_slots(spec))
    except Exception:  # noqa: BLE001 - a plan is an optimisation, never a failure
        return Plan()
    for _path, raw in slots:
        c = as_chart(raw)
        if c is None:
            continue
        n += 1
        key = subject_key(c)
        if key not in subjects:
            subjects[key] = pal[len(subjects) % min(SUBJECT_SLOTS, len(pal))]
        for name in colour_keys(c):
            if name not in names:
                names[name] = pal[len(names) % len(pal)]
    return Plan(subjects=subjects, names=names, charts=n)


# ================================================================= scheme ==


@dataclass(frozen=True)
class Scheme:
    """The automatic colours of ONE chart. `rule` names the rule that fired
    (the renderers branch on `by_category`, the tests read `rule`)."""

    rule: str = ""
    series: Tuple[Optional[str], ...] = ()
    categories: Tuple[Optional[str], ...] = ()
    by_name: Dict[str, str] = field(default_factory=dict)
    by_category: bool = False
    force_labels: bool = False
    signed: Optional[Tuple[str, str, str]] = None
    legend: Tuple[Tuple[str, str], ...] = ()

    def series_colour(self, index: int, name: str = "") -> Optional[str]:
        if name and name in self.by_name:
            return self.by_name[name]
        if 0 <= index < len(self.series):
            return self.series[index]
        return None

    def category_colour(self, index: int, label: str = "") -> Optional[str]:
        if label and label in self.by_name:
            return self.by_name[label]
        if 0 <= index < len(self.categories):
            return self.categories[index]
        return None


EMPTY = Scheme()


def _subject_colour(chart: CS.Chart, plan: Optional[Plan], pal: Tuple[str, ...]) -> str:
    if plan is not None:
        got = plan.subjects.get(subject_key(chart))
        if got:
            return got
    return pal[0]


def scheme_for(chart: Any, plan: Optional[Plan] = None, palette: Optional[Sequence[str]] = None) -> Scheme:
    """The automatic colours for `chart`. Consulted only after every
    explicit field of `chart.style` (see PRECEDENCE above); it reads
    `chart.style` but never writes it."""
    c = as_chart(chart)
    if c is None:
        return EMPTY
    pal = tuple(palette or CHART_PALETTE) or CHART_PALETTE
    rule = rule_for(c)
    cats = [str(x) for x in c.categories]
    n_series = len(c.series)

    if rule == "signed":
        values = list(c.series[0].values) if c.series else []
        colours = []
        for j, cat in enumerate(cats):
            v = values[j] if j < len(values) else 0.0
            colours.append(NEUTRAL if _TOTAL_RE.match(cat.strip()) else (GAIN if v >= 0 else LOSS))
        legend = [("Increase", GAIN), ("Decrease", LOSS)]
        if c.type == "waterfall" or any(col == NEUTRAL for col in colours):
            legend.append(("Total", NEUTRAL))
        return Scheme(rule=rule, series=(GAIN,) * max(1, n_series), categories=tuple(colours),
                      by_category=True, signed=(GAIN, LOSS, NEUTRAL), legend=tuple(legend))

    if rule == "ordinal":
        ramp = ordinal_ramp(_subject_colour(c, plan, pal), len(cats))
        return Scheme(rule=rule, series=(ramp[0],) if ramp else (), categories=tuple(ramp), by_category=True)

    if rule == "status":
        names = _names_of(c)
        colours = tuple(STATUS_MARKS.get(cls, NEUTRAL) for cls in (status_classes(names) or []))
        by_category = n_series <= 1
        return Scheme(rule=rule, series=() if by_category else colours,
                      categories=colours if by_category else (), by_category=by_category,
                      force_labels=True, by_name=dict(zip(names, colours)))

    if rule == "categorical":
        names = colour_keys(c)
        by_name: Dict[str, str] = {}
        for i, name in enumerate(names):
            if name in by_name:
                continue
            by_name[name] = (plan.names.get(name) if plan is not None else None) or pal[len(by_name) % len(pal)]
        colours = tuple(by_name[name] for name in names)
        by_category = n_series <= 1
        return Scheme(rule=rule, series=() if by_category else colours,
                      categories=colours if by_category else (), by_name=by_name, by_category=by_category)

    if rule == "ranking":
        values = list(c.series[0].values) if c.series else []
        leader = max(range(len(values)), key=lambda i: values[i]) if values else -1
        subject = _subject_colour(c, plan, pal)
        colours = tuple(subject if j == leader else NEUTRAL for j in range(len(cats)))
        return Scheme(rule=rule, series=(subject,), categories=colours, by_category=True)

    # "time" and "subject": one colour for the whole chart.
    subject = _subject_colour(c, plan, pal)
    return Scheme(rule=rule, series=(subject,) * max(1, n_series), categories=(subject,) * len(cats))


__all__ = [
    "CHART_PALETTE", "SUBJECT_SLOTS", "GAIN", "LOSS", "NEUTRAL", "GRID", "STATUS_MARKS",
    "Plan", "Scheme", "EMPTY", "plan_for", "scheme_for", "rule_for", "colour_keys", "subject_key",
    "status_classes", "ordinal_ramp", "to_oklch", "from_oklch", "contrast_ratio", "luminance", "as_chart",
]
