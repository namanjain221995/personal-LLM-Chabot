# -*- coding: utf-8 -*-
"""RAISE 2 — the Hindi / Gujarati / Hinglish / Gujlish half of the
understanding layer.

Every case here was WRONG on the 120-case multilingual corpus
(scratchpad/understanding/measure2/run_lang.py, measured 2026-09-16 at
105/120) and is right after the lexicon fixes. The corpus id of each case is
in its assertion so a regression points back at the measurement.

English must not move, and did not: every non-Indic string of the English
corpora (313 of them) was run through normalize, formats_in, explicit_formats,
language_of, chart_signal, style_phrases, undo_signal, request_marker,
reads_source, file_signal and negative_shape before and after — the only three
that changed are Hinglish ("isko pie chart me dikhao", "font thoda bada karo",
"ise thoda chhota karo"). The 188-turn follow-up corpus (measure3) is
byte-identical, and the 150-case English corpus (measure1) moved exactly one
case, its one Hinglish case C12. The pins below hold the English shapes these
same fixes touch.
"""
from __future__ import annotations

import pytest

from app.artifacts import formats as F, intent as I, lexicon as LX

FILECARD = dict(has_artifacts=True, has_assistant_answer=True, last_turn_is_artifact=True)
ANSWERED = dict(has_artifacts=False, has_assistant_answer=True, last_turn_is_artifact=False)
CREATE = dict(has_artifacts=False, has_assistant_answer=False, last_turn_is_artifact=False)


# ------------------------------------------------ "show this as a chart" --

@pytest.mark.parametrize("case,text", [
    ("hi-chart-4", "इस डेटा को पाई चार्ट पर दिखाओ"),
    ("gu-chart-4", "આ ડેટા પાઇ ચાર્ટમાં બતાવો"),
    ("hn-chart-4", "is data ko pie chart me dikhao"),
    ("gj-chart-4", "aa data ne pie chart ma batavo"),
])
def test_show_this_as_a_chart_is_a_chart_request(case, text):
    """4/4 languages answered "show this as a pie chart" in chat: the SHOW
    verb (दिखाओ / બતાવો / dikhao / batavo) had no entry in the normaliser."""
    got = I.decide(text, **ANSWERED)
    assert got.action == "create", f"{case}: rule={got.rule} norm={LX.normalize(text)!r}"
    assert got.chart_request is True, case


def test_show_after_a_chart_word_is_a_hand_over():
    for text in ("isko pie chart me dikhao", "aa data ne pie chart ma batavo",
                 "इसे पाई चार्ट में दिखा दो", "इस टेबल को बार चार्ट में दिखाइए"):
        assert "_give_" in LX.normalize(text), text
    # "batao"/"kaho" (tell me) were already reads and stay reads.
    assert "_read_" in LX.normalize("is file ka summary batao")
    assert "_read_" in LX.normalize("aa file no saransh kaho")


@pytest.mark.parametrize("text", [
    "mujhe ye pdf dikhao",
    "इस पीडीएफ का पहला पेज दिखाओ",
])
def test_show_on_an_uploaded_source_is_a_read(text):
    """SHOW is a hand-over only where there is something to hand over. On an
    attachment it asks ABOUT the file: exporting the previous ANSWER as a PDF
    is not what "show me this pdf" asked for."""
    assert "_read_" in LX.normalize(text), LX.normalize(text)
    got = I.decide(text, upload_formats=("pdf",), has_assistant_answer=True)
    assert got.action == "none", f"rule={got.rule}"
    assert got.rule == "negative:read_source", got.rule


# ------------------------------------- "convert it to Word" said SOV-first --

def test_word_before_a_postposition_names_docx():
    """The normaliser emits the destination BEFORE the postposition, so the
    English-only alias table ("in word", "word file") never saw it."""
    assert F.explicit_formats("_this_ word _in_ _convert_") == ["docx"]
    assert F.explicit_formats("_this_ word _in_ _give_") == ["docx"]
    assert LX.formats_in("_this_ word _in_ _convert_") == ["docx"]


def test_a_bare_word_postposition_is_still_not_a_format():
    """The postposition rule rewrites the ENGLISH "the word me" to "word _in_"
    as well, so the alias needs the hand-over token the Indic input carries;
    without it these three named docx while the fix was being written."""
    for text in ("what does the word me mean in english",
                 "the word me is a pronoun",
                 "spell the word ma for me"):
        assert F.explicit_formats(text) == [], text
        assert LX.formats_in(LX.normalize(text)) == [], text


@pytest.mark.parametrize("case,text", [
    ("hi-conv-2", "इसको वर्ड में कन्वर्ट कर दो"),
    ("gu-conv-2", "આને વર્ડમાં કન્વર્ટ કરો"),
    ("hn-conv-2", "ise word me badal do"),
    ("gj-conv-2", "aane word ma convert karo"),
])
def test_convert_this_to_word_converts_the_file(case, text):
    got = I.decide(text, **FILECARD)
    assert got.action == "convert", f"{case}: rule={got.rule} norm={LX.normalize(text)!r}"
    assert got.formats == ["docx"], case


# ------------------------------------------- "tayyar karo" / "taiyar karo" --

@pytest.mark.parametrize("case,text", [
    ("hn-make-4", "hamare project par presentation tayyar karo"),
    ("gj-make-4", "amara project par presentation taiyar karo"),
])
def test_romanised_tayyar_karo_is_a_request(case, text):
    """The SCRIPT forms तैयार करो / તૈયાર કરો were mapped; the romanised ones
    were not, so a deck request read as no-request."""
    got = I.decide(text, **CREATE)
    assert got.action == "create", f"{case}: rule={got.rule} norm={LX.normalize(text)!r}"


# ------------------------------------------- romanised Gujarati undo ------

def test_romanised_gujarati_undo_is_an_edit():
    """gj-edit-5: only the Gujarati SCRIPT undo and the Hinglish romanised one
    existed; "chhello ferfar kadhi nakho" degraded to a bare "remove"."""
    assert LX.undo_signal("chhello ferfar kadhi nakho") is True
    got = I.decide("chhello ferfar kadhi nakho", **FILECARD)
    assert got.action == "edit", f"rule={got.rule} norm={LX.normalize('chhello ferfar kadhi nakho')!r}"


# ------------------------------------------------------------- language --

def test_a_shared_marker_no_longer_decides_the_language():
    """'karo' is in BOTH marker lists, and the tie went to Gujlish, so 2/30
    Hinglish cases were labelled Gujlish (measure2, 2026-09-16)."""
    assert LX.language_of("title bold karo") == "hinglish"          # hn-edit-2
    assert LX.language_of("aa answer ne pdf ma aapo") == "gujlish"  # unchanged


@pytest.mark.parametrize("case,text,lang", [
    ("hn-chart-4", "is data ko pie chart me dikhao", "hinglish"),
    ("hn-data-5", "is data me sabse zyada kisne becha", "hinglish"),
    ("hn-chat-5", "python me code likho jo document banaye", "hinglish"),
    ("hn-make-4", "hamare project par presentation tayyar karo", "hinglish"),
    ("gj-data-3", "aa file no saransh kaho", "gujlish"),
    ("gj-edit-5", "chhello ferfar kadhi nakho", "gujlish"),
    ("gj-make-4", "amara project par presentation taiyar karo", "gujlish"),
])
def test_content_words_carry_the_language(case, text, lang):
    """Both marker lists were function words only, so a five- to seven-word
    request could score 0 or 1 and be answered in English."""
    assert LX.language_of(text) == lang, case


# ---------------------------------------------------- English is untouched --

ENGLISH = [
    "give it in docs",
    "the word count of this pdf",
    "in a word, no",
    "make a pdf on the employee training plan",
    "summarize this pdf",
    "make the headings dark blue",
    "convert it to a word document",
    "i need a word file of the audit",
    "how do i convert word to pdf",
    "no more edits please",
    "chart this",
    "put the totals in a new column",
    "add a heading to the report",
    "can you make this landscape",
    "sort the rows by value",
]


@pytest.mark.parametrize("text", ENGLISH)
def test_english_language_is_still_english(text):
    assert LX.language_of(text) == "en"


def test_english_normalisation_is_unchanged():
    assert LX.normalize("give it in docs") == "give it in docx"
    assert LX.normalize("convert it to a word document") == "convert it to a word document"
    assert F.explicit_formats("convert it to a word document") == ["docx"]
    assert F.explicit_formats("the word count of this pdf") == ["pdf"]
    assert F.explicit_formats("in a word, no") == []


def test_a_known_english_false_positive_is_unchanged():
    """"show me the chart you made" has scored Hinglish since the one-marker
    rule was written ("me", 6 words). This patch does not move it either way:
    pinned so a later change to the marker lists has to be deliberate."""
    assert LX.language_of("show me the chart you made") == "hinglish"
