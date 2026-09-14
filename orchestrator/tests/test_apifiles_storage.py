"""`apifiles/ids.py`, `storage.py`, `limits.py`: the shapes, the fence, the watermark."""
from __future__ import annotations

import os

import pytest
from starlette.testclient import TestClient

from app.apifiles import ids, limits, storage
from app.config import settings
from tests.apifiles_test_support import (  # noqa: F401 - fixture by name
    MULTIPART,
    StubAuth,
    build_app,
    headers_for,
    isolated_app_db,
    make_caller,
    multipart_body,
)

PROJECT = "proj_" + "a" * 24
SHA = "b" * 64


def test_new_ids_have_the_openai_spellings_and_match_their_regexes():
    assert ids.is_file_id(ids.new_file_id()) and ids.new_file_id().startswith("file-")
    assert ids.is_upload_id(ids.new_upload_id()) and ids.new_upload_id().startswith("upload_")
    assert ids.is_part_id(ids.new_part_id())
    assert ids.is_blob_id(ids.new_blob_id())
    assert len({ids.new_file_id() for _ in range(1000)}) == 1000


@pytest.mark.parametrize("value", ["file-" + "A" * 24, "file_" + "a" * 24, "file-" + "a" * 23, "file-../../etc", None, 7, "file-" + "a" * 24 + "\n"])
def test_a_malformed_file_id_is_refused(value):
    assert not ids.is_file_id(value)


@pytest.mark.parametrize(
    "call",
    [
        lambda: storage.blob_dir("../proj", SHA),
        lambda: storage.blob_dir(PROJECT, "../" + SHA[3:]),
        lambda: storage.blob_dir(PROJECT, SHA.upper()),
        lambda: storage.upload_dir("upload_../../x"),
        lambda: storage.part_path("upload_" + "c" * 24, -1),
        lambda: storage.inline_dir("req_../x"),
        lambda: storage.api_video_hash("proj_x", SHA),
        lambda: storage.assembled_tmp_path("upload_" + "c" * 24, "../x"),
    ],
)
def test_no_path_is_built_from_a_segment_that_did_not_match_its_regex(call):
    with pytest.raises(ValueError):
        call()


def test_the_layout_nests_blobs_by_project_and_digest_under_the_files_root(tmp_path):
    root = os.environ["PUBLIC_API_FILES_DIR"]
    assert storage.original_path(PROJECT, SHA) == os.path.join(root, PROJECT, SHA, "original")
    assert storage.part_path("upload_" + "c" * 24, 12).endswith(os.path.join("_uploads", "upload_" + "c" * 24, "parts", "12"))


def test_remove_tree_deletes_inside_the_fence(tmp_path):
    target = storage.blob_dir(PROJECT, SHA)
    os.makedirs(os.path.join(target, "derived"))
    open(os.path.join(target, "original"), "wb").close()
    assert storage.remove_tree(target) is True
    assert not os.path.exists(target)
    assert storage.remove_tree(target) is False


def test_remove_tree_refuses_the_root_itself_and_anything_outside(tmp_path):
    storage.ensure_dirs()
    outside = tmp_path / "precious"
    outside.mkdir()
    with pytest.raises(ValueError):
        storage.remove_tree(storage.root())
    with pytest.raises(ValueError):
        storage.remove_tree(str(outside))
    with pytest.raises(ValueError):
        storage.remove_tree(os.path.join(storage.root(), "..", "precious"))
    assert outside.exists()


def test_remove_tree_refuses_a_symlink_that_escapes_the_fence(tmp_path):
    storage.ensure_dirs()
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep").write_text("x")
    link = os.path.join(storage.root(), PROJECT)
    os.symlink(str(outside), link)
    with pytest.raises(ValueError):
        storage.remove_tree(link)
    assert (outside / "keep").exists()


def test_the_video_tree_is_inside_the_fence_too(tmp_path):
    video_dir = os.path.join(settings.video_data_dir, "a" * 64)
    os.makedirs(video_dir)
    assert storage.remove_tree(video_dir) is True


def test_the_api_video_hash_differs_per_project_and_from_the_bytes_digest():
    other = "proj_" + "d" * 24
    assert storage.api_video_hash(PROJECT, SHA) != storage.api_video_hash(other, SHA)
    assert storage.api_video_hash(PROJECT, SHA) != SHA
    assert ids.is_sha256(storage.api_video_hash(PROJECT, SHA))


def test_require_free_refuses_when_the_write_would_cross_the_watermark(monkeypatch):
    monkeypatch.setattr(storage, "free_bytes", lambda: limits.min_free_bytes() + 1000)
    storage.require_free(1000)
    with pytest.raises(storage.StorageUnavailable) as caught:
        storage.require_free(1001)
    assert caught.value.retry_after == 60


def test_limits_read_settings_first_then_the_environment_with_blank_meaning_default(monkeypatch):
    monkeypatch.delenv("PUBLIC_API_FILES_PART_MAX_BYTES", raising=False)
    assert limits.part_max_bytes() == 67_108_864
    monkeypatch.setenv("PUBLIC_API_FILES_PART_MAX_BYTES", "  ")
    assert limits.part_max_bytes() == 67_108_864
    monkeypatch.setenv("PUBLIC_API_FILES_PART_MAX_BYTES", "1024")
    assert limits.part_max_bytes() == 1024
    monkeypatch.setattr(settings, "public_api_files_part_max_bytes", 2048, raising=False)
    assert limits.part_max_bytes() == 2048
    monkeypatch.setattr(settings, "public_api_files_part_max_bytes", None, raising=False)
    monkeypatch.setenv("PUBLIC_API_FILES_PART_MAX_BYTES", "lots")
    with pytest.raises(ValueError):
        limits.part_max_bytes()


def test_the_design_defaults_hold(monkeypatch):
    monkeypatch.delenv("PUBLIC_API_FILES_MIN_FREE_GIB", raising=False)  # the fixture's test floor is not the default
    assert limits.max_body_bytes() == 68_157_440
    assert limits.upload_max_bytes() == 107_374_182_400
    assert limits.max_parts() == 10_000
    assert limits.min_free_bytes() == 250 * 1024 ** 3
    assert limits.upload_idle_ttl_s() == 86_400 and limits.upload_max_ttl_s() == 604_800


def test_a_derived_writing_stage_projects_more_than_nothing_for_every_kind():
    for kind in ("pdf", "document", "presentation", "spreadsheet", "tabular", "text", "html", "image", "audio", "video", "unknown"):
        assert storage.projected_derived_bytes(kind, 10 * 1024 * 1024) > 0
    assert storage.projected_derived_bytes("document", 1024) >= 34 * 1024, "a DOCX expanded 34.8x (measured)"


def _refused(response):
    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "storage_unavailable" and body["type"] == "api_error"
    assert response.headers["retry-after"] == "60"
    assert response.headers["x-should-retry"] == "false"


def test_below_the_watermark_new_bytes_are_refused_with_503_and_reads_continue(monkeypatch):
    caller = make_caller()
    client = TestClient(build_app(StubAuth(caller)))
    headers = headers_for(caller)
    created = client.post("/v1/files", headers={**headers, "Content-Type": MULTIPART},
                          content=multipart_body([("purpose", "user_data"), ("file", "a.txt", b"hello", "text/plain")]))
    assert created.status_code == 200
    monkeypatch.setattr(storage, "free_bytes", lambda: limits.min_free_bytes() - 1)
    _refused(client.post("/v1/files", headers={**headers, "Content-Type": MULTIPART},
                         content=multipart_body([("purpose", "user_data"), ("file", "b.txt", b"world", "text/plain")])))
    _refused(client.post("/v1/uploads", headers=headers,
                         json={"bytes": 10, "filename": "a.bin", "mime_type": "application/octet-stream", "purpose": "user_data"}))
    assert client.get(f"/v1/files/{created.json()['id']}", headers=headers).status_code == 200
    assert client.get(f"/v1/files/{created.json()['id']}/content", headers=headers).content == b"hello"
    assert os.listdir(os.path.join(storage.root(), storage.SINGLE_DIR)) == []


def test_an_upload_is_refused_when_its_parts_and_assembled_copy_would_cross_the_watermark(monkeypatch):
    caller = make_caller()
    client = TestClient(build_app(StubAuth(caller)))
    monkeypatch.setattr(storage, "free_bytes", lambda: limits.min_free_bytes() + 1_000_000)
    body = {"bytes": 500_000, "filename": "a.bin", "mime_type": "application/octet-stream", "purpose": "user_data"}
    assert client.post("/v1/uploads", headers=headers_for(caller), json=body).status_code == 200
    _refused(client.post("/v1/uploads", headers=headers_for(caller), json={**body, "bytes": 500_001}))


# --------------------------------------------------- review fixes 2026-09-13 --


def test_a_blob_directory_moves_into_the_trash_and_only_trash_entries_can_be_removed_that_way(tmp_path):
    from app.apifiles import ids as ids_mod

    blob = {"id": ids_mod.new_blob_id(), "project_id": PROJECT, "sha256": SHA}
    os.makedirs(storage.derived_dir(PROJECT, SHA))
    with open(storage.original_path(PROJECT, SHA), "wb") as fh:
        fh.write(b"bytes")
    moved = storage.move_blob_dir_aside(blob)
    assert moved is not None and os.path.dirname(moved) == storage.trash_root()
    assert os.path.basename(moved).startswith(blob["id"] + ".")
    assert not os.path.exists(storage.blob_dir(PROJECT, SHA))
    assert storage.move_blob_dir_aside(blob) is None, "already gone is not an error"
    with pytest.raises(ValueError):
        storage.remove_trash(storage.upload_dir(ids_mod.new_upload_id()))
    assert storage.remove_trash(moved) is True and not os.path.exists(moved)
    assert storage.remove_trash(moved) is False


def test_moving_a_blob_directory_aside_refuses_a_project_directory_that_escapes_the_tree(tmp_path):
    from app.apifiles import ids as ids_mod

    outside = tmp_path / "precious"
    (outside / SHA).mkdir(parents=True)
    os.makedirs(storage.root(), exist_ok=True)
    os.symlink(str(outside), os.path.join(storage.root(), PROJECT))
    with pytest.raises(ValueError):
        storage.move_blob_dir_aside({"id": ids_mod.new_blob_id(), "project_id": PROJECT, "sha256": SHA})
    assert (outside / SHA).is_dir()


def test_the_upload_sweep_clears_trash_a_crash_left_behind_after_a_minute(tmp_path):
    import time

    from app.apifiles import uploads_sweep

    storage.ensure_dirs()
    entry = os.path.join(storage.trash_root(), "blob_leftover.0123456789abcdef")
    os.makedirs(os.path.join(entry, "derived"))
    report = uploads_sweep.SweepReport()
    uploads_sweep._sweep_trash(report, time.time())
    assert os.path.exists(entry), "a rename a moment ago belongs to the purge that made it"
    uploads_sweep._sweep_trash(report, time.time() + uploads_sweep.TRASH_GRACE_S + 1)
    assert not os.path.exists(entry) and report.leftovers_removed == 1
