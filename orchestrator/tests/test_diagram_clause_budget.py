"""DIAGRAM_CLAUSE is a prompt tax on every composed document, so its size is
a test — the same rule `test_diagram_instruction_budget.py` sets for the
chat side, applied to the file side.

THE NUMBER THIS BRANCH RECORDED WAS WRONG IN UNIT AND IN SCOPE. Commit
63401aa's message gave the cost as a BYTE count of 251, paid once per call.
Re-measured 2026-09-28 by assembling the real
prompts and removing the clause from them IN MEMORY, so both columns are
the same tree and no cross-tree diff can drift:

    the clause itself              251 CHARACTERS, 253 UTF-8 bytes
                                   (one em dash, U+2014, at index 78)
    whole-document system prompt   +253 characters, on every template
    named-sections user message    +253 characters
    per-section system prompt      +506 characters — the clause is said
                                   TWICE there, on purpose
    a 15-section report            15 x 506 = 7,590 characters, and up to
                                   (15 + SECTION_EXTEND_MAX) x 506 = 9,108

So 251 is the CHARACTER count and not the byte count, and "once per call"
holds only on the whole-document route. The route a long technical report
actually takes is the sectioned one, which pays 506 per section.

THIS IS THE SECOND TIME THIS SUBSYSTEM HAS MADE THE CHARACTER/BYTE MISTAKE.
`test_diagram_instruction_budget.py` exists because the same error was made
one commit earlier about the chat instruction (1,082 given as bytes before
and after, which was the character count wearing a byte's name), and it is right
about why that matters: "a guard that names the wrong unit is a guard that
will mislead the next person to raise it". Writing the corrected numbers
down is not enough on its own — prose has no gate — so they are pinned
here, each against its own re-measured value.

WHY A SEPARATE FILE FROM THE CHAT ONE. The two strings have nothing to do
with each other: `DIAGRAM_INSTRUCTION` is concatenated at eleven CHAT call
sites at every effort, and `compose.py` never imports it (that file asserts
so). A tax on Fast chat latency and a tax on a composed document are
different decisions with different owners.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import llm
from app.artifacts import compose as C
from app.artifacts import types as T

#: `len()` of a `str` is CHARACTERS.
CLAUSE_CHARS = 251
#: The same string in UTF-8. It is NOT the same number: one em dash.
CLAUSE_UTF8_BYTES = 253

#: What the clause adds to each assembled prompt, measured by removing it
#: from that prompt rather than by comparing trees.
WHOLE_DOCUMENT_SYSTEM = 253
NAMED_SECTIONS_USER = 253
#: Twice the clause: `_material_messages` carries it through
#: `_KIND_GUIDE['document']` and `_write_one_section` appends it again.
#: Deliberate — see the DIAGRAM_CLAUSE comment block — and this is its price.
PER_SECTION_SYSTEM = 506
PER_SECTION_COPIES = 2


def _req(template_id="technical_report", instruction="Write a technical report on the retrieval architecture."):
    return C.ComposeRequest(kind="document", formats=["pdf"], template_id=template_id, effort="think",
                            instruction=instruction, material=C.Material(instruction=instruction))


def _without_clause(text: str) -> str:
    """The same prompt with the clause taken out, joiner and all."""
    return text.replace(C.DIAGRAM_CLAUSE + ", ", "").replace(C.DIAGRAM_CLAUSE, "")


def _cost(text: str) -> int:
    return len(text) - len(_without_clause(text))


def _section_system(req) -> str:
    seen: list = []

    async def fake(messages, **kw):
        seen.append(messages[0]["content"])
        return json.dumps({"blocks": [{"type": "heading", "level": 1, "text": "Architecture"},
                                      {"type": "paragraph", "text": "Two nodes."}]})

    real = llm.json_completion
    llm.json_completion = fake
    try:
        asyncio.run(C._write_one_section(
            req, T.EFFORT_BUDGETS["think"], C.target_for(req),
            {"title": "T", "sections": []},
            {"heading": "Architecture", "purpose": "how the parts connect",
             "elements": ["paragraphs", "diagram"]},
            written=[], words=400, position=(1, 8)))
    finally:
        llm.json_completion = real
    assert seen, "the section writer did not reach the model"
    return seen[0]


def test_the_clause_is_measured_in_the_unit_it_names():
    """Both units, each against what it really is, so neither can be quoted
    as the other again."""
    chars = len(C.DIAGRAM_CLAUSE)
    encoded = len(C.DIAGRAM_CLAUSE.encode("utf-8"))
    assert chars == CLAUSE_CHARS, f"the clause is {chars} characters, not {CLAUSE_CHARS}"
    assert encoded == CLAUSE_UTF8_BYTES, f"the clause is {encoded} UTF-8 bytes, not {CLAUSE_UTF8_BYTES}"
    assert encoded != chars, "this string carries non-ASCII, so the two units are not interchangeable"
    non_ascii = [(i, ch) for i, ch in enumerate(C.DIAGRAM_CLAUSE) if ord(ch) > 127]
    assert non_ascii == [(78, "—")], f"the +2 bytes is one em dash at index 78; found {non_ascii}"


@pytest.mark.parametrize("template_id", ["generic", "technical_report", "executive_report"])
def test_the_whole_document_prompt_pays_it_once(template_id):
    req = _req(template_id)
    system = C._material_messages(req, budget=T.EFFORT_BUDGETS["think"], target=C.target_for(req))[0]["content"]
    assert _cost(system) == WHOLE_DOCUMENT_SYSTEM, (
        f"{template_id}: the clause costs {_cost(system)} characters in the whole-document system prompt, "
        f"not {WHOLE_DOCUMENT_SYSTEM}")
    assert system.count(C.DIAGRAM_CLAUSE) == 1


def test_the_named_sections_message_pays_it_once():
    req = _req()
    user = C._material_messages(req, budget=T.EFFORT_BUDGETS["think"], target=C.target_for(req),
                                requested=["Executive Summary", "Architecture", "Data path"])[1]["content"]
    assert _cost(user) == NAMED_SECTIONS_USER
    assert user.count(C.DIAGRAM_CLAUSE) == 1


def test_the_sectioned_route_pays_it_twice_per_section():
    """THE CORRECTION TO "once per call". The sectioned route is the one a
    long technical report takes (`target.words > SECTIONED_WRITER_WORDS`),
    and its system message is `_material_messages` — which already carries
    the clause — plus the scoped append, which says it again."""
    system = _section_system(_req())
    assert system.count(C.DIAGRAM_CLAUSE) == PER_SECTION_COPIES, (
        f"the per-section prompt says the clause {system.count(C.DIAGRAM_CLAUSE)} times, not {PER_SECTION_COPIES}; "
        "if that is intended, re-measure the per-section cost below before moving it")
    assert _cost(system) == PER_SECTION_SYSTEM, (
        f"the clause costs {_cost(system)} characters per section call, not {PER_SECTION_SYSTEM}")


def test_the_cost_of_the_report_the_owner_asks_for():
    """The number that actually decides whether this clause is affordable:
    not 251 of anything, but what fifteen sections and their extension calls
    pay between them."""
    per = _cost(_section_system(_req()))
    assert per * 15 == 7_590
    assert per * (15 + C.SECTION_EXTEND_MAX) == 9_108
    assert C.SECTION_EXTEND_MAX == 3, "the worst case above was measured against three extension calls"


def test_the_withdrawn_byte_claim_is_not_restated_in_the_composer():
    """The precedent this subsystem set: the correction is made where a
    reader meets the number, not only in the test that found it. Reads the
    file, because a prose claim has no other gate."""
    text = (Path(__file__).resolve().parents[1] / "app" / "artifacts" / "compose.py").read_text(encoding="utf-8")
    for wrong in ("251 bytes", "251 BYTES"):
        offenders = [line.strip() for line in text.splitlines() if wrong in line]
        assert not offenders, f"compose.py still states the withdrawn claim {wrong!r}: {offenders}"
    assert "251 CHARACTERS" in text, "compose.py should name the unit that is actually 251"
    assert "253 UTF-8 bytes" in text, "compose.py should carry the re-measured byte count"
    assert "once per call" in text, "the scope correction should say what it is correcting"


def test_the_composer_still_does_not_import_the_chat_instruction():
    """The two budgets stay separate. `test_diagram_instruction_budget.py`
    asserts the same thing from its side; repeated here so neither file can
    be deleted without the other noticing."""
    text = (Path(__file__).resolve().parents[1] / "app" / "artifacts" / "compose.py").read_text(encoding="utf-8")
    assert "DIAGRAM_INSTRUCTION" not in text


def test_a_deck_and_a_workbook_pay_nothing():
    """The cost above is a DOCUMENT cost. A kind that cannot hold a diagram
    must not be charged for one — which is also the gate on the leak
    `_template_guide` closes."""
    for kind in ("presentation", "workbook"):
        req = C.ComposeRequest(kind=kind, formats=["pdf"], template_id="technical_report", effort="think",
                               instruction="Write about the services.",
                               material=C.Material(instruction="Write about the services."))
        system, user = (m["content"] for m in C._material_messages(
            req, budget=T.EFFORT_BUDGETS["think"], target=C.target_for(req), requested=["Architecture"]))
        assert _cost(system) == 0 and _cost(user) == 0, kind
