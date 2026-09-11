"""The PPTX writer: 16:9, one slide per planned slide, native charts and
tables, notes, footers, a sources slide, no text under 14 pt, no macros."""
from __future__ import annotations

import zipfile

import pytest

from app.artifacts import spec as S
from app.artifacts import types as T
from app.artifacts.render import html as H
from app.artifacts.render import theme
from tests.test_artifact_render_samples import deck, region_table, revenue_chart

pytest.importorskip("pptx")


def render(spec: S.ArtifactSpec, tmp_path, name="deck.pptx"):
    """Returns (path, plan, plan.warnings): every layout decision, and every
    warning about one, is the plan's — the writer has no warnings of its own."""
    from app.artifacts.render.pptx import render_pptx

    plan = H.plan_deck(spec.body)
    path = render_pptx(spec.body, tmp_path / name, plan=plan)
    return path, plan, plan.warnings


def all_runs(prs):
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for p in shape.text_frame.paragraphs:
                    for r in p.runs:
                        yield slide, shape, r
            if shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        for p in cell.text_frame.paragraphs:
                            for r in p.runs:
                                yield slide, shape, r


@pytest.mark.parametrize("template_id", T.PRESENTATION_TEMPLATES)
def test_every_template_renders_one_slide_per_planned_slide(tmp_path, template_id):
    from pptx import Presentation
    from pptx.util import Inches

    path, plan, warnings = render(deck(template_id), tmp_path)
    prs = Presentation(str(path))
    assert len(prs.slides) == len(plan.slides) == 13
    assert prs.slide_width == Inches(theme.SLIDE_W_IN) and prs.slide_height == Inches(theme.SLIDE_H_IN)
    assert abs(prs.slide_width / prs.slide_height - 16 / 9) < 0.001
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None
        names = z.namelist()
        rels = b"".join(z.read(n) for n in names if n.endswith(".rels"))
    assert not any("vbaProject" in n for n in names)
    assert b'TargetMode="External"' not in rels
    # Three native charts (line, pie, horizontal bar), one native table.
    assert sum(1 for n in names if n.startswith("ppt/charts/chart") and n.endswith(".xml")) == 3
    assert sum(1 for s in prs.slides for sh in s.shapes if sh.has_table) == 1


def test_body_text_never_below_14pt_and_titles_large(tmp_path):
    from pptx import Presentation
    from pptx.util import Pt

    path, plan, _ = render(deck("ceo"), tmp_path)
    prs = Presentation(str(path))
    sizes = []
    for slide, shape, run in all_runs(prs):
        if run.font.size is not None and run.font.size >= Pt(theme.SLIDE_MIN_BODY_PT):
            sizes.append(run.font.size.pt)
        elif run.font.size is not None:
            # Only the footer line is smaller than body text.
            assert run.font.size == Pt(plan.type.small), (run.text, run.font.size.pt)
    assert max(sizes) >= plan.type.cover_title == 44
    assert plan.type.kpi_value == 60 and 60.0 in sizes


def test_native_chart_carries_the_data_and_the_palette(tmp_path):
    from pptx import Presentation
    from pptx.enum.chart import XL_CHART_TYPE

    spec = S.ArtifactSpec(kind="presentation", presentation=S.PresentationSpec(title="d", slides=[
        S.Slide(layout="chart", title="bar", chart=revenue_chart()),
        S.Slide(layout="chart", title="hbar", chart=revenue_chart(type="horizontal_bar")),
        S.Slide(layout="chart", title="line", chart=revenue_chart(type="line")),
        S.Slide(layout="chart", title="pie", chart=S.Chart(type="pie", categories=["a", "b"], series=[S.Series(name="s", values=[1, 3])])),
    ], sources=[S.Citation(id="ledger", title="Ledger")]))
    path, _, _ = render(spec, tmp_path)
    prs = Presentation(str(path))
    charts = [sh.chart for s in prs.slides for sh in s.shapes if sh.has_chart]
    assert [c.chart_type for c in charts] == [
        XL_CHART_TYPE.COLUMN_CLUSTERED, XL_CHART_TYPE.BAR_CLUSTERED, XL_CHART_TYPE.LINE_MARKERS, XL_CHART_TYPE.PIE,
    ]
    bar = charts[0]
    assert list(bar.plots[0].categories) == ["Q1", "Q2", "Q3", "Q4"]
    assert [s.name for s in bar.series] == ["FY25", "FY26"]
    assert list(bar.series[1].values) == [130.0, 150.0, 170.0, 210.0]
    assert str(bar.series[0].format.fill.fore_color.rgb) == theme.PALETTE[0].lstrip("#").upper()
    assert bar.has_legend and not charts[2].plots[0].series[0].smooth
    assert charts[3].has_legend  # a pie always gets a legend


def test_native_table_header_shading_and_numeric_alignment(tmp_path):
    from pptx import Presentation
    from pptx.enum.text import PP_ALIGN

    spec = S.ArtifactSpec(kind="presentation", presentation=S.PresentationSpec(title="d", slides=[S.Slide(layout="table", title="t", table=region_table())], sources=[S.Citation(id="crm", title="crm")]))
    path, _, _ = render(spec, tmp_path)
    prs = Presentation(str(path))
    table = next(sh.table for sh in prs.slides[0].shapes if sh.has_table)
    assert len(table.rows) == 5 and len(table.columns) == 4
    assert str(table.cell(0, 0).fill.fore_color.rgb) == "0A1D37"
    assert table.cell(1, 1).text == "120,000"
    assert table.cell(1, 1).text_frame.paragraphs[0].alignment == PP_ALIGN.RIGHT
    assert table.cell(1, 0).text_frame.paragraphs[0].alignment != PP_ALIGN.RIGHT


def test_oversized_table_is_cut_by_the_plan_with_a_warning(tmp_path):
    """The cut is the PLAN's (html.fit_table), so the .pptx and the preview
    PDF carry the same ten rows; the writer draws exactly the planned table."""
    from pptx import Presentation

    big = S.Table(columns=[f"c{i}" for i in range(12)], rows=[[i] * 12 for i in range(30)])
    spec = S.ArtifactSpec(kind="presentation", presentation=S.PresentationSpec(title="d", slides=[S.Slide(layout="table", title="t", table=big)]))
    path, plan, warnings = render(spec, tmp_path)
    assert len(plan.slides[0].table.rows) == 10 and len(plan.slides[0].table.columns) == 8
    prs = Presentation(str(path))
    table = next(sh.table for sh in prs.slides[0].shapes if sh.has_table)
    assert len(table.rows) == 11 and len(table.columns) == 8
    assert [table.cell(r, 0).text for r in range(1, 11)] == [str(i) for i in range(10)]
    assert len(warnings) == 2 and all("fit on the slide" in w for w in warnings)
    # An untouched table is the spec's own object: nothing copied, nothing cut.
    small = S.ArtifactSpec(kind="presentation", presentation=S.PresentationSpec(title="d", slides=[S.Slide(layout="table", title="t", table=region_table())], sources=[S.Citation(id="crm", title="crm")]))
    _, plan2, warnings2 = render(small, tmp_path, "small.pptx")
    assert plan2.slides[0].table is small.body.slides[0].table and warnings2 == []


def test_bullets_the_plan_fitted_are_the_bullets_written(tmp_path):
    """Eight 155-character bullets do not fit the body box at 18 pt; the
    writer draws the plan's fitted list, never the spec's, with the gap the
    plan's line budget assumed."""
    from pptx import Presentation
    from pptx.util import Pt

    bullets = [(f"Bullet {k} " + "lorem ipsum dolor sit amet " * 6).strip()[:150].rstrip() + f" END{k}" for k in range(1, 9)]
    spec = S.ArtifactSpec(kind="presentation", presentation=S.PresentationSpec(title="d", slides=[S.Slide(layout="bullets", title="t", bullets=bullets)]))
    path, plan, warnings = render(spec, tmp_path)
    assert len(warnings) == 1 and warnings[0].startswith("Slide 1:")
    prs = Presentation(str(path))
    body = next(sh.text_frame for sh in prs.slides[0].shapes if sh.has_text_frame and "Bullet 1" in sh.text_frame.text)
    written = ["".join(r.text for r in p.runs[1:]) for p in body.paragraphs]
    assert written == plan.slides[0].bullets
    assert body.paragraphs[0].space_after == Pt(plan.type.body * H.BULLET_GAP_EM)


def test_notes_footer_sources_slide_and_fallback(tmp_path):
    from pptx import Presentation

    path, plan, _ = render(deck("training"), tmp_path)
    prs = Presentation(str(path))
    assert prs.slides[3].notes_slide.notes_text_frame.text == "Speaker notes for slide four."
    footer_texts = [sh.text_frame.text for sh in prs.slides[3].shapes if sh.has_text_frame]
    assert "4 / 13" in footer_texts and any("CONFIDENTIAL" in t for t in footer_texts)
    # The title slide has no footer.
    assert not any(t.endswith("/ 13") for t in (sh.text_frame.text for sh in prs.slides[0].shapes if sh.has_text_frame))
    last = [sh.text_frame.text for sh in prs.slides[12].shapes if sh.has_text_frame]
    assert "Sources" in last and any("CRM pipeline report" in t and "https://example.com/crm" in t for t in last)
    # Training decks number their bullets.
    body = next(sh.text_frame for sh in prs.slides[3].shapes if sh.has_text_frame and "Bullet 1" in sh.text_frame.text)
    assert body.paragraphs[0].runs[0].text == "1.  "
    # A layout without its content falls back to bullets (plan warning) and still renders.
    spec = S.ArtifactSpec(kind="presentation", presentation=S.PresentationSpec(title="d", slides=[S.Slide(layout="kpis", title="empty", bullets=["fallback"])]))
    path2, plan2, _ = render(spec, tmp_path, "fb.pptx")
    assert plan2.slides[0].layout == "bullets"
    assert any("fallback" in sh.text_frame.text for sh in Presentation(str(path2)).slides[0].shapes if sh.has_text_frame)


def test_timeline_and_kpis_are_shapes(tmp_path):
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    path, plan, _ = render(deck("quarterly_review"), tmp_path)
    prs = Presentation(str(path))
    timeline = prs.slides[9]
    kinds = [sh.shape_type for sh in timeline.shapes]
    assert MSO_SHAPE_TYPE.LINE in kinds                       # the connector
    assert sum(1 for sh in timeline.shapes if sh.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE) >= 4   # the dots (+ the rules)
    kpi_slide = prs.slides[1]
    values = [sh.text_frame.text for sh in kpi_slide.shapes if sh.has_text_frame]
    assert {"$1.4M", "14", "112%", "3.1%"} <= set(values)


def test_core_properties(tmp_path):
    from pptx import Presentation

    path, _, _ = render(deck(), tmp_path)
    props = Presentation(str(path)).core_properties
    assert props.title == "FY26 Quarterly Review" and props.author == "Finance"
