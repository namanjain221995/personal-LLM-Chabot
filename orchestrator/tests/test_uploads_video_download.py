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


def _partial_only(conv: str, upload_id: str) -> str:
    """The analysis store as a crash in the middle of adopt_source's copy
    leaves it (VIDEO_DATA_DIR on another filesystem than the workspace): only
    source.<ext>.part, holding a third of the bytes. Returns the hash."""
    from pathlib import Path

    from app.video import store

    content_hash = db.get_video_by_upload(conv, upload_id)["content_hash"]
    root = Path(store.analysis_dir(content_hash))
    source = next(p for p in root.iterdir() if p.name.startswith("source."))
    source.unlink()
    (root / (source.name + ".part")).write_bytes(MP4[: len(MP4) // 3])
    return content_hash


def test_a_partial_copy_in_the_analysis_store_is_never_served(alice):
    """store.source_path matched any 'source.*' name, the .part included, so
    the route answered 200 with a third of the video as the whole file (QA,
    2026-09-30). A partial copy is not the file: 410, as with no copy."""
    from app.video import store

    conv = _chat(alice, "conv-part")
    upload_id = _upload(alice, conv, "clip.mp4", MP4, "video", "video/mp4")
    content_hash = _partial_only(conv, upload_id)
    _sweep(conv, upload_id)
    assert store.source_path(content_hash) is None
    assert alice.get(f"/uploads/{conv}/{upload_id}/file").status_code == 410


def test_adopt_source_replaces_a_leftover_partial_copy(tmp_path):
    """The same .part also made adopt_source's "already stored" check hand
    the pipeline a third of the video; the next adopt now finishes the copy."""
    from pathlib import Path

    from app.video import store

    content_hash = "c" * 64
    root = Path(store.analysis_dir(content_hash))
    root.mkdir(parents=True)
    (root / "source.mp4.part").write_bytes(MP4[:100])
    upload = tmp_path / "upload.mp4"
    upload.write_bytes(MP4)
    dest = store.adopt_source(content_hash, str(upload), "upload.mp4")
    assert Path(dest) == root / "source.mp4"
    assert Path(dest).read_bytes() == MP4
    assert store.source_path(content_hash) == dest
    assert not (root / "source.mp4.part").exists()


class _Crash(Exception):
    """The process dying at a given line, as far as adopt_source can tell."""


def _parts(root) -> list:
    return sorted(p.name for p in root.iterdir() if p.name.endswith(".part"))


def _adopt_and_crash_before_the_rename(monkeypatch, content_hash: str, path) -> None:
    """Run adopt_source until its link is made, then stop it where a crash
    between os.link and os.replace would."""
    import os

    from app.video import store

    def crash(*_args, **_kwargs):
        raise _Crash

    with monkeypatch.context() as patched:
        patched.setattr(os, "replace", crash)
        with pytest.raises(_Crash):
            store.adopt_source(content_hash, str(path), "clip.mp4")


def test_adopt_source_recovers_from_a_crash_between_link_and_rename(tmp_path, monkeypatch):
    """A crash between the link and the rename left `source.mp4.part` as a
    hard link to the upload, holding the whole file. source_path skips a
    .part, so the next adopt of the same upload ran again, met the leftover
    at os.link, fell back to shutil.copyfile onto the same file and raised
    SameFileError (QA, 2026-10-01)."""
    import os
    from pathlib import Path

    from app.video import store

    content_hash = "d" * 64
    root = Path(store.analysis_dir(content_hash))
    upload = tmp_path / "upload.mp4"
    upload.write_bytes(MP4)
    _adopt_and_crash_before_the_rename(monkeypatch, content_hash, upload)
    (leftover,) = _parts(root)
    assert os.path.samefile(root / leftover, upload), "the crash left a hard link to the upload"
    assert store.source_path(content_hash) is None

    dest = store.adopt_source(content_hash, str(upload), "clip.mp4")
    assert Path(dest) == root / "source.mp4"
    assert Path(dest).read_bytes() == MP4
    assert _parts(root) == []
    assert upload.read_bytes() == MP4


def test_a_leftover_link_to_another_upload_is_never_written_through(tmp_path, monkeypatch):
    """The leftover is a hard link to the EARLIER upload's workspace file.
    Adopting a later upload of the same video copied through it, truncating
    and rewriting the earlier upload's own file (QA, 2026-10-01)."""
    import os
    from pathlib import Path

    from app.video import store

    content_hash = "e" * 64
    root = Path(store.analysis_dir(content_hash))
    earlier = tmp_path / "earlier.mp4"
    earlier.write_bytes(MP4)
    _adopt_and_crash_before_the_rename(monkeypatch, content_hash, earlier)
    os.utime(earlier, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    before = os.stat(earlier)

    later = tmp_path / "later.mp4"
    later.write_bytes(MP4)
    dest = store.adopt_source(content_hash, str(later), "clip.mp4")
    after = os.stat(earlier)
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size), "written through the link"
    assert earlier.read_bytes() == MP4
    assert Path(dest).read_bytes() == MP4 and os.path.samefile(dest, later)
    assert _parts(root) == []


def test_two_adopters_of_one_video_at_once_both_get_the_whole_file(tmp_path, monkeypatch):
    """The same video uploaded twice at the same moment: the second adopt runs
    between the first one's link and its rename. With one shared temporary
    name the second copied through the first one's link and renamed it away,
    and the first then failed with FileNotFoundError."""
    import os
    from pathlib import Path

    from app.video import store

    content_hash = "f" * 64
    root = Path(store.analysis_dir(content_hash))
    first, second = tmp_path / "first.mp4", tmp_path / "second.mp4"
    first.write_bytes(MP4)
    second.write_bytes(MP4)
    real_replace = os.replace
    seen: dict = {}

    def second_adopter_arrives(src, dst):
        if not seen:  # the first rename only; the second adopter's own passes through
            seen["second"] = None
            seen["second"] = store.adopt_source(content_hash, str(second), "clip.mp4")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", second_adopter_arrives)
    seen["first"] = store.adopt_source(content_hash, str(first), "clip.mp4")
    monkeypatch.setattr(os, "replace", real_replace)
    assert seen["first"] == seen["second"] == str(root / "source.mp4")
    assert Path(seen["first"]).read_bytes() == MP4
    assert first.read_bytes() == MP4 and second.read_bytes() == MP4
    assert _parts(root) == []
