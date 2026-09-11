"""PresentationSpec → .pptx with python-pptx, one slide per planned slide.

THE DECK IS DRAWN, NOT TEMPLATED. python-pptx's default template has
placeholder layouts whose fonts, positions and colours we do not control and
whose theme is Office's. Every slide here uses the BLANK layout and our own
shapes, placed on the same geometry (theme.SLIDE_*_IN) the PDF preview uses,
so the .pptx a person opens is the deck they saw in the viewer. Charts are
NATIVE (pptx.chart.data.CategoryChartData): a person can click the chart in
PowerPoint and edit the numbers. Tables are native tables. Timelines are
shapes and connectors. Speaker notes go into the notes slide.

WHAT FITS. Body text never drops below 14 pt; instead of overflowing, the
plan (html.plan_deck) trims bullets over the template's cap and the slide's
line budget, cuts tables to the rows a slide holds, and records one warning
per slide. This writer trusts the plan and draws what it says.

python-pptx is imported lazily (tests/test_imports.py). Its native charts
embed an .xlsx workbook written with XlsxWriter — that is why XlsxWriter is
a transitive requirement; the workbook holds only the chart's numbers.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence

from .. import spec as S
from . import theme
from .html import BULLET_GAP_EM, DeckPlan, PlannedSlide, cell_text, plan_deck

# python-pptx's blank layout index in the default template.
_BLANK_LAYOUT = 6


def _rgb(colour: str):
    from pptx.dml.color import RGBColor

    return RGBColor(*theme.hex_to_rgb(colour))


def _text_box(slide, x, y, w, h, text: str, *, size: float, bold: bool = False, colour: str = theme.INK,
              align=None, anchor=None, font: str = theme.OFFICE_SANS, wrap: bool = True):
    from pptx.util import Inches, Pt

    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = wrap
    if anchor is not None:
        tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Inches(0.05)
    tf.margin_top = tf.margin_bottom = Inches(0.03)
    p = tf.paragraphs[0]
    if align is not None:
        p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.size = Pt(max(size, 1))
    run.font.bold = bold
    run.font.name = font
    run.font.color.rgb = _rgb(colour)
    return box


def _rect(slide, x, y, w, h, fill: str, *, line: Optional[str] = None):
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(fill)
    if line is None:
        shape.line.fill.background()
    else:
        shape.line.color.rgb = _rgb(line)
    shape.shadow.inherit = False
    # No text in a plain rectangle: keep the frame empty so nothing can be
    # read from it by an accessibility tool.
    return shape


def _bullets(slide, x, y, w, h, items: Sequence[str], size: float, *, numbered: bool = False, colour: str = theme.INK):
    from pptx.util import Inches, Pt

    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    size = max(size, theme.SLIDE_MIN_BODY_PT)
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.space_after = Pt(size * BULLET_GAP_EM)  # the gap the plan's line budget assumes
        marker = f"{i + 1}.  " if numbered else "•  "
        run = p.add_run()
        run.text = marker
        run.font.size = Pt(size)
        run.font.bold = True
        run.font.name = theme.OFFICE_SANS
        run.font.color.rgb = _rgb(theme.TEAL if numbered else theme.ACCENT)
        run = p.add_run()
        run.text = item
        run.font.size = Pt(size)
        run.font.name = theme.OFFICE_SANS
        run.font.color.rgb = _rgb(colour)
    return box


def _slide_title(slide, s: PlannedSlide, plan: DeckPlan) -> None:
    from pptx.enum.text import MSO_ANCHOR

    t = plan.type
    m = theme.SLIDE_MARGIN_IN
    _text_box(slide, m, theme.SLIDE_TITLE_TOP_IN, theme.SLIDE_W_IN - 2 * m, theme.SLIDE_TITLE_H_IN, s.title,
              size=t.title, bold=True, colour=theme.NAVY, anchor=MSO_ANCHOR.BOTTOM)
    _rect(slide, m, theme.SLIDE_TITLE_TOP_IN + theme.SLIDE_TITLE_H_IN + 0.02, theme.SLIDE_W_IN - 2 * m, 0.03, theme.ACCENT)


def _footer(slide, s: PlannedSlide, plan: DeckPlan, total: int) -> None:
    from pptx.enum.text import PP_ALIGN

    t = plan.type
    m = theme.SLIDE_MARGIN_IN
    y = theme.SLIDE_FOOTER_TOP_IN
    w = theme.SLIDE_W_IN - 2 * m
    _rect(slide, m, y, w, 0.01, theme.BORDER)
    label = plan.spec.title + ("   CONFIDENTIAL" if plan.spec.confidential else "")
    _text_box(slide, m, y + 0.03, w * 0.75, theme.SLIDE_FOOTER_H_IN, label, size=t.small, colour=theme.INK_FAINT)
    _text_box(slide, m + w * 0.75, y + 0.03, w * 0.25, theme.SLIDE_FOOTER_H_IN, f"{s.number} / {total}",
              size=t.small, colour=theme.INK_FAINT, align=PP_ALIGN.RIGHT)


def _band_slide(prs, s: PlannedSlide, plan: DeckPlan) -> None:
    """title / section / closing: a full-bleed band in the template colour."""
    spec = plan.spec
    t = plan.type
    slide = prs.slides.add_slide(prs.slide_layouts[_BLANK_LAYOUT])
    bg = slide.background.fill
    bg.solid()
    bg.fore_color.rgb = _rgb(plan.band)
    title = s.title or (spec.title if s.layout == "title" else ("Thank you" if s.layout == "closing" else ""))
    subtitle = s.subtitle or (spec.subtitle if s.layout == "title" else "")
    _rect(slide, 0.9, 2.0, 1.2, 0.08, theme.TEAL)
    if s.layout == "section":
        _text_box(slide, 0.9, 2.9, theme.SLIDE_W_IN - 1.8, 1.4, title, size=t.section, bold=True, colour=theme.WHITE)
        if subtitle:
            _text_box(slide, 0.9, 4.4, theme.SLIDE_W_IN - 1.8, 1.0, subtitle, size=t.body + 4, colour=theme.WHITE)
    else:
        _text_box(slide, 0.9, 2.3, theme.SLIDE_W_IN - 1.8, 1.5, title, size=t.cover_title, bold=True, colour=theme.WHITE)
        if subtitle:
            _text_box(slide, 0.9, 3.9, theme.SLIDE_W_IN - 1.8, 1.0, subtitle, size=t.body + 4, colour=theme.WHITE)
        meta_bits: List[str] = []
        if s.layout == "title":
            meta_bits = [b for b in (spec.author, spec.date, spec.audience and f"Prepared for {spec.audience}") if b]
        elif s.bullets:
            meta_bits = list(s.bullets)
        if meta_bits:
            _text_box(slide, 0.9, 6.4, theme.SLIDE_W_IN - 1.8, 0.6, " · ".join(meta_bits), size=t.body, colour=theme.WHITE)
    _notes(slide, s)


def _notes(slide, s: PlannedSlide) -> None:
    if s.notes:
        slide.notes_slide.notes_text_frame.text = s.notes


def _chart_type(chart: S.Chart):
    from pptx.enum.chart import XL_CHART_TYPE

    return {
        "bar": XL_CHART_TYPE.COLUMN_CLUSTERED,
        "horizontal_bar": XL_CHART_TYPE.BAR_CLUSTERED,
        "line": XL_CHART_TYPE.LINE_MARKERS,
        "pie": XL_CHART_TYPE.PIE,
    }[chart.type]


def _native_chart(slide, x, y, w, h, chart: S.Chart, body_pt: float) -> None:
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_LEGEND_POSITION
    from pptx.util import Inches, Pt

    data = CategoryChartData()
    data.categories = list(chart.categories)
    for series in chart.series:
        data.add_series(series.name, [float(v) for v in series.values])
    frame = slide.shapes.add_chart(_chart_type(chart), Inches(x), Inches(y), Inches(w), Inches(h), data)
    c = frame.chart
    c.font.size = Pt(max(body_pt - 4, theme.SLIDE_MIN_BODY_PT - 2))
    c.font.name = theme.OFFICE_SANS
    c.font.color.rgb = _rgb(theme.INK_MUTED)
    c.has_title = bool(chart.title)
    if chart.title:
        c.chart_title.text_frame.text = chart.title
        para = c.chart_title.text_frame.paragraphs[0]
        para.runs[0].font.size = Pt(body_pt)
        para.runs[0].font.bold = True
        para.runs[0].font.color.rgb = _rgb(theme.INK)
    c.has_legend = len(chart.series) > 1 or chart.type == "pie"
    if c.has_legend:
        c.legend.position = XL_LEGEND_POSITION.BOTTOM
        c.legend.include_in_layout = False
    plot = c.plots[0]
    if chart.type == "pie":
        plot.has_data_labels = True
        plot.data_labels.number_format = "0.0%"
        plot.data_labels.number_format_is_linked = False
        plot.data_labels.show_percentage = True
        plot.data_labels.show_value = False
        points = plot.series[0].points
        for i in range(len(chart.categories)):
            points[i].format.fill.solid()
            points[i].format.fill.fore_color.rgb = _rgb(theme.series_colour(i))
    else:
        if len(chart.categories) <= theme.CHART_LABEL_MAX_CATEGORIES and chart.type != "line":
            plot.has_data_labels = True
            plot.data_labels.font.size = Pt(max(body_pt - 6, 10))
            plot.data_labels.font.color.rgb = _rgb(theme.INK_MUTED)
        for i, series in enumerate(plot.series):
            fill = series.format.line if chart.type == "line" else series.format.fill
            if chart.type == "line":
                fill.color.rgb = _rgb(theme.series_colour(i))
                fill.width = Pt(2.25)
                series.smooth = False
            else:
                fill.solid()
                fill.fore_color.rgb = _rgb(theme.series_colour(i))
        if chart.type != "pie":
            va = c.value_axis
            va.has_major_gridlines = True
            va.major_gridlines.format.line.color.rgb = _rgb(theme.BORDER)
            va.format.line.fill.background()
            if chart.y_label:
                va.has_title = True
                va.axis_title.text_frame.text = chart.y_label
                va.axis_title.text_frame.paragraphs[0].runs[0].font.size = Pt(max(body_pt - 6, 10))
            c.category_axis.format.line.color.rgb = _rgb(theme.BORDER)


def _native_table(slide, x, y, w, h, table: S.Table, body_pt: float) -> None:
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    numeric = set(table.numeric_columns)
    rows = len(table.rows) + 1
    cols = len(table.columns)
    # Rows past what fits at the minimum body size are not drawn smaller —
    # the plan caps slide tables to what a slide can hold (html.fit_table).
    row_h = min(0.45, h / rows)
    shape = slide.shapes.add_table(rows, cols, Inches(x), Inches(y), Inches(w), Inches(row_h * rows))
    tbl = shape.table
    size = Pt(max(body_pt - 2, theme.SLIDE_MIN_BODY_PT))

    def put(cell, text: str, *, bold: bool = False, colour: str = theme.INK, right: bool = False):
        cell.text = ""
        p = cell.text_frame.paragraphs[0]
        run = p.add_run()
        run.text = text
        run.font.size = size
        run.font.bold = bold
        run.font.name = theme.OFFICE_SANS
        run.font.color.rgb = _rgb(colour)
        if right:
            p.alignment = PP_ALIGN.RIGHT
        cell.margin_left = cell.margin_right = Inches(0.08)

    for j, name in enumerate(table.columns):
        cell = tbl.cell(0, j)
        cell.fill.solid()
        cell.fill.fore_color.rgb = _rgb(theme.NAVY)
        put(cell, name, bold=True, colour=theme.WHITE, right=j in numeric)
    for i, row in enumerate(table.rows, start=1):
        for j, value in enumerate(row):
            cell = tbl.cell(i, j)
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(theme.SURFACE if i % 2 == 0 else theme.WHITE)
            put(cell, cell_text(value, j in numeric), right=j in numeric)


def _kpi_boxes(slide, s: PlannedSlide, plan: DeckPlan) -> None:
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN

    t = plan.type
    m = theme.SLIDE_MARGIN_IN
    n = max(1, len(s.kpis))
    gap = 0.3
    w = (theme.SLIDE_W_IN - 2 * m - gap * (n - 1)) / n
    y = theme.SLIDE_BODY_TOP_IN + 0.7
    h = 2.4
    for i, k in enumerate(s.kpis):
        x = m + i * (w + gap)
        _rect(slide, x, y, w, h, theme.SURFACE)
        _rect(slide, x, y, w, 0.07, theme.ACCENT)
        _text_box(slide, x, y + 0.35, w, 1.0, k.value, size=t.kpi_value, bold=True, colour=theme.NAVY,
                  align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        _text_box(slide, x, y + 1.45, w, 0.45, k.label.upper(), size=t.kpi_label, colour=theme.INK_MUTED, align=PP_ALIGN.CENTER)
        if k.note:
            _text_box(slide, x, y + 1.9, w, 0.4, k.note, size=max(t.kpi_label - 2, theme.SLIDE_MIN_BODY_PT), colour=theme.INK_FAINT, align=PP_ALIGN.CENTER)


def _timeline(slide, s: PlannedSlide, plan: DeckPlan) -> None:
    from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches

    t = plan.type
    m = theme.SLIDE_MARGIN_IN
    n = max(1, len(s.steps))
    left = m
    width = theme.SLIDE_W_IN - 2 * m
    line_y = theme.SLIDE_BODY_TOP_IN + 1.6
    step_w = width / n
    connector = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(left), Inches(line_y), Inches(left + width), Inches(line_y))
    connector.line.color.rgb = _rgb(theme.BORDER)
    connector.line.width = Inches(0.06)
    for i, (label, text) in enumerate(s.steps):
        cx = left + step_w * i + step_w / 2
        dot = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(cx - 0.16), Inches(line_y - 0.16), Inches(0.32), Inches(0.32))
        dot.fill.solid()
        dot.fill.fore_color.rgb = _rgb(theme.ACCENT)
        dot.line.color.rgb = _rgb(theme.WHITE)
        dot.shadow.inherit = False
        _text_box(slide, left + step_w * i, line_y - 1.0, step_w, 0.6, label, size=t.body, bold=True, colour=theme.NAVY, align=PP_ALIGN.CENTER)
        _text_box(slide, left + step_w * i + 0.05, line_y + 0.35, step_w - 0.1, 2.0, text, size=max(t.body - 2, theme.SLIDE_MIN_BODY_PT),
                  colour=theme.INK_MUTED, align=PP_ALIGN.CENTER)


def _columns(slide, s: PlannedSlide, plan: DeckPlan, *, cards: bool) -> None:
    t = plan.type
    m = theme.SLIDE_MARGIN_IN
    gap = 0.4
    w = (theme.SLIDE_W_IN - 2 * m - gap) / 2
    y = theme.SLIDE_BODY_TOP_IN
    h = theme.SLIDE_BODY_H_IN
    for i, (title, items, default) in enumerate(((s.left_title, s.left, "Option A"), (s.right_title, s.right, "Option B"))):
        x = m + i * (w + gap)
        if cards:
            _rect(slide, x, y, w, h - 0.3, theme.SURFACE)
            _rect(slide, x, y, w, 0.06, theme.ACCENT if i == 0 else theme.TEAL)
            title = title or default
        inner_x = x + (0.25 if cards else 0)
        inner_w = w - (0.5 if cards else 0)
        top = y + (0.25 if cards else 0)
        if title:
            _text_box(slide, inner_x, top, inner_w, 0.5, title, size=t.body + 2, bold=True, colour=theme.BOARDROOM)
            if not cards:
                _rect(slide, inner_x, top + 0.5, inner_w, 0.015, theme.BORDER)
            top += 0.65
        _bullets(slide, inner_x, top, inner_w, h - (top - y) - 0.3, items, t.body)


def _sources_slide(prs, s: PlannedSlide, plan: DeckPlan, total: int) -> None:
    from pptx.util import Inches, Pt

    t = plan.type
    slide = prs.slides.add_slide(prs.slide_layouts[_BLANK_LAYOUT])
    _slide_title(slide, s, plan)
    m = theme.SLIDE_MARGIN_IN
    box = slide.shapes.add_textbox(Inches(m), Inches(theme.SLIDE_BODY_TOP_IN), Inches(theme.SLIDE_W_IN - 2 * m), Inches(theme.SLIDE_BODY_H_IN))
    tf = box.text_frame
    tf.word_wrap = True
    size = Pt(max(t.body - 3, theme.SLIDE_MIN_BODY_PT))
    for i, c in enumerate(plan.spec.sources):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.space_after = Pt(6)
        run = p.add_run()
        run.text = f"{i + 1}.  {c.title}"
        run.font.size = size
        run.font.name = theme.OFFICE_SANS
        run.font.color.rgb = _rgb(theme.INK)
        if c.url:
            r2 = p.add_run()
            r2.text = f"  —  {c.url}"
            r2.font.size = size
            r2.font.name = theme.OFFICE_SANS
            r2.font.color.rgb = _rgb(theme.ACCENT)
    _footer(slide, s, plan, total)


def _content_slide(prs, s: PlannedSlide, plan: DeckPlan, total: int) -> None:
    t = plan.type
    m = theme.SLIDE_MARGIN_IN
    slide = prs.slides.add_slide(prs.slide_layouts[_BLANK_LAYOUT])
    _slide_title(slide, s, plan)
    x, y, w, h = m, theme.SLIDE_BODY_TOP_IN, theme.SLIDE_W_IN - 2 * m, theme.SLIDE_BODY_H_IN
    numbered = plan.template_id == "training"
    if s.layout == "chart" and s.chart is not None:
        if s.bullets:
            _bullets(slide, x, y, 4.0, h, s.bullets, t.body, numbered=numbered)
            _native_chart(slide, x + 4.35, y, w - 4.35, h - 0.2, s.chart, t.body)
        else:
            _native_chart(slide, x + 0.5, y, w - 1.0, h - 0.2, s.chart, t.body)
        if s.chart.caption:
            _text_box(slide, x, y + h - 0.35, w, 0.35, s.chart.caption, size=theme.SLIDE_MIN_BODY_PT, colour=theme.INK_MUTED)
    elif s.layout == "table" and s.table is not None:
        _native_table(slide, x, y, w, h - 0.4, s.table, t.body)
        if s.table.caption:
            _text_box(slide, x, y + h - 0.35, w, 0.35, s.table.caption, size=theme.SLIDE_MIN_BODY_PT, colour=theme.INK_MUTED)
    elif s.layout == "kpis":
        _kpi_boxes(slide, s, plan)
    elif s.layout == "timeline":
        _timeline(slide, s, plan)
    elif s.layout == "two_column":
        _columns(slide, s, plan, cards=False)
    elif s.layout == "comparison":
        _columns(slide, s, plan, cards=True)
    else:
        if s.bullets:
            _bullets(slide, x, y, w, h, s.bullets, t.body, numbered=numbered)
    _footer(slide, s, plan, total)
    _notes(slide, s)


def render_pptx(spec: S.PresentationSpec, out_path: str | Path, *, plan: Optional[DeckPlan] = None) -> Path:
    """Write the deck to `out_path` and return it. Every layout decision —
    bullets trimmed, table rows cut — is the plan's, with its warnings; this
    writer makes none of its own, so the .pptx cannot differ from the preview."""
    from pptx import Presentation
    from pptx.util import Inches

    plan = plan or plan_deck(spec)
    prs = Presentation()
    prs.slide_width = Inches(theme.SLIDE_W_IN)
    prs.slide_height = Inches(theme.SLIDE_H_IN)
    total = len(plan.slides)
    for s in plan.slides:
        if s.layout in ("title", "section", "closing"):
            _band_slide(prs, s, plan)
        elif s.is_sources_slide:
            _sources_slide(prs, s, plan, total)
        else:
            _content_slide(prs, s, plan, total)
    props = prs.core_properties
    props.title = spec.title
    props.subject = spec.subtitle or spec.purpose
    props.author = spec.author or "TechSara Local AI"
    props.comments = "Generated by TechSara Local AI Artifact Studio"
    out = Path(out_path)
    prs.save(str(out))
    return out


__all__ = ["render_pptx"]
