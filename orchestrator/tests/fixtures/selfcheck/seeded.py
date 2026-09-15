"""Sixty seeded file defects, each with a correct twin.

A case = (id, base, item, twin, defect): `base` names a rendered sample
(doc: docx+pdf, wb: xlsx+csv, deck: pptx+pdf, svg: a written SVG), `item` is
the checklist item under test, and `twin`/`defect` edit a COPY of the files
in place. The twin must evaluate to pass and the defect to fail. Written
against the file formats, not the inspector's code.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, NamedTuple

from app.artifacts.requirements import ChecklistItem

from . import files as F

Files = Dict[str, Path]


class Case(NamedTuple):
    id: str
    base: str
    item: ChecklistItem
    twin: Callable[[Files], None]
    defect: Callable[[Files], None]


def _item(category, target, prop, expected, **locator) -> ChecklistItem:
    return ChecklistItem("x", category, target, prop, expected, must=True, locator=locator)


def _one(files: Files, ext: str) -> Path:
    return next(p for n, p in files.items() if n.endswith("." + ext) and n != "preview.pdf")


def _noop(files: Files) -> None:
    return None


BODY_STYLES = ("Normal", "Body", "Bullet")


def _docx(fn):
    return lambda files: fn(_one(files, "docx"))


def _xlsx(fn):
    return lambda files: F.xlsx_edit(_one(files, "xlsx"), fn)


def _pptx(fn):
    return lambda files: F.pptx_edit(_one(files, "pptx"), fn)


def _pdf(fn):
    return lambda files: fn(_one(files, "pdf"))


def _styles(path, names, **kw):
    for n in names:
        F.docx_style(path, n, **kw)


def _no_cf(ws):
    from openpyxl.formatting.formatting import ConditionalFormattingList

    ws.conditional_formatting = ConditionalFormattingList()


def _fill(hex_):
    from openpyxl.styles import PatternFill

    return PatternFill("solid", fgColor="FF" + hex_.lstrip("#"))


def _header(ws):
    return [c for c in ws[1] if c.value not in (None, "")][:4]


def _font(cell, **kw):
    from copy import copy

    f = copy(cell.font)
    for k, v in kw.items():
        setattr(f, k, v)
    cell.font = f


def _set_color(cell, hex_):
    from copy import copy

    from openpyxl.styles import Font

    f = copy(cell.font)
    cell.font = Font(name=f.name, sz=f.sz, b=f.b, i=f.i, u=f.u, color="FF" + hex_.lstrip("#"))


def _col(ws, name):
    for c in ws[1]:
        if str(c.value or "").lower() == name.lower():
            return c.column
    raise KeyError(name)


def _data_cells(ws, name):
    col = _col(ws, name)
    return [ws.cell(row=r, column=col) for r in range(2, 14)]


def _chart(wb):
    return wb["Tickets"]._charts[0]


def _series_fill(hex_):
    def fn(wb):
        from openpyxl.chart.shapes import GraphicalProperties

        ser = _chart(wb).series[0]
        ser.graphicalProperties = GraphicalProperties(solidFill=hex_.lstrip("#"))
    return fn


def _legend(pos):
    def fn(wb):
        from openpyxl.chart.legend import Legend

        _chart(wb).legend = Legend(legendPos=pos)
    return fn


def _labels(on):
    def fn(wb):
        from openpyxl.chart.label import DataLabelList

        _chart(wb).dataLabels = DataLabelList(showVal=True) if on else None
    return fn


def _to_line(wb):
    from openpyxl.chart import LineChart

    ws = wb["Tickets"]
    old = ws._charts[0]
    new = LineChart()
    for s in old.series:
        new.series.append(s)
    new.title = old.title
    ws._charts[0] = new


def _chart_ref_cell_change(wb):
    ws = wb["Tickets"]
    ser = ws._charts[0].series[0]
    ref = ser.val.numRef.f
    target = ref.split("!")[1].split(":")[0].replace("$", "")
    ws[target].value = float(ws[target].value or 0) + 7


def _slide_title_runs(prs, fn):
    titles = {"Revenue", "Detail", "Q3 Results"}
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for p in shape.text_frame.paragraphs:
                    text = "".join(r.text for r in p.runs).strip()
                    sizes = [r.font.size.pt for r in p.runs if r.font.size is not None]
                    if text in titles and (not sizes or max(sizes) >= 16):
                        for r in p.runs:
                            fn(r)


def _body_runs(prs, fn):
    titles = {"Revenue", "Detail", "Q3 Results"}
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for p in shape.text_frame.paragraphs:
                    text = "".join(r.text for r in p.runs).strip()
                    sizes = [r.font.size.pt for r in p.runs if r.font.size is not None]
                    is_title = text in titles and (not sizes or max(sizes) >= 16)
                    if text and not is_title:
                        for r in p.runs:
                            fn(r)


def _rgb(hex_):
    from pptx.dml.color import RGBColor

    return RGBColor.from_string(hex_.lstrip("#"))


def _pptx_header_fill(hex_):
    def fn(prs):
        for slide in prs.slides:
            for shape in slide.shapes:
                if getattr(shape, "has_table", False) and shape.has_table:
                    for cell in shape.table.rows[0].cells:
                        cell.fill.solid()
                        cell.fill.fore_color.rgb = _rgb(hex_)
    return fn


def _pptx_chart(fn):
    def outer(prs):
        for slide in prs.slides:
            for shape in slide.shapes:
                if getattr(shape, "has_chart", False) and shape.has_chart:
                    fn(shape.chart)
    return outer


def _pptx_values(chart):
    from pptx.chart.data import CategoryChartData

    data = CategoryChartData()
    data.categories = ["Jul", "Aug", "Sep"]
    data.add_series("Revenue", (3, 6, 4))
    chart.replace_data(data)


def _pdf_remove_text(path, match):
    import ctypes
    import io

    import pypdfium2 as pdfium
    import pypdfium2.raw as R

    pdf = pdfium.PdfDocument(str(path))
    for i in range(len(pdf)):
        page = pdf[i]
        tp = page.get_textpage()
        victims = []
        for j in range(R.FPDFPage_CountObjects(page.raw)):
            obj = R.FPDFPage_GetObject(page.raw, j)
            if R.FPDFPageObj_GetType(obj) != R.FPDF_PAGEOBJ_TEXT:
                continue
            ln = R.FPDFTextObj_GetText(obj, tp.raw, None, 0)
            buf = ctypes.create_string_buffer(ln * 2)
            R.FPDFTextObj_GetText(obj, tp.raw, ctypes.cast(buf, ctypes.POINTER(ctypes.c_ushort)), ln)
            if match(buf.raw.decode("utf-16-le", "ignore").rstrip("\x00")):
                victims.append(obj)
        for obj in victims:
            R.FPDFPage_RemoveObject(page.raw, obj)
            R.FPDFPageObj_Destroy(obj)
        R.FPDFPage_GenerateContent(page.raw)
    out = io.BytesIO()
    pdf.save(out)
    pdf.close()
    Path(path).write_bytes(out.getvalue())


SVG_CLEAN = b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><rect width="40" height="20" fill="#2F6FB2"/></svg>'
SVG_BAD = b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><script>alert(1)</script><rect width="40" height="20" fill="#2F6FB2"/></svg>'


def _svg(data):
    return lambda files: _one(files, "svg").write_bytes(data)


def cases() -> List[Case]:
    c: List[Case] = []
    add = lambda *a: c.append(Case(*a))  # noqa: E731
    H = ("Heading 1", "Heading 2", "Heading 3")
    # ---------------------------------------------------------------- DOCX
    add("d01", "doc", _item("style", "heading", "color", "#1F3864"), _docx(lambda p: _styles(p, H, color="1F3864")), _docx(lambda p: (_styles(p, H, color="1F3864"), F.docx_style(p, "Heading 1", color="8B0000"))))
    add("d02", "doc", _item("style", "heading1", "color", "#2E5597"), _docx(lambda p: F.docx_style(p, "Heading 1", color="2E5597")), _docx(lambda p: F.docx_style(p, "Heading 1", color="000000")))
    add("d03", "doc", _item("style", "heading2", "color", "#6B7280"), _docx(lambda p: F.docx_style(p, "Heading 2", color="6B7280")), _docx(lambda p: F.docx_style(p, "Heading 2", color="1F3864")))
    add("d04", "doc", _item("style", "title", "color", "#1F3864"), _docx(lambda p: F.docx_style(p, "Title", color="1F3864")), _docx(lambda p: F.docx_style(p, "Title", color="C00000")))
    add("d05", "doc", _item("style", "title", "size_pt", 28.0), _docx(lambda p: F.docx_style(p, "Title", size_pt=28)), _docx(lambda p: F.docx_style(p, "Title", size_pt=20)))
    add("d06", "doc", _item("style", "title", "bold", True), _docx(lambda p: F.docx_style(p, "Title", bold=True)), _docx(lambda p: F.docx_style(p, "Title", bold=False)))
    add("d07", "doc", _item("style", "subtitle", "italic", True), _docx(lambda p: F.docx_style(p, "Subtitle", italic=True)), _docx(lambda p: F.docx_style(p, "Subtitle", italic=False)))
    add("d08", "doc", _item("style", "heading", "underline", True), _docx(lambda p: _styles(p, H, underline=True)), _docx(lambda p: _styles(p, H[1:], underline=True)))
    add("d09", "doc", _item("style", "paragraph", "font_family", "Georgia"), _docx(lambda p: _styles(p, BODY_STYLES, font="Georgia")), _docx(lambda p: _styles(p, BODY_STYLES, font="Arial")))
    add("d10", "doc", _item("style", "paragraph", "size_pt", 12.0), _docx(lambda p: _styles(p, BODY_STYLES, size_pt=12)), _docx(lambda p: _styles(p, BODY_STYLES, size_pt=9)))
    add("d11", "doc", _item("style", "paragraph", "color", "#1F2937"), _docx(lambda p: _styles(p, BODY_STYLES, color="1F2937")), _docx(lambda p: _styles(p, BODY_STYLES, color="FF0000")))
    add("d12", "doc", _item("style", "table_header", "background", "#1F3864"), _docx(lambda p: F.docx_table_header(p, fill="1F3864")), _docx(lambda p: F.docx_table_header(p, fill="8B0000")))
    add("d13", "doc", _item("style", "table_header", "color", "#FFFFFF"), _docx(lambda p: F.docx_table_header(p, color="FFFFFF")), _docx(lambda p: F.docx_table_header(p, color="000000")))
    add("d14", "doc", _item("layout", "page", "orientation", "landscape"), _docx(lambda p: F.docx_section(p, landscape=True)), _docx(lambda p: F.docx_section(p, landscape=False)))
    add("d15", "doc", _item("layout", "page", "page_size", "Letter"), _docx(lambda p: F.docx_section(p, size=(12240, 15840))), _docx(lambda p: F.docx_section(p, size=(11906, 16838))))
    add("d16", "doc", _item("layout", "page", "margins", "narrow"), _docx(lambda p: F.docx_section(p, margins_twips=720)), _docx(lambda p: F.docx_section(p, margins_twips=1440)))
    add("d17", "doc", _item("layout", "page", "margins", "wide"), _docx(lambda p: F.docx_section(p, margins_twips=1440)), _docx(lambda p: F.docx_section(p, margins_twips=720)))
    add("d18", "doc", _item("layout", "page", "page_numbers", True), _noop, _docx(F.docx_strip_page_fields))
    add("d19", "doc", _item("security", "file", "no_unsafe_links", True), _docx(lambda p: F.docx_external_link(p, "https://example.com/policy")), _docx(lambda p: F.docx_external_link(p, "javascript:alert(1)")))
    add("d20", "doc", _item("faithfulness", "document", "headings_covered", True), _noop, _docx(lambda p: F.docx_delete_paragraph_text(p, "Recommendations")))
    add("d21", "doc", _item("faithfulness", "document", "table_cells_covered", True), _noop, _docx(lambda p: F.docx_replace_text(p, "Tokens", "")))
    add("d22", "doc", _item("content", "section:findings", "present", True), _noop, _docx(lambda p: F.docx_replace_text(p, "Findings", "Results")))
    add("d23", "doc", _item("house_style", "title", "no_letter_spacing", True), _docx(lambda p: F.docx_style(p, "Title", spacing=0)), _docx(lambda p: F.docx_style(p, "Title", spacing=40)))
    add("d24", "doc", _item("house_style", "table_header", "contrast", True), _docx(lambda p: F.docx_table_header(p, fill="1F3864", color="FFFFFF")), _docx(lambda p: F.docx_table_header(p, fill="FFFFFF", color="FFFFFF")))
    # ---------------------------------------------------------------- XLSX
    add("x01", "wb", _item("style", "table_header", "background", "#1F3864"), _xlsx(lambda wb: [setattr(x, "fill", _fill("1F3864")) for x in _header(wb["Tickets"])]), _xlsx(lambda wb: [setattr(x, "fill", _fill("FFFF00")) for x in _header(wb["Tickets"])]))
    add("x02", "wb", _item("style", "table_header", "bold", True), _xlsx(lambda wb: [_font(x, b=True) for x in _header(wb["Tickets"])]), _xlsx(lambda wb: [_font(x, b=False) for x in _header(wb["Tickets"])]))
    add("x03", "wb", _item("style", "table_header", "color", "#FFFFFF"), _xlsx(lambda wb: [_set_color(x, "FFFFFF") for x in _header(wb["Tickets"])]), _xlsx(lambda wb: [_set_color(x, "000000") for x in _header(wb["Tickets"])]))
    add("x04", "wb", _item("style", "row:5", "background", "#FFD54F"), _xlsx(lambda wb: [setattr(wb["Tickets"].cell(row=5, column=i), "fill", _fill("FFD54F")) for i in range(1, 5)]), _xlsx(lambda wb: [setattr(wb["Tickets"].cell(row=5, column=i), "fill", _fill("FFD54F")) for i in range(1, 3)]))
    # AS3 integration: the styled workbook bands its rows with conditional
    # formats, which Excel draws OVER a static fill; the seeded static fills
    # are only visible with those formats taken off.
    add("x05", "wb", _item("style", "cell_range:B2:C3", "background", "#FFF1C7"), _xlsx(lambda wb: [_no_cf(wb["Tickets"])] + [setattr(wb["Tickets"][a], "fill", _fill("FFF1C7")) for a in ("B2", "B3", "C2", "C3")]), _xlsx(lambda wb: [_no_cf(wb["Tickets"])] + [setattr(wb["Tickets"][a], "fill", _fill("FFF1C7")) for a in ("B2", "B3", "C2")]))
    add("x06", "wb", _item("style", "column:owner", "bold", True), _xlsx(lambda wb: [_font(x, b=True) for x in _data_cells(wb["Tickets"], "Owner")]), _xlsx(lambda wb: [_font(x, b=False) for x in _data_cells(wb["Tickets"], "Owner")]))
    add("x07", "wb", _item("style", "column:status", "color", "#C62828"), _xlsx(lambda wb: [_set_color(x, "C62828") for x in _data_cells(wb["Tickets"], "Status")]), _xlsx(lambda wb: [_set_color(x, "000000") for x in _data_cells(wb["Tickets"], "Status")]))
    add("x08", "wb", _item("data", "sheet", "row_count", 12), _noop, _xlsx(lambda wb: wb["Tickets"].delete_rows(5)))
    add("x09", "wb", _item("data", "column:owner", "present", True), _noop, _xlsx(lambda wb: setattr(wb["Tickets"].cell(row=1, column=_col(wb["Tickets"], "Owner")), "value", "Assignee")))
    add("x10", "wb", _item("security", "file", "formula_text_neutralised", True), _xlsx(lambda wb: (setattr(wb["Tickets"]["C4"], "value", "+cmd"), setattr(wb["Tickets"]["C4"], "quotePrefix", True))), _xlsx(lambda wb: (setattr(wb["Tickets"]["C4"], "value", "+cmd"), setattr(wb["Tickets"]["C4"], "quotePrefix", False))))
    add("x11", "wb", _item("security", "file", "formula_text_neutralised", True), _xlsx(lambda wb: setattr(wb["Tickets"]["C5"], "value", "=SUM(D2:D3)")), _xlsx(lambda wb: setattr(wb["Tickets"]["C5"], "value", '=HYPERLINK("http://example.invalid","x")')))
    add("x12", "wb", _item("chart", "chart", "type", "bar"), _noop, _xlsx(_to_line))
    add("x13", "wb", _item("chart", "chart", "series_color", "#2F6FB2"), _xlsx(_series_fill("2F6FB2")), _xlsx(_series_fill("E07B00")))
    add("x14", "wb", _item("chart", "chart", "legend_position", "bottom"), _xlsx(_legend("b")), _xlsx(_legend("r")))
    add("x15", "wb", _item("chart", "chart", "data_labels", True), _xlsx(_labels(True)), _xlsx(_labels(False)))
    add("x16", "wb", _item("chart", "chart", "values_match", True), _noop, _xlsx(_chart_ref_cell_change))
    add("x17", "wb", _item("style", "table_total", "bold", True), _noop, _xlsx(lambda wb: [_font(wb["Tickets"].cell(row=wb["Tickets"].max_row, column=i), b=False) for i in range(1, 5)]))
    add("x18", "wb", _item("style", "column:amount", "italic", True), _xlsx(lambda wb: [_font(x, i=True) for x in _data_cells(wb["Tickets"], "Amount")]), _xlsx(lambda wb: [_font(x, i=True) for x in _data_cells(wb["Tickets"], "Amount")[:6]]))
    add("x19", "wb", _item("style", "cell_range:B2:C3", "background", "#FFF1C7", color_name="light yellow", shade="light"), _xlsx(lambda wb: [_no_cf(wb["Tickets"])] + [setattr(wb["Tickets"][a], "fill", _fill("FFF1C7")) for a in ("B2", "B3", "C2", "C3")]), _xlsx(lambda wb: [_no_cf(wb["Tickets"])] + [setattr(wb["Tickets"][a], "fill", _fill("8B0000")) for a in ("B2", "B3", "C2", "C3")]))
    add("x20", "wb", _item("data", "sheet", "row_count", 12), _noop, lambda files: _one(files, "csv").write_text("\n".join(_one(files, "csv").read_text(encoding="utf-8-sig").splitlines()[:-1]) + "\n", encoding="utf-8"))
    # ---------------------------------------------------------------- PPTX
    add("p01", "deck", _item("style", "slide_title", "color", "#7B1E1E"), _pptx(lambda prs: _slide_title_runs(prs, lambda r: setattr(r.font.color, "rgb", _rgb("7B1E1E")))), _pptx(lambda prs: _slide_title_runs(prs, lambda r: setattr(r.font.color, "rgb", _rgb("000000")))))
    add("p02", "deck", _item("style", "slide_title", "size_pt", 28.0), _pptx(lambda prs: _slide_title_runs(prs, lambda r: setattr(r.font, "size", __import__("pptx.util", fromlist=["Pt"]).Pt(28)))), _pptx(lambda prs: _slide_title_runs(prs, lambda r: setattr(r.font, "size", __import__("pptx.util", fromlist=["Pt"]).Pt(18)))))
    add("p03", "deck", _item("style", "table_header", "background", "#1F3864"), _pptx(_pptx_header_fill("1F3864")), _pptx(_pptx_header_fill("FFFFFF")))
    add("p04", "deck", _item("chart", "chart", "data_labels", True), _pptx(_pptx_chart(lambda ch: setattr(ch.plots[0], "has_data_labels", True))), _pptx(_pptx_chart(lambda ch: setattr(ch.plots[0], "has_data_labels", False))))
    add("p05", "deck", _item("chart", "chart", "values_match", True), _noop, _pptx(_pptx_chart(_pptx_values)))
    add("p06", "deck", _item("style", "paragraph", "font_family", "Georgia"), _pptx(lambda prs: _body_runs(prs, lambda r: setattr(r.font, "name", "Georgia"))), _pptx(lambda prs: _body_runs(prs, lambda r: setattr(r.font, "name", "Arial"))))
    add("p07", "deck", _item("style", "slide_title", "bold", True), _pptx(lambda prs: _slide_title_runs(prs, lambda r: setattr(r.font, "bold", True))), _pptx(lambda prs: _slide_title_runs(prs, lambda r: setattr(r.font, "bold", False))))
    add("p08", "deck", _item("content", "section:detail", "present", True), _noop, _pptx(lambda prs: _slide_title_runs(prs, lambda r: setattr(r, "text", "Other") if r.text.strip() == "Detail" else None)))
    # ----------------------------------------------------------------- PDF
    add("f01", "doc", _item("style", "title", "color", "#1F3864"), _pdf(lambda p: F.pdf_edit_text(p, lambda t: t.strip() == "Vendor Access Review", rgb=(31, 56, 100))), _pdf(lambda p: F.pdf_edit_text(p, lambda t: t.strip() == "Vendor Access Review", rgb=(200, 0, 0))))
    # AS3 integration: #1F3864 is now the house heading colour, so the seeded
    # colour is one the house style never uses.
    add("f02", "doc", _item("style", "heading1", "color", "#7B1E1E"), _pdf(lambda p: F.pdf_edit_text(p, lambda t: t.strip() in ("Scope", "Findings", "Recommendations"), rgb=(123, 30, 30))), _pdf(lambda p: F.pdf_edit_text(p, lambda t: t.strip() in ("Scope", "Findings"), rgb=(123, 30, 30))))
    add("f03", "doc", _item("style", "table_header", "color", "#FFFFFF"), _noop, _pdf(lambda p: F.pdf_edit_text(p, lambda t: t.strip() in ("Area", "Status", "Count"), rgb=(0, 0, 0))))
    add("f04", "doc", _item("layout", "page", "orientation", "landscape"), _pdf(F.pdf_swap_mediabox), _noop)
    add("f05", "doc", _item("layout", "page", "page_numbers", True), _noop, _pdf(lambda p: _pdf_remove_text(p, lambda t: "Page" in t and "of" in t)))
    add("f06", "doc", _item("faithfulness", "document", "table_cells_covered", True), _noop, _pdf(lambda p: _pdf_remove_text(p, lambda t: t.strip() == "Tokens")))
    add("f07", "doc", _item("style", "paragraph", "color", "#1F2937"), _noop, _pdf(lambda p: F.pdf_edit_text(p, lambda t: len(t.strip()) > 1 and t.strip() not in ("Vendor Access Review", "Quarterly control check", "Scope", "Findings", "Detail", "Recommendations", "Area", "Status", "Count") and "Page" not in t, rgb=(200, 0, 0))))
    add("f08", "doc", _item("style", "heading2", "color", "#2E5597"), _noop, _pdf(lambda p: F.pdf_edit_text(p, lambda t: t.strip() == "Detail", rgb=(200, 0, 0))))
    # ----------------------------------------------------------------- SVG
    add("s01", "svg", _item("security", "file", "no_unsafe_links", True), _svg(SVG_CLEAN), _svg(SVG_BAD))
    return c


FORMAT_OF_BASE = {"doc": ["docx", "pdf"], "wb": ["xlsx", "csv"], "deck": ["pptx", "pdf"]}
