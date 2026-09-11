"""The HTML renderer and the layout plans: escaping, structure per template,
hoisting, page-break hygiene, slide fallbacks and bullet trimming. Pure
string tests — no WeasyPrint here."""
from __future__ import annotations

import re

import pytest

from app.artifacts import spec as S
from app.artifacts import types as T
from app.artifacts.render import html as H
from app.artifacts.render import theme
from tests.test_artifact_render_samples import deck, document

# ------------------------------------------------------------- escaping --


def test_every_model_string_is_escaped():
    spec = document("generic").body
    out = H.document_html(spec)
    # The title carries <draft>, & and a quote; none may survive as markup.
    assert "<draft>" not in out and "&lt;draft&gt;" in out
    assert "<b>markup</b>" not in out and "&lt;b&gt;markup&lt;/b&gt;" in out
    # A bullet with markup in a deck.
    d = deck("generic").body
    dout = H.deck_html(d)
    assert "<with markup>" not in dout and "&lt;with markup&gt;" in dout


def test_hostile_strings_everywhere_stay_text():
    payload = '<script>alert(1)</script><img src="http://127.0.0.1:1/x">'
    short = '<script>alert(1)</script>'   # KPI values are capped at 40 characters
    spec = S.DocumentSpec(
        title=payload, subtitle=payload, author=payload, date=short, audience=payload, purpose=payload, template_id="sop",
        blocks=[
            S.Heading(level=1, text=payload), S.Paragraph(text=payload, sources=["s"]), S.Bullets(items=[payload]), S.Numbered(items=[payload]),
            S.Callout(kind="tip", title=payload, text=payload), S.KPIRow(items=[S.KPI(label=payload, value=short, note=payload)]),
            S.TableBlock(table=S.Table(columns=[payload], rows=[[payload]], caption=payload)),
            S.ChartBlock(chart=S.Chart(title=payload, categories=[payload], series=[S.Series(name=payload, values=[1])], caption=payload)),
        ],
        sources=[S.Citation(id="s", title=payload, url="https://example.com/?q=%3Cx%3E&a=1", note=payload)],
        assumptions=[payload], toc=True, cover=True, confidential=True,
    )
    out = H.document_html(spec)
    assert "<script>" not in out
    assert 'src="http' not in out
    assert out.count("&lt;script&gt;") >= 14
    # The one img in the page is the chart, by bare filename.
    assert re.findall(r'<img src="([^"]+)"', out) == ["chart-1.png"]


def test_chart_images_are_bare_filenames_in_order():
    spec = document("generic", sections=8).body  # sections 4 and 8 carry charts
    out = H.document_html(spec)
    assert re.findall(r'<img src="([^"]+)"', out) == ["chart-1.png", "chart-2.png"]
    assert H.chart_filename(3) == "chart-3.png"


# ------------------------------------------------------------ the plan --


def test_executive_report_hoists_kpis_and_forces_a_cover():
    plan = H.plan_document(document("executive_report").body)
    assert plan.cover is True
    assert plan.hoisted_kpis is not None
    assert not any(isinstance(b, S.KPIRow) for b in plan.blocks)
    assert plan.lede_index is not None and isinstance(plan.blocks[plan.lede_index], S.Paragraph)


def test_brief_has_no_cover_no_toc_and_tight_type():
    plan = H.plan_document(document("brief", toc=True, cover=True).body)
    assert plan.cover is False and plan.toc is False
    assert plan.type is theme.BRIEF_TYPE and plan.type.body >= 10.5
    assert plan.grid is theme.BRIEF_GRID


def test_sop_and_technical_report_number_headings():
    plan = H.plan_document(document("sop", sections=3).body)
    assert plan.numbered and plan.control_strip
    assert [h.number for h in plan.headings] == ["1", "1.1", "2", "2.1", "3", "3.1"]
    out = H.document_html(plan.spec, plan)
    assert 'class="tpl-sop doc numbered"' in out
    assert 'class="steps"' not in out  # no numbered block in this sample
    # The number in the page IS the plan's string (no CSS counter to drift).
    assert '<h2 id="h-2"><span class="hnum">1.1</span>' in out
    assert "counter-increment: h1" not in H.print_css() and "h1::before" not in H.print_css()
    generic = H.plan_document(document("generic", sections=3).body)
    assert [h.number for h in generic.headings] == [""] * 6
    assert 'class="hnum"' not in H.document_html(generic.spec, generic)


def test_a_skipped_heading_level_counts_as_its_first_entry():
    """1 → 1.1.1 (not 1.0.1) when a level-3 follows a level-1 directly, as
    Word numbers it; the DOCX and the page carry the same string."""
    spec = S.DocumentSpec(title="t", template_id="technical_report", blocks=[
        S.Heading(level=1, text="one"), S.Heading(level=3, text="deep"), S.Heading(level=2, text="sub"),
        S.Heading(level=3, text="deeper"), S.Heading(level=1, text="two"), S.Heading(level=2, text="sub2"),
    ])
    plan = H.plan_document(spec)
    assert [h.number for h in plan.headings] == ["1", "1.1.1", "1.2", "1.2.1", "2", "2.1"]
    assert "0" not in "".join(h.number for h in plan.headings)
    # A document that opens on a level-2 is "1.1", not "0.1".
    opens_deep = H.plan_document(S.DocumentSpec(title="t", template_id="sop", blocks=[S.Heading(level=2, text="x"), S.Heading(level=1, text="y")]))
    assert [h.number for h in opens_deep.headings] == ["1.1", "2"]


def test_templates_differ_in_structure_not_just_title():
    """Eight templates, eight distinct structural fingerprints."""
    prints = {}
    for template_id in T.DOCUMENT_TEMPLATES:
        plan = H.plan_document(document(template_id).body)
        out = H.document_html(plan.spec, plan)
        prints[template_id] = (
            plan.cover, plan.toc, plan.numbered, plan.control_strip, plan.hoisted_kpis is not None, plan.lede_index,
            plan.type.body, 'class="actions"' in out, "References" in out,
        )
    assert len(set(prints.values())) == len(T.DOCUMENT_TEMPLATES), prints


def test_meeting_summary_renders_bullets_as_action_items():
    out = H.document_html(document("meeting_summary").body)
    assert 'class="actions"' in out and "<section class=\"cover\"" not in out


def test_research_report_calls_sources_references():
    out = H.document_html(document("research_report").body)
    assert "<h2>References</h2>" in out


def test_toc_is_built_from_headings_with_code_made_anchors():
    plan = H.plan_document(document("generic", sections=2).body)
    out = H.document_html(plan.spec, plan)
    assert '<nav class="toc">' in out
    assert out.count('href="#h-') == 4 and 'id="h-1"' in out and 'id="h-4"' in out


def test_toc_without_headings_is_dropped_with_a_warning():
    spec = S.DocumentSpec(title="x", toc=True, blocks=[S.Paragraph(text="only prose")])
    plan = H.plan_document(spec)
    assert plan.toc is False and plan.warnings and "contents page" in plan.warnings[0]


def test_trailing_and_doubled_page_breaks_are_removed():
    spec = S.DocumentSpec(title="x", blocks=[S.Paragraph(text="a"), S.PageBreak(), S.PageBreak(), S.Paragraph(text="b"), S.PageBreak(), S.PageBreak()])
    plan = H.plan_document(spec)
    kinds = [type(b).__name__ for b in plan.blocks]
    assert kinds == ["Paragraph", "PageBreak", "Paragraph"]
    # With a sources section after the blocks the last break is kept: it
    # puts the sources on their own page, which is a real page, not a blank.
    with_tail = S.DocumentSpec(title="x", blocks=[S.Paragraph(text="a"), S.PageBreak()], sources=[S.Citation(id="s", title="t")])
    assert [type(b).__name__ for b in H.plan_document(with_tail).blocks] == ["Paragraph", "PageBreak"]


def test_numeric_columns_right_align_and_format_only_numeric_cells():
    t = S.Table(columns=["Year", "Revenue"], rows=[[2024, 1234567.891], ["2025", 5]], numeric_columns=[1])
    out = H._table_html(t, {})
    assert '<td class="txt">2024</td>' in out          # a year in a text column stays 2024
    assert '<td class="num">1,234,567.89</td>' in out
    assert '<td class="num">5</td>' in out
    assert '<th class="num">Revenue</th>' in out


def test_citation_markers_number_sources_in_manifest_order():
    spec = document("generic", sections=1).body
    plan = H.plan_document(spec)
    assert plan.citation_index == {"ledger": 1, "crm": 2}
    assert '<sup class="cite">[1]</sup>' in H.document_html(spec, plan)


# ---------------------------------------------------------------- decks --


def test_deck_plan_falls_back_to_bullets_when_the_layout_has_no_content():
    spec = S.PresentationSpec(title="d", slides=[
        S.Slide(layout="chart", title="no chart", bullets=["a"]),
        S.Slide(layout="table", title="no table"),
        S.Slide(layout="kpis", title="no kpis"),
        S.Slide(layout="timeline", title="no steps"),
        S.Slide(layout="two_column", title="no columns"),
    ])
    plan = H.plan_deck(spec)
    assert [s.layout for s in plan.slides] == ["bullets"] * 5
    assert len(plan.warnings) == 5 and all("laid out as bullets" in w for w in plan.warnings)


def test_bullets_over_the_template_cap_are_trimmed_with_a_warning():
    long = "x" * 300
    spec = S.PresentationSpec(title="d", template_id="ceo", slides=[S.Slide(layout="bullets", title="t", bullets=[long] + ["b"] * 7)])
    plan = H.plan_deck(spec)
    s = plan.slides[0]
    assert len(s.bullets) == theme.PRESENTATION_TEMPLATES["ceo"]["max_bullets"] == 4
    assert len(s.bullets[0]) == theme.PRESENTATION_TEMPLATES["ceo"]["max_bullet_chars"]
    assert s.bullets[0].endswith("…")
    assert any("shortened" in w for w in plan.warnings) and any("dropped" in w for w in plan.warnings)
    # ONE warning for the slide, not one per bullet.
    assert plan.warnings == ["Slide 1: 1 bullet shortened and 4 bullets dropped to fit the slide."]
    generic = H.plan_deck(S.PresentationSpec(title="d", slides=[S.Slide(layout="bullets", title="t", bullets=["b"] * 8)]))
    assert len(generic.slides[0].bullets) == 8 and not generic.warnings


def _line_budget_pt(layout: str = "bullets") -> float:
    return H.BULLET_BOXES[layout][1] * 72


def _estimated_height_pt(bullets, layout: str = "bullets", body_pt: float = theme.SLIDE_TYPE.body) -> float:
    """The plan's own estimate, recomputed here so a test can assert the
    fitted list is inside the box it was fitted to."""
    width_in = H.BULLET_BOXES[layout][0]
    cpl = int((width_in - H.BULLET_INDENT_IN) * 72 / (H.BULLET_CHAR_EM * body_pt))
    return sum(max(1, -(-len(t) // cpl)) * body_pt * H.BULLET_LINE_HEIGHT + body_pt * H.BULLET_GAP_EM for t in bullets)


def test_bullets_within_the_character_cap_still_fit_the_slides_line_budget():
    """Eight bullets of 159 characters are within the generic cap (8 x 160)
    and wrap to 16 lines at 18 pt, but the 5.1 in body box holds 12: the
    plan shortens the longest bullets a line at a time, keeps the count when
    it can, and says so once."""
    bullets = [(f"Bullet {k} " + "lorem ipsum dolor sit amet " * 6).strip()[:150].rstrip() + f" END{k}" for k in range(1, 9)]
    assert all(len(b) < theme.PRESENTATION_TEMPLATES["generic"]["max_bullet_chars"] for b in bullets)
    plan = H.plan_deck(S.PresentationSpec(title="d", slides=[S.Slide(layout="bullets", title="t", bullets=bullets)]))
    fitted = plan.slides[0].bullets
    assert _estimated_height_pt(fitted) <= _line_budget_pt()
    assert len(fitted) >= 6                          # shortened before dropped
    assert all(t.endswith("…") or t.endswith(f"END{k}") for k, t in enumerate(fitted, start=1))
    assert len(plan.warnings) == 1 and plan.warnings[0].startswith("Slide 1: ") and "shortened" in plan.warnings[0]
    # Short bullets are untouched, however many the cap allows.
    short = H.plan_deck(S.PresentationSpec(title="d", slides=[S.Slide(layout="bullets", title="t", bullets=[f"Point {k}: a concrete observation with a number ({k * 7}%)" for k in range(1, 9)])]))
    assert len(short.slides[0].bullets) == 8 and short.warnings == []


def test_column_and_chart_bullets_are_fitted_to_their_narrower_boxes():
    """The same 120-character bullets fit a full-width slide but wrap to more
    lines in a comparison card or beside a chart, so those are fitted to
    their own boxes and the slide gets one warning."""
    text = ("An observation about the quarter with a number, 12.5%, and a reason " * 2).strip()[:120]
    spec = S.PresentationSpec(title="d", template_id="training", slides=[
        S.Slide(layout="bullets", title="full", bullets=[text] * 4),
        S.Slide(layout="comparison", title="cards", left=[text] * 4, right=[text] * 4),
        S.Slide(layout="chart", title="beside", bullets=[text] * 4, chart=S.Chart(type="bar", categories=["a"], series=[S.Series(name="s", values=[1])])),
    ])
    plan = H.plan_deck(spec)
    assert plan.slides[0].bullets == [text] * 4 and not any(w.startswith("Slide 1") for w in plan.warnings)
    for n, layout, lists in ((2, "comparison", (plan.slides[1].left, plan.slides[1].right)), (3, "chart", (plan.slides[2].bullets,))):
        for fitted in lists:
            assert _estimated_height_pt(fitted, layout) <= _line_budget_pt(layout)
        assert sum(1 for w in plan.warnings if w.startswith(f"Slide {n}:")) == 1
    assert len(plan.warnings) == 2


def test_fit_bullets_shortens_at_a_word_boundary_and_aggregates():
    warnings: list = []
    out = H.fit_bullets(["word " * 40, "fine"], 8, 100, "Slide 9", warnings)
    assert out[0].endswith("…") and len(out[0]) <= 100 and not out[0].endswith("wor…")
    assert out[1] == "fine"
    assert warnings == ["Slide 9: 1 bullet shortened to fit the slide."]


def test_slide_tables_are_cut_in_the_plan():
    big = S.Table(columns=[f"c{i}" for i in range(12)], rows=[[f"Row {i}"] + [i] * 11 for i in range(1, 41)], numeric_columns=list(range(1, 12)))
    spec = S.PresentationSpec(title="d", slides=[S.Slide(layout="table", title="t", table=big)])
    plan = H.plan_deck(spec)
    t = plan.slides[0].table
    assert len(t.rows) == H.TABLE_MAX_ROWS == 10 and len(t.columns) == H.TABLE_MAX_COLS == 8
    assert t.numeric_columns == list(range(1, 8))
    assert t.rows[-1][0] == "Row 10"
    assert plan.warnings == [
        "Slide 1: the table has 12 columns; only the first 8 fit on the slide.",
        "Slide 1: the table has 40 rows; only the first 10 fit on the slide.",
    ]
    # The preview HTML draws the planned table: ten rows, not forty.
    out = H.deck_html(spec, plan)
    assert "Row 10" in out and "Row 11" not in out and out.count("<tr>") == 11


def test_sources_slide_is_appended_when_citations_exist():
    plan = H.plan_deck(deck().body)
    assert plan.slides[-1].is_sources_slide and plan.slides[-1].number == 13
    bare = deck().body.model_copy(update={"sources": []})
    for s in bare.slides:
        for field in ("chart", "table"):
            if getattr(s, field) is not None:
                getattr(s, field).sources = []
    assert len(H.plan_deck(bare).slides) == 12


def test_deck_html_has_one_section_per_planned_slide_at_16_by_9():
    spec = deck("ceo").body
    plan = H.plan_deck(spec)
    out = H.deck_html(spec, plan)
    assert out.count('<section class="slide') == len(plan.slides)
    assert f"@page{{size:{theme.SLIDE_W_IN}in {theme.SLIDE_H_IN}in;margin:0}}" in out
    assert out.count('class="slide band') == 3   # title, section, closing
    assert 'class="slide layout-timeline' in out and 'class="kpi-box"' in out
    assert re.findall(r'<img src="([^"]+)"', out) == ["chart-1.png", "chart-2.png", "chart-3.png"]
    assert out.count(" last") == 1


def test_presentation_templates_differ():
    prints = {}
    for template_id in T.PRESENTATION_TEMPLATES:
        plan = H.plan_deck(deck(template_id).body)
        prints[template_id] = (plan.band, plan.type.kpi_value, plan.template["max_bullets"], len(plan.slides[3].bullets))
    assert len(set(prints.values())) == len(T.PRESENTATION_TEMPLATES)
    assert prints["ceo"][1] == theme.CEO_SLIDE_TYPE.kpi_value == 60


def test_spec_charts_walks_documents_and_decks_only():
    assert len(H.spec_charts(document("generic", sections=8))) == 2
    assert len(H.spec_charts(deck())) == 3
    from tests.test_artifact_render_samples import workbook

    assert H.spec_charts(workbook()) == []


def test_workbook_summary_html_shows_first_rows_only():
    from tests.test_artifact_render_samples import workbook

    out = H.workbook_summary_html(workbook(rows=100).body, max_rows=5)
    assert "100 rows in the sheet; the first 5 are shown." in out
    assert out.count("<h2>") == 3
    assert "&lt;draft&gt;" not in out and "evil.example" in out and "<script" not in out


def test_theme_reports_scripts_the_fonts_cannot_draw():
    assert theme.unsupported_scripts("Plain ASCII, café, €5, 10 ≥ 3 → ok") == []
    assert theme.unsupported_scripts("नमस्ते and 你好 and ગુજરાતી") == ["Devanagari", "CJK", "Gujarati"]
    assert "Devanagari" in theme.font_coverage_warning("नमस्ते")
    assert theme.font_coverage_warning("hello") == ""


def test_format_number():
    assert H.format_number(1234567) == "1,234,567"
    assert H.format_number(1234.0) == "1,234"
    assert H.format_number(1234.5) == "1,234.50"
    assert H.format_number(None) == ""
    assert H.format_number("as is") == "as is"


@pytest.mark.parametrize("kind", ["note", "tip", "warning", "quote"])
def test_callout_kinds_have_colours(kind):
    out = H._callout_html(S.Callout(kind=kind, text="t"))
    accent, bg = theme.CALLOUT_COLOURS[kind]
    assert accent in out and bg in out
