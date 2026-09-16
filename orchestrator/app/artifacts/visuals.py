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
    #: May the nearest chart be offered when the conversation holds NO table?
    #: For the geographic family yes — "on a map" is always asked of values
    #: that exist, so "the same values by category as a bar chart" fits. For a
    #: flow diagram it does not: "draw a flow diagram of the onboarding
    #: process" has no values at all, and offering a bar chart of them was
    #: an offer of nothing (verifier, 2026-09-16).
    offer_without_table: bool = True

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
    # Latin-script Hinglish: "isko map pe dikhao", "ise map par dikhao". The
    # postposition is what makes it "on a map"; "map parameters" keeps its
    # word boundary and is not touched (verifier gap, 2026-09-16).
    rf"|{_NOT_A_MAP}\bmaps?\s+(?:pe|par|pr)\b"
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
           r"\bsankeys?\b|\bflow\s+diagrams?\b", offer_without_table=False),
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
    # The same asks in Latin-script Hinglish, so "isko map pe dikhao" is read
    # as a request and answered, not dropped (verifier gap, 2026-09-16).
    r"|\b(?:dikhao|dikha\s+do|dikhaiye|dikhaao|banao|bana\s+do|batao)\b"
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


#: A chart type named only to RULE IT OUT is not a request for it: "draw
#: this on a map, not a bar chart" asks for the map, and drawing the bar
#: chart would draw the very thing the person rejected.
_NEGATED_BEFORE = re.compile(r"\b(?:not|no|instead\s+of|rather\s+than|without|besides|other\s+than)\s+(?:an?\s+|the\s+)?$", re.I)


#: An OFFER of a drawable type has one of two shapes, and nothing else does.
#:
#:  * a connector immediately in front of the name — "…on a map, or a bar
#:    chart", "otherwise draw a bar chart", "failing that a pie chart";
#:  * an if-clause anywhere in the message that says what to do when the
#:    visual cannot be drawn — "if a map is not possible, draw a pie chart",
#:    "draw a pie chart if a map is not supported".
#:
#: Anything else that merely CONTAINS a chart word is a mention, not an
#: offer: "plot these counts on a map like the bar chart you drew last
#: time" is still a map request (verifier recheck, 2026-09-16).
_OFFER_CONNECTOR = re.compile(
    r"\b(?:or|otherwise|alternatively|failing\s+that|else)\b[\s,;:—–-]*"
    r"(?:just\s+|maybe\s+|please\s+|simply\s+)?"
    r"(?:draw|make|give|show|use|put|do|plot|render)?\s*(?:me\s+|it\s+|them\s+|this\s+|these\s+)?"
    r"(?:as\s+|in\s+|into\s+)?(?:an?\s+|the\s+)?$",
    re.I,
)
_OFFER_CONDITION = re.compile(
    r"\bif\s+(?:you|that|this|it|there|an?|the)?[^.?!;]{0,60}?"
    r"(?:can(?:'|’)?t\b|cannot\b|can\s+not\b|not\s+possible\b|not\s+supported\b|not\s+available\b|"
    r"unable\b|impossible\b|unsupported\b|no\s+(?:\w+\s+){0,2}(?:type|types|support|option|way)\b)"
    r"|\bif\s+not\b|\bwhen\s+you\s+can(?:'|’)?t\b",
    re.I,
)


def names_a_drawable_type(text: str) -> bool:
    """Does this text OFFER a chart type this platform CAN draw?

    THE MESSAGE THAT NAMES BOTH (verifier, 2026-09-16). "if a map is not
    possible, draw a pie chart of the states" and "plot this on a map or a
    bar chart if you can't" were answered with the map refusal and no chart:
    the gate's no-other-deliverable guard only looked at FILE formats
    (formats.kind_for), so a named CHART type did not count as the other
    deliverable. The words come from the same single source as everything
    else here — `type_words` over `chart_spec.CHART_TYPES`.

    AN OFFER, NOT A WORD (verifier recheck, 2026-09-16). The first fix read
    any chart-type word anywhere in the sentence, so a map request lost its
    honest refusal the moment it referred to a chart that already exists —
    "plot these counts on a map like the bar chart you drew last time", "we
    already have a bar chart; now plot these on a map". The guard is for the
    person who says what to draw INSTEAD when the map cannot be drawn, so
    it reads the two shapes that say that (`_OFFER_CONNECTOR`,
    `_OFFER_CONDITION`) and nothing else. A message that names both still answers with the
    drawable one, which is the whole point of the guard.
    """
    t = (text or "")[:4000]
    if not t:
        return False
    conditional = bool(_OFFER_CONDITION.search(t))
    for token in CS.CHART_TYPES:
        for m in re.finditer(rf"\b{re.escape(type_words(token))}\b", t, re.I):
            if _NEGATED_BEFORE.search(t[max(0, m.start() - 24): m.start()]):
                continue
            if conditional or _OFFER_CONNECTOR.search(t[max(0, m.start() - 40): m.start()]):
                return True
    return False


#: A question about an ATTACHMENT'S CONTENT — what the file says, not what to
#: draw from the person's data.
_ABOUT_CONTENT_RE = re.compile(
    r"\bwhat(?:'|’)?s?\b[^.?!]{0,90}?\b(?:say|says|said|show|shows|showing|shown|mean|means|meant|about|of|"
    r"in\s+(?:it|this|that|the)|on\s+(?:it|this|that|the))\b"
    r"|\b(?:read|re-?read|transcribe|translate|describe|summari[sz]e|explain|interpret|extract|"
    r"identify|analy[sz]e|caption|ocr)\b"
    r"|\bon\s+page\s+\d+\b|\bat\s+\d{1,2}:\d{2}\b"
    r"|\bin\s+(?:the\s+|this\s+|that\s+|my\s+|your\s+)?(?:attach(?:ed|ment)|upload(?:ed)?|image|photo|"
    r"picture|screenshot|scan|pdf|document|doc|file|slide|page|video|clip|frame)s?\b",
    re.I,
)

#: The verbs that ask for a NEW picture of the person's own data. They are the
#: drawing family only: "show", "see", "make" and "put" are in `_ASK_RE`
#: because a request to see a map is a request, but they are also how people
#: ask what a file says, so they cannot decide this.
#: The past forms are deliberately absent: "describe the map drawn on page 4"
#: is a question about a picture that already exists, not an ask for a new one.
_DRAW_ASK_RE = re.compile(
    r"\b(?:plot|plots|plotting|draw|draws|visuali[sz]e|visuali[sz]ing|render|renders|overlay|pin|pins)\b"
    r"|\bmap\s+(?:it|this|these|them|the\s+\w+)\b",
    re.I,
)


def asks_about_attachment_content(text: str) -> bool:
    """Is this turn a question about a FILE, rather than a request to draw?

    WHY THE CARVE-OUT EXISTS (verifier, 2026-09-16). "what does the map on
    page 2 show?" with a PDF attached and "can you show me what the map
    says?" with a photo attached were answered "I can't draw a map" and the
    file was never read: a map in a file is something to READ.

    WHY IT IS THIS NARROW (verifier recheck, 2026-09-16). The first fix
    skipped the refusal for every image/pdf/video turn, so "plot these
    records on a map" with the table attached as a PDF went to the document
    engine — which draws nothing, so the turn came back as prose, which is
    the incident. A drawing verb means the person wants a picture made from
    their data, and no attachment changes that this platform has no
    geographic chart type to make it with.
    """
    t = (text or "")[:4000]
    if not t or _DRAW_ASK_RE.search(t):
        return False
    return bool(_ABOUT_CONTENT_RE.search(t))


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
    have_table = bool(category)
    if not nearest or nearest not in CS.CHART_TYPES:
        # "so the numbers are best left as the table above" asserted a table
        # that is not there whenever the question arrived without one
        # ("can you show me a network diagram of how these services talk to
        # each other" — no table in that conversation; verifier 2026-09-16).
        # Only the branch that really found one says "above".
        if have_table:
            return said + " Nothing this platform draws is close to it, so the numbers are best left as the table above."
        return said + " Nothing this platform draws is close to it, so I can describe it in words instead."
    if not have_table and not visual.offer_without_table:
        # A process has no values yet: ask for them rather than offering a
        # bar chart of numbers nobody has given.
        return f"{said} If you give me the values behind it, I can draw them as a {type_words(nearest)}."
    what = f"{measure} by {category}" if measure and category else (f"records by {category}" if category else "the same values by category")
    view = f"{what} as a {type_words(nearest)}"
    # THE QUOTED PHRASE IS ONE THE GATE ANSWERS (verifier, 2026-09-16). The
    # offer used to read: Ask for "Count (Approx) by State as a bar chart" —
    # and `intent.decide` on exactly that string returned
    # action=none rule=no-request, because it has no verb. A person who
    # copied the sentence got nothing, which is the same "it does not do what
    # it said" the incident was about. `ask_for` is verb-led, and
    # tests/test_vt3_adversarial.py feeds the quoted substring back through
    # decide() so the sentence and the gate cannot drift apart again.
    ask_for = f"draw {view}"
    return f"{said} The closest view of the same data is {view}. Ask for \"{ask_for}\" and I will draw it."


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
    "Visual", "supported", "unsupported", "by_token", "named_unsupported", "asked_for", "names_a_drawable_type",
    "asks_about_attachment_content",
    "refusal_sentence", "refusal_for", "nearest_columns", "limits_sentence", "type_words",
]
