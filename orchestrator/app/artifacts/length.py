"""How long the person asked the file to be, read from their English.

WHY THIS EXISTS (owner report, 2026-09-17). "Big report" came back as two
pages. Nothing in the composer mapped a size word to a number: the request
reached the model as prose, the Fast prompt asked it to "Be concise and
concrete", and the single 12,000-token call had no reason to write more.
The QA baseline measured it — the median file turn was 2 pages, the length
dimension scored 0.640, and the big_report category 0.737.

WHAT IT DOES. One pure function, `parse_size`, turns the request's words
into a `LengthTarget`: a word count for a document, a slide count for a
deck, the phrase that decided it, and whether the person named a size at
all. Nothing here calls a model, touches a file or reads settings, so the
composer can consult it before it spends a token and the tests can pin
every number.

THE MAPPING (CONTRACT-3 §B.1). English only, which is the owner's rule for
this round:

    big, large, lengthy, detailed, in-depth, thorough   3,000 words
    long, longer, expanded, extended NEXT TO the
        deliverable ("a long report", not "the long
        tail" or "how long does onboarding take")       3,000 words
    comprehensive, extensive, exhaustive                4,500 words
    complete, full, whole, entire NEXT TO the
        deliverable ("the full report", not "the
        full quarter")                                  4,500 words
    "N pages"                                           N x 450 words
    "N words"                                           N words
    "at least N", "N+", "N or more"                     the same, as a floor
    "N slides"                                          N slides
    short, brief, one-page, summary, concise            no growth target

WHY THE PROXIMITY GUARD (QA B-3, measured on the integration tree
2026-09-18). `long` fired wherever it appeared, so "how long does
onboarding take? write a note", "a report on the long tail of small
customers", "why the queue is no longer draining", "our extended warranty
programme" and "the expanded team onboarding" each asked for 3,000 words.
3,000 is over compose.SECTIONED_WRITER_WORDS, so every one of them took the
sectioned path — nine model calls, each re-prefilling the whole material on
the TP=2 engine that also serves live chat — for a request nobody sized.
The size a request asks for has to be EXPLICIT, which for these four words
means describing the deliverable.

A growth word and a shrink word in the same request mean growth: "a
comprehensive report, not a summary" and "a big report with an executive
summary" are both asks for a big report, and reading their shrink word
would put the 2-page document straight back.

THE DATA-REPORT FLOOR. A request that says report, analysis, overview or
understand over material that HAS data (tables or sources) and names no
size gets 1,500 words — three pages. A dataset deserves more than a page
of prose, and the QA case R03 ("give me a big report" on a 100-row CSV) is
only the loudest version of a request people make without a size word. A
shrink word disables it, which is what keeps a one-page brief one page.

THE CEILINGS are the renderers' own (`types.MAX_PAGES`, `types.MAX_SLIDES`):
no target may ask for a file the renderer would refuse to make.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import types as T

#: A page of a report in words. 450 is what the renderer's body text, its
#: 11pt leading and its A4 margins actually fit (measured on the PDF
#: samples in tests/data), not a round guess.
WORDS_PER_PAGE = 450

#: What each family of size words is worth.
BIG_WORDS = 3_000
COMPREHENSIVE_WORDS = 4_500

#: A report over real data with no size word at all.
DATA_REPORT_FLOOR = 1_500

#: One top-level section, in words. Short enough that the model writes
#: prose rather than a bullet list, long enough that 3,000 words is not
#: thirty headings. `sections_for` divides by it; the composer, which
#: derives a word target from the number of sections a request NAMES,
#: multiplies by it — one constant, read in both directions.
WORDS_PER_SECTION = 400

#: The renderers' hard ceilings, in the units this module speaks.
MAX_WORDS = T.MAX_PAGES * WORDS_PER_PAGE      # 27,000
MAX_SLIDES = T.MAX_SLIDES                     # 40


@dataclass(frozen=True)
class LengthTarget:
    """What the file should come to.

    `words` is 0 when nothing asked the document to grow — that is the
    normal case and it means "compose as before". `explicit` is True when
    the person NAMED a size (grow or shrink); it is False for the
    data-report floor, which is code's judgement and not the person's
    words. `phrase` is the wording that decided it, for the prompt and for
    the version warning.
    """

    words: int = 0
    slides: int = 0
    phrase: str = ""
    explicit: bool = False

    def __bool__(self) -> bool:
        return bool(self.words or self.slides)

    @property
    def pages(self) -> int:
        """The word target as whole pages, for the wording of a warning."""
        return max(1, round(self.words / WORDS_PER_PAGE)) if self.words else 0


# ------------------------------------------------------------- the words --

#: Size words that are also ordinary English ("the full year", "a complete
#: list of ids", "the long tail"). They count only when they describe the
#: DELIVERABLE, so they need one of its nouns within two words. Without
#: this guard, "a report on the full quarter" asked for 4,500 words.
_DELIVERABLE = (r"(?:report|document|doc|docs|documentation|write[-\s]?up|writeup|paper|analysis|study|review|guide|"
                r"overview|breakdown|deep[-\s]?dive|memo|brief(?:ing)?|dossier|deck|presentation|slides?|file|pdf|docx|word)")

#: What may stand between a size word and the deliverable it describes:
#: more adjectives ("a long detailed report"), never a verb. "how long IS
#: the report?" asks a question; before 2026-09-18 it ordered a 3,000-word
#: one, because `long` fired wherever it appeared.
_NOT_AN_ADJECTIVE = (r"(?:is|are|was|were|be|been|being|do|does|did|will|would|can|could|should|shall|may|might|"
                     r"must|has|have|had|take|takes|took|get|gets|got)")
_ADJECTIVES_BETWEEN = rf"(?:\s+(?!{_NOT_AN_ADJECTIVE}\b)\w+){{0,2}}"

#: Size words that mean one thing. They fire wherever they appear.
_BIG_WORDS = (r"(?:big|bigger|biggest|large|larger|lengthy|detailed|"
              r"in[-\s]?depth|in\s+detail|thorough|elaborate)")
#: Growth words that are ALSO ordinary English, so they get the same
#: deliverable-proximity guard as complete/full/whole/entire. Measured on
#: the integrated tree (QA B-3, 2026-09-18): "how long does onboarding
#: take? write a note", "the long tail of small customers", "why the queue
#: is no longer draining", "our extended warranty programme" and "the
#: expanded team onboarding" each asked for 3,000 words — over
#: SECTIONED_WRITER_WORDS, so each took the nine-call sectioned path on the
#: TP=2 engine that also serves live chat.
_BIG_NEAR_WORDS = r"(?:long|longer|expanded|extended)"
_COMPREHENSIVE_WORDS = r"(?:comprehensive|extensive|exhaustive)"
_BIG_RE = re.compile(rf"\b{_BIG_WORDS}\b", re.IGNORECASE)
_BIG_NEAR_RE = re.compile(rf"\b{_BIG_NEAR_WORDS}\b{_ADJECTIVES_BETWEEN}\s+{_DELIVERABLE}\b", re.IGNORECASE)
_COMPREHENSIVE_RE = re.compile(rf"\b{_COMPREHENSIVE_WORDS}\b", re.IGNORECASE)

_AMBIGUOUS_RE = re.compile(
    rf"\b(?:complete|full|whole|entire)\b(?!\s+(?:the|this|that|these|those|my|your|our|their|it)\b)"
    rf"{_ADJECTIVES_BETWEEN}\s+{_DELIVERABLE}\b",
    re.IGNORECASE,
)

#: The qualifiers that turn `summary` from a size word into a SECTION NAME.
#: An executive summary is the first heading of most reports. The owner's
#: request of 2026-09-22 numbered "1. Executive Summary" as section one of
#: fifteen, the bare `summary` alternative below matched it at offset 363,
#: and `parse_size` returns at its shrink branch BEFORE the data-report
#: floor is considered — so a fifteen-section technical report was read as
#: "the person asked for less" and came back as four pages. One negative
#: lookbehind per qualifier, each fixed-width, which is exactly the guard
#: `_AMBIGUOUS_RE` already gives complete|full|whole|entire. Deliberately
#: narrow: "a short summary", "a concise summary" and "summarise the
#: release" all still ask for less.
_SECTION_NAME_BEFORE_SUMMARY = ("executive", "management", "technical")
_NOT_A_SECTION_NAME = "".join(rf"(?<!{word}[\s-])" for word in _SECTION_NAME_BEFORE_SUMMARY)

#: Words that ask for LESS. "Summary" is here as a document kind ("a short
#: summary"), which is why a growth word in the same request wins over it:
#: "include an executive summary" names a section, not the file's size.
_SHRINK_RE = re.compile(
    r"\b(?:short|shorter|shortest|brief|briefly|briefer|concise|concisely|succinct|terse|snappy|crisp|"
    r"one[-\s]?pager?|1[-\s]?pager?|single[-\s]?page|half[-\s]?(?:a[-\s]?)?page|"
    + _NOT_A_SECTION_NAME +
    r"(?:summary|summarise|summarize|summarised|summarized)|tl;?dr|high[-\s]?level|at[-\s]?a[-\s]?glance|"
    r"quick|bullet[-\s]?points?\s+only|keep\s+it\s+(?:short|small|tight)|not\s+too\s+long|no\s+fluff)\b",
    re.IGNORECASE,
)

#: A request whose subject is the data itself.
_REPORT_RE = re.compile(r"\b(?:report|analysis|analyse|analyze|analytics|overview|understand|understanding|insights?|findings)\b", re.IGNORECASE)

_LEAD = (r"(?:at\s+least\s+|atleast\s+|minimum\s+(?:of\s+)?|min\.?\s+|no\s+(?:less|fewer)\s+than\s+|"
         r"more\s+than\s+|over\s+|about\s+|around\s+|approx(?:\.|imately)?\s+|roughly\s+|~\s*)?")
_TAIL = r"\s*(?P<plus>\+)?\s*(?:or\s+(?:more|so)\s+)?[-\s]?"

_PAGES_RE = re.compile(rf"(?P<lead>{_LEAD})(?P<n>\d[\d,]*){_TAIL}pages?\b", re.IGNORECASE)
_WORDS_RE = re.compile(rf"(?P<lead>{_LEAD})(?P<n>\d[\d,]*){_TAIL}words?\b", re.IGNORECASE)
_SLIDES_RE = re.compile(rf"(?P<lead>{_LEAD})(?P<n>\d[\d,]*){_TAIL}slides?\b", re.IGNORECASE)

#: The counts people spell out. Only up to twenty plus the round tens: past
#: that everyone types digits.
_SPELLED = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
}
_SPELLED_RE = re.compile(rf"(?P<lead>{_LEAD})(?P<n>{'|'.join(_SPELLED)})[-\s]+(?P<unit>pages?|slides?)\b", re.IGNORECASE)


def _int(text: str) -> Optional[int]:
    try:
        return int(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _numbered(text: str) -> Tuple[List[Tuple[int, str]], List[Tuple[int, str]]]:
    """(word candidates, slide candidates) the request states as numbers,
    each with the phrase that carried it. A single page is not a growth
    target — "a one-page brief" is the opposite of one."""
    words: List[Tuple[int, str]] = []
    slides: List[Tuple[int, str]] = []
    for m in _PAGES_RE.finditer(text):
        n = _int(m.group("n"))
        if n is not None and n >= 2:
            words.append((n * WORDS_PER_PAGE, m.group(0).strip()))
    for m in _WORDS_RE.finditer(text):
        n = _int(m.group("n"))
        if n is not None and n >= 100:
            words.append((n, m.group(0).strip()))
    for m in _SLIDES_RE.finditer(text):
        n = _int(m.group("n"))
        if n is not None and n >= 1:
            slides.append((n, m.group(0).strip()))
    for m in _SPELLED_RE.finditer(text):
        n = _SPELLED[m.group("n").lower()]
        if m.group("unit").lower().startswith("slide"):
            slides.append((n, m.group(0).strip()))
        elif n >= 2:
            words.append((n * WORDS_PER_PAGE, m.group(0).strip()))
    return words, slides


def shrink_asked(instruction: str) -> bool:
    """True when the request asks for a SHORT file and nothing in it asks
    for a big one. Public because the composer's data-report floor and its
    growth pass both have to honour it."""
    text = instruction or ""
    if _BIG_RE.search(text) or _BIG_NEAR_RE.search(text) or _COMPREHENSIVE_RE.search(text) or _AMBIGUOUS_RE.search(text):
        return False
    words, _slides = _numbered(text)
    if words:
        return False
    return bool(_SHRINK_RE.search(text))


def is_data_report(instruction: str) -> bool:
    """True when the request asks for a report, an analysis, an overview or
    an understanding of something — the wording that, over real rows, means
    a report ABOUT THE DATA."""
    return bool(_REPORT_RE.search(instruction or ""))


def parse_size(instruction: str, kind: str = "document", *, has_data: bool = False) -> LengthTarget:
    """The size the request asks for.

    `has_data` says the material carries tables or sources — the only fact
    outside the instruction this reads, and only for the data-report floor.
    A word target is for documents; a slide target is for decks. Workbooks
    get neither: a sheet is as long as its rows (out of scope, CONTRACT-3).
    """
    text = " ".join((instruction or "").split())
    numbered_words, numbered_slides = _numbered(text)

    slides = 0
    slide_phrase = ""
    if numbered_slides:
        slides, slide_phrase = max(numbered_slides)
        slides = min(slides, MAX_SLIDES)

    if kind != "document":
        # A deck's length is its slide count; a workbook has none.
        phrase = slide_phrase if kind == "presentation" else ""
        return LengthTarget(words=0, slides=slides if kind == "presentation" else 0,
                            phrase=phrase, explicit=bool(phrase))

    candidates: List[Tuple[int, str]] = list(numbered_words)
    m = _BIG_RE.search(text)
    if m:
        candidates.append((BIG_WORDS, m.group(0)))
    m = _BIG_NEAR_RE.search(text)
    if m:
        candidates.append((BIG_WORDS, m.group(0)))
    m = _COMPREHENSIVE_RE.search(text)
    if m:
        candidates.append((COMPREHENSIVE_WORDS, m.group(0)))
    m = _AMBIGUOUS_RE.search(text)
    if m:
        candidates.append((COMPREHENSIVE_WORDS, m.group(0)))

    if candidates:
        words, phrase = max(candidates)
        return LengthTarget(words=min(words, MAX_WORDS), slides=slides, phrase=phrase, explicit=True)

    if _SHRINK_RE.search(text):
        # The person asked for less. No growth target, and the data-report
        # floor below must not put one back.
        return LengthTarget(words=0, slides=slides, phrase=_SHRINK_RE.search(text).group(0), explicit=True)

    if has_data and is_data_report(text):
        return LengthTarget(words=DATA_REPORT_FLOOR, slides=slides, phrase="a report over data", explicit=False)

    return LengthTarget(words=0, slides=slides, phrase=slide_phrase, explicit=bool(slide_phrase))


# ------------------------------------------------------------- the shapes --


def sections_for(words: int) -> int:
    """How many top-level sections a word target needs."""
    return max(1, math.ceil(max(0, int(words)) / WORDS_PER_SECTION))


def section_words(words: int, sections: int) -> int:
    """The per-section target handed to one scoped write."""
    if sections <= 0:
        return 0
    return max(120, int(round(words / sections)))


__all__ = [
    "LengthTarget", "parse_size", "shrink_asked", "is_data_report", "sections_for", "section_words",
    "WORDS_PER_PAGE", "WORDS_PER_SECTION", "BIG_WORDS", "COMPREHENSIVE_WORDS", "DATA_REPORT_FLOOR",
    "MAX_WORDS", "MAX_SLIDES",
]
