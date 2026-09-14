"""POST /v1/audio/transcriptions through the SDK (CONTRACT-3 §8.6). Planned 2026-09-13.

The clip is a generated two-second tone, so the assertions are about SHAPE:
a silence/no-speech gate may legitimately return empty text for it."""
from __future__ import annotations

import openai
import pytest

from techsara_conformance import asserts, media

pytestmark = pytest.mark.feature("audio_transcriptions")


@pytest.fixture(scope="module")
def clip() -> bytes:
    return media.tone_wav(seconds=2.0)


def test_json_transcription_returns_text_and_duration_usage(client, target, clip):
    result = client.audio.transcriptions.create(model=target.models["whisper"], file=("tone.wav", clip, "audio/wav"))
    assert isinstance(result.text, str)
    usage = result.model_dump().get("usage") or {}
    # ceil() of the engine's measured duration: a decoder may report 2.0 or a
    # hair over it for an exactly two-second clip, so 2 or 3.
    assert usage.get("type") == "duration" and usage.get("seconds") in (2, 3), f"usage {usage!r} (§8.6)"


def test_text_response_format_is_a_plain_text_body(client, target, clip):
    result = client.audio.transcriptions.create(
        model=target.models["whisper"], file=("tone.wav", clip, "audio/wav"), response_format="text"
    )
    assert isinstance(result, str)


def test_verbose_json_carries_segments_duration_and_task(client, target, clip):
    result = client.audio.transcriptions.create(
        model=target.models["whisper"], file=("tone.wav", clip, "audio/wav"), response_format="verbose_json",
        timestamp_granularities=["segment"],
    )
    wire = result.model_dump()
    assert wire.get("task") == "transcribe"
    assert abs(float(wire["duration"]) - 2.0) < 0.2, wire["duration"]
    assert isinstance(wire.get("segments"), list)


@pytest.mark.parametrize("fmt", ["srt", "vtt"])
def test_subtitle_formats_are_refused(client, target, clip, fmt):
    with pytest.raises(openai.BadRequestError) as caught:
        client.audio.transcriptions.create(model=target.models["whisper"], file=("tone.wav", clip, "audio/wav"), response_format=fmt)
    asserts.sdk_error(caught.value, code="invalid_request_error")


def test_a_field_the_platform_cannot_honour_is_refused_by_name(client, target, clip):
    with pytest.raises(openai.BadRequestError) as caught:
        client.audio.transcriptions.create(model=target.models["whisper"], file=("tone.wav", clip, "audio/wav"), temperature=0.3)
    asserts.sdk_error(caught.value, code="invalid_request_error", param="temperature")


def test_an_undecodable_file_is_a_400_with_no_decoder_output(client, target):
    with pytest.raises(openai.BadRequestError) as caught:
        client.audio.transcriptions.create(model=target.models["whisper"], file=("broken.wav", b"RIFF\x00\x00not audio at all", "audio/wav"))
    error = asserts.sdk_error(caught.value, code="invalid_request_error")
    for leak in ("ffmpeg", "Invalid data found", "stderr"):
        assert leak not in error["message"], error["message"]
