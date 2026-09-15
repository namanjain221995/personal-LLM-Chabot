"""Visuals this platform cannot draw — one list, and the sentence that says so.

THE INCIDENT (2026-09-16). A person asked twice to plot a table of records
per state "on a map". There is no geographic chart type, no geo dependency is
installed, and the file's PersonMailingLatitude / PersonMailingLongitude are
null in every row. The platform never said any of that: it answered with prose
wrapped in a Word file and a PDF nobody had asked for.

ONE SOURCE OF TRUTH. `chart_spec.CHART_TYPES` is what the renderers can draw.
Nothing here keeps a second list of what IS supported: every name below is
tied to the chart-type token it would need, and a name counts as unsupported
exactly when that token is missing from CHART_TYPES. The day a type lands,
its word stops being a refusal on the same import — `unsupported()` is
computed, not written down.

THE SENTENCE IS CODE. `refusal_sentence` names the visual, says why it cannot
be drawn, and offers the nearest chart that CAN be drawn over the same data
("Count by State as a bar chart"). No model writes it, so it cannot drift into
"as an AI I cannot…" or into a document nobody asked for.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import chart_spec as CS


@dataclass(frozen=True)
class Visual:
    """A visual people ask for by name.

    `token` is the `chart_spec` type id it would need — that is the whole
    supported/unsupported test. `nearest` is the type id of the closest thing
    this platform does draw, or "" when nothing is close.
    """

    token: str
    #: What the answer calls it, with its article: "a map", "a Sankey diagram".
    phrase: str
    #: Why it cannot be drawn, as a clause. True for this platform today.
    why: str
    nearest: str
    pattern: str

    @property
    def supported(self) -> bool:
        return self.token in CS.CHART_TYPES


#: The geographic family shares one reason: there is no chart type that takes
#: a place name or a latitude/longitude pair and paints an outline.
_GEO_WHY = "this platform has no geographic chart type, so nothing here can place a value on a country, state or street outline"

#: `map` is a word people use for things that are not charts. It counts only
#: in the shapes that ask for a drawing — "on a map", "a map of", "map chart",
#: "geographic map" — and the words that carry their own noun (road map, site
#: map, mind map, heat map, roadmap) are kept out by the lookbehind. A bare
#: leading "map" is NOT one of the shapes: "map the columns to the schema and
#: create a PDF" is a mapping verb and a real document request.
_NOT_A_MAP = r"(?<!heat )(?<!heat)(?<!road )(?<!road)(?<!site )(?<!site)(?<!mind )(?<!mind)(?<!bit)"
_MAP_PATTERN = (
    rf"\b(?:on|onto|in|over|across|upon)\s+(?:an?\s+|the\s+)?{_NOT_A_MAP}maps?\b"
    rf"|\b(?:an?|the)\s+{_NOT_A_MAP}maps?\b"
    rf"|{_NOT_A_MAP}\bmaps?\s+(?:chart|view|visuali[sz]ation|of\b)"
    rf"|\b(?:geo|geographic(?:al)?|geo[- ]?spatial|world|country|state[- ]wise|india|indian|us|usa|regional|location)\s+maps?\b"
    rf"|नक्शा|नक्शे|मैप|નકશો|નકશા|મેપ"
)

#: Every named visual the platform is asked for that its renderers do not
#: have. Order is the order they are tested in; the map family is last of the
#: geographic group so "choropleth map" is named as a choropleth.
_NAMED: Tuple[Visual, ...] = (
    Visual("choropleth", "a choropleth", _GEO_WHY, "bar", r"\bchoropleths?\b"),
    Visual("globe", "a globe", _GEO_WHY, "bar", r"\b(?:3-?d\s+)?globes?\b|\bglobe\s+view\b"),
    Visual("geo", "a geographic chart", _GEO_WHY, "bar",
           r"\bgeo(?:graphic(?:al)?|[- ]?spatial)?\s+(?:charts?|plots?|graphs?|visuali[sz]ations?)\b|\bgeo\s?charts?\b"),
    Visual("map", "a map", _GEO_WHY, "bar", _MAP_PATTERN),
    Visual("sankey", "a Sankey diagram", "this platform draws no flow diagrams, so there is no type that shows a quantity moving from one stage to the next", "bar",
           r"\bsankeys?\b|\bflow\s+diagrams?\b"),
    Visual("treemap", "a treemap", "this platform has no nested-area chart type", "pie", r"\btree\s?maps?\b"),
    Visual("sunburst", "a sunburst", "this platform has no nested-area chart type", "pie", r"\bsun\s?bursts?\b"),
    Visual("wordcloud", "a word cloud", "this platform has no word-cloud type", "bar", r"\bword\s?clouds?\b|\btag\s?clouds?\b"),
    Visual("violin", "a violin plot", "this platform has no violin type", "box", r"\bviolin\s*(?:plots?|charts?)?\b"),
    Visual("candlestick", "a candlestick chart", "this platform has no candlestick (open/high/low/close) type", "line",
           r"\bcandle\s?sticks?\b|\bohlc\b"),
    Visual("venn", "a Venn diagram", "this platform draws no set diagrams", "", r"\bvenn\b"),
    Visual("network", "a network diagram", "this platform draws no node-and-edge diagrams", "",
           r"\bnetwork\s+(?:diagrams?|graphs?|charts?)\b|\bnode[- ]link\b|\bforce[- ]directed\b"),
)

#: The verbs and shapes that ask to SEE something. A name on its own ("the
#: treemap in that paper is unreadable") is a remark, not a request.
_ASK_RE = re.compile(
    r"\b(?:plot|plotted|plotting|chart|charted|graph|graphed|draw|drawn|show|shows|showing|display|render|visuali[sz]e|"
    r"visuali[sz]ing|visuali[sz]ation|map\s+(?:it|this|these|them)|mark|overlay|see|view|make|create|generate|build|give|"
    r"put|need|want|can\s+you|could\s+you)\b"
    r"|दिखा|बना|બતાવ|બનાવ",
    re.I,
)

#: How a chart type is said in prose. Anything not here is its id with the
#: underscores opened up, which reads correctly for every current type.
_TYPE_WORDS: Dict[str, str] = {
    "bar": "bar chart", "horizontal_bar": "horizontal bar chart", "line": "line chart", "area": "area chart",
    "pie": "pie chart", "donut": "donut chart", "scatter": "scatter plot", "histogram": "histogram",
    "box": "box plot", "heatmap": "heatmap", "radar": "radar chart", "bubble": "bubble chart",
    "funnel": "funnel chart", "waterfall": "waterfall chart", "gantt": "Gantt chart", "combo": "combo chart",
}


def type_words(token: str) -> str:
    """"bar" → "bar chart". Used in the refusal and in the capability line."""
    return _TYPE_WORDS.get(token, f"{token.replace('_', ' ')} chart")


def supported() -> Tuple[str, ...]:
    """The chart types this platform draws — chart_spec's list, unchanged."""
    return tuple(CS.CHART_TYPES)


def unsupported() -> Tuple[Visual, ...]:
    """The named visuals whose type id is NOT in CHART_TYPES, right now."""
    return tuple(v for v in _NAMED if not v.supported)


def by_token(token: str) -> Optional[Visual]:
    return next((v for v in _NAMED if v.token == token), None)


def named_unsupported(text: str) -> Optional[Visual]:
    """The first visual named in `text` that this platform cannot draw.

    Naming only: a request is `asked_for`. A name whose type has since landed
    in CHART_TYPES is not returned at all — it is a chart like any other.
    """
    t = (text or "")[:4000]
    if not t:
        return None
    for v in unsupported():
        if re.search(v.pattern, t, re.I):
            return v
    return None


def asked_for(text: str) -> Optional[Visual]:
    """The visual this text ASKS for and this platform cannot draw.

    The name plus a word that asks to see it. "Plot this on a map", "show it
    on a map", "can you draw a choropleth" are requests; "the treemap in that
    paper is unreadable" is not.
    """
    v = named_unsupported(text)
    if v is None:
        return None
    return v if _ASK_RE.search((text or "")[:4000]) else None


# ------------------------------------------------------------- the answer --


def refusal_sentence(visual: Visual, *, category: str = "", measure: str = "") -> str:
    """What cannot be drawn, why, and the nearest chart over the same data.

    Written here rather than by the model because the model's version of this
    turn was a Word file and a PDF (2026-09-16). Every clause is checked
    against CHART_TYPES, so the offer is never of something that cannot be
    drawn either.
    """
    said = f"I can't draw {visual.phrase} — {visual.why}."
    nearest = visual.nearest
    if not nearest or nearest not in CS.CHART_TYPES:
        return said + " Nothing this platform draws is close to it, so the numbers are best left as the table above."
    what = f"{measure} by {category}" if measure and category else (f"records by {category}" if category else "the same values by category")
    view = f"{what} as a {type_words(nearest)}"
    return f"{said} The closest view of the same data is {view}. Ask for \"{view}\" and I will draw it."


def fallback_sentence() -> str:
    """The honest answer when the name is not one this module knows: the list
    of what CAN be drawn, read straight off CHART_TYPES."""
    drawable = ", ".join(type_words(t) for t in CS.CHART_TYPES)
    return f"That is not a visual this platform can draw. What it can draw over the same data: {drawable}. Say which one and I will draw it."


def refusal_for(token: str, *, history: Sequence[Any] = ()) -> str:
    """The refusal for one visual, with the nearest view named from the most
    recent answer's table when there is one."""
    visual = by_token(token)
    if visual is None or visual.supported:
        return fallback_sentence()
    measure, category = ("", "")
    try:
        measure, category = nearest_columns(history)
    except Exception:  # noqa: BLE001 — the generic sentence still answers the person
        pass
    return refusal_sentence(visual, category=category, measure=measure)


#: How many characters of the previous answer are scanned for a table. The
#: refusal is written on the event loop, and a 120,000-character answer is
#: not worth a millisecond there (memory: fast-mode-cpu-bound-prepass).
NEAREST_SCAN_CHARS = 20_000


def nearest_columns(history: Sequence[Any]) -> Tuple[str, str]:
    """(measure, category) named from the most recent answer's first usable
    table: the first text column with more than one value, and the single
    numeric column that is a measure rather than a rank or a percentage.
    ("", "") when the conversation has no table to point at."""
    from . import material_in as MI

    md, _idx, _notes = MI.previous_answer(list(history or []))
    if not md:
        return "", ""
    tables = MI.tables_from_markdown(md[:NEAREST_SCAN_CHARS], id_prefix="answer", source_id="assistant_answer")
    for table in tables[:4]:
        measure, category = _columns_of(table)
        if category:
            return measure, category
    return "", ""


def _columns_of(table: Any) -> Tuple[str, str]:
    # chart_data owns what counts as a MEASURE — the rule that keeps a Rank
    # column and a "% of Total" column off an axis (2026-09-16). Reading its
    # pattern here rather than restating it is what stops the offer naming a
    # column the chart would then refuse to plot.
    from . import chart_data as CD

    columns = [str(c) for c in (getattr(table, "columns", None) or [])]
    if not columns:
        return "", ""
    category = ""
    measures: List[str] = []
    for idx, name in enumerate(columns):
        info = CD.infer_column(table, idx)
        if info.kind == "text" and not category and info.n_distinct > 1:
            category = name
        elif info.kind == "number" and not CD._NON_MEASURE_RE.match(name.strip()):
            measures.append(name)
    return (measures[0] if len(measures) == 1 else ""), category


# ------------------------------------------------------- the prompt line --


def limits_sentence() -> str:
    """The carve-out for the answering prompts: the real limits, named, so a
    model that is told never to deny file creation still admits the one thing
    that does not exist instead of inventing a document."""
    names = [v.phrase.split(" ", 1)[1] if v.phrase.startswith(("a ", "an ")) else v.phrase for v in unsupported()]
    return (
        "It cannot draw a visual that has no chart type here — "
        + ", ".join(names)
        + ". If someone asks for one, say plainly in one sentence that it cannot be drawn and offer the nearest chart that "
        "does exist (for example the same values by category as a bar chart); do not make a document instead of saying it."
    )


__all__ = [
    "Visual", "supported", "unsupported", "by_token", "named_unsupported", "asked_for",
    "refusal_sentence", "refusal_for", "nearest_columns", "limits_sentence", "type_words",
]
