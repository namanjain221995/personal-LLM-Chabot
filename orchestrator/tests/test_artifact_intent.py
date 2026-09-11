"""The artifact-intent gate (app/artifacts/intent.py), example by example.

Every MUST / MUST NOT in the product brief is here as a test, plus the
follow-up shapes. Offline: no model is consulted by the rules.
"""
from __future__ import annotations

import asyncio

import pytest

from app.artifacts import intent as I


# ------------------------------------------------------------- must create --


@pytest.mark.parametrize("text,formats", [
    ("Create a PDF.", ["pdf"]),
    ("Create a professional PDF about this.", ["pdf"]),
    ("Make this into a Word document.", ["docx"]),
    ("Make a Word document from this conversation.", ["docx"]),
    ("Build a presentation.", []),
    ("Generate a PowerPoint presentation for the CEO.", ["pptx"]),
    ("Generate an Excel tracker.", ["xlsx"]),
    ("Turn this CSV into an Excel dashboard.", ["xlsx"]),
    ("Give me this as a document.", []),
    ("Give this to me as a document.", []),
    ("Prepare a CEO-ready one-pager.", []),
    ("Prepare a one-page executive brief.", []),
    ("Turn these meeting notes into a professional report.", []),
    ("Create the best deliverable for this.", []),
    ("Make all required files.", []),
    ("Create all the final deliverables.", []),
    ("Make the best format for this.", []),
    ("Create an SOP document.", []),
    ("Can you make a PDF of this?", ["pdf"]),
    ("Could you please put together a proposal for them", []),
    ("I need a deck for Monday's board meeting.", []),
])
def test_requests_that_must_create_a_file(text, formats):
    intent = I.decide(text)
    assert intent.action == "create", (text, intent)
    assert intent.formats == formats, (text, intent)
    assert intent.instruction == text.strip()


# --------------------------------------------------------- must not create --


@pytest.mark.parametrize("text", [
    "What is a PDF?",
    "Can a PDF contain video?",
    "Explain how Word documents work.",
    "Should I use Excel or a database?",
    "What does PPTX mean?",
    "Show me Python code that reads a DOCX.",
    "Write a Python script that generates a PDF from HTML.",
    "How do I open a docx on Linux?",
    "Is a spreadsheet the right tool for inventory?",
    "The report from finance said revenue was up 8%.",
    "I attached the deck they sent; what do you think of slide 3?",
    "Tell me about the document formats Salesforce supports.",
    "Why does Excel round my numbers?",
    "Summarise what we discussed.",
])
def test_requests_that_must_not_create_a_file(text):
    intent = I.decide(text, has_artifacts=False)
    assert intent.action == "none", (text, intent)
    assert intent.ambiguous is False, (text, intent)


def test_a_format_named_in_conversation_is_not_a_request():
    # A person discussing their week; no verb asks for anything.
    intent = I.decide("We usually send the PDF on Fridays and the Excel on Mondays.")
    assert intent.action == "none" and intent.formats == ["pdf", "xlsx"]


# ---------------------------------------------------------------- follow-ups --


@pytest.mark.parametrize("text,action,reference,hint", [
    ("Make it shorter.", "edit", "latest", ""),
    ("Make slide 4 shorter.", "edit", "latest", "slide 4"),
    ("Change the title.", "edit", "latest", "title"),
    ("Use a more professional tone.", "edit", "latest", ""),
    ("Add a chart to the report.", "edit", "named", "report"),
    ("Add our logo.", "edit", "latest", ""),
    ("Change page 2.", "edit", "latest", "page 2"),
    ("Remove confidential information from the document.", "edit", "named", "document"),
    ("Convert it to PowerPoint.", "convert", "latest", ""),
    ("Convert the previous document to PDF.", "convert", "latest", "document"),
    ("Create a PDF too.", "convert", "latest", ""),
    ("Also as PDF.", "convert", "latest", ""),
    ("Give me a Word version of the deck.", "convert", "named", "deck"),
])
def test_follow_ups_on_an_existing_artifact(text, action, reference, hint):
    intent = I.decide(text, has_artifacts=True, artifact_hints=["Q3 deck", "Pricing SOP"])
    assert intent.action == action, (text, intent)
    assert intent.reference == reference, (text, intent)
    assert intent.reference_hint == hint, (text, intent)


def test_a_named_artifact_wins_over_the_latest():
    intent = I.decide("Make the Pricing SOP shorter.", has_artifacts=True, artifact_hints=["Q3 deck", "Pricing SOP"])
    assert intent.action == "edit" and intent.reference == "named" and intent.reference_hint == "Pricing SOP"


def test_going_back_to_a_version():
    intent = I.decide("Go back to version 1.", has_artifacts=True)
    assert intent.action == "edit" and intent.version == 1 and intent.rule == "restore-version"


def test_follow_up_words_without_an_artifact_are_not_edits():
    # Nothing to edit: "make it shorter" is about the previous text answer.
    intent = I.decide("Make it shorter.", has_artifacts=False)
    assert intent.action == "none"
    # …but a conversion of the previous ANSWER is an export.
    intent = I.decide("Export the previous answer as PDF.", has_artifacts=False, has_assistant_answer=True)
    assert intent.action == "export" and intent.formats == ["pdf"] and intent.reference == "previous_answer"
    intent = I.decide("Give me that answer as a Word document.", has_assistant_answer=True)
    assert intent.action == "export" and intent.formats == ["docx"]


def test_an_edit_of_a_named_format_keeps_the_explicit_format():
    intent = I.decide("Convert the previous document to PDF.", has_artifacts=True)
    assert intent.action == "convert" and intent.formats == ["pdf"]


# ------------------------------------------------------------- ambiguity --


def test_the_ambiguous_band_is_narrow_and_defaults_to_no_file():
    # A creation verb, a document noun, a question mark, no format, not a
    # polite imperative: the one shape the rules hand to the classifier.
    text = "Maybe write up a document for this?"
    intent = I.decide(text)
    assert intent.action == "none" and intent.ambiguous is True
    # A plain question about formats is NOT ambiguous — it is simply not a request.
    assert I.decide("Would a report help here?").ambiguous is False

    async def run():
        # No hook → no file.
        assert (await I.decide_with_hook(text, None)).action == "none"

        # A hook that says yes.
        async def yes(t):
            return I.ArtifactIntent("create", rule="model")

        verdict = await I.decide_with_hook(text, yes)
        assert verdict.action == "create" and verdict.rule == "classifier:model"

        # A hook that fails → the rules' answer stands.
        async def boom(t):
            raise RuntimeError("engine down")

        assert (await I.decide_with_hook(text, boom)).action == "none"

        # A clear request never consults the hook.
        calls = {"n": 0}

        async def counting(text):
            calls["n"] += 1
            return None

        assert (await I.decide_with_hook("Create a PDF.", counting)).action == "create"
        assert calls["n"] == 0

    asyncio.run(run())


def test_empty_text_is_nothing():
    assert I.decide("   ").action == "none"
