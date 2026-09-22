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
white, and no dark set has been validated for this use.

PAGE FIT IS PART OF THE DRAWING. `docx.py` pins a picture to the content
width, so a figure wider than the page is scaled DOWN and its labels shrink
with it. Measured on the 15-node architecture graph before this module
existed: it laid out at 9.64 x 12.54 in, which at 6.3 in of portrait width is
a scale of 0.654 and puts a 9.5 pt label on the page at 6.2 pt; the five-node
RAG loop landed at 5.4 pt. Both are under the ~8 pt floor at which a printed
label stops being readable. So the layout takes the page box as INPUT: it
wraps a layer that is too wide into sub-rows, tries both orientations and
several label-wrap widths, and reports the effective point size it achieves.
When no configuration reaches 8 pt the caller is told (`fits=False`) and puts
the figure on a landscape or full-page run instead of silently shipping a
6 pt label.

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

#: chart_spec.DEFAULT_PALETTE slots 1-4, in that fixed order. Validated above.
ROLE_COLOURS: Dict[str, str] = {
    "service": "#2F6FB2",
    "store": "#E07B00",
    "model": "#0E9D9A",
    "external": "#C0566B",
}

#: A role the vocabulary does not know folds here. The palette never cycles.
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
_DIR_RE = re.compile(r"^(?:flowchart|graph)\s+(TD|TB|LR|RL|BT)$", re.IGNORECASE)


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
_DECL_RE = re.compile(rf"^{_node_part('a')}$")


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
            got = m.group(1).upper()
            direction = "LR" if got in ("LR", "RL") else "TD"
            continue
        m = _EDGE_RE.match(line)
        if m:
            g = m.groupdict()
            touch(g["aid"], g["alabel"], g["arole"])
            touch(g["bid"], g["blabel"], g["brole"])
            edges.append({
                "source": g["aid"],
                "target": g["bid"],
                "label": (g["elabel"] or "").strip(),
                "style": "dashed" if g["arrow"].startswith("-.") else "solid",
            })
            continue
        m = _DECL_RE.match(line)
        if m:
            g = m.groupdict()
            touch(g["aid"], g["alabel"], g["arole"])
            continue
        return None

    if len(order) < 2 or not edges:
        return None
    if len(order) > max_nodes or len(edges) > max_edges:
        return None
    return {
        "direction": direction,
        "nodes": [{"id": nid, "label": labels[nid], "kind": roles.get(nid, "service")} for nid in order],
        "edges": edges,
    }


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
    """
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


def role_colour(role: str) -> str:
    """The saturated slot for a role; neutral grey for anything else."""
    return ROLE_COLOURS.get(role, NEUTRAL)


def role_fill(role: str) -> str:
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
