"""DIAGRAM_INSTRUCTION is a Fast-path tax, so its size is a test.

WHY THIS FILE EXISTS. `DIAGRAM_INSTRUCTION` is concatenated onto the system
prompt at ELEVEN call sites — engines/chat.py x2, rag.py, repo.py x2,
agent.py, url.py x2, dataset.py, document.py and search.py — on the chat
path, at EVERY effort, with no effort gate. A byte added here is a byte of
prefill on every Fast turn in nine engines, and the only prompt-size budget
the suite otherwise has (`test_fast_lane_classifier.py`) covers the
small-talk lane, which skips this string entirely.

THE NUMBER. 1,082 bytes, measured on origin/dev at 593af55 before the roles
edit of 2026-09-22 and unchanged by it: the ban on `style`, `classDef`,
`linkStyle`, `click` and `%%{init}%%` became a ban on those PLUS colours and
hex values, and gained the four role names, paid for by shortening prose
elsewhere. Raising this ceiling is a decision about Fast latency in nine
engines and needs the measurement that justifies it, not a bump.

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

#: origin/dev 593af55, before feat/document-vocabulary.
PRE_EDIT_BYTES = 1082


def test_the_instruction_did_not_grow():
    assert len(DIAGRAM_INSTRUCTION) <= PRE_EDIT_BYTES, (
        f"DIAGRAM_INSTRUCTION is {len(DIAGRAM_INSTRUCTION)} bytes, {len(DIAGRAM_INSTRUCTION) - PRE_EDIT_BYTES} more "
        "than before; it reaches eleven chat call sites at every effort"
    )


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
    assert "NEVER a colour" in DIAGRAM_INSTRUCTION


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
