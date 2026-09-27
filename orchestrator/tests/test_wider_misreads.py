"""WIDER MISREADS — the turns the artifact gate reads as something else.

THE COMPLAINT (owner, 2026-09-27): "When it creates a document or a sheet,
why does it not understand the user? ... Not this issue only, there are
multiple issues."

The anchor case of that complaint — a QUESTION about a workbook answered by
re-rendering the workbook — is owned by another track. This file is the
sweep AROUND it: five further shapes that `intent.decide` reads as something
the person did not say, each measured on origin/dev 1f80aa3a2b, each with
the contrast case that shows the rules get the neighbouring shape right.

HOW TO READ A CASE. Every defect below is stated as two tests:

  * a `xfail(strict=True)` test asserting what the person asked for. It
    xfails on origin/dev — that IS the reproduction. When the fix lands it
    XPASSes, which `strict=True` turns into a failure, so the marker has to
    be deleted in the same commit as the fix. Nothing here is skipped and no
    assertion is softened: the wrong answer is never asserted as correct.
  * a plain test pinning the CONTRAST — the same words in the context, or
    with the one synonym, where the rules already do the right thing. Those
    pass today. They are the regression guard on the working side, and they
    are why each finding is a defect rather than a shape nobody taught.

THE FIVE, most harmful first.

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


# ----------------------------------------------------------------------- W3 --
def _pasted_rows(n: int) -> str:
    """`n` tab-separated rows, the shape a person pastes out of a
    spreadsheet. Built here rather than stored, so the case stays readable
    at ten thousand rows."""
    return "\n".join(f"EMP{i:04d}\tTeam {i % 7}\tEngineer\t{50000 + i}\tActive" for i in range(1, n + 1))


#: The ask people type after their data. 40 rows (~1.5 kB) is a file today;
#: 120 rows (~4.4 kB) is not, because `_clean(text)[:4000]` has already cut
#: the sentence off. Nothing is logged and no metric counts it.
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


# ----------------------------------------------------------------------- W4 --
@pytest.mark.parametrize("word,fmt", [("sheet", "xlsx"), ("sheets", "xlsx"), ("doc", "docx")])
def test_the_format_vocabularies_have_drifted(word: str, fmt: str) -> None:
    """The measurement behind W4, asserted as itself: `formats` knows these
    three words and `intent._FORMAT_WORD` does not. This test passes today
    and is the evidence; the two below are the consequences."""
    import re

    assert F.explicit_formats(word) == [fmt]
    assert re.compile(I._FORMAT_WORD, re.I).fullmatch(word) is None


@pytest.mark.xfail(strict=True, reason="W4: _NEGATED_FORMAT_RE is built on _FORMAT_WORD, which has no `sheet` (intent.py:172/309)")
def test_a_format_ruled_out_as_a_sheet_is_not_produced() -> None:
    """"not a sheet" must remove the workbook exactly as "not a
    spreadsheet" does. Measured: formats ['pdf', 'xlsx'] — the person is
    handed the very thing they excluded."""
    assert I.decide("make it a pdf, not a sheet", **PA).formats == ["pdf"]


def test_the_same_exclusion_spelled_spreadsheet_is_already_honoured() -> None:
    """THE CONTRAST: one synonym, and the exclusion works."""
    assert I.decide("make it a pdf, not a spreadsheet", **PA).formats == ["pdf"]


@pytest.mark.xfail(strict=True, reason="W4: `sheet` is not in _FORMAT_WORD, so _AS_FORMAT_RE sees no destination (intent.py:172/238)")
def test_an_elliptical_handover_to_a_sheet_is_a_request() -> None:
    """"all of that as a sheet" names a destination. Measured: no-request."""
    assert I.decide("all of that as a sheet", **PA).wants_file is True


def test_the_same_handover_to_a_spreadsheet_is_already_a_request() -> None:
    """THE CONTRAST: `export-elliptical` fires on the synonym."""
    assert I.decide("all of that as a spreadsheet", **PA).wants_file is True


@pytest.mark.xfail(strict=True, reason="W4: `doc` is not in _FORMAT_WORD, so the docx is lost and the answer is not the source (intent.py:172)")
def test_a_handover_to_a_doc_keeps_its_format_and_its_source() -> None:
    """"give me a doc of this" must export the answer as a docx. Measured:
    action=create with formats=[] — the format is dropped, the reference to
    the answer is dropped, and the model is asked to invent the content."""
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
@pytest.mark.xfail(strict=True, reason="W5: _FORMAT_ONLY_RE allows only seven leading words, and `meant`/`said`/`asked` are topic words to _content_words (intent.py:597/1016)")
def test_an_elliptical_correction_still_asks_for_the_file(text: str) -> None:
    """The correction names the format and nothing else. It is the same ask
    as the bare word, said by someone the product has already missed once."""
    assert I.decide(text, **PA).wants_file is True


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
