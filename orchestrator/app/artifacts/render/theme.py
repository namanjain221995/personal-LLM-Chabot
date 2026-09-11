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

FONTS. The orchestrator images install fonts-dejavu-core and fonts-liberation
only (Dockerfile.cuda / Dockerfile.cpu). Liberation Sans and Liberation Serif
are metric-compatible with Arial and Times New Roman, so a DOCX or PPTX that
names Arial opens identically on a Windows desktop and renders with the same
line breaks in the container's PDF preview. Indic, CJK, Arabic and emoji
glyphs are NOT covered in the image; `unsupported_scripts()` finds them so
the render can carry a warning instead of failing or shipping tofu silently.

TYPE SCALE. Body text is 11 pt in documents and 10.5 pt in the tight brief —
never smaller; a 9 pt report is the single most common complaint about
generated documents. Slides never go below 14 pt for body text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

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

#: Callout tints: (border/accent colour, background) per callout kind.
CALLOUT_COLOURS: Dict[str, Tuple[str, str]] = {
    "note": (ACCENT, ACCENT_SOFT),
    "tip": (OK, "#e6f4ea"),
    "warning": (WARN, "#fbf1e0"),
    "quote": (SLATE, PAPER),
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


DOCUMENT_TYPE = TypeScale(body=11, small=9.5, caption=9, h1=20, h2=15, h3=12.5, title=30, subtitle=14, kpi_value=22)
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
    """Script names present in `text` that the image's fonts do not cover,
    in first-seen order. Empty for Latin, Greek-extended, punctuation,
    currency and arrows — everything DejaVu/Liberation draw."""
    seen: List[str] = []
    for ch in _NON_LATIN_RE.findall(text or ""):
        code = ord(ch)
        for name, lo, hi in _SCRIPT_RANGES:
            if lo <= code <= hi and name not in seen:
                seen.append(name)
                break
    return seen


def font_coverage_warning(text: str) -> str:
    """The one-sentence warning for a spec that uses scripts the container
    cannot draw, or '' when it uses none."""
    missing = unsupported_scripts(text)
    if not missing:
        return ""
    return (
        f"The text uses {', '.join(missing)} characters; the PDF preview may show them as boxes "
        "because the server's fonts cover Latin scripts only. The DOCX/PPTX/XLSX files keep the text intact."
    )


def series_colour(index: int) -> str:
    return PALETTE[index % len(PALETTE)]


def hex_to_rgb(colour: str) -> Tuple[int, int, int]:
    c = colour.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


__all__ = [
    "PALETTE", "INK", "INK_MUTED", "INK_FAINT", "SURFACE", "SURFACE_2", "BORDER", "ACCENT",
    "ACCENT_STRONG", "ACCENT_SOFT", "NAVY", "BOARDROOM", "SLATE", "PAPER", "TEAL", "DANGER", "WARN", "OK",
    "WHITE", "CALLOUT_COLOURS", "FONT_SANS", "FONT_SERIF", "FONT_MONO", "CSS_SANS", "CSS_SERIF", "CSS_MONO",
    "OFFICE_SANS", "OFFICE_SERIF", "TypeScale", "DOCUMENT_TYPE", "BRIEF_TYPE", "PageGrid", "DOCUMENT_GRID",
    "BRIEF_GRID", "SLIDE_W_IN", "SLIDE_H_IN", "SLIDE_MARGIN_IN", "SLIDE_TITLE_TOP_IN", "SLIDE_TITLE_H_IN",
    "SLIDE_BODY_TOP_IN", "SLIDE_BODY_H_IN", "SLIDE_FOOTER_TOP_IN", "SLIDE_FOOTER_H_IN", "SlideType",
    "SLIDE_TYPE", "CEO_SLIDE_TYPE", "SLIDE_MIN_BODY_PT", "PRESENTATION_TEMPLATES", "DOCUMENT_TEMPLATES",
    "CHART_DPI", "CHART_FIGSIZE", "CHART_LABEL_MAX_CATEGORIES", "unsupported_scripts",
    "font_coverage_warning", "series_colour", "hex_to_rgb",
]
