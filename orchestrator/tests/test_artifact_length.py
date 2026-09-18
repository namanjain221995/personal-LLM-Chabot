"""English size wording → a word or slide target (app/artifacts/length.py).

The owner's report of 2026-09-17: "Big report" produced two pages. Nothing
mapped a size word to a number, so these are the numbers — pinned, because
every other part of the round (the prompt, the token ceiling, the section
caps, the sectioned writer, the short-draft pass) reads them.
"""
from __future__ import annotations

from app.artifacts import length as L
from app.artifacts import types as T


def test_size_words_map_to_targets():
    # The two families of size words.
    assert L.parse_size("please give Big report ??", "document").words == 3_000
    for word in ("a large report", "a long report", "write it in detail — a detailed report",
                 "an in-depth report", "a thorough report"):
        assert L.parse_size(word, "document").words == 3_000, word
    assert L.parse_size("comprehensive, not a summary", "document").words == 4_500
    for word in ("an extensive write-up", "an exhaustive report", "the complete report"):
        assert L.parse_size(word, "document").words == 4_500, word

    # Pages and words, with and without a floor wording.
    assert L.parse_size("at least 5 pages", "document").words == 5 * L.WORDS_PER_PAGE == 2_250
    assert L.parse_size("10+ pages", "document").words == 4_500
    assert L.parse_size("make it 8 pages", "document").words == 3_600
    assert L.parse_size("~5,000 words", "document").words == 5_000
    assert L.parse_size("about 1200 words please", "document").words == 1_200

    # Slides are a deck's length.
    assert L.parse_size("16 slides", "presentation").slides == 16
    assert L.parse_size("a deck of sixteen slides", "presentation").slides == 16
    assert L.parse_size("16 slides", "presentation").words == 0, "a deck's target is slides, not words"

    # The renderers' ceilings bound every target.
    assert L.parse_size("a 100 page manual", "document").words == 27_000 == T.MAX_PAGES * L.WORDS_PER_PAGE
    assert L.parse_size("80,000 words", "document").words == 27_000
    assert L.parse_size("120 slides", "presentation").slides == T.MAX_SLIDES == 40

    # Shrink words mean no growth target at all.
    for text in ("one-page brief", "short summary", "a concise summary", "keep it brief"):
        target = L.parse_size(text, "document")
        assert target.words == 0, text
        assert target.explicit is True, "the person named a size; the data floor must not put one back"

    # A growth word and a shrink word together mean growth: "include an
    # executive summary" names a section, not the size of the file.
    assert L.parse_size("a big report with an executive summary", "document").words == 3_000
    assert L.parse_size("comprehensive, not a summary", "document").explicit is True

    # Words that only look like size words.
    assert L.parse_size("a long-term strategy note", "document").words == 0
    assert L.parse_size("write up the full quarter's numbers", "document").words == 0


def test_a_plain_report_over_data_gets_the_1500_word_floor_and_a_brief_does_not():
    # A report over real rows with no size word: three pages, not one.
    for text in ("make a report on this data", "analysis of the uploaded csv", "an overview of this file",
                 "help me understand this data"):
        target = L.parse_size(text, "document", has_data=True)
        assert target.words == L.DATA_REPORT_FLOOR == 1_500, text
        assert target.explicit is False, "code's floor, not the person's words"

    # No data behind it: nothing to write 1,500 words from.
    assert L.parse_size("make a report on this data", "document", has_data=False).words == 0
    # Not a report at all.
    assert L.parse_size("write a thank-you note", "document", has_data=True).words == 0
    # A brief is a brief (QA case R07): the floor never grows a one-pager.
    for text in ("a one-page brief on this data", "a short summary of the csv", "a concise overview of this data"):
        assert L.parse_size(text, "document", has_data=True).words == 0, text
    # A size the person DID name still wins over the floor.
    assert L.parse_size("a big report on this data", "document", has_data=True).words == 3_000
    # Workbooks have no length target: a sheet is as long as its rows.
    assert L.parse_size("a big report on this data", "workbook", has_data=True).words == 0


def test_sections_and_per_section_words_follow_the_target():
    assert L.sections_for(3_000) == 8 and L.sections_for(4_500) == 12 and L.sections_for(9_000) == 23
    assert L.section_words(3_000, 8) == 375
    assert L.section_words(0, 0) == 0


def test_shrink_asked_reads_only_a_request_that_wants_less():
    assert L.shrink_asked("keep it short") is True
    assert L.shrink_asked("a big report, not a summary") is False
    assert L.shrink_asked("a short report of at least 6 pages") is False, "a number is a size the person chose"
