"""Images: decode safely, then resolution variants (design §4.4, §5.5).

DECODE ORDER, AND WHY. `PIL.Image.MAX_IMAGE_PIXELS` is set to the kind's pixel
ceiling (89,478,485, Pillow's own decompression-bomb default) and bombs are
made an ERROR, not a warning — Pillow only warns between 1× and 2× the limit,
and a warning decodes the bomb anyway. Then `open` (header only) → `verify()`
(structure, no pixels) → reopen (verify leaves the image unusable) → first
frame only for GIF/TIFF → `ImageOps.exif_transpose` (a phone photo's pixels
are sideways until the orientation tag is applied) → RGB or RGBA.

VARIANTS. Long edge 896, 1,600 and 2,560 px — only those at or below the
source, never an upscale — as PNG when the image has alpha, JPEG q=90 else;
`image.png` is the normalised copy (long edge ≤ 2,560) the derived allowlist
serves. Saving from a fresh `Image` built from the pixels drops EXIF, ICC and
text chunks: GPS coordinates in a phone photo must not survive into a file
another key of the project can download. Token costs per detail level are
team MODEL INPUT's (§5.5): ~525 at 896 px, ~1,413 at 1600×900, ~3,613 at
2560×1440.
"""
from __future__ import annotations

import io
import os
from typing import Any, Dict, List, Tuple

from . import FileCorrupt, FileTooComplex, Spec, atomic_write_bytes, check_source_size

VARIANT_EDGES = (896, 1600, 2560)
NORMALISED_NAME = "image.png"
NORMALISED_EDGE = 2560
JPEG_QUALITY = 90


def _load(spec: Spec):
    from PIL import Image, ImageOps

    check_source_size(spec, 64 * 1024 * 1024)
    pixels = int(spec.cap("pixels", 89_478_485))
    Image.MAX_IMAGE_PIXELS = pixels
    import warnings

    warnings.simplefilter("error", Image.DecompressionBombWarning)
    try:
        with Image.open(spec.source) as probe:
            probe.verify()
        image = Image.open(spec.source)
        fmt = str(image.format or "").upper()
        width, height = image.size
        if width * height > pixels:
            raise FileTooComplex(f"more than {pixels:,} pixels")
        image.seek(0)  # the first frame of a GIF / multi-page TIFF
        image = ImageOps.exif_transpose(image)
        has_alpha = image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info)
        image = image.convert("RGBA" if has_alpha else "RGB")
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise FileTooComplex(f"more than {pixels:,} pixels") from None
    except FileTooComplex:
        raise
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 — UnidentifiedImageError, truncated data, bad chunks
        raise FileCorrupt() from None
    return image, fmt, has_alpha


def _clean_copy(image):
    """Pixels only: a new image from the pixel buffer carries no metadata."""
    from PIL import Image

    clean = Image.new(image.mode, image.size)
    clean.paste(image)
    return clean


def _encode(image, has_alpha: bool) -> Tuple[bytes, str]:
    buf = io.BytesIO()
    if has_alpha:
        image.save(buf, format="PNG", optimize=False)
        return buf.getvalue(), "png"
    image.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue(), "jpg"


def _resized(image, edge: int):
    from PIL import Image

    width, height = image.size
    scale = edge / float(max(width, height))
    if scale >= 1.0:
        return image
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return image.resize(size, Image.LANCZOS)


def decode(spec: Spec) -> Dict[str, Any]:
    """The `decode` stage: prove the image is safe to decode; facts only."""
    image, fmt, has_alpha = _load(spec)
    width, height = image.size
    return {"width": width, "height": height, "format": fmt or "UNKNOWN", "alpha": has_alpha}


def variants(spec: Spec) -> Dict[str, Any]:
    """The `variants` stage: image_<edge>.<ext> + image.png, metadata stripped."""
    image, fmt, has_alpha = _load(spec)
    image = _clean_copy(image)
    width, height = image.size
    written: List[str] = []
    long_edge = max(width, height)
    for edge in VARIANT_EDGES:
        # Only variants at or below the source — never an upscale — EXCEPT
        # the smallest, which always exists (as the source size when the
        # image is smaller than 896 px) so every detail level has a file.
        if edge > long_edge and edge != VARIANT_EDGES[0]:
            continue
        payload, ext = _encode(_resized(image, edge), has_alpha)
        name = f"image_{edge}.{ext}"
        atomic_write_bytes(os.path.join(spec.derived_dir, name), payload)
        written.append(name)
    normalised = io.BytesIO()
    _resized(image, NORMALISED_EDGE).save(normalised, format="PNG")
    atomic_write_bytes(os.path.join(spec.derived_dir, NORMALISED_NAME), normalised.getvalue())
    written.append(NORMALISED_NAME)
    return {"width": width, "height": height, "format": fmt or "UNKNOWN", "variants": written}
