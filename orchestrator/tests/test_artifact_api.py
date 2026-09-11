"""The artifact HTTP surface (app/artifacts/api.py) through the real app.

Owner-scoping, the inline/attachment distinction, Range and HEAD on files,
page images rasterised once and cached, the sheet grid, job status, cancel,
retry, convert — and that another person's id answers 404 like a missing
one. The version on disk is produced by the real pipeline with the composer
stubbed and the render subprocess replaced by a writer of real bytes.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import db, metrics
from app.artifacts import db as adb
from app.artifacts import pipeline
from app.artifacts import spec as S
from app.artifacts import types as T
from app.config import settings
from app.main import app


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path / "reports"))
    monkeypatch.setattr(settings, "artifact_lease_ttl_s", 60.0)
    monkeypatch.setattr(settings, "artifact_stage_timeout_s", 30.0)
    monkeypatch.setattr(settings, "artifact_render_timeout_s", 30.0)
    monkeypatch.setattr(settings, "artifact_min_free_mb", 1)
    monkeypatch.setattr(settings, "artifact_user_quota_mb", 1024)
    monkeypatch.setattr(settings, "video_pace_max_wait_s", 0.0)
    monkeypatch.setattr(settings, "session_secret_file", str(tmp_path / ".session_secret"))
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    pipeline.reset_for_tests()
    metrics.reset()
    yield
    pipeline.reset_for_tests()
    pipeline.set_composer(None)
    metrics.reset()


_PDF = b"%PDF-1.7\n" + b"x" * 4000 + b"\n%%EOF"


def _install(monkeypatch):
    async def composer(ctx):
        await ctx.progress_stage("intent", "done", "")
        if ctx.kind == "workbook":
            return S.parse_body("workbook", {"title": "Budget", "sheets": [{"name": "Data", "columns": [{"name": "Item"}, {"name": "Cost", "type": "currency"}], "rows": [["Rent", 1000]], "totals": [{"column": 1, "fn": "sum"}]}]})
        return S.parse_body("document", {"title": "Quarterly Review", "blocks": [{"type": "paragraph", "text": "Hello."}]})

    async def render(work_dir, spec, formats, title_slug, version, effort):
        files = []
        for fmt in formats:
            name = f"{title_slug}-v{version}.{fmt}"
            body = _PDF if fmt == "pdf" else (f"{fmt} bytes".encode() * 100)
            with open(os.path.join(work_dir, name), "wb") as fh:
                fh.write(body)
            files.append({"format": fmt, "filename": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), "pages": 3 if fmt == "pdf" else None})
        with open(os.path.join(work_dir, T.PREVIEW_PDF_NAME), "wb") as fh:
            fh.write(_PDF)
        return {"files": files, "preview_pdf": T.PREVIEW_PDF_NAME, "preview_kind": "grid" if spec.kind == "workbook" else "pages", "preview_pages": 3, "warnings": [], "validation": {"reopened": True}, "chart_files": [], "timings": {}}

    pipeline.set_composer(composer)
    monkeypatch.setattr(pipeline, "_render_in_subprocess", render)
    monkeypatch.setattr(pipeline, "_page_count", lambda pdf: 3)
    monkeypatch.setattr(pipeline, "_rasterise_page", lambda pdf, page, width: b"\x89PNG-" + str(width).encode())


def _make(owner_id: int, *, kind="document", formats=("pdf", "docx"), conv="conv-api", gen=None) -> dict:
    job = pipeline.accept(user_id=owner_id, conversation_id=conv, generation_id=gen or f"g-{kind}", operation="create", instruction="Make it",
                          kind=kind, formats=list(formats), effort="fast", mode="assistant", template_id="generic", material={})

    async def run():
        await pipeline.ensure_running(job["id"])
        return await pipeline.wait_for(job["id"])

    row = asyncio.run(run())
    assert row["status"] == "completed", row
    return row


@pytest.fixture()
def alice(login_client, monkeypatch):
    _install(monkeypatch)
    client = login_client("api-alice")
    uid = int(db.get_user_by_username("api-alice")["id"])
    return client, uid


def test_listing_and_detail_are_owner_scoped(alice, login_client):
    client, uid = alice
    row = _make(uid)
    aid = row["artifact_id"]
    listing = client.get("/artifacts", params={"conversation_id": "conv-api"}).json()["artifacts"]
    assert [a["artifact_id"] for a in listing] == [aid] and listing[0]["status"] == "completed"
    detail = client.get(f"/artifacts/{aid}").json()
    assert detail["artifact"]["current_version"] == 1 and len(detail["versions"]) == 1
    version = client.get(f"/artifacts/{aid}/v/1").json()
    assert [f["format"] for f in version["files"]] == ["pdf", "docx"] and "validation" in version

    bob = login_client("api-bob")
    assert bob.get(f"/artifacts/{aid}").status_code == 404
    assert bob.get(f"/artifacts/{aid}/v/1").status_code == 404
    assert bob.get(f"/artifacts/{aid}/v/1/file/pdf").status_code == 404
    assert bob.get(f"/artifacts/{aid}/v/1/preview/1.png").status_code == 404
    assert bob.get(f"/artifacts/jobs/{row['id']}").status_code == 404
    assert bob.post(f"/artifacts/jobs/{row['id']}/cancel").status_code == 404
    assert bob.get("/artifacts", params={"conversation_id": "conv-api"}).json()["artifacts"] == []


def test_ids_that_are_not_ids_are_404_before_any_lookup(alice):
    client, _ = alice
    for bad in ("../../etc/passwd", "a" * 31, "A" * 32, "x%2e%2e", "abc"):
        assert client.get(f"/artifacts/{bad}").status_code == 404
        assert client.get(f"/artifacts/{bad}/v/1/file/pdf").status_code == 404
    aid = "b" * 32
    assert client.get(f"/artifacts/{aid}/v/0/file/pdf").status_code == 404
    assert client.get(f"/artifacts/{aid}/v/1/file/exe").status_code == 404


def test_download_and_inline_differ_and_ranges_work(alice):
    client, uid = alice
    row = _make(uid)
    aid = row["artifact_id"]
    down = client.get(f"/artifacts/{aid}/v/1/file/pdf")
    assert down.status_code == 200 and down.content == _PDF
    assert down.headers["content-type"].startswith("application/pdf")
    assert down.headers["content-disposition"].startswith('attachment; filename="quarterly-review-v1.pdf"')
    assert "filename*=UTF-8''" in down.headers["content-disposition"]
    assert down.headers["cache-control"] == "private, no-store"
    assert down.headers["etag"] == f'"{hashlib.sha256(_PDF).hexdigest()}"'
    assert down.headers["accept-ranges"] == "bytes"

    inline = client.get(f"/artifacts/{aid}/v/1/file/pdf", params={"disposition": "inline"})
    assert inline.headers["content-disposition"].startswith("inline;")

    head = client.head(f"/artifacts/{aid}/v/1/file/pdf")
    assert head.status_code == 200 and head.content == b"" and int(head.headers["content-length"]) == len(_PDF)

    part = client.get(f"/artifacts/{aid}/v/1/file/pdf", headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and part.content == _PDF[:10]
    assert part.headers["content-range"] == f"bytes 0-9/{len(_PDF)}"

    bad = client.get(f"/artifacts/{aid}/v/1/file/pdf", headers={"Range": f"bytes={len(_PDF) + 10}-"})
    assert bad.status_code == 416

    docx = client.get(f"/artifacts/{aid}/v/1/file/docx")
    assert docx.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert client.get(f"/artifacts/{aid}/v/1/file/pptx").status_code == 404, "a format this version does not have"


def test_preview_pdf_and_page_images_are_cached(alice, monkeypatch):
    client, uid = alice
    row = _make(uid)
    aid = row["artifact_id"]
    pdf = client.get(f"/artifacts/{aid}/v/1/preview")
    assert pdf.status_code == 200 and pdf.headers["content-disposition"].startswith("inline;") and pdf.content == _PDF

    calls = {"n": 0}

    def rasterise(pdf_path, page, width):
        calls["n"] += 1
        return b"\x89PNG-" + str(page).encode() + b"-" + str(width).encode()

    from app.artifacts.render import preview as preview_mod

    monkeypatch.setattr(preview_mod, "rasterise_page", rasterise)
    first = client.get(f"/artifacts/{aid}/v/1/preview/2.png", params={"w": 1400})
    assert first.status_code == 200 and first.headers["content-type"] == "image/png" and first.content == b"\x89PNG-2-1400"
    again = client.get(f"/artifacts/{aid}/v/1/preview/2.png", params={"w": 1400})
    assert again.content == first.content and calls["n"] == 1, "rasterised once, served from the cache after"
    # Width snaps to the two the contract allows; the thumbnail is its own file.
    thumb = client.get(f"/artifacts/{aid}/v/1/preview/2.png", params={"w": 300})
    assert thumb.content == b"\x89PNG-2-240" and calls["n"] == 2
    assert client.get(f"/artifacts/{aid}/v/1/preview/4.png").status_code == 404, "past preview_pages"
    assert client.get(f"/artifacts/{aid}/v/1/preview/0.png").status_code == 404


def test_the_sheet_grid_is_bounded_and_never_evaluates(alice, monkeypatch):
    client, uid = alice
    row = _make(uid, kind="workbook", formats=("xlsx",))
    aid = row["artifact_id"]

    from app.artifacts.render import preview as preview_mod

    seen = {}

    def grid(path, sheet, max_rows, max_cols):
        seen.update(sheet=sheet, rows=max_rows, cols=max_cols)
        return {"sheets": [{"name": "Data", "rows": 2, "cols": 2}], "sheet": {"name": "Data", "columns": ["Item", "Cost"], "rows": [["Rent", 1000], ["Total", "=SUM(B2:B2)"]], "truncated": False, "formulas": {"B3": "=SUM(B2:B2)"}}}

    monkeypatch.setattr(preview_mod, "sheet_grid", grid)
    resp = client.get(f"/artifacts/{aid}/v/1/sheets", params={"rows": 5000, "cols": 50})
    assert resp.status_code == 422, "rows over the ceiling are refused by validation, not clamped silently"
    resp = client.get(f"/artifacts/{aid}/v/1/sheets", params={"rows": 100})
    assert resp.status_code == 200 and resp.json()["sheet"]["formulas"]["B3"] == "=SUM(B2:B2)"
    assert seen == {"sheet": None, "rows": 100, "cols": 50}
    assert resp.headers["cache-control"] == "private, no-store"
    # A document version has no workbook to grid.
    doc = _make(uid, conv="conv-api-2")
    assert client.get(f"/artifacts/{doc['artifact_id']}/v/1/sheets").status_code == 404


# ------------------------------------------------ files by id, zip, grid --


def _files_of(client, aid: str, version: int = 1) -> dict:
    """{format: file dict} of a version as the API describes it."""
    return {f["format"]: f for f in client.get(f"/artifacts/{aid}/v/{version}").json()["files"]}


def test_a_file_is_served_by_its_id_with_the_same_rules_as_by_format(alice, login_client, monkeypatch):
    """CONTRACT-2 §2: /f/{file_id} is the URL every FileRef carries —
    owner-scoped, inline vs attachment, HEAD, Range, ETag exactly as
    /file/{fmt}, which stays as the first-of-format alias."""
    client, uid = alice
    row = _make(uid)
    aid = row["artifact_id"]
    files = _files_of(client, aid)
    pdf = files["pdf"]
    assert T.is_file_id(pdf["file_id"]) and pdf["role"] == "companion" and files["docx"]["role"] == "primary"
    assert pdf["download_url"] == f"/artifacts/{aid}/v/1/f/{pdf['file_id']}?disposition=attachment"
    assert pdf["title"] == "Quarterly Review"

    down = client.get(f"/artifacts/{aid}/v/1/f/{pdf['file_id']}")
    assert down.status_code == 200 and down.content == _PDF
    assert down.headers["content-type"].startswith("application/pdf")
    assert down.headers["content-disposition"].startswith('attachment; filename="quarterly-review-v1.pdf"')
    assert "filename*=UTF-8''" in down.headers["content-disposition"]
    assert down.headers["cache-control"] == "private, no-store"
    assert down.headers["etag"] == f'"{hashlib.sha256(_PDF).hexdigest()}"'
    assert down.headers["accept-ranges"] == "bytes"
    inline = client.get(f"/artifacts/{aid}/v/1/f/{pdf['file_id']}", params={"disposition": "inline"})
    assert inline.status_code == 200 and inline.headers["content-disposition"].startswith("inline;")
    assert client.get(f"/artifacts/{aid}/v/1/f/{pdf['file_id']}", params={"disposition": "open"}).status_code == 422
    head = client.head(f"/artifacts/{aid}/v/1/f/{pdf['file_id']}")
    assert head.status_code == 200 and head.content == b"" and int(head.headers["content-length"]) == len(_PDF)
    part = client.get(f"/artifacts/{aid}/v/1/f/{pdf['file_id']}", headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and part.content == _PDF[:10] and part.headers["content-range"] == f"bytes 0-9/{len(_PDF)}"
    docx = client.get(f"/artifacts/{aid}/v/1/f/{files['docx']['file_id']}")
    assert docx.status_code == 200 and docx.headers["content-type"].startswith(T.MIME_TYPES["docx"])
    assert docx.content == b"docx bytes" * 100
    # The by-format alias still answers the same bytes.
    assert client.get(f"/artifacts/{aid}/v/1/file/pdf").content == _PDF
    # Counted under the format label (the `result` label folds to "other":
    # metrics.py's result vocabulary predates inline/attachment).
    assert sum(v for k, v in metrics._counters["artifact_download_total"].items() if ("format", "pdf") in k) >= 2

    # A stranger: 404, the same as a missing one. An id this version does
    # not have: 404.
    bob = login_client("api-bob")
    assert bob.get(f"/artifacts/{aid}/v/1/f/{pdf['file_id']}").status_code == 404
    assert bob.head(f"/artifacts/{aid}/v/1/f/{pdf['file_id']}").status_code == 404
    assert client.get(f"/artifacts/{aid}/v/1/f/{'0' * 16}").status_code == 404
    assert client.get(f"/artifacts/{aid}/v/2/f/{pdf['file_id']}").status_code == 404


def test_a_malformed_file_id_is_404_before_any_lookup(alice, monkeypatch):
    client, uid = alice
    row = _make(uid)
    aid = row["artifact_id"]
    looked = {"n": 0}
    real = adb.get_version

    def counting(*args, **kw):
        looked["n"] += 1
        return real(*args, **kw)

    monkeypatch.setattr(adb, "get_version", counting)
    for bad in ("../../etc/passwd", "a" * 15, "a" * 17, "A" * 16, "g" * 16, "0123456789abcde%2e", "x"):
        assert client.get(f"/artifacts/{aid}/v/1/f/{bad}").status_code == 404, bad
        assert client.get(f"/artifacts/{aid}/v/1/grid", params={"file": bad}).status_code == 404, bad
    assert looked["n"] == 0, "no row was read for an id that is not an id"
    assert client.get(f"/artifacts/{'b' * 32}/v/1/f/{'a' * 16}").status_code == 404


def test_a_legacy_version_row_downloads_by_both_urls(alice):
    """A version persisted before file ids: the API synthesises the id
    (pipeline.ref_for) and /f/{id} resolves it; /file/{fmt} still works."""
    client, uid = alice
    row = _make(uid)
    aid = row["artifact_id"]
    with db.connection() as con:
        stripped = [{k: v for k, v in f.items() if k not in ("file_id", "role", "title", "rows", "columns")} for f in adb.get_version(aid, 1, uid)["files"]]
        con.execute("UPDATE artifact_versions SET files = %s WHERE artifact_id = %s AND version = %s", (db._json_param(stripped), aid, 1))
    files = _files_of(client, aid)
    assert files["pdf"]["file_id"] == T.file_id_for(aid, 1, "companion", "pdf") and files["pdf"]["role"] == "companion"
    assert files["docx"]["file_id"] == T.file_id_for(aid, 1, "primary", "docx") and files["docx"]["title"] == "Quarterly Review"
    assert client.get(f"/artifacts/{aid}/v/1/f/{files['pdf']['file_id']}").content == _PDF
    assert client.get(f"/artifacts/{aid}/v/1/file/pdf").content == _PDF
    bundle = client.get(f"/artifacts/{aid}/v/1/zip")
    assert bundle.status_code == 200 and sorted(zipfile.ZipFile(io.BytesIO(bundle.content)).namelist()) == ["quarterly-review-v1.docx", "quarterly-review-v1.pdf"]


def test_the_zip_route_streams_every_file_of_the_version(alice, login_client, monkeypatch):
    """CONTRACT-2 §2: one bundle, ZIP_STORED, entries named by filename,
    read through a 64 KiB chunker (the response never holds a file
    whole); 413 past MAX_ZIP_BYTES; 404 for a stranger and for a version
    with no file."""
    client, uid = alice
    row = _make(uid, kind="workbook", formats=("xlsx", "csv", "pdf"))
    aid = row["artifact_id"]
    version = client.get(f"/artifacts/{aid}/v/1").json()
    assert version["package"] == {"count": 3} and version["download_all_url"] == f"/artifacts/{aid}/v/1/zip"
    assert [f["role"] for f in version["files"]] == ["primary", "data", "companion"]

    from app.artifacts import api as api_mod

    reads = {"n": 0}
    real_open = open

    def chunk_watch(path, mode="r", *a, **kw):
        fh = real_open(path, mode, *a, **kw)
        if mode == "rb":
            real_read = fh.read

            def read(n=-1):
                reads["n"] += 1
                assert n == api_mod._ZIP_CHUNK, "the zip route reads in 64 KiB pieces, never the whole file"
                return real_read(n)

            fh.read = read
        return fh

    monkeypatch.setattr(api_mod, "open", chunk_watch, raising=False)
    resp = client.get(f"/artifacts/{aid}/v/1/zip")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert resp.headers["content-disposition"].startswith('attachment; filename="budget-v1.zip"')
    assert resp.headers["cache-control"] == "private, no-store"
    assert reads["n"] >= 3
    bundle = zipfile.ZipFile(io.BytesIO(resp.content))
    assert bundle.testzip() is None
    infos = {i.filename: i for i in bundle.infolist()}
    assert set(infos) == {"budget-v1.xlsx", "budget-v1.csv", "budget-v1.pdf"}
    assert all(i.compress_type == zipfile.ZIP_STORED for i in infos.values())
    assert infos["budget-v1.pdf"].file_size == len(_PDF) and bundle.read("budget-v1.pdf") == _PDF
    assert infos["budget-v1.csv"].file_size == len(b"csv bytes" * 100) and bundle.read("budget-v1.xlsx") == b"xlsx bytes" * 100
    assert sum(v for k, v in metrics._counters["artifact_download_total"].items() if ("format", "zip") in k) == 1.0

    monkeypatch.setattr(T, "MAX_ZIP_BYTES", 100)
    too_big = client.get(f"/artifacts/{aid}/v/1/zip")
    assert too_big.status_code == 413 and "one by one" in too_big.json()["detail"]
    monkeypatch.setattr(T, "MAX_ZIP_BYTES", 200 * 1024 * 1024)

    bob = login_client("api-bob")
    assert bob.get(f"/artifacts/{aid}/v/1/zip").status_code == 404
    # A version with no file yet (queued): 404, not an empty archive.
    queued = pipeline.accept(user_id=uid, conversation_id="conv-api", generation_id="g-queued", operation="create", instruction="Make it",
                             kind="document", formats=["pdf"], effort="fast", mode="assistant", template_id="generic", material={})
    assert client.get(f"/artifacts/{queued['artifact_id']}/v/1/zip").status_code == 404
    assert client.get(f"/artifacts/{aid}/v/9/zip").status_code == 404


def _real_grid_render():
    """A render whose xlsx and csv are REAL files (openpyxl, the csv
    module): the grid route reads them back."""
    async def render(work_dir, spec, formats, title_slug, version, effort):
        from openpyxl import Workbook

        files = []
        for fmt in formats:
            name = f"{title_slug}-v{version}.{fmt}"
            path = os.path.join(work_dir, name)
            if fmt == "xlsx":
                wb = Workbook()
                ws = wb.active
                ws.title = "Data"
                ws.append(["Item", "Cost"])
                for i in range(1, 6):
                    ws.append([f"item {i}", i * 10])
                ws.append(["Total", "=SUM(B2:B6)"])
                other = wb.create_sheet("Notes")
                other.append(["Note"])
                other.append(["hello"])
                wb.save(path)
                facts = {"sheets": 2, "rows": 6, "columns": 2}
            elif fmt == "csv":
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write("Item,Cost\r\n" + "".join(f"item {i},{i * 10}\r\n" for i in range(1, 6)))
                facts = {"rows": 5, "columns": 2, "sheet": "Data", "title": "Data"}
            else:
                with open(path, "wb") as fh:
                    fh.write(_PDF)
                facts = {"pages": 3}
            body = open(path, "rb").read()
            files.append({"format": fmt, "filename": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), **facts})
        with open(os.path.join(work_dir, T.PREVIEW_PDF_NAME), "wb") as fh:
            fh.write(_PDF)
        return {"files": files, "preview_pdf": T.PREVIEW_PDF_NAME, "preview_kind": "grid", "preview_pages": 0, "warnings": [], "validation": {}, "chart_files": [], "timings": {}}

    return render


def test_the_grid_route_pages_through_a_csv_and_an_xlsx_sheet(alice, login_client, monkeypatch):
    """CONTRACT-2 §11: /grid?file=<id>&sheet=&offset=&limit= for BOTH grid
    formats, one shape — sheets, sheet, columns, rows, total_rows,
    total_columns, truncated, formulas_as_text — formulas as text; bounds
    refused by validation; /sheets stays as the xlsx alias."""
    client, uid = alice
    monkeypatch.setattr(pipeline, "_render_in_subprocess", _real_grid_render())
    row = _make(uid, kind="workbook", formats=("xlsx", "csv"))
    aid = row["artifact_id"]
    files = _files_of(client, aid)
    csv_id, xlsx_id = files["csv"]["file_id"], files["xlsx"]["file_id"]
    assert files["csv"]["rows"] == 5 and files["csv"]["columns"] == 2 and files["csv"]["title"] == "Budget — Data"
    assert files["csv"]["preview_url"] == f"/artifacts/{aid}/v/1/grid?file={csv_id}"

    page = client.get(f"/artifacts/{aid}/v/1/grid", params={"file": csv_id, "offset": 1, "limit": 2})
    assert page.status_code == 200 and page.headers["cache-control"] == "private, no-store"
    grid = page.json()
    assert set(grid) >= {"sheets", "sheet", "columns", "rows", "total_rows", "total_columns", "truncated", "formulas_as_text"}
    assert grid["columns"] == ["Item", "Cost"] and grid["rows"] == [["item 2", "20"], ["item 3", "30"]]
    assert grid["total_rows"] == 5 and grid["total_columns"] == 2 and grid["truncated"] is True and grid["formulas_as_text"] is True
    assert len(grid["sheets"]) == 1 and grid["sheet"] == grid["sheets"][0]
    whole = client.get(f"/artifacts/{aid}/v/1/grid", params={"file": csv_id}).json()
    assert len(whole["rows"]) == 5 and whole["truncated"] is False

    page = client.get(f"/artifacts/{aid}/v/1/grid", params={"file": xlsx_id, "offset": 1, "limit": 2})
    assert page.status_code == 200
    grid = page.json()
    assert grid["sheets"] == ["Data", "Notes"] and grid["sheet"] == "Data"
    assert grid["columns"] == ["Item", "Cost"] and grid["rows"] == [["item 2", 20], ["item 3", 30]]
    assert grid["total_rows"] == 6 and grid["total_columns"] == 2 and grid["truncated"] is True and grid["formulas_as_text"] is True
    last = client.get(f"/artifacts/{aid}/v/1/grid", params={"file": xlsx_id, "offset": 5, "limit": 10}).json()
    assert last["rows"] == [["Total", "=SUM(B2:B6)"]] and last["truncated"] is False, "the formula is text, never a number"
    notes = client.get(f"/artifacts/{aid}/v/1/grid", params={"file": xlsx_id, "sheet": "Notes"}).json()
    assert notes["sheet"] == "Notes" and notes["rows"] == [["hello"]]
    assert client.get(f"/artifacts/{aid}/v/1/grid", params={"file": xlsx_id, "sheet": "Nope"}).status_code == 404
    # No file named: the workbook's xlsx.
    assert client.get(f"/artifacts/{aid}/v/1/grid").json()["sheet"] == "Data"
    # Bounds: refused, never clamped silently.
    assert client.get(f"/artifacts/{aid}/v/1/grid", params={"file": csv_id, "limit": 501}).status_code == 422
    assert client.get(f"/artifacts/{aid}/v/1/grid", params={"file": csv_id, "offset": -1}).status_code == 422
    assert client.get(f"/artifacts/{aid}/v/1/grid", params={"file": xlsx_id, "sheet": "s" * 32}).status_code == 422
    assert client.get(f"/artifacts/{aid}/v/1/grid", params={"file": "0" * 16}).status_code == 404
    # The xlsx alias still answers its own shape.
    alias = client.get(f"/artifacts/{aid}/v/1/sheets").json()
    assert alias["sheet"]["name"] == "Data" and alias["sheet"]["formulas"]
    bob = login_client("api-bob")
    assert bob.get(f"/artifacts/{aid}/v/1/grid", params={"file": csv_id}).status_code == 404
    # A document version has no grid.
    doc = _make(uid, conv="conv-api-3")
    pdf_id = _files_of(client, doc["artifact_id"])["pdf"]["file_id"]
    assert client.get(f"/artifacts/{doc['artifact_id']}/v/1/grid", params={"file": pdf_id}).status_code == 404
    assert client.get(f"/artifacts/{doc['artifact_id']}/v/1/grid").status_code == 404


def test_convert_a_workbook_to_csv(alice):
    client, uid = alice
    row = _make(uid, kind="workbook", formats=("xlsx",))
    aid = row["artifact_id"]
    assert client.post(f"/artifacts/{aid}/convert", json={"format": "pptx"}).status_code == 400
    assert client.post(f"/artifacts/{aid}/convert", json={"format": "xlsx"}).status_code == 409
    resp = client.post(f"/artifacts/{aid}/convert", json={"format": "csv"})
    assert resp.status_code == 200 and resp.json()["version"] == 2

    async def wait():
        pipeline.reset_for_tests()
        await pipeline.ensure_running(resp.json()["job_id"])
        return await pipeline.wait_for(resp.json()["job_id"])

    assert asyncio.run(wait())["status"] == "completed"
    files = _files_of(client, aid, 2)
    assert list(files) == ["csv"] and files["csv"]["role"] == "data" and files["csv"]["mime_type"] == "text/csv; charset=utf-8"
    # A document cannot become a csv: the refusal names what it can be.
    doc = _make(uid, conv="conv-api-4")
    refused = client.post(f"/artifacts/{doc['artifact_id']}/convert", json={"format": "csv"})
    assert refused.status_code == 400 and "docx or pdf" in refused.json()["detail"]


def test_job_status_cancel_retry_and_convert(alice, monkeypatch):
    client, uid = alice
    row = _make(uid, formats=("pdf",))
    aid, jid = row["artifact_id"], row["id"]
    status = client.get(f"/artifacts/jobs/{jid}").json()
    assert status["status"] == "completed" and status["artifact"]["artifact_id"] == aid and status["stage_title"]
    # Cancelling a finished job is idempotent and changes nothing.
    assert client.post(f"/artifacts/jobs/{jid}/cancel").status_code == 404 or client.get(f"/artifacts/jobs/{jid}").json()["status"] == "completed"
    assert adb.get_version(aid, 1, uid)["status"] == "completed"

    # Convert: a new version, no new artifact, the kind's formats only.
    resp = client.post(f"/artifacts/{aid}/convert", json={"format": "pptx"})
    assert resp.status_code == 400 and "can be made as" in resp.json()["detail"] and "pptx" not in resp.json()["detail"].split("as")[1]
    # Malformed bodies are 422s with nothing echoed, never a 500.
    assert client.post(f"/artifacts/{aid}/convert", content="{", headers={"content-type": "application/json"}).status_code == 422
    assert client.post(f"/artifacts/{aid}/convert", json=[1, 2]).status_code == 422
    assert client.post(f"/artifacts/{aid}/convert", json={"format": "<script>" * 100}).status_code == 422
    assert client.post(f"/artifacts/{aid}/convert", json={"format": "pdf"}).status_code == 409, "already has a PDF"
    resp = client.post(f"/artifacts/{aid}/convert", json={"format": "docx"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["artifact_id"] == aid and body["version"] == 2

    async def wait():
        # The request's loop is gone with the response (TestClient); the
        # queued job is picked up like a requeued one would be.
        pipeline.reset_for_tests()
        await pipeline.ensure_running(body["job_id"])
        return await pipeline.wait_for(body["job_id"])

    done = asyncio.run(wait())
    assert done["status"] == "completed"
    detail = client.get(f"/artifacts/{aid}").json()
    assert detail["artifact"]["current_version"] == 2 and [v["version"] for v in detail["versions"]] in ([1, 2], [2, 1])

    # Retry of a job that did not fail is refused; a failed one is re-queued.
    assert client.post(f"/artifacts/jobs/{jid}/retry").status_code == 409
    adb.set_job_status(jid, "failed", error="boom", failure_category="renderer_failure")
    resp = client.post(f"/artifacts/jobs/{jid}/retry")
    assert resp.status_code == 200 and resp.json()["status"] in ("queued", "running", "completed")


def test_the_feature_gate_answers_403_when_documents_are_off(alice, monkeypatch):
    client, uid = alice
    row = _make(uid)
    from app.authn import features as feature_access

    real_allowed = feature_access.allowed

    def denied(features, feature):
        if feature == feature_access.Feature.ARTIFACTS:
            return False
        return real_allowed(features, feature)

    monkeypatch.setattr(feature_access, "allowed", denied)
    resp = client.get(f"/artifacts/{row['artifact_id']}")
    assert resp.status_code == 403 and "turned off" in resp.json()["detail"]


def test_a_caller_without_a_session_sees_nothing():
    # The test harness resolves a cookie-less client to an ambient identity
    # (conftest.as_user); what is pinned here is that it sees NO artifact,
    # not the shape of the 401 the real cookie gate raises (tests/test_auth*).
    anon = TestClient(app)
    assert anon.get(f"/artifacts/{'c' * 32}").status_code in (401, 404)
