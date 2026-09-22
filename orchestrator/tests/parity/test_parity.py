"""The parity harness's own guards. Green in CI, forever, by construction.

    cd orchestrator && python -m pytest -q tests/parity

WHAT THIS FILE IS FOR, AND WHAT IT IS NOT FOR

It asserts that the SCORER is still the scorer that was calibrated, and that
the recorded baselines still score exactly what they scored. It does NOT
assert that any build is good enough -- that is `test_parity_gate.py`, which
measures the CANDIDATE runs the tracks produce against `PARITY_MIN`.

The split is deliberate and it is about incentives. `shard_tests.py`
discovers `test_*.py` at any depth under `orchestrator/tests`, so everything
here runs in a CI shard on every push. A test file that is permanently red
because today's product is not good enough yet is a pipeline that the first
person under deadline pressure repairs by lowering the bar. So nothing in
this file can be made green by softening a floor. Three guards do that
work, and they catch DIFFERENT things:

  * `test_every_floor_still_has_its_pinned_value` compares every floor in
    checklist.py against `FLOOR_VALUES` below, a mapping of floor name to
    value written out in this file. Move any floor by any amount in either
    direction and this fails, and the failure names the floor and what it
    moved from and to. This is the guard that catches a floor being softened
    QUIETLY; the last block of this docstring says what it does not do.
  * `test_every_floor_is_below_the_reference` asserts no floor sits above
    what the calibration reference achieved, so a floor RAISED past the
    reference fails here and the failure names it.
  * every frozen baseline is pinned to its EXACT score and its EXACT
    per-check pass/fail vector. This catches what the value pin cannot: a
    change in score.py's LOGIC -- a parser that starts counting a block
    differently, a check whose comparison flips -- with every floor value
    left exactly where it is.

WHAT THE BASELINE PIN DOES NOT CATCH, AND WHY THE VALUE PIN EXISTS

An exact-score pin on five recordings is NOT a general floor guard, and the
first version of this file said it was. A recording's vector only changes
when a floor CROSSES that recording's observed count. Between two
recordings' counts there is a gap, and a floor moved anywhere inside its gap
leaves all five vectors identical and this whole directory green.

Measured on the commit that added this pin, by setting each floor to every
value in turn and re-deriving all five baseline vectors plus the calibration
guards: 16 of the 17 floors had such a gap.

    SECTION_WORD_FLOOR            100   free anywhere in    76 .. 102
    TOTAL_WORDS_MIN             2,000   free anywhere in 1,725 .. 2,007
    HEADINGS_MIN                   15   free anywhere in     0 .. 16
    SECTIONS_WITH_SUBHEADING_MIN    8   free anywhere in     1 .. 13
    BULLET_LISTS_MIN                3   free anywhere in     0 .. 10
    TOTAL_WORDS_MAX             8,000   free from 2,475 upward, no ceiling
    DIAGRAM_ROLED_NODES_MIN         1   free from 0 upward, no ceiling

RECOMMENDATION_MENTIONS_MIN was the only floor with a recording on each side
of it. HEADINGS_MIN could have gone to 0 -- the check deleted in all but
name -- with every test here still green. That gap is what a later track
could have used to turn a red build green, and closing it is what
`FLOOR_VALUES` is for.

`test_moving_any_floor_off_its_pinned_value_fails_the_value_pin` is the test
of that test: it walks every floor, moves it by the smallest step that
changes it, and asserts the value pin fails and names it. Of those 34
single-step moves, 26 are invisible to the baseline pin and the calibration
guards put together.

WHAT THE VALUE PIN STILL DOES NOT DO, said plainly, because the thing it was
added to fix was a docstring that claimed more than its test delivered.

  * It does not make a floor unchangeable. Edit checklist.py AND `FLOOR_VALUES`
    in one commit and the suite is green again -- by design, because a floor
    must be changeable. What it buys is that the change cannot be quiet: it
    is two edits, in a file called a pin, and the failure text asks for the
    reason in the changelog before it goes green.
  * It does not check that the changelog line was written. Nothing here can:
    a test cannot tell a considered floor change from an unconsidered one.
    `test_the_floor_pin_changelog_is_not_empty` only keeps the list alive.
    This one is on the reviewer, which is why the procedure is in the failure
    message rather than only in this docstring.
  * It sees only floors that live in checklist.py. A comparison hard-coded as
    a literal inside score.py is invisible to it, to the calibration guard and
    to the coverage guard alike. Two such literals already had to be lifted
    out into `HEADINGS_MIN` and `RECOMMENDATION_MENTIONS_MIN` when this
    harness landed. Keep floors in checklist.py.

REGENERATING `calibration/reference_counts.json`. It is the reference's
observed counts and nothing else -- integers and one ratio, no prose, because
the reference is a third-party-generated business document that must not
enter this repository. `_observe()` below is the generator as well as the
checker: with the reference present, print `json.dumps(_observe(text))`.
`test_reference_passes_when_present` re-derives it and asserts the committed
fixture still matches, so the derived fixture cannot silently drift from the
file it was derived from.
"""
from __future__ import annotations

import json
import os
import pathlib
import re

import pytest

from . import checklist as K
from . import normalise
from . import score as SC

HERE = pathlib.Path(__file__).resolve().parent
RUNS = HERE / "runs"
CALIBRATION = HERE / "calibration" / "reference_counts.json"

#: orchestrator/tests/parity/test_parity.py -> the repository root.
#: Derived, never written out: the operator's checkout lives under a home
#: directory and a scratch path that must not be published, and a hardcoded
#: path also breaks this file for everybody else.
REPO = HERE.parents[2]

#: The calibration reference: the answer another assistant gave to the same
#: request, used ONCE, to place the floors somewhere defensible. It is NOT in
#: this repository (`.gitignore:143` excludes `backups/`) and must not be:
#: it is a third-party-generated business document and this repository is
#: public. Everything the guards need from it is in the committed fixture.
REFERENCE = pathlib.Path(
    os.environ.get("PARITY_REFERENCE") or (REPO / "backups" / "chatgptoutput.txt"))

#: Each floor, and the observed count in the fixture that calibrates it.
#: A floor must be at or BELOW what the reference achieved, or the bar is
#: "match the reference" rather than "the request was honoured".
FLOOR_CALIBRATION = {
    "SECTION_WORD_FLOOR": "min_section_words",
    "TOTAL_WORDS_MIN": "total_words",
    "HEADINGS_MIN": "headings",
    "SECTIONS_WITH_SUBHEADING_MIN": "sections_with_subheading",
    "TABLES_MIN": "tables",
    "CODE_BLOCKS_MIN": "code_fences",
    "BULLET_LISTS_MIN": "bullet_lists",
    "NUMBERED_LISTS_MIN": "numbered_lists",
    "BOLD_RUNS_MIN": "bold_runs",
    "CALLOUTS_MIN": "callouts",
    "WARNINGS_MIN": "warning_callouts",
    "RECOMMENDATION_MENTIONS_MIN": "recommendation_mentions",
    "CONTEXT_ITEMS_MIN": "context_items_used",
    "DIAGRAMS_MIN": "diagrams",
}

#: The ONE floor the reference cannot calibrate, and why. This dict is
#: pinned by `test_the_uncalibrated_floors_are_exactly_the_known_one`, so
#: adding an entry is a visible change to a reviewer rather than a quiet
#: exemption.
NOT_CALIBRATED_BY_THE_REFERENCE = {
    "DIAGRAM_ROLED_NODES_MIN": (
        "the reference draws its five diagrams as ASCII art inside ```text "
        "fences, so it carries zero mermaid node classes. It is a reference "
        "for what the PROMPT asked for, and the prompt did not ask for a "
        "diagram at all; the role vocabulary is this release's own invention "
        "and the reference predates it. Calibrating this floor against the "
        "reference would force it to 0, which is not a check."
    ),
}


def _observe(md: str) -> dict:
    """The reference's observed counts -- the generator AND the checker.

    Every value is a count the scorer itself produces, read off the scorer's
    own parser rather than off the human-readable `observed` strings, so the
    fixture cannot drift from the code that reads it.
    """
    blocks = SC.parse(md)
    secs = SC._sections(blocks)
    section_words = sorted(SC._prose_words(body) for _, _, body in secs)
    callouts = [b for b in blocks if b.kind == "quote"]
    diagrams = [b for b in blocks if b.kind == "diagram"]
    warn_re = re.compile(r"\b(warning|caution|danger|important|do not|never)\b", re.I)

    prose = "\n".join(
        b.text() for b in blocks
        if b.kind in ("paragraph", "bullets", "numbered", "quote", "table"))
    prose += "\n" + "\n".join(b.heading_text for b in blocks if b.kind == "heading")

    paragraphs = [b.text() for b in blocks
                  if b.kind == "paragraph" and len(b.text().split()) >= 25]
    shingles = [SC._shingles(p) for p in paragraphs]
    worst = 0.0
    for a in range(len(shingles)):
        for b in range(a + 1, len(shingles)):
            if shingles[a] and shingles[b]:
                worst = max(worst, len(shingles[a] & shingles[b])
                            / min(len(shingles[a]), len(shingles[b])))

    names = [SC._norm(t) for _, t, _ in secs]
    low = md.lower()
    return {
        "min_section_words": section_words[0] if section_words else 0,
        "max_section_words": section_words[-1] if section_words else 0,
        "sections": len(secs),
        "total_words": SC._prose_words(blocks),
        "headings": sum(1 for b in blocks if b.kind == "heading"),
        "sections_with_subheading": sum(
            1 for _, _, body in secs if any(b.kind == "heading" for b in body)),
        "tables": sum(1 for b in blocks if b.kind == "table"),
        "code_fences": sum(1 for b in blocks if b.kind == "code"),
        "bullet_lists": sum(1 for b in blocks if b.kind == "bullets"),
        "numbered_lists": sum(1 for b in blocks if b.kind == "numbered"),
        "bold_runs": len(SC.BOLD_RE.findall(prose)),
        "callouts": len(callouts),
        "warning_callouts": sum(1 for b in callouts if warn_re.search(b.text())),
        "recommendation_mentions": len(
            re.findall(r"\brecommend(?:ation|ations|ed|s)?\b", md, re.I)),
        "context_items_used": sum(1 for c in K.CONTEXT_ITEMS if c.lower() in low),
        "duplicate_headings": sum(1 for h in set(names) if names.count(h) > 1),
        "max_paragraph_shingle_overlap": round(worst, 4),
        "diagrams": len(diagrams),
        "roled_diagram_nodes": sum(
            len(SC.DIAGRAM_ROLE_RE.findall(b.text())) for b in diagrams),
    }


def _fixture() -> dict:
    return json.loads(CALIBRATION.read_text(encoding="utf-8"))["observed"]


# ------------------------------------------------------ the floor VALUES --
#
# The pin that actually stops a floor being softened. Everything else in
# this file is indirect: the calibration guard only bounds a floor from
# ABOVE, and the baseline pin only notices a floor that crosses one of five
# recordings' observed counts. This one compares the number to the number.

#: Every edit to `FLOOR_VALUES`, newest first: date, floor, old -> new, and
#: the reason. A floor that moved without a line here moved without a reason,
#: and a reviewer reading the diff sees the missing line.
FLOOR_PIN_CHANGELOG = [
    "2026-09-23 -- pin created. No floor moved: these are the values the "
    "harness entered the repository with at 41e7a7b. The pin was added "
    "because the baseline recordings alone left 16 of the 17 floors free to "
    "move inside a gap (HEADINGS_MIN could have reached 0) with this whole "
    "directory green.",
]

#: FLOOR NAME -> ITS VALUE, every floor in checklist.py, written out.
#:
#: Written out rather than derived, because a pin derived from the thing it
#: pins is not a pin. `test_every_floor_still_has_its_pinned_value` asserts
#: this mapping equals the live checklist attributes, and
#: `test_every_floor_in_the_checklist_is_pinned_by_value` asserts a floor
#: added to checklist.py cannot stay out of this mapping.
FLOOR_VALUES = {
    "SECTION_WORD_FLOOR": 100,
    "TOTAL_WORDS_MIN": 2_000,
    "TOTAL_WORDS_MAX": 8_000,
    "HEADINGS_MIN": 15,
    "SECTIONS_WITH_SUBHEADING_MIN": 8,
    "TABLES_MIN": 3,
    "CODE_BLOCKS_MIN": 3,
    "CALLOUTS_MIN": 2,
    "WARNINGS_MIN": 1,
    "BULLET_LISTS_MIN": 3,
    "NUMBERED_LISTS_MIN": 1,
    "BOLD_RUNS_MIN": 10,
    "RECOMMENDATION_MENTIONS_MIN": 3,
    "REPEAT_SHINGLE_OVERLAP": 0.60,
    "CONTEXT_ITEMS_MIN": 10,
    "DIAGRAMS_MIN": 1,
    "DIAGRAM_ROLED_NODES_MIN": 1,
}

#: What to do about it, quoted in the failure so nobody has to find this file
#: to learn the procedure.
MOVED_A_FLOOR = (
    "change the pin in the same commit with a one-line reason in this file's "
    "CHANGELOG list, and re-record the five baselines if a recording's "
    "pass/fail changes")

#: The smallest step that changes a floor, used by the test of the test.
#: One for a count; one hundredth for the single ratio, because +/- 1 on a
#: ratio is a mutation nobody could make by accident and would prove less.
FLOOR_STEP = {"REPEAT_SHINGLE_OVERLAP": 0.01}

_ABSENT = object()


def _floor_moves() -> list:
    """Every floor whose live value is not the pinned one. The pin's engine."""
    moves = []
    for name, want in sorted(FLOOR_VALUES.items()):
        live = getattr(K, name, _ABSENT)
        if live is _ABSENT:
            moves.append(
                f"{name} is pinned at {want!r} but no longer exists in "
                "checklist.py")
        elif type(live) is not type(want):
            # `WARNINGS_MIN = True` compares equal to 1 and would slip past a
            # bare `!=`. It scores the same today; it is still a floor that
            # changed without a reason, and the next edit to it is unreadable.
            moves.append(
                f"{name} moved {want!r} -> {live!r} "
                f"({type(want).__name__} -> {type(live).__name__})")
        elif live != want:
            moves.append(f"{name} moved {want!r} -> {live!r}")
    return moves


def test_every_floor_still_has_its_pinned_value():
    """The direct pin: floor by floor, the number against the number.

    This is the test a later track hits when it softens a floor to turn its
    own build green, and unlike the baseline pin it does not care whether any
    recording happened to sit on the far side of the move.

    It stops a SILENT change, not a change. Moving a floor deliberately means
    editing checklist.py and this pin together, and the failure says so.
    """
    moves = _floor_moves()
    assert not moves, (
        f"{len(moves)} checklist floor(s) no longer match the values pinned "
        f"in FLOOR_VALUES: " + "; ".join(moves) + ". If the move is "
        f"deliberate, {MOVED_A_FLOOR}."
    )


def test_every_floor_in_the_checklist_is_pinned_by_value():
    """A floor added to checklist.py and not to the pin is an unpinned floor.

    Deliberately broader than `test_every_floor_in_the_checklist_is_calibrated`,
    which looks only at `*_MIN` and `*_FLOOR` integers: that rule does not see
    `TOTAL_WORDS_MAX` or the `REPEAT_SHINGLE_OVERLAP` ratio, and both of them
    decide whether an answer passes. Every upper-case number in checklist.py
    is a floor for this purpose.
    """
    declared = {
        name for name in vars(K)
        if name.isupper()
        and isinstance(vars(K)[name], (int, float))
        and not isinstance(vars(K)[name], bool)
    }
    assert declared == set(FLOOR_VALUES), (
        "checklist.py's floors and FLOOR_VALUES disagree, so a floor is "
        "moving unwatched. Not pinned: "
        f"{sorted(declared - set(FLOOR_VALUES))}; pinned but no longer "
        f"declared: {sorted(set(FLOOR_VALUES) - declared)}. "
        f"To change a floor, {MOVED_A_FLOOR}."
    )


def test_the_floor_pin_changelog_is_not_empty():
    """A pin with no changelog is a pin whose procedure nobody wrote down."""
    assert FLOOR_PIN_CHANGELOG and all(
        isinstance(line, str) and line.strip() for line in FLOOR_PIN_CHANGELOG)


@pytest.mark.parametrize("direction", ("down", "up"))
@pytest.mark.parametrize("floor", sorted(FLOOR_VALUES))
def test_moving_any_floor_off_its_pinned_value_fails_the_value_pin(
        floor, direction, monkeypatch):
    """The test of the test, over EVERY floor, in BOTH directions.

    This is the verifier's sweep, kept as a test. It is the claim the module
    docstring makes, made checkable: the value pin is not selective, it does
    not depend on any recording, and its failure names the floor.

    Measured when this was written: of these same 34 single-step mutations,
    26 are INVISIBLE to the five-recording pin and the calibration guards put
    together -- including SECTION_WORD_FLOOR 100 -> 99, HEADINGS_MIN 15 -> 14
    and TOTAL_WORDS_MIN 2,000 -> 1,999. That is the whole reason this pin
    exists, and it is why this test asserts against the VALUE pin rather than
    against the suite as a whole.
    """
    step = FLOOR_STEP.get(floor, 1)
    want = FLOOR_VALUES[floor]
    moved = round(want - step, 4) if direction == "down" else round(want + step, 4)
    assert moved != want, f"the step for {floor} does not change it"
    monkeypatch.setattr(K, floor, moved)

    with pytest.raises(AssertionError) as caught:
        test_every_floor_still_has_its_pinned_value()

    message = str(caught.value)
    assert floor in message, (
        f"moving {floor} {want} -> {moved} fails the pin but the failure does "
        f"not name the floor, so the reader cannot act on it: {message}")
    assert f"{want!r} -> {moved!r}" in message, (
        f"the failure for {floor} does not say what it moved from and to: "
        f"{message}")
    assert MOVED_A_FLOOR in message, (
        f"the failure for {floor} does not tell the reader what to do about "
        f"it: {message}")


def test_a_floor_deleted_from_the_checklist_fails_the_value_pin(monkeypatch):
    """Deleting a floor is the other way to stop it failing a build."""
    monkeypatch.delattr(K, "HEADINGS_MIN")
    with pytest.raises(AssertionError) as caught:
        test_every_floor_still_has_its_pinned_value()
    assert "HEADINGS_MIN" in str(caught.value)
    assert "no longer exists" in str(caught.value)


def test_a_floor_added_to_the_checklist_fails_the_coverage_pin(monkeypatch):
    """And adding one without pinning it is how the pin rots."""
    monkeypatch.setattr(K, "A_BRAND_NEW_MIN", 7, raising=False)
    with pytest.raises(AssertionError) as caught:
        test_every_floor_in_the_checklist_is_pinned_by_value()
    assert "A_BRAND_NEW_MIN" in str(caught.value)



# ----------------------------------------------------- calibration guards --

def test_every_floor_is_below_the_reference():
    """No floor may sit above what the calibration reference achieved.

    This is the guard against the bar quietly becoming "match the other
    assistant". It runs from the committed fixture, so it works in CI where
    the reference file does not and must not exist.
    """
    observed = _fixture()
    too_high = []
    for name, key in FLOOR_CALIBRATION.items():
        floor = getattr(K, name)
        if floor > observed[key]:
            too_high.append(f"{name}={floor} > reference {key}={observed[key]}")
    assert not too_high, (
        "a floor is now ABOVE what the reference itself achieved, so the bar "
        "is no longer 'the request was honoured' but 'match the reference': "
        + "; ".join(too_high)
    )

    # The word band's CEILING is not a floor: it exists so that "pad it out"
    # is not a winning strategy, and it must not be so low that the reference
    # itself would be called padded.
    assert K.TOTAL_WORDS_MAX >= observed["total_words"], (
        f"TOTAL_WORDS_MAX={K.TOTAL_WORDS_MAX} is below the reference's own "
        f"{observed['total_words']} words, so the reference would score as padding"
    )

    # The repeat threshold is stricter as it gets LOWER, so the reference
    # calibrates it from the other side: it must stay above the worst overlap
    # the reference itself contains, or a clean answer is called repetitive.
    assert K.REPEAT_SHINGLE_OVERLAP > observed["max_paragraph_shingle_overlap"], (
        f"REPEAT_SHINGLE_OVERLAP={K.REPEAT_SHINGLE_OVERLAP} is at or below the "
        f"reference's own worst paragraph overlap "
        f"{observed['max_paragraph_shingle_overlap']}"
    )


def test_every_floor_in_the_checklist_is_calibrated():
    """A floor added without a calibration is a floor nobody has justified."""
    declared = {
        name for name in dir(K)
        if (name.endswith("_MIN") or name.endswith("_FLOOR"))
        and isinstance(getattr(K, name), int)
        and not isinstance(getattr(K, name), bool)
    }
    accounted = set(FLOOR_CALIBRATION) | set(NOT_CALIBRATED_BY_THE_REFERENCE)
    assert declared == accounted, (
        "every floor must either be calibrated against the reference or be "
        "listed, with its reason, as one the reference cannot calibrate. "
        f"uncovered: {sorted(declared - accounted)}; "
        f"listed but no longer declared: {sorted(accounted - declared)}"
    )


def test_the_uncalibrated_floors_are_exactly_the_known_one():
    """Exemptions are the way a calibration guard dies. Pin the list."""
    assert set(NOT_CALIBRATED_BY_THE_REFERENCE) == {"DIAGRAM_ROLED_NODES_MIN"}, (
        "a floor has been exempted from the reference calibration. That is "
        "allowed, but it is a decision a reviewer must see, so it changes "
        "this assertion too."
    )


def test_reference_passes_when_present():
    """With the reference in hand, score it for real: 17/17 on the prompt.

    An EXTRA assertion on top of the derived fixture, never the only one and
    never a skip -- `test_every_floor_is_below_the_reference` stands alone in
    CI. What this adds is proof that the committed fixture is still a true
    reading of the file it was derived from.
    """
    if not REFERENCE.is_file():
        # Not a skip: the two assertions above already did the calibration
        # work from the committed fixture. This records, in the run's own
        # output, that the stronger check did not get to run.
        print(f"[parity] reference not present at {REFERENCE}; "
              "calibration asserted from calibration/reference_counts.json alone")
        return

    text = REFERENCE.read_text(encoding="utf-8")
    assert _observe(text) == _fixture(), (
        "the committed calibration fixture no longer matches the reference it "
        "was derived from; either the reference changed or the scorer did"
    )
    s = SC.score(text, "reference")
    failed = [(r.check_id, r.observed) for r in s.prompt_results if not r.passed]
    assert not failed, (
        "the reference no longer clears every prompt check, so a floor is "
        f"above the reference and the bar is not defensible: {failed}")
    assert s.passed == 17 and s.total == 17


def test_normaliser_reads_every_block():
    """No document block type may be silently dropped by the normaliser.

    The document schema's block union is read at RUNTIME from the app, not
    written out by hand here, so a block type added to the vocabulary and not
    to the normaliser fails this test instead of quietly scoring as nothing.
    This is the seam that stops a new block being judged on content the
    scorer never saw.

    There is deliberately no skip. This file lives inside the orchestrator
    package; if `app.artifacts.spec` cannot be imported, the import error is
    the correct outcome, because a calibration guard that can skip is a
    calibration guard that will.
    """
    from app.artifacts import spec as S

    schema = json.dumps(S.schema_for("document"))
    in_schema = set(re.findall(
        r'"type"\s*:\s*\{\s*"const"\s*:\s*"([a-z_]+)"', schema))
    assert in_schema, "could not read the block types out of the document schema"
    missing = in_schema - normalise.KNOWN_BLOCKS
    assert not missing, (
        f"block type(s) {sorted(missing)} exist in the document schema but the "
        "normaliser has no Markdown form for them; every scored answer that "
        "used one would be judged on content the scorer never saw"
    )


def test_empty_table_is_not_counted_as_a_table():
    """A nested payload must be READ, and an empty one must not be credited.

    The real bug this eval hit on its first pass: a `TableBlock` nests its
    payload under `"table"` while every other block keeps its fields at the
    top level, so reading `block["columns"]` scored six real tables as zero.
    The `diagram` block nests the same way, for the same reason, so it is
    pinned here beside the table rather than in a test of its own.
    """
    def doc(*blocks):
        return {"kind": "document", "document": {
            "title": "T",
            "blocks": [{"type": "heading", "level": 1, "text": "S"}, *blocks]}}

    full_table = {"type": "table", "table": {
        "columns": ["A", "B"], "rows": [["1", "2"]], "caption": ""}}
    empty_table = {"type": "table", "table": {
        "columns": [], "rows": [], "caption": ""}}
    assert SC.score(normalise.spec_to_markdown(doc(full_table)), "f").stats["tables"] == 1
    assert SC.score(normalise.spec_to_markdown(doc(empty_table)), "e").stats["tables"] == 0

    full_diagram = {"type": "diagram", "diagram": {
        "title": "Architecture", "direction": "LR",
        "nodes": [{"id": "A", "label": "Frontend", "kind": "service"},
                  {"id": "B", "label": "Database", "kind": "store"}],
        "edges": [{"source": "A", "target": "B", "label": "reads", "style": "solid"}],
        "caption": ""}}
    empty_diagram = {"type": "diagram", "diagram": {
        "title": "", "direction": "TD", "nodes": [], "edges": [], "caption": ""}}
    got = SC.score(normalise.spec_to_markdown(doc(full_diagram)), "d")
    assert got.stats["diagrams"] == 1
    assert got.stats["code_fences"] == 0, "a diagram fence is not a code block"
    assert {r.check_id: r.passed for r in got.results}["diagram_roles"] is True
    assert SC.score(normalise.spec_to_markdown(doc(empty_diagram)), "d0").stats["diagrams"] == 0

    code = {"type": "code", "language": "bash", "text": "echo hello", "caption": ""}
    coded = SC.score(normalise.spec_to_markdown(doc(code)), "c")
    assert coded.stats["code_fences"] == 1
    assert coded.stats["diagrams"] == 0, "a code fence is not a diagram"


#: 16-, 32- and 64-character lowercase hex runs are the shapes this system's
#: live handles take: `file_id`, `artifact_id` and `sha256`. A dashed UUID is
#: the shape a conversation id takes. None of them is anything the scorer
#: reads -- it reads blocks and warnings -- and all of them are live handles
#: into a real person's content on a public-facing deployment. Committing one
#: publishes it permanently, in git history, in a public repository.
IDENTIFIER_SHAPED = re.compile(
    r"\b[0-9a-f]{16}\b|\b[0-9a-f]{32}\b|\b[0-9a-f]{64}\b"
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


def test_no_identifier_shaped_literals_in_runs():
    """Recordings carry answers, never handles.

    Checked by a test AND by hand before the commit, because this project's
    own history records that gitleaks run from a worktree scans nothing --
    so the secret gate is not the thing standing between a live identifier
    and a public repository. This is.
    """
    offenders = []
    for path in sorted(RUNS.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            for hit in IDENTIFIER_SHAPED.findall(line):
                offenders.append(f"{path.relative_to(HERE)}:{n}: {hit}")
    assert not offenders, (
        "identifier-shaped literals in a recorded run. A recording is scored "
        "on its blocks and its warnings; an artifact id, a file id, a sha256 "
        "or a conversation id is a live handle and this repository is public: "
        + "; ".join(offenders[:10])
    )


# ------------------------------------------------- the frozen baselines --

BASELINES = RUNS / "BASELINE_SCORES.json"

#: A sentinel parameter, so that an unreadable pin file is a NAMED test
#: failure rather than a collection error. A collection error takes the whole
#: file down, including the calibration guards, and reports it as a crash
#: instead of as the one thing that is wrong.
NO_BASELINES = "<runs/BASELINE_SCORES.json is missing or unreadable>"


def _baselines() -> dict:
    return json.loads(BASELINES.read_text(encoding="utf-8"))


def _baseline_names() -> list:
    try:
        return sorted(_baselines()["runs"]) or [NO_BASELINES]
    except (OSError, ValueError, KeyError):
        return [NO_BASELINES]


@pytest.mark.parametrize("name", _baseline_names())
def test_a_frozen_baseline_still_scores_exactly_what_it_scored(name):
    """An EXACT pin on five recordings. It catches SCORER DRIFT.

    What it catches, and catches better than anything else here: score.py's
    LOGIC changing underneath fixed floors. A parser that starts counting a
    nested table, a warning regex that widens, a comparison that flips from
    `>=` to `>` -- every floor still at its pinned value, and one of these
    five vectors moves. `FLOOR_VALUES` cannot see any of that.

    What it does NOT catch, and what this docstring claimed it did until the
    value pin was added: a floor move in general. A recording's vector only
    changes when a floor crosses THAT recording's observed count, so between
    two recordings' counts every floor has a gap it can be moved inside with
    this test green -- 26 values wide for SECTION_WORD_FLOOR, 282 for
    TOTAL_WORDS_MIN, and all the way down to 0 for HEADINGS_MIN. The module
    docstring has the measured table. Floor VALUES are pinned by
    `test_every_floor_still_has_its_pinned_value`; that is the guard against
    softening, and this one is not a substitute for it.

    The five numbers -- 10, 10, 11, 12, 14 -- are the proof that the harness
    came into the repository without the SCORER being softened.
    """
    assert name != NO_BASELINES, (
        f"{BASELINES} could not be read, so no baseline is pinned at all and "
        "the whole anti-softening guard is inert")
    recorded = _baselines()["runs"][name]
    path = RUNS / name
    assert path.is_file(), (
        f"{name} is pinned in BASELINE_SCORES.json but is not in runs/. A "
        "baseline is evidence; deleting it is how a pin gets dodged.")

    s = SC.score(normalise.load(path), name)
    got = {r.check_id: r.passed for r in s.results}
    want = recorded["checks"]

    # The VOCABULARY first. A check id that disappeared would otherwise be
    # reported as "was True, is now None" while the message builder looked
    # for an observation no result carries, and the reader would get a
    # StopIteration instead of the one fact that matters.
    assert set(got) == set(want), (
        f"{name}: the checklist's check ids changed "
        f"(added {sorted(set(got) - set(want))}, "
        f"removed {sorted(set(want) - set(got))}). A baseline pins a vector; "
        "changing the vocabulary means re-freezing every baseline in one commit."
    )

    observed = {r.check_id: r.observed for r in s.results}
    moved = sorted(k for k in want if got[k] != want[k])
    assert not moved, (
        f"{name}: {len(moved)} check(s) changed verdict since this baseline "
        f"was frozen: "
        + "; ".join(f"{k} was {want[k]}, is now {got[k]} ({observed[k]})"
                    for k in moved)
    )
    assert s.passed == recorded["passed"], (
        f"{name} scored {s.passed}/{s.total}, frozen at {recorded['passed']}")
    assert s.total == recorded["total"]


def test_every_recording_is_pinned():
    """A recording in runs/ that no baseline pins is a recording nobody scores."""
    assert BASELINES.is_file(), (
        "runs/BASELINE_SCORES.json is gone, so nothing is pinned and the "
        "whole anti-softening guard is inert")
    scorable = {
        p.name for p in RUNS.iterdir()
        if p.is_file() and p.suffix in (".json", ".md")
        and not p.name.endswith(".meta.json")
        and p.name != "BASELINE_SCORES.json"
    }
    assert scorable == set(_baselines()["runs"]), (
        "runs/ and BASELINE_SCORES.json disagree. Unpinned: "
        f"{sorted(scorable - set(_baselines()['runs']))}; pinned but absent: "
        f"{sorted(set(_baselines()['runs']) - scorable)}"
    )
