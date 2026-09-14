"""Audio of any length: windows, decode, dispatch, cache, stitch (2026-09-13).

The engine is `publicapi_fake_whisper.FakeWhisper` — the speech server's
contract over a decodable synthetic recording — reached through an in-process
transport, and the decoder is an ffmpeg stand-in that honours the real argv.
Nothing reaches a network, a GPU or a production engine.

NO DATABASE. Every test here works on files and in-process state; none reads
or writes a table. The suite's autouse fixture TRUNCATEs ~60 tables before
every test, which measured 1.45 s per test on the shared fsync-on test server
(2026-09-13, three empty tests: 3.08 s, 1.46 s, 1.45 s of setup), so the two
database fixtures are overridden below for this module only. A test added here
that needs the database must not live in this module.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
import numpy as np
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from tests import publicapi_fake_whisper as fw
from app.publicapi import audio_jobs as aj
from app.publicapi import capacity, disk_ledger, errors

from app.video.vad import Window


@pytest.fixture(scope="session")
def app_database():
    """Overridden: this module touches no table (see the module docstring)."""
    yield None


@pytest.fixture()
def isolated_app_db():
    """Overridden: no TRUNCATE for tests that touch no table."""
    yield


# ------------------------------------------------------------ the world --


SPEECH_CACHE: Dict[tuple, tuple] = {}


@pytest.fixture(scope="module")
def speech_dir(tmp_path_factory):
    return str(tmp_path_factory.mktemp("speech"))


def speech(speech_dir: str, seconds: float, seed: int = 7):
    """(script, wav path) for a generated recording, built once per module."""
    key = (seconds, seed)
    if key not in SPEECH_CACHE:
        script = fw.build_script(seconds, seed=seed)
        path = os.path.join(speech_dir, f"speech-{int(seconds)}-{seed}.wav")
        fw.write_speech_wav(path, script, seed=seed * 13)
        SPEECH_CACHE[key] = (script, path)
    return SPEECH_CACHE[key]


@dataclass
class World:
    root: str
    tools: Dict[str, str]
    fleet: fw.Fleet
    jobs: aj.AudioJobs
    ffmpeg_log: str
    rundir: str
    order: List[str]


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """Fake tools on disk, a two-replica fake fleet, a registry wired to both."""
    capacity.reset_for_tests()
    disk_ledger.reset_for_tests()
    tools = fw.install_fake_media_tools(str(tmp_path / "bin"))
    log_path = str(tmp_path / "ffmpeg.jsonl")
    rundir = str(tmp_path / "running")
    monkeypatch.setenv("FAKE_FFMPEG_LOG", log_path)
    monkeypatch.setenv("FAKE_FFMPEG_RUNDIR", rundir)
    monkeypatch.setenv("PUBLIC_API_ASR_HEALTH_POLL_S", "0.05")
    monkeypatch.setattr(capacity, "YIELD_STEP_S", 0.02)
    # WHY (2026-09-13): the tmp root shares a device with the default
    # PUBLIC_API_FILES_DIR's nearest ancestor on this host, so the ledger would
    # enforce the Files watermark (250 GiB) and the suite would need 250 GiB
    # free to run. Zeroing the Files watermark leaves the 20 GiB ASR floor, as
    # before; the watermark itself is pinned by the tests in the disk section.
    monkeypatch.setenv("PUBLIC_API_FILES_MIN_FREE_GIB", "0")
    fleet = fw.Fleet()
    order = list(fleet.urls())
    root = str(tmp_path / "asr")

    def build(**overrides: Any) -> aj.AudioJobs:
        kwargs: Dict[str, Any] = dict(
            root=root,
            decoder=aj.Decoder(ffmpeg=tools["ffmpeg"], ffprobe=tools["ffprobe"]),
            dispatcher_factory=lambda: aj.WhisperDispatcher(
                replicas=lambda: list(order), transport=fleet.transport()
            ),
            model="whisper-test",
        )
        kwargs.update(overrides)
        return aj.AudioJobs(**kwargs)

    w = World(root, tools, fleet, build(), log_path, rundir, order)
    w.build = build  # type: ignore[attr-defined]
    yield w
    capacity.reset_for_tests()
    disk_ledger.reset_for_tests()


def run(coro):
    return asyncio.run(coro)


async def transcribe_path(jobs: aj.AudioJobs, path: str, *, project: str = "proj_a", fmt: str = "verbose_json", owned: bool = False, language: Optional[str] = None):
    source = await aj.AudioSource.from_path(path, owned=owned)
    spec = aj.JobSpec.for_request(project_id=project, sha256=source.sha256, language=language, response_format=fmt)
    job = await jobs.start(spec, source)
    return job, await job.wait()


def windows_sent(fleet: fw.Fleet) -> int:
    return sum(1 for call in fleet.calls if "seconds" in call)


class AnonSampler:
    """Peak RssAnon above a baseline, sampled every 10 ms from a thread."""

    def __init__(self) -> None:
        self.baseline = fw.rss_anon_bytes()
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, fw.rss_anon_bytes() - self.baseline)
            time.sleep(0.01)

    def __enter__(self) -> "AnonSampler":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()


def assert_monotonic(segments) -> None:
    previous_end = 0.0
    for segment in segments:
        assert segment.start_s >= previous_end - 1e-6, (segment, previous_end)
        assert segment.end_s >= segment.start_s
        previous_end = segment.end_s


# ------------------------------------------------------ the acceptance run --


def test_a_two_hour_recording_transcribes_word_for_word_with_true_timestamps_in_bounded_memory(world, speech_dir):
    """The owner's acceptance: 2 hours → every word once, in order, at its time.

    MEMORY. The recording is 230 MB of PCM. A 10-minute job runs first to warm
    the allocator; the 2-hour job must then peak within 16 MiB of it, and under
    64 MiB above the baseline in absolute terms. Measured when this was written
    (five runs): 27.0 MiB after the 10-minute job, 28.1-30.5 MiB after the
    2-hour one — the audio is a memory map, one clip is in memory at a time,
    and what grows with duration is the transcript text and the plan's
    per-30-ms flags.
    """
    short_script, short_path = speech(speech_dir, 600, seed=3)
    long_script, long_path = speech(speech_dir, 7200, seed=7)
    assert os.path.getsize(long_path) == 7200 * 32000 + 44

    async def scenario():
        with AnonSampler() as sampler:
            _, short = await transcribe_path(world.jobs, short_path, project="proj_warm")
            short_peak = sampler.peak
            job, result = await transcribe_path(world.jobs, long_path)
            long_peak = sampler.peak
        return short, short_peak, job, result, long_peak

    short, short_peak, job, result, long_peak = run(scenario())

    assert fw.compare_to_script(short.segments, short_script)["equal"]
    verdict = fw.compare_to_script(result.segments, long_script)
    # The measurement, for `pytest -s` and for whoever retunes the bounds.
    print(json.dumps({
        "words": verdict["words"], "equal": verdict["equal"],
        "max_start_error_s": verdict.get("max_start_error_s"), "max_end_error_s": verdict.get("max_end_error_s"),
        "windows": result.report["windows"], "stitch": result.report["stitch"],
        "anon_peak_mib_10min": round(short_peak / 2**20, 1), "anon_peak_mib_2h": round(long_peak / 2**20, 1),
    }))
    assert verdict["equal"], verdict
    assert verdict["duplicates"] == 0 and verdict["missing"] == 0
    assert verdict["words"] == len(long_script.words) > 10_000
    # Segment times against the TRUE word times of the recording: the 20 ms
    # timestamp grid plus the 10 ms burst frames plus the interpolated edge of
    # a cue trimmed at a seam. Measured max 0.188 s start, 0.075 s end.
    assert verdict["max_start_error_s"] <= 0.25, verdict
    assert verdict["max_end_error_s"] <= 0.25, verdict
    assert_monotonic(result.segments)

    report = result.report
    # webrtcvad when installed (requirements-dev.txt installs it, as CI and the
    # image do), the energy detector otherwise — as test_video_understanding.
    assert report["plan"]["detector"] in ("webrtcvad", "energy")
    assert report["stitch"]["seams_aligned"] >= 10, report["stitch"]  # overlaps really happened
    assert report["longest_clip_s"] <= 90.0
    assert all(call["seconds"] <= 93.0 for call in world.fleet.calls)
    assert world.fleet.peak_in_flight == 1, "one window at a time, fleet-wide"
    assert result.usage_seconds == 7200 == math.ceil(7200 * 16000 / 16000)
    assert result.text == " ".join(s.text for s in result.segments)

    assert os.path.exists(long_path), "a source the job does not own is never deleted"
    assert not os.listdir(os.path.join(world.root, "jobs")), "decoded PCM removed at the end"
    assert world.jobs.ledger.outstanding_bytes() == 0

    assert long_peak - short_peak < 16 * 1024 * 1024, (short_peak, long_peak)
    assert long_peak < 64 * 1024 * 1024, long_peak


# -------------------------------------------------------------- windows --


def test_windows_are_at_most_ninety_seconds_and_overlap_only_inside_continuous_speech(speech_dir):
    script, path = speech(speech_dir, 1200)
    with open(path, "rb") as fh:
        fh.seek(44)
        pcm = np.frombuffer(fh.read(), dtype="<i2")
    windows, report = aj.plan(pcm, total_s=1200, window=90.0, overlap=3.0, gap=2.0)
    assert report["detector"] in ("webrtcvad", "energy")
    assert windows and all(w.duration_s <= 90.0 + 1e-6 for w in windows)
    overlapping = [w for w in windows if w.overlaps_previous]
    assert overlapping, "the script has stretches longer than one window"
    for previous, current in zip(windows, windows[1:]):
        if current.overlaps_previous:
            assert previous.end_s - current.start_s == pytest.approx(3.0, abs=1e-6)
            # inside one continuous stretch of the script
            assert any(a <= current.start_s and previous.end_s <= b for a, b in script.continuous)
        else:
            assert current.start_s >= previous.end_s
    # every word is inside some window
    for word in script.words:
        assert any(w.start_s <= word.start_s and word.end_s <= w.end_s for w in windows), word


def test_a_detector_that_raises_falls_back_to_fixed_windows_and_still_loses_no_word(world, speech_dir):
    script, path = speech(speech_dir, 1200)

    def broken(*_args, **_kwargs):
        raise RuntimeError("detector bug")

    jobs = world.build(planner=broken)
    _, result = run(transcribe_path(jobs, path))
    assert result.report["plan"] == {"detector": "fixed", "reason": "detector_failed", "windows": 14}
    verdict = fw.compare_to_script(result.segments, script)
    assert verdict["equal"], verdict  # fixed windows cut mid-word, anywhere
    stitch = result.report["stitch"]
    assert stitch["seams_aligned"] + stitch["seams_split_by_time"] == 13
    assert stitch["seams_aligned"] >= 10


def test_a_plan_that_breaks_the_window_rules_is_replaced_by_fixed_windows():
    def too_long(*_args, total_s, **_kwargs):
        return [Window(0.0, 120.0)], {"detector": "energy"}

    windows, report = aj.plan(None, total_s=200.0, window=90.0, overlap=3.0, gap=2.0, planner=too_long)
    assert report["reason"] == "plan_invalid"
    assert [(w.start_s, w.end_s, w.overlaps_previous) for w in windows] == [
        (0.0, 90.0, False),
        (87.0, 177.0, True),
        (174.0, 200.0, True),
    ]

    def silent_overlap(*_args, total_s, **_kwargs):
        # Overlapping, but not flagged: nothing would de-duplicate the seam.
        return [Window(0.0, 60.0), Window(50.0, 100.0)], {"detector": "energy"}

    _, report = aj.plan(None, total_s=100.0, window=90.0, overlap=3.0, gap=2.0, planner=silent_overlap)
    assert report["reason"] == "plan_invalid"


# --------------------------------------------------------------- stitch --


def test_the_seam_keeps_one_copy_of_the_overlap_and_drops_both_cut_words():
    """Spoken at 0.5 s a word from 84.9 s: "the quick brown fox jumps over the
    lazy dog and runs away". The first clip ends at 90.0 inside "runs" and
    hears "ru"; the second starts at 87.0 inside "jumps" and hears "ps", with
    its own capitalisation and punctuation."""
    windows = [Window(0.0, 90.0), Window(87.0, 170.0, overlaps_previous=True)]
    stitcher = aj.Stitcher(windows)
    first = stitcher.feed(
        0,
        [
            {"start": 60.0, "end": 64.0, "text": "Once upon"},
            {"start": 65.0, "end": 70.0, "text": "a time"},
            {"start": 84.9, "end": 86.8, "text": "The quick brown fox"},
            {"start": 86.9, "end": 90.0, "text": "jumps over the lazy dog and ru"},
        ],
    )
    # Two cues are held back, and so is every cue the next overlap may trim.
    assert [s.text for s in first] == ["Once upon", "a time"]
    second = stitcher.feed(1, [{"start": 0.0, "end": 3.9, "text": "ps over the lazy dog and runs away."}])
    tail = stitcher.finish()
    out = first + second + tail
    words = [fw.norm_word(w) for w in " ".join(s.text for s in out).split()]
    assert words == "once upon a time the quick brown fox jumps over the lazy dog and runs away".split()
    assert stitcher.report["seams_aligned"] == 1 and stitcher.report["seam_words_dropped"] == 7
    assert_monotonic(out)
    joined = [s for s in out if "runs" in s.text][0]
    assert joined.end_s == pytest.approx(90.9, abs=0.01)


def test_cues_the_next_overlap_may_trim_stay_held_even_when_the_last_two_are_garbled():
    """One word per cue inside the overlap, and a clip edge that left two
    garbled cues last. Held back by the 2-cue rule alone, "over the lazy dog"
    would already be streamed when the second clip repeats it, and the
    alignment would find nothing to match in the two garbled cues."""
    windows = [Window(0.0, 90.0), Window(87.0, 170.0, overlaps_previous=True)]
    stitcher = aj.Stitcher(windows)
    cues = [("jumps", 86.9, 87.3), ("over", 87.4, 87.8), ("the", 87.9, 88.3), ("lazy", 88.4, 88.8), ("dog", 88.9, 89.3), ("xn", 89.4, 89.7), ("ru", 89.8, 90.0)]
    first = stitcher.feed(0, [{"start": a, "end": b, "text": t} for t, a, b in cues])
    assert first == []
    rest = stitcher.feed(1, [{"start": 0.0, "end": 3.9, "text": "ps over the lazy dog and runs away."}]) + stitcher.finish()
    words = [fw.norm_word(w) for w in " ".join(x.text for x in first + rest).split()]
    assert words == "jumps over the lazy dog and runs away".split()


def test_a_seam_with_no_shared_words_is_split_at_the_overlap_midpoint_without_losing_words():
    windows = [Window(0.0, 90.0), Window(87.0, 170.0, overlaps_previous=True)]
    stitcher = aj.Stitcher(windows)
    out = stitcher.feed(0, [{"start": 86.0, "end": 88.0, "text": "alpha bravo"}])
    out += stitcher.feed(1, [{"start": 1.5, "end": 3.5, "text": "charlie delta"}])
    out += stitcher.finish()
    assert " ".join(s.text for s in out) == "alpha bravo charlie delta"
    assert stitcher.report["seams_split_by_time"] == 1


def test_streamed_deltas_are_never_retracted_and_join_exactly_into_the_final_text(world, speech_dir):
    script, path = speech(speech_dir, 1200)

    async def scenario():
        source = await aj.AudioSource.from_path(path)
        spec = aj.JobSpec.for_request(project_id="proj_a", sha256=source.sha256, language=None, response_format="json")
        job = await world.jobs.start(spec, source)
        deltas: List[str] = []
        stages = set()
        async with job.follow() as follower:
            while True:
                event = await follower.next(5.0)
                assert event is not None
                if event.type == "delta":
                    deltas.append(event.data["delta"])
                elif event.type in ("queued", "progress"):
                    stages.add(event.data.get("stage"))
                elif event.type == "done":
                    return job.result, deltas, stages
                else:
                    raise AssertionError(event)

    result, deltas, stages = run(scenario())
    assert len(deltas) > 5
    assert "".join(deltas) == result.text
    assert fw.normalized_words(result.text) == [fw.norm_word(w.text) for w in script.words]
    # Progress is latest-state (`Job.publish` keeps only the newest queued or
    # progress event), so a follower that attaches after the job has moved on
    # legitimately never sees "queued": seen flaking 1 run in 3 on Python 3.11
    # (2026-09-14). The stages a 1,200 s job must pass through are still seen.
    assert {"decoding", "transcribing"} <= stages
    assert result.body("json") == {"text": result.text, "usage": {"type": "duration", "seconds": 1200}}
    assert result.body("text") == result.text


def test_a_decoder_loop_inside_a_window_is_collapsed_out_of_the_transcript(world, speech_dir):
    script, path = speech(speech_dir, 600, seed=3)
    world.fleet["asr-head"].loop_on_calls = {1}
    _, result = run(transcribe_path(world.jobs, path))
    words = fw.normalized_words(result.text)
    assert words.count("no") == 2, "a 40-copy loop keeps two copies (video/loops.py)"
    assert [w for w in words if w != "no"] == [fw.norm_word(w.text) for w in script.words]
    assert result.report["stitch"]["loop_chars_removed"] > 0


# ------------------------------------------------------------- dispatch --


def test_a_retry_after_five_windows_sends_only_the_windows_that_never_finished(world, speech_dir, monkeypatch):
    script, path = speech(speech_dir, 1200)
    monkeypatch.setenv("PUBLIC_API_ASR_UNAVAILABLE_GRACE_S", "0")
    del world.order[1:]
    head = world.fleet["asr-head"]
    head.fail_status, head.fail_after_calls = 503, 5

    async def scenario():
        with pytest.raises(errors.ApiError) as refused:
            await transcribe_path(world.jobs, path)
        first_calls = windows_sent(world.fleet)
        head.fail_status = None
        _, result = await transcribe_path(world.jobs, path)
        return refused.value, first_calls, result

    refusal, first_calls, result = run(scenario())
    assert refusal.code == "model_unavailable" and refusal.status == 503 and refusal.retry_after
    assert first_calls == 5
    total_windows = result.report["windows"]
    assert result.report["windows_cached"] == 5
    assert windows_sent(world.fleet) - first_calls == total_windows - 5
    assert fw.compare_to_script(result.segments, script)["equal"]


def test_a_replica_that_turns_not_ready_mid_window_is_failed_over_at_once(world, speech_dir, monkeypatch):
    script, path = speech(speech_dir, 600, seed=3)
    # Without the health watch the silence rule would still rescue the window,
    # after 10 s: the elapsed-time bound below tells the two apart, fast.
    monkeypatch.setenv("PUBLIC_API_ASR_WINDOW_SILENCE_S", "10")
    head, worker = world.fleet["asr-head"], world.fleet["asr-worker"]
    head.hang_calls = {3}

    def on_call(number: int) -> None:
        if number == 3:
            asyncio.get_running_loop().call_later(0.2, lambda: setattr(head, "ready", False))

    head.on_call = on_call
    started = time.monotonic()
    _, result = run(transcribe_path(world.jobs, path))
    assert time.monotonic() - started < 8
    assert fw.compare_to_script(result.segments, script)["equal"]
    assert len(head.calls) == 3 and len(worker.calls) >= 1
    assert result.report["dispatch"]["silences"] == 0
    assert result.report["dispatch"]["failovers"] >= 1


def test_a_rising_cuda_failure_count_fails_the_window_over_even_while_ready(world, speech_dir, monkeypatch):
    script, path = speech(speech_dir, 600, seed=3)
    monkeypatch.setenv("PUBLIC_API_ASR_WINDOW_SILENCE_S", "10")
    head, worker = world.fleet["asr-head"], world.fleet["asr-worker"]
    head.hang_calls = {2}

    def on_call(number: int) -> None:
        if number == 2:
            asyncio.get_running_loop().call_later(0.2, lambda: setattr(head, "cuda_failures", 1))

    head.on_call = on_call
    started = time.monotonic()
    _, result = run(transcribe_path(world.jobs, path))
    assert time.monotonic() - started < 8
    assert fw.compare_to_script(result.segments, script)["equal"]
    assert head.ready and len(worker.calls) >= 1
    assert result.report["dispatch"]["silences"] == 0


def test_a_window_silent_on_a_ready_replica_is_resent_once_to_the_other_and_a_second_silence_fails_retry_safe(world, speech_dir, monkeypatch):
    script, path = speech(speech_dir, 600, seed=3)
    monkeypatch.setenv("PUBLIC_API_ASR_WINDOW_SILENCE_S", "0.4")
    head, worker = world.fleet["asr-head"], world.fleet["asr-worker"]
    head.hang_calls = {2}
    _, result = run(transcribe_path(world.jobs, path))
    assert fw.compare_to_script(result.segments, script)["equal"]
    assert result.report["dispatch"]["silences"] == 1 and len(worker.calls) == 1

    # Both replicas silent on the same window: model_unavailable, and the
    # windows already finished stay cached for the retry.
    other_script, other_path = speech(speech_dir, 600, seed=5)
    head.calls.clear()
    worker.calls.clear()
    head.hang_calls = {3}
    worker.hang_calls = {1}
    with pytest.raises(errors.ApiError) as refused:
        run(transcribe_path(world.jobs, other_path))
    assert refused.value.code == "model_unavailable" and refused.value.retry_after
    cached = sum(len(files) for _, _, files in os.walk(os.path.join(world.root, "windows")))
    assert cached >= 2 + result.report["windows"]


def test_while_dictation_needs_a_replica_windows_wait_instead_of_failing(world, speech_dir, monkeypatch):
    script, path = speech(speech_dir, 600, seed=3)
    busy = {"until": 0.0}
    monkeypatch.setattr(capacity, "dictation_is_busy", lambda: time.monotonic() < busy["until"])

    async def scenario():
        busy["until"] = time.monotonic() + 1.5
        source = await aj.AudioSource.from_path(path)
        spec = aj.JobSpec.for_request(project_id="proj_a", sha256=source.sha256, language=None, response_format="json")
        job = await world.jobs.start(spec, source)
        failed = 0
        async with job.follow() as follower:
            while True:
                event = await follower.next(5.0)
                if event.type == "failed":
                    failed += 1
                if event.type in ("done", "failed"):
                    break
        return job, failed

    job, failed = run(scenario())
    assert job.state == "done", job.error
    # Assembler, 2026-09-14: this counted `queued` events carrying `requeued`,
    # which only the pre-T2 re-queue shim of `audio_jobs.capacity_gate` emits
    # (each finite hold that ran out). With T2's `capacity.hold(wait_s=None)`
    # the window waits ONCE in the gate's dictation loop, so there is nothing
    # to re-queue; the wait itself is what the first-call assertion proves.
    assert failed == 0
    first_call = min(call["started"] for call in world.fleet.calls)
    assert first_call >= busy["until"] - 0.05
    assert fw.compare_to_script(job.result.segments, script)["equal"]


def test_identical_audio_in_another_project_is_never_answered_from_this_projects_cache(world, speech_dir):
    _, path = speech(speech_dir, 600, seed=3)

    async def scenario():
        _, first = await transcribe_path(world.jobs, path, project="proj_a")
        calls_a = windows_sent(world.fleet)
        _, second = await transcribe_path(world.jobs, path, project="proj_b")
        return first, calls_a, second

    first, calls_a, second = run(scenario())
    assert second.text == first.text
    assert second.report["windows_cached"] == 0
    assert windows_sent(world.fleet) == 2 * calls_a


def test_the_speech_gate_waits_without_a_deadline_and_reports_queue_positions(monkeypatch):
    """Windows wait on `capacity.hold(wait_s=None, on_wait=…)` with no
    deadline, and a position update reaches the job whether or not the gate
    awaits the callback."""
    seen: Dict[str, Any] = {}

    import contextlib

    @contextlib.asynccontextmanager
    async def hold(engine, *, weight_tokens=0, wait_s=None, yield_to_chat=False, abandon=None, on_wait=None):
        seen.update(engine=engine, wait_s=wait_s, yield_to_chat=yield_to_chat)
        on_wait(3)  # not awaited
        await on_wait(2)  # awaited
        yield

    monkeypatch.setattr(capacity, "hold", hold)
    updates: List[Dict[str, Any]] = []

    async def on_wait(data):
        updates.append(data)

    async def scenario():
        async with aj.capacity_gate(on_wait):
            await asyncio.sleep(0)

    run(scenario())
    assert seen == {"engine": capacity.GATE_ASR, "wait_s": None, "yield_to_chat": True}
    assert sorted(u["queue_position"] for u in updates) == [2, 3]


# ---------------------------------------------------------- jobs & keys --


def test_job_keys_change_with_project_language_format_class_and_chunking_but_not_between_json_and_text(monkeypatch):
    base = dict(project_id="proj_a", sha256="ab" * 32, language=None, response_format="json")
    key = aj.JobSpec.for_request(**base).key()
    assert aj.JobSpec.for_request(**{**base, "response_format": "text"}).key() == key
    assert aj.JobSpec.for_request(**{**base, "response_format": "verbose_json"}).key() != key
    assert aj.JobSpec.for_request(**{**base, "project_id": "proj_b"}).key() != key
    assert aj.JobSpec.for_request(**{**base, "language": "hi"}).key() != key
    assert aj.JobSpec.for_request(**{**base, "sha256": "cd" * 32}).key() != key
    monkeypatch.setenv("PUBLIC_API_ASR_OVERLAP_S", "2")
    assert aj.JobSpec.for_request(**base).key() != key


def test_a_second_start_joins_the_running_job_and_a_finished_job_is_attached_from_its_stored_result(world, speech_dir):
    _, path = speech(speech_dir, 600, seed=3)

    async def scenario():
        source = await aj.AudioSource.from_path(path)
        spec = aj.JobSpec.for_request(project_id="proj_a", sha256=source.sha256, language=None, response_format="json")
        first, second = await asyncio.gather(world.jobs.start(spec, source), world.jobs.start(spec, source))
        assert second is first
        assert await world.jobs.attach(spec.key(), "proj_b") is None
        result = await first.wait()
        calls = windows_sent(world.fleet)
        attached = await world.jobs.attach(spec.key(), "proj_a")
        again = await world.jobs.start(spec, source)
        return spec, result, calls, attached, again

    spec, result, calls, attached, again = run(scenario())
    assert calls == result.report["windows"], "the joined start sent nothing of its own"
    assert attached is not None and attached.state == "done" and attached.result.text == result.text
    assert again.state == "done" and again.result.text == result.text
    assert windows_sent(world.fleet) == calls
    assert run(world.jobs.attach(spec.key(), "proj_b")) is None


def test_a_job_left_without_followers_is_cancelled_after_the_grace_and_its_windows_stay_cached(world, speech_dir, monkeypatch):
    script, path = speech(speech_dir, 1200)
    monkeypatch.setenv("PUBLIC_API_ASR_ORPHAN_GRACE_S", "0.05")
    world.fleet["asr-head"].latency_s = 0.05

    async def scenario():
        source = await aj.AudioSource.from_path(path)
        spec = aj.JobSpec.for_request(project_id="proj_a", sha256=source.sha256, language=None, response_format="json")
        job = await world.jobs.start(spec, source)
        async with job.follow() as follower:
            while True:
                event = await follower.next(5.0)
                if event.type == "delta":
                    break
        with pytest.raises(asyncio.CancelledError):
            await job.task
        abandoned_after = windows_sent(world.fleet)
        world.fleet["asr-head"].latency_s = 0.0
        _, result = await transcribe_path(world.jobs, path, fmt="json")
        return job, abandoned_after, result

    job, abandoned_after, result = run(scenario())
    assert job.state == "failed"
    assert 0 < abandoned_after < result.report["windows"]
    assert result.report["windows_cached"] >= abandoned_after - 1
    assert fw.normalized_words(result.text) == [fw.norm_word(w.text) for w in script.words]


def test_a_file_assembled_from_three_parts_gives_the_same_transcript_and_is_left_in_place(world, speech_dir, tmp_path):
    _, path = speech(speech_dir, 600, seed=3)
    with open(path, "rb") as fh:
        data = fh.read()
    third = len(data) // 3
    parts = [data[:third], data[third : 2 * third], data[2 * third :]]
    assembled = tmp_path / "assembled.wav"
    with open(assembled, "wb") as out:
        for number, part in enumerate(parts):
            part_path = tmp_path / f"part-{number}"
            part_path.write_bytes(part)
            out.write(part_path.read_bytes())

    async def scenario():
        async def body():
            for start in range(0, len(data), 256 * 1024):
                yield data[start : start + 256 * 1024]

        ingested = await aj.ingest(body(), cap_bytes=len(data), root=world.root, ledger=world.jobs.ledger)
        assert ingested.owned and ingested.sha256 == hashlib.sha256(data).hexdigest()
        # Deleted after DECODE, not at the end: checked when the first window
        # reaches the engine, while the job still has its whole transcript to do.
        source_present_at_first_window: List[bool] = []
        world.fleet["asr-head"].on_call = lambda n: n == 1 and source_present_at_first_window.append(os.path.exists(ingested.path))
        spec = aj.JobSpec.for_request(project_id="proj_multi", sha256=ingested.sha256, language=None, response_format="verbose_json")
        from_body = await (await world.jobs.start(spec, ingested)).wait()
        assert source_present_at_first_window == [False]
        from_file = await transcribe_path(world.jobs, str(assembled), project="proj_file")
        return ingested, from_body, from_file[1]

    ingested, from_body, from_file = run(scenario())
    assert from_file.text == from_body.text
    assert [s.to_json() for s in from_file.segments] == [s.to_json() for s in from_body.segments]
    assert assembled.exists(), "a Files API original is never deleted by a transcription"
    assert not os.path.exists(ingested.path), "an ingested body is deleted after decode"


# --------------------------------------------------------------- decode --


def test_three_concurrent_jobs_run_at_most_two_decoders_each_at_nice_19_in_the_idle_io_class(world, speech_dir, monkeypatch):
    monkeypatch.setenv("FAKE_FFMPEG_MIN_S", "0.8")
    paths = [speech(speech_dir, 120, seed=seed)[1] for seed in (21, 22, 23)]

    async def scenario():
        return await asyncio.gather(*(transcribe_path(world.jobs, p, fmt="json") for p in paths))

    results = run(scenario())
    assert all(result.text for _, result in results)
    entries = fw.read_tool_log(world.ffmpeg_log)
    assert len(entries) == 3
    assert max(entry["concurrent"] for entry in entries) == 2
    assert world.jobs.admission.peak_active == 2
    for entry in entries:
        assert entry["nice"] == 19
        assert entry["ionice"] == "idle"
        assert entry["exit"] == 0
        assert entry["input"] == "pipe:0"


def test_the_disk_ledger_refuses_a_decode_it_cannot_reserve_with_503_retry_after_60_and_starts_no_decoder(world, speech_dir):
    _, path = speech(speech_dir, 600, seed=3)
    need = 600 * 32000
    free = {"blocks": 0}

    def statvfs(_path):
        return os.statvfs_result((4096, 4096, 10**9, 0, free["blocks"], 0, 0, 0, 0, 255))

    ledger = disk_ledger.DiskLedger(world.root, statvfs=statvfs)
    free["blocks"] = (ledger.floor_bytes() + need // 2) // 4096
    jobs = world.build(ledger=ledger)
    with pytest.raises(errors.ApiError) as refused:
        run(transcribe_path(jobs, path))
    assert refused.value.status == 503 and refused.value.retry_after == 60
    assert fw.read_tool_log(world.ffmpeg_log) == [], "no decoder was started"
    assert world.fleet.calls == []
    assert ledger.outstanding_bytes() == 0
    with pytest.raises(disk_ledger.DiskFull) as preflight:
        jobs.preflight(need)
    assert preflight.value.api_error().retry_after == 60


def test_a_decoder_that_stops_making_progress_is_killed_after_the_stall_window(world, speech_dir, monkeypatch):
    _, path = speech(speech_dir, 600, seed=3)
    monkeypatch.setenv("FAKE_FFMPEG_STALL_AFTER_BYTES", "1")
    monkeypatch.setenv("PUBLIC_API_DECODE_STALL_S", "0.5")
    started = time.monotonic()
    with pytest.raises(errors.ApiError) as refused:
        run(asyncio.wait_for(transcribe_path(world.jobs, path), timeout=30))
    assert time.monotonic() - started < 10
    assert refused.value.code == "model_unavailable" and refused.value.retry_after
    stalled = [int(name) for name in os.listdir(world.rundir)]
    assert len(stalled) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(stalled[0], 0)
    assert not os.listdir(os.path.join(world.root, "jobs"))


def test_a_file_that_is_not_audio_is_refused_as_400_on_file_after_both_decode_modes(world, tmp_path):
    junk = tmp_path / "notes.txt"
    junk.write_bytes(b"this is not audio at all\n" * 400)
    with pytest.raises(errors.ApiError) as refused:
        run(transcribe_path(world.jobs, str(junk)))
    assert refused.value.status == 400 and refused.value.param == "file"
    modes = [entry["input"] for entry in fw.read_tool_log(world.ffmpeg_log)]
    assert modes == ["pipe:0", str(junk)]


def test_every_decoder_and_probe_command_carries_the_input_allowlists_and_the_low_priority_prefix():
    decoder = aj.Decoder()
    piped = decoder.argv("/src", "/out", pipe=True)
    from_file = decoder.argv("/src", "/out", pipe=False)
    probe = decoder.probe_argv("/src")
    for argv in (piped, from_file, probe):
        assert argv[:6] == ["nice", "-n", "19", "ionice", "-c", "3"]
        joined = " ".join(argv)
        assert "-format_whitelist" in argv and "-protocol_whitelist" in argv
        assert argv.index("-format_whitelist") < argv.index("-i")
        assert argv.index("-protocol_whitelist") < argv.index("-i")
        formats = argv[argv.index("-format_whitelist") + 1].split(",")
        assert {"hls", "concat", "image2", "lavfi"}.isdisjoint(formats)
        assert "-enable_drefs" not in joined and "-use_absolute_path" not in joined
    assert piped[piped.index("-protocol_whitelist") + 1] == "pipe"
    assert piped[piped.index("-i") + 1] == "pipe:0"
    assert from_file[from_file.index("-protocol_whitelist") + 1] == "file"


def test_the_duration_estimate_prefers_the_wav_header_then_ffprobe_then_a_bitrate_upper_bound(world, speech_dir, tmp_path, monkeypatch):
    _, path = speech(speech_dir, 120, seed=21)
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"\x00" * 7500)
    # A header claiming one byte per second: 96,000 s for 96 KB of data.
    liar = tmp_path / "liar.wav"
    liar.write_bytes(
        b"RIFF" + (36 + 96000).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + (16000).to_bytes(4, "little")
        + (1).to_bytes(4, "little") + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
        + b"data" + (96000).to_bytes(4, "little") + b"\x00" * 96000
    )
    monkeypatch.setenv("FAKE_FFPROBE_FAIL", "1")

    async def scenario():
        wav = await world.jobs._estimate_seconds(await aj.AudioSource.from_path(path))
        unknown = await world.jobs._estimate_seconds(await aj.AudioSource.from_path(str(blob)))
        lying = await world.jobs._estimate_seconds(await aj.AudioSource.from_path(str(liar)))
        return wav, unknown, lying

    wav, unknown, lying = run(scenario())
    assert wav == (pytest.approx(120.0), "wav_header")
    assert unknown == (pytest.approx(7500 * 8 / 6000), "bitrate_bound")
    assert lying == (pytest.approx(os.path.getsize(liar) * 8 / 6000), "wav_header")


# ------------------------------------------------------------------ disk --


def test_a_disk_sink_hashes_on_the_way_refuses_past_its_cap_and_leaves_nothing_behind(tmp_path):
    ledger = disk_ledger.DiskLedger(str(tmp_path), floor_bytes=lambda: 0)
    payload = os.urandom(3 * 1024 * 1024 + 17)
    incoming = tmp_path / "asr" / "incoming"

    async def chunks(data: bytes, size: int = 65536):
        for start in range(0, len(data), size):
            yield data[start : start + size]

    async def scenario():
        source = await aj.ingest(chunks(payload), cap_bytes=len(payload), root=str(tmp_path / "asr"), ledger=ledger)
        with pytest.raises(disk_ledger.CapExceeded):
            await aj.ingest(chunks(payload), cap_bytes=len(payload) - 1, root=str(tmp_path / "asr"), ledger=ledger)
        return source

    source = run(scenario())
    assert source.sha256 == hashlib.sha256(payload).hexdigest() and source.bytes == len(payload)
    assert oct(os.stat(source.path).st_mode & 0o777) == "0o600"
    assert oct(os.stat(incoming).st_mode & 0o777) == "0o700"
    assert os.listdir(incoming) == [os.path.basename(source.path)], "the refused body left no temporary file"
    assert ledger.outstanding_bytes() == 0


def test_streaming_a_ninety_mebibyte_body_to_disk_adds_less_than_sixteen_mebibytes_of_memory(tmp_path):
    ledger = disk_ledger.DiskLedger(str(tmp_path), floor_bytes=lambda: 0)
    block = os.urandom(64 * 1024)
    total = 90 * 1024 * 1024

    async def body():
        for _ in range(total // len(block)):
            yield block

    async def scenario():
        with AnonSampler() as sampler:
            source = await aj.ingest(body(), cap_bytes=total, root=str(tmp_path / "asr"), ledger=ledger)
        return source, sampler.peak

    source, peak = run(scenario())
    assert source.bytes == total
    assert peak < 16 * 1024 * 1024, peak


def test_a_reservation_stops_counting_written_bytes_and_growth_past_free_space_is_refused():
    free = {"bytes": 1000 * 1024 * 1024}

    def statvfs(_path):
        return os.statvfs_result((4096, 1, 10**12, 0, free["bytes"], 0, 0, 0, 0, 255))

    ledger = disk_ledger.DiskLedger("/", statvfs=statvfs, floor_bytes=lambda: 100 * 1024 * 1024)
    first = ledger.reserve(600 * 1024 * 1024, purpose="a")
    assert ledger.available_bytes() == 300 * 1024 * 1024
    with pytest.raises(disk_ledger.DiskFull):
        ledger.reserve(301 * 1024 * 1024, purpose="b")
    # 400 MiB of the first reservation reach the disk: statvfs shows them,
    # the reservation stops counting them, the net is unchanged.
    free["bytes"] -= 400 * 1024 * 1024
    first.note_written(400 * 1024 * 1024)
    assert ledger.available_bytes() == 300 * 1024 * 1024
    with pytest.raises(disk_ledger.DiskFull):
        first.ensure_covers(1000 * 1024 * 1024)
    first.ensure_covers(650 * 1024 * 1024, step_bytes=1)
    assert first.reserved == 650 * 1024 * 1024
    first.release()
    first.release()
    assert ledger.outstanding_bytes() == 0


def _free_disk(state):
    """A statvfs whose free bytes are `state["free"]` (f_frsize 1)."""

    def statvfs(_path):
        return os.statvfs_result((4096, 1, 10**15, 0, state["free"], 0, 0, 0, 0, 255))

    return statvfs


@pytest.fixture()
def shared_disk_env(tmp_path, monkeypatch):
    """The Files store and the ASR cache under one tmp directory (one device),
    both watermarks at their defaults."""
    monkeypatch.setenv("PUBLIC_API_FILES_DIR", str(tmp_path / "api-files"))
    monkeypatch.delenv("PUBLIC_API_FILES_MIN_FREE_GIB", raising=False)
    monkeypatch.delenv("PUBLIC_API_MIN_FREE_DISK_BYTES", raising=False)
    disk_ledger.reset_for_tests()
    yield {"files": str(tmp_path / "api-files"), "asr": str(tmp_path / "asr")}
    disk_ledger.reset_for_tests()


def test_on_the_files_store_device_transcription_stops_at_the_files_watermark_so_uploads_are_not_starved(shared_disk_env, monkeypatch):
    # Review finding 2026-09-13: with 20 GiB for ASR and 250 GiB for Files on
    # one /data, transcription could take the device from 260 GiB free to
    # 20 GiB while every upload was refused. Now it stops at 250 GiB.
    from app.apifiles import limits as files_limits
    from app.apifiles import storage as files_storage

    gib = disk_ledger.GIB
    disk = {"free": 260 * gib}
    ledger = disk_ledger.DiskLedger(shared_disk_env["asr"], statvfs=_free_disk(disk))
    assert disk_ledger.min_free_bytes() == 20 * gib
    assert ledger.floor_bytes() == files_limits.min_free_bytes() == 250 * gib
    assert ledger.snapshot()["floor_bytes"] == 250 * gib
    assert ledger.available_bytes() == 10 * gib
    with pytest.raises(disk_ledger.DiskFull) as refused:
        ledger.reserve(10 * gib + 1, purpose="asr-decode")
    assert refused.value.api_error().status == 503 and refused.value.api_error().retry_after == 60

    # Concurrent 89 MiB ingests take everything the ledger allows and write it.
    cap = 89 * 1024 * 1024
    taken = []
    while True:
        try:
            taken.append(ledger.reserve(cap, purpose="asr-ingest"))
        except disk_ledger.DiskFull:
            break
    assert len(taken) == (10 * gib) // cap == 115
    for reservation in taken:
        disk["free"] -= cap
        reservation.note_written(cap)
    # The device is still at or above the Files watermark, by the Files API's own check.
    monkeypatch.setattr(files_storage, "free_bytes", lambda: disk["free"])
    files_storage.require_free(0)
    assert disk["free"] >= files_limits.min_free_bytes()


def test_the_files_store_and_the_asr_cache_on_one_device_share_one_ledger_so_either_sees_the_others_promises(shared_disk_env):
    asr = disk_ledger.ledger_for(shared_disk_env["asr"])
    files = disk_ledger.ledger_for(shared_disk_env["files"])
    assert files is asr
    promise = asr.reserve(0, purpose="asr-decode")
    promise.ensure_covers(5 * 1024 * 1024, step_bytes=1)
    assert files.outstanding_bytes() == 5 * 1024 * 1024
    promise.release()
    assert files.outstanding_bytes() == 0


def test_on_a_device_the_files_store_does_not_use_the_asr_ledger_keeps_its_own_twenty_gibibyte_floor(shared_disk_env, monkeypatch):
    real_device_of = disk_ledger.device_of
    files_dir = shared_disk_env["files"]

    def device_of(path):
        other = os.path.abspath(path).startswith(files_dir)
        return real_device_of(path) + (1 if other else 0)

    monkeypatch.setattr(disk_ledger, "device_of", device_of)
    gib = disk_ledger.GIB
    ledger = disk_ledger.DiskLedger(shared_disk_env["asr"], statvfs=_free_disk({"free": 30 * gib}))
    assert not disk_ledger.shares_files_store_device(shared_disk_env["asr"])
    assert ledger.floor_bytes() == 20 * gib
    assert ledger.available_bytes() == 10 * gib


def test_the_larger_watermark_wins_and_an_unreadable_device_or_malformed_watermark_fails_closed(shared_disk_env, monkeypatch):
    gib = disk_ledger.GIB
    asr = shared_disk_env["asr"]
    monkeypatch.setenv("PUBLIC_API_FILES_MIN_FREE_GIB", "300")
    assert disk_ledger.floor_for(asr) == 300 * gib
    monkeypatch.setenv("PUBLIC_API_MIN_FREE_DISK_BYTES", str(400 * gib))
    assert disk_ledger.floor_for(asr) == 400 * gib
    monkeypatch.delenv("PUBLIC_API_MIN_FREE_DISK_BYTES")
    monkeypatch.setenv("PUBLIC_API_FILES_MIN_FREE_GIB", "lots")
    assert disk_ledger.floor_for(asr) == 250 * gib
    monkeypatch.delenv("PUBLIC_API_FILES_MIN_FREE_GIB")

    def unreadable(_path):
        raise PermissionError("stat refused")

    monkeypatch.setattr(disk_ledger, "device_of", unreadable)
    assert disk_ledger.shares_files_store_device(asr)
    assert disk_ledger.floor_for(asr) == 250 * gib


def test_the_sweep_removes_expired_windows_and_results_and_only_job_directories_whose_heartbeat_stopped(world):
    cache = world.jobs.cache
    old_key = aj.WindowCache.key(project_id="p", clip_sha256="a" * 64, language=None, model="m")
    new_key = aj.WindowCache.key(project_id="p", clip_sha256="b" * 64, language=None, model="m")
    cache.put(old_key, {"segments": [], "language": None, "duration": 1.0})
    cache.put(new_key, {"segments": [], "language": None, "duration": 1.0})
    now = time.time()
    os.utime(cache._path(old_key), (now - 90000, now - 90000))
    jobs_root = os.path.join(world.root, "jobs")
    stale, live = os.path.join(jobs_root, "stale"), os.path.join(jobs_root, "live")
    for directory, age in ((stale, 3600), (live, 5)):
        os.makedirs(directory)
        beat = os.path.join(directory, "heartbeat")
        open(beat, "w").close()
        os.utime(beat, (now - age, now - age))
    removed = world.jobs.sweep(now=now)
    assert removed["windows"] == 1 and removed["jobs"] == 1
    assert cache.get(old_key) is None and cache.get(new_key) is not None
    assert os.path.isdir(live) and not os.path.exists(stale)


# ------------------------------------------------------------ the wire --


def harness(world: World):
    """A stand-in route, NOT the production one (integration wires
    endpoints.py): it exists to put `sse_stream`'s bytes in front of the real
    SDK parser, over the same ingest and registry the route will call."""
    app = FastAPI()

    @app.post("/v1/audio/transcriptions")
    async def create(request: Request):
        form = await request.form()
        data = await form["file"].read()

        async def body():
            yield data

        source = await aj.ingest(body(), cap_bytes=len(data), root=world.root, ledger=world.jobs.ledger)
        spec = aj.JobSpec.for_request(project_id="proj_stream", sha256=source.sha256, language=None, response_format="json")
        job = await world.jobs.start(spec, source)
        if str(form.get("stream")).lower() == "true":
            return StreamingResponse(aj.sse_stream(job, heartbeat_s=0.05), media_type="text/event-stream")
        result = await job.wait()
        return JSONResponse(result.body("json"))

    return app


async def sdk_stream(app, audio: bytes):
    import openai

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://harness") as http:
        client = openai.AsyncOpenAI(base_url="http://harness/v1", api_key="tsk_test_x", http_client=http, max_retries=0)
        stream = await client.audio.transcriptions.create(
            file=("speech.wav", audio, "audio/wav"), model="techsara-whisper", stream=True
        )
        events = [event async for event in stream]
        raw = await http.post(
            "/v1/audio/transcriptions",
            files={"file": ("speech.wav", audio, "audio/wav")},
            data={"model": "techsara-whisper", "stream": "true"},
        )
        return events, raw.text


def test_openai_python_parses_the_stream_as_text_deltas_then_one_done_event(world, speech_dir):
    pytest.importorskip("openai")
    script, path = speech(speech_dir, 600, seed=3)
    with open(path, "rb") as fh:
        audio = fh.read()
    events, raw = run(sdk_stream(harness(world), audio))
    types = [event.type for event in events]
    assert types[-1] == "transcript.text.done" and types.count("transcript.text.done") == 1
    assert set(types[:-1]) == {"transcript.text.delta"}
    text = "".join(event.delta for event in events[:-1])
    assert text == events[-1].text
    assert fw.normalized_words(text) == [fw.norm_word(w.text) for w in script.words]
    lines = raw.splitlines()
    assert lines[0] == ": ping"
    assert not any(line.startswith(("id:", "retry:", "event:")) for line in lines)


def test_a_failed_job_streams_one_error_object_that_openai_python_raises(world):
    openai = pytest.importorskip("openai")
    with pytest.raises(openai.APIError) as raised:
        run(sdk_stream(harness(world), b"\x01\x02" * 5000))
    error = raised.value.body
    assert error["code"] == "invalid_request_error" and error["param"] == "file"
    assert "decoded" in error["message"] and "/" not in error["message"]


# ---------------------------------------------------------- the fake itself --


def test_the_fake_engine_answers_like_the_speech_server(tmp_path):
    fleet = fw.Fleet(("asr-head",))
    head = fleet["asr-head"]

    def wav(seconds: float) -> bytes:
        samples = int(seconds * 16000)
        return fw._wav_header(samples * 2) + b"\x00\x00" * samples

    script = fw.build_script(40, seed=9)
    fw.write_speech_wav(str(tmp_path / "s.wav"), script)
    speech_bytes = (tmp_path / "s.wav").read_bytes()

    async def scenario():
        async with httpx.AsyncClient(transport=fleet.transport(), base_url="http://asr-head.internal:30007") as client:
            post = lambda payload, **fields: client.post(  # noqa: E731
                "/v1/audio/transcriptions", files={"file": ("a.wav", payload, "audio/wav")}, data=fields
            )
            too_long = await post(wav(601))
            not_audio = await post(b"hello there")
            empty = await client.post("/v1/audio/transcriptions", files={"file": ("a.wav", b"", "audio/wav")})
            verbose = await post(speech_bytes, response_format="verbose_json", no_speech_check="false")
            text = await post(speech_bytes, response_format="text")
            silent = await post(wav(5))
            head.latency_s = 0.2
            together = await asyncio.gather(post(wav(2), no_speech_check="false"), post(wav(2), no_speech_check="false"))
            head.ready = False
            loading = await post(wav(2))
            health = await client.get("/health")
            return too_long, not_audio, empty, verbose, text, silent, together, loading, health

    too_long, not_audio, empty, verbose, text, silent, together, loading, health = run(scenario())
    assert too_long.status_code == 413
    assert not_audio.status_code == 400 and "could not decode audio" in not_audio.json()["detail"]
    assert empty.status_code == 400 and empty.json()["detail"] == "empty upload"
    body = verbose.json()
    assert {"text", "language", "language_code", "duration", "no_speech_prob", "segments", "processing_ms", "task"} <= set(body)
    assert {"id", "start", "end", "text", "language"} <= set(body["segments"][0])
    assert fw.normalized_words(body["text"]) == [fw.norm_word(w.text) for w in script.words]
    assert text.headers["content-type"].startswith("text/plain") and text.text.startswith('"')
    assert silent.json()["text"] == ""
    assert all(r.status_code == 200 for r in together) and fleet.peak_in_flight == 1
    assert loading.status_code == 503
    assert health.json()["ready"] is False and health.json()["cuda_failures"] == 0
