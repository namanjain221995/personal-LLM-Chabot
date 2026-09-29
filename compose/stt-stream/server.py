"""Live dictation transcripts, streamed from the worker's CPU.

One copy of this runs on the WORKER Spark (spark-476e), in its own Compose
project (sf-local-ai-stt), pinned to the ten Cortex-X925 cores. Its only
client is the orchestrator's WebSocket gateway (orchestrator/app/voice_live.py):
the browser never reaches it, and nothing here knows about users, sessions or
cookies.

    WS   /v1/stream   one live stream per connection
    GET  /health      readiness, capacity and what is loaded (never a path)
    GET  /metrics     Prometheus text; every label is a closed set

WHY THE CPU AND NOT THE GPU. Chat is tensor-parallel across both Sparks, so any
sustained GPU work on either node slows every chat user (one saturated whisper
replica: 107 -> 53 tok/s). Measured on this node on 2026-09-29, Nemotron 3.5
ASR Streaming 0.6B int8 on sherpa-onnx decodes a 160 ms chunk in 31.6 ms on two
threads, four decode workers hold 12 real-time streams on about five cores, and
chat decode did not move (107.0-107.9 against a 107.2-108.9 baseline). All
twenty cores saturated DID cost chat 11-16%, which is why the container is
pinned to a cpuset, every profile refuses streams above its capacity, and the
engine refuses any stream its decode budget (STT_MAX_COST) cannot pay for.

WHISPER STAYS AUTHORITATIVE. The stored recording and its whisper transcript
are the record; this is a preview that may fail at any moment without losing
anything, which is why an overloaded engine refuses rather than queues.

PROFILES. A profile is one model at one chunk size with its own capacity.
STT_PROFILES lists them in ADMISSION ORDER: a stream goes to the first profile
that allows its language and has room. The deployed list
(compose/compose.stt-stream.yaml) sends "en" to the English-only Nemotron
Speech Streaming model (LibriSpeech WER 4.13% at 160 ms) and "auto" and "hi" to
the multilingual Nemotron 3.5 (LibriSpeech 6.23% with its own language ID,
FLEURS Hindi 8.58%, 7.48% pinned), each first at 160 ms chunks and then, for
the overflow, at 560 ms. Gujarati is not offered: both models read it at 104%
WER.

PROTOCOL (the gateway speaks it; build spec section 3). The upgrade carries
`Authorization: Bearer <STT_TOKEN>` or is refused BEFORE accept: the handler
closes without accepting, which uvicorn answers with a bare HTTP 403. Then:

    -> {"type":"start","sample_rate":16000,"encoding":"pcm_s16le",
        "first_sample":N,"first_u":K,"mode":"dictation"|"meeting",
        "language":"auto","frame_ms":40}
    <- {"type":"ready",...}
    -> binary frames: little-endian int16 mono PCM at 16 kHz, even length,
       2..16384 bytes
    <- {"type":"partial","u":k,"text":...,"start_sample":a,"end_sample":b,"compute_ms":c}
    <- {"type":"final","u":k,"text":...,"start_sample":a,"end_sample":b,"compute_ms":c,"endpoint_ms":e}
    <- {"type":"speech","active":true|false,"sample":n}
    -> {"type":"ping","t":x}   <- {"type":"pong","t":x}
    -> {"type":"flush"}        <- the last final, then {"type":"done"}, close 1000

A partial is the WHOLE current hypothesis of utterance k (replace, never
append); a final commits it. Samples are absolute session positions: the
stream's first sample is `first_sample`, so a reconnect that resumes from the
browser's ring buffer keeps one time base. An endpoint final's `end_sample`
lies inside the silence that closed it, so a reconnect that resumes from the
largest `end_sample` it saw neither repeats the utterance's last word nor
clips the next one's first. `mode` "meeting" waits longer for the end of an
utterance (STT_MEETING_ENDPOINT_S) and forces one out sooner
(STT_MEETING_MAX_UTTERANCE_S) than "dictation". `frame_ms` (optional, 40 when
absent) is the sender's frame length; it sizes the message ceiling below.
Refusals and failures send one `{"type":"error","code","message","retryable"}`
and close: 4400 protocol, unsupported language or mode, 4408 idle, 4413 frame
too large, 4429 capacity (a profile, or the engine's decode budget, is full)
or arrival rate (samples or messages), 4500 internal, 4503 still loading.

UTTERANCE TEXT, FOR CONSUMERS. The model decides how a sentence ends only
once it hears how the next one begins, so an utterance's text can START with
the closing mark of the previous one: ", and I was told", "। लेकिन". That mark
is kept (it is the previous sentence's comma or danda, and a Hindi transcript
without it is wrong). A consumer that joins utterances must therefore join
one whose text starts with a character of CLOSING_PUNCTUATION to the previous
utterance WITHOUT a space ("told" + ", and" -> "told, and"), and every other
one with a single space.

SEGMENTATION IS THE HEART OF IT, AND IT NEVER CUTS SPEECH. See `Segmenter`.

THREADING. ONE recognizer per profile, shared by STT_WORKERS decode threads
(build spec 8B: four threads decoding disjoint streams through one recognizer
gave 16/16 transcripts identical to a sequential run, and one copy of the
model is 1.45 GB of RSS where a copy per thread would not fit the container).
`decode_streams` releases the GIL, so the threads decode in parallel. Each
thread OWNS the streams pinned to it: every sherpa-onnx call on a stream
happens on its worker's thread, because a stream's feature buffer is not safe
to append to while another thread decodes it, and a lock would stall the event
loop behind a 30-100 ms decode step. The event loop only converts PCM and
queues it. Events come back through `loop.call_soon_threadsafe` into one queue
per connection, and one task per connection is the only writer to its socket.

WHAT IS NEVER LOGGED: audio, transcript text, the token, request headers.
One structured line per stream close carries counts and durations only.
"""
from __future__ import annotations

import asyncio
import bisect
import collections
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse

log = logging.getLogger("stt-stream")

# --------------------------------------------------------------------------
# Fixed facts of the protocol and the models.
# --------------------------------------------------------------------------

#: The rate every profile is exported for and the browser worklet resamples to.
SAMPLE_RATE = 16000
#: One frame per message, normally 640 samples (1280 bytes). The ceiling is the
#: gateway's VOICE_LIVE_MAX_FRAME_BYTES; above it the frame is refused, below
#: uvicorn's own message cap so the refusal is ours and says why.
MAX_FRAME_BYTES = 16384
#: uvicorn's per-message ceiling (--ws-max-size). Text messages are tiny; a
#: frame between MAX_FRAME_BYTES and this is refused by the handler with 4413.
WS_MAX_MESSAGE_BYTES = 65536
#: Offered by the gateway; echoed so a client that insists on it is satisfied.
SUBPROTOCOL = "techsara.voice.v1"
#: The files every profile directory must hold: the sherpa-onnx int8 export.
MODEL_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")

#: Endpoint rules other than rule 2 (STT_ENDPOINT_S). Rule 1 closes an
#: utterance after 2.4 s of silence even with no text. Rule 3 closes one after
#: N seconds whatever is being said, which is exactly the mid-speech cut this
#: engine exists to avoid, so it is set out of reach. OUT OF REACH MEANS
#: NEVER, not "an hour": sherpa-onnx measures rule 3 from the stream's
#: creation (GetNumProcessedFrames; only Reset clears it) and this engine
#: never resets a stream, so a stream that reached it would hold is_endpoint
#: true for good -- no endpoint finals, no speech edges, no renewal (a forced
#: final always leaves words pending) -- for the rest of a dictation the owner
#: wants to run for hours. 1e7 s is 115 days of audio on ONE recognizer stream.
RULE1_TRAILING_S = 2.4
RULE3_UTTERANCE_S = 1e7

#: The message ceiling, the gateway's (orchestrator/app/voice_live.py): every
#: message after the start counts, audio or text, and a stream may have sent
#: at most MESSAGES_FACTOR times the frames its elapsed time holds, plus the
#: resume allowance. The frame length is the start's frame_ms clamped to
#: [MIN_FRAME_MS, FRAME_MS]: 50 messages a second at 40 ms after a 65 s head
#: start with the defaults.
FRAME_MS = 40
MIN_FRAME_MS = 10
MESSAGES_FACTOR = 2
#: The arrival ceilings' head start on top of STT_RESUME_MAX_S.
ARRIVAL_SLACK_S = 5.0

#: What a stream is for. "meeting" waits longer before it closes an utterance
#: (people pause mid-thought when they talk to each other) and forces a long
#: one out sooner.
MODES = ("dictation", "meeting")

#: Words held back from a forced final (spec section 7): the newest tokens may
#: still be a word in progress ("sau" before "ce"), and committing half a word
#: would split it across two finals.
KEEP_WORDS = 3
#: Pending text is forced out past this many characters too, whatever its age:
#: the gateway drops any event over 4,000 characters, and scripts written
#: without spaces (zh, ja) never have more than KEEP_WORDS "words", so there
#: the last KEEP_CHARS characters are what stays pending.
MAX_PENDING_CHARS = 1000
KEEP_CHARS = 3


class ConfigError(ValueError):
    """The environment describes an engine that must not start."""


# --------------------------------------------------------------------------
# Configuration. Everything is read and validated ONCE, before anything loads.
# --------------------------------------------------------------------------

_PROFILE_KEYS = {"id", "dir", "chunk_ms", "max_streams", "languages", "flush_pad_ms", "model", "cost"}
_PROFILE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
#: "auto" or a code the model's tokenizer knows: en, hi, en-US, hi-IN, ...
_LANGUAGE = re.compile(r"^(auto|[a-z]{2,3}(-[a-z]{2})?)$", re.IGNORECASE)
_MODEL_LABEL = re.compile(r"^[A-Za-z0-9._-]{1,96}$")


@dataclass(frozen=True)
class Profile:
    """One model at one chunk size: one shared recognizer, its own capacity."""

    id: str
    dir: str
    chunk_ms: int
    max_streams: int
    languages: Tuple[str, ...]
    #: Silence appended at flush so the model's right context covers the last
    #: word. Measured 2026-09-29 on 30 LibriSpeech utterances cut right after
    #: the last word (160 ms export): the last word survived 1/30 times with
    #: no padding, 14/30 with 250 ms, 26/30 with 500 ms and 27/30 with 1 s.
    #: The default scales with the chunk, because the model will not decode
    #: its last chunk until a whole right context of audio has arrived.
    flush_pad_ms: int
    #: What /health calls the model. A label, never a path.
    model: str
    #: Units of the engine's decode budget (STT_MAX_COST) one stream of this
    #: profile holds; 0 means "derive it from chunk_ms" (default_cost).
    cost: int = 0

    def __post_init__(self) -> None:
        if self.cost <= 0:
            object.__setattr__(self, "cost", default_cost(self.chunk_ms))

    def supports(self, language: str) -> Optional[str]:
        """The allow-list's own spelling of `language`, or None."""
        wanted = language.lower()
        for known in self.languages:
            if known.lower() == wanted:
                return known
        return None


def default_flush_pad_ms(chunk_ms: int) -> int:
    return max(800, 2 * chunk_ms + 400)


def default_cost(chunk_ms: int) -> int:
    """Budget units one stream costs, by chunk size: 2 at 160 ms, 1 at 560 ms.

    MEASURED on the worker (build spec section 7), for those two sizes only:
    twelve 160 ms streams kept 4.93 cores busy (0.41 core each), and one
    4-thread decode loop holds ~7 real-time streams at 160 ms but ~14 at
    560 ms, so a 560 ms stream costs half as much. A smaller chunk pays the
    per-step overhead more often, hence the inverse scaling; any other size is
    an estimate, and a profile using one should set "cost" after measuring.
    """
    return max(1, round(320 / chunk_ms))


def _int_field(item: Mapping[str, Any], key: str, where: str, lo: int, hi: int) -> int:
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ConfigError(f"{where}: {key!r} must be an integer from {lo} to {hi}")
    return value


def parse_profiles(raw: str, *, check_files: bool = True) -> Tuple[Profile, ...]:
    """STT_PROFILES, strictly: an engine started on a typo serves nobody.

    A JSON list, in ADMISSION ORDER: a stream goes to the first profile that
    allows its language and has room, so the low-latency profile comes first
    and a wider-chunk one takes the overflow.
    """
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(f"STT_PROFILES is not valid JSON: {exc}") from None
    if not isinstance(data, list) or not 1 <= len(data) <= 8:
        raise ConfigError("STT_PROFILES must be a JSON list of 1 to 8 profiles")
    profiles: List[Profile] = []
    for index, item in enumerate(data):
        where = f"STT_PROFILES[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} must be an object")
        unknown = sorted(set(item) - _PROFILE_KEYS)
        if unknown:
            raise ConfigError(f"{where}: unknown keys {unknown}")
        pid = item.get("id")
        if not isinstance(pid, str) or not _PROFILE_ID.match(pid):
            raise ConfigError(f"{where}: 'id' must match {_PROFILE_ID.pattern}")
        if any(p.id == pid for p in profiles):
            raise ConfigError(f"{where}: duplicate id {pid!r}")
        where = f"profile {pid!r}"
        directory = item.get("dir")
        if not isinstance(directory, str) or not os.path.isabs(directory):
            raise ConfigError(f"{where}: 'dir' must be an absolute path")
        if check_files:
            missing = [n for n in MODEL_FILES if not os.path.isfile(os.path.join(directory, n))]
            if missing:
                raise ConfigError(f"{where}: the model directory is missing {', '.join(missing)}")
        chunk_ms = _int_field(item, "chunk_ms", where, 40, 2000)
        max_streams = _int_field(item, "max_streams", where, 1, 256)
        languages = item.get("languages")
        if (not isinstance(languages, list) or not languages
                or not all(isinstance(x, str) and _LANGUAGE.match(x) for x in languages)):
            raise ConfigError(f"{where}: 'languages' must be a non-empty list of 'auto' or codes like en, hi, en-US")
        if len({x.lower() for x in languages}) != len(languages):
            raise ConfigError(f"{where}: 'languages' lists a code twice")
        if "flush_pad_ms" in item:
            flush_pad_ms = _int_field(item, "flush_pad_ms", where, 0, 5000)
        else:
            flush_pad_ms = default_flush_pad_ms(chunk_ms)
        model = item.get("model", os.path.basename(os.path.normpath(directory)))
        if not isinstance(model, str) or not _MODEL_LABEL.match(model):
            raise ConfigError(f"{where}: 'model' must match {_MODEL_LABEL.pattern}")
        cost = _int_field(item, "cost", where, 1, 64) if "cost" in item else default_cost(chunk_ms)
        profiles.append(Profile(pid, directory, chunk_ms, max_streams, tuple(languages), flush_pad_ms, model, cost))
    return tuple(profiles)


@dataclass(frozen=True)
class Settings:
    profiles: Tuple[Profile, ...]
    #: "" only when STT_ALLOW_NO_TOKEN=1, which exists for tests and a
    #: loopback smoke run; the deployed engine always has one.
    token: str
    bind: str = "127.0.0.1"
    port: int = 30009
    #: Decode threads per profile, all sharing the profile's one recognizer,
    #: and the ONNX Runtime threads that recognizer runs each decode on.
    workers: int = 4
    threads: int = 2
    endpoint_s: float = 0.6
    max_utterance_s: float = 30.0
    #: start.mode "meeting": the trailing silence that closes an utterance and
    #: the length that forces one out (build spec section 11). The meeting
    #: endpoint is enforced by the engine ON TOP of the recognizer's rule 2
    #: (endpoint_s), which is per recognizer, not per stream, so it can only
    #: be longer.
    meeting_endpoint_s: float = 0.9
    meeting_max_utterance_s: float = 25.0
    #: THE ENGINE'S DECODE BUDGET, in the units Profile.cost counts: a stream
    #: is admitted only while the units of every open stream plus its own fit.
    #: Per-profile max_streams alone admitted 40 streams (8+8+12+12), about
    #: 11.5 cores of decode against the container's 8. Sized from the
    #: worker's measurements (build spec sections 7 and 11): the 8-core
    #: cpuset decodes ~16 streams at 160 ms in real time (0.41 core each,
    #: 6.6 cores), which is 32 units at 2 units a stream; the same 32 units
    #: hold 32 streams at 560 ms (~0.2 core each, 6.5 cores). Both leave
    #: ~1.4 cores for the event loop, feature extraction and the p95 of a
    #: decode step. A flat stream count could not say that: 16 streams would
    #: refuse the 17th with three cores idle when the overflow sits on the
    #: cheap 560 ms profiles.
    max_cost: int = 32
    idle_s: float = 60.0
    start_timeout_s: float = 10.0
    #: Mirrors the gateway's VOICE_LIVE_RESUME_MAX_S: a reconnect replays up
    #: to this much buffered audio faster than real time, and no more.
    resume_max_s: float = 60.0
    #: Silence put in FRONT of every new recognizer stream (build spec 8C).
    #: Nemotron 3.5 drops the opening words of a stream that starts on speech:
    #: measured on 30 LibriSpeech utterances trimmed to their first word, the
    #: first word survived 4/30 times with no padding, 11/30 with 80 ms and
    #: 27/30 with 160 ms. Positions sent to the gateway never include it.
    lead_pad_ms: int = 160
    #: RENEWAL AT EVERY SAFE POINT (build spec section 11): right after an
    #: endpoint final, once the silence since the stream's last token reaches
    #: this, the stream is swapped for a fresh one. A long-lived stream drops
    #: whole clauses; a fresh one sometimes drops the first word after it. So
    #: the threshold decides, measured on the worker 2026-09-30 (real models,
    #: 4 sessions x 5 utterances, 2.5 s between utterances, WER):
    #:
    #:     renewal              FLEURS Hindi   LibriSpeech (English model)
    #:     off                     15.78          3.97
    #:     every endpoint 0.6 s    13.78          6.20   (47 renewals)
    #:     1.2 s                   12.22          4.96
    #:     2.0 s                   12.44          4.47   (20: one per pause)
    #:
    #: At 0.6 s every short pause inside a sentence renews, and errors on an
    #: utterance's first word rose from 3 to 8 in English (dropped "who",
    #: "old", "thus"); from 1.2 s up the renewals fall between utterances.
    #: 2.0 s keeps nearly all of the Hindi gain at almost no English cost,
    #: and loses fewer words than never renewing (deletions: Hindi 14 -> 4,
    #: English 3 -> 1). With the level check in DecodeWorker._still_silent it
    #: measured 12.89% / 4.47% there, and 12.44% / 4.22% against 13.33% /
    #: 4.22% unrenewed with 1.2 s between utterances. (Build spec section 11
    #: asked for 0.6; these numbers are why not.) A fresh stream's first
    #: Hindi partial comes ~200 ms later (p50 571 -> 801 ms; English
    #: unchanged). 0 switches it off (the backstops below remain).
    renew_silence_s: float = 2.0
    #: The backstops (see Segmenter._should_renew). Not environment knobs:
    #: they are part of the no-word-loss argument, not tuning.
    renew_chars: int = 2000
    renew_hold_s: float = 1.5
    renew_after_s: float = 1800.0
    #: The newest client audio each stream keeps for a renewal to re-decode.
    #: A renewal re-decodes from the last final's end (inside the silence
    #: that closed it); this bounds how far back that can be when a renewal
    #: had to wait for the decoder to catch up. It covers the undecoded tail
    #: (under 0.27 s at 160 ms and 0.67 s at 560 ms, measured) plus more than
    #: a chunk for an onset whose first token the model has not emitted yet.
    renew_seed_s: float = 2.0
    #: How long a flush may take before the stream is failed instead.
    flush_timeout_s: float = 10.0

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, check_files: bool = True) -> "Settings":
        raw = env.get("STT_PROFILES", "").strip()
        if not raw:
            raise ConfigError("STT_PROFILES must be set")
        profiles = parse_profiles(raw, check_files=check_files)
        token = env.get("STT_TOKEN", "").strip()
        allow_no_token = env.get("STT_ALLOW_NO_TOKEN", "").strip() == "1"
        if not token and not allow_no_token:
            raise ConfigError("STT_TOKEN is empty; the engine does not serve without one (STT_ALLOW_NO_TOKEN=1 is for tests)")
        if token and len(token) < 16:
            raise ConfigError("STT_TOKEN is shorter than 16 characters")
        bind = env.get("STT_BIND", "127.0.0.1").strip()
        try:
            address = ipaddress.ip_address(bind)
        except ValueError:
            raise ConfigError("STT_BIND must be an IP address") from None
        if address.is_unspecified:
            # Host networking: a wildcard would put an engine that decodes on
            # demand onto the office LAN, the tailnet and the RoCE rails.
            raise ConfigError("STT_BIND must be one address, never a wildcard")
        endpoint_s = _env_float(env, "STT_ENDPOINT_S", 0.6, 0.2, 5.0)
        meeting_endpoint_s = _env_float(env, "STT_MEETING_ENDPOINT_S", 0.9, 0.2, 5.0)
        if meeting_endpoint_s < endpoint_s:
            # The recognizer's own rule 2 (endpoint_s) fires first for every
            # stream; the engine can only wait longer than that, never less.
            raise ConfigError("STT_MEETING_ENDPOINT_S must be at least STT_ENDPOINT_S")
        max_cost = _env_int(env, "STT_MAX_COST", 32, 1, 4096)
        for profile in profiles:
            if profile.cost > max_cost:
                raise ConfigError(f"profile {profile.id!r} costs {profile.cost} units, more than all of STT_MAX_COST ({max_cost})")
        return cls(
            profiles=profiles,
            token=token,
            bind=bind,
            port=_env_int(env, "STT_PORT", 30009, 1024, 65535),
            workers=_env_int(env, "STT_WORKERS", 4, 1, 16),
            threads=_env_int(env, "STT_THREADS", 2, 1, 8),
            endpoint_s=endpoint_s,
            max_utterance_s=_env_float(env, "STT_MAX_UTTERANCE_S", 30.0, 5.0, 300.0),
            meeting_endpoint_s=meeting_endpoint_s,
            meeting_max_utterance_s=_env_float(env, "STT_MEETING_MAX_UTTERANCE_S", 25.0, 5.0, 300.0),
            max_cost=max_cost,
            idle_s=_env_float(env, "STT_IDLE_S", 60.0, 5.0, 3600.0),
            start_timeout_s=_env_float(env, "STT_START_TIMEOUT_S", 10.0, 1.0, 120.0),
            resume_max_s=_env_float(env, "STT_RESUME_MAX_S", 60.0, 0.0, 600.0),
            lead_pad_ms=_env_int(env, "STT_LEAD_PAD_MS", 160, 0, 1000),
            renew_silence_s=_env_float(env, "STT_RENEW_SILENCE_S", 2.0, 0.0, 30.0),
        )


def _env_int(env: Mapping[str, str], key: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an integer") from None
    if not lo <= value <= hi:
        raise ConfigError(f"{key} must be from {lo} to {hi}")
    return value


def _env_float(env: Mapping[str, str], key: str, default: float, lo: float, hi: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a number") from None
    if not math.isfinite(value) or not lo <= value <= hi:
        raise ConfigError(f"{key} must be from {lo} to {hi}")
    return value


# --------------------------------------------------------------------------
# Metrics: stdlib text exposition, closed label sets.
# --------------------------------------------------------------------------

#: How a stream that was admitted ended.
OUTCOMES = ("completed", "disconnected", "idle", "protocol", "rate_limited", "error", "shutdown")
#: Why a connection was refused before a stream was admitted. Closed and
#: complete: anything else counts as "other", which exists from the start
#: like the rest (build spec section 11).
REJECT_REASONS = ("unauthorized", "not_ready", "protocol", "language", "capacity", "other")
#: What was sent to the gateway. The three finals are counted apart because
#: the ratio of forced to endpoint finals is the segmentation's health signal.
EVENT_KINDS = ("partial", "final", "final_forced", "final_flush", "speech", "done", "error")

_STEP_BUCKETS = (0.005, 0.01, 0.02, 0.03, 0.04, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.5, 1.0, 2.0)
_BATCH_BUCKETS = (1, 2, 3, 4, 6, 8, 12, 16, 24, 32)

_HELP = {
    "stt_stream_ready": ("gauge", "1 once every profile's recognizer is loaded and warm."),
    "stt_stream_active_streams": ("gauge", "Streams admitted and open now."),
    "stt_stream_capacity_streams": ("gauge", "Streams a profile admits before refusing with capacity."),
    "stt_stream_cost_budget_units": ("gauge", "The engine's decode budget (STT_MAX_COST); admission refuses beyond it."),
    "stt_stream_cost_in_use_units": ("gauge", "Budget units the open streams hold."),
    "stt_stream_streams_total": ("counter", "Admitted streams by how they ended."),
    "stt_stream_audio_seconds_total": ("counter", "Seconds of client audio received."),
    "stt_stream_compute_seconds_total": ("counter", "Decode wall seconds charged to streams: every stream of a "
                                                    "decode_streams call is charged the call's wall time, so RTF = "
                                                    "compute / audio reaches 1 when decoding stops keeping up."),
    "stt_stream_rejections_total": ("counter", "Connections refused before a stream was admitted."),
    "stt_stream_events_total": ("counter", "Events sent to the gateway, by kind."),
    "stt_stream_renewals_total": ("counter", "Stream renewals, each at a safe point: right after an endpoint final, "
                                             "inside its silence."),
    "stt_stream_decode_step_seconds": ("histogram", "Wall time of one decode_streams call; the budget is one chunk."),
    "stt_stream_batch_size": ("histogram", "Streams decoded together in one decode_streams call."),
}


class Metrics:
    """Thread-safe counters and histograms, rendered at scrape time.

    Every series of a closed label set exists from the start at zero, so a
    rate() works from the first scrape and a dashboard never shows "no data"
    for an outcome that simply has not happened yet. A label value outside its
    set folds to "other": a caller's typo must not mint a series.
    """

    def __init__(self, profile_ids: Sequence[str]):
        self._lock = threading.Lock()
        self._profiles = tuple(profile_ids)
        self._counters: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = {}
        self._hists: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], List[float]] = {}
        for pid in self._profiles:
            for outcome in OUTCOMES:
                self._counters[("stt_stream_streams_total", (("profile", pid), ("outcome", outcome)))] = 0.0
            for name in ("stt_stream_audio_seconds_total", "stt_stream_compute_seconds_total",
                         "stt_stream_renewals_total"):
                self._counters[(name, (("profile", pid),))] = 0.0
            for name, buckets in (("stt_stream_decode_step_seconds", _STEP_BUCKETS),
                                  ("stt_stream_batch_size", _BATCH_BUCKETS)):
                self._hists[(name, (("profile", pid),))] = [0.0] * (len(buckets) + 2)
        for reason in REJECT_REASONS:
            self._counters[("stt_stream_rejections_total", (("reason", reason),))] = 0.0
        for kind in EVENT_KINDS:
            self._counters[("stt_stream_events_total", (("kind", kind),))] = 0.0

    def _profile(self, pid: str) -> str:
        return pid if pid in self._profiles else "other"

    def _inc(self, name: str, labels: Tuple[Tuple[str, str], ...], value: float) -> None:
        with self._lock:
            key = (name, labels)
            self._counters[key] = self._counters.get(key, 0.0) + value

    def stream_closed(self, profile: str, outcome: str) -> None:
        outcome = outcome if outcome in OUTCOMES else "other"
        self._inc("stt_stream_streams_total", (("profile", self._profile(profile)), ("outcome", outcome)), 1.0)

    def audio(self, profile: str, seconds: float) -> None:
        self._inc("stt_stream_audio_seconds_total", (("profile", self._profile(profile)),), seconds)

    def renewal(self, profile: str) -> None:
        self._inc("stt_stream_renewals_total", (("profile", self._profile(profile)),), 1.0)

    def reject(self, reason: str) -> None:
        reason = reason if reason in REJECT_REASONS else "other"
        self._inc("stt_stream_rejections_total", (("reason", reason),), 1.0)

    def event(self, kind: str) -> None:
        kind = kind if kind in EVENT_KINDS else "other"
        self._inc("stt_stream_events_total", (("kind", kind),), 1.0)

    def decode_step(self, profile: str, seconds: float, batch: int) -> None:
        """One decode_streams call that took `seconds` of wall time for
        `batch` streams. Every one of those streams waited the whole call, so
        each is charged all of it: RTF = compute / audio is then the fraction
        of real time a stream's decoding takes, and it reaches 1.0 when
        decoding stops keeping up (a per-stream SHARE of the call reported
        1/batch of that, so an alert at 0.8 could never fire while a batch of
        four fell behind). The step histogram keeps the call's own wall time."""
        labels = (("profile", self._profile(profile)),)
        with self._lock:
            key = ("stt_stream_compute_seconds_total", labels)
            self._counters[key] = self._counters.get(key, 0.0) + seconds * batch
            for name, buckets, value in (("stt_stream_decode_step_seconds", _STEP_BUCKETS, seconds),
                                         ("stt_stream_batch_size", _BATCH_BUCKETS, float(batch))):
                h = self._hists.setdefault((name, labels), [0.0] * (len(buckets) + 2))
                h[bisect.bisect_left(buckets, value)] += 1  # the +Inf slot is index len(buckets)
                h[-1] += value

    def render(self, gauges: Sequence[Tuple[str, Tuple[Tuple[str, str], ...], float]]) -> str:
        with self._lock:
            counters = dict(self._counters)
            hists = {k: list(v) for k, v in self._hists.items()}
        lines: List[str] = []
        rows: Dict[str, List[str]] = collections.defaultdict(list)
        for name, labels, value in gauges:
            rows[name].append(f"{name}{_labels(labels)} {_num(value)}")
        for (name, labels), value in sorted(counters.items()):
            rows[name].append(f"{name}{_labels(labels)} {_num(value)}")
        for (name, labels), counts in sorted(hists.items()):
            buckets = _STEP_BUCKETS if name == "stt_stream_decode_step_seconds" else _BATCH_BUCKETS
            running = 0.0
            for edge, count in zip(buckets, counts):
                running += count
                rows[name].append(f"{name}_bucket{_labels(labels + (('le', _num(edge)),))} {_num(running)}")
            running += counts[len(buckets)]
            rows[name].append(f"{name}_bucket{_labels(labels + (('le', '+Inf'),))} {_num(running)}")
            rows[name].append(f"{name}_sum{_labels(labels)} {_num(counts[-1])}")
            rows[name].append(f"{name}_count{_labels(labels)} {_num(running)}")
        for name in sorted(rows):
            kind, text = _HELP.get(name, ("untyped", ""))
            lines.append(f"# HELP {name} {text}")
            lines.append(f"# TYPE {name} {kind}")
            lines.extend(rows[name])
        return "\n".join(lines) + "\n"


def _labels(labels: Tuple[Tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}"


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


# --------------------------------------------------------------------------
# Reading the recognizer.
# --------------------------------------------------------------------------


def clean_text(text: str) -> str:
    """Collapse whitespace runs. The multilingual model emits ' ' tokens of
    its own, so its text carries double spaces nobody wants to see."""
    return " ".join(text.split())


def has_words(text: str) -> bool:
    """Whether `text` is an utterance or only punctuation.

    An utterance with no words produces no final (the protocol says so), and a
    lone "." the model emits after an endpoint is not a sentence of its own:
    it stays pending and leads the next utterance (see utterance_text).
    """
    return any(ch.isalnum() for ch in text)


#: Sentence punctuation that CLOSES something, Latin, Devanagari and CJK. An
#: utterance whose text starts with one of these continues the previous
#: utterance's sentence: consumers join it WITHOUT a space (see the module
#: docstring, "UTTERANCE TEXT, FOR CONSUMERS").
CLOSING_PUNCTUATION = ".,;:!?\u2026\u0964\u0965\u3002\uff0c\u3001\uff01\uff1f\uff1b\uff1a"


def utterance_text(raw: str) -> str:
    """What an utterance shows: its text with whitespace runs collapsed.

    NOTHING IS DROPPED, including punctuation in front of the first word.
    The model decides how a sentence ENDS only once it hears how the next one
    begins, so the comma or full stop of an utterance that was already
    committed often arrives as the first token of the next one: measured on
    the worker, 5 of 40 LibriSpeech finals and 11 of 40 FLEURS Hindi finals
    began ", and ..." or "। ...". A final is never retracted, so the mark
    stays where it arrived, and the consumer puts it back against its
    sentence by joining such an utterance without a space ("told" + ", and"
    -> "told, and"). Deleting it instead cost the live Hindi transcript,
    which is the text inserted for Hindi sessions, the danda of about one
    sentence in four.
    """
    return clean_text(raw)


class TokenTimes:
    """Where each token of the recognizer's text was heard, by character.

    sherpa-onnx gives the tokens and their emission times in seconds since the
    stream's last reset (this engine never resets a stream, so `start_time`
    stays 0; it is added anyway). Its text is the tokens joined, minus the
    first word's leading space: measured on both models, "".join(tokens)
    stripped equals the text, token for token. Offsets here are in the
    stripped text's coordinates, the ones the segmenter slices with, and the
    positions are CLIENT samples: `to_client` takes away the silence the
    engine put in front of the audio.
    """

    __slots__ = ("starts", "ends", "positions")

    def __init__(self, starts: List[int], ends: List[int], positions: List[int]):
        self.starts, self.ends, self.positions = starts, ends, positions

    @classmethod
    def build(cls, text: str, tokens: Sequence[str], stamps: Sequence[float], start_time: float,
              to_client: Callable[[float], int]) -> Optional["TokenTimes"]:
        if len(tokens) != len(stamps):
            return None
        joined = "".join(tokens)
        if joined.strip() != text:
            # A byte-fallback or normalised text no longer lines up with its
            # tokens; the segmenter falls back to arrival positions.
            return None
        position = -(len(joined) - len(joined.lstrip()))
        starts: List[int] = []
        ends: List[int] = []
        positions: List[int] = []
        for token, stamp in zip(tokens, stamps):
            if token.strip():
                starts.append(position)
                ends.append(position + len(token))
                positions.append(to_client(start_time + float(stamp)))
            position += len(token)
        return cls(starts, ends, positions)

    def span(self, c0: int, c1: int) -> Optional[Tuple[int, int]]:
        """Client positions of the first and last token overlapping [c0, c1)."""
        first = bisect.bisect_right(self.ends, c0)
        last = bisect.bisect_left(self.starts, c1) - 1
        if first >= len(self.starts) or last < first:
            return None
        return self.positions[first], self.positions[last]


def read_result(recognizer: Any, stream: Any) -> Tuple[str, Any]:
    """The stream's text (stripped, as `get_result` returns it) and the full
    result object when the binding has one, for its token times."""
    get_all = getattr(recognizer, "get_result_all", None)
    if get_all is None:
        return recognizer.get_result(stream), None
    result = get_all(stream)
    return (result.text or "").strip(), result


# --------------------------------------------------------------------------
# Segmentation.
# --------------------------------------------------------------------------

_WORD = re.compile(r"\S+")


class Segmenter:
    """One stream's running hypothesis -> partial and final events.

    THE RULE THAT AVOIDS WORD LOSS (build spec section 7 and 8D). The obvious
    design resets the recognizer at every endpoint and starts each utterance
    from a clean state. Measured on LibriSpeech (30 utterances joined by
    0.8 s pauses, 160 ms export), that more than doubles the word error rate,
    5.86% -> 13.71% (12.94% with a lead pad after each reset): a 0.6 s pause is
    a short one, the next word's onset is often already inside the encoder's
    context, and a reset throws that context away. So the recognizer is NEVER
    restarted while speech may be pending. It keeps decoding one growing
    hypothesis and this class keeps `committed`, the number of its characters
    already emitted as finals:

    * after each decode step, pending = text[committed:]; if it changed, it is
      the partial for utterance u (at most one per step, i.e. per chunk);
    * when the endpoint flips false -> true and pending has words, pending is
      final(u), committed moves to the end, u += 1;
    * pending that has run past max_utterance_s of audio (or MAX_PENDING_CHARS)
      is forced out as a final, all but its last KEEP_WORDS words;
    * the stream is RENEWED -- swapped for a fresh one -- at SAFE POINTS only:
      the endpoint holds, nothing worded is pending, and a final has been
      committed from this stream, i.e. right after an endpoint final, in the
      silence that produced it, once that silence has lasted renew_silence_s
      (build spec section 11: a long-lived stream drops whole clauses;
      renewing after 2 s pauses lowered FLEURS Hindi WER from 15.78% to
      12.4-12.9% and lost fewer words than never renewing --
      Settings.renew_silence_s has the numbers).
      What differs from the reset that lost words is WHERE the fresh stream
      starts: not at the endpoint but at the final's end, inside the
      silence, with the audio from there on re-decoded (DecodeWorker._renew),
      which is exactly what a reconnect does. Two backstops renew in a held
      silence (renew_hold_s) when the text is past renew_chars or
      renew_after_s of audio has gone by, so strings (and the O(n) read of
      every token on every step) stay small even with renewals switched off.

    The recognizer's text only ever grows at the end (greedy transducer
    decoding never retracts a token), so a character offset stays valid until
    the next renewal.

    THE ENDPOINT. The recognizer's rule 2 is one value for every stream it
    serves (the engine's STT_ENDPOINT_S). A stream whose own endpoint_s is
    longer -- start.mode "meeting" -- takes the recognizer's endpoint only once
    the silence since its last token has also reached endpoint_s, measured
    from the token times the way sherpa-onnx measures rule 2 (from the step
    that grew the text, without them).

    POSITIONS are in the client's time base. `now` is how far into the
    client's audio the DECODER has got (not how much audio has arrived: a
    reconnect's replay arrives all at once and is decoded over the next
    seconds, and every rule here is about what has been heard), and token
    positions count client samples the same way; the worker has already taken
    away any silence the engine inserted. Events carry first_sample plus that.
    With token times the positions come from the tokens themselves; without
    them, the spec's fallback: an utterance starts one chunk before its first
    partial appeared. An ENDPOINT final ends inside the silence that closed
    it, half the endpoint's silence before the step that closed it (and never
    before its last token): the browser resumes a dropped stream from the
    largest end_sample it saw, and a token time is when the model emitted the
    word, a median 100 ms (p90 286 ms) before the word ended -- resuming from
    there re-decoded the word's tail as a new first word in 6 of 60 real
    reconnects. A forced final keeps its last token's time: speech goes on,
    and there is no silence to aim into. An utterance never starts before the
    previous final ended.

    Pure: no I/O, no clock, no recognizer. The decode worker hands it one
    observation per decode step and does what it says.
    """

    def __init__(self, *, first_sample: int, first_u: int, chunk_samples: int, endpoint_s: float,
                 max_utterance_s: float, renew_silence_s: float, renew_chars: int, renew_hold_s: float,
                 renew_after_s: float, recognizer_endpoint_s: Optional[float] = None,
                 sample_rate: int = SAMPLE_RATE):
        self.first_sample = first_sample
        self.u = first_u
        self.chunk = chunk_samples
        self.sample_rate = sample_rate
        self.endpoint_ms = int(round(endpoint_s * 1000))
        self.endpoint_samples = int(round(endpoint_s * sample_rate))
        # Whether this stream waits longer than the recognizer's own rule 2.
        base = endpoint_s if recognizer_endpoint_s is None else recognizer_endpoint_s
        self._longer_endpoint = endpoint_s > base + 1e-9
        self.max_utterance = int(round(max_utterance_s * sample_rate))
        #: Samples of silence past the endpoint before a safe-point renewal,
        #: or None when those are switched off (renew_silence_s 0).
        self.renew_wait: Optional[int] = (
            None if renew_silence_s <= 0 else max(0, int(round((renew_silence_s - endpoint_s) * sample_rate))))
        self.renew_chars = renew_chars
        self.renew_hold = int(round(renew_hold_s * sample_rate))
        self.renew_after = int(round(renew_after_s * sample_rate))
        self.committed = 0
        self.finals = 0
        self.renewals = 0
        self._last_partial: Optional[str] = None
        self._was_endpoint = False
        self._seen: Optional[int] = None      # client position when the utterance first showed words
        self._speaking = False
        self._quiet_since: Optional[int] = None
        self._renewed_at = 0
        self._floor = 0                       # where the previous final ended
        self._text: Optional[str] = None      # the text at the last observation...
        self._grew_at = 0                     # ...and the position of the step that last changed it

    @property
    def floor(self) -> int:
        """Client position where the last final ended (0 before the first):
        a renewal re-decodes from here, and nothing before it is pending."""
        return self._floor

    # -- the three entry points --------------------------------------------

    def observe(self, text: str, endpoint: bool, now: int,
                timing: Optional[TokenTimes] = None) -> Tuple[List[dict], bool]:
        """One decode step's view -> (events, whether to renew the stream now)."""
        events: List[dict] = []
        if self.committed > len(text):
            # The text never shrinks between renewals; if a binding ever makes
            # it, re-emitting nothing is better than slicing garbage.
            self.committed = len(text)
        if text != self._text:
            self._text = text
            self._grew_at = now
        if endpoint and self._longer_endpoint and text and now - self._last_token_at(timing) < self.endpoint_samples:
            endpoint = False
        raw = text[self.committed:]
        rising = endpoint and not self._was_endpoint
        self._was_endpoint = endpoint
        if has_words(raw):
            if self._seen is None:
                self._seen = now
            if rising:
                events.extend(self._commit(text, len(text), now, timing, "final"))
            else:
                cut = self._forced_cut(raw, now)
                if cut:
                    events.extend(self._commit(text, self.committed + cut, now, timing, "final_forced"))
            rest = utterance_text(text[self.committed:])
            if has_words(rest) and rest != self._last_partial:
                events.extend(self._partial(rest, text, now, timing))
        return events, self._should_renew(text, endpoint, now)

    def after_renew(self, now: int) -> None:
        """The worker renewed the stream at client position `now`."""
        self.renewals += 1
        self.committed = 0
        self._last_partial = None
        self._was_endpoint = False
        self._seen = None
        self._quiet_since = None
        self._renewed_at = now
        self._text = None
        self._grew_at = now

    def finish(self, text: str, now: int, timing: Optional[TokenTimes] = None) -> List[dict]:
        """Flush: whatever is still pending becomes the last final."""
        if self.committed > len(text):
            self.committed = len(text)
        if not has_words(text[self.committed:]):
            return []
        return self._commit(text, len(text), now, timing, "final_flush")

    # -- internals -----------------------------------------------------------

    def _last_token_at(self, timing: Optional[TokenTimes]) -> int:
        """Client position of the stream's newest token: its time when the
        binding gives token times, else the step that last changed the text."""
        if timing is not None and timing.positions:
            return timing.positions[-1]
        return self._grew_at

    def _bounds(self, c0: int, c1: int, now: int, timing: Optional[TokenTimes], kind: str) -> Tuple[int, int]:
        """Client positions (not yet offset by first_sample) of text[c0:c1]."""
        span = timing.span(c0, c1) if timing is not None else None
        if span is not None:
            start, end = span
        else:
            seen = self._seen if self._seen is not None else now
            start, end = seen - self.chunk, now
        if kind == "final":
            # An endpoint final: into the silence that closed it (see the
            # class docstring, POSITIONS), never before its last token.
            half = now - self.endpoint_samples // 2
            end = max(end, half) if span is not None else half
        start = min(max(start, self._floor, 0), now)
        end = min(max(end, start), now)
        return start, end

    def _partial(self, pending: str, text: str, now: int, timing: Optional[TokenTimes]) -> List[dict]:
        start, end = self._bounds(self.committed, len(text), now, timing, "partial")
        start, end = self.first_sample + start, self.first_sample + end
        events: List[dict] = []
        if not self._speaking:
            self._speaking = True
            events.append({"type": "speech", "active": True, "sample": start, "_kind": "speech"})
        self._last_partial = pending
        events.append({"type": "partial", "u": self.u, "text": pending,
                       "start_sample": start, "end_sample": end, "_kind": "partial"})
        return events

    def _commit(self, text: str, cut: int, now: int, timing: Optional[TokenTimes], kind: str) -> List[dict]:
        start, end = self._bounds(self.committed, cut, now, timing, kind)
        self._floor = end
        start, end = self.first_sample + start, self.first_sample + end
        final = {"type": "final", "u": self.u, "text": utterance_text(text[self.committed:cut]),
                 "start_sample": start, "end_sample": end,
                 "endpoint_ms": self.endpoint_ms if kind == "final" else 0, "_kind": kind}
        events = [final]
        self.committed = cut
        self.u += 1
        self.finals += 1
        self._last_partial = None
        if kind == "final_forced":
            # Speech goes on: the held-back words are the next utterance, and
            # they were first seen now as far as the fallback bounds know.
            self._seen = now
        else:
            self._seen = None
            if self._speaking:
                self._speaking = False
                events.append({"type": "speech", "active": False, "sample": end, "_kind": "speech"})
        return events

    def _forced_cut(self, raw: str, now: int) -> int:
        """Where a too-long utterance is cut, relative to `committed`, or 0.

        Its age is measured from when its words first showed (one chunk
        earlier, as the fallback bounds do): a 30 s limit does not need the
        token times, and reading them on every step of a long utterance would
        cost more than the whole decision.
        """
        began = (self._seen if self._seen is not None else now) - self.chunk
        size = len(clean_text(raw))
        if now - began < self.max_utterance and size <= MAX_PENDING_CHARS:
            return 0
        words = list(_WORD.finditer(raw))
        if len(words) > KEEP_WORDS:
            cut = words[-KEEP_WORDS - 1].end()
        elif size > MAX_PENDING_CHARS:
            cut = len(raw.rstrip()) - KEEP_CHARS
        else:
            return 0
        return cut if cut > 0 and has_words(raw[:cut]) else 0

    def _should_renew(self, text: str, endpoint: bool, now: int) -> bool:
        """Whether this is a safe point to renew the stream at.

        SAFE: the endpoint holds and nothing worded is pending, so the last
        commit was an endpoint final and everything since is silence the
        decoder has heard (a forced final always leaves words pending).
        """
        if not endpoint or has_words(text[self.committed:]):
            self._quiet_since = None
            return False
        if self._quiet_since is None:
            self._quiet_since = now
        quiet = now - self._quiet_since
        if self.renew_wait is not None and self.committed > 0 and quiet >= self.renew_wait:
            return True  # the default: right after a final from this stream
        if quiet < self.renew_hold:
            return False
        return len(text) > self.renew_chars or now - self._renewed_at >= self.renew_after


# --------------------------------------------------------------------------
# Decode workers.
# --------------------------------------------------------------------------

_FLUSH = object()
_CLOSE = object()


#: A renewal's last check (DecodeWorker._still_silent): the pause must still
#: be a pause. Levels are the RMS of 20 ms windows of float PCM; the newest
#: audio is an onset when one of its windows stands above max(ONSET_FLOOR,
#: ONSET_RATIO x the pause's 20th-percentile window) -- the rule the
#: streaming benchmark uses to find where speech starts in its references.
ONSET_WINDOW = SAMPLE_RATE // 50
ONSET_FLOOR = 0.01
ONSET_RATIO = 3.0


def onset_after(pause: np.ndarray, newest: np.ndarray) -> bool:
    """Whether `newest` holds a sound that stands out of `pause`."""
    def levels(audio: np.ndarray) -> np.ndarray:
        n = len(audio) // ONSET_WINDOW
        if n == 0:
            return np.zeros(0, dtype=np.float32)
        frames = audio[:n * ONSET_WINDOW].reshape(n, ONSET_WINDOW)
        return np.sqrt(np.mean(frames * frames, axis=1))

    loud = levels(newest)
    if not len(loud):
        return False
    quiet = levels(pause)
    floor = float(np.percentile(quiet, 20)) if len(quiet) else 0.0
    return bool(loud.max() > max(ONSET_FLOOR, ONSET_RATIO * floor))


class Ring:
    """The newest `capacity` samples of a stream's client audio: a renewal's seed."""

    __slots__ = ("buf", "pos", "filled")

    def __init__(self, capacity: int):
        self.buf = np.zeros(max(0, capacity), dtype=np.float32)
        self.pos = 0
        self.filled = 0

    def write(self, samples: np.ndarray) -> None:
        cap = len(self.buf)
        n = len(samples)
        if cap == 0 or n == 0:
            return
        if n >= cap:
            self.buf[:] = samples[-cap:]
            self.pos, self.filled = 0, cap
            return
        end = self.pos + n
        if end <= cap:
            self.buf[self.pos:end] = samples
        else:
            head = cap - self.pos
            self.buf[self.pos:] = samples[:head]
            self.buf[:n - head] = samples[head:]
        self.pos = end % cap
        self.filled = min(cap, self.filled + n)

    def last(self, n: int) -> np.ndarray:
        cap = len(self.buf)
        n = min(n, self.filled)
        if n <= 0:
            return np.zeros(0, dtype=np.float32)
        start = (self.pos - n) % cap
        if start + n <= cap:
            return self.buf[start:start + n].copy()
        return np.concatenate((self.buf[start:], self.buf[:start + n - cap]))


class Slot:
    """One connection's stream, as its decode worker sees it.

    The event loop only appends to `inbox` (PCM arrays, _FLUSH, _CLOSE); the
    worker owns everything else. collections.deque append/popleft are atomic.
    """

    def __init__(self, *, language: str, segmenter: Segmenter, chunk: int, flush_pad: int, lead_pad: int,
                 seed: int, deliver: Callable[[List[dict]], None], loop: asyncio.AbstractEventLoop):
        self.language = language
        self.segmenter = segmenter
        #: Samples one decode step consumes (the profile's chunk, checked
        #: against the model when it loads).
        self.chunk = chunk
        self.flush_pad = flush_pad
        self.lead_pad = lead_pad
        #: `deliver` runs on `loop`, the loop of the connection that owns the slot.
        self.deliver = deliver
        self.loop = loop
        self.inbox: "collections.deque[Any]" = collections.deque()
        self.stream: Any = None
        #: Client samples fed so far: the client position of the audio's end.
        self.fed = 0
        #: Client position of the current recognizer stream's first client
        #: sample; the stream itself starts with `lead_pad` samples of silence.
        self.origin = 0
        #: Decode steps the current recognizer stream has taken.
        self.steps = 0
        self.recent = Ring(seed)
        self.flush_requested = False
        self.flushing = False
        self.finished = False
        self.compute_s = 0.0
        self._text: Optional[str] = None
        self._timing: Optional[TokenTimes] = None

    def to_client(self, seconds: float) -> int:
        """A stream time (seconds) -> client position."""
        return self.origin + max(0, int(round(seconds * SAMPLE_RATE)) - self.lead_pad)

    def position(self) -> int:
        """How far into the client's audio the decoder has got: exactly one
        chunk per decode step (measured on every deployed model), less the
        lead silence, never past what has arrived."""
        return min(self.fed, self.origin + max(0, self.steps * self.chunk - self.lead_pad))

    def timing(self, text: str, result: Any) -> Optional[TokenTimes]:
        """Token positions for `text`, rebuilt only when the text changed."""
        if result is None:
            return None
        if text != self._text:
            self._text = text
            try:
                self._timing = TokenTimes.build(text, list(result.tokens), list(result.timestamps),
                                                float(result.start_time), self.to_client)
            except (AttributeError, TypeError, ValueError):
                self._timing = None
        return self._timing

    def forget_timing(self) -> None:
        self._text = None
        self._timing = None


def _merge(out: List[dict], event: dict) -> None:
    """Append `event`, letting it replace the partial it supersedes."""
    if event["type"] in ("partial", "final") and out:
        last = out[-1]
        if last["type"] == "partial" and last["u"] == event["u"]:
            event["compute_ms"] = event.get("compute_ms", 0) + last.get("compute_ms", 0)
            out[-1] = event
            return
    out.append(event)


def coalesce(events: List[dict]) -> List[dict]:
    """Drop every partial a later event of the same batch supersedes.

    A replayed backlog (a reconnect resumes up to 60 s of audio at once), or
    a gateway that reads slower than the decoders write, queues many partials
    of one utterance; the client only needs the newest hypothesis, and a final
    supersedes its own partial. A dropped partial's compute_ms moves to the
    next event that is sent, so the sum still covers every decode step.
    """
    newest: Dict[int, int] = {}
    for index, event in enumerate(events):
        if event.get("type") in ("partial", "final"):
            newest[event["u"]] = index
    out: List[dict] = []
    carried = 0
    for index, event in enumerate(events):
        kind = event.get("type")
        if kind == "partial" and newest[event["u"]] != index:
            carried += event.get("compute_ms", 0)
            continue
        if carried and kind in ("partial", "final"):
            event["compute_ms"] = event.get("compute_ms", 0) + carried
            carried = 0
        out.append(event)
    return out


class DecodeWorker:
    """A thread that owns some streams of one profile and decodes them through
    the profile's shared recognizer."""

    def __init__(self, profile: Profile, index: int, recognizer: Any, settings: Settings, metrics: Metrics):
        self.profile = profile
        self.index = index
        self.recognizer = recognizer
        self.metrics = metrics
        self.lead = settings.lead_pad_ms * SAMPLE_RATE // 1000
        #: Streams assigned to this worker. Event-loop side only.
        self.active = 0
        self._adopt: "collections.deque[Slot]" = collections.deque()
        self._slots: List[Slot] = []
        self._wake = threading.Event()
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name=f"decode-{profile.id}-{index}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stopping = True
        self._wake.set()
        self._thread.join(timeout)

    def adopt(self, slot: Slot) -> None:
        self._adopt.append(slot)
        self._wake.set()

    def notify(self) -> None:
        self._wake.set()

    # -- the thread ----------------------------------------------------------

    def _run(self) -> None:
        while not self._stopping:
            self._wake.wait(0.5)
            self._wake.clear()
            try:
                self._pump()
            except Exception:  # noqa: BLE001 - a worker must outlive one bad pump
                log.exception("decode worker %s/%d failed a pump; its streams are closed", self.profile.id, self.index)
                for slot in list(self._slots):
                    self._fail(slot)

    def _send(self, slot: Slot, events: List[dict]) -> None:
        if not events:
            return
        try:
            slot.loop.call_soon_threadsafe(slot.deliver, events)
        except RuntimeError:
            pass  # the event loop is gone: the process is stopping

    def _fail(self, slot: Slot) -> None:
        if slot in self._slots:
            self._slots.remove(slot)
        slot.finished = True
        self._send(slot, [{"type": "error", "code": "internal", "message": "the stream failed to decode",
                           "retryable": True, "_kind": "error", "_close": 4500}])

    def _new_stream(self, slot: Slot) -> Any:
        stream = self.recognizer.create_stream()
        stream.set_option("language", slot.language)
        if self.lead:
            stream.accept_waveform(SAMPLE_RATE, np.zeros(self.lead, dtype=np.float32))
        return stream

    def _pump(self) -> None:
        recognizer = self.recognizer
        while self._adopt:
            slot = self._adopt.popleft()
            try:
                slot.stream = self._new_stream(slot)
            except Exception:  # noqa: BLE001
                log.exception("could not create a stream on %s/%d", self.profile.id, self.index)
                self._fail(slot)
                continue
            self._slots.append(slot)

        # Audio is moved into the streams on EVERY pass, not once per pump: a
        # reconnect's replay can keep this loop decoding for seconds, and the
        # other streams on this worker must not wait that long for their
        # newest audio to be decoded (or their events to be delivered). The
        # same goes for a Stop: a flushed stream is finished after EVERY
        # pass, not once the loop runs dry -- a neighbour's 60 s replay, or an
        # overload that never lets it run dry, would otherwise hold its last
        # final and `done` past the gateway's 5 s wait.
        while not self._stopping:
            self._feed()
            ready = [s for s in self._slots if recognizer.is_ready(s.stream)]
            if not ready:
                break
            started = time.perf_counter()
            try:
                recognizer.decode_streams([s.stream for s in ready])
            except Exception:  # noqa: BLE001
                log.exception("decode_streams failed on %s/%d", self.profile.id, self.index)
                for slot in ready:
                    self._fail(slot)
                continue
            elapsed = time.perf_counter() - started
            self.metrics.decode_step(self.profile.id, elapsed, len(ready))
            for slot in ready:
                slot.steps += 1
                # Every stream of the batch waited the whole call (see
                # Metrics.decode_step): compute_ms and the counter agree.
                slot.compute_s += elapsed
                self._observe(slot)
            self._finish_flushed()
        self._finish_flushed()

    def _finish_flushed(self) -> None:
        """Send the last final and `done` of every flushed stream that has
        nothing left to decode, and let it go."""
        recognizer = self.recognizer
        for slot in list(self._slots):
            if slot.flushing and not recognizer.is_ready(slot.stream):
                text, result = read_result(recognizer, slot.stream)
                events = slot.segmenter.finish(text, slot.fed, slot.timing(text, result))
                events.append({"type": "done", "_kind": "done"})
                self._send(slot, self._stamp(slot, events))
                slot.finished = True
                self._slots.remove(slot)

    def _feed(self) -> None:
        for slot in list(self._slots):
            chunks = []
            closed = False
            while slot.inbox:
                item = slot.inbox.popleft()
                if item is _CLOSE:
                    closed = True
                    break
                if item is _FLUSH:
                    slot.flush_requested = True
                elif not slot.flush_requested:
                    chunks.append(item)
            if closed:
                self._slots.remove(slot)
                slot.finished = True
                continue
            if chunks:
                samples = chunks[0] if len(chunks) == 1 else np.concatenate(chunks)
                slot.stream.accept_waveform(SAMPLE_RATE, samples)
                slot.fed += len(samples)
                slot.recent.write(samples)
            if slot.flush_requested and not slot.flushing:
                if slot.flush_pad:
                    slot.stream.accept_waveform(SAMPLE_RATE, np.zeros(slot.flush_pad, dtype=np.float32))
                slot.stream.input_finished()
                slot.flushing = True

    def _observe(self, slot: Slot) -> None:
        recognizer = self.recognizer
        text, result = read_result(recognizer, slot.stream)
        events, renew = slot.segmenter.observe(text, recognizer.is_endpoint(slot.stream), slot.position(),
                                               slot.timing(text, result))
        self._send(slot, self._stamp(slot, events))
        # Only once the stream has CAUGHT UP (too little undecoded audio for
        # another step): the renewal drops the old stream's undecoded tail,
        # and only a short tail is inside the seed. During a replay backlog
        # the renewal simply waits; the silence that asked for it is still
        # there when the decoder reaches the live edge.
        if (renew and not slot.flush_requested and not recognizer.is_ready(slot.stream)
                and self._still_silent(slot)):
            self._renew(slot)

    @staticmethod
    def _still_silent(slot: Slot) -> bool:
        """Whether the newest audio is still the pause the renewal is for.

        The recognizer says "endpoint, nothing pending" until the next
        utterance's first token is EMITTED, which is a step or two after it
        begins. A renewal in that gap loses nothing (the seed carries the
        onset), but the fresh stream must first re-decode its whole seed, up
        to renew_seed_s: measured on the worker with pauses near the 2 s
        threshold, the first partial's p50 rose from 543 to 792 ms (English)
        and 571 to 850 ms (Hindi). So a renewal waits for the next pause when
        the undecoded tail, or the last chunk decoded, already sounds louder
        than the pause behind it. It can only hold a renewal back; it never
        decides what is decoded.
        """
        audio = slot.recent.last(slot.recent.filled)
        start = slot.fed - len(audio)                      # client position of audio[0]
        newest = slot.position() - max(slot.chunk, SAMPLE_RATE // 5)
        split = min(len(audio), max(0, newest - start))
        pause_from = min(split, max(0, slot.segmenter.floor - start))
        return not onset_after(audio[pause_from:split], audio[split:])

    def _renew(self, slot: Slot) -> None:
        """Swap the slot onto a fresh stream at a safe point.

        A FRESH STREAM, NOT recognizer.reset(). For these models reset() also
        re-initialises the encoder's cache, so it is no gentler than a new
        stream, and it keeps the audio already queued but not yet decoded: the
        lead pad a restarted model needs (8C) could then only go AFTER that
        tail, in the middle of whatever it holds. A fresh stream gets the pad
        and the stream's language first (_new_stream), then a SEED: the client
        audio from where the last final ended up to the newest sample.

        WHERE THE SEED STARTS is the no-loss argument. At a safe point nothing
        worded is pending, so the last final was an endpoint final (a forced
        one always leaves words pending), and an endpoint final ends inside
        the silence that closed it, half the endpoint's silence past its last
        token (Segmenter._bounds). Re-decoding from there cannot repeat a
        committed word, and it covers everything after it: the rest of the
        silence, the undecoded tail the old stream takes with it (the renewal
        waits until the stream has caught up, so that tail is under one step
        plus the right context: 0.27 s at 160 ms, 0.67 s at 560 ms, measured)
        and any onset inside that tail. It is the point a reconnect resumes
        from, where it was measured on the real models: 0 of 120 English and
        0 of 80 Hindi resumes repeated a committed word, and no English one
        clipped the next utterance's first word. When the renewal had to wait
        for the decoder and the silence outgrew the ring, the newest
        renew_seed_s (2 s) of it are the seed: still more than the tail plus
        a chunk.
        """
        keep = min(slot.recent.filled, max(0, slot.fed - slot.segmenter.floor))
        seed = slot.recent.last(keep)
        try:
            stream = self._new_stream(slot)
            if len(seed):
                stream.accept_waveform(SAMPLE_RATE, seed)
        except Exception:  # noqa: BLE001
            log.exception("could not renew a stream on %s/%d", self.profile.id, self.index)
            self._fail(slot)
            return
        now = slot.position()
        slot.stream = stream
        slot.origin = slot.fed - len(seed)
        slot.steps = 0
        slot.forget_timing()
        slot.segmenter.after_renew(now)
        self.metrics.renewal(self.profile.id)

    @staticmethod
    def _stamp(slot: Slot, events: List[dict]) -> List[dict]:
        """compute_ms: decode wall time this stream waited on since its
        previous event (every stream of a batch is charged the whole call,
        as stt_stream_compute_seconds_total is)."""
        out: List[dict] = []
        for event in events:
            if event["type"] in ("partial", "final"):
                event["compute_ms"] = int(round(slot.compute_s * 1000))
                slot.compute_s = 0.0
            _merge(out, event)
        return out


# --------------------------------------------------------------------------
# The engine: profiles, recognizers, admission.
# --------------------------------------------------------------------------

RecognizerFactory = Callable[[Profile, Settings], Any]


def build_sherpa_recognizer(profile: Profile, settings: Settings) -> Any:
    """The real recognizer: ONE per profile, shared by its decode workers."""
    import sherpa_onnx  # imported here so the tests run without it

    def path(name: str) -> str:
        return os.path.join(profile.dir, name)

    return sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=path("tokens.txt"),
        encoder=path("encoder.int8.onnx"),
        decoder=path("decoder.int8.onnx"),
        joiner=path("joiner.int8.onnx"),
        num_threads=settings.threads,
        sample_rate=SAMPLE_RATE,
        feature_dim=80,
        enable_endpoint_detection=True,
        rule1_min_trailing_silence=RULE1_TRAILING_S,
        rule2_min_trailing_silence=settings.endpoint_s,
        rule3_min_utterance_length=RULE3_UTTERANCE_S,
        decoding_method="greedy_search",
        provider="cpu",
    )


def _warm(recognizer: Any) -> int:
    """Silence through a throwaway stream, so the first real stream does not
    pay ORT's first-call costs and `ready` means ready. Returns the samples
    one decode step consumes, measured to the 10 ms feature frame: the
    decoder's position is counted in steps (Slot.position), so a profile
    whose chunk_ms is not its model's chunk must not load."""
    frame = np.zeros(SAMPLE_RATE // 100, dtype=np.float32)
    stream = recognizer.create_stream()
    fed = 0
    while not recognizer.is_ready(stream):
        if fed >= 10 * SAMPLE_RATE:
            raise RuntimeError("the model was not ready to decode after 10 s of audio")
        stream.accept_waveform(SAMPLE_RATE, frame)
        fed += len(frame)
    recognizer.decode_streams([stream])
    step = 0
    while not recognizer.is_ready(stream) and step < 10 * SAMPLE_RATE:
        stream.accept_waveform(SAMPLE_RATE, frame)
        step += len(frame)
    stream.accept_waveform(SAMPLE_RATE, np.zeros(SAMPLE_RATE, dtype=np.float32))
    stream.input_finished()
    while recognizer.is_ready(stream):
        recognizer.decode_streams([stream])
    return step


@dataclass
class ProfileRuntime:
    profile: Profile
    workers: List[DecodeWorker] = field(default_factory=list)
    active: int = 0
    _next: int = 0

    def pick_worker(self) -> DecodeWorker:
        """Least-loaded worker, round-robin among equals: plain round-robin
        would pile new streams onto a worker whose earlier ones stayed open."""
        count = len(self.workers)
        order = [self.workers[(self._next + i) % count] for i in range(count)]
        self._next = (self._next + 1) % count
        return min(order, key=lambda w: w.active)


class Engine:
    def __init__(self, settings: Settings, factory: RecognizerFactory):
        self.settings = settings
        self.factory = factory
        self.metrics = Metrics([p.id for p in settings.profiles])
        self.runtimes: List[ProfileRuntime] = [ProfileRuntime(p) for p in settings.profiles]
        self.ready = False
        self.error: Optional[str] = None
        self.load_seconds: Optional[float] = None
        #: Budget units the open streams hold (Settings.max_cost). Admission
        #: and release both run on the event loop, so it needs no lock.
        self.cost_in_use = 0
        self._lock = threading.Lock()
        self._stopped = False

    def load(self) -> None:
        """Build and warm each profile's recognizer, then start its workers.
        Runs in a thread at startup.

        All or nothing: a profile that cannot load leaves the engine not ready
        with the reason on /health, rather than serving some languages and
        refusing others for a reason no client can see.
        """
        started = time.perf_counter()
        built: List[DecodeWorker] = []
        for runtime in self.runtimes:
            profile = runtime.profile
            if self._stopped:
                return
            try:
                recognizer = self.factory(profile, self.settings)
                step = _warm(recognizer)
            except Exception as exc:  # noqa: BLE001
                log.exception("profile %s failed to load", profile.id)
                # The type only: an ORT message quotes the model path.
                self.error = f"profile {profile.id!r} failed to load ({type(exc).__name__})"
                return
            if abs(step - profile.chunk_ms * SAMPLE_RATE // 1000) > SAMPLE_RATE // 100:
                self.error = (f"profile {profile.id!r} says chunk_ms {profile.chunk_ms} but its model "
                              f"decodes {step * 1000 // SAMPLE_RATE} ms per step")
                log.error("%s; the engine stays not ready", self.error)
                return
            built.extend(DecodeWorker(profile, index, recognizer, self.settings, self.metrics)
                         for index in range(self.settings.workers))
        with self._lock:
            if self._stopped:
                return
            for runtime in self.runtimes:
                runtime.workers = [w for w in built if w.profile is runtime.profile]
            for worker in built:
                worker.start()
            self.load_seconds = round(time.perf_counter() - started, 2)
            self.ready = True
        log.info("ready: %d profiles, one recognizer each, x %d workers x %d threads in %.1fs",
                 len(self.runtimes), self.settings.workers, self.settings.threads, self.load_seconds)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self.ready = False
            workers = [w for runtime in self.runtimes for w in runtime.workers]
        for worker in workers:
            worker.stop()

    def authorized(self, header: Optional[str]) -> bool:
        if not self.settings.token:
            return True
        scheme, _, credential = (header or "").partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(credential.strip().encode("utf-8"), self.settings.token.encode("utf-8"))

    def admit(self, language: str) -> Tuple[Optional[ProfileRuntime], Optional[str], str]:
        """(runtime, allow-list spelling of the language, refusal reason),
        with the stream's place RESERVED when a runtime is returned: the
        caller must hand it back with release().

        The first profile in list order that allows the language, has a free
        slot, and whose cost still fits the engine's budget. A wide-chunk
        profile after a full fast one is therefore where a nearly spent
        budget still has room (it costs half as much). A language no profile
        allows is a client error; one whose profiles are all full, or whose
        streams no longer fit the budget, is capacity, worth a retry.
        """
        allowed = False
        for runtime in self.runtimes:
            spelled = runtime.profile.supports(language)
            if spelled is None:
                continue
            allowed = True
            if (runtime.active < runtime.profile.max_streams
                    and self.cost_in_use + runtime.profile.cost <= self.settings.max_cost):
                runtime.active += 1
                self.cost_in_use += runtime.profile.cost
                return runtime, spelled, ""
        return None, None, "capacity" if allowed else "language"

    def release(self, runtime: ProfileRuntime) -> None:
        """Give back what admit() reserved for one stream."""
        runtime.active -= 1
        self.cost_in_use -= runtime.profile.cost

    def capacity(self) -> int:
        """The most streams the engine could hold at once: its slots, or as
        many of the cheapest profile's streams as the budget pays for."""
        slots = sum(r.profile.max_streams for r in self.runtimes)
        cheapest = min(r.profile.cost for r in self.runtimes)
        return min(slots, self.settings.max_cost // cheapest)

    def health(self) -> dict:
        models = list(dict.fromkeys(r.profile.model for r in self.runtimes))
        return {
            "ready": self.ready,
            "streams": sum(r.active for r in self.runtimes),
            "capacity": self.capacity(),
            "cost": {"budget": self.settings.max_cost, "in_use": self.cost_in_use},
            "model": ",".join(models),
            "profiles": [
                {"id": r.profile.id, "model": r.profile.model, "chunk_ms": r.profile.chunk_ms,
                 "active": r.active, "max_streams": r.profile.max_streams, "cost": r.profile.cost,
                 "languages": list(r.profile.languages)}
                for r in self.runtimes
            ],
            "workers": self.settings.workers,
            "threads": self.settings.threads,
            "load_seconds": self.load_seconds,
            "error": self.error,
        }

    def gauges(self) -> List[Tuple[str, Tuple[Tuple[str, str], ...], float]]:
        rows: List[Tuple[str, Tuple[Tuple[str, str], ...], float]] = [
            ("stt_stream_ready", (), 1.0 if self.ready else 0.0),
            ("stt_stream_cost_budget_units", (), float(self.settings.max_cost)),
            ("stt_stream_cost_in_use_units", (), float(self.cost_in_use)),
        ]
        for runtime in self.runtimes:
            labels = (("profile", runtime.profile.id),)
            rows.append(("stt_stream_active_streams", labels, float(runtime.active)))
            rows.append(("stt_stream_capacity_streams", labels, float(runtime.profile.max_streams)))
        return rows


# --------------------------------------------------------------------------
# One connection.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Start:
    first_sample: int
    first_u: int
    language: str
    mode: str = "dictation"
    #: The sender's frame length, as sent (the message ceiling clamps it).
    frame_ms: float = float(FRAME_MS)


def parse_start(text: str) -> Tuple[Optional[Start], str]:
    """The start message, or why it is not one."""
    try:
        message = json.loads(text)
    except (ValueError, RecursionError):
        # RecursionError, not ValueError, is how json gives up on a few
        # thousand nested brackets, which fit in one message.
        return None, "the first message must be a JSON start message"
    if not isinstance(message, dict) or message.get("type") != "start":
        return None, "the first message must be start"
    if message.get("v", 1) != 1:
        return None, "unsupported protocol version"
    if message.get("sample_rate") != SAMPLE_RATE or message.get("encoding") != "pcm_s16le":
        return None, "only pcm_s16le at 16000 Hz is accepted"
    if message.get("channels", 1) != 1:
        return None, "only mono is accepted"
    mode = message.get("mode", "dictation")
    if not isinstance(mode, str) or mode not in MODES:
        return None, "mode must be dictation or meeting"
    numbers = {}
    for key, ceiling in (("first_sample", 1 << 40), ("first_u", 1 << 30)):
        value = message.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < ceiling:
            return None, f"{key} must be a non-negative integer"
        numbers[key] = value
    frame_ms = message.get("frame_ms", FRAME_MS)
    if isinstance(frame_ms, bool) or not isinstance(frame_ms, (int, float)) or (
            isinstance(frame_ms, float) and not math.isfinite(frame_ms)):
        return None, "frame_ms must be a finite number"
    language = message.get("language", "auto")
    if language is None or language == "":
        language = "auto"
    if not isinstance(language, str) or not _LANGUAGE.match(language):
        return None, "language must be 'auto' or a code like en, hi or en-US"
    return Start(numbers["first_sample"], numbers["first_u"], language, mode, float(frame_ms)), ""


def message_rate(frame_ms: float) -> float:
    """Messages a second the message ceiling allows after its head start:
    MESSAGES_FACTOR frames a second of frame_ms, clamped to [MIN_FRAME_MS,
    FRAME_MS] -- a shorter frame buys more messages, a longer one no fewer
    than the browser's own 40 ms."""
    return MESSAGES_FACTOR * 1000.0 / min(max(frame_ms, MIN_FRAME_MS), FRAME_MS)


def _dumps(event: dict) -> str:
    # allow_nan=False: NaN and Infinity are not JSON, and a peer's strict
    # parser would drop the stream over them.
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


#: What a send to a peer that has gone raises: starlette's disconnect, its
#: RuntimeError for a socket already closed, or the transport's OSError.
_PEER_GONE = (WebSocketDisconnect, RuntimeError, OSError)


class Connection:
    """Admission, then two loops: this one reads the socket, a task writes it."""

    def __init__(self, ws: WebSocket, engine: Engine):
        self.ws = ws
        self.engine = engine
        self.settings = engine.settings
        self.queue: "asyncio.Queue[dict]" = asyncio.Queue()
        self.runtime: Optional[ProfileRuntime] = None
        self.worker: Optional[DecodeWorker] = None
        self.slot: Optional[Slot] = None
        self.outcome: Optional[str] = None
        self.close_code: Optional[int] = None
        self.language = "auto"
        self.samples = 0
        self.messages = 0
        self.message_rate = message_rate(FRAME_MS)
        self.flushed = False

    async def refuse(self, reason: str, code: str, message: str, retryable: bool, close: int) -> None:
        """Before admission, nothing else writes, so this writes directly."""
        self.engine.metrics.reject(reason)
        try:
            await self.ws.send_text(_dumps({"type": "error", "code": code, "message": message, "retryable": retryable}))
            await self.ws.close(close)
        except Exception:  # noqa: BLE001 - the peer may already be gone
            pass

    async def run(self) -> None:
        if not self.engine.ready:
            await self.refuse("not_ready", "engine_unavailable", "the engine is still loading", True, 4503)
            return
        start = await self._read_start()
        if start is None:
            return
        runtime, language, refusal = self.engine.admit(start.language)
        if runtime is None or language is None:
            if refusal == "language":
                await self.refuse("language", "unsupported_language",
                                  "no profile on this engine transcribes that language", False, 4400)
            else:
                await self.refuse("capacity", "capacity",
                                  "no profile for that language has room (its streams, or the engine's decode "
                                  "budget, are full)", True, 4429)
            return
        try:
            await self._stream(start, runtime, language)
        finally:
            self.engine.release(runtime)

    async def _stream(self, start: Start, runtime: ProfileRuntime, language: str) -> None:
        """An admitted stream, from ready to close."""
        profile = runtime.profile
        chunk = profile.chunk_ms * SAMPLE_RATE // 1000
        meeting = start.mode == "meeting"
        segmenter = Segmenter(
            first_sample=start.first_sample, first_u=start.first_u, chunk_samples=chunk,
            endpoint_s=self.settings.meeting_endpoint_s if meeting else self.settings.endpoint_s,
            recognizer_endpoint_s=self.settings.endpoint_s,
            max_utterance_s=self.settings.meeting_max_utterance_s if meeting else self.settings.max_utterance_s,
            renew_silence_s=self.settings.renew_silence_s, renew_chars=self.settings.renew_chars,
            renew_hold_s=self.settings.renew_hold_s, renew_after_s=self.settings.renew_after_s,
        )
        slot = Slot(language=language, segmenter=segmenter, chunk=chunk,
                    flush_pad=profile.flush_pad_ms * SAMPLE_RATE // 1000,
                    lead_pad=self.settings.lead_pad_ms * SAMPLE_RATE // 1000,
                    seed=int(round(self.settings.renew_seed_s * SAMPLE_RATE)),
                    deliver=self._deliver, loop=asyncio.get_running_loop())
        worker = runtime.pick_worker()
        self.runtime, self.language, self.worker, self.slot = runtime, language, worker, slot
        self.message_rate = message_rate(start.frame_ms)
        self.queue.put_nowait({
            "type": "ready", "v": 1, "sample_rate": SAMPLE_RATE, "frame_ms": FRAME_MS,
            "max_frame_bytes": MAX_FRAME_BYTES, "resume_from_sample": start.first_sample,
            "next_u": start.first_u, "first_sample": start.first_sample, "first_u": start.first_u,
            "profile": profile.id, "chunk_ms": profile.chunk_ms, "language": language, "mode": start.mode,
        })
        started = time.monotonic()
        worker.active += 1
        worker.adopt(slot)
        sender = asyncio.create_task(self._send_loop())
        try:
            if await self._receive_loop(started) and not sender.done():
                # The loop queued an error and a close of its own: let the
                # writer deliver them (bounded; a stuck peer is not waited on).
                try:
                    async with asyncio.timeout(5.0):
                        await sender
                except TimeoutError:
                    pass
        finally:
            if not sender.done():
                sender.cancel()
            slot.inbox.append(_CLOSE)
            worker.notify()
            worker.active -= 1
            outcome = self.outcome or "disconnected"
            self.engine.metrics.stream_closed(profile.id, outcome)
            log.info("stream closed %s", _dumps({
                "profile": profile.id, "language": language, "mode": start.mode, "outcome": outcome,
                "code": self.close_code, "duration_s": round(time.monotonic() - started, 1),
                "audio_s": round(self.samples / SAMPLE_RATE, 1),
                "finals": segmenter.finals, "renewals": segmenter.renewals,
            }))

    async def _read_start(self) -> Optional[Start]:
        try:
            async with asyncio.timeout(self.settings.start_timeout_s):
                message = await self.ws.receive()
        except TimeoutError:
            await self.refuse("protocol", "protocol", "no start message in time", False, 4400)
            return None
        if message["type"] == "websocket.disconnect":
            return None
        text = message.get("text")
        if text is None:
            await self.refuse("protocol", "protocol", "the first message must be start", False, 4400)
            return None
        start, problem = parse_start(text)
        if start is None:
            await self.refuse("protocol", "protocol", problem, False, 4400)
        return start

    def _deliver(self, events: List[dict]) -> None:
        """Runs on the event loop (call_soon_threadsafe from a worker)."""
        for event in events:
            self.queue.put_nowait(event)

    def _fail(self, outcome: str, code: str, message: str, retryable: bool, close: int) -> None:
        """Queue the error and the close behind whatever is already queued."""
        if self.outcome is None:
            self.outcome = outcome
        self.queue.put_nowait({"type": "error", "code": code, "message": message, "retryable": retryable,
                               "_kind": "error", "_close": close})

    async def _receive_loop(self, started: float) -> bool:
        """Read until the peer leaves or a close is queued.

        True when this loop queued an error and a close itself (the caller
        waits for the writer to send them); False when the socket is gone.
        """
        loop = asyncio.get_running_loop()
        assert self.slot is not None and self.worker is not None and self.runtime is not None
        slot, worker, profile_id = self.slot, self.worker, self.runtime.profile.id
        #: No audio for idle_s closes the stream -- until a flush, after which
        #: no audio is expected and only the flush's own deadline applies.
        deadline = loop.time() + self.settings.idle_s
        ceiling_slack = self.settings.resume_max_s + ARRIVAL_SLACK_S
        while True:
            try:
                async with asyncio.timeout_at(deadline):
                    message = await self.ws.receive()
            except TimeoutError:
                if self.close_code is not None:
                    return False  # we closed and the peer never answered
                if self.flushed:
                    self._fail("error", "internal", "the flush did not finish in time", True, 4500)
                else:
                    self._fail("idle", "idle", f"no audio for {self.settings.idle_s:g} s", True, 4408)
                return True
            if message["type"] == "websocket.disconnect":
                if self.close_code is None:
                    code = message.get("code")
                    self.close_code = code
                    if self.outcome is None:
                        self.outcome = "shutdown" if code == 1012 else "disconnected"
                return False
            if self.close_code is not None or self.outcome is not None:
                continue  # closing: drain until the peer's close arrives
            # THE MESSAGE CEILING, before the message is looked at: counting
            # samples never bounded MESSAGES (2-byte frames, pings), and each
            # costs this event loop, which every stream shares, about what a
            # 40 ms frame costs. The gateway's rule (build spec section 11).
            self.messages += 1
            if self.messages > (time.monotonic() - started + ceiling_slack) * self.message_rate:
                self._fail("rate_limited", "rate_limited", "messages are arriving faster than a live stream sends them",
                           True, 4429)
                return True
            data = message.get("bytes")
            if data is not None:
                if self.flushed:
                    self._fail("protocol", "protocol", "audio after flush", False, 4400)
                    return True
                size = len(data)
                if size > MAX_FRAME_BYTES:
                    self._fail("protocol", "frame_too_large", f"frames are at most {MAX_FRAME_BYTES} bytes", False, 4413)
                    return True
                if size < 2 or size % 2:
                    self._fail("protocol", "protocol", "a frame is an even number of bytes of int16 PCM", False, 4400)
                    return True
                count = size // 2
                self.samples += count
                if self.samples > (time.monotonic() - started + ceiling_slack) * SAMPLE_RATE:
                    self._fail("rate_limited", "rate_limited", "audio is arriving faster than a resume allows", True, 4429)
                    return True
                pcm = np.frombuffer(data, dtype="<i2").astype(np.float32)
                pcm *= np.float32(1.0 / 32768.0)
                slot.inbox.append(pcm)
                worker.notify()
                self.engine.metrics.audio(profile_id, count / SAMPLE_RATE)
                deadline = loop.time() + self.settings.idle_s
                continue
            was_flushed = self.flushed
            problem = self._control(message.get("text"), slot, worker)
            if problem:
                self._fail("protocol", "protocol", problem, False, 4400)
                return True
            if self.flushed and not was_flushed:
                deadline = loop.time() + self.settings.flush_timeout_s

    def _control(self, text: Optional[str], slot: Slot, worker: DecodeWorker) -> str:
        """A text message after start; returns a protocol problem or ""."""
        try:
            message = json.loads(text or "")
        except (ValueError, RecursionError):
            return "text messages are JSON"
        kind = message.get("type") if isinstance(message, dict) else None
        if kind == "ping":
            stamp = message.get("t")
            if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or (
                    isinstance(stamp, float) and not math.isfinite(stamp)):
                # NaN and Infinity parse (Python's json accepts them) but would
                # go back out as a pong no strict parser reads.
                return "ping carries a finite number t"
            self.queue.put_nowait({"type": "pong", "t": stamp})
            return ""
        if kind == "flush":
            if not self.flushed:
                self.flushed = True
                slot.inbox.append(_FLUSH)
                worker.notify()
            return ""
        if kind == "client_stats":
            return ""  # the gateway's business, harmless if forwarded
        return "unknown message type"

    async def _send_loop(self) -> None:
        metrics = self.engine.metrics
        while True:
            batch = [await self.queue.get()]
            while not self.queue.empty():
                batch.append(self.queue.get_nowait())
            for event in coalesce(batch):
                close = event.pop("_close", None)
                kind = event.pop("_kind", None)
                try:
                    payload = _dumps(event)
                except (TypeError, ValueError) as exc:
                    # A bug, not the peer. Say so (the event's type and the
                    # error's, never its text) and fail the stream: skipping
                    # the event would leave the stream running with events
                    # silently missing, and stopping would leave it running
                    # with none at all.
                    log.error("a %s event could not be encoded (%s); the stream is closed with 4500",
                              event.get("type"), type(exc).__name__)
                    payload = _dumps({"type": "error", "code": "internal", "message": "the engine could not encode an event",
                                      "retryable": True})
                    kind, close = "error", 4500
                if kind == "error" and self.outcome is None:
                    self.outcome = "error"
                try:
                    await self.ws.send_text(payload)
                    if kind is not None:
                        metrics.event(kind)
                    if event.get("type") == "done":
                        self.outcome = self.outcome or "completed"
                        close = 1000
                    if close is not None:
                        self.close_code = close
                        await self.ws.close(close)
                        return
                except _PEER_GONE as exc:
                    log.debug("stopped sending: the peer went away (%s)", type(exc).__name__)
                    return
                except Exception:  # noqa: BLE001 - logged: a stream whose events stop must say why
                    log.exception("stopped sending: the send failed unexpectedly")
                    return


# --------------------------------------------------------------------------
# The application.
# --------------------------------------------------------------------------


async def _deny(ws: WebSocket) -> None:
    """Refuse the upgrade BEFORE accept: a close before accept, which uvicorn
    answers with a bare HTTP 403. Not send_denial_response: uvicorn's
    websockets-sansio never marks that handshake complete and logs "ASGI
    callable returned without completing handshake" at ERROR for every
    refusal (0.46 and 0.54, measured 2026-09-29), and 0.46 wrote clients an
    invalid HTTP response -- a wrong token would fill the error log."""
    try:
        await ws.close(code=1008)
    except Exception:  # noqa: BLE001 - the peer may already be gone
        pass


def create_app(settings: Settings, recognizer_factory: Optional[RecognizerFactory] = None) -> FastAPI:
    engine = Engine(settings, recognizer_factory or build_sherpa_recognizer)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Loaded in a thread so /health answers (ready: false) while the
        # recognizers load. A load failure is RECORDED, not raised, the same
        # promise compose/whisper/server.py makes: a process that exits
        # restart-loops and tells nobody why.
        asyncio.get_running_loop().run_in_executor(None, engine.load)
        try:
            yield
        finally:
            engine.stop()

    app = FastAPI(title="stt-stream", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.engine = engine

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(engine.health())

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(engine.metrics.render(engine.gauges()),
                                 media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.websocket("/v1/stream")
    async def stream(ws: WebSocket) -> None:
        if not engine.authorized(ws.headers.get("authorization")):
            engine.metrics.reject("unauthorized")
            await _deny(ws)
            return
        offered = ws.scope.get("subprotocols") or []
        await ws.accept(subprotocol=SUBPROTOCOL if SUBPROTOCOL in offered else None)
        await Connection(ws, engine).run()

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env(os.environ)
    except ConfigError as exc:
        log.error("refusing to start: %s", exc)
        raise SystemExit(2) from None
    import uvicorn

    log.info("binding %s:%s", settings.bind, settings.port)
    uvicorn.run(
        create_app(settings),
        host=settings.bind,
        port=settings.port,
        # websockets-sansio: no per-connection protocol task on top of the
        # handler's own, and the implementation the orchestrator's gateway
        # runs too. PCM does not compress, so no permessage-deflate: it would
        # only spend CPU the decoders need.
        ws="websockets-sansio",
        ws_max_size=WS_MAX_MESSAGE_BYTES,
        ws_per_message_deflate=False,
        ws_ping_interval=20.0,
        ws_ping_timeout=20.0,
        log_level="info",
        access_log=False,
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
