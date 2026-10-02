"""Chat media: deletion, the reaper, accounts, admin inspection and sharing
(V44, 2026-10-02; docs/chat-media/CONTRACT.md §4.4, §7, §8).

A store that keeps a person's photos for the life of the chat has to let go
of them when the chat or the account goes, and must never become a way round
the rules that already guard a chat: the audited, rank-checked admin viewer,
and the share policy that keeps private material off public links.
"""
from __future__ import annotations

import io
import os
import shutil
import time
import uuid

import pytest
from PIL import Image

from app import chat_media, db, metrics, sharing
from app.config import settings

GRACE_PAST = 25 * 3600  # seconds; the default grace is 24 h


@pytest.fixture(autouse=True)
def _room(monkeypatch):
    monkeypatch.setattr(settings, "chat_media_min_free_gib", 0.0)
    metrics.reset()
    yield
    metrics.reset()


def _png(colour=(200, 30, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 48), colour).save(out, format="PNG")
    return out.getvalue()


def _uid(username: str) -> int:
    return int(db.get_user_by_username(username)["id"])


def _chat(client, conv: str) -> None:
    assert client.post("/history/conversations", json={"id": conv, "title": "photos"}).status_code == 200


def _store_one(client, conv: str, attachment_id: str = "att-00000001", payload: bytes | None = None) -> dict:
    resp = client.post(
        f"/chat-media/{conv}",
        files=[("file", ("p.png", payload if payload is not None else _png(), "image/png"))],
        data={"attachment_id": [attachment_id]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["items"][0]


def _rows(conv: str) -> list:
    with db.connection() as con:
        return [
            dict(r)
            for r in con.execute("SELECT * FROM chat_media WHERE conversation_id = %s", (conv,)).fetchall()
        ]


def _conv_dir(username: str, conv: str) -> str:
    return os.path.join(settings.chat_media_dir, str(_uid(username)), conv)


def _age(path: str, seconds: float = GRACE_PAST) -> None:
    """Make a tree look `seconds` old, deepest entries first."""
    when = time.time() - seconds
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files + dirs:
            os.utime(os.path.join(root, name), (when, when))
        os.utime(root, (when, when))


def _age_rows(seconds: float = GRACE_PAST) -> None:
    with db.connection() as con:
        con.execute(
            "UPDATE chat_media SET created_at = now() - make_interval(secs => %s)", (float(seconds),)
        )


def _counter(name: str, **labels) -> float:
    return metrics._counters.get(name, {}).get(tuple(sorted(labels.items())), 0.0)


# ---------------------------------------------------------------- deletion --


def test_deleting_a_chat_removes_its_rows_and_its_bytes_at_once(login_client):
    alice = login_client("alice")
    _chat(alice, "conv-delete")
    _chat(alice, "conv-keep")
    _store_one(alice, "conv-delete", "att-00000001")
    _store_one(alice, "conv-delete", "att-00000002", _png(colour=(1, 1, 1)))
    kept = _store_one(alice, "conv-keep")
    # The files track's lasting copies live under CHAT_FILES_DIR/<chat>.
    lasting = os.path.join(settings.chat_files_dir, "conv-delete", uuid.uuid4().hex)
    os.makedirs(lasting)
    with open(os.path.join(lasting, "original"), "wb") as fh:
        fh.write(b"a document")

    assert alice.delete("/history/conversations/conv-delete").status_code == 200
    assert _rows("conv-delete") == []
    assert not os.path.exists(_conv_dir("alice", "conv-delete"))
    assert not os.path.exists(os.path.join(settings.chat_files_dir, "conv-delete"))
    assert _counter("chat_media_erase_total", store="media", result="ok") == 1
    assert _counter("chat_media_erase_total", store="files", result="ok") == 1
    # The other chat is untouched, bytes and row.
    assert [r["media_id"] for r in _rows("conv-keep")] == [kept["media_id"]]
    assert alice.get("/chat-media/conv-keep/att-00000001").status_code == 200
    # And the deleted chat's picture is unreachable, by list and by URL.
    assert alice.get("/chat-media/conv-delete/att-00000001").status_code == 404


def test_a_failed_erase_is_counted_and_the_reaper_finishes_it(login_client, monkeypatch):
    alice = login_client("alice")
    _chat(alice, "conv-stuck")
    _store_one(alice, "conv-stuck")
    real = shutil.rmtree

    def refuse_chat_media(path, *args, **kwargs):
        if str(path).startswith(settings.chat_media_dir):
            raise PermissionError("read-only for a moment")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", refuse_chat_media)
    assert alice.delete("/history/conversations/conv-stuck").status_code == 200  # never the person's problem
    assert _rows("conv-stuck") == []
    assert os.path.isdir(_conv_dir("alice", "conv-stuck"))
    assert _counter("chat_media_erase_total", store="media", result="error") == 1

    monkeypatch.setattr(shutil, "rmtree", real)
    _age(settings.chat_media_dir)
    assert chat_media.reap_once() == {"rows": 0, "dirs": 1}
    assert os.listdir(_conv_dir("alice", "conv-stuck")) == []


# ------------------------------------------------------------------ reaper --


def test_the_reaper_waits_out_the_grace_then_removes_only_orphans(login_client):
    alice = login_client("alice")
    # A picture stored for a chat that never got its row (the person left
    # before sending): an orphan once the grace has passed.
    abandoned = _store_one(alice, "conv-abandoned")
    # A real chat: its picture stays whatever its age.
    _chat(alice, "conv-owned")
    owned = _store_one(alice, "conv-owned")
    # A directory no row names (a crash between the files and the row), and
    # names this module never makes.
    stray = os.path.join(_conv_dir("alice", "conv-owned"), uuid.uuid4().hex)
    os.makedirs(stray)
    with open(os.path.join(stray, ".full.png.abcd1234.tmp"), "wb") as fh:
        fh.write(b"half a picture")
    foreign_file = os.path.join(_conv_dir("alice", "conv-owned"), "notes.txt")
    with open(foreign_file, "w") as fh:
        fh.write("not ours")
    foreign_dir = os.path.join(settings.chat_media_dir, "not-a-user", "conv-x", uuid.uuid4().hex)
    os.makedirs(foreign_dir)

    # Inside the grace nothing goes: /chat stores before the chat row exists.
    assert chat_media.reap_once() == {"rows": 0, "dirs": 0}
    assert len(_rows("conv-abandoned")) == 1 and os.path.isdir(stray)

    _age_rows()
    _age(settings.chat_media_dir)
    assert chat_media.reap_once() == {"rows": 1, "dirs": 1}
    assert _rows("conv-abandoned") == []
    assert not os.path.exists(os.path.join(_conv_dir("alice", "conv-abandoned"), abandoned["media_id"]))
    assert not os.path.exists(stray)
    assert [r["media_id"] for r in _rows("conv-owned")] == [owned["media_id"]]
    assert alice.get("/chat-media/conv-owned/att-00000001").status_code == 200
    assert os.path.exists(foreign_file) and os.path.isdir(foreign_dir)
    assert _counter("chat_media_reaped_total", kind="row") == 1
    assert _counter("chat_media_reaped_total", kind="dir") == 1
    # A second pass finds nothing more to do.
    _age(settings.chat_media_dir)
    assert chat_media.reap_once() == {"rows": 0, "dirs": 0}


def test_a_picture_under_a_chat_someone_else_claimed_is_reaped_after_the_grace(login_client):
    alice = login_client("alice")
    bob = login_client("bob")
    _store_one(bob, "conv-claimed")  # stored while nobody owned the id
    _chat(alice, "conv-claimed")  # then Alice's chat took it
    _age_rows()
    _age(settings.chat_media_dir)
    assert chat_media.reap_once()["rows"] == 1
    assert _rows("conv-claimed") == []


def test_deleting_an_account_cascades_its_rows_and_the_reaper_takes_the_bytes(login_client):
    bob = login_client("bob")
    _chat(bob, "conv-bob")
    _store_one(bob, "conv-bob")
    directory = _conv_dir("bob", "conv-bob")
    uid = _uid("bob")
    with db.connection() as con:
        con.execute("DELETE FROM users WHERE id = %s", (uid,))
    assert _rows("conv-bob") == []
    assert os.path.isdir(directory)
    _age(settings.chat_media_dir)
    assert chat_media.reap_once() == {"rows": 0, "dirs": 1}
    assert os.listdir(directory) == []


# --------------------------------------------------------- admin inspection --


def _admin_path(user_id: int, conv: str, attachment_id: str = "att-00000001", size: str = "full") -> str:
    return f"/admin/api/members/{user_id}/chat-media/{conv}/{attachment_id}?size={size}"


def _audit_rows() -> list:
    with db.connection() as con:
        return [
            dict(r)
            for r in con.execute(
                "SELECT actor_user_id, target_user_id, resource_type, resource_id, meta "
                "FROM audit_events WHERE action = 'admin_viewed_chat_media' ORDER BY id"
            ).fetchall()
        ]


def test_an_admin_reads_a_members_picture_audited_and_never_a_super_admins(login_client):
    admin = login_client("adm", role="admin")
    boss = login_client("boss", role="super_admin")
    bob = login_client("bob")
    _chat(boss, "conv-boss")
    _store_one(boss, "conv-boss")
    _chat(bob, "conv-bob")
    payload = _png(colour=(7, 7, 7))
    item = _store_one(bob, "conv-bob", payload=payload)

    # The rank rule of every inspection route: an admin does not read a
    # super admin, and the refusal leaves no audit event.
    assert admin.get(_admin_path(_uid("boss"), "conv-boss")).status_code == 404
    assert _audit_rows() == []
    # A member is not on the admin surface at all.
    assert bob.get(_admin_path(_uid("bob"), "conv-bob")).status_code == 404

    resp = admin.get(_admin_path(_uid("bob"), "conv-bob"))
    assert resp.status_code == 200, resp.text
    assert resp.content == payload
    owner = bob.get("/chat-media/conv-bob/att-00000001")
    for header in (
        "content-type", "x-content-type-options", "content-security-policy",
        "content-disposition", "cache-control", "etag",
    ):
        assert resp.headers[header] == owner.headers[header], header
    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == _uid("adm")
    assert rows[0]["target_user_id"] == _uid("bob")
    assert rows[0]["resource_type"] == "chat_media"
    assert rows[0]["resource_id"] == item["media_id"]
    assert rows[0]["meta"] == {"conversation_id": "conv-bob", "size": "full"}

    # A 304 confirms the copy the admin already holds: audited too.
    again = admin.get(
        _admin_path(_uid("bob"), "conv-bob"), headers={"If-None-Match": f'"{item["sha256"]}"'}
    )
    assert again.status_code == 304
    assert len(_audit_rows()) == 2
    # Nothing there: a 404 and no event.
    assert admin.get(_admin_path(_uid("bob"), "conv-bob", "att-nothing-here")).status_code == 404
    assert admin.get(_admin_path(_uid("bob"), "conv-boss")).status_code == 404
    assert len(_audit_rows()) == 2

    # Owner decision 2026-09-14: a super admin inspects every member.
    assert boss.get(_admin_path(_uid("bob"), "conv-bob", size="thumb")).status_code == 200
    assert len(_audit_rows()) == 3


# ----------------------------------------------------------------- sharing --


def _say(cid: str, role: str, content: str, meta=None) -> None:
    with db.connection() as con:
        con.execute(
            "INSERT INTO messages (conversation_id, role, content, meta, created_at) "
            "VALUES (%s, %s, %s, %s, now())",
            (cid, role, content, db._json_param(meta or {})),
        )


def test_a_chat_with_stored_photos_is_private_to_the_share_policy(login_client):
    owner = login_client("olive")
    db.create_conversation(_uid("olive"), "conv-photo-share", "T")
    _say("conv-photo-share", "user", "what is in this photo?", {"images": [{"attachment_id": "att-00000001"}]})
    _say("conv-photo-share", "assistant", "a cat on a sofa", {"route": "vision"})
    resp = owner.post("/conversations/conv-photo-share/share", json={"visibility": "public", "expiry": "7d"})
    assert resp.status_code == 422, resp.text
    assert "uploaded photos" in resp.json()["detail"]

    # The control: the same chat without the reference may be shared.
    db.create_conversation(_uid("olive"), "conv-no-photo", "T")
    _say("conv-no-photo", "user", "what is a sofa?")
    _say("conv-no-photo", "assistant", "a long seat", {"route": "chat"})
    ok = owner.post("/conversations/conv-no-photo/share", json={"visibility": "public", "expiry": "7d"})
    assert ok.status_code in (200, 201), ok.text

    assert sharing.PRIVATE_META_KEYS["images"] == "uploaded photos"
    verdict = sharing.evaluate(
        [
            {"role": "user", "content": "q", "meta": {"images": [{"attachment_id": "att-00000001"}]}},
            {"role": "assistant", "content": "a", "meta": {"route": "vision"}},
        ]
    )
    assert verdict.public_allowed is False
