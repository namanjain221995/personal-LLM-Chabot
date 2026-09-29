"""The composer's microphone, server side: two paths.

RECORDING SESSIONS (/audio/sessions/*, since 2026-09-29) are what the
composer uses: the recording arrives in parts while it is made, is STORED per
user (the owner asked for that), has no length limit, and is transcribed in
voice-activity windows as it arrives. The routes are at the end of this file;
the work is in app/dictation.py, whose docstring has the design and the
measurements behind it.

POST /audio/transcribe is the LEGACY one-request path, kept exactly as it was
for stale tabs, direct callers and deployments with VOICE_SESSIONS_ENABLED
off. Its defects stay with it on purpose: the engine judges a clip by its
first 30 s, so a long recording that opens with a pause comes back empty, and
it refuses anything over ASR_MAX_AUDIO_SECONDS. The rest of this docstring is
about that path.

THE CONTRACT. Audio in, text out, nothing kept here. The bytes are read under
a size cap, sent to the local engine, and dropped when the request ends. This
process writes no temporary file and no row records what was said. The ENGINE
does write one, briefly: it takes the clip as a multipart field and Starlette
spools any part over 1,048,576 bytes to the engine container's /tmp while it
decodes (measured 2026-09-28 on starlette 1.6.0), so the promise that the
audio exists "in memory on both ends and nowhere else" was never true for a
dictation over about a minute. The transcript goes back to the browser as a
DRAFT: it becomes a message only if the person presses Send, through the
ordinary chat path, exactly as if they had typed it.

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

AND THAT WAIT IS BOUNDED. The heartbeat is what makes a long wait survivable,
not a licence for an unbounded one: one dictation is ONE ASR_TIMEOUT_S of
engine time for both of its decodes together (app/asr.VLLMAudioProvider
.transcribe), so the wall clock this route serves is bounded by the same
number the engine client times out on — never the other way round. The
cancellation rule is unchanged and deliberate: the deadline belongs to the
engine call, because cancelling HERE would free the pool slot while the
engine kept decoding (see `_hold_until_done`).
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
    from . import dictation

    dictation.reset_for_tests()
    _POLLS.clear()
    voice_live.reset_for_tests()


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


def _timeout_detail(duration_ms: int) -> str:
    """The 504's sentence: advice the person can act on.

    "Try a shorter one" helps only when the clip's own length could have used
    up the budget — when decoding it on a busy replica takes at least half of
    ASR_TIMEOUT_S (411 s of audio at the 600 s default). A shorter clip that
    timed out met a stuck or queued engine, and recording less would not have
    helped; neither does a clip whose length the browser did not report.

    The rate is `asr.LOADED_DECODE_S_PER_AUDIO_S` — the same measurement the
    engine client sizes its own bounds from, kept in one place so this
    sentence and the deadline that produces it can never disagree.
    """
    decode_s = duration_ms / 1000.0 * asr.LOADED_DECODE_S_PER_AUDIO_S
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
    #
    # `confidence` is the one field that is about the ANSWER rather than the
    # clock: one of asr.CONFIDENCE_* or null. It is a closed vocabulary, not a
    # sentence, because the wording belongs to the browser — and it names no
    # engine, no threshold and no probability, so it stays as free of
    # reconnaissance as the rest of this reply.
    return {
        "text": result.text,
        "language": result.language,
        "language_code": result.language_code,
        "duration_ms": duration_ms or None,
        "processing_ms": total_ms,
        "upload_ms": upload_ms,
        "engine_ms": result.engine_ms,
        "confidence": result.confidence,
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

    `live` is live dictation's gateway (app/voice_live.py): whether it is
    configured, the streams open, and its engines by number, never address.
    """
    live = voice_live.health()
    if not settings.asr_enabled:
        return {"enabled": False, "ready": False, "reason": "voice input is disabled", "live": live}
    # No engine is installed. `provider()` raises rather than returning a stub,
    # and an administrator asking whether dictation works deserves that answer
    # rather than an "enabled, not ready" that hides the reason.
    try:
        engine = asr.provider()
    except asr.ASRUnavailable as exc:
        return {"enabled": True, "ready": False, "model": None,
                "active": 0, "waiting": 0, "engines": [], "reason": str(exc), "live": live}
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
        "live": live,
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


# ===========================================================================
# Recording sessions (/audio/sessions/*, 2026-09-29)
#
# Thin routes over app/dictation.py. Every refusal is a FLAT body,
# {"detail": sentence, "reason": code, ...}, returned as a JSONResponse so it
# is not nested under FastAPI's own "detail"; a signed-out caller still gets
# require_user's 401. The browser owns the wording; the reason is a closed
# vocabulary it maps (frontend/lib/voice.ts).
# ===========================================================================

import hashlib  # noqa: E402
from datetime import datetime as _datetime  # noqa: E402

from fastapi import WebSocket  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from starlette.requests import ClientDisconnect  # noqa: E402

from . import dictation  # noqa: E402
from . import voice_live  # noqa: E402


def _flat(status: int, reason: str, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "reason": reason, **extra})


def _refused(exc: "dictation.SessionError") -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.body())


async def _voice_refusal(request: Request) -> Optional[JSONResponse]:
    """require_voice's rule with the flat body: 403 voice_off."""
    try:
        await require_voice(request)
    except HTTPException as exc:
        if exc.status_code == 403:
            return _flat(403, "voice_off", str(exc.detail))
        raise
    return None


def _int_param(value: Any, *, minimum: int = 0) -> Optional[int]:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= minimum else None


async def _json_body(request: Request) -> Optional[dict]:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — any unreadable body is a bad request
        return None
    return payload if isinstance(payload, dict) else None


def _not_json(request: Request) -> Optional[JSONResponse]:
    """415 unless the body is declared application/json. `request.json()`
    ignores the content type, so without this a cross-site text/plain POST (a
    form, needing no preflight) could start, stop or retranscribe somebody's
    recording on the strength of their SameSite=Lax cookie alone (security
    review item 13, server side)."""
    declared = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if declared != "application/json":
        return _flat(415, "bad_content_type", "The body must be sent as application/json.")
    return None


#: Session long-polls open now, per person (security review item 11): a
#: second tab is normal, a dozen is a stuck page holding database polls.
_POLLS: dict[int, int] = {}

#: How often a long-poll for a session run by ANOTHER process re-reads its row.
#: Was 1 s; Postgres has 60 connection slots for everything.
_ROW_POLL_S = 2.5


@router.post("/sessions")
async def create_session(request: Request, user: UserRow = Depends(require_user)) -> Any:
    """Open a recording session: 201 with the session state and the recorder's
    config, or 200 with the same session for a retried client_key.

    "continues": "<session id>" opens a CONTINUATION of a recording the
    server idle-closed while the browser was offline: the audio the browser
    still holds goes into the new session, linked to the old one, with its
    own transcript (app/dictation.create)."""
    refusal = await _voice_refusal(request) or _not_json(request)
    if refusal is not None:
        return refusal
    if not settings.asr_enabled:
        return _flat(404, "voice_unavailable", "Voice input isn't available on this server right now.")
    if not settings.voice_sessions_enabled:
        return _flat(404, "sessions_off", "Long recordings aren't enabled on this server.")
    payload = await _json_body(request)
    if payload is None:
        return _flat(400, "bad_request", "The body must be a JSON object.")
    continues = payload.get("continues")
    if continues is not None and not isinstance(continues, str):
        return _flat(400, "bad_request", "continues must be a session id.")
    try:
        row, created = await db.run_in_thread(
            lambda: dictation.create(
                int(user["id"]), payload.get("client_key"), str(payload.get("mime_type") or ""),
                continues=continues,
            )
        )
    except dictation.SessionError as exc:
        return _refused(exc)
    if created or row["status"] in dictation.LIVE_STATUSES:
        dictation.start_worker(row["id"])
    body = dictation.state(row)
    body["config"] = dictation.config()
    return JSONResponse(status_code=201 if created else 200, content=body)


@router.put("/sessions/{session_id}/parts/{seq}")
async def put_part(
    session_id: str,
    seq: str,
    request: Request,
    cursor: str = Query("0"),
    user: UserRow = Depends(require_user),
) -> Any:
    """Store one part of the recording. 200 means its bytes are fsynced into
    the stored recording and the browser may forget them; nothing short of a
    200 gives that permission."""
    refusal = await _voice_refusal(request)
    if refusal is not None:
        return refusal
    number = _int_param(seq)
    since = _int_param(cursor)
    sha = (request.headers.get("x-part-sha256") or "").strip().lower()
    if number is None or since is None:
        return _flat(400, "bad_request", "seq and cursor must be non-negative integers.")
    if not dictation._SHA256.match(sha):
        return _flat(400, "bad_request", "X-Part-SHA256 must be the part's lowercase hex SHA-256.")
    user_id = int(user["id"])
    try:
        await db.run_in_thread(dictation._owned_row, session_id, user_id)
    except dictation.SessionError as exc:
        return _refused(exc)
    if not dictation._rate_ok("part", user_id, settings.voice_part_per_min):
        return _flat(429, "rate_limited", "Too many parts in a minute. Send this one again shortly.")
    limit = dictation.part_limit_bytes()
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                return _flat(
                    413, "part_too_large", f"A part may be at most {limit} bytes.", part_limit_bytes=limit
                )
            chunks.append(chunk)
    except ClientDisconnect:
        return _flat(408, "part_incomplete", "The part was cut off before it all arrived.")
    body = b"".join(chunks)
    if not body:
        return _flat(400, "bad_request", "The part is empty.")
    if hashlib.sha256(body).hexdigest() != sha:
        return _flat(422, "part_corrupt", "The part's bytes do not match its SHA-256.")
    try:
        row, duplicate = await db.run_in_thread(dictation.append_part, user_id, session_id, number, body, sha)
    except dictation.SessionError as exc:
        if exc.reason in ("storage_full", "quota_full"):
            dictation.RUNNER.notify(session_id, status=dictation.STATUS_FINISHING)
        response = _refused(exc)
        if exc.reason == "too_fast":
            response.headers["Retry-After"] = str(exc.extra.get("retry_after_s", 1))
        return response
    dictation.RUNNER.notify(
        session_id, bytes_stored=row["bytes_stored"], next_part=row["next_part"], rev=row["rev"]
    )
    return {"accepted": number, "duplicate": duplicate, **dictation.state(row, cursor=since)}


@router.post("/sessions/{session_id}/finish")
async def finish_session(session_id: str, request: Request, user: UserRow = Depends(require_user)) -> Any:
    """The person pressed Stop (or the recorder did): 202 while the rest is
    transcribed, 200 for a finish that already happened."""
    refusal = await _voice_refusal(request) or _not_json(request)
    if refusal is not None:
        return refusal
    payload = await _json_body(request)
    if payload is None:
        payload = {}
    last_part = payload.get("last_part")
    if last_part is not None and (isinstance(last_part, bool) or not isinstance(last_part, int)):
        return _flat(400, "bad_request", "last_part must be an integer or null.")
    ended_by = str(payload.get("ended_by") or "person")
    try:
        row, started = await db.run_in_thread(
            lambda: dictation.finish(int(user["id"]), session_id, last_part=last_part, ended_by=ended_by)
        )
    except dictation.SessionError as exc:
        return _refused(exc)
    return JSONResponse(status_code=202 if started else 200, content=dictation.state(row))


@router.get("/sessions")
async def list_sessions(
    request: Request,
    limit: str = Query("50"),
    before: Optional[str] = Query(None),
    user: UserRow = Depends(require_user),
) -> Any:
    """The caller's own recordings, newest first. A stored recording its owner
    cannot find or delete would be retention without consent, so this needs
    only a signed-in owner: NOT the VOICE_INPUT feature (security review item
    7), which gates making recordings, never reaching the ones you have."""
    count = _int_param(limit, minimum=1)
    if count is None or count > 100:
        return _flat(400, "bad_request", "limit must be 1 to 100.")
    cutoff = None
    if before:
        # `next_before` is isoformat with "+00:00". Sent unencoded, the query
        # string turns that "+" into a space; sent as %2B it arrives as "+".
        # Both are the same instant.
        text = before.strip().replace(" ", "+").replace("Z", "+00:00")
        try:
            cutoff = _datetime.fromisoformat(text)
        except ValueError:
            return _flat(400, "bad_request", "before must be an ISO 8601 time.")
    return await db.run_in_thread(
        lambda: dictation.list_sessions(int(user["id"]), limit=count, before=cutoff)
    )


@router.get("/sessions/{session_id}")
async def session_state(
    session_id: str,
    request: Request,
    cursor: str = Query("0"),
    since_rev: str = Query("-1"),
    wait_s: str = Query("0"),
    user: UserRow = Depends(require_user),
) -> Any:
    """The session state, as a long-poll: answered as soon as `rev` moves past
    `since_rev`, or after `wait_s` (at most 25 s) unchanged. Owner only, no
    feature gate (item 7): a finished recording's text must stay readable.
    At most VOICE_LONG_POLLS_PER_USER open at once per person (429)."""
    since = _int_param(cursor)
    try:
        rev_seen = int(since_rev)
        wait = float(wait_s)
    except (TypeError, ValueError):
        return _flat(400, "bad_request", "since_rev and wait_s must be numbers.")
    if since is None:
        return _flat(400, "bad_request", "cursor must be a non-negative integer.")
    wait = max(0.0, min(float(dictation.LONG_POLL_MAX_S), wait))
    user_id = int(user["id"])
    try:
        row = await db.run_in_thread(dictation._owned_row, session_id, user_id)
    except dictation.SessionError as exc:
        return _refused(exc)
    if row.get("audio_deleted_at") is not None:
        return _refused(dictation._not_found())
    if row["status"] in dictation.LIVE_STATUSES and dictation.RUNNER.get(session_id) is None:
        # Nobody in this process runs it: adopt it if its lease has lapsed.
        dictation.RUNNER.ensure(session_id)
    if wait > 0 and dictation.current_rev(row) <= rev_seen:
        if _POLLS.get(user_id, 0) >= settings.voice_long_polls_per_user:
            response = _flat(
                429, "too_many_polls", "Too many open requests for your recordings. Close other tabs and try again.",
            )
            response.headers["Retry-After"] = "3"
            return response
        _POLLS[user_id] = _POLLS.get(user_id, 0) + 1
        try:
            row = await _long_poll(row, session_id, rev_seen, wait)
        finally:
            left = _POLLS.get(user_id, 1) - 1
            if left > 0:
                _POLLS[user_id] = left
            else:
                _POLLS.pop(user_id, None)
    try:
        row = await db.run_in_thread(dictation._owned_row, session_id, user_id)
    except dictation.SessionError as exc:
        return _refused(exc)
    return dictation.state(row, cursor=since)


async def _long_poll(row: dict, session_id: str, rev_seen: int, wait: float) -> dict:
    deadline = time.monotonic() + wait
    while dictation.current_rev(row) <= rev_seen and time.monotonic() < deadline:
        if dictation.RUNNER.get(session_id) is not None:
            # Run here: its rev is in memory, and cheap to look at.
            await asyncio.sleep(0.25)
            continue
        # Run elsewhere, or just finished here: the row is the only source.
        fresh = await db.run_in_thread(dictation._row, session_id)
        if fresh is None:
            break
        row = fresh
        if dictation.current_rev(row) > rev_seen:
            break
        await asyncio.sleep(min(_ROW_POLL_S, max(0.0, deadline - time.monotonic())))
    return row


@router.delete("/sessions/{session_id}", response_class=Response)
async def discard_session(session_id: str, request: Request, user: UserRow = Depends(require_user)) -> Any:
    """The person's discard: the recording and its transcript are deleted.
    Owner only, no feature gate: a person whose voice input was turned off
    must still be able to delete what the microphone stored (item 7)."""
    try:
        await db.run_in_thread(dictation.discard, int(user["id"]), session_id)
    except dictation.SessionError as exc:
        return _refused(exc)
    return Response(status_code=204)


@router.post("/sessions/{session_id}/retranscribe")
async def retranscribe_session(session_id: str, request: Request, user: UserRow = Depends(require_user)) -> Any:
    """Transcribe the STORED audio again (the failed windows, or all of it):
    'Try again' no longer means 'say it all again'."""
    refusal = await _voice_refusal(request) or _not_json(request)
    if refusal is not None:
        return refusal
    payload = await _json_body(request) or {}
    scope = str(payload.get("scope") or "gaps")
    try:
        row, started = await db.run_in_thread(dictation.retranscribe, int(user["id"]), session_id, scope)
    except dictation.SessionError as exc:
        return _refused(exc)
    return JSONResponse(status_code=202 if started else 200, content=dictation.state(row))


def _recording_response(row: dict) -> FileResponse:
    path = dictation.audio_file(row)
    stamp = row["created_at"].strftime("%Y%m%d-%H%M") if row.get("created_at") else "recording"
    return FileResponse(
        path,
        media_type=row["mime_type"],
        filename=f"recording-{stamp}.{row['ext']}",
        headers={
            "Cache-Control": "no-store",
            "X-Recording-Complete": "false" if row["status"] == dictation.STATUS_RECORDING else "true",
        },
    )


@router.get("/sessions/{session_id}/audio")
async def session_audio(session_id: str, request: Request, user: UserRow = Depends(require_user)) -> Any:
    """The stored recording, exactly as the browser encoded it. Its owner
    only, with no feature gate (item 7); a super admin uses the audited admin
    route."""
    try:
        row = await db.run_in_thread(dictation._owned_row, session_id, int(user["id"]))
        return _recording_response(row)
    except dictation.SessionError as exc:
        return _refused(exc)


@router.websocket("/sessions/{session_id}/live")
async def live_session(websocket: WebSocket, session_id: str) -> None:
    """The live preview of a recording being made (2026-09-29): 16 kHz PCM
    in, partial and final text out, relayed to the streaming engine by
    app/voice_live.py. A WebSocket gets none of this app's middleware and no
    Request-typed dependency, so every check (Origin, sign-in, VOICE_INPUT,
    ownership, limits) is made there, not here. Nothing it does touches the
    recording session, whose transcript stays the record."""
    await voice_live.serve(websocket, session_id)


# ---------------------------------------------------------------------------
# A super admin reading a member's recordings (audited)
#
# SUPER ADMIN ONLY, because the owner said "super admin": stricter than
# rbac.may_inspect alone, under which an admin may also read a member's
# conversations. The role is checked here because this change does not touch
# rbac.py (a dedicated Cap.VOICE_RECORDINGS_READ is the cleaner home, left to
# whoever owns that file). Every refusal is 404, as on the rest of the admin
# surface. Ownership is re-derived from the row, so a session id in the URL
# cannot reach another member's recording. main.py mounts only this module's
# `router`, so these routes ride on it with their own full paths.
# ---------------------------------------------------------------------------

_admin = APIRouter(tags=["admin"])


async def _admin_member(principal: Principal, user_id: int) -> dict:
    """The member whose recordings a SUPER ADMIN is reaching: a current
    member under the inspection rule, or one REMOVED from this workspace,
    whose recordings removal kept (authn/admin_api.voice_member)."""
    from .authn.admin_api import voice_member
    from .authn.rbac import Role

    if Role(principal.role) is not Role.SUPER_ADMIN:
        raise HTTPException(status_code=404, detail="No such member.")
    return await voice_member(principal, user_id)


async def _admin_session(principal: Principal, user_id: int, session_id: str) -> dict:
    await _admin_member(principal, user_id)
    row = await db.run_in_thread(dictation._row, session_id)
    if row is None or int(row["user_id"]) != int(user_id) or row["status"] == dictation.STATUS_CANCELLED:
        raise HTTPException(status_code=404, detail="No such recording.")
    return row


async def _admin_audit(principal: Principal, request: Request, action: str, user_id: int, **kwargs: Any) -> None:
    if int(user_id) == int(principal.user_id):
        return  # reading your own is not oversight
    from .authn.principal import audit

    await db.run_in_thread(lambda: audit(principal, request, action, target_user_id=user_id, **kwargs))


@_admin.get("/admin/api/members/{user_id}/voice")
async def admin_member_recordings(
    user_id: int,
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    principal: Principal = Depends(require_capability(Cap.WORKSPACE_CONTENT_READ)),
) -> dict:
    await _admin_member(principal, user_id)
    listing = await db.run_in_thread(
        lambda: dictation.list_sessions(user_id, limit=limit, before=None, preview=False)
    )
    from .authn.admin_api import READ_AUDIT_COALESCE_S

    await _admin_audit(
        principal, request, "admin_listed_voice_recordings", user_id,
        meta={"limit": limit, "count": len(listing["sessions"])},
        coalesce_seconds=READ_AUDIT_COALESCE_S,
    )
    return listing


@_admin.get("/admin/api/members/{user_id}/voice/{session_id}/audio")
async def admin_member_recording_audio(
    user_id: int,
    session_id: str,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.WORKSPACE_CONTENT_READ)),
) -> Any:
    row = await _admin_session(principal, user_id, session_id)
    try:
        response = _recording_response(row)
    except dictation.SessionError as exc:
        return _refused(exc)
    await _admin_audit(
        principal, request, "admin_downloaded_voice_recording", user_id,
        resource_type="voice_session", resource_id=session_id,
    )
    return response


@_admin.get("/admin/api/members/{user_id}/voice/{session_id}/transcript")
async def admin_member_recording_transcript(
    user_id: int,
    session_id: str,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.WORKSPACE_CONTENT_READ)),
) -> Any:
    row = await _admin_session(principal, user_id, session_id)
    if row.get("audio_deleted_at") is not None:
        return _flat(410, "audio_deleted", "This recording has been deleted.")
    transcript = await db.run_in_thread(dictation.transcript_of, row)
    await _admin_audit(
        principal, request, "admin_read_voice_transcript", user_id,
        resource_type="voice_session", resource_id=session_id,
    )
    return transcript


@_admin.delete("/admin/api/members/{user_id}/voice/{session_id}", response_class=Response)
async def admin_delete_member_recording(
    user_id: int,
    session_id: str,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.WORKSPACE_CONTENT_READ)),
) -> Any:
    """A super admin deletes a REMOVED member's recording. The owner was told
    (2026-09-29) that removal keeps recordings and that a super admin can
    list, play and delete them; nobody else can reach them any more. A
    current member deletes their own, so this refuses them (409). The audit
    event is written BEFORE the deletion and a failure to write it refuses
    the deletion: an irreversible act on another person's data must never
    go unrecorded (security review item 14)."""
    member = await _admin_member(principal, user_id)
    if not member.get("removed"):
        return _flat(409, "member_active", "A current member deletes their own recordings.")
    await _admin_session(principal, user_id, session_id)
    from .authn import store as authn_store
    from .authn.sessions import client_meta

    ip, agent = client_meta(request)
    try:
        await db.run_in_thread(
            lambda: authn_store.record_audit(
                workspace_id=principal.workspace_id, actor_user_id=principal.user_id,
                action="admin_deleted_voice_recording", target_user_id=user_id,
                resource_type="voice_session", resource_id=session_id, ip=ip, user_agent=agent,
            )
        )
    except Exception:  # noqa: BLE001 — refused, not deleted, when unaudited
        log.exception("voice recording %s not deleted: its audit event could not be written", session_id)
        return _flat(503, "audit_unavailable", "The deletion could not be recorded, so nothing was deleted.")
    try:
        await db.run_in_thread(dictation.admin_discard, user_id, session_id)
    except dictation.SessionError as exc:
        return _refused(exc)
    return Response(status_code=204)


@_admin.get("/admin/api/voice/removed-members")
async def admin_removed_members_with_recordings(
    request: Request,
    principal: Principal = Depends(require_capability(Cap.WORKSPACE_CONTENT_READ)),
) -> dict:
    """Members removed from this workspace whose recordings are still stored:
    where a super admin finds the ids the routes above take. Counts and
    names only; audited like every list read of other members' content."""
    from .authn.admin_api import READ_AUDIT_COALESCE_S, removed_members_with_recordings
    from .authn.rbac import Role

    if Role(principal.role) is not Role.SUPER_ADMIN:
        raise HTTPException(status_code=404, detail="Not found.")
    members = await db.run_in_thread(removed_members_with_recordings, principal.workspace_id)
    from .authn.principal import audit

    await db.run_in_thread(
        lambda: audit(
            principal, request, "admin_listed_removed_voice_members",
            meta={"count": len(members)}, coalesce_seconds=READ_AUDIT_COALESCE_S,
        )
    )
    return {"members": members}


router.routes.extend(_admin.routes)
