"""Which extracted frames are worth reading: perceptual-hash dedupe and a cap.

`media.extract_frames` keeps a frame at every scene change AND every FLOOR
seconds. A slide held for six minutes therefore arrives as a dozen
near-identical JPEGs. Reading each one with OCR would cost a dozen model
calls to learn the same words, and — worse for the reader — would fill the
evidence with a dozen copies of the same text at different times.

A perceptual hash collapses that: two frames whose 64-bit pHash differs in a
handful of bits are the same picture. The FIRST of a run is kept, and the run
is remembered as a SPAN (`t_s` .. `end_s`), so "the pricing slide was on
screen from 1:22 to 7:40" survives even though only one frame was read.

pHash is implemented here in a few lines of NumPy rather than through the
`imagehash` package, which drags in SciPy for its DCT. A 32x32 DCT is one
matrix product on each side.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import List, Optional, Sequence

from .media import FrameFile

log = logging.getLogger(__name__)

#: Hamming distance at or below which two frames are "the same picture".
#: 64-bit pHash: identical slides score 0-2, a slide with one bullet added
#: scores ~6-12, different slides score 20+. 5 keeps the added bullet as a new
#: frame, which is the right call for on-screen text.
DEFAULT_DISTANCE = 5


@dataclass(frozen=True)
class KeptFrame:
    """A frame that will be read, and the span of time it stood for."""

    path: str
    t_s: float
    end_s: float
    index: int
    phash: int
    #: How many raw frames collapsed into this one (1 = it stood alone).
    collapsed: int = 1


def _dct_matrix(n: int):
    import numpy as np

    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    c = np.sqrt(2.0 / n) * np.cos(np.pi * (2 * i + 1) * k / (2 * n))
    c[0, :] *= 1.0 / np.sqrt(2.0)
    return c


def phash_of_gray(gray32) -> int:
    """64-bit perceptual hash of a 32x32 float grayscale array."""
    import numpy as np

    c = _dct_matrix(32)
    d = c @ gray32.astype(np.float64) @ c.T
    low = d[:8, :8].flatten()
    # Median WITHOUT the DC term, which is just overall brightness.
    med = float(np.median(low[1:]))
    bits = 0
    for value in low:
        bits = (bits << 1) | (1 if value > med else 0)
    return bits


def phash_file(path: str) -> int:
    """Hash a JPEG on disk. Any read failure hashes to 0 (never matches)."""
    try:
        import numpy as np
        from PIL import Image

        with Image.open(path) as im:
            small = im.convert("L").resize((32, 32), Image.LANCZOS)
            return phash_of_gray(np.asarray(small, dtype=np.float64))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not hash %s: %s", path, exc)
        return 0


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def dedupe(
    frames: Sequence[FrameFile],
    *,
    total_s: float,
    distance: int = DEFAULT_DISTANCE,
    hashes: Optional[Sequence[int]] = None,
) -> List[KeptFrame]:
    """Collapse runs of near-identical frames into spans.

    Only CONSECUTIVE duplicates collapse. A slide shown at 2:00, replaced at
    2:30 and shown again at 9:00 is two spans, because it was on screen twice
    and a question about "the slide at nine minutes" should find it there.
    """
    kept: List[KeptFrame] = []
    if not frames:
        return kept
    if hashes is None:
        hashes = [phash_file(f.path) for f in frames]
    for f, h in zip(frames, hashes):
        if kept and h and kept[-1].phash and hamming(kept[-1].phash, h) <= distance:
            prev = kept[-1]
            kept[-1] = replace(prev, end_s=f.t_s, collapsed=prev.collapsed + 1)
            continue
        if kept:
            kept[-1] = replace(kept[-1], end_s=f.t_s)
        kept.append(KeptFrame(path=f.path, t_s=f.t_s, end_s=f.t_s, index=f.index, phash=h))
    kept[-1] = replace(kept[-1], end_s=max(kept[-1].t_s, total_s))
    return kept


def frame_cap(duration_s: float, *, per_seconds: float, minimum: int, maximum: int) -> int:
    """How many frames a video of this length may keep: one per `per_seconds`,
    clamped. A twelve-second clip still gets `minimum`; a twelve-hour one
    never exceeds `maximum` — the reader's budget is a fixed number of model
    calls, not a fixed fraction of the video."""
    return int(max(minimum, min(maximum, duration_s / max(1.0, per_seconds))))


def thin(kept: Sequence[KeptFrame], cap: int) -> List[KeptFrame]:
    """Keep at most `cap` frames, preferring the ones that stood alone longest.

    Ranking by span length keeps the slides that were actually on screen and
    drops the flicker between them; ties fall to the earlier frame so a cap
    never favours the end of a video over its start. The survivors are
    returned in time order with their spans re-joined so the timeline has no
    holes.
    """
    if cap <= 0 or len(kept) <= cap:
        return list(kept)
    ranked = sorted(kept, key=lambda k: (-(k.end_s - k.t_s), k.t_s))
    chosen = sorted(ranked[:cap], key=lambda k: k.t_s)
    out: List[KeptFrame] = []
    for i, k in enumerate(chosen):
        end = chosen[i + 1].t_s if i + 1 < len(chosen) else max(k.end_s, kept[-1].end_s)
        out.append(replace(k, end_s=end))
    return out


def select(
    frames: Sequence[FrameFile],
    *,
    total_s: float,
    cap: int,
    distance: int = DEFAULT_DISTANCE,
) -> tuple[List[KeptFrame], dict]:
    """dedupe + thin, with the numbers that explain what happened."""
    hashes = [phash_file(f.path) for f in frames]
    deduped = dedupe(frames, total_s=total_s, distance=distance, hashes=hashes)
    final = thin(deduped, cap)
    report = {
        "extracted": len(frames),
        "distinct": len(deduped),
        "kept": len(final),
        "cap": cap,
        "hash_distance": distance,
    }
    return final, report
