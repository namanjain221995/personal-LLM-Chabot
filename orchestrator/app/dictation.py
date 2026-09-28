"""Recording sessions: chunked, stored dictation with no length limit (2026-09-29).

THE OWNER'S ASK, 2026-09-28: "user click on that audio button then it record
right ?? and store and also ??? as it give transcript as long as i talk More
then 1 hr then Also it work ???". Four requirements came out of it: STORE the
recording, NO LENGTH LIMIT (one hour must work, two hours is the stress case),
the transcript appears AS THEY TALK, and the browser sends the recording in
pieces WHILE it records.

WHY THE OLD PATH FAILED, AS MEASURED. POST /audio/transcribe took the whole
recording as one request. The speech engine (compose/whisper/server.py) judges
only the FIRST 30 s of a clip against its no-speech gate and, when that opening
is quiet, returns empty text for the whole clip without decoding it; asr.py
skips its gate-off second decode for any clip over 120 s; and the browser shows
"closer to the microphone". The owner's own 181,427 ms dictation of 2026-09-24
came back empty in 2,175 ms exactly that way, and the reproduction (25 s of
quiet, then 156 s of LibriVox) did the same in 0.52-0.76 s, 5 of 5.

WHAT A SESSION IS. The browser opens one (POST /audio/sessions) and uploads the
output of ONE MediaRecorder, 5 s at a time, as numbered PARTS of one continuous
container stream. Each part is fsynced onto the end of ONE file per recording
before it is acknowledged, so the recording is stored as it arrives and a
dropped network costs one retried part. A worker per session tails that file
through one streaming decoder, runs the video pipeline's own voice-activity
detector over the PCM (video/vad.py: frame_flags, regions_from_flags,
windows_from_regions) and sends the engine WINDOWS of at most 30 s, cut at
pauses, with its silence gate OFF, as video/pipeline.py already does. The
public API's incremental Stitcher (publicapi/audio_jobs.Stitcher) joins the
windows and never takes back text it has released, so the transcript grows
while the person talks.

WHY THE OWNER'S ERROR CANNOT HAPPEN HERE. Silence is decided by voice-activity
detection on this side, a stretch with no speech is never sent at all, and
every window that IS sent goes with `no_speech_check=false`. A quiet opening
produces no window; it cannot suppress the speech after it. Each window is its
own engine request with its own decoder budget, so no token or length ceiling
applies to the session as a whole. Each window's words are then judged on
their own, the way dictation judges a clip (`_Live._transcribe`).

MEASURED ON THE LIVE WORKER ENGINE, 2026-09-29 (in-process harness, worker
replica only). The owner's reproduction (25 s of quiet, then 156 s of
LibriVox): legacy path 5 of 5 empty, 'unclear', 0.57-0.62 s; session 3 of 3
'transcribed', 424 words, the chapter title included. A 18.3 s dictation
arriving 5 s into somebody's 540 s one waited 73.0 and 98.8 s on the legacy
path and 4.5 and 5.3 s as a session (alone: 2.0 s either way).

TWO KINDS OF CHUNK, KEPT APART. A PART is transport and durability; a WINDOW is
transcription. A part boundary is a byte offset in the container, not a moment
in the audio: the stored file is exactly what the encoder produced and it is
decoded as one stream, so no part boundary can cut a word.

WHAT IS STORED, per user, directories 0700 and files 0600, under
VOICE_DATA_DIR/<user_id>/<session_id>/:
    source.<ext>     the recording exactly as the browser encoded it
    parts.jsonl      one line per accepted part (seq, offset, bytes, sha256)
    audio.pcm        16 kHz mono s16le derived from source, deleted at the end
    plan.jsonl       one line per committed window, with the planner cursor
    results.jsonl    one line per finished window attempt (the latest wins)
    transcript.json  segments, text, language, gaps and a report
    transcript.txt   the final text
Readable through the application by its owner, and by a super admin through
the audited admin routes. VOICE_RETENTION_DAYS (0 = keep) is the only thing
that deletes a finished recording, apart from the person.

THE COST TO CHAT, STATED PLAINLY. Whisper decodes one clip at a time per
replica, and the chat model is tensor-parallel across both Sparks, so while a
speech window decodes on EITHER node chat slows for everyone: 106 tok/s idle,
53 with one node's speech engine saturated, 28 with both (measured
2026-09-28). A session spreads that cost across the recording instead of
delivering it as one exclusive block after Stop, keeps at most
VOICE_SESSION_ASR_CONCURRENCY (1) session window decoding fleet-wide so
sessions alone never take both nodes, and waits up to VOICE_SESSION_PACE_LIVE_S
before each window while a chat answer is streaming. It does not make the cost
disappear.

WHERE IT RUNS. Every session worker lives on one dedicated event-loop thread
(`_Runner`), not on the request loop: a request's lifetime (and a test client's
per-request portal) must not own a job that outlives it, and the decoder, the
voice-activity detector and the stitcher then never compete with the chat
streams for the main loop.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import asr, db, metrics
from .config import settings
from .publicapi import audio_jobs
from .video import loops, vad
from .video.transcribe import dominant_language, wav_bytes
from .video.types import Segment

log = logging.getLogger(__name__)

SAMPLE_RATE = vad.SAMPLE_RATE
PCM_BYTES_PER_S = SAMPLE_RATE * 2
FRAME_S = vad.FRAME_MS / 1000.0

STATUS_RECORDING = "recording"
STATUS_FINISHING = "finishing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
LIVE_STATUSES = (STATUS_RECORDING, STATUS_FINISHING)

ENDED_BY = ("person", "idle", "storage_full", "recorder_error", "lost_parts", "page_hidden")
#: What the BROWSER may say about why it stopped; the other two are the server's.
ENDED_BY_CLIENT = ("person", "recorder_error", "lost_parts", "page_hidden")

#: The owner of leases taken by this process (the video pipeline's shape).
_OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

#: The long-poll ceiling. Far under Cloudflare's 125 s first-byte limit and the
#: 300 s Node and undici timers, so no heartbeat bytes are needed.
LONG_POLL_MAX_S = 25

#: A window that met a busy or unreachable engine is sent again this many
#: times before it becomes a gap; the waits are video/transcribe's backoff.
_WINDOW_ATTEMPTS = 4

#: How long an orphaned directory (no row, e.g. after an account was deleted
#: and its rows cascaded) is left before the sweep removes it.
_ORPHAN_GRACE_S = 24 * 3600.0

#: The session's row is written at most this often while it runs (status
#: changes are written at once). A part's own update is separate.
_ROW_WRITE_EVERY_S = 2.0

_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

#: MediaRecorder's MIME type -> the stored file's extension. The CONTENT TYPE
#: decides, never a filename (app/audio_api.py's rule).
_EXTENSIONS = {
    "audio/webm": "webm", "video/webm": "webm",
    "audio/mp4": "mp4", "video/mp4": "mp4", "audio/m4a": "m4a", "audio/x-m4a": "m4a",
    "audio/ogg": "ogg", "audio/opus": "opus",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
    "audio/mpeg": "mp3", "audio/mpga": "mp3",
    "audio/flac": "flac", "audio/x-flac": "flac",
    "audio/aac": "aac", "audio/3gpp": "3gp",
}


class SessionError(Exception):
    """A refusal with the flat body the contract promises:
    {"detail": sentence, "reason": code, ...extra}."""

    def __init__(self, http_status: int, reason: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status = int(http_status)
        self.reason = reason
        self.detail = detail
        self.extra = extra

    def body(self) -> Dict[str, Any]:
        return {"detail": self.detail, "reason": self.reason, **self.extra}


def _not_found() -> SessionError:
    # One answer for unknown, malformed, someone else's, cancelled and
    # removed, so the id space is not an oracle.
    return SessionError(404, "not_found", "This recording is no longer on the server.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


# --------------------------------------------------------------- files --


def data_root() -> str:
    return settings.voice_data_dir


def session_dir(user_id: int, session_id: str) -> str:
    if not _SESSION_ID.match(session_id or ""):
        raise ValueError("session id must be 32 hex characters")
    return os.path.join(data_root(), str(int(user_id)), session_id)


def _path(user_id: int, session_id: str, name: str) -> str:
    return os.path.join(session_dir(user_id, session_id), name)


def source_path(row: Dict[str, Any]) -> str:
    return _path(row["user_id"], row["id"], f"source.{row['ext']}")


def _private_dirs(user_id: int, session_id: str) -> str:
    """<root>/<user>/<session>, every level 0700 whatever the umask
    (publicapi/disk_ledger.ensure_private_dir)."""
    from .publicapi.disk_ledger import ensure_private_dir

    ensure_private_dir(data_root())
    ensure_private_dir(os.path.join(data_root(), str(int(user_id))))
    return ensure_private_dir(session_dir(user_id, session_id))


def _append_line(path: str, payload: Dict[str, Any]) -> None:
    """One JSON line onto an append-only 0600 file, fsynced."""
    line = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        view = memoryview(line)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_lines(path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    # A torn last line after a crash: everything before it
                    # is intact, and the unrecorded work is simply redone.
                    continue
    except OSError:
        return []
    return out


def _read_json(path: str) -> Optional[Any]:
    from .video import store

    return store.read_json(path)


def free_bytes(path: Optional[str] = None) -> Optional[int]:
    """Free space on the filesystem holding `path` (default VOICE_DATA_DIR),
    for an unprivileged writer; None when it cannot be measured."""
    probe = path or data_root()
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        st = os.statvfs(probe or "/")
    except OSError:
        return None
    return int(st.f_bavail) * int(st.f_frsize)


def _storage_refusal() -> Optional[SessionError]:
    try:
        root = data_root()
        from .publicapi.disk_ledger import ensure_private_dir

        ensure_private_dir(root)
        if not os.access(root, os.W_OK):
            raise OSError("not writable")
    except OSError:
        return SessionError(503, "storage_unavailable", "The server can't save audio right now.")
    free = free_bytes(root)
    if free is not None and free < settings.voice_min_free_bytes:
        return SessionError(507, "storage_full", "The server has no space left for recordings.")
    return None


# ------------------------------------------------------------- the rows --


_COLUMNS = (
    "id, user_id, client_key, status, mime_type, ext, rev, next_part, bytes_stored, "
    "audio_ms, transcribed_ms, speech_ms, windows_total, windows_done, windows_failed, "
    "outcome, ended_by, language, language_code, source_sha256, retranscribe, created_at, "
    "last_part_at, finish_requested_at, finished_at, audio_deleted_at, lease_owner, "
    "lease_expires_at, error"
)


def _row(session_id: str) -> Optional[Dict[str, Any]]:
    if not _SESSION_ID.match(session_id or ""):
        return None
    with db.connection() as con:
        row = con.execute(f"SELECT {_COLUMNS} FROM voice_sessions WHERE id = %s", (session_id,)).fetchone()
    return dict(row) if row else None


def _owned_row(session_id: str, user_id: int, *, allow_cancelled: bool = False) -> Dict[str, Any]:
    row = _row(session_id)
    if row is None or int(row["user_id"]) != int(user_id):
        raise _not_found()
    if row["status"] == STATUS_CANCELLED and not allow_cancelled:
        raise _not_found()
    return row


def _update(
    session_id: str,
    *,
    bump: bool = True,
    rev: Optional[int] = None,
    only_live: bool = False,
    **fields: Any,
) -> Optional[Dict[str, Any]]:
    """Set `fields`, move `rev` forward, return the row. `only_live` touches a
    row only while it is recording or finishing, so a worker that is still
    unwinding can never resurrect a session its owner discarded."""
    sets = []
    values: List[Any] = []
    for key, value in fields.items():
        if key == "error" and value is not None:
            value = json.dumps(value)
        sets.append(f"{key} = %s")
        values.append(value)
    if rev is not None:
        sets.append("rev = GREATEST(rev + 1, %s)")
        values.append(int(rev))
    elif bump:
        sets.append("rev = rev + 1")
    if not sets:
        return _row(session_id)
    values.append(session_id)
    guard = " AND status IN ('recording', 'finishing')" if only_live else ""
    with db.connection() as con:
        row = con.execute(
            f"UPDATE voice_sessions SET {', '.join(sets)} WHERE id = %s{guard} RETURNING {_COLUMNS}",
            tuple(values),
        ).fetchone()
    return dict(row) if row else None


def _create_row(user_id: int, client_key: str, mime: str, ext: str) -> Tuple[Dict[str, Any], bool]:
    """(row, created). Idempotent on (user, client_key); SessionError 409 when
    this person already has a recording in progress."""
    import psycopg

    session_id = uuid.uuid4().hex
    try:
        with db.connection() as con:
            row = con.execute(
                f"""INSERT INTO voice_sessions (id, user_id, client_key, status, mime_type, ext,
                        created_at, lease_owner, lease_expires_at)
                    VALUES (%s, %s, %s, 'recording', %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, client_key) DO NOTHING
                    RETURNING {_COLUMNS}""",
                (
                    session_id, int(user_id), client_key, mime, ext, _now(), _OWNER,
                    _now() + timedelta(seconds=settings.voice_session_lease_ttl_s),
                ),
            ).fetchone()
    except psycopg.errors.UniqueViolation:
        # The partial unique index: one live recording per person.
        with db.connection() as con:
            other = con.execute(
                "SELECT id, created_at, audio_ms FROM voice_sessions WHERE user_id = %s AND status = 'recording'",
                (int(user_id),),
            ).fetchone()
        raise SessionError(
            409,
            "session_active",
            "You're already recording in another tab or on another device.",
            session_id=(other or {}).get("id"),
            started_at=_iso((other or {}).get("created_at")),
            audio_ms=int((other or {}).get("audio_ms") or 0),
        ) from None
    if row is not None:
        return dict(row), True
    with db.connection() as con:
        existing = con.execute(
            f"SELECT {_COLUMNS} FROM voice_sessions WHERE user_id = %s AND client_key = %s",
            (int(user_id), client_key),
        ).fetchone()
    if existing is None:  # pragma: no cover - the conflict row vanished in between
        raise SessionError(503, "storage_unavailable", "The server can't save audio right now.")
    if existing["status"] == STATUS_CANCELLED:
        raise _not_found()
    return dict(existing), False


def _live_count() -> int:
    with db.connection() as con:
        row = con.execute(
            "SELECT count(*) AS n FROM voice_sessions WHERE status IN ('recording', 'finishing')"
        ).fetchone()
    return int(row["n"]) if row else 0


def _claim_lease(session_id: str) -> Optional[Dict[str, Any]]:
    now = _now()
    with db.connection() as con:
        row = con.execute(
            f"""UPDATE voice_sessions SET lease_owner = %s, lease_expires_at = %s
                WHERE id = %s AND status IN ('recording', 'finishing')
                  AND (lease_owner IS NULL OR lease_owner = %s OR lease_expires_at IS NULL
                       OR lease_expires_at < %s)
                RETURNING {_COLUMNS}""",
            (_OWNER, now + timedelta(seconds=settings.voice_session_lease_ttl_s), session_id, _OWNER, now),
        ).fetchone()
    return dict(row) if row else None


def _release_lease(session_id: str) -> None:
    with db.connection() as con:
        con.execute(
            "UPDATE voice_sessions SET lease_owner = NULL, lease_expires_at = NULL "
            "WHERE id = %s AND lease_owner = %s",
            (session_id, _OWNER),
        )


def _record_attempt(row: Dict[str, Any], status: str, processing_ms: Optional[int]) -> None:
    """One V19 row per finished session, so the console keeps counting
    dictation. Metadata only, as V19 promises of its own table."""
    db.record_voice_transcription(
        user_id=int(row["user_id"]),
        duration_ms=int(row.get("audio_ms") or 0) or None,
        language=row.get("language"),
        processing_ms=processing_ms,
        status=status,
        degraded=False,
    )


# ----------------------------------------------------------- rate limits --

_rate_lock = threading.Lock()
_recent: Dict[Tuple[str, int], List[float]] = {}


def _rate_ok(kind: str, user_id: int, limit: int) -> bool:
    now = time.monotonic()
    with _rate_lock:
        key = (kind, int(user_id))
        window = [t for t in _recent.get(key, []) if now - t < 60.0]
        if len(window) >= limit:
            _recent[key] = window
            return False
        window.append(now)
        _recent[key] = window
        return True


# ------------------------------------------------------------- formats --


def base_type(mime: str) -> str:
    return (mime or "").split(";")[0].strip().lower()


def extension_for(mime: str) -> Optional[str]:
    return _EXTENSIONS.get(base_type(mime))


def _magic_ok(ext: str, head: bytes) -> bool:
    """Whether part 0 opens with the declared container's signature. Nothing
    after a wrong opening could decode, so it is refused at once."""
    if ext == "webm":
        return head[:4] == b"\x1a\x45\xdf\xa3"
    if ext in ("mp4", "m4a", "3gp"):
        return head[4:8] == b"ftyp"
    if ext in ("ogg", "opus"):
        return head[:4] == b"OggS"
    if ext == "wav":
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if ext == "flac":
        return head[:4] == b"fLaC"
    return True  # mp3 and ADTS have no fixed opening worth trusting


def part_limit_bytes() -> int:
    """The largest part this deployment accepts.

    VOICE_PART_MAX_BYTES, but never above the orchestrator's DEFAULT request
    body cap (app/main.body_cap_for; 1 MiB unless MAX_REQUEST_BODY_BYTES says
    otherwise). A part route has no larger family in that table, so a bigger
    part would be refused by the middleware before this code saw it, and the
    browser is told the real number instead. At the measured 128.7 kb/s a
    1 MiB part is about 65 s of audio, which is what a catch-up after an
    outage concatenates."""
    cap = getattr(settings, "max_request_body_bytes", None)
    if not cap:
        try:
            cap = int(os.environ.get("MAX_REQUEST_BODY_BYTES") or 0)
        except ValueError:
            cap = 0
    cap = int(cap) if cap and int(cap) > 0 else 1024 * 1024
    return max(1024, min(int(settings.voice_part_max_bytes), cap))


# ---------------------------------------------------------- the decoder --


class _Undecodable(Exception):
    """The bytes are not audio this server can read."""


class _WavPassthrough:
    """16 kHz mono 16-bit WAV straight through, without ffmpeg.

    A RIFF header around exactly the samples the pipeline wants is not worth
    a subprocess, and it is what the CI host (which has no ffmpeg) sends. Any
    other WAV raises `_Undecodable` from `feed`, and the caller switches to
    ffmpeg with the bytes seen so far."""

    progressive_after_bytes = 0

    def __init__(self, on_pcm: Callable[[bytes], None]) -> None:
        self._on_pcm = on_pcm
        self._head = b""
        self._in_data = False
        self._odd = b""

    async def start(self) -> None:
        return None

    async def feed(self, data: bytes) -> None:
        if self._in_data:
            self._emit(data)
            return
        self._head += data
        buf = self._head
        if len(buf) < 12:
            return
        if buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
            raise _Undecodable("not a RIFF/WAVE file")
        offset = 12
        fmt_ok = False
        while offset + 8 <= len(buf):
            tag = buf[offset:offset + 4]
            size = int.from_bytes(buf[offset + 4:offset + 8], "little")
            body = offset + 8
            if tag == b"fmt ":
                if body + 16 > len(buf):
                    return
                fmt_tag = int.from_bytes(buf[body:body + 2], "little")
                channels = int.from_bytes(buf[body + 2:body + 4], "little")
                rate = int.from_bytes(buf[body + 4:body + 8], "little")
                bits = int.from_bytes(buf[body + 14:body + 16], "little")
                if (fmt_tag, channels, rate, bits) != (1, 1, SAMPLE_RATE, 16):
                    raise _Undecodable("not 16 kHz mono 16-bit PCM")
                fmt_ok = True
                offset = body + size + (size % 2)
                continue
            if tag == b"data":
                if not fmt_ok:
                    raise _Undecodable("WAV data before its format")
                self._in_data = True
                rest = buf[body:]
                self._head = b""
                self._emit(rest)
                return
            if body + size + (size % 2) > len(buf):
                return  # a chunk we skip, not fully arrived yet
            offset = body + size + (size % 2)

    def _emit(self, data: bytes) -> None:
        data = self._odd + data
        cut = len(data) - (len(data) % 2)
        self._odd = data[cut:]
        if cut:
            self._on_pcm(data[:cut])

    async def close(self) -> Optional[str]:
        return None if self._in_data else "no audio data"

    async def kill(self) -> None:
        return None


class _FfmpegStream:
    """ONE ffmpeg for the life of a session: the container stream on stdin as
    it arrives, 16 kHz mono s16le on stdout as it decodes.

    The argv is publicapi/audio_jobs.Decoder's (nice 19, ionice idle, one
    thread, the input format and protocol allowlists), reading `pipe:0` and
    writing `pipe:1`, plus `aresample=async=1` so a PCM sample index stays
    container time across timestamp gaps. Measured here with ffmpeg 7.0.2: a
    live Chrome WebM/Opus stream fed 80 KB (5 s) at a time decoded 4.9, 9.9,
    14.9 and 19.8 s of PCM after parts 1-4, and a fragmented MP4/AAC stream
    4.6, 9.4, 14.1 and 18.8 s: both containers transcribe while they record."""

    #: Fed this much with no PCM back, the container is not decoding while it
    #: is still being written; the transcript then arrives after Stop.
    progressive_after_bytes = 512 * 1024
    #: The wait for ffmpeg to flush and exit once the input has ended.
    drain_timeout_s = 120.0

    def __init__(self, on_pcm: Callable[[bytes], None]) -> None:
        self._on_pcm = on_pcm
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.Task] = None
        self._stderr: Optional[asyncio.Task] = None
        self._dead = False

    @staticmethod
    def binary() -> Optional[str]:
        return shutil.which("ffmpeg")

    def argv(self) -> List[str]:
        prefix: List[str] = []
        if shutil.which("nice") and shutil.which("ionice"):
            prefix = list(audio_jobs.NICE_PREFIX)
        return [
            *prefix,
            self.binary() or "ffmpeg",
            "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1",
            *audio_jobs.input_args(pipe=True),
            "-i", "pipe:0",
            "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-af", "aresample=async=1",
            "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1",
        ]

    async def start(self) -> None:
        if self.binary() is None:
            raise _Undecodable("this server has no audio decoder (ffmpeg is not installed)")
        self._proc = await asyncio.create_subprocess_exec(
            *self.argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader = asyncio.get_running_loop().create_task(self._read())
        self._stderr = asyncio.get_running_loop().create_task(self._proc.stderr.read(65536))

    async def _read(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        odd = b""
        while True:
            chunk = await self._proc.stdout.read(64 * 1024)
            if not chunk:
                break
            chunk = odd + chunk
            cut = len(chunk) - (len(chunk) % 2)
            odd = chunk[cut:]
            if cut:
                self._on_pcm(chunk[:cut])

    async def feed(self, data: bytes) -> None:
        if self._dead or self._proc is None or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # ffmpeg stopped (a corrupt stream). What it decoded is kept;
            # `close` reports why.
            self._dead = True

    async def close(self) -> Optional[str]:
        """End of input; wait for the last PCM. Returns ffmpeg's complaint, if any."""
        if self._proc is None:
            return "the decoder never started"
        if self._proc.stdin is not None and not self._proc.stdin.is_closing():
            with contextlib.suppress(Exception):
                self._proc.stdin.close()
        try:
            # After end of input ffmpeg only flushes what it holds; a decoder
            # that has not exited in this long is stuck, not busy.
            async with asyncio.timeout(self.drain_timeout_s):
                if self._reader is not None:
                    await self._reader
                code = await self._proc.wait()
        except TimeoutError:
            await self.kill()
            return "the decoder did not finish after the recording ended"
        err = b""
        if self._stderr is not None:
            with contextlib.suppress(Exception):
                err = await self._stderr
        if code != 0:
            return (err.decode("utf-8", "replace").strip()[-300:] or f"ffmpeg exited {code}")
        return None

    async def kill(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            with contextlib.suppress(Exception):
                await self._proc.wait()
        for task in (self._reader, self._stderr):
            if task is not None and not task.done():
                task.cancel()


# ---------------------------------------------------------- the planner --


def _speech_s(window: vad.Window) -> float:
    return sum(b - a for a, b in window.regions)


class Planner:
    """Voice activity -> committed transcription windows, while the audio is
    still arriving: a thin incremental wrapper around video/vad.py.

    Flags come from `vad.frame_flags` (webrtcvad when installed, as in
    production and CI; the energy detector otherwise), regions from
    `vad.regions_from_flags`, windows from `vad.windows_from_regions` with the
    video pipeline's own 1.2 s gap and 3 s overlap. What this adds is WHEN a
    window may be sent:

      * A region is FINAL once more than max_gap + the detector's 0.2 s
        padding of non-speech follows it: no later speech can join its window.
      * A window of final regions is COMMITTED when it holds at least
        VOICE_SESSION_WINDOW_MIN_S of speech, when a pause of at least
        VOICE_SESSION_FLUSH_PAUSE_S follows it, or when the window after it
        could not be packed with it. A shorter one with speech close behind is
        packed with that speech (up to the 30 s ceiling): fewer, longer clips,
        because every clip costs the engine a whole 30 s encoder pass.
      * Continuous speech longer than the ceiling is cut into the planner's
        overlapping pieces; every piece but the growing last one is final.
      * When the recording ends, everything left is committed.

    A stretch with no speech produces no window at all, so a quiet opening,
    however long, suppresses nothing after it.
    """

    #: How much earlier audio the energy detector sees when it judges a new
    #: chunk: its floor is a percentile of the recording, not of 5 s.
    _ENERGY_CONTEXT_S = 60.0

    def __init__(self, *, cursor_s: float = 0.0, next_overlaps: bool = False, skip_until_s: float = 0.0) -> None:
        self.max_s = float(settings.voice_session_window_max_s)
        self.min_s = float(settings.voice_session_window_min_s)
        self.flush_s = float(settings.voice_session_flush_pause_s)
        self.gap_s = float(settings.video_asr_max_gap_s)
        self.overlap_s = float(settings.video_asr_overlap_s)
        self.cursor_s = float(cursor_s)
        self.next_overlaps = bool(next_overlaps)
        #: True once everything has been planned (the recording ended, or a
        #: retranscription reuses a finished plan): no more windows.
        self.closed = False
        #: Frames before this were planned by an earlier worker; after a
        #: restart they are decoded again but need no voice-activity pass.
        #: The detector starts 10 s before the cursor, so it has adapted to
        #: the recording by the time its flags matter again.
        self._skip_frames = int(max(0.0, skip_until_s - 10.0) / FRAME_S)
        self._vad: Any = None
        self._flags = bytearray()
        self._base = 0  # frame index of _flags[0]
        self._pending = b""
        self._frames = 0  # frames flagged (or skipped) so far
        self._energy_tail = b""
        self.detector: Optional[str] = None
        self.speech_counted_to = float(cursor_s)
        self.speech_s = 0.0
        #: Where uncommitted speech begins (or the end of the decoded audio):
        #: everything before it is committed to a window or known silence.
        self.pending_start = float(cursor_s)

    @property
    def decoded_s(self) -> float:
        return self._frames * FRAME_S

    def push(self, pcm: bytes) -> None:
        import numpy as np

        data = self._pending + pcm
        step = vad.FRAME_SAMPLES * 2
        whole = len(data) - (len(data) % step)
        self._pending = data[whole:]
        if not whole:
            return
        count = whole // step
        skip = min(count, max(0, self._skip_frames - self._frames))
        if skip:
            self._flags.extend(b"\x00" * skip)
            self._frames += skip
        fresh = data[skip * step:whole]
        if fresh:
            if _has_webrtcvad():
                flags, detector = self._webrtc(fresh), "webrtcvad"
            else:
                # The energy detector's floor is a percentile of the audio it
                # is given; judged on 5 s alone, continuous speech would set
                # its own floor. It judges the new frames with up to a minute
                # of the audio before them, and only the new frames are kept.
                context = np.frombuffer(self._energy_tail + fresh, dtype="<i2")
                all_flags, detector = vad.frame_flags(context)
                new = len(fresh) // step
                flags = all_flags[len(all_flags) - new:] if new else []
                keep = int(self._ENERGY_CONTEXT_S * PCM_BYTES_PER_S)
                self._energy_tail = (self._energy_tail + fresh)[-keep:]
            self.detector = self.detector or detector
            self._flags.extend(1 if f else 0 for f in flags)
            self._frames += len(flags)
        self._trim()

    def _webrtc(self, pcm: bytes) -> List[bool]:
        """vad._frames_speech_webrtc's detector, with ONE instance kept for the
        whole recording. webrtcvad adapts to the audio it has heard; a fresh
        instance per 5 s part (which calling vad.frame_flags per part gives)
        misjudged the start of a word that straddled a part boundary, a window
        closed inside it, and the word came back cut ("pojat" as "p" at 4,015 s
        of the two-hour test, 2026-09-29). One instance fed every frame in order
        gives the flags one pass over the whole recording would."""
        import webrtcvad

        if self._vad is None:
            self._vad = webrtcvad.Vad(2)
        step = vad.FRAME_SAMPLES * 2
        return [bool(self._vad.is_speech(pcm[off:off + step], SAMPLE_RATE)) for off in range(0, len(pcm) - step + 1, step)]

    def _trim(self) -> None:
        """Drop flags well behind the cursor: memory stays flat in duration."""
        keep_from = int(max(0.0, self.cursor_s - 5.0) / FRAME_S)
        if keep_from - self._base > 20000:
            del self._flags[: keep_from - self._base]
            self._base = keep_from

    def _regions(self) -> List[Tuple[float, float]]:
        first = max(self._base, int(self.cursor_s / FRAME_S))
        sub = self._flags[first - self._base:]
        if not sub:
            return []
        rel = vad.regions_from_flags([bool(f) for f in sub], total_s=len(sub) * FRAME_S)
        origin = first * FRAME_S
        out = []
        for a, b in rel:
            a, b = max(self.cursor_s, origin + a), origin + b
            if b > a:
                out.append((a, b))
        return out

    def plan(self, *, finishing: bool) -> List[Dict[str, Any]]:
        """Newly committed windows, in order, as plain records; the cursor moves
        past them."""
        if self.closed:
            return []
        total = self.decoded_s
        regions = self._regions()
        if not regions:
            # Nothing but silence since the cursor: move it on, keeping enough
            # behind that speech starting now still opens its own region.
            margin = self.gap_s + 1.0
            if total - margin > self.cursor_s:
                self.cursor_s = total - margin
                self.next_overlaps = False
            self.pending_start = total
            self._trim()
            return []
        last = regions[-1]
        open_region = None
        if not finishing and total - last[1] <= self.gap_s + 0.25:
            open_region = last
        plan = vad.windows_from_regions(
            regions, max_window_s=self.max_s, max_gap_s=self.gap_s, overlap_s=self.overlap_s
        )
        if plan and self.next_overlaps and not plan[0].overlaps_previous:
            first = plan[0]
            plan[0] = vad.Window(first.start_s, first.end_s, True, regions=first.regions)
        # Pack a short window with the speech right after it.
        i = 0
        while i + 1 < len(plan):
            w, nxt = plan[i], plan[i + 1]
            if (
                not nxt.overlaps_previous
                and _speech_s(w) < self.min_s
                and nxt.start_s - w.end_s < self.flush_s
                and nxt.end_s - w.start_s <= self.max_s
            ):
                plan[i] = vad.Window(
                    w.start_s, nxt.end_s, w.overlaps_previous, regions=tuple(w.regions) + tuple(nxt.regions)
                )
                del plan[i + 1]
                continue
            i += 1
        committed: List[Dict[str, Any]] = []
        self.pending_start = total
        for idx, w in enumerate(plan):
            later = plan[idx + 1] if idx + 1 < len(plan) else None
            holds_open = open_region is not None and w.end_s > open_region[0]
            piece = holds_open and later is not None and later.overlaps_previous
            if not (finishing or piece):
                if holds_open:
                    self.pending_start = w.start_s
                    break
                after = later.start_s if later is not None else total
                if not (_speech_s(w) >= self.min_s or after - w.end_s >= self.flush_s or later is not None):
                    self.pending_start = w.start_s
                    break
            next_overlaps = bool(later is not None and later.overlaps_previous)
            committed.append({
                "start_s": round(w.start_s, 3),
                "end_s": round(w.end_s, 3),
                "overlaps_previous": bool(w.overlaps_previous),
                "regions": [[round(a, 3), round(b, 3)] for a, b in w.regions],
                "next_overlaps": next_overlaps,
                "next_start_s": round(later.start_s, 3) if next_overlaps and later is not None else None,
            })
            for a, b in w.regions:
                a = max(a, self.speech_counted_to)
                if b > a:
                    self.speech_s += b - a
                self.speech_counted_to = max(self.speech_counted_to, b)
            if next_overlaps and later is not None:
                self.cursor_s = later.start_s
                self.next_overlaps = True
            else:
                self.cursor_s = w.end_s
                self.next_overlaps = False
        if committed:
            committed[-1]["cursor_s"] = round(self.cursor_s, 3)
            committed[-1]["cursor_next_overlaps"] = self.next_overlaps
        self._trim()
        return committed


_WEBRTCVAD: Optional[bool] = None


def _has_webrtcvad() -> bool:
    global _WEBRTCVAD
    if _WEBRTCVAD is None:
        import importlib.util

        _WEBRTCVAD = importlib.util.find_spec("webrtcvad") is not None
    return _WEBRTCVAD


# ---------------------------------------------------------- the stitcher --


class SessionStitcher(audio_jobs.Stitcher):
    """publicapi/audio_jobs.Stitcher over an APPEND-ONLY window list.

    The public API knows its whole plan before the first window; a session
    learns its windows as the person talks. The one thing `feed` reads from
    the future is whether the NEXT window overlaps this one (to hold back
    every cue its overlap could trim), and the planner knows that when it
    commits a window. So each window is fed with a placeholder for its
    successor when there is an overlap, and the parent's seam alignment,
    2-cue holdback and loop collapse run unchanged."""

    def __init__(self) -> None:
        super().__init__([])

    def feed_window(
        self,
        window: vad.Window,
        segments: Sequence[Dict[str, Any]],
        *,
        next_start_s: Optional[float],
    ) -> List[Segment]:
        index = len(self.windows)
        self.windows.append(window)
        placeholder = next_start_s is not None
        if placeholder:
            self.windows.append(vad.Window(float(next_start_s), window.end_s + 1.0, True))
        try:
            return self.feed(index, segments)
        finally:
            if placeholder:
                self.windows.pop()

    def held_text(self) -> str:
        return " ".join(cue.text for cue in self._held)


_UNSPACED = re.compile(
    r"[฀-໿က-႟ក-៿ༀ-࿿぀-ヿ㐀-䶿一-鿿豈-﫿]"
)


def join_texts(parts: Sequence[str]) -> str:
    """Segments -> one string. A space between two cues, except between two
    characters of a script written without spaces (asr._UNSPACED's ranges),
    where a space would be a wrong character in the person's text."""
    out = ""
    for part in parts:
        part = (part or "").strip()
        if not part:
            continue
        if out and not (_UNSPACED.match(out[-1]) and _UNSPACED.match(part[0])):
            out += " "
        out += part
    return out


_LANGUAGE_NAMES = {code: name for name, code in asr._LANGUAGE_CODES.items()}


# ------------------------------------------------------------ the runner --


class _Runner:
    """One daemon thread running one event loop for every session worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self.live: Dict[str, "_Live"] = {}
        self._maintenance: Optional[concurrent.futures.Future] = None

    def loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            ready = threading.Event()
            holder: Dict[str, Any] = {}

            def run() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                holder["loop"] = loop
                ready.set()
                loop.run_forever()

            thread = threading.Thread(target=run, name="voice-sessions", daemon=True)
            thread.start()
            ready.wait()
            self._loop, self._thread = holder["loop"], thread
            return self._loop

    def submit(self, coro: Any) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop())

    def call(self, fn: Callable[..., Any], *args: Any) -> None:
        self.loop().call_soon_threadsafe(fn, *args)

    def get(self, session_id: str) -> Optional["_Live"]:
        return self.live.get(session_id)

    def ensure(self, session_id: str) -> None:
        """Run (or adopt) this session's worker in this process, if its lease
        is free. Safe from any thread; idempotent."""
        self.ensure_maintenance()
        self.call(self._ensure_in_loop, session_id)

    def _ensure_in_loop(self, session_id: str) -> None:
        if session_id in self.live:
            return
        live = _Live(session_id)
        self.live[session_id] = live
        live.task = asyncio.get_running_loop().create_task(live.run(), name=f"voice-session-{session_id[:8]}")

    def notify(self, session_id: str, **fields: Any) -> None:
        live = self.live.get(session_id)
        if live is not None:
            self.call(live.poke, fields)
        else:
            self.ensure(session_id)

    def ensure_maintenance(self) -> None:
        with self._lock:
            running = self._maintenance is not None and not self._maintenance.done()
        if running:
            return
        future = asyncio.run_coroutine_threadsafe(_maintain(), self.loop())
        with self._lock:
            self._maintenance = future

    def stop_all(self, timeout: float = 20.0) -> None:
        """Cancel every worker and the maintenance loop (tests; a shutdown)."""
        if self._loop is None:
            return

        async def _stop() -> None:
            tasks = [live.task for live in list(self.live.values()) if live.task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.live.clear()
            asr.SESSION_GATE.reset_for_tests()

        with self._lock:
            maintenance, self._maintenance = self._maintenance, None
        if maintenance is not None:
            maintenance.cancel()
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_stop(), self._loop).result(timeout)

    def wait_idle(self, session_id: str, timeout: float) -> bool:
        """Block until this session's worker has ended (tests, measurement)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if session_id not in self.live:
                return True
            time.sleep(0.02)
        return False


RUNNER = _Runner()


# ------------------------------------------------------------ the worker --


@dataclass
class _WindowState:
    i: int
    start_s: float
    end_s: float
    overlaps_previous: bool
    regions: Tuple[Tuple[float, float], ...]
    next_start_s: Optional[float]
    status: str = "pending"  # pending | done | failed | dropped
    segments: List[Dict[str, Any]] = field(default_factory=list)
    language: Optional[str] = None
    low: bool = False
    reason: Optional[str] = None
    engine_ms: int = 0
    dropped: List[Dict[str, Any]] = field(default_factory=list)

    def window(self) -> vad.Window:
        return vad.Window(self.start_s, self.end_s, self.overlaps_previous, regions=self.regions)


def _speech_of(windows: Sequence[Any]) -> float:
    """Seconds of detected speech across committed windows, the overlap of a
    continuous-speech split counted once."""
    total, counted_to = 0.0, 0.0
    for w in windows:
        for a, b in w.regions:
            a = max(float(a), counted_to)
            if b > a:
                total += float(b) - a
            counted_to = max(counted_to, float(b))
    return total


class _Live:
    """One session's worker, on the runner loop.

    Two coroutines share it: INGEST (stored bytes -> decoder -> PCM ->
    planner -> committed windows) and DISPATCH (committed windows, in order,
    -> the session gate -> the engine -> the stitcher). Request threads read
    it only through `snapshot`, under `lock`."""

    def __init__(self, session_id: str) -> None:
        self.id = session_id
        self.task: Optional[asyncio.Task] = None
        self.row: Dict[str, Any] = {}
        self.lock = threading.Lock()
        self.ingest_kick = asyncio.Event()
        self.dispatch_kick = asyncio.Event()
        self.rev = 0
        self.status = STATUS_RECORDING
        self.bytes_stored = 0
        self.next_part = 0
        self.fed = 0
        self.pcm_bytes = 0
        self.pcm_file: Optional[Any] = None
        self.planner: Optional[Planner] = None
        self.windows: List[_WindowState] = []
        self.stitched = 0  # windows fed to the stitcher, in order
        self.stitcher = SessionStitcher()
        self.fed_windows: List[Tuple[float, float, bool]] = []
        self.segments: List[Dict[str, Any]] = []
        self.tentative = ""
        self.waiting_on = "none"
        self.progressive = True
        self.ingest_done = False
        self.decode_error: Optional[str] = None
        self.started = False
        self._last_row_write = 0.0
        self._last_row_read = time.monotonic()
        self._last_transcript_write = 0.0
        self._decoder: Any = None
        self.engine_ms = 0
        self.paced_s = 0.0

    # -- state shared with request threads --------------------------------

    def current_rev(self) -> int:
        with self.lock:
            return self.rev

    def bump(self) -> None:
        with self.lock:
            self.rev += 1

    def _kick(self) -> None:
        self.ingest_kick.set()
        self.dispatch_kick.set()

    def poke(self, fields: Dict[str, Any]) -> None:
        """A request in this process changed the row (a part, a finish, a
        discard). Runs on the runner loop."""
        with self.lock:
            if "bytes_stored" in fields:
                self.bytes_stored = max(self.bytes_stored, int(fields["bytes_stored"]))
            if "next_part" in fields:
                self.next_part = max(self.next_part, int(fields["next_part"]))
            if "rev" in fields:
                self.rev = max(self.rev, int(fields["rev"]))
            if fields.get("status") == STATUS_FINISHING and self.status == STATUS_RECORDING:
                self.status = STATUS_FINISHING
            if fields.get("status") == STATUS_CANCELLED:
                self.status = STATUS_CANCELLED
            self.rev += 1
        self._kick()

    def audio_ms(self) -> int:
        return int(self.pcm_bytes * 1000 // PCM_BYTES_PER_S)

    def _transcribed_ms_locked(self) -> int:
        """Audio before this point is transcribed or known to be silence."""
        marks = [w.start_s for w in self.windows[self.stitched:] if w.status == "pending"]
        if self.planner is not None and not self.ingest_done:
            marks.append(self.planner.pending_start)
        audio = self.audio_ms()
        if not marks:
            return audio
        return max(0, min(audio, int(min(marks) * 1000)))

    def _gaps_locked(self) -> List[Dict[str, Any]]:
        return [
            {"start_ms": int(w.start_s * 1000), "end_ms": int(w.end_s * 1000), "reason": w.reason}
            for w in self.windows
            if w.status in ("failed", "dropped")
        ]

    def snapshot(self, cursor: int = 0) -> Dict[str, Any]:
        with self.lock:
            cursor = max(0, int(cursor))
            return {
                "rev": self.rev,
                "status": self.status,
                "next_part": self.next_part,
                "bytes_stored": self.bytes_stored,
                "audio_ms": self.audio_ms(),
                "transcribed_ms": self._transcribed_ms_locked(),
                "waiting_on": self.waiting_on,
                "progressive": self.progressive,
                "count": len(self.segments),
                "segments": self.segments[cursor:],
                "tentative": self.tentative,
                "gaps": self._gaps_locked(),
                "speech_ms": int((self.planner.speech_s if self.planner else 0.0) * 1000),
            }

    # -- the run ----------------------------------------------------------

    async def run(self) -> None:
        renew: Optional[asyncio.Task] = None
        try:
            row = await db.run_in_thread(_claim_lease, self.id)
            if row is None:
                return  # another process holds it, or it is no longer live
            self.row = row
            with self.lock:
                self.status = row["status"]
                self.bytes_stored = int(row["bytes_stored"] or 0)
                self.next_part = int(row["next_part"] or 0)
                self.rev = max(self.rev, int(row["rev"] or 0))
            self.started = True
            renew = asyncio.get_running_loop().create_task(self._renew())
            metrics.set_gauge("voice_sessions_live", len(RUNNER.live), "recording sessions with a worker in this process")
            await self._restore()
            ingest = asyncio.get_running_loop().create_task(self._ingest())
            try:
                await self._dispatch()
            finally:
                if not ingest.done():
                    ingest.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ingest
            if self.status == STATUS_CANCELLED:
                return
            await self._finalize()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if self.status != STATUS_CANCELLED:
                log.exception("voice session %s failed unexpectedly", self.id)
                with contextlib.suppress(Exception):
                    await db.run_in_thread(
                        _update, self.id, only_live=True, status=STATUS_FAILED, finished_at=_now(),
                        error={
                            "reason": "internal",
                            "detail": f"The server failed while transcribing this recording ({type(exc).__name__}).",
                        },
                        rev=self.current_rev() + 1,
                    )
        finally:
            if renew is not None:
                renew.cancel()
            await self.shutdown()
            RUNNER.live.pop(self.id, None)
            if self.started:
                with contextlib.suppress(Exception):
                    await db.run_in_thread(_release_lease, self.id)
            metrics.set_gauge("voice_sessions_live", len(RUNNER.live), "recording sessions with a worker in this process")

    async def shutdown(self) -> None:
        if self._decoder is not None:
            with contextlib.suppress(Exception):
                await self._decoder.kill()
            self._decoder = None
        if self.pcm_file is not None:
            with contextlib.suppress(Exception):
                self.pcm_file.close()
            self.pcm_file = None

    async def _renew(self) -> None:
        period = max(1.0, settings.voice_session_lease_ttl_s / 3.0)
        while True:
            await asyncio.sleep(period)
            row = await db.run_in_thread(_claim_lease, self.id)
            if row is not None:
                continue
            current = await db.run_in_thread(_row, self.id)
            if current is None or current["status"] == STATUS_CANCELLED:
                self.poke({"status": STATUS_CANCELLED})
            elif current["status"] in LIVE_STATUSES and self.task is not None:
                # Another process holds it now: stop without finishing anything.
                log.warning("voice session %s: lease lost to another process", self.id)
                self.task.cancel()
            return

    def _p(self, name: str) -> str:
        return _path(self.row["user_id"], self.id, name)

    async def _restore(self) -> None:
        """Pick up whatever an earlier worker (this process, or one that died)
        left: the committed plan and the finished windows. The source is cut
        back to what the row acknowledged, and the PCM, which is derived, is
        decoded again from byte 0."""
        src = source_path(self.row)
        with contextlib.suppress(OSError):
            if os.path.getsize(src) > self.bytes_stored:
                # A crash between the append and the row update: the file is
                # longer than anything acknowledged, the safe direction.
                with open(src, "r+b") as fh:
                    fh.truncate(self.bytes_stored)
        retranscribe = self.row.get("retranscribe")
        results: Dict[int, Dict[str, Any]] = {}
        for line in _read_lines(self._p("results.jsonl")):
            results[int(line.get("i", -1))] = line
        cursor, next_overlaps, closed = 0.0, False, False
        for line in _read_lines(self._p("plan.jsonl")):
            if line.get("final_plan"):
                closed = True
                continue
            w = _WindowState(
                i=int(line["i"]),
                start_s=float(line["start_s"]),
                end_s=float(line["end_s"]),
                overlaps_previous=bool(line.get("overlaps_previous")),
                regions=tuple((float(a), float(b)) for a, b in line.get("regions") or ()),
                next_start_s=(float(line["next_start_s"]) if line.get("next_start_s") is not None else None),
            )
            result = results.get(w.i)
            redo = retranscribe == "all" or (retranscribe == "gaps" and (result or {}).get("status") == "failed")
            if result is not None and not redo:
                w.status = str(result.get("status") or "done")
                w.segments = list(result.get("segments") or [])
                w.language = result.get("language")
                w.low = bool(result.get("low"))
                w.reason = result.get("reason")
                w.engine_ms = int(result.get("engine_ms") or 0)
            self.windows.append(w)
            if line.get("cursor_s") is not None:
                cursor = float(line["cursor_s"])
                next_overlaps = bool(line.get("cursor_next_overlaps"))
        self.planner = Planner(cursor_s=cursor, next_overlaps=next_overlaps, skip_until_s=cursor)
        self.planner.closed = closed
        self.planner.speech_s = _speech_of(self.windows)
        self.planner.speech_counted_to = max([w.end_s for w in self.windows] or [0.0])
        pcm = self._p("audio.pcm")
        fd = os.open(pcm, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        self.pcm_file = os.fdopen(fd, "wb", buffering=0)
        if retranscribe:
            await db.run_in_thread(_update, self.id, retranscribe=None, bump=False)
        self.bump()

    # -- ingest: stored bytes -> PCM -> planned windows --------------------

    def _on_pcm(self, data: bytes) -> None:
        if self.pcm_file is not None:
            self.pcm_file.write(data)
        with self.lock:
            self.pcm_bytes += len(data)
        if self.planner is not None and not self.planner.closed:
            self.planner.push(data)
            self._commit(self.planner.plan(finishing=False))
        self.dispatch_kick.set()

    def _commit(self, records: List[Dict[str, Any]], *, final: bool = False) -> None:
        for record in records:
            i = len(self.windows)
            _append_line(self._p("plan.jsonl"), {"i": i, **record})
            with self.lock:
                self.windows.append(
                    _WindowState(
                        i=i,
                        start_s=record["start_s"],
                        end_s=record["end_s"],
                        overlaps_previous=record["overlaps_previous"],
                        regions=tuple((a, b) for a, b in record["regions"]),
                        next_start_s=record["next_start_s"],
                    )
                )
        if final and self.planner is not None:
            _append_line(self._p("plan.jsonl"), {"final_plan": True})
            self.planner.closed = True
        if records or final:
            self.bump()
            self.dispatch_kick.set()

    async def _open_decoder(self, head: bytes) -> Any:
        if self.row["ext"] == "wav" and head[:4] == b"RIFF":
            return _WavPassthrough(self._on_pcm)
        decoder = _FfmpegStream(self._on_pcm)
        await decoder.start()
        return decoder

    async def _ingest(self) -> None:
        src = source_path(self.row)
        fed_since_pcm = 0
        try:
            while self.status != STATUS_CANCELLED:
                if self.fed < self.bytes_stored:
                    with open(src, "rb") as fh:
                        fh.seek(self.fed)
                        chunk = fh.read(min(1024 * 1024, self.bytes_stored - self.fed))
                    if not chunk:
                        await asyncio.sleep(0.2)
                        continue
                    if self._decoder is None:
                        self._decoder = await self._open_decoder(chunk)
                    before = self.pcm_bytes
                    try:
                        await self._decoder.feed(chunk)
                    except _Undecodable:
                        if not isinstance(self._decoder, _WavPassthrough):
                            raise
                        # Not the WAV the passthrough reads: ffmpeg gets the
                        # recording from its first byte instead.
                        self._decoder = _FfmpegStream(self._on_pcm)
                        await self._decoder.start()
                        self.fed = 0
                        continue
                    self.fed += len(chunk)
                    fed_since_pcm = 0 if self.pcm_bytes > before else fed_since_pcm + len(chunk)
                    threshold = getattr(self._decoder, "progressive_after_bytes", 0)
                    if threshold and self.pcm_bytes == 0 and fed_since_pcm > threshold and self.progressive:
                        with self.lock:
                            self.progressive = False
                            self.rev += 1
                    await asyncio.sleep(0)  # let the decoder's reader run
                    continue
                if self.status == STATUS_FINISHING:
                    await self._refresh(force=True)
                    if self.fed >= self.bytes_stored and self.status == STATUS_FINISHING:
                        break
                    continue
                await self._wait(self.ingest_kick, 2.0)
                await self._refresh()
                await self._persist()
            if self.status == STATUS_CANCELLED:
                return
            if self._decoder is not None:
                self.decode_error = await self._decoder.close()
                self._decoder = None
            # No bytes at all (the microphone was never allowed, the tab went
            # away before the first part) is a recording with no speech in
            # it, not an undecodable one.
        except _Undecodable as exc:
            self.decode_error = str(exc)
            if self._decoder is not None:
                await self._decoder.kill()
                self._decoder = None
        if self.planner is not None and not self.planner.closed:
            self._commit(self.planner.plan(finishing=True), final=True)
        with self.lock:
            self.ingest_done = True
            self.rev += 1
        self.dispatch_kick.set()

    @staticmethod
    async def _wait(event: asyncio.Event, timeout: float) -> None:
        try:
            async with asyncio.timeout(timeout):
                await event.wait()
        except TimeoutError:
            pass
        finally:
            event.clear()

    async def _refresh(self, *, force: bool = False) -> None:
        """Re-read the row: a part or a finish may have landed in another
        process (a rolling recreate), and a DELETE anywhere must stop us."""
        if not force and time.monotonic() - self._last_row_read < 2.0:
            return
        self._last_row_read = time.monotonic()
        row = await db.run_in_thread(_row, self.id)
        if row is None or row["status"] == STATUS_CANCELLED:
            self.poke({"status": STATUS_CANCELLED})
            return
        self.row.update(row)
        with self.lock:
            self.bytes_stored = max(self.bytes_stored, int(row["bytes_stored"] or 0))
            self.next_part = max(self.next_part, int(row["next_part"] or 0))
            self.rev = max(self.rev, int(row["rev"] or 0))
            if row["status"] == STATUS_FINISHING and self.status == STATUS_RECORDING:
                self.status = STATUS_FINISHING

    async def _persist(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_row_write < _ROW_WRITE_EVERY_S:
            return
        self._last_row_write = now
        snap = self.snapshot(cursor=1 << 30)
        await db.run_in_thread(
            _update,
            self.id,
            only_live=True,
            audio_ms=snap["audio_ms"],
            transcribed_ms=snap["transcribed_ms"],
            speech_ms=snap["speech_ms"],
            windows_total=len(self.windows),
            windows_done=sum(1 for w in self.windows if w.status in ("done", "dropped")),
            windows_failed=sum(1 for w in self.windows if w.status == "failed"),
            rev=snap["rev"],
        )
        if self.status == STATUS_RECORDING and now - self._last_transcript_write >= 30.0:
            # For a reader in another process (a rolling recreate). Bounded:
            # rewriting a two-hour transcript every part would be O(n^2).
            self._last_transcript_write = now
            self._write_transcript(final=False)

    # -- dispatch: planned windows -> the engine -> the stitcher -----------

    async def _dispatch(self) -> None:
        while self.status != STATUS_CANCELLED:
            if self.stitched >= len(self.windows):
                if self.ingest_done and self.stitched >= len(self.windows):
                    return
                await self._wait(self.dispatch_kick, 2.0)
                continue
            w = self.windows[self.stitched]
            if w.status == "pending":
                covered = self.pcm_bytes >= int(w.end_s * SAMPLE_RATE) * 2
                if not covered and not self.ingest_done:
                    await self._wait(self.dispatch_kick, 1.0)
                    continue
                await self._transcribe(w)
                if self.status == STATUS_CANCELLED:
                    return
            self._stitch(w)
            await self._persist()

    async def _pace(self) -> float:
        """Wait while a chat answer is streaming, up to the class's limit:
        VOICE_SESSION_PACE_LIVE_S while the person talks (their text is a
        preview), VOICE_SESSION_PACE_FINISH_S once they pressed Stop. The
        wait happens BEFORE the gate, so a paced window never holds the
        slot idle. The probe is main.py's, installed on the video pipeline."""
        from .video import pipeline as video_pipeline

        probe = getattr(video_pipeline, "_busy_probe", None)
        waited = 0.0
        while probe is not None:
            limit = (
                settings.voice_session_pace_live_s if self.status == STATUS_RECORDING
                else settings.voice_session_pace_finish_s
            )
            if waited >= limit:
                break
            try:
                busy = bool(probe())
            except Exception:  # noqa: BLE001 — the probe is advisory
                busy = False
            if not busy:
                break
            self._set_waiting("chat")
            await asyncio.sleep(1.0)
            waited += 1.0
        if waited:
            metrics.observe("voice_session_pace_seconds", waited, "seconds a session window waited for chat")
        self.paced_s += waited
        return waited

    def _set_waiting(self, value: str) -> None:
        with self.lock:
            if self.waiting_on != value:
                self.waiting_on = value
                self.rev += 1

    def _clip(self, w: _WindowState) -> bytes:
        """The window as a WAV built in memory: 960,044 bytes for 30 s, under
        the engine's 1,048,576-byte multipart spool threshold."""
        import numpy as np

        a = int(w.start_s * SAMPLE_RATE) * 2
        b = int(w.end_s * SAMPLE_RATE) * 2
        with open(self._p("audio.pcm"), "rb") as fh:
            fh.seek(a)
            raw = fh.read(max(0, b - a))
        raw = raw[: len(raw) - (len(raw) % 2)]
        return wav_bytes(np.frombuffer(raw, dtype="<i2"))

    async def _transcribe(self, w: _WindowState) -> None:
        from .video.transcribe import _backoff_s

        reply = None
        reason = None
        for attempt in range(1, _WINDOW_ATTEMPTS + 1):
            if self.status == STATUS_CANCELLED:
                return
            await self._pace()
            self._set_waiting("engine")
            try:
                reply = await asr.transcribe_session_window(
                    self._clip(w),
                    filename=f"s{w.i:05d}.wav",
                    urgent=lambda: self.status != STATUS_RECORDING,
                    on_admitted=lambda: self._set_waiting("none"),
                )
                break
            except asr.ASRRejected as exc:
                log.info("voice session %s window %d refused by the engine: %s", self.id, w.i, exc)
                reason = "engine_refused"
                break
            except (asr.ASRUnavailable, asr.ASRBusy) as exc:
                reason = "engine_unavailable"
                log.info("voice session %s window %d: %s (attempt %d of %d)", self.id, w.i, exc, attempt, _WINDOW_ATTEMPTS)
                if attempt < _WINDOW_ATTEMPTS:
                    await asyncio.sleep(_backoff_s(attempt))
            finally:
                self._set_waiting("none")
        if reply is None:
            w.status, w.reason, w.segments = "failed", reason or "engine_unavailable", []
        else:
            transcript = reply.transcript
            w.engine_ms = int(transcript.engine_ms or 0)
            self.engine_ms += w.engine_ms
            code = transcript.language_code or None
            segments = []
            for s in transcript.segments or ():
                text = str(s.get("text") or "").strip()
                if text:
                    segments.append({
                        "start": float(s.get("start") or 0.0),
                        "end": float(s.get("end") or 0.0),
                        "text": text,
                        "language": s.get("language") or code,
                    })
            text = " ".join(s["text"] for s in segments)
            seconds = w.end_s - w.start_s
            w.language = code
            nsp = reply.no_speech_prob
            # EACH WINDOW IS JUDGED ON ITS OWN, the way dictation judges a
            # clip: the engine still REPORTS its no-speech probability with
            # the gate off. Where it would NOT have gated this window, its
            # words stand (only a lone stock phrase it was unsure of goes);
            # where it would have, the words must be as dense as speech, the
            # rule asr.py applies to its ungated second decode. Applying the
            # density rule to every window, as first built, dropped a real
            # sentence: LibriVox reading the chapter title "Chapter III, The
            # Stock-Broker's Clerk" slowly came back as 5 words in one 12.52 s
            # segment (0.40 words/s) with no_speech_prob 0.0 (live worker
            # engine, 2026-09-29, 3 of 3 runs).
            heard = nsp is None or nsp <= asr._ENGINE_NO_SPEECH_THRESHOLD
            if text and not asr.speech_is_plausible(
                text, seconds, segments, engine_heard_speech=heard, no_speech_prob=nsp
            ):
                w.status, w.reason, w.segments = "dropped", "dropped_as_noise", []
                w.dropped = segments
            else:
                w.status, w.segments = "done", segments
                w.low = asr._reply_confidence(text, seconds, nsp) == asr.CONFIDENCE_LOW
        if self.status == STATUS_CANCELLED:
            return
        _append_line(
            self._p("results.jsonl"),
            {
                "i": w.i, "status": w.status, "segments": w.segments, "language": w.language,
                "low": w.low, "reason": w.reason, "engine_ms": w.engine_ms,
                # What a dropped window said, kept beside the audio it came
                # from so the drop can be checked (the person's own folder).
                "dropped_segments": w.dropped,
            },
        )
        metrics.inc("voice_session_windows_total", "recording-session windows finished", result=w.status)

    def _stitch(self, w: _WindowState) -> None:
        emitted = self.stitcher.feed_window(
            w.window(), w.segments if w.status == "done" else [], next_start_s=w.next_start_s
        )
        self.fed_windows.append((w.start_s, w.end_s, w.low))
        with self.lock:
            for seg in emitted:
                self.segments.append(self._segment_payload(len(self.segments), seg))
            self.tentative = self.stitcher.held_text()
            self.stitched += 1
            self.rev += 1

    def _low_at(self, t: float) -> bool:
        for start, end, low in reversed(self.fed_windows):
            if start <= t <= end:
                return low
        return False

    def _segment_payload(self, i: int, seg: Segment) -> Dict[str, Any]:
        return {
            "i": i,
            "start_ms": int(round(seg.start_s * 1000)),
            "end_ms": int(round(seg.end_s * 1000)),
            "text": seg.text,
            "language": seg.language,
            "low": self._low_at((seg.start_s + seg.end_s) / 2.0),
        }

    # -- the end ----------------------------------------------------------

    def _final_text(self) -> Tuple[str, List[Segment], Dict[str, int]]:
        """The authoritative transcript: every released segment, with a last
        `loops.collapse` over the whole session."""
        with self.lock:
            segments = [
                Segment(s["start_ms"] / 1000.0, s["end_ms"] / 1000.0, s["text"], s.get("language"))
                for s in self.segments
            ]
        collapsed, report = loops.collapse(segments)
        return join_texts([s.text for s in collapsed]), collapsed, report

    def _write_transcript(self, *, final: bool, extra: Optional[Dict[str, Any]] = None) -> None:
        if self.status == STATUS_CANCELLED:
            return
        snap = self.snapshot()
        text, _collapsed, loop_report = self._final_text() if final else (None, [], {})
        payload = {
            "session_id": self.id,
            "final": final,
            "rev": snap["rev"],
            "segments": snap["segments"],
            "tentative": snap["tentative"],
            "gaps": snap["gaps"],
            "audio_ms": snap["audio_ms"],
            "transcribed_ms": snap["transcribed_ms"],
            "speech_ms": snap["speech_ms"],
            "progressive": snap["progressive"],
            "text": text,
            **(extra or {}),
        }
        if final:
            payload["report"] = {
                "detector": self.planner.detector if self.planner else None,
                "windows": len(self.windows),
                "windows_done": sum(1 for w in self.windows if w.status == "done"),
                "windows_failed": sum(1 for w in self.windows if w.status == "failed"),
                "windows_dropped": sum(1 for w in self.windows if w.status == "dropped"),
                "engine_ms": self.engine_ms,
                "paced_s": round(self.paced_s, 1),
                "stitcher": dict(self.stitcher.report),
                "loops": loop_report,
            }
        _write_private_json(self._p("transcript.json"), payload)

    async def _finalize(self) -> None:
        tail = self.stitcher.finish()
        with self.lock:
            for seg in tail:
                self.segments.append(self._segment_payload(len(self.segments), seg))
            self.tentative = ""
        text, collapsed, _report = self._final_text()
        code = dominant_language(collapsed)
        language = _LANGUAGE_NAMES.get(code) if code else None
        speech_ms = int((self.planner.speech_s if self.planner else 0.0) * 1000)
        failed = [w for w in self.windows if w.status == "failed"]
        error = None
        status = STATUS_DONE
        if self.pcm_bytes == 0 and self.decode_error:
            status, outcome = STATUS_FAILED, "undecodable"
            error = {"reason": "undecodable", "detail": "The server couldn't read the audio in this recording."}
        elif speech_ms == 0 or not self.windows:
            outcome = "no_speech"
        elif len(failed) == len(self.windows):
            status, outcome = STATUS_FAILED, "engine_unavailable"
            error = {"reason": "engine_unavailable", "detail": "The speech engine was unavailable, so nothing was transcribed yet."}
        elif failed:
            outcome = "transcribed_with_gaps"
        elif not text:
            outcome = "no_words"
        else:
            outcome = "transcribed"
        src = source_path(self.row)
        sha = await asyncio.to_thread(_sha256_file, src) if os.path.exists(src) else None
        self._write_transcript(
            final=True,
            extra={"outcome": outcome, "language": language, "language_code": code, "decode_error": self.decode_error},
        )
        with contextlib.suppress(OSError):
            _write_private_text(self._p("transcript.txt"), text + ("\n" if text else ""))
        if self.pcm_file is not None:
            self.pcm_file.close()
            self.pcm_file = None
        with contextlib.suppress(OSError):
            os.unlink(self._p("audio.pcm"))
        finished = _now()
        # The row FIRST, then the in-memory rev: a long-poll that wakes on the
        # rev must find the row already done.
        row = await db.run_in_thread(
            _update,
            self.id,
            only_live=True,
            status=status,
            outcome=outcome,
            language=language,
            language_code=code,
            source_sha256=sha,
            finished_at=finished,
            audio_ms=self.audio_ms(),
            transcribed_ms=self.audio_ms(),
            speech_ms=speech_ms,
            windows_total=len(self.windows),
            windows_done=sum(1 for w in self.windows if w.status in ("done", "dropped")),
            windows_failed=len(failed),
            error=error,
            rev=self.current_rev() + 1,
        )
        with self.lock:
            self.status = status
            self.rev = max(self.rev + 1, int((row or {}).get("rev") or 0))
        if row is not None:
            waited = None
            if row.get("finish_requested_at") is not None:
                waited = int((finished - row["finish_requested_at"]).total_seconds() * 1000)
            v19 = {"undecodable": "error", "engine_unavailable": "unavailable"}.get(outcome, "ok")
            await db.run_in_thread(_record_attempt, row, v19, waited)
        metrics.inc("voice_sessions_total", "recording sessions finished", outcome=outcome)
        log.info(
            "voice session %s %s: %s, %d ms of audio, %d windows, engine %d ms, paced %.0f s",
            self.id, status, outcome, self.audio_ms(), len(self.windows), self.engine_ms, self.paced_s,
        )


def _write_private_json(path: str, payload: Any) -> None:
    """Replace `path` atomically with a 0600 file. Unlike video/store.write_json
    it never creates the directory: a session the person discarded must not be
    brought back by a late write."""
    import tempfile

    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".stage-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _write_private_text(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------- maintenance --


async def maintain_once() -> Dict[str, int]:
    """One pass: adopt live sessions nobody holds (the process that ran them
    died), and close the ones no part has reached for VOICE_SESSION_IDLE_S
    (a locked phone, a closed laptop). Runs on the runner loop."""
    rows = await db.run_in_thread(_lapsed_and_idle)
    for session_id in rows["lapsed"]:
        RUNNER._ensure_in_loop(session_id)
    for session_id in rows["idle"]:
        await db.run_in_thread(_finish_sync, session_id, None, "idle", None)
        live = RUNNER.get(session_id)
        if live is not None:
            live.poke({"status": STATUS_FINISHING})
        else:
            RUNNER._ensure_in_loop(session_id)
    return {"adopted": len(rows["lapsed"]), "closed_idle": len(rows["idle"])}


async def _maintain() -> None:
    """Every 15 s, `maintain_once`; hourly, the retention sweep."""
    last_sweep = 0.0
    while True:
        try:
            await maintain_once()
            if time.monotonic() - last_sweep > 3600.0:
                last_sweep = time.monotonic()
                removed = await asyncio.to_thread(retention_sweep)
                if removed:
                    log.info("voice retention sweep removed %d recording(s)", removed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("voice session maintenance failed; trying again")
        await asyncio.sleep(15.0)


def _lapsed_and_idle() -> Dict[str, List[str]]:
    now = _now()
    with db.connection() as con:
        lapsed = con.execute(
            "SELECT id FROM voice_sessions WHERE status IN ('recording', 'finishing') "
            "AND (lease_expires_at IS NULL OR lease_expires_at < %s)",
            (now,),
        ).fetchall()
        idle = con.execute(
            "SELECT id FROM voice_sessions WHERE status = 'recording' "
            "AND COALESCE(last_part_at, created_at) < %s",
            (now - timedelta(seconds=settings.voice_session_idle_s),),
        ).fetchall()
    return {"lapsed": [r["id"] for r in lapsed], "idle": [r["id"] for r in idle]}


def retention_sweep(*, now: Optional[datetime] = None) -> int:
    """Delete finished recordings older than VOICE_RETENTION_DAYS (0 keeps
    them), and directories that have had no row for 24 h (an account removal
    cascades the rows, not the files). The row stays as a tombstone."""
    now = now or _now()
    removed = 0
    days = int(settings.voice_retention_days)
    if days > 0:
        with db.connection() as con:
            rows = con.execute(
                "SELECT id, user_id FROM voice_sessions WHERE status IN ('done', 'failed', 'cancelled') "
                "AND audio_deleted_at IS NULL AND finished_at < %s",
                (now - timedelta(days=days),),
            ).fetchall()
        for row in rows:
            shutil.rmtree(session_dir(row["user_id"], row["id"]), ignore_errors=True)
            _update(row["id"], audio_deleted_at=now)
            removed += 1
    root = data_root()
    try:
        users = [e for e in os.scandir(root) if e.is_dir() and e.name.isdigit()]
    except OSError:
        return removed
    for user in users:
        try:
            entries = [e for e in os.scandir(user.path) if e.is_dir() and _SESSION_ID.match(e.name)]
        except OSError:
            continue
        if not entries:
            continue
        with db.connection() as con:
            known = {
                r["id"]: r["audio_deleted_at"]
                for r in con.execute(
                    "SELECT id, audio_deleted_at FROM voice_sessions WHERE id = ANY(%s)",
                    ([e.name for e in entries],),
                ).fetchall()
            }
        for entry in entries:
            if entry.name in known:
                if known[entry.name] is not None:
                    # Deleted by its owner or by retention, but something
                    # left files behind: they go too.
                    shutil.rmtree(entry.path, ignore_errors=True)
                    removed += 1
                continue
            try:
                age = now.timestamp() - entry.stat().st_mtime
            except OSError:
                continue
            if age > _ORPHAN_GRACE_S:
                shutil.rmtree(entry.path, ignore_errors=True)
                removed += 1
    return removed


# ------------------------------------------------------ the service API --
#
# Called by the routes in app/audio_api.py. Every function takes the caller's
# user id and re-derives ownership from the row; a SessionError carries the
# refusal.


def _stored(row: Dict[str, Any]) -> Dict[str, Any]:
    days = int(settings.voice_retention_days)
    delete_after = None
    if days > 0 and row.get("finished_at") is not None:
        delete_after = _iso(row["finished_at"] + timedelta(days=days))
    return {
        "kept": row.get("audio_deleted_at") is None,
        "retention_days": days,
        "delete_after": delete_after,
        "bytes": int(row.get("bytes_stored") or 0),
    }


def _saved_transcript(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        data = _read_json(_path(row["user_id"], row["id"], "transcript.json"))
    except ValueError:
        data = None
    return data if isinstance(data, dict) else {}


def state(row: Dict[str, Any], *, cursor: int = 0) -> Dict[str, Any]:
    """The SESSION STATE object every session endpoint returns.

    The ROW is authoritative for status, outcome and language (the worker
    writes the row before it moves its own rev, so a long-poll that wakes
    on the rev finds the row already there). The live worker, when this
    process runs it, supplies what changes between row writes: the decoded
    length, the segments, the held-back tail and why it is behind."""
    cursor = max(0, int(cursor))
    live = RUNNER.get(row["id"])
    snap = live.snapshot(cursor) if live is not None and live.started else None
    status = row["status"]
    saved: Dict[str, Any] = {}
    if snap is None or status in (STATUS_DONE, STATUS_FAILED):
        saved = _saved_transcript(row)
    if snap is not None and status in LIVE_STATUSES:
        count = int(snap["count"])
        segments = snap["segments"]
        source = snap
    else:
        every = saved.get("segments") or []
        count = len(every)
        segments = every[cursor:]
        source = saved
    audio_ms = int((snap or {}).get("audio_ms") or row.get("audio_ms") or saved.get("audio_ms") or 0)
    if status in (STATUS_DONE, STATUS_FAILED):
        transcribed_ms = audio_ms
    elif snap is not None:
        transcribed_ms = int(snap["transcribed_ms"])
    else:
        transcribed_ms = int(row.get("transcribed_ms") or 0)
    body = {
        "session_id": row["id"],
        "status": status,
        "rev": max(int(row.get("rev") or 0), int((snap or {}).get("rev") or 0)),
        "next_part": max(int(row.get("next_part") or 0), int((snap or {}).get("next_part") or 0)),
        "bytes_stored": max(int(row.get("bytes_stored") or 0), int((snap or {}).get("bytes_stored") or 0)),
        "audio_ms": audio_ms,
        "transcribed_ms": transcribed_ms,
        "backlog_ms": max(0, audio_ms - transcribed_ms),
        "waiting_on": ((snap or {}).get("waiting_on") or "none") if status in LIVE_STATUSES else "none",
        "progressive": bool(source.get("progressive", True)),
        "cursor": count,
        "segments": segments,
        "tentative": (source.get("tentative") or "") if status in LIVE_STATUSES else "",
        "gaps": source.get("gaps") or [],
        "outcome": row.get("outcome") or (saved.get("outcome") if status in (STATUS_DONE, STATUS_FAILED) else None),
        "text": None,
        "language": row.get("language"),
        "language_code": row.get("language_code"),
        "speech_ms": int((snap or {}).get("speech_ms") or row.get("speech_ms") or 0),
        "ended_by": row.get("ended_by"),
        "stored": _stored(row),
        "error": row.get("error"),
    }
    if status == STATUS_DONE:
        body["text"] = saved.get("text") or ""
    return body


def current_rev(row: Dict[str, Any]) -> int:
    """The rev a long-poll compares against: the live worker's when this
    process runs it, the row's otherwise."""
    live = RUNNER.get(row["id"])
    if live is not None and live.started:
        return max(int(row.get("rev") or 0), live.current_rev())
    return int(row.get("rev") or 0)


def config() -> Dict[str, Any]:
    bits = int(settings.voice_recorder_bits_per_second)
    return {
        "part_ms": int(settings.voice_part_ms),
        "part_limit_bytes": part_limit_bytes(),
        "bits_per_second": bits or None,
        "idle_close_s": int(settings.voice_session_idle_s),
        "long_poll_max_s": LONG_POLL_MAX_S,
    }


def create(user_id: int, client_key: str, mime: str) -> Tuple[Dict[str, Any], bool]:
    """Open (or, for a retried create, find) this person's session."""
    try:
        client_key = str(uuid.UUID(str(client_key)))
    except (ValueError, TypeError, AttributeError):
        raise SessionError(400, "bad_request", "client_key must be a UUID.") from None
    if not mime:
        raise SessionError(400, "bad_request", "mime_type is required.")
    ext = extension_for(mime)
    if ext is None:
        raise SessionError(415, "unsupported_format", f"{base_type(mime)} is not a supported audio format.")
    # A retried create answers before anything that could refuse it.
    with db.connection() as con:
        existing = con.execute(
            f"SELECT {_COLUMNS} FROM voice_sessions WHERE user_id = %s AND client_key = %s",
            (int(user_id), client_key),
        ).fetchone()
    if existing is not None:
        if existing["status"] == STATUS_CANCELLED:
            raise _not_found()
        return dict(existing), False
    if not _rate_ok("create", user_id, settings.voice_session_create_per_min):
        raise SessionError(429, "rate_limited", "Too many recordings were started just now.")
    if _live_count() >= settings.voice_session_max_active:
        raise SessionError(503, "capacity_full", "Too many people are recording right now.")
    refusal = _storage_refusal()
    if refusal is not None:
        raise refusal
    row, created = _create_row(user_id, client_key, base_type(mime), ext)
    if created:
        _private_dirs(user_id, row["id"])
        metrics.inc("voice_sessions_started_total", "recording sessions opened")
    return row, created


def start_worker(session_id: str) -> None:
    RUNNER.ensure(session_id)


def _parts_record(row: Dict[str, Any], seq: int) -> Optional[Dict[str, Any]]:
    for line in _read_lines(_path(row["user_id"], row["id"], "parts.jsonl")):
        if int(line.get("seq", -1)) == seq:
            return line
    return None


_append_locks: Dict[str, threading.Lock] = {}
_append_locks_guard = threading.Lock()


def _append_lock(session_id: str) -> threading.Lock:
    with _append_locks_guard:
        lock = _append_locks.get(session_id)
        if lock is None:
            lock = _append_locks[session_id] = threading.Lock()
        return lock


def append_part(user_id: int, session_id: str, seq: int, body: bytes, sha: str) -> Tuple[Dict[str, Any], bool]:
    """Store one part: fsynced into source.<ext> at the next offset BEFORE the
    row says so. Returns (row, duplicate). Blocking; run it in a thread."""
    with _append_lock(session_id):
        row = _owned_row(session_id, user_id)
        next_part = int(row["next_part"] or 0)
        if seq < next_part:
            stored = _parts_record(row, seq)
            if stored is not None and stored.get("sha256") == sha:
                return row, True
            raise SessionError(
                409, "part_conflict",
                "This recording is also being uploaded from another tab.",
                next_part=next_part,
            )
        if row["status"] != STATUS_RECORDING:
            raise SessionError(
                409, "session_closed", "This recording is no longer accepting audio.",
                status=row["status"], ended_by=row.get("ended_by"),
            )
        if seq > next_part:
            raise SessionError(409, "out_of_order", "A part is missing before this one.", next_part=next_part)
        if seq == 0 and not _magic_ok(row["ext"], body[:16]):
            # Nothing after a wrong opening can decode: the session goes.
            _cancel_sync(row)
            raise SessionError(415, "unsupported_format", f"{row['mime_type']} audio did not start the way that format starts.")
        free = free_bytes()
        if free is not None and free < settings.voice_min_free_bytes:
            _finish_sync(session_id, user_id, "storage_full", None)
            raise SessionError(507, "storage_full", "The server ran out of space for recordings.")
        bytes_stored = int(row["bytes_stored"] or 0)
        try:
            _private_dirs(row["user_id"], session_id)
            src = source_path(row)
            fd = os.open(src, os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                # Another process appending during a rolling recreate is held
                # off by the lock; the row check below catches the rest.
                fcntl.flock(fd, fcntl.LOCK_EX)
                size = os.fstat(fd).st_size
                if size < bytes_stored:
                    raise OSError(f"source is {size} bytes, the row acknowledges {bytes_stored}")
                if size > bytes_stored:
                    os.ftruncate(fd, bytes_stored)  # an unacknowledged tail from a crash
                view = memoryview(body)
                offset = bytes_stored
                while view:
                    written = os.pwrite(fd, view, offset)
                    view = view[written:]
                    offset += written
                os.fsync(fd)
            finally:
                os.close(fd)
            _append_line(
                _path(row["user_id"], session_id, "parts.jsonl"),
                {"seq": seq, "offset": bytes_stored, "bytes": len(body), "sha256": sha, "received_at": _iso(_now())},
            )
        except OSError as exc:
            log.warning("voice session %s: part %d not stored: %s", session_id, seq, exc)
            raise SessionError(503, "storage_unavailable", "The server can't save audio right now.") from None
        with db.connection() as con:
            updated = con.execute(
                f"""UPDATE voice_sessions SET next_part = %s, bytes_stored = %s, last_part_at = %s, rev = rev + 1
                    WHERE id = %s AND next_part = %s AND status = 'recording' RETURNING {_COLUMNS}""",
                (seq + 1, bytes_stored + len(body), _now(), session_id, seq),
            ).fetchone()
        if updated is None:
            raise SessionError(409, "part_conflict", "This recording is also being uploaded from another tab.", next_part=next_part)
        metrics.inc("voice_session_parts_total", "recording-session parts stored")
        return dict(updated), False


def _finish_sync(session_id: str, user_id: Optional[int], ended_by: str, last_part: Optional[int]) -> Dict[str, Any]:
    row = _row(session_id)
    if row is None or (user_id is not None and int(row["user_id"]) != int(user_id)):
        raise _not_found()
    if row["status"] == STATUS_CANCELLED:
        raise _not_found()
    if row["status"] != STATUS_RECORDING:
        return row
    next_part = int(row["next_part"] or 0)
    if last_part is not None and last_part >= next_part:
        raise SessionError(
            409, "parts_missing", "The server is missing parts of this recording.",
            next_part=next_part, last_part=last_part,
        )
    with db.connection() as con:
        updated = con.execute(
            f"""UPDATE voice_sessions SET status = 'finishing', ended_by = %s, finish_requested_at = %s,
                    rev = rev + 1
                WHERE id = %s AND status = 'recording' RETURNING {_COLUMNS}""",
            (ended_by, _now(), session_id),
        ).fetchone()
    return dict(updated) if updated else (_row(session_id) or row)


def finish(user_id: int, session_id: str, *, last_part: Optional[int], ended_by: str) -> Tuple[Dict[str, Any], bool]:
    """(row, started). Idempotent: a repeat answers the current state."""
    if ended_by not in ENDED_BY_CLIENT:
        raise SessionError(400, "bad_request", "ended_by is not one this server knows.")
    if last_part is not None and last_part < 0:
        raise SessionError(400, "bad_request", "last_part must be a non-negative integer.")
    before = _owned_row(session_id, user_id)
    row = _finish_sync(session_id, user_id, ended_by, last_part)
    started = before["status"] == STATUS_RECORDING and row["status"] == STATUS_FINISHING
    RUNNER.notify(session_id, status=row["status"], ended_by=row.get("ended_by"), rev=row["rev"])
    return row, started


def _cancel_sync(row: Dict[str, Any]) -> None:
    with db.connection() as con:
        con.execute(
            "UPDATE voice_sessions SET status = 'cancelled', audio_deleted_at = COALESCE(audio_deleted_at, %s), "
            "finished_at = COALESCE(finished_at, %s), rev = rev + 1 WHERE id = %s",
            (_now(), _now(), row["id"]),
        )
    shutil.rmtree(session_dir(row["user_id"], row["id"]), ignore_errors=True)


def discard(user_id: int, session_id: str) -> None:
    """The person's own X: stop, forget, delete the files. Idempotent.

    Under the session's append lock, so a part being stored at the same
    moment cannot recreate the directory after it is removed; the row is
    cancelled first, so the worker (whose row writes are `only_live`) cannot
    bring the session back while it unwinds."""
    with _append_lock(session_id):
        row = _owned_row(session_id, user_id, allow_cancelled=True)
        live = RUNNER.get(session_id)
        if live is not None:
            RUNNER.call(live.poke, {"status": STATUS_CANCELLED})
            if live.task is not None:
                # A window already inside the engine cannot be stopped (whisper
                # decodes in an executor); its answer is thrown away.
                RUNNER.call(live.task.cancel)
        _cancel_sync(row)
    metrics.inc("voice_sessions_discarded_total", "recording sessions discarded by their owner")


def retranscribe(user_id: int, session_id: str, scope: str) -> Tuple[Dict[str, Any], bool]:
    if scope not in ("gaps", "all"):
        raise SessionError(400, "bad_request", "scope must be gaps or all.")
    row = _owned_row(session_id, user_id)
    if row["status"] in LIVE_STATUSES:
        raise SessionError(409, "session_busy", "This recording is still being transcribed.")
    if row.get("audio_deleted_at") is not None or not os.path.exists(source_path(row)):
        raise SessionError(410, "audio_deleted", "This recording's audio has been deleted.")
    if scope == "gaps" and row["status"] == STATUS_DONE and not int(row.get("windows_failed") or 0):
        return row, False
    if row.get("outcome") == "undecodable":
        # Nothing was decoded, so nothing was planned: plan again from the audio.
        for name in ("plan.jsonl", "results.jsonl"):
            with contextlib.suppress(OSError):
                os.unlink(_path(row["user_id"], row["id"], name))
    updated = _update(
        session_id, status=STATUS_FINISHING, retranscribe=scope, outcome=None, error=None,
        finished_at=None, finish_requested_at=_now(),
    )
    RUNNER.ensure(session_id)
    return updated or row, True


def list_sessions(user_id: int, *, limit: int, before: Optional[datetime], preview: bool = True) -> Dict[str, Any]:
    params: List[Any] = [int(user_id)]
    where = "user_id = %s AND status <> 'cancelled'"
    if before is not None:
        where += " AND created_at < %s"
        params.append(before)
    params.append(int(limit) + 1)
    with db.connection() as con:
        rows = [dict(r) for r in con.execute(
            f"SELECT {_COLUMNS} FROM voice_sessions WHERE {where} ORDER BY created_at DESC LIMIT %s",
            tuple(params),
        ).fetchall()]
    more = len(rows) > limit
    rows = rows[:limit]
    out = []
    for row in rows:
        item = {
            "session_id": row["id"],
            "created_at": _iso(row["created_at"]),
            "status": row["status"],
            "outcome": row.get("outcome"),
            "audio_ms": int(row.get("audio_ms") or 0),
            "bytes": int(row.get("bytes_stored") or 0),
            "mime_type": row["mime_type"],
            "delete_after": _stored(row)["delete_after"],
        }
        if preview:
            text = _saved_transcript(row).get("text") if row["status"] == STATUS_DONE else None
            item["preview"] = (text or "")[:120] or None
        out.append(item)
    return {"sessions": out, "next_before": _iso(rows[-1]["created_at"]) if more and rows else None}


def audio_file(row: Dict[str, Any]) -> str:
    """The stored recording's path, or SessionError 410 when retention (or
    the person) removed it."""
    path = source_path(row)
    if row.get("audio_deleted_at") is not None or not os.path.exists(path):
        raise SessionError(410, "audio_deleted", "This recording's audio has been deleted.")
    return path


def transcript_of(row: Dict[str, Any]) -> Dict[str, Any]:
    saved = _saved_transcript(row)
    return {
        "text": saved.get("text"),
        "segments": saved.get("segments") or [],
        "language": row.get("language"),
        "outcome": row.get("outcome"),
    }


def reset_for_tests() -> None:
    RUNNER.stop_all()
    with _rate_lock:
        _recent.clear()
    with _append_locks_guard:
        _append_locks.clear()
