"""The artifact engine: a chat turn that asks for a file.

    person: "Create a one-page executive brief from this."
    engine: intent → formats → material → ACCEPT a durable job → forward its
            stages as `step` events → wait → one sentence + ONE meta with
            `artifacts[]`.

The turn is a WITNESS to the job, not its owner. `pipeline.accept` persists
the job before any model call; the run happens under the pipeline's lease
whether or not this turn (or its browser) survives; this engine subscribes
to the job's progress exactly as engines/video.py subscribes to an analysis,
forwards each stage as a `step` with the fixed id from artifacts.types, and
when the job ends it says what was made. A reload re-attaches through the
chat_requests machinery and sees the same steps and the same final meta.

WHAT THE ENGINE DECIDES (code, never the model): the operation (create /
edit / convert / export), which existing artifact a follow-up means, the
kind, formats and template (formats.decide — explicit wins, the rule is
recorded), and the material the composer may use. In Salesforce mode a
data-shaped request gets ONE guarded query through the existing SQL engine
and its rows travel as a DataTable the model may place but not recompute; in
Assistant mode at Think/Max, a request that needs current facts may gather a
bounded set of web sources through the existing search engine, recorded
with URLs and retrieval dates. Uploaded documents are already pinned into
`history` by main.py and reach the composer as conversation material.

WHAT THE ENGINE NEVER DOES: invent an id, a filename, a URL or a completion
state; paste the document into the chat; emit more than one meta.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .. import db
from ..artifacts import compose as C
from ..artifacts import formats as F
from ..artifacts import pipeline
from ..artifacts import types as T
from ..artifacts import db as adb
from ..artifacts.intent import ArtifactIntent
from ..config import settings

log = logging.getLogger(__name__)

Emit = Callable[[str, dict], Awaitable[None]]

#: How the pipeline's stage events map to the browser's fixed step ids.
_STEP_IDS = T.STEP_IDS
_TITLES = T.STAGE_TITLES

_FORMAT_WORDS = {"pdf": "PDF", "docx": "Word", "pptx": "PowerPoint", "xlsx": "Excel"}
_KIND_WORDS = {"document": "document", "presentation": "deck", "workbook": "workbook"}


# ------------------------------------------------------------ references --


def _pick_artifact(candidates: Sequence[dict], intent: ArtifactIntent) -> Optional[dict]:
    """Which existing artifact a follow-up means. The newest unless the
    words name another — by title fragment, by kind word ("the deck"), or
    by format ("the PDF"). Two equal matches are genuinely ambiguous and
    the caller asks."""
    if not candidates:
        return None
    hint = (intent.reference_hint or "").lower().strip()
    if not hint or intent.reference == "latest":
        return candidates[0]
    kind_for_word = {"deck": "presentation", "presentation": "presentation", "slides": "presentation",
                     "spreadsheet": "workbook", "workbook": "workbook", "tracker": "workbook",
                     "document": "document", "report": "document", "brief": "document", "proposal": "document",
                     "sop": "document", "memo": "document", "pdf": None, "docx": None, "pptx": "presentation", "xlsx": "workbook"}
    by_title = [c for c in candidates if hint in str(c.get("title") or "").lower()]
    if len(by_title) >= 1:
        return by_title[0]
    kind = kind_for_word.get(hint)
    if kind:
        by_kind = [c for c in candidates if c.get("kind") == kind]
        if by_kind:
            return by_kind[0]
    return candidates[0]


# -------------------------------------------------------------- material --


async def _salesforce_table(instruction: str, history: Sequence[dict]) -> Tuple[List[C.DataTable], List[C.Source], List[str]]:
    """Salesforce mode: ONE guarded query for a data-shaped request, through
    the same engine the chat uses. Empty data is said, not invented."""
    from .sql import generate_and_run_sql

    notes: List[str] = []
    try:
        sql, columns, rows = await asyncio.wait_for(
            generate_and_run_sql(instruction, history=history, fetch_cap=500), timeout=90.0,
        )
    except asyncio.TimeoutError:
        notes.append("Salesforce data could not be queried in time; the document says so where numbers were expected.")
        return [], [], notes
    except Exception as exc:  # noqa: BLE001 — the document is still made, without the numbers
        log.info("artifact: salesforce query failed: %s", type(exc).__name__)
        notes.append("Salesforce data was not available for this request; the document says so where numbers were expected.")
        return [], [], notes
    if not columns or not rows:
        notes.append("The Salesforce query returned no rows; the document must say the data is empty rather than estimate it.")
        return [], [], notes
    today = _dt.date.today().isoformat()
    source = C.Source(id="sf1", title="Salesforce (synced data)", text=f"Query run on {today}: {sql[:400]}", kind="salesforce", retrieved_at=today)
    table = C.DataTable(id="t1", title="Salesforce query result", columns=[str(c) for c in columns], rows=[list(r) for r in rows[:T.MAX_ROWS_PER_SHEET]], source_id="sf1")
    return [table], [source], notes


_DATA_SHAPED_RE = re.compile(
    r"\b(pipeline|opportunit|account|lead|contact|revenue|bookings|forecast|quota|deal|win rate|close|stage|"
    r"by (?:region|owner|rep|quarter|month|stage|product)|top \d+|total|sum|average|count|how many)\b",
    re.I,
)


async def _web_sources(instruction: str, history: Sequence[dict], *, effort: str, user_id: int, conversation_id: str, emit: Emit) -> List[C.Source]:
    """Think/Max in Assistant mode: a bounded set of web sources through the
    existing search engine, only when the search gate says the request
    needs the web. Best-effort with a deadline; no sources is not an error."""
    budget = T.EFFORT_BUDGETS.get(effort, T.EFFORT_BUDGETS["fast"])
    if not budget.research or budget.max_sources <= 0:
        return []
    try:
        from . import search as search_engine

        if not await asyncio.wait_for(search_engine.should_search(instruction, history), timeout=15.0):
            return []
        queries = await asyncio.wait_for(search_engine.rewrite_queries(instruction, history=history, effort=effort), timeout=20.0)
        results = await asyncio.wait_for(search_engine._collect_results(list(queries)[:3], effort=effort), timeout=30.0)
        fetched = await asyncio.wait_for(
            search_engine._fetch_sources(results[: budget.max_sources], instruction, user_id=user_id, conversation_id=conversation_id),
            timeout=60.0,
        )
    except Exception as exc:  # noqa: BLE001 — research is an enhancement
        log.info("artifact: web research skipped: %s", type(exc).__name__)
        return []
    today = _dt.date.today().isoformat()
    out: List[C.Source] = []
    for s in fetched[: budget.max_sources]:
        text = (getattr(s, "text", "") or "").strip()
        if not text:
            continue
        out.append(C.Source(id=f"w{len(out) + 1}", title=str(getattr(s, "title", "") or getattr(s, "url", "")), text=text[:6000],
                            url=str(getattr(s, "url", "") or ""), retrieved_at=today, kind="web"))
    return out


def _previous_answer(history: Sequence[dict]) -> str:
    for turn in reversed(list(history)):
        if str(turn.get("role")) == "assistant":
            content = turn.get("content")
            if isinstance(content, list):
                content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
            return str(content or "").strip()
    return ""


def _material_dict(m: C.Material) -> dict:
    return {
        "history_text": m.history_text,
        "previous_answer": m.previous_answer,
        "uploads_text": m.uploads_text,
        "sources": [s.__dict__ for s in m.sources],
        "tables": [t.__dict__ for t in m.tables],
        "notes": list(m.notes),
        "salesforce": {},
    }


def _material_from_dict(d: dict) -> C.Material:
    return C.Material(
        instruction="",
        history_text=str(d.get("history_text") or ""),
        previous_answer=str(d.get("previous_answer") or ""),
        uploads_text=str(d.get("uploads_text") or ""),
        sources=[C.Source(**{k: v for k, v in s.items() if k in C.Source.__dataclass_fields__}) for s in d.get("sources") or [] if isinstance(s, dict)],
        tables=[C.DataTable(**{k: v for k, v in t.items() if k in C.DataTable.__dataclass_fields__}) for t in d.get("tables") or [] if isinstance(t, dict)],
        notes=[str(n) for n in d.get("notes") or []],
    )


# ----------------------------------------------------------- the composer --


async def compose_for_pipeline(ctx: "pipeline.ComposeContext"):
    """Installed on the pipeline at startup: the composer the runner calls
    from the compose stage — in THIS process, under the job's lease, from
    the material persisted at acceptance (so a requeued job sees the same
    evidence). Announces the composer-owned stages on the way."""
    material = _material_from_dict(ctx.material)
    material.instruction = ctx.instruction
    await ctx.progress_stage("intent", "done", f"{_KIND_WORDS.get(ctx.kind, ctx.kind)} · {', '.join(_FORMAT_WORDS.get(f, f) for f in ctx.formats)} · {ctx.template_id.replace('_', ' ')}")
    gathered = []
    if material.sources:
        gathered.append(f"{len(material.sources)} source(s)")
    if material.tables:
        gathered.append(f"{len(material.tables)} data table(s)")
    if material.uploads_text:
        gathered.append("uploaded material")
    await ctx.progress_stage("gather", "done", " · ".join(gathered) or "from the conversation")

    budget = ctx.budget
    if ctx.operation == "convert":
        # No model call: the stored spec renders again in the new format.
        if ctx.parent_spec is None:
            raise pipeline.StageFailure("invalid_request", "The version to convert has no stored content.")
        await ctx.progress_stage("outline", "skipped", "conversion keeps the content")
        return ctx.parent_spec

    if budget.outline_pass and ctx.operation != "edit":
        await ctx.progress_stage("outline", "running", "")
    else:
        await ctx.progress_stage("outline", "skipped", "Fast goes straight to writing" if not budget.outline_pass else "an edit keeps the structure")

    req = C.ComposeRequest(
        kind=ctx.kind, formats=ctx.formats, template_id=ctx.template_id, effort=ctx.effort,
        operation="edit" if ctx.operation == "edit" else "create", material=material,
        parent_spec=ctx.parent_spec if ctx.operation == "edit" else None,
        instruction=ctx.instruction, date=_dt.date.today().strftime("%d %B %Y"),
    )
    outline_seen = {"done": False}

    async def progress(pct: Optional[float], detail: str) -> None:
        if not outline_seen["done"] and detail == "writing" and budget.outline_pass and ctx.operation != "edit":
            outline_seen["done"] = True
            await ctx.progress_stage("outline", "done", "")
        await ctx.progress(pct, detail)

    try:
        result = await C.compose(req, progress=progress)
    except C.ComposeError as exc:
        raise pipeline.StageFailure(exc.category, str(exc)) from exc
    for w in result.warnings:
        ctx.warn(w)
    if result.corrections > 1:
        ctx.warn(f"{result.corrections} correction passes were made")
    return result.spec


# ---------------------------------------------------------------- the turn --


def _sentence(ref: T.ArtifactRef, operation: str, warnings: Sequence[str]) -> str:
    fmts = " and ".join(_FORMAT_WORDS.get(f.format, f.format) for f in ref.files) or "the file"
    what = ref.title or _KIND_WORDS.get(ref.kind, "document")
    verb = {"create": "Created", "edit": "Updated", "convert": "Converted"}.get(operation, "Created")
    line = f"{verb} **{what}** as {fmts}." if operation != "convert" else f"{verb} **{what}** to {fmts}."
    if warnings:
        line += " " + " ".join(f"_{w.rstrip('.')}._" for w in list(warnings)[:2])
    return line


async def _forward_progress(job_id: str, emit: Emit) -> Optional[dict]:
    """Subscribe to the job and forward each stage as a `step`, like
    engines/video._wait_for_analysis. Returns the fresh job row when done."""
    queue = pipeline.subscribe(job_id)
    last: Dict[str, Tuple[float, Optional[float], str]] = {}
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                if not pipeline.is_running(job_id):
                    row = await db.run_in_thread(adb.load_job, job_id)
                    if row and row.get("status") in T.TERMINAL_STATUSES:
                        break
                    if row and row.get("status") == "queued":
                        await emit("status", {"text": "Waiting for capacity to build the file…"})
                        await pipeline.ensure_running(job_id)
                continue
            stage = str(event.get("stage") or "")
            status = str(event.get("status") or "")
            if stage == "_done":
                break
            if stage not in _STEP_IDS:
                continue
            percent = event.get("percent")
            detail = str(event.get("detail") or "")
            elapsed = float(event.get("elapsed_s") or 0.0)
            now = time.monotonic()
            prev = last.get(stage)
            if status == "running" and prev is not None:
                prev_t, prev_pct, prev_status = prev
                moved = percent is None or prev_pct is None or abs(float(percent) - float(prev_pct)) >= 5
                if prev_status == "running" and now - prev_t < 3.0 and not moved:
                    continue
            step_status = "running" if status == "running" else ("failed" if status in ("failed", "deferred") else "done")
            bits: List[str] = []
            if status == "running" and isinstance(percent, (int, float)):
                bits.append(f"{float(percent):.0f}%")
            if detail:
                bits.append(detail)
            if status == "running" and elapsed >= 1:
                bits.append(f"{elapsed:.0f}s")
            if status == "skipped":
                bits.insert(0, "skipped")
            elif event.get("cached"):
                bits.insert(0, "cached")
            await emit("step", {"id": _STEP_IDS[stage], "title": _TITLES[stage], "status": step_status, "detail": " · ".join(bits)})
            if status == "running":
                await emit("status", {"text": f"{_TITLES[stage]}…"})
            last[stage] = (now, float(percent) if isinstance(percent, (int, float)) else None, status)
    finally:
        pipeline.unsubscribe(job_id, queue)
    return await pipeline.wait_for(job_id)


async def run_artifact_engine(
    text: str,
    history: Sequence[dict],
    emit: Emit,
    *,
    intent: ArtifactIntent,
    conversation_id: str,
    user_id: int,
    generation_id: str,
    effort: str = "fast",
    mode: str = "assistant",
    web_allowed: bool = False,
) -> str:
    """The whole turn. Returns the sentence that was streamed."""
    effort = effort if effort in T.EFFORT_BUDGETS else "fast"
    instruction = intent.instruction or text

    # 1. What exists already, for follow-ups.
    existing = await db.run_in_thread(adb.list_artifacts, int(user_id), conversation_id)
    candidates = [a for a in existing if (a.get("current") or {}).get("status") in ("completed", "completed_with_warnings")]
    operation = intent.action if intent.action in ("edit", "convert") else "create"
    parent: Optional[Tuple[str, int]] = None
    parent_row: Optional[dict] = None
    if intent.action in ("edit", "convert"):
        parent_row = _pick_artifact(candidates, intent)
        if parent_row is None:
            # Nothing to edit: the words were about a file, but there is none — make one.
            operation = "create"
        else:
            version = int(intent.version or parent_row.get("current_version") or 1)
            parent = (str(parent_row["id"]), version)

    # 2. Kind, formats, template.
    if parent_row is not None and operation == "edit":
        kind = str(parent_row.get("kind") or "document")
        current = parent_row.get("current") or {}
        formats = [f.get("format") for f in (current.get("files") or []) if f.get("format")] or list(T.FORMATS_FOR_KIND[kind][:1])
        template_id = str(current.get("template_id") or "generic")
        reason = "edit keeps the formats"
        warnings: List[str] = []
    elif parent_row is not None and operation == "convert":
        kind = str(parent_row.get("kind") or "document")
        ok, bad = F.formats_for_conversion(kind, intent.formats or [])
        if intent.version and not intent.formats:
            # "go back to version 1": the same formats that version had.
            ok = [f.get("format") for f in ((parent_row.get("current") or {}).get("files") or []) if f.get("format")]
        if not ok and re.search(r"\bconvert\b", instruction, re.I):
            what = ", ".join(_FORMAT_WORDS.get(b, b) for b in bad) or "that format"
            line = f"A {_KIND_WORDS.get(kind, kind)} cannot be converted to {what}. I can make it as {' or '.join(_FORMAT_WORDS[f] for f in T.FORMATS_FOR_KIND[kind])}."
            await emit("token", {"text": line})
            await emit("meta", {"route": "artifact", "effort": effort})
            return line
        if not ok:
            # "Also give me this as Excel" after a deck: not a conversion the
            # deck can take — a NEW workbook from the same conversation is
            # what was asked for.
            operation, parent, parent_row = "create", None, None
            decision = F.decide(instruction, explicit_only=intent.formats or None)
            kind, formats, template_id, reason, warnings = decision.kind, decision.formats, decision.template_id, f"new {decision.kind}: {decision.reason}", list(decision.warnings)
        else:
            formats, template_id = ok, str((parent_row.get("current") or {}).get("template_id") or "generic")
            reason = f"convert: {', '.join(ok)}"
            warnings = [f"{', '.join(bad)} cannot be produced for a {kind}"] if bad else []
    else:
        decision = F.decide(instruction, explicit_only=intent.formats or None)
        kind, formats, template_id, reason, warnings = decision.kind, decision.formats, decision.template_id, decision.reason, list(decision.warnings)

    # 3. Material.
    material = C.Material(instruction=instruction, history_text=C.material_from_history(history))
    material.notes.append(f"Today is {_dt.date.today().strftime('%d %B %Y')}.")
    material.notes.append("Mode: Salesforce workspace" if mode == "salesforce" else "Mode: assistant")
    if intent.action == "export":
        material.previous_answer = _previous_answer(history)
        material.notes.append("Turn the previous answer into the file faithfully; do not add claims it did not make.")
    if operation == "create" and mode == "salesforce" and _DATA_SHAPED_RE.search(instruction):
        await emit("status", {"text": "Querying Salesforce data…"})
        tables, sources, notes = await _salesforce_table(instruction, history)
        material.tables.extend(tables)
        material.sources.extend(sources)
        material.notes.extend(notes)
    if operation == "create" and mode == "assistant" and web_allowed:
        await emit("status", {"text": "Checking whether current sources are needed…"})
        material.sources.extend(await _web_sources(instruction, history, effort=effort, user_id=int(user_id), conversation_id=conversation_id, emit=emit))
    if not material.sources and not material.tables:
        material.notes.append("No external sources were gathered; write from the conversation and say where something is an assumption.")

    # 4. Accept — persisted before any model call.
    try:
        job = await db.run_in_thread(
            pipeline.accept,
            user_id=int(user_id), conversation_id=conversation_id, generation_id=generation_id,
            operation=operation, instruction=instruction, kind=kind, formats=formats, format_reason=reason,
            effort=effort, mode=mode, template_id=template_id, parent=parent,
            requested_formats=intent.formats or None, material=_material_dict(material),
            title=str(parent_row.get("title") or "") if parent_row else "",
        )
    except pipeline.ArtifactRefused as exc:
        line = str(exc) or pipeline.safe_error(getattr(exc, "category", "invalid_request"))
        await emit("token", {"text": line})
        await emit("meta", {"route": "artifact", "effort": effort})
        return line

    await emit("step", {"id": _STEP_IDS["intent"], "title": _TITLES["intent"], "status": "running", "detail": ""})
    await emit("status", {"text": "Preparing the file…"})
    await pipeline.ensure_running(str(job["id"]))
    row = await _forward_progress(str(job["id"]), emit) or job

    # 5. Say what happened, once, and hand over the reference.
    version_row = None
    try:
        version_row = await db.run_in_thread(adb.get_version, str(row["artifact_id"]), int(row["version"]), int(user_id))
    except Exception:  # noqa: BLE001 — the ref still carries the job
        version_row = None
    ref = pipeline.ref_for(row, version_row)
    all_warnings = list(warnings) + [w for w in ref.warnings if w not in warnings]
    ref.warnings = all_warnings
    status = str(row.get("status") or "")
    if status in ("completed", "completed_with_warnings"):
        line = _sentence(ref, operation, all_warnings)
    elif status == "cancelled":
        line = "The file was cancelled before it was finished."
    else:
        line = str(row.get("error") or "") or pipeline.safe_error(str(row.get("failure_category") or "renderer_failure"))
        line = f"I couldn't finish the file: {line}"
    await emit("token", {"text": line})
    await emit("meta", {"route": "artifact", "effort": effort, "artifacts": [ref.to_json()]})
    return line


async def visual_reviewer(ctx: "pipeline.ComposeContext", spec, pages: List[bytes]):
    """Installed on the pipeline for Max effort: the vision-capable model
    looks at a few rendered pages; layout defects it reports become ONE
    correction pass through the composer's `revise`. None means the pages
    looked right."""
    verdict = await C.visual_review(pages, kind=spec.kind, title=spec.title)
    issues = [i for i in (verdict or {}).get("issues", []) if isinstance(i, dict)]
    if not issues:
        return None
    material = _material_from_dict(ctx.material)
    material.instruction = ctx.instruction
    req = C.ComposeRequest(kind=ctx.kind, formats=ctx.formats, template_id=ctx.template_id, effort=ctx.effort,
                           operation="edit", material=material, parent_spec=spec, instruction=ctx.instruction)
    return await C.revise(req, spec, issues)


async def classify_hook(text: str) -> Optional[ArtifactIntent]:
    """The strict-JSON classifier the intent gate may consult for its
    ambiguous band. Returns an intent only when the model is sure."""
    verdict = await C.classify_intent(text)
    if not verdict or not verdict.get("wants_file"):
        return None
    return ArtifactIntent("create", rule="model", instruction=text)


__all__ = ["run_artifact_engine", "compose_for_pipeline", "visual_reviewer", "classify_hook"]
