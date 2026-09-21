"""QA round 1 (B12, 2026-09-18): an audio-only file is never labelled a screen recording.

Measured live: the fusion model labelled a 36 s speech .mp3 and a 38 s speech
.wav (Gettysburg Address, no video stream) `screen_recording` in 7 of 12
fusion passes, and the overview the person reads then says
"0:36 · screen recording · en" beside a frames step that says "the file has no
video stream". The fusion header never tells the model there is no picture.
"""
from __future__ import annotations

import asyncio

import pytest

from app.video import fusion, pipeline
from app.video.types import Understanding


@pytest.mark.parametrize("has_video,expected", [(False, "other"), (True, "screen_recording")])
def test_a_file_with_no_video_stream_is_not_a_screen_recording(monkeypatch, tmp_path, has_video, expected):
    from app.config import settings

    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))

    async def fake_understand(**kwargs):
        return Understanding(content_type="screen_recording", summary="A speech.", method="direct")

    monkeypatch.setattr(fusion, "understand", fake_understand)
    content_hash = "c" * 64
    import os

    os.makedirs(os.path.dirname(pipeline.store.stage_path(content_hash, "x")), exist_ok=True)
    ctx = pipeline._Ctx(row={"id": 1, "filename": "memo.m4a"}, content_hash=content_hash, source="")
    ctx.probe = {"duration_s": 36.0, "has_audio": True, "has_video": has_video}
    ctx.segments, ctx.spans = [], []

    async def progress(percent, detail):
        return None

    result = asyncio.run(pipeline._stage_fusion(ctx, progress))
    assert result.detail.split(" · ")[0] == expected
    assert ctx.understanding.content_type == expected
