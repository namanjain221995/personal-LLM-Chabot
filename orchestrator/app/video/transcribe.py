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

CUE TIMES. The engine's timestamps are not anchored to the speech: it opens a
cue where the previous one ended (in the pause), and it can squeeze a long
utterance into a short stamp seconds late (21.8 s on a live clip,
2026-09-18). Each window carries the VAD regions it covers, and
`snap_to_regions` holds its cues to them before the seams are stitched.

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
from bisect import bisect_right
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from ..config import settings
from . import loops
from .artifacts import _MIN_CUE_S
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
    ends: List[float] = []  # the end of each kept cue's window
    prev_window: Optional[Window] = None
    for window, segments in pieces:
        # By start alone, and stable: two cues at the same start keep the
        # engine's order, which is the order the words were said. Ending
        # the key on `end_s` put the shorter cue first, and snapping makes
        # such ties (see `snap_to_regions`).
        segs = sorted(segments, key=lambda s: s.start_s)
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
        first_here = len(out)
        for s in segs:
            if not s.text.strip():
                continue
            # The engine repeating itself. Inside a snapped window that was
            # judged on the engine's own times (`_without_engine_repeats`):
            # snapped times can make two real replies overlap -- a "No." the
            # VAD missed, moved onto the region where a second "No." begins,
            # was dropped as its repeat (QA r3). So: across a seam, and in a
            # window nobody snapped.
            if (
                out
                and (not window.regions or len(out) == first_here)
                and s.start_s < out[-1].end_s - _SEAM_TOLERANCE_S
                and _norm(s.text) == _norm(out[-1].text)
            ):
                continue
            out.append(s)
            ends.append(window.end_s)
        prev_window = window
    # Monotonic and non-overlapping, which subtitle players insist on.
    fixed: List[Segment] = []
    for n, s in enumerate(out):
        start, end = s.start_s, s.end_s
        if fixed and start < fixed[-1].end_s:
            start = fixed[-1].end_s
            # Pushed off the cue before: keep up to a minimum cue of its own
            # length, inside its window. Cut to 0 s, the SRT writer stretched
            # it to `_MIN_CUE_S` over the cue after (QA r2, r3). A run of
            # short cues after it moves by the same push, no more, and the
            # first gap or longer cue takes it up.
            end = max(end, min(start + min(max(0.0, s.end_s - s.start_s), _MIN_CUE_S), ends[n]))
        end = max(end, start)
        fixed.append(Segment(start, end, s.text, s.language))
    return fixed


# ------------------------------------------------------------------- snap --

#: How far into a speech region a cue may reach at its edge before that
#: region counts as holding some of the cue's words. The engine opens a cue
#: where the previous cue ended, and ends it a little before the speech does:
#: on the live 70 s clip (2026-09-18) the cue for words at 18.40 s began at
#: 16.12 s, 0.49 s inside the previous region's tail — 0.27 s of that is the
#: detector's padding (`regions_from_flags` pads 0.2 s) and frame rounding,
#: 0.22 s the engine closing the earlier cue before its last word ended —
#: and none of its words were there. A cue that reaches further into two
#: regions is one sentence across a pause and keeps both. See `_grazes` for
#: the second condition, which keeps a SHORT region the cue mostly covers.
_REGION_EDGE_S = 0.6

#: A fast English speaker (~220 words a minute) is ~20 characters a second.
#: The engine's honest cues on the two live clips ran 10.2-17.5 characters a
#: second; the one it mis-stamped ran 116.1 — 288 characters, 22 s of speech,
#: squeezed into 2.48 s and placed 21.8 s late (2026-09-18). A cue whose words
#: cannot be spoken in its span at this rate, by more than
#: `_STAMP_SHORTFALL_S`, carries wrong times, not fast speech.
_FAST_SPEECH_CHARS_PER_S = 25.0
_STAMP_SHORTFALL_S = 1.0

#: The slowest honest rate the engine's cues ran at on the live clips (10.2
#: characters a second, 2026-09-18). A compressed cue is pulled back no
#: further than its words would take at this rate, ending where it ends. The
#: bound matters when the speech before the cue has no words because it is
#: music or noise the VAD took for speech: unbounded, a 134-character cue at
#: 31 s moved 20 s early onto 19 s of music; bounded, 11.1 s. The live
#: compressed cue (288 characters from 25.29 s, stamped at 47.09 s) still
#: reaches its speech: 28.2 s at this rate is more than the 22 s it spans.
_SLOWEST_HONEST_CHARS_PER_S = 10.2


def _grazes(region: Tuple[float, float], overlap: float, last_start: Optional[float]) -> bool:
    """Does a cue's START only graze this region, the tail of the speech
    before its own, holding none of its words? `_REGION_EDGE_S` or less, AND
    either under half of the region or in a region the cue before it BEGAN
    in (`last_start`).

    START side only. At the end, the same short foot in the next region is
    the cue's own last word after a pause: live clip E (QA r2, 2026-09-18),
    "lovely" at 3.90-4.31 s opened a 10.5 s region, the engine ended the cue
    at 4.12 s, and letting that 0.42 s go ended the cue at 2.81 s with the
    word spoken under no subtitle. On 8 live clips the end-side rule fired
    once, and that once was wrong.

    Half of the region: a short region no earlier cue has words in is this
    cue's own opening word — "So," alone in a 0.65 s region, which the cue
    overlaps by 0.45 s, keeps the cue at 12.0 s instead of 13.25 s (QA r1).
    The cue before began in the region: its words are there, and the engine
    opens a cue where the previous one ended, so what is left of that region
    is the earlier cue's last word and padding — "Yes." at 10.1-10.4 s in a
    0.9 s region, and the answer opened at 10.4 s stayed 2.1 s early, as on
    4810da0 (QA r2). Merely REACHING into the region is not that: a question
    whose cue the engine closed 0.1 s into a 0.6 s reply region holds none of
    the reply, and counting it moved "Yes." 2 s late onto the answer (QA r3).
    The 0.49 s the live 70 s clip's cue reached into a 16.6 s region is a
    graze either way.
    """
    a, b = region
    return overlap <= _REGION_EDGE_S and (
        overlap < 0.5 * (b - a) or (last_start is not None and a <= last_start < b)
    )


def _without_engine_repeats(segments: Sequence[Segment]) -> List[Segment]:
    """The window's cues without empty ones and without the engine's repeats:
    the same text overlapping the cue before it by more than the seam
    tolerance, judged on the ENGINE's times -- `stitch`'s rule as it applied
    before snapping. On snapped times it cannot be judged: two copies of one
    "Thank you." moved out of a pause stop overlapping, and a real "No." the
    VAD missed, moved onto the region where a second real "No." begins, starts
    overlapping it (QA r2 and r3, 2026-09-18)."""
    kept: List[Segment] = []
    for s in segments:
        if not s.text.strip():
            continue
        if kept and s.start_s < kept[-1].end_s - _SEAM_TOLERANCE_S and _norm(s.text) == _norm(kept[-1].text):
            continue
        kept.append(s)
    return kept


def _merged(regions: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Sorted and non-overlapping, which the bisects below rely on. The
    detector's regions already are (`regions_from_flags` merges touching
    ones the same way); a hand-built window may not be."""
    out: List[Tuple[float, float]] = []
    for a, b in sorted(regions):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _speech_from(t: float, regs: Sequence[Tuple[float, float]], starts: Sequence[float]) -> Optional[float]:
    """The first instant at or after `t` with more than an edge of speech."""
    for k in range(max(0, bisect_right(starts, t) - 1), len(regs)):
        a, b = regs[k]
        if b - max(a, t) > _REGION_EDGE_S:
            return max(a, t)
    return None


def snap_to_regions(
    segments: Sequence[Segment], regions: Sequence[Tuple[float, float]]
) -> List[Segment]:
    """One window's cues (video time, the engine's order) held to the speech
    the VAD found in that window.

    * A cue's start or end in a pause moves to the edge of the speech it
      belongs to. Everything it has INSIDE its regions is kept, so a real
      utterance is never cut shorter than its own span there; only a graze
      (see `_grazes`) of the previous speech's tail, at the cue's START, is
      let go. A cue whose start grazes that tail and whose end is in the
      pause after it lies in that pause -- but only when the engine left the
      next speech's start without a cue. When the next cue opens at or
      before it, that speech is the next cue's, and the foot is this cue's
      own last words: "Thanks." said at 9.4-9.8 s, timed 9.4-10.8 with the
      next speaker's cue from 10.8, went 2.1 s late onto the next speaker
      (QA r3). Timed the other way, "Right." 4.6-6.5 with the next cue at
      7.6, the words are the 7.0-7.6 s no cue holds, and it moves there.
    * A cue entirely inside a pause moves to the next region's start, with
      its own length, but never past where the next cue starts by more than
      a player's minimum cue: the engine timed that one on speech, and a
      moved cue pushing it later delayed a correctly timed 7.0 s cue to
      8.6 s (QA, 2026-09-18). Several cues in one pause share that room,
      end to end and each at least `_MIN_CUE_S`, so they neither overlap on
      screen nor push the next cue further; when more are waiting than fit,
      the earliest are joined into the first cue, their words in order.
      All moved onto one instant, the second was 0 s long, the SRT writer
      stretched it over the real cue, and a two-phrase decoder loop stacked
      about 79 cues there (QA r3). Left in the pause instead, they would
      start in known silence, which the brief forbids. `stitch` pushes the
      cue after a moved one off it, keeping up to `_MIN_CUE_S` of its own.
    * A cue stamped too short to hold its words (see
      `_FAST_SPEECH_CHARS_PER_S`) starts at the first speech no earlier cue
      covers, bounded by `_SLOWEST_HONEST_CHARS_PER_S`. That is where the
      engine's compressed cue came from on the live clip (the auditor, on
      another clip, measured a cue 9.5 s late). Its length is measured on
      the text with its repetition loops collapsed: 434 copies of "no" are
      not 1,302 characters of speech, and counted raw they pulled a cue
      28 s back. A cue whose span fits its words is NOT pulled back over
      uncovered speech: the engine leaving a region without words is also
      what music or noise the VAD took for speech looks like, and a cue
      moved onto it would be early by the length of the music.
    * The engine's order is the order the words were said. A cue never
      starts before one the engine emitted ahead of it (when the engine had
      the two in order), so two cues that snap to the same instant keep
      their order through `stitch`'s stable sort. Without this, a cue moved
      out of a pause with its own length sorted AFTER the next cue, which
      snapped to the same start with an earlier end: 7 of 3,000 random
      in-order replies put later words first (QA, 2026-09-18).

    The engine's repeats are dropped first, on its own times (see
    `_without_engine_repeats`); what overlap is left, from cues the engine
    itself overlapped, is `stitch`'s to resolve.

    Bisects rather than scans: this runs on the event loop, and a decoder
    loop of 5 cues a second over a 240 s window is 1,200 cues.

    No regions (a window nobody ran the detector for): nothing moves.
    """
    if not regions or not segments:
        return list(segments)
    regs = _merged(regions)
    starts = [a for a, _ in regs]
    segments = _without_engine_repeats(segments)
    out: List[Segment] = []
    covered = regs[0][0]  # speech before this instant already has a cue
    run: List[Tuple[Segment, float, float, bool]] = []  # cues lying in the pause before regs[run_to]
    run_to = -1

    def emit(seg: Segment, start: float, end: float, in_order: bool) -> None:
        nonlocal covered
        if out and in_order:
            start = max(start, out[-1].start_s)
        end = max(end, start)
        out.append(Segment(start, end, seg.text, seg.language))
        covered = max(covered, end)

    def flush(nxt: float) -> None:
        # The room is the next region's speech before the next cue's start,
        # never less than one minimum cue: `fit` cues of `_MIN_CUE_S` or
        # more. When more are waiting, the earliest share the first slot as
        # ONE cue, their words in order: n separate cues on screen in the
        # room of one either overlap there or push the real cue n x 0.4 s.
        a, b = regs[run_to]
        room = min(b, max(a, nxt)) - a
        fit = max(1, min(len(run), int(room / _MIN_CUE_S + 1e-9)))
        shared = len(run) - fit + 1
        groups = [run[:shared]] + [[m] for m in run[shared:]]
        cursor, last = a, a + max(room, _MIN_CUE_S)
        for n, group in enumerate(groups):
            head = group[0][0]
            seg = head if len(group) == 1 else Segment(
                head.start_s, group[-1][0].end_s, " ".join(m[0].text for m in group), head.language
            )
            extent = group[-1][2] - group[0][1]
            span = max(_MIN_CUE_S, min(extent, last - cursor - (fit - 1 - n) * _MIN_CUE_S))
            emit(seg, cursor, min(b, cursor + span), group[0][3])
            cursor = out[-1].end_s
        run.clear()

    for k, seg in enumerate(segments):
        start, end = seg.start_s, max(seg.end_s, seg.start_s)
        chars = len(loops.clean_text(seg.text))
        if chars / _FAST_SPEECH_CHARS_PER_S - (end - start) > _STAMP_SHORTFALL_S:
            first_speech = _speech_from(covered, regs, starts)
            if first_speech is not None:
                target = max(first_speech, end - chars / _SLOWEST_HONEST_CHARS_PER_S)
                if target < start:
                    start = target
        i = max(0, bisect_right(starts, start) - 1)
        touched = []  # (index into regs, overlap)
        for n in range(i, len(regs)):
            if regs[n][0] >= end:
                break
            overlap = min(end, regs[n][1]) - max(start, regs[n][0])
            if overlap > 0:
                touched.append((n, overlap))
        # A foot in the tail of the speech before the cue's own is let go when
        # the cue runs on past it into later speech, or ends in the pause
        # before speech the engine left without a cue: then the cue lies in
        # that pause, and moves out of it below.
        last_start = regs[run_to][0] if run else (out[-1].start_s if out else None)
        nxt = segments[k + 1].start_s if k + 1 < len(segments) else float("inf")
        after = None  # the cue's words begin after regs[after]
        while (
            touched
            and touched[0][0] + 1 < len(regs)
            and regs[touched[0][0]][1] < end
            and _grazes(regs[touched[0][0]], touched[0][1], last_start)
            and (
                len(touched) > 1
                # Ends in the pause: only when earlier cues cover the tail up to
                # this cue. Speech before it that no cue holds is where its words
                # are: the live engine stamped late cues at the END of their speech
                # (G8455 48.16 s words at 50.36 s; H3570 47.54 s at 50.32 s), and
                # moving them on put them 4.3 s and 4.9 s late (reviewer, 2026-09-19).
                or (nxt > regs[touched[0][0] + 1][0] and covered >= start - _SEAM_TOLERANCE_S)
            )
        ):
            after = touched.pop(0)[0]
        to = None  # the region this cue moves to the start of
        if touched:
            start, end = max(start, regs[touched[0][0]][0]), min(end, regs[touched[-1][0]][1])
        else:
            if after is not None:
                j = after + 1
            else:
                j = bisect_right(starts, start)
                # Half-open, like the overlaps above: a cue opening exactly
                # at a region's end has none of it and lies in the pause,
                # as one at 4.99 or 5.01 s does (QA r3).
                if j > 0 and regs[j - 1][0] <= start < regs[j - 1][1]:
                    j = len(regs)  # inside speech already: nowhere to move
            if j < len(regs):
                to = j
        in_order = k == 0 or seg.start_s >= segments[k - 1].start_s
        if run and to != run_to:
            flush(seg.start_s)
        if to is not None:
            run.append((seg, start, end, in_order))
            run_to = to
            continue
        emit(seg, start, end, in_order)
    if run:
        flush(regs[run_to][1])
    return out


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
            # The engine's times are in-clip and not anchored to the speech;
            # the window's own VAD regions are (see `snap_to_regions`).
            results[i] = snap_to_regions(
                [
                    Segment(
                        start_s=window.start_s + float(s.get("start", 0.0)),
                        end_s=window.start_s + float(s.get("end", 0.0)),
                        text=str(s.get("text") or "").strip(),
                        language=(str(s.get("language")) if s.get("language") else result.language_code),
                    )
                    for s in (result.segments or [])
                ],
                window.regions,
            )
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
