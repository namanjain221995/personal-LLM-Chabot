"""ffprobe / ffmpeg, as subprocesses: probe a file, pull its audio, pull frames.

WHY SUBPROCESSES AND NOT PyAV OR OpenCV. Three reasons, all measured or
observed on this deployment rather than assumed:

* A two-hour 1080p decode is minutes of CPU. Done in-process it competes with
  the orchestrator's event loop for the GIL and the cores; an `ffmpeg` child
  is its own process with its own `-threads` cap, and killing it on a
  deadline is `proc.kill()` rather than hoping a C extension checks a flag.
* Nothing is loaded whole. Audio streams to a WAV on disk, frames are written
  as they are selected, and a 4 GB upload never becomes a 4 GB array.
* The container already runs ffmpeg for dictation (compose/whisper) and the
  apt package on aarch64 Ubuntu 24.04 decodes every codec a browser or phone
  produces — H.264, HEVC, VP8/VP9, AV1, AAC, Opus — with no wheel to trust.

EVERY CALL HAS A DEADLINE. A corrupt file can make a decoder spin; a deadline
turns that into a clean `MediaError` with the last line ffmpeg wrote, which is
almost always the real reason ("moov atom not found", "Invalid data found
when processing input").
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

log = logging.getLogger(__name__)


class MediaError(RuntimeError):
    """The file could not be read as media, or a tool is missing."""


class MediaTimeout(MediaError):
    """A media tool ran past its deadline and was killed."""


@dataclass(frozen=True)
class Probe:
    """What ffprobe found. Nothing here is invented: an absent field is None."""

    duration_s: float
    has_video: bool
    has_audio: bool
    width: int
    height: int
    fps: float
    video_codec: str
    audio_codec: str
    container: str
    bytes: int
    raw: dict = field(default_factory=dict)

    def summary(self) -> dict:
        """The part worth persisting on the analysis row."""
        return {
            "duration_s": round(self.duration_s, 3),
            "has_video": self.has_video,
            "has_audio": self.has_audio,
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 3),
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "container": self.container,
            "bytes": self.bytes,
        }


@dataclass(frozen=True)
class FrameFile:
    """One extracted frame: where it is, and WHEN it is."""

    path: str
    t_s: float
    index: int


def tools_available() -> bool:
    return bool(shutil.which("ffprobe") and shutil.which("ffmpeg"))


def _last_line(stderr: bytes) -> str:
    lines = [ln.strip() for ln in stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


async def _run(
    argv: Sequence[str], *, timeout_s: float, what: str
) -> tuple[bytes, bytes]:
    """Run one tool to completion under a deadline; return (stdout, stderr)."""
    if not tools_available():
        raise MediaError("ffmpeg/ffprobe are not installed in this container")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise MediaError(f"could not start {argv[0]}: {exc}") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        raise MediaTimeout(f"{what} did not finish within {timeout_s:.0f}s") from None
    if proc.returncode != 0:
        detail = _last_line(err) or f"{argv[0]} exited with {proc.returncode}"
        # The message reaches the chat; a storage path does not belong there.
        for arg in argv:
            if isinstance(arg, str) and arg.startswith("/") and arg in detail:
                detail = detail.replace(arg, os.path.basename(arg) or "the file")
        raise MediaError(f"{what} failed: {detail}")
    return out, err


# ------------------------------------------------------------------- probe --


async def probe(path: str, *, timeout_s: float = 60.0) -> Probe:
    """ffprobe the file. Raises MediaError when it is not readable media.

    Duration comes from the container first and falls back to the longest
    stream; a WebM straight out of a browser's MediaRecorder often has no
    container duration at all, and refusing those would refuse the most
    common screen recording there is.
    """
    out, _err = await _run(
        [
            "ffprobe", "-v", "error", "-hide_banner",
            "-print_format", "json",
            "-show_format", "-show_streams",
            path,
        ],
        timeout_s=timeout_s,
        what="probing the file",
    )
    try:
        data = json.loads(out.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError("ffprobe returned something that is not JSON") from exc
    fmt = data.get("format") or {}
    streams = [s for s in (data.get("streams") or []) if isinstance(s, dict)]
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None and audio is None:
        raise MediaError("the file has no audio or video stream")

    def _f(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    duration = _f(fmt.get("duration"))
    if duration <= 0:
        duration = max((_f(s.get("duration")) for s in streams), default=0.0)
    if duration <= 0 and video is not None:
        # Last resort: frames / rate, when the muxer wrote a frame count.
        rate = _fps(video)
        frames = _f(video.get("nb_frames"))
        if rate > 0 and frames > 0:
            duration = frames / rate
    if duration <= 0:
        raise MediaError("the file reports no duration; it may be truncated")

    # A cover-art "video" stream (one attached picture in an MP3/M4A) is not a
    # video. ffprobe marks those with disposition.attached_pic.
    if video is not None and (video.get("disposition") or {}).get("attached_pic"):
        video = None

    return Probe(
        duration_s=duration,
        has_video=video is not None,
        has_audio=audio is not None,
        width=int(_f((video or {}).get("width"))),
        height=int(_f((video or {}).get("height"))),
        fps=_fps(video) if video else 0.0,
        video_codec=str((video or {}).get("codec_name") or ""),
        audio_codec=str((audio or {}).get("codec_name") or ""),
        container=str(fmt.get("format_name") or ""),
        bytes=int(_f(fmt.get("size")) or (os.path.getsize(path) if os.path.exists(path) else 0)),
        raw={
            "format": {k: fmt.get(k) for k in ("format_name", "format_long_name", "duration", "size", "bit_rate")},
            "streams": [
                {k: s.get(k) for k in ("codec_type", "codec_name", "width", "height", "r_frame_rate", "avg_frame_rate", "sample_rate", "channels", "duration", "nb_frames")}
                for s in streams
            ],
        },
    )


def _fps(stream: Optional[dict]) -> float:
    """avg_frame_rate first (what was actually recorded), r_frame_rate second."""
    if not stream:
        return 0.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = str(stream.get(key) or "")
        if "/" in raw:
            num, _sep, den = raw.partition("/")
            try:
                n, d = float(num), float(den)
            except ValueError:
                continue
            if d > 0 and n > 0:
                return n / d
        else:
            try:
                value = float(raw)
            except ValueError:
                continue
            if value > 0:
                return value
    return 0.0


# ------------------------------------------------------------------- audio --

SAMPLE_RATE = 16000


async def extract_audio(
    path: str, out_wav: str, *, timeout_s: float, threads: int = 2
) -> int:
    """Decode the audio track to 16 kHz mono 16-bit PCM WAV at `out_wav`.

    Returns the number of samples written. Whisper wants exactly this shape;
    VAD wants exactly this shape; doing it once here means every later stage
    reads one small, seekable file instead of touching the upload again — and
    the upload lives in a directory that is swept after 24 hours, while this
    file lives beside the analysis.

    Nothing is enhanced: no normalisation, no denoise. The honest thing to
    transcribe is what was recorded (the dictation engine reached the same
    conclusion by measurement).
    """
    os.makedirs(os.path.dirname(out_wav) or ".", exist_ok=True)
    tmp = out_wav + ".part"
    await _run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-threads", str(max(1, threads)),
            "-i", path,
            "-vn", "-sn", "-dn",
            "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-c:a", "pcm_s16le", "-f", "wav",
            tmp,
        ],
        timeout_s=timeout_s,
        what="extracting the audio track",
    )
    os.replace(tmp, out_wav)
    size = os.path.getsize(out_wav)
    # 44-byte canonical header; ffmpeg writes a WAV with a LIST chunk too, so
    # the sample count is derived from the data size on read (see pcm.py).
    return max(0, (size - 44) // 2)


# ------------------------------------------------------------------ frames --

_PTS_RE = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")


async def extract_frames(
    path: str,
    out_dir: str,
    *,
    scene_threshold: float,
    floor_s: float,
    max_width: int,
    keyframes_only: bool,
    timeout_s: float,
    threads: int = 2,
    jpeg_quality: int = 3,
) -> List[FrameFile]:
    """Write the frames worth looking at, with their timestamps.

    ONE PASS, TWO RULES, inside ffmpeg's own `select` filter:

      gt(scene, T)                     the picture changed (a cut, a new slide,
                                       a window switch);
      gte(t - prev_selected_t, FLOOR)  or it has been FLOOR seconds since the
                                       last one we kept — the periodic floor,
                                       so a slow zoom or a scrolling document
                                       that never trips the detector is still
                                       sampled.

    `isnan(prev_selected_t)` admits the very first frame. Frames are scaled
    to at most `max_width` wide (OCR needs legible text; a caption does not
    need 4K) and written as JPEG, and `showinfo` prints one line per SELECTED
    frame carrying `pts_time`, which is the timestamp — parsed from stderr in
    order, one per file. Dedupe (a slide held for six minutes still yields a
    frame every FLOOR seconds here) is `frames.py`'s job, by perceptual hash.

    `keyframes_only` asks the decoder to skip everything but I-frames
    (`-skip_frame nokey`). On a long recording that is the difference between
    minutes and tens of minutes of CPU: only keyframes are decoded, so the
    detector sees one picture every GOP (typically 1-10 s) rather than every
    frame, and a slide change is noticed at the next keyframe rather than the
    exact frame. The pipeline turns it on above a duration threshold.
    """
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, "f_%06d.jpg")
    threshold = min(max(float(scene_threshold), 0.01), 1.0)
    floor = max(0.5, float(floor_s))
    select = (
        f"select='gt(scene,{threshold:.3f})+isnan(prev_selected_t)"
        f"+gte(t-prev_selected_t,{floor:.3f})'"
    )
    scale = f"scale='min({int(max_width)},iw)':-2:flags=area"
    argv = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-y",
        "-threads", str(max(1, threads)),
    ]
    if keyframes_only:
        argv += ["-skip_frame", "nokey"]
    argv += [
        "-i", path,
        "-an", "-sn", "-dn",
        "-vf", f"{select},{scale},showinfo",
        "-fps_mode", "vfr",
        "-q:v", str(int(jpeg_quality)),
        "-f", "image2", pattern,
    ]
    _out, err = await _run(argv, timeout_s=timeout_s, what="extracting frames")
    stamps = [float(m.group(1)) for m in _PTS_RE.finditer(err.decode("utf-8", "replace"))]
    files = sorted(
        f for f in os.listdir(out_dir) if f.startswith("f_") and f.endswith(".jpg")
    )
    if len(files) != len(stamps):
        # Every selected frame produces exactly one showinfo line and one
        # file; a mismatch means stderr was clipped or a file failed to write.
        # Trust the files that exist and keep the timestamps that pair with
        # them in order, rather than inventing any.
        log.warning(
            "frame extraction: %d file(s) but %d timestamp(s); pairing in order",
            len(files), len(stamps),
        )
    frames: List[FrameFile] = []
    for i, (name, t) in enumerate(zip(files, stamps)):
        frames.append(FrameFile(path=os.path.join(out_dir, name), t_s=max(0.0, t), index=i))
    # Anything unpaired is deleted so the directory only ever holds frames
    # the analysis knows the time of.
    for name in files[len(frames):]:
        try:
            os.unlink(os.path.join(out_dir, name))
        except OSError:
            pass
    return frames


async def render_frame(path: str, t_s: float, out_jpg: str, *, max_width: int, timeout_s: float = 60.0) -> str:
    """One frame at one time, for a question that needs the model to LOOK.

    Seeks before decoding (`-ss` before `-i`) — a seek to 1:14:22 in a
    two-hour file is instant that way, and would decode 74 minutes of video
    the other way round.
    """
    os.makedirs(os.path.dirname(out_jpg) or ".", exist_ok=True)
    await _run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{max(0.0, float(t_s)):.3f}",
            "-i", path,
            "-frames:v", "1",
            "-vf", f"scale='min({int(max_width)},iw)':-2:flags=area",
            "-q:v", "3",
            out_jpg,
        ],
        timeout_s=timeout_s,
        what="rendering a frame",
    )
    return out_jpg
