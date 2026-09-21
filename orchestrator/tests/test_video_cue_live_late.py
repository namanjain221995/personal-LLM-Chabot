"""Reviewer probe (2026-09-19): the live engine stamped a cue at the END of its
speech, ending in the pause; snap_to_regions let its start "graze" go and moved
it onto the next speaker. Replays of the worker whisper's own replies with the
regions webrtcvad found on the same clips (LibriSpeech, public domain).
Passes on 4810da0 behaviour (nothing moves), FAILS on a98d0cd, passes with the one-condition fix."""
import pytest

from app.video import transcribe, vad
from app.video.types import Segment

G8455_REGIONS = [(0.0, 16.22), (18.19, 24.65), (24.76, 50.81), (52.48, 64.46)]
G8455 = [
    (0.0, 4.38, 'I remained there alone for many hours, but I must acknowledge that before I left the chambers,'),
    (4.66, 7.84, 'I had gradually brought myself to look at the matter in another light.'),
    (8.66, 13.86, 'On arriving at home at my own residence, I found that our salon was filled with a brilliant company.'),
    (14.24, 15.78, 'Quite satisfied, said Eva.'),
    (17.98, 24.28, 'The ladies, in compliance with that softness of heart, which is their characteristic, are on one side,'),
    (25.14, 29.04, 'and the men, by whom the world has to be managed, are on the other.'),
    (32.88, 33.32, 'No doubt, in process of time, the ladies will follow.'),
    (35.38, 35.76, "They're masters, said Mrs. Neverbend."),
    (41.3, 41.76, 'I did not mean, said Captain Battleaxe, to touch upon public subjects at such a moment as this.'),
    (44.26, 44.86, 'Mrs. Neverbend, you must indeed be proud of your son.'),
    (47.72, 48.16, 'Sir Kennington Oval is a very fine player, said my wife.'),
    (50.36, 52.26, "Oh yes, said Jack, and I'm nowhere."),
    (54.64, 55.08, 'But I mean to have my innings before long.'),
    (55.44, 59.28, 'We sat with the officer some little time after dinner, and then went ashore.'),
    (59.7, 62.4, 'What could I do now but just lay myself down and die?'),
    (62.78, 64.1, 'It is a duty, said I.'),
]
H3570_REGIONS = [(0.0, 16.64), (18.19, 50.75), (52.48, 61.67)]
H3570 = [
    (0.0, 4.42, 'But already at a point in economic evolution far antedating the emergence of the lady,'),
    (4.8, 8.38, 'specialized consumption of goods as an evidence of pecuniary strength'),
    (8.38, 11.18, 'had begun to work out in a more or less elaborate system.'),
    (11.56, 16.24, 'The utility of consumption as an evidence of wealth is to be classed as a derivative growth.'),
    (18.1, 22.14, 'Such consumption as falls to the women is merely incidental to their work.'),
    (22.14, 24.26, 'It is a means to their continued labor,'),
    (24.68, 28.1, 'and not a consumption directed to their own comfort and fullness of life.'),
    (34.76, 35.3, 'With a further advance in culture, this taboo may change into simple custom of a more or less rigorous character,'),
    (39.16, 39.8, 'but whatever be the theoretical basis of the distinction which is maintained,'),
    (42.56, 43.08, 'whether it be a taboo or a larger conventionality,'),
    (47.16, 47.72, 'the features of the conventional scheme of consumption do not change easily.'),
    (50.32, 52.22, 'There is a more or less elaborate system of rank and grades.'),
    (57.6, 58.1, 'This differentiation is furthered by the inheritance of wealth and the consequent inheritance of gentility.'),
    (61.2, 61.67, 'but the general distinction is not on that account to be overlooked.'),
]


def _snapped(regions, cues):
    win = vad.Window(0.0, regions[-1][1], regions=tuple(regions))
    segs = transcribe.snap_to_regions([Segment(a, b, t, "en") for a, b, t in cues], win.regions)
    return {s.text: s for s in transcribe.stitch([(win, segs)])}


@pytest.mark.parametrize("regions,cues,text,said_at", [
    (G8455_REGIONS, G8455, "Oh yes, said Jack, and I'm nowhere.", 48.16),
    (H3570_REGIONS, H3570, "There is a more or less elaborate system of rank and grades.", 47.54),
])
def test_a_late_cue_ending_in_the_pause_is_not_moved_onto_the_next_speaker(regions, cues, text, said_at):
    got = _snapped(regions, cues)[text]
    # 4810da0: 50.36 / 50.32 (2.2 / 2.8 s late). a98d0cd: 52.48 (4.3 / 4.9 s late, over the next speaker).
    assert got.start_s < 52.48, (got.start_s, got.end_s)
    assert got.start_s - said_at <= 2.8 + 1e-6
