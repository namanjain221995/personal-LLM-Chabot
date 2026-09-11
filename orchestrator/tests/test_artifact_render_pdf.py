"""HTML → PDF: the fetcher fetches nothing, page caps hold, no blank trailing
page, one preview page per slide, every template renders.

The fetcher unit tests run everywhere; the tests that produce a PDF need
WeasyPrint (excluded from requirements-dev by design — see the comment
there) and skip when it is absent. They run for real in the venv that has
it and inside the :cpu image.
"""
from __future__ import annotations

import http.server
import socketserver
import threading
import time

import pytest

from app.artifacts import spec as S
from app.artifacts import types as T
from app.artifacts.render import html as H
from app.artifacts.render import pdf as P
from app.artifacts.render import theme
from tests.test_artifact_render_samples import deck, document, revenue_chart


@pytest.fixture
def weasy():
    return pytest.importorskip("weasyprint")


@pytest.fixture
def assets(tmp_path):
    """An assets dir holding one real chart PNG (and a decoy outside it)."""
    from app.artifacts.render import charts

    charts.render_chart_png(revenue_chart(), tmp_path / "chart-1.png")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.png"
    outside.write_bytes((tmp_path / "chart-1.png").read_bytes())
    return tmp_path


# --------------------------------------------------------------- fetcher --


def _body(resource) -> bytes:
    return resource["string"] if isinstance(resource, dict) else resource.read()


def _mime(resource) -> str:
    return resource["mime_type"] if isinstance(resource, dict) else resource.content_type


def test_fetcher_serves_only_a_bare_png_inside_the_assets_dir(assets):
    fetch = P.make_url_fetcher(assets)
    ok = fetch(f"file://{assets}/chart-1.png")
    assert _mime(ok) == "image/png" and _body(ok)[:8] == b"\x89PNG\r\n\x1a\n"


def test_fetcher_meets_the_installed_weasyprints_contract(weasy, assets):
    """Through WeasyPrint's OWN `fetch()`, not ours: 70 asserts the result is
    a URLFetcherResponse and reads `_fail_on_errors` on a refusal (a plain
    function raised AttributeError there — the 2026-09-11 e2e failure)."""
    from weasyprint.urls import URLFetchingError, fetch as weasy_fetch

    fetcher = P.make_url_fetcher(assets)
    with weasy_fetch(fetcher, f"file://{assets}/chart-1.png") as resource:
        assert resource.read()[:8] == b"\x89PNG\r\n\x1a\n" and resource.content_type == "image/png"
    with pytest.raises(URLFetchingError):
        with weasy_fetch(fetcher, "http://127.0.0.1:1/x.png"):
            pass


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:1/x.png",
    "https://example.com/x.png",
    "data:image/png;base64,iVBORw0KGgo=",
    "ftp://example.com/x.png",
    "file:///etc/passwd",
    "file:///etc/hostname.png",
    "chart-1.png",                       # relative: WeasyPrint always resolves first; a bare name here is refused
    "//example.com/x.png",
])
def test_fetcher_refuses_every_other_shape(assets, url):
    fetch = P.make_url_fetcher(assets)
    with pytest.raises(P.RefusedFetch):
        fetch(url)


def test_fetcher_refuses_traversal_symlink_and_non_image(assets):
    fetch = P.make_url_fetcher(assets)
    with pytest.raises(P.RefusedFetch):
        fetch(f"file://{assets}/../{assets.name}-outside.png")
    with pytest.raises(P.RefusedFetch):
        fetch(f"file://{assets.parent}/{assets.name}-outside.png")
    link = assets / "link.png"
    link.symlink_to(assets.parent / f"{assets.name}-outside.png")
    with pytest.raises(P.RefusedFetch):
        fetch(f"file://{link}")
    (assets / "print.css").write_text("body{}")
    with pytest.raises(P.RefusedFetch):
        fetch(f"file://{assets}/print.css")
    with pytest.raises(P.RefusedFetch):
        fetch(f"file://{assets}/missing.png")


# ------------------------------------------------------------------ SSRF --


class _Recorder(http.server.BaseHTTPRequestHandler):
    hits: list = []

    def do_GET(self):  # noqa: N802 - http.server's name
        type(self).hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.end_headers()
        self.wfile.write(b"\x89PNG\r\n\x1a\n")

    def log_message(self, *args):  # silence
        return


@pytest.fixture
def listener():
    _Recorder.hits = []
    server = socketserver.TCPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_a_hostile_page_produces_no_fetch(weasy, assets, listener, monkeypatch):
    """PROOF, not a regex: a listening socket that must receive nothing, and
    a recording fetcher that must have refused every attempt."""
    port = listener
    attempts: list = []

    class Recording(P.AssetFetcher):
        def fetch(self, url):
            try:
                return super().fetch(url)
            except P.RefusedFetch as exc:
                attempts.append((url, str(exc)))
                raise

    monkeypatch.setattr(P, "make_url_fetcher", Recording)
    html = (
        "<!DOCTYPE html><html><head><style>"
        f"@import url('http://127.0.0.1:{port}/import.css');"
        f"body{{background:url('http://127.0.0.1:{port}/bg.png')}}"
        f"@font-face{{font-family:x;src:url('http://127.0.0.1:{port}/f.woff')}}"
        "</style>"
        f'<link rel="stylesheet" href="http://127.0.0.1:{port}/link.css">'
        "</head><body>"
        f'<p style="font-family:x">hostile</p><img src="http://127.0.0.1:{port}/img.png">'
        '<img src="file:///etc/hostname"><img src="../chart-1.png"><img src="chart-1.png">'
        "</body></html>"
    )
    pages = P.render_html_pdf(html, assets / "hostile.pdf", assets)
    time.sleep(0.2)
    assert pages == 1
    assert _Recorder.hits == []
    refused = {u for u, _ in attempts}
    assert any(u.startswith(f"http://127.0.0.1:{port}/") for u in refused)
    assert any(u.startswith("file:///etc/hostname") for u in refused)
    assert any("outside" in why for _, why in attempts)
    # The legitimate chart WAS served (no refusal recorded for it).
    assert not any(u.endswith("/chart-1.png") and str(assets) in u for u in refused)


def test_a_spec_cannot_smuggle_a_url_into_the_pdf_path(weasy, assets, listener):
    """Belt and braces: the spec's strings are escaped before the fetcher
    ever sees them, so a URL in a title is text, not a reference."""
    port = listener
    hostile = f'<img src="http://127.0.0.1:{port}/t.png">'
    spec = S.DocumentSpec(title=hostile, blocks=[S.Paragraph(text=hostile), S.ChartBlock(chart=revenue_chart(caption=hostile))])
    P.render_html_pdf(H.document_html(spec), assets / "spec.pdf", assets)
    time.sleep(0.2)
    assert _Recorder.hits == []


# --------------------------------------------------------------- pages --


def test_trailing_page_break_does_not_add_a_blank_page(weasy, tmp_path):
    spec = S.DocumentSpec(title="t", blocks=[S.Paragraph(text="one paragraph"), S.PageBreak()])
    pages = P.render_html_pdf(H.document_html(spec), tmp_path / "a.pdf", tmp_path)
    assert pages == 1
    spec2 = S.DocumentSpec(title="t", blocks=[S.Paragraph(text="one"), S.PageBreak(), S.Paragraph(text="two"), S.PageBreak(), S.PageBreak()])
    assert P.render_html_pdf(H.document_html(spec2), tmp_path / "b.pdf", tmp_path) == 2
    # And no page is blank: every page has text.
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(tmp_path / "b.pdf"))
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            text = page.get_textpage().get_text_range()
            page.close()
            assert text.strip()
    finally:
        pdf.close()


def test_max_pages_is_enforced_after_render(weasy, tmp_path):
    blocks = [S.Paragraph(text="p"), S.PageBreak()] * (T.MAX_PAGES + 2)
    spec = S.DocumentSpec(title="bomb", blocks=blocks)
    with pytest.raises(ValueError) as exc:
        P.render_html_pdf(H.document_html(spec), tmp_path / "bomb.pdf", tmp_path)
    assert f"the ceiling is {T.MAX_PAGES}" in str(exc.value)
    assert not (tmp_path / "bomb.pdf").exists()
    # A caller may lower the cap.
    with pytest.raises(ValueError):
        P.render_html_pdf(H.document_html(S.DocumentSpec(title="t", blocks=[S.Paragraph(text="a"), S.PageBreak(), S.Paragraph(text="b")])), tmp_path / "two.pdf", tmp_path, max_pages=1)


def test_presentation_preview_has_exactly_one_page_per_slide(weasy, tmp_path):
    from app.artifacts.render import charts

    spec = deck("ceo").body
    plan = H.plan_deck(spec)
    for n, chart in enumerate(H.spec_charts(S.ArtifactSpec(kind="presentation", presentation=spec)), start=1):
        charts.render_chart_png(chart, tmp_path / H.chart_filename(n))
    pages = P.render_html_pdf(H.deck_html(spec, plan), tmp_path / "deck.pdf", tmp_path)
    assert pages == len(plan.slides) == 13
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(tmp_path / "deck.pdf"))
    try:
        w, h = pdf[0].get_size()
    finally:
        pdf.close()
    assert abs(w / h - 16 / 9) < 0.01
    assert abs(w - 13.333 * 72) < 1.0


def _page_text_and_boxes(pdf_path, page_index: int = 0):
    """(text, charbox(i)) for one page; boxes are (left, bottom, right, top)
    in points from the page's bottom-left corner."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page = pdf[page_index]
        tp = page.get_textpage()
        text = tp.get_text_range()
        boxes = [tp.get_charbox(i) for i in range(tp.count_chars())]
        page.close()
    finally:
        pdf.close()
    return text, boxes


def test_fitted_bullets_all_land_inside_the_body_box(weasy, tmp_path):
    """Eight 155-character bullets, within the generic cap: before the line
    budget the eighth was clipped out of the page (absent from the text
    layer) and the seventh ended at y = 42 pt, under the footer line at
    y = 39.6 pt. Now every planned bullet's last character sits above the
    body box's bottom edge."""
    bullets = [(f"Bullet {k} " + "lorem ipsum dolor sit amet " * 6).strip()[:150].rstrip() + f" END{k}" for k in range(1, 9)]
    spec = S.PresentationSpec(title="d", slides=[S.Slide(layout="bullets", title="t", bullets=bullets)])
    plan = H.plan_deck(spec)
    assert P.render_html_pdf(H.deck_html(spec, plan), tmp_path / "fit.pdf", tmp_path) == 1
    text, boxes = _page_text_and_boxes(tmp_path / "fit.pdf")
    page_h = theme.SLIDE_H_IN * 72
    body_bottom = page_h - (theme.SLIDE_BODY_TOP_IN + theme.SLIDE_BODY_H_IN) * 72
    for t in plan.slides[0].bullets:
        tail = t[-12:]
        idx = text.find(tail)
        assert idx >= 0, f"planned bullet ending {tail!r} is not in the page's text layer"
        last = boxes[idx + len(tail) - 1]
        assert last[1] >= body_bottom - 0.5, (tail, last)
    assert len(plan.slides[0].bullets) >= 6 and plan.warnings


def test_table_slide_preview_shows_the_planned_rows_only(weasy, tmp_path):
    """Preview and .pptx carry the same ten rows: Row 10 is on the page,
    Row 11 is not (it used to be drawn and clipped mid-row)."""
    big = S.Table(columns=["Name", "Value"], rows=[[f"Row {i}", i] for i in range(1, 41)], numeric_columns=[1])
    spec = S.PresentationSpec(title="d", slides=[S.Slide(layout="title", title="t"), S.Slide(layout="table", title="Numbers", table=big)])
    plan = H.plan_deck(spec)
    assert P.render_html_pdf(H.deck_html(spec, plan), tmp_path / "table.pdf", tmp_path) == 2
    text, boxes = _page_text_and_boxes(tmp_path / "table.pdf", 1)
    assert "Row 10" in text and "Row 11" not in text
    footer_top = theme.SLIDE_H_IN * 72 - theme.SLIDE_FOOTER_TOP_IN * 72
    idx = text.find("Row 10")
    assert boxes[idx][1] > footer_top, "the last planned row must sit above the footer"


def test_heading_numbers_in_the_page_are_the_plans(weasy, tmp_path):
    spec = S.DocumentSpec(title="t", template_id="technical_report", toc=False, blocks=[
        S.Heading(level=1, text="Alpha"), S.Heading(level=3, text="Deep"), S.Heading(level=2, text="Beta"),
    ])
    plan = H.plan_document(spec)
    P.render_html_pdf(H.document_html(spec, plan), tmp_path / "h.pdf", tmp_path)
    text, _ = _page_text_and_boxes(tmp_path / "h.pdf")
    assert [h.number for h in plan.headings] == ["1", "1.1.1", "1.2"]
    assert "1.1.1" in text and "1.0.1" not in text and "Deep" in text


@pytest.mark.parametrize("template_id", T.DOCUMENT_TEMPLATES)
def test_every_document_template_renders_to_pages(weasy, tmp_path, template_id):
    from app.artifacts.render import charts

    spec = document(template_id, sections=10)
    for n, chart in enumerate(H.spec_charts(spec), start=1):
        charts.render_chart_png(chart, tmp_path / H.chart_filename(n))
    plan = H.plan_document(spec.body)
    pages = P.render_html_pdf(H.document_html(spec.body, plan), tmp_path / f"{template_id}.pdf", tmp_path)
    assert 0 < pages <= T.MAX_PAGES
    minimum = 1 + (1 if plan.cover else 0) + (1 if plan.toc else 0)
    assert pages >= minimum


def test_running_header_footer_and_toc_page_numbers_exist(weasy, tmp_path):
    import pypdfium2 as pdfium

    spec = document("technical_report", sections=3).body
    P.render_html_pdf(H.document_html(spec), tmp_path / "t.pdf", tmp_path)
    pdf = pdfium.PdfDocument(str(tmp_path / "t.pdf"))
    try:
        texts = []
        for i in range(len(pdf)):
            page = pdf[i]
            texts.append(page.get_textpage().get_text_range())
            page.close()
    finally:
        pdf.close()
    n = len(texts)
    assert f"Page 2 of {n}" in texts[1] and f"Page {n} of {n}" in texts[-1]
    assert "CONFIDENTIAL" in texts[1]
    assert "Contents" in texts[1] and "1.1" in texts[1]     # the TOC page with numbered entries
    assert "Page 1 of" not in texts[0]                        # the cover carries no footer


# ------------------------------------------------------ tabular document --


def _page_texts(path):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        out = []
        for i in range(len(pdf)):
            page = pdf[i]
            w, h = page.get_size()
            out.append((w, h, page.get_textpage().get_text_range()))
        return out
    finally:
        pdf.close()


def test_workbook_pdf_is_landscape_repeats_the_header_and_has_no_blank_page(weasy, tmp_path):
    """The PDF twin of the tabular document (CONTRACT-2 §1): landscape
    pages for a nine-column sheet, the header row on every page, the title
    and the highlighted header in the text, every row present, no blank
    trailing page, page numbers."""
    from app.artifacts import spec as S
    from app.artifacts.render import html as H
    from app.artifacts.render.pdf import render_html_pdf
    from tests.test_artifact_render_xlsx import _styled

    spec = _styled(rows=120, highlight=[S.Highlight(column="Audit Comments", color="red")], wrap=True)
    spec.body.sheets.append(S.Sheet(name="Summary", columns=[S.Column(name="Outcome"), S.Column(name="Count", type="integer")], rows=[["Selected", 120]]))
    html = H.workbook_document_html(spec.body)
    assert "9C0006" in html and "FFC7CE" in html and "table-layout" in H.print_css()
    pages = render_html_pdf(html, tmp_path / "t.pdf", tmp_path)
    texts = _page_texts(tmp_path / "t.pdf")
    assert pages == len(texts) >= 4
    landscape = [w > h for w, h, _ in texts]
    assert all(landscape[:-1]) and landscape[-1] is False, "the audit sheet is landscape; the two-column summary sheet is portrait"
    first = texts[0][2]
    assert "Audit" in first and "Blank values are blank in the source." in first and "Audit Comments" in first
    for _, _, text in texts[:-1]:
        assert "Audit Comments" in text and "Session ID" in text, "the header row repeats on every page of the table"
        assert "Page " in text
    assert "Cand 0" in first and "Cand 119" in "".join(t for _, _, t in texts), "every row is there"
    assert texts[-1][2].strip(), "no blank trailing page"
    assert "Summary" in texts[-1][2] and "Selected" in texts[-1][2]


def test_workbook_pdf_columns_are_not_clipped(weasy, tmp_path):
    """A 12-column sheet with a 300-character comment: every column's
    header is in the page text, and the long text wraps within its share
    rather than running off the page (the column shares are bounded)."""
    from app.artifacts import spec as S
    from app.artifacts.render import html as H
    from app.artifacts.render.pdf import render_html_pdf

    names = [f"Column number {i}" for i in range(1, 12)] + ["Notes"]
    rows = [[f"v{i}-{j}" for j in range(11)] + ["word " * 60] for i in range(15)]
    spec = S.WorkbookSpec(title="Wide", sheets=[S.Sheet(name="Wide", columns=[S.Column(name=n) for n in names], rows=rows)])
    shares = H.column_shares(spec.sheets[0])
    assert abs(sum(shares) - 1.0) < 0.01 and max(shares) <= H._MAX_COL_SHARE + 0.01 and min(shares) >= H._MIN_COL_SHARE - 0.01
    render_html_pdf(H.workbook_document_html(spec), tmp_path / "w.pdf", tmp_path)
    w, h, text = _page_texts(tmp_path / "w.pdf")[0]
    assert w > h
    for n in names:
        assert n.split()[0] in text
    assert "v14-10" in "".join(t for _, _, t in _page_texts(tmp_path / "w.pdf")), "the last cell of the last column is on the page"
