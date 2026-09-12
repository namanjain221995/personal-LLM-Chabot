"""ArtifactSpec → a complete, self-contained HTML string for WeasyPrint.

TWO JOBS IN ONE MODULE, ON PURPOSE. The first is the obvious one: turn a
DocumentSpec into pages, or a PresentationSpec into 16:9 slide pages, with
print.css inlined and the @page rules generated from theme.py. The second is
the LAYOUT PLAN: which blocks go where, which slide falls back to bullets,
which bullets were trimmed to fit, which table rows fit a slide, which
heading gets which number, which source gets which citation number.
`plan_document()` and `plan_deck()` decide
that ONCE, and the DOCX and PPTX writers consume the same plan — so a .docx
reads block-for-block like its PDF, and a .pptx has exactly the slides the
preview PDF shows. A renderer that made its own layout decisions would drift
from the preview, and a preview that lies is worse than none.

ESCAPING. Every string that came from the model or a person passes through
`html.escape` here — titles, cells, captions, notes, URLs, the lot. Nothing
in the spec is markup and nothing is rendered as markup. Chart images are
referenced by BARE filename (`chart-3.png`); pdf.py's fetcher refuses
anything else, so a spec cannot name a URL even if a field slipped through.

TEMPLATES DIFFER IN STRUCTURE, NOT JUST TITLE. An executive report opens with
a cover and its KPI row hoisted to the top; a brief has no cover, tight
margins and a KPI band; an SOP prints a document-control strip and numbered
steps with big step markers; a research report ends with a sources page in
the heading style; a meeting summary renders lists as action items. The
per-template switches are the table in theme.DOCUMENT_TEMPLATES; the
rendering of each switch is here.
"""
from __future__ import annotations

import html as _html
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from .. import spec as S
from . import theme

e = _html.escape

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"



def _kpi_size_class(value: str) -> str:
    """A KPI value is a big number; a long one ("$59/month", "Existing Team
    Subscribers") must shrink, not wrap mid-token ("$59/" over "month" —
    seen on the first real brief, 2026-09-11). The step is by length;
    print.css sets the sizes and forbids breaking inside the value."""
    n = len(value or "")
    if n > 18:
        return " kpi-xs"
    if n > 10:
        return " kpi-sm"
    return ""


def print_css() -> str:
    """The first-party stylesheet, read from assets/print.css."""
    return (_ASSETS_DIR / "print.css").read_text(encoding="utf-8")


# --------------------------------------------------------------- numbers --


def format_number(value: Union[int, float, str, None]) -> str:
    """Thousands separators; integral floats print as integers; two
    decimals otherwise. Strings are returned as they are."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return str(value)
        if abs(value - round(value)) < 1e-9:
            return f"{int(round(value)):,}"
        return f"{value:,.2f}"
    return str(value)


def cell_text(value: Union[int, float, str, None], numeric: bool) -> str:
    """What a table cell shows. Only NUMERIC columns get thousands separators —
    a year in a text column must stay `2024`, not become `2,024`."""
    if value is None:
        return ""
    if numeric:
        return format_number(value)
    return str(value)


# ------------------------------------------------------------ chart walk --


def spec_charts(spec: S.ArtifactSpec) -> List[S.Chart]:
    """Every chart in the spec, in document order. The ordinal in this list
    (1-based) is the chart's file number: `chart-<n>.png`. Workbook charts
    are drawn natively by openpyxl and are not in this list."""
    body = spec.body
    out: List[S.Chart] = []
    if isinstance(body, S.DocumentSpec):
        out.extend(b.chart for b in body.blocks if isinstance(b, S.ChartBlock))
    elif isinstance(body, S.PresentationSpec):
        out.extend(s.chart for s in body.slides if s.chart is not None)
    return out


def chart_filename(ordinal: int) -> str:
    return f"chart-{int(ordinal)}.png"


# -------------------------------------------------------- document plan --


@dataclass
class PlannedHeading:
    index: int          # position in DocumentPlan.blocks
    level: int
    text: str
    anchor: str         # code-made id: h-<n>
    number: str         # "2.1" when the template numbers headings, else ""


@dataclass
class DocumentPlan:
    spec: S.DocumentSpec
    template_id: str
    template: dict
    cover: bool
    toc: bool
    numbered: bool
    control_strip: bool
    hoisted_kpis: Optional[S.KPIRow]
    blocks: List[S.DocumentBlock]
    headings: List[PlannedHeading]
    citation_index: Dict[str, int]
    lede_index: Optional[int]
    has_tail: bool
    warnings: List[str] = field(default_factory=list)

    @property
    def type(self) -> theme.TypeScale:
        return self.template["type"]

    @property
    def grid(self) -> theme.PageGrid:
        return self.template["grid"]


def citation_numbers(sources: Sequence[S.Citation]) -> Dict[str, int]:
    return {c.id: i + 1 for i, c in enumerate(sources)}


def plan_document(spec: S.DocumentSpec) -> DocumentPlan:
    """The layout decisions for one document, made once."""
    template_id = spec.template_id if spec.template_id in theme.DOCUMENT_TEMPLATES else "generic"
    template = theme.DOCUMENT_TEMPLATES[template_id]
    warnings: List[str] = []

    blocks: List[S.DocumentBlock] = list(spec.blocks)
    hoisted: Optional[S.KPIRow] = None
    if template["hoist_kpis"]:
        for i, b in enumerate(blocks):
            if isinstance(b, S.KPIRow):
                hoisted = b
                del blocks[i]
                break

    # Consecutive page breaks are one page break; a page break with nothing
    # after it in the flow is nothing at all (it would print a blank page).
    collapsed: List[S.DocumentBlock] = []
    for b in blocks:
        if isinstance(b, S.PageBreak) and (not collapsed or isinstance(collapsed[-1], S.PageBreak)):
            continue
        collapsed.append(b)
    has_tail = bool(spec.sources) or bool(spec.assumptions)
    if not has_tail:
        while collapsed and isinstance(collapsed[-1], S.PageBreak):
            collapsed.pop()
    blocks = collapsed

    cover = spec.cover if template["cover"] is None else template["cover"]
    numbered = bool(template["numbered_headings"])
    headings: List[PlannedHeading] = []
    counters = [0, 0, 0]
    for i, b in enumerate(blocks):
        if not isinstance(b, S.Heading):
            continue
        number = ""
        if numbered:
            lvl = b.level - 1
            # A level-3 straight after a level-1 (no level-2 between) would
            # read "1.0.1"; a skipped level counts as its first entry, as Word
            # numbers it, so it reads "1.1.1".
            for shallower in range(lvl):
                if counters[shallower] == 0:
                    counters[shallower] = 1
            counters[lvl] += 1
            for deeper in range(lvl + 1, 3):
                counters[deeper] = 0
            number = ".".join(str(c) for c in counters[: lvl + 1])
        headings.append(PlannedHeading(index=i, level=b.level, text=b.text, anchor=f"h-{len(headings) + 1}", number=number))
    toc_flag = spec.toc if template["toc"] is None else template["toc"]
    toc = bool(toc_flag and headings)
    if toc_flag and not headings:
        warnings.append("A contents page was requested but the document has no headings, so none was added.")

    lede_index: Optional[int] = None
    if template["lede"]:
        for i, b in enumerate(blocks):
            if isinstance(b, S.Paragraph):
                lede_index = i
                break
            if isinstance(b, (S.TableBlock, S.ChartBlock)):
                break  # the first prose is not an opening statement

    return DocumentPlan(
        spec=spec, template_id=template_id, template=template, cover=cover, toc=toc, numbered=numbered,
        control_strip=bool(template["control_strip"]), hoisted_kpis=hoisted, blocks=blocks, headings=headings,
        citation_index=citation_numbers(spec.sources), lede_index=lede_index, has_tail=has_tail, warnings=warnings,
    )


# ------------------------------------------------------------ deck plan --

#: Layouts whose defining field must be present, else the slide falls back to
#: bullets: (layout, predicate over the Slide).
_LAYOUT_NEEDS = {
    "chart": lambda s: s.chart is not None,
    "table": lambda s: s.table is not None,
    "kpis": lambda s: bool(s.kpis),
    "timeline": lambda s: bool(s.steps),
    "two_column": lambda s: bool(s.left) or bool(s.right),
    "comparison": lambda s: bool(s.left) or bool(s.right),
}


@dataclass
class PlannedSlide:
    number: int
    layout: str
    title: str
    subtitle: str
    bullets: List[str]
    left: List[str]
    right: List[str]
    left_title: str
    right_title: str
    chart: Optional[S.Chart]
    chart_ordinal: Optional[int]     # 1-based, into spec_charts(); None without a chart
    table: Optional[S.Table]
    kpis: List[S.KPI]
    steps: List[Tuple[str, str]]
    notes: str
    sources: List[str]
    is_sources_slide: bool = False


@dataclass
class DeckPlan:
    spec: S.PresentationSpec
    template_id: str
    template: dict
    slides: List[PlannedSlide]
    citation_index: Dict[str, int]
    warnings: List[str] = field(default_factory=list)

    @property
    def type(self) -> theme.SlideType:
        return self.template["type"]

    @property
    def band(self) -> str:
        return self.template["band"]


#: Bullet geometry shared by print.css (.slide ul.bullets > li) and pptx._bullets:
#: the marker indent (the training template's numbered marker is the widest),
#: the line height, and the gap after each bullet as a fraction of the size.
BULLET_INDENT_IN = 0.45
BULLET_LINE_HEIGHT = 1.3
BULLET_GAP_EM = 0.6
#: Average glyph advance of Arial/Liberation Sans prose as a fraction of the
#: point size. Measured 0.43 em on lowercase lorem ipsum at 18 pt (113
#: characters across 12.0 in); capitals, digits and % are wider, so the
#: estimate is 0.5 em and errs towards shortening, never towards overflow.
BULLET_CHAR_EM = 0.5

#: Where bullets go, by layout: (usable width in, usable height in). Columns
#: give up a title row; comparison cards also lose their padding and the
#: shorter card (pptx._columns and print.css .card agree on these).
_FULL_W = theme.SLIDE_W_IN - 2 * theme.SLIDE_MARGIN_IN
_COL_W = (_FULL_W - 0.4) / 2
BULLET_BOXES: Dict[str, Tuple[float, float]] = {
    "bullets": (_FULL_W, theme.SLIDE_BODY_H_IN),
    "chart": (4.0, theme.SLIDE_BODY_H_IN),
    "two_column": (_COL_W, theme.SLIDE_BODY_H_IN - 0.65),
    "comparison": (_COL_W - 0.5, theme.SLIDE_BODY_H_IN - 0.25 - 0.65 - 0.3),
}


def _shorten(text: str, limit: int) -> str:
    """`text` cut to at most `limit` characters ending in an ellipsis, at a
    word boundary when one is near the cut."""
    head = text[: limit - 1]
    space = head.rfind(" ")
    if space >= max(limit - 25, limit // 2):
        head = head[:space]
    return head.rstrip(" ,;:-") + "…"


def _fit_list(items: Sequence[str], max_items: int, max_chars: int, *, width_in: float, height_in: float,
              body_pt: float) -> Tuple[List[str], int, int]:
    """The bullets that fit: the template's caps first, then a LINE budget —
    8 x 160 characters at 18 pt is 16 wrapped lines, and the 5.1 in body box
    holds 12 (review 2026-09-11: the eighth bullet was clipped and the seventh
    ran into the footer, with no warning). Over budget, the longest wrapped
    bullet is shortened by one line at a time; only when every bullet is one
    line already is the last one dropped. Returns (bullets, shortened, dropped)."""
    chars_per_line = max(20, int((width_in - BULLET_INDENT_IN) * 72 / (BULLET_CHAR_EM * body_pt)))
    line_pt = body_pt * BULLET_LINE_HEIGHT
    gap_pt = body_pt * BULLET_GAP_EM
    budget_pt = height_in * 72

    out: List[str] = []
    shortened: set = set()
    for i, text in enumerate(list(items)[:max_items]):
        t = (text or "").strip()
        if len(t) > max_chars:
            t = _shorten(t, max_chars)
            shortened.add(i)
        out.append(t)
    dropped = len(items) - len(out)

    def lines(t: str) -> int:
        return max(1, -(-len(t) // chars_per_line))

    def height(bullets: Sequence[str]) -> float:
        return sum(lines(t) * line_pt + gap_pt for t in bullets)

    while out and height(out) > budget_pt:
        multi = [i for i, t in enumerate(out) if lines(t) > 1]
        if multi:
            i = max(multi, key=lambda k: len(out[k]))
            out[i] = _shorten(out[i], (lines(out[i]) - 1) * chars_per_line)
            shortened.add(i)
        else:
            out.pop()
            dropped += 1
    return out, len(shortened), dropped


def _fit_warning(where: str, shortened: int, dropped: int) -> str:
    """One sentence per slide, however many bullets were touched."""
    bits = []
    if shortened:
        bits.append(f"{shortened} bullet{'s' if shortened != 1 else ''} shortened")
    if dropped:
        bits.append(f"{dropped} bullet{'s' if dropped != 1 else ''} dropped")
    return f"{where}: {' and '.join(bits)} to fit the slide."


def fit_bullets(items: Sequence[str], max_items: int, max_chars: int, where: str, warnings: List[str], *,
                layout: str = "bullets", body_pt: float = theme.SLIDE_TYPE.body) -> List[str]:
    """Trim a bullet list to the template's cap and to the slide's line
    budget rather than overflow; one warning per call names what changed."""
    width_in, height_in = BULLET_BOXES.get(layout, BULLET_BOXES["bullets"])
    out, shortened, dropped = _fit_list(items, max_items, max_chars, width_in=width_in, height_in=height_in, body_pt=body_pt)
    if shortened or dropped:
        warnings.append(_fit_warning(where, shortened, dropped))
    return out


#: A slide table at 16 pt: about ten rows and eight columns before the rows
#: run under the footer or the columns narrow past a word.
TABLE_MAX_ROWS = 10
TABLE_MAX_COLS = 8


def fit_table(table: S.Table, where: str, warnings: List[str], max_rows: int = TABLE_MAX_ROWS, max_cols: int = TABLE_MAX_COLS) -> S.Table:
    """The rows and columns of a slide table that fit, decided HERE so the
    preview PDF and the .pptx show the same ten rows (the writer used to cut
    its own copy while the preview drew all 200 and clipped mid-row)."""
    rows = table.rows
    cols = table.columns
    numeric = list(table.numeric_columns)
    if len(cols) > max_cols:
        warnings.append(f"{where}: the table has {len(cols)} columns; only the first {max_cols} fit on the slide.")
        cols = cols[:max_cols]
        rows = [r[:max_cols] for r in rows]
        numeric = [i for i in numeric if i < max_cols]
    if len(rows) > max_rows:
        warnings.append(f"{where}: the table has {len(rows)} rows; only the first {max_rows} fit on the slide.")
        rows = rows[:max_rows]
    if cols is table.columns and rows is table.rows:
        return table
    return S.Table(columns=list(cols), rows=[list(r) for r in rows], caption=table.caption, numeric_columns=numeric, sources=list(table.sources))


def plan_deck(spec: S.PresentationSpec) -> DeckPlan:
    """The slide list both the PPTX writer and the PDF preview render."""
    template_id = spec.template_id if spec.template_id in theme.PRESENTATION_TEMPLATES else "generic"
    template = theme.PRESENTATION_TEMPLATES[template_id]
    warnings: List[str] = []
    max_b, max_c = template["max_bullets"], template["max_bullet_chars"]
    slides: List[PlannedSlide] = []
    chart_ordinal = 0
    for i, s in enumerate(spec.slides):
        n = i + 1
        where = f"Slide {n}"
        layout = s.layout
        needs = _LAYOUT_NEEDS.get(layout)
        if needs is not None and not needs(s):
            warnings.append(f"{where}: the {layout} layout had no {layout.replace('_', ' ')} content, so it was laid out as bullets.")
            layout = "bullets"
        ordinal: Optional[int] = None
        if s.chart is not None:
            chart_ordinal += 1
            ordinal = chart_ordinal
        # Column caps are half the bullet cap (two lists share the slide).
        col_cap = max(3, max_b // 2 + 1)
        body_pt = template["type"].body
        shortened = dropped = 0
        fitted: Dict[str, List[str]] = {}
        for name, items, cap, box in (
            ("bullets", s.bullets, 4 if layout == "chart" else max_b, BULLET_BOXES["chart" if layout == "chart" else "bullets"]),
            ("left", s.left, col_cap, BULLET_BOXES["comparison" if layout == "comparison" else "two_column"]),
            ("right", s.right, col_cap, BULLET_BOXES["comparison" if layout == "comparison" else "two_column"]),
        ):
            fitted[name], short_n, drop_n = _fit_list(items, cap, max_c, width_in=box[0], height_in=box[1], body_pt=body_pt)
            shortened += short_n
            dropped += drop_n
        if shortened or dropped:
            warnings.append(_fit_warning(where, shortened, dropped))
        table = fit_table(s.table, where, warnings) if (layout == "table" and s.table is not None) else s.table
        slides.append(PlannedSlide(
            number=n, layout=layout, title=s.title, subtitle=s.subtitle,
            bullets=fitted["bullets"], left=fitted["left"], right=fitted["right"],
            left_title=s.left_title, right_title=s.right_title,
            chart=s.chart, chart_ordinal=ordinal, table=table, kpis=list(s.kpis[:4]),
            steps=[(a, b) for a, b in s.steps[:6]], notes=s.notes, sources=list(s.sources),
        ))
    if spec.sources:
        slides.append(PlannedSlide(
            number=len(slides) + 1, layout="sources", title="Sources", subtitle="", bullets=[], left=[], right=[],
            left_title="", right_title="", chart=None, chart_ordinal=None, table=None, kpis=[], steps=[], notes="",
            sources=[c.id for c in spec.sources], is_sources_slide=True,
        ))
    return DeckPlan(spec=spec, template_id=template_id, template=template, slides=slides,
                    citation_index=citation_numbers(spec.sources), warnings=warnings)


# ------------------------------------------------------------ html bits --


def _cite(ids: Sequence[str], index: Dict[str, int]) -> str:
    nums = sorted({index[i] for i in ids if i in index})
    if not nums:
        return ""
    return f'<sup class="cite">[{", ".join(str(n) for n in nums)}]</sup>'


def _table_html(table: S.Table, index: Dict[str, int]) -> str:
    numeric = set(table.numeric_columns)
    head = "".join(
        f'<th class="{"num" if i in numeric else "txt"}">{e(c)}</th>' for i, c in enumerate(table.columns)
    )
    body_rows = []
    for row in table.rows:
        cells = "".join(
            f'<td class="{"num" if i in numeric else "txt"}">{e(cell_text(v, i in numeric))}</td>'
            for i, v in enumerate(row)
        )
        body_rows.append(f"<tr>{cells}</tr>")
    caption = ""
    if table.caption or table.sources:
        caption = f'<p class="table-caption">{e(table.caption)}{_cite(table.sources, index)}</p>'
    return f'<table class="data"><thead><tr>{head}</tr></thead><tbody>{"".join(body_rows)}</tbody></table>{caption}'


def _chart_html(chart: S.Chart, ordinal: int, index: Dict[str, int]) -> str:
    caption = chart.caption or ""
    cap = f"<figcaption>{e(caption)}{_cite(chart.sources, index)}</figcaption>" if (caption or chart.sources) else ""
    # The src is a bare filename minted by code, never the spec's text.
    return f'<figure class="chart"><img src="{chart_filename(ordinal)}" alt="{e(chart.title)}">{cap}</figure>'


def _kpis_html(row: S.KPIRow) -> str:
    items = "".join(
        f'<div class="kpi"><div class="kpi-value{_kpi_size_class(k.value)}">{e(k.value)}</div><div class="kpi-label">{e(k.label)}</div>'
        + (f'<div class="kpi-note">{e(k.note)}</div>' if k.note else "") + "</div>"
        for k in row.items
    )
    return f'<div class="kpis">{items}</div>'


def _callout_html(c: S.Callout) -> str:
    accent, bg = theme.CALLOUT_COLOURS.get(c.kind, theme.CALLOUT_COLOURS["note"])
    title = f'<div class="callout-title">{e(c.title or c.kind.capitalize())}</div>' if c.kind != "quote" or c.title else ""
    return (
        f'<div class="callout {e(c.kind)}" style="--callout-accent:{accent};--callout-bg:{bg}">'
        f"{title}<p>{e(c.text)}</p></div>"
    )


def _sources_html(sources: Sequence[S.Citation], heading: str = "Sources") -> str:
    if not sources:
        return ""
    items = []
    for c in sources:
        bits = [f"<span class=\"title\">{e(c.title)}</span>"]
        if c.url:
            bits.append(f'<span class="url">{e(c.url)}</span>')
        if c.retrieved_at:
            bits.append(f'<span class="retrieved">retrieved {e(c.retrieved_at)}</span>')
        if c.note:
            bits.append(f'<span class="note">{e(c.note)}</span>')
        items.append(f"<li>{' — '.join(bits)}</li>")
    return f'<section class="sources"><h2>{e(heading)}</h2><ol>{"".join(items)}</ol></section>'


def _assumptions_html(assumptions: Sequence[str]) -> str:
    if not assumptions:
        return ""
    items = "".join(f"<li>{e(a)}</li>" for a in assumptions)
    return f'<section class="assumptions"><h2>Assumptions</h2><ul>{items}</ul></section>'


def _root_vars(t: theme.TypeScale, extra: str = "") -> str:
    return (
        ":root{" + extra +
        f"--font-sans:{theme.CSS_SANS};--font-serif:{theme.CSS_SERIF};--font-mono:{theme.CSS_MONO};"
        f"--ink:{theme.INK};--ink-muted:{theme.INK_MUTED};--ink-faint:{theme.INK_FAINT};--surface:{theme.SURFACE};"
        f"--border:{theme.BORDER};--accent:{theme.ACCENT};--navy:{theme.NAVY};--boardroom:{theme.BOARDROOM};"
        f"--slate:{theme.SLATE};--teal:{theme.TEAL};--danger:{theme.DANGER};"
        f"--body-pt:{t.body}pt;--small-pt:{t.small}pt;--caption-pt:{t.caption}pt;--h1-pt:{t.h1}pt;--h2-pt:{t.h2}pt;"
        f"--h3-pt:{t.h3}pt;--title-pt:{t.title}pt;--subtitle-pt:{t.subtitle}pt;--kpi-pt:{t.kpi_value}pt;"
        f"--line-height:{t.line_height};"
        "}"
    )


def _page_rules(plan: DocumentPlan) -> str:
    g = plan.grid
    size = f"A4 {plan.spec.orientation}"
    boxes = []
    if g.header:
        boxes.append("@top-center{content:element(header);width:100%;vertical-align:bottom;padding-bottom:4pt}")
    if g.footer:
        boxes.append(
            '@bottom-center{content:"Page " counter(page) " of " counter(pages);'
            f"font-family:{theme.CSS_SANS};font-size:8.5pt;color:{theme.INK_FAINT}}}"
        )
        boxes.append("@bottom-left{content:element(footer);vertical-align:top}")
    page = f"@page{{size:{size};margin:{g.top_mm}mm {g.right_mm}mm {g.bottom_mm}mm {g.left_mm}mm;{''.join(boxes)}}}"
    cover = "@page cover{@top-center{content:none}@bottom-center{content:none}@bottom-left{content:none}}"
    # The cover fills its page exactly: A4 is 210 x 297 mm.
    page_h = 297 if plan.spec.orientation == "portrait" else 210
    cover_box = (
        f"section.cover{{height:{page_h - g.top_mm - g.bottom_mm}mm}}"
        f"section.cover .band{{margin:-{g.top_mm}mm -{g.right_mm}mm 0 -{g.left_mm}mm}}"
    )
    return page + cover + cover_box


# --------------------------------------------------------- document html --


def document_html(spec: S.DocumentSpec, plan: Optional[DocumentPlan] = None) -> str:
    """A complete HTML page for one document."""
    plan = plan or plan_document(spec)
    index = plan.citation_index
    body_classes = [f"tpl-{plan.template_id}", "doc"]
    if plan.numbered:
        body_classes.append("numbered")

    parts: List[str] = []
    mark = '<span class="mark">CONFIDENTIAL</span>' if spec.confidential else ""
    parts.append(f'<div class="running-header"><span class="title">{e(spec.title)}</span>{mark}</div>')
    footer_bits = [b for b in (spec.author, spec.date) if b]
    parts.append(f'<div class="running-footer">{e(" · ".join(footer_bits))}</div>')

    if plan.cover:
        meta = []
        for label, value in (("Prepared for", spec.audience), ("Prepared by", spec.author), ("Date", spec.date), ("Purpose", spec.purpose)):
            if value:
                meta.append(f'<tr><td class="label">{e(label)}</td><td>{e(value)}</td></tr>')
        kicker = plan.template_id.replace("_", " ").title()
        parts.append(
            '<section class="cover"><div class="band">'
            f'<div class="kicker">{e(kicker)}</div><h1 class="doc-title">{e(spec.title)}</h1>'
            + (f'<div class="subtitle">{e(spec.subtitle)}</div>' if spec.subtitle else "")
            + f'</div><table class="meta"><tbody>{"".join(meta)}</tbody></table>'
            + ('<div class="confidential">CONFIDENTIAL</div>' if spec.confidential else "")
            + "</section>"
        )
    else:
        byline = " · ".join(b for b in (spec.author, spec.date, spec.audience and f"For {spec.audience}") if b)
        parts.append(
            '<header class="titleblock">'
            f'<h1 class="doc-title">{e(spec.title)}</h1>'
            + (f'<div class="subtitle">{e(spec.subtitle)}</div>' if spec.subtitle else "")
            + (f'<div class="byline">{e(byline)}</div>' if byline else "")
            + "</header>"
        )

    if plan.toc:
        entries = "".join(
            f'<li class="l{h.level}"><a href="#{h.anchor}">{e((h.number + "  ") if h.number else "")}{e(h.text)}</a></li>'
            for h in plan.headings
        )
        parts.append(f'<nav class="toc"><h2>Contents</h2><ol>{entries}</ol></nav>')

    if plan.control_strip:
        rows = [("Document", spec.title)]
        if spec.author:
            rows.append(("Owner", spec.author))
        if spec.date:
            rows.append(("Date", spec.date))
        if spec.audience:
            rows.append(("Audience", spec.audience))
        if spec.purpose:
            rows.append(("Purpose", spec.purpose))
        parts.append(
            '<table class="control"><tbody>'
            + "".join(f'<tr><td class="k">{e(k)}</td><td>{e(v)}</td></tr>' for k, v in rows)
            + "</tbody></table>"
        )

    if plan.hoisted_kpis is not None:
        parts.append(_kpis_html(plan.hoisted_kpis))

    heading_by_index = {h.index: h for h in plan.headings}
    main: List[str] = []
    for i, b in enumerate(plan.blocks):
        if isinstance(b, S.Heading):
            h = heading_by_index[i]
            # The number comes from the plan, not from CSS counters, so the
            # PDF shows exactly what the DOCX and the contents page show.
            num = f'<span class="hnum">{e(h.number)}</span>' if h.number else ""
            main.append(f'<h{b.level} id="{h.anchor}">{num}{e(b.text)}</h{b.level}>')
        elif isinstance(b, S.Paragraph):
            cls = ' class="lede"' if i == plan.lede_index else ""
            main.append(f"<p{cls}>{e(b.text)}{_cite(b.sources, index)}</p>")
        elif isinstance(b, S.Numbered):
            cls = ' class="steps"' if plan.template_id == "sop" else ""
            main.append(f"<ol{cls}>" + "".join(f"<li>{e(t)}</li>" for t in b.items) + f"</ol>{_cite(b.sources, index)}")
        elif isinstance(b, S.Bullets):
            cls = ' class="actions"' if plan.template_id == "meeting_summary" else ""
            main.append(f"<ul{cls}>" + "".join(f"<li>{e(t)}</li>" for t in b.items) + f"</ul>{_cite(b.sources, index)}")
        elif isinstance(b, S.TableBlock):
            main.append(_table_html(b.table, index))
        elif isinstance(b, S.ChartBlock):
            ordinal = sum(1 for x in plan.blocks[: i + 1] if isinstance(x, S.ChartBlock))
            main.append(_chart_html(b.chart, ordinal, index))
        elif isinstance(b, S.Callout):
            main.append(_callout_html(b))
        elif isinstance(b, S.KPIRow):
            main.append(_kpis_html(b))
        elif isinstance(b, S.PageBreak):
            main.append('<div class="page-break"></div>')
    parts.append(f"<main>{''.join(main)}</main>")

    parts.append(_sources_html(spec.sources, "References" if plan.template_id == "research_report" else "Sources"))
    parts.append(_assumptions_html(spec.assumptions))

    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{e(spec.title)}</title><style>{_root_vars(plan.type)}{print_css()}{_page_rules(plan)}</style></head>"
        f'<body class="{" ".join(body_classes)}">{"".join(parts)}</body></html>'
    )


# ------------------------------------------------------------- deck html --


def _deck_vars(plan: DeckPlan) -> str:
    t = plan.type
    return (
        ":root{"
        f"--font-sans:{theme.CSS_SANS};--font-serif:{theme.CSS_SERIF};--ink:{theme.INK};--ink-muted:{theme.INK_MUTED};"
        f"--ink-faint:{theme.INK_FAINT};--surface:{theme.SURFACE};--border:{theme.BORDER};--accent:{theme.ACCENT};"
        f"--navy:{theme.NAVY};--boardroom:{theme.BOARDROOM};--slate:{theme.SLATE};--teal:{theme.TEAL};--danger:{theme.DANGER};"
        f"--band:{plan.band};"
        f"--slide-w:{theme.SLIDE_W_IN}in;--slide-h:{theme.SLIDE_H_IN}in;--slide-margin:{theme.SLIDE_MARGIN_IN}in;"
        f"--slide-title-top:{theme.SLIDE_TITLE_TOP_IN}in;--slide-title-h:{theme.SLIDE_TITLE_H_IN}in;"
        f"--slide-body-top:{theme.SLIDE_BODY_TOP_IN}in;--slide-body-h:{theme.SLIDE_BODY_H_IN}in;"
        f"--slide-footer-top:{theme.SLIDE_FOOTER_TOP_IN}in;--slide-footer-h:{theme.SLIDE_FOOTER_H_IN}in;"
        f"--slide-title-pt:{t.title}pt;--slide-cover-pt:{t.cover_title}pt;--slide-body-pt:{t.body}pt;"
        f"--slide-small-pt:{t.small}pt;--slide-kpi-pt:{t.kpi_value}pt;--slide-kpi-label-pt:{t.kpi_label}pt;"
        f"--slide-section-pt:{t.section}pt;--body-pt:{t.body}pt;--small-pt:{t.small}pt;--caption-pt:{t.small}pt;"
        f"--line-height:1.3;--h2-pt:{t.title}pt;"
        "}"
        f"@page{{size:{theme.SLIDE_W_IN}in {theme.SLIDE_H_IN}in;margin:0}}"
    )


def _bullets_html(items: Sequence[str]) -> str:
    return '<ul class="bullets">' + "".join(f"<li>{e(t)}</li>" for t in items) + "</ul>"


def _slide_body_html(s: PlannedSlide, plan: DeckPlan) -> str:
    index = plan.citation_index
    if s.layout == "two_column":
        left_title = f'<div class="col-title">{e(s.left_title)}</div>' if s.left_title else ""
        right_title = f'<div class="col-title">{e(s.right_title)}</div>' if s.right_title else ""
        return (
            '<div class="cols">'
            f'<div class="col">{left_title}{_bullets_html(s.left)}</div>'
            f'<div class="col">{right_title}{_bullets_html(s.right)}</div>'
            "</div>"
        )
    if s.layout == "comparison":
        return (
            '<div class="cols">'
            f'<div class="col card left"><div class="col-title">{e(s.left_title or "Option A")}</div>{_bullets_html(s.left)}</div>'
            f'<div class="col card right"><div class="col-title">{e(s.right_title or "Option B")}</div>{_bullets_html(s.right)}</div>'
            "</div>"
        )
    if s.layout == "chart" and s.chart is not None and s.chart_ordinal:
        fig = _chart_html(s.chart, s.chart_ordinal, index)
        if s.bullets:
            return f'<div class="chart-with-text">{_bullets_html(s.bullets)}{fig}</div>'
        return fig
    if s.layout == "table" and s.table is not None:
        return _table_html(s.table, index)
    if s.layout == "kpis":
        boxes = "".join(
            f'<div class="kpi-box"><div class="v{_kpi_size_class(k.value)}">{e(k.value)}</div><div class="l">{e(k.label)}</div>'
            + (f'<div class="n">{e(k.note)}</div>' if k.note else "") + "</div>"
            for k in s.kpis
        )
        return f'<div class="kpi-row">{boxes}</div>'
    if s.layout == "timeline":
        steps = "".join(
            f'<div class="step"><div class="label">{e(label)}</div><div class="dot"></div><div class="text">{e(text)}</div></div>'
            for label, text in s.steps
        )
        return f'<div class="timeline"><div class="line"></div><div class="steps">{steps}</div></div>'
    if s.layout == "sources":
        items = []
        for c in plan.spec.sources:
            bits = [e(c.title)]
            if c.url:
                bits.append(f'<span class="url">{e(c.url)}</span>')
            items.append(f"<li>{' — '.join(bits)}</li>")
        return f'<ol class="sources">{"".join(items)}</ol>'
    # bullets (and any fallback)
    return _bullets_html(s.bullets) if s.bullets else ""


def _slide_html(s: PlannedSlide, plan: DeckPlan, total: int) -> str:
    spec = plan.spec
    last = " last" if s.number == total else ""
    if s.layout in ("title", "section", "closing"):
        title = s.title or (spec.title if s.layout == "title" else ("Thank you" if s.layout == "closing" else ""))
        subtitle = s.subtitle or (spec.subtitle if s.layout == "title" else "")
        meta_bits = [b for b in (spec.author, spec.date, spec.audience and f"Prepared for {spec.audience}") if b]
        meta = f'<div class="deck-meta">{e(" · ".join(meta_bits))}</div>' if (s.layout == "title" and meta_bits) else ""
        if s.layout == "closing" and s.bullets:
            meta = f'<div class="deck-meta">{e(" · ".join(s.bullets))}</div>'
        cls = f"slide band {e(s.layout)}{last}"
        return (
            f'<section class="{cls}"><div class="rule"></div><div class="deck-title">{e(title)}</div>'
            + (f'<div class="deck-subtitle">{e(subtitle)}</div>' if subtitle else "")
            + meta + "</section>"
        )
    mark = '<span class="mark">CONFIDENTIAL</span>' if spec.confidential else ""
    footer = f'<div class="footer">{e(spec.title)}{mark}<span class="n">{s.number} / {total}</span></div>'
    cite = "" if s.is_sources_slide else _cite(s.sources, plan.citation_index)
    return (
        f'<section class="slide layout-{e(s.layout)}{last}">'
        f'<div class="title">{e(s.title)}{cite}</div>'
        f'<div class="body">{_slide_body_html(s, plan)}</div>{footer}</section>'
    )


def deck_html(spec: S.PresentationSpec, plan: Optional[DeckPlan] = None) -> str:
    """A complete HTML page whose pages are the 16:9 slides of the deck."""
    plan = plan or plan_deck(spec)
    total = len(plan.slides)
    slides = "".join(_slide_html(s, plan, total) for s in plan.slides)
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{e(spec.title)}</title><style>{_deck_vars(plan)}{print_css()}</style></head>"
        f'<body class="deck tpl-{e(plan.template_id)}">{slides}</body></html>'
    )


# --------------------------------------------------------- workbook html --


def workbook_summary_html(spec: S.WorkbookSpec, max_rows: int = 25, max_cols: int = 10) -> str:
    """A short PDF-able summary of a workbook — each sheet's header and first
    rows — so the card has a thumbnail. The real preview is the grid."""
    parts = [
        '<div class="running-header"><span class="title">' + e(spec.title) + "</span></div>",
        '<div class="running-footer"></div>',
        f'<header class="titleblock"><h1 class="doc-title">{e(spec.title)}</h1>'
        + (f'<div class="subtitle">{e(spec.purpose)}</div>' if spec.purpose else "")
        + f'<div class="byline">{len(spec.sheets)} sheet{"s" if len(spec.sheets) != 1 else ""}</div></header>',
    ]
    main: List[str] = []
    for sheet in spec.sheets:
        cols = sheet.columns[:max_cols]
        numeric = {i for i, c in enumerate(cols) if c.type != "text"}
        head = "".join(f'<th class="{"num" if i in numeric else "txt"}">{e(c.name)}</th>' for i, c in enumerate(cols))
        rows = []
        for row in sheet.rows[:max_rows]:
            rows.append("<tr>" + "".join(
                f'<td class="{"num" if i in numeric else "txt"}">{e(cell_text(v, i in numeric))}</td>'
                for i, v in enumerate(row[:max_cols])
            ) + "</tr>")
        shown = min(len(sheet.rows), max_rows)
        note = ""
        if len(sheet.rows) > shown:
            note = f"<p>{len(sheet.rows):,} rows in the sheet; the first {shown} are shown.</p>"
        body = "".join(rows)
        main.append(f'<h2>{e(sheet.name)}</h2>{note}<table class="data"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>')
    parts.append(f"<main>{''.join(main)}</main>")
    t = theme.DOCUMENT_TYPE
    page = (
        f"@page{{size:A4 landscape;margin:16mm;@top-center{{content:element(header);width:100%}}"
        f'@bottom-center{{content:"Page " counter(page) " of " counter(pages);font-size:8.5pt;color:{theme.INK_FAINT}}}}}'
    )
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{e(spec.title)}</title><style>{_root_vars(t)}{print_css()}{page}</style></head>"
        f'<body class="doc tpl-workbook">{"".join(parts)}</body></html>'
    )


# ------------------------------------------------------ tabular document --
#
# WHY. "XLSX, Word, PDF and CSV of this audit" (CONTRACT-2 §1) is four files
# from one WorkbookSpec; the Word and PDF are the TABULAR DOCUMENT — every
# sheet as a section with its whole table, landscape when the sheet's style
# says so or when it has more than six columns, the header row repeated on
# every page, the highlighted column in the same red the Excel file uses, a
# methodology note that says blank means blank, page numbers, the title. The
# HTML below is the PDF's source and the plan docx.py follows (sections,
# orientation, widths, colours), so the two documents agree.

#: The tabular document's type: 9.5 pt cells, never under 9 — a 500-row
#: audit at 8 pt is the "unreadable PDF" complaint.
TABULAR_CELL_PT = 9.5
#: Column width shares are clipped to this range of a table's width so a
#: one-character column keeps a readable cell and a comment column cannot
#: crowd the others out.
_MIN_COL_SHARE = 0.05
_MAX_COL_SHARE = 0.45
#: How many rows are sampled for a column's width.
_WIDTH_SAMPLE_ROWS = 300

TABULAR_HIGHLIGHT = {
    "red": ("#9C0006", "#FFFFFF", "#FFC7CE", "#9C0006"),
    "amber": ("#9C5700", "#FFFFFF", "#FFEB9C", "#9C5700"),
    "green": ("#006100", "#FFFFFF", "#C6EFCE", "#006100"),
    "blue": ("#1F4E78", "#FFFFFF", "#DDEBF7", "#1F4E78"),
}
TABULAR_HEADER_FILLS = {
    "dark": (theme.NAVY, theme.WHITE),
    "light": (theme.SURFACE_2, theme.INK),
    "none": ("", theme.INK),
}


def column_shares(sheet: S.Sheet) -> List[float]:
    """Each column's share of the table width, from the longest text in
    the header and a sample of its cells (square-rooted, so a 300-character
    comment does not take 80% of the page and a date column is not
    squeezed to nothing), clipped to [_MIN_COL_SHARE, _MAX_COL_SHARE] and
    normalised to sum to 1. Long text WRAPS inside its share; nothing is
    clipped."""
    import math

    weights: List[float] = []
    for j, col in enumerate(sheet.columns):
        longest = len(col.name)
        for row in sheet.rows[:_WIDTH_SAMPLE_ROWS]:
            v = row[j] if j < len(row) else None
            if v is not None:
                longest = max(longest, len(str(v)))
        weights.append(math.sqrt(max(longest, 3)))
    total = sum(weights) or 1.0
    shares = [min(max(w / total, _MIN_COL_SHARE), _MAX_COL_SHARE) for w in weights]
    total = sum(shares)
    return [round(sh / total, 4) for sh in shares]


def sheet_orientation(sheet: S.Sheet) -> str:
    """`landscape` or `portrait` for the sheet's section (CONTRACT-2 §4:
    `auto` is landscape past six columns) — one rule, read by the DOCX
    writer too."""
    style = sheet.style if sheet.style is not None else S.SheetStyle()
    if style.orientation == "auto":
        return "landscape" if len(sheet.columns) > 6 else "portrait"
    return style.orientation


def highlight_columns(sheet: S.Sheet) -> Dict[int, str]:
    """Column index → colour name for the style's highlighted columns."""
    style = sheet.style if sheet.style is not None else S.SheetStyle()
    by_name: Dict[str, int] = {}
    for j, c in enumerate(sheet.columns):
        by_name.setdefault(" ".join(c.name.split()).casefold(), j)
    out: Dict[int, str] = {}
    for h in style.highlight:
        j = by_name.get(" ".join(h.column.split()).casefold())
        if j is not None:
            out[j] = h.color
    return out


def methodology_note(spec: S.WorkbookSpec, transform: Optional[dict] = None) -> str:
    """The one-paragraph note under the title: blank means blank, plus
    what code did to a pasted table (`transform`, the engine's report when
    the pipeline passes one) or what the spec itself records (rows copied
    from a pasted table, generated, a column rewritten)."""
    bits = ["Blank values are blank in the source."]
    t = transform or {}
    copied = [sh for sh in spec.sheets if sh.rows_from]
    generated = [sh for sh in spec.sheets if sh.generator is not None]
    if t.get("rows"):
        bits.append(f"{int(t['rows']):,} source row{'s were' if int(t['rows']) != 1 else ' was'} preserved as pasted.")
    elif copied:
        n = sum(len(sh.rows) for sh in copied)
        bits.append(f"{n:,} row{'s were' if n != 1 else ' was'} copied verbatim from the pasted table.")
    if t.get("forward_filled"):
        bits.append(f"{int(t['forward_filled']):,} blank grouping cell{'s were' if int(t['forward_filled']) != 1 else ' was'} filled from the row above.")
    rewritten = [r.column for sh in spec.sheets for r in sh.rewrite]
    if t.get("rewritten") or rewritten:
        cols = ", ".join(dict.fromkeys(rewritten)) or "comment"
        bits.append(f"The {cols} column was rewritten for clarity; timestamps and quoted text were kept as written.")
    if generated:
        n = sum(len(sh.rows) for sh in generated)
        bits.append(f"{n:,} row{'s were' if n != 1 else ' was'} generated from a column recipe (seed {generated[0].generator.seed}).")
    return " ".join(bits)


def _tabular_table_html(sheet: S.Sheet) -> str:
    numeric = {i for i, c in enumerate(sheet.columns) if c.type != "text"}
    highlight = highlight_columns(sheet)
    style = sheet.style if sheet.style is not None else S.SheetStyle()
    shares = column_shares(sheet)
    cols = "".join(f'<col style="width:{sh * 100:.2f}%">' for sh in shares)
    fill, ink = TABULAR_HEADER_FILLS.get(style.header_fill, TABULAR_HEADER_FILLS["dark"])
    head_cells = []
    for i, c in enumerate(sheet.columns):
        cls = "num" if i in numeric else "txt"
        colour = highlight.get(i)
        if colour:
            h_fill, h_ink, _, _ = TABULAR_HIGHLIGHT[colour]
            head_cells.append(f'<th class="{cls} hl hl-{e(colour)}" style="background:{h_fill};color:{h_ink}">{e(c.name)}</th>')
        else:
            bg = f"background:{fill};" if fill else "background:none;"
            head_cells.append(f'<th class="{cls}" style="{bg}color:{ink}">{e(c.name)}</th>')
    body_rows = []
    for row in sheet.rows:
        cells = []
        for i, v in enumerate(row):
            cls = "num" if i in numeric else "txt"
            colour = highlight.get(i)
            text = e(cell_text(v, i in numeric))
            if colour:
                _, _, c_fill, c_ink = TABULAR_HIGHLIGHT[colour]
                cells.append(f'<td class="{cls} hl" style="background:{c_fill};color:{c_ink}">{text}</td>')
            else:
                cells.append(f'<td class="{cls}">{text}</td>')
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    classes = "data tabular" + (" borders" if style.borders == "thin" else " no-borders") + (" wrap" if style.wrap else "")
    bold = "" if style.header_bold else ' data-header-bold="no"'
    return (
        f'<table class="{classes}"{bold}><colgroup>{cols}</colgroup>'
        f'<thead><tr>{"".join(head_cells)}</tr></thead><tbody>{"".join(body_rows)}</tbody></table>'
    )


def workbook_document_html(spec: S.WorkbookSpec, transform: Optional[dict] = None) -> str:
    """The tabular document as one HTML page: title, methodology note, then
    one section per sheet — a heading and the FULL table — each on its
    own named page (`landscape` / `portrait`) so orientation follows the
    sheet. The header row repeats on every page (print.css `thead
    {display: table-header-group}`), rows never split across pages, and
    there is no trailing page break, so no blank last page."""
    first = sheet_orientation(spec.sheets[0]) if spec.sheets else "portrait"
    # The title block shares the first sheet's named page: a change of page
    # name is a page break in WeasyPrint, and a title alone on page 1 with
    # the table starting on page 2 is the blank-looking first page.
    parts = [
        '<div class="running-header"><span class="title">' + e(spec.title) + "</span></div>",
        '<div class="running-footer"></div>',
        f'<div class="front {first}"><header class="titleblock"><h1 class="doc-title">{e(spec.title)}</h1>'
        + (f'<div class="subtitle">{e(spec.purpose)}</div>' if spec.purpose else "")
        + f'<div class="byline">{len(spec.sheets)} sheet{"s" if len(spec.sheets) != 1 else ""} · '
        + f'{sum(len(sh.rows) for sh in spec.sheets):,} rows</div></header>'
        + f'<p class="methodology">{e(methodology_note(spec, transform))}</p></div>',
    ]
    sections: List[str] = []
    for k, sheet in enumerate(spec.sheets):
        orientation = sheet_orientation(sheet)
        cls = f"sheet {orientation}" + (" first" if k == 0 else "")
        note = f"<p class=\"sheet-note\">{e(sheet.notes)}</p>" if sheet.notes else ""
        sections.append(
            f'<section class="{cls}"><h2>{e(sheet.name)}</h2>{note}{_tabular_table_html(sheet)}'
            f'<p class="sheet-count">{len(sheet.rows):,} row{"s" if len(sheet.rows) != 1 else ""} · {len(sheet.columns)} columns</p></section>'
        )
    parts.append(f"<main>{''.join(sections)}</main>")
    parts.append(_sources_html(spec.sources))
    parts.append(_assumptions_html(spec.assumptions))
    t = theme.DOCUMENT_TYPE
    footer = (
        '@bottom-center{content:"Page " counter(page) " of " counter(pages);'
        f"font-family:{theme.CSS_SANS};font-size:8.5pt;color:{theme.INK_FAINT}}}"
    )
    header = "@top-center{content:element(header);width:100%;vertical-align:bottom;padding-bottom:4pt}"
    pages = (
        f"@page{{size:A4 {first};margin:16mm 14mm 16mm 14mm;{header}{footer}}}"
        f"@page landscape{{size:A4 landscape;margin:16mm 14mm 16mm 14mm;{header}{footer}}}"
        f"@page portrait{{size:A4 portrait;margin:16mm 14mm 16mm 14mm;{header}{footer}}}"
    )
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{e(spec.title)}</title><style>{_root_vars(t, f'--tabular-pt:{TABULAR_CELL_PT}pt;')}{print_css()}{pages}</style></head>"
        f'<body class="doc tpl-tabular">{"".join(parts)}</body></html>'
    )


__all__ = [
    "print_css", "format_number", "cell_text", "spec_charts", "chart_filename",
    "PlannedHeading", "DocumentPlan", "plan_document", "PlannedSlide", "DeckPlan", "plan_deck", "fit_bullets",
    "fit_table", "BULLET_BOXES", "TABLE_MAX_ROWS", "TABLE_MAX_COLS",
    "citation_numbers", "document_html", "deck_html", "workbook_summary_html",
    "workbook_document_html", "column_shares", "sheet_orientation", "highlight_columns", "methodology_note",
    "TABULAR_CELL_PT", "TABULAR_HIGHLIGHT", "TABULAR_HEADER_FILLS",
]
