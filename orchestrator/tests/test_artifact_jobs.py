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
import hashlib
import json
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
    with pytest.raises(pipeline.ArtifactRefused) as exc:
        _accept(owner, kind="workbook", formats=["pdf"])
    assert exc.value.category == "invalid_request"
    row = _accept(owner, kind="presentation", formats=["pdf", "pptx", "xlsx"])
    assert row["selected_formats"] == ["pdf", "pptx"]


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
    # The ArtifactRef the card is built from.
    ref = pipeline.ref_for(fresh, version).to_json()
    assert ref["status"] == "completed" and ref["files"][0]["download_url"].endswith("/file/pdf?disposition=attachment")
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
    assert asyncio.run(pipeline.sweep()) == 1
    assert not os.path.exists(work)
    assert store.is_published(owner, published["artifact_id"], 1)


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


def test_the_render_subprocess_protocol_reads_the_report_and_the_error(monkeypatch, tmp_path):
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

    with pytest.raises(pipeline.RenderFailed) as exc:
        asyncio.run(pipeline._render_in_subprocess(str(work), _spec("boom"), ["pdf"], "boom", 1, "fast"))
    assert exc.value.category == "renderer_failure" and exc.value.message == "no fonts"


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
