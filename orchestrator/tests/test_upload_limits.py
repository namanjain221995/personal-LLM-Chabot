"""No upload limit (owner decision 2026-10-03, docs/chat-media/LIMITS.md).

A message carries any number of pictures, documents and videos, of any size;
999 is only the technical ceiling a request is validated against (vLLM's
per-prompt image maximum). What reading can do has physical bounds, and they
degrade, never refuse. Pinned here, the server's side of that:
  * the counts: 999 accepted, 1,000 refused, nothing below it;
  * the request bytes that still bound a POST (the batch budget and framing);
  * the chunked rail's part count no longer caps a file at 8 GiB;
  * many pictures are fitted to the model: smaller before fewer, and the
    answer says how many were read when not all were;
  * the router (Qwen3-VL-8B, a 65,536-token window) never gets more pictures
    than fit;
  * image_memory keeps what fits of many photos and never fails the turn;
  * a video over the analysis window is analysed over the window, and says so;
  * an archive past the unpack caps is unpacked up to them, not refused;
  * the dataset readers cannot take the head's memory.
The chat-media routes and the document engine have their own tests
(test_chat_media_chat.py, test_chat_media_api.py, test_document_uploads.py).
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import random
import zipfile
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from app import chat_media, context, llm
from app import main as app_main
from app import uploads as uploads_module
from app.config import settings
from app.engines import image_memory, vision
from app.main import MAX_IMAGES, ChatRequest, _resolve_video_refs


def _noise_jpeg(seed: int, size=(240, 180)) -> str:
    rnd = random.Random(seed)
    image = Image.new("RGB", size)
    image.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(size[0] * size[1])])
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def _photo(seed: int, size=(1600, 1200)) -> str:
    """A 1600x1200 JPEG, what the composer sends after its shrink."""
    image = Image.new("RGB", size, ((seed * 37) % 256, (seed * 91) % 256, 120))
    ImageDraw.Draw(image).text((40, 40), f"photo {seed}", fill=(255, 255, 255))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def _size(url: str) -> tuple:
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).size


# ------------------------------------------------------------ the counts --


def test_counts_are_only_a_technical_ceiling():
    assert MAX_IMAGES == 999 == chat_media.MAX_FILES
    assert app_main._MAX_DOC_REFS == 999 == app_main._MAX_VIDEO_REFS
    ids = [f"att-{i:08d}" for i in range(999)]
    req = ChatRequest(message="x", images=["IMG"] * 999, image_ids=ids)
    assert len(req.images_data) == 999
    assert len(ChatRequest(message="x", image_refs=ids).image_refs) == 999
    with pytest.raises(ValidationError, match="at most 999 images per message"):
        ChatRequest(message="x", images=["IMG"] * 1000)
    # Inline pictures and stored ones sent by reference share the one ceiling.
    with pytest.raises(ValidationError, match="at most 999 images per message"):
        ChatRequest(message="x", images=["IMG"], image_refs=ids)
    for field in ("image_ids", "image_refs"):
        with pytest.raises(ValidationError, match="at most 999 image ids per message"):
            ChatRequest(message="x", **{field: ids + ["att-99999999"]})


def test_videos_per_message_are_only_a_technical_ceiling(monkeypatch):
    rows = {f"{i:032x}": {"id": i, "status": "done"} for i in range(1000)}
    monkeypatch.setattr("app.db.get_video_by_upload", lambda conv, upload_id: rows.get(upload_id))

    def request(n):
        return SimpleNamespace(video_uploads=[{"upload_id": f"{i:032x}", "name": f"v{i}.mp4"} for i in range(n)])

    got, err = asyncio.run(_resolve_video_refs(request(999), "conv-v", []))
    assert err is None and len(got) == 999
    got, err = asyncio.run(_resolve_video_refs(request(1000), "conv-v", []))
    assert got == [] and err == "A message can carry at most 999 videos."


def test_one_post_of_999_pictures_is_bounded_by_bytes_not_count():
    """The framing of 999 `file` parts and their ids fits the 1 MiB the body
    caps allow beside the 48 MiB batch, under Cloudflare's 100 MB wall."""
    from urllib3.filepost import encode_multipart_formdata

    fields = [("file", (f"IMG_{i:04d}.jpg", b"", "image/jpeg")) for i in range(chat_media.MAX_FILES)]
    fields += [("attachment_id", f"att-{i:032d}") for i in range(chat_media.MAX_FILES)]
    fields.append(("source", "upload"))
    framing, _ctype = encode_multipart_formdata(fields)
    assert len(framing) <= app_main._MULTIPART_FRAMING_BYTES
    cap = app_main.body_cap_for("POST", "/chat-media/c1").signed_in
    assert chat_media.BATCH_BUDGET_BYTES + app_main._MULTIPART_FRAMING_BYTES <= cap < 100_000_000
    assert chat_media._FORM_MAX_FIELDS > chat_media.MAX_FILES


def test_a_chunked_upload_is_not_capped_at_8_gib_by_its_part_count(login_client, monkeypatch, tmp_path):
    """128 parts of the browser's 64 MiB was a hidden 8 GiB limit."""
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    alice = login_client("alice")
    assert alice.post("/history/conversations", json={"id": "conv-big", "title": "big"}).status_code == 200
    monkeypatch.setattr(settings, "upload_max_mb", 102400)
    part = 64 * 1024 * 1024
    size = 40 * 1024 * 1024 * 1024  # 40 GiB
    resp = alice.post(
        "/uploads/chunked/init",
        data={
            "conversation_id": "conv-big", "filename": "archive-footage.pdf", "purpose": "document",
            "size": str(size), "parts": str(size // part), "part_size": str(part),
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["max_parts"] == uploads_module._MAX_PARTS >= 100 * 1024 // 64


def test_a_video_may_be_as_large_as_any_upload(monkeypatch):
    """VIDEO_MAX_UPLOAD_MB unset follows UPLOAD_MAX_MB (production: 100 GB)."""
    from app import config

    monkeypatch.setenv("UPLOAD_MAX_MB", "102400")
    monkeypatch.delenv("VIDEO_MAX_UPLOAD_MB", raising=False)
    fresh = config.Settings()
    assert fresh.video_max_upload_mb == fresh.upload_max_mb == 102400
    monkeypatch.setenv("VIDEO_MAX_UPLOAD_MB", "4096")
    assert config.Settings().video_max_upload_mb == 4096


# ------------------------------------------------- pictures for the model --


@pytest.fixture()
def _main_window(monkeypatch):
    monkeypatch.setitem(context._window_cache, settings.openai_base_url, 1_000_000)
    monkeypatch.delenv("VISION_IMAGE_TOKEN_BUDGET", raising=False)
    monkeypatch.setattr(settings, "ocr_enabled", False)


def _run_vision(monkeypatch, images, message="describe them"):
    seen: dict = {}

    async def fake_stream(messages, **kw):
        seen["messages"] = messages
        yield ("token", "ok")

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)

    async def emit(kind, payload):
        pass

    answer = asyncio.run(vision.run_vision_engine(message, images, [], emit, effort="fast"))
    parts = seen["messages"][-1]["content"]
    pictures = [p["image_url"]["url"] for p in parts if p.get("type") == "image_url"]
    words = " ".join(p["text"] for p in parts if p.get("type") == "text")
    return answer, pictures, words


def test_a_few_pictures_go_exactly_as_they_came(monkeypatch, _main_window):
    pictures = [_photo(i) for i in range(3)]
    answer, sent, words = _run_vision(monkeypatch, pictures)
    assert sent == pictures and answer == "ok"
    assert "px on the long edge" not in words


def test_a_hundred_photos_all_reach_the_model_at_a_smaller_size(monkeypatch, _main_window):
    """100 photos of 1600x1200 are ~190,000 image tokens, over the turn's
    budget: every one is sent, at 896 px, and nothing is dropped."""
    pictures = [_photo(i) for i in range(100)]
    answer, sent, words = _run_vision(monkeypatch, pictures)
    assert len(sent) == 100
    assert all(max(_size(u)) == 896 for u in sent)
    tokens = sum(context.estimate_image_tokens({"image_url": {"url": u}}) for u in sent)
    assert tokens <= vision.image_token_budget() == vision._IMAGE_TOKEN_BUDGET
    assert "sent all 100 pictures at 896 px" in words
    assert answer == "ok"  # nothing dropped, so nothing to tell the person


def test_pictures_that_cannot_all_fit_say_how_many_were_read(monkeypatch, _main_window):
    monkeypatch.setenv("VISION_IMAGE_TOKEN_BUDGET", str(10 * 158))
    pictures = [_photo(i) for i in range(30)]
    answer, sent, words = _run_vision(monkeypatch, pictures)
    assert len(sent) == 10 and all(max(_size(u)) == vision.FIT_EDGES[-1] for u in sent)
    assert "The app sent 10 of the 30 attached pictures at 448 px on the long edge" in words
    assert answer.startswith("ok")
    assert "I read the first 10 of the 30 pictures in this message; the other 20 did not fit" in answer


def test_a_picture_that_will_not_open_is_named_not_counted_as_read(monkeypatch, _main_window):
    monkeypatch.setenv("VISION_IMAGE_TOKEN_BUDGET", str(2 * 592))
    pictures = [_photo(0), "data:image/png;base64,bm90IGEgcGljdHVyZQ==", _photo(1)]
    answer, sent, words = _run_vision(monkeypatch, pictures)
    assert len(sent) == 2 and all(max(_size(u)) == 640 for u in sent)
    assert answer.endswith("_I read 2 of the 3 pictures in this message: 1 could not be opened as a picture._")


def test_fitting_is_arithmetic_on_the_headers():
    pictures = [_photo(i) for i in range(4)]
    one = context.estimate_image_tokens({"image_url": {"url": pictures[0]}})
    assert one == 50 * 38 + 4  # 1600x1200, one token per 32x32 pixels
    same = vision.fit_images(pictures, 4 * one)
    assert same.images == pictures and same.edge is None and same.dropped == 0
    smaller = vision.fit_images(pictures, 4 * one - 1)
    assert smaller.edge == 896 and smaller.dropped == 0
    assert [max(_size(u)) for u in smaller.images] == [896] * 4


# ------------------------------------------------------------ the router --


def test_the_router_share_is_the_number_v1_publishes_for_it():
    from app.publicapi import registry

    assert context.CLASSIFICATION_MAX_IMAGES == registry.ROUTER_MAX_IMAGES
    assert context.CLASSIFICATION_MAX_IMAGE_TOKENS <= 65_536 // 2


def test_clipping_keeps_the_newest_pictures_and_every_word():
    old = {"role": "user", "content": [{"type": "text", "text": "earlier"}, {"type": "image_url", "image_url": {"url": "data:old"}}]}
    now = {
        "role": "user",
        "content": [{"type": "text", "text": "which is newest?"}]
        + [{"type": "image_url", "image_url": {"url": f"data:{i}"}} for i in range(20)],
    }
    out = context.clip_message_contents([{"role": "system", "content": "s"}, old, now], 100, max_images=8)
    assert out[1]["content"] == [{"type": "text", "text": "earlier"}]
    kept = [p["image_url"]["url"] for p in out[2]["content"] if p.get("type") == "image_url"]
    assert kept == [f"data:{i}" for i in range(12, 20)]
    assert out[2]["content"][0] == {"type": "text", "text": "which is newest?"}
    # The caller's list is not touched, and without max_images nothing is dropped.
    assert len(now["content"]) == 21
    assert context.clip_message_contents([now], 100)[0]["content"] == now["content"]


def test_the_router_never_gets_more_image_tokens_than_half_its_window():
    """Eight pictures whose size cannot be read are charged 16,388 tokens
    each: 131,104, twice the router's whole window. One fits its half."""
    now = {
        "role": "user",
        "content": [{"type": "text", "text": "what?"}]
        + [{"type": "image_url", "image_url": {"url": f"https://example.invalid/{i}.png"}} for i in range(8)],
    }
    out = context.clip_message_contents(
        [now], 100, max_images=8, max_image_tokens=context.CLASSIFICATION_MAX_IMAGE_TOKENS
    )
    kept = [p["image_url"]["url"] for p in out[0]["content"] if p.get("type") == "image_url"]
    assert kept == ["https://example.invalid/7.png"]
    assert out[0]["content"][0] == {"type": "text", "text": "what?"}


def test_the_router_is_never_handed_more_than_its_share(monkeypatch):
    rec: dict = {}

    async def create(**kwargs):
        rec["kwargs"] = kwargs
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"route": "vision"}'))])

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "_client", lambda base_url, api_key=None, **options: fake)
    pictures = [_noise_jpeg(i, size=(32, 24)) for i in range(20)]
    turn = [{"type": "text", "text": "what are these?"}] + [{"type": "image_url", "image_url": {"url": u}} for u in pictures]
    asyncio.run(llm.router_chat_completion([{"role": "user", "content": turn}]))
    sent = [
        part["image_url"]["url"]
        for message in rec["kwargs"]["messages"]
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part.get("type") == "image_url"
    ]
    assert sent == pictures[-context.CLASSIFICATION_MAX_IMAGES:]
    assert rec["kwargs"]["model"] == settings.router_model


# ---------------------------------------------------------- image_memory --


class _Rows:
    def __init__(self):
        self.saved: list = []

    def save_conversation_image(self, user_id, conversation_id, images, context):
        self.saved.append(list(images))

    def prune_conversation_images(self, ttl):
        return 0

    def delete_conversation_image(self, user_id, conversation_id):
        pass


def test_many_photos_keep_what_fits_and_never_fail_the_turn(monkeypatch):
    pictures = [_noise_jpeg(i) for i in range(40)]
    one = max(map(len, pictures))
    monkeypatch.setenv("IMAGE_MEMORY_MAX_CHARS", str(8 * one))
    monkeypatch.setenv("IMAGE_MEMORY_DB_CHARS", str(4 * one))
    monkeypatch.delenv("IMAGE_MEMORY_DURABLE", raising=False)
    rows = _Rows()
    monkeypatch.setattr(image_memory, "_db", lambda: rows)
    image_memory.clear()
    try:
        image_memory.remember("conv-many", pictures, question="compare", answer="ok", user_id=7)
        kept = image_memory.recall("conv-many", user_id=7)
        # The process keeps the turn's first pictures, in order, within its budget.
        assert 8 <= len(kept) < 40
        assert kept[:8] == pictures[:8]
        assert sum(map(len, kept)) <= image_memory.max_chars()
        # The durable row keeps fewer, within ITS budget, and is still written.
        assert len(rows.saved) == 1
        stored = rows.saved[0]
        assert 4 <= len(stored) < len(kept)
        assert stored[:4] == pictures[:4]
        assert sum(map(len, stored)) <= image_memory.max_db_chars()
    finally:
        image_memory.clear()


# ----------------------------------------------------------------- video --


def test_a_video_longer_than_the_window_is_analysed_over_the_window(monkeypatch, tmp_path):
    """Not refused (it was until 2026-10-03): the row keeps the file's own
    length, every later stage sizes its work by the window, ffmpeg is told
    `-t`, and the overview and the answer's prompt say what was analysed."""
    from app.engines import video as video_engine
    from app.video import media, pipeline, store

    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    monkeypatch.setattr(settings, "video_max_duration_s", 4 * 3600)

    async def probe(path, timeout_s=60.0):
        return media.Probe(
            duration_s=6 * 3600.0 + 720, has_video=True, has_audio=True, width=1280, height=720, fps=25.0,
            video_codec="h264", audio_codec="aac", container="mov,mp4", bytes=5 * 1024 ** 3,
        )

    monkeypatch.setattr(media, "probe", probe)
    ctx = pipeline._Ctx(row={"id": 1}, content_hash="9" * 64, source="x")

    async def progress(*a, **k):
        pass

    result = asyncio.run(pipeline._stage_probe(ctx, progress))
    assert result.status == "done"
    assert result.detail.startswith("the video is 6:12:00 long; its first 4:00:00 is analysed · "), result.detail
    assert result.row_fields["duration_ms"] == (6 * 3600 + 720) * 1000
    with open(store.stage_path("9" * 64, "probe.json")) as fh:
        saved = json.load(fh)
    assert saved["duration_s"] == 4 * 3600 and saved["full_duration_s"] == 6 * 3600 + 720
    assert media._limit_args(saved["analysed_s"]) == ["-t", "14400.000"]
    assert media._limit_args(None) == []

    row = {"display_name": "workshop.mp4", "duration_ms": result.row_fields["duration_ms"],
           "probe": result.row_fields["probe"], "status": "done", "understanding": {}}
    told = "only its first 4:00:00 of 6:12:00 was analysed"
    assert video_engine.window_note(row) == told
    assert told in video_engine.overview_markdown(row)
    assert told in video_engine.pinned_block([row], max_chars=4000)
    assert video_engine.window_note({**row, "probe": {"duration_s": 60}}) == ""


def test_the_window_reaches_ffmpeg(monkeypatch, tmp_path):
    from app.video import media

    seen: list = []

    async def run(argv, *, timeout_s, what):
        seen.append(argv)
        with open(argv[-1], "wb") as fh:
            fh.write(b"\0" * 100)
        return b"", b""

    monkeypatch.setattr(media, "_run", run)
    asyncio.run(media.extract_audio("in.mp4", str(tmp_path / "a.wav"), timeout_s=10, limit_s=14400.0))
    argv = seen[-1]
    at = argv.index("-i")
    assert argv[at + 1: at + 4] == ["in.mp4", "-t", "14400.000"]


# --------------------------------------------------------------- archives --


def _zip(path, members):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in members:
            zf.writestr(name, payload)
    return str(path)


def test_an_archive_past_the_member_cap_is_unpacked_up_to_it_not_refused(tmp_path, monkeypatch):
    from app.core import archive

    monkeypatch.setattr(settings, "archive_max_files", 10)
    src = _zip(tmp_path / "many.zip", [(f"f{i:02d}.csv", b"a\n1\n") for i in range(25)])
    plan = archive.extract(src, str(tmp_path / "out"))
    assert len(plan.members) == 10 and len(os.listdir(tmp_path / "out")) == 10
    assert plan.skipped[-1] == (
        "15 more file(s)", "not unpacked: only the first 10 entries are (the archive itself is kept whole)"
    )
    # An .xlsx (a zip a reader opens itself) is still checked strictly.
    with pytest.raises(archive.ArchiveError):
        archive.check_zip_container(src, label="spreadsheet")


def test_an_archive_past_the_byte_cap_is_unpacked_up_to_it_not_refused(tmp_path, monkeypatch):
    from app.core import archive

    monkeypatch.setattr(settings, "archive_max_uncompressed_mb", 1)
    monkeypatch.setattr(settings, "archive_max_ratio", 10_000_000)
    rnd = random.Random(3)
    sizes = [300, 300, 300, 600, 50]  # KiB: the fourth is past the 1 MB cap, the fifth still fits
    src = _zip(tmp_path / "big.zip", [(f"f{i}.bin", rnd.randbytes(k * 1024)) for i, k in enumerate(sizes)])
    plan = archive.extract(src, str(tmp_path / "out"))
    assert [m.name for m in plan.members] == ["f0.bin", "f1.bin", "f2.bin", "f4.bin"]
    assert plan.skipped == [
        ("f3.bin", "not unpacked: only 1 MB of an archive are (the archive itself is kept whole)")
    ]
    # A bomb-shaped member is still refused outright.
    monkeypatch.setattr(settings, "archive_max_ratio", 200)
    monkeypatch.setattr(settings, "archive_max_uncompressed_mb", 64)
    bomb = _zip(tmp_path / "bomb.zip", [("bomb.bin", b"\0" * (8 * 1024 * 1024))])
    with pytest.raises(archive.ArchiveError, match="bomb"):
        archive.extract(bomb, str(tmp_path / "out2"))


# --------------------------------------------------------------- datasets --


def test_the_dataset_profiler_cannot_take_the_heads_memory():
    from app.core import profile

    con = profile._duck()
    limit, spill = con.execute(
        "SELECT current_setting('memory_limit'), current_setting('temp_directory')"
    ).fetchone()
    number, unit = limit.split()
    assert unit in ("MiB", "GiB") and float(number) * (1024 if unit == "GiB" else 1) <= 2048
    assert spill.endswith("duckdb-profile-spill")


def test_a_file_turn_reads_a_huge_csv_only_up_to_its_byte_budget(tmp_path, monkeypatch):
    """A CSV of any size is never read whole for a file turn (a SPARSE file:
    nothing real is written past its first rows)."""
    from app.artifacts import material_in

    monkeypatch.setattr(material_in, "_CSV_READ_BYTES", 4096)
    path = tmp_path / "big.csv"
    path.write_bytes(b"id,name\n" + b"".join(f"{i},row {i}\n".encode() for i in range(2000)))
    with open(path, "r+b") as fh:
        fh.truncate(700 * 1024 * 1024)
    monkeypatch.setattr(
        material_in, "dataset_uploads",
        lambda conv, text="": [{"id": "a" * 32, "filename": "big.csv", "status": "ready"}],
    )
    monkeypatch.setattr(
        "app.core.upload_paths.resolve_upload_file",
        lambda ws, conv, uid, name, subdir: path if subdir == "extracted" else tmp_path / "missing",
    )
    tables, notes = material_in._workspace_tables(str(tmp_path), "conv-csv", 1, 200_000)
    assert len(tables) == 1 and 0 < len(tables[0].rows) < 2000
    rows = len(tables[0].rows)
    assert f"big.csv: only its first {rows:,} rows were read (the first 0 MB of the file)." in notes


def test_a_file_turn_reads_every_document_attached_to_it(tmp_path):
    """Seven documents attached to one file turn are seven read (five were
    read and the rest dropped in silence before 2026-10-03); a document past
    the whole-read budget arrives as a file and is read from disk."""
    from app.artifacts import material_in
    from app.engines.document import DocFile

    docs = [(f"note{i}.txt", base64.b64encode(f"fact number {i}".encode()).decode()) for i in range(6)]
    big = tmp_path / "big.txt"
    big.write_bytes(b"the big one")
    docs.append(DocFile("big.txt", str(big)))
    g = asyncio.run(material_in.gather(history=[], pdf_uploads=docs, save_documents=False))
    assert g.upload_names == [f"note{i}.txt" for i in range(6)] + ["big.txt"]
    assert "fact number 5" in g.uploads_text and "the big one" in g.uploads_text
