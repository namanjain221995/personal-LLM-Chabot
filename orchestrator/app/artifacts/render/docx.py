"""DocumentSpec → .docx with python-docx, following the same plan as the PDF.

WHY python-docx AND NOT PANDOC. The legacy report path converts Markdown with
pandoc and ships pandoc's reference.docx styles (CURRENT_STATE.md: "no
--reference-doc"). Here the DOCX is built from the plan html.py made — the
same hoisted KPI row, the same heading numbers, the same citation numbers,
the same sources and assumptions sections — so a person who opens the .docx
next to the .pdf sees the same document, block for block. Every paragraph
gets a NAMED style (Title, Subtitle, Heading 1-3, Body, Caption, Callout,
Bullet, Number) so the file behaves like one a person made: the navigation
pane works, "update table of contents" works, and restyling is one click.

WHAT WORD DOES THAT WE CANNOT. Page numbers are FIELDS (PAGE / NUMPAGES)
that Word evaluates on open — python-docx cannot know the page count, and a
number typed into the footer would be a lie. The contents page is a TOC
field for the same reason; Word offers to update it on open, and the PDF
twin already shows the real page numbers.

python-docx is imported lazily: tests/test_imports.py asserts the app
imports without heavy or optional libraries.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .. import spec as S
from . import theme
from .html import DocumentPlan, cell_text, chart_filename, plan_document

_OXML = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _qn(tag: str) -> str:
    from docx.oxml.ns import qn

    return qn(tag)


def _rgb(colour: str):
    from docx.shared import RGBColor

    return RGBColor(*theme.hex_to_rgb(colour))


def _shade(cell, fill: str) -> None:
    """Cell background: python-docx has no API for w:shd, so write the element."""
    from docx.oxml import OxmlElement

    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(_qn("w:val"), "clear")
    shd.set(_qn("w:color"), "auto")
    shd.set(_qn("w:fill"), fill.lstrip("#").upper())
    tc_pr.append(shd)


def _field(paragraph, instruction: str, placeholder: str = "") -> None:
    """A complex field (PAGE, NUMPAGES, TOC ...): begin / instrText / separate
    / placeholder result / end. Word recomputes it on open."""
    from docx.oxml import OxmlElement

    def run_with(child_tag: str, text: Optional[str] = None, **attrs: str):
        run = paragraph.add_run()
        el = OxmlElement(child_tag)
        for k, v in attrs.items():
            el.set(_qn(k), v)
        if text is not None:
            el.text = text
        run._r.append(el)
        return run

    run_with("w:fldChar", **{"w:fldCharType": "begin"})
    run_with("w:instrText", instruction, **{"xml:space": "preserve"})
    run_with("w:fldChar", **{"w:fldCharType": "separate"})
    paragraph.add_run(placeholder)
    run_with("w:fldChar", **{"w:fldCharType": "end"})


def _ensure_styles(document, t: theme.TypeScale) -> None:
    """Named styles, created or restyled to the theme. python-docx's default
    template already defines Title/Subtitle/Heading n/Caption/List Bullet/
    List Number; Body and Callout are ours."""
    from docx.enum.style import WD_STYLE_TYPE
    from docx.shared import Pt

    styles = document.styles

    def para_style(name: str, base: Optional[str], size: float, *, bold: bool = False, colour: str = theme.INK,
                   font: str = theme.OFFICE_SANS, space_before: float = 0, space_after: float = 6, italic: bool = False):
        try:
            st = styles[name]
        except KeyError:
            st = styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
            if base:
                st.base_style = styles[base]
        st.font.name = font
        st.font.size = Pt(size)
        st.font.bold = bold
        st.font.italic = italic
        st.font.color.rgb = _rgb(colour)
        # East-Asian font slot, or Word substitutes its own for non-Latin runs.
        rpr = st.element.get_or_add_rPr()
        rfonts = rpr.find(_qn("w:rFonts"))
        if rfonts is None:
            from docx.oxml import OxmlElement

            rfonts = OxmlElement("w:rFonts")
            rpr.append(rfonts)
        for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            rfonts.set(_qn(attr), font)
        # The default template's Title/Heading styles name THEME fonts, which
        # win over an explicit family (measured: the title came out in the
        # theme's Calibri-Light substitute, not Arial). Drop them.
        for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme"):
            rfonts.attrib.pop(_qn(attr), None)
        pf = st.paragraph_format
        pf.space_before = Pt(space_before)
        pf.space_after = Pt(space_after)
        pf.line_spacing = t.line_height
        return st

    normal = para_style("Normal", None, t.body)
    para_style("Body", "Normal", t.body)
    para_style("Title", "Normal", t.title, bold=True, colour=theme.NAVY, space_after=4)
    para_style("Subtitle", "Normal", t.subtitle, colour=theme.INK_MUTED, space_after=10)
    h1 = para_style("Heading 1", "Normal", t.h1, bold=True, colour=theme.NAVY, space_before=18, space_after=8)
    para_style("Heading 2", "Normal", t.h2, bold=True, colour=theme.NAVY, space_before=14, space_after=6)
    para_style("Heading 3", "Normal", t.h3, bold=True, colour=theme.BOARDROOM, space_before=10, space_after=4)
    para_style("Caption", "Normal", t.caption, colour=theme.INK_MUTED, space_after=10, italic=False)
    para_style("Callout", "Normal", t.body, space_after=8)
    para_style("Bullet", "List Bullet", t.body, space_after=3)
    para_style("Number", "List Number", t.body, space_after=3)
    para_style("Lede", "Normal", t.body + 1.5, colour=theme.BOARDROOM, space_after=10)
    para_style("KPI Value", "Normal", t.kpi_value, bold=True, colour=theme.NAVY, space_after=0)
    para_style("KPI Label", "Normal", t.small, colour=theme.INK_MUTED, space_after=0)
    para_style("Header", "Normal", t.small, colour=theme.INK_MUTED, space_after=0)
    para_style("Footer", "Normal", t.caption, colour=theme.INK_FAINT, space_after=0)
    para_style("Table Text", "Normal", t.small, space_after=0)
    for st in (h1,):
        st.paragraph_format.keep_with_next = True
    normal.paragraph_format.widow_control = True


def _set_margins(document, grid: theme.PageGrid, orientation: str) -> None:
    from docx.enum.section import WD_ORIENT
    from docx.shared import Mm

    section = document.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    if orientation == "landscape":
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width, section.page_height = Mm(297), Mm(210)
    section.top_margin = Mm(grid.top_mm)
    section.bottom_margin = Mm(grid.bottom_mm)
    section.left_margin = Mm(grid.left_mm)
    section.right_margin = Mm(grid.right_mm)
    section.header_distance = Mm(10)
    section.footer_distance = Mm(10)


def _header_footer(document, spec: S.DocumentSpec, plan: DocumentPlan) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    section = document.sections[0]
    if plan.cover:
        section.different_first_page_header_footer = True  # the cover carries none
    if plan.grid.header:
        header = section.header
        p = header.paragraphs[0]
        p.style = document.styles["Header"]
        run = p.add_run(spec.title)
        run.bold = True
        run.font.color.rgb = _rgb(theme.INK)
        if spec.confidential:
            p.add_run("\t\tCONFIDENTIAL").font.color.rgb = _rgb(theme.DANGER)
    if plan.grid.footer:
        footer = section.footer
        p = footer.paragraphs[0]
        p.style = document.styles["Footer"]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        left = " · ".join(b for b in (spec.author, spec.date) if b)
        if left:
            p.add_run(left + "     ")
        p.add_run("Page ")
        _field(p, "PAGE", "1")
        p.add_run(" of ")
        _field(p, "NUMPAGES", "1")


def _cover(document, spec: S.DocumentSpec, plan: DocumentPlan) -> None:
    from docx.enum.text import WD_BREAK
    from docx.shared import Pt

    kicker = document.add_paragraph(plan.template_id.replace("_", " ").upper(), style="KPI Label")
    kicker.paragraph_format.space_before = Pt(120)
    document.add_paragraph(spec.title, style="Title")
    if spec.subtitle:
        document.add_paragraph(spec.subtitle, style="Subtitle")
    meta = document.add_paragraph(style="Body")
    meta.paragraph_format.space_before = Pt(60)
    for label, value in (("Prepared for", spec.audience), ("Prepared by", spec.author), ("Date", spec.date), ("Purpose", spec.purpose)):
        if value:
            r = meta.add_run(f"{label}:  ")
            r.font.color.rgb = _rgb(theme.INK_FAINT)
            meta.add_run(value + "\n")
    if spec.confidential:
        c = document.add_paragraph(style="KPI Label")
        c.add_run("CONFIDENTIAL").font.color.rgb = _rgb(theme.DANGER)
    document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def _kpis(document, row: S.KPIRow) -> None:
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    table = document.add_table(rows=1, cols=len(row.items))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for cell, kpi in zip(table.rows[0].cells, row.items):
        _shade(cell, theme.SURFACE)
        p = cell.paragraphs[0]
        p.style = document.styles["KPI Value"]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run(kpi.value)
        lab = cell.add_paragraph(kpi.label.upper(), style="KPI Label")
        lab.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if kpi.note:
            note = cell.add_paragraph(kpi.note, style="KPI Label")
            note.alignment = WD_ALIGN_PARAGRAPH.CENTER
    document.add_paragraph(style="Body")


def _table(document, table: S.Table, cite: str) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    numeric = set(table.numeric_columns)
    t = document.add_table(rows=1, cols=len(table.columns))
    t.style = document.styles["Table Grid"]
    for i, (cell, name) in enumerate(zip(t.rows[0].cells, table.columns)):
        _shade(cell, theme.NAVY)
        p = cell.paragraphs[0]
        p.style = document.styles["Table Text"]
        run = p.add_run(name)
        run.bold = True
        run.font.color.rgb = _rgb(theme.WHITE)
        if i in numeric:
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for r_index, row in enumerate(table.rows):
        cells = t.add_row().cells
        for i, (cell, value) in enumerate(zip(cells, row)):
            p = cell.paragraphs[0]
            p.style = document.styles["Table Text"]
            p.add_run(cell_text(value, i in numeric))
            if i in numeric:
                p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            if r_index % 2 == 1:
                _shade(cell, theme.SURFACE)
    # Repeat the header row on every page.
    from docx.oxml import OxmlElement

    tr_pr = t.rows[0]._tr.get_or_add_trPr()
    hdr = OxmlElement("w:tblHeader")
    hdr.set(_qn("w:val"), "true")
    tr_pr.append(hdr)
    if table.caption or cite:
        document.add_paragraph((table.caption + cite).strip(), style="Caption")
    else:
        document.add_paragraph(style="Body")


def _chart(document, chart: S.Chart, image: Optional[str], cite: str, missing: List[str]) -> None:
    from docx.shared import Inches

    if image and Path(image).is_file():
        document.add_picture(str(image), width=Inches(6.3))
    else:
        missing.append(chart.title or "chart")
        document.add_paragraph(f"[Chart not available: {chart.title or 'untitled'}]", style="Caption")
    if chart.caption or cite:
        document.add_paragraph((chart.caption + cite).strip(), style="Caption")


def _callout(document, c: S.Callout) -> None:
    from docx.shared import Pt

    accent, bg = theme.CALLOUT_COLOURS.get(c.kind, theme.CALLOUT_COLOURS["note"])
    table = document.add_table(rows=1, cols=1)
    cell = table.rows[0].cells[0]
    _shade(cell, bg)
    p = cell.paragraphs[0]
    p.style = document.styles["Callout"]
    if c.kind != "quote" or c.title:
        title = p.add_run((c.title or c.kind.capitalize()).upper() + "\n")
        title.bold = True
        title.font.size = Pt(theme.DOCUMENT_TYPE.small)
        title.font.color.rgb = _rgb(accent)
    body = p.add_run(c.text)
    if c.kind == "quote":
        body.italic = True
        body.font.name = theme.OFFICE_SERIF
    document.add_paragraph(style="Body")


def _list_number_abstract_id(document) -> Optional[int]:
    """The abstractNumId behind the built-in 'List Number' style, or None
    when the template has no numbering for it."""
    style = document.styles["List Number"]
    ppr = style.element.pPr
    num_pr = ppr.numPr if ppr is not None else None
    if num_pr is None or num_pr.numId is None:
        return None
    numbering = document.part.numbering_part.numbering_definitions._numbering
    num = numbering.num_having_numId(num_pr.numId.val)
    return int(num.abstractNumId.val)


def _restart_numbering(document, paragraphs: Sequence, abstract_id: Optional[int]) -> None:
    """Give one Numbered block its OWN <w:num> so it starts at 1. Every
    paragraph in the 'Number' style otherwise shares the single numbering
    instance of 'List Number', and the second list in an SOP continued
    "3. 4." where the PDF (a fresh <ol>) showed "1. 2." (review 2026-09-11)."""
    if abstract_id is None or not paragraphs:
        return
    numbering = document.part.numbering_part.numbering_definitions._numbering
    num = numbering.add_num(abstract_id)
    num.add_lvlOverride(ilvl=0).add_startOverride(1)
    for p in paragraphs:
        num_pr = p._p.get_or_add_pPr().get_or_add_numPr()
        num_pr.get_or_add_ilvl().val = 0
        num_pr.get_or_add_numId().val = num.numId


def _cite_text(ids: Sequence[str], index: Dict[str, int]) -> str:
    nums = sorted({index[i] for i in ids if i in index})
    return f" [{', '.join(str(n) for n in nums)}]" if nums else ""


def render_docx(spec: S.DocumentSpec, out_path: str | Path, chart_dir: str | Path, *,
                plan: Optional[DocumentPlan] = None, warnings: Optional[List[str]] = None) -> Path:
    """Write the document to `out_path`. Chart PNGs are read from
    `chart_dir` as `chart-<n>.png` (rendered first by charts.py)."""
    from docx import Document
    from docx.enum.text import WD_BREAK

    plan = plan or plan_document(spec)
    warnings = warnings if warnings is not None else []
    index = plan.citation_index
    chart_dir = Path(chart_dir)
    missing: List[str] = []

    document = Document()
    _ensure_styles(document, plan.type)
    _set_margins(document, plan.grid, spec.orientation)
    _header_footer(document, spec, plan)

    props = document.core_properties
    props.title = spec.title
    props.subject = spec.subtitle or spec.purpose
    props.author = spec.author or "TechSara Local AI"
    props.created = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    props.comments = "Generated by TechSara Local AI Artifact Studio"

    if plan.cover:
        _cover(document, spec, plan)
    else:
        document.add_paragraph(spec.title, style="Title")
        if spec.subtitle:
            document.add_paragraph(spec.subtitle, style="Subtitle")
        byline = " · ".join(b for b in (spec.author, spec.date, spec.audience and f"For {spec.audience}") if b)
        if byline:
            document.add_paragraph(byline, style="Caption")

    if plan.toc:
        document.add_paragraph("Contents", style="Heading 1")
        p = document.add_paragraph(style="Body")
        _field(p, 'TOC \\o "1-3" \\h \\z \\u', "Right-click and choose Update Field to build the table of contents.")
        document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    if plan.control_strip:
        rows = [("Document", spec.title)]
        for k, v in (("Owner", spec.author), ("Date", spec.date), ("Audience", spec.audience), ("Purpose", spec.purpose)):
            if v:
                rows.append((k, v))
        t = document.add_table(rows=0, cols=2)
        t.style = document.styles["Table Grid"]
        for k, v in rows:
            cells = t.add_row().cells
            _shade(cells[0], theme.SURFACE)
            cells[0].paragraphs[0].style = document.styles["Table Text"]
            cells[0].paragraphs[0].add_run(k.upper()).font.color.rgb = _rgb(theme.INK_MUTED)
            cells[1].paragraphs[0].style = document.styles["Table Text"]
            cells[1].paragraphs[0].add_run(v)
        document.add_paragraph(style="Body")

    if plan.hoisted_kpis is not None:
        _kpis(document, plan.hoisted_kpis)

    heading_by_index = {h.index: h for h in plan.headings}
    list_number_abstract = _list_number_abstract_id(document)
    chart_ordinal = 0
    for i, b in enumerate(plan.blocks):
        if isinstance(b, S.Heading):
            h = heading_by_index[i]
            text = f"{h.number}  {b.text}" if h.number else b.text
            document.add_paragraph(text, style=f"Heading {b.level}")
        elif isinstance(b, S.Paragraph):
            style = "Lede" if i == plan.lede_index else "Body"
            document.add_paragraph(b.text + _cite_text(b.sources, index), style=style)
        elif isinstance(b, S.Numbered):
            _restart_numbering(document, [document.add_paragraph(item, style="Number") for item in b.items], list_number_abstract)
            if b.sources:
                document.add_paragraph(_cite_text(b.sources, index).strip(), style="Caption")
        elif isinstance(b, S.Bullets):
            for item in b.items:
                document.add_paragraph(item, style="Bullet")
            if b.sources:
                document.add_paragraph(_cite_text(b.sources, index).strip(), style="Caption")
        elif isinstance(b, S.TableBlock):
            _table(document, b.table, _cite_text(b.table.sources, index))
        elif isinstance(b, S.ChartBlock):
            chart_ordinal += 1
            _chart(document, b.chart, str(chart_dir / chart_filename(chart_ordinal)), _cite_text(b.chart.sources, index), missing)
        elif isinstance(b, S.Callout):
            _callout(document, b)
        elif isinstance(b, S.KPIRow):
            _kpis(document, b)
        elif isinstance(b, S.PageBreak):
            document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    if spec.sources:
        document.add_paragraph("References" if plan.template_id == "research_report" else "Sources", style="Heading 2")
        for n, c in enumerate(spec.sources, start=1):
            bits = [c.title]
            if c.url:
                bits.append(c.url)
            if c.retrieved_at:
                bits.append(f"retrieved {c.retrieved_at}")
            if c.note:
                bits.append(c.note)
            document.add_paragraph(f"[{n}] " + " — ".join(bits), style="Body")
    if spec.assumptions:
        document.add_paragraph("Assumptions", style="Heading 2")
        for a in spec.assumptions:
            document.add_paragraph(a, style="Bullet")

    if missing:
        warnings.append(f"{len(missing)} chart image{'s' if len(missing) != 1 else ''} could not be placed in the DOCX.")
    out = Path(out_path)
    document.save(str(out))
    return out


__all__ = ["render_docx"]
