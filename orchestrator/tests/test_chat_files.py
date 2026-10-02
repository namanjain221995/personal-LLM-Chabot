"""Lasting originals for documents and datasets (2026-10-02,
docs/chat-media/CONTRACT.md §8-9).

THE DEFECT. A document's or dataset's original lived only in the upload
workspace, which the 24 h sweep empties (a dataset's original went at once,
right after extraction). A second device that opened a live chat a day later
was told the file had "expired". Now every finished document or dataset
upload, single-shot or chunked, gets a lasting copy under CHAT_FILES_DIR for
the life of the chat, and the member's and the admin's download fall back to
it.

What is pinned: the copy is made by every rail that finishes a document or
dataset (and by none that finishes a video); the download order is workspace,
lasting copy, video store, 410; byte ranges answer 206/416 (Starlette's own
FileResponse, which the players need); below the free-space floor nothing is
kept and the old behaviour holds, counted; deleting a chat erases its copies
at once; the reaper removes only orphans, only after the grace, and never
follows a symbolic link out of its root.
"""
from __future__ import annotations

import io
import os
import shutil
import time
import uuid
import zipfile

import pytest

from app import db, metrics
from app import uploads as up
from app.config import settings

PDF = b"%PDF-1.4 a contract that must outlive the sweep\n" * 64
CSV = b"region,sales\nnorth,10\nsouth,12\n"
MP4 = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 64


@pytest.fixture(autouse=True)
def _stores(tmp_path, monkeypatch):
    from app.video import pipeline

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path / "ws"))
    monkeypatch.setattr(settings, "dataset_uploads_enabled", True)
    monkeypatch.setattr(settings, "video_analysis_enabled", True)
    monkeypatch.setattr(settings, "document_prewarm_enabled", False)
    monkeypatch.setattr(up, "_last_sweep_at", 0.0)

    async def not_started(_analysis_id):
        return False

    monkeypatch.setattr(pipeline, "ensure_running", not_started)
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture()
def alice(login_client):
    return login_client("alice")


def _chat(client, cid: str) -> str:
    resp = client.post("/history/conversations", json={"id": cid, "title": "Contracts"})
    assert resp.status_code == 200, resp.text
    return cid


def _upload(client, conv: str, name: str, data: bytes, purpose: str, ctype: str) -> str:
    resp = client.post(
        "/uploads",
        files={"file": (name, data, ctype)},
        data={"conversation_id": conv, "purpose": purpose},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["upload_id"]


def _chunked(client, conv: str, name: str, data: bytes, purpose: str) -> str:
    resp = client.post(
        "/uploads/chunked/init",
        data={"conversation_id": conv, "filename": name, "purpose": purpose},
    )
    assert resp.status_code == 200, resp.text
    upload_id = resp.json()["upload_id"]
    half = len(data) // 2
    for index, body in enumerate((data[:half], data[half:])):
        put = client.put(f"/uploads/chunked/{conv}/{upload_id}/part/{index}", content=body)
        assert put.status_code == 200, put.text
    done = client.post(f"/uploads/chunked/{conv}/{upload_id}/complete")
    assert done.status_code == 200, done.text
    return upload_id


def _zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def _sweep(conv: str, upload_id: str) -> None:
    """What the workspace TTL does to one upload."""
    shutil.rmtree(up.upload_root(conv, upload_id))


def _lasting(conv: str, upload_id: str) -> str:
    return up.lasting_path(conv, upload_id)


def _counter(name: str, **labels) -> float:
    return metrics._counters.get(name, {}).get(tuple(sorted(labels.items())), 0.0)


def _age(root: str, seconds: float) -> None:
    """Every entry under `root` (links themselves, never their targets) as
    old as `seconds`."""
    then = time.time() - seconds
    for current, dirs, files in os.walk(root):
        for name in dirs + files:
            os.utime(os.path.join(current, name), (then, then), follow_symlinks=False)
    os.utime(root, (then, then))


# ------------------------------------------------------------ the copy itself --


def test_a_document_upload_keeps_a_lasting_hard_link(alice):
    conv = _chat(alice, "conv-doc")
    upload_id = _upload(alice, conv, "contract.pdf", PDF, "document", "application/pdf")
    lasting = _lasting(conv, upload_id)
    assert lasting == os.path.join(settings.chat_files_dir, conv, upload_id, "original")
    with open(lasting, "rb") as fh:
        assert fh.read() == PDF
    workspace = os.path.join(up.upload_root(conv, upload_id), "_original", "contract.pdf")
    # One filesystem: a link, not a second copy of the bytes.
    assert os.stat(lasting).st_ino == os.stat(workspace).st_ino
    assert _counter("chat_files_lasting_total", purpose="document", result="stored") == 1


def test_a_chunked_document_keeps_a_lasting_copy(alice):
    conv = _chat(alice, "conv-chunked")
    upload_id = _chunked(alice, conv, "big.pdf", PDF, "document")
    with open(_lasting(conv, upload_id), "rb") as fh:
        assert fh.read() == PDF
    assert _counter("chat_files_lasting_total", purpose="document", result="stored") == 1


def test_a_dataset_original_survives_extraction(alice):
    conv = _chat(alice, "conv-data")
    table = _upload(alice, conv, "sales.csv", CSV, "dataset", "text/csv")
    archive_bytes = _zip({"q1.csv": CSV, "q2.csv": CSV})
    archive = _upload(alice, conv, "quarters.zip", archive_bytes, "dataset", "application/zip")
    # The workspace original still goes at extraction (quota), the copy stays.
    for upload_id in (table, archive):
        assert not os.path.exists(os.path.join(up.upload_root(conv, upload_id), "_original"))
    with open(_lasting(conv, table), "rb") as fh:
        assert fh.read() == CSV
    with open(_lasting(conv, archive), "rb") as fh:
        assert fh.read() == archive_bytes
    assert _counter("chat_files_lasting_total", purpose="dataset", result="stored") == 2
    # The archive the person chose downloads, which it never did before.
    got = alice.get(f"/uploads/{conv}/{archive}/file")
    assert got.status_code == 200 and got.content == archive_bytes


def test_a_chunked_dataset_keeps_a_lasting_copy(alice):
    conv = _chat(alice, "conv-chunked-data")
    upload_id = _chunked(alice, conv, "sales.csv", CSV * 20, "dataset")
    with open(_lasting(conv, upload_id), "rb") as fh:
        assert fh.read() == CSV * 20


def test_a_video_gets_no_lasting_copy(alice):
    conv = _chat(alice, "conv-video")
    upload_id = _upload(alice, conv, "stand-up.mp4", MP4, "video", "video/mp4")
    assert not os.path.exists(os.path.join(settings.chat_files_dir, conv, upload_id))
    assert _counter("chat_files_lasting_total", purpose="document", result="stored") == 0


# ------------------------------------------------------------ the download --


def test_after_the_sweep_the_file_route_and_the_admin_download_still_answer(alice, login_client):
    conv = _chat(alice, "conv-swept")
    document = _upload(alice, conv, "contract.pdf", PDF, "document", "application/pdf")
    table = _upload(alice, conv, "sales.csv", CSV, "dataset", "text/csv")
    _sweep(conv, document)
    _sweep(conv, table)

    got = alice.get(f"/uploads/{conv}/{document}/file")
    assert got.status_code == 200 and got.content == PDF
    assert got.headers["content-type"].startswith("application/pdf")
    assert "contract.pdf" in got.headers["content-disposition"]
    assert "original" not in got.headers["content-disposition"]
    got = alice.get(f"/uploads/{conv}/{table}/file")
    assert got.status_code == 200 and got.content == CSV

    admin = login_client("adm", role="admin")
    alice_id = int(db.get_user_by_username("alice")["id"])
    for upload_id, payload, name in ((document, PDF, "contract.pdf"), (table, CSV, "sales.csv")):
        resp = admin.get(f"/admin/api/members/{alice_id}/uploads/{upload_id}/download")
        assert resp.status_code == 200, resp.text
        assert resp.content == payload
        assert name in resp.headers["content-disposition"]
    # Another member still cannot reach it by either route.
    bob = login_client("bob")
    assert bob.get(f"/uploads/{conv}/{document}/file").status_code == 404


def test_the_admin_download_falls_back_to_the_video_store_too(alice, login_client):
    conv = _chat(alice, "conv-admin-video")
    upload_id = _upload(alice, conv, "stand-up.mp4", MP4, "video", "video/mp4")
    _sweep(conv, upload_id)
    admin = login_client("adm", role="admin")
    alice_id = int(db.get_user_by_username("alice")["id"])
    resp = admin.get(f"/admin/api/members/{alice_id}/uploads/{upload_id}/download")
    assert resp.status_code == 200 and resp.content == MP4


def test_with_nothing_left_on_disk_the_routes_answer_as_before(alice, login_client):
    conv = _chat(alice, "conv-gone")
    document = _upload(alice, conv, "contract.pdf", PDF, "document", "application/pdf")
    _sweep(conv, document)
    shutil.rmtree(os.path.dirname(_lasting(conv, document)))
    assert alice.get(f"/uploads/{conv}/{document}/file").status_code == 410
    admin = login_client("adm", role="admin")
    alice_id = int(db.get_user_by_username("alice")["id"])
    resp = admin.get(f"/admin/api/members/{alice_id}/uploads/{document}/download")
    assert resp.status_code == 404 and resp.json()["detail"] == "The file has expired."


def test_a_video_still_answers_from_the_video_store(alice):
    conv = _chat(alice, "conv-video")
    upload_id = _upload(alice, conv, "stand-up.mp4", MP4, "video", "video/mp4")
    _sweep(conv, upload_id)
    got = alice.get(f"/uploads/{conv}/{upload_id}/file")
    assert got.status_code == 200 and got.content == MP4
    assert got.headers["content-type"].startswith("video/mp4")


# ------------------------------------------------------------ byte ranges --


def _assert_ranges(client, url: str, payload: bytes) -> None:
    size = len(payload)
    part = client.get(url, headers={"Range": "bytes=10-19"})
    assert part.status_code == 206, part.text
    assert part.headers["content-range"] == f"bytes 10-19/{size}"
    assert part.headers["content-length"] == "10"
    assert part.content == payload[10:20]
    assert part.headers.get("accept-ranges") == "bytes"
    tail = client.get(url, headers={"Range": f"bytes={size - 5}-"})
    assert tail.status_code == 206 and tail.content == payload[-5:]
    assert tail.headers["content-range"] == f"bytes {size - 5}-{size - 1}/{size}"
    past = client.get(url, headers={"Range": f"bytes={size + 10}-"})
    assert past.status_code == 416
    assert past.headers["content-range"] == f"bytes */{size}"
    whole = client.get(url)
    assert whole.status_code == 200 and whole.content == payload


def test_ranges_on_a_lasting_file(alice):
    conv = _chat(alice, "conv-range")
    upload_id = _upload(alice, conv, "contract.pdf", PDF, "document", "application/pdf")
    _sweep(conv, upload_id)
    _assert_ranges(alice, f"/uploads/{conv}/{upload_id}/file", PDF)


def test_ranges_on_a_video_from_the_video_store(alice):
    conv = _chat(alice, "conv-range-video")
    upload_id = _upload(alice, conv, "stand-up.mp4", MP4, "video", "video/mp4")
    _assert_ranges(alice, f"/uploads/{conv}/{upload_id}/file", MP4)  # workspace copy
    _sweep(conv, upload_id)
    _assert_ranges(alice, f"/uploads/{conv}/{upload_id}/file", MP4)  # analysis store


def test_ranges_on_the_admin_download(alice, login_client):
    conv = _chat(alice, "conv-range-admin")
    upload_id = _upload(alice, conv, "contract.pdf", PDF, "document", "application/pdf")
    _sweep(conv, upload_id)
    admin = login_client("adm", role="admin")
    alice_id = int(db.get_user_by_username("alice")["id"])
    _assert_ranges(admin, f"/admin/api/members/{alice_id}/uploads/{upload_id}/download", PDF)


# ------------------------------------------------------------ the floor --


def test_below_the_floor_nothing_is_kept_and_the_old_behaviour_holds(alice, monkeypatch):
    monkeypatch.setattr(settings, "chat_media_min_free_gib", 1e9)  # no disk is that free
    conv = _chat(alice, "conv-full")
    document = _upload(alice, conv, "contract.pdf", PDF, "document", "application/pdf")
    table = _upload(alice, conv, "sales.csv", CSV, "dataset", "text/csv")
    chunked = _chunked(alice, conv, "big.pdf", PDF, "document")
    for upload_id in (document, table, chunked):
        assert not os.path.exists(os.path.join(settings.chat_files_dir, conv, upload_id))
    assert _counter("chat_files_lasting_total", purpose="document", result="no_space") == 2
    assert _counter("chat_files_lasting_total", purpose="dataset", result="no_space") == 1
    assert _counter("chat_files_lasting_total", purpose="document", result="stored") == 0
    # The upload itself is never refused for it, and serves as before.
    assert alice.get(f"/uploads/{conv}/{document}/file").content == PDF
    _sweep(conv, document)
    _sweep(conv, table)
    assert alice.get(f"/uploads/{conv}/{document}/file").status_code == 410
    assert alice.get(f"/uploads/{conv}/{table}/file").status_code == 410


def test_the_floor_is_measured_on_the_files_root(monkeypatch, tmp_path):
    """CHAT_FILES_DIR's own filesystem (its nearest existing ancestor before
    the first copy), not CHAT_MEDIA_DIR's."""
    import collections

    seen = []
    usage = collections.namedtuple("usage", "total used free")

    def fake_usage(path):
        seen.append(str(path))
        return usage(100, 50, 50 * 1024 ** 3)

    monkeypatch.setattr(settings, "chat_files_dir", str(tmp_path / "not" / "yet" / "files"))
    monkeypatch.setattr(shutil, "disk_usage", fake_usage)
    monkeypatch.setattr(settings, "chat_media_min_free_gib", 49.0)
    assert up.lasting_has_room() is True
    monkeypatch.setattr(settings, "chat_media_min_free_gib", 51.0)
    assert up.lasting_has_room() is False
    assert seen and all(p == str(tmp_path) for p in seen)


def test_a_failed_copy_is_counted_and_never_fails_the_upload(alice, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise PermissionError("read-only for a moment")

    monkeypatch.setattr(up.os, "link", refuse)
    monkeypatch.setattr(up.shutil, "copyfile", refuse)
    conv = _chat(alice, "conv-refused")
    upload_id = _upload(alice, conv, "contract.pdf", PDF, "document", "application/pdf")
    assert not os.path.exists(_lasting(conv, upload_id))
    assert not os.path.exists(_lasting(conv, upload_id) + ".tmp")
    assert _counter("chat_files_lasting_total", purpose="document", result="error") == 1
    assert alice.get(f"/uploads/{conv}/{upload_id}/file").content == PDF


def test_a_refused_link_falls_back_to_a_copy(alice, monkeypatch):
    """Another filesystem (EXDEV) refuses a hard link; the bytes are copied."""
    def cross_device(*_args, **_kwargs):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(up.os, "link", cross_device)
    conv = _chat(alice, "conv-exdev")
    upload_id = _upload(alice, conv, "contract.pdf", PDF, "document", "application/pdf")
    workspace = os.path.join(up.upload_root(conv, upload_id), "_original", "contract.pdf")
    assert os.stat(_lasting(conv, upload_id)).st_ino != os.stat(workspace).st_ino
    with open(_lasting(conv, upload_id), "rb") as fh:
        assert fh.read() == PDF
    assert _counter("chat_files_lasting_total", purpose="document", result="stored") == 1


# ------------------------------------------------------------ deletion --


def test_deleting_a_chat_removes_its_lasting_copies_at_once(alice):
    _chat(alice, "conv-delete")
    _chat(alice, "conv-keep")
    gone = _upload(alice, "conv-delete", "contract.pdf", PDF, "document", "application/pdf")
    kept = _upload(alice, "conv-keep", "sales.csv", CSV, "dataset", "text/csv")
    assert os.path.exists(_lasting("conv-delete", gone))

    assert alice.delete("/history/conversations/conv-delete").status_code == 200
    assert not os.path.exists(os.path.join(settings.chat_files_dir, "conv-delete"))
    # The workspace copy is the same bytes (a hard link): it goes too.
    assert not os.path.exists(os.path.join(settings.workspace_dir, "uploads", "conv-delete"))
    assert os.path.exists(_lasting("conv-keep", kept))
    assert _counter("chat_media_erase_total", store="files", result="ok") == 1


def test_erase_names_nothing_outside_its_roots(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"x")
    assert up.erase_conversation_files("../outside") is False
    assert up.erase_conversation_files("") is False
    assert (outside / "keep").exists()


# ------------------------------------------------------------ the reaper --


def test_the_reaper_removes_orphans_only_after_the_grace(alice):
    conv = _chat(alice, "conv-live")
    live = _upload(alice, conv, "contract.pdf", PDF, "document", "application/pdf")
    root = settings.chat_files_dir
    # A chat whose row is gone (an erase that failed) and, in a live chat, a
    # copy no uploads row names (a crash between the copy and the row).
    orphan_chat = os.path.join(root, "conv-deleted", uuid.uuid4().hex)
    orphan_upload = os.path.join(root, conv, uuid.uuid4().hex)
    for path in (orphan_chat, orphan_upload):
        os.makedirs(path)
        with open(os.path.join(path, "original"), "wb") as fh:
            fh.write(b"bytes")
    # A name this module never makes is never judged.
    stranger = os.path.join(root, "conv-deleted", "notes.txt")
    with open(stranger, "w") as fh:
        fh.write("not ours")

    grace = float(settings.chat_media_orphan_grace_h) * 3600.0
    _age(root, grace - 600)
    assert up.reap_lasting_files() == 0
    assert os.path.exists(orphan_chat) and os.path.exists(orphan_upload)

    _age(root, grace + 600)
    assert up.reap_lasting_files() == 2
    assert not os.path.exists(orphan_chat) and not os.path.exists(orphan_upload)
    assert os.path.exists(_lasting(conv, live))
    assert os.path.exists(stranger)
    assert _counter("chat_media_reaped_total", kind="dir") == 2


def test_the_reaper_takes_a_deleted_accounts_copies(login_client):
    bob = login_client("bob")
    _chat(bob, "conv-bob")
    upload_id = _upload(bob, "conv-bob", "contract.pdf", PDF, "document", "application/pdf")
    with db.connection() as con:
        con.execute("DELETE FROM users WHERE username = 'bob'")  # conversations cascade
    _age(settings.chat_files_dir, float(settings.chat_media_orphan_grace_h) * 3600.0 + 600)
    assert up.reap_lasting_files() == 1
    assert not os.path.exists(os.path.dirname(_lasting("conv-bob", upload_id)))


def test_the_reaper_never_follows_a_symlink_out_of_its_root(tmp_path):
    root = settings.chat_files_dir
    os.makedirs(root)
    outside = tmp_path / "outside"
    # <root>/<chat> is a link to a directory shaped like a chat.
    (outside / "linked" / ("a" * 32)).mkdir(parents=True)
    (outside / "linked" / ("a" * 32) / "original").write_bytes(b"precious")
    os.symlink(outside / "linked", os.path.join(root, "conv-linked"))
    # <root>/<chat>/<upload> is a link to a directory outside.
    (outside / "target").mkdir()
    (outside / "target" / "original").write_bytes(b"precious")
    os.makedirs(os.path.join(root, "conv-real"))
    os.symlink(outside / "target", os.path.join(root, "conv-real", "b" * 32))
    # A real orphan directory holding a link to a file outside.
    (outside / "file").write_bytes(b"precious")
    real = os.path.join(root, "conv-real", "c" * 32)
    os.makedirs(real)
    os.symlink(outside / "file", os.path.join(real, "original"))

    _age(root, float(settings.chat_media_orphan_grace_h) * 3600.0 + 600)
    assert up.reap_lasting_files() == 1
    assert not os.path.exists(real)
    assert (outside / "linked" / ("a" * 32) / "original").read_bytes() == b"precious"
    assert (outside / "target" / "original").read_bytes() == b"precious"
    assert (outside / "file").read_bytes() == b"precious"
    assert os.path.islink(os.path.join(root, "conv-linked"))
    assert os.path.islink(os.path.join(root, "conv-real", "b" * 32))


def test_a_database_error_stops_the_pass_and_removes_nothing(monkeypatch):
    orphan = os.path.join(settings.chat_files_dir, "conv-x", "d" * 32)
    os.makedirs(orphan)
    _age(settings.chat_files_dir, float(settings.chat_media_orphan_grace_h) * 3600.0 + 600)

    def down(_conversation_id):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(up, "_live_upload_ids", down)
    monkeypatch.setattr(up, "_LASTING_REAP_DUE", 0.0)
    assert up.maybe_reap_lasting_files() == 0  # logged, never raised
    assert os.path.exists(orphan)


def test_the_session_sweep_runs_the_reaper_at_most_once_per_interval(monkeypatch):
    """main.py's ten-minute upload-session sweep is what runs the reaper;
    it throttles itself to CHAT_MEDIA_REAP_INTERVAL_S."""
    grace = float(settings.chat_media_orphan_grace_h) * 3600.0 + 600
    monkeypatch.setattr(up, "_LASTING_REAP_DUE", 0.0)
    first = os.path.join(settings.chat_files_dir, "conv-gone", "e" * 32)
    os.makedirs(first)
    _age(settings.chat_files_dir, grace)
    up.sweep_expired_upload_sessions()
    assert not os.path.exists(first)

    second = os.path.join(settings.chat_files_dir, "conv-gone-too", "f" * 32)
    os.makedirs(second)
    _age(settings.chat_files_dir, grace)
    up.sweep_expired_upload_sessions()
    assert os.path.exists(second)  # inside the interval: no second pass
