"""Unlimited-OCR integration (2026-08-06): transcripts enhance, never gate."""
import asyncio

from app.config import settings
from app.engines.ocr import clean_transcript, ocr_images, transcript_block


def test_clean_transcript_strips_markers_keeps_text():
    raw = "<|det|>title [[12, 34, 567, 89]]<|/det|>INVOICE #42\nTotal: $99"
    assert clean_transcript(raw) == "INVOICE #42\nTotal: $99"


def test_clean_transcript_handles_empty():
    assert clean_transcript("") == ""
    assert clean_transcript(None) == ""


def test_clean_transcript_strips_the_live_region_format():
    # Observed from the served model (2026-08-06): "type [bbox]Content" lines.
    raw = (
        "text [31, 306, 212, 347]Vendor: TechSara Solutions Pvt Ltd\n"
        "text [31, 500, 131, 540]Total: USD 4,250.00\n"
        "title [10, 10, 400, 60]INVOICE #TS-2026-0806"
    )
    assert clean_transcript(raw) == (
        "Vendor: TechSara Solutions Pvt Ltd\n"
        "Total: USD 4,250.00\n"
        "INVOICE #TS-2026-0806"
    )


def test_transcript_block_empty_when_nothing_legible():
    # The model must never see an "OCR transcript:" header with no body.
    assert transcript_block(["", "  "], "page") == ""
    assert transcript_block([], "page") == ""


def test_transcript_block_single_and_multi():
    single = transcript_block(["hello"], "image")
    assert "OCR transcript of the image" in single
    assert "hello" in single

    multi = transcript_block(["one", "", "three"], "page")
    assert "--- Page 1 ---" in multi
    assert "(nothing legible)" in multi
    assert "--- Page 3 ---" in multi


def test_ocr_disabled_returns_empty_per_image(monkeypatch):
    monkeypatch.setattr(settings, "ocr_enabled", False)
    assert asyncio.run(ocr_images(["AAA", "BBB"])) == ["", ""]


def test_ocr_failure_is_an_empty_transcript_not_an_error(monkeypatch):
    """Service down → '' per image, order kept, no exception (fallback path)."""
    monkeypatch.setattr(settings, "ocr_enabled", True)

    class BrokenClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    raise ConnectionError("vllm-ocr is down")

    from app import llm

    monkeypatch.setattr(llm, "_client", lambda base_url, api_key=None: BrokenClient())
    assert asyncio.run(ocr_images(["AAA", "BBB"])) == ["", ""]
