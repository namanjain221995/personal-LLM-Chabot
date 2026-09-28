"""AN EDGE MUST NOT BE DRAWN THROUGH A BOX IT DOES NOT CONNECT TO.

THIS FILE IS A GUARD FOR A DEFECT THAT IS STILL OPEN, and it is written
that way on purpose: `test_an_edge_never_crosses_a_box_it_does_not_connect_to`
is `xfail(strict=True)`, so it stays quiet while the defect is there and
FAILS LOUDLY as an XPASS the day the router is fixed — which is the signal
to delete the marker, not the test.

WHY IT IS HERE AND NOT FIXED HERE. The routing lives in
`app/artifacts/render/diagrams.py`, which belongs to the diagram group
(`integ/diagram-group-r3`); this branch owns `compose.py` and touches no
geometry. What this branch DOES do is raise the defect's exposure from "a
person pasted a mermaid fence" to "every model-composed technical report
asks for one of these pictures", so it must not merge with the suite still
blind to it. `test_artifact_render_diagrams.py::test_no_two_boxes_overlap`
guards box-against-box and nothing guarded edge-against-box.

MEASURED 2026-09-28 on this branch, and LOOKED AT, not only counted. The
nine-node request path below lays out with 14 drawn segments, 6 of which
cross a box they do not connect to (Postgres twice, Reranker,
Orchestrator, vLLM head, Cloudflare). Rendered to a PNG and read as a
person would read it: the dashed `Answer stream --SSE--> Browser` edge
disappears under the Postgres box and re-emerges from its RIGHT edge as a
short dashed stub pointing straight at Router model, so the picture states
a Postgres -> Router model connection that the diagram never declares. The
same dashed line also cuts the Reranker box, and the "SSE" label is
stranded beside vLLM head, nowhere near either of its endpoints.

WHAT CAUSES IT. `AN --SSE--> U` is a back edge; `_feedback_arcs` reverses it
and `_add_dummies` gives it a corridor of dummy nodes, but `_order`/`_place`
position that corridor without asking whether a real box already occupies
it, and `_draw` paints the boxes after the edges. So the line is occluded
rather than routed.

WHY IT MATTERS MORE THAN A COSMETIC NOTE. A diagram's whole job is to say
which parts connect. An edge drawn through an unrelated box asserts a
connection, and a reader has no way to tell the asserted one from the
declared one. The colour work on this renderer is sound — four roles, a
light tint fill with a saturated border, a legend naming every role that
appears, so hue is never the only encoding — and that is exactly why the
geometry now carries the whole meaning.
"""
from __future__ import annotations

import pytest

from app.artifacts import spec as S
from app.artifacts.render import diagrams as D

#: The request path this platform's own architecture draws. TYPED, built
#: straight from `spec.Diagram` and not from mermaid, so this is not the
#: widened edge grammar and reproduces without it.
NODES = [
    {"id": "U", "label": "Browser", "kind": "external"},
    {"id": "CF", "label": "Cloudflare", "kind": "external"},
    {"id": "OR", "label": "Orchestrator", "kind": "service"},
    {"id": "RT", "label": "Router model", "kind": "model"},
    {"id": "PG", "label": "Postgres", "kind": "store"},
    {"id": "LN", "label": "LanceDB", "kind": "store"},
    {"id": "RR", "label": "Reranker", "kind": "model"},
    {"id": "VL", "label": "vLLM head", "kind": "model"},
    {"id": "AN", "label": "Answer stream", "kind": "service"},
]
EDGES = [
    {"source": "U", "target": "CF"},
    {"source": "CF", "target": "OR"},
    {"source": "OR", "target": "RT", "label": "classify"},
    {"source": "OR", "target": "PG"},
    {"source": "OR", "target": "LN", "label": "recall"},
    {"source": "LN", "target": "RR"},
    {"source": "RR", "target": "VL"},
    {"source": "VL", "target": "AN"},
    # The back edge. This is the one that is routed through three boxes.
    {"source": "AN", "target": "U", "label": "SSE", "style": "dashed"},
]

#: Samples taken along each segment's interior. A segment that merely STARTS
#: or ENDS at a node reports interior samples too (`_segments` joins node
#: CENTRES), so a segment is only counted against a box when the box is
#: neither of its endpoints.
SAMPLES = 100
#: How many of those samples inside a box count as passing through it. Three
#: of a hundred is a clipped corner; more is a line crossing the box.
THROUGH = 3


def _crossings(diagram):
    """Every (segment, box) pair where the segment crosses a box it does not
    connect to, in layout space."""
    layout = D.layout_for(diagram)
    segments = D._segments(layout.nodes, layout.edges)
    boxes = {nid: n for nid, n in layout.nodes.items()
             if not getattr(n, "dummy", False) and getattr(n, "label", None)}
    found = []
    for p, q in segments:
        for nid, n in boxes.items():
            if (abs(p[0] - n.x) < 1e-9 and abs(p[1] - n.y) < 1e-9) or \
               (abs(q[0] - n.x) < 1e-9 and abs(q[1] - n.y) < 1e-9):
                continue  # this box is one of the segment's own endpoints
            x0, x1 = n.x - n.w / 2, n.x + n.w / 2
            y0, y1 = n.y - n.h / 2, n.y + n.h / 2
            hits = sum(1 for i in range(1, SAMPLES)
                       if x0 < p[0] + (q[0] - p[0]) * (i / SAMPLES) < x1
                       and y0 < p[1] + (q[1] - p[1]) * (i / SAMPLES) < y1)
            if hits > THROUGH:
                found.append((nid, n.label, hits))
    return layout, segments, found


def test_a_straight_run_of_boxes_routes_cleanly():
    """The control. Without a back edge the router is correct, so a failure
    of the xfail below really is about the corridor and not about this
    file's geometry."""
    diagram = S.Diagram(title="Straight", direction="TD", nodes=NODES, edges=EDGES[:-1])
    _, segments, found = _crossings(diagram)
    assert segments, "nothing was drawn"
    assert found == [], f"even without the back edge, {len(found)} segment(s) cross a box: {found}"


def test_the_back_edge_is_what_creates_the_corridor():
    """Names the cause, so the open defect is not mistaken for a general
    layout failure: adding ONE back edge to the clean graph above is what
    puts a line through three boxes."""
    clean = S.Diagram(title="Straight", direction="TD", nodes=NODES, edges=EDGES[:-1])
    looped = S.Diagram(title="Request path", direction="TD", nodes=NODES, edges=EDGES)
    _, _, clean_found = _crossings(clean)
    layout, _, looped_found = _crossings(looped)
    assert clean_found == []
    assert looped_found, "the defect did not reproduce; if it is fixed, drop the xfail below"
    assert layout.reversed_edges == 1, "the back edge was expected to be reversed into a corridor"
    crossed = sorted({label for _, label, _ in looped_found})
    assert "Postgres" in crossed, crossed


@pytest.mark.xfail(
    strict=True,
    reason=(
        "OPEN DEFECT, owned by integ/diagram-group-r3 (app/artifacts/render/diagrams.py): a reversed "
        "back edge's dummy corridor is placed without asking whether a real box already occupies it, "
        "and the boxes are painted after the edges, so the line is occluded rather than routed. "
        "Measured on this branch: 14 drawn segments, 6 crossing a box they do not connect to. "
        "WHEN THIS XPASSES the router has been fixed — delete this marker, keep the test."
    ),
)
def test_an_edge_never_crosses_a_box_it_does_not_connect_to():
    """The contract a reader relies on: every line in the picture means the
    connection it is drawn between, and nothing else."""
    diagram = S.Diagram(title="Request path", direction="TD", nodes=NODES, edges=EDGES)
    _, segments, found = _crossings(diagram)
    assert found == [], (
        f"{len(found)} of {len(segments)} drawn segments cross a box they do not connect to: "
        + ", ".join(f"{label} ({hits}/{SAMPLES - 1} samples)" for _, label, hits in found)
    )


def test_the_role_colours_are_not_the_only_encoding():
    """Not the routing defect, but the reason the routing defect is the whole
    story: hue is never load-bearing on its own here, so geometry carries the
    meaning. Every box keeps its own label and the legend names every role
    that appears, which is what makes four hues legal at the CVD separation
    they were validated at."""
    diagram = S.Diagram(title="Request path", direction="TD", nodes=NODES, edges=EDGES)
    layout = D.layout_for(diagram)
    drawn = [n for n in layout.nodes.values() if not getattr(n, "dummy", False)]
    assert drawn and all(n.label for n in drawn), "a box without a label would leave colour alone to say what it is"
    roles_used = {n.role for n in drawn if n.role}
    assert roles_used <= set(layout.legend), f"{roles_used - set(layout.legend)} appear with no legend entry"
    assert set(S.DIAGRAM_ROLES) == set(D.DIAGRAM_ROLES), "the schema and the painter disagree about the roles"
