"""Chat media routes (V44, 2026-10-02; docs/chat-media/CONTRACT.md §3-4, §11).

THE DEFECT. A photo sent from a phone showed on the phone and on no other
device: the only lasting copy was the sending browser's IndexedDB. These are
the routes that store a chat's pictures on the server and read them back,
and every test below fails on the parent commit, where none of them exist.

What is pinned: who may reach a picture (the owner; never another account,
never the F034 reserved key, and one 404 body for every refusal), what is
accepted (only rasters whose magic bytes AND decode agree), the limits, the
idempotent first-write-wins store, the thumbnail, every header of the byte
route, conditional requests, 410 for a row that outlived its file, 507 under
the free-space floor, and the closed metric registry.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import io
import os
import random
import shutil
import stat
import threading
import time

import pytest
from PIL import Image

from app import chat_media, db, metrics
from app.config import settings

PNG_CTYPE = "image/png"


@pytest.fixture(autouse=True)
def _room(monkeypatch):
    """The 250 GiB floor is measured on whatever disk runs the suite, and a
    CI runner has less than that free (the Files watermark hung PR #66 the
    same way). The floor has its own test below."""
    monkeypatch.setattr(settings, "chat_media_min_free_gib", 0.0)


# ------------------------------------------------------------------ helpers --


def _png(width: int = 64, height: int = 48, colour=(200, 30, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, format="PNG")
    return out.getvalue()


def _jpeg(width: int = 64, height: int = 48, *, orientation: int | None = None, noise: bool = False) -> bytes:
    image = (
        Image.effect_noise((width, height), 64).convert("RGB")
        if noise
        else Image.new("RGB", (width, height), (20, 120, 220))
    )
    out = io.BytesIO()
    kwargs = {"quality": 92}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kwargs["exif"] = exif.tobytes()
    image.save(out, format="JPEG", **kwargs)
    return out.getvalue()


def _uid(username: str) -> int:
    return int(db.get_user_by_username(username)["id"])


def _post(client, conv: str, files, ids, *, source: str | None = None):
    """files: [(filename, bytes, content type)]; ids: the attachment_id parts."""
    data: dict = {"attachment_id": list(ids)}
    if source is not None:
        data["source"] = source
    return client.post(
        f"/chat-media/{conv}",
        files=[("file", (name, payload, ctype)) for name, payload, ctype in files],
        data=data,
    )


def _store_one(client, conv: str, attachment_id: str = "att-00000001", payload: bytes | None = None, name="a.png"):
    resp = _post(client, conv, [(name, payload if payload is not None else _png(), PNG_CTYPE)], [attachment_id])
    assert resp.status_code == 200, resp.text
    return resp.json()["items"][0]


def _rows(conv: str) -> list:
    with db.connection() as con:
        return [
            dict(r)
            for r in con.execute(
                "SELECT * FROM chat_media WHERE conversation_id = %s ORDER BY created_at", (conv,)
            ).fetchall()
        ]


def _chat(client, conv: str) -> None:
    resp = client.post("/history/conversations", json={"id": conv, "title": "photos"})
    assert resp.status_code == 200, resp.text


# ------------------------------------------------------- ownership and IDOR --


def test_only_the_owner_reaches_a_picture_and_every_refusal_reads_the_same(login_client):
    alice = login_client("alice")
    bob = login_client("bob")
    _chat(alice, "conv-alice")
    stored = _store_one(alice, "conv-alice")
    assert stored["created"] is True
    assert alice.get("/chat-media/conv-alice/att-00000001").status_code == 200
    assert [i["attachment_id"] for i in alice.get("/chat-media/conv-alice").json()["items"]] == [
        "att-00000001"
    ]

    never = alice.get("/chat-media/conv-alice/att-never-existed")
    assert never.status_code == 404
    refusal = never.json()

    # Another account: the list, both renditions, and an upload INTO the chat.
    for resp in (
        bob.get("/chat-media/conv-alice"),
        bob.get("/chat-media/conv-alice/att-00000001"),
        bob.get("/chat-media/conv-alice/att-00000001?size=thumb"),
        _post(bob, "conv-alice", [("b.png", _png(), PNG_CTYPE)], ["att-bob-0001"]),
    ):
        assert resp.status_code == 404, resp.text
        assert resp.json() == refusal
    assert len(_rows("conv-alice")) == 1

    # F034: the reserved bare-call key is refused by SHAPE, whoever asks,
    # with the same body as a picture that never existed.
    reserved = f"u{_uid('alice')}-default"
    for resp in (
        alice.get(f"/chat-media/{reserved}"),
        alice.get(f"/chat-media/{reserved}/att-00000001"),
        _post(alice, reserved, [("a.png", _png(), PNG_CTYPE)], ["att-00000002"]),
    ):
        assert resp.status_code == 404, resp.text
        assert resp.json() == refusal
    assert _rows(reserved) == []


def test_a_brand_new_chat_with_no_row_works_for_each_person_s_own_pictures_only(login_client):
    """/chat and the upload route may store a picture before the browser's
    first history push creates the chat's row. Such a chat is nobody's yet:
    the route must not claim it, and each person sees only their own rows."""
    alice = login_client("alice")
    bob = login_client("bob")
    _store_one(alice, "brand-new-chat")
    assert db.conversation_owner("brand-new-chat") is None  # not claimed

    assert [i["attachment_id"] for i in alice.get("/chat-media/brand-new-chat").json()["items"]] == [
        "att-00000001"
    ]
    assert alice.get("/chat-media/brand-new-chat/att-00000001").status_code == 200
    assert bob.get("/chat-media/brand-new-chat").json() == {"items": []}
    assert bob.get("/chat-media/brand-new-chat/att-00000001").status_code == 404

    # Bob may store under the same unowned id; it stays his alone.
    _store_one(bob, "brand-new-chat", payload=_png(colour=(0, 255, 0)))
    assert len(alice.get("/chat-media/brand-new-chat").json()["items"]) == 1
    assert len(bob.get("/chat-media/brand-new-chat").json()["items"]) == 1
    assert (
        alice.get("/chat-media/brand-new-chat/att-00000001").content
        != bob.get("/chat-media/brand-new-chat/att-00000001").content
    )

    # Once Alice's history push creates the row, the chat is hers and Bob's
    # copy under it is unreachable (the reaper removes it after the grace).
    _chat(alice, "brand-new-chat")
    assert bob.get("/chat-media/brand-new-chat/att-00000001").status_code == 404
    assert alice.get("/chat-media/brand-new-chat/att-00000001").status_code == 200


# ---------------------------------------------------------- what is accepted --


def test_a_png_named_jpg_is_stored_as_the_png_it_is(login_client):
    alice = login_client("alice")
    _chat(alice, "conv-types")
    item = _store_one(alice, "conv-types", payload=_png(), name="holiday.jpg")
    assert item["mime"] == "image/png"
    resp = alice.get("/chat-media/conv-types/att-00000001")
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["content-disposition"] == 'inline; filename="image.png"'


def _gif(width: int = 300, height: int = 200) -> bytes:
    out = io.BytesIO()
    Image.effect_noise((width, height), 64).convert("P").save(out, format="GIF")
    return out.getvalue()


_REFUSED = [
    (
        "drawing.svg",
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        "image/svg+xml",
    ),
    ("page.png", b"<!doctype html><html><script>alert(document.cookie)</script></html>", PNG_CTYPE),
    ("photo.heic", b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64, "image/heic"),
    ("scan.bmp", b"BM" + b"\x00" * 80, "image/bmp"),
    ("cut.jpg", _jpeg(300, 200, noise=True)[:9000], "image/jpeg"),
    ("cut.png", _png(300, 200)[:-20], PNG_CTYPE),
    ("cut.gif", _gif()[:-40], "image/gif"),
    ("magic-only.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 200, PNG_CTYPE),
]


@pytest.mark.parametrize("name,payload,ctype", _REFUSED, ids=[case[0] for case in _REFUSED])
def test_anything_but_a_verified_raster_is_refused_with_415(login_client, name, payload, ctype):
    alice = login_client("alice")
    _chat(alice, "conv-refused")
    resp = _post(alice, "conv-refused", [(name, payload, ctype)], ["att-00000001"])
    assert resp.status_code == 415, resp.text
    assert resp.json()["code"] == "unsupported_type"
    assert _rows("conv-refused") == []
    assert not os.path.exists(os.path.join(settings.chat_media_dir, str(_uid("alice")), "conv-refused"))


def test_a_cut_file_is_refused_even_after_weasyprint_relaxed_pillow(monkeypatch):
    """WeasyPrint sets Pillow's process-wide ImageFile.LOAD_TRUNCATED_IMAGES
    to True when it is imported, and the artifact renderer imports it in this
    process. With it on, Pillow decodes a cut JPEG or GIF without complaint:
    this test failed in the full suite (accepted, 200) while passing alone.
    The store's own end-of-file check must hold whatever the switch says."""
    from PIL import ImageFile

    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", True)
    whole = {
        "jpeg-small": _jpeg(300, 200, noise=True),
        "jpeg-large": _jpeg(1200, 900, orientation=6, noise=True),
        "png": _png(300, 200),
        "gif": _gif(),
    }
    for label, data in whole.items():
        assert chat_media.inspect(data).sha256, label  # a whole file still passes
        for cut in (len(data) // 3, len(data) // 2, len(data) - 40, len(data) - 2):
            with pytest.raises(chat_media.Refused) as refused:
                chat_media.inspect(data[:cut])
            assert refused.value.result == "unsupported", (label, cut)


def _with_exif_thumbnail(jpeg: bytes) -> bytes:
    """A camera JPEG: an APP1 EXIF segment holding a small JPEG of its own
    (SOI .. SOS .. EOI) before the main picture's first scan."""
    small = io.BytesIO()
    Image.new("RGB", (160, 120), (5, 5, 5)).save(small, format="JPEG")
    body = b"Exif\x00\x00" + small.getvalue()
    app1 = b"\xff\xe1" + (len(body) + 2).to_bytes(2, "big") + body
    return jpeg[:2] + app1 + jpeg[2:]


def test_camera_and_phone_jpegs_browsers_show_are_stored_and_cut_ones_are_not(monkeypatch):
    """Pillow names a JPEG with more than one picture in its MPF block "MPO"
    (many camera and phone photos), and a phone's motion photo appends an MP4
    after the picture's end. Both were refused: the first by the format
    check, the second because the end check searched the whole buffer for the
    last start-of-scan and found one inside the trailer. Browsers show both.
    A cut picture is still refused, trailer or not, EXIF thumbnail or not."""
    from PIL import ImageFile

    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", True)
    out = io.BytesIO()
    Image.new("RGB", (1200, 800), (200, 0, 0)).save(
        out, format="MPO", save_all=True, append_images=[Image.new("RGB", (1200, 800), (0, 0, 200))]
    )
    mpo = out.getvalue()
    base = _jpeg(1200, 800, noise=True)
    trailer = b"\x00\x00\x00\x18ftypmp42" + random.Random(7).randbytes(200_000)
    assert b"\xff\xda" in trailer
    camera = _with_exif_thumbnail(base)
    for label, data in {"mpo": mpo, "motion photo": base + trailer, "exif thumbnail": camera}.items():
        picture = chat_media.inspect(data)
        assert (picture.mime, picture.width, picture.height) == ("image/jpeg", 1200, 800), label
    cut = len(base) // 2
    for label, data in {
        "cut + trailer": base[:cut] + trailer,
        "exif thumbnail, main cut": camera[: len(camera) - len(base) // 2],
        "mpo cut in its first picture": mpo[: len(mpo) // 4],
    }.items():
        with pytest.raises(chat_media.Refused):
            chat_media.inspect(data)
            pytest.fail(label)


def test_a_sixteen_bit_grey_png_thumbnails_as_dark_as_it_is():
    # 1000/65535 is near black; a straight RGB convert clipped it to white.
    out = io.BytesIO()
    Image.new("I;16", (1200, 800), 1000).save(out, format="PNG")
    picture = chat_media.inspect(out.getvalue())
    thumb = Image.open(io.BytesIO(picture.thumb)).convert("RGB")
    assert max(high for _low, high in thumb.getextrema()) < 32, thumb.getextrema()


def test_a_picture_over_the_store_ceiling_is_refused_before_it_is_decoded(monkeypatch):
    """A 38-byte lossless WebP of 16383x2440 decoded to +601 MiB peak under
    the old 40 MP ceiling (security review, 2026-10-02). The store now refuses
    anything over MAX_STORE_PIXELS from its header, for every format, before
    `verify` or `load` runs."""
    bomb = io.BytesIO()
    Image.new("RGBA", (16383, 2440), (10, 20, 30, 255)).save(bomb, format="WEBP", lossless=True, quality=0, method=0)
    assert len(bomb.getvalue()) < 100
    big_jpeg = io.BytesIO()
    Image.new("RGB", (5000, 3300), (1, 2, 3)).save(big_jpeg, format="JPEG", progressive=True)
    from PIL import ImageFile

    def no_decode(self, *a, **k):
        raise AssertionError("a picture over the ceiling was decoded")

    monkeypatch.setattr(ImageFile.ImageFile, "load", no_decode)
    for data in (bomb.getvalue(), big_jpeg.getvalue()):
        with pytest.raises(chat_media.Refused) as refused:
            chat_media.inspect(data)
        assert refused.value.result == "unsupported"
    assert chat_media.MAX_STORE_PIXELS == 16_000_000


def test_decodes_run_on_their_own_small_pool(login_client, monkeypatch):
    """At most DECODE_WORKERS pictures are decoded at once, whoever asks, and
    never on the default executor every other to_thread shares."""
    running = {"now": 0, "most": 0}
    names = set()
    lock = threading.Lock()
    real = chat_media.inspect

    def counted(data):
        with lock:
            running["now"] += 1
            running["most"] = max(running["most"], running["now"])
            names.add(threading.current_thread().name.rsplit("_", 1)[0])
        try:
            time.sleep(0.05)
            return real(data)
        finally:
            with lock:
                running["now"] -= 1

    monkeypatch.setattr(chat_media, "inspect", counted)

    async def burst():
        return await asyncio.gather(*(chat_media.run_decode(chat_media.inspect, _png()) for _ in range(8)))

    assert len(asyncio.run(burst())) == 8
    assert running["most"] == chat_media.DECODE_WORKERS
    alice = login_client("alice")
    _chat(alice, "conv-pool")
    _store_one(alice, "conv-pool")
    assert names == {"chat-media-decode"}


def test_one_refused_picture_stores_none_of_the_request(login_client):
    alice = login_client("alice")
    _chat(alice, "conv-all-or-none")
    resp = _post(
        alice,
        "conv-all-or-none",
        [("a.png", _png(), PNG_CTYPE), ("b.svg", b"<svg/>", "image/svg+xml")],
        ["att-00000001", "att-00000002"],
    )
    assert resp.status_code == 415
    assert _rows("conv-all-or-none") == []


def test_a_picture_over_10_mib_is_413(login_client):
    alice = login_client("alice")
    _chat(alice, "conv-big")
    payload = b"\xff\xd8\xff" + b"\x00" * (chat_media.MAX_IMAGE_BYTES - 2)
    assert len(payload) == chat_media.MAX_IMAGE_BYTES + 1
    resp = _post(alice, "conv-big", [("big.jpg", payload, "image/jpeg")], ["att-00000001"])
    assert resp.status_code == 413, resp.text
    assert resp.json()["code"] == "too_large"
    assert _rows("conv-big") == []


def test_the_shape_of_the_form_is_checked_before_anything_is_stored(login_client):
    alice = login_client("alice")
    _chat(alice, "conv-shape")
    over = chat_media.MAX_FILES + 1
    assert over == 1000  # docs/chat-media/LIMITS.md: no limit since 2026-10-03, 999 the ceiling
    tiny = _png()
    many = [(f"{i}.png", tiny, PNG_CTYPE) for i in range(over)]
    resp = _post(alice, "conv-shape", many, [f"att-{i:08d}" for i in range(over)])
    assert resp.status_code in (400, 413), resp.text  # CONTRACT: >MAX_FILES files is 400/413

    two = [("a.png", _png(), PNG_CTYPE), ("b.png", _png(colour=(1, 2, 3)), PNG_CTYPE)]
    assert _post(alice, "conv-shape", two, ["att-00000001"]).status_code == 400  # count mismatch
    assert _post(alice, "conv-shape", two[:1], ["short"]).status_code == 400  # under 8 characters
    assert _post(alice, "conv-shape", two[:1], ["has spaces!!"]).status_code == 400
    assert _post(alice, "conv-shape", two[:1], ["a" * 65]).status_code == 400
    assert _post(alice, "conv-shape", two, ["att-00000001", "att-00000001"]).status_code == 400
    assert _post(alice, "conv-shape", two[:1], ["att-00000001"], source="chat").status_code == 400
    assert _post(alice, "conv-shape", [], ["att-00000001"]).status_code == 400
    assert _rows("conv-shape") == []


# ------------------------------------------------- idempotence, thumbnails --


def test_a_retry_returns_the_first_write_unchanged(login_client):
    alice = login_client("alice")
    _chat(alice, "conv-retry")
    first = _store_one(alice, "conv-retry")
    again = _store_one(alice, "conv-retry")
    assert again["created"] is False
    assert again["media_id"] == first["media_id"]
    # Different bytes under the same attachment id: the first write still
    # wins, so the URL never changes content (immutable caching depends on it).
    other = _store_one(alice, "conv-retry", payload=_png(colour=(0, 0, 255)))
    assert other["created"] is False
    assert other["sha256"] == first["sha256"]
    assert len(_rows("conv-retry")) == 1
    media_dirs = os.listdir(os.path.join(settings.chat_media_dir, str(_uid("alice")), "conv-retry"))
    assert media_dirs == [first["media_id"]]


def test_a_backfill_is_stored_with_its_source(login_client):
    alice = login_client("alice")
    _chat(alice, "conv-backfill")
    resp = _post(
        alice, "conv-backfill", [("a.png", _png(), PNG_CTYPE)], ["bf-" + "0" * 32], source="backfill"
    )
    assert resp.status_code == 200, resp.text
    assert _rows("conv-backfill")[0]["source"] == "backfill"


def test_a_large_picture_gets_an_oriented_thumbnail_and_a_small_one_does_not(login_client):
    alice = login_client("alice")
    _chat(alice, "conv-thumb")
    # 1200 x 800 as stored, EXIF orientation 6: it DISPLAYS as 800 x 1200.
    big = _jpeg(1200, 800, orientation=6, noise=True)
    resp = _post(
        alice,
        "conv-thumb",
        [("big.jpg", big, "image/jpeg"), ("small.png", _png(100, 80), PNG_CTYPE)],
        ["att-big-0001", "att-small-01"],
    )
    assert resp.status_code == 200, resp.text
    big_item, small_item = resp.json()["items"]
    assert (big_item["width"], big_item["height"]) == (800, 1200)
    assert (small_item["width"], small_item["height"]) == (100, 80)
    rows = {r["attachment_id"]: r for r in _rows("conv-thumb")}
    assert rows["att-big-0001"]["has_thumb"] is True
    assert rows["att-small-01"]["has_thumb"] is False

    thumb = alice.get("/chat-media/conv-thumb/att-big-0001?size=thumb")
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/webp"
    assert thumb.headers["etag"] == f'"{big_item["sha256"]}-t"'
    assert thumb.headers["content-disposition"] == 'inline; filename="image.webp"'
    with Image.open(io.BytesIO(thumb.content)) as image:
        assert image.format == "WEBP"
        assert max(image.size) == chat_media.THUMB_EDGE
        assert image.height > image.width  # the orientation was applied
    full = alice.get("/chat-media/conv-thumb/att-big-0001")
    assert full.content == big
    assert full.headers["etag"] == f'"{big_item["sha256"]}"'

    # A picture too small for a thumbnail is its own thumbnail, under its own tag.
    small_thumb = alice.get("/chat-media/conv-thumb/att-small-01?size=thumb")
    assert small_thumb.status_code == 200
    assert small_thumb.headers["content-type"] == "image/png"
    assert small_thumb.headers["etag"] == f'"{small_item["sha256"]}"'
    assert small_thumb.content == alice.get("/chat-media/conv-thumb/att-small-01").content


def test_the_layout_on_disk_is_private_and_named_by_the_server(login_client):
    alice = login_client("alice")
    _chat(alice, "conv-layout")
    item = _post(
        alice, "conv-layout", [("big.jpg", _jpeg(900, 600, noise=True), "image/jpeg")], ["att-00000001"]
    ).json()["items"][0]
    directory = os.path.join(settings.chat_media_dir, str(_uid("alice")), "conv-layout", item["media_id"])
    assert sorted(os.listdir(directory)) == ["full.jpg", "thumb.webp"]
    for level in (
        settings.chat_media_dir,
        os.path.dirname(os.path.dirname(directory)),
        os.path.dirname(directory),
        directory,
    ):
        assert stat.S_IMODE(os.stat(level).st_mode) == 0o700, level
    for name in ("full.jpg", "thumb.webp"):
        assert stat.S_IMODE(os.stat(os.path.join(directory, name)).st_mode) == 0o600


# ------------------------------------------------------------- the byte route --


def test_every_header_of_the_byte_route(login_client):
    alice = login_client("alice")
    _chat(alice, "conv-headers")
    payload = _png()
    item = _store_one(alice, "conv-headers", payload=payload)
    resp = alice.get("/chat-media/conv-headers/att-00000001?size=full")
    assert resp.status_code == 200
    assert resp.content == payload
    headers = resp.headers
    assert headers["content-type"] == "image/png"
    assert headers["content-length"] == str(len(payload))
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert headers["content-disposition"] == 'inline; filename="image.png"'
    assert headers["cache-control"] == "private, max-age=31536000, immutable"
    assert headers["etag"] == f'"{item["sha256"]}"'
    assert alice.get("/chat-media/conv-headers/att-00000001?size=huge").status_code == 400


def test_if_none_match_answers_304_with_no_body(login_client):
    alice = login_client("alice")
    _chat(alice, "conv-304")
    item = _store_one(alice, "conv-304")
    tag = f'"{item["sha256"]}"'
    for header in (tag, f"W/{tag}", f'"nope", {tag}', "*"):
        resp = alice.get("/chat-media/conv-304/att-00000001", headers={"If-None-Match": header})
        assert resp.status_code == 304, header
        assert resp.content == b""
        assert resp.headers["etag"] == tag
        assert resp.headers["cache-control"] == chat_media.CACHE_CONTROL
    changed = alice.get("/chat-media/conv-304/att-00000001", headers={"If-None-Match": '"other"'})
    assert changed.status_code == 200
    # Someone else's tag guess earns them the same 404 as everything else.
    bob = login_client("bob")
    assert bob.get("/chat-media/conv-304/att-00000001", headers={"If-None-Match": tag}).status_code == 404


def test_a_row_whose_file_is_gone_is_410_media_missing(login_client):
    alice = login_client("alice")
    _chat(alice, "conv-gone")
    item = _store_one(alice, "conv-gone")
    path = os.path.join(
        settings.chat_media_dir, str(_uid("alice")), "conv-gone", item["media_id"], "full.png"
    )
    os.unlink(path)
    for size in ("full", "thumb"):
        resp = alice.get(f"/chat-media/conv-gone/att-00000001?size={size}")
        assert resp.status_code == 410
        assert resp.json()["code"] == "media_missing"
    # Never a 410 for a picture that is not yours: that would confirm it exists.
    assert login_client("bob").get("/chat-media/conv-gone/att-00000001").status_code == 404


def test_a_retry_of_the_same_bytes_restores_a_row_whose_file_is_gone(login_client):
    """A row outlived its file (a restored volume, a manual cleanup, an erase
    that raced the store). A retry of the very same bytes under the same id
    used to answer `created: false` and write nothing, so the picture stayed
    410 for good. Different bytes never restore it: the URL is immutable."""
    alice = login_client("alice")
    _chat(alice, "conv-heal")
    big = _jpeg(1200, 900, noise=True)  # big enough to have a thumbnail
    item = _store_one(alice, "conv-heal", payload=big)
    directory = os.path.join(settings.chat_media_dir, str(_uid("alice")), "conv-heal", item["media_id"])
    shutil.rmtree(directory)
    assert alice.get("/chat-media/conv-heal/att-00000001").status_code == 410

    other = _post(alice, "conv-heal", [("b.jpg", _jpeg(1200, 900, noise=True), "image/jpeg")], ["att-00000001"])
    assert other.status_code == 200 and other.json()["items"][0]["created"] is False
    assert alice.get("/chat-media/conv-heal/att-00000001").status_code == 410

    again = _post(alice, "conv-heal", [("a.jpg", big, "image/jpeg")], ["att-00000001"])
    assert again.status_code == 200, again.text
    assert again.json()["items"][0] == {**item, "created": False}  # the same row, unchanged
    full = alice.get("/chat-media/conv-heal/att-00000001")
    assert full.status_code == 200 and full.content == big
    thumb = alice.get("/chat-media/conv-heal/att-00000001?size=thumb")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/webp"
    assert sorted(os.listdir(directory)) == ["full.jpg", "thumb.webp"]


def test_the_chat_path_restores_a_row_whose_file_is_gone(login_client):
    # /chat's background store: the same picture sent again under its id.
    alice = login_client("alice")
    _chat(alice, "conv-heal-chat")
    payload = _png()
    item = _store_one(alice, "conv-heal-chat", payload=payload)
    row = chat_media.get_row(_uid("alice"), "conv-heal-chat", "att-00000001")
    os.unlink(chat_media.file_path(row, "full"))
    value = "data:image/png;base64," + base64.b64encode(payload).decode("ascii")
    assert chat_media._store_inline_one(_uid("alice"), "conv-heal-chat", "att-00000001", value) == "stored"
    assert alice.get("/chat-media/conv-heal-chat/att-00000001").content == payload
    # With the file back, the next retry is the plain duplicate again.
    assert chat_media._store_inline_one(_uid("alice"), "conv-heal-chat", "att-00000001", value) == "duplicate"
    assert chat_media.get_row(_uid("alice"), "conv-heal-chat", "att-00000001")["media_id"] == item["media_id"]


def test_below_the_free_space_floor_new_bytes_are_507(login_client, monkeypatch):
    alice = login_client("alice")
    _chat(alice, "conv-full")
    stored = _store_one(alice, "conv-full")
    monkeypatch.setattr(settings, "chat_media_min_free_gib", 250.0)
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(chat_media.shutil, "disk_usage", lambda path: usage(4 << 40, 4 << 40, 10 << 30))
    resp = _post(alice, "conv-full", [("b.png", _png(colour=(9, 9, 9)), PNG_CTYPE)], ["att-00000002"])
    assert resp.status_code == 507, resp.text
    assert resp.json()["code"] == "insufficient_storage"
    assert [r["attachment_id"] for r in _rows("conv-full")] == ["att-00000001"]
    # A retry of a picture already stored needs no new bytes: still 200.
    again = _store_one(alice, "conv-full")
    assert again["created"] is False and again["media_id"] == stored["media_id"]


def test_the_list_route_shape(login_client):
    alice = login_client("alice")
    _chat(alice, "conv-list")
    _store_one(alice, "conv-list", "att-00000001")
    _store_one(alice, "conv-list", "att-00000002", payload=_png(colour=(1, 1, 1)))
    items = alice.get("/chat-media/conv-list").json()["items"]
    assert [i["attachment_id"] for i in items] == ["att-00000001", "att-00000002"]
    assert set(items[0]) == {"attachment_id", "media_id", "mime", "width", "height", "bytes", "created_at"}
    assert (items[0]["width"], items[0]["height"]) == (64, 48)


def test_an_account_without_attachments_cannot_upload(login_client, monkeypatch):
    from app.authn import features as feature_access

    alice = login_client("alice")
    _chat(alice, "conv-gated")
    real = feature_access.allowed
    monkeypatch.setattr(
        feature_access,
        "allowed",
        lambda features, feature: False if feature == feature_access.Feature.ATTACHMENTS else real(features, feature),
    )
    resp = _post(alice, "conv-gated", [("a.png", _png(), PNG_CTYPE)], ["att-00000001"])
    assert resp.status_code == 403
    assert _rows("conv-gated") == []


# ------------------------------------------------------------------ metrics --


def test_the_metric_registry_holds_every_contract_entry_and_the_module_agrees():
    labels = metrics._LABELS_BY_METRIC
    assert labels["chat_media_writes_total"] == {
        "source": {"chat", "upload", "backfill"},
        "result": {"stored", "duplicate", "unsupported", "too_large", "no_space", "error", "unlinked"},
    }
    assert labels["chat_media_reads_total"] == {
        "size": {"thumb", "full"},
        "result": {"ok", "not_modified", "not_found", "missing"},
    }
    assert labels["chat_media_write_seconds"] == {"source": {"chat", "upload", "backfill"}}
    assert labels["chat_files_lasting_total"] == {
        "purpose": {"document", "dataset"},
        "result": {"stored", "no_space", "error"},
    }
    assert set(chat_media.SOURCES) == metrics.CHAT_MEDIA_SOURCES
    assert set(chat_media.WRITE_RESULTS) == metrics.CHAT_MEDIA_WRITE_RESULTS
    assert set(chat_media.SIZES) == metrics.CHAT_MEDIA_SIZES
    assert set(chat_media.READ_RESULTS) == metrics.CHAT_MEDIA_READ_RESULTS
    assert "chat_media_erase_total" in labels and "chat_media_reaped_total" in labels


def test_the_metrics_count_writes_and_reads_and_carry_nothing_of_the_person(login_client):
    alice = login_client("alice")
    _chat(alice, "conv-metrics-secret")
    metrics.reset()
    try:
        item = _store_one(alice, "conv-metrics-secret")
        _store_one(alice, "conv-metrics-secret")
        _post(alice, "conv-metrics-secret", [("x.svg", b"<svg/>", "image/svg+xml")], ["att-00000009"])
        alice.get("/chat-media/conv-metrics-secret/att-00000001")
        alice.get(
            "/chat-media/conv-metrics-secret/att-00000001",
            headers={"If-None-Match": f'"{item["sha256"]}"'},
        )
        alice.get("/chat-media/conv-metrics-secret/att-nothing-here?size=thumb")
        rendered = metrics.render()
        assert 'chat_media_writes_total{result="stored",source="upload"} 1' in rendered
        assert 'chat_media_writes_total{result="duplicate",source="upload"} 1' in rendered
        assert 'chat_media_writes_total{result="unsupported",source="upload"} 1' in rendered
        assert 'chat_media_reads_total{result="ok",size="full"} 1' in rendered
        assert 'chat_media_reads_total{result="not_modified",size="full"} 1' in rendered
        assert 'chat_media_reads_total{result="not_found",size="thumb"} 1' in rendered
        assert 'chat_media_write_seconds_count{source="upload"} 1' in rendered
        lines = [line for line in rendered.splitlines() if line.startswith("chat_media")]
        for text in ("conv-metrics-secret", "att-0000", "alice", item["media_id"]):
            assert not any(text in line for line in lines), text
    finally:
        metrics.reset()
