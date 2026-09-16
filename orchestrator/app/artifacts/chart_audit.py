"""A SECOND reading of a chart that is already computed, for the self-check.

WHY THIS IS NOT chart_data. selfcheck.chart_values_check verifies a chart by
calling chart_data.recompute_matches, which re-runs chart_data.compute over
the same binding and compares the answer with itself. A WRONG binding is
self-consistent, so that comparison passes every time: production 2026-09-16
drew a pie of six states as seven equal 14 % slices (a row count over a table
that already held one row per state) and the check reported the values as
matching. Nothing here calls compute, resolve_chart or recompute_matches.
Only PARSERS and RULES ABOUT ROWS are borrowed from chart_data — `to_number`
(so "~67" and "1,200" read as figures, exactly as the cells were read),
`to_date` (to recognise a date column and stand aside), `_TOTAL_LABEL_RE`,
the Hindi/Gujarati summary vocabulary, `_is_total_row` (WHICH ROWS THE
COMPUTATION DROPPED: a second reading that dropped a different set would
report a mismatch against itself, measured 2026-09-16) and the name of the
`Other` fold. Forking any of them would make this a different question.

THREE CHECKS, all deterministic, no model:

  binding           A part-of-whole chart (pie, donut) with >= 3 categories
                    whose every slice carries the SAME value, over a table
                    that holds a measure column with different values per
                    row. Equal slices are then a fact about the binding, not
                    about the data.
  summary_category  A category that says it is a summary — total, grand
                    total, subtotal, sum, overall, all <x>, distinct <x>,
                    कुल, योग, કુલ, સરવાળો — drawn as a slice or a bar. It is
                    the sum of the others; beside them every figure counts
                    twice. Not on a waterfall or a funnel, whose closing or
                    opening bar IS the chart, and a QUALIFIED word ("All
                    Saints Hospital") only when the qualifier names the
                    chart's own dimension or the arithmetic agrees.
  values            The plotted numbers against a plain-python regrouping of
                    the bound table's rows: group the x column, apply the
                    aggregate to the y column, compare. Deliberately narrow —
                    a binding this module cannot reproduce exactly (a filter,
                    a group split, a date bucket, a numeric x) is reported as
                    UNVERIFIABLE, never as a mismatch, so the check never
                    invents a failure it does not understand.

Every finding says whether a re-bind + re-render is the mechanical fix
(`mechanical`), because that is what selfcheck.plan_repair can actually do.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import chart_spec as CS
from .chart_data import OTHER, _TOTAL_LABEL_RE, _is_total_row, to_date, to_number

log = logging.getLogger(__name__)

#: A plotted value and a regrouped one are the same number when they agree to
#: this much: the chart carries float64 sums of the same cells, so only the
#: last bits may differ.
EPSILON = 1e-6

#: Types whose categories are groups of rows, so a summary row drawn beside
#: them double-counts. `box` joins them for the same reason chart_data does.
#:
#: WATERFALL AND FUNNEL ARE EXCLUDED. Their closing/opening bar IS the chart:
#: a waterfall of Opening/Sales/Costs/Total is drawn precisely to land on that
#: Total, and a funnel's top stage ("Total Visits") is the mouth of the
#: funnel. Measured 2026-09-16 with them included: a document whose only
#: chart was the literal waterfall [100, 50, -30, 120] published as
#: `completed_with_warnings`, and the repair (below) REPLACED the chart with
#: a callout — the person asked for a chart and the self-check deleted it.
_SUMMARY_SENSITIVE_TYPES: Tuple[str, ...] = tuple(
    t for t in CS.AGGREGATING_TYPES if t not in ("waterfall", "funnel")) + ("box",)

#: The summary words _TOTAL_LABEL_RE does not carry, in two halves, because
#: they are not equally safe. DECISIVE on their own: a bare "sum", "sum of
#: X", "combined", "everything", a bare "all". Only ever matched against a
#: label of at most 40 characters (is_summary_label checks that first), so
#: the alternation cannot be walked into a backtracking cost.
_SUMMARY_PLAIN_RE = re.compile(
    r"^(?:sum(?:\s*of\s+.{1,30})?|all|combined|everything)\s*:?$",
    re.IGNORECASE,
)

#: QUALIFIED: the shape that made the 2026-09-16 pie wrong — a state column
#: whose summary row read "Distinct States". The qualifier is what makes it a
#: summary OF something, and it is also what ordinary business names look
#: like: measured, this alternation alone calls "All Saints Hospital", "Total
#: Rewards", "Distinct Designs Ltd" and "Unique Fitness" summaries. A drawn
#: category is therefore only flagged when the qualifier names the chart's
#: own grouping dimension ("Distinct States" over x="State") or when its
#: value really is the sum of the other categories — see _summary_categories.
_SUMMARY_QUALIFIED_RE = re.compile(
    r"^(?:all|distinct|unique|total)\s+(?P<of>[\w\s.'-]{1,30}?)\s*:?$",
    re.IGNORECASE,
)

#: Columns that number the rows rather than measure them. DELIBERATELY
#: narrower than chart_data._NON_MEASURE_RE, which also excludes a percentage
#: or share column: for the binding check a "% of Total" column that differs
#: row by row is perfectly good evidence that the categories are not equal,
#: even though it is a poor thing to plot.
_NOT_A_MEASURE_RE = re.compile(
    r"^(?:rank|ranking|sr\.?\s*no\.?|s\.?\s*no\.?|sr\.?|no\.?|#|index|serial|position|row|id|code)\s*\.?$",
    re.IGNORECASE,
)


@dataclass
class Finding:
    code: str            # binding | summary_category | values
    message: str         # the plain sentence a person can act on
    chart: str = ""      # the chart's title, for the message
    mechanical: bool = False  # a re-bind + re-render fixes it


@dataclass
class ChartAudit:
    findings: List[Finding] = field(default_factory=list)
    charts: int = 0
    #: True when the spec holds a chart the matching check can speak about at
    #: all — what selfcheck gates its checklist items on.
    has_part_of_whole: bool = False
    has_summary_sensitive: bool = False
    #: match | mismatch | unverifiable, over every chart that was regrouped.
    recomputed: str = "unverifiable"

    def codes(self) -> List[str]:
        return [f.code for f in self.findings]

    def messages(self, code: str) -> List[str]:
        return [f.message for f in self.findings if f.code == code]

    def mechanical(self, code: str) -> bool:
        return any(f.mechanical for f in self.findings if f.code == code)


# ------------------------------------------------------------- accessors --


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _columns(table: Any) -> List[str]:
    return [str(c) for c in (_attr(table, "columns", []) or [])]


def _rows(table: Any) -> List[Sequence[Any]]:
    return [r for r in (_attr(table, "rows", []) or []) if isinstance(r, (list, tuple))]


def _fold(name: Any) -> str:
    return " ".join(str(name or "").split()).casefold()


def _cell(row: Sequence[Any], idx: Optional[int]) -> Any:
    if idx is None or idx < 0 or idx >= len(row):
        return None
    return row[idx]


def _label(value: Any) -> str:
    """The text chart_data draws under a text category: whitespace collapsed
    and clipped at 80, so a comparison by label lines up with the chart."""
    return " ".join(str(value).split())[:80]


def _column_index(table: Any, name: Optional[str]) -> Optional[int]:
    """The named column, matched EXACTLY once it is folded. chart_data also
    matches fuzzily; guessing here would let this reading agree with a
    mis-resolved binding, which is the whole thing being checked."""
    if not name:
        return None
    want = _fold(name)
    hits = [i for i, c in enumerate(_columns(table)) if _fold(c) == want]
    return hits[0] if len(hits) == 1 else None


# ---------------------------------------------------------- summary rows --


def _clean_label(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"[*_`]", "", value).strip()
    return text if text and len(text) <= 40 else ""


def is_summary_label(value: Any) -> bool:
    """"Total", "Grand Total", "Sum", "All regions", "Distinct States", कुल …

    The VOCABULARY, and nothing else. Whether a drawn category that carries a
    qualified summary word is really a summary is decided against the chart
    it was drawn in (check_summary_categories), because the same words spell
    ordinary names — "All Saints Hospital", "Total Rewards"."""
    text = _clean_label(value)
    if not text:
        return False
    return bool(_TOTAL_LABEL_RE.match(text) or _SUMMARY_PLAIN_RE.match(text) or _SUMMARY_QUALIFIED_RE.match(text))


def _qualifier(value: Any) -> Optional[str]:
    """The "<x>" of "all <x>" / "distinct <x>" / "unique <x>" / "total <x>",
    when that qualified form is the ONLY reason the label reads as a summary.
    None when a decisive word ("Total", "Sum", कुल) already settled it."""
    text = _clean_label(value)
    if not text or _TOTAL_LABEL_RE.match(text) or _SUMMARY_PLAIN_RE.match(text):
        return None
    m = _SUMMARY_QUALIFIED_RE.match(text)
    return m.group("of") if m else None


def _is_summary_row(row: Sequence[Any]) -> bool:
    """chart_data's OWN rule, deliberately.

    The regrouping below must drop exactly the rows chart_data dropped, or it
    reports a mismatch against itself. Measured 2026-09-16 with the widened
    vocabulary here: a Month|Metric|Value table whose Metric cells read
    "Total Revenue" lost four of its six rows in this reading only, and a
    correct bar of [110, 85, 64] was reported as "Jan is drawn as 110; the
    sum of Value over the table's rows is 10". The widened vocabulary stays
    where the contract puts it — over the FINAL chart.categories."""
    return _is_total_row(row)


# ------------------------------------------------------ (a) binding check --


def _plotted_values(chart: CS.Chart) -> List[float]:
    if len(chart.series) != 1:
        return []
    return [float(v) for v in (chart.series[0].values or [])]


def _all_equal(values: Sequence[float]) -> bool:
    if len(values) < 3:
        return False
    first = values[0]
    return all(math.isclose(v, first, rel_tol=EPSILON, abs_tol=1e-9) for v in values)


def _measure_columns_with_spread(chart: CS.Chart, table: Any) -> List[Tuple[str, List[float]]]:
    """Every column of the table, other than the ones the chart groups by,
    that reads as numbers and does NOT hold the same number on every row."""
    binding = chart.data
    taken = {_column_index(table, binding.x if binding else None), _column_index(table, binding.group_by if binding else None)}
    rows = [r for r in _rows(table) if not _is_summary_row(r)]
    out: List[Tuple[str, List[float]]] = []
    for idx, name in enumerate(_columns(table)):
        if idx in taken or _NOT_A_MEASURE_RE.match(name.strip()):
            continue
        numbers = [to_number(_cell(r, idx)) for r in rows]
        present = [n for n in numbers if n is not None]
        if len(present) < 3 or len(present) < 0.6 * len(rows):
            continue
        if len({round(n, 9) for n in present}) > 1:
            out.append((name, present))
    return out


def _own_column_is_flat(chart: CS.Chart, table: Any) -> bool:
    """The chart NAMES one numeric column of the table and that column really
    does hold the same number on every row, so the equal slices ARE the data.

    Measured 2026-09-16 without this: Team|Members|Founded =
    [[Alpha, 5, 2011], [Beta, 5, 2015], [Gamma, 5, 2019]] with a correct pie
    of Members [5, 5, 5] was reported as "it is bound to the wrong column",
    naming the YEAR column as the one it should have used. A year passes
    every part of the guard the report claimed — three categories, one
    series, a name that is not a row number."""
    b = chart.data
    if b is None or len(b.y) != 1:
        return False
    idx = _column_index(table, b.y[0])
    if idx is None:
        return False
    present = [n for n in (to_number(_cell(r, idx)) for r in _rows(table) if not _is_summary_row(r)) if n is not None]
    return len(present) >= 3 and len({round(n, 9) for n in present}) == 1


def check_binding(chart: CS.Chart, table: Any) -> List[Finding]:
    """A pie of >= 3 equal slices over a table whose measure column is not
    flat is a binding bug, not a finding about the data."""
    if chart.type not in CS.PART_OF_WHOLE_TYPES or chart.data is None:
        return []
    values = _plotted_values(chart)
    if len(chart.categories) < 3 or len(values) != len(chart.categories) or not _all_equal(values):
        return []
    if _own_column_is_flat(chart, table):
        return []
    spread = _measure_columns_with_spread(chart, table)
    if not spread:
        return []
    name, present = spread[0]
    shown = ", ".join(_fmt(n) for n in present[:4]) + ("…" if len(present) > 4 else "")
    title = chart.title or "the chart"
    return [Finding(
        "binding",
        f"{title}: all {len(values)} slices are {_fmt(values[0])}, so the chart says nothing about the data, "
        f"while {name} in the table it is drawn from differs row by row ({shown}) — it is bound to the wrong column",
        chart=title,
        mechanical=True,
    )]


def _fmt(v: float) -> str:
    return f"{int(round(v)):,}" if abs(v - round(v)) < 1e-9 else f"{v:,.2f}".rstrip("0").rstrip(".")


# ----------------------------------------------- (b) summary-label check --


def _word_forms(word: str) -> set:
    """{"states", "state"}, {"boxes", "box"}, {"cities", "city"} — every
    reading of one word, so a PLURAL qualifier lines up with the SINGULAR
    column name it was written from ("Distinct States" over x="State").
    English is irregular enough that guessing one form is wrong either way
    ("states" minus "es" is "stat"), so both candidates are kept."""
    forms = {word}
    if word.endswith("ies") and len(word) > 4:
        forms.add(word[:-3] + "y")
    if word.endswith("es") and len(word) > 3:
        forms.add(word[:-2])
    if word.endswith("s") and len(word) > 2:
        forms.add(word[:-1])
    return forms


def _dimension_words(chart: CS.Chart) -> set:
    """What this chart says it is ABOUT, in its own words: the columns it
    groups by and measures, and its series names, folded and singularised."""
    names: List[str] = []
    b = chart.data
    if b is not None:
        names += [b.x or "", b.group_by or ""] + [str(y) for y in (b.y or [])]
    names += [str(s.name or "") for s in chart.series]
    out: set = set()
    for n in names:
        folded = _fold(n)
        if folded:
            out |= _word_forms(folded)
    return out


def _is_the_sum_of_the_others(index: int, values: Sequence[float]) -> bool:
    """The drawn value at `index` equals the other drawn values added up —
    the arithmetic that makes a summary slice a double count."""
    if len(values) < 3:
        return False
    others = [v for i, v in enumerate(values) if i != index]
    total = float(sum(others))
    return abs(values[index] - total) <= max(EPSILON * abs(total), 1e-9)


def _summary_categories(chart: CS.Chart, labels: Sequence[Any], values: Sequence[float]) -> List[Any]:
    """The labels of `labels` that are really a summary of the others.

    A decisive word ("Total", "Grand Total", "Sum", कुल) needs no support. A
    QUALIFIED one ("Distinct States", "All regions") must either name the
    chart's own dimension — x="State" for "Distinct States", which is the
    2026-09-16 incident — or be the arithmetic sum of the other categories.
    Without that support the same words are ordinary names, measured:
    "All Saints Hospital", "Total Rewards", "Distinct Designs Ltd",
    "Unique Fitness" all match the vocabulary."""
    dims = _dimension_words(chart)
    out: List[Any] = []
    for i, label in enumerate(labels):
        if not is_summary_label(label):
            continue
        of = _qualifier(label)
        if of is not None and not (_word_forms(_fold(of)) & dims) and not (
                i < len(values) and _is_the_sum_of_the_others(i, values)):
            continue
        out.append(label)
    return out


def check_summary_categories(chart: CS.Chart) -> List[Finding]:
    if chart.type not in _SUMMARY_SENSITIVE_TYPES:
        return []
    named = _summary_categories(chart, list(chart.categories), _plotted_values(chart))
    if chart.extra is not None:
        boxes = [b.name for b in (chart.extra.box or [])]
        named += _summary_categories(chart, boxes, [])
    if not named:
        return []
    title = chart.title or "the chart"
    word = "slice" if chart.type in CS.PART_OF_WHOLE_TYPES else "bar"
    return [Finding(
        "summary_category",
        f"{title}: {', '.join(sorted(set(named))[:3])} is a summary of the other rows, not a {word} of its own — "
        f"drawn beside them every figure is counted twice",
        chart=title,
        mechanical=True,
    )]


# ------------------------------------------------- (c) recomputation check --

#: The bindings this module can reproduce with a dict and a for loop. Anything
#: else is UNVERIFIABLE here, never a mismatch.
_SIMPLE_AGGS = ("sum", "avg", "count", "min", "max", "median", "none")


def _effective_binding(chart: CS.Chart, table: Any) -> Tuple[Optional[str], Optional[str]]:
    """(agg, y column) as the FINISHED chart describes itself.

    chart_data may rebind before it computes (2026-09-16: agg="count" over a
    one-row-per-category table becomes a sum of the measure column) and it
    writes the binding it actually used into provenance.agg and into the
    series name, while chart.data keeps the words the model wrote. Reading
    the chart's own account of itself and then checking it against the table
    is what makes this a second opinion rather than a rerun.
    """
    binding = chart.data
    if binding is None:
        return None, None
    prov = chart.provenance
    agg = str(prov.agg) if prov is not None and prov.agg else str(binding.agg)
    if agg == "none":
        agg = "sum"
    if agg == "count":
        return agg, None
    named = list(binding.y)
    if len(chart.series) == 1 and _column_index(table, chart.series[0].name) is not None:
        named = [chart.series[0].name]
    return (agg, named[0]) if len(named) == 1 else (agg, None)


def _too_complex(chart: CS.Chart) -> bool:
    b = chart.data
    if b is None:
        return True
    return bool(
        chart.type not in CS.AGGREGATING_TYPES
        or b.group_by or b.filters or b.date_bucket or b.bins or b.y2
        or len(b.y) > 1 or len(chart.series) != 1
    )


def _text_keys(table: Any, x_idx: int) -> Optional[List[Tuple[str, str]]]:
    """[(fold, label)] per non-summary row, or None when the x column is not
    plain text — chart_data buckets dates and formats numbers into labels
    this module does not reproduce, so it stands aside."""
    out: List[Tuple[str, str]] = []
    for row in _rows(table):
        if _is_summary_row(row):
            continue
        cell = _cell(row, x_idx)
        if cell is None or not str(cell).strip():
            return None
        if to_number(cell) is not None or to_date(cell) is not None:
            return None
        label = _label(cell)
        out.append((label.casefold(), label))
    return out or None


def _apply(agg: str, values: List[float]) -> Optional[float]:
    if agg == "sum":
        return float(sum(values))
    if not values:
        # An average/min/max/median of no rows is not 0; chart_data draws 0
        # there and says so in a note. Nothing to compare: stand aside.
        return None
    if agg == "avg":
        return float(sum(values)) / len(values)
    if agg == "min":
        return float(min(values))
    if agg == "max":
        return float(max(values))
    if agg == "median":
        ordered = sorted(values)
        mid = len(ordered) // 2
        return float(ordered[mid]) if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
    return None


def recompute(chart: CS.Chart, table: Any) -> Tuple[str, List[str]]:
    """(match | mismatch | unverifiable, messages). Plain python: group the
    x column, apply the aggregate to the y column, compare with the chart."""
    if chart.data is None or _too_complex(chart):
        return "unverifiable", []
    x_idx = _column_index(table, chart.data.x)
    if x_idx is None:
        return "unverifiable", []
    keys = _text_keys(table, x_idx)
    if keys is None:
        return "unverifiable", []
    agg, ycol = _effective_binding(chart, table)
    if agg not in _SIMPLE_AGGS:
        return "unverifiable", []
    y_idx: Optional[int] = None
    if agg != "count":
        y_idx = _column_index(table, ycol)
        if y_idx is None:
            return "unverifiable", []
    plotted = _plotted_values(chart)
    if len(plotted) != len(chart.categories):
        return "unverifiable", []

    groups: Dict[str, List[float]] = {}
    counts: Dict[str, int] = {}
    unreadable = False
    rows = [r for r in _rows(table) if not _is_summary_row(r)]
    for (fold, _label_text), row in zip(keys, rows):
        counts[fold] = counts.get(fold, 0) + 1
        if y_idx is None:
            continue
        number = to_number(_cell(row, y_idx))
        if number is None:
            unreadable = True
            continue
        groups.setdefault(fold, []).append(number)
    if agg != "count" and unreadable:
        # A cell this reading cannot parse would make every comparison in its
        # group meaningless: say nothing rather than something wrong.
        return "unverifiable", []

    title = chart.title or "the chart"
    unit = "rows" if agg == "count" else f"{agg} of {ycol}"
    diffs: List[str] = []
    compared = 0
    for category, drawn in zip(chart.categories, plotted):
        fold = _fold(category)
        if fold == _fold(OTHER):
            # chart_data folds the tail of a pie into a bucket it CALLS
            # "Other" (PIE_MAX_SLICES = 7). A table row that happens to be
            # labelled "Other" is then compared with that fold: measured
            # 2026-09-16 on a ten-category spend table, "Other is drawn as
            # 282; the sum of Spend over the table's rows is 3" — a correct
            # chart told its numbers are wrong.
            continue
        if fold not in counts:
            # "Other", a top_n trim, a renamed category: not this check's
            # business, and never a failure invented from an absence.
            continue
        mine = float(counts[fold]) if agg == "count" else _apply(agg, groups.get(fold, []))
        if mine is None:
            continue
        compared += 1
        if not math.isclose(drawn, mine, rel_tol=EPSILON, abs_tol=1e-9):
            diffs.append(f"{title}: {category} is drawn as {_fmt(drawn)}; the {unit} over the table's rows is {_fmt(mine)}")
    if not compared:
        return "unverifiable", []
    return ("mismatch", diffs[:5]) if diffs else ("match", [])


# ------------------------------------------------------------------ audit --


def audit_chart(chart: Any, table: Any) -> Tuple[List[Finding], str]:
    """(findings, recomputed) for one resolved chart over the table it names."""
    c = chart if isinstance(chart, CS.Chart) else CS.Chart.model_validate(
        chart.model_dump() if hasattr(chart, "model_dump") else chart)
    findings = check_summary_categories(c)
    if table is None:
        return findings, "unverifiable"
    findings += check_binding(c, table)
    state, diffs = recompute(c, table)
    title = c.title or "the chart"
    findings += [Finding("values", m, chart=title, mechanical=True) for m in diffs]
    return findings, state


def audit_spec(spec: Any, tables: Sequence[Any] = ()) -> ChartAudit:
    """Every chart of the spec against the table it is bound to. Never
    raises: a chart this module cannot read is one it says nothing about."""
    audit = ChartAudit()
    states: List[str] = []
    for sheet_rows, chart in _charts_of(spec):
        try:
            c = chart if isinstance(chart, CS.Chart) else CS.Chart.model_validate(
                chart.model_dump() if hasattr(chart, "model_dump") else chart)
        except Exception:  # noqa: BLE001 — an unreadable chart is not a finding
            log.debug("chart_audit: a chart could not be read", exc_info=True)
            continue
        audit.charts += 1
        audit.has_part_of_whole = audit.has_part_of_whole or (c.type in CS.PART_OF_WHOLE_TYPES and c.data is not None)
        audit.has_summary_sensitive = audit.has_summary_sensitive or c.type in _SUMMARY_SENSITIVE_TYPES
        try:
            findings, state = audit_chart(c, _table_for(c, tables, sheet_rows))
        except Exception:  # noqa: BLE001 — the self-check never fails a job
            log.warning("chart_audit: %r could not be audited", (c.title or "")[:40], exc_info=True)
            continue
        audit.findings.extend(findings)
        states.append(state)
    if "mismatch" in states:
        audit.recomputed = "mismatch"
    elif "match" in states:
        audit.recomputed = "match"
    return audit


def _table_for(chart: CS.Chart, tables: Sequence[Any], sheet_table: Any) -> Any:
    """The table the chart names, by id or title. Exact matches only: a
    close-enough guess would let this reading inherit the binding's mistake."""
    if chart.data is None:
        return None
    wanted = str(chart.data.table_id or "")
    if not wanted:
        return sheet_table if sheet_table is not None else (tables[0] if len(tables) == 1 else None)
    for t in list(tables) + ([sheet_table] if sheet_table is not None else []):
        if t is None:
            continue
        if str(_attr(t, "id", "")) == wanted or _fold(_attr(t, "id", "")) == _fold(wanted) or _fold(_attr(t, "title", "")) == _fold(wanted):
            return t
    # A sheet chart names its own sheet ("sheet:Data"); the provenance says so.
    if sheet_table is not None and wanted.startswith("sheet:"):
        return sheet_table
    return None


def _charts_of(spec: Any):
    """(the sheet's own rows as a table or None, chart) for every chart.

    The same walk selfcheck._charts_with_sheets makes; kept here so this
    module can be used (and tested) without the self-check around it."""
    try:
        body = spec.body
    except Exception:  # noqa: BLE001
        return
    for b in list(getattr(body, "blocks", None) or []):
        if getattr(b, "type", "") == "chart" and getattr(b, "chart", None) is not None:
            yield None, b.chart
    for sl in list(getattr(body, "slides", None) or []):
        if getattr(sl, "chart", None) is not None:
            yield None, sl.chart
    for sh in list(getattr(body, "sheets", None) or []):
        charts = list(getattr(sh, "charts", None) or [])
        if not charts:
            continue
        # spec.Sheet.columns are Column models, not strings — the same
        # unwrapping chart_data._sheet_table does.
        own = {"id": f"sheet:{getattr(sh, 'name', '')}", "title": str(getattr(sh, "name", "")),
               "columns": [str(c.get("name") if isinstance(c, dict) else getattr(c, "name", c)) for c in (getattr(sh, "columns", None) or [])],
               "rows": [list(r) for r in (getattr(sh, "rows", None) or [])]}
        for c in charts:
            yield own, c


__all__ = ["EPSILON", "Finding", "ChartAudit", "audit_chart", "audit_spec", "check_binding",
           "check_summary_categories", "recompute", "is_summary_label"]
