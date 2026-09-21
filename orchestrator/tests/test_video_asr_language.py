"""One clip is decoded in one language (platform audit #4, 2026-09-21).

Measured on this deployment's engine (openai/whisper-large-v3, task forced to
`transcribe`) with clips built from a Hindi sentence and an English one:

* the two sentences in ONE clip came back entirely in English, and the Hindi
  sentence for "this is pork, not cow's meat" was rendered "This is a mouse's
  flesh, not a cow's flesh" — a fluent English sentence that says something
  else, which is what reaches understanding.json, the chapter titles and the
  .srt the person downloads;
* every cue of that clip was labelled `en`, the Hindi one included, so the
  engine's own per-segment language cannot find the defect;
* the same speech sent as one clip per speech region came back in Hindi and
  in English, each correct.

The pause between the two sentences measured 0.80 s after the detector's
padding, while within-sentence pauses in one speaker's narration measured
0.29-0.89 s — so no `max_gap_s` separates a turn from a breath, and the fix
is a guard rather than a threshold.

The engine is faked at its one seam (`asr.transcribe_segments`) and the
detector pinned (`vad.regions_from_flags`), so every test here drives the
real `plan_windows`, `transcribe_audio` and `language_guard`.
"""
from __future__ import annotations

import asyncio
import wave

import pytest

from app import asr
from app.config import settings
from app.video import transcribe, vad

#: The reproduction's regions: a Hindi sentence, a 0.80 s pause, an English
#: one, as `webrtcvad` marked them on the clip built for the audit.
MIXED_REGIONS = [(0.0, 4.07), (4.87, 9.14)]

HINDI = "yah suar ka maans hai, gaay ka maans nahin hai"
FORCED = "This is a mouse's flesh, not a cow's flesh."
ENGLISH = "The kitchen serves lunch from twelve to three on every weekday."


def _silent_wav(path, seconds: float) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(vad.SAMPLE_RATE)
        w.writeframes(b"\x00\x00" * int(seconds * vad.SAMPLE_RATE))
    return str(path)


class _Engine:
    """Answers a clip by the span of audio it covers, so a region clip and a
    window clip get different replies — which is the whole defect."""

    def __init__(self, replies, *, total_s: float):
        #: [(start_s, end_s, language_code, [(start, end, text)])], matched by
        #: duration: the module sends bytes, and the duration is what a clip
        #: carries of where it came from.
        self.replies = replies
        self.total_s = total_s
        self.clips: list[float] = []

    async def __call__(self, audio, *, filename, content_type, **kwargs):
        seconds = (len(audio) - 44) / (vad.SAMPLE_RATE * 2)
        self.clips.append(round(seconds, 2))
        for start, end, code, cues in self.replies:
            if abs((end - start) - seconds) < 0.02:
                return asr.TranscriptSegments(
                    text=" ".join(t for _a, _b, t in cues),
                    language=code, language_code=code, provider="test", model="test", engine_ms=1,
                    segments=tuple({"start": a, "end": b, "text": t} for a, b, t in cues),
                )
        raise AssertionError(f"no reply for a {seconds:.2f}s clip; have {[round(e - s, 2) for s, e, _c, _q in self.replies]}")


def _run(monkeypatch, tmp_path, *, regions, total_s, engine, max_gap_s=None):
    monkeypatch.setattr(vad, "regions_from_flags", lambda flags, **kw: list(regions))
    monkeypatch.setattr(asr, "transcribe_segments", engine)

    async def progress(*_a):
        return None

    wav = _silent_wav(tmp_path / "clip.wav", total_s)
    return asyncio.run(
        transcribe.transcribe_audio(
            wav,
            total_s=total_s,
            progress=progress,
            max_window_s=90.0,
            max_gap_s=settings.video_asr_max_gap_s if max_gap_s is None else max_gap_s,
            overlap_s=3.0,
        )
    )


def _mixed_engine():
    return _Engine(
        [
            # The window, decoded as one clip: one language for both turns.
            (0.0, 9.14, "en", [(0.0, 3.72, FORCED), (5.04, 8.74, ENGLISH)]),
            # The same speech, one clip per region.
            (0.0, 4.07, "hi", [(0.0, 3.70, HINDI)]),
            (4.87, 9.14, "en", [(0.0, 3.86, ENGLISH)]),
        ],
        total_s=9.14,
    )


def test_a_window_holding_two_languages_keeps_the_per_region_transcripts(monkeypatch, tmp_path):
    segments, language, report = _run(
        monkeypatch, tmp_path, regions=MIXED_REGIONS, total_s=9.5, engine=_mixed_engine()
    )
    said = [s.text for s in segments]
    # The English sentence the engine wrote over the Hindi one must not reach
    # the transcript: it is what the summary and the .srt quote.
    assert FORCED not in said, said
    assert HINDI in said, said
    assert ENGLISH in said, said
    assert [s.language for s in segments] == ["hi", "en"], segments
    assert language in {"hi", "en"}
    assert report["language_guard"]["windows_repaired"] == 1, report["language_guard"]


def _many_regions(n: int, *, gap: float = 1.0):
    """`n` speech regions, each a different length so a fake engine can tell
    one clip from another by its duration, separated by a pause short enough
    that the planner packs several into one window."""
    out, t = [], 0.0
    for k in range(n):
        length = 6.0 + 0.1 * k
        out.append((round(t, 2), round(t + length, 2)))
        t = round(t + length + gap, 2)
    return out


def _monolingual(regions, *, window_language=lambda n: "en"):
    """Replies for every window of `regions` and for every region of them,
    all agreeing about the language."""
    windows = vad.windows_from_regions(
        regions, max_window_s=90.0, max_gap_s=settings.video_asr_max_gap_s, overlap_s=3.0
    )
    replies = [
        (w.start_s, w.end_s, window_language(n), [(0.0, w.duration_s, f"window {n}")])
        for n, w in enumerate(windows)
    ]
    for n, w in enumerate(windows):
        replies += [
            (a, b, window_language(n), [(0.0, b - a, f"region {a:.1f}")]) for a, b in w.regions
        ]
    return windows, replies


def test_the_guard_leaves_a_monolingual_recording_alone_and_is_cheap(monkeypatch, tmp_path):
    """Six windows of English. The transcript is untouched, and only the
    sample is re-read: a recording with no second language pays for four
    windows, not eighty."""
    regions = _many_regions(60)
    windows, replies = _monolingual(regions)
    assert len(windows) > settings.video_asr_language_probe_windows
    engine = _Engine(replies, total_s=regions[-1][1])
    segments, _language, report = _run(
        monkeypatch, tmp_path, regions=regions, total_s=regions[-1][1] + 1.0, engine=engine
    )
    assert [s.text for s in segments] == [f"window {n}" for n in range(len(windows))]
    guard = report["language_guard"]
    assert guard["windows_repaired"] == 0, guard
    assert guard["at_risk"] == len(windows), guard
    assert guard["windows_checked"] == settings.video_asr_language_probe_windows, guard
    # Every region of a sampled window, and nothing outside the sample.
    sampled = transcribe._spread(list(range(len(windows))), settings.video_asr_language_probe_windows)
    assert guard["clips"] == sum(len(windows[n].regions) for n in sampled) > 0, guard
    assert guard["clips"] < len(regions), guard


def test_a_second_language_anywhere_escalates_to_every_window_with_a_pause(monkeypatch, tmp_path):
    """A recording whose windows already disagree about the language is
    checked whole, not sampled: the switch is somewhere in it, and the sample
    exists only to find the recordings that have none."""
    regions = _many_regions(60)
    windows, replies = _monolingual(regions, window_language=lambda n: "en" if n == 0 else "hi")
    engine = _Engine(replies, total_s=regions[-1][1])
    _segments, _language, report = _run(
        monkeypatch, tmp_path, regions=regions, total_s=regions[-1][1] + 1.0, engine=engine
    )
    guard = report["language_guard"]
    assert guard["at_risk"] == len(windows) > settings.video_asr_language_probe_windows, guard
    assert guard["windows_checked"] == guard["at_risk"], guard
    # Every region of every at-risk window was re-read.
    assert guard["clips"] == len(regions), guard
    # Each window's own regions agreed with it, so nothing was rewritten.
    assert guard["windows_repaired"] == 0, guard


def test_a_region_the_engine_refuses_keeps_the_first_pass_words(monkeypatch, tmp_path):
    """A repair must never answer a mistranslation with a hole."""
    engine = _mixed_engine()
    inner = engine.__call__

    async def refusing(audio, *, filename, content_type, **kwargs):
        seconds = (len(audio) - 44) / (vad.SAMPLE_RATE * 2)
        if abs(seconds - 4.27) < 0.02:  # the English region, on its own
            raise asr.ASRRejected("the engine refused this clip")
        return await inner(audio, filename=filename, content_type=content_type, **kwargs)

    segments, _language, report = _run(
        monkeypatch, tmp_path, regions=MIXED_REGIONS, total_s=9.5, engine=refusing
    )
    said = [s.text for s in segments]
    assert HINDI in said, said          # the region that was re-read
    assert ENGLISH in said, said        # the region that was not, from pass 1
    assert FORCED not in said, said
    assert report["windows_failed"] == 0, report


def test_a_short_region_may_not_overrule_the_window(monkeypatch, tmp_path):
    """A clip of a few hundred milliseconds is where the engine invents, and
    an invented cue comes with an invented language."""
    regions = [(0.0, 20.0), (21.0, 21.7)]
    engine = _Engine(
        [
            (0.0, 21.7, "en", [(0.0, 19.0, "the whole of the evidence was read out"), (20.9, 21.6, "Thank you.")]),
            (21.0, 21.7, "cy", [(0.0, 0.7, "Diolch.")]),
            (0.0, 20.0, "en", [(0.0, 19.0, "the whole of the evidence was read out")]),
        ],
        total_s=21.7,
    )
    segments, _language, report = _run(
        monkeypatch, tmp_path, regions=regions, total_s=22.0, engine=engine
    )
    assert "Diolch." not in [s.text for s in segments]
    assert report["language_guard"]["windows_repaired"] == 0, report["language_guard"]


def test_the_guard_can_be_turned_off(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "video_asr_language_guard", False)
    engine = _mixed_engine()
    _segments, _language, report = _run(
        monkeypatch, tmp_path, regions=MIXED_REGIONS, total_s=9.5, engine=engine
    )
    assert engine.clips == [9.14], engine.clips
    assert report["language_guard"]["clips"] == 0, report["language_guard"]


def test_the_default_gap_does_not_swallow_a_paragraph_length_pause():
    """Measured 2026-09-21: within-sentence pauses in one speaker's narration
    ran 0.29-0.89 s and the pauses between paragraphs on the live clips ran
    1.67-2.00 s. The default has to sit between them, and the 2.0 s it was sat
    ON the second cluster — one window or two turned on a hundredth of a
    second."""
    breath = [(0.0, 10.0), (10.89, 20.0)]
    paragraph = [(0.0, 10.0), (11.67, 20.0)]
    gap = settings.video_asr_max_gap_s
    assert len(vad.windows_from_regions(breath, max_window_s=90.0, max_gap_s=gap, overlap_s=3.0)) == 1
    assert len(vad.windows_from_regions(paragraph, max_window_s=90.0, max_gap_s=gap, overlap_s=3.0)) == 2
    # And the audit's 0.8 s is ruled out by the same measurement: it does not
    # split the reproduction's 0.80 s Hindi-to-English turn.
    assert len(vad.windows_from_regions(MIXED_REGIONS, max_window_s=90.0, max_gap_s=0.8, overlap_s=3.0)) == 1


@pytest.mark.parametrize("items,n,expected", [
    ([0, 1, 2], 4, [0, 1, 2]),
    (list(range(8)), 4, [1, 3, 5, 7]),
    (list(range(100)), 4, [12, 37, 62, 87]),
    ([3, 9], 0, []),
])
def test_the_sample_is_spread_over_the_recording(items, n, expected):
    """A person who switches language forty minutes in is invisible to a
    sample of the first four windows."""
    assert transcribe._spread(items, n) == expected
