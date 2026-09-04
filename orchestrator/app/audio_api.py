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
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

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
    asr.POOL.reset_for_tests()


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
) -> dict:
    """Transcribe one recording and return it as editable text."""
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
    try:
        result = await asr.transcribe(
            audio,
            filename=_FILENAMES.get(content_type, "recording.webm"),
            content_type=content_type or "audio/webm",
            language=wanted if wanted != "auto" else settings.asr_language,
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
    except asr.ASRUnavailable as exc:
        await _record(user_id, duration_ms, None, _since(started), "unavailable")
        log.warning("ASR engine unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Transcription couldn't be completed. Please try again.",
        ) from None
    except Exception:  # noqa: BLE001
        # The catch-all, and the reason `status = 'error'` exists in V19.
        # Anything the engine client did not anticipate — a 200 whose body is
        # an HTML proxy page, a shape nobody has seen — would otherwise leave
        # FastAPI to answer 500 with a stack trace id, write no row, and make
        # the console's error rate a count of the failures we happened to
        # name. A person holding a microphone gets a sentence instead.
        await _record(user_id, duration_ms, None, _since(started), "error")
        log.exception("ASR transcription failed unexpectedly")
        raise HTTPException(
            status_code=503,
            detail="Transcription couldn't be completed. Please try again.",
        ) from None

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
    ready = await asr.provider().health()
    return {
        "enabled": True,
        "ready": ready,
        "model": settings.asr_model,
        "active": asr.POOL.active,
        "waiting": asr.POOL.waiting,
        "reason": "" if ready else "the speech engine is not answering",
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
