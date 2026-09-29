"""Live dictation: the words while they are spoken (2026-09-29).

WHAT THIS IS. A recording session (app/dictation.py) stores the browser's Opus
parts and whisper transcribes them in windows cut at pauses, so its preview
arrives five seconds or more after the words. Beside it the browser now also
streams 16 kHz PCM over a WebSocket to this gateway, which relays it to a CPU
streaming engine on the WORKER Spark (compose/stt-stream/server.py) and relays
back a PARTIAL hypothesis a few hundred milliseconds after the words and a
FINAL one per utterance once the speaker pauses:

    browser --frontend/server-ws-relay.cjs--> WS /audio/sessions/{id}/live
    this gateway --ws://<worker>/v1/stream, Bearer VOICE_LIVE_ENGINE_TOKEN--> engine

The stored recording and whisper's transcript stay the record. Anything that
fails here costs the preview and nothing else, which is why an engine that is
down or full is refused rather than queued for, and why no audio is kept.

THE PROTOCOLS are the real-time dictation build spec's section 2 (browser,
subprotocol techsara.voice.v1) and section 3 (engine); each message is
described where it is handled below.

WHY THE HANDLER DOES ITS OWN SECURITY. A WebSocket scope passes through every
middleware this app has (the cross-site write check, CORS, the body cap)
untouched, the Request-typed auth dependencies cannot be injected into a
WebSocket route, and the orchestrator is reachable on the LAN, so nothing the
relay checks is a boundary. Before a byte of audio is taken, in this order:

  1. Origin, before accept. A WebSocket answer is readable cross-origin and
     SameSite=Lax lets a sibling site's page open one with the victim's
     cookie, so a page elsewhere could otherwise read somebody's dictation.
     A bare 403, with no detail (a close before accept).
  2. the sign-in session, 3. VOICE_INPUT, 4. the deployment's switches,
  5. the recording is the caller's and still recording (one 404 for a
     stranger's and an unknown id, like every session route),
  6. VOICE_LIVE_CONNECTS_PER_MIN, 7. VOICE_LIVE_MAX_STREAMS, with one stream
     per recording: a new connection supersedes the old one (4409).

Every refusal after the first ACCEPTS, sends one `error` message and closes
with the code, because a browser never shows a handshake's status to script.

AND KEEPS DOING IT. A socket resolves its sign-in once and nothing pushes a
revocation, so every VOICE_LIVE_REVALIDATE_S the stream resolves its cookie
again past the per-connection cache and re-reads the feature and the
recording's status: signing out, a deactivation, a removal or voice being
turned off ends the stream within that interval. Frames are bounded in size
and in rate (never more audio than wall time plus the resume allowance, and
never more messages than that time holds in frames, twice over), and a socket
that sends no audio for VOICE_LIVE_IDLE_S is closed.

WHAT IS NEVER LOGGED: audio, transcript text, the cookie, the engine token.
One line per stream close carries counts and durations.
"""
from __future__ import annotations

import asyncio
import bisect
import collections
import dataclasses
import functools
import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from fastapi import WebSocket

from . import db, dictation, metrics
from .authn import features as feature_access
from .authn import principal as principal_module
from .authn import proxy_trust
from .authn import sessions as auth_sessions
from .config import settings

log = logging.getLogger(__name__)

#: The only rate and frame the browser's tap produces (public/voice/
#: pcm-capture-worklet.js): 640 samples a message.
SAMPLE_RATE = 16000
FRAME_MS = 40
SUBPROTOCOL = "techsara.voice.v1"
#: Appended to a VOICE_LIVE_ENGINE_URLS entry that names no path.
ENGINE_PATH = "/v1/stream"

#: The start message must arrive this soon after accept.
START_TIMEOUT_S = 10.0
#: After a flush, how long the engine has to send its last final and `done`.
#: The browser gets its `done` when this runs out, whatever the engine says.
FLUSH_WAIT_S = 5.0
#: The longest text one partial or final may carry.
MAX_TEXT_CHARS = 4000
#: A text message longer than this is no message of this protocol.
MAX_TEXT_MESSAGE = 16384
#: client_stats: at most STATS_MAX numbers per list are read, each clamped to
#: [0, STATS_CLAMP_MS] because the browser's word is untrusted. The browser
#: sends one every 5 s; one arriving sooner than STATS_EVERY_S after the last
#: one read is ignored (the margin is network jitter).
STATS_MAX = 64
STATS_CLAMP_MS = 60_000
STATS_EVERY_S = 4.0
#: The arrival ceiling's head start on top of VOICE_LIVE_RESUME_MAX_S.
ARRIVAL_SLACK_S = 5.0
#: The message ceiling beside it. Counting samples never bounded MESSAGES:
#: 2-byte frames came through at 160,000 a second until their samples met
#: the ceiling 6.5 s later, and pings, which carry none, without end. Each
#: costs the one event loop every stream and chat in this process shares
#: about what a 40 ms frame costs (measured 2026-09-29 under uvicorn: either
#: flood took a whole core and slowed another stream's pong from 0.4 to 40 ms).
#: So every message after the start, audio or text, counts against that same
#: time in frames of the start's frame_ms (FRAME_MS unless it names a shorter
#: one, and never shorter than MIN_FRAME_MS) times MESSAGES_FACTOR, which
#: leaves room for the short frames a browser does send (a replay that
#: resumes mid-frame ends on one, Stop sends one) and for its pings and
#: client_stats. With the defaults: 3,250 at the start, 50 a second after.
MIN_FRAME_MS = 10
MESSAGES_FACTOR = 2
#: How much arrival history the latency bookkeeping keeps: when each frame
#: came in, so an event can be timed from the moment its audio arrived.
ARRIVALS_KEEP_S = 60.0
ARRIVALS_MAX = 4096
#: Received audio reaches its counter this often, not once per 40 ms frame.
AUDIO_COUNT_EVERY_S = 5.0
#: An engine that fails to open a stream is stood down this long, doubling
#: with each consecutive failure up to the ceiling (asr.RoutedProvider's).
ENGINE_COOLDOWN_S = 20.0
ENGINE_COOLDOWN_MAX_S = 600.0
#: Closing an engine socket never waits longer than this.
ENGINE_CLOSE_S = 2.0

_DEFAULT_PORTS = {"http": 80, "https": 443}


# --------------------------------------------------------- how it ends --


@dataclass(frozen=True)
class _End:
    """How a stream, or a refused handshake, ends."""

    #: The close code; None when the browser is already gone.
    close: Optional[int]
    #: voice_stream_sessions_total{outcome}, for a stream that was admitted.
    outcome: str
    #: The `error` message's code; empty sends no error message.
    code: str = ""
    message: str = ""
    retryable: bool = False
    #: Send {"type":"done"} before closing: a flush that completed.
    done: bool = False
    #: voice_stream_rejections_total{reason} and voice_stream_errors_total{reason}.
    rejection: str = ""
    error: str = ""


def _refusal(close: int, code: str, message: str, *, rejection: str, retryable: bool = False) -> _End:
    return _End(close=close, outcome="rejected", code=code, message=message, retryable=retryable, rejection=rejection)


def _protocol(message: str) -> _End:
    return _refusal(4400, "protocol", message, rejection="protocol")


_SIGNED_OUT = _refusal(4401, "signed_out", "Sign in again to use live dictation.", rejection="signed_out")
_VOICE_OFF = _refusal(
    4403, "voice_off", "Voice input is turned off for your account. Ask an administrator.", rejection="voice_off"
)
_UNAVAILABLE = _refusal(
    4404, "live_unavailable", "Live dictation isn't available on this server.", rejection="unavailable"
)
# dictation._not_found's wording: one answer for unknown and someone else's.
_NOT_FOUND = _refusal(4404, "not_found", "This recording is no longer on the server.", rejection="not_found")
_CLOSED = _refusal(4404, "session_closed", "This recording has stopped.", rejection="not_found")
_RATE_LIMITED = _refusal(
    4429, "rate_limited", "Too many live connections in a minute.", rejection="rate_limited", retryable=True
)
_CAPACITY = _refusal(4429, "capacity", "Live dictation is busy. It will try again.", rejection="capacity", retryable=True)
_TOO_FAST = _refusal(
    4429, "rate_limited", "Audio is arriving faster than it can be spoken.", rejection="rate_limited", retryable=True
)
_UNSUPPORTED_LANGUAGE = _refusal(
    4400, "unsupported_language", "Live dictation does not transcribe that language.", rejection="protocol"
)
_SUPERSEDED = _End(close=4409, outcome="superseded", code="superseded",
                   message="Another connection took over this recording.")
_IDLE = _End(close=4408, outcome="idle", code="idle", message="No audio arrived for too long.", retryable=True)
_ENGINE_DOWN = _End(
    close=4503, outcome="engine_unavailable", code="engine_unavailable",
    message="The live transcription engine isn't answering.", retryable=True, error="engine_unavailable",
)
_INTERNAL = _End(close=4500, outcome="error", code="internal", message="Live dictation failed on the server.",
                 retryable=True, error="internal")
_GONE = _End(close=None, outcome="disconnected")
_COMPLETED = _End(close=1000, outcome="completed", done=True)


class _Stop(Exception):
    """Raised inside a stream's tasks to end it; carries how."""

    def __init__(self, end: _End) -> None:
        super().__init__(end.outcome)
        self.end = end


def _client_end(code: Any) -> _End:
    """The browser closed the socket (a close frame: 1000 for Stop's cancel,
    1001 for a closing tab) or vanished (1005/1006: no close frame)."""
    try:
        number = int(code)
    except (TypeError, ValueError):
        number = 1005
    closed = number in (1000, 1001) or 4000 <= number <= 4999
    return _End(close=None, outcome="client_closed" if closed else "disconnected")


# ------------------------------------------------------------- metrics --

_HELP = {
    "voice_stream_sessions_active": "Live dictation streams open in this process.",
    "voice_stream_sessions_started_total": "Live dictation streams admitted (every handshake check passed).",
    "voice_stream_sessions_total": "Live dictation streams ended, by how they ended.",
    "voice_stream_audio_received_seconds_total": (
        "Seconds of 16 kHz PCM received from browsers; the denominator of the engine's real-time factor."
    ),
    "voice_stream_utterances_total": "Final utterances relayed to browsers.",
    "voice_stream_rejections_total": "Live dictation streams refused, at the handshake or later, by reason.",
    "voice_stream_errors_total": "Live dictation failures on the server's side, by reason; engine attempts included.",
    "voice_stream_first_partial_seconds": (
        "Arrival of an utterance's first sample to its first partial written to the browser."
    ),
    "voice_stream_final_seconds": "Arrival of an utterance's last sample to its final written to the browser.",
    "voice_stream_event_lag_seconds": (
        "Arrival of the newest sample a partial or final covers to that event written to the browser."
    ),
    "voice_stream_client_e2e_seconds": (
        "Capture to render as the browser measured it (client_stats; untrusted, clamped to 60 s)."
    ),
}

#: Every counter series, registered at zero at each scrape so the first event
#: after a restart is visible to increase(): (name, label, closed values).
_ZEROED: Tuple[Tuple[str, str, Any], ...] = (
    ("voice_stream_sessions_started_total", "", ()),
    ("voice_stream_sessions_total", "outcome", metrics.VOICE_STREAM_OUTCOMES),
    ("voice_stream_audio_received_seconds_total", "", ()),
    ("voice_stream_utterances_total", "", ()),
    ("voice_stream_rejections_total", "reason", metrics.VOICE_STREAM_REJECTION_REASONS),
    ("voice_stream_errors_total", "reason", metrics.VOICE_STREAM_ERROR_REASONS),
)


def _count(name: str, amount: float = 1.0, **labels: str) -> None:
    metrics.inc_by(name, amount, _HELP[name], **labels)


def _observe(name: str, seconds: float, **labels: str) -> None:
    metrics.observe(name, max(0.0, seconds), _HELP[name], **labels)


def _collect() -> None:
    """At every scrape: the open-stream gauge, read from the registry rather
    than set on transitions (a gauge keeps its last value for ever), and every
    counter series at zero until its first event."""
    metrics.set_gauge("voice_stream_sessions_active", REGISTRY.active(), _HELP["voice_stream_sessions_active"])
    for name, label, values in _ZEROED:
        if label:
            for value in sorted(values):
                metrics.inc_by(name, 0.0, _HELP[name], **{label: value})
        else:
            metrics.inc_by(name, 0.0, _HELP[name])


# ------------------------------------------------------------- helpers --


def _dumps(message: Dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def _error_message(end: _End) -> str:
    return _dumps({"type": "error", "code": end.code, "message": end.message, "retryable": end.retryable})


def _is_count(value: Any) -> bool:
    """A non-negative integer, and not a bool (json has no ints that are)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _json_object(text: Any) -> Optional[Dict[str, Any]]:
    """A JSON object with a string `type`, or None. RecursionError is a
    parse failure too: sixteen kilobytes of "[" nest past the interpreter's
    limit."""
    if not isinstance(text, str):
        return None
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        return None
    return value


def configured() -> bool:
    """Whether this deployment offers live dictation at all: the recording
    sessions it rides on, and an engine address (scripts/stt-stream.sh writes
    VOICE_LIVE_ENGINE_URLS). dictation.config() tells the browser the same."""
    return bool(
        settings.asr_enabled
        and settings.voice_sessions_enabled
        and settings.voice_live_enabled
        and settings.voice_live_engine_urls
    )


def health() -> Dict[str, Any]:
    """/audio/health's live block. Engines are numbered in configuration
    order and never named: an address is no business of a health answer."""
    return {"configured": configured(), "active": REGISTRY.active(), "engines": ENGINES.stats()}


# --------------------------------------------------------------- Origin --


def _origin_parts(origin: str) -> Optional[Tuple[str, str, int]]:
    """(scheme, hostname, port) of a browser Origin; None for anything but
    http(s), which is how `null` and extension origins are refused."""
    try:
        parts = urlsplit(origin)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    return parts.scheme, parts.hostname, port if port is not None else _DEFAULT_PORTS[parts.scheme]


def _host_matches(host: str, scheme: str, hostname: str, port: int) -> bool:
    """Is `host` (a Host or X-Forwarded-Host value) the Origin's own host?
    Hostname and effective port: a Host without a port stands for the
    Origin scheme's default one, as frontend/server-ws-relay.cjs decides it."""
    try:
        parts = urlsplit("//" + host.strip())
        host_port = parts.port
    except ValueError:
        return False
    if not parts.hostname or parts.path or parts.query or parts.fragment or "@" in parts.netloc:
        return False
    effective = host_port if host_port is not None else _DEFAULT_PORTS[scheme]
    return parts.hostname == hostname and effective == port


async def _origin_allowed(websocket: WebSocket) -> bool:
    """Step 1: the page that opened this socket is ours.

    Allowed when the Origin is one of CORS_ALLOW_ORIGINS (the browser origins
    this app already trusts with credentials), or is the host the browser
    connected to. Behind the frontend's relay that host arrives as
    X-Forwarded-Host, which is believed only from a peer that is one of our
    own proxies (authn/proxy_trust, the rule sign-in uses); from anyone else
    the Host header decides. A missing Origin is refused: every browser sends
    one on a WebSocket handshake, and a client that does not is not a page
    this check could protect anyway."""
    origin = (websocket.headers.get("origin") or "").strip()
    if not origin:
        return False
    if origin in settings.cors_allow_origins:
        return True
    parsed = _origin_parts(origin)
    if parsed is None:
        return False
    host = websocket.headers.get("host") or ""
    forwarded = websocket.headers.get("x-forwarded-host")
    if forwarded is not None:
        # In a thread, allowed to wait a moment: the first handshake after a
        # start may be what triggers the frontend's name lookup.
        source = await asyncio.to_thread(
            auth_sessions.client_origin, websocket, resolve_wait=proxy_trust.RESOLVE_WAIT_S
        )
        if source.via_trusted_proxy:
            host = forwarded
    return _host_matches(host, *parsed)


async def _deny(websocket: WebSocket) -> None:
    """A bare 403 before accept. A close before accept IS that in ASGI, and
    uvicorn answers it with a 403 and no body. Not send_denial_response:
    uvicorn's websockets-sansio never marks that handshake complete and logs
    "ASGI callable returned without completing handshake" at ERROR for every
    refusal (0.46.0 and 0.54.0, measured 2026-09-29), which would let anyone
    fill the error log with foreign Origins."""
    try:
        await websocket.close(code=1008)
    except Exception:  # noqa: BLE001 - the peer may already be gone
        pass


# ------------------------------------------------------------ the gate --


async def _admit(websocket: WebSocket, session_id: str) -> Any:
    """Steps 2 to 6: the Principal, or the _End that refuses the socket."""
    try:
        # Through the module attribute, so the suite's identity shim applies.
        principal = await db.run_in_thread(principal_module.resolve_principal_sync, websocket)
    except Exception:  # noqa: BLE001 - no middleware turns this into a 500 for a socket
        log.exception("live dictation: the sign-in session could not be resolved")
        return _INTERNAL
    if principal is None:
        return _SIGNED_OUT
    if not feature_access.allowed(principal.features, feature_access.Feature.VOICE_INPUT):
        return _VOICE_OFF
    if not configured():
        return _UNAVAILABLE
    try:
        row = await db.run_in_thread(dictation._owned_row, session_id, int(principal.user_id))
    except dictation.SessionError:
        return _NOT_FOUND
    except Exception:  # noqa: BLE001
        log.exception("live dictation: the recording could not be read")
        return _INTERNAL
    if row["status"] != dictation.STATUS_RECORDING:
        return _CLOSED
    if not dictation.live_connect_ok(int(principal.user_id)):
        return _RATE_LIMITED
    return principal


async def _refuse(websocket: WebSocket, subprotocol: Optional[str], end: _End) -> None:
    if end.rejection:
        _count("voice_stream_rejections_total", reason=end.rejection)
    if end.error:
        _count("voice_stream_errors_total", reason=end.error)
    try:
        await websocket.accept(subprotocol=subprotocol)
        await websocket.send_text(_error_message(end))
        await websocket.close(code=end.close or 1000)
    except Exception:  # noqa: BLE001 - the peer may already be gone
        pass


async def serve(websocket: WebSocket, session_id: str) -> None:
    """The /audio/sessions/{id}/live route: the handshake, then the stream."""
    if not await _origin_allowed(websocket):
        _count("voice_stream_rejections_total", reason="origin")
        await _deny(websocket)
        return
    offered = websocket.scope.get("subprotocols") or ()
    subprotocol = SUBPROTOCOL if SUBPROTOCOL in offered else None
    admitted = await _admit(websocket, session_id)
    if isinstance(admitted, _End):
        await _refuse(websocket, subprotocol, admitted)
        return
    stream = _Stream(websocket, session_id, int(admitted.user_id))
    refused = REGISTRY.admit(stream)
    if refused is not None:
        await _refuse(websocket, subprotocol, refused)
        return
    try:
        await stream.run(subprotocol)
    finally:
        REGISTRY.release(stream)


# ------------------------------------------------------------ registry --


class _Registry:
    """The streams open in this process, at most one per recording.

    Behind a thread lock, not only the event loop: Starlette's TestClient
    runs each socket on its own loop, and a superseded stream is told so
    through its own loop (`_Stream.supersede`)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._streams: Dict[str, "_Stream"] = {}

    def admit(self, stream: "_Stream") -> Optional[_End]:
        """Step 7: room for one more stream, a new connection for a
        recording replacing its old one (which is then closed 4409)."""
        with self._lock:
            old = self._streams.get(stream.session_id)
            others = len(self._streams) - (1 if old is not None else 0)
            if others >= settings.voice_live_max_streams:
                return _CAPACITY
            self._streams[stream.session_id] = stream
        if old is not None:
            old.supersede()
        return None

    def release(self, stream: "_Stream") -> None:
        with self._lock:
            if self._streams.get(stream.session_id) is stream:
                del self._streams[stream.session_id]

    def active(self) -> int:
        with self._lock:
            return len(self._streams)

    def reset(self) -> None:
        with self._lock:
            self._streams.clear()


# ------------------------------------------------------------- engines --


async def connect_engine(url: str, token: str, timeout_s: float) -> Any:
    """Open one stream socket to an engine: THE SEAM the suite replaces with
    an in-process fake. What it returns has async send(str | bytes),
    recv() -> str | bytes (raising once the engine is gone) and close().

    `websockets` arrives with uvicorn[standard]. No permessage-deflate (PCM
    does not compress; it would spend CPU the decoders need), no system
    proxy (the engine is on the LAN), no User-Agent to advertise versions."""
    from websockets.asyncio.client import connect

    options: Dict[str, Any] = {
        "additional_headers": {"Authorization": f"Bearer {token}"} if token else None,
        "open_timeout": timeout_s,
        "close_timeout": ENGINE_CLOSE_S,
        "compression": None,
        "user_agent_header": None,
    }
    if _takes_proxy_option():
        options["proxy"] = None
    return await connect(url, **options)


@functools.lru_cache(maxsize=1)
def _takes_proxy_option() -> bool:
    """websockets 15 added `proxy` (default: the environment's); older
    releases pass unknown options on to the socket and fail."""
    import inspect

    from websockets.asyncio.client import connect

    return "proxy" in inspect.signature(connect).parameters


def _stream_url(base: str) -> str:
    parts = urlsplit(base)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    path = parts.path if parts.path not in ("", "/") else ENGINE_PATH
    return urlunsplit((scheme, parts.netloc, path, parts.query, ""))


async def _close_quietly(conn: Any) -> None:
    if conn is None:
        return
    try:
        async with asyncio.timeout(ENGINE_CLOSE_S):
            await conn.close()
    except Exception:  # noqa: BLE001 - a socket that will not close is dropped
        pass


#: Engine sockets being closed on behalf of a cancelled handler: a task
#: nobody references can be collected before it runs.
_closing: set = set()


def _close_later(conn: Any) -> None:
    """Close an engine socket from a handler that is being cancelled, where
    nothing more may be awaited: the engine then sees the stream end now,
    not at its idle timeout."""
    if conn is None:
        return
    try:
        task = asyncio.get_running_loop().create_task(_close_quietly(conn))
    except RuntimeError:
        return
    _closing.add(task)
    task.add_done_callback(_closing.discard)


@dataclass
class _Link:
    index: int
    conn: Any


class _EngineState:
    __slots__ = ("active", "down_until", "failures")

    def __init__(self) -> None:
        self.active = 0
        self.down_until = 0.0
        self.failures = 0


class _Failed(Exception):
    """This engine could not open the stream; try the next."""


class _Refused(Exception):
    """This engine is healthy and said no (full, or not that language)."""

    def __init__(self, end: _End) -> None:
        super().__init__(end.code)
        self.end = end


_ENGINE_FULL = _CAPACITY
_ENGINE_REFUSAL_CODES = frozenset({"capacity", "unsupported_language", "engine_unavailable", "internal",
                                   "protocol", "rate_limited"})


class _Engines:
    """VOICE_LIVE_ENGINE_URLS, least active healthy engine first.

    Like asr.RoutedProvider: an engine that fails to open a stream is stood
    down for ENGINE_COOLDOWN_S, doubling with consecutive failures to
    ENGINE_COOLDOWN_MAX_S, and rejoins by itself. Unlike it, an engine
    standing down is not tried: a browser retries with backoff, and a dead
    engine should cost it nothing but that. A FULL engine is not failing, so
    it is not stood down."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._urls: Tuple[str, ...] = ()
        self._state: List[_EngineState] = []

    def _current(self) -> List[_EngineState]:
        # Rebuilt when the configured list changes (a test, never production).
        urls = tuple(settings.voice_live_engine_urls)
        if urls != self._urls:
            self._urls = urls
            self._state = [_EngineState() for _ in urls]
        return self._state

    def _candidates(self) -> List[Tuple[int, str]]:
        now = time.monotonic()
        with self._lock:
            state = self._current()
            healthy = [i for i, engine in enumerate(state) if engine.down_until <= now]
            healthy.sort(key=lambda i: state[i].active)
            return [(i, self._urls[i]) for i in healthy]

    def _claim(self, index: int) -> None:
        with self._lock:
            state = self._current()
            if index < len(state):
                state[index].active += 1

    def release(self, index: int) -> None:
        with self._lock:
            state = self._current()
            if index < len(state):
                state[index].active = max(0, state[index].active - 1)

    def _answered(self, index: int) -> None:
        with self._lock:
            state = self._current()
            if index < len(state):
                if state[index].failures:
                    log.info("live dictation engine %d answered again after %d consecutive failures",
                             index, state[index].failures)
                state[index].failures = 0
                state[index].down_until = 0.0

    def _stand_down(self, index: int, why: str) -> None:
        with self._lock:
            state = self._current()
            if index >= len(state):
                return
            engine = state[index]
            engine.failures += 1
            cooldown = min(ENGINE_COOLDOWN_S * (2 ** (engine.failures - 1)), ENGINE_COOLDOWN_MAX_S)
            engine.down_until = time.monotonic() + cooldown
            failures = engine.failures
        log.warning(
            "live dictation engine %d is unavailable (%s); standing it down for %.0f s "
            "(consecutive failures: %d)", index, why, cooldown, failures,
        )

    async def open(self, stream: "_Stream") -> _Link:
        """A stream on the first engine that takes it, or _Stop: 4503 when
        none is healthy or answering, 4429 when the answering ones are full,
        4400 when none transcribes the language."""
        candidates = self._candidates()
        if not candidates:
            raise _Stop(_ENGINE_DOWN)
        refusal: Optional[_End] = None
        for index, url in candidates:
            self._claim(index)
            try:
                conn = await self._attempt(index, url, stream)
            except _Refused as refused:
                self.release(index)
                if refusal is None or refused.end is _ENGINE_FULL:
                    refusal = refused.end
                continue
            except _Failed:
                self.release(index)
                continue
            except BaseException:
                self.release(index)
                raise
            self._answered(index)
            return _Link(index, conn)
        # Each failed attempt counted its own reason in voice_stream_errors_total.
        raise _Stop(refusal or dataclasses.replace(_ENGINE_DOWN, error=""))

    async def _attempt(self, index: int, url: str, stream: "_Stream") -> Any:
        """Connect, send the engine's start and wait for its `ready`, all
        inside VOICE_LIVE_ENGINE_CONNECT_S."""
        conn = None
        start = {
            "type": "start", "sample_rate": SAMPLE_RATE, "encoding": "pcm_s16le",
            "first_sample": stream.first_sample, "first_u": stream.next_u,
            "mode": "dictation", "language": stream.language,
        }
        budget = settings.voice_live_engine_connect_s
        try:
            async with asyncio.timeout(budget):
                conn = await connect_engine(_stream_url(url), settings.voice_live_engine_token, budget)
                await conn.send(_dumps(start))
                reply = _json_object(await conn.recv())
        except TimeoutError:
            await _close_quietly(conn)
            _count("voice_stream_errors_total", reason="engine_timeout")
            self._stand_down(index, f"no ready within {budget:g} s")
            raise _Failed() from None
        except Exception as exc:  # noqa: BLE001 - refused, unreachable, closed
            await _close_quietly(conn)
            _count("voice_stream_errors_total", reason="engine_unavailable")
            self._stand_down(index, f"{type(exc).__name__}: {str(exc)[:200]}")
            raise _Failed() from None
        except BaseException:
            _close_later(conn)
            raise
        kind = reply.get("type") if reply else None
        if kind == "ready":
            return conn
        await _close_quietly(conn)
        if kind == "error":
            code = reply.get("code")
            if code == "capacity":
                raise _Refused(_ENGINE_FULL)
            if code == "unsupported_language":
                raise _Refused(_UNSUPPORTED_LANGUAGE)
            _count("voice_stream_errors_total", reason="engine_unavailable")
            self._stand_down(index, f"refused the stream ({code if code in _ENGINE_REFUSAL_CODES else 'other'})")
            raise _Failed()
        _count("voice_stream_errors_total", reason="engine_protocol")
        self._stand_down(index, "answered the start with something other than ready")
        raise _Failed()

    def stats(self) -> List[Dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            return [
                {
                    "index": i,
                    "healthy": engine.down_until <= now,
                    "active": engine.active,
                    "stood_down_s": max(0, round(engine.down_until - now)),
                }
                for i, engine in enumerate(self._current())
            ]

    def reset(self) -> None:
        with self._lock:
            self._urls = ()
            self._state = []


# -------------------------------------------------------------- stream --


class _Stream:
    """One admitted socket: its start, its engine, its limits and timers.

    Three tasks run once the start is read: the browser's messages, the
    engine's events, and the timers (idle, flush, re-validation). Any of them
    ends the stream by raising _Stop; `run` then stops the others, tells the
    browser how it ended and closes both sockets. Only `_send` writes to the
    browser while the tasks run."""

    def __init__(self, websocket: WebSocket, session_id: str, user_id: int) -> None:
        self.ws = websocket
        self.session_id = session_id
        self.user_id = user_id
        self.loop = asyncio.get_running_loop()
        self.opened = time.monotonic()
        self._stopped: "asyncio.Future[_End]" = self.loop.create_future()
        self._wake = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._tasks: List["asyncio.Task[None]"] = []
        self.link: Optional[_Link] = None
        # The start message.
        self.first_sample = 0
        self.next_u = 0
        self.language = "auto"
        self.started = self.opened
        # State.
        self.streaming = False
        self.flushing = False
        self.flush_deadline = 0.0
        self.last_audio = self.opened
        self.revalidate_at = self.opened + settings.voice_live_revalidate_s
        self.samples = 0
        # The message ceiling's count, and its rate (the start may raise it).
        self.messages = 0
        self.message_rate = MESSAGES_FACTOR * 1000.0 / FRAME_MS
        self.utterances = 0
        self.client_code: Optional[int] = None
        self._uncounted = 0
        self._counted_at = self.opened
        self._arrival_ends: Deque[int] = collections.deque()
        self._arrival_times: Deque[float] = collections.deque()
        self._arrival_floor = 0
        self._max_partial_u = -1
        self._stats_at = -math.inf

    # -- lifecycle -------------------------------------------------------

    def supersede(self) -> None:
        """Another connection took this recording over. Called from that
        connection's handler, which may be on another thread's loop."""
        try:
            self.loop.call_soon_threadsafe(self._stop, _SUPERSEDED)
        except RuntimeError:
            pass  # this stream's loop is gone, and the stream with it

    def _stop(self, end: _End) -> None:
        if not self._stopped.done():
            self._stopped.set_result(end)

    def _spawn(self, coro: Any) -> None:
        self._tasks.append(self.loop.create_task(self._guard(coro)))

    async def _guard(self, coro: Any) -> None:
        try:
            await coro
        except _Stop as stop:
            self._stop(stop.end)
        except Exception:  # noqa: BLE001 - one stream's bug must not reach the others
            log.exception("live dictation stream for session %s failed", self.session_id)
            self._stop(_INTERNAL)

    async def run(self, subprotocol: Optional[str]) -> None:
        _count("voice_stream_sessions_started_total")
        end: Optional[_End] = None
        try:
            try:
                await self.ws.accept(subprotocol=subprotocol)
            except Exception:  # noqa: BLE001 - gone during the handshake
                end = _GONE
                return
            self._spawn(self._pipeline())
            self._spawn(self._timers())
            end = await self._stopped
            for task in self._tasks:
                task.cancel()
            # asyncio.wait, not gather: cancelled while gathering tasks it had
            # cancelled itself, this handler would raise THEIR CancelledError
            # instead of its own, which a canceller (anyio's cancel scope)
            # does not recognise as the one it sent.
            await asyncio.wait(self._tasks)
            await self._deliver(end)
        finally:
            for task in self._tasks:
                task.cancel()
            # Before anything that awaits: a cancelled handler (a shutdown
            # past its grace, a test client leaving) still counts its stream.
            self._account(end or _GONE)
            await self._close_engine()

    async def _deliver(self, end: _End) -> None:
        if end.close is None:
            return
        try:
            if end.done:
                await self.ws.send_text(_dumps({"type": "done"}))
            if end.code:
                await self.ws.send_text(_error_message(end))
            await self.ws.close(code=end.close)
        except Exception:  # noqa: BLE001 - the peer may already be gone
            pass

    async def _close_engine(self) -> None:
        link, self.link = self.link, None
        if link is None:
            return
        ENGINES.release(link.index)
        try:
            await _close_quietly(link.conn)
        except asyncio.CancelledError:
            _close_later(link.conn)
            raise

    def _account(self, end: _End) -> None:
        now = time.monotonic()
        self._count_audio(now)
        _count("voice_stream_sessions_total", outcome=end.outcome)
        if end.rejection:
            _count("voice_stream_rejections_total", reason=end.rejection)
        if end.error:
            _count("voice_stream_errors_total", reason=end.error)
        log.info("live dictation stream closed %s", _dumps({
            "session": self.session_id,
            "outcome": end.outcome,
            "code": end.close if end.close is not None else self.client_code,
            "duration_s": round(now - self.opened, 1),
            "audio_s": round(self.samples / SAMPLE_RATE, 1),
            "utterances": self.utterances,
            "reconnect": bool(self.first_sample or self.next_u),
        }))

    # -- the start, the engine, `ready` -----------------------------------

    async def _pipeline(self) -> None:
        await self._read_start()
        self.link = await ENGINES.open(self)
        await self._send({
            "type": "ready", "v": 1, "sample_rate": SAMPLE_RATE, "frame_ms": FRAME_MS,
            "max_frame_bytes": settings.voice_live_max_frame_bytes,
            "resume_from_sample": self.first_sample, "next_u": self.next_u,
        })
        self.streaming = True
        self.last_audio = time.monotonic()
        self._wake.set()
        self._spawn(self._browser())
        self._spawn(self._engine())

    async def _read_start(self) -> None:
        """{"type":"start","v":1,"encoding":"pcm_s16le","sample_rate":16000,
        "channels":1,"frame_ms":40,"source":"mic","resume_from_sample":N,
        "next_u":K,"clock_offset_ms":C,"language":L}, first, within
        START_TIMEOUT_S. resume_from_sample is the sample index of the first
        PCM sample that follows (a reconnect replays from its ring buffer),
        next_u the next utterance number; the engine continues both."""
        try:
            async with asyncio.timeout(START_TIMEOUT_S):
                received = await self.ws.receive()
        except TimeoutError:
            raise _Stop(_protocol("No start message arrived in time.")) from None
        message = _json_object(self._text(received))
        if message is None or message["type"] != "start":
            raise _Stop(_protocol("The first message must be start."))
        if message.get("v", 1) != 1:
            raise _Stop(_protocol("Unsupported protocol version."))
        rate = message.get("sample_rate")
        if message.get("encoding") != "pcm_s16le" or isinstance(rate, bool) or rate != SAMPLE_RATE:
            raise _Stop(_protocol("Audio must be pcm_s16le at 16000 Hz."))
        channels = message.get("channels", 1)
        if isinstance(channels, bool) or channels != 1:
            raise _Stop(_protocol("Audio must be mono."))
        first_sample = message.get("resume_from_sample", 0)
        next_u = message.get("next_u", 0)
        if not (_is_count(first_sample) and first_sample < 1 << 40):
            raise _Stop(_protocol("resume_from_sample must be a non-negative integer."))
        if not (_is_count(next_u) and next_u < 1 << 30):
            raise _Stop(_protocol("next_u must be a non-negative integer."))
        offset = message.get("clock_offset_ms", 0)
        if isinstance(offset, bool) or not isinstance(offset, (int, float)) or (
            isinstance(offset, float) and not math.isfinite(offset)
        ):
            raise _Stop(_protocol("clock_offset_ms must be a number."))
        frame_ms = message.get("frame_ms", FRAME_MS)
        if isinstance(frame_ms, bool) or not isinstance(frame_ms, (int, float)) or (
            isinstance(frame_ms, float) and not math.isfinite(frame_ms)
        ):
            raise _Stop(_protocol("frame_ms must be a number."))
        language = message.get("language")
        if language is None or language == "":
            language = "auto"
        # Passed to the engine exactly as sent, so only the allow-list's own
        # spellings are accepted (it is lowercase, entries of 16 characters
        # at most).
        if not isinstance(language, str) or language not in settings.voice_live_languages:
            raise _Stop(_UNSUPPORTED_LANGUAGE)
        self.first_sample, self.next_u, self.language = first_sample, next_u, language
        # Shorter frames buy more messages a second, down to MIN_FRAME_MS's
        # worth; a longer frame_ms buys no fewer than the browser's own.
        self.message_rate = MESSAGES_FACTOR * 1000.0 / min(max(frame_ms, MIN_FRAME_MS), FRAME_MS)
        self._arrival_floor = first_sample
        self.started = time.monotonic()

    # -- the browser's messages -------------------------------------------

    def _text(self, received: Dict[str, Any]) -> Optional[str]:
        """The text of a received message; _Stop when the browser left."""
        if received.get("type") == "websocket.disconnect":
            self.client_code = received.get("code")
            raise _Stop(_client_end(received.get("code")))
        return received.get("text")

    async def _browser(self) -> None:
        while True:
            received = await self.ws.receive()
            if received.get("type") == "websocket.receive":
                self._paced()
            data = received.get("bytes")
            if data is not None and received.get("type") == "websocket.receive":
                await self._audio(data)
                continue
            text = self._text(received)
            if text is None:
                raise _Stop(_protocol("Unreadable message."))
            await self._control(text)

    def _paced(self) -> None:
        """The message ceiling (MESSAGES_FACTOR), before a message is handled:
        what it costs this process is paid per message, whatever it holds."""
        self.messages += 1
        allowed_s = time.monotonic() - self.started + settings.voice_live_resume_max_s + ARRIVAL_SLACK_S
        if self.messages > allowed_s * self.message_rate:
            raise _Stop(_TOO_FAST)

    async def _audio(self, data: bytes) -> None:
        """One PCM frame: little-endian int16 mono at 16 kHz, 2 to
        VOICE_LIVE_MAX_FRAME_BYTES bytes, even. The arrival ceiling: never
        more samples than wall seconds since start plus the resume allowance
        plus ARRIVAL_SLACK_S, times 16,000 -- a reconnect may replay its ring
        buffer at once, a microphone cannot outrun the clock."""
        if self.flushing:
            raise _Stop(_protocol("Audio arrived after flush."))
        size = len(data)
        limit = settings.voice_live_max_frame_bytes
        if size > limit:
            raise _Stop(_refusal(4413, "frame_too_large", f"An audio frame may be at most {limit} bytes.",
                                 rejection="frame_too_large"))
        if size < 2 or size % 2:
            raise _Stop(_protocol("An audio frame is an even number of bytes of 16-bit PCM."))
        now = time.monotonic()
        count = size // 2
        self.samples += count
        ceiling = (now - self.started + settings.voice_live_resume_max_s + ARRIVAL_SLACK_S) * SAMPLE_RATE
        if self.samples > ceiling:
            raise _Stop(_TOO_FAST)
        self.last_audio = now
        self._arrived(self.first_sample + self.samples, now)
        self._uncounted += count
        if now - self._counted_at >= AUDIO_COUNT_EVERY_S:
            self._count_audio(now)
        try:
            await self.link.conn.send(data)
        except Exception:  # noqa: BLE001 - the engine went away
            raise _Stop(self._engine_gone()) from None

    async def _control(self, text: str) -> None:
        """ping -> pong; flush; client_stats. Anything else breaks the protocol."""
        if len(text) > MAX_TEXT_MESSAGE:
            raise _Stop(_protocol("That message is too long."))
        message = _json_object(text)
        kind = message["type"] if message else None
        if kind == "ping":
            stamp = message.get("t")
            if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or (
                isinstance(stamp, float) and not math.isfinite(stamp)
            ):
                raise _Stop(_protocol("A ping carries a number t."))
            await self._send({"type": "pong", "t": stamp})
        elif kind == "flush":
            await self._flush()
        elif kind == "client_stats":
            self._client_stats(message)
        elif kind == "start":
            raise _Stop(_protocol("start was already sent."))
        else:
            raise _Stop(_protocol("Unknown message type."))

    async def _flush(self) -> None:
        """Stop pressed: no more audio. The engine commits what is pending and
        says `done`; the browser gets `done` then a 1000 close, after at most
        FLUSH_WAIT_S either way."""
        if self.flushing:
            return
        self.flushing = True
        self.flush_deadline = time.monotonic() + FLUSH_WAIT_S
        self._wake.set()
        try:
            await self.link.conn.send(_dumps({"type": "flush"}))
        except Exception:  # noqa: BLE001
            raise _Stop(self._engine_gone()) from None

    def _client_stats(self, message: Dict[str, Any]) -> None:
        """{"type":"client_stats","partial_ms":[...],"final_ms":[...]}: the
        browser's own capture-to-render latencies. Telemetry, so a malformed
        report is ignored rather than refused."""
        now = time.monotonic()
        if now - self._stats_at < STATS_EVERY_S:
            return
        self._stats_at = now
        for key, event in (("partial_ms", "partial"), ("final_ms", "final")):
            values = message.get(key)
            if not isinstance(values, list):
                continue
            for value in values[:STATS_MAX]:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                if isinstance(value, float) and not math.isfinite(value):
                    continue
                ms = min(max(float(value), 0.0), float(STATS_CLAMP_MS))
                _observe("voice_stream_client_e2e_seconds", ms / 1000.0, event=event)

    # -- the engine's events ----------------------------------------------

    async def _engine(self) -> None:
        """partial / final / speech are relayed REBUILT from their known
        fields, so nothing else the engine says (its profile, its model, its
        timings) reaches a browser; done ends a flush; error ends the stream."""
        conn = self.link.conn
        while True:
            try:
                raw = await conn.recv()
            except Exception:  # noqa: BLE001 - closed, reset, ping timeout
                raise _Stop(self._engine_gone()) from None
            event = _json_object(raw)
            kind = event["type"] if event else None
            if kind in ("partial", "final"):
                words = _words(event)
                if words is None:
                    _count("voice_stream_errors_total", reason="engine_protocol")
                    continue
                await self._send(words)
                self._measure(words)
            elif kind == "speech":
                edge = _speech(event)
                if edge is None:
                    _count("voice_stream_errors_total", reason="engine_protocol")
                    continue
                await self._send(edge)
            elif kind == "done":
                raise _Stop(_COMPLETED if self.flushing else self._engine_gone())
            elif kind == "error":
                raise _Stop(_IDLE if event.get("code") == "idle" and not self.flushing else self._engine_gone())
            elif kind is None:
                _count("voice_stream_errors_total", reason="engine_protocol")
            # ready, pong, and whatever a newer engine adds: nothing to relay.

    def _engine_gone(self) -> _End:
        if self.flushing:
            # The words were committed as far as they go; the browser still
            # gets its orderly end.
            return _End(close=1000, outcome="completed", done=True, error="engine_unavailable")
        return _ENGINE_DOWN

    # -- timers -------------------------------------------------------------

    async def _timers(self) -> None:
        while True:
            self._wake.clear()
            now = time.monotonic()
            due = self._due()
            if due > now:
                try:
                    async with asyncio.timeout(due - now):
                        await self._wake.wait()
                except TimeoutError:
                    pass
                continue
            if self.flushing:
                raise _Stop(_End(close=1000, outcome="completed", done=True, error="engine_timeout"))
            if self.streaming and now >= self.last_audio + settings.voice_live_idle_s:
                raise _Stop(_IDLE)
            end = await self._revalidate()
            if end is not None:
                raise _Stop(end)
            self.revalidate_at = time.monotonic() + settings.voice_live_revalidate_s

    def _due(self) -> float:
        if self.flushing:
            return self.flush_deadline
        due = self.revalidate_at
        if self.streaming:
            due = min(due, self.last_audio + settings.voice_live_idle_s)
        return due

    async def _revalidate(self) -> Optional[_End]:
        try:
            return await db.run_in_thread(self._revalidate_sync)
        except Exception:  # noqa: BLE001 - unverifiable is not allowed
            log.exception("live dictation stream for session %s could not re-check its access", self.session_id)
            return _INTERNAL

    def _revalidate_sync(self) -> Optional[_End]:
        """The handshake's checks again, past the per-connection cache: the
        cookie resolved afresh (sessions.resolve + principal._build, through
        the same seam), the feature, and the recording still recording."""
        try:
            delattr(self.ws.state, principal_module._STATE_KEY)
        except (AttributeError, KeyError):
            pass
        principal = principal_module.resolve_principal_sync(self.ws)
        if principal is None or int(principal.user_id) != self.user_id:
            return _SIGNED_OUT
        if not feature_access.allowed(principal.features, feature_access.Feature.VOICE_INPUT):
            return _VOICE_OFF
        try:
            row = dictation._owned_row(self.session_id, self.user_id)
        except dictation.SessionError:
            return _NOT_FOUND
        if row["status"] != dictation.STATUS_RECORDING:
            return _CLOSED
        return None

    # -- writing to the browser, and what it cost --------------------------

    async def _send(self, message: Dict[str, Any]) -> None:
        text = _dumps(message)
        try:
            async with self._send_lock:
                await self.ws.send_text(text)
        except Exception:  # noqa: BLE001 - the browser went away
            raise _Stop(_GONE) from None

    def _arrived(self, end_sample: int, now: float) -> None:
        self._arrival_ends.append(end_sample)
        self._arrival_times.append(now)
        while self._arrival_times and (
            now - self._arrival_times[0] > ARRIVALS_KEEP_S or len(self._arrival_times) > ARRIVALS_MAX
        ):
            self._arrival_floor = self._arrival_ends.popleft()
            self._arrival_times.popleft()

    def _received_at(self, sample: int) -> Optional[float]:
        """When the frame carrying `sample` arrived; None when it is older
        than the history kept, or was never received on this connection."""
        if sample < self._arrival_floor:
            return None
        i = bisect.bisect_right(self._arrival_ends, sample)
        if i >= len(self._arrival_ends):
            return None
        return self._arrival_times[i]

    def _measure(self, words: Dict[str, Any]) -> None:
        """first partial: arrival of the utterance's first sample to its first
        partial written; final and event lag: arrival of the newest sample
        covered to the event written."""
        now = time.monotonic()
        newest = self._received_at(max(words["start_sample"], words["end_sample"] - 1))
        if words["type"] == "final":
            self.utterances += 1
            _count("voice_stream_utterances_total")
            if newest is not None:
                _observe("voice_stream_final_seconds", now - newest)
                _observe("voice_stream_event_lag_seconds", now - newest, event="final")
            return
        if newest is not None:
            _observe("voice_stream_event_lag_seconds", now - newest, event="partial")
        if words["u"] > self._max_partial_u:
            self._max_partial_u = words["u"]
            first = self._received_at(words["start_sample"])
            if first is not None:
                _observe("voice_stream_first_partial_seconds", now - first)

    def _count_audio(self, now: float) -> None:
        if self._uncounted:
            _count("voice_stream_audio_received_seconds_total", self._uncounted / SAMPLE_RATE)
            self._uncounted = 0
        self._counted_at = now


def _words(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """{"type":"partial"|"final","u","text","start_sample","end_sample"}: a
    partial is the WHOLE current hypothesis of utterance u (the browser
    replaces, never appends); a final commits it."""
    u, text = event.get("u"), event.get("text")
    start, end = event.get("start_sample"), event.get("end_sample")
    if not (_is_count(u) and _is_count(start) and _is_count(end)):
        return None
    if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
        return None
    return {"type": event["type"], "u": u, "text": text, "start_sample": start, "end_sample": end}


def _speech(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """{"type":"speech","active","sample"}: the engine's voice-activity edge."""
    active, sample = event.get("active"), event.get("sample")
    if not isinstance(active, bool) or not _is_count(sample):
        return None
    return {"type": "speech", "active": active, "sample": sample}


REGISTRY = _Registry()
ENGINES = _Engines()
metrics.register_collector(_collect)


def reset_for_tests() -> None:
    REGISTRY.reset()
    ENGINES.reset()
    _takes_proxy_option.cache_clear()
