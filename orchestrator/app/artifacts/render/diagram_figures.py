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
text; nothing is executed or fetched.

COLOUR. Ink on paper. These families have no role vocabulary and mermaid's
own colouring of them is decorative (a mindmap's branch hues, a timeline's
section tints), so nothing is painted that could be read as a category. Two
fills only: BOX_FILL, a light neutral wash for a box, and HEAD_FILL, a shade
darker for a header row, both tints of diagrams.NEUTRAL against PAPER. Where
a diagram carries a magnitude — a journey's 1-5 score — it is drawn as a
COUNT of filled dots beside the number, never as a colour ramp, so it reads
for someone who cannot see colour and for a grayscale print.

PAGE FIT reuses diagrams.py's contract. Every plan reports the figure's
natural size in inches and the smallest label size it used; the callers
scale a figure that is wider or taller than the page box DOWN, and when a
label would land under MIN_EFFECTIVE_PT (8 pt) the layout says `fits=False`
and render/__init__.py puts a sentence in the render report. The caps in
spec.py bound the sizes: measured on this branch (see
tests/test_mermaid_grammars.py), every family's largest allowed instance
still draws; the wide ones (12 lifelines, 24 timeline periods) report their
scale honestly rather than silently shipping 5 pt text.

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


def _wrap(text: str, width: int, max_lines: int = 3) -> str:
    """diagrams._wrap, joined; a `<br/>` already became a newline upstream."""
    out: List[str] = []
    for part in (text or "").split("\n"):
        out.extend(DG._wrap(part, width))
    if len(out) > max_lines:
        head = out[: max_lines - 1]
        tail = " ".join(out[max_lines - 1:])
        head.append(tail if len(tail) <= width else tail[: width - 1] + "…")
        out = head
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
                 *, layer_gap: float = 0.62, node_gap: float = 0.42) -> Tuple[Dict[str, Tuple[float, float]], float, float, Set[int]]:
    """Centres for boxes of the given sizes, layered along `direction`.

    A small Sugiyama: back edges found by depth-first search are reversed
    for layering (a cyclic graph otherwise collapses onto one layer),
    longest-path layering, then four barycentre sweeps to order each layer.
    Returns (centres, width, height, indices of the reversed links), all in
    inches with PAD around.
    """
    ids = list(sizes)
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
    row_extent = {li: sum(across(k) for k in row) + node_gap * (len(row) - 1) for li, row in layers.items()}
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
            start += across(k) + node_gap
        cursor += layer_extent[li] + layer_gap
    total_along = cursor - layer_gap + PAD
    w, h = (total_across + 2 * PAD, total_along) if direction == "TD" else (total_along, total_across + 2 * PAD)
    return centres, w, h, back


def _edge_between(ax, a: Tuple[float, float, float, float], b: Tuple[float, float, float, float], *,
                  dashed: bool = False, bow: bool = False) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """A line from box a (cx, cy, w, h) to box b, clipped at both borders.
    Returns the two end points so heads and labels can be placed."""
    p = _anchor(a[0], a[1], a[2], a[3], (b[0], b[1]))
    q = _anchor(b[0], b[1], b[2], b[3], (a[0], a[1]))
    if bow:
        _arrow(ax, p, q, head="none", dashed=dashed, rad=0.25)
    else:
        _line(ax, p, q, dashed=dashed)
    return p, q


def _mid_label(ax, p: Tuple[float, float], q: Tuple[float, float], text: str, pt: float = SMALL_PT,
               bow: float = 0.0, twin: bool = False) -> None:
    """A label at the middle of the line p->q.

    On a bowed line (arc3 with `rad=bow`) it sits at the bow's apex. When
    the edge has a TWIN running the other way (`twin`), a straight edge's
    label is pushed 0.2 in to its own side, which is the side opposite the
    twin's bow — so "pause" and "resume" between the same two states never
    print on top of each other (measured in the pixels on 2026-09-28: at the
    midpoint they did, and the apex alone was 0.19 in away, not enough).
    """
    if not text:
        return
    mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
    ux, uy, d = _unit(p, q)
    # arc3's control point is mid + rad*d perpendicular and the curve's
    # apex is halfway to it. The perpendicular is taken in DISPLAY space,
    # and `_axes` inverts y, so the sign below is the one that lands on the
    # bow (checked in the pixels: the other sign put "resume" on "pause").
    off = max(bow * d * 0.5, 0.24) if bow else (0.20 if twin else 0.0)
    mx += uy * off
    my += -ux * off
    _text(ax, mx, my, text, pt=pt, bg=PAPER, z=5)


def _twins(links: Sequence[Tuple[str, str]]) -> Set[int]:
    """Indices of links whose reverse is also present."""
    pairs = set(links)
    return {i for i, (a, b) in enumerate(links) if a != b and (b, a) in pairs}


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
    any_actor = any(p.actor for p in parts)
    head_h = 0.72 if any_actor else 0.46

    def centres() -> List[float]:
        xs: List[float] = []
        x = PAD
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
                    widths[0] = max(widths[0], 2 * need)  # room on the left of the first lifeline
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
    detail = {"lifelines": {p.id: xs[i] for i, p in enumerate(parts)}, "rows": len(rows)}

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
                _box(ax, left, y, right - left, h, fill=DG._tint("#E07B00", 0.86), edge=EDGE_INK, rounding=0.02, z=3)
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
    centres, W, H, back = _place_boxes(sizes, links, direction, layer_gap=0.75)
    twins = _twins(links)
    labels = {e.id: e.label for e in d.entities}
    detail = {"boxes": {k: (centres[k][0] - sizes[k][0] / 2, centres[k][1] - sizes[k][1] / 2, sizes[k][0], sizes[k][1]) for k in sizes}}

    def draw(ax) -> None:
        for i, r in enumerate(d.relations):
            a = (*centres[r.source], *sizes[r.source])
            b = (*centres[r.target], *sizes[r.target])
            if r.source == r.target:
                cx, cy, w, h = a
                _arrow(ax, (cx + w / 2, cy - 0.12), (cx + w / 2, cy + 0.12), head="none", rad=-1.8)
                _text(ax, cx + w / 2 + 0.34, cy, r.label, pt=SMALL_PT, ha="left", bg=PAPER, z=5)
                continue
            p, q = _edge_between(ax, a, b, dashed=not r.identifying, bow=i in back)
            _crow_foot(ax, p, q, r.source_card)
            _crow_foot(ax, q, p, r.target_card)
            _mid_label(ax, p, q, r.label, bow=0.25 if i in back else 0.0, twin=i in twins)
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
    centres, W, H, back = _place_boxes(sizes, links, direction, layer_gap=0.8)
    twins = _twins(links)
    by_id = {c.id: c for c in d.classes}
    detail = {"boxes": {k: (centres[k][0] - sizes[k][0] / 2, centres[k][1] - sizes[k][1] / 2, sizes[k][0], sizes[k][1]) for k in sizes}}

    def draw(ax) -> None:
        for i, r in enumerate(d.relations):
            a = (*centres[r.source], *sizes[r.source])
            b = (*centres[r.target], *sizes[r.target])
            if r.source == r.target:
                cx, cy, w, h = a
                _arrow(ax, (cx + w / 2, cy - 0.12), (cx + w / 2, cy + 0.12), head="none", rad=-1.8)
                _text(ax, cx + w / 2 + 0.34, cy, r.label, pt=SMALL_PT, ha="left", bg=PAPER, z=5)
                continue
            p = _anchor(a[0], a[1], a[2], a[3], (b[0], b[1]))
            q = _anchor(b[0], b[1], b[2], b[3], (a[0], a[1]))
            p2 = _uml_head(ax, p, q, r.source_head)
            q2 = _uml_head(ax, q, p, r.target_head)
            if i in back:
                _arrow(ax, p2, q2, head="none", dashed=r.line == "dashed", rad=0.25)
            else:
                _line(ax, p2, q2, dashed=r.line == "dashed")
            _mid_label(ax, p, q, r.label, bow=0.25 if i in back else 0.0, twin=i in twins)
            ux, uy, _ = _unit(p, q)
            nx, ny = -uy, ux
            if r.source_card:
                _text(ax, p[0] + ux * 0.22 + nx * 0.14, p[1] + uy * 0.22 + ny * 0.14, r.source_card, pt=TINY_PT, bg=PAPER, z=5)
            if r.target_card:
                _text(ax, q[0] - ux * 0.22 + nx * 0.14, q[1] - uy * 0.22 + ny * 0.14, r.target_card, pt=TINY_PT, bg=PAPER, z=5)
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
        lines = [_wrap(l, 22, 2) for l in s.lines]
        w = max(1.0, _tw(s.label) + 0.36, *([_tw(l, SMALL_PT) + 0.3 for l in lines] or [0.0]))
        h = 0.36 + (0.06 + sum(_nlines(l) * _lh(SMALL_PT) for l in lines) if lines else 0.0)
        sizes[s.id] = (w, h)
    used = {t.source for t in d.transitions} | {t.target for t in d.transitions}
    if STATE_START in used:
        sizes[STATE_START] = (0.26, 0.26)
    if STATE_END in used:
        sizes[STATE_END] = (0.30, 0.30)
    links = [(t.source, t.target) for t in d.transitions]
    # 0.46 in between layers, as diagrams.GAP_MAJOR_IN: a 7.5 pt transition
    # label sits in it, and an eight-state chain (ten layers with the two
    # pseudo-states) stays inside the portrait box at 8 pt.
    centres, W, H, back = _place_boxes(sizes, links, direction, layer_gap=0.46)
    twins = _twins(links)
    by_id = {s.id: s for s in d.states}
    detail = {"boxes": {k: (centres[k][0] - sizes[k][0] / 2, centres[k][1] - sizes[k][1] / 2, sizes[k][0], sizes[k][1]) for k in sizes}}

    def draw(ax) -> None:
        from matplotlib.patches import Circle

        for i, t in enumerate(d.transitions):
            a = (*centres[t.source], *sizes[t.source])
            b = (*centres[t.target], *sizes[t.target])
            if t.source == t.target:
                cx, cy, w, h = a
                _arrow(ax, (cx + w / 2, cy - 0.12), (cx + w / 2, cy + 0.12), head="filled", rad=-1.8)
                _text(ax, cx + w / 2 + 0.34, cy, t.label, pt=SMALL_PT, ha="left", bg=PAPER, z=5)
                continue
            p = _anchor(a[0], a[1], a[2], a[3], (b[0], b[1]))
            q = _anchor(b[0], b[1], b[2], b[3], (a[0], a[1]))
            _arrow(ax, p, q, head="filled", rad=0.25 if i in back else 0.0, shrink_b=1.5)
            _mid_label(ax, p, q, t.label, bow=0.25 if i in back else 0.0, twin=i in twins)
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
                    txt = _wrap(l, 22, 2)
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


def plan_timeline(d: Any) -> FigureLayout:
    periods = list(d.periods)
    ev_text = [[_wrap(e, 16) for e in p.events] for p in periods]
    widths = [max(1.1, _tw(p.time) + 0.3, *([_tw(t) + 0.3 for t in evs] or [0.0])) for p, evs in zip(periods, ev_text)]
    gap = 0.22
    lefts: List[float] = []
    x = PAD
    for w in widths:
        lefts.append(x)
        x += w + gap
    rights = [l + w for l, w in zip(lefts, widths)]
    W = x - gap + PAD
    has_sections = any(p.section for p in periods)
    y = PAD
    band_h = 0.30 if has_sections else 0.0
    y_axis = y + band_h + 0.22
    time_h = 0.34
    ev_gap = 0.10
    tallest = 0.0
    for evs in ev_text:
        h = sum(_nlines(t) * _lh() + 0.14 for t in evs) + ev_gap * len(evs)
        tallest = max(tallest, h)
    H = y_axis + time_h / 2 + 0.26 + tallest + PAD
    bands = _section_bands([p.section for p in periods], lefts, rights)
    detail = {"columns": list(zip(lefts, widths)), "bands": bands}

    def draw(ax) -> None:
        for name, x0, x1 in bands:
            _box(ax, x0, y, x1 - x0, band_h - 0.06, fill=HEAD_FILL, edge="none", rounding=0.04)
            _text(ax, (x0 + x1) / 2, y + (band_h - 0.06) / 2, name, pt=SMALL_PT, weight="bold")
        _line(ax, (PAD, y_axis), (W - PAD, y_axis), lw=1.6, z=1)
        for i, p in enumerate(periods):
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

    return FigureLayout("timeline", (W, H), DG.PORTRAIT_BOX_IN, draw, direction="LR", detail=detail)


# --------------------------------------------------------------- journey --


def plan_journey(d: Any) -> FigureLayout:
    tasks = list(d.tasks)
    names = [_wrap(t.name, 14) for t in tasks]
    actors = [_wrap(", ".join(t.actors), 18, 2) for t in tasks]
    widths = [max(1.25, _tw(n) + 0.3, _tw(a, SMALL_PT) + 0.2) for n, a in zip(names, actors)]
    gap = 0.22
    lefts: List[float] = []
    x = PAD
    for w in widths:
        lefts.append(x)
        x += w + gap
    rights = [l + w for l, w in zip(lefts, widths)]
    W = x - gap + PAD
    has_sections = any(t.section for t in tasks)
    y = PAD
    band_h = 0.30 if has_sections else 0.0
    name_h = max(_nlines(n) for n in names) * _lh() + 0.18
    y_name = y + band_h + 0.06
    y_score = y_name + name_h + 0.14
    y_actor = y_score + 0.30
    actor_h = max(_nlines(a) for a in actors) * _lh(SMALL_PT) + 0.06
    H = y_actor + actor_h + PAD
    bands = _section_bands([t.section for t in tasks], lefts, rights)
    detail = {"columns": list(zip(lefts, widths)), "bands": bands, "scores": [t.score for t in tasks]}

    def draw(ax) -> None:
        from matplotlib.patches import Circle

        for name, x0, x1 in bands:
            _box(ax, x0, y, x1 - x0, band_h - 0.06, fill=HEAD_FILL, edge="none", rounding=0.04)
            _text(ax, (x0 + x1) / 2, y + (band_h - 0.06) / 2, name, pt=SMALL_PT, weight="bold")
        # The path: one line through every task, in order.
        _line(ax, (lefts[0] + widths[0] / 2, y_name + name_h / 2), (rights[-1] - widths[-1] / 2, y_name + name_h / 2), lw=1.4, z=1)
        for i, t in enumerate(tasks):
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

    return FigureLayout("journey", (W, H), DG.PORTRAIT_BOX_IN, draw, direction="LR", detail=detail)


# ---------------------------------------------------------------- kanban --


def plan_kanban(d: Any) -> FigureLayout:
    cols = list(d.columns)
    col_w = 1.7
    gap = 0.22
    head_h = 0.36
    card_texts = [[_wrap(c, 22, 4) for c in col.cards] for col in cols]
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
    layout = planner(diagram)
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


__all__ = ["FigureLayout", "PLANNERS", "layout_figure", "draw_figure", "figure_text"]
