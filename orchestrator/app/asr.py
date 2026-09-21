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
client identifies no languages, splits no audio and guesses nothing. What it
does do, for dictation only, is decode a silence-gated clip once more with the
gate off and keep the words only if they are dense enough to be speech — see
`VLLMAudioProvider.transcribe` and `speech_is_plausible`.

TWO DECODES, ONE WALL CLOCK, AND A BOUND ON THE SECOND (2026-09-21). That
optional retry is a whole extra decode of the clip, and it used to have no
upper length bound and a fresh ASR_TIMEOUT_S of its own, so one noisy
ten-minute dictation could hold a Spark for twenty minutes. It is now skipped
above `_RETRY_MAX_SECONDS` of audio, and both decodes share the ONE deadline
`transcribe` opens.

AND THE NUMBERS ARE NOT THROWN AWAY. `no_speech_prob` and the plausibility
verdict decide a `confidence` word on every dictation (CONFIDENCE_*), which
the composer turns into one short line — so an empty draft is never called
silence unless silence is what was measured.

FORMAT. None is converted here for dictation. The engine decodes with ffmpeg
on its own side, so the WebM/Opus a browser's MediaRecorder produces is
understood natively. Video analysis (app/video) is the one caller that sends
PCM it cut itself — see `transcribe_segments` and the BATCH pool.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
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


class ASRTimeout(ASRUnavailable):
    """The engine HAS the clip and did not answer within ASR_TIMEOUT_S.

    Not retried on another replica. The engine decodes in an executor that a
    closed request cannot stop, so it is still working on this clip; sending
    it to the other Spark as well only put a second GPU on a doomed decode
    while chat slowed on both (2026-09-18: a 595 s clip, 268.3 s of decoding,
    was abandoned at 240 s on each replica in turn). A subclass of
    ASRUnavailable so callers that already handle an unavailable engine —
    video analysis, /v1's routing hints — keep working unchanged.

    ONLY A READ TIMEOUT IS ONE. A connect, write or pool timeout means the
    clip never fully reached an engine, so it is an ordinary ASRUnavailable
    and the other replica gets the clip.

    `answer` is set when the engine had already answered honestly and only
    dictation's optional second decode timed out: RoutedProvider stands the
    replica down and returns that answer instead of a 504 for a clip whose
    first pass said, correctly, that nothing was said.
    """

    def __init__(self, message: str = "", *, answer: Optional["Transcript"] = None) -> None:
        super().__init__(message)
        self.answer = answer


class ASRBusy(Exception):
    """Every transcription slot is taken and the queue wait ran out."""


class ASRRejected(Exception):
    """The audio itself is the problem — too long, unreadable, empty."""


#: How sure this client is of a draft, in a CLOSED vocabulary the composer
#: turns into one short sentence. The engine's `no_speech_prob` and this
#: file's plausibility verdict were both computed and then thrown away until
#: 2026-09-21, so the composer said "Nothing was said in that recording." for
#: every empty draft — including the ones this client emptied itself, and the
#: ones where a second opinion was never taken. Measured on this fleet the
#: same day, one request per clip: 20 s of pink noise at -25 dBFS came back
#: as FOURTEEN invented words with no caution of any kind.
#:
#:   None        the engine decoded the clip and was sure somebody spoke.
#:   "low"       there IS a draft and it may be invented — check it.
#:   "unclear"   the draft is empty and silence is NOT established.
#:   "silent"    the draft is empty and nobody spoke.
CONFIDENCE_LOW = "low"
CONFIDENCE_UNCLEAR = "unclear"
CONFIDENCE_SILENT = "silent"


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
    #: One of the CONFIDENCE_* words above, or None when there is nothing to
    #: say. Set by `VLLMAudioProvider.transcribe` — dictation's path — and
    #: left None by `transcribe_segments`, whose caller (video analysis) runs
    #: its own voice-activity detection and has no composer to tell.
    confidence: Optional[str] = None


@dataclass(frozen=True)
class TranscriptSegments(Transcript):
    """A transcript that also carries the engine's timestamped segments.

    `segments` is a tuple of plain dicts — {start, end, text, language?} in
    seconds from the start of THE CLIP — exactly as the engine's verbose_json
    reply spells them. Video analysis offsets them into video time; nothing
    else reads them.
    """

    segments: tuple = ()


@dataclass(frozen=True)
class _Heard:
    """What the engine measured about a clip, beside its transcript: its own
    judgement that nobody spoke, and the length it decoded. Dictation's retry
    and plausibility check read these; None from an engine that does not
    report them. Kept off `Transcript`, whose fields are the public response's
    contract (tests/test_asr_retired.py)."""

    no_speech_prob: Optional[float]
    duration_s: Optional[float]


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
        from .core.net import shared_ssl_context

        return httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s, connect=_CONNECT_TIMEOUT_S), verify=shared_ssl_context())

    async def _transcriptions(
        self,
        audio: bytes,
        filename: str,
        content_type: str,
        *,
        segments: bool = False,
        no_speech_check: bool = True,
    ) -> Transcript:
        result, _heard = await self._exchange(
            audio, filename, content_type, segments=segments, no_speech_check=no_speech_check
        )
        return result

    async def _exchange(
        self,
        audio: bytes,
        filename: str,
        content_type: str,
        *,
        segments: bool = False,
        no_speech_check: bool = True,
        timeout_s: Optional[float] = None,
    ) -> "tuple[Transcript, _Heard]":
        """The transcription endpoint — the only one Whisper serves.

        `timeout_s` is how long to wait for THIS exchange, and it defaults to
        the engine's own ASR_TIMEOUT_S. Dictation passes a smaller number for
        its second decode: the pair shares ONE wall clock rather than taking
        ASR_TIMEOUT_S each (`transcribe`).

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
        from .core.net import shared_ssl_context

        budget = self.timeout_s if timeout_s is None else float(timeout_s)
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(budget, connect=min(_CONNECT_TIMEOUT_S, budget)), verify=shared_ssl_context()) as client:
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
        except httpx.ReadTimeout as exc:
            # The whole clip was sent and the answer did not come back within
            # the budget: the engine has it. Never failed over; see ASRTimeout.
            # A connect, write or pool timeout falls through to the outage
            # below — the clip never fully reached an engine, so the other
            # replica is the right place for it.
            raise ASRTimeout(f"no answer within {budget:.0f}s") from exc
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
        heard = _Heard(_number(body.get("no_speech_prob")), _number(body.get("duration")))
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
            ), heard
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
        ), heard

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

        NOISY SPEECH GETS ONE MORE DECODE, AND EVERY ANSWER IS CHECKED. The
        engine's silence gate judges only its no-speech probability, and
        babble pushes real speech past it: LibriSpeech under six-talker
        babble at -3 dB scored 0.839 and came back empty, where the same
        bytes decoded with the gate off gave 112 words — word error rate
        0.54 against the reference, the rest being the babble, which is an
        editable draft instead of "Nothing was said". So a clip
        the gate emptied is decoded ONCE more without it, and what that
        yields is kept only if `speech_is_plausible` believes it — the gate
        exists because Whisper invents words from silence ("Thank you." from
        10 s of digital silence, measured), and a retry that let those
        through would turn a correct empty draft into a wrong one.

        ONE WALL CLOCK FOR BOTH DECODES (2026-09-21). Until this change each
        exchange carried its own ASR_TIMEOUT_S, so a clip the gate emptied
        could hold a replica for 2 x 600 s — twenty minutes of one Spark for a
        recording the engine had already called silent. Whisper decodes one
        clip at a time and the chat model is tensor-parallel across both
        Sparks, so that window degrades chat for everybody. The deadline below
        is the SAME 600 s that already clears the worst legitimate single
        decode (600 s of audio at the loaded 0.73 s/s = 438 s) by ~37%; the
        second decode spends what the first one left of it, and is skipped
        outright when the clip cannot finish in that (`_retry_affordable`).

        AND THE ANSWER CARRIES WHAT WE KNOW ABOUT IT. Every return below sets
        `confidence` — see the CONFIDENCE_* vocabulary — so the composer can
        say "check this" or "the recording was not clear" instead of claiming
        silence it did not measure.
        """
        deadline = time.monotonic() + self.timeout_s
        first, heard = await self._exchange(audio, filename, content_type)
        if first.text:
            if speech_is_plausible(
                first.text,
                heard.duration_s or 0.0,
                engine_heard_speech=True,
                no_speech_prob=heard.no_speech_prob,
            ):
                return dataclasses.replace(
                    first,
                    confidence=_reply_confidence(
                        first.text, heard.duration_s or 0.0, heard.no_speech_prob
                    ),
                )
            metrics.inc("asr_implausible_reply_total", "dictation replies dropped as invented", result="rejected")
            # The engine heard something and this client judged it invented.
            # Whatever was on that clip, "nothing was said" is not what was
            # measured, so the empty draft goes back as UNCLEAR.
            return dataclasses.replace(
                first, text="", language=None, language_code=None,
                confidence=CONFIDENCE_UNCLEAR,
            )
        if not _gated(first, heard):
            if _gate_emptied(first, heard) and (heard.duration_s or 0.0) > _RETRY_MAX_SECONDS:
                # Counted, because a bound nobody can see is a bound nobody
                # can revisit: this is how often a long recording loses its
                # second opinion to `_RETRY_MAX_SECONDS`.
                metrics.inc(
                    "asr_gated_retry_too_long_total",
                    "silence-gated dictations too long to decode a second time",
                )
            return dataclasses.replace(
                first, confidence=_silence_confidence(first, heard)
            )
        remaining = deadline - time.monotonic()
        if not _retry_affordable(heard.duration_s or 0.0, remaining):
            # The first pass used the dictation's wall clock up. Starting a
            # decode that cannot finish inside it would take a GPU for a
            # transcript nobody will ever receive.
            metrics.inc(
                "asr_gated_retry_skipped_total",
                "silence-gated dictations whose second decode did not fit the wall clock",
            )
            return dataclasses.replace(first, confidence=CONFIDENCE_UNCLEAR)
        # THE RETRY IS AN OPTIONAL SECOND OPINION: it must never leave a gated
        # clip worse off than the first pass's honest empty answer. A refusal
        # (4xx) or a reply this client cannot parse ends in that answer, not
        # in a 422 or a 503 for a silent clip. An outage (5xx, connection)
        # still propagates so RoutedProvider fails over; a timeout carries
        # the first answer so the replica is stood down AND the person gets it.
        try:
            second, heard_again = await self._exchange(
                audio, filename, content_type,
                segments=True, no_speech_check=False, timeout_s=remaining,
            )
        except ASRTimeout as exc:
            metrics.inc("asr_gated_retry_total", "silence-gated dictations decoded again", result="fail")
            raise ASRTimeout(
                str(exc), answer=dataclasses.replace(first, confidence=CONFIDENCE_UNCLEAR)
            ) from exc
        except (ASRRejected, ValueError, TypeError) as exc:
            metrics.inc("asr_gated_retry_total", "silence-gated dictations decoded again", result="fail")
            log.info("ASR gated retry gave nothing usable (%s); keeping the empty first pass", exc)
            return dataclasses.replace(first, confidence=CONFIDENCE_UNCLEAR)
        if not isinstance(second, TranscriptSegments):
            return dataclasses.replace(first, confidence=CONFIDENCE_UNCLEAR)
        engine_ms = first.engine_ms + second.engine_ms
        if speech_is_plausible(
            second.text,
            heard_again.duration_s or heard.duration_s or 0.0,
            second.segments,
            engine_heard_speech=False,
        ):
            metrics.inc("asr_gated_retry_total", "silence-gated dictations decoded again", result="accepted")
            # A plain Transcript, the shape dictation always returns: the
            # segments were only the evidence. ALWAYS low confidence: the
            # engine's own gate called this clip silent and these words exist
            # only because we asked a second time with the gate off.
            return Transcript(
                text=second.text,
                language=second.language,
                language_code=second.language_code,
                provider=second.provider,
                model=second.model,
                engine_ms=engine_ms,
                confidence=CONFIDENCE_LOW,
            )
        metrics.inc("asr_gated_retry_total", "silence-gated dictations decoded again", result="rejected")
        # Asked twice, nothing plausible either time: this is the one empty
        # draft we may honestly call silence.
        return dataclasses.replace(
            first, engine_ms=engine_ms, confidence=CONFIDENCE_SILENT
        )

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
        from .core.net import shared_ssl_context

        try:
            async with httpx.AsyncClient(timeout=min(5.0, self.timeout_s), verify=shared_ssl_context()) as client:
                response = await client.get(f"{self.base_url}/models")
            return response.status_code == 200
        except Exception:  # noqa: BLE001
            return False


def _number(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Dictation's second pass and plausibility check (2026-09-18)
#
# Every threshold below comes from one labelled set, decoded on the worker
# replica: ten LibriSpeech test-clean utterances (5-8 s), LibriSpeech under
# six-talker babble at 0 and -3 dB and under pink noise at 0 dB, one real
# three-word utterance in 20 s of quiet room noise, and six clips with no
# speech at all (pink at -20 and -35 dBFS, brown, white, 50 Hz hum with hiss,
# digital silence) — plus QA's rows: seven real short utterances after a
# 5-7 s lead-in, and white, fan and pink noise at room level.
# tests/test_asr_long_and_noisy.py carries the table.
# ---------------------------------------------------------------------------

#: compose/whisper/server.py's NO_SPEECH_THRESHOLD, which both replicas report
#: on /health as 0.6. An empty reply above it is the gate, not the decoder.
_ENGINE_NO_SPEECH_THRESHOLD = 0.6

#: A gated clip shorter than this is not decoded again. The engine's own
#: validation set put its most marginal REAL utterance at three seconds.
_RETRY_MIN_SECONDS = 3.0

#: Seconds of DECODING per second of audio, measured on this fleet 2026-09-18
#: and recorded beside ASR_TIMEOUT_S in config.py (the comment at :266-279):
#: 0.45 s/s on a quiet replica (300 s of audio in 132.8 s, 595 s in 268.3 s)
#: and 0.73 s/s with other clips queued on the same replica (300 s in 219.7 s
#: — whisper serialises, and the wait counts). Every bound below is sized
#: from the LOADED rate, because the clip that costs the most is the one that
#: arrives while the fleet is already busy. app/audio_api.py reads the same
#: constant for the sentence it puts on a 504; there is one copy of the
#: number, here.
QUIET_DECODE_S_PER_AUDIO_S = 0.45
LOADED_DECODE_S_PER_AUDIO_S = 0.73

#: And a gated clip LONGER than this is not decoded again either (2026-09-21).
#: The retry costs a WHOLE second decode of the clip, and it had no upper
#: bound at all: at the loaded rate above, the 600 s the composer records is
#: 438 s of one Spark — spent on a recording the engine has already said was
#: silent, on top of the first pass. Two minutes of audio costs 87.6 s loaded
#: (54.0 s quiet), which is what an OPTIONAL second opinion may take.
#:
#: Two minutes and not less: every retry the labelled set above accepted was
#: an utterance of 5-8 s under babble, the longest clip in it is 30 s, and
#: nothing measured needs two minutes of audio to be recognised as speech.
#: Past it the first pass's empty answer stands and the person is told the
#: recording was UNCLEAR rather than silent — which is the truth, because
#: nobody asked twice.
_RETRY_MAX_SECONDS = 120.0

#: Words per second of speech-covered audio a transcript must average when
#: the engine judged the clip SILENT and the words come only from the
#: ungated retry. Measured with the gate off: every no-speech clip decoded
#: at 0.27 words/s or less ("Thank you for watching!" over 15 s of brown
#: noise, the worst); noisy speech at 2.95 or more; the slowest clean
#: utterance at 1.35. 1.0 sits ~3.7x above the first and below the rest.
_GATED_MIN_WORDS_PER_S = 1.0

#: For text the engine decoded NORMALLY (its gate judged the clip speech),
#: one signature is doubted, and only when all three parts of it hold.
#:
#: 1. The text is nothing but Whisper's stock phrases — a CLOSED list, not
#:    "any short reply". The first version dropped every reply of four words
#:    or fewer from 10 s or more, and QA measured it deleting real dictations
#:    4810da0 returned verbatim: "He knows them both." (15 s), "The problem
#:    was solved." (12 s), "no sir certainly not" (11 s). Measured here as
#:    inventions: "Thank you." (digital silence, dither, room and fan noise
#:    with the gate off), "Thank you for watching!" (brown noise), "you" (30 s
#:    of silence), "All right." (20 s of pink room noise, the audit), "Okay."
#:    (10 s of white noise). "Thanks for watching" and "Bye" are the same
#:    family, widely reported for Whisper. A sentence-by-sentence match, so
#:    "Thank you. Thank you." (60 s of dither, measured) is one too.
#: How long to wait for a TCP connection to a replica. A replica that drops
#: connection attempts took 134.9 s to fail (the kernel's SYN retries) before
#: failing over; 10 s is ample for a LAN replica (review 2026-09-19). Reading the
#: transcript keeps the full ASR_TIMEOUT_S.
_CONNECT_TIMEOUT_S = 10.0

_STOCK_PHRASES = frozenset({
    "you", "thank you", "thank you for watching", "thanks for watching",
    "all right", "alright", "okay", "ok", "bye",
})
#: 2. From at least this much audio. A person who says "Okay." into a
#:    three-second clip is answering. 20 s, not 10 s (review 2026-09-19): live,
#:    a real "Thank you." said after a 9 s pause scored 0.0385, above the line
#:    below, and was dropped as noise at 10 s. From 20 s of audio a lone stock
#:    phrase is still dropped ("All right." from room noise); from 10-20 s it is
#:    kept, as 4810da0 kept it.
_STOCK_PHRASE_MIN_SECONDS = 20.0
#: 3. The engine was NOT sure somebody spoke. Its no-speech probability on
#:    real short utterances after a 5-7 s lead-in in a quiet room measured
#:    0.0003-0.018 (n=7, LibriSpeech); on noise that passed its gate, 0.025-
#:    0.060 (20 s of pink at -20 and -35 dBFS, five clips), 0.0355 (hum and
#:    hiss) and 0.1225 (white, which decoded as "Okay."). The audit's "All
#:    right." came from room-level pink noise; its number was not recorded.
#:    That is a SMALL labelled set: the line sits between the speech group
#:    and most of the noise rather than claiming a margin, and a reply the
#:    engine was surer of than this is always kept. The price: a stock phrase from a noise
#:    clip scoring under the line (one pink clip, 0.0251) is kept, as 4810da0
#:    kept it. Unknown (an engine that does not report it) counts as unsure.
_CONFIDENT_SPEECH_NSP = 0.03
#: NOT CAUGHT, and not claimed: inventions that are not stock phrases. 20 s
#: of pink noise at -20 and -35 dBFS passed the gate (no_speech_prob 0.025-
#: 0.060, five clips) and decoded as whole video-outro sentences at 1.05-1.40
#: words/s — inside the clean-speech range (1.35-3.35) — and 20 s of fan
#: noise as "Stabilization is very good." (0.0355). 4810da0 returned the
#: same text. Nothing on this side separates those from speech without a
#: second decode of every clip; that belongs to the engine's gate.

#: Scripts written without spaces between words. Counted by whitespace, a
#: whole Chinese sentence would be one "word" and always look sparse, so each
#: character in these ranges counts as one unit (a syllable, roughly).
_UNSPACED = re.compile(
    r"[\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff\u0f00-\u0fff"  # Thai, Lao, Myanmar, Khmer, Tibetan
    r"\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"  # kana, CJK
)
_WORDISH = re.compile(r"\w", re.UNICODE)
_SENTENCE_BREAK = re.compile(r"[.,!?;:\u2026\u3002\uff01\uff1f]+")


def _speech_units(text: str) -> int:
    """Words, with each character of an unspaced script counted as one.
    Punctuation alone (". ." from noise, measured) is zero."""
    units = 0
    for token in (text or "").split():
        unspaced = len(_UNSPACED.findall(token))
        if unspaced:
            units += unspaced
        elif _WORDISH.search(token):
            units += 1
    return units


def _covered_seconds(segments: Sequence[Dict[str, Any]]) -> float:
    """Seconds of the clip that segments carrying words span, overlaps merged."""
    spans = sorted(
        (float(s.get("start") or 0.0), float(s.get("end") or 0.0))
        for s in segments or ()
        if _speech_units(str(s.get("text") or ""))
    )
    total, current = 0.0, None
    for start, end in spans:
        if current is None or start > current[1]:
            if current is not None:
                total += current[1] - current[0]
            current = [start, max(start, end)]
        else:
            current[1] = max(current[1], end)
    if current is not None:
        total += current[1] - current[0]
    return total


def _only_stock_phrases(text: str) -> bool:
    """Every sentence of `text` is one of Whisper's stock inventions."""
    parts = [
        " ".join(re.sub(r"[^\w\s']", " ", part).lower().split())
        for part in _SENTENCE_BREAK.split(text or "")
    ]
    parts = [part for part in parts if part]
    return bool(parts) and all(part in _STOCK_PHRASES for part in parts)


def speech_is_plausible(
    text: str,
    seconds: float,
    segments: Sequence[Dict[str, Any]] = (),
    *,
    engine_heard_speech: bool,
    no_speech_prob: Optional[float] = None,
) -> bool:
    """Whether a dictation transcript is speech rather than Whisper's invention.

    `engine_heard_speech` is the engine's prior. True: its silence gate let
    the clip through and decoded it normally, so the words are kept unless
    they are nothing but a stock phrase, from a long clip, that the engine
    was not sure was speech (`no_speech_prob`). False: the gate called the
    clip silent and the text comes from the ungated retry, so the words must
    be as dense as speech — measured over the audio the segments cover, or
    the whole clip when the reply carries no segments.
    """
    units = _speech_units(text)
    if units == 0:
        return False
    if engine_heard_speech:
        if no_speech_prob is not None and no_speech_prob < _CONFIDENT_SPEECH_NSP:
            return True
        return not (seconds >= _STOCK_PHRASE_MIN_SECONDS and _only_stock_phrases(text))
    covered = _covered_seconds(segments) or seconds
    return units / max(covered, 1.0) >= _GATED_MIN_WORDS_PER_S


def _gate_emptied(result: Transcript, heard: _Heard) -> bool:
    """The engine's SILENCE GATE is why this clip came back empty.

    compose/whisper/server.py short-circuits before the model runs when the
    no-speech probability of the first thirty seconds clears its threshold, so
    an empty reply above that line is the gate's answer and not the decoder's.
    """
    return (
        not result.text
        and heard.no_speech_prob is not None
        and heard.no_speech_prob > _ENGINE_NO_SPEECH_THRESHOLD
    )


def _gated(result: Transcript, heard: _Heard) -> bool:
    """The engine's silence gate emptied this clip, and it is long enough —
    and short enough — to be worth one more decode. See `_RETRY_MIN_SECONDS`
    and `_RETRY_MAX_SECONDS` for both ends and what they were measured from."""
    duration = heard.duration_s or 0.0
    return (
        _gate_emptied(result, heard)
        and _RETRY_MIN_SECONDS <= duration <= _RETRY_MAX_SECONDS
    )


def _retry_affordable(duration_s: float, remaining_s: float) -> bool:
    """Whether the second decode can finish in what is left of the dictation's
    one wall clock.

    Estimated at the LOADED rate: a replica that is already decoding somebody
    else's clip is exactly when this optional decode is most expensive, and
    guessing the quiet rate would start decodes that end as a 504 having spent
    the GPU anyway.
    """
    return remaining_s > 0.0 and duration_s * LOADED_DECODE_S_PER_AUDIO_S <= remaining_s


def _silence_confidence(result: Transcript, heard: _Heard) -> str:
    """Why an EMPTY draft is empty, claiming no more than was measured.

    Only one case earns the word "silent" without a second decode: the gate
    fired on a clip shorter than the most marginal real utterance in the
    engine's own validation set (`_RETRY_MIN_SECONDS`, 3 s). Everything else
    — a clip the decoder itself returned empty, a gated clip whose second
    opinion was refused or did not fit the wall clock — is UNCLEAR, because
    babble is measured to push real speech past that gate (the LibriSpeech
    row in the block above: 0.839, empty, 112 words with the gate off).
    """
    if (
        _gate_emptied(result, heard)
        # An engine that reported no duration has not told us whether this
        # was a two-second tap or ten minutes, and the 3 s line is the whole
        # reason the short case may be believed unasked.
        and heard.duration_s is not None
        and heard.duration_s < _RETRY_MIN_SECONDS
    ):
        return CONFIDENCE_SILENT
    return CONFIDENCE_UNCLEAR


def _reply_confidence(
    text: str, seconds: float, no_speech_prob: Optional[float]
) -> Optional[str]:
    """Whether a transcript the engine decoded NORMALLY is worth a caution.

    BOTH of this file's already-measured lines have to agree, and no third
    number is introduced. The engine must have been unsure (`no_speech_prob`
    at or above `_CONFIDENT_SPEECH_NSP`, the line below which every real
    utterance in the labelled set sits and above which noise decoded as whole
    sentences), AND the words must be as sparse as the things noise produces
    (`_GATED_MIN_WORDS_PER_S`).

    Measured on this fleet 2026-09-21, one request per clip, with the same
    real recording behind the speech rows:

        clip                      no_speech_prob   words/s   cautioned
        8 s clean speech                  0.0088      2.13   no
        8 s of the same, 26 dB down       0.0238      2.13   no
        30 s clean dictation              0.1034      2.17   no
        20 s pink noise at -25 dBFS       0.0352      0.70   YES (14 words)

    The probability alone would have cautioned that 30-second dictation, and
    a caution on ordinary speech is how a warning stops being read. Sparse
    alone would caution a real one-word answer. Together they caught the
    noise clip and left all three speech clips alone.

    An engine that reports no probability gets no opinion: unknown is not
    evidence, and a caution on every dictation is noise.
    """
    if no_speech_prob is None or no_speech_prob < _CONFIDENT_SPEECH_NSP:
        return None
    units = _speech_units(text)
    if units and units / max(seconds, 1.0) < _GATED_MIN_WORDS_PER_S:
        return CONFIDENCE_LOW
    return None


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
    rejoin by itself, without anybody editing configuration. The exception is
    ASRTimeout: that engine is stood down too, but the clip is NOT retried —
    the engine still has it, and a second replica would decode it in vain.
    """

    #: Long enough that a restarting engine is not hammered, short enough that
    #: a recovered one is back before anyone notices it left.
    _COOLDOWN_S = 20.0
    #: The ceiling on the doubling below. An engine that has been broken for
    #: ten minutes is checked every ten minutes, not every twenty seconds.
    _COOLDOWN_MAX_S = 600.0

    def __init__(self, engines: Sequence[ASRProvider]) -> None:
        if not engines:
            raise ValueError("RoutedProvider needs at least one engine")
        self._engines = list(engines)
        self._active: Dict[int, int] = {i: 0 for i in range(len(self._engines))}
        self._down_until: Dict[int, float] = {i: 0.0 for i in range(len(self._engines))}
        #: Consecutive failures per engine, reset by the first success. It
        #: lengthens the stand-down and — just as importantly — is REPORTED,
        #: because the failure this exists for is invisible otherwise.
        self._failures: Dict[int, int] = {i: 0 for i in range(len(self._engines))}
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
                if self._failures[index]:
                    log.info(
                        "ASR engine %s answered again after %d consecutive failures",
                        getattr(engine, "base_url", index), self._failures[index],
                    )
                self._down_until[index] = 0.0
                self._failures[index] = 0
                return result
            except ASRRejected:
                # The engine understood the request and refused the AUDIO.
                # Another engine would refuse it identically, more slowly.
                raise
            except ASRUnavailable as exc:
                last = exc
                timed_out = isinstance(exc, ASRTimeout)
                # THE STAND-DOWN LENGTHENS WITH CONSECUTIVE FAILURES, and the
                # reason is a real outage: on 2026-09-10 one node's engine
                # answered /health for twelve hours while every transcription
                # on it died with a CUDA error. A fixed twenty seconds meant a
                # clip was fed to it every twenty seconds, failed after the
                # round trip, and was retried elsewhere — so half the fleet
                # was dead, the work serialised onto one node, and nothing
                # said so. Doubling turns a permanent fault into a cheap
                # periodic retry, and the count below makes it visible.
                self._failures[index] += 1
                cooldown = min(
                    self._COOLDOWN_S * (2 ** (self._failures[index] - 1)),
                    self._COOLDOWN_MAX_S,
                )
                self._down_until[index] = time.monotonic() + cooldown
                metrics.inc(
                    "asr_engine_standdown_total",
                    "times a speech engine was stood down after failing a request",
                )
                log.warning(
                    "ASR engine %s is unavailable (%s); standing it down for %.0fs "
                    "(consecutive failures: %d)",
                    getattr(engine, "base_url", index), exc, cooldown, self._failures[index],
                )
                if timed_out:
                    # Stood down (it is still decoding this clip, so the next
                    # dictation should not queue behind it) but NOT re-sent:
                    # the other replica would only decode the same doomed
                    # clip. See ASRTimeout. When only dictation's optional
                    # second decode timed out, the first pass's answer stands.
                    answer = getattr(exc, "answer", None)
                    if answer is not None:
                        return answer
                    raise
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
                # An engine that answers /health and fails every transcription
                # looks available here without these two.
                "consecutive_failures": self._failures[i],
                "standing_down_for_s": max(0, round(self._down_until[i] - now)),
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
        urls = [u for u in settings.asr_base_urls if u and u.strip()]
        if not urls:
            # Raise, never stub: a provider that answered every recording
            # with an empty transcript would look like a microphone that
            # hears nothing. The route turns this into a plain 503 and
            # /audio/health into a reason an administrator can read.
            raise ASRUnavailable("no speech engine is configured (ASR_BASE_URLS is empty)")
        engines = [
            VLLMAudioProvider(
                base_url=url,
                model=settings.asr_model,
                name=settings.asr_backend,
                timeout_s=settings.asr_timeout_s,
            )
            for url in urls
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
    if result.confidence == CONFIDENCE_LOW:
        # Counted in ONE place for both ways a draft earns a caution — a
        # sparse reply the engine was unsure of, and words that exist only
        # because the gated clip was decoded a second time.
        metrics.inc(
            "asr_low_confidence_total",
            "dictations returned with a caution the composer shows",
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
