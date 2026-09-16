"""What the person asked for, as a checklist the produced files can be held to.

TWO INDEPENDENT DERIVATIONS (the circularity correction). The style patch
and the edit plan are built by style.parse_style_request. If the checklist
were built by the same parser, a misparse ("headings dark blue" read as the
title) would make the plan and the check agree and the file would "pass"
while missing the intent. So:

  (A) the RULE EXTRACTOR below has its own vocabulary and its own clause
      grammar (English, Hinglish, Hindi, Gujarati, common typos). It does
      not import the style parser for its decisions; when the styling
      track's parser is present it is consulted only to ADD items this
      extractor has no words for, marked source='rule'.
  (B) the MODEL PROPOSER (Think/Max, thinking off, <= 600 tokens, 8 s,
      skipped when chat is busy) sees the raw instruction and a short
      summary of the source material — never the spec, the plan, or (A)'s
      items — and writes items in the same typed vocabulary.
  (C) MERGE: both agree -> must; rule-only -> must for format, layout,
      data, content, faithfulness, preservation and security, and for
      style when the clause named its element explicitly; model-only ->
      should (must=False); the two disagree on the element or the value ->
      contested (must=False, never auto-repaired, reported as "I read X
      as Y").

VAGUE WORDS ARE NOT JUDGED. "Classy", "professional", "standard" map to
measurable house-style invariants (a title block, a filled table header,
page numbers, no letter-spaced title, fonts that cover the script, header
text that reaches 4.5:1 on its fill). They are must-items only when the
adjective was used; otherwise they are recorded as should-items.

EXPORTS ARE ABOUT FAITHFULNESS. When the job exports an earlier answer
("give it in docs"), the dominant requirement is that every heading line
and every table cell of that answer is in the file: two faithfulness items
carry the source structure, extracted here from the markdown.

The vocabulary (category, target, property, expected) is documented in
tests/fixtures/selfcheck/requests_labelled.py and docs/artifact-studio/as3/
agentic-selfcheck.md; the category enum is closed and a checklist holds at
most 40 items.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .inspect_files import norm_hex, norm_text, script_of

log = logging.getLogger(__name__)

CATEGORIES = ("format", "content", "style", "chart", "layout", "data", "language", "faithfulness", "preservation", "house_style", "security")
MAX_ITEMS = 40
#: Categories whose rule-only items are must-items without the model's vote.
_RULE_MUST = frozenset({"format", "layout", "data", "content", "faithfulness", "preservation", "security", "chart"})
STYLE_PROPERTIES = ("color", "background", "font_family", "size_pt", "bold", "italic", "underline")


@dataclass
class ChecklistItem:
    id: str
    category: str
    target: str
    property: str
    expected: Any
    must: bool = True
    contested: bool = False
    source: str = "rule"  # rule | model | both
    locator: Dict[str, Any] = field(default_factory=dict)
    phrase: str = ""  # the words of the request this came from (for "I read X as Y")
    note: str = ""

    def key(self) -> Tuple[str, str, str, str]:
        return (self.category, self.target, self.property, _norm_expected(self.expected))

    def to_dict(self) -> dict:
        return {"id": self.id, "category": self.category, "target": self.target, "property": self.property,
                "expected": self.expected, "must": self.must, "contested": self.contested, "source": self.source,
                "locator": dict(self.locator), "phrase": self.phrase[:120], "note": self.note[:200]}


@dataclass
class Checklist:
    items: List[ChecklistItem] = field(default_factory=list)
    #: The source answer's structure for faithfulness items (headings and
    #: table cells) — kept in memory for evaluation, counted in the report.
    source_structure: Dict[str, List[str]] = field(default_factory=dict)
    model_calls: int = 0
    model_skipped: str = ""  # "" | fast | busy | disabled | timeout | error
    rule_items: int = 0
    model_items: int = 0

    def to_dict(self) -> dict:
        return {"items": [i.to_dict() for i in self.items], "model_calls": self.model_calls, "model_skipped": self.model_skipped,
                "rule_items": self.rule_items, "model_items": self.model_items,
                "source_structure": {k: len(v) for k, v in self.source_structure.items()}}


def _scope_of(item: "ChecklistItem") -> Tuple[str, str]:
    return (norm_text(item.locator.get("section") or ""), str(item.locator.get("section_index") or ""))


def _norm_expected(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{float(value):g}"
    hx = norm_hex(value) if isinstance(value, str) and value.strip().startswith("#") else None
    if hx:
        return hx
    return norm_text(value)


# ------------------------------------------------------------ vocabulary --

#: The style guide's named colours (docs: TechSara Classic §1). Kept here,
#: not imported from style.py, on purpose: this is the INDEPENDENT reading.
COLOR_NAMES: Dict[str, str] = {
    "dark blue": "#1F3864", "navy blue": "#1F3864", "navy": "#1F3864", "blue": "#2F6FB2", "light blue": "#DCE6F2",
    "dark green": "#1E6B34", "green": "#3F8F4F", "light green": "#E3F2E6", "red": "#C62828", "dark red": "#9B1C1C",
    "light red": "#FDE4E4", "orange": "#E07B00", "amber": "#B7791F", "yellow": "#FFD54F", "light yellow": "#FFF1C7",
    "purple": "#6D5AE6", "pink": "#D63384", "grey": "#6B7280", "gray": "#6B7280", "light grey": "#EEF0F3",
    "light gray": "#EEF0F3", "black": "#000000", "white": "#FFFFFF", "brown": "#8A5A44", "teal": "#0E9D9A",
    "gold": "#B7791F", "maroon": "#7B1E1E",
    # Hinglish
    "gehra neela": "#1F3864", "gehra nila": "#1F3864", "neela": "#2F6FB2", "nila": "#2F6FB2", "hara": "#3F8F4F", "lal": "#C62828",
    "peela": "#FFD54F", "pila": "#FFD54F", "narangi": "#E07B00", "kala": "#000000", "safed": "#FFFFFF", "gulabi": "#D63384",
    "baingani": "#6D5AE6", "bhura": "#8A5A44", "sleti": "#6B7280",
    # Hindi
    "गहरा नीला": "#1F3864", "नीला": "#2F6FB2", "नीले": "#2F6FB2", "हरा": "#3F8F4F", "हरे": "#3F8F4F", "लाल": "#C62828", "पीला": "#FFD54F",
    "नारंगी": "#E07B00", "काला": "#000000", "सफ़ेद": "#FFFFFF", "सफेद": "#FFFFFF", "गुलाबी": "#D63384", "बैंगनी": "#6D5AE6",
    "भूरा": "#8A5A44", "स्लेटी": "#6B7280",
    # Gujarati
    "ઘેરો વાદળી": "#1F3864", "વાદળી": "#2F6FB2", "લીલો": "#3F8F4F", "લીલા": "#3F8F4F", "લાલ": "#C62828", "પીળો": "#FFD54F",
    "નારંગી": "#E07B00", "કાળો": "#000000", "સફેદ": "#FFFFFF", "ગુલાબી": "#D63384", "જાંબલી": "#6D5AE6", "ભૂરો": "#8A5A44",
    "રાખોડી": "#6B7280",
}
_COLOR_RE = re.compile(
    r"(?<![\w#])(" + "|".join(re.escape(k) for k in sorted(COLOR_NAMES, key=len, reverse=True)) + r")(?!\w)"
    r"|(#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b)",
    re.I,
)

FONTS = ("Times New Roman", "Courier New", "Segoe UI", "Open Sans", "Book Antiqua", "Noto Sans", "Arial", "Calibri", "Cambria",
         "Georgia", "Helvetica", "Verdana", "Tahoma", "Garamond", "Roboto", "Lato", "Montserrat", "Palatino", "Carlito",
         "Caladea", "Aptos", "Trebuchet MS", "Century Gothic", "Poppins", "Inter")
_FONT_RE = re.compile(r"(?<!\w)(" + "|".join(re.escape(f) for f in FONTS) + r")(?!\w)", re.I)

#: Element words -> target. Order matters: longer phrases first.
_ELEMENTS: List[Tuple[str, str]] = [
    (r"slide\s*titles?", "slide_title"),
    (r"chart\s*titles?|graph\s*titles?", "chart_title"),
    (r"sub[\s-]?titles?|sub[\s-]?heading\s+of\s+the\s+document|उपशीर्षक", "subtitle"),
    (r"table\s*headers?|header\s*rows?|headers?\s*row|column\s*headers?|table\s*heading", "table_header"),
    (r"total\s*rows?|totals?", "table_total"),
    (r"h1\b|heading\s*1\b|main\s+headings?", "heading1"),
    (r"h2\b|heading\s*2\b|sub\s*headings?|subheadings?", "heading2"),
    (r"h3\b|heading\s*3\b", "heading3"),
    (r"headings?|headers?|hedings?|headdings?|haedings?|हेडिंग्स?|हेडिंग|શીર્ષકો|હેડિંગ્સ?|હેડિંગ|section\s+titles", "heading"),
    (r"titles?|titel|tittle|शीर्षक|टाइटल|શીર્ષક|ટાઇટલ", "title"),
    (r"body(\s*text)?|paragraphs?|normal\s+text|body\s*font|content\s*text|बॉडी", "paragraph"),
]
# Latin words end at a word boundary ("titled Regional Sales" names no
# title element — verifier 2026-09-15); Indic words are left open because
# their vowel signs are not \w.
_ELEMENT_RE = [(re.compile(r"(?<![\w-])(?:" + rx + r")(?![A-Za-z])", re.I), target) for rx, target in _ELEMENTS]
_COLUMN_RE = re.compile(r"(?:\bthe\s+)?\b([A-Za-z][\w ]{0,30}?)\s+column\b|\bcolumn\s+([A-Z][\w]{0,30})", re.I)
_ROW_RE = re.compile(r"\brow\s+(\d{1,5})\b", re.I)
_RANGE_RE = re.compile(r"\b([A-Z]{1,3}\d{1,7}:[A-Z]{1,3}\d{1,7})\b")
#: A clause word that is a style or a level, never a section name ("bold section").
_STYLE_WORD_RE = re.compile(r"(?:bold|italics?|underlined?|body|main|sub|top|level|\d+)", re.I)
_BACKGROUND_WORDS = re.compile(r"\b(background|bg|fill|filled|highlight(?:ed)?|shad(?:e|ing|ed)|band)\b", re.I)
_TEXT_WORDS = re.compile(r"\b(text|font\s*colou?r|letters|writing)\b", re.I)
_SIZE_RE = re.compile(r"(?:\bfont\s*size\s*(?:of\s*)?(\d{1,2}(?:\.\d)?)|\b(\d{1,2}(?:\.\d)?)\s*(?:pt|pts|point|points)\b|\bsize\s*(\d{1,2}(?:\.\d)?)\b)", re.I)
_BOLD_RE = re.compile(r"\b(bold|bolded|mota|मोटा|बोल्ड|બોલ્ડ)\b", re.I)
_ITALIC_RE = re.compile(r"\b(italics?|italicised|italicized|तिरछा|इटैलिक|ઇટાલિક)\b", re.I)
_UNDERLINE_RE = re.compile(r"\b(underlined?|underlines?|रेखांकित)\b", re.I)
_CHART_WORD_RE = re.compile(r"\b(chart|graph|plot|bars?|lines?|slices?|series|pie|scatter|histogram|donut|doughnut|funnel|heatmap)\b|चार्ट|ग्राफ|ચાર્ટ", re.I)

_FORMAT_WORDS: List[Tuple[str, str]] = [
    (r"\bdocx\b|\bdox\b|\bdocs?\b|\bword\b|वर्ड|વર્ડ|\bdoc\s*file\b", "docx"),
    (r"\bpdf\b|पीडीएफ|પીડીએફ", "pdf"),
    (r"\bxlsx\b|\bxlxs\b|\bexcel\b|\bexel\b|\bexcell\b|एक्सेल|એક્સેલ", "xlsx"),
    (r"\bcsv\b", "csv"),
    (r"\bpptx?\b|\bpresentation\b|\bpresentaion\b|\bpowerpoint\b|\bdeck\b|\bslides\b|प्रेजेंटेशन", "pptx"),
    (r"\bpng\b", "png"),
    (r"\bsvg\b", "svg"),
]
_FORMAT_RES = [(re.compile(rx, re.I), fmt) for rx, fmt in _FORMAT_WORDS]

_CHART_TYPES: List[Tuple[str, str]] = [
    (r"stacked\s*(bar|column)s?", "stacked_bar"),
    (r"horizontal\s*bar", "bar"),
    (r"\bline\s*(?:chart|graph|plot)s?\b|\b(?:as|in)\s+(?:a\s+)?line\b(?!\s+(?:of|item|break|spacing|height)\b)"
     r"|(?<!trend\s)(?<!trend-)\bline\b(?!\s+(?:of|item|break|spacing|height)\b)(?=.*\b(?:chart|graph|plot)\b)(?<!\bof\sa\sline)|trend\s*chart", "line"),
    (r"\bbar\s*(chart|graph|plot)?|\bcolumn\s*chart", "bar"),
    (r"\bpie\b", "pie"),
    (r"\bdonut|\bdoughnut", "donut"),
    (r"\bscatter", "scatter"),
    (r"\bhistogram", "histogram"),
    (r"\bheat\s*map", "heatmap"),
    (r"\barea\s*(chart|graph)", "area"),
    (r"\bfunnel", "funnel"),
    (r"\bgantt|\btimeline\s*chart", "gantt"),
    (r"\bbox\s*(plot|chart)", "box"),
    (r"\bwaterfall", "waterfall"),
    (r"\bradar", "radar"),
    (r"\bbubble", "bubble"),
    (r"\bdual\s*axis|\bcombo", "combo"),
    (r"\bpareto", "pareto"),
    (r"\btree\s*map", "treemap"),
    (r"\bviolin", "violin"),
    # "candle" alone is a wax object; the chart always says candlestick, OHLC
    # or "candle chart".
    (r"\bcandlestick|\bohlc\b|\bcandle\s*(chart|graph|plot)", "candlestick"),
    (r"\bsun\s*burst", "sunburst"),
    # "bullet" alone is a bullet point in every other sentence of a deck.
    (r"\bbullet\s*(chart|graph)", "bullet"),
]
_CHART_TYPE_RES = [(re.compile(rx, re.I), t) for rx, t in _CHART_TYPES]

_CLASSY_RE = re.compile(r"\b(classy|professional|standard|formal|elegant|executive|boardroom|polished|corporate|sophisticated)\b|प्रोफेशनल|પ્રોફેશનલ", re.I)
_EXPORT_RE = re.compile(
    r"\b(it|this|that|above|same|last\s+answer|previous\s+answer|your\s+answer|the\s+answer|the\s+report|isko|ise|isse|ye|yeh|aa|aane)\b"
    r"|इसे|इसको|इस\s*को|आને|આને|આ\s+જવાબ|\bconvert\b",
    re.I,
)
_CONDENSED_RE = re.compile(
    r"\b(summar(?:y|ies|i[sz]e[ds]?)|short(?:er)?|brief|briefly|condense[ds]?|key\s+points|highlights|gist|tl;?dr|overview|outline\s+only"
    r"|one[\s-]pag(?:e|er)|1[\s-]pag(?:e|er)|\d+[\s-]slides?|only\s+the|top\s+\d+|abstract|executive\s+summary|saar|saransh|chhota|chota)\b"
    r"|सारांश|संक्षेप|छोटा|સારાંશ|ટૂંકમાં|ટૂંકું",
    re.I,
)
_SPLIT_RE = re.compile(r"\s*(?:[,;.](?!\d)|\s+and\s+|\s+aur\s+|\s+with\s+|\s+ane\s+|\s+और\s+|\s+અને\s+|\s+plus\s+|\s+but\s+|\s+then\s+)\s*", re.I)


_SOURCE_BEFORE_RE = re.compile(r"\b(the|this|that|uploaded|attached|above|your|my)\s+$", re.I)
_SOURCE_AFTER_RE = re.compile(r"^\s+(about|says|said|was|is|has|had|looks|from|i|we|you|attached|uploaded|shared|ki|ka|ke|me\s+kya)\b", re.I)


def _names_a_source(text: str, m: "re.Match[str]") -> bool:
    """'the pdf about the red team was good' names what was READ, not what
    to make: a determiner before the format word and a verb or preposition
    after it."""
    return bool(_SOURCE_BEFORE_RE.search(text[:m.start()]) and _SOURCE_AFTER_RE.search(text[m.end():]))


#: Marks a chart colour can apply to. "bar chart of apple vs orange sales"
#: names a fruit, not a series colour (verifier 2026-09-15): the colour must
#: sit within a few words of one of these, and "bar chart" itself is not a mark.
_MARK_RE = re.compile(r"\b(?:bars?|lines?|slices?|series|points?|markers?|wedges?|columns?|areas?|colou?r(?:s|ed)?|palette|rang)\b|रंग|રંગ", re.I)
_CHART_NOUN_RE = re.compile(r"\b(?:bar|line|column|area|pie)\s*(?:chart|graph|plot)s?\b", re.I)


def _colours_a_mark(clause: str, words: str) -> bool:
    low = clause.lower()
    at = low.find((words or "").lower())
    if at < 0:
        return False
    window = low[max(0, at - 30): at + len(words) + 30]
    return bool(_MARK_RE.search(_CHART_NOUN_RE.sub(" ", window)))


#: A style scoped to ONE section ("the Risks section paragraphs in italic",
#: "paragraphs in the Risks section italic", "section 2 text bold",
#: "Risks section ke paragraphs italic"). Its own grammar, not style.py's
#: (the independent reading). A scoped style is never checked as a global
#: one: live 2026-09-15, a correct Risks-only italic was reported as
#: "not met: body text italic — docx: 8 of 9 show False".
_SECTION_NUM_RE = re.compile(r"\bsection\s*(?:no\.?\s*|number\s*|#\s*)?(\d{1,2})\b", re.I)
_SECTION_CALLED_RE = re.compile(r"\bsection\s+(?:called|named|titled)\s+(.+)$", re.I)
_SECTION_WORD_RE = re.compile(r"\bsection(?:'s)?\b", re.I)
#: Words that end a section name read backwards from "section" or forwards
#: from "section called".
_SECTION_BOUNDARY = frozenset({
    "in", "of", "under", "within", "inside", "for", "the", "make", "set", "keep", "put", "to", "a", "an", "and", "please", "pls",
    "this", "that", "each", "every", "whole", "entire", "one", "any", "same", "new", "other", "all", "my", "our", "your", "it",
    "which", "what", "paragraphs", "paragraph", "text", "body", "font", "ke", "ki", "ka", "me", "mein", "wala", "wale", "sirf", "only",
    "is", "be", "should", "colour", "color", "coloured", "colored", "size", "heading", "headings", "title", "doc", "document",
})
_PARAGRAPH_WORD_RE = re.compile(r"\b(?:paragraphs?|body(?:\s*text)?|prose)\b", re.I)


def _name_word_ok(word: str) -> bool:
    w = word.strip(" \"'“”‘’").lower()
    return bool(w) and w not in _SECTION_BOUNDARY and not _STYLE_WORD_RE.fullmatch(w) and not _COLOR_RE.fullmatch(w) \
        and not _SIZE_RE.fullmatch(w) and w not in ("dark", "light", "pale", "pt")


def _section_scope(clause: str) -> Dict[str, Any]:
    """{'section': name} or {'section_index': n} for a clause that scopes a
    style to one section; {} otherwise."""
    m = _SECTION_NUM_RE.search(clause)
    if m:
        return {"section_index": int(m.group(1))}
    m = _SECTION_CALLED_RE.search(clause)
    if m:
        words: List[str] = []
        for w in m.group(1).split():
            if not _name_word_ok(w) or len(words) == 4:
                break
            words.append(w.strip(" \"'“”‘’"))
        return {"section": " ".join(words)[:80]} if words else {}
    for m in _SECTION_WORD_RE.finditer(clause):
        before = clause[:m.start()].split()
        words = []
        while before and len(words) < 4 and _name_word_ok(before[-1]):
            words.insert(0, before.pop().strip(" \"'“”‘’"))
        if words:
            return {"section": " ".join(words)[:80]}
    return {}


_DARK_WORDS = ("dark", "navy", "maroon", "gehra", "गहरा", "ઘેરો")
_LIGHT_WORDS = ("light", "pale", "halka", "हल्का", "આછો")


def _named(words: str) -> Dict[str, Any]:
    """A colour given by NAME is a hue (and a shade class), not a hex: the
    renderers legitimately use a text-safe or fill variant of the hue
    (style guide §1: orange text is #B35F00; a red highlight is a light red
    fill). A hex the person typed is held exactly."""
    w = (words or "").strip()
    if not w or w.startswith("#"):
        return {}
    shade = "dark" if any(d in w.lower() for d in _DARK_WORDS) else ("light" if any(x in w.lower() for x in _LIGHT_WORDS) else "")
    return {"color_name": w.lower(), "shade": shade}


def _elements_in(clause: str, kind: str) -> List[Tuple[str, Dict[str, Any], int]]:
    """(target, locator, position) for every element word of one clause.
    Workbook requests read a bare 'header' as the header row."""
    found: List[Tuple[str, Dict[str, Any], int]] = []
    taken: List[Tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= x or a >= y for x, y in taken)

    for m in _RANGE_RE.finditer(clause):
        found.append((f"cell_range:{m.group(1).upper()}", {}, m.start()))
        taken.append(m.span())
    for m in _ROW_RE.finditer(clause):
        if free(*m.span()):
            found.append((f"row:{m.group(1)}", {}, m.start()))
            taken.append(m.span())
    for m in _COLUMN_RE.finditer(clause):
        name = (m.group(1) or m.group(2) or "").strip()
        name = re.sub(r"^(?:(?:the|a|an|make|and|with|of|set|change|colou?r|highlight)\s+)+", "", name, flags=re.I).strip()
        if name and free(*m.span()) and name.lower() not in ("new", "a", "one", "extra"):
            found.append((f"column:{norm_text(name.split()[-1] if len(name.split()) > 2 else name)}", {}, m.start()))
            taken.append(m.span())
    for rx, target in _ELEMENT_RE:
        for m in rx.finditer(clause):
            if not free(*m.span()):
                continue
            tgt = target
            word = m.group(0).lower()
            if tgt == "heading" and word.startswith("header"):
                tgt = "table_header" if kind == "workbook" else "heading"
            if tgt == "title" and kind == "workbook" and _CHART_WORD_RE.search(clause):
                continue
            locator: Dict[str, Any] = {}
            before = clause[max(0, m.start() - 12):m.start()].lower()
            if re.search(r"\bfirst\s*$", before):
                locator["which"] = "first"
            found.append((tgt, locator, m.start()))
            taken.append(m.span())
    # "H2 headings": the level word names the element; the plural after it
    # is the same element, not all headings.
    if any(t in ("heading1", "heading2", "heading3") for t, _, _ in found):
        found = [f for f in found if f[0] != "heading"]
    found.sort(key=lambda t: t[2])
    return found


def _style_props(clause: str) -> List[Tuple[str, Any, str]]:
    """(property, expected, words) of one clause, colours unassigned yet as
    'colour' so the element decides text vs background."""
    props: List[Tuple[str, Any, str]] = []
    for m in _COLOR_RE.finditer(clause):
        hx = norm_hex(m.group(2)) if m.group(2) else COLOR_NAMES.get(m.group(1).lower(), COLOR_NAMES.get(m.group(1), ""))
        if hx:
            props.append(("colour", hx, m.group(0)))
    fm = _FONT_RE.search(clause)
    if fm:
        canonical = next(f for f in FONTS if f.lower() == fm.group(1).lower())
        props.append(("font_family", canonical, fm.group(0)))
    sm = _SIZE_RE.search(clause)
    if sm:
        value = float(next(g for g in sm.groups() if g))
        if 6 <= value <= 72:
            props.append(("size_pt", value, sm.group(0)))
    if _BOLD_RE.search(clause):
        props.append(("bold", True, "bold"))
    if _ITALIC_RE.search(clause):
        props.append(("italic", True, "italic"))
    if _UNDERLINE_RE.search(clause):
        props.append(("underline", True, "underline"))
    return props


def _unique_pairs(seq: Sequence[Any]) -> List[Any]:
    """`seq` in order with repeats removed (entries may hold dicts/lists)."""
    seen: Set[str] = set()
    out: List[Any] = []
    for entry in seq:
        marker = repr(entry)
        if marker not in seen:
            seen.add(marker)
            out.append(entry)
    return out


def extract_rules(instruction: str, *, kind: str, operation: str, has_source_answer: bool = False) -> List[ChecklistItem]:
    """(A) The rule extractor. Pure and synchronous."""
    text = " ".join(str(instruction or "").split())
    items: List[ChecklistItem] = []

    def add(category: str, target: str, prop: str, expected: Any, *, must: Optional[bool] = None, phrase: str = "", locator=None, note: str = "") -> None:
        item = ChecklistItem(id="", category=category, target=target, property=prop, expected=expected,
                             must=(category in _RULE_MUST) if must is None else must, source="rule",
                             phrase=phrase or text[:120], locator=dict(locator or {}), note=note)
        if all(i.key() != item.key() or _scope_of(i) != _scope_of(item) for i in items):
            items.append(item)

    low = text.lower()
    # Formats the words name (the engine's own choice is not a requirement).
    fmts: List[str] = []
    for rx, fmt in _FORMAT_RES:
        m = next((mm for mm in rx.finditer(text) if not _names_a_source(text, mm)), None)
        if m and fmt not in fmts:
            # "the word 'doc' in 'document'" is excluded by \b; "docs" as
            # in "in docs" is the production typo and counts.
            fmts.append(fmt)
            add("format", "file", "format", fmt, phrase=m.group(0))

    # Layout.
    if re.search(r"\blandscape\b|लैंडस्केप|લેન્ડસ્કેપ|\bland\s*scape\b", text, re.I):
        add("layout", "page", "orientation", "landscape", phrase="landscape")
    elif re.search(r"\bportrait\b|पोर्ट्रेट", text, re.I):
        add("layout", "page", "orientation", "portrait", phrase="portrait")
    for rx, size in ((r"\bA4\b", "A4"), (r"\bletter\s*(size|paper|page)?\b(?!\s*(to|for|of))", "Letter"), (r"\blegal\s*(size|paper|page)?\b", "Legal"), (r"\bA3\b", "A3")):
        if re.search(rx, text, re.I) and (size != "Letter" or re.search(r"\bletter\s*(size|paper|page)\b|\bus\s*letter\b", text, re.I)):
            add("layout", "page", "page_size", size, phrase=size)
    if re.search(r"\bnarrow\s*margins?\b|\bsmall\s*margins?\b", text, re.I):
        add("layout", "page", "margins", "narrow", phrase="narrow margins")
    elif re.search(r"\bwide\s*margins?\b|\bbig\s*margins?\b|\blarge\s*margins?\b", text, re.I):
        add("layout", "page", "margins", "wide", phrase="wide margins")
    if re.search(r"\bpage\s*(numbers?|no\.?s?|numbering)\b|पेज\s*नंबर|पृष्ठ\s*संख्या|પેજ\s*નંબર|પાન\s*નંબર", text, re.I):
        add("layout", "page", "page_numbers", True, phrase="page numbers")

    # Charts.
    chart_types: List[str] = []
    bar_match = None
    for rx, ctype in _CHART_TYPE_RES:
        m = rx.search(text)
        if not m or ctype in chart_types:
            continue
        if ctype == "bar":
            # "red bars" describes a histogram's marks; a bar chart only
            # when no more specific type was named.
            bar_match = bar_match or (m if re.search(r"\b(chart|graph|plot|bars)\b", low) else None)
            continue
        chart_types.append(ctype)
        add("chart", "chart", "type", ctype, phrase=m.group(0))
    if bar_match is not None and not chart_types:
        chart_types.append("bar")
        add("chart", "chart", "type", "bar", phrase=bar_match.group(0))
    charted = bool(chart_types) or bool(re.search(r"\b(chart|graph|plot)\b|चार्ट|ग्राफ|ચાર્ટ", text, re.I))
    if charted:
        tm = re.search(r"\btitled\s+[\"']?(.+?)[\"']?(?=\s+(?:with|and|in|as|,)\b|[,.;]|$)", text, re.I) or \
            re.search(r"\b(?:chart\s+)?title\s+[\"']?([A-Z][\w]*(?:\s+(?:[A-Z][\w]*|by|of|and|per|vs|in))*\s*[A-Z]?[\w]*)[\"']?", text)
        if tm and tm.group(1).strip():
            add("chart", "chart", "title", tm.group(1).strip(), phrase=tm.group(0))
        lm = re.search(r"\blegend\s+(?:at|on|to)?\s*(?:the\s+)?(bottom|top|right|left)\b", text, re.I)
        if lm:
            add("chart", "chart", "legend_position", lm.group(1).lower(), phrase=lm.group(0))
        if re.search(r"\b(data|value)\s*labels?\b|\blabels?\s+on\s+(the\s+)?(bars|slices|points)\b", text, re.I):
            add("chart", "chart", "data_labels", True, phrase="data labels")
        if re.search(r"\btrend\s*-?\s*line\b|\bline\s+of\s+best\s+fit\b|\bregression\s+line\b", text, re.I):
            add("chart", "chart", "trendline", True, phrase="trend line")

    # Style, clause by clause.
    clauses = [c for c in _SPLIT_RE.split(text) if c and c.strip()]
    previous: List[Tuple[str, Dict[str, Any]]] = []
    style_seen = False
    for clause in clauses:
        props = _style_props(clause)
        elements = _elements_in(clause, kind)
        in_chart = bool(_CHART_WORD_RE.search(clause)) and not elements
        if not props:
            if elements:
                previous = [(t, loc) for t, loc, _ in elements]
            continue
        if in_chart and charted:
            series_clause = False
            for prop, value, words in props:
                if prop == "colour" and _colours_a_mark(clause, words):
                    add("chart", "chart", "series_color", value, phrase=clause, locator=_named(words))
                    series_clause = True
            previous = [("__series__", {})] if series_clause else []
            continue
        if previous == [("__series__", {})] and not elements:
            # "slices in red and green": the colour after the conjunction
            # belongs to the same marks.
            for prop, value, words in props:
                if prop == "colour":
                    add("chart", "chart", "series_color", value, phrase=clause, locator=_named(words))
            continue
        scope = _section_scope(clause) if kind == "document" else {}
        explicit = bool(elements) or bool(scope)
        targets = [(t, loc) for t, loc, _ in elements]
        if scope:
            # A section named with no element word styles its body text.
            targets = targets or [("paragraph", {})]
            if _PARAGRAPH_WORD_RE.search(clause):
                scope = {**scope, "paragraphs_only": True}
            targets = [(t, {**loc, **scope}) if t in ("paragraph", "table_header", "table_total") or t.startswith("column:") else (t, loc)
                       for t, loc in targets]
        if not targets:
            if any(p == "font_family" for p, _, _ in props) and re.search(r"\bfont\b", clause, re.I):
                targets = [("paragraph", {})]
            elif previous:
                targets = previous
            else:
                continue
        previous = targets
        # Read once per clause, and each (target, locator) / (prop, value,
        # words) once: a 4,000-character clause of repeated element and
        # colour words made targets x props regex searches (10 s of event
        # loop at the cap, verifier 2026-09-15).
        background_words = bool(_BACKGROUND_WORDS.search(clause))
        text_words = bool(_TEXT_WORDS.search(clause))
        targets = _unique_pairs(targets)
        props = _unique_pairs(props)
        for target, locator in targets:
            if target == "chart_title":
                continue
            for prop, value, words in props:
                if prop == "colour":
                    if background_words:
                        prop_name = "background"
                    elif text_words:
                        prop_name = "color"
                    elif target in ("table_header", "table_total") or target.startswith(("row:", "cell_range:")):
                        prop_name = "background"
                    elif target.startswith("column:") and kind == "workbook" and not text_words:
                        prop_name = "background"
                    else:
                        prop_name = "color"
                    add("style", target, prop_name, value, must=explicit, phrase=clause, locator={**locator, **_named(words)})
                else:
                    add("style", target, prop, value, must=explicit, phrase=clause, locator=locator)
                style_seen = True

    # A CSV cannot carry styling: the styled companion is an XLSX (style
    # guide §7), so it is a requirement the moment styling is asked.
    if style_seen and "csv" in fmts and "xlsx" not in fmts:
        add("format", "file", "format", "xlsx", phrase="styling on a CSV")

    # Requested sections, on the text with the style clauses taken out
    # (the P0 bug: "dark blue headings" is not a chapter).
    if operation != "edit" and kind in ("document", "presentation"):
        try:
            from .compose import requested_sections

            content_text = " , ".join(c for c in clauses if not _style_props(c) and not re.search(r"\b(landscape|portrait|margins?|page\s*numbers?)\b", c, re.I))
            for name in requested_sections(content_text):
                add("content", f"section:{norm_text(name)}", "present", True, phrase=name)
        except Exception:  # noqa: BLE001 — compose unavailable: no section items, never a crash
            pass

    # Data.
    rm = re.search(r"\b(\d{1,6})\s+(rows|records|entries|lines)\b", text, re.I)
    if rm and kind == "workbook":
        add("data", "sheet", "row_count", int(rm.group(1)), phrase=rm.group(0))
    for cm in re.finditer(r"\b(?:add|include|insert|with)\s+(?:a|an|one|new|extra|\s)*column\s+(?:for|called|named|of)?\s*[\"']?([\w][\w ]{0,30}?)[\"']?(?=\s*(?:\b(?:after|before|at|to|in)\b|,|\.|$))", text, re.I):
        add("data", f"column:{norm_text(cm.group(1))}", "present", True, phrase=cm.group(0))

    # Content language: a request written in an Indic script.
    counts = script_of(text)
    letters = sum(counts.values()) or 1
    for script in ("devanagari", "gujarati"):
        if counts[script] / letters >= 0.3 and operation != "edit":
            add("language", "document", "script", script, must=False, phrase=script)

    # Export faithfulness.
    if has_source_answer and operation == "create" and _EXPORT_RE.search(text):
        # "a one page summary of this", "a short 3-slide deck with the key
        # points": a condensed export cannot carry every cell, so coverage is
        # recorded but is not a must (verifier 2026-09-15). A deck never holds
        # a long answer whole either.
        condensed = bool(_CONDENSED_RE.search(text)) or kind == "presentation"
        note = "a condensed export: coverage is recorded, not required" if condensed else ""
        add("faithfulness", "document", "headings_covered", True, must=not condensed, phrase="the earlier answer's headings", note=note)
        add("faithfulness", "document", "table_cells_covered", True, must=not condensed, phrase="the earlier answer's tables", note=note)

    # Preservation on an edit.
    if operation == "edit":
        add("preservation", "untouched", "unchanged", True, phrase="everything else unchanged")

    # House style: measurable invariants for the vague adjectives.
    classy = bool(_CLASSY_RE.search(text))
    if kind in ("document", "presentation") or "pdf" in fmts or "docx" in fmts:
        add("house_style", "title", "title_block", True, must=classy, phrase="a title block")
        add("house_style", "table_header", "fill_present", True, must=classy, phrase="a filled table header")
        if kind == "document":
            add("house_style", "page", "page_numbers", True, must=classy, phrase="page numbers")
            add("house_style", "title", "no_letter_spacing", True, must=False, phrase="no letter-spaced title")
        add("house_style", "document", "fonts_cover_script", True, must=False, phrase="fonts that cover the text")
        add("house_style", "table_header", "contrast", True, must=False, phrase="readable table header")
    elif kind == "workbook":
        add("house_style", "table_header", "fill_present", True, must=classy, phrase="a filled header row")
    # Security: always.
    add("security", "file", "no_unsafe_links", True, phrase="no unsafe links")
    add("security", "file", "formula_text_neutralised", True, phrase="formula-like text neutralised")
    return items


def _extra_from_style_module(instruction: str, kind: str) -> List[ChecklistItem]:
    """Items the styling track's parser finds that the extractor has no
    words for. Best effort, shape-tolerant: a missing module or a patch
    shape this adapter does not know adds nothing."""
    try:
        from . import style as _style  # type: ignore
    except ImportError:
        return []
    try:
        patch, _unparsed = _style.parse_style_request(instruction, kind)
    except Exception:  # noqa: BLE001
        return []
    out: List[ChecklistItem] = []
    for rule in list(getattr(patch, "rules", None) or []):
        tgt = getattr(rule, "target", None)
        tkind = str(getattr(tgt, "kind", None) or getattr(tgt, "type", None) or "")
        level = getattr(tgt, "level", None)
        target = {"heading": f"heading{level}" if level else "heading"}.get(tkind, tkind)
        if not target:
            continue
        st = getattr(rule, "style", None)
        for prop in STYLE_PROPERTIES:
            value = getattr(st, prop, None) if st is not None else None
            if value in (None, False, ""):
                continue
            if prop in ("color", "background"):
                value = norm_hex(value) or value
            out.append(ChecklistItem("", "style", target, prop, value, must=False, source="rule", note="from the style parser"))
    return out


# -------------------------------------------------------- model proposer --

_PROPOSER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "items": {
            "type": "array", "maxItems": 20,
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "category": {"type": "string", "enum": ["format", "content", "style", "chart", "layout", "data", "language", "faithfulness"]},
                    "target": {"type": "string", "maxLength": 60},
                    "property": {"type": "string", "maxLength": 40},
                    "expected": {"type": "string", "maxLength": 80},
                },
                "required": ["category", "target", "property", "expected"],
            },
        },
    },
    "required": ["items"],
}

_PROPOSER_PROMPT = (
    "List the checkable requirements in a request for a generated file. Output JSON only. One item per requirement "
    "the person actually stated (no defaults, no guesses). Vocabulary:\n"
    "- format: target 'file', property 'format', expected docx|pdf|xlsx|csv|pptx|png|svg\n"
    "- layout: target 'page', property orientation (landscape|portrait) | page_size (A4|Letter|Legal) | margins (narrow|wide) | page_numbers (true)\n"
    "- style: target title|subtitle|heading|heading1|heading2|heading3|paragraph|table_header|table_total|slide_title|column:<name>|row:<n>|cell_range:<A1:B2>; "
    "property color (text colour) | background (fill) | font_family | size_pt | bold | italic | underline; expected a colour NAME or #hex, a font, a number, or true\n"
    "- chart: target 'chart', property type|series_color|legend_position|data_labels|title|trendline\n"
    "- content: target 'section:<name>', property 'present', expected true (only sections the person named)\n"
    "- data: target 'sheet' property 'row_count' expected N; or target 'column:<name>' property 'present'\n"
    "- faithfulness: target 'document', property headings_covered|table_cells_covered, expected true (when the request exports an earlier answer or document)\n"
    "The request may be in English, Hindi, Gujarati or Hinglish, with typos ('dox' is docx, 'peela' is yellow)."
)

Proposer = Callable[[str, str, str], Awaitable[List[dict]]]


async def _default_proposer(instruction: str, source_summary: str, kind: str) -> List[dict]:
    from .compose import _json

    messages = [
        {"role": "system", "content": _PROPOSER_PROMPT},
        {"role": "user", "content": f"File kind: {kind}\nRequest: {instruction[:1500]}\nSource material (summary): {source_summary[:1500] or '(none)'}"},
    ]
    obj = await _json(messages, _PROPOSER_SCHEMA, "artifact_checklist", thinking=False, max_tokens=600)
    return list(obj.get("items") or [])


def _normalize_model_item(raw: dict) -> Optional[ChecklistItem]:
    category = str(raw.get("category") or "").strip().lower()
    if category not in CATEGORIES:
        return None
    target = norm_text(raw.get("target") or "").replace(" ", "_") if not str(raw.get("target") or "").lower().startswith(("column:", "section:", "row:", "cell_range:")) else str(raw.get("target"))
    prop = norm_text(raw.get("property") or "").replace(" ", "_")
    expected: Any = str(raw.get("expected") or "").strip()
    target = {"headings": "heading", "header": "table_header", "headers": "table_header", "body": "paragraph", "body_text": "paragraph",
              "paragraphs": "paragraph", "titles": "title", "slide_titles": "slide_title"}.get(target, target)
    if target.startswith(("column:", "section:")):
        head, _, name = target.partition(":")
        target = f"{head}:{norm_text(name)}"
    if target.startswith("cell_range:"):
        target = "cell_range:" + target.split(":", 1)[1].upper()
    prop = {"colour": "color", "text_color": "color", "fill": "background", "font": "font_family", "font_size": "size_pt", "size": "size_pt"}.get(prop, prop)
    low = expected.lower()
    if low in ("true", "yes"):
        expected = True
    elif low in ("false", "no"):
        expected = False
    elif prop in ("color", "background", "series_color"):
        expected = norm_hex(expected) if expected.startswith("#") else COLOR_NAMES.get(low, COLOR_NAMES.get(expected, expected))
    elif prop in ("size_pt",):
        try:
            expected = float(re.sub(r"[^\d.]", "", expected))
        except ValueError:
            return None
    elif prop == "row_count":
        try:
            expected = int(re.sub(r"[^\d]", "", expected))
        except ValueError:
            return None
    elif prop == "font_family":
        expected = next((f for f in FONTS if f.lower() == low), expected)
    elif category in ("format", "layout", "chart") and isinstance(expected, str):
        expected = {"word": "docx", "excel": "xlsx", "powerpoint": "pptx", "ppt": "pptx", "letter": "Letter", "legal": "Legal", "a4": "A4"}.get(low, low if prop != "title" else expected)
    locator = _named(str(raw.get("expected") or "")) if prop in ("color", "background", "series_color") else {}
    return ChecklistItem("", category, target, prop, expected, must=False, source="model", locator=locator)


_ELEMENT_TARGETS = ("title", "subtitle", "heading", "heading1", "heading2", "heading3", "paragraph", "table_header", "table_total", "slide_title", "chart_title")


def _same_target_family(a: str, b: str) -> bool:
    return a == b or ({a, b} <= {"heading", "heading1", "heading2", "heading3"} and "heading" in (a, b))


def merge(rule_items: Sequence[ChecklistItem], model_items: Sequence[ChecklistItem]) -> List[ChecklistItem]:
    """(C) Agreement -> must; conflicts -> contested; model-only -> should."""
    out: List[ChecklistItem] = [ChecklistItem(**{**i.__dict__}) for i in rule_items]
    for m in model_items:
        exact = next((r for r in out if r.category == m.category and _same_target_family(r.target, m.target)
                      and r.property == m.property and _norm_expected(r.expected) == _norm_expected(m.expected)), None)
        if exact is not None:
            exact.source = "both"
            exact.must = True if exact.category not in ("house_style",) else exact.must
            continue
        if m.category == "style":
            # Same property and value, different element: "headings dark
            # blue" read as the title by one reader and as headings by the
            # other. Same element and property, different value: two
            # readings of the colour.
            other_target = next((r for r in out if r.category == "style" and r.property == m.property
                                 and _norm_expected(r.expected) == _norm_expected(m.expected) and not _same_target_family(r.target, m.target)
                                 and r.target in _ELEMENT_TARGETS and m.target in _ELEMENT_TARGETS), None)
            other_value = next((r for r in out if r.category == "style" and _same_target_family(r.target, m.target)
                                and r.property == m.property and _norm_expected(r.expected) != _norm_expected(m.expected)), None)
            conflict = other_target or other_value
            if conflict is not None and conflict.source != "both":
                conflict.contested = True
                conflict.must = False
                reading = f"{m.target} {m.property} {m.expected}"
                conflict.note = f"the request can also be read as {reading}"
                m.contested = True
                m.note = f"the request can also be read as {conflict.target} {conflict.property} {conflict.expected}"
                out.append(m)
                continue
        if m.category == "format" and not any(r.category == "format" and r.expected == m.expected for r in out):
            out.append(m)
            continue
        if all(r.key() != m.key() for r in out):
            out.append(m)
    return out


def _source_structure(markdown: str) -> Dict[str, List[str]]:
    """Heading lines and table cells of an earlier answer. md_import (the
    intent track) is the canonical reader when present; this fallback reads
    ATX/setext headings and GFM pipe tables."""
    try:
        from . import md_import  # type: ignore

        spec, _notes = md_import.markdown_to_document(markdown)
        body = spec.body
        headings = [b.text for b in body.blocks if getattr(b, "type", "") == "heading"]
        cells: List[str] = []
        for b in body.blocks:
            if getattr(b, "type", "") == "table":
                cells.extend(str(c) for c in b.table.columns)
                cells.extend(str(v) for row in b.table.rows for v in row if v not in (None, ""))
        return {"headings": headings, "cells": cells}
    except Exception:  # noqa: BLE001 — module absent or its reader failed: the fallback below
        pass
    headings: List[str] = []
    cells = []
    lines = (markdown or "").splitlines()
    in_code = False
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if m:
            headings.append(_strip_inline(m.group(1)))
            continue
        if i + 1 < len(lines) and re.match(r"^\s{0,3}(=+|-+)\s*$", lines[i + 1]) and line.strip() and not line.strip().startswith("|"):
            headings.append(_strip_inline(line.strip()))
            continue
        if line.strip().startswith("|") and line.strip().endswith("|"):
            if re.fullmatch(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?", line.strip()):
                continue
            for cell in line.strip().strip("|").split("|"):
                c = _strip_inline(cell.strip())
                if c:
                    cells.append(c)
    return {"headings": headings, "cells": cells}


def _strip_inline(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(\*\*|__|\*|_|`|~~)", "", text)
    return text.strip()


# ------------------------------------------------------------------ build --


async def build(job_row: Optional[dict], *, instruction: str, kind: str, formats: Sequence[str], operation: str,
                parent_spec: Any = None, source_summary: str = "", effort: str = "fast", source_markdown: str = "",
                proposer: Optional[Proposer] = None, busy: Optional[Callable[[], bool]] = None,
                model_enabled: bool = True, timeout_s: float = 8.0) -> Checklist:
    """The checklist for one job. Fast never calls the model; Think/Max
    call the proposer once unless chat is busy or the flag is off."""
    checklist = Checklist()
    rule_items = extract_rules(instruction, kind=kind, operation=operation, has_source_answer=bool((source_markdown or "").strip()))
    for extra in _extra_from_style_module(instruction, kind):
        if all(extra.key() != r.key() and not (r.category == "style" and r.target == extra.target and r.property == extra.property) for r in rule_items):
            rule_items.append(extra)
    checklist.rule_items = len(rule_items)
    model_items: List[ChecklistItem] = []
    if effort == "fast":
        checklist.model_skipped = "fast"
    elif not model_enabled:
        checklist.model_skipped = "disabled"
    elif busy is not None and _safe_busy(busy):
        checklist.model_skipped = "busy"
    else:
        fn = proposer or _default_proposer
        checklist.model_calls = 1
        try:
            raw_items = await asyncio.wait_for(fn(instruction, source_summary[:1500], kind), timeout=timeout_s)
            model_items = [i for i in (_normalize_model_item(r) for r in raw_items if isinstance(r, dict)) if i is not None]
        except asyncio.TimeoutError:
            checklist.model_skipped = "timeout"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the rule items stand alone
            log.info("checklist proposer failed: %s", type(exc).__name__)
            checklist.model_skipped = "error"
    checklist.model_items = len(model_items)
    items = merge(rule_items, model_items)
    # Faithfulness carries the source structure; without a source there is
    # nothing to be faithful to.
    if any(i.category == "faithfulness" for i in items):
        structure = _source_structure(source_markdown)
        checklist.source_structure = structure
        if not source_markdown.strip():
            items = [i for i in items if i.category != "faithfulness"]
    # Musts first, then the cap; ids are stable within the job.
    items.sort(key=lambda i: (not i.must, i.contested, CATEGORIES.index(i.category) if i.category in CATEGORIES else 99))
    items = items[:MAX_ITEMS]
    for n, item in enumerate(items, start=1):
        item.id = f"c{n:02d}"
    checklist.items = items
    return checklist


def _safe_busy(fn: Callable[[], bool]) -> bool:
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001
        return False


def describe(item: ChecklistItem) -> str:
    """A short phrase for the answer sentence: 'headings in #1F3864'."""
    target = item.target
    label = {"heading": "headings", "heading1": "main headings", "heading2": "second-level headings", "heading3": "third-level headings",
             "paragraph": "body text", "table_header": "table header", "table_total": "totals row", "slide_title": "slide titles",
             "title": "title", "subtitle": "subtitle"}.get(target, target.replace("column:", "column ").replace("row:", "row ").replace("cell_range:", "cells ").replace("section:", "section "))
    p, e = item.property, item.expected
    if item.category == "style" and (item.locator.get("section") or item.locator.get("section_index")):
        where = f"the {item.locator['section']} section" if item.locator.get("section") else f"section {item.locator['section_index']}"
        label = f"{'paragraphs' if target == 'paragraph' else label} in {where}"
    if item.category == "style":
        if p == "color":
            return f"{label} text {e}"
        if p == "background":
            return f"{label} fill {e}"
        if p == "font_family":
            return f"{label} in {e}"
        if p == "size_pt":
            return f"{label} at {e:g}pt" if isinstance(e, (int, float)) else f"{label} size {e}"
        return f"{label} {p}"
    if item.category == "format":
        if p == "formats_only":
            return "only the files that were asked for"
        return {"docx": "a Word file", "pdf": "a PDF", "xlsx": "an Excel file", "csv": "a CSV", "pptx": "a PowerPoint file",
                "png": "a PNG image", "svg": "an SVG image"}.get(str(e), f"a {e} file")
    if item.category == "layout":
        return {"orientation": f"{e} pages", "page_size": f"{e} paper", "margins": f"{e} margins", "page_numbers": "page numbers"}.get(p, p)
    if item.category == "chart":
        return {"type": f"a {str(e).replace('_', ' ')} chart", "series_color": f"chart colour {e}", "legend_position": f"legend at the {e}",
                "data_labels": "data labels", "title": f"chart title '{e}'", "trendline": "a trend line", "values_match": "chart values from the data",
                # The independent audit (artifacts/chart_audit.py).
                "binding_plausible": "a chart whose slices follow the table's numbers",
                "no_summary_category": "no totals row drawn as a slice or a bar",
                "values_recomputed": "chart values that match the table regrouped by hand"}.get(p, p)
    if item.category == "content":
        return f"a section on {target.split(':', 1)[-1]}"
    if item.category == "data":
        return f"{e} rows" if p == "row_count" else f"a column {target.split(':', 1)[-1]}"
    if item.category == "faithfulness":
        return "every heading of the earlier answer" if p == "headings_covered" else "every table cell of the earlier answer"
    if item.category == "preservation":
        return "the rest of the document unchanged"
    if item.category == "language":
        return f"text in {str(e).title()} script"
    if item.category == "house_style":
        return {"title_block": "a title block", "fill_present": "a filled table header", "page_numbers": "page numbers",
                "no_letter_spacing": "a title without letter spacing", "fonts_cover_script": "fonts that cover the script",
                "contrast": "a readable table header"}.get(p, p)
    return p.replace("_", " ")


# --------------------------------------------------- what cannot be done --

#: WHAT THIS PLATFORM CANNOT DO, in the words people ask for it.
#:
#: There is a vocabulary for what it CAN do (engines/capability.py's
#: CAPABILITY_LINE) and, until 2026-09-16, none at all for what it cannot:
#: "Make a fillable PDF form", "Build an interactive dashboard I can
#: filter", "with tracked changes turned on", "with our company letterhead
#: image", "Make a PDF and print two copies" and "Make an editable Figma
#: file of this layout" each ended on a plain "Created **X** as PDF." with
#: the impossible half of the request never mentioned (measured 2026-09-16,
#: I3-I9; I5 asked for Figma and got a Word file with no comment).
#:
#: Each entry is (the words, the clause the person reads). The clause names
#: the part that cannot be done and, where there is one, what was done
#: instead — never an apology on its own.
_CANNOT: Tuple[Tuple[str, str], ...] = (
    (r"\bfillable\b|\bfill[- ]in(?:able)?\s+form\b|\bform\s+fields?\b|\bacro ?form\b",
     "the form fields aren't fillable; the PDFs I make are flat"),
    (r"\binteractive\s+(?:dashboard|report|chart|excel|sheet|workbook|pdf|deck|version|file)\b"
     r"|\b(?:i|we|you)\s+can\s+filter\b|\bfilterable\b|\bslicers?\b|\bdrill[- ]downs?\b|\bclickable\s+(?:dashboard|filter|chart)\b",
     "it isn't interactive — the file holds the numbers as a static sheet"),
    (r"\btracked?\s+changes\b|\bredlines?\b|\bsuggesting\s+mode\b|\bcomment\s+bubbles?\b",
     "tracked changes can't be turned on in the files I make"),
    (r"\b(?:add|insert|put|place|include|use|with|using)\s+(?:(?:our|the|company|corporate|my|a|an|some|client'?s?)\s+){0,2}"
     r"(?:letterhead|logos?|watermarks?|brand\s+images?|images?|photos?|pictures?|screenshots?|icons?)\b",
     "I can't place an image, logo or letterhead in a file"),
    (r"\bprint\s+(?:\w+\s+){0,2}?(?:cop(?:y|ies)|pages?|it|this|them|out)\b|\bsend\s+(?:it|this)\s+to\s+the\s+printer\b",
     "I can't print — you can download the PDF and print it"),
    (r"\b(?:e-?mail|send)\s+(?:(?:it|this|that|them|the|our|my|a|an)\s+)?(?:\w+\s+){0,2}?to\b"
     r"|\bpost\s+(?:it|this|the\s+\w+)\s+to\b|\bslack\b|\bteams\s+channel\b",
     "I can't email or post files — you download the file from its card here"),
    (r"\bpassword[- ]protect\w*\b|\bencrypt\w*\b|\bdigitally\s+sign\w*\b|\bsign\s+it\s+digitally\b|\be-?sign\w*\b",
     "I can't password-protect, encrypt or sign a file"),
    (r"\b(?:embed|connect|link|pull\s+in)\w*\s+(?:an?\s+|the\s+)?(?:live|real[- ]time)\b"
     r"|\blive\s+(?:dashboard|feed|chart|data)\s+(?:in|into|on|to)\b|\bauto[- ]updat\w+\b|\brefreshes?\s+automatically\b",
     "I can't embed anything live — the file holds a snapshot of the numbers"),
    (r"\b(?:add|insert|embed|include|put)\s+(?:an?\s+|the\s+|our\s+)?(?:videos?|gifs?|animations?|audio|sound|clips?|recordings?)\b",
     "a file I make can't hold video or audio — a link to it can go in instead"),
    (r"\b(?:add|with|include|write|put)\s+(?:\w+\s+){0,2}?macros?\b|\bvba\b",
     "I can't put macros in a workbook"),
)
_CANNOT_RES: Tuple[Tuple["re.Pattern[str]", str], ...] = tuple((re.compile(rx, re.I), clause) for rx, clause in _CANNOT)
#: A negated or hypothetical mention is not a request ("no need to
#: password-protect it", "can a PDF hold video?").
_NOT_ASKED_RE = re.compile(r"\b(?:don'?t|do not|no need to|without|never|not)\s+(?:\w+\s+){0,3}$", re.I)
#: ...nor is a SUBJECT in front of the verb: "a report on how WE SEND data
#: to vendors" describes the topic of the file, not what to do with it.
_DESCRIBED_RE = re.compile(r"\b(?:we|they|you|he|she|it|who|that|which|users?|clients?|customers?|teams?|vendors?|staff)\s+$", re.I)


def unsupported_asks(text: str) -> List[str]:
    """The clauses for the parts of this request the platform cannot do, in
    the order they were asked for. Empty when everything asked for is
    possible. Read by engines/artifact.py, which puts them on the completion
    sentence beside the other warnings — the person is told in the same
    breath as "Created …", not left to discover it in the file."""
    low = (text or "")[:4000]
    out: List[str] = []
    for rx, clause in _CANNOT_RES:
        m = rx.search(low)
        if not m or clause in out:
            continue
        before = low[max(0, m.start() - 40): m.start()]
        if _NOT_ASKED_RE.search(before) or _DESCRIBED_RE.search(before):
            continue
        out.append(clause)
    return out


__all__ = ["CATEGORIES", "MAX_ITEMS", "ChecklistItem", "Checklist", "COLOR_NAMES", "FONTS", "extract_rules", "merge", "build",
           "describe", "unsupported_asks"]
