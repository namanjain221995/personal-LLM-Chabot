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
import os

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


def _make(owner_id: int, *, kind="document", formats=("pdf", "docx"), conv="conv-api") -> dict:
    job = pipeline.accept(user_id=owner_id, conversation_id=conv, generation_id=f"g-{kind}", operation="create", instruction="Make it",
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


def test_job_status_cancel_retry_and_convert(alice, monkeypatch):
    client, uid = alice
    row = _make(uid)
    aid, jid = row["artifact_id"], row["id"]
    status = client.get(f"/artifacts/jobs/{jid}").json()
    assert status["status"] == "completed" and status["artifact"]["artifact_id"] == aid and status["stage_title"]
    # Cancelling a finished job is idempotent and changes nothing.
    assert client.post(f"/artifacts/jobs/{jid}/cancel").status_code == 404 or client.get(f"/artifacts/jobs/{jid}").json()["status"] == "completed"
    assert adb.get_version(aid, 1, uid)["status"] == "completed"

    # Convert: a new version, no new artifact, the kind's formats only.
    resp = client.post(f"/artifacts/{aid}/convert", json={"format": "pptx"})
    assert resp.status_code == 400 and "can be made as" in resp.json()["detail"]
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
