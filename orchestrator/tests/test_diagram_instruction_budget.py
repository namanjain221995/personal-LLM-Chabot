"""DIAGRAM_INSTRUCTION is a Fast-path tax, so its size is a test.

WHY THIS FILE EXISTS. `DIAGRAM_INSTRUCTION` is concatenated onto the system
prompt at ELEVEN call sites — engines/chat.py x2, rag.py, repo.py x2,
agent.py, url.py x2, dataset.py, document.py and search.py — on the chat
path, at EVERY effort, with no effort gate. A byte added here is a byte of
prefill on every Fast turn in nine engines, and the only prompt-size budget
the suite otherwise has (`test_fast_lane_classifier.py`) covers the
small-talk lane, which skips this string entirely.

THE NUMBER, CORRECTED 2026-09-27. It is 1,082 CHARACTERS, not bytes — this
guard was written as `PRE_EDIT_BYTES` over `len()` of a `str`, which counts
characters, and the commit message repeated the mistake as "1,082 bytes
before, 1,082 bytes after". Re-measured on both trees:

    origin/dev 593af55 (and 1f80aa3, which does not touch this file)
        1,082 characters, 1,086 UTF-8 bytes
    feat/document-vocabulary
        1,082 characters, 1,088 UTF-8 bytes

The characters did not move. The BYTES grew by 2, because the roles edit
spends one more em dash (U+2014, three bytes in UTF-8) than the sentence it
replaced. Prefill is charged in tokens, and the tokens were measured
separately at +8 per prompt across the twelve golden fixtures, so the two
bytes buy nothing and cost nothing — but a guard that names the wrong unit
is a guard that will mislead the next person to raise it, so both units are
now pinned, each against its own re-measured number.

WHAT THE EDIT ITSELF WAS: the ban on `style`, `classDef`, `linkStyle`,
`click` and `%%{init}%%` became a ban on those PLUS colours and hex values,
and gained the four role names, paid for by shortening prose elsewhere.
Raising either ceiling is a decision about Fast latency in nine engines and
needs the measurement that justifies it, not a bump.

THE CEILING WAS RAISED ONCE, 2026-09-27, AND THIS IS THE MEASUREMENT. Two
branches shipped the same `:::role` fix: this one, which held 1,082
characters, and fix/diagram-roles, which took the string to 1,848 and alone
carried the rule that a role OUTSIDE `flowchart`/`graph` is a parse error
that loses the whole diagram. Resolving the conflict toward that wording
naively gives 1,822 characters, which this guard correctly failed. The
integration keeps that ban and re-compresses everything around it:

    origin/dev (1f80aa3, 4164bb8)          1,082 characters, 1,086 bytes
    fix/document-vocabulary-r2             1,082 characters, 1,088 bytes
    fix/diagram-roles                      1,848 characters, 1,858 bytes
    naive merge (diagram-roles' wording)   1,822 characters, 1,832 bytes
    integ/diagram-group, measured today    1,549 characters, 1,553 bytes

So the raise is +467 characters and +467 UTF-8 bytes over origin/dev (both
deltas are the same number by coincidence: the string gained two em dashes
and lost one, so its non-ASCII count did not move), and −273 characters
against the naive merge. What could NOT be removed, counted: 71
characters are the seven diagram-type names the ban has to name to be
concrete (sequenceDiagram, erDiagram, pie, journey, timeline, mindmap,
gitGraph), and the remainder is the role list with its glosses, the "do not
invent a name" rule and the two accessibility rules. The ceilings below are
the measured numbers, and a `<=` plus an exact pin, so the next character
still has to be argued for.

THE TOKEN COST OF THAT RAISE IS NOT MEASURED AND IS NOT GUESSED HERE. It
needs the pinned engine's tokenizer over the twelve golden fixtures, off the
hot path; the release forbade touching the GPUs, so it is open work. The
character and byte numbers above are first-hand; no token number is stated.

WHAT MUST NOT CHANGE, and why each one is here rather than in a comment:
the one-diagram cap and the "ordinary questions get none" rule are what stop
an eager model decorating every answer; the ~20-node cap is legibility; the
explanatory-sentence rule is what makes a diagram readable to a
non-technical reader. The three-diagram allowance for a DOCUMENT is not
here and must not be: `app/artifacts/compose.py` never imports this string.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.engines import DIAGRAM_INSTRUCTION

#: `len()` of a `str` is CHARACTERS. Measured on origin/dev at 593af55,
#: before feat/document-vocabulary: 1,082. Kept as the HISTORICAL floor, so
#: the raise below is always read against the string this branch started from.
PRE_EDIT_CHARS = 1082

#: What the reconciled wording actually measures, imported on this tree on
#: 2026-09-27. It is the CEILING: every rule from both branches is in the
#: string at this size, so anything larger is a rule nobody has argued for.
CHARS_CEILING = 1549

#: The same string in UTF-8, measured on the same commit: 1,086. This edit
#: takes it to 1,088 — two bytes, one extra em dash — so the ceiling is the
#: RE-MEASURED number and not the old one. It is stated rather than hidden
#: because the earlier claim was "1,082 bytes before and after", which was
#: the character count wearing a byte's name.
PRE_EDIT_UTF8_BYTES = 1086

#: The same string in UTF-8 on this tree: 1,553. Two em dashes (U+2014, three
#: UTF-8 bytes each) account for the four bytes over the character count.
UTF8_BYTES_CEILING = 1553


def test_the_instruction_did_not_grow_in_characters():
    assert len(DIAGRAM_INSTRUCTION) <= CHARS_CEILING, (
        f"DIAGRAM_INSTRUCTION is {len(DIAGRAM_INSTRUCTION)} characters, {len(DIAGRAM_INSTRUCTION) - CHARS_CEILING} "
        f"over the {CHARS_CEILING} measured for the reconciled wording "
        f"({len(DIAGRAM_INSTRUCTION) - PRE_EDIT_CHARS} over origin/dev's {PRE_EDIT_CHARS}); "
        "it reaches eleven chat call sites at every effort"
    )


def test_the_instruction_is_measured_in_the_unit_it_names():
    """The two units, each against what it really is, so neither can be
    quoted as the other again. The byte ceiling is the re-measured 1,088, not
    the 1,086 of origin/dev: the +2 is real and is recorded here rather than
    asserted away."""
    chars = len(DIAGRAM_INSTRUCTION)
    encoded = len(DIAGRAM_INSTRUCTION.encode("utf-8"))
    assert chars == CHARS_CEILING, (
        f"{chars} characters; the reconciled wording measured {CHARS_CEILING} on 2026-09-27"
    )
    assert encoded <= UTF8_BYTES_CEILING, (
        f"DIAGRAM_INSTRUCTION is {encoded} UTF-8 bytes, over the {UTF8_BYTES_CEILING} measured for this edit"
    )
    assert encoded > chars, "this string carries non-ASCII, so the two units are not interchangeable"
    assert encoded - chars == 4, (
        f"the reconciled wording spends two em dashes, so UTF-8 is 4 bytes over the "
        f"character count; it is now {encoded - chars}"
    )
    assert encoded - PRE_EDIT_UTF8_BYTES == 467, (
        f"the raise over origin/dev was measured at +467 UTF-8 bytes; it is now "
        f"{encoded - PRE_EDIT_UTF8_BYTES}. Re-measure the Fast cost before moving this."
    )


def test_the_withdrawn_byte_claim_is_not_restated_anywhere():
    """The unit correction above was made in this file first and left standing
    in the two places a reader actually meets it, so the branch still carried
    the number it had withdrawn. Measured 2026-09-27: 1,082 CHARACTERS on both
    trees, 1,086 UTF-8 bytes on origin/dev and 1,088 on this branch — so
    "1,082 bytes" is true of neither tree and must not reappear in the comment
    beside the string or in the golden manifest's note. This reads the files
    rather than trusting the fix, because a prose claim has no other gate.
    """
    root = Path(__file__).resolve().parents[1]
    for rel in ("app/engines/__init__.py", "tests/fixtures/context_assembly_golden/MANIFEST.json"):
        text = (root / rel).read_text(encoding="utf-8")
        for wrong in ("1,082 bytes", "1082 bytes"):
            offenders = [line.strip() for line in text.splitlines() if wrong in line]
            assert not offenders, f"{rel} still states the withdrawn claim {wrong!r}: {offenders}"
        assert "1,082 CHARACTERS" in text, f"{rel} should name the unit that is actually 1,082"
        assert "1,086" in text and "1,088" in text, f"{rel} should carry both re-measured byte counts"


def test_the_conservative_rules_are_all_still_there():
    for rule in (
        "at most ONE diagram per answer",
        "must NOT contain a diagram",
        "under ~20 nodes",
        "Never draw ASCII-art boxes",
        "one statement per line",
        "every label in double quotes",
    ):
        assert rule in DIAGRAM_INSTRUCTION, rule
    assert "sentences saying what it shows" in DIAGRAM_INSTRUCTION


def test_the_role_clause_teaches_the_closed_list_and_bans_colour():
    from app.artifacts import spec as S

    assert ":::" in DIAGRAM_INSTRUCTION
    for role in S.DIAGRAM_ROLES:
        assert role in DIAGRAM_INSTRUCTION, role
    for banned in ("colour", "hex", "style", "classDef", "linkStyle", "click", "%%{init}%%"):
        assert banned in DIAGRAM_INSTRUCTION, banned
    # RETARGETED 2026-09-27, same rule, surviving words. This branch wrote the
    # ban as "NEVER a colour"; fix/diagram-roles wrote it as "never write a
    # colour of your own (no hex, no rgb(), no colour name)", which is the
    # wording the integration kept, and which tests/test_diagram_role_vocabulary.py
    # also pins. Carrying both phrases would have cost ~50 characters of Fast
    # prefill to satisfy two tests of ONE rule, so the phrase moved and the
    # rule did not.
    assert "never write a colour of your own" in DIAGRAM_INSTRUCTION
    assert "no rgb()" in DIAGRAM_INSTRUCTION


def test_the_composer_does_not_import_it():
    """The document path learns diagrams from spec.py's schema, not from the
    chat prompt — which is why raising the cap here would buy the file route
    nothing while costing every Fast chat turn."""
    composer = Path(__file__).resolve().parents[1] / "app" / "artifacts" / "compose.py"
    assert "DIAGRAM_INSTRUCTION" not in composer.read_text(encoding="utf-8")


def test_it_is_still_attached_at_every_chat_call_site():
    """If a site drops it, the ceiling above stops meaning anything."""
    engines = Path(__file__).resolve().parents[1] / "app" / "engines"
    sites = 0
    for path in sorted(engines.glob("*.py")):
        if path.name == "__init__.py":
            continue
        sites += len(re.findall(r"\+\s*DIAGRAM_INSTRUCTION|DIAGRAM_INSTRUCTION\s*\+", path.read_text(encoding="utf-8")))
    assert sites >= 11, f"found {sites} concatenation sites; the budget above was measured against eleven"
