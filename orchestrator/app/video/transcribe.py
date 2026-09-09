"""Audio -> timestamped segments, one VAD window at a time, stitched.

The speech engine (compose/whisper/server.py) answers one clip at a time and
refuses clips over ten minutes. `vad.plan_windows` cuts the recording into
clips that open on speech, never span a long pause, and overlap only where
continuous speech had to be split. This module sends each clip through the
orchestrator's ASR client — the SAME `RoutedProvider` dictation uses, so a
window lands on whichever engine is freest — and puts the pieces back
together in video time.

WHY THE CLIPS ARE WAV BYTES BUILT IN MEMORY. The engine decodes with ffmpeg
on stdin; a container it cannot seek (an MP4 with its index at the end) is a
"moov atom not found" from a pipe. PCM in a RIFF header has no index, decodes
in microseconds, and a 240-second window at 16 kHz mono 16-bit is 7.7 MB —
under the engine's 32 MiB upload ceiling with room to spare.

SEAMS. Two adjacent clips that overlapped (a lecture with no pauses) both
transcribe the overlap. `stitch` keeps the earlier clip's version of a
segment that ends inside the overlap and drops the later clip's copy, then
checks the first surviving segment of the later clip against the tail of
the earlier one textually, because Whisper's timestamps at a clip boundary
drift by up to a second.
"""
from __future__ import annotations

import io
import logging
import os
import struct
import time
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .types import Segment
from .vad import SAMPLE_RATE, Window, plan_windows

log = logging.getLogger(__name__)

Progress = Callable[[Optional[float], str], Awaitable[None]]


# --------------------------------------------------------------------- PCM --


def read_wav_pcm16(path: str):
    """Memory-map the sample data of a 16 kHz mono 16-bit WAV.

    Parses the RIFF chunks rather than assuming a 44-byte header: ffmpeg
    writes a LIST chunk before `data`, and a fixed offset would return
    metadata bytes as audio. Returns a read-only int16 numpy view.
    """
    import numpy as np

    with open(path, "rb") as fh:
        head = fh.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError("not a RIFF/WAVE file")
        channels, rate, bits = 1, SAMPLE_RATE, 16
        while True:
            chunk = fh.read(8)
            if len(chunk) < 8:
                raise ValueError("WAV has no data chunk")
            tag, size = chunk[:4], struct.unpack("<I", chunk[4:8])[0]
            if tag == b"fmt ":
                fmt = fh.read(size)
                _tag, channels, rate = struct.unpack("<HHI", fmt[:8])
                bits = struct.unpack("<H", fmt[14:16])[0]
                if size % 2:
                    fh.read(1)
            elif tag == b"data":
                offset = fh.tell()
                break
            else:
                fh.seek(size + (size % 2), 1)
    if channels != 1 or rate != SAMPLE_RATE or bits != 16:
        raise ValueError(f"expected 16 kHz mono 16-bit PCM, got {rate} Hz {channels} ch {bits} bit")
    total = (os.path.getsize(path) - offset) // 2 if size == 0 or size == 0xFFFFFFFF else size // 2
    return np.memmap(path, dtype="<i2", mode="r", offset=offset, shape=(int(total),))



def wav_bytes(pcm16) -> bytes:
    """A canonical 44-byte-header WAV around int16 samples."""
    data = pcm16.tobytes()
    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + len(data)))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16))
    buf.write(b"data")
    buf.write(struct.pack("<I", len(data)))
    buf.write(data)
    return buf.getvalue()


# ------------------------------------------------------------------ stitch --

_SEAM_TOLERANCE_S = 0.35


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def stitch(
    pieces: Sequence[Tuple[Window, List[Segment]]],
) -> List[Segment]:
    """Join per-window segments into one timeline, de-duplicating seams."""
    out: List[Segment] = []
    prev_window: Optional[Window] = None
    for window, segments in pieces:
        segs = sorted(segments, key=lambda s: (s.start_s, s.end_s))
        if window.overlaps_previous and prev_window is not None and out:
            seam = prev_window.end_s
            # Anything the earlier clip already covered up to the seam is
            # dropped from the later one; a segment that straddles the seam
            # is kept because the earlier clip was cut mid-segment there.
            kept = [s for s in segs if s.end_s > seam - _SEAM_TOLERANCE_S]
            # And one textual check, for the drifted-timestamp case: the
            # first survivor repeating the tail of the previous clip.
            if kept and out:
                tail = _norm(out[-1].text)
                head = _norm(kept[0].text)
                if head and tail and (head in tail or tail.endswith(head) or head == tail):
                    kept = kept[1:]
            segs = kept
        for s in segs:
            if not s.text.strip():
                continue
            if out and s.start_s < out[-1].end_s - _SEAM_TOLERANCE_S and _norm(s.text) == _norm(out[-1].text):
                continue
            out.append(s)
        prev_window = window
    # Monotonic and non-overlapping, which subtitle players insist on.
    fixed: List[Segment] = []
    for s in out:
        start = s.start_s
        if fixed and start < fixed[-1].end_s:
            start = fixed[-1].end_s
        end = max(s.end_s, start)
        fixed.append(Segment(start, end, s.text, s.language))
    return fixed


def dominant_language(segments: Sequence[Segment]) -> Optional[str]:
    """The language most of the speech is in, weighted by duration."""
    weight: Dict[str, float] = {}
    for s in segments:
        if s.language:
            weight[s.language] = weight.get(s.language, 0.0) + max(0.1, s.end_s - s.start_s)
    if not weight:
        return None
    return max(weight.items(), key=lambda kv: kv[1])[0]


# ------------------------------------------------------------- transcribe --


async def transcribe_audio(
    wav_path: str,
    *,
    total_s: float,
    progress: Progress,
    max_window_s: float,
    max_gap_s: float,
    overlap_s: float,
) -> Tuple[List[Segment], Optional[str], dict]:
    """The whole audio track -> (segments, language, report).

    Windows go to the engine SEQUENTIALLY. The engine decodes one clip at a
    time per node and the pool for batch work holds one slot on purpose (see
    `asr.transcribe_segments`): a video must never take both nodes' engines
    at once, because dictation would then answer "busy" and the chat model —
    tensor-parallel across both nodes — would slow for everyone.
    """
    import asyncio

    from .. import asr
    from . import pipeline as _pipe

    pcm = await asyncio.to_thread(read_wav_pcm16, wav_path)
    windows, report = await asyncio.to_thread(
        plan_windows,
        pcm,
        total_s=total_s,
        max_window_s=max_window_s,
        max_gap_s=max_gap_s,
        overlap_s=overlap_s,
    )
    if not windows:
        return [], None, {**report, "engine_ms": 0, "windows_done": 0}

    speech_total = sum(w.duration_s for w in windows) or 1.0
    done_s = 0.0
    engine_ms = 0
    pieces: List[Tuple[Window, List[Segment]]] = []
    started = time.perf_counter()
    failures = 0
    paced_s = 0.0
    for i, window in enumerate(windows):
        paced_s += await _pipe.pace()
        a = int(window.start_s * SAMPLE_RATE)
        b = int(window.end_s * SAMPLE_RATE)
        clip = await asyncio.to_thread(wav_bytes, pcm[a:b])
        try:
            # The engine's own silence gate is OFF here: it judges a clip on
            # its first 30 s, and a window that opens on a quiet lead-in
            # would lose every word after it (a 12-second LibriVox intro
            # scored 0.62 against the 0.6 threshold and came back empty; a
            # four-minute window would go the same way). The windows above
            # are voice-activity regions already — that is the gate.
            result = await asr.transcribe_segments(
                clip, filename=f"w{i:04d}.wav", content_type="audio/wav", no_speech_check=False
            )
        except asr.ASRRejected as exc:
            # The engine refused THIS clip (a decode fault, say). One bad
            # window is a gap, not a failed video — recorded, and on we go.
            log.warning("window %d (%.1f-%.1fs) rejected: %s", i, window.start_s, window.end_s, exc)
            failures += 1
            if failures > max(3, len(windows) // 4):
                raise
            continue
        engine_ms += int(result.engine_ms or 0)
        segs = [
            Segment(
                start_s=window.start_s + float(s.get("start", 0.0)),
                end_s=window.start_s + float(s.get("end", 0.0)),
                text=str(s.get("text") or "").strip(),
                language=(str(s.get("language")) if s.get("language") else result.language_code),
            )
            for s in (result.segments or [])
        ]
        pieces.append((window, segs))
        done_s += window.duration_s
        await progress(
            100.0 * done_s / speech_total,
            f"{_mmss(window.end_s)} of {_mmss(total_s)} · {len(windows) - i - 1} clip(s) left",
        )
    segments = stitch(pieces)
    language = dominant_language(segments)
    report = {
        **report,
        "engine_ms": engine_ms,
        "windows_done": len(pieces),
        "windows_failed": failures,
        "paced_s": round(paced_s, 1),
        "segments": len(segments),
        "chars": sum(len(s.text) for s in segments),
        "wall_s": round(time.perf_counter() - started, 2),
    }
    return segments, language, report


def _mmss(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
