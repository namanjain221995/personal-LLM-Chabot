"""Four LOW findings from the independent vision review (2026-09-19), each
pinned so removing its fix turns a test red."""
from __future__ import annotations

import base64
import io
import logging

from PIL import Image

from app.engines import image_memory, image_quality, vision


def _ico_data_url() -> str:
    buf = io.BytesIO()
    Image.new("RGBA", (64, 64), (200, 30, 30, 255)).save(buf, format="ICO")
    return "data:image/x-icon;base64," + base64.b64encode(buf.getvalue()).decode()


def test_an_ico_is_never_opened_for_measuring():
    """ICO/CUR decode their embedded PNG inside Image.open, before any pixel
    ceiling can run: a 256x256 ICO wrapping a 12000x12000 PNG cost +559 MiB."""
    assert image_quality.measure(_ico_data_url()) is None


def test_an_ico_is_never_opened_for_downscaling():
    assert image_memory._smaller_copy(_ico_data_url(), 10_000) is None


def test_pillow_never_logs_below_info():
    """Pillow's TIFF plugin logs short tag values at DEBUG, e.g.
    ImageDescription b'PIN 4471 secret'."""
    assert logging.getLogger("PIL").getEffectiveLevel() >= logging.INFO


def test_a_phone_like_or_long_cell_is_not_copied_as_a_name():
    assert vision._data_name("CALL 0800 555 0199 NOW", "row 3") == "row 3"
    assert vision._data_name("Buy two get one free today", "row 4") == "row 4"
    # ordinary names still come through
    assert vision._data_name("East", "row 1") == "East"
    assert vision._data_name("Widget Pro X", "row 2") == "Widget Pro X"
    assert vision._data_name("SKU-4471", "row 5") == "SKU-4471"


def test_a_question_about_chart_types_is_not_a_back_reference():
    ask = "What's the best chart type for showing sign-ups over time?"
    assert image_memory._BACK_REFERENCE.search(ask) is None
    assert image_memory._DEMONSTRATIVE.search("which of those chart styles is clearest?") is None
    # a real back-reference still is one
    assert image_memory._BACK_REFERENCE.search("Which month is highest in the chart?")
    assert image_memory._DEMONSTRATIVE.search("what is the smallest value in that chart?")
