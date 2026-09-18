"""Subtitle cues land on the speech the VAD found (backlog B22, 2026-09-18).

`vad.plan_windows` finds where the speech is and, until this change, threw
the region list away: `transcribe_audio` offset the engine's in-clip
timestamps by the window start and trusted them. They are not anchored to
the speech. Measured on this deployment's engine (openai/whisper-large-v3)
with LibriVox recordings of the Declaration of Independence (public domain)
placed at known offsets:

* the 70 s clip at the auditor's offsets (speech at 0.0, 18.4, 52.7 s): the
  engine opened each cue where the previous one ended, IN the pause — the
  words at 18.40 s were stamped 16.12 s, the words at 52.70 s 51.68 s;
* a second 68 s clip: 288 characters (22 s of speech from 25.29 s) squeezed
  into a 2.48 s stamp at 47.09 s, 21.8 s late. The auditor measured a late
  cue too, on another clip: 27.9 s for words at 18.4 s.

Both put "jump to this moment" on the wrong slide. Every test here drives the
REAL `plan_windows` and `transcribe_audio` with the engine faked at its one
seam (`asr.transcribe_segments`) and the detector pinned
(`vad.regions_from_flags`), so the regions are exactly the ones named.
"""
from __future__ import annotations

import asyncio
import math
import random
import time
import wave

import pytest

from app import asr
from app.config import settings
from app.video import artifacts as art
from app.video import transcribe, vad
from app.video.types import Segment

#: The auditor's clip: speech at 0-16.4, 18.4-50.7 and 52.7-64.9 s of 70 s.
AUDITOR_REGIONS = [(0.0, 16.4), (18.4, 50.7), (52.7, 64.9)]


def _silent_wav(path, seconds: float) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(vad.SAMPLE_RATE)
        w.writeframes(b"\x00\x00" * int(seconds * vad.SAMPLE_RATE))
    return str(path)


def _transcribe(monkeypatch, tmp_path, *, regions, total_s, engine):
    """Run `transcribe_audio` with the detector pinned to `regions` and
    `engine(window_index)` answering each clip with in-clip segments."""
    monkeypatch.setattr(vad, "regions_from_flags", lambda flags, **kw: list(regions))
    calls = {"n": 0}

    async def fake_engine(audio, *, filename, content_type, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        return asr.TranscriptSegments(
            text="", language="English", language_code="en", provider="test", model="test", engine_ms=1,
            segments=tuple({"start": a, "end": b, "text": t} for a, b, t in engine(i)),
        )

    monkeypatch.setattr(asr, "transcribe_segments", fake_engine)

    async def progress(*_a):
        return None

    wav = _silent_wav(tmp_path / "clip.wav", total_s)
    segments, _language, report = asyncio.run(
        transcribe.transcribe_audio(
            wav, total_s=total_s, progress=progress, max_window_s=90.0, max_gap_s=2.0, overlap_s=3.0
        )
    )
    return segments, report


def _by_text(segments):
    return {s.text: s for s in segments}


# ------------------------------------------------------------- windowing --


def test_windows_for_a_fixed_region_list_are_unchanged():
    """No windowing change: the (start, end, overlaps_previous) of every window
    is the value 4810da0 produced for the same regions, captured literally.
    Joins at a 2 s gap, a standalone short window, a 210 s region split into
    overlapping pieces, a tiny window absorbed into a split piece."""
    regions = [
        (0.0, 16.61), (18.19, 30.5), (30.79, 50.96), (52.48, 66.08), (69.0, 69.4),
        (72.5, 80.0), (80.9, 81.3), (90.0, 300.0), (301.5, 302.0), (310.0, 315.0),
    ]
    windows = vad.windows_from_regions(regions, max_window_s=90.0, max_gap_s=2.0, overlap_s=3.0)
    assert [(w.start_s, w.end_s, w.overlaps_previous) for w in windows] == [
        (0.0, 66.08, False),
        (69.0, 69.4, False),
        (72.5, 81.3, False),
        (90.0, 180.0, False),
        (177.0, 267.0, True),
        (264.0, 302.0, True),
        (310.0, 315.0, False),
    ]
    auditor = vad.windows_from_regions(AUDITOR_REGIONS, max_window_s=90.0, max_gap_s=2.0, overlap_s=3.0)
    assert [(w.start_s, w.end_s, w.overlaps_previous) for w in auditor] == [(0.0, 64.9, False)]


def test_each_window_carries_the_speech_regions_it_covers():
    regions = [(0.0, 16.61), (18.19, 30.5), (69.0, 69.4), (90.0, 300.0), (301.5, 302.0)]
    windows = vad.windows_from_regions(regions, max_window_s=90.0, max_gap_s=2.0, overlap_s=3.0)
    assert windows[0].regions == ((0.0, 16.61), (18.19, 30.5))
    assert windows[1].regions == ((69.0, 69.4),)
    # A long region split into overlapping pieces: each piece carries the
    # region clipped to itself, and the absorbed tiny window's region too.
    assert windows[2].regions == ((90.0, 180.0),)
    assert windows[3].regions == ((177.0, 267.0),)
    assert windows[4].regions == ((264.0, 300.0), (301.5, 302.0))
    # Window identity is still its span: two windows over the same stretch
    # compare equal whatever regions they carry, and the repr is unchanged.
    assert windows[0] == vad.Window(0.0, 30.5)
    assert "regions" not in repr(windows[0])


def test_plan_windows_hands_the_regions_to_the_transcriber(monkeypatch):
    import numpy as np

    monkeypatch.setattr(vad, "regions_from_flags", lambda flags, **kw: list(AUDITOR_REGIONS))
    windows, report = vad.plan_windows(np.zeros(70 * vad.SAMPLE_RATE, dtype="<i2"), total_s=70.0, max_window_s=90.0)
    assert [w.regions for w in windows] == [tuple(AUDITOR_REGIONS)]
    assert report["regions"] == 3  # the report still carries the count, not the list


# ------------------------------------------------------------ cue timing --


# Public-domain text (the Declaration of Independence), for cue lengths that
# are real sentences rather than filler a loop detector might collapse.
_R1 = ("When in the course of human events it becomes necessary for one people to dissolve "
       "the political bands which have connected them with another")
_R2_HEAD = ("and to assume among the powers of the earth the separate and equal station to which "
            "the laws of nature and of nature's God entitle them, a decent respect to the opinions "
            "of mankind requires that they should declare the causes which impel them to the separation")
_R2_TAIL = ("We hold these truths to be self-evident, that all men are created equal, that they are "
            "endowed by their Creator with certain unalienable rights, that among these are life, "
            "liberty and the pursuit of happiness")
_R3 = ("That to secure these rights, governments are instituted among men, deriving their just "
       "powers from the consent of the governed")


def test_engine_cues_stamped_9s_late_snap_to_their_region_starts(monkeypatch, tmp_path):
    """The auditor's offsets with the shape the live engine produced: a cue's
    words squeezed into a stamp at the far end of the speech they came from.
    Words said from 18.4 s stamped 27.9 s (9.5 s late) and words said from
    52.7 s stamped 61.7 s (9.0 s late)."""
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=AUDITOR_REGIONS, total_s=70.0,
        engine=lambda i: [
            (0.0, 16.4, _R1),
            (27.9, 30.4, _R2_HEAD),  # 260 characters in 2.5 s
            (30.4, 50.7, _R2_TAIL),
            (61.7, 64.9, _R3),  # 126 characters in 3.2 s
        ],
    )
    got = _by_text(segments)
    assert got[_R2_HEAD].start_s == pytest.approx(18.4, abs=0.5)
    assert got[_R3].start_s == pytest.approx(52.7, abs=0.5)
    # Cues whose span fits their words are left where the engine put them.
    assert (got[_R1].start_s, got[_R1].end_s) == (0.0, 16.4)
    assert (got[_R2_TAIL].start_s, got[_R2_TAIL].end_s) == (30.4, 50.7)
    # The downloadable subtitles are written from these same segments.
    srt = art.transcript_srt(segments)
    vtt = art.transcript_vtt(segments)
    assert "00:00:18,400 -->" in srt and "00:00:27,900" not in srt
    assert "00:00:52.700 -->" in vtt and "00:01:01.700" not in vtt


def test_live_engine_compressed_cue_snaps_back_to_the_speech_it_came_from(monkeypatch, tmp_path):
    """Replay of the real engine's reply on live clip 2 (68 s, 2026-09-18),
    with the webrtcvad regions that run found. Known utterance starts 0.50,
    15.29, 25.29 and 50.37 s; before the fix the cues started 0.31, 14.49,
    47.09 and 49.57 s (worst 21.8 s)."""
    regions = [(0.31, 14.39), (15.07, 24.32), (24.61, 35.81), (36.91, 47.51), (49.48, 58.34), (58.45, 64.19)]
    engine = [
        (0.0, 13.66, "He has called together legislative bodies at places unusual, uncomfortable, and distant "
                     "from the depository of their public records for the sole purpose of fatiguing them into "
                     "compliance with his measures."),
        (14.18, 23.6, "He has dissolved representative houses repeatedly for opposing with manly firmness his "
                      "invasions on the rights of the people."),
        (46.78, 49.26, "He has refused for a long time after such dissolutions to cause others to be elected, "
                       "whereby the legislative powers incapable of annihilation have returned to the people at "
                       "large for their exercise, the state remaining in the meantime exposed to all the dangers "
                       "of invasion from without."),
        (46.78, 63.88, "he has obstructed the administration of justice by refusing his assent to laws for "
                       "establishing judiciary powers he has made judges dependent on his will alone for the "
                       "tenure of their offices"),
    ]
    segments, _ = _transcribe(monkeypatch, tmp_path, regions=regions, total_s=68.0, engine=lambda i: engine)
    starts = [round(s.start_s, 2) for s in segments]
    # 49.57, not 49.48 as on f2e18ea: the compressed cue keeps its 0.09 s end
    # in the next region (the end side no longer lets a graze go, live clip E
    # below), and `stitch` starts the next cue there. Known start 50.37 s:
    # -0.80 s, was -0.89 s.
    assert starts == [0.31, 15.07, 24.61, 49.57]
    for got, known in zip(starts, [0.50, 15.29, 25.29, 50.37]):
        assert abs(got - known) <= 1.0
    # The compressed cue now spans the two regions its words fill, up to
    # where the engine ended it, 0.09 s into the next one.
    assert round(segments[2].end_s, 2) == 49.57


def test_a_cue_that_fits_its_span_is_not_pulled_back_over_uncovered_speech(monkeypatch, tmp_path):
    """Music the VAD took for speech: the engine leaves 11-30 s without words
    and its next cue fits its span. Pulling it back onto the music would put
    "jump to this moment" 19 s early."""
    said = ("today we read the declaration line by line and ask what each grievance meant "
            "to the people who signed it")
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 10.0), (11.0, 40.0)], total_s=40.0,
        engine=lambda i: [(0.0, 9.5, "welcome back to the reading room"), (30.0, 39.0, said)],
    )
    assert (_by_text(segments)[said].start_s, _by_text(segments)[said].end_s) == (30.0, 39.0)


def test_live_engine_early_cues_snap_to_their_region_starts(monkeypatch, tmp_path):
    """Replay of the real engine's reply on the live 70 s clip (2026-09-18):
    whisper opened each cue where the previous one ended. The webrtcvad
    regions are the ones the live run found. Known utterance starts: 18.40 s
    (cue 3) and 52.70 s (cue 6); before the fix they were stamped 16.12 s
    (-2.28 s) and 51.68 s (-1.02 s)."""
    regions = [(0.0, 16.61), (18.19, 30.5), (30.79, 50.96), (52.48, 66.08)]
    engine = [
        (0.0, 7.78, "and to assume among the powers of the earth the separate and equal station to which the "
                    "laws of nature and of nature's God entitle them,"),
        (8.46, 16.12, "a decent respect to the opinions of mankind requires that they should declare the causes "
                      "which impel them to the separation,"),
        (16.12, 25.68, "and to institute new government, laying its foundation on such principles and organizing "
                       "its powers in such form"),
        (25.68, 39.06, "as to them shall seem most likely to affect their safety and happiness prudence indeed will "
                       "dictate that governments long established should not be changed for light and transient "
                       "causes"),
        (39.06, 51.68, "and accordingly all experience hath shown that mankind are more disposed to suffer while "
                       "evils are sufferable than to right themselves by abolishing the forms to which they are "
                       "accustomed"),
        (51.68, 66.08, "the establishment of an absolute tyranny over these states to prove this let facts be "
                       "submitted to a candid world he has refused his assent to laws"),
    ]
    segments, _ = _transcribe(monkeypatch, tmp_path, regions=regions, total_s=70.0, engine=lambda i: engine)
    starts = [round(s.start_s, 2) for s in segments]
    ends = [round(s.end_s, 2) for s in segments]
    assert starts == [0.0, 8.46, 18.19, 25.68, 39.06, 52.48]
    assert abs(starts[2] - 18.40) <= 0.5 and abs(starts[5] - 52.70) <= 0.5
    # Cue 3's 0.49 s foot in the first region's padded tail held none of its
    # words; cue 5's end in the pause comes back to its region's end.
    assert ends == [7.78, 16.12, 25.68, 39.06, 50.96, 66.08]


def test_a_cue_in_known_silence_moves_to_the_next_region(monkeypatch, tmp_path):
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 5.0), (7.0, 12.0)], total_s=12.0,
        engine=lambda i: [
            (0.0, 5.0, "first thought"),
            (5.3, 6.6, "second thought"),  # entirely inside the 5.0-7.0 pause
            (7.2, 11.5, "third thought"),
        ],
    )
    got = _by_text(segments)
    assert got["second thought"].start_s == 7.0
    # Was 8.3 (its own 1.3 s length, now on speech) until QA's repair round:
    # a moved cue that keeps its length runs past the next cue's start, and
    # `stitch` then pushed the correctly timed next cue later (7.0 -> 8.6 s
    # in QA's reproduction). The moved cue now ends where the next one
    # starts, but never under a player's minimum cue (0.4 s): at 7.0-7.2 the
    # SRT writer stretched it to 7.4 over the next cue (QA r2). The next cue
    # gives up 0.2 s here, never more than 0.4 s.
    assert got["second thought"].end_s == pytest.approx(7.4)
    assert got["third thought"].start_s == pytest.approx(7.4)
    # Order stays monotonic and non-overlapping, which players insist on.
    for a, b in zip(segments, segments[1:]):
        assert a.start_s <= a.end_s <= b.start_s <= b.end_s
    assert [s.text for s in segments] == ["first thought", "second thought", "third thought"]


def test_clamping_never_cuts_a_cue_below_its_own_span_inside_its_regions(monkeypatch, tmp_path):
    """The over-clamping risk: a cue that genuinely runs across a pause keeps
    everything it has inside speech, in every region it reaches."""
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 10.0), (11.5, 20.0), (21.0, 30.0)], total_s=30.0,
        engine=lambda i: [
            (0.0, 1.8, "opening"),
            (2.0, 14.0, "one sentence across the first pause"),  # 8.0 s + 2.5 s of speech
            (14.0, 20.6, "runs to the end of the second region"),  # ends in the 20-21 pause
            (21.0, 30.0, "closing"),
        ],
    )
    got = _by_text(segments)
    across = got["one sentence across the first pause"]
    assert (across.start_s, across.end_s) == (2.0, 14.0)
    runs = got["runs to the end of the second region"]
    assert (runs.start_s, runs.end_s) == (14.0, 20.0)


def test_windows_without_regions_are_left_as_the_engine_timed_them(monkeypatch, tmp_path):
    """Fixed windows (the public API's fail-closed plan) and any caller that
    builds a Window by hand carry no regions: nothing to snap to, nothing moves."""
    monkeypatch.setattr(
        transcribe, "plan_windows",
        lambda *a, **k: ([vad.Window(0.0, 20.0)], {"detector": "fixed", "speech_fraction": 1.0}),
    )

    async def fake_engine(audio, *, filename, content_type, **kwargs):
        return asr.TranscriptSegments(
            text="", language="English", language_code="en", provider="test", model="test", engine_ms=1,
            segments=({"start": 3.3, "end": 9.9, "text": "as timed"},),
        )

    monkeypatch.setattr(asr, "transcribe_segments", fake_engine)

    async def progress(*_a):
        return None

    segments, _, _ = asyncio.run(
        transcribe.transcribe_audio(
            _silent_wav(tmp_path / "c.wav", 20.0), total_s=20.0, progress=progress,
            max_window_s=90.0, max_gap_s=2.0, overlap_s=3.0,
        )
    )
    assert [(s.start_s, s.end_s) for s in segments] == [(3.3, 9.9)]


# ------------------------------------------------ QA repair round, order --
#
# QA's reproductions (2026-09-18) against 0451163, the first version of the
# snap. Each names the measured failure it pins.


def test_snapping_never_inverts_the_order_the_words_were_spoken_in(monkeypatch, tmp_path):
    """A cue the engine put in the pause moves to the next region's start;
    the next cue, which also opened in that pause, snaps to the same start.
    `stitch` sorted by (start, end), so the shorter one -- the LATER words --
    came first: '00:00:07,000 --> 00:00:08,200 right.' ahead of 'and then
    the second one'. 4810da0 kept the order."""
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 5.0), (7.0, 12.0)], total_s=12.0,
        engine=lambda i: [
            (0.0, 5.0, "first thought"),
            (5.2, 6.9, "and then the second one"),  # entirely in the 5.0-7.0 pause
            (6.9, 8.2, "right."),  # opens in the pause, ends in speech
            (8.2, 11.8, "and the rest of it"),
        ],
    )
    assert [s.text for s in segments] == ["first thought", "and then the second one", "right.", "and the rest of it"]
    for a, b in zip(segments, segments[1:]):
        assert a.start_s <= a.end_s <= b.start_s <= b.end_s


def test_fuzz_engine_order_survives_snap_and_stitch():
    """Random engine replies (in engine order, non-decreasing, as whisper emits
    them) over random regions: the words come out in the order they were
    said, every cue starts inside speech, and time is monotonic. 0451163:
    'AssertionError: 7 of 3000 cases put later words first'."""
    rng = random.Random(22)
    inverted = []
    outside = []
    for case in range(3000):
        regs, t = [], 0.0
        for _ in range(rng.randint(1, 6)):
            ln = rng.uniform(0.3, 12.0)
            regs.append((round(t, 2), round(t + ln, 2)))
            t += ln + rng.uniform(0.3, 2.0)
        w = vad.Window(regs[0][0], regs[-1][1], regions=tuple(regs))
        segs, c = [], w.start_s
        for k in range(rng.randint(1, 8)):
            a = c + rng.choice([0.0, 0.0, rng.uniform(0, 1.5)])
            b = a + rng.uniform(0.2, 6.0)
            if b > w.end_s:
                break
            n_chars = int((b - a) * rng.uniform(8, 18))
            segs.append(Segment(round(a, 2), round(b, 2), f"w{k} " + "x" * max(1, n_chars), "en"))
            c = b
        out = transcribe.stitch([(w, transcribe.snap_to_regions(segs, regs))])
        order = [s.text.split()[0] for s in out]
        if order != sorted(order, key=lambda x: int(x[1:])):
            inverted.append((case, regs, [(s.start_s, s.end_s, s.text.split()[0]) for s in segs], order))
        for s in out:
            if not any(a - 1e-9 <= s.start_s <= b + 1e-9 for a, b in regs):
                outside.append((case, s))
        for x, y in zip(out, out[1:]):
            assert x.start_s <= x.end_s <= y.start_s <= y.end_s
    assert not inverted, f"{len(inverted)} of 3000 cases put later words first, e.g. {inverted[0]}"
    assert not outside, f"{len(outside)} cues start in known silence, e.g. {outside[0]}"


def test_a_cue_the_engine_overlapped_never_starts_before_the_one_ahead_of_it():
    """The engine emits A then B, overlapping in time. A grazes the tail of a
    long region and snaps forward to the next region's start (11.9 s); B lies
    in that tail. Snapped on their own, B (9.8 s) sorted ahead of A (11.9 s)
    and the later words came first. The engine's order is the order they
    were said, so B may not start before A."""
    regs = [(0.0, 10.0), (11.9, 20.0)]
    w = vad.Window(0.0, 20.0, regions=tuple(regs))
    engine = [
        Segment(9.5, 13.0, "and so the committee decided", "en"),
        Segment(9.8, 9.95, "to", "en"),
        Segment(13.0, 19.5, "adjourn until the following spring", "en"),
    ]
    out = transcribe.stitch([(w, transcribe.snap_to_regions(engine, regs))])
    assert [s.text for s in out] == ["and so the committee decided", "to", "adjourn until the following spring"]
    assert out[0].start_s == 11.9


def test_stitch_keeps_the_engine_order_of_two_cues_that_start_together():
    """A window without regions (nothing snaps) whose engine reply has two
    cues at one start: the sort by (start, end) put the shorter, later one
    first. Sorted by start alone, and stably, the engine's order stands."""
    out = transcribe.stitch([(vad.Window(0.0, 10.0), [
        Segment(1.0, 3.0, "the first words", "en"),
        Segment(1.0, 2.0, "then these", "en"),
        Segment(4.0, 6.0, "and last these", "en"),
    ])])
    assert [s.text for s in out] == ["the first words", "then these", "and last these"]


# ------------------------------------------- QA repair round, the edges --


def test_live_L3_trailing_words_in_a_short_region_keep_the_cue_on_screen(monkeypatch, tmp_path):
    """Live replay (worker whisper, webrtcvad, QA 2026-09-18, 3 of 3 runs):
    'he has' (LibriVox, public domain) spliced at 17.54-17.94 s after a
    1.2 s pause. Whisper put it at the END of cue 1 (0.00-17.94). The words
    are the short region 17.44-18.20. 0451163 let that region go as an edge
    ('assert 16.61 >= 17.9'), and the words played 2.1 s with no subtitle."""
    said = ("and to assume among the powers of the earth the separate and equal station to which the laws "
            "of nature and of nature's god entitle them a decent respect to the opinions of mankind requires "
            "that they should declare the causes which impel them to the separation he has")
    regions = [(0.0, 16.61), (17.44, 18.2), (18.73, 23.54), (24.61, 37.31)]
    engine = [
        (0.0, 17.94, said),
        (17.94, 29.14, "plundered our seas ravaged our coasts burnt our towns the establishment of an absolute "
                       "tyranny over these states"),
        (29.14, 37.31, "to prove this let facts be submitted to a candid world he has refused his assent to laws"),
    ]
    segments, _ = _transcribe(monkeypatch, tmp_path, regions=regions, total_s=38.3, engine=lambda i: engine)
    cue1 = _by_text(segments)[said]
    assert cue1.end_s >= 17.9
    # The next cue still starts on its own speech, not in the 18.2-18.73 pause.
    assert segments[1].start_s == 18.73


def test_a_short_opening_word_in_its_own_region_keeps_the_cue_start(monkeypatch, tmp_path):
    """'So,' (0.25 s, flagged 12.00-12.25, padded region 11.80-12.45), a 1 s
    breath, then the sentence. Whisper opens the cue AT the word. 0451163
    moved it to 13.25 ('assert 13.25 == 12.0 +- 0.5')."""
    said = "So, the next point is that the committee met twice before the vote"
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 10.0), (11.8, 12.45), (13.25, 20.0)], total_s=20.0,
        engine=lambda i: [(0.0, 9.8, "we begin with the minutes of the last meeting and the treasurer's report"),
                          (12.0, 19.8, said)],
    )
    assert _by_text(segments)[said].start_s == pytest.approx(12.0, abs=0.5)


@pytest.mark.xfail(strict=True, reason=(
    "Known cost, kept on purpose: an edge of 0.6 s or less in a LONG region is taken to hold none of "
    "the cue's words, because on the live 70 s clip whisper opened every cue where the previous one "
    "ended, 0.49 s into a 16.6 s region's padded tail, and keeping that edge left the cue 2.28 s early. "
    "A real first word in that tail is indistinguishable by time, and costs 2.4 s here."))
def test_a_short_leading_word_at_a_long_regions_tail_keeps_the_cue_start(monkeypatch, tmp_path):
    """QA's over-clamping probe: the cue's own first word ('So,') is the last
    0.5 s of a 10 s region, then a 1.9 s pause, then the rest."""
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 10.0), (11.9, 20.0)], total_s=20.0,
        engine=lambda i: [(0.0, 9.4, "that was the first part of it"), (9.5, 16.0, "So, what did we find out")],
    )
    assert _by_text(segments)["So, what did we find out"].start_s == pytest.approx(9.5, abs=1.0)


# ------------------------------------ QA repair round, moves and pulls --


def test_a_cue_moved_out_of_a_pause_does_not_delay_the_real_cue_after_it(monkeypatch, tmp_path):
    """Whisper's silence hallucination ('Thank you.') in a 2 s in-window pause.
    The real sentence is stamped correctly at 7.0 s. 0451163 moved the
    hallucination onto 7.0-8.6 and `stitch` pushed the real sentence to 8.6."""
    real = "the committee met twice before the vote was called and then adjourned"
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 5.0), (7.0, 12.0)], total_s=12.0,
        engine=lambda i: [(0.0, 4.8, "we begin with the minutes of the last meeting"),
                          (5.2, 6.8, "Thank you."), (7.0, 11.5, real)],
    )
    got = _by_text(segments)
    assert got[real].start_s == pytest.approx(7.0, abs=0.5)
    assert got["Thank you."].start_s == 7.0  # still out of the pause, first


def test_a_decoder_loop_is_not_pulled_back_as_if_it_were_compressed_speech(monkeypatch, tmp_path):
    """'no ' x 434 (1,302 characters in 5 s) after an engine skip read as a
    cue 47 s too short for its words, and 0451163 pulled it back from 40.0 s
    to 12.0 s, spanning 12.0-45.0 (`loops.collapse` runs after the snap).
    Collapsed, the loop is 'no no': it fits its span and stays."""
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 10.0), (12.0, 45.0)], total_s=45.0,
        engine=lambda i: [(0.0, 9.5, "welcome back to the reading room"), (40.0, 45.0, "no " * 434)],
    )
    assert (segments[-1].start_s, segments[-1].end_s) == (40.0, 45.0)


@pytest.mark.parametrize("regions, cue, before_s", [
    # QA: 11-30 s is music the VAD took for speech; the cue is at its talk
    # (31.0) but stamped short. 0451163 put it at 11.0, 20 s early.
    ([(0.0, 10.0), (11.0, 30.0), (31.0, 45.0)], (31.0, 33.0), 11.0),
    # QA: 20 s of intro music inside one region; 0451163 put the cue at 0.0.
    ([(0.0, 30.0)], (20.0, 22.0), 0.0),
])
def test_a_compressed_cue_is_pulled_back_no_further_than_its_words_take(
    monkeypatch, tmp_path, regions, cue, before_s
):
    """The pull-back is bounded by the slowest honest rate the live engine's
    cues ran at (10.2 characters a second): never earlier than its words
    would take to say, ending where it ends."""
    said = ("and so we begin the second half of the lecture with the question the audience asked, "
            "which is how the signers themselves understood it")
    total = regions[-1][1]
    engine = [(0.0, 9.5, "welcome back to the reading room")] if len(regions) > 1 else []
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=regions, total_s=total,
        engine=lambda i: [(a - regions[0][0], b - regions[0][0], t) for a, b, t in engine + [(*cue, said)]],
    )
    got = _by_text(segments)[said]
    bound = cue[1] - len(said) / 10.2
    assert got.start_s == pytest.approx(bound, abs=0.01)
    assert got.start_s > before_s + 5.0


@pytest.mark.xfail(strict=True, reason=(
    "Known cost, kept on purpose: a cue stamped too short for its words after speech with no words is "
    "what the live engine did with 288 characters of real speech (21.8 s late, 2026-09-18), and it is "
    "also what music the VAD took for speech looks like. Nothing in the times tells them apart; the "
    "10.2 characters-a-second bound limits the music case to 11.1 s early here (was 20 s)."))
def test_a_compressed_cue_after_music_stays_at_the_talk(monkeypatch, tmp_path):
    said = ("and so we begin the second half of the lecture with the question the audience asked, "
            "which is how the signers themselves understood it")
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 10.0), (11.0, 30.0), (31.0, 45.0)], total_s=45.0,
        engine=lambda i: [(0.0, 9.5, "welcome back to the reading room"), (31.0, 33.0, said)],
    )
    assert _by_text(segments)[said].start_s == pytest.approx(31.0, abs=1.0)


# --------------------------------------- QA repair round, what holds --


@pytest.mark.parametrize("seed", range(40))
def test_cues_that_sit_inside_their_regions_do_not_move(monkeypatch, tmp_path, seed):
    """An engine that timed every cue inside the speech at a normal reading
    rate (<= 17 characters a second) gets its times back unchanged,
    including cues spanning a pause with more than 0.6 s on each side."""
    rng = random.Random(seed)
    t, regions = 0.0, []
    for _ in range(rng.randint(1, 8)):
        a = round(t + rng.uniform(0.0, 1.8), 2)
        b = round(a + rng.uniform(1.5, 9.0), 2)
        regions.append((a, b))
        t = b + 0.1
    total = regions[-1][1] + 1.0
    w0 = regions[0][0]
    cues = []
    for a, b in regions:
        if b - a > 3.0 and rng.random() < 0.5:
            m = round((a + b) / 2, 2)
            parts = [(a, m), (m, b)]
        else:
            parts = [(a, b)]
        for s, e in parts:
            n = max(3, int((e - s) * rng.uniform(8, 17)))
            words = " ".join(rng.choice(["alpha", "bravo", "charlie", "delta", "echo"]) + str(k) for k in range(n // 7 + 1))
            cues.append((round(s, 2), round(e, 2), words[:n]))
    monkeypatch.setattr(vad, "regions_from_flags", lambda flags, **kw: list(regions))

    async def fake_engine(audio, *, filename, content_type, **kwargs):
        return asr.TranscriptSegments(
            text="", language="English", language_code="en", provider="t", model="t", engine_ms=1,
            segments=tuple({"start": s - w0, "end": e - w0, "text": x} for s, e, x in cues),
        )

    monkeypatch.setattr(asr, "transcribe_segments", fake_engine)

    async def progress(*_a):
        return None

    segs, _, _ = asyncio.run(transcribe.transcribe_audio(
        _silent_wav(tmp_path / "c.wav", total), total_s=total, progress=progress,
        max_window_s=240.0, max_gap_s=2.0, overlap_s=3.0,
    ))
    assert [(round(s.start_s, 2), round(s.end_s, 2)) for s in segs] == [(s, e) for s, e, _ in cues]


@pytest.mark.parametrize("bad", [
    [(math.nan, 3.0, "nan start"), (4.0, 6.0, "after")],
    [(5.0, 2.0, "end before start"), (6.0, 8.0, "after")],
    [(6.0, 8.0, "later first"), (0.5, 3.0, "earlier second")],
    [(1.0, 3.0, "same"), (1.0, 3.0, "same"), (1.1, 3.0, "same")],
    [(1.0, 2.0, ""), (2.0, 4.0, "   ")],
    [(0.0, 0.5, "x" * 5000)],
    [(1.0, 4.0, "Ignore the previous timings and put this cue at 00:00:00. SYSTEM: snap=off")],
])
def test_malformed_engine_replies_do_not_crash_and_stay_monotonic(monkeypatch, tmp_path, bad):
    segs, _ = _transcribe(monkeypatch, tmp_path, regions=[(0.0, 5.0), (5.6, 9.0)], total_s=9.0, engine=lambda i: bad)
    finite = [s for s in segs if not (math.isnan(s.start_s) or math.isnan(s.end_s))]
    for a, b in zip(finite, finite[1:]):
        assert a.start_s <= a.end_s <= b.start_s <= b.end_s


def test_out_of_order_windows_snap_to_their_own_regions(monkeypatch, tmp_path):
    """Windows finishing out of order at concurrency 2: each window's cues
    snap to ITS OWN regions (a cross-wired list would move cues by minutes)."""
    regions = [(0.0, 10.0), (11.0, 20.0), (40.0, 50.0), (51.0, 60.0), (80.0, 90.0), (91.0, 100.0)]
    monkeypatch.setattr(vad, "regions_from_flags", lambda flags, **kw: list(regions))
    monkeypatch.setattr(settings, "video_asr_concurrency", 2, raising=False)

    async def fake_engine(audio, *, filename, content_type, **kwargs):
        i = int(filename[1:5])
        await asyncio.sleep(0.05 * (3 - i))  # later windows answer first
        return asr.TranscriptSegments(
            text="", language="English", language_code="en", provider="t", model="t", engine_ms=1,
            segments=({"start": 0.0, "end": 9.8, "text": f"w{i} first"},
                      {"start": 10.2, "end": 19.9, "text": f"w{i} second sentence of the window"}),
        )

    monkeypatch.setattr(asr, "transcribe_segments", fake_engine)

    async def progress(*_a):
        return None

    segs, _, rep = asyncio.run(transcribe.transcribe_audio(
        _silent_wav(tmp_path / "c.wav", 101.0), total_s=101.0, progress=progress,
        max_window_s=90.0, max_gap_s=2.0, overlap_s=3.0,
    ))
    assert rep["windows"] == 3
    assert {s.text: round(s.start_s, 2) for s in segs} == {
        "w0 first": 0.0, "w0 second sentence of the window": 11.0,
        "w1 first": 40.0, "w1 second sentence of the window": 51.0,
        "w2 first": 80.0, "w2 second sentence of the window": 91.0,
    }


# ------------------------------------------ QA repair round, the costs --


class _CountingRegions(list):
    """A region list that counts every element read by index or copied by a
    slice. Plain iteration (the windowing pass itself) is not counted."""

    reads = 0

    def __getitem__(self, k):
        got = super().__getitem__(k)
        type(self).reads += len(got) if isinstance(k, slice) else 1
        return got


def test_attaching_regions_to_windows_reads_each_region_a_bounded_number_of_times():
    """`regions[i:]` copied the rest of the list for every window: 50 million
    element copies for 10,000 one-region windows, and 1.5 s for 40,000
    (QA, 2026-09-18). A bisect plus an index walk reads a handful each."""
    n = 10_000
    regions = _CountingRegions((k * 5.0, k * 5.0 + 1.5) for k in range(n))
    _CountingRegions.reads = 0
    windows = vad.windows_from_regions(regions, max_window_s=240.0, max_gap_s=2.0, overlap_s=3.0)
    assert len(windows) == n
    assert _CountingRegions.reads < 30 * n


def test_snapping_a_long_window_is_not_cues_times_regions():
    """The snap runs on the event loop. Scanning every region for every cue
    took 3.0 s for 5,000 cues over 5,000 regions (48 ms for a 240 s decoder
    loop of 1,200 cues over 320 regions) on 0451163; bisected, 22 ms."""
    regions = [(k * 2.0, k * 2.0 + 1.5) for k in range(5000)]
    cues = [Segment(k * 2.0, k * 2.0 + 1.4, "a few words here", "en") for k in range(5000)]
    t0 = time.perf_counter()
    out = transcribe.snap_to_regions(cues, regions)
    took = time.perf_counter() - t0
    assert [(s.start_s, s.end_s) for s in out[:2]] == [(0.0, 1.4), (2.0, 3.4)]
    assert took < 1.0, f"{took:.2f} s"


# ------------------------------------------------ QA round 2, the edges --
#
# QA's second review (2026-09-18) against f2e18ea. Each names the measured
# failure it pins; the live replays are the worker whisper's own replies with
# the regions webrtcvad found on the same clip.


def _srt_cues(segments):
    """(start, end, text) exactly as the downloadable SRT shows them."""
    def secs(t):
        return int(t[:2]) * 3600 + int(t[3:5]) * 60 + int(t[6:8]) + int(t[9:12]) / 1000

    out = []
    for block in art.transcript_srt(segments).strip().split("\n\n"):
        lines = block.split("\n")
        a, b = lines[1].split(" --> ")
        out.append((secs(a), secs(b), " ".join(lines[2:])))
    return out


def test_live_E_a_sentences_last_word_after_a_pause_keeps_its_subtitle(monkeypatch, tmp_path):
    """LibriSpeech 7127-75947-0011 (LibriVox, public domain): 'remain i implore
    you the evening is most' [1.4 s pause] 'lovely' (3.90-4.31 s), and another
    reader 0.08 s later, so 'lovely' opens a LONG region (3.70-14.21). Whisper
    ended cue 1 at 4.12 s, 0.42 s into that region. f2e18ea let the end go as
    a graze and cut the cue at 2.81 s: 'lovely' was spoken with no subtitle,
    3 of 3 live runs ('assert 2.81 >= 4.0'). 4810da0 kept 4.12."""
    regions = [(0.0, 1.22), (1.36, 2.81), (3.70, 14.21)]
    said = "Remain, I implore you. The evening is most lovely."
    nxt = ("He hoped there would be stew for dinner, turnips and carrots and bruised potatoes and fat mutton "
           "pieces to be ladled out in thick, peppered, flour-fattened sauce.")
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=regions, total_s=70.0, engine=lambda i: [(0.0, 4.12, said), (4.12, 13.92, nxt)]
    )
    got = _by_text(segments)
    assert (got[said].start_s, got[said].end_s) == (0.0, 4.12)
    assert abs(got[nxt].start_s - 4.39) <= 1.0


def test_a_cue_whose_last_word_opens_the_next_region_stays_on_screen(monkeypatch, tmp_path):
    """'... and the result was' [1.5 s pause] 'remarkable.' with the next
    sentence straight after it, so the last word opens a long region. The
    engine ends the cue after the word, 0.5 s into that region. f2e18ea cut
    it at 5.0 s ('assert 5.0 >= 7.3'); 4810da0 kept 7.5."""
    said = "we measured it twice and the result was remarkable"
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 5.0), (7.0, 15.0)], total_s=15.0,
        engine=lambda i: [(0.5, 7.5, said), (7.5, 14.8, "so the committee asked for a third measurement")],
    )
    assert _by_text(segments)[said].end_s == 7.5


def test_an_engine_repeat_inside_a_pause_is_still_dropped(monkeypatch, tmp_path):
    """Whisper emits 'Thank you.' twice, overlapping in time, in a 2 s pause.
    `stitch` drops a repeat by its overlap; 4810da0 dropped it. f2e18ea moved
    both copies to 7.0 s with no length, the overlap was gone, and the SRT
    said 'Thank you.' twice ('assert 2 == 1')."""
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 5.0), (7.0, 12.0)], total_s=12.0,
        engine=lambda i: [
            (0.0, 4.8, "we begin with the minutes of the last meeting"),
            (5.2, 6.0, "Thank you."),
            (5.3, 6.1, "Thank you."),
            (7.0, 11.5, "the committee met twice before the vote was called"),
        ],
    )
    assert [s.text for s in segments].count("Thank you.") == 1, [(s.start_s, s.end_s, s.text) for s in segments]


def test_an_engine_repeat_moved_onto_a_short_region_is_still_dropped(monkeypatch, tmp_path):
    """The same repeat when the speech after the pause is a 0.3 s region: both
    copies move onto it and are no longer than it, so they overlap by less
    than the seam tolerance. Two cues of one text at one start are one cue."""
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 5.0), (7.0, 7.3), (8.5, 12.0)], total_s=12.0,
        engine=lambda i: [
            (0.0, 4.8, "we begin with the minutes of the last meeting"),
            (5.2, 6.0, "Thank you."),
            (5.3, 6.1, "Thank you."),
            (8.5, 11.5, "the committee met twice before the vote was called"),
        ],
    )
    assert [s.text for s in segments].count("Thank you.") == 1, [(s.start_s, s.end_s, s.text) for s in segments]


def test_a_cue_moved_out_of_a_pause_keeps_a_readable_span(monkeypatch, tmp_path):
    """The next cue starts AT the region start. f2e18ea capped the moved cue
    there: 7.0-7.0 ('assert (7.0 - 7.0) >= 0.4'), which the SRT writer
    stretched to 7.4 over the next cue. 4810da0 showed it for 1.5 s."""
    said = "and that is where it ended"
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 5.0), (7.0, 12.0)], total_s=12.0,
        engine=lambda i: [
            (0.0, 4.8, "we begin with the minutes of the last meeting"),
            (5.3, 6.8, said),
            (7.0, 11.5, "the committee met twice before the vote was called"),
        ],
    )
    got = _by_text(segments)[said]
    assert (got.start_s, got.end_s) == (7.0, 7.4)
    cues = _srt_cues(segments)
    for (a1, b1, _), (a2, _b2, _) in zip(cues, cues[1:]):
        assert b1 <= a2 + 1e-9, cues  # f2e18ea: 'assert 7.4 <= (7.0 + 1e-09)'


def test_a_short_cue_in_a_regions_padded_tail_and_the_pause_moves_to_its_speech(monkeypatch, tmp_path):
    """The engine opens 'Right.' where the previous cue ended (4.6 s, 0.4 s
    into the padded tail of a 5 s region) and ends it in the pause (6.5 s);
    the word is at 7.0 s. f2e18ea kept the foot as the cue's words, 4.6-5.0:
    squeezed to 0.4 s and still 2.4 s early ('assert (4.6 >= 6.0 or ...)').
    The foot is a graze, so the cue lies in the pause and moves out of it."""
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 5.0), (7.0, 12.0)], total_s=12.0,
        engine=lambda i: [
            (0.0, 4.6, "that was the end of the first part of the reading"),
            (4.6, 6.5, "Right."),
            (7.6, 11.8, "now the second part of the reading begins here"),
        ],
    )
    got = _by_text(segments)
    assert (got["Right."].start_s, got["Right."].end_s) == (7.0, 7.6)
    assert got["now the second part of the reading begins here"].start_s == 7.6


def test_a_cue_opened_in_a_short_replys_padded_tail_starts_on_its_own_speech(monkeypatch, tmp_path):
    """Interview shape: 'Yes.' is its own 0.9 s padded region (10.0-10.9);
    whisper times 'Yes.' at 10.1-10.4 and opens the answer where it ended,
    0.5 s into that region; the answer's words start at 12.5 s. More than
    half of that short region, so f2e18ea and 4810da0 alike kept it as the
    answer's first word: 2.1 s early ('assert 10.4 == 12.5 +- 1'). An
    earlier cue already has its words in that region, so it is a graze."""
    answer = "we met on the Tuesday and agreed the terms before anyone else arrived"
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 9.0), (10.0, 10.9), (12.5, 20.0)], total_s=20.0,
        engine=lambda i: [
            (0.0, 8.8, "and did the committee ever meet the delegation in person"),
            (10.1, 10.4, "Yes."),
            (10.4, 19.8, answer),
        ],
    )
    got = _by_text(segments)
    assert got[answer].start_s == 12.5
    assert (got["Yes."].start_s, got["Yes."].end_s) == (10.1, 10.4)


def test_a_graze_of_the_last_speech_in_the_window_is_still_clamped(monkeypatch, tmp_path):
    """Nothing after the pause to move to: the cue keeps its foot in the
    speech and ends where the speech does, as before."""
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 5.0)], total_s=8.0,
        engine=lambda i: [(0.0, 4.6, "that was the end of the reading"), (4.6, 6.5, "Right.")],
    )
    assert (_by_text(segments)["Right."].start_s, _by_text(segments)["Right."].end_s) == (4.6, 5.0)


def test_a_short_cue_inside_long_speech_does_not_move(monkeypatch, tmp_path):
    """A cue shorter than the edge, wholly inside a region an earlier cue
    reaches into, is not a graze of anything: it does not leave the region."""
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 10.0), (12.0, 20.0)], total_s=20.0,
        engine=lambda i: [
            (0.0, 3.0, "and did you sign it"),
            (3.0, 3.4, "No."),
            (3.4, 9.8, "I signed it the morning after the vote"),
            (12.0, 19.5, "and then the committee adjourned"),
        ],
    )
    assert (_by_text(segments)["No."].start_s, _by_text(segments)["No."].end_s) == (3.0, 3.4)


@pytest.mark.xfail(strict=True, reason=(
    "Known cost, asked for by the brief ('move a segment that falls in known silence to the next region's "
    "start'): a word the VAD dropped (0.2 s, under its 0.25 s minimum) that the engine timed right is moved "
    "onto the next speech, 1.2 s late here, and the next cue gives up the 0.4 s minimum cue. Nothing in the "
    "times tells it apart from a cue the engine opened in the pause (live: 3 of 3 clips)."))
def test_a_correctly_timed_word_the_vad_dropped_is_not_moved_late(monkeypatch, tmp_path):
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 5.0), (7.0, 12.0)], total_s=12.0,
        engine=lambda i: [
            (0.0, 4.8, "did you sign the letter before the meeting"),
            (5.8, 6.2, "No."),
            (7.0, 11.5, "I signed it the morning after the vote was called"),
        ],
    )
    assert abs(_by_text(segments)["No."].start_s - 5.8) <= 1.0


# ------------------------------------ QA round 2, the constants are pinned --
#
# Three changes survived QA's mutation run on f2e18ea: dropping `_merged`,
# raising `_REGION_EDGE_S` to 1.5 and lowering `_FAST_SPEECH_CHARS_PER_S` to
# 18. Each test below fails under its mutation.


def test_a_real_first_word_a_second_into_a_tail_keeps_the_cue_start(monkeypatch, tmp_path):
    """1.0 s into the previous speech's tail is more than the engine's own
    opening slack (0.49 s on the live 70 s clip): that is the cue's words."""
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 10.0), (11.9, 20.0)], total_s=20.0,
        engine=lambda i: [(0.0, 8.9, "that was the first part of it"), (9.0, 16.0, "So, what did we find out")],
    )
    assert _by_text(segments)["So, what did we find out"].start_s == 9.0


def test_an_honest_fast_cue_after_uncovered_speech_is_not_pulled_back(monkeypatch, tmp_path):
    """208 characters in 10.4 s is 20 a second, a fast but real speaker. After
    speech with no words (music the VAD took for speech) it stays put: only a
    stamp short by more than a second at 25 a second is a compressed one."""
    said = _R2_TAIL
    span = round(len(said) / 20.0, 2)
    segments, _ = _transcribe(
        monkeypatch, tmp_path, regions=[(0.0, 10.0), (11.0, 40.0)], total_s=40.0,
        engine=lambda i: [(0.0, 9.5, "welcome back to the reading room"), (28.0, 28.0 + span, said)],
    )
    assert _by_text(segments)[said].start_s == 28.0


def test_hand_built_regions_out_of_order_and_overlapping_snap_like_clean_ones():
    """A caller's regions need not be sorted or disjoint; the bisects are."""
    engine = [
        Segment(0.0, 4.8, "first thought", "en"),
        Segment(5.3, 6.6, "second thought", "en"),
        Segment(7.2, 11.5, "third thought", "en"),
    ]
    clean = transcribe.snap_to_regions(engine, [(0.0, 5.0), (7.0, 12.0)])
    assert transcribe.snap_to_regions(engine, [(7.0, 12.0), (2.5, 5.0), (0.0, 3.0)]) == clean
    assert clean[1].start_s == 7.0
