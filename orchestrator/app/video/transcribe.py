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

LOOPS. After stitching, `loops.collapse` removes what the decoder repeated
rather than heard — a phrase cycling inside one cue, or one cue emitted
dozens of times over a few seconds. 17.9% of a real 2h23m transcript was
that (2026-09-11); the module has the measurement.

A BUSY ENGINE IS NOT A FAILED VIDEO. `ASRBusy` (the batch pool's queue wait
ran out) and `ASRUnavailable` (both engines standing down after a 5xx) are
about the engine at this instant, not about this clip, so a clip that meets
one waits and asks again — four attempts, exponential with jitter. Only a
clip that fails every attempt counts against the failure threshold, and only
crossing that threshold fails the stage. When the stage does fail, every
sibling clip is CANCELLED before the failure leaves this module: gather()
used to leave them decoding to the end of the recording, holding the two
batch-pool slots, so the retry that a failed stage triggers met a busy engine
and failed again (2026-09-10).
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import random
import struct
import time
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from ..config import settings
from . import loops
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

    THE MEMORY BOUND, PRECISELY (2026-09-11). This does NOT read the track:
    `np.memmap` maps it, so the process's own allocation is the mapping
    object, and the bytes that become resident are page cache the kernel
    reclaims under pressure. 16 kHz mono 16-bit is 32 kB/s — 115 MB an hour,
    461 MB for a four-hour recording — and none of it is anonymous memory.

    What IS allocated, per clip in flight: `wav_bytes(pcm[a:b])` copies one
    WINDOW (the slice of a memmap is a view, not a copy) and the RIFF buffer
    holds it a second time, so ~2x the window while the clip is built. The
    window ceiling is `VIDEO_ASR_WINDOW_S` (240 s = 7.7 MB), the clip bytes
    live until the engine answers, and `VIDEO_ASR_CONCURRENCY` (2) clips are
    in flight at once: ~31 MB, independent of how long the recording is.
    `vad.frame_flags` reads the mapping in blocks for the same reason.
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

#: A clip that meets a BUSY or UNREACHABLE engine is sent again this many
#: times before it counts as a failed window. The waits (2 s, 4 s, 8 s, capped
#: at 20 s, each multiplied by a random 0.5-1.0) are sized against the two
#: things that produce those errors: the batch pool refuses after an 8-second
#: queue wait, and an engine that answered 5xx is stood down for 20 seconds.
#: The jitter matters because every clip of a long recording meets the same
#: outage at the same moment and must not come back in step.
_RETRY_ATTEMPTS = 4
_RETRY_BASE_S = 2.0
_RETRY_CAP_S = 20.0


def _backoff_s(attempt: int) -> float:
    """Seconds to wait after a failed attempt, jittered."""
    return min(_RETRY_CAP_S, _RETRY_BASE_S * (2 ** (attempt - 1))) * (0.5 + random.random() / 2.0)


async def _cancel_all(tasks: Sequence["asyncio.Task"]) -> None:
    """Cancel every clip still running and wait for it to actually stop.

    The wait is shielded because this also runs while THIS coroutine is being
    cancelled (a stage timeout, a shutdown), and a cancellation arriving here
    must not leave a clip decoding — leaving one is the whole thing this
    function exists to prevent.
    """
    pending = [t for t in tasks if not t.done()]
    for task in pending:
        task.cancel()
    if not pending:
        return
    try:
        await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
    except asyncio.CancelledError:
        # The caller re-raises the cancellation it was already carrying; the
        # clips have been cancelled, which is what had to happen here.
        pass


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
    started = time.perf_counter()
    # Windows go to the engines `video_asr_concurrency` at a time — two by
    # default, which is one clip per Spark: the router hands each new clip
    # to the engine with the fewest in flight, so the two nodes decode side
    # by side and the transcript takes half the wall clock it did in series.
    # Each clip is still paced against live chat before it is sent.
    width = max(1, int(settings.video_asr_concurrency))
    gate = asyncio.Semaphore(width)
    results: Dict[int, List[Segment]] = {}
    tally = {"engine_ms": 0, "failures": 0, "paced_s": 0.0, "done_s": 0.0, "in_flight": 0, "retries": 0}
    threshold = max(3, len(windows) // 4)

    async def _say() -> None:
        left = len(windows) - len(results) - tally["failures"] - tally["in_flight"]
        done_pct = 100.0 * tally["done_s"] / speech_total
        await progress(
            done_pct,
            f"{len(results)}/{len(windows)} clip(s) done · {tally['in_flight']} in the engine"
            + (f" · {left} waiting" if left > 0 else ""),
        )

    async def send(i: int, clip: bytes):
        """One clip to the engine, waiting out a busy or unreachable one.

        The retry happens INSIDE the dispatch gate, so a stood-down engine
        never turns into every window retrying at once, and each attempt is
        paced against live chat like any other GPU-heavy unit of this
        pipeline — a retry is more work for the same two Sparks.
        """
        last: Exception = asr.ASRUnavailable("no attempt was made")
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                # The engine's own silence gate is OFF here: it judges a clip
                # on its first 30 s, and a window that opens on a quiet
                # lead-in would lose every word after it (a 12-second
                # LibriVox intro scored 0.62 against the 0.6 threshold and
                # came back empty). The windows are voice-activity regions
                # already — that is the gate.
                return await asr.transcribe_segments(
                    clip, filename=f"w{i:04d}.wav", content_type="audio/wav", no_speech_check=False
                )
            except (asr.ASRBusy, asr.ASRUnavailable) as exc:
                last = exc
                if attempt == _RETRY_ATTEMPTS:
                    break
                wait = _backoff_s(attempt)
                tally["retries"] += 1
                log.info(
                    "window %d: %s; asking again in %.1fs (attempt %d of %d)",
                    i, exc, wait, attempt + 1, _RETRY_ATTEMPTS,
                )
                await asyncio.sleep(wait)
                tally["paced_s"] += await _pipe.pace()
        raise last

    async def one(i: int, window: Window) -> None:
        async with gate:
            tally["paced_s"] += await _pipe.pace()
            a = int(window.start_s * SAMPLE_RATE)
            b = int(window.end_s * SAMPLE_RATE)
            clip = await asyncio.to_thread(wav_bytes, pcm[a:b])
            tally["in_flight"] += 1
            await _say()
            try:
                result = await send(i, clip)
            except (asr.ASRRejected, asr.ASRBusy, asr.ASRUnavailable) as exc:
                # Either the engine refused THIS clip (a decode fault, say),
                # or it stayed busy/unreachable through every attempt. One bad
                # window is a gap, not a failed video — recorded, and on we go.
                # Past the threshold the stage gives up, and the dispatcher
                # below cancels the siblings rather than paying for a
                # transcript nobody will read.
                log.warning("window %d (%.1f-%.1fs) failed: %s", i, window.start_s, window.end_s, exc)
                tally["failures"] += 1
                if tally["failures"] > threshold:
                    raise
                return
            finally:
                tally["in_flight"] -= 1
            tally["engine_ms"] += int(result.engine_ms or 0)
            results[i] = [
                Segment(
                    start_s=window.start_s + float(s.get("start", 0.0)),
                    end_s=window.start_s + float(s.get("end", 0.0)),
                    text=str(s.get("text") or "").strip(),
                    language=(str(s.get("language")) if s.get("language") else result.language_code),
                )
                for s in (result.segments or [])
            ]
            tally["done_s"] += window.duration_s
            await _say()

    # FAIL FAST, LEAVING NOTHING BEHIND. `gather` propagates the first error
    # and leaves every other clip running to the end of the recording, each
    # holding one of the two batch-pool slots — which is why the retry after a
    # failed transcript stage met 'every transcription slot is busy'. Waiting
    # for the FIRST exception and cancelling the rest keeps the two-wide
    # dispatch and ends the stage with no work outstanding.
    tasks = [
        asyncio.get_running_loop().create_task(one(i, w), name=f"asr-window-{i}")
        for i, w in enumerate(windows)
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    except BaseException:
        # The stage timed out, or the job was cancelled by a shutdown: none of
        # this transcript will be read, so none of it may keep decoding.
        await _cancel_all(tasks)
        raise
    fatal = next(
        (t.exception() for t in done if not t.cancelled() and t.exception() is not None), None
    )
    if fatal is not None:
        await _cancel_all(pending)
        raise fatal
    # Stitching wants the windows in time order, whichever finished first.
    pieces: List[Tuple[Window, List[Segment]]] = [(windows[i], results[i]) for i in sorted(results)]
    engine_ms, failures, paced_s = tally["engine_ms"], tally["failures"], tally["paced_s"]
    segments = stitch(pieces)
    # AND THEN THE LOOPS COME OUT. A Whisper decoder that loses its place
    # repeats itself — 434 copies of "no" inside one cue, "I am a" as 85 cues
    # in sixteen seconds — and none of it was said. See video/loops.py for the
    # measurement and for why this is a post-processing step rather than a
    # decoder setting. It runs after `stitch` so a seam's duplicate is already
    # gone and the timeline is monotonic.
    segments, loop_report = loops.collapse(segments)
    if loop_report["chars_removed"] or loop_report["cue_runs_merged"]:
        log.info(
            "repetition loops removed: %d cue(s) merged from %d run(s), %d character(s)",
            loop_report["cues_before"] - loop_report["cues_after"],
            loop_report["cue_runs_merged"],
            loop_report["chars_removed"],
        )
    language = dominant_language(segments)
    report = {
        **report,
        "engine_ms": engine_ms,
        "windows_done": len(pieces),
        "windows_failed": failures,
        "engine_retries": tally["retries"],
        "paced_s": round(paced_s, 1),
        "segments": len(segments),
        "chars": sum(len(s.text) for s in segments),
        "loops": loop_report,
        "wall_s": round(time.perf_counter() - started, 2),
    }
    return segments, language, report


def _mmss(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
