"""The composer: from a request and its material to a validated ArtifactSpec.

This is the only part of the Artifact Studio that talks to the model, and it
talks to it in one shape: a strict JSON schema (`spec.schema_for(kind)`),
`llm.json_completion`, then `spec.parse_body` — the same validated-JSON
pattern the Salesforce planner uses (core/sf_intel/planner.py). The model
writes CONTENT; it never sees a filename, an id, a URL, or a byte of a file.

WHAT EFFORT BUYS (types.EFFORT_BUDGETS is the only place the numbers live):

    fast   one call: material → spec. Thinking off. One repair if the JSON
           does not validate; one correction if it holds placeholders, or
           if an edit that asked for less came back with more.
    think  an OUTLINE call first (structure, audience, what each section is
           for), then the spec, then a CONTENT REVIEW (does it cover what was
           asked, are the numbers consistent with the material, is anything
           unsupported) with up to two corrections.
    max    the same, with more sources and a VISUAL review after rendering
           (`visual_review` — the pipeline calls it with page images and
           feeds the findings back through `revise`).

GROUNDING. Everything the spec may assert comes from `Material`: the
conversation, the previous answer, uploaded text, Salesforce results
(columns and rows — never re-summed by the model; totals are formulas the
renderer writes), and web sources. Sources are given to the model with
short ids and the spec must cite them by id; `spec.parse_body` refuses a
citation to an id that is not in the manifest, which is how a fabricated
reference is caught before it is rendered.

FOLLOW-UPS. An edit gets the PARENT spec as JSON plus the instruction and
returns a whole new spec — the renderers then produce a new version. A
conversion needs no model call at all: the stored spec is rendered again in
the new format.

ROWS THE MODEL NEVER TYPES (CONTRACT-2 §4, §6; 2026-09-12). A pasted table
reaches the composer as a material `DataTable` and a sheet built from it
says `rows_from: "<id>"` with `rows: []`; a dataset that does not exist yet
is a `generator` recipe with `rows: []`. `_fill_code_made_rows` copies or
generates the rows BEFORE `spec.parse_body`, so the same validated Sheet
carries them — a 500-row dataset costs the model a schema, not 25,000
tokens, and a 34-row audit table keeps every blank cell. A `rewrite` column
(an audit comment made concise) is the ONE place the model touches a row,
and it does so cell by cell in batches with per-row ids, one in, one out,
through `tables.apply_rewrites`, which keeps the original wherever a
timestamp, a quoted phrase or a figure would change. Coverage is checked
too: the sections a request named ("include A, B and C") are matched to the
draft's headings, and a missing one costs one correction at every effort.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from pydantic import ValidationError

from .. import llm
from ..core.sf_intel.planner import extract_json_object
from . import spec as S
from . import tables
from . import types as T

log = logging.getLogger(__name__)

Progress = Callable[[Optional[float], str], Awaitable[None]]


class ComposeError(RuntimeError):
    """The model could not produce a valid spec within the budget."""

    def __init__(self, category: str, message: str) -> None:
        self.category = category
        super().__init__(message)


# ---------------------------------------------------------------- material --


@dataclass
class Source:
    """A source the model may cite. `text` is what it reads; `id` is what
    the spec cites; the rest is what the sources section prints."""

    id: str
    title: str
    text: str
    url: str = ""
    retrieved_at: str = ""
    kind: str = "web"      # web | upload | salesforce | conversation


@dataclass
class DataTable:
    """A table of real numbers — a Salesforce query result, a CSV upload —
    that the model may place and describe but never recompute."""

    id: str
    title: str
    columns: List[str]
    rows: List[List[Any]]
    source_id: str = ""


@dataclass
class Material:
    """What the composer may use. Built by the chat engine, which knows the
    conversation, the uploads, the mode and what research was allowed."""

    #: The turn's own words.
    instruction: str
    #: The conversation so far, already clipped to a budget, newest last.
    history_text: str = ""
    #: The last assistant answer (for "export the previous answer").
    previous_answer: str = ""
    #: Uploaded documents' text, already extracted and clipped.
    uploads_text: str = ""
    sources: List[Source] = field(default_factory=list)
    tables: List[DataTable] = field(default_factory=list)
    #: Things the engine decided the model should know: audience it inferred,
    #: the mode, that Salesforce data is or is not available, today's date.
    notes: List[str] = field(default_factory=list)
    #: What code did to a pasted table before it became a DataTable
    #: (CONTRACT-2 §6: rows, blanks, forward_filled, the filled column) —
    #: the engine's parse report, carried so the completion sentence and the
    #: files' methodology note can say it.
    transform: Dict[str, Any] = field(default_factory=dict)
    #: "500 rows" — the count the person asked for; a generator that says
    #: another number is set to this one (the model forgets).
    row_count: Optional[int] = None


@dataclass
class ComposeRequest:
    kind: str
    formats: List[str]
    template_id: str
    effort: str
    operation: str = "create"          # create | edit | export
    material: Optional[Material] = None
    parent_spec: Optional[S.ArtifactSpec] = None
    #: "make slide 4 shorter" — kept verbatim for an edit.
    instruction: str = ""
    author: str = ""
    date: str = ""


@dataclass
class ComposeResult:
    spec: S.ArtifactSpec
    warnings: List[str] = field(default_factory=list)
    corrections: int = 0
    outline: Optional[dict] = None
    review: Optional[dict] = None
    model_calls: int = 0
    #: CONTRACT-2 §6/§7: {rows, blanks, forward_filled, rewritten,
    #: kept_original, generated, …} — what code did to the rows, for the
    #: sentence. Empty for a document, a deck, or a workbook the model typed.
    transform: Dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------- prompts --

_ROLE = (
    "You are TechSara's document writer. You write the CONTENT of a business "
    "deliverable as JSON that matches the schema exactly. You never write "
    "HTML, Markdown, formulas, file names or links. Every fact comes from the "
    "material you are given or from the conversation; when the material does "
    "not support a claim you leave it out or say it is an assumption. You "
    "cite a source by its id only, and only ids in the source list. You never "
    "invent statistics, names, dates or quotations. NUMBERS: use only figures "
    "that appear in the material or follow from them by arithmetic you state; "
    "when a figure is missing (a current price, a customer count, a rate) say "
    "it is not given or list it under assumptions — never supply a plausible "
    "one. You never leave placeholders such as 'lorem ipsum', '[insert …]', "
    "'TBD' or 'TODO'."
)

_KIND_GUIDE = {
    "document": (
        "Write a document: a title, then blocks in reading order. Use heading "
        "levels for structure, short paragraphs, bullet lists for enumerations, "
        "a table when the material has rows, a chart when numbers compare, a "
        "callout for a warning or key takeaway, a kpis row for a brief's "
        "headline numbers. Put an assumptions list where you had to infer. "
        "Do not repeat the same content in two blocks."
    ),
    "presentation": (
        "Write a slide deck. Use the layouts: the first slide has layout "
        "'title' (title and subtitle only); a slide whose content is a chart "
        "has layout 'chart' and its data in `chart` (no bullets); a small "
        "table is layout 'table'; headline numbers are layout 'kpis'; a "
        "topic change is layout 'section'; the last slide is layout 'closing'. "
        "Every other slide is 'bullets' with ONE idea and short bullets (under "
        "15 words each). Speaker notes carry the detail that does not fit on "
        "the slide."
    ),
    "workbook": (
        "Write a workbook: one sheet per table of data, typed columns, real "
        "rows from the material only (never invented numbers — sample data is "
        "a generator recipe, below), totals as "
        "column+function requests where `column` is the column's HEADER TEXT "
        "exactly as you wrote it in `columns` (the file writes the formula), "
        "a chart per sheet where it helps: its `categories` are the CELLS of "
        "the label column in row order (one per row, not the header), and "
        "each series is named after a numeric column and lists that column's "
        "cells in the same order. For a dashboard, name the first sheet "
        "'Dashboard' and give it the summary rows."
    ),
}

_TEMPLATE_GUIDE = {
    "executive_report": "Executive report: cover on, an executive summary first (five sentences at most), then findings, then recommendations clearly separated from findings, then appendix material.",
    "brief": "One-page executive brief: no cover, a kpis row at the top, three to five tight sections, no section longer than 120 words. It must fit one page.",
    "sop": "Standard operating procedure: purpose, scope, roles, then NUMBERED steps with a warning callout where a step can go wrong, then a checklist.",
    "technical_report": "Technical report: context, approach, findings with tables, limitations, next steps. Precise, no marketing language.",
    "research_report": "Research report: question, method, findings with citations on every claim that came from a source, discussion, sources.",
    "proposal": "Proposal: the need, the proposed approach, scope, timeline (a table), pricing or effort if given, next steps.",
    "meeting_summary": "Meeting summary: attendees if known, decisions, action items with owners as a table, open questions.",
    "generic": "A clean professional document with sensible sections.",
    "ceo": "CEO deck: few words, big numbers, one message per slide, at most 10 slides plus title and closing.",
    "training": "Training deck: learning objectives first, one concept per slide, a recap slide, exercises where useful.",
    "quarterly_review": "Quarterly review deck: headline kpis, results vs plan with charts, what changed, risks, next quarter.",
    "tracker": "Tracker workbook: one sheet with the items, status and owner columns, a totals row where numbers exist.",
    "dashboard": "Dashboard workbook: a Dashboard sheet of headline figures and charts, then the data sheets behind it.",
    "data": "Data workbook: the rows as given, typed columns, filters on.",
}

_TONE = {"fast": "Be concise and concrete.", "think": "Be thorough but never padded.", "max": "Be thorough, precise, and polished."}


def _source_block(sources: Sequence[Source], limit_chars: int) -> str:
    out: List[str] = []
    used = 0
    for s in sources:
        chunk = s.text[: max(200, limit_chars - used)] if used < limit_chars else ""
        used += len(chunk)
        head = f"[{s.id}] {s.title}" + (f" ({s.url})" if s.url else "") + (f" — retrieved {s.retrieved_at}" if s.retrieved_at else "")
        out.append(f"{head}\n{chunk}".rstrip())
        if used >= limit_chars:
            break
    return "\n\n".join(out)


def _workbook_guide(req: ComposeRequest) -> str:
    """The workbook rules the schema alone does not carry (CONTRACT-2 §4,
    §6): the material tables by id and shape, with the order to use
    `rows_from` and never retype rows; a `generator` for sample data, with
    the exact count when one was asked for; `rewrite` for a comment column;
    `style` for borders, highlights and orientation."""
    m = req.material
    lines: List[str] = []
    if m is not None and m.tables:
        lines.append(
            "MATERIAL TABLES. These rows already exist and code copies them into the file. A sheet made "
            "from one sets rows_from to the table's id and leaves rows EMPTY ([]); never retype, drop, merge "
            "or reorder rows, and a blank cell stays blank. Give the sheet the same column names in the same "
            "order (with types), and put a totals row or a chart on it only if asked."
        )
        for t in m.tables:
            lines.append(f"  TABLE {t.id}: {len(t.columns)} columns × {len(t.rows):,} rows: {', '.join(t.columns)}")
    exact = f" The person asked for exactly {m.row_count:,} rows: the generator's rows MUST be {m.row_count:,}." if m is not None and m.row_count else ""
    lines.append(
        "SAMPLE DATA. When the request asks for N sample, dummy, synthetic, test or realistic records, do NOT type "
        "rows: give the sheet a generator — rows: N, a seed, and ONE recipe per column with the SAME names in the "
        "SAME order (kinds: id with a pattern such as \"CAND-{n:04d}\"; name; email; choice with values and weights; "
        "int or float with min and max; date or datetime with start and end; text with a pool of phrases; derived "
        "from other columns) — and rows: []. Put the consistency rules in the recipes: only_when for a cell that "
        "exists only in some states (a completion time only when status is Completed), unique for keys, realistic "
        "ranges." + exact
    )
    lines.append(
        "REWRITE. When asked to humanise, clean up, tidy, professionalise or rewrite a text column (audit comments, "
        "notes, feedback), keep the rows exactly as they are and add rewrite: [{column, instruction}] to the sheet; "
        "code rewrites that column cell by cell and keeps timestamps and quoted text. Never rewrite rows yourself."
    )
    lines.append(
        "STYLE. Borders, a bold header, a header fill, a highlighted column (red, amber, green or blue), wrapped text "
        "and landscape pages go in the sheet's style; a CSV carries data only, the Excel, Word and PDF files carry the style."
    )
    return "\n".join(lines)


def _table_block(tables: Sequence[DataTable]) -> str:
    out: List[str] = []
    for t in tables:
        rows = t.rows[: T.MAX_TABLE_ROWS]
        body = "\n".join(" | ".join("" if c is None else str(c) for c in r) for r in rows[:40])
        more = f"\n… {len(t.rows) - 40} more rows (all of them are available to the file)" if len(t.rows) > 40 else ""
        out.append(f"TABLE {t.id}: {t.title}\ncolumns: {' | '.join(t.columns)}\n{body}{more}")
    return "\n\n".join(out)


def material_text(req: ComposeRequest) -> str:
    """Everything the model was given to write from, as one string — what
    a figure in the draft is looked up in."""
    m = req.material or Material(instruction=req.instruction)
    parts = [req.instruction or "", m.instruction, m.history_text, m.previous_answer, m.uploads_text, *m.notes]
    parts.extend(f"{s.title}\n{s.text}" for s in m.sources)
    parts.extend(_table_block([t]) for t in m.tables)
    if req.parent_spec is not None:
        parts.append(S.text_of(req.parent_spec))
        parts.append(req.parent_spec.body.model_dump_json())
    return "\n".join(p for p in parts if p)


def _material_messages(req: ComposeRequest, *, budget: T.EffortBudget) -> List[dict]:
    m = req.material or Material(instruction=req.instruction)
    parts: List[str] = []
    if m.notes:
        parts.append("Context:\n- " + "\n- ".join(m.notes))
    if m.history_text:
        parts.append("Conversation so far:\n" + m.history_text)
    if m.previous_answer:
        parts.append("The answer to turn into the file:\n" + m.previous_answer)
    if m.uploads_text:
        parts.append("Uploaded material:\n" + m.uploads_text)
    if m.tables:
        parts.append("Data (use these numbers as they are; do not recompute totals):\n" + _table_block(m.tables))
    if m.sources:
        parts.append("Sources you may cite, by id:\n" + _source_block(m.sources[: budget.max_sources or len(m.sources)], 40_000))
    caps = ""
    if req.kind == "presentation":
        # The renderer's per-template bullet caps, said up front: a slide
        # written over them is trimmed with a warning, which is a worse
        # deck than one written to fit (the first e2e run dropped two
        # bullets on five of six slides of a CEO deck).
        try:
            from .render.theme import PRESENTATION_TEMPLATES

            tpl = PRESENTATION_TEMPLATES.get(req.template_id) or PRESENTATION_TEMPLATES["generic"]
            caps = (f" Each slide fits at most {tpl['max_bullets']} bullets of at most "
                    f"{tpl['max_bullet_chars']} characters; write to that, never over it.")
        except Exception:  # noqa: BLE001 — the caps are advice; the renderer still fits
            caps = ""
    guide = f"\n\n{_workbook_guide(req)}" if req.kind == "workbook" else ""
    system = (
        f"{_ROLE}\n\n{_KIND_GUIDE[req.kind]}{caps}{guide}\n\n{_TEMPLATE_GUIDE.get(req.template_id, _TEMPLATE_GUIDE['generic'])}\n\n"
        f"{_TONE.get(req.effort, '')} Limits: at most {budget.max_sections} top-level sections, "
        f"{budget.max_slides} slides, {budget.max_sheets} sheets. "
        f"Set template_id to \"{req.template_id}\"."
        + (f" Author: {req.author}." if req.author else "") + (f" Date: {req.date}." if req.date else "")
    )
    user = "\n\n".join(parts + [f"Request: {req.instruction or m.instruction}"])
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ------------------------------------------------------------- the calls --


async def _json(messages: List[dict], schema: dict, name: str, *, thinking: bool, max_tokens: int, effort: Optional[str] = None) -> dict:
    # `max_tokens` is the answer's ceiling; with thinking on, json_completion
    # sizes the pool the reasoning shares (see its docstring).
    raw = await llm.json_completion(messages, json_schema=schema, schema_name=name, temperature=0.0, max_tokens=max_tokens, thinking=thinking, effort=effort)
    obj = extract_json_object(raw or "")
    if not isinstance(obj, dict):
        if llm.get_finish_reason() == "length":
            # Cut off at the budget: a runaway, or a document that really
            # was that long. Either way the person should hear the true
            # shape of it, not "not JSON".
            raise ComposeError("model_failure", "The model's answer was cut off before the document was complete; a shorter or narrower request should work.")
        raise ComposeError("model_failure", "The model did not return the document as JSON.")
    return obj


_OUTLINE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string", "maxLength": 120},
        "audience": {"type": "string", "maxLength": 120},
        "purpose": {"type": "string", "maxLength": 300},
        "sections": {
            "type": "array", "maxItems": 20,
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "heading": {"type": "string", "maxLength": 120},
                    "purpose": {"type": "string", "maxLength": 200},
                    "elements": {"type": "array", "maxItems": 6, "items": {"type": "string", "enum": ["paragraphs", "bullets", "table", "chart", "callout", "kpis", "numbered"]}},
                },
                "required": ["heading", "purpose", "elements"],
            },
        },
        "needs_current_facts": {"type": "boolean"},
        "assumptions": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 200}},
    },
    "required": ["title", "audience", "purpose", "sections", "needs_current_facts", "assumptions"],
}

_REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ok": {"type": "boolean"},
        "issues": {
            "type": "array", "maxItems": 12,
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "where": {"type": "string", "maxLength": 120},
                    "problem": {"type": "string", "maxLength": 300},
                    "fix": {"type": "string", "maxLength": 300},
                    "severity": {"type": "string", "enum": ["must", "should"]},
                },
                "required": ["where", "problem", "fix", "severity"],
            },
        },
    },
    "required": ["ok", "issues"],
}


def _max_tokens_for(kind: str, effort: str) -> int:
    base = {"document": 12_000, "presentation": 8_000, "workbook": 12_000}[kind]
    return base if effort == "fast" else int(base * 1.5)


async def outline(req: ComposeRequest, budget: T.EffortBudget) -> dict:
    messages = _material_messages(req, budget=budget)
    messages[0]["content"] += (
        "\n\nFIRST, plan only: return the outline — title, audience, purpose, "
        "the sections in order with what each is for and which elements it "
        "uses, whether the request needs current external facts you were not "
        "given, and the assumptions you will make."
    )
    return await _json(messages, _OUTLINE_SCHEMA, "artifact_outline", thinking=budget.thinking, max_tokens=2500, effort=req.effort)


async def _compose_once(req: ComposeRequest, budget: T.EffortBudget, *, outline_json: Optional[dict], extra: str = "") -> dict:
    messages = _material_messages(req, budget=budget)
    if outline_json:
        messages[0]["content"] += "\n\nFollow this outline you planned:\n" + json.dumps(outline_json, ensure_ascii=False)
    if req.operation == "edit" and req.parent_spec is not None:
        messages[0]["content"] += (
            "\n\nYou are EDITING an existing document. Here is its current content as JSON. "
            "Apply the request and return the WHOLE document again with the change applied and "
            "everything else preserved unless the request says otherwise:\n"
            + body_json_for_prompt(req.parent_spec)
        )
    if extra:
        messages.append({"role": "user", "content": extra})
    return await _json(messages, S.schema_for(req.kind), f"artifact_{req.kind}", thinking=budget.thinking, max_tokens=_max_tokens_for(req.kind, req.effort), effort=req.effort)


def body_json_for_prompt(spec: S.ArtifactSpec) -> str:
    """The body as the model re-reads it in an edit or a review: a sheet
    whose rows are code-made (rows_from / generator) travels with
    `rows: []`, exactly as the model wrote it. The filled rows are code's
    to copy or generate again after the edit (`_fill_code_made_rows`);
    shown, they were 25,000 tokens of prompt for a 500-row dataset (C6,
    discovery of 2026-09-12) AND an invitation to retype them in the
    answer — which Fast's 12,000-token ceiling then cut off. Every other
    kind, and a workbook the model typed, is the plain dump."""
    body = spec.body
    if not isinstance(body, S.WorkbookSpec) or not any(sh.rows_are_code_made for sh in body.sheets):
        return body.model_dump_json()
    dump = body.model_dump(mode="json", by_alias=True, exclude_none=True)
    for sheet, raw in zip(body.sheets, dump.get("sheets") or []):
        if sheet.rows_are_code_made and isinstance(raw, dict):
            raw["rows"] = []
    return json.dumps(dump, ensure_ascii=False)


def _enforce_caps(spec: S.ArtifactSpec, budget: T.EffortBudget, requested: Sequence[str] = ()) -> List[str]:
    """Trim what the effort level allows rather than refuse: a deck with 14
    slides at Fast becomes 12 with a warning, not an error. Two exceptions
    (CONTRACT-2 §4, §11): a sheet whose rows code copied or generated is
    held only to the hard ceiling — the effort cap exists for rows the
    model would type, and a 3,000-row pasted table at Fast is the person's
    table, not a token bill; and the section cap never falls below the
    sections the request named plus two."""
    warnings: List[str] = []
    body = spec.body
    if isinstance(body, S.PresentationSpec) and len(body.slides) > budget.max_slides:
        warnings.append(f"the deck was trimmed from {len(body.slides)} to {budget.max_slides} slides for this effort level")
        body.slides = body.slides[: budget.max_slides]
    if isinstance(body, S.WorkbookSpec):
        if len(body.sheets) > budget.max_sheets:
            warnings.append(f"the workbook was trimmed from {len(body.sheets)} to {budget.max_sheets} sheets")
            body.sheets = body.sheets[: budget.max_sheets]
        for sh in body.sheets:
            cap = T.MAX_ROWS_PER_SHEET if sh.rows_are_code_made else budget.max_rows_per_sheet
            if len(sh.rows) > cap:
                warnings.append(f"sheet {sh.name!r} was cut to {cap:,} rows")
                sh.rows = sh.rows[:cap]
    if isinstance(body, S.DocumentSpec):
        top = sum(1 for b in body.blocks if isinstance(b, S.Heading) and b.level == 1)
        cap = max(budget.max_sections, len(requested) + 2)
        if top > cap:
            warnings.append(f"the document has {top} top-level sections; this effort level asked for at most {cap}")
    return warnings


# ---------------------------------------------------------------- coverage --
#
# WHY. "Include an executive summary, risks and a roadmap" is a requirement,
# not a suggestion, and a draft that skipped two of the three passed every
# other check (the schema, the placeholders, the review at Think — and Fast
# has no review). The sections a request names are parsed here and matched
# to the draft's headings by word overlap; a missing one costs exactly one
# correction, naming the sections, at every effort (CONTRACT-2 §11).

_SECTIONS_COLON_RE = re.compile(r"\bsections?\s*:\s*(?P<list>[^.;!?\n]{3,300})", re.I)
_SECTION_LIST_RE = re.compile(
    r"\b(?P<verb>includ(?:e|es|ing)|contain(?:s|ing)?|cover(?:s|ing)?|with)\s+"
    r"(?:the\s+|these\s+)?(?:following\s+)?(?:sections?\s+(?:on|for|about)?\s*)?(?P<list>[^.;:!?\n]{3,300})",
    re.I,
)
_LIST_SPLIT_RE = re.compile(r"\s*(?:,|;|\band\b|&|\bplus\b)\s*", re.I)
_LEAD_WORDS_RE = re.compile(r"^(?:a|an|the|some|its|our|their|one|two|three|four|five|several|separate|short|brief|detailed|clear|full)\s+", re.I)
_TRAIL_WORDS_RE = re.compile(r"\s+(?:sections?|parts?|pages?|chapters?|paragraphs?)$", re.I)
_STOP_WORDS = frozenset({
    "a", "an", "the", "of", "for", "on", "in", "to", "and", "with", "section", "sections", "part", "parts", "its",
    "our", "their", "this", "that", "some", "any", "each", "every", "all", "brief", "short", "detailed", "please",
    "also", "key", "main", "current", "proposed", "clear", "full",
})
#: Words that name a file's parts or properties, never its sections.
_NOT_SECTION_WORDS = frozenset({
    "row", "rows", "column", "columns", "table", "tables", "chart", "charts", "graph", "graphs", "total", "totals",
    "file", "files", "format", "formats", "pdf", "docx", "word", "excel", "csv", "xlsx", "pptx", "powerpoint",
    "slide", "slides", "page", "pages", "logo", "logos", "colour", "colours", "color", "colors", "font", "fonts",
    "header", "headers", "footer", "footers", "number", "numbers", "data", "record", "records", "image", "images",
    "picture", "pictures", "sheet", "sheets", "tab", "tabs", "version", "versions", "copy", "copies", "link", "links",
    "date", "dates", "name", "names", "title", "titles", "bullet", "bullets", "formatting", "style", "styles",
})


def _stem(word: str) -> str:
    w = re.sub(r"[^a-z0-9]", "", word.lower())
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _content_words(text: str) -> set:
    return {_stem(w) for w in re.split(r"\s+", text.strip()) if w and _stem(w) and _stem(w) not in _STOP_WORDS}


def _section_phrase(raw: str) -> str:
    text = " ".join(raw.split()).strip(" -–—:'\"")
    text = _LEAD_WORDS_RE.sub("", text)
    text = _TRAIL_WORDS_RE.sub("", text)
    return text.strip()


def requested_sections(instruction: str) -> List[str]:
    """The sections a request names — "include A, B, C and D", "covering
    A and B", "sections: A, B" — as short phrases, in order, deduplicated.
    A phrase is a section only when it is one to five words with no digit
    and at least one content word that does not name a file part (rows,
    logo, PDF, chart …); "with" needs a list of two or more, because "with
    a total row" describes the table, not a chapter. Empty when the
    request names none."""
    text = " ".join((instruction or "").split())
    if not text:
        return []
    found: List[str] = []
    seen: set = set()

    def take(raw_list: str, minimum: int) -> None:
        phrases = [_section_phrase(x) for x in _LIST_SPLIT_RE.split(raw_list) if x and x.strip()]
        keep: List[str] = []
        for ph in phrases:
            words = ph.split()
            if not ph or not 1 <= len(words) <= 5 or any(ch.isdigit() for ch in ph):
                continue
            content = _content_words(ph)
            if not content or content <= {_stem(w) for w in _NOT_SECTION_WORDS}:
                continue
            keep.append(ph)
        if len(keep) < minimum:
            return
        for ph in keep:
            key = ph.lower()
            if key not in seen:
                seen.add(key)
                found.append(ph)

    for m in _SECTIONS_COLON_RE.finditer(text):
        take(m.group("list"), 1)
    for m in _SECTION_LIST_RE.finditer(text):
        take(m.group("list"), 2 if m.group("verb").lower() == "with" else 1)
    return found[:12]


def _missing_sections(spec: S.ArtifactSpec, requested: Sequence[str]) -> List[str]:
    """The requested phrases no heading of the document covers: a heading
    covers a phrase when at least 60% of the phrase's content words are in
    it (CONTRACT-2 §11)."""
    body = spec.body
    if not requested or not isinstance(body, S.DocumentSpec):
        return []
    headings = [_content_words(b.text) for b in body.blocks if isinstance(b, S.Heading)]
    missing: List[str] = []
    for phrase in requested:
        words = _content_words(phrase)
        if not words:
            continue
        if not any(len(words & h) / len(words) >= 0.6 for h in headings):
            missing.append(phrase)
    return missing


async def compose(req: ComposeRequest, *, progress: Optional[Progress] = None) -> ComposeResult:
    """Request + material → validated spec, within the effort's budget."""
    budget = T.EFFORT_BUDGETS.get(req.effort, T.EFFORT_BUDGETS["fast"])
    result_warnings: List[str] = []
    calls = 0
    corrections = 0

    async def say(pct: Optional[float], detail: str) -> None:
        if progress is not None:
            await progress(pct, detail)

    outline_json: Optional[dict] = None
    if budget.outline_pass and req.operation != "edit":
        await say(10.0, "outlining")
        try:
            outline_json = await outline(req, budget)
            calls += 1
        except ComposeError:
            outline_json = None  # a missing outline is a smaller loss than a missing document

    await say(30.0, "writing")
    raw = await _compose_once(req, budget, outline_json=outline_json)
    calls += 1
    spec, repaired, notes = await _validate_or_repair(req, budget, raw, outline_json)
    calls += repaired
    corrections += repaired
    result_warnings.extend(notes)

    async def correct(pct: float, detail: str, extra: str, *, allow_shrink: bool = False) -> None:
        """One correction pass: the whole document again with `extra`, then
        validated, then HELD AGAINST THE DRAFT IT CORRECTS. A correction that
        returns a fraction of the content is not applied — the Think brief
        of the 2026-09-11 e2e run came back from its review as one KPI row
        on an empty page. Shrinking is allowed only when it was asked for."""
        nonlocal spec, calls, corrections
        await say(pct, detail)
        raw = await _compose_once(req, budget, outline_json=outline_json, extra=extra)
        calls += 1
        corrections += 1
        candidate, repaired, notes = await _validate_or_repair(req, budget, raw, outline_json)
        calls += repaired
        result_warnings.extend(n for n in notes if n not in result_warnings)
        why = _worse(spec, candidate, allow_shrink=allow_shrink)
        if why:
            result_warnings.append(f"a correction {why} and was not applied; the draft before it is what you see")
            log.info("artifact compose: a correction (%s) %s; kept the draft", detail, why)
            return
        spec = candidate

    # A draft with no body — a KPI row and nothing else, a deck of one
    # slide, a workbook with no rows — is repaired once before anything
    # else is done to it.
    hollow = S.hollow(spec)
    if hollow and corrections < budget.max_corrections:
        await correct(50.0, "adding the missing body", f"Your draft is incomplete: {hollow}. Write the whole document, every section with its content.", allow_shrink=True)
        hollow = S.hollow(spec)
    if hollow:
        result_warnings.append(f"the document is thin: {hollow}")

    # Placeholders are a correction, bounded by the budget.
    holes = S.placeholders_in(spec)
    if holes and corrections < budget.max_corrections:
        await correct(55.0, "removing placeholders", f"Your draft still contains placeholder text: {', '.join(holes[:6])}. Replace every placeholder with real content from the material, or remove it. Return the whole document.")
        holes = S.placeholders_in(spec)
    if holes:
        result_warnings.append("placeholder text remains: " + ", ".join(holes[:4]))

    # "Make it shorter" that came back longer is a correction too — a
    # deterministic one every effort can afford. (The e2e run of 2026-09-11:
    # a one-page brief asked to be shorter came back as two pages.)
    longer = _asked_shorter_but_longer(req, spec)
    if longer and corrections < budget.max_corrections:
        before, after = longer
        await correct(60.0, "shortening", f"The request asked for a SHORTER document, but your draft is longer than the one being edited ({after} words against {before}). Cut it well below {before} words, keeping the change that was asked for. Return the whole document.", allow_shrink=True)
        longer = _asked_shorter_but_longer(req, spec)
    if longer:
        result_warnings.append(f"the edit asked for a shorter document but this version is longer ({longer[1]} words against {longer[0]})")

    # The sections the request named, matched to the headings: ONE
    # correction at every effort (the budget does not gate it — it is the
    # request itself), then a warning if the model still cannot.
    requested = requested_sections(req.instruction or (req.material.instruction if req.material else "")) if req.kind == "document" else []
    missing = _missing_sections(spec, requested)
    if missing:
        await correct(
            62.0, "adding the requested sections",
            f"The request asked for these sections, which your draft does not have: {', '.join(missing)}. "
            "Add each as its own headed section with real content from the material, keep everything else, "
            "and return the WHOLE document.",
        )
        missing = _missing_sections(spec, requested)
    if missing:
        result_warnings.append("requested sections not found in the document: " + ", ".join(missing))

    # Figures the material never gave. Named on the version at every effort;
    # handed to the reviewer where there is one.
    figures = S.unsupported_figures(spec, material_text(req))

    review_json: Optional[dict] = None
    if budget.content_review and corrections < budget.max_corrections:
        await say(70.0, "reviewing the content")
        try:
            review_json = await content_review(req, spec, budget, figures)
            calls += 1
        except ComposeError:
            review_json = None
        issues = (review_json or {}).get("issues") if isinstance(review_json, dict) else None
        musts = [i for i in (issues if isinstance(issues, list) else []) if isinstance(i, dict) and i.get("severity") == "must"]
        if musts:
            fix_text = "\n".join(f"- {i['where']}: {i['problem']} → {i['fix']}" for i in musts[:8])
            shrink_asked = any(re.search(r"too long|shorter|shorten|cut|trim|condense", f"{i.get('problem', '')} {i.get('fix', '')}", re.IGNORECASE) for i in musts)
            await correct(80.0, f"correcting {len(musts)} issue(s)", f"A reviewer found these problems. Fix each and return the WHOLE document with every section, not only the parts you changed:\n{fix_text}", allow_shrink=shrink_asked)
            figures = S.unsupported_figures(spec, material_text(req))

    if figures:
        result_warnings.append("figures not in the material (derived or assumed): " + ", ".join(figures[:8]) + (" …" if len(figures) > 8 else ""))
    result_warnings.extend(_enforce_caps(spec, budget, requested))

    # The rewrite columns, last — on the draft that will be rendered, once,
    # at every effort: it is the task ("humanise the comments"), not a
    # nicety, and it costs one model call per batch of rows.
    if isinstance(spec.body, S.WorkbookSpec) and any(sh.rewrite and sh.rows for sh in spec.body.sheets):
        await say(90.0, "rewriting the text column")
    rewrite_calls, rewrite_warnings, rewrite_report = await _rewrite_columns(req, spec)
    calls += rewrite_calls
    result_warnings.extend(w for w in rewrite_warnings if w not in result_warnings)
    transform = _transform_report(req, spec, rewrite_report)
    await say(95.0, "content ready")
    return ComposeResult(spec=spec, warnings=result_warnings, corrections=corrections, outline=outline_json, review=review_json, model_calls=calls, transform=transform)


def _transform_report(req: ComposeRequest, spec: S.ArtifactSpec, rewrite_report: Dict[str, Any]) -> Dict[str, Any]:
    """CONTRACT-2 §6: what code did to the rows — the engine's parse report
    (rows, blanks, forward-filled cells) first, then what the spec shows
    (rows copied by `rows_from`, rows generated), then the rewrite's
    counts. Empty when nothing was code-made."""
    out: Dict[str, Any] = dict(req.material.transform) if req.material is not None and req.material.transform else {}
    body = spec.body
    if isinstance(body, S.WorkbookSpec):
        copied = [sh for sh in body.sheets if sh.rows_from]
        if copied:
            out.setdefault("rows", sum(len(sh.rows) for sh in copied))
            out.setdefault("blanks", sum(1 for sh in copied for r in sh.rows for c in r if c is None or (isinstance(c, str) and not c.strip())))
        generated = [sh for sh in body.sheets if sh.generator is not None]
        if generated:
            out["generated"] = sum(len(sh.rows) for sh in generated)
    out.update(rewrite_report)
    return out


def _gutted(before: S.ArtifactSpec, after: S.ArtifactSpec) -> bool:
    """True when `after` keeps less than half of `before` — by words of
    prose, or by parts (blocks, slides, sheets)."""
    words_before = len(S.text_of(before).split())
    words_after = len(S.text_of(after).split())
    parts_before, parts_after = S.part_count(before), S.part_count(after)
    if words_before >= 40 and words_after < words_before * 0.5:
        return True
    return parts_before >= 3 and parts_after < parts_before * 0.5


def _worse(before: S.ArtifactSpec, after: S.ArtifactSpec, *, allow_shrink: bool) -> str:
    """Why `after` is a worse document than `before`, or "" — the reasons a
    correction is refused: it dropped most of the content (unless less was
    asked for), it introduced placeholders, or it emptied the body."""
    if not allow_shrink and _gutted(before, after):
        return "dropped most of the content"
    if S.placeholders_in(after) and not S.placeholders_in(before):
        return "replaced content with placeholders"
    if S.hollow(after) and not S.hollow(before):
        return "emptied the document"
    return ""


_SHORTER_RE = re.compile(
    r"\b(shorter|shorten|condense|condensed|trim|tighten|tighter|more concise|concise|cut (?:it |this )?down|"
    r"half (?:the|its) length|briefer|less wordy|fewer words|shrink)\b",
    re.IGNORECASE,
)


def _asked_shorter_but_longer(req: ComposeRequest, spec: S.ArtifactSpec) -> Optional[Tuple[int, int]]:
    """(parent words, new words) when an EDIT asked for less and got more;
    None otherwise. Words of the prose the person reads (`text_of`), so a
    deck and a workbook are measured the same way as a document."""
    if req.operation != "edit" or req.parent_spec is None or not _SHORTER_RE.search(req.instruction or ""):
        return None
    before = len(S.text_of(req.parent_spec).split())
    after = len(S.text_of(spec).split())
    # A retitled draft is a few words either way; a correction is for a
    # draft that grew — by more than 5% and more than five words.
    if before and after > before + max(5, before // 20):
        return before, after
    return None


def _pin_template(raw: dict, req: ComposeRequest) -> None:
    """The template was decided from the request's words before the model
    was called; the model is told which, and still changes it (the Think
    brief of 2026-09-11 came back `generic` from its correction pass and
    rendered as a report). Code decides; the model writes content."""
    if isinstance(raw, dict) and req.template_id in S.templates_for(req.kind):
        raw["template_id"] = req.template_id


def _reconcile_sources(raw: dict, material: Optional[Material]) -> List[str]:
    """The sources manifest is CODE-BUILT from the material, never taken
    from the model. The model may cite an id it was given; every entry it
    invents — a title, a URL, a date — is dropped and the citations to it
    stripped, because a fabricated reference that passes validation is
    exactly the injected 'source' a hostile upload would plant (review,
    2026-09-11). Returns the warnings to show. Mutates `raw` in place."""
    known = {s.id: s for s in (material.sources if material else [])}
    warnings: List[str] = []
    cited: set = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            refs = node.get("sources")
            if isinstance(refs, list) and node is not raw:
                kept = [r for r in refs if isinstance(r, str) and r in known]
                dropped = len(refs) - len(kept)
                if dropped:
                    warnings.append(f"{dropped} citation(s) to sources that were not provided were removed")
                node["sources"] = kept
                cited.update(kept)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    body_sources = raw.get("sources")
    raw["sources"] = None  # so the walk does not treat the manifest as block refs
    walk(raw)
    invented = [s for s in (body_sources or []) if not (isinstance(s, dict) and s.get("id") in known)]
    if invented:
        warnings.append(f"{len(invented)} source(s) the model listed were not among those provided and were removed")
    raw["sources"] = [
        {"id": s.id, "title": s.title[:300], "url": s.url or None, "retrieved_at": s.retrieved_at or None}
        for sid, s in known.items() if sid in cited
    ]
    return sorted(set(warnings))


def _fold(name: Any) -> str:
    return " ".join(str(name).split()).casefold()


def _recipe(column: S.GenColumn) -> Dict[str, Any]:
    """One column's recipe as tables.generate_rows reads it: the alias
    (`in`) and no unset field — a None `start` on an id column would be
    read as a bad id start, an empty `weights` as the wrong number of
    weights."""
    return {k: v for k, v in column.model_dump(by_alias=True).items() if v not in (None, [], "")}


def _fill_code_made_rows(raw: dict, req: ComposeRequest, notes: List[str]) -> List[str]:
    """CONTRACT-2 §4/§6, BEFORE parse_body: a sheet with `rows_from` gets
    the material table's rows verbatim — blanks included, and in place of
    any rows the model typed; a sheet with a `generator` gets exactly
    `rows` rows from tables.generate_rows, the recipes put in the sheet's
    column order first (the row cells follow the recipe order). When the
    sheet's column count differs from the table's, the table's column
    names win (a note says so); when the person asked for a count, the
    generator gets it. Mutates `raw`. Returns the problems the model must
    repair — an unknown table id, recipes that do not match the columns, a
    rule the generator refuses — each naming the sheet."""
    if req.kind != "workbook" or not isinstance(raw, dict) or not isinstance(raw.get("sheets"), list):
        return []
    material = req.material
    known = {t.id: t for t in (material.tables if material is not None else [])}
    wanted = int(material.row_count) if material is not None and material.row_count else 0
    with_generator = [sh for sh in raw["sheets"] if isinstance(sh, dict) and isinstance(sh.get("generator"), dict)]
    problems: List[str] = []
    for index, sh in enumerate(raw["sheets"]):
        if not isinstance(sh, dict):
            continue
        name = str(sh.get("name") or f"sheet {index + 1}")
        source = sh.get("rows_from")
        gen = sh.get("generator")
        if isinstance(source, str) and source.strip():
            table = known.get(source.strip())
            if table is None:
                ids = ", ".join(repr(t) for t in known) or "none"
                problems.append(f"sheets.{index}.rows_from: {source!r} names no material table; the tables are {ids}")
                continue
            columns = sh.get("columns")
            if not isinstance(columns, list) or len(columns) != len(table.columns):
                sh["columns"] = [{"name": c} for c in table.columns]
                notes.append(f"sheet {name!r}: columns taken from the pasted table")
            if sh.get("rows"):
                notes.append(f"sheet {name!r}: the rows the model typed were replaced by the pasted table's")
            sh["rows"] = _without_trailing_blank_rows([list(r) for r in table.rows], name, notes)
        elif isinstance(gen, dict):
            if wanted and len(with_generator) == 1 and gen.get("rows") != wanted:
                if gen.get("rows"):
                    notes.append(f"sheet {name!r}: the generator's {gen.get('rows')} rows were set to the {wanted:,} that were asked for")
                gen["rows"] = wanted
            try:
                model = S.Generator.model_validate(gen)
            except ValidationError as exc:
                problems.append(f"sheets.{index}.generator: {S.validation_summary(exc, limit=4).replace(chr(10), ' ')}")
                continue
            columns = sh.get("columns")
            names = [c.get("name") for c in columns if isinstance(c, dict) and isinstance(c.get("name"), str)] if isinstance(columns, list) else []
            recipes = {_fold(c.name): c for c in model.columns}
            missing = [n for n in names if _fold(n) not in recipes]
            extra = [c.name for c in model.columns if _fold(c.name) not in {_fold(n) for n in names}]
            if not names or missing or extra:
                what = []
                if missing:
                    what.append(f"no recipe for column{'s' if len(missing) > 1 else ''} {', '.join(repr(m) for m in missing)}")
                if extra:
                    what.append(f"recipe{'s' if len(extra) > 1 else ''} for {', '.join(repr(e) for e in extra)}, which the sheet has no column for")
                problems.append(f"sheets.{index}.generator: one recipe per column, same names — {'; '.join(what) or 'the sheet has no columns'}")
                continue
            data = {"rows": model.rows, "seed": model.seed, "columns": [_recipe(recipes[_fold(n)]) for n in names]}
            try:
                rows, _report = tables.generate_rows(data)
            except ValueError as exc:
                problems.append(f"sheets.{index}.generator: {exc}")
                continue
            sh["rows"] = rows
    return problems


def _without_trailing_blank_rows(rows: List[list], name: str, notes: List[str]) -> List[list]:
    """A row that is blank in every cell at the END of a table is not a row
    the XLSX can hold (openpyxl writes nothing for it, so the reopened
    sheet counts one row fewer than the spec and the CSV) — the review of
    2026-09-12 reproduced a refused render from a blank line at the end
    of an uploaded CSV. Interior blank rows stay: they are the person's."""
    kept = list(rows)
    dropped = 0
    while kept and all(c in (None, "") for c in kept[-1]):
        kept.pop()
        dropped += 1
    if dropped:
        notes.append(f"sheet {name!r}: {dropped} blank row{'s' if dropped != 1 else ''} at the end of the table {'were' if dropped != 1 else 'was'} left out")
    return kept


async def _validate_or_repair(req: ComposeRequest, budget: T.EffortBudget, raw: dict, outline_json: Optional[dict]):
    """parse_body, and on a validation error ONE repair pass with the field
    paths — the same recipe as the Salesforce planner. Returns (spec, repairs).
    The sources manifest is reconciled against the material FIRST, so the
    model's own list never reaches validation, let alone a page; then the
    code-made rows are filled (CONTRACT-2 §4), so the Sheet that validates
    is the one that renders."""
    notes = _reconcile_sources(raw, req.material)
    _pin_template(raw, req)
    problems = _fill_code_made_rows(raw, req, notes)
    if problems:
        summary = "\n".join(f"- {p}" for p in problems)
        lead = "The rows could not be filled from your sheet definitions:"
    else:
        try:
            return S.parse_body(req.kind, raw), 0, notes
        except ValidationError as exc:
            summary = S.validation_summary(exc)
            lead = "Your JSON did not match the schema:"
    log.info("artifact compose: spec invalid, repairing once: %s", summary.replace("\n", " | ")[:400])
    fixed = await _compose_once(
        req, budget, outline_json=outline_json,
        extra=f"{lead}\n{summary}\nReturn the corrected, complete document.",
    )
    notes = _reconcile_sources(fixed, req.material)
    _pin_template(fixed, req)
    problems = _fill_code_made_rows(fixed, req, notes)
    if problems:
        log.info("artifact compose: the repair's rows could not be filled either: %s", " | ".join(problems)[:400])
        raise ComposeError("model_failure", "The model could not produce a valid document structure.")
    try:
        return S.parse_body(req.kind, fixed), 1, notes
    except ValidationError as exc:
        # Field paths and rule names only — the operator's key to a repair
        # that did not take; the content never reaches the log.
        log.info("artifact compose: the repair was invalid too: %s", S.validation_summary(exc).replace("\n", " | ")[:400])
        raise ComposeError("model_failure", "The model could not produce a valid document structure.") from exc


# ---------------------------------------------------------------- rewrite --

_REWRITE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "rewrites": {
            "type": "array", "maxItems": 200,
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"row": {"type": "integer", "minimum": 0}, "text": {"type": "string", "maxLength": 2000}},
                "required": ["row", "text"],
            },
        },
    },
    "required": ["rewrites"],
}
#: Rows per rewrite call: 40 audit comments of ~100 characters is ~1,500
#: tokens in and about the same out — one call for the 34-row paste, 13
#: for a 500-row sheet.
REWRITE_BATCH_ROWS = 40


def _rewrite_tokens(batch: Sequence[Tuple[int, str]]) -> int:
    """The answer's ceiling for one batch: the originals at ~4 characters a
    token, doubled (a rewrite may run to twice the original), plus the
    JSON around each row."""
    return max(600, min(12_000, sum(len(t) for _, t in batch) // 2 + 24 * len(batch)))


def _rewrite_messages(sheet: S.Sheet, rule: S.Rewrite, batch: Sequence[Tuple[int, str]]) -> List[dict]:
    system = (
        "You rewrite ONE column of a table, cell by cell. Instruction for every cell: "
        f"{rule.instruction}. Rules: return exactly one rewrite per row id you are given — no more, no fewer; "
        "keep every timestamp (such as 00:12:30) and every quoted phrase EXACTLY as written; keep every name, id, "
        "number, date and outcome; add no fact, figure or opinion; never merge or split rows; a rewrite is at most "
        "about twice the original's length; when a cell cannot be improved, return it unchanged."
    )
    user = (
        f"Sheet: {sheet.name}\nColumn to rewrite: {rule.column}\nOther columns (context only): "
        f"{', '.join(c.name for c in sheet.columns if _fold(c.name) != _fold(rule.column))}\n\nCells:\n"
        + json.dumps([{"row": i, "text": t} for i, t in batch], ensure_ascii=False)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _figures_kept(original: str, rewrite: str) -> bool:
    """A rewrite may drop a figure or spell it out; it may not introduce one
    the original did not have (a changed duration is a changed finding)."""
    return set(S._figures_in_text(rewrite)) <= set(S._figures_in_text(original))


async def _rewrite_columns(req: ComposeRequest, spec: S.ArtifactSpec) -> Tuple[int, List[str], Dict[str, Any]]:
    """CONTRACT-2 §4 `Sheet.rewrite`: every rewrite column of every sheet,
    row by row in batches of REWRITE_BATCH_ROWS through one JSON call each
    (thinking off, temperature 0), joined back by row id through
    tables.apply_rewrites — one in, one out; a reply that loses a
    timestamp or a quoted span, changes a figure, runs far too long or is
    empty keeps the original with a warning naming the row; a batch whose
    call fails keeps every original. Blank cells are never sent and stay
    blank. Mutates the sheets. Returns (model calls, warnings, {rewritten,
    kept_original, rewrite_columns})."""
    body = spec.body
    if not isinstance(body, S.WorkbookSpec):
        return 0, [], {}
    calls = 0
    warnings: List[str] = []
    rewritten = 0
    kept_total = 0
    columns: List[str] = []
    for sheet in body.sheets:
        by_name: Dict[str, int] = {}
        for j, c in enumerate(sheet.columns):
            by_name.setdefault(_fold(c.name), j)
        for rule in sheet.rewrite:
            j = by_name.get(_fold(rule.column))
            if j is None or not sheet.rows:
                continue
            columns.append(sheet.columns[j].name)
            batches = tables.rewrite_batches(sheet.rows, j, REWRITE_BATCH_ROWS)
            originals = {i: text for batch in batches for i, text in batch}
            replies: Dict[int, str] = {}
            failed = 0
            for batch in batches:
                calls += 1
                try:
                    answer = await _json(_rewrite_messages(sheet, rule, batch), _REWRITE_SCHEMA, "artifact_rewrite", thinking=False, max_tokens=_rewrite_tokens(batch))
                except ComposeError as exc:
                    failed += len(batch)
                    log.info("artifact compose: a rewrite batch failed (%s); originals kept", exc.category)
                    continue
                for item in answer.get("rewrites") or []:
                    if isinstance(item, dict) and isinstance(item.get("row"), int) and not isinstance(item.get("row"), bool) and isinstance(item.get("text"), str):
                        replies[item["row"]] = item["text"]
            changed = [i for i, text in replies.items() if i in originals and not _figures_kept(originals[i], text)]
            for i in changed:
                replies.pop(i)
                warnings.append(f"sheet {sheet.name!r}, column {rule.column!r}: row {i + 1} kept the original (the rewrite changed a figure)")
            new_rows, kept, notes = tables.apply_rewrites(sheet.rows, j, replies)
            sheet.rows = new_rows
            rewritten += len(originals) - len(kept)
            kept_total += len(kept)
            warnings.extend(f"sheet {sheet.name!r}, column {rule.column!r}: {n}" for n in notes[:6])
            if failed:
                warnings.append(f"sheet {sheet.name!r}, column {rule.column!r}: {failed} row{'s' if failed != 1 else ''} could not be rewritten (the model call failed) and keep the original")
    if not columns:
        return calls, warnings, {}
    return calls, warnings, {"rewritten": rewritten, "kept_original": kept_total, "rewrite_columns": list(dict.fromkeys(columns))}


async def content_review(req: ComposeRequest, spec: S.ArtifactSpec, budget: T.EffortBudget, figures: Sequence[str] = ()) -> dict:
    """Does the draft do what was asked? A second, adversarial read of the
    spec against the request and the material: coverage, audience, length,
    numbers that disagree with the data, claims without a source, sections
    the template promised, recommendations mixed into findings."""
    m = req.material or Material(instruction=req.instruction)
    messages = [
        {"role": "system", "content": (
            "You are a demanding editor reviewing a draft deliverable against the request "
            "and the material it was written from. List concrete problems with where they "
            "are and how to fix them. 'must' = the reader would be misled or the request is "
            "not met (a missing requested section, a number that disagrees with the data, a "
            "claim with no support, a placeholder, the wrong audience, far too long or short). "
            "'should' = polish. Say ok=true only when there are no 'must' issues."
        )},
        {"role": "user", "content": (
            f"Request: {req.instruction or m.instruction}\n\n"
            + (f"Data:\n{_table_block(m.tables)}\n\n" if m.tables else "")
            + (f"Sources:\n{_source_block(m.sources[: budget.max_sources or 1], 12_000)}\n\n" if m.sources else "")
            + (f"Figures in the draft that appear nowhere in the material (each is derived, assumed or invented — "
               f"a 'must' unless the draft says which; the fix is to state the assumption or the arithmetic beside "
               f"the figure, or to say the figure is not given — never a placeholder, never a zero): {', '.join(figures[:12])}\n\n" if figures else "")
            + f"Draft (JSON):\n{body_json_for_prompt(spec)[:60_000]}"
        )},
    ]
    return await _json(messages, _REVIEW_SCHEMA, "artifact_review", thinking=budget.thinking, max_tokens=2000, effort=req.effort)


# ------------------------------------------------------------- visual QA --

_VISUAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ok": {"type": "boolean"},
        "issues": {
            "type": "array", "maxItems": 10,
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "page": {"type": "integer", "minimum": 1},
                    "kind": {"type": "string", "enum": ["clipped_text", "overflow", "overlap", "tiny_text", "low_contrast", "empty_page", "duplicate", "stranded_heading", "cut_table", "too_dense", "too_sparse", "broken_chart", "other"]},
                    "problem": {"type": "string", "maxLength": 300},
                    "fix": {"type": "string", "maxLength": 300},
                },
                "required": ["page", "kind", "problem", "fix"],
            },
        },
    },
    "required": ["ok", "issues"],
}


async def visual_review(pages_png: Sequence[bytes], *, kind: str, title: str) -> dict:
    """Max only: show the vision-capable main model a few rendered pages and
    ask for layout problems. Bounded by the caller (ARTIFACT_QA_PAGES,
    downscaled to ARTIFACT_QA_WIDTH); thinking OFF, because a vision call
    with thinking on and a small output budget returns nothing (engines/
    vision.py measured it)."""
    import base64

    content: List[dict] = [{"type": "text", "text": (
        f"These are rendered pages of a {kind} titled {title!r}. Look for layout defects only: "
        "clipped or overflowing text, overlapping objects, unreadably small text, low contrast, "
        "empty or duplicate pages, a heading stranded at the bottom, a table cut badly, a page "
        "far too dense or too sparse, a broken chart. Report each with its page number. If the "
        "pages look professional, say ok=true with no issues."
    )}]
    for png in pages_png:
        content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}})
    messages = [{"role": "user", "content": content}]
    return await _json(messages, _VISUAL_SCHEMA, "artifact_visual_review", thinking=False, max_tokens=1500)


async def revise(req: ComposeRequest, spec: S.ArtifactSpec, issues: Sequence[dict]) -> S.ArtifactSpec:
    """One correction pass from a list of issues (content or visual)."""
    budget = T.EFFORT_BUDGETS.get(req.effort, T.EFFORT_BUDGETS["fast"])
    fix_text = "\n".join(
        f"- page {i.get('page', '?')} {i.get('kind', '')}: {i.get('problem', '')} → {i.get('fix', '')}" if "page" in i
        else f"- {i.get('where', '')}: {i.get('problem', '')} → {i.get('fix', '')}"
        for i in list(issues)[:8]
    )
    edit_req = ComposeRequest(
        kind=req.kind, formats=req.formats, template_id=req.template_id, effort=req.effort,
        operation="edit", material=req.material, parent_spec=spec, instruction=req.instruction, author=req.author, date=req.date,
    )
    raw = await _compose_once(edit_req, budget, outline_json=None, extra=f"Fix these problems in the document and return the whole document:\n{fix_text}")
    fixed, _, _ = await _validate_or_repair(edit_req, budget, raw, None)
    # The revised draft's rows were filled afresh from the material; its
    # rewrite columns are rewritten again so the rendered file has them.
    await _rewrite_columns(edit_req, fixed)
    return fixed


# --------------------------------------------------------- the classifier --

_INTENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "wants_file": {"type": "boolean"},
        "kind": {"type": "string", "enum": ["document", "presentation", "workbook", "none"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["wants_file", "kind", "confidence"],
}


async def classify_intent(text: str) -> Optional[dict]:
    """The small strict-JSON classifier the intent gate consults ONLY for its
    ambiguous band. Returns the dict, or None when unsure (< 0.8)."""
    messages = [
        {"role": "system", "content": (
            "Decide whether this chat message asks the assistant to PRODUCE a file (a document, "
            "a slide deck, a spreadsheet) as opposed to answering in text or asking about file "
            "formats. Answer with JSON only."
        )},
        {"role": "user", "content": text[:2000]},
    ]
    obj = await _json(messages, _INTENT_SCHEMA, "artifact_intent", thinking=False, max_tokens=200)
    if float(obj.get("confidence", 0)) < 0.8:
        return None
    return obj


def material_from_history(history: Sequence[dict], *, max_chars: int = 24_000) -> str:
    """The conversation as the model should see it: role-tagged, newest last,
    clipped from the OLD end so the recent turns survive."""
    lines: List[str] = []
    for turn in history:
        role = str(turn.get("role") or "user")
        content = turn.get("content")
        if isinstance(content, list):
            content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        text = re.sub(r"\s+", " ", str(content or "")).strip()
        if text:
            lines.append(f"{role}: {text}")
    joined = "\n".join(lines)
    return joined[-max_chars:] if len(joined) > max_chars else joined


__all__ = [
    "ComposeError", "Source", "DataTable", "Material", "ComposeRequest", "ComposeResult",
    "compose", "outline", "content_review", "visual_review", "revise", "classify_intent",
    "material_from_history", "requested_sections", "body_json_for_prompt", "REWRITE_BATCH_ROWS",
]
