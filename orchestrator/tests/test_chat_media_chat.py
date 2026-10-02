"""/chat and chat media (V44, 2026-10-02; docs/chat-media/CONTRACT.md §5-6).

THE DEFECT, ON THE CHAT PATH. The bytes of a photo reached this server only
inline in the /chat body, and nothing kept them: `_request_snapshot` strips
them and the V41 row keeps the latest picture for two hours. So a second
device had no photo to show, a regenerate on a device without the bytes sent
no image at all, and a follow-up the next morning found no picture.

What is pinned here, against the real /chat handler with the engines faked
(what is under test is what the turn stores and what it hands the engine,
never the model):
  * `image_ids` stores every inline picture BEHIND the turn, on the vision
    route and on a document + picture turn alike, and a storage failure never
    fails the chat;
  * `image_refs` loads stored pictures into the same path as inline ones,
    counts against the same five-picture cap, and answers 422
    image_ref_missing before anything starts when one cannot be loaded;
  * the request snapshot holds ids and never bytes;
  * image_memory reads the stored picture for a follow-up after its V41 row
    expired and after a restart, and only then.
"""
from __future__ import annotations

import base64
import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import chat_media, db, metrics
from app.config import settings
from app.engines import image_memory
from app.engines import router as router_engine
from app.engines import vision
from app.main import app

TURN1 = "From this note: what number do I call, and how much is still owed after the deposit?"
ANSWER1 = "**Number to call:** +31 6 24 88 17 05. **Still owed:** 3,570 EUR."
FOLLOW = "What else is written in the photo?"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # The free-space floor is measured on whatever disk runs the suite (see
    # test_chat_media_api._room); its own test is there.
    monkeypatch.setattr(settings, "chat_media_min_free_gib", 0.0)
    monkeypatch.delenv("IMAGE_MEMORY_STORE_FALLBACK", raising=False)
    image_memory.clear()
    metrics.reset()
    yield
    image_memory.clear()
    metrics.reset()


@pytest.fixture()
def engines(monkeypatch):
    """Every engine these turns can reach, faked to record what it was handed."""
    seen: dict = {"vision": [], "document": [], "chat": []}

    async def fake_vision(message, images, history, emit, *, effort="think", max_tokens=None, conversation_id=None):
        seen["vision"].append({"message": message, "images": list(images)})
        text = ANSWER1 if message == TURN1 else "vision answer"
        await emit("token", {"text": text})
        await emit("meta", {"route": "vision"})
        return text

    async def fake_chat(text, history, emit, **kw):
        seen["chat"].append(text)
        await emit("token", {"text": "chat answer"})
        await emit("meta", {"route": "chat"})
        return "chat answer"

    async def fake_document(text, docs, history, emit, **kw):
        seen["document"].append({"docs": list(docs), "extra_images": list(kw.get("extra_images") or [])})
        await emit("token", {"text": "document answer"})
        await emit("meta", {"route": "document"})
        return "document answer"

    async def route_chat(message, has_image=False, history=()):
        return "vision" if has_image else "chat"

    from app.engines import chat as chat_engine
    from app.engines import document as document_engine

    monkeypatch.setattr(vision, "run_vision_engine", fake_vision)
    monkeypatch.setattr(chat_engine, "run_chat_engine", fake_chat)
    monkeypatch.setattr(document_engine, "run_pdf_engine_multi", fake_document)
    monkeypatch.setattr(router_engine, "route_request", route_chat)
    return seen


# ------------------------------------------------------------------ helpers --


def _png(colour=(200, 30, 30), size=(64, 48)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, format="PNG")
    return out.getvalue()


def _jpeg(colour=(20, 120, 220), size=(64, 48)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, format="JPEG", quality=90)
    return out.getvalue()


def _data_url(payload: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(payload).decode("ascii")


def _decoded(url: str) -> bytes:
    return base64.b64decode(url.split(",", 1)[1])


def _events(resp):
    out = []
    for block in resp.text.split("\n\n"):
        lines = [line for line in block.split("\n") if line and not line.startswith(":")]
        if len(lines) < 2:
            continue
        out.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return out


def _route(resp) -> str:
    metas = [d for e, d in _events(resp) if e == "meta"]
    return (metas[-1] if metas else {}).get("route", "")


def _answer(resp) -> str:
    return "".join(d.get("text", "") for e, d in _events(resp) if e == "token")


def _rows(conv: str) -> list:
    with db.connection() as con:
        return [
            dict(r)
            for r in con.execute(
                "SELECT * FROM chat_media WHERE conversation_id = %s ORDER BY attachment_id", (conv,)
            ).fetchall()
        ]


def _wait_rows(conv: str, n: int, timeout: float = 15.0) -> list:
    """The store runs BEHIND the turn, so a test waits for it rather than
    assuming the response waited (which is exactly what it must not do)."""
    deadline = time.monotonic() + timeout
    rows = _rows(conv)
    while len(rows) < n and time.monotonic() < deadline:
        time.sleep(0.05)
        rows = _rows(conv)
    return rows


def _wait_settled(timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while chat_media._inflight and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not chat_media._inflight, "a background store never finished"


def _counter(name: str, **labels) -> float:
    key = tuple(sorted(labels.items()))
    return metrics._counters.get(name, {}).get(key, 0.0)


def _chat(client, **body):
    body.setdefault("effort", "think")
    return client.post("/chat", json=body)


def _upload(client, conv: str, attachment_id: str, payload: bytes, name: str = "p.png", ctype: str = "image/png"):
    resp = client.post(
        f"/chat-media/{conv}",
        files=[("file", (name, payload, ctype))],
        data={"attachment_id": [attachment_id]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["items"][0]


def _say(cid: str, role: str, content: str, meta=None) -> None:
    with db.connection() as con:
        con.execute(
            "INSERT INTO messages (conversation_id, role, content, meta, created_at) "
            "VALUES (%s, %s, %s, %s, now())",
            (cid, role, content, db._json_param(meta)),
        )


def _restart() -> None:
    """A new process: image_memory's in-process dicts empty, nothing else
    (tests/test_image_memory_restart.py's definition, kept identical)."""
    image_memory._remembered_images.clear()
    image_memory._latest.clear()


# ----------------------------------------------------- storing inline images --


def test_a_vision_turn_stores_its_pictures_behind_the_turn(engines, as_user):
    alice = as_user("alice")
    png, jpeg = _png(), _jpeg()
    with TestClient(app) as client:
        resp = _chat(
            client,
            message="compare these",
            conversation_id="conv-vision",
            images=[_data_url(png), base64.b64encode(jpeg).decode("ascii")],
            image_ids=["att-vision-01", "att-vision-02"],
        )
        assert resp.status_code == 200, resp.text
        assert _route(resp) == "vision"
        rows = _wait_rows("conv-vision", 2)
        assert [(r["attachment_id"], r["mime"], r["source"]) for r in rows] == [
            ("att-vision-01", "image/png", "chat"),
            ("att-vision-02", "image/jpeg", "chat"),
        ]
        assert all(r["user_id"] == int(alice["id"]) for r in rows)
        # The bytes on disk ARE what was sent, readable on any device.
        assert client.get("/chat-media/conv-vision/att-vision-01").content == png
        assert client.get("/chat-media/conv-vision/att-vision-02").content == jpeg
    assert _counter("chat_media_writes_total", source="chat", result="stored") == 2


def test_the_turn_never_waits_for_the_store(engines, as_user, monkeypatch):
    """Nothing is added to the latency of a send (owner, 2026-10-02): the
    whole answer streams while the store is still blocked."""
    import threading

    as_user("alice")
    release = threading.Event()
    real = chat_media._store_inline_one

    def held(*args):
        release.wait(15)
        return real(*args)

    monkeypatch.setattr(chat_media, "_store_inline_one", held)
    with TestClient(app) as client:
        try:
            resp = _chat(
                client, message="what is it", conversation_id="conv-held",
                images=[_data_url(_png())], image_ids=["att-held-0001"],
            )
            assert resp.status_code == 200
            assert _answer(resp) == "vision answer"
            assert _rows("conv-held") == []  # answered; the store has not run
        finally:
            release.set()
        assert [r["attachment_id"] for r in _wait_rows("conv-held", 1)] == ["att-held-0001"]


def test_the_single_image_spelling_is_stored_under_image_ids_0(engines, as_user):
    as_user("alice")
    with TestClient(app) as client:
        resp = _chat(
            client, message="what is it", conversation_id="conv-single",
            image=_data_url(_png()), image_ids=["att-single-1"],
        )
        assert resp.status_code == 200
        assert [r["attachment_id"] for r in _wait_rows("conv-single", 1)] == ["att-single-1"]


def test_a_document_and_picture_turn_stores_the_picture_too(engines, as_user):
    """The hook is at intake, not in the vision branch: this turn routes to
    the document engine and its picture must still show on other devices."""
    as_user("alice")
    png = _png(colour=(1, 200, 1))
    with TestClient(app) as client:
        resp = _chat(
            client,
            message="compare the chart to the notes",
            conversation_id="conv-doc-image",
            pdf=base64.b64encode(b"Quarterly notes: revenue up 12%.").decode("ascii"),
            pdf_filename="notes.txt",
            images=[_data_url(png)],
            image_ids=["att-doc-img-1"],
        )
        assert resp.status_code == 200, resp.text
        assert _route(resp) == "document"
        assert engines["document"] and engines["document"][0]["extra_images"]
        rows = _wait_rows("conv-doc-image", 1)
        assert [r["attachment_id"] for r in rows] == ["att-doc-img-1"]
        assert client.get("/chat-media/conv-doc-image/att-doc-img-1").content == png


def test_a_storage_failure_never_fails_the_chat(engines, as_user, monkeypatch):
    as_user("alice")

    def broken(data):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(chat_media, "inspect", broken)
    with TestClient(app) as client:
        resp = _chat(
            client, message="what is it", conversation_id="conv-broken",
            images=[_data_url(_png())], image_ids=["att-broken-1"],
        )
        assert resp.status_code == 200
        assert _answer(resp) == "vision answer"
        _wait_settled()
    assert _rows("conv-broken") == []
    assert _counter("chat_media_writes_total", source="chat", result="error") == 1


def test_a_picture_the_store_refuses_is_counted_and_the_chat_goes_on(engines, as_user):
    as_user("alice")
    svg = "data:image/svg+xml;base64," + base64.b64encode(b"<svg><script>1</script></svg>").decode()
    with TestClient(app) as client:
        resp = _chat(
            client, message="what is it", conversation_id="conv-svg",
            images=[svg, _data_url(_png())], image_ids=["att-svg-0001", "att-png-0001"],
        )
        assert resp.status_code == 200
        assert _route(resp) == "vision"
        _wait_settled()
    assert [r["attachment_id"] for r in _rows("conv-svg")] == ["att-png-0001"]
    assert _counter("chat_media_writes_total", source="chat", result="unsupported") == 1


def test_mismatched_image_ids_store_nothing_and_are_never_a_4xx(engines, as_user):
    as_user("alice")
    with TestClient(app) as client:
        resp = _chat(
            client, message="compare", conversation_id="conv-mismatch",
            images=[_data_url(_png()), _data_url(_jpeg(), "image/jpeg")], image_ids=["att-only-one"],
        )
        assert resp.status_code == 200
        assert _route(resp) == "vision"
        _wait_settled()
    assert _rows("conv-mismatch") == []


def test_a_malformed_image_id_is_a_422(engines, as_user):
    as_user("alice")
    with TestClient(app) as client:
        for ids in (["no"], ["../../etc"], [f"att-{i:08d}" for i in range(6)]):
            resp = _chat(
                client, message="x", conversation_id="conv-bad-id",
                images=[_data_url(_png())], image_ids=ids,
            )
            assert resp.status_code == 422, ids
    assert not engines["vision"]


def test_a_bare_call_with_no_conversation_stores_nothing(engines, as_user):
    alice = as_user("alice")
    with TestClient(app) as client:
        resp = _chat(client, message="what is it", images=[_data_url(_png())], image_ids=["att-bare-0001"])
        assert resp.status_code == 200
        _wait_settled()
    assert _rows(f"u{alice['id']}-default") == []


# ----------------------------------------------------------- stored by ref --


def test_image_refs_load_the_stored_bytes_into_the_inline_path(engines, as_user):
    """A regenerate on a device that never held the bytes: the stored picture
    reaches the engine exactly as an inline one would, the full file and not
    the thumbnail, stored pictures first."""
    as_user("alice")
    stored = _jpeg(size=(900, 700))
    inline = _png()
    with TestClient(app) as client:
        assert client.post("/history/conversations", json={"id": "conv-refs", "title": "t"}).status_code == 200
        _upload(client, "conv-refs", "att-stored-1", stored, name="s.jpg", ctype="image/jpeg")
        resp = _chat(client, message="what is in it?", conversation_id="conv-refs", image_refs=["att-stored-1"])
        assert resp.status_code == 200, resp.text
        assert _route(resp) == "vision"
        handed = engines["vision"][-1]["images"]
        assert len(handed) == 1
        assert handed[0].startswith("data:image/jpeg;base64,")
        assert _decoded(handed[0]) == stored

        resp = _chat(
            client, message="and with this one?", conversation_id="conv-refs",
            image_refs=["att-stored-1"], images=[_data_url(inline)], image_ids=["att-new-0001"],
        )
        assert resp.status_code == 200
        handed = engines["vision"][-1]["images"]
        assert [_decoded(i) for i in handed] == [stored, inline]
        # The inline one is stored; the ref is not stored a second time.
        assert [r["attachment_id"] for r in _wait_rows("conv-refs", 2)] == ["att-new-0001", "att-stored-1"]


def test_a_ref_only_turn_needs_no_message_text(engines, as_user):
    as_user("alice")
    with TestClient(app) as client:
        _upload(client, "conv-refs-only", "att-stored-1", _png())
        resp = _chat(client, conversation_id="conv-refs-only", image_refs=["att-stored-1"])
        assert resp.status_code == 200, resp.text
        assert _route(resp) == "vision"
        # The engine still gets a question, as for an inline picture with no
        # words (the placeholder was decided before the refs were loaded).
        assert engines["vision"][-1]["message"] == "Analyze the attached image."


def test_a_missing_ref_is_422_before_anything_starts(engines, as_user):
    as_user("bob")
    with TestClient(app) as client:
        # Bob's picture, under Bob's own chat: never Alice's to load.
        _upload(client, "conv-bob", "att-bobs-0001", _png(colour=(0, 0, 0)))
        as_user("alice")
        _upload(client, "conv-missing", "att-stored-1", _png())
        resp = _chat(
            client,
            message="regenerate",
            conversation_id="conv-missing",
            intent_id="intent-missing-1",
            image_refs=["att-stored-1", "att-gone-0001", "att-bobs-0001"],
        )
        assert resp.status_code == 422, resp.text
        assert resp.json() == {
            "detail": {"code": "image_ref_missing", "missing": ["att-gone-0001", "att-bobs-0001"]}
        }
        assert resp.headers["content-type"].startswith("application/json")
    assert not engines["vision"]
    with db.connection() as con:
        assert con.execute("SELECT count(*) AS n FROM chat_requests").fetchone()["n"] == 0


def test_a_ref_whose_file_is_gone_is_missing(engines, as_user):
    import os

    alice = as_user("alice")
    with TestClient(app) as client:
        item = _upload(client, "conv-file-gone", "att-stored-1", _png())
        os.unlink(
            os.path.join(
                settings.chat_media_dir, str(alice["id"]), "conv-file-gone", item["media_id"], "full.png"
            )
        )
        resp = _chat(client, message="again", conversation_id="conv-file-gone", image_refs=["att-stored-1"])
        assert resp.status_code == 422
        assert resp.json()["detail"]["missing"] == ["att-stored-1"]


def test_refs_and_inline_pictures_share_the_five_picture_cap(engines, as_user):
    as_user("alice")
    with TestClient(app) as client:
        for i in range(3):
            _upload(client, "conv-cap", f"att-stored-{i}", _png(colour=(i, 0, 0)))
        inline = [_data_url(_png(colour=(0, i, 0))) for i in range(3)]
        refs = [f"att-stored-{i}" for i in range(3)]
        over = _chat(client, message="all", conversation_id="conv-cap", images=inline, image_refs=refs)
        assert over.status_code == 422
        assert "at most 5 images" in over.text
        ok = _chat(client, message="all", conversation_id="conv-cap", images=inline[:2], image_refs=refs)
        assert ok.status_code == 200, ok.text
        assert len(engines["vision"][-1]["images"]) == 5


def test_an_account_without_attachments_cannot_read_a_ref_into_a_turn(engines, as_user, monkeypatch):
    from app.authn import features as feature_access

    as_user("alice")
    with TestClient(app) as client:
        _upload(client, "conv-gated", "att-stored-1", _png())
        real = feature_access.allowed
        monkeypatch.setattr(
            feature_access,
            "allowed",
            lambda features, feature: False
            if feature == feature_access.Feature.ATTACHMENTS
            else real(features, feature),
        )
        resp = _chat(client, message="what is it", conversation_id="conv-gated", image_refs=["att-stored-1"])
        assert resp.status_code == 200
        assert not engines["vision"]


# ---------------------------------------------------------------- snapshot --


def test_the_request_snapshot_keeps_ids_and_never_bytes(engines, as_user):
    as_user("alice")
    png = _png()
    with TestClient(app) as client:
        _upload(client, "conv-snap", "att-stored-1", _png(colour=(9, 9, 9)))
        assert _chat(
            client, message="inline", conversation_id="conv-snap", intent_id="intent-inline-1",
            images=[_data_url(png)], image_ids=["att-inline-1"],
        ).status_code == 200
        assert _chat(
            client, message="by ref", conversation_id="conv-snap", intent_id="intent-refs-1",
            image_refs=["att-stored-1"],
        ).status_code == 200
        _wait_settled()
    with db.connection() as con:
        rows = {
            r["intent_id"]: r
            for r in con.execute("SELECT intent_id, request, resumable FROM chat_requests").fetchall()
        }
    inline_row, ref_row = rows["intent-inline-1"], rows["intent-refs-1"]
    assert inline_row["request"]["image_ids"] == ["att-inline-1"]
    assert ref_row["request"]["image_refs"] == ["att-stored-1"]
    for row in (inline_row, ref_row):
        for field in ("images", "image", "image_base64"):
            assert field not in row["request"], field
        assert "base64," not in json.dumps(row["request"])
    # Inline bytes cannot be resumed server-side; stored pictures can, from
    # their ids.
    assert inline_row["resumable"] is False
    assert ref_row["resumable"] is True


# --------------------------------------------- image_memory's store fallback --


def _stored_image_turn(client, conv: str, payload: bytes, attachment_id: str = "att-note-0001") -> None:
    assert client.post("/history/conversations", json={"id": conv, "title": "note"}).status_code == 200
    _upload(client, conv, attachment_id, payload)
    _say(conv, "user", TURN1, {"images": [{"attachment_id": attachment_id, "name": "note.png"}]})
    _say(conv, "assistant", ANSWER1, {"route": "vision"})


def test_the_follow_up_reads_the_stored_picture_after_the_v41_row_expired(engines, as_user):
    alice = as_user("alice")
    payload = _png(colour=(10, 200, 10))
    with TestClient(app) as client:
        _stored_image_turn(client, "conv-expired", payload)
        # The V41 row: past its two hours, holding some other picture.
        db.save_conversation_image(int(alice["id"]), "conv-expired", [_data_url(_png(colour=(1, 1, 1)))], "old")
        with db.connection() as con:
            con.execute(
                "UPDATE conversation_images SET created_at = now() - interval '3 hours' "
                "WHERE conversation_id = 'conv-expired'"
            )
        _restart()
        resp = _chat(client, message=FOLLOW, conversation_id="conv-expired")
        assert resp.status_code == 200, resp.text
        assert _route(resp) == "vision"
        assert [_decoded(i) for i in engines["vision"][-1]["images"]] == [payload]


def test_the_follow_up_reads_the_stored_picture_after_a_restart(engines, as_user):
    as_user("alice")
    payload = _png(colour=(10, 10, 200))
    with TestClient(app) as client:
        _stored_image_turn(client, "conv-restart", payload)
        _restart()
        with db.connection() as con:
            assert con.execute("SELECT count(*) AS n FROM conversation_images").fetchone()["n"] == 0
        resp = _chat(client, message=FOLLOW, conversation_id="conv-restart")
        assert resp.status_code == 200
        assert _route(resp) == "vision"
        assert [_decoded(i) for i in engines["vision"][-1]["images"]] == [payload]
        # And a second follow-up in the same process reads the process copy
        # (one that names the picture: "the second line" fires only on the
        # turn right after it, V41's own rule, and this is the second).
        _chat(client, message="Is the date in the photo legible?", conversation_id="conv-restart")
        assert len(engines["vision"]) == 2
        assert [_decoded(i) for i in engines["vision"][-1]["images"]] == [payload]


def test_the_fallback_switch_restores_the_old_behaviour(engines, as_user, monkeypatch):
    as_user("alice")
    monkeypatch.setenv("IMAGE_MEMORY_STORE_FALLBACK", "0")
    with TestClient(app) as client:
        _stored_image_turn(client, "conv-switch", _png())
        _restart()
        resp = _chat(client, message=FOLLOW, conversation_id="conv-switch")
        assert resp.status_code == 200
        assert _route(resp) == "chat"
    assert not engines["vision"]


def test_a_chat_with_no_stored_picture_routes_exactly_as_before(engines, as_user):
    as_user("alice")
    with TestClient(app) as client:
        assert client.post("/history/conversations", json={"id": "conv-plain", "title": "t"}).status_code == 200
        _say("conv-plain", "user", TURN1, {"images": [{"attachment_id": "att-never-stored"}]})
        _say("conv-plain", "assistant", ANSWER1)
        _restart()
        resp = _chat(client, message=FOLLOW, conversation_id="conv-plain")
        assert resp.status_code == 200
        assert _route(resp) == "chat"
    assert not engines["vision"]


def test_another_account_never_gets_the_stored_picture(as_user):
    as_user("alice")
    with TestClient(app) as client:
        _stored_image_turn(client, "conv-mine", _png())
    bob = as_user("bob")
    assert chat_media.latest_turn_images(int(bob["id"]), "conv-mine") is None


def test_latest_turn_images_reads_the_newest_picture_turn_and_counts_the_turns_since(as_user):
    alice = as_user("alice")
    uid = int(alice["id"])
    old, new = _png(colour=(1, 2, 3)), _png(colour=(4, 5, 6))
    with TestClient(app) as client:
        assert client.post("/history/conversations", json={"id": "conv-count", "title": "t"}).status_code == 200
        _upload(client, "conv-count", "att-old-0001", old)
        _upload(client, "conv-count", "att-new-0001", new)
    _say("conv-count", "user", "First Photo", {"images": [{"attachment_id": "att-old-0001"}]})
    _say("conv-count", "assistant", "Old answer")
    _say("conv-count", "user", "Second PHOTO question", {"images": [{"attachment_id": "att-new-0001"}]})
    _say("conv-count", "assistant", "New ANSWER")
    _say("conv-count", "user", "a text turn")
    _say("conv-count", "assistant", "a text answer")
    found = chat_media.latest_turn_images(uid, "conv-count")
    assert [_decoded(i) for i in found["images"]] == [new]
    assert found["context"] == "second photo question\nnew answer"
    assert found["turns_after"] == 1
    # The turn being asked now, pushed by the browser before /chat read the
    # thread, is not a turn SINCE the picture.
    _say("conv-count", "user", "what was the total again?")
    assert chat_media.latest_turn_images(uid, "conv-count")["turns_after"] == 1


# ------------------------------------- the store fallback follows the branch --


def _branch(self_id: str, parent: str) -> dict:
    return {"branch": {"self": self_id, "parent": parent}}


def test_a_photo_on_a_branch_the_person_edited_away_is_never_read_into_a_turn(engines, as_user):
    """The stored list is flat and append-only (lib/branching.ts): editing an
    EARLIER message adds a sibling and keeps the old versions. The fallback
    took the newest picture message in that list, so a photo on a branch
    nobody sees any more was handed to the model ("what does the photo
    show?" -> vision with it). It now takes only a picture on the path the
    browser sent."""
    as_user("alice")
    photo = _png(colour=(9, 99, 9))
    with TestClient(app) as client:
        assert client.post("/history/conversations", json={"id": "conv-branch", "title": "t"}).status_code == 200
        _upload(client, "conv-branch", "att-branch-01", photo)
        _say("conv-branch", "user", "hi", _branch("b-1", ""))
        _say("conv-branch", "assistant", "hello", _branch("b-2", "b-1"))
        _say("conv-branch", "user", TURN1, {"images": [{"attachment_id": "att-branch-01"}], **_branch("b-3", "b-2")})
        _say("conv-branch", "assistant", ANSWER1, {"route": "vision", **_branch("b-4", "b-3")})
        # The person edits the FIRST message: the photo turn leaves the path.
        _say("conv-branch", "user", "tell me about paris", _branch("b-5", ""))
        _say("conv-branch", "assistant", "paris is a city", _branch("b-6", "b-5"))
        _restart()
        edited_path = [
            {"role": "user", "content": "tell me about paris"},
            {"role": "assistant", "content": "paris is a city"},
            {"role": "user", "content": FOLLOW},
        ]
        resp = _chat(client, message=FOLLOW, conversation_id="conv-branch", messages=edited_path)
        assert resp.status_code == 200
        assert _route(resp) == "chat"
        assert not engines["vision"]

        # Switched back to the photo's branch, the same question reads it.
        _restart()
        photo_path = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": TURN1},
            {"role": "assistant", "content": ANSWER1},
            {"role": "user", "content": FOLLOW},
        ]
        resp = _chat(client, message=FOLLOW, conversation_id="conv-branch", messages=photo_path)
        assert _route(resp) == "vision"
        assert [_decoded(i) for i in engines["vision"][-1]["images"]] == [photo]


def test_a_photo_with_no_words_is_found_on_the_path_by_its_answer(engines, as_user):
    # The browser drops an empty turn from `messages`, so a photo sent with
    # no words is placed on the path by the answer stored under it.
    as_user("alice")
    photo = _png(colour=(90, 9, 9))
    with TestClient(app) as client:
        assert client.post("/history/conversations", json={"id": "conv-wordless", "title": "t"}).status_code == 200
        _upload(client, "conv-wordless", "att-wordless1", photo)
        _say("conv-wordless", "user", "", {"images": [{"attachment_id": "att-wordless1"}]})
        _say("conv-wordless", "assistant", ANSWER1, {"route": "vision"})
        _restart()
        resp = _chat(
            client,
            message=FOLLOW,
            conversation_id="conv-wordless",
            messages=[{"role": "assistant", "content": ANSWER1}, {"role": "user", "content": FOLLOW}],
        )
        assert _route(resp) == "vision"
        assert [_decoded(i) for i in engines["vision"][-1]["images"]] == [photo]


def test_the_visible_path_match_and_its_turn_count(as_user):
    alice = as_user("alice")
    uid = int(alice["id"])
    photo = _png(colour=(3, 33, 133))
    with TestClient(app) as client:
        assert client.post("/history/conversations", json={"id": "conv-path", "title": "t"}).status_code == 200
        _upload(client, "conv-path", "att-path-0001", photo)
    _say("conv-path", "user", "Read this", {"images": [{"attachment_id": "att-path-0001"}], **_branch("p-1", "")})
    _say("conv-path", "assistant", "First answer", _branch("p-2", "p-1"))
    # "Try again" on that answer: a sibling stored at the END of the list.
    _say("conv-path", "user", "unrelated", _branch("p-3", "p-2"))
    _say("conv-path", "assistant", "Second answer", _branch("p-4", "p-1"))
    path = [
        # A pasted block is folded in front of the words by the browser.
        ("user", "pasted log line\n\nRead this"),
        ("assistant", "Second answer"),
        ("user", "a text turn"),
        ("assistant", "a text answer"),
        ("user", "what did it say?"),  # the question being asked now
    ]
    found = chat_media.latest_turn_images(uid, "conv-path", path)
    assert [_decoded(i) for i in found["images"]] == [photo]
    assert found["context"] == "read this\nsecond answer"
    assert found["turns_after"] == 1
    # Words that only END the same way are a different turn.
    assert chat_media.latest_turn_images(uid, "conv-path", [("user", "please Read this"), ("user", "q")]) is None
    # No history sent: the stored order alone, as before.
    assert chat_media.latest_turn_images(uid, "conv-path")["images"]
    assert chat_media.latest_turn_images(uid, "conv-path", []) is None


def test_a_wordless_photo_is_found_by_a_regenerated_answer_stored_later(as_user):
    alice = as_user("alice")
    uid = int(alice["id"])
    photo = _png(colour=(13, 13, 113))
    with TestClient(app) as client:
        assert client.post("/history/conversations", json={"id": "conv-regen", "title": "t"}).status_code == 200
        _upload(client, "conv-regen", "att-regen-001", photo)
    _say("conv-regen", "user", "", {"images": [{"attachment_id": "att-regen-001"}], **_branch("r-1", "")})
    _say("conv-regen", "assistant", "First answer", _branch("r-2", "r-1"))
    _say("conv-regen", "user", "more", _branch("r-3", "r-2"))
    _say("conv-regen", "assistant", "more answer", _branch("r-4", "r-3"))
    # "Try again" on the photo's answer, stored at the end; it is what shows.
    _say("conv-regen", "assistant", "Regenerated answer", _branch("r-5", "r-1"))
    found = chat_media.latest_turn_images(uid, "conv-regen", [("assistant", "Regenerated answer"), ("user", "q")])
    assert [_decoded(i) for i in found["images"]] == [photo]
    assert found["context"] == "\nregenerated answer"
    assert found["turns_after"] == 0
    # An answer that is not stored under the photo does not place it.
    assert chat_media.latest_turn_images(uid, "conv-regen", [("assistant", "more answer"), ("user", "q")]) is None
