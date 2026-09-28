# -*- coding: utf-8 -*-
"""The product understands what the person typed: the classes the
understanding benchmark (1,553 turns, 2026-09-28) ranked worst, each fixed by
a rule and each pinned here so it cannot come back by accident.

THE PRINCIPLE, because it is why these kept recurring: THE WORD IS NOT THE
MEANING. "pie" after "as a" over an uploaded sheet is a chart; "pie" in a
recipe is not. "no i meant pdf" after our file card is a conversion; "no,
the chart is fine" after the same card is nothing. Every rule below decides
from the SHAPE of the request -- what it asks for and where it is said --
and lets the word be evidence.

Every case here was measured on the release ae25da28 (the shared production
checkout, byte-identical to the running container) as the WRONG decision
named in its class comment, and is decided by the rules alone: no model, no
database, no network. On Fast the classifier has ~2.5 s and falls back to
these rules in silence, so a turn understood only through the model is not
understood.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.artifacts import intent as I
from app.artifacts import lexicon as LX

#: The contexts of the understanding corpus, by the names it uses.
P0 = dict()
PA = dict(has_assistant_answer=True)
PC = dict(has_artifacts=True, last_turn_is_artifact=True,
          artifact_hints=("TechSara AI Engineering Workflow Tracker",))
PCC = dict(PC, has_assistant_answer=True,
           last_deliverable=SimpleNamespace(has_chart=True, kind="xlsx", formats=["xlsx"], chart_types=["bar"]))
T = dict(has_dataset=True, upload_formats=["xlsx"])


# ------------------------------------------------------------ #1 a chart named by its type --

@pytest.mark.parametrize("text", [
    "Headcount per department as a pie.",
    "Stacked bar of Q1 and Q2 sales by region.",
    "Average score by owner, bars please.",
    "Revenue per quarter as columns.",
    "Weekly order count as a line.",
    "Share of revenue by product as a donut.",
    "Funnel of the conversion stages.",
    "100% stacked column of product mix by region.",
    "Critical and high priority tickets by owner, stacked.",
    "Only open tickets: how many per priority? Column chart.",
    "Which product brings the most revenue? Chart it.",
    "Tickets per category, bar chart in dark blue.",
    # typed with the fingers slipping
    "pie chrat of staus",
    "bar grpah of revnue per regoin",
    "histogarm of hours",
    "bar chat of status counts",
    # Hindi, Gujarati, Hinglish
    "क्षेत्र के अनुसार बिक्री का बार चार्ट",
    "प्राथमिकता के अनुसार टिकटों की संख्या का चार्ट",
    "દર મહિનાના વેચાણનો લાઇન ગ્રાફ",
    "region wise sales ka bar chart, neele rang me",
    "Q1 aur Q2 ki region wise sales stacked bar me do",
])
def test_a_chart_type_over_the_uploaded_sheet_is_a_chart(text):
    """Was: none/no-request -- 38 of 140 chart asks made NOTHING, 26 of them
    not even offered to the classifier, because no rule read a chart TYPE
    with no request verb. Over a table, "as a pie" can only mean draw one."""
    intent = I.decide(text, **T)
    assert intent.action == "create", (text, intent.rule)
    assert intent.chart_request, (text, intent.rule)


@pytest.mark.parametrize("text, rule", [
    ("should I plot this?", "ambiguous"),          # a question about plotting
    ("what is a bar chart?", "negative:trivia"),   # the term
    ("add revenue as a column", "no-request"),     # a column of the TABLE
    ("Average order amount by region.", "no-request"),  # no chart word at all: left open
])
def test_a_type_word_outside_a_chart_slot_is_not_a_chart(text, rule):
    intent = I.decide(text, **T)
    assert intent.action == "none" and intent.rule == rule, (text, intent.action, intent.rule)


# ------------------------------------------------ #3 the answer handed over, in Hindi or Gujarati --

@pytest.mark.parametrize("text, fmt", [
    ("ફાઈલ PDF માં આપો", "pdf"),
    ("upar wala jawab pdf me de do", "pdf"),
    ("upar no jawab pdf ma aapo", "pdf"),
    ("Mujhe excel sheet chahiye.", "xlsx"),
    ("डॉक्स फाइल बना दो।", "docx"),
    ("Excel sheet bana ke de.", "xlsx"),
    ("file banavo pdf mate.", "pdf"),
    ("ફાઇલ બનાવો પીડીએફ માટે.", "pdf"),
    ("સીએસવી ફોર્મેટમાં બનાવો.", "csv"),
    ("aapjo ye docx ma save karo.", "docx"),
    ("હું પીડીએફ માંગું છું.", "pdf"),
    ("ફાઇલ નિર્યાત કરો.", ""),
])
def test_a_content_free_hand_over_after_an_answer_exports_it(text, fmt):
    """Was: create/create-postposition -- a NEW file of model-invented content,
    while the English twins ("give me the answer above as pdf", "I need an
    excel sheet.") were exported. The function words of Hindi, Gujarati and
    their romanisations counted as a TOPIC."""
    intent = I.decide(text, **PA)
    assert intent.action == "export", (text, intent.action, intent.rule)
    assert intent.target == "previous_answer"
    if fmt:
        assert fmt in intent.formats, (text, intent.formats)


def test_a_topic_after_an_answer_is_still_a_create():
    intent = I.decide("sales ki report banao", **PA)
    assert intent.action == "create", intent.rule


def test_indic_function_words_are_not_content():
    assert not I._content_words("mujhe excel sheet _give_ .")
    assert not I._content_words("file pdf માં _give_")
    assert not I._content_words("મને a document _give_ છે .")
    assert I._content_words("sales ki report _give_")
    assert I._content_words("छुट्टी नीति की pdf _give_")


def test_the_classifier_create_after_an_answer_is_a_hand_over():
    """The live classifier answered `create` for "Excel sheet bana ke de."
    and "PDF. PDF. PDF." after an answer; each became a fresh file."""
    rules = I.decide("Excel sheet bana ke de.", **PA)
    verdict = SimpleNamespace(action="create", formats=["xlsx"], target="conversation")
    out = I.verdict_to_intent(verdict, rules, has_artifacts=False, has_assistant_answer=True)
    assert out is not None and out.action == "export" and out.target == "previous_answer"
    # a topic stays a create
    rules = I.decide("sales ki report banao", **PA)
    out = I.verdict_to_intent(verdict, rules, has_artifacts=False, has_assistant_answer=True)
    assert out is not None and out.action == "create"


# --------------------------------------------------------- #4 the possible and the polite --

@pytest.mark.parametrize("text, ctx, action", [
    ("क्या इसकी पीडीएफ बन सकती है?", PA, "export"),
    ("શું આની પીડીએફ બની શકે?", PA, "export"),
    ("aa answer ni pdf bani shake?", PA, "export"),
    ("kya is answer ki pdf ban sakti hai?", PA, "export"),
    ("क्या आप छुट्टी नीति की पीडीएफ बना सकते हैं?", P0, "create"),
    ("શું તમે રજા નીતિની પીડીએફ બનાવી શકો?", P0, "create"),
    ("kya tum vendor payments ki sheet bana sakte ho?", P0, "create"),
    ("would you mind making a deck of this?", PC, "create"),
    ("do you mind making a deck?", PC, "create"),
    ("are you able to make a deck?", PC, "create"),
])
def test_the_possibility_form_is_a_request(text, ctx, action):
    """Was: none/no-request (none/about-format for "are you able to"). The
    give-verb table held imperatives only; "can this be made into a pdf?"
    was already a request in English."""
    intent = I.decide(text, **ctx)
    assert intent.action == action, (text, intent.action, intent.rule)


@pytest.mark.parametrize("text", [
    "can you make pdf files?",
    "can u make ppt's?",
    "kya tum pdf bana sakte ho?",
    "kya aap excel bhi bana sakte ho?",
    "क्या आप पीडीएफ बना सकते हैं?",
    "શું તમે પીડીએફ બનાવી શકો છો?",
])
def test_a_bare_format_capability_question_in_a_fresh_chat_is_answered(text):
    """A pdf of WHAT? With nothing in the room and no topic in the words,
    this asks what the product can do. Was: create -- a pdf/pptx of nothing."""
    intent = I.decide(text, **P0)
    assert intent.action == "none" and intent.rule == "about-format", (text, intent.action, intent.rule)


def test_the_same_capability_words_with_an_answer_in_the_room_are_a_request():
    intent = I.decide("can you make a pdf of this?", **PA)
    assert intent.action == "export", intent.rule
    intent = I.decide("do you have the bandwidth to make a pdf?", **P0)
    assert intent.action == "create", intent.rule


# ------------------------------------------------- #5 a question about OUR file, not in English --

@pytest.mark.parametrize("text", [
    "इस फ़ाइल में कौन-कौन से कॉलम हैं?",
    "इस फ़ाइल में कौन से कॉलम हैं?",
    "આ ફાઇલમાં કયા કૉલમ છે?",
    "isme kaun se column hai?",
    "wat colums does it hav ??",
])
def test_which_columns_is_a_question_about_the_file(text):
    """Was: none/no-request with answer_about_artifact False -- answered
    without reading the file -- while "what colums does it have ??" was
    read back. `_Q_WH_ANY` had no WHICH in Hindi or Gujarati, and the typo
    table had no `wat`."""
    intent = I.decide(text, **PC)
    assert intent.action == "none"
    assert intent.answer_about_artifact, (text, intent.rule)


def test_the_normaliser_reads_the_typos():
    assert LX.normalize("wat colums does it hav ??") == "what columns does it have ??"
    assert LX.normalize("pie chrat of staus") == "pie chart of staus"
    assert LX.normalize("bar chat of status counts") == "bar chart of status counts"
    assert LX.normalize("i sed pdf!! not docx!!!") == "i said pdf!! not docx!!!"


# -------------------------------------------------------- #6 a correction of the last file --

@pytest.mark.parametrize("text, ctx", [
    ("no, the deadline column is wrong, it should be end of month", PC),
    ("nahi, deadline column galat hai, month end hona chahiye", PC),
    ("no, the chart should be by region not by month", PCC),
    ("नहीं, चार्ट क्षेत्र के अनुसार होना चाहिए, महीने के अनुसार नहीं", PCC),
    ("nahi, chart region wise hona chahiye, month wise nahi", PCC),
    ("ના, ચાર્ટ પ્રદેશ પ્રમાણે હોવો જોઈએ, મહિના પ્રમાણે નહીં", PCC),
])
def test_a_negation_and_a_part_named_wrong_is_an_edit(text, ctx):
    """Was: none/no-request (English), or create/create-postposition -- a
    SECOND chart -- for the Hindi and Gujarati chart corrections."""
    intent = I.decide(text, **ctx)
    assert intent.action == "edit", (text, intent.action, intent.rule)
    assert intent.rule == "edit-correction"


@pytest.mark.parametrize("text, fmt", [
    ("no i ment pdf", "pdf"),
    ("I said pdf. PDF. not docx", "pdf"),
    ("i sed pdf!! not docx!!!", "pdf"),
    ("wrong format, I wanted a deck", "pptx"),
    ("maine excel bola tha, word nahi", "xlsx"),
    ("मैंने एक्सेल कहा था, वर्ड नहीं", "xlsx"),
    ("મેં એક્સેલ કહ્યું હતું, વર્ડ નહીં", "xlsx"),
    ("mein excel kidhu tu, word nai", "xlsx"),
    ("na, mane pdf joiti hati", "pdf"),
    ("ના, મારે પીડીએફ જોઈતી હતી", "pdf"),
    ("PDF. PDF. PDF.", "pdf"),
    ("yes go ahead with the pdf", "pdf"),
])
def test_a_correction_that_names_only_a_format_converts_to_it(text, fmt):
    """Was: none/no-request -- the person repeated the format, sometimes three
    times, and nothing happened. The format they REJECT ("not docx", "word
    nahi") is read off the words, so the one they want is the one left."""
    intent = I.decide(text, **PC)
    assert intent.action == "convert", (text, intent.action, intent.rule)
    assert intent.formats == [fmt], (text, intent.formats)


@pytest.mark.parametrize("text", [
    "no, the chart is fine",
    "no, the columns are correct, thanks",
])
def test_praise_after_a_negation_is_not_a_correction(text):
    intent = I.decide(text, **PC)
    assert intent.action == "none", (text, intent.action, intent.rule)


def test_a_correction_with_an_edit_asked_in_it_keeps_the_edit():
    intent = I.decide("not the pdf, the docx, make it shorter", **PC)
    assert intent.action == "edit", (intent.action, intent.rule)


# ---------------------------------------------------------------- #7 a refusal, in Gujarati --

@pytest.mark.parametrize("text", [
    "નવી ફાઇલ ન બનાવો, બસ અહીં કહો",
    "navi file nathi joiti, khali jawab aapo",
    "नई फ़ाइल मत बनाओ, बस यहीं बताओ",
    "नहीं, मैंने फ़ाइल नहीं माँगी थी, पूछा था इसमें क्या है",
])
def test_do_not_make_a_new_file_just_tell_me_is_heard(text):
    """Was: create/create-first-clause -- an explicit refusal BUILT a document,
    because the negation list had no bare Gujarati ન, no "nathi joiti" and no
    past "नहीं माँगी थी". This is "please tell me Only Not create" in Gujarati."""
    intent = I.decide(text, **PC)
    assert intent.action == "none", (text, intent.action, intent.rule)
    assert intent.answer_about_artifact, (text, intent.rule)


def test_the_normaliser_writes_the_negation_token():
    assert "_neg_" in LX.normalize("નવી ફાઇલ ન બનાવો")
    assert "_neg_" in LX.normalize("navi file nathi joiti")
    assert "_neg_" in LX.normalize("मैंने फ़ाइल नहीं माँगी थी")


# ------------------------------------------------- #8 a question about the file's FORMAT --

@pytest.mark.parametrize("text", [
    "is it in excel?",
    "is it excel?",
    "is this file an excel?",
])
def test_is_it_in_excel_is_a_question_not_a_conversion(text):
    """Was: convert/convert-artifact-turn ['xlsx'] -- an xlsx job on the
    tracker for a copula question. The format word is the SUBJECT of the
    question, never a destination."""
    intent = I.decide(text, **PC)
    assert intent.action == "none", (text, intent.action, intent.rule)
    assert intent.answer_about_artifact and intent.rule == "answer-artifact:format", (text, intent.rule)


def test_a_conversion_in_the_same_words_still_converts():
    intent = I.decide("give it in excel", **PC)
    assert intent.action == "convert", (intent.action, intent.rule)
