"""The API media lane: hand-off, pacing, progress relay, vectors, mapping.

No database and no chat pipeline are needed — the collaborators are injected
(`MediaDeps`) and `media.probe` is stubbed — so the three autouse fixtures from
conftest are replaced by no-ops, as test_api_soak_tool.py does.

The one place a real subprocess is exercised is `extract_audio_via_pipe`,
gated on an ffmpeg binary (the container's, or the static build the design
verified on); it is skipped where neither is present.

Proved (Files API design §6):
  * the hand-off keys the video analysis by PROJECT (isolation), sets the api
    lane, adopts the source, records the analysis on the blob, and starts the
    pipeline — and refuses a too-long or streamless file with the right code;
  * `probe`'s parse handles video, audio-only, cover-art and no-duration files;
  * the progress relay maps pipeline stages onto the fixed step ids and NEVER
    copies the engine-naming `detail` (mutation check: a detail carrying a
    model name / http:// / /data/ does not appear in the mapped view);
  * `api_pace` waits for chat then a chat GPU unit, each bounded, then runs;
  * `classify_outcome` maps done/deferred/corrupt to the file vocabulary;
  * `build_media_chunks` turns stage files into the evidence chunks;
  * the audio decode goes through `pipe:0` with the format whitelist, feeds
    EVERY byte of a source larger than one feed chunk (the communicate()
    truncation, measured at 174,773 of 960,000 samples), writes a seekable
    WAV with true header sizes instead of buffering pipe:1, reports the exact
    sample count from the `data` chunk, and on a timeout or a mid-source read
    error leaves neither an output nor a `.part` behind;
  * a streamless file refused by the REAL probe parse, and an ffprobe refusal,
    reach the caller as MediaUnreadable (file_corrupt) before anything is
    written, while missing tools stay a plain MediaError;
  * the pipeline's terminal event is never relayed as a processed file.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import struct
import subprocess

import pytest

from app.apifiles import media
from app.video import media as vmedia


# --- no database for this module ------------------------------------------
@pytest.fixture(scope="session")
def app_database():
    yield None


@pytest.fixture
def isolated_app_db():
    yield None


@pytest.fixture
def ambient_identity():
    yield None


# --- valid ids for storage.api_video_hash ---------------------------------
_SHA = "a" * 64
_P1 = "proj_" + "1" * 24
_P2 = "proj_" + "2" * 24


def _video_probe(duration_s: float = 10.0, *, has_audio=True, has_video=True) -> "vmedia.Probe":
    return vmedia.Probe(
        duration_s=duration_s, has_video=has_video, has_audio=has_audio,
        width=1920 if has_video else 0, height=1080 if has_video else 0,
        fps=30.0 if has_video else 0.0, video_codec="h264" if has_video else "",
        audio_codec="aac" if has_audio else "", container="mov,mp4", bytes=1234,
    )


def _recording_deps():
    """A MediaDeps whose calls land in `calls`, so the hand-off's side effects
    are observable without a database."""
    calls: dict = {"upsert": [], "adopt": [], "update_blob": [], "ensure": [], "cancel": []}

    def upsert(content_hash, size, media_type, filename, *, lane="api"):
        calls["upsert"].append({"content_hash": content_hash, "size": size,
                                "media_type": media_type, "filename": filename, "lane": lane})
        return {"id": 42, "content_hash": content_hash, "status": "queued"}

    def adopt(content_hash, path, filename):
        calls["adopt"].append((content_hash, path, filename))
        return path

    def update_blob(blob_id, **fields):
        calls["update_blob"].append((blob_id, fields))

    async def ensure(analysis_id):
        calls["ensure"].append(analysis_id)
        return True

    async def cancel(analysis_id):
        calls["cancel"].append(analysis_id)

    deps = media.MediaDeps(
        upsert_video_analysis=upsert,
        update_api_file_blob=update_blob,
        get_video_analysis=lambda aid: {"id": aid, "status": "done"},
        adopt_source=adopt,
        ensure_running=ensure,
        cancel=cancel,
        remove_analysis=lambda h: None,
        read_stage=lambda h, n: None,
        build_chunks=lambda seg, spans: [],
        segment_from_json=lambda d: d,
        ocrspan_from_json=lambda d: d,
    )
    return deps, calls


# ======================================================================
# The hand-off (design §6.2)
# ======================================================================


def test_the_handoff_keys_the_analysis_by_project_and_starts_the_api_lane(monkeypatch, tmp_path):
    deps, calls = _recording_deps()

    async def fake_probe(path, *, timeout_s=60.0):
        return _video_probe(10.0)

    monkeypatch.setattr(media, "probe", fake_probe)
    src = os.path.join(str(tmp_path), "clip.mp4")
    open(src, "wb").close()

    start = asyncio.run(media.start_media_analysis(
        project_id=_P1, sha256=_SHA, blob_id="blob_x", original_path=src,
        filename="clip.mp4", media_type="video/mp4", deps=deps,
    ))

    # content hash is the PROJECT-KEYED hash, never the raw sha256.
    from app.apifiles import storage
    assert start.content_hash == storage.api_video_hash(_P1, _SHA)
    assert start.content_hash != _SHA
    assert start.kind == "video"
    assert calls["upsert"][0]["lane"] == "api"
    assert calls["adopt"][0] == (start.content_hash, src, "clip.mp4")
    # the analysis id and kind are recorded on the blob, and the pipeline runs.
    blob_id, fields = calls["update_blob"][0]
    assert blob_id == "blob_x" and fields["video_analysis_id"] == 42 and fields["kind"] == "video"
    assert calls["ensure"] == [42]


def test_the_same_bytes_in_two_projects_get_different_video_hashes(monkeypatch, tmp_path):
    """The cross-tenant presence oracle defence (§7.2 rule 3): identical bytes
    in P1 and P2 never share a video_analyses content_hash."""
    from app.apifiles import storage

    h1 = storage.api_video_hash(_P1, _SHA)
    h2 = storage.api_video_hash(_P2, _SHA)
    assert h1 != h2 and h1 != _SHA and h2 != _SHA
    assert len(h1) == 64 and all(c in "0123456789abcdef" for c in h1)


def test_a_too_long_clip_is_refused_as_file_too_complex(monkeypatch, tmp_path):
    deps, _calls = _recording_deps()

    async def fake_probe(path, *, timeout_s=60.0):
        return _video_probe(10.0)

    monkeypatch.setattr(media, "probe", fake_probe)
    src = os.path.join(str(tmp_path), "clip.mp4")
    open(src, "wb").close()

    with pytest.raises(media.MediaTooLong):
        asyncio.run(media.start_media_analysis(
            project_id=_P1, sha256=_SHA, blob_id="b", original_path=src,
            filename="clip.mp4", media_type="video/mp4", deps=deps, max_seconds=5.0,
        ))


def test_a_cover_art_only_file_with_no_audio_is_refused_as_file_corrupt(monkeypatch, tmp_path):
    # The parse strips an attached picture, so a file holding ONLY cover art
    # yields a Probe with neither stream — the one shape that reaches this
    # branch of start_media_analysis rather than the parse's own refusal.
    deps, _calls = _recording_deps()

    async def fake_probe(path, *, timeout_s=60.0):
        return _video_probe(3.0, has_audio=False, has_video=False)

    monkeypatch.setattr(media, "probe", fake_probe)
    src = os.path.join(str(tmp_path), "x.mp4")
    open(src, "wb").close()

    with pytest.raises(media.MediaUnreadable):
        asyncio.run(media.start_media_analysis(
            project_id=_P1, sha256=_SHA, blob_id="b", original_path=src,
            filename="x.mp4", media_type="video/mp4", deps=deps,
        ))


def _fake_run_returning(payload: dict):
    async def fake_run(argv, *, timeout_s, what):
        return json.dumps(payload).encode(), b""
    return fake_run


def test_a_truly_streamless_file_through_the_real_probe_is_media_unreadable(monkeypatch, tmp_path):
    """Review finding (2026-09-13): the real parse raised a BARE MediaError for
    a streamless file before start_media_analysis's own check ran, while the
    old test stubbed a Probe the real parse never yields. Here only the
    subprocess is stubbed; the hardened probe and its parse are real."""
    deps, calls = _recording_deps()
    monkeypatch.setattr(vmedia, "tools_available", lambda: True)
    monkeypatch.setattr(vmedia, "_run", _fake_run_returning(
        {"format": {"duration": "3.0", "format_name": "mov,mp4"}, "streams": []}))
    src = os.path.join(str(tmp_path), "x.mp4")
    open(src, "wb").close()

    with pytest.raises(media.MediaUnreadable):
        asyncio.run(media.start_media_analysis(
            project_id=_P1, sha256=_SHA, blob_id="b", original_path=src,
            filename="x.mp4", media_type="video/mp4", deps=deps,
        ))
    # Refused before anything was written or started.
    assert calls["upsert"] == [] and calls["adopt"] == [] and calls["update_blob"] == [] and calls["ensure"] == []

    # A file reporting no duration is the file's fault too.
    monkeypatch.setattr(vmedia, "_run", _fake_run_returning(
        {"format": {}, "streams": [{"codec_type": "audio", "codec_name": "aac"}]}))
    with pytest.raises(media.MediaUnreadable):
        asyncio.run(media.probe(src))


def test_an_ffprobe_refusal_is_unreadable_but_missing_tools_are_not_the_files_fault(monkeypatch, tmp_path):
    src = os.path.join(str(tmp_path), "x.mp4")
    open(src, "wb").close()
    monkeypatch.setattr(vmedia, "tools_available", lambda: True)

    async def refused(argv, *, timeout_s, what):
        raise vmedia.MediaError(f"{what} failed: Format not on whitelist 'hls'")

    monkeypatch.setattr(vmedia, "_run", refused)
    with pytest.raises(media.MediaUnreadable):
        asyncio.run(media.probe(src))

    async def timed_out(argv, *, timeout_s, what):
        raise vmedia.MediaTimeout(f"{what} did not finish within 60s")

    monkeypatch.setattr(vmedia, "_run", timed_out)
    with pytest.raises(media.MediaTimeout) as caught:
        asyncio.run(media.probe(src))
    assert not isinstance(caught.value, media.MediaUnreadable)

    async def cannot_start(argv, *, timeout_s, what):
        raise vmedia.MediaError("could not start ffprobe: [Errno 13] Permission denied")

    monkeypatch.setattr(vmedia, "_run", cannot_start)
    with pytest.raises(media.MediaError) as caught:
        asyncio.run(media.probe(src))
    assert not isinstance(caught.value, media.MediaUnreadable)

    monkeypatch.setattr(vmedia, "tools_available", lambda: False)
    with pytest.raises(media.MediaError) as caught:
        asyncio.run(media.probe(src))
    assert not isinstance(caught.value, media.MediaUnreadable)


# ======================================================================
# probe parse (design §6.2.1)
# ======================================================================


def test_the_probe_parse_reads_video_audio_coverart_and_missing_duration():
    v = media._parse_probe({
        "format": {"duration": "12.5", "size": "999", "format_name": "mov,mp4"},
        "streams": [
            {"codec_type": "video", "width": 1920, "height": 1080, "codec_name": "h264", "avg_frame_rate": "30/1"},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }, "/x.mp4")
    assert v.has_video and v.has_audio and v.width == 1920 and abs(v.duration_s - 12.5) < 1e-6
    assert media.decide_kind(v) == "video"

    a = media._parse_probe({
        "format": {"duration": "8.0"},
        "streams": [{"codec_type": "audio", "codec_name": "mp3"}],
    }, "/x.mp3")
    assert a.has_audio and not a.has_video and media.decide_kind(a) == "audio"

    # A cover-art picture in an MP3 is NOT a video.
    cover = media._parse_probe({
        "format": {"duration": "8.0"},
        "streams": [
            {"codec_type": "video", "disposition": {"attached_pic": 1}, "codec_name": "mjpeg"},
            {"codec_type": "audio", "codec_name": "mp3"},
        ],
    }, "/song.mp3")
    assert not cover.has_video and media.decide_kind(cover) == "audio"

    # No container duration → frames / rate.
    derived = media._parse_probe({
        "format": {},
        "streams": [{"codec_type": "video", "nb_frames": "300", "avg_frame_rate": "30/1", "width": 640, "height": 480}],
    }, "/x.mkv")
    assert abs(derived.duration_s - 10.0) < 1e-6

    with pytest.raises(media.MediaUnreadable):
        media._parse_probe({"format": {}, "streams": []}, "/empty")


# ======================================================================
# Progress relay (design §6.1.5) — never the engine name
# ======================================================================


def test_the_progress_relay_maps_steps_and_drops_the_engine_detail():
    ev = {"stage": "vision", "status": "running", "percent": 40.0,
          "detail": "12/20 frames described by Qwen3-VL-8B at http://engine:8000 /data/video/x"}
    view = media.map_progress("video", ev)
    assert view == {"stage": "vision", "step": 7, "total_steps": 11, "percent": 40.0, "status": "running"}
    # The engine name, URL and path never appear in ANY value of the view.
    blob = " ".join(str(v) for v in view.values())
    assert "Qwen3-VL" not in blob and "http://" not in blob and "/data/" not in blob


def test_the_relay_drops_the_pipeline_index_stage_and_never_reports_the_terminal_event_as_a_file_state():
    # The pipeline's own `index` writes the shared LanceDB and is skipped on
    # the api lane; the files service owns the index step, so it is not relayed.
    assert media.map_progress("video", {"stage": "index", "status": "skipped"}) is None

    # Review finding (2026-09-13): `_done` used to map to finalize/100%/
    # processed while the files `index` (vectors) and `finalize` steps had not
    # run. It is now "nothing to relay", recognised by is_pipeline_done.
    for kind in ("video", "audio"):
        for status in ("done", "failed", "queued"):
            event = {"stage": "_done", "status": status}
            assert media.map_progress(kind, event) is None
            assert media.is_pipeline_done(event) is True
    assert media.is_pipeline_done({"stage": "artifacts", "status": "done"}) is False

    # No pipeline event of any stage or status is ever relayed as a finished
    # FILE, nor as the files service's own index/finalize steps.
    stages = ("probe", "audio", "transcript", "frames", "ocr", "vision", "fusion",
              "artifacts", "index", "_done", "")
    for kind in ("video", "audio"):
        for stage in stages:
            for status in ("running", "done", "skipped", "failed", "queued"):
                view = media.map_progress(kind, {"stage": stage, "status": status, "percent": 100})
                if view is not None:
                    assert view["status"] != "processed"
                    assert view["stage"] not in ("index", "finalize")


def test_audio_kind_omits_the_frame_stages():
    assert media.step_index("audio", "transcript") == 4
    assert media.step_index("audio", "ocr") is None       # no frame stages for audio
    assert media.step_index("video", "ocr") == 6
    assert media.steps_for("audio")[-1] == "finalize"


# ======================================================================
# Pacing (design §6.1.2)
# ======================================================================


def test_api_pace_waits_for_chat_then_a_chat_gpu_unit_then_runs():
    slept = {"n": 0}

    async def fake_sleep(_s):
        slept["n"] += 1

    # Chat is live for the whole first window; a chat GPU unit for the whole
    # second. Each wait is bounded, so the unit runs after live+unit seconds.
    waited = asyncio.run(media.api_pace(
        chat_live=lambda: True,
        chat_in_gpu_unit=lambda: True,
        live_max_wait_s=3.0,
        unit_max_wait_s=5.0,
        sleep=fake_sleep,
    ))
    assert waited == 8.0 and slept["n"] == 8


def test_api_pace_returns_at_once_when_chat_is_idle():
    async def fake_sleep(_s):
        raise AssertionError("should not sleep when chat is idle")

    waited = asyncio.run(media.api_pace(
        chat_live=lambda: False, chat_in_gpu_unit=lambda: False, sleep=fake_sleep,
    ))
    assert waited == 0.0


def test_api_pace_survives_a_throwing_probe():
    async def fake_sleep(_s):
        raise AssertionError("advisory probe error must be treated as not-busy")

    def boom():
        raise RuntimeError("probe blew up")

    waited = asyncio.run(media.api_pace(
        chat_live=boom, chat_in_gpu_unit=boom, sleep=fake_sleep,
    ))
    assert waited == 0.0


# ======================================================================
# Failure mapping (design §6.2.5)
# ======================================================================


def test_classify_outcome_maps_done_deferred_and_corrupt():
    assert media.classify_outcome({"status": "done"}) == ("processed", None)
    assert media.classify_outcome({"status": "failed", "error": "waiting for the model (attempt 5): Transcribing"}) \
        == ("failed", "processing_unavailable")
    assert media.classify_outcome({"status": "failed", "error": "Probing the file: moov atom not found"}) \
        == ("failed", "file_corrupt")
    assert media.classify_outcome({"status": "failed", "error": "something odd"}) \
        == ("failed", "processing_unavailable")


# ======================================================================
# Vectors from stage files (design §6.3)
# ======================================================================


def test_build_media_chunks_turns_stage_files_into_evidence_chunks():
    from app.video.types import OcrSpan, Segment
    from app.video import index as vindex

    stages = {
        "transcript.json": {"segments": [
            {"start": 0.0, "end": 3.0, "text": "the deployment window moves to Thursday"},
            {"start": 3.0, "end": 6.0, "text": "everyone agreed on the ninth"},
        ]},
        "screen.json": {"spans": [
            {"start": 2823.0, "end": 2843.0, "text": "CHECKPOINT BRAVO 9082", "kind": "slide"},
        ]},
    }
    deps = media.MediaDeps(
        upsert_video_analysis=lambda *a, **k: {"id": 1},
        update_api_file_blob=lambda *a, **k: None,
        get_video_analysis=lambda aid: None,
        adopt_source=lambda *a: "",
        ensure_running=lambda aid: None,
        cancel=lambda aid: None,
        remove_analysis=lambda h: None,
        read_stage=lambda h, name: stages.get(name),
        build_chunks=vindex.build_chunks,
        segment_from_json=Segment.from_json,
        ocrspan_from_json=OcrSpan.from_json,
    )

    chunks = media.build_media_chunks("f" * 64, deps=deps)

    modalities = {c["modality"] for c in chunks}
    assert "speech" in modalities and "screen" in modalities
    joined = " ".join(c["text"] for c in chunks)
    assert "Thursday" in joined and "9082" in joined
    # chunk_ix is assigned in time order (the chunker's contract).
    assert [c["chunk_ix"] for c in chunks] == list(range(len(chunks)))


# ======================================================================
# The audio decode goes through pipe:0 (design §6.4) — needs ffmpeg
# ======================================================================


def _resolve_ffmpeg():
    found = shutil.which("ffmpeg")
    if found:
        return found, shutil.which("ffprobe")
    static = "/home/techsphere/Downloads/saleforce-LLM/orchestrator/.venv/lib/python3.12/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-aarch64-v7.0.2"
    if os.path.exists(static):
        return static, None
    return None, None


def test_extract_audio_via_pipe_decodes_from_stdin(monkeypatch, tmp_path):
    ffmpeg, _ffprobe = _resolve_ffmpeg()
    if not ffmpeg:
        pytest.skip("no ffmpeg binary available")
    # Put the resolved ffmpeg on PATH under the name create_subprocess_exec
    # looks up, and satisfy tools_available() (which also checks ffprobe).
    bindir = tmp_path / "bin"
    bindir.mkdir()
    os.symlink(ffmpeg, str(bindir / "ffmpeg"))
    os.symlink(ffmpeg, str(bindir / "ffprobe"))  # only existence is needed here
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(vmedia, "tools_available", lambda: True)

    # A real 2-second WAV made by the same ffmpeg.
    src = str(tmp_path / "a.wav")
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2", src], check=True)

    out = str(tmp_path / "out.wav")
    samples = asyncio.run(media.extract_audio_via_pipe(src, out, timeout_s=60.0))

    assert samples > 0 and os.path.exists(out)
    # 16 kHz mono, 2 s: exactly 32,000 samples, read from the data chunk.
    assert samples == 32_000
    assert not os.path.exists(out + ".part")


def _ffmpeg_on_path(monkeypatch, tmp_path):
    ffmpeg, _ffprobe = _resolve_ffmpeg()
    if not ffmpeg:
        pytest.skip("no ffmpeg binary available")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    os.symlink(ffmpeg, str(bindir / "ffmpeg"))
    os.symlink(ffmpeg, str(bindir / "ffprobe"))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(vmedia, "tools_available", lambda: True)
    return ffmpeg


def _wav_chunks(path):
    """(riff_size, file_size, {chunk_id: (offset, length)}) of a WAV file."""
    data = open(path, "rb").read()
    riff = struct.unpack("<I", data[4:8])[0]
    chunks, pos = {}, 12
    while pos + 8 <= len(data):
        cid, clen = struct.unpack("<4sI", data[pos:pos + 8])
        chunks[cid] = (pos, clen)
        if cid == b"data":
            break
        pos += 8 + clen + (clen & 1)
    return riff, len(data), chunks


def test_extract_audio_via_pipe_feeds_every_byte_of_a_source_larger_than_one_chunk(monkeypatch, tmp_path):
    """The truncation bug (2026-09-13): communicate() closed stdin after the
    first 1 MiB chunk, so a 60 s, 11.5 MB WAV came back as 174,773 samples
    (10.9 s) with exit 0. Mutation-checked by restoring communicate()."""
    ffmpeg = _ffmpeg_on_path(monkeypatch, tmp_path)
    src = str(tmp_path / "long.wav")
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=60:sample_rate=48000", "-ac", "2", src],
                   check=True)
    assert os.path.getsize(src) > 8 * 1024 * 1024  # several feed chunks

    out = str(tmp_path / "out.wav")
    samples = asyncio.run(media.extract_audio_via_pipe(src, out, timeout_s=120.0))

    assert samples == 60 * 16_000


def test_extract_audio_via_pipe_writes_a_seekable_wav_with_true_sizes_and_buffers_nothing(monkeypatch, tmp_path):
    """Review finding (2026-09-13): `-f wav pipe:1` + communicate() held the
    whole decoded track in RAM (439 MiB at the 4-hour ceiling) and left the
    RIFF and data sizes at 0xFFFFFFFF. The decode now writes a file, stdout is
    DEVNULL, and the header sizes are the real ones."""
    ffmpeg = _ffmpeg_on_path(monkeypatch, tmp_path)
    src = str(tmp_path / "a.wav")
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=5", src], check=True)

    seen = {}
    real_exec = asyncio.create_subprocess_exec

    async def recording_exec(*argv, **kwargs):
        seen["argv"], seen["kwargs"] = list(argv), dict(kwargs)
        return await real_exec(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_exec)
    out = str(tmp_path / "out.wav")
    samples = asyncio.run(media.extract_audio_via_pipe(src, out, timeout_s=60.0))

    assert seen["kwargs"]["stdout"] is asyncio.subprocess.DEVNULL
    assert "pipe:1" not in seen["argv"]
    assert seen["argv"][-1] == "file:" + os.path.abspath(out + ".part")
    riff, size, chunks = _wav_chunks(out)
    assert riff == size - 8 and riff != 0xFFFFFFFF
    data_off, data_len = chunks[b"data"]
    assert data_len != 0xFFFFFFFF and data_len == size - data_off - 8
    assert samples == data_len // 2 == 5 * 16_000


def test_the_sample_count_comes_from_the_data_chunk_not_a_44_byte_header(tmp_path):
    pcm = b"\x01\x00" * 1000  # 1,000 samples
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, 16_000, 32_000, 2, 16)
    info = b"LIST" + struct.pack("<I", 26) + b"INFOISFT" + struct.pack("<I", 14) + b"Lavf61.1.100\x00\x00"
    body = b"WAVE" + fmt + info + b"data" + struct.pack("<I", len(pcm)) + pcm
    path = str(tmp_path / "x.wav")
    with open(path, "wb") as fh:
        fh.write(b"RIFF" + struct.pack("<I", len(body)) + body)
    # (size - 44) // 2 would say 1,017.
    assert media._wav_pcm16_samples(path) == 1000

    # A streamed header (0xFFFFFFFF sizes) falls back to the bytes present.
    streamed = str(tmp_path / "s.wav")
    body2 = b"WAVE" + fmt + b"data" + struct.pack("<I", 0xFFFFFFFF) + pcm
    with open(streamed, "wb") as fh:
        fh.write(b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + body2)
    assert media._wav_pcm16_samples(streamed) == 1000

    with pytest.raises(media.MediaError):
        bad = str(tmp_path / "bad.wav")
        open(bad, "wb").write(b"not a wav at all")
        media._wav_pcm16_samples(bad)


def test_extract_audio_via_pipe_timeout_stops_the_decode_and_leaves_no_part_file(monkeypatch, tmp_path):
    ffmpeg = _ffmpeg_on_path(monkeypatch, tmp_path)
    src = str(tmp_path / "long.wav")
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=60:sample_rate=48000", "-ac", "2", src],
                   check=True)
    out = str(tmp_path / "out.wav")

    with pytest.raises(media.MediaTimeout):
        asyncio.run(media.extract_audio_via_pipe(src, out, timeout_s=0.001))

    assert not os.path.exists(out) and not os.path.exists(out + ".part")


def test_a_read_error_part_way_through_the_source_is_not_returned_as_a_short_track(monkeypatch, tmp_path):
    """If reading the SOURCE fails mid-way, stdin closes early and ffmpeg exits
    0 on what it got. The feeder's failure must win over that exit code."""
    ffmpeg = _ffmpeg_on_path(monkeypatch, tmp_path)
    src = str(tmp_path / "long.wav")
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=30:sample_rate=48000", "-ac", "2", src],
                   check=True)
    real_open = open

    class _FailingAfterTwoChunks:
        def __init__(self, path):
            self._fh = real_open(path, "rb")
            self._reads = 0

        def read(self, n=-1):
            self._reads += 1
            if self._reads > 2:
                raise OSError(5, "Input/output error")
            return self._fh.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()
            return False

    def fake_open(path, mode="r", *a, **k):
        if path == src and "b" in mode and "r" in mode:
            return _FailingAfterTwoChunks(path)
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(media, "open", fake_open, raising=False)
    out = str(tmp_path / "out.wav")

    with pytest.raises(media.MediaError) as caught:
        asyncio.run(media.extract_audio_via_pipe(src, out, timeout_s=60.0))
    assert "reading the source" in str(caught.value)
    assert str(src) not in str(caught.value)  # no storage path in the message
    assert not os.path.exists(out) and not os.path.exists(out + ".part")


def test_extract_audio_via_pipe_refuses_a_disguised_playlist(monkeypatch, tmp_path):
    ffmpeg, _ffprobe = _resolve_ffmpeg()
    if not ffmpeg:
        pytest.skip("no ffmpeg binary available")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    os.symlink(ffmpeg, str(bindir / "ffmpeg"))
    os.symlink(ffmpeg, str(bindir / "ffprobe"))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(vmedia, "tools_available", lambda: True)

    # A victim WAV another tenant should never reach, and a playlist naming it.
    victim = str(tmp_path / "victim.wav")
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=880:duration=1", victim], check=True)
    playlist = str(tmp_path / "evil.m3u8")
    with open(playlist, "w") as fh:
        fh.write(f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1.0,\n{victim}\n#EXT-X-ENDLIST\n")

    # pipe:0 with the format whitelist has no `hls`, so the demuxer is refused
    # and the victim is never read.
    out = str(tmp_path / "o.wav")
    with pytest.raises(media.MediaError):
        asyncio.run(media.extract_audio_via_pipe(playlist, out, timeout_s=30.0))
    assert not os.path.exists(out) and not os.path.exists(out + ".part")
