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

SHARPNESS IS HOW STEEP THE STRONGEST EDGES ARE FOR THEIR OWN CONTRAST
(repair round 2, 2026-09-19). Two statistics came before this one, and
each failed a measured case:

* 4810da0 used the standard deviation of PIL's FIND_EDGES over the whole
  thumbnail. The filter's one-pixel frame dominated it (a UNIFORM grey-225
  image scored 21.4, grey-16 1.5), and empty area diluted real edges, so
  sharp but sparse dark pictures - a terminal with two crisp lines, a dark
  IDE, a night photo of a lit sign - were declared unreadable.
* 2f43474 used the mean of the strongest 0.1 % of neighbour differences.
  That number is contrast times sharpness, so a CRISP print in light ink
  scored as blurred (grey 200 on 250: 8.8 against a threshold of 11; a
  pale whiteboard 9.6; one 14 px line on a 4K screenshot 8.2), while a
  dark sign whose last digit is a smeared blob scored 13.3 and lost the
  note it had at 4810da0 - and live at Fast then answered "ext. 4471".

Blur spreads an edge over more pixels whatever its contrast, so the
measurement now divides the two: in every 7x7 neighbourhood, the steepest
one-pixel step over the neighbourhood's range (1.0 for a step, about
0.4/sigma for a Gaussian blur of sigma pixels), taken over the
neighbourhoods whose range is at least half the picture's strongest (so the
strokes, not smooth shading), and summarised by its median (kept as a
percentage in the code). Measured at a 512 px long side on a labelled set
of 216 pictures - 153 readable, 7 unreadable, 5 dark signs with one smeared
digit, 51 photos and blanks - drawn from the 37 of 2f43474, the reviewers'
probes, the repo's own UI screenshots and diagrams, and the OS wallpapers
and account pictures as real photos:

    readable, any brightness       >= 0.438  (a receipt at a 2 px defocus)
    readable, dark (mean < 60)     >= 0.527  (a dim sign at a 1.5 px blur)
    dark sign, smeared last digit  0.432 - 0.483
    unreadable dark signs          0.304 - 0.333
    blurred away / blank           no stroke at all
    photos without text            >= 0.323 (coffee), dark ones >= 0.585

So a picture is flagged when no neighbourhood holds a stroke, when its
edges are soft everywhere (below 0.30, whatever the light), or when it is
dark AND soft (mean brightness below 60 and below 0.51). The last rule is
the one that flags the smeared sign - because the whole dark sign is soft,
exactly as 4810da0 flagged it - and it is a whole-picture rule: one smeared
digit on an otherwise crisp picture scores like the crisp picture (0.765)
and is not seen, which is recorded, not hidden. A dark sign blurred 2.0 px
at 1400 px scores 0.500 and is flagged; at 1.6 px, 0.553, it is not. Of the
photos and blanks, the flagged ones are the three blanks and two receipts
defocused 4 and 6 px (0.255, 0.206), which 2f43474's set called "either".
Contrast and brightness are still measured, for the sentence the model
reads.

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
#: decides nothing: a dark terminal with two crisp lines has a contrast of
#: 4.5 and reads perfectly.
_LOW_CONTRAST = 12.0
#: Mean brightness below this is "very dark", and a dark picture is judged
#: against the stricter softness bar below.
_DARK = 60.0
#: Sharpness (see the module docstring), as a percentage: the steepest step
#: in a stroke's neighbourhood over that neighbourhood's range. Soft
#: everywhere below this, whatever the light: unreadable signs <= 33.3,
#: readable pictures >= 43.8, the one photo below 32.3 is a defocused
#: receipt the builder's set called "either".
_SOFT_EVERYWHERE = 30.0
#: ...and a DARK picture this soft is not resolvable: dark signs with a
#: smeared digit <= 48.3, the softest readable dark picture 52.7.
_SOFT_AND_DARK = 51.0
#: Long side the measurement runs at. Blur is judged relative to the
#: picture, and it keeps the cost to milliseconds.
_SAMPLE_PX = 512
#: The neighbourhood a stroke's edge is judged in (pixels at _SAMPLE_PX).
_WINDOW = 7
#: A neighbourhood whose range is below this holds no stroke (grey levels).
_STROKE_FLOOR = 16
#: Only neighbourhoods at least this fraction of the picture's strongest
#: range (its 99th percentile) count: the strokes, not smooth shading - with
#: every stroked neighbourhood counted, product photos with soft gradients
#: fell to 0.28 and would have been flagged.
_STRONG_FRACTION = 0.5
#: Pixels decoded to measure, read from the header before anything is
#: decoded: a 13000 x 13000 PNG took 847 ms and +353 MiB here in the
#: security review (round 2). 40 MP covers an 8K screenshot (33.2 MP) and a
#: 600-dpi A4 scan (34.8 MP); a JPEG may go to the Files API's own ceiling
#: (apifiles/images.py) because `draft` decodes it at an eighth of its size.
_MAX_PIXELS = 40_000_000
_MAX_JPEG_PIXELS = 89_478_485


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
    #: Sharpness, 0-100 (`_sharpness`); 0 when nothing in the picture is a
    #: stroke. The name predates 2026-09-18, when it was a whole-image
    #: FIND_EDGES deviation.
    edges: float
    hard: bool

    def sentence(self, index: int, total: int) -> str:
        where = f"Image {index} of {total}" if total > 1 else "The image"
        reasons: List[str] = []
        if self.brightness < 60:
            reasons.append(f"very dark (mean brightness {self.brightness:.0f} of 255)")
        if self.contrast < _LOW_CONTRAST:
            reasons.append(f"very low contrast ({self.contrast:.0f} of 255)")
        if self.edges <= 0:
            reasons.append("without a single stroke sharp enough to read")
        elif _is_soft(self.edges, self.brightness):
            reasons.append("blurred — its edges are soft")
        return f"{where} is " + ", ".join(reasons) + "."


def _decode(image_base64: str) -> Optional[bytes]:
    raw = (image_base64 or "").strip()
    raw = _DATA_URL_RE.sub("", raw)
    try:
        return base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError):
        return None


def _is_soft(sharpness: float, brightness: float) -> bool:
    return sharpness < _SOFT_EVERYWHERE or (brightness < _DARK and sharpness < _SOFT_AND_DARK)


def _window(a, op):
    """`op` (np.maximum / np.minimum) over each _WINDOW x _WINDOW
    neighbourhood, edges repeated. Separable, in numpy: PIL's rank filters
    took 57-84 ms per picture here, which the event loop cannot spare."""
    import numpy as np

    r = _WINDOW // 2
    h, w = a.shape
    p = np.pad(a, r, mode="edge")
    rows = op.reduce([p[:, i : i + w] for i in range(_WINDOW)])
    return op.reduce([rows[i : i + h, :] for i in range(_WINDOW)])


def _sharpness(grey) -> float:
    """How steep the picture's strongest edges are for their own contrast,
    as a percentage; 0.0 when no neighbourhood holds a stroke at all."""
    import numpy as np

    a = np.asarray(grey, dtype=np.int16)
    if a.shape[0] < 2 or a.shape[1] < 2:
        return 0.0
    step = np.maximum(np.abs(np.diff(a, axis=1))[:-1, :], np.abs(np.diff(a, axis=0))[:, :-1])
    core = a[:-1, :-1]
    rng = _window(core, np.maximum) - _window(core, np.minimum)
    steepest = _window(step, np.maximum).astype(np.float32)
    stroked = rng >= _STROKE_FLOOR
    if int(stroked.sum()) < 30:
        return 0.0
    strong = stroked & (rng >= _STRONG_FRACTION * float(np.percentile(rng[stroked], 99)))
    return float(np.median(np.minimum(steepest[strong] / rng[strong], 1.0))) * 100.0


def measure(image_base64: str) -> Optional[Quality]:
    """Measure one image, or None when it cannot be measured at all."""
    payload = _decode(image_base64)
    if not payload:
        return None
    try:
        from PIL import Image, ImageStat

        with Image.open(io.BytesIO(payload)) as im:
            ceiling = _MAX_JPEG_PIXELS if (im.format or "").upper() == "JPEG" else _MAX_PIXELS
            if im.width * im.height > ceiling:
                log.debug("image quality not measured: %dx%d is over the ceiling", im.width, im.height)
                return None
            # A JPEG decodes at a reduced scale; other formats ignore it.
            im.draft("L", (_SAMPLE_PX * 2, _SAMPLE_PX * 2))
            grey = im.convert("L")
            grey.thumbnail((_SAMPLE_PX, _SAMPLE_PX))
            stat = ImageStat.Stat(grey)
            brightness = float(stat.mean[0])
            contrast = float(stat.stddev[0])
            sharpness = _sharpness(grey)
    except Exception as exc:  # noqa: BLE001 — a hint, never a gate
        # The type only: a decoder's message can quote the bytes it choked on.
        log.debug("image quality could not be measured: %s", type(exc).__name__)
        return None
    return Quality(brightness, contrast, sharpness, sharpness <= 0 or _is_soft(sharpness, brightness))


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
