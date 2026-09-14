"""Images from Files: normalisation, the variant ladder, and inline resize.

These are pure Pillow — no database, no engine, no ffmpeg — so the three
autouse fixtures from conftest (a PostgreSQL test DB truncated before every
test, plus the auth shim) are replaced by no-ops here, exactly as
test_api_soak_tool.py does: this module never touches the app database.

What is proved (Files API design §4.4 image extractor, §5.5 images from files):
  * a large image yields the 896 / 1600 / 2560 ladder plus a canonical copy,
    and `detail` selects the right rung;
  * transparency keeps the PNG path; a photo becomes JPEG;
  * EXIF orientation is applied and then stripped, and NO metadata rides into
    an output;
  * a multi-frame GIF collapses to its first frame;
  * the decompression-bomb ceiling refuses an over-pixel image (mutation
    check: shrink the ceiling and the same image is refused), and it is a HARD
    ceiling: an image between 1x and 2x the ceiling — which Pillow's own guard
    only warns about — is refused before any pixel is decoded, on both the
    normalise path and the inline data URL path;
  * an inline data URL is resized down for `detail`, a small one is left
    exactly as sent, and a remote URL is refused rather than fetched (SSRF).
"""
from __future__ import annotations

import base64
import io
import os

import pytest

from app.apifiles import images


# --- no database for this module (see the docstring) ----------------------
@pytest.fixture(scope="session")
def app_database():
    yield None


@pytest.fixture
def isolated_app_db():
    yield None


@pytest.fixture
def ambient_identity():
    yield None


# --- fixtures --------------------------------------------------------------

def _png(width: int, height: int, *, alpha: bool = False, color=(180, 60, 40)) -> bytes:
    from PIL import Image

    mode = "RGBA" if alpha else "RGB"
    fill = (*color, 128) if alpha else color
    im = Image.new(mode, (width, height), fill)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg(width: int, height: int) -> bytes:
    from PIL import Image

    im = Image.new("RGB", (width, height), (30, 120, 200))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _write(tmp_path, name: str, data: bytes) -> str:
    path = os.path.join(str(tmp_path), name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _open_dims(path: str):
    from PIL import Image

    with Image.open(path) as im:
        return im.width, im.height, im.format, im.info


# --- normalise: the ladder -------------------------------------------------


def test_a_large_image_produces_the_full_variant_ladder_and_a_canonical_copy(tmp_path):
    src = _write(tmp_path, "big.png", _png(3000, 2000))
    out = os.path.join(str(tmp_path), "derived")

    result = images.normalise(src, out)

    assert result.width == 3000 and result.height == 2000
    assert result.format == "PNG"
    names = {v.name for v in result.variants}
    # An opaque source: the engine ladder is JPEG; the canonical download copy
    # (route 8's `image.png`) is always PNG.
    assert {"image_896.jpg", "image_1600.jpg", "image_2560.jpg", "image.png"} <= names
    for edge in (896, 1600, 2560):
        w, h, _fmt, _info = _open_dims(os.path.join(out, f"image_{edge}.jpg"))
        assert max(w, h) == edge, f"variant {edge} long edge is {max(w, h)}"
    cw, ch, cfmt, _ = _open_dims(os.path.join(out, "image.png"))
    assert cfmt == "PNG" and max(cw, ch) == 2560


def test_detail_selects_the_matching_ladder_rung(tmp_path):
    src = _write(tmp_path, "big.png", _png(3000, 2000))
    out = os.path.join(str(tmp_path), "derived")
    images.normalise(src, out)

    low_path, low_mime = images.variant_for_detail(out, "low")
    auto_path, _ = images.variant_for_detail(out, "auto")
    high_path, _ = images.variant_for_detail(out, "high")
    original_path, _ = images.variant_for_detail(out, "original")

    assert os.path.basename(low_path) == "image_896.jpg" and low_mime == "image/jpeg"
    assert os.path.basename(auto_path) == "image_1600.jpg"
    assert os.path.basename(high_path) == "image_2560.jpg"
    assert max(_open_dims(high_path)[:2]) == 2560
    assert max(_open_dims(original_path)[:2]) == 2560  # original == largest stored


def test_a_source_smaller_than_the_smallest_rung_keeps_one_variant_at_its_own_size(tmp_path):
    src = _write(tmp_path, "small.png", _png(400, 300))
    out = os.path.join(str(tmp_path), "derived")

    result = images.normalise(src, out)

    # No 896/1600/2560 files — nothing is upscaled — but a served detail still
    # resolves to the one real variant, and the canonical download copy exists.
    assert not os.path.exists(os.path.join(out, "image_896.jpg"))
    assert os.path.exists(os.path.join(out, "image_400.jpg"))
    assert os.path.exists(os.path.join(out, "image.png"))
    path, _mime = images.variant_for_detail(out, "high")
    assert max(_open_dims(path)[:2]) == 400
    assert result.width == 400 and result.height == 300


def test_default_detail_is_auto_and_an_unknown_detail_is_refused():
    assert images.normalise_detail(None) == "auto"
    assert images.variant_tokens("low") == 525 and images.variant_tokens("high") == 3613
    with pytest.raises(images.ImageError):
        images.normalise_detail("medium")


# --- normalise: alpha, metadata, animation --------------------------------


def test_transparency_keeps_the_png_path_and_a_photo_becomes_jpeg(tmp_path):
    rgba = images.normalise(_write(tmp_path, "a.png", _png(2000, 2000, alpha=True)),
                            os.path.join(str(tmp_path), "d_rgba"))
    assert rgba.has_alpha is True
    # Alpha: every variant, ladder and canonical, is PNG.
    assert all(v.mime == "image/png" for v in rgba.variants)

    photo = images.normalise(_write(tmp_path, "p.jpg", _jpeg(2000, 2000)),
                             os.path.join(str(tmp_path), "d_jpg"))
    assert photo.has_alpha is False
    ladder = [v for v in photo.variants if v.name.startswith("image_")]
    assert ladder and all(v.mime == "image/jpeg" and v.name.endswith(".jpg") for v in ladder)
    # The canonical download copy is PNG regardless (route 8's `image.png`).
    canon = next(v for v in photo.variants if v.name == "image.png")
    assert canon.mime == "image/png"


def test_exif_orientation_is_applied_and_then_stripped(tmp_path):
    from PIL import Image

    # A 1000x600 image tagged orientation 6 (rotate 90°): after transpose the
    # stored dimensions are swapped, and the output carries no EXIF at all.
    base = Image.new("RGB", (1000, 600), (10, 20, 30))
    exif = base.getexif()
    exif[0x0112] = 6  # Orientation
    src = os.path.join(str(tmp_path), "rot.jpg")
    base.save(src, format="JPEG", exif=exif)

    result = images.normalise(src, os.path.join(str(tmp_path), "d"))

    assert (result.width, result.height) == (600, 1000), "orientation 6 swaps the axes"
    w, h, _fmt, info = _open_dims(result.variants[0].path)
    assert "exif" not in info, "no EXIF may ride into an output"


def test_a_multi_frame_gif_collapses_to_its_first_frame(tmp_path):
    from PIL import Image

    f0 = Image.new("RGB", (1000, 1000), (255, 0, 0))
    f1 = Image.new("RGB", (1000, 1000), (0, 255, 0))
    src = os.path.join(str(tmp_path), "anim.gif")
    f0.save(src, format="GIF", save_all=True, append_images=[f1], duration=100, loop=0)

    result = images.normalise(src, os.path.join(str(tmp_path), "d"))

    # It decoded (one frame) at the source dimensions, and the canonical copy
    # is a single still image (1000 px → the 896 ladder rung is the largest).
    assert result.format == "GIF"
    assert result.width == 1000 and result.height == 1000
    canon = next(v for v in result.variants if v.name == "image.png")
    w, h, fmt, _info = _open_dims(canon.path)
    assert fmt == "PNG" and max(w, h) == 896


# --- the bomb guard --------------------------------------------------------


def test_the_pixel_ceiling_refuses_an_over_size_image(tmp_path, monkeypatch):
    src = _write(tmp_path, "big.png", _png(2000, 2000))  # 4,000,000 px
    out = os.path.join(str(tmp_path), "d")

    # Baseline: it decodes under the real ceiling.
    images.normalise(src, out)

    # Mutation check: drop the ceiling below the image and the SAME bytes are
    # refused as a bomb, not silently decoded.
    monkeypatch.setenv("PUBLIC_API_FILES_IMAGE_MAX_PIXELS", "1000000")
    assert images.image_max_pixels() == 1_000_000
    with pytest.raises(images.ImageError):
        images.normalise(src, os.path.join(str(tmp_path), "d2"))


def test_pillow_alone_only_errors_above_twice_its_limit_which_is_why_the_check_is_explicit():
    """The measured fact behind `_refuse_over_ceiling` (2026-09-13, Pillow
    12.3.0): with MAX_IMAGE_PIXELS = 1,000,000 a 1,200 x 1,200 image only
    WARNS and decodes. If a Pillow upgrade makes this raise, the explicit
    check is still correct — this test just records why it exists."""
    import warnings

    from PIL import Image

    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = 1_000_000
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(_png(1200, 1200))) as im:
                im.load()
                assert im.size == (1200, 1200)
    finally:
        Image.MAX_IMAGE_PIXELS = previous


def test_normalise_refuses_an_image_between_one_and_two_times_the_pixel_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_API_FILES_IMAGE_MAX_PIXELS", "1000000")
    bomb_bytes = _png(1200, 1200)  # 1,440,000 px: over 1x, under Pillow's 2x error
    # The bomb shape: a solid image compresses to almost nothing, so the byte
    # cap can never stand in for the pixel ceiling.
    assert len(bomb_bytes) < 16 * 1024
    src = _write(tmp_path, "bomb.png", bomb_bytes)

    with pytest.raises(images.ImageTooLarge) as caught:
        images.normalise(src, os.path.join(str(tmp_path), "d"))
    assert "1440000 pixels" in str(caught.value) and "1000000" in str(caught.value)
    assert not os.path.exists(os.path.join(str(tmp_path), "d", "image.png"))

    # The boundary: exactly at the ceiling is allowed (the ceiling is inclusive).
    at = _write(tmp_path, "at.png", _png(1000, 1000))
    result = images.normalise(at, os.path.join(str(tmp_path), "d_at"))
    assert (result.width, result.height) == (1000, 1000)


def test_the_pixel_ceiling_is_enforced_before_any_pixel_is_decoded(tmp_path, monkeypatch):
    from PIL import ImageFile

    monkeypatch.setenv("PUBLIC_API_FILES_IMAGE_MAX_PIXELS", "1000000")

    def _decode_forbidden(self, *a, **k):
        raise AssertionError("pixels were decoded before the ceiling was checked")

    monkeypatch.setattr(ImageFile.ImageFile, "load", _decode_forbidden)
    src = _write(tmp_path, "bomb.png", _png(1200, 1200))

    with pytest.raises(images.ImageTooLarge):
        images.normalise(src, os.path.join(str(tmp_path), "d"))
    with pytest.raises(images.ImageTooLarge):
        images.resize_data_url(_data_url(_png(1200, 1200), "image/png"), "low")


def test_the_byte_ceiling_refuses_an_over_size_original(tmp_path, monkeypatch):
    src = _write(tmp_path, "big.png", _png(1200, 1200))
    monkeypatch.setenv("PUBLIC_API_FILES_IMAGE_MAX_BYTES", "1024")  # 1 KiB
    with pytest.raises(images.ImageError):
        images.normalise(src, os.path.join(str(tmp_path), "d"))


# --- inline data URL resize (design §5.5) ---------------------------------


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def test_an_inline_data_url_is_resized_down_for_detail(tmp_path):
    url = _data_url(_jpeg(3000, 2000), "image/jpeg")

    out = images.resize_data_url(url, "low")

    assert out.startswith("data:image/")
    payload = out.split(",", 1)[1]
    raw = base64.b64decode(payload)
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as im:
        assert max(im.width, im.height) == 896


def test_an_inline_image_already_within_budget_is_returned_unchanged():
    url = _data_url(_jpeg(600, 400), "image/jpeg")
    # 600 <= auto's 1600, so the caller's exact bytes come back.
    assert images.resize_data_url(url, "auto") == url


def test_an_inline_image_between_one_and_two_ceilings_is_refused(monkeypatch):
    """The request-time path decodes caller bytes in the orchestrator process,
    so the ceiling must refuse — not warn, and not fall through to "return the
    original" (ImageError is a ValueError, which the undecodable-image handler
    catches; mutation-checked by deleting `except ImageError: raise`)."""
    monkeypatch.setenv("PUBLIC_API_FILES_IMAGE_MAX_PIXELS", "1000000")
    url = _data_url(_png(1200, 1200), "image/png")

    with pytest.raises(images.ImageTooLarge):
        images.resize_data_url(url, "low")

    # Under the ceiling the same shape is resized as usual.
    monkeypatch.setenv("PUBLIC_API_FILES_IMAGE_MAX_PIXELS", "1440000")
    assert images.resize_data_url(url, "low").startswith("data:image/jpeg;base64,")


def test_a_remote_image_url_is_refused_and_never_fetched():
    with pytest.raises(images.ImageError):
        images.resize_data_url("https://evil.example/x.png", "auto")
    with pytest.raises(images.ImageError):
        images.resize_data_url("data:text/plain;base64,QQ==", "auto")


def test_to_data_url_reads_a_written_variant(tmp_path):
    src = _write(tmp_path, "big.png", _png(2000, 1500, alpha=True))
    out = os.path.join(str(tmp_path), "d")
    images.normalise(src, out)
    path, _mime = images.variant_for_detail(out, "low")

    url = images.to_data_url(path)

    assert url.startswith("data:image/png;base64,")
    raw = base64.b64decode(url.split(",", 1)[1])
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as im:
        assert max(im.width, im.height) == 896
