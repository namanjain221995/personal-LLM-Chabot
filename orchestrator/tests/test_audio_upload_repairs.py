"""B12 / B25b repair round (2026-09-18): what QA found once audio rode the video rail.

* A row is keyed by the bytes alone and shared by everyone who attaches them;
  its stored filename is the FIRST uploader's. Fusion's prompt and the
  artifact titles were built from it, so a second person's summary could
  discuss the first person's name for the file. The shared outputs now use a
  neutral name, and a version bump re-runs fusion and the artifacts (only)
  for rows written before.
* A file with no picture is never labelled a screen recording.
* A failed or deferred stage stored `str(exc)`, and ModelUnavailable's text
  carries the engine's base URL; the stage detail, the row's `error` and the
  published progress event now carry none.
* A playlist or a MIDI score is not a recording, and the stored source keeps
  only a known media extension (ffmpeg demuxes a file NAMED .m3u/.m3u8 as
  HLS and opens the local paths it lists).
* The duration and size refusals say "recording" for audio.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess

import httpx
import openai
import pytest
from fastapi import HTTPException

from app import db, resilience
from app.config import settings

FIRST_NAME = "Rahul Mehta disciplinary hearing.m4a"
FIRST_TOKENS = ("rahul", "mehta", "disciplinary", "hearing")
ENGINE = "http://10.100.184.2:8000/v1"


def _progress():
    async def progress(percent, detail):
        return None

    return progress


def _seed_stage_files(content_hash: str, *, has_video: bool) -> None:
    from app.video import store

    os.makedirs(store.analysis_dir(content_hash), exist_ok=True)
    store.write_json(
        store.stage_path(content_hash, "probe.json"),
        {"duration_s": 36.0, "has_audio": True, "has_video": has_video},
    )
    store.write_json(
        store.stage_path(content_hash, "transcript.json"),
        {
            "language": "en",
            "segments": [
                {"start": 2.1, "end": 9.0, "text": "This is a LibriVox recording.", "language": "en"},
                {"start": 9.0, "end": 32.5, "text": "The Gettysburg Address, by Abraham Lincoln.", "language": "en"},
            ],
            "report": {"speech_fraction": 0.8},
        },
    )
    store.write_json(store.stage_path(content_hash, "screen.json"), {"spans": []})


# --------------------------------------------- the first uploader's name --


def test_fusion_and_the_artifacts_never_see_the_stored_name_of_a_shared_row(monkeypatch, tmp_path):
    from app.video import fusion, pipeline, store
    from app.video.types import Understanding

    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    content_hash = "d" * 64
    _seed_stage_files(content_hash, has_video=False)
    told = {}

    async def fake_understand(**kwargs):
        told.update(kwargs)
        return Understanding(content_type="lecture", summary="The Gettysburg Address is read aloud.", method="direct")

    monkeypatch.setattr(fusion, "understand", fake_understand)
    row = {"id": 1, "filename": FIRST_NAME, "lane": "chat"}
    ctx = pipeline._Ctx(row=row, content_hash=content_hash, source="")
    asyncio.run(pipeline._stage_fusion(ctx, _progress()))
    assert told["filename"] == "recording.m4a"

    result = asyncio.run(pipeline._stage_artifacts(ctx, _progress()))
    assert result.status == "done"
    written = {a["filename"] for a in result.row_fields["artifacts"]}
    assert {"transcript.txt", "transcript.json", "summary.md"} <= written
    for name in written:
        with open(os.path.join(store.artifacts_dir(content_hash), name), encoding="utf-8") as fh:
            body = fh.read().lower()
        assert not [t for t in FIRST_TOKENS if t in body], name
    with open(os.path.join(store.artifacts_dir(content_hash), "summary.md"), encoding="utf-8") as fh:
        assert "recording.m4a" in fh.read()


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"filename": FIRST_NAME, "lane": "chat"}, "recording.m4a"),
        ({"filename": "Standup.MP4"}, "recording.mp4"),  # a pre-V37 row has no lane: chat
        ({"filename": "no extension"}, "recording"),
        ({"filename": "weird.<script>"}, "recording"),
        ({"filename": ""}, "recording"),
        # An api-lane row is keyed by project AND bytes: its name is that
        # project's own, and the Files API has always titled with it.
        ({"filename": "board-meeting.mp4", "lane": "api"}, "board-meeting.mp4"),
    ],
)
def test_the_shared_name(row, expected):
    from app.video import pipeline

    assert pipeline._shared_name(row) == expected


def test_a_version_4_bump_reruns_fusion_and_the_artifacts_only():
    from app.video import pipeline

    assert pipeline.PIPELINE_VERSION == 4
    assert pipeline.stages_to_rerun(3) == {"fusion", "artifacts"}
    # v2 already re-ran these four; v4 adds nothing new for it.
    assert pipeline.stages_to_rerun(2) == {"transcript", "fusion", "index", "artifacts"}


def test_a_v3_audio_row_loses_its_old_name_and_label_on_the_next_attach(monkeypatch, tmp_path):
    """A row written before this fix holds a summary built from the first
    uploader's name, and for audio possibly the screen_recording label. The
    next attach re-runs fusion and the artifacts from the files on disk and
    asks no speech or screen engine again."""
    from app.video import fusion, pipeline, store
    from app.video.types import Understanding

    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    content_hash = "9" * 64
    src = tmp_path / "memo.m4a"
    src.write_bytes(b"\x00" * 64)
    os.makedirs(store.analysis_dir(content_hash), exist_ok=True)
    store.adopt_source(content_hash, str(src), "memo.m4a")
    _seed_stage_files(content_hash, has_video=False)
    row = db.upsert_video_analysis(content_hash, 64, "audio/mp4", FIRST_NAME)
    for stage in pipeline.STAGES:
        output = pipeline._OUTPUTS.get(stage)
        if output and not os.path.exists(store.stage_path(content_hash, output)):
            store.write_json(store.stage_path(content_hash, output), {"old": True})
    old = {"content_type": "screen_recording", "summary": f"The file name '{FIRST_NAME}' implies a hearing."}
    store.write_json(store.stage_path(content_hash, "understanding.json"), old)
    db.update_video_analysis(
        row["id"], status="done", pipeline_version=3,
        stages={s: {"status": "done", "ms": 1, "detail": "old"} for s in pipeline.STAGES},
        understanding=old, summary=old["summary"],
    )

    ran: list = []
    told = {}

    async def fake_understand(**kwargs):
        told.update(kwargs)
        return Understanding(content_type="screen_recording", summary="A speech is read aloud.", method="direct")

    monkeypatch.setattr(fusion, "understand", fake_understand)
    real = dict(pipeline._STAGE_FNS)

    def wrap(name):
        async def run(ctx, progress):
            ran.append(name)
            if name in ("fusion", "artifacts"):
                return await real[name](ctx, progress)
            raise AssertionError(f"{name} was asked again; v4 re-runs fusion and the artifacts only")
        return run

    monkeypatch.setattr(pipeline, "_STAGE_FNS", {s: wrap(s) for s in pipeline.STAGES})

    async def scenario():
        assert await pipeline.ensure_running(row["id"]) is True
        return await pipeline.wait_for(row["id"])

    fresh = asyncio.run(scenario())
    assert fresh["status"] == "done", fresh.get("error")
    assert int(fresh["pipeline_version"]) == 4
    assert sorted(ran) == ["artifacts", "fusion"]
    assert told["filename"] == "recording.m4a"
    assert fresh["understanding"]["content_type"] == "other"
    assert not [t for t in FIRST_TOKENS if t in json.dumps(fresh["understanding"]).lower()]
    for kept in ("probe", "audio", "transcript", "frames", "ocr", "vision", "index"):
        assert fresh["stages"][kept]["detail"] == "old", f"{kept} must be served from disk"
    db.delete_video_analysis(row["id"])


# --------------------------------------------- no picture, no screen label --


@pytest.mark.parametrize("label", ["meeting", "lecture", "interview", "demo", "other"])
def test_every_other_label_of_an_audio_file_is_kept(monkeypatch, tmp_path, label):
    from app.video import fusion, pipeline
    from app.video.types import Understanding

    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    content_hash = "e" * 64
    _seed_stage_files(content_hash, has_video=False)

    async def fake_understand(**kwargs):
        return Understanding(content_type=label, summary="x", method="direct")

    monkeypatch.setattr(fusion, "understand", fake_understand)
    ctx = pipeline._Ctx(row={"id": 1, "filename": "memo.m4a"}, content_hash=content_hash, source="")
    result = asyncio.run(pipeline._stage_fusion(ctx, _progress()))
    assert result.detail.split(" · ")[0] == label


# ------------------------------------------------- no engine address -------


def _unavailable() -> resilience.ModelUnavailable:
    last = openai.APIConnectionError(request=httpx.Request("POST", ENGINE))
    return resilience.ModelUnavailable(ENGINE, 780.0, 6, last)


def _seed_row(tmp_path, monkeypatch, content_hash: str):
    from app.video import store

    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    os.makedirs(store.analysis_dir(content_hash), exist_ok=True)
    src = tmp_path / "memo.m4a"
    src.write_bytes(b"\x00" * 64)
    store.adopt_source(content_hash, str(src), "memo.m4a")
    return db.upsert_video_analysis(content_hash, 64, "audio/mp4", "memo.m4a")


def _stub_fns(pipeline, overrides):
    from app.video import store

    def make(name):
        async def run(ctx, progress):
            output = pipeline._OUTPUTS.get(name)
            if output:
                store.write_json(store.stage_path(ctx.content_hash, output), {"stub": name})
            return pipeline._StageResult("done", name)
        return run

    fns = {s: make(s) for s in pipeline.STAGES}
    fns.update(overrides)
    return fns


def test_a_stage_waiting_for_the_model_stores_and_publishes_no_engine_address(monkeypatch, tmp_path, caplog):
    from app.video import pipeline
    from app.video.api import status_payload

    monkeypatch.setattr(settings, "video_max_attempts", 3)
    row = _seed_row(tmp_path, monkeypatch, "7" * 64)

    async def fusion_down(ctx, progress):
        raise _unavailable()

    async def vision_broken(ctx, progress):
        raise RuntimeError("Client error '404 Not Found' for url 'http://10.100.184.2:8002/v1/chat/completions' via 10.100.184.2:8002")

    monkeypatch.setattr(pipeline, "_STAGE_FNS", _stub_fns(pipeline, {"fusion": fusion_down, "vision": vision_broken}))
    published: list = []
    real_publish = pipeline._publish
    monkeypatch.setattr(pipeline, "_publish", lambda aid, event: (published.append(dict(event)), real_publish(aid, event))[1])

    with caplog.at_level("WARNING", logger="app.video.pipeline"):
        asyncio.run(pipeline._run(row["id"]))
    fresh = db.get_video_analysis(row["id"])
    assert fresh["status"] == "queued" and fresh["error"].startswith(pipeline.DEFERRED_MARK)
    assert fresh["stages"]["fusion"]["status"] == "deferred"
    assert fresh["stages"]["vision"]["status"] == "failed"
    stored = json.dumps({"error": fresh["error"], "stages": fresh["stages"]})
    wire = json.dumps(status_payload(fresh))
    events = json.dumps(published)
    for blob in (stored, wire, events):
        assert "10.100.184.2" not in blob and "http://" not in blob, blob
    # What the person reads still says what happened.
    assert "unavailable after 780s" in fresh["stages"]["fusion"]["detail"]
    assert "404 Not Found" in fresh["stages"]["vision"]["detail"]
    # The operator's log keeps the address.
    assert ENGINE in caplog.text
    db.delete_video_analysis(row["id"])


def test_a_running_stage_serves_its_live_detail_without_an_address(monkeypatch):
    from app.video import api as video_api
    from app.video import pipeline

    monkeypatch.setitem(pipeline._latest, 464646, {"stage": "fusion", "status": "running", "percent": 10.0, "detail": f"retrying {ENGINE}", "elapsed_s": 3.0})
    row = {"id": 464646, "status": "running", "stages": {"fusion": {"status": "pending"}}}
    served = {s["stage"]: s for s in video_api.status_payload(row)["stages"]}
    assert served["fusion"]["detail"] == "retrying the engine"


# ------------------------------------------ playlists, MIDI, stored names --


@pytest.mark.parametrize(
    "name,ctype",
    [
        ("radio.m3u", "audio/mpegurl"),
        ("radio.m3u", "audio/x-mpegurl"),
        ("radio.m3u", ""),
        ("live.m3u8", ""),
        ("live.m3u8", "application/vnd.apple.mpegurl"),
        ("stations.pls", "audio/x-scpls"),
        ("tune.mid", "audio/midi"),
        ("tune.MIDI", ""),
        ("ringtone", "audio/sp-midi"),
        ("playlist", "audio/x-mpegurl; charset=utf-8"),
    ],
)
def test_a_playlist_or_a_score_is_not_a_recording(name, ctype):
    from app.video.api import looks_like_video

    assert looks_like_video(name, ctype) is False


@pytest.mark.parametrize(
    "name,stored",
    [
        ("memo.m4a", "memo.m4a"),
        ("Clip.MOV", "Clip.MOV"),
        ("capture.ts", "capture.ts"),
        ("radio.m3u", "source.bin"),
        ("live.M3U8", "source.bin"),
        ("x.hls", "source.bin"),
        ("voice.oga", "source.bin"),
        ("recording", "source.bin"),
    ],
)
def test_the_source_keeps_only_a_known_media_extension(name, stored):
    from app.video import api as video_api

    assert video_api._stored_name(name) == stored


def test_an_hls_playlist_posted_as_video_is_stored_as_bin(monkeypatch, tmp_path, login_client):
    from app.video import pipeline, store

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path / "ws"))
    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    monkeypatch.setattr(settings, "video_analysis_enabled", True)
    monkeypatch.setattr(settings, "dataset_uploads_enabled", True)

    async def no_run(analysis_id):
        return False

    monkeypatch.setattr(pipeline, "ensure_running", no_run)
    dana = login_client("dana")
    resp = dana.post(
        "/uploads",
        files={"file": ("live.m3u8", b"#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXTINF:3,\n/etc/x.wav\n#EXT-X-ENDLIST\n", "application/vnd.apple.mpegurl")},
        data={"conversation_id": "conv-dana-1", "purpose": "video"},
    )
    assert resp.status_code == 200, resp.text
    row = db.get_video_analysis(resp.json()["video"]["analysis_id"])
    assert os.path.basename(store.source_path(row["content_hash"]) or "") == "source.bin"
    # The person's own name is untouched everywhere they read it.
    assert resp.json()["filename"] == "live.m3u8"


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True, timeout=60)


def test_a_bin_source_is_probed_by_its_bytes_and_never_demuxed_as_hls(monkeypatch, tmp_path):
    """The .bin fallback must not cost a real recording anything, and must
    stop a playlist from being opened as one. The positive control proves
    the playlist IS decoded under a playlist name on this ffmpeg, so the
    negative half is not passing for some unrelated reason."""
    from app.video import api as video_api
    from app.video import media, store

    if not media.tools_available() or shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed here")
    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    target = str(tmp_path / "elsewhere" / "tone.wav")
    os.makedirs(os.path.dirname(target))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=3", target)
    oga = str(tmp_path / "voice.oga")
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=300:duration=2", "-c:a", "libopus", "-f", "ogg", oga)

    stored = store.adopt_source("1" * 64, oga, video_api._stored_name("voice.oga"))
    assert stored.endswith("source.bin")
    probe = asyncio.run(media.probe(stored))
    assert probe.has_audio and probe.duration_s > 1.5

    playlist = tmp_path / "radio.m3u8"
    playlist.write_text(f"#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXTINF:3,\n{target}\n#EXT-X-ENDLIST\n")
    as_bin = store.adopt_source("2" * 64, str(playlist), video_api._stored_name("radio.m3u8"))
    assert as_bin.endswith("source.bin")
    with pytest.raises(Exception):
        asyncio.run(media.probe(as_bin))
    # Positive control: the same bytes stored under the uploader's own name.
    as_named = store.adopt_source("3" * 64, str(playlist), "radio.m3u8")
    control = asyncio.run(media.probe(as_named))
    assert control.has_audio and control.duration_s > 2.5


# --------------------------------------------------- "recording" wording --


def test_the_duration_limit_calls_an_audio_file_a_recording(monkeypatch, tmp_path):
    from app.video import media, pipeline

    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    monkeypatch.setattr(settings, "video_max_duration_s", 60.0)

    def fake(has_video):
        async def probe(path, timeout_s=60.0):
            return media.Probe(
                duration_s=3600.0, has_video=has_video, has_audio=True, width=0, height=0, fps=0.0,
                video_codec="", audio_codec="aac", container="mov,mp4,m4a", bytes=10,
            )
        return probe

    for has_video, word in ((False, "the recording is 1:00:00 long"), (True, "the video is 1:00:00 long")):
        monkeypatch.setattr(media, "probe", fake(has_video))
        ctx = pipeline._Ctx(row={"id": 1}, content_hash="8" * 64, source="x")
        result = asyncio.run(pipeline._stage_probe(ctx, _progress()))
        assert result.status == "failed"
        assert result.detail.startswith(word), result.detail


@pytest.mark.parametrize("name,word", [("memo.m4a", "recording"), ("standup.mp4", "video")])
def test_the_size_refusal_calls_an_audio_file_a_recording(monkeypatch, name, word):
    from app.video import api as video_api

    monkeypatch.setattr(settings, "video_max_upload_mb", 0)
    with pytest.raises(HTTPException) as err:
        asyncio.run(video_api.attach_upload(
            conversation_id="c", upload_id="u", filename=name, raw_path="/nonexistent", size=10, user_id=None,
        ))
    assert err.value.status_code == 413
    assert err.value.detail.startswith(f"That {word} is larger than")
