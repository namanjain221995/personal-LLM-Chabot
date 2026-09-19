"""QA round 1 (B12 / B25b, 2026-09-18): the seams the builder's tests leave open.

* looks_like_video must not claim documents whose names merely CONTAIN an
  audio word, nor double extensions that end in a document suffix.
* The status surface strips only the old model-naming sentence: every other
  detail, on every stage, is served exactly as stored, and a running vision
  stage's live detail carries no model name either.
* A real audio file with embedded cover art (mp3 APIC, m4a covr, FLAC
  picture) is audio-only to the probe, so the frames stage skips honestly
  rather than describing the album cover as a video.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess

import pytest

from app.video import api as video_api


@pytest.mark.parametrize(
    "name,ctype",
    [
        ("m4a.txt", "text/plain"),
        ("memo.m4a.pdf", "application/pdf"),
        ("song.mp3.zip", "application/zip"),
        ("wav_notes.md", ""),
        ("mp3", ""),
        ("", ""),
        ("report.pdf", "application/octet-stream"),
        ("sales.csv", "application/vnd.ms-excel"),
    ],
)
def test_names_that_merely_mention_audio_are_not_media(name, ctype):
    assert video_api.looks_like_video(name, ctype) is False


@pytest.mark.parametrize(
    "name,ctype",
    [
        ("اجتماع الفريق.m4a", ""),
        ("会议记录.OPUS", ""),
        ("voice.ogg", "application/ogg"),
        ("call.m4a", "application/octet-stream"),
        ("recording", "AUDIO/WEBM;codecs=opus"),
    ],
)
def test_audio_from_the_wild_is_media(name, ctype):
    assert video_api.looks_like_video(name, ctype) is True


@pytest.mark.parametrize(
    "stage,detail",
    [
        ("ocr", "5/12 frames had readable text"),
        ("transcript", "4 segments · 2 clip(s) · en"),
        ("frames", "the file has no video stream"),
        ("vision", "the vision model described none of the frames"),
        ("vision", "frame captions are not enabled"),
        ("vision", "no frames to describe"),
        ("vision", "12/12 frames described"),
        ("fusion", "lecture · 2 chapters · 0 decisions · direct"),
        # Only the vision stage's one sentence is rewritten.
        ("ocr", "3/4 frames described by someone"),
    ],
)
def test_every_other_detail_is_served_exactly_as_stored(stage, detail):
    row = {"id": 424242, "status": "done", "stages": {stage: {"status": "done", "ms": 1, "detail": detail}}}
    served = {s["stage"]: s for s in video_api.status_payload(row)["stages"]}
    assert served[stage]["detail"] == detail


@pytest.mark.parametrize(
    "stored",
    [
        "11/12 frames described by Qwen3-VL-8B-Instruct",
        "11/12 frames described by Qwen3-VL-8B-Instruct-FP8",
        "0/0 frames described by Qwen/Qwen3-VL-8B-Instruct-FP8",
        "1234/5678 frames described by some-other-router_v2.1",
    ],
)
def test_every_stored_model_sentence_shape_is_stripped(stored):
    row = {"id": 434343, "status": "done", "stages": {"vision": {"status": "done", "ms": 1, "detail": stored}}}
    served = {s["stage"]: s for s in video_api.status_payload(row)["stages"]}
    assert served["vision"]["detail"] == stored.split(" by ")[0]
    assert " by " not in served["vision"]["detail"]


def test_a_running_vision_stage_serves_its_live_detail_without_a_model(monkeypatch):
    from app.video import pipeline

    monkeypatch.setitem(pipeline._latest, 454545, {"stage": "vision", "status": "running", "percent": 40.0, "detail": "4/10 frames", "elapsed_s": 3.0})
    row = {"id": 454545, "status": "running", "stages": {"vision": {"status": "pending"}}}
    served = {s["stage"]: s for s in video_api.status_payload(row)["stages"]}
    assert served["vision"]["status"] == "running"
    assert served["vision"]["detail"] == "4/10 frames"


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, timeout=60)


@pytest.mark.parametrize(
    "suffix,codec_args",
    [
        (".mp3", ["-c:a", "libmp3lame", "-c:v", "mjpeg", "-id3v2_version", "3"]),
        (".m4a", ["-c:a", "aac", "-c:v", "png"]),
        (".flac", ["-c:a", "flac", "-c:v", "png"]),
    ],
)
def test_cover_art_is_not_a_video_stream_so_frames_skip(tmp_path, monkeypatch, suffix, codec_args):
    from app.config import settings
    from app.video import media, pipeline

    if not media.tools_available() or shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed here")
    tone = str(tmp_path / "tone.wav")
    cover = str(tmp_path / "cover.png")
    out = str(tmp_path / f"song{suffix}")
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=3", tone)
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=64x64:d=1", "-frames:v", "1", cover)
    _ffmpeg("-i", tone, "-i", cover, "-map", "0:a", "-map", "1:v", *codec_args, "-disposition:v", "attached_pic", out)

    probe = asyncio.run(media.probe(out))
    assert probe.has_audio is True
    assert probe.has_video is False, "an album cover was taken for a video stream"
    # The raw streams DO include the picture: the skip is a decision, not luck.
    assert any(s.get("codec_type") == "video" for s in probe.raw["streams"])

    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    content_hash = "b" * 64
    os.makedirs(os.path.dirname(pipeline.store.stage_path(content_hash, "probe.json")), exist_ok=True)
    ctx = pipeline._Ctx(row={"id": 1}, content_hash=content_hash, source=out)
    ctx.probe = {**probe.summary(), "raw": probe.raw}

    async def progress(percent, detail):
        return None

    result = asyncio.run(pipeline._stage_frames(ctx, progress))
    assert (result.status, result.detail) == ("skipped", "the file has no video stream")
