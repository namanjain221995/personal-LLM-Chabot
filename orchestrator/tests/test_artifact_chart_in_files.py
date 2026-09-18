"""The colour plan has to survive the trip into the FILE the owner opens.

`chart_colours` gives two charts of different subjects two different
colours, and every test of that module reads the scheme (or a hand-built
`render.charts._Ctx`) directly. That is how this shipped broken: the chart
step of `render_version` called `render_chart_png(chart, path)` with no
resolved style, so `ResolvedStyle.chart_plan` never reached the PNG that the
DOCX and the PDF embed, while the NATIVE PPTX and XLSX charts — which do get
the resolved style — used the plan. Measured on the integrated tree before
the fix, for one two-chart document/deck/workbook of the same two subjects:

    word/media/image1.png, image2.png : #2F6FB2, #2F6FB2
    rendered PDF (document)           : #2F6FB2 only
    rendered PDF (deck)               : #2F6FB2 only
    ppt/charts/chart1..2.xml          : #2F6FB2, #E07B00
    xl/charts/chart1..2.xml           : #2F6FB2, #E07B00

so the same chart was one colour in the document and another in the deck.
These tests go through the REAL render loop and read the bytes of the
produced files, because no assertion on a scheme object can see that gap.
"""
from __future__ import annotations

import collections
import io
import re
import zipfile
from pathlib import Path
from typing import Dict, List

import pytest

from app.artifacts import chart_colours as CC
from app.artifacts import spec as S
from app.artifacts.render import capabilities, render_version

pytestmark = pytest.mark.skipif(not capabilities().get("pdf"), reason="WeasyPrint is not installed (excluded from requirements-dev)")

#: rgb triple -> palette hex, for reading a colour back out of pixels.
_BY_RGB: Dict[tuple, str] = {tuple(int(p[i:i + 2], 16) for i in (1, 3, 5)): p for p in CC.CHART_PALETTE}


def _count(im) -> collections.Counter:
    """Palette pixels of a PIL image. Read through `tobytes` rather than
    `getdata`, which Pillow 12 deprecates."""
    raw = im.convert("RGB").tobytes()
    return collections.Counter(raw[i:i + 3] for i in range(0, len(raw), 3))


def _palette_of(im) -> List[str]:
    counts = _count(im)
    return [_BY_RGB[tuple(px)] for px, _n in counts.most_common() if tuple(px) in _BY_RGB]


def _chart(title: str, measure: str) -> S.Chart:
    """A plain single-series bar chart — the `subject` rule, one colour for
    the whole series, taken from the document's plan."""
    return S.Chart(type="bar", title=title, categories=["North", "South", "West"],
                   series=[S.Series(name=measure, values=[120.0, 90.0, 70.0])], y_label=measure)


def _palette_in_png(data: bytes) -> List[str]:
    """Every palette colour present in a PNG, most pixels first."""
    from PIL import Image

    return _palette_of(Image.open(io.BytesIO(data)))


def _palette_in_pdf(path: Path) -> List[str]:
    """Every palette colour present in the RENDERED pages of a PDF."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    counts: collections.Counter = collections.Counter()
    for page in pdf:
        counts.update(_count(page.render(scale=1.5).to_pil()))
    return [_BY_RGB[tuple(px)] for px, _n in counts.most_common() if tuple(px) in _BY_RGB]


def _images_in(path: Path, folder: str) -> List[bytes]:
    with zipfile.ZipFile(path) as z:
        return [z.read(n) for n in sorted(z.namelist()) if n.startswith(folder) and n.endswith(".png")]


def _palette_in_chart_xml(path: Path, folder: str) -> List[List[str]]:
    """The palette colours each native chart part of an Office file fills
    its marks with, one list per chart, in part order."""
    out: List[List[str]] = []
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist() if re.fullmatch(rf"{folder}/chart\d+\.xml", n))
        for n in names:
            xml = z.read(n).decode("utf-8", "replace")
            hexes = ["#" + h.upper() for h in re.findall(r'srgbClr val="([0-9A-Fa-f]{6})"', xml)]
            out.append([h for h in hexes if h in CC.CHART_PALETTE])
    return out


def test_a_documents_two_subjects_reach_the_docx_and_the_pdf_as_two_colours(tmp_path):
    """E-F1, the owner's reported failure: 'every chart is the same blue'
    was still true inside the document even after the colour plan existed."""
    spec = S.ArtifactSpec(kind="document", document=S.DocumentSpec(
        title="Two subjects", template_id="generic",
        blocks=[S.Paragraph(text="Revenue and head count, one document."),
                S.ChartBlock(chart=_chart("Revenue by region", "Revenue")),
                S.ChartBlock(chart=_chart("Head count by region", "Head count"))]))
    report = render_version(spec, ["docx", "pdf"], str(tmp_path), title_slug="two", version=1)
    assert report.chart_files == ["chart-1.png", "chart-2.png"]

    loose = [_palette_in_png((tmp_path / name).read_bytes()) for name in report.chart_files]
    assert [p[:1] for p in loose] == [["#2F6FB2"], ["#E07B00"]], loose

    embedded = [_palette_in_png(data) for data in _images_in(tmp_path / "two-v1.docx", "word/media/")]
    assert [p[:1] for p in embedded] == [["#2F6FB2"], ["#E07B00"]], embedded

    # And the PDF the reader actually scrolls carries both, not one twice.
    assert set(_palette_in_pdf(tmp_path / "two-v1.pdf")) == {"#2F6FB2", "#E07B00"}


def test_a_decks_chart_pictures_and_its_native_charts_use_the_same_colours(tmp_path):
    """The half of E-F1 that made the round WORSE than before it: the deck's
    native PowerPoint charts read the plan while the PDF preview of the same
    deck embedded the plan-less PNGs, so one chart was two colours."""
    spec = S.ArtifactSpec(kind="presentation", presentation=S.PresentationSpec(
        title="Two subjects", template_id="generic",
        slides=[S.Slide(layout="chart", title="Revenue by region", chart=_chart("Revenue by region", "Revenue")),
                S.Slide(layout="chart", title="Head count by region", chart=_chart("Head count by region", "Head count"))]))
    report = render_version(spec, ["pptx", "pdf"], str(tmp_path), title_slug="two", version=1)

    native = _palette_in_chart_xml(tmp_path / "two-v1.pptx", "ppt/charts")
    assert [sorted(set(c)) for c in native] == [["#2F6FB2"], ["#E07B00"]], native
    # The PNGs behind the PDF preview must agree with them, chart for chart.
    loose = [_palette_in_png((tmp_path / name).read_bytes()) for name in report.chart_files]
    assert [p[:1] for p in loose] == [["#2F6FB2"], ["#E07B00"]], loose
    assert set(_palette_in_pdf(tmp_path / "two-v1.pdf")) == {"#2F6FB2", "#E07B00"}


def test_a_workbooks_native_charts_carry_the_plan_too(tmp_path):
    """The workbook's charts are native (openpyxl), so they already read the
    resolved style — this pins the OTHER end of the comparison that made the
    document's flat blue a contradiction rather than merely dull."""
    spec = S.ArtifactSpec(kind="workbook", workbook=S.WorkbookSpec(
        title="Two subjects", template_id="generic",
        sheets=[S.Sheet(name="Revenue", columns=[S.Column(name="Region"), S.Column(name="Revenue", type="number")],
                        rows=[["North", 120], ["South", 90], ["West", 70]],
                        charts=[_chart("Revenue by region", "Revenue")]),
                S.Sheet(name="People", columns=[S.Column(name="Region"), S.Column(name="Head count", type="number")],
                        rows=[["North", 12], ["South", 9], ["West", 7]],
                        charts=[_chart("Head count by region", "Head count")])]))
    render_version(spec, ["xlsx"], str(tmp_path), title_slug="two", version=1)
    native = _palette_in_chart_xml(tmp_path / "two-v1.xlsx", "xl/charts")
    assert [sorted(set(c)) for c in native] == [["#2F6FB2"], ["#E07B00"]], native
