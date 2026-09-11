"""Whisper repetition loops (app/video/loops.py).

Every "from the recording" case below is copied from the transcript of the
2h23m Gujarati/Hindi/English video a person uploaded on 2026-09-11 (analysis
28): the counts are the counts that were actually in the file.
"""
from __future__ import annotations

from app.video import loops
from app.video.types import Segment


def _seg(start, end, text, language="en"):
    return Segment(start_s=start, end_s=end, text=text, language=language)


# ------------------------------------------------------- inside one cue --


def test_a_word_repeated_four_hundred_times_becomes_two():
    """From the recording: one cue at 8374.1 s held "no" 434 times."""
    assert loops.clean_text("no " * 434) == "no no"


def test_two_in_a_row_is_speech_and_is_left_alone():
    """A person saying a word twice is not a decoder in a cycle. The floor is
    three, so ordinary emphasis survives untouched."""
    assert loops.clean_text("no, no, I said Tuesday") == "no, no, I said Tuesday"
    assert loops.clean_text("very very good") == "very very good"


def test_a_repeated_phrase_is_found_as_a_phrase():
    """From the recording: "It will happen," 110 times and "It will match
    with the IDC," 55 times. Scanning single words alone finds neither — no
    individual word repeats — so the scan takes the longest block it can."""
    text = loops.clean_text("It will happen, " * 110)
    assert text == "It will happen, It will happen,"

    six = "It will match with the IDC, "
    assert loops.clean_text(six * 55) == (six + six).strip()


def test_a_ten_word_loop_is_within_reach():
    """From the recording, and the reason the phrase limit is not eight: one
    cue repeated a TEN-word phrase 40 times, 2,178 characters of it, and
    survived an earlier eight-word limit completely intact."""
    ten = "is not possible to develop only with an extension, it "
    assert loops.clean_text(ten * 40) == (ten + ten).strip()


def test_a_long_genuine_cue_is_not_touched():
    """The longest real cue in the same transcript is 113 words with no
    back-to-back repeat. Length alone must never be the trigger."""
    text = " ".join(f"word{i}" for i in range(113))
    assert loops.clean_text(text) == text


def test_the_speech_around_a_loop_survives_it():
    assert loops.clean_text("सबका देजिए " + "फॉल्ड " * 53 + "और बस") == "सबका देजिए फॉल्ड फॉल्ड और बस"


def test_a_character_cycling_inside_one_word_is_cut_to_three():
    """From the recording: `ਹ` plus its vowel sign, 59 times, as one word."""
    assert loops.clean_text("ਹ" + "ੱ" * 59) == "ਹ" + "ੱ" * 3
    # Three or fewer is left exactly as written — no script needs a fourth,
    # and nothing below the threshold is anyone's business.
    assert loops.clean_text("aaa") == "aaa"


def test_cleaning_is_idempotent():
    once = loops.clean_text("no " * 434)
    assert loops.clean_text(once) == once


def test_empty_and_blank_text_are_safe():
    assert loops.clean_text("") == ""
    assert loops.clean_text("   ") == ""


# --------------------------------------------------------- across cues --


def test_eighty_five_identical_cues_become_one_spanning_the_whole_run():
    """From the recording: "I am a" filled 85 cues between 2177.83 s and
    2193.77 s — five a second. The merged cue keeps the full span, so the
    transcript still says those words were spoken over those sixteen
    seconds; it stops claiming they were spoken 85 times."""
    segs = [_seg(2177.83 + i * 0.19, 2178.23 + i * 0.19, "I am a") for i in range(85)]
    out, report = loops.collapse(segs)

    assert [s.text for s in out] == ["I am a"]
    assert out[0].start_s == 2177.83
    assert out[0].end_s == segs[-1].end_s
    assert report["cues_before"] == 85 and report["cues_after"] == 1
    assert report["cue_runs_merged"] == 1


def test_two_identical_cues_in_a_row_are_kept_as_two():
    segs = [_seg(0.0, 1.0, "Yes."), _seg(4.0, 5.0, "Yes.")]
    out, report = loops.collapse(segs)
    assert len(out) == 2 and report["cue_runs_merged"] == 0


def test_a_run_is_recognised_through_case_and_spacing():
    segs = [_seg(0.0, 1.0, "Thank you."), _seg(1.0, 2.0, "thank  you."), _seg(2.0, 3.0, "THANK YOU.")]
    out, _ = loops.collapse(segs)
    assert [s.text for s in out] == ["Thank you."], "the first cue's own spelling is the one kept"


def test_the_speech_on_either_side_of_a_run_is_untouched():
    segs = [
        _seg(0.0, 2.0, "So the ATS score is stored where?"),
        _seg(2.0, 2.2, "I am a"),
        _seg(2.2, 2.4, "I am a"),
        _seg(2.4, 2.6, "I am a"),
        _seg(2.6, 5.0, "In the candidate object."),
    ]
    out, report = loops.collapse(segs)
    assert [s.text for s in out] == [
        "So the ATS score is stored where?",
        "I am a",
        "In the candidate object.",
    ]
    assert report["cue_runs_merged"] == 1


def test_a_cue_emptied_by_cleaning_is_dropped_not_kept_blank():
    out, _ = loops.collapse([_seg(0.0, 1.0, "   "), _seg(1.0, 2.0, "real words")])
    assert [s.text for s in out] == ["real words"]


def test_language_and_order_survive_the_repair():
    segs = [_seg(0.0, 1.0, "તો આપણે " * 33, "gu"), _seg(1.0, 2.0, "next", "en")]
    out, _ = loops.collapse(segs)
    assert [s.language for s in out] == ["gu", "en"]
    assert out[0].start_s < out[1].start_s


def test_a_clean_transcript_is_returned_unchanged_and_reports_nothing():
    segs = [_seg(0.0, 2.0, "One."), _seg(2.0, 4.0, "Two."), _seg(4.0, 6.0, "Three.")]
    out, report = loops.collapse(segs)
    assert [s.text for s in out] == ["One.", "Two.", "Three."]
    assert report["chars_removed"] == 0 and report["cue_runs_merged"] == 0
    assert loops.describe(report) == ""


def test_the_repair_is_described_for_whoever_reads_the_run():
    segs = [_seg(float(i), i + 0.2, "I am a") for i in range(5)]
    _, report = loops.collapse(segs)
    assert "4 repeated cue(s) merged" in loops.describe(report)


def test_collapse_of_an_empty_transcript_is_empty():
    out, report = loops.collapse([])
    assert out == [] and report["cues_before"] == 0 and loops.describe(report) == ""
