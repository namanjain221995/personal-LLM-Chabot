"""The file capability line in the answering prompts, and the denial detector
the post-answer backstop uses (app/engines/capability.py).

AS3 (2026-09-15): the chat model answered a request for a Word file with
"as an AI I cannot create a .docx" and python-docx code. The denials and the
benign answers below are authored for this test (no user content); the three
"repro" answers are the heads of what the local model actually said to the
paraphrased production prompts before the capability line existed.
"""
from __future__ import annotations

import json
import os

import pytest

from app.engines import capability as cap

DENIALS = [
    "I cannot directly create or send a `.docx` file because I am an AI running in a text-based interface.",
    "Since I cannot directly create and send a binary .docx file, I have written a Python script instead.",
    "As an AI, I can't generate files, but here is the text you can paste into Word.",
    "I'm unable to attach a PDF to this chat.",
    "Unfortunately I am not able to provide a downloadable Excel file.",
    "I don't have the ability to create a Word document for you.",
    "I do not have the capability to export a spreadsheet.",
    "There is no way for me to send you an actual PDF file.",
    "I can't produce a PowerPoint file, but I can outline the slides.",
    "A .docx file cannot be created from here, so copy the text below.",
    "As a text-based assistant, I can only give you the content, not a file.",
    "Main file nahi bana sakta, lekin yeh text copy kar lo.",
    "Sorry, pdf nahi bhej sakti, aap isko Word me paste kar do.",
    "मैं फ़ाइल नहीं बना सकता, लेकिन यह टेक्स्ट कॉपी कर सकते हैं।",
    "मैं पीडीएफ भेजने में असमर्थ हूँ।",
    "હું ફાઇલ બનાવી શકતો નથી, પણ ટેક્સ્ટ આપી શકું છું.",
    "હું પીડીએફ મોકલી શકતો નથી.",
    "I won't be able to generate an xlsx for you here.",
    "Here is the Python code to generate a classy Word document. ```python\nfrom docx import Document\n```",
    "Copy the text below and paste it into Microsoft Word, then save it. ```python\nimport openpyxl\n```",
]

BENIGN = [
    "I can't open that link — could you paste the text?",
    "I can't see the attachment clearly; the scan is blurry.",
    "The PDF you attached has 12 pages and mentions a termination clause on page 4.",
    "To convert a Word file to PDF, use File > Save As and pick PDF.",
    "Your spreadsheet has 60 rows; 24 are Resolved.",
    "I cannot find a revenue figure for March in the document.",
    "I'm unable to verify that number without the source.",
    "The report looks complete; the only gap is the appendix.",
    "Excel and CSV differ: CSV has no formatting or formulas.",
    "I don't have access to your Salesforce org in this mode.",
    "I can't read handwriting in this image reliably.",
    "Here is a summary of the attached document in five points.",
    "The file you uploaded is a CSV with 3 columns.",
    "Sure — ask me to make it a Word document whenever you are ready.",
    "मैं यह लिंक नहीं खोल सकता।",
    "यह फ़ाइल 12 पेज की है।",
    "આ ફાઇલમાં 3 કૉલમ છે.",
    "Is file me 60 rows hain aur 24 resolved hain.",
    "You can write a Python script with openpyxl if you want to automate this yourself.",
    "I can't guarantee the totals are right because two rows are blank.",
]


@pytest.mark.parametrize("text", DENIALS)
def test_denials_are_detected(text):
    assert cap.denial_in(text), text


@pytest.mark.parametrize("text", BENIGN)
def test_benign_file_mentions_are_not_denials(text):
    assert not cap.denial_in(text), text


def test_the_counts_are_20_of_20_and_0_of_20():
    assert sum(cap.denial_in(t) for t in DENIALS) == 20
    assert sum(cap.denial_in(t) for t in BENIGN) == 0


_REPRO = os.path.join(os.path.dirname(__file__), "fixtures", "as3_chat_repro_answers.json")


def test_the_recorded_denials_before_and_after_the_capability_line_are_detected():
    """Measured 2026-09-15: WITH the capability line the model still denied
    twice and pasted python-docx once for the three prompts (the gate is the
    fix; the backstop must see all six)."""
    answers = json.load(open(_REPRO, encoding="utf-8"))
    assert len(answers["before_capability_line"]) == 3 and len(answers["after_capability_line"]) == 3
    assert all(cap.denial_in(a) for a in answers["before_capability_line"] + answers["after_capability_line"])


def test_the_capability_line_is_in_every_answering_prompt_and_not_in_the_lane_or_tool_steps():
    from app.engines import agent, chat, dataset, document, vision

    line = cap.CAPABILITY_LINE
    assert line in chat._messages("hello", [], "assistant")[0]["content"]
    assert line in chat._messages("hello", [], "salesforce")[0]["content"]
    assert line in agent._SYNTH_SYSTEM
    # Track G (2026-09-18) replaced document._SYSTEM with _system_for(question),
    # which assembles BASE + the question's mode block + STRUCTURE. The capability
    # line must ride on EVERY mode, so all four are checked, not one string.
    for question in ("summarise this", "what is the total on this invoice?",
                     "is this helpful for my setup?", "extract the parties and tell me if I should renew"):
        assert line in document._system_for(question), question
    assert line in vision._SYSTEM
    assert line in dataset._SYSTEM
    # The Fast small-talk lane keeps its own short persona prompt, and tool
    # steps never talk about files.
    assert line not in chat._lane_messages("hello", [])[0]["content"]
    assert line not in agent._STEP_LLM_SYSTEM
    assert line not in chat.ASSISTANT_SYSTEM


def test_the_line_never_promises_a_file_is_being_prepared():
    low = cap.CAPABILITY_LINE.lower()
    assert "never claim a file is being prepared" in low
    assert "is being prepared" not in low.replace("never claim a file is being prepared", "")


@pytest.mark.parametrize("lang", ["en", "hinglish", "hi", "gu", "gujlish"])
def test_offer_line_in_every_language_form(lang):
    line = cap.offer_line(lang)
    assert line and ("Word" in line or "वर्ड" in line or "વર્ડ" in line)
    assert not cap.denial_in(line)


def test_denial_detection_is_bounded_on_a_huge_answer():
    import time

    huge = ("word " * 200_000) + " I cannot create a docx file."
    t0 = time.perf_counter()
    cap.denial_in(huge)
    assert time.perf_counter() - t0 < 1.0
