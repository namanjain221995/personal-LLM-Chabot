"""validate.py refuses what it must (macros, external targets, broken zips,
non-PDFs, page bombs) and preview.py rasterises pages at the asked width."""
from __future__ import annotations

import zipfile

import pytest

from app.artifacts import types as T
from app.artifacts.render import preview, validate
from tests.test_artifact_render_samples import deck, workbook


def _pptx(tmp_path):
    pytest.importorskip("pptx")
    from app.artifacts.render.pptx import render_pptx

    return render_pptx(deck().body, tmp_path / "d.pptx")


def _xlsx(tmp_path):
    pytest.importorskip("openpyxl")
    from app.artifacts.render.xlsx import render_xlsx

    return render_xlsx(workbook().body, tmp_path / "w.xlsx")


def _add_part(path, name: str, data: bytes):
    """Re-zip with one extra part (the zip stays valid)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr(name, data)
    tmp.replace(path)


def test_office_files_validate_and_report_counts(tmp_path):
    facts = validate.validate_file(_pptx(tmp_path), "pptx")
    assert facts["ok"] and facts["slides"] == 13 and len(facts["sha256"]) == 64
    facts = validate.validate_file(_xlsx(tmp_path), "xlsx")
    assert facts["ok"] and facts["sheets"] == 4 and facts["sheet_names"][0] == "Pipeline"


def test_macro_part_is_refused(tmp_path):
    path = _pptx(tmp_path)
    _add_part(path, "ppt/vbaProject.bin", b"\x00" * 16)
    with pytest.raises(validate.ValidationFailed, match="macro"):
        validate.validate_file(path, "pptx")


def test_external_relationship_target_is_refused(tmp_path):
    path = _xlsx(tmp_path)
    _add_part(path, "xl/worksheets/_rels/sheet1.xml.rels",
              b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
              b'<Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
              b'Target="http://169.254.169.254/latest/" TargetMode="External"/></Relationships>')
    with pytest.raises(validate.ValidationFailed, match="external target"):
        validate.validate_file(path, "xlsx")


def test_broken_zip_and_empty_and_wrong_format_are_refused(tmp_path):
    broken = tmp_path / "b.docx"
    broken.write_bytes(b"PK\x03\x04 not really a zip")
    with pytest.raises(validate.ValidationFailed):
        validate.validate_file(broken, "docx")
    empty = tmp_path / "e.xlsx"
    empty.write_bytes(b"")
    with pytest.raises(validate.ValidationFailed, match="not written"):
        validate.validate_file(empty, "xlsx")
    with pytest.raises(validate.ValidationFailed, match="no validator"):
        validate.validate_file(broken, "exe")
    # An XLSX is a valid zip but not a PPTX.
    with pytest.raises(validate.ValidationFailed):
        validate.validate_file(_xlsx(tmp_path), "pptx")


def test_pdf_header_and_page_rules(tmp_path):
    pytest.importorskip("pypdfium2")
    fake = tmp_path / "f.pdf"
    fake.write_bytes(b"%PDF-1.7 but nothing else")
    with pytest.raises(validate.ValidationFailed, match="could not be opened"):
        validate.validate_file(fake, "pdf")
    fake.write_bytes(b"not a pdf at all")
    with pytest.raises(validate.ValidationFailed, match="header"):
        validate.validate_file(fake, "pdf")


def test_pdf_validates_and_rasterises_at_the_asked_width(tmp_path):
    pytest.importorskip("weasyprint")
    from app.artifacts import spec as S
    from app.artifacts.render import html as H
    from app.artifacts.render.pdf import render_html_pdf

    spec = S.DocumentSpec(title="t", blocks=[S.Paragraph(text="one"), S.PageBreak(), S.Paragraph(text="two")])
    render_html_pdf(H.document_html(spec), tmp_path / "t.pdf", tmp_path)
    facts = validate.validate_file(tmp_path / "t.pdf", "pdf")
    assert facts["pages"] == 2
    with pytest.raises(validate.ValidationFailed, match="ceiling is 1"):
        validate.validate_pdf(tmp_path / "t.pdf", max_pages=1)
    assert preview.page_count(tmp_path / "t.pdf") == 2
    from PIL import Image
    import io

    for width in T.PREVIEW_WIDTHS:
        png = preview.rasterise_page(tmp_path / "t.pdf", 2, width)
        with Image.open(io.BytesIO(png)) as im:
            assert im.width == width
            assert abs(im.height / im.width - 297 / 210) < 0.01
    with pytest.raises(IndexError):
        preview.rasterise_page(tmp_path / "t.pdf", 3, 240)
    with pytest.raises(IndexError):
        preview.rasterise_page(tmp_path / "t.pdf", 0, 240)
    with pytest.raises(ValueError):
        preview.rasterise_page(tmp_path / "t.pdf", 1, 10)


def test_validate_all_collects_every_file_and_the_preview(tmp_path):
    pytest.importorskip("weasyprint")
    from app.artifacts import spec as S
    from app.artifacts.render import html as H
    from app.artifacts.render.pdf import render_html_pdf

    render_html_pdf(H.document_html(S.DocumentSpec(title="t", blocks=[S.Paragraph(text="x")])), tmp_path / "p.pdf", tmp_path)
    out = validate.validate_all({"pptx": str(_pptx(tmp_path)), "xlsx": str(_xlsx(tmp_path))}, str(tmp_path / "p.pdf"))
    assert set(out["files"]) == {"pptx", "xlsx"} and out["preview"]["pages"] == 1
    assert out["renderer_version"] == T.RENDERER_VERSION
