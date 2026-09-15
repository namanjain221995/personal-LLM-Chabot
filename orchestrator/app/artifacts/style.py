"""How an artifact LOOKS: the style model, the request parser, the resolver.

WHY A SEPARATE MODEL, WRITTEN BY CODE. A person asks for "headings dark
blue, body in Georgia 12pt, table header dark green with white bold text,
landscape". That is not content, and it is not the composer's job: a large
optional style union in the guided-JSON schema slows decoding, raises
invalid-structure failures and invites the model to decorate things nobody
asked about. So `StyleSpec` is NEVER in `spec.schema_for(kind)`. It is
written onto `spec.style` by code, from `parse_style_request` (deterministic,
EN / Hinglish / Hindi / Gujarati, typo-tolerant) and — only for the phrases
the parser could not read — ONE small `extract_patch_llm` call whose schema
is the patch alone.

WHAT IS SAFE TO INTERPOLATE. Every colour is validated to `#RRGGBB` here
(names resolve by code), every font is a key of FONT_ALLOWLIST whose CSS
stack is a constant, every size is a clamped number. The renderers therefore
interpolate only validated hex, allowlisted stacks and numbers into CSS,
Office XML and spreadsheet formulas. User TEXT that reaches a formula goes
through `xlsx_formula_literal` (quotes doubled, capped at 100 characters)
and user text that reaches an Excel header/footer through
`xlsx_header_footer_text` ('&' doubled, so `&F` cannot print the file path).

ONE RESOLVER. `resolve(spec)` turns preset + tokens + rules into a
`ResolvedStyle` that every renderer (DOCX, HTML→PDF, PPTX, XLSX, charts)
reads, so the files agree. The contrast policy lives here: a colour the
SYSTEM picks (header text on a user fill, totals text, data labels) flips
between white and ink to reach 4.5:1; an explicit user colour is honoured as
asked and a pair under 3:1 earns one warning sentence; a DOCX/PDF never gets
a full-page background.

The palette values and every contrast figure are the style guide's
(docs/artifact-studio/as3/styling-engine.md), computed with the WCAG
relative-luminance formula; tests/test_artifact_style.py recomputes them.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# =============================================================== colours ==

INK = "#1F2937"
WHITE = "#FFFFFF"
BLACK = "#000000"

#: Named colours → hex, by code. English first; Hinglish (Latin), Hindi and
#: Gujarati names map to the same values (style guide §1).
_BASE_COLOURS: Dict[str, str] = {
    "dark blue": "#1F3864", "navy": "#1F3864", "navy blue": "#1F3864", "blue": "#2F6FB2",
    "light blue": "#DCE6F2", "sky blue": "#5B9BD5", "dark green": "#1E6B34", "green": "#3F8F4F",
    "light green": "#E3F2E6", "red": "#C62828", "dark red": "#9B1C1C", "light red": "#FDE4E4",
    "orange": "#E07B00", "amber": "#B7791F", "yellow": "#FFD54F", "light yellow": "#FFF1C7",
    "purple": "#6D5AE6", "violet": "#6D5AE6", "pink": "#D63384", "grey": "#6B7280", "gray": "#6B7280",
    "light grey": "#EEF0F3", "light gray": "#EEF0F3", "dark grey": "#374151", "dark gray": "#374151",
    "black": "#000000", "white": "#FFFFFF", "brown": "#8A5A44", "teal": "#0E9D9A", "gold": "#B7791F",
    "golden": "#B7791F", "maroon": "#7B1E1E", "ink": INK,
}

#: Base hue words in other scripts and in romanised Hindi/Gujarati → the
#: English base word. Modifiers (dark/light) are matched separately.
_HUE_ALIASES: Dict[str, str] = {
    # Hinglish / Gujlish (Latin)
    "neela": "blue", "nila": "blue", "neeli": "blue", "vadli": "blue", "vadlee": "blue",
    "hara": "green", "hari": "green", "haraa": "green", "lilo": "green", "lila": "green",
    "lal": "red", "laal": "red", "peela": "yellow", "pila": "yellow", "peeli": "yellow", "pilo": "yellow",
    "narangi": "orange", "kala": "black", "kaala": "black", "kalo": "black", "safed": "white", "safaid": "white",
    "gulabi": "pink", "baingani": "purple", "jambli": "purple", "bhura": "brown", "bhuro": "brown",
    "sleti": "grey", "rakhodi": "grey", "sunehra": "gold",
    # Hindi
    "नीला": "blue", "नीले": "blue", "नीली": "blue", "हरा": "green", "हरे": "green", "हरी": "green",
    "लाल": "red", "पीला": "yellow", "पीले": "yellow", "पीली": "yellow", "नारंगी": "orange",
    "काला": "black", "काले": "black", "काली": "black", "सफ़ेद": "white", "सफेद": "white",
    "गुलाबी": "pink", "बैंगनी": "purple", "भूरा": "brown", "भूरे": "brown", "स्लेटी": "grey", "ग्रे": "grey",
    "सुनहरा": "gold", "मैरून": "maroon",
    # Gujarati
    "વાદળી": "blue", "ભૂરો": "brown", "લીલો": "green", "લીલા": "green", "લીલી": "green", "લાલ": "red",
    "પીળો": "yellow", "પીળા": "yellow", "પીળી": "yellow", "નારંગી": "orange", "કાળો": "black", "કાળા": "black",
    "કાળી": "black", "સફેદ": "white", "ગુલાબી": "pink", "જાંબલી": "purple", "રાખોડી": "grey", "સોનેરી": "gold",
}
_DARK_WORDS = ("dark", "deep", "gehra", "gehri", "gehre", "gahra", "गहरा", "गहरे", "गहरी", "ઘેરો", "ઘેરા", "ઘેરી", "ઘાટો", "ઘાટા")
_LIGHT_WORDS = ("light", "pale", "halka", "halki", "halke", "हल्का", "हल्के", "हल्की", "આછો", "આછા", "આછી", "હળવો", "હળવા")

#: The flat lookup: every base, every alias, and "dark/light + hue" in every
#: script (when the English combination exists).
COLOR_NAMES: Dict[str, str] = dict(_BASE_COLOURS)
for _alias, _hue in _HUE_ALIASES.items():
    COLOR_NAMES.setdefault(_alias, _BASE_COLOURS[_hue])
    for _mod in _DARK_WORDS:
        if f"dark {_hue}" in _BASE_COLOURS:
            COLOR_NAMES.setdefault(f"{_mod} {_alias}", _BASE_COLOURS[f"dark {_hue}"])
    for _mod in _LIGHT_WORDS:
        if f"light {_hue}" in _BASE_COLOURS:
            COLOR_NAMES.setdefault(f"{_mod} {_alias}", _BASE_COLOURS[f"light {_hue}"])
for _hue in list(_BASE_COLOURS):
    if " " in _hue:
        continue
    for _mod in _DARK_WORDS:
        if f"dark {_hue}" in _BASE_COLOURS:
            COLOR_NAMES.setdefault(f"{_mod} {_hue}", _BASE_COLOURS[f"dark {_hue}"])
    for _mod in _LIGHT_WORDS:
        if f"light {_hue}" in _BASE_COLOURS:
            COLOR_NAMES.setdefault(f"{_mod} {_hue}", _BASE_COLOURS[f"light {_hue}"])

#: When a NAMED colour is used as text on white and fails 4.5:1, the same
#: hue's text-safe variant is used and the answer names it (style guide §1).
#: A user hex code is always used as given.
TEXT_SAFE: Dict[str, str] = {
    "#E07B00": "#B35F00",  # orange
    "#B7791F": "#8A5A00",  # amber / gold
    "#FFD54F": "#806600",  # yellow
    "#0E9D9A": "#0B7A77",  # teal
    "#3F8F4F": "#2E7D32",  # green
    "#D63384": "#B02A6B",  # pink
}

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")


def _canon_hex(value: str) -> Optional[str]:
    m = _HEX_RE.match(value.strip())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h.upper()


def _fold_words(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", str(text)).replace("-", " ").split()).casefold()


def resolve_color(name_or_hex: Any) -> Optional[str]:
    """`#RRGGBB` for a hex code (3 or 6 digits, with or without '#') or a
    colour name in any of the four language forms; None when unknown."""
    if not isinstance(name_or_hex, str) or not name_or_hex.strip() or len(name_or_hex) > 40:
        return None
    hexed = _canon_hex(name_or_hex)
    if hexed:
        return hexed
    return COLOR_NAMES.get(_fold_words(name_or_hex))


def colour_name(hex_value: str) -> str:
    """The first English name of `hex_value`, or the hex itself."""
    h = (hex_value or "").upper()
    for name, value in _BASE_COLOURS.items():
        if value == h:
            return name
    return h


def _lin(channel: float) -> float:
    return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4


def luminance(hex_value: str) -> float:
    h = hex_value.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast_ratio(a: str, b: str) -> float:
    """WCAG 2.x contrast ratio of two `#RRGGBB` colours (1..21)."""
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def readable_on(background: str) -> str:
    """White or ink, whichever reads better on `background` (the system's
    choice for text it places on a fill)."""
    return WHITE if contrast_ratio(WHITE, background) >= contrast_ratio(INK, background) else INK


def _color_value(v: Any) -> Optional[str]:
    if v is None:
        return None
    out = resolve_color(v)
    if out is None:
        raise ValueError(f"not a colour: {str(v)[:40]!r}")
    return out


# ================================================================= fonts ==


@dataclass(frozen=True)
class FontFace:
    """One allowlisted family. `css_stack` is a CONSTANT string (never built
    from user text); `office_name` is what DOCX/PPTX/XLSX name. What the
    server draws with (the PDF, chart images) is the first installed family
    of `pdf_candidates`: the family itself, its `metric_substitute`, the
    documented open `fallback` the images carry when no twin is packaged,
    then the generic Liberation/DejaVu family. `render.theme.resolve_font`
    asks fontconfig and says which of those it was."""

    name: str
    office_name: str
    css_stack: str
    installed_file_candidates: Tuple[str, ...]
    metric_substitute: str
    generic: str  # sans | serif | mono
    fallback: str = ""

    @property
    def pdf_candidates(self) -> Tuple[str, ...]:
        """Families in the order the server-side renderers try them."""
        return tuple(dict.fromkeys([*self.installed_file_candidates, self.metric_substitute, *([self.fallback] if self.fallback else []),
                                    *GENERIC_FAMILIES[self.generic]]))

    @property
    def metric_compatible(self) -> bool:
        """Is `metric_substitute` a true metric twin (same advance widths),
        so text set in it breaks lines where the requested font would?"""
        return (self.name.casefold(), self.metric_substitute.casefold()) in METRIC_TWINS


#: The generic families every orchestrator image installs (fonts-liberation,
#: fonts-dejavu-core): the last resort after a face's twin and fallback.
GENERIC_FAMILIES: Dict[str, Tuple[str, ...]] = {
    "sans": ("Liberation Sans", "DejaVu Sans"),
    "serif": ("Liberation Serif", "DejaVu Serif"),
    "mono": ("Liberation Mono", "DejaVu Sans Mono"),
}

#: (requested, substitute) pairs that are metric-compatible by design:
#: Liberation 2 = Arial/Helvetica, Times New Roman, Courier New; Carlito =
#: Calibri; Caladea = Cambria; Gelasio = Georgia.
METRIC_TWINS = frozenset({
    ("arial", "liberation sans"), ("helvetica", "liberation sans"), ("times new roman", "liberation serif"),
    ("courier new", "liberation mono"), ("calibri", "carlito"), ("cambria", "caladea"), ("georgia", "gelasio"),
})


def _face(name: str, sub: str, generic: str, *, office: str = "", extra: Tuple[str, ...] = (), fallback: str = "") -> FontFace:
    tail = {
        "sans": '"Liberation Sans", Arial, "DejaVu Sans", "Noto Sans Devanagari", "Lohit Devanagari", "Noto Sans Gujarati", "Lohit Gujarati", sans-serif',
        "serif": '"Liberation Serif", "Times New Roman", "DejaVu Serif", "Noto Serif Devanagari", "Lohit Devanagari", "Noto Serif Gujarati", "Lohit Gujarati", serif',
        "mono": '"Liberation Mono", "Courier New", "DejaVu Sans Mono", monospace',
    }[generic]
    names = [name, *extra, sub, *([fallback] if fallback else [])]
    head = ", ".join(f'"{n}"' for n in dict.fromkeys(names))
    return FontFace(name=name, office_name=office or name, css_stack=f"{head}, {tail}",
                    installed_file_candidates=tuple(dict.fromkeys([name, *extra])), metric_substitute=sub, generic=generic,
                    fallback=fallback)


FONT_ALLOWLIST: Dict[str, FontFace] = {f.name.casefold(): f for f in (
    _face("Calibri", "Carlito", "sans"),
    _face("Cambria", "Caladea", "serif"),
    _face("Arial", "Liberation Sans", "sans"),
    _face("Helvetica", "Liberation Sans", "sans", office="Arial"),
    _face("Times New Roman", "Liberation Serif", "serif"),
    # Georgia: Gelasio (its metric twin, OFL) is packaged by neither Debian
    # trixie (Dockerfile.cpu) nor Ubuntu noble (Dockerfile.cuda), so the
    # images draw Georgia with Caladea, a screen serif of similar width and
    # x-height; Times-metric Liberation Serif sets it visibly narrower.
    _face("Georgia", "Gelasio", "serif", fallback="Caladea"),
    _face("Garamond", "EB Garamond", "serif"),
    _face("Book Antiqua", "TeX Gyre Pagella", "serif"),
    _face("Palatino Linotype", "TeX Gyre Pagella", "serif"),
    _face("Verdana", "DejaVu Sans", "sans"),
    _face("Tahoma", "DejaVu Sans", "sans"),
    _face("Trebuchet MS", "DejaVu Sans", "sans"),
    _face("Segoe UI", "Carlito", "sans"),
    _face("Aptos", "Carlito", "sans"),
    _face("Century Gothic", "URW Gothic", "sans"),
    _face("Roboto", "DejaVu Sans", "sans"),
    _face("Open Sans", "DejaVu Sans", "sans"),
    _face("Lato", "Carlito", "sans"),
    _face("Montserrat", "DejaVu Sans", "sans"),
    _face("Inter", "DejaVu Sans", "sans"),
    _face("Poppins", "DejaVu Sans", "sans"),
    _face("Source Sans Pro", "DejaVu Sans", "sans"),
    _face("Courier New", "Liberation Mono", "mono"),
    _face("Consolas", "DejaVu Sans Mono", "mono"),
    _face("Liberation Sans", "Liberation Sans", "sans"),
    _face("Liberation Serif", "Liberation Serif", "serif"),
    _face("Liberation Mono", "Liberation Mono", "mono"),
    _face("DejaVu Sans", "DejaVu Sans", "sans"),
    _face("DejaVu Serif", "DejaVu Serif", "serif"),
    _face("Carlito", "Carlito", "sans"),
    _face("Caladea", "Caladea", "serif"),
    _face("Noto Sans", "DejaVu Sans", "sans"),
    _face("Noto Serif", "DejaVu Serif", "serif"),
    _face("Noto Sans Devanagari", "Lohit Devanagari", "sans"),
    _face("Noto Sans Gujarati", "Lohit Gujarati", "sans"),
    _face("Lohit Devanagari", "Lohit Devanagari", "sans", fallback="Noto Sans Devanagari"),
    _face("Lohit Gujarati", "Lohit Gujarati", "sans", fallback="Noto Sans Gujarati"),
    _face("Nirmala UI", "Noto Sans Devanagari", "sans", extra=("Lohit Devanagari",)),
    _face("Mangal", "Lohit Devanagari", "sans", fallback="Noto Sans Devanagari"),
    _face("Shruti", "Lohit Gujarati", "sans", fallback="Noto Sans Gujarati"),
)}

#: Informal names people type → the allowlist key.
_FONT_ALIASES: Dict[str, str] = {
    "times": "times new roman", "times roman": "times new roman", "tnr": "times new roman",
    "courier": "courier new", "palatino": "palatino linotype", "segoe": "segoe ui", "trebuchet": "trebuchet ms",
    "sans serif": "arial", "sans-serif": "arial", "sans": "arial", "serif": "times new roman", "monospace": "courier new",
    "mono": "courier new", "source sans": "source sans pro", "calibry": "calibri", "calbri": "calibri", "georiga": "georgia",
    "gorgia": "georgia", "arail": "arial", "verdena": "verdana", "helvetica neue": "helvetica",
}


def font_face(name: Any) -> Optional[FontFace]:
    """The allowlisted face for a family name (case/space-insensitive,
    common aliases and typos), or None."""
    if not isinstance(name, str) or len(name) > 60:
        return None
    key = _fold_words(name)
    key = _FONT_ALIASES.get(key, key)
    return FONT_ALLOWLIST.get(key)


def _font_value(v: Any) -> Optional[str]:
    if v is None:
        return None
    face = font_face(v)
    if face is None:
        raise ValueError("the font is not on the allowlist")
    return face.name


# ================================================================= model ==


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Align = Literal["left", "center", "right", "justify"]


class TextStyle(_M):
    """Properties one element may carry. Every field optional: None means
    'not asked for' and the resolver's default applies."""

    font_family: Optional[str] = None
    size_pt: Optional[float] = Field(default=None, ge=6, le=72)
    bold: Optional[bool] = None
    italic: Optional[bool] = None
    underline: Optional[bool] = None
    color: Optional[str] = None
    background: Optional[str] = None
    align: Optional[Align] = None

    @field_validator("font_family", mode="before")
    @classmethod
    def _font(cls, v: Any) -> Optional[str]:
        return _font_value(v)

    @field_validator("color", "background", mode="before")
    @classmethod
    def _colour(cls, v: Any) -> Optional[str]:
        return _color_value(v)

    @field_validator("size_pt")
    @classmethod
    def _finite(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not math.isfinite(v):
            raise ValueError("size must be finite")
        return None if v is None else round(float(v), 1)

    def is_empty(self) -> bool:
        return all(getattr(self, k) is None for k in TextStyle.model_fields)

    def over(self, base: "TextStyle") -> "TextStyle":
        """`base` with this style's set fields on top."""
        data = base.model_dump()
        data.update({k: v for k, v in self.model_dump().items() if v is not None})
        return TextStyle.model_construct(**data)

    def set_fields(self) -> Dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


TargetKind = Literal[
    "title", "subtitle", "heading", "paragraph", "bullet", "table_header", "table_body", "table_total", "table",
    "column", "row", "cell_range", "kpi_value", "kpi_label", "callout", "caption", "slide_title", "slide_body",
    "header_footer", "chart_title", "chart_axis", "chart_legend", "chart_labels",
]
TARGET_KINDS: Tuple[str, ...] = TargetKind.__args__  # type: ignore[attr-defined]

_A1_RE = re.compile(r"^([A-Z]{1,3})([0-9]{1,7})(?::([A-Z]{1,3})([0-9]{1,7}))?$")
_SHEET_NAME_RE = re.compile(r"^[^\[\]\*\?/\\:]{1,31}$")

#: Which locator fields each target kind may carry.
_LOCATORS: Dict[str, Tuple[str, ...]] = {
    "heading": ("level", "text", "index"),
    "paragraph": ("section", "first"),
    "table": ("index",), "table_header": ("index", "sheet"), "table_body": ("index", "sheet"), "table_total": ("index", "sheet"),
    "column": ("sheet", "name", "index"),
    "row": ("sheet", "index"),
    "cell_range": ("sheet", "a1"),
    "slide_title": ("index",),
    "chart_title": ("index",), "chart_axis": ("index",), "chart_legend": ("index",), "chart_labels": ("index",),
}


class StyleTarget(_M):
    """An element a rule applies to. `index` is 1-based: the Nth heading
    (of `level` when given), the Nth table, the Nth slide, the Nth chart, a
    column's position, or — for a `row` — the SPREADSHEET row number in a
    sheet (row 1 is the header) and the data row number in a document
    table. There is no free paragraph index: a paragraph target is the
    first paragraph, or the paragraphs of one section."""

    kind: TargetKind
    level: Optional[int] = Field(default=None, ge=1, le=3)
    text: Optional[str] = Field(default=None, max_length=200)
    index: Optional[int] = Field(default=None, ge=1, le=1_048_576)
    section: Optional[str] = Field(default=None, max_length=200)
    first: Optional[bool] = None
    sheet: Optional[str] = Field(default=None, max_length=31)
    name: Optional[str] = Field(default=None, max_length=80)
    a1: Optional[str] = Field(default=None, max_length=24)

    @field_validator("a1", mode="before")
    @classmethod
    def _a1(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        text = re.sub(r"\s+", "", str(v)).upper().replace("$", "")
        m = _A1_RE.match(text)
        if not m:
            raise ValueError("a cell range is A1 or A1:D10")
        return text

    @field_validator("sheet")
    @classmethod
    def _sheet(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _SHEET_NAME_RE.match(v):
            raise ValueError("not a sheet name")
        return v

    @model_validator(mode="after")
    def _locators_fit_kind(self) -> "StyleTarget":
        allowed = set(_LOCATORS.get(self.kind, ()))
        for f in ("level", "text", "index", "section", "first", "sheet", "name", "a1"):
            if getattr(self, f) is not None and f not in allowed:
                raise ValueError(f"a {self.kind} target has no {f}")
        if self.kind == "column" and not (self.name or self.index):
            raise ValueError("a column target names the column")
        if self.kind == "row" and self.index is None:
            raise ValueError("a row target gives the row number")
        if self.kind == "cell_range" and not self.a1:
            raise ValueError("a cell range target gives the range")
        return self

    def key(self) -> str:
        """A canonical identity: two rules with the same key replace."""
        bits = [self.kind] + [f"{f}={getattr(self, f)!s}" for f in ("level", "text", "index", "section", "first", "sheet", "name", "a1") if getattr(self, f) is not None]
        return "|".join(b.casefold() if isinstance(b, str) else str(b) for b in bits)

    @property
    def has_locator(self) -> bool:
        return any(getattr(self, f) is not None for f in ("level", "text", "index", "section", "first", "sheet", "name", "a1"))


class StyleRule(_M):
    target: StyleTarget
    style: TextStyle


CondOp = Literal["eq", "ne", "in", "contains", "gt", "gte", "lt", "lte", "between", "blank", "date_past"]


def _cond_scalar(v: Any) -> Union[str, float, None]:
    if v is None or isinstance(v, bool):
        return None if v is None else str(v)
    if isinstance(v, (int, float)):
        if not math.isfinite(float(v)):
            raise ValueError("a condition number must be finite")
        return float(v)
    s = str(v).strip()
    if len(s) > 100:
        raise ValueError("a condition value is at most 100 characters")
    return s


class CondRule(_M):
    """Colour cells (or whole rows) whose `column` value meets a condition.
    Values are data, never markup: strings are at most 100 characters and
    reach a spreadsheet only as an escaped formula literal."""

    sheet: Optional[str] = Field(default=None, max_length=31)
    column: str = Field(min_length=1, max_length=80)
    op: CondOp = "eq"
    value: Optional[Union[float, str]] = None
    value2: Optional[Union[float, str]] = None
    values: List[Union[float, str]] = Field(default_factory=list, max_length=20)
    style: TextStyle
    whole_row: bool = False

    @field_validator("value", "value2", mode="before")
    @classmethod
    def _scalar(cls, v: Any) -> Any:
        return _cond_scalar(v)

    @field_validator("values", mode="before")
    @classmethod
    def _list(cls, v: Any) -> Any:
        return [_cond_scalar(x) for x in (v or []) if x is not None]

    @model_validator(mode="after")
    def _needs(self) -> "CondRule":
        if self.op in ("eq", "ne", "contains", "gt", "gte", "lt", "lte") and self.value is None:
            raise ValueError(f"{self.op} needs a value")
        if self.op in ("gt", "gte", "lt", "lte") and not isinstance(self.value, float):
            raise ValueError(f"{self.op} compares numbers")
        if self.op == "between" and not (isinstance(self.value, float) and isinstance(self.value2, float)):
            raise ValueError("between needs two numbers")
        if self.op == "in" and not self.values:
            raise ValueError("in needs values")
        if self.style.is_empty():
            raise ValueError("a conditional rule needs a style")
        return self

    def key(self) -> str:
        return "|".join(str(x).casefold() for x in (self.sheet, self.column, self.op, self.value, self.value2, tuple(self.values), self.whole_row))


class ColorScale(_M):
    sheet: Optional[str] = Field(default=None, max_length=31)
    column: str = Field(min_length=1, max_length=80)
    min_color: str = "#F8696B"
    mid_color: Optional[str] = "#FFEB84"
    max_color: str = "#63BE7B"

    @field_validator("min_color", "mid_color", "max_color", mode="before")
    @classmethod
    def _colour(cls, v: Any) -> Optional[str]:
        return _color_value(v)


PageSize = Literal["A4", "Letter", "Legal", "A3", "A5"]
Margins = Literal["normal", "narrow", "wide"]


class PageStyle(_M):
    size: Optional[PageSize] = None
    orientation: Optional[Literal["portrait", "landscape"]] = None
    margins: Optional[Margins] = None
    slide_ratio: Optional[Literal["16:9", "4:3"]] = None
    #: A slide background (PPTX/deck preview). On a DOCX/PDF it is refused
    #: with a warning: Word does not print page colour by default and a
    #: full-page fill is ink-heavy in a PDF.
    background: Optional[str] = None

    @field_validator("background", mode="before")
    @classmethod
    def _colour(cls, v: Any) -> Optional[str]:
        return _color_value(v)


class FontSpec(_M):
    body: Optional[str] = None
    heading: Optional[str] = None

    @field_validator("body", "heading", mode="before")
    @classmethod
    def _font(cls, v: Any) -> Optional[str]:
        return _font_value(v)


class ColorTokens(_M):
    """Token overrides: the colours the preset would otherwise choose."""

    primary: Optional[str] = None
    accent: Optional[str] = None
    ink: Optional[str] = None
    header_fill: Optional[str] = None
    header_text: Optional[str] = None
    band: Optional[str] = None
    total_fill: Optional[str] = None

    @field_validator("*", mode="before")
    @classmethod
    def _colour(cls, v: Any) -> Optional[str]:
        return _color_value(v)


_HF_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class HeaderFooter(_M):
    page_numbers: Optional[bool] = None
    header_text: Optional[str] = Field(default=None, max_length=120)
    footer_text: Optional[str] = Field(default=None, max_length=120)

    @field_validator("header_text", "footer_text", mode="before")
    @classmethod
    def _plain(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        text = _HF_CONTROL_RE.sub(" ", str(v)).strip()
        return text[:120] or None


Preset = Literal["classic", "modern", "minimal", "boardroom", "teal"]


class StyleSpec(_M):
    """The persisted style of one artifact (spec.style)."""

    preset: Preset = "classic"
    page: PageStyle = Field(default_factory=PageStyle)
    fonts: FontSpec = Field(default_factory=FontSpec)
    base_size_pt: Optional[float] = Field(default=None, ge=8, le=16)
    colors: ColorTokens = Field(default_factory=ColorTokens)
    header_footer: HeaderFooter = Field(default_factory=HeaderFooter)
    rules: List[StyleRule] = Field(default_factory=list, max_length=50)
    conditional: List[CondRule] = Field(default_factory=list, max_length=30)
    scales: List[ColorScale] = Field(default_factory=list, max_length=10)
    banded: bool = True
    freeze_header: bool = True
    auto_status_colors: bool = True
    auto_score_scale: bool = True


class ClearRule(_M):
    """Take properties OFF an element ("remove the yellow from row 5",
    "headings no colour"): every rule on `target` (all rules of that kind
    when the target has no locator) loses `props`; a rule left empty is
    dropped. For a column target, conditional colours on that column are
    dropped too when a colour property is cleared."""

    target: StyleTarget
    props: List[Literal["font_family", "size_pt", "bold", "italic", "underline", "color", "background", "align"]] = Field(min_length=1, max_length=8)


class StylePatch(_M):
    """A change to a StyleSpec: the same shape, every field optional, plus
    `clear` (properties to take off elements) and `reset` (start again from
    the house style: "remove all formatting")."""

    reset: Optional[bool] = None
    clear: List[ClearRule] = Field(default_factory=list, max_length=20)

    preset: Optional[Preset] = None
    page: Optional[PageStyle] = None
    fonts: Optional[FontSpec] = None
    base_size_pt: Optional[float] = Field(default=None, ge=8, le=16)
    colors: Optional[ColorTokens] = None
    header_footer: Optional[HeaderFooter] = None
    rules: List[StyleRule] = Field(default_factory=list, max_length=50)
    conditional: List[CondRule] = Field(default_factory=list, max_length=30)
    scales: List[ColorScale] = Field(default_factory=list, max_length=10)
    banded: Optional[bool] = None
    freeze_header: Optional[bool] = None
    auto_status_colors: Optional[bool] = None
    auto_score_scale: Optional[bool] = None

    def is_empty(self) -> bool:
        return self == StylePatch()


def _merge_model(base: Optional[BaseModel], patch: Optional[BaseModel], cls):
    if patch is None:
        return base if base is not None else cls()
    data = (base or cls()).model_dump()
    data.update({k: v for k, v in patch.model_dump().items() if v is not None})
    return cls.model_validate(data)


def _apply_clears(style: StyleSpec, clears: Sequence[ClearRule]) -> StyleSpec:
    rules = list(style.rules)
    conds = list(style.conditional)
    scales = list(style.scales)
    for c in clears:
        t = c.target
        kept: List[StyleRule] = []
        for r in rules:
            same = r.target.key() == t.key() or (not t.has_locator and r.target.kind == t.kind)
            if not same:
                kept.append(r)
                continue
            data = r.style.model_dump()
            for prop in c.props:
                data[prop] = None
            new = TextStyle.model_validate(data)
            if not new.is_empty():
                kept.append(StyleRule(target=r.target, style=new))
        rules = kept
        if t.kind == "column" and t.name and ({"color", "background"} & set(c.props)):
            conds = [x for x in conds if not (_fold(x.column) == _fold(t.name) and (x.style.color or x.style.background))]
            if "background" in c.props:
                scales = [x for x in scales if _fold(x.column) != _fold(t.name)]
    return style.model_copy(update={"rules": rules, "conditional": conds, "scales": scales})


def merge(base: Optional[StyleSpec], patch: Optional[StylePatch]) -> StyleSpec:
    """`base` with `patch` applied. Scalars override when set; nested
    groups merge per field; a rule whose target is the same as an existing
    rule's MERGES into it property by property (a later "headings bold"
    keeps an earlier "headings dark blue"); conditional rules and scales
    with the same key replace. The lists keep their caps by dropping the
    oldest entries."""
    base = base or StyleSpec()
    if patch is None:
        return base
    if patch.reset:
        base = StyleSpec()
    out = base.model_copy(deep=True)
    if patch.clear:
        out = _apply_clears(out, patch.clear)
    for name in ("preset", "base_size_pt", "banded", "freeze_header", "auto_status_colors", "auto_score_scale"):
        value = getattr(patch, name)
        if value is not None:
            setattr(out, name, value)
    out.page = _merge_model(base.page, patch.page, PageStyle)
    out.fonts = _merge_model(base.fonts, patch.fonts, FontSpec)
    out.colors = _merge_model(base.colors, patch.colors, ColorTokens)
    out.header_footer = _merge_model(base.header_footer, patch.header_footer, HeaderFooter)
    rules: Dict[str, StyleRule] = {r.target.key(): r for r in out.rules}
    for r in patch.rules:
        k = r.target.key()
        if k in rules:
            rules[k] = StyleRule(target=r.target, style=r.style.over(rules[k].style))
            rules[k] = StyleRule.model_validate(rules[k].model_dump())
            # Re-inserted at the end: the newest rule wins a tie in resolve.
            rules[k] = rules.pop(k)
        else:
            rules[k] = r
    out.rules = list(rules.values())[-50:]
    conds: Dict[str, CondRule] = {c.key(): c for c in out.conditional}
    for c in patch.conditional:
        conds.pop(c.key(), None)
        conds[c.key()] = c
    out.conditional = list(conds.values())[-30:]
    scales: Dict[str, ColorScale] = {f"{s.sheet}|{s.column}".casefold(): s for s in out.scales}
    for s in patch.scales:
        scales[f"{s.sheet}|{s.column}".casefold()] = s
    out.scales = list(scales.values())[-10:]
    return StyleSpec.model_validate(out.model_dump())


# ================================================================ presets ==


@dataclass(frozen=True)
class Tokens:
    """The colours one preset resolves to (style guide §1)."""

    primary: str
    accent: str          # rules, bars, KPI top bar
    accent_text: str     # H2 and links: a text-safe accent
    ink: str = INK
    muted: str = "#5F6B7A"
    caption: str = "#6B7280"
    hairline: str = "#D0D7E2"
    grid: str = "#E5E9F0"
    band: str = "#F3F6FA"
    total_fill: str = "#DCE6F2"
    total_text: str = ""
    header_fill: str = ""
    header_text: str = WHITE
    title: str = ""
    h1: str = ""
    h2: str = ""
    h3: str = INK
    kpi_bar: str = ""
    cover_band: str = ""
    databar: str = "#5B9BD5"
    heading_font: str = "Calibri"
    body_font: str = "Calibri"


def _tokens(primary: str, accent: str, accent_text: str, **over: str) -> Tokens:
    base = dict(primary=primary, accent=accent, accent_text=accent_text, total_text=primary, header_fill=primary,
                title=primary, h1=primary, h2=accent_text, kpi_bar=accent, cover_band=primary)
    base.update(over)
    return Tokens(**base)


PRESETS: Dict[str, Tokens] = {
    "classic": _tokens("#1F3864", "#2E5597", "#2E5597"),
    "modern": _tokens("#0F4C81", "#0E9D9A", "#0B7A77", heading_font="Segoe UI", body_font="Segoe UI"),
    "minimal": _tokens("#111827", "#4B5563", "#4B5563", header_fill="#F3F4F6", header_text=INK, kpi_bar="#4B5563"),
    "boardroom": _tokens("#0A1D37", "#B7791F", "#8A5A00", heading_font="Cambria"),
    "teal": _tokens("#0B5563", "#0E9D9A", "#0B7A77"),
}

#: status class → (fill, text), per VALUE (style guide §4).
STATUS_PAIRS: Dict[str, Tuple[str, str]] = {
    "success": ("#E3F2E6", "#1E6B34"),
    "warning": ("#FFF1C7", "#7A4F00"),
    "danger": ("#FDE4E4", "#9B1C1C"),
    "info": ("#E3EDF8", "#1F4E79"),
    "neutral": ("#EEF0F3", "#374151"),
}
STATUS_VALUES: Dict[str, Tuple[str, ...]] = {
    "success": ("done", "pass", "passed", "complete", "completed", "closed", "resolved", "yes", "low", "approved", "ok", "on track"),
    "warning": ("in progress", "partial", "pending", "on hold", "medium", "at risk", "review", "in review", "waiting"),
    "danger": ("fail", "failed", "overdue", "blocked", "rejected", "high", "critical", "no", "off track", "delayed"),
    "info": ("not started", "n/a", "na", "open", "new", "planned", "todo", "to do"),
}
STATUS_COLUMN_RE = re.compile(r"\b(status|state|result|stage|health|priority|risk|outcome|rag)\b", re.IGNORECASE)
SCORE_COLUMN_RE = re.compile(r"\b(score|rating|percent|percentage|pct|completion|progress|%)", re.IGNORECASE)
DUE_COLUMN_RE = re.compile(r"\b(due|deadline|target date|due date|eta)\b", re.IGNORECASE)
SCORE_SCALE = ("#F8696B", "#FFEB84", "#63BE7B")

#: Series colours for charts, reordered for colour-vision deficiency (the
#: first five are >= 25 CIELAB apart under simulated deuteranopia).
CHART_PALETTE: Tuple[str, ...] = ("#2F6FB2", "#E07B00", "#0E9D9A", "#C0566B", "#6D5AE6", "#8A5A44", "#3F8F4F", "#5F6B7A")

PAGE_SIZES_MM: Dict[str, Tuple[float, float]] = {"A4": (210, 297), "Letter": (215.9, 279.4), "Legal": (215.9, 355.6), "A3": (297, 420), "A5": (148, 210)}
MARGINS_MM: Dict[str, Optional[Tuple[float, float, float, float]]] = {
    "normal": (22, 20, 20, 20),  # top, bottom, left, right
    "narrow": (12.7, 12.7, 12.7, 12.7),
    "wide": (25.4, 25.4, 25.4, 25.4),
}

# ============================================================== resolved ==


@dataclass(frozen=True)
class ChartStyleDefaults:
    palette: Tuple[str, ...]
    font_family: str
    font_stack: str
    title_size_pt: float
    axis_size_pt: float
    grid_color: str
    axis_text_color: str
    title_color: str

    def label_color_for(self, fill_hex: str) -> str:
        """White or ink, whichever reaches the better contrast on the fill."""
        return readable_on(fill_hex)


@dataclass
class ResolvedPage:
    size: str
    orientation: str
    width_mm: float
    height_mm: float
    margins_mm: Optional[Tuple[float, float, float, float]]  # None: the template's grid
    page_numbers: bool
    header_text: str
    footer_text: str
    slide_ratio: str
    background: Optional[str]
    orientation_explicit: bool
    margins: str = "normal"


@dataclass(frozen=True)
class TypeSizes:
    title: float = 28
    subtitle: float = 14
    h1: float = 18
    h2: float = 14
    h3: float = 12
    body: float = 11
    table: float = 10
    caption: float = 9
    kpi_value: float = 22
    kpi_label: float = 9
    slide_title: float = 32
    slide_body: float = 18
    chart_title: float = 12
    chart_axis: float = 10


@dataclass
class ResolvedStyle:
    preset: str
    tokens: Tokens
    body_face: FontFace
    heading_face: FontFace
    sizes: TypeSizes
    page: ResolvedPage
    rules: List[StyleRule]
    conditional: List[CondRule]
    scales: List[ColorScale]
    banded: bool
    freeze_header: bool
    auto_status_colors: bool
    auto_score_scale: bool
    explicit: bool
    #: The palette differs from the house default (a preset other than
    #: classic, or a primary colour): template band colours give way to it.
    custom_palette: bool = False
    warnings: List[str] = field(default_factory=list)
    _warned: set = field(default_factory=set)

    # --- fonts --------------------------------------------------------
    @property
    def fonts(self) -> Dict[str, FontFace]:
        return {"body": self.body_face, "heading": self.heading_face}

    def face(self, family: Optional[str]) -> FontFace:
        return font_face(family) or self.body_face

    # --- elements -----------------------------------------------------
    def base(self, kind: str, *, level: Optional[int] = None) -> TextStyle:
        """The preset's style for an element kind before any rule."""
        t, s = self.tokens, self.sizes
        body, head = self.body_face.name, self.heading_face.name
        table = {
            "title": dict(font_family=head, size_pt=s.title, bold=True, color=t.title),
            "subtitle": dict(font_family=body, size_pt=s.subtitle, color=t.muted),
            "paragraph": dict(font_family=body, size_pt=s.body, color=t.ink),
            "bullet": dict(font_family=body, size_pt=s.body, color=t.ink),
            "callout": dict(font_family=body, size_pt=s.body, color=t.ink),
            "table": dict(font_family=body, size_pt=s.table, color=t.ink),
            "table_header": dict(font_family=body, size_pt=s.table, bold=True, color=t.header_text, background=t.header_fill),
            "table_body": dict(font_family=body, size_pt=s.table, color=t.ink),
            "table_total": dict(font_family=body, size_pt=s.table, bold=True, color=t.total_text, background=t.total_fill),
            "column": dict(font_family=body, size_pt=s.table, color=t.ink),
            "row": dict(font_family=body, size_pt=s.table, color=t.ink),
            "cell_range": dict(font_family=body, size_pt=s.table, color=t.ink),
            "kpi_value": dict(font_family=head, size_pt=s.kpi_value, bold=True, color=t.primary),
            "kpi_label": dict(font_family=body, size_pt=s.kpi_label, color=t.muted),
            "caption": dict(font_family=body, size_pt=s.caption, italic=True, color=t.caption),
            "slide_title": dict(font_family=head, size_pt=s.slide_title, bold=True, color=t.primary),
            "slide_body": dict(font_family=body, size_pt=s.slide_body, color=t.ink),
            "header_footer": dict(font_family=body, size_pt=9, color=t.caption),
            "chart_title": dict(font_family=body, size_pt=s.chart_title, bold=True, color=t.ink),
            "chart_axis": dict(font_family=body, size_pt=s.chart_axis, color=t.muted),
            "chart_legend": dict(font_family=body, size_pt=s.chart_axis, color=t.muted),
            "chart_labels": dict(font_family=body, size_pt=9, color=t.ink),
        }
        if kind == "heading":
            lvl = level or 1
            data = dict(font_family=head, bold=True, size_pt={1: s.h1, 2: s.h2, 3: s.h3}[min(max(lvl, 1), 3)],
                        color={1: t.h1, 2: t.h2, 3: t.h3}[min(max(lvl, 1), 3)])
        else:
            data = table.get(kind, dict(font_family=body, size_pt=s.body, color=t.ink))
        full = {k: None for k in TextStyle.model_fields}
        full.update(bold=False, italic=False, underline=False)
        full.update(data)
        return TextStyle.model_construct(**full)

    def matching_rules(self, kind: str, **loc: Any) -> List[StyleRule]:
        return [r for r in self.rules if _target_matches(r.target, kind, loc)]

    def element(self, kind: str, *, generic_only: bool = False, **loc: Any) -> TextStyle:
        """The resolved style of one element. `loc` describes the element
        (level, text, index, section, first, sheet, column, column_index,
        row, table). With `generic_only`, rules that name a specific
        instance (a heading text, a column, a row) are skipped: that is
        the style a Word named style carries."""
        base = self.base(kind, level=loc.get("level"))
        explicit_color = False
        explicit_bg = False
        style = base
        for r in self.matching_rules(kind, **loc):
            if generic_only and _is_specific(r.target, kind):
                continue
            s = r.style
            if s.color is not None:
                explicit_color = True
            if s.background is not None:
                explicit_bg = True
            style = s.over(style)
        if style.background and not explicit_color:
            # (a) the system chose the text colour: it must read on the fill.
            if contrast_ratio(style.color or INK, style.background) < 4.5:
                style = TextStyle.model_construct(**{**style.model_dump(), "color": readable_on(style.background)})
        elif explicit_color or explicit_bg:
            ground = style.background or WHITE
            ratio = contrast_ratio(style.color or INK, ground)
            if ratio < 3.0:
                self._warn_pair(style.color or INK, ground)
        return style

    def _warn_pair(self, fg: str, bg: str) -> None:
        key = (fg, bg)
        if key in self._warned:
            return
        self._warned.add(key)
        self.warnings.append(f"{colour_name(fg).capitalize()} text on {colour_name(bg)} is hard to read (contrast {contrast_ratio(fg, bg):.1f}:1); it was kept as asked.")

    def has_specific_rules(self, kind: str) -> bool:
        return any(r.target.kind == kind and r.target.has_locator for r in self.rules)

    # --- charts -------------------------------------------------------
    @property
    def chart_defaults(self) -> ChartStyleDefaults:
        title = self.element("chart_title")
        axis = self.element("chart_axis")
        face = self.face(title.font_family)
        return ChartStyleDefaults(
            palette=CHART_PALETTE, font_family=face.name, font_stack=face.css_stack,
            title_size_pt=float(title.size_pt or 12), axis_size_pt=float(axis.size_pt or 10),
            grid_color=self.tokens.grid, axis_text_color=axis.color or self.tokens.muted, title_color=title.color or INK,
        )

    # --- page ---------------------------------------------------------
    def orientation_for(self, default: str) -> str:
        return self.page.orientation if self.page.orientation_explicit else default


def _norm_text(text: Any) -> str:
    t = unicodedata.normalize("NFC", str(text or "")).casefold()
    t = re.sub(r"^\s*(?:\d+(?:\.\d+)*\.?|[ivxlc]+\.)\s+", "", t)
    return " ".join(re.sub(r"[^\w\sऀ-૿]", " ", t).split())


def text_matches(wanted: str, actual: str) -> bool:
    """A heading/section named in a request matches the heading in the
    document: equal after normalising case, numbering and punctuation, or
    the request's words all present in order (a prefix-like 'Recommend'
    for 'Recommendations' also matches)."""
    w, a = _norm_text(wanted), _norm_text(actual)
    if not w or not a:
        return False
    if w == a:
        return True
    if len(w) >= 4 and (a.startswith(w) or f" {w}" in f" {a}"):
        return True
    return False


_GENERIC_FOR: Dict[str, Tuple[str, ...]] = {
    # element kind → rule kinds that also apply to it
    "table_header": ("table",),
    "table_body": ("table",),
    "table_total": ("table",),
    "bullet": (),
}


def _is_specific(target: StyleTarget, kind: str) -> bool:
    if target.kind != kind:
        return True  # a column/row/table rule on a cell
    return target.has_locator and not (target.kind == "heading" and target.level is not None and target.text is None and target.index is None)


def _target_matches(t: StyleTarget, kind: str, loc: Dict[str, Any]) -> bool:
    sheet = loc.get("sheet")
    if t.sheet is not None and sheet is not None and t.sheet.casefold() != str(sheet).casefold():
        return False
    if t.kind == kind:
        if t.level is not None and loc.get("level") is not None and t.level != loc.get("level"):
            return False
        if t.level is not None and loc.get("level") is None and kind == "heading":
            return False
        if t.text is not None and not text_matches(t.text, loc.get("text") or ""):
            return False
        if t.index is not None:
            if kind in ("table_header", "table_body", "table_total"):
                if loc.get("table") is not None and t.index != loc.get("table"):
                    return False
            elif kind == "heading":
                ordinal = loc.get("level_index") if t.level is not None else loc.get("index")
                if ordinal != t.index:
                    return False
            elif kind == "row":
                return False
            elif loc.get("index") != t.index:
                return False
        if t.section is not None and not any(text_matches(t.section, s or "") for s in (loc.get("sections") or [loc.get("section") or ""])):
            return False
        if t.first and not loc.get("first"):
            return False
        return True
    if kind in _GENERIC_FOR and t.kind in _GENERIC_FOR[kind]:
        return t.index is None or loc.get("table") is None or t.index == loc.get("table")
    if kind in ("table_body", "table_total") and t.kind == "column":
        if t.name is not None:
            return bool(loc.get("column")) and _fold(t.name) == _fold(loc.get("column"))
        return t.index is not None and loc.get("column_index") is not None and t.index == int(loc["column_index"]) + 1
    if kind == "table_body" and t.kind == "row":
        return loc.get("row") is not None and t.index == loc.get("row")
    if kind == "table_body" and t.kind == "cell_range":
        return _in_a1(t.a1 or "", loc.get("row"), loc.get("column_index"))
    return False


def _fold(name: Any) -> str:
    return " ".join(str(name or "").split()).casefold()


def col_index(letters: str) -> int:
    """'A' → 1, 'AA' → 27."""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def parse_a1(a1: str) -> Tuple[int, int, int, int]:
    """(min_col, min_row, max_col, max_row), 1-based, ordered."""
    m = _A1_RE.match(a1)
    if not m:
        raise ValueError("bad range")
    c1, r1 = col_index(m.group(1)), int(m.group(2))
    c2, r2 = (col_index(m.group(3)), int(m.group(4))) if m.group(3) else (c1, r1)
    return min(c1, c2), min(r1, r2), max(c1, c2), max(r1, r2)


def clip_a1(a1: str, max_col: int, max_row: int) -> Optional[Tuple[int, int, int, int]]:
    """A range clipped to the used area (1..max_col, 1..max_row), or None
    when nothing of it is inside. This is the DoS guard: A1:XFD1048576 on
    a 50-row sheet becomes A1:<last col>51 before a single cell is touched."""
    c1, r1, c2, r2 = parse_a1(a1)
    c2, r2 = min(c2, max_col), min(r2, max_row)
    if c1 > c2 or r1 > r2:
        return None
    return c1, r1, c2, r2


def _in_a1(a1: str, row: Optional[int], column_index: Optional[int]) -> bool:
    if row is None or column_index is None or not a1:
        return False
    c1, r1, c2, r2 = parse_a1(a1)
    c = int(column_index) + 1
    return r1 <= row <= r2 and c1 <= c <= c2


def resolve(spec: Any, *, type_scale: Any = None) -> ResolvedStyle:
    """The ResolvedStyle for an ArtifactSpec (or a body, or None for the
    house default). `type_scale` (a theme.TypeScale) supplies the template's
    density — a brief is tighter than a report — and the guide's sizes
    apply on top of it."""
    body = getattr(spec, "body", spec)
    style: Optional[StyleSpec] = getattr(body, "style", None) if body is not None else None
    explicit = style is not None
    style = style or StyleSpec()
    tokens = PRESETS.get(style.preset, PRESETS["classic"])
    c = style.colors
    over: Dict[str, str] = {}
    if c.primary:
        over.update(primary=c.primary, title=c.primary, h1=c.primary, header_fill=c.primary, total_text=c.primary, cover_band=c.primary)
    if c.accent:
        over.update(accent=c.accent, kpi_bar=c.accent, accent_text=c.accent if contrast_ratio(c.accent, WHITE) >= 4.5 else tokens.accent_text,
                    h2=c.accent if contrast_ratio(c.accent, WHITE) >= 4.5 else tokens.h2)
    if c.ink:
        over.update(ink=c.ink, h3=c.ink)
    if c.header_fill:
        over["header_fill"] = c.header_fill
    if c.band:
        over["band"] = c.band
    if c.total_fill:
        over["total_fill"] = c.total_fill
    if over:
        tokens = Tokens(**{**tokens.__dict__, **over})
    header_text = c.header_text or (tokens.header_text if contrast_ratio(tokens.header_text, tokens.header_fill) >= 4.5 else readable_on(tokens.header_fill))
    total_text = tokens.total_text if contrast_ratio(tokens.total_text, tokens.total_fill) >= 4.5 else readable_on(tokens.total_fill)
    tokens = Tokens(**{**tokens.__dict__, "header_text": header_text, "total_text": total_text})

    body_face = font_face(style.fonts.body) or font_face(tokens.body_font) or FONT_ALLOWLIST["calibri"]
    heading_face = font_face(style.fonts.heading) or (font_face(style.fonts.body) if style.fonts.body and not style.fonts.heading and tokens.heading_font == tokens.body_font else None) or font_face(tokens.heading_font) or body_face

    sizes = TypeSizes()
    if type_scale is not None:
        sizes = TypeSizes(
            title=min(sizes.title, float(getattr(type_scale, "title", sizes.title))),
            subtitle=min(sizes.subtitle, float(getattr(type_scale, "subtitle", sizes.subtitle))),
            h1=min(sizes.h1, float(getattr(type_scale, "h1", sizes.h1))),
            h2=min(sizes.h2, float(getattr(type_scale, "h2", sizes.h2))),
            h3=min(sizes.h3, float(getattr(type_scale, "h3", sizes.h3))),
            body=float(getattr(type_scale, "body", sizes.body)),
            table=min(sizes.table, float(getattr(type_scale, "small", sizes.table))),
            caption=float(getattr(type_scale, "caption", sizes.caption)),
            kpi_value=float(getattr(type_scale, "kpi_value", sizes.kpi_value)),
        )
    if style.base_size_pt:
        b = float(style.base_size_pt)
        sizes = TypeSizes(**{**sizes.__dict__, "body": b, "table": max(8.0, b - 1), "caption": max(8.0, b - 2)})

    p = style.page
    size = p.size or "A4"
    w, h = PAGE_SIZES_MM[size]
    orientation = p.orientation or ("portrait")
    if orientation == "landscape":
        w, h = h, w
    hf = style.header_footer
    page = ResolvedPage(
        size=size, orientation=orientation, width_mm=w, height_mm=h,
        margins_mm=MARGINS_MM.get(p.margins) if p.margins and p.margins != "normal" else (MARGINS_MM["normal"] if p.margins == "normal" else None),
        page_numbers=True if hf.page_numbers is None else bool(hf.page_numbers),
        header_text=hf.header_text or "", footer_text=hf.footer_text or "",
        slide_ratio=p.slide_ratio or "16:9", background=p.background, orientation_explicit=p.orientation is not None, margins=p.margins or "normal",
    )
    return ResolvedStyle(
        preset=style.preset, tokens=tokens, body_face=body_face, heading_face=heading_face, sizes=sizes, page=page,
        rules=list(style.rules), conditional=list(style.conditional), scales=list(style.scales), banded=style.banded,
        freeze_header=style.freeze_header, auto_status_colors=style.auto_status_colors, auto_score_scale=style.auto_score_scale,
        explicit=explicit, custom_palette=explicit and (style.preset != "classic" or c.primary is not None),
    )


# ======================================================== support matrix ==

_TEXT_PROPS = ("font_family", "size_pt", "bold", "italic", "underline", "color", "background", "align")
_ALL = _TEXT_PROPS
_NO_BG_ALIGN = ("font_family", "size_pt", "bold", "italic", "underline", "color")
_FONTISH = ("font_family", "size_pt", "bold", "italic", "color")
_PAGE_PROPS = ("background", "orientation", "size", "margins", "page_numbers", "slide_ratio")


def _declare(cells: Dict[str, Sequence[str]], page: Sequence[str]) -> Dict[Tuple[str, str], str]:
    out: Dict[Tuple[str, str], str] = {}
    for t in TARGET_KINDS:
        props = set(cells.get(t, ()))
        for p in _TEXT_PROPS:
            out[(t, p)] = "supported" if p in props else "unsupported"
    for p in _PAGE_PROPS:
        out[("page", p)] = "supported" if p in page else "unsupported"
    return out


_DOC_CELLS: Dict[str, Sequence[str]] = {
    **{t: _ALL for t in ("title", "subtitle", "heading", "paragraph", "bullet", "caption", "table_header", "table_body", "table", "column", "row")},
    "kpi_value": _NO_BG_ALIGN, "kpi_label": _NO_BG_ALIGN, "callout": _NO_BG_ALIGN + ("background",), "header_footer": _NO_BG_ALIGN,
}
_TABULAR_CELLS: Dict[str, Sequence[str]] = {
    **{t: _ALL for t in ("title", "subtitle", "caption", "table_header", "table_body", "table_total", "table", "column", "row")},
    "header_footer": _NO_BG_ALIGN,
}
_DECK_CELLS: Dict[str, Sequence[str]] = {
    "title": _NO_BG_ALIGN + ("align",), "subtitle": _NO_BG_ALIGN + ("align",), "slide_title": _ALL, "slide_body": _NO_BG_ALIGN,
    "bullet": _NO_BG_ALIGN, **{t: _ALL for t in ("table_header", "table_body", "table", "column", "row")},
    "kpi_value": _NO_BG_ALIGN + ("align",), "kpi_label": _NO_BG_ALIGN + ("align",), "caption": _ALL, "header_footer": _NO_BG_ALIGN,
}
_SHEET_CELLS: Dict[str, Sequence[str]] = {t: _ALL for t in ("table_header", "table_body", "table_total", "table", "column", "row", "cell_range")}

#: The declared support matrix, per (format, kind): (target, property) →
#: supported | unsupported. Every "supported" cell has an introspection test
#: on the PRODUCED file (tests/test_artifact_style_matrix.py); every
#: "unsupported" group a spec uses emits exactly one warning sentence.
#: Charts drawn as images (DOCX/PDF/PNG/SVG) are the charts track's: their
#: cells stay unsupported here until render/charts.py reads
#: ResolvedStyle.chart_defaults. Page-level rows: ("page", background |
#: orientation | size | margins | page_numbers | slide_ratio).
_SUPPORT: Dict[Tuple[str, str], Dict[Tuple[str, str], str]] = {
    ("docx", "document"): _declare(_DOC_CELLS, ("orientation", "size", "margins", "page_numbers")),
    ("pdf", "document"): _declare(_DOC_CELLS, ("orientation", "size", "margins", "page_numbers")),
    ("docx", "workbook"): _declare(_TABULAR_CELLS, ("orientation", "size", "margins", "page_numbers")),
    ("pdf", "workbook"): _declare(_TABULAR_CELLS, ("orientation", "size", "margins", "page_numbers")),
    ("pptx", "presentation"): _declare({**_DECK_CELLS, "chart_title": _NO_BG_ALIGN, "chart_axis": _FONTISH, "chart_legend": _FONTISH, "chart_labels": _FONTISH},
                                       ("background", "page_numbers")),
    ("pdf", "presentation"): _declare(_DECK_CELLS, ("background", "page_numbers")),
    ("xlsx", "workbook"): _declare(_SHEET_CELLS, ("orientation", "size", "margins", "page_numbers")),
    ("csv", "workbook"): _declare({}, ()),
    ("png", "document"): _declare({}, ()),
}
for _kind in ("presentation", "workbook"):
    _SUPPORT[("png", _kind)] = _SUPPORT[("png", "document")]
for _kind in ("document", "presentation", "workbook"):
    _SUPPORT[("svg", _kind)] = _SUPPORT[("png", "document")]


def _kind_of(body: Any) -> str:
    if hasattr(body, "sheets"):
        return "workbook"
    if hasattr(body, "slides"):
        return "presentation"
    return "document"


def support_matrix(fmt: str, kind: str = "document") -> Dict[Tuple[str, str], str]:
    """The declared (target, property) → 'supported' | 'unsupported' map
    for one format and artifact kind (empty for a pair this module does
    not know: a document cannot be an xlsx)."""
    return dict(_SUPPORT.get((fmt, kind), {}))


_PROP_WORDS = {"font_family": "font", "size_pt": "size", "bold": "bold", "italic": "italic", "underline": "underline",
               "color": "colour", "background": "background colour", "align": "alignment"}
_TARGET_WORDS = {"title": "the title", "subtitle": "the subtitle", "heading": "headings", "paragraph": "body text", "bullet": "bullet points",
                 "table_header": "the table header", "table_body": "table cells", "table_total": "the totals row", "table": "tables",
                 "column": "a column", "row": "a row", "cell_range": "a cell range", "kpi_value": "KPI values", "kpi_label": "KPI labels",
                 "callout": "callouts", "caption": "captions", "slide_title": "slide titles", "slide_body": "slide text",
                 "header_footer": "the header and footer", "chart_title": "the chart title", "chart_axis": "chart axes",
                 "chart_legend": "the chart legend", "chart_labels": "chart labels", "page": "the page"}
_FORMAT_WORDS = {"docx": "Word file", "pdf": "PDF", "pptx": "PowerPoint file", "xlsx": "Excel file", "csv": "CSV", "png": "PNG image", "svg": "SVG image"}


def requested_pairs(style: Optional[StyleSpec]) -> List[Tuple[str, str]]:
    """Every (target kind, property) the style asks for, in rule order."""
    if style is None:
        return []
    out: List[Tuple[str, str]] = []
    for r in style.rules:
        for prop in r.style.set_fields():
            out.append((r.target.kind, prop))
    for c in style.conditional:
        for prop in c.style.set_fields():
            out.append(("row" if c.whole_row else "column", prop))
    if style.scales:
        out.append(("column", "background"))
    if style.page.background:
        out.append(("page", "background"))
    if style.page.orientation:
        out.append(("page", "orientation"))
    if style.page.size:
        out.append(("page", "size"))
    if style.page.margins:
        out.append(("page", "margins"))
    if style.header_footer.page_numbers is not None:
        out.append(("page", "page_numbers"))
    if style.page.slide_ratio == "4:3":
        out.append(("page", "slide_ratio"))
    return list(dict.fromkeys(out))


def warnings_for(spec: Any, formats: Sequence[str]) -> List[str]:
    """One sentence per unsupported (target, property, format) group the
    spec's style actually uses. A CSV's whole styling is one sentence
    (it carries data only); every other format names the target and the
    properties it cannot carry."""
    body = getattr(spec, "body", spec)
    style: Optional[StyleSpec] = getattr(body, "style", None)
    pairs = requested_pairs(style)
    if not pairs:
        return []
    out: List[str] = []
    kind = _kind_of(body)
    for fmt in formats:
        matrix = _SUPPORT.get((fmt, kind))
        if matrix is None:
            continue
        if fmt == "csv":
            out.append("The CSV carries the data only; the formatting is in the Excel file.")
            continue
        missing: Dict[str, List[str]] = {}
        for target, prop in pairs:
            if matrix.get((target, prop), "unsupported") != "supported":
                missing.setdefault(target, []).append(prop)
        for target, props in missing.items():
            if target == "page" and props == ["background"] and fmt in ("docx", "pdf"):
                out.append(f"The {_FORMAT_WORDS[fmt]} has no page background colour (Word does not print one and it floods a PDF with ink); element backgrounds were kept.")
                continue
            words = " and ".join(dict.fromkeys(_PROP_WORDS.get(p, p) if target != "page" else p.replace("_", " ") for p in props))
            out.append(f"The {_FORMAT_WORDS[fmt]} cannot set the {words} of {_TARGET_WORDS.get(target, target)}.")
    return out


# ================================================= spreadsheet escaping ==

_FORMULA_TEXT_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def xlsx_formula_literal(value: Any) -> str:
    """A value as an Excel formula LITERAL: a number as a finite decimal, a
    string as "…" with every quote doubled, control characters removed and
    at most 100 characters. Nothing a person typed can end the literal and
    continue the formula."""
    if isinstance(value, bool):
        value = str(value)
    if isinstance(value, (int, float)):
        f = float(value)
        if not math.isfinite(f):
            raise ValueError("not a finite number")
        return format(f, ".15g")
    text = _FORMULA_TEXT_CONTROL.sub(" ", str(value))[:100]
    return '"' + text.replace('"', '""') + '"'


def xlsx_search_literal(value: Any) -> str:
    """A literal for SEARCH(): the wildcard characters ~ * ? escaped with ~
    first, then quoted as xlsx_formula_literal does."""
    text = _FORMULA_TEXT_CONTROL.sub(" ", str(value))[:100]
    text = text.replace("~", "~~").replace("*", "~*").replace("?", "~?")
    return '"' + text[:100].replace('"', '""') + '"'


def xlsx_header_footer_text(text: Any) -> str:
    """User text for an Excel header/footer section: every '&' doubled so
    `&F` (file path), `&A`, `&P` and font codes print as text."""
    clean = _FORMULA_TEXT_CONTROL.sub(" ", str(text or ""))[:120]
    return clean.replace("&", "&&")


# ================================================= normalising a spec ====


def _spec_outline(body: Any) -> Dict[str, Any]:
    """What exists in the spec, for dropping rules that target nothing."""
    from . import spec as S  # lazy: spec imports this module

    out: Dict[str, Any] = {"kind": None, "headings": [], "tables": 0, "columns": set(), "sheets": {}, "slides": 0, "charts": 0,
                           "has": set()}
    if isinstance(body, S.DocumentSpec):
        out["kind"] = "document"
        out["has"].update({"title", "paragraph", "header_footer", "caption"})
        if body.subtitle:
            out["has"].add("subtitle")
        for b in body.blocks:
            if isinstance(b, S.Heading):
                out["headings"].append((b.level, b.text))
            elif isinstance(b, S.TableBlock):
                out["tables"] += 1
                out["columns"].update(_fold(c) for c in b.table.columns)
                out["max_cols"] = max(out.get("max_cols", 0), len(b.table.columns))
            elif isinstance(b, S.ChartBlock):
                out["charts"] += 1
            elif isinstance(b, S.KPIRow):
                out["has"].update({"kpi_value", "kpi_label"})
            elif isinstance(b, S.Callout):
                out["has"].add("callout")
            elif isinstance(b, S.Bullets):
                out["has"].add("bullet")
        if out["tables"]:
            out["has"].update({"table", "table_header", "table_body", "column", "row"})
        if out["headings"]:
            out["has"].add("heading")
        if out["charts"]:
            out["has"].update({"chart_title", "chart_axis", "chart_legend", "chart_labels"})
    elif isinstance(body, S.PresentationSpec):
        out["kind"] = "presentation"
        out["slides"] = len(body.slides)
        out["has"].update({"title", "subtitle", "slide_title", "slide_body", "bullet", "header_footer", "caption"})
        for s in body.slides:
            if s.table is not None:
                out["tables"] += 1
                out["columns"].update(_fold(c) for c in s.table.columns)
            if s.chart is not None:
                out["charts"] += 1
            if s.kpis:
                out["has"].update({"kpi_value", "kpi_label"})
        if out["tables"]:
            out["has"].update({"table", "table_header", "table_body", "column", "row"})
        if out["charts"]:
            out["has"].update({"chart_title", "chart_axis", "chart_legend", "chart_labels"})
    elif isinstance(body, S.WorkbookSpec):
        out["kind"] = "workbook"
        out["has"].update({"table", "table_header", "table_body", "column", "row", "cell_range", "header_footer", "title", "subtitle", "caption"})
        for sh in body.sheets:
            out["sheets"][_fold(sh.name)] = sh
            out["columns"].update(_fold(c.name) for c in sh.columns)
            if sh.totals:
                out["has"].add("table_total")
            if sh.charts:
                out["charts"] += len(sh.charts)
                out["has"].add("chart_title")
    return out


def _status_column(body: Any) -> Optional[str]:
    for sh in getattr(body, "sheets", None) or []:
        for c in sh.columns:
            if STATUS_COLUMN_RE.search(c.name):
                return c.name
    for b in getattr(body, "blocks", None) or []:
        table = getattr(b, "table", None)
        if table is not None:
            for c in table.columns:
                if STATUS_COLUMN_RE.search(c):
                    return c
    return None


def _column_exists(outline: Dict[str, Any], name: str, sheet: Optional[str]) -> bool:
    if outline["kind"] == "workbook" and sheet is not None:
        sh = outline["sheets"].get(_fold(sheet))
        return sh is not None and any(_fold(c.name) == _fold(name) for c in sh.columns)
    return _fold(name) in outline["columns"]


def _table_views(body: Any) -> List[Tuple[Optional[str], List[str], List[List[Any]]]]:
    """(sheet name or None, column names, rows) for every table in a spec
    body — workbook sheets, document tables, slide tables."""
    out: List[Tuple[Optional[str], List[str], List[List[Any]]]] = []
    for sh in getattr(body, "sheets", None) or []:
        out.append((sh.name, [c.name for c in sh.columns], list(sh.rows or [])))
    for b in getattr(body, "blocks", None) or []:
        table = getattr(b, "table", None)
        if table is not None:
            out.append((None, list(table.columns), list(table.rows or [])))
    for sl in getattr(body, "slides", None) or []:
        table = getattr(sl, "table", None)
        if table is not None:
            out.append((None, list(table.columns), list(table.rows or [])))
    return out


def _value_matcher(values: Sequence[str], contains: bool) -> Callable[[Any], bool]:
    """A cell test for text values, folded once: whitespace-collapsed and
    case-insensitive, equal (or containing, for `contains`). Numbers and
    blanks never match a text value."""
    wanted = {_fold(v) for v in values if _fold(v)}
    if contains:
        return lambda cell: isinstance(cell, str) and any(w in _fold(cell) for w in wanted)
    return lambda cell: isinstance(cell, str) and _fold(cell) in wanted


def columns_holding_value(views: Sequence[Tuple[Optional[str], Sequence[str], Sequence[Sequence[Any]]]], values: Sequence[str], *,
                          sheet: Optional[str] = None, contains: bool = False) -> List[Tuple[str, str]]:
    """(column name, the cell text as written) for every column whose cells
    hold one of `values`, the column with the most matching cells first.
    Plain data — works for spec bodies and for the edit planner's JSON."""
    match = _value_matcher(values, contains)
    counts: Dict[str, List[Any]] = {}
    for name, columns, rows in views:
        if sheet is not None and name is not None and _fold(name) != _fold(sheet):
            continue
        for j, col in enumerate(columns):
            for row in rows:
                if j < len(row) and match(row[j]):
                    entry = counts.setdefault(_fold(col), [col, str(row[j]).strip(), 0])
                    entry[2] += 1
    ranked = sorted(counts.values(), key=lambda e: -e[2])
    return [(e[0], e[1]) for e in ranked]


def column_holds_value(views: Sequence[Tuple[Optional[str], Sequence[str], Sequence[Sequence[Any]]]], column: str, values: Sequence[str], *,
                       sheet: Optional[str] = None, contains: bool = False) -> Optional[bool]:
    """True when `column` has a cell holding one of `values`; False when it
    has cells and none do; None when there is no such column or no rows to
    look at (nothing is known, so nothing is moved)."""
    match = _value_matcher(values, contains)
    seen = False
    for name, columns, rows in views:
        if sheet is not None and name is not None and _fold(name) != _fold(sheet):
            continue
        for j, col in enumerate(columns):
            if _fold(col) != _fold(column):
                continue
            for row in rows:
                if j < len(row) and row[j] not in (None, ""):
                    seen = True
                    if match(row[j]):
                        return True
    return False if seen else None


def rebind_condition_column(views: Sequence[Tuple[Optional[str], Sequence[str], Sequence[Sequence[Any]]]], column: str, op: str,
                            value: Any, values: Sequence[Any], sheet: Optional[str]) -> Optional[Tuple[str, str]]:
    """'Critical in orange' is aimed at Status by default; when Critical is
    not in that column (or there is none) but lives in Priority, the rule
    belongs on Priority. Returns (new column, the sentence that says so),
    or None to leave the rule where it is."""
    if op not in ("eq", "in", "contains"):
        return None
    wanted = [v for v in ([value] if op in ("eq", "contains") else list(values)) if isinstance(v, str) and v.strip()]
    if not wanted:
        return None
    contains = op == "contains"
    held = column_holds_value(views, column, wanted, sheet=sheet, contains=contains)
    if held is True:
        return None
    exists = any(_fold(c) == _fold(column) for name, cols, _ in views if sheet is None or name is None or _fold(name) == _fold(sheet) for c in cols)
    if exists and held is None:
        return None
    holders = [h for h in columns_holding_value(views, wanted, sheet=sheet, contains=contains) if _fold(h[0]) != _fold(column)]
    if not holders:
        return None
    new, shown = holders[0]
    where = f"not in {column!r}" if exists else f"there is no {column!r} column"
    return new, f"{shown!r} is in the {new!r} column ({where}), so the {new!r} column is coloured."


#: Integer columns that are labels, not quantities: never summed.
_NOT_A_QUANTITY_RE = re.compile(
    r"(?:^|[\s_#-])(?:id|ids|year|years|yr|fy|no|no\.|num|number|code|zip|pin|pincode|postcode|phone|mobile|rank|serial|sl|sr|s\.?no|#|month|day|week|quarter|qtr)(?:$|[\s_#.-])",
    re.IGNORECASE)


def totalable_columns(columns: Sequence[Tuple[str, str]], rows: Sequence[Sequence[Any]]) -> List[int]:
    """0-based positions of the columns a code-written totals row may sum:
    integer, number or currency columns that are quantities (not an ID, a
    year or a phone number) and, when there are rows, hold a number."""
    out: List[int] = []
    for j, (name, ctype) in enumerate(columns):
        if ctype not in ("integer", "number", "currency") or _NOT_A_QUANTITY_RE.search(f" {name} "):
            continue
        cells = [r[j] for r in rows if j < len(r) and r[j] not in (None, "")]
        if cells and not any(isinstance(c, (int, float)) and not isinstance(c, bool) for c in cells):
            continue
        out.append(j)
    return out


def _add_code_totals(body: Any, style: "StyleSpec", notes: List[str]) -> None:
    """A totals-row style ('bold total row') on a workbook with no totals
    row: the renderer's own SUBTOTAL formulas are added over the numeric
    columns of the sheets it names — computed from the rows by the
    spreadsheet, never typed — and a sentence says so. Nothing is added
    when a named sheet already has a totals row or has no numeric column."""
    from . import spec as S  # lazy: spec imports this module

    if not isinstance(body, S.WorkbookSpec):
        return
    sheets_named = [r.target.sheet for r in style.rules if r.target.kind == "table_total"]
    if not sheets_named:
        return
    wanted = [sh for sh in body.sheets if any(n is None or _fold(n) == _fold(sh.name) for n in sheets_named)]
    if not wanted or any(sh.totals for sh in wanted):
        return
    for sh in wanted:
        cols = totalable_columns([(c.name, c.type) for c in sh.columns], sh.rows or [])
        if not cols:
            continue
        sh.totals = [S.Total(column=j, fn="sum", label="Total") for j in cols]
        names = ", ".join(sh.columns[j].name for j in cols)
        notes.append(f"Sheet {sh.name!r} had no totals row, so one was added: spreadsheet formulas that sum {names} from the rows.")


def normalize_spec_style(spec: Any, *, add_totals: bool = True) -> Tuple[Any, List[str]]:
    """spec.style made safe to render against THIS spec: sizes clamped,
    a status rule's default column bound to the sheet's real status
    column, and rules whose targets do not exist dropped — each drop is a
    note. Colours and fonts are already validated by the model. Returns
    (spec, notes); the spec is updated in place when it has a style."""
    body = getattr(spec, "body", spec)
    style: Optional[StyleSpec] = getattr(body, "style", None)
    if style is None:
        return spec, []
    notes: List[str] = []
    if add_totals:
        # Only where a spec is being MADE (a create, a style request). A
        # render draws the spec as saved, and an edit adds a totals row
        # only when asked (edits._fit_style_to_data): otherwise a totals row
        # an edit removed came back on every render and every later edit,
        # and the file had a row its spec.json did not (verifier 2026-09-15).
        _add_code_totals(body, style, notes)
    outline = _spec_outline(body)
    kept: List[StyleRule] = []
    for r in style.rules:
        t = r.target
        why = ""
        if t.kind == "column" and t.name is not None and re.fullmatch(r"[A-Za-z]{1,2}", t.name) and not _column_exists(outline, t.name, t.sheet):
            # "column B yellow": a spreadsheet letter, when no column is
            # literally named "B".
            n = col_index(t.name.upper())
            widths = [len(sh.columns) for key, sh in outline["sheets"].items() if t.sheet is None or key == _fold(t.sheet)] or [outline.get("max_cols", 0)]
            if 1 <= n <= max(widths):
                t = StyleTarget(kind="column", index=n, sheet=t.sheet)
                r = StyleRule(target=t, style=r.style)
        if t.kind == "table_total" and t.kind not in outline["has"]:
            # Said plainly, never "there is no the totals row".
            if outline["kind"] == "workbook":
                why = "the workbook has no totals row, and no quantity column (a number or an amount) to add one over"
            elif outline["tables"]:
                why = f"the tables in this {outline['kind']} have no totals row"
            else:
                why = f"there is no table in this {outline['kind'] or 'file'}"
        elif t.kind not in outline["has"] and t.kind not in ("row", "column"):
            why = f"there is no {re.sub(r'^the ', '', _TARGET_WORDS.get(t.kind, t.kind))} in this {outline['kind'] or 'file'}"
        elif t.kind == "heading" and (t.text or t.index or t.level):
            matches = [h for h in outline["headings"] if (t.level is None or h[0] == t.level) and (t.text is None or text_matches(t.text, h[1]))]
            if not matches or (t.index is not None and t.index > len(matches) and t.text is None):
                why = f"no heading matches {t.text or ('number ' + str(t.index)) if (t.text or t.index) else 'level ' + str(t.level)}"
        elif t.kind == "column" and t.name is not None and not _column_exists(outline, t.name, t.sheet):
            why = f"there is no column named {t.name!r}"
        elif t.kind in ("column", "row") and "table" not in outline["has"]:
            why = "there is no table to colour"
        elif t.sheet is not None and outline["kind"] == "workbook" and _fold(t.sheet) not in outline["sheets"]:
            why = f"there is no sheet named {t.sheet!r}"
        elif t.kind == "paragraph" and t.section is not None and not any(text_matches(t.section, h[1]) for h in outline["headings"]):
            why = f"no section is called {t.section!r}"
        elif t.kind == "slide_title" and t.index is not None and t.index > outline["slides"]:
            why = f"the deck has {outline['slides']} slides"
        if why:
            notes.append(f"Not applied: {_describe(r)} — {why}.")
        else:
            kept.append(r)
    conds: List[CondRule] = []
    views = _table_views(body)
    for c in style.conditional:
        moved = rebind_condition_column(views, c.column, c.op, c.value, c.values, c.sheet)
        if moved is not None:
            c = c.model_copy(update={"column": moved[0][:80]})
            notes.append(moved[1])
        column = c.column
        if not _column_exists(outline, column, c.sheet):
            status = _status_column(body)
            if status and _fold(column) in ("status", "*status*"):
                c = c.model_copy(update={"column": status})
            else:
                notes.append(f"Not applied: colouring by {column!r} — there is no such column.")
                continue
        conds.append(c)
    scales: List[ColorScale] = []
    for s in style.scales:
        if _column_exists(outline, s.column, s.sheet):
            scales.append(s)
        else:
            notes.append(f"Not applied: a colour scale on {s.column!r} — there is no such column.")
    new = style.model_copy(update={"rules": kept, "conditional": conds, "scales": scales})
    try:
        body.style = new
    except Exception:  # pragma: no cover - frozen bodies are not used here
        pass
    return spec, notes


def _describe(rule: StyleRule) -> str:
    props = ", ".join(f"{_PROP_WORDS.get(k, k)} {v if not isinstance(v, bool) else ('on' if v else 'off')}" for k, v in rule.style.set_fields().items())
    what = _TARGET_WORDS.get(rule.target.kind, rule.target.kind)
    if rule.target.text:
        what = f"the {rule.target.text!r} heading"
    elif rule.target.name:
        what = f"the {rule.target.name!r} column"
    return f"{what} ({props})"


# ================================================== request → patch ======

_ZW_RE = re.compile("[​‌‍⁠﻿]")
_B = r"(?<![0-9a-zऀ-૿])"
_E = r"(?![0-9a-zऀ-૿])"


def _w(pattern: str) -> str:
    return f"{_B}(?:{pattern}){_E}"


#: Typos and spelling variants folded before parsing (word level).
_TYPOS: Tuple[Tuple[str, str], ...] = (
    (r"colou?rs?|colr|colur|clr|colro|coler|kalar|kolor", "color"),
    (r"head(?:ing|in|ng|ign|dings?|ings)|hed(?:ing|dings?|ings?)|headng|heaidng|haeding", "heading"),
    (r"headings?s", "headings"),
    (r"tabel|tabl|tble|taable|teble", "table"),
    (r"backgr(?:ound|oud|nd)|bakground|backround|bg|bckground|background", "background"),
    (r"bol|bld|boldd|boled", "bold"),
    (r"itallic|italics|itlaic|itelic|italic", "italic"),
    (r"underlined|underlin|undeline", "underline"),
    (r"land\s?scape|lanscape|landscap|landscpe|landscae|landsape", "landscape"),
    (r"portait|potrait|portrate|protrait|portrait", "portrait"),
    (r"cent(?:er|re|ered|red|ered)", "center"),
    (r"fonts?|fnt|fount|phont", "font"),
    (r"titel|tital|tittle|titl", "title"),
    (r"colum|coloum|colmn|collumn|coulmn|clmn|col", "column"),
    (r"grey|gray", "grey"),
    (r"proffesional|profesional|professinal|proffessional", "professional"),
    (r"clasy|classsy|clasyy", "classy"),
    (r"pdf|pfd", "pdf"),
    (r"blu|bleu|bule", "blue"),
    (r"gren|grean|greeen", "green"),
    (r"yelow|yello|yallow", "yellow"),
    (r"whte|wite|whit|whiite", "white"),
    (r"blak|blck", "black"),
    (r"purpel|purple", "purple"),
    (r"orenge|ornage|orange", "orange"),
    (r"dakr|drak|dark", "dark"),
    (r"lite|ligth|light", "light"),
    (r"navi|navy", "navy"),
)
_TYPO_RES = [(re.compile(_w(p)), r) for p, r in _TYPOS]

_SCRIPT_WORDS: Tuple[Tuple[str, str], ...] = (
    # Hindi
    ("सबटाइटल", " subtitle "), ("मुख्य शीर्षक", " title "), ("टाइटल", " title "), ("शीर्षकों", " heading "), ("शीर्षक", " heading "), ("हेडिंग्स", " heading "),
    ("हेडिंग", " heading "), ("उपशीर्षक", " subheading "), ("टेबल हेडर", " table header "), ("तालिका हेडर", " table header "),
    ("तालिका", " table "), ("टेबल", " table "), ("हेडर", " header "), ("फुटर", " footer "), ("फ़ुटर", " footer "),
    ("बॉडी टेक्स्ट", " body text "), ("बॉडी", " body "), ("टेक्स्ट", " text "), ("पाठ", " text "), ("अक्षर", " text "),
    ("फ़ॉन्ट", " font "), ("फॉन्ट", " font "), ("साइज़", " size "), ("साइज", " size "), ("आकार", " size "),
    ("पॉइंट", " pt "), ("बोल्ड", " bold "), ("मोटा", " bold "), ("मोटे", " bold "), ("इटैलिक", " italic "), ("तिरछा", " italic "),
    ("अंडरलाइन", " underline "), ("रेखांकित", " underline "), ("बैकग्राउंड", " background "), ("पृष्ठभूमि", " background "),
    ("लैंडस्केप", " landscape "), ("पोर्ट्रेट", " portrait "), ("पेज नंबर", " page numbers "), ("पृष्ठ संख्या", " page numbers "),
    ("कॉलम", " column "), ("स्तंभ", " column "), ("पंक्ति", " row "), ("रो ", " row "), ("रंग", " color "), ("रंगीन", " colorful "),
    ("प्रोफेशनल", " professional "), ("क्लासी", " classy "), ("सुंदर", " elegant "), ("कैप्शन", " caption "), ("बुलेट", " bullets "),
    ("और", " and "), ("के साथ", " with "), ("साथ", " with "), ("में", " "), ("को", " "), ("करो", " "), ("करें", " "), ("कर दो", " "),
    ("दो", " "), ("रखो", " "), ("रखें", " "), ("बनाओ", " "), ("बना दो", " "), ("का", " "), ("की", " "), ("के", " "),
    # Gujarati
    ("સબટાઇટલ", " subtitle "), ("મુખ્ય શીર્ષક", " title "), ("ટાઇટલ", " title "), ("ટાઈટલ", " title "), ("શીર્ષકો", " heading "), ("શીર્ષક", " heading "),
    ("હેડિંગ્સ", " heading "), ("હેડિંગ", " heading "), ("ટેબલ હેડર", " table header "), ("કોષ્ટક હેડર", " table header "),
    ("કોષ્ટક", " table "), ("ટેબલ", " table "), ("હેડર", " header "), ("ફૂટર", " footer "), ("બોડી", " body "),
    ("ટેક્સ્ટ", " text "), ("લખાણ", " text "), ("અક્ષરો", " text "), ("અક્ષર", " text "), ("ફોન્ટ", " font "), ("સાઇઝ", " size "),
    ("કદ", " size "), ("પોઇન્ટ", " pt "), ("પોઈન્ટ", " pt "), ("બોલ્ડ", " bold "), ("જાડા", " bold "), ("ઇટાલિક", " italic "),
    ("અન્ડરલાઇન", " underline "), ("રેખાંકિત", " underline "), ("બેકગ્રાઉન્ડ", " background "), ("પૃષ્ઠભૂમિ", " background "),
    ("લેન્ડસ્કેપ", " landscape "), ("પોર્ટ્રેટ", " portrait "), ("પેજ નંબર", " page numbers "), ("પાના નંબર", " page numbers "),
    ("કૉલમ", " column "), ("કોલમ", " column "), ("હરોળ", " row "), ("રંગ", " color "), ("કલર", " color "), ("રંગીન", " colorful "),
    ("પ્રોફેશનલ", " professional "), ("ક્લાસી", " classy "), ("સુંદર", " elegant "), ("કેપ્શન", " caption "),
    ("અને", " and "), ("સાથે", " with "), ("માં", " "), ("ને", " "), ("કરો", " "), ("કરી દો", " "), ("રાખો", " "), ("બનાવો", " "),
    ("નો", " "), ("ની", " "), ("નું", " "), ("ના", " "),
)
_HINGLISH_FILLER = re.compile(_w(r"karo|kar do|kardo|kr do|krdo|kare|karein|kijiye|rakho|rakh do|banao|bana do|do|dena|me|mein|mai|ka|ki|ke|ko|wala|wali|wale|hona|chahiye|please|pls|plz|thoda|zara|sab|saare|sare|sari|saari|ekdum|bilkul"))
_STOP = {"the", "a", "an", "all", "every", "make", "set", "use", "in", "to", "of", "for", "my", "our", "this", "that", "it", "and", "with", "be", "is", "are", "should", "please", "change", "turn", "give", "apply", "put", "have", "want", "i", "file", "doc", "document", "sheet", "excel", "word", "report", "only", "just", "also", "font", "text", "color", "style", "on", "as", "both"}

_CLASSIC_RE = re.compile(_w(r"classy|classic|professional|elegant|standard|formal|corporate|polished|sophisticated|premium|neat|stylish"))
_MODERN_RE = re.compile(_w(r"modern|contemporary|sleek|fresh"))
_MINIMAL_RE = re.compile(_w(r"minimal|minimalist|minimalistic|clean look|clean design|clean style|simple style|simple look|simple design|understated"))
_BOARDROOM_RE = re.compile(_w(r"boardroom|executive|board room|luxury|luxurious"))
_TEAL_PRESET_RE = re.compile(_w(r"teal theme|teal preset|teal style"))

_SIZE_RE = re.compile(r"(?<![a-z\d.:])(\d{1,3}(?:\.\d)?)\s*(pt|pts|point|points|px)\b|\bsize\s*(?:of\s*|to\s*|=\s*|:\s*)?(\d{1,3}(?:\.\d)?)(?![\d.%:])|(?<![a-z\d.:])(\d{1,3}(?:\.\d)?)\s*(?:size|sized)\b|\bfont\s+(\d{1,2}(?:\.\d)?)(?![\d.%:])")
_HEX_IN_TEXT = re.compile(r"(?<![0-9a-z])#([0-9a-f]{6}|[0-9a-f]{3})(?![0-9a-z])")
_A1_IN_TEXT = re.compile(r"(?<![a-z0-9])([a-z]{1,3}\d{1,7})\s*(?::|to|-|–|se|thi)\s*([a-z]{1,3}\d{1,7})(?![a-z0-9])|\bcell\s+([a-z]{1,3}\d{1,7})\b")
_QUOTED = re.compile(r"[\"“”'‘’]([^\"“”'‘’]{1,120})[\"“”'‘’]")

_STYLE_SIGNAL = re.compile(_w(
    r"color|colorful|font|bold|italic|underline|background|fill|shade|highlight|landscape|portrait|size|pt|style|styled|theme|look|"
    r"format|formatting|classy|professional|elegant|modern|minimal|align|center|margin|margins|zebra|banded|striped|header|footer|"
    r"page numbers?|a4|letter|legal|contrast|serif|sans|typeface|pretty|beautiful|attractive|design"
) + "|#[0-9a-f]{3,6}")


def _colour_tokens() -> List[Tuple[str, str]]:
    names = sorted(COLOR_NAMES.items(), key=lambda kv: -len(kv[0]))
    return names


_COLOUR_NAME_RE = re.compile("|".join(
    f"{_B}{re.escape(n)}{_E}" for n, _ in _colour_tokens()
))


def _normalise_request(text: str) -> str:
    t = _ZW_RE.sub("", unicodedata.normalize("NFC", text or ""))
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    low = t.casefold()
    for src, dst in _SCRIPT_WORDS:
        low = low.replace(src, dst)
    for rx, repl in _TYPO_RES:
        low = rx.sub(repl, low)
    low = re.sub(r"\bh([123])\b", r"heading level \1", low)
    # "heading 2 blue", "heading-1 size 20", "level 2 headings": a level,
    # never a size ("heading 12pt" is a size: the digit must end the word).
    low = re.sub(r"\bheadings?[\s-]?([123])(?![\d.%]|\s*(?:pt|pts|points?|px)\b)", r"heading level \1", low)
    low = re.sub(r"\blevel[\s-]?([123]) headings?\b", r"heading level \1", low)
    low = re.sub(r"\bsub[\s-]?headings?\b", "heading level 2", low)
    low = re.sub(r"\b(?:main|section|top[\s-]level|chapter) headings?\b", "heading level 1", low)
    return low


def _find_colours(clause: str) -> List[Tuple[int, int, str, bool]]:
    """(start, end, hex, named) for every colour in the clause."""
    out: List[Tuple[int, int, str, bool]] = []
    for m in _HEX_IN_TEXT.finditer(clause):
        out.append((m.start(), m.end(), _canon_hex(m.group(0)) or "", False))
    taken = [(s, e) for s, e, _, _ in out]
    for m in _COLOUR_NAME_RE.finditer(clause):
        if any(s <= m.start() < e for s, e in taken):
            continue
        out.append((m.start(), m.end(), COLOR_NAMES[_fold_words(m.group(0))], True))
    rgb = re.finditer(r"rgb\s*\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)", clause)
    for m in rgb:
        vals = [min(255, int(x)) for x in m.groups()]
        out.append((m.start(), m.end(), "#" + "".join(f"{v:02X}" for v in vals), False))
    return sorted(out)


def _find_font(clause: str, original: str) -> Optional[str]:
    keys = sorted(list(FONT_ALLOWLIST) + list(_FONT_ALIASES), key=len, reverse=True)
    for k in keys:
        if k in ("sans", "serif", "mono", "sans serif", "sans-serif", "monospace") and not re.search(
                r"(?:font|typeface)(?:\s+(?:family|style|type))?\s+(?:to\s+|as\s+|=\s*|:\s*|a\s+|in\s+)?" + re.escape(k) + _E
                + "|" + _B + re.escape(k) + r"\s+(?:font|typeface)", clause):
            # "Comic Sans MS" is not a request for a sans-serif face: only
            # "font sans-serif" / "a serif font" name the generic family.
            continue
        if re.search(_B + re.escape(k) + _E, clause):
            face = font_face(k)
            if face is not None:
                return face.name
    return None


@dataclass
class _Target:
    target: Optional[StyleTarget]
    scope: str = ""  # "", "global", "body", "page", "slide_background"


_TARGET_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"slide backgrounds?|background of (?:the )?slides?", "slide_background"),
    (r"page backgrounds?|background of (?:the )?pages?|document background", "page_background"),
    (r"chart titles?|graph titles?|plot titles?", "chart_title"),
    (r"axis labels?|axes|axis|x axis|y axis", "chart_axis"),
    (r"legends?", "chart_legend"),
    (r"data labels?|bar labels?|value labels?|chart labels?", "chart_labels"),
    (r"slide titles?|slide headings?", "slide_title"),
    (r"slide (?:text|body|bullets?|content)", "slide_body"),
    (r"(?:table )?header rows?|table headers?|column headers?|column headings?|headers? of (?:the )?table|table heading|table head", "table_header"),
    (r"totals? rows?|grand totals?|totals? line|totals?", "table_total"),
    (r"table (?:body|rows|cells|text|data)|body rows|data rows|cells", "table_body"),
    (r"page headers?|running headers?|header text|header and footer|header/footer|footers?|footer text", "header_footer"),
    (r"kpi (?:values?|numbers?|figures?)|kpis?|headline numbers?|metric values?", "kpi_value"),
    (r"kpi labels?|metric labels?", "kpi_label"),
    (r"callouts?|call[\s-]outs?|note boxes?|notes? box|info boxes?|quote boxes?", "callout"),
    (r"captions?", "caption"),
    (r"subtitles?|sub[\s-]?titles?|tagline", "subtitle"),
    (r"(?:document |doc |report |main |cover |deck )?titles?", "title"),
    (r"heading level ([123])(?:s)?", "heading_level"),
    (r"headings?|section titles?|section names?", "heading"),
    (r"first paragraph|opening paragraph|intro paragraph|introduction paragraph", "first_paragraph"),
    (r"bullet points?|bullets?|list items?|lists?", "bullet"),
    (r"body text|body font|body|paragraphs?|normal text|main text|content text|text body|whole (?:document|text)|all (?:the )?text|everything", "body"),
    (r"tables?", "table"),
    (r"headers?", "table_header_ambiguous"),
)
_TARGET_RES = [(re.compile(_w(p)), k) for p, k in _TARGET_PATTERNS]

_COLUMN_RE = re.compile(r"(?:the\s+)?([a-z0-9][\w %/&().-]{0,40}?)\s+(?:column|col)\b|\bcolumn\s+(?:named\s+|called\s+)?(?:\"([^\"]{1,40})\"|([a-z0-9][\w%/&().-]{0,40}))(?=$|\s|[,.;])")
_ROW_RE = re.compile(r"\brow\s*(?:no\.?|number|#)?\s*(\d{1,7})\b|\b(\d{1,7})(?:st|nd|rd|th)\s+row\b")
_SHEET_RE = re.compile(r"\b(?:in|on|of)\s+(?:the\s+)?(?:sheet\s+\"?([\w -]{1,31}?)\"?|\"?([\w -]{1,31}?)\"?\s+sheet)\b")

_COND_OPS: Tuple[Tuple[str, str], ...] = (
    (r">=|at least|minimum of|or more|greater than or equal to", "gte"),
    (r"<=|at most|or less|less than or equal to", "lte"),
    (r">|above|over|greater than|more than|higher than|exceeds?|se zyada|se jyada|thi vadhu|से ज़्यादा|से अधिक|થી વધુ", "gt"),
    (r"<|below|under|less than|lower than|se kam|thi ochhu|से कम|થી ઓછું", "lt"),
)


def _heading_text_before(clause: str, start: int, original_clause: str) -> Optional[str]:
    """'the Recommendations heading' → 'Recommendations'; quoted text wins."""
    q = _QUOTED.search(clause)
    if q:
        return _original_case(q.group(1).strip(), original_clause)
    head = clause[:start].strip()
    words = [w for w in re.split(r"\s+", head) if w]
    kept: List[str] = []
    for w in reversed(words):
        wc = w.strip(",.;:").casefold()
        if wc in _STOP or wc in COLOR_NAMES or wc in ("dark", "light", "bold", "italic", "level", "from", "remove", "no", "without", "off") or _COLOUR_NAME_RE.fullmatch(wc):
            break
        kept.append(w.strip(",.;:"))
        if len(kept) >= 4:
            break
    text = " ".join(reversed(kept)).strip()
    return text or None


def _target_in(clause: str, kind: str, original: str) -> Tuple[Optional[_Target], int]:
    """The element a clause talks about, and where the mention starts."""
    m = _A1_IN_TEXT.search(clause)
    if m:
        a1 = f"{m.group(1)}:{m.group(2)}" if m.group(1) else m.group(3)
        sheet = _sheet_in(clause, original)
        try:
            return _Target(StyleTarget(kind="cell_range", a1=a1, sheet=sheet)), m.start()
        except Exception:
            return None, -1
    m = _ROW_RE.search(clause)
    if m and not re.search(_w("header row|totals? row"), clause):
        n = int(m.group(1) or m.group(2))
        return _Target(StyleTarget(kind="row", index=n, sheet=_sheet_in(clause, original))), m.start()
    m = _COLUMN_RE.search(clause)
    if m and not re.search(_w("column headers?|column headings?"), clause):
        raw = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        name = _clean_column_name(raw, original)
        if name:
            return _Target(StyleTarget(kind="column", name=name, sheet=_sheet_in(clause, original))), m.start()
    best: Optional[Tuple[int, str, "re.Match[str]"]] = None
    for rx, k in _TARGET_RES:
        for mm in rx.finditer(clause):
            if best is None or mm.start() < best[0] or (mm.start() == best[0] and len(mm.group(0)) > len(best[2].group(0))):
                best = (mm.start(), k, mm)
            break
    if best is None:
        return None, -1
    start, k, mm = best
    if k == "slide_background":
        return _Target(None, "slide_background"), start
    if k == "page_background":
        return _Target(None, "page_background"), start
    if k == "body":
        return _Target(None, "body"), start
    if k == "table_header_ambiguous":
        if kind == "document" and not re.search(_w("table|row|column"), clause):
            return _Target(StyleTarget(kind="header_footer")), start
        k = "table_header"
    if k == "heading_level":
        level = int(mm.group(1)) if mm.lastindex else 1
        if kind == "presentation":
            return _Target(StyleTarget(kind="slide_title")), start
        if kind == "workbook":
            return _Target(StyleTarget(kind="table_header")), start
        return _Target(StyleTarget(kind="heading", level=level)), start
    if k == "heading":
        if kind == "presentation":
            return _Target(StyleTarget(kind="slide_title")), start
        if kind == "workbook":
            return _Target(StyleTarget(kind="table_header")), start
        text = _heading_text_before(clause, start, original) if not re.search(_w("all|every"), clause[:start]) or _QUOTED.search(clause) else None
        if text and (text.casefold() in ("section", "main", "sub") or len(text) < 3):
            text = None
        if text:
            try:
                return _Target(StyleTarget(kind="heading", text=_original_case(text, original))), start
            except Exception:
                pass
        return _Target(StyleTarget(kind="heading")), start
    if k == "title" and kind == "workbook":
        return _Target(StyleTarget(kind="title")), start
    if k == "first_paragraph":
        return _Target(StyleTarget(kind="paragraph", first=True)), start
    if k == "bullet" and kind == "presentation":
        return _Target(StyleTarget(kind="slide_body")), start
    if k == "table_body" and kind == "workbook" and False:
        pass
    return _Target(StyleTarget(kind=k)), start


def _original_case(text: str, original: str) -> str:
    i = original.casefold().find(text.casefold())
    return original[i:i + len(text)] if i >= 0 else text


def _clean_column_name(raw: str, original: str) -> Optional[str]:
    words = [w for w in raw.split() if w]
    while words and (words[0].casefold() in _STOP or words[0].casefold() in ("make", "the", "color", "colour", "paint", "highlight", "fill", "shade", "bold", "italic") or _COLOUR_NAME_RE.fullmatch(words[0].casefold())):
        words.pop(0)
    while words and (words[-1].casefold() in _STOP):
        words.pop()
    if not words:
        return None
    name = " ".join(words)
    if _COLOUR_NAME_RE.fullmatch(name.casefold()) or name.casefold() in ("a", "new", "each", "every", "first", "last", "this", "that", "one"):
        return None
    return _original_case(name, original)[:80]


def _sheet_in(clause: str, original: str) -> Optional[str]:
    m = _SHEET_RE.search(clause)
    if not m:
        return None
    name = (m.group(1) or m.group(2) or "").strip()
    if not name or name.casefold() in _STOP:
        return None
    return _original_case(name, original)[:31]


_TEXT_ROLE_KINDS = {"title", "subtitle", "heading", "paragraph", "bullet", "caption", "kpi_value", "kpi_label", "slide_title", "slide_body",
                    "chart_title", "chart_axis", "chart_legend", "chart_labels", "header_footer", "callout"}
_FILL_ROLE_KINDS = {"table_header", "table_total", "row", "cell_range", "column", "table_body", "table"}


def _style_in(clause: str, kind_for_role: Optional[str], notes: List[str]) -> Tuple[TextStyle, List[str]]:
    """The properties a clause asks for. Returns (style, consumed markers)."""
    data: Dict[str, Any] = {}
    found: List[str] = []
    colours = _find_colours(clause)
    text_words = re.compile(_w(r"text|font|letters?|writing|lettering|foreground|ink|words?"))
    bg_words = re.compile(_w(r"background|fill|filled|shade|shaded|shading|highlight|highlighted|band|backdrop|cell color"))
    on_match = re.search(r"(?<![\w])on(?![\w])", clause)
    if colours:
        found.append("color")
        if len(colours) >= 2 and on_match and colours[0][0] < on_match.start() < colours[1][0]:
            data["color"], data["_color_named"] = colours[0][2], colours[0][3]
            data["background"] = colours[1][2]
        else:
            for start, end, hexv, named in colours[:2]:
                after = clause[end:end + 22]
                before = clause[max(0, start - 22):start]
                if bg_words.search(after) or bg_words.search(before):
                    data.setdefault("background", hexv)
                elif text_words.search(after) or text_words.search(before):
                    data.setdefault("color", hexv)
                    data["_color_named"] = named
                elif kind_for_role in _FILL_ROLE_KINDS or kind_for_role in ("slide_background", "page_background"):
                    data.setdefault("background", hexv)
                else:
                    if "color" in data:
                        data.setdefault("background", hexv)
                    else:
                        data["color"] = hexv
                        data["_color_named"] = named
    if re.search(_w(r"not bold|no bold|unbold|remove bold|without bold|non[\s-]?bold|regular weight|normal weight"), clause):
        data["bold"] = False
        found.append("bold")
    elif re.search(_w(r"bold|bolded|thick|strong|heavy|gadha|gaadha|mota|mote|jada"), clause):
        data["bold"] = True
        found.append("bold")
    if re.search(_w(r"not italic|no italic|remove italic|without italic|upright"), clause):
        data["italic"] = False
        found.append("italic")
    elif re.search(_w(r"italic|italicised|italicized|slanted|tircha|tirchha"), clause):
        data["italic"] = True
        found.append("italic")
    if re.search(_w(r"no underline|remove underline|without underline|not underlined"), clause):
        data["underline"] = False
        found.append("underline")
    elif re.search(_w(r"underline|underlined"), clause):
        data["underline"] = True
        found.append("underline")
    m = re.search(_w(r"(?:align(?:ed)?\s+)?(left|right|center|justify|justified)(?:\s+align(?:ed)?)?"), clause)
    if m and re.search(_w(r"align|aligned|alignment|center|justify|justified"), clause):
        data["align"] = {"justified": "justify"}.get(m.group(1), m.group(1))
        found.append("align")
    m = _SIZE_RE.search(re.sub(r"\blevel [123]s?\b", " ", _HEX_IN_TEXT.sub(" ", clause)))
    if m is None:
        # AS3 integration (live 2026-09-15): "headings in Arial 16" — a bare
        # number right after a known font name is its size.
        fm = re.search(r"\b(" + "|".join(re.escape(k) for k in sorted(FONT_ALLOWLIST, key=len, reverse=True)) + r")\s+(\d{1,2}(?:\.\d)?)(?![\d.%:a-z])", clause)
        if fm and 6 <= float(fm.group(2)) <= 72:
            m = re.match(r"(\d{1,2}(?:\.\d)?)()", fm.group(2))
    if m:
        raw = m.group(1) or m.group(3) or m.group(4) or m.group(5)
        unit = (m.group(2) or "pt").casefold()
        size = float(raw) * (0.75 if unit == "px" else 1.0)
        if size < 6 or size > 72:
            notes.append(f"A font size of {raw} was changed to {min(max(size, 6), 72):g} pt, the range a document can carry.")
            size = min(max(size, 6), 72)
        data["size_pt"] = size
        found.append("size")
    if re.search(_w(r"bigger|larger|large|big|bada|badi|bade"), clause) and "size_pt" not in data:
        data["_bigger"] = True
    face = _find_font(clause, clause)
    if face:
        data["font_family"] = face
        found.append("font")
    named = data.pop("_color_named", False)
    bigger = data.pop("_bigger", False)
    style = TextStyle.model_validate({k: v for k, v in data.items()})
    if bigger:
        found.append("bigger")
    if named and style.color and kind_for_role in _TEXT_ROLE_KINDS | {None} and not style.background:
        safe = TEXT_SAFE.get(style.color)
        if safe and contrast_ratio(style.color, WHITE) < 4.5:
            notes.append(f"{colour_name(style.color).capitalize()} text uses the darker {safe} so it stays readable on white.")
            style = style.model_copy(update={"color": safe})
    return style, found


_CLAUSE_SPLIT = re.compile(r"\s*(?:;|\n|,(?!\d)|\.(?=\s|$)|\s+(?:and|aur|or|also|plus|then|with|wih|while|&)\s+|\bbut\b)\s*")


def _clauses(low: str) -> List[Tuple[str, bool]]:
    """(clause, continues_previous) pairs. 'with' and a leading colour
    continue the previous clause's target."""
    protected = re.sub(r"between\s+(\S+)\s+and\s+(\S+)", r"between \1 ~and~ \2", low)
    parts: List[Tuple[str, bool]] = []
    pos = 0
    for m in _CLAUSE_SPLIT.finditer(protected):
        piece = protected[pos:m.start()]
        parts.append((piece, False))
        pos = m.end()
        sep = m.group(0).strip()
        if parts and sep in ("with", "wih", "&", "and", "aur", ",", "while"):
            parts.append(("__CONT__", True))
    parts.append((protected[pos:], False))
    out: List[Tuple[str, bool]] = []
    cont = False
    for piece, is_marker in parts:
        if is_marker:
            cont = True
            continue
        piece = piece.replace("~and~", "and").strip()
        if piece:
            out.append((piece, cont))
        cont = False
    return out


def _cond_in(clause: str, column_ctx: Optional[str], notes: List[str]) -> Optional[CondRule]:
    """'Status column red for Fail', 'Fail in red', 'rows where status is
    Blocked in red', 'score above 80 green', 'amount > 1000 bold'."""
    colours = _find_colours(clause)
    whole_row = bool(re.search(_w(r"rows?|entire row|whole row|full row"), clause)) and not _ROW_RE.search(clause)
    column = column_ctx
    m = re.search(r"(?:where|when|if|jahan|jaha|jyare|जहाँ|જ્યાં)\s+(?:the\s+)?([a-z][\w ]{0,30}?)\s+(?:is|=|==|equals|hai|ho|che|છે|है)\s+\"?([\w /-]{1,40}?)\"?(?=$|\s+(?:in|as|to|then|make|color|red|green|blue|yellow|orange|amber|bold|italic)\b|\s*[,.;])", clause)
    style_data: Dict[str, Any] = {}
    value: Any = None
    op = "eq"
    value2: Any = None
    if m:
        column = m.group(1).strip()
        value = m.group(2).strip()
    for pattern, cop in _COND_OPS:
        mm = re.search(r"(?:([a-z][\w ]{0,30}?)\s+)?(?:is\s+)?(?:" + pattern + r")\s*(-?\d+(?:\.\d+)?)", clause)
        if mm:
            if mm.group(1):
                cand = _clean_column_name(mm.group(1), mm.group(1))
                if cand and not _COLOUR_NAME_RE.fullmatch(cand.casefold()):
                    column = cand
            op, value = cop, float(mm.group(2))
            break
    mb = re.search(r"between\s+(-?\d+(?:\.\d+)?)\s+and\s+(-?\d+(?:\.\d+)?)", clause)
    if mb:
        op, value, value2 = "between", float(mb.group(1)), float(mb.group(2))
    # --- AS3 integration BEGIN (live 2026-09-15) ---
    # "make Status red where Blocked", "green where Done" (the column carried
    # from the clause before), "negative growth in red", "positive in green".
    if value is None and colours:
        after = clause[colours[0][1]:]
        mw = re.match(r"\s*(?:(?:fill|background|text|colou?r)\s+)?(where|when|if|for)\s+(?:it\s+is\s+|it's\s+|its\s+|the\s+value\s+is\s+)?\"?([a-z][\w /-]{0,38}?)\"?\s*$", after)
        if mw and (re.fullmatch(r"(?:possible|applicable|needed|necessary|required|appropriate|relevant|you can|it fits)", mw.group(2).strip())
                   or (mw.group(1) == "for" and not _is_status_value(mw.group(2).strip()))):
            mw = None
        if mw:
            head = re.sub(r"^(?:(?:please|pls|make|set|colou?r|highlight|mark|show|paint|fill|the|a|an)\s+)+", "", clause[:colours[0][0]].strip())
            head = re.sub(r"\s+(?:column|col|cells?|values?|text)$", "", head).strip()
            cand = _clean_column_name(head, head) if head else None
            if cand and len(cand.split()) <= 3 and not _COLOUR_NAME_RE.fullmatch(cand.casefold()) and not re.fullmatch(
                    r"(?:title|subtitle|header|headers|header row|heading|headings|paragraphs?|text|table|rows?|body|totals?|total row|chart|slide|document|page|it|this|that)", cand.casefold()):
                column = cand
            if column:
                value = mw.group(2).strip()
    if value is None and colours and re.search(_w(r"negative|below zero|less than zero|losses|loss"), clause) and not re.search(_w(r"positive"), clause):
        op, value = "lt", 0.0
        column = column or _numeric_column_hint(clause, column_ctx)
    elif value is None and colours and re.search(_w(r"positive|above zero|greater than zero|gains?"), clause) and not re.search(_w(r"negative"), clause):
        op, value = "gt", 0.0
        column = column or _numeric_column_hint(clause, column_ctx)
    # --- AS3 integration END ---
    if value is None:
        mf = re.search(r"(?:for|when|if|=|wale|vala|wala|ho to|hoy to|वाले|વાળા)\s+\"?([a-z][\w /-]{0,30}?)\"?\s*(?:$|values?|rows?|cells?|status|hai|che)", clause)
        mi = re.search(r"^\"?([a-z][\w /-]{0,30}?)\"?\s+(?:in|as|=|->|→|:)\s+", clause)
        named_column: Optional[str] = None
        if mi is None and colours:  # AS3 integration: "Overdue wale red me" names no column; a status word implies Status
            head = clause[:colours[0][0]].strip()
            for n in (2, 1):
                words = head.split()[-n:]
                if len(words) == n and _is_status_value(" ".join(words)):
                    mi = re.match(r"(.+?) in $", " ".join(words) + " in ")
                    # "make Priority Critical orange": the words before the
                    # value name its column.
                    split = _split_column_value(head) if column_ctx is None else None
                    named_column = split[0] if split else None
                    break
        cand = None
        if mf:
            cand = mf.group(1).strip()
        elif mi and colours:
            cand = mi.group(1).strip()
        if cand and _is_status_value(cand):
            value = cand
            column = column or named_column
        elif cand and column_ctx and not _COLOUR_NAME_RE.fullmatch(cand):
            value = cand
        elif cand and colours and column_ctx is None:
            # "highlight Priority Critical in orange": a column name then a
            # status value ("Critical priority" the other way round).
            split = _split_column_value(cand)
            if split is not None:
                column, value = split
    if value is None or (not colours and not re.search(_w("bold|italic|underline"), clause)):
        return None
    if isinstance(value, str):
        value = _strip_colour_words(value)
        if not value:
            return None
    if column is None:
        column = "Status" if isinstance(value, str) and _is_status_value(value) else None
    if column is None:
        return None
    if colours:
        hexv = colours[0][2]
        if re.search(_w(r"text|font|letters?"), clause):
            style_data["color"] = hexv
        else:
            style_data["background"] = hexv
            style_data["color"] = readable_on(hexv) if contrast_ratio(INK, hexv) < 4.5 else None
    if re.search(_w("bold"), clause):
        style_data["bold"] = True
    if re.search(_w("italic"), clause):
        style_data["italic"] = True
    try:
        return CondRule(column=column[:80], op=op, value=value, value2=value2, style=TextStyle.model_validate({k: v for k, v in style_data.items() if v is not None}), whole_row=whole_row)
    except Exception:
        return None


_ELEMENT_WORDS_RE = re.compile(
    r"(?:title|subtitle|header|headers|header row|heading|headings|paragraphs?|text|table|tables|rows?|body|totals?|total row|"
    r"chart|slide|slides|document|page|cells?|values?|it|this|that|all|every|everything|anything|each|any|entire|whole|full|column|columns|items?|tasks?|entries|records?)")


def _split_column_value(text: str) -> Optional[Tuple[str, str]]:
    """(column, value) from 'Priority Critical' or 'Critical priority': a
    known status value at one end, a column name at the other. The
    column-first form takes any name that is not an element word; the
    value-first form only a status-like column word, so 'critical rows'
    stays a whole-row rule on the status column."""
    words = [w for w in re.sub(r"^(?:(?:please|pls|make|set|colou?r|highlight|mark|show|paint|fill|shade|the|a|an|all)\s+)+", "", text.strip()).split() if w]
    if len(words) < 2:
        return None
    for n in (2, 1):
        if len(words) <= n:
            continue
        tail, rest = " ".join(words[-n:]), " ".join(words[:-n])
        if _is_status_value(tail):
            name = _clean_column_name(re.sub(r"\s+(?:column|col)$", "", rest), rest)
            if name and len(name.split()) <= 3 and not _COLOUR_NAME_RE.fullmatch(name.casefold()) \
                    and not _ELEMENT_WORDS_RE.fullmatch(name.casefold()) and not _is_status_value(name):
                return name, tail
        head, rest = " ".join(words[:n]), " ".join(words[n:])
        if _is_status_value(head):
            name = re.sub(r"\s+(?:column|col)$", "", rest).strip()
            if name and len(name.split()) <= 2 and STATUS_COLUMN_RE.fullmatch(name):
                return name, head
    return None


def _numeric_column_hint(clause: str, column_ctx: Optional[str]) -> Optional[str]:
    """AS3 integration: "negative growth in red" names the column by a word
    of it ("growth" → the "Growth %" column carried as context)."""
    if column_ctx:
        return column_ctx
    m = re.search(r"(?:negative|positive|losses|loss|gains?)\s+([a-z][\w %]{1,30}?)\s+(?:in|as|to)\b", clause)
    return _clean_column_name(m.group(1), m.group(1)) if m else None


def _strip_colour_words(value: str) -> str:
    words = [w for w in value.split() if not _COLOUR_NAME_RE.fullmatch(w) and w not in ("in", "as", "color", "colour", "text", "background")]
    return " ".join(words).strip()


_ALL_STATUS = {v for vals in STATUS_VALUES.values() for v in vals}


def _is_status_value(text: str) -> bool:
    return _fold(text) in _ALL_STATUS


def parse_style_request(text: str, kind: str = "document") -> Tuple[StylePatch, List[str]]:
    """A person's styling request → (StylePatch, unparsed style phrases).

    Deterministic, no model: preset adjectives ('classy', 'modern'),
    orientation, paper size, margins, page numbers, header/footer text,
    fonts and sizes, bold/italic/underline, alignment and colours per
    element, conditional colours ('Status red for Fail'), banding and
    freezing, in English, Hinglish, Hindi and Gujarati, with common typos.
    A clause that carries a style word but could not be read is returned
    as an unparsed phrase for `extract_patch_llm`; a clause with no style
    word (content) is ignored. Notes (a clamped size, a text-safe colour
    substitution) are available via `parse_style_request_with_notes`."""
    patch, unparsed, _notes = parse_style_request_with_notes(text, kind)
    return patch, unparsed


_REMOVE_RE = re.compile(_w(
    r"remove|removes|delete|clear|reset|get rid of|take off|take away|strip|revert|undo|hatao|hata do|hatado|hata|nikalo|nikal do|"
    r"हटाओ|हटा|हटाएं|हटाइए|निकालो|निकाल|કાઢો|કાઢી|દૂર|no fill|no colou?r|no background|no highlight|no shading|"
    r"without (?:colou?r|fill|background|highlight)"
))
#: "do" is a Hinglish filler the normaliser removes, so "do not make" reaches
#: here as "not make".
_NEGATED_RE = re.compile(_w(r"don'?t|dont|do not|never|mat|nahi|nahin|नहीं|मत|નહીં|નહિ|not (?:make|use|put|color|change|set|apply|give|turn|want)"))
_RESET_RE = re.compile(_w(
    r"(?:remove|clear|reset|undo|strip|delete) (?:all )?(?:the )?(?:formatting|styles?|styling)|"
    r"(?:reset|restore) (?:to )?(?:the )?(?:default|original) (?:style|look|formatting)|back to (?:the )?default (?:style|look|formatting)"
))
_SECTION_RE = re.compile(
    r"(?:in|of|under|within)\s+(?:the\s+)?(?:\"([^\"]{1,60})\"|([\w&' -]{2,40}?))\s+section\b"
    r"|\bsection\s+(?:called\s+|named\s+)?(?:\"([^\"]{1,60})\"|(\d{1,2}|[\w&'-]{2,40}?))(?=\s+(?:paragraphs?|text|body)\b|\s*$)"
    # AS3 integration (live 2026-09-15): "the Risks section paragraphs in italic".
    r"|(?:^|\b(?:the|make|set)\s+)(?:\"([^\"]{1,60})\"|([\w&'-]{2,40}?))\s+section(?:'s)?\s+(?:paragraphs?|text|body)\b"
)
_CONTENT_PREP_RE = re.compile(_w(r"about|regarding|on|of|for|par|pe|vishay|विषय|પર|વિશે|बारे"))
def _trailing_colour_style(clause: str) -> bool:
    """'… in blue', '… with dark green colour' at the end of a clause: a
    style request even inside a content ask."""
    m = re.search(r"\b(?:in|with)\s+((?:dark\s+|light\s+)?[^\s]+(?:\s+[^\s]+)?)\s*(?:color)?\s*$", clause)
    return bool(m and _COLOUR_NAME_RE.fullmatch(m.group(1).strip()))


def _section_in(clause: str, original: str) -> Optional[str]:
    m = _SECTION_RE.search(clause)
    if not m:
        return None
    name = next((g for g in m.groups() if g), "").strip().strip('"')
    if not name or name.casefold() in _STOP or name.casefold() in ("this", "that", "each", "every", "the"):
        return None
    if name.isdigit():
        return f"Section {name}"
    return _original_case(name, original)[:200]


def _removed_props(clause: str, style: TextStyle) -> List[str]:
    """The colour/font/size/alignment properties a removal clause takes off."""
    props: List[str] = []
    colour_word = bool(style.color or style.background) or bool(re.search(_w(r"color|colors|fill|background|highlight|highlighting|shade|shading"), clause))
    if colour_word:
        if re.search(_w(r"background|fill|highlight|highlighting|shade|shading"), clause):
            props.append("background")
        elif re.search(_w(r"text color|font color|letters?"), clause):
            props.append("color")
        else:
            props.extend(["color", "background"])
    if style.font_family or re.search(_w(r"font|typeface"), clause) and not colour_word:
        props.append("font_family")
    if style.size_pt or re.search(_w(r"size"), clause):
        props.append("size_pt")
    if style.align or re.search(_w(r"alignment|align"), clause):
        props.append("align")
    return list(dict.fromkeys(props))


def parse_style_request_with_notes(text: str, kind: str = "document") -> Tuple[StylePatch, List[str], List[str]]:
    low = _normalise_request(text)
    low = _HINGLISH_FILLER.sub(" ", low)
    # Every whitespace run (a no-break space included) is one space, and a
    # line break swallows the blank lines after it: _CLAUSE_SPLIT's `\s*`
    # retried from every position of a long run (a 4,000-character run of
    # no-break spaces took 120 s, verifier 2026-09-15).
    low = re.sub(r"[^\S\n]+", " ", low)
    low = re.sub(r"\n\s*", "\n", low)
    notes: List[str] = []
    unparsed: List[str] = []
    rules: List[StyleRule] = []
    conds: List[CondRule] = []
    scales: List[ColorScale] = []
    page: Dict[str, Any] = {}
    fonts: Dict[str, Any] = {}
    hf: Dict[str, Any] = {}
    top: Dict[str, Any] = {}
    clears: List[ClearRule] = []

    # Quoted header/footer text is read from the ORIGINAL (case kept).
    for which in ("header", "footer"):
        m = re.search(which + r"(?:\s+text)?\s*(?:should\s+(?:say|read)|saying|reading|as|:|=|with)?\s*[\"“']([^\"”']{1,120})[\"”']", text or "", re.IGNORECASE)
        if m:
            hf[f"{which}_text"] = m.group(1).strip()

    # Quoted text is data (a footer line, a heading name), never clause
    # structure: "Internal & confidential" must not split on its "&".
    quoted: List[str] = []

    def _hold(m: "re.Match[str]") -> str:
        quoted.append(m.group(0))
        return f' "q{len(quoted) - 1}" '

    low = re.sub(r"(?<![\w])[\"'][^\"'\n]{1,120}[\"'](?![\w])", _hold, low)

    if _BOARDROOM_RE.search(low):
        top["preset"] = "boardroom"
    elif _MODERN_RE.search(low):
        top["preset"] = "modern"
    elif _TEAL_PRESET_RE.search(low):
        top["preset"] = "teal"
    elif _MINIMAL_RE.search(low) and not re.search(_w("clean data|simple table"), low):
        top["preset"] = "minimal"
    elif _CLASSIC_RE.search(low):
        top["preset"] = "classic"

    if re.search(_w(r"with colou?rs?|in colou?rs?|colorful|colourful|rangeen|colou?rs? (?:do|dena|daalo|dalo|chahiye)|colored|coloured"), low) and not _find_colours(low):
        top["auto_status_colors"] = True
        top["banded"] = True
        low = re.sub(_w(r"with colou?rs?|in colou?rs?|colorful|colourful|rangeen|colou?rs? (?:do|dena|daalo|dalo|chahiye)|colored|coloured"), " ", low)

    ctx: Optional[_Target] = None
    column_ctx: Optional[str] = None
    prev_content = False
    for clause, continues in _clauses(low):
        consumed = False
        was_content, prev_content = prev_content, False
        clause = re.sub(r'"q(\d+)"', lambda m: quoted[int(m.group(1))] if int(m.group(1)) < len(quoted) else m.group(0), clause)
        original_clause = clause
        # --- page-level --------------------------------------------------
        if re.search(_w(r"landscape|horizontal page|wide page|sideways|aadu|aada|आड़ा"), clause):
            page["orientation"] = "landscape"
            consumed = True
        elif re.search(_w(r"portrait|vertical page|khada|ubhu|सीधा"), clause):
            page["orientation"] = "portrait"
            consumed = True
        m = re.search(_w(r"a4|a3|a5|letter size|us letter|letter|legal size|legal"), clause)
        if m and re.search(_w(r"a4|a3|a5|paper|page|size|letter size|us letter|legal size"), clause):
            page["size"] = {"a4": "A4", "a3": "A3", "a5": "A5", "letter size": "Letter", "us letter": "Letter", "letter": "Letter", "legal size": "Legal", "legal": "Legal"}[m.group(0)]
            consumed = True
        m = re.search(_w(r"(narrow|small|thin|wide|large|big|normal|standard) margins?|margins? (?:narrow|small|wide|large|normal)"), clause)
        if m:
            word = re.search(r"narrow|small|thin|wide|large|big|normal|standard", m.group(0)).group(0)
            page["margins"] = {"narrow": "narrow", "small": "narrow", "thin": "narrow", "wide": "wide", "large": "wide", "big": "wide", "normal": "normal", "standard": "normal"}[word]
            consumed = True
        if re.search(_w(r"16:9|widescreen"), clause):
            page["slide_ratio"] = "16:9"
            consumed = True
        elif re.search(_w(r"4:3"), clause):
            page["slide_ratio"] = "4:3"
            consumed = True
        if re.search(_w(r"no page numbers?|without page numbers?|remove page numbers?|hide page numbers?"), clause):
            hf["page_numbers"] = False
            consumed = True
        elif re.search(_w(r"page numbers?|page no\.?|page count|numbered pages"), clause):
            hf["page_numbers"] = True
            consumed = True
        if re.search(_w(r"no (?:banding|zebra|stripes|alternate row colou?rs?)|without (?:banding|zebra|stripes)|remove (?:banding|zebra|stripes)"), clause):
            top["banded"] = False
            consumed = True
        elif re.search(_w(r"zebra|banded|banding|striped|stripes|alternate rows?|alternating rows?"), clause):
            top["banded"] = True
            consumed = True
        if re.search(_w(r"(?:don'?t|do not|no|without|unfreeze) (?:freeze|frozen|freezing)|unfreeze"), clause):
            top["freeze_header"] = False
            consumed = True
        elif re.search(_w(r"freeze|frozen|sticky header|lock (?:the )?header"), clause):
            top["freeze_header"] = True
            consumed = True
        if re.search(_w(r"colorful|with colors?|in colors?|colored|rangeen|colou?rs? (?:do|dena|daalo|dalo)"), clause) and not _find_colours(clause):
            top["auto_status_colors"] = True
            top["banded"] = True
            consumed = True
        if re.search(_w(r"no colors?|without colors?|black and white|plain (?:black|white)|monochrome"), clause):
            top["auto_status_colors"] = False
            top["auto_score_scale"] = False
            top["banded"] = False
            consumed = True
        if re.search(_w(r"(?:colou?r|heat) ?scale|heat ?map colou?rs?|gradient"), clause):
            mcol = _COLUMN_RE.search(clause)
            name = _clean_column_name((mcol.group(1) or mcol.group(2) or ""), text) if mcol else None
            if name and re.search(_w(r"scale|gradient|heat"), name.casefold()):
                # AS3 integration (live): "put a colour scale on the Score
                # column" named the column 'scale on the Score'.
                tail = re.search(r"(?:on|for|of|to)\s+(?:the\s+)?([a-z][\w %]{0,30}?)$", name, re.I)
                name = _clean_column_name(tail.group(1), text) if tail else None
            if name is None:
                mo = re.search(r"(?:on|for|of)\s+(?:the\s+)?([a-z][\w ]{0,30}?)(?:\s+column)?$", clause)
                name = _clean_column_name(mo.group(1), text) if mo else None
            if name:
                scales.append(ColorScale(column=name))
                consumed = True
        if hf.get("header_text") and re.search(_w("header"), clause) and re.search(r"[\"']", clause):
            consumed = True
        if hf.get("footer_text") and re.search(_w("footer"), clause) and re.search(r"[\"']", clause):
            consumed = True
        if top.get("preset") and (_CLASSIC_RE.search(clause) or _MODERN_RE.search(clause) or _MINIMAL_RE.search(clause) or _BOARDROOM_RE.search(clause)):
            consumed = True

        if _RESET_RE.search(clause):
            top["reset"] = True
            consumed = True
            continue

        # --- removals and negations -------------------------------------
        # "remove the yellow from row 5" takes the fill OFF (a ClearRule);
        # "don't make the headings blue" asks for nothing. Neither may ever
        # add the colour it names.
        removal = bool(_REMOVE_RE.search(clause))
        negated = bool(_NEGATED_RE.search(clause)) and not removal
        if removal or negated:
            tgt, start = _target_in(clause, kind, text or "")
            if tgt is None and continues and ctx is not None:
                tgt = ctx
            role = tgt.scope if (tgt is not None and tgt.scope) else (tgt.target.kind if tgt is not None and tgt.target is not None else None)
            n0 = len(notes)
            style, found = _style_in(clause, role if role != "body" else "paragraph", notes)
            del notes[n0:]
            falses = {k: False for k in ("bold", "italic", "underline") if getattr(style, k) is False or (removal and getattr(style, k) is True)}
            targets: List[StyleTarget] = []
            if tgt is not None and tgt.target is not None:
                targets = [tgt.target]
            elif tgt is not None and tgt.scope == "body":
                targets = [StyleTarget(kind="slide_body" if kind == "presentation" else ("table_body" if kind == "workbook" else "paragraph"))]
                if kind == "document":
                    targets.append(StyleTarget(kind="bullet"))
            if targets:
                props = _removed_props(clause, style) if removal else []
                for t in targets:
                    if props:
                        clears.append(ClearRule(target=t, props=props))
                    if falses:
                        rules.append(StyleRule(target=t, style=TextStyle(**falses)))
                if tgt is not None:
                    ctx = tgt
            if tgt is not None and tgt.scope in ("page_background", "slide_background") and removal:
                page["background"] = None
            if removal and not targets and not consumed and not (tgt is not None and tgt.scope) and _STYLE_SIGNAL.search(clause):
                unparsed.append(original_clause.strip())
            continue

        # --- conditional colours ----------------------------------------
        tgt, start = _target_in(clause, kind, text or "")
        if tgt is not None and tgt.target is not None and tgt.target.kind == "column":
            column_ctx = tgt.target.name
        cond = _cond_in(clause, column_ctx if (tgt is None or (tgt.target is not None and tgt.target.kind == "column") or continues) else None, notes)
        if cond is not None:
            conds.append(cond)
            ctx = _Target(StyleTarget(kind="column", name=cond.column)) if cond.column else ctx
            column_ctx = cond.column
            continue

        # --- element rules ----------------------------------------------
        if tgt is None and continues and ctx is not None:
            tgt = ctx
        role = tgt.scope if (tgt is not None and tgt.scope) else (tgt.target.kind if tgt is not None and tgt.target is not None else None)
        n0 = len(notes)
        style, found = _style_in(clause, role if role != "body" else "paragraph", notes)
        if tgt is None and found == ["color"] and not _HEX_IN_TEXT.search(clause) and not _STYLE_SIGNAL.search(clause) \
                and not _trailing_colour_style(clause) \
                and (_CONTENT_VERB_RE.search(clause) or _CONTENT_PREP_RE.search(clause) or (continues and was_content)):
            # A colour word inside a content ask ("a report on the red team
            # exercise", "the white house briefing", "black friday deals")
            # is a name, not a style: no rule, no model call, no note.
            del notes[n0:]
            prev_content = True
            continue
        if tgt is None and style.is_empty() and _CONTENT_VERB_RE.search(clause) and not _STYLE_SIGNAL.search(clause):
            prev_content = True
        if tgt is None:
            if style.font_family and not (style.color or style.background):
                fonts["body"] = style.font_family
                fonts["heading"] = style.font_family
                consumed = True
                if style.size_pt:
                    top["base_size_pt"] = min(max(style.size_pt, 8), 16)
            elif style.size_pt and re.search(_w("font|text|size"), clause) and not (style.color or style.background):
                top["base_size_pt"] = min(max(style.size_pt, 8), 16)
                consumed = True
            elif not style.is_empty() or "bigger" in found:
                unparsed.append(original_clause.strip())
                continue
        else:
            if tgt.scope == "body":
                section = _section_in(clause, text or "") if kind == "document" else None
                if section is not None and not style.is_empty():
                    # "paragraphs in the Risks section italic": that section
                    # only, fonts and size included — never the whole file.
                    rules.append(StyleRule(target=StyleTarget(kind="paragraph", section=section), style=style))
                    ctx = tgt
                    continue
                if style.font_family:
                    fonts["body"] = style.font_family
                    consumed = True
                if style.size_pt:
                    top["base_size_pt"] = min(max(style.size_pt, 8), 16)
                    consumed = True
                rest = style.model_copy(update={"font_family": None, "size_pt": None})
                if not rest.is_empty():
                    target_kind = "slide_body" if kind == "presentation" else ("table_body" if kind == "workbook" else "paragraph")
                    rules.append(StyleRule(target=StyleTarget(kind=target_kind), style=rest))
                    if kind == "document":
                        rules.append(StyleRule(target=StyleTarget(kind="bullet"), style=rest))
                    consumed = True
                if not consumed and _leftover_words(clause, tgt):
                    unparsed.append(original_clause.strip())
                    ctx = tgt
                    continue
                ctx = tgt
            elif tgt.scope in ("slide_background", "page_background"):
                bg = style.background or style.color
                if bg:
                    page["background"] = bg
                    consumed = True
            elif tgt.target is not None:
                t = tgt.target
                if t.kind == "heading" and style.font_family and len(style.set_fields()) == 1 and t.text is None and t.level is None:
                    fonts["heading"] = style.font_family
                    consumed = True
                elif not style.is_empty():
                    rules.append(StyleRule(target=t, style=style))
                    consumed = True
                elif not consumed and _leftover_words(clause, tgt):
                    # The clause names an element and says something about
                    # it this parser cannot read ("a burgundy tone", "bada"):
                    # that is a style phrase for the model, not content.
                    unparsed.append(original_clause.strip())
                    ctx = tgt
                    continue
                ctx = tgt
        if not consumed and _STYLE_SIGNAL.search(clause):
            unparsed.append(original_clause.strip())

    # Merge rules with the same target (clause continuations).
    merged: Dict[str, StyleRule] = {}
    for r in rules:
        k = r.target.key()
        if k in merged:
            merged[k] = StyleRule(target=r.target, style=TextStyle.model_validate(r.style.over(merged[k].style).model_dump()))
        else:
            merged[k] = r
    patch_data: Dict[str, Any] = dict(top)
    if page:
        patch_data["page"] = PageStyle.model_validate(page)
    if fonts:
        patch_data["fonts"] = FontSpec.model_validate(fonts)
    if hf:
        patch_data["header_footer"] = HeaderFooter.model_validate(hf)
    patch_data["rules"] = list(merged.values())[:50]
    patch_data["clear"] = clears[:20]
    patch_data["conditional"] = conds[:30]
    patch_data["scales"] = scales[:10]
    unparsed = [u for u in dict.fromkeys(unparsed) if u]
    return StylePatch.model_validate(patch_data), unparsed, notes


_TARGET_WORD_RE = re.compile(_w(
    r"the|a|an|all|every|make|made|set|give|use|put|in|to|of|for|with|and|it|should|be|look|looks|like|please|pls|"
    r"title|titles|subtitle|heading|headings|level|section|sub|table|header|headers|row|rows|body|text|paragraph|paragraphs|"
    r"bullet|bullets|points|caption|captions|column|columns|kpi|kpis|slide|slides|chart|legend|axis|labels?|footer|callout|"
    r"total|totals|cells?|values?|numbers?|first|main|document|doc|sheet|me|ko|karo|kar|do|thoda|zara"
))


def _leftover_words(clause: str, tgt: Optional["_Target"]) -> bool:
    """Words in a targeted clause that are neither element words nor
    fillers: a style description the parser did not understand."""
    rest = _TARGET_WORD_RE.sub(" ", clause)
    rest = re.sub(r"[^\w\u0900-\u0aff]+", " ", rest).strip()
    if tgt is not None and tgt.target is not None and tgt.target.text:
        rest = rest.replace(tgt.target.text.casefold(), " ").strip()
    if tgt is not None and tgt.target is not None and tgt.target.name:
        rest = rest.replace(tgt.target.name.casefold(), " ").strip()
    if not rest or len(rest.split()) > 4:
        return False
    return not _CONTENT_VERB_RE.search(clause)


_CONTENT_VERB_RE = re.compile(_w(
    r"add|adds|remove|delete|drop|insert|include|summari[sz]e|create|write|rewrite|list|update|explain|show|move|rename|"
    r"sort|filter|merge|split|calculate|compute|count|fill in|replace|translate|jodo|hatao|banao|likho|जोड़ो|हटाओ|ઉમેરો|કાઢો"
))


def strip_style_clauses(text: str) -> str:
    """The request without its style clauses — the content ask alone. A
    local fallback for lexicon.strip_style_clauses (intent track)."""
    kept = []
    for piece in re.split(r"(?<=[.;\n])\s+|,\s+(?!\d)", text or ""):
        if not piece.strip():
            continue
        patch, unparsed = parse_style_request(piece)
        if patch.is_empty() and not unparsed:
            kept.append(piece)
    return " ".join(kept).strip()


def patch_fields(patch: StylePatch) -> Dict[str, Any]:
    """A flat view of a patch for comparing parses field by field:
    'preset', 'page.orientation', 'fonts.body', 'base_size_pt',
    'rule:<target key>:<property>', 'cond:<column>:<op>:<value>:<property>',
    'scale:<column>', 'banded', ..."""
    out: Dict[str, Any] = {}
    for name in ("preset", "base_size_pt", "banded", "freeze_header", "auto_status_colors", "auto_score_scale"):
        v = getattr(patch, name)
        if v is not None:
            out[name] = v
    for group in ("page", "fonts", "header_footer", "colors"):
        g = getattr(patch, group)
        if g is not None:
            for k, v in g.model_dump().items():
                if v is not None:
                    out[f"{group}.{k}"] = v
    for r in patch.rules:
        for k, v in r.style.set_fields().items():
            out[f"rule:{r.target.key()}:{k}"] = v
    for c in patch.conditional:
        val = c.value if not isinstance(c.value, str) else c.value.casefold()
        for k, v in c.style.set_fields().items():
            if k == "color" and "background" in c.style.set_fields():
                continue  # the system's readable text colour on a fill
            out[f"cond:{_fold(c.column)}:{c.op}:{val}:{k}"] = v
        if c.whole_row:
            out[f"cond:{_fold(c.column)}:{c.op}:{val}:whole_row"] = True
    for s in patch.scales:
        out[f"scale:{_fold(s.column)}"] = True
    for c in patch.clear:
        for prop in c.props:
            out[f"clear:{c.target.key()}:{prop}"] = True
    if patch.reset:
        out["reset"] = True
    return out


# ============================================== residual phrases → model ==

_PATCH_LLM_SCHEMA: Dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "rules": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "target": {"type": "object", "additionalProperties": False, "properties": {
                    "kind": {"type": "string", "enum": list(TARGET_KINDS)},
                    "level": {"type": "integer", "minimum": 1, "maximum": 3},
                    "text": {"type": "string", "maxLength": 120},
                    "name": {"type": "string", "maxLength": 80},
                    "index": {"type": "integer", "minimum": 1},
                    "a1": {"type": "string", "maxLength": 24},
                }, "required": ["kind"]},
                "style": {"type": "object", "additionalProperties": False, "properties": {
                    "font_family": {"type": "string", "maxLength": 40},
                    "size_pt": {"type": "number", "minimum": 6, "maximum": 72},
                    "bold": {"type": "boolean"}, "italic": {"type": "boolean"}, "underline": {"type": "boolean"},
                    "color": {"type": "string", "maxLength": 20}, "background": {"type": "string", "maxLength": 20},
                    "align": {"type": "string", "enum": ["left", "center", "right", "justify"]},
                }},
            }, "required": ["target", "style"]}},
        "orientation": {"type": "string", "enum": ["portrait", "landscape", ""]},
        "preset": {"type": "string", "enum": ["classic", "modern", "minimal", "boardroom", "teal", ""]},
        "body_font": {"type": "string", "maxLength": 40},
        "heading_font": {"type": "string", "maxLength": 40},
        # AS3 integration: value conditions and colour scales ("Status red
        # where Blocked", "negative growth in red", "a colour scale on Score")
        # — before, the model had no way to say them and wrote prose instead.
        "conditional": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "column": {"type": "string", "maxLength": 80},
                "op": {"type": "string", "enum": ["eq", "ne", "contains", "gt", "gte", "lt", "lte"]},
                "value": {"type": ["string", "number"]},
                "color": {"type": "string", "maxLength": 20}, "background": {"type": "string", "maxLength": 20},
                "bold": {"type": "boolean"}, "whole_row": {"type": "boolean"},
            }, "required": ["column", "op", "value"]}},
        "scales": {"type": "array", "maxItems": 4, "items": {"type": "object", "additionalProperties": False,
                                                              "properties": {"column": {"type": "string", "maxLength": 80}}, "required": ["column"]}},
        "not_understood": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 120}},
    },
    "required": ["rules", "not_understood"],
}

_PATCH_LLM_SYSTEM = (
    "You translate a person's request about how a file should LOOK into a JSON style patch. "
    "Only styling: fonts, sizes, bold/italic/underline, text and background colours, alignment, orientation. "
    "Targets are element kinds: title, subtitle, heading (level 1-3 or a heading text), paragraph, bullet, table_header, "
    "table_body, table_total, column (name), row (index), cell_range (a1, spreadsheets), kpi_value, kpi_label, callout, "
    "caption, slide_title, slide_body, header_footer, chart_title, chart_axis, chart_legend, chart_labels. "
    "Colours are ALWAYS #RRGGBB hex codes (wine -> #722F37, charcoal -> #36454F). Sizes are points. "
    "A colour that depends on a cell's VALUE is a conditional entry (column, op, value; negative numbers: op lt, value 0), "
    "and a colour scale on a numeric column is a scales entry; use the column names from the outline. "
    "Put the ORIGINAL phrase you cannot express in not_understood, never an explanation. "
    "Never invent a target the phrase does not name. Output JSON only."
)


async def extract_patch_llm(phrases: Sequence[str], kind: str, outline: str = "", *, effort: Optional[str] = None,
                            completion: Optional[Callable[..., Any]] = None, timeout_s: float = 5.0) -> Tuple[StylePatch, List[str]]:
    """ONE JSON call (thinking off, max_tokens 300, 5 s) over the style
    phrases the parser could not read. Returns (patch, notes): every rule
    the model wrote is validated on its own — an unknown colour or font or
    an impossible target is dropped with a note, never guessed. Empty input
    makes no call. `completion` is injectable for tests (defaults to
    app.llm.json_completion)."""
    import asyncio
    import json as _json

    phrases = [p.strip()[:300] for p in phrases if p and p.strip()][:8]
    if not phrases:
        return StylePatch(), []
    if completion is None:
        from .. import llm  # lazy: keeps this module importable without the app

        completion = llm.json_completion
    user = f"File kind: {kind}.\n" + (f"Outline: {outline[:800]}\n" if outline else "") + "Phrases:\n" + "\n".join(f"- {p}" for p in phrases)
    messages = [{"role": "system", "content": _PATCH_LLM_SYSTEM}, {"role": "user", "content": user}]
    notes: List[str] = []
    try:
        # asyncio.timeout, not wait_for: on Python 3.11 (CI) wait_for can
        # swallow a cancellation that lands in the same pass as completion.
        async with asyncio.timeout(timeout_s):
            raw = await completion(messages, json_schema=_PATCH_LLM_SCHEMA, schema_name="style_patch", temperature=0.0, max_tokens=300, thinking=False, effort=effort)
    except Exception as exc:  # noqa: BLE001 — a failed call leaves the phrases unapplied
        return StylePatch(), [f"The style phrase{'s' if len(phrases) != 1 else ''} {', '.join(repr(p) for p in phrases)} could not be read ({type(exc).__name__})."]
    try:
        obj = _json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", raw or "", re.S) if isinstance(raw, str) else None
        try:
            obj = _json.loads(m.group(0)) if m else {}
        except Exception:  # noqa: BLE001
            obj = {}
    return patch_from_llm_object(obj, kind, notes)


def patch_from_llm_object(obj: Any, kind: str, notes: Optional[List[str]] = None) -> Tuple[StylePatch, List[str]]:
    """Validate a model's style object rule by rule (see extract_patch_llm)."""
    notes = notes if notes is not None else []
    if not isinstance(obj, dict):
        return StylePatch(), notes
    rules: List[StyleRule] = []
    for raw in (obj.get("rules") or [])[:8]:
        try:
            if not isinstance(raw, dict):
                raise ValueError("not an object")
            target = dict(raw.get("target") or {})
            style = {k: v for k, v in dict(raw.get("style") or {}).items() if v not in (None, "")}
            if kind == "presentation" and target.get("kind") == "heading":
                target = {"kind": "slide_title"}
            rules.append(StyleRule(target=StyleTarget.model_validate({k: v for k, v in target.items() if v not in (None, "")}), style=TextStyle.model_validate(style)))
        except Exception as exc:  # noqa: BLE001
            detail = "; ".join(str(e.get("msg", "")).removeprefix("Value error, ") for e in exc.errors()[:2]) if isinstance(exc, ValidationError) else str(exc).splitlines()[0]
            notes.append(f"A style instruction was not applied: {detail[:160]}.")
    data: Dict[str, Any] = {"rules": [r for r in rules if not r.style.is_empty()]}
    if obj.get("orientation") in ("portrait", "landscape"):
        data["page"] = PageStyle(orientation=obj["orientation"])
    if obj.get("preset") in ("classic", "modern", "minimal", "boardroom", "teal"):
        data["preset"] = obj["preset"]
    fonts: Dict[str, str] = {}
    for key, slot in (("body_font", "body"), ("heading_font", "heading")):
        if obj.get(key):
            face = font_face(obj[key])
            if face:
                fonts[slot] = face.name
            else:
                notes.append(f"The font {str(obj[key])[:40]!r} is not available; the house font was kept.")
    if fonts:
        data["fonts"] = FontSpec(**fonts)
    conds: List[CondRule] = []
    for raw in (obj.get("conditional") or [])[:8]:
        try:
            st = {k: raw.get(k) for k in ("color", "background", "bold") if isinstance(raw, dict) and raw.get(k) not in (None, "")}
            value = raw.get("value")
            if raw.get("op") in ("gt", "gte", "lt", "lte") and isinstance(value, str):
                value = float(value.replace(",", "").rstrip("%"))
            conds.append(CondRule(column=str(raw.get("column") or ""), op=raw.get("op") or "eq", value=value,
                                  style=TextStyle.model_validate(st), whole_row=bool(raw.get("whole_row"))))
        except Exception as exc:  # noqa: BLE001
            detail = "; ".join(str(e.get("msg", "")).removeprefix("Value error, ") for e in exc.errors()[:2]) if isinstance(exc, ValidationError) else str(exc).splitlines()[0]
            notes.append(f"A conditional colour was not applied: {detail[:160]}.")
    if conds:
        data["conditional"] = conds
    scales = [ColorScale(column=str(x["column"])[:80]) for x in (obj.get("scales") or [])[:4] if isinstance(x, dict) and str(x.get("column") or "").strip()]
    if scales:
        data["scales"] = scales
    for phrase in (obj.get("not_understood") or [])[:8]:
        # AS3 integration (live 2026-09-15): the model wrote explanations
        # here ("The request implies conditional formatting…"), which became
        # four "Not applied:" lines in the answer. Only a short phrase is said.
        if isinstance(phrase, str) and phrase.strip() and len(phrase.split()) <= 8 and not re.search(r"[.;]\s+\S", phrase.strip()):
            notes.append(f"Not applied: {phrase.strip()[:120]!r}.")
    return StylePatch.model_validate(data), notes


# ==================================================== formats and notes ==

CSV_STYLE_SENTENCE = "The CSV carries the data only; the formatting is in the Excel file."


def formats_with_style(formats: Sequence[str], *, kind: str, styled: bool) -> Tuple[List[str], List[str]]:
    """The formats to render when styling was asked for: a CSV cannot carry
    styling, so a styled request whose formats include csv but not xlsx
    ALSO gets the xlsx, with the one sentence that says so."""
    out = list(dict.fromkeys(formats))
    notes: List[str] = []
    if styled and kind == "workbook" and "csv" in out and "xlsx" not in out:
        out.append("xlsx")
        notes.append(CSV_STYLE_SENTENCE)
    return out, notes


def apply_request(spec: Any, text: str, *, formats: Sequence[str] = ()) -> Tuple[Any, List[str], List[str], List[str]]:
    """Parse `text` and merge the patch into spec.style (creating it), then
    normalise against the spec. Returns (spec, formats, notes, unparsed
    phrases). A request with no style content leaves spec.style as it was."""
    body = getattr(spec, "body", spec)
    kind = getattr(spec, "kind", None) or ("workbook" if hasattr(body, "sheets") else "presentation" if hasattr(body, "slides") else "document")
    patch, unparsed, notes = parse_style_request_with_notes(text, kind)
    styled = not patch.is_empty()
    if styled:
        body.style = merge(getattr(body, "style", None), patch)
        _, more = normalize_spec_style(spec)
        notes.extend(more)
    out_formats, fnotes = formats_with_style(formats, kind=kind, styled=styled)
    return spec, out_formats, notes + fnotes, unparsed


__all__ = [
    "Color", "COLOR_NAMES", "TEXT_SAFE", "resolve_color", "colour_name", "contrast_ratio", "readable_on", "luminance",
    "FontFace", "GENERIC_FAMILIES", "METRIC_TWINS", "FONT_ALLOWLIST", "font_face", "TextStyle", "StyleTarget", "StyleRule", "CondRule", "ColorScale",
    "PageStyle", "FontSpec", "ColorTokens", "HeaderFooter", "StyleSpec", "StylePatch", "ClearRule", "merge", "parse_style_request",
    "parse_style_request_with_notes", "extract_patch_llm", "patch_from_llm_object", "normalize_spec_style", "resolve",
    "ResolvedStyle", "ResolvedPage", "Tokens", "PRESETS", "STATUS_PAIRS", "STATUS_VALUES", "CHART_PALETTE",
    "ChartStyleDefaults", "support_matrix", "warnings_for", "requested_pairs", "xlsx_formula_literal",
    "xlsx_search_literal", "xlsx_header_footer_text", "clip_a1", "parse_a1", "col_index", "text_matches", "patch_fields",
    "strip_style_clauses", "formats_with_style", "apply_request", "CSV_STYLE_SENTENCE", "STATUS_COLUMN_RE",
    "SCORE_COLUMN_RE", "DUE_COLUMN_RE", "SCORE_SCALE", "INK", "WHITE", "columns_holding_value", "column_holds_value",
    "rebind_condition_column", "totalable_columns",
]

#: The interface names `Color` as a type: a validated `#RRGGBB` string.
Color = str
