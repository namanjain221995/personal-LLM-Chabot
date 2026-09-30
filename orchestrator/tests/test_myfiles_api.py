"""My files (2026-09-30): one list of everything a person uploaded.

WHY. The owner asked "if any user uploads anything in our AI app, it is
stored, right, but it does not show on the user side". Until this page the
only upload a person could list anywhere was a voice recording (/recordings);
documents, spreadsheets, videos and audio files were reachable only from the
chat they were attached to, and an admin could list a member's uploads while
the member could not list their own.

What this file pins:

* the list is the CALLER's and nobody else's — identity comes from the
  session alone, a `user_id` parameter is ignored, a super admin sees their
  own files here (inspection stays on the audited admin routes), and the
  reserved `u<digits>-` conversation shape never lists (the F034 IDOR);
* every upload rail lands under the kind the page shows, and a new
  `save_upload` writer cannot appear without choosing one;
* each row says what is REALLY stored: bytes, only the text the chat read,
  only a spreadsheet's summary, still processing, or nothing;
* keyset paging, every sort, the filters and the summary counts agree with
  one another;
* the first page stays one statement at 2,000 uploads.
"""
from __future__ import annotations

import base64
import io
import json
import shutil
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db
from app.config import settings

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
PDF = b"%PDF-1.4 stand-in bytes for a contract\n" * 50
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8192
MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 2048


# ----------------------------------------------------------------- fixtures --


@pytest.fixture(autouse=True)
def _stores(tmp_path, monkeypatch):
    """Every byte store in a temporary directory, the rails switched on, and
    the video job never actually started (its bytes are what matters here)."""
    from app.video import pipeline

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path / "ws"))
    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    monkeypatch.setattr(settings, "voice_data_dir", str(tmp_path / "voice"))
    monkeypatch.setattr(settings, "dataset_uploads_enabled", True)
    monkeypatch.setattr(settings, "video_analysis_enabled", True)
    monkeypatch.setattr(settings, "workspace_ttl_hours", 24)
    monkeypatch.setattr(settings, "voice_retention_days", 0)
    # The upload-time extraction runs behind the response and writes into the
    # upload's directory; these tests remove that directory to stand for the
    # sweep, and a late prewarm must not put part of it back.
    monkeypatch.setattr(settings, "document_prewarm_enabled", False)

    async def not_started(_analysis_id):
        return False

    monkeypatch.setattr(pipeline, "ensure_running", not_started)


def uid(username: str) -> int:
    return int(db.get_user_by_username(username)["id"])


def conversation(owner: str, cid: str, title: str = "A chat", *, at: datetime = T0) -> str:
    """A conversations row written directly: the reserved shape included,
    which the history route has refused since 2026-09-13 but older rows
    still carry."""
    with db.connection() as con:
        con.execute(
            "INSERT INTO conversations (id, user_id, title, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (cid, uid(owner), title, at, at),
        )
    return cid


def upload_row(conv: str, filename: str, *, size: int = 100, notes=None, status: str = "ready",
               at: datetime = T0, profile=None, upload_id: str | None = None) -> str:
    upload_id = upload_id or uuid.uuid4().hex
    with db.connection() as con:
        con.execute(
            "INSERT INTO uploads (id, conversation_id, filename, bytes, status, profile, notes, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (upload_id, conv, filename, size, status,
             None if profile is None else json.dumps(profile), notes, at),
        )
    return upload_id


def document_row(conv: str, filename: str, *, at: datetime = T0, text: str = "the text") -> int:
    with db.connection() as con:
        row = con.execute(
            "INSERT INTO documents (conversation_id, filename, text, total_pages, created_at) "
            "VALUES (%s, %s, %s, 1, %s) RETURNING id",
            (conv, filename, text, at),
        ).fetchone()
    return int(row["id"])


def recording_row(owner: str, *, status: str = "done", size: int = 4096, at: datetime = T0,
                  audio_ms: int = 61_000, deleted: bool = False, session_id: str | None = None) -> str:
    session_id = session_id or uuid.uuid4().hex
    with db.connection() as con:
        con.execute(
            "INSERT INTO voice_sessions (id, user_id, client_key, status, mime_type, ext, bytes_stored, "
            "audio_ms, created_at, audio_deleted_at) VALUES (%s, %s, %s, %s, 'audio/webm', 'webm', %s, %s, %s, %s)",
            (session_id, uid(owner), str(uuid.uuid4()), status, size, audio_ms, at,
             at + timedelta(minutes=5) if deleted else None),
        )
    return session_id


def mine(client, **params):
    resp = client.get("/files/mine", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def every(client, **params):
    """Follow next_cursor to the end; returns (items, pages)."""
    items, pages, cursor = [], 0, None
    while True:
        query = dict(params)
        if cursor:
            query["cursor"] = cursor
        body = mine(client, **query)
        pages += 1
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            return items, pages
        assert pages < 100, "paging never ended"


def names(items):
    return [i["name"] for i in items]


def by_name(items):
    return {i["name"]: i for i in items}


def post_upload(client, conv: str, filename: str, data: bytes, purpose: str, ctype: str = "application/octet-stream"):
    resp = client.post(
        "/uploads",
        files={"file": (filename, data, ctype)},
        data={"conversation_id": conv, "purpose": purpose},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["upload_id"]


def zip_bytes(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def workspace_copy(conv: str, upload_id: str) -> Path:
    from app.uploads import upload_root

    return Path(upload_root(conv, upload_id))


# ----------------------------------------------------------- authentication --


def test_signed_out_is_401_on_both_routes(anonymous_mode):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    assert client.get("/files/mine").status_code == 401
    assert client.get("/files/mine/summary").status_code == 401


def test_the_list_needs_no_upload_voice_or_video_feature(login_client):
    """A person must always be able to find what is stored about them — the
    rule /audio/sessions already follows. Switching a tool off gates making
    new files, never reaching the ones that exist."""
    from app.authn import store

    alice = login_client("alice")
    workspace = store.default_workspace()
    store.set_member_feature_overrides(
        workspace["id"], uid("alice"),
        {"attachments": False, "voice_input": False, "video_analysis": False},
    )
    conv = conversation("alice", "alice-chat")
    upload_row(conv, "kept.pdf", notes="document")
    recording_row("alice")
    listed = mine(alice)["items"]
    assert sorted(i["kind"] for i in listed) == ["document", "recording"]
    summary = alice.get("/files/mine/summary")
    assert summary.status_code == 200, summary.text


# ------------------------------------------------------------------ isolation --


@pytest.mark.parametrize("role_of_viewer", ["member", "admin", "super_admin"])
def test_each_person_sees_only_their_own_files(login_client, role_of_viewer):
    viewer = login_client("viewer", role=role_of_viewer)
    login_client("other")
    for owner in ("viewer", "other"):
        conv = conversation(owner, f"{owner}-chat", f"{owner} planning")
        upload_row(conv, f"{owner}-report.pdf", notes="document")
        document_row(conv, f"{owner}-notes.docx")
        recording_row(owner)
    items, _ = every(viewer)
    assert sorted(names(items)) == ["Voice recording", "viewer-notes.docx", "viewer-report.pdf"]
    assert all((i["conversation"] or {}).get("id") in (None, "viewer-chat") for i in items)
    summary = viewer.get("/files/mine/summary").json()
    assert summary["total"]["count"] == 3


def test_a_user_id_parameter_is_ignored(login_client):
    login_client("alice")
    bob = login_client("bob")
    upload_row(conversation("alice", "alice-chat"), "alice-only.pdf", notes="document")
    upload_row(conversation("bob", "bob-chat"), "bob-only.pdf", notes="document")
    body = mine(bob, user_id=str(uid("alice")), owner=str(uid("alice")))
    assert names(body["items"]) == ["bob-only.pdf"]
    summary = bob.get("/files/mine/summary", params={"user_id": uid("alice")}).json()
    assert summary["total"]["count"] == 1


def test_the_admin_upload_and_report_views_are_unchanged(login_client):
    """My files adds a list for the OWNER; the audited admin views keep
    listing what they listed, rejected uploads included, in the same shape."""
    root = login_client("root", role="super_admin")
    login_client("alice")
    conv = conversation("alice", "alice-chat", "Alice's chat")
    upload_row(conv, "good.csv", at=T0)
    upload_row(conv, "bad.xlsx", status="rejected", at=T0 + timedelta(minutes=1))
    resp = root.get(f"/admin/api/members/{uid('alice')}/uploads")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert [u["filename"] for u in body["uploads"]] == ["bad.xlsx", "good.csv"]
    assert set(body["uploads"][0]) == {
        "id", "conversation_id", "conversation_title", "filename", "bytes", "status", "created_at",
    }
    reports = root.get(f"/admin/api/members/{uid('alice')}/reports")
    assert reports.status_code == 200 and reports.json() == {"reports": []}


def test_a_reserved_shape_conversation_never_lists(login_client):
    """F034. A legacy chat named `u2-default` can be owned by one person
    while another person's bare /chat calls stored document text under that
    same key. Joining on conversations.user_id alone would hand the owner the
    other person's file names; the SHAPE is refused whoever owns the row."""
    alice = login_client("alice")
    login_client("bob")
    conversation("alice", "u2-default", "legacy")
    document_row("u2-default", "victim-secret.pdf", at=T0 + timedelta(hours=1))
    upload_row("u2-default", "victim-upload.pdf", notes="document", at=T0 + timedelta(hours=1))
    conversation("alice", "alice-chat")
    upload_row("alice-chat", "mine.pdf", notes="document")
    items, _ = every(alice)
    assert names(items) == ["mine.pdf"]
    assert every(alice, q="victim")[0] == []
    assert alice.get("/files/mine/summary").json()["total"]["count"] == 1


# ---------------------------------------------------------------------- kinds --


def test_each_upload_rail_lists_under_its_kind(login_client):
    alice = login_client("alice")
    conv = conversation("alice", "alice-chat", "Uploads")
    post_upload(alice, conv, "contract.pdf", PDF, "document", "application/pdf")
    post_upload(alice, conv, "sales.csv", b"region,amount\nnorth,10\nsouth,20\n", "dataset", "text/csv")
    post_upload(alice, conv, "bundle.zip", zip_bytes({"inner/a.csv": "x,y\n1,2\n"}), "dataset", "application/zip")
    post_upload(alice, conv, "standup.mp4", MP4, "video", "video/mp4")
    post_upload(alice, conv, "call.mp3", MP3, "video", "audio/mpeg")
    upload_row(conv, "refused.xlsx", status="rejected")
    upload_row(conv, "broken.csv", status="failed")
    kinds = {i["name"]: i["kind"] for i in every(alice)[0]}
    assert kinds == {
        "contract.pdf": "document",
        "sales.csv": "dataset",
        "bundle.zip": "dataset",
        "standup.mp4": "video",
        "call.mp3": "audio",
    }


def _save_upload_writers() -> list:
    """(file, source of the `notes` argument) for every call that writes an
    uploads row: `db.save_upload(...)` itself, or `db.save_upload` handed to
    `db.run_in_thread` with its arguments after it."""
    import ast

    app_dir = Path(__file__).resolve().parents[1] / "app"
    writers = []
    for path in sorted(app_dir.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            args = None
            if isinstance(node.func, ast.Attribute) and node.func.attr == "save_upload":
                args = node.args
            for i, arg in enumerate(node.args):
                if isinstance(arg, ast.Attribute) and arg.attr == "save_upload":
                    args = node.args[i + 1:]
            if args is not None:
                writers.append((path.relative_to(app_dir).as_posix(), ast.get_source_segment(text, args[-1])))
    return writers


def test_every_save_upload_writer_marks_a_known_kind():
    """The kind comes from uploads.notes, a free-text column written at a
    handful of call sites ('document', 'video', or a dataset's own notes). A
    new writer must be classified here, or its files would silently list as
    spreadsheets."""
    assert sorted(_save_upload_writers()) == sorted([
        ("uploads.py", '"document"'),  # _finalise_document
        ("uploads.py", "str(exc)"),  # a rejected dataset (not listed)
        ("uploads.py", 'f"{type(exc).__name__}"'),  # a failed dataset (not listed)
        ("uploads.py", '"; ".join(notes[:20]) or None'),  # a ready dataset
        ("video/api.py", '"video"'),  # video and audio
    ])


# ------------------------------------------------------------------ documents --


def test_text_only_documents_list_once_and_archive_members_fold(login_client):
    alice = login_client("alice")
    conv = conversation("alice", "alice-chat")
    upload_row(conv, "report.pdf", notes="document", at=T0)
    document_row(conv, "report.pdf", at=T0 + timedelta(seconds=5))
    document_row(conv, "inline-only.docx", at=T0 + timedelta(minutes=1))
    upload_row(conv, "bundle.zip", profile=[{"file": "inner/a.csv", "kind": "table"}], at=T0 + timedelta(minutes=2))
    document_row(conv, "bundle.zip (archive contents)", at=T0 + timedelta(minutes=2))
    document_row(conv, "bundle.zip/inner/a.txt", at=T0 + timedelta(minutes=2))
    items = every(alice)[0]
    assert sorted(names(items)) == ["bundle.zip", "inline-only.docx", "report.pdf"]
    listed = by_name(items)
    assert listed["inline-only.docx"]["source"] == "text"
    assert listed["inline-only.docx"]["availability"] == "text_only"
    assert listed["inline-only.docx"]["bytes"] is None
    assert listed["report.pdf"]["source"] == "upload"


def test_an_archive_sent_as_a_document_keeps_its_contents_text(login_client):
    """The composer sends a .zip on the DOCUMENT rail (found in the browser
    E2E, 2026-09-30): the archive keeps `_original`, and the chat stores what
    it read as "<name> (archive contents)" plus one row per member. Once the
    archive itself is swept, that text is still there, so the row is
    text-only, previewed from the manifest — not "removed"."""
    alice = login_client("alice")
    conv = conversation("alice", "alice-chat")
    upload_id = post_upload(
        alice, conv, "export.zip", zip_bytes({"customers.csv": "id,name\n1,Asha\n"}), "document", "application/zip"
    )
    document_row(conv, "export.zip (archive contents)", text="customers.csv (15 bytes)")
    document_row(conv, "export.zip/customers.csv")
    fresh = every(alice)[0]
    assert names(fresh) == ["export.zip"]
    assert fresh[0]["kind"] == "document" and fresh[0]["availability"] == "available"
    assert fresh[0]["can"]["preview"] == "text"
    assert fresh[0]["text_name"] == "export.zip (archive contents)"
    shutil.rmtree(workspace_copy(conv, upload_id))
    swept = every(alice)[0]
    assert swept[0]["availability"] == "text_only"
    assert swept[0]["can"] == {"download": False, "preview": "text", "delete": False}
    # A document whose own text exists previews that, never the manifest.
    plain = post_upload(alice, conv, "plain.pdf", PDF, "document", "application/pdf")
    document_row(conv, "plain.pdf")
    assert by_name(every(alice)[0])["plain.pdf"]["text_name"] == "plain.pdf"
    assert plain


# ----------------------------------------------------------------- recordings --


def test_recordings_list_by_status_and_hide_what_is_gone(login_client):
    alice = login_client("alice")
    done = recording_row("alice", status="done", at=T0 + timedelta(minutes=1))
    failed = recording_row("alice", status="failed", at=T0 + timedelta(minutes=2))
    live = recording_row("alice", status="recording", at=T0 + timedelta(minutes=3))
    finishing = recording_row("alice", status="finishing", at=T0 + timedelta(minutes=4))
    recording_row("alice", status="cancelled", at=T0 + timedelta(minutes=5), deleted=True)
    recording_row("alice", status="done", at=T0 + timedelta(minutes=6), deleted=True)
    items = every(alice)[0]
    got = {i["id"]: i["availability"] for i in items}
    assert got == {
        f"recording:{done}": "available",
        f"recording:{failed}": "available",
        f"recording:{live}": "processing",
        f"recording:{finishing}": "processing",
    }
    first = next(i for i in items if i["id"] == f"recording:{done}")
    assert first["kind"] == "recording" and first["conversation"] is None
    assert first["media"] == {"status": "done", "duration_ms": 61_000}
    assert first["can"] == {"download": True, "preview": "audio", "delete": True}
    processing = next(i for i in items if i["id"] == f"recording:{live}")
    assert processing["can"]["download"] is False


def test_deleting_a_recording_removes_it_from_both_lists(login_client):
    alice = login_client("alice")
    kept = recording_row("alice", at=T0)
    gone = recording_row("alice", at=T0 + timedelta(minutes=1))
    assert alice.delete(f"/audio/sessions/{gone}").status_code == 204
    assert [i["id"] for i in every(alice)[0]] == [f"recording:{kept}"]
    sessions = alice.get("/audio/sessions").json()["sessions"]
    assert [s["session_id"] for s in sessions] == [kept]


# --------------------------------------------------------------- availability --


def test_availability_follows_what_is_really_stored(login_client):
    alice = login_client("alice")
    conv = conversation("alice", "alice-chat", "Everything")
    present = post_upload(alice, conv, "present.pdf", PDF, "document", "application/pdf")
    swept_with_text = post_upload(alice, conv, "swept-text.pdf", PDF, "document", "application/pdf")
    document_row(conv, "swept-text.pdf")
    swept_bare = post_upload(alice, conv, "swept-bare.pdf", PDF, "document", "application/pdf")
    table = post_upload(alice, conv, "table.csv", b"a,b\n1,2\n", "dataset", "text/csv")
    table_swept = post_upload(alice, conv, "table-swept.csv", b"a,b\n3,4\n", "dataset", "text/csv")
    post_upload(alice, conv, "archive.zip", zip_bytes({"a.csv": "a,b\n1,2\n"}), "dataset", "application/zip")
    video_present = post_upload(alice, conv, "kept.mp4", MP4, "video", "video/mp4")
    video_store_only = post_upload(alice, conv, "store-only.mp4", MP4 + b"1", "video", "video/mp4")
    video_gone = post_upload(alice, conv, "gone.mp4", MP4 + b"2", "video", "video/mp4")

    for swept in (swept_with_text, swept_bare, table_swept, video_store_only, video_gone):
        shutil.rmtree(workspace_copy(conv, swept))
    gone_hash = db.get_video_by_upload(conv, video_gone)["content_hash"]
    from app.video import store

    store.remove_analysis(gone_hash)

    listed = by_name(every(alice)[0])
    availability = {name: item["availability"] for name, item in listed.items()}
    assert availability == {
        "present.pdf": "available",
        "swept-text.pdf": "text_only",
        "swept-bare.pdf": "expired",
        "table.csv": "available",
        "table-swept.csv": "summary_only",
        "archive.zip": "summary_only",
        "kept.mp4": "available",
        "store-only.mp4": "available",
        "gone.mp4": "expired",
    }
    assert listed["present.pdf"]["can"] == {"download": True, "preview": None, "delete": False}
    assert listed["swept-text.pdf"]["can"] == {"download": False, "preview": "text", "delete": False}
    assert listed["swept-bare.pdf"]["can"] == {"download": False, "preview": None, "delete": False}
    assert listed["table-swept.csv"]["can"] == {"download": False, "preview": "summary", "delete": False}
    assert listed["archive.zip"]["can"]["download"] is False
    assert listed["store-only.mp4"]["can"]["download"] is True
    assert listed["kept.mp4"]["media"]["status"] == "queued"
    assert listed["present.pdf"]["media"] is None
    assert listed["present.pdf"]["conversation"] == {"id": conv, "title": "Everything"}
    assert listed["present.pdf"]["id"] == f"upload:{present}"
    assert listed["table.csv"]["id"] == f"upload:{table}"
    assert listed["kept.mp4"]["id"] == f"upload:{video_present}"


# --------------------------------------------------------------------- paging --


def _seed_mixed(owner: str, count: int) -> list:
    """`count` items across all three sources, in groups of three that share
    one created_at (the keyset tiebreak is what keeps those apart)."""
    conv = conversation(owner, f"{owner}-many", "Many")
    ids = []
    for n in range(count):
        at = T0 + timedelta(minutes=n // 3)
        kind = n % 4
        if kind == 0:
            ids.append("upload:" + upload_row(conv, f"file-{n:03d}.pdf", notes="document", at=at, size=1000 + n))
        elif kind == 1:
            ids.append("upload:" + upload_row(conv, f"data-{n:03d}.csv", at=at, size=5000 - n))
        elif kind == 2:
            ids.append(f"text:{document_row(conv, f'note-{n:03d}.docx', at=at)}")
        else:
            ids.append("recording:" + recording_row(owner, at=at, size=300 + n))
    return ids


def test_keyset_paging_has_no_duplicates_or_gaps(login_client):
    alice = login_client("alice")
    seeded = _seed_mixed("alice", 120)
    items, pages = every(alice, limit=50)
    assert pages == 3
    ids = [i["id"] for i in items]
    assert len(ids) == len(set(ids)) == 120
    assert set(ids) == set(seeded)
    stamps = [i["created_at"] for i in items]
    assert stamps == sorted(stamps, reverse=True)


def test_rows_inserted_or_deleted_between_pages_shift_nothing(login_client):
    alice = login_client("alice")
    _seed_mixed("alice", 30)
    first = mine(alice, limit=10)
    before = every(alice, limit=10)[0]
    newer = conversation("alice", "alice-later", "Later", at=T0 + timedelta(days=1))
    upload_row(newer, "brand-new.pdf", notes="document", at=T0 + timedelta(days=1))
    victim = first["items"][3]["id"]
    if victim.startswith("recording:"):
        with db.connection() as con:
            con.execute("DELETE FROM voice_sessions WHERE id = %s", (victim.split(":", 1)[1],))
    elif victim.startswith("text:"):
        with db.connection() as con:
            con.execute("DELETE FROM documents WHERE id = %s", (int(victim.split(":", 1)[1]),))
    else:
        with db.connection() as con:
            con.execute("DELETE FROM uploads WHERE id = %s", (victim.split(":", 1)[1],))
    second = mine(alice, limit=10, cursor=first["next_cursor"])
    assert [i["id"] for i in second["items"]] == [i["id"] for i in before[10:20]]


def _expected_order(items, sort):
    def key(i):
        source, item_id = i["id"].split(":", 1)
        if sort in ("newest", "oldest"):
            return (i["created_at"], source, item_id)
        if sort == "largest":
            return (-1 if i["bytes"] is None else i["bytes"], source, item_id)
        return (i["name"].lower(), source, item_id)

    return sorted(items, key=key, reverse=sort in ("newest", "largest"))


@pytest.mark.parametrize("sort", ["newest", "oldest", "largest", "name"])
def test_every_sort_pages_without_gaps(login_client, sort):
    alice = login_client("alice")
    _seed_mixed("alice", 40)
    everything, _ = every(alice, limit=100)
    paged, pages = every(alice, sort=sort, limit=7)
    assert pages == 6
    assert [i["id"] for i in paged] == [i["id"] for i in _expected_order(everything, sort)]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "101"},
        {"limit": "ten"},
        {"cursor": "not-a-cursor"},
        {"cursor": base64.urlsafe_b64encode(b'{"v":1,"sort":"newest","k":[1,2,3]}').decode()},
        {"sort": "sideways"},
        {"kind": "document,pictures"},
        {"since": "yesterday"},
        {"min_bytes": "-1"},
        {"min_bytes": "10", "max_bytes": "5"},
        {"q": "x" * 101},
    ],
)
def test_invalid_parameters_are_a_flat_400(login_client, params):
    alice = login_client("alice")
    resp = alice.get("/files/mine", params=params)
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert set(body) == {"detail", "reason"} and body["reason"] == "bad_request"


def test_a_cursor_from_another_sort_is_refused(login_client):
    alice = login_client("alice")
    _seed_mixed("alice", 12)
    cursor = mine(alice, limit=5)["next_cursor"]
    assert cursor
    resp = alice.get("/files/mine", params={"cursor": cursor, "sort": "largest", "limit": 5})
    assert resp.status_code == 400
    assert resp.json()["reason"] == "bad_request"


# -------------------------------------------------------------------- filters --


def test_the_kind_filter_takes_several_kinds(login_client):
    alice = login_client("alice")
    _seed_mixed("alice", 16)
    items = every(alice, kind="document,recording")[0]
    assert {i["kind"] for i in items} == {"document", "recording"}
    assert len(items) == 12  # 4 uploaded documents, 4 text-only, 4 recordings
    assert {i["kind"] for i in every(alice, kind="dataset")[0]} == {"dataset"}


@pytest.mark.parametrize("kind", ["document", "dataset", "video", "audio", "recording"])
@pytest.mark.parametrize("sort", ["newest", "oldest", "largest", "name"])
def test_each_kind_alone_lists_pages_and_searches(login_client, kind, sort):
    """Each Type chip on its own. The union's column names come from its
    FIRST branch, so a kind that selects a single branch (the "Voice
    recordings" chip selects only voice_sessions) must name its own columns:
    found by the 2026-09-30 end-to-end run as a 500 on ?kind=recording."""
    alice = login_client("alice")
    conv = conversation("alice", "alice-kinds", "Kinds")
    for n in range(3):
        at = T0 + timedelta(minutes=n)
        upload_row(conv, f"doc-{n}.pdf", notes="document", at=at, size=100 + n)
        upload_row(conv, f"data-{n}.csv", at=at, size=200 + n)
        upload_row(conv, f"clip-{n}.mp4", notes="video", at=at, size=300 + n)
        upload_row(conv, f"call-{n}.mp3", notes="video", at=at, size=400 + n)
        recording_row("alice", at=at, size=500 + n)
    items, pages = every(alice, kind=kind, sort=sort, limit=2)
    assert pages == 2
    assert len(items) == 3 and {i["kind"] for i in items} == {kind}
    assert len({i["id"] for i in items}) == 3
    term = {"document": "doc-1", "dataset": "data-1", "video": "clip-1", "audio": "call-1",
            "recording": "voice"}[kind]
    found = every(alice, kind=kind, sort=sort, q=term)[0]
    assert len(found) == (3 if kind == "recording" else 1)


def test_search_is_literal_and_reaches_the_chat_title(login_client):
    alice = login_client("alice")
    plans = conversation("alice", "alice-plans", "Quarterly planning")
    other = conversation("alice", "alice-other", "Misc")
    upload_row(other, "50%_off.pdf", notes="document")
    upload_row(other, "50x off.pdf", notes="document")
    upload_row(other, "a_b.csv")
    document_row(plans, "notes.txt")
    recording_row("alice")
    assert names(every(alice, q="50%")[0]) == ["50%_off.pdf"]
    assert sorted(names(every(alice, q="_")[0])) == ["50%_off.pdf", "a_b.csv"]
    assert names(every(alice, q="QUARTERLY")[0]) == ["notes.txt"]
    assert names(every(alice, q="voice")[0]) == ["Voice recording"]


def test_since_and_until_are_half_open(login_client):
    alice = login_client("alice")
    conv = conversation("alice", "alice-chat")
    for n in range(3):
        upload_row(conv, f"t{n}.pdf", notes="document", at=T0 + timedelta(hours=n))
    since = (T0 + timedelta(hours=1)).isoformat()
    until = (T0 + timedelta(hours=2)).isoformat()
    assert names(every(alice, since=since)[0]) == ["t2.pdf", "t1.pdf"]
    assert names(every(alice, until=until)[0]) == ["t1.pdf", "t0.pdf"]
    assert names(every(alice, since=since, until=until)[0]) == ["t1.pdf"]
    # A date without a time is midnight UTC; "Z" is UTC.
    assert names(every(alice, since="2026-09-01", until="2026-09-01T13:00:00Z")[0]) == ["t0.pdf"]


def test_size_filters_are_half_open_and_skip_text_only_documents(login_client):
    alice = login_client("alice")
    conv = conversation("alice", "alice-chat")
    upload_row(conv, "small.pdf", notes="document", size=999)
    upload_row(conv, "edge.pdf", notes="document", size=1000)
    upload_row(conv, "big.pdf", notes="document", size=5000)
    document_row(conv, "text-only.docx")
    assert sorted(names(every(alice, min_bytes=0)[0])) == ["big.pdf", "edge.pdf", "small.pdf"]
    assert names(every(alice, max_bytes=1000)[0]) == ["small.pdf"]
    assert names(every(alice, min_bytes=1000, max_bytes=5000)[0]) == ["edge.pdf"]


# -------------------------------------------------------------------- summary --


def test_the_summary_agrees_with_the_list(login_client):
    alice = login_client("alice")
    bob = login_client("bob")
    _seed_mixed("alice", 24)
    upload_row(conversation("bob", "bob-chat"), "bobs.pdf", notes="document", size=777)
    items = every(alice)[0]
    summary = alice.get("/files/mine/summary").json()
    for kind in ("document", "dataset", "video", "audio", "recording"):
        of_kind = [i for i in items if i["kind"] == kind]
        assert summary["kinds"][kind] == {
            "count": len(of_kind),
            "bytes": sum(i["bytes"] or 0 for i in of_kind),
        }, kind
    assert summary["total"] == {"count": len(items), "bytes": sum(i["bytes"] or 0 for i in items)}
    filtered = alice.get("/files/mine/summary", params={"q": "note"}).json()
    assert filtered["total"]["count"] == len(every(alice, q="note")[0])
    assert bob.get("/files/mine/summary").json()["total"] == {"count": 1, "bytes": 777}


def test_the_retention_block_reports_the_deployment(login_client, monkeypatch):
    alice = login_client("alice")
    monkeypatch.setattr(settings, "workspace_ttl_hours", 36)
    monkeypatch.setattr(settings, "voice_retention_days", 30)
    monkeypatch.setattr(settings, "video_orphan_ttl_hours", 72)
    retention = mine(alice)["retention"]
    assert retention == {
        "upload_hours": 36,
        "recording_days": 30,
        "video_kept_with_chat": True,
        "video_grace_hours": 72,
        "pictures": "browser_only",
        "picture_memory_hours": 2,
    }
    assert alice.get("/files/mine/summary").json()["retention"] == retention


# -------------------------------------------------------------------- metrics --


def test_the_metrics_count_each_outcome_and_never_carry_content(login_client):
    """myfiles_list_seconds{view} and myfiles_list_total{view,result}: closed
    label sets (metrics._LABELS_BY_METRIC), and nothing of the person's own —
    no file name, no search text, no user — in the exposition."""
    from app import metrics

    alice = login_client("alice")
    upload_row(conversation("alice", "alice-chat", "Board pack"), "secret-merger.pdf", notes="document")
    metrics.reset()
    try:
        mine(alice)
        mine(alice, q="secret-merger")
        assert alice.get("/files/mine/summary").status_code == 200
        assert alice.get("/files/mine", params={"limit": "0"}).status_code == 400
        rendered = metrics.render()
        assert 'myfiles_list_total{result="ok",view="list"} 2' in rendered
        assert 'myfiles_list_total{result="ok",view="summary"} 1' in rendered
        assert 'myfiles_list_total{result="bad_request",view="list"} 1' in rendered
        assert 'myfiles_list_seconds_count{view="list"} 3' in rendered
        assert 'myfiles_list_seconds_count{view="summary"} 1' in rendered
        lines = [line for line in rendered.splitlines() if line.startswith("myfiles_")]
        assert lines
        for text in ("secret-merger", "Board pack", "alice"):
            assert not any(text in line for line in lines), text
        # Only the closed label names ever appear (le is the histogram's own).
        label_names = {
            pair.split("=", 1)[0]
            for line in lines if "{" in line
            for pair in line[line.index("{") + 1:line.index("}")].split(",")
        }
        assert label_names <= {"view", "result", "le"}, label_names
    finally:
        metrics.reset()


# ---------------------------------------------------------------- performance --


@contextmanager
def _counting(monkeypatch):
    counter = {"statements": 0}
    real_read, real_conn = db.read_connection, db.connection

    class _Counted:
        def __init__(self, con):
            self._con = con

        def execute(self, *args, **kwargs):
            counter["statements"] += 1
            return self._con.execute(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._con, name)

    @contextmanager
    def read_connection():
        with real_read() as con:
            yield _Counted(con)

    @contextmanager
    def connection():
        with real_conn() as con:
            yield _Counted(con)

    monkeypatch.setattr(db, "read_connection", read_connection)
    monkeypatch.setattr(db, "connection", connection)
    yield counter


def test_the_first_page_is_one_bounded_statement_at_two_thousand_uploads(login_client, monkeypatch):
    from app import myfiles

    login_client("heavy")
    heavy = uid("heavy")
    with db.connection() as con:
        con.execute(
            "INSERT INTO conversations (id, user_id, title, created_at, updated_at) "
            "SELECT 'heavy-' || g, %s, 'Chat ' || g, %s, %s FROM generate_series(1, 1000) g",
            (heavy, T0, T0),
        )
        con.execute(
            "INSERT INTO uploads (id, conversation_id, filename, bytes, status, notes, created_at) "
            "SELECT md5(g::text), 'heavy-' || (1 + mod(g, 1000)), 'file-' || g || '.pdf', g, 'ready', "
            "'document', %s + g * interval '1 second' FROM generate_series(1, 2000) g",
            (T0,),
        )
        con.execute("ANALYZE uploads")
        con.execute("ANALYZE conversations")
    query = myfiles.parse_list_query({})
    myfiles.list_files(heavy, query)  # warm the pool and the plan cache
    with _counting(monkeypatch) as counter:
        started = time.perf_counter()
        body = myfiles.list_files(heavy, query)
        elapsed = time.perf_counter() - started
    assert len(body["items"]) == 50 and body["next_cursor"]
    assert counter["statements"] <= 3, counter
    assert elapsed < 0.25, f"first page took {elapsed * 1000:.1f} ms"
