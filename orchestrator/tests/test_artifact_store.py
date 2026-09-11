"""artifacts/store.py — where a version lives, how it is published, and
what the sweep may touch. Filesystem only; no database, no renderer.

The invariants pinned here are CONTRACT §4 and §6: everything is written
under `v<N>.tmp/`, publication is one rename, a published `v<N>/` is never
removed by the sweep, and the file resolver refuses any path that leaves
the version directory."""
from __future__ import annotations

import os
import time

import pytest

from app.artifacts import spec as S
from app.artifacts import store
from app.artifacts import types as T
from app.config import settings

USER = 7
ART = "a" * 32


@pytest.fixture(autouse=True)
def reports(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path / "reports"))
    yield tmp_path / "reports"


def _spec(title: str = "Quarterly Review") -> S.ArtifactSpec:
    return S.parse_body("document", {"title": title, "blocks": [{"type": "paragraph", "text": "Hello."}]})


def test_paths_are_owner_scoped_and_id_keyed(reports):
    assert store.version_dir(USER, ART, 2) == f"{reports}/artifacts/{USER}/{ART}/v2"
    assert store.version_workdir(USER, ART, 2) == f"{reports}/artifacts/{USER}/{ART}/v2.tmp"
    with pytest.raises(ValueError):
        store.version_dir(USER, "../../etc", 1)
    with pytest.raises(ValueError):
        store.version_dir(USER, "A" * 32, 1)  # uppercase is not an id


def test_spec_round_trips_through_spec_json(reports):
    work = store.ensure_workdir(USER, ART, 1)
    assert os.path.isdir(os.path.join(work, T.PREVIEWS_DIR))
    store.write_spec(work, _spec())
    back = store.read_spec(work)
    assert back is not None and back.title == "Quarterly Review" and back.kind == "document"
    assert store.read_spec(str(reports / "nowhere")) is None


def test_a_newer_spec_version_is_refused_not_guessed(reports):
    work = store.ensure_workdir(USER, ART, 1)
    store.write_json(os.path.join(work, T.SPEC_NAME), {"spec_version": 99, "kind": "document"})
    with pytest.raises(ValueError):
        store.read_spec(work)


def test_publish_is_one_rename_and_the_manifest_lists_the_files(reports):
    work = store.ensure_workdir(USER, ART, 1)
    with open(os.path.join(work, "quarterly-review-v1.pdf"), "wb") as fh:
        fh.write(b"%PDF-1.7 fake")
    digest = store.sha256_file(os.path.join(work, "quarterly-review-v1.pdf"))
    files = [{"format": "pdf", "filename": "quarterly-review-v1.pdf", "size": 13, "sha256": digest}]
    manifest = store.build_manifest(artifact_id=ART, version=1, files=files)
    final = store.publish(work, manifest)
    assert final == store.version_dir(USER, ART, 1)
    assert not os.path.exists(work), "the working directory became the version directory"
    assert store.is_published(USER, ART, 1)
    read = store.read_manifest(USER, ART, 1)
    assert read["artifact_id"] == ART and read["version"] == 1
    assert read["sha256s"] == {"quarterly-review-v1.pdf": digest}
    assert read["template_version"] == T.TEMPLATE_VERSION and read["renderer_version"] == T.RENDERER_VERSION


def test_publish_strips_the_scratch_and_keeps_what_the_contract_lists(reports):
    """CONTRACT §6. material.json (chat content), render-job.json and
    render-report.json (absolute paths), preview.json, the matplotlib cache
    and the renderer's chart PNGs were all being renamed into the immutable
    published directory and kept forever."""
    work = store.ensure_workdir(USER, ART, 1)
    for name in (store.MATERIAL_NAME, store.JOB_NAME, store.RENDER_REPORT_NAME, store.PREVIEW_META_NAME):
        store.write_json(os.path.join(work, name), {"secret": "history text", "path": work})
    os.makedirs(os.path.join(work, ".mpl"))
    with open(os.path.join(work, ".mpl", "fontlist.json"), "w") as fh:
        fh.write("{}")
    for name in ("quarterly-review-v1.pdf", T.PREVIEW_PDF_NAME, "chart-1.png"):
        with open(os.path.join(work, name), "wb") as fh:
            fh.write(b"%PDF")
    store.write_spec(work, _spec())
    store.write_json(os.path.join(work, T.VALIDATION_NAME), {"files": []})
    manifest = store.build_manifest(
        artifact_id=ART, version=1, files=[],
        preview={"preview_kind": "pages", "preview_pages": 3, "thumbnails": ["1-240.png"]}, warnings=["a caveat", "", "a caveat"],
    )
    final = store.publish(work, manifest, scratch=["chart-1.png", "../escape.pdf", T.SPEC_NAME])
    assert set(os.listdir(final)) == {"manifest.json", "spec.json", "validation.json", "preview.pdf", "previews", "quarterly-review-v1.pdf"}
    read = store.read_manifest(USER, ART, 1)
    assert read["preview"] == {"preview_kind": "pages", "preview_pages": 3, "thumbnails": ["1-240.png"]}
    assert read["warnings"] == ["a caveat"]
    assert store.read_spec(final) is not None, "a published name can never be named as scratch"


def test_publish_is_idempotent_on_an_already_published_version(reports):
    """A crash between the rename and the row update leaves a published
    directory; the next attempt must not rename over it or fail."""
    work = store.ensure_workdir(USER, ART, 1)
    store.publish(work, store.build_manifest(artifact_id=ART, version=1, files=[]))
    stale = store.ensure_workdir(USER, ART, 1)  # a second attempt's scratch
    with open(os.path.join(stale, "junk.bin"), "wb") as fh:
        fh.write(b"x")
    final = store.publish(stale, store.build_manifest(artifact_id=ART, version=1, files=[]))
    assert final == store.version_dir(USER, ART, 1)
    assert not os.path.exists(stale)
    assert not os.path.exists(os.path.join(final, "junk.bin")), "the published version is immutable"


def test_publish_refuses_a_directory_that_is_not_a_workdir(reports):
    published = store.version_dir(USER, ART, 1)
    os.makedirs(published)
    with pytest.raises(store.StorageError):
        store.publish(published, {})
    with pytest.raises(store.StorageError):
        store.publish(store.version_workdir(USER, ART, 3), {})  # never created


def test_sweep_removes_old_workdirs_and_never_a_published_version(reports):
    old_work = store.ensure_workdir(USER, ART, 2)
    fresh_work = store.ensure_workdir(USER, ART, 3)
    published = store.ensure_workdir(USER, ART, 1)
    store.publish(published, store.build_manifest(artifact_id=ART, version=1, files=[]))
    ancient = time.time() - 3 * 24 * 3600
    os.utime(old_work, (ancient, ancient))
    os.utime(store.version_dir(USER, ART, 1), (ancient, ancient))
    # A directory that merely LOOKS old but is not a v<N>.tmp is left alone too.
    stray = os.path.join(store.artifact_dir(USER, ART), "notes")
    os.makedirs(stray)
    os.utime(stray, (ancient, ancient))

    removed = store.sweep_abandoned(24)

    assert removed == 1
    assert not os.path.exists(old_work)
    assert os.path.isdir(fresh_work), "younger than the TTL"
    assert store.is_published(USER, ART, 1), "a published version is never swept"
    assert os.path.isdir(stray)


def test_sweep_skips_a_workdir_a_running_job_owns(reports):
    work = store.ensure_workdir(USER, ART, 4)
    ancient = time.time() - 3 * 24 * 3600
    os.utime(work, (ancient, ancient))
    assert store.sweep_abandoned(24, skip={work}) == 0
    assert os.path.isdir(work)


def test_sweep_skip_survives_a_reports_dir_with_a_trailing_slash(reports, monkeypatch):
    """types.artifact_dir rstrips the '/', os.scandir does not: with
    REPORTS_DIR='/reports/' the skip set never matched and a live job
    older than the TTL lost its working directory mid-run."""
    monkeypatch.setattr(settings, "reports_dir", str(reports) + "/")
    work = store.ensure_workdir(USER, ART, 5)
    assert "//" not in work
    ancient = time.time() - 3 * 24 * 3600
    os.utime(work, (ancient, ancient))
    assert store.sweep_abandoned(24, skip={work}) == 0
    assert os.path.isdir(work)
    assert store.sweep_abandoned(24) == 1


def test_sweep_on_a_missing_root_is_zero(reports):
    assert store.sweep_abandoned(1) == 0


def test_resolver_stays_inside_the_version_directory(reports):
    work = store.ensure_workdir(USER, ART, 1)
    with open(os.path.join(work, "quarterly-review-v1.docx"), "wb") as fh:
        fh.write(b"PK")
    store.publish(work, store.build_manifest(artifact_id=ART, version=1, files=[]))
    path = store.resolve_version_file(USER, ART, 1, "docx", "quarterly-review-v1.docx")
    assert path == os.path.realpath(os.path.join(store.version_dir(USER, ART, 1), "quarterly-review-v1.docx"))
    for bad in ("../v2/x.docx", "/etc/passwd.docx", ".hidden.docx", "quarterly-review-v1.pdf"):
        with pytest.raises(store.PathRefused):
            store.resolve_version_file(USER, ART, 1, "docx", bad)
    with pytest.raises(store.PathRefused):
        store.resolve_version_file(USER, ART, 1, "exe", "quarterly-review-v1.exe")
    assert store.resolve_preview_pdf(USER, ART, 1).endswith("/v1/preview.pdf")
    assert store.preview_png_path(USER, ART, 1, 3, 240).endswith("/v1/previews/3-240.png")
    with pytest.raises(store.PathRefused):
        store.preview_png_path(USER, ART, 1, 0, 240)
    with pytest.raises(store.PathRefused):
        store.preview_png_path(USER, ART, 1, 1, 999)


def test_a_symlink_out_of_the_version_directory_is_refused(reports):
    work = store.ensure_workdir(USER, ART, 1)
    outside = reports / "secret.pdf"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"%PDF")
    os.symlink(str(outside), os.path.join(work, "leak-v1.pdf"))
    store.publish(work, store.build_manifest(artifact_id=ART, version=1, files=[]))
    with pytest.raises(store.PathRefused):
        store.resolve_version_file(USER, ART, 1, "pdf", "leak-v1.pdf")


def test_resolve_file_by_id_uses_the_row_and_stays_inside_the_directory(reports):
    """CONTRACT-2 §2: a file is found by the id the pipeline minted, through
    the version row's list — never a request-supplied name. The list's
    entry is checked like a by-format name (a bare basename whose
    extension is its format) and the path for containment; a value that
    is not an id is refused before anything is looked at."""
    work = store.ensure_workdir(USER, ART, 1)
    for name in ("quarterly-review-v1.docx", "quarterly-review-v1-data.csv"):
        with open(os.path.join(work, name), "wb") as fh:
            fh.write(b"PK")
    outside = reports / "secret.pdf"
    outside.write_bytes(b"%PDF")
    os.symlink(str(outside), os.path.join(work, "quarterly-review-v1.pdf"))
    docx_id = T.file_id_for(ART, 1, "primary", "docx")
    csv_id = T.file_id_for(ART, 1, "data", "csv", "Data")
    pdf_id = T.file_id_for(ART, 1, "companion", "pdf")
    files = [
        {"file_id": docx_id, "role": "primary", "format": "docx", "filename": "quarterly-review-v1.docx", "size": 2},
        {"file_id": csv_id, "role": "data", "format": "csv", "filename": "quarterly-review-v1-data.csv", "size": 2, "title": "Quarterly Review — Data"},
        {"file_id": pdf_id, "role": "companion", "format": "pdf", "filename": "quarterly-review-v1.pdf", "size": 4},
    ]
    store.publish(work, store.build_manifest(artifact_id=ART, version=1, files=files))
    root = store.version_dir(USER, ART, 1)

    path, entry = store.resolve_file_by_id(USER, ART, 1, csv_id, files)
    assert path == os.path.realpath(os.path.join(root, "quarterly-review-v1-data.csv"))
    assert entry["title"] == "Quarterly Review — Data" and entry["role"] == "data"
    # With no list given, the manifest on disk is the list.
    assert store.resolve_file_by_id(USER, ART, 1, docx_id)[0].endswith("/v1/quarterly-review-v1.docx")
    # A symlink out of the directory, even when the row names it.
    with pytest.raises(store.PathRefused):
        store.resolve_file_by_id(USER, ART, 1, pdf_id, files)
    # Not an id: refused before any list is read.
    for bad in ("", "..", "../" + docx_id, docx_id[:15], docx_id.upper(), "g" * 16, "/etc/passwd"):
        with pytest.raises(store.PathRefused):
            store.resolve_file_by_id(USER, ART, 1, bad, files)
    # An id the version does not have.
    with pytest.raises(store.PathRefused):
        store.resolve_file_by_id(USER, ART, 1, T.file_id_for(ART, 2, "primary", "docx"), files)
    # A row entry that names a path, a hidden file, or a wrong extension
    # cannot reach outside — whatever wrote the row.
    for entry in (
        {"file_id": docx_id, "format": "docx", "filename": "../v2/quarterly-review-v2.docx"},
        {"file_id": docx_id, "format": "docx", "filename": "previews/../quarterly-review-v1.docx"},
        {"file_id": docx_id, "format": "docx", "filename": "sub\\quarterly-review-v1.docx"},
        {"file_id": docx_id, "format": "docx", "filename": ".quarterly-review-v1.docx"},
        {"file_id": docx_id, "format": "pdf", "filename": "quarterly-review-v1.docx"},
        {"file_id": docx_id, "format": "exe", "filename": "quarterly-review-v1.exe"},
        {"file_id": docx_id, "format": "json", "filename": "manifest.json"},
    ):
        with pytest.raises(store.PathRefused):
            store.resolve_file_by_id(USER, ART, 1, docx_id, [entry])
    # resolve_version_file now takes every type we serve, still by extension.
    assert store.resolve_version_file(USER, ART, 1, "csv", "quarterly-review-v1-data.csv").endswith("-data.csv")
    with pytest.raises(store.PathRefused):
        store.resolve_version_file(USER, ART, 1, "csv", "quarterly-review-v1.docx")


def test_publish_never_removes_a_file_the_manifest_lists(reports):
    """A per-sheet CSV named as scratch by a confused caller stays: the
    published names are the fixed ones plus the manifest's files."""
    work = store.ensure_workdir(USER, ART, 1)
    for name in ("audit-v1.xlsx", "audit-v1-data.csv", "audit-v1-pipeline.csv", "chart-1.png"):
        with open(os.path.join(work, name), "wb") as fh:
            fh.write(b"x")
    files = [{"format": "xlsx", "filename": "audit-v1.xlsx", "size": 1}, {"format": "csv", "filename": "audit-v1-data.csv", "size": 1}, {"format": "csv", "filename": "audit-v1-pipeline.csv", "size": 1}]
    manifest = store.build_manifest(artifact_id=ART, version=1, files=files)
    assert store.published_names(manifest) >= {"audit-v1.xlsx", "audit-v1-data.csv", "audit-v1-pipeline.csv", T.MANIFEST_NAME}
    final = store.publish(work, manifest, scratch=["chart-1.png", "audit-v1-pipeline.csv"])
    assert set(os.listdir(final)) == {"manifest.json", "previews", "audit-v1.xlsx", "audit-v1-data.csv", "audit-v1-pipeline.csv"}


def test_free_space_and_volume_writable(reports, monkeypatch):
    monkeypatch.setattr(settings, "artifact_min_free_mb", 1)
    assert store.volume_writable() is True
    assert store.free_space_ok() is True
    monkeypatch.setattr(settings, "artifact_min_free_mb", 10 ** 9)  # a petabyte
    assert store.free_space_ok() is False
    assert isinstance(store.free_space_mb(), int)


def test_quota_compares_published_bytes_to_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "artifact_user_quota_mb", 1)
    assert store.quota_ok(USER, published_bytes=0) is True
    assert store.quota_ok(USER, published_bytes=1024 * 1024 - 1) is True
    assert store.quota_ok(USER, published_bytes=1024 * 1024) is False


def test_write_bytes_is_atomic_and_write_json_never_leaves_a_partial(reports):
    work = store.ensure_workdir(USER, ART, 1)
    dest = os.path.join(work, T.PREVIEWS_DIR, "1-240.png")
    assert store.write_bytes(dest, b"\x89PNG") == 4
    assert open(dest, "rb").read() == b"\x89PNG"
    store.write_json(os.path.join(work, "x.json"), {"a": 1})
    assert store.read_json(os.path.join(work, "x.json")) == {"a": 1}
    assert not [n for n in os.listdir(work) if n.startswith(".stage-")]
    assert not [n for n in os.listdir(os.path.dirname(dest)) if n.startswith(".png-")]
