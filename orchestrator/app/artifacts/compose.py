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
        "rows from the material only (never invented numbers), totals as "
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
    system = (
        f"{_ROLE}\n\n{_KIND_GUIDE[req.kind]}{caps}\n\n{_TEMPLATE_GUIDE.get(req.template_id, _TEMPLATE_GUIDE['generic'])}\n\n"
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
            + req.parent_spec.body.model_dump_json()
        )
    if extra:
        messages.append({"role": "user", "content": extra})
    return await _json(messages, S.schema_for(req.kind), f"artifact_{req.kind}", thinking=budget.thinking, max_tokens=_max_tokens_for(req.kind, req.effort), effort=req.effort)


def _enforce_caps(spec: S.ArtifactSpec, budget: T.EffortBudget) -> List[str]:
    """Trim what the effort level allows rather than refuse: a deck with 14
    slides at Fast becomes 12 with a warning, not an error."""
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
            if len(sh.rows) > budget.max_rows_per_sheet:
                warnings.append(f"sheet {sh.name!r} was cut to {budget.max_rows_per_sheet} rows")
                sh.rows = sh.rows[: budget.max_rows_per_sheet]
    if isinstance(body, S.DocumentSpec):
        top = sum(1 for b in body.blocks if isinstance(b, S.Heading) and b.level == 1)
        if top > budget.max_sections:
            warnings.append(f"the document has {top} top-level sections; this effort level asked for at most {budget.max_sections}")
    return warnings


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
    result_warnings.extend(_enforce_caps(spec, budget))
    await say(95.0, "content ready")
    return ComposeResult(spec=spec, warnings=result_warnings, corrections=corrections, outline=outline_json, review=review_json, model_calls=calls)


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


async def _validate_or_repair(req: ComposeRequest, budget: T.EffortBudget, raw: dict, outline_json: Optional[dict]):
    """parse_body, and on a validation error ONE repair pass with the field
    paths — the same recipe as the Salesforce planner. Returns (spec, repairs).
    The sources manifest is reconciled against the material FIRST, so the
    model's own list never reaches validation, let alone a page."""
    notes = _reconcile_sources(raw, req.material)
    _pin_template(raw, req)
    try:
        return S.parse_body(req.kind, raw), 0, notes
    except ValidationError as exc:
        summary = S.validation_summary(exc)
        log.info("artifact compose: spec invalid, repairing once: %s", summary.replace("\n", " | ")[:400])
    fixed = await _compose_once(
        req, budget, outline_json=outline_json,
        extra=f"Your JSON did not match the schema:\n{summary}\nReturn the corrected, complete document.",
    )
    notes = _reconcile_sources(fixed, req.material)
    _pin_template(fixed, req)
    try:
        return S.parse_body(req.kind, fixed), 1, notes
    except ValidationError as exc:
        # Field paths and rule names only — the operator's key to a repair
        # that did not take; the content never reaches the log.
        log.info("artifact compose: the repair was invalid too: %s", S.validation_summary(exc).replace("\n", " | ")[:400])
        raise ComposeError("model_failure", "The model could not produce a valid document structure.") from exc


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
            + f"Draft (JSON):\n{spec.body.model_dump_json()[:60_000]}"
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
    "material_from_history",
]
