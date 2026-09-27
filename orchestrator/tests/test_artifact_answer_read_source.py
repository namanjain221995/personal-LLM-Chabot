"""A question about a file the PERSON sent is not answered from OUR spec.

THE DEFECT THESE PIN (verifier, 2026-09-27). The artifact read-back branch
(app/main.py) sits ABOVE the video route, the document route and the two
image routes, and the first version of it claimed every turn the intent gate
called a question:

    elif artifact_intent is not None and (
        artifact_intent.wants_file
        or _as3_describe.is_artifact_question(artifact_intent)
    ):

Composed with the intent gate that records the verdict (track
`question-not-edit`), measured on 2026-09-27 with has_artifacts=True and no
upload in the current turn, "what does the chart in that photo say?", "what
does that image contain?" and "what does the map on page 2 show?" all come
back action=none, rule=answer-artifact:contents, answer_about_artifact=True.
Each was then claimed by that branch; `wants_file` is False so main.py's own
image read is skipped; and the answer was read back from a stored WORKBOOK's
spec.json — "**TechSara AI Engineering Workflow Tracker** (v1) is a workbook
with 2 sheets…" to someone asking about a photo. The attachment was never
opened and the route that would have opened it never ran.

This is the same failure `_asks_about_an_attachment` was written for on
2026-09-16, one branch below in the same elif chain, whose comment quotes
"what does the map on page 2 show?" verbatim.
"""
from __future__ import annotations

import inspect

import pytest

from app.artifacts import describe as D


def _request(text: str = "", **kw):
    from app.main import ChatRequest

    return ChatRequest(message=text, **kw)


def _intent_that_asks():
    """The verdict the gate records: action stays "none", the flag is set."""
    from app.artifacts.intent import ArtifactIntent

    intent = ArtifactIntent("none", rule="answer-artifact:contents")
    intent.answer_about_artifact = True
    return intent


# ------------------------------------------------- the rule, in one place --


def test_a_question_turn_that_holds_a_read_source_is_not_answered_from_a_spec():
    """`answers_from_spec` is the whole rule: the gate's verdict AND no file of
    the person's own to read. The engine's read-back cannot open a photo, a PDF
    page or a video, so a turn that holds one belongs to the engine that can."""
    intent = _intent_that_asks()
    assert D.is_artifact_question(intent) is True
    assert D.answers_from_spec(intent, has_read_source=False) is True
    assert D.answers_from_spec(intent, has_read_source=True) is False
    # A file request is untouched by the carve-out — it is not a question at
    # all, so the branch above still claims it whatever the turn carries.
    assert D.answers_from_spec(None, has_read_source=False) is False


def test_main_reaches_the_read_back_only_through_answers_from_spec():
    """The structural guard. The carve-out lives in `answers_from_spec`, so a
    later edit that calls `is_artifact_question` directly at that branch would
    re-open the defect silently. main.py must not name it."""
    from app import main

    source = inspect.getsource(main)
    assert "_as3_describe.answers_from_spec(" in source
    assert "is_artifact_question" not in source


# ------------------------------------- what counts as a file to read, and not --


@pytest.mark.parametrize("text", [
    "what does the chart in that photo say?",
    "what does that image contain?",
    "what does the map on page 2 show?",
    "what does this document say?",
])
def test_an_earlier_image_makes_the_turn_the_image_routes(text):
    """`image_memory.followup` has already read the words and produced bytes;
    that IS the words test, and the route two branches below opens the
    picture."""
    from app.main import _carries_a_file_to_read

    assert _carries_a_file_to_read(text, _request(text), False, True) is True


@pytest.mark.parametrize("text", [
    "what does the map on page 2 show?",
    "what is written in the pdf i sent?",
    "what does the diagram in the attachment mean?",
    "read the file i uploaded",
])
def test_an_earlier_document_needs_the_words_and_these_have_them(text):
    """A document uploaded in an EARLIER turn has no route of its own — it
    rides as a pinned system block — so it takes the words. They are narrow on
    purpose: `visuals.asks_about_attachment_content` is True for 13 of the 49
    labelled artifact questions (measured 2026-09-27: q03 "what is in this
    sheet", q17 "list the headings in the document", …), so using it here
    would take a quarter of the read-back class away in the commonest flow of
    all — upload a PDF, make a file from it, ask about the file."""
    from app.main import _carries_a_file_to_read

    assert _carries_a_file_to_read(text, _request(text), False, False,
                                   stored_documents=True) is True
    # …and with no document in the conversation the same words read back the
    # artifact, because there is nothing else to read.
    assert _carries_a_file_to_read(text, _request(text), False, False,
                                   stored_documents=False) is False


def test_a_video_or_an_attached_file_is_a_read_source_without_any_words_test():
    from app.main import _carries_a_file_to_read

    text = "Ok What This sheet have ??"
    assert _carries_a_file_to_read(text, _request(text), True, False) is True
    assert _carries_a_file_to_read(
        text, _request(text, pdf_uploads=[{"upload_id": "a" * 32, "name": "q3.pdf"}]),
        False, False) is True


@pytest.mark.parametrize("text", [
    "Ok What This sheet have ??",
    "what is in this sheet",
    "what did you put in the second sheet ??",
    "summarise the tracker you made",
    "list the headings in the document",
    "what data is in the excel you just made",
    "read back what the sheet has",
    "what formulas are in it?",
    "i want to Know ?? please tell me Only Not create d??",
])
def test_the_anchor_and_its_neighbours_keep_the_read_back(text):
    """The production anchor and eight of the 49 labelled turns, in a
    conversation that ALSO holds an uploaded document. None of them names the
    upload, so the artifact is still what is read back — otherwise this fix
    would cost the class it exists to serve."""
    from app.main import _carries_a_file_to_read

    assert _carries_a_file_to_read(text, _request(text), False, False,
                                   stored_documents=True) is False
    assert D.answers_from_spec(
        _intent_that_asks(),
        has_read_source=_carries_a_file_to_read(text, _request(text), False, False,
                                                stored_documents=True),
    ) is True


def test_every_labelled_artifact_question_survives_a_stored_document():
    """The whole class, not a sample: none of the 49 turns labelled
    `answer_about_artifact` in the programme's corpus may be diverted by the
    document words test. Measured 2026-09-27: 0 of 119."""
    import json
    import os

    from app.main import _carries_a_file_to_read

    path = os.environ.get("INTENT_CORPUS") or ""
    if not path or not os.path.isfile(path):
        pytest.skip("the programme corpus is not on this machine (set INTENT_CORPUS)")
    items = json.load(open(path, encoding="utf-8"))["items"]
    # Five items carry `text_build` instead of a literal text, so a pasted
    # table of ten thousand rows is not in the corpus file; the scorer builds
    # prefix + header + row_template * rows, and so does this.
    def _text_of(item):
        if (item.get("text") or "").strip():
            return item["text"]
        r = item.get("text_build") or {}
        rows = "".join(
            str(r.get("row_template", "")).format(i=i, owner=i % 7, day=1 + i % 28, pri=1 + i % 4)
            for i in range(1, int(r.get("rows", 0)) + 1)
        )
        return str(r.get("prefix", "")) + str(r.get("header", "")) + rows

    texts = [(t["id"], _text_of(t)) for t in items]
    # 117 of the 119, because s12 and s13 ARE empty on purpose ("an empty
    # message is not a question about anything").
    assert len(texts) == 119, "the corpus changed shape"
    assert len([t for t in texts if t[1].strip()]) == 117, "the corpus did not build"
    # A ChatRequest that carries NOTHING: the only thing under test here is the
    # words half, so the request is a placeholder and the text is the argument.
    bare = _request("x")
    diverted = [i for i, txt in texts
                if _carries_a_file_to_read(txt, bare, False, False, stored_documents=True)]
    assert diverted == [], diverted


# --------------------------------------------------- HELD-OUT neighbours --
#
# The programme's 119-turn corpus was authored the same day as the gate, so it
# is IN-SAMPLE for a rule written against it: `_NAMES_AN_UPLOAD_RE` scoring 0
# hits on it proves only that it does not contradict the turns it was checked
# against. These 32 sentences are NOT in that corpus and were written before
# the numbers below were measured (2026-09-27).

#: Questions about a file THIS PLATFORM made. Every one of them must keep the
#: read-back even in a conversation that also holds an uploaded document.
HELD_OUT_ABOUT_OUR_FILE = (
    "how many rows does the tracker have?",
    "which columns are on the summary tab?",
    "tell me the sheet names again",
    "is there a notes column?",
    "how many slides are in the deck?",
    "what headings does the report use?",
    "does the workbook have a totals row?",
    "remind me what's in the second tab",
    "what did you call the columns in the tracker?",
    "kitne rows hai is sheet me?",
    "what charts are in the report you made?",
    # The one the veto exists for: a PDF this platform produced HAS pages, and
    # `page 3` alone would have diverted this to the chat engine.
    "what's on page 3 of the report you made?",
    "what format did you save the tracker in?",
    "just tell me what the deck covers, no new file",
    "how big is the pdf you generated?",
    "શીટમાં કેટલી પંક્તિઓ છે?",
    "what is the title of the second slide?",
    "list the tabs",
    "does it have a due date column or not ??",
    "summarise what the workbook covers",
    "what are the headings in the report, only tell me",
    "how many columns did you end up with?",
)

#: Questions about a document the PERSON uploaded in an earlier turn. Every one
#: must reach the engine that has the document, not the artifact's spec.
HELD_OUT_ABOUT_THEIR_UPLOAD = (
    "what does the table on page 4 of the pdf say?",
    "what is in the document i uploaded?",
    "translate the text in the attachment",
    "what does the invoice i sent say?",
    "read page 7 for me",
    "summarise the report i shared earlier",
    "what's the total on page 2?",
    "what did the pdf i attached say about pricing?",
    "in the file i uploaded, what is the second column?",
    "what does the scan i sent show?",
)


@pytest.mark.parametrize("text", HELD_OUT_ABOUT_OUR_FILE)
def test_held_out_questions_about_our_own_file_keep_the_read_back(text):
    from app.main import _carries_a_file_to_read

    assert _carries_a_file_to_read(text, _request(text), False, False,
                                   stored_documents=True) is False


@pytest.mark.parametrize("text", HELD_OUT_ABOUT_THEIR_UPLOAD)
def test_held_out_questions_about_their_upload_reach_the_document(text):
    from app.main import _carries_a_file_to_read

    assert _carries_a_file_to_read(text, _request(text), False, False,
                                   stored_documents=True) is True
    # …and with no document in the conversation there is nothing else to read,
    # so the same words read the artifact back rather than answering nothing.
    assert _carries_a_file_to_read(text, _request(text), False, False,
                                   stored_documents=False) is False
