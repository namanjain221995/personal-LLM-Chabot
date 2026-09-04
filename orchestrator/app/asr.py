"""Speech to text — the client for the local ASR engine.

WHAT THIS IS. One provider abstraction over an OpenAI-compatible audio
endpoint, plus the admission control that keeps a workspace's microphones from
becoming a queue on the main model's GPU. It is deliberately small: the engine
does the hard part, and everything here is about doing it safely, once, with a
number attached.

WHERE THE AUDIO GOES. Nowhere but the engine. The bytes arrive in a request,
are held in memory for the length of one call, and are dropped. Nothing is
written to disk, nothing reaches the database, and the transcript is returned
to the browser as a DRAFT — it becomes a message only if the person presses
Send. See app/audio_api.py for the route that enforces that.

WHY THE CHAT ENDPOINT AND NOT /v1/audio/transcriptions. Both work. Measured on
this deployment (2026-09-04, Qwen3-ASR-1.7B on the worker, 15 seconds of
audio): transcriptions 1.04s, chat 1.04s — identical. The chat path
additionally returns the language the model IDENTIFIED, in its own output
contract (`language English<asr_text>…`), and a console that reports which
languages a workspace speaks needs that. The transcriptions endpoint stays as
the fallback: it is a different code path in the engine, so a failure in one
is not automatically a failure in both.

FORMAT. None is converted here. vLLM decodes through PyAV, which bundles
ffmpeg, so the WebM/Opus a browser's MediaRecorder produces is understood
natively — verified byte-for-byte against the same clip as WAV. That is why
this orchestrator needs no audio library at all.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Sequence

from . import metrics
from .config import settings

log = logging.getLogger(__name__)

#: The model's own output contract on the chat path. Not a heuristic: the
#: engine always emits the identified language before the transcript marker.
_CHAT_OUTPUT = re.compile(r"^language\s+(?P<language>[^<]+)<asr_text>(?P<text>.*)$", re.S)

#: Languages Qwen3-ASR officially identifies (config.json `support_languages`).
#: Anything else becomes "other" so a mis-parse cannot invent a language, and
#: so the metric's label can never grow without bound.
SUPPORTED_LANGUAGES = (
    "Chinese", "English", "Cantonese", "Arabic", "German", "French", "Spanish",
    "Portuguese", "Indonesian", "Italian", "Korean", "Russian", "Thai",
    "Vietnamese", "Japanese", "Turkish", "Hindi", "Malay", "Dutch", "Swedish",
    "Danish", "Finnish", "Polish", "Czech", "Filipino", "Persian", "Greek",
    "Romanian", "Hungarian", "Macedonian",
)

#: Human name -> BCP-47-ish code, for a response field a browser can use.
_LANGUAGE_CODES = {
    "Chinese": "zh", "English": "en", "Cantonese": "yue", "Arabic": "ar",
    "German": "de", "French": "fr", "Spanish": "es", "Portuguese": "pt",
    "Indonesian": "id", "Italian": "it", "Korean": "ko", "Russian": "ru",
    "Thai": "th", "Vietnamese": "vi", "Japanese": "ja", "Turkish": "tr",
    "Hindi": "hi", "Malay": "ms", "Dutch": "nl", "Swedish": "sv",
    "Danish": "da", "Finnish": "fi", "Polish": "pl", "Czech": "cs",
    "Filipino": "fil", "Persian": "fa", "Greek": "el", "Romanian": "ro",
    "Hungarian": "hu", "Macedonian": "mk",
}


class ASRUnavailable(Exception):
    """The engine could not be reached, or refused. Retryable."""


class ASRBusy(Exception):
    """Every transcription slot is taken and the queue wait ran out."""


class ASRRejected(Exception):
    """The audio itself is the problem — too long, unreadable, empty."""


@dataclass(frozen=True)
class Transcript:
    """One finished transcription. `language` is None when nobody identified it."""

    text: str
    language: Optional[str]
    language_code: Optional[str]
    #: Where the transcript came from, for the log and the metric — never
    #: shown to a member, who has no reason to learn the model's name.
    provider: str
    model: str
    engine_ms: int
    #: True when the primary path failed and the fallback answered.
    degraded: bool = False


class ASRProvider(Protocol):
    """What the route needs from a speech engine, and nothing more."""

    name: str
    model: str

    async def transcribe(
        self, audio: bytes, *, filename: str, content_type: str, language: str = ""
    ) -> Transcript: ...

    async def health(self) -> bool: ...


def language_code(language: str) -> Optional[str]:
    return _LANGUAGE_CODES.get(language.strip().title())


def normalise_language(raw: str) -> Optional[str]:
    """A language the model actually supports, or None.

    Never invents one: an unrecognised value means the identification did not
    survive, and the console shows nothing rather than a plausible guess.
    """
    candidate = (raw or "").strip().rstrip(".").title()
    return candidate if candidate in SUPPORTED_LANGUAGES else None


def parse_chat_output(content: str) -> tuple[Optional[str], str]:
    """Split `language English<asr_text>Hello.` into (language, text)."""
    match = _CHAT_OUTPUT.match((content or "").strip())
    if not match:
        # The engine answered something else — keep the words, drop the claim
        # about which language they are in.
        return None, (content or "").strip()
    return normalise_language(match.group("language")), match.group("text").strip()


class VLLMAudioProvider:
    """An OpenAI-compatible audio endpoint served by vLLM.

    One class covers Qwen3-ASR and Whisper because vLLM serves both behind the
    same two routes; only the model id and how the language arrives differ.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        name: str = "qwen3_asr",
        timeout_s: float = 60.0,
        prefer_chat: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = name
        self.timeout_s = timeout_s
        self.prefer_chat = prefer_chat

    # -- transport ---------------------------------------------------------

    async def _client(self):
        import httpx

        return httpx.AsyncClient(timeout=self.timeout_s)

    async def _chat(self, audio: bytes, content_type: str, language: str) -> Transcript:
        """The path that reports the identified language."""
        import httpx

        data_url = f"data:{content_type};base64,{base64.b64encode(audio).decode()}"
        content: list[dict[str, Any]] = [
            {"type": "audio_url", "audio_url": {"url": data_url}}
        ]
        # A forced language is the caller's explicit choice; auto-detection is
        # the default because a person dictating should not have to declare a
        # language before speaking.
        if language and language != "auto":
            content.append({"type": "text", "text": language})
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": settings.asr_max_tokens,
            # Transcription is not a creative task: the same audio must give
            # the same words every time.
            "temperature": 0.0,
        }
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions", json=payload
                )
        except Exception as exc:  # noqa: BLE001
            raise ASRUnavailable(str(exc)) from exc
        elapsed = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 500:
            raise ASRUnavailable(f"engine returned {response.status_code}")
        if response.status_code >= 400:
            raise ASRRejected(_detail(response))
        # A 200 is not a promise about the shape of the body. A proxy that
        # answers with an HTML interstitial, or an engine build whose reply
        # nests differently, would otherwise raise out of this module as
        # something the route has no name for — and the person holding the
        # microphone would get a 500 instead of a sentence. Unreadable is
        # UNAVAILABLE: it is the same engine trouble as a refused connection,
        # and it is worth trying the other endpoint for.
        try:
            body = response.json()
            raw = str(
                (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            )
        except Exception as exc:  # noqa: BLE001
            raise ASRUnavailable("the engine returned a reply this client cannot read") from exc
        detected, text = parse_chat_output(raw)
        return Transcript(
            text=text,
            language=detected,
            language_code=language_code(detected or ""),
            provider=self.name,
            model=self.model,
            engine_ms=elapsed,
        )

    async def _transcriptions(self, audio: bytes, filename: str, content_type: str) -> Transcript:
        """The fallback: a different code path in the same engine.

        Returns text alone — this endpoint does not report the language for
        this model (the engine answers 400 to `verbose_json`), so `language`
        is None rather than a guess.
        """
        import httpx

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(
                    f"{self.base_url}/audio/transcriptions",
                    files={"file": (filename, audio, content_type)},
                    data={"model": self.model},
                )
        except Exception as exc:  # noqa: BLE001
            raise ASRUnavailable(str(exc)) from exc
        elapsed = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 500:
            raise ASRUnavailable(f"engine returned {response.status_code}")
        if response.status_code >= 400:
            raise ASRRejected(_detail(response))
        try:
            spoken = str(response.json().get("text") or "").strip()
        except Exception as exc:  # noqa: BLE001
            raise ASRUnavailable("the engine returned a reply this client cannot read") from exc
        return Transcript(
            text=spoken,
            language=None,
            language_code=None,
            provider=self.name,
            model=self.model,
            engine_ms=elapsed,
            degraded=True,
        )

    # -- interface ---------------------------------------------------------

    async def transcribe(
        self, audio: bytes, *, filename: str, content_type: str, language: str = ""
    ) -> Transcript:
        if not self.prefer_chat:
            return await self._transcriptions(audio, filename, content_type)
        try:
            return await self._chat(audio, content_type, language)
        except ASRRejected:
            # The engine understood the request and refused the audio. Trying
            # the other endpoint would refuse it again, more slowly.
            raise
        except ASRUnavailable as exc:
            log.warning("ASR chat path failed (%s); trying the transcription path", exc)
            return await self._transcriptions(audio, filename, content_type)

    async def health(self) -> bool:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=min(5.0, self.timeout_s)) as client:
                response = await client.get(f"{self.base_url}/models")
            return response.status_code == 200
        except Exception:  # noqa: BLE001
            return False


def _detail(response: Any) -> str:
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return f"engine returned {response.status_code}"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    return f"engine returned {response.status_code}"


class RoutedProvider:
    """Several engines, one interface: send each clip to the freest one.

    WHY REPLICAS AND NOT ONE SHARDED MODEL. "Use both GPUs" has two possible
    meanings and only one of them is faster here. Splitting a single 1.7B
    model across two Sparks puts every layer's activations on the RoCE fabric
    — 13 Gb/s a link, already carrying the main model's own tensor-parallel
    traffic — to save memory that was never short: the weights are 4.4 GB and
    each node has room for them twice over. Two whole copies with requests
    balanced between them adds throughput without adding a single byte of
    cross-node chatter, and it degrades to one engine gracefully when a node
    goes away. That is what this class does.

    LEAST ACTIVE, not round robin. Clips are not the same size — a
    four-second question and a two-minute dictation are one request each —
    so counting requests sent would send the long one and the next one to the
    same engine. Counting requests still RUNNING sends work where there is
    room for it, which is the property that actually matters.

    A FAILING ENGINE IS SKIPPED, BRIEFLY. An endpoint that raises
    ASRUnavailable is stood down for `_COOLDOWN_S` and the request is retried
    on another. It is never removed permanently: a node that reboots must
    rejoin by itself, without anybody editing configuration.
    """

    #: Long enough that a restarting engine is not hammered, short enough that
    #: a recovered one is back before anyone notices it left.
    _COOLDOWN_S = 20.0

    def __init__(self, engines: Sequence[ASRProvider]) -> None:
        if not engines:
            raise ValueError("RoutedProvider needs at least one engine")
        self._engines = list(engines)
        self._active: Dict[int, int] = {i: 0 for i in range(len(self._engines))}
        self._down_until: Dict[int, float] = {i: 0.0 for i in range(len(self._engines))}
        self.name = self._engines[0].name
        self.model = self._engines[0].model

    def _order(self) -> List[int]:
        """Healthy engines first, freest first; then the ones standing down.

        The stood-down engines stay on the end rather than being dropped, so a
        fleet where every engine is cooling off still tries one instead of
        failing a request nobody had to lose.
        """
        now = time.monotonic()
        healthy = [i for i in range(len(self._engines)) if self._down_until[i] <= now]
        cooling = [i for i in range(len(self._engines)) if self._down_until[i] > now]
        healthy.sort(key=lambda i: self._active[i])
        cooling.sort(key=lambda i: self._down_until[i])
        return healthy + cooling

    async def transcribe(
        self, audio: bytes, *, filename: str, content_type: str, language: str = ""
    ) -> Transcript:
        last: Optional[Exception] = None
        for index in self._order():
            engine = self._engines[index]
            self._active[index] += 1
            try:
                result = await engine.transcribe(
                    audio,
                    filename=filename,
                    content_type=content_type,
                    language=language,
                )
                self._down_until[index] = 0.0
                return result
            except ASRRejected:
                # The engine understood the request and refused the AUDIO.
                # Another engine would refuse it identically, more slowly.
                raise
            except ASRUnavailable as exc:
                last = exc
                self._down_until[index] = time.monotonic() + self._COOLDOWN_S
                log.warning(
                    "ASR engine %s is unavailable (%s); standing it down for %.0fs",
                    getattr(engine, "base_url", index), exc, self._COOLDOWN_S,
                )
            finally:
                self._active[index] -= 1
        raise ASRUnavailable(str(last) if last else "no speech engine answered")

    async def health(self) -> bool:
        """True when ANY engine answers — the feature works on one node."""
        for engine in self._engines:
            if await engine.health():
                return True
        return False

    def stats(self) -> List[Dict[str, Any]]:
        """Per-engine state, for /audio/health and the admin console."""
        now = time.monotonic()
        return [
            {
                "endpoint": getattr(engine, "base_url", ""),
                "active": self._active[i],
                "available": self._down_until[i] <= now,
            }
            for i, engine in enumerate(self._engines)
        ]


# ---------------------------------------------------------------------------
# Admission control
#
# The engine batches happily — eight simultaneous 15-second clips finished in
# 1.10s of wall clock, measured 2026-09-04 — so the limit here is not about
# protecting the ASR engine. It is about the main model: an unbounded fan-out
# of audio requests would eventually contend for the same GPU the chat model
# runs on, and a person waiting for an ANSWER must never be slowed down by
# someone else's dictation. A bounded pool with a short queue is the whole
# mechanism: past it, callers are told to try again rather than queued
# indefinitely behind work they cannot see.
# ---------------------------------------------------------------------------


class _Pool:
    """A semaphore that reports its own depth, and refuses rather than hangs."""

    def __init__(self) -> None:
        self._sem: Optional[asyncio.Semaphore] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.waiting = 0
        self.active = 0

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        # Rebuilt per event loop: a Semaphore bound to a dead loop (the test
        # suite makes a new one per test) blocks forever on the next acquire.
        if self._sem is None or self._loop is not loop:
            # Per engine, times the fleet: two nodes carry twice the work at
            # the same pressure each. `settings.asr_base_urls` is never empty
            # (config falls back to the single URL), so this is at least one.
            self._sem = asyncio.Semaphore(
                max(1, settings.asr_max_concurrent) * max(1, len(settings.asr_base_urls))
            )
            self._loop = loop
        return self._sem

    async def __aenter__(self) -> "_Pool":
        sem = self._semaphore()
        self.waiting += 1
        metrics.set_gauge("asr_queue_depth", self.waiting, "requests waiting for a slot")
        try:
            await asyncio.wait_for(sem.acquire(), timeout=settings.asr_queue_wait_s)
        except asyncio.TimeoutError as exc:
            raise ASRBusy("every transcription slot is busy") from exc
        finally:
            self.waiting -= 1
            metrics.set_gauge("asr_queue_depth", self.waiting, "requests waiting for a slot")
        self.active += 1
        metrics.set_gauge("asr_active_requests", self.active, "transcriptions in flight")
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        self.active -= 1
        metrics.set_gauge("asr_active_requests", self.active, "transcriptions in flight")
        if self._sem is not None:
            self._sem.release()

    def reset_for_tests(self) -> None:
        self._sem = None
        self._loop = None
        self.waiting = 0
        self.active = 0


POOL = _Pool()

_provider: Optional[ASRProvider] = None


def provider() -> ASRProvider:
    """The configured engine, built once.

    Cached because building it is free but re-reading settings on every
    request would let a mid-flight config change split one workspace's
    transcripts across two engines.
    """
    global _provider
    if _provider is None:
        engines = [
            VLLMAudioProvider(
                base_url=url,
                model=settings.asr_model,
                name=settings.asr_backend,
                timeout_s=settings.asr_timeout_s,
            )
            for url in settings.asr_base_urls
        ]
        # One engine still goes through the router: the code path a workspace
        # runs every day should be the one the tests exercise, not a special
        # case that only appears on smaller deployments.
        _provider = RoutedProvider(engines)
    return _provider


def set_provider(value: Optional[ASRProvider]) -> None:
    """Swap the engine. For tests, and for a future second provider."""
    global _provider
    _provider = value


async def transcribe(
    audio: bytes, *, filename: str, content_type: str, language: str = ""
) -> Transcript:
    """Transcribe one clip under the pool, with the metrics that go with it."""
    started = time.perf_counter()
    async with POOL:
        try:
            result = await provider().transcribe(
                audio, filename=filename, content_type=content_type, language=language
            )
        except ASRRejected:
            metrics.inc("asr_requests_total", "transcription attempts", result="fail")
            raise
        except Exception:
            metrics.inc("asr_requests_total", "transcription attempts", result="fail")
            raise
    metrics.inc("asr_requests_total", "transcription attempts", result="ok")
    metrics.observe(
        "asr_request_duration_seconds",
        time.perf_counter() - started,
        "wall clock for one transcription, orchestrator side",
    )
    metrics.inc(
        "asr_detected_language_total",
        "identified languages",
        language=result.language or "unknown",
    )
    return result
