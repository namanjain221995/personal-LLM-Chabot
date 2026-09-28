"""A mistyped "world" is not a request for a Word document.

THE OWNER'S OWN MESSAGE, 2026-09-28, verbatim:

    Best Ai model ?? in word n 2026 ??

"world" mistyped as "word", "in" mistyped as "n". The product created a
Word document called "AI Model Landscape Assessment" and offered it for
download, instead of answering a question about the state of the field.
Two dropped letters turned a question into a file.

`formats._ALIAS["docx"]` carried a bare `in word|as word|to word`, so any
sentence containing those two words named the format. A year or any number
after it is the tell: nobody writes "in word 2026" meaning the format, and
everybody who does mean the format has a way to say it that still matches
— "in word format", "as a word document", "convert this to word".

There are TWO alias tables — `lexicon.FORMAT_ALIASES` and this one — and
only `formats._ALIAS` decides `explicit_formats`. Both are narrowed here;
the lexicon's copy is what `lexicon.formats_in` reads.
"""
from __future__ import annotations

import pytest

from app.artifacts import formats as FM
from app.artifacts import intent as I

OWNER = "Best Ai model ?? in word n 2026 ??"


def test_the_owners_message_asks_a_question_and_makes_nothing():
    assert FM.explicit_formats(OWNER) == [], "the typo still names the Word format"
    intent = I.decide(OWNER)
    assert intent.action == "none", (intent.action, intent.rule)
    assert intent.wants_file is False


@pytest.mark.parametrize("text", [
    "best ai model in word 2026",
    "what is the best ai model in word 2026",
    "Best Ai model ?? in word n 2026 ??",
    "who is the fastest in word 2025",
    "the biggest company in word 2030",
])
def test_a_number_after_word_means_world(text):
    assert FM.explicit_formats(text) == [], text


@pytest.mark.parametrize("text,fmt", [
    ("give me this in word", "docx"),
    ("put it in word format", "docx"),
    ("as a word document please", "docx"),
    ("convert this to word", "docx"),
    ("send it as word", "docx"),
    ("make a word doc about the best ai model", "docx"),
    ("i need a word file", "docx"),
    ("ms word please", "docx"),
])
def test_every_way_of_actually_asking_for_word_still_works(text, fmt):
    assert fmt in FM.explicit_formats(text), text


def test_the_format_survives_beside_a_year_when_it_is_said_properly():
    """The narrowing must not cost someone who wants Word AND names a year —
    they have three spellings that still match."""
    for text in ("in word format, the 2026 releases",
                 "a word document about 2026",
                 "convert the 2026 report to word format"):
        assert "docx" in FM.explicit_formats(text), text


def test_both_alias_tables_agree_about_the_owners_message():
    """`lexicon.FORMAT_ALIASES` and `formats._ALIAS` are separate tables and
    only the second decides `explicit_formats`; a fix applied to one and not
    the other looks right in a unit test and ships the bug."""
    from app.artifacts import lexicon as LX

    assert FM.explicit_formats(OWNER) == []
    assert LX.formats_in(OWNER) == []
