"""The media lane: API audio and video through the existing video pipeline.

ADDED 2026-09-13 (Files API design §6). An uploaded audio or video blob is
analysed by the SAME `video/pipeline.py` the chat app uses — probe, audio,
transcript, frames, OCR, vision, fusion, artifacts — on a new `api` lane that
has its own concurrency slot, yields to live chat, never leaks engine names,
and dedupes only within one project. This module is the hand-off: it hardens
every ffmpeg/ffprobe input, starts the analysis on the api lane, relays its
progress as bare step ids, and turns the finished stage files into the vectors
the model-input side searches.

FOUR THINGS LIVE HERE, and each is testable on its own:

1. FFMPEG INPUT HARDENING (§6.4). A file that is really an HLS playlist or an
   ffconcat script makes ffmpeg's demuxer open the LOCAL PATHS or URLs named
   inside it — reading another tenant's audio, or probing an internal host.
   `SAFE_INPUT_ARGS` (`-protocol_whitelist file,pipe` + a `-format_whitelist`
   with no `hls`/`concat`/`image2`/`lavfi`/`sdp`/`rtsp`/`tee`) refuses that,
   and `PIPE_INPUT_ARGS` (`-protocol_whitelist pipe`, decode from `pipe:0`)
   removes even the `file` protocol for the audio path so no secondary path
   can be opened at all. `-enable_drefs` and `-use_absolute_path` are NEVER
   emitted (their defaults are false on ffmpeg 6.1.1 and 7.0.2, verified this
   round), and `assert_no_dangerous_flags` — the argv canary — is called on
   every argv this module builds so a future edit cannot reintroduce them.

     MEASURED 2026-09-13, container ffmpeg 6.1.1 (sf-local-ai-orchestrator:e2e)
     and static 7.0.2: an unhardened decode of a `source.m3u8` naming a victim
     `secret.wav` produced bytes whose sha256 EQUALLED the victim's; the
     `-format_whitelist` (no `hls`) refused it ("Format not on whitelist
     'hls'"), `-i pipe:0` refused it ("Not detecting m3u8/hls…"), and a
     playlist naming a FIFO was refused BEFORE the FIFO was opened (rc≠124), so
     a reader never blocks. `ffmpeg -h demuxer=mov` shows enable_drefs and
     use_absolute_path both default false.

2. THE HAND-OFF (`start_media_analysis`, §6.2). Probe (hardened) → duration
   ceiling → the PROJECT-KEYED content hash (`storage.api_video_hash`, so the
   chat app's global video dedupe can never tell one tenant about another's
   bytes) → `upsert_video_analysis(lane='api')` → `adopt_source` (a hard link)
   → record the analysis id on the blob → `pipeline.ensure_running`. It does
   NOT call `link_video_attachment` (no conversation) or `video/api.attach_upload`
   (that writes a chat `uploads` row and returns `reused`, a cross-tenant
   presence oracle).

3. PROGRESS RELAY (`map_progress`, §6.1.5). The pipeline's `detail` strings
   name engines (`pipeline.py:884` the router model, `:860` the OCR engine);
   the API renders stage name, status and percent ONLY. `map_progress` maps a
   pipeline event onto the fixed file step ids and DROPS the detail. The
   pipeline's terminal `_done` event is NOT a file state: the files service
   still runs its own `index` and `finalize` steps after it, so `map_progress`
   returns None for it and `is_pipeline_done` is how a consumer sees it; the
   file's terminal status comes from `classify_outcome` after those steps.

4. PACING (`api_pace`, §6.1.2). Before a GPU unit an api-lane job waits while
   a chat generation is live (≤ VIDEO_PACE_MAX_WAIT_S, 20 s) and then while a
   chat-lane job is inside a GPU unit (≤ 120 s), then runs anyway so an api job
   is never starved. Chat first, always.

The pipeline itself, its lane semaphore and its lane-aware `pace()` are
`video/pipeline.py` — an existing file this wave does not edit. The exact
seams are listed in this module's `INTEGRATION_NOTES`.
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Mapping, Optional, Sequence, Tuple

from ..video import media as vmedia  # stdlib-only import: _run, Probe, MediaError

MediaError = vmedia.MediaError
MediaTimeout = vmedia.MediaTimeout


class MediaTooLong(MediaError):
    """The clip is longer than the media duration ceiling → `file_too_complex`."""


class MediaUnreadable(MediaError):
    """The bytes are not readable media → `file_corrupt`: ffprobe refused them
    (damaged, or a format off the whitelist such as a disguised playlist), or
    they hold no audio and no video stream, or report no duration. A plain
    MediaError (the tools are missing or would not start) is NOT this — that is
    the host's fault, not the file's."""


# ==========================================================================
# 1. FFmpeg input hardening (design §6.4)
# ==========================================================================

#: The container/codec formats an API media file may legitimately be. hls,
#: concat, image2, lavfi, sdp, rtsp and tee are DELIBERATELY absent: those are
#: the demuxers that open a second path or URL named inside the file. A file
#: whose real format is not on this list is refused as "Format not on
#: whitelist", which is exactly what an HLS/concat playlist disguised as a
#: `.mp4` becomes (measured 2026-09-13).
FORMAT_WHITELIST: Tuple[str, ...] = (
    "mov", "mp4", "m4a", "3gp", "3g2", "mj2", "matroska", "webm", "avi",
    "mpeg", "mpegts", "mpegps", "ogg", "wav", "mp3", "flac", "aac", "asf",
    "flv", "m4v", "caf", "aiff", "amr",
)

_FORMAT_WHITELIST_STR = ",".join(FORMAT_WHITELIST)

#: For a file input that must be random-accessed (probe, frame seeks): the
#: `file` protocol is allowed, but with drefs off (never set) a file cannot
#: pull in a sibling, and the format whitelist stops a playlist.
SAFE_INPUT_ARGS: Tuple[str, ...] = (
    "-protocol_whitelist", "file,pipe",
    "-format_whitelist", _FORMAT_WHITELIST_STR,
)

#: For the audio path: bytes come through stdin, and NO file protocol is
#: allowed at all, so there is no secondary path to open even in principle.
PIPE_INPUT_ARGS: Tuple[str, ...] = (
    "-protocol_whitelist", "pipe",
    "-format_whitelist", _FORMAT_WHITELIST_STR,
)

#: Flags that turn on ffmpeg's external-data-reference / absolute-path
#: behaviour (the CVE-2016-1897/1898 `dref` local-file-read family). Their
#: defaults are false; this design NEVER sets them, and the canary below keeps
#: it that way. Matched with and without the leading dash so a bare token in an
#: argv list is caught too.
DANGEROUS_INPUT_FLAGS: Tuple[str, ...] = (
    "-enable_drefs", "enable_drefs",
    "-use_absolute_path", "use_absolute_path",
)


class UnsafeFfmpegArgv(AssertionError):
    """An argv built here would enable a dangerous demuxer behaviour."""


def assert_no_dangerous_flags(argv: Sequence[str]) -> None:
    """The argv canary: raise if any dangerous flag is present, ENABLED or not.

    We refuse the flag's mere presence rather than only `... true`, because the
    safe posture is to never mention it — an argv that names `enable_drefs` at
    all is a bug this project does not ship."""
    for token in argv:
        if not isinstance(token, str):
            continue
        low = token.strip().lower()
        if low in DANGEROUS_INPUT_FLAGS:
            raise UnsafeFfmpegArgv(f"refusing an ffmpeg argv that names {token!r}")


def probe_argv(path: str) -> List[str]:
    """ffprobe a file with the hardened input args (design §6.4)."""
    argv = [
        "ffprobe", "-v", "error", "-hide_banner",
        *SAFE_INPUT_ARGS,
        "-print_format", "json",
        "-show_format", "-show_streams",
        "-i", path,
    ]
    assert_no_dangerous_flags(argv)
    return argv


def audio_decode_pipe_argv(out_path: str, *, threads: int = 2) -> List[str]:
    """ffmpeg decode of the audio stream from stdin to a 16 kHz mono PCM WAV
    FILE at `out_path`.

    `pipe:0` in — the belt-and-suspenders audio path (§6.4): the INPUT has no
    `file` protocol (`-protocol_whitelist` precedes `-i`, so it binds the input
    only), so a disguised playlist cannot name a sibling path.

    The output is a seekable file, not `pipe:1` (review finding, 2026-09-13).
    To a pipe, ffmpeg cannot seek back to fill in the WAV header, so the RIFF
    and `data` sizes stay 0xFFFFFFFF (measured on static ffmpeg 7.0.2), and the
    whole decoded track — 439 MiB at the 4-hour ceiling — had to be buffered
    in the orchestrator. To a file the header carries the true sizes (1,920,070
    RIFF / 1,920,000 data bytes for 60 s) and nothing is held in memory. The
    path is passed as `file:<absolute path>` so a name containing `:` can never
    be read as another protocol."""
    target = out_path if out_path.startswith("file:") else "file:" + os.path.abspath(out_path)
    argv = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        *PIPE_INPUT_ARGS,
        "-threads", str(max(1, int(threads))),
        "-i", "pipe:0",
        "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", str(vmedia.SAMPLE_RATE),
        "-c:a", "pcm_s16le", "-f", "wav", target,
    ]
    assert_no_dangerous_flags(argv)
    return argv


# ------------------------------------------------------------- probe -------

_SAMPLE_RATE = vmedia.SAMPLE_RATE


async def probe(path: str, *, timeout_s: float = 60.0) -> "vmedia.Probe":
    """ffprobe the file with the hardened input args and parse it into a
    `video.media.Probe`.

    A DELIBERATELY SEPARATE codepath from `video.media.probe`, which builds an
    un-whitelisted argv (its hardening is an integration item). Keeping the
    hardened argv in THIS module is what makes the argv canary meaningful.

    Raises MediaUnreadable when ffprobe refuses the bytes (a non-zero exit on
    a hardened input: damaged, or a format off the whitelist) or the parse
    finds no stream / no duration; MediaTimeout past the deadline; a plain
    MediaError only when the tools are missing or would not start."""
    if not vmedia.tools_available():
        raise MediaError("ffmpeg/ffprobe are not installed in this container")
    try:
        out, _err = await vmedia._run(  # reuse the kill-on-cancel subprocess runner
            probe_argv(path), timeout_s=timeout_s, what="probing the file"
        )
    except MediaTimeout:
        raise
    except MediaError as exc:
        # vmedia._run raises MediaError for exactly three things: tools
        # missing (checked above), "could not start <tool>" (an OSError — the
        # host's fault), and a non-zero exit (the file's fault).
        if str(exc).startswith("could not start"):
            raise
        raise MediaUnreadable(str(exc)) from exc
    try:
        data = json.loads(out.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as exc:
        raise MediaUnreadable("ffprobe returned something that is not JSON") from exc
    return _parse_probe(data, path)


def _parse_probe(data: dict, path: str) -> "vmedia.Probe":
    """Mirror of video.media.probe's parse, over already-run JSON.

    Kept in step with that function on purpose (attached-picture handling and
    the duration fallbacks are the same); the only difference upstream is the
    hardened argv."""
    fmt = data.get("format") or {}
    streams = [s for s in (data.get("streams") or []) if isinstance(s, dict)]
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None and audio is None:
        raise MediaUnreadable("the file has no audio or video stream")

    def _f(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    duration = _f(fmt.get("duration"))
    if duration <= 0:
        duration = max((_f(s.get("duration")) for s in streams), default=0.0)
    if duration <= 0 and video is not None:
        rate = vmedia._fps(video)
        frames = _f(video.get("nb_frames"))
        if rate > 0 and frames > 0:
            duration = frames / rate
    if duration <= 0:
        raise MediaUnreadable("the file reports no duration; it may be truncated")

    # A cover-art "video" stream (one attached picture in an MP3/M4A) is audio.
    if video is not None and (video.get("disposition") or {}).get("attached_pic"):
        video = None

    return vmedia.Probe(
        duration_s=duration,
        has_video=video is not None,
        has_audio=audio is not None,
        width=int(_f((video or {}).get("width"))),
        height=int(_f((video or {}).get("height"))),
        fps=vmedia._fps(video) if video else 0.0,
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


#: The most ffmpeg stderr kept for an error message. `-loglevel error` is
#: normally a line or two, but a hostile file can make a decoder log one error
#: per packet; only the tail is ever reported, so only the tail is kept.
_STDERR_TAIL_BYTES = 64 * 1024

_FEED_CHUNK_BYTES = 1024 * 1024


async def extract_audio_via_pipe(src_path: str, out_wav: str, *, timeout_s: float,
                                 threads: int = 2) -> int:
    """Decode the audio track to 16 kHz mono WAV at `out_wav` by streaming the
    source bytes through stdin (design §6.4 belt-and-suspenders). Returns the
    sample count read from the written WAV's `data` chunk.

    The whisper server already decodes from `pipe:0`; this is the same shape
    for the video pipeline's own audio stage, so a disguised playlist cannot
    reach a `file` protocol here at all.

    STREAMING, BOTH WAYS (review finding + a truncation bug, 2026-09-13):
      * ffmpeg writes the WAV straight to `<out_wav>.part` (see
        `audio_decode_pipe_argv`), so the decoded track is never held in the
        orchestrator and the header sizes are real;
      * stdin is fed by ONE writer that closes it at EOF. The first version
        called `proc.communicate()` alongside the feeder; communicate() closes
        stdin as soon as it starts, so every write after the first 1 MiB chunk
        was dropped by the closed transport. Measured on a 60 s, 11.5 MB WAV:
        174,773 samples came back instead of 960,000 — a silently truncated
        transcript. stderr is drained concurrently (tail only) and the exit is
        awaited with `proc.wait()`.

    On failure, timeout or cancellation the child is stopped and the `.part`
    file removed; `out_wav` only ever appears complete (os.replace)."""
    if not vmedia.tools_available():
        raise MediaError("ffmpeg/ffprobe are not installed in this container")
    os.makedirs(os.path.dirname(out_wav) or ".", exist_ok=True)
    tmp = out_wav + ".part"
    argv = audio_decode_pipe_argv(tmp, threads=threads)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise MediaError(f"could not start ffmpeg: {exc}") from exc

    async def _feed() -> None:
        assert proc.stdin is not None
        try:
            with open(src_path, "rb") as fh:
                while True:
                    chunk = fh.read(_FEED_CHUNK_BYTES)
                    if not chunk:
                        break
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # ffmpeg stopped reading (a refused format); its exit says why
        finally:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass

    async def _stderr_tail() -> bytes:
        assert proc.stderr is not None
        tail = bytearray()
        while True:
            chunk = await proc.stderr.read(65536)
            if not chunk:
                return bytes(tail)
            tail += chunk
            if len(tail) > _STDERR_TAIL_BYTES:
                del tail[:-_STDERR_TAIL_BYTES]

    feeder = asyncio.ensure_future(_feed())
    err_reader = asyncio.ensure_future(_stderr_tail())
    ok = False
    try:
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_s)
            err = await asyncio.wait_for(err_reader, timeout=5.0)
        except asyncio.TimeoutError:
            await vmedia._stop_child(proc)
            raise MediaTimeout(f"decoding audio did not finish within {timeout_s:.0f}s") from None
        except BaseException:
            await vmedia._stop_child(proc)
            raise
        # A read error part-way through the SOURCE closes stdin early, and
        # ffmpeg then exits 0 on a truncated track — so the feeder's own
        # failure is checked before the exit code is trusted.
        feed_exc = feeder.exception() if feeder.done() and not feeder.cancelled() else None
        if feed_exc is not None:
            raise MediaError(
                f"reading the source for the audio decode failed ({type(feed_exc).__name__})"
            )
        if proc.returncode != 0:
            detail = vmedia._last_line(err) or f"ffmpeg exited with {proc.returncode}"
            for arg in (src_path, tmp):  # a storage path does not belong in a message
                if arg and arg in detail:
                    detail = detail.replace(arg, os.path.basename(arg) or "the file")
            raise MediaError(f"decoding audio failed: {detail}")
        samples = _wav_pcm16_samples(tmp)
        os.replace(tmp, out_wav)
        ok = True
        return samples
    finally:
        for task in (feeder, err_reader):
            if not task.done():
                task.cancel()
        if not ok:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _wav_pcm16_samples(path: str) -> int:
    """The sample count of a mono 16-bit PCM WAV, from its `data` chunk.

    Not `(size - 44) // 2`: ffmpeg's WAV muxer writes a 34-byte LIST/INFO
    chunk before `data` (measured 2026-09-13: fmt@12, LIST@36, data@70), so
    the canonical-header arithmetic over-counts by 17 samples. A `data` size
    of 0xFFFFFFFF (a streamed header) falls back to the bytes actually there."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise MediaError("decoding audio produced no WAV")
        pos = 12
        while pos + 8 <= size:
            fh.seek(pos)
            chunk_id, chunk_len = struct.unpack("<4sI", fh.read(8))
            if chunk_id == b"data":
                available = size - pos - 8
                length = available if chunk_len == 0xFFFFFFFF else min(chunk_len, available)
                return max(0, length) // 2
            pos += 8 + chunk_len + (chunk_len & 1)
    raise MediaError("decoding audio produced a WAV with no data chunk")


# ==========================================================================
# Kind decision + step ids (design §4.3, §6.2)
# ==========================================================================

MEDIA_KINDS: Tuple[str, ...] = ("audio", "video")


def decide_kind(p: "vmedia.Probe") -> str:
    """`video` when there is a (non-cover-art) video stream, else `audio`."""
    return "video" if p.has_video else "audio"


#: The fixed file step ids per kind (design §4.3). SSE and the File object use
#: these numbers, so a resumed run reports the same step it did before.
_VIDEO_STEPS: Tuple[str, ...] = (
    "sniff", "probe", "audio", "transcript", "frames", "ocr", "vision",
    "fusion", "artifacts", "index", "finalize",
)
_AUDIO_STEPS: Tuple[str, ...] = (
    "sniff", "probe", "audio", "transcript", "fusion", "artifacts",
    "index", "finalize",
)


def steps_for(kind: str) -> Tuple[str, ...]:
    return _AUDIO_STEPS if kind == "audio" else _VIDEO_STEPS


def step_index(kind: str, stage: str) -> Optional[int]:
    """The 1-based file step number for a pipeline stage, or None when the
    stage has no file step (the pipeline's own `index`, which the api lane
    skips — the files service builds vectors as its own `index` step)."""
    steps = steps_for(kind)
    try:
        return steps.index(stage) + 1
    except ValueError:
        return None


# ==========================================================================
# 3. Progress relay (design §6.1.5) — bare step ids, NEVER the engine name
# ==========================================================================

#: The pipeline's terminal marker (video/pipeline._DONE).
_PIPELINE_DONE = "_done"

#: The pipeline's own `index` stage writes the shared LanceDB and is SKIPPED on
#: the api lane; the files service owns the `index` step. So a pipeline `index`
#: event is dropped here rather than reported as the files index.
_DROP_PIPELINE_STAGES = frozenset({"index"})


def is_pipeline_done(event: Mapping) -> bool:
    """True for the pipeline's terminal event. It means "go read the row and
    run the files `index` + `finalize` steps", never "the file is processed"."""
    return str(event.get("stage") or "") == _PIPELINE_DONE


def map_progress(kind: str, event: Mapping) -> Optional[dict]:
    """One pipeline progress event → the API's `processing` view, or None.

    Carries ONLY the stage name, its file step id, the total step count, the
    percent and a coarse status. The pipeline's `detail` — which names the
    router and OCR engines and can carry engine error text (§1.2 reuse note) —
    is never copied. None means "nothing to relay": a dropped stage, an event
    with no known stage, or the pipeline's terminal `_done`.

    WHY `_done` IS None (review finding, 2026-09-13). The first version mapped
    it to step=finalize / percent=100 / status=processed. But after the
    pipeline ends the files service still runs step `index` (build the
    vectors) and step `finalize` (§4.3, §6.2 step 5), so relaying that view
    marked a file processed while a retrieval over it would find no vectors,
    and skipped the `index` step in the progress. The terminal file status
    comes from `classify_outcome(row)` AFTER those steps; detect the event with
    `is_pipeline_done`."""
    stage = str(event.get("stage") or "")
    if stage == _PIPELINE_DONE:
        return None
    if stage in _DROP_PIPELINE_STAGES or not stage:
        return None
    step = step_index(kind, stage)
    if step is None:
        return None
    percent = event.get("percent")
    return {
        "stage": stage,
        "step": step,
        "total_steps": len(steps_for(kind)),
        "percent": None if percent is None else round(min(100.0, max(0.0, float(percent))), 1),
        "status": _running_status(str(event.get("status") or "running")),
    }


def _running_status(pipeline_status: str) -> str:
    return {"done": "done", "skipped": "done", "failed": "failed"}.get(
        pipeline_status, "running"
    )


# ==========================================================================
# 4. Pacing (design §6.1.2) — chat first, api job never starved
# ==========================================================================


async def api_pace(
    *,
    chat_live: Callable[[], bool],
    chat_in_gpu_unit: Callable[[], bool],
    live_max_wait_s: Optional[float] = None,
    unit_max_wait_s: Optional[float] = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> float:
    """Hold an api-lane GPU unit while chat needs the engines, then run anyway.

    Two waits in order (design §6.1.2):
      1. while a chat GENERATION is live — ≤ `live_max_wait_s`
         (VIDEO_PACE_MAX_WAIT_S, 20 s), the existing video pace;
      2. while a chat-lane VIDEO job is inside a GPU unit — ≤ `unit_max_wait_s`
         (PUBLIC_API_FILES_YIELD_TO_CHAT_VIDEO_MAX_WAIT_S, 120 s).
    Then it returns and the unit runs, so a busy chat workspace slows an api
    job but never starves it. Returns the seconds waited.

    `sleep` is injectable so a test can drive it without wall-clock waits."""
    if live_max_wait_s is None:
        live_max_wait_s = _video_pace_max_wait_s()
    if unit_max_wait_s is None:
        unit_max_wait_s = _yield_to_chat_video_max_wait_s()
    waited = 0.0
    waited += await _wait_while(chat_live, live_max_wait_s, sleep)
    waited += await _wait_while(chat_in_gpu_unit, unit_max_wait_s, sleep)
    return waited


async def _wait_while(probe: Callable[[], bool], limit: float,
                      sleep: Callable[[float], Awaitable[None]]) -> float:
    waited = 0.0
    while limit > 0 and waited < limit:
        try:
            busy = bool(probe())
        except Exception:  # noqa: BLE001 — the probe is advisory
            busy = False
        if not busy:
            break
        await sleep(1.0)
        waited += 1.0
    return waited


def _video_pace_max_wait_s() -> float:
    try:
        from ..config import settings  # noqa: PLC0415

        return float(getattr(settings, "video_pace_max_wait_s", 20.0))
    except Exception:  # noqa: BLE001
        return 20.0


def _yield_to_chat_video_max_wait_s() -> float:
    try:
        from . import limits  # noqa: PLC0415

        return float(limits.yield_to_chat_video_max_wait_s())
    except Exception:  # noqa: BLE001
        return 120.0


# ==========================================================================
# 2. The hand-off (design §6.2) — start the analysis on the api lane
# ==========================================================================


@dataclass
class MediaDeps:
    """The collaborators the hand-off calls, injected so a test needs no
    database, no ffmpeg pipeline and no disk. `default_deps()` binds the real
    ones lazily (some are integration items, see INTEGRATION_NOTES)."""

    upsert_video_analysis: Callable[..., dict]         # (content_hash, size, media_type, filename, lane=) -> row
    update_api_file_blob: Callable[..., Any]           # (blob_id, **fields)
    get_video_analysis: Callable[[int], Optional[dict]]
    adopt_source: Callable[[str, str, str], str]       # (content_hash, path, filename) -> stored path
    ensure_running: Callable[[int], Awaitable[bool]]
    cancel: Callable[[int], Awaitable[None]]
    remove_analysis: Callable[[str], None]
    read_stage: Callable[[str, str], Any]              # (content_hash, name) -> parsed json or None
    build_chunks: Callable[[Sequence[Any], Sequence[Any]], List[dict]]
    segment_from_json: Callable[[dict], Any]
    ocrspan_from_json: Callable[[dict], Any]


def default_deps() -> MediaDeps:
    """Bind the production collaborators, lazily so this module imports even
    while its neighbours are mid-edit in the shared worktree."""
    from .. import db  # noqa: PLC0415
    from ..video import pipeline as vpipeline  # noqa: PLC0415
    from ..video import store as vstore  # noqa: PLC0415
    from ..video import index as vindex  # noqa: PLC0415
    from ..video.types import OcrSpan, Segment  # noqa: PLC0415

    def _upsert(content_hash: str, size: int, media_type: str, filename: str, *, lane: str = "api") -> dict:
        # Integration: db.upsert_video_analysis gains a `lane` parameter
        # (design §9.2). Until it does, the extra kwarg is dropped so the row
        # is still created (its lane defaults to 'chat' in the V36 column, and
        # the integration turns it to 'api').
        try:
            return db.upsert_video_analysis(content_hash, size, media_type, filename, lane=lane)
        except TypeError:
            return db.upsert_video_analysis(content_hash, size, media_type, filename)

    def _update_blob(blob_id: str, **fields: Any) -> Any:
        # Integration: db.update_api_file_blob (design §9.2) or apifiles.schema.
        try:
            from . import schema  # noqa: PLC0415

            if hasattr(schema, "update_api_file_blob"):
                return schema.update_api_file_blob(blob_id, **fields)
        except Exception:  # noqa: BLE001
            pass
        return db.update_api_file_blob(blob_id, **fields)

    def _read_stage(content_hash: str, name: str) -> Any:
        return vstore.read_json(vstore.stage_path(content_hash, name))

    return MediaDeps(
        upsert_video_analysis=_upsert,
        update_api_file_blob=_update_blob,
        get_video_analysis=db.get_video_analysis,
        adopt_source=vstore.adopt_source,
        ensure_running=vpipeline.ensure_running,
        cancel=vpipeline.cancel if hasattr(vpipeline, "cancel") else _cancel_via_task,
        remove_analysis=vstore.remove_analysis,
        read_stage=_read_stage,
        build_chunks=vindex.build_chunks,
        segment_from_json=Segment.from_json,
        ocrspan_from_json=OcrSpan.from_json,
    )


async def _cancel_via_task(analysis_id: int) -> None:
    """Fallback cancel until `video.pipeline.cancel` lands (§6.1.4)."""
    from ..video import pipeline as vpipeline  # noqa: PLC0415

    task = vpipeline._tasks.get(int(analysis_id))
    if task is not None and not task.done():
        task.cancel()


@dataclass(frozen=True)
class MediaStart:
    analysis_id: int
    content_hash: str
    kind: str
    probe: "vmedia.Probe"


async def start_media_analysis(
    *,
    project_id: str,
    sha256: str,
    blob_id: str,
    original_path: str,
    filename: str,
    media_type: str,
    deps: Optional[MediaDeps] = None,
    max_seconds: Optional[float] = None,
) -> MediaStart:
    """Probe the blob, create its project-keyed analysis on the api lane, and
    start the pipeline. Raises, in the design's failure vocabulary (§6.2):

      * MediaTooLong    → `file_too_complex` (longer than the ceiling);
      * MediaUnreadable → `file_corrupt` (ffprobe refused the bytes, or no
        audio/video stream, or no duration — raised from `probe`, whose parse
        refuses a streamless file before this function sees a Probe);
      * MediaTimeout    → the probe ran out of time (retryable, not the file's
        verdict);
      * MediaError      → the tools are missing or would not start (the host's
        fault: `processing_unavailable`, not `file_corrupt`).

    Nothing is written (no analysis row, no link, no blob update) before the
    probe and the ceiling have passed.

    The video-analysis content hash is `storage.api_video_hash(project_id,
    sha256)` — a project-keyed 64-hex string, never the raw sha256 — so the
    chat app's global dedupe cannot match across projects (§7.2 rule 3)."""
    deps = deps or default_deps()
    p = await probe(original_path)
    if not (p.has_audio or p.has_video):
        raise MediaUnreadable("the file has no audio or video stream")
    ceiling = max_seconds if max_seconds is not None else _media_max_seconds()
    if p.duration_s > ceiling:
        raise MediaTooLong(f"the media is longer than {_hours_phrase(ceiling)}")

    kind = decide_kind(p)
    content_hash = _api_video_hash(project_id, sha256)
    row = deps.upsert_video_analysis(content_hash, int(p.bytes), media_type, filename, lane="api")
    analysis_id = int(row["id"])
    # adopt_source is idempotent: the same bytes handed twice link once.
    deps.adopt_source(content_hash, original_path, filename)
    deps.update_api_file_blob(blob_id, video_analysis_id=analysis_id, kind=kind)
    await deps.ensure_running(analysis_id)
    return MediaStart(analysis_id=analysis_id, content_hash=content_hash, kind=kind, probe=p)


def _api_video_hash(project_id: str, sha256: str) -> str:
    from . import storage  # noqa: PLC0415

    return storage.api_video_hash(project_id, sha256)


def _media_max_seconds() -> float:
    try:
        from . import limits  # noqa: PLC0415

        caps = limits.kind_caps("video")
        if caps.seconds:
            return float(caps.seconds)
    except Exception:  # noqa: BLE001
        pass
    return 14_400.0  # VIDEO_MAX_DURATION_S


def _hours_phrase(seconds: float) -> str:
    hours = seconds / 3600.0
    if abs(hours - round(hours)) < 1e-6:
        n = int(round(hours))
        return f"{n} hour" + ("s" if n != 1 else "")
    return f"{hours:.1f} hours"


# ==========================================================================
# Failure mapping (design §6.2.5)
# ==========================================================================

#: The pipeline marks a deferred (engine-outage) row with this prefix on its
#: `error` (video/pipeline.DEFERRED_MARK). A row that still carries it after
#: the pipeline gave up mapped to `processing_unavailable`, not `file_corrupt`.
_DEFERRED_MARK = "waiting for the model"

#: Substrings in a pipeline `error` that mean the FILE itself was unreadable —
#: a probe or audio-decode failure — so the file is `file_corrupt`, not a
#: transient engine problem.
_CORRUPT_MARKERS = (
    "no audio or video stream", "no duration", "moov atom", "invalid data",
    "could not be read", "truncated", "probing the file",
)


def classify_outcome(row: Mapping) -> Tuple[str, Optional[str]]:
    """A finished `video_analyses` row → (file status, error_code).

    - pipeline `done` → (`processed`, None); optional stages (ocr, vision) may
      have failed and the video is still understood from what remains (§6.2.5);
    - pipeline `failed` after deferrals → (`failed`, `processing_unavailable`);
    - pipeline `failed` because the bytes were unreadable → (`failed`,
      `file_corrupt`);
    - any other failure → (`failed`, `processing_unavailable`)."""
    status = str(row.get("status") or "")
    if status == "done":
        return "processed", None
    error = str(row.get("error") or "").lower()
    if error.startswith(_DEFERRED_MARK) or "deferrals" in error:
        return "failed", "processing_unavailable"
    if any(marker in error for marker in _CORRUPT_MARKERS):
        return "failed", "file_corrupt"
    return "failed", "processing_unavailable"


# ==========================================================================
# Vectors from the finished stage files (design §6.3)
# ==========================================================================


def build_media_chunks(content_hash: str, *, deps: Optional[MediaDeps] = None) -> List[dict]:
    """Read the analysis's `transcript.json` and `screen.json` and build the
    per-blob evidence chunks the files vector index embeds (§6.3).

    Speech ≤ 45 s / 700 chars, screen ≤ 1,500 chars — the SAME chunker the
    chat video index uses (`video/index.build_chunks`), so a 3-hour talk's
    ~240 speech chunks plus screen chunks sit far inside the 50,000 cap. The
    embedding itself is the files `vectors` module's job; this returns the
    chunk rows."""
    deps = deps or default_deps()
    transcript = deps.read_stage(content_hash, "transcript.json") or {}
    screen = deps.read_stage(content_hash, "screen.json") or {}
    segments = [deps.segment_from_json(s) for s in (transcript.get("segments") or [])]
    spans = [deps.ocrspan_from_json(s) for s in (screen.get("spans") or [])]
    return deps.build_chunks(segments, spans)


# ==========================================================================
# Integration notes and settings (for the integration team, design §13.5)
# ==========================================================================

#: The seams in EXISTING files this module's design depends on. Written here so
#: the integration team can apply them; this wave does not edit those files.
INTEGRATION_NOTES = (
    "video/media.py: apply apifiles.media.SAFE_INPUT_ARGS to every ffprobe/"
    "ffmpeg input (probe, extract_frames, render_frame); replace "
    "extract_audio's body with apifiles.media.extract_audio_via_pipe (pipe:0 "
    "input under PIPE_INPUT_ARGS, output written to a seekable file, one "
    "stdin writer — never proc.communicate() alongside a feeder, which "
    "truncated a 60 s source to 10.9 s); never emit -enable_drefs/"
    "-use_absolute_path.",
    "publicapi/sidecars.py:probe_seconds: add PIPE_INPUT_ARGS (it reads "
    "pipe:0) to its ffprobe argv.",
    "apifiles/jobs.py:stage_media: map media.MediaUnreadable -> FileCorrupt "
    "and a PLAIN media.MediaError (tools missing / would not start) -> "
    "processing_unavailable, not FileCorrupt; _follow_pipeline may use "
    "media.is_pipeline_done(event) (map_progress returns None for _done).",
    "video/pipeline.py: add a `lane` column read from the row; a per-lane "
    "semaphore (chat=VIDEO_MAX_CONCURRENT_JOBS, api=PUBLIC_API_FILES_MEDIA_JOBS);"
    " a lane-aware pace() that calls apifiles.media.api_pace for lane='api'; "
    "skip the `index` stage for lane='api'; add cancel(analysis_id); and the "
    "engine-name fixes at :860 and :884.",
    "db.py: upsert_video_analysis gains lane='chat'; add update_api_file_blob "
    "and get_api_file_blob (design §9.2); orphan_video_analyses excludes rows "
    "referenced by api_file_blobs.video_analysis_id and lane='api'.",
    "compose/whisper/server.py: add the same -format_whitelist to its pipe:0 "
    "decoder input.",
)

#: Every setting this module reads — for config.py and the operator reference.
SETTINGS = (
    ("PUBLIC_API_FILES_MEDIA_MAX_SECONDS", "float", 14_400.0,
     "audio/video duration ceiling (VIDEO_MAX_DURATION_S); duration drives cost"),
    ("PUBLIC_API_FILES_YIELD_TO_CHAT_VIDEO_MAX_WAIT_S", "float", 120.0,
     "how long an api media unit waits on a chat video GPU unit before running"),
    ("PUBLIC_API_FILES_MEDIA_JOBS", "int", 1,
     "concurrent api audio/video jobs; the pipeline's api-lane semaphore"),
)
