"""Diagrams in a document: the layout, the 8 pt floor, the colours, the
escaping, and the picture that reaches the DOCX and the PDF.

WHAT THESE TESTS ARE FOR. The owner's report came back with zero image
objects in the PDF, no colour and — on the two diagrams that were measured
by hand — labels at 6.2 pt and 5.4 pt, which is under the size at which a
printed label can be read. Every number below is one of those, turned into
an assertion:

    layout        a cycle, a self-loop, a loop spanning four layers, a
                  disconnected graph and the 15-node architecture graph all
                  lay out with every node on the page and no two boxes
                  overlapping
    8 pt floor    `FONT_PT * scale >= 8` for every diagram in the corpus
    colour        the four role colours separate under the dataviz
                  validator's all-pairs rule, AND four distinct fills are
                  read back out of the rendered pixels
    escaping      a fence whose text is `</pre><script>` and whose info
                  string is `x" onload="` produces no `<script` and no
                  broken attribute in the HTML or the PDF, and reaches the
                  DOCX as text
    the file      a DocumentSpec with one diagram renders a DOCX with
                  exactly one media part and a PDF with at least one image
"""
from __future__ import annotations

import re
import time
import zipfile
from pathlib import Path

import pytest

from app.artifacts import spec as S
from app.artifacts.render import diagrams as D
from app.artifacts.render import html as H

pytest.importorskip("matplotlib")


# ------------------------------------------------------------- the corpus --


def diagram(nodes, edges, **over) -> S.Diagram:
    base = dict(title="T", direction="TD", nodes=nodes, edges=edges)
    base.update(over)
    return S.Diagram(**base)


ARCH15 = diagram(
    [
        {"id": "U", "label": "300 enterprise users", "kind": "external"},
        {"id": "LB", "label": "Load balancer", "kind": "service"},
        {"id": "FE", "label": "Next.js frontend", "kind": "service"},
        {"id": "API", "label": "FastAPI backend", "kind": "service"},
        {"id": "AUTH", "label": "Auth and RBAC", "kind": "service"},
        {"id": "RAG", "label": "RAG pipeline", "kind": "service"},
        {"id": "CHAT", "label": "Chat orchestrator", "kind": "service"},
        {"id": "EMB", "label": "Embedding service", "kind": "model"},
        {"id": "VDB", "label": "Vector store", "kind": "store"},
        {"id": "VLLM", "label": "vLLM Qwen cluster", "kind": "model"},
        {"id": "DGX", "label": "10x DGX Spark", "kind": "model"},
        {"id": "PG", "label": "PostgreSQL", "kind": "store"},
        {"id": "REDIS", "label": "Redis cache", "kind": "store"},
        {"id": "STT", "label": "Speech to text", "kind": "model"},
        {"id": "TTS", "label": "Text to speech", "kind": "model"},
    ],
    [
        {"source": "U", "target": "LB"}, {"source": "LB", "target": "FE"},
        {"source": "FE", "target": "API"}, {"source": "API", "target": "AUTH"},
        {"source": "AUTH", "target": "RAG"}, {"source": "AUTH", "target": "CHAT"},
        {"source": "RAG", "target": "EMB"}, {"source": "RAG", "target": "VDB"},
        {"source": "CHAT", "target": "VLLM"}, {"source": "VLLM", "target": "DGX"},
        {"source": "API", "target": "PG"}, {"source": "API", "target": "REDIS"},
        {"source": "CHAT", "target": "STT"}, {"source": "CHAT", "target": "TTS"},
    ],
    title="Enterprise platform architecture",
)

RAG5 = diagram(
    [
        {"id": "Q", "label": "User question", "kind": "external"},
        {"id": "R", "label": "Retrieve chunks", "kind": "service"},
        {"id": "E", "label": "Enough evidence?", "kind": "service"},
        {"id": "A", "label": "Answer", "kind": "service"},
        {"id": "W", "label": "Web search", "kind": "external"},
    ],
    [
        {"source": "Q", "target": "R"}, {"source": "R", "target": "E"},
        {"source": "E", "target": "A", "label": "yes"}, {"source": "E", "target": "W", "label": "no"},
        {"source": "W", "target": "R", "style": "dashed", "label": "retry"},
    ],
    direction="LR", title="Retrieval loop",
)

SELF_LOOP = diagram(
    [{"id": "A", "label": "Poller", "kind": "service"}, {"id": "B", "label": "Queue", "kind": "store"}],
    [{"source": "A", "target": "A", "label": "tick"}, {"source": "A", "target": "B"}],
)

TWO_CYCLE = diagram(
    [{"id": "A", "label": "Planner", "kind": "service"}, {"id": "B", "label": "Critic", "kind": "model"}],
    [{"source": "A", "target": "B"}, {"source": "B", "target": "A", "label": "revise"}],
)

LONG_LOOP = diagram(
    [
        {"id": "A", "label": "Intake", "kind": "external"}, {"id": "B", "label": "Normalise", "kind": "service"},
        {"id": "C", "label": "Enrich", "kind": "service"}, {"id": "E", "label": "Score", "kind": "model"},
        {"id": "F", "label": "Publish", "kind": "store"},
    ],
    [
        {"source": "A", "target": "B"}, {"source": "B", "target": "C"}, {"source": "C", "target": "E"},
        {"source": "E", "target": "F"}, {"source": "F", "target": "B", "label": "rework"},
        {"source": "A", "target": "E", "label": "fast path"},
    ],
)

DISCONNECTED = diagram(
    [
        {"id": "A", "label": "Alpha", "kind": "service"}, {"id": "B", "label": "Beta", "kind": "store"},
        {"id": "C", "label": "Gamma", "kind": "model"}, {"id": "D", "label": "Delta", "kind": "external"},
    ],
    [{"source": "A", "target": "B"}, {"source": "C", "target": "D"}],
)

CORPUS = {
    "arch15": ARCH15, "rag5": RAG5, "self_loop": SELF_LOOP,
    "two_cycle": TWO_CYCLE, "long_loop": LONG_LOOP, "disconnected": DISCONNECTED,
}


# ------------------------------------------------------------- the layout --


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_every_node_is_laid_out_once_and_inside_the_figure(name):
    layout = D.layout_diagram(CORPUS[name])
    real = layout.real_nodes()
    assert len(real) == len(CORPUS[name].nodes)
    x0, y0, x1, y1 = layout.frame
    for n in real:
        assert x0 <= n.x - n.w / 2 and n.x + n.w / 2 <= x1, f"{n.nid} is off the figure horizontally"
        assert y0 <= n.y - n.h / 2 and n.y + n.h / 2 <= y1, f"{n.nid} is off the figure vertically"


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_no_two_boxes_overlap(name):
    """The bug feedback-arc removal exists to stop: a cyclic graph layered as
    if the cycle were one node, and four of five boxes drawn on top of each
    other with nothing raising."""
    layout = D.layout_diagram(CORPUS[name])
    real = layout.real_nodes()
    for i, a in enumerate(real):
        for b in real[i + 1:]:
            gap_x = abs(a.x - b.x) - (a.w + b.w) / 2
            gap_y = abs(a.y - b.y) - (a.h + b.h) / 2
            assert gap_x > -1e-6 or gap_y > -1e-6, f"{a.nid} and {b.nid} overlap"


def test_a_cycle_is_broken_and_the_back_edge_is_marked():
    layout = D.layout_diagram(TWO_CYCLE)
    assert layout.reversed_edges == 1
    assert sum(1 for e in layout.edges if e.reversed_) == 1
    # Both nodes still get their own layer: the cycle did not collapse.
    assert len({n.layer for n in layout.real_nodes()}) == 2


def test_a_self_loop_does_not_take_a_layer():
    layout = D.layout_diagram(SELF_LOOP)
    assert [e.self_loop for e in layout.edges].count(True) == 1
    assert len(layout.layers) == 2       # A above B, and nothing for the loop


def test_a_loop_spanning_four_layers_keeps_its_order():
    layout = D.layout_diagram(LONG_LOOP)
    by_id = {n.nid: n for n in layout.real_nodes()}
    assert by_id["A"].layer < by_id["B"].layer < by_id["C"].layer < by_id["E"].layer < by_id["F"].layer
    assert layout.reversed_edges == 1
    # An edge that spans more than one layer is routed through dummies, not
    # drawn straight across the boxes between it.
    spanning = [e for e in layout.edges if e.via]
    assert spanning, "the fast path and the rework edge both span layers"


def test_a_disconnected_graph_keeps_both_components():
    layout = D.layout_diagram(DISCONNECTED)
    by_id = {n.nid: n for n in layout.real_nodes()}
    assert by_id["A"].layer == by_id["C"].layer == 0
    assert by_id["B"].layer == by_id["D"].layer == 1


def test_the_architecture_graph_wraps_a_wide_layer_instead_of_shrinking():
    """Five nodes side by side are 8.7 in across, which a 6.3 in page can
    only answer by scaling the picture down. The wide layer becomes two
    layers instead, so the labels stay at full size."""
    layout = D.layout_diagram(ARCH15)
    assert layout.direction == "TD"
    assert len(layout.layers) > 8, "the five-wide layer was split"
    assert layout.fig_in[0] <= D.PORTRAIT_BOX_IN[0] + 0.05


def test_the_declared_direction_is_honoured_when_it_fits():
    assert D.layout_diagram(RAG5).direction == "LR"
    assert D.layout_diagram(LONG_LOOP).direction == "TD"


# ----------------------------------------------------------- the 8 pt floor --


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_a_label_reaches_the_page_at_eight_points_or_more(name):
    """The owner's complaint, as a number. Before this module the 15-node
    graph laid out at 9.64 x 12.54 in — 0.654 of the portrait width, and a
    9.5 pt label on the page at 6.2 pt — and the five-node loop at 5.4 pt."""
    layout = D.layout_diagram(CORPUS[name], box_in=D.PORTRAIT_BOX_IN)
    effective = D.FONT_PT * min(1.0, D.PORTRAIT_BOX_IN[0] / layout.fig_in[0],
                                D.PORTRAIT_BOX_IN[1] / layout.fig_in[1])
    assert effective == pytest.approx(layout.effective_pt)
    assert layout.fits, f"{name} lays out at {layout.effective_pt:.1f} pt"
    assert layout.effective_pt >= 8.0


def test_a_graph_that_cannot_fit_says_so_rather_than_shrinking_silently():
    """Honesty, not a guess: a diagram the page genuinely cannot hold comes
    back with fits=False so the renderer can warn instead of printing a 7 pt
    label as if nothing were wrong."""
    layout = D.layout_diagram(ARCH15, box_in=(3.0, 3.0))
    assert not layout.fits
    assert layout.effective_pt < 8.0


# ---------------------------------------------------------------- colour --


def _rgb(hex_colour: str):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _oklab(hex_colour: str):
    """OKLab, the space the dataviz validator measures ΔE in."""
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(v / 255.0) for v in _rgb(hex_colour))
    long_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    med = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    short = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * long_ + 0.7936177850 * med - 0.0040720468 * short,
            1.9779984951 * long_ - 2.4285922050 * med + 0.4505937099 * short,
            0.0259040371 * long_ + 0.7827717662 * med - 0.8086757660 * short)


def _delta_e(a: str, b: str) -> float:
    pa, pb = _oklab(a), _oklab(b)
    return 100.0 * sum((pa[i] - pb[i]) ** 2 for i in range(3)) ** 0.5


def test_the_role_vocabulary_is_the_shared_one():
    """Vocabulary drift fails HERE. The schema, the renderer and the parity
    harness all name the same four roles; the prompt teaches the same four."""
    assert D.DIAGRAM_ROLES == ("service", "store", "model", "external")
    assert S.DIAGRAM_ROLES == D.DIAGRAM_ROLES
    assert tuple(D.ROLE_COLOURS) == D.DIAGRAM_ROLES

    from app.engines import DIAGRAM_INSTRUCTION

    assert ", ".join(D.DIAGRAM_ROLES[:-1]) + " or " + D.DIAGRAM_ROLES[-1] in DIAGRAM_INSTRUCTION


def test_the_four_role_colours_separate_under_all_pairs():
    """The governing check for a diagram is ALL pairs, not adjacent pairs:
    the layout, not the palette order, decides which nodes end up touching.
    The numbers are the dataviz validator's, re-derived here so the check
    runs without node: worst normal-vision ΔE 15.1 (floor 15), worst CVD ΔE
    7.1 (floor 6, legal only with a secondary encoding — every node carries
    its own label and a legend names every role)."""
    slots = [D.ROLE_COLOURS[r] for r in D.DIAGRAM_ROLES]
    worst = min(_delta_e(a, b) for i, a in enumerate(slots) for b in slots[i + 1:])
    assert worst >= 15.0, f"worst all-pairs normal-vision ΔE is {worst:.1f}"
    assert len(set(slots)) == 4


def test_an_unknown_role_folds_to_neutral_and_the_palette_never_cycles():
    assert D.role_colour("service") == "#2F6FB2"
    assert D.role_colour("nonsense") == D.NEUTRAL
    assert D.role_colour("") == D.NEUTRAL
    assert D.NEUTRAL not in D.ROLE_COLOURS.values()


def test_colour_follows_the_role_and_not_the_layer():
    """The version that coloured by layer was built and looked at: on an
    eight-layer graph, eight of fifteen nodes came out neutral grey. Here
    two nodes on DIFFERENT layers with the same role get the same colour,
    and two on the SAME layer with different roles do not."""
    layout = D.layout_diagram(ARCH15)
    by_id = {n.nid: n for n in layout.real_nodes()}
    assert by_id["PG"].layer == by_id["REDIS"].layer
    assert by_id["VDB"].layer != by_id["PG"].layer
    assert D.role_colour(by_id["VDB"].role) == D.role_colour(by_id["PG"].role)
    assert D.role_colour(by_id["PG"].role) != D.role_colour(by_id["AUTH"].role)
    assert not any(n.role == "" for n in layout.real_nodes()), "no node fell to neutral"


def test_four_distinct_fills_are_in_the_rendered_pixels(tmp_path):
    """The claim proved on the OUTPUT, not on the constants. The Markdown
    scorer counts roles and cannot see colour at all, so this is the only
    place the colour claim is checked against what is drawn."""
    from PIL import Image

    D.render_diagram_png(ARCH15, tmp_path / "a.png")
    with Image.open(tmp_path / "a.png") as im:
        counts = im.convert("RGB").getcolors(maxcolors=1 << 20)
    present = {colour for _n, colour in counts}
    for role in D.DIAGRAM_ROLES:
        fill = _rgb(D.role_fill(role))
        border = _rgb(D.role_colour(role))
        assert fill in present, f"the {role} fill is not in the picture"
        assert border in present, f"the {role} border is not in the picture"
    fills = {D.role_fill(r) for r in D.DIAGRAM_ROLES}
    assert len(fills) == 4, "two roles share a fill"


def test_there_is_no_dark_palette():
    """Paper only, on purpose: a DOCX and a PDF print on white and no dark
    set has been validated for this. A dark diagram palette arriving without
    a validator run should fail here first."""
    source = Path(D.__file__).read_text(encoding="utf-8")
    assert "PAPER ONLY. There is no dark palette here." in source
    assert D.PAPER == "#FFFFFF"
    # No dark surface, ink or palette constant exists to be switched on.
    assert not re.search(r"^\s*DARK[A-Z_]*\s*[:=]", source, re.MULTILINE)
    assert "dark=" not in source


# ------------------------------------------------------------- the mermaid --


def test_a_mermaid_fence_reads_into_the_typed_shape():
    fields = D.parse_mermaid(
        'flowchart LR\n'
        '  A["User"]:::external --> B["API"]:::service\n'
        '  B --> C["Postgres"]:::store\n'
        '  B -.->|"on miss"| D["Cache"]:::store\n'
    )
    assert fields is not None
    d = S.Diagram(**fields)
    assert d.direction == "LR"
    assert [n.id for n in d.nodes] == ["A", "B", "C", "D"]
    assert [n.kind for n in d.nodes] == ["external", "service", "store", "store"]
    assert d.edges[2].style == "dashed" and d.edges[2].label == "on miss"


@pytest.mark.parametrize("source", [
    'flowchart TD\n  A["a"] --> B["b"]\n  classDef x fill:#f00\n',
    'flowchart TD\n  A["a"] --> B["b"]\n  style A fill:#ff0000\n',
    '%%{init: {"theme":"dark"}}%%\nflowchart TD\n  A["a"] --> B["b"]\n',
    'flowchart TD\n  A["a"] --> B["b"]\n  click A "https://example.com"\n',
    'flowchart TD\n  subgraph one\n    A["a"] --> B["b"]\n  end\n',
    'flowchart TD\n  A["<img src=x onerror=alert(1)>"] --> B["b"]\n  linkStyle 0 stroke:#f00\n',
])
def test_a_source_with_anything_outside_the_grammar_is_refused_whole(source):
    """A partly understood graph is never drawn. The refusal is what keeps
    md_import's callout fallback, and it is also what keeps a style or init
    directive from reaching the renderer at all."""
    assert D.parse_mermaid(source) is None


def test_an_unknown_role_name_in_a_fence_falls_to_the_default_not_to_a_colour():
    fields = D.parse_mermaid('flowchart TD\n  A["a"]:::crimson --> B["b"]\n')
    assert fields is not None
    assert [n["kind"] for n in fields["nodes"]] == ["service", "service"]


def test_a_fence_with_one_node_or_no_edges_is_not_a_diagram():
    assert D.parse_mermaid('flowchart TD\n  A["only"]\n') is None
    assert D.parse_mermaid("") is None


# --------------------------------------------------------------- the files --


def _doc(*blocks, **over) -> S.ArtifactSpec:
    body = S.DocumentSpec(title="Overview", template_id="technical_report", blocks=list(blocks), **over)
    return S.ArtifactSpec(kind="document", document=body)


def test_a_document_with_a_diagram_renders_one_media_part_and_one_pdf_image(tmp_path):
    """What the owner actually received: a PDF with ZERO image objects."""
    pytest.importorskip("docx")
    pytest.importorskip("weasyprint")
    import pypdfium2 as pdfium
    from app.artifacts.render import render_version

    spec = _doc(
        S.Heading(level=1, text="Architecture"),
        S.DiagramBlock(diagram=ARCH15),
        S.Paragraph(text="The request path is above."),
    )
    report = render_version(spec, ["pdf", "docx"], tmp_path, title_slug="doc", version=1, effort="think")
    assert report.chart_files == ["diagram-1.png"]
    assert (tmp_path / "diagram-1.png").is_file()

    docx_path = next(Path(v) for k, v in report.paths.items() if k.endswith(":docx:"))
    with zipfile.ZipFile(docx_path) as z:
        media = [n for n in z.namelist() if n.startswith("word/media/")]
    assert len(media) == 1, media

    pdf_path = next(Path(v) for k, v in report.paths.items() if k.endswith(":pdf:"))
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        images = sum(1 for i in range(len(doc)) for obj in doc[i].get_objects() if obj.type == 3)
    finally:
        doc.close()
    assert images >= 1


def test_the_docx_places_the_picture_inside_the_page(tmp_path):
    """docx.py used to pin every picture to the content width, which scales
    a tall figure down and takes its labels with it."""
    pytest.importorskip("docx")
    from app.artifacts.render import render_version

    spec = _doc(S.Heading(level=1, text="A"), S.DiagramBlock(diagram=ARCH15))
    report = render_version(spec, ["docx"], tmp_path, title_slug="doc", version=1, effort="think")
    docx_path = next(Path(v) for k, v in report.paths.items() if k.endswith(":docx:"))
    with zipfile.ZipFile(docx_path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    extents = [(int(a) / 914400, int(b) / 914400) for a, b in re.findall(r'<wp:extent cx="(\d+)" cy="(\d+)"', xml)]
    assert len(extents) == 1
    w_in, h_in = extents[0]
    assert w_in <= D.PORTRAIT_BOX_IN[0] + 0.01
    assert h_in <= D.PORTRAIT_BOX_IN[1] + 0.01


def test_a_landscape_document_lays_the_diagram_out_for_the_landscape_box(tmp_path):
    """Landscape is a different box in BOTH directions — 9.7 x 5.6 in rather
    than 6.3 x 8.4 — and both the layout and the DOCX placement have to use
    the same one, or a figure laid out for a portrait page is stretched or
    cropped on a landscape run."""
    pytest.importorskip("docx")
    from app.artifacts.render import render_version

    body = S.DocumentSpec(title="L", orientation="landscape", template_id="technical_report",
                          blocks=[S.Heading(level=1, text="A"), S.DiagramBlock(diagram=RAG5)])
    spec = S.ArtifactSpec(kind="document", document=body)
    report = render_version(spec, ["docx"], tmp_path, title_slug="l", version=1, effort="think")
    assert report.chart_files == ["diagram-1.png"]
    w_in, h_in = D.png_size_in(tmp_path / "diagram-1.png")
    assert w_in <= D.LANDSCAPE_BOX_IN[0] + 0.01 and h_in <= D.LANDSCAPE_BOX_IN[1] + 0.01
    docx_path = next(Path(v) for k, v in report.paths.items() if k.endswith(":docx:"))
    with zipfile.ZipFile(docx_path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    placed = [(int(a) / 914400, int(b) / 914400) for a, b in re.findall(r'<wp:extent cx="(\d+)" cy="(\d+)"', xml)]
    assert len(placed) == 1
    assert placed[0][0] <= D.LANDSCAPE_BOX_IN[0] + 0.01 and placed[0][1] <= D.LANDSCAPE_BOX_IN[1] + 0.01


def test_a_diagram_that_does_not_fit_produces_a_warning_not_a_silent_shrink(tmp_path, monkeypatch):
    from app.artifacts.render import render_version

    monkeypatch.setattr(D, "PORTRAIT_BOX_IN", (2.5, 2.5))
    spec = _doc(S.Heading(level=1, text="A"), S.DiagramBlock(diagram=ARCH15))
    report = render_version(spec, ["docx"], tmp_path, title_slug="doc", version=1, effort="think")
    assert any("readable size" in w for w in report.warnings), report.warnings


def test_a_markdown_answer_with_a_mermaid_fence_exports_to_a_pdf_with_a_picture(tmp_path):
    """The whole seam, end to end and with ZERO model calls: the Markdown a
    chat answer is made of goes through md_import into a DocumentSpec, and
    the document that comes out has the drawn diagram and the code block —
    where the owner's file had a callout reading "Diagram omitted" and no
    image at all."""
    pytest.importorskip("weasyprint")
    import pypdfium2 as pdfium
    from app.artifacts import md_import
    from app.artifacts.render import render_version

    md = (
        "# Platform\n\n## Architecture\n\nThe request path.\n\n"
        "```mermaid\nflowchart TD\n"
        '  U["300 enterprise users"]:::external --> FE["Next.js frontend"]:::service\n'
        '  FE --> API["FastAPI backend"]:::service\n'
        '  API --> PG["PostgreSQL"]:::store\n'
        '  API --> V["vLLM Qwen cluster"]:::model\n'
        "```\n\nIt shows where a request goes.\n\n"
        "## Monitoring\n\n```bash\ncurl -s http://localhost:8000/metrics\n```\n"
    )
    doc, notes = md_import.markdown_to_document(md)
    kinds = [b.type for b in doc.blocks]
    assert "diagram" in kinds and "code" in kinds
    assert not any("omitted" in n.lower() for n in notes)

    report = render_version(S.ArtifactSpec(kind="document", document=doc), ["pdf"], tmp_path,
                            title_slug="rt", version=1, effort="think")
    assert report.chart_files == ["diagram-1.png"]
    pdf_path = next(Path(v) for k, v in report.paths.items() if k.endswith(":pdf:"))
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        images = sum(1 for i in range(len(pdf)) for obj in pdf[i].get_objects() if obj.type == 3)
    finally:
        pdf.close()
    assert images >= 1


def test_the_png_filename_is_minted_from_an_integer():
    """pdf.py's fetcher serves a bare name from the assets directory and
    needs no change — but only while no model string reaches a filename."""
    assert H.diagram_filename(3) == "diagram-3.png"
    assert H.diagram_filename(True) == "diagram-1.png"
    with pytest.raises((TypeError, ValueError)):
        H.diagram_filename("../../etc/passwd")  # type: ignore[arg-type]


def test_spec_diagrams_walks_the_document_in_order():
    spec = _doc(S.DiagramBlock(diagram=ARCH15), S.Paragraph(text="x"), S.DiagramBlock(diagram=RAG5))
    assert [d.title for d in H.spec_diagrams(spec)] == ["Enterprise platform architecture", "Retrieval loop"]
    deck = S.ArtifactSpec(kind="presentation", presentation=S.PresentationSpec(title="D", slides=[S.Slide(title="one")]))
    assert H.spec_diagrams(deck) == []


# ------------------------------------------------------------- the escaping --

#: A fence as it would arrive inside an uploaded Markdown file: the text is
#: lifted verbatim, and the info string reaches a class attribute.
NASTY_TEXT = '</pre><script>alert(1)</script>'
NASTY_LANG = 'x" onload="'


def test_a_code_block_cannot_escape_the_html_or_the_pdf(tmp_path):
    pytest.importorskip("weasyprint")
    from app.artifacts.render import render_version

    block = S.Code(language=NASTY_LANG, text=NASTY_TEXT, caption='</figcaption><script>x</script>')
    assert block.language == "", "an info string outside the allowlist is dropped, not escaped and kept"
    spec = _doc(S.Heading(level=1, text="A"), block)
    page = H.document_html(spec.body)
    assert "<script" not in page
    assert "onload=" not in page
    assert "&lt;script&gt;" in page
    assert 'class="lang-' not in page

    report = render_version(spec, ["pdf"], tmp_path, title_slug="doc", version=1, effort="think")
    pdf_path = next(Path(v) for k, v in report.paths.items() if k.endswith(":pdf:"))
    raw = pdf_path.read_bytes()
    assert b"<script" not in raw


def test_a_legal_language_becomes_a_class_and_nothing_more():
    spec = _doc(S.Code(language="python", text="print(1)"))
    page = H.document_html(spec.body)
    assert 'class="lang-python"' in page
    assert "print(1)" in page


def test_a_code_block_reaches_the_docx_as_text(tmp_path):
    pytest.importorskip("docx")
    from app.artifacts.render import render_version

    spec = _doc(S.Heading(level=1, text="A"), S.Code(language="", text=NASTY_TEXT))
    report = render_version(spec, ["docx"], tmp_path, title_slug="doc", version=1, effort="think")
    docx_path = next(Path(v) for k, v in report.paths.items() if k.endswith(":docx:"))
    with zipfile.ZipFile(docx_path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert "&lt;/pre&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in xml
    assert "<script>" not in xml


def test_code_keeps_its_line_breaks_and_indentation_in_the_docx(tmp_path):
    """A code block that loses its indentation is not code. python-docx turns
    a newline into `<w:br/>` and marks a run with leading spaces
    `xml:space="preserve"`; this pins both, because a Word run that drops
    them renders as one unreadable line."""
    pytest.importorskip("docx")
    from app.artifacts.render import render_version

    body = "def f(x):\n    return x * 2\n\nif True:\n    print(f(1))"
    spec = _doc(S.Heading(level=1, text="A"), S.Code(language="python", text=body))
    report = render_version(spec, ["docx"], tmp_path, title_slug="doc", version=1, effort="think")
    docx_path = next(Path(v) for k, v in report.paths.items() if k.endswith(":docx:"))
    with zipfile.ZipFile(docx_path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert '<w:t xml:space="preserve">    return x * 2</w:t>' in xml
    assert xml.count("<w:br/>") >= 4
    assert 'w:ascii="Courier New"' in xml


def test_every_diagram_text_field_is_escaped():
    d = S.Diagram(
        title='<script>t</script>',
        caption='"><img src=x onerror=alert(1)>',
        nodes=[{"id": "A", "label": '<b>A</b>', "kind": "service"}, {"id": "B", "label": "B", "kind": "store"}],
        edges=[{"source": "A", "target": "B", "label": '"><i>'}],
    )
    spec = _doc(S.Heading(level=1, text="A"), S.DiagramBlock(diagram=d))
    page = H.document_html(spec.body)
    assert "<script>t</script>" not in page
    assert "&lt;script&gt;t&lt;/script&gt;" in page
    # The payload survives as TEXT with its angle brackets escaped, so no
    # tag and no attribute of it is ever parsed.
    assert "<img src=x" not in page and "&lt;img src=x onerror=alert(1)&gt;" in page
    assert 'alt="&lt;script&gt;t&lt;/script&gt;"' in page
    # The node and edge labels are drawn into the PNG, never into the markup.
    assert "<b>A</b>" not in page


# ------------------------------------------------------------------ cost --


def test_render_cost_is_small(tmp_path):
    """Measured in this worktree against the 180 s render timeout
    (config.artifact_render_timeout_s). The ceiling here is deliberately
    loose — it is a guard against an accidental quadratic, not a benchmark."""
    D.render_diagram_png(ARCH15, tmp_path / "warm.png")   # import and font cache
    times = []
    for i in range(3):
        t0 = time.perf_counter()
        D.render_diagram_png(ARCH15, tmp_path / f"a{i}.png")
        times.append((time.perf_counter() - t0) * 1000)
    assert min(times) < 3000, f"15 nodes took {min(times):.0f} ms"


def test_the_biggest_diagram_the_schema_allows_still_fits_and_is_cheap(tmp_path):
    """The ceiling, not the typical case: 24 nodes and 40 edges is the most
    the schema permits. It has to lay out at a readable size and it has to
    stay far away from the 180 s render timeout — the fitter tries up to
    eight configurations and each one counts crossings pairwise, so this is
    where an accidental quadratic would show."""
    import random

    random.seed(7)
    nodes = [{"id": f"n{i}", "label": f"Service number {i}", "kind": D.DIAGRAM_ROLES[i % 4]} for i in range(24)]
    edges, seen = [], set()
    while len(edges) < 40:
        a, b = random.randrange(24), random.randrange(24)
        if a == b or (a, b) in seen:
            continue
        seen.add((a, b))
        edges.append({"source": f"n{a}", "target": f"n{b}"})
    big = S.Diagram(title="Ceiling", nodes=nodes, edges=edges)

    D.render_diagram_png(big, tmp_path / "warm.png")
    D._LAYOUT_MEMO.clear()
    t0 = time.perf_counter()
    layout = D.render_diagram_png(big, tmp_path / "big.png")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert layout.fits and layout.effective_pt >= 8.0
    assert elapsed_ms < 5000, f"the largest allowed diagram took {elapsed_ms:.0f} ms"


def test_a_label_in_another_script_picks_a_font_that_can_draw_it(tmp_path):
    """A Devanagari or Gujarati label must not come out as boxes, and must
    not crash the font lookup. Which families exist is the server's business;
    what is asserted is that the stack is script-first and ends in a family
    that is always installed."""
    d = S.Diagram(nodes=[{"id": "a", "label": "उपयोगकर्ता", "kind": "external"},
                         {"id": "b", "label": "સેવા", "kind": "service"}],
                  edges=[{"source": "a", "target": "b"}])
    families = D.fonts_for(d)
    assert families[-1] in ("Liberation Sans", "DejaVu Sans", "sans-serif")
    D.render_diagram_png(d, tmp_path / "indic.png")
    assert (tmp_path / "indic.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_two_renders_of_one_diagram_are_byte_identical(tmp_path):
    a = D.render_diagram_png(ARCH15, tmp_path / "a.png")
    b = D.render_diagram_png(ARCH15, tmp_path / "b.png")
    assert (tmp_path / "a.png").read_bytes() == (tmp_path / "b.png").read_bytes()
    assert a.fig_in == b.fig_in


def test_the_png_reports_its_own_size_in_inches(tmp_path):
    layout = D.render_diagram_png(RAG5, tmp_path / "a.png")
    w_in, h_in = D.png_size_in(tmp_path / "a.png")
    assert w_in == pytest.approx(layout.fig_in[0], abs=0.02)
    assert h_in == pytest.approx(layout.fig_in[1], abs=0.02)


def test_the_module_imports_no_layout_library():
    """networkx is not in requirements.txt and must not become a dependency
    of the render path; matplotlib already is."""
    source = Path(D.__file__).read_text(encoding="utf-8")
    for banned in ("networkx", "pydot", "pygraphviz", "graphviz", "playwright", "mermaid"):
        assert f"import {banned}" not in source
