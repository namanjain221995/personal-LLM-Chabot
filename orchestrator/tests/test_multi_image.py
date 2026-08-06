"""Composer multi-upload (2026-08-05): up to MAX_IMAGES images per turn.

The frontend sends {image_base64: first, images: [all]} — old clients keep
sending only the single spelling, and both must reach the vision engine.
"""
import pytest
from pydantic import ValidationError

from app.engines.vision import build_user_content
from app.main import MAX_IMAGES, ChatRequest


def test_images_list_wins_over_the_single_spelling():
    req = ChatRequest(message="compare", image_base64="ONE", images=["A", "B"])
    assert req.images_data == ["A", "B"]
    assert req.image_data == "A"


def test_single_image_spelling_still_works():
    req = ChatRequest(message="what is this", image_base64="ONLY")
    assert req.images_data == ["ONLY"]
    assert req.image_data == "ONLY"


def test_more_than_max_images_is_rejected():
    with pytest.raises(ValidationError, match=f"at most {MAX_IMAGES} images"):
        ChatRequest(message="x", images=[f"IMG{i}" for i in range(MAX_IMAGES + 1)])


def test_exactly_max_images_is_accepted():
    req = ChatRequest(message="x", images=[f"IMG{i}" for i in range(MAX_IMAGES)])
    assert len(req.images_data) == MAX_IMAGES


def test_blank_entries_are_dropped_not_counted():
    req = ChatRequest(message="x", images=["A", "", "  ", "B"])
    assert req.images_data == ["A", "B"]


def test_vision_content_carries_one_image_url_part_per_image():
    parts = build_user_content("compare these", ["AAA", "BBB", "CCC"])
    assert parts[0] == {"type": "text", "text": "compare these"}
    urls = [p["image_url"]["url"] for p in parts[1:]]
    assert urls == [
        "data:image/png;base64,AAA",
        "data:image/png;base64,BBB",
        "data:image/png;base64,CCC",
    ]


def test_vision_content_accepts_the_legacy_single_string():
    parts = build_user_content("", "SOLO")
    assert parts[0]["text"] == "Describe this image."
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,SOLO"
