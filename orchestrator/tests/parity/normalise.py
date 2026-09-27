"""Turn either route's output into the same Markdown so one scorer judges both.

A chat answer is already Markdown. A file is an ArtifactSpec (spec.json, or
the dict the composer returns). `spec_to_markdown` writes the Markdown the
DocumentSpec is EQUIVALENT to -- it invents nothing: a block type that has
no Markdown form (there is none today) would raise rather than be dropped,
because a scorer that silently ignores what it cannot read is a scorer that
always passes.

The point of this file is the comparison being honest. If the file route
scores badly here it is because the document HAS no subheading, not because
the reader could not find one.

TWO BLOCK SHAPES ARE FIXED HERE AHEAD OF THE SCHEMA. `code` and `diagram`
do not exist in `DocumentSpec` yet; the document-vocabulary track adds them.
Their JSON shapes and their Markdown forms are written down here first, so
that track builds to a reader that already exists and the route track can
report `code_blocks` as UNSUPPORTED rather than as a permanent fail while it
waits. Nothing in this file causes either block to be produced.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List


#: The nine block types `app.artifacts.spec.DocumentBlock` has TODAY, plus the
#: two this programme adds. `test_normaliser_reads_every_block` reads the real
#: schema at runtime and fails if anything in it is missing from this set, so
#: a block type added to the document vocabulary and not to the renderer below
#: cannot be scored as nothing.
KNOWN_BLOCKS = {
    "heading", "paragraph", "bullets", "numbered",
    "table", "chart", "callout", "kpis", "page_break",
    # Not in the schema yet. The shapes are fixed HERE so the tracks that add
    # them build to a reader that already exists, instead of shipping a block
    # the scorer renders as nothing while every answer using it is judged on
    # content the scorer never saw.
    "code", "diagram",
}

#: The diagram node vocabulary, and the single source of it.
#:
#: `app/artifacts/spec.py`'s role enum, `app/engines/__init__.py`'s
#: DIAGRAM_INSTRUCTION, `app/artifacts/render/diagrams.py` and
#: `frontend/lib/mermaidTheme.ts` each copy these four words VERBATIM. They
#: are copies because a Python package cannot be imported by TypeScript and a
#: prompt is a string; this tuple is the one they are copied from, and a
#: disagreement is a bug in whichever file drifted.
#:
#: FOUR IS THE CEILING, and it is a palette fact rather than a taste: the
#: theme has to separate these fills from each other AND from the page in
#: both light and dark mode, at the contrast the renderer guarantees. Five
#: roles cannot be separated at that contrast, so a fifth role would be a
#: colour a reader cannot tell from another colour -- which is the defect the
#: owner reported, arrived at from the other direction.
DIAGRAM_ROLES = ("service", "store", "model", "external")


def _body(spec: Dict[str, Any]) -> Dict[str, Any]:
    """The DocumentSpec inside whatever wrapper it arrived in."""
    if "spec" in spec and isinstance(spec["spec"], dict):
        # A live producer's envelope: run_live.py records the decisions, the
        # stage timings and the warnings ALONGSIDE the spec, because those are
        # half the evidence. Unwrapping it here rather than in each caller is
        # what stops the gate and the hand-scoring CLI from being two readers
        # that disagree -- they were, and the CLI raised on a producer file
        # the gate scored fine.
        return _body(spec["spec"])
    if "document" in spec:
        return spec["document"]
    if "body" in spec and isinstance(spec["body"], dict):
        return spec["body"]
    if "blocks" in spec:
        return spec
    raise ValueError(f"no document body in spec with keys {sorted(spec)}")


def spec_to_markdown(spec: Dict[str, Any]) -> str:
    """The Markdown this document spec is equivalent to.

    Heading level N -> N+1 hashes, because the spec's level 1 is a top-level
    SECTION and the document's title is the single '# '. That mapping is
    what makes a spec's 15 level-1 headings comparable to a chat answer's 15
    '## ' sections.
    """
    body = _body(spec)
    out: List[str] = []
    title = (body.get("title") or "").strip()
    if title:
        out.append(f"# {title}\n")
    subtitle = (body.get("subtitle") or "").strip()
    if subtitle:
        out.append(f"{subtitle}\n")

    for b in body.get("blocks") or []:
        kind = b.get("type")
        if kind not in KNOWN_BLOCKS:
            raise ValueError(
                f"unknown block type {kind!r}; the scorer would silently drop it"
            )
        if kind == "heading":
            level = int(b.get("level", 1)) + 1
            out.append(f"{'#' * min(level, 6)} {b.get('text','').strip()}\n")
        elif kind == "paragraph":
            out.append(f"{b.get('text','').strip()}\n")
        elif kind == "bullets":
            for it in b.get("items") or []:
                out.append(f"- {str(it).strip()}")
            out.append("")
        elif kind == "numbered":
            for i, it in enumerate(b.get("items") or [], 1):
                out.append(f"{i}. {str(it).strip()}")
            out.append("")
        elif kind == "table":
            # A TableBlock nests its payload under "table"
            # ({"type":"table","table":{"columns":[...],"rows":[...]}});
            # every other block keeps its fields at the top level. Reading
            # b["columns"] here scored six real tables as zero on the first
            # pass, so the nesting is handled explicitly and an EMPTY table
            # is reported rather than silently skipped.
            t = b.get("table") if isinstance(b.get("table"), dict) else b
            cols = [str(c.get("header", c) if isinstance(c, dict) else c)
                    for c in (t.get("columns") or [])]
            if not cols and not (t.get("rows") or []):
                out.append("[EMPTY TABLE BLOCK]\n")
                continue
            if cols:
                out.append("| " + " | ".join(cols) + " |")
                out.append("|" + "|".join([" --- "] * len(cols)) + "|")
            for row in t.get("rows") or []:
                cells = row if isinstance(row, list) else list(row.values())
                out.append("| " + " | ".join(str(c) for c in cells) + " |")
            cap = (t.get("caption") or "").strip()
            if cap:
                out.append(f"\n{cap}")
            out.append("")
        elif kind == "callout":
            k = (b.get("kind") or "note").strip()
            t = (b.get("title") or "").strip()
            # NOT bold: a Callout has no inline markup in the spec, and the
            # normaliser must never invent formatting the document lacks --
            # that would credit the file route with the "bold text" the
            # request asked for and the schema cannot express.
            head = f"{k.upper()}{': ' + t if t else ''}"
            out.append(f"> {head}\n>\n> {b.get('text','').strip()}\n")
        elif kind == "kpis":
            items = b.get("items") or b.get("kpis") or []
            for it in items:
                if isinstance(it, dict):
                    out.append(f"- {it.get('label','')}: {it.get('value','')}")
                else:
                    out.append(f"- {it}")
            out.append("")
        elif kind == "code":
            # {"type":"code","language":str,"text":str,"caption":str}
            lang = (b.get("language") or "").strip()
            out.append(f"```{lang}")
            out.append(str(b.get("text") or "").rstrip("\n"))
            out.append("```")
            cap = (b.get("caption") or "").strip()
            if cap:
                out.append(f"\n{cap}")
            out.append("")
        elif kind == "diagram":
            # A diagram block NESTS its payload under "diagram", exactly as a
            # TableBlock nests under "table" and for the same reason -- and
            # `test_empty_table_is_not_counted_as_a_table` covers both, because
            # reading the payload off the top level is the bug that scored six
            # real tables as zero on this eval's first pass.
            d = b.get("diagram") if isinstance(b.get("diagram"), dict) else b
            nodes = d.get("nodes") or []
            edges = d.get("edges") or []
            if not nodes and not edges:
                out.append("[EMPTY DIAGRAM BLOCK]\n")
                continue
            direction = str(d.get("direction") or "TD").strip().upper()
            if direction not in ("TD", "LR"):
                direction = "TD"
            out.append("```mermaid")
            out.append(f"flowchart {direction}")
            for nd in nodes:
                nid = str(nd.get("id") or "").strip()
                label = str(nd.get("label") or "").replace('"', "'")
                role = str(nd.get("kind") or "").strip()
                # An unknown role is rendered WITHOUT a class rather than with
                # a made-up one: the scorer must see the vocabulary the answer
                # actually used, not the vocabulary it was supposed to use.
                suffix = f":::{role}" if role in DIAGRAM_ROLES else ""
                out.append(f'    {nid}["{label}"]{suffix}')
            for ed in edges:
                src = str(ed.get("source") or "").strip()
                dst = str(ed.get("target") or "").strip()
                label = str(ed.get("label") or "").strip().replace('"', "'")
                arrow = "-.->" if str(ed.get("style") or "") == "dashed" else "-->"
                if label:
                    out.append(f'    {src} {arrow}|"{label}"| {dst}')
                else:
                    out.append(f"    {src} {arrow} {dst}")
            out.append("```")
            cap = (d.get("caption") or "").strip()
            if cap:
                out.append(f"\n{cap}")
            out.append("")
        elif kind == "chart":
            # A chart is a figure, not prose. It is represented so the
            # scorer can SEE it, and deliberately not as a code fence --
            # a chart is not a code block and must not satisfy that check.
            out.append(f"[chart: {b.get('title') or b.get('chart_type') or 'chart'}]\n")
        elif kind == "page_break":
            out.append("")
    return "\n".join(out).strip() + "\n"


def load(path) -> str:
    """Markdown for a recording, whatever shape it was recorded in.

    THE SINGLE READER. Both test files and the hand-scoring CLI go through
    here, so a recording can never be scored one way by the gate and another
    way by the person arguing with the gate.
    """
    path = str(path)
    raw = open(path, encoding="utf-8").read()
    if path.endswith((".md", ".txt")):
        return raw
    return spec_to_markdown(json.loads(raw))
