"""An audio file rides the video rail, and the video steps never name a model.

B12 (2026-09-18). A voice memo or a recorded call (.m4a, .mp3, .wav ...) is
the audio half of a video: the pipeline already analyses a file with no video
stream (the frames stage skips with "the file has no video stream"), so the
only thing standing between an audio file and a transcript was the composer
routing it to the document path. The composer now sends it with
purpose=video; this file pins the server half of that promise: the name test
agrees, the upload starts the same job behind the same gate.

B25b (2026-09-18). The "Describing frames" step said "12/12 frames described
by Qwen3-VL-8B-Instruct". The status surface promises it never names a model
(video/api.py's docstring), and the stored step text is what that surface
serves -- including for rows written before this fix.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from app import db
from app.config import settings

AUDIO_NAMES = ("memo.m4a", "call.mp3", "clip.wav", "note.ogg", "talk.opus", "take.flac", "voice.aac", "MEETING.M4A")
ROUTER_MODEL = "Qwen/Qwen3-VL-8B-Instruct"


# ------------------------------------------------------- looks_like_video --


@pytest.mark.parametrize("name", AUDIO_NAMES)
def test_an_audio_name_rides_the_video_rail(name):
    from app.video.api import looks_like_video

    assert looks_like_video(name) is True


def test_a_nameless_file_that_declares_audio_rides_the_video_rail():
    from app.video.api import looks_like_video

    assert looks_like_video("recording", "audio/mpeg") is True
    assert looks_like_video("recording", "Audio/Ogg; codecs=opus") is True


@pytest.mark.parametrize(
    "name,ctype",
    [("standup.mp4", ""), ("screen.webm", ""), ("recording", "video/mp4")],
)
def test_a_video_is_still_a_video(name, ctype):
    from app.video.api import looks_like_video

    assert looks_like_video(name, ctype) is True


@pytest.mark.parametrize(
    "name,ctype",
    [("report.pdf", "application/pdf"), ("sales.csv", "text/csv"), ("notes.txt", "text/plain"), ("deck.pptx", "")],
)
def test_a_document_is_not_media(name, ctype):
    from app.video.api import looks_like_video

    assert looks_like_video(name, ctype) is False


# ------------------------------------------------ the step names no model --


def _frames_on_disk(content_hash: str, count: int) -> list:
    from app.video import store

    os.makedirs(store.frames_dir(content_hash), exist_ok=True)
    frames = []
    for i in range(count):
        name = f"f{i:04d}.jpg"
        with open(os.path.join(store.frames_dir(content_hash), name), "wb") as fh:
            fh.write(b"\xff\xd8\xff\xd9")
        frames.append({"file": name, "t": float(i * 10), "end": float(i * 10 + 10), "phash": i, "collapsed": 1})
    with open(store.stage_path(content_hash, "frames.json"), "w", encoding="utf-8") as fh:
        json.dump({"frames": frames}, fh)
    return frames


def test_the_frame_description_step_names_no_model(monkeypatch, tmp_path):
    from app.video import pipeline, screen

    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    monkeypatch.setattr(settings, "video_captions_enabled", True)
    monkeypatch.setattr(settings, "router_model", ROUTER_MODEL)
    monkeypatch.setattr(settings, "router_base_url", "http://router.invalid/v1")
    monkeypatch.setattr(pipeline, "_router_enabled", lambda: True)

    async def fake_captions(frames, *, progress):
        return ["a slide titled Q3 plan", None, "a spreadsheet"][: len(frames)]

    monkeypatch.setattr(screen, "caption_frames", fake_captions)
    content_hash = "a" * 64
    _frames_on_disk(content_hash, 3)
    ctx = pipeline._Ctx(row={"id": 1}, content_hash=content_hash, source="")

    async def progress(percent, detail):
        return None

    result = asyncio.run(pipeline._stage_vision(ctx, progress))
    assert result.status == "done"
    assert result.detail == "2/3 frames described"
    for fragment in ("Qwen", "VL-8B", ROUTER_MODEL.split("/")[-1], " by "):
        assert fragment not in result.detail


def test_the_status_surface_hides_a_model_name_an_older_run_stored():
    """Rows finished before the fix still hold the old sentence in their
    stored stages; the surface serves it, so the surface drops the name."""
    from app.video.api import status_payload

    row = {
        "id": 987654,
        "status": "done",
        "stages": {
            "frames": {"status": "done", "ms": 10, "detail": "40 extracted → 12 distinct → 12 kept"},
            "vision": {"status": "done", "ms": 20, "detail": "11/12 frames described by Qwen3-VL-8B-Instruct"},
            "fusion": {"status": "done", "ms": 30, "detail": "meeting · 3 chapters · 2 decisions · direct"},
        },
    }
    stages = {s["stage"]: s for s in status_payload(row)["stages"]}
    assert stages["vision"]["detail"] == "11/12 frames described"
    # Everything else is served exactly as stored.
    assert stages["frames"]["detail"] == "40 extracted → 12 distinct → 12 kept"
    assert stages["fusion"]["detail"] == "meeting · 3 chapters · 2 decisions · direct"
    assert "Qwen" not in json.dumps(status_payload(row))


# ------------------------------------- the upload: same job, same gate --


def test_an_audio_upload_with_purpose_video_starts_the_same_job(monkeypatch, tmp_path, login_client):
    from app.video import pipeline, store

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path / "ws"))
    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    monkeypatch.setattr(settings, "video_analysis_enabled", True)
    monkeypatch.setattr(settings, "dataset_uploads_enabled", True)
    started = []

    async def fake_ensure_running(analysis_id):
        started.append(int(analysis_id))
        return True

    monkeypatch.setattr(pipeline, "ensure_running", fake_ensure_running)
    bob = login_client("bob")
    payload = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 4096
    resp = bob.post(
        "/uploads",
        files={"file": ("memo.m4a", payload, "audio/mp4")},
        data={"conversation_id": "conv-audio-1", "purpose": "video"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["filename"] == "memo.m4a"
    assert body["video"]["status"] == "queued"
    assert started == [body["video"]["analysis_id"]]
    row = db.get_video_analysis(body["video"]["analysis_id"])
    assert row["media_type"] == "audio/mp4"
    # The bytes were adopted into the analysis store, outside the workspace sweep.
    assert store.source_path(row["content_hash"])


def test_an_audio_upload_meets_the_video_gate(monkeypatch, tmp_path, login_client):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path / "ws"))
    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    monkeypatch.setattr(settings, "dataset_uploads_enabled", True)
    monkeypatch.setattr(settings, "video_analysis_enabled", False)
    bob = login_client("bob")
    resp = bob.post(
        "/uploads",
        files={"file": ("memo.m4a", b"\x00" * 2048, "audio/mp4")},
        data={"conversation_id": "conv-audio-2", "purpose": "video"},
    )
    assert resp.status_code == 404
    assert not os.path.isdir(os.path.join(settings.workspace_dir, "uploads", "conv-audio-2"))
