"""DELETE removes bytes and derived data; expiry, tombstones, reconciliation
(design §2.7, §7.3, acceptance A-5 at unit scale).

Real Postgres (the private test database, four-table cleanup per test as in
test_apifiles_jobs.py), real directories under a tmp PUBLIC_API_FILES_DIR and
VIDEO_DATA_DIR, the real `schema.delete_api_file` transaction and the real
`storage.remove_tree` fence. A document blob is processed for real first, so
the tree being removed is exactly what processing leaves behind.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List

import pytest

from app import db
from app.apifiles import ids, jobs, retention, schema, storage
from tests.test_apifiles_jobs import (  # noqa: F401 — fixtures are used by name
    ambient_identity,
    blob,
    docx_bytes,
    files_root,
    files_schema,
    insert_blob,
    isolated_app_db,
    new_project,
    runner,
)


def run(coro):
    return asyncio.run(coro)


def live_file_ids(blob_id: str) -> List[str]:
    return [f["id"] for f in schema.live_files_for_blob(blob_id)]


def add_file(tenant: Dict[str, str], blob_id: str, *, expires_in_s: int = None) -> str:
    file_id = ids.new_file_id()
    with db.connection() as con:
        con.execute(
            "INSERT INTO api_files (id, project_id, workspace_id, blob_id, filename, purpose, bytes, expires_at) "
            "VALUES (%s, %s, %s, %s, 'second.docx', 'user_data', 1, "
            "        CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(secs => %s::int) END)",
            (file_id, tenant["project_id"], tenant["workspace_id"], blob_id, expires_in_s, expires_in_s),
        )
    return file_id


def tree(path: str) -> List[str]:
    out = []
    for base, _dirs, files in os.walk(path):
        out.extend(os.path.relpath(os.path.join(base, f), path) for f in files)
    return sorted(out)


def processed_document(tmp_path) -> Dict[str, Any]:
    os.makedirs(tmp_path, exist_ok=True)
    tenant = new_project()
    row = insert_blob(tenant, docx_bytes(tmp_path), filename="report.docx")
    assert [o.outcome for o in run(runner().run_once())] == ["processed"]
    return {"tenant": tenant, "blob": blob(row["id"]), "file_id": live_file_ids(row["id"])[0]}


# ================================================================== purge ==


def test_delete_of_the_last_file_removes_original_derived_vectors_and_the_blob_row(tmp_path):
    doc = processed_document(tmp_path)
    tenant, row = doc["tenant"], doc["blob"]
    blob_dir = storage.blob_dir(tenant["project_id"], row["sha256"])
    before = tree(blob_dir)
    assert {"original", "derived/vectors.f32", "derived/chunks.jsonl", "derived/pages.jsonl"} <= set(before)
    tomb, deleting = schema.delete_api_file(tenant["project_id"], doc["file_id"])
    assert deleting is not None and deleting["status"] == "deleting"
    result = run(retention.purge_blob(row["id"]))
    assert result.removed_dir and result.removed_row
    assert not os.path.exists(blob_dir)
    assert blob(row["id"]) is None
    with db.connection() as con:
        tombstone = con.execute("SELECT * FROM api_files WHERE id = %s", (doc["file_id"],)).fetchone()
    assert tombstone["deleted_at"] is not None and tombstone["blob_id"] is None
    assert tombstone["filename"] == "report.docx"
    assert schema.get_api_file(tenant["project_id"], doc["file_id"]) is None
    again = run(retention.purge_blob(row["id"]))  # idempotent
    assert again.skipped == "gone"


def test_bytes_shared_by_another_live_file_of_the_project_survive_its_sibling_being_deleted(tmp_path):
    doc = processed_document(tmp_path)
    tenant, row = doc["tenant"], doc["blob"]
    second = add_file(tenant, row["id"])
    _tomb, deleting = schema.delete_api_file(tenant["project_id"], doc["file_id"])
    assert deleting is None
    result = run(retention.purge_blob(row["id"]))
    assert result.skipped == "not deleting" and not result.removed_dir
    assert os.path.exists(storage.original_path(tenant["project_id"], row["sha256"]))
    assert schema.get_api_file(tenant["project_id"], second)["blob_status"] == "processed"
    _tomb, deleting = schema.delete_api_file(tenant["project_id"], second)
    assert deleting is not None
    assert run(retention.purge_blob(row["id"])).removed_row
    assert not os.path.exists(storage.blob_dir(tenant["project_id"], row["sha256"]))


def test_a_media_blob_purge_also_removes_its_analysis_directory_and_row(tmp_path):
    tenant = new_project()
    row = insert_blob(tenant, b"RIFF\x00\x00\x00\x00WAVEfake audio bytes", kind="audio", lane="media", status="processed")
    content_hash = storage.api_video_hash(tenant["project_id"], row["sha256"])
    analysis = db.upsert_video_analysis(content_hash, 20, "audio/wav", "media.wav")
    from app.video import store

    analysis_dir = store.analysis_dir(content_hash)
    os.makedirs(os.path.join(analysis_dir, "artifacts"))
    for name in ("audio.wav", "transcript.json", "artifacts/transcript.vtt"):
        with open(os.path.join(analysis_dir, name), "w") as fh:
            fh.write("x")
    schema.update_api_file_blob(row["id"], video_analysis_id=int(analysis["id"]))
    file_id = live_file_ids(row["id"])[0]
    schema.delete_api_file(tenant["project_id"], file_id)
    result = run(retention.purge_blob(row["id"]))
    assert result.removed_analysis and result.removed_dir and result.removed_row
    assert not os.path.exists(analysis_dir)
    assert db.get_video_analysis(int(analysis["id"])) is None


def test_a_purge_cancels_the_blobs_running_job_before_removing_its_directory(tmp_path):
    tenant = new_project()
    row = insert_blob(tenant, b"long running text\n" * 50)
    started = asyncio.Event()
    finished: List[str] = []

    async def slow(ctx):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            # A cancelled stage still sees its directory: removal waits for it.
            finished.append("present" if os.path.isdir(ctx.derived_dir) else "gone")

    async def scenario():
        r = runner(stages={"text": slow})
        jobs._runners["cpu"] = r
        try:
            tasks = await r._claim_and_spawn()
            await asyncio.wait_for(started.wait(), timeout=20)
            schema.delete_api_file(tenant["project_id"], live_file_ids(row["id"])[0])
            result = await retention.purge_blob(row["id"])
            assert tasks[0].done()  # the job was gone before purge_blob returned
            await asyncio.gather(tasks[0], return_exceptions=True)
            return result, r.outcomes[-1]
        finally:
            jobs._runners.pop("cpu", None)

    result, outcome = run(scenario())
    assert finished == ["present"]
    assert outcome.outcome == "stopped" and outcome.error_code == "deleting"
    assert result.removed_dir and result.removed_row


def test_the_purge_refuses_a_path_outside_the_files_and_video_trees(tmp_path, monkeypatch):
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    with pytest.raises(ValueError):
        storage.remove_tree(str(outside))
    link_parent = os.path.join(storage.root(), "proj_" + "e" * 24)
    os.makedirs(link_parent)
    link = os.path.join(link_parent, "e" * 64)
    os.symlink(str(outside), link)
    with pytest.raises(ValueError):
        storage.remove_tree(link + "/")
    assert (outside / "keep.txt").exists()


# ========================================================== the loop pass ==


def test_an_expired_file_goes_through_the_delete_path_and_its_bytes_are_purged(tmp_path):
    doc = processed_document(tmp_path)
    tenant, row = doc["tenant"], doc["blob"]
    with db.connection() as con:
        con.execute("UPDATE api_files SET expires_at = now() - interval '1 minute' WHERE id = %s", (doc["file_id"],))
    report = run(retention.run_pass())
    assert report.expired_files == [doc["file_id"]] and report.purged == [row["id"]]
    assert not os.path.exists(storage.blob_dir(tenant["project_id"], row["sha256"]))
    with db.connection() as con:
        tomb = con.execute("SELECT deleted_at, blob_id FROM api_files WHERE id = %s", (doc["file_id"],)).fetchone()
    assert tomb["deleted_at"] is not None and tomb["blob_id"] is None


def test_a_purge_interrupted_by_a_crash_is_finished_by_the_next_pass(tmp_path):
    doc = processed_document(tmp_path)
    tenant, row = doc["tenant"], doc["blob"]
    schema.delete_api_file(tenant["project_id"], doc["file_id"])  # committed; the kick never ran
    report = run(retention.run_pass())
    assert report.purged == [row["id"]]
    assert blob(row["id"]) is None


def _deleting_blob(tenant: Dict[str, str], payload: bytes) -> Dict[str, Any]:
    row = insert_blob(tenant, payload)
    os.makedirs(storage.derived_dir(tenant["project_id"], row["sha256"]), exist_ok=True)
    with open(os.path.join(storage.derived_dir(tenant["project_id"], row["sha256"]), "pages.jsonl"), "w") as fh:
        fh.write('{"page": 1, "text": "x"}\n')
    _tomb, deleting = schema.delete_api_file(tenant["project_id"], live_file_ids(row["id"])[0])
    return deleting


def test_one_blob_whose_purge_raises_is_requeued_and_every_later_purge_and_expiry_still_runs(tmp_path):
    # Review finding, 2026-09-13: the first failing `deleting` row ended the
    # step and stayed first by updated_at, so nothing behind it was ever freed.
    stuck_tenant, ok_tenant = new_project("stuck"), new_project("ok")
    stuck = _deleting_blob(stuck_tenant, b"cannot be moved aside")
    time.sleep(0.05)
    fine = _deleting_blob(ok_tenant, b"purged normally")
    expiring = insert_blob(ok_tenant, b"expires behind the stuck one")
    with db.connection() as con:
        con.execute("UPDATE api_files SET expires_at = now() - interval '1 minute' WHERE blob_id = %s", (expiring["id"],))
    project_dir = os.path.join(storage.root(), stuck_tenant["project_id"])
    os.chmod(project_dir, 0o500)  # the rename out of it is refused (EACCES)
    try:
        before = blob(stuck["id"])["updated_at"]
        report = run(retention.run_pass())
        assert report.failed == [stuck["id"]]
        assert sorted(report.purged) == sorted([fine["id"], expiring["id"]])
        assert blob(fine["id"]) is None and blob(expiring["id"]) is None
        after = blob(stuck["id"])
        assert after["status"] == "deleting" and after["updated_at"] > before  # moved behind the others
        assert os.path.exists(storage.blob_dir(stuck_tenant["project_id"], stuck["sha256"]))
    finally:
        os.chmod(project_dir, 0o750)
    report = run(retention.run_pass())
    assert report.purged == [stuck["id"]] and report.failed == []
    assert not os.path.exists(storage.blob_dir(stuck_tenant["project_id"], stuck["sha256"]))


def test_a_deletes_kick_and_the_periodic_pass_purging_one_blob_at_once_share_a_single_purge(tmp_path):
    tenant = new_project()
    deleting = _deleting_blob(tenant, b"two purges race")
    calls: List[str] = []
    real_retire = retention._retire

    def counting(blob_id):
        calls.append(blob_id)
        return real_retire(blob_id)

    async def scenario():
        retention._retire = counting  # type: ignore[assignment]
        try:
            return await asyncio.gather(retention.purge_blob(deleting["id"]), retention.purge_blob(deleting))
        finally:
            retention._retire = real_retire  # type: ignore[assignment]

    first, second = run(scenario())
    assert calls == [deleting["id"]]
    assert first is second and first.removed_row and first.removed_dir
    assert not os.path.exists(storage.blob_dir(tenant["project_id"], deleting["sha256"]))
    assert not os.listdir(storage.trash_root())


def test_tombstones_are_kept_for_the_retention_window_then_removed(tmp_path, monkeypatch):
    doc = processed_document(tmp_path)
    tenant = doc["tenant"]
    schema.delete_api_file(tenant["project_id"], doc["file_id"])
    with db.connection() as con:
        con.execute("UPDATE api_files SET deleted_at = now() - interval '10 days' WHERE id = %s", (doc["file_id"],))
    assert run(retention.run_pass()).tombstones_removed == 0
    with db.connection() as con:
        con.execute("UPDATE api_files SET deleted_at = now() - interval '31 days' WHERE id = %s", (doc["file_id"],))
    assert run(retention.run_pass()).tombstones_removed == 1
    with db.connection() as con:
        assert con.execute("SELECT count(*) AS n FROM api_files WHERE id = %s", (doc["file_id"],)).fetchone()["n"] == 0
    # The acceptance drill (A-5 step 7): with the window forced to 0 days a
    # fresh tombstone goes on the next pass.
    doc2 = processed_document(tmp_path / "second")
    schema.delete_api_file(doc2["tenant"]["project_id"], doc2["file_id"])
    monkeypatch.setenv("PUBLIC_API_FILES_TOMBSTONE_DAYS", "0")
    assert run(retention.run_pass()).tombstones_removed == 1


def _age(path: str, seconds: float) -> None:
    moment = time.time() - seconds
    os.utime(path, (moment, moment), follow_symlinks=False)


def test_orphan_blob_directories_older_than_an_hour_are_reconciled_and_young_or_owned_ones_kept(tmp_path):
    doc = processed_document(tmp_path)
    tenant, row = doc["tenant"], doc["blob"]
    root = storage.root()
    old_orphan = os.path.join(root, "proj_" + "a" * 24, "a" * 64)
    young_orphan = os.path.join(root, "proj_" + "b" * 24, "b" * 64)
    not_a_blob = os.path.join(root, "proj_" + "c" * 24, "not-a-digest")
    for path in (old_orphan, young_orphan, not_a_blob):
        os.makedirs(os.path.join(path, "derived"))
        with open(os.path.join(path, "original"), "w") as fh:
            fh.write("bytes")
    _age(old_orphan, 7200)
    owned = storage.blob_dir(tenant["project_id"], row["sha256"])
    _age(owned, 7200)
    report = run(retention.run_pass())
    assert report.orphan_dirs_removed == 1
    assert not os.path.exists(old_orphan)
    assert os.path.exists(young_orphan) and os.path.exists(not_a_blob) and os.path.exists(owned)


def test_crash_leftover_renders_and_temporaries_older_than_a_day_are_removed_from_live_blobs(tmp_path):
    doc = processed_document(tmp_path)
    tenant, row = doc["tenant"], doc["blob"]
    derived = storage.derived_dir(tenant["project_id"], row["sha256"])
    os.makedirs(os.path.join(derived, "renders"))
    stale_render = os.path.join(derived, "renders", "7.png")
    fresh_render = os.path.join(derived, "renders", "8.png")
    stale_tmp = os.path.join(derived, "pages.jsonl.abc.tmp")
    for path in (stale_render, fresh_render, stale_tmp):
        with open(path, "w") as fh:
            fh.write("x")
    _age(stale_render, 90_000)
    _age(stale_tmp, 90_000)
    report = run(retention.run_pass())
    assert report.leftovers_removed == 2
    assert os.path.exists(fresh_render) and not os.path.exists(stale_render) and not os.path.exists(stale_tmp)
    assert os.path.exists(os.path.join(derived, "pages.jsonl"))


def test_api_lane_analyses_that_no_blob_references_are_removed_after_an_hour(tmp_path):
    from app.video import store

    content_hash = "d" * 64
    with db.connection() as con:  # video_analyses is not in this suite's per-test cleanup
        con.execute("DELETE FROM video_analyses WHERE content_hash IN (%s, %s)", (content_hash, "f" * 64))
    analysis = db.upsert_video_analysis(content_hash, 10, "video/mp4", "media.mp4")
    with db.connection() as con:
        con.execute(
            "UPDATE video_analyses SET lane = 'api', created_at = now() - interval '2 hours' WHERE id = %s",
            (analysis["id"],),
        )
    chat = db.upsert_video_analysis("f" * 64, 10, "video/mp4", "chat.mp4")
    with db.connection() as con:
        con.execute("UPDATE video_analyses SET created_at = now() - interval '2 hours' WHERE id = %s", (chat["id"],))
    os.makedirs(store.analysis_dir(content_hash))
    report = run(retention.run_pass())
    assert report.orphan_analyses_removed == 1
    assert db.get_video_analysis(int(analysis["id"])) is None
    assert db.get_video_analysis(int(chat["id"])) is not None  # the chat lane is never reaped here
    assert not os.path.exists(store.analysis_dir(content_hash))


def test_the_purge_accepts_the_blob_row_the_routes_hold_and_re_reads_it_before_acting(tmp_path):
    doc = processed_document(tmp_path)
    tenant, row = doc["tenant"], doc["blob"]
    stale = dict(row, status="deleting")  # a row that SAYS deleting, for a blob that is live
    assert run(retention.purge_blob(stale)).skipped == "not deleting"
    assert os.path.exists(storage.blob_dir(tenant["project_id"], row["sha256"]))
    _tomb, deleting = schema.delete_api_file(tenant["project_id"], doc["file_id"])
    assert run(retention.purge_blob(deleting)).removed_row
