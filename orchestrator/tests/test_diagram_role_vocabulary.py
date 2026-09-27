"""The chat prompt may ask for a diagram ROLE, and only for one we can paint.

The owner's report was "our AI diagrams are grey". Both halves of the colour
were already built and they could not meet: the frontend has carried a
four-colour role palette since 2026-09-22 (`DIAGRAM_ROLES` in
frontend/lib/mermaidTheme.ts, paints in frontend/app/globals.css), and
`DIAGRAM_INSTRUCTION` banned `classDef` while never mentioning the `:::` form
that reaches the palette. No answer could emit a role, so every chat diagram
rendered on the one default node fill.

Measured 2026-09-27 in Chromium 153 / mermaid 11.17, against an esbuild bundle
of the real <MermaidBlock> in a 702 px chat column: the five-node flowchart
without roles painted every node fill rgb(51,56,61) stroke rgb(139,148,158) in
dark and fill rgb(228,231,234) stroke rgb(107,115,123) in light. The same five
nodes with roles painted rgb(34,48,63)/rgb(47,111,178) service,
rgb(64,50,30)/rgb(183,121,31) store, rgb(54,44,78)/rgb(139,92,246) model and
rgb(70,41,52)/rgb(213,81,129) external, ink rgb(236,236,236) / rgb(13,13,13).

What this file pins is the CONTRACT, which is what a prompt can be tested on:

* the instruction offers the `:::` form and exactly the four names,
* it never offers a fifth name — one the frontend has no classDef for would
  render as an uncoloured node, i.e. the defect coming back one node at a time,
* the colour ban it replaces is still there, verbatim,
* the two accessibility rules are stated: the label carries the meaning, and
  roles stay inside the diagram types where they are legal (outside them a role
  is a parse error and the reader gets no diagram at all),
* the vocabulary is the SAME four names on both sides of the app, checked
  against the frontend's list, its CSS tokens in BOTH themes, and the parity
  harness's copy.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.engines import DIAGRAM_INSTRUCTION, DIAGRAM_ROLES

#: Repository root, from this file: orchestrator/tests/x.py -> repo.
REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"


def _mermaid_theme() -> str:
    return (FRONTEND / "lib" / "mermaidTheme.ts").read_text(encoding="utf-8")


def test_the_instruction_offers_the_role_form_and_exactly_the_four_names():
    # The `:::` form is the whole point: it is what the frontend turns into a
    # classDef. Before 2026-09-27 this assertion failed on every role.
    for role in DIAGRAM_ROLES:
        assert f":::{role}" in DIAGRAM_INSTRUCTION or role in DIAGRAM_INSTRUCTION, role
    assert ":::" in DIAGRAM_INSTRUCTION
    assert 'A["Payments API"]:::service' in DIAGRAM_INSTRUCTION

    # And nothing else may be offered with it. A name the frontend has no
    # classDef for paints the default node -- grey, which is the bug.
    offered = set(re.findall(r":::([A-Za-z][\w-]*)", DIAGRAM_INSTRUCTION))
    assert offered <= set(DIAGRAM_ROLES), offered


def test_the_colour_ban_the_roles_ride_on_is_still_there():
    # A role is a NAME. The ban is on values, and letting one in would undo
    # both themes: `style A fill:#ff0000` wins over our classDef (measured,
    # 2026-09-23) and the frontend has to strip it in code.
    assert "NEVER use style, classDef, linkStyle, click" in DIAGRAM_INSTRUCTION
    assert "%%{init}%%" in DIAGRAM_INSTRUCTION
    assert "never write a colour of your own" in DIAGRAM_INSTRUCTION
    assert "no hex" in DIAGRAM_INSTRUCTION and "no rgb()" in DIAGRAM_INSTRUCTION
    # No colour literal may be taught by example, either.
    assert not re.search(r"#[0-9a-fA-F]{6}\b", DIAGRAM_INSTRUCTION)


def test_the_instruction_states_both_accessibility_rules():
    # 1. The label carries the meaning: a role is decoration on top of it, so
    #    two nodes must never differ by colour alone. The four role fills also
    #    repeat -- two `external` nodes are the same colour by design.
    assert "LABEL carries the meaning" in DIAGRAM_INSTRUCTION
    assert "never differ by colour alone" in DIAGRAM_INSTRUCTION
    assert "cannot see colour" in DIAGRAM_INSTRUCTION

    # 2. Roles belong to the diagram types that can paint them. Measured
    #    2026-09-27: `U:::external` in a sequenceDiagram is "Parse error on
    #    line 6 ... got 'TXT'" and the whole diagram is replaced by the error
    #    card -- strictly worse than grey, so the prompt names the types.
    assert "`flowchart`/`graph`" in DIAGRAM_INSTRUCTION
    for other in ("sequenceDiagram", "erDiagram", "pie", "journey", "timeline",
                  "mindmap", "gitGraph"):
        assert other in DIAGRAM_INSTRUCTION, other
    assert "syntax error" in DIAGRAM_INSTRUCTION


def test_the_vocabulary_is_the_frontend_s_vocabulary():
    # The frontend's list is the one that becomes a classDef. If the two drift,
    # a schema-valid diagram carries a class nobody defined and the node loses
    # its colour silently -- so the drift fails HERE instead.
    theme = _mermaid_theme()
    match = re.search(r"export const DIAGRAM_ROLES = \[([^\]]*)\]", theme)
    assert match, "DIAGRAM_ROLES not found in frontend/lib/mermaidTheme.ts"
    frontend_roles = tuple(re.findall(r"'([^']+)'", match.group(1)))
    assert frontend_roles == DIAGRAM_ROLES


def test_every_role_has_a_paint_in_both_themes():
    # globals.css is the source of truth for the values (resolveRolePaints
    # reads them with getComputedStyle); the literals in mermaidTheme.ts are
    # its SSR fallback. A role with no token would resolve to the fallback in
    # one theme and to nothing in the other.
    css = (FRONTEND / "app" / "globals.css").read_text(encoding="utf-8")
    for role in DIAGRAM_ROLES:
        for channel in ("fill", "stroke"):
            token = f"--ts-diagram-{role}-{channel}:"
            # Once for the dark :root block and once for the light override.
            assert css.count(token) == 2, f"{token} appears {css.count(token)}x"
    assert css.count("--ts-diagram-ink:") == 2


def test_the_parity_harness_scores_against_the_same_four_names():
    # tests/parity/normalise.py renders a DocumentSpec diagram to mermaid and
    # writes `:::role` for a role it recognises; a name it does not recognise
    # is dropped there. Same list, or the harness silently scores a diagram
    # the app would paint differently.
    normalise = (Path(__file__).parent / "parity" / "normalise.py").read_text(encoding="utf-8")
    match = re.search(r"DIAGRAM_ROLES = \(([^)]*)\)", normalise)
    assert match, "DIAGRAM_ROLES not found in tests/parity/normalise.py"
    parity_roles = tuple(re.findall(r'"([^"]+)"', match.group(1)))
    assert parity_roles == DIAGRAM_ROLES


def test_the_chat_prompt_actually_carries_the_roles():
    # The constant is only worth anything where it is attached: the assistant
    # prompt, both modes, in engines/chat.py.
    chat = (REPO / "orchestrator" / "app" / "engines" / "chat.py").read_text(encoding="utf-8")
    assert chat.count("+ DIAGRAM_INSTRUCTION +") == 2
