"""One clip is decoded in one language — on `/v1/audio/transcriptions` too.

Measured live on this deployment's whisper (openai/whisper-large-v3, task
pinned to `transcribe`) through `AudioJobs` itself, 2026-09-21:

* a 31.4 s recording of five English sentences and one Hindi one planned as
  ONE window of six speech regions came back entirely in English, and the
  Hindi sentence was rendered "This is a pig's flesh, not a cow's flesh" —
  English words where Hindi was spoken, which is what `text`, `segments` and
  every `transcript.text.delta` then carried;
* every cue of that window was labelled `en`, the Hindi one included, so the
  engine's own per-segment language cannot find the defect;
* the same region sent as its own clip came back `hi`, in Devanagari
  ("ये सूआ का मास है, गाए का मास नहीं है।"), and the five English regions came
  back `en`.

The engine is faked at `WhisperDispatcher.transcribe` — the one seam between
this module and the network — and the detector is pinned, so every test here
drives the real `plan`, the real `AudioJobs._run`, the real `LanguageGuard`
and the real `video.transcribe.language_guard`.

NO DATABASE (as in test_publicapi_audio_windows.py): the suite's autouse
TRUNCATE costs ~1.45 s per test and nothing here touches a table.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import wave
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from tests import publicapi_fake_whisper as fw
from app.config import settings
from app.publicapi import audio_jobs as aj
from app.publicapi import capacity, disk_ledger, errors
from app.video import vad


@pytest.fixture(scope="session")
def app_database():
    """Overridden: this module touches no table."""
    yield None


@pytest.fixture()
def isolated_app_db():
    """Overridden: no TRUNCATE for tests that touch no table."""
    yield


# --------------------------------------------------------- the recording --

HINDI = "ये सूआ का मास है, गाए का मास नहीं है।"
#: What the engine wrote over the Hindi sentence when it decoded the whole
#: window as one clip. English words, fluent, and not what was said.
FORCED = "This is a pig's flesh, not a cow's flesh."
ENGLISH = [
    "Good afternoon, this is the weekly operations summary.",
    "The transcription queue cleared 11 jobs since Monday.",
    "One recording failed to decode and the caller was asked to resend it.",
    "The next review is scheduled for Thursday at 10 in the morning.",
    "I will attach the numbers to the report tonight.",
]

#: The live plan of the reproduction, as `webrtcvad` marked it: five English
#: regions and the Hindi one, all inside a single 31.43 s window.
MIXED_REGIONS: List[Tuple[float, float]] = [
    (0.0, 8.87),
    (9.64, 14.42),
    (14.8, 19.07),
    (19.93, 23.33),
    (23.83, 27.17),
    (27.55, 31.43),
]
MIXED_TOTAL_S = 31.55


def silent_wav(path, seconds: float) -> str:
    """A WAV of the right shape; what the engine 'hears' is the fake's table."""
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(vad.SAMPLE_RATE)
        handle.writeframes(b"\x00\x00" * int(seconds * vad.SAMPLE_RATE))
    return str(path)


def spread_regions(windows: int, per_window: int) -> List[Tuple[float, float]]:
    """Regions that plan into `windows` windows of `per_window` regions each:
    ordinary pauses inside a window, and one longer than `max_gap_s` between
    windows. Every region is over `_LANGUAGE_MIN_REGION_S`, so each may vote.

    EVERY REGION IS A DIFFERENT LENGTH, and so is every window. The clips here
    are silence, so two clips of equal length are equal BYTES — one window
    cache entry, one engine call — and the fake engine, which answers by
    duration, could not tell them apart either."""
    regions: List[Tuple[float, float]] = []
    cursor = 0.0
    n = 0
    for _window in range(windows):
        for k in range(per_window):
            length = round(4.0 + n * 0.37, 2)
            regions.append((round(cursor, 2), round(cursor + length, 2)))
            cursor = round(cursor + length + (1.0 if k + 1 < per_window else 0.0), 2)
            n += 1
        cursor = round(cursor + 5.0, 2)  # longer than PUBLIC_API_ASR_MAX_GAP_S
    return regions


def window_spans(regions: Sequence[Tuple[float, float]], per_window: int) -> List[Tuple[float, float]]:
    return [
        (regions[i * per_window][0], regions[i * per_window + per_window - 1][1])
        for i in range(len(regions) // per_window)
    ]


# ------------------------------------------------------------ the engine --


class Engine:
    """Answers a clip by the span of audio it covers.

    A region clip and the window clip that holds it get DIFFERENT replies,
    which is the whole defect: the module sends bytes, and the duration is
    what a clip carries of where it came from.
    """

    def __init__(
        self,
        replies: Sequence[Tuple[float, float, str, Sequence[Tuple[float, float, str]]]],
        *,
        refuse: Sequence[Tuple[float, float]] = (),
        delay_s: float = 0.0,
        window_spans: Sequence[Tuple[float, float]] = (),
    ) -> None:
        self.replies = list(replies)
        self.refuse = {round(b - a, 2) for a, b in refuse}
        self.delay_s = delay_s
        self.window_spans = {round(b - a, 2) for a, b in window_spans}
        self.clips: List[float] = []
        self.stats: Dict[str, int] = {"engine_calls": 0, "failovers": 0, "silences": 0, "unavailable_waits": 0}

    def factory(self) -> "Engine":
        return self

    async def aclose(self) -> None:
        return None

    async def transcribe(self, clip: bytes, *, language: Optional[str], index: int, on_wait) -> dict:
        seconds = round((len(clip) - 44) / (vad.SAMPLE_RATE * 2), 2)
        self.clips.append(seconds)
        self.stats["engine_calls"] += 1
        if self.delay_s and (not self.window_spans or seconds not in self.window_spans):
            await asyncio.sleep(self.delay_s)
        if seconds in self.refuse:
            raise aj.EngineFailure(errors.internal_error(), reason="engine refused a window")
        for start, end, code, cues in self.replies:
            if abs((end - start) - seconds) < 0.02:
                return {
                    "text": " ".join(text for _a, _b, text in cues),
                    "language": code,
                    "language_code": code,
                    "duration": seconds,
                    "processing_ms": 1,
                    "segments": [
                        {"id": n, "start": a, "end": b, "text": text, "language": code}
                        for n, (a, b, text) in enumerate(cues)
                    ],
                }
        raise AssertionError(
            f"no reply for a {seconds:.2f}s clip; have {sorted({round(e - s, 2) for s, e, _c, _q in self.replies})}"
        )

    @property
    def region_clips(self) -> List[float]:
        return [c for c in self.clips if c not in self.window_spans]


def mixed_replies() -> List[Tuple[float, float, str, List[Tuple[float, float, str]]]]:
    """The live reproduction: the window in English, its regions in their own
    languages. Cue times are the engine's, clip-relative."""
    window = (
        0.0,
        31.43,
        "en",
        [
            (0.0, 3.8, ENGLISH[0]),
            (4.98, 8.38, ENGLISH[1]),
            (9.64, 14.06, ENGLISH[2]),
            (14.8, 18.52, ENGLISH[3]),
            (19.93, 22.81, ENGLISH[4]),
            (23.83, 26.63, "The caller then explained what was in the parcel."),
            (30.66, 31.43, FORCED),
        ],
    )
    regions = [
        (0.0, 8.87, "en", [(0.0, 3.8, ENGLISH[0]), (4.98, 8.38, ENGLISH[1])]),
        (9.64, 14.42, "en", [(0.0, 4.42, ENGLISH[2])]),
        (14.8, 19.07, "en", [(0.0, 3.72, ENGLISH[3])]),
        (19.93, 23.33, "en", [(0.0, 2.88, ENGLISH[4])]),
        (23.83, 27.17, "en", [(0.0, 2.8, "The caller then explained what was in the parcel.")]),
        (27.55, 31.43, "hi", [(0.0, 3.88, HINDI)]),
    ]
    return [window, *regions]


# ------------------------------------------------------------- the world --


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """A registry whose decoder is the repo's ffmpeg stand-in and whose engine
    is whatever the test hands `run_job`. No network, no GPU, no table."""
    capacity.reset_for_tests()
    disk_ledger.reset_for_tests()
    monkeypatch.setenv("PUBLIC_API_FILES_MIN_FREE_GIB", "0")
    monkeypatch.setenv("PUBLIC_API_ASR_HEALTH_POLL_S", "0.05")
    monkeypatch.setattr(capacity, "YIELD_STEP_S", 0.02)
    tools = fw.install_fake_media_tools(str(tmp_path / "bin"))
    root = str(tmp_path / "asr")

    def build(engine: Engine) -> aj.AudioJobs:
        return aj.AudioJobs(
            root=root,
            decoder=aj.Decoder(ffmpeg=tools["ffmpeg"], ffprobe=tools["ffprobe"]),
            dispatcher_factory=engine.factory,
            model="whisper-test",
        )

    yield build
    capacity.reset_for_tests()
    disk_ledger.reset_for_tests()


def run_job(
    build,
    monkeypatch,
    tmp_path,
    *,
    regions: Sequence[Tuple[float, float]],
    total_s: float,
    engine: Engine,
    language: Optional[str] = None,
    jobs: Optional[aj.AudioJobs] = None,
    name: str = "clip",
    project: str = "proj_lang",
):
    """The real job on a pinned plan. Returns (jobs, job, result)."""
    monkeypatch.setattr(vad, "regions_from_flags", lambda flags, **kw: [tuple(r) for r in regions])
    path = silent_wav(tmp_path / f"{name}.wav", total_s)
    registry = jobs or build(engine)

    async def scenario():
        source = await aj.AudioSource.from_path(path, owned=False)
        spec = aj.JobSpec.for_request(
            project_id=project, sha256=source.sha256, language=language, response_format="verbose_json"
        )
        job = await registry.start(spec, source)
        return job, await job.wait()

    job, result = asyncio.run(scenario())
    return registry, job, result


def said(result) -> List[str]:
    return [segment.text for segment in result.segments]


def deltas(job) -> List[str]:
    return [event.data["delta"] for event in job._events if event.type == "delta"]


# ------------------------------------------------------------- the defect --


def test_a_window_holding_two_languages_keeps_the_per_region_transcripts(world, monkeypatch, tmp_path):
    """The reproduction. The English sentence the engine wrote over the Hindi
    one must not reach `text`, `segments` or a streamed delta."""
    engine = Engine(mixed_replies(), window_spans=[(0.0, 31.43)])
    _jobs, job, result = run_job(
        world, monkeypatch, tmp_path, regions=MIXED_REGIONS, total_s=MIXED_TOTAL_S, engine=engine
    )

    assert FORCED not in said(result), said(result)
    assert HINDI in said(result), said(result)
    assert FORCED not in result.text
    assert HINDI in result.text
    # A delta is never retracted, so the guard must have run before the window
    # was emitted: the fabricated sentence was never streamed at all.
    assert not any(FORCED in delta for delta in deltas(job)), deltas(job)
    assert any(HINDI in delta for delta in deltas(job)), deltas(job)
    # The five English sentences survive, in order, once each.
    for sentence in ENGLISH:
        assert result.text.count(sentence) == 1, result.text
    # And the repaired cue sits on the speech its clip came from, not at the
    # 30.66 s the engine stamped it inside the window.
    hindi_cue = next(s for s in result.segments if s.text == HINDI)
    assert hindi_cue.language == "hi"
    assert 27.5 <= hindi_cue.start_s <= 27.6, hindi_cue

    guard = result.report["language_guard"]
    assert guard["at_risk"] == 1 and guard["windows_checked"] == 1
    assert guard["windows_repaired"] == 1
    assert guard["clips"] == len(MIXED_REGIONS)
    assert guard["budget_spent"] is False and guard["regions_refused"] == 0


def test_the_repaired_window_is_what_a_retry_replays_from_the_cache(world, monkeypatch, tmp_path):
    """A resumed job re-derives the repair from the cached region clips: the
    window cache holds the FIRST pass, so the guard has to run again, and it
    must not cost another engine call."""
    engine = Engine(mixed_replies(), window_spans=[(0.0, 31.43)])
    registry, _job, first = run_job(
        world, monkeypatch, tmp_path, regions=MIXED_REGIONS, total_s=MIXED_TOTAL_S, engine=engine
    )
    calls = engine.stats["engine_calls"]
    assert calls == 1 + len(MIXED_REGIONS)
    # Forget the finished result, keep the window cache: a job that failed
    # after some windows and is retried.
    shutil.rmtree(os.path.join(registry.root, "results"), ignore_errors=True)
    registry._running.clear()

    _registry, _job2, again = run_job(
        world,
        monkeypatch,
        tmp_path,
        regions=MIXED_REGIONS,
        total_s=MIXED_TOTAL_S,
        engine=engine,
        jobs=registry,
        name="clip",
    )
    assert engine.stats["engine_calls"] == calls, engine.clips
    assert again.text == first.text
    assert [(s.start_s, s.end_s, s.text) for s in again.segments] == [
        (s.start_s, s.end_s, s.text) for s in first.segments
    ]
    assert again.report["language_guard"]["windows_repaired"] == 1


# ------------------------------------------ a monolingual job is undisturbed --


def test_an_english_only_recording_transcribes_identically_with_the_guard_on(world, monkeypatch, tmp_path):
    """The guard is a second opinion, not an edit: on a recording with one
    language the transcript is the same byte for byte with it on and off, and
    nothing is repaired."""
    regions = spread_regions(1, 5)
    total_s = regions[-1][1] + 0.5
    window = (regions[0][0], regions[-1][1])
    replies = [
        (
            *window,
            "en",
            [(a - window[0], b - window[0], ENGLISH[k]) for k, (a, b) in enumerate(regions)],
        ),
        *[(a, b, "en", [(0.0, b - a, ENGLISH[k])]) for k, (a, b) in enumerate(regions)],
    ]

    def body(result) -> bytes:
        return json.dumps(result.body("verbose_json"), ensure_ascii=False, sort_keys=True).encode("utf-8")

    on = Engine(replies, window_spans=[window])
    _jobs, _job, with_guard = run_job(
        world, monkeypatch, tmp_path, regions=regions, total_s=total_s, engine=on, name="en_on"
    )
    monkeypatch.setenv("PUBLIC_API_ASR_LANGUAGE_GUARD", "false")
    off = Engine(replies, window_spans=[window])
    _jobs2, _job2, without_guard = run_job(
        world, monkeypatch, tmp_path, regions=regions, total_s=total_s, engine=off, name="en_off", project="proj_off"
    )

    assert body(with_guard) == body(without_guard)
    assert with_guard.report["language_guard"]["windows_repaired"] == 0
    assert with_guard.report["language_guard"]["windows_checked"] == 1
    assert without_guard.report["language_guard"] == {"skipped": "off"}
    # What it cost: the sampled window's regions, once each, and nothing else.
    assert on.region_clips == [round(b - a, 2) for a, b in regions]
    assert off.region_clips == []


def test_a_caller_who_named_a_language_is_never_re_read(world, monkeypatch, tmp_path):
    """`language=en` tells the engine which language to use, so every re-read
    would be told the same: there is no second opinion left to ask for, and
    the caller does not pay for one."""
    engine = Engine(mixed_replies(), window_spans=[(0.0, 31.43)])
    _jobs, _job, result = run_job(
        world, monkeypatch, tmp_path, regions=MIXED_REGIONS, total_s=MIXED_TOTAL_S, engine=engine, language="en"
    )
    assert result.report["language_guard"] == {"skipped": "language_pinned"}
    assert engine.stats["engine_calls"] == 1
    assert engine.region_clips == []


# ------------------------------------------------------------ what it costs --


def test_a_long_monolingual_recording_pays_for_the_sample_and_nothing_else(world, monkeypatch, tmp_path):
    """Twelve at-risk windows, one language: only `probe_windows` of them are
    re-read, spread over the recording rather than taken from the front."""
    windows_wanted, per_window = 12, 2
    regions = spread_regions(windows_wanted, per_window)
    total_s = regions[-1][1] + 0.5
    spans = window_spans(regions, per_window)
    replies = [
        (a, b, "en", [(0.0, b - a, f"window {n} spoken in english")]) for n, (a, b) in enumerate(spans)
    ] + [(a, b, "en", [(0.0, b - a, f"region {n} spoken in english")]) for n, (a, b) in enumerate(regions)]
    engine = Engine(replies, window_spans=spans)

    _jobs, _job, result = run_job(
        world, monkeypatch, tmp_path, regions=regions, total_s=total_s, engine=engine, name="long_en"
    )
    guard = result.report["language_guard"]
    assert result.report["windows"] == windows_wanted
    assert guard["at_risk"] == windows_wanted
    assert guard["sampled"] == aj.language_probe_windows()
    assert guard["windows_checked"] == aj.language_probe_windows()
    assert guard["escalated"] is False
    assert guard["clips"] == aj.language_probe_windows() * per_window
    # Spread, not the first four: a person who switches language forty minutes
    # into a recording is invisible to a sample taken from the front.
    sampled = aj.spread_over_recording(list(range(windows_wanted)), aj.language_probe_windows())
    assert sampled != list(range(aj.language_probe_windows())), sampled
    assert engine.region_clips == [
        round(b - a, 2) for w in sampled for a, b in regions[w * per_window : (w + 1) * per_window]
    ]


def test_two_windows_in_different_languages_escalate_without_re_reading_anything(world, monkeypatch, tmp_path):
    """The free evidence: when the FIRST PASS already reports two languages,
    every at-risk window from then on is checked, with no sampling.

    The sample is turned off for this test so that only the escalation can
    account for a re-read."""
    monkeypatch.setenv("PUBLIC_API_ASR_LANGUAGE_PROBE_WINDOWS", "0")
    windows_wanted, per_window = 6, 2
    regions = spread_regions(windows_wanted, per_window)
    total_s = regions[-1][1] + 0.5
    spans = window_spans(regions, per_window)
    # Window 1 is spoken entirely in Hindi; the rest in English.
    replies = [
        (a, b, "hi" if n == 1 else "en", [(0.0, b - a, f"window {n}")]) for n, (a, b) in enumerate(spans)
    ] + [(a, b, "en", [(0.0, b - a, f"region {n}")]) for n, (a, b) in enumerate(regions)]
    engine = Engine(replies, window_spans=spans)

    _jobs, _job, result = run_job(
        world, monkeypatch, tmp_path, regions=regions, total_s=total_s, engine=engine, name="switch"
    )
    guard = result.report["language_guard"]
    assert guard["sampled"] == 0
    assert guard["escalated"] is True
    # Window 0 was decoded and emitted before the second language was known,
    # and a delta is never retracted; every window from the switch on is
    # checked.
    assert guard["windows_checked"] == windows_wanted - 1
    assert guard["clips"] == (windows_wanted - 1) * per_window
    assert engine.region_clips == [round(b - a, 2) for a, b in regions[per_window:]]


# -------------------------------------------------- honest when it cannot --


def test_a_spent_budget_stops_the_guard_and_finishes_the_job(world, monkeypatch, tmp_path):
    """A second opinion is not worth an hour of a caller's job. With the
    budget spent the guard stops, the report says so, and the job finishes
    on its first pass rather than running long in silence or failing."""
    monkeypatch.setenv("PUBLIC_API_ASR_LANGUAGE_GUARD_BUDGET_S", "0")
    # The window answers at once (so the first pass buys almost no budget) and
    # every region clip takes long enough that no probe can fit in what is left.
    engine = Engine(mixed_replies(), delay_s=0.5, window_spans=[(0.0, 31.43)])
    _jobs, job, result = run_job(
        world, monkeypatch, tmp_path, regions=MIXED_REGIONS, total_s=MIXED_TOTAL_S, engine=engine
    )
    assert job.state == "done"
    guard = result.report["language_guard"]
    assert guard["budget_spent"] is True
    assert guard["windows_repaired"] == 0
    # The first pass is what the caller gets — including its mistranslation.
    # A guard that cannot run must not also lose the words that were read.
    assert FORCED in result.text
    for sentence in ENGLISH:
        assert sentence in result.text
    assert len(engine.region_clips) <= 1


def test_a_region_the_engine_refuses_keeps_the_first_passs_words(world, monkeypatch, tmp_path):
    """A window that cannot be transcribed fails the job; a REGION that cannot
    be re-read does not — its words are already in the transcript, and
    answering a mistranslation with a hole would be worse."""
    refused = MIXED_REGIONS[2]
    engine = Engine(mixed_replies(), refuse=[refused], window_spans=[(0.0, 31.43)])
    _jobs, job, result = run_job(
        world, monkeypatch, tmp_path, regions=MIXED_REGIONS, total_s=MIXED_TOTAL_S, engine=engine
    )
    assert job.state == "done"
    guard = result.report["language_guard"]
    assert guard["regions_refused"] == 1
    assert guard["windows_repaired"] == 1
    # Repaired where it could be, and the refused region's sentence is still
    # there — from the first pass, once.
    assert HINDI in result.text and FORCED not in result.text
    assert result.text.count(ENGLISH[2]) == 1, result.text


def test_a_region_that_re_reads_to_silence_keeps_the_words_the_first_pass_found(world, monkeypatch, tmp_path):
    """The engine answering a region with nothing is not evidence that nothing
    was said there: the repaired window must still carry the first pass's
    words for it, not a hole."""
    replies = [
        (a, b, code, cues) if (a, b) != MIXED_REGIONS[1] else (a, b, "en", [])
        for a, b, code, cues in mixed_replies()
    ]
    engine = Engine(replies, window_spans=[(0.0, 31.43)])
    _jobs, job, result = run_job(
        world, monkeypatch, tmp_path, regions=MIXED_REGIONS, total_s=MIXED_TOTAL_S, engine=engine
    )
    assert job.state == "done"
    assert result.report["language_guard"]["windows_repaired"] == 1
    assert result.report["language_guard"]["regions_refused"] == 1
    assert HINDI in result.text and FORCED not in result.text
    assert result.text.count(ENGLISH[2]) == 1, result.text


def test_the_guard_never_turns_a_finished_transcript_into_a_failed_job(world, monkeypatch, tmp_path):
    """Every region refused: the job still succeeds with its first pass."""
    engine = Engine(mixed_replies(), refuse=MIXED_REGIONS, window_spans=[(0.0, 31.43)])
    _jobs, job, result = run_job(
        world, monkeypatch, tmp_path, regions=MIXED_REGIONS, total_s=MIXED_TOTAL_S, engine=engine
    )
    assert job.state == "done" and job.error is None
    assert result.report["language_guard"]["regions_refused"] == len(MIXED_REGIONS)
    assert result.report["language_guard"]["windows_repaired"] == 0
    assert FORCED in result.text


# ------------------------------------------------------------- the knobs --


def test_the_guard_defaults_match_the_video_path_and_the_gap_deliberately_does_not():
    """The guard is the same rule on the same engine, so its defaults are the
    video path's. `max_gap_s` is NOT: see `audio_jobs.max_gap_s` for the window
    counts that decided it."""
    assert aj.language_guard_enabled() is bool(settings.video_asr_language_guard)
    assert aj.language_probe_windows() == int(settings.video_asr_language_probe_windows)
    assert aj.max_gap_s() == 2.0
    assert settings.video_asr_max_gap_s == 1.2


def test_the_budget_grows_with_what_the_engine_has_already_done():
    """Only an engine that has stopped answering runs the budget out."""
    base = aj.language_guard_budget_s(0.0)
    assert base == 60.0
    assert aj.language_guard_budget_s(12.46) == pytest.approx(base + aj.LANGUAGE_GUARD_WORK_FACTOR * 12.46)
    # The measurement this is sized on: a 31.4 s window of six regions cost
    # 12.46 s of wall and its six regions 18.63 s (live, 2026-09-21).
    assert aj.language_guard_budget_s(12.46) > 18.63
