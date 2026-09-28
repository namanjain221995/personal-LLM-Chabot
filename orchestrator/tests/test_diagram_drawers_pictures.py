"""The pictures the family drawers make of the ENGINE'S OWN diagrams.

Every source below was produced by the running engine
(Qwen/Qwen3.6-35B-A3B-NVFP4 on 2026-09-28, asked for the diagram named in
its key and nothing else), rendered to PNG through the real drawers, and
LOOKED AT. The unit tests of feat/every-diagram-type-reaches-a-file passed
while these pictures were wrong. Each test here is a geometric statement
about the layout that was false in the picture and is true now; each was
RED on 1db966b6 (the branch tip before this file) and is GREEN after.

  D1  a flowchart node whose id spells a mermaid keyword (`info`, `Info`,
      `timeline`, `pie`, `graph`) declared on its own line REFUSED, while
      the same node written on the edge's line drew, and main drew both.
  D2  a relation between boxes more than one layer apart was a straight
      line through whatever sat between: the blog ER's USERS -> COMMENTS
      `writes` ran through POSTS with its label on a POSTS row, and both
      crow's feet landed on COMMENTS together; the order lifecycle's
      Cancelled -> [*] crossed Shipped and Delivered.
  D3  a self-transition on the last state printed its loop and label over
      the end bullseye and the Running -> [*] arrow in LR.
  D4  `Note left of <first participant>` widened that participant's head
      box to twice the note instead of adding a left margin (about four
      times the other boxes for a 45-character note).
  and, seen in the same pictures: two labels fanning out of one state
  printed on top of each other; in LR the transition labels were wider than
  the 0.46 in layer gap and erased the box borders on both sides; the
  engine's browser timeline shipped "...first web brows…" (a `_wrap` cut)
  at 4.0 pt and its eight-task journey at 5.0 pt, unreadable in print.
"""
from __future__ import annotations

import logging

import pytest

from app.artifacts import md_import
from app.artifacts import spec as S
from app.artifacts.render import diagram_figures as F
from app.artifacts.render import diagrams as D

ENGINE = {
    "blog_er": "erDiagram\n    USERS {\n        int user_id PK\n        string username\n        string email\n        string password_hash\n        datetime created_at\n    }\n\n    POSTS {\n        int post_id PK\n        int user_id FK\n        string title\n        string content\n        datetime published_at\n        string status\n    }\n\n    COMMENTS {\n        int comment_id PK\n        int post_id FK\n        int user_id FK\n        string body\n        datetime created_at\n    }\n\n    TAGS {\n        int tag_id PK\n        string name\n        string slug\n    }\n\n    POST_TAGS {\n        int post_id FK\n        int tag_id FK\n    }\n\n    USERS ||--o{ POSTS : \"writes\"\n    USERS ||--o{ COMMENTS : \"writes\"\n    POSTS ||--o{ COMMENTS : \"has\"\n    POSTS }|--|{ TAGS : \"tagged\"",
    "order_state": "stateDiagram-v2\n    [*] --> Created: Place Order\n    Created --> Paid: Payment Successful\n    Paid --> Packed: Warehouse Processing\n    Packed --> Shipped: Dispatched\n    Shipped --> Delivered: Arrived at Destination\n    Delivered --> [*]\n    Created --> Cancelled: Customer Cancelled\n    Cancelled --> [*]\n    Delivered --> Running-return: Initiate Return\n    Running-return --> Running-return: retry\n    Running-return --> [*]",
    "seq_note_left": "sequenceDiagram\n    Note left of Browser: User types username and password\n    Browser->>API: POST /login {user, pass}\n    API->>Database: SELECT * FROM users\n    Database-->>API: Return user record\n    API-->>Browser: 200 OK with token",
    "flow_info": "flowchart TD\n    info[\"Info page\"]\n    info --> B[\"Next\"]\n    info --> contact",
    "order_state_lr": "stateDiagram-v2\n    direction LR\n    [*] --> Created: Order Placed\n    Created --> Paid: Payment Confirmed\n    Paid --> Shipped: Dispatched\n    Shipped --> Running: Processing\n    Running --> Running: retry\n    Running --> [*]: Completed",
    "seq_note_left_45": "sequenceDiagram\n    Note left of Customer: This is a 45 character long note text here\n    Customer->>Shop: Initiate Checkout\n    Shop->>Bank: Process Payment\n    Bank-->>Shop: Payment Approved\n    Shop-->>Customer: Order Confirmed",
    "journey_real": "journey\n    title Customer's Day with Online Bank\n    section Morning Routine\n      Check account balance: 5: Customer, Mobile App\n      Review recent transactions: 4: Customer, Web Portal\n      Pay utility bill: 3: Customer, Mobile App\n      Check credit score: 2: Customer, Web Portal\n    section Afternoon Tasks\n      Deposit paycheck via mobile: 5: Customer, Mobile App\n      Transfer funds to savings: 4: Customer, Web Portal\n      Report suspicious activity: 1: Customer, Support Chat\n      Update personal details: 3: Customer, Web Portal",
    "timeline_real": "timeline\n    section Early Web\n        1990 : Tim Berners-Lee creates the first web browser\n        1993 : Mosaic releases, popularizing the GUI web\n    section Browser Wars\n        1995 : Internet Explorer 1.0 launches\n        1998 : Mozilla Firefox project begins\n        2004 : Firefox 1.0 is released\n    section Modern Era\n        2008 : Google Chrome is launched\n        2013 : Microsoft announces Edge browser\n        2018 : Apple switches WebKit engine for Safari on iOS\n        2020 : Chromium-based Edge replaces EdgeHTML engine"
}


def _layout(key: str, box_in=D.PORTRAIT_BOX_IN):
    fields = D.parse_mermaid(ENGINE[key])
    assert fields is not None, key
    return D.layout_for(S.diagram_from_fields(fields), box_in=box_in)


def _crossings(layout) -> list:
    """(link, box) pairs where a relation's curve or its label lands inside
    a box that is not one of its two ends."""
    boxes = layout.detail["boxes"]
    bad = []
    for i, (a, b, rad, p, q, label_xy) in layout.detail["routes"].items():
        pts = F.bezier_points(p, q, rad, 48)[2:-2]
        for k, rect in boxes.items():
            if k in (a, b):
                continue
            if any(F._in_rect(pt, rect, 0.0) for pt in pts) or F._in_rect(label_xy, rect, 0.0):
                bad.append((i, a, b, k))
    return bad


# ----------------------------------------------------------------- D1 --


@pytest.mark.parametrize("source, ids", [
    (ENGINE["flow_info"], ["info", "B", "contact"]),
    ('flowchart TD\n  info["Info page"]\n  info --> B["Next"]', ["info", "B"]),
    ('flowchart TD\n  Info["Info page"]\n  Info --> B["Next"]', ["Info", "B"]),
    ("flowchart TD\n  Info\n  Info --> B", ["Info", "B"]),
    ('flowchart LR\n  timeline["Timeline"]\n  timeline --> B', ["timeline", "B"]),
    ('flowchart LR\n  pie["Pie chart"]\n  pie --> B', ["pie", "B"]),
    ('flowchart LR\n  graph["Graph view"]\n  graph --> B', ["graph", "B"]),
    ('flowchart LR\n  journey["Journey"]:::process\n  journey --> kanban["Board"]', ["journey", "kanban"]),
])
def test_a_node_that_spells_a_keyword_draws_when_declared_on_its_own_line(source, ids):
    """main (ae25da28) drew every one of these; 1db966b6 refused all but
    the edge-line form. mermaid's flowchart lexer reserves no words."""
    fields = D.parse_mermaid(source)
    assert fields is not None, source
    assert [n["id"] for n in fields["nodes"]] == ids


@pytest.mark.parametrize("source", [
    "flowchart TD\n  A --> B\n  classDiagram",     # a BARE keyword line is a keyword
    "flowchart TD\n  A --> B\n  info",
    "flowchart TD\n  A --> B\n  sequenceDiagram",
])
def test_a_bare_keyword_line_inside_a_flowchart_is_a_NODE(source):
    """MEASURED AGAINST MAIN, 2026-09-28, and main won.

    This was a refusal until the branch's own verifier drove it: main
    (ae25da28) DRAWS `flowchart TD / requirement / design / requirement -->
    design` and the same shape with `block`, `kanban`, `pie` and `info`,
    because mermaid's flowchart lexer reserves no words. The refusal cost five
    pictures the product already made, and declaring nodes on their own lines
    before the edges is a common habit of the model that writes them.

    The spelling can only mean a GRAMMAR at the head of the source, and
    `parse_mermaid`'s header dispatch already sends such a source to that
    keyword's own reader before this loop sees a line."""
    fields = D.parse_mermaid(source)
    assert fields is not None, source
    ids = [n["id"] for n in fields["nodes"]]
    last = source.strip().splitlines()[-1].strip()
    assert last in ids, (last, ids)


def test_a_keyword_at_the_HEAD_of_the_source_is_still_a_grammar():
    """The other half of the same rule: the refusal is not needed because the
    header decides first. A source that STARTS with `classDiagram` is read by
    the class reader, never by the flowchart loop, so nothing has to guard
    against a box labelled "classDiagram"."""
    assert D.parse_mermaid("classDiagram\n  class Order\n  class Customer\n  Order --> Customer") is not None
    drawn = D.parse_mermaid("flowchart TD\n  A --> B\n  classDiagram")
    assert drawn is not None and "classDiagram" in [n["id"] for n in drawn["nodes"]]


def test_a_keyword_spelled_node_draws_in_either_case():
    """`Info` and `info` are both nodes. The refusal it replaces was
    case-sensitive, which meant the same picture drew or vanished on a capital
    letter -- a distinction mermaid does not make in a flowchart."""
    for src in ("flowchart TD\n  A --> B\n  Info", "flowchart TD\n  A --> B\n  info",
                'flowchart TD\n  info["Info page"]\n  info --> B["Next"]'):
        assert D.parse_mermaid(src) is not None, src


# ----------------------------------------------------------------- D2 --


@pytest.mark.parametrize("key", ["blog_er", "order_state"])
def test_no_relation_runs_through_a_box_it_does_not_join(key):
    layout = _layout(key)
    assert _crossings(layout) == []


def test_the_two_relations_into_comments_land_apart_and_the_long_one_bows():
    layout = _layout("blog_er")
    routes = {(a, b): (rad, p, q, xy) for a, b, rad, p, q, xy in layout.detail["routes"].values()}
    users_comments = routes[("USERS", "COMMENTS")]
    posts_comments = routes[("POSTS", "COMMENTS")]
    assert users_comments[0] != 0.0, "USERS -> COMMENTS spans POSTS's layer and must bow"
    assert posts_comments[0] == 0.0
    qa, qb = users_comments[2], posts_comments[2]
    assert abs(qa[0] - qb[0]) > 0.3, "the two crow's feet on COMMENTS printed on top of each other"
    posts = layout.detail["boxes"]["POSTS"]
    assert not F._in_rect(users_comments[3], posts, 0.0), "the `writes` label sat on a POSTS row"


def test_a_bow_and_its_label_stay_inside_the_figure():
    for key in ("blog_er", "order_state"):
        layout = _layout(key)
        W, H = layout.fig_in
        for a, b, rad, p, q, xy in layout.detail["routes"].values():
            for x, y in F.bezier_points(p, q, rad, 24):
                assert 0.0 <= x <= W and 0.0 <= y <= H, (key, a, b)
            assert 0.0 <= xy[0] <= W and 0.0 <= xy[1] <= H


def test_labels_fanning_out_of_one_state_do_not_overprint():
    layout = _layout("order_state")
    d = S.diagram_from_fields(D.parse_mermaid(ENGINE["order_state"]))
    labels = {i: t.label for i, t in enumerate(d.transitions)}
    rects = {}
    for i, (a, b, rad, p, q, xy) in layout.detail["routes"].items():
        if labels[i]:
            w, h = F._tw(labels[i], F.SMALL_PT), F._lh(F.SMALL_PT)
            rects[i] = (xy[0] - w / 2, xy[1] - h / 2, w, h)
    idx = sorted(rects)
    for n, i in enumerate(idx):
        for j in idx[n + 1:]:
            assert not F._rects_overlap(rects[i], rects[j], 0.0), (labels[i], labels[j])


def test_twins_bow_on_opposite_sides():
    d = S.diagram_from_fields(D.parse_mermaid("stateDiagram-v2\n  [*] --> A\n  A --> B : pause\n  B --> A : resume\n  B --> [*]"))
    layout = D.layout_for(d)
    routes = {(a, b): (rad, p, q, xy) for a, b, rad, p, q, xy in layout.detail["routes"].values()}
    ab, ba = routes[("A", "B")], routes[("B", "A")]
    assert ab[0] > 0 and ba[0] > 0
    # the same sign on reversed chords is the opposite side of the page
    assert abs(ab[3][0] - ba[3][0]) > 0.4 or abs(ab[3][1] - ba[3][1]) > 0.4


# ----------------------------------------------------------------- D3 --


def test_a_self_loop_on_the_last_state_sits_on_the_across_side_and_clears_the_end():
    d = S.diagram_from_fields(D.parse_mermaid(ENGINE["order_state_lr"]))
    for direction in ("LR", "TD"):
        layout = F.plan_state(d, direction)
        layout.draw(_NullAxes())
        loop = layout.detail["loops"]["Running"]
        boxes = layout.detail["boxes"]
        running = boxes["Running"]
        if direction == "LR":
            assert loop[1] >= running[1] + running[3] - 1e-6, "the loop hangs BELOW the box in LR"
        else:
            assert loop[0] >= running[0] + running[2] - 1e-6, "the loop sits RIGHT of the box in TD"
        for k, rect in boxes.items():
            if k != "Running":
                assert not F._rects_overlap(loop, rect, 0.0), (direction, k)
        W, H = layout.fig_in
        assert loop[0] + loop[2] <= W + 1e-6 and loop[1] + loop[3] <= H + 1e-6
        assert _crossings(layout) == []


class _NullAxes:
    """Enough of an Axes for a plan's draw() to run without a figure."""

    def add_patch(self, *_a, **_k):
        return None

    def plot(self, *_a, **_k):
        return None

    def text(self, *_a, **_k):
        return None


def test_lr_layers_leave_room_for_the_widest_transition_label():
    d = S.diagram_from_fields(D.parse_mermaid(ENGINE["order_state_lr"]))
    layout = F.plan_state(d, "LR")
    boxes = layout.detail["boxes"]
    created, paid = boxes["Created"], boxes["Paid"]
    gap = paid[0] - (created[0] + created[2])
    assert gap >= F._tw("Payment Confirmed", F.SMALL_PT) + 0.2, gap


# ----------------------------------------------------------------- D4 --


def test_a_left_note_adds_a_margin_and_does_not_widen_the_first_head_box():
    layout = _layout("seq_note_left_45")
    widths = layout.detail["head_widths"]
    plain = max(1.0, F._tw("Customer") + 0.36)
    assert abs(widths[0] - plain) < 1e-6, widths
    assert max(widths) / min(widths) < 1.3
    assert layout.detail["left_extra"] > 0.5
    note = "This is a 45 character long note text here"
    w = F._tw(F._wrap(note, 28)) + 0.3
    left_edge = layout.detail["lifelines"]["Customer"] - 0.12 - w
    assert left_edge >= 0.04
    assert layout.fits and layout.effective_pt >= 9.0


# ---------------------------------------------- folding and no cutting --


@pytest.mark.parametrize("key, min_rows", [("journey_real", 2), ("timeline_real", 3)])
def test_the_horizontal_families_fold_into_rows_and_print_at_full_size(key, min_rows):
    layout = _layout(key)
    assert layout.detail["rows"] >= min_rows
    assert layout.fig_in[0] <= D.PORTRAIT_BOX_IN[0] + 1e-6
    assert layout.fits and layout.effective_pt == pytest.approx(F.FONT_PT)


def test_wrap_never_cuts_a_label():
    text = "Tim Berners-Lee creates the first web browser"
    assert "…" not in F._wrap(text, 16)
    assert " ".join(F._wrap(text, 16).split("\n")) == text
    long = " ".join(["word"] * 40)
    assert " ".join(F._wrap(long, 22).split("\n")) == long


def test_a_mindmap_line_between_41_and_48_characters_draws():
    """A plain mindmap line is its own id; the id cap (40) was shorter than
    the label cap (48), so this map raised out of validation on the branch
    tip and was refused whole once the reader validated."""
    line = "Grammars read by regular expressions, closed"     # 44 characters
    fields = D.parse_mermaid(f"mindmap\n  root((Product))\n    Parsing\n      {line}\n    Rendering")
    assert fields is not None
    d = S.diagram_from_fields(fields)
    assert [n.label for n in d.nodes] == ["Product", "Parsing", line, "Rendering"]
    assert d.nodes[2].parent == "Parsing" and len(d.nodes[2].id) <= 40
    assert D.layout_for(d).fits


def test_the_section_bands_survive_the_fold():
    layout = _layout("timeline_real")
    names = [b[0] for b in layout.detail["bands"]]
    assert names[0] == "Early Web" and names[-1] == "Modern Era" and "Browser Wars" in names


# --------------------------------------------- the reader refuses caps --


def test_an_over_cap_source_is_refused_by_the_reader_not_raised(caplog):
    src = "erDiagram\n" + "".join(f"  E{i} ||--o{{ E{i + 1} : has\n" for i in range(17))
    assert D.parse_mermaid(src) is None
    with caplog.at_level(logging.INFO, logger="app.artifacts.md_import"):
        doc, notes = md_import.markdown_to_document(f"# T\n\n```mermaid\n{src}\n```\n")
    assert [b.type for b in doc.blocks if b.type == "callout"] == ["callout"]
    assert not any("did not build" in r.getMessage() for r in caplog.records)


# ------------------------------------------------ the whole seam, once --


def test_the_engine_sources_reach_a_docx_and_a_pdf_without_a_warning(tmp_path):
    pytest.importorskip("weasyprint")
    from app.artifacts.render import render_version

    md = "# Engine\n\n" + "".join(f"## {k}\n\n```mermaid\n{src}\n```\n\n" for k, src in ENGINE.items())
    doc, notes = md_import.markdown_to_document(md)
    assert sum(1 for b in doc.blocks if b.type == "diagram") == len(ENGINE)
    assert not any("omitted" in n.lower() for n in notes)
    report = render_version(S.ArtifactSpec(kind="document", document=doc), ["docx", "pdf"], tmp_path,
                            title_slug="engine", version=1, effort="think")
    assert report.warnings == [], report.warnings
    assert len(report.chart_files) == len(ENGINE)
