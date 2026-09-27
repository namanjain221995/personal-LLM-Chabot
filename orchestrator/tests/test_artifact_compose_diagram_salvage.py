"""One bad word inside a figure must not cost the document.

WHAT THIS IS ABOUT. `spec.DiagramNode.kind` is a closed four-word Literal
that REFUSES an unknown role rather than folding it, and
`compose._validate_or_repair` answers ANY ValidationError with ONE
whole-document `_compose_once` whose reply REPLACES the sectioned draft. So
before `_salvage_diagrams`, a single mis-declared role inside one figure
discarded every other section's prose, and the only thing the person was
told was that the document came back short.

Measured end to end on 2026-09-28 through the real `compose()`, on the
owner's own route — a 6,000-word technical report with eight named sections,
sectioned because `target.words > SECTIONED_WRITER_WORDS`:

    every role correct              8 level-1 headings, 3,378 words, 1 diagram
    section 3 of 8 writes
    kind='database'  (before)       0 headings, 6 words, 0 diagrams
    section 3 of 8 writes
    kind='database'  (after)        8 headings, 3,390 words, 0 diagrams,
                                    1 "Diagram omitted" callout, and a
                                    warning that names the diagram

`'Store'`, `'STORE'` and `''` collapse it identically, and so do six other
shapes a model plausibly emits. The COLLAPSE MECHANISM is pre-existing — any
invalid block of any type does it — but only the diagram is newly invited
into every technical report by this branch, and it is the strictest
sub-schema in `DocumentBlock`.

WHAT IS NOT CLAIMED. Nothing here proves a model draws a GOOD diagram, or
that it mis-declares a role at any particular rate. These pin what happens
WHEN it does, which is the part code decides.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import llm
from app.artifacts import compose as C
from app.artifacts import spec as S
from app.artifacts import types as T

SECTIONS = ["Executive Summary", "Architecture", "Data path", "Retrieval",
            "Reranking", "Limitations", "Operations", "Next steps"]

INSTRUCTION = (
    "Write a 6,000 word technical report on the retrieval architecture. "
    "Requirements: 1. Executive Summary 2. Architecture 3. Data path "
    "4. Retrieval 5. Reranking 6. Limitations 7. Operations 8. Next steps"
)


def _node(nid, kind="service", label=None):
    return {"id": nid, "label": nid if label is None else label, "kind": kind}


def _diagram(nodes, edges, **over):
    return {"type": "diagram",
            "diagram": dict(title="Request path", direction="TD", nodes=nodes, edges=edges, **over)}


GOOD = _diagram([_node("Q", "external"), _node("V", "store"), _node("A")],
                [{"source": "Q", "target": "V"}, {"source": "V", "target": "A"}])


def _with_role(kind):
    block = json.loads(json.dumps(GOOD))
    block["diagram"]["nodes"][1]["kind"] = kind
    return block


#: The shapes, by what a model would have done wrong. Each one is REFUSED by
#: `spec.DiagramBlock` — measured, not assumed — so each one used to be a
#: whole-document loss.
REFUSED_SHAPES = {
    "an invented role": _with_role("database"),
    "a role in the wrong case": _with_role("Store"),
    "a role shouted": _with_role("STORE"),
    "an empty role": _with_role(""),
    "no nodes at all": _diagram([], []),
    "one node and no edges": _diagram([_node("A")], []),
    "two nodes sharing an id": _diagram([_node("A"), _node("A")], []),
    "more nodes than the schema allows": _diagram([_node(f"n{i}") for i in range(10_000)],
                                                  [{"source": "n0", "target": "n1"}]),
    "an empty label": _diagram([_node("A", label=""), _node("B", "store")],
                               [{"source": "A", "target": "B"}]),
    "a 4kB label": _diagram([_node("A", label="x" * 4096), _node("B", "store")],
                            [{"source": "A", "target": "B"}]),
}

#: Shapes that LOOK dangerous and are not: the spec handles them on purpose,
#: so the salvage must leave them alone. A dangling edge is DROPPED by
#: `Diagram._shape` rather than refused ("the rest of the picture is still
#: true"), a self-loop is drawn (`render/diagrams._Edge.self_loop`), and the
#: text fields are unicode-clean.
KEPT_SHAPES = {
    "an edge naming a node that does not exist": _diagram(
        [_node("A"), _node("B", "store")], [{"source": "A", "target": "ZZ"}]),
    "a self-loop": _diagram([_node("A"), _node("B", "store")], [{"source": "A", "target": "A"}]),
    "RTL, CJK and an emoji in the labels": _diagram(
        [_node("A", label="ملف 文件 🎉"), _node("B", "store", label="ولا")],
        [{"source": "A", "target": "B"}]),
    "a twenty-edge cycle": _diagram([_node(f"n{i}") for i in range(20)],
                                    [{"source": f"n{i}", "target": f"n{(i + 1) % 20}"} for i in range(20)]),
}


def _req(**over):
    kw = dict(kind="document", formats=["pdf"], template_id="technical_report", effort="think",
              instruction=INSTRUCTION, material=C.Material(instruction=INSTRUCTION))
    kw.update(over)
    return C.ComposeRequest(**kw)


def _prose(words, chunk=500):
    out = []
    while words > 0:
        out.append({"type": "paragraph", "text": " ".join(["retrieval"] * min(chunk, words))})
        words -= chunk
    return out


class _Model:
    """The sectioned route's model: an outline, then one call per section,
    with `block` appended to section `at`. Any whole-document call after
    that — the repair — returns a stub, which is what makes a collapse
    visible as a short document rather than as an error."""

    def __init__(self, block=None, at=3, sections=SECTIONS):
        self.block, self.at, self.sections = block, at, list(sections)
        self.calls, self.whole_document_calls = [], 0

    async def __call__(self, messages, *, json_schema=None, schema_name="", temperature=0.0,
                       max_tokens=None, thinking=False, effort=None):
        self.calls.append(schema_name)
        if schema_name == "artifact_outline":
            return json.dumps({
                "title": "Retrieval architecture", "audience": "engineers", "purpose": "how it works",
                "sections": [{"heading": h, "purpose": f"what {h} covers",
                              "elements": ["paragraphs", "diagram"]} for h in self.sections],
                "needs_current_facts": False, "assumptions": []})
        if schema_name == "artifact_section_write":
            i = sum(1 for c in self.calls if c == "artifact_section_write")
            head = self.sections[i - 1] if i <= len(self.sections) else f"Extra {i}"
            blocks = [{"type": "heading", "level": 1, "text": head}] + _prose(420)
            if i == self.at and self.block is not None:
                blocks.append(json.loads(json.dumps(self.block)))
            return json.dumps({"blocks": blocks})
        self.whole_document_calls += 1
        return json.dumps({"title": "Retrieval architecture", "template_id": "technical_report",
                           "blocks": [{"type": "heading", "level": 1, "text": "Retrieval architecture"},
                                      {"type": "paragraph", "text": "A short repaired document."}]})


def _compose(model, monkeypatch, **over):
    monkeypatch.setattr(llm, "json_completion", model)
    return asyncio.run(C.compose(_req(**over)))


def _shape_of(result):
    doc = result.spec.document
    return {
        "headings": sum(1 for b in doc.blocks if isinstance(b, S.Heading) and b.level == 1),
        "diagrams": sum(1 for b in doc.blocks if isinstance(b, S.DiagramBlock)),
        "omitted": sum(1 for b in doc.blocks if isinstance(b, S.Callout)
                       and b.title == C.DIAGRAM_OMITTED_TITLE),
        "words": len(S.text_of(result.spec).split()),
    }


# ------------------------------------------------ the shapes, one by one --


@pytest.mark.parametrize("name", sorted(REFUSED_SHAPES))
def test_every_shape_the_schema_refuses_really_is_refused(name):
    """The premise, measured rather than assumed. If the spec ever starts
    accepting one of these, the salvage below stops being needed for it and
    this file should say so."""
    with pytest.raises(Exception):
        S.DiagramBlock.model_validate(REFUSED_SHAPES[name])


@pytest.mark.parametrize("name", sorted(KEPT_SHAPES))
def test_the_shapes_the_spec_handles_on_purpose_are_not_refused(name):
    """The other half, so the salvage is never widened into dropping a
    figure the platform can draw. A dangling edge is dropped by
    `Diagram._shape`; a self-loop is a picture the renderer has a branch
    for."""
    S.DiagramBlock.model_validate(KEPT_SHAPES[name])


@pytest.mark.parametrize("name", sorted(REFUSED_SHAPES))
def test_a_refused_diagram_becomes_a_callout_and_a_warning(name):
    """`_salvage_diagrams` in isolation: the block is replaced in place, the
    blocks around it are untouched, and the caller gets a note to show."""
    raw = {"title": "T", "blocks": [
        {"type": "heading", "level": 1, "text": "Architecture"},
        {"type": "paragraph", "text": "The parts connect like this."},
        json.loads(json.dumps(REFUSED_SHAPES[name])),
        {"type": "paragraph", "text": "And that is the path."},
    ]}
    notes = C._tidy_document(raw, _req())
    types = [b["type"] for b in raw["blocks"]]
    assert types == ["heading", "paragraph", "callout", "paragraph"], name
    assert raw["blocks"][2]["title"] == C.DIAGRAM_OMITTED_TITLE
    assert raw["blocks"][2]["kind"] == "note"
    assert notes == [f"1 {C.DIAGRAM_OMITTED_WARNING}"]
    # and the document the caller goes on to validate really does validate
    S.DocumentSpec(title="T", blocks=raw["blocks"])


@pytest.mark.parametrize("name", sorted(KEPT_SHAPES))
def test_a_drawable_diagram_is_left_exactly_as_it_was(name):
    raw = {"title": "T", "blocks": [{"type": "heading", "level": 1, "text": "Architecture"},
                                    json.loads(json.dumps(KEPT_SHAPES[name]))]}
    before = json.dumps(raw["blocks"][1], sort_keys=True)
    notes = C._tidy_document(raw, _req())
    assert notes == []
    assert json.dumps(raw["blocks"][1], sort_keys=True) == before, name


def test_the_wording_is_the_one_a_mermaid_fence_already_gets():
    """A person meeting this in a composed report and a person meeting it in
    an exported answer are being told the same thing, so it must not read as
    two different failures. `md_import` writes the title; keep them equal."""
    root = Path(__file__).resolve().parents[1]
    text = (root / "app" / "artifacts" / "md_import.py").read_text(encoding="utf-8")
    assert f'"{C.DIAGRAM_OMITTED_TITLE}"' in text


# ----------------------------------------------------- the whole document --


def test_the_owner_route_keeps_its_sections_when_one_diagram_is_wrong(monkeypatch):
    """The measurement this file exists for, through the real `compose()`."""
    good = _shape_of(_compose(_Model(GOOD), monkeypatch))
    assert good == {"headings": 8, "diagrams": 1, "omitted": 0, "words": good["words"]}
    assert good["words"] > 3_000, good

    bad_model = _Model(_with_role("database"))
    result = _compose(bad_model, monkeypatch)
    bad = _shape_of(result)
    assert bad["headings"] == 8, "seven sections of real prose were discarded over one role word"
    assert bad["omitted"] == 1 and bad["diagrams"] == 0
    assert bad["words"] > 3_000, bad
    # WITHIN A HANDFUL OF WORDS of the good run: the callout replaces the
    # figure and costs nothing else. (It is not equal: the callout's own
    # sentence is text the gates can read.)
    assert abs(bad["words"] - good["words"]) < 40, (bad["words"], good["words"])


def test_the_person_is_told_which_block_was_lost(monkeypatch):
    """Before this, the ONLY warning naming anything was "the document is
    about 6 words against the 6,000 asked for" — which names the symptom and
    not one word of the cause."""
    result = _compose(_Model(_with_role("database")), monkeypatch)
    assert any(C.DIAGRAM_OMITTED_WARNING in w for w in result.warnings), result.warnings


@pytest.mark.parametrize("role", ["database", "Store", "STORE", ""])
def test_every_near_miss_of_a_role_behaves_the_same(role, monkeypatch):
    """The four spellings a model actually reaches for. `DiagramRole` is a
    closed Literal, so none of them folds."""
    shape = _shape_of(_compose(_Model(_with_role(role)), monkeypatch))
    assert shape["headings"] == 8 and shape["omitted"] == 1 and shape["words"] > 3_000, (role, shape)


def test_the_repair_pass_still_runs_for_everything_else(monkeypatch):
    """The salvage must not have quietly disabled the repair. A block that is
    NOT a diagram and does not validate still buys the whole-document rewrite
    it always did."""
    model = _Model({"type": "kpis", "items": []}, at=3)
    _compose(model, monkeypatch)
    assert model.whole_document_calls >= 1, "an invalid non-diagram block no longer reaches the repair"


def test_a_wrong_diagram_costs_no_extra_model_call(monkeypatch):
    """It used to cost one: the whole-document REPAIR, on top of the review
    and correction passes Think pays for anyway. Not paying for it is the
    cheaper AND the better outcome, which is rare enough to pin. Compared
    against the run whose roles are all correct rather than against zero,
    because a document composition at Think makes whole-document calls of
    its own that have nothing to do with a figure."""
    bad = _Model(_with_role("database"))
    _compose(bad, monkeypatch)
    good = _Model(GOOD)
    _compose(good, monkeypatch)
    assert bad.whole_document_calls == good.whole_document_calls, (
        f"a mis-declared role bought {bad.whole_document_calls - good.whole_document_calls} "
        "extra whole-document call(s); the repair is meant to be out of its way")
    assert len(bad.calls) == len(good.calls), (len(bad.calls), len(good.calls))


# ------------------------------------------------------------ the figure cap --


def _doc_with(n_diagrams):
    blocks = [{"type": "heading", "level": 1, "text": "Architecture"}]
    for i in range(n_diagrams):
        blocks.append({"type": "paragraph", "text": f"Figure {i}."})
        blocks.append(json.loads(json.dumps(GOOD)))
    return S.ArtifactSpec(kind="document", document=S.DocumentSpec(title="T", blocks=blocks))


def test_a_composed_document_holds_at_most_three_diagrams():
    """`DIAGRAM_CLAUSE` names no limit and is repeated in EVERY per-section
    prompt, so a fifteen-section report invited up to fifteen rendered PNGs
    on the box that is also answering live chat. `engines/__init__.py` states
    that "the three-diagram allowance for a DOCUMENT lives on the artifact
    path"; until now it lived nowhere."""
    assert C.MAX_DIAGRAMS_PER_DOCUMENT == 3
    spec = _doc_with(6)
    warnings = C._enforce_caps(spec, T.EFFORT_BUDGETS["max"])
    kept = [b for b in spec.document.blocks if isinstance(b, S.DiagramBlock)]
    assert len(kept) == 3
    assert any("trimmed from 6 diagrams to 3" in w for w in warnings), warnings


def test_the_cap_keeps_the_first_three_and_every_word_of_prose():
    """A figure past the third is CPU; a paragraph is the person's document.
    Only the figures are cut, and the earliest ones survive."""
    spec = _doc_with(5)
    before = [b.text for b in spec.document.blocks if isinstance(b, S.Paragraph)]
    C._enforce_caps(spec, T.EFFORT_BUDGETS["max"])
    after = [b.text for b in spec.document.blocks if isinstance(b, S.Paragraph)]
    assert after == before
    kinds = [type(b).__name__ for b in spec.document.blocks]
    assert kinds.count("DiagramBlock") == 3
    assert kinds.index("DiagramBlock") == 2, "the first figure was not the one kept"


@pytest.mark.parametrize("effort", ["fast", "think", "max"])
def test_three_diagrams_are_never_trimmed_at_any_effort(effort):
    """The allowance is a property of the DOCUMENT, not of what was paid for
    it: effort decides depth, never whether a figure exists."""
    spec = _doc_with(3)
    warnings = C._enforce_caps(spec, T.EFFORT_BUDGETS[effort])
    assert sum(1 for b in spec.document.blocks if isinstance(b, S.DiagramBlock)) == 3
    assert not any("diagram" in w for w in warnings), warnings
