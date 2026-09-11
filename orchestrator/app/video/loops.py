"""Whisper repetition loops, and how a transcript is cleaned of them.

WHAT THIS IS FOR. An autoregressive decoder that loses its place repeats
itself, and Whisper does it often enough to have a name for it. The model is
not confused about the audio; it has fallen into a cycle and keeps emitting
the same token until the window ends. The result is not a mis-transcription —
it is text that was never spoken, in quantity.

MEASURED, on the 2h23m Gujarati/Hindi/English recording a person uploaded on
2026-09-11 (analysis 28, 1,698 cues, 114,266 characters):

    "no" 434 times inside ONE cue
    "It will happen," 110 times back to back
    "It will match with the IDC," 55 times
    "यूज़" 55 times, "ਹ" with its vowel sign 59 times inside a single word
    "I am a" as 85 SEPARATE cues spanning 15.9 seconds — five a second
    265 of the 1,698 cues were part of a run of identical consecutive cues

Together that is 8.6% of the characters inside cues plus 15.6% of the cues.
In the chat answer it reads as noise; in the WebVTT file it is 20 ms cues
stacked on top of each other, which is what the person actually saw.

WHY THIS IS A POST-PROCESSING STEP AND NOT AN ENGINE SETTING. The decoder
settings usually reached for — `compression_ratio_threshold` with the
temperature fallback — were measured on this deployment on 2026-09-08 and
rejected: slower on every clip, and on the 128-second Hindi-English recording
actively worse (1080 characters against 1364, whole utterances dropped). That
measurement stands, and this module does not re-litigate it. It also does not
depend on it: a loop is identifiable from its OUTPUT, exactly and cheaply,
whatever engine or settings produced it. Cleaning it here means the fix
applies to transcripts already on disk and cannot make a good clip worse.

WHAT IS DELIBERATELY LOST. A person who says a word three times in a row gets
two of them, and three identical cues in a row become one cue spanning all
three. The transcript still says the words at the time they were said; what
it stops claiming is how many times. That is the right side of the trade when
the alternative is 434.

Everything here is pure text: no model, no I/O, no settings. It is written to
be run over a stored transcript as easily as over a fresh one.
"""
from __future__ import annotations

import re
from typing import Dict, List, Sequence, Tuple

from .types import Segment

#: Copies of a repeated unit kept when a run is collapsed. Two, not one: the
#: second copy is what keeps genuine emphasis ("no, no") readable, and the
#: difference between two and four hundred is the entire problem.
_KEEP = 2

#: A run must reach this many back-to-back copies before it is treated as a
#: loop. Three is the first count that is hard to say by accident and easy to
#: produce by decoding: two is ordinary speech and is never touched.
_MIN_RUN = 3

#: The longest repeated phrase looked for, in words. Sized from the recording
#: rather than guessed: "It will match with the IDC," is six and "is not
#: possible to develop only with an extension, it" is TEN, repeated 40 times
#: inside one cue — a limit of eight left that one cue, 2,178 characters of
#: it, untouched. Sixteen clears the longest loop seen with room over, and the
#: longest genuine cue in the same transcript (113 words) contains no
#: back-to-back repeat at all, so nothing real is within reach of it.
_MAX_PHRASE_WORDS = 16

#: Identical characters in a row kept inside one word. Three: no script writes
#: a fourth, and `ਹੱੱੱੱੱੱੱ…` (59 of them) is a decoder cycling on one token.
_MAX_CHAR_RUN = 3


def _collapse_char_runs(word: str) -> str:
    """`ਹੱੱੱੱੱੱੱ…` -> `ਹੱੱੱ`. Combining marks included, which is the case seen."""
    return re.sub(r"(.)\1{%d,}" % _MAX_CHAR_RUN, lambda m: m.group(1) * _MAX_CHAR_RUN, word)


def _collapse_phrases(words: Sequence[str]) -> Tuple[List[str], int]:
    """Collapse every run of a back-to-back repeated phrase.

    Walks left to right; at each position it looks for the repeated block that
    COVERS THE MOST WORDS, so "It will happen, it will happen, …" is found as a
    three-word phrase repeated 110 times rather than as no single-word run at
    all. Returns the words kept and how many were dropped.
    """
    out: List[str] = []
    dropped = 0
    i = 0
    n_words = len(words)
    while i < n_words:
        best_repeats = 0
        best_size = 0
        for size in range(1, _MAX_PHRASE_WORDS + 1):
            if i + size * _MIN_RUN > n_words:
                break  # not even the minimum run fits; longer phrases cannot either
            block = words[i : i + size]
            repeats = 1
            j = i + size
            while words[j : j + size] == block:
                repeats += 1
                j += size
            if repeats >= _MIN_RUN and repeats * size > best_repeats * best_size:
                best_repeats, best_size = repeats, size
        if best_repeats:
            kept = min(best_repeats, _KEEP)
            out.extend(list(words[i : i + best_size]) * kept)
            dropped += (best_repeats - kept) * best_size
            i += best_repeats * best_size
        else:
            out.append(words[i])
            i += 1
    return out, dropped


def clean_text(text: str) -> str:
    """One cue's text with its repetition loops collapsed.

    Whitespace is normalised on the way through, because a loop that ran to
    the end of a window often leaves a ragged tail behind it.
    """
    words = [_collapse_char_runs(w) for w in (text or "").split()]
    kept, _ = _collapse_phrases(words)
    return " ".join(kept)


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def collapse(segments: Sequence[Segment]) -> Tuple[List[Segment], Dict[str, int]]:
    """A transcript with its loops removed, and what was removed.

    Two passes, because the two shapes are different failures of the same
    decoder: a loop that stays inside one cue ("no no no …" 434 times) and a
    loop that emits a fresh cue each time round ("I am a", 85 cues in 15.9 s).
    The second pass merges a run of identical cues into ONE cue spanning the
    whole run, so the time the words occupy is preserved exactly.

    The report is for the stage detail and the log: silent repair of a
    transcript this size should be visible to whoever reads the run.
    """
    before_chars = sum(len(s.text) for s in segments)

    tidied: List[Segment] = []
    for s in segments:
        text = clean_text(s.text)
        if text:
            tidied.append(Segment(s.start_s, s.end_s, text, s.language))

    merged: List[Segment] = []
    runs_merged = 0
    i = 0
    while i < len(tidied):
        j = i + 1
        key = _norm(tidied[i].text)
        while j < len(tidied) and _norm(tidied[j].text) == key:
            j += 1
        run = j - i
        if run >= _MIN_RUN:
            first, last = tidied[i], tidied[j - 1]
            merged.append(Segment(first.start_s, max(last.end_s, first.end_s), first.text, first.language))
            runs_merged += 1
        else:
            merged.extend(tidied[i:j])
        i = j

    report = {
        "cues_before": len(segments),
        "cues_after": len(merged),
        "cue_runs_merged": runs_merged,
        "chars_removed": before_chars - sum(len(s.text) for s in merged),
    }
    return merged, report


def describe(report: Dict[str, int]) -> str:
    """One line for the stage detail, or "" when there was nothing to repair."""
    cues = report.get("cues_before", 0) - report.get("cues_after", 0)
    chars = report.get("chars_removed", 0)
    if not cues and not chars:
        return ""
    parts = []
    if cues:
        parts.append(f"{cues} repeated cue(s) merged")
    if chars:
        parts.append(f"{chars} looped character(s) removed")
    return " · ".join(parts)
