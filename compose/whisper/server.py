"""openai/whisper-large-v3, served on a GB10.

One copy of this runs per node that carries speech-to-text — the worker
always, the head as well when a workspace dictates concurrently. The copies
are REPLICAS: each holds the whole model and answers whole clips, and neither
knows the other exists. Balancing between them is the orchestrator's job
(`RoutedProvider` in orchestrator/app/asr.py), not this file's.

    POST /v1/audio/transcriptions   multipart: file, [model], [language],
                                    [response_format=json|text|verbose_json],
                                    [no_speech_check=true|false]
    GET  /health                    readiness, and what is actually loaded
    GET  /v1/models                 the one model, for tooling that asks

ONE MODEL, AND IT IS NOT CONFIGURABLE. `MODEL_ID` and `MODEL_REVISION` below
are constants, not environment variables. That is the whole point: the
platform previously carried an ASR_BACKEND selector so two candidate engines
could be A/B'd, and a selector left behind after the decision is a lever a
stale environment can pull. There is one engine now, it is pinned to an exact
Hugging Face revision, and changing it means editing this file and rebuilding
— which is a code review rather than a restart with a different variable.

WHY LARGE-V3 AND NOT TURBO. TechSara is an analytical assistant: a person
dictates "don't delete the Salesforce account" and the difference between the
transcript keeping "don't" and losing it is the difference between the right
action and the wrong one. Large-v3 is the accuracy checkpoint. Turbo is the
same encoder with a four-layer decoder and it is faster; it was deliberately
not installed.

LONG FORM IS SEQUENTIAL, WHICH IS A CHOICE. Whisper sees thirty seconds at a
time. There are two documented ways past that. The CHUNKED algorithm cuts the
audio into fixed windows and transcribes them independently — fast, batchable,
and it decides sentence boundaries with a stride rather than with the model.
The SEQUENTIAL algorithm slides the window using the model's own timestamp
predictions, so each window starts where the last utterance actually ended and
the decoder carries its context across the seam. The upstream model card
recommends sequential when accuracy matters more than speed, which here it
does. Concretely: passing `chunk_length_s` to the pipeline selects chunked.
This module does not pass it. That single omission IS the long-form strategy,
so it is written down rather than left to be inferred.

WHERE THE AUDIO GOES. Into memory, through ffmpeg, into the model, and out of
scope. Nothing is written to disk — `_decode` streams the upload through
ffmpeg's stdin and reads the waveform off stdout, so a recording never becomes
a file that somebody has to remember to delete.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from contextlib import asynccontextmanager
from typing import Optional

import numpy as np
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("whisper")

# --------------------------------------------------------------------------
# The model. Constants, deliberately — see the module docstring.
# --------------------------------------------------------------------------

MODEL_ID = "openai/whisper-large-v3"
#: The exact commit on the Hub, resolved 2026-09-08. `main` moves; a result
#: file that says "main" cannot be reproduced six months later.
MODEL_REVISION = "06f233fe06e710322aca913c1bc4249a0d71fce1"
MODEL_LICENSE = "apache-2.0"

#: Whisper's fixed input rate. Not a tunable — the feature extractor's mel
#: filterbank is built for it.
SAMPLE_RATE = 16000

#: Host networking on the worker, so the process binds its own address rather
#: than Docker publishing a port to it. That is not style: a published
#: `192.168.9.68:PORT` binding fails at container-create time after a reboot,
#: before the NIC has an address, and `restart: unless-stopped` never engages
#: because the process never ran. Binding here makes the same race an ordinary
#: process failure that the restart policy retries. The monitoring stack
#: learned this on 2026-08-31; see compose/compose.monitoring-worker.yaml.
BIND_HOST = os.environ.get("WHISPER_BIND", "127.0.0.1")
BIND_PORT = int(os.environ.get("WHISPER_PORT", "30007"))

#: Refuse rather than swap. A ten-minute clip is already the orchestrator's
#: ceiling; this is the same promise enforced where the audio actually lands.
MAX_AUDIO_SECONDS = float(os.environ.get("WHISPER_MAX_AUDIO_SECONDS", "600"))

#: One clip at a time on the GPU. The orchestrator's pool already bounds how
#: many requests are in flight; this bounds what reaches the device, so a
#: burst queues here instead of competing for VRAM with the main model that
#: shares this node.
_gpu_lock = asyncio.Lock()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Load the model once, before the first request.

    THE FAILURE IS RECORDED, NOT RAISED. A container that exits when a model
    download fails restart-loops and tells nobody why; /health carries the
    reason instead, and the orchestrator reports the engine as not ready.
    """
    try:
        await asyncio.get_running_loop().run_in_executor(None, _load)
    except Exception as exc:  # noqa: BLE001
        _state["error"] = f"{type(exc).__name__}: {exc}"
        log.exception("model failed to load")
    yield


app = FastAPI(title="whisper-large-v3", docs_url=None, redoc_url=None,
              lifespan=_lifespan)

_state: dict = {
    "ready": False,
    "pipe": None,
    "model": None,
    "processor": None,
    "sot_id": None,
    "nospeech_id": None,
    "dtype": None,
    "device": None,
    "load_seconds": None,
    "error": None,
}


def _pick_dtype() -> "torch.dtype":
    """The safest GPU dtype on this device, checked rather than assumed.

    Whisper's weights are published in float16 and the upstream example uses
    it on CUDA, so that is the target. GB10 is Blackwell and supports it — but
    "supports float16" is a fact about a device, and this runs on exactly one
    kind of device only because somebody said so. Asking costs nothing and
    turns a wrong assumption into a startup log line instead of NaNs.
    """
    if not torch.cuda.is_available():
        return torch.float32
    major, _minor = torch.cuda.get_device_capability(0)
    # Every CUDA GPU this could plausibly run on has fp16 tensor cores from
    # Volta (7.0) onward. Below that, float32 is correct and slow, which is
    # the right order of priorities.
    return torch.float16 if major >= 7 else torch.float32


def _load() -> None:
    """Load the model onto the GPU. Called once, at startup."""
    from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

    started = time.perf_counter()
    if not torch.cuda.is_available():
        # CPU inference for a 1.55B encoder-decoder is not a degraded mode, it
        # is a different product. Fail loudly: /health will say so and the
        # orchestrator will report the engine as not ready.
        raise RuntimeError(
            "no CUDA device is visible to this container — refusing to run "
            "Whisper Large-v3 on CPU"
        )

    dtype = _pick_dtype()
    device = "cuda:0"
    log.info(
        "loading %s (revision %s) as %s on %s (%s, sm_%s%s)",
        MODEL_ID, MODEL_REVISION[:12], dtype, device,
        torch.cuda.get_device_name(0), *torch.cuda.get_device_capability(0),
    )

    model = AutoModelForSpeechSeq2Seq.from_pretrained(
        MODEL_ID,
        revision=MODEL_REVISION,
        dtype=dtype,
        low_cpu_mem_usage=True,
        use_safetensors=True,
    )
    model.to(device)
    model.eval()
    processor = AutoProcessor.from_pretrained(MODEL_ID, revision=MODEL_REVISION)

    # NO `chunk_length_s`. Its absence selects the sequential long-form
    # algorithm; passing it would select the chunked one. See the docstring.
    pipe = pipeline(
        "automatic-speech-recognition",
        model=model,
        tokenizer=processor.tokenizer,
        feature_extractor=processor.feature_extractor,
        dtype=dtype,
        device=device,
    )

    _state.update(
        ready=True,
        pipe=pipe,
        model=model,
        processor=processor,
        sot_id=processor.tokenizer.convert_tokens_to_ids("<|startoftranscript|>"),
        nospeech_id=processor.tokenizer.convert_tokens_to_ids("<|nospeech|>"),
        dtype=str(dtype).replace("torch.", ""),
        device=torch.cuda.get_device_name(0),
        load_seconds=round(time.perf_counter() - started, 2),
        error=None,
    )
    log.info("model ready in %.1fs", _state["load_seconds"])


def _decode(payload: bytes) -> np.ndarray:
    """Container bytes to a 16 kHz mono float32 waveform, through ffmpeg.

    ALLOWED, and nothing more: demux, decode, downmix to mono, resample, and
    convert dtype. No noise suppression, no speech enhancement, no gain. Those
    change what the model hears, and the honest thing to measure first is what
    Whisper does with the audio the browser actually recorded.

    `-nostdin` matters: without it ffmpeg competes for the parent's stdin and
    a container run non-interactively can hang instead of failing.
    """
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
    """The language to force, or None to let Whisper decide.

    AUTO IS THE DEFAULT AND MUST STAY IT. This deployment is English, Indian
    English, Hindi and Hinglish in one conversation; forcing a language would
    make one of those four the only one that works. `""`, `"auto"` and the
    absent field all mean detect. Anything else is passed to Whisper as-is —
    it accepts both `"en"` and `"english"` — so an engine that grows a new
    language needs no change here.
    """
    if raw is None:
        return None
    cleaned = raw.strip().lower()
    if cleaned in ("", "auto", "none", "null"):
        return None
    return cleaned


#: Whisper's own documented no-speech threshold, and the value OpenAI's
#: decoder ships with. NOT tuned here: measured on this GPU on 2026-09-08
#: across the whole validation set, digital silence scores 0.708 and the most
#: marginal real utterance — a three-second Hinglish clip — scores 0.238. The
#: documented default sits in the middle of a 0.47-wide gap, so it separates
#: the two without being fitted to either.
NO_SPEECH_THRESHOLD = float(os.environ.get("WHISPER_NO_SPEECH_THRESHOLD", "0.6"))


def _no_speech_probability(audio: np.ndarray) -> float:
    """How sure the model is that nobody is speaking.

    THE MODEL'S OWN OUTPUT, NOT A RULE ABOUT ITS TEXT. Whisper is known to
    answer digital silence with a plausible sentence — this deployment gets
    "Thank you." every time — and the obvious fix, dropping transcripts that
    equal some list of stock phrases, is a trap: it also deletes a person who
    really did say thank you, and it never generalises to the next phrase the
    model invents. `<|nospeech|>` is the token Whisper emits when it hears no
    speech, and its probability at the first decoding step is exactly what the
    reference implementation gates on.

    Measured rather than assumed: the documented long-form recipe
    (`no_speech_threshold` with the temperature fallback) does NOT reach this
    case, because transformers only applies those thresholds on the segmented
    long-form path and a ten-second clip never takes it. Hence the explicit
    check here.

    COSTS ONE ENCODER PASS over the first 30 seconds. Whisper's receptive
    field is 30 seconds anyway, so a longer recording is judged on its opening
    window — which is the window a recording of nothing but silence is
    entirely made of.
    """
    processor, model = _state["processor"], _state["model"]
    feats = processor(
        audio[: 30 * SAMPLE_RATE], sampling_rate=SAMPLE_RATE, return_tensors="pt"
    ).input_features.to(model.device, model.dtype)
    ids = torch.tensor([[_state["sot_id"]]], device=model.device)
    with torch.no_grad():
        logits = model(input_features=feats, decoder_input_ids=ids).logits
    return float(logits[0, 0].float().softmax(-1)[_state["nospeech_id"]])


def _iso_code(language: Optional[str]) -> Optional[str]:
    """"english" -> "en", and "en" -> "en". Whisper's own table, not ours.

    The pipeline reports a language by NAME; the response contract carries a
    code as well, because that is what the composer's draft is tagged with.
    `TO_LANGUAGE_CODE` ships inside the Whisper tokenizer, so the mapping is
    the model's rather than a list somebody here has to keep in step with it —
    the previous engine left behind exactly such a hand-maintained table and
    it was the first thing to rot.
    """
    if not language:
        return None
    candidate = language.strip().lower()
    try:
        from transformers.models.whisper.tokenization_whisper import TO_LANGUAGE_CODE
    except Exception:  # noqa: BLE001
        return candidate if len(candidate) == 2 else None
    if candidate in TO_LANGUAGE_CODE:
        return TO_LANGUAGE_CODE[candidate]
    # Already a code the model knows.
    if candidate in set(TO_LANGUAGE_CODE.values()):
        return candidate
    return None


def _run(
    audio: np.ndarray,
    language: Optional[str],
    *,
    segments: bool = False,
    no_speech_check: bool = True,
) -> dict:
    """One transcription, on the GPU, in this thread.

    `segments` asks for timestamped segments in the reply (the caller wants
    them for subtitles, chapters or a searchable index — video analysis does).
    `no_speech_check=False` skips the silence gate: the gate judges the FIRST
    thirty seconds, which is right for a dictation clip and wrong for a window
    cut out of a longer recording by a voice-activity detector that has
    already decided there is speech in it — a window that opens on a pause
    would otherwise come back empty, with everything said after the pause
    silently lost. A caller that has run its own VAD says so and takes the
    responsibility.
    """
    pipe = _state["pipe"]
    seconds = audio.size / SAMPLE_RATE

    silence = _no_speech_probability(audio) if no_speech_check else 0.0
    if no_speech_check and silence > NO_SPEECH_THRESHOLD:
        # Nothing was said. An empty draft is the honest answer: the composer
        # leaves whatever the person had already typed alone, which is exactly
        # what should happen when they pressed the microphone and then did not
        # speak.
        return {
            "text": "", "language": None, "language_code": None,
            "duration": round(seconds, 3), "no_speech_prob": round(silence, 4),
            "segments": [],
        }

    generate_kwargs: dict = {
        # TRANSCRIBE, NEVER TRANSLATE. Whisper's other task silently rewrites
        # Hindi into English, which for a dictation box means the words a
        # person said are not the words they get back.
        "task": "transcribe",
    }
    if language is not None:
        generate_kwargs["language"] = language

    # NOTHING ELSE. The upstream model card also documents a long-form recipe
    # — condition_on_prev_tokens, compression_ratio_threshold, a temperature
    # fallback, logprob and no_speech thresholds — and it was measured here on
    # 2026-09-08 before being rejected. It did not fix the silence
    # hallucination it is usually reached for (see `_no_speech_probability`),
    # it was slower on every clip, and on the 128-second Hindi-English
    # recording it was actively WORSE: 1080 characters against 1364, with
    # whole utterances dropped and one reordered, in 71s instead of 38s. The
    # temperature fallback re-rolls a segment whose score looks poor and on
    # code-switched speech it re-rolls into something further from what was
    # said. Defaults win here, and that is a measurement rather than an
    # omission. The card's `max_new_tokens: 448` is separately unusable on
    # transformers 5.16: the decoder has 448 positions in total, so 448 new
    # tokens plus the three forced prompt tokens overflows and generate()
    # refuses outright.

    kwargs: dict = {"return_language": True, "generate_kwargs": generate_kwargs}
    # SEQUENTIAL LONG-FORM REQUIRES TIMESTAMPS — they are how the algorithm
    # decides where the next 30-second window begins. This is a PIPELINE
    # argument: passed inside generate_kwargs instead, transformers does not
    # see it and every clip over 30 seconds fails with "You have passed more
    # than 3000 mel input features ... Please either pass return_timestamps".
    # That is not a hypothetical; it is what this service did on its first
    # deploy, and it is why the boundary is >= rather than >.
    if seconds >= 30.0 or segments:
        kwargs["return_timestamps"] = True

    result = pipe({"raw": audio, "sampling_rate": SAMPLE_RATE}, **kwargs)

    text = (result.get("text") or "").strip()
    detected = language
    if detected is None:
        # `return_language` reports per chunk. The clip is one person speaking,
        # so the first chunk that names a language names the clip's.
        for chunk in result.get("chunks") or []:
            if chunk.get("language"):
                detected = str(chunk["language"])
                break
    out = {
        "text": text,
        "language": detected,
        "language_code": _iso_code(detected),
        "duration": round(seconds, 3),
        "no_speech_prob": round(silence, 4),
    }
    if segments:
        out["segments"] = _segments(result.get("chunks") or [], seconds)
    return out


def _segments(chunks: list, seconds: float) -> list:
    """transformers' chunks -> OpenAI verbose_json-shaped segments.

    Each chunk carries `timestamp: (start, end)` in seconds from the start of
    THIS clip. `end` is None when the model ran out of audio mid-segment (the
    last one, usually) — the clip's own length is the honest end. The
    sequential long-form algorithm can also emit a segment that begins before
    the previous one ended when a window boundary falls inside a word; those
    are kept in order and clamped so `start <= end` always holds, because a
    subtitle file with a negative-length cue is a broken file.
    """
    out = []
    prev_end = 0.0
    for i, chunk in enumerate(chunks):
        text = (chunk.get("text") or "").strip()
        if not text:
            continue
        ts = chunk.get("timestamp") or (None, None)
        start = ts[0] if ts[0] is not None else prev_end
        end = ts[1] if ts[1] is not None else seconds
        start = max(0.0, min(float(start), seconds))
        end = max(start, min(float(end), seconds))
        out.append({
            "id": i,
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text,
            "language": _iso_code(chunk.get("language")) if chunk.get("language") else None,
        })
        prev_end = end
    return out


@app.get("/health")
async def health() -> dict:
    return {
        "ready": bool(_state["ready"]),
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "device": _state["device"],
        "dtype": _state["dtype"],
        "load_seconds": _state["load_seconds"],
        "long_form": "sequential",
        "task": "transcribe",
        "no_speech_threshold": NO_SPEECH_THRESHOLD,
        "error": _state["error"],
    }


@app.get("/v1/models")
async def models() -> dict:
    return {
        "object": "list",
        "data": [{"id": MODEL_ID, "object": "model", "owned_by": "openai"}],
    }


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    # Accepted and ignored, in that order. The field is part of the
    # OpenAI-compatible shape every client and the benchmark scorer already
    # send; honouring it would be a model selector, which is exactly what this
    # deployment does not have.
    model: str = Form(default=MODEL_ID),
    language: Optional[str] = Form(default=None),
    # "json" (text only), "text" (a plain-text body) or "verbose_json" —
    # OpenAI's name for the shape that also carries timestamped segments.
    response_format: str = Form(default="json"),
    # Off only for callers that ran their own voice-activity detection over a
    # longer recording; see `_run`. Dictation leaves it on.
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

    async with _gpu_lock:
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
        except Exception as exc:  # noqa: BLE001
            log.exception("transcription failed")
            raise HTTPException(status_code=500, detail=f"transcription failed: {exc}") from None

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
