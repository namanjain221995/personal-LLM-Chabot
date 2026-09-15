"""Read what a PRODUCED file shows for one piece of text — independently of
the renderers (no ResolvedStyle, no spec): the Office XML with python-docx /
python-pptx / openpyxl, and the PDF content stream through PDFium's page
objects. Used by the support-matrix and parity tests.

Each reader returns {font, size, bold, italic, underline, color, background,
align} for the element whose text contains a marker, with None for what the
format does not show.
"""
from __future__ import annotations

import ctypes
import math
from typing import Any, Dict, List, Optional, Tuple

Observed = Dict[str, Any]


# ------------------------------------------------------------------- DOCX --


def _w(tag: str) -> str:
    from docx.oxml.ns import qn

    return qn(tag)


def _style_chain(style) -> List[Any]:
    chain = []
    while style is not None:
        chain.append(style)
        style = style.base_style
    return chain


def _rpr_value(rpr, tag: str) -> Optional[Any]:
    if rpr is None:
        return None
    el = rpr.find(_w(tag))
    if el is None:
        return None
    val = el.get(_w("w:val"))
    if tag in ("w:b", "w:i"):
        return val not in ("0", "false", "off")
    if tag == "w:u":
        return val not in (None, "none")
    if tag == "w:sz":
        return int(val) / 2
    if tag == "w:color":
        return ("#" + val.upper()) if val and val != "auto" else None
    if tag == "w:rFonts":
        return el.get(_w("w:ascii"))
    return val


def _ppr_value(ppr, tag: str) -> Optional[Any]:
    if ppr is None:
        return None
    el = ppr.find(_w(tag))
    if el is None:
        return None
    if tag == "w:shd":
        fill = el.get(_w("w:fill"))
        return ("#" + fill.upper()) if fill and fill != "auto" else None
    return el.get(_w("w:val"))


def docx_observe(document, marker: str) -> Optional[Observed]:
    """The first paragraph (body, table cell, header or footer) with a run
    containing `marker`."""
    candidates: List[Tuple[Any, Any]] = []
    for p in document.paragraphs:
        candidates.append((p, None))
    for t in document.tables:
        for row in t.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    candidates.append((p, cell))
    for section in document.sections:
        for part in (section.header, section.footer):
            for p in part.paragraphs:
                candidates.append((p, None))
    for p, cell in candidates:
        run = next((r for r in p.runs if marker in r.text), None)
        if run is None:
            continue
        rpr = run._r.rPr
        chain = _style_chain(p.style)
        doc_defaults = document.styles.element.find(_w("w:docDefaults"))

        def rprop(tag):
            v = _rpr_value(rpr, tag)
            if v is not None:
                return v
            for st in chain:
                v = _rpr_value(st.element.rPr, tag)
                if v is not None:
                    return v
            if doc_defaults is not None:
                d = doc_defaults.find(_w("w:rPrDefault"))
                if d is not None:
                    return _rpr_value(d.find(_w("w:rPr")), tag)
            return None

        def pprop(tag):
            v = _ppr_value(p._p.pPr, tag)
            if v is not None:
                return v
            for st in chain:
                v = _ppr_value(st.element.pPr, tag)
                if v is not None:
                    return v
            return None

        background = pprop("w:shd")
        if background is None and cell is not None:
            shd = cell._tc.tcPr.find(_w("w:shd")) if cell._tc.tcPr is not None else None
            if shd is not None and shd.get(_w("w:fill")) not in (None, "auto"):
                background = "#" + shd.get(_w("w:fill")).upper()
        jc = pprop("w:jc")
        return {
            "font": rprop("w:rFonts"), "size": rprop("w:sz"), "bold": bool(rprop("w:b")), "italic": bool(rprop("w:i")),
            "underline": bool(rprop("w:u")), "color": rprop("w:color"), "background": background,
            "align": {"both": "justify", "start": "left", "end": "right"}.get(jc, jc) if jc else "left",
        }
    return None


# ------------------------------------------------------------------- PPTX --


def pptx_observe(prs, marker: str) -> Optional[Observed]:
    from pptx.enum.text import PP_ALIGN

    align_names = {PP_ALIGN.LEFT: "left", PP_ALIGN.CENTER: "center", PP_ALIGN.RIGHT: "right", PP_ALIGN.JUSTIFY: "justify"}

    def run_obs(p, run, background):
        f = run.font
        color = None
        try:
            color = "#" + str(f.color.rgb) if f.color and f.color.type is not None else None
        except AttributeError:
            color = None
        return {"font": f.name, "size": f.size.pt if f.size else None, "bold": bool(f.bold), "italic": bool(f.italic),
                "underline": bool(f.underline), "color": color, "background": background,
                "align": align_names.get(p.alignment, "left")}

    def shape_fill(fill):
        try:
            return "#" + str(fill.fore_color.rgb) if fill.type == 1 else None  # MSO_FILL.SOLID
        except (AttributeError, TypeError):
            return None

    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for p in shape.text_frame.paragraphs:
                    for run in p.runs:
                        if marker in run.text:
                            return run_obs(p, run, shape_fill(shape.fill))
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        for p in cell.text_frame.paragraphs:
                            for run in p.runs:
                                if marker in run.text:
                                    return run_obs(p, run, shape_fill(cell.fill))
    return None


def pptx_chart_fonts(prs) -> Dict[str, Observed]:
    """title / axis / legend / labels font facts of the first native chart."""
    for slide in prs.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_chart", False) and shape.has_chart:
                c = shape.chart

                def fobs(f):
                    color = None
                    try:
                        color = "#" + str(f.color.rgb) if f.color and f.color.type is not None else None
                    except AttributeError:
                        pass
                    return {"font": f.name, "size": f.size.pt if f.size else None, "bold": bool(f.bold), "italic": bool(f.italic),
                            "underline": bool(f.underline), "color": color}

                out = {"chart_axis": fobs(c.font)}
                if c.has_title:
                    out["chart_title"] = fobs(c.chart_title.text_frame.paragraphs[0].runs[0].font)
                if c.has_legend:
                    out["chart_legend"] = fobs(c.legend.font)
                plot = c.plots[0]
                if plot.has_data_labels:
                    out["chart_labels"] = fobs(plot.data_labels.font)
                return out
    return {}


# ------------------------------------------------------------------- XLSX --


def xlsx_observe(path: str, marker: str) -> Optional[Observed]:
    from openpyxl import load_workbook

    from app.artifacts.render.preview import styled_window

    wb = load_workbook(path)
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and marker in cell.value:
                        window = styled_window(ws, max(2, cell.row), max(2, cell.row), ws.max_column)
                        if cell.row == 1:
                            style = window["header_styles"][cell.column - 1]
                        else:
                            style = window["cell_styles"].get(f"0:{cell.column - 1}", {})
                        return {"font": cell.font.name, "size": cell.font.sz, "bold": bool(style.get("bold") or cell.font.b),
                                "italic": bool(style.get("italic") or cell.font.i), "underline": bool(style.get("underline") or cell.font.u),
                                "color": style.get("color"), "background": style.get("fill"),
                                "align": cell.alignment.horizontal or "left"}
    finally:
        wb.close()
    return None


# -------------------------------------------------------------------- PDF --


def _text_of(raw, obj, textpage) -> str:
    n = raw.FPDFTextObj_GetText(obj, textpage, None, 0)
    if n <= 0:
        return ""
    buf = (ctypes.c_ushort * n)()
    raw.FPDFTextObj_GetText(obj, textpage, buf, n)
    return bytes(buf).decode("utf-16-le", errors="ignore").rstrip("\x00")


def _font_name(raw, obj) -> str:
    font = raw.FPDFTextObj_GetFont(obj)
    if not font:
        return ""
    n = raw.FPDFFont_GetBaseFontName(font, None, 0)
    buf = ctypes.create_string_buffer(max(n, 1))
    raw.FPDFFont_GetBaseFontName(font, buf, n)
    return buf.value.decode("utf-8", errors="ignore")


def _font_weight_italic(raw, obj) -> Tuple[int, int]:
    font = raw.FPDFTextObj_GetFont(obj)
    if not font:
        return 400, 0
    weight = raw.FPDFFont_GetWeight(font)
    angle = ctypes.c_int(0)
    raw.FPDFFont_GetItalicAngle(font, ctypes.byref(angle))
    return int(weight), int(angle.value)


def _colour(raw, obj, getter) -> Optional[str]:
    r, g, b, a = (ctypes.c_uint(0) for _ in range(4))
    if not getter(obj, ctypes.byref(r), ctypes.byref(g), ctypes.byref(b), ctypes.byref(a)):
        return None
    if a.value == 0:
        return None
    return "#%02X%02X%02X" % (r.value, g.value, b.value)


def _bounds(raw, obj) -> Tuple[float, float, float, float]:
    left, bottom, right, top = (ctypes.c_float(0) for _ in range(4))
    raw.FPDFPageObj_GetBounds(obj, ctypes.byref(left), ctypes.byref(bottom), ctypes.byref(right), ctypes.byref(top))
    return left.value, bottom.value, right.value, top.value


Matrix = Tuple[float, float, float, float, float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _compose(inner: Matrix, outer: Matrix) -> Matrix:
    """The matrix that applies `inner`, then `outer` (PDF [a b c d e f])."""
    a, b, c, d, e, f = inner
    A, B, C, D, E, F = outer
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)


def _apply(m: Matrix, x: float, y: float) -> Tuple[float, float]:
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def pdf_objects(path: str) -> List[Dict[str, Any]]:
    """Every text and path object: {page, kind, text, font, weight, italic,
    size, fill, stroke, bounds, filled, segments, page_width, page_height}.
    Objects inside form XObjects (WeasyPrint draws opacity groups as forms)
    are included with the form matrices applied, so bounds and sizes are in
    page space for every object."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    out: List[Dict[str, Any]] = []
    pdf = pdfium.PdfDocument(path)

    def matrix_of(handle) -> Matrix:
        m = raw.FS_MATRIX()
        if not raw.FPDFPageObj_GetMatrix(handle, ctypes.byref(m)):
            return _IDENTITY
        return (m.a, m.b, m.c, m.d, m.e, m.f)

    try:
        for index in range(len(pdf)):
            page = pdf[index]
            width, height = page.get_size()
            textpage = page.get_textpage()

            def walk(handle, parent: Matrix, depth: int) -> None:
                kind = raw.FPDFPageObj_GetType(handle)
                if kind == raw.FPDF_PAGEOBJ_FORM:
                    if depth > 8:
                        return
                    total = _compose(matrix_of(handle), parent)
                    for i in range(raw.FPDFFormObj_CountObjects(handle)):
                        walk(raw.FPDFFormObj_GetObject(handle, i), total, depth + 1)
                    return
                if kind not in (raw.FPDF_PAGEOBJ_TEXT, raw.FPDF_PAGEOBJ_PATH):
                    return
                x0, y0, x1, y1 = _bounds(raw, handle)
                corners = [_apply(parent, x, y) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
                bounds = (min(x for x, _ in corners), min(y for _, y in corners), max(x for x, _ in corners), max(y for _, y in corners))
                item = {"page": index, "bounds": bounds, "fill": _colour(raw, handle, raw.FPDFPageObj_GetFillColor),
                        "stroke": _colour(raw, handle, raw.FPDFPageObj_GetStrokeColor), "page_width": width, "page_height": height}
                if kind == raw.FPDF_PAGEOBJ_TEXT:
                    size = ctypes.c_float(0)
                    raw.FPDFTextObj_GetFontSize(handle, ctypes.byref(size))
                    own = matrix_of(handle)
                    scale = (math.hypot(own[0], own[1]) or 1.0) * (math.hypot(parent[0], parent[1]) or 1.0)
                    weight, angle = _font_weight_italic(raw, handle)
                    item.update(kind="text", text=_text_of(raw, handle, textpage.raw), font=_font_name(raw, handle),
                                size=round(size.value * scale, 2), weight=weight, italic_angle=angle)
                else:
                    fillmode, stroke = ctypes.c_int(0), ctypes.c_int(0)
                    raw.FPDFPath_GetDrawMode(handle, ctypes.byref(fillmode), ctypes.byref(stroke))
                    item.update(kind="path", filled=fillmode.value != 0, stroked=bool(stroke.value), segments=raw.FPDFPath_CountSegments(handle))
                out.append(item)

            for i in range(raw.FPDFPage_CountObjects(page.raw)):
                walk(raw.FPDFPage_GetObject(page.raw, i), _IDENTITY, 0)
            textpage.close()
            page.close()
    finally:
        pdf.close()
    return out


def pdf_observe(path: str, marker: str, *, container: Optional[Tuple[float, float]] = None, objects=None) -> Optional[Observed]:
    """The text object holding `marker` and what surrounds it: a filled
    rectangle under its centre (the background), a thin filled bar or line
    in the text colour just below it (the underline), and its position in
    `container` (x0, x1 in points) or in that background rectangle."""
    objects = objects if objects is not None else pdf_objects(path)
    text = next((o for o in objects if o["kind"] == "text" and marker in o["text"]), None)
    if text is None:
        return None
    left, b, r, t = text["bounds"]
    cx, cy = (left + r) / 2, (b + t) / 2
    same_page = [o for o in objects if o["page"] == text["page"] and o["kind"] == "path"]
    # A background is a plain rectangle (<= 5 segments); a border is drawn
    # as a frame (outer and inner rectangle, 10 segments) and is not one.
    backgrounds = [o for o in same_page if o.get("filled") and o["fill"] and o.get("segments", 0) <= 5 and o["bounds"][0] <= cx <= o["bounds"][2] and o["bounds"][1] <= cy <= o["bounds"][3]
                   and (o["bounds"][2] - o["bounds"][0]) >= (r - left) * 0.9 and (o["bounds"][3] - o["bounds"][1]) >= (t - b) * 0.8]
    backgrounds.sort(key=lambda o: (o["bounds"][2] - o["bounds"][0]) * (o["bounds"][3] - o["bounds"][1]))
    background = backgrounds[0]["fill"] if backgrounds else None
    underline = any(
        o for o in same_page
        if (o.get("filled") and o["fill"] == text["fill"] or o.get("stroked") and o["stroke"] == text["fill"])
        and (o["bounds"][3] - o["bounds"][1]) <= 2.5 and o["bounds"][0] <= cx <= o["bounds"][2] and b - 5 <= o["bounds"][3] <= b + 4
    )
    if container is None and backgrounds:
        container = (backgrounds[0]["bounds"][0], backgrounds[0]["bounds"][2])
    align = None
    if container is not None:
        gap_l, gap_r = left - container[0], container[1] - r
        if abs(gap_l - gap_r) <= 3:
            align = "center"
        elif gap_l < gap_r:
            align = "left"
        else:
            align = "right"
    name = text["font"]
    return {"font": name, "size": text["size"], "bold": text["weight"] >= 600 or "Bold" in name,
            "italic": text["italic_angle"] != 0 or "Italic" in name or "Oblique" in name, "underline": underline,
            "color": text["fill"], "background": background, "align": align, "bounds": text["bounds"], "page": text["page"],
            "page_width": text["page_width"], "page_height": text["page_height"]}
