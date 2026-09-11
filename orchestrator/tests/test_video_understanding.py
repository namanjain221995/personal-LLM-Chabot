"""Video understanding (2026-09-09): the stages that can be proven offline.

The pipeline's expensive stages call ffmpeg, Whisper, the OCR sidecar, the
router and the main model. None of those exist in CI, so this file tests the
LOGIC around them — the windowing, the seam stitching, the hash dedupe and
cap, the subtitle writers, the evidence pack and its splitting, the JSON
tolerance, the index chunking and scoping predicate, the follow-up gate, the
pinned block, the migration, the upload gate — with the readers faked at the
one seam each has. The live run is scripts/video_smoke.py, against the real
engines, and its numbers are in docs/video-understanding/README.md.
"""
from __future__ import annotations

import asyncio
import json
import os
import struct

import pytest

from app import db
from app.config import settings
from app.video import artifacts as art
from app.video import frames as fr
from app.video import fusion, vad
from app.video.media import FrameFile
from app.video.transcribe import stitch, wav_bytes
from app.video.types import Chapter, OcrSpan, Segment, Understanding
from app.video.vad import Window


# ------------------------------------------------------------------ VAD --


def _tone(seconds: float, *, freq: float = 220.0, amp: float = 0.4):
    import numpy as np

    t = np.arange(int(seconds * vad.SAMPLE_RATE)) / vad.SAMPLE_RATE
    return (np.sin(2 * np.pi * freq * t) * amp * 32767).astype("<i2")


def _silence(seconds: float):
    import numpy as np

    return np.zeros(int(seconds * vad.SAMPLE_RATE), dtype="<i2")


def _concat(*parts):
    import numpy as np

    return np.concatenate(parts)


def test_energy_detector_finds_speech_and_silence():
    pcm = _concat(_silence(1.0), _tone(2.0), _silence(3.0), _tone(1.5), _silence(0.5))
    flags = vad._frames_speech_energy(pcm)
    regions = vad.regions_from_flags(flags, total_s=8.0)
    assert len(regions) == 2
    (a0, a1), (b0, b1) = regions
    assert 0.6 <= a0 <= 1.1 and 2.8 <= a1 <= 3.3
    assert 5.7 <= b0 <= 6.2 and 7.3 <= b1 <= 7.8


def test_windows_never_span_a_long_gap_and_never_exceed_the_ceiling():
    regions = [(0.0, 50.0), (51.0, 100.0), (110.0, 160.0), (161.0, 400.0)]
    windows = vad.windows_from_regions(regions, max_window_s=120.0, max_gap_s=2.0, overlap_s=3.0)
    for w in windows:
        assert w.duration_s <= 120.0 + 1e-6
    # 0-100 joins (gap 1 s); 110-160 is its own; 161-400 (239 s of continuous
    # speech) is split into overlapping pieces.
    assert windows[0].start_s == 0.0 and windows[0].end_s == 100.0
    assert windows[1].start_s == 110.0 and windows[1].end_s == 160.0
    tail = [w for w in windows if w.start_s >= 161.0]
    assert len(tail) >= 2
    assert tail[0].overlaps_previous is False
    assert all(w.overlaps_previous for w in tail[1:])
    for prev, nxt in zip(tail, tail[1:]):
        assert nxt.start_s == pytest.approx(prev.end_s - 3.0)


def test_a_silent_recording_produces_no_windows():
    pcm = _silence(30.0)
    windows, report = vad.plan_windows(pcm, total_s=30.0, max_window_s=240.0)
    assert windows == []
    assert report["speech_fraction"] == 0.0
    assert report["detector"] in ("webrtcvad", "energy")


def test_plan_windows_reports_which_detector_ran():
    pcm = _concat(_silence(0.5), _tone(3.0), _silence(0.5))
    windows, report = vad.plan_windows(pcm, total_s=4.0, max_window_s=240.0)
    assert report["detector"] in ("webrtcvad", "energy")
    assert report["windows"] == len(windows)


# ---------------------------------------------------------------- PCM --


def test_wav_bytes_round_trip(tmp_path):
    from app.video.transcribe import read_wav_pcm16

    pcm = _tone(1.0)
    path = tmp_path / "a.wav"
    path.write_bytes(wav_bytes(pcm))
    back = read_wav_pcm16(str(path))
    assert back.shape == pcm.shape
    assert int(back[100]) == int(pcm[100])


def test_read_wav_skips_a_list_chunk_before_data(tmp_path):
    """ffmpeg writes LIST before data; a fixed 44-byte offset would read
    metadata as audio."""
    from app.video.transcribe import read_wav_pcm16

    pcm = _tone(0.5)
    data = pcm.tobytes()
    fmt = struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
    lst = b"INFOISFT" + struct.pack("<I", 6) + b"Lavf60"
    body = b"WAVE" + b"fmt " + fmt + b"LIST" + struct.pack("<I", len(lst)) + lst + b"data" + struct.pack("<I", len(data)) + data
    path = tmp_path / "b.wav"
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    back = read_wav_pcm16(str(path))
    assert back.shape == pcm.shape
    assert int(back[7]) == int(pcm[7])


# --------------------------------------------------------------- stitch --


def test_video_windows_bypass_the_engine_silence_gate(monkeypatch, tmp_path):
    """A 12-second clip of real speech came back empty because the engine's
    first-30-seconds gate scored its quiet lead-in at 0.62. The pipeline has
    run voice-activity detection already, so it must say so to the engine."""
    import asyncio
    import math
    import struct
    import wave

    from app import asr
    from app.video import transcribe
    from app.video.vad import SAMPLE_RATE, Window

    wav = tmp_path / "a.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 7.0))) for i in range(SAMPLE_RATE * 2)))

    seen = {}

    async def fake_engine(audio, *, filename, content_type, **kwargs):
        seen.update(kwargs)
        return asr.TranscriptSegments(
            text="hello", language="English", language_code="en",
            provider="test", model="test", engine_ms=5,
            segments=({"start": 0.0, "end": 2.0, "text": "hello"},),
        )

    monkeypatch.setattr(asr, "transcribe_segments", fake_engine)
    monkeypatch.setattr(transcribe, "plan_windows", lambda *a, **k: ([Window(start_s=0.0, end_s=2.0)], {"detector": "test", "speech_fraction": 1.0}))

    async def progress(*_a):
        return None

    segments, language, report = asyncio.run(
        transcribe.transcribe_audio(str(wav), total_s=2.0, progress=progress, max_window_s=240.0, max_gap_s=2.0, overlap_s=1.0)
    )
    assert seen.get("no_speech_check") is False
    assert [s.text for s in segments] == ["hello"] and language == "en"


def test_media_errors_never_carry_a_storage_path(tmp_path):
    import asyncio

    from app.video import media

    if not media.tools_available():
        pytest.skip("ffmpeg is not installed here")
    bad = tmp_path / "corrupt.mp4"
    bad.write_bytes(b"not a video at all" * 100)
    with pytest.raises(media.MediaError) as err:
        asyncio.run(media.probe(str(bad)))
    assert str(tmp_path) not in str(err.value)
    assert "corrupt.mp4" in str(err.value) or "the file" in str(err.value)


def test_stitch_drops_the_overlap_copy_and_keeps_time_monotonic():
    w1 = Window(0.0, 100.0)
    w2 = Window(97.0, 200.0, overlaps_previous=True)
    s1 = [Segment(0.0, 50.0, "first half"), Segment(50.0, 99.0, "second half")]
    s2 = [Segment(97.2, 99.1, "second half"), Segment(99.5, 150.0, "third"), Segment(150.0, 200.0, "fourth")]
    out = stitch([(w1, s1), (w2, s2)])
    texts = [s.text for s in out]
    assert texts == ["first half", "second half", "third", "fourth"]
    for a, b in zip(out, out[1:]):
        assert b.start_s >= a.end_s


def test_stitch_keeps_a_segment_straddling_the_seam():
    w1 = Window(0.0, 60.0)
    w2 = Window(57.0, 120.0, overlaps_previous=True)
    out = stitch([(w1, [Segment(0.0, 59.0, "before")]), (w2, [Segment(58.0, 63.0, "across"), Segment(63.0, 90.0, "after")])])
    assert [s.text for s in out] == ["before", "across", "after"]


# --------------------------------------------------------------- frames --


def _png(path, *, seed: int, width=64, height=36, blank=False):
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (width, height), (240, 240, 245) if not blank else (0, 0, 0))
    d = ImageDraw.Draw(im)
    if not blank:
        # Deterministic "content": a few rectangles whose layout depends on seed.
        for i in range(4):
            x = (seed * 7 + i * 13) % (width - 10)
            y = (seed * 3 + i * 5) % (height - 6)
            d.rectangle((x, y, x + 8, y + 5), fill=((seed * 37) % 255, (i * 60) % 255, 80))
    im.save(path)


def test_phash_matches_identical_frames_and_separates_different_ones(tmp_path):
    a, b, c = tmp_path / "a.jpg", tmp_path / "b.jpg", tmp_path / "c.jpg"
    _png(a, seed=1)
    _png(b, seed=1)
    _png(c, seed=9)
    ha, hb, hc = fr.phash_file(str(a)), fr.phash_file(str(b)), fr.phash_file(str(c))
    assert fr.hamming(ha, hb) == 0
    assert fr.hamming(ha, hc) > fr.DEFAULT_DISTANCE


def test_dedupe_collapses_a_held_slide_into_one_span(tmp_path):
    files = []
    for i in range(6):
        p = tmp_path / f"f_{i:06d}.jpg"
        _png(p, seed=1 if i < 4 else 2)  # slide A held for four samples, then B
        files.append(FrameFile(path=str(p), t_s=float(i * 10), index=i))
    kept = fr.dedupe(files, total_s=60.0)
    assert len(kept) == 2
    assert kept[0].t_s == 0.0 and kept[0].end_s == 40.0 and kept[0].collapsed == 4
    assert kept[1].t_s == 40.0 and kept[1].end_s == 60.0


def test_frame_cap_is_adaptive_and_clamped():
    assert fr.frame_cap(12, per_seconds=8, minimum=24, maximum=400) == 24
    assert fr.frame_cap(800, per_seconds=8, minimum=24, maximum=400) == 100
    assert fr.frame_cap(7200, per_seconds=8, minimum=24, maximum=400) == 400


def test_thin_keeps_the_longest_held_frames_in_time_order():
    kept = [
        fr.KeptFrame(path=f"f{i}", t_s=float(i * 10), end_s=float(i * 10 + (30 if i % 2 == 0 else 5)), index=i, phash=i)
        for i in range(6)
    ]
    out = fr.thin(kept, 3)
    assert [k.index for k in out] == [0, 2, 4]
    assert out[0].end_s == out[1].t_s and out[1].end_s == out[2].t_s


# ------------------------------------------------------------ artifacts --

SEGS = [
    Segment(0.0, 2.5, "Hello everyone."),
    Segment(2.5, 2.5, "Zero length cue."),
    Segment(61.0, 65.2, "Let's talk about pricing --> now."),
]


def test_fmt_ts_and_parse_ts_agree():
    for s in (0, 5, 59, 60, 754, 3599, 3600, 4523):
        assert art.parse_ts(art.fmt_ts(s)) == s
    assert art.fmt_ts(754) == "12:34"
    assert art.fmt_ts(4523) == "1:15:23"
    with pytest.raises(ValueError):
        art.parse_ts("twelve")


def test_srt_numbers_cues_and_never_writes_a_zero_length_cue():
    srt = art.transcript_srt(SEGS)
    blocks = [b for b in srt.strip().split("\n\n") if b.strip()]
    assert [b.splitlines()[0] for b in blocks] == ["1", "2", "3"]
    for b in blocks:
        start, end = b.splitlines()[1].split(" --> ")
        assert start < end, b


def test_vtt_has_header_and_escapes_the_arrow():
    vtt = art.transcript_vtt(SEGS)
    assert vtt.startswith("WEBVTT\n")
    assert "pricing --> now" not in vtt
    assert "pricing → now" in vtt


def test_transcript_txt_and_json_share_timestamps():
    txt = art.transcript_txt(SEGS, title="t")
    data = json.loads(art.transcript_json(SEGS, language="en", duration_s=70.0))
    assert "[1:01] Let's talk about pricing --> now." in txt
    assert data["segments"][-1]["start"] == 61.0 and data["language"] == "en"


def test_summary_md_renders_every_section():
    u = Understanding(
        content_type="meeting",
        summary="A planning meeting.",
        chapters=[Chapter(0.0, 60.0, "Agenda", "What we will cover")],
        key_points=["Point"],
        decisions=["Team tier rises to $59"],
        action_items=["Elizabeth updates the pricing page"],
        entities=["Elizabeth"],
        not_covered="Budget totals were not stated.",
    )
    md = art.summary_md(u, title="Weekly", duration_s=632.0, language="English")
    for needle in ("# Weekly", "## Summary", "## Chapters", "**[0:00]** Agenda", "## Decisions", "$59", "## Action items", "## Not covered"):
        assert needle in md


# --------------------------------------------------------------- fusion --


def test_evidence_pack_interleaves_speech_and_screen_by_time():
    segs = [Segment(0.0, 5.0, "one"), Segment(5.0, 70.0, "two"), Segment(70.0, 80.0, "three")]
    spans = [OcrSpan(30.0, 90.0, "Agenda\n1. Pricing", kind="slide", caption="A slide")]
    lines = fusion.evidence_lines(segs, spans)
    kinds = [ln.split("] ")[1].split(":")[0] for _t, ln in lines]
    assert kinds[0] == "SPEECH"
    assert any(k.startswith("SCREEN (slide)") for k in kinds)
    starts = [t for t, _ in lines]
    assert starts == sorted(starts)


def test_split_pack_never_cuts_a_line(monkeypatch):
    lines = [(float(i), f"[{art.fmt_ts(i)}] SPEECH: " + "word " * 200) for i in range(12)]
    parts = fusion.split_pack(lines, part_tokens=600)
    assert len(parts) > 1
    assert sum(len(p) for p in parts) == len(lines)
    assert all(p for p in parts)


def test_chapters_from_model_are_validated_against_the_duration():
    data = {
        "chapters": [
            {"start": "0:00", "title": "Intro"},
            {"start": "2:00", "end": "5:00", "title": "Pricing", "summary": "s"},
            {"start": "2:03", "title": "Too close to the previous"},
            {"start": "99:00", "title": "Past the end — invented"},
            {"start": "banana", "title": "Not a time"},
        ]
    }
    chapters = fusion._chapters_from(data, duration_s=600.0)
    assert [c.title for c in chapters] == ["Intro", "Pricing"]
    assert chapters[0].end_s == 120.0 and chapters[1].end_s == 600.0


def test_model_json_is_parsed_through_fences_and_prefixes():
    raw = 'Sure, here it is:\n```json\n{"summary": "x", "chapters": []}\n```'
    assert fusion._as_notes(raw)["summary"] == "x"
    assert fusion._as_notes("not json") is None
    assert fusion._as_notes("")["summary"] if fusion._as_notes("") else True


def test_understand_goes_direct_under_the_limit_and_map_reduce_above(monkeypatch):
    async def scenario():
        calls = []

        async def fake_json(messages, **kwargs):
            calls.append(messages[-1]["content"][:400])
            return json.dumps({
                "summary": "s", "key_points": ["k"], "decisions": [], "action_items": [],
                "entities": [], "chapters": [{"start": "0:00", "title": "c"}],
                "content_type": "lecture", "not_covered": "",
            })

        from app import llm

        monkeypatch.setattr(llm, "json_completion", fake_json)
        segs = [Segment(float(i * 60), float(i * 60 + 55), "word " * 180) for i in range(60)]

        async def progress(_p, _d):
            pass

        monkeypatch.setattr(settings, "video_fusion_direct_tokens", 1_000_000)
        u = await fusion.understand(filename="v.mp4", duration_s=600.0, language="English", has_audio=True, speech_fraction=0.9, segments=segs, spans=[], progress=progress)
        assert u.method == "direct" and u.content_type == "lecture" and len(calls) == 1

        calls.clear()
        monkeypatch.setattr(settings, "video_fusion_direct_tokens", 8_000)
        monkeypatch.setattr(settings, "video_fusion_part_tokens", 4_000)
        u = await fusion.understand(filename="v.mp4", duration_s=600.0, language="English", has_audio=True, speech_fraction=0.9, segments=segs, spans=[], progress=progress)
        assert u.method.startswith("map_reduce:") and len(calls) >= 3
        assert "PART 1 of" in calls[0]

    asyncio.run(scenario())


def test_pack_header_says_no_speech_when_the_track_yielded_none():
    from app.video.fusion import _header

    h = _header(filename="a.mp4", duration_s=12.0, language=None, has_audio=True, speech_fraction=1.0, has_speech=False)
    assert "no transcribable speech" in h
    h = _header(filename="a.mp4", duration_s=12.0, language="en", has_audio=True, speech_fraction=1.0, has_speech=True)
    assert "no transcribable speech" not in h and "Speech language: en" in h


def test_understand_with_no_evidence_is_honest():
    async def scenario():
        async def progress(_p, _d):
            pass

        u = await fusion.understand(filename="v.mp4", duration_s=12.0, language=None, has_audio=False, speech_fraction=None, segments=[], spans=[], progress=progress)
        assert u.method == "empty" and u.summary == "" and "nothing to summarise" in u.not_covered

    asyncio.run(scenario())


# ---------------------------------------------------------------- index --


def test_index_chunks_carry_modality_and_time_and_are_sorted():
    from app.video import index

    segs = [Segment(float(i * 10), float(i * 10 + 9), "spoken words " * 10) for i in range(12)]
    spans = [OcrSpan(30.0, 90.0, "on screen text", kind="slide", caption="a slide"), OcrSpan(100.0, 110.0, "", kind="person", caption="a face")]
    chunks = index.build_chunks(segs, spans)
    modalities = {c["modality"] for c in chunks}
    assert modalities == {"speech", "screen", "visual"}
    assert [c["chunk_ix"] for c in chunks] == list(range(len(chunks)))
    starts = [c["start_s"] for c in chunks]
    assert starts == sorted(starts)
    speech = [c for c in chunks if c["modality"] == "speech"]
    assert all(c["end_s"] - c["start_s"] <= 45.0 + 9.0 for c in speech)


def test_index_writes_its_own_directory_and_scopes_queries_by_analysis(monkeypatch, tmp_path):
    async def scenario():
        from app import llm
        from app.video import index

        dim = 8

        async def fake_embed_texts(texts, **kw):
            out = []
            for t in texts:
                v = [0.0] * dim
                v[hash(t) % dim] = 1.0
                out.append(v)
            return out

        async def fake_embed_query(text, **kw):
            v = [0.0] * dim
            v[hash("the spoken line") % dim] = 1.0
            return v

        monkeypatch.setattr(llm, "embed_texts", fake_embed_texts)
        monkeypatch.setattr(llm, "embed_query", fake_embed_query)
        monkeypatch.setattr(settings, "embed_model", "fake-embed")

        await index.index_analysis(1, [{"chunk_ix": 0, "modality": "speech", "start_s": 0.0, "end_s": 5.0, "text": "the spoken line"}])
        await index.index_analysis(2, [{"chunk_ix": 0, "modality": "speech", "start_s": 0.0, "end_s": 5.0, "text": "the spoken line"}])
        assert os.path.isdir(settings.lancedb_video_dir)
        assert not os.path.exists(os.path.join(settings.lancedb_web_dir, "web_chunks.lance"))

        from app import rerank

        async def passthrough(query, items, **kw):
            return list(items[: kw.get("top_n") or len(items)])

        monkeypatch.setattr(rerank, "order", passthrough)
        only_one = await index.retrieve("the spoken line", [1], top_k=5)
        assert {h["analysis_id"] for h in only_one} == {1}
        both = await index.retrieve("the spoken line", [1, 2], top_k=5)
        assert {h["analysis_id"] for h in both} == {1, 2}
        none = await index.retrieve("the spoken line", [3], top_k=5)
        assert none == []
        await index.delete_analysis(1)
        assert {h["analysis_id"] for h in await index.retrieve("the spoken line", [1, 2], top_k=5)} == {2}

    asyncio.run(scenario())


def test_the_video_index_refuses_to_share_a_directory(monkeypatch, tmp_path):
    from app.video import index

    monkeypatch.setattr(settings, "lancedb_video_dir", settings.lancedb_web_dir)
    with pytest.raises(index.VideoIndexUnavailable):
        index.video_dir()


# --------------------------------------------------------------- engine --


def _row(**over):
    base = {
        "id": 7, "content_hash": "a" * 64, "filename": "meeting.mp4", "display_name": "meeting.mp4",
        "status": "done", "error": "", "duration_ms": 632_000, "language": "English", "has_audio": True,
        "counts": {"segments": 120, "frames_kept": 12, "captions": 12},
        "understanding": {
            "content_type": "meeting", "summary": "A planning meeting about pricing.",
            "chapters": [{"start": 0, "end": 60, "title": "Agenda"}, {"start": 60, "end": 632, "title": "Pricing"}],
            "key_points": ["k"], "decisions": ["Team tier to $59"], "action_items": ["Darcy: budget sheet"],
            "entities": ["Elizabeth"], "not_covered": "",
        },
        "stages": {}, "artifacts": [],
    }
    base.update(over)
    return base


def test_overview_renders_from_the_analysis_without_a_model():
    from app.engines import video as engine

    md = engine.overview_markdown(_row())
    for needle in ("**meeting.mp4**", "10:32", "## Summary", "## Chapters", "**[1:00]** Pricing", "## Decisions", "$59", "transcript files are attached"):
        assert needle in md


def test_pinned_block_is_bounded_and_names_a_failed_analysis():
    from app.engines import video as engine

    rows = [_row(), _row(id=8, display_name="second.mp4", status="failed", error="moov atom not found")]
    block = engine.pinned_block(rows, max_chars=1500)
    assert "meeting.mp4 (10:32, meeting, English)" in block
    assert "second.mp4" in block and "analysis failed: moov atom not found" in block
    assert len(block) <= 1500 + 200


def test_follow_up_gate_uses_cues_then_distance(monkeypatch):
    async def scenario():
        from app.engines import video as engine
        from app.video import index

        rows = [_row()]
        assert await engine.is_about_video("what did she say about pricing?", rows)
        assert await engine.is_about_video("summarise the recording", rows)

        async def far(*a, **k):
            return 1.66  # what "write a haiku about autumn" measured against a tutorial

        monkeypatch.setattr(index, "best_distance", far)
        assert not await engine.is_about_video("write a haiku about autumn", rows)

        async def near(*a, **k):
            return 0.6

        monkeypatch.setattr(index, "best_distance", near)
        assert await engine.is_about_video("and the second tier?", rows)

    asyncio.run(scenario())


def test_timestamp_and_visual_cues_are_recognised():
    from app.engines import video as engine

    assert engine.mentioned_times("what is on screen at 14:22 and at 1:02:03?") == [862, 3723]
    assert engine.is_visual("read the code on the slide")
    assert not engine.is_visual("what did they decide")
    assert engine.is_placeholder("Analyze the attached video.")
    assert engine.is_placeholder("")
    assert not engine.is_placeholder("what was decided?")


def test_the_engine_waits_forwards_steps_and_renders_the_overview(monkeypatch):
    async def scenario():
        from app.engines import video as engine
        from app.video import pipeline

        pipeline.reset_for_tests()
        row_running = _row(status="running")
        row_done = _row()
        state = {"row": row_running}

        async def fake_ensure(analysis_id):
            # Simulate the job: publish two stages then finish.
            pipeline._publish(7, {"stage": "transcript", "status": "running", "percent": 40, "detail": "5:00 of 10:32", "elapsed_s": 12})
            pipeline._publish(7, {"stage": "transcript", "status": "done", "percent": 100, "detail": "120 segments", "elapsed_s": 60})
            state["row"] = row_done
            pipeline._publish(7, {"stage": "_done", "status": "done"})
            return True

        async def fake_wait(analysis_id):
            return state["row"]

        monkeypatch.setattr(pipeline, "ensure_running", fake_ensure)
        monkeypatch.setattr(pipeline, "wait_for", fake_wait)
        monkeypatch.setattr(pipeline, "is_running", lambda aid: False)
        monkeypatch.setattr(settings, "reports_dir", "")

        events = []

        async def emit(kind, data):
            events.append((kind, data))

        answer = await engine.run_video_engine("Analyze the attached video.", [row_running], [], emit, conversation_id="c1", effort="fast", user_id=1, attach_turn=True)
        steps = [d for k, d in events if k == "step"]
        assert steps and steps[0]["id"] == 3 and steps[0]["title"] == "Transcribing" and steps[0]["status"] == "running"
        assert steps[-1]["status"] == "done"
        assert "## Summary" in answer
        metas = [d for k, d in events if k == "meta"]
        assert len(metas) == 1 and metas[0]["route"] == "video"
        assert metas[0]["video"]["videos"][0]["chapters"][1]["title"] == "Pricing"

    asyncio.run(scenario())


def test_a_question_goes_to_the_model_with_evidence_and_citations_asked_for(monkeypatch):
    async def scenario():
        from app import llm
        from app.engines import video as engine
        from app.video import index, pipeline

        pipeline.reset_for_tests()
        monkeypatch.setattr(settings, "reports_dir", "")

        async def hits(question, ids, **kw):
            return [{"analysis_id": 7, "modality": "speech", "start_s": 61.0, "end_s": 70.0, "text": "Team goes to fifty-nine dollars", "distance": 0.5}]

        monkeypatch.setattr(index, "retrieve", hits)
        seen = {}

        async def fake_stream(messages, **kw):
            seen["messages"] = messages
            yield ("token", "It rises to $59 [1:01].")

        monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
        events = []

        async def emit(kind, data):
            events.append((kind, data))

        answer = await engine.run_video_engine("what did they decide about the Team tier?", [_row()], [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}], emit, conversation_id="c1", effort="think", user_id=1)
        assert "$59" in answer
        system = seen["messages"][0]["content"]
        assert "Cite timestamps" in system and "doesn't cover" in system
        user = seen["messages"][-1]["content"]
        text_part = next(p["text"] for p in user if p["type"] == "text")
        assert "Team goes to fifty-nine dollars" in text_part and "[1:01-1:10]" in text_part
        assert "QUESTION: what did they decide" in text_part
        meta = next(d for k, d in events if k == "meta")
        assert meta["video"]["evidence"][0]["start"] == 61.0

    asyncio.run(scenario())


# --------------------------------------------------------------- schema --


def test_v28_tables_exist_and_cascade_correctly():
    assert db.LATEST_SCHEMA_VERSION >= 28
    row = db.upsert_video_analysis("b" * 64, 1234, "video/mp4", "clip.mp4")
    assert row["created"] is True and row["status"] == "queued"
    again = db.upsert_video_analysis("b" * 64, 1234, "video/mp4", "clip.mp4")
    assert again["created"] is False and again["id"] == row["id"]
    db.update_video_analysis(row["id"], status="failed", error="boom", stages={"probe": {"status": "failed"}})
    retry = db.upsert_video_analysis("b" * 64, 1234, "video/mp4", "clip.mp4")
    assert retry["status"] == "queued" and retry["error"] == ""
    with pytest.raises(ValueError):
        db.update_video_analysis(row["id"], content_hash="x")

    user_id = int(db.create_user("vid-owner", "hash"))
    db.create_conversation(user_id, "conv-video-1", "t")
    link = db.link_video_attachment(row["id"], "conv-video-1", user_id, "f" * 32, "clip.mp4")
    assert link["conversation_id"] == "conv-video-1"
    videos = db.get_conversation_videos("conv-video-1")
    assert len(videos) == 1 and videos[0]["display_name"] == "clip.mp4" and videos[0]["upload_id"] == "f" * 32
    assert db.get_video_by_upload("conv-video-1", "f" * 32)["id"] == row["id"]
    assert db.get_video_by_upload("conv-video-1", "0" * 32) is None
    # Deleting the conversation clears the link (side table) but not the analysis.
    assert db.delete_conversation(user_id, "conv-video-1") is True
    assert db.get_conversation_videos("conv-video-1") == []
    assert db.get_video_analysis(row["id"]) is not None
    # Orphaned, but inside the grace period — not reaped yet.
    assert db.orphan_video_analyses(24) == []
    with db.connection() as con:
        con.execute("UPDATE video_analyses SET updated_at = updated_at - interval '2 days' WHERE id = %s", (row["id"],))
    assert [r["id"] for r in db.orphan_video_analyses(24)] == [row["id"]]
    # And a restart requeues a running row.
    db.update_video_analysis(row["id"], status="running")
    assert db.requeue_interrupted_video_analyses() == 1
    assert db.get_video_analysis(row["id"])["status"] == "queued"
    assert db.delete_video_analysis(row["id"]) is True


def test_an_analysis_from_an_older_pipeline_is_rerun_not_served(monkeypatch, tmp_path):
    """The 12-second clip was analysed under v1 and cached with no words. A
    fix that never reaches a cached analysis is not a fix: a row stamped
    with an older version re-runs every stage on its next attach."""
    from app.video import pipeline, store

    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    content_hash = "c" * 64
    os.makedirs(store.analysis_dir(content_hash), exist_ok=True)
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00" * 64)
    store.adopt_source(content_hash, str(src), "clip.mp4")
    row = db.upsert_video_analysis(content_hash, 64, "video/mp4", "clip.mp4")
    for stage in pipeline.STAGES:
        store.write_json(store.stage_path(content_hash, pipeline._OUTPUTS[stage]) if pipeline._OUTPUTS.get(stage) else str(tmp_path / f"{stage}.json"), {"old": True})
    db.update_video_analysis(
        row["id"],
        status="done",
        pipeline_version=1,
        stages={s: {"status": "done", "ms": 1, "detail": "old"} for s in pipeline.STAGES},
        counts={"segments": 0},
    )

    ran = []

    async def stub(ctx, progress, _name=None):
        return pipeline._StageResult("done", "fresh")

    monkeypatch.setattr(pipeline, "_STAGE_FNS", {s: (lambda ctx, progress, _s=s: (ran.append(_s), stub(ctx, progress))[1]) for s in pipeline.STAGES})

    async def scenario():
        assert await pipeline.ensure_running(row["id"]) is True
        assert (await db.run_in_thread(db.get_video_analysis, row["id"]))["status"] in ("queued", "running")
        fresh = await pipeline.wait_for(row["id"])
        return fresh

    fresh = asyncio.run(scenario())
    assert sorted(ran) == sorted(pipeline.STAGES), "every stage must run again, none from the cache"
    assert ran[0] == "probe" and ran[-3:] == ["fusion", "index", "artifacts"]  # branches in between, in any order
    assert fresh["status"] == "done" and int(fresh["pipeline_version"]) == pipeline.PIPELINE_VERSION
    assert all(v["detail"] == "fresh" for v in fresh["stages"].values())
    # And now it is current: nothing to do.
    assert asyncio.run(pipeline.ensure_running(row["id"])) is False
    db.delete_video_analysis(row["id"])


def test_speech_and_screen_branches_run_side_by_side(monkeypatch, tmp_path):
    """Transcription and frame reading never read each other's output, so
    they run at the same time and the job is as long as the longer one.
    Fusion must still wait for both."""
    from app.video import pipeline, store

    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    content_hash = "d" * 64
    os.makedirs(store.analysis_dir(content_hash), exist_ok=True)
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00" * 64)
    store.adopt_source(content_hash, str(src), "clip.mp4")
    row = db.upsert_video_analysis(content_hash, 64, "video/mp4", "clip.mp4")

    clock = {"t": 0.0}
    log = []

    def make(name, hold):
        async def run(ctx, progress):
            log.append(("start", name, clock["t"]))
            # A cooperative sleep: every branch advances the shared clock, so
            # overlap shows as a start that lands before another's end.
            for _ in range(hold):
                await asyncio.sleep(0.01)
                clock["t"] += 1
            log.append(("end", name, clock["t"]))
            return pipeline._StageResult("done", name)
        return run

    monkeypatch.setattr(pipeline, "_STAGE_FNS", {
        "probe": make("probe", 1), "audio": make("audio", 1), "transcript": make("transcript", 6),
        "frames": make("frames", 1), "ocr": make("ocr", 2), "vision": make("vision", 2),
        "fusion": make("fusion", 1), "index": make("index", 1), "artifacts": make("artifacts", 1),
    })
    asyncio.run(pipeline._run(row["id"]))
    at = {(kind, name): t for kind, name, t in log}
    assert at[("start", "frames")] < at[("end", "transcript")], "screen branch must not wait for speech"
    assert at[("start", "transcript")] < at[("end", "vision")], "speech branch must not wait for screen"
    assert at[("start", "fusion")] >= max(at[("end", "transcript")], at[("end", "vision")]), "fusion waits for both"
    assert at[("start", "audio")] >= at[("end", "probe")]
    fresh = db.get_video_analysis(row["id"])
    assert fresh["status"] == "done" and set(fresh["stages"]) == set(pipeline.STAGES)
    db.delete_video_analysis(row["id"])


def test_a_fatal_failure_in_one_branch_fails_the_job_after_the_other_finishes(monkeypatch, tmp_path):
    from app.video import pipeline, store

    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    content_hash = "e" * 64
    os.makedirs(store.analysis_dir(content_hash), exist_ok=True)
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00" * 64)
    store.adopt_source(content_hash, str(src), "clip.mp4")
    row = db.upsert_video_analysis(content_hash, 64, "video/mp4", "clip.mp4")
    ran = []

    def make(name, status="done"):
        async def run(ctx, progress):
            ran.append(name)
            return pipeline._StageResult(status, name)
        return run

    fns = {s: make(s) for s in pipeline.STAGES}
    fns["transcript"] = make("transcript", "failed")
    monkeypatch.setattr(pipeline, "_STAGE_FNS", fns)
    asyncio.run(pipeline._run(row["id"]))
    fresh = db.get_video_analysis(row["id"])
    assert fresh["status"] == "failed" and "Transcribing" in fresh["error"]
    assert {"frames", "ocr", "vision"} <= set(ran), "the screen branch still finishes (its files are kept for the retry)"
    assert "fusion" not in ran
    db.delete_video_analysis(row["id"])


def test_transcription_windows_go_out_two_at_a_time_and_come_back_in_order(monkeypatch, tmp_path):
    import math
    import struct
    import wave

    from app import asr
    from app.video import transcribe
    from app.video.vad import SAMPLE_RATE, Window

    wav = tmp_path / "a.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 7.0))) for i in range(SAMPLE_RATE * 8)))
    monkeypatch.setattr(settings, "video_asr_concurrency", 2)
    in_flight = {"now": 0, "peak": 0}

    async def fake_engine(audio, *, filename, content_type, **kwargs):
        in_flight["now"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        i = int(filename[1:5])
        await asyncio.sleep(0.05 if i % 2 else 0.01)  # odd clips finish last
        in_flight["now"] -= 1
        return asr.TranscriptSegments(text=f"w{i}", language="English", language_code="en", provider="t", model="t", engine_ms=1, segments=({"start": 0.0, "end": 1.0, "text": f"w{i}"},))

    monkeypatch.setattr(asr, "transcribe_segments", fake_engine)
    monkeypatch.setattr(transcribe, "plan_windows", lambda *a, **k: ([Window(start_s=float(i * 2), end_s=float(i * 2 + 2)) for i in range(4)], {"detector": "test", "speech_fraction": 1.0}))
    seen = []

    async def progress(pct, detail):
        seen.append((pct, detail))

    segments, language, report = asyncio.run(
        transcribe.transcribe_audio(str(wav), total_s=8.0, progress=progress, max_window_s=90.0, max_gap_s=2.0, overlap_s=1.0)
    )
    assert in_flight["peak"] == 2, "two clips in the engines at once, never more"
    assert [s.text for s in segments] == ["w0", "w1", "w2", "w3"], "time order, whichever clip came back first"
    assert [round(s.start_s) for s in segments] == [0, 2, 4, 6]
    assert report["windows_done"] == 4 and language == "en"
    assert any("in the engine" in d for _p, d in seen)


def test_status_line_names_every_stage_in_flight():
    from app.engines.video import _status_line

    assert _status_line({"transcript": 40.0, "ocr": 30.0}) == "Transcribing 40% · Reading on-screen text 30%…"
    assert _status_line({"ocr": None}) == "Reading on-screen text…"
    assert _status_line({}) == "Analysing the video…"


# ------------------------------------------------------------- the gate --


def test_video_upload_is_gated_per_member_then_per_deployment(monkeypatch, login_client):
    root = login_client("root", role="super_admin")
    bob = login_client("bob")
    monkeypatch.setattr(settings, "video_analysis_enabled", True)
    monkeypatch.setattr(settings, "dataset_uploads_enabled", True)
    me = bob.get("/auth/me").json()
    assert me["features"]["video_analysis"] is True

    bob_id = db.get_user_by_username("bob")["id"]
    r = root.put(f"/admin/api/members/{bob_id}/access", json={"features": {"video_analysis": False}})
    assert r.status_code == 200
    resp = bob.post("/uploads", files={"file": ("a.mp4", b"\x00" * 2048, "video/mp4")}, data={"conversation_id": "conv-vid-gate", "purpose": "video"})
    assert resp.status_code == 403 and "administrator" in resp.json()["detail"]

    root.put(f"/admin/api/members/{bob_id}/access", json={"features": {"video_analysis": True}})
    monkeypatch.setattr(settings, "video_analysis_enabled", False)
    assert bob.get("/auth/me").json()["features"]["video_analysis"] is False
    resp = bob.post("/uploads", files={"file": ("a.mp4", b"\x00" * 2048, "video/mp4")}, data={"conversation_id": "conv-vid-gate", "purpose": "video"})
    assert resp.status_code == 404


def test_video_analysis_is_private_for_sharing():
    from app import sharing

    assert "video" in sharing.PRIVATE_ROUTES
    assert "video" in sharing.PRIVATE_META_KEYS


def test_stage_label_vocabulary_matches_the_pipeline():
    from app import metrics
    from app.video.types import STAGES

    assert set(STAGES) == metrics._ALLOWED["stage"]


def test_the_chat_request_accepts_a_video_only_send():
    from app.main import ChatRequest

    req = ChatRequest(video_uploads=[{"upload_id": "c" * 32, "name": "a.mp4"}])
    assert req.text == "" and req.video_uploads
    with pytest.raises(ValueError):
        ChatRequest()


# ------------------------------------------------- added 2026-09-11 (V3) --


def test_the_evidence_pack_never_reports_an_unread_screen_as_blank():
    """The model reads these lines as facts. 'Nothing legible on screen' is a
    claim about the video; when the OCR reader was down, nobody checked."""
    spans = [OcrSpan(0.0, 10.0, "", kind="", caption=None)]
    blank = fusion.evidence_lines([], spans)
    unread = fusion.evidence_lines([], spans, screen_unread=True)
    assert "nothing legible on screen" in blank[0][1]
    assert "NOT read" in unread[0][1] and "nothing legible" not in unread[0][1]


def test_read_wav_maps_the_track_rather_than_reading_it(tmp_path):
    """The memory bound the transcription stage rests on: a four-hour track
    is 461 MB, and none of it is the process's own memory."""
    import numpy as np

    from app.video.transcribe import read_wav_pcm16

    path = tmp_path / "long.wav"
    path.write_bytes(wav_bytes(_tone(2.0)))
    pcm = read_wav_pcm16(str(path))
    assert isinstance(pcm, np.memmap)
    # A window is a VIEW of the mapping; only `wav_bytes` copies, and only
    # the window it is given.
    window = pcm[16000:32000]
    assert isinstance(window, np.memmap) and window.base is not None
    assert len(wav_bytes(window)) == 44 + 32000
