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
from . import pictures as PIC


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
#: EVERY WORD THAT QUALIFIES "map" INTO SOMETHING ELSE. `tree`, `stream`,
#: `journey` and `value` joined the list on 2026-09-28. The first is a
#: PRE-EXISTING bug, measured on ae25da28 before any change here:
#:
#:   "draw a tree map of revenue by product" -> unsupported-visual:map
#:
#: A treemap has been a real chart type since 2026-09-16 (it is in
#: `chart_spec.CHART_TYPES`, and `Visual("treemap", …)` below is therefore
#: SUPPORTED and never refused) — but the `map` entry is tested first and its
#: lookbehind had no `tree`, so the person was told this platform has no
#: geographic chart type for a chart it draws. `value stream map` and `user
#: journey map` are the same shape on the diagram side.
_NOT_A_MAP = (r"(?<!heat )(?<!heat)(?<!road )(?<!road)(?<!site )(?<!site)(?<!mind )(?<!mind)(?<!bit)"
              r"(?<!tree )(?<!tree)(?<!stream )(?<!stream)(?<!journey )(?<!journey)(?<!value )(?<!sun)")
#: The same idea in Devanagari and Gujarati. Each head is spelled both with and
#: without the separating space, and the Latin heads are here too because one
#: sentence often mixes scripts.
_NOT_A_MAP_DEV = (
    r"(?<!माइंड )(?<!माइंड)(?<!माइन्ड )(?<!माइन्ड)(?<!माईंड )(?<!माईंड)"
    r"(?<!હીટ )(?<!હીટ)(?<!હિટ )(?<!હિટ)"
    r"(?<!हीट )(?<!हीट)(?<!हिट )(?<!हिट)"
    r"(?<!रोड )(?<!रोड)(?<!રોડ )(?<!રોડ)"
    r"(?<!साइट )(?<!साइट)(?<!સાઇટ )(?<!સાઇટ)"
    r"(?<!માઇન્ડ )(?<!માઇન્ડ)(?<!માઈન્ડ )(?<!માઈન્ડ)"
    r"(?<!mind )(?<!mind)(?<!heat )(?<!heat)(?<!road )(?<!road)(?<!site )(?<!site)"
)
_MAP_PATTERN = (
    rf"\b(?:on|onto|in|over|across|upon)\s+(?:an?\s+|the\s+)?{_NOT_A_MAP}maps?\b"
    rf"|\b(?:an?|the)\s+{_NOT_A_MAP}maps?\b"
    rf"|{_NOT_A_MAP}\bmaps?\s+(?:chart|view|visuali[sz]ation|of\b)"
    rf"|\b(?:geo|geographic(?:al)?|geo[- ]?spatial|world|country|state[- ]wise|india|indian|us|usa|regional|location)\s+maps?\b"
    # Latin-script Hinglish: "isko map pe dikhao", "ise map par dikhao". The
    # postposition is what makes it "on a map"; "map parameters" keeps its
    # word boundary and is not touched (verifier gap, 2026-09-16).
    rf"|{_NOT_A_MAP}\bmaps?\s+(?:pe|par|pr)\b"
    # The LATIN compounds are kept out by `_NOT_A_MAP`; these had no guard at
    # all, so a Devanagari or Gujarati compound was read as a geographic map.
    # Measured 2026-09-28: "माइंड मैप बनाओ रोडमैप का" — Hindi for "make a mind
    # map of the roadmap" — was answered "I can't draw a map: this platform has
    # no geographic chart type", while the identical English sentence drew a
    # real `mindmap`. The same trap was waiting for हीट मैप, रोड मैप, साइट मैप
    # and માઇન્ડ મેપ. `_NOT_A_MAP_DEV` is the same lookbehind idea in the two
    # scripts, and the Latin heads are included because people mix scripts
    # inside one sentence ("mind मैप").
    rf"|{_NOT_A_MAP_DEV}(?:नक्शा|नक्शे|नक्शो|मैप|मेप|નકશો|નકશા|નકશું|મેપ|મૅપ)"
)

#: THE NETWORK ASK, SPLIT ON WHERE THE BOXES COME FROM — the second half of
#: the correction feat/document-vocabulary made to the sankey.
#:
#: That commit dropped `flow diagram` from the sankey pattern because
#: render/diagrams.py had just learned to draw one, and it reworded the
#: network `why` from "this platform draws no node-and-edge diagrams"
#: (false the moment that renderer landed) to "never from the ROWS OF A
#: TABLE" — but it left the PATTERN matching every network ask, so the
#: sentence and the gate disagreed. Measured on this tree before this edit:
#: "draw a network diagram of how the services talk" was refused, while
#: "draw an architecture diagram of the platform" and "can you draw a flow
#: diagram of the pipeline" were answered. That is the SAME PICTURE
#: refused or drawn on the noun alone, and it is now the one place a person
#: asking for boxes and arrows is told no by a platform that draws them —
#: on the file route too, since the composer can ask a section for one.
#:
#: WHAT IS STILL REFUSED IS WHAT THE `why` ALREADY SAYS: a network laid out
#: from DATA. `node-link` and `force-directed` are layout-algorithm names
#: that only mean anything over rows, so they stay bare; "network
#: graph/chart/plot" stays too, because -graph and -chart are what a person
#: calls the picture OF a dataset. A network DIAGRAM is refused only when
#: the ask points at the data — a table, a CSV, rows, columns, an edge
#: list, an adjacency matrix — and is otherwise a diagram like any other.
_NETWORK_DATA = (
    r"tables?|csvs?|spreadsheets?|sheets?|rows?|columns?|datasets?|"
    r"data(?:\s*set)?|edge\s*lists?|adjacency|matri(?:x|ces)|records?|"
    r"this\s+file|these\s+numbers|the\s+export"
)
_NETWORK_PATTERN = (
    # The layout algorithms, and the "graph"/"chart" nouns: always a picture
    # of a dataset, never a hand-named architecture.
    r"\bnetwork\s+(?:graphs?|charts?|plots?)\b|\bnode[- ]link\b|\bforce[- ]directed\b"
    # "a network diagram of this table", "draw a network diagram from the CSV",
    # "network diagram from these rows" — the ask that names its data.
    rf"|\bnetwork\s+diagrams?\b[^.;!?\n]{{0,60}}?\b(?:{_NETWORK_DATA})\b"
    # ... and the same sentence the other way round: "from this table, a
    # network diagram".
    rf"|\b(?:{_NETWORK_DATA})\b[^.;!?\n]{{0,60}}?\bnetwork\s+diagrams?\b"
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
    Visual("sankey", "a Sankey diagram", "this platform has no chart type that shows a quantity moving from one stage to the next", "bar",
           r"\bsankeys?\b", offer_without_table=False),
    Visual("treemap", "a treemap", "this platform has no nested-area chart type", "pie", r"\btree\s?maps?\b"),
    Visual("sunburst", "a sunburst", "this platform has no nested-area chart type", "pie", r"\bsun\s?bursts?\b"),
    Visual("wordcloud", "a word cloud", "this platform has no word-cloud type", "bar", r"\bword\s?clouds?\b|\btag\s?clouds?\b"),
    Visual("violin", "a violin plot", "this platform has no violin type", "box", r"\bviolin\s*(?:plots?|charts?)?\b"),
    Visual("candlestick", "a candlestick chart", "this platform has no candlestick (open/high/low/close) type", "line",
           r"\bcandle\s?sticks?\b|\bohlc\b"),
    Visual("venn", "a Venn diagram", "this platform draws no set diagrams", "", r"\bvenn\b"),
    Visual("network", "a network diagram", "this platform lays a diagram out from boxes and arrows that are named, never from the rows of a table, so there is no chart type that draws a network out of this data", "",
           _NETWORK_PATTERN),

    # ------------------------------------------------------------------------
    # THE PICTURES NOBODY HAD WRITTEN A NO FOR (2026-09-28). Fourteen phrasings
    # in the 206-phrasing survey asked for something this platform genuinely
    # cannot draw and got no sentence saying so, because no word for any of
    # them appeared anywhere in the product. What the model improvised instead,
    # measured live in the running container:
    #
    #   "draw a wireframe of the chat screen"        -> a `flowchart TD`
    #                                                  presented as a wireframe
    #   "draw me a picture of a cat wearing
    #    sunglasses"                                 -> ASCII art, which
    #                                                  DIAGRAM_INSTRUCTION
    #                                                  itself forbids
    #   "floor plan of a 2bhk with the kitchen east" -> a table of room
    #                                                  dimensions that never
    #                                                  said it could not draw
    #                                                  the plan
    #   "draw me a calendar for October with the
    #    sprints marked"                             -> a markdown table headed
    #                                                  "October 2024" (it is
    #                                                  2026)
    #
    # A person who asks for something this product cannot draw is owed a plain
    # sentence saying so and what it CAN do instead. These are added HERE, with
    # the map and the sankey, rather than invented somewhere else: the sentence
    # is written by `refusal_sentence` from the fields, every `nearest` is
    # checked against CHART_TYPES, and the day a renderer for one of them lands
    # its word stops being a refusal on the same import.
    Visual("chord", "a chord diagram",
           "this platform has no chart type that draws a circular flow between categories", "heatmap",
           r"\bchords?\s+(?:diagram(?:me)?s?|charts?|plots?)\b|\bchord\s+diagram(?:me)?s?\b",
           offer_without_table=False),
    Visual("calendar", "a calendar",
           "this platform has no calendar type, so nothing here can lay dates out on a month grid", "gantt",
           # "add it to my calendar" is not a drawing ask, so the name counts
           # only when a picture word or a data relation follows it.
           r"\bcalendars?\s+(?:views?|charts?|grids?|layouts?|diagram(?:me)?s?)\b"
           r"|\bcalendars?\s+(?:of|for|with|showing)\b",
           offer_without_table=False),
    Visual("wireframe", "a wireframe",
           "this platform draws no interface mock-ups: there is no renderer here that lays out screens, controls and blocks of copy", "",
           r"\bwire[\s-]?frames?\b|\bmock[\s-]?ups?\b|\blo-?fi\s+designs?\b"),
    Visual("floor_plan", "a floor plan",
           "this platform draws nothing to scale, so nothing here can lay out rooms, walls or seats", "",
           r"\bfloor\s*plans?\b|\bseating\s*(?:plans?|charts?|arrangements?)\b"
           r"|\bsite\s+plans?\b|\belevations?\s+drawings?\b|\bblue\s?prints?\b"),
    Visual("circuit", "a circuit diagram",
           "this platform has no symbol library for electronics, so nothing here can draw a resistor, a capacitor or a gate", "",
           r"\bcircuits?\s+(?:diagram(?:me)?s?|schematics?)\b|\bwiring\s+diagram(?:me)?s?\b"
           r"|\bbreadboards?\b|\bpcb\s+layouts?\b"),
    Visual("image", "a picture of something real",
           "there is no image model on this platform, so nothing here can paint a scene, a logo or a photograph", "",
           # THE ARTICLE IS THE SIGNAL, and it is what keeps this entry off the
           # diagrams. "a picture of A cat" asks for a scene to be painted;
           # "a picture of THE deploy pipeline" asks for the thing this
           # conversation owns, which the model draws as a flowchart. So the
           # bare-noun words need an indefinite article, while `image`, `photo`
           # and `logo` — which name a raster picture outright — do not.
           r"\b(?:pictures?|drawings?|illustrations?|sketch(?:es)?|paintings?|artworks?)\s+of\s+(?:a|an)\b"
           r"|\b(?:images?|photos?|photographs?|logos?|posters?|banners?|avatars?|thumbnails?)\s+of\b"
           r"|\btext[\s-]to[\s-]image\b|\bimage\s+generation\b|\bai[\s-]generated\s+(?:image|picture|art)s?\b"),
    Visual("infographic", "an infographic",
           "an infographic is hand-composed artwork rather than a chart type, and this platform has no illustration renderer", "bar",
           r"\binfo\s?graphics?\b", offer_without_table=False),
    Visual("isometric_3d", "a 3D drawing",
           "this platform renders nothing in three dimensions: every chart and diagram here is flat", "",
           r"\bisometric\b|\b3\s?-?\s?d\s+(?:diagram(?:me)?s?|render(?:ing)?s?|models?|views?|illustrations?|drawings?|pictures?|scenes?)\b"),
    # A DECISION TREE IS TWO DIFFERENT ASKS, and the word is not the meaning
    # here either. "decision tree for choosing an effort level" is a diagram of
    # a structure and `pictures` draws it. "fit a decision tree on this dataset
    # and show it" asks for a model to be TRAINED and its splits drawn, which
    # is not a chart type and not a diagram: there is no training step here.
    Visual("decision_tree_model", "a fitted decision tree",
           "this platform has no model-training step, so nothing here can fit a tree to a dataset and draw the splits it learned", "bar",
           r"\b(?:fit|fits|fitting|fitted|train|trains|training|trained|learn|learns|grow)\b"
           r"[^.;!?\n]{0,40}?\bdecision\s+trees?\b"
           r"|\bdecision\s+trees?\b[^.;!?\n]{0,40}?\b(?:on|from|over|against|using)\s+"
           r"(?:this|that|the|my|our)\s+(?:data\s?set|data|table|sheet|csv|rows|records|file)\b",
           offer_without_table=False),
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


#: A PICTURE ASKED FOR AS A NOUN PHRASE, which is how people actually type it.
#: `_ASK_RE` needs a verb, so the honest refusal was lost exactly where the
#: sentence was shortest. Measured pairs in the running container, 2026-09-28:
#:
#:   "venn diagram of the three plans and what they share"  -> no refusal
#:   "draw a VENN of python vs sql skills on the team"       -> refused, well
#:   "sankey diagram of how users move between stages"       -> no refusal
#:   "draw a sankey of the energy flow"                      -> refused, well
#:   "choropleth of revenue by state"                        -> no refusal
#:   "draw a choropleth of revenue by state"                 -> refused, well
#:
#: In the verb-less cases the model happened to refuse well on its own — but
#: that is the model's judgement, not this product's guarantee, and the same
#: turn on a loaded box or a different model is the 2026-09-16 incident again.
#:
#: The shape is: the visual's name, optionally then the picture noun, then a
#: RELATION that says what the picture is of. "floor plan OF a 2bhk", "circuit
#: diagram FOR an LED", "chord diagram OF who emails whom", "network diagram
#: FROM this edge list". A bare mention keeps no relation — "the wireframe in
#: that doc is unreadable" — and stays a remark.
_NOUN_PHRASE_ASK_RE = re.compile(
    r"^(?:\s*(?:diagram(?:me)?|chart|graph|plot|map|view|board|picture|visual|visuali[sz]ation)s?)?"
    r"\s+(?:of|for|from|showing|shows|between|across|by|per|with)\b",
    re.I,
)
#: The same relation words, looked for INSIDE the match. `_NETWORK_PATTERN`'s
#: data arm swallows its own relation ("network diagram FROM this edge list"),
#: so there is nothing left after the match to read.
_RELATION_INSIDE_RE = re.compile(r"\b(?:of|for|from|showing|between|across|by|per|with)\b", re.I)


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

    The name plus either a word that asks to see it — "plot this on a map",
    "can you draw a choropleth" — or the noun-phrase shape people type instead
    of a verb: "choropleth of revenue by state" (`_NOUN_PHRASE_ASK_RE`). "The
    treemap in that paper is unreadable" is neither, and stays a remark.
    """
    t = (text or "")[:4000]
    v = named_unsupported(t)
    if v is None:
        return None
    if _ASK_RE.search(t):
        return v
    m = re.search(v.pattern, t, re.I)
    if m is None:
        return None
    if _NOUN_PHRASE_ASK_RE.match(t[m.end():]) or _RELATION_INSIDE_RE.search(m.group(0)):
        return v
    return None


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
