"""openai/whisper-large-v3 on the worker's CPU cores: the overflow replica.

The GPU replicas (compose/whisper/server.py, one per Spark) are the speech engine. This is a third
copy of the SAME model that runs on ten Cortex-X925 cores of the worker instead of a GPU, and the
orchestrator sends it a clip only when every GPU replica is already decoding one and the clip can
still finish inside its deadline at this copy's measured speed (`RoutedProvider` in
orchestrator/app/asr.py, ASR_CPU_*). It exists so a burst of speech costs waiting time on the CPU
instead of more of the chat model's GPU time: the chat model is tensor-parallel across both Sparks
and a busy GPU replica on either one slows every chat answer.

THE CONTRACT IS compose/whisper/server.py's, EXACTLY. Same routes, same form fields, the same
response fields (text, language, language_code, duration, no_speech_prob, segments, processing_ms,
task), the same no-speech gate at the same threshold, the same limits and the same error statuses,
so the orchestrator's client (`VLLMAudioProvider`) cannot tell which replica answered except by
the time it took. Anything added here is additive (/health carries a few more keys).

    POST /v1/audio/transcriptions   multipart: file, [model], [language],
                                    [response_format=json|text|verbose_json],
                                    [no_speech_check=true|false]
    GET  /health                    readiness, and what is actually loaded
    GET  /v1/models                 the one model, for tooling that asks

THE SAME MODEL, NOT A SMALLER ONE. The weights are openai/whisper-large-v3 at the revision the GPU
replicas pin, converted to whisper.cpp's format by whisper.cpp's own converter and quantised to
q8_0 (8-bit blocks of 32 weights, one fp16 scale each). Measured on this project's sets before
this file existed (docs/voice/CPU-REPLICA.md): the word error rate is the GPU replicas' within
noise, and q8_0 was the fastest variant on these cores. `MODEL_SHA256` pins the exact file.

THE DECODE SETTINGS ARE THE GPU REPLICAS', ONE BY ONE, and they are set in wcpp_worker.cpp next to
the reason for each: greedy, no temperature fallback, no conditioning on previous text, transcribe
never translate, timestamps only for segments or clips of 30 s and more, sequential long form.

WHY A SEPARATE DECODER PROCESS. whisper.cpp is a C++ library. The decoder runs in `wcpp-worker`,
one long-lived child process that holds the model; this file keeps the HTTP half identical to the
GPU replica's (FastAPI, ffmpeg, the limits, the error shapes) and talks to it over a pipe. A crash
in native code ends one request and restarts the child; it cannot take the HTTP server with it.

WHERE THE AUDIO GOES. Into memory, through ffmpeg, into the decoder's pipe, and out of scope, as on
the GPU replica. Starlette spools a multipart part over 1 MiB to /tmp, which is a tmpfs here
(compose/compose.whisper-cpu.yaml), so a recording never reaches a disk on this service either.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("whisper-cpu")

# --------------------------------------------------------------------------
# The model. Constants, as on the GPU replica: changing them is a code review.
# --------------------------------------------------------------------------

MODEL_ID = "openai/whisper-large-v3"
#: The GPU replicas' pinned revision (compose/whisper/server.py). The CPU file is made FROM it.
MODEL_REVISION = "06f233fe06e710322aca913c1bc4249a0d71fce1"
MODEL_LICENSE = "apache-2.0"
#: whisper.cpp at the commit the image builds (compose/whisper-cpu/Dockerfile) and the one that
#: converted and quantised the weights. The file format is tied to it.
WHISPER_CPP_VERSION = "v1.9.4"
WHISPER_CPP_COMMIT = "927cfce34f31707e17f2bff35c349632fb9e2c3a"
QUANTIZATION = "q8_0"
#: SHA-256 of ggml-large-v3-q8_0.bin as scripts/whisper-cpu.sh produces it from MODEL_REVISION.
#: Checked at startup: a different file is refused, not served.
MODEL_SHA256 = "37efc6b68f300ab717465685f7c3e175a66c11cf92bb3ab9912e86f4116c465e"
MODEL_FILE = os.environ.get("WHISPER_CPU_MODEL_FILE", "/models/ggml-large-v3-q8_0.bin")
WORKER_BIN = os.environ.get("WHISPER_CPU_WORKER", "/usr/local/bin/wcpp-worker")
#: Eight threads: the service's CPU quota is eight cores (cpus: 8) inside a ten-core cpuset.
THREADS = max(1, int(os.environ.get("WHISPER_CPU_THREADS", "8")))

SAMPLE_RATE = 16000

#: Host networking on the worker, binding the management address itself; see
#: compose/compose.whisper.yaml for why a published port does not survive a reboot.
BIND_HOST = os.environ.get("WHISPER_BIND", "127.0.0.1")
BIND_PORT = int(os.environ.get("WHISPER_PORT", "30008"))

#: The GPU replica's ceiling, the same promise where the audio lands.
MAX_AUDIO_SECONDS = float(os.environ.get("WHISPER_MAX_AUDIO_SECONDS", "600"))

#: The GPU replica's threshold (compose/whisper/server.py NO_SPEECH_THRESHOLD), measured the same
#: way: P(<|nospeech|>) at <|startoftranscript|> over the first 30 s. Digital silence scored 0.708
#: on the GPU and 0.710 here.
NO_SPEECH_THRESHOLD = float(os.environ.get("WHISPER_NO_SPEECH_THRESHOLD", "0.6"))

#: One clip at a time, as on the GPU replica. The cores are the resource here, and two decodes
#: sharing eight threads would each take twice as long.
_cpu_lock = asyncio.Lock()

#: Worker failures in a row before the process gives up and lets Docker restart it (the GPU
#: replica's CUDA_DEATH_THRESHOLD, for a native decoder that keeps dying).
WORKER_DEATH_THRESHOLD = int(os.environ.get("WHISPER_CPU_WORKER_DEATH_THRESHOLD", "3"))

_state: dict = {
    "ready": False,
    "worker": None,
    "load_seconds": None,
    "error": None,
    "system_info": None,
    "worker_failures": 0,
    "busy_since": None,
    "decoded_audio_s": 0.0,
    "decode_wall_s": 0.0,
}
_worker_io = threading.Lock()


def _sha256(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _start_worker() -> None:
    """Start wcpp-worker and wait for its ready line. Raises on failure."""
    proc = subprocess.Popen(
        [WORKER_BIN, "--model", MODEL_FILE, "--threads", str(THREADS)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "WCPP_QUIET": "1", "OMP_NUM_THREADS": str(THREADS)},
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    try:
        hello = json.loads(line)
    except (ValueError, TypeError):
        proc.kill()
        raise RuntimeError(f"wcpp-worker did not start: {line[:200]!r}") from None
    if not hello.get("ready"):
        proc.kill()
        raise RuntimeError(f"wcpp-worker refused the model: {hello.get('error')}")
    _state["worker"] = proc
    _state["system_info"] = hello.get("system_info")


def _load() -> None:
    """Check the pinned file, start the decoder. Called once, at startup."""
    started = time.perf_counter()
    if not os.path.isfile(MODEL_FILE):
        raise RuntimeError(f"{MODEL_FILE} is missing; scripts/whisper-cpu.sh up converts it")
    if os.environ.get("WHISPER_CPU_SKIP_SHA256") != "1":
        found = _sha256(MODEL_FILE)
        if found != MODEL_SHA256:
            raise RuntimeError(
                f"{MODEL_FILE} has SHA-256 {found[:16]}…, not the pinned {MODEL_SHA256[:16]}…; "
                "refusing to serve an unreviewed model"
            )
    _start_worker()
    _state.update(ready=True, load_seconds=round(time.perf_counter() - started, 2), error=None)
    log.info("model ready in %.1fs (%s, %d threads)", _state["load_seconds"], QUANTIZATION, THREADS)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """THE FAILURE IS RECORDED, NOT RAISED, as on the GPU replica: /health carries the reason."""
    try:
        await asyncio.get_running_loop().run_in_executor(None, _load)
    except Exception as exc:  # noqa: BLE001
        _state["error"] = f"{type(exc).__name__}: {exc}"
        log.exception("model failed to load")
    yield
    proc = _state.get("worker")
    if proc is not None:
        proc.kill()


app = FastAPI(title="whisper-large-v3 (cpu)", docs_url=None, redoc_url=None, lifespan=_lifespan)


class WorkerDied(RuntimeError):
    """The decoder process ended or answered out of step: it is restarted."""


def _ask_worker(audio: np.ndarray, language: Optional[str], *, timestamps: bool, gate: bool) -> dict:
    """One request to wcpp-worker. Runs in an executor thread, under `_cpu_lock`."""
    with _worker_io:
        proc = _state.get("worker")
        if proc is None or proc.poll() is not None:
            _start_worker()
            proc = _state["worker"]
        header = {
            "n_samples": int(audio.size),
            "language": language,
            "timestamps": bool(timestamps),
            "gate": bool(gate),
            "threshold": NO_SPEECH_THRESHOLD,
        }
        try:
            proc.stdin.write((json.dumps(header) + "\n").encode())
            proc.stdin.write(np.ascontiguousarray(audio, dtype="<f4").tobytes())
            proc.stdin.flush()
            line = proc.stdout.readline()
        except (BrokenPipeError, OSError) as exc:
            proc.kill()
            _state["worker"] = None
            raise WorkerDied(f"decoder pipe failed: {exc}") from None
        if not line:
            proc.kill()
            _state["worker"] = None
            raise WorkerDied(f"decoder exited (status {proc.poll()})")
        try:
            return json.loads(line)
        except ValueError:
            proc.kill()
            _state["worker"] = None
            raise WorkerDied("decoder answered out of step") from None


def _decode(payload: bytes) -> np.ndarray:
    """Container bytes to a 16 kHz mono float32 waveform, through ffmpeg — the GPU replica's
    `_decode`, unchanged: demux, decode, downmix, resample, convert, and nothing else."""
    proc = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-threads", "1",
            "-i", "pipe:0",
            "-f", "f32le", "-acodec", "pcm_f32le",
            "-ac", "1", "-ar", str(SAMPLE_RATE),
            "pipe:1",
        ],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise HTTPException(
            status_code=400,
            detail=f"could not decode audio: {detail[-1] if detail else 'ffmpeg failed'}",
        )
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    if audio.size == 0:
        raise HTTPException(status_code=400, detail="audio decoded to zero samples")
    return audio


def _language_arg(raw: Optional[str]) -> Optional[str]:
    """The GPU replica's `_language_arg`: "", "auto", "none", "null" and absent mean detect."""
    if raw is None:
        return None
    cleaned = raw.strip().lower()
    if cleaned in ("", "auto", "none", "null"):
        return None
    return cleaned


def _run(
    audio: np.ndarray,
    language: Optional[str],
    *,
    segments: bool = False,
    no_speech_check: bool = True,
) -> dict:
    """One transcription, in the decoder process. The GPU replica's `_run`, field for field."""
    seconds = audio.size / SAMPLE_RATE
    started = time.perf_counter()
    reply = _ask_worker(
        audio,
        language,
        # SEQUENTIAL LONG FORM NEEDS TIMESTAMPS, as on the GPU replica (>= 30 s, or segments).
        timestamps=(seconds >= 30.0 or segments),
        gate=no_speech_check,
    )
    if not reply.get("ok"):
        raise RuntimeError(str(reply.get("error") or "the decoder failed"))
    _state["decoded_audio_s"] += seconds
    _state["decode_wall_s"] += time.perf_counter() - started
    # With the gate off the GPU replica reports 0.0 (it never computed the probability).
    silence = float(reply.get("no_speech_prob") or 0.0) if no_speech_check else 0.0
    if reply.get("gated"):
        return {
            "text": "", "language": None, "language_code": None,
            "duration": round(seconds, 3), "no_speech_prob": round(silence, 4),
            "segments": [],
        }
    chunks = reply.get("segments") or []
    text = "".join(str(c.get("text") or "") for c in chunks).strip()
    code = reply.get("language")
    # The GPU replica reports the pipeline's language NAME ("english") when it detected it, and the
    # caller's own string when the caller forced one; the code comes from the model's table.
    detected = language if language is not None else reply.get("language_name")
    out = {
        "text": text,
        "language": detected,
        "language_code": code,
        "duration": round(seconds, 3),
        "no_speech_prob": round(silence, 4),
    }
    if segments:
        out["segments"] = _segments(chunks, seconds, code)
    return out


def _segments(chunks: list, seconds: float, code: Optional[str]) -> list:
    """The GPU replica's `_segments`: ordered, clamped so start <= end, empty text skipped."""
    out = []
    prev_end = 0.0
    for i, chunk in enumerate(chunks):
        text = (chunk.get("text") or "").strip()
        if not text:
            continue
        start = chunk.get("t0")
        end = chunk.get("t1")
        start = float(start) if start is not None else prev_end
        end = float(end) if end is not None else seconds
        start = max(0.0, min(start, seconds))
        end = max(start, min(end, seconds))
        out.append({
            "id": i,
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text,
            "language": code,
        })
        prev_end = end
    return out


@app.get("/health")
async def health() -> dict:
    decoded = _state["decoded_audio_s"]
    return {
        "ready": bool(_state["ready"]),
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "device": f"cpu ({THREADS} threads, whisper.cpp {WHISPER_CPP_VERSION})",
        "dtype": QUANTIZATION,
        "load_seconds": _state["load_seconds"],
        "long_form": "sequential",
        "task": "transcribe",
        "no_speech_threshold": NO_SPEECH_THRESHOLD,
        "error": _state["error"],
        # The GPU replica's key, kept so a reader of either /health parses both; always 0 here.
        "cuda_failures": 0,
        # Additive: what only this replica can say.
        "backend": "whisper.cpp",
        "backend_commit": WHISPER_CPP_COMMIT,
        "model_sha256": MODEL_SHA256,
        "threads": THREADS,
        "busy": _cpu_lock.locked(),
        "worker_failures": int(_state["worker_failures"]),
        "decoded_audio_s": round(decoded, 1),
        "seconds_per_audio_second": round(_state["decode_wall_s"] / decoded, 3) if decoded else None,
    }


@app.get("/v1/models")
async def models() -> dict:
    return {
        "object": "list",
        "data": [{"id": MODEL_ID, "object": "model", "owned_by": "openai"}],
    }


def _exit_soon(code: int = 3, delay_s: float = 1.0) -> None:
    """Let the in-flight response leave, then end the process for a restart."""
    loop = asyncio.get_running_loop()
    loop.call_later(delay_s, os._exit, code)


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    # Accepted and ignored, as on the GPU replica: there is one model.
    model: str = Form(default=MODEL_ID),
    language: Optional[str] = Form(default=None),
    response_format: str = Form(default="json"),
    no_speech_check: bool = Form(default=True),
) -> JSONResponse:
    if response_format not in ("json", "text", "verbose_json"):
        raise HTTPException(status_code=400, detail=f"unknown response_format {response_format!r}")
    if not _state["ready"]:
        raise HTTPException(
            status_code=503,
            detail=_state["error"] or "model is still loading",
        )

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="empty upload")

    started = time.perf_counter()
    loop = asyncio.get_running_loop()
    audio = await loop.run_in_executor(None, _decode, payload)

    seconds = audio.size / SAMPLE_RATE
    if seconds > MAX_AUDIO_SECONDS:
        raise HTTPException(
            status_code=413,
            detail=f"audio is {seconds:.0f}s; the limit is {MAX_AUDIO_SECONDS:.0f}s",
        )

    async with _cpu_lock:
        _state["busy_since"] = time.monotonic()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: _run(
                    audio,
                    _language_arg(language),
                    segments=(response_format == "verbose_json"),
                    no_speech_check=no_speech_check,
                ),
            )
        except HTTPException:
            raise
        except WorkerDied as exc:
            _state["worker_failures"] += 1
            log.error("decoder died: %s (%d in a row)", exc, _state["worker_failures"])
            if _state["worker_failures"] >= WORKER_DEATH_THRESHOLD:
                _state["ready"] = False
                _state["error"] = f"decoder died {_state['worker_failures']} times in a row: {exc}; restarting"
                _exit_soon()
            raise HTTPException(status_code=503, detail=f"transcription failed: {exc}") from None
        except Exception as exc:  # noqa: BLE001
            log.exception("transcription failed")
            raise HTTPException(status_code=500, detail=f"transcription failed: {exc}") from None
        finally:
            _state["busy_since"] = None
        _state["worker_failures"] = 0

    result["processing_ms"] = int((time.perf_counter() - started) * 1000)
    if response_format == "text":
        return JSONResponse(content=result["text"], media_type="text/plain")
    if response_format == "verbose_json":
        result["task"] = "transcribe"
    return JSONResponse(content=result)


if __name__ == "__main__":
    import uvicorn

    log.info("binding %s:%s", BIND_HOST, BIND_PORT)
    uvicorn.run(app, host=BIND_HOST, port=BIND_PORT, log_level="info", access_log=False)
