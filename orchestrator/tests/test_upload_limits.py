"""Upload limits per message, raised safely (owner decision 2026-10-03).

docs/chat-media/LIMITS.md: a message carries up to 20 pictures (inline and
`image_refs` together) and up to 20 documents or videos; the per-file sizes
stay. What is pinned here is the server's side of that:
  * the counts and the words a refusal uses;
  * the router (Qwen3-VL-8B, a small window) is never handed more pictures
    than its safe share, whatever a turn carries;
  * image_memory keeps what fits of twenty photos and never fails the turn.
The chat-media routes, the body caps and the document engine have their own
tests (test_chat_media_chat.py, test_chat_media_api.py,
test_orchestrator_hardening.py, test_document_uploads.py).
"""
from __future__ import annotations

import asyncio
import base64
import io
import random
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from app import chat_media, context, llm
from app.config import settings
from app.engines import image_memory
from app.main import MAX_IMAGES, ChatRequest, _resolve_video_refs


def _noise_jpeg(seed: int, size=(240, 180)) -> str:
    rnd = random.Random(seed)
    image = Image.new("RGB", size)
    image.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(size[0] * size[1])])
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


# ------------------------------------------------------------ the counts --


def test_twenty_pictures_per_message_and_the_twenty_first_is_refused():
    assert MAX_IMAGES == 20 == chat_media.MAX_FILES
    req = ChatRequest(message="x", images=[f"IMG{i}" for i in range(20)], image_ids=[f"att-{i:08d}" for i in range(20)])
    assert len(req.images_data) == 20
    with pytest.raises(ValidationError, match="at most 20 images per message"):
        ChatRequest(message="x", images=[f"IMG{i}" for i in range(21)])
    # Inline pictures and stored ones sent by reference share the one cap.
    with pytest.raises(ValidationError, match="at most 20 images per message"):
        ChatRequest(message="x", images=["IMG"], image_refs=[f"att-{i:08d}" for i in range(20)])
    for field in ("image_ids", "image_refs"):
        with pytest.raises(ValidationError, match="at most 20 image ids per message"):
            ChatRequest(message="x", **{field: [f"att-{i:08d}" for i in range(21)]})


def test_twenty_videos_per_message_and_the_twenty_first_is_refused(monkeypatch):
    rows = {f"{i:032x}": {"id": i, "status": "done"} for i in range(21)}
    monkeypatch.setattr("app.db.get_video_by_upload", lambda conv, upload_id: rows.get(upload_id))

    def request(n):
        return SimpleNamespace(video_uploads=[{"upload_id": f"{i:032x}", "name": f"v{i}.mp4"} for i in range(n)])

    got, err = asyncio.run(_resolve_video_refs(request(20), "conv-v", []))
    assert err is None and [r["id"] for r in got] == list(range(20))
    got, err = asyncio.run(_resolve_video_refs(request(21), "conv-v", []))
    assert got == [] and err == "A message can carry at most 20 videos."


# ------------------------------------------------------------ the router --


def test_the_router_share_is_the_number_v1_publishes_for_it():
    from app.publicapi import registry

    assert context.CLASSIFICATION_MAX_IMAGES == registry.ROUTER_MAX_IMAGES


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


def test_twenty_photos_keep_what_fits_and_never_fail_the_turn(monkeypatch):
    pictures = [_noise_jpeg(i) for i in range(20)]
    one = max(map(len, pictures))
    monkeypatch.setenv("IMAGE_MEMORY_MAX_CHARS", str(8 * one))
    monkeypatch.setenv("IMAGE_MEMORY_DB_CHARS", str(4 * one))
    monkeypatch.delenv("IMAGE_MEMORY_DURABLE", raising=False)
    rows = _Rows()
    monkeypatch.setattr(image_memory, "_db", lambda: rows)
    image_memory.clear()
    try:
        image_memory.remember("conv-twenty", pictures, question="compare", answer="ok", user_id=7)
        kept = image_memory.recall("conv-twenty", user_id=7)
        # The process keeps the turn's first pictures, in order, within its budget.
        assert 8 <= len(kept) < 20
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
