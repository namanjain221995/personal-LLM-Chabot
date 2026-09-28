"""Node-and-edge diagrams → PNG, laid out and drawn here, with no new dependency.

WHY THE MODEL DOES NOT WRITE MERMAID. A diagram in a document used to be
impossible: `md_import` turned a ```mermaid fence into a callout reading
"Diagram omitted", and the document schema had no figure block at all, so the
PDF a person received had zero image objects. The fix is NOT to run mermaid —
that needs a browser inside the container that holds other people's documents.
The model declares TYPED nodes and edges (spec.Diagram), code lays them out,
and matplotlib — already the server-side chart painter, pinned in
requirements.txt — draws the picture. Nothing is executed, nothing is fetched,
and a node's role is DECLARED rather than guessed from its label.

NO LAYOUT LIBRARY. `networkx` is not in requirements.txt and this module must
not add it, so the Sugiyama pipeline is written out below: feedback-arc
removal, longest-path layering, dummy nodes for edges that span more than one
layer, barycentre ordering, then coordinates. Feedback-arc removal is not
optional. Measured on the five-node RAG loop with one retry edge, layering a
cyclic graph collapsed four of the five nodes into one layer and drew them on
top of each other, with nothing raising.

COLOUR CARRIES THE ROLE, NEVER THE DEPTH. Four roles — service, store, model,
external — take the repository's own chart slots 1-4 in a fixed order; an
unrecognised role folds to neutral grey and the list NEVER cycles. Measured
with the dataviz validator (`validate_palette.js`, light mode, surface
#FFFFFF, `--pairs all`, which is the governing check here because the LAYOUT,
not the palette order, decides which nodes end up touching):

    #2F6FB2,#E07B00,#0E9D9A,#C0566B      ALL CHECKS PASS
        worst all-pairs CVD  #C0566B↔#0E9D9A ΔE 7.1 (deutan), tritan 9.5  WARN
        worst normal vision  #0E9D9A↔#2F6FB2 ΔE 15.1                      PASS
    the repository's full eight slots      FAILED
        #B38C15↔#E07B00 ΔE 1.5 (protan) and ΔE 8.3 under normal vision

and no five-slot subset of those eight passes all-pairs either (all 56 were
run). A CVD ΔE in the 6-8 band is legal only with a secondary encoding, so
one is mandatory here and always present: every node carries its own text
label, and a legend names each role that appears. Colour is never the only
thing that tells two nodes apart.

Colour follows what a node IS, never how deep it sits. The layer-coloured
version was built, rendered and looked at: with four hues and an eight-layer
architecture graph, eight of the fifteen nodes came out neutral grey, which
reproduces the owner's "no colour" complaint from the other side.

The fill is a light TINT of the role's colour, the border is the saturated
slot at 1.8 pt, and the label is dark ink — so hue carries the role while
label contrast stays constant whatever the role. The tints are deliberately
NOT the identity carrier, and the validator says why: run as a palette of
their own they fail the lightness band, the chroma floor and the normal-vision
floor (worst pair ΔE 3.0). The border and the label are what identify a node;
the tint is a wash that makes the role visible at a glance.

PAPER ONLY. There is no dark palette here. A DOCX and a PDF are printed on
white, and no dark set has been validated for this use. What that would cost
is now measured rather than left open (2026-09-28, the same validator,
`--mode dark --surface "#1a1a19" --pairs all`): the four hold their CVD and
normal-vision separation unchanged on a dark surface (7.1 deutan, 15.1 normal)
and all four clear 3:1 against it, but `#E07B00` sits at OKLCH L 0.682 and
falls outside the dark mode's lightness band, so a dark set is a RE-STEP of at
least the orange and not a reuse of these four. Nothing here reads `theme` for
a dark surface, so that is a future decision, not a gap in this one.

PAGE FIT IS PART OF THE DRAWING. `docx.py` pins a picture to the content
width, so a figure wider than the page is scaled DOWN and its labels shrink
with it. Measured on the 15-node architecture graph before this module
existed: it laid out at 9.64 x 12.54 in, which at 6.3 in of portrait width is
a scale of 0.654 and puts a 9.5 pt label on the page at 6.2 pt; the five-node
RAG loop landed at 5.4 pt. Both are under the ~8 pt floor at which a printed
label stops being readable. So the layout takes the page box as INPUT: it
wraps a layer that is too wide into sub-rows, tries both orientations and
several label-wrap widths, and reports the effective point size it achieves.
When no configuration reaches 8 pt the caller is told (`fits=False`) and says
so in the render report, instead of silently shipping a 6 pt label.

WHAT THE 8 pt FLOOR ACTUALLY COVERS, re-measured 2026-09-27 after the first
statement of it was found to be a property of the test corpus rather than of
the schema. The floor is bounded by the SIZE of the laid-out figure, not by
the node count: it holds while the figure is at most 7.48 in wide and 9.97 in
tall, which is the page box divided by 8.0/9.5. `_split_wide_layers` keeps
WIDTH inside that bound; nothing keeps HEIGHT inside it, because no pass
folds a deep graph into columns. So, on portrait (6.3 x 8.4 in):

  * a chain of short one-line labels holds to 11 layers (8.21 pt) and breaks
    at 12 (7.53 pt); labels that wrap to several lines hold to 8 layers
    (8.23 pt) and break at 9 (7.31 pt). The deepest diagram in the test
    corpus, the 15-node architecture graph, is 9 layers at 9.50 pt.
  * the 24-node / 40-edge schema cap does NOT bound it. Of 40 random graphs
    at exactly that size, 3 reach 8 pt and 37 do not; the worst is 5.19 pt,
    and a 24-node cycle is 24 layers at 3.70 pt. An earlier note here read
    "the largest diagram the schema allows still fits at 8.7 pt" — 8.7 pt is
    the value for one random seed, not a bound.

A diagram past that depth is not shipped as a silent 6 pt label: `fits` is
False and render/__init__.py puts a sentence in the report that names the
real point size. The missing capability is a fold of a deep graph into
columns, which would make the tall case fit the way `_split_wide_layers`
already makes the wide case fit; turning the page landscape does NOT help,
and is measured to make it worse (a 12-step chain 7.53 -> 5.02 pt, a 24-step
chain 3.76 -> 2.51 pt), because a deep graph is already the tall shape.

COST (measured in this worktree, aarch64, matplotlib 3.11.1; see
tests/test_artifact_render_diagrams.py::test_render_cost_is_small): a 15-node
graph lays out and draws in ~100-150 ms against the 180 s render timeout.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import theme

# ------------------------------------------------------------------ roles --

#: The closed role vocabulary. It is the SAME list the parity harness's
#: `normalise.DIAGRAM_ROLES` scores against and the same list the chat
#: prompt teaches; a test pins all three together so the vocabulary cannot
#: drift in one place only. Never rename, never extend: the palette below is
#: exactly four slots wide and has no fifth that passes.
DIAGRAM_ROLES: Tuple[str, ...] = ("service", "store", "model", "external")

#: chart_spec.DEFAULT_PALETTE slots 1-4, in that fixed order. Validated above,
#: and RE-VALIDATED 2026-09-28 because feat/understand-every-picture-ask routes
#: many more requests down this path — every flow chart, org chart, dependency
#: graph, state diagram and mind map that used to be answered "attach a CSV"
#: now arrives here. `scripts/validate_palette.js "#2F6FB2,#E07B00,#0E9D9A,#C0566B"
#: --mode light`, surface #fcfcfb: ALL CHECKS PASS, with the same two warnings
#: the design already answers —
#:
#:   CVD separation  #C0566B <-> #0E9D9A  ΔE 7.1 deutan (tritan 28.4), inside
#:                   the 6-8 floor band, legal only with secondary encoding;
#:   contrast        #E07B00 at 2.92:1, under 3:1, relief required.
#:
#: Both obligations are met by construction and not by luck: `spec.DiagramNode
#: .label` is min_length=1 so every box carries its own text, `_legend_for`
#: names each role a diagram declares, and the fills are blended 0.78 toward
#: white so the label's contrast does not move with the role.
#:
#: `--mode dark` FAILS the lightness band on #E07B00 and is NOT APPLICABLE: this
#: module is paper-only (see the module docstring) and never draws on a dark
#: surface. The browser's mermaid is a separate renderer with its own classDefs.
ROLE_COLOURS: Dict[str, str] = {
    "service": "#2F6FB2",
    "store": "#E07B00",
    "model": "#0E9D9A",
    "external": "#C0566B",
}

#: A role the vocabulary does not know folds here — and, since 2026-09-27, so
#: does a node nobody tagged (`DiagramNode.kind is None`). The palette never
#: cycles.
#:
#: THE NEUTRAL IS NOT A FIFTH SLOT, AND THE VALIDATOR SAYS SO OUT LOUD. Before
#: 2026-09-27 this grey could not appear in a validated figure at all —
#: `DiagramNode.kind` was a closed Literal with a "service" default, so every
#: node arrived carrying one of the four. Making an untagged node honest puts
#: the grey on the page beside them, so the five were run through the dataviz
#: validator together (light, surface #FFFFFF, `--pairs all`):
#:
#:     #2F6FB2,#E07B00,#0E9D9A,#C0566B            ALL CHECKS PASS
#:     the same four + #6B7280                    FAILED
#:         chroma floor   #6B7280 at 0.023 — reads gray
#:         all-pairs CVD  #6B7280↔#C0566B ΔE 3.4 (protan), tritan 7.5
#:         normal vision  #6B7280↔#2F6FB2 ΔE 10.3, under the floor of 15
#:
#: That FAIL is the design, not a defect to fix: the neutral's job is to look
#: UNPAINTED, and a grey that separated from four hues like a fifth hue would
#: read as a fifth role. What it means is that colour alone must never be what
#: tells an unclassified box from a `service` or an `external` one — and it
#: never is. Every node carries its own text label (`spec.DiagramNode.label`
#: is min_length=1), and `_legend_for` lists only the roles a diagram actually
#: declares, so an untagged box appears in no key and claims nothing. Alone
#: against paper the grey passes its own check: contrast 3:1 or better.
#: tests/test_artifact_render_diagrams.py pins both halves of that.
NEUTRAL = "#6B7280"

#: #C0566B is also chart_colours.LOSS. A document could therefore show that
#: red as "loss" in a chart and as the `external` role on the next page. That
#: is a deliberate, stated cost: a diagram carries its own labels and its own
#: legend and shares no scale, axis or legend with a chart, so the two
#: meanings never meet inside one figure. The alternative — a fifth hue for
#: diagrams — does not exist: no five-slot subset of the repository's eight
#: passes the all-pairs check.
_RESERVED_NOTE = "external reuses the chart 'loss' colour; see the module docstring"

PAPER = "#FFFFFF"
INK = "#101418"
EDGE_INK = "#55606D"
#: How far each role's colour is blended towards white for the node fill.
#: 0.78 keeps the hue visible on paper while any label sits on a near-white
#: ground, so label contrast does not move with the role.
FILL_TINT = 0.78

# ------------------------------------------------------------- geometry --

#: Node label size. Everything else is derived from it, so the 8 pt floor
#: below is a statement about THIS number after page scaling.
FONT_PT = 9.5
EDGE_LABEL_PT = 7.5
LEGEND_PT = 8.0
#: Under this, on paper, a label stops being readable. The fitter treats it
#: as a hard target and reports failure rather than shipping below it.
MIN_EFFECTIVE_PT = 8.0

#: Average glyph advance of Liberation Sans as a fraction of the point size.
#: Measured over the label corpus in tests/test_artifact_render_diagrams.py
#: (matplotlib text extents at 9.5 pt): 0.50 em on mixed-case prose, 0.56 em
#: with digits and capitals. The wider figure is used so a box is never
#: narrower than its text.
CHAR_EM = 0.56
LINE_SPACING = 1.25
PAD_X_IN = 0.20
PAD_Y_IN = 0.13
MIN_NODE_W_IN = 1.15
GAP_MINOR_IN = 0.30      # between nodes inside one layer
GAP_MAJOR_IN = 0.46      # between layers
FIG_PAD_IN = 0.22
LEGEND_H_IN = 0.34

#: Where a document puts a figure: (width, height) of the usable box, in
#: inches. Portrait is docx.py's 6.3 in content width and what is left of an
#: A4 page under DOCUMENT_GRID once the running header and footer are taken;
#: landscape is the 9.7 in run.
PORTRAIT_BOX_IN: Tuple[float, float] = (6.3, 8.4)
LANDSCAPE_BOX_IN: Tuple[float, float] = (9.7, 5.6)

#: Label wrap widths tried, in order of preference. Wider reads better; the
#: narrower ones are what make a wide layer fit a portrait page.
_WRAP_WIDTHS: Tuple[int, ...] = (20, 16, 13, 11)
_MAX_LABEL_LINES = 3

DPI = 200


# ------------------------------------------------------------- the graph --


@dataclass
class _Node:
    nid: str
    label: str
    role: str
    lines: List[str] = field(default_factory=list)
    layer: int = 0
    row: int = 0            # sub-row inside the layer, after wrapping
    order: float = 0.0
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    dummy: bool = False


@dataclass
class _Edge:
    src: str
    dst: str
    label: str = ""
    dashed: bool = False
    reversed_: bool = False
    #: Dummy node ids the edge passes through, source-to-target order.
    via: List[str] = field(default_factory=list)
    self_loop: bool = False


@dataclass
class DiagramLayout:
    """Everything a test or a renderer needs to know about one layout."""

    direction: str                       # "TD" or "LR", after the fitter chose
    nodes: Dict[str, _Node]
    edges: List[_Edge]
    layers: Dict[int, List[str]]
    #: (x0, y0, x1, y1) of the whole drawing in figure inches, padding and
    #: legend band included. The figure is exactly this big, so the page
    #: scale below is computed on what is actually drawn.
    frame: Tuple[float, float, float, float]
    box_in: Tuple[float, float]
    wrap: int
    reversed_edges: int
    crossings: int
    legend: Tuple[str, ...]

    @property
    def fig_in(self) -> Tuple[float, float]:
        x0, y0, x1, y1 = self.frame
        return (x1 - x0, y1 - y0)

    @property
    def scale(self) -> float:
        """How far the page shrinks this figure: 1.0 when it fits as drawn."""
        w, h = self.fig_in
        bw, bh = self.box_in
        return min(1.0, bw / w if w > 0 else 1.0, bh / h if h > 0 else 1.0)

    @property
    def effective_pt(self) -> float:
        """The point size a label actually reaches on the page."""
        return FONT_PT * self.scale

    @property
    def fits(self) -> bool:
        return self.effective_pt >= MIN_EFFECTIVE_PT - 1e-9

    @property
    def display_in(self) -> Tuple[float, float]:
        """The size to place the picture at, in inches."""
        s = self.scale
        return (self.fig_in[0] * s, self.fig_in[1] * s)

    def real_nodes(self) -> List[_Node]:
        return [n for n in self.nodes.values() if not n.dummy]

    def box(self, nid: str) -> Tuple[float, float, float, float]:
        """(x0, y0, x1, y1) of a node, in figure inches."""
        n = self.nodes[nid]
        return (n.x - n.w / 2, n.y - n.h / 2, n.x + n.w / 2, n.y + n.h / 2)


class DiagramError(ValueError):
    """A diagram that cannot be laid out. The caller falls back to text."""


# --------------------------------------------------------------- parsing --

#: The mermaid subset the chat prompt restricts the model to, and the only
#: thing this parser accepts. Anything else makes the whole source refuse, so
#: a half-understood graph is never drawn: the caller keeps today's callout.
#: The direction is OPTIONAL, because in mermaid it is: a bare `flowchart` or
#: `graph` line is legal and defaults to TD. Requiring it here did not refuse
#: such a source — it fell through to `_DECL_RE`, which reads a bare word as a
#: node id, so the word "flowchart" became a box in the drawing next to the
#: real graph. That broke this module's own promise that a source it cannot
#: read falls back to the callout and never to a wrong picture. Measured
#: before the fix: `flowchart\nA["Ingest"] --> B["Index"]` gave three nodes,
#: one of them called "flowchart".
_DIR_RE = re.compile(r"^(?:flowchart|graph)(?:\s+(TD|TB|LR|RL|BT))?$", re.IGNORECASE)


def _node_part(prefix: str) -> str:
    """`A`, `A["label"]`, `A:::role`, `A["label"]:::role` — and nothing else."""
    shape_open = r"\[|\(\(|\(\[|\(|\{"
    shape_close = r"\]\)|\)\)|\]|\)|\}"
    return (
        rf"(?P<{prefix}id>[A-Za-z_][\w-]{{0,39}})\s*"
        rf'(?:(?:{shape_open})\s*"(?P<{prefix}label>[^"]{{0,200}})"\s*(?:{shape_close}))?'
        rf"(?P<{prefix}role>:::[A-Za-z_][A-Za-z0-9_]{{0,23}})?"
    )


_ARROW = r"(?P<arrow>-->|---|-\.->|-\.-|==>)"
_EDGE_LABEL = r'(?:\|\s*"?(?P<elabel>[^"|]{0,120})"?\s*\|)?'
_EDGE_RE = re.compile(rf"^{_node_part('a')}\s*{_ARROW}\s*{_EDGE_LABEL}\s*{_node_part('b')}$")

#: MERMAID'S OTHER LEGAL EDGE LABEL, and why refusing it was expensive.
#:
#: `A -->|"Yes"| B` is the form DIAGRAM_INSTRUCTION teaches. `A -- Yes --> B`
#: is the form models actually write, it is equally legal mermaid, and nothing
#: in the instruction forbids it: the model has satisfied "one statement per
#: line" and "every label in double quotes" and still lost its whole diagram,
#: because one unreadable line makes `parse_mermaid` refuse the entire source
#: and md_import falls back to the "Diagram omitted" callout — the exact
#: defect this module exists to close. Measured over real model output:
#: 2 of 7 diagrams were lost to this line shape, on both the old prompt and
#: the new one. The grammar is the cheap side to fix, because
#: DIAGRAM_INSTRUCTION is at its byte ceiling.
#:
#: The label sits between the two halves of the arrow, and each arrow family
#: keeps its own style: `--`/`-->`/`---` solid, `-.`/`.->`/`.-` dashed,
#: `==`/`==>` solid-thick (drawn as solid, as `==>` already is). The label
#: class is bounded at 120 characters, as the `|...|` form is, so the
#: alternation cannot backtrack unboundedly.
_MID_LABEL = r'\s*"?(?P<{name}>[^"|]{{0,120}}?)"?\s*'
_MID_ARROW = (
    r"(?:--" + _MID_LABEL.format(name="mlabel_s") + r"(?P<mtail_s>-->|---)"
    r"|-\." + _MID_LABEL.format(name="mlabel_d") + r"(?P<mtail_d>\.->|\.-)"
    r"|==" + _MID_LABEL.format(name="mlabel_t") + r"(?P<mtail_t>==>)"
    r")"
)
_EDGE_MID_RE = re.compile(rf"^{_node_part('a')}\s*{_MID_ARROW}\s*{_node_part('b')}$")

#: A MID-LABEL MAY NOT CARRY ARROW OR STATEMENT PUNCTUATION, and this is the
#: guard that says so. `_MID_LABEL` excludes only `"` and `|`, so it is free to
#: SWALLOW a second arrow: the class is non-greedy, but backtracking widens it
#: until `_node_part('b')` can reach the end of the line. A CHAINED statement
#: was therefore read as ONE edge. Measured on this branch, 2026-09-27, before
#: this guard existed:
#:
#:     A -- yes --> B -- no --> C     -> one edge A->C labelled "yes --> B -- no"
#:     A --> B --> C --> D            -> one edge A->D labelled "> B --> C"
#:     A --> B; C --> D               -> one edge A->D labelled "> B; C"
#:     A == x ==> B == y ==> C        -> one edge A->C labelled "x ==> B == y"
#:     A -.-> B -.-> C                -> one edge A->C labelled "-> B -"
#:     A -- <b>html</b> --> B         -> an HTML label accepted as text
#:
#: In the declare-then-chain shape the model is taught to write — every label
#: quoted, every node given a role — four boxes came back with a single A->D
#: arrow: three real edges deleted, one false edge invented, two boxes left
#: orphaned, raw mermaid painted on the arrow, and `notes` empty, so nothing
#: told the reader anything was lost. That is the wrong picture this module
#: promises never to draw, and it is the same defect the `_DIR_RE` fix above
#: closed for a bare `flowchart` line.
#:
#: A chained statement is SEVERAL edges, not one, so a label holding `--`,
#: `-.`, `.-`, `==`, `<`, `>` or `;` means the line is not readable here and
#: the whole source refuses to the callout — which is exactly what these
#: shapes did before the mid-label form was accepted at all (`_EDGE_MID_RE`
#: does not exist on 381a62d, where every one of the six lines above is
#: refused). Reading a chain properly, as consecutive edges, is a real
#: capability and a separate change; drawing it wrong is not a substitute.
#: Hyphenated words are unaffected: "e-mail sent" holds no arrow.
#:
#: WHY `return None` AND NOT A FALL-THROUGH, corrected 2026-09-27. An earlier
#: note here said a fall-through "could read the leftovers as a node and put a
#: box in the drawing that nobody wrote", citing `_DECL_RE`'s id class. That
#: hazard is real (see `_ID_IS_AN_ARROW_RE` below, which closes it) but it is
#: NOT what these lines do: measured on all seven chained shapes,
#: `_DECL_RE.match(line)` fails on every one of them, so a fall-through would
#: have reached the loop's final `return None` anyway and the outcome is
#: identical. The reason to refuse HERE is the plain one: the line has been
#: read, it is understood to be several statements, and this module's promise
#: is that a source it cannot read as one picture becomes the callout. Naming
#: a hazard the guard never sees was the wrong reason for the right line.
#:
#: THE PROPOSED PROTOTYPE, AND WHY THE CLASS IS WIDER THAN IT. A prototype
#: put up during the 2026-09-27 review proposed this same guard with the
#: narrower class `--|==|\.-|->|<-` and a fall-through in place of the
#: refusal. Its IDEA is what is shipped here. Its class is not, and the
#: reason is measured, not asserted — run over `parse_mermaid` with only the
#: class swapped (the swap is a test, not a story:
#: tests/test_artifact_render_diagrams.py::
#: test_the_swallow_class_is_strictly_wider_than_the_proposed_prototype):
#:
#:     A -- yes --> B -- no --> C   prototype REFUSES     shipped REFUSES
#:     A --> B --> C --> D          prototype REFUSES     shipped REFUSES
#:     A == x ==> B == y ==> C      prototype REFUSES     shipped REFUSES
#:     A -.-> B -.-> C              prototype REFUSES     shipped REFUSES
#:     A --> B; C --> D             prototype DRAWS A->D labelled "> B; C"
#:     A -- <b>html</b> --> B       prototype DRAWS A->B labelled "<b>html</b>"
#:     A-->B-->C                    prototype DRAWS A->C labelled ">B"
#:
#: So the prototype closes four of the seven wrong pictures and leaves three
#: standing: it has no `;`, and its `->`/`<-` need the hyphen, so a bare `>`
#: or `<` passes — which is every chain written without spaces, every
#: semicolon-separated pair, and every HTML label. `-\.`, `<`, `>` and `;` in
#: the class below are exactly those three rows. An earlier justification for
#: rejecting the prototype described what it skips rather than what it draws;
#: it was wrong, and the table above is what replaced it.
_MID_LABEL_SWALLOWED_AN_ARROW_RE = re.compile(r"--|-\.|\.-|==|[<>;]")

#: A DASH RUN IS AN ARROW, NOT THE INSIDE OF A NAME.
#:
#: (Narrowed 2026-09-28. This block used to be headed "AN ID IS A NAME, NOT AN
#: ARROW THAT LOST ITS SPACES" and to say the hole was closed "at every site an
#: id comes from". Both were wider than this regex: it closes DASH RUNS only,
#: and an arrowhead that lost its space — `A---oB` — walked straight through
#: it. That family is closed below, by `_arrowhead_became_the_target`, and the
#: two guards together are what the old heading claimed.)
#:
#: `_node_part` admits `-` inside an id, because mermaid does: `api-gateway`
#: is a legal node id and is written in real sources. The cost of that, found
#: by the 2026-09-27 review and measured here before the fix:
#:
#:     A--B        -> ONE node whose id and label are both "A--B"   (_DECL_RE)
#:     A--B---C    -> an edge FROM a node called "A--B"             (_EDGE_RE)
#:     A----B      -> an edge from a node called "A-" to "B"        (_EDGE_RE)
#:
#: mermaid reads all three as links (`--`, `----` are open links of different
#: lengths); this grammar read the extra dashes as part of a name and drew a
#: box nobody wrote — the same class of defect as the bare `flowchart` line
#: above, and the same broken promise. It is NOT reached through
#: `_MID_LABEL_SWALLOWED_AN_ARROW_RE`: that guard sees only lines that matched
#: `_EDGE_MID_RE`, and none of these three does. So it is closed at all three
#: sites a `-` can reach an id — `_EDGE_RE`, `_EDGE_MID_RE` and `_DECL_RE` —
#: rather than beside that guard.
#:
#: A single inner hyphen stays legal, which is the whole point: `api-gateway`
#: and `my-node` pass, `A--B`, `-A` and `A-` do not. `A---B` keeps working as
#: mermaid's `---` link, because the arrow consumes all three dashes and the
#: ids either side are `A` and `B`.
_ID_IS_AN_ARROW_RE = re.compile(r"^-|--|-$")


def _ids_are_names(*ids: Optional[str]) -> bool:
    """True when every id a matched line produced is a name, not a dash run."""
    return not any(_ID_IS_AN_ARROW_RE.search(i) for i in ids if i)


#: AN ARROWHEAD IS NOT THE FIRST LETTER OF THE TARGET'S NAME.
#:
#: mermaid's links may END in `o` or `x` as well as `>` — a circle or a cross
#: drawn at the target end — and the head belongs to the LINK whether or not a
#: space follows it: its own lexer rules are
#: /^(?:\s*[xo<]?--+[-xo>]\s*)/ and /^(?:\s*[xo<]?-?\.+-[xo>]?\s*)/
#: (frontend/node_modules/mermaid/dist/chunks/mermaid.core/chunk-RHFEMEQ7.mjs,
#: 11.17.0). `_ARROW` and `_MID_ARROW` stop at the last dash, so the `o`/`x`
#: fell through to `_node_part('b')` and became the first letter of a name.
#: `_ID_IS_AN_ARROW_RE` cannot see it: `oB` holds no dash.
#:
#: MEASURED 2026-09-28 on d9edd8a7, beside what mermaid 11.17.0 itself does —
#: not read off its regexes but RUN: `diagram.parser.parse()` out of the
#: chunk above, under node with `frontend/node_modules/jsdom` supplying the
#: DOM its label sanitiser needs, reading back `db.getVertices()` and
#: `db.getEdges()`. The wrong picture was also read in the pixels first —
#: `A["Ingest"]:::service---oB["Index"]` with `B --> C["Answer"]` drew FOUR
#: boxes, one of them labelled literally "B", where mermaid draws three:
#:
#:     source              mermaid 11.17.0        this grammar, before
#:     A---oB              A -> B, circle head    A -> a node called "oB"
#:     A---xB              A -> B, cross head     A -> a node called "xB"
#:     A-.-oB              A -> B, dotted         A -> "oB", dashed
#:     A-..-oB             A -> B, dotted         A -> "oB", dashed
#:     A-- yes ---oB       A -> B "yes"           A -> "oB" "yes"
#:     A-.yes.-oB          A -> B "yes" dotted    A -> "oB" "yes"
#:     A---o               PARSE ERROR            A -> a node called "o"
#:     A---oscar           A -> a node "scar"     A -> a node "oscar"
#:     A---xylophone       A -> a node "ylophone" A -> "xylophone"
#:
#: WHY THIS REFUSES RATHER THAN DRAWING mermaid'S PICTURE. Mirroring the two
#: lexer rules was tried first and rejected on the last two rows: mermaid
#: reads `A---oscar` as an edge to a node called "scar", so copying it trades
#: one box nobody wrote for another. The source does not say which the author
#: meant, and a grammar that has to guess is exactly the case this module
#: answers with the callout (`style`, `subgraph`, an HTML label and a chain
#: all refuse here while the browser draws them happily). Refusing also costs
#: no correct capability, because the unspaced form is the ONLY one in doubt:
#: `A --- oB`, `A --> oscar` and `A---|"n"|oB` keep their o-initial targets,
#: and mermaid agrees with us on all three. The price is recorded rather than
#: hidden: `api---orders` now loses its whole diagram to the callout where it
#: used to draw api -> orders, and a source written with the spaces mermaid's
#: own examples use never reaches this guard.
#:
#: WHAT IS LEFT, stated narrowly this time. After this, the id class
#: `[A-Za-z_][\w-]` admits exactly two characters mermaid can also read as
#: part of a link — `-` (above) and the arrowheads `o`/`x` (here). Every other
#: link character (`.`, `=`, `<`, `>`, `|`, `&`) is outside the class, so such
#: a line matches nothing and the source refuses: `A----B`, `A==B`, `A==oB`,
#: `A<--B`, `Ao--oB` and `A o--o B` are all callouts today, and a
#: LEFT-hand head that abuts its source (`Ao---B`, `Ax--xB`) is read as the id
#: `Ao`/`Ax` by mermaid too, so it is not this defect.
def _arrowhead_became_the_target(m: "re.Match[str]", *arrow_groups: str) -> bool:
    """True when the target id begins with the `o` or `x` of an arrowhead.

    The head is only in doubt when it ABUTS the target: `m.start("bid")` has
    to be exactly where the arrow ended, so `A --- oB` (spaced) and
    `A---|"n"|oB` (an explicit label between them) are names and stay legal.
    """
    bid = m.group("bid")
    if not bid or bid[0] not in ("o", "x"):
        return False
    for name in arrow_groups:
        tail = m.group(name)
        if tail is None:
            continue
        # Only a link whose body ENDS in a dash can take an `o`/`x` head:
        # `-->`, `-.->` and `==>` are already complete at the `>`.
        return tail.endswith("-") and m.end(name) == m.start("bid")
    return False


_DECL_RE = re.compile(rf"^{_node_part('a')}$")


# ------------------------------------------- other heads, same picture --
#
# ELEVEN OF THE TWELVE KINDS THE BROWSER DRAWS BECAME "Diagram omitted"
# (2026-09-28). `_DIR_RE` accepts `flowchart`/`graph`, so a source opening
# `erDiagram`, `stateDiagram-v2` or `mindmap` refused at its first line and
# `md_import` substituted a callout. Measured that day by calling
# `parse_mermaid` on twelve real sources: `flowchart` parsed; the other eleven
# were all refused. So "a docx of the schema with an ER diagram" and "export
# the roadmap as a pdf with a mind map" routed perfectly — 7/7 on the
# diagram-in-file label — and then shipped an apology where the picture goes.
#
# THREE OF THEM ARE THE SAME PICTURE IN DIFFERENT WORDS. An `erDiagram`, a
# `stateDiagram` and a `mindmap` are each literally a set of named nodes and
# labelled edges between them, which is exactly what the flowchart layout in
# this module draws. Rewriting one into the flowchart grammar loses the shape of
# the boxes and nothing else — no arrow moves, no label changes, no edge is
# invented — so the picture a reader sees is the picture the model drew.
#
# THE OTHER EIGHT ARE NOT, AND ARE NOT TRANSLATED. A `sequenceDiagram` has a
# time axis, activations and loops; a `gantt` has dates on a scale; a `journey`
# has a rating per step; `classDiagram`, `gitGraph`, `kanban`, `timeline` and
# `quadrantChart` each carry something a box-and-arrow layout cannot hold.
# Drawing them as flowcharts would be drawing a DIFFERENT picture, which this
# module's promise forbids — so they keep the callout, and `md_import` now names
# the kind in it instead of apologising anonymously.

#: The heads this module can rewrite, and what each one is.
TRANSLATABLE = ("erdiagram", "statediagram", "statediagram-v2", "mindmap")

#: The heads it cannot, with the reason a reader is given. Read by md_import.
UNTRANSLATABLE_REASON: Dict[str, str] = {
    "sequencediagram": "a sequence diagram carries a time axis that a page of boxes and arrows cannot hold",
    "classdiagram": "a class diagram carries fields and methods inside each box",
    "gantt": "a Gantt chart needs dates on a scale",
    "journey": "a user journey carries a rating for every step",
    "timeline": "a timeline needs dates on a scale",
    "gitgraph": "a git graph carries branch positions",
    "kanban": "a kanban board carries cards in columns",
    "quadrantchart": "a quadrant chart needs two axes",
    "pie": "a pie chart needs its slice values",
    "radar": "a radar chart needs an axis per score",
    "sankey": "a Sankey diagram needs its flow volumes",
    "treemap": "a treemap needs nested areas",
    "block": "a block diagram carries its own stacking",
    "packet": "a packet diagram carries bit offsets",
    "c4context": "a C4 context diagram carries its own notation",
    "requirementdiagram": "a requirement diagram carries the type of each link",
    "xychart": "an xy chart needs plotted values",
    "architecture": "an architecture-beta diagram carries its own notation",
}


def head_of(source: str) -> str:
    """The mermaid head of a source, lowercased, or "".

    Reads past a `%%{…}%%` directive and a `---`-fenced frontmatter block the
    same way `frontend/lib/mermaid.ts withoutPreamble` does, so this module and
    the browser agree on what the first line IS.
    """
    text = re.sub(r"%%\{[\s\S]*?\}%%", "", source or "")
    text = re.sub(r"^\s*---[ \t]*\r?\n[\s\S]*?\r?\n[ \t]*---[ \t]*(\r?\n|$)", "", text)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("%%"):
            continue
        return re.split(r"[\s]", line, 1)[0].strip().lower()
    return ""


#: An erDiagram relationship: `USERS ||--o{ SESSIONS : has`. The cardinality
#: glyphs are a closed set in mermaid and are read as one token; the label after
#: the colon is the relationship's own name and is kept verbatim.
_ER_REL_RE = re.compile(
    r"^(?P<a>[A-Za-z_][\w-]{0,39})\s+"
    r"(?P<card>[|{}o<>\-.]{2,8})\s+"
    r"(?P<b>[A-Za-z_][\w-]{0,39})\s*(?::\s*(?P<label>[^\n]{0,120}?))?\s*$"
)
#: A state transition: `Queued --> Running : picked up`, `[*] --> Queued`.
_STATE_RE = re.compile(
    r"^(?P<a>\[\*\]|[A-Za-z_][\w-]{0,39})\s*(?P<arrow>-->|-\.->)\s*"
    r"(?P<b>\[\*\]|[A-Za-z_][\w-]{0,39})\s*(?::\s*(?P<label>[^\n]{0,120}?))?\s*$"
)
#: `state Queued as "Waiting to run"` / `state "Waiting to run" as Queued`.
_STATE_DECL_RE = re.compile(
    r'^state\s+(?:"(?P<qlabel>[^"]{0,120})"\s+as\s+(?P<qid>[A-Za-z_][\w-]{0,39})'
    r'|(?P<id>[A-Za-z_][\w-]{0,39})\s+as\s+"?(?P<label>[^"\n]{0,120}?)"?)\s*$'
)
#: A mindmap node: the indentation is the tree, and the label may wear any of
#: mermaid's shape brackets.
#:
#: TWO ALTERNATIVES, NOT ONE OPTIONAL ID, and the reason is a bug the rendered
#: PNG showed that no assertion in this file would have. Written as one branch
#: with an OPTIONAL id in front of an OPTIONAL bracket, the id group happily
#: matched the first WORD of a bracket-less label and the rest became the label:
#:
#:     "Upload reliability"  -> id="Upload"    label="reliability"
#:     "Fast lane"           -> id="Fast"      label="lane"
#:     "Diagrams in files"   -> id="Diagrams"  label="in files"
#:
#: Every multi-word branch of the mind map was drawn under its LAST word, and
#: the picture was wrong in a way only looking at it could show. An id exists in
#: mermaid only when a bracket follows it (`root((Roadmap))`, `a[Feature A]`),
#: so that is its own branch now and a line without brackets is a plain label,
#: whole.
_MIND_RE = re.compile(
    # `root((Roadmap))`, `a[Feature A]`, `x{Decision}` — an id THEN a bracket.
    r"^(?P<indent>[ \t]*)(?:"
    r"(?P<id>[A-Za-z_][\w-]{0,39})\s*(?:\(\(|\[\[|\[|\(|\{\{|\{)\s*"
    r"(?P<label>[^\n]*?)\s*(?:\)\)|\]\]|\]|\)|\}\}|\})\s*"
    # …or the whole line, brackets and all absent.
    r"|(?P<plain>[^\n]+?)\s*"
    r")$"
)


#: `spec.DiagramNode.label` is `max_length=48` and `spec.DiagramEdge.label`
#: `max_length=24`. A rewrite that hands either a longer string gets the whole
#: diagram rejected by pydantic and the picture is lost to the callout it was
#: rewritten to avoid, so the two ceilings are enforced HERE, where the text is
#: still in hand and can be cut with an ellipsis a reader understands.
_NODE_LABEL_CHARS = 48
_EDGE_LABEL_CHARS = 24


def _q(text: str, limit: int = _NODE_LABEL_CHARS) -> str:
    """A label safe inside the flowchart grammar this module reads back: no
    quote, pipe, bracket or arrow punctuation, and short enough for `spec`."""
    out = re.sub(r'["|\[\]{}()<>]', " ", (text or "").replace("-->", " ").replace("--", " "))
    out = " ".join(out.split())
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "\u2026"


def as_flowchart(source: str) -> Optional[Tuple[str, str]]:
    """An `erDiagram`, `stateDiagram` or `mindmap` rewritten as a `flowchart`.

    Returns `(flowchart_source, note)` — the note naming anything the rewrite
    left behind, for `md_import` to put in the document's notes — or None when
    the head is not one of the three or the body cannot be read completely.

    NOTHING IS GUESSED. A line this function does not recognise makes it return
    None, exactly as `parse_mermaid` does, so the callout is still what a source
    this module cannot read becomes.
    """
    head = head_of(source)
    if head not in TRANSLATABLE or not source or len(source) > 20_000:
        return None
    lines = [l.rstrip() for l in source.splitlines()]
    body = [l for l in lines[1:] if l.strip() and not l.strip().startswith("%%")]
    if head == "mindmap":
        return _mindmap_as_flowchart(body)
    if head == "erdiagram":
        return _er_as_flowchart(body)
    return _state_as_flowchart(body)


def rewrite_note(source: str) -> str:
    """What `as_flowchart` left behind for this source, as a sentence, or "".

    `md_import` appends it to the document's notes after a successful read, so a
    reader who gets the picture is also told what the picture does not carry.
    """
    out = as_flowchart(source)
    return out[1] if out else ""


def _er_as_flowchart(body: List[str]) -> Optional[Tuple[str, str]]:
    # TD, NOT LR, and the rendered PNG is why. Four entities in a chain laid out
    # left to right filled the 6.3-inch box edge to edge: the boxes butted
    # together, two arrowheads disappeared between them, and the "contains" and
    # "produces" edge labels were painted over the box borders. The same graph
    # downward has room for every label — compare `_state_as_flowchart`, which
    # was TD from the start and reads cleanly at six nodes.
    out: List[str] = ["flowchart TD"]
    dropped = 0
    depth = 0
    for raw in body:
        line = raw.strip().rstrip(";")
        if depth:
            # Inside an attribute block. Mermaid puts a table's COLUMNS here,
            # which a flowchart box has nowhere to show, so they are counted
            # and reported rather than silently lost.
            if line == "}":
                depth = 0
            continue
        if line.endswith("{"):
            depth = 1
            dropped += 1
            continue
        m = _ER_REL_RE.match(line)
        if not m:
            return None
        label = _q(m.group("label") or "", _EDGE_LABEL_CHARS)
        a, b = m.group("a"), m.group("b")
        out.append(f'{a}["{_q(a)}"] -->|"{label}"| {b}["{_q(b)}"]' if label
                   else f'{a}["{_q(a)}"] --> {b}["{_q(b)}"]')
    if len(out) < 2:
        return None
    note = ("An ER diagram was drawn as boxes and arrows; the column lists inside "
            f"{dropped} of its tables are not shown." if dropped else "")
    return "\n".join(out), note


def _state_as_flowchart(body: List[str]) -> Optional[Tuple[str, str]]:
    out: List[str] = ["flowchart TD"]
    labels: Dict[str, str] = {}
    seen_terminal = False
    for raw in body:
        line = raw.strip().rstrip(";")
        if line in ("}", "{") or line.startswith(("note ", "end note", "direction ")):
            # `direction LR` is a hint the flowchart head already carries; a
            # composite state's braces and a note are structure this layout has
            # nowhere to put, so a source using them is refused rather than
            # flattened.
            if line.startswith("direction "):
                continue
            return None
        d = _STATE_DECL_RE.match(line)
        if d:
            nid = d.group("qid") or d.group("id")
            labels[nid] = _q(d.group("qlabel") or d.group("label") or nid)
            continue
        m = _STATE_RE.match(line)
        if not m:
            return None
        ids = []
        for side in ("a", "b"):
            val = m.group(side)
            if val == "[*]":
                # mermaid's start/end marker. Which one it is depends on the
                # side it sits on, and both get a box a reader can name.
                nid = "START" if side == "a" else "DONE"
                labels.setdefault(nid, "start" if side == "a" else "end")
                seen_terminal = True
            else:
                nid = val
                labels.setdefault(nid, _q(val))
            ids.append(nid)
        label = _q(m.group("label") or "", _EDGE_LABEL_CHARS)
        arrow = "-.->" if m.group("arrow") == "-.->" else "-->"
        a, b = ids
        out.append(f'{a}["{labels[a]}"] {arrow}|"{label}"| {b}["{labels[b]}"]' if label
                   else f'{a}["{labels[a]}"] {arrow} {b}["{labels[b]}"]')
    if len(out) < 2:
        return None
    note = ("A state diagram was drawn as boxes and arrows; its start and end markers "
            "are shown as boxes." if seen_terminal else "")
    return "\n".join(out), note


def _mindmap_as_flowchart(body: List[str]) -> Optional[Tuple[str, str]]:
    """A mindmap's INDENTATION is its tree, so the rewrite is a stack.

    Each line is a node; its parent is the nearest line above it with less
    indentation. mermaid draws this as a radial map and the flowchart layout
    draws it left to right, which is the same tree read the same way.
    """
    out: List[str] = ["flowchart LR"]
    stack: List[Tuple[int, str]] = []          # (indent, node id)
    used = 0
    for raw in body:
        m = _MIND_RE.match(raw)
        if not m:
            return None
        label = _q(m.group("label") or m.group("plain") or m.group("id") or "")
        if not label:
            # A bracket on a line of its own is not a node and not something
            # this reader understands.
            return None
        indent = len(m.group("indent").replace("\t", "    "))
        used += 1
        nid = f"m{used}"
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if stack:
            out.append(f'{stack[-1][1]}["{stack[-1][1]}"] --> {nid}["{label}"]'
                       if False else f'{stack[-1][1]} --> {nid}["{label}"]')
        else:
            out.append(f'{nid}["{label}"]')
        stack.append((indent, nid))
    if used < 2:
        return None
    return "\n".join(out), ""


def parse_mermaid(source: str, *, max_nodes: int = 24, max_edges: int = 40) -> Optional[Dict[str, Any]]:
    """A ```mermaid fence → the fields of a `spec.Diagram`, or None.

    NOTHING IS EXECUTED AND NOTHING IS FETCHED. This is a regular-expression
    reader over a closed grammar: a direction line, `A["label"]:::role`
    declarations and `A --> B` edges with an optional `|"label"|`. A line it
    does not recognise — a `style`, `classDef`, `linkStyle`, `click` or
    `%%{init}%%` directive, a subgraph, an HTML label, anything at all —
    makes the whole source refuse by returning None, and the caller keeps its
    text fallback. A partly understood graph is never drawn.

    The returned dict is passed to `spec.Diagram`, which validates it; this
    function does not decide what is legal, only what it can read.
    """
    if not source or len(source) > 20_000:
        return None
    from . import mermaid_grammars as G

    # FRONTMATTER: `---\ntitle: X\n---` at the very top is mermaid's way of
    # titling any diagram. Only `title:` is read; a `config:` block or
    # anything else there is a directive by another name and refuses.
    src_lines = source.splitlines()
    title = ""
    if src_lines and src_lines[0].strip() == "---":
        end = next((i for i in range(1, len(src_lines)) if src_lines[i].strip() == "---"), None)
        if end is None:
            return None
        for fm in src_lines[1:end]:
            fm = fm.strip()
            if not fm:
                continue
            if not fm.startswith("title:"):
                return None
            title = fm[6:].strip().strip('"').strip("'")
        src_lines = src_lines[end + 1:]

    # THE HEADER DECIDES THE GRAMMAR. A source whose first statement is a
    # family this module reads goes to that family's reader; a keyword it
    # does not read (gantt, pie, journey, gitGraph, ...) refuses HERE, at the
    # header, rather than falling through to `_DECL_RE` below and becoming a
    # box labelled "gantt" — which is what it did until 2026-09-28 (measured
    # in the running container: `classDiagram\nA --> B\nB --> C` drew a
    # fourth box called "classDiagram"). A headerless source stays a
    # flowchart, as it always was.
    if any(l.strip().startswith("%%{") for l in src_lines):
        # `%%{init: ...}%%` is a directive wherever it sits. The flowchart
        # loop below refuses it line by line; a family reader only sees the
        # lines AFTER its header, so a directive placed before the header
        # walked past both until this check (found by
        # tests/test_mermaid_grammars.py on 2026-09-28).
        return None
    first_index = next((i for i, l in enumerate(src_lines) if l.strip() and not l.strip().startswith("%%")), None)
    if first_index is not None:
        first = src_lines[first_index].strip().rstrip(";")
        keyword = G.header_keyword(first)
        if keyword is not None and keyword not in ("flowchart", "graph"):
            family = G.FAMILY_OF_KEYWORD.get(keyword)
            fields = None
            if family is not None and first.lower() == keyword:
                fields = G.read_family(family, src_lines[first_index + 1:])
            if fields is None:
                # TRANSLATION IS THE SAFETY NET, never the first choice. A
                # family with its own reader is drawn in its own shapes (ER
                # crow's feet, state bullseyes, sequence lifelines); only when
                # that reader cannot read this instance does `as_flowchart`
                # restate an `erDiagram`, `stateDiagram` or `mindmap` in the
                # flowchart grammar below, without moving an arrow or changing
                # a label. Two designs for one job landed on parallel branches
                # and the picture, not the branch, decides: a native drawing
                # beats a translation, and a translation beats the plain
                # callout a refusal leaves behind.
                if head_of(source) in TRANSLATABLE:
                    rewritten = as_flowchart(source)
                    if rewritten is not None:
                        # The note does NOT travel in this dict: `spec.Diagram`
                        # is strict and an extra key makes it raise, which
                        # would lose the very picture the rewrite exists to
                        # keep. `rewrite_note` is how the caller asks for it.
                        return parse_mermaid(rewritten[0], max_nodes=max_nodes, max_edges=max_edges)
                return None
            if title and not fields.get("title"):
                fields["title"] = title
            return _within_caps(fields)
    source = "\n".join(src_lines)

    direction = "TD"
    order: List[str] = []
    labels: Dict[str, str] = {}
    roles: Dict[str, str] = {}
    edges: List[Dict[str, Any]] = []

    def touch(nid: str, label: Optional[str], role: Optional[str]) -> None:
        if nid not in labels:
            order.append(nid)
            labels[nid] = (label or nid).strip()
        elif label:
            labels[nid] = label.strip()
        if role:
            name = role[3:].lower()
            if name in DIAGRAM_ROLES:
                roles[nid] = name

    for raw in source.splitlines():
        line = raw.strip()
        if not line or line.startswith("%%"):
            # A bare comment is skipped; `%%{init}%%` is a directive and is
            # NOT a comment — it starts with "%%{" and is refused below.
            if line.startswith("%%{"):
                return None
            continue
        line = line.rstrip(";")
        m = _DIR_RE.match(line)
        if m:
            # The group is optional now, so a bare `flowchart` means TD —
            # mermaid's own default. Without the `or`, this raised
            # AttributeError on None.
            got = (m.group(1) or "TD").upper()
            direction = "LR" if got in ("LR", "RL") else "TD"
            continue
        m = _EDGE_RE.match(line)
        if m:
            g = m.groupdict()
            if not _ids_are_names(g["aid"], g["bid"]):
                return None
            if _arrowhead_became_the_target(m, "arrow"):
                return None
            touch(g["aid"], g["alabel"], g["arole"])
            touch(g["bid"], g["blabel"], g["brole"])
            edges.append({
                "source": g["aid"],
                "target": g["bid"],
                "label": (g["elabel"] or "").strip(),
                "style": "dashed" if g["arrow"].startswith("-.") else "solid",
            })
            continue
        m = _EDGE_MID_RE.match(line)
        if m:
            # The same edge, written with the label inside the arrow. Only one
            # of the three alternatives can have matched, so exactly one label
            # group and one tail group are not None.
            g = m.groupdict()
            label = next((g[k] for k in ("mlabel_s", "mlabel_d", "mlabel_t") if g[k] is not None), "")
            if _MID_LABEL_SWALLOWED_AN_ARROW_RE.search(label):
                # The label ate a second arrow, so this line is a chain or
                # several statements rather than one edge, and a chain drawn
                # as one edge is the wrong picture. Refuse the whole source:
                # this module's promise is the callout, never a half-read
                # graph. (A fall-through would land on the loop's own
                # `return None` for every shape measured — see the regex's
                # note; refusing here says so at the point it is decided.)
                return None
            if not _ids_are_names(g["aid"], g["bid"]):
                return None
            if _arrowhead_became_the_target(m, "mtail_s", "mtail_d", "mtail_t"):
                return None
            touch(g["aid"], g["alabel"], g["arole"])
            touch(g["bid"], g["blabel"], g["brole"])
            edges.append({
                "source": g["aid"],
                "target": g["bid"],
                "label": (label or "").strip(),
                "style": "dashed" if g["mtail_d"] is not None else "solid",
            })
            continue
        m = _DECL_RE.match(line)
        if m:
            g = m.groupdict()
            if not _ids_are_names(g["aid"]):
                return None
            # NO KEYWORD CHECK HERE, deliberately. Mermaid's flowchart lexer
            # reserves no words, so `requirement`, `block`, `kanban`, `pie`,
            # `info` and `Info` declared on their own line are all NODES in the
            # browser, and main (ae25da28) drew every one of them. The header
            # dispatch above already sends a source whose FIRST statement is a
            # keyword to that keyword's own reader, which is the only place the
            # spelling can mean a grammar. A bare-keyword refusal was tried on
            # 2026-09-28 and measured against main: it refused five flowcharts
            # main drew (`flowchart TD / requirement / design / requirement -->
            # design`, `block`, `kanban`, `pie`, `info`), and declaring nodes on
            # their own lines first is a common habit of the model that writes
            # them. A lost picture is worse than a box named after a keyword.
            touch(g["aid"], g["alabel"], g["arole"])
            continue
        return None

    if len(order) < 2 or not edges:
        return None
    if len(order) > max_nodes or len(edges) > max_edges:
        return None
    return _within_caps({
        "title": title,
        "direction": direction,
        # `roles` holds only the names the closed vocabulary knows, so a node
        # nobody tagged — and a node tagged with a name nobody defined —
        # arrives as `kind=None` and is drawn NEUTRAL. That is what
        # DIAGRAM_INSTRUCTION promises the model ("Tag only the nodes one
        # fits; an invented name paints nothing") and what the browser's
        # mermaid already did with an undefined `:::class`. It used to say
        # "service" here, so an untagged box came back painted as a service
        # and the figure's legend named a role the model never wrote.
        "nodes": [{"id": nid, "label": labels[nid], "kind": roles.get(nid)} for nid in order],
        "edges": edges,
    })


def _within_caps(fields: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """`fields`, or None when the typed model refuses them: a label past its
    cap, more entities or steps than spec.py allows. The READER refuses an
    over-cap source, like any other it cannot carry, so the caller sees the
    ordinary None and keeps its callout. Until 2026-09-28 the pydantic
    ValidationError escaped `diagram_from_fields`, md_import caught it and
    logged a "parsed but did not build" bug line for what is a size refusal.
    """
    if fields is None:
        return None
    from pydantic import ValidationError

    from ..spec import diagram_from_fields

    try:
        diagram_from_fields(fields)
    except ValidationError:
        return None
    return fields


# ---------------------------------------------------------------- layout --


def _wrap(label: str, width: int) -> List[str]:
    """`label` broken into at most three lines of about `width` characters.
    A single word longer than the width is hard-split rather than left to
    stick out of its box."""
    words = (label or "").split()
    if not words:
        return [""]
    lines: List[str] = []
    cur = ""
    for word in words:
        while len(word) > width:
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[: width - 1] + "-")
            word = word[width - 1:]
        candidate = f"{cur} {word}".strip()
        if len(candidate) <= width or not cur:
            cur = candidate
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if len(lines) > _MAX_LABEL_LINES:
        head = lines[: _MAX_LABEL_LINES - 1]
        tail = " ".join(lines[_MAX_LABEL_LINES - 1:])
        head.append(tail if len(tail) <= width else tail[: width - 1] + "…")
        lines = head
    return lines


def _char_in() -> float:
    return FONT_PT * CHAR_EM / 72.0


def _line_in() -> float:
    return FONT_PT * LINE_SPACING / 72.0


def _feedback_arcs(nodes: Dict[str, _Node], edges: List[_Edge]) -> Set[int]:
    """Indices of the edges to REVERSE so the graph can be layered.

    Depth-first back-edge detection: an edge into a node still on the stack
    closes a cycle. Without this step a cyclic graph layers as if the cycle
    were one node and the nodes draw on top of each other.
    """
    succ: Dict[str, List[Tuple[str, int]]] = {k: [] for k in nodes}
    for i, e in enumerate(edges):
        if e.self_loop or e.src not in nodes or e.dst not in nodes:
            continue
        succ[e.src].append((e.dst, i))
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {k: WHITE for k in nodes}
    back: Set[int] = set()
    for root in nodes:
        if colour[root] != WHITE:
            continue
        colour[root] = GREY
        stack: List[Tuple[str, Any]] = [(root, iter(succ[root]))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for dst, idx in it:
                if colour[dst] == GREY:
                    back.add(idx)
                elif colour[dst] == WHITE:
                    colour[dst] = GREY
                    stack.append((dst, iter(succ[dst])))
                    advanced = True
                    break
            if not advanced:
                colour[node] = BLACK
                stack.pop()
    return back


def _layer(nodes: Dict[str, _Node], edges: List[_Edge]) -> None:
    """Longest-path layering over the acyclic graph. Every component starts
    at layer 0, so a disconnected graph lays out side by side rather than
    losing a component off the top."""
    succ: Dict[str, Set[str]] = {k: set() for k in nodes}
    indeg: Dict[str, int] = {k: 0 for k in nodes}
    for e in edges:
        if e.self_loop or e.src not in nodes or e.dst not in nodes:
            continue
        a, b = (e.dst, e.src) if e.reversed_ else (e.src, e.dst)
        if a == b or b in succ[a]:
            continue
        succ[a].add(b)
        indeg[b] += 1
    queue = [k for k in nodes if indeg[k] == 0]
    remaining = dict(indeg)
    for k in nodes:
        nodes[k].layer = 0
    seen = 0
    while queue:
        k = queue.pop(0)
        seen += 1
        for s in succ[k]:
            nodes[s].layer = max(nodes[s].layer, nodes[k].layer + 1)
            remaining[s] -= 1
            if remaining[s] == 0:
                queue.append(s)
    if seen != len(nodes):  # pragma: no cover - feedback-arc removal prevents it
        raise DiagramError("the graph still has a cycle after feedback-arc removal")


def _add_dummies(nodes: Dict[str, _Node], edges: List[_Edge]) -> None:
    """A node per intermediate layer for every edge spanning more than one,
    so a long edge is routed through the layers it crosses instead of cutting
    across the boxes that sit between them."""
    made = 0
    for e in edges:
        if e.self_loop or e.src not in nodes or e.dst not in nodes:
            continue
        a, b = (e.dst, e.src) if e.reversed_ else (e.src, e.dst)
        la, lb = nodes[a].layer, nodes[b].layer
        if lb - la <= 1:
            continue
        chain: List[str] = []
        for layer in range(la + 1, lb):
            made += 1
            nid = f"__via{made}"
            nodes[nid] = _Node(nid=nid, label="", role="", layer=layer, dummy=True, w=0.08, h=0.08)
            chain.append(nid)
        e.via = chain if not e.reversed_ else list(reversed(chain))


def _order(nodes: Dict[str, _Node], edges: List[_Edge], sweeps: int = 6) -> Dict[int, List[str]]:
    """Barycentre crossing reduction, dummies included."""
    layers: Dict[int, List[str]] = {}
    for k, n in nodes.items():
        layers.setdefault(n.layer, []).append(k)
    for lv in layers:
        layers[lv].sort()
        for i, k in enumerate(layers[lv]):
            nodes[k].order = float(i)

    preds: Dict[str, List[str]] = {k: [] for k in nodes}
    succs: Dict[str, List[str]] = {k: [] for k in nodes}
    for e in edges:
        if e.self_loop:
            continue
        a, b = (e.dst, e.src) if e.reversed_ else (e.src, e.dst)
        if a not in nodes or b not in nodes:
            continue
        chain = [a] + list(e.via) + [b]
        for u, v in zip(chain, chain[1:]):
            succs[u].append(v)
            preds[v].append(u)

    keys = sorted(layers)
    for sweep in range(sweeps):
        down = sweep % 2 == 0
        seq = keys[1:] if down else list(reversed(keys[:-1]))
        rel = preds if down else succs
        for lv in seq:
            for k in layers[lv]:
                nbrs = [nodes[p].order for p in rel[k] if p in nodes]
                if nbrs:
                    nodes[k].order = sum(nbrs) / len(nbrs)
            layers[lv].sort(key=lambda k: (nodes[k].order, k))
            for i, k in enumerate(layers[lv]):
                nodes[k].order = float(i)
    return layers


def _segments(nodes: Dict[str, _Node], edges: List[_Edge]) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """Every drawn edge as straight segments in figure coordinates."""
    out: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    for e in edges:
        if e.self_loop or e.src not in nodes or e.dst not in nodes:
            continue
        chain = [nodes[e.src]] + [nodes[v] for v in e.via if v in nodes] + [nodes[e.dst]]
        pts = [(n.x, n.y) for n in chain]
        out.extend(zip(pts, pts[1:]))
    return out


def _crosses(p: Tuple[float, float], q: Tuple[float, float], r: Tuple[float, float], s: Tuple[float, float]) -> bool:
    def side(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)

    if p in (r, s) or q in (r, s):
        return False        # segments that share an endpoint fan out, they do not cross
    d1, d2 = side(p, q, r), side(p, q, s)
    d3, d4 = side(r, s, p), side(r, s, q)
    return d1 * d2 < 0 and d3 * d4 < 0


def _count_crossings(nodes: Dict[str, _Node], edges: List[_Edge]) -> int:
    """Edge crossings COUNTED ON THE DRAWN COORDINATES, not on the ordering
    indices. The first version counted index inversions per layer pair, which
    reported zero for the 15-node architecture graph while the picture plainly
    had crossings: a layer split into sub-rows keeps one flat index sequence,
    so the indices no longer say where anything is. Whatever is counted here
    is what a reader sees, which is the only version worth optimising."""
    segs = _segments(nodes, edges)
    total = 0
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            if _crosses(segs[i][0], segs[i][1], segs[j][0], segs[j][1]):
                total += 1
    return total


def _size_nodes(nodes: Dict[str, _Node], wrap: int) -> None:
    char_in, line_in = _char_in(), _line_in()
    for n in nodes.values():
        if n.dummy:
            continue
        n.lines = _wrap(n.label, wrap)
        longest = max((len(line) for line in n.lines), default=1)
        n.w = max(MIN_NODE_W_IN, longest * char_in + 2 * PAD_X_IN)
        n.h = len(n.lines) * line_in + 2 * PAD_Y_IN


def _split_wide_layers(nodes: Dict[str, _Node], direction: str, budget: float) -> bool:
    """A layer too wide for the page becomes CONSECUTIVE LAYERS.

    A five-way fan-out is 8.7 in across at full label size, which a 6.3 in
    page can only answer by scaling the whole picture — and a scaled picture
    is exactly the 6 pt label the owner complained about. Splitting the layer
    instead keeps every label at full size.

    The split happens before the dummy nodes and before the ordering sweeps,
    so an edge that now spans two layers gets its dummy and is routed round
    the boxes in between. The first version wrapped a layer into sub-rows
    inside the placement step instead; that left the ordering indices
    describing a layout that no longer existed, and the 15-node architecture
    graph came out with edges crossing boxes while the crossing counter
    reported zero.

    Returns True when anything moved.
    """
    horizontal = direction == "LR"
    layers: Dict[int, List[str]] = {}
    for k, n in nodes.items():
        layers.setdefault(n.layer, []).append(k)
    moved = False
    shift = 0
    for lv in sorted(layers):
        ks = sorted(layers[lv], key=lambda k: (nodes[k].order, k))
        groups: List[List[str]] = [[]]
        extent = 0.0
        for k in ks:
            size = nodes[k].h if horizontal else nodes[k].w
            add = size if not groups[-1] else size + GAP_MINOR_IN
            if groups[-1] and extent + add > budget:
                groups.append([k])
                extent = size
            else:
                groups[-1].append(k)
                extent += add
        for g, group in enumerate(groups):
            for k in group:
                nodes[k].layer = lv + shift + g
                nodes[k].row = g
        if len(groups) > 1:
            moved = True
            shift += len(groups) - 1
    return moved


def _place(nodes: Dict[str, _Node], layers: Dict[int, List[str]], direction: str) -> Tuple[float, float]:
    """Coordinates: layers along the major axis, real nodes centred inside
    each. Dummies take no space and are routed afterwards."""
    horizontal = direction == "LR"
    major = 0.0
    major_of: Dict[int, float] = {}
    for lv in sorted(layers):
        ks = [k for k in sorted(layers[lv], key=lambda k: (nodes[k].order, k)) if not nodes[k].dummy]
        if not ks:
            major_of[lv] = major + 0.15
            major += GAP_MAJOR_IN
            continue
        total = sum((nodes[k].h if horizontal else nodes[k].w) for k in ks) + GAP_MINOR_IN * (len(ks) - 1)
        depth = max((nodes[k].w if horizontal else nodes[k].h) for k in ks)
        cur = -total / 2
        for k in ks:
            n = nodes[k]
            if horizontal:
                n.y = -(cur + n.h / 2)
                n.x = major + depth / 2
            else:
                n.x = cur + n.w / 2
                n.y = -(major + depth / 2)
            cur += (n.h if horizontal else n.w) + GAP_MINOR_IN
        major_of[lv] = major + depth / 2
        major += depth + GAP_MAJOR_IN
    for k, n in nodes.items():
        if n.dummy:
            if horizontal:
                n.x, n.y = major_of.get(n.layer, 0.0), 0.0
            else:
                n.x, n.y = 0.0, -major_of.get(n.layer, 0.0)
    real = [n for n in nodes.values() if not n.dummy]
    xs = [n.x - n.w / 2 for n in real] + [n.x + n.w / 2 for n in real]
    ys = [n.y - n.h / 2 for n in real] + [n.y + n.h / 2 for n in real]
    return (max(xs) - min(xs), max(ys) - min(ys))


#: How far a routed edge keeps off a box it passes, in inches.
_CLEARANCE_IN = 0.14


def _route_dummies(nodes: Dict[str, _Node], edges: List[_Edge], layers: Dict[int, List[str]], direction: str) -> None:
    """Put every dummy on the straight line between its edge's endpoints,
    then push it clear of any box it would land on.

    The first version left dummies in the layer's node sequence, which put a
    zero-width waypoint at the far end of the row and bent a two-layer edge
    into a long detour across the page — visible in the 15-node architecture
    render. Interpolating and then dodging keeps a long edge close to
    straight while still going round the boxes it passes.
    """
    horizontal = direction == "LR"
    real_by_layer: Dict[int, List[_Node]] = {}
    for n in nodes.values():
        if not n.dummy:
            real_by_layer.setdefault(n.layer, []).append(n)
    taken: Dict[int, List[float]] = {}
    for e in edges:
        if not e.via or e.src not in nodes or e.dst not in nodes:
            continue
        a, b = nodes[e.src], nodes[e.dst]
        steps = len(e.via) + 1
        for i, vid in enumerate(e.via, start=1):
            d = nodes.get(vid)
            if d is None:
                continue
            t = i / steps
            minor = (a.x + (b.x - a.x) * t) if not horizontal else (a.y + (b.y - a.y) * t)
            for other in sorted(real_by_layer.get(d.layer, []), key=lambda n: (n.x if not horizontal else n.y)):
                lo = (other.x - other.w / 2 if not horizontal else other.y - other.h / 2) - _CLEARANCE_IN
                hi = (other.x + other.w / 2 if not horizontal else other.y + other.h / 2) + _CLEARANCE_IN
                if lo <= minor <= hi:
                    minor = lo if (minor - lo) <= (hi - minor) else hi
            # Two long edges that dodge the same box land on the same line
            # and their labels sit on top of each other; step the second one
            # further out so both are readable.
            lane = taken.setdefault(d.layer, [])
            outward = -0.22 if minor <= 0 else 0.22
            while any(abs(minor - used) < 0.12 for used in lane):
                minor += outward
            lane.append(minor)
            if horizontal:
                d.y = minor
            else:
                d.x = minor


def _build(diagram: Any, direction: str, wrap: int, box: Tuple[float, float], roles: Tuple[str, ...]) -> DiagramLayout:
    nodes: Dict[str, _Node] = {}
    for dn in diagram.nodes:
        role = dn.kind if dn.kind in DIAGRAM_ROLES else ""
        nodes[dn.id] = _Node(nid=dn.id, label=dn.label, role=role)
    edges: List[_Edge] = []
    for de in diagram.edges:
        if de.source not in nodes or de.target not in nodes:
            continue
        edges.append(_Edge(src=de.source, dst=de.target, label=de.label,
                           dashed=(de.style == "dashed"), self_loop=de.source == de.target))
    if len(nodes) < 2:
        raise DiagramError("a diagram needs at least two nodes")

    back = _feedback_arcs(nodes, edges)
    for i in back:
        edges[i].reversed_ = True
    _size_nodes(nodes, wrap)
    _layer(nodes, edges)
    # A first ordering with no dummies, so the split below groups nodes that
    # belong together rather than by id; then the split; then the real
    # ordering, over the layering the split produced.
    _order(nodes, edges, sweeps=2)
    budget = max((box[1] if direction == "LR" else box[0]) - 2 * FIG_PAD_IN, MIN_NODE_W_IN)
    _split_wide_layers(nodes, direction, budget)
    _add_dummies(nodes, edges)
    layers = _order(nodes, edges)
    _place(nodes, layers, direction)
    _route_dummies(nodes, edges, layers, direction)
    return DiagramLayout(direction=direction, nodes=nodes, edges=edges, layers=layers,
                         frame=_frame(nodes, edges, roles), box_in=box, wrap=wrap,
                         reversed_edges=len(back), crossings=_count_crossings(nodes, edges),
                         legend=tuple(roles))


def _legend_for(diagram: Any) -> Tuple[str, ...]:
    """The roles that appear, in the palette's fixed order. A legend is drawn
    only when there are at least two: one role is already said by the label."""
    present = {n.kind for n in diagram.nodes if n.kind in DIAGRAM_ROLES}
    roles = tuple(r for r in DIAGRAM_ROLES if r in present)
    return roles if len(roles) >= 2 else ()


def _legend_width(roles: Sequence[str]) -> float:
    """How wide the legend row is, in inches — part of the figure's extent,
    so a legend wider than the graph is not cut off (it was, on the two-node
    cases: the second swatch's word ran off the right edge)."""
    if not roles:
        return 0.0
    w = 0.0
    for role in roles:
        w += 0.16 + 0.06 + len(role) * LEGEND_PT * CHAR_EM / 72.0 + 0.30
    return w - 0.30


def _frame(nodes: Dict[str, _Node], edges: List[_Edge], roles: Sequence[str]) -> Tuple[float, float, float, float]:
    """Everything that gets drawn, padded: node boxes, routed waypoints, the
    bulge of a self-loop and the legend row."""
    real = [n for n in nodes.values() if not n.dummy]
    xs = [n.x - n.w / 2 for n in real] + [n.x + n.w / 2 for n in real]
    ys = [n.y - n.h / 2 for n in real] + [n.y + n.h / 2 for n in real]
    for n in nodes.values():
        if n.dummy:
            xs.append(n.x)
            ys.append(n.y)
    for e in edges:
        if e.self_loop and e.src in nodes:
            n = nodes[e.src]
            xs.append(n.x + n.w / 2 + _self_loop_reach(n, e.label))
            ys.extend([n.y - n.h / 2, n.y + n.h / 2])
        elif e.label and e.via:
            # A routed edge's label sits on a waypoint that has been pushed
            # clear of the boxes, so it can be the leftmost thing drawn.
            mid = nodes.get(e.via[0])
            if mid is not None:
                half = len(e.label) * EDGE_LABEL_PT * CHAR_EM / 144.0 + 0.16
                xs.extend([mid.x - half, mid.x + half])
    x0, x1 = min(xs) - FIG_PAD_IN, max(xs) + FIG_PAD_IN
    y0, y1 = min(ys) - FIG_PAD_IN, max(ys) + FIG_PAD_IN
    legend_w = _legend_width(roles)
    if legend_w:
        y0 -= LEGEND_H_IN
        if legend_w > (x1 - x0) - 2 * FIG_PAD_IN:
            x1 = x0 + legend_w + 2 * FIG_PAD_IN
    return (x0, y0, x1, y1)


def _self_loop_reach(n: _Node, label: str) -> float:
    """How far right of its node a self-loop and its label go."""
    r = min(0.26, n.h * 0.6)
    return r * 1.6 + (len(label) * EDGE_LABEL_PT * CHAR_EM / 72.0 + 0.2 if label else 0.0)


#: The last few layouts, keyed by the diagram's JSON and the page box. The
#: HTML writer needs a diagram's size to place the picture and the PNG
#: renderer needs the same layout to draw it; the layout is deterministic, so
#: the second caller reads the first one's work instead of laying it out
#: again. Small and bounded: this is a per-render memo, not a store.
_LAYOUT_MEMO: "Dict[Tuple[str, float, float], DiagramLayout]" = {}
_LAYOUT_MEMO_MAX = 16


def layout_for(diagram: Any, *, box_in: Tuple[float, float] = PORTRAIT_BOX_IN) -> DiagramLayout:
    """`layout_diagram`, memoised on the diagram's own JSON."""
    try:
        key = (diagram.model_dump_json(), float(box_in[0]), float(box_in[1]))
    except Exception:  # pragma: no cover - a duck-typed diagram in a test
        return layout_diagram(diagram, box_in=box_in)
    hit = _LAYOUT_MEMO.get(key)
    if hit is not None:
        return hit
    out = layout_diagram(diagram, box_in=box_in)
    if len(_LAYOUT_MEMO) >= _LAYOUT_MEMO_MAX:
        _LAYOUT_MEMO.clear()
    _LAYOUT_MEMO[key] = out
    return out


def layout_diagram(diagram: Any, *, box_in: Tuple[float, float] = PORTRAIT_BOX_IN) -> DiagramLayout:
    """The best layout of `diagram` inside `box_in`.

    Both orientations and several label-wrap widths are tried; the first that
    keeps a label at or above 8 pt on the page wins, and otherwise the one
    that gets closest does, with `fits` False so the caller can give the
    figure more room. The declared direction is tried first, so a model that
    says LR gets LR whenever LR fits.

    A diagram of another family (`spec.MermaidFigure`: a sequence, ER,
    class, state, mindmap, timeline, journey, kanban or packet diagram) is
    not a graph of boxes and is laid out by render/diagram_figures.py; what
    comes back exposes the same `fits` / `effective_pt` / `fig_in` /
    `display_in` the callers read.
    """
    if getattr(diagram, "family", None):
        from . import diagram_figures as F

        return F.layout_figure(diagram, box_in=box_in)  # type: ignore[return-value]
    declared = "LR" if getattr(diagram, "direction", "TD") == "LR" else "TD"
    other = "TD" if declared == "LR" else "LR"
    roles = _legend_for(diagram)
    best: Optional[DiagramLayout] = None
    for direction in (declared, other):
        for wrap in _WRAP_WIDTHS:
            candidate = _build(diagram, direction, wrap, box_in, roles)
            if best is None or _better(candidate, best, declared):
                best = candidate
            if candidate.fits and direction == declared and wrap == _WRAP_WIDTHS[0]:
                return candidate
    assert best is not None
    return best


def _better(a: DiagramLayout, b: DiagramLayout, declared: str) -> bool:
    """Prefer a layout that fits; then the direction the model declared, as
    long as it fits, because the model said LR when the flow reads across;
    then the larger effective point size, the wider label wrap and fewer
    crossings."""
    key_a = (a.fits, a.fits and a.direction == declared, round(a.effective_pt, 2), a.wrap, -a.crossings)
    key_b = (b.fits, b.fits and b.direction == declared, round(b.effective_pt, 2), b.wrap, -b.crossings)
    return key_a > key_b


# ---------------------------------------------------------------- colour --


def _tint(hex_colour: str, amount: float) -> str:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    r = round(r + (255 - r) * amount)
    g = round(g + (255 - g) * amount)
    b = round(b + (255 - b) * amount)
    return f"#{r:02X}{g:02X}{b:02X}"


def role_colour(role: Optional[str]) -> str:
    """The saturated slot for a role; neutral grey for anything else.

    `Optional`, and measured rather than assumed: since `DiagramNode.kind`
    became nullable on 2026-09-27 an untagged node's role is None, and None
    is what a test now passes here. The `.get` already answered it with the
    neutral; the annotation said `str` and was the only thing that was wrong.
    The render path still never sends None — `_build` folds a missing role to
    `""` — so this is the annotation catching up with the caller, not a
    behaviour change.
    """
    return ROLE_COLOURS.get(role, NEUTRAL)


def role_fill(role: Optional[str]) -> str:
    """The node fill: the role's colour blended towards paper."""
    return _tint(role_colour(role), FILL_TINT)


# ----------------------------------------------------------------- draw --


def _anchor(n: _Node, toward: Tuple[float, float]) -> Tuple[float, float]:
    dx, dy = toward[0] - n.x, toward[1] - n.y
    if dx == 0 and dy == 0:
        return n.x, n.y
    hw, hh = max(n.w / 2, 1e-6), max(n.h / 2, 1e-6)
    sx = hw / abs(dx) if dx else float("inf")
    sy = hh / abs(dy) if dy else float("inf")
    s = min(sx, sy)
    return n.x + dx * s, n.y + dy * s


def _draw(layout: DiagramLayout) -> Any:
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
    from matplotlib.path import Path as MPath
    import matplotlib.pyplot as plt

    nodes, edges = layout.nodes, layout.edges
    legend_roles = list(layout.legend)
    legend_h = LEGEND_H_IN if legend_roles else 0.0
    x0, y0, x1, y1 = layout.frame
    fig, ax = plt.subplots(figsize=(x1 - x0, y1 - y0))
    fig.patch.set_facecolor(PAPER)
    ax.set_facecolor(PAPER)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)

    for e in edges:
        if e.self_loop:
            n = nodes.get(e.src)
            if n is None:
                continue
            r = min(0.26, n.h * 0.6)
            cx, cy = n.x + n.w / 2, n.y
            arc = FancyArrowPatch((cx, cy + r * 0.5), (cx, cy - r * 0.5), arrowstyle="-|>", mutation_scale=10,
                                  linewidth=1.2, color=EDGE_INK, zorder=1,
                                  connectionstyle=f"arc3,rad={-1.6}", shrinkA=0, shrinkB=0)
            ax.add_patch(arc)
            if e.label:
                ax.text(cx + r * 1.2, cy, e.label, fontsize=EDGE_LABEL_PT, color=INK, ha="left", va="center",
                        zorder=3, bbox=dict(boxstyle="round,pad=0.16", facecolor=PAPER, edgecolor="none"))
            continue
        a, b = nodes.get(e.src), nodes.get(e.dst)
        if a is None or b is None:
            continue
        chain = [a] + [nodes[v] for v in e.via if v in nodes] + [b]
        pts: List[Tuple[float, float]] = []
        for i, n in enumerate(chain):
            if i == 0:
                pts.append(_anchor(n, (chain[1].x, chain[1].y)))
            elif i == len(chain) - 1:
                pts.append(_anchor(n, (chain[-2].x, chain[-2].y)))
            else:
                pts.append((n.x, n.y))
        style = (0, (4, 3)) if e.dashed else "solid"
        if len(pts) == 2:
            # A reversed edge is drawn in its ORIGINAL direction and bowed,
            # so a back edge is visibly a back edge and does not lie on top
            # of the forward edge it answers.
            rad = 0.15 if e.reversed_ else 0.0
            arrow = FancyArrowPatch(pts[0], pts[1], arrowstyle="-|>", mutation_scale=11, linewidth=1.25,
                                    color=EDGE_INK, zorder=1, linestyle=style, shrinkA=0, shrinkB=2,
                                    connectionstyle=f"arc3,rad={rad}")
        else:
            path = MPath(pts, [MPath.MOVETO] + [MPath.LINETO] * (len(pts) - 1))
            arrow = FancyArrowPatch(path=path, arrowstyle="-|>", mutation_scale=11, linewidth=1.25,
                                    color=EDGE_INK, zorder=1, linestyle=style, shrinkA=0, shrinkB=2)
        ax.add_patch(arrow)
        if e.label:
            # A routed edge is labelled at its FIRST waypoint, not its
            # middle: two long edges that dodge the same column have their
            # middles on the same line, and their labels printed over each
            # other ("fast path" and "rework" came out as "fast path ork").
            mid = pts[1] if len(pts) > 2 else ((pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2)
            ax.text(mid[0], mid[1], e.label, fontsize=EDGE_LABEL_PT, color=INK, ha="center", va="center", zorder=3,
                    bbox=dict(boxstyle="round,pad=0.16", facecolor=PAPER, edgecolor="none"))

    for n in layout.real_nodes():
        base = role_colour(n.role)
        ax.add_patch(FancyBboxPatch((n.x - n.w / 2, n.y - n.h / 2), n.w, n.h,
                                    boxstyle="round,pad=0,rounding_size=0.07",
                                    facecolor=role_fill(n.role), edgecolor=base, linewidth=1.8, zorder=2))
        ax.text(n.x, n.y, "\n".join(n.lines), fontsize=FONT_PT, color=INK, ha="center", va="center",
                zorder=4, linespacing=LINE_SPACING)

    if legend_h:
        # Identity is never colour alone: the roles that appear are named.
        cy = y0 + legend_h * 0.5
        cursor = x0 + FIG_PAD_IN
        for role in legend_roles:
            ax.add_patch(FancyBboxPatch((cursor, cy - 0.055), 0.16, 0.11,
                                        boxstyle="round,pad=0,rounding_size=0.03",
                                        facecolor=role_fill(role), edgecolor=role_colour(role),
                                        linewidth=1.4, zorder=2))
            ax.text(cursor + 0.16 + 0.06, cy, role, fontsize=LEGEND_PT, color=INK, ha="left", va="center", zorder=4)
            cursor += 0.16 + 0.06 + len(role) * LEGEND_PT * CHAR_EM / 72.0 + 0.30
    return fig


def render_diagram_png(diagram: Any, out_path: str | Path, *,
                       box_in: Tuple[float, float] = PORTRAIT_BOX_IN) -> DiagramLayout:
    """Draw `diagram` to `out_path` as a PNG sized for `box_in`.

    Under the same guards render/charts.py carries: the Agg backend, forced,
    so nothing tries to open a display; `text.parse_math` off, so a label
    full of dollars is text and not TeX; and pinned PNG metadata so two
    renders of the same diagram are byte-identical.
    """
    import matplotlib

    matplotlib.use("Agg", force=True)
    matplotlib.rcParams["text.parse_math"] = False
    import matplotlib.pyplot as plt

    layout = layout_for(diagram, box_in=box_in)
    rc = {
        "text.parse_math": False,
        "font.family": list(fonts_for(diagram)),
        "axes.unicode_minus": False,
        "path.simplify": True,
    }
    with matplotlib.rc_context(rc):
        if getattr(diagram, "family", None):
            from . import diagram_figures as F

            fig = F.draw_figure(layout)  # type: ignore[arg-type]
        else:
            fig = _draw(layout)
        try:
            buf = io.BytesIO()
            fig.savefig(buf, dpi=DPI, format="png", facecolor=PAPER, metadata={"Software": None})
            Path(out_path).write_bytes(buf.getvalue())
        finally:
            plt.close(fig)
    return layout


@lru_cache(maxsize=1)
def _installed_families() -> frozenset:
    from matplotlib import font_manager

    return frozenset(f.name for f in font_manager.fontManager.ttflist)


def fonts_for(diagram: Any) -> Tuple[str, ...]:
    """The font stack, with a script fallback when a label is not Latin —
    the same families theme.py lists for charts.

    Only families this server actually has are returned. matplotlib prints a
    `findfont: Font family 'X' not found.` warning for every missing name on
    every text object, which on a 15-node diagram is hundreds of lines in the
    render worker's log for nothing.
    """
    if getattr(diagram, "family", None):
        from . import diagram_figures as F

        text = F.figure_text(diagram)
    else:
        text = " ".join([getattr(diagram, "title", "") or ""] + [n.label for n in diagram.nodes]
                        + [e.label for e in diagram.edges])
    families: List[str] = []
    for script in theme.unsupported_scripts(text):
        families.extend(theme.SCRIPT_FONTS.get(script, ()))
    families.extend(["Carlito", "Calibri", "Liberation Sans", "DejaVu Sans"])
    installed = _installed_families()
    seen: Set[str] = set()
    out = tuple(f for f in families if f in installed and not (f in seen or seen.add(f)))
    return out or ("sans-serif",)


def png_size_in(path: str | Path) -> Tuple[float, float]:
    """A PNG's natural size in inches, read from its own header.

    Word places a picture at its natural size when no width is given, so the
    renderer needs the same number the file carries. Read here from IHDR and
    pHYs rather than with an image library, because neither Pillow nor any
    other reader is a dependency of this path.
    """
    data = Path(path).read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise DiagramError("not a PNG")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    dpi_x = dpi_y = 96.0
    pos = 8
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        if kind == b"pHYs" and pos + 8 + 9 <= len(data):
            body = data[pos + 8:pos + 17]
            if body[8] == 1:  # unit is the metre
                ppm_x = int.from_bytes(body[0:4], "big")
                ppm_y = int.from_bytes(body[4:8], "big")
                if ppm_x and ppm_y:
                    dpi_x, dpi_y = ppm_x * 0.0254, ppm_y * 0.0254
            break
        if kind == b"IDAT":
            break
        pos += 12 + length
    return (width / dpi_x, height / dpi_y)


__all__ = [
    "DIAGRAM_ROLES", "ROLE_COLOURS", "NEUTRAL", "PAPER", "INK", "FONT_PT", "MIN_EFFECTIVE_PT",
    "PORTRAIT_BOX_IN", "LANDSCAPE_BOX_IN", "DiagramError", "DiagramLayout",
    "layout_diagram", "layout_for", "render_diagram_png", "parse_mermaid", "role_colour", "role_fill", "png_size_in",
]
