"""POST /audio/transcribe — the composer's microphone, server side.

THE CONTRACT. Audio in, text out, nothing kept. The bytes are read under a
size cap, sent to the local engine, and dropped when the request ends. No
temporary file is written, no row records what was said, and the transcript
goes back to the browser as a DRAFT: it becomes a message only if the person
presses Send, through the ordinary chat path, exactly as if they had typed it.

WHY THE RECORDING IS THE BODY AND NOT A MULTIPART FIELD. Because "no temporary
file is written" has to be TRUE, and with `UploadFile` it is not: Starlette
parses a multipart body into a SpooledTemporaryFile whose max_size is a class
attribute fixed at 1 MB, so every recording longer than about ninety seconds
rolls over onto the container's disk before this module sees a byte of it.
That is invisible, unconfigurable per route, and exactly the promise this
feature is sold on. Reading `request.stream()` ourselves keeps the audio in
this process's memory for the length of one call and nowhere else. The two
scalars a multipart form used to carry — how long the browser thinks it
recorded, and which language to force — are query parameters, which is all
they ever needed to be.

WHAT IS RECORDED is metadata and only metadata — who, how long, which
language, how fast, whether it worked. That is what the admin console reports
and it is deliberately not enough to reconstruct anything anyone said.

AUTHORIZATION. A signed-in user, like every other upload route. This is not a
public transcription service: an open ASR endpoint on a GPU is a free
denial-of-service against the chat model sharing it.

A LONG CLIP IS ANSWERED WHILE IT DECODES. Whisper's long-form pass took
268.3 s for 595 s of audio on a quiet replica and 219.7 s for 300 s on a busy
one (2026-09-18), and Cloudflare gives up on a first byte at 125 s — so a
route that said nothing until the transcript existed capped public dictation
at a few minutes of audio while the composer records ten. Work that is not finished after HEARTBEAT_S is answered
with a streamed 200: one whitespace byte now and every HEARTBEAT_S after
(leading whitespace is insignificant in JSON), then the JSON itself. Anything
known before that point keeps its own status line.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from . import asr, db, metrics
from .auth import UserRow, require_user
from .authn.principal import Principal, require_capability
from .authn.rbac import Cap
from .config import settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/audio", tags=["audio"])


#: What a browser's MediaRecorder actually produces, plus the formats a
#: desktop might hand over. The engine decodes through PyAV (bundled ffmpeg),
#: so this list is about refusing obvious nonsense early, not about capability.
#:
#: The CONTENT TYPE is checked, never the filename: an extension is a claim by
#: the client, and the engine is the thing that finally decides whether bytes
#: are audio.
ALLOWED_TYPES = {
    "audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/mpga",
    "audio/wav", "audio/x-wav", "audio/wave", "audio/flac", "audio/x-flac",
    "audio/aac", "audio/m4a", "audio/x-m4a", "audio/opus", "audio/3gpp",
    "video/webm",  # Chrome labels an audio-only MediaRecorder blob this way
    "video/mp4",   # Safari does the same
}

#: Below this there is nothing to transcribe — a tap on the button, not
#: speech. Refused with a message the UI can show, rather than sent to a GPU.
_MIN_BYTES = 1024

#: Seconds of work before the answer becomes a streamed 200, and between
#: heartbeat bytes after that. The same cadence /v1's streams keep; far under
#: Cloudflare's 125 s first-byte limit, and long enough that a short
#: dictation — the common case, 2-4 s — is still one plain JSON response.
HEARTBEAT_S = 15.0

_BEAT = b" "

#: `no-transform` is what keeps a compressing hop (Next's own compression,
#: the Cloudflare edge) from holding the bytes back to find something worth
#: compressing — a heartbeat that is buffered is not a heartbeat.
_STREAM_HEADERS = {
    "cache-control": "no-store, no-transform",
    "x-accel-buffering": "no",
}

# --------------------------------------------------------------------------
# Per-user rate limit.
#
# The same sliding window the web-search path uses (engines/search.rate_ok),
# for the same reason and with the same honest limitation: it is in-process,
# so it bounds one container. That is the whole deployment today. It exists to
# stop a stuck client retrying in a loop, not to stop a determined attacker —
# the concurrency pool in app/asr.py is what protects the GPU.
# --------------------------------------------------------------------------
_recent: dict[int, list[float]] = {}


def rate_ok(user_id: int) -> bool:
    now = time.monotonic()
    window = [t for t in _recent.get(user_id, []) if now - t < 60.0]
    if len(window) >= settings.asr_rate_per_min:
        _recent[user_id] = window
        return False
    window.append(now)
    _recent[user_id] = window
    return True


def reset_for_tests() -> None:
    _recent.clear()
    _IN_FLIGHT.clear()
    asr.POOL.reset_for_tests()
    asr.BATCH_POOL.reset_for_tests()


async def require_voice(request: Request) -> None:
    """Refuse when this account may not dictate (V17 feature access).

    THIS is the real gate: audio lands here, so it is a hard 403 rather than a
    downgrade. The composer hides the microphone for these accounts, but
    hiding is a courtesy — a stale tab or a direct API call reaches this.
    """
    from .authn import features as feature_access
    from .authn.principal import require_principal

    principal = await require_principal(request)
    if not feature_access.allowed(principal.features, feature_access.Feature.VOICE_INPUT):
        raise HTTPException(
            status_code=403,
            detail="Voice input is turned off for your account. Ask an administrator.",
        )


async def _read_capped(request: Request) -> bytes:
    """The recording, or 413 — the cap is enforced WHILE reading.

    The failure this shape prevents is an out-of-memory kill of the process
    that streams everybody's answers: `await request.body()` would let anyone
    with a session hand this container as many megabytes as they can send
    before anything measured them.
    """
    cap = settings.asr_max_upload_bytes
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > cap:
            raise HTTPException(
                status_code=413,
                detail=(
                    "That recording is too long to transcribe. "
                    f"Keep it under {settings.asr_max_audio_seconds // 60} minutes."
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)


#: The engine's fallback endpoint is multipart and wants a filename. It is
#: derived from the CONTENT TYPE rather than taken from the client, because a
#: name the browser chose tells us nothing and travelling through this process
#: would only give it somewhere to be logged.
_FILENAMES = {
    "audio/mp4": "recording.mp4", "video/mp4": "recording.mp4",
    "audio/m4a": "recording.m4a", "audio/x-m4a": "recording.m4a",
    "audio/ogg": "recording.ogg", "audio/opus": "recording.opus",
    "audio/mpeg": "recording.mp3", "audio/mpga": "recording.mp3",
    "audio/wav": "recording.wav", "audio/x-wav": "recording.wav",
    "audio/wave": "recording.wav", "audio/flac": "recording.flac",
    "audio/x-flac": "recording.flac", "audio/aac": "recording.aac",
    "audio/3gpp": "recording.3gp",
}


@router.post("/transcribe")
async def transcribe(
    request: Request,
    # Seconds, as the browser measured them. Advisory: it is used to refuse a
    # too-long clip before spending a GPU on it and to report duration in the
    # console, and it is never trusted as a fact about the bytes.
    duration_ms: int = Query(0),
    language: str = Query("auto"),
    user: UserRow = Depends(require_user),
    _voice: None = Depends(require_voice),
) -> Any:
    """Transcribe one recording and return it as editable text: a JSON object,
    or — past HEARTBEAT_S of work — a streamed 200 that ends in one."""
    if not settings.asr_enabled:
        # 404, not 503: a deployment without a speech engine does not have
        # this feature, and the composer hides the button for the same reason.
        raise HTTPException(status_code=404, detail="voice input is not enabled")

    user_id = int(user["id"])
    if not rate_ok(user_id):
        raise HTTPException(
            status_code=429,
            detail="Too many recordings just now. Wait a moment and try again.",
        )

    content_type = (request.headers.get("content-type") or "").split(";")[0]
    content_type = content_type.strip().lower()
    if content_type and content_type not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=415, detail=f"{content_type} is not a supported audio format."
        )

    # A duration is whatever the client typed into a query string. A negative
    # one would survive into voice_transcriptions and pull the console's
    # "minutes dictated" below zero, which is a number no report can explain.
    duration_ms = max(0, duration_ms)
    if duration_ms > settings.asr_max_audio_seconds * 1000:
        raise HTTPException(
            status_code=413,
            detail=(
                "That recording is longer than "
                f"{settings.asr_max_audio_seconds // 60} minutes."
            ),
        )

    started = time.perf_counter()
    audio = await _read_capped(request)
    upload_ms = _since(started)

    if len(audio) < _MIN_BYTES:
        raise HTTPException(
            status_code=422, detail="That recording was too short to transcribe."
        )

    wanted = (language or "auto").strip() or "auto"
    task = asyncio.ensure_future(
        _transcribe_or_refuse(
            audio,
            content_type=content_type,
            language=wanted if wanted != "auto" else settings.asr_language,
            user_id=user_id,
            duration_ms=duration_ms,
            started=started,
            upload_ms=upload_ms,
        )
    )
    # Held until the engine answers, whatever happens to this request: see
    # `_hold_until_done`. Never cancelled from here on.
    _hold_until_done(task)
    done, _pending = await asyncio.wait({task}, timeout=HEARTBEAT_S)
    if done:
        # Finished (or refused) before the heartbeat was due: an ordinary
        # response with its real status line.
        return task.result()
    return StreamingResponse(
        _heartbeat_then(task), media_type="application/json", headers=_STREAM_HEADERS
    )


async def _heartbeat_then(task: "asyncio.Future[dict]") -> AsyncIterator[bytes]:
    """Whitespace until the work is done, then its JSON.

    The status line has already gone out as 200, so a failure from here on is
    carried IN the body as {"detail", "status"} — the same sentence and the
    same status the early path would have sent, which the browser maps the
    same way. A client that leaves does NOT cancel the work: see
    `_hold_until_done`.
    """
    while True:
        yield _BEAT
        done, _pending = await asyncio.wait({task}, timeout=HEARTBEAT_S)
        if done:
            break
    try:
        payload: dict = task.result()
    except HTTPException as exc:
        payload = {"detail": exc.detail, "status": exc.status_code}
    except Exception:  # noqa: BLE001
        # Past the status line an escaped exception would only cut the body
        # off mid-way; the browser is owed a failure it can read.
        log.error("ASR transcription failed after the heartbeat started", exc_info=task.exception())
        payload = {"detail": _GENERIC_FAILURE, "status": 503}
    # The same encoding Starlette's JSONResponse uses for the early path.
    yield json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


#: Every transcription in flight, held until the engine answers. asyncio
#: keeps only a weak reference to a task, so work whose client has gone —
#: nobody awaits it any more — could be collected mid-flight; this set is
#: what keeps it (and its pool slot) alive.
_IN_FLIGHT: "set[asyncio.Future[dict]]" = set()


def _hold_until_done(task: "asyncio.Future[dict]") -> None:
    """Keep the work running if its client goes, and its pool slot taken.

    NOT CANCELLED, on purpose (security review, 2026-09-18). The speech
    server queues every request on one lock and decodes in an executor that
    a closed connection cannot stop, so cancelling here freed the dictation
    pool slot — the only bound on GPU work — while the engine kept decoding.
    Measured with real sockets: six clients that each hung up 0.4 s after
    the first heartbeat left a backlog of 6 decodes on a one-slot engine with
    the pool reading 0, and the next person's 4 s clip waited 7.5 s behind
    the orphans. Letting the task finish keeps the slot taken for exactly as
    long as the engine is busy, which is what 4810da0 did by never noticing
    the hang-up. Registered for EVERY request, not in a `finally`: a client
    that leaves before the first heartbeat is sent never starts the stream's
    generator, so its cleanup would never run. The outcome is still recorded
    (the row and the metric), and the exception, if any, is consumed so an
    abandoned failure is not logged as unretrieved.
    """
    _IN_FLIGHT.add(task)

    def _done(finished: "asyncio.Future[dict]") -> None:
        _IN_FLIGHT.discard(finished)
        if not finished.cancelled():
            finished.exception()

    task.add_done_callback(_done)


_GENERIC_FAILURE = "Transcription couldn't be completed. Please try again."

#: Seconds of decoding per second of audio on a BUSY replica: 300 s of audio
#: took 219.7 s with other clips queued on it (2026-09-18).
_LOADED_DECODE_S_PER_AUDIO_S = 0.73


def _timeout_detail(duration_ms: int) -> str:
    """The 504's sentence: advice the person can act on.

    "Try a shorter one" helps only when the clip's own length could have used
    up the budget — when decoding it on a busy replica takes at least half of
    ASR_TIMEOUT_S (411 s of audio at the 600 s default). A shorter clip that
    timed out met a stuck or queued engine, and recording less would not have
    helped; neither does a clip whose length the browser did not report.
    """
    decode_s = duration_ms / 1000.0 * _LOADED_DECODE_S_PER_AUDIO_S
    if duration_ms and decode_s >= settings.asr_timeout_s / 2:
        return "That recording took too long to transcribe. Try a shorter one."
    return "The speech engine did not answer in time. Please try again."


async def _transcribe_or_refuse(
    audio: bytes,
    *,
    content_type: str,
    language: str,
    user_id: int,
    duration_ms: int,
    started: float,
    upload_ms: int,
) -> dict:
    """The engine call and its bookkeeping: the transcript's JSON, or an
    HTTPException carrying the status and sentence the person should see."""
    try:
        result = await asr.transcribe(
            audio,
            filename=_FILENAMES.get(content_type, "recording.webm"),
            content_type=content_type or "audio/webm",
            language=language,
        )
    except asr.ASRBusy:
        await _record(user_id, duration_ms, None, _since(started), "busy")
        raise HTTPException(
            status_code=503,
            detail="Transcription is busy right now. Try again in a moment.",
        ) from None
    except asr.ASRRejected as exc:
        await _record(user_id, duration_ms, None, _since(started), "rejected")
        log.info("ASR refused a clip: %s", exc)
        raise HTTPException(
            status_code=422, detail="That recording could not be transcribed."
        ) from None
    except asr.ASRTimeout as exc:
        # BEFORE ASRUnavailable, which it subclasses. V19's status vocabulary
        # (a CHECK constraint) has no 'timeout', and the engine did not
        # deliver, so the row says 'unavailable'; the counter below is what
        # tells a timeout from an outage on the console's Prometheus half.
        await _record(user_id, duration_ms, None, _since(started), "unavailable")
        metrics.inc("asr_timeouts_total", "dictations the speech engine did not answer in time")
        log.warning("ASR engine did not answer in time: %s", exc)
        raise HTTPException(status_code=504, detail=_timeout_detail(duration_ms)) from None
    except asr.ASRUnavailable as exc:
        await _record(user_id, duration_ms, None, _since(started), "unavailable")
        log.warning("ASR engine unavailable: %s", exc)
        raise HTTPException(status_code=503, detail=_GENERIC_FAILURE) from None
    except Exception:  # noqa: BLE001
        # The catch-all, and the reason `status = 'error'` exists in V19.
        # Anything the engine client did not anticipate — a 200 whose body is
        # an HTML proxy page, a shape nobody has seen — would otherwise leave
        # FastAPI to answer 500 with a stack trace id, write no row, and make
        # the console's error rate a count of the failures we happened to
        # name. A person holding a microphone gets a sentence instead.
        await _record(user_id, duration_ms, None, _since(started), "error")
        log.exception("ASR transcription failed unexpectedly")
        raise HTTPException(status_code=503, detail=_GENERIC_FAILURE) from None

    total_ms = _since(started)
    await _record(
        user_id, duration_ms, result.language, total_ms, "ok", degraded=result.degraded
    )

    # No model name, no engine URL, no provider — a member has no reason to
    # learn the platform's internals from a dictation box.
    return {
        "text": result.text,
        "language": result.language,
        "language_code": result.language_code,
        "duration_ms": duration_ms or None,
        "processing_ms": total_ms,
        "upload_ms": upload_ms,
        "engine_ms": result.engine_ms,
    }


@router.get("/health")
async def health(
    _principal: Principal = Depends(require_capability(Cap.ANALYTICS_READ)),
) -> dict:
    """Whether dictation can work right now.

    OPERATIONAL, not a member's business. It names the model and the engine's
    queue depth, and POST /transcribe goes to some trouble never to tell a
    member either — a signed-in gate here would have handed the same
    reconnaissance back through a second door. The composer does not poll
    this: it learns the feature exists from /auth/me. Missing the capability
    is 404, like the rest of the admin surface, so the route does not confirm
    its own existence to someone probing for it.
    """
    if not settings.asr_enabled:
        return {"enabled": False, "ready": False, "reason": "voice input is disabled"}
    # No engine is installed. `provider()` raises rather than returning a stub,
    # and an administrator asking whether dictation works deserves that answer
    # rather than an "enabled, not ready" that hides the reason.
    try:
        engine = asr.provider()
    except asr.ASRUnavailable as exc:
        return {"enabled": True, "ready": False, "model": None,
                "active": 0, "waiting": 0, "engines": [], "reason": str(exc)}
    ready = await engine.health()
    fleet = engine.stats() if hasattr(engine, "stats") else []
    return {
        "enabled": True,
        "ready": ready,
        "model": getattr(engine, "model", None),
        "active": asr.POOL.active,
        "waiting": asr.POOL.waiting,
        # One row per engine, so a half-down fleet is visible as exactly that
        # rather than as an "enabled, ready" that quietly lost half its
        # capacity.
        "engines": fleet,
        "reason": "" if ready else "no speech engine is answering",
    }


def _since(started: float) -> int:
    """Milliseconds of wall clock since `started`.

    Used on the failure paths too: the wait before a 503 is still a wait
    somebody sat through, and analytics.voice_totals averages processing_ms
    across every attempt. Recording 0 there would silently make that average
    a average of the successes only.
    """
    return int((time.perf_counter() - started) * 1000)


async def _record(
    user_id: int,
    duration_ms: int,
    language: Optional[str],
    processing_ms: int,
    status: str,
    *,
    degraded: bool = False,
) -> None:
    """One metadata row per attempt. Never the audio, never the transcript.

    Failures are recorded too — an error rate computed only from successes is
    not an error rate, and "voice never works for me" is a claim the console
    should be able to check.
    """
    try:
        await db.run_in_thread(
            db.record_voice_transcription,
            user_id=user_id,
            # 0 here means the client never told us how long it recorded, and
            # a clip of zero seconds would drag every average down. NULL says
            # "not reported", which is what happened.
            duration_ms=duration_ms or None,
            language=language,
            # NOT coerced the same way: this one WE measured, so 0 is the
            # honest answer for a failure that came back faster than a
            # millisecond, and NULL would claim it was never timed.
            processing_ms=processing_ms,
            status=status,
            degraded=degraded,
        )
    except Exception:  # noqa: BLE001 — telemetry must never fail a request
        log.debug("voice transcription not recorded", exc_info=True)
    if status != "ok":
        metrics.inc("asr_errors_total", "transcriptions that did not return text")
