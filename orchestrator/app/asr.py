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

ONE ENDPOINT. /v1/audio/transcriptions, and nothing else. There used to be
two, because Qwen3-ASR was a chat model that also answered on
/v1/chat/completions, and the chat contract was preferred for the language it
named in its own output format. Whisper does not mount the chat route — it
answers 404 — and the old code classified a 404 as "the engine refused this
audio" and pointedly did NOT fall back, so every dictation would have failed
while telling the member their recording was at fault.

WHAT THE ENGINE RETURNS, and why this file is small. The speech server on the
worker (compose/whisper/server.py) answers with the transcript, the language
by name AND as an ISO code, the clip duration, and `no_speech_prob` — its own
judgement that there was nothing to transcribe. Whisper is documented to
hallucinate on silence and, measured here, answers a 30-second silent clip
with " you"; the engine short-circuits that before the model runs. So this
client identifies no languages, splits no audio and guesses nothing.

FORMAT. None is converted here for dictation. The engine decodes with ffmpeg
on its own side, so the WebM/Opus a browser's MediaRecorder produces is
understood natively. Video analysis (app/video) is the one caller that sends
PCM it cut itself — see `transcribe_segments` and the BATCH pool.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Sequence

from . import metrics
from .config import settings

log = logging.getLogger(__name__)

#: The languages the engine can identify — Whisper's own published set,
#: generated from the model's tokenizer rather than typed by hand.
#:
#: This was Qwen3-ASR's THIRTY. Whisper reports ninety-nine, and a name that
#: is not in this tuple is dropped by `normalise_language` — so leaving the
#: short list in place would have silently discarded the language of every
#: clip in the other sixty-nine, while the transcript itself came out fine.
#: `metrics._ALLOWED["language"]` must be widened with it or the metric folds
#: them to "other"; a test asserts the two agree.
SUPPORTED_LANGUAGES = (
    "Afrikaans", "Albanian", "Amharic", "Arabic", "Armenian", "Assamese",
    "Azerbaijani", "Bashkir", "Basque", "Belarusian", "Bengali",
    "Bosnian", "Breton", "Bulgarian", "Cantonese", "Catalan", "Chinese",
    "Croatian", "Czech", "Danish", "Dutch", "English", "Estonian",
    "Faroese", "Finnish", "French", "Galician", "Georgian", "German",
    "Greek", "Gujarati", "Haitian Creole", "Hausa", "Hawaiian", "Hebrew",
    "Hindi", "Hungarian", "Icelandic", "Indonesian", "Italian",
    "Japanese", "Javanese", "Kannada", "Kazakh", "Khmer", "Korean", "Lao",
    "Latin", "Latvian", "Lingala", "Lithuanian", "Luxembourgish",
    "Macedonian", "Malagasy", "Malay", "Malayalam", "Maltese", "Maori",
    "Marathi", "Mongolian", "Myanmar", "Nepali", "Norwegian", "Nynorsk",
    "Occitan", "Pashto", "Persian", "Polish", "Portuguese", "Punjabi",
    "Romanian", "Russian", "Sanskrit", "Serbian", "Shona", "Sindhi",
    "Sinhala", "Slovak", "Slovenian", "Somali", "Spanish", "Sundanese",
    "Swahili", "Swedish", "Tagalog", "Tajik", "Tamil", "Tatar", "Telugu",
    "Thai", "Tibetan", "Turkish", "Turkmen", "Ukrainian", "Urdu", "Uzbek",
    "Vietnamese", "Welsh", "Yiddish", "Yoruba"
)

#: Human name -> BCP-47-ish code, for a response field a browser can use.
#: The engine already returns `language_code` itself; this stays as the
#: fallback for a reply that carries only the name.
#: The engine already returns `language_code` itself; this stays as the
#: fallback for a reply that carries only the name.
_LANGUAGE_CODES = {
    "Afrikaans": "af", "Albanian": "sq", "Amharic": "am", "Arabic": "ar",
    "Armenian": "hy", "Assamese": "as", "Azerbaijani": "az", "Bashkir":
    "ba", "Basque": "eu", "Belarusian": "be", "Bengali": "bn", "Bosnian":
    "bs", "Breton": "br", "Bulgarian": "bg", "Cantonese": "yue",
    "Catalan": "ca", "Chinese": "zh", "Croatian": "hr", "Czech": "cs",
    "Danish": "da", "Dutch": "nl", "English": "en", "Estonian": "et",
    "Faroese": "fo", "Finnish": "fi", "French": "fr", "Galician": "gl",
    "Georgian": "ka", "German": "de", "Greek": "el", "Gujarati": "gu",
    "Haitian Creole": "ht", "Hausa": "ha", "Hawaiian": "haw", "Hebrew":
    "he", "Hindi": "hi", "Hungarian": "hu", "Icelandic": "is",
    "Indonesian": "id", "Italian": "it", "Japanese": "ja", "Javanese":
    "jw", "Kannada": "kn", "Kazakh": "kk", "Khmer": "km", "Korean": "ko",
    "Lao": "lo", "Latin": "la", "Latvian": "lv", "Lingala": "ln",
    "Lithuanian": "lt", "Luxembourgish": "lb", "Macedonian": "mk",
    "Malagasy": "mg", "Malay": "ms", "Malayalam": "ml", "Maltese": "mt",
    "Maori": "mi", "Marathi": "mr", "Mongolian": "mn", "Myanmar": "my",
    "Nepali": "ne", "Norwegian": "no", "Nynorsk": "nn", "Occitan": "oc",
    "Pashto": "ps", "Persian": "fa", "Polish": "pl", "Portuguese": "pt",
    "Punjabi": "pa", "Romanian": "ro", "Russian": "ru", "Sanskrit": "sa",
    "Serbian": "sr", "Shona": "sn", "Sindhi": "sd", "Sinhala": "si",
    "Slovak": "sk", "Slovenian": "sl", "Somali": "so", "Spanish": "es",
    "Sundanese": "su", "Swahili": "sw", "Swedish": "sv", "Tagalog": "tl",
    "Tajik": "tg", "Tamil": "ta", "Tatar": "tt", "Telugu": "te", "Thai":
    "th", "Tibetan": "bo", "Turkish": "tr", "Turkmen": "tk", "Ukrainian":
    "uk", "Urdu": "ur", "Uzbek": "uz", "Vietnamese": "vi", "Welsh": "cy",
    "Yiddish": "yi", "Yoruba": "yo"
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


@dataclass(frozen=True)
class TranscriptSegments(Transcript):
    """A transcript that also carries the engine's timestamped segments.

    `segments` is a tuple of plain dicts — {start, end, text, language?} in
    seconds from the start of THE CLIP — exactly as the engine's verbose_json
    reply spells them. Video analysis offsets them into video time; nothing
    else reads them.
    """

    segments: tuple = ()


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


class VLLMAudioProvider:
    """An OpenAI-compatible /v1/audio/transcriptions endpoint.

    Any engine that speaks that route works here — the speech server on the
    worker (compose/whisper/server.py) and vLLM's own transcription endpoint
    both do. Nothing in this class is specific to a model.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        name: str = "whisper",
        timeout_s: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = name
        self.timeout_s = timeout_s

    # -- transport ---------------------------------------------------------

    async def _client(self):
        import httpx

        return httpx.AsyncClient(timeout=self.timeout_s)

    async def _transcriptions(
        self,
        audio: bytes,
        filename: str,
        content_type: str,
        *,
        segments: bool = False,
        no_speech_check: bool = True,
    ) -> Transcript:
        """The transcription endpoint — the only one Whisper serves.

        The engine's reply carries more than text: the language it identified
        by NAME and as an ISO code, the clip's duration, and `no_speech_prob`
        — its own judgement that the audio held no speech at all. That last
        one matters: Whisper is documented to hallucinate on silence, and
        measured here it answers a 30-second silent clip with " you". The
        engine short-circuits that before the model runs; this reads the
        number so the orchestrator can see it happen.

        Fields are read defensively. A server that answers with text alone is
        still a working server — it just identifies no language.
        """
        import httpx

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                data = {"model": self.model}
                if segments:
                    # OpenAI's name for "the shape with timestamped segments";
                    # the speech server answers it since 2026-09-09.
                    data["response_format"] = "verbose_json"
                if not no_speech_check:
                    # Only a caller that ran its own voice-activity detection
                    # over a longer recording turns the engine's first-30-s
                    # silence gate off; dictation never does.
                    data["no_speech_check"] = "false"
                response = await client.post(
                    f"{self.base_url}/audio/transcriptions",
                    files={"file": (filename, audio, content_type)},
                    data=data,
                )
        except Exception as exc:  # noqa: BLE001
            raise ASRUnavailable(str(exc)) from exc
        elapsed = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 500:
            raise ASRUnavailable(f"engine returned {response.status_code}")
        if response.status_code >= 400:
            raise ASRRejected(_detail(response))
        try:
            body = response.json()
            spoken = str(body.get("text") or "").strip()
        except Exception as exc:  # noqa: BLE001
            raise ASRUnavailable("the engine returned a reply this client cannot read") from exc
        # The engine reports the language by name; keep our own vocabulary as
        # the gate so a surprise value cannot mint a metric series, and fall
        # back to our table when it sends only the name.
        language = normalise_language(str(body.get("language") or ""))
        code = str(body.get("language_code") or "").strip() or None
        if language and not code:
            code = language_code(language)
        if segments:
            raw = body.get("segments")
            parsed = tuple(
                {
                    "start": float(s.get("start", 0.0) or 0.0),
                    "end": float(s.get("end", 0.0) or 0.0),
                    "text": str(s.get("text") or ""),
                    "language": (str(s["language"]) if s.get("language") else None),
                }
                for s in (raw if isinstance(raw, list) else [])
                if isinstance(s, dict)
            )
            if not parsed and spoken:
                # An engine that does not know verbose_json answers text
                # alone. One segment spanning the clip is honest: the words
                # are right, the timing is "somewhere in this window".
                duration = float(body.get("duration") or 0.0)
                parsed = ({"start": 0.0, "end": duration, "text": spoken, "language": code},)
            return TranscriptSegments(
                text=spoken,
                language=language,
                language_code=code,
                provider=self.name,
                model=self.model,
                engine_ms=elapsed,
                degraded=False,
                segments=parsed,
            )
        return Transcript(
            text=spoken,
            language=language,
            language_code=code,
            provider=self.name,
            model=self.model,
            engine_ms=elapsed,
            # NOT degraded. This is the primary path now, and marking every
            # successful dictation as degraded would make the console read as
            # though the service were permanently limping.
            degraded=False,
        )

    # -- interface ---------------------------------------------------------

    async def transcribe(
        self, audio: bytes, *, filename: str, content_type: str, language: str = ""
    ) -> Transcript:
        """One path. Whisper serves /v1/audio/transcriptions and nothing else.

        There used to be two, because Qwen3-ASR was a CHAT model that also
        answered here, and the chat contract was preferred for the language it
        reported. Whisper does not mount the chat route at all — it answers
        404 — and the old code read a 404 as ASRRejected, "the engine refused
        this audio", and deliberately did not fall back. Every dictation would
        have failed, and the member would have been told their recording was
        the problem.

        `language` is accepted and ignored: the engine is deliberately left to
        detect it, because these users code-switch mid-sentence and forcing
        one language mistranscribes the other.
        """
        return await self._transcriptions(audio, filename, content_type)

    async def transcribe_segments(
        self,
        audio: bytes,
        *,
        filename: str,
        content_type: str,
        no_speech_check: bool = True,
    ) -> TranscriptSegments:
        """One clip -> text AND timestamped segments (video analysis)."""
        result = await self._transcriptions(
            audio, filename, content_type, segments=True, no_speech_check=no_speech_check
        )
        assert isinstance(result, TranscriptSegments)
        return result

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
    meanings, and the sharded one is not the faster one here. Splitting a
    single 1.55B model across two Sparks puts every layer's activations on the
    RoCE fabric the main model's own tensor-parallel traffic already uses —
    and cross-node tensor parallelism costs LATENCY per collective (~12.8 µs a
    round trip, on activations a few KB wide), not bandwidth, so the fabric
    being fast (~109 Gb/s a rail) does not recover it. All of that to save
    memory that was never short: whisper-large-v3 is 3.1 GB in float16 and
    either node holds it several times over. Two whole copies with requests
    balanced between them add throughput without one byte of cross-node
    chatter, and degrade to one engine gracefully when a node goes away.

    AND BE HONEST ABOUT WHAT THAT BUYS: a second concurrent SPEAKER, not a
    faster transcript. One clip is decoded by one engine start to finish.
    Two engines double how many people can dictate at once; they do not halve
    the time any one of them waits.

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
        return await self._routed(
            "transcribe", audio, filename=filename, content_type=content_type, language=language
        )

    async def transcribe_segments(
        self,
        audio: bytes,
        *,
        filename: str,
        content_type: str,
        no_speech_check: bool = True,
    ) -> TranscriptSegments:
        return await self._routed(
            "transcribe_segments",
            audio,
            filename=filename,
            content_type=content_type,
            no_speech_check=no_speech_check,
        )

    async def _routed(self, method: str, audio: bytes, **kwargs: Any):
        """Send one call to the freest healthy engine, standing down any
        that is unavailable and trying the next — the same loop for every
        method an engine exposes."""
        last: Optional[Exception] = None
        for index in self._order():
            engine = self._engines[index]
            self._active[index] += 1
            try:
                result = await getattr(engine, method)(audio, **kwargs)
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

    #: How many may be in flight at once. Dictation's pool scales with the
    #: fleet (see below); the batch pool does NOT — see `BATCH_POOL`.
    def _size(self) -> int:
        # Per engine, times the fleet: two nodes carry twice the work at
        # the same pressure each. `settings.asr_base_urls` is never empty
        # (config falls back to the single URL), so this is at least one.
        return max(1, settings.asr_max_concurrent) * max(1, len(settings.asr_base_urls))

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        # Rebuilt per event loop: a Semaphore bound to a dead loop (the test
        # suite makes a new one per test) blocks forever on the next acquire.
        if self._sem is None or self._loop is not loop:
            self._sem = asyncio.Semaphore(self._size())
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


class _BatchPool(_Pool):
    """Admission for BATCH transcription (video analysis).

    A separate pool from dictation's: the engine decodes one clip at a
    time, so a video's windows sent through the dictation pool would hold
    its slots and a person pressing the microphone would be told "busy"
    after eight seconds. The size is `VIDEO_ASR_CONCURRENCY` — two by
    default, one clip per Spark, spread by the router's least-active order
    so a 10-minute recording is transcribed on both nodes at once. The
    chat model is tensor-parallel across both Sparks and slows while either
    engine decodes; the video job pays for that by pacing itself against
    live chat before every clip (video.pipeline.pace), not by idling a GPU.
    """

    def _size(self) -> int:
        return max(1, settings.video_asr_concurrency)


BATCH_POOL = _BatchPool()

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


async def transcribe_segments(
    audio: bytes, *, filename: str, content_type: str, no_speech_check: bool = True
) -> TranscriptSegments:
    """One clip of a longer recording -> timestamped segments, under the
    BATCH pool. Video analysis's door to the speech engine; dictation never
    comes through here and is never queued behind it."""
    started = time.perf_counter()
    async with BATCH_POOL:
        try:
            result = await provider().transcribe_segments(
                audio,
                filename=filename,
                content_type=content_type,
                no_speech_check=no_speech_check,
            )
        except Exception:
            metrics.inc("asr_batch_requests_total", "batch transcription clips", result="fail")
            raise
    metrics.inc("asr_batch_requests_total", "batch transcription clips", result="ok")
    metrics.observe(
        "asr_batch_request_duration_seconds",
        time.perf_counter() - started,
        "wall clock for one batch clip, orchestrator side",
    )
    return result
