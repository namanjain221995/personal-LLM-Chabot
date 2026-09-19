"""Which chart the data can honestly carry — decided by code, not the model.

WHY THIS EXISTS. The model picks the chart type. When the person names one
("pie chart") it usually obeys, and when they do not it guesses; nothing read
the table to check either answer. Production 2026-09-16: "visualise this table
on pie chart" over a six-state count table drew seven equal 14% slices, and no
sentence anywhere said what had been drawn or why.

THE TWO JOBS.
  - The person named a type -> it WINS whenever the shape can carry it. When
    it cannot (a pie of 40 categories, a pie of a time series, a funnel whose
    stages grow), the fallback note names BOTH types and the reason, so the
    person is never handed a different picture in silence.
  - Nobody named a type -> `recommend` decides from the shape alone. The
    model's guess is kept only when it is in the same family as the
    recommendation (a horizontal bar where a bar was recommended), because a
    family swap is a layout preference, not a different reading of the data.

SHAPE, NOT SEMANTICS. Everything here comes from column kinds
(chart_data.infer_column), distinct counts, row counts, the sign of the
values, the measure's name and the binding's own extra columns. Nothing is
guessed from the subject matter, so the answer is reproducible and the reason
string is always true of the table in front of it.

NO MODULE-LEVEL IMPORT OF chart_data: chart_data imports this module to run
the chooser inside repair_binding, so the table helpers are imported lazily.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import chart_spec as CS

# ------------------------------------------------------------- thresholds --

#: A pie the CHOOSER picks on its own. Seven wedges are already hard to
#: compare by angle, so an unrequested pie stays well inside that
#: (core.chart_decision.MAX_PART_TO_WHOLE_CATEGORIES is the same number for
#: the same reason).
AUTO_PIE_CATEGORIES = 6

#: A pie the PERSON asked for. chart_data draws at most PIE_MAX_SLICES (7)
#: wedges and folds the tail into "Other", so beyond twice that the picture
#: hides more categories than it shows and stops being the thing they asked
#: for; below it the fold is a detail the existing note already reports.
NAMED_PIE_CATEGORIES = 13

#: Above this a vertical bar's tick labels collide (the value
#: core.chart_decision.VERTICAL_BAR_MAX_CATEGORIES uses), and long labels
#: never fit a vertical tick at any count.
VERTICAL_BAR_CATEGORIES = 8
LONG_LABEL_CHARS = 16

#: A trend line is drawn only when the straight-line fit says something: at
#: |r| < 0.5 it is a line through a cloud, which reads as a finding that is
#: not in the data.
TREND_MIN_R = 0.5
TREND_SAMPLE = 5_000

#: A heatmap is a matrix. Fewer rows or columns than this is a grouped bar
#: with extra ink, and a sparse grid is mostly empty cells.
MIN_HEATMAP_SIDE = 3
MIN_MATRIX_FILL = 0.6

#: A box plot summarises a distribution: it needs several rows per category
#: to have quartiles at all.
MIN_BOX_ROWS_PER_CATEGORY = 5

#: Rows read when the chooser needs the values themselves (signs, trend fit,
#: matrix density). Evenly spread, so a 2M-row table costs milliseconds —
#: the chooser runs before compute, on the same thread.
VALUE_SAMPLE = 4_000

#: Distinct x labels are counted up to here; past it the exact count only
#: has to be "more than any categorical chart can use".
DISTINCT_CAP = 400

# ------------------------------------------------------------ measure kind --

#: A measure whose name says it is already a share of a whole. These DO add
#: up to one whole, so a pie of them is honest.
_PERCENT_RE = re.compile(r"(%|\bpercent(?:age)?\b|\bpct\b|\bshare\b|\bproportion\b)", re.I)

#: A measure that does NOT add up across categories: averaging averages, or
#: summing "days to resolve", produces a number that means nothing, so a
#: part-of-a-whole chart of it is a lie however few categories there are.
_NON_ADDITIVE_RE = re.compile(
    r"\b(avg|average|mean|median|rate|ratio|score|index|margin|price|per\s+\w+|"
    r"days?|hours?|hrs?|minutes?|mins?|seconds?|secs?|duration|age|latency|time\s+taken|"
    r"temperature|temp|balance|level)\b",
    re.I,
)
#: A numeric x that only NAMES the row — a year, a rank, an id. Reading it
#: as the second measure of a scatter draws "revenue against 2024" as a
#: cloud; it is an ordered category. chart_data._NON_MEASURE_RE is the same
#: list, used there to keep such a column off the y axis.
_PERIOD_RE = re.compile(r"^(year|yr|fy|month|week|quarter|qtr|day|period)\b", re.I)

_DURATION_RE = re.compile(
    r"\b(days?|hours?|hrs?|minutes?|mins?|seconds?|secs?|duration|age|latency|time\s+taken|elapsed|turnaround|tat)\b",
    re.I,
)

# ------------------------------------------------------- the named type --

#: An optional chart word after a type's own noun. "funnel chart" is the
#: person naming a type; "sales funnel" is a subject. The suffix is what
#: lets the scoring below tell the two apart by the length of the match.
_CW = r"(?:\s+(?:chart|graph|plot))?"

#: A chart word INSIDE the matched text. Measured 2026-09-16 on the merged
#: tree: with first-pattern-wins, named_type("show our sales funnel as a bar
#: chart") returned "funnel" and CC.choose drew a funnel with NO note — the
#: person typed "bar chart". Substrings, not whole words: "waterfall"
#: carries "fall", "heat map" carries "map", "box and whisker" carries
#: "whisker".
_QUALIFIED_RE = re.compile(r"chart|graph|plot|map|fall|stacked|horizontal|whisker", re.I)

#: "Draw it AS this type." These lead-ins introduce the type the person is
#: ASKING for and nothing else, so a noun one of them introduces beats every
#: bare noun in the same sentence.
#:
#: WHY A SECOND SIGNAL. The chart word above closed the hole only for the
#: type nouns that carry one of its substrings — heat map, waterfall,
#: stacked bar, horizontal bar, and any noun written with "chart"/"graph"/
#: "plot". It left it open for every noun that does not, and measured
#: 2026-09-16 on the first fix round named_type("show our sales funnel as a
#: pie") still returned "funnel" (and the same for donut and gantt: the
#: subject noun is simply the longer string). A lead-in is what makes the
#: rule UNIFORM — it reads the same for pie, donut, gantt, box, radar and
#: every other name, because it looks at the words in front of the noun
#: instead of the letters inside it.
_CONVERT_LEAD = (
    r"(?:\bas|\bto|\binto|\blike|\bmake\s+(?:it|them|this|that)|"
    r"\bturn\s+(?:it|them|this|that)\s+into|\bredraw\s+(?:it|them|this|that)\s+as|"
    r"\bswitch\s+to|\bchange\s+to|\bconvert\s+to)"
)

#: A request verb handing the noun its own article: "draw a pie", "show me a
#: gantt". Weaker than a conversion lead-in — "show the funnel" can still be
#: the subject of the sentence — but stronger than a noun sitting inside a
#: compound ("our sales funnel", "the housing bubble", "a text box").
_ASK_LEAD = (
    r"(?:\bdraw|\bshow(?:\s+me)?|\brender|\bplot|\bgive\s+me|\bcreate|\bgenerate|"
    r"\bbuild|\bmake|\buse|\bwant|\bneed|\bprefer|\badd|\bvisuali[sz]e)"
)

#: The lead-in sitting immediately before the noun ("... as a |pie").
_CONVERT_BEFORE_RE = re.compile(_CONVERT_LEAD + r"\s+(?:an?\s+|the\s+)?$", re.I)
#: The lead-in the follow-up rows below already carry inside their own match
#: ("|as a bar|").
_CONVERT_INSIDE_RE = re.compile(r"\s*" + _CONVERT_LEAD + r"\b", re.I)
_ASK_BEFORE_RE = re.compile(_ASK_LEAD + r"\s+(?:an?\s+|the\s+)?$", re.I)

#: Type nouns that are also ordinary English: "a text box", "on our radar",
#: "the housing bubble". They are matched BARE like every other name and
#: then kept only when the sentence names them as a type — their own chart
#: word, or a CONVERSION lead-in ("as a box", "make it a bubble") — the
#: weaker "add a box" is not enough, because that is as likely to be a box
#: round a title. Demanding the chart word outright (the first fix round)
#: threw away the legitimate follow-up "as a box by team", which names a
#: type and carries no chart word at all.
_NEEDS_NAMING = frozenset({"box", "radar", "bubble"})

#: The chart words a person writes. Order is now only a tie-break (see
#: `named_type`): the BEST match wins, so "stacked bar" beats "bar" on
#: length and "as a pie" beats a bare "funnel" on the lead-in.
#: This is the ARTIFACT side of core.chart_decision._TYPE_PHRASES (which
#: answers a different question — whether a chat answer should carry a chart
#: at all) and covers the tier-2 types that one does not name.
_NAMED_PHRASES: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(?:100%?\s*stacked|percent(?:age)?\s+stacked)\s+(?:bar|column)s?\b", re.I), "percent_stacked_bar"),
    (re.compile(r"\bstacked\s+(?:horizontal\s+)?(?:area|areas)\b", re.I), "stacked_area"),
    (re.compile(r"\bstacked\s+horizontal\s+(?:bar|column)s?\b", re.I), "stacked_horizontal_bar"),
    (re.compile(r"\bstacked\s+(?:bar|column)s?\b", re.I), "stacked_bar"),
    (re.compile(r"\bhorizontal\s+(?:bar|column)s?\b", re.I), "horizontal_bar"),
    (re.compile(r"\b(?:donut|doughnut)\b" + _CW, re.I), "donut"),
    (re.compile(r"\bheat\s*map\b" + _CW, re.I), "heatmap"),
    (re.compile(r"\bwater\s*fall\b" + _CW, re.I), "waterfall"),
    (re.compile(r"\bfunnel\b" + _CW, re.I), "funnel"),
    (re.compile(r"\bgantt\b" + _CW, re.I), "gantt"),
    # box / radar / bubble are matched bare and gated by _NEEDS_NAMING.
    (re.compile(r"\b(?:radar|spider)\b" + _CW, re.I), "radar"),
    (re.compile(r"\bbubbles?\b" + _CW, re.I), "bubble"),
    (re.compile(r"\bbox\s+and\s+whiskers?(?:\s+(?:plot|chart|graph))?\b|"
                r"\bbox\b(?:\s+(?:plot|chart|graph))?|\bwhiskers?\b", re.I), "box"),
    (re.compile(r"\bhistogram\b" + _CW, re.I), "histogram"),
    (re.compile(r"\bdistribution\s+(?:chart|graph|plot)\b", re.I), "histogram"),
    (re.compile(r"\bscatter(?:\s*(?:plot|chart|graph))?\b", re.I), "scatter"),
    (re.compile(r"\bcombo\s+(?:chart|graph)\b|\bdual[-\s]axis\b", re.I), "combo"),
    (re.compile(r"\bpie(?:\s+(?:chart|graph|plot))?\b", re.I), "pie"),
    (re.compile(r"\barea\s+(?:chart|graph|plot)\b", re.I), "area"),
    (re.compile(r"\b(?:line|trend)\s+(?:chart|graph|plot|line)\b", re.I), "line"),
    (re.compile(r"\btrend\s+over\s+time\b", re.I), "line"),
    (re.compile(r"\b(?:bar|column)\s+(?:chart|graph|plot)\b", re.I), "bar"),
    (re.compile(r"\bbar\s*graph\b", re.I), "bar"),
    # Follow-up phrasings. They carry no chart word, so the lead-in they
    # begin with is the whole of their claim to be a named type.
    (re.compile(r"\b(?:as|to|into)\s+(?:an?\s+)?areas?\b", re.I), "area"),
    (re.compile(r"\b(?:as|to|into)\s+(?:an?\s+)?lines?\b", re.I), "line"),
    (re.compile(r"\b(?:as|to|into)\s+(?:an?\s+)?bars?\b", re.I), "bar"),
    (re.compile(r"\bmake\s+(?:it|them|that)\s+(?:an?\s+)?lines?\b", re.I), "line"),
    (re.compile(r"\bmake\s+(?:it|them|that)\s+(?:an?\s+)?bars?\b", re.I), "bar"),
)


def _naming_rank(text: str, m: "re.Match") -> int:
    """How hard the sentence says this noun is the TYPE being asked for.

    2 — a conversion lead-in ("as a pie", "make it a donut", "switch to a
        gantt"): the words allow no other reading.
    1 — the match carries a chart word of its own ("pie chart", "heat map",
        "stacked bar"), or a request verb handed it an article ("draw a
        pie").
    0 — a bare noun in the middle of a phrase, which is what a SUBJECT reads
        like: "our sales funnel", "the housing bubble", "a text box".
    """
    hit = m.group(0)
    before = text[:m.start()]
    if _CONVERT_INSIDE_RE.match(hit) or _CONVERT_BEFORE_RE.search(before):
        return 2
    if _QUALIFIED_RE.search(hit) or _ASK_BEFORE_RE.search(before):
        return 1
    return 0


def named_type(text: str) -> Optional[str]:
    """The chart type the person named in their own words, or None.

    None means "they did not say", which is what hands the decision to
    `recommend`. False positives are removed first with the chat side's own
    list ("plot twist", "graph theory"), so ordinary prose never counts as a
    request for a type.
    """
    if not text:
        return None
    try:
        from ..core.chart_decision import _strip_false_positives  # noqa: PLC0415

        text = _strip_false_positives(text)
    except Exception:  # noqa: BLE001 — the chat module is optional in this build
        pass
    # BEST match wins, not the first one. Ranked on (how the sentence names
    # the noun, then the length of the match, then the table order), so "as a
    # pie" outranks the "funnel" that names the subject, and "stacked bar"
    # still outranks the "bar chart" inside it. Every occurrence of a noun is
    # scored, because the subject and the named type can be the same word
    # ("our sales funnel — redraw it as a funnel chart").
    best: Optional[Tuple[Tuple[int, int, int], str]] = None
    for index, (pattern, ctype) in enumerate(_NAMED_PHRASES):
        for m in pattern.finditer(text):
            rank = _naming_rank(text, m)
            if ctype in _NEEDS_NAMING and rank < 2 and not _QUALIFIED_RE.search(m.group(0)):
                # An ordinary English noun is a type only when the sentence
                # can mean nothing else: "as a box", "box plot", "box and
                # whisker" — never "a text box" and never the weaker
                # "add a box", which is as likely to be a box round a title.
                continue
            key = (rank, len(m.group(0)), -index)
            if best is None or key > best[0]:
                best = (key, ctype)
    return best[1] if best else None


#: Types that are the same reading of the data in a different layout. When
#: nobody named a type, a model pick inside the recommended family is kept:
#: bar vs horizontal bar is a label-length preference, not a claim about the
#: numbers.
_FAMILIES: Tuple[Tuple[str, ...], ...] = (
    ("bar", "horizontal_bar"),
    ("stacked_bar", "stacked_horizontal_bar", "percent_stacked_bar"),
    ("line", "area"),
    ("stacked_area",),
    ("pie", "donut"),
    ("scatter", "bubble"),
)


def _family(chart_type: str) -> Tuple[str, ...]:
    for fam in _FAMILIES:
        if chart_type in fam:
            return fam
    return (chart_type,)


# ------------------------------------------------------------------ shape --


@dataclass
class Shape:
    """What the bound table and binding actually are, in chart terms."""

    x_name: str = ""
    x_kind: str = ""              # number | date | text | empty | "" (no x)
    x_is_measure: bool = False    # numeric AND not a year / rank / id column
    x_is_period: bool = False     # a year or period number: ordered, not a measure
    n_rows: int = 0
    n_categories: int = 0         # distinct x labels, counted up to DISTINCT_CAP
    max_label_chars: int = 0
    measures: List[str] = field(default_factory=list)
    measure_kind: str = ""        # count | percent | duration | amount
    agg: str = "sum"
    group_name: str = ""
    n_groups: int = 0
    one_row_per_category: bool = False
    parts_of_whole: bool = False
    has_negative: bool = False
    mixed_signs: bool = False
    stage_order: bool = False     # labels are a trusted stage order that only shrinks
    matrix: bool = False
    has_gantt_columns: bool = False
    has_size: bool = False
    has_y2: bool = False
    trend_r: float = 0.0

    @property
    def n_measures(self) -> int:
        return len(self.measures)

    @property
    def measure_name(self) -> str:
        if self.measures:
            return self.measures[0]
        return "the row count" if self.agg == "count" else "the value"

    @property
    def rows_per_category(self) -> float:
        return self.n_rows / self.n_categories if self.n_categories else 0.0

    @property
    def is_counting(self) -> bool:
        return self.agg == "count" and not self.measures


@dataclass
class Choice:
    """(type, reason) — plus the note a person reads when the type changed."""

    type: str
    reason: str
    note: str = ""
    trendline: bool = False


def _spread(rows: Sequence[Any], limit: int) -> Sequence[Any]:
    if len(rows) <= limit:
        return rows
    stride = math.ceil(len(rows) / limit)
    return rows[::stride]


def _measure_kind(name: str) -> str:
    if _PERCENT_RE.search(name):
        return "percent"
    if _DURATION_RE.search(name):
        return "duration"
    return "amount"


def shape_of(chart: CS.Chart, table: Any) -> Optional[Shape]:
    """The shape of `chart.data` over `table`, or None when the binding does
    not resolve (a missing column is chart_data's callout to write, not a
    reason to pick a different type here)."""
    from . import chart_data as CD  # lazy: chart_data imports this module

    b = chart.data
    if b is None or table is None:
        return None
    columns = [str(c) for c in (CD._table_attr(table, "columns", []) or [])]
    if not columns:
        return None
    rows = list(CD._table_attr(table, "rows", []) or [])
    sh = Shape(agg=b.agg, n_rows=len(rows))
    sh.has_gantt_columns = bool(b.label and b.start and b.end)
    sh.has_size = bool(b.size)
    sh.has_y2 = bool(b.y2)

    x_idx, _ = CD.match_column(b.x, columns)
    if b.x and x_idx is None:
        # The docstring's own contract. Measured 2026-09-16: with x="Zzzqqq"
        # over ["State","Count"] the shape came back with x_kind="" and
        # recommend() read that as "no category column", so repair_binding
        # retyped the chart to a histogram and wrote "there is no category
        # column" beside resolve_chart's true "The column 'Zzzqqq' was not
        # found in m." One of those two sentences was false. A misspelled
        # column is chart_data's callout, not a reason to change the type.
        return None
    if x_idx is not None:
        info = CD.infer_column(table, x_idx)
        sh.x_name, sh.x_kind = info.name, info.kind
        if info.kind == "number":
            sh.x_is_period = bool(_PERIOD_RE.match(info.name.strip()))
            sh.x_is_measure = not CD._NON_MEASURE_RE.match(info.name.strip())
        body = [r for r in rows if not CD._is_total_row(r)]
        labels = [CD._label_of(r[x_idx]) if x_idx < len(r) else CD.BLANK for r in _spread(body, DISTINCT_CAP * 8)]
        distinct = list(dict.fromkeys(labels))
        sh.n_categories = min(len(distinct), DISTINCT_CAP)
        sh.max_label_chars = max((len(s) for s in distinct[:DISTINCT_CAP]), default=0)
        sh.one_row_per_category = len(labels) > 1 and len(distinct) == len(labels)
        sh.n_rows = len(body)

    g_idx, _ = CD.match_column(b.group_by, columns)
    if g_idx is not None:
        ginfo = CD.infer_column(table, g_idx)
        sh.group_name = ginfo.name
        sh.n_groups = ginfo.n_distinct

    for name in list(b.y) + list(b.y2):
        idx, _ = CD.match_column(name, columns)
        if idx is not None and CD.infer_column(table, idx).kind == "number":
            sh.measures.append(columns[idx])

    if sh.measures:
        sh.measure_kind = _measure_kind(sh.measures[0])
        idx, _ = CD.match_column(sh.measures[0], columns)
        vals = [v for v in (CD.to_number(r[idx]) if idx < len(r) else None
                            for r in _spread([r for r in rows if not CD._is_total_row(r)], VALUE_SAMPLE)) if v is not None]
        sh.has_negative = any(v < 0 for v in vals)
        sh.mixed_signs = sh.has_negative and any(v > 0 for v in vals)
    elif b.agg == "count":
        sh.measure_kind = "count"

    # A count of rows is always a non-negative part of the whole table; a
    # named measure only adds up when its name does not say otherwise.
    additive = sh.is_counting or (
        bool(sh.measures)
        and b.agg in ("sum", "none", "count")
        and (sh.measure_kind == "percent" or not _NON_ADDITIVE_RE.search(sh.measures[0]))
    )
    sh.parts_of_whole = bool(additive and not sh.has_negative and sh.n_measures <= 1)

    sh.matrix = _is_matrix(sh)
    sh.stage_order = _is_shrinking_stages(table, x_idx, b, sh)
    if sh.x_kind == "number" and sh.x_is_measure and sh.measures:
        sh.trend_r = _correlation(table, x_idx, sh.measures[0], columns)
    return sh


def _is_matrix(sh: Shape) -> bool:
    """A category x group grid dense enough to read as a heatmap."""
    if sh.x_kind not in ("text", "date") or not sh.group_name:
        return False
    if sh.n_categories < MIN_HEATMAP_SIDE or sh.n_groups < MIN_HEATMAP_SIDE:
        return False
    cells = sh.n_categories * sh.n_groups
    return bool(cells) and sh.n_rows / cells >= MIN_MATRIX_FILL


def _is_shrinking_stages(table: Any, x_idx: Optional[int], binding: CS.Binding, sh: Shape) -> bool:
    """Stages that only shrink — and only when the labels belong to an order
    somebody else defined.

    Values alone are not enough: a table sorted by value is monotonic too, so
    reading "Texas 21, Missouri 11, Illinois 9" as a funnel would invent a
    process that does not exist. core.chart_decision.trusted_stage_order is
    the existing answer to "is this an order we did not make up".
    """
    from . import chart_data as CD  # lazy

    if x_idx is None or sh.x_kind != "text" or not sh.one_row_per_category or sh.n_categories < MIN_HEATMAP_SIDE:
        return False
    if sh.n_rows > DISTINCT_CAP:
        # This is the one check that reads every row; a process has stages,
        # not hundreds of thousands of them.
        return False
    try:
        from ..core.chart_decision import trusted_stage_order  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return False
    rows = [r for r in (CD._table_attr(table, "rows", []) or []) if not CD._is_total_row(r)]
    labels = [CD._label_of(r[x_idx]) if x_idx < len(r) else "" for r in rows]
    order = trusted_stage_order(labels)
    if order is None:
        return False
    columns = [str(c) for c in (CD._table_attr(table, "columns", []) or [])]
    y_idx, _ = CD.match_column(sh.measures[0], columns) if sh.measures else (None, "")
    if y_idx is None:
        return False
    by_label = {}
    for r in rows:
        v = CD.to_number(r[y_idx]) if y_idx < len(r) else None
        if v is not None:
            by_label[CD._label_of(r[x_idx]) if x_idx < len(r) else ""] = v
    seq = [by_label[s] for s in dict.fromkeys(order) if s in by_label]
    return len(seq) >= MIN_HEATMAP_SIDE and all(a >= b for a, b in zip(seq, seq[1:])) and seq[0] > seq[-1]


def _correlation(table: Any, x_idx: Optional[int], y_name: str, columns: Sequence[str]) -> float:
    """Pearson r of (x, y) over a sample, 0.0 when it cannot be computed."""
    from . import chart_data as CD  # lazy

    y_idx, _ = CD.match_column(y_name, columns)
    if x_idx is None or y_idx is None:
        return 0.0
    pairs = []
    for r in _spread(list(CD._table_attr(table, "rows", []) or []), TREND_SAMPLE):
        a = CD.to_number(r[x_idx]) if x_idx < len(r) else None
        b = CD.to_number(r[y_idx]) if y_idx < len(r) else None
        if a is not None and b is not None:
            pairs.append((a, b))
    n = len(pairs)
    if n < 4:
        return 0.0
    mx = sum(a for a, _ in pairs) / n
    my = sum(b for _, b in pairs) / n
    sxy = sum((a - mx) * (b - my) for a, b in pairs)
    sxx = sum((a - mx) ** 2 for a, _ in pairs)
    syy = sum((b - my) ** 2 for _, b in pairs)
    if sxx <= 0 or syy <= 0:
        return 0.0
    return sxy / math.sqrt(sxx * syy)


# --------------------------------------------------------------- the rules --


def _bar_flavour(sh: Shape) -> str:
    if sh.n_categories > VERTICAL_BAR_CATEGORIES or sh.max_label_chars > LONG_LABEL_CHARS:
        return "horizontal_bar"
    return "bar"


def recommend(sh: Shape) -> Choice:
    """The type this shape carries, and the sentence that says why.

    First match wins; the order is the rule table in `RULES` and in the
    model's own guidance (chart_spec.prompt_guide), so the model's choice and
    the code's choice are made against the same list.
    """
    m = sh.measure_name

    if sh.has_gantt_columns:
        return Choice("gantt", "the binding names a label, a start and an end, which is a schedule")
    if sh.has_size:
        return Choice("bubble", f"a third measure ({m}) sizes each point")

    if sh.has_y2:
        return Choice("combo", f"{m} and {sh.measures[-1] if sh.n_measures > 1 else 'a second measure'} are on two axes")

    if sh.x_kind == "number" and sh.x_is_measure and sh.measures:
        trend = abs(sh.trend_r) >= TREND_MIN_R
        why = f"{sh.x_name} and {m} are both measures, so each row is a point"
        if trend:
            return Choice("scatter", why + f" (r = {sh.trend_r:+.2f}, so a trend line is drawn)", trendline=True)
        return Choice("scatter", why)

    if not sh.x_kind and sh.measures:
        return Choice("histogram", f"there is no category column, so {m} is shown as a distribution")

    if sh.x_kind == "number" and sh.x_is_period:
        return Choice("line", f"{sh.x_name} is a period, so {m} is drawn as a line in {sh.x_name} order")

    if sh.x_kind == "date":
        if sh.group_name:
            if sh.parts_of_whole or sh.matrix:
                return Choice("stacked_area", f"{m} is split by {sh.group_name} over {sh.x_name}, and the parts add up to the total")
            return Choice("line", f"{sh.x_name} is a date, so {sh.group_name} becomes one line each over time")
        if sh.n_measures > 1:  # y2 was already answered with a combo above
            return Choice("line", f"{sh.x_name} is a date, so the {sh.n_measures} measures are drawn as lines over time")
        return Choice("line", f"{sh.x_name} is a date, so {m} is drawn as a line over time")

    if sh.group_name and sh.matrix:
        return Choice("heatmap", f"{sh.x_name} x {sh.group_name} is a {sh.n_categories}x{sh.n_groups} grid of {m}, which reads as a heatmap")
    if sh.group_name:
        if sh.parts_of_whole:
            return Choice("stacked_bar", f"{m} is split by {sh.group_name} and the parts add up to each {sh.x_name} total")
        return Choice("bar", f"{m} is compared across {sh.x_name}, one bar per {sh.group_name}")

    # `not one_row_per_category` is what can_draw("box", sh) checks. Without
    # it the two disagreed: 10,000 rows with 10,000 distinct labels count as
    # n_categories == DISTINCT_CAP (400), so rows_per_category read 25 and
    # recommend() returned a box of 10,000 groups of one row each — a type
    # can_draw refuses for the same shape.
    if (sh.measures and sh.x_kind == "text" and sh.agg in ("none", "median")
            and not sh.one_row_per_category and sh.rows_per_category >= MIN_BOX_ROWS_PER_CATEGORY):
        return Choice("box", f"{sh.x_name} has about {sh.rows_per_category:.0f} rows each, so {m} is shown as a distribution per group")

    if sh.x_kind in ("text", "number"):
        if sh.mixed_signs:
            return Choice("waterfall", f"{m} holds positive and negative contributions, which a waterfall adds up to the net total")
        if sh.stage_order:
            return Choice("funnel", f"{sh.x_name} is a known stage order and {m} only shrinks along it")
        if sh.parts_of_whole and sh.x_kind == "text" and sh.n_categories <= AUTO_PIE_CATEGORIES:
            return Choice("pie", f"{sh.x_name} has {sh.n_categories} values and {m} adds up to one whole, so each share is a slice")
        flavour = _bar_flavour(sh)
        if flavour == "horizontal_bar":
            return Choice("horizontal_bar", f"{sh.x_name} has {sh.n_categories} values, too many for a pie or a vertical axis, so they are ranked by {m}")
        return Choice("bar", f"{m} is compared across the {sh.n_categories} values of {sh.x_name}")

    if sh.measures:
        return Choice("histogram", f"{m} is one measure with no category to compare, so it is shown as a distribution")
    return Choice("bar", "the values are compared across categories")


def can_draw(chart_type: str, sh: Shape) -> str:
    """"" when `chart_type` can carry this shape; otherwise the reason it
    cannot, as a clause that reads after "a pie chart ...".

    Only refusals that make the picture WRONG or empty are here. A type that
    is merely a weaker choice is still drawn: the person asked for it.
    """
    if chart_type in CS.PART_OF_WHOLE_TYPES:
        if sh.has_negative:
            return f"cannot show the negative values in {sh.measure_name}"
        if sh.x_kind == "date" and sh.n_categories > 1:
            return f"turns the {sh.n_categories} dates in {sh.x_name} into slices, which loses the order they happened in"
        if sh.n_categories > NAMED_PIE_CATEGORIES:
            return f"shows at most {_pie_slices()} slices, so {sh.n_categories - _pie_slices() + 1} of the {sh.n_categories} values of {sh.x_name} would disappear into one Other wedge"
        if sh.measures and sh.measure_kind == "duration":
            return f"adds {sh.measure_name} up into a whole, and durations do not add up to a meaningful total"
        if sh.measures and sh.agg in ("avg", "median", "min", "max"):
            return f"adds its slices up into a whole, and {CS.AGG_LABELS[sh.agg].lower()}s do not add up"
        return ""
    if chart_type == "heatmap":
        if not sh.group_name:
            return "needs a second category for its rows, and the binding has only one"
        return ""
    if chart_type in CS.XY_TYPES:
        if sh.x_kind != "number":
            return f"needs a numeric x, and {sh.x_name or 'the x column'} is {sh.x_kind or 'missing'}"
        if chart_type == "bubble" and not sh.has_size:
            return "needs a third measure for the size of each point"
        return ""
    if chart_type == "histogram":
        if not sh.measures:
            return "needs one numeric column to put into bins"
        return ""
    if chart_type == "gantt":
        if not sh.has_gantt_columns:
            return "needs a task label, a start date and an end date"
        return ""
    if chart_type == "funnel":
        if sh.has_negative:
            return f"cannot show the negative values in {sh.measure_name}"
        if not sh.stage_order:
            return f"is for stages that only shrink, and {sh.x_name or 'the x column'} is not a known stage order that shrinks"
        return ""
    if chart_type == "box":
        if not sh.measures:
            return "needs a numeric column to summarise"
        if sh.one_row_per_category:
            return f"summarises many rows per group, and this table has one row per {sh.x_name}"
        return ""
    if chart_type == "combo":
        if sh.n_measures < 2 and not sh.has_y2:
            return "draws two measures on two axes, and the binding names one"
        return ""
    if chart_type in CS.STACKED_TYPES:
        if not sh.group_name and sh.n_measures < 2:
            return "stacks parts of each total, and the binding names neither a split column nor a second measure"
        if chart_type == "percent_stacked_bar" and sh.has_negative:
            return f"cannot show the negative values in {sh.measure_name} as shares of 100%"
        return ""
    return ""


def _pie_slices() -> int:
    from . import chart_data as CD  # lazy

    return CD.PIE_MAX_SLICES


def _article(chart_type: str) -> str:
    return f"{'an' if chart_type[0] in 'aeiou' else 'a'} {label(chart_type)}"


def label(chart_type: str) -> str:
    return chart_type.replace("_", " ")


def about_charts(instruction: str) -> bool:
    """True when the person's words are about a chart at all.

    The gate on retyping a chart somebody has already accepted: "make the
    title bigger" is not a chart instruction, so it must not steer a type.
    `core.chart_decision.explicit_chart_request` already owns this judgement
    for the chat side (it fires on chart/graph/plot/visualise, on a named
    type, on "dashboard", on "show this visually", and on a typo of any of
    them); a type this module names is a chart instruction by definition.
    """
    if not instruction:
        # No words at all: the caller is asking the chooser to decide, not
        # handing it an instruction to read.
        return True
    if named_type(instruction) is not None:
        return True
    try:
        from ..core.chart_decision import explicit_chart_request  # noqa: PLC0415

        return bool(explicit_chart_request(instruction))
    except Exception:  # noqa: BLE001 — the chat module is optional in this build
        return True


def _keep_the_chart(chart: CS.Chart, sh: Shape, best: Choice, trend: bool) -> Optional[Choice]:
    """The one thing the chooser is never silent about: a type this shape
    cannot carry AT ALL.

    None here means "no opinion about the type" — leave `chart.type` exactly
    as it is. It must not also mean "draw whatever is there", because a type
    can_draw refuses does not compute: chart_data.resolve_chart returns no
    chart and resolve_spec swaps the block for a callout, a slide chart for a
    bullet, and drops a sheet chart. Measured 2026-09-16 on the merged tree:
    a bubble chart of Height/Weight with no size column sat inside
    _family("scatter"), so choose() returned None, repair_binding changed
    nothing and resolve_chart answered "A bubble chart needs a size column."
    — the person asked for a report and got a sentence where the picture was.
    """
    why_not = can_draw(chart.type, sh)
    if not why_not:
        return None
    return Choice(
        best.type,
        best.reason,
        note=f"{_article(chart.type)} {why_not}, so the data is drawn as {_article(best.type)}: {best.reason}",
        trendline=trend and best.type == "scatter",
    )


def choose(chart: CS.Chart, table: Any, instruction: str = "", *,
           keep_accepted_type: bool = False) -> Optional[Choice]:
    """The type this chart should be drawn as, or None to leave it alone.

    `instruction` is the person's own words: a type named there WINS whenever
    the shape carries it, and when it does not the returned note names both
    types and the reason. With no type named, `recommend` decides and the
    model's pick survives only inside the recommended family.

    None is "no opinion about the type", NOT "do not draw": whatever else is
    decided here, a type the shape cannot carry is still replaced, because
    the alternative is no chart at all (see `_keep_the_chart`).

    `keep_accepted_type` is True when the chart already exists in a version
    the person has seen (an EDIT). Measured 2026-09-16: "make the title
    bigger" over an accepted line chart came back as a pie, because the
    unnamed path retypes every chart in the spec whatever the edit was
    about. On an edit an instruction that is not about charts changes no
    type at all; a fresh compose keeps the whole rule table.
    """
    sh = shape_of(chart, table)
    if sh is None:
        return None
    want = named_type(instruction)
    best = recommend(sh)

    # The invariant: never hand back a type can_draw refuses for this shape.
    # recommend()'s rules are checked against the same Shape, but this makes
    # it hold for any future rule as well.
    if can_draw(best.type, sh):
        best = Choice(_bar_flavour(sh), f"{sh.measure_name} is compared across {sh.x_name or 'the categories'}")

    # A trend line is a property of the FIT, not of who picked the type: it
    # is drawn on any scatter whose straight line says something.
    trend = abs(sh.trend_r) >= TREND_MIN_R

    if keep_accepted_type and not about_charts(instruction):
        # An edit that is not about charts has no opinion about the type —
        # and still may not leave behind a chart that will not draw.
        return _keep_the_chart(chart, sh, best, trend)

    if want:
        why_not = can_draw(want, sh)
        if not why_not:
            # Their type, their chart — even when `recommend` prefers another.
            return Choice(want, f"you asked for {_article(want)}", trendline=trend and want == "scatter")
        fallback = best
        if fallback.type == want:
            return _keep_the_chart(chart, sh, best, trend)
        # A note explains a SUBSTITUTION. When the chart is already the
        # fallback type nothing is being substituted, and "so it is drawn as
        # a line" would claim a change that did not happen — measured
        # 2026-09-16 on "pie chart of statuses please" against an untouched
        # line chart of Date/Amount, which is not even the chart the
        # instruction was about.
        note = ""
        if fallback.type != chart.type:
            note = f"{_article(want)} {why_not}, so it is drawn as {_article(fallback.type)}: {fallback.reason}"
        return Choice(fallback.type, fallback.reason, note=note, trendline=trend and fallback.type == "scatter")

    if chart.type in _family(best.type):
        # The model's layout preference inside the right reading of the data
        # — unless that preference is a type the shape cannot carry (a bubble
        # with no size column sits in scatter's own family).
        net = _keep_the_chart(chart, sh, best, trend)
        if net is not None:
            return net
        if trend and chart.type == "scatter":
            return Choice(chart.type, best.reason, trendline=True)
        return None
    why_not = can_draw(chart.type, sh)
    detail = f"{_article(chart.type)} {why_not}, so " if why_not else "no chart type was asked for, so "
    return Choice(
        best.type,
        best.reason,
        note=f"{detail}the data is drawn as {_article(best.type)}: {best.reason}",
        trendline=trend and best.type == "scatter",
    )


# ------------------------------------------------------- suggestions --
#
# CHARTS DECIDED BY CODE, WITH NO MODEL CALL (2026-09-17). Two turns in the
# owner's report ended with no picture at all: "I want plot" over a CSV the
# composer had written no chart for, and "also i want Plots on this docs".
# Nothing in the pipeline could look at a table and say what is worth
# drawing — `recommend` only types a chart somebody has ALREADY bound. These
# functions read the table's own shape and return BINDINGS (never numbers:
# chart_data.compute still does that, in its worker thread).

#: A column whose values are nearly all different carries no comparison: a
#: count of it is a row of 1s and a bar of it is one bar per row.
NEAR_UNIQUE_SHARE = 0.8
#: A text column is worth a category axis when a real share of the rows
#: repeat: at most this many distinct values per row.
CATEGORY_DISTINCT_SHARE = 0.5
#: …and its biggest group has to hold at least this many rows.
MIN_TOP_COUNT = 3
#: A category axis longer than this folds its tail into "Other".
TAIL_MAX_CATEGORIES = 15
TAIL_TOP_N = 10
#: Rows read to profile a column for a suggestion. `infer_column`'s own
#: sample; the distinct counts below are taken over the same rows.
SUGGEST_SAMPLE = 20_000

#: Columns that NAME a row rather than describe it. Charting one is always
#: noise, whatever its cardinality says.
_ID_NAME_RE = re.compile(
    r"^(?:.*\b)?(?:id|ids|uuid|guid|key|code|ref|reference|index|idx|serial|"
    r"sr\.?\s*no\.?|s\.?\s*no\.?|no\.?|number|email|e-?mail|phone|phone\s*\d+|mobile|"
    r"tel|telephone|fax|website|url|uri|link|address|postcode|zip|pin|slug|hash|token)"
    r"(?:\b.*)?$",
    re.I,
)

#: The date bucket a span of this many days is counted in. Read in order.
_BUCKET_BY_SPAN: Tuple[Tuple[int, str], ...] = ((366 * 3, "month"), (366 * 10, "quarter"), (10 ** 9, "year"))

#: "top 10 countries" — the person's own tail, which the fold below obeys
#: instead of imposing its own.
_TOP_N_RE = re.compile(r"\btop\s+(\d{1,2})\b", re.I)


def top_n_named(instruction: str) -> Optional[int]:
    """The `top N` the person typed, when it is a tail a chart can carry."""
    m = _TOP_N_RE.search(instruction or "")
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 50 else None


def _is_id_name(name: str) -> bool:
    return bool(_ID_NAME_RE.match(" ".join(str(name).split())))


#: Columns that name ONE row's person or contact, whatever their cardinality
#: says. The hotfix 1.1 replay's file repeats 25 first names over 100 rows,
#: so First Name passed as a category and "Records by First Name" was drawn,
#: by the composer and by "Plots on this docs" alike (2026-09-19): how many
#: customers are called Linda compares nothing. Narrower than `_ID_NAME_RE`
#: on purpose — this one REFUSES a chart somebody bound, and rows per
#: "Product Code" or "Country Code" is a real comparison.
_ROW_NAMING_RE = re.compile(
    r"^(?:.*\b)?(?:(?:first|last|middle|given|family|full|nick|maiden|sur|fore)[\s_-]*name|firstname|lastname|"
    r"e-?mail(?:\s*address)?|(?:phone|mobile|telephone|fax)(?:\s*(?:no\.?|number|\d+))?|web\s*site|homepage|url|"
    r"id|ids|uuid|guid)$",
    re.I,
)


def names_a_row(name: str) -> bool:
    """A person-name part, an e-mail, phone or website, or a row id."""
    return bool(_ROW_NAMING_RE.match(" ".join(str(name).split())))


@dataclass
class _Col:
    index: int
    name: str
    kind: str
    n_distinct: int
    top_count: int
    n_rows: int

    @property
    def near_unique(self) -> bool:
        return self.n_rows > 0 and self.n_distinct >= NEAR_UNIQUE_SHARE * self.n_rows


def _profile_column(table: Any, index: int, sample: int = SUGGEST_SAMPLE) -> _Col:
    """One column as (kind, distinct, biggest group), over an evenly spread
    sample — so a 200,000-row table costs what a 20,000-row one costs."""
    from . import chart_data as CD  # lazy: chart_data imports this module

    columns = [str(c) for c in (CD._table_attr(table, "columns", []) or [])]
    rows = list(CD._table_attr(table, "rows", []) or [])
    scan = _spread(rows, sample)
    info = CD.infer_column(table, index, sample=sample)
    counts: Dict[str, int] = {}
    for r in scan:
        v = r[index] if index < len(r) else None
        if CD._is_blank(v):
            continue
        label = CD._label_of(v)
        counts[label] = counts.get(label, 0) + 1
    return _Col(index=index, name=columns[index] if index < len(columns) else f"Column {index + 1}",
                kind=info.kind, n_distinct=len(counts),
                top_count=max(counts.values()) if counts else 0, n_rows=len(scan))


def _profile(table: Any, sample: int = SUGGEST_SAMPLE) -> List[_Col]:
    """Every column of `table`. Only `suggest_charts` needs them all; the
    guards below profile the ONE column they are about."""
    from . import chart_data as CD  # lazy

    n = len(list(CD._table_attr(table, "columns", []) or []))
    return [_profile_column(table, i, sample) for i in range(n)]


def _date_bucket(table: Any, col: _Col) -> str:
    """month / quarter / year, from the span the column actually covers."""
    from . import chart_data as CD  # lazy

    rows = list(CD._table_attr(table, "rows", []) or [])
    info = CD.infer_column(table, col.index, sample=SUGGEST_SAMPLE)
    order = info.date_order or "dmy"
    dates = [d for d in (CD.to_date(r[col.index] if col.index < len(r) else None, order) for r in _spread(rows, SUGGEST_SAMPLE)) if d is not None]
    if len(dates) < 2:
        return "month"
    span = (max(dates) - min(dates)).days
    for limit, bucket in _BUCKET_BY_SPAN:
        if span <= limit:
            return bucket
    return "year"


def _drawn_keys(drawn: Sequence[Any]) -> Dict[Tuple[str, Tuple[str, ...]], set]:
    """(x, y) -> the date buckets already drawn for it, from bindings."""
    out: Dict[Tuple[str, Tuple[str, ...]], set] = {}
    for b in drawn or ():
        get = (lambda k, _b=b: _b.get(k)) if isinstance(b, dict) else (lambda k, _b=b: getattr(_b, k, None))
        x = " ".join(str(get("x") or "").split()).casefold()
        if not x:
            continue
        ys = tuple(sorted(" ".join(str(v).split()).casefold() for v in (get("y") or [])))
        out.setdefault((x, ys), set()).add(str(get("date_bucket") or ""))
    return out


def _years_spanned(table: Any, col: "_Col") -> int:
    from . import chart_data as CD  # lazy

    rows = list(CD._table_attr(table, "rows", []) or [])
    order = CD.infer_column(table, col.index, sample=SUGGEST_SAMPLE).date_order or "dmy"
    years = {d.year for d in (CD.to_date(r[col.index] if col.index < len(r) else None, order) for r in _spread(rows, SUGGEST_SAMPLE)) if d is not None}
    return len(years)


def suggest_charts(table: Any, *, instruction: str = "", limit: int = 3,
                   drawn: Sequence[Any] = ()) -> Tuple[List[CS.Chart], List[str]]:
    """(charts, reasons) — chart BINDINGS this table can honestly carry.

    Dates first (how many rows per month / quarter / year, as a line), then
    the text columns that really are categories, then the numeric measures.
    Id-like and near-unique columns are never suggested: a bar per e-mail
    address is not a chart. No numbers are computed here — every chart comes
    back with `data` only, and `chart_data.compute` fills it in the job's
    worker thread.

    `drawn` are the bindings the document already draws: a chart it has is
    not suggested again. A date column it draws per month or quarter can
    still be drawn per year, as a different reading of the same rows (the
    replay's "Plots on this docs" re-added the monthly chart the report had).

    CPU-bound (one pass per column): call it through `asyncio.to_thread`.
    """
    from . import chart_data as CD  # lazy

    tid = str(CD._table_attr(table, "id", "") or "")
    cols = _profile(table)
    charts: List[CS.Chart] = []
    reasons: List[str] = []
    named = top_n_named(instruction)
    have = _drawn_keys(drawn)

    def usable(c: _Col) -> bool:
        return not _is_id_name(c.name) and not names_a_row(c.name) and c.n_rows > 0

    def counted(c: _Col) -> Optional[set]:
        return have.get((" ".join(c.name.split()).casefold(), ()))

    dates = [c for c in cols if c.kind == "date" and usable(c)]
    texts = [c for c in cols if c.kind == "text" and usable(c) and not c.near_unique
             and c.n_distinct <= CATEGORY_DISTINCT_SHARE * c.n_rows and c.top_count >= MIN_TOP_COUNT]
    texts.sort(key=lambda c: (-c.top_count, c.n_distinct, c.index))
    measures = [c for c in cols if c.kind == "number" and usable(c) and not _PERIOD_RE.match(c.name)]

    for col in dates:
        if len(charts) >= limit:
            break
        buckets = counted(col)
        if buckets is not None:
            if "year" in buckets or _years_spanned(table, col) < 2:
                continue
            charts.append(CS.Chart(type="bar", title=f"Records by year of {col.name}"[:120], x_label="Year", y_label="Records",
                                   data=CS.Binding(table_id=tid, x=col.name, y=[], agg="count", date_bucket="year", sort="x")))
            reasons.append(f"{col.name} is already drawn over time, so the rows are counted per year")
            continue
        bucket = _date_bucket(table, col)
        charts.append(CS.Chart(type="line", title=f"Records by {col.name}"[:120], x_label=col.name[:60], y_label="Records",
                               data=CS.Binding(table_id=tid, x=col.name, y=[], agg="count", date_bucket=bucket, sort="x")))
        reasons.append(f"{col.name} is a date, so the rows are counted per {bucket}")

    for col in texts:
        if len(charts) >= limit:
            break
        if counted(col) is not None:
            continue
        top_n = named or (TAIL_TOP_N if col.n_distinct > TAIL_MAX_CATEGORIES else None)
        charts.append(CS.Chart(type=_bar_flavour_for(col), title=f"Records by {col.name}"[:120], x_label=col.name[:60], y_label="Records",
                               data=CS.Binding(table_id=tid, x=col.name, y=[], agg="count", sort="value_desc",
                                               top_n=top_n, other_bucket=True)))
        tail = f", so the {top_n - 1} largest are shown and the rest are grouped as Other" if top_n else ""
        reasons.append(f"{col.name} has {col.n_distinct} values in {col.n_rows:,} rows{tail or ', so the rows are counted per value'}")

    for col in measures:
        if len(charts) >= limit:
            break
        if texts:
            cat = texts[0]
            charts.append(CS.Chart(type=_bar_flavour_for(cat), title=f"{col.name} by {cat.name}"[:120], x_label=cat.name[:60], y_label=col.name[:60],
                                   data=CS.Binding(table_id=tid, x=cat.name, y=[col.name], agg="sum", sort="value_desc",
                                                   top_n=named or (TAIL_TOP_N if cat.n_distinct > TAIL_MAX_CATEGORIES else None), other_bucket=True)))
            reasons.append(f"{col.name} is a number, so it is totalled by {cat.name}")
        else:
            charts.append(CS.Chart(type="histogram", title=f"Distribution of {col.name}"[:120], x_label=col.name[:60],
                                   data=CS.Binding(table_id=tid, x=col.name, y=[], agg="count")))
            reasons.append(f"{col.name} is a number with no category column beside it, so its distribution is drawn")

    return charts[:limit], reasons[:limit]


def _bar_flavour_for(col: "_Col") -> str:
    """bar or horizontal_bar for a category axis of this many values."""
    if col.n_distinct > VERTICAL_BAR_CATEGORIES:
        return "horizontal_bar"
    return "bar"


def skipped_columns(table: Any) -> List[str]:
    """The columns `suggest_charts` will not chart, for a sentence that has
    to say which ones are missing."""
    return [c.name for c in _profile(table) if c.kind != "date" and (_is_id_name(c.name) or names_a_row(c.name) or c.near_unique)]


def fold_long_tail(chart: CS.Chart, table: Any, instruction: str = "") -> Tuple[Optional[CS.Binding], str]:
    """(binding, note) when a bar axis is too long to read, else (None, "").

    More than `TAIL_MAX_CATEGORIES` bars is a picture nobody reads and a
    legend nobody matches. The person's own `top N` wins; a binding that
    already carries one is left alone."""
    b = chart.data
    if b is None or chart.type not in ("bar", "horizontal_bar") or b.top_n is not None or not b.x:
        return None, ""
    from . import chart_data as CD  # lazy

    columns = [str(c) for c in (CD._table_attr(table, "columns", []) or [])]
    idx, _ = CD.match_column(b.x, columns)
    if idx is None:
        return None, ""
    col = _profile_column(table, idx)
    if col.kind == "date" or col.n_distinct <= TAIL_MAX_CATEGORIES:
        # A date axis is BUCKETED before it is drawn (one bar per month, not
        # per timestamp), so its raw cardinality is not the number of bars.
        return None, ""
    top_n = top_n_named(instruction) or TAIL_TOP_N
    # `Binding.top_n` is the CATEGORY CAP, and `chart_data.compute` spends one
    # of those slots on the Other bucket: a cap of 10 draws the 9 largest
    # beside Other. The note says the number the person will count.
    note = (f"{col.name} has {col.n_distinct} values, so the chart shows the {top_n - 1} largest "
            f"and groups the rest as {_other_label()}")
    return b.model_copy(update={"top_n": top_n, "other_bucket": True}), note


def _other_label() -> str:
    from . import chart_data as CD  # lazy

    return CD.OTHER


def not_worth_drawing(chart: CS.Chart, table: Any, computed: Any = None) -> str:
    """Why a COUNTING chart of this column says nothing, or "".

    Only counting charts (`agg='count'` with no measure): a bar per row is
    what a near-unique column produces, and a tallest bar of two rows is a
    row of equal bars. A chart with a real measure is never refused here — a
    revenue bar per product is one row per category by design."""
    b = chart.data
    if b is None or b.y or b.agg != "count" or not b.x:
        return ""
    if chart.type not in ("bar", "horizontal_bar", "pie", "donut"):
        return ""
    from . import chart_data as CD  # lazy

    columns = [str(c) for c in (CD._table_attr(table, "columns", []) or [])]
    idx, _ = CD.match_column(b.x, columns)
    if idx is None:
        return ""
    if names_a_row(columns[idx]):
        return f"{columns[idx]} names one person or contact per row rather than a group, so counting it compares nothing"
    col = _profile_column(table, idx)
    if col.n_rows < 10 or col.kind == "date":
        # A DATE axis is bucketed before it is counted (one bar per month,
        # not per timestamp), so its raw cardinality says nothing about the
        # picture. Every daily sales file would be refused otherwise.
        return ""
    if col.near_unique:
        return (f"{col.name} has {col.n_distinct} different values in {col.n_rows:,} rows, "
                f"so counting them draws one bar per row")
    if computed is not None:
        values = [v for s in (getattr(computed, "series", None) or []) for v in (getattr(s, "values", None) or [])]
        top = max(values) if values else 0.0
    else:
        top = float(col.top_count)
    if top <= 2:
        return f"no {col.name} appears more than {int(top)} time{'s' if int(top) != 1 else ''}, so every bar would be the same height"
    return ""


# ------------------------------------------------- the same table, for the model --

#: The rule table, in the order `recommend` applies it. chart_spec.prompt_guide
#: prints it so the model chooses against the list code will check it against;
#: tests assert the two never drift.
RULES: Tuple[Tuple[str, str], ...] = (
    ("one category column and one measure, at most 6 categories, values that add up to one whole", "pie (donut when asked)"),
    ("one category column and one measure, more than 6 categories or long labels", "horizontal_bar, sorted by value, the tail folded into Other"),
    ("one category column and one measure, few short labels", "bar"),
    ("a date x and one measure", "line"),
    ("a date x and several measures", "line, one per measure"),
    ("a date x split by a category whose parts add up", "stacked_area"),
    ("a category x split by a category", "stacked_bar when the parts add up, otherwise bar"),
    ("a category x by a category, a dense grid of one measure", "heatmap"),
    ("two measures and no category", "scatter (trendline true only when the fit is real)"),
    ("a numeric x that is a year or a period, not a measure", "line in that order, never scatter"),
    ("a second measure on its own axis (y2)", "combo"),
    ("a third measure sizing each point", "bubble"),
    ("one measure and no category", "histogram"),
    ("one measure with many rows per group", "box"),
    ("positive and negative contributions to a net total", "waterfall"),
    ("stages of a known process where the measure only shrinks", "funnel"),
    ("a task label with a start and an end date", "gantt"),
)


def rules_text() -> str:
    """The rule table as the model's guidance lines."""
    head = ("Type when the person does not name one — code checks the chart against this same table after you "
            "write it, and replaces a type the data cannot carry:")
    body = "\n".join(f"- {shape} -> {ctype}" for shape, ctype in RULES)
    tail = ("When the person DOES name a type it is used, unless the data cannot carry it (a pie of more than "
            f"{NAMED_PIE_CATEGORIES} categories, a pie of negative values or of dates, a funnel whose stages grow); "
            "then the nearest workable type is drawn and a note says which and why.")
    return f"{head}\n{body}\n{tail}"


__all__ = [
    "AUTO_PIE_CATEGORIES", "NAMED_PIE_CATEGORIES", "VERTICAL_BAR_CATEGORIES", "LONG_LABEL_CHARS", "TREND_MIN_R",
    "MIN_HEATMAP_SIDE", "MIN_BOX_ROWS_PER_CATEGORY", "RULES", "Shape", "Choice",
    "shape_of", "recommend", "can_draw", "choose", "named_type", "about_charts", "rules_text", "label",
    "suggest_charts", "skipped_columns", "fold_long_tail", "not_worth_drawing", "top_n_named",
    "NEAR_UNIQUE_SHARE", "CATEGORY_DISTINCT_SHARE", "MIN_TOP_COUNT", "TAIL_MAX_CATEGORIES", "TAIL_TOP_N",
]
