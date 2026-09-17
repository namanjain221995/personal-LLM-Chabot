"""Is this picture legible at all? Measured, not guessed (2026-09-18).

A prompt rule alone did not stop the invented digit. Measured live on the
audit's own dark, blurred door sign (ground truth "Emergency contact: ext.
4471"), six runs at Think with the character-level honesty rule in place:
three answered "ext. 447?" and said the last digit is unreadable, two still
asserted "4472" — one of them confidently — and one produced no answer at
all. The model cannot reliably tell "I read this" from "I expect this" on a
picture this bad, so the app measures the picture and says so.

WHAT IS MEASURED, from the pixels and nothing else: mean brightness,
contrast (standard deviation of luminance) and edge energy. On the six
images of the audit set this separates the one unreadable photo from every
other, including a DARK but sharp UI screenshot, which must not be flagged:

    01-screenshot.png    mean  24.9  contrast 13.4  edges 28.3   sharp
    02-whiteboard.jpg    mean 239.9  contrast 33.9  edges 46.5   fine
    03-chart.png         mean 195.0  contrast 67.0  edges 61.5   fine
    04-table-photo.jpg   mean 203.5  contrast 79.1  edges 57.7   fine
    05-handwritten.jpg   mean 242.4  contrast 23.4  edges 50.2   fine
    06-dark-blurry.jpg   mean  16.2  contrast  8.6  edges  6.0   FLAGGED

(measured at the thumbnail size below; 6-37 ms per image on this box.)

THE DARK SCREENSHOT IS WHY THE TEST IS A CONJUNCTION. Its contrast, 13.4, is
not far above a low-contrast threshold, but its edges are crisp and it reads
perfectly — flagging it would make the model hedge about text it can see. So
low contrast only counts when the edges are soft with it, and soft edges on
their own have to be very soft.

THIS IS A HINT, NEVER A GATE. The note is appended to the prompt; the image
is answered either way, and any failure to decode is silence, not an error.
"""
from __future__ import annotations

import base64
import binascii
import io
import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

log = logging.getLogger(__name__)

_DATA_URL_RE = re.compile(r"^data:image/[\w.+-]+;base64,", re.I)

#: Contrast below this is a picture whose characters have no edges to stand
#: on. The audit's sign is 8.6; the next lowest in the set is 13.4.
_LOW_CONTRAST = 12.0
#: With low contrast, edges this soft mean the characters are not resolvable.
#: The sign is 6.0; every other image in the set is 28 or above.
_BLURRED_EDGES = 20.0
#: Edge energy this low is blur on its own, whatever the contrast.
_SOFT_EDGES = 12.0
#: Long side the measurement runs at — the statistics are scale-free enough
#: and a thumbnail keeps the cost in milliseconds.
_SAMPLE_PX = 512


def _enabled() -> bool:
    return (os.environ.get("IMAGE_QUALITY_NOTE") or "").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


@dataclass(frozen=True)
class Quality:
    """One image's measurement. `hard` is the only decision this file makes."""

    brightness: float
    contrast: float
    edges: float
    hard: bool

    def sentence(self, index: int, total: int) -> str:
        where = f"Image {index} of {total}" if total > 1 else "The image"
        reasons: List[str] = []
        if self.brightness < 60:
            reasons.append(f"very dark (mean brightness {self.brightness:.0f} of 255)")
        if self.contrast < _LOW_CONTRAST:
            reasons.append(f"very low contrast ({self.contrast:.0f} of 255)")
        if self.edges < _SOFT_EDGES:
            reasons.append("blurred — its edges are soft")
        return f"{where} is " + ", ".join(reasons) + "."


def _decode(image_base64: str) -> Optional[bytes]:
    raw = (image_base64 or "").strip()
    raw = _DATA_URL_RE.sub("", raw)
    try:
        return base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError):
        return None


def measure(image_base64: str) -> Optional[Quality]:
    """Measure one image, or None when it cannot be measured at all."""
    payload = _decode(image_base64)
    if not payload:
        return None
    try:
        from PIL import Image, ImageFilter, ImageStat

        with Image.open(io.BytesIO(payload)) as im:
            grey = im.convert("L")
            grey.thumbnail((_SAMPLE_PX, _SAMPLE_PX))
            stat = ImageStat.Stat(grey)
            edges = ImageStat.Stat(grey.filter(ImageFilter.FIND_EDGES))
            brightness = float(stat.mean[0])
            contrast = float(stat.stddev[0])
            edge_energy = float(edges.stddev[0])
    except Exception as exc:  # noqa: BLE001 — a hint, never a gate
        log.debug("image quality could not be measured: %s", exc)
        return None
    hard = (
        contrast < _LOW_CONTRAST and edge_energy < _BLURRED_EDGES
    ) or edge_energy < _SOFT_EDGES
    return Quality(brightness, contrast, edge_energy, hard)


def legibility_note(images: Sequence[str]) -> str:
    """The block to append when a picture is too poor to read characters off.

    '' when every image is ordinary — which is the common case, so the
    ordinary turn's prompt is not changed by this file at all.
    """
    if not _enabled() or not images:
        return ""
    total = len(images)
    lines = [
        q.sentence(i, total)
        for i, q in ((i, measure(img)) for i, img in enumerate(images, 1))
        if q is not None and q.hard
    ]
    if not lines:
        return ""
    return (
        "\n\nImage quality measured by the app from the pixels (this is a "
        "measurement, not something read in the picture):\n- "
        + "\n- ".join(lines)
        + "\nOn an image flagged here, individual characters may not be "
        "resolvable at all. Give the characters you can actually make out, "
        "put \"?\" where a character is not resolvable, and say the image is "
        "too dark or too blurred to read the rest. Do NOT pick the most "
        "likely digit, letter or word: on this image a confident reading and "
        "a guess look the same, and the person will act on it."
    )
