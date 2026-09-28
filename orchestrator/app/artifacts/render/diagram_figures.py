"""Paper drawings of the mermaid families that are not flowcharts.

ONE DRAWER PER FAMILY, as render/mermaid_grammars.py has one reader per
grammar. A sequence diagram is lifelines and ordered arrows; an ER diagram is
tables joined by lines with a cardinality glyph at each end; a class diagram
is three-compartment boxes with UML heads; a state diagram is rounded boxes
with a start dot and an end bullseye; a mindmap is a tree; a timeline and a
journey are periods and tasks along a line; a kanban is columns of cards; a
packet is bit cells in 32-bit rows. None of these is a graph of boxes and
arrows in the diagrams.py sense, and none is drawn by that module's layout.

THE PROMISE IS THE SAME: what is drawn is what the reader read, and the
reader refused everything it could not carry. Nothing here fills a gap the
source left, invents a node, or drops one. Every label reaches the page as
text; nothing is executed or fetched. A label longer than its box is wide
is WRAPPED onto as many lines as it needs and the box grows to hold them;
this module's `_wrap` never cuts a line off with an ellipsis. (Until
2026-09-28 it did — a kanban card past 4 lines, a mindmap or journey label
past 3, a timeline event past 3 lines of 16 characters — and the engine's
own browser timeline shipped "Tim Berners-Lee creates the first web brows…"
while this docstring said nothing was dropped. diagrams.py's flowchart
`_wrap` still caps a node label at three lines; that is the graph family's
contract, not this one's.)

COLOUR. Ink on paper. These families have no role vocabulary and mermaid's
own colouring of them is decorative (a mindmap's branch hues, a timeline's
section tints), so nothing is painted that could be read as a category.
Boxes and header rows take two neutral tints — BOX_FILL, a light wash, and
HEAD_FILL, a shade darker, both tints of diagrams.NEUTRAL against PAPER —
and ONE more fill exists: a sequence diagram's NOTE is filled with a light
orange tint (NOTE_FILL, `_tint("#E07B00", 0.86)`), so a note reads as a
note and not as a participant's box, the way mermaid's own yellow note does.
Every note gets the same tint, so it carries no category. Where a diagram
carries a magnitude — a journey's 1-5 score — it is drawn as a COUNT of
filled dots beside the number, never as a colour ramp, so it reads for
someone who cannot see colour and for a grayscale print.

ROUTING. A relation between two boxes is a straight line when that line
clears every other box; when it would cross one — a link that spans more
than one layer runs straight through whatever sits between, which is what
USERS -> COMMENTS did through POSTS in a blog ER drawn from the engine's own
output on 2026-09-28, its label overprinting a POSTS row — it is bowed by the
smallest arc that clears the boxes in between, its label at the arc's apex,
and the figure grows to hold the arc. A back edge and each half of a twin
pair (A -> B and B -> A) bow too, the twins on opposite sides. A
self-transition loops on the ACROSS side of the flow (right of the box in
TD, below it in LR) and its reach is part of the layer's across size, so it
neither lands on the next layer's box nor runs off the figure.

PAGE FIT reuses diagrams.py's contract. Every plan reports the figure's
natural size in inches and the smallest label size it used; the callers
scale a figure that is wider or taller than the page box DOWN, and when a
label would land under MIN_EFFECTIVE_PT (8 pt) the layout says `fits=False`
and render/__init__.py puts a sentence in the render report. A TIMELINE and
a JOURNEY, the two families that grow sideways with every item, FOLD into
rows that fit the page width instead (the engine's typical eight-task
journey was 12.1 in wide and shipped at 5.0 pt; a ten-period timeline 14.8
in and 4.0 pt, measured 2026-09-28; folded, both print at full size). The
caps in spec.py bound the rest: every family's largest allowed instance
still draws, and the one that cannot fold — twelve lifelines — reports its
scale honestly rather than silently shipping 5 pt text. A mindmap is a
tree and does not fold; a deep one still shrinks and says so.

COORDINATES. Every plan lays out in INCHES with y growing DOWNWARD, as a page
reads; `_axes` inverts the matplotlib y-axis once so the code above it never
has to think about it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import diagrams as DG

PAPER = DG.PAPER
INK = DG.INK
EDGE_INK = DG.EDGE_INK
FONT_PT = DG.FONT_PT
SMALL_PT = 7.5
TINY_PT = 6.5
CHAR_EM = DG.CHAR_EM
LINE_SPACING = DG.LINE_SPACING
MIN_EFFECTIVE_PT = DG.MIN_EFFECTIVE_PT
PAD = 0.22
BOX_FILL = DG._tint(DG.NEUTRAL, 0.90)
HEAD_FILL = DG._tint(DG.NEUTRAL, 0.78)
#: The one non-neutral fill: a sequence note (see the module docstring).
NOTE_FILL = DG._tint("#E07B00", 0.86)


@dataclass
class FigureLayout:
    """What a renderer, a test or the render report needs about one figure.

    The same properties diagrams.DiagramLayout exposes for the graph family
    (`fits`, `effective_pt`, `fig_in`, `display_in`, `scale`), so the callers
    in render/__init__.py, html.py and docx.py do not care which family a
    picture is.
    """

    family: str
    fig_in: Tuple[float, float]
    box_in: Tuple[float, float]
    draw: Callable[[Any], None]
    #: The smallest label size used on the figure, before page scaling.
    font_pt: float = FONT_PT
    direction: str = "TD"
    legend: Tuple[str, ...] = ()
    #: Positions and sizes the tests read back (ids -> boxes in inches).
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def scale(self) -> float:
        w, h = self.fig_in
        bw, bh = self.box_in
        return min(1.0, bw / w if w > 0 else 1.0, bh / h if h > 0 else 1.0)

    @property
    def effective_pt(self) -> float:
        return self.font_pt * self.scale

    @property
    def fits(self) -> bool:
        return self.effective_pt >= MIN_EFFECTIVE_PT - 1e-9

    @property
    def display_in(self) -> Tuple[float, float]:
        s = self.scale
        return (self.fig_in[0] * s, self.fig_in[1] * s)


# ------------------------------------------------------------ measuring --


def _tw(text: str, pt: float = FONT_PT) -> float:
    """Width of the widest line of `text`, in inches, at `pt`."""
    lines = (text or "").split("\n")
    return max((len(l) for l in lines), default=0) * pt * CHAR_EM / 72.0


def _lh(pt: float = FONT_PT) -> float:
    return pt * LINE_SPACING / 72.0


def _wrap(text: str, width: int) -> str:
    """`text` broken into lines of about `width` characters — ALL of them,
    never cut with an ellipsis (see the module docstring); a word longer
    than the width is hard-split as diagrams._wrap does. A `<br/>` already
    became a newline upstream and stays a line break."""
    out: List[str] = []
    for part in (text or "").split("\n"):
        words = part.split()
        if not words:
            out.append("")
            continue
        cur = ""
        for word in words:
            while len(word) > width:
                if cur:
                    out.append(cur)
                    cur = ""
                out.append(word[: width - 1] + "-")
                word = word[width - 1:]
            candidate = f"{cur} {word}".strip()
            if len(candidate) <= width or not cur:
                cur = candidate
            else:
                out.append(cur)
                cur = word
        if cur:
            out.append(cur)
    return "\n".join(out)


def _nlines(text: str) -> int:
    return max(1, (text or "").count("\n") + 1)


# --------------------------------------------------------------- drawing --


def _axes(fig_in: Tuple[float, float]):
    import matplotlib.pyplot as plt

    w, h = fig_in
    fig, ax = plt.subplots(figsize=(w, h))
    fig.patch.set_facecolor(PAPER)
    ax.set_facecolor(PAPER)
    ax.set_xlim(0, w)
    ax.set_ylim(h, 0)          # y grows downward, as the plans lay out
    ax.set_aspect("equal")
    ax.axis("off")
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    return fig, ax


def _box(ax, x: float, y: float, w: float, h: float, *, fill: str = BOX_FILL, edge: str = EDGE_INK,
         lw: float = 1.2, rounding: float = 0.06, dashed: bool = False, z: int = 2) -> None:
    from matplotlib.patches import FancyBboxPatch

    style = f"round,pad=0,rounding_size={rounding}" if rounding > 0 else "square,pad=0"
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=style, facecolor=fill, edgecolor=edge, linewidth=lw,
                                linestyle=(0, (4, 3)) if dashed else "solid", zorder=z))


def _text(ax, x: float, y: float, s: str, *, pt: float = FONT_PT, ha: str = "center", va: str = "center",
          color: str = INK, weight: str = "normal", z: int = 4, bg: Optional[str] = None, rotation: float = 0.0) -> None:
    kw: Dict[str, Any] = {}
    if bg:
        kw["bbox"] = dict(boxstyle="round,pad=0.16", facecolor=bg, edgecolor="none")
    ax.text(x, y, s, fontsize=pt, color=color, ha=ha, va=va, zorder=z, linespacing=LINE_SPACING,
            fontweight=weight, rotation=rotation, **kw)


def _line(ax, p: Tuple[float, float], q: Tuple[float, float], *, dashed: bool = False, lw: float = 1.2,
          color: str = EDGE_INK, z: int = 1) -> None:
    ax.plot([p[0], q[0]], [p[1], q[1]], color=color, linewidth=lw, linestyle=(0, (4, 3)) if dashed else "solid",
            zorder=z, solid_capstyle="butt")


_SEQ_HEADS = {"filled": "-|>", "none": "-", "open": "->", "cross": "-", "both": "<|-|>"}


def _arrow(ax, p: Tuple[float, float], q: Tuple[float, float], *, head: str = "filled", dashed: bool = False,
           rad: float = 0.0, lw: float = 1.2, z: int = 1, shrink_b: float = 0.0) -> None:
    from matplotlib.patches import FancyArrowPatch

    ax.add_patch(FancyArrowPatch(p, q, arrowstyle=_SEQ_HEADS.get(head, "-|>"), mutation_scale=11, linewidth=lw,
                                 color=EDGE_INK, zorder=z, linestyle=(0, (4, 3)) if dashed else "solid",
                                 shrinkA=0, shrinkB=shrink_b, connectionstyle=f"arc3,rad={rad}"))
    if head == "cross":
        r = 0.05
        _line(ax, (q[0] - r, q[1] - r), (q[0] + r, q[1] + r), lw=lw, z=z + 1)
        _line(ax, (q[0] - r, q[1] + r), (q[0] + r, q[1] - r), lw=lw, z=z + 1)


def _unit(p: Tuple[float, float], q: Tuple[float, float]) -> Tuple[float, float, float]:
    dx, dy = q[0] - p[0], q[1] - p[1]
    d = (dx * dx + dy * dy) ** 0.5 or 1e-9
    return dx / d, dy / d, d


def _anchor(cx: float, cy: float, w: float, h: float, toward: Tuple[float, float]) -> Tuple[float, float]:
    """Where a line from the box centre toward `toward` leaves the box."""
    dx, dy = toward[0] - cx, toward[1] - cy
    if dx == 0 and dy == 0:
        return cx, cy
    hw, hh = max(w / 2, 1e-6), max(h / 2, 1e-6)
    sx = hw / abs(dx) if dx else float("inf")
    sy = hh / abs(dy) if dy else float("inf")
    s = min(sx, sy)
    return cx + dx * s, cy + dy * s


# ------------------------------------------------- shared box placement --


def _place_boxes(sizes: Dict[str, Tuple[float, float]], links: Sequence[Tuple[str, str]], direction: str,
                 *, layer_gap: float = 0.62, node_gap: float = 0.42,
                 across_extra: Optional[Dict[str, float]] = None) -> Tuple[Dict[str, Tuple[float, float]], float, float, Set[int]]:
    """Centres for boxes of the given sizes, layered along `direction`.

    A small Sugiyama: back edges found by depth-first search are reversed
    for layering (a cyclic graph otherwise collapses onto one layer),
    longest-path layering, then four barycentre sweeps to order each layer.
    Returns (centres, width, height, indices of the reversed links), all in
    inches with PAD around. `across_extra[k]` is room a box needs beyond its
    own across-size on the across side (a self-loop and its label); it is
    part of the layer's across extent, so the next box in the layer and the
    figure's edge both clear it.
    """
    ids = list(sizes)
    extra = across_extra or {}
    succ: Dict[str, List[Tuple[str, int]]] = {k: [] for k in ids}
    for i, (a, b) in enumerate(links):
        if a in succ and b in succ and a != b:
            succ[a].append((b, i))
    colour = {k: 0 for k in ids}
    back: Set[int] = set()
    for root in ids:
        if colour[root]:
            continue
        colour[root] = 1
        stack: List[Tuple[str, Any]] = [(root, iter(succ[root]))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for dst, idx in it:
                if colour[dst] == 1:
                    back.add(idx)
                elif colour[dst] == 0:
                    colour[dst] = 1
                    stack.append((dst, iter(succ[dst])))
                    advanced = True
                    break
            if not advanced:
                colour[node] = 2
                stack.pop()
    fwd: List[Tuple[str, str]] = []
    for i, (a, b) in enumerate(links):
        if a == b or a not in sizes or b not in sizes:
            continue
        fwd.append((b, a) if i in back else (a, b))
    layer = {k: 0 for k in ids}
    indeg = {k: 0 for k in ids}
    out: Dict[str, List[str]] = {k: [] for k in ids}
    for a, b in fwd:
        out[a].append(b)
        indeg[b] += 1
    ready = [k for k in ids if indeg[k] == 0]
    while ready:
        n = ready.pop(0)
        for m in out[n]:
            layer[m] = max(layer[m], layer[n] + 1)
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
    for _pass in range(len(ids)):
        moved = False
        for k in ids:
            if out[k] and not any(b == k for _a, b in fwd):
                want = min(layer[m] for m in out[k]) - 1
                if want > layer[k]:
                    layer[k] = want
                    moved = True
        if not moved:
            break
    layers: Dict[int, List[str]] = {}
    for k in ids:
        layers.setdefault(layer[k], []).append(k)
    depth = max(layers) + 1 if layers else 0
    pos_in_layer = {k: layers[layer[k]].index(k) for k in ids}
    preds: Dict[str, List[str]] = {k: [] for k in ids}
    for a, b in fwd:
        preds[b].append(a)
    for _sweep in range(4):
        for li in range(1, depth):
            row = layers[li]
            bary = {k: (sum(pos_in_layer[p] for p in preds[k]) / len(preds[k])) if preds[k] else pos_in_layer[k] for k in row}
            row.sort(key=lambda k: (bary[k], k))
            for i, k in enumerate(row):
                pos_in_layer[k] = i
        for li in range(depth - 2, -1, -1):
            row = layers[li]
            bary = {k: (sum(pos_in_layer[s] for s in out[k]) / len(out[k])) if out[k] else pos_in_layer[k] for k in row}
            row.sort(key=lambda k: (bary[k], k))
            for i, k in enumerate(row):
                pos_in_layer[k] = i
    # Sizes along and across the flow. TD: layers stack down, boxes run across.
    along = (lambda k: sizes[k][1]) if direction == "TD" else (lambda k: sizes[k][0])
    across = (lambda k: sizes[k][0]) if direction == "TD" else (lambda k: sizes[k][1])
    layer_extent = {li: max(along(k) for k in row) for li, row in layers.items()}
    row_extent = {li: sum(across(k) + extra.get(k, 0.0) for k in row) + node_gap * (len(row) - 1) for li, row in layers.items()}
    total_across = max(row_extent.values()) if row_extent else 0.0
    centres: Dict[str, Tuple[float, float]] = {}
    cursor = PAD
    for li in range(depth):
        row = layers[li]
        start = PAD + (total_across - row_extent[li]) / 2
        mid = cursor + layer_extent[li] / 2
        for k in row:
            c_across = start + across(k) / 2
            centres[k] = (c_across, mid) if direction == "TD" else (mid, c_across)
            start += across(k) + extra.get(k, 0.0) + node_gap
        cursor += layer_extent[li] + layer_gap
    total_along = cursor - layer_gap + PAD
    w, h = (total_across + 2 * PAD, total_along) if direction == "TD" else (total_along, total_across + 2 * PAD)
    return centres, w, h, back


#: A relation keeps this far from a box it does not touch; so does its label.
_CLEAR_IN = 0.10
#: Bows tried, smallest first, either side, when a straight line would cross
#: a box. arc3's `rad` is a fraction of the chord length.
_BOW_RADS: Tuple[float, ...] = (0.3, -0.3, 0.45, -0.45, 0.65, -0.65, 0.9, -0.9, 1.2, -1.2, 1.6, -1.6)
#: The bows a twin pair may take: one sign only, because the same `rad` on
#: the reversed chord bows to the OTHER side of the page, which is what puts
#: A -> B and B -> A on opposite sides of each other.
_TWIN_RADS: Tuple[float, ...] = (0.3, 0.45, 0.65, 0.9, 1.2, 1.6)


@dataclass
class _Route:
    """One relation's path: arc3 `rad` (0 for a straight line), its two end
    points on the box borders, and where its label sits."""
    rad: float
    p: Tuple[float, float]
    q: Tuple[float, float]
    label_xy: Tuple[float, float]

    def shifted(self, dx: float, dy: float) -> "_Route":
        return _Route(self.rad, (self.p[0] + dx, self.p[1] + dy), (self.q[0] + dx, self.q[1] + dy),
                      (self.label_xy[0] + dx, self.label_xy[1] + dy))


def _control(p: Tuple[float, float], q: Tuple[float, float], rad: float) -> Tuple[float, float]:
    """arc3's control point for the curve p -> q at `rad`, in the plans' inch
    coordinates (y down). Measured on the inverted axes on 2026-09-28: a
    positive `rad` bows toward (-uy, ux), the page-left when travelling down
    the page — NOT the side `_mid_label` used to assume."""
    ux, uy, d = _unit(p, q)
    return ((p[0] + q[0]) / 2 - uy * rad * d, (p[1] + q[1]) / 2 + ux * rad * d)


def bezier_points(p: Tuple[float, float], q: Tuple[float, float], rad: float, n: int = 24) -> List[Tuple[float, float]]:
    """Points along the curve arc3 draws for p -> q at `rad` (the straight
    line when `rad` is 0), for clearance checks and for the tests."""
    if not rad:
        return [(p[0] + (q[0] - p[0]) * i / n, p[1] + (q[1] - p[1]) * i / n) for i in range(n + 1)]
    c = _control(p, q, rad)
    out: List[Tuple[float, float]] = []
    for i in range(n + 1):
        t = i / n
        a, b = (1 - t) ** 2, 2 * (1 - t) * t
        out.append((a * p[0] + b * c[0] + t * t * q[0], a * p[1] + b * c[1] + t * t * q[1]))
    return out


def _label_xy(p: Tuple[float, float], q: Tuple[float, float], rad: float) -> Tuple[float, float]:
    """Where a label of the curve p -> q sits: the midpoint of a straight
    line, the apex of a bow (never nearer the chord than 0.24 in, so the
    text clears the line)."""
    mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
    if not rad:
        return mx, my
    ux, uy, d = _unit(p, q)
    off = max(abs(rad) * d * 0.5, 0.24) * (1 if rad > 0 else -1)
    return mx - uy * off, my + ux * off


def _in_rect(pt: Tuple[float, float], r: Tuple[float, float, float, float], clear: float) -> bool:
    return r[0] - clear <= pt[0] <= r[0] + r[2] + clear and r[1] - clear <= pt[1] <= r[1] + r[3] + clear


def _rects_overlap(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float], clear: float) -> bool:
    return not (a[0] + a[2] < b[0] - clear or b[0] + b[2] + clear < a[0] or a[1] + a[3] < b[1] - clear or b[1] + b[3] + clear < a[1])


def _route(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float],
           others: Sequence[Tuple[float, float, float, float]], label_wh: Tuple[float, float],
           rads: Sequence[float]) -> _Route:
    """The path from box a to box b (cx, cy, w, h): the first of `rads`
    (0 for straight) whose curve and label clear every rectangle in
    `others`, else the one that crosses the least."""
    ca, cb = (a[0], a[1]), (b[0], b[1])
    best: Optional[Tuple[int, _Route]] = None
    for rad in rads:
        c = _control(ca, cb, rad) if rad else None
        p = _anchor(a[0], a[1], a[2], a[3], c or cb)
        q = _anchor(b[0], b[1], b[2], b[3], c or ca)
        pts = bezier_points(p, q, rad, 48)
        hits = sum(1 for pt in pts[2:-2] for r in others if _in_rect(pt, r, _CLEAR_IN))
        lx, ly = _label_xy(p, q, rad)
        lw, lh = label_wh
        if lw:
            hits += sum(1 for r in others if _rects_overlap((lx - lw / 2, ly - lh / 2, lw, lh), r, _CLEAR_IN))
        route = _Route(rad, p, q, (lx, ly))
        if hits == 0:
            return route
        if best is None or hits < best[0]:
            best = (hits, route)
    assert best is not None
    return best[1]


def _route_links(centres: Dict[str, Tuple[float, float]], sizes: Dict[str, Tuple[float, float]],
                 links: Sequence[Tuple[str, str]], labels: Sequence[str], back: Set[int],
                 label_pt: float = SMALL_PT, obstacles: Sequence[Tuple[float, float, float, float]] = ()) -> Dict[int, _Route]:
    """A `_Route` per link index (self-links have none). A straight line is
    tried first except for a back edge, which bows as before, and for each
    half of a twin pair, which bow on opposite sides. `obstacles` are
    rectangles no route may cross besides the boxes: the self-loops and
    their labels. Two straight routes whose labels would print on top of
    each other (two transitions fanning out of one state) have their labels
    slid apart along their own lines, one toward its source and one toward
    its target."""
    pairs = set(links)
    rects = {k: (centres[k][0] - sizes[k][0] / 2, centres[k][1] - sizes[k][1] / 2, sizes[k][0], sizes[k][1]) for k in sizes}
    routes: Dict[int, _Route] = {}
    for i, (a, b) in enumerate(links):
        if a == b or a not in sizes or b not in sizes:
            continue
        others = [r for k, r in rects.items() if k not in (a, b)] + list(obstacles)
        label = labels[i] if i < len(labels) else ""
        label_wh = (_tw(label, label_pt) + 0.12, _lh(label_pt)) if label else (0.0, 0.0)
        if (b, a) in pairs:
            rads: Sequence[float] = _TWIN_RADS
        elif i in back:
            rads = _BOW_RADS
        else:
            rads = (0.0,) + _BOW_RADS
        routes[i] = _route((*centres[a], *sizes[a]), (*centres[b], *sizes[b]), others, label_wh, rads)
    _spread_labels(routes, labels, label_pt)
    return routes


def _spread_labels(routes: Dict[int, _Route], labels: Sequence[str], label_pt: float) -> None:
    def rect(i: int) -> Optional[Tuple[float, float, float, float]]:
        label = labels[i] if i < len(labels) else ""
        if not label:
            return None
        lw, lh = _tw(label, label_pt) + 0.12, _lh(label_pt)
        x, y = routes[i].label_xy
        return (x - lw / 2, y - lh / 2, lw, lh)

    def at(i: int, t: float) -> Tuple[float, float]:
        pts = bezier_points(routes[i].p, routes[i].q, routes[i].rad, 50)
        return pts[int(round(t * 50))]

    idx = sorted(routes)
    for n, i in enumerate(idx):
        ri = rect(i)
        if ri is None or routes[i].rad:
            continue
        for j in idx[n + 1:]:
            rj = rect(j)
            if rj is None or routes[j].rad or not _rects_overlap(ri, rj, 0.0):
                continue
            # 0.30 / 0.70 along their lines: on the 0.46 in layer gap that
            # is 0.18 in apart, one 7.5 pt line height plus clearance.
            routes[i].label_xy = at(i, 0.30)
            routes[j].label_xy = at(j, 0.70)
            ri = rect(i)
            assert ri is not None


def _fit_routes(centres: Dict[str, Tuple[float, float]], routes: Dict[int, _Route], labels: Sequence[str],
                W: float, H: float, label_pt: float = SMALL_PT) -> Tuple[Dict[str, Tuple[float, float]], Dict[int, _Route], float, float]:
    """Grow the figure (and shift everything in it) so every bow and every
    label stays at least 0.08 in inside the page."""
    xs: List[float] = []
    ys: List[float] = []
    for i, r in routes.items():
        for x, y in bezier_points(r.p, r.q, r.rad, 24):
            xs.append(x)
            ys.append(y)
        label = labels[i] if i < len(labels) else ""
        if label:
            lw, lh = _tw(label, label_pt) + 0.12, _lh(label_pt)
            xs += [r.label_xy[0] - lw / 2, r.label_xy[0] + lw / 2]
            ys += [r.label_xy[1] - lh / 2, r.label_xy[1] + lh / 2]
    if not xs:
        return centres, routes, W, H
    margin = 0.08
    dx = max(0.0, margin - min(xs))
    dy = max(0.0, margin - min(ys))
    W = max(W, max(xs) + margin) + dx
    H = max(H, max(ys) + margin) + dy
    if dx or dy:
        centres = {k: (x + dx, y + dy) for k, (x, y) in centres.items()}
        routes = {i: r.shifted(dx, dy) for i, r in routes.items()}
    return centres, routes, W, H


def _draw_route(ax, r: _Route, *, head: str = "none", dashed: bool = False, shrink_b: float = 0.0) -> None:
    if r.rad:
        _arrow(ax, r.p, r.q, head=head, dashed=dashed, rad=r.rad, shrink_b=shrink_b)
    elif head == "none":
        _line(ax, r.p, r.q, dashed=dashed)
    else:
        _arrow(ax, r.p, r.q, head=head, dashed=dashed, shrink_b=shrink_b)


def _route_label(ax, r: _Route, text: str, pt: float = SMALL_PT) -> None:
    if text:
        _text(ax, r.label_xy[0], r.label_xy[1], text, pt=pt, bg=PAPER, z=5)


def _toward_from(r: _Route, at_p: bool) -> Tuple[float, float]:
    """The direction a glyph at one end of the route should look along: the
    control point on a bow (the curve's tangent there), the far end on a
    straight line."""
    if r.rad:
        return _control(r.p, r.q, r.rad)
    return r.q if at_p else r.p


# ------------------------------------------------------------- self loops --


def _self_loop_extent(label: str, direction: str) -> float:
    """How far a self-loop and its label reach beyond the box on the across
    side: right of it in TD, below it in LR."""
    if direction == "TD":
        return 0.34 + (_tw(label, SMALL_PT) + 0.08 if label else 0.0)
    return 0.34 + (_lh(SMALL_PT) + 0.06 if label else 0.0)


def _loop_rect(box: Tuple[float, float, float, float], label: str, direction: str) -> Tuple[float, float, float, float]:
    """The rectangle a self-loop and its label occupy beside `box`."""
    cx, cy, w, h = box
    reach = _self_loop_extent(label, direction)
    if direction == "TD":
        return (cx + w / 2, cy - 0.16, reach, 0.32)
    lw = _tw(label, SMALL_PT) + 0.12 if label else 0.32
    return (cx - max(0.16, lw / 2), cy + h / 2, max(0.32, lw), reach)


def _self_loop(ax, box: Tuple[float, float, float, float], label: str, direction: str, head: str) -> Tuple[float, float, float, float]:
    """Draw the loop on the across side of `box`; return `_loop_rect`."""
    cx, cy, w, h = box
    if direction == "TD":
        x = cx + w / 2
        _arrow(ax, (x, cy - 0.12), (x, cy + 0.12), head=head, rad=-1.8)
        if label:
            _text(ax, x + 0.34, cy, label, pt=SMALL_PT, ha="left", bg=PAPER, z=5)
    else:
        y = cy + h / 2
        _arrow(ax, (cx + 0.12, y), (cx - 0.12, y), head=head, rad=-1.8)
        if label:
            _text(ax, cx, y + 0.34 + _lh(SMALL_PT) / 2, label, pt=SMALL_PT, bg=PAPER, z=5)
    return _loop_rect(box, label, direction)


def _layer_gap(base: float, labels: Sequence[str], direction: str) -> float:
    """The gap between layers: `base`, or in LR wide enough for the widest
    relation label, which sits IN the gap and otherwise prints over the
    borders of the boxes on both sides (seen 2026-09-28 on an LR order
    lifecycle: "Payment Confirmed" erased the edges of Created and Paid)."""
    if direction != "LR":
        return base
    widest = max((_tw(l, SMALL_PT) for l in labels if l), default=0.0)
    return max(base, widest + 0.24)


# --------------------------------------------------------------- sequence --

_ROW_MSG = 0.44
_ROW_SELF = 0.58
_ROW_FRAME_OPEN = 0.34
_ROW_FRAME_DIVIDE = 0.30
_ROW_FRAME_CLOSE = 0.18


def plan_sequence(d: Any) -> FigureLayout:
    parts = list(d.participants)
    idx = {p.id: i for i, p in enumerate(parts)}
    n = len(parts)
    widths = [max(1.0, _tw(p.label) + 0.36) for p in parts]
    gaps = [0.36] * max(0, n - 1)
    right_extra = 0.0
    left_need = 0.0
    left_extra = 0.0
    any_actor = any(p.actor for p in parts)
    head_h = 0.72 if any_actor else 0.46

    def centres() -> List[float]:
        xs: List[float] = []
        x = PAD + left_extra
        for i in range(n):
            xs.append(x + widths[i] / 2)
            x += widths[i] + (gaps[i] if i < n - 1 else 0.0)
        return xs

    # Widen the gaps so every message text and note fits between its lifelines.
    numbered = 0
    for s in d.steps:
        if s.kind == "message":
            numbered += 1
            label = (f"{numbered}. " if d.autonumber else "") + s.text
            i, j = idx[s.source], idx[s.target]
            if i == j:
                need = _tw(label) + 0.5
                if i < n - 1:
                    gaps[i] = max(gaps[i], need)
                else:
                    right_extra = max(right_extra, need)
                continue
            lo, hi = min(i, j), max(i, j)
            xs = centres()
            span = xs[hi] - xs[lo]
            need = _tw(label) + 0.3
            if need > span:
                add = (need - span) / (hi - lo)
                for g in range(lo, hi):
                    gaps[g] += add
        elif s.kind == "note":
            text = _wrap(s.text, 28)
            need = _tw(text) + 0.3
            ids = [idx[i] for i in s.ids]
            if s.position == "over":
                lo, hi = min(ids), max(ids)
                xs = centres()
                span = (xs[hi] + widths[hi] / 2) - (xs[lo] - widths[lo] / 2)
                if need > span and hi > lo:
                    add = (need - span) / (hi - lo)
                    for g in range(lo, hi):
                        gaps[g] += add
                elif need > span:
                    widths[lo] = max(widths[lo], need)
            elif s.position == "right":
                i = ids[0]
                if i < n - 1:
                    gaps[i] = max(gaps[i], need + 0.1)
                else:
                    right_extra = max(right_extra, need + 0.1)
            else:
                i = ids[0]
                if i > 0:
                    gaps[i - 1] = max(gaps[i - 1], need + 0.1)
                else:
                    left_need = max(left_need, need)
    # Room on the LEFT of the first lifeline for a `Note left of` it, as
    # `right_extra` makes room on the right of the last: a margin, not a
    # wider box. Until 2026-09-28 this widened `widths[0]` to twice the
    # note, and a 45-character note made the first participant's head box
    # about four times the width of the others (seen in the pixels).
    left_extra = max(0.0, left_need + 0.12 + 0.06 - widths[0] / 2) if left_need else 0.0
    xs = centres()
    W = xs[-1] + widths[-1] / 2 + right_extra + PAD
    y_head = PAD
    y_life = y_head + head_h
    cursor = y_life + 0.26
    frames: List[Tuple[str, str, float, int]] = []
    rows: List[Tuple[str, Any, float]] = []   # (kind, step-or-frame, y)
    numbered = 0
    depth = 0
    for s in d.steps:
        if s.kind == "message":
            numbered += 1
            self_msg = s.source == s.target
            rows.append(("message", (s, numbered), cursor))
            cursor += _ROW_SELF if self_msg else _ROW_MSG
        elif s.kind == "note":
            text = _wrap(s.text, 28)
            h = _nlines(text) * _lh() + 0.16
            rows.append(("note", (s, text, h), cursor))
            cursor += h + 0.16
        elif s.kind == "frame_open":
            rows.append(("frame_open", (s, depth), cursor))
            frames.append((s.frame, s.text, cursor, depth))
            depth += 1
            cursor += _ROW_FRAME_OPEN
        elif s.kind == "frame_divide":
            rows.append(("frame_divide", (s, depth - 1), cursor))
            cursor += _ROW_FRAME_DIVIDE
        else:
            depth -= 1
            kind, text, y_open, dep = frames.pop()
            rows.append(("frame_close", (kind, text, y_open, dep), cursor))
            cursor += _ROW_FRAME_CLOSE
    H = cursor + PAD
    detail = {"lifelines": {p.id: xs[i] for i, p in enumerate(parts)}, "rows": len(rows),
              "head_widths": list(widths), "left_extra": left_extra}

    def draw(ax) -> None:
        for i, p in enumerate(parts):
            x, w = xs[i], widths[i]
            if p.actor:
                hx, hy = x, y_head + 0.09
                from matplotlib.patches import Circle
                ax.add_patch(Circle((hx, hy), 0.075, facecolor=PAPER, edgecolor=EDGE_INK, linewidth=1.2, zorder=3))
                _line(ax, (hx, hy + 0.075), (hx, hy + 0.27), lw=1.2, z=3)
                _line(ax, (hx - 0.11, hy + 0.15), (hx + 0.11, hy + 0.15), lw=1.2, z=3)
                _line(ax, (hx, hy + 0.27), (hx - 0.09, hy + 0.40), lw=1.2, z=3)
                _line(ax, (hx, hy + 0.27), (hx + 0.09, hy + 0.40), lw=1.2, z=3)
                _text(ax, x, y_head + 0.58, p.label, pt=FONT_PT)
            else:
                _box(ax, x - w / 2, y_head, w, head_h - 0.04, fill=HEAD_FILL)
                _text(ax, x, y_head + (head_h - 0.04) / 2, p.label, pt=FONT_PT)
            _line(ax, (x, y_life), (x, H - PAD), dashed=True, lw=1.0, z=0)
        for kind, payload, y in rows:
            if kind == "message":
                s, num = payload
                label = (f"{num}. " if d.autonumber else "") + s.text
                xa, xb = xs[idx[s.source]], xs[idx[s.target]]
                dashed = s.line == "dashed"
                if xa == xb:
                    x0 = xa
                    _line(ax, (x0, y + 0.14), (x0 + 0.3, y + 0.14), dashed=dashed)
                    _line(ax, (x0 + 0.3, y + 0.14), (x0 + 0.3, y + 0.40), dashed=dashed)
                    _arrow(ax, (x0 + 0.3, y + 0.40), (x0, y + 0.40), head=s.head, dashed=dashed)
                    if label:
                        _text(ax, x0 + 0.38, y + 0.27, label, pt=SMALL_PT, ha="left")
                else:
                    _arrow(ax, (xa, y + 0.30), (xb, y + 0.30), head=s.head, dashed=dashed)
                    if label:
                        _text(ax, (xa + xb) / 2, y + 0.13, label, pt=SMALL_PT, bg=PAPER)
            elif kind == "note":
                s, text, h = payload
                ids = [idx[i] for i in s.ids]
                w = _tw(text) + 0.3
                if s.position == "over":
                    lo, hi = min(ids), max(ids)
                    left = xs[lo] - widths[lo] / 2 if hi > lo else xs[lo] - w / 2
                    right = xs[hi] + widths[hi] / 2 if hi > lo else xs[lo] + w / 2
                    if right - left < w:
                        c = (left + right) / 2
                        left, right = c - w / 2, c + w / 2
                elif s.position == "right":
                    left, right = xs[ids[0]] + 0.12, xs[ids[0]] + 0.12 + w
                else:
                    left, right = xs[ids[0]] - 0.12 - w, xs[ids[0]] - 0.12
                _box(ax, left, y, right - left, h, fill=NOTE_FILL, edge=EDGE_INK, rounding=0.02, z=3)
                _text(ax, (left + right) / 2, y + h / 2, text, pt=SMALL_PT, z=5)
            elif kind == "frame_open":
                s, dep = payload
                inset = 0.06 + dep * 0.08
                tab = s.frame
                _text(ax, PAD - 0.10 + inset + 0.05, y + 0.12, tab, pt=SMALL_PT, ha="left", weight="bold", z=5, bg=PAPER)
                if s.text:
                    _text(ax, PAD - 0.10 + inset + 0.05 + _tw(tab, SMALL_PT) + 0.12, y + 0.12, f"[{s.text}]", pt=SMALL_PT, ha="left", z=5, bg=PAPER)
            elif kind == "frame_divide":
                s, dep = payload
                inset = 0.06 + dep * 0.08
                _line(ax, (PAD - 0.10 + inset, y + 0.10), (W - PAD + 0.10 - inset, y + 0.10), dashed=True, lw=1.0, z=3)
                if s.text:
                    _text(ax, PAD - 0.10 + inset + 0.05, y + 0.22, f"[{s.text}]", pt=SMALL_PT, ha="left", z=5, bg=PAPER)
            else:
                kind_, text, y_open, dep = payload
                inset = 0.06 + dep * 0.08
                from matplotlib.patches import Rectangle
                ax.add_patch(Rectangle((PAD - 0.10 + inset, y_open), W - 2 * PAD + 0.20 - 2 * inset, y + 0.06 - y_open,
                                       facecolor="none", edgecolor=EDGE_INK, linewidth=1.0, zorder=2))

    return FigureLayout("sequence", (W, H), DG.PORTRAIT_BOX_IN, draw, detail=detail)


# ------------------------------------------------------------------- er --

_ER_ROW = 0.24
_ER_HEAD = 0.34


def _er_entity_size(e: Any) -> Tuple[float, float, List[Tuple[str, str, str, str]]]:
    rows = [(a.type, a.name, a.keys, a.comment) for a in e.attributes]
    w_type = max([_tw(r[0], SMALL_PT) for r in rows], default=0)
    w_name = max([_tw(r[1], SMALL_PT) for r in rows], default=0)
    w_keys = max([_tw(r[2], SMALL_PT) for r in rows], default=0)
    w_comm = max([_tw(r[3], SMALL_PT) for r in rows], default=0)
    body_w = w_type + w_name + w_keys + w_comm + 0.16 * (1 + bool(w_keys) + bool(w_comm)) + 0.24
    w = max(1.1, _tw(e.label) + 0.36, body_w)
    h = _ER_HEAD + _ER_ROW * len(rows) + (0.06 if rows else 0.0)
    return w, h, rows


def _crow_foot(ax, p: Tuple[float, float], toward: Tuple[float, float], card: str) -> None:
    """The cardinality glyph at end `p` of a relation running toward `toward`.
    Drawn along the first 0.26 in of the line, outside the entity box."""
    from matplotlib.patches import Circle

    ux, uy, _ = _unit(p, toward)
    nx, ny = -uy, ux
    def at(t: float) -> Tuple[float, float]:
        return p[0] + ux * t, p[1] + uy * t
    def bar(t: float) -> None:
        c = at(t)
        _line(ax, (c[0] + nx * 0.07, c[1] + ny * 0.07), (c[0] - nx * 0.07, c[1] - ny * 0.07), lw=1.2, z=3)
    def crow() -> None:
        c = at(0.16)
        _line(ax, (c[0] + nx * 0.08, c[1] + ny * 0.08), p, lw=1.2, z=3)
        _line(ax, (c[0] - nx * 0.08, c[1] - ny * 0.08), p, lw=1.2, z=3)
    def ring(t: float) -> None:
        c = at(t)
        ax.add_patch(Circle(c, 0.045, facecolor=PAPER, edgecolor=EDGE_INK, linewidth=1.2, zorder=3))
    if card == "exactly_one":
        bar(0.10); bar(0.17)
    elif card == "zero_or_one":
        bar(0.10); ring(0.22)
    elif card == "zero_or_more":
        crow(); ring(0.25)
    else:  # one_or_more
        crow(); bar(0.20)


def plan_er(d: Any, direction: Optional[str] = None) -> FigureLayout:
    direction = direction or d.direction
    sizes: Dict[str, Tuple[float, float]] = {}
    rows_of: Dict[str, List[Tuple[str, str, str, str]]] = {}
    for e in d.entities:
        w, h, rows = _er_entity_size(e)
        sizes[e.id] = (w, h)
        rows_of[e.id] = rows
    links = [(r.source, r.target) for r in d.relations]
    rel_labels = [r.label for r in d.relations]
    loops = {r.source: r.label for r in d.relations if r.source == r.target}
    extra = {k: _self_loop_extent(l, direction) for k, l in loops.items()}
    centres, W, H, back = _place_boxes(sizes, links, direction, layer_gap=_layer_gap(0.75, rel_labels, direction), across_extra=extra)
    loop_rects = [_loop_rect((*centres[k], *sizes[k]), l, direction) for k, l in loops.items()]
    routes = _route_links(centres, sizes, links, rel_labels, back, obstacles=loop_rects)
    centres, routes, W, H = _fit_routes(centres, routes, rel_labels, W, H)
    labels = {e.id: e.label for e in d.entities}
    detail = {"boxes": {k: (centres[k][0] - sizes[k][0] / 2, centres[k][1] - sizes[k][1] / 2, sizes[k][0], sizes[k][1]) for k in sizes},
              "routes": {i: (links[i][0], links[i][1], r.rad, r.p, r.q, r.label_xy) for i, r in routes.items()},
              "loops": {}}

    def draw(ax) -> None:
        for i, r in enumerate(d.relations):
            if r.source == r.target:
                detail["loops"][r.source] = _self_loop(ax, (*centres[r.source], *sizes[r.source]), r.label, direction, "none")
                continue
            route = routes[i]
            _draw_route(ax, route, dashed=not r.identifying)
            _crow_foot(ax, route.p, _toward_from(route, True), r.source_card)
            _crow_foot(ax, route.q, _toward_from(route, False), r.target_card)
            _route_label(ax, route, r.label)
        for k, (w, h) in sizes.items():
            cx, cy = centres[k]
            x, y = cx - w / 2, cy - h / 2
            _box(ax, x, y, w, h, fill=PAPER, rounding=0.0)
            _box(ax, x, y, w, _ER_HEAD, fill=HEAD_FILL, rounding=0.0)
            _text(ax, cx, y + _ER_HEAD / 2, labels[k], pt=FONT_PT, weight="bold")
            rows = rows_of[k]
            if rows:
                w_type = max(_tw(r[0], SMALL_PT) for r in rows)
                w_name = max(_tw(r[1], SMALL_PT) for r in rows)
                w_keys = max(_tw(r[2], SMALL_PT) for r in rows)
                ry = y + _ER_HEAD + 0.03
                for t, name, keys, comment in rows:
                    cy_r = ry + _ER_ROW / 2
                    _text(ax, x + 0.12, cy_r, t, pt=SMALL_PT, ha="left", color=EDGE_INK)
                    _text(ax, x + 0.12 + w_type + 0.16, cy_r, name, pt=SMALL_PT, ha="left")
                    if keys:
                        _text(ax, x + 0.12 + w_type + 0.16 + w_name + 0.16, cy_r, keys, pt=SMALL_PT, ha="left", weight="bold")
                    if comment:
                        _text(ax, x + w - 0.12, cy_r, comment, pt=SMALL_PT, ha="right", color=EDGE_INK)
                    ry += _ER_ROW

    return FigureLayout("er", (W, H), DG.PORTRAIT_BOX_IN, draw, direction=direction, detail=detail)


# ---------------------------------------------------------------- class --

_CL_LINE = 0.22
_CL_NAME = 0.34
_CL_EMPTY = 0.12


def _class_size(c: Any) -> Tuple[float, float]:
    texts = [c.label] + list(c.attributes) + list(c.methods) + ([f"«{c.annotation}»"] if c.annotation else [])
    w = max(1.1, max(_tw(t, SMALL_PT if t != c.label else FONT_PT) for t in texts) + 0.30)
    h = _CL_NAME + (0.18 if c.annotation else 0.0)
    h += _CL_LINE * len(c.attributes) if c.attributes else _CL_EMPTY
    h += _CL_LINE * len(c.methods) if c.methods else _CL_EMPTY
    return w, h + 0.06


def _uml_head(ax, p: Tuple[float, float], toward_src: Tuple[float, float], head: str) -> Tuple[float, float]:
    """Draw the UML glyph at line end `p` (the line comes from `toward_src`).
    Returns where the line itself should now stop."""
    from matplotlib.patches import Polygon

    if head == "none":
        return p
    ux, uy, _ = _unit(p, toward_src)       # points back along the line
    nx, ny = -uy, ux
    if head == "inheritance":
        L, Wd = 0.20, 0.10
        base = (p[0] + ux * L, p[1] + uy * L)
        ax.add_patch(Polygon([p, (base[0] + nx * Wd, base[1] + ny * Wd), (base[0] - nx * Wd, base[1] - ny * Wd)],
                             closed=True, facecolor=PAPER, edgecolor=EDGE_INK, linewidth=1.2, zorder=3))
        return base
    if head in ("composition", "aggregation"):
        L, Wd = 0.24, 0.08
        mid = (p[0] + ux * L / 2, p[1] + uy * L / 2)
        tail = (p[0] + ux * L, p[1] + uy * L)
        ax.add_patch(Polygon([p, (mid[0] + nx * Wd, mid[1] + ny * Wd), tail, (mid[0] - nx * Wd, mid[1] - ny * Wd)],
                             closed=True, facecolor=EDGE_INK if head == "composition" else PAPER,
                             edgecolor=EDGE_INK, linewidth=1.2, zorder=3))
        return tail
    # open arrow
    L, Wd = 0.16, 0.08
    base = (p[0] + ux * L, p[1] + uy * L)
    _line(ax, p, (base[0] + nx * Wd, base[1] + ny * Wd), lw=1.2, z=3)
    _line(ax, p, (base[0] - nx * Wd, base[1] - ny * Wd), lw=1.2, z=3)
    return p


def plan_class(d: Any, direction: Optional[str] = None) -> FigureLayout:
    direction = direction or d.direction
    sizes = {c.id: _class_size(c) for c in d.classes}
    links = [(r.source, r.target) for r in d.relations]
    rel_labels = [r.label for r in d.relations]
    loops = {r.source: r.label for r in d.relations if r.source == r.target}
    extra = {k: _self_loop_extent(l, direction) for k, l in loops.items()}
    centres, W, H, back = _place_boxes(sizes, links, direction, layer_gap=_layer_gap(0.8, rel_labels, direction), across_extra=extra)
    loop_rects = [_loop_rect((*centres[k], *sizes[k]), l, direction) for k, l in loops.items()]
    routes = _route_links(centres, sizes, links, rel_labels, back, obstacles=loop_rects)
    centres, routes, W, H = _fit_routes(centres, routes, rel_labels, W, H)
    by_id = {c.id: c for c in d.classes}
    detail = {"boxes": {k: (centres[k][0] - sizes[k][0] / 2, centres[k][1] - sizes[k][1] / 2, sizes[k][0], sizes[k][1]) for k in sizes},
              "routes": {i: (links[i][0], links[i][1], r.rad, r.p, r.q, r.label_xy) for i, r in routes.items()},
              "loops": {}}

    def draw(ax) -> None:
        for i, r in enumerate(d.relations):
            if r.source == r.target:
                detail["loops"][r.source] = _self_loop(ax, (*centres[r.source], *sizes[r.source]), r.label, direction, "none")
                continue
            route = routes[i]
            p, q = route.p, route.q
            tp, tq = _toward_from(route, True), _toward_from(route, False)
            p2 = _uml_head(ax, p, tp, r.source_head)
            q2 = _uml_head(ax, q, tq, r.target_head)
            _draw_route(ax, _Route(route.rad, p2, q2, route.label_xy), dashed=r.line == "dashed")
            _route_label(ax, route, r.label)
            if r.source_card:
                ux, uy, _ = _unit(p, tp)
                _text(ax, p[0] + ux * 0.22 - uy * 0.14, p[1] + uy * 0.22 + ux * 0.14, r.source_card, pt=TINY_PT, bg=PAPER, z=5)
            if r.target_card:
                ux, uy, _ = _unit(q, tq)
                _text(ax, q[0] + ux * 0.22 - uy * 0.14, q[1] + uy * 0.22 + ux * 0.14, r.target_card, pt=TINY_PT, bg=PAPER, z=5)
        for k, (w, h) in sizes.items():
            c = by_id[k]
            cx, cy = centres[k]
            x, y = cx - w / 2, cy - h / 2
            _box(ax, x, y, w, h, fill=PAPER, rounding=0.0)
            yy = y + 0.03
            if c.annotation:
                _text(ax, cx, yy + 0.09, f"«{c.annotation}»", pt=SMALL_PT, color=EDGE_INK)
                yy += 0.18
            _text(ax, cx, yy + _CL_NAME / 2, c.label, pt=FONT_PT, weight="bold")
            yy += _CL_NAME
            _line(ax, (x, yy), (x + w, yy), lw=1.0, z=3)
            if c.attributes:
                for m in c.attributes:
                    _text(ax, x + 0.12, yy + _CL_LINE / 2, m, pt=SMALL_PT, ha="left")
                    yy += _CL_LINE
            else:
                yy += _CL_EMPTY
            _line(ax, (x, yy), (x + w, yy), lw=1.0, z=3)
            if c.methods:
                for m in c.methods:
                    _text(ax, x + 0.12, yy + _CL_LINE / 2, m, pt=SMALL_PT, ha="left")
                    yy += _CL_LINE

    return FigureLayout("class", (W, H), DG.PORTRAIT_BOX_IN, draw, direction=direction, detail=detail)


# ---------------------------------------------------------------- state --


def plan_state(d: Any, direction: Optional[str] = None) -> FigureLayout:
    direction = direction or d.direction
    from ..spec import STATE_END, STATE_START

    sizes: Dict[str, Tuple[float, float]] = {}
    for s in d.states:
        lines = [_wrap(l, 22) for l in s.lines]
        w = max(1.0, _tw(s.label) + 0.36, *([_tw(l, SMALL_PT) + 0.3 for l in lines] or [0.0]))
        h = 0.36 + (0.06 + sum(_nlines(l) * _lh(SMALL_PT) for l in lines) if lines else 0.0)
        sizes[s.id] = (w, h)
    used = {t.source for t in d.transitions} | {t.target for t in d.transitions}
    if STATE_START in used:
        sizes[STATE_START] = (0.26, 0.26)
    if STATE_END in used:
        sizes[STATE_END] = (0.30, 0.30)
    links = [(t.source, t.target) for t in d.transitions]
    tr_labels = [t.label for t in d.transitions]
    loops = {t.source: t.label for t in d.transitions if t.source == t.target}
    extra = {k: _self_loop_extent(l, direction) for k, l in loops.items()}
    # 0.46 in between layers, as diagrams.GAP_MAJOR_IN: a 7.5 pt transition
    # label sits in it, and an eight-state chain (ten layers with the two
    # pseudo-states) stays inside the portrait box at 8 pt.
    centres, W, H, back = _place_boxes(sizes, links, direction, layer_gap=_layer_gap(0.46, tr_labels, direction), across_extra=extra)
    loop_rects = [_loop_rect((*centres[k], *sizes[k]), l, direction) for k, l in loops.items()]
    routes = _route_links(centres, sizes, links, tr_labels, back, obstacles=loop_rects)
    centres, routes, W, H = _fit_routes(centres, routes, tr_labels, W, H)
    by_id = {s.id: s for s in d.states}
    detail = {"boxes": {k: (centres[k][0] - sizes[k][0] / 2, centres[k][1] - sizes[k][1] / 2, sizes[k][0], sizes[k][1]) for k in sizes},
              "routes": {i: (links[i][0], links[i][1], r.rad, r.p, r.q, r.label_xy) for i, r in routes.items()},
              "loops": {}}

    def draw(ax) -> None:
        from matplotlib.patches import Circle

        for i, t in enumerate(d.transitions):
            if t.source == t.target:
                detail["loops"][t.source] = _self_loop(ax, (*centres[t.source], *sizes[t.source]), t.label, direction, "filled")
                continue
            route = routes[i]
            _draw_route(ax, route, head="filled", shrink_b=1.5)
            _route_label(ax, route, t.label)
        for k, (w, h) in sizes.items():
            cx, cy = centres[k]
            if k == STATE_START:
                ax.add_patch(Circle((cx, cy), 0.09, facecolor=INK, edgecolor=INK, zorder=3))
                continue
            if k == STATE_END:
                ax.add_patch(Circle((cx, cy), 0.12, facecolor=PAPER, edgecolor=INK, linewidth=1.3, zorder=3))
                ax.add_patch(Circle((cx, cy), 0.065, facecolor=INK, edgecolor=INK, zorder=4))
                continue
            s = by_id[k]
            x, y = cx - w / 2, cy - h / 2
            _box(ax, x, y, w, h, fill=BOX_FILL, rounding=0.12)
            if s.lines:
                _text(ax, cx, y + 0.18, s.label, pt=FONT_PT, weight="bold")
                _line(ax, (x, y + 0.36), (x + w, y + 0.36), lw=1.0, z=3)
                yy = y + 0.36 + 0.03
                for l in s.lines:
                    txt = _wrap(l, 22)
                    hh = _nlines(txt) * _lh(SMALL_PT)
                    _text(ax, cx, yy + hh / 2, txt, pt=SMALL_PT)
                    yy += hh
            else:
                _text(ax, cx, cy, s.label, pt=FONT_PT)

    return FigureLayout("state", (W, H), DG.PORTRAIT_BOX_IN, draw, direction=direction, detail=detail)


# -------------------------------------------------------------- mindmap --


def plan_mindmap(d: Any) -> FigureLayout:
    nodes = list(d.nodes)
    by_id = {n.id: n for n in nodes}
    children: Dict[str, List[str]] = {n.id: [] for n in nodes}
    root = nodes[0].id
    depth: Dict[str, int] = {root: 0}
    for n in nodes[1:]:
        children[n.parent].append(n.id)
        depth[n.id] = depth[n.parent] + 1
    texts = {n.id: _wrap(n.label, 18) for n in nodes}
    sizes: Dict[str, Tuple[float, float]] = {}
    for n in nodes:
        tw, th = _tw(texts[n.id]), _nlines(texts[n.id]) * _lh()
        if n.shape == "circle":
            side = max(tw, th) + 0.34
            sizes[n.id] = (side, side)
        elif n.shape == "hexagon":
            sizes[n.id] = (tw + 0.55, th + 0.26)
        else:
            sizes[n.id] = (tw + 0.34, th + 0.24)
    max_depth = max(depth.values())
    col_w = {dp: max(sizes[k][0] for k in nodes_ids) for dp, nodes_ids in
             ((dp, [k for k in depth if depth[k] == dp]) for dp in range(max_depth + 1))}
    col_x: Dict[int, float] = {}
    x = PAD
    for dp in range(max_depth + 1):
        col_x[dp] = x
        x += col_w[dp] + 0.55
    W = x - 0.55 + PAD
    sub_h: Dict[str, float] = {}

    def measure(k: str) -> float:
        h = sizes[k][1] + 0.16
        if children[k]:
            h = max(h, sum(measure(c) for c in children[k]))
        sub_h[k] = h
        return h

    measure(root)
    centres: Dict[str, Tuple[float, float]] = {}

    def place(k: str, top: float) -> None:
        cy = top + sub_h[k] / 2
        centres[k] = (col_x[depth[k]] + sizes[k][0] / 2, cy)
        y = top + (sub_h[k] - sum(sub_h[c] for c in children[k])) / 2 if children[k] else top
        for c in children[k]:
            place(c, y)
            y += sub_h[c]

    place(root, PAD)
    H = PAD + sub_h[root] + PAD
    detail = {"boxes": {k: (centres[k][0] - sizes[k][0] / 2, centres[k][1] - sizes[k][1] / 2, *sizes[k]) for k in centres}}

    def draw(ax) -> None:
        from matplotlib.patches import Ellipse, PathPatch, Polygon
        from matplotlib.path import Path as MPath

        for k, kids in children.items():
            px = centres[k][0] + sizes[k][0] / 2
            py = centres[k][1]
            for c in kids:
                qx = centres[c][0] - sizes[c][0] / 2
                qy = centres[c][1]
                mx = (px + qx) / 2
                path = MPath([(px, py), (mx, py), (mx, qy), (qx, qy)], [MPath.MOVETO, MPath.CURVE4, MPath.CURVE4, MPath.CURVE4])
                ax.add_patch(PathPatch(path, facecolor="none", edgecolor=EDGE_INK, linewidth=1.2, zorder=1))
        for k, (cx, cy) in centres.items():
            n = by_id[k]
            w, h = sizes[k]
            x, y = cx - w / 2, cy - h / 2
            if n.shape == "circle":
                ax.add_patch(Ellipse((cx, cy), w, h, facecolor=BOX_FILL, edgecolor=EDGE_INK, linewidth=1.2, zorder=2))
            elif n.shape == "hexagon":
                dx = 0.16
                ax.add_patch(Polygon([(x + dx, y), (x + w - dx, y), (x + w, cy), (x + w - dx, y + h), (x + dx, y + h), (x, cy)],
                                     closed=True, facecolor=BOX_FILL, edgecolor=EDGE_INK, linewidth=1.2, zorder=2))
            elif n.shape == "square":
                _box(ax, x, y, w, h, fill=BOX_FILL, rounding=0.0)
            elif n.shape == "rounded":
                _box(ax, x, y, w, h, fill=BOX_FILL, rounding=0.10)
            else:
                _box(ax, x, y, w, h, fill=BOX_FILL if k == root else PAPER, edge=EDGE_INK if k == root else "none", rounding=0.10)
            _text(ax, cx, cy, texts[k], pt=FONT_PT, weight="bold" if k == root else "normal")

    return FigureLayout("mindmap", (W, H), DG.PORTRAIT_BOX_IN, draw, direction="LR", detail=detail)


# -------------------------------------------------------------- timeline --


def _section_bands(items: Sequence[str], lefts: Sequence[float], rights: Sequence[float]) -> List[Tuple[str, float, float]]:
    """Contiguous runs of the same non-empty section name -> (name, x0, x1)."""
    bands: List[Tuple[str, float, float]] = []
    i = 0
    while i < len(items):
        j = i
        while j + 1 < len(items) and items[j + 1] == items[i]:
            j += 1
        if items[i]:
            bands.append((items[i], lefts[i], rights[j]))
        i = j + 1
    return bands


def _fold(widths: Sequence[float], gap: float, limit: float) -> List[List[int]]:
    """Rows of consecutive column indices whose widths, with `gap` between,
    fit `limit`; a column wider than the limit is a row of its own."""
    rows: List[List[int]] = []
    cur: List[int] = []
    used = 0.0
    for i, cw in enumerate(widths):
        add = cw + (gap if cur else 0.0)
        if cur and used + add > limit:
            rows.append(cur)
            cur, used, add = [], 0.0, cw
        cur.append(i)
        used += add
    if cur:
        rows.append(cur)
    return rows


def _fold_columns(widths: Sequence[float], gap: float, box_w: float) -> Tuple[List[List[int]], List[float], float]:
    """(rows, lefts, W): the columns folded into rows no wider than the page
    box, each row starting at PAD."""
    rows = _fold(widths, gap, box_w - 2 * PAD)
    lefts = [0.0] * len(widths)
    W = 0.0
    for row in rows:
        x = PAD
        for i in row:
            lefts[i] = x
            x += widths[i] + gap
        W = max(W, x - gap + PAD)
    return rows, lefts, W


_ROW_GAP = 0.34


def plan_timeline(d: Any, box_in: Tuple[float, float] = DG.PORTRAIT_BOX_IN) -> FigureLayout:
    periods = list(d.periods)
    ev_text = [[_wrap(e, 16) for e in p.events] for p in periods]
    widths = [max(1.1, _tw(p.time) + 0.3, *([_tw(t) + 0.3 for t in evs] or [0.0])) for p, evs in zip(periods, ev_text)]
    gap = 0.22
    rows, lefts, W = _fold_columns(widths, gap, box_in[0])
    rights = [l + w for l, w in zip(lefts, widths)]
    has_sections = any(p.section for p in periods)
    band_h = 0.30 if has_sections else 0.0
    time_h = 0.34
    ev_gap = 0.10
    row_top: List[float] = []
    row_axis: List[float] = []
    y = PAD
    for row in rows:
        tallest = max((sum(_nlines(t) * _lh() + 0.14 for t in ev_text[i]) + ev_gap * len(ev_text[i]) for i in row), default=0.0)
        row_top.append(y)
        row_axis.append(y + band_h + 0.22)
        y = row_axis[-1] + time_h / 2 + 0.26 + tallest + _ROW_GAP
    H = y - _ROW_GAP + PAD
    bands: List[Tuple[str, float, float, int]] = []
    for r, row in enumerate(rows):
        for name, x0, x1 in _section_bands([periods[i].section for i in row], [lefts[i] for i in row], [rights[i] for i in row]):
            bands.append((name, x0, x1, r))
    detail = {"columns": list(zip(lefts, widths)), "bands": [b[:3] for b in bands], "rows": len(rows)}

    def draw(ax) -> None:
        for name, x0, x1, r in bands:
            _box(ax, x0, row_top[r], x1 - x0, band_h - 0.06, fill=HEAD_FILL, edge="none", rounding=0.04)
            _text(ax, (x0 + x1) / 2, row_top[r] + (band_h - 0.06) / 2, name, pt=SMALL_PT, weight="bold")
        for r, row in enumerate(rows):
            y_axis = row_axis[r]
            _line(ax, (lefts[row[0]], y_axis), (rights[row[-1]], y_axis), lw=1.6, z=1)
            for i in row:
                p = periods[i]
                cx = lefts[i] + widths[i] / 2
                _box(ax, lefts[i], y_axis - time_h / 2, widths[i], time_h, fill=BOX_FILL, rounding=0.06, z=2)
                _text(ax, cx, y_axis, p.time, pt=FONT_PT, weight="bold")
                yy = y_axis + time_h / 2 + 0.26
                if ev_text[i]:
                    _line(ax, (cx, y_axis + time_h / 2), (cx, yy), lw=1.0, z=1)
                for t in ev_text[i]:
                    h = _nlines(t) * _lh() + 0.14
                    _box(ax, lefts[i], yy, widths[i], h, fill=PAPER, rounding=0.05, z=2)
                    _text(ax, cx, yy + h / 2, t, pt=FONT_PT)
                    yy += h + ev_gap

    return FigureLayout("timeline", (W, H), box_in, draw, direction="LR", detail=detail)


# --------------------------------------------------------------- journey --


def plan_journey(d: Any, box_in: Tuple[float, float] = DG.PORTRAIT_BOX_IN) -> FigureLayout:
    tasks = list(d.tasks)
    names = [_wrap(t.name, 14) for t in tasks]
    actors = [_wrap(", ".join(t.actors), 18) for t in tasks]
    widths = [max(1.25, _tw(n) + 0.3, _tw(a, SMALL_PT) + 0.2) for n, a in zip(names, actors)]
    gap = 0.22
    rows, lefts, W = _fold_columns(widths, gap, box_in[0])
    rights = [l + w for l, w in zip(lefts, widths)]
    has_sections = any(t.section for t in tasks)
    band_h = 0.30 if has_sections else 0.0
    name_h = max(_nlines(n) for n in names) * _lh() + 0.18
    actor_h = max(_nlines(a) for a in actors) * _lh(SMALL_PT) + 0.06
    row_h = band_h + 0.06 + name_h + 0.14 + 0.30 + actor_h
    row_top = [PAD + r * (row_h + _ROW_GAP) for r in range(len(rows))]
    H = row_top[-1] + row_h + PAD
    bands: List[Tuple[str, float, float, int]] = []
    for r, row in enumerate(rows):
        for name, x0, x1 in _section_bands([tasks[i].section for i in row], [lefts[i] for i in row], [rights[i] for i in row]):
            bands.append((name, x0, x1, r))
    detail = {"columns": list(zip(lefts, widths)), "bands": [b[:3] for b in bands], "scores": [t.score for t in tasks],
              "rows": len(rows)}

    def draw(ax) -> None:
        from matplotlib.patches import Circle

        for name, x0, x1, r in bands:
            _box(ax, x0, row_top[r], x1 - x0, band_h - 0.06, fill=HEAD_FILL, edge="none", rounding=0.04)
            _text(ax, (x0 + x1) / 2, row_top[r] + (band_h - 0.06) / 2, name, pt=SMALL_PT, weight="bold")
        for r, row in enumerate(rows):
            y_name = row_top[r] + band_h + 0.06
            y_score = y_name + name_h + 0.14
            y_actor = y_score + 0.30
            # The path: one line through every task of the row, in order.
            _line(ax, (lefts[row[0]] + widths[row[0]] / 2, y_name + name_h / 2),
                  (rights[row[-1]] - widths[row[-1]] / 2, y_name + name_h / 2), lw=1.4, z=1)
            for i in row:
                t = tasks[i]
                cx = lefts[i] + widths[i] / 2
                _box(ax, lefts[i], y_name, widths[i], name_h, fill=BOX_FILL, rounding=0.06, z=2)
                _text(ax, cx, y_name + name_h / 2, names[i], pt=FONT_PT)
                # Score as a COUNT of filled dots plus the number: magnitude
                # without a colour ramp.
                dots_w = 5 * 0.13
                sx = cx - (dots_w + 0.36) / 2
                for k in range(5):
                    ax.add_patch(Circle((sx + k * 0.13 + 0.05, y_score + 0.12), 0.045,
                                        facecolor=INK if k < t.score else PAPER, edgecolor=INK, linewidth=1.0, zorder=3))
                _text(ax, sx + dots_w + 0.08, y_score + 0.12, f"{t.score}/5", pt=SMALL_PT, ha="left")
                if actors[i]:
                    _text(ax, cx, y_actor + actor_h / 2, actors[i], pt=SMALL_PT, color=EDGE_INK)

    return FigureLayout("journey", (W, H), box_in, draw, direction="LR", detail=detail)


# ---------------------------------------------------------------- kanban --


def plan_kanban(d: Any) -> FigureLayout:
    cols = list(d.columns)
    col_w = 1.7
    gap = 0.22
    head_h = 0.36
    card_texts = [[_wrap(c, 22) for c in col.cards] for col in cols]
    col_h = [head_h + 0.12 + sum(_nlines(t) * _lh() + 0.16 + 0.08 for t in texts) + 0.04 for texts in card_texts]
    W = PAD + len(cols) * col_w + (len(cols) - 1) * gap + PAD
    H = PAD + max(col_h) + PAD
    detail = {"columns": len(cols), "cards": [len(t) for t in card_texts]}

    def draw(ax) -> None:
        for i, col in enumerate(cols):
            x = PAD + i * (col_w + gap)
            _box(ax, x, PAD, col_w, max(col_h) , fill=PAPER, edge=EDGE_INK, lw=1.0, rounding=0.06, dashed=True, z=1)
            _box(ax, x, PAD, col_w, head_h, fill=HEAD_FILL, rounding=0.06, z=2)
            _text(ax, x + col_w / 2, PAD + head_h / 2, col.label, pt=FONT_PT, weight="bold")
            yy = PAD + head_h + 0.12
            for t in card_texts[i]:
                h = _nlines(t) * _lh() + 0.16
                _box(ax, x + 0.08, yy, col_w - 0.16, h, fill=BOX_FILL, rounding=0.05, z=3)
                _text(ax, x + col_w / 2, yy + h / 2, t, pt=FONT_PT, z=5)
                yy += h + 0.08

    return FigureLayout("kanban", (W, H), DG.PORTRAIT_BOX_IN, draw, direction="LR", detail=detail)


# ---------------------------------------------------------------- packet --


def plan_packet(d: Any) -> FigureLayout:
    per_row = d.bits_per_row
    bw = 0.19
    row_h = 0.42
    W = PAD + per_row * bw + PAD
    cells: List[Tuple[int, int, int, str]] = []   # (row, bit0, bit1, label)
    for f in d.fields:
        s = f.start
        while s <= f.end:
            row = s // per_row
            e = min(f.end, (row + 1) * per_row - 1)
            cells.append((row, s % per_row, e % per_row, f.label))
            s = e + 1
    rows = max(c[0] for c in cells) + 1
    strip = 0.16                 # the bit numbers live here, above each row
    top = PAD + strip
    pitch = row_h + strip
    H = top + rows * pitch - strip + PAD
    smallest = FONT_PT
    label_pt: Dict[int, float] = {}
    for i, (_row, b0, b1, label) in enumerate(cells):
        avail = (b1 - b0 + 1) * bw - 0.06
        pt = FONT_PT
        while pt > 5.0 and _tw(label, pt) > avail:
            pt -= 0.5
        label_pt[i] = pt
        smallest = min(smallest, pt)
    detail = {"rows": rows, "cells": [(r, b0, b1) for r, b0, b1, _ in cells]}

    def draw(ax) -> None:
        for i, (row, b0, b1, label) in enumerate(cells):
            x = PAD + b0 * bw
            w = (b1 - b0 + 1) * bw
            y = top + row * pitch
            _box(ax, x, y, w, row_h, fill=BOX_FILL, rounding=0.0, z=2)
            _text(ax, x + w / 2, y + row_h / 2, label, pt=label_pt[i], z=4)
            # Bit numbers as mermaid prints them: the first and last bit of
            # the cell, above its corners.
            _text(ax, x + 0.02, y - 0.02, str(row * per_row + b0), pt=TINY_PT, ha="left", va="bottom", color=EDGE_INK)
            if b1 > b0:
                _text(ax, x + w - 0.02, y - 0.02, str(row * per_row + b1), pt=TINY_PT, ha="right", va="bottom", color=EDGE_INK)

    return FigureLayout("packet", (W, H), DG.PORTRAIT_BOX_IN, draw, font_pt=smallest, direction="LR", detail=detail)


# ---------------------------------------------------------------- entry --

PLANNERS: Dict[str, Callable[[Any], FigureLayout]] = {
    "sequence": plan_sequence,
    "er": plan_er,
    "class": plan_class,
    "state": plan_state,
    "mindmap": plan_mindmap,
    "timeline": plan_timeline,
    "journey": plan_journey,
    "kanban": plan_kanban,
    "packet": plan_packet,
}


def layout_figure(diagram: Any, *, box_in: Tuple[float, float] = DG.PORTRAIT_BOX_IN) -> FigureLayout:
    """The layout of a non-graph family inside `box_in`."""
    family = getattr(diagram, "family", "")
    planner = PLANNERS.get(family)
    if planner is None:
        raise DG.DiagramError(f"no drawer for the diagram family {family!r}")
    box = (float(box_in[0]), float(box_in[1]))
    if family in ("er", "class", "state"):
        # Box graphs try the declared direction first and the other one
        # second, as diagrams.layout_diagram does: the declared direction
        # wins whenever it fits; otherwise the layout with the larger
        # label size on the page does.
        declared = "LR" if getattr(diagram, "direction", "TD") == "LR" else "TD"
        best: Optional[FigureLayout] = None
        for direction in (declared, "TD" if declared == "LR" else "LR"):
            candidate = planner(diagram, direction)
            candidate.box_in = box
            if candidate.fits and direction == declared:
                return candidate
            if best is None or (candidate.fits, round(candidate.effective_pt, 2)) > (best.fits, round(best.effective_pt, 2)):
                best = candidate
        assert best is not None
        return best
    layout = planner(diagram, box) if family in ("timeline", "journey") else planner(diagram)
    layout.box_in = box
    return layout


def draw_figure(layout: FigureLayout) -> Any:
    fig, ax = _axes(layout.fig_in)
    layout.draw(ax)
    return fig


def figure_text(diagram: Any) -> str:
    """Every string a family carries, for the font-stack decision."""
    parts: List[str] = [getattr(diagram, "title", "") or "", getattr(diagram, "caption", "") or ""]
    family = getattr(diagram, "family", "")
    if family == "sequence":
        parts += [p.label for p in diagram.participants] + [getattr(s, "text", "") or "" for s in diagram.steps]
    elif family == "er":
        for e in diagram.entities:
            parts += [e.label] + [f"{a.type} {a.name} {a.keys} {a.comment}" for a in e.attributes]
        parts += [r.label for r in diagram.relations]
    elif family == "class":
        for c in diagram.classes:
            parts += [c.label, c.annotation] + list(c.attributes) + list(c.methods)
        parts += [r.label for r in diagram.relations]
    elif family == "state":
        for s in diagram.states:
            parts += [s.label] + list(s.lines)
        parts += [t.label for t in diagram.transitions]
    elif family == "mindmap":
        parts += [n.label for n in diagram.nodes]
    elif family == "timeline":
        for p in diagram.periods:
            parts += [p.time, p.section] + list(p.events)
    elif family == "journey":
        for t in diagram.tasks:
            parts += [t.name, t.section] + list(t.actors)
    elif family == "kanban":
        for c in diagram.columns:
            parts += [c.label] + list(c.cards)
    elif family == "packet":
        parts += [f.label for f in diagram.fields]
    return " ".join(p for p in parts if p)


__all__ = ["FigureLayout", "PLANNERS", "NOTE_FILL", "bezier_points", "layout_figure", "draw_figure", "figure_text"]
