"""Can the COMPOSER ask for a diagram? The schema half is not the whole gap.

`spec.DocumentBlock` gained a `diagram` member, `md_import` turns a mermaid
fence into one, and `render/` draws it into the DOCX and the PDF — all of
that is covered by `test_artifact_render_diagrams.py`. It closes exactly one
of the two ways a document is made: an answer that ALREADY held a diagram,
exported to a file.

The other way is the model composing a document from an instruction, and
there the schema being able to carry a diagram buys nothing on its own.
`_schema_with_defs(DocumentSpec, "blocks")` has accepted a diagram block
since the member landed — asserted below, so this file says what was already
true and what was not — while `_OUTLINE_SCHEMA`'s `elements` is a CLOSED enum
that did not list one, and no block vocabulary in `compose.py` named one.
The comment above `_write_one_section` records what that costs, measured on
this platform rather than assumed: "the model follows it literally: fifteen
scoped calls produced fifteen headings and no sub-heading at all, twice
measured". A block the vocabulary does not name is a block the model does
not write, so a model-composed technical report held zero pictures however
capable the renderer had become.

WHAT THESE TESTS DO NOT CLAIM. None of them proves the model draws a GOOD
diagram, or draws one in the right place — that needs a live generation, and
the GPUs are not this branch's to spend. They pin the contract underneath
that: the word exists in the closed enum, every vocabulary names it, the
roles the prompt teaches are the roles the schema accepts, and a diagram the
model returns survives the section path into a real `DiagramBlock`.
"""
from __future__ import annotations

import asyncio
import json
import re

from app import llm
from app.artifacts import compose as C
from app.artifacts import spec as S
from app.artifacts import types as T


def _element_enum() -> list:
    return C._OUTLINE_SCHEMA["properties"]["sections"]["items"]["properties"]["elements"]["items"]["enum"]


def test_the_outline_planner_can_ask_a_section_for_a_diagram():
    """The enum is closed, so a word missing from it cannot be planned."""
    assert "diagram" in _element_enum(), (
        "the outline planner picks a section's elements from this list and nothing else; "
        "without the word, no section is ever planned as a diagram"
    )


def test_the_planned_word_is_the_block_type_the_renderer_dispatches_on():
    """`elements` and the block discriminator must not drift apart.

    The outline says "diagram" and the model then writes a block whose `type`
    the renderers switch on. If the two were ever spelled differently the
    plan would ask for something no block satisfies, and the failure is
    silent: the section simply comes back as prose.
    """
    assert S.DiagramBlock.model_fields["type"].default == "diagram"
    assert S.DiagramBlock.model_fields["type"].default in _element_enum()


def test_the_section_block_schema_already_accepted_a_diagram():
    """The half that was NOT missing, pinned so the gap is not misread.

    The model's per-section JSON schema is built from `DocumentSpec`, so it
    has carried a diagram since the member landed. The gap was the prompt,
    not the schema, and a future reader deserves to see that stated rather
    than inferred from the fix.
    """
    schema = json.dumps(C._schema_with_defs(S.DocumentSpec, "blocks"))
    assert '"diagram"' in schema


def test_every_block_vocabulary_names_the_diagram():
    """All three lists, because they have already drifted apart in wording.

    `_KIND_GUIDE["document"]` is the whole-document prompt, `_requested_line`
    is the one used when the request names its own sections, and the
    per-section prompt is the sectioned route. A diagram named in two of the
    three is a diagram that disappears on whichever route was missed.
    """
    assert C.DIAGRAM_CLAUSE in C._KIND_GUIDE["document"]
    assert C.DIAGRAM_CLAUSE in C._requested_line(["Architecture", "Data path", "Limitations"])


def test_a_scoped_section_call_is_told_a_diagram_is_available(monkeypatch):
    """The sectioned route, asserted on the REAL prompt the model receives.

    Built the way `test_a_scoped_section_call_is_told_a_section_has_parts`
    builds it: the model call is replaced, and what is inspected is the
    message `_write_one_section` actually assembled.

    ASSERTED ON THE SCOPED HALF ONLY, because `in seen[0]` passed either
    way. `seen[0]` is the WHOLE system message, and `_material_messages`
    has already filled it with `_KIND_GUIDE['document']`, which carries the
    same clause — so with the `+ DIAGRAM_CLAUSE +` inside
    `_write_one_section` reverted this file still reported "10 passed"
    (measured 2026-09-28), and the per-section prompt is the biggest
    per-call cost and the route a long technical report actually takes.
    Splitting on the marker is the idiom
    `test_artifact_compose.py::test_a_scoped_section_call_is_told_a_section_has_parts`
    already uses for exactly this reason.
    """
    seen: list = []

    async def fake(messages, **kw):
        seen.append(messages[0]["content"])
        return json.dumps({"blocks": [{"type": "heading", "level": 1, "text": "Architecture"},
                                      {"type": "paragraph", "text": "Two nodes."}]})

    monkeypatch.setattr(llm, "json_completion", fake)
    req = C.ComposeRequest(kind="document", formats=["pdf"], template_id="technical_report", effort="think",
                           instruction="Write a technical report on the retrieval architecture.",
                           material=C.Material(instruction="Write a technical report on the retrieval architecture."))
    asyncio.run(C._write_one_section(
        req, T.EFFORT_BUDGETS["think"], C.target_for(req),
        {"title": "T", "sections": []},
        {"heading": "Architecture", "purpose": "how the parts connect", "elements": ["paragraphs", "diagram"]},
        written=[], words=400, position=(1, 5)))
    assert seen, "the section writer did not reach the model"
    marker = "YOU ARE WRITING ONE SECTION"
    assert marker in seen[0], "the scoped append is no longer recognisable"
    assert C.DIAGRAM_CLAUSE in seen[0].split(marker)[1], (
        "the SCOPED half of the section prompt does not name a diagram; the whole-document "
        "guide naming one is not the same thing, and this assertion used to be satisfied by it"
    )
    # The duplication is deliberate — see the DIAGRAM_CLAUSE comment block —
    # so it is pinned rather than left to look like a slip. The cost of the
    # second copy is in tests/test_diagram_clause_budget.py.
    assert seen[0].count(C.DIAGRAM_CLAUSE) == 2, (
        "the sectioned system message says the clause once from _KIND_GUIDE['document'] and once "
        "from the scoped append; if that changed, re-measure the per-section cost"
    )


def test_the_prompt_teaches_exactly_the_roles_the_schema_accepts():
    """A role the prompt invents is not refused loudly — it is re-coloured.

    `DiagramNode.kind` is a closed Literal and `md_import`'s reader folds an
    unknown role to the default, so a prompt teaching "database" instead of
    "store" would cost every one of those boxes its own colour and its legend
    entry with nothing raised anywhere. The clause therefore reads the names
    off `spec.DIAGRAM_ROLES` instead of spelling them, and this pins that it
    kept doing so.
    """
    assert S.DIAGRAM_ROLES, "the shared role vocabulary is empty"
    taught = re.search(r"its role \(([^)]*)\)", C.DIAGRAM_CLAUSE)
    assert taught, "the clause no longer names the roles in a parenthesised list"
    names = [w.strip() for w in taught.group(1).split(",") if w.strip()]
    # EQUAL, not a subset either way: a role the schema has and the prompt
    # omits is a role the model never reaches for, and a role the prompt has
    # and the schema lacks is silently folded to the default.
    assert names == list(S.DIAGRAM_ROLES), f"prompt teaches {names}, schema accepts {list(S.DIAGRAM_ROLES)}"
    for role in names:
        S.DiagramNode(id="n", label="n", kind=role)  # the schema accepts every word the prompt teaches


def test_the_clause_separates_a_diagram_from_a_chart():
    """The one confusion that would make this change worse than no change.

    A diagram drawn where a chart belongs loses the numbers; a chart also
    needs `material.tables`, so a model reaching for a diagram to show
    magnitude would produce a picture with no data in it at all.
    """
    clause = C.DIAGRAM_CLAUSE.lower()
    assert "connect" in clause, "the clause must say what a diagram IS for"
    assert "never a picture of numbers" in clause, "the clause must say what a diagram is NOT for"


def test_the_technical_report_template_asks_for_a_diagram():
    """The shape the owner asked for, and got with no picture in it."""
    assert "diagram" in C._TEMPLATE_GUIDE["technical_report"].lower()


def test_a_diagram_the_model_returns_survives_the_section_path(monkeypatch):
    """End of the composer road: a returned diagram reaches a real block.

    A new block type can be dropped between the model and the spec by
    anything that rebuilds a section's blocks. This drives the real
    `_write_one_section` with a model that returns a diagram and then parses
    the result the way the pipeline does, so the block is proven to arrive as
    a `DiagramBlock` and not as a discarded dict.
    """
    diagram_block = {
        "type": "diagram",
        "diagram": {
            "title": "Retrieval path",
            "direction": "TD",
            "nodes": [{"id": "Q", "label": "Question", "kind": "external"},
                      {"id": "V", "label": "Vector store", "kind": "store"},
                      {"id": "A", "label": "Answer", "kind": "service"}],
            "edges": [{"source": "Q", "target": "V"}, {"source": "V", "target": "A"}],
            "caption": "The path a question takes.",
        },
    }

    async def fake(messages, **kw):
        return json.dumps({"blocks": [{"type": "heading", "level": 1, "text": "Architecture"},
                                      {"type": "paragraph", "text": "The parts connect like this."},
                                      diagram_block]})

    monkeypatch.setattr(llm, "json_completion", fake)
    req = C.ComposeRequest(kind="document", formats=["pdf"], template_id="technical_report", effort="think",
                           instruction="Write a technical report on the retrieval architecture.",
                           material=C.Material(instruction="Write a technical report on the retrieval architecture."))
    blocks = asyncio.run(C._write_one_section(
        req, T.EFFORT_BUDGETS["think"], C.target_for(req),
        {"title": "T", "sections": []},
        {"heading": "Architecture", "purpose": "how the parts connect", "elements": ["paragraphs", "diagram"]},
        written=[], words=400, position=(1, 5)))
    assert any(b.get("type") == "diagram" for b in blocks), f"the diagram was dropped: {[b.get('type') for b in blocks]}"

    doc = S.DocumentSpec(title="T", blocks=blocks)
    drawn = [b for b in doc.blocks if isinstance(b, S.DiagramBlock)]
    assert len(drawn) == 1
    assert [n.kind for n in drawn[0].diagram.nodes] == ["external", "store", "service"]
    # And the gates can read the figure's own words, so an unfinished caption
    # on a model-composed diagram is caught the same way an imported one is.
    assert "Retrieval path" in S.text_of(S.ArtifactSpec(kind="document", document=doc))


def test_a_deck_and_a_workbook_are_never_told_about_a_diagram():
    """The leak this change had to avoid, and nearly shipped with.

    `_requested_line` is built for EVERY kind — `_material_messages` calls it
    once and `_KIND_GUIDE[req.kind]` is what varies — so a clause dropped into
    it plainly would reach a deck and a workbook too. `Slide` has no diagram
    field and no renderer draws one into a .pptx or an .xlsx, so a deck told
    to use diagrams is a deck told to write a block its own schema refuses.
    The whole-document guide is keyed by kind already; this pins the other
    one, and pins that the deck's sentence still says everything it used to.
    """
    for kind in ("presentation", "workbook"):
        line = C._requested_line(["Architecture", "Limits"], kind=kind)
        assert C.DIAGRAM_CLAUSE not in line, f"a {kind} was told to use a block it cannot hold"
        # unchanged in every other respect
        assert "a table where things are compared" in line
        assert '"warning"' in line and '"note"' in line and "numbered block" in line
        assert C.DIAGRAM_CLAUSE not in C._KIND_GUIDE[kind]
    assert "diagram" not in " ".join(S.Slide.model_fields)
    assert C.DIAGRAM_CLAUSE in C._requested_line(["Architecture"], kind="document")


def test_no_assembled_deck_or_workbook_prompt_mentions_a_diagram():
    """The same promise as the test above, asserted on what the model RECEIVES.

    The test above checks two of the THREE places a kind can be told about a
    block, and the third was leaking. `_material_messages` builds its system
    message from `_KIND_GUIDE[kind]` AND `_TEMPLATE_GUIDE[template_id]`, and
    `_TEMPLATE_GUIDE` is one flat dict holding document, deck and workbook ids
    together, read with no kind gate. Measured over all 42 kind x template_id
    pairs on 2026-09-28: `presentation|technical_report` and
    `workbook|technical_report` each carried the technical report's own
    diagram sentence, +124 characters, into a prompt whose schema has no
    diagram in it.

    LATENT, NOT LIVE — `formats.template_for` is kind-scoped and the three id
    namespaces are disjoint today — but nothing pinned that disjointness
    either, so both halves are pinned here: the gate, and the accident it
    was relying on.
    """
    every_id = sorted({t for k in ("document", "presentation", "workbook") for t in S.templates_for(k)})
    for kind in ("presentation", "workbook"):
        for tpl in every_id:
            req = C.ComposeRequest(kind=kind, formats=["pdf"], template_id=tpl, effort="think",
                                   instruction="Write about the services.",
                                   material=C.Material(instruction="Write about the services."))
            system, user = (m["content"] for m in C._material_messages(
                req, budget=T.EFFORT_BUDGETS["think"], target=C.target_for(req),
                requested=["Architecture", "Limits"]))
            assert C.DIAGRAM_CLAUSE not in system, f"{kind}|{tpl} system"
            assert "diagram" not in system.lower(), f"{kind}|{tpl} system says 'diagram'"
            assert "diagram" not in user.lower(), f"{kind}|{tpl} user says 'diagram'"
    # A document still gets it, from both the kind guide and the template.
    doc = C.ComposeRequest(kind="document", formats=["pdf"], template_id="technical_report", effort="think",
                           instruction="Write about the services.",
                           material=C.Material(instruction="Write about the services."))
    doc_system = C._material_messages(doc, budget=T.EFFORT_BUDGETS["think"], target=C.target_for(doc))[0]["content"]
    assert C.DIAGRAM_CLAUSE in doc_system and "A diagram where the architecture" in doc_system


def test_the_template_id_namespaces_are_disjoint():
    """What made the leak above latent rather than live, pinned so that a new
    id shared between two kinds cannot quietly make it live. `grep -rn
    'templates_for' tests/` returned nothing before this."""
    for a, b in (("document", "presentation"), ("document", "workbook"), ("presentation", "workbook")):
        shared = (set(S.templates_for(a)) & set(S.templates_for(b))) - {"generic"}
        assert not shared, f"{a} and {b} now share {sorted(shared)}; _template_guide is the only thing between them"


def test_a_template_from_another_kind_falls_back_to_the_generic_guide():
    """The gate itself, on the pairing that leaked: a deck asked to use the
    technical report's id gets the generic guide, not the document's."""
    assert C._template_guide("presentation", "technical_report") == C._TEMPLATE_GUIDE["generic"]
    assert C._template_guide("workbook", "technical_report") == C._TEMPLATE_GUIDE["generic"]
    assert C._template_guide("document", "technical_report") == C._TEMPLATE_GUIDE["technical_report"]
    assert C._template_guide("presentation", "ceo") == C._TEMPLATE_GUIDE["ceo"]
    assert C._template_guide("workbook", "tracker") == C._TEMPLATE_GUIDE["tracker"]
    # A stranger id behaves as the bare `.get` miss always did.
    assert C._template_guide("document", "no_such_template") == C._TEMPLATE_GUIDE["generic"]
