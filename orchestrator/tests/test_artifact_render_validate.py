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


# ------------------------------------------------ row counts and the grid --


def test_validate_all_keys_by_role_format_sheet_and_checks_rows_against_the_spec(tmp_path):
    """CONTRACT-2 §11: `role:format:sheet_slug` keys; a CSV is held to its
    sheet's rows and columns, an XLSX to every sheet's rows; the mismatch
    sentence carries both numbers."""
    pytest.importorskip("openpyxl")
    from app.artifacts.render.csv import write_csv
    from app.artifacts.render.xlsx import render_xlsx

    spec = workbook(rows=30)
    body = spec.body
    xlsx = render_xlsx(body, tmp_path / "w.xlsx")
    pipeline, regions = body.sheets[0], body.sheets[1]
    write_csv([c.name for c in pipeline.columns], pipeline.rows, tmp_path / "w-pipeline.csv")
    write_csv([c.name for c in regions.columns], regions.rows, tmp_path / "w-regions.csv")
    out = validate.validate_all({"primary:xlsx:": str(xlsx), "data:csv:pipeline": str(tmp_path / "w-pipeline.csv"), "data:csv:regions": str(tmp_path / "w-regions.csv")}, spec=spec)
    assert set(out["files"]) == {"primary:xlsx:", "data:csv:pipeline", "data:csv:regions"}
    assert out["files"]["primary:xlsx:"]["sheet_rows"] == {"Pipeline": 30, "Regions": 3, "Notes   draft": 2}
    assert out["files"]["primary:xlsx:"]["rows"] == 35 and out["files"]["primary:xlsx:"]["columns"] == 6
    assert out["files"]["data:csv:pipeline"]["rows"] == 30 and out["files"]["data:csv:pipeline"]["columns"] == 6
    assert out["files"]["data:csv:regions"]["rows"] == 3
    # One row short: refused with both numbers.
    write_csv([c.name for c in pipeline.columns], pipeline.rows[:-1], tmp_path / "w-pipeline.csv")
    with pytest.raises(validate.ValidationFailed, match="the CSV has 29 data rows; 30 were required"):
        validate.validate_all({"data:csv:pipeline": str(tmp_path / "w-pipeline.csv")}, spec=spec)
    # A column short too.
    write_csv([c.name for c in pipeline.columns][:-1], [r[:-1] for r in pipeline.rows], tmp_path / "w-pipeline.csv")
    with pytest.raises(validate.ValidationFailed, match="header has 5 columns; 6 were required"):
        validate.validate_all({"data:csv:pipeline": str(tmp_path / "w-pipeline.csv")}, spec=spec)
    # The XLSX against a spec with one row more than the file holds.
    more = body.model_copy(deep=True)
    more.sheets[1].rows.append(["East", 1, 2])
    with pytest.raises(validate.ValidationFailed, match="sheet 'Regions' of the XLSX has 3 data rows; 4 were required"):
        validate.validate_all({"primary:xlsx:": str(xlsx)}, spec=more)
    # Bare format keys still work (the older callers), without a spec.
    assert set(validate.validate_all({"xlsx": str(xlsx)})["files"]) == {"xlsx"}
    assert validate.split_key("data:csv:pipeline") == ("data", "csv", "pipeline") and validate.split_key("pdf") == ("", "pdf", "")


def test_xlsx_row_count_ignores_totals_and_chart_blocks_and_keeps_blank_rows(tmp_path):
    pytest.importorskip("openpyxl")
    from app.artifacts import spec as S
    from app.artifacts.render.xlsx import render_xlsx

    sheet = S.Sheet.model_validate({
        "name": "p", "columns": [{"name": "k"}, {"name": "v", "type": "number"}],
        "rows": [["a", 1], [None, None], ["c", 3]], "totals": [{"column": "v", "fn": "sum"}],
        "charts": [{"type": "bar", "title": "Elsewhere", "categories": ["x", "y", "z", "w"], "series": [{"name": "other", "values": [3, 4, 5, 6]}]}],
    })
    body = S.WorkbookSpec(title="w", sheets=[sheet])
    path = render_xlsx(body, tmp_path / "w.xlsx")
    facts = validate.validate_xlsx(path, spec=body)
    assert facts["sheet_rows"] == {"p": 3}, "the interior blank row counts; the totals row and the chart data block do not"


def test_grid_for_reads_a_csv_and_an_xlsx_in_one_shape(tmp_path):
    pytest.importorskip("openpyxl")
    from app.artifacts.render.csv import write_csv
    from app.artifacts.render.xlsx import render_xlsx

    spec = workbook("dashboard", rows=30)
    xlsx = render_xlsx(spec.body, tmp_path / "w.xlsx")
    grid = preview.grid_for(xlsx, "xlsx", sheet="Pipeline", offset=10, limit=5, max_cols=3)
    assert grid["sheets"] == ["Dashboard", "Pipeline", "Regions", "Notes   draft", "Notes"] and grid["sheet"] == "Pipeline"
    assert grid["columns"] == ["Deal", "Close date", "Amount"] and len(grid["rows"]) == 5 and grid["rows"][0][0] == "Deal 11"
    assert grid["total_columns"] == 6 and grid["total_rows"] >= 31 and grid["truncated"] is True and grid["formulas_as_text"] is True
    assert preview.grid_for(xlsx, "xlsx", sheet="Pipeline", offset=0, limit=100, max_cols=60)["truncated"] is False
    first = preview.grid_for(xlsx, "xlsx")
    assert first["sheet"] == "Dashboard" and "='Pipeline'!C32" in [c for r in first["rows"] for c in r]
    # The xlsx grid is trimmed to the sheet's real last column, not padded to max_cols.
    regions = preview.grid_for(xlsx, "xlsx", sheet="Regions", max_cols=60)
    assert regions["columns"] == ["Region", "Q1", "Q2"] and all(len(r) == 3 for r in regions["rows"])
    with pytest.raises(KeyError):
        preview.grid_for(xlsx, "xlsx", sheet="Nope")
    write_csv(["Id", "Name"], [[str(i), f"n{i}"] for i in range(12)], tmp_path / "d.csv")
    grid = preview.grid_for(tmp_path / "d.csv", "csv", offset=10, limit=5, title="Leads — Data")
    assert grid == {"sheets": ["Leads — Data"], "sheet": "Leads — Data", "columns": ["Id", "Name"], "rows": [["10", "n10"], ["11", "n11"]],
                    "total_rows": 12, "total_columns": 2, "truncated": False, "formulas_as_text": True}
    assert preview.grid_for(tmp_path / "d.csv", "csv")["sheets"] == ["d"], "no title: the file's stem"
    with pytest.raises(ValueError):
        preview.grid_for(tmp_path / "d.csv", "pdf")


def test_grid_shows_a_date_cell_as_a_date():
    """openpyxl reads a date cell back as a datetime at midnight; the grid
    showed "2026-08-03T00:00:00" in the 2026-09-12 screenshots."""
    import datetime as _dt

    from app.artifacts.render import preview

    assert preview._json_value(_dt.datetime(2026, 8, 3)) == "2026-08-03"
    assert preview._json_value(_dt.datetime(2026, 8, 3, 9, 30)) == "2026-08-03T09:30:00"
    assert preview._json_value(_dt.date(2026, 8, 3)) == "2026-08-03"
