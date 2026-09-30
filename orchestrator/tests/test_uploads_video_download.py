"""A video's download outlives the workspace sweep (2026-09-30, My files).

GET /uploads/{conversation}/{upload}/file looked only in the upload's
workspace directory. A video's bytes are ALSO hard-linked into the analysis
store (VIDEO_DATA_DIR/<sha256>/source.<ext>, video/store.adopt_source), where
they stay while any chat links them — so 24 hours after the upload the route
answered 410 "expired" for a file that was still on disk and still being
answered about. My files lists such a video as downloadable, and this is the
route its Download link uses.

The fallback runs only AFTER the owner check and the (conversation, upload)
scoping the route already did, only for rows the video rail wrote, and serves
the file under the person's own filename, never `source.mp4`.
"""
from __future__ import annotations

import hashlib
import shutil

import pytest

from app import db
from app.config import settings

MP4 = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 64


@pytest.fixture(autouse=True)
def _stores(tmp_path, monkeypatch):
    from app.video import pipeline

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path / "ws"))
    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    monkeypatch.setattr(settings, "dataset_uploads_enabled", True)
    monkeypatch.setattr(settings, "video_analysis_enabled", True)
    monkeypatch.setattr(settings, "document_prewarm_enabled", False)

    async def not_started(_analysis_id):
        return False

    monkeypatch.setattr(pipeline, "ensure_running", not_started)


@pytest.fixture()
def alice(login_client):
    return login_client("alice")


def _chat(client, cid: str) -> str:
    resp = client.post("/history/conversations", json={"id": cid, "title": "Stand-up"})
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


def _sweep(conv: str, upload_id: str) -> None:
    """What the workspace TTL does to one upload."""
    from app.uploads import upload_root

    shutil.rmtree(upload_root(conv, upload_id))


def test_a_swept_video_still_downloads_from_the_analysis_store(alice):
    conv = _chat(alice, "conv-video")
    upload_id = _upload(alice, conv, "Team stand-up.mp4", MP4, "video", "video/mp4")
    _sweep(conv, upload_id)

    resp = alice.get(f"/uploads/{conv}/{upload_id}/file")
    assert resp.status_code == 200, resp.text
    assert hashlib.sha256(resp.content).hexdigest() == hashlib.sha256(MP4).hexdigest()
    disposition = resp.headers["content-disposition"]
    assert "Team%20stand-up.mp4" in disposition or "Team stand-up.mp4" in disposition
    assert "source." not in disposition
    assert resp.headers["content-type"].startswith("video/mp4")


def test_an_audio_file_on_the_video_rail_falls_back_too(alice):
    conv = _chat(alice, "conv-audio")
    data = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 512
    upload_id = _upload(alice, conv, "call.mp3", data, "video", "audio/mpeg")
    _sweep(conv, upload_id)
    resp = alice.get(f"/uploads/{conv}/{upload_id}/file")
    assert resp.status_code == 200, resp.text
    assert resp.content == data
    assert resp.headers["content-type"].startswith("audio/mpeg")


def test_another_person_gets_404_not_the_fallback(alice, login_client):
    conv = _chat(alice, "conv-video")
    upload_id = _upload(alice, conv, "private.mp4", MP4, "video", "video/mp4")
    _sweep(conv, upload_id)
    bob = login_client("bob")
    assert bob.get(f"/uploads/{conv}/{upload_id}/file").status_code == 404


def test_a_video_gone_from_both_stores_is_410(alice):
    from app.video import store

    conv = _chat(alice, "conv-video")
    upload_id = _upload(alice, conv, "gone.mp4", MP4, "video", "video/mp4")
    _sweep(conv, upload_id)
    store.remove_analysis(db.get_video_by_upload(conv, upload_id)["content_hash"])
    assert alice.get(f"/uploads/{conv}/{upload_id}/file").status_code == 410


def test_document_and_dataset_downloads_are_unchanged(alice):
    conv = _chat(alice, "conv-docs")
    pdf = b"%PDF-1.4 unchanged\n" * 40
    document = _upload(alice, conv, "contract.pdf", pdf, "document", "application/pdf")
    table = _upload(alice, conv, "sales.csv", b"a,b\n1,2\n", "dataset", "text/csv")
    first = alice.get(f"/uploads/{conv}/{document}/file")
    assert first.status_code == 200 and first.content == pdf
    second = alice.get(f"/uploads/{conv}/{table}/file")
    assert second.status_code == 200 and second.content == b"a,b\n1,2\n"
    _sweep(conv, document)
    _sweep(conv, table)
    assert alice.get(f"/uploads/{conv}/{document}/file").status_code == 410
    assert alice.get(f"/uploads/{conv}/{table}/file").status_code == 410


def test_a_non_video_row_never_reads_the_analysis_store(alice):
    """Belt and braces: only a row the video rail wrote may fall back. A
    document that somehow shares a conversation with a video must still 410
    once swept, not hand out the video's bytes."""
    import os

    from app.video import store

    conv = _chat(alice, "conv-mixed")
    document = _upload(alice, conv, "notes.pdf", b"%PDF-1.4 n\n" * 30, "document", "application/pdf")
    content_hash = "b" * 64
    os.makedirs(store.analysis_dir(content_hash))
    with open(os.path.join(store.analysis_dir(content_hash), "source.mp4"), "wb") as fh:
        fh.write(MP4)
    with db.connection() as con:
        # An analysis linked to the DOCUMENT's upload id, bytes on disk.
        analysis = con.execute(
            "INSERT INTO video_analyses (content_hash, bytes, created_at, updated_at) "
            "VALUES (%s, %s, now(), now()) RETURNING id",
            (content_hash, len(MP4)),
        ).fetchone()
        con.execute(
            "INSERT INTO video_attachments (analysis_id, conversation_id, user_id, upload_id, filename, created_at) "
            "VALUES (%s, %s, %s, %s, 'notes.pdf', now())",
            (analysis["id"], conv, int(db.get_user_by_username("alice")["id"]), document),
        )
    assert store.source_path(content_hash)
    _sweep(conv, document)
    assert alice.get(f"/uploads/{conv}/{document}/file").status_code == 410
