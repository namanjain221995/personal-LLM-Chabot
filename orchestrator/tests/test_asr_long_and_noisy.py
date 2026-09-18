"""Long dictation, noisy dictation, and the wait in between (B9, B10, 2026-09-18).

THREE THINGS WERE WRONG, all measured on this hardware before a line changed.

  1. A LONG DICTATION COULD NEVER FINISH. Whisper decodes long-form audio at
     0.45 s per second of audio on a quiet replica (595 s -> 268.3 s) and
     0.73 s on a busy one (300 s -> 219.7 s), and the client gave up at
     ASR_TIMEOUT_S = 240. The timeout was raised as
     ASRUnavailable, so RoutedProvider stood the engine down and sent the
     whole clip to the OTHER replica, which could not finish it either. Both
     Sparks decoded a doomed clip while chat slowed on both, and the person
     was told to try again. A timeout is not an outage: the engine has the
     clip and is still decoding it (its decode runs in an executor a closed
     request cannot stop), so re-sending it only doubles the doomed work.

  2. NOTHING CAME BACK UNTIL IT WAS DONE. /audio/transcribe sent no byte
     until the transcript existed, and Cloudflare gives up on a first byte at
     125 s, so the public wall was about 4.5 minutes of audio while the UI
     allows ten. The route now answers within HEARTBEAT_S of starting work:
     a streamed 200 whose leading whitespace is legal JSON, then the JSON.

  3. THE SILENCE GATE THREW AWAY NOISY SPEECH, AND THE DECODER INVENTED WORDS
     FROM NOISE. The engine returns empty, without decoding, when its
     no-speech probability is over 0.6. Measured on public-domain LibriSpeech
     mixed with six-talker babble at -3 dB: no_speech_prob 0.839, empty; the
     same bytes decoded with the gate off gave 112 words (word error rate 0.54
     against the reference: an editable draft, not silence). The other
     direction: stock phrases ("Thank you.", "Thank you for watching!") from
     silence and noise. One ungated retry, and a plausibility check measured
     on a labelled set, close both on the orchestrator side.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app import asr, audio_api
from app.auth import require_user
from app.config import settings

REPO = Path(__file__).resolve().parents[2]
MODEL = "openai/whisper-large-v3"
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 8192

#: A dense, ordinary reply (words are public-domain LibriSpeech text).
SPOKEN = "after early nightfall the yellow lamps would light up here and there"
SPEECH_REPLY = {
    "text": SPOKEN, "language": "english", "language_code": "en",
    "duration": 6.625, "no_speech_prob": 0.0,
}


# ---------------------------------------------------------------------------
# A fake socket under the REAL VLLMAudioProvider
# ---------------------------------------------------------------------------


def _fields(content: bytes) -> dict:
    body = content.decode("latin-1")
    out = {}
    for field in ("model", "response_format", "no_speech_check"):
        marker = f'name="{field}"\r\n\r\n'
        if marker in body:
            out[field] = body.split(marker, 1)[1].split("\r\n", 1)[0]
    return out


class _Wire:
    """Every engine host answers from its own script, in order; the last step
    repeats. A step is (status, json), or an httpx exception CLASS to raise."""

    def __init__(self, monkeypatch) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.scripts: dict[str, list] = {}
        transport = httpx.MockTransport(self._handle)
        real = httpx.AsyncClient

        def fake(*args, **kwargs):
            kwargs["transport"] = transport
            return real(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", fake)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.requests.append((host, _fields(request.content)))
        script = self.scripts[host]
        step = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(step, type) and issubclass(step, Exception):
            raise step("simulated", request=request)
        status, body = step
        return httpx.Response(status, json=body)

    @property
    def hosts(self) -> list[str]:
        return [host for host, _ in self.requests]


def _engine(host: str) -> asr.VLLMAudioProvider:
    return asr.VLLMAudioProvider(
        base_url=f"http://{host}:30007/v1", model=MODEL, name="whisper", timeout_s=5.0
    )


def _dictate(provider) -> asr.Transcript:
    return asyncio.run(
        provider.transcribe(WEBM, filename="recording.webm", content_type="audio/webm")
    )


# ---------------------------------------------------------------------------
# 1. A timeout is not an outage
# ---------------------------------------------------------------------------


def test_a_clip_that_times_out_is_not_decoded_again_on_the_other_replica(monkeypatch):
    wire = _Wire(monkeypatch)
    wire.scripts["slow"] = [httpx.ReadTimeout]
    wire.scripts["spare"] = [(200, SPEECH_REPLY)]
    router = asr.RoutedProvider([_engine("slow"), _engine("spare")])

    try:
        _dictate(router)
        raised: BaseException | None = None
    except Exception as exc:  # noqa: BLE001 - the type is asserted below
        raised = exc

    # At 4810da0 this was ["slow", "spare"]: the doomed clip was decoded twice.
    assert wire.hosts == ["slow"]
    assert isinstance(raised, asr.ASRTimeout)
    # Still an ASRUnavailable, so every caller that already catches that (the
    # video pipeline, /v1's routing hints) keeps working unchanged.
    assert isinstance(raised, asr.ASRUnavailable)
    # And the replica is stood down: it is still decoding that clip, so the
    # next dictation should not queue behind it.
    assert router.stats()[0]["available"] is False


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError, httpx.ConnectTimeout, (503, {"detail": "model is still loading"})],
    ids=["connection-refused", "connect-timeout", "engine-503"],
)
def test_a_replica_that_is_down_still_fails_over(monkeypatch, failure):
    """Connection errors and 5xx are outages: the engine never decoded the clip,
    so the other replica is exactly the right place for it. A connect timeout
    is a connection error — no engine ever saw the audio."""
    wire = _Wire(monkeypatch)
    wire.scripts["down"] = [failure]
    wire.scripts["spare"] = [(200, SPEECH_REPLY)]
    router = asr.RoutedProvider([_engine("down"), _engine("spare")])

    assert _dictate(router).text == SPOKEN
    assert wire.hosts == ["down", "spare"]


def test_the_default_timeout_outlasts_the_longest_clip_the_ui_allows(monkeypatch):
    """Ten minutes is the ceiling the composer records to. Measured
    2026-09-18: 595 s of audio decoded in 268.3 s on a quiet replica, and
    300 s took 219.7 s with other clips queued on it. 240 s could never
    finish the longest clip, and 360 s could not under load."""
    from app.config import Settings

    monkeypatch.delenv("ASR_TIMEOUT_S", raising=False)
    monkeypatch.delenv("ASR_MAX_AUDIO_SECONDS", raising=False)
    fresh = Settings()
    assert fresh.asr_timeout_s >= 360
    quiet_s = 268.3 / 595 * fresh.asr_max_audio_seconds
    loaded_s = 219.7 / 300 * fresh.asr_max_audio_seconds
    assert fresh.asr_timeout_s > quiet_s
    assert fresh.asr_timeout_s > loaded_s


def test_compose_and_the_example_env_ship_the_same_default():
    """The production .env does not set ASR_TIMEOUT_S, so compose's default is
    the value that runs; config.py's is the one tests see. They must agree."""
    compose = (REPO / "compose.yaml").read_text()
    assert "ASR_TIMEOUT_S: ${ASR_TIMEOUT_S:-600}" in compose
    example = (REPO / ".env.example").read_text()
    assert re.search(r"^ASR_TIMEOUT_S=600$", example, re.M)
    assert "ASR_TIMEOUT_S" in (REPO / "docs" / "CONFIG.md").read_text()


# ---------------------------------------------------------------------------
# 2. The route answers while the engine works
# ---------------------------------------------------------------------------


class _GatedEngine:
    """Answers (or fails) only when the test says so."""

    name = "whisper"
    model = MODEL

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.go = asyncio.Event()
        self.raises = raises
        self.calls = 0

    async def transcribe(self, audio, *, filename, content_type, language=""):
        self.calls += 1
        await self.go.wait()
        if self.raises is not None:
            raise self.raises
        return asr.Transcript(
            text=SPOKEN, language="English", language_code="en",
            provider="whisper", model=MODEL, engine_ms=12,
        )

    async def health(self) -> bool:
        return True


@pytest.fixture()
def route(monkeypatch):
    """The real /audio/transcribe router, with sign-in and the feature gate
    stubbed (test_voice_input.py owns those) and the telemetry row captured,
    so these tests need no database and drive the ASGI app byte by byte."""
    monkeypatch.setattr(settings, "asr_enabled", True)
    audio_api.reset_for_tests()
    rows: list[str] = []

    async def record(user_id, duration_ms, language, processing_ms, status, **_kw):
        rows.append(status)

    monkeypatch.setattr(audio_api, "_record", record)
    app = FastAPI()
    app.include_router(audio_api.router)
    app.dependency_overrides[require_user] = lambda: {"id": 7, "username": "bob"}
    app.dependency_overrides[audio_api.require_voice] = lambda: None
    yield app, rows
    asr.set_provider(None)
    audio_api.reset_for_tests()


async def _drive(app, *, content_type=b"audio/webm", on_body=None):
    """One POST through the ASGI app; returns (status, [body chunks])."""
    sent = False
    start: dict = {}
    chunks: list[bytes] = []

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": WEBM, "more_body": False}
        await asyncio.Event().wait()  # the client never leaves

    async def send(message):
        if message["type"] == "http.response.start":
            start.update(message)
        elif message["type"] == "http.response.body" and message.get("body"):
            chunks.append(message["body"])
            if on_body is not None:
                on_body(chunks)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/audio/transcribe",
        "raw_path": b"/audio/transcribe", "root_path": "",
        "query_string": b"duration_ms=4200&language=auto",
        "headers": [(b"content-type", content_type), (b"host", b"test")],
        "client": ("127.0.0.1", 5000), "server": ("test", 80),
    }
    await app(scope, receive, send)
    return start["status"], chunks


def _beats_then(engine: _GatedEngine, beats: int):
    """Release the engine once `beats` whitespace chunks have gone out."""

    def on_body(chunks):
        if sum(1 for c in chunks if not c.strip()) >= beats:
            engine.go.set()

    return on_body


def test_a_long_transcription_answers_with_whitespace_heartbeats_then_the_json(route, monkeypatch):
    app, rows = route
    monkeypatch.setattr(audio_api, "HEARTBEAT_S", 0.02)
    engine = _GatedEngine()
    asr.set_provider(engine)

    status, chunks = asyncio.run(_drive(app, on_body=_beats_then(engine, 3)))

    assert status == 200
    beats = [c for c in chunks if not c.strip()]
    assert len(beats) >= 3
    assert all(re.fullmatch(rb"\s+", c) for c in beats)
    # Every heartbeat precedes the JSON, and the whole body is ONE JSON value:
    # leading whitespace is insignificant in JSON, so a client that just
    # parses the body reads the transcript.
    body = b"".join(chunks)
    assert body[: len(beats)].strip() == b""
    payload = json.loads(body)
    assert payload["text"] == SPOKEN
    assert "status" not in payload and "detail" not in payload
    assert rows == ["ok"]


@pytest.mark.parametrize(
    "failure, status, phrase",
    [
        (asr.ASRUnavailable("worker unreachable"), 503, "try again"),
        (asr.ASRRejected("unreadable container"), 422, "could not be transcribed"),
    ],
)
def test_a_failure_after_the_heartbeat_started_carries_its_status_in_the_body(
    route, monkeypatch, failure, status, phrase
):
    """The status line is already 200 by then. The failure must still be a
    failure: {'detail', 'status'} in the body, never a transcript."""
    app, rows = route
    monkeypatch.setattr(audio_api, "HEARTBEAT_S", 0.02)
    engine = _GatedEngine(raises=failure)
    asr.set_provider(engine)

    http_status, chunks = asyncio.run(_drive(app, on_body=_beats_then(engine, 1)))

    assert http_status == 200
    payload = json.loads(b"".join(chunks))
    assert payload["status"] == status
    assert phrase in payload["detail"].lower()
    assert "text" not in payload
    assert str(failure) not in payload["detail"]


def test_a_timeout_is_reported_as_a_timeout_not_as_a_broken_engine(route, monkeypatch):
    app, rows = route
    monkeypatch.setattr(audio_api, "HEARTBEAT_S", 0.02)
    engine = _GatedEngine(raises=asr.ASRTimeout("no answer within 360s"))
    asr.set_provider(engine)

    _status, chunks = asyncio.run(_drive(app, on_body=_beats_then(engine, 1)))

    payload = json.loads(b"".join(chunks))
    assert payload["status"] == 504
    assert "too long" in payload["detail"].lower()
    assert "360" not in payload["detail"]
    # V19's status vocabulary has no 'timeout'; the engine did not deliver.
    assert rows == ["unavailable"]


def test_refusals_before_any_work_keep_their_real_status(route, monkeypatch):
    """A 415, a busy pool and a quick failure are known long before the
    heartbeat would start; they are answered with their own status line."""
    app, _rows = route
    monkeypatch.setattr(audio_api, "HEARTBEAT_S", 0.05)

    engine = _GatedEngine()
    asr.set_provider(engine)
    status, chunks = asyncio.run(_drive(app, content_type=b"text/html"))
    assert status == 415 and engine.calls == 0

    busy = _GatedEngine(raises=asr.ASRBusy("every transcription slot is busy"))
    busy.go.set()
    asr.set_provider(busy)
    status, chunks = asyncio.run(_drive(app))
    assert status == 503
    assert "busy" in json.loads(b"".join(chunks))["detail"].lower()


def test_a_quick_transcript_is_an_ordinary_json_response(route, monkeypatch):
    app, _rows = route
    monkeypatch.setattr(audio_api, "HEARTBEAT_S", 5.0)
    engine = _GatedEngine()
    engine.go.set()
    asr.set_provider(engine)

    status, chunks = asyncio.run(_drive(app))

    assert status == 200
    assert chunks[0][:1] == b"{"
    assert json.loads(b"".join(chunks))["text"] == SPOKEN


def test_the_heartbeat_survives_the_real_application_and_its_middleware(login_client, monkeypatch):
    """Through main.app, signed in for real: the body-size limit, CORS and the
    cross-site guard are plain ASGI and must pass the whitespace through
    rather than buffer it."""
    monkeypatch.setattr(settings, "asr_enabled", True)
    monkeypatch.setattr(audio_api, "HEARTBEAT_S", 0.05)
    audio_api.reset_for_tests()

    class Slow(_GatedEngine):
        async def transcribe(self, audio, *, filename, content_type, language=""):
            self.go.set()
            await asyncio.sleep(0.5)
            return await super().transcribe(audio, filename=filename, content_type=content_type)

    asr.set_provider(Slow())
    try:
        bob = login_client("bob")
        response = bob.post(
            "/audio/transcribe", content=WEBM, headers={"content-type": "audio/webm"},
            params={"duration_ms": "4200", "language": "auto"},
        )
    finally:
        asr.set_provider(None)
        audio_api.reset_for_tests()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "no-transform" in response.headers["cache-control"]
    assert response.content[:1] == b" "
    assert response.json()["text"] == SPOKEN


# ---------------------------------------------------------------------------
# 3. Noisy speech is transcribed, and noise is not
# ---------------------------------------------------------------------------

#: The engine's reply when its gate fires: no decode, nothing but the number.
#: (babble at -3 dB, measured: no_speech_prob 0.8387 over 34.925 s)
GATED = {"text": "", "language": None, "language_code": None,
         "duration": 34.925, "no_speech_prob": 0.8387, "segments": []}

#: The same bytes with the gate off, verbose: 112 words over four segments.
_DENSE_TEXT = " ".join(["here she would stay comforted and soothed among the lovely plants"] * 10)
DENSE = {
    "text": _DENSE_TEXT, "language": "english", "language_code": "en",
    "duration": 34.925, "no_speech_prob": 0.0,
    "segments": [
        {"id": 0, "start": 0.0, "end": 11.54, "text": "…"},
        {"id": 1, "start": 12.06, "end": 24.70, "text": "…"},
        {"id": 2, "start": 24.70, "end": 30.04, "text": "…"},
        {"id": 3, "start": 30.22, "end": 34.90, "text": "…"},
    ],
}


def test_noisy_speech_the_gate_threw_away_is_decoded_once_more_and_kept(monkeypatch):
    wire = _Wire(monkeypatch)
    wire.scripts["w"] = [(200, GATED), (200, DENSE)]

    result = _dictate(_engine("w"))

    assert result.text == _DENSE_TEXT
    assert result.language == "English"
    assert len(wire.requests) == 2
    first, second = (fields for _host, fields in wire.requests)
    assert "no_speech_check" not in first
    assert second["no_speech_check"] == "false"
    assert second["response_format"] == "verbose_json"


def test_the_retry_happens_once_and_what_it_invents_from_silence_is_dropped(monkeypatch):
    """Measured: 10 s of digital silence, gate off, decodes as "Thank you."
    across the whole clip. The retry is bounded to one and its invention does
    not reach the composer."""
    wire = _Wire(monkeypatch)
    silent = dict(GATED, duration=10.0, no_speech_prob=0.708)
    invented = {"text": "Thank you.", "language": "english", "language_code": "en",
                "duration": 10.0, "no_speech_prob": 0.0,
                "segments": [{"id": 0, "start": 0.0, "end": 10.0, "text": "Thank you."}]}
    wire.scripts["w"] = [(200, silent), (200, invented)]

    assert _dictate(_engine("w")).text == ""
    assert len(wire.requests) == 2


def test_punctuation_alone_is_not_speech(monkeypatch):
    """Measured: pink and fan noise at -35 dBFS decode, gate off, as ". ."."""
    wire = _Wire(monkeypatch)
    dots = {"text": ". .", "language": "nynorsk", "language_code": "nn", "duration": 20.0,
            "no_speech_prob": 0.0, "segments": [{"id": 0, "start": 0.0, "end": 10.0, "text": "."},
                                                {"id": 1, "start": 10.0, "end": 20.0, "text": "."}]}
    wire.scripts["w"] = [(200, dict(GATED, duration=20.0)), (200, dots)]

    assert _dictate(_engine("w")).text == ""


def test_a_short_gated_clip_is_not_sent_again(monkeypatch):
    wire = _Wire(monkeypatch)
    wire.scripts["w"] = [(200, dict(GATED, duration=2.4))]

    assert _dictate(_engine("w")).text == ""
    assert len(wire.requests) == 1


def test_an_empty_answer_the_gate_did_not_give_is_not_sent_again(monkeypatch):
    wire = _Wire(monkeypatch)
    wire.scripts["w"] = [(200, dict(GATED, no_speech_prob=0.21))]

    assert _dictate(_engine("w")).text == ""
    assert len(wire.requests) == 1


def test_a_few_words_invented_from_long_noise_are_dropped(monkeypatch):
    """The auditor's case: 20 s of room-level pink noise passed the gate and
    came back "All right." as a draft in the composer."""
    wire = _Wire(monkeypatch)
    wire.scripts["w"] = [(200, {"text": "All right.", "language": "english", "language_code": "en",
                                "duration": 20.0, "no_speech_prob": 0.0385})]

    assert _dictate(_engine("w")).text == ""
    assert len(wire.requests) == 1


def test_video_windows_are_never_second_guessed(monkeypatch):
    """Video analysis runs its own voice-activity detection and owns its gate
    (no_speech_check); dictation's retry and check are not its business."""
    wire = _Wire(monkeypatch)
    wire.scripts["w"] = [(200, GATED)]

    result = asyncio.run(
        _engine("w").transcribe_segments(WEBM, filename="w.wav", content_type="audio/wav")
    )
    assert result.text == ""
    assert len(wire.requests) == 1


def _words(n: int) -> str:
    return " ".join(["word"] * n)


def test_the_check_rejects_two_words_from_twenty_seconds_and_accepts_dense_speech():
    assert not asr.speech_is_plausible("All right.", 20.0, engine_heard_speech=True)
    assert not asr.speech_is_plausible("All right.", 20.0, engine_heard_speech=False)
    assert asr.speech_is_plausible(_DENSE_TEXT, 34.925, DENSE["segments"], engine_heard_speech=False)
    assert asr.speech_is_plausible(SPOKEN, 6.625, engine_heard_speech=True)
    # A short answer in a short clip is an ordinary dictation, not noise.
    assert asr.speech_is_plausible("Yes.", 2.5, engine_heard_speech=True)


def test_words_are_counted_in_scripts_written_without_spaces():
    """Chinese, Japanese and Thai put no spaces between words; counted by
    whitespace a whole sentence would be ONE word and always look sparse."""
    sentence = "我明天去北京开会然后回家"
    assert asr.speech_is_plausible(
        sentence, 4.0, [{"start": 0.0, "end": 4.0, "text": sentence}], engine_heard_speech=False
    )


#: The labelled set, measured 2026-09-18 on the worker replica with public-
#: domain LibriSpeech test-clean and generated noise (see the commit message).
#: (label, words, clip seconds, worded segment spans, is speech)
GATE_OFF_VERBOSE = [
    ("pink -20 dBFS 20 s", 4, 20.0, [(0.0, 20.0)], False),
    ("digital silence 10 s", 2, 10.0, [(0.0, 10.0)], False),
    ("pink -35 dBFS 20 s", 0, 20.0, [], False),
    ("50 Hz hum + hiss 20 s", 0, 20.0, [], False),
    ("brown -20 dBFS 15 s", 4, 15.0, [(0.0, 14.98)], False),
    ("white 10 s", 0, 10.0, [], False),
    ("babble -3 dB 34.9 s", 112, 34.925, [(0.0, 11.54), (12.06, 24.70), (24.70, 30.04), (30.22, 34.90)], True),
    ("pink-noise speech 0 dB 16.9 s", 50, 16.93, [(0.0, 4.0), (4.0, 10.0), (10.0, 16.93)], True),
    ("clean 6.6 s", 18, 6.625, [(0.0, 6.08)], True),
]


@pytest.mark.parametrize("label, words, seconds, spans, speech", GATE_OFF_VERBOSE,
                         ids=[row[0] for row in GATE_OFF_VERBOSE])
def test_the_measured_set_decoded_with_the_gate_off_is_classified_correctly(label, words, seconds, spans, speech):
    segments = [{"start": a, "end": b, "text": "x"} for a, b in spans]
    assert asr.speech_is_plausible(_words(words), seconds, segments, engine_heard_speech=False) is speech


#: Decoded normally (the gate judged it speech). The ten clean clips range
#: 1.35-3.35 words per second; the sparse row is REAL speech — three words in
#: twenty seconds of quiet room — and it is dropped: indistinguishable from a
#: stock phrase by rate AND by segment (whisper stamped it 0-20 s too). That
#: is the measured price of dropping "All right." from room noise.
GATE_ON_FIRST_PASS = [
    ("clean min rate 5.9 s", 8, 5.92, True),
    ("clean max rate 7.8 s", 26, 7.75, True),
    ("babble 0 dB 34.9 s", 108, 34.925, True),
    ("auditor room noise 20 s", 2, 20.0, False),
    ("real 3 words in 20 s", 3, 20.0, False),
]


@pytest.mark.parametrize("label, words, seconds, keep", GATE_ON_FIRST_PASS,
                         ids=[row[0] for row in GATE_ON_FIRST_PASS])
def test_the_measured_first_pass_set_is_classified_as_designed(label, words, seconds, keep):
    assert asr.speech_is_plausible(_words(words), seconds, engine_heard_speech=True) is keep
