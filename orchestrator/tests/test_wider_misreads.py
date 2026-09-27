"""WIDER MISREADS — the turns the artifact gate reads as something else.

THE COMPLAINT (owner, 2026-09-27): "When it creates a document or a sheet,
why does it not understand the user? ... Not this issue only, there are
multiple issues."

The anchor case of that complaint — a QUESTION about a workbook answered by
re-rendering the workbook — is owned by another track. This file is the
sweep AROUND it: five further shapes that `intent.decide` reads as something
the person did not say, each measured on origin/dev 1f80aa3a2b, each with
the contrast case that shows the rules get the neighbouring shape right.

ALL FIVE ARE FIXED, on the five commits after 8acbcd2 (2026-09-27), and this
file is now the regression guard rather than the reproduction. It was written
the other way round: every defect carried an `xfail(strict=True)` test
asserting what the person asked for, next to a plain test pinning the
CONTRAST — the same words in a context, or with the one synonym, where the
rules already did the right thing. The 49 xfails are gone because the fixes
turned them into passes; nothing was deleted, nothing was softened, and no
assertion was reversed. Each fixed test's docstring says what it measured
before and after. Several gained assertions rather than losing them, and the
W4 drift measurement — which asserted the drift as a fact and therefore had
to go — was replaced by the anti-drift guard it argued for.

HOW TO READ A CASE. What the person asked for, then the CONTRAST that bounds
it: the neighbouring shape that already worked, or the same words with no
format, which must not move.

THE FIVE, most harmful first. Each heading states what it did BEFORE the fix.

W1  A REFUSAL MAKES THE FILE. "I don't want another file" -> create. The
    negation guards (`_NEGATED_CLAUSE_RE` line 301, `_NEGATED_FORMAT_RE`
    line 309, `_NEGATED_FILE_RE` line 872, `_CHAT_ONLY_RE` line 852) all
    want the noun adjacent to the determiner, and `new|another|separate|
    fresh|second|different` — the exact words `_NEW_FILE_RE` (line 216)
    keys on — sit between them. So the one phrase that makes the strongest
    create signal is the one phrase no negation guard can see. 19 of 28
    measured "I don't want a <det> <noun>" turns produce a file.

W2  A QUESTION MAKES THE FILE, but only once a file exists. Step 2b
    (line 1427, `create-first-clause`) has no question guard, while the
    equivalent create path at step 4 (line 1559) has one. So "would a new
    report help here?" is correctly chat with nothing in the room and a new
    report once something is.

W2b …and step 4's own question guard is switched off by a named format
    (line 1559: `and not explicit`), so "did you make a new sheet?" and
    "what happens if I generate another workbook?" build one in EVERY
    context, fresh conversation included.

W3  A PASTE SWALLOWS THE ASK. `_DECIDE_CHARS = 4000` (line 118) is a
    deliberate bound on a quadratic regex, but the ask people type AFTER
    their data is past it: a 120-row paste then "Make a sheet of this for me
    please" gets no file, where 40 rows gets one. `_should_consult`
    (line 1648) truncates the same way, so the classifier cannot rescue it
    either — there is no escape hatch, and nothing counts the loss.

W4  `sheet` AND `doc` ARE NOT FORMAT WORDS HERE. `formats.explicit_formats`
    maps both ("sheet" -> xlsx, "doc" -> docx); `intent._FORMAT_WORD`
    (line 172) holds neither, only `spread ?sheet` and `docx`. Every rule
    built on `_FORMAT_WORD` is therefore blind to the word the owner
    actually types. Both directions hurt: "all of that as a sheet" loses
    its file, and "make it a pdf, not a sheet" produces the xlsx the person
    just ruled out because `_NEGATED_FORMAT_RE` cannot see it.

W5  A CORRECTION LOSES THE FILE. `_FORMAT_ONLY_RE` (line 597) allows a
    closed set of seven leading words, so bare "pdf" exports the answer and
    "no, I meant pdf" — the words a person uses immediately after a miss —
    reaches `no-request`. This is the second strike in the owner's
    three-strike transcript, and the layer answers it with chat.

W6 is not a defect. It pins a capability the programme needs and already
    has: `inspect_files.inspect_xlsx` reads sheets, columns and row counts
    out of the RENDERED workbook, so "2 sheets, 5 rows, 6 columns" is
    answerable with no migration, no spec read and no model call.

Rules only — no model, no network, no GPU. `intent.decide` is what
fast_lane.py:336 and main.py:5743 run on the event loop.
"""
from __future__ import annotations

import pytest

from app.artifacts import formats as F
from app.artifacts import intent as I

#: The turn CONTEXT main.py:5733-5768 assembles, in the shapes these cases
#: arrive in. `artifact_id` is deliberately absent from all of them: it is
#: set only from the UI's "Edit with a prompt" box, and a plain chat turn
#: never carries one, so a harness that passes it short-circuits step 0 of
#: `decide` and hides every rule below.
TRACKER = "TechSara AI Engineering Workflow Tracker"

P0 = dict(has_artifacts=False, last_turn_is_artifact=False, has_assistant_answer=False)
PA = dict(has_artifacts=False, last_turn_is_artifact=False, has_assistant_answer=True)
PC = dict(has_artifacts=True, last_turn_is_artifact=True, has_assistant_answer=True,
          artifact_hints=(TRACKER,))
PF = dict(has_artifacts=True, last_turn_is_artifact=False, has_assistant_answer=True,
          artifact_hints=(TRACKER,))


# ----------------------------------------------------------------------- W1 --
#: Every one of these was measured as a FILE on origin/dev 1f80aa3a2b under
#: PC, and as `none` under P0. The person is refusing, in the plainest words
#: the product's own vocabulary offers.
W1_REFUSALS = [
    "I don't want a new file",
    "I don't want another file",
    "I don't want a separate file",
    "I don't want a new pdf",
    "I don't want another pdf",
    "I don't want a new excel",
    "I don't want another deck",
    "I don't want a separate report",
    "I do not need a new excel",
    "we do not need a separate report",
    "I didn't ask for a new excel",
    "don't bother with another deck",
]


@pytest.mark.parametrize("text", W1_REFUSALS)
@pytest.mark.parametrize("ctx", [PC, PF], ids=["card-last", "file-earlier"])
def test_a_refusal_of_another_file_makes_no_file(text: str, ctx: dict) -> None:
    """The person says they do not want one. Nothing may be built.

    FIXED 2026-09-27 by `_REFUSED_NEW_FILE_RE` / `_refuses_another_file`:
    the refusal now reads the same determiner words the create signal does.
    All 24 of these (12 refusals x 2 artifact contexts) decided `create`
    before the fix and `none` (`no-file-asked`) after it."""
    assert I.decide(text, **ctx).wants_file is False


@pytest.mark.parametrize("text", W1_REFUSALS)
def test_the_same_refusal_is_already_honoured_with_nothing_in_the_room(text: str) -> None:
    """THE CONTRAST. With no artifact in the conversation the identical words
    are correctly chat, which is what makes W1 a defect and not a gap: the
    words are understood, and an existing file un-understands them."""
    assert I.decide(text, **P0).wants_file is False


@pytest.mark.parametrize("text", ["I don't want a file", "I don't want a pdf", "I don't want a document"])
def test_a_refusal_with_the_noun_next_to_the_determiner_is_honoured(text: str) -> None:
    """THE CONTRAST, narrowed to one word. Drop `new|another|separate` and the
    same sentence is honoured under PC — so the defect is the adjacency the
    negation patterns require, not the negation itself."""
    assert I.decide(text, **PC).wants_file is False


# ----------------------------------------------------------------------- W2 --
#: Questions ABOUT whether a file would be a good idea. All `none` under P0
#: (`ambiguous` or `no-request`), all `create` under PC.
W2_QUESTIONS = [
    "would a new report help here?",
    "do you want me to write a memo?",
    "should we prepare a separate brief for legal?",
    "do I need another document for this?",
    "why did you make another file?",
]


@pytest.mark.parametrize("text", W2_QUESTIONS)
def test_a_question_about_making_a_file_does_not_make_one(text: str) -> None:
    """Asking whether to build something is not asking for it.

    FIXED 2026-09-27: step 2b now runs `_question_not_a_request`, the test
    step 4 always had. All five decided create under PC before the fix and
    none (`ambiguous`, or `no-request` for the "would" opener) after it."""
    assert I.decide(text, **PC).action != "create"


@pytest.mark.parametrize("text", W2_QUESTIONS)
def test_those_questions_are_already_read_correctly_with_nothing_in_the_room(text: str) -> None:
    """THE CONTRAST. Step 4 reads the same questions correctly; only the
    has_artifacts branch above it does not."""
    assert I.decide(text, **P0).wants_file is False


#: …and step 4's own guard is defeated by a named format, so these build a
#: file even in a fresh conversation.
W2B_QUESTIONS = [
    "did you make a new sheet?",
    "what happens if I generate another workbook?",
    "is it normal to create a second excel for this?",
]


@pytest.mark.parametrize("text", W2B_QUESTIONS)
@pytest.mark.parametrize("ctx", [P0, PC], ids=["fresh", "card-last"])
def test_a_question_that_names_a_format_does_not_make_a_file(text: str, ctx: dict) -> None:
    """A question about the past, or a hypothetical, is not an order.

    FIXED 2026-09-27: a named format no longer defeats step 4's question
    guard on its own -- a request marker has to be there too -- and
    `_export_shape` declines the same shape, which is where the third of
    these went (convert-artifact-turn) once the create paths let go. All six
    made a file before the fix and none after."""
    assert I.decide(text, **ctx).wants_file is False


@pytest.mark.parametrize("text", ["have you made the excel yet?", "so you created a docx?"])
def test_the_same_question_without_a_new_noun_is_already_correct(text: str) -> None:
    """THE CONTRAST: the identical question shape, with no `new|another|
    second` and no bare creation verb in the first clause, is chat today."""
    assert I.decide(text, **PC).wants_file is False


# ---------------------------------------------------------------------- W2c --
# W2c IS THE COST OF W2 AND W2b, FOUND BY QA ON 2026-09-27 AND FIXED IN THE
# SAME PASS. The two guards above are right that a question about building is
# not an order; widened, they also refused every INDIRECT request, which is
# the shape a polite person uses. Measured on this branch before the fix,
# with `create`/`create-first-clause` on origin/dev 1f80aa3a2b in each case:
#
#   PC  'do you have the bandwidth to also make a deck?'   none / ambiguous
#   PC  'do you have time to also make a deck?'            none / ambiguous
#   PC  'do you have the capacity to build a one-pager?'   none / ambiguous
#   PC  'do you think you could make a deck?'              none / ambiguous
#   PC  'is it possible to also make a deck?'              none / ambiguous
#   PC  'is there any way you can make a deck?'            none / ambiguous
#   PA  'would it be possible to get a pdf of this?'       none / no-request
#   PA  'can I download this as a file?'          create (a NEW document, in
#                                                 place of the answer the
#                                                 person had just read)
#   PA  'may I download the answer as a file?'             none / ambiguous
#
# With a FORMAT named, the loss reached a fresh conversation as well: 'do you
# have the bandwidth to make a pdf?' was `create`/['pdf'] on origin/dev under
# P0, PA, PC and PF, and `none`/`ambiguous` under all four here.
#
# NONE of this was visible to the three instruments the branch was verified
# with. tests/test_wider_misreads.py passed 137/137; the 119-case per-class
# corpus (scratchpad intent-eval/score_intent.py) scored 67 (56%) before and
# after, identical to origin/dev; the 77 authored chart requests moved 0 rows.
# `tests/test_artifact_intent_labelled.py` DID hold the download case as item
# v16 of the 205-item set, and passed anyway: its export-recall assertion is
# `>= 0.85` and the set went 38/38 -> 37/38 (1.0000 -> 0.9737), inside the
# threshold. The gates below are the ones that would have caught it.

#: The AVAILABILITY / FEASIBILITY frames. Asking whether the assistant is
#: free to do the thing, or whether the thing can be done, is how a polite
#: person asks FOR the thing. Every one of these was a file on origin/dev.
W2C_INDIRECT_REQUESTS = [
    "do you have the bandwidth to also make a deck?",
    "do you have time to also make a deck?",
    "do you have the capacity to build a one-pager?",
    "do you think you could make a deck?",
    "is it possible to also make a deck?",
    "is there any way you can make a deck?",
    "any chance you could make a deck?",
]


@pytest.mark.parametrize("text", W2C_INDIRECT_REQUESTS)
@pytest.mark.parametrize("ctx", [PC, PF], ids=["card-last", "file-earlier"])
def test_an_indirect_request_for_a_file_still_makes_one(text: str, ctx: dict) -> None:
    """A polite frame around a build verb is a request, not a question.

    Six of these seven decided `none`/`ambiguous` under both contexts before
    the QA fix of 2026-09-27 and `create` after it; "any chance you could make
    a deck?" was already right and is here so the band cannot narrow again."""
    got = I.decide(text, **ctx)
    assert got.wants_file is True, (text, got.action, got.rule)
    assert got.action == "create", (text, got.action, got.rule)


@pytest.mark.xfail(strict=True, reason=(
    "PRE-EXISTING, not a W2 cost and not fixed here. Measured 2026-09-27 on "
    "origin/dev 1f80aa3a2b and on this branch, identically, with and without "
    "the question mark and under P0/PC/PF: 'would you mind making a deck of "
    "this?' and 'do you mind making a deck?' decide none/no-request, and 'are "
    "you able to make a deck?' decides none/about-format -- 'able to make a "
    "deck' reads as a question ABOUT a format. A request marker for these "
    "shapes changes nothing, because a different rule is refusing them; the "
    "fix belongs to whoever owns `about-format` and the noun-phrase create "
    "path. strict=True so this goes red, and gets deleted, the day it works."))
@pytest.mark.parametrize("text", [
    "would you mind making a deck of this?",
    "do you mind making a deck?",
    "are you able to make a deck?",
])
def test_two_more_polite_frames_are_still_not_read_as_requests(text: str) -> None:
    assert I.decide(text, **PC).wants_file is True


@pytest.mark.parametrize("ctx,ctx_id", [(P0, "fresh"), (PA, "answer-only"), (PC, "card-last"), (PF, "file-earlier")])
def test_an_indirect_request_that_names_a_format_makes_one_in_every_context(ctx: dict, ctx_id: str) -> None:
    """The format is what carried this into a FRESH conversation: 'do you have
    the bandwidth to make a pdf?' was create/['pdf'] on origin/dev under all
    four contexts and none/ambiguous under all four before the fix."""
    got = I.decide("do you have the bandwidth to make a pdf?", **ctx)
    assert got.wants_file is True, (ctx_id, got.action, got.rule)
    assert "pdf" in got.formats, (ctx_id, got.formats)


@pytest.mark.parametrize("text", [
    "can I download this as a file?",
    "may I download the answer as a file?",
    "could I have this as a docx?",
    "would it be possible to get a pdf of this?",
])
def test_asking_to_be_given_the_answer_exports_it_rather_than_writing_a_new_one(text: str) -> None:
    """A first-person question about RECEIVING the answer hands over the
    answer. `can I download this as a file?` is item v16 of
    tests/fixtures/artifact_intent_set.py (gold `convert` under PA, which
    `_accept` reads as `export`); before the fix it decided `create`, i.e. the
    model was asked to invent a fresh document instead of rendering the one
    the person had just read, and `may I download the answer as a file?`
    decided none/ambiguous. The verb list after "can I" was a closed three
    (get|have|please), so every other verb of receiving read as a question
    about building."""
    got = I.decide(text, **PA)
    assert got.action == "export", (text, got.action, got.rule)
    assert got.target == "previous_answer" and got.reference == "previous_answer", (text, got.target, got.reference)


@pytest.mark.parametrize("text,is_request", [
    ("is it possible to create a second excel for this?", True),
    ("is it normal to create a second excel for this?", False),
    ("is it usual to build a separate deck for this?", False),
    ("do people normally make a deck for this?", False),
    ("did you have time to make the deck?", False),
    ("was a new report generated?", False),
])
def test_a_feasibility_question_is_a_request_and_a_norm_question_is_not(text: str, is_request: bool) -> None:
    """THE LINE W2c HAD TO DRAW, and the reason "a build verb plus a
    deliverable noun makes a file" is the wrong rule: every W2/W2b question
    carries both of those ("did you make a new SHEET?", "do you want me to
    write a MEMO?"), so that rule would reverse W2 and W2b. What separates the
    families is who is asked to act and when -- `possible` is feasibility and
    a request, `normal`/`usual`/`normally` ask about a practice, and `did`
    asks about the past."""
    assert I.decide(text, **PC).wants_file is is_request, (text, I.decide(text, **PC).rule)


#: fix/question-not-edit-r2's `HELD_OUT_STILL_A_FILE`, copied here VERBATIM on
#: 2026-09-27 as a permanent gate on this branch, because it is the list that
#: caught the regression above and nothing in this file could see it. The two
#: branches both edit orchestrator/app/artifacts/intent.py and are merged one
#: after the other, so each needs the other's file-request list: item 38 below
#: is the deck case, and it passes on origin/dev, on
#: fix/question-not-edit-r2, on this branch after the fix and on the two
#: merged together. If that branch's copy grows, copy the new cases here too.
#: `(text, action, extra kwargs)`; the context is PC (a file card was the last
#: turn), which is what CARD_LAST is there.
HELD_OUT_STILL_A_FILE = [
    # -- charts
    ("show me the numbers in a bar chart", "create", {}),
    ("show me the totals as a pie chart", "create", {}),
    ("show me the rows on a line chart", "create", {}),
    ("also show me the totals in a chart", "create", {}),
    ("show me a chart of the totals", "create", {}),
    ("show me the data in a chart", "create", {}),
    ("show me the columns as a chart", "create", {}),
    ("show me the sheet contents in a chart", "create", {}),
    ("can you show me the data as a chart?", "create", {}),
    ("please show me the totals in a bar chart", "create", {}),
    ("show me the chart data", "create", {}),
    ("show me the chart values as a table", "create", {}),
    ("show me the chart you made", "create", {}),
    ("show me the chart as a line graph", "create", {}),
    ("show me it as a pie chart", "create", {}),
    ("can you show me this as a bar chart", "create", {}),
    ("show me the chart as a bar chart instead", "create", {}),
    ("Bar chart of how many tickets each priority has.", "create", {}),
    ("show me the numbers in a bar chart", "create", dict(has_dataset=True)),
    # -- a format named as the target, in the same clause as the speech verb
    ("summarise the sheet into a pdf", "convert", {}),
    ("recap the sheet as a pdf", "convert", {}),
    ("show me the sheet as a pdf", "convert", {}),
    ("show me the tracker in pdf", "convert", {}),
    ("show me the data in excel", "convert", {}),
    ("summarise the report as a pdf", "convert", {}),
    ("recap the tracker in slides", "convert", {}),
    ("what i need is the sheet in pdf", "convert", {}),
    ("what i want is a totals row in the sheet", "convert", {}),
    ("tell me the summary and export it as pdf", "convert", dict(has_assistant_answer=True)),
    ("summarise the sheet in a new document", "create", {}),
    # -- one clause that both asks to be told AND asks for work
    ("tell me the totals and put them in the sheet", "convert", {}),
    ("please tell me the deadline and put it in the sheet", "convert", {}),
    ("tell me the totals and add them to the sheet", "edit", {}),
    ("tell me the deadline and add it to the tracker", "edit", {}),
    ("read back the sheet and fix the totals", "edit", {}),
    ("list the risks in the report and add a column for each", "edit", {}),
    # -- a POLITE INSTRUCTION wearing a question word
    ("what if you made it a pdf as well", "convert", {}),
    ("do you have the bandwidth to also make a deck?", "create", {}),
    ("is it possible to add a status column?", "edit", {}),
    # -- the UI's "Edit with a prompt" box
    ("what if you add a column for owner?", "edit", dict(artifact_id="a1")),
    ("what if we add a column for owner?", "edit", dict(artifact_id="a1")),
    ("how about you make it two pages", "edit", dict(artifact_id="a1")),
    ("why not add a priority column?", "edit", dict(artifact_id="a1")),
    ("why don't you add a totals row", "edit", dict(artifact_id="a1")),
    ("what about adding a totals row?", "edit", dict(artifact_id="a1")),
    ("is it possible to add a status column?", "edit", dict(artifact_id="a1")),
    ("which columns do you want removed?", "edit", dict(artifact_id="a1")),
    ("do you mind making it landscape", "edit", dict(artifact_id="a1")),
]


@pytest.mark.parametrize("text,action,extra", HELD_OUT_STILL_A_FILE,
                         ids=[f"{t[:44]}|{k.get('artifact_id') or k.get('has_dataset') or ''}"
                              for t, _a, k in HELD_OUT_STILL_A_FILE])
def test_a_held_out_request_for_a_file_survives_the_wider_misread_fixes(text, action, extra):
    """THE CROSS-BRANCH GATE. 48/48 on origin/dev 1f80aa3a2b and on
    fix/question-not-edit-r2; 47/48 on this branch before the QA fix (item 38,
    the deck) and 48/48 after it."""
    kw = dict(PC)
    kw.update(extra)
    got = I.decide(text, **kw)
    assert got.wants_file, (text, got.action, got.rule)
    assert got.action == action, (text, got.action, got.rule)


# --------------------------------------------------------------- W2d (QA) --
# W2d IS W2c A SECOND TIME, AND THE REASON THIS SECTION IS A HELD-OUT LIST
# RATHER THAN A PATTERN. W2c widened `_REQUEST_OF_YOU_RE` with an
# availability NOUN (`bandwidth|time|capacity|...`) and a feasibility
# ADJECTIVE (`possible|feasible|doable`), and QA then measured FIVE MORE
# phrasings of the same family losing the same file -- on the integration
# head 1a660b58d7 AND on it merged with fix/question-not-edit-r2. Measured
# again here on 2026-09-27 against origin/dev 2559fd1f36, `decide()` rules
# only, no model and no network:
#
#   PC, PF  'do you have a moment to make a deck?'      create/create-first-clause
#   PC, PF  'do you have a sec to make a deck of this?'            -> none/ambiguous
#   PC, PF  'have you got time to make a deck?'                    (10 rows of 20;
#   PC, PF  'do you have the bandwith to make a deck?'              P0 and PA are
#   PC, PF  'is it viable to make a deck?'                          none on dev too)
#
# A moment. A sec. `have you got` instead of `do you have`. A TYPO. `viable`
# instead of `possible`. Five more strings would have bought the sixth
# phrasing, so the rule no longer reads the wrapper at all: `_asks_you_to_build`
# reads the INFINITIVE OF WORK (`to <build verb>` plus a deliverable noun) and
# excludes the three families that must stay questions by a property of the
# wrapper -- the past, someone else's work, and a practice.
#
# THE CLASS IS OPEN-ENDED AND THIS LIST IS A FLOOR, NOT A PROOF. English has
# no closed set of ways to ask politely. What the list is for is that a
# phrasing once measured can never be lost again, and that the next one found
# is added HERE rather than to a pattern.

#: The seven W2c was written for and the five QA found after it, in one
#: permanent list. Every one of these is a file on origin/dev 2559fd1f36
#: under PC and PF.
HELD_OUT_INDIRECT_REQUESTS = [
    # -- the seven of W2c (2026-09-27, first pass)
    "do you have the bandwidth to also make a deck?",
    "do you have time to also make a deck?",
    "do you have the capacity to build a one-pager?",
    "do you think you could make a deck?",
    "is it possible to also make a deck?",
    "is there any way you can make a deck?",
    "any chance you could make a deck?",
    # -- the five QA found on the integration head AND on the shipping pair
    "do you have a moment to make a deck?",
    "do you have a sec to make a deck of this?",
    "have you got time to make a deck?",
    "do you have the bandwith to make a deck?",   # the person's typo, kept
    "is it viable to make a deck?",
]
#: NOBODY ENUMERATED THESE, and that is what they are here for: they were
#: written after `_asks_you_to_build` existed, never added to any pattern, and
#: they pass because the rule is structural. If a later change makes this
#: list go red while the twelve above stay green, the rule has been narrowed
#: back into an enumeration.
NOT_ENUMERATED_EITHER = [
    "do you have any appetite to make a deck?",
    "have you got the bandwidth to put together a one-pager?",
    "is it conceivable to make a deck?",
    "is it at all realistic to put together a deck of this?",
    "do you have a window to draft a one-pager?",
    # `minute` is in no list either, and this one was already a file on the
    # integration head for a different reason (the question guard is off for a
    # `would you` opener), so it is here as a floor rather than as a fix.
    "would you have a spare minute to write a one-pager?",
]


@pytest.mark.parametrize("text", HELD_OUT_INDIRECT_REQUESTS + NOT_ENUMERATED_EITHER,
                         ids=[t[:46] for t in HELD_OUT_INDIRECT_REQUESTS + NOT_ENUMERATED_EITHER])
@pytest.mark.parametrize("ctx", [PC, PF], ids=["card-last", "file-earlier"])
def test_the_indirect_request_family_still_makes_the_file(text: str, ctx: dict) -> None:
    """THE PERMANENT GATE ON THIS CLASS, run beside `HELD_OUT_STILL_A_FILE`.

    12 phrasings x 2 contexts for the measured ones, 6 x 2 for the ones no
    pattern was ever widened for. The five of W2d decided none/ambiguous on
    the integration head and on the shipping pair before `_asks_you_to_build`
    and create/create-first-clause after it; the seven of W2c were already
    green and are here so the band cannot narrow again."""
    got = I.decide(text, **ctx)
    assert got.wants_file is True, (text, got.action, got.rule)
    assert got.action == "create", (text, got.action, got.rule)


#: The phrasings in `HELD_OUT_INDIRECT_REQUESTS` that carry a build
#: infinitive, which is the half `_asks_you_to_build` decides on its own. The
#: other four ("do you think you could make a deck?", "is there any way you
#: can make a deck?", "any chance you could make a deck?", "do you have the
#: capacity to build a one-pager?" -- that last one HAS an infinitive) are
#: named openers in `_REQUEST_OF_YOU_RE` and are listed above, not here.
_INFINITIVE_CARRIED = [t for t in HELD_OUT_INDIRECT_REQUESTS + NOT_ENUMERATED_EITHER
                       if I._BUILD_INFINITIVE_RE.match(t.lower())]


def test_the_structural_rule_and_not_the_enumeration_carries_these() -> None:
    """WHAT MAKES THIS A RULE AND NOT A LIST.

    Ten of the eighteen gated phrasings above are decided by
    `_asks_you_to_build` reading `to <build verb> <deliverable>`, with no
    availability noun and no feasibility adjective consulted. Measured today:
    deleting the availability alternative from `_REQUEST_OF_YOU_RE` entirely
    moves 0 of 920 probe rows, 0 of `HELD_OUT_STILL_A_FILE`, 0 of these 18, 0
    of the 686 labelled items and 0 of the 77 authored chart requests --
    that constant no longer decides anything a build infinitive covers."""
    assert len(_INFINITIVE_CARRIED) >= 10, _INFINITIVE_CARRIED
    for text in _INFINITIVE_CARRIED:
        assert I._asks_you_to_build(text.lower()) is True, text


def test_the_two_indirect_request_enumerations_are_frozen() -> None:
    """THE TRIPWIRE, and the only reason the two enumerations are still here.

    W2c widened these two lists for seven phrasings and QA found five more the
    same day; W2d's answer is that the class is open-ended, so the lists stop
    growing and `HELD_OUT_INDIRECT_REQUESTS` grows instead. If you are here
    because a phrasing was reported lost: add the phrasing to that list, make
    `_asks_you_to_build` cover it, and leave these two alone. If a word really
    must be added -- because the shape carries no infinitive, which is the
    only case that justifies it -- add the phrasing to the held-out list in
    the same commit and re-pin the string here."""
    assert I._CAPACITY_NOUNS == (
        r"(?:bandwidth|time|capacity|capabilit(?:y|ies)|abilit(?:y|ies)|resources?|room|cycles|"
        r"headroom|energy)")
    assert r"(?:possible|feasible|doable)" in I._REQUEST_OF_YOU_RE.pattern


#: THE OTHER SIDE OF THE SAME RULE, and the list that IS enumerated now.
#: `(text, why)`. None of these may produce a file in any context: they ask
#: about a practice, about the past, or about work the PERSON would do.
STILL_A_QUESTION = [
    ("is it normal to create a second excel for this?", "a practice (W2b)"),
    ("is it usual to build a separate deck for this?", "a practice"),
    ("is it OK to create a second excel for this?", "a practice"),
    ("is it the norm to create a second excel for this?", "a practice"),
    ("is it good practice to build a separate deck for this?", "a practice"),
    ("was it possible to build a deck?", "the past"),
    ("is it clear what they need to make a deck?", "a third party's clause"),
    ("is it overkill to build a separate deck for this?", "a practice"),
    ("is it worth making a separate deck?", "a practice"),
    ("is it rude to ask you to make a deck?", "a practice"),
    ("do people normally make a deck for this?", "a practice"),
    ("did you have time to make the deck?", "the past"),
    ("did you manage to make the deck?", "the past"),
    ("do you want me to write a memo?", "the person's own work (W2)"),
    ("do you want me to make a deck?", "the person's own work"),
    ("do you need me to build a deck?", "the person's own work"),
]


@pytest.mark.parametrize("text,why", STILL_A_QUESTION, ids=[t[:46] for t, _w in STILL_A_QUESTION])
@pytest.mark.parametrize("ctx,ctx_id", [(P0, "fresh"), (PC, "card-last"), (PF, "file-earlier")])
def test_a_practice_the_past_and_someone_elses_work_stay_questions(
        text: str, why: str, ctx: dict, ctx_id: str) -> None:
    """THE CONTRAST FOR W2d, and the three exclusions `_asks_you_to_build`
    carries. Six of these twelve contain a build infinitive AND a deliverable
    noun ("is it normal to create a second EXCEL"), which is why the rule is
    not "a build verb plus a deliverable noun makes a file" -- that reading
    reverses W2 and W2b. All 36 rows are no-file here; on origin/dev
    2559fd1f36 the PC and PF halves of most of them are create."""
    got = I.decide(text, **ctx)
    assert got.wants_file is False, (text, why, ctx_id, got.action, got.rule)


#: `_asks_you_to_build` ON ITS OWN, as a truth table. `decide()` has other
#: rules in front of it -- a discourse opener already turns the question guard
#: off, so several of the False rows below are files anyway for a reason that
#: has nothing to do with this predicate -- so the predicate is tested here
#: directly. `(text, is a request of the assistant)`.
ASKS_YOU_TO_BUILD = [
    # -- the wrapper is not read: no availability noun, no feasibility
    #    adjective, and an invented word in either slot works the same
    ("do you have a thingummy to make a deck?", True),
    ("is it thingummy to make a deck?", True),
    ("do you have a moment to make a deck?", True),
    ("have you got time to make a deck?", True),
    ("do you have the bandwith to make a deck?", True),
    ("is it viable to make a deck?", True),
    ("is it possible for you to make a deck?", True),
    # -- a discourse opener before the wrapper does not hide it
    ("ok, do you have a moment to make a deck?", True),
    ("right, is it viable to make a deck?", True),
    ("by the way, do you have a moment to make a deck?", True),
    # -- A PRACTICE
    ("is it normal to create a second excel for this?", False),
    ("is it the norm to create a second excel for this?", False),
    ("is it good practice to build a separate deck for this?", False),
    ("is it rude to ask you to make a deck?", False),
    # -- THE PAST, inverted or declarative, opener or none
    ("did you have time to make the deck?", False),
    ("ok, did you have time to make the deck?", False),
    ("was it possible to build a deck?", False),
    ("there was no time to make a deck", False),
    # -- SOMEONE ELSE'S WORK
    ("do you want me to write a memo?", False),
    ("do you need me to build a deck?", False),
    ("is it clear what they need to make a deck?", False),
    ("the client asked us to make a deck by friday", False),
    # -- not addressed at all: a statement about the person
    ("i forgot to make a deck", False),
    ("we decided to make a deck of this", False),
    # -- a verb of ACCESS is not a verb of production
    ("do you have the bandwidth to review my pdf?", False),
    ("is it possible to read the pdf?", False),
]


@pytest.mark.parametrize("text,is_request", ASKS_YOU_TO_BUILD, ids=[t[:46] for t, _r in ASKS_YOU_TO_BUILD])
def test_asks_you_to_build_reads_the_infinitive_and_not_the_wrapper(text: str, is_request: bool) -> None:
    """THE INVERSION AND THE THREE EXCLUSIONS, pinned at the predicate.

    `_asks_you_to_build` reads no list of polite openers; the one list it
    reads is `_NORM_WRAPPER_RE`, the practice words, which is why an invented
    availability noun and an invented feasibility adjective both work. A word
    missing from a list of shapes that MUST make a file is a file the person
    asked for and silently did not get -- the W2c/W2d defect, invisible to
    every instrument in this repo. A word missing from `_NORM_WRAPPER_RE` is
    an unwanted file, which the person can see and refuse, and which W1's
    refusal path then honours. Neither list is a proof; the open end is on the
    visible side."""
    assert I._asks_you_to_build(text) is is_request, text


def test_the_practice_words_are_read_in_the_predicate_and_not_anywhere() -> None:
    """The first draft of `_NORM_WRAPPER_RE` was a bare word list, and `ok`
    and `right` are discourse openers before they are judgements: "ok, do you
    have a moment to make a deck?" lost the file to it. The word has to sit in
    a copula's predicate, or be the -ly adverb."""
    assert I._NORM_WRAPPER_RE.search("is it normal to ") is not None
    assert I._NORM_WRAPPER_RE.search("do people normally ") is not None
    assert I._NORM_WRAPPER_RE.search("ok, do you have a moment ") is None
    assert I._NORM_WRAPPER_RE.search("right, is it viable ") is None


#: THE BOUND ON THE BUILD-VERB SET, as the ACTION origin/dev 2559fd1f36 gives
#: under PC -- not as "no file", because three of these already re-render or
#: restore the artifact there, and that is the anchor case of the owner's
#: complaint, owned by fix/question-not-edit-r2 and not by this rule. The
#: `rule` is deliberately not pinned: question-not-edit-r2 relabels one of
#: these (`no-request` -> `answer-artifact:contents`) without changing the
#: action, and this list has to hold on that branch too.
NON_BUILD_VERB_FRAMES = [
    ("do you have the bandwidth to review my pdf?", "none"),
    ("do you have time to explain the sheet to me?", "none"),
    ("is there any way to know who edited the sheet?", "none"),
    ("is it possible to open the sheet in google sheets?", "convert"),
    ("is it possible to read the pdf?", "convert"),
    ("is it possible to see the sheet?", "convert"),
    ("is it possible to undo the last edit to the deck?", "edit"),
]


@pytest.mark.parametrize("text,action", NON_BUILD_VERB_FRAMES, ids=[t[:46] for t, _a in NON_BUILD_VERB_FRAMES])
def test_a_polite_frame_around_a_NON_build_verb_decides_what_dev_decides(text: str, action: str) -> None:
    """`review`, `explain`, `know`, `open`, `read`, `see` and `undo` are verbs
    of ACCESS, so the same wrappers ask a question about the artifact.

    Both halves measured 2026-09-27 on origin/dev 2559fd1f36, on the
    integration head 1a660b58d7, here, and on the merge with
    fix/question-not-edit-r2. The second assertion is the branch-independent
    one: it fails the day `_asks_you_to_build` starts reading a verb of access
    as a verb of production, whatever any other rule then does with the
    turn."""
    assert I._asks_you_to_build(text) is False, text
    got = I.decide(text, **PC)
    assert got.action == action, (text, got.action, got.rule)


# ----------------------------------------------------------------------- W3 --
def _pasted_rows(n: int) -> str:
    """`n` tab-separated rows, the shape a person pastes out of a
    spreadsheet. Built here rather than stored, so the case stays readable
    at ten thousand rows."""
    return "\n".join(f"EMP{i:04d}\tTeam {i % 7}\tEngineer\t{50000 + i}\tActive" for i in range(1, n + 1))


#: The ask people type after their data. BEFORE the fix, 40 rows (~1.5 kB)
#: was a file and 120 rows (~4.4 kB) was not, because `_clean(text)[:4000]`
#: had already cut the sentence off; nothing was logged and no metric
#: counted it. `_decide_window` reads the tail as well, at the same total
#: scanned length.
@pytest.mark.parametrize("rows", [120, 400, 10_000])
def test_an_ask_after_a_pasted_table_is_still_a_request(rows: int) -> None:
    """Paste the data, then ask. The ask must survive the paste.

    FIXED 2026-09-27 by `_decide_window`: the rules read the first 3,000
    collapsed characters and the last 1,000 instead of the first 4,000, so
    the total scanned length -- the bound that exists to keep a quadratic
    regex off the event loop -- is unchanged. All three decided
    none/no-request before the fix and create/['xlsx'] after it."""
    text = _pasted_rows(rows) + "\n\nMake a sheet of this for me please"
    assert I.decide(text, **P0).wants_file is True


@pytest.mark.parametrize("rows", [5, 40])
def test_the_same_ask_after_a_short_paste_is_already_a_request(rows: int) -> None:
    """THE CONTRAST: identical words, fewer rows above them."""
    text = _pasted_rows(rows) + "\n\nMake a sheet of this for me please"
    assert I.decide(text, **P0).wants_file is True


@pytest.mark.parametrize("rows", [120, 400, 10_000])
def test_the_same_ask_stated_first_survives_any_paste(rows: int) -> None:
    """THE CONTRAST, the other way round: the ask ABOVE the paste is read at
    every size, so the loss is positional and not about length."""
    text = "Make a sheet of this for me please\n\n" + _pasted_rows(rows)
    assert I.decide(text, **P0).wants_file is True


@pytest.mark.parametrize("rows", [120, 400])
def test_a_swallowed_ask_at_least_reaches_the_classifier(rows: int) -> None:
    """The escape hatch for a shape the rules cannot read is the classifier.
    It was behind the same truncation, so a swallowed ask reached nothing:
    `_should_consult` measured False at 120 and 400 rows.

    FIXED 2026-09-27: it is given the same `_decide_window`. Both halves are
    asserted, because the first alone would no longer exercise the second --
    the rules now read this ask themselves, and `_should_consult` is False
    for any turn that already has a verdict. The second assertion hands it
    the `none` verdict directly, which is the only way to test the window it
    is given rather than the verdict it is handed."""
    text = _pasted_rows(rows) + "\n\nMake a sheet of this for me please"
    assert I.decide(text, **P0).wants_file is True
    assert I._should_consult(I.ArtifactIntent("none", rule="no-request"), text) is True


# THE DECISION ON W3's DISCLOSED COST (2026-09-27, r3). QA disclosed that the
# head+tail window lets a request QUOTED from someone else, at the bottom of a
# pasted mail thread, be read as the person's own. DECIDED: ACCEPT IT, DO NOT
# CLOSE IT HERE, and record it as an xfail so it cannot be forgotten. Three
# reasons, in the order they decided it:
#
# 1. IT IS NOT A REGRESSION OF W3. The same quoted imperative is a file on
#    origin/dev 2559fd1f36 above the paste at every size, and below a paste of
#    40 rows or fewer. The rules have never had a model of quotation. What
#    `_decide_window` removed is a TRUNCATION that hid the shape once a paste
#    passed 4,000 collapsed characters -- an accident, not a guard, and one
#    that swallowed the person's own ask in the same breath (that is W3).
# 2. CLOSING IT IS A CAPABILITY, NOT A GUARD. It needs attribution: `>`
#    prefixes, "From:", "wrote:", "-----Original Message-----", and the
#    knowledge that everything under them is somebody else's sentence. Bolted
#    on as one more veto it would have to run at EVERY position or it would
#    just move the inconsistency, and it would take the person's own quoted
#    ask ("he asked for a deck -- please make one") with it.
# 3. THE EXPOSURE IS BOUNDED, AND IT IS THE VISIBLE DIRECTION. Only the
#    quoted IMPERATIVE reaches a file; every quoted QUESTION form is
#    none/ambiguous here, measured below, including the indirect requests this
#    commit adds. An unwanted file is a card the person can see and refuse,
#    and W1's refusal path now honours the refusal. The loss on the other side
#    is silent.

#: A QUOTED THIRD-PARTY REQUEST: `>`-quoted mail, addressed to a colleague.
_QUOTED_ASK = ("\n\n> From: priya@client.example\n"
               "> Ravi, please make a deck of this before Friday.")


@pytest.mark.parametrize("rows", [5, 40, 120, 400, 10_000])
def test_a_quoted_third_party_imperative_is_read_as_a_request_at_every_size(rows: int) -> None:
    """THE DISCLOSED COST OF W3, MEASURED AND ACCEPTED -- see the decision
    note above this test.

    Measured 2026-09-27 on origin/dev 2559fd1f36 and here, `decide()` rules
    only, P0, `_pasted_rows(n)` then `_QUOTED_ASK`:

      rows   collapsed chars   origin/dev              here
         5              263    create/create           create/create
        40            1,558    create/create           create/create
       120            4,518    none/no-request         create/create
       400           14,878    none/no-request         create/create
    10,000          370,079    none/no-request         create/create

    and with the SAME quote placed ABOVE the paste, origin/dev decides
    create/create at 5 and at 400 rows. So the rules have never told the
    person's words from a quotation: what `_decide_window` removed is the
    truncation that HID the shape once a paste passed 4,000 collapsed
    characters, and the three rows it changes are the three that used to be
    rescued by an accident. What this test asserts is the property W3 was
    for: the verdict no longer depends on the size of the paste. The
    origin/dev column is the measurement above, recorded, not re-derived."""
    text = _pasted_rows(rows) + _QUOTED_ASK
    got = I.decide(text, **P0)
    assert got.action == "create" and got.rule == "create", (rows, got.action, got.rule)


@pytest.mark.xfail(strict=True, reason=(
    "OPEN AND ACCEPTED, NOT FIXED: the rules have no model of QUOTATION, so a "
    "request a colleague made inside a pasted mail thread reads as the "
    "person's own. Measured 2026-09-27: it is a file on origin/dev "
    "2559fd1f36 too whenever the truncation does not swallow it (above the "
    "paste at every size, below a paste of 40 rows or fewer), so this is not "
    "a regression of `_decide_window` -- it is a pre-existing gap that the "
    "window made size-independent. Closing it needs an attribution model "
    "('>' prefixes, 'From:', 'wrote:') applied at EVERY position, which is a "
    "capability and not a guard, and belongs to whoever owns the paste path. "
    "strict=True so this goes red, and gets deleted, the day that lands."))
def test_a_quoted_third_party_request_should_not_make_a_file() -> None:
    text = _pasted_rows(400) + _QUOTED_ASK
    assert I.decide(text, **P0).wants_file is False


@pytest.mark.parametrize("tail", [
    "> Ravi, can you make a deck of this for the Friday review?",
    "> Ravi, do you have the bandwidth to make a deck of this?",
    "> Ravi, is it viable to make a deck of this?",
])
def test_the_indirect_request_rule_cannot_be_ARMED_from_the_tail(tail: str) -> None:
    """THE BOUND ON W2d's OWN EXPOSURE TO W3, which is why
    `_BUILD_INFINITIVE_RE` is matched against the START of the window and its
    word gap cannot cross a sentence end.

    `_decide_window` joins the head and the tail with " . ", so the new rule
    reads the opening clause of the HEAD and nothing else: a polite request
    quoted at the bottom of a 400-row paste decides none/ambiguous here, as
    it does on the integration head 1a660b58d7. Only the quoted IMPERATIVE
    above reaches a file, and that one predates this branch."""
    text = _pasted_rows(400) + "\n\n> From: priya@client.example\n" + tail
    got = I.decide(text, **P0)
    assert got.wants_file is False, (tail, got.action, got.rule)


# ----------------------------------------------------------------------- W4 --
#: The SINGULAR format words `formats.explicit_formats` reads on their own.
#: `sheet`, `sheets` and `doc` were the three `intent._FORMAT_WORD` did not
#: hold, which is W4; the rest were already in both.
FORMAT_VOCABULARY = [
    ("pdf", "pdf"),
    ("docx", "docx"), ("doc", "docx"),
    ("xlsx", "xlsx"), ("xls", "xlsx"), ("excel", "xlsx"), ("exel", "xlsx"),
    ("spreadsheet", "xlsx"), ("workbook", "xlsx"), ("sheet", "xlsx"), ("sheets", "xlsx"),
    ("pptx", "pptx"), ("ppt", "pptx"), ("powerpoint", "pptx"), ("slides", "pptx"),
    ("csv", "csv"), ("dataset", "csv"), ("data set", "csv"),
]


@pytest.mark.parametrize("word,fmt", FORMAT_VOCABULARY)
def test_the_two_format_vocabularies_agree(word: str, fmt: str) -> None:
    """THE ANTI-DRIFT GUARD, which is what W4 was.

    `formats.explicit_formats` decides which files get MADE;
    `intent._FORMAT_WORD` decides which rules can SEE a format at all. When
    they disagree the gate acts on a format policy it cannot read: measured
    2026-09-27, `explicit_formats('sheet')` was ['xlsx'] while
    `_FORMAT_WORD` did not match 'sheet', so "make it a pdf, not a sheet"
    returned ['pdf', 'xlsx'] and built the very thing that was ruled out.
    A word added to one side from here on has to be added to the other, or
    this fails."""
    import re

    assert F.explicit_formats(word) == [fmt], "formats no longer reads this word"
    assert re.compile(I._FORMAT_WORD, re.I).fullmatch(word) is not None, (
        f"intent._FORMAT_WORD cannot see {word!r}, which formats maps to {fmt}")


# --------------------------------------------------------------- W4b (QA) --
#: A PLACE the data already lives is not a format. lexicon.py has carried
#: `(?<!google )docs?` for exactly this since before W4; adding `sheets?` to
#: `intent._FORMAT_WORD` without the same guard made "google sheets" a
#: destination, and the create and convert paths acted on it. Each pair is
#: (text, ctx) and each one produces NO file on origin/dev 1f80aa3a2b.
W4B_GOOGLE_SHEETS = [
    ("can you open the sheet in google sheets?", P0),
    ("can you open the sheet in google sheets?", PA),
    ("can you open the sheet in google sheets?", PC),
    ("open the sheet in google sheets", PC),
    ("is it possible to open the sheet in google sheets?", P0),
    ("is it possible to open the sheet in google sheets?", PA),
    ("the numbers live in google sheets", PC),
    ("i keep the tracker in google sheets", PC),
]


@pytest.mark.parametrize("text,ctx", W4B_GOOGLE_SHEETS,
                         ids=[f"{t[:40]}|{'PC' if c is PC else 'PA' if c is PA else 'P0'}"
                              for t, c in W4B_GOOGLE_SHEETS])
def test_google_sheets_is_a_place_and_not_a_format(text: str, ctx: dict) -> None:
    """FIXED 2026-09-27 by `(?<!google )` in `_SHEET_FORMAT`. Before it, five
    of these eight produced a file where origin/dev produced none: "can you
    open the sheet in google sheets?" create/['xlsx'] under P0 and PA, "open
    the sheet in google sheets" convert/['xlsx'] under PC, and the two "is it
    possible ..." rows once the W2c fix stopped the question guard masking
    them (create/['xlsx'] under P0, export/['xlsx'] under PA). The last two
    rows are statements and were already right; they are here so the guard is
    not narrowed to the interrogative forms."""
    assert I.decide(text, **ctx).wants_file is False, (text, I.decide(text, **ctx).rule)


@pytest.mark.parametrize("word", ["sheet", "sheets"])
def test_the_google_guard_takes_only_the_product_name(word: str) -> None:
    """The guard is a lookbehind on one word, so the format word itself still
    reads everywhere else: "as a sheet" and "in sheets" are unaffected."""
    import re

    rx = re.compile(I._FORMAT_WORD, re.I)
    assert rx.fullmatch(word) is not None, word
    assert rx.search(f"google {word}") is None, f"google {word} must not be a format"
    assert rx.search(f"a {word}") is not None, f"a bare {word} is still a format"


#: WHAT THIS FIX DID NOT CLOSE, measured 2026-09-27 and pinned so the next
#: person has to face it deliberately rather than discover it as a bug.
#: These words still diverge, in both directions. They are outside W4 (which
#: is `sheet`/`sheets`/`doc`) and each would move a different family of
#: turns, so none of them is a one-line follow-on.
KNOWN_DIVERGENCES = [
    # formats reads the PLURAL, `_FORMAT_WORD` reads only the singular.
    ("pdfs", ["pdf"]), ("docs", ["docx"]), ("spreadsheets", ["xlsx"]),
    ("workbooks", ["xlsx"]), ("csvs", ["csv"]),
    # ...and the other direction: `formats._ALIAS["xlsx"]` guards `sheet`
    # with cheat/fact/term/style/rate/balance/time and NOT with score or
    # answer, so it reads these two as workbooks while the gate does not.
    ("score sheet", ["xlsx"]), ("answer sheet", ["xlsx"]),
]


@pytest.mark.parametrize("word,fmts", KNOWN_DIVERGENCES)
def test_the_remaining_divergences_are_the_known_ones(word: str, fmts: list) -> None:
    """See `KNOWN_DIVERGENCES`. Both halves are asserted, so closing one of
    these fails here and the person closing it has to say so."""
    import re

    assert F.explicit_formats(word) == fmts
    assert re.compile(I._FORMAT_WORD, re.I).fullmatch(word) is None


@pytest.mark.parametrize("phrase", [
    "cheat sheet", "balance sheet", "term sheet", "style sheet", "rate sheet",
    "time sheet", "fact sheet", "sheet 2",
])
def test_the_compound_sheets_are_still_not_formats(phrase: str) -> None:
    """THE BOUND on the `sheet` half of W4: the lookbehinds `_ARTIFACT_NOUNS`
    and `formats._ALIAS["xlsx"]` already carried, taken as their union, so
    widening `_FORMAT_WORD` cannot turn "balance sheet" into a workbook, and
    the digit veto keeps "sheet 2" a PART of a workbook."""
    import re

    assert F.explicit_formats(phrase) == []
    assert re.compile(rf"\b{I._FORMAT_WORD}\b", re.I).search(phrase) is None


def test_a_format_ruled_out_as_a_sheet_is_not_produced() -> None:
    """"not a sheet" must remove the workbook exactly as "not a
    spreadsheet" does. Measured before the fix: formats ['pdf', 'xlsx'] --
    the person is handed the very thing they excluded. FIXED 2026-09-27 by
    `sheets?` joining `_FORMAT_WORD`, which `_NEGATED_FORMAT_RE` is built
    on."""
    assert I.decide("make it a pdf, not a sheet", **PA).formats == ["pdf"]


def test_the_same_exclusion_spelled_spreadsheet_is_already_honoured() -> None:
    """THE CONTRAST: one synonym, and the exclusion works."""
    assert I.decide("make it a pdf, not a spreadsheet", **PA).formats == ["pdf"]


def test_an_elliptical_handover_to_a_sheet_is_a_request() -> None:
    """"all of that as a sheet" names a destination. Measured: no-request."""
    assert I.decide("all of that as a sheet", **PA).wants_file is True


def test_the_same_handover_to_a_spreadsheet_is_already_a_request() -> None:
    """THE CONTRAST: `export-elliptical` fires on the synonym."""
    assert I.decide("all of that as a spreadsheet", **PA).wants_file is True


def test_a_handover_to_a_doc_keeps_its_format_and_its_source() -> None:
    """"give me a doc of this" must export the answer as a docx. Measured
    before the fix: action=create with formats=[] -- the format is dropped,
    the reference to the answer is dropped, and the model is asked to invent
    the content.

    This one needed BOTH vocabularies moved. `_FORMAT_WORD` learning `doc`
    fixes the shape; the format itself comes from
    `formats.explicit_formats`, and `lexicon.normalize` mapped a bare `doc`
    to `docx` only at the end of a turn or after in/as/into/to, so
    `explicit_formats("a doc of this")` was [] while
    `explicit_formats("doc")` was ['docx']. An indefinite article before
    `doc` now names the deliverable there too."""
    got = I.decide("give me a doc of this", **PA)
    assert got.formats == ["docx"]
    assert got.action == "export"


def test_the_same_handover_to_a_docx_is_already_correct() -> None:
    """THE CONTRAST: the four-letter spelling keeps both."""
    got = I.decide("give me a docx of this", **PA)
    assert got.formats == ["docx"]
    assert got.action == "export"


# ----------------------------------------------------------------------- W5 --
#: A correction, which is what a person types the moment an answer missed.
#: The owner's own transcript opens its third turn with "I said ???".
W5_CORRECTIONS = [
    "no, I meant pdf",
    "I said pdf",
    "I asked for pdf",
    "as I said, pdf",
    "sorry, I meant pdf",
    "again, pdf",
]


@pytest.mark.parametrize("text", W5_CORRECTIONS)
def test_an_elliptical_correction_still_asks_for_the_file(text: str) -> None:
    """The correction names the format and nothing else. It is the same ask
    as the bare word, said by someone the product has already missed once.

    FIXED 2026-09-27 by `_CORRECTION_PREFACE` in front of the anchored
    `_FORMAT_ONLY_RE`. All six were none/no-request before and are
    export/['pdf']/export-format-only after."""
    assert I.decide(text, **PA).wants_file is True


@pytest.mark.parametrize("text,fmt", [
    ("no, I meant excel", "xlsx"), ("again, sheet", "xlsx"), ("sorry, I meant doc", "docx"),
    ("no I meant pdf, not excel", "pdf"),
])
def test_the_correction_carries_whichever_format_was_named(text: str, fmt: str) -> None:
    """The same shape in the other formats, including the one the survey
    paired it with: "not excel, pdf" and "pdf instead of excel" already
    exported, and "no I meant pdf, not excel" -- the same meaning in a
    different word order -- did not."""
    got = I.decide(text, **PA)
    assert got.action == "export" and got.formats == [fmt], (text, got)


@pytest.mark.parametrize("text", [
    "sorry", "no thanks", "I said no", "no, I meant the blue one",
    "what is a PDF?", "what should I put in it?",
])
def test_a_correction_opener_with_no_format_is_still_not_a_file(text: str) -> None:
    """THE BOUND on W5: the preface is optional and decides nothing on its
    own -- a format word still has to be the whole of what follows. These
    six were `none` before the fix and are `none` after."""
    assert I.decide(text, **PA).wants_file is False


def test_the_bare_format_word_is_already_a_request() -> None:
    """THE CONTRAST: strip the correction and `export-format-only` fires."""
    assert I.decide("pdf", **PA).wants_file is True


@pytest.mark.parametrize("text", [f"{op} give me a pdf of that" for op in
                                  ("no, I meant", "I said", "sorry, I meant", "again,")])
def test_a_correction_in_front_of_a_full_ask_is_already_harmless(text: str) -> None:
    """THE CONTRAST that bounds W5: when a finite verb phrase follows the
    opener, the opener costs nothing. Only the ELLIPTICAL correction loses
    the file, which is why widening the leading set is the whole fix."""
    assert I.decide(text, **PA).wants_file is True


# ----------------------------------------------------------------------- W6 --
def test_the_workbook_can_already_be_read_back_from_the_rendered_bytes(tmp_path) -> None:
    """NOT A DEFECT — a capability the programme should reuse instead of
    widening a jsonb column or re-reading the spec.

    The brief for this programme says a spec-grounded answer needs either the
    version row's `deliverable` widened by a migration or `store.read_spec`
    at answer time. There is a third route already shipped and already
    tested: `inspect_files.inspect_xlsx` opens the workbook the person
    downloaded and reports its sheets, their headers and their row counts.
    That is better than the spec, for the reason inspect_files' own module
    docstring gives — the spec is the renderer's INPUT, so a renderer bug
    reads back as a pass.

    The workbook below is the owner's transcript shape: 2 sheets, 5 rows,
    6 columns. Asserting it here pins the capability so nobody re-derives
    the dependency."""
    openpyxl = pytest.importorskip("openpyxl")
    from app.artifacts import inspect_files as IF

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Workstreams"
    ws.append(["Workstream", "Owner", "Status", "Due", "Priority", "Notes"])
    for i in range(5):
        ws.append([f"WS-{i + 1}", "Naman", "Active", "2026-10-01", "High", "-"])
    second = wb.create_sheet("Milestones")
    second.append(["Milestone", "Date"])
    second.append(["Kickoff", "2026-10-01"])
    path = tmp_path / "tracker.xlsx"
    wb.save(path)

    obs = IF.inspect_xlsx(path)
    facts = {
        (o.locator.get("sheet"), o.property): o.value
        for o in obs
        if o.target == "sheet" and o.property in ("columns", "row_count")
    }
    assert {s for s, _ in facts} == {"Workstreams", "Milestones"}
    assert facts[("Workstreams", "row_count")] == 5
    assert len(facts[("Workstreams", "columns")]) == 6
    assert facts[("Workstreams", "columns")][0] == "Workstream"
    assert facts[("Milestones", "row_count")] == 1


# ------------------------------------------------------- the seams, swept --
@pytest.mark.parametrize("text", [
    "", "   \t\n  ", "?", "??", "sheet" * 40_000,
    "I don't want another file " * 400,
    "\u0000no file",
    "‮I don't want another file‬",       # RTL override around the words
    "﻿I don't want another file",             # a BOM the paste path leaves in
])
def test_decide_never_raises_on_a_hostile_turn(text: str) -> None:
    """A seam sweep, not a defect: empty, duplicated, huge, NUL-bearing and
    bidi-wrapped input must come back as a decision, never as an exception
    on the event loop."""
    got = I.decide(text, **PC)
    assert got.action in ("create", "edit", "convert", "export", "none")
    assert isinstance(got.wants_file, bool)
