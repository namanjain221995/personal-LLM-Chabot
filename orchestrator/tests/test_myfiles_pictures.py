"""My files lists stored chat pictures and counts lasting copies as stored
(2026-10-02, docs/chat-media/CONTRACT.md §9).

THE DEFECT. Pictures were left off the list ("they were never stored on the
account"), and once the workspace sweep ran a document was "Removed" although
the chat could still open it. Now a `chat_media` row lists as kind `image` in
exactly the shape the page parses (frontend/lib/myfiles.ts parseFile, and the
fe-files section of docs/chat-media/NOTES.md), and a document or dataset with
a lasting copy is `available`.

What is pinned: the picture row's shape, the summary count and the retention
words the page reads; each picture is the caller's twice over (the row's own
user AND the chat's owner) with the reserved `u<digits>-` shape excluded;
`?kind=image` on its own pages under every sort and searches (the UNION's
first branch names its columns); a picture whose file is gone is Removed.
"""
from __future__ import annotations

import io
import os
import shutil
import uuid

import pytest
from PIL import Image

from app import chat_media, db
from app.config import settings

PDF = b"%PDF-1.4 a contract that must outlive the sweep\n" * 64


@pytest.fixture(autouse=True)
def _stores(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path / "ws"))
    monkeypatch.setattr(settings, "dataset_uploads_enabled", True)
    monkeypatch.setattr(settings, "document_prewarm_enabled", False)


def _uid(username: str) -> int:
    return int(db.get_user_by_username(username)["id"])


def _chat(client, cid: str, title: str = "Holiday") -> str:
    resp = client.post("/history/conversations", json={"id": cid, "title": title})
    assert resp.status_code == 200, resp.text
    return cid


def _png(width: int = 64, height: int = 48, colour=(200, 30, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, format="PNG")
    return out.getvalue()


def _picture(client, conv: str, attachment_id: str, payload: bytes | None = None) -> dict:
    payload = payload or _png()
    resp = client.post(
        f"/chat-media/{conv}",
        files=[("file", ("photo.png", payload, "image/png"))],
        data={"attachment_id": [attachment_id]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["items"][0]


def _mine(client, **params) -> dict:
    resp = client.get("/files/mine", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _every(client, **params) -> list:
    items, cursor = [], None
    for _ in range(50):
        query = dict(params)
        if cursor:
            query["cursor"] = cursor
        body = _mine(client, **query)
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            return items
    raise AssertionError("paging never ended")


def _insert_row(user_id: int, conversation_id: str, attachment_id: str) -> str:
    """A chat_media row written directly (no file), for shapes the routes
    refuse to create."""
    media_id = uuid.uuid4().hex
    with db.connection() as con:
        con.execute(
            "INSERT INTO chat_media (media_id, user_id, conversation_id, attachment_id, sha256, mime, "
            "bytes, width, height, has_thumb, source) "
            "VALUES (%s, %s, %s, %s, %s, 'image/png', 10, 1, 1, false, 'chat')",
            (media_id, user_id, conversation_id, attachment_id, "0" * 64),
        )
    return media_id


# ------------------------------------------------------------- pictures --


def test_a_stored_picture_lists_in_the_shape_the_page_parses(login_client):
    alice = login_client("alice")
    conv = _chat(alice, "conv-photos", "Holiday")
    stored = _picture(alice, conv, "att-00000001", _png(640, 480))
    body = _mine(alice)
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item == {
        "id": f"media:{stored['media_id']}",
        "source": "media",
        "kind": "image",
        "name": "Picture.png",
        "bytes": stored["bytes"],
        "created_at": item["created_at"],
        "conversation": {"id": conv, "title": "Holiday"},
        "availability": "available",
        "media": {"width": 640, "height": 480, "mime": "image/png"},
        "can": {"download": True, "preview": "image", "delete": False},
        "text_name": None,
        "attachment_id": "att-00000001",
    }
    assert item["created_at"].endswith("+00:00")
    # The URL the page builds from the row answers.
    assert alice.get(f"/chat-media/{conv}/{item['attachment_id']}?size=thumb").status_code == 200

    retention = body["retention"]
    assert retention["pictures"] == "kept_with_chat"
    assert retention["files_kept_with_chat"] is True
    summary = alice.get("/files/mine/summary").json()
    assert summary["kinds"]["image"] == {"count": 1, "bytes": stored["bytes"]}
    assert summary["total"] == {"count": 1, "bytes": stored["bytes"]}
    assert summary["retention"] == retention


def test_a_picture_whose_file_is_gone_is_removed(login_client):
    alice = login_client("alice")
    conv = _chat(alice, "conv-photos")
    stored = _picture(alice, conv, "att-00000001")
    shutil.rmtree(chat_media.media_dir(_uid("alice"), conv, stored["media_id"]))
    item = _mine(alice)["items"][0]
    assert item["availability"] == "expired"
    assert item["can"] == {"download": False, "preview": None, "delete": False}


@pytest.mark.parametrize("sort", ["newest", "oldest", "largest", "name"])
def test_kind_image_alone_pages_and_searches(login_client, sort):
    """The picture branch alone is the UNION's first branch: it must name
    its own columns (docs/MY-FILES.md trap 1), or this is a 500."""
    alice = login_client("alice")
    conv = _chat(alice, "conv-photos", "Holiday in Goa")
    other = _chat(alice, "conv-docs", "Contracts")
    for n in range(3):
        _picture(alice, conv, f"att-0000000{n}", _png(32 + n, 32, (n, n, n)))
    resp = alice.post(
        "/uploads",
        files={"file": ("contract.pdf", PDF, "application/pdf")},
        data={"conversation_id": other, "purpose": "document"},
    )
    assert resp.status_code == 200, resp.text

    first = _mine(alice, kind="image", sort=sort, limit=2)
    assert len(first["items"]) == 2 and first["next_cursor"]
    items = _every(alice, kind="image", sort=sort, limit=2)
    assert len(items) == 3 and len({i["id"] for i in items}) == 3
    assert {i["kind"] for i in items} == {"image"}
    # Searchable by the name the page shows and by the chat's title.
    assert len(_every(alice, kind="image", sort=sort, q="picture")) == 3
    assert len(_every(alice, kind="image", sort=sort, q="goa")) == 3
    assert _every(alice, kind="image", sort=sort, q="contract") == []
    # And beside the other kinds.
    assert {i["kind"] for i in _every(alice, kind="image,document", sort=sort)} == {"image", "document"}
    assert len(_every(alice, sort=sort, limit=1)) == 4


def test_another_persons_pictures_are_never_listed(login_client):
    alice = login_client("alice")
    bob = login_client("bob")
    alice_chat = _chat(alice, "conv-alice")
    bob_chat = _chat(bob, "conv-bob")
    _picture(alice, alice_chat, "att-alice-01")
    _picture(bob, bob_chat, "att-bob-0001")
    # A row of Bob's under Alice's chat id (stored while the id was unowned,
    # then claimed by Alice): neither of them lists it.
    _insert_row(_uid("bob"), alice_chat, "att-bob-0002")
    # A row of Alice's under a chat Bob owns: not hers to list either.
    _insert_row(_uid("alice"), bob_chat, "att-alice-02")

    def pictures(client):
        return [i["attachment_id"] for i in _every(client, kind="image")]

    assert pictures(alice) == ["att-alice-01"]
    assert pictures(bob) == ["att-bob-0001"]
    assert alice.get("/files/mine/summary").json()["kinds"]["image"]["count"] == 1
    assert bob.get("/files/mine/summary").json()["kinds"]["image"]["count"] == 1


def test_the_reserved_conversation_shape_never_lists_a_picture(login_client):
    """F034: `u<digits>-` keys are user N's bare-call store, whoever's row
    names them."""
    alice = login_client("alice")
    reserved = f"u{_uid('alice')}-default"
    with db.connection() as con:
        con.execute(
            "INSERT INTO conversations (id, user_id, title, created_at, updated_at) "
            "VALUES (%s, %s, 'legacy', now(), now())",
            (reserved, _uid("alice")),
        )
    _insert_row(_uid("alice"), reserved, "att-reserved")
    assert _every(alice, kind="image") == []
    assert alice.get("/files/mine/summary").json()["kinds"]["image"]["count"] == 0


# ------------------------------------------------------------- lasting copies --


def test_a_swept_document_with_a_lasting_copy_is_available(login_client):
    from app import uploads

    alice = login_client("alice")
    conv = _chat(alice, "conv-docs", "Contracts")
    resp = alice.post(
        "/uploads",
        files={"file": ("contract.pdf", PDF, "application/pdf")},
        data={"conversation_id": conv, "purpose": "document"},
    )
    upload_id = resp.json()["upload_id"]
    shutil.rmtree(uploads.upload_root(conv, upload_id))
    item = _mine(alice)["items"][0]
    assert item["availability"] == "available"
    assert item["can"]["download"] is True
    # What the row promises, the route serves.
    got = alice.get(f"/uploads/{conv}/{upload_id}/file")
    assert got.status_code == 200 and got.content == PDF
    shutil.rmtree(os.path.dirname(uploads.lasting_path(conv, upload_id)))
    assert _mine(alice)["items"][0]["availability"] == "expired"
