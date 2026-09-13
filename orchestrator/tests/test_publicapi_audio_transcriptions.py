"""`POST /v1/audio/transcriptions` over real HTTP, real keys and a real database.

The speech replicas are an `httpx.MockTransport` that parses the multipart
upload the way compose/whisper/server.py does and answers its reply shape
(text, language, language_code, duration, no_speech_prob, processing_ms,
segments). The multipart reader, the caps, the gate, the replica choice and
the ledger are the production code.
"""
from __future__ import annotations

import tempfile

import httpx
import pytest

from app.config import settings
from app.publicapi import capacity, multipart, sidecars
from tests.publicapi_sidecar_support import (  # noqa: F401 - fixtures by name
    ASR_URLS,
    _pepper,
    api,
    assert_nothing_internal,
    auth,
    daily,
    engines_configured,
    install_transport,
    platform,
    usage_events,
)

URL = "/v1/audio/transcriptions"
AUDIO = b"RIFF" + b"\x01\x02" * 4000


def _whisper(*, duration=12.3, status=200, text=" Hello there. ", segments=None):
    """A speech replica that records the form it was sent."""
    seen = {}

    async def handler(request, body):
        async def one():
            yield body

        form = await multipart.read_form(
            one(), request.headers["content-type"], max_body_bytes=10**8, max_file_bytes=10**8
        )
        seen.setdefault("forms", []).append(form)
        seen.setdefault("urls", []).append(str(request.url))
        if status != 200:
            return httpx.Response(status, json={"detail": "could not decode audio: [mp3 @ 0x55] Header missing /tmp/x"})
        return httpx.Response(
            200,
            json={
                "text": text,
                "language": "english",
                "language_code": "en",
                "duration": duration,
                "no_speech_prob": 0.01,
                "processing_ms": 1234,
                "segments": segments
                if segments is not None
                else [
                    {"id": 0, "start": 0.0, "end": 2.5, "text": "Hello", "language": "en"},
                    {"id": 3, "start": 2.5, "end": 4.0, "text": "there.", "language": "en"},
                ],
                "task": "transcribe",
            },
        )

    return handler, seen


def _post(api, headers=None, *, fields=None, file=("clip.wav", AUDIO, "audio/wav")):
    data = {"model": "techsara-whisper"}
    data.update(fields or {})
    headers = headers if headers is not None else auth()
    if file is None:
        # Still multipart/form-data — only the file part is missing.
        parts = [(name, (None, value)) for name, value in data.items()]
        return api.post(URL, files=parts, headers=headers)
    return api.post(URL, data=data, files={"file": file}, headers=headers)


# ------------------------------------------------------------- the answer --


def test_a_clip_is_transcribed_as_json_with_duration_usage_and_nothing_internal(api):
    handler, seen = _whisper()
    install_transport(handler)

    response = _post(api)

    assert response.status_code == 200, response.text
    assert response.json() == {"text": "Hello there.", "usage": {"type": "duration", "seconds": 13}}
    assert_nothing_internal(response)
    form = seen["forms"][0]
    assert bytes(form.file("file").data) == AUDIO
    assert form.file("file").content_type == "audio/wav"
    assert form.field_value("response_format") == "json"
    # Auto-detection: no language is forced unless the caller forces one.
    assert form.field_value("language") is None
    # The caller's filename never travels.
    assert "clip.wav" not in form.file("file").filename


def test_text_format_is_a_plain_text_body(api):
    handler, _ = _whisper()
    install_transport(handler)

    response = _post(api, fields={"response_format": "text"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "Hello there."


def test_verbose_json_carries_segments_and_the_language_name_and_no_engine_internals(api):
    handler, seen = _whisper()
    install_transport(handler)

    response = _post(
        api, fields={"response_format": "verbose_json", "timestamp_granularities[]": "segment"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "task": "transcribe",
        "language": "english",
        "duration": 12.3,
        "text": "Hello there.",
        "segments": [
            {"id": 0, "start": 0.0, "end": 2.5, "text": "Hello"},
            {"id": 1, "start": 2.5, "end": 4.0, "text": "there."},
        ],
        "usage": {"type": "duration", "seconds": 13},
    }
    assert "processing_ms" not in response.text and "no_speech_prob" not in response.text
    assert seen["forms"][0].field_value("response_format") == "verbose_json"


def test_a_forced_language_is_passed_to_the_engine(api):
    handler, seen = _whisper()
    install_transport(handler)

    response = _post(api, fields={"language": "HI"})

    assert response.status_code == 200
    assert seen["forms"][0].field_value("language") == "hi"


def test_a_public_clip_goes_to_the_replica_dictation_reaches_for_last(api):
    handler, seen = _whisper()
    install_transport(handler)

    _post(api)

    assert seen["urls"] == [f"{ASR_URLS[-1]}/audio/transcriptions"]


def test_the_audio_is_never_spooled_to_a_temporary_file(api, monkeypatch):
    """Starlette's UploadFile rolls a part over 1 MB onto disk; this route
    promises memory only (app/audio_api.py's rule, kept on /v1)."""

    def refuse(*args, **kwargs):
        raise AssertionError("a temporary file was created for the upload")

    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", refuse)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", refuse)
    monkeypatch.setattr(tempfile, "TemporaryFile", refuse)
    # Starlette binds the name at import; the multipart form parser is the
    # component that would spool, so its own reference is the one to refuse.
    import starlette.formparsers

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", refuse)
    handler, _ = _whisper()
    install_transport(handler)

    response = _post(api, file=("big.mp3", b"\xff\xfb" * (2 * 1024 * 1024), "audio/mpeg"))

    assert response.status_code == 200


# ------------------------------------------------------ who may call it --


def test_a_request_without_a_key_is_a_401_and_never_reaches_the_engine(api):
    handler, seen = _whisper()
    install_transport(handler)

    response = _post(api, headers={})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert "forms" not in seen


def test_a_key_without_audio_write_is_a_403_naming_the_scope(api):
    handler, seen = _whisper()
    install_transport(handler)

    response = _post(api, headers=auth("narrow"))

    assert response.status_code == 403
    assert response.headers["WWW-Authenticate"] == 'Bearer error="insufficient_scope", scope="audio.write"'
    assert "forms" not in seen


def test_a_deployment_without_speech_does_not_offer_the_model(api, monkeypatch):
    monkeypatch.setattr(settings, "asr_enabled", False)
    handler, seen = _whisper()
    install_transport(handler)

    response = _post(api)

    assert response.status_code == 404
    assert "forms" not in seen


# ------------------------------------------------------------ the body --


def test_a_file_over_the_audio_cap_is_a_413_and_never_reaches_the_engine(api, monkeypatch):
    monkeypatch.setattr(settings, "public_api_max_audio_bytes", 4096, raising=False)
    handler, seen = _whisper()
    install_transport(handler)

    response = _post(api, file=("a.wav", b"\x00" * 5000, "audio/wav"))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert "forms" not in seen


def test_a_declared_body_over_the_body_cap_is_refused_before_it_is_read(api, monkeypatch):
    monkeypatch.setattr(settings, "public_api_max_audio_body_bytes", 1024, raising=False)
    handler, seen = _whisper()
    install_transport(handler)

    response = _post(api)

    assert response.status_code == 413
    assert "forms" not in seen


@pytest.mark.parametrize(
    "fields, file, param",
    [
        ({"prompt": "names: Sara"}, ("a.wav", AUDIO, "audio/wav"), "prompt"),
        ({"temperature": "0.2"}, ("a.wav", AUDIO, "audio/wav"), "temperature"),
        ({"response_format": "srt"}, ("a.wav", AUDIO, "audio/wav"), "response_format"),
        ({"language": "klingon"}, ("a.wav", AUDIO, "audio/wav"), "language"),
        ({}, ("a.txt", b"hello", "text/plain"), "file"),
        ({}, ("a.wav", b"", "audio/wav"), "file"),
        ({}, None, "file"),
    ],
)
def test_a_form_the_engine_cannot_honour_is_a_400_naming_the_field(api, fields, file, param):
    handler, seen = _whisper()
    install_transport(handler)

    response = _post(api, fields=fields, file=file)

    assert response.status_code == 400, response.text
    assert response.json()["error"]["param"] == param
    assert "forms" not in seen


def test_a_json_body_is_refused_because_the_endpoint_is_multipart(api):
    response = api.post(URL, json={"model": "techsara-whisper"}, headers=auth())

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "Content-Type"


def test_an_idempotency_key_is_refused_before_the_upload_is_read(api):
    handler, seen = _whisper()
    install_transport(handler)

    response = _post(api, headers={**auth(), "Idempotency-Key": "k-1"})

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "Idempotency-Key"
    assert "forms" not in seen


def test_audio_the_probe_measures_over_the_public_limit_never_reaches_the_gpu(api, monkeypatch):
    async def long_clip(audio, **_kwargs):
        return 450.0

    monkeypatch.setattr(sidecars, "probe_seconds", long_clip)
    handler, seen = _whisper()
    install_transport(handler)

    response = _post(api)

    assert response.status_code == 413
    assert "300 second" in response.json()["error"]["message"]
    assert "forms" not in seen


def test_audio_the_engine_measures_over_the_public_limit_is_refused_afterwards(api, platform):
    handler, _ = _whisper(duration=412.0)
    install_transport(handler)

    response = _post(api)

    assert response.status_code == 413
    assert response.json()["error"]["param"] == "file"
    # The GPU did the work, so it is an error on the ledger, not a no-op.
    assert daily(platform["project"]["id"])["errors"] == 1


def test_the_engines_own_413_is_the_same_refusal(api):
    handler, _ = _whisper(status=413)
    install_transport(handler)

    response = _post(api)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_undecodable_audio_is_a_400_with_a_fixed_sentence_never_the_decoder_output(api):
    handler, _ = _whisper(status=400)
    install_transport(handler)

    response = _post(api)

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "The audio could not be decoded. Send a supported audio file."
    assert "mp3 @" not in response.text and "/tmp" not in response.text


# ---------------------------------------------------------- the engine --


def test_every_replica_down_is_a_503_with_retry_after_that_names_no_host(api, platform):
    def down(request, body):
        raise httpx.ConnectError(f"connection refused: {request.url}")

    recorded = install_transport(down)

    response = _post(api)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_unavailable"
    assert int(response.headers["Retry-After"]) >= 1
    assert_nothing_internal(response)
    assert recorded.calls == 2
    rows = usage_events("v1_audio_transcriptions")
    assert [(row["status"], row["input_tokens"], row["output_tokens"]) for row in rows] == [("error", None, None)]


def test_an_unreachable_replica_falls_over_to_the_other(api):
    handler, seen = _whisper()

    async def first_down(request, body):
        if str(request.url).startswith(ASR_URLS[-1]):
            raise httpx.ConnectError("refused")
        return await handler(request, body)

    install_transport(first_down)

    response = _post(api)

    assert response.status_code == 200
    assert seen["urls"] == [f"{ASR_URLS[0]}/audio/transcriptions"]


def test_a_replica_that_times_out_is_a_504_and_is_not_retried_on_the_other_gpu(api):
    def slow(request, body):
        raise httpx.ReadTimeout("timed out")

    recorded = install_transport(slow)

    response = _post(api)

    assert response.status_code == 504
    assert recorded.calls == 1


def test_the_speech_gate_is_taken_fleet_wide_and_yields_to_chat(api, monkeypatch):
    handler, _ = _whisper()
    install_transport(handler)
    taken = []
    real_hold = capacity.hold

    def recording_hold(gate, **kwargs):
        taken.append((gate, kwargs.get("yield_to_chat")))
        return real_hold(gate, **kwargs)

    monkeypatch.setattr(capacity, "hold", recording_hold)

    response = _post(api)

    assert response.status_code == 200
    assert taken == [("asr", True)]


# ------------------------------------------------------------ metering --


def test_a_transcription_is_one_usage_row_with_seconds_and_no_invented_tokens(api, platform):
    handler, _ = _whisper()
    install_transport(handler)

    response = _post(api, fields={"language": "en"})

    assert response.status_code == 200
    rows = usage_events("v1_audio_transcriptions")
    assert len(rows) == 1
    row = rows[0]
    assert (row["status"], row["model"], row["input_tokens"], row["output_tokens"]) == (
        "ok",
        "techsara-whisper",
        None,
        None,
    )
    assert row["generation_id"].startswith("asr_")
    assert row["meta"]["audio_seconds"] == 12.3
    assert row["meta"]["processing_ms"] == 1234
    assert row["meta"]["language_forced"] is True
    assert row["meta"]["response_format"] == "json"
    assert daily(platform["project"]["id"]) == {
        "requests": 1,
        "input_tokens": 0,
        "output_tokens": 0,
        "errors": 0,
    }


# ------------------------------------------------------------- the probe --


def _fake_ffprobe(tmp_path, monkeypatch, script: str) -> None:
    """An `ffprobe` on PATH that is a shell script: the probe's plumbing (all
    the audio fed through stdin, the answer parsed off stdout) is what is under
    test, not ffmpeg's demuxers. Nothing it reads is written anywhere."""
    import os
    import stat

    binary = tmp_path / "ffprobe"
    binary.write_text("#!/bin/sh\n" + script + "\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")


def test_the_probe_feeds_the_whole_clip_through_stdin_and_reads_the_duration(tmp_path, monkeypatch):
    import asyncio

    from app.publicapi.sidecars import probe_seconds as real_probe

    # The "duration" it prints is the number of bytes it was given, so a probe
    # that stopped feeding after the first slice reads as the wrong number.
    _fake_ffprobe(tmp_path, monkeypatch, "wc -c | tr -d ' '")
    audio = bytearray(b"\x07" * (1024 * 1024 + 17))

    assert asyncio.run(real_probe(audio)) == float(len(audio))


def test_a_probe_that_stops_reading_early_or_knows_no_duration_is_not_an_error(tmp_path, monkeypatch):
    import asyncio

    from app.publicapi.sidecars import probe_seconds as real_probe

    _fake_ffprobe(tmp_path, monkeypatch, "echo 7.5")
    assert asyncio.run(real_probe(bytearray(b"\x00" * (4 * 1024 * 1024)))) == 7.5

    _fake_ffprobe(tmp_path, monkeypatch, "cat > /dev/null; echo N/A")
    assert asyncio.run(real_probe(bytearray(b"\x00" * 1000))) is None
