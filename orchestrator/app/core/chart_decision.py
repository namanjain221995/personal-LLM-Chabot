"""Deterministic chart decision engine (trusted backend code).

Two questions, both answered here and never by the model:

  1. *Should* this result be charted?  — `decide()`
  2. *Which* chart, over which columns? — `decide()` again, or `use_model`
     when an explicit request is genuinely ambiguous.

The backend has final authority. A model recommendation only ever survives
`parse_chart_spec` column validation, and only for the ambiguous-explicit
case; every automatic (hybrid) chart is built here from column metadata
alone.

Trigger modes (CHART_TRIGGER_MODE):
  explicit — today's behaviour: a chart appears only when the user asked.
  hybrid   — explicit requests still work, plus a small set of
             high-confidence shapes chart themselves.
There is deliberately no `automatic` mode.

Pure module: stdlib only (+ ChartSpec / ColumnProfile).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .chart_profile import ColumnProfile, profile_columns
from .chart_spec import ChartSpec

# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------

#: The historical trigger. Kept verbatim so `explicit` mode is bit-for-bit
#: the behaviour that shipped: chart | graph | plot | visualize | visualise |
#: visualization | visualisation.
LEGACY_CHART_RE = re.compile(
    r"\b(chart|graph|plot|visuali[sz]e|visuali[sz]ation)\b", re.I
)

#: Natural phrasings that mean "draw this" without using the word "chart".
_NATURAL_RE = re.compile(
    r"\b(show|showing|display|draw|render|represent|give me|make|create|turn)\b"
    r"[^.?!]{0,40}?\b("
    r"visually|graphically|as a picture|graphical(?:ly)?|"
    r"pictorial(?:ly)?|in a visual"
    r")\b",
    re.I,
)
_NAMED_CHART_RE = re.compile(
    r"\b(bar|column|line|pie|donut|doughnut|area|scatter|funnel|histogram|"
    r"trend|distribution)\s+(chart|graph|plot)\b",
    re.I,
)
_BARE_NAMED_RE = re.compile(r"\b(funnel|histogram)\b", re.I)

#: "plot" and "graph" are ordinary English. These readings are not requests
#: to draw anything, and firing on them turns a normal answer into a chart.
_FALSE_POSITIVE_RE = re.compile(
    r"\b(?:"
    r"plot\s+(?:twist|line|of\s+the\s+(?:book|film|movie|story|novel))|"
    r"(?:story|sub)plot|plotting\s+against|lost\s+the\s+plot|"
    r"graph\s*ql|knowledge\s+graph|graph\s+(?:database|theory|api)|"
    r"burial\s+plot|plot\s+of\s+land"
    r")\b",
    re.I,
)


def _strip_false_positives(message: str) -> str:
    return _FALSE_POSITIVE_RE.sub(" ", message or "")


#: Follow-ups that change a chart the user is already looking at. They name
#: no chart word at all ("make it horizontal"), so without these a
#: follow-up would silently answer with a table.
_MODIFIER_RE = re.compile(
    r"\b(?:"
    r"make\s+(?:it|them|that)\s+(?:horizontal|vertical|stacked|an?\s+\w+|"
    r"bars?|lines?|pies?)|"
    r"(?:un)?stack\s+(?:the\s+)?(?:series|bars|them|it)|"
    r"as\s+a\s+(?:bar|line|pie|donut|doughnut|area|scatter|funnel|histogram)\b|"
    r"switch\s+to\s+(?:a\s+)?(?:bar|line|pie|donut|doughnut|area|scatter)"
    r")\b",
    re.I,
)

#: "Show me the table instead." The user is telling us to stop drawing.
#: This beats every other signal, including hybrid mode and an explicit
#: chart word earlier in the same sentence.
_SUPPRESS_RE = re.compile(
    r"\b(?:"
    r"(?:just|only)\s+(?:the\s+)?(?:table|data|numbers)|"
    r"table\s+(?:only|instead)|"
    r"(?:remove|drop|hide|skip)\s+the\s+(?:chart|graph|plot|visuali[sz]ation)|"
    r"no\s+(?:chart|graph|plot)|"
    r"without\s+(?:a\s+|the\s+)?(?:chart|graph|plot)|"
    r"don'?t\s+(?:chart|graph|plot)"
    r")\b",
    re.I,
)


def chart_suppressed(message: str) -> bool:
    """True when the user asked NOT to see a chart."""
    return bool(_SUPPRESS_RE.search(message or ""))


def explicit_chart_request(message: str) -> bool:
    """True when the user asked, in words, to see a chart.

    Superset of the historical regex: it still fires on everything the old
    one did, plus named types ("funnel", "histogram") and natural phrasings
    ("show this visually"). Common non-visual uses of plot/graph are removed
    first so ordinary prose does not trigger a drawing.
    """
    text = _strip_false_positives(message)
    if LEGACY_CHART_RE.search(text):
        return True
    if _NAMED_CHART_RE.search(text) or _BARE_NAMED_RE.search(text):
        return True
    if _MODIFIER_RE.search(text):
        return True
    return bool(_NATURAL_RE.search(text))


#: Longest-first: "horizontal bar" must win over "bar", "donut chart" over
#: "chart". Each entry maps a phrase to a ChartType member.
_TYPE_PHRASES: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bhorizontal(?:\s+(?:bar|column))?\b", re.I), "horizontal_bar"),
    (re.compile(r"\b(?:donut|doughnut)\b", re.I), "donut"),
    (re.compile(r"\bfunnel\b", re.I), "funnel"),
    (re.compile(r"\bhistogram\b", re.I), "histogram"),
    (re.compile(r"\bdistribution\s+(?:chart|graph|plot)\b", re.I), "histogram"),
    (re.compile(r"\bscatter(?:\s*plot)?\b", re.I), "scatter"),
    (re.compile(r"\bpie(?:\s+chart)?\b", re.I), "pie"),
    (re.compile(r"\barea\s+(?:chart|graph|plot)\b", re.I), "area"),
    (re.compile(r"\bline\s+(?:chart|graph|plot)\b", re.I), "line"),
    (re.compile(r"\b(?:bar|column)\s+(?:chart|graph|plot)\b", re.I), "bar"),
    (re.compile(r"\bbar\s*graph\b", re.I), "bar"),
    # Follow-up phrasings, checked last so a named type above always wins:
    # "switch to a line", "make it bars", "as an area".
    (re.compile(r"\b(?:as|to|into)\s+(?:an?\s+)?area\b", re.I), "area"),
    (re.compile(r"\b(?:as|to|into)\s+(?:an?\s+)?line\b", re.I), "line"),
    (re.compile(r"\b(?:as|to|into)\s+(?:an?\s+)?bars?\b", re.I), "bar"),
    (re.compile(r"\bmake\s+(?:it|them|that)\s+(?:an?\s+)?lines?\b", re.I), "line"),
    (re.compile(r"\bmake\s+(?:it|them|that)\s+(?:an?\s+)?bars?\b", re.I), "bar"),
)


def requested_chart_type(message: str) -> Optional[str]:
    """The chart type the user named, if any. None means "they didn't say"."""
    text = _strip_false_positives(message)
    for pattern, ctype in _TYPE_PHRASES:
        if pattern.search(text):
            return ctype
    return None


_STACK_RE = re.compile(r"\bstack(?:ed|ing)?\b", re.I)


def requested_stacked(message: str) -> bool:
    return bool(_STACK_RE.search(_strip_false_positives(message or "")))


# ---------------------------------------------------------------------------
# Trusted stage orders (funnel)
# ---------------------------------------------------------------------------
#
# A funnel asserts an ORDER. Sorting stages alphabetically, or by value,
# would draw a confident lie: "Closed Lost" is not the first step of a sales
# process just because C sorts early. So a funnel is only ever built when
# every stage in the result belongs to ONE order we did not invent —
# Salesforce's own standard picklists, or an order the operator supplied.
#
# Operators whose org uses a custom sales process set:
#   CHART_FUNNEL_STAGE_ORDER='{"sales":["Discovery","Pilot","Signed"]}'
# and their order is trusted the same way.

STANDARD_STAGE_ORDERS: Dict[str, Tuple[str, ...]] = {
    # Salesforce standard Opportunity StageName picklist.
    "opportunity": (
        "Prospecting",
        "Qualification",
        "Needs Analysis",
        "Value Proposition",
        "Id. Decision Makers",
        "Perception Analysis",
        "Proposal/Price Quote",
        "Negotiation/Review",
        "Closed Won",
        "Closed Lost",
    ),
    # Salesforce standard Lead Status picklist.
    "lead": (
        "Open - Not Contacted",
        "Working - Contacted",
        "Closed - Converted",
        "Closed - Not Converted",
    ),
    # Salesforce standard Case Status picklist.
    "case": ("New", "Working", "Escalated", "Closed"),
}


def _load_custom_orders() -> Dict[str, Tuple[str, ...]]:
    raw = os.getenv("CHART_FUNNEL_STAGE_ORDER", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    out: Dict[str, Tuple[str, ...]] = {}
    if isinstance(parsed, list):  # a bare list is one unnamed order
        if all(isinstance(s, str) for s in parsed) and parsed:
            out["custom"] = tuple(parsed)
        return out
    if isinstance(parsed, dict):
        for name, seq in parsed.items():
            if isinstance(seq, list) and seq and all(isinstance(s, str) for s in seq):
                out[str(name)] = tuple(seq)
    return out


def stage_orders() -> Dict[str, Tuple[str, ...]]:
    """Standard picklists plus any operator-supplied order (env, read live)."""
    merged = dict(STANDARD_STAGE_ORDERS)
    merged.update(_load_custom_orders())
    return merged


def trusted_stage_order(labels: Sequence[str]) -> Optional[List[str]]:
    """Return `labels` in trusted order, or None when no order is trusted.

    Every distinct label must belong to a SINGLE known order. One unknown
    stage and the answer is None — a funnel missing a step, or with a step
    guessed into place, is worse than a bar chart.
    """
    seen = [str(x) for x in labels]
    distinct = {s.strip().casefold() for s in seen if s.strip()}
    if not distinct:
        return None
    for order in stage_orders().values():
        index = {s.casefold(): i for i, s in enumerate(order)}
        if distinct <= set(index):
            return sorted(seen, key=lambda s: index[s.strip().casefold()])
    return None


def can_funnel(profile: ColumnProfile, labels: Sequence[str]) -> bool:
    return trusted_stage_order(labels) is not None


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

#: Above this many categories a vertical bar's tick labels collide; a
#: horizontal bar reads fine well past it.
VERTICAL_BAR_MAX_CATEGORIES = 8
#: Long labels ("Global Media Holdings (EMEA)") never fit a vertical tick.
LONG_LABEL_CHARS = 16
#: Hard ceiling for any categorical chart. Beyond this it is a table.
MAX_CATEGORIES = 40
#: Part-to-whole is only honest with few slices.
MAX_PART_TO_WHOLE_CATEGORIES = 6


@dataclass
class ChartDecision:
    """What the trusted engine concluded. `use_model` is the only escape."""

    should_chart: bool
    chart_type: Optional[str] = None
    reason: str = ""
    confidence: float = 0.0
    x_key: Optional[str] = None
    y_keys: List[str] = field(default_factory=list)
    stacked: bool = False
    #: True only for an explicit request the engine could not resolve on
    #: its own — the caller may then ask the model for a spec.
    use_model: bool = False
    #: Explicit histogram requests carry the numeric source column; the
    #: caller re-bins the data in trusted code before building a spec.
    histogram_source: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "should_chart": self.should_chart,
            "chart_type": self.chart_type,
            "reason": self.reason,
            "confidence": round(self.confidence, 2),
        }


_NO = ChartDecision(should_chart=False, reason="no_chart")


def _pick_numeric(profiles: Sequence[ColumnProfile]) -> List[ColumnProfile]:
    return [p for p in profiles if p.is_numeric]


def _pick_dimension(profiles: Sequence[ColumnProfile]) -> List[ColumnProfile]:
    # NOT filtered on `all_distinct`: an aggregated result — the only kind
    # worth charting — has exactly one row per category, so every dimension
    # in it is all-distinct. Free text is already excluded upstream, where
    # `profile_column` classifies a long all-distinct column as "text"
    # rather than "categorical".
    return [p for p in profiles if p.kind in ("categorical", "boolean")]


def _pick_dates(profiles: Sequence[ColumnProfile]) -> List[ColumnProfile]:
    return [p for p in profiles if p.is_date]


def _bar_flavour(dim: ColumnProfile) -> str:
    if dim.unique > VERTICAL_BAR_MAX_CATEGORIES or dim.max_label_len > LONG_LABEL_CHARS:
        return "horizontal_bar"
    return "bar"


def _labels_of(columns: Sequence[str], rows: Sequence[Sequence], name: str) -> List[str]:
    try:
        i = list(columns).index(name)
    except ValueError:
        return []
    out = []
    for r in rows:
        try:
            out.append(str(r[i]))
        except (IndexError, KeyError, TypeError):
            continue
    return out


def decide(
    message: str,
    columns: Sequence[str],
    rows: Sequence[Sequence],
    mode: str = "explicit",
    profiles: Optional[Sequence[ColumnProfile]] = None,
    explicit_override: Optional[bool] = None,
) -> ChartDecision:
    """Decide whether and how to chart a result set.

    `mode` is CHART_TRIGGER_MODE. Anything other than "hybrid" behaves as
    "explicit" — an unrecognised value must never start drawing charts on
    its own.

    `explicit_override=True` means the *caller* has already established
    intent by some other route than the user's wording. The report engine
    uses it: its planner emits a per-section `"chart": true`, which is an
    explicit request even though the section instruction never says
    "chart".
    """
    columns = list(columns)
    rows = list(rows)
    explicit = (
        explicit_chart_request(message or "")
        if explicit_override is None
        else bool(explicit_override)
    )

    if not columns or not rows:
        return ChartDecision(False, reason="empty_result")

    # "Show me the table instead" outranks everything — an explicit chart
    # word earlier in the sentence, hybrid mode, even a report section's
    # `chart: true`. When someone says stop drawing, stop drawing.
    if chart_suppressed(message or ""):
        return ChartDecision(False, reason="chart_suppressed_by_request")

    profs = list(profiles) if profiles is not None else profile_columns(columns, rows)
    numeric = _pick_numeric(profs)
    dims = _pick_dimension(profs)
    dates = _pick_dates(profs)

    if explicit:
        return _decide_explicit(message, columns, rows, profs, numeric, dims, dates)
    if mode == "hybrid":
        return _decide_hybrid(columns, rows, profs, numeric, dims, dates)
    return ChartDecision(False, reason="not_requested")


def _decide_explicit(
    message: str,
    columns: Sequence[str],
    rows: Sequence[Sequence],
    profs: Sequence[ColumnProfile],
    numeric: Sequence[ColumnProfile],
    dims: Sequence[ColumnProfile],
    dates: Sequence[ColumnProfile],
) -> ChartDecision:
    named = requested_chart_type(message)
    stacked = requested_stacked(message)

    # --- the user named a type: honour it when the data can carry it -----
    if named == "histogram":
        if not numeric:
            return ChartDecision(
                False, reason="histogram_needs_a_numeric_column", confidence=1.0
            )
        return ChartDecision(
            True,
            "histogram",
            "explicit_histogram",
            1.0,
            histogram_source=numeric[0].name,
        )

    if named == "funnel":
        if not dims or not numeric:
            return ChartDecision(False, reason="funnel_needs_stage_and_metric", confidence=1.0)
        stage = next((d for d in dims if d.stage_named), dims[0])
        labels = _labels_of(columns, rows, stage.name)
        if trusted_stage_order(labels) is None:
            # Do not fabricate an order. Fall back to a ranked bar, which
            # claims nothing about sequence.
            return ChartDecision(
                True,
                "horizontal_bar",
                "funnel_requested_but_stage_order_not_trusted",
                0.6,
                stage.name,
                [numeric[0].name],
            )
        return ChartDecision(
            True, "funnel", "explicit_funnel_trusted_order", 1.0, stage.name, [numeric[0].name]
        )

    if named == "scatter":
        # Two numeric axes or nothing — a category string is not a coordinate.
        if len(numeric) >= 2:
            return ChartDecision(
                True, "scatter", "explicit_scatter", 1.0,
                numeric[0].name, [numeric[1].name],
            )
        return ChartDecision(False, reason="scatter_needs_two_numeric_columns", confidence=1.0)

    if named in ("pie", "donut"):
        if not dims or not numeric:
            return ChartDecision(False, reason="part_to_whole_needs_category_and_metric",
                                 confidence=1.0)
        metric = numeric[0]
        if metric.has_negative:
            return ChartDecision(
                True, _bar_flavour(dims[0]),
                "part_to_whole_requested_but_values_negative", 0.6,
                dims[0].name, [metric.name],
            )
        return ChartDecision(
            True, named, f"explicit_{named}", 1.0, dims[0].name, [metric.name]
        )

    if named in ("bar", "horizontal_bar", "line", "area"):
        x = dates[0] if (named in ("line", "area") and dates) else (
            dims[0] if dims else (dates[0] if dates else None)
        )
        if x is None or not numeric:
            return ChartDecision(False, reason="no_usable_axis_pair", confidence=1.0)
        return ChartDecision(
            True, named, f"explicit_{named}", 1.0,
            x.name, [n.name for n in numeric], stacked=stacked,
        )

    # --- the user asked for "a chart" without naming one -----------------
    unambiguous = _unambiguous_shape(profs, numeric, dims, dates, columns, rows)
    if unambiguous is not None:
        unambiguous.stacked = stacked or unambiguous.stacked
        return unambiguous
    # Ambiguous: several dimensions or several metrics and no obvious
    # pairing. This is exactly the case the existing model call is good at.
    return ChartDecision(True, None, "ambiguous_explicit_request", 0.0, use_model=True)


def _unambiguous_shape(
    profs: Sequence[ColumnProfile],
    numeric: Sequence[ColumnProfile],
    dims: Sequence[ColumnProfile],
    dates: Sequence[ColumnProfile],
    columns: Sequence[str],
    rows: Sequence[Sequence],
) -> Optional[ChartDecision]:
    """A single obvious reading of the result, or None.

    "Obvious" means one axis candidate and at least one metric. Anything
    that needs a judgement call is left to the model path.
    """
    if not numeric:
        return None

    # One time axis + metrics → a line. Ordering is a real signal, not a guess.
    if len(dates) == 1 and not dims:
        return ChartDecision(
            True, "line", "single_time_axis_with_metrics", 0.9,
            dates[0].name, [n.name for n in numeric],
        )

    # One dimension + metrics → bar, oriented by label length / cardinality.
    if len(dims) == 1 and not dates:
        dim = dims[0]
        if dim.unique > MAX_CATEGORIES:
            return None
        labels = _labels_of(columns, rows, dim.name)
        if (
            dim.stage_named
            and len(numeric) == 1
            and trusted_stage_order(labels) is not None
        ):
            return ChartDecision(
                True, "funnel", "stage_column_with_trusted_order", 0.9,
                dim.name, [numeric[0].name],
            )
        return ChartDecision(
            True, _bar_flavour(dim), "single_dimension_with_metrics", 0.85,
            dim.name, [n.name for n in numeric],
        )
    return None


def _decide_hybrid(
    columns: Sequence[str],
    rows: Sequence[Sequence],
    profs: Sequence[ColumnProfile],
    numeric: Sequence[ColumnProfile],
    dims: Sequence[ColumnProfile],
    dates: Sequence[ColumnProfile],
) -> ChartDecision:
    """Automatic charts. Four shapes, all deterministic, no model call.

    Everything else stays text + table. A wrong automatic chart is worse
    than no chart, so every rule here is narrow on purpose.
    """
    n_rows = len(rows)
    # One row is a scalar answer; hundreds of categories is a table.
    if n_rows < 2 or n_rows > 500:
        return ChartDecision(False, reason="row_count_outside_auto_range")
    if not numeric:
        return ChartDecision(False, reason="no_metric_to_plot")
    if len(numeric) > 1 and not dates:
        # Which of three numbers is the story? Only the user knows. A
        # multi-metric category comparison stays a table unless the x axis
        # is time, where plotting them together is the obvious reading.
        return ChartDecision(False, reason="ambiguous_metrics_for_auto_chart")

    # 1. Time series → line.
    if len(dates) == 1 and not dims and numeric:
        return ChartDecision(
            True, "line", "time_series", 0.9,
            dates[0].name, [n.name for n in numeric],
        )

    if len(dims) != 1 or dates or len(numeric) != 1:
        return ChartDecision(False, reason="no_high_confidence_shape")

    dim, metric = dims[0], numeric[0]
    if dim.unique != n_rows:
        # Each category must appear once — otherwise the result is not
        # aggregated and a bar would silently overplot.
        return ChartDecision(False, reason="result_not_aggregated_by_category")
    labels = _labels_of(columns, rows, dim.name)

    # 2. Salesforce stage data with a trusted order → funnel.
    if dim.stage_named and trusted_stage_order(labels) is not None:
        return ChartDecision(
            True, "funnel", "salesforce_stage_with_trusted_order", 0.9,
            dim.name, [metric.name],
        )

    # 3. Small, non-negative part-to-whole → donut.
    if (
        dim.unique <= MAX_PART_TO_WHOLE_CATEGORIES
        and not metric.has_negative
        and (metric.minimum or 0) >= 0
    ):
        return ChartDecision(
            True, "donut", "small_part_to_whole", 0.8, dim.name, [metric.name]
        )

    # 4. Category comparison → bar / horizontal_bar.
    if 2 <= dim.unique <= MAX_CATEGORIES:
        flavour = _bar_flavour(dim)
        return ChartDecision(
            True, flavour,
            "categorical_ranking_with_long_labels"
            if flavour == "horizontal_bar"
            else "category_comparison",
            0.85,
            dim.name, [metric.name],
        )
    return ChartDecision(False, reason="no_high_confidence_shape")


def build_spec(decision: ChartDecision, columns: Sequence[str], title: str = "") -> Optional[ChartSpec]:
    """Turn a trusted decision into a validated ChartSpec.

    Returns None unless every key the decision names is a real result
    column — the same invariant `parse_chart_spec` enforces for model
    output, applied to our own output too.
    """
    if not decision.should_chart or not decision.chart_type or not decision.x_key:
        return None
    colset = set(columns)
    if decision.x_key not in colset:
        return None
    y_keys = [y for y in decision.y_keys if y in colset]
    if not y_keys:
        return None
    try:
        return ChartSpec(
            type=decision.chart_type,
            x_key=decision.x_key,
            y_keys=y_keys,
            title=title,
            stacked=decision.stacked,
        )
    except Exception:
        return None
