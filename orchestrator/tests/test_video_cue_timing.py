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
import wave

import pytest

from app import asr
from app.video import artifacts as art
from app.video import transcribe, vad

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
    assert starts == [0.31, 15.07, 24.61, 49.48]
    for got, known in zip(starts, [0.50, 15.29, 25.29, 50.37]):
        assert abs(got - known) <= 1.0
    # The compressed cue now spans the two regions its words fill, and lets
    # go of the 0.09 s edge of the next one.
    assert round(segments[2].end_s, 2) == 47.51


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
    assert got["second thought"].end_s == pytest.approx(8.3)  # its own length, now on speech
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
