"""Is this picture legible at all? Measured, not guessed (2026-09-18).

A prompt rule alone did not stop the invented digit. Measured live on the
audit's own dark, blurred door sign (ground truth "Emergency contact: ext.
4471"), six runs at Think with the character-level honesty rule in place:
three answered "ext. 447?" and said the last digit is unreadable, two still
asserted "4472" — one of them confidently — and one produced no answer at
all. The model cannot reliably tell "I read this" from "I expect this" on a
picture this bad, so the app measures the picture and says so.

WHAT IS MEASURED, from the pixels and nothing else: mean brightness,
contrast (standard deviation of luminance) and sharpness (below). Only
sharpness decides; brightness and contrast explain the decision to the
model.

SHARPNESS IS THE STRONGEST EDGES IN THE PICTURE, NOT THE AVERAGE EDGE
(adversarial QA, 2026-09-18). The first version measured "edge energy" as
the standard deviation of PIL's FIND_EDGES over the whole thumbnail. Two
things were wrong with that, both measured:

* the filter draws a one-pixel frame round the image, and the frame
  dominated the statistic: a UNIFORM grey-225 image with nothing in it
  scored 21.4 and a uniform grey-16 one 1.5 - the number tracked
  brightness, not focus. A bright picture whose text was blurred away
  entirely scored 22.3 and was never flagged;
* empty area diluted real edges, so a sharp but SPARSE dark picture looked
  soft: a dark terminal with two crisp lines (13.5), a dark IDE with six
  (12.8), a dark three-row screenshot (19.2) and a sharp night photo of a
  lit sign (11.3) were all declared unreadable, and live at Fast two of
  five answers about the terminal then carried a false "very dark and low
  contrast" disclaimer.

Legibility is a property of the best-resolved characters in the picture, so
the measurement is now the mean of the strongest 0.1 % of neighbouring-pixel
differences (at least 200 pixels), after a 3x3 box filter so sensor noise is
not mistaken for a stroke, with no frame to count. On a labelled set of 37
synthetic pictures (28 readable, 4 unreadable, 2 blank, 2 ambiguous, 1 photo
with no text) every unreadable one scored 7.6 or less (a dark sign with its
last digit smudged, 7.6; the test suite's dark blurred card and text blurred
away on white, 2.0) and every readable one 14.7 or more (a faded green
marker on a whiteboard at a 3-pixel camera blur; the same board in the test
suite's font, 14.2). The threshold sits between them at 11. Two receipts
defocused until their digits are guesses (19.4 and 32.5) are NOT flagged:
the contract is that a readable picture is never declared unreadable, and a
missed flag only leaves the prompt as it was. Contrast and brightness are
still measured, for the sentence the model reads.

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

#: Contrast below this is reported to the model as "very low contrast". It
#: no longer decides anything on its own: a dark terminal with two crisp
#: lines has a contrast of 4.5 and reads perfectly.
_LOW_CONTRAST = 12.0
#: Sharpness (mean of the strongest 0.1 % of neighbour differences, 0-255)
#: below this means no character in the picture is resolved. Measured on the
#: labelled set: unreadable <= 7.6, readable >= 14.2.
_SOFT_EDGES = 11.0
#: Long side the measurement runs at - small text survives it (the sparse
#: 300-dpi scan scores 35) and it keeps the cost at ~10 ms.
_SAMPLE_PX = 512
#: The strongest edges are the top 0.1 % of pixels, and never fewer than this
#: many - two lines of terminal text at 512 px are ~300 edge pixels.
_TOP_FRACTION = 0.001
_TOP_MIN = 200


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
    #: Sharpness: the mean of the strongest 0.1 % of neighbour differences
    #: (`_sharpness`). The name predates 2026-09-18, when it was a
    #: whole-image FIND_EDGES deviation.
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


def _sharpness(grey) -> float:
    """Mean of the strongest neighbour differences in the picture, 0-255.

    `np.diff` has no border to invent, unlike FIND_EDGES, whose frame was
    the old statistic's largest term (see the module docstring)."""
    import numpy as np
    from PIL import ImageFilter

    a = np.asarray(grey.filter(ImageFilter.BoxBlur(1)), dtype=np.float32)
    if a.shape[0] < 2 or a.shape[1] < 2:
        return 0.0
    dx = np.abs(np.diff(a, axis=1))[:-1, :]
    dy = np.abs(np.diff(a, axis=0))[:, :-1]
    grad = np.maximum(dx, dy).ravel()
    n = min(grad.size, max(_TOP_MIN, int(grad.size * _TOP_FRACTION)))
    return float(np.partition(grad, grad.size - n)[grad.size - n:].mean())


def measure(image_base64: str) -> Optional[Quality]:
    """Measure one image, or None when it cannot be measured at all."""
    payload = _decode(image_base64)
    if not payload:
        return None
    try:
        from PIL import Image, ImageStat

        with Image.open(io.BytesIO(payload)) as im:
            grey = im.convert("L")
            grey.thumbnail((_SAMPLE_PX, _SAMPLE_PX))
            stat = ImageStat.Stat(grey)
            brightness = float(stat.mean[0])
            contrast = float(stat.stddev[0])
            sharpness = _sharpness(grey)
    except Exception as exc:  # noqa: BLE001 — a hint, never a gate
        log.debug("image quality could not be measured: %s", exc)
        return None
    return Quality(brightness, contrast, sharpness, sharpness < _SOFT_EDGES)


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
        "a guess look the same, and the person will act on it. A number or "
        "code on such an image may also run on past the characters you can "
        "make out: never say it is complete, that no characters follow, or "
        "that every character is legible - give the characters you can read "
        "and say that more may be there."
    )
