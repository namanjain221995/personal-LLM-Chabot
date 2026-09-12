"""The artifact job system (V31): artifacts/db.py and artifacts/pipeline.py.

Offline and private: the composer is a stub installed through
`pipeline.set_composer`, the render subprocess is `pipeline.
_render_in_subprocess` replaced by a function that writes real bytes into
the working directory, and the rasteriser hooks return a PNG header. What
is REAL is the database (the V31 tables under the test DSN), the working
directory, the atomic publish, the lease, the heartbeat and the stage
resume — the parts a person's document depends on when the process dies.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time

import pytest

from app import db, metrics
from app.artifacts import db as adb
from app.artifacts import pipeline, store
from app.artifacts import spec as S
from app.artifacts import types as T
from app.config import settings
from app.resilience import ModelUnavailable

# ---------------------------------------------------------------- setup --


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path / "reports"))
    monkeypatch.setattr(settings, "artifact_lease_ttl_s", 60.0)
    monkeypatch.setattr(settings, "artifact_stage_timeout_s", 30.0)
    monkeypatch.setattr(settings, "artifact_render_timeout_s", 30.0)
    monkeypatch.setattr(settings, "artifact_min_free_mb", 1)
    monkeypatch.setattr(settings, "artifact_user_quota_mb", 1024)
    monkeypatch.setattr(settings, "video_pace_max_wait_s", 0.0)
    pipeline.reset_for_tests()
    pipeline.set_composer(None)
    pipeline.install_busy_probe(None)
    metrics.reset()
    yield
    pipeline.reset_for_tests()
    pipeline.set_composer(None)
    pipeline.install_busy_probe(None)
    metrics.reset()


@pytest.fixture()
def owner():
    return int(db.create_user("artifact-owner", "hash"))


@pytest.fixture()
def stranger():
    return int(db.create_user("artifact-stranger", "hash"))


def _spec(title: str = "Quarterly Review", kind: str = "document") -> S.ArtifactSpec:
    if kind == "presentation":
        return S.parse_body("presentation", {"title": title, "slides": [{"layout": "title", "title": title}]})
    if kind == "workbook":
        return S.parse_body("workbook", {"title": title, "sheets": [{"name": "Data", "columns": [{"name": "A"}], "rows": [["x"]]}]})
    return S.parse_body("document", {"title": title, "blocks": [{"type": "paragraph", "text": "Hello."}], "assumptions": ["figures are Q3"]})


def _composer(spec=None, *, calls=None, fail=None):
    async def compose(ctx):
        if calls is not None:
            calls.append(ctx.instruction)
        await ctx.progress_stage("intent", "running", "")
        await ctx.progress_stage("intent", "done", f"{ctx.kind} · {', '.join(ctx.formats)}")
        await ctx.progress_stage("gather", "done", "conversation")
        await ctx.progress_stage("outline", "skipped" if ctx.effort == "fast" else "done", "")
        await ctx.progress(50.0, "writing")
        if fail is not None:
            raise fail
        return spec if spec is not None else _spec(kind=ctx.kind)

    return compose


async def _fake_render(work_dir, spec, formats, title_slug, version, effort):
    files = []
    for fmt in formats:
        name = f"{title_slug}-v{version}.{fmt}"
        body = (f"{fmt} bytes for {spec.title}").encode() * 10
        with open(os.path.join(work_dir, name), "wb") as fh:
            fh.write(body)
        files.append({"format": fmt, "filename": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), "pages": 2 if fmt == "pdf" else None})
    with open(os.path.join(work_dir, T.PREVIEW_PDF_NAME), "wb") as fh:
        fh.write(b"%PDF-1.7 preview")
    return {"files": files, "preview_pdf": T.PREVIEW_PDF_NAME, "preview_kind": "pages", "preview_pages": 2,
            "warnings": [], "validation": {"reopened": True}, "chart_files": [], "timings": {f: 0.01 for f in formats}}


def _install_render(monkeypatch, render=_fake_render):
    monkeypatch.setattr(pipeline, "_render_in_subprocess", render)
    monkeypatch.setattr(pipeline, "_page_count", lambda pdf: 2)
    monkeypatch.setattr(pipeline, "_rasterise_page", lambda pdf, page, width: b"\x89PNG" + bytes([width % 256]))


def _accept(user_id, **kw):
    args = dict(
        user_id=user_id, conversation_id="conv-art", generation_id="gen-1", operation="create",
        instruction="Make me a quarterly review PDF", kind="document", formats=["pdf", "docx"],
        format_reason="explicit: pdf", effort="fast", mode="assistant", template_id="executive_report",
        material={"history_text": "Q3 went well."},
    )
    args.update(kw)
    return pipeline.accept(**args)


def _run(job_id: str) -> dict:
    async def scenario():
        assert await pipeline.ensure_running(job_id)
        return await pipeline.wait_for(job_id)

    return asyncio.run(scenario())


async def _retry_and_wait(job_id: str, user_id: int) -> dict:
    """retry() starts the task on the CURRENT loop; wait on the same loop."""
    row = await pipeline.retry(job_id, user_id)
    assert row is not None
    return await pipeline.wait_for(job_id)


def _lease(job_id: str):
    with db.connection() as con:
        row = con.execute("SELECT lease_owner, lease_expires_at FROM artifact_jobs WHERE id = %s", (job_id,)).fetchone()
    return row["lease_owner"], row["lease_expires_at"]


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


# -------------------------------------------------------------- schema --


def test_v31_tables_exist_and_cascade_from_users(owner):
    assert db.LATEST_SCHEMA_VERSION >= 31
    row = _accept(owner)
    assert T.is_artifact_id(row["id"]) and T.is_artifact_id(row["artifact_id"])
    assert row["status"] == "queued" and row["stage"] == "intent" and row["version"] == 1
    assert adb.get_version(row["artifact_id"], 1, owner)["status"] == "queued"
    with db.connection() as con:
        con.execute("DELETE FROM users WHERE id = %s", (owner,))
        left = con.execute("SELECT count(*) AS n FROM artifact_jobs").fetchone()["n"]
        versions = con.execute("SELECT count(*) AS n FROM artifact_versions").fetchone()["n"]
    assert left == 0 and versions == 0


def test_deleting_a_conversation_keeps_the_artifacts(owner):
    """report_files' reasoning, applied: the deliverable outlives the chat."""
    db.create_conversation(owner, "conv-art", "t")
    row = _accept(owner)
    assert db.delete_conversation(owner, "conv-art") is True
    assert adb.get_job(row["id"], owner) is not None
    assert adb.get_artifact(row["artifact_id"], owner) is not None


# ---------------------------------------------------------- acceptance --


def test_acceptance_is_idempotent_on_the_key(owner):
    first = _accept(owner)
    again = _accept(owner)
    assert again["id"] == first["id"] and again["artifact_id"] == first["artifact_id"]
    assert first["created"] is True and again["created"] is False
    with db.connection() as con:
        assert con.execute("SELECT count(*) AS n FROM artifacts").fetchone()["n"] == 1
    # A different generation is a different turn: a new artifact.
    other = _accept(owner, generation_id="gen-2")
    assert other["artifact_id"] != first["artifact_id"]
    # An explicit key wins over the derived one.
    explicit = _accept(owner, generation_id="gen-3", idempotency_key="k" * 64)
    assert _accept(owner, generation_id="gen-4", idempotency_key="k" * 64)["id"] == explicit["id"]


def test_acceptance_without_a_turn_identity_never_collapses_two_turns(owner):
    """CONTRACT §1: the key includes generation_id or intent_id. Two turns
    with neither used to share ONE job — the second person's 'make a pdf'
    silently returned the first's."""
    first = _accept(owner, generation_id="")
    second = _accept(owner, generation_id="")
    assert first["created"] is True and second["created"] is True
    assert first["id"] != second["id"] and first["artifact_id"] != second["artifact_id"]
    assert first["idempotency_key"] and first["idempotency_key"] != second["idempotency_key"]
    # An explicit key still dedupes with no generation_id at all.
    a = _accept(owner, generation_id="", idempotency_key="explicit-1")
    assert _accept(owner, generation_id="", idempotency_key="explicit-1")["id"] == a["id"]


def test_re_acceptance_of_an_existing_key_returns_the_job_even_over_quota(owner, monkeypatch):
    """'The same acceptance returns the same job' — the lookup precedes the
    refusal checks, so a retried POST after the job pushed the person over
    quota (or the volume filled) gets the job, not quota_exceeded."""
    row = _accept(owner)
    monkeypatch.setattr(settings, "artifact_user_quota_mb", 0)
    monkeypatch.setattr(settings, "artifact_min_free_mb", 10 ** 9)
    again = _accept(owner)
    assert again["id"] == row["id"] and again["created"] is False
    with pytest.raises(pipeline.ArtifactRefused) as exc:
        _accept(owner, generation_id="gen-new")
    assert exc.value.category == "storage_failure"


def test_an_explicit_key_another_person_holds_is_not_their_job(owner, stranger):
    mine = _accept(owner, idempotency_key="shared-key")
    with pytest.raises(pipeline.ArtifactRefused) as exc:
        _accept(stranger, idempotency_key="shared-key")
    assert exc.value.category == "permission_denied"
    assert adb.get_job(mine["id"], stranger) is None


def test_acceptance_writes_material_and_records_the_format_decision(owner):
    row = _accept(owner, requested_formats=["pdf", "docx", "pptx"], formats=["pdf", "docx"])
    assert row["requested_formats"] == ["pdf", "docx", "pptx"]
    assert row["selected_formats"] == ["pdf", "docx"] and row["format_reason"] == "explicit: pdf"
    assert row["kind"] == "document" and row["effort"] == "fast" and row["template_id"] == "executive_report"
    work = store.version_workdir(owner, row["artifact_id"], 1)
    material = store.read_json(os.path.join(work, store.MATERIAL_NAME))
    assert material["history_text"] == "Q3 went well." and material["sources"] == [] and material["salesforce"] == {}


def test_acceptance_refuses_a_format_the_kind_cannot_take(owner):
    # Re-pinned 2026-09-12 (CONTRACT-2 §1): a workbook may now carry pdf
    # (a tabular document from the same spec); a document can never be a
    # csv, so that is the genuinely impossible pair.
    with pytest.raises(pipeline.ArtifactRefused) as exc:
        _accept(owner, kind="document", formats=["csv"])
    assert exc.value.category == "invalid_request"
    row = _accept(owner, kind="presentation", formats=["pdf", "pptx", "xlsx"])
    assert row["selected_formats"] == ["pdf", "pptx"]
    wb = _accept(owner, generation_id="gen-wb", kind="workbook", formats=["xlsx", "csv", "docx", "pdf"])
    assert wb["selected_formats"] == ["xlsx", "csv", "docx", "pdf"]


def test_quota_refusal_happens_before_any_row(owner, monkeypatch):
    monkeypatch.setattr(settings, "artifact_user_quota_mb", 0)
    with pytest.raises(pipeline.ArtifactRefused) as exc:
        _accept(owner)
    assert exc.value.category == "quota_exceeded"
    with db.connection() as con:
        assert con.execute("SELECT count(*) AS n FROM artifact_jobs").fetchone()["n"] == 0


def test_disk_space_refusal_happens_before_any_row(owner, monkeypatch):
    monkeypatch.setattr(settings, "artifact_min_free_mb", 10 ** 9)
    with pytest.raises(pipeline.ArtifactRefused) as exc:
        _accept(owner)
    assert exc.value.category == "storage_failure"


def test_user_bytes_counts_only_published_versions(owner, monkeypatch):
    _install_render(monkeypatch)
    pipeline.set_composer(_composer())
    assert adb.user_bytes(owner) == 0
    row = _accept(owner)
    assert adb.user_bytes(owner) == 0, "queued is not published"
    fresh = _run(row["id"])
    assert fresh["status"] == "completed"
    version = adb.get_version(row["artifact_id"], 1, owner)
    assert adb.user_bytes(owner) == sum(f["size"] for f in version["files"]) > 0


# ------------------------------------------------------------ the run --


def test_a_job_runs_every_stage_publishes_atomically_and_fans_out_steps(owner, monkeypatch):
    _install_render(monkeypatch)
    pipeline.set_composer(_composer())
    row = _accept(owner)
    events = []

    async def scenario():
        q = pipeline.subscribe(row["id"])
        assert await pipeline.ensure_running(row["id"])
        while True:
            event = await asyncio.wait_for(q.get(), timeout=10)
            events.append(event)
            if event["stage"] == pipeline._DONE:
                break
        pipeline.unsubscribe(row["id"], q)
        return await pipeline.wait_for(row["id"])

    fresh = asyncio.run(scenario())
    assert fresh["status"] == "completed" and fresh["error"] == "" and fresh["attempt"] == 1
    assert fresh["completed_at"] and fresh["started_at"]
    stages_seen = [e["stage"] for e in events]
    for stage in ("intent", "gather", "outline", "compose", "render", "validate", "preview"):
        assert stage in stages_seen, f"{stage} was never announced"
    assert stages_seen.index("compose") < stages_seen.index("render") < stages_seen.index("validate") < stages_seen.index("preview")
    for e in events:
        assert set(e) >= {"stage", "status", "ts"}
        if e["stage"] != pipeline._DONE:
            assert set(e) >= {"percent", "detail", "elapsed_s"}
    assert events[-1]["status"] == "completed"
    # Published: the directory, the manifest, the files, the rows.
    final = store.version_dir(owner, row["artifact_id"], 1)
    assert store.is_published(owner, row["artifact_id"], 1)
    assert not os.path.exists(final + ".tmp")
    # CONTRACT §6: EXACTLY these — no material.json (chat content outside
    # every retention path), no render-job/render-report (absolute paths),
    # no preview.json, no .mpl cache.
    assert set(os.listdir(final)) == {"manifest.json", "spec.json", "validation.json", "preview.pdf", "previews",
                                      "quarterly-review-v1.pdf", "quarterly-review-v1.docx"}
    assert set(os.listdir(os.path.join(final, "previews"))) == {"1-240.png", "1-1400.png"}
    version = adb.get_version(row["artifact_id"], 1, owner)
    assert version["status"] == "completed" and version["preview_kind"] == "pages" and version["preview_pages"] == 2
    assert [f["format"] for f in version["files"]] == ["pdf", "docx"]
    assert all(len(f["sha256"]) == 64 and f["size"] > 0 for f in version["files"])
    assert version["files"][0]["pages"] == 2 and version["files"][0]["mime_type"] == T.MIME_TYPES["pdf"]
    assert version["assumptions"] == ["figures are Q3"] and version["template_id"] == "generic"
    assert version["template_version"] == T.TEMPLATE_VERSION and version["renderer_version"] == T.RENDERER_VERSION
    art = adb.get_artifact(row["artifact_id"], owner)
    assert art["current_version"] == 1 and art["title"] == "Quarterly Review"
    # The stage map on the row says where a resume would start.
    stamped = fresh["progress"]["stages"]
    assert all(stamped[s]["status"] == "done" for s in ("compose", "render", "validate", "preview"))
    assert stamped["outline"]["status"] == "skipped"
    # The ArtifactRef the card is built from. Since CONTRACT-2 §2 a file
    # is addressed by the id the pipeline minted for it, not by format.
    ref = pipeline.ref_for(fresh, version).to_json()
    assert ref["status"] == "completed"
    first = ref["files"][0]
    assert first["download_url"] == f"/artifacts/{row['artifact_id']}/v/1/f/{first['file_id']}?disposition=attachment"
    assert first["inline_url"].endswith(f"/f/{first['file_id']}?disposition=inline") and first["preview_url"].endswith("/v/1/preview")
    assert ref["download_all_url"] == f"/artifacts/{row['artifact_id']}/v/1/zip" and ref["package"] == {"count": 2}
    assert ref["thumbnail_url"] == f"/artifacts/{row['artifact_id']}/v/1/preview/1.png?w=240"
    assert ref["status_url"] == f"/artifacts/jobs/{row['id']}"
    # Metrics.
    assert metrics._counters["artifact_jobs_total"] == {(("result", "ok"),): 1.0}
    assert (("stage", "compose"),) in metrics._hists["artifact_stage_seconds"]
    assert (("format", "pdf"),) in metrics._hists["artifact_render_seconds"]
    # The manifest agrees with the row.
    manifest = store.read_manifest(owner, row["artifact_id"], 1)
    assert manifest["sha256s"]["quarterly-review-v1.pdf"] == version["files"][0]["sha256"]
    assert manifest["preview"] == {"preview_kind": "pages", "preview_pages": 2, "thumbnails": ["1-240.png", "1-1400.png"]}
    assert manifest["warnings"] == []
    # Listing carries the current version.
    listed = adb.list_artifacts(owner, "conv-art")
    assert len(listed) == 1 and listed[0]["current"]["version"] == 1
    assert adb.list_artifacts(owner, "other-conv") == []


def test_render_warnings_make_the_version_completed_with_warnings(owner, monkeypatch):
    async def warn_render(*args, **kw):
        report = await _fake_render(*args, **kw)
        report["warnings"] = ["bullet list cut to 40 items"]
        return report

    _install_render(monkeypatch, warn_render)
    pipeline.set_composer(_composer())
    row = _accept(owner)
    fresh = _run(row["id"])
    assert fresh["status"] == "completed_with_warnings"
    version = adb.get_version(row["artifact_id"], 1, owner)
    assert version["warnings"] == ["bullet list cut to 40 items"] and version["status"] == "completed_with_warnings"


def test_a_failure_before_publish_leaves_no_version_directory(owner, monkeypatch):
    async def broken_render(work_dir, spec, formats, title_slug, version, effort):
        raise pipeline.RenderFailed("renderer_failure", "The PDF engine refused the page size.")

    _install_render(monkeypatch, broken_render)
    pipeline.set_composer(_composer())
    row = _accept(owner)
    fresh = _run(row["id"])
    assert fresh["status"] == "failed" and fresh["failure_category"] == "renderer_failure"
    assert fresh["error"] == "The PDF engine refused the page size."
    assert fresh["diagnostic_ref"] and len(fresh["diagnostic_ref"]) == 12
    assert not os.path.exists(store.version_dir(owner, row["artifact_id"], 1))
    assert os.path.isdir(store.version_workdir(owner, row["artifact_id"], 1)), "kept for the retry"
    assert adb.get_version(row["artifact_id"], 1, owner)["status"] == "failed"
    assert adb.get_artifact(row["artifact_id"], owner)["current_version"] == 0
    assert metrics._counters["artifact_jobs_total"] == {(("result", "fail"),): 1.0}


def test_an_uncategorised_exception_never_reaches_the_row(owner, monkeypatch):
    async def exploding_render(work_dir, spec, formats, title_slug, version, effort):
        raise RuntimeError("psycopg connection to postgresql://user:pw@db:5432 lost at /reports/x")

    _install_render(monkeypatch, exploding_render)
    pipeline.set_composer(_composer())
    row = _accept(owner)
    fresh = _run(row["id"])
    assert fresh["status"] == "failed" and fresh["failure_category"] == "renderer_failure"
    assert "postgresql" not in fresh["error"] and "/reports" not in fresh["error"] and "RuntimeError" not in fresh["error"]


def test_validate_refuses_a_missing_or_empty_file(owner, monkeypatch):
    async def lying_render(work_dir, spec, formats, title_slug, version, effort):
        report = await _fake_render(work_dir, spec, formats, title_slug, version, effort)
        os.truncate(os.path.join(work_dir, f"{title_slug}-v{version}.docx"), 0)
        return report

    _install_render(monkeypatch, lying_render)
    pipeline.set_composer(_composer())
    row = _accept(owner)
    fresh = _run(row["id"])
    assert fresh["status"] == "failed" and fresh["failure_category"] == "validation_failure"
    assert "docx" in fresh["error"]
    assert not store.is_published(owner, row["artifact_id"], 1)


def test_validate_refuses_a_symlink_out_of_the_workdir_a_missing_file_and_one_over_the_ceiling(owner, monkeypatch, tmp_path):
    """Three distinct refusals, each asserted by its own problem text — a
    basename'd absolute path degrades to 'missing', so the containment
    guard is exercised the way test_artifact_store exercises the resolver:
    a correctly named symlink inside the workdir that points outside."""
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF outside")

    async def symlinked_render(work_dir, spec, formats, title_slug, version, effort):
        report = await _fake_render(work_dir, spec, formats, title_slug, version, effort)
        name = f"{title_slug}-v{version}.pdf"
        os.unlink(os.path.join(work_dir, name))
        os.symlink(str(outside), os.path.join(work_dir, name))
        return report

    _install_render(monkeypatch, symlinked_render)
    pipeline.set_composer(_composer())
    row = _accept(owner, formats=["pdf"])
    fresh = _run(row["id"])
    assert fresh["status"] == "failed" and fresh["failure_category"] == "validation_failure"
    assert "resolves outside the working directory" in fresh["error"]

    async def absent_render(work_dir, spec, formats, title_slug, version, effort):
        report = await _fake_render(work_dir, spec, formats, title_slug, version, effort)
        os.unlink(os.path.join(work_dir, f"{title_slug}-v{version}.pdf"))
        return report

    _install_render(monkeypatch, absent_render)
    row2 = _accept(owner, generation_id="gen-missing", formats=["pdf"])
    fresh2 = _run(row2["id"])
    assert fresh2["status"] == "failed" and "the pdf file is missing" in fresh2["error"]

    monkeypatch.setattr(T, "MAX_FILE_BYTES", 10)
    row3 = _accept(owner, generation_id="gen-big", formats=["pdf"])
    _install_render(monkeypatch)
    fresh3 = _run(row3["id"])
    assert fresh3["status"] == "failed" and "ceiling" in fresh3["error"]


def test_validate_refuses_a_file_not_named_by_the_contract(owner, monkeypatch):
    """A report naming 'spec.json.bak' for pdf used to publish a COMPLETED
    version whose download URL the resolver then refused (404)."""
    async def misnamed_render(work_dir, spec, formats, title_slug, version, effort):
        report = await _fake_render(work_dir, spec, formats, title_slug, version, effort)
        with open(os.path.join(work_dir, "spec.json.bak"), "wb") as fh:
            fh.write(b"%PDF not really")
        report["files"][0]["filename"] = "spec.json.bak"
        report["files"][0].pop("sha256", None)
        return report

    _install_render(monkeypatch, misnamed_render)
    pipeline.set_composer(_composer())
    row = _accept(owner, formats=["pdf"])
    fresh = _run(row["id"])
    assert fresh["status"] == "failed" and fresh["failure_category"] == "validation_failure"
    assert "not named quarterly-review-v1.pdf" in fresh["error"]
    assert not store.is_published(owner, row["artifact_id"], 1)


def test_a_composer_that_returns_junk_is_a_model_failure(owner, monkeypatch):
    _install_render(monkeypatch)

    async def junk(ctx):
        return {"title": "not a spec"}

    pipeline.set_composer(junk)
    row = _accept(owner)
    fresh = _run(row["id"])
    assert fresh["status"] == "failed" and fresh["failure_category"] == "model_failure"


def test_a_composer_may_name_its_category(owner, monkeypatch):
    _install_render(monkeypatch)
    pipeline.set_composer(_composer(fail=pipeline.StageFailure("source_unavailable", "Salesforce did not answer.")))
    row = _accept(owner)
    fresh = _run(row["id"])
    assert fresh["status"] == "failed" and fresh["failure_category"] == "source_unavailable"
    assert fresh["error"] == "Salesforce did not answer."


def test_no_composer_installed_is_dependency_unavailable(owner, monkeypatch):
    _install_render(monkeypatch)
    row = _accept(owner)
    fresh = _run(row["id"])
    assert fresh["status"] == "failed" and fresh["failure_category"] == "dependency_unavailable"


# ------------------------------------------------------------- resume --


def test_a_requeued_job_resumes_at_the_first_stage_without_an_output(owner, monkeypatch):
    calls: list = []
    pipeline.set_composer(_composer(calls=calls))
    attempts = {"render": 0}

    async def flaky_render(*args, **kw):
        attempts["render"] += 1
        if attempts["render"] == 1:
            raise pipeline.RenderFailed("renderer_failure", "first try fails")
        return await _fake_render(*args, **kw)

    _install_render(monkeypatch, flaky_render)
    row = _accept(owner)
    first = _run(row["id"])
    assert first["status"] == "failed" and calls == ["Make me a quarterly review PDF"]
    assert first["progress"]["stages"]["compose"]["status"] == "done"
    assert first["progress"]["stages"]["render"]["status"] == "failed"

    fresh = asyncio.run(_retry_and_wait(row["id"], owner))
    assert fresh["status"] == "completed" and fresh["attempt"] == 2 and fresh["version"] == 1
    assert calls == ["Make me a quarterly review PDF"], "compose was served from spec.json, not re-asked"
    assert attempts["render"] == 2
    assert store.is_published(owner, row["artifact_id"], 1)
    with db.connection() as con:
        assert con.execute("SELECT count(*) AS n FROM artifact_versions").fetchone()["n"] == 1


def test_a_done_stamp_without_its_file_re_runs_the_stage(owner, monkeypatch):
    calls: list = []
    pipeline.set_composer(_composer(calls=calls))
    _install_render(monkeypatch)
    row = _accept(owner)
    work = store.ensure_workdir(owner, row["artifact_id"], 1)
    adb.set_job_progress(row["id"], {"stages": {"compose": {"status": "done", "ms": 1, "detail": "old"}}})
    assert not os.path.exists(os.path.join(work, T.SPEC_NAME))
    fresh = _run(row["id"])
    assert fresh["status"] == "completed" and calls == ["Make me a quarterly review PDF"]


def test_retry_is_owner_scoped_and_only_for_failed_jobs(owner, stranger, monkeypatch):
    _install_render(monkeypatch)
    pipeline.set_composer(_composer())
    row = _accept(owner)
    assert asyncio.run(pipeline.retry(row["id"], stranger)) is None
    fresh = _run(row["id"])
    assert fresh["status"] == "completed"
    same = asyncio.run(pipeline.retry(row["id"], owner))
    assert same["status"] == "completed", "a completed job is not re-run"


async def _gated_render(gate: "asyncio.Event", *args, **kw):
    """A render that waits at `gate` — the run is inside its render stage,
    the single slot taken, until the test lets it go."""
    await gate.wait()
    return await _fake_render(*args, **kw)


def _fail_at_render(monkeypatch, *, on: dict) -> None:
    async def render(*args, **kw):
        if on["fail"]:
            raise pipeline.RenderFailed("renderer_failure", "boom")
        return await _fake_render(*args, **kw)

    _install_render(monkeypatch, render)


def test_retry_enforces_the_open_jobs_ceiling_and_the_quota_like_accept(owner, monkeypatch):
    """Security review 2026-09-12 (#1): retry() flipped a failed row back to
    'queued' with none of accept()'s refusals. A model outage fails every
    job as dependency_unavailable after three deferrals; anyone could then
    requeue all of theirs at once past ARTIFACT_MAX_OPEN_JOBS_PER_USER and
    hold the one render slot and the shared engine against everyone, and an
    over-quota person kept publishing bytes through retries."""
    knob = {"fail": True}
    _fail_at_render(monkeypatch, on=knob)
    pipeline.set_composer(_composer())
    monkeypatch.setattr(settings, "artifact_max_open_jobs_per_user", 1)
    failed = []
    for i in range(3):
        row = _accept(owner, generation_id=f"gen-{i}")
        assert _run(row["id"])["status"] == "failed"
        failed.append(row["id"])
    assert adb.count_open_jobs(owner) == 0
    # The retried job stays queued (the runner is held off) so it counts as open.
    monkeypatch.setattr(pipeline, "ensure_running", lambda jid: asyncio.sleep(0))
    first = asyncio.run(pipeline.retry(failed[0], owner))
    assert first["status"] == "queued" and adb.count_open_jobs(owner) == 1
    with pytest.raises(pipeline.ArtifactRefused) as exc:
        asyncio.run(pipeline.retry(failed[1], owner))
    assert exc.value.category == "quota_exceeded" and "1 document(s) being built" in exc.value.message
    assert adb.load_job(failed[1])["status"] == "failed", "a refused retry leaves the row as it was"
    assert adb.count_open_jobs(owner) == 1
    # accept() says the same thing for the same person at the same moment.
    with pytest.raises(pipeline.ArtifactRefused) as same:
        _accept(owner, generation_id="gen-9")
    assert same.value.message == exc.value.message

    # The storage quota and the volume's free space refuse a retry exactly as they refuse an acceptance.
    asyncio.run(pipeline.cancel(failed[0], owner))
    assert adb.count_open_jobs(owner) == 0
    monkeypatch.setattr(settings, "artifact_user_quota_mb", 0)
    with pytest.raises(pipeline.ArtifactRefused) as over:
        asyncio.run(pipeline.retry(failed[1], owner))
    assert over.value.category == "quota_exceeded" and "storage is full" in over.value.message
    monkeypatch.setattr(settings, "artifact_user_quota_mb", 1024)
    monkeypatch.setattr(settings, "artifact_min_free_mb", 10 ** 9)
    with pytest.raises(pipeline.ArtifactRefused) as disk:
        asyncio.run(pipeline.retry(failed[1], owner))
    assert disk.value.category == "storage_failure"
    monkeypatch.setattr(settings, "artifact_min_free_mb", 1)
    assert adb.load_job(failed[1])["status"] == "failed" and adb.count_open_jobs(owner) == 0
    # Owner scoping and the not-failed answer are unchanged: a stranger's
    # retry is None before any check, a finished job is handed back as is.
    stranger = int(db.create_user("artifact-quota-stranger", "hash"))
    monkeypatch.setattr(settings, "artifact_user_quota_mb", 0)
    assert asyncio.run(pipeline.retry(failed[1], stranger)) is None
    monkeypatch.setattr(settings, "artifact_user_quota_mb", 1024)


def test_a_retry_whose_material_was_swept_is_refused_with_a_sentence(owner, monkeypatch):
    """Security review 2026-09-12 (#5): the 24 h sweep removes a FAILED
    job's v<N>.tmp — material.json (the conversation, the uploads, the
    pasted table) with it. A retry then composed from empty material and
    handed over a document written from the instruction alone with no
    word about it. The retry is refused with a sentence instead, and the
    row is left failed for the card to show."""
    seen: list = []

    async def compose(ctx):
        seen.append(dict(ctx.material))
        return _spec()

    knob = {"fail": True}
    _fail_at_render(monkeypatch, on=knob)
    pipeline.set_composer(compose)
    material = {"history_text": "the conversation", "uploads_text": "the uploaded csv", "tables": [{"id": "paste1", "columns": ["a"], "rows": [["1"]]}]}
    row = _accept(owner, material=material)
    first = _run(row["id"])
    assert first["status"] == "failed" and seen[0]["history_text"] == "the conversation"
    knob["fail"] = False
    # 24 h later: the real sweep, with the working directory aged past the TTL.
    work = store.version_workdir(owner, row["artifact_id"], 1)
    ancient = time.time() - 3 * 24 * 3600
    os.utime(work, (ancient, ancient))
    monkeypatch.setattr(settings, "artifact_tmp_ttl_hours", 24)
    assert asyncio.run(pipeline.sweep()) == 1 and not os.path.exists(work)
    with pytest.raises(pipeline.ArtifactRefused) as exc:
        asyncio.run(pipeline.retry(row["id"], owner))
    assert exc.value.message == "This request can no longer be retried — please ask for it again."
    assert exc.value.category == "source_unavailable"
    fresh = adb.load_job(row["id"])
    assert fresh["status"] == "failed" and fresh["error"] == first["error"] == "boom" and fresh["attempt"] == 1
    assert len(seen) == 1, "nothing was composed from nothing"
    assert adb.count_open_jobs(owner) == 0

    # A job whose compose stage is cached needs no material: spec.json and
    # its stamp are what the next attempt composes from (the resume rule).
    cached = _accept(owner, generation_id="gen-cached", material=material)
    knob["fail"] = True
    assert _run(cached["id"])["status"] == "failed"
    knob["fail"] = False
    os.unlink(os.path.join(store.version_workdir(owner, cached["artifact_id"], 1), store.MATERIAL_NAME))
    done = asyncio.run(_retry_and_wait(cached["id"], owner))
    assert done["status"] == "completed" and len(seen) == 2, "compose came from spec.json, not from empty material"

    # A convert re-renders its parent's spec and never reads material:
    # its retry is allowed with no working directory at all.
    convert = _accept(owner, generation_id="gen-convert", operation="convert", formats=["pdf"], parent=(cached["artifact_id"], 1), material=None)
    knob["fail"] = True
    assert _run(convert["id"])["status"] == "failed"
    knob["fail"] = False
    store.remove_workdir(store.version_workdir(owner, cached["artifact_id"], 2))
    assert asyncio.run(_retry_and_wait(convert["id"], owner))["status"] == "completed"


def test_a_version_published_but_unrecorded_is_completed_without_rendering_again(owner, monkeypatch):
    """The crash window between the rename and the row update."""
    renders = {"n": 0}

    async def counting(*args, **kw):
        renders["n"] += 1
        return await _fake_render(*args, **kw)

    _install_render(monkeypatch, counting)
    pipeline.set_composer(_composer())
    row = _accept(owner)
    original_publish = adb.publish_version
    monkeypatch.setattr(adb, "publish_version", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db went away")))
    first = _run(row["id"])
    assert first["status"] == "failed" and store.is_published(owner, row["artifact_id"], 1)
    monkeypatch.setattr(adb, "publish_version", original_publish)
    fresh = asyncio.run(_retry_and_wait(row["id"], owner))
    assert fresh["status"] == "completed" and renders["n"] == 1
    assert adb.get_artifact(row["artifact_id"], owner)["current_version"] == 1


# -------------------------------------------------------------- lease --


def test_a_second_runner_leaves_a_leased_run_to_its_owner(owner, monkeypatch):
    _install_render(monkeypatch)
    calls: list = []
    pipeline.set_composer(_composer(calls=calls))
    row = _accept(owner)

    assert adb.claim_lease(row["id"], "other-host:4242:deadbeef", 300.0)
    asyncio.run(pipeline._run(row["id"]))
    assert calls == []
    assert adb.load_job(row["id"])["status"] == "queued"
    assert _lease(row["id"])[0] == "other-host:4242:deadbeef"
    assert "artifact_lease_steal_total" not in metrics._counters

    # That process died: its lease lapses, this one takes over and counts it.
    assert adb.claim_lease(row["id"], "other-host:4242:deadbeef", -1.0)
    asyncio.run(pipeline._run(row["id"]))
    assert calls == ["Make me a quarterly review PDF"]
    assert adb.load_job(row["id"])["status"] == "completed"
    assert _lease(row["id"]) == ("", None)
    assert metrics._counters["artifact_lease_steal_total"] == {(): 1.0}


def test_the_run_heartbeats_and_startup_requeue_respects_a_live_lease(owner, monkeypatch):
    _install_render(monkeypatch)
    monkeypatch.setattr(settings, "artifact_lease_ttl_s", 3.0)  # beat every 1 s
    seen: dict = {}

    async def slow_compose(ctx):
        seen["at_compose"] = _lease(ctx.job_id)
        assert adb.requeue_lapsed() == 0, "a heartbeating run is not interrupted"
        await asyncio.sleep(1.3)
        seen["after"] = _lease(ctx.job_id)
        return _spec()

    pipeline.set_composer(slow_compose)
    row = _accept(owner)
    fresh = _run(row["id"])
    assert fresh["status"] == "completed"
    assert seen["at_compose"][0] == pipeline._OWNER and seen["after"][0] == pipeline._OWNER
    assert seen["after"][1] > seen["at_compose"][1], "the heartbeat moved the lease forward"
    assert fresh["heartbeat_at"] is not None
    assert _lease(row["id"]) == ("", None)


def test_the_heartbeat_stands_down_when_the_lease_is_lost(owner, monkeypatch):
    _install_render(monkeypatch)
    monkeypatch.setattr(settings, "artifact_lease_ttl_s", 1.5)  # beat every 0.5 s
    started = {}

    async def slow_compose(ctx):
        started["yes"] = True
        await asyncio.sleep(30)
        return _spec()

    pipeline.set_composer(slow_compose)
    row = _accept(owner)

    async def scenario():
        assert await pipeline.ensure_running(row["id"])
        await _wait_until(lambda: "yes" in started)
        # Another process steals the row (our lease is forced to lapse).
        with db.connection() as con:
            con.execute("UPDATE artifact_jobs SET lease_expires_at = now() - interval '1 minute' WHERE id = %s", (row["id"],))
        assert adb.claim_lease(row["id"], "thief:1:00000000", 300.0)
        await _wait_until(lambda: not pipeline.is_running(row["id"]), timeout=5.0)

    asyncio.run(scenario())
    assert _lease(row["id"])[0] == "thief:1:00000000", "the thief's lease was not released by the loser"
    assert adb.load_job(row["id"])["status"] == "running", "the row is the other owner's now"


def test_stop_cancels_runs_releases_leases_and_start_requeues_them(owner, monkeypatch):
    _install_render(monkeypatch)
    started = {}

    async def slow_compose(ctx):
        started["yes"] = True
        await asyncio.sleep(30)
        return _spec()

    pipeline.set_composer(slow_compose)
    row = _accept(owner)

    async def scenario():
        assert await pipeline.ensure_running(row["id"])
        await _wait_until(lambda: "yes" in started)
        assert _lease(row["id"])[0] == pipeline._OWNER
        await pipeline.stop()

    asyncio.run(scenario())
    assert _lease(row["id"]) == ("", None), "stop() waits for the release before the pool closes"
    assert adb.load_job(row["id"])["status"] == "running"
    assert adb.requeue_lapsed() == 1
    assert adb.load_job(row["id"])["status"] == "queued"

    # start() requeues + drains: the job runs to completion behind the app.
    async def restart():
        pipeline.reset_for_tests()
        pipeline.set_composer(_composer())
        adb.update_job(row["id"], status="running")
        with db.connection() as con:
            con.execute("UPDATE artifact_jobs SET lease_owner = 'dead:1:0', lease_expires_at = now() - interval '1 hour' WHERE id = %s", (row["id"],))
        await pipeline.start()
        await _wait_until(lambda: adb.load_job(row["id"])["status"] == "queued", timeout=2.0)
        assert await pipeline.drain_queue() == 1
        fresh = await pipeline.wait_for(row["id"])
        await pipeline.stop()
        return fresh

    fresh = asyncio.run(restart())
    assert fresh["status"] == "completed"


def test_a_lapsed_lease_is_requeued_within_seconds_not_at_the_next_sweep(owner, monkeypatch):
    """The restart drill of 2026-09-12: the orchestrator restarted 7 s into
    a compose, the startup pass ran before the dead lease had expired, and
    the job sat 'running' for the 15 minutes the test waited because the
    only later pass was the 30-minute sweep. The lease check now has its
    own cadence; the sweep keeps its interval and is NOT run each time."""
    _install_render(monkeypatch)
    pipeline.set_composer(_composer())
    row = _accept(owner)
    adb.update_job(row["id"], status="running")
    with db.connection() as con:
        con.execute("UPDATE artifact_jobs SET lease_owner = 'dead:1:0', lease_expires_at = now() - interval '1 minute' WHERE id = %s", (row["id"],))
    monkeypatch.setattr(pipeline, "REQUEUE_INTERVAL_S", 0.05)
    monkeypatch.setattr(settings, "artifact_maintenance_interval_s", 1800.0)
    sweeps = []

    async def counting_sweep():
        sweeps.append(1)
        return 0

    monkeypatch.setattr(pipeline, "sweep", counting_sweep)

    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(pipeline.asyncio, "sleep", lambda s: real_sleep(min(s, 0.05)))
        task = asyncio.create_task(pipeline._maintenance_loop())
        try:
            await _wait_until(lambda: adb.load_job(row["id"])["status"] in ("completed", "completed_with_warnings", "failed"), timeout=10.0)
            fresh = adb.load_job(row["id"])
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await pipeline.stop()
        return fresh

    fresh = asyncio.run(scenario())
    assert fresh["status"] == "completed", fresh
    assert len(sweeps) == 1, "the first pass sweeps once (its clock starts at zero); the requeue passes after it do not"


def test_requeue_lapsed_leaves_a_run_this_process_is_still_heartbeating(owner, monkeypatch):
    """Security review 2026-09-12 (#2): a heartbeat late by more than the
    TTL (a saturated pool, a stop-the-world pause) left the live run's
    lease lapsed; the 30 s maintenance pass then flipped the row to
    'queued', the next beat re-claimed it, the run went on, publish_version
    found no 'running' row and the person was told the file was CANCELLED
    with the version on disk and nothing pointing at it for up to 30 min.
    The pass now leaves a row this process holds a live task for alone."""
    gate = asyncio.Event()
    _install_render(monkeypatch, lambda *a, **kw: _gated_render(gate, *a, **kw))
    pipeline.set_composer(_composer())
    monkeypatch.setattr(pipeline, "REQUEUE_INTERVAL_S", 0.05)
    monkeypatch.setattr(settings, "artifact_maintenance_interval_s", 1800.0)
    monkeypatch.setattr(pipeline, "sweep", _no_sweep)
    row = _accept(owner)
    events: list = []

    async def scenario():
        q = pipeline.subscribe(row["id"])
        assert await pipeline.ensure_running(row["id"])
        await _wait_until(lambda: adb.load_job(row["id"])["stage"] == "render")
        with db.connection() as con:
            con.execute("UPDATE artifact_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (row["id"],))
        real_sleep = asyncio.sleep
        monkeypatch.setattr(pipeline.asyncio, "sleep", lambda s: real_sleep(min(s, 0.05)))
        loop_task = asyncio.create_task(pipeline._maintenance_loop())
        try:
            await real_sleep(0.5)  # several passes
            held = adb.load_job(row["id"])
            assert held["status"] == "running" and held["lease_owner"] == pipeline._OWNER, held
            gate.set()
            fresh = await pipeline.wait_for(row["id"])
        finally:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
        while not q.empty():
            events.append(q.get_nowait())
        pipeline.unsubscribe(row["id"], q)
        return fresh

    fresh = asyncio.run(scenario())
    assert fresh["status"] == "completed" and fresh["attempt"] == 1, fresh
    assert [e["status"] for e in events if e["stage"] == "_done"] == ["completed"]
    assert adb.get_artifact(row["artifact_id"], owner)["current_version"] == 1
    assert store.is_published(owner, row["artifact_id"], 1)


def test_a_run_whose_row_a_peer_requeued_stands_down_and_the_retry_runs(owner, monkeypatch):
    """The other half of #2: a SECOND orchestrator's pass does flip the row
    (it holds no task for it). The heartbeat that finds its row 'queued'
    after a renewal stands down — no terminal event, never 'cancelled' —
    releases the lease, and the drain runs the attempt again from the
    stages already on disk."""
    gates = [asyncio.Event(), asyncio.Event()]
    renders = {"n": 0}

    async def render(*args, **kw):
        renders["n"] += 1
        await gates[min(renders["n"], 2) - 1].wait()
        return await _fake_render(*args, **kw)

    _install_render(monkeypatch, render)
    monkeypatch.setattr(settings, "artifact_lease_ttl_s", 1.5)  # beat every 0.5 s
    calls: list = []
    pipeline.set_composer(_composer(calls=calls))
    row = _accept(owner)
    events: list = []

    async def scenario():
        q = pipeline.subscribe(row["id"])
        assert await pipeline.ensure_running(row["id"])
        first_task = pipeline._tasks[row["id"]]
        await _wait_until(lambda: adb.load_job(row["id"])["stage"] == "render")
        with db.connection() as con:
            con.execute("UPDATE artifact_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (row["id"],))
        assert adb.requeue_lapsed() == 1, "the peer holds no task for it: it requeues the row"
        # The heartbeat notices within TTL/3, stands down, and the drain it
        # scheduled starts the next attempt from the stages on disk.
        await _wait_until(lambda: renders["n"] == 2, timeout=5.0)
        assert first_task.done() and first_task.cancelled(), "the first run stood down"
        assert not gates[0].is_set(), "its render never finished"
        handed_over = adb.load_job(row["id"])
        assert handed_over["status"] == "running" and handed_over["attempt"] == 2 and handed_over["lease_owner"] == pipeline._OWNER, handed_over
        assert [e for e in events if e.get("stage") == "_done"] == []
        gates[1].set()
        fresh = await pipeline.wait_for(row["id"])
        while not q.empty():
            events.append(q.get_nowait())
        pipeline.unsubscribe(row["id"], q)
        return fresh

    fresh = asyncio.run(scenario())
    assert fresh["status"] == "completed" and fresh["attempt"] == 2, fresh
    assert [e["status"] for e in events if e["stage"] == "_done"] == ["completed"], "no bogus 'cancelled' reached the card"
    assert calls == ["Make me a quarterly review PDF"] and renders["n"] == 2, "compose was cached; only the render ran again"
    assert adb.get_artifact(row["artifact_id"], owner)["current_version"] == 1 and _lease(row["id"]) == ("", None)
    assert metrics._counters["artifact_jobs_total"] == {(("result", "ok"),): 1.0}
    assert metrics._counters["artifact_lease_requeued_total"] == {(): 1.0}


async def _no_sweep() -> int:
    return 0


# ------------------------------------------------------------- cancel --


def test_cancel_is_owner_scoped_idempotent_and_never_deletes_a_published_version(owner, stranger, monkeypatch):
    _install_render(monkeypatch)
    pipeline.set_composer(_composer())
    row = _accept(owner)
    assert asyncio.run(pipeline.cancel(row["id"], stranger)) is None
    assert adb.load_job(row["id"])["status"] == "queued"
    cancelled = asyncio.run(pipeline.cancel(row["id"], owner))
    assert cancelled["status"] == "cancelled" and cancelled["failure_category"] == "cancelled"
    assert asyncio.run(pipeline.cancel(row["id"], owner))["status"] == "cancelled"
    assert adb.get_version(row["artifact_id"], 1, owner)["status"] == "cancelled"
    assert asyncio.run(pipeline.ensure_running(row["id"])) is False

    done = _accept(owner, generation_id="gen-done")
    assert _run(done["id"])["status"] == "completed"
    after = asyncio.run(pipeline.cancel(done["id"], owner))
    assert after["status"] == "completed"
    assert store.is_published(owner, done["artifact_id"], 1)
    assert adb.get_version(done["artifact_id"], 1, owner)["status"] == "completed"


def test_cancelling_a_running_job_stops_it_and_keeps_the_row_cancelled(owner, monkeypatch):
    _install_render(monkeypatch)
    started = {}

    async def slow_compose(ctx):
        started["yes"] = True
        await asyncio.sleep(30)
        return _spec()

    pipeline.set_composer(slow_compose)
    row = _accept(owner)
    events = []

    async def scenario():
        q = pipeline.subscribe(row["id"])
        assert await pipeline.ensure_running(row["id"])
        await _wait_until(lambda: "yes" in started)
        result = await pipeline.cancel(row["id"], owner)
        assert result["status"] == "cancelled"
        await _wait_until(lambda: not pipeline.is_running(row["id"]))
        while not q.empty():
            events.append(q.get_nowait())
        pipeline.unsubscribe(row["id"], q)

    asyncio.run(scenario())
    assert adb.load_job(row["id"])["status"] == "cancelled"
    assert _lease(row["id"]) == ("", None)
    assert events[-1]["stage"] == pipeline._DONE and events[-1]["status"] == "cancelled"
    assert metrics._counters["artifact_jobs_total"] == {(("result", "cancelled"),): 1.0}
    assert not store.is_published(owner, row["artifact_id"], 1)


def test_wait_for_returns_the_cancelled_row_instead_of_raising(owner, monkeypatch):
    """The chat turn awaits wait_for right after the _done event. When the
    job task is cancelled (Cancel on the card) the awaited shield used to be
    cancelled with it and CancelledError left wait_for — the turn died as if
    Stop had been pressed and no final meta was emitted."""
    _install_render(monkeypatch)
    started = {}

    async def slow_compose(ctx):
        started["yes"] = True
        await asyncio.sleep(30)
        return _spec()

    pipeline.set_composer(slow_compose)
    row = _accept(owner)

    async def scenario():
        assert await pipeline.ensure_running(row["id"])
        waiter = asyncio.get_running_loop().create_task(pipeline.wait_for(row["id"]))
        await _wait_until(lambda: "yes" in started)
        await asyncio.sleep(0.05)  # the waiter is parked on the task
        assert (await pipeline.cancel(row["id"], owner))["status"] == "cancelled"
        fresh = await asyncio.wait_for(waiter, timeout=5.0)
        assert not waiter.cancelled()
        # The CALLER's own cancellation still propagates.
        other = _accept(owner, generation_id="gen-other")
        pipeline.set_composer(slow_compose)
        assert await pipeline.ensure_running(other["id"])
        waiter2 = asyncio.get_running_loop().create_task(pipeline.wait_for(other["id"]))
        await asyncio.sleep(0.05)
        waiter2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter2
        assert pipeline.is_running(other["id"]), "cancelling the waiter did not cancel the job"
        await pipeline.cancel(other["id"], owner)
        await pipeline.wait_for(other["id"])
        return fresh

    fresh = asyncio.run(scenario())
    assert fresh["status"] == "cancelled"


def test_a_cancel_landing_before_mark_running_is_not_overwritten(owner, monkeypatch):
    """The window between _run's load_job and mark_running: the API answered
    {status: cancelled}; the run must not turn the row back to running."""
    _install_render(monkeypatch)
    calls: list = []
    pipeline.set_composer(_composer(calls=calls))
    row = _accept(owner)
    real_mark = adb.mark_running

    def racing_mark(job_id, attempt):
        assert adb.cancel_job(job_id, owner)["status"] == "cancelled"
        return real_mark(job_id, attempt)

    monkeypatch.setattr(adb, "mark_running", racing_mark)
    events = []

    async def scenario():
        q = pipeline.subscribe(row["id"])
        assert await pipeline.ensure_running(row["id"])
        fresh = await pipeline.wait_for(row["id"])
        while not q.empty():
            events.append(q.get_nowait())
        pipeline.unsubscribe(row["id"], q)
        return fresh

    fresh = asyncio.run(scenario())
    assert fresh["status"] == "cancelled" and fresh["attempt"] == 0 and calls == []
    assert adb.get_version(row["artifact_id"], 1, owner)["status"] == "cancelled"
    assert not store.is_published(owner, row["artifact_id"], 1)
    assert events[-1]["stage"] == pipeline._DONE and events[-1]["status"] == "cancelled"
    assert metrics._counters["artifact_jobs_total"] == {(("result", "cancelled"),): 1.0}
    assert _lease(row["id"]) == ("", None)


def test_a_cancel_landing_before_publish_keeps_the_rows_cancelled(owner, monkeypatch):
    """The last heartbeat interval before the publish: the directory is
    renamed (a published version is never deleted) but the rows say what
    the API answered — the version is not completed and the artifact's
    current_version does not move."""
    _install_render(monkeypatch)
    pipeline.set_composer(_composer())
    row = _accept(owner)
    real_publish = adb.publish_version

    def racing_publish(artifact_id, version, job_id, **kw):
        assert adb.cancel_job(job_id, owner)["status"] == "cancelled"
        return real_publish(artifact_id, version, job_id, **kw)

    monkeypatch.setattr(adb, "publish_version", racing_publish)
    events = []

    async def scenario():
        q = pipeline.subscribe(row["id"])
        assert await pipeline.ensure_running(row["id"])
        fresh = await pipeline.wait_for(row["id"])
        while not q.empty():
            events.append(q.get_nowait())
        pipeline.unsubscribe(row["id"], q)
        return fresh

    fresh = asyncio.run(scenario())
    assert fresh["status"] == "cancelled" and fresh["failure_category"] == "cancelled"
    version = adb.get_version(row["artifact_id"], 1, owner)
    assert version["status"] == "cancelled" and version["files"] == []
    assert adb.get_artifact(row["artifact_id"], owner)["current_version"] == 0
    assert store.is_published(owner, row["artifact_id"], 1), "a published directory is never deleted"
    assert events[-1]["stage"] == pipeline._DONE and events[-1]["status"] == "cancelled"
    assert metrics._counters["artifact_jobs_total"] == {(("result", "cancelled"),): 1.0}
    assert adb.user_bytes(owner) == 0, "a cancelled version is not counted against the quota"


def test_a_cancel_landing_before_a_failure_or_a_deferral_is_kept(owner, monkeypatch):
    _install_render(monkeypatch)
    real_set = adb.set_job_status

    def racing_set(job_id, status, **kw):
        adb.cancel_job(job_id, owner)
        return real_set(job_id, status, **kw)

    monkeypatch.setattr(adb, "set_job_status", racing_set)
    pipeline.set_composer(_composer(fail=pipeline.StageFailure("source_unavailable", "gone")))
    row = _accept(owner)
    assert _run(row["id"])["status"] == "cancelled"
    pipeline.set_composer(_composer(fail=ModelUnavailable("http://vllm:30000/v1", 1.0, 1, ConnectionError("x"))))
    row2 = _accept(owner, generation_id="gen-defer")
    assert _run(row2["id"])["status"] == "cancelled"
    assert metrics._counters["artifact_jobs_total"] == {(("result", "cancelled"),): 2.0}


def test_two_concurrent_ensure_running_calls_start_one_run(owner, monkeypatch):
    """The drain and the chat turn (or retry() and the engine's re-kick) both
    pass is_running() and both await the row; with two job slots both used
    to claim the lease (same _OWNER) and run the same stages on the same
    v<N>.tmp at once."""
    _install_render(monkeypatch)
    monkeypatch.setattr(settings, "artifact_max_concurrent_jobs", 2)
    calls: list = []
    pipeline.set_composer(_composer(calls=calls))
    row = _accept(owner)

    async def scenario():
        results = await asyncio.gather(pipeline.ensure_running(row["id"]), pipeline.ensure_running(row["id"]), pipeline.drain_queue())
        fresh = await pipeline.wait_for(row["id"])
        return results, fresh

    results, fresh = asyncio.run(scenario())
    assert results[0] is True and results[1] is True
    assert fresh["status"] == "completed" and fresh["attempt"] == 1
    assert calls == ["Make me a quarterly review PDF"], "compose ran exactly once"


# ----------------------------------------------------------- ownership --


def test_another_users_ids_answer_none(owner, stranger, monkeypatch):
    _install_render(monkeypatch)
    pipeline.set_composer(_composer())
    row = _accept(owner)
    _run(row["id"])
    assert adb.get_job(row["id"], stranger) is None
    assert adb.get_artifact(row["artifact_id"], stranger) is None
    assert adb.get_version(row["artifact_id"], 1, stranger) is None
    assert adb.list_versions(row["artifact_id"], stranger) == []
    assert adb.list_artifacts(stranger) == []
    assert adb.cancel_job(row["id"], stranger) is None
    assert adb.retry_job(row["id"], stranger) is None
    assert adb.get_job(row["id"], owner) is not None
    with pytest.raises(pipeline.ArtifactRefused) as exc:
        _accept(stranger, operation="edit", instruction="shorter", parent=(row["artifact_id"], 1))
    assert exc.value.category == "permission_denied"


# --------------------------------------------------------------- edits --


def test_an_edit_creates_version_two_with_parent_version_one(owner, monkeypatch):
    _install_render(monkeypatch)
    seen = {}

    async def compose(ctx):
        seen["parent"] = ctx.parent_spec
        seen["operation"] = ctx.operation
        return _spec("Quarterly Review, shorter")

    pipeline.set_composer(_composer())
    first = _accept(owner)
    assert _run(first["id"])["status"] == "completed"

    pipeline.set_composer(compose)
    edit = _accept(owner, generation_id="gen-edit", operation="edit", instruction="make it shorter", parent=(first["artifact_id"], 1), formats=["pdf", "docx"])
    assert edit["artifact_id"] == first["artifact_id"] and edit["version"] == 2 and edit["operation"] == "edit"
    assert edit["id"] != first["id"]
    fresh = _run(edit["id"])
    assert fresh["status"] == "completed"
    assert seen["operation"] == "edit" and seen["parent"] is not None and seen["parent"].title == "Quarterly Review"
    v2 = adb.get_version(first["artifact_id"], 2, owner)
    assert v2["parent_version"] == 1 and v2["instruction"] == "make it shorter" and v2["operation"] == "edit"
    assert v2["files"][0]["filename"] == "quarterly-review-shorter-v2.pdf"
    art = adb.get_artifact(first["artifact_id"], owner)
    assert art["current_version"] == 2 and art["title"] == "Quarterly Review, shorter"
    assert [v["version"] for v in adb.list_versions(first["artifact_id"], owner)] == [1, 2]
    assert store.is_published(owner, first["artifact_id"], 1), "v1 is untouched by v2"
    listed = adb.list_artifacts(owner)
    assert listed[0]["current"]["version"] == 2
    # A parent version that does not exist is refused like a foreign id.
    with pytest.raises(pipeline.ArtifactRefused):
        _accept(owner, generation_id="gen-edit-2", operation="edit", instruction="x", parent=(first["artifact_id"], 9))


def test_a_conversion_is_a_new_version_of_the_same_artifact(owner, monkeypatch):
    _install_render(monkeypatch)
    pipeline.set_composer(_composer(_spec("Deck", kind="presentation")))
    first = _accept(owner, kind="presentation", formats=["pptx"])
    assert first["selected_formats"] == ["pptx"]
    assert _run(first["id"])["status"] == "completed"
    conv = _accept(owner, generation_id="gen-conv", operation="convert", instruction="also as pdf", kind="presentation", formats=["pdf"], parent=(first["artifact_id"], 1))
    assert conv["version"] == 2 and conv["operation"] == "convert"
    assert _run(conv["id"])["status"] == "completed"
    assert adb.get_version(first["artifact_id"], 2, owner)["files"][0]["format"] == "pdf"


# -------------------------------------------------------------- defer --


def test_a_model_outage_defers_the_job_rather_than_failing_it(owner, monkeypatch):
    _install_render(monkeypatch)
    outage = ModelUnavailable("http://vllm:30000/v1", 1200.0, 40, ConnectionError("refused"))
    pipeline.set_composer(_composer(fail=outage))
    row = _accept(owner)
    fresh = _run(row["id"])
    assert fresh["status"] == "queued"
    assert fresh["error"].startswith(pipeline.DEFERRED_MARK) and "vllm:30000" not in fresh["error"]
    assert fresh["progress"]["deferrals"] == 1
    assert pipeline._deferred_too_recently(fresh) is True
    assert metrics._counters["artifact_jobs_total"] == {(("result", "deferred"),): 1.0}
    assert asyncio.run(pipeline.drain_queue()) == 0, "not polled again inside the retry delay"
    # The engine comes back: the same job completes.
    pipeline.set_composer(_composer())
    assert _run(row["id"])["status"] == "completed"


def test_deferrals_are_bounded(owner, monkeypatch):
    _install_render(monkeypatch)
    outage = ModelUnavailable("http://vllm:30000/v1", 1.0, 1, ConnectionError("refused"))
    pipeline.set_composer(_composer(fail=outage))
    row = _accept(owner)
    adb.set_job_progress(row["id"], {"deferrals": pipeline._MAX_DEFERRALS - 1})
    fresh = _run(row["id"])
    assert fresh["status"] == "failed" and fresh["failure_category"] == "dependency_unavailable"
    assert "vllm" not in fresh["error"]


def test_a_compose_stage_that_outlasts_the_stage_timeout_still_defers(owner, monkeypatch):
    """With the defaults (600 s stage timeout, 1200 s recovery window) the
    stage timeout always fired before the llm client could raise
    ModelUnavailable, so a model outage FAILED the job as model_failure and
    the contracted requeue never ran. Scaled: 0.3 s timeout, 0.6 s window,
    a composer that waits out the window and then reports the outage."""
    _install_render(monkeypatch)
    monkeypatch.setattr(settings, "artifact_stage_timeout_s", 0.3)
    monkeypatch.setattr(settings, "llm_recovery_window_s", 0.6)
    assert pipeline.stage_timeout("compose") >= 60.6
    assert pipeline.stage_timeout("render") == 0.3, "only the stage that waits on the engine is widened"

    async def waiting_compose(ctx):
        await asyncio.sleep(0.8)
        raise ModelUnavailable("http://vllm:30000/v1", 0.6, 3, ConnectionError("refused"))

    pipeline.set_composer(waiting_compose)
    row = _accept(owner)
    fresh = _run(row["id"])
    assert fresh["status"] == "queued" and fresh["error"].startswith(pipeline.DEFERRED_MARK)
    assert fresh["progress"]["deferrals"] == 1 and fresh["progress"]["stages"]["compose"]["status"] == "deferred"
    assert metrics._counters["artifact_jobs_total"] == {(("result", "deferred"),): 1.0}

    # A render that outlasts the (unwidened) stage timeout still fails.
    async def stuck_render(*args, **kw):
        await asyncio.sleep(2.0)
        return await _fake_render(*args, **kw)

    _install_render(monkeypatch, stuck_render)
    pipeline.set_composer(_composer())
    row2 = _accept(owner, generation_id="gen-stuck")
    fresh2 = _run(row2["id"])
    assert fresh2["status"] == "failed" and "did not finish within 0s" in fresh2["error"]


def test_an_asr_outage_inside_compose_defers_like_a_model_outage(owner, monkeypatch):
    """The brief names ModelUnavailable/ASRUnavailable; the video precedent
    catches the trio. A composer transcribing an attached clip at think/max
    must not fail as model_failure when whisper is the thing that is down."""
    from app.asr import ASRBusy, ASRUnavailable

    _install_render(monkeypatch)
    for exc in (ASRUnavailable("whisper is reloading"), ASRBusy("one clip at a time")):
        pipeline.set_composer(_composer(fail=exc))
        row = _accept(owner, generation_id=f"gen-{type(exc).__name__}")
        fresh = _run(row["id"])
        assert fresh["status"] == "queued" and fresh["error"].startswith(pipeline.DEFERRED_MARK)
        assert "whisper" not in fresh["error"] and "clip" not in fresh["error"]


def test_the_latest_event_map_is_pruned(owner, monkeypatch):
    _install_render(monkeypatch)
    pipeline.set_composer(_composer())
    row = _accept(owner)

    async def scenario():
        q = pipeline.subscribe(row["id"])
        assert await pipeline.ensure_running(row["id"])
        await pipeline.wait_for(row["id"])
        assert pipeline._latest[row["id"]]["stage"] == pipeline._DONE
        # A late subscriber still sees the terminal event while one is watching.
        late = pipeline.subscribe(row["id"])
        assert late.get_nowait()["stage"] == pipeline._DONE
        pipeline.unsubscribe(row["id"], q)
        assert row["id"] in pipeline._latest, "not the last listener yet"
        pipeline.unsubscribe(row["id"], late)
        assert row["id"] not in pipeline._latest, "the last listener left after the terminal event"

    asyncio.run(scenario())
    # The cap: terminal entries nobody watches go first.
    monkeypatch.setattr(pipeline, "_LATEST_CAP", 3)
    for n in range(3):
        pipeline._publish(f"done-{n}", {"stage": pipeline._DONE, "status": "completed"})
    pipeline._publish("live-1", {"stage": "compose", "status": "running"})
    assert set(pipeline._latest) == {"done-1", "done-2", "live-1"}
    pipeline._publish("live-2", {"stage": "render", "status": "running"})
    assert set(pipeline._latest) == {"done-2", "live-1", "live-2"}


def test_compose_paces_against_live_chat_then_runs(owner, monkeypatch):
    _install_render(monkeypatch)
    monkeypatch.setattr(settings, "video_pace_max_wait_s", 2.0)
    pipeline.set_composer(_composer())
    ticks = {"n": 0}

    def busy():
        ticks["n"] += 1
        return ticks["n"] <= 1  # busy once, then free

    pipeline.install_busy_probe(busy)
    row = _accept(owner)
    t0 = time.monotonic()
    assert _run(row["id"])["status"] == "completed"
    assert time.monotonic() - t0 >= 1.0 and ticks["n"] == 2


# ------------------------------------------------------------- health --


def test_health_work_section_counts_artifact_jobs(owner, monkeypatch):
    from app import health

    monkeypatch.setattr(health, "_work_cache", (0.0, {}))
    queued = _accept(owner)
    running = _accept(owner, generation_id="gen-running")
    adb.update_job(running["id"], status="running")
    with db.connection() as con:
        con.execute("UPDATE artifact_jobs SET created_at = created_at - interval '90 seconds' WHERE id = %s", (queued["id"],))
    work = health._check_work()
    assert work["artifacts"]["queued"] == 1 and work["artifacts"]["running"] == 1
    assert 85 <= work["artifacts"]["oldest_queued_age_s"] <= 120
    assert adb.queue_depth() == {"queued": 1, "running": 1} and adb.oldest_queued_age() >= 85
    rendered = metrics.render()
    assert 'artifact_queue_depth{state="queued"} 1' in rendered
    assert 'artifact_queue_depth{state="running"} 1' in rendered
    assert "artifact_oldest_queued_age_seconds" in rendered
    for leak in ("conv-art", queued["id"], queued["artifact_id"], "quarterly"):
        assert leak not in json.dumps(work)
    monkeypatch.setattr(health, "_work_cache", (0.0, {}))


def test_health_artifacts_check_is_additive_and_tolerates_a_missing_renderer(monkeypatch):
    from app import health

    result = health._check_artifacts()
    assert result["volume_writable"] is True
    assert result["status"] in ("ok", "degraded")
    assert "renderers" in result and isinstance(result["free_mb"], int)
    monkeypatch.setattr(settings, "artifacts_enabled", False)
    assert health._check_artifacts()["status"] == "disabled"


# ------------------------------------------------------------- sweep --


def test_the_sweep_removes_an_abandoned_workdir_but_not_one_in_flight(owner, monkeypatch):
    _install_render(monkeypatch)
    pipeline.set_composer(_composer())
    published = _accept(owner)
    assert _run(published["id"])["status"] == "completed"
    abandoned = _accept(owner, generation_id="gen-abandoned")
    work = store.version_workdir(owner, abandoned["artifact_id"], 1)
    ancient = time.time() - 3 * 24 * 3600
    os.utime(work, (ancient, ancient))
    os.utime(store.version_dir(owner, published["artifact_id"], 1), (ancient, ancient))
    monkeypatch.setattr(settings, "artifact_tmp_ttl_hours", 24)
    # A job still QUEUED after the TTL is failed with a plain sentence before
    # its directory goes: material.json is what it would compose from, and a
    # queued job younger than the TTL keeps it (the review of 2026-09-11).
    with db.connection() as con:
        con.execute("UPDATE artifact_jobs SET created_at = created_at - interval '3 days', updated_at = updated_at - interval '3 days' WHERE id = %s", (abandoned["id"],))
    assert asyncio.run(pipeline.sweep()) == 1
    assert not os.path.exists(work)
    assert store.is_published(owner, published["artifact_id"], 1)
    stale = adb.get_job(abandoned["id"], owner)
    assert stale["status"] == "failed" and "never built" in stale["error"]

    fresh = _accept(owner, generation_id="gen-fresh")
    fresh_work = store.version_workdir(owner, fresh["artifact_id"], 1)
    os.utime(fresh_work, (ancient, ancient))
    assert asyncio.run(pipeline.sweep()) == 0, "a queued job's directory is kept while the job is young"
    assert os.path.exists(fresh_work)


def test_the_sweep_ages_a_queued_job_by_its_last_change_and_never_fails_one_a_runner_holds(owner, monkeypatch):
    """Security review 2026-09-12 (#3): the sweep aged queued rows by
    created_at, so a job that failed yesterday and was retried today —
    'queued' with a task parked on the single render slot — was failed by
    the next 30-minute pass as 'never built'; and its set_job_status had
    no only_from, so it landed on a RUNNING row too: the run then found
    its row failed, publish_version answered 'cancelled', and the version
    sat on disk with nothing pointing at it."""
    gate = asyncio.Event()
    knob = {"fail": True}

    async def render(*args, **kw):
        if knob["fail"]:
            raise pipeline.RenderFailed("renderer_failure", "boom")
        await gate.wait()
        return await _fake_render(*args, **kw)

    _install_render(monkeypatch, render)
    pipeline.set_composer(_composer())
    monkeypatch.setattr(settings, "artifact_tmp_ttl_hours", 24)
    old = _accept(owner)
    assert _run(old["id"])["status"] == "failed"
    knob["fail"] = False
    with db.connection() as con:
        con.execute("UPDATE artifact_jobs SET created_at = now() - interval '25 hours', updated_at = now() - interval '25 hours' WHERE id = %s", (old["id"],))
    other = int(db.create_user("artifact-slot-holder", "hash"))
    busy = _accept(other, generation_id="gen-busy")
    work = store.version_workdir(owner, old["artifact_id"], 1)

    async def scenario():
        assert await pipeline.ensure_running(busy["id"])
        await _wait_until(lambda: adb.load_job(busy["id"])["stage"] == "render")
        # (a) The owner retries; the task waits for the slot. The pass must leave it.
        retried = await pipeline.retry(old["id"], owner)
        assert retried["status"] == "queued" and pipeline.is_running(old["id"])
        assert await pipeline.sweep() == 0
        after = adb.load_job(old["id"])
        assert after["status"] == "queued" and after["error"] == "" and os.path.isdir(work), after
        # Even aged past the TTL by its last change, a row this process
        # holds a task for is not the sweep's to fail.
        with db.connection() as con:
            con.execute("UPDATE artifact_jobs SET updated_at = now() - interval '25 hours' WHERE id = %s", (old["id"],))
        assert await pipeline.sweep() == 0
        assert adb.load_job(old["id"])["status"] == "queued" and os.path.isdir(work)
        # (b) The row is RUNNING (the slot is free, the retry is in its render): same.
        gate.set()
        assert (await pipeline.wait_for(busy["id"]))["status"] == "completed"
        await _wait_until(lambda: adb.load_job(old["id"])["stage"] == "render" and adb.load_job(old["id"])["status"] == "running")
        gate.clear()
        with db.connection() as con:
            con.execute("UPDATE artifact_jobs SET updated_at = now() - interval '25 hours' WHERE id = %s", (old["id"],))
        assert await pipeline.sweep() == 0
        assert adb.load_job(old["id"])["status"] == "running"
        # The write the sweep makes never lands on a row that is not queued.
        assert adb.set_job_status(old["id"], "failed", error="never built", failure_category="dependency_unavailable", completed=True, only_from=("queued",)) is False
        gate.set()
        return await pipeline.wait_for(old["id"])

    final = asyncio.run(scenario())
    assert final["status"] == "completed" and final["attempt"] == 2, final
    assert adb.get_artifact(old["artifact_id"], owner)["current_version"] == 1

    # A queued row held by ANOTHER process's live lease (claimed, not yet
    # marked running) is not this process's to fail either.
    stale = _accept(owner, generation_id="gen-leased")

    def aged():
        with db.connection() as con:
            con.execute("UPDATE artifact_jobs SET updated_at = now() - interval '25 hours' WHERE id = %s", (stale["id"],))

    assert adb.claim_lease(stale["id"], "peer:1:00000000", 300.0)
    aged()  # a claim bumps updated_at; the age alone would now fail it
    assert asyncio.run(pipeline.sweep()) == 0
    assert adb.load_job(stale["id"])["status"] == "queued"
    # Its lease lapsed and nothing runs it: now it is the abandoned row the sweep is for.
    assert adb.claim_lease(stale["id"], "peer:1:00000000", -1.0)
    aged()
    asyncio.run(pipeline.sweep())
    gone = adb.load_job(stale["id"])
    assert gone["status"] == "failed" and gone["error"] == pipeline.NEVER_BUILT


# ------------------------------------------------- file identity (§2/§3/§11) --


def _workbook_spec(title: str = "IR Session Audit", *, generator_rows: int = 0, sheets=("Data",)) -> S.ArtifactSpec:
    """A workbook whose first sheet's rows are typed, or — with
    `generator_rows` — made by code from a one-column recipe."""
    body = {"title": title, "sheets": []}
    for i, name in enumerate(sheets):
        sheet = {"name": name, "columns": [{"name": "Host"}, {"name": "Score", "type": "integer"}], "rows": [["a", 1], ["b", 2], ["c", 3]]}
        if generator_rows and i == 0:
            sheet["rows"] = []
            sheet["generator"] = {"rows": generator_rows, "seed": 7, "columns": [
                {"name": "Host", "kind": "name"}, {"name": "Score", "kind": "int", "min": 1, "max": 9},
            ]}
        body["sheets"].append(sheet)
    return S.parse_body("workbook", body)


def _csv_bytes(rows: int, header=("Host", "Score")) -> bytes:
    lines = [",".join(header)] + [f"h{i},{i}" for i in range(1, rows + 1)]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _workbook_render(*, csv_rows=3, per_sheet=(), companion_pdf=True, extra=None):
    """A render of a workbook version in the CONTRACT-2 §3 report shape:
    the xlsx (primary), a CSV per sheet (data; per-sheet names when the
    workbook has several sheets), and docx/pdf companions when selected.
    preview.pdf is written only with `companion_pdf`."""
    async def render(work_dir, spec, formats, title_slug, version, effort):
        files = []

        def put(name, body, **facts):
            with open(os.path.join(work_dir, name), "wb") as fh:
                fh.write(body)
            files.append({"filename": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), **facts})

        sheet_names = [s.name for s in spec.body.sheets]
        for fmt in formats:
            if fmt == "csv":
                if per_sheet:
                    for name in per_sheet:
                        put(f"{title_slug}-v{version}-{T.slug_for(name)}.csv", _csv_bytes(csv_rows), format="csv", role="data", sheet=name, title=name, rows=csv_rows, columns=2)
                else:
                    put(f"{title_slug}-v{version}.csv", _csv_bytes(csv_rows), format="csv", role="data", sheet=sheet_names[0], title=sheet_names[0], rows=csv_rows, columns=2)
            elif fmt == "xlsx":
                put(f"{title_slug}-v{version}.xlsx", b"PK xlsx bytes " * 20, format="xlsx", role="primary", sheets=len(sheet_names), rows=csv_rows, columns=2)
            else:
                put(f"{title_slug}-v{version}.{fmt}", f"{fmt} companion bytes ".encode() * 20, format=fmt, role="companion", pages=2 if fmt == "pdf" else None)
        if companion_pdf:
            with open(os.path.join(work_dir, T.PREVIEW_PDF_NAME), "wb") as fh:
                fh.write(b"%PDF-1.7 companion")
        report = {"files": files, "preview_pdf": T.PREVIEW_PDF_NAME if companion_pdf else None, "preview_kind": "grid", "preview_pages": 0,
                  "warnings": [], "validation": {"reopened": True}, "chart_files": [], "timings": {f: 0.01 for f in formats}}
        if extra is not None:
            extra(work_dir, report)
        return report

    return render


def test_a_workbook_version_publishes_four_files_with_minted_ids_roles_and_counts(owner, monkeypatch):
    """CONTRACT-2 §1-§3: xlsx (primary), csv (data), docx and pdf
    (companions) from one spec; every FileRef carries a code-minted
    file_id, its role, its title, rows/columns; the manifest and the
    published directory hold all four; the wire ref bundles them."""
    _install_render(monkeypatch, _workbook_render())
    pipeline.set_composer(_composer(_workbook_spec()))
    row = _accept(owner, kind="workbook", formats=["xlsx", "csv", "docx", "pdf"])
    fresh = _run(row["id"])
    assert fresh["status"] == "completed", fresh
    version = adb.get_version(row["artifact_id"], 1, owner)
    files = version["files"]
    assert [f["format"] for f in files] == ["xlsx", "csv", "docx", "pdf"]
    assert [f["role"] for f in files] == ["primary", "data", "companion", "companion"]
    for f in files:
        assert T.is_file_id(f["file_id"]) and f["mime_type"] == T.MIME_TYPES[f["format"]]
    xlsx, csv_file, docx, pdf = files
    assert csv_file["file_id"] == T.file_id_for(row["artifact_id"], 1, "data", "csv", "Data")
    assert xlsx["file_id"] == T.file_id_for(row["artifact_id"], 1, "primary", "xlsx")
    assert pdf["file_id"] == T.file_id_for(row["artifact_id"], 1, "companion", "pdf")
    assert csv_file["filename"] == "ir-session-audit-v1.csv" and csv_file["rows"] == 3 and csv_file["columns"] == 2
    # A one-sheet workbook's CSV is the workbook: no " — Data" suffix (the
    # 2026-09-12 screenshots showed "Book — Sheet" on every one-sheet CSV).
    assert csv_file["title"] == "IR Session Audit" and xlsx["title"] == "IR Session Audit"
    assert csv_file["mime_type"] == "text/csv; charset=utf-8" and pdf["pages"] == 2
    final = store.version_dir(owner, row["artifact_id"], 1)
    assert {"ir-session-audit-v1.xlsx", "ir-session-audit-v1.csv", "ir-session-audit-v1.docx", "ir-session-audit-v1.pdf"} <= set(os.listdir(final))
    manifest = store.read_manifest(owner, row["artifact_id"], 1)
    assert set(manifest["sha256s"]) == {f["filename"] for f in files}
    assert (("format", "csv"),) in metrics._hists["artifact_render_seconds"]
    ref = pipeline.ref_for(fresh, version).to_json()
    assert ref["package"] == {"count": 4} and ref["download_all_url"] == f"/artifacts/{row['artifact_id']}/v/1/zip"
    by_fmt = {f["format"]: f for f in ref["files"]}
    assert by_fmt["csv"]["preview_url"] == f"/artifacts/{row['artifact_id']}/v/1/grid?file={csv_file['file_id']}"
    assert by_fmt["xlsx"]["preview_url"].endswith(f"/grid?file={xlsx['file_id']}")
    assert by_fmt["csv"]["download_url"] == f"/artifacts/{row['artifact_id']}/v/1/f/{csv_file['file_id']}?disposition=attachment"


def test_file_ids_are_stable_across_a_retry_and_never_the_workers(owner, monkeypatch):
    """The id is minted from (artifact, version, role, format, sheet): a
    second attempt of the same version mints the same ids, and an id the
    worker writes into its report is ignored."""
    attempts = {"n": 0}

    def plant(work_dir, report):
        for f in report["files"]:
            f["file_id"] = "deadbeefdeadbeef"
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise pipeline.RenderFailed("renderer_failure", "first try fails")

    _install_render(monkeypatch, _workbook_render(extra=plant))
    pipeline.set_composer(_composer(_workbook_spec()))
    row = _accept(owner, kind="workbook", formats=["xlsx", "csv"])
    assert _run(row["id"])["status"] == "failed"
    fresh = asyncio.run(_retry_and_wait(row["id"], owner))
    assert fresh["status"] == "completed" and fresh["attempt"] == 2
    files = adb.get_version(row["artifact_id"], 1, owner)["files"]
    assert "deadbeefdeadbeef" not in {f["file_id"] for f in files}
    assert {f["file_id"] for f in files} == {
        T.file_id_for(row["artifact_id"], 1, "primary", "xlsx"), T.file_id_for(row["artifact_id"], 1, "data", "csv", "Data"),
    }
    # A second, independent version of the same content mints DIFFERENT ids.
    assert T.file_id_for(row["artifact_id"], 2, "primary", "xlsx") != files[0]["file_id"]


def test_per_sheet_csv_names_are_accepted_and_distinct(owner, monkeypatch):
    """A two-sheet workbook delivers one CSV per sheet, named
    download_name(title, version, 'csv', part=<sheet>), each with its own
    id and title (CONTRACT-2 §2); the plain name is still accepted for the
    one-sheet case."""
    _install_render(monkeypatch, _workbook_render(per_sheet=("Data", "Pipeline")))
    pipeline.set_composer(_composer(_workbook_spec(sheets=("Data", "Pipeline"))))
    row = _accept(owner, kind="workbook", formats=["xlsx", "csv"])
    fresh = _run(row["id"])
    assert fresh["status"] == "completed", fresh
    files = adb.get_version(row["artifact_id"], 1, owner)["files"]
    csvs = [f for f in files if f["format"] == "csv"]
    assert [f["filename"] for f in csvs] == ["ir-session-audit-v1-data.csv", "ir-session-audit-v1-pipeline.csv"]
    assert [f["title"] for f in csvs] == ["IR Session Audit — Data", "IR Session Audit — Pipeline"]
    assert csvs[0]["file_id"] != csvs[1]["file_id"]
    assert csvs[1]["file_id"] == T.file_id_for(row["artifact_id"], 1, "data", "csv", "Pipeline")
    final = store.version_dir(owner, row["artifact_id"], 1)
    assert {"ir-session-audit-v1-data.csv", "ir-session-audit-v1-pipeline.csv"} <= set(os.listdir(final)), "per-sheet CSVs survive publication"
    ref = pipeline.ref_for(fresh, adb.get_version(row["artifact_id"], 1, owner)).to_json()
    assert len({f["file_id"] for f in ref["files"]}) == 3 and ref["package"] == {"count": 3}


def test_a_sheet_named_after_the_title_and_a_precomposed_label_both_keep_their_sheet(owner, monkeypatch):
    """The review of 2026-09-12: the old `startswith(title)` rule collapsed
    a sheet called "IR Session Audit 2025" — and a renderer that already
    reported "<title> — <sheet>" — back to the bare title on the wire."""
    def relabel(work_dir, report):
        for f in report["files"]:
            if f.get("sheet") == "Pipeline":
                f["title"] = "IR Session Audit — Pipeline"  # already composed by the renderer
    _install_render(monkeypatch, _workbook_render(per_sheet=("IR Session Audit 2025", "Pipeline"), extra=relabel))
    pipeline.set_composer(_composer(_workbook_spec(sheets=("IR Session Audit 2025", "Pipeline"))))
    row = _accept(owner, kind="workbook", formats=["xlsx", "csv"])
    fresh = _run(row["id"])
    assert fresh["status"] == "completed", fresh
    csvs = [f for f in adb.get_version(row["artifact_id"], 1, owner)["files"] if f["format"] == "csv"]
    assert [f["title"] for f in csvs] == ["IR Session Audit — IR Session Audit 2025", "IR Session Audit — Pipeline"]


def test_the_transform_report_reaches_the_render_job(owner, monkeypatch):
    """CONTRACT-2 §6: what code did to the pasted table (hosts forward-filled,
    blanks kept) is printed in the Word/PDF methodology note — so it must
    travel from the composer to the render worker, through a restart."""
    seen = {}
    real_render = pipeline._render_in_subprocess

    async def render(work_dir, spec, formats, title_slug, version, effort, *, transform=None):
        seen["transform"] = transform
        return await _workbook_render()(work_dir, spec, formats, title_slug, version, effort)

    monkeypatch.setattr(pipeline, "_render_in_subprocess", render)
    monkeypatch.setattr(pipeline, "_page_count", lambda pdf: 1)
    monkeypatch.setattr(pipeline, "_rasterise_page", lambda pdf, page, width: b"\x89PNG")

    async def composer(ctx):
        ctx.record_transform({"rows": 34, "blanks": 19, "forward_filled": 25})
        return _workbook_spec()

    pipeline.set_composer(composer)
    row = _accept(owner, kind="workbook", formats=["xlsx", "csv"])
    assert _run(row["id"])["status"] == "completed"
    assert seen["transform"] == {"rows": 34, "blanks": 19, "forward_filled": 25}
    # Scratch, like material.json: the published directory holds exactly
    # what CONTRACT §6 lists (security review 2026-09-12, #4).
    published = store.version_dir(owner, row["artifact_id"], 1)
    assert not os.path.exists(os.path.join(published, store.TRANSFORM_NAME))
    assert set(os.listdir(published)) <= store.PUBLISHED_NAMES | {f["filename"] for f in adb.get_version(row["artifact_id"], 1, owner)["files"]}
    # And the real subprocess writer puts it in the job file the worker reads.
    work = os.path.join(store.reports_dir(), "transform-probe")
    os.makedirs(work, exist_ok=True)
    try:
        asyncio.run(real_render(work, _workbook_spec(), ["xlsx"], "x", 1, "fast", transform={"rows": 2}))
    except pipeline.RenderFailed:
        pass  # the job file is written before the worker runs; its outcome is not this test's
    assert json.load(open(os.path.join(work, store.JOB_NAME)))["transform"] == {"rows": 2}


def test_the_material_round_trip_keeps_row_count_and_transform():
    m = pipeline._material({"history_text": "h", "row_count": "500", "transform": {"rows": 34}})
    assert m["row_count"] == 500 and m["transform"] == {"rows": 34}
    assert pipeline._material({})["row_count"] is None and pipeline._material({})["transform"] == {}


def test_two_files_for_one_role_format_sheet_are_refused(owner, monkeypatch):
    def duplicate(work_dir, report):
        report["files"].append(dict(report["files"][-1]))

    _install_render(monkeypatch, _workbook_render(extra=duplicate))
    pipeline.set_composer(_composer(_workbook_spec()))
    row = _accept(owner, kind="workbook", formats=["xlsx", "csv"])
    fresh = _run(row["id"])
    assert fresh["status"] == "failed" and fresh["failure_category"] == "validation_failure"
    assert "two csv files" in fresh["error"]


def test_a_csv_whose_rows_differ_from_the_generator_is_not_published(owner, monkeypatch):
    """CONTRACT-2 §11, belt and braces: the spec's generator promised 5
    rows; the CSV on disk has 3. The pipeline counts the rows from the
    bytes — a renderer that REPORTS 5 over a 3-row file is caught the
    same way — and refuses with both numbers in the sentence."""
    _install_render(monkeypatch, _workbook_render(csv_rows=3))
    pipeline.set_composer(_composer(_workbook_spec(generator_rows=5)))
    row = _accept(owner, kind="workbook", formats=["xlsx", "csv"])
    fresh = _run(row["id"])
    assert fresh["status"] == "failed" and fresh["failure_category"] == "validation_failure"
    assert "3 data rows" in fresh["error"] and "5 were asked for" in fresh["error"]
    assert not store.is_published(owner, row["artifact_id"], 1)

    def lie(work_dir, report):
        for f in report["files"]:
            if f["format"] == "csv":
                f["rows"] = 5

    _install_render(monkeypatch, _workbook_render(csv_rows=3, extra=lie))
    row2 = _accept(owner, generation_id="gen-lie", kind="workbook", formats=["xlsx", "csv"])
    fresh2 = _run(row2["id"])
    assert fresh2["status"] == "failed" and "3 data rows" in fresh2["error"] and "reported 5" in fresh2["error"]

    # The promised count, delivered: published, rows recorded from the bytes.
    _install_render(monkeypatch, _workbook_render(csv_rows=5))
    row3 = _accept(owner, generation_id="gen-ok", kind="workbook", formats=["xlsx", "csv"])
    fresh3 = _run(row3["id"])
    assert fresh3["status"] == "completed", fresh3
    csv_file = next(f for f in adb.get_version(row3["artifact_id"], 1, owner)["files"] if f["format"] == "csv")
    assert csv_file["rows"] == 5


def test_a_grid_version_with_a_pdf_companion_gets_pages_and_a_thumbnail(owner, monkeypatch):
    """Wave 1's addendum: ArtifactRef links a pdf/docx companion of a grid
    version to /preview only while preview_pages > 0, so the preview
    stage counts the companion's pages and rasterises page 1 exactly as
    for a pages version — preview_kind stays 'grid'. Without a
    preview.pdf the companion is download-only and nothing is warned."""
    _install_render(monkeypatch, _workbook_render(companion_pdf=True))
    pipeline.set_composer(_composer(_workbook_spec()))
    row = _accept(owner, kind="workbook", formats=["xlsx", "csv", "pdf"])
    fresh = _run(row["id"])
    assert fresh["status"] == "completed", fresh
    version = adb.get_version(row["artifact_id"], 1, owner)
    assert version["preview_kind"] == "grid" and version["preview_pages"] == 2
    final = store.version_dir(owner, row["artifact_id"], 1)
    assert set(os.listdir(os.path.join(final, "previews"))) == {"1-240.png", "1-1400.png"}
    ref = pipeline.ref_for(fresh, version).to_json()
    by_fmt = {f["format"]: f for f in ref["files"]}
    assert by_fmt["pdf"]["preview_url"] == f"/artifacts/{row['artifact_id']}/v/1/preview"
    assert by_fmt["xlsx"]["preview_url"].startswith(f"/artifacts/{row['artifact_id']}/v/1/grid?file=")
    assert ref["preview_url"].endswith("/v/1/sheets") and ref["thumbnail_url"].endswith("/v/1/preview/1.png?w=240")

    _install_render(monkeypatch, _workbook_render(companion_pdf=False))
    row2 = _accept(owner, generation_id="gen-no-pdf", kind="workbook", formats=["xlsx", "csv", "docx"])
    fresh2 = _run(row2["id"])
    assert fresh2["status"] == "completed", fresh2
    version2 = adb.get_version(row2["artifact_id"], 1, owner)
    assert version2["preview_kind"] == "grid" and version2["preview_pages"] == 0 and version2["warnings"] == []
    ref2 = pipeline.ref_for(fresh2, version2).to_json()
    assert {f["format"]: f["preview_url"] for f in ref2["files"]}["docx"] == "", "download-only without a page preview"
    assert ref2["thumbnail_url"] == ""


def test_ref_for_upgrades_a_legacy_version_row_without_file_ids():
    """A row persisted before 2026-09-12 has files with no file_id, role,
    title, rows or columns. On the wire every file still carries them:
    the id is the one the pipeline would have minted (so it is stable
    across reloads), the role follows the kind's native format, the title
    is the version's, rows/columns are None (CONTRACT-2 §2)."""
    aid = "c" * 32
    job = {"artifact_id": aid, "version": 3, "id": "j" * 32, "status": "completed", "kind": "document", "title": "Old Report"}
    legacy = {"files": [
        {"format": "pdf", "filename": "old-report-v3.pdf", "mime_type": "application/pdf", "size": 10, "sha256": "a" * 64, "pages": 4},
        {"format": "docx", "filename": "old-report-v3.docx", "mime_type": T.MIME_TYPES["docx"], "size": 20, "sha256": "b" * 64},
    ], "preview_kind": "pages", "preview_pages": 4, "title": "Old Report", "kind": "document"}
    ref = pipeline.ref_for(job, legacy)
    pdf, docx = ref.files
    assert (pdf.role, docx.role) == ("companion", "primary"), "docx is a document's native format (FORMATS_FOR_KIND)"
    assert pdf.file_id == T.file_id_for(aid, 3, "companion", "pdf") and docx.file_id == T.file_id_for(aid, 3, "primary", "docx")
    assert pdf.title == "Old Report" and pdf.rows is None and pdf.columns is None and pdf.pages == 4
    wire = ref.to_json()
    assert wire["files"][0]["download_url"] == f"/artifacts/{aid}/v/3/f/{pdf.file_id}?disposition=attachment"
    assert wire["files"][0]["preview_url"] == f"/artifacts/{aid}/v/3/preview"
    assert wire["download_all_url"] == f"/artifacts/{aid}/v/3/zip" and wire["package"] == {"count": 2}
    # The same row read twice gives the same ids — a reload never re-keys a card.
    assert [f.file_id for f in pipeline.ref_for(job, legacy).files] == [pdf.file_id, docx.file_id]
    # A deck: pptx primary, pdf companion; a workbook: xlsx primary.
    deck = pipeline.ref_for({**job, "kind": "presentation"}, {"files": [{"format": "pdf", "filename": "d-v3.pdf", "size": 1}, {"format": "pptx", "filename": "d-v3.pptx", "size": 1}], "kind": "presentation"})
    assert [f.role for f in deck.files] == ["companion", "primary"]
    book = pipeline.ref_for({**job, "kind": "workbook"}, {"files": [{"format": "xlsx", "filename": "b-v3.xlsx", "size": 1}], "kind": "workbook", "preview_kind": "grid"})
    assert book.files[0].role == "primary" and book.to_json()["files"][0]["preview_url"] == f"/artifacts/{aid}/v/3/grid?file={book.files[0].file_id}"
    # A row that already carries an id keeps it, whatever the derivation says.
    kept = pipeline.ref_for(job, {"files": [{"format": "pdf", "filename": "x-v3.pdf", "size": 1, "file_id": "0123456789abcdef", "role": "primary", "title": "Sheet — A", "rows": 7, "columns": 2}]})
    assert kept.files[0].file_id == "0123456789abcdef" and kept.files[0].role == "primary" and kept.files[0].rows == 7 and kept.files[0].title == "Sheet — A"


def test_ref_for_skips_a_malformed_files_entry_with_a_log_line(caplog):
    """Security review 2026-09-12 (#6): `int(f.get("size") or 0)` raised on
    a row whose size was text ("abc", "4021.0", "1e3") and the version,
    its files, the listing, the artifact and the job routes all answered
    500 for one corrupt row (a bad migration, a manual edit, a writer that
    stores size as text). A non-dict entry was dropped silently. Both are
    skipped with a log line naming the entry; the rest of the version is
    served."""
    aid = "d" * 32
    job = {"artifact_id": aid, "version": 1, "id": "j" * 32, "status": "completed", "kind": "document", "title": "Report"}
    good = {"format": "pdf", "filename": "report-v1.pdf", "size": 10, "sha256": "a" * 64, "file_id": "0123456789abcdef", "role": "companion"}
    bad_size = {"format": "docx", "filename": "report-v1.docx", "size": "abc"}
    with caplog.at_level(logging.WARNING, logger="app.artifacts.pipeline"):
        ref = pipeline.ref_for(job, {"files": [good, bad_size, "x", 1, None, [good], {"format": "docx", "filename": "r.docx", "size": "4021.0"}, {"format": "docx", "filename": "r.docx", "size": "1e3"}, {"format": "docx", "filename": "r.docx", "size": []}]})
    assert [f.file_id for f in ref.files] == ["0123456789abcdef"]
    lines = [r.getMessage() for r in caplog.records if "malformed" in r.getMessage()]
    assert len(lines) == 8 and all(aid[:8] in line for line in lines), lines
    # A size that is missing, empty or a numeric string is the legacy shape and reads as it did.
    lenient = pipeline.ref_for(job, {"files": [{**good, "size": None}, {**good, "size": ""}, {**good, "size": "12"}, {**good, "size": 7.0}]})
    assert [f.size for f in lenient.files] == [0, 0, 12, 7]
    # A files value that is not a list at all is an empty version, not a crash.
    assert pipeline.ref_for(job, {"files": "not a list"}).files == []
    assert pipeline.ref_for(job, {"files": {"a": 1}}).files == []


# ---------------------------------------------------------- subprocess --


def test_render_env_is_scrubbed(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("APP_DATABASE_URL", "postgresql://u:p@db/x")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = pipeline.render_env(str(tmp_path))
    assert "OPENAI_API_KEY" not in env and "APP_DATABASE_URL" not in env
    assert env["PATH"] == "/usr/bin" and env["MPLCONFIGDIR"] == str(tmp_path / ".mpl")
    assert env["PYTHONPATH"].split(os.pathsep)[0].endswith("orchestrator")
    assert set(env) <= {"PATH", "HOME", "LANG", "PYTHONPATH", "MPLCONFIGDIR", "FONTCONFIG_FILE"}


def test_the_render_subprocess_protocol_reads_the_report_and_the_error(monkeypatch, tmp_path, caplog):
    """A stand-in worker module: proves the argv/cwd/report contract without
    the real render package (which ships separately)."""
    fake_pkg = tmp_path / "pkg" / "app" / "artifacts" / "render"
    fake_pkg.mkdir(parents=True)
    for d in (fake_pkg.parent.parent, fake_pkg.parent, fake_pkg):
        (d / "__init__.py").write_text("")
    (fake_pkg / "worker.py").write_text(
        "import json, os, sys\n"
        "job = json.load(open(sys.argv[1]))\n"
        "out = job['out_dir']\n"
        "assert os.getcwd() == os.path.realpath(out), (os.getcwd(), out)\n"
        "if job['spec']['document']['title'] == 'boom':\n"
        "    json.dump({'error': {'category': 'renderer_failure', 'message': 'no fonts'}}, open(os.path.join(out, 'render-report.json'), 'w'))\n"
        "    print('Traceback (most recent call last):\\n  File x\\nAssertionError: URL fetcher must return', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "name = f\"{job['title_slug']}-v{job['version']}.pdf\"\n"
        "open(os.path.join(out, name), 'wb').write(b'%PDF')\n"
        "json.dump({'files': [{'format': 'pdf', 'filename': name, 'size': 4}], 'preview_pdf': None, 'preview_kind': 'none', 'preview_pages': 0, 'warnings': [], 'validation': {}, 'chart_files': []}, open(os.path.join(out, 'render-report.json'), 'w'))\n"
    )
    monkeypatch.setattr(pipeline, "_APP_ROOT", str(tmp_path / "pkg"))
    work = tmp_path / "work" / "v1.tmp"
    work.mkdir(parents=True)

    report = asyncio.run(pipeline._render_in_subprocess(str(work), _spec("ok"), ["pdf"], "ok", 1, "fast"))
    assert report["files"][0]["filename"] == "ok-v1.pdf" and (work / "ok-v1.pdf").read_bytes() == b"%PDF"
    assert json.load(open(work / store.JOB_NAME))["formats"] == ["pdf"]

    with caplog.at_level(logging.WARNING, logger="app.artifacts.pipeline"):
        with pytest.raises(pipeline.RenderFailed) as exc:
            asyncio.run(pipeline._render_in_subprocess(str(work), _spec("boom"), ["pdf"], "boom", 1, "fast"))
    assert exc.value.category == "renderer_failure" and exc.value.message == "no fonts"
    # The person gets the sentence; the operator's log gets the worker's
    # stderr (the traceback), which exists nowhere else.
    assert "URL fetcher must return" in caplog.text and "renderer_failure" in caplog.text


def test_the_render_subprocess_is_killed_on_timeout(monkeypatch, tmp_path):
    fake_pkg = tmp_path / "pkg" / "app" / "artifacts" / "render"
    fake_pkg.mkdir(parents=True)
    for d in (fake_pkg.parent.parent, fake_pkg.parent, fake_pkg):
        (d / "__init__.py").write_text("")
    (fake_pkg / "worker.py").write_text("import time\ntime.sleep(30)\n")
    monkeypatch.setattr(pipeline, "_APP_ROOT", str(tmp_path / "pkg"))
    monkeypatch.setattr(settings, "artifact_render_timeout_s", 1.0)
    work = tmp_path / "work" / "v1.tmp"
    work.mkdir(parents=True)
    t0 = time.monotonic()
    with pytest.raises(pipeline.RenderFailed) as exc:
        asyncio.run(pipeline._render_in_subprocess(str(work), _spec("slow"), ["pdf"], "slow", 1, "fast"))
    assert time.monotonic() - t0 < 10 and "within 1s" in exc.value.message
