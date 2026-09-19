"""The TechSara design tokens, for print and for slides.

WHY A THEME MODULE. Four renderers (HTML→PDF, DOCX, PPTX, XLSX) and one chart
painter must agree on colour, type and geometry, or a deck's PDF preview
shows a different blue from its .pptx and a chart in the .docx matches
nothing on screen. Every number they share lives here and nowhere else.

WHERE THE VALUES COME FROM. The categorical palette is the app's own
(`--ts-chart-1..5` in frontend/app/globals.css, the same five colours
core/charts_png.py paints reports with, in the same fixed order) — the light-
surface variant, since paper is white. Ink, muted, surface, border and accent
are the LIGHT theme's semantic tokens (`html.light` in globals.css: text
#0d0d0d, muted #5d5d5d, faint #8f8f8f, surface #f4f4f4, border #e3e3e3,
accent #1d4ed8 "deepened for AA on paper"); navy/boardroom/slate/paper/teal
are the brand constants. Nothing here is borrowed from another product.

FONTS. The orchestrator images (Dockerfile.cuda / Dockerfile.cpu) install
open-licence fonts only: fonts-liberation + fonts-liberation2 (metric twins of
Arial/Helvetica, Times New Roman and Courier New), fonts-crosextra-carlito and
-caladea (metric twins of Calibri and Cambria, the TechSara Classic families),
fonts-dejavu-core, and fonts-noto-core + fonts-lohit-deva/gujr for Devanagari
and Gujarati. Office files name the family a person asked for
(artifacts/style.FONT_ALLOWLIST). What the server draws with — the PDF and
chart images — is `resolve_font(face)`: the family itself when fontconfig
resolves it, else its metric twin, else the face's documented open fallback
(Georgia -> Caladea: Gelasio, Georgia's metric twin, is packaged for neither
base image), else Liberation/DejaVu. `pdf_font_stack()` puts that family
first in the PDF stylesheet; the render warnings and the self-check name it.
`font_installed()` asks fc-match once per family. `uncovered_scripts()` finds
text no installed font draws, so the render carries one warning instead of
shipping boxes silently.

STYLE TOKENS. The TechSara Classic palette, presets and contrast policy live
in artifacts/style.py (the resolver every renderer reads); the CLASSIC_*
names below re-export its default values. The older constants (NAVY, SURFACE,
PALETTE ...) stay for the chart painter and legacy callers: PALETTE must
match core/charts_png.py until the charts track moves to
ResolvedStyle.chart_defaults.

TYPE SCALE. Body text is 11 pt in documents and 10.5 pt in the tight brief —
never smaller; a 9 pt report is the single most common complaint about
generated documents. Slides never go below 14 pt for body text.
"""
from __future__ import annotations

import functools
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------- colours --

#: Series colours, fixed assignment order (teal → blue → amber → violet →
#: rose). Identical to charts_png.py and frontend/lib/chartTheme.ts.
PALETTE: Tuple[str, ...] = ("#0e9d9a", "#2f6fb2", "#b7791f", "#6d5ae6", "#c0566b")

INK = "#0d0d0d"          # --ts-text (light)
INK_MUTED = "#5d5d5d"    # --ts-text-muted (light)
INK_FAINT = "#8f8f8f"    # --ts-text-faint (light)
SURFACE = "#f4f4f4"      # --ts-surface (light)
SURFACE_2 = "#ececec"    # --ts-surface-2 (light)
BORDER = "#e3e3e3"       # --ts-border (light)
ACCENT = "#1d4ed8"       # --ts-accent (light, AA on paper)
ACCENT_STRONG = "#2563eb"
ACCENT_SOFT = "#e4ebfb"  # --ts-accent-soft (rgba .12 over white, flattened for Office)
NAVY = "#0a1d37"         # --ts-navy
BOARDROOM = "#143a66"    # --ts-boardroom
SLATE = "#5b6b7f"        # --ts-slate
PAPER = "#f6f8fb"        # --ts-paper
TEAL = "#0e9f9a"         # --ts-teal
DANGER = "#c62a30"       # --ts-danger (light)
WARN = "#b26a00"         # --ts-warn (light)
OK = "#1a7f37"           # --ts-ok (light)
WHITE = "#ffffff"

#: Callout tints: (left-bar/title colour, background) per callout kind —
#: the style guide's status pairs (info, success, warning, neutral), each
#: text colour >= 5.7:1 on its fill.
CALLOUT_COLOURS: Dict[str, Tuple[str, str]] = {
    "note": ("#1F4E79", "#E3EDF8"),
    "tip": ("#1E6B34", "#E3F2E6"),
    "warning": ("#7A4F00", "#FFF1C7"),
    "quote": ("#374151", "#EEF0F3"),
}

# ----------------------------------------------------------------- fonts --

#: Family names as Office files and CSS name them. Liberation Sans is
#: metric-compatible with Arial; the stack falls back to what any desktop has.
FONT_SANS = "Liberation Sans"
FONT_SERIF = "Liberation Serif"
FONT_MONO = "Liberation Mono"
CSS_SANS = '"Liberation Sans", Arial, Helvetica, "DejaVu Sans", sans-serif'
CSS_SERIF = '"Liberation Serif", "Times New Roman", Times, "DejaVu Serif", serif'
CSS_MONO = '"Liberation Mono", "Courier New", "DejaVu Sans Mono", monospace'
#: What the Office writers put in the file: the metric twin a desktop has.
OFFICE_SANS = "Arial"
OFFICE_SERIF = "Times New Roman"

# ------------------------------------------------------------ type scale --


@dataclass(frozen=True)
class TypeScale:
    """Point sizes for one document density."""

    body: float
    small: float
    caption: float
    h1: float
    h2: float
    h3: float
    title: float
    subtitle: float
    kpi_value: float
    line_height: float = 1.42


#: TechSara Classic (style guide §2): title 28, H1 18, H2 14, H3 12, body 11
#: at 1.25 line height, tables 10, captions 9.
DOCUMENT_TYPE = TypeScale(body=11, small=10, caption=9, h1=18, h2=14, h3=12, title=28, subtitle=14, kpi_value=22, line_height=1.25)
#: The one-page brief: tighter, but body never below 10.5 pt.
BRIEF_TYPE = TypeScale(body=10.5, small=9, caption=8.5, h1=16, h2=13, h3=11.5, title=22, subtitle=12, kpi_value=20, line_height=1.35)

# ------------------------------------------------------------- page grid --


@dataclass(frozen=True)
class PageGrid:
    """Margins in millimetres and the running header/footer rules."""

    top_mm: float
    bottom_mm: float
    left_mm: float
    right_mm: float
    header: bool
    footer: bool


DOCUMENT_GRID = PageGrid(top_mm=22, bottom_mm=20, left_mm=20, right_mm=20, header=True, footer=True)
BRIEF_GRID = PageGrid(top_mm=16, bottom_mm=16, left_mm=16, right_mm=16, header=False, footer=True)

# ------------------------------------------------------------ slide grid --

#: 16:9 at PowerPoint's default widescreen size. The HTML preview pages and
#: the .pptx shapes both use these, in inches, so the preview IS the deck.
SLIDE_W_IN = 13.333
SLIDE_H_IN = 7.5
SLIDE_MARGIN_IN = 0.5
SLIDE_TITLE_TOP_IN = 0.45
SLIDE_TITLE_H_IN = 1.0
SLIDE_BODY_TOP_IN = 1.6
SLIDE_BODY_H_IN = 5.1
SLIDE_FOOTER_TOP_IN = 6.95
SLIDE_FOOTER_H_IN = 0.35


@dataclass(frozen=True)
class SlideType:
    title: float          # slide title, pt
    cover_title: float    # the deck title on the title slide
    body: float           # bullets, table cells, captions — never below 14
    small: float          # footer, sources
    kpi_value: float
    kpi_label: float
    section: float


SLIDE_TYPE = SlideType(title=32, cover_title=40, body=18, small=11, kpi_value=44, kpi_label=16, section=36)
#: CEO decks: big numbers, few words.
CEO_SLIDE_TYPE = SlideType(title=34, cover_title=44, body=20, small=11, kpi_value=60, kpi_label=18, section=40)

#: The floor the PPTX writer and the HTML preview never go under.
SLIDE_MIN_BODY_PT = 14

# ------------------------------------------------------------- templates --

#: What a presentation template changes: bullet cap per slide, characters
#: per bullet, type scale, and the colour of the title/section slides.
PRESENTATION_TEMPLATES: Dict[str, dict] = {
    "ceo": {"max_bullets": 4, "max_bullet_chars": 90, "type": CEO_SLIDE_TYPE, "band": NAVY},
    "training": {"max_bullets": 6, "max_bullet_chars": 140, "type": SLIDE_TYPE, "band": TEAL},
    "quarterly_review": {"max_bullets": 6, "max_bullet_chars": 120, "type": SLIDE_TYPE, "band": BOARDROOM},
    "generic": {"max_bullets": 8, "max_bullet_chars": 160, "type": SLIDE_TYPE, "band": BOARDROOM},
}

#: What a document template changes. `cover`/`toc` here are FORCED values;
#: None leaves the spec's own flag in charge. `hoist_kpis` moves the first
#: KPI row to the top (summary-first). `numbered_headings` puts 1. / 1.1 on
#: headings. `control_strip` prints author/date/audience as a table at the
#: top. `warn_pages` records a warning when the PDF runs past that many pages.
DOCUMENT_TEMPLATES: Dict[str, dict] = {
    "executive_report": {"cover": True, "toc": None, "hoist_kpis": True, "lede": True, "numbered_headings": False, "control_strip": False, "type": DOCUMENT_TYPE, "grid": DOCUMENT_GRID, "warn_pages": None},
    "brief": {"cover": False, "toc": False, "hoist_kpis": True, "lede": False, "numbered_headings": False, "control_strip": False, "type": BRIEF_TYPE, "grid": BRIEF_GRID, "warn_pages": 2},
    "sop": {"cover": None, "toc": None, "hoist_kpis": False, "lede": False, "numbered_headings": True, "control_strip": True, "type": DOCUMENT_TYPE, "grid": DOCUMENT_GRID, "warn_pages": None},
    "technical_report": {"cover": None, "toc": None, "hoist_kpis": False, "lede": False, "numbered_headings": True, "control_strip": False, "type": DOCUMENT_TYPE, "grid": DOCUMENT_GRID, "warn_pages": None},
    "research_report": {"cover": None, "toc": None, "hoist_kpis": False, "lede": True, "numbered_headings": False, "control_strip": False, "type": DOCUMENT_TYPE, "grid": DOCUMENT_GRID, "warn_pages": None},
    "proposal": {"cover": True, "toc": None, "hoist_kpis": False, "lede": True, "numbered_headings": True, "control_strip": False, "type": DOCUMENT_TYPE, "grid": DOCUMENT_GRID, "warn_pages": None},
    "meeting_summary": {"cover": False, "toc": False, "hoist_kpis": False, "lede": False, "numbered_headings": False, "control_strip": True, "type": DOCUMENT_TYPE, "grid": DOCUMENT_GRID, "warn_pages": None},
    "generic": {"cover": None, "toc": None, "hoist_kpis": False, "lede": False, "numbered_headings": False, "control_strip": False, "type": DOCUMENT_TYPE, "grid": DOCUMENT_GRID, "warn_pages": None},
}

# ---------------------------------------------------------------- charts --

CHART_DPI = 160
CHART_FIGSIZE = (8.0, 4.5)
#: Value labels on bars only up to this many categories — past it they overlap.
CHART_LABEL_MAX_CATEGORIES = 12

# ------------------------------------------------------ script coverage --

#: Unicode ranges the container's two font families cannot draw. Ranges are
#: coarse on purpose: the question is "will this render as boxes", not the
#: exact script.
_SCRIPT_RANGES: Tuple[Tuple[str, int, int], ...] = (
    ("Arabic", 0x0600, 0x06FF),
    ("Hebrew", 0x0590, 0x05FF),
    ("Devanagari", 0x0900, 0x097F),
    ("Bengali", 0x0980, 0x09FF),
    ("Gurmukhi", 0x0A00, 0x0A7F),
    ("Gujarati", 0x0A80, 0x0AFF),
    ("Odia", 0x0B00, 0x0B7F),
    ("Tamil", 0x0B80, 0x0BFF),
    ("Telugu", 0x0C00, 0x0C7F),
    ("Kannada", 0x0C80, 0x0CFF),
    ("Malayalam", 0x0D00, 0x0D7F),
    ("Sinhala", 0x0D80, 0x0DFF),
    ("Thai", 0x0E00, 0x0E7F),
    ("CJK", 0x3040, 0x30FF),      # Hiragana + Katakana
    ("CJK", 0x4E00, 0x9FFF),      # unified ideographs
    ("CJK", 0xAC00, 0xD7AF),      # Hangul syllables
    ("Emoji", 0x1F300, 0x1FAFF),
)
#: Everything Liberation/DejaVu draw: control/Basic Latin through Latin
#: Extended-B, general punctuation, currency, letterlike symbols, arrows,
#: mathematical operators and geometric shapes.
_NON_LATIN_RE = re.compile(
    "[^\u0000-\u024f\u2000-\u206f\u20a0-\u20cf\u2100-\u214f\u2190-\u21ff\u2200-\u22ff\u25a0-\u25ff]"
)


def unsupported_scripts(text: str) -> List[str]:
    """Script names present in `text` that Liberation/DejaVu (the fonts the
    image has always carried) do not cover, in first-seen order. Empty for
    Latin, Greek-extended, punctuation, currency and arrows. Whether THIS
    server can draw them is `uncovered_scripts`, which asks fontconfig."""
    seen: List[str] = []
    for ch in _NON_LATIN_RE.findall(text or ""):
        code = ord(ch)
        for name, lo, hi in _SCRIPT_RANGES:
            if lo <= code <= hi and name not in seen:
                seen.append(name)
                break
    return seen


#: Families that draw a script, in preference order; the first installed
#: one is what WeasyPrint and matplotlib fall back to.
SCRIPT_FONTS: Dict[str, Tuple[str, ...]] = {
    "Devanagari": ("Noto Sans Devanagari", "Lohit Devanagari", "Nirmala UI", "Mangal"),
    "Gujarati": ("Noto Sans Gujarati", "Lohit Gujarati", "Shruti"),
    "Bengali": ("Noto Sans Bengali", "Lohit Bengali"),
    "Gurmukhi": ("Noto Sans Gurmukhi", "Lohit Gurmukhi"),
    "Odia": ("Noto Sans Oriya", "Lohit Odia"),
    "Tamil": ("Noto Sans Tamil", "Lohit Tamil"),
    "Telugu": ("Noto Sans Telugu", "Lohit Telugu"),
    "Kannada": ("Noto Sans Kannada", "Lohit Kannada"),
    "Malayalam": ("Noto Sans Malayalam", "Lohit Malayalam"),
    "CJK": ("Noto Sans CJK SC", "Noto Sans CJK JP", "WenQuanYi Zen Hei"),
    "Arabic": ("Noto Sans Arabic", "DejaVu Sans"),
    "Hebrew": ("Noto Sans Hebrew", "DejaVu Sans"),
    "Thai": ("Noto Sans Thai",),
    "Sinhala": ("Noto Sans Sinhala",),
    "Emoji": ("Noto Color Emoji",),
}


@functools.lru_cache(maxsize=256)
def font_installed(family: str) -> bool:
    """Does fontconfig resolve `family` to ITSELF (not a fallback)? Asked
    once per family per process with `fc-match`; False when fontconfig is
    absent. The family name comes from the allowlist or SCRIPT_FONTS, never
    from a request, and is passed as an argv element, never a shell string."""
    if not family or len(family) > 60 or not re.fullmatch(r"[A-Za-z0-9 ._-]+", family):
        return False
    exe = shutil.which("fc-match")
    if exe is None:
        return False
    try:
        out = subprocess.run([exe, "-f", "%{family}", family], capture_output=True, text=True, timeout=3, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    names = [n.strip().casefold() for n in (out or "").split(",")]
    return family.casefold() in names


def installed_family(candidates: Sequence[str]) -> str:
    """The first installed family of `candidates`, or ''."""
    for name in candidates:
        if font_installed(name):
            return name
    return ""


@dataclass(frozen=True)
class FontChoice:
    """The family this server draws a requested font with, and why.

    kind: "exact" (the family itself is installed), "metric" (its metric-
    compatible twin: Carlito for Calibri, Liberation Sans for Arial ...),
    "substitute" (the face's declared substitute that is not a metric twin),
    "fallback" (the documented open fallback the images carry when no twin
    is packaged, e.g. Caladea for Georgia), "generic" (Liberation/DejaVu of
    the same generic class) or "none" (fontconfig absent or nothing found)."""

    requested: str
    family: str
    kind: str

    @property
    def substituted(self) -> bool:
        return self.kind != "exact"

    def sentence(self) -> str:
        """'Georgia was drawn with Caladea, its documented fallback' — or ''
        when the family itself was used."""
        if self.kind == "exact" or not self.family:
            return ""
        why = {"metric": "its metric-compatible equivalent", "substitute": "its substitute",
               "fallback": "its documented open fallback",
               "generic": "the generic fallback"}.get(self.kind, "a fallback")
        return f"{self.requested} was drawn with {self.family}, {why}"


def resolve_font(face: Any) -> FontChoice:
    """Which installed family renders `face` (a style.FontFace) on this
    server: its `pdf_candidates` in order, asked of fontconfig. The one
    mapping the PDF stylesheet, the chart painter, the render warnings and
    the self-check all read."""
    name = str(getattr(face, "name", "") or "")
    for family in tuple(getattr(face, "pdf_candidates", ()) or ()):
        if not font_installed(family):
            continue
        if family in tuple(getattr(face, "installed_file_candidates", ()) or ()):
            kind = "exact"
        elif family == getattr(face, "metric_substitute", None):
            kind = "metric" if getattr(face, "metric_compatible", False) else "substitute"
        elif family == getattr(face, "fallback", None):
            kind = "fallback"
        else:
            kind = "generic"
        return FontChoice(name, family, kind)
    return FontChoice(name, "", "none")


def pdf_font_stack(face: Any) -> str:
    """The face's constant CSS stack with the family `resolve_font` picked
    first, so WeasyPrint draws exactly the mapped family instead of letting
    a fontconfig alias of the missing name ("Georgia" -> DejaVu Serif) jump
    the queue. Every name comes from the allowlist, never from a request."""
    stack = str(getattr(face, "css_stack", "") or CSS_SANS)
    chosen = resolve_font(face)
    if not chosen.family or chosen.kind == "exact" or not re.fullmatch(r"[A-Za-z0-9 ._-]+", chosen.family):
        return stack
    return f'"{chosen.family}", {stack}'


def uncovered_scripts(text: str) -> List[str]:
    """The scripts in `text` that no installed font on this server draws."""
    return [s for s in unsupported_scripts(text) if not installed_family(SCRIPT_FONTS.get(s, ()))]


def font_coverage_warning(text: str) -> str:
    """The one-sentence warning for a spec that uses scripts this server
    has no font for, or '' when every script is covered. Chart images and
    the PDF are drawn HERE, so they are the files that show boxes; the
    Office files carry the characters and display them on a computer with
    a font for the script."""
    missing = uncovered_scripts(text)
    if not missing:
        return ""
    return (
        f"The text uses {', '.join(missing)} characters and this server has no font for them, so the PDF and any chart "
        "images may show them as boxes; the Word, PowerPoint and Excel files keep the characters, which display on a "
        "computer that has a font for the script."
    )


# ------------------------------------------------------- TechSara Classic --
#
# The style guide's tokens (docs/artifact-studio/as3/styling-engine.md) live
# in artifacts/style.py, where the resolver applies presets, user tokens and
# the contrast policy; these names re-export the Classic values for code
# that wants a constant.

CLASSIC_PRIMARY = "#1F3864"
CLASSIC_ACCENT = "#2E5597"
CLASSIC_INK = "#1F2937"
CLASSIC_MUTED = "#5F6B7A"
CLASSIC_CAPTION = "#6B7280"
CLASSIC_HAIRLINE = "#D0D7E2"
CLASSIC_GRID = "#E5E9F0"
CLASSIC_BAND = "#F3F6FA"
CLASSIC_TOTAL_FILL = "#DCE6F2"
#: Office names and the container's metric twins (Carlito ≈ Calibri,
#: Caladea ≈ Cambria; Dockerfile.* install fonts-crosextra-carlito/caladea).
CLASSIC_BODY_FONT = "Calibri"
CLASSIC_HEADING_FONT = "Calibri"
CSS_CLASSIC = '"Calibri", "Carlito", "Liberation Sans", Arial, "DejaVu Sans", "Noto Sans Devanagari", "Lohit Devanagari", "Noto Sans Gujarati", "Lohit Gujarati", sans-serif'

_CURRENCY_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}


def indian_grouping(integer_digits: str) -> str:
    """'12000000' → '1,20,00,000' (lakh/crore grouping)."""
    if len(integer_digits) <= 3:
        return integer_digits
    head, tail = integer_digits[:-3], integer_digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def _number_text(value: float, decimals: int, *, indian: bool = False) -> str:
    text = f"{abs(value):,.{decimals}f}"
    if indian:
        whole, _, frac = f"{abs(value):.{decimals}f}".partition(".")
        text = indian_grouping(whole) + (f".{frac}" if frac else "")
    return ("-" if value < 0 and float(text.replace(",", "") or 0) != 0 else "") + text


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        for sym in _CURRENCY_SYMBOLS.values():
            s = s.replace(sym, "")
        s = s.rstrip("%").strip()
        if re.match(r"^\s*[-+]?0\d", s):
            return None
        try:
            f = float(s)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def format_cell(value: Any, column_format: Any = None, *, percent_scale: float = 1.0) -> str:
    """What a cell SHOWS in a Word/PDF table and the sheet preview, from the
    same rules the XLSX number formats follow: integers `12,345`; decimals
    two places (`#,##0` when integral); percent `12.5%` (a fraction ×100,
    or a whole percentage when `percent_scale` is 0.01); currency with its
    symbol (INR in lakh/crore grouping); dates `03-Aug-2026` (ISO when
    asked). `column_format` is a spec Column (its `type` and `format`), a
    column type string, or None (text). Text never changes."""
    import datetime as _dt

    if value is None:
        return ""
    ctype = column_format if isinstance(column_format, str) else getattr(column_format, "type", None) or "text"
    fmt = getattr(column_format, "format", None)
    kind = getattr(fmt, "kind", None) or {"integer": "integer", "number": "decimal", "currency": "currency", "percent": "percent", "date": "date"}.get(ctype, "text")
    decimals = getattr(fmt, "decimals", None)
    if kind == "text":
        return str(value)
    if kind in ("date", "datetime"):
        moment = None
        if isinstance(value, _dt.datetime):
            moment = value
        elif isinstance(value, _dt.date):
            moment = _dt.datetime.combine(value, _dt.time())
        elif isinstance(value, str):
            m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?", value.strip())
            if m:
                try:
                    moment = _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0), int(m.group(5) or 0))
                except ValueError:
                    moment = None
        if moment is None:
            return str(value)
        if getattr(fmt, "date_style", None) == "iso":
            return moment.strftime("%Y-%m-%d %H:%M" if kind == "datetime" else "%Y-%m-%d")
        return moment.strftime("%d-%b-%Y %H:%M" if kind == "datetime" else "%d-%b-%Y")
    number = _as_float(value)
    if number is None:
        return str(value)
    if kind == "integer":
        return _number_text(round(number), 0)
    if kind == "percent":
        pct = number * (100.0 if percent_scale == 1.0 else 1.0)
        return f"{_number_text(pct, 1 if decimals is None else decimals)}%"
    if kind == "currency":
        code = getattr(fmt, "currency", None)
        places = 2 if decimals is None else decimals
        text = _number_text(number, places, indian=code == "INR")
        symbol = _CURRENCY_SYMBOLS.get(code or "", "")
        return (f"-{symbol}{text[1:]}" if text.startswith("-") else f"{symbol}{text}")
    places = decimals if decimals is not None else (0 if abs(number - round(number)) < 1e-9 else 2)
    return _number_text(number, places)


def series_colour(index: int) -> str:
    return PALETTE[index % len(PALETTE)]


def hex_to_rgb(colour: str) -> Tuple[int, int, int]:
    c = colour.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


#: An Office core property holds at most 255 characters and python-docx /
#: python-pptx raise past it. The spec lets `purpose` reach 300: a
#: 283-character one failed the whole DOCX of "give Big report" (live run,
#: hotfix 1.1, 2026-09-19). The file's metadata is cut; its text is not.
CORE_PROPERTY_MAX = 255


def core_property(value: Optional[str]) -> str:
    return str(value or "")[:CORE_PROPERTY_MAX]


__all__ = [
    "PALETTE", "INK", "INK_MUTED", "INK_FAINT", "SURFACE", "SURFACE_2", "BORDER", "ACCENT",
    "ACCENT_STRONG", "ACCENT_SOFT", "NAVY", "BOARDROOM", "SLATE", "PAPER", "TEAL", "DANGER", "WARN", "OK",
    "WHITE", "CALLOUT_COLOURS", "FONT_SANS", "FONT_SERIF", "FONT_MONO", "CSS_SANS", "CSS_SERIF", "CSS_MONO",
    "OFFICE_SANS", "OFFICE_SERIF", "TypeScale", "DOCUMENT_TYPE", "BRIEF_TYPE", "PageGrid", "DOCUMENT_GRID",
    "BRIEF_GRID", "SLIDE_W_IN", "SLIDE_H_IN", "SLIDE_MARGIN_IN", "SLIDE_TITLE_TOP_IN", "SLIDE_TITLE_H_IN",
    "SLIDE_BODY_TOP_IN", "SLIDE_BODY_H_IN", "SLIDE_FOOTER_TOP_IN", "SLIDE_FOOTER_H_IN", "SlideType",
    "SLIDE_TYPE", "CEO_SLIDE_TYPE", "SLIDE_MIN_BODY_PT", "PRESENTATION_TEMPLATES", "DOCUMENT_TEMPLATES",
    "CHART_DPI", "CHART_FIGSIZE", "CHART_LABEL_MAX_CATEGORIES", "unsupported_scripts",
    "font_coverage_warning", "series_colour", "hex_to_rgb", "SCRIPT_FONTS", "font_installed", "installed_family",
    "FontChoice", "resolve_font", "pdf_font_stack",
    "uncovered_scripts", "format_cell", "indian_grouping", "CLASSIC_PRIMARY", "CLASSIC_ACCENT", "CLASSIC_INK",
    "CLASSIC_MUTED", "CLASSIC_CAPTION", "CLASSIC_HAIRLINE", "CLASSIC_GRID", "CLASSIC_BAND", "CLASSIC_TOTAL_FILL",
    "CLASSIC_BODY_FONT", "CLASSIC_HEADING_FONT", "CSS_CLASSIC", "CORE_PROPERTY_MAX", "core_property",
]
