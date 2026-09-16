"""Follow-ups, the object a turn refers to, and the honesty of a refusal.

THE COMPLAINT (owner, 2026-09-16): "our ai can't understand what user want".
Three measured corpora stand behind the cases below; every one of them was
WRONG before the change it is written against.

  * the follow-up corpus (188 turns of 62 conversations): 141 right before,
    177 after. "Do the same for headcount", "same but shorter", "do it
    again", "make it two pages", "sort it by value", "and the report longer"
    and "mention the SLA in the intro" were all answered as chat, because
    nothing in the layer resolved "the same" and `make` was an edit verb
    only in front of a closed adjective list. "Now as a docx", "all of that
    as a briefing doc", "both of those as one pdf", "download this table"
    and "put the revenue table in excel" made a new file out of the words,
    or nothing at all, instead of handing over the answer they pointed at.

  * the refusal corpus (66 turns): 18 right before. A conversion to a format
    this platform does not make produced a SECOND artifact under the same
    title; LaTeX, JSON and EPUB vanished from a request without a word; a
    presentation asked for "as an xlsx" came back as a spreadsheet with no
    sentence about the swap; four "column not found" warnings were printed
    as two; an unreadable attachment was composed around in silence; and an
    edit that changed nothing said "Updated".

Rules only — no model, no database: `intent.decide` and `formats.decide` are
what fast_lane.py and main.py run on the event loop.
"""
from __future__ import annotations

import pytest

from app.artifacts import formats as F
from app.artifacts import intent as I
from app.artifacts import requirements as R
from app.engines import artifact as E
from app.engines import capability as CAP
from app.engines import sql as SQL

#: The turn CONTEXT main.py:5439-5489 assembles, in the three shapes these
#: cases arrive in.
CARD = dict(has_artifacts=True, last_turn_is_artifact=True)
CARD_AFTER_ANSWER = dict(has_artifacts=True, last_turn_is_artifact=True, has_assistant_answer=True)
ANSWER = dict(has_assistant_answer=True)
TWO_FILES = dict(has_artifacts=True, last_turn_is_artifact=True, artifact_hints=["Leave policy", "Hiring plan"])


# ------------------------------------------- the file that was just made --


@pytest.mark.parametrize("text,ctx", [
    # "the same" resolved to nothing anywhere in the layer before 2026-09-16.
    ("do the same for headcount", CARD),
    ("and the same again but as a line", CARD),
    ("same but shorter", CARD_AFTER_ANSWER),
    ("now do the same on map", CARD_AFTER_ANSWER),
    ("do it again", CARD),
    ("try again with a cleaner layout", CARD),
    ("graph this as bars instead", CARD_AFTER_ANSWER),
    ("and for last year too", CARD_AFTER_ANSWER),
    # `make` with a pronoun object and any complement.
    ("make it two pages", CARD_AFTER_ANSWER),
    ("make it a pie", CARD_AFTER_ANSWER),
    ("make it five slides", CARD_AFTER_ANSWER),
    # A verb the element rule knew and the main edit rule did not.
    ("sort it by value", CARD_AFTER_ANSWER),
    ("mention the SLA in the intro", CARD),
    # No verb at all.
    ("and the report longer", CARD),
    ("a bit more spacing", CARD),
    # A request said as a NEED.
    ("the deck needs our logo on every slide", CARD),
])
def test_a_followup_on_the_latest_file_is_an_edit_of_it(text, ctx):
    intent = I.decide(text, **ctx)
    assert intent.action == "edit", (text, intent.rule, intent.action)
    assert intent.target == "artifact"


def test_one_more_like_that_is_a_new_file_not_an_edit():
    """An anaphor that asks for an ADDITIONAL file keeps its own rule: the
    edit rules must not swallow it (S27 of the corpus)."""
    intent = I.decide("one more like that but for onboarding", **CARD)
    assert intent.action == "create" and intent.rule == "another-like"
    assert intent.new_artifact is True


def test_an_anaphor_needs_a_file_to_point_at():
    """With no artifact in the conversation the same words are chat."""
    assert I.decide("do the same for headcount").action == "none"
    assert I.decide("same but shorter", has_assistant_answer=True).action == "none"


def test_a_question_about_what_was_done_is_not_an_anaphor_edit():
    assert I.decide("did you do the same for q2?", **CARD).action == "none"
    assert I.decide("can you explain that again", **CARD).action == "none"


# --------------------------------------------- which file, and what for --


def test_an_edit_that_names_a_file_by_title_beats_the_newest_one():
    """"not that one, change the leave policy pdf" converted the HIRING PLAN
    deck: the convert-artifact-turn branch hard-codes reference="latest",
    and engines/artifact.pick_artifact honours a hint only when the
    reference is not "latest"."""
    intent = I.decide("not that one, change the leave policy pdf", **TWO_FILES)
    assert intent.action == "edit"
    assert intent.reference == "named" and intent.reference_hint == "Leave policy"


def test_making_the_deck_shorter_is_an_edit_not_a_re_render():
    """`deck` names the pptx format in a format position ("a deck version"),
    so an edit that names the file must not be read as a conversion."""
    intent = I.decide("make the deck shorter", has_artifacts=True, last_turn_is_artifact=True,
                      artifact_hints=["Q3 deck"])
    assert intent.action == "edit", intent.rule


def test_a_deck_version_is_a_conversion_of_the_file():
    """intent.py's own vocabulary held `deck`; formats._ALIAS did not, so
    the convert rule found no explicit format and the turn was chat."""
    intent = I.decide("and a deck version", **CARD)
    assert intent.action == "convert" and intent.formats == ["pptx"]


@pytest.mark.parametrize("text,expected", [
    ("and a deck version", ["pptx"]),
    ("make it as a deck", ["pptx"]),
    # ...but a deck named as the SOURCE, or as the thing itself, is not a
    # target: these three were right before the alias grew and must stay so.
    ("Give me a Word version of the deck.", ["docx"]),
    ("I need a deck for Monday's board meeting.", []),
    ("Create a deck about X and add the logo", []),
])
def test_the_deck_alias_only_reads_a_format_position(text, expected):
    assert F.explicit_formats(text) == expected


def test_a_remark_about_the_file_is_not_an_instruction():
    """`cut` is an edit verb and `the docx` a reference, so a complaint
    silently re-rendered the artifact (intent.py's `_STATEMENT_RE` already
    knew this shape and was read only by the create rules)."""
    for text in ("i opened the docx on my phone and the table is cut off",
                 "I opened the pdf on my phone and the table is cut off"):
        intent = I.decide(text, **CARD)
        assert intent.action == "none", (text, intent.rule)


# --------------------------------- handing the previous ANSWER over as a file --


@pytest.mark.parametrize("text,formats", [
    # A destination, a named format and no verb at all.
    ("now as a docx", ["docx"]),
    ("now as a pdf", ["pdf"]),
    # An elliptical hand-over whose adjective the closed list did not know.
    ("all of that as a briefing doc", ["docx"]),
    ("both of those as one pdf", ["pdf"]),
    # A keeping verb on a bare reference, format left to the policy.
    ("download this table", []),
    # One content word inside the reference.
    ("put the revenue table in excel", ["xlsx"]),
    ("pdf of the second summary", ["pdf"]),
])
def test_a_followup_that_points_at_the_answer_exports_it(text, formats):
    intent = I.decide(text, **ANSWER)
    assert intent.action == "export", (text, intent.rule)
    assert intent.reference == "previous_answer" and intent.target == "previous_answer"
    assert intent.formats == formats


def test_the_original_answer_beats_the_file_card():
    """"also give me the original answer as a word file" after a card
    converted the PDF: `original` was not in the answer vocabulary."""
    intent = I.decide("also give me the original answer as a word file", **CARD_AFTER_ANSWER)
    assert intent.action == "export" and intent.reference == "previous_answer"
    assert intent.formats == ["docx"]


def test_a_statement_about_a_file_is_not_an_export():
    """The elliptical rule must not read a description as a hand-over."""
    assert I.decide("this data is in excel", **ANSWER).action == "none"
    assert I.decide("I have this in Excel", **ANSWER).action == "none"
    assert I.decide("the numbers in the report are wrong", **CARD).action == "none"


def test_in_the_report_is_still_a_place():
    """The widened `as/in a <word> <format>` gap excludes determiners, so
    the 2026-09-15 rule survives."""
    assert not I._AS_FORMAT_RE.search("the numbers in the report are wrong")
    assert I._AS_FORMAT_RE.search("all of that as a briefing docx")
    assert I._AS_FORMAT_RE.search("both of those as one pdf")


def test_a_chart_word_used_as_a_verb_is_a_chart_request():
    """"chart it" made no file at all: the chart word switched the export
    path off and then failed the chart gate."""
    intent = I.decide("chart it", **ANSWER)
    assert intent.action == "create" and intent.chart_request is True
    assert intent.target == "previous_answer", "the table it points at is in the answer"


def test_show_that_as_a_line_chart_is_a_chart_request():
    intent = I.decide("show that as a line chart", **ANSWER)
    assert intent.action == "create" and intent.chart_request is True


# ------------------------------------------- formats: what is NOT made --


@pytest.mark.parametrize("text,name", [
    ("Give me the audit as an xlsx, a csv, a Word file and a LaTeX file.", "LaTeX"),
    ("Export this conversation as JSON.", "JSON"),
    ("Give me the proposal as an .epub.", "EPUB"),
    ("Convert it to a .txt file.", "plain text"),
    ("Make an editable Figma file of this layout.", "Figma"),
])
def test_a_format_we_do_not_make_is_named_in_the_warnings(text, name):
    decision = F.decide(text)
    assert any(name in w for w in decision.warnings), (text, decision.warnings)


def test_the_files_that_were_asked_for_are_still_made():
    decision = F.decide("Give me the audit as an xlsx, a csv, a Word file and a LaTeX file.")
    assert decision.formats == ["xlsx", "csv", "docx"]


def test_google_slides_does_not_name_a_powerpoint():
    """`Slides` inside "Google Slides" matched the pptx alias and produced a
    second deck."""
    assert F.explicit_formats("Convert it to a Google Slides file.") == []
    assert F.decide("Convert it to a Google Slides file.").kind == "document"


def test_a_kind_swapped_by_a_format_word_is_said_out_loud():
    """kind_for lets the first explicit format pick the kind, so "make a
    presentation as an xlsx" returned a workbook with no warning at all."""
    decision = F.decide("Make a presentation as an xlsx.")
    assert decision.kind == "workbook"
    assert any("presentation cannot be" in w for w in decision.warnings), decision.warnings


def test_an_ordinary_mixed_list_is_not_a_kind_swap():
    """"A PDF report plus an Excel with the data" names a document and drops
    the xlsx with its own warning — the swap rule must stay quiet."""
    decision = F.decide("Make a PDF report on AI, plus an Excel with the data.")
    assert not any("cannot be a" in w for w in decision.warnings), decision.warnings


@pytest.mark.parametrize("text", [
    "make a pdf of the travel policy",
    "Create a professional PDF report on the audit.",
    "give me a csv of 500 sample customers",
    "summarise it as text",
])
def test_a_request_we_can_fulfil_carries_no_refusal(text):
    assert F.decide(text).warnings == []


# ---------------------------------- the engine's sentences and refusals --


def test_the_conversion_offer_reads_like_a_sentence():
    """"PowerPoint or PDF or png" — three defects: the raw id, the repeated
    "or", and an image format offered for a document conversion."""
    assert E._conversion_offer("presentation") == "PowerPoint or PDF"
    assert E._conversion_offer("document") == "Word or PDF"
    assert E._conversion_offer("workbook") == "Excel, CSV, Word or PDF"


def test_a_conversion_to_a_format_we_do_not_make_is_refused():
    intent = I.ArtifactIntent("create", formats=[])
    deck = [{"id": "a1", "kind": "presentation", "title": "Pricing Update"}]
    line = E._refuse_unmakeable_conversion("Convert it to a .txt file.", intent, deck)
    assert line.startswith("I don't make plain text files")
    assert "PowerPoint or PDF" in line


def test_a_conversion_to_an_image_the_kind_cannot_hold_is_refused():
    intent = I.ArtifactIntent("create", formats=[])
    deck = [{"id": "a1", "kind": "presentation", "title": "Pricing Update"}]
    line = E._refuse_unmakeable_conversion("Convert it to SVG.", intent, deck)
    assert "cannot be converted to SVG image" in line and "PowerPoint or PDF" in line


@pytest.mark.parametrize("instruction", [
    "Make a PDF about the pricing change.",          # not a conversion
    "Convert it to Excel.",                          # a format we DO make
])
def test_a_normal_turn_is_not_refused(instruction):
    intent = I.ArtifactIntent("create", formats=["xlsx"] if "Excel" in instruction else [])
    deck = [{"id": "a1", "kind": "presentation", "title": "Pricing Update"}]
    assert E._refuse_unmakeable_conversion(instruction, intent, deck) == ""


def test_more_than_two_warnings_are_counted_not_truncated():
    """Four "column not found" warnings were printed as two, and the person
    read two thirds of the truth."""
    clause = E._warning_clause(["The column 'Profit' was not found",
                                "The column 'Margin' was not found",
                                "The column 'Cost' was not found",
                                "The column 'Headcount' was not found"])
    assert "Profit" in clause and "Margin" in clause
    assert "and 2 more" in clause and "see the card" in clause


def test_two_warnings_are_still_said_in_full():
    clause = E._warning_clause(["one thing", "another thing"])
    assert clause == " _one thing._ _another thing._"


def test_an_unreadable_attachment_is_named_in_the_sentence():
    """material_in wrote the note; nothing read it, so the turn ended on a
    plain "Created …" and the person believed their file was used."""
    clause = E._unreadable_clause("resume.pages is not a readable document or table.")
    assert clause.startswith("I couldn't read resume.pages")
    assert "PDF, Word, Excel, CSV, Markdown and plain text" in clause
    assert E._unreadable_clause("sheet 'Audit': 3 rows were typed by the model") == ""


# ------------------------------------- what the platform cannot do at all --


@pytest.mark.parametrize("text,fragment", [
    ("Make a fillable PDF form for the onboarding checklist.", "fillable"),
    ("Build an interactive dashboard I can filter, as an Excel file.", "interactive"),
    ("Give me the contract with tracked changes turned on.", "tracked changes"),
    ("Make the proposal with our company letterhead image.", "image, logo or letterhead"),
    ("Make a PDF and print two copies.", "can't print"),
    ("Email this report to the leadership team as a PDF.", "email or post"),
    ("Password-protect the Excel file.", "password-protect"),
    ("Embed a live Salesforce dashboard in it.", "embed anything live"),
    ("Add a video of the demo to page 2.", "video or audio"),
])
def test_the_impossible_half_of_a_request_is_said(text, fragment):
    clauses = R.unsupported_asks(text)
    assert any(fragment in c for c in clauses), (text, clauses)


@pytest.mark.parametrize("text", [
    "a report on the video pipeline",
    "a report on our logo redesign",
    "a report on our live data pipeline",
    "write a report on how we send data to vendors",
    "no need to password-protect it, just make the xlsx",
    "make a pdf of the travel policy",
    "Make a bar chart of revenue by region as a PDF.",
])
def test_a_topic_is_not_a_request_to_do_the_impossible(text):
    assert R.unsupported_asks(text) == []


def test_an_unsupported_change_to_a_file_is_an_edit_of_it():
    """"Sign it digitally and password-protect the PDF" and "Embed a live
    Salesforce dashboard in it" minted a SECOND artifact, so even the edit
    path's "Not applied: …" channel was never reached."""
    for text in ("Sign it digitally and password-protect the PDF.",
                 "Embed a live Salesforce dashboard in it.",
                 "Add a video of the demo to page 2."):
        intent = I.decide(text, **CARD)
        assert intent.action == "edit", (text, intent.rule)


# ------------------------------------------------ promises and zero rows --


@pytest.mark.parametrize("answer", [
    "I've emailed the report to the team.",
    "I have posted the deck to your Slack channel.",
    "The PDF has been password-protected.",
    "I will email it to them once you confirm.",
])
def test_a_delivery_the_platform_cannot_perform_is_caught(answer):
    assert CAP.promise_in(answer) is True


@pytest.mark.parametrize("answer", [
    "You can email the PDF to your team.",
    "I sent you the numbers above.",
    "Created **Pricing Update** as PDF.",
    "Here is the summary you asked for.",
])
def test_an_ordinary_answer_is_not_a_false_promise(answer):
    assert CAP.promise_in(answer) is False


def test_the_capability_line_says_what_is_not_delivered():
    """It told the model what the platform can MAKE and nothing about
    delivery, so "I've emailed it" passed every check."""
    line = CAP.CAPABILITY_LINE.lower()
    for word in ("email", "slack", "print", "encrypt", "sign"):
        assert word in line, word


def test_a_zero_row_result_carries_its_caveat_from_the_engine():
    """The authoritative block asks the MODEL to say what zero rows mean;
    the incident it documents is the failure of trusting that relay."""
    empty = SQL.deterministic_summary(["Name"], [])
    assert empty.get("empty_result") is True
    prefix = SQL.zero_rows_prefix(empty)
    assert prefix.startswith("The query ran and matched no rows")
    assert "not evidence the records do not exist" in prefix
    assert SQL.zero_rows_prefix(SQL.deterministic_summary(["Name"], [["a"]])) == ""
