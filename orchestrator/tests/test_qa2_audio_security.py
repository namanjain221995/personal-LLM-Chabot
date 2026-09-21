"""QA2 round 1, security lens, for B12/B25b (audio on the video rail).

Three things the builder's tests do not look at:

1. The chat-lane analysis row is keyed by the SHA-256 of the bytes alone, and
   the FIRST uploader's filename is stored on it. Every later uploader of the
   same bytes gets the same row, and the fusion prompt ("File: <name>") and
   the artifact titles are built from that stored name, not from theirs.
2. The status surface promises "never engine names", but a stage that failed
   or was deferred stores `str(exc)`, and ModelUnavailable's text carries the
   engine's base URL. B25b strips one sentence shape only.
3. The upload keeps the uploader's extension on the stored source, so a
   playlist attached with purpose=video is stored as `source.m3u`.
"""
from __future__ import annotations

import asyncio
import json
import os

import httpx

from app import db
from app.config import settings

PAYLOAD = b"\x00\x00\x00\x20ftypM4A " + b"\x01" * 8192


def _video_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path / "ws"))
    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    monkeypatch.setattr(settings, "video_analysis_enabled", True)
    monkeypatch.setattr(settings, "dataset_uploads_enabled", True)
    from app.video import pipeline

    async def no_run(analysis_id):
        return False

    monkeypatch.setattr(pipeline, "ensure_running", no_run)


def test_a_second_uploader_of_the_same_audio_is_analysed_under_the_first_uploaders_name(
    monkeypatch, tmp_path, login_client
):
    _video_env(monkeypatch, tmp_path)
    alice = login_client("alice")
    bob = login_client("bob")
    first = alice.post(
        "/uploads",
        files={"file": ("Rahul Mehta disciplinary call.m4a", PAYLOAD, "audio/mp4")},
        data={"conversation_id": "conv-alice-1", "purpose": "video"},
    )
    assert first.status_code == 200, first.text
    second = bob.post(
        "/uploads",
        files={"file": ("memo.m4a", PAYLOAD, "audio/mp4")},
        data={"conversation_id": "conv-bob-1", "purpose": "video"},
    )
    assert second.status_code == 200, second.text
    aid = second.json()["video"]["analysis_id"]
    # One row for both people.
    assert aid == first.json()["video"]["analysis_id"]

    from app.video import fusion, pipeline, store

    row = db.get_video_analysis(aid)
    seen = {}

    async def capture(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(fusion, "understand", capture)
    ctx = pipeline._Ctx(row=row, content_hash=row["content_hash"], source=store.source_path(row["content_hash"]) or "")
    monkeypatch.setattr(ctx, "load_probe", lambda: {"duration_s": 30.0, "has_audio": True})
    monkeypatch.setattr(ctx, "load_transcript", lambda: [])
    monkeypatch.setattr(ctx, "load_spans", lambda: [])

    async def progress(percent, detail):
        return None

    try:
        asyncio.run(pipeline._stage_fusion(ctx, progress))
    except RuntimeError:
        pass
    # Bob's summary is written from a prompt that names Alice's file.
    assert "Rahul" not in str(seen.get("filename")), seen.get("filename")


def test_the_status_surface_names_no_engine_address_when_a_stage_waits_for_the_model():
    from app.resilience import ModelUnavailable
    from app.video import pipeline
    from app.video.api import status_payload

    exc = ModelUnavailable("http://10.100.184.2:8000/v1", 780.0, 6, httpx.ConnectError("All connection attempts failed"))
    detail = f"{pipeline.DEFERRED_MARK}: {str(exc)[:300]}"  # what _Runner.stage stores
    row = {
        "id": 424242,
        "status": "queued",
        "error": f"{pipeline.DEFERRED_MARK} (attempt 1): Summarising: {detail}"[:1000],
        "stages": {
            "transcript": {"status": "done", "ms": 10, "detail": "4 segments · 2 clip(s) · en"},
            "fusion": {"status": "deferred", "ms": 780000, "detail": detail},
        },
    }
    wire = json.dumps(status_payload(row))
    assert "10.100.184.2" not in wire and "http://" not in wire, wire


def test_a_playlist_sent_as_video_is_not_stored_under_an_hls_extension(monkeypatch, tmp_path, login_client):
    """ffmpeg 6.1.1 demuxes a file named *.m3u/*.m3u8/*.hls as HLS and opens
    the local media paths it lists (measured 2026-09-18 in the production
    ffmpeg build); a file named anything else is not probed as HLS."""
    _video_env(monkeypatch, tmp_path)
    carol = login_client("carol")
    playlist = b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:3,\n/data/video/x/audio.wav\n#EXT-X-ENDLIST\n"
    resp = carol.post(
        "/uploads",
        files={"file": ("radio.m3u", playlist, "audio/mpegurl")},
        data={"conversation_id": "conv-carol-1", "purpose": "video"},
    )
    if resp.status_code != 200:
        return  # refused at the door: fine
    from app.video import store

    row = db.get_video_analysis(resp.json()["video"]["analysis_id"])
    source = store.source_path(row["content_hash"]) or ""
    assert os.path.splitext(source)[1].lower() not in (".m3u", ".m3u8", ".hls"), source


def test_looks_like_video_does_not_claim_a_playlist():
    from app.video.api import looks_like_video

    assert looks_like_video("radio.m3u", "audio/mpegurl") is False
    assert looks_like_video("radio.m3u", "audio/x-mpegurl") is False
