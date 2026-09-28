"""Every mermaid diagram type reaches a file, or refuses with its reason.

THE DEFECT, measured in the running container (ae25da28) on 2026-09-28 with
`parse_mermaid` over one representative source per grammar: of the 31
grammars mermaid 11.17.0 registers (read off its own detector regexes in
frontend/node_modules/mermaid/dist/mermaid.core.mjs, 38 ids folded by
renderer variant), ONE drew — the flowchart — and thirty refused to the
"Diagram omitted" callout. Worse, two of the thirty drew a WRONG picture
when written with bare arrows: `classDiagram\\nA --> B\\nB --> C` came back
as four boxes, one of them labelled "classDiagram", because the keyword
line fell through to the flowchart's node-declaration rule, and
`stateDiagram-v2\\nIdle --> Busy\\nBusy --> Done` did the same.

THE SHAPE OF THE FIX. One reader per grammar (render/mermaid_grammars.py)
and one drawer per family (render/diagram_figures.py), because these are
different grammars and not one grammar with more keywords. The header
decides: a keyword the readers know goes to its reader; the six chart types
refuse as charts (their numbers were typed by the model, and a document's
charts are computed from data); every other registered keyword refuses at
the header with a stated reason, and a keyword can no longer become a box.
The prompt (DIAGRAM_INSTRUCTION) names exactly the ten types the file path
draws, pinned in tests/test_diagram_instruction_budget.py.

EVERY TEST HERE IS A GUARD THAT GOES RED WHEN ITS FIX IS REVERTED, checked
that way on 2026-09-28 (the table test, the wrong-picture tests, the render
tests and the callout test all fail on ae25da28's parse_mermaid / md_import;
see the commit message for the commands).
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.artifacts import md_import
from app.artifacts import spec as S
from app.artifacts.render import diagrams as D
from app.artifacts.render import mermaid_grammars as G

# --------------------------------------------------- the thirty-one grammars --

#: One representative source per grammar mermaid 11.17.0 registers, and the
#: disposition each has on this tree: ("graph", None) draws through the
#: flowchart reader; ("family", name) through a family reader;
#: ("chart", name) refuses as a chart; ("excluded", name) refuses at the
#: header with G.EXCLUDED_REASONS[name].
THIRTY_ONE = {
    "architecture": ("architecture-beta\n  group api(cloud)[API]\n  service db(database)[Database] in api\n  db:L -- R:api", ("excluded", "architecture")),
    "block": ("block-beta\n  columns 3\n  a:3\n  b c d", ("excluded", "block")),
    "c4": ('C4Context\n  Person(customer, "Customer")\n  System(bank, "Bank")\n  Rel(customer, bank, "Uses")', ("excluded", "c4")),
    "class": ("classDiagram\n  class Animal {\n    +String name\n    +speak()\n  }\n  Animal <|-- Dog", ("family", "class")),
    "cynefin": ("cynefin-beta\n  title Sample\n  Clear: Simple stuff", ("excluded", "cynefin")),
    "er": ("erDiagram\n  CUSTOMER ||--o{ ORDER : places\n  ORDER ||--|{ LINE : contains", ("family", "er")),
    "eventmodeling": ('eventmodeling\n  swimlane cmd "Commands"\n  cmd: Command PlaceOrder', ("excluded", "eventmodeling")),
    "flowchart": ('flowchart TD\n  A["Ingest"] --> B["Index"]\n  B --> C["Answer"]', ("graph", None)),
    "gantt": ("gantt\n  title Plan\n  dateFormat YYYY-MM-DD\n  section Build\n  Parse :a1, 2026-09-28, 3d", ("excluded", "gantt")),
    "git": ("gitGraph\n  commit\n  branch dev\n  commit\n  checkout main\n  merge dev", ("excluded", "git")),
    "ishikawa": ('ishikawa-beta\n  root("Defect")\n    cause("People")', ("excluded", "ishikawa")),
    "kanban": ("kanban\n  Todo\n    t1[Write parser]\n  Done\n    t2[Read grammar]", ("family", "kanban")),
    "mindmap": ("mindmap\n  root((Product))\n    Parsing\n      Grammars\n    Rendering", ("family", "mindmap")),
    "packet": ('packet-beta\n  0-15: "Source Port"\n  16-31: "Destination Port"', ("family", "packet")),
    "pie": ('pie title Share\n  "A" : 40\n  "B" : 60', ("chart", "pie")),
    "quadrant-chart": ("quadrantChart\n  title Reach\n  x-axis Low --> High\n  y-axis Low --> High\n  A: [0.3, 0.6]", ("chart", "quadrant-chart")),
    "radar": ('radar-beta\n  title Skills\n  axis a["A"], b["B"]\n  curve x["X"]{1, 2}', ("chart", "radar")),
    "railroad": ("railroad-beta\n  expr ::= term ('+' term)*", ("excluded", "railroad")),
    "requirement": ("requirementDiagram\n  requirement r1 {\n    id: 1\n    text: must parse\n  }\n  element e1 {\n    type: module\n  }\n  e1 - satisfies -> r1", ("excluded", "requirement")),
    "sankey": ("sankey-beta\n  A,B,10\n  B,C,5", ("chart", "sankey")),
    "sequence": ("sequenceDiagram\n  participant U as User\n  participant S as Server\n  U->>S: login\n  S-->>U: token", ("family", "sequence")),
    "state": ("stateDiagram-v2\n  [*] --> Idle\n  Idle --> Busy : start\n  Busy --> [*]", ("family", "state")),
    "swimlanes": ("swimlane-beta\n  lane A\n  lane B", ("excluded", "swimlanes")),
    "timeline": ("timeline\n  title History\n  2002 : LinkedIn\n  2004 : Facebook : Google", ("family", "timeline")),
    "treemap": ('treemap-beta\n  "Root"\n    "A": 10\n    "B": 20', ("chart", "treemap")),
    "treeView": ("treeView-beta\n  root\n    child", ("excluded", "treeView")),
    "user-journey": ("journey\n  title Day\n  section Morning\n    Wake: 5: Me\n    Coffee: 3: Me", ("family", "journey")),
    "venn": ("venn-beta\n  set A\n  set B\n  A & B", ("excluded", "venn")),
    "wardley": ("wardley-beta\n  title Map\n  component Customer [0.9, 0.7]", ("excluded", "wardley")),
    "xychart": ('xychart-beta\n  title Sales\n  x-axis [a, b]\n  y-axis "n" 0 --> 10\n  bar [3, 7]', ("chart", "xychart")),
    "info": ("info", ("excluded", "info")),
}

#: What production (ae25da28) did with the same 31 sources, measured in the
#: running container on 2026-09-28: the flowchart drew, everything else
#: refused. (The bare-arrow class/state wrong pictures are separate sources,
#: tested below.)
PRODUCTION_DREW = {"flowchart"}


def test_the_table_has_thirty_one_rows_and_every_registered_keyword_is_in_it():
    assert len(THIRTY_ONE) == 31
    # Every keyword mermaid registers is classified by header_kind and the
    # classification is one of the four the table uses.
    kinds = {}
    for kw in G.MERMAID_KEYWORDS:
        kinds[kw] = G.header_kind(kw + "\n  x")[0]
    assert set(kinds.values()) <= {"flowchart", "family", "chart", "excluded"}, kinds
    # No excluded grammar is missing its reason, and no reason is orphaned.
    assert set(G.EXCLUDED_OF_KEYWORD.values()) - {"sequence"} == set(G.EXCLUDED_REASONS)


@pytest.mark.parametrize("name", sorted(THIRTY_ONE))
def test_each_of_the_thirty_one_grammars_draws_or_refuses_as_the_table_says(name):
    source, (kind, family) = THIRTY_ONE[name]
    hk, hn = G.header_kind(source)
    fields = D.parse_mermaid(source)
    if kind == "graph":
        assert hk == "flowchart"
        assert fields is not None and "family" not in fields
        assert isinstance(S.diagram_from_fields(fields), S.Diagram)
    elif kind == "family":
        assert (hk, hn) == ("family", family)
        assert fields is not None and fields["family"] == family, f"{name} should be read by the {family} reader"
        diagram = S.diagram_from_fields(fields)
        assert S.diagram_family(diagram) == family
        # ...and laid out: every admitted family has a drawer.
        layout = D.layout_for(diagram)
        assert layout.fig_in[0] > 0 and layout.fig_in[1] > 0
    elif kind == "chart":
        assert (hk, hn) == ("chart", family)
        assert fields is None, f"{name} draws numbers the model typed and must refuse"
    else:
        assert (hk, hn) == ("excluded", family)
        assert fields is None, f"{name} is excluded and must refuse"
        assert G.EXCLUDED_REASONS[family], f"{name} needs a stated reason"


def test_the_widening_is_from_one_to_ten_of_thirty_one():
    """The before/after count, as a number the suite computes."""
    after = {n for n, (_src, (k, _f)) in THIRTY_ONE.items() if k in ("graph", "family")}
    assert PRODUCTION_DREW == {"flowchart"}
    assert len(after) == 10 and after == {"flowchart", "sequence", "er", "class", "state", "mindmap", "timeline",
                                          "user-journey", "kanban", "packet"}
    charts = {n for n, (_s, (k, _f)) in THIRTY_ONE.items() if k == "chart"}
    assert charts == {"pie", "quadrant-chart", "radar", "sankey", "treemap", "xychart"}
    assert len(THIRTY_ONE) - len(after) - len(charts) == 15


# ------------------------------------------------ never a wrong picture --


@pytest.mark.parametrize("source,family", [
    ("classDiagram\nA --> B\nB --> C", "class"),
    ("stateDiagram-v2\nIdle --> Busy\nBusy --> Done", "state"),
    ("stateDiagram\nIdle --> Busy", "state"),
])
def test_a_keyword_line_is_never_drawn_as_a_box(source, family):
    """Production (ae25da28) drew `classDiagram\\nA --> B\\nB --> C` as FOUR
    boxes — 'classDiagram', A, B, C — and the state source as four with a
    box called 'stateDiagram-v2'. Measured in the running container,
    2026-09-28. Now the header sends the source to its own reader and the
    keyword is a keyword."""
    fields = D.parse_mermaid(source)
    assert fields is not None and fields["family"] == family
    ids = [x["id"] for x in fields.get("classes", fields.get("states", []))]
    assert not any(G.is_keyword(i) for i in ids), ids
    assert "classDiagram" not in ids and "stateDiagram-v2" not in ids


@pytest.mark.parametrize("source", [
    # a keyword in the middle of a flowchart, which used to be a node
    'flowchart TD\n  A["a"] --> B["b"]\n  gantt\n  B --> C["c"]',
    'flowchart TD\n  A --> B\n  sequenceDiagram',
])
def test_a_keyword_inside_a_flowchart_refuses_the_source(source):
    assert D.parse_mermaid(source) is None


@pytest.mark.parametrize("source", [
    # sequence: constructs mermaid draws but this drawer cannot lay out faithfully
    "sequenceDiagram\n  A->>+B: hi\n  B-->>-A: bye",           # activation bars
    "sequenceDiagram\n  A->>B: hi\n  critical x\n  A->>B: y\n  end",
    "sequenceDiagram\n  A->>B: hi\n  activate B",
    "sequenceDiagram\n  A->>B: <b>bold</b>",                   # markup in a label
    "sequenceDiagram\n  A->>B: hi\n  alt x\n  A->>B: y",        # unclosed frame
    "sequenceDiagram\n  participant A\n  participant B",       # no message
    # er
    "erDiagram\n  A ||--o{ B : has\n  A {\n    int id PK",     # unclosed entity
    "erDiagram\n  A ||--o{ gantt : has",                        # keyword as entity
    # class
    "classDiagram\n  namespace X {\n    class A\n  }",
    "classDiagram\n  A --> B\n  note for A \"x\"",
    "classDiagram\n  A --> B\n  style A fill:#f00",
    # state
    "stateDiagram-v2\n  state Fork <<fork>>\n  [*] --> Fork",
    "stateDiagram-v2\n  [*] --> A\n  state A {\n    B --> C\n  }",
    "stateDiagram-v2\n  [*] --> A\n  note right of A : x",
    # mindmap
    "mindmap\n  root((a))\n    b\n  second((root))",            # two roots
    "mindmap\n  root((a))\n    b::icon(fa fa-book)",
    "mindmap\n  root)cloud(",
    # timeline / journey / kanban / packet
    "timeline\n  2002 : <i>x</i>",
    "journey\n  Wake: 9: Me",                                   # not a face
    "journey\n  accTitle: x\n  Wake: 3: Me",
    "kanban\n  Todo\n    t1[x]@{ ticket: 1 }",                  # metadata dropped would be a wrong card
    "kanban\n  Todo\n    t1[x]\n      deeper",
    'packet-beta\n  0-15: "a"\n  20-31: "b"',                   # a gap
    'packet-beta\n  0-15: "a"\n  8-31: "b"',                    # an overlap
    'packet-beta\n  +16: "a"',                                  # relative form, not read
    # directives everywhere
    "%%{init: {'theme':'dark'}}%%\nsequenceDiagram\n  A->>B: x",
    "---\nconfig:\n  theme: dark\n---\nsequenceDiagram\n  A->>B: x",
])
def test_a_construct_the_drawer_cannot_carry_refuses_the_whole_source(source):
    """Refuse whole, never drop: a picture missing a construct the author
    wrote is a wrong picture, and the callout is the honest answer."""
    assert D.parse_mermaid(source) is None, source


def test_what_is_read_is_what_is_drawn_in_the_sequence_family():
    src = ("sequenceDiagram\n  autonumber\n  actor U as User\n  participant S\n  U->>S: login\n"
           "  alt ok\n    S-->>U: token\n  else bad\n    S-xU: refused\n  end\n  Note over U,S: done\n  U-)S: async")
    f = D.parse_mermaid(src)
    d = S.diagram_from_fields(f)
    assert [p.id for p in d.participants] == ["U", "S"] and d.participants[0].actor and d.participants[0].label == "User"
    kinds = [s.kind for s in d.steps]
    assert kinds == ["message", "frame_open", "message", "frame_divide", "message", "frame_close", "note", "message"]
    msgs = [s for s in d.steps if s.kind == "message"]
    assert [(m.line, m.head) for m in msgs] == [("solid", "filled"), ("dashed", "filled"), ("solid", "cross"), ("solid", "open")]
    assert d.autonumber
    layout = D.layout_for(d)
    assert set(layout.detail["lifelines"]) == {"U", "S"}


def test_what_is_read_is_what_is_drawn_in_the_er_family():
    src = ('erDiagram\n  CUSTOMER ||--o{ ORDER : places\n  PRODUCT }o..o| LINE : "appears in"\n'
           '  CUSTOMER {\n    int id PK\n    string email UK "unique"\n  }')
    d = S.diagram_from_fields(D.parse_mermaid(src))
    assert [e.id for e in d.entities] == ["CUSTOMER", "ORDER", "PRODUCT", "LINE"]
    r0, r1 = d.relations
    assert (r0.source_card, r0.target_card, r0.identifying, r0.label) == ("exactly_one", "zero_or_more", True, "places")
    assert (r1.source_card, r1.target_card, r1.identifying, r1.label) == ("zero_or_more", "zero_or_one", False, "appears in")
    attrs = d.entities[0].attributes
    assert [(a.type, a.name, a.keys, a.comment) for a in attrs] == [("int", "id", "PK", ""), ("string", "email", "UK", "unique")]
    boxes = D.layout_for(d).detail["boxes"]
    assert set(boxes) == {"CUSTOMER", "ORDER", "PRODUCT", "LINE"}


def test_what_is_read_is_what_is_drawn_in_the_class_family():
    src = ('classDiagram\n  class Animal {\n    <<abstract>>\n    +String name\n    +speak() String\n    -List~int~ ids\n  }\n'
           '  Animal <|-- Dog\n  Dog *-- Collar\n  Owner "1" --> "*" Dog : owns\n  Dog ..> Food')
    d = S.diagram_from_fields(D.parse_mermaid(src))
    a = d.classes[0]
    assert (a.annotation, a.attributes, a.methods) == ("abstract", ["+String name", "-List<int> ids"], ["+speak() : String"])
    heads = [(r.source, r.target, r.source_head, r.target_head, r.line) for r in d.relations]
    assert heads == [("Animal", "Dog", "inheritance", "none", "solid"), ("Dog", "Collar", "composition", "none", "solid"),
                     ("Owner", "Dog", "none", "arrow", "solid"), ("Dog", "Food", "none", "arrow", "dashed")]
    assert (d.relations[2].source_card, d.relations[2].target_card, d.relations[2].label) == ("1", "*", "owns")


def test_what_is_read_is_what_is_drawn_in_the_state_mindmap_timeline_families():
    st = S.diagram_from_fields(D.parse_mermaid('stateDiagram-v2\n  [*] --> Idle\n  Idle --> Busy : go\n  Busy : working\n  Busy --> [*]'))
    assert [(t.source, t.target, t.label) for t in st.transitions] == [(S.STATE_START, "Idle", ""), ("Idle", "Busy", "go"), ("Busy", S.STATE_END, "")]
    assert st.states[1].lines == ["working"]
    mm = S.diagram_from_fields(D.parse_mermaid("mindmap\n  root((Top))\n    A\n      A1\n    B[sq]\n    C{{hex}}"))
    # `root((Top))` is id "root" with label "Top", as mermaid reads it.
    assert [(n.id, n.label, n.parent, n.shape) for n in mm.nodes] == [
        ("root", "Top", None, "circle"), ("A", "A", "root", "default"), ("A1", "A1", "A", "default"),
        ("B", "sq", "root", "square"), ("C", "hex", "root", "hexagon")]
    tl = S.diagram_from_fields(D.parse_mermaid("timeline\n  title T\n  section S1\n    2002 : LinkedIn\n    2004 : Facebook : Google\n  section S2\n    2005 : YouTube"))
    assert tl.title == "T"
    assert [(p.time, p.section, p.events) for p in tl.periods] == [("2002", "S1", ["LinkedIn"]), ("2004", "S1", ["Facebook", "Google"]), ("2005", "S2", ["YouTube"])]
    bands = D.layout_for(tl).detail["bands"]
    assert [b[0] for b in bands] == ["S1", "S2"]


def test_what_is_read_is_what_is_drawn_in_the_journey_kanban_packet_families():
    jn = S.diagram_from_fields(D.parse_mermaid("journey\n  title D\n  section Ask\n    Type: 4: User\n    Wait: 2: User, Cat\n  section Read\n    Read: 5"))
    assert [(t.name, t.section, t.score, t.actors) for t in jn.tasks] == [("Type", "Ask", 4, ["User"]), ("Wait", "Ask", 2, ["User", "Cat"]), ("Read", "Read", 5, [])]
    assert D.layout_for(jn).detail["scores"] == [4, 2, 5]
    kb = S.diagram_from_fields(D.parse_mermaid("kanban\n  todo[To do]\n    a[Card A]\n    Plain card\n  Done"))
    assert [(c.id, c.label, c.cards) for c in kb.columns] == [("todo", "To do", ["Card A", "Plain card"]), ("Done", "Done", [])]
    pk = S.diagram_from_fields(D.parse_mermaid('packet-beta\ntitle UDP\n0-15: "Source"\n16-31: "Dest"\n32-63: "Length and checksum"\n64: "Flag"'))
    assert pk.title == "UDP"
    assert [(f.start, f.end) for f in pk.fields] == [(0, 15), (16, 31), (32, 63), (64, 64)]
    cells = D.layout_for(pk).detail["cells"]
    assert cells == [(0, 0, 15), (0, 16, 31), (1, 0, 31), (2, 0, 0)], "a field never crosses a 32-bit row without splitting"


def test_the_frontmatter_title_reaches_the_family():
    d = S.diagram_from_fields(D.parse_mermaid("---\ntitle: Login flow\n---\nsequenceDiagram\n  A->>B: hi"))
    assert d.title == "Login flow"


# ------------------------------------------------------ md_import + files --


def test_a_chart_written_as_a_diagram_becomes_the_chart_callout_not_a_picture():
    """1c: a pie with typed numbers must not be drawn as if it were a chart
    of data. RED on ae25da28, where it was the generic 'Diagram omitted'."""
    doc, notes = md_import.markdown_to_document('# T\n\n```mermaid\npie title Share\n  "A" : 40\n  "B" : 60\n```\n')
    callouts = [b for b in doc.blocks if b.type == "callout"]
    assert len(callouts) == 1 and callouts[0].title == md_import.CHART_NOT_A_DIAGRAM_TITLE
    assert "pie" in callouts[0].text and "drawn from data" in callouts[0].text
    assert any("pie" in n and "chart of the data" in n for n in notes)
    assert not any(b.type == "diagram" for b in doc.blocks)


def test_an_excluded_grammar_keeps_the_plain_callout():
    doc, notes = md_import.markdown_to_document("# T\n\n```mermaid\ngantt\n  title x\n```\n")
    callouts = [b for b in doc.blocks if b.type == "callout"]
    assert len(callouts) == 1 and callouts[0].title == "Diagram omitted"


FAMILY_FENCES = {
    "sequence": "sequenceDiagram\n  actor U as User\n  participant S as Server\n  U->>S: login\n  S-->>U: token",
    "er": "erDiagram\n  CUSTOMER ||--o{ ORDER : places\n  CUSTOMER {\n    int id PK\n  }",
    "class": "classDiagram\n  class Animal {\n    +speak()\n  }\n  Animal <|-- Dog",
    "state": "stateDiagram-v2\n  [*] --> Idle\n  Idle --> Busy : start\n  Busy --> [*]",
    "mindmap": "mindmap\n  root((Product))\n    Parsing\n    Rendering",
    "timeline": "timeline\n  section S\n    2002 : LinkedIn\n    2004 : Facebook",
    "journey": "journey\n  section Ask\n    Type: 4: User\n    Wait: 2: User",
    "kanban": "kanban\n  Todo\n    t1[Write]\n  Done\n    t2[Read]",
    "packet": 'packet-beta\n0-15: "Source Port"\n16-31: "Destination Port"',
}


def test_every_family_renders_into_the_docx_and_the_pdf(tmp_path):
    """The whole seam with zero model calls: nine family fences through
    md_import into a document, rendered to DOCX and PDF by the real renderer.
    The DOCX carries one media part per family and the PDF one image object
    per family. RED on ae25da28: one 'Diagram omitted' callout per fence, 0
    media parts, 0 images."""
    pytest.importorskip("weasyprint")
    import pypdfium2 as pdfium
    from app.artifacts.render import render_version

    md = "# Families\n\n" + "".join(f"## {k}\n\n```mermaid\n{src}\n```\n\nText.\n\n" for k, src in FAMILY_FENCES.items())
    doc, notes = md_import.markdown_to_document(md)
    families = [S.diagram_family(b.diagram) for b in doc.blocks if b.type == "diagram"]
    assert families == list(FAMILY_FENCES)
    assert not any("omitted" in n.lower() for n in notes)
    report = render_version(S.ArtifactSpec(kind="document", document=doc), ["docx", "pdf"], tmp_path,
                            title_slug="fam", version=1, effort="think")
    assert report.chart_files == [f"diagram-{i}.png" for i in range(1, 10)]
    assert report.warnings == []
    docx_path = next(Path(v) for k, v in report.paths.items() if k.endswith(":docx:"))
    with zipfile.ZipFile(docx_path) as z:
        media = [n for n in z.namelist() if n.startswith("word/media/")]
    assert len(media) == 9, media
    pdf_path = next(Path(v) for k, v in report.paths.items() if k.endswith(":pdf:"))
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        images = sum(1 for i in range(len(pdf)) for obj in pdf[i].get_objects() if obj.type == 3)
    finally:
        pdf.close()
    assert images == 9


@pytest.mark.parametrize("family", sorted(FAMILY_FENCES))
def test_each_family_png_is_paper_sized_and_its_labels_are_readable(family, tmp_path):
    d = S.diagram_from_fields(D.parse_mermaid(FAMILY_FENCES[family]))
    layout = D.render_diagram_png(d, tmp_path / "d.png")
    w, h = D.png_size_in(tmp_path / "d.png")
    assert abs(w - layout.fig_in[0]) < 0.02 and abs(h - layout.fig_in[1]) < 0.02
    assert layout.fits, f"{family}: {layout.effective_pt:.1f} pt"
    assert layout.display_in[0] <= D.PORTRAIT_BOX_IN[0] + 1e-6


def test_the_largest_allowed_instances_still_draw_and_report_their_scale():
    """The spec caps bound the figure sizes; the wide one that cannot fold
    (twelve lifelines) does not fit a portrait page at 8 pt and SAYS so
    through `fits`, rather than shipping unreadable text. Twenty-four
    timeline periods FOLD into rows and fit (they shipped at under 4 pt
    until 2026-09-28; tests/test_diagram_drawers_pictures.py)."""
    twelve = "sequenceDiagram\n" + "".join(f"  participant P{i} as Participant {i}\n" for i in range(12)) + "  P0->>P11: across\n"
    lay = D.layout_for(S.diagram_from_fields(D.parse_mermaid(twelve)))
    assert lay.fig_in[0] > D.PORTRAIT_BOX_IN[0] and not lay.fits and lay.effective_pt < D.MIN_EFFECTIVE_PT
    periods = "timeline\n" + "".join(f"  {2000 + i} : event {i}\n" for i in range(24))
    lay = D.layout_for(S.diagram_from_fields(D.parse_mermaid(periods)))
    assert lay.fits and lay.detail["rows"] >= 4 and lay.fig_in[0] <= D.PORTRAIT_BOX_IN[0]
    # A deep-but-narrow one fits: eight states in a chain are ten layers
    # with the start dot and the end bullseye, 8.18 in tall at the 0.46 in
    # layer gap, inside the 8.4 in portrait box (measured 2026-09-28; at the
    # 0.7 in gap the same chain was 10.18 in and printed at 7.8 pt).
    chain = "stateDiagram-v2\n  [*] --> S0\n" + "".join(f"  S{i} --> S{i + 1}\n" for i in range(7)) + "  S7 --> [*]\n"
    lay = D.layout_for(S.diagram_from_fields(D.parse_mermaid(chain)))
    assert lay.fits and lay.direction == "TD", (lay.fig_in, lay.effective_pt)


def test_the_render_cost_of_every_family_is_small(tmp_path):
    import time

    for family, src in FAMILY_FENCES.items():
        d = S.diagram_from_fields(D.parse_mermaid(src))
        D.render_diagram_png(d, tmp_path / "warm.png")   # matplotlib warm-up is not the family's cost
        t0 = time.perf_counter()
        D.render_diagram_png(d, tmp_path / f"{family}.png")
        assert time.perf_counter() - t0 < 2.0, family


# --------------------------------------------------- the model's schema --


def test_the_guided_decoding_schema_the_model_sees_is_unchanged():
    """`DiagramBlock.diagram` accepts the families through a SkipJsonSchema
    arm, so the JSON schema the composer constrains the model to is exactly
    the `Diagram` it always was: no family field, no sequence/er/class
    branch. The model never writes these; only the mermaid reader does."""
    schema = S.DiagramBlock.model_json_schema()
    text = json.dumps(schema)
    for word in ("family", "participants", "lifeline", "SequenceDiagram", "ErDiagram", "KanbanDiagram", "PacketDiagram"):
        assert word not in text, word
    props = schema["properties"]["diagram"]
    assert "$ref" in props and props["$ref"].endswith("/Diagram")


def test_a_family_dict_the_model_might_emit_still_validates_into_a_typed_block():
    """The arm is skipped in the schema, not in validation: compose's
    `_salvage_diagrams` calls `DiagramBlock.model_validate`, and a family
    dict validates like any other typed block rather than raising."""
    fields = D.parse_mermaid(FAMILY_FENCES["kanban"])
    block = S.DiagramBlock.model_validate({"type": "diagram", "diagram": fields})
    assert isinstance(block.diagram, S.KanbanDiagram)
    with pytest.raises(Exception):
        S.DiagramBlock.model_validate({"type": "diagram", "diagram": {"family": "packet", "fields": [{"start": 4, "end": 8, "label": "gap"}]}})
