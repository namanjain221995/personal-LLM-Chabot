"""Voice activity -> transcription windows.

Whisper sees thirty seconds at a time and the speech server refuses clips
over ten minutes, so a recording has to be cut. WHERE it is cut decides the
transcript's quality:

* Cut inside a word and the word is lost at both ends. Cut in a pause and
  nothing is.
* Send a stretch of silence and Whisper invents a sentence in it — measured
  on this deployment, a silent clip comes back as "Thank you." every time.
  A window that only ever contains speech (plus a little padding) gives the
  model nothing to hallucinate over.
* The speech server's own silence gate judges the FIRST thirty seconds of
  what it is sent. A window that opens on a pause would be judged silent and
  dropped whole. Windows here open ON speech.

So: a voice-activity detector marks speech regions; regions are padded and
merged into windows that never span a long gap and never exceed the window
ceiling; a region of continuous speech longer than the ceiling (a lecture
with no pauses) is split into OVERLAPPING windows and the transcripts are
de-duplicated at the seam by `transcribe.stitch`.

TWO DETECTORS. `webrtcvad` (Google's, a 100 KB C wheel) when it is installed;
a signal-energy detector otherwise. The energy detector is not a stand-in for
the real thing on noisy audio — music reads as speech, a whisper reads as
silence — but the WINDOWING only needs pauses, not phonetics, and a wrong
guess costs a slightly worse cut, never a lost word.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Sequence, Tuple

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
#: webrtcvad accepts 10, 20 or 30 ms frames at 16 kHz.
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


@dataclass(frozen=True)
class Window:
    """A stretch of audio to send to Whisper as one clip."""

    start_s: float
    end_s: float
    #: True when this window's opening overlaps the previous one's tail
    #: (a continuous-speech split); the stitcher de-duplicates that seam.
    overlaps_previous: bool = False

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def _frames_speech_webrtc(pcm16, aggressiveness: int) -> List[bool]:
    import webrtcvad  # lazy: optional dependency

    vad = webrtcvad.Vad(int(aggressiveness))
    raw = pcm16.tobytes()
    step = FRAME_SAMPLES * 2
    flags: List[bool] = []
    for off in range(0, len(raw) - step + 1, step):
        flags.append(bool(vad.is_speech(raw[off : off + step], SAMPLE_RATE)))
    return flags


def _frames_speech_energy(pcm16) -> List[bool]:
    """RMS per frame against an adaptive floor.

    The floor is the 15th percentile of frame energy (what "quiet" sounds
    like in THIS recording) scaled up, with an absolute minimum so a
    digitally silent file is silent rather than "everything above nothing".
    """
    import numpy as np

    n = (len(pcm16) // FRAME_SAMPLES) * FRAME_SAMPLES
    if n == 0:
        return []
    x = pcm16[:n].astype(np.float32).reshape(-1, FRAME_SAMPLES) / 32768.0
    rms = np.sqrt(np.mean(x * x, axis=1) + 1e-12)
    floor = float(np.percentile(rms, 15))
    threshold = max(floor * 4.0, 0.006)
    return [bool(v) for v in (rms > threshold)]


def frame_flags(pcm16, *, aggressiveness: int = 2) -> Tuple[List[bool], str]:
    """Per-30ms speech flags and which detector produced them."""
    try:
        return _frames_speech_webrtc(pcm16, aggressiveness), "webrtcvad"
    except ImportError:
        return _frames_speech_energy(pcm16), "energy"
    except Exception as exc:  # noqa: BLE001 — a detector bug must not lose the recording
        log.warning("webrtcvad failed (%s); using the energy detector", exc)
        return _frames_speech_energy(pcm16), "energy"


def regions_from_flags(
    flags: Sequence[bool],
    *,
    min_speech_s: float = 0.25,
    min_silence_s: float = 0.5,
    pad_s: float = 0.2,
    total_s: float,
) -> List[Tuple[float, float]]:
    """Runs of speech frames -> [(start_s, end_s)], padded and cleaned.

    A pause shorter than `min_silence_s` does not end a region (people breathe
    mid-sentence); a burst shorter than `min_speech_s` is not a region (a
    click, a cough). Each region is padded by `pad_s` so the first and last
    phonemes are not clipped.
    """
    frame_s = FRAME_MS / 1000.0
    min_speech = max(1, int(round(min_speech_s / frame_s)))
    min_silence = max(1, int(round(min_silence_s / frame_s)))
    regions: List[Tuple[int, int]] = []
    i, n = 0, len(flags)
    while i < n:
        if not flags[i]:
            i += 1
            continue
        start = i
        silence_run = 0
        j = i
        last_speech = i
        while j < n:
            if flags[j]:
                silence_run = 0
                last_speech = j
            else:
                silence_run += 1
                if silence_run >= min_silence:
                    break
            j += 1
        end = last_speech + 1
        if end - start >= min_speech:
            regions.append((start, end))
        i = j + 1
    out: List[Tuple[float, float]] = []
    for start, end in regions:
        s = max(0.0, start * frame_s - pad_s)
        e = min(total_s, end * frame_s + pad_s)
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        elif e > s:
            out.append((s, e))
    return out


def windows_from_regions(
    regions: Sequence[Tuple[float, float]],
    *,
    max_window_s: float,
    max_gap_s: float,
    overlap_s: float,
    min_window_s: float = 1.0,
) -> List[Window]:
    """Pack speech regions into clips for the engine.

    Consecutive regions join one window while the pause between them is at
    most `max_gap_s` and the window stays within `max_window_s`. A single
    region longer than the ceiling is cut into pieces of `max_window_s` that
    overlap by `overlap_s`, so no word is lost at a cut and the stitcher can
    drop the repeat. Windows shorter than `min_window_s` are merged into a
    neighbour when one is close, and otherwise kept — a one-word answer is
    still an answer.
    """
    windows: List[Window] = []
    cur_start: float | None = None
    cur_end: float = 0.0

    def _flush() -> None:
        nonlocal cur_start
        if cur_start is None:
            return
        length = cur_end - cur_start
        if length <= max_window_s:
            windows.append(Window(cur_start, cur_end))
        else:
            # Continuous speech longer than one clip: overlapping pieces.
            s = cur_start
            first = True
            while s < cur_end:
                e = min(cur_end, s + max_window_s)
                windows.append(Window(s, e, overlaps_previous=not first))
                first = False
                if e >= cur_end:
                    break
                s = e - overlap_s
        cur_start = None

    for start, end in regions:
        if cur_start is None:
            cur_start, cur_end = start, end
            continue
        gap = start - cur_end
        if gap <= max_gap_s and (end - cur_start) <= max_window_s:
            cur_end = end
        else:
            _flush()
            cur_start, cur_end = start, end
    _flush()

    # Absorb tiny windows into a close neighbour (within one gap).
    merged: List[Window] = []
    for w in windows:
        if merged and w.duration_s < min_window_s and not w.overlaps_previous:
            prev = merged[-1]
            if w.start_s - prev.end_s <= max_gap_s and (w.end_s - prev.start_s) <= max_window_s:
                merged[-1] = Window(prev.start_s, w.end_s, prev.overlaps_previous)
                continue
        merged.append(w)
    return merged


def plan_windows(
    pcm16,
    *,
    total_s: float,
    max_window_s: float,
    max_gap_s: float = 2.0,
    overlap_s: float = 3.0,
    aggressiveness: int = 2,
) -> Tuple[List[Window], dict]:
    """The whole thing: PCM -> windows, plus a small report for the row.

    The report says which detector ran and how much of the recording is
    speech — "0 % speech" on a screen recording with a muted mic is the
    honest explanation for an empty transcript, and it is written down
    rather than left for someone to guess at.
    """
    flags, detector = frame_flags(pcm16, aggressiveness=aggressiveness)
    regions = regions_from_flags(flags, total_s=total_s)
    speech_s = sum(e - s for s, e in regions)
    windows = windows_from_regions(
        regions,
        max_window_s=max_window_s,
        max_gap_s=max_gap_s,
        overlap_s=overlap_s,
    )
    report = {
        "detector": detector,
        "speech_s": round(speech_s, 2),
        "speech_fraction": round(speech_s / total_s, 4) if total_s > 0 else 0.0,
        "regions": len(regions),
        "windows": len(windows),
    }
    return windows, report
