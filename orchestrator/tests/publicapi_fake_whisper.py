"""A speech engine, a recording and a decoder for the audio-window tests (2026-09-13).

Not a test module (no `test_` prefix); `test_publicapi_audio_windows.py`
imports it.

WHY A FAKE THAT "HEARS". Asserting that a two-hour transcript has no
duplicated or missing words at window joins and correct timestamps needs a
ground truth, and a stub that returns canned text per request cannot give one:
the pipeline cuts windows by voice activity, so which words land in which clip
is decided by the code under test. So the recording is SYNTHETIC SPEECH whose
words are decodable: each word is a 0.28-0.42 s burst of two tones (one of 20
low frequencies, one of 20 high: a 400-word vocabulary), separated by short
gaps inside sentences and longer pauses between them, with stretches of
continuous speech longer than one window so overlapping splits happen. The
fake engine finds the bursts in whatever clip it is sent, reads the tones with
an FFT, and answers with segments timed RELATIVE TO THE CLIP — exactly what
whisper does — so the pipeline has to put them back in recording time itself.
A burst the clip cut off at either edge comes back as the truncated word a
real decoder produces there ("tiren" → "ti"), which is the case that
duplicated words at seams.

WHAT IS COPIED FROM compose/whisper/server.py, AND WHAT IS NOT.
Copied: the routes (POST /v1/audio/transcriptions with the same form fields,
GET /health, GET /v1/models); ONE clip decoding at a time (`_gpu_lock`); the
600 s refusal as 413; an undecodable or empty upload as 400; 503 while not
ready; `no_speech_check` judging the first 30 s; the reply shapes of json, text
(a JSON-encoded string served as text/plain, as the server does) and
verbose_json (`text`, `language`, `language_code`, `duration`,
`no_speech_prob`, `segments[{id,start,end,text,language}]`, `processing_ms`,
`task`); `/health`'s `ready` and `cuda_failures`.
Not copied: the model, and ffmpeg — the engine accepts RIFF/WAVE only, which is
all the pipeline sends it.

THE DECODER. The host has no ffmpeg (checked 2026-09-13: `which ffmpeg
ffprobe` finds nothing), so `install_fake_media_tools` writes executable
stand-ins that honour the argv the pipeline builds: they refuse to run without
the input allowlists, read WAV from stdin or a path, write raw s16le, print
`-progress` lines, and log their pid, niceness, I/O class and how many copies
were running, so a test can prove the scheduling the real ones get.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import stat
import struct
import sys
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import httpx
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.publicapi.audio_jobs import on_worker as aj_on_worker

SAMPLE_RATE = 16000
LOW_TONES = [400.0 + 40.0 * k for k in range(20)]
HIGH_TONES = [1400.0 + 80.0 * k for k in range(20)]
_FIRST = ["ba", "de", "fi", "go", "ku", "la", "me", "ni", "po", "ru", "sa", "te", "vi", "wo", "zu", "ka", "le", "mi", "no", "pu"]
_SECOND = ["ren", "tas", "lok", "mir", "dun", "vel", "sop", "kar", "nim", "tol", "bex", "rud", "fam", "gil", "hov", "jat", "kes", "lun", "mog", "pry"]
VOCABULARY = [_FIRST[i // 20] + _SECOND[i % 20] for i in range(400)]
TONE_AMPLITUDE = 0.25
NOISE_AMPLITUDE = 0.002
RAMP_S = 0.005
#: The shortest word the script contains; a burst shorter than this that
#: touches a clip edge was cut by the clip.
MIN_WORD_S = 0.28


def norm_word(word: str) -> str:
    return "".join(ch for ch in word.lower() if ch.isalnum())


def normalized_words(text: str) -> List[str]:
    return [w for w in (norm_word(t) for t in text.split()) if w]


# ------------------------------------------------------------ the script --


@dataclass(frozen=True)
class Word:
    position: int
    vocab: int
    start_s: float
    end_s: float

    @property
    def text(self) -> str:
        return VOCABULARY[self.vocab]


@dataclass
class Script:
    seconds: float
    words: List[Word]
    continuous: List[Tuple[float, float]] = field(default_factory=list)

    @property
    def starts(self) -> List[float]:
        return [w.start_s for w in self.words]


def build_script(seconds: float, *, seed: int = 7) -> Script:
    """Deterministic speech: sentences with 0.6-1.8 s pauses, paragraphs with
    2.5-9 s breaks (longer than the 2 s window gap), and stretches of 95-200 s
    with no pause of 0.5 s or more (longer than one 90 s window)."""
    rng = random.Random(seed)
    words: List[Word] = []
    continuous: List[Tuple[float, float]] = []
    t = 0.6
    previous = -1
    limit = seconds - 3.0

    def add(at: float) -> Optional[float]:
        nonlocal previous
        duration = rng.uniform(0.28, 0.42)
        if at + duration > limit:
            return None
        vocab = rng.randrange(len(VOCABULARY))
        while vocab == previous:
            vocab = rng.randrange(len(VOCABULARY))
        previous = vocab
        words.append(Word(len(words), vocab, round(at, 4), round(at + duration, 4)))
        return at + duration

    while t < limit:
        if rng.random() < 0.18:
            block_end = min(limit, t + rng.uniform(95.0, 200.0))
            block_start = t
            while t < block_end:
                end = add(t)
                if end is None:
                    t = limit
                    break
                t = end + rng.uniform(0.08, 0.2)
            continuous.append((block_start, t))
            t += rng.uniform(2.5, 6.0)
        else:
            for _ in range(rng.randint(2, 7)):
                for _ in range(rng.randint(4, 14)):
                    end = add(t)
                    if end is None:
                        t = limit
                        break
                    t = end + rng.uniform(0.08, 0.2)
                t += rng.uniform(0.6, 1.8)
            t += rng.uniform(2.5, 9.0)
    return Script(seconds=seconds, words=words, continuous=continuous)


def _render_block(script: Script, first_sample: int, count: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    out = rng.uniform(-NOISE_AMPLITUDE, NOISE_AMPLITUDE, count)
    block_start = first_sample / SAMPLE_RATE
    block_end = (first_sample + count) / SAMPLE_RATE
    starts = script.starts
    lo = max(0, bisect_left(starts, block_start - 1.0))
    hi = bisect_right(starts, block_end)
    ramp = int(RAMP_S * SAMPLE_RATE)
    for word in script.words[lo:hi]:
        ws = int(round(word.start_s * SAMPLE_RATE))
        we = int(round(word.end_s * SAMPLE_RATE))
        a, b = max(ws, first_sample), min(we, first_sample + count)
        if a >= b:
            continue
        idx = np.arange(a, b)
        tt = (idx - ws) / SAMPLE_RATE
        low, high = LOW_TONES[word.vocab // 20], HIGH_TONES[word.vocab % 20]
        tone = TONE_AMPLITUDE * (np.sin(2 * np.pi * low * tt) + np.sin(2 * np.pi * high * tt))
        envelope = np.minimum(1.0, np.minimum((idx - ws) / ramp, (we - idx) / ramp))
        out[a - first_sample : b - first_sample] += tone * np.clip(envelope, 0.0, 1.0)
    return np.clip(out * 32767.0, -32768, 32767).astype("<i2")


def write_speech_wav(path: str, script: Script, *, block_s: float = 60.0, seed: int = 11) -> int:
    """Stream the script to a 16 kHz mono 16-bit WAV. Returns the byte size."""
    total = int(round(script.seconds * SAMPLE_RATE))
    with open(path, "wb") as fh:
        fh.write(_wav_header(total * 2))
        block = int(block_s * SAMPLE_RATE)
        for number, first in enumerate(range(0, total, block)):
            fh.write(_render_block(script, first, min(block, total - first), seed + number).tobytes())
    return os.path.getsize(path)


def _wav_header(data_bytes: int, *, channels: int = 1, rate: int = SAMPLE_RATE) -> bytes:
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_bytes)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * 2 * channels, 2 * channels, 16)
        + b"data"
        + struct.pack("<I", data_bytes)
    )


# ------------------------------------------------------------ hearing --


@dataclass(frozen=True)
class Heard:
    start_s: float
    end_s: float
    text: str
    partial: bool


def _nearest(frequency: float, grid: Sequence[float]) -> int:
    return int(min(range(len(grid)), key=lambda k: abs(grid[k] - frequency)))


def hear(pcm: np.ndarray) -> List[Heard]:
    """Bursts in a clip → words, timed relative to the clip."""
    frame = SAMPLE_RATE // 100  # 10 ms
    usable = (len(pcm) // frame) * frame
    if usable == 0:
        return []
    # In 10 s blocks: this engine runs in the test's own process, and a whole
    # 90 s clip as float32 twice over (~23 MB) would be counted against the
    # pipeline in the memory measurement it is there to support.
    block = frame * 1000
    parts = []
    for start in range(0, usable, block):
        x = pcm[start : min(usable, start + block)].astype(np.float32).reshape(-1, frame) / 32768.0
        parts.append(np.sqrt(np.mean(x * x, axis=1)) > 0.05)
    loud = np.concatenate(parts)
    runs: List[Tuple[int, int]] = []
    i = 0
    n = len(loud)
    while i < n:
        if not loud[i]:
            i += 1
            continue
        j = i
        while j < n and loud[j]:
            j += 1
        if runs and i - runs[-1][1] <= 2:
            runs[-1] = (runs[-1][0], j)
        else:
            runs.append((i, j))
        i = j
    heard: List[Heard] = []
    for a, b in runs:
        start_s, end_s = a * 0.01, b * 0.01
        duration = end_s - start_s
        if duration < 0.08:
            continue
        lo = a * frame + frame // 2
        hi = min(len(pcm), b * frame - frame // 2)
        samples = pcm[lo:hi].astype(np.float64)
        if len(samples) < 256:
            continue
        spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
        freqs = np.fft.rfftfreq(len(samples), 1.0 / SAMPLE_RATE)
        low_band = (freqs >= 380) & (freqs <= 1180)
        high_band = (freqs >= 1360) & (freqs <= 2960)
        low = freqs[low_band][int(np.argmax(spectrum[low_band]))]
        high = freqs[high_band][int(np.argmax(spectrum[high_band]))]
        vocab = _nearest(low, LOW_TONES) * 20 + _nearest(high, HIGH_TONES)
        word = VOCABULARY[vocab]
        touches_edge = a <= 1 or b >= n - 1
        partial = touches_edge and duration < MIN_WORD_S - 0.02
        if partial:
            keep = max(1, min(len(word) - 1, int(len(word) * duration / 0.35)))
            word = word[:keep]
        heard.append(Heard(start_s, end_s, word, partial))
    return heard


def segments_from_heard(heard: Sequence[Heard], clip_seconds: float) -> List[dict]:
    """Whisper-shaped segments: a new one after a 0.45 s pause or 12 words,
    capitalised and full-stopped, times on whisper's 0.02 s grid."""
    groups: List[List[Heard]] = []
    for word in heard:
        if not groups or word.start_s - groups[-1][-1].end_s >= 0.45 or len(groups[-1]) >= 12:
            groups.append([word])
        else:
            groups[-1].append(word)
    out = []
    for number, group in enumerate(groups):
        texts = [w.text for w in group]
        texts[0] = texts[0][:1].upper() + texts[0][1:]
        texts[-1] = texts[-1] + "."
        start = round(group[0].start_s / 0.02) * 0.02
        end = round(group[-1].end_s / 0.02) * 0.02
        start = max(0.0, min(start, clip_seconds))
        end = max(start, min(end, clip_seconds))
        out.append({"id": number, "start": round(start, 3), "end": round(end, 3), "text": " ".join(texts), "language": "en"})
    return out


def parse_wav(payload: bytes) -> np.ndarray:
    if len(payload) < 12 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        raise ValueError("Invalid data found when processing input")
    offset = 12
    channels = rate = bits = None
    while offset + 8 <= len(payload):
        tag = payload[offset : offset + 4]
        size = struct.unpack("<I", payload[offset + 4 : offset + 8])[0]
        body = offset + 8
        if tag == b"fmt ":
            _fmt, channels, rate = struct.unpack("<HHI", payload[body : body + 8])
            bits = struct.unpack("<H", payload[body + 14 : body + 16])[0]
        elif tag == b"data":
            if channels != 1 or rate != SAMPLE_RATE or bits != 16:
                raise ValueError("unsupported sample format")
            data = payload[body : body + size]
            return np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2")
        offset = body + size + (size % 2)
    raise ValueError("no data chunk")


# ------------------------------------------------------------ the engine --


class FakeWhisper:
    """One replica. Knobs are plain attributes a test flips while it runs."""

    MAX_AUDIO_SECONDS = 600.0
    NO_SPEECH_THRESHOLD = 0.6

    def __init__(self, name: str, fleet: Optional["Fleet"] = None) -> None:
        self.name = name
        self.fleet = fleet
        self.ready = True
        self.cuda_failures = 0
        self.down = False  # connections refused
        self.fail_status: Optional[int] = None  # every call answers this
        self.fail_after_calls: Optional[int] = None  # calls numbered above this fail with fail_status
        self.hang_calls: set = set()  # call numbers that never answer
        self.loop_on_calls: set = set()  # call numbers whose first segment loops
        self.latency_s = 0.0
        self.on_call: Optional[Callable[[int], Any]] = None
        self.calls: List[Dict[str, Any]] = []
        self.health_reads = 0
        self._gpu_lock = asyncio.Lock()
        self.app = self._build()
        self.transport = httpx.ASGITransport(app=self.app)

    def _build(self) -> FastAPI:
        app = FastAPI()
        fake = self

        @app.get("/health")
        async def health() -> dict:
            fake.health_reads += 1
            return {
                "ready": bool(fake.ready),
                "model": "openai/whisper-large-v3",
                "long_form": "sequential",
                "task": "transcribe",
                "no_speech_threshold": fake.NO_SPEECH_THRESHOLD,
                "error": None,
                "cuda_failures": int(fake.cuda_failures),
            }

        @app.get("/v1/models")
        async def models() -> dict:
            return {"object": "list", "data": [{"id": "openai/whisper-large-v3", "object": "model", "owned_by": "openai"}]}

        @app.post("/v1/audio/transcriptions")
        async def transcriptions(
            file: UploadFile = File(...),
            model: str = Form(default="openai/whisper-large-v3"),
            language: Optional[str] = Form(default=None),
            response_format: str = Form(default="json"),
            no_speech_check: bool = Form(default=True),
        ):
            return await fake._transcribe(file, language, response_format, no_speech_check)

        return app

    async def _transcribe(self, file: UploadFile, language: Optional[str], response_format: str, no_speech_check: bool):
        if response_format not in ("json", "text", "verbose_json"):
            raise HTTPException(status_code=400, detail=f"unknown response_format {response_format!r}")
        if not self.ready:
            raise HTTPException(status_code=503, detail="model is still loading")
        payload = await file.read()
        if not payload:
            raise HTTPException(status_code=400, detail="empty upload")
        number = len(self.calls) + 1
        record: Dict[str, Any] = {"number": number, "bytes": len(payload), "started": time.monotonic(), "language": language}
        self.calls.append(record)
        if self.on_call is not None:
            outcome = self.on_call(number)
            if asyncio.iscoroutine(outcome):
                await outcome
        if self.fail_status is not None and (self.fail_after_calls is None or number > self.fail_after_calls):
            record["status"] = self.fail_status
            raise HTTPException(status_code=self.fail_status, detail="injected failure")
        try:
            audio = parse_wav(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"could not decode audio: {exc}") from None
        seconds = audio.size / SAMPLE_RATE
        record["seconds"] = seconds
        if seconds > self.MAX_AUDIO_SECONDS:
            raise HTTPException(status_code=413, detail=f"audio is {seconds:.0f}s; the limit is {self.MAX_AUDIO_SECONDS:.0f}s")
        async with self._gpu_lock:
            if self.fleet is not None:
                self.fleet.enter()
            try:
                if number in self.hang_calls:
                    await asyncio.Event().wait()
                if self.latency_s:
                    await asyncio.sleep(self.latency_s)
                # The pipeline's own worker thread, so this in-process engine's
                # buffers land in the arena the measurement already counts
                # once, instead of a fresh arena per executor thread.
                heard = await aj_on_worker(hear, audio)
            finally:
                if self.fleet is not None:
                    self.fleet.leave()
        record["ended"] = time.monotonic()
        first = audio[: 30 * SAMPLE_RATE].astype(np.float32) / 32768.0
        silence = 0.9 if (first.size == 0 or float(np.sqrt(np.mean(first * first))) < 0.01) else 0.02
        if no_speech_check and silence > self.NO_SPEECH_THRESHOLD:
            result = {"text": "", "language": None, "language_code": None, "duration": round(seconds, 3), "no_speech_prob": silence, "segments": []}
        else:
            segments = segments_from_heard(heard, seconds)
            if number in self.loop_on_calls and segments:
                segments[0]["text"] = segments[0]["text"] + " " + " ".join(["no"] * 40)
            result = {
                "text": " ".join(s["text"] for s in segments),
                "language": language or "english",
                "language_code": language or "en",
                "duration": round(seconds, 3),
                "no_speech_prob": silence,
                "segments": segments,
            }
        result["processing_ms"] = 1
        if response_format == "text":
            return JSONResponse(content=result["text"], media_type="text/plain")
        if response_format != "verbose_json":
            result.pop("segments", None)
        else:
            result["task"] = "transcribe"
        return JSONResponse(content=result)


class Fleet:
    """The replicas, addressed by host, and the fleet-wide concurrency seen."""

    def __init__(self, names: Sequence[str] = ("asr-head", "asr-worker")) -> None:
        self.replicas: Dict[str, FakeWhisper] = {name: FakeWhisper(name, self) for name in names}
        self.in_flight = 0
        self.peak_in_flight = 0

    def enter(self) -> None:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)

    def leave(self) -> None:
        self.in_flight -= 1

    def urls(self) -> List[str]:
        return [f"http://{name}.internal:30007/v1" for name in self.replicas]

    def __getitem__(self, name: str) -> FakeWhisper:
        return self.replicas[name]

    @property
    def calls(self) -> List[Dict[str, Any]]:
        return [call for fake in self.replicas.values() for call in fake.calls]

    def transport(self) -> httpx.AsyncBaseTransport:
        fleet = self

        class _Routed(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                name = (request.url.host or "").split(".")[0]
                fake = fleet.replicas.get(name)
                if fake is None or fake.down:
                    raise httpx.ConnectError("connection refused", request=request)
                return await fake.transport.handle_async_request(request)

        return _Routed()


# ------------------------------------------------------ media tool stand-ins --

_FAKE_FFMPEG = r'''#!{python}
"""ffmpeg stand-in for tests: WAV (16 kHz, 16-bit, mono or stereo) -> raw s16le mono."""
import json, os, subprocess, sys, time

args = sys.argv[1:]

def opt(name):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None

def fail(message, code=1):
    sys.stderr.write(message + "\n")
    sys.exit(code)

if "-i" not in args:
    fail("no input", 2)
before_input = args[: args.index("-i")]
if "-protocol_whitelist" not in before_input or "-format_whitelist" not in before_input:
    fail("refusing to run without input allowlists", 2)
protocols = opt("-protocol_whitelist").split(",")
formats = opt("-format_whitelist").split(",")
source = opt("-i")
out = args[-1]
if source == "pipe:0":
    if "pipe" not in protocols:
        fail("Protocol 'pipe' not on whitelist")
    stream = sys.stdin.buffer
else:
    if "file" not in protocols:
        fail("Protocol 'file' not on whitelist")
    stream = open(source, "rb")

log = os.environ.get("FAKE_FFMPEG_LOG")
rundir = os.environ.get("FAKE_FFMPEG_RUNDIR")
marker = None
concurrent = 1
if rundir:
    os.makedirs(rundir, exist_ok=True)
    marker = os.path.join(rundir, str(os.getpid()))
    open(marker, "w").close()
    concurrent = len(os.listdir(rundir))
started = time.time()
ionice = subprocess.run(["ionice", "-p", str(os.getpid())], capture_output=True, text=True).stdout.strip()
entry = {{"pid": os.getpid(), "argv": sys.argv, "nice": os.nice(0), "ionice": ionice, "concurrent": concurrent, "input": source}}

def finish(code):
    entry["exit"] = code
    entry["elapsed"] = time.time() - started
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    if marker and os.path.exists(marker):
        os.unlink(marker)
    sys.exit(code)

head = stream.read(12)
if head.startswith(b"#EXTM3U") and "hls" not in formats:
    sys.stderr.write("Format not on whitelist\n")
    finish(1)
if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE" or "wav" not in formats:
    sys.stderr.write("Invalid data found when processing input\n")
    finish(1)
channels = rate = bits = None
remaining = None
while True:
    chunk = stream.read(8)
    if len(chunk) < 8:
        sys.stderr.write("no data chunk\n")
        finish(1)
    tag, size = chunk[:4], int.from_bytes(chunk[4:8], "little")
    if tag == b"fmt ":
        body = stream.read(size)
        channels = int.from_bytes(body[2:4], "little")
        rate = int.from_bytes(body[4:8], "little")
        bits = int.from_bytes(body[14:16], "little")
        if size % 2:
            stream.read(1)
    elif tag == b"data":
        remaining = size
        break
    else:
        stream.read(size + (size % 2))
if rate != 16000 or bits != 16 or channels not in (1, 2):
    sys.stderr.write("the stand-in decodes 16 kHz 16-bit WAV only\n")
    finish(1)
stall_after = int(os.environ.get("FAKE_FFMPEG_STALL_AFTER_BYTES", "-1"))
minimum = float(os.environ.get("FAKE_FFMPEG_MIN_S", "0"))
frame = 2 * channels
total = 0
with open(out, "wb") as fh:
    while remaining > 0:
        block = stream.read(min(remaining, 960000 * channels))
        if not block:
            break
        remaining -= len(block)
        block = block[: len(block) // frame * frame]
        if channels == 2:
            import numpy as np
            pairs = np.frombuffer(block, dtype="<i2").reshape(-1, 2).astype(np.int32)
            block = (pairs.sum(axis=1) // 2).astype("<i2").tobytes()
        fh.write(block)
        total += len(block)
        sys.stdout.write("total_size=%d\nout_time_us=%d\nprogress=continue\n" % (total, total * 1000000 // 32000))
        sys.stdout.flush()
        if 0 <= stall_after <= total:
            fh.flush()
            time.sleep(3600)
elapsed = time.time() - started
if elapsed < minimum:
    time.sleep(minimum - elapsed)
sys.stdout.write("progress=end\n")
sys.stdout.flush()
finish(0)
'''

_FAKE_FFPROBE = r'''#!{python}
"""ffprobe stand-in: the duration of a WAV named by -i."""
import os, sys
args = sys.argv[1:]
if "-format_whitelist" not in args or "-protocol_whitelist" not in args:
    sys.exit(2)
if os.environ.get("FAKE_FFPROBE_FAIL"):
    sys.exit(1)
source = args[args.index("-i") + 1]
with open(source, "rb") as fh:
    head = fh.read(12)
    if head[:4] != b"RIFF":
        sys.exit(1)
    rate = None
    while True:
        chunk = fh.read(8)
        if len(chunk) < 8:
            sys.exit(1)
        tag, size = chunk[:4], int.from_bytes(chunk[4:8], "little")
        if tag == b"fmt ":
            body = fh.read(size)
            byte_rate = int.from_bytes(body[8:12], "little")
        elif tag == b"data":
            print(size / byte_rate)
            sys.exit(0)
        else:
            fh.seek(size, 1)
'''


def install_fake_media_tools(directory: str) -> Dict[str, str]:
    """Write executable `ffmpeg` and `ffprobe` stand-ins; return their paths."""
    os.makedirs(directory, exist_ok=True)
    paths = {}
    for name, body in (("ffmpeg", _FAKE_FFMPEG), ("ffprobe", _FAKE_FFPROBE)):
        path = os.path.join(directory, name)
        with open(path, "w") as fh:
            fh.write(body.format(python=sys.executable))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP)
        paths[name] = path
    return paths


def read_tool_log(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ------------------------------------------------------------ verdicts --


def compare_to_script(segments: Sequence[Any], script: Script) -> Dict[str, Any]:
    """Word-for-word and time-for-time comparison of a stitched transcript
    against the words actually spoken.

    Returns `{"equal": bool, "first_difference": index|None, "duplicates": n,
    "missing": n, "max_start_error_s", "max_end_error_s", "words": n}`. The
    timestamp errors compare each segment's start with its first word's true
    start and its end with its last word's true end.
    """
    out: List[Tuple[str, int]] = []
    for number, segment in enumerate(segments):
        text = segment.text if hasattr(segment, "text") else segment["text"]
        out.extend((w, number) for w in normalized_words(text))
    truth = [norm_word(w.text) for w in script.words]
    produced = [w for w, _ in out]
    first_difference = None
    for i, (a, b) in enumerate(zip(produced, truth)):
        if a != b:
            first_difference = i
            break
    if first_difference is None and len(produced) != len(truth):
        first_difference = min(len(produced), len(truth))
    report: Dict[str, Any] = {
        "equal": produced == truth,
        "first_difference": first_difference,
        "words": len(produced),
        "truth_words": len(truth),
        "duplicates": max(0, len(produced) - len(truth)),
        "missing": max(0, len(truth) - len(produced)),
    }
    if first_difference is not None:
        lo = max(0, first_difference - 6)
        report["context_produced"] = produced[lo : first_difference + 6]
        report["context_truth"] = truth[lo : first_difference + 6]
        report["context_time_s"] = script.words[min(first_difference, len(script.words) - 1)].start_s
        return report
    starts: List[float] = []
    ends: List[float] = []
    position = 0
    for number, segment in enumerate(segments):
        text = segment.text if hasattr(segment, "text") else segment["text"]
        count = len(normalized_words(text))
        if count == 0:
            continue
        first, last = script.words[position], script.words[position + count - 1]
        start = segment.start_s if hasattr(segment, "start_s") else segment["start"]
        end = segment.end_s if hasattr(segment, "end_s") else segment["end"]
        starts.append(abs(start - first.start_s))
        ends.append(abs(end - last.end_s))
        position += count
    report["max_start_error_s"] = round(max(starts), 3) if starts else 0.0
    report["max_end_error_s"] = round(max(ends), 3) if ends else 0.0
    report["p99_start_error_s"] = round(float(np.percentile(starts, 99)), 3) if starts else 0.0
    return report


def rss_anon_bytes() -> int:
    """This process's anonymous resident memory (heap, numpy buffers) — NOT the
    file pages of a memory-mapped recording, which the kernel reclaims."""
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("RssAnon:"):
                return int(line.split()[1]) * 1024
    return 0
