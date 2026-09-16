"""The English requests MEASURE 1 found the understanding layer misreading
(2026-09-16, harness accuracy 127/137 before these fixes).

Every case here is one a careful human would call a plain misreading — the
words say what they want, and the rules in app/artifacts/intent.py and
app/artifacts/formats.py answered with something else. Each test names the
measured symptom, and each fix carries a companion NEGATIVE so the widening
cannot be paid for with a false positive.

Offline: no model, no database — these are the rules only.
"""
from __future__ import annotations

import pytest

from app.artifacts import formats as F, intent as I


CARD = dict(has_artifacts=True, has_assistant_answer=True, last_turn_is_artifact=True,
            artifact_hints=["Q3 Vendor Risk Review"])
FILE_EARLIER = dict(has_artifacts=True, has_assistant_answer=True, last_turn_is_artifact=False,
                    artifact_hints=["Q3 Vendor Risk Review"])
FRESH = dict(has_artifacts=False, has_assistant_answer=False, last_turn_is_artifact=False)


# ------------------------------------------------- a remark is not an edit --

@pytest.mark.parametrize("text", [
    # MEASURE 1 A26: read as rule="edit" and the artifact was silently
    # re-rendered, because `cut` is an edit verb and `the docx` a reference.
    "i opened the docx on my phone and the table is cut off",
    "i read the report on the train and the fonts are tiny",
    "we use the same deck with every client",
])
def test_a_remark_about_the_file_is_not_an_instruction(text):
    assert I.decide(text, **CARD).action == "none"


@pytest.mark.parametrize("text", [
    # The same words as an instruction still edit: the guard reads the
    # first-person REPORTING shape ("i opened…"), not the vocabulary, and a
    # request marker anywhere in the sentence takes the remark back.
    "the table is cut off, fix it",
    "cut the last section from the docx",
    "i opened the docx on my phone and the table is cut off, can you fix it?",
    "i opened the docx and the table is cut off — please fix the layout",
])
def test_an_instruction_about_the_same_file_still_edits(text):
    assert I.decide(text, **CARD).action == "edit"


# --------------------------------------------- the kind reads the same text --

def test_a_typo_in_the_deck_word_still_asks_for_a_deck():
    # MEASURE 1 B18: "need a presentaion on cyber security" came back as a
    # Word file and a PDF, although the gate had read the deck correctly —
    # decide_base normalised the text for the FORMATS and handed kind_for the
    # raw string.
    d = F.decide("need a presentaion on cyber security")
    assert d.kind == "presentation"
    assert d.formats == ["pptx", "pdf"]


def test_the_kind_is_read_through_the_normaliser_in_every_language():
    # MEASURE 1 B24, the same defect: lexicon.normalize already maps
    # प्रेजेंटेशन -> presentation, and now the kind rules see it.
    d = F.decide("एक प्रेजेंटेशन बनाओ मार्केटिंग प्लान पर")
    assert d.kind == "presentation"
    assert d.formats == ["pptx", "pdf"]


def test_a_document_about_a_presentation_is_still_a_document():
    d = F.decide("write a report on our cyber security posture")
    assert d.kind == "document"
    assert d.formats == ["docx", "pdf"]


# ------------------------------------------------------ counted rows are data --

def test_counted_rows_are_a_data_file_not_a_word_document():
    # MEASURE 1 B29: "generate 250 realistic sample rows of support tickets"
    # was delivered as a Word file and a PDF — _KIND_RULES knew `sample data`
    # and `data set` but not `N rows`, so the kind defaulted to document
    # before _DATASET was ever consulted.
    d = F.decide("generate 250 realistic sample rows of support tickets")
    assert d.kind == "workbook"
    assert d.formats == ["csv"]


@pytest.mark.parametrize("text", [
    "write a report on the 250 rows of tickets we logged",
    "make a one-pager summarising 40 entries from the audit",
])
def test_prose_about_counted_rows_is_still_prose(text):
    assert F.decide(text).kind == "document"


# ------------------------------------- the gate knows the nouns the policy knows --

def test_a_calculator_is_a_request_the_format_policy_can_fulfil():
    # MEASURE 1 B33: the gate refused "create a budget calculator" as
    # rule="no-request" while formats.kind_for read it as
    # ("workbook", "spreadsheet words") — the two vocabularies had drifted.
    it = I.decide("create a budget calculator", **FRESH)
    assert it.action == "create"
    d = F.decide(it.instruction or "create a budget calculator")
    assert d.kind == "workbook"
    assert d.formats == ["xlsx"]


@pytest.mark.parametrize("text", [
    "build me a financial model for the next three years",
    "i need a budget tracker for the team",
])
def test_the_other_spreadsheet_nouns_are_requests_too(text):
    assert I.decide(text, **FRESH).action == "create"


@pytest.mark.parametrize("text", [
    # A bare `budget` is a topic, not a file: these are questions for the
    # dataset engine and must not mint a spreadsheet.
    "give me the budget for q3",
    "what is our marketing budget this year",
])
def test_a_bare_budget_is_a_topic_not_a_file(text):
    assert I.decide(text, has_artifacts=False, has_assistant_answer=False,
                    last_turn_is_artifact=False, upload_formats=("xlsx",)).action == "none"


# -------------------------------------------- an instruction said as a need --

@pytest.mark.parametrize("text", [
    # MEASURE 1 E20: the instruction was dropped (rule="no-request") because
    # a request said as a NEED is in no edit-verb list.
    "the deck needs our logo on every slide",
    "the report is missing the summary section",
    "the document should have a table of contents",
])
def test_a_request_said_as_a_need_is_an_edit(text):
    assert I.decide(text, **CARD).action == "edit"


@pytest.mark.parametrize("text", [
    # A NEED about the world, not about the file's content: an infinitive
    # complement ("needs to go out") and a past one ("should have been sent")
    # are remarks, and the harness has them as chat.
    "the report needs to go out by friday",
    "the deck should have been sent to the client last week",
])
def test_a_need_about_the_world_is_not_an_edit(text):
    assert I.decide(text, **CARD).action == "none"
