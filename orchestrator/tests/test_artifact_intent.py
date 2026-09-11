"""The artifact-intent gate (app/artifacts/intent.py), example by example.

Every MUST / MUST NOT in the product brief is here as a test, plus the
follow-up shapes. Offline: no model is consulted by the rules.
"""
from __future__ import annotations

import asyncio

import pytest

from app.artifacts import formats as F, intent as I


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
    # CONTRACT-2 §5 (2026-09-12): csv is a format, dataset words are nouns,
    # multi-format lists keep their order, typos count, hand-over verbs create.
    ("Create a CSV of 500 sample customers", ["csv"]),
    ("Create a CSV with 500 rows of sample customer data", ["csv"]),
    ("Give me a csv file with 500 rows", ["csv"]),
    ("Share XLSX, Word, PDF, and CSV of this audit.", ["xlsx", "docx", "pdf", "csv"]),
    ("share XLSX, Word, PDF and CSV", ["xlsx", "docx", "pdf", "csv"]),
    ("Share the audit results as XLSX, Word, PDF and CSV", ["xlsx", "docx", "pdf", "csv"]),
    ("Create XLSX or CSV of the table above.", ["xlsx", "csv"]),
    ("Make the best output for this data.", []),
    ("Give me a spread sheet and a cvs file of these rows", ["xlsx", "csv"]),
    ("Give me the audit as a cvs file", ["csv"]),
    ("Create a spread sheet of the customers", ["xlsx"]),
    ("Export this as xlxs", ["xlsx"]),
    ("Provide a CSV of the leads", ["csv"]),
    ("Deliver the report as a PDF", ["pdf"]),
    ("Prepare a dataset of 200 sample orders", ["csv"]),
    ("Compile a comma-separated file of the leads", ["csv"]),
    ("Produce a ppt on onboarding", ["pptx"]),
    ("Generate 1,000 sample records for testing", []),
])
def test_requests_that_must_create_a_file(text, formats):
    intent = I.decide(text)
    assert intent.action == "create", (text, intent)
    assert intent.formats == formats, (text, intent)
    assert intent.instruction == text.strip()
    assert intent.new_artifact is True and intent.raw_text == text


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
    # Scenario D (discovery 2026-09-12): csv in a question is still a question.
    "What is a CSV?",
    "Should I use CSV or Excel?",
    "Show me Python code that reads a CSV.",
    "What is the difference between PDF and DOCX?",
    "Is Excel or CSV better for this data?",
    "Share your thoughts on the report",
    "I need the records from last week",
    "Summarise the slides above",
    "I attached the deck they sent, see slide 3.",
])
def test_requests_that_must_not_create_a_file(text):
    intent = I.decide(text, has_artifacts=False)
    assert intent.action == "none", (text, intent)
    assert intent.ambiguous is False, (text, intent)


def test_a_format_named_in_conversation_is_not_a_request():
    # A person discussing their week; no verb asks for anything.
    intent = I.decide("We usually send the PDF on Fridays and the Excel on Mondays.")
    assert intent.action == "none" and intent.formats == ["pdf", "xlsx"]


# ------------------------------------------------- scenarios of 2026-09-12 --


_SCENARIO_A = (
    "Create a CSV dataset containing 500 realistic sample records for an AI project evaluation system. "
    "Include candidate_id, candidate_name, department, project_name, technical_score, communication_score, "
    "quality_score, completion_time, status, evaluator, and evaluation_date. Ensure the data is internally consistent."
)
_SCENARIO_B = (
    "Create a professional PDF report on Artificial Intelligence in Indian Businesses in 2026. Include an executive "
    "summary, current use cases, benefits, risks, implementation roadmap, comparison table, recommendations, and "
    "conclusion. Make it visually professional and suitable for senior management."
)


def test_scenario_a_a_csv_dataset_with_a_row_count():
    """C1 (discovery 2026-09-12): this was `none / no-request` and the model
    typed the rows in the answer. CONTRACT-2 §5."""
    intent = I.decide(_SCENARIO_A)
    assert intent.action == "create" and intent.formats == ["csv"], intent
    assert intent.row_count == 500
    assert intent.raw_text == _SCENARIO_A
    decision = F.decide(intent.instruction, explicit_only=intent.formats)
    assert (decision.kind, decision.formats, decision.template_id) == ("workbook", ["csv"], "data"), decision


def test_scenario_b_a_new_pdf_after_an_artifact_is_a_create():
    """C2: `edit / reference=latest` — rule 3c took "make it visually
    professional" plus a bare "it" before the creation verb in the first
    clause was reached. CONTRACT-2 §5: the positional create rule."""
    intent = I.decide(_SCENARIO_B, has_artifacts=True, artifact_hints=["Monsoon in Mumbai: A Poetic Reflection"])
    assert intent.action == "create" and intent.formats == ["pdf"], intent
    assert intent.reference == "none" and intent.new_artifact is True
    assert intent.rule == "create-first-clause"


@pytest.mark.parametrize("text,formats", [
    (_SCENARIO_B, ["pdf"]),
    ("Create a professional PDF report on X and make it concise", ["pdf"]),
    ("Create a deck about X and add the logo", []),
    ("Create a PDF report on the audit. Include the table I pasted", ["pdf"]),
    ("Create a new PDF about Q4, not an update to the previous one", ["pdf"]),
    ("I want another report on Q4", []),
    ("Make a separate spreadsheet for the APAC numbers.", ["xlsx"]),
])
def test_creation_after_an_artifact_is_a_create(text, formats):
    intent = I.decide(text, has_artifacts=True, artifact_hints=["Q3 deck", "Pricing SOP"])
    assert intent.action == "create", (text, intent)
    assert intent.formats == formats and intent.new_artifact is True and intent.reference == "none", (text, intent)


@pytest.mark.parametrize("text,action", [
    # The conversions and edits the positional rule must NOT take.
    ("Create a PDF version too.", "convert"),
    ("Create a PDF too.", "convert"),
    ("Make slide 4 shorter.", "edit"),
    ("Make the deck shorter.", "edit"),
    ("Use a more professional tone.", "edit"),
    ("Add a comparison chart.", "edit"),
    ("Add another slide about pricing", "edit"),
    ("Export this as xlxs", "convert"),
    ("Also as a spread sheet.", "convert"),
    ("Convert it to CSV.", "convert"),
])
def test_follow_ups_the_positional_rule_leaves_alone(text, action):
    intent = I.decide(text, has_artifacts=True, artifact_hints=["Q3 deck", "Pricing SOP"])
    assert intent.action == action and intent.new_artifact is False, (text, intent)


def test_explicit_formats_with_an_object_and_no_verb_create():
    """C3: "share XLSX, Word, PDF and CSV" was `none` with two formats
    found — no verb it knew, and no "explicit formats ⇒ create" arm."""
    for text in ("XLSX, Word, PDF and CSV of this audit please", "PDF of the above", "a CSV and an Excel for these rows"):
        intent = I.decide(text)
        assert intent.action == "create" and intent.rule == "create-formats-object", (text, intent)
    intent = I.decide("XLSX, Word, PDF and CSV of this audit please", has_artifacts=True)
    assert intent.action == "create" and intent.formats == ["xlsx", "docx", "pdf", "csv"]
    # A question in that shape is not taken.
    assert I.decide("PDF or Excel of this table?").action == "none"


@pytest.mark.parametrize("text,count", [
    ("Create a CSV of 500 sample customers", 500),
    ("Generate 1,000 sample records for testing", 1000),
    ("Give me a spreadsheet with 250 rows of data", 250),
    ("Make a dataset of 40 realistic sample entries", 40),
    ("Create a CSV of the customers", None),
    ("Create a PDF about the 2026 plan", None),
])
def test_row_count_is_read_from_the_words(text, count):
    assert I.decide(text).row_count == count


def test_raw_text_keeps_the_paste_and_the_decision_reads_a_prefix():
    """C5: a pasted table reached the composer flattened and cut at 4,000
    characters. The decision still reads the collapsed prefix; the engine
    gets the original, tabs and newlines intact."""
    table = "\n".join(f"host-{i}\t{'' if i % 3 else 'blank'}\tfinding {i}" for i in range(400))
    text = "Share XLSX and CSV of this audit table:\n" + table
    intent = I.decide(text)
    assert intent.action == "create" and intent.formats == ["xlsx", "csv"]
    assert intent.raw_text == text and "\t" in intent.raw_text and len(intent.raw_text) > 4000
    assert len(intent.instruction) <= 4000 and "\t" not in intent.instruction


def test_typos_and_aliases_name_their_format():
    """C4: cvs, spread sheet, xlxs, powerpint and a bare "word" in a list."""
    assert I.decide("Give me the audit as a cvs file").formats == ["csv"]
    assert I.decide("Create a spread sheet of the customers").formats == ["xlsx"]
    assert I.decide("Export this as xlxs").formats == ["xlsx"]
    assert I.decide("Make a powerpint about Q3").formats == ["pptx"]
    assert I.decide("Give me xlsx, word, pdf and csv of this").formats == ["xlsx", "docx", "pdf", "csv"]
    # "cvs" without a creation context is not a format.
    assert I.decide("The cvs pipeline failed again").formats == []
    # "word" outside a format list is a word.
    assert I.decide("Write a report in plain words about the launch").formats == []


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


def test_a_conversion_to_csv_names_the_dataset():
    intent = I.decide("Give me a CSV version of the dataset.", has_artifacts=True, artifact_hints=["Q3 deck"])
    assert intent.action == "convert" and intent.formats == ["csv"] and intent.reference_hint == "dataset"


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

        # A verdict from the hook carries the original text for the engine.
        assert (await I.decide_with_hook(text, yes)).raw_text == text

    asyncio.run(run())


def test_empty_text_is_nothing():
    assert I.decide("   ").action == "none"
