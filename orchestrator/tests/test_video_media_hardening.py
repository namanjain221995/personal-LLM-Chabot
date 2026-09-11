"""The media stages under failure (2026-09-11): busy engines, killed children
and unread screens.

Three production faults are pinned here, each of which used to be invisible
in a green pipeline:

* V1 — a transcription clip that met a BUSY or UNREACHABLE engine failed the
  whole stage while its siblings kept decoding to the end of the recording,
  holding both batch-pool slots, so the retry met 'every transcription slot
  is busy' and failed again.
* V2 — `media._run` killed its ffmpeg only on its own deadline, so a stage
  timeout or a shutdown left the decoder running and the next attempt wrote
  the same files underneath it.
* V3 — an OCR outage, a batch deadline and a slide with no words all came
  back as '', the stage was stamped done, and (analyses being
  content-addressed) that answer was served from cache for those bytes
  forever. Measured on the live engine on 2026-09-11: the app's prompt
  ("document parsing") looped on four of six real frames and 251 of the 740
  stored frame transcripts on this box are loops, while the prompt "OCR"
  read the same frames correctly.

Everything here is offline: the engines are stubbed at the one seam each
has, and the only real subprocess is `sleep`, standing in for ffmpeg so the
assertion can be made against a process that actually exists.
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from app import asr
from app.config import settings
from app.engines import ocr
from app.video import fusion, media, screen, transcribe, vad
from app.video.artifacts import summary_md
from app.video.frames import KeptFrame
from app.video.types import Limitation, OcrSpan, Segment, Understanding
from app.video.vad import SAMPLE_RATE, Window


async def _quiet(*_args) -> None:
    """A progress callback that says nothing."""
    return None


def _segments(text: str = "hello"):
    return asr.TranscriptSegments(
        text=text, language="English", language_code="en", provider="t", model="t",
        engine_ms=1, segments=({"start": 0.0, "end": 1.0, "text": text},),
    )


def _wav(tmp_path, seconds: float = 4.0):
    import math
    import struct
    import wave

    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(b"".join(
            struct.pack("<h", int(8000 * math.sin(i / 7.0)))
            for i in range(int(SAMPLE_RATE * seconds))
        ))
    return path


def _plan(monkeypatch, count: int, seconds: float = 2.0):
    windows = [Window(start_s=i * seconds, end_s=(i + 1) * seconds) for i in range(count)]
    monkeypatch.setattr(
        transcribe, "plan_windows",
        lambda *a, **k: (windows, {"detector": "test", "speech_fraction": 1.0}),
    )
    return windows


# --------------------------------------------------- V1: a busy engine --


def test_a_busy_engine_is_retried_and_the_clip_still_lands(monkeypatch, tmp_path):
    """ASRBusy is a fact about the engine at this instant, not about the clip.

    The pool refuses after an 8-second queue wait; twenty seconds later the
    same clip transcribes. Failing the stage on the first refusal threw away
    a transcript that was one wait away."""
    wav = _wav(tmp_path)
    _plan(monkeypatch, 1)
    monkeypatch.setattr(transcribe, "_RETRY_BASE_S", 0.01)
    monkeypatch.setattr(transcribe, "_RETRY_CAP_S", 0.05)
    calls = {"n": 0}

    async def engine(audio, *, filename, content_type, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise asr.ASRBusy("every transcription slot is busy")
        return _segments("recovered")

    monkeypatch.setattr(asr, "transcribe_segments", engine)
    segments, language, report = asyncio.run(
        transcribe.transcribe_audio(
            str(wav), total_s=2.0, progress=_quiet, max_window_s=90.0, max_gap_s=2.0, overlap_s=1.0
        )
    )
    assert calls["n"] == 3, "two refusals, then the clip goes through"
    assert [s.text for s in segments] == ["recovered"] and language == "en"
    assert report["windows_failed"] == 0 and report["engine_retries"] == 2


def test_a_clip_that_stays_unavailable_counts_once_against_the_threshold(monkeypatch, tmp_path):
    """Four attempts, then it is one failed window — not four, and not a
    failed video while the rest of the recording still transcribes."""
    wav = _wav(tmp_path, 8.0)
    _plan(monkeypatch, 4)
    monkeypatch.setattr(transcribe, "_RETRY_BASE_S", 0.01)
    monkeypatch.setattr(transcribe, "_RETRY_CAP_S", 0.02)
    calls = {"n": 0}

    async def engine(audio, *, filename, content_type, **kwargs):
        calls["n"] += 1
        if filename.startswith("w0000"):
            raise asr.ASRUnavailable("no speech engine answered")
        return _segments(filename[:5])

    monkeypatch.setattr(asr, "transcribe_segments", engine)
    segments, _language, report = asyncio.run(
        transcribe.transcribe_audio(
            str(wav), total_s=8.0, progress=_quiet, max_window_s=90.0, max_gap_s=2.0, overlap_s=1.0
        )
    )
    assert calls["n"] == transcribe._RETRY_ATTEMPTS + 3, "one clip retried four times, three sent once"
    assert report["windows_failed"] == 1 and report["windows_done"] == 3
    assert [s.text for s in segments] == ["w0001", "w0002", "w0003"], "the gap is a gap, in time order"


def test_a_fatal_clip_cancels_its_siblings_instead_of_leaving_them_decoding(monkeypatch, tmp_path):
    """The bug this test exists for: `gather` propagated the first error and
    left every other window running to the end of the recording, each holding
    one of the two batch-pool slots."""
    wav = _wav(tmp_path, 16.0)
    _plan(monkeypatch, 8)
    monkeypatch.setattr(settings, "video_asr_concurrency", 2)
    state = {"started": 0, "finished": 0, "cancelled": 0}

    async def engine(audio, *, filename, content_type, **kwargs):
        state["started"] += 1
        if filename.startswith("w0000"):
            raise RuntimeError("the engine client is broken")
        try:
            await asyncio.sleep(30)  # a real clip: minutes of decoding
        except asyncio.CancelledError:
            state["cancelled"] += 1
            raise
        state["finished"] += 1
        return _segments(filename[:5])

    monkeypatch.setattr(asr, "transcribe_segments", engine)
    started = time.perf_counter()
    with pytest.raises(RuntimeError):
        asyncio.run(
            transcribe.transcribe_audio(
                str(wav), total_s=16.0, progress=_quiet, max_window_s=90.0, max_gap_s=2.0, overlap_s=1.0
            )
        )
    assert time.perf_counter() - started < 5.0, "the stage fails fast, it does not wait out the siblings"
    assert state["finished"] == 0
    assert state["cancelled"] >= 1, "the sibling in the engine was cancelled, not abandoned"
    assert state["started"] < 8, "windows that had not been sent are never sent"


def test_crossing_the_failure_threshold_ends_the_stage_and_leaves_nothing_running(monkeypatch, tmp_path):
    wav = _wav(tmp_path, 20.0)
    _plan(monkeypatch, 10)
    monkeypatch.setattr(settings, "video_asr_concurrency", 2)
    monkeypatch.setattr(transcribe, "_RETRY_BASE_S", 0.01)
    monkeypatch.setattr(transcribe, "_RETRY_CAP_S", 0.02)
    sent = {"n": 0}

    async def engine(audio, *, filename, content_type, **kwargs):
        sent["n"] += 1
        raise asr.ASRBusy("every transcription slot is busy")

    monkeypatch.setattr(asr, "transcribe_segments", engine)
    with pytest.raises(asr.ASRBusy):
        asyncio.run(
            transcribe.transcribe_audio(
                str(wav), total_s=20.0, progress=_quiet, max_window_s=90.0, max_gap_s=2.0, overlap_s=1.0
            )
        )
    before = sent["n"]

    async def settle():
        await asyncio.sleep(0.1)

    asyncio.run(settle())
    assert sent["n"] == before, "no clip is still asking the engine after the stage gave up"


def test_a_cancelled_transcription_stops_every_clip(monkeypatch, tmp_path):
    """A stage timeout or a shutdown cancels the stage coroutine; the clips
    it dispatched must go with it."""
    wav = _wav(tmp_path, 8.0)
    _plan(monkeypatch, 4)
    monkeypatch.setattr(settings, "video_asr_concurrency", 2)
    state = {"cancelled": 0, "started": 0}

    async def engine(audio, *, filename, content_type, **kwargs):
        state["started"] += 1
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] += 1
            raise
        return _segments()

    monkeypatch.setattr(asr, "transcribe_segments", engine)

    async def scenario():
        task = asyncio.ensure_future(
            transcribe.transcribe_audio(
                str(wav), total_s=8.0, progress=_quiet, max_window_s=90.0, max_gap_s=2.0, overlap_s=1.0
            )
        )
        while state["started"] < 2:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert state["cancelled"] == state["started"] >= 2


# ------------------------------------------------- V2: the ffmpeg child --


def _spy_on_children(monkeypatch, seen: dict):
    real = asyncio.create_subprocess_exec

    async def spy(*argv, **kwargs):
        proc = await real(*argv, **kwargs)
        seen["proc"] = proc
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)


def _assert_reaped(proc) -> None:
    assert proc.returncode is not None, "the child is still running"
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)


def test_a_cancelled_media_call_leaves_no_child_behind(monkeypatch):
    """The V2 bug: only `asyncio.TimeoutError` killed the child, so a stage
    timeout or a shutdown — which arrive as CancelledError — left ffmpeg
    decoding into the very files the retry was about to write."""
    monkeypatch.setattr(media, "tools_available", lambda: True)
    seen: dict = {}
    _spy_on_children(monkeypatch, seen)

    async def scenario():
        task = asyncio.ensure_future(
            media._run(["sleep", "30"], timeout_s=30.0, what="pretending to decode")
        )
        while "proc" not in seen:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(seen["proc"].wait(), timeout=5.0)

    asyncio.run(scenario())
    _assert_reaped(seen["proc"])


def test_a_media_deadline_still_kills_the_child(monkeypatch):
    monkeypatch.setattr(media, "tools_available", lambda: True)
    seen: dict = {}
    _spy_on_children(monkeypatch, seen)

    async def scenario():
        with pytest.raises(media.MediaTimeout) as err:
            await media._run(["sleep", "30"], timeout_s=0.2, what="pretending to decode")
        assert "did not finish within" in str(err.value)
        await asyncio.wait_for(seen["proc"].wait(), timeout=5.0)

    asyncio.run(scenario())
    _assert_reaped(seen["proc"])


def test_a_child_that_ignores_sigterm_is_killed(monkeypatch):
    """SIGTERM, a short grace, then SIGKILL: a wedged decoder must not
    survive the stage that started it."""
    monkeypatch.setattr(media, "tools_available", lambda: True)
    monkeypatch.setattr(media, "_TERM_GRACE_S", 0.2)
    seen: dict = {}
    _spy_on_children(monkeypatch, seen)
    # A shell that ignores SIGTERM stands in for the decoder that will not
    # unwind; nothing else in this file runs a shell.
    argv = ["sh", "-c", "trap '' TERM; sleep 30"]

    async def scenario():
        with pytest.raises(media.MediaTimeout):
            await media._run(argv, timeout_s=0.2, what="pretending to decode")
        await asyncio.wait_for(seen["proc"].wait(), timeout=5.0)

    asyncio.run(scenario())
    _assert_reaped(seen["proc"])
    assert seen["proc"].returncode == -9, "SIGKILL, after the grace period"


def test_every_media_call_is_an_argv_list_with_the_path_as_one_argument(monkeypatch):
    """No shell, ever: a file called `; rm -rf /` is a filename here."""
    source = (media.__file__ or "").replace(".pyc", ".py")
    text = open(source, encoding="utf-8").read()
    assert "create_subprocess_shell" not in text and "shell=True" not in text
    assert "os.system" not in text

    seen: dict = {}

    async def fake_run(argv, *, timeout_s, what):
        seen["argv"] = list(argv)
        return b"{}", b""

    monkeypatch.setattr(media, "_run", fake_run)
    nasty = "/data/video/deadbeef/; rm -rf / $(whoami).mp4"
    with pytest.raises(media.MediaError):
        asyncio.run(media.probe(nasty))  # no streams in '{}' -> MediaError
    argv = seen["argv"]
    assert all(isinstance(a, str) for a in argv)
    assert argv.count(nasty) == 1 and argv[-1] == nasty, "the path is one argument, uninterpolated"


# ----------------------------------------------- the PCM memory bound --


def test_the_energy_detector_reads_the_track_in_blocks(monkeypatch):
    """`read_wav_pcm16` maps the track instead of reading it (461 MB for four
    hours), and the detector must not undo that with one `astype` — but the
    flags have to be identical either way."""
    import numpy as np

    rng = np.random.default_rng(11)
    pcm = (rng.normal(0, 4000, vad.FRAME_SAMPLES * 500)).astype("<i2")
    monkeypatch.setattr(vad, "_BLOCK_FRAMES", 100000)
    whole = vad._frames_speech_energy(pcm)
    monkeypatch.setattr(vad, "_BLOCK_FRAMES", 7)
    blocked = vad._frames_speech_energy(pcm)
    assert blocked == whole and len(whole) == 500


def test_a_partial_final_frame_is_dropped_exactly_as_before(monkeypatch):
    import numpy as np

    pcm = np.zeros(vad.FRAME_SAMPLES * 10 + 13, dtype="<i2")
    monkeypatch.setattr(vad, "_BLOCK_FRAMES", 3)
    assert len(vad._frames_speech_energy(pcm)) == 10


# --------------------------------------------- V3: an unread screen --


_LOOP = "ovišnje pjeski je " + "nije " * 700
_REAL = "Weekly Planning Meeting\nAgenda\n1. Pricing for the Meryton launch\n2. Budget: Q4 allocation"


def test_a_looping_read_is_degenerate_and_a_blank_frame_is_empty():
    """The two must never share an answer: one is a failed read, the other is
    a fact about the video."""
    assert ocr.classify(_LOOP).status == "degenerate"
    assert ocr.classify("").status == "empty"
    assert ocr.classify(_REAL).status == "ok"
    # Real text that happens to repeat a little is still text: this is the
    # engine's own placeholder for a region with no words, eight times over.
    assert not ocr.is_degenerate("Welcome to Colab\n" + "[Non-Text]\n" * 8 + "docs.google.com/document/d/1rh")


def test_degeneracy_is_not_decided_by_length_alone():
    assert not ocr.is_degenerate(_REAL * 12), "a long, varied transcript is a transcript"
    assert ocr.is_degenerate("ovišati" + "ševanja" * 400), "a loop with no spaces in it is still a loop"


def _frames(tmp_path, count: int):
    out = []
    for i in range(count):
        path = tmp_path / f"f_{i:06d}.jpg"
        path.write_bytes(b"\xff\xd8\xff" + bytes([i]) * 64)
        out.append(KeptFrame(path=str(path), t_s=float(i), end_s=float(i + 1), index=i, phash=i, collapsed=1))
    return out


def test_an_ocr_outage_is_recorded_as_unread_not_as_a_blank_screen(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ocr_enabled", True)
    monkeypatch.setattr(settings, "video_ocr_enabled", True)

    async def dead(images, **kwargs):
        return [ocr.OcrRead("", "failed", "APIConnectionError: connection refused") for _ in images]

    monkeypatch.setattr(ocr, "read_images", dead)
    result = asyncio.run(screen.read_frames(_frames(tmp_path, 3), progress=_quiet))
    assert result.texts == ["", "", ""]
    assert [r.status for r in result.reads] == ["failed"] * 3
    assert result.summary["status"] == "unavailable" and result.unavailable
    assert result.summary["unread"] == 3
    assert "connection refused" in result.summary["errors"][0]


def test_a_degenerate_frame_is_unread_and_its_text_never_reaches_the_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ocr_enabled", True)
    monkeypatch.setattr(settings, "video_ocr_enabled", True)
    monkeypatch.setattr(settings, "video_ocr_concurrency", 4)

    async def mixed(images, **kwargs):
        return [ocr.classify(_LOOP), ocr.classify(_REAL), ocr.classify("")]

    monkeypatch.setattr(ocr, "read_images", mixed)
    result = asyncio.run(screen.read_frames(_frames(tmp_path, 3), progress=_quiet))
    assert [r.status for r in result.reads] == ["degenerate", "ok", "empty"]
    assert result.texts[0] == "", "a loop is not on-screen text"
    assert "Weekly Planning Meeting" in result.texts[1]
    assert result.summary["status"] == "partial" and result.summary["degenerate"] == 1
    assert "unreadable" in result.summary["detail"]


def test_the_video_ocr_prompt_is_the_one_that_works_and_can_be_overridden(monkeypatch, tmp_path):
    """Measured 2026-09-11 against the live engine: 'document parsing' looped
    on four of six real frames; 'OCR' read all six."""
    monkeypatch.delenv("VIDEO_OCR_PROMPT", raising=False)
    assert screen.video_ocr_prompt() == "OCR"
    monkeypatch.setenv("VIDEO_OCR_PROMPT", "document parsing")
    assert screen.video_ocr_prompt() == "document parsing"
    monkeypatch.setenv("VIDEO_OCR_PROMPT", "   ")
    assert screen.video_ocr_prompt() == "OCR", "a blank setting is not a prompt"

    monkeypatch.delenv("VIDEO_OCR_PROMPT", raising=False)
    monkeypatch.setattr(settings, "ocr_enabled", True)
    monkeypatch.setattr(settings, "video_ocr_enabled", True)
    seen: dict = {}

    async def capture(images, **kwargs):
        seen.update(kwargs)
        return [ocr.classify(_REAL) for _ in images]

    monkeypatch.setattr(ocr, "read_images", capture)
    result = asyncio.run(screen.read_frames(_frames(tmp_path, 1), progress=_quiet))
    assert seen["prompt"] == "OCR"
    assert result.summary["prompt"] == "OCR"


def test_the_document_prompt_is_unchanged_by_default_but_has_a_lever(monkeypatch):
    """The document and interactive image routes make the SAME call with the
    SAME prompt, and it is degenerate here too: one real PDF page from this
    deployment came back as 16,775 characters at a unique-token ratio of
    0.002 with 'document parsing' and 948 varied characters with 'OCR'
    (2026-09-11). That route is not this change's subject, so the default
    stands and OCR_PROMPT is the way to move it."""
    monkeypatch.delenv("OCR_PROMPT", raising=False)
    assert ocr.document_prompt() == "document parsing"
    monkeypatch.setenv("OCR_PROMPT", "OCR")
    assert ocr.document_prompt() == "OCR"


def test_a_batch_deadline_keeps_what_was_read_and_fails_only_the_rest(monkeypatch):
    """The deadline was documented as dropping the transcripts still IN
    FLIGHT; it dropped the finished ones too."""
    monkeypatch.setattr(settings, "ocr_enabled", True)

    async def one(client, image, max_tokens=None, prompt=None):
        if image == "slow":
            await asyncio.sleep(30)
        return _REAL

    monkeypatch.setattr(ocr, "_ocr_one", one)
    monkeypatch.setattr(ocr, "concurrency", lambda: 4)
    reads = asyncio.run(ocr.read_images(["fast", "slow"], deadline_s=0.2))
    assert [r.status for r in reads] == ["ok", "failed"]
    assert "deadline" in reads[1].error
    assert reads[0].text.startswith("Weekly Planning Meeting")


def test_ocr_images_still_answers_the_old_callers_with_strings(monkeypatch):
    """The document and interactive image routes are untouched: '' for a read
    that did not happen, the text otherwise."""
    monkeypatch.setattr(settings, "ocr_enabled", True)

    async def one(client, image, max_tokens=None, prompt=None):
        if image == "bad":
            raise RuntimeError("the engine is down")
        return _REAL

    monkeypatch.setattr(ocr, "_ocr_one", one)
    texts = asyncio.run(ocr.ocr_images(["good", "bad"]))
    assert texts[0].startswith("Weekly Planning Meeting") and texts[1] == ""


# ---------------------------------------- V3: what the person is told --


def _understanding_with(monkeypatch, limitations, *, said="The meeting covered pricing."):
    captured: dict = {}

    async def fake_ask(system, user, *, max_tokens):
        captured["user"] = user
        return {
            "summary": said, "key_points": [], "decisions": [], "action_items": [],
            "entities": [], "chapters": [], "content_type": "meeting",
            "not_covered": "Nothing else was discussed.",
        }

    monkeypatch.setattr(fusion, "_ask", fake_ask)
    u = asyncio.run(fusion.understand(
        filename="clip.mp4", duration_s=60.0, language="en", has_audio=True,
        speech_fraction=0.8,
        segments=[Segment(0.0, 5.0, "Good morning everyone.")],
        spans=[OcrSpan(0.0, 5.0, "", kind="", caption=None)],
        progress=_quiet, limitations=limitations,
    ))
    return u, captured


def test_an_ocr_outage_reaches_the_understanding_as_a_sentence(monkeypatch):
    lims = fusion.limitations_from_reports(ocr={"status": "unavailable", "frames": 12, "unread": 12})
    u, captured = _understanding_with(monkeypatch, lims)
    assert [lim.stage for lim in u.limitations] == ["ocr"]
    assert "On-screen text was NOT read" in u.not_covered
    assert "missing from this analysis rather than absent" in u.not_covered
    assert "LIMITS OF THIS EVIDENCE" in captured["user"], "the model is told before it writes"
    assert "the on-screen text was NOT read for this frame" in captured["user"], (
        "a frame nobody read is not reported to the model as a blank screen"
    )


def test_the_limitation_survives_a_model_that_omits_it(monkeypatch):
    lims = [Limitation("ocr", "On-screen text was NOT read for this video.")]

    async def fake_ask(system, user, *, max_tokens):
        return {
            "summary": "A quiet video.", "key_points": [], "decisions": [], "action_items": [],
            "entities": [], "chapters": [], "content_type": "other", "not_covered": "",
        }

    monkeypatch.setattr(fusion, "_ask", fake_ask)
    u = asyncio.run(fusion.understand(
        filename="clip.mp4", duration_s=10.0, language=None, has_audio=True, speech_fraction=0.5,
        segments=[Segment(0.0, 2.0, "hello")], spans=[], progress=_quiet, limitations=lims,
    ))
    assert u.not_covered == "On-screen text was NOT read for this video."


def test_a_video_with_no_evidence_says_why_when_a_reader_was_down():
    lims = fusion.limitations_from_reports(
        ocr={"status": "unavailable", "frames": 4, "unread": 4},
        transcript={"reason": "asr disabled"},
    )
    u = asyncio.run(fusion.understand(
        filename="clip.mp4", duration_s=30.0, language=None, has_audio=True, speech_fraction=0.0,
        segments=[], spans=[], progress=_quiet, limitations=lims,
    ))
    assert u.method == "empty"
    assert "no readable on-screen content" not in u.not_covered, "that would blame the video"
    assert "On-screen text was NOT read" in u.not_covered
    assert "speech-to-text is turned off" in u.not_covered
    assert {lim.stage for lim in u.limitations} == {"ocr", "transcript"}


def test_limitation_sentences_name_the_stage_that_was_unavailable():
    lims = fusion.limitations_from_reports(
        ocr={"status": "partial", "frames": 20, "unread": 7},
        captions={"status": "unavailable", "frames": 20, "described": 0},
        transcript={"windows_failed": 2},
    )
    by_stage = {lim.stage: lim.sentence for lim in lims}
    assert "7 of 20" in by_stage["ocr"]
    assert "vision model returned nothing" in by_stage["vision"]
    assert "2 clip(s)" in by_stage["transcript"]
    assert all(sentence.endswith(".") for sentence in by_stage.values())


def test_a_complete_analysis_carries_no_limitations():
    assert fusion.limitations_from_reports(
        ocr={"status": "ok", "frames": 9, "unread": 0},
        captions={"status": "ok", "frames": 9, "described": 9},
        transcript={"windows_failed": 0},
    ) == []
    assert fusion.limitations_from_reports() == [], "an old analysis is described as it always was"


def test_the_summary_document_says_which_stage_was_unavailable():
    u = Understanding(
        content_type="meeting", summary="Pricing was discussed.",
        not_covered="On-screen text was NOT read for this video.",
        limitations=[Limitation("ocr", "On-screen text was NOT read for this video: the reader failed on all 12 sampled frame(s).")],
    )
    md = summary_md(u, title="clip.mp4", duration_s=60.0, language="en")
    assert "## What this analysis could not see" in md
    assert "the reader failed on all 12 sampled frame(s)" in md
    assert md.index("## What this analysis could not see") < md.index("## Not covered")


def test_the_understanding_round_trips_its_limitations():
    u = Understanding(summary="s", limitations=[Limitation("vision", "The frames were not described.")])
    back = Understanding.from_json(u.to_json())
    assert back.limitations == u.limitations
    assert Understanding.from_json({"summary": "s"}).limitations == [], "old files still load"


# --------------------------------------------------- the fleet's memory ---


def test_a_persistently_broken_engine_is_stood_down_for_longer_each_time(monkeypatch):
    """2026-09-10: one node's speech engine answered /health for twelve hours
    while every transcription on it died with a CUDA error. A fixed 20 s
    stand-down meant a clip was fed to it every 20 s, failed after the round
    trip, and was retried elsewhere — half the fleet dead, the work serialised
    onto one node, and nothing saying so.

    The clock is driven here rather than waited on: what is under test is the
    length of the next stand-down, not the passage of time.
    """
    import asyncio

    from app import asr

    clock = {"t": 1000.0}
    monkeypatch.setattr(asr.time, "monotonic", lambda: clock["t"])

    class Dead:
        name, model, base_url = "dead", "m", "http://dead:30007/v1"

        async def transcribe_segments(self, audio, **kwargs):
            raise asr.ASRUnavailable("CUDA error: unknown error")

        async def health(self):
            return True  # exactly what the broken engine did, for twelve hours

    class Live:
        name, model, base_url = "live", "m", "http://live:30007/v1"

        async def transcribe_segments(self, audio, **kwargs):
            return asr.TranscriptSegments(
                text="ok", language="English", language_code="en",
                provider="live", model="m", engine_ms=1,
                segments=({"start": 0.0, "end": 1.0, "text": "ok"},),
            )

        async def health(self):
            return True

    router = asr.RoutedProvider([Dead(), Live()])
    waits = []

    async def scenario():
        for _ in range(4):
            out = await router.transcribe_segments(b"x", filename="a.wav", content_type="audio/wav")
            assert out.text == "ok", "the live engine keeps answering throughout"
            dead = router.stats()[0]
            waits.append(dead["standing_down_for_s"])
            # Let the stand-down lapse, so the next call tries it again — which
            # is what a long transcription does over and over.
            clock["t"] += dead["standing_down_for_s"] + 1

    asyncio.run(scenario())
    assert waits == sorted(waits) and waits[-1] > waits[0], f"the wait must grow: {waits}"
    assert waits[0] == asr.RoutedProvider._COOLDOWN_S, waits
    assert waits[-1] <= asr.RoutedProvider._COOLDOWN_MAX_S, waits
    dead, live = router.stats()
    assert dead["consecutive_failures"] == 4, "the count is what makes this visible"
    assert live["consecutive_failures"] == 0 and live["available"] is True
    # /health alone would have called the dead engine available the whole time.
    assert asyncio.run(Dead().health()) is True


def test_one_success_forgives_an_engine_completely():
    """A node that reboots must rejoin by itself, with no penalty carried."""
    import asyncio

    from app import asr

    class Flaky:
        name, model, base_url = "flaky", "m", "http://flaky:30007/v1"
        fail = True

        async def transcribe_segments(self, audio, **kwargs):
            if Flaky.fail:
                raise asr.ASRUnavailable("down")
            return asr.TranscriptSegments(
                text="back", language="English", language_code="en",
                provider="flaky", model="m", engine_ms=1, segments=(),
            )

        async def health(self):
            return True

    router = asr.RoutedProvider([Flaky()])

    async def scenario():
        for _ in range(3):
            try:
                await router.transcribe_segments(b"x", filename="a.wav", content_type="audio/wav")
            except asr.ASRUnavailable:
                pass
        assert router.stats()[0]["consecutive_failures"] == 3
        Flaky.fail = False
        out = await router.transcribe_segments(b"x", filename="a.wav", content_type="audio/wav")
        assert out.text == "back"
        return router.stats()[0]

    after = asyncio.run(scenario())
    assert after["consecutive_failures"] == 0
    assert after["standing_down_for_s"] == 0 and after["available"] is True
