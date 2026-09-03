"""Attachment latency (2026-09-03): render policy, the upload-time cache,
and the bounded OCR pass on the image route.

Measured before the change on the deployed model: a 12-page born-digital
PDF took 7.8 s to the first token at Fast (six full-page renders prefilled
for text the model already had as text); a text-dense screenshot at Think
took 56 s (the OCR sidecar decoding a long transcript before the main model
could start). Both are policy, not physics, and these tests pin the policy.
"""
import asyncio
import json
import os

import pytest

from app.config import settings
from app.engines import document as doc_engine
from app.engines import ocr
from app.engines.document import _Doc, page_images_wanted


RICH = ["x" * 800 for _ in range(12)]
SCAN = ["", "", "x" * 800]


def test_render_policy_sends_pictures_only_where_text_cannot_answer():
    # Born-digital, ordinary question: Fast sends none, Think keeps a couple.
    assert page_images_wanted(RICH, 12, "fast", "summarize this") == 0
    assert page_images_wanted(RICH, 12, "think", "summarize this") == doc_engine.LAYOUT_PAGES
    assert page_images_wanted(RICH, 12, "max", "summarize this") == doc_engine.LAYOUT_PAGES
    # A question about the page's appearance earns the renders at any effort.
    assert page_images_wanted(RICH, 12, "fast", "is the signature on page 1 legible?") == doc_engine.MAX_PDF_PAGES
    assert page_images_wanted(RICH, 12, "fast", "extract the table") == doc_engine.MAX_PDF_PAGES
    # A scan must be SEEN.
    assert page_images_wanted(SCAN, 3, "fast", "summarize") == doc_engine.MAX_PDF_PAGES
    assert page_images_wanted([], 0, "think", "x") == 0


def test_cache_roundtrip_and_policy_gate(tmp_path):
    root = str(tmp_path / "up")
    doc = _Doc("a.pdf", "[Page 1]\n" + RICH[0], ["data:image/png;base64,AAA", "data:image/png;base64,BBB"], 12, 0, RICH)
    path = doc_engine.write_document_cache(root, doc)
    assert os.path.isfile(path)
    # A Fast answer trims the renders it does not want.
    fast = doc_engine.load_document_cache(root, "a.pdf", effort="fast", question="summarize")
    assert fast is not None and fast.images == [] and fast.total == 12 and fast.full_text.startswith("[Page 1]")
    # A Think answer gets exactly the layout pages that were prewarmed.
    think = doc_engine.load_document_cache(root, "a.pdf", effort="think", question="summarize")
    assert think is not None and len(think.images) == 2
    # A visual question wants more than was rendered → no cache, extract fresh.
    assert doc_engine.load_document_cache(root, "a.pdf", effort="think", question="show me the chart") is None
    # No cache at all → None, never an exception.
    assert doc_engine.load_document_cache(str(tmp_path / "missing"), "a.pdf") is None


def test_engine_accepts_a_pre_extracted_document(monkeypatch):
    rec = {}

    async def fake_stream(messages, **kw):
        rec["messages"] = messages
        yield "token", "ok"

    monkeypatch.setattr(doc_engine.llm, "stream_chat_events", fake_stream)
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    doc = _Doc("cached.pdf", "[Page 1]\nHello from the cache", [], 1, 0, ["Hello from the cache"])
    out = asyncio.run(doc_engine.run_pdf_engine_multi("what does it say", [doc], [], emit, effort="fast"))
    assert out == "ok"
    parts = rec["messages"][-1]["content"]
    assert any("Hello from the cache" in p.get("text", "") for p in parts if p["type"] == "text")
    assert not any(p["type"] == "image_url" for p in parts)
    meta = [p for k, p in events if k == "meta"][-1]
    assert meta["document"]["filename"] == "cached.pdf" and meta["document"]["total_pages"] == 1


def test_ocr_on_the_image_route_gives_up_at_the_deadline(monkeypatch):
    async def slow(client, image, max_tokens=None):
        await asyncio.sleep(0.3)
        return "late transcript"

    monkeypatch.setattr(settings, "ocr_enabled", True)
    monkeypatch.setattr(ocr, "_ocr_one", slow)
    monkeypatch.setattr(ocr.llm if hasattr(ocr, "llm") else __import__("app.llm", fromlist=["_client"]), "_client", lambda base: object(), raising=False)
    out = asyncio.run(ocr.ocr_images(["img1", "img2"], max_output_tokens=500, deadline_s=0.05))
    assert out == ["", ""], "past the deadline the answer proceeds pixels-only"
    out = asyncio.run(ocr.ocr_images(["img1"], deadline_s=2.0))
    assert out == ["late transcript"]


def test_ocr_output_cap_is_honoured(monkeypatch):
    seen = {}

    class _Choices:
        def __init__(self):
            self.message = type("M", (), {"content": "text"})()
            self.finish_reason = "stop"

    class _Resp:
        choices = [_Choices()]

    class _Completions:
        async def create(self, **kw):
            seen["max_tokens"] = kw["max_tokens"]
            return _Resp()

    class _Client:
        chat = type("C", (), {"completions": _Completions()})()

    monkeypatch.setattr(settings, "ocr_enabled", True)
    monkeypatch.setattr(__import__("app.llm", fromlist=["_client"]), "_client", lambda base: _Client())
    asyncio.run(ocr.ocr_images(["img"], max_output_tokens=1500))
    assert seen["max_tokens"] == min(1500, ocr.output_limit())
    asyncio.run(ocr.ocr_images(["img"]))
    assert seen["max_tokens"] == ocr.output_limit(), "document pages keep the full budget"


def test_prewarm_writes_the_cache_and_skips_what_it_should(tmp_path, monkeypatch):
    from app import uploads

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "document_prewarm_enabled", True)
    root = uploads.upload_root("conv1", "u1")
    os.makedirs(os.path.join(root, "_original"), exist_ok=True)
    with open(os.path.join(root, "_original", "notes.txt"), "w") as fh:
        fh.write("hello prewarm " * 50)

    asyncio.run(uploads._prewarm_document("conv1", "u1", "notes.txt"))
    cached = doc_engine.load_document_cache(root, "notes.txt", effort="fast", question="")
    assert cached is not None and "hello prewarm" in cached.full_text

    # Archives and oversized files are left to the chat path.
    scheduled = []
    monkeypatch.setattr(uploads.asyncio, "create_task", lambda coro: scheduled.append(coro) or coro.close() or type("T", (), {"add_done_callback": lambda self, cb: None})())
    uploads._schedule_prewarm("conv1", "u2", "bundle.zip", 10)
    uploads._schedule_prewarm("conv1", "u3", "huge.pdf", (settings.document_prewarm_max_mb + 1) * 1024 * 1024)
    assert scheduled == []


def test_resolve_document_refs_prefers_the_cache(tmp_path, monkeypatch):
    from app import uploads
    from app.main import _resolve_document_refs

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    root = uploads.upload_root("conv1", "a" * 32)
    os.makedirs(os.path.join(root, "_original"), exist_ok=True)
    with open(os.path.join(root, "_original", "report.pdf"), "wb") as fh:
        fh.write(b"%PDF-1.4 not really")
    # Born-digital pages (a thin page would mean "scan → render", which a
    # cache with no renders cannot serve).
    rich = ["cached text " * 40] * 3
    doc_engine.write_document_cache(root, _Doc("report.pdf", "[Page 1]\ncached text", [], 3, 0, rich))

    class Req:
        pdf_uploads = [{"upload_id": "a" * 32, "name": "report.pdf"}]
        pdf_data = None
        pdf_filename = None
        effort = "fast"
        text = "summarize"

    docs, images, err = asyncio.run(_resolve_document_refs(Req(), "conv1"))
    assert err is None and images == []
    assert len(docs) == 1 and isinstance(docs[0], _Doc) and docs[0].full_text.endswith("cached text")
