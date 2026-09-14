"""Audio of any length for `POST /v1/audio/transcriptions` (2026-09-13).

WHY THIS EXISTS. Until this wave a public transcription was one clip, read
into memory, capped at 300 s, sent whole to one whisper replica under a 240 s
total timeout. The owner removed every usage limit and every server timeout
from `/v1` (no-timeout design, 2026-09-13), and whisper itself refuses clips
over 600 s and decodes ONE clip per GPU — so "any length" cannot mean "a longer
clip". It means the pipeline the video analyser already runs, made safe for
strangers' traffic:

  1. INGEST. The body streams to a 0600 file under the disk ledger
     (`disk_ledger.DiskSink`), sha256 computed on the way. A Files API file is
     used in place (`AudioSource.from_path(owned=False)`) and never deleted.
  2. JOB KEY. sha256 of (project, audio sha256, language, format class,
     chunking parameters). Same key while running = the same job (a retry or
     a gateway re-attach follows it instead of starting a second one).
  3. DECODE, JUST IN TIME. Only when the job is at most one place behind the
     head of the public `asr` queue, at most PUBLIC_API_DECODE_CONCURRENCY (2)
     at a time, as `nice -n 19 ionice -c 3 ffmpeg … -progress pipe:1`, killed
     after PUBLIC_API_DECODE_STALL_S (120) without progress. The raw PCM it
     writes (32,000 B/s) is RESERVED in the disk ledger first, from the WAV
     header, ffprobe, or a bytes-over-lowest-bitrate upper bound — and grown
     while decoding if the estimate was low. The source is deleted after
     decode when this job owns it.
  4. WINDOWS. `video.vad.plan_windows` at 90 s, 3 s overlap, 2 s gap — the
     analyser's own planner. A planner that raises, or returns a plan that
     breaks the window rules, falls back to fixed overlapping windows over
     the whole recording: fail CLOSED (every second is sent), never open.
  5. DISPATCH. One window at a time through the fleet-wide public `asr` gate
     (yields to chat for up to 10 s, waits while dictation needs a replica),
     to the replica dictation does not prefer. While a window is out, the
     replica's /health is read every 15 s: `ready: false` or a rising
     `cuda_failures` fails the window over at once. A window silent for
     PUBLIC_API_ASR_WINDOW_SILENCE_S (1800) on a ready replica is sent once
     more, to the other replica; a second silence is `model_unavailable`
     (retry-safe). Engines that refuse connections or answer 5xx are waited
     out with backoff for up to PUBLIC_API_ASR_UNAVAILABLE_GRACE_S (1800) of
     CONTINUOUS unavailability. No window is ever skipped: a gap in a
     transcript is a silent lie, so a window that cannot be transcribed fails
     the job instead.
  6. CACHE. Every finished window is written to a content-addressed cache
     keyed by sha256(project, model, language, sha256 of the exact clip
     bytes), kept PUBLIC_API_ASR_CACHE_TTL_S (24 h). A retry after a failure,
     a deploy or a disconnect re-sends only the windows that never finished.
     The project is in the key so identical audio in another project is never
     answered from this one's cache (no cross-tenant presence signal).
  7. STITCH. Window offsets added; overlap seams de-duplicated WORD BY WORD
     (below); `video.loops.collapse` removes decoder loops; a 2-cue holdback
     (plus every cue the next overlap may still trim) so streamed deltas are
     never retracted.
  8. OUTPUT. Events for a stream (`transcript.text.delta`, `…done`, `: ping`,
     `: queued`), or one result for a committed JSON body. Usage seconds are
     ceil(decoded samples / 16,000).

WHY THE SEAM IS DE-DUPLICATED BY WORDS, NOT SEGMENTS (measured 2026-09-13 on
the decodable synthetic speech of tests/publicapi_fake_whisper.py, 20 minutes,
1,987 words). `video.transcribe.stitch` keeps the earlier clip's segments and
drops the later clip's segments that end before the seam; a segment that
STRADDLES the seam is kept from both clips because the earlier clip was cut
mid-segment. Whisper's segments are sentences of several seconds, so on
continuous speech the straddling segment is the common case, and the overlap's
words came out twice: 32 extra words over the 5 overlapping seams of the VAD
plan, 60 over the 13 seams of the fixed plan. The alignment here gave 0 extra
and 0 missing on both, segment starts within 0.19 s of the true word times
(and the same on 2 hours: 11,999 words). The earlier clip's tail words
that fall inside the overlap are aligned with the later clip's head: the
longest run of identical normalised words, whose positions agree in time,
marks one copy; the earlier clip's words after the run (its cut-off word) and
the later clip's words before it (its cut-in word) are dropped. With no such
run (a pause fills the overlap, or a script without spaces) the overlap is
split at its midpoint on estimated word times instead.

MEMORY IS BOUNDED BY THE WINDOW, NOT THE RECORDING. The PCM is a memory map
(page cache the kernel reclaims, not process memory); one clip (≤ 90 s,
2.9 MB, twice while its WAV is built) is in memory at a time; the planner
reads the map in 60 s blocks; the transcript itself (text) is the only thing
that grows with duration. The 2-hour acceptance test measures RssAnon.

WHAT THIS MODULE DOES NOT DO: parse multipart, authenticate, count usage, or
write HTTP. `endpoints.create_transcription` (integration) does those and
calls `jobs().start(...)`.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import functools
import hashlib
import inspect
import json
import logging
import math
import os
import re
import secrets
import shutil
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

import httpx

from ..config import _float, _int, settings
from ..video import loops
from ..video.transcribe import wav_bytes
from ..video.types import Segment
from ..video.vad import SAMPLE_RATE, Window, plan_windows
from . import capacity, errors
from .disk_ledger import (
    CapExceeded,
    DiskFull,
    DiskLedger,
    DiskSink,
    Reservation,
    ensure_private_dir,
    ledger_for,
)

log = logging.getLogger(__name__)

#: 16 kHz mono 16-bit PCM: the decoded bytes per second of audio.
PCM_BYTES_PER_SECOND = SAMPLE_RATE * 2

#: The window cache's own version. Bump when what a cached window MEANS
#: changes (engine request shape, segment post-processing before caching).
WINDOW_CACHE_VERSION = 1

#: Cues held back after every window so a loop run or a seam trim that the
#: next window causes never retracts text already streamed (design: "a 2-cue
#: holdback").
HOLDBACK_CUES = 2

#: How far before an overlap a cue may still be trimmed by the alignment.
#: Whisper's segment timestamps drift by up to about a second at a clip edge
#: (the note in video/transcribe.py that motivated its textual seam check);
#: one second of slack on each side covers it.
SEAM_SLACK_S = 1.0

#: Two words of the same text are the same spoken word only if their
#: estimated times agree this closely. A run that matches by text alone at
#: different times is a repeated phrase, not the overlap.
SEAM_TIME_AGREEMENT_S = 2.0

#: The SSE heartbeat of the byte invariant (CONTRACT §10): a byte every 15 s.
HEARTBEAT_S = 15.0

#: Progress events are coalesced to at most one per second.
PROGRESS_INTERVAL_S = 1.0

#: The job heartbeat file is touched this often; the start-up sweep removes a
#: job directory whose heartbeat is older than STALE_JOB_DIR_S (a crashed
#: process), and never a live peer's during a blue/green overlap.
JOB_HEARTBEAT_S = 60.0
STALE_JOB_DIR_S = 600.0

#: The containers the decoder accepts, when video/media.py does not yet
#: publish its own list (Files design §6.4, Team B). Kept identical to it.
_FALLBACK_FORMATS = (
    "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mpeg,mpegts,mpegps,ogg,wav,mp3,flac,aac,asf,flv,"
    "m4v,caf,aiff,amr"
)

#: Scheduling class for our own decoders: lowest CPU priority, idle I/O class.
NICE_PREFIX: Tuple[str, ...] = ("nice", "-n", "19", "ionice", "-c", "3")


# ---------------------------------------------------------- the worker --

#: ONE thread does every per-window allocation of every job (the clip copy,
#: its hash, the cache read and write, the plan, stored results). WHY
#: (measured 2026-09-13 with the 2-hour test in
#: tests/test_publicapi_audio_windows.py, four runs each): with `asyncio.to_thread` the
#: default executor spread those 2.9 MB allocations over up to 24 threads, and
#: glibc keeps a malloc arena per thread holding freed chunks, so the job's
#: peak anonymous memory was 23-70 MiB depending only on how many threads
#: happened to be used; with a single arena (MALLOC_ARENA_MAX=1) it was
#: 1.2 MiB for 10 minutes and 8.6-11.4 MiB for 2 hours, every run. With this
#: one dedicated thread and default arenas: 27.0 MiB for 10 minutes (the
#: thread's first allocations included) and 28.1-30.5 MiB for 2 hours, five
#: runs — stable, and nearly flat in duration, without a process-wide
#: allocator setting. Clip building is milliseconds per window and windows are
#: dispatched one at a time fleet-wide, so nothing waits on it.
_worker_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None


def _worker() -> concurrent.futures.ThreadPoolExecutor:
    global _worker_pool
    if _worker_pool is None:
        _worker_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr-windows")
    return _worker_pool


async def on_worker(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await asyncio.get_running_loop().run_in_executor(_worker(), functools.partial(fn, *args, **kwargs))


def _clip_and_hash(pcm: Any, a: int, b: int) -> Tuple[bytes, str]:
    clip = wav_bytes(pcm[a:b])
    return clip, hashlib.sha256(clip).hexdigest()


# ------------------------------------------------------------- settings --


def _setting_int(name: str, default: int) -> int:
    value = getattr(settings, name.lower(), None)
    if value is not None:
        return int(value)
    return int(_int(name, int(default)))


def _setting_float(name: str, default: float) -> float:
    value = getattr(settings, name.lower(), None)
    if value is not None:
        return float(value)
    return float(_float(name, float(default)))


def _setting_str(name: str, default: str) -> str:
    value = getattr(settings, name.lower(), None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def cache_dir() -> str:
    """PUBLIC_API_ASR_CACHE_DIR (/data/publicapi/asr): ingest files, decoded
    PCM while a job runs, the window cache and finished results."""
    return _setting_str("PUBLIC_API_ASR_CACHE_DIR", "/data/publicapi/asr")


def window_s() -> float:
    """PUBLIC_API_ASR_WINDOW_S (90): the longest clip sent to whisper."""
    return max(5.0, _setting_float("PUBLIC_API_ASR_WINDOW_S", 90.0))


def overlap_s() -> float:
    """PUBLIC_API_ASR_OVERLAP_S (3): overlap where continuous speech is split."""
    return max(0.0, _setting_float("PUBLIC_API_ASR_OVERLAP_S", 3.0))


def max_gap_s() -> float:
    """PUBLIC_API_ASR_MAX_GAP_S (2): the longest pause kept inside one window."""
    return max(0.0, _setting_float("PUBLIC_API_ASR_MAX_GAP_S", 2.0))


def decode_concurrency() -> int:
    """PUBLIC_API_DECODE_CONCURRENCY (2)."""
    return max(1, _setting_int("PUBLIC_API_DECODE_CONCURRENCY", 2))


def decode_stall_s() -> float:
    """PUBLIC_API_DECODE_STALL_S (120): ffmpeg killed after this long without progress."""
    return max(0.1, _setting_float("PUBLIC_API_DECODE_STALL_S", 120.0))


def window_silence_s() -> float:
    """PUBLIC_API_ASR_WINDOW_SILENCE_S (1800): a ready replica silent this long on one window."""
    return max(0.1, _setting_float("PUBLIC_API_ASR_WINDOW_SILENCE_S", 1800.0))


def health_poll_s() -> float:
    """PUBLIC_API_ASR_HEALTH_POLL_S (15): replica /health cadence while a window is out."""
    return max(0.05, _setting_float("PUBLIC_API_ASR_HEALTH_POLL_S", 15.0))


def unavailable_grace_s() -> float:
    """PUBLIC_API_ASR_UNAVAILABLE_GRACE_S (1800): continuous engine unavailability tolerated."""
    return max(0.0, _setting_float("PUBLIC_API_ASR_UNAVAILABLE_GRACE_S", 1800.0))


def cache_ttl_s() -> float:
    """PUBLIC_API_ASR_CACHE_TTL_S (86400): finished windows and results kept."""
    return max(0.0, _setting_float("PUBLIC_API_ASR_CACHE_TTL_S", 86400.0))


def orphan_grace_s() -> float:
    """PUBLIC_API_ASR_ORPHAN_GRACE_S (120): a job with no follower is cancelled after this.
    The unkeyed orphan grace of the no-timeout design; finished windows stay cached."""
    return max(0.0, _setting_float("PUBLIC_API_ASR_ORPHAN_GRACE_S", 120.0))


def min_bitrate_bps() -> int:
    """PUBLIC_API_ASR_MIN_BITRATE_BPS (6000): the lowest audio bitrate assumed
    when a duration cannot be read (Opus's 6 kbit/s floor), so bytes ÷ bitrate
    is an UPPER bound on duration for the disk reservation."""
    return max(1000, _setting_int("PUBLIC_API_ASR_MIN_BITRATE_BPS", 6000))


def gate_shim_wait_s() -> float:
    """PUBLIC_API_ASR_GATE_SHIM_WAIT_S (5): only while `capacity.hold` still
    requires a finite wait — each refusal is re-queued, never surfaced."""
    return max(0.01, _setting_float("PUBLIC_API_ASR_GATE_SHIM_WAIT_S", 5.0))


# ------------------------------------------------------------ job spec --

#: What the requested format needs from the engine. Text formats and the
#: segment format are cached and keyed apart because they are different
#: RESULTS for the same audio, even though the windows under them are shared.
FORMAT_CLASSES = {"json": "text", "text": "text", "verbose_json": "segments"}


@dataclass(frozen=True)
class JobSpec:
    project_id: str
    sha256: str
    language: Optional[str]
    response_format: str
    window_s: float
    overlap_s: float
    max_gap_s: float

    @classmethod
    def for_request(
        cls, *, project_id: str, sha256: str, language: Optional[str], response_format: str
    ) -> "JobSpec":
        if response_format not in FORMAT_CLASSES:
            raise ValueError(f"unknown response_format {response_format!r}")
        return cls(
            project_id=str(project_id),
            sha256=str(sha256),
            language=(language or None),
            response_format=response_format,
            window_s=window_s(),
            overlap_s=overlap_s(),
            max_gap_s=max_gap_s(),
        )

    @property
    def format_class(self) -> str:
        return FORMAT_CLASSES[self.response_format]

    def key(self) -> str:
        material = json.dumps(
            [
                "asr-job",
                1,
                self.project_id,
                self.sha256,
                self.language or "auto",
                self.format_class,
                round(self.window_s, 3),
                round(self.overlap_s, 3),
                round(self.max_gap_s, 3),
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class AudioSource:
    """Bytes to transcribe, on disk. `owned` sources are deleted after decode."""

    path: str
    bytes: int
    sha256: str
    owned: bool = False

    @classmethod
    async def from_path(cls, path: str, *, sha256: Optional[str] = None, owned: bool = False) -> "AudioSource":
        def measure() -> Tuple[int, str]:
            size = os.path.getsize(path)
            if sha256:
                return size, sha256
            digest = hashlib.sha256()
            with open(path, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(block)
            return size, digest.hexdigest()

        size, digest = await asyncio.to_thread(measure)
        return cls(path=path, bytes=size, sha256=digest, owned=owned)


async def ingest(
    chunks: AsyncIterable[bytes],
    *,
    cap_bytes: int,
    root: Optional[str] = None,
    ledger: Optional[DiskLedger] = None,
) -> AudioSource:
    """A request's file part → an owned `AudioSource`, streamed under the ledger.

    Raises `DiskFull` before the first byte when the cap cannot be reserved,
    and `CapExceeded` the moment the body passes the cap (the temporary file
    is already gone when either leaves this function).
    """
    base = root or cache_dir()
    incoming = os.path.join(base, "incoming")
    ledger = ledger or ledger_for(base)
    async with DiskSink(incoming, cap_bytes=cap_bytes, ledger=ledger, purpose="asr-ingest") as sink:
        async for chunk in chunks:
            await sink.write(chunk)
        final = os.path.join(incoming, f"src.{secrets.token_hex(12)}")
        stored = await sink.commit(final)
    return AudioSource(path=stored.path, bytes=stored.bytes, sha256=stored.sha256, owned=True)


# --------------------------------------------------------------- windows --


def fixed_windows(total_s: float, window: float, overlap: float) -> List[Window]:
    """Every second of the recording in windows of `window`, overlapping by
    `overlap` — the fail-closed plan."""
    if total_s <= 0:
        return []
    step = max(1.0, window - overlap)
    out: List[Window] = []
    start = 0.0
    first = True
    while True:
        end = min(total_s, start + window)
        out.append(Window(start, end, overlaps_previous=not first))
        first = False
        if end >= total_s:
            break
        start = start + step
    return out


def _plan_is_sound(windows: Sequence[Window], *, total_s: float, window: float) -> bool:
    previous_end = -1.0
    previous_start = -1.0
    for w in windows:
        if w.start_s < 0 or w.end_s > total_s + 0.5 or w.end_s <= w.start_s:
            return False
        if w.duration_s > window + 1e-6:
            return False
        if w.start_s < previous_start:
            return False
        if w.overlaps_previous and not (w.start_s < previous_end):
            return False
        if not w.overlaps_previous and w.start_s < previous_end - 1e-6:
            return False  # an overlap nobody would de-duplicate
        previous_start, previous_end = w.start_s, w.end_s
    return True


def plan(
    pcm: Any,
    *,
    total_s: float,
    window: float,
    overlap: float,
    gap: float,
    planner: Callable[..., Tuple[List[Window], dict]] = plan_windows,
) -> Tuple[List[Window], dict]:
    """VAD windows, or fixed windows when the detector fails (fail closed)."""
    try:
        windows, report = planner(
            pcm, total_s=total_s, max_window_s=window, max_gap_s=gap, overlap_s=overlap
        )
    except Exception as exc:  # noqa: BLE001 - a detector bug must not lose words
        log.warning("voice activity planning failed (%s); using fixed windows", type(exc).__name__)
        windows = fixed_windows(total_s, window, overlap)
        return windows, {"detector": "fixed", "reason": "detector_failed", "windows": len(windows)}
    if not _plan_is_sound(windows, total_s=total_s, window=window):
        log.warning("voice activity plan broke the window rules; using fixed windows")
        windows = fixed_windows(total_s, window, overlap)
        return windows, {"detector": "fixed", "reason": "plan_invalid", "windows": len(windows)}
    return list(windows), dict(report)


# ----------------------------------------------------------- window cache --


class WindowCache:
    """Finished windows on disk, content-addressed. 0700 dirs, 0600 files."""

    def __init__(self, root: str) -> None:
        self.root = os.path.join(root, "windows")

    @staticmethod
    def key(*, project_id: str, clip_sha256: str, language: Optional[str], model: str) -> str:
        material = "\0".join(
            ["asr-window", str(WINDOW_CACHE_VERSION), project_id, model or "", language or "auto", clip_sha256]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> str:
        return os.path.join(self.root, key[:2], f"{key}.json")

    def get(self, key: str) -> Optional[dict]:
        path = self._path(key)
        try:
            with open(path, "rb") as fh:
                data = json.loads(fh.read().decode("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            with contextlib.suppress(OSError):
                os.unlink(path)
            return None
        return data if isinstance(data, dict) and isinstance(data.get("segments"), list) else None

    def put(self, key: str, reply: Mapping[str, Any]) -> None:
        path = self._path(key)
        ensure_private_dir(os.path.dirname(path))
        tmp = f"{path}.{secrets.token_hex(6)}.tmp"
        payload = json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        _write_private(tmp, payload)
        os.replace(tmp, path)

    def sweep(self, ttl_s: float, *, now: Optional[float] = None) -> int:
        return _sweep_files(self.root, ttl_s, now=now)


def _write_private(path: str, payload: bytes) -> None:
    """A new 0600 file holding `payload`, fsynced (a short write is retried)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _sweep_files(root: str, ttl_s: float, *, now: Optional[float] = None) -> int:
    cutoff = (time.time() if now is None else now) - ttl_s
    removed = 0
    if not os.path.isdir(root):
        return 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                if os.stat(path).st_mtime < cutoff:
                    os.unlink(path)
                    removed += 1
            except FileNotFoundError:
                continue
    return removed


def _slim_reply(reply: Mapping[str, Any]) -> dict:
    """What a cached window keeps: segments, language, duration. Nothing the
    engine adds for its own diagnostics (processing_ms, no_speech_prob)."""
    segments = []
    raw = reply.get("segments")
    for segment in raw if isinstance(raw, list) else []:
        if not isinstance(segment, Mapping):
            continue
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(segment.get("start") or 0.0)
            end = float(segment.get("end") if segment.get("end") is not None else start)
        except (TypeError, ValueError):
            continue
        segments.append(
            {
                "start": start,
                "end": max(start, end),
                "text": text,
                "language": (str(segment["language"]) if segment.get("language") else None),
            }
        )
    language = reply.get("language_code") or reply.get("language")
    duration = reply.get("duration")
    return {
        "segments": segments,
        "language": str(language) if language else None,
        "duration": float(duration) if isinstance(duration, (int, float)) else None,
    }


# --------------------------------------------------------------- stitch --


def _norm_word(word: str) -> str:
    return "".join(ch for ch in word.lower() if ch.isalnum())


@dataclass
class _Cue:
    start_s: float
    end_s: float
    words: List[str]
    language: Optional[str] = None

    @property
    def text(self) -> str:
        return " ".join(self.words)

    def word_span(self, index: int) -> Tuple[float, float]:
        """Estimated [start, end] of word `index`, by character position: whisper
        reports segment times only, and speech rate within one segment is the
        best available model of where a word falls."""
        total = sum(len(w) for w in self.words) + max(0, len(self.words) - 1)
        if total <= 0:
            return self.start_s, self.end_s
        offset = sum(len(w) + 1 for w in self.words[:index])
        span = self.end_s - self.start_s
        a = self.start_s + span * offset / total
        b = self.start_s + span * (offset + len(self.words[index])) / total
        return a, b

    def word_mid(self, index: int) -> float:
        a, b = self.word_span(index)
        return (a + b) / 2.0


def _cues_from_segments(window: Window, segments: Sequence[Mapping[str, Any]]) -> List[_Cue]:
    cues = []
    for segment in segments:
        words = str(segment.get("text") or "").split()
        if not words:
            continue
        start = window.start_s + max(0.0, float(segment.get("start") or 0.0))
        end = window.start_s + max(0.0, float(segment.get("end") or 0.0))
        start = min(max(start, window.start_s), window.end_s)
        end = min(max(end, start), window.end_s)
        lang = segment.get("language")
        cues.append(_Cue(start, end, words, str(lang) if lang else None))
    cues.sort(key=lambda c: (c.start_s, c.end_s))
    return cues


def _longest_aligned_run(
    prev: Sequence[Tuple[int, int, str, float]],
    new: Sequence[Tuple[int, int, str, float]],
) -> Optional[Tuple[int, int, int]]:
    """(end index in prev, end index in new, length) of the longest run of
    equal normalised words whose times agree; of equal runs, the one latest in
    `prev` (closest to the seam) wins."""
    best: Optional[Tuple[int, int, int]] = None
    # lengths[j] = run length ending at prev[i-1], new[j-1] (rolling row)
    previous_row = [0] * (len(new) + 1)
    for i in range(1, len(prev) + 1):
        row = [0] * (len(new) + 1)
        _, _, pword, ptime = prev[i - 1]
        for j in range(1, len(new) + 1):
            _, _, nword, ntime = new[j - 1]
            if pword and pword == nword and abs(ptime - ntime) <= SEAM_TIME_AGREEMENT_S:
                row[j] = previous_row[j - 1] + 1
                length = row[j]
                if best is None or length > best[2] or (length == best[2] and i - 1 >= best[0]):
                    best = (i - 1, j - 1, length)
        previous_row = row
    return best


class Stitcher:
    """Per-window segments → one timeline, emitted incrementally and never retracted."""

    def __init__(self, windows: Sequence[Window]) -> None:
        self.windows = list(windows)
        self._held: List[_Cue] = []
        self._last_end: float = 0.0
        self.report: Dict[str, int] = {
            "seams_aligned": 0,
            "seams_split_by_time": 0,
            "seam_words_dropped": 0,
            "loop_chars_removed": 0,
            "loop_cue_runs_merged": 0,
        }

    # -- the seam --

    def _join(self, index: int, new: List[_Cue]) -> List[_Cue]:
        window = self.windows[index]
        previous = self.windows[index - 1]
        seam_start, seam_end = window.start_s, previous.end_s
        if seam_end <= seam_start or not new:
            return new
        prev_words = [
            (ci, wi, _norm_word(cue.words[wi]), cue.word_mid(wi))
            for ci, cue in enumerate(self._held)
            for wi in range(len(cue.words))
            if cue.word_mid(wi) >= seam_start - SEAM_SLACK_S
        ]
        new_words = [
            (ci, wi, _norm_word(cue.words[wi]), cue.word_mid(wi))
            for ci, cue in enumerate(new)
            for wi in range(len(cue.words))
            if cue.word_mid(wi) <= seam_end + SEAM_SLACK_S
        ]
        run = _longest_aligned_run(prev_words, new_words)
        accept = False
        if run is not None:
            p_end, n_end, length = run
            if length >= 2:
                accept = True
            elif length == 1 and len(prev_words[p_end][2]) >= 3:
                accept = abs(prev_words[p_end][3] - new_words[n_end][3]) <= SEAM_SLACK_S
        if accept and run is not None:
            p_end, n_end, _length = run
            pci, pwi = prev_words[p_end][0], prev_words[p_end][1]
            dropped_prev = sum(len(c.words) for c in self._held[pci + 1 :]) + (len(self._held[pci].words) - pwi - 1)
            self._truncate_held_after(pci, pwi)
            if n_end + 1 < len(new_words):
                nci, nwi = new_words[n_end + 1][0], new_words[n_end + 1][1]
                dropped_new = sum(len(c.words) for c in new[:nci]) + nwi
                new = self._trim_new_before(new, nci, nwi)
            else:
                # Every head word matched: the new clip's words up to and
                # including the run end go; anything after the head range stays.
                nci, nwi = new_words[n_end][0], new_words[n_end][1]
                dropped_new = sum(len(c.words) for c in new[:nci]) + nwi + 1
                new = self._trim_new_before(new, nci, nwi + 1)
            self.report["seams_aligned"] += 1
            self.report["seam_words_dropped"] += dropped_prev + dropped_new
            return new
        # No aligned run: split the overlap at its midpoint on estimated times,
        # and only on a side the other side actually covers (never lose words).
        middle = (seam_start + seam_end) / 2.0
        prev_covers = any(t >= seam_start for _, _, _, t in prev_words)
        new_covers = any(t <= seam_end for _, _, _, t in new_words)
        dropped = 0
        if new_covers:
            kept: List[_Cue] = []
            for cue in self._held:
                keep = [w for i, w in enumerate(cue.words) if cue.word_mid(i) < middle or cue.end_s <= seam_start]
                if len(keep) == len(cue.words):
                    kept.append(cue)
                    continue
                dropped += len(cue.words) - len(keep)
                if keep:
                    end = cue.word_span(len(keep) - 1)[1]
                    kept.append(_Cue(cue.start_s, end, keep, cue.language))
            self._held = kept
        if prev_covers:
            trimmed: List[_Cue] = []
            for cue in new:
                keep_idx = [i for i in range(len(cue.words)) if cue.word_mid(i) >= middle]
                if len(keep_idx) == len(cue.words):
                    trimmed.append(cue)
                    continue
                dropped += len(cue.words) - len(keep_idx)
                if keep_idx:
                    start = cue.word_span(keep_idx[0])[0]
                    trimmed.append(_Cue(start, cue.end_s, [cue.words[i] for i in keep_idx], cue.language))
            new = trimmed
        self.report["seams_split_by_time"] += 1
        self.report["seam_words_dropped"] += dropped
        return new

    def _truncate_held_after(self, cue_index: int, word_index: int) -> None:
        cue = self._held[cue_index]
        end = cue.word_span(word_index)[1]
        self._held = self._held[:cue_index] + [
            _Cue(cue.start_s, max(cue.start_s, end), cue.words[: word_index + 1], cue.language)
        ]

    @staticmethod
    def _trim_new_before(new: List[_Cue], cue_index: int, word_index: int) -> List[_Cue]:
        out: List[_Cue] = []
        for ci, cue in enumerate(new):
            if ci < cue_index:
                continue
            if ci == cue_index and word_index > 0:
                if word_index >= len(cue.words):
                    continue
                start = cue.word_span(word_index)[0]
                out.append(_Cue(start, cue.end_s, cue.words[word_index:], cue.language))
                continue
            out.append(cue)
        return out

    # -- loops and emission --

    def _collapse_held(self) -> None:
        if not self._held:
            return
        segments = [Segment(c.start_s, c.end_s, c.text, c.language) for c in self._held]
        collapsed, report = loops.collapse(segments)
        self.report["loop_chars_removed"] += int(report.get("chars_removed", 0))
        self.report["loop_cue_runs_merged"] += int(report.get("cue_runs_merged", 0))
        self._held = [_Cue(s.start_s, s.end_s, s.text.split(), s.language) for s in collapsed if s.text.strip()]

    def _emit(self, cues: Sequence[_Cue]) -> List[Segment]:
        out = []
        for cue in cues:
            start = max(cue.start_s, self._last_end)
            end = max(cue.end_s, start)
            out.append(Segment(round(start, 3), round(end, 3), cue.text, cue.language))
            self._last_end = end
        return out

    def feed(self, index: int, segments: Sequence[Mapping[str, Any]]) -> List[Segment]:
        """Window `index`'s engine segments (clip-relative) → cues now final."""
        window = self.windows[index]
        new = _cues_from_segments(window, segments)
        if window.overlaps_previous and index > 0:
            new = self._join(index, new)
        self._held.extend(new)
        self._collapse_held()
        boundary = len(self._held) - HOLDBACK_CUES
        following = self.windows[index + 1] if index + 1 < len(self.windows) else None
        if following is not None and following.overlaps_previous:
            reach = following.start_s - SEAM_SLACK_S
            while boundary > 0 and self._held[boundary - 1].end_s > reach:
                boundary -= 1
        # Never split a run of identical cues: `loops.collapse` must see all of it.
        while 0 < boundary < len(self._held) and _norm_cue(self._held[boundary - 1]) == _norm_cue(self._held[boundary]):
            boundary -= 1
        boundary = max(0, boundary)
        ready, self._held = self._held[:boundary], self._held[boundary:]
        return self._emit(ready)

    def finish(self) -> List[Segment]:
        ready, self._held = self._held, []
        return self._emit(ready)


def _norm_cue(cue: _Cue) -> str:
    return " ".join(w.lower() for w in cue.words)


# -------------------------------------------------------------- decoding --


class DecodeFailed(Exception):
    """ffmpeg could not turn the input into audio (a caller's file problem)."""


class DecodeStalled(Exception):
    """ffmpeg made no progress for the stall window and was killed."""


def safe_format_list() -> str:
    """The container allowlist: video/media.py's when it publishes one."""
    media = sys.modules.get(f"{__name__.rsplit('.', 2)[0]}.video.media")
    args = tuple(getattr(media, "SAFE_INPUT_ARGS", ()) or ())
    if "-format_whitelist" in args:
        position = args.index("-format_whitelist")
        if position + 1 < len(args):
            return str(args[position + 1])
    return _FALLBACK_FORMATS


def input_args(*, pipe: bool) -> Tuple[str, ...]:
    """Input options for every ffmpeg/ffprobe this module starts. Reading from
    stdin, only the pipe protocol is allowed at all."""
    return (
        "-protocol_whitelist",
        "pipe" if pipe else "file",
        "-format_whitelist",
        safe_format_list(),
    )


@dataclass(frozen=True)
class Decoded:
    path: str
    samples: int
    decode_ms: int
    input_mode: str

    @property
    def seconds(self) -> float:
        return self.samples / float(SAMPLE_RATE)


def wav_header_seconds(path: str) -> Optional[float]:
    """Duration of a RIFF/WAVE PCM file from its header, or None."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
            if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return None
            byte_rate = None
            for _ in range(64):
                chunk = fh.read(8)
                if len(chunk) < 8:
                    return None
                tag, size = chunk[:4], struct.unpack("<I", chunk[4:8])[0]
                if tag == b"fmt ":
                    fmt = fh.read(size)
                    if len(fmt) < 16:
                        return None
                    fmt_tag = struct.unpack("<H", fmt[:2])[0]
                    if fmt_tag not in (1, 3, 0xFFFE):
                        return None
                    byte_rate = struct.unpack("<I", fmt[8:12])[0]
                    if size % 2:
                        fh.read(1)
                elif tag == b"data":
                    if not byte_rate:
                        return None
                    available = os.path.getsize(path) - fh.tell()
                    data = available if size in (0, 0xFFFFFFFF) else min(size, available)
                    return data / float(byte_rate)
                else:
                    fh.seek(size + (size % 2), 1)
    except OSError:
        return None
    return None


class Decoder:
    """`nice -n 19 ionice -c 3 ffmpeg` → raw 16 kHz mono s16le, with stall detection."""

    def __init__(self, *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe", nice_prefix: Sequence[str] = NICE_PREFIX) -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.nice_prefix = tuple(nice_prefix)

    def argv(self, source_path: str, out_path: str, *, pipe: bool) -> List[str]:
        return [
            *self.nice_prefix,
            self.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            *input_args(pipe=pipe),
            "-i",
            "pipe:0" if pipe else source_path,
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            "-progress",
            "pipe:1",
            "-nostats",
            "-y",
            out_path,
        ]

    def probe_argv(self, source_path: str) -> List[str]:
        return [
            *self.nice_prefix,
            self.ffprobe,
            "-v",
            "error",
            *input_args(pipe=False),
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            "-i",
            source_path,
        ]

    async def probe_seconds(self, source_path: str, *, timeout_s: float = 30.0) -> Optional[float]:
        if shutil.which(self.ffprobe) is None:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.probe_argv(source_path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return None
        try:
            value = float((out or b"").decode("ascii", "replace").strip().splitlines()[0])
        except (ValueError, IndexError):
            return None
        return value if math.isfinite(value) and value > 0 else None

    async def decode(
        self,
        source: AudioSource,
        out_path: str,
        *,
        reservation: Reservation,
        on_progress: Callable[[float], Awaitable[None]],
        stall_s: Optional[float] = None,
    ) -> Decoded:
        """Decode through stdin first; if the container needs seeking (an MP4
        whose index is at the end cannot be read from a pipe), once more from
        the file, still under the format and protocol allowlists."""
        started = time.perf_counter()
        try:
            samples = await self._run(source, out_path, pipe=True, reservation=reservation, on_progress=on_progress, stall_s=stall_s)
            mode = "pipe"
        except DecodeFailed:
            samples = await self._run(source, out_path, pipe=False, reservation=reservation, on_progress=on_progress, stall_s=stall_s)
            mode = "file"
        return Decoded(out_path, samples, int((time.perf_counter() - started) * 1000), mode)

    async def _run(
        self,
        source: AudioSource,
        out_path: str,
        *,
        pipe: bool,
        reservation: Reservation,
        on_progress: Callable[[float], Awaitable[None]],
        stall_s: Optional[float],
    ) -> int:
        stall = decode_stall_s() if stall_s is None else float(stall_s)
        tmp = f"{out_path}.{secrets.token_hex(6)}.tmp"
        argv = self.argv(source.path, tmp, pipe=pipe)
        if shutil.which(self.ffmpeg) is None:
            # Our deployment is missing its decoder: not the caller's file.
            raise errors.model_unavailable(30)
        reservation.forget_written()  # a previous attempt's output was deleted
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if pipe else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            # `nice`/`ionice`/ffmpeg could not be started: ours, not the file's.
            log.warning("the audio decoder could not be started")
            raise errors.model_unavailable(30) from None
        loop = asyncio.get_running_loop()
        state = {"last": loop.time(), "fed": 0, "out_us": 0, "size": 0, "ended": False}
        stderr_tail = bytearray()

        def progressed() -> None:
            state["last"] = loop.time()

        async def feed() -> None:
            if not pipe or proc.stdin is None:
                return
            try:
                with open(source.path, "rb") as fh:
                    while True:
                        block = await asyncio.to_thread(fh.read, 256 * 1024)
                        if not block:
                            break
                        proc.stdin.write(block)
                        await proc.stdin.drain()
                        state["fed"] += len(block)
                        progressed()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the decoder exited; its exit status decides
            finally:
                with contextlib.suppress(Exception):
                    proc.stdin.close()

        async def read_progress() -> None:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                key, _, value = line.decode("ascii", "replace").strip().partition("=")
                if key == "out_time_us":
                    with contextlib.suppress(ValueError):
                        us = int(value)
                        if us > state["out_us"]:
                            state["out_us"] = us
                            progressed()
                            await on_progress(us / 1e6)
                elif key == "total_size":
                    with contextlib.suppress(ValueError):
                        size = int(value)
                        if size > state["size"]:
                            state["size"] = size
                            progressed()
                            reservation.ensure_covers(size + (1 << 20))
                            reservation.note_written(size)
                elif key == "progress" and value == "end":
                    state["ended"] = True
                    progressed()

        async def read_stderr() -> None:
            assert proc.stderr is not None
            while True:
                block = await proc.stderr.read(4096)
                if not block:
                    return
                stderr_tail.extend(block)
                del stderr_tail[:-4096]

        tasks = [
            loop.create_task(feed()),
            loop.create_task(read_progress()),
            loop.create_task(read_stderr()),
        ]
        waiter = loop.create_task(proc.wait())
        try:
            while True:
                done, _ = await asyncio.wait({waiter}, timeout=min(1.0, max(0.05, stall / 4)))
                _raise_task_failure(tasks)
                if done:
                    break
                # Output growth is progress too (a decoder that writes without
                # printing, or prints only every few seconds).
                with contextlib.suppress(OSError):
                    size = os.path.getsize(tmp)
                    if size > state["size"]:
                        state["size"] = size
                        progressed()
                        reservation.ensure_covers(size + (1 << 20))
                        reservation.note_written(size)
                if loop.time() - state["last"] > stall:
                    raise DecodeStalled(f"no progress for {stall:.0f}s")
            await asyncio.gather(*tasks, return_exceptions=True)
            _raise_task_failure(tasks)
            code = waiter.result()
            size = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            if code != 0 or size < 2:
                log.info(
                    "decode failed (exit %s, %d bytes out, mode %s)", code, size, "pipe" if pipe else "file"
                )
                raise DecodeFailed("the audio could not be decoded")
            reservation.ensure_covers(size)
            reservation.note_written(size)
            await asyncio.to_thread(os.replace, tmp, out_path)
            return size // 2
        except BaseException:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.shield(waiter)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise


def _raise_task_failure(tasks: Sequence["asyncio.Task"]) -> None:
    """A helper task's exception (the ledger refusing growth mid-decode) is
    the decode's exception; `gather(return_exceptions=True)` would hide it."""
    for task in tasks:
        if task.done() and not task.cancelled() and task.exception() is not None:
            raise task.exception()  # type: ignore[misc]


# -------------------------------------------------------------- dispatch --


class EngineFailure(Exception):
    """A window could not be transcribed; `error` is the wire refusal."""

    def __init__(self, error: errors.ApiError, *, reason: str) -> None:
        super().__init__(reason)
        self.error = error
        self.reason = reason


GateFactory = Callable[[Callable[[Dict[str, Any]], Awaitable[None]]], "contextlib.AbstractAsyncContextManager[None]"]


def _hold_supports_unbounded_wait() -> bool:
    try:
        return "on_wait" in inspect.signature(capacity.hold).parameters
    except (TypeError, ValueError):  # pragma: no cover
        return False


@contextlib.asynccontextmanager
async def capacity_gate(on_wait: Callable[[Dict[str, Any]], Awaitable[None]]) -> AsyncIterator[None]:
    """The fleet-wide public `asr` gate, waited on WITHOUT a deadline.

    With T2's `capacity.hold(wait_s=None, on_wait=…)` the wait is one FIFO
    wait. Until that lands, `hold` needs a finite wait and answers
    `model_unavailable` when it ends; that refusal is a clock, and no capacity
    wait on `/v1` may end on a clock (no-timeout design), so it is re-queued
    here and reported as `queued` instead of surfacing.
    """
    if _hold_supports_unbounded_wait():

        def position_changed(*args: Any, **_kwargs: Any) -> "asyncio.Future[None]":
            # T2's callback shape is not frozen: it may pass a position or
            # nothing, and may or may not await the result. A scheduled task
            # runs either way and is also awaitable.
            position = args[0] if args and isinstance(args[0], int) else None
            return asyncio.ensure_future(on_wait({"stage": "queued", "gate": "asr", "queue_position": position}))

        async with capacity.hold(  # type: ignore[call-arg]
            capacity.GATE_ASR, wait_s=None, yield_to_chat=True, on_wait=position_changed
        ):
            yield
        return
    stack = contextlib.AsyncExitStack()
    waited = 0
    while True:
        try:
            await stack.enter_async_context(
                capacity.hold(capacity.GATE_ASR, wait_s=gate_shim_wait_s(), yield_to_chat=True)
            )
            break
        except errors.ApiError as exc:
            if exc.code != "model_unavailable":
                raise
            waited += 1
            await on_wait({"stage": "queued", "gate": "asr", "requeued": waited})
    async with stack:
        yield


@dataclass
class _Health:
    ready: bool
    cuda_failures: int


def _root_of(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


def _replicas() -> List[str]:
    from . import sidecars

    return list(sidecars.replica_order())


@contextlib.contextmanager
def _counted(base_url: str):
    from . import sidecars

    with sidecars.counted_in_dictation_routing(base_url):
        yield


def _transport() -> Optional[httpx.AsyncBaseTransport]:
    from . import sidecars

    return sidecars._transport


class WhisperDispatcher:
    """One window → one engine reply, under the gate, with fail-over."""

    def __init__(
        self,
        *,
        replicas: Callable[[], List[str]] = _replicas,
        gate: GateFactory = capacity_gate,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._replicas = replicas
        self._gate = gate
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._client: Optional[httpx.AsyncClient] = None
        self.stats: Dict[str, int] = {"engine_calls": 0, "failovers": 0, "silences": 0, "unavailable_waits": 0}

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            transport = self._transport if self._transport is not None else _transport()
            timeout = httpx.Timeout(connect=10.0, read=window_silence_s() + 60.0, write=60.0, pool=60.0)
            self._client = await asyncio.to_thread(
                lambda: httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False)
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None

    async def _health(self, base_url: str) -> Optional[_Health]:
        client = await self._http()
        try:
            response = await asyncio.wait_for(client.get(f"{_root_of(base_url)}/health"), timeout=5.0)
        except (asyncio.TimeoutError, httpx.HTTPError):
            return None
        if response.status_code != 200:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        return _Health(bool(body.get("ready")), int(body.get("cuda_failures") or 0))

    @staticmethod
    def _multipart(clip: bytes, language: Optional[str], index: int) -> Tuple[bytes, str]:
        boundary = secrets.token_hex(16)
        fields = [
            ("model", str(getattr(settings, "asr_model", "") or "whisper")),
            ("response_format", "verbose_json"),
            # The engine's 30-s silence gate is off: these clips are voice
            # activity regions already (video/transcribe.py measured a quiet
            # lead-in emptying a whole window with it on).
            ("no_speech_check", "false"),
        ]
        if language:
            fields.append(("language", language))
        head = bytearray()
        for name, value in fields:
            head += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        head += (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"w{index:05d}.wav\"\r\n"
            "Content-Type: audio/wav\r\n\r\n"
        ).encode()
        return bytes(head) + clip + f"\r\n--{boundary}--\r\n".encode(), boundary

    async def _send_watched(self, base_url: str, clip: bytes, language: Optional[str], index: int) -> Tuple[str, Optional[dict]]:
        """("ok", reply) | ("unavailable", None) | ("silent", None) | ("rejected", None)."""
        baseline = await self._health(base_url)
        if baseline is not None and not baseline.ready:
            return "unavailable", None
        client = await self._http()
        body, boundary = self._multipart(clip, language, index)
        loop = asyncio.get_running_loop()
        self.stats["engine_calls"] += 1
        with _counted(base_url):
            task = loop.create_task(
                client.post(
                    f"{base_url.rstrip('/')}/audio/transcriptions",
                    content=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                )
            )
            sent_at = self._clock()
            try:
                while True:
                    silence_left = window_silence_s() - (self._clock() - sent_at)
                    if silence_left <= 0:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        return "silent", None
                    done, _ = await asyncio.wait({task}, timeout=min(health_poll_s(), silence_left))
                    if done:
                        break
                    health = await self._health(base_url)
                    if health is not None and (
                        not health.ready
                        or (baseline is not None and health.cuda_failures > baseline.cuda_failures)
                    ):
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        return "unavailable", None
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
        try:
            response = task.result()
        except httpx.HTTPError:
            return "unavailable", None
        if response.status_code == 200:
            try:
                reply = response.json()
            except ValueError:
                return "unavailable", None
            return ("ok", reply) if isinstance(reply, dict) else ("unavailable", None)
        if response.status_code >= 500:
            return "unavailable", None
        return "rejected", None

    async def transcribe(
        self,
        clip: bytes,
        *,
        language: Optional[str],
        index: int,
        on_wait: Callable[[Dict[str, Any]], Awaitable[None]],
    ) -> dict:
        """The engine's reply for one clip. Never skips: a window that cannot
        be transcribed raises `EngineFailure`, and the job fails retry-safe."""
        silences = 0
        unavailable_since: Optional[float] = None
        backoff = 1.0
        while True:
            order = self._replicas()
            if not order:
                raise EngineFailure(errors.model_unavailable(30), reason="no speech engine configured")
            async with self._gate(on_wait):
                attempts = list(order)
                position = 0
                while position < len(attempts):
                    base_url = attempts[position]
                    if position:
                        self.stats["failovers"] += 1
                    position += 1
                    outcome, reply = await self._send_watched(base_url, clip, language, index)
                    if outcome == "ok" and reply is not None:
                        return reply
                    if outcome == "rejected":
                        raise EngineFailure(errors.internal_error(), reason="engine refused a window")
                    if outcome == "silent":
                        silences += 1
                        self.stats["silences"] += 1
                        if silences >= 2:
                            raise EngineFailure(errors.model_unavailable(30), reason="window silent twice")
                        # A ready replica that is silent is alive: this is not
                        # unavailability. The one re-send goes to the other
                        # replica, or to the same one when there is only one.
                        unavailable_since = None
                        if len(order) == 1:
                            attempts.append(base_url)
            now = self._clock()
            unavailable_since = now if unavailable_since is None else unavailable_since
            if now - unavailable_since >= unavailable_grace_s():
                raise EngineFailure(errors.model_unavailable(30), reason="speech engines unavailable")
            self.stats["unavailable_waits"] += 1
            await on_wait({"stage": "waiting_for_engine"})
            await self._sleep(backoff)
            backoff = min(30.0, backoff * 2)


# ------------------------------------------------------ decode admission --


class DecodeAdmission:
    """FIFO admission to decoding: at most `concurrency()` decoders, and only
    for a job at most one place behind the head of the public `asr` queue."""

    def __init__(
        self,
        *,
        concurrency: Callable[[], int] = decode_concurrency,
        queue_ahead: Optional[Callable[[], int]] = None,
        poll_s: float = 0.25,
    ) -> None:
        self._concurrency = concurrency
        self._queue_ahead = queue_ahead or self._asr_waiting
        self._poll_s = poll_s
        self._waiters: List[object] = []
        self.active = 0
        self.decoded_not_dispatched = 0
        self.peak_active = 0

    @staticmethod
    def _asr_waiting() -> int:
        try:
            return int(capacity.snapshot()[capacity.GATE_ASR]["waiting"])
        except Exception:  # noqa: BLE001 - advisory
            return 0

    @contextlib.asynccontextmanager
    async def admit(self, on_wait: Callable[[Dict[str, Any]], Awaitable[None]]) -> AsyncIterator[None]:
        ticket = object()
        self._waiters.append(ticket)
        announced = None
        try:
            while True:
                position = self._waiters.index(ticket)
                ahead = self._queue_ahead() + self.decoded_not_dispatched + self.active
                if position == 0 and self.active < self._concurrency() and ahead <= 1:
                    break
                if announced != position:
                    announced = position
                    await on_wait({"stage": "queued", "queue_position": position + max(0, ahead)})
                await asyncio.sleep(self._poll_s)
        finally:
            self._waiters.remove(ticket)
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            yield
        finally:
            self.active -= 1


# ------------------------------------------------------------------ jobs --


@dataclass
class JobEvent:
    type: str  # queued | progress | delta | done | failed
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[errors.ApiError] = None


@dataclass
class TranscriptResult:
    text: str
    segments: List[Segment]
    language: Optional[str]
    duration_s: float
    usage_seconds: int
    report: Dict[str, Any]

    def to_json(self) -> dict:
        return {
            "text": self.text,
            "segments": [s.to_json() for s in self.segments],
            "language": self.language,
            "duration_s": self.duration_s,
            "usage_seconds": self.usage_seconds,
            "report": self.report,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "TranscriptResult":
        return cls(
            text=str(data.get("text") or ""),
            segments=[Segment.from_json(s) for s in data.get("segments") or []],
            language=data.get("language"),
            duration_s=float(data.get("duration_s") or 0.0),
            usage_seconds=int(data.get("usage_seconds") or 0),
            report=dict(data.get("report") or {}),
        )

    def body(self, response_format: str) -> Any:
        """The OpenAI shape of `response_format` (json | text | verbose_json)."""
        usage = {"type": "duration", "seconds": int(self.usage_seconds)}
        if response_format == "text":
            return self.text
        if response_format == "json":
            return {"text": self.text, "usage": usage}
        return {
            "task": "transcribe",
            "language": self.language,
            "duration": round(self.duration_s, 3),
            "text": self.text,
            "segments": [
                {"id": i, "start": s.start_s, "end": s.end_s, "text": s.text}
                for i, s in enumerate(self.segments)
            ],
            "usage": usage,
        }


def _dominant_language(segments: Sequence[Segment]) -> Optional[str]:
    weight: Dict[str, float] = {}
    for s in segments:
        if s.language:
            weight[s.language] = weight.get(s.language, 0.0) + max(0.1, s.end_s - s.start_s)
    return max(weight.items(), key=lambda kv: kv[1])[0] if weight else None


class AudioJob:
    """One transcription run. Followers read its events; it outlives a
    disconnected follower for the orphan grace, and its windows outlive it."""

    def __init__(self, key: str, spec: JobSpec) -> None:
        self.key = key
        self.spec = spec
        self.project_id = spec.project_id
        self.state = "queued"
        self.result: Optional[TranscriptResult] = None
        self.error: Optional[errors.ApiError] = None
        self.task: Optional[asyncio.Task] = None
        self._events: List[JobEvent] = []
        self._progress: Optional[JobEvent] = None
        self._progress_version = 0
        self._changed: List[asyncio.Future] = []
        self.followers = 0
        self._orphan_handle: Optional[asyncio.TimerHandle] = None

    @property
    def terminal(self) -> bool:
        return self.state in ("done", "failed")

    def _notify(self) -> None:
        waiters, self._changed = self._changed, []
        for future in waiters:
            if not future.done():
                future.set_result(None)

    def publish(self, event: JobEvent) -> None:
        if event.type in ("queued", "progress"):
            self._progress = event
            self._progress_version += 1
        else:
            self._events.append(event)
        self._notify()

    async def _changed_or_timeout(self, timeout: Optional[float]) -> bool:
        future = asyncio.get_running_loop().create_future()
        self._changed.append(future)
        try:
            async with asyncio.timeout(timeout):
                await asyncio.shield(future)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            with contextlib.suppress(ValueError):
                self._changed.remove(future)

    @contextlib.asynccontextmanager
    async def follow(self) -> AsyncIterator["Follower"]:
        self.followers += 1
        if self._orphan_handle is not None:
            self._orphan_handle.cancel()
            self._orphan_handle = None
        try:
            yield Follower(self)
        finally:
            self.followers -= 1
            if self.followers <= 0 and not self.terminal and self.task is not None:
                grace = orphan_grace_s()
                loop = asyncio.get_running_loop()
                self._orphan_handle = loop.call_later(grace, self._orphaned)

    def _orphaned(self) -> None:
        self._orphan_handle = None
        if self.followers <= 0 and not self.terminal and self.task is not None:
            log.info("audio job %s has no follower after the grace; cancelling", self.key[:12])
            self.task.cancel()

    async def wait(self) -> TranscriptResult:
        async with self.follow() as follower:
            while True:
                event = await follower.next(None)
                if event is None:
                    if follower.finished:  # pragma: no cover - a terminal event always precedes
                        raise errors.internal_error()
                    continue
                if event.type == "done":
                    assert self.result is not None
                    return self.result
                if event.type == "failed":
                    raise event.error or errors.internal_error()


class Follower:
    """A cursor over one job's events: every delta once, the latest progress."""

    def __init__(self, job: AudioJob) -> None:
        self.job = job
        self.cursor = 0
        self.progress_seen = 0
        self.finished = False

    async def next(self, timeout: Optional[float]) -> Optional[JobEvent]:
        """The next event, or None when `timeout` passes with nothing new."""
        job = self.job
        while True:
            if self.cursor < len(job._events):
                event = job._events[self.cursor]
                self.cursor += 1
                if event.type in ("done", "failed"):
                    self.finished = True
                return event
            if job._progress is not None and self.progress_seen != job._progress_version and not job.terminal:
                self.progress_seen = job._progress_version
                return job._progress
            if self.finished:
                return None
            if not await job._changed_or_timeout(timeout):
                return None


class AudioJobs:
    """The process-wide registry: start, attach, sweep."""

    def __init__(
        self,
        *,
        root: Optional[str] = None,
        ledger: Optional[DiskLedger] = None,
        decoder: Optional[Decoder] = None,
        dispatcher_factory: Optional[Callable[[], WhisperDispatcher]] = None,
        planner: Callable[..., Tuple[List[Window], dict]] = plan_windows,
        admission: Optional[DecodeAdmission] = None,
        model: Optional[str] = None,
    ) -> None:
        self._root = root
        self._ledger = ledger
        self.decoder = decoder or Decoder()
        self._dispatcher_factory = dispatcher_factory or WhisperDispatcher
        self._planner = planner
        self.admission = admission or DecodeAdmission()
        self._model = model
        self._running: Dict[str, AudioJob] = {}

    @property
    def root(self) -> str:
        return self._root or cache_dir()

    @property
    def ledger(self) -> DiskLedger:
        if self._ledger is None:
            ensure_private_dir(self.root)
            self._ledger = ledger_for(self.root)
        return self._ledger

    @property
    def cache(self) -> WindowCache:
        return WindowCache(self.root)

    def model(self) -> str:
        return self._model or str(getattr(settings, "asr_model", "") or "whisper")

    def _result_path(self, key: str) -> str:
        return os.path.join(self.root, "results", key[:2], f"{key}.json")

    def _load_result(self, key: str, project_id: str) -> Optional[TranscriptResult]:
        path = self._result_path(key)
        try:
            with open(path, "rb") as fh:
                data = json.loads(fh.read().decode("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("project_id") != project_id:
            return None
        if time.time() - os.path.getmtime(path) > cache_ttl_s():
            return None
        return TranscriptResult.from_json(data.get("result") or {})

    def _store_result(self, job: AudioJob, result: TranscriptResult) -> None:
        path = self._result_path(job.key)
        ensure_private_dir(os.path.dirname(path))
        tmp = f"{path}.{secrets.token_hex(6)}.tmp"
        payload = json.dumps({"project_id": job.project_id, "result": result.to_json()}, ensure_ascii=False)
        _write_private(tmp, payload.encode("utf-8"))
        os.replace(tmp, path)

    def preflight(self, expected_bytes: int) -> None:
        """Raise `DiskFull` now if `expected_bytes` would not fit (before headers)."""
        self.ledger.check(expected_bytes, purpose="asr-preflight")

    async def attach(self, key: str, project_id: str) -> Optional[AudioJob]:
        """A running or finished job of THIS project, or None (a 404 upstream).
        A stored result is parsed on the worker thread: two hours of segments
        are megabytes of JSON, which the event loop must not parse."""
        running = self._running.get(key)
        if running is not None:
            return running if running.project_id == project_id else None
        stored = await on_worker(self._load_result, key, project_id)
        running = self._running.get(key)
        if running is not None:
            return running if running.project_id == project_id else None
        if stored is None:
            return None
        return self._finished_job(key, project_id, stored)

    def _finished_job(self, key: str, project_id: str, result: TranscriptResult) -> AudioJob:
        spec = JobSpec(project_id, "", None, "json", window_s(), overlap_s(), max_gap_s())
        job = AudioJob(key, spec)
        job.state = "done"
        job.result = result
        job.publish(JobEvent("done", {"text": result.text, "usage_seconds": result.usage_seconds, "cached": True}))
        return job

    async def start(self, spec: JobSpec, source: AudioSource) -> AudioJob:
        key = spec.key()
        running = self._running.get(key)
        if running is not None and running.project_id == spec.project_id:
            await self._discard(source)
            return running
        stored = await on_worker(self._load_result, key, spec.project_id)
        running = self._running.get(key)
        if running is not None and running.project_id == spec.project_id:
            # Another start registered this key while the store was read.
            await self._discard(source)
            return running
        if stored is not None:
            await self._discard(source)
            return self._finished_job(key, spec.project_id, stored)
        job = AudioJob(key, spec)
        self._running[key] = job
        job.task = asyncio.get_running_loop().create_task(self._run(job, source), name=f"asr-job-{key[:12]}")
        return job

    @staticmethod
    async def _discard(source: AudioSource) -> None:
        if source.owned:
            with contextlib.suppress(FileNotFoundError):
                await asyncio.to_thread(os.unlink, source.path)

    async def _estimate_seconds(self, source: AudioSource) -> Tuple[float, str]:
        """The duration the decode reservation is sized from. A header or a
        container can claim any duration, so neither may exceed the bitrate
        upper bound: a file that lies cannot turn itself into a false
        "storage unavailable" (and a real overrun grows the reservation)."""
        bound = source.bytes * 8.0 / float(min_bitrate_bps())
        seconds = await asyncio.to_thread(wav_header_seconds, source.path)
        if seconds:
            return min(seconds, bound), "wav_header"
        probed = await self.decoder.probe_seconds(source.path)
        if probed:
            return min(probed, bound), "ffprobe"
        return bound, "bitrate_bound"

    async def _run(self, job: AudioJob, source: AudioSource) -> None:
        """Decode, plan, dispatch, stitch. The terminal event is published only
        AFTER the decoded PCM is removed and the reservation released, so a
        follower that sees `done` or `failed` never races the cleanup."""
        job_dir = os.path.join(self.root, "jobs", f"{job.key}.{secrets.token_hex(4)}")
        reservation: Optional[Reservation] = None
        heartbeat: Optional[asyncio.Task] = None
        # Counted in `admission.decoded_not_dispatched` from the end of decode
        # until the windows are planned, whatever happens in between.
        waiting_to_dispatch = [False]

        def dispatched() -> None:
            if waiting_to_dispatch[0]:
                waiting_to_dispatch[0] = False
                self.admission.decoded_not_dispatched = max(0, self.admission.decoded_not_dispatched - 1)

        last_progress = [0.0]
        report: Dict[str, Any] = {}
        started = time.perf_counter()
        result: Optional[TranscriptResult] = None
        failure: Optional[Tuple[errors.ApiError, str]] = None
        cancelled = False

        async def progress(data: Dict[str, Any], *, force: bool = False) -> None:
            now = time.monotonic()
            if not force and now - last_progress[0] < PROGRESS_INTERVAL_S:
                return
            last_progress[0] = now
            job.publish(JobEvent("queued" if data.get("stage") == "queued" else "progress", dict(data)))

        async def on_wait(data: Dict[str, Any]) -> None:
            await progress(data, force=True)

        try:
            await asyncio.to_thread(ensure_private_dir, job_dir)
            heartbeat = asyncio.get_running_loop().create_task(self._heartbeat(job_dir))
            await progress({"stage": "queued", "queue_position": 0}, force=True)
            pcm_path = os.path.join(job_dir, "audio.pcm")
            async with self.admission.admit(on_wait):
                job.state = "decoding"
                seconds, basis = await self._estimate_seconds(source)
                need = int(math.ceil(seconds * PCM_BYTES_PER_SECOND * 1.02)) + (1 << 20)
                reservation = self.ledger.reserve(need, purpose="asr-decode")
                await progress({"stage": "decoding", "percent": 0.0}, force=True)

                async def decode_progress(done_s: float) -> None:
                    pct = 0.0 if seconds <= 0 else min(100.0, 100.0 * done_s / seconds)
                    await progress({"stage": "decoding", "percent": round(pct, 1)})

                decoded = await self.decoder.decode(source, pcm_path, reservation=reservation, on_progress=decode_progress)
                self.admission.decoded_not_dispatched += 1
                waiting_to_dispatch[0] = True
            report.update(
                {"duration_basis": basis, "decode_ms": decoded.decode_ms, "decode_input": decoded.input_mode}
            )
            if source.owned:
                await self._discard(source)
            job.state = "transcribing"
            result = await self._transcribe(job, decoded, progress, on_wait, report, dispatched)
            result.report.setdefault("wall_ms", int((time.perf_counter() - started) * 1000))
            await on_worker(self._store_result, job, result)
        except asyncio.CancelledError:
            cancelled = True
            failure = (errors.model_unavailable(30), "cancelled")
        except DiskFull as exc:
            failure = (exc.api_error(), "disk")
        except DecodeStalled:
            failure = (errors.model_unavailable(30), "decode_stalled")
        except DecodeFailed:
            failure = (
                errors.invalid_request("The audio could not be decoded. Send a supported audio file.", param="file"),
                "decode_failed",
            )
        except EngineFailure as exc:
            failure = (exc.error, exc.reason)
        except errors.ApiError as exc:
            failure = (exc, exc.code)
        except Exception:  # noqa: BLE001 - never a traceback on the wire
            log.exception("audio job %s failed", job.key[:12])
            failure = (errors.internal_error(), "internal")
        finally:
            dispatched()
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if reservation is not None:
                reservation.release()
            # Shielded: a cancelled job must still leave no PCM behind.
            await asyncio.shield(asyncio.to_thread(shutil.rmtree, job_dir, True))
            if source.owned:
                await asyncio.shield(self._discard(source))
            if self._running.get(job.key) is job:
                del self._running[job.key]
            if result is not None and failure is None:
                job.result = result
                job.state = "done"
                job.publish(JobEvent("done", {"text": result.text, "usage_seconds": result.usage_seconds}))
            else:
                error, reason = failure or (errors.internal_error(), "internal")
                job.error = error
                job.state = "failed"
                job.publish(JobEvent("failed", {"reason": reason}, error))
        if cancelled:
            raise asyncio.CancelledError()

    async def _heartbeat(self, job_dir: str) -> None:
        beat = os.path.join(job_dir, "heartbeat")
        while True:
            with contextlib.suppress(OSError):
                with open(beat, "ab"):
                    os.utime(beat, None)
            await asyncio.sleep(JOB_HEARTBEAT_S)

    async def _transcribe(
        self,
        job: AudioJob,
        decoded: Decoded,
        progress: Callable[..., Awaitable[None]],
        on_wait: Callable[[Dict[str, Any]], Awaitable[None]],
        report: Dict[str, Any],
        dispatched: Callable[[], None],
    ) -> TranscriptResult:
        import numpy as np

        dispatcher = self._dispatcher_factory()
        try:
            samples = decoded.samples
            total_s = samples / float(SAMPLE_RATE)
            pcm = np.memmap(decoded.path, dtype="<i2", mode="r", shape=(samples,)) if samples else np.zeros(0, dtype="<i2")
            windows, vad_report = await on_worker(
                plan,
                pcm,
                total_s=total_s,
                window=job.spec.window_s,
                overlap=job.spec.overlap_s,
                gap=job.spec.max_gap_s,
                planner=self._planner,
            )
            report["plan"] = vad_report
            dispatched()
            stitcher = Stitcher(windows)
            cache = self.cache
            speech_total = sum(w.duration_s for w in windows) or 1.0
            speech_done = 0.0
            segments: List[Segment] = []
            parts: List[str] = []
            cached = 0
            engine_ms = 0
            longest_clip = 0.0

            def publish_delta(new: Sequence[Segment]) -> None:
                if not new:
                    return
                text = " ".join(s.text for s in new)
                delta = (" " if parts else "") + text
                parts.append(text)
                segments.extend(new)
                job.publish(JobEvent("delta", {"delta": delta, "cues": len(new)}))

            await progress({"stage": "transcribing", "percent": 0.0, "windows_total": len(windows), "windows_done": 0}, force=True)
            for index, window in enumerate(windows):
                a = int(round(window.start_s * SAMPLE_RATE))
                b = int(round(window.end_s * SAMPLE_RATE))
                clip, clip_sha = await on_worker(_clip_and_hash, pcm, a, b)
                longest_clip = max(longest_clip, (b - a) / float(SAMPLE_RATE))
                key = WindowCache.key(
                    project_id=job.project_id, clip_sha256=clip_sha, language=job.spec.language, model=self.model()
                )
                reply = await on_worker(cache.get, key)
                if reply is None:
                    raw = await dispatcher.transcribe(clip, language=job.spec.language, index=index, on_wait=on_wait)
                    engine_ms += int(raw.get("processing_ms") or 0) if isinstance(raw.get("processing_ms"), (int, float)) else 0
                    reply = _slim_reply(raw)
                    await on_worker(cache.put, key, reply)
                else:
                    cached += 1
                del clip
                for segment in reply["segments"]:
                    if not segment.get("language"):
                        segment["language"] = reply.get("language")
                publish_delta(stitcher.feed(index, reply["segments"]))
                speech_done += window.duration_s
                await progress(
                    {
                        "stage": "transcribing",
                        "percent": round(100.0 * speech_done / speech_total, 1),
                        "windows_total": len(windows),
                        "windows_done": index + 1,
                    }
                )
            publish_delta(stitcher.finish())
            text = " ".join(parts)
            report.update(
                {
                    "windows": len(windows),
                    "windows_cached": cached,
                    "longest_clip_s": round(longest_clip, 3),
                    "engine_ms": engine_ms,
                    "stitch": dict(stitcher.report),
                    "dispatch": dict(dispatcher.stats),
                }
            )
            return TranscriptResult(
                text=text,
                segments=segments,
                language=job.spec.language or _dominant_language(segments),
                duration_s=round(total_s, 3),
                usage_seconds=int(math.ceil(samples / float(SAMPLE_RATE))) if samples else 0,
                report=report,
            )
        finally:
            dispatched()
            await dispatcher.aclose()

    def sweep(self, *, now: Optional[float] = None) -> Dict[str, int]:
        """Start-up and periodic hygiene: expired windows and results, source
        files of crashed ingests, and job directories whose heartbeat stopped.
        Blocking file walks: call it through `asyncio.to_thread`."""
        ttl = cache_ttl_s()
        current = time.time() if now is None else now
        removed_jobs = 0
        jobs_root = os.path.join(self.root, "jobs")
        if os.path.isdir(jobs_root):
            for name in os.listdir(jobs_root):
                path = os.path.join(jobs_root, name)
                beat = os.path.join(path, "heartbeat")
                try:
                    stamp = os.path.getmtime(beat) if os.path.exists(beat) else os.path.getmtime(path)
                except FileNotFoundError:
                    continue
                if current - stamp > STALE_JOB_DIR_S:
                    shutil.rmtree(path, ignore_errors=True)
                    removed_jobs += 1
        return {
            "windows": self.cache.sweep(ttl, now=current),
            "results": _sweep_files(os.path.join(self.root, "results"), ttl, now=current),
            "incoming": _sweep_files(os.path.join(self.root, "incoming"), ttl, now=current),
            "jobs": removed_jobs,
        }


_jobs: Optional[AudioJobs] = None


def jobs() -> AudioJobs:
    """The process-wide registry (settings read at call time inside it)."""
    global _jobs
    if _jobs is None:
        _jobs = AudioJobs()
    return _jobs


def reset_for_tests(replacement: Optional[AudioJobs] = None) -> None:
    global _jobs
    _jobs = replacement


# ------------------------------------------------------------ rendering --


def _sse_data(payload: Mapping[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"


async def sse_stream(job: AudioJob, *, heartbeat_s: float = HEARTBEAT_S) -> AsyncIterator[bytes]:
    """`stream=true`: comments while waiting, text deltas, one terminal event.

    The first byte is sent at once (the byte invariant); `: ping` whenever
    nothing else was sent for `heartbeat_s`; `: queued` while the job waits
    for capacity; no `id:` or `retry:` lines. A failure is a `data:` object
    carrying `error`, the shape both SDKs raise from.
    """
    yield b": ping\n\n"
    async with job.follow() as follower:
        while True:
            event = await follower.next(heartbeat_s)
            if event is None:
                if follower.finished:
                    return
                yield b": ping\n\n"
                continue
            if event.type == "queued":
                yield b": queued\n\n"
            elif event.type == "progress":
                stage = str(event.data.get("stage") or "working")
                percent = event.data.get("percent")
                suffix = f" {percent:.0f}%" if isinstance(percent, (int, float)) else ""
                yield f": {re.sub(r'[^a-z_]', '', stage)}{suffix}\n\n".encode("ascii")
            elif event.type == "delta":
                yield _sse_data({"type": "transcript.text.delta", "delta": event.data["delta"], "logprobs": None})
            elif event.type == "done":
                result = job.result
                assert result is not None
                yield _sse_data(
                    {
                        "type": "transcript.text.done",
                        "text": result.text,
                        "logprobs": None,
                        "usage": {"type": "duration", "seconds": int(result.usage_seconds)},
                    }
                )
                return
            elif event.type == "failed":
                error = event.error or errors.internal_error()
                yield _sse_data(error.envelope())
                return


__all__ = [
    "AudioJob",
    "AudioJobs",
    "AudioSource",
    "CapExceeded",
    "DecodeAdmission",
    "DecodeFailed",
    "DecodeStalled",
    "Decoder",
    "DiskFull",
    "EngineFailure",
    "Follower",
    "JobEvent",
    "JobSpec",
    "Stitcher",
    "TranscriptResult",
    "WhisperDispatcher",
    "WindowCache",
    "capacity_gate",
    "fixed_windows",
    "ingest",
    "input_args",
    "jobs",
    "plan",
    "reset_for_tests",
    "sse_stream",
    "wav_header_seconds",
]
