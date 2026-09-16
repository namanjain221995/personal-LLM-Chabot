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

A PASTED TABLE IS DATA, NOT PROSE (CONTRACT-2 §6, §11). The turn's ORIGINAL
text (`intent.raw_text`, tabs and newlines intact) and the last three user
turns are parsed with tables.parse_table; the first table found travels as
`DataTable(id="paste1")` with every blank cell blank, a second as "paste2",
and the leading column is forward-filled only on evidence (a grouping header
such as Host with blank continuation rows). What code did is the `transform`
the completion sentence reports. An uploaded CSV in this conversation's
dataset workspace is read the same way as "upload1". The parse runs in a
thread within a byte budget (security review of 2026-09-12, #10): four
worst-case pastes held the event loop — every stream, heartbeat and health
probe — for 43 seconds when it ran inline and unbounded.

THE ENGINE'S REGEXES READ THE DECISION'S VIEW. Every instruction this
module runs a regex over (formats.decide, the data-shaped test, the
sentence's "audit") is whitespace-collapsed and cut at intent._DECIDE_CHARS
first, as the rules cut theirs: the classifier path once handed the full
message through, and a 60 KB paste cost seven seconds of event loop in
formats' word-gap regex (#8).

WHAT THE ENGINE NEVER DOES: invent an id, a filename, a URL or a completion
state; paste the document into the chat; emit more than one meta.

AN EDIT IS PLANNED BEFORE IT IS ACCEPTED (AS3 prompt-edits, see the
"AS3 edits" section below and artifacts/edits.py): typed ops applied by
code, a no-op answered without a job, a restore copied byte for byte.
"""
from __future__ import annotations

import asyncio
import functools
import datetime as _dt
import logging
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .. import db
from ..artifacts import compose as C
from ..artifacts import formats as F
from ..artifacts import pipeline
from ..artifacts import tables
from ..artifacts import types as T
from ..artifacts import db as adb
from ..artifacts import intent as intent_rules
from ..artifacts.intent import ArtifactIntent
from ..config import settings

log = logging.getLogger(__name__)

Emit = Callable[[str, dict], Awaitable[None]]

#: How the pipeline's stage events map to the browser's fixed step ids.
_STEP_IDS = T.STEP_IDS
_TITLES = T.STAGE_TITLES

#: Read with .get everywhere: a format this table does not name is said by
#: its id, never a KeyError in the middle of a turn (the discovery of
#: 2026-09-12 found `[]` indexing at the conversion refusal).
_FORMAT_WORDS = {"pdf": "PDF", "docx": "Word", "pptx": "PowerPoint", "xlsx": "Excel", "csv": "CSV",
                 # types.py gained the chart images on 2026-09-15; without
                 # them here the conversion refusal printed the raw ids
                 # ("PowerPoint or PDF or png").
                 "png": "PNG image", "svg": "SVG image"}
_KIND_WORDS = {"document": "document", "presentation": "deck", "workbook": "workbook"}
#: How many of the previous user turns are searched for a pasted table
#: (CONTRACT-2 §11): the table is often the message BEFORE "now make it
#: an Excel file".
_TABLE_HISTORY_TURNS = 3
#: Words for small counts in the completion sentence.
_COUNT_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}


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


def _turn_text(turn: dict) -> str:
    content = turn.get("content")
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content or "")


def _decision_text(text: str) -> str:
    """The rules' view of a message: whitespace collapsed, cut at
    intent._DECIDE_CHARS — the only text this module runs a regex over
    (#8). intent.decide builds `instruction` the same way; this is for an
    intent that reached the engine with more than that."""
    return " ".join((text or "").split())[:intent_rules._DECIDE_CHARS]


def _utf8_len(text: str) -> int:
    """UTF-8 bytes of `text`, without encoding a text that is over the
    paste cap by its character count alone (a character is at least one
    byte)."""
    return len(text) if len(text) > tables.MAX_PASTE_BYTES else len(text.encode("utf-8"))


def _pasted_tables(raw_text: str, history: Sequence[dict]) -> Tuple[List[C.DataTable], Dict[str, Any], List[str]]:
    """Blocking (the caller runs it in a thread). The tables pasted into
    this turn and the last _TABLE_HISTORY_TURNS user turns, newest first
    — at most two, as `paste1` and `paste2` (CONTRACT-2 §11). Each is
    parsed by tables.parse_table (every line a row, blanks kept) and its
    leading column forward-filled ONLY on evidence (tables.forward_fill's
    rules: a grouping header, a text column, ≥ 30% blank, every blank run
    under a filled cell). Returns the tables, the transform report of the
    first (rows, blanks, forward_filled, the filled column's label, the
    parse warnings) and the notes the composer should read. A paste past
    the size caps is returned with `truncated` in the report so the
    caller can refuse.

    BOUNDED BEFORE A BYTE IS PARSED (#10). The turn's own text is cut at
    the paste cap plus one character (parse_table then reports it
    truncated, and the caller refuses with the count of the rows it saw);
    a history turn over the cap is skipped — it was refused when it was
    sent, or was never a table — and the turns are parsed newest first
    only while the whole budget (tables.MAX_PASTE_BYTES, the same cap)
    is unspent. So one request parses at most ~5 MB, in a thread, and a
    history the client wrote cannot multiply that."""
    cap = int(tables.MAX_PASTE_BYTES)
    texts: List[str] = [(raw_text or "")[: cap + 1]]
    users = [t for t in history if str(t.get("role")) == "user"]
    texts.extend(_turn_text(t) for t in reversed(users[-_TABLE_HISTORY_TURNS:]))
    out: List[C.DataTable] = []
    transform: Dict[str, Any] = {}
    notes: List[str] = []
    budget = cap
    for index, text in enumerate(texts):
        if len(out) >= 2:
            break
        size = _utf8_len(text)
        if index > 0 and (size > cap or size > budget):
            continue
        budget -= size
        try:
            parsed = tables.parse_table(text)
        except Exception as exc:  # noqa: BLE001 — a paste the parser chokes on is prose
            log.info("artifact: table parse failed: %s", type(exc).__name__)
            parsed = None
        if parsed is None:
            continue
        filled = tables.forward_fill(parsed, 0, evidence=True)
        table_id = f"paste{len(out) + 1}"
        out.append(filled.to_material_table(table_id, title=f"Pasted table {len(out) + 1}"))
        report = filled.report
        if not transform:
            transform = {
                "rows": report["rows"], "columns": report["columns"], "blanks": report["blanks"],
                "forward_filled": report["forward_filled"], "warnings": report["warnings"][:10],
                "truncated": report["truncated"], "total_rows": report["total_rows"],
            }
            if report["forward_filled"]:
                transform["forward_filled_column"] = filled.columns[0].strip()
        bits = [f"TABLE {table_id} was pasted as text: {report['rows']:,} rows × {report['columns']} columns"]
        if report["blanks"]:
            bits.append(f"{report['blanks']} cells are blank in the source and stay blank")
        if report["forward_filled"]:
            bits.append(f"{report['forward_filled']} blank {filled.columns[0].strip()!r} cells were filled from the row above (continuation rows)")
        notes.append("; ".join(bits) + ".")
    return out, transform, notes


def _upload_tables(conversation_id: str) -> List[C.DataTable]:
    """Blocking. The CSV files uploaded to this conversation's dataset
    workspace, read through the same rows the dataset engine profiles
    (db.get_uploads → the stored profile, `core.upload_paths.
    resolve_upload_file` for the bytes): the file itself when it is still
    on disk (up to MAX_ROWS_PER_SHEET rows through csv.reader), else the
    profile's `full_rows` when the file was small enough to be kept whole.
    An .xlsx or a file the profile only sampled is left out — a sampled
    table would be a silently short dataset. At most two, as "upload1"
    and "upload2"; a turn with a pasted table does not look here."""
    import csv as _csv

    from ..core.upload_paths import UploadPathError, resolve_upload_file

    try:
        uploads = db.get_uploads(conversation_id)
    except Exception as exc:  # noqa: BLE001 — no uploads is the common case
        log.info("artifact: uploads unavailable: %s", type(exc).__name__)
        return []
    out: List[C.DataTable] = []
    for up in uploads:
        if len(out) >= 2:
            break
        profiles = up.get("profile")
        if isinstance(profiles, dict):
            profiles = [profiles]
        if not isinstance(profiles, list):
            continue
        for prof in profiles:
            if not isinstance(prof, dict) or prof.get("kind") != "table":
                continue
            name = str(prof.get("file") or "")
            columns = [str(c.get("name")) for c in (prof.get("columns") or []) if isinstance(c, dict) and c.get("name")]
            rows: List[List[Any]] = []
            read = False
            if name.lower().endswith(".csv") and "/" not in name and "\\" not in name:
                try:
                    path = resolve_upload_file(settings.workspace_dir, conversation_id, str(up.get("id") or ""), name)
                except UploadPathError:
                    path = None
                if path is not None and path.is_file():
                    try:
                        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
                            reader = _csv.reader(fh)
                            header = next(reader, None)
                            if header:
                                columns = [str(h) for h in header]
                                for record in reader:
                                    if len(rows) >= T.MAX_ROWS_PER_SHEET:
                                        break
                                    if not any(c.strip() for c in record):
                                        continue  # a blank line is not a row (csv.reader yields [] for it)
                                    cells = [c if c != "" else None for c in record]
                                    cells = (cells + [None] * len(columns))[: len(columns)]
                                    rows.append(cells)
                                read = True
                    except (OSError, UnicodeError, _csv.Error) as exc:
                        log.info("artifact: upload %s could not be read: %s", name, type(exc).__name__)
            if not read and prof.get("full_content") and isinstance(prof.get("full_rows"), list) and columns:
                rows = [[(None if r.get(c) in (None, "") else r.get(c)) for c in columns] for r in prof["full_rows"] if isinstance(r, dict)]
                read = True
            if not read or not columns or not rows:
                continue
            table_id = f"upload{len(out) + 1}"
            out.append(C.DataTable(id=table_id, title=name or "Uploaded table", columns=columns, rows=rows[: T.MAX_ROWS_PER_SHEET]))
            if len(out) >= 2:
                break
    return out


def _previous_answer(history: Sequence[dict]) -> str:
    for turn in reversed(list(history)):
        if str(turn.get("role")) == "assistant":
            content = turn.get("content")
            if isinstance(content, list):
                content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
            return str(content or "").strip()
    return ""


#: The note that carries the requested row count through material.json —
#: pipeline._material (wave 3) whitelists the material's keys and does not
#: yet pass `row_count` or `transform`; the count rides as a note the
#: composer reads back, and the transform on the pasted table's own dict
#: (a table's provenance), until the pipeline passes both through.
_ROW_COUNT_NOTE = "The person asked for exactly {n:,} rows."
_ROW_COUNT_NOTE_RE = re.compile(r"asked for exactly ([\d,]+) rows")


def _material_dict(m: C.Material) -> dict:
    tables_out = [dict(t.__dict__) for t in m.tables]
    if m.transform and tables_out:
        tables_out[0]["transform"] = dict(m.transform)
    return {
        "history_text": m.history_text,
        "previous_answer": m.previous_answer,
        "uploads_text": m.uploads_text,
        "sources": [s.__dict__ for s in m.sources],
        "tables": tables_out,
        "notes": list(m.notes),
        "salesforce": {},
        "transform": dict(m.transform),
        "row_count": m.row_count,
    }


def _material_from_dict(d: dict) -> C.Material:
    row_count = d.get("row_count")
    if not (isinstance(row_count, int) and not isinstance(row_count, bool) and row_count > 0):
        row_count = None
        for note in d.get("notes") or []:
            m = _ROW_COUNT_NOTE_RE.search(str(note))
            if m:
                row_count = int(m.group(1).replace(",", ""))
                break
    transform = d.get("transform") if isinstance(d.get("transform"), dict) else None
    if not transform:
        first = next((t for t in d.get("tables") or [] if isinstance(t, dict) and isinstance(t.get("transform"), dict)), None)
        transform = first["transform"] if first else {}
    return C.Material(
        instruction="",
        history_text=str(d.get("history_text") or ""),
        previous_answer=str(d.get("previous_answer") or ""),
        uploads_text=str(d.get("uploads_text") or ""),
        sources=[C.Source(**{k: v for k, v in s.items() if k in C.Source.__dataclass_fields__}) for s in d.get("sources") or [] if isinstance(s, dict)],
        tables=[C.DataTable(**{k: v for k, v in t.items() if k in C.DataTable.__dataclass_fields__}) for t in d.get("tables") or [] if isinstance(t, dict)],
        notes=[str(n) for n in d.get("notes") or []],
        transform=dict(transform or {}),
        row_count=row_count,
    )


# ----------------------------------------------------------- the composer --


def _tables_from_parent(material: C.Material, parent: Any) -> None:
    """An EDIT of a workbook whose sheets were copied from a pasted table:
    the edit turn's material has no `paste1` (the paste was in an earlier
    turn, and the engine gathers pastes at acceptance of a create), but
    the parent version's sheet IS that table, row for row — so it is put
    back under the same id, and the composer's fill copies it again after
    the model returns `rows_from` with `rows: []` (compose.body_json_for_
    prompt shows it the parent that way). A table already in the material
    (a fresh paste in the edit turn) wins; a generated sheet needs nothing —
    its recipe is in the parent and code regenerates it."""
    body = getattr(parent, "body", None)
    sheets = list(getattr(body, "sheets", None) or [])
    have = {t.id for t in material.tables}
    for sheet in sheets:
        source = getattr(sheet, "rows_from", None)
        if not source or source in have or not sheet.rows:
            continue
        material.tables.append(C.DataTable(id=source, title=sheet.name, columns=[c.name for c in sheet.columns], rows=[list(r) for r in sheet.rows]))
        have.add(source)


# --- AS3 integration BEGIN: requested styling on a NEW file ---
#: A style phrase the parser could not read goes to ONE small JSON call
#: (style.extract_patch_llm: thinking off, 300 tokens) under this deadline.
_STYLE_LLM_TIMEOUT_S = {"fast": 5.0, "think": 8.0, "max": 8.0}


async def compose_for_pipeline(ctx: "pipeline.ComposeContext"):
    """Installed on the pipeline at startup: the composer (below), then —
    for a create — the styling the request asked for merged into the spec's
    `style` BY CODE (style.apply_request), so "a Word report with purple
    headings" or "an excel tracker with a yellow header row" is honoured in
    the file and not only in the checklist. An edit carries its styling as a
    planned `set_style` op, and a convert keeps the parent's style."""
    spec = await _compose_for_pipeline_inner(ctx)
    if ctx.operation != "create" or _style is None or not hasattr(_style, "apply_request"):
        return spec
    try:
        spec = await _apply_requested_style(ctx, spec)
    except Exception as exc:  # noqa: BLE001 — the file still renders in the house style
        log.info("artifact: requested style not applied: %s", type(exc).__name__)
        ctx.warn("the requested formatting could not be applied; the file uses the house style")
    return spec


async def _apply_requested_style(ctx: "pipeline.ComposeContext", spec: Any) -> Any:
    instruction = ctx.instruction or ""
    spec, _formats, notes, unparsed = await asyncio.to_thread(_style.apply_request, spec, instruction, formats=list(ctx.formats))
    if unparsed and hasattr(_style, "extract_patch_llm"):
        body = getattr(spec, "body", spec)
        # Names only (sheets and their columns, headings) — never cell values.
        parts: List[str] = []
        for sh in list(getattr(body, "sheets", None) or [])[:8]:
            parts.append(f"sheet {sh.name}: columns " + ", ".join(c.name for c in sh.columns[:40]))
        heads = [b.text for b in list(getattr(body, "blocks", None) or []) if getattr(b, "type", "") == "heading"][:20]
        if heads:
            parts.append("headings: " + "; ".join(heads))
        outline = "\n".join(parts)[:800]
        patch, more = await _style.extract_patch_llm(
            unparsed, str(ctx.kind), outline, effort=ctx.effort, timeout_s=_STYLE_LLM_TIMEOUT_S.get(ctx.effort, 5.0),
        )
        notes = list(notes) + list(more)
        if not patch.is_empty():
            body.style = _style.merge(getattr(body, "style", None), patch)
            _, norm_notes = await asyncio.to_thread(_style.normalize_spec_style, spec)
            notes.extend(norm_notes)
    for n in dict.fromkeys(str(n) for n in notes if n):
        if n == getattr(_style, "CSV_STYLE_SENTENCE", None):
            continue  # formats.decide already said where a CSV's styling went
        ctx.warn(n)
    if ctx.kind == "workbook":
        _house_sheet_style(spec, instruction, styled=bool(getattr(getattr(spec, "body", spec), "style", None)) and bool(_style.patch_fields(_style.parse_style_request(instruction, "workbook")[0])))
    return spec


_ASKS_HIGHLIGHT_RE = re.compile(r"highlight|colou?r|fill|shade|rang|रंग|કલર", re.I)
_ASKS_HEADER_LOOK_RE = re.compile(r"header|plain|minimal|simple|no colou?rs?|without colou?rs?|light|black and white|monochrome", re.I)


def _house_sheet_style(spec: Any, instruction: str, *, styled: bool) -> None:
    """The model's legacy SheetStyle, when the person did not ask for it
    (live 2026-09-15): a whole-column `highlight` it invented to approximate
    "Status red where Blocked" or "Vendor column bold" is dropped once code
    has applied the real request, and a light or absent header fill nobody
    asked for is the professional dark header."""
    for sh in list(getattr(getattr(spec, "body", spec), "sheets", None) or []):
        st = getattr(sh, "style", None)
        if st is None:
            continue
        upd: Dict[str, Any] = {}
        if st.highlight and (styled or not _ASKS_HIGHLIGHT_RE.search(instruction or "")):
            upd["highlight"] = []
        if st.header_fill != "dark" and not _ASKS_HEADER_LOOK_RE.search(instruction or ""):
            upd["header_fill"] = "dark"
        if upd:
            sh.style = st.model_copy(update=upd)
# --- AS3 integration END ---


async def _compose_for_pipeline_inner(ctx: "pipeline.ComposeContext"):
    """Installed on the pipeline at startup: the composer the runner calls
    from the compose stage — in THIS process, under the job's lease, from
    the material persisted at acceptance (so a requeued job sees the same
    evidence). Announces the composer-owned stages on the way."""
    material = _material_from_dict(ctx.material)
    material.instruction = ctx.instruction
    reason = str((getattr(ctx, "job", None) or {}).get("format_reason") or "")
    if ctx.operation == "edit" and reason.startswith(EDIT_REASON):
        payload = await _load_payload(ctx, "edit")
        if payload is None:
            # The payload never reached material.json (a restart between
            # acceptance and the attach, or a drain that won the race).
            # Rebuilt from what the job row carries — never a whole-document
            # rewrite, which would retype a restore or an untouched section.
            payload = await _rebuild_edit_payload(ctx, reason)
        if payload is not None:
            return await _compose_edit(ctx, payload, material)
        ctx.warn("the planned change was not found, so the edit was made by rewriting the document")
    if ctx.operation == "create" and reason.startswith(IMPORT_REASON):
        payload = await _load_payload(ctx, "import")
        if payload is not None and isinstance(payload.get("spec"), dict):
            from ..artifacts import spec as S

            await ctx.progress_stage("intent", "done", f"{_KIND_WORDS.get(ctx.kind, ctx.kind)} · {', '.join(_FORMAT_WORDS.get(f, f) for f in ctx.formats)}")
            await ctx.progress_stage("gather", "done", "the answer, converted as written")
            await ctx.progress_stage("outline", "skipped", "a conversion keeps the structure")
            for n in payload.get("notes") or []:
                ctx.warn(str(n))
            raw = dict(payload["spec"])
            if "kind" not in raw:
                # AS3 integration: md_import/docx_to_document return the
                # DocumentSpec BODY; S.load reads the envelope. Every export
                # of an answer failed "The content could not be written"
                # with the real composer (live run 2026-09-15).
                raw = {"kind": "document", "document": raw}
            return await _post_process(S.load(raw), material.tables, ctx.warn, ctx.instruction)
    if ctx.operation == "edit" and ctx.parent_spec is not None:
        _tables_from_parent(material, ctx.parent_spec)
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
    # B1: the figures warning is held until the typed aggregates have been
    # replaced by computed ones (derived.enforce, in _post_process) — a
    # warning about 63,668 that is no longer in the file would be false.
    figures_warning = next((w for w in result.warnings if str(w).startswith(C.FIGURES_WARNING)), None)
    typed_notes = [w for w in result.warnings if _TYPED_ROWS_NOTE_RE.match(str(w))]
    for w in result.warnings:
        if w is not figures_warning and w not in typed_notes:
            ctx.warn(w)
    if result.corrections > 1:
        ctx.warn(f"{result.corrections} correction passes were made")
    kept = int((result.transform or {}).get("kept_original") or 0)
    if kept:
        ctx.warn(f"{kept} rewritten cell{'s' if kept != 1 else ''} kept the original wording (a timestamp, a quoted phrase or a figure would have changed)")
    spec = result.spec
    derived_report: Dict[str, Any] = {}
    if (material.tables and (_chart_data is not None or _style is not None)) or _has_charts(result.spec):
        # AS3 integration: a chart is resolved even with no table in the
        # material — a binding to nothing becomes a note, never a chart
        # with numbers the model wrote.
        spec = await _post_process(result.spec, material.tables, ctx.warn, ctx.instruction, derived_report=derived_report,
                                   parent=ctx.parent_spec if ctx.operation == "edit" else None)
    transform = dict(result.transform or {})
    gone = set(derived_report.get("computed") or []) | set(derived_report.get("dropped") or [])
    for w in typed_notes:
        m = _TYPED_ROWS_NOTE_RE.match(str(w))
        if m and m.group(1) not in gone:
            ctx.warn(w)
    if figures_warning is not None:
        if derived_report.get("changed"):
            figures = await asyncio.to_thread(_figures_still_typed, result.spec, spec, C.material_text(req))
            if figures:
                ctx.warn(C.FIGURES_WARNING + ", ".join(figures[:8]) + (" …" if len(figures) > 8 else ""))
        else:
            ctx.warn(figures_warning)
    if derived_report.get("changed") and transform.get("typed_sheets"):
        body = getattr(spec, "body", None)
        typed = [sh for sh in list(getattr(body, "sheets", None) or []) if sh.name not in gone and not sh.rows_are_code_computed and sh.rows]
        if typed:
            transform["typed_rows"] = sum(len(sh.rows) for sh in typed)
            transform["typed_sheets"] = [sh.name for sh in typed]
        else:
            transform.pop("typed_rows", None)
            transform.pop("typed_sheets", None)
    if transform and hasattr(ctx, "record_transform"):
        ctx.record_transform(transform)
    return spec


#: compose's note on a sheet the model typed beside a copied one; stale once
#: derived.enforce computed that sheet's figures or left it out.
_TYPED_ROWS_NOTE_RE = re.compile(r"^sheet '(.+)': [\d,]+ rows? (?:were|was) typed by the model")


def _figures_still_typed(before: Any, after: Any, material_text: str) -> List[str]:
    """The unsupported figures of the composed draft that are still in the
    file once typed aggregates were computed or left out (B1). A figure
    code computed is not re-flagged: only the draft's own list is kept."""
    from ..artifacts import spec as S

    had = set(S.unsupported_figures(before, material_text))
    return [f for f in S.unsupported_figures(after, material_text) if f in had]


def _has_charts(spec: Any) -> bool:
    try:
        from ..artifacts import chart_spec as _CS

        return any(True for _ in _CS.iter_chart_slots(spec))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- the turn --


def _format_list(files: Sequence[T.FileRef]) -> str:
    """"Excel, CSV, Word and PDF" — one word per distinct format, in the
    order the files were made."""
    words = list(dict.fromkeys(_FORMAT_WORDS.get(f.format, f.format.upper()) for f in files))
    if not words:
        return "the file"
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + " and " + words[-1]


def _count_word(n: int, noun: str) -> str:
    return f"{_COUNT_WORDS.get(n, f'{n:,}')} {noun}{'s' if n != 1 else ''}"


def _or_list(words: Sequence[str]) -> str:
    """"PowerPoint or PDF", "Excel, CSV, Word or PDF" — the Oxford join
    `_format_list` already makes for "and". Until 2026-09-16 the conversion
    refusal joined four names with " or " three times over."""
    items = [str(w) for w in words if str(w).strip()]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " or " + items[-1]


#: "convert it to X", "make it a X", "give me this as an X", "save it as
#: X": the turn asks for the SAME file in another form. The reference has
#: to be there — without it the words are a request for a NEW file, which
#: the create path answers as it always has.
_CONVERSION_ASK_RE = re.compile(
    r"\b(?:convert|turn|change|save|export|render|give\s+me|make|need|want)\b[^.!?\n]{0,30}?"
    r"\b(?:it|this|that|the\s+(?:deck|presentation|slides?|report|document|doc|file|pdf|docx|pptx|xlsx|workbook|spreadsheet|sheet|one))\b"
    r"[^.!?\n]{0,25}?\b(?:to|into|as|in)\b"
    r"|\b(?:convert|turn)\b[^.!?\n]{0,40}?\b(?:to|into)\b",
    re.I,
)


def _refuse_unmakeable_conversion(instruction: str, intent: ArtifactIntent, candidates: Sequence[dict]) -> str:
    """The sentence for "convert it to <something we do not make>", or "" .

    Two shapes reach here, both with NO makeable format named: a format this
    platform does not make at all (Google Slides, .txt, LaTeX), and a chart
    image the artifact's kind cannot carry (an SVG of a deck). Both used to
    fall through `if not ok:` into a fresh create.

    The kind is the NEWEST artifact's, which is what "it" means in a
    conversion follow-up; the sentence names no title, so picking the wrong
    one of two files can only make the offer slightly wrong, never claim a
    change that did not happen.
    """
    if intent.formats or not candidates:
        return ""
    if not _CONVERSION_ASK_RE.search(instruction or ""):
        return ""
    kind = str(candidates[0].get("kind") or "document")
    names = F.unmakeable_names(instruction)
    if names:
        return (f"I don't make {_and_list_words(names)} files. I can make {_a_kind_word(kind)} as "
                f"{_conversion_offer(kind)}.")
    images = [f for f in F.named_image_formats(instruction) if f not in T.FORMATS_FOR_KIND.get(kind, ())]
    if images:
        what = _or_list([_FORMAT_WORDS.get(f, f.upper()) for f in images])
        return (f"A {_KIND_WORDS.get(kind, kind)} cannot be converted to {what}. "
                f"I can make it as {_conversion_offer(kind)}.")
    return ""


def _and_list_words(words: Sequence[str]) -> str:
    items = [str(w) for w in words if str(w).strip()]
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


def _a_kind_word(kind: str) -> str:
    word = _KIND_WORDS.get(kind, kind)
    return f"an {word}" if word[:1].lower() in "aeiou" else f"a {word}"


def _cannot_clauses(instruction: str, *, all_warnings: Sequence[str] = ()) -> List[str]:
    """The parts of this request the platform cannot do, as clauses for the
    sentence. Measured 2026-09-16 (I3-I9): "Make a fillable PDF form",
    "Build an interactive dashboard I can filter", "with tracked changes
    turned on" and "Make a PDF and print two copies" each ended on a plain
    "Created …" with the impossible half never mentioned."""
    try:
        from ..artifacts import requirements as R

        said = {str(w) for w in all_warnings}
        return [c for c in R.unsupported_asks(instruction or "") if c not in said]
    except Exception as exc:  # noqa: BLE001 — the file still gets its sentence
        log.info("artifact: capability clauses skipped: %s", type(exc).__name__)
        return []


def _conversion_offer(kind: str) -> str:
    """The formats a conversion of this kind CAN produce, said to a person.
    The chart images are left out: "I can make it as PowerPoint or PDF or
    png" is true and useless — a PNG of a deck is not what anyone who asked
    to convert a deck means (measured 2026-09-16, A1)."""
    return _or_list([_FORMAT_WORDS.get(f, f) for f in T.FORMATS_FOR_KIND.get(kind, ()) if f not in T.IMAGE_FORMATS])


def _sentence(ref: T.ArtifactRef, operation: str, warnings: Sequence[str], *, transform: Optional[Dict[str, Any]] = None,
              dataset: bool = False, data_only_note: str = "", instruction: str = "", converted_to: Sequence[str] = ()) -> str:
    """The one line the person reads (CONTRACT-2 §7). "Updated" ONLY when
    the operation is an edit; "Converted" for a conversion; otherwise a
    create — of a pasted table ("Done — I preserved 34 audit rows and
    created four files. …"), of a dataset ("Created the CSV dataset with
    500 validated records."), of several files ("Created **X** in Excel,
    CSV, Word and PDF."), or of one ("Created **X** as PDF."). The
    data-only clause follows when a CSV was asked for with styling."""
    fmts = _format_list(ref.files)
    what = ref.title or _KIND_WORDS.get(ref.kind, "document")
    t = transform or {}
    if operation == "edit":
        line = f"Updated **{what}** as {fmts}."
    elif operation == "convert":
        if converted_to:
            new = [f for f in ref.files if f.format in converted_to]
            kept = [f for f in ref.files if f.format not in converted_to]
            line = f"Converted **{what}** to {_format_list(new) if new else fmts}." + (f" The {_format_list(kept)} {'is' if len({f.format for f in kept}) == 1 else 'are'} kept." if kept else "")
        else:
            line = f"Converted **{what}** to {fmts}."
    elif t.get("rows"):
        noun = "audit rows" if re.search(r"\baudit", instruction or what, re.I) else "rows"
        line = f"Done — I preserved {int(t['rows']):,} {noun} and created {_count_word(len(ref.files), 'file')}."
        clauses: List[str] = []
        if t.get("blanks"):
            clauses.append(f"{int(t['blanks']):,} blank source field{'s stay' if int(t['blanks']) != 1 else ' stays'} blank")
        if t.get("forward_filled"):
            label = str(t.get("forward_filled_column") or "grouping").strip().lower()
            n = int(t["forward_filled"])
            clauses.append(f"{n:,} {label} name{'s were' if n != 1 else ' was'} filled from the row above")
        if t.get("rewritten") or t.get("rewrite_columns"):
            cols = [str(c) for c in (t.get("rewrite_columns") or [])]
            subject = "comments" if not cols or any("comment" in c.lower() for c in cols) else f"the {cols[0]} column"
            verb = "were" if subject == "comments" else "was"
            clauses.append(f"{subject} {verb} rewritten for clarity without changing the findings")
        if t.get("typed_rows"):
            # Rows the MODEL wrote beside the preserved ones — a summary
            # sheet, or a sheet an injected cell asked for (security
            # review 2026-09-12, #1): "preserved" must not cover them.
            n = int(t["typed_rows"])
            names = [str(x) for x in (t.get("typed_sheets") or [])]
            where = (" and ".join([", ".join(names[:-1]), names[-1]]) if len(names) > 1 else names[0]) if names else "another"
            clauses.append(f"{n:,} row{'s' if n != 1 else ''} on the {where} sheet{'s' if len(names) > 1 else ''} "
                           f"{'were' if n != 1 else 'was'} written by the model, not copied from the paste")
        if clauses:
            line += " " + "; ".join(clauses) + "."
    elif dataset and any(f.format == "csv" and f.rows for f in ref.files):
        rows = next(f.rows for f in ref.files if f.format == "csv" and f.rows)
        formats = [f.format for f in ref.files]
        if formats == ["csv"]:
            line = f"Created the CSV dataset with {int(rows):,} validated records."
        else:
            line = f"Created the dataset with {int(rows):,} validated records as {fmts}."
    elif len(ref.files) >= 2:
        line = f"Created **{what}** in {fmts}."
    else:
        line = f"Created **{what}** as {fmts}."
    if data_only_note and operation == "create":
        note = data_only_note.strip().rstrip(".")
        line += f" {note[0].upper()}{note[1:]}."
    line += _warning_clause([w for w in warnings if not _CELL_NOTE_RE.match(str(w))])
    return line


def _warning_clause(warnings: Sequence[str], *, limit: int = 2) -> str:
    """Up to `limit` warnings in full, then a COUNT of the rest. The slice
    this replaces truncated: four "column not found" warnings were printed
    as two and the person read two thirds of the truth, while the card's
    `warnings` array carried all four (measured 2026-09-16, J1)."""
    said = [str(w).strip() for w in warnings if str(w).strip()]
    if not said:
        return ""
    out = " " + " ".join(f"_{w.rstrip('.')}._" for w in said[:limit])
    if len(said) > limit:
        out += f" _…and {len(said) - limit} more — see the card._"
    return out


#: The notes material_in leaves when a file could not be opened at all.
_UNREADABLE_NOTE_RE = re.compile(
    r"^(?P<name>.+?)\s+(?:is not a readable document or table|could not be read)\b", re.IGNORECASE)
#: What the readers DO open (material_in._read_one_upload).
_READABLE_KINDS = "PDF, Word, Excel, CSV, Markdown and plain text"


def _unreadable_clause(note: str) -> str:
    """"I couldn't read resume.pages — I can read PDF, Word, …", or "" when
    the note is not a read failure."""
    m = _UNREADABLE_NOTE_RE.match(str(note or "").strip())
    if not m:
        return ""
    name = m.group("name").strip().strip('"')
    return f"I couldn't read {name} — I can read {_READABLE_KINDS}, so it was not used"


#: A per-cell or per-sheet note ("sheet 'Audit', column 'Comments': row 21:
#: kept the original …") belongs on the card's notes, not in the sentence
#: the person reads first — the 2026-09-12 screenshots showed two lines of
#: it before the cards.
_CELL_NOTE_RE = re.compile(r"^sheet '", re.IGNORECASE)


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
    intent_id: str = "",
    gathered: Any = None,
    artifact_id: Optional[str] = None,
) -> str:
    """The whole turn. Returns the sentence that was streamed.

    `intent_id` is the client's DURABLE id for this send (chat_requests):
    the acceptance is keyed on it, so a turn resumed after a restart — a new
    generation under the same intent — finds its job instead of minting a
    second artifact (review, 2026-09-11). Without one, the generation id.
    """
    effort = effort if effort in T.EFFORT_BUDGETS else "fast"
    instruction = _decision_text(intent.instruction or text)
    raw_text = intent.raw_text or text

    # 1. What exists already, for follow-ups. A create that the rules
    #    marked as a NEW artifact (CONTRACT-2 §5) never picks a parent,
    #    whatever the later sentences say.
    existing = await db.run_in_thread(adb.list_artifacts, int(user_id), conversation_id)
    candidates = [a for a in existing if (a.get("current") or {}).get("status") in ("completed", "completed_with_warnings")]
    # A CONVERSION whose target this platform cannot make is refused by
    # name, never answered with a second file. Measured 2026-09-16: "convert
    # it to a .txt file", "to SVG", "to a Google Slides file" and "an
    # editable Google Doc" each created another artifact under the SAME
    # title as the one the person pointed at, so nothing the person read
    # said the conversion had not happened.
    refusal = _refuse_unmakeable_conversion(instruction, intent, candidates)
    if refusal:
        await emit("token", {"text": refusal})
        await emit("meta", {"route": "artifact", "effort": effort})
        return refusal
    operation = intent.action if intent.action in ("edit", "convert") and not intent.new_artifact else "create"
    parent: Optional[Tuple[str, int]] = None
    parent_row: Optional[dict] = None
    if artifact_id and operation == "create" and not intent.new_artifact and any(str(c.get("id")) == str(artifact_id) for c in candidates):
        # The UI's "Edit with a prompt" names the artifact (owner-checked:
        # it is one of the caller's candidates).
        operation = "edit"
    if operation in ("edit", "convert"):
        parent_row, question = pick_artifact(candidates, intent, artifact_id=artifact_id, history=history, instruction=instruction)
        if question:
            await emit("token", {"text": question})
            await emit("meta", {"route": "artifact", "effort": effort})
            return question
        if parent_row is None:
            # Nothing to edit: the words were about a file, but there is none — make one.
            operation = "create"
        elif operation == "edit":
            return await _run_edit(
                text=text, history=history, emit=emit, intent=intent, parent_row=parent_row, candidates=candidates,
                conversation_id=conversation_id, user_id=int(user_id), generation_id=generation_id, effort=effort, mode=mode,
                intent_id=intent_id, instruction=instruction, raw_text=raw_text, gathered=gathered,
            )
        else:
            version = int(intent.version or parent_row.get("current_version") or 1)
            parent = (str(parent_row["id"]), version)

    # 2. Kind, formats, template. `data_only_note` is set when a CSV was
    #    asked for with styling words (formats.decide): the sentence says
    #    where the styling went.
    data_only_note = ""
    converted_to: List[str] = []
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
            what = _or_list([_FORMAT_WORDS.get(b, b) for b in bad]) or "that format"
            line = f"A {_KIND_WORDS.get(kind, kind)} cannot be converted to {what}. I can make it as {_conversion_offer(kind)}."
            await emit("token", {"text": line})
            await emit("meta", {"route": "artifact", "effort": effort})
            return line
        if not ok:
            # "Also give me this as Excel" after a deck: not a conversion the
            # deck can take, so a NEW file is built from the conversation —
            # and the sentence must SAY so. Measured 2026-09-16 (A10): the
            # second artifact came back under the same title as the deck,
            # reported by the plain create sentence, so the two were
            # indistinguishable in the card list.
            was = _KIND_WORDS.get(kind, kind)
            operation, parent, parent_row = "create", None, None
            decision = F.decide(instruction, explicit_only=intent.formats or None, chart_request=bool(intent.chart_request))
            kind, formats, template_id, reason, warnings = decision.kind, decision.formats, decision.template_id, f"new {decision.kind}: {decision.reason}", list(decision.warnings)
            warnings.append(f"that {was} can't become {_a_kind_word(kind)}, so this is a new file built from the conversation")
            data_only_note = str(getattr(decision, "data_only_note", "") or "")
        else:
            # AS3: a convert KEEPS the formats the artifact has and adds the new one.
            lineage = lineage_formats(await db.run_in_thread(adb.list_versions, str(parent_row["id"]), int(user_id)), kind)
            formats = list(dict.fromkeys([*lineage, *ok])) if not intent.version else ok
            converted_to = list(ok)
            template_id = str((parent_row.get("current") or {}).get("template_id") or "generic")
            reason = f"convert: {', '.join(ok)}"
            warnings = [f"{', '.join(bad)} cannot be produced for a {kind}"] if bad else []
    else:
        # The GATE decided whether this is a chart, on normalised text with
        # the negated clauses blanked; formats.py is told that verdict
        # instead of running its own smaller chart vocabulary over the
        # instruction a second time. Production 2026-09-16: "visualise this
        # table on pie chart" came back as a Word file AND a PDF because the
        # two readings disagreed.
        decision = F.decide(instruction, explicit_only=intent.formats or None, chart_request=bool(intent.chart_request))
        kind, formats, template_id, reason, warnings = decision.kind, decision.formats, decision.template_id, decision.reason, list(decision.warnings)
        data_only_note = str(getattr(decision, "data_only_note", "") or "")

    # 3. Material.
    material = C.Material(instruction=instruction, history_text=C.material_from_history(history))
    material.notes.append(f"Today is {_dt.date.today().strftime('%d %B %Y')}.")
    material.notes.append("Mode: Salesforce workspace" if mode == "salesforce" else "Mode: assistant")
    import_payload: Optional[dict] = None
    if intent.action == "export":
        material.previous_answer = _previous_answer(history)
        material.notes.append("Turn the previous answer into the file faithfully; do not add claims it did not make.")
        answer_md = str(getattr(gathered, "previous_answer_md", "") or "") if gathered is not None else ""
        if answer_md:
            # AS3 integration: the gathered SUBSTANTIAL answer, never a
            # "You're welcome!" that came after it (the chat route used to
            # slice the history for this; with `gathered` it no longer does).
            material.previous_answer = answer_md
        if answer_md and kind == "document" and _md_import is not None and hasattr(_md_import, "markdown_to_document"):
            # AS3 (a): the export is the answer as written, imported by code —
            # never a model retype of a long answer.
            try:
                doc, notes = await asyncio.to_thread(_md_import.markdown_to_document, answer_md, title_hint="")
                import_payload = {"spec": doc.model_dump(mode="json", by_alias=True, exclude_none=True), "notes": list(notes or [])}
                reason = f"{IMPORT_REASON}: previous answer · {reason}"
            except Exception as exc:  # noqa: BLE001 — the composer path still works
                log.info("artifact: markdown import failed: %s", type(exc).__name__)
    if gathered is not None and import_payload is None and kind == "document" and getattr(intent, "target", "") == "upload" and _md_import is not None:
        docs = [d for d in (getattr(gathered, "upload_docs", None) or []) if isinstance(d, dict) and d.get("kind") in ("docx", "md", "txt")]
        if len(docs) == 1 and hasattr(docs[0].get("spec_or_text"), "model_dump"):
            import_payload = {"spec": docs[0]["spec_or_text"].model_dump(mode="json", by_alias=True, exclude_none=True), "notes": []}
            reason = f"{IMPORT_REASON}: upload · {reason}"
    if gathered is not None:
        for attr in ("upload_tables", "answer_tables", "prompt_tables"):
            material.tables.extend(list(getattr(gathered, attr, []) or []))
        if getattr(gathered, "uploads_text", ""):
            material.uploads_text = str(gathered.uploads_text)
        # What the reader could NOT read. `GatheredInput.notes` had exactly
        # one consumer — `to_material_dict`, which persists it into
        # material.json where nobody reads it — so an attachment this
        # platform cannot open (resume.pages, an .exe, a corrupt .xlsx) was
        # composed around in silence and the turn still ended on a plain
        # "Created …" (measured 2026-09-16, D1). The notes now reach the
        # composer, and the READ FAILURES reach the sentence.
        notes = [str(n) for n in (getattr(gathered, "notes", None) or []) if str(n).strip()]
        material.notes.extend(notes)
        for note in notes:
            clause = _unreadable_clause(note)
            if clause and clause not in warnings:
                warnings.append(clause)
    transform: Dict[str, Any] = {}
    if operation == "create":
        # The pasted table(s), parsed from the ORIGINAL text — never from
        # the flattened instruction (CONTRACT-2 §6) — and the uploads.
        pasted, transform, table_notes = await asyncio.to_thread(_pasted_tables, raw_text, history)
        if transform.get("truncated"):
            total = int(transform.get("total_rows") or 0)
            line = (f"The pasted table has {total:,} rows; the most I can take from one message is {tables.MAX_PASTE_ROWS:,} "
                    f"rows or {tables.MAX_PASTE_BYTES // (1024 * 1024)} MB. Attach it as a file instead.")
            await emit("token", {"text": line})
            await emit("meta", {"route": "artifact", "effort": effort})
            return line
        material.tables.extend(pasted)
        material.notes.extend(table_notes)
        if kind == "workbook" and not pasted and gathered is None:
            try:
                material.tables.extend(await db.run_in_thread(_upload_tables, conversation_id))
            except Exception as exc:  # noqa: BLE001 — an upload is an enhancement to the material
                log.info("artifact: upload tables skipped: %s", type(exc).__name__)
        material.transform = {k: v for k, v in transform.items() if k not in ("warnings", "truncated", "total_rows")}
        if intent.row_count:
            material.row_count = int(intent.row_count)
            material.notes.append(_ROW_COUNT_NOTE.format(n=int(intent.row_count)))
    if operation == "create" and mode == "salesforce" and _DATA_SHAPED_RE.search(instruction):
        await emit("status", {"text": "Querying Salesforce data…"})
        sf_tables, sf_sources, sf_notes = await _salesforce_table(instruction, history)
        material.tables.extend(sf_tables)
        material.sources.extend(sf_sources)
        material.notes.extend(sf_notes)
    if operation == "create" and mode == "assistant" and web_allowed:
        await emit("status", {"text": "Checking whether current sources are needed…"})
        material.sources.extend(await _web_sources(instruction, history, effort=effort, user_id=int(user_id), conversation_id=conversation_id, emit=emit))
    if not material.sources and not material.tables:
        material.notes.append("No external sources were gathered; write from the conversation and say where something is an assumption.")
    if import_payload is not None and operation != "create":
        import_payload = None

    # 4. Accept — persisted before any model call, keyed on the send's
    #    durable identity plus what it targets, so the same send answers
    #    with the same job and two different edits never share a key.
    key_seed = (intent_id or generation_id or "") + (f":{parent[0]}:v{parent[1]}" if parent else "")
    try:
        job = await db.run_in_thread(
            pipeline.accept,
            user_id=int(user_id), conversation_id=conversation_id, generation_id=generation_id,
            operation=operation, instruction=instruction, kind=kind, formats=formats, format_reason=reason,
            effort=effort, mode=mode, template_id=template_id, parent=parent,
            requested_formats=intent.formats or None, material=_material_dict(material),
            title=str(parent_row.get("title") or "") if parent_row else "",
            idempotency_key=pipeline.idempotency_key(int(user_id), conversation_id, key_seed, operation, instruction) if key_seed else "",
        )
    except pipeline.ArtifactRefused as exc:
        line = str(exc) or pipeline.safe_error(getattr(exc, "category", "invalid_request"))
        await emit("token", {"text": line})
        await emit("meta", {"route": "artifact", "effort": effort})
        return line
    if import_payload is not None and job.get("created"):
        await db.run_in_thread(_attach_payload, int(user_id), str(job["artifact_id"]), int(job["version"]), "import", import_payload)

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
    all_warnings = list(warnings) + [w for w in ref.warnings if w not in warnings] + _cannot_clauses(instruction, all_warnings=warnings)
    ref.warnings = all_warnings
    status = str(row.get("status") or "")
    if status in ("completed", "completed_with_warnings"):
        report = dict(material.transform) if operation == "create" else {}
        if report or (kind == "workbook" and operation == "create"):
            published = await _published_transform(int(user_id), str(row["artifact_id"]), int(row["version"]))
            if "rows" not in published:
                # The table was pasted but no sheet was built from it (the
                # model typed rows, or wrote a document): the sentence
                # must not claim rows were preserved.
                for key in ("rows", "columns", "blanks", "forward_filled", "forward_filled_column"):
                    report.pop(key, None)
            report.update(published)
        # A dataset: rows were asked for by count, or the published spec
        # generated them. A workbook the model typed from the conversation
        # is "Created **X** in …", however its template is named.
        dataset = kind == "workbook" and operation == "create" and bool(intent.row_count or report.get("generated"))
        line = _sentence(ref, operation, all_warnings, transform=report, dataset=dataset, data_only_note=data_only_note, instruction=instruction,
                         converted_to=converted_to)
    elif status == "cancelled":
        line = "The file was cancelled before it was finished."
    else:
        line = str(row.get("error") or "") or pipeline.safe_error(str(row.get("failure_category") or "renderer_failure"))
        line = f"I couldn't finish the file: {line}"
    await emit("token", {"text": line})
    await emit("meta", {"route": "artifact", "effort": effort, "artifacts": [ref.to_json()]})
    return line


async def _published_transform(user_id: int, artifact_id: str, version: int) -> Dict[str, Any]:
    """What the PUBLISHED spec says code did to its rows — the sheets
    copied from a pasted table (their rows and blank cells), the sheets
    generated, the columns rewritten — read from the version's spec.json
    the pipeline wrote. The composer's own counts do not survive the job
    boundary; the spec does, and it is the one that was rendered."""
    from ..artifacts import store

    try:
        spec = await db.run_in_thread(store.read_spec, store.version_dir(user_id, artifact_id, version))
    except Exception as exc:  # noqa: BLE001 — the sentence still has the parse report
        log.info("artifact: published spec unreadable for the sentence: %s", type(exc).__name__)
        return {}
    body = getattr(spec, "body", None)
    sheets = list(getattr(body, "sheets", None) or [])
    if not sheets:
        return {}
    out: Dict[str, Any] = {}
    copied = [sh for sh in sheets if getattr(sh, "rows_from", None)]
    if copied:
        out["rows"] = sum(len(sh.rows) for sh in copied)
        out["blanks"] = sum(1 for sh in copied for r in sh.rows for c in r if c is None or (isinstance(c, str) and not c.strip()))
        typed = [sh for sh in sheets if not getattr(sh, "rows_are_code_computed", getattr(sh, "rows_are_code_made", False)) and sh.rows]
        if typed:
            out["typed_rows"] = sum(len(sh.rows) for sh in typed)
            out["typed_sheets"] = [sh.name for sh in typed]
    generated = [sh for sh in sheets if getattr(sh, "generator", None) is not None]
    if generated:
        out["generated"] = sum(len(sh.rows) for sh in generated)
    columns = [r.column for sh in sheets for r in getattr(sh, "rewrite", [])]
    if columns:
        out["rewrite_columns"] = list(dict.fromkeys(columns))
        out["rewritten"] = True
    return out


# ------------------------------------------------------------ AS3 edits --
#
# PROMPT-DRIVEN EDITS (AS3 prompt-edits). An edit is planned and applied
# HERE, before acceptance: artifacts.edits turns the request into typed ops
# (0 model calls for style / orientation / title / undo / restore / rename
# column / add a blank column / delete rows by a named condition; otherwise
# ONE strict-JSON call over an outline), applies the deterministic ones to
# the parent spec, and says what it could not do. Nothing changed → one
# sentence, NO job and NO version (the version row is inserted at
# acceptance, so this is the only place a no-op can be caught). Otherwise
# the child spec rides into the job inside material.json under "edit"; the
# composer returns it, writing only the pending sections (≤ 3 scoped calls).
# A restore copies version N's spec.json and re-renders it (0 calls); undo
# is a restore of the current version's parent.

#: format_reason prefix of a job whose spec travels in material.json.
EDIT_REASON = "edit plan"
IMPORT_REASON = "import"
#: How long the composer waits for the engine to attach the payload after
#: acceptance (the maintenance drain may pick a queued row first).
_PAYLOAD_WAIT_S = 5.0

try:  # intent-capability track
    from ..artifacts import material_in as _material_in  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    _material_in = None  # type: ignore[assignment]
try:
    from ..artifacts import md_import as _md_import  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    _md_import = None  # type: ignore[assignment]
try:  # charts track
    from ..artifacts import chart_data as _chart_data  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    _chart_data = None  # type: ignore[assignment]
try:  # styling-engine track
    from ..artifacts import style as _style  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    _style = None  # type: ignore[assignment]

_ELEMENT_KIND = (
    (re.compile(r"\b(columns?|rows?|cells?|sheets?|tabs?|totals?)\b", re.I), "workbook"),
    (re.compile(r"\b(slides?|deck)\b", re.I), "presentation"),
    (re.compile(r"\b(sections?|headings?|paragraphs?|pages?|chapters?|appendix|landscape|portrait)\b", re.I), "document"),
)


def _last_artifact_turn(history: Sequence[dict]) -> Optional[str]:
    """The artifact id of the most recent assistant turn that carried an
    artifact card, when the history rows carry their meta."""
    for turn in reversed(list(history)):
        if str(turn.get("role")) != "assistant":
            continue
        meta = turn.get("meta") if isinstance(turn.get("meta"), dict) else {}
        refs = meta.get("artifacts") if isinstance(meta, dict) else None
        if isinstance(refs, list):
            for ref in refs:
                if isinstance(ref, dict) and T.is_artifact_id(str(ref.get("artifact_id") or "")):
                    return str(ref["artifact_id"])
    return None


def pick_artifact(candidates: Sequence[dict], intent: ArtifactIntent, *, artifact_id: Optional[str] = None,
                  history: Sequence[dict] = (), instruction: str = "") -> Tuple[Optional[dict], str]:
    """Which existing artifact a follow-up means (AS3 order): the UI's
    artifact_id (owner-checked: it must be one of the caller's candidates)
    > a title the words name > the artifact of the last artifact turn >
    element-word/kind affinity ("add a column" → a workbook) > the newest.
    Two equally named candidates → (None, question) and no job."""
    if not candidates:
        return None, ""
    hint_id = artifact_id or getattr(intent, "artifact_id_hint", None)
    if hint_id:
        for c in candidates:
            if str(c.get("id")) == str(hint_id):
                return c, ""
    last_id = _last_artifact_turn(history)
    hint = (intent.reference_hint or "").lower().strip()
    if hint and intent.reference != "latest":
        by_title = [c for c in candidates if hint in str(c.get("title") or "").lower()]
        if len(by_title) == 1:
            return by_title[0], ""
        if len(by_title) > 1:
            exact = [c for c in by_title if str(c.get("title") or "").lower().strip() == hint]
            if len(exact) == 1:
                return exact[0], ""
            if last_id and any(str(c.get("id")) == last_id for c in by_title):
                return next(c for c in by_title if str(c.get("id")) == last_id), ""
            names = " or ".join(f"**{c.get('title')}**" for c in by_title[:2])
            return None, f"Which one should I change — {names}?"
        legacy = _pick_artifact(candidates, intent)
        if legacy is not None and legacy is not candidates[0]:
            return legacy, ""
    if last_id:
        for c in candidates:
            if str(c.get("id")) == last_id:
                return c, ""
    words_kind = next((k for rx, k in _ELEMENT_KIND if rx.search(instruction or "")), None)
    if words_kind and candidates[0].get("kind") != words_kind:
        by_kind = [c for c in candidates if c.get("kind") == words_kind]
        if by_kind:
            return by_kind[0], ""
    return candidates[0], ""


def lineage_formats(versions: Sequence[dict], kind: str, *, upto: Optional[int] = None) -> List[str]:
    """The union of formats across an artifact's published versions, in
    first-seen order — what an edit inherits (a convert ADDS its format)."""
    allowed = T.FORMATS_FOR_KIND.get(kind, ())
    out: List[str] = []
    for v in sorted(versions, key=lambda r: int(r.get("version") or 0)):
        if upto is not None and int(v.get("version") or 0) > upto:
            continue
        if str(v.get("status") or "") not in ("completed", "completed_with_warnings"):
            continue
        fmts = [f.get("format") for f in (v.get("files") or []) if isinstance(f, dict) and f.get("format")] or list(v.get("formats") or [])
        for f in fmts:
            if f in allowed and f not in out:
                out.append(f)
    return out


def _attach_payload(user_id: int, artifact_id: str, version: int, key: str, payload: dict) -> None:
    """Blocking. Put the edit/import payload into the job's material.json
    (scratch: removed at publication)."""
    import os as _os

    from ..artifacts import store

    path = _os.path.join(store.ensure_workdir(int(user_id), artifact_id, int(version)), store.MATERIAL_NAME)
    data = store.read_json(path)
    data = dict(data) if isinstance(data, dict) else {}
    data[key] = payload
    store.write_json(path, data)


async def _load_payload(ctx: "pipeline.ComposeContext", key: str) -> Optional[dict]:
    import os as _os

    from ..artifacts import store

    path = _os.path.join(ctx.work_dir, store.MATERIAL_NAME)
    deadline = time.monotonic() + _PAYLOAD_WAIT_S
    while True:
        data = await asyncio.to_thread(store.read_json, path)
        if isinstance(data, dict) and isinstance(data.get(key), dict):
            return data[key]
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.1)


async def _rebuild_edit_payload(ctx: "pipeline.ComposeContext", reason: str) -> Optional[dict]:
    """The edit payload re-derived inside the job: a restore from the
    version its format_reason names; any other planned edit re-planned
    and re-applied over the job's parent spec (deterministic ops cost
    nothing; a model plan is the same one call the engine made)."""
    from ..artifacts import edits as E

    m = re.search(r"restore v(\d+)", reason or "")
    if m:
        return {"restore_version": int(m.group(1)), "pending": []}
    parent = ctx.parent_spec
    if parent is None:
        return None
    try:
        plan = await E.plan(ctx.instruction, parent, effort=ctx.effort)
        outcome = await asyncio.to_thread(E.apply, parent, plan, instruction=ctx.instruction)
    except Exception as exc:  # noqa: BLE001
        log.info("artifact edit: re-plan failed: %s", type(exc).__name__)
        return None
    if plan.summary == "undo" or outcome.restore_version:
        return None
    for n in outcome.not_applied[:2]:
        ctx.warn(f"not applied: {n.get('reason', '')}")
    return outcome.to_payload()


async def _post_process(spec: Any, tables_: Sequence[Any], warn: Callable[[str], None], instruction: str = "", *,
                        parent: Any = None, derived_report: Optional[Dict[str, Any]] = None, model_wrote: bool = True) -> Any:
    """AS3 (d): after any compose/apply, the CPU-bound post-processing in a
    thread — chart values from the bound tables, then (B1) every aggregate a
    sheet shows computed from those tables by code, then the style
    normalised — whichever of those modules this build has. `parent` is the
    version an edit started from: a sheet it kept unchanged is not re-checked.
    `model_wrote` is False for an edit whose ops were all deterministic: the
    figures in it are the person's own (an added row), not a model's."""
    if _chart_data is not None and hasattr(_chart_data, "repair_binding") and _has_charts(spec):
        # AS3 integration: the charts track's deterministic binding repair
        # (a skipped group_by named in the request, stray fields of another
        # chart type) BEFORE compute — built but never called before (live
        # 2026-09-15: "heatmap of ticket count by Status and Priority" drew
        # a note "A heatmap needs a column for its rows").
        try:
            # `parent is not None` is this function's own test for an edit:
            # a chart in a version the person already accepted is not
            # retyped by an instruction that is not about charts.
            spec = await asyncio.to_thread(_repair_bindings, spec, list(tables_), instruction, warn,
                                           parent is not None)
        except Exception as exc:  # noqa: BLE001 — resolve still runs on the model's binding
            log.info("artifact: chart binding repair skipped: %s", type(exc).__name__)
    chart_tables = list(tables_)
    if parent is not None and _chart_data is not None and hasattr(_chart_data, "tables_from_parent_charts") and _has_charts(spec):
        # THE EDIT TURN HAS NO PASTE (recheck BLOCKER, 2026-09-16). The chart
        # was computed in the CREATE turn from a pasted or uploaded table;
        # this turn is six words ("make it a bar chart instead") and
        # material.tables is empty, so resolve_spec below found no 'paste1'
        # and replaced the picture with a "the table is not available"
        # callout — measured with the parent untouched, on this branch and on
        # main (075ee8b), so every edit of a file holding a bound chart lost
        # the chart. The parent's own chart is rebuilt into that table, and
        # only when it provably reproduces the parent's numbers under an
        # unchanged binding (chart_data.tables_from_parent_charts).
        #
        # Kept OUT of `tables_`: derived.enforce below reads that list to
        # decide which figures a model wrote must be recomputed, and a table
        # rebuilt from a chart is not the material it would check against.
        try:
            recovered = await asyncio.to_thread(_chart_data.tables_from_parent_charts, spec, parent, list(tables_))
            chart_tables.extend(recovered or [])
        except Exception as exc:  # noqa: BLE001 — the chart refuses as before
            log.info("artifact: parent chart tables not rebuilt: %s", type(exc).__name__)
    if _chart_data is not None and hasattr(_chart_data, "resolve_spec"):
        try:
            spec, notes = await asyncio.to_thread(_chart_data.resolve_spec, spec, chart_tables)  # type: ignore[attr-defined]
            for n in notes or []:
                warn(str(n))
        except Exception as exc:  # noqa: BLE001 — never fails the job
            log.info("artifact: chart resolve skipped: %s", type(exc).__name__)
    if tables_ and model_wrote:
        from ..artifacts import derived as _derived

        # Not guarded like the steps above: a failure here must fail the
        # job, never publish a table of figures the model typed.
        spec, notes, report = await asyncio.to_thread(_derived.enforce, spec, list(tables_), parent=parent)
        for n in notes:
            warn(str(n))
        if derived_report is not None:
            derived_report.update(report)
            derived_report["changed"] = bool(notes)
    if _style is not None and hasattr(_style, "normalize_spec_style"):
        try:
            spec, notes = await asyncio.to_thread(functools.partial(_style.normalize_spec_style, add_totals=parent is None), spec)  # type: ignore[attr-defined]
            for n in notes or []:
                warn(str(n))
        except Exception as exc:  # noqa: BLE001
            log.info("artifact: style normalise skipped: %s", type(exc).__name__)
    return spec


def _repair_bindings(spec: Any, tables_: List[Any], instruction: str, warn: Callable[[str], None],
                     keep_accepted_type: bool = False) -> Any:
    from ..artifacts import chart_spec as CS

    body = getattr(spec, "body", None)
    if body is None:
        return spec
    for b in list(getattr(body, "blocks", None) or []):
        if getattr(b, "type", "") == "chart" and b.chart.data is not None and not b.chart.series:
            b.chart, notes = _chart_data.repair_binding(b.chart, tables_, instruction,
                                                        keep_accepted_type=keep_accepted_type)
            for n in notes:
                warn(str(n))
    for sl in list(getattr(body, "slides", None) or []):
        if getattr(sl, "chart", None) is not None and sl.chart.data is not None and not sl.chart.series:
            sl.chart, notes = _chart_data.repair_binding(sl.chart, tables_, instruction,
                                                         keep_accepted_type=keep_accepted_type)
            for n in notes:
                warn(str(n))
    for sh in list(getattr(body, "sheets", None) or []):
        if not getattr(sh, "charts", None):
            continue
        own = _chart_data._sheet_table(sh.model_dump(mode="python"))
        fixed = []
        for c in sh.charts:
            if isinstance(c, CS.Chart) and c.data is not None and not c.series:
                c, notes = _chart_data.repair_binding(c, [*tables_, own] if not c.data.table_id else tables_,
                                                     instruction, keep_accepted_type=keep_accepted_type)
                for n in notes:
                    warn(str(n))
            fixed.append(c)
        sh.charts = fixed
    return spec


async def _compose_edit(ctx: "pipeline.ComposeContext", payload: dict, material: C.Material):
    """The job side of a planned edit: the child spec from the payload, the
    pending sections written (scoped), the preservation re-checked."""
    from ..artifacts import edits as E
    from ..artifacts import spec as S
    from ..artifacts import store

    await ctx.progress_stage("intent", "done", f"edit · {', '.join(_FORMAT_WORDS.get(f, f) for f in ctx.formats)}")
    await ctx.progress_stage("gather", "done", "the current version")
    restore = payload.get("restore_version")
    if restore:
        await ctx.progress_stage("outline", "skipped", f"restoring v{int(restore)}")
        spec = await asyncio.to_thread(store.read_spec, store.version_dir(int(ctx.user_id), str(ctx.artifact_id), int(restore)))
        if spec is None:
            raise pipeline.StageFailure("invalid_request", f"Version {int(restore)} has no stored content to restore.")
        return spec
    await ctx.progress_stage("outline", "skipped", "an edit keeps the structure")
    try:
        spec = S.load(payload.get("spec") or {})
    except Exception as exc:  # noqa: BLE001
        raise pipeline.StageFailure("invalid_request", "The planned change could not be read back.") from exc
    pending = [p for p in payload.get("pending") or [] if isinstance(p, dict)]
    req = C.ComposeRequest(
        kind=ctx.kind, formats=ctx.formats, template_id=ctx.template_id, effort=ctx.effort, operation="edit",
        material=material, parent_spec=spec, instruction=ctx.instruction, date=_dt.date.today().strftime("%d %B %Y"),
    )
    if any(p.get("kind") == "regenerate" for p in pending):
        await ctx.progress(30.0, "rewriting")
        try:
            result = await C.compose(req, progress=ctx.progress)
        except C.ComposeError as exc:
            raise pipeline.StageFailure(exc.category, str(exc)) from exc
        for w in result.warnings:
            ctx.warn(w)
        spec = result.spec
        _refuse_unchanged_edit(ctx, payload, spec, [])
    else:
        writes = [p for p in pending if p.get("kind") in ("section", "slide")]
        if writes:
            async def writer(item: dict, current: Any, whole: Any) -> Any:
                await ctx.progress(30.0 + 50.0 * writes.index(item) / max(1, len(writes)), f"writing {item.get('heading') or 'the section'}")
                return await C.write_section(req, whole, item, current)

            before = spec
            spec, applied, not_applied = await E.resolve_pending(spec, writes, section_writer=writer)
            for n in not_applied:
                ctx.warn(f"not applied: {n['reason']}")
            if not any(p.get("mode") == "insert" for p in writes):
                broken = E.restore_pending_guard(before, spec, writes)
                if broken:
                    ctx.warn("a rewritten section touched other parts of the file; the earlier content was kept")
                    spec = before
            if not applied and not payload.get("applied_ops_deterministic"):
                ctx.warn("nothing in the file changed")
            unwritten = [str(n.get("reason") or "") for n in not_applied if n.get("reason")]
            if not any(p.get("mode") == "insert" for p in writes) and spec is before and applied:
                unwritten.append("the rewritten section changed other parts of the file, so it was not kept")
        else:
            unwritten = []
        _refuse_unchanged_edit(ctx, payload, spec, unwritten)
    if material.tables or (_chart_data is not None):
        spec = await _post_process(spec, material.tables, ctx.warn, ctx.instruction, parent=ctx.parent_spec,
                                   model_wrote=any(p.get("kind") in ("regenerate", "section", "slide") for p in pending))
    return spec


#: The row error of an edit job that was stopped because nothing in the
#: file would change. `_run_edit` reads it back to answer "I didn't change
#: X" instead of "I couldn't finish the change".
UNCHANGED_EDIT_MARK = "nothing in the file changed"


def _refuse_unchanged_edit(ctx: "pipeline.ComposeContext", payload: dict, spec: Any, reasons: Sequence[str]) -> None:
    """AS3 fix B4: a planned edit whose child is still the parent — every
    pending section write failed or came back unchanged, a rewrite that was
    reverted, a regenerate that returned the same content — must NOT publish
    a version. The version row already exists (acceptance inserts it), so the
    job fails here, before render and publication, and the version is never
    current. A format added for the formatting (a CSV lineage gaining XLSX)
    is a change even when the spec is equal."""
    from ..artifacts import edits as E

    parent = ctx.parent_spec
    if parent is None or payload.get("formats_added"):
        return
    try:
        same = E.canonical_json(spec) == E.canonical_json(parent)
    except Exception:  # noqa: BLE001 — a comparison that cannot run never blocks a change
        return
    if not same:
        return
    why = "; ".join(r for r in dict.fromkeys(str(x).strip() for x in reasons) if r)[:400]
    raise pipeline.StageFailure("model_failure", f"{UNCHANGED_EDIT_MARK}: {why}" if why else UNCHANGED_EDIT_MARK)


def _edit_sentence(title: str, version: int, changes: Sequence[str], not_applied: Sequence[dict], *, unmet: Sequence[str] = (),
                   data_only_note: str = "", restored: Optional[int] = None, warnings: Sequence[str] = ()) -> str:
    """"Updated **X** v3: headings dark blue (#1F3864); Owner column added."
    plus what was not applied (≤ 2) and the selfcheck's unmet items (≤ 3).
    Never "Updated" when nothing changed."""
    if restored is not None:
        line = f"Restored **{title}** to v{restored} — saved as v{version}."
    elif changes:
        said = "; ".join(changes[:6]) + (f"; and {len(changes) - 6} more" if len(changes) > 6 else "")
        line = f"Updated **{title}** v{version}: {said}."
    else:
        line = f"Saved **{title}** v{version}, but nothing in it changed."
    if not_applied:
        bits = [f"{_op_words(n.get('op', ''))} ({n.get('reason', '')})" for n in not_applied[:2]]
        line += " Not applied: " + "; ".join(bits) + "."
    if unmet:
        line += " Not confirmed in the file: " + "; ".join(str(u) for u in list(unmet)[:3]) + "."
    if data_only_note:
        note = data_only_note.strip().rstrip(".")
        line += f" {note[0].upper()}{note[1:]}."
    line += _warning_clause([w for w in warnings if not _CELL_NOTE_RE.match(str(w)) and not str(w).startswith("not applied:")])
    return line


def _op_words(op: str) -> str:
    return {
        "set_style": "the styling", "set_title": "the title", "set_subtitle": "the subtitle", "set_orientation": "the orientation",
        "set_page": "the page setup", "set_chart": "the chart change", "replace_section": "the section rewrite",
        "insert_section": "the new section", "delete_blocks": "the section delete", "rename_heading": "the heading rename",
        "add_column": "the new column", "rename_column": "the column rename", "delete_column": "the column delete",
        "reorder_columns": "the column order", "add_rows": "the new rows", "delete_rows": "the row delete",
        "update_cells": "the cell update", "set_slide_title": "the slide title", "replace_slide": "the slide rewrite",
        "insert_slide": "the new slide", "delete_slide": "the slide delete", "replace_section_section": "the section rewrite",
        "insert_section_section": "the new section", "replace_slide_slide": "the slide rewrite", "insert_slide_slide": "the new slide",
    }.get(op, op.replace("_", " "))


def _no_change_sentence(title: str, not_applied: Sequence[dict]) -> str:
    if not not_applied:
        return f"**{title}** already looks that way, so I didn't make a new version."
    bits = [f"{_op_words(n.get('op', ''))}: {n.get('reason', '')}" for n in not_applied[:2]]
    return f"I didn't change **{title}** — " + "; ".join(bits) + "."


async def _run_edit(
    *, text: str, history: Sequence[dict], emit: Emit, intent: ArtifactIntent, parent_row: dict, candidates: Sequence[dict],
    conversation_id: str, user_id: int, generation_id: str, effort: str, mode: str, intent_id: str, instruction: str,
    raw_text: str, gathered: Any = None,
) -> str:
    from ..artifacts import edits as E
    from ..artifacts import store

    started = time.perf_counter()
    kind = str(parent_row.get("kind") or "document")
    artifact_id = str(parent_row["id"])
    current = parent_row.get("current") or {}
    current_version = int(parent_row.get("current_version") or current.get("version") or 1)
    title = str(parent_row.get("title") or "")
    versions = await db.run_in_thread(adb.list_versions, artifact_id, int(user_id))
    published = {int(v["version"]): v for v in versions if str(v.get("status") or "") in ("completed", "completed_with_warnings")}

    async def say(line: str) -> str:
        await emit("token", {"text": line})
        await emit("meta", {"route": "artifact", "effort": effort})
        return line

    # 1. The plan.
    restore_n: Optional[int] = None
    # A restore is a byte copy ONLY when the restore words are the whole
    # request ("go back to version 1"). "use the version 1 numbers in the
    # Scope section" or "go back to v1 but keep the new title" names a
    # version AND asks for a change: that is an edit planned against
    # version N (the pre-AS3 meaning of a version-named edit), never a
    # restore that silently drops the rest of the request.
    base_version = current_version
    covered = E.restore_request(instruction)
    if covered is not None:
        restore_n = covered
    elif getattr(intent, "rule", "") == "restore-version" and intent.version:
        named = int(intent.version)
        if named not in published:
            return await say(f"**{title}** has no version {named} to start from.")
        base_version = named
    elif E.undo_signal(instruction):
        parent_version = current.get("parent_version")
        if not parent_version:
            return await say(f"**{title}** has no earlier version to go back to.")
        restore_n = int(parent_version)
    parent_spec = None
    plan = None
    if restore_n is not None:
        if restore_n not in published:
            other = [c for c in candidates if str(c.get("id")) != artifact_id]
            for c in other:
                vs = await db.run_in_thread(adb.list_versions, str(c["id"]), int(user_id))
                if any(int(v["version"]) == restore_n and v.get("status") in ("completed", "completed_with_warnings") for v in vs):
                    return await _run_edit(text=text, history=history, emit=emit, intent=intent, parent_row=c, candidates=[], conversation_id=conversation_id,
                                           user_id=user_id, generation_id=generation_id, effort=effort, mode=mode, intent_id=intent_id,
                                           instruction=instruction, raw_text=raw_text, gathered=gathered)
            return await say(f"**{title}** has no version {restore_n} to go back to.")
        if restore_n == current_version:
            return await say(f"**{title}** is already at v{restore_n}; nothing changed.")
        outcome = E.EditOutcome(spec=None, restore_version=restore_n, applied=[f"restored v{restore_n}"], applied_ops=["restore_version"])  # type: ignore[arg-type]
        planner = "deterministic"
        formats = lineage_formats(versions, kind, upto=current_version) or [f for f in (published[restore_n].get("formats") or []) if f]
    else:
        try:
            parent_spec = await db.run_in_thread(store.read_spec, store.version_dir(int(user_id), artifact_id, base_version))
        except Exception as exc:  # noqa: BLE001
            log.info("artifact edit: parent spec unreadable: %s", type(exc).__name__)
            parent_spec = None
        if parent_spec is None:
            return await say(f"I couldn't read the current version of **{title}** to change it.")
        await emit("status", {"text": "Planning the change…"})
        language = "en"
        if _lexicon_mod is not None and hasattr(_lexicon_mod, "language_of"):
            try:
                language = str(_lexicon_mod.language_of(instruction))
            except Exception:  # noqa: BLE001
                language = "en"
        plan = await E.plan(instruction, parent_spec, lineage=sorted(published), language=language, effort=effort)
        pasted, _transform, _notes = await asyncio.to_thread(_pasted_tables, raw_text, [])
        edit_tables = list(pasted)
        if gathered is not None:
            edit_tables.extend(list(getattr(gathered, "prompt_tables", []) or []))
        if plan.summary == "undo":  # an undo the intent did not see as one
            parent_version = current.get("parent_version")
            if not parent_version:
                return await say(f"**{title}** has no earlier version to go back to.")
            restore_n = int(parent_version)
            outcome = E.EditOutcome(spec=parent_spec, restore_version=restore_n, applied=[f"restored v{restore_n}"], applied_ops=["restore_version"])
        else:
            outcome = await asyncio.to_thread(E.apply, parent_spec, plan, tables=edit_tables, instruction=instruction)
            restore_n = outcome.restore_version
            if restore_n is not None:
                if restore_n not in published or restore_n == current_version:
                    return await say(f"**{title}** has no other version {restore_n} to go back to." if restore_n not in published else f"**{title}** is already at v{restore_n}; nothing changed.")
        planner = plan.planner
        formats = lineage_formats(versions, kind) or [f.get("format") for f in (current.get("files") or []) if f.get("format")] or list(T.FORMATS_FOR_KIND[kind][:1])

    metrics_labels = {"planner": planner}
    if outcome.question:
        _edit_metric("asked", **metrics_labels)
        return await say(outcome.question)

    data_only_note = ""
    style_asked = plan is not None and any(getattr(o, "op", "") == "set_style" for o in plan.ops)
    added_xlsx = False
    if kind == "workbook" and style_asked and "csv" in formats and "xlsx" not in formats:
        formats = formats + ["xlsx"]
        added_xlsx = True
        data_only_note = "The CSV carries the data only; the formatting is in the Excel file."

    if not outcome.changed and not added_xlsx:
        _edit_metric("noop", **metrics_labels)
        return await say(_no_change_sentence(title, outcome.not_applied))

    # 2. Accept — the child spec rides in the payload.
    material = C.Material(instruction=instruction, history_text=C.material_from_history(history))
    material.notes.append(f"Today is {_dt.date.today().strftime('%d %B %Y')}.")
    material.notes.append("Mode: Salesforce workspace" if mode == "salesforce" else "Mode: assistant")
    if gathered is not None:
        for attr in ("prompt_tables", "upload_tables", "answer_tables"):
            material.tables.extend(list(getattr(gathered, attr, []) or []))
    changes = list(outcome.applied)
    if added_xlsx:
        changes.append("an Excel copy added for the formatting")
    key_seed = (intent_id or generation_id or "") + f":{artifact_id}:v{base_version}"
    reason = f"{EDIT_REASON}: {planner}" + (f" · restore v{restore_n}" if restore_n else "")
    try:
        job = await db.run_in_thread(
            pipeline.accept,
            user_id=int(user_id), conversation_id=conversation_id, generation_id=generation_id,
            operation="edit", instruction=instruction, kind=kind, formats=formats, format_reason=reason,
            effort=effort, mode=mode, template_id=str(current.get("template_id") or "generic"), parent=(artifact_id, current_version if restore_n is not None else base_version),
            requested_formats=intent.formats or None, material=_material_dict(material), title=title,
            idempotency_key=pipeline.idempotency_key(int(user_id), conversation_id, key_seed, "edit", instruction) if key_seed.strip(":") else "",
        )
    except pipeline.ArtifactRefused as exc:
        return await say(str(exc) or pipeline.safe_error(getattr(exc, "category", "invalid_request")))
    if job.get("created"):
        if restore_n is not None:
            payload = {"restore_version": int(restore_n), "applied": changes, "not_applied": [], "pending": []}
        else:
            payload = outcome.to_payload()
            payload["applied"] = changes
            payload["applied_ops_deterministic"] = [o for o in outcome.applied_ops if o not in E.PENDING_OPS]
            payload["formats_added"] = bool(added_xlsx)
        payload["planner"] = planner
        payload["parent_version"] = current_version if restore_n is not None else base_version
        await db.run_in_thread(_attach_payload, int(user_id), str(job["artifact_id"]), int(job["version"]), "edit", payload)
    _edit_metric("accepted", **metrics_labels)
    log.info("artifact edit planned in %.2fs (%s, %d op(s), %d pending)", time.perf_counter() - started, planner,
             len(outcome.applied_ops), len(outcome.pending_sections))

    await emit("step", {"id": _STEP_IDS["intent"], "title": _TITLES["intent"], "status": "running", "detail": ""})
    await emit("status", {"text": "Applying the change…"})
    await pipeline.ensure_running(str(job["id"]))
    row = await _forward_progress(str(job["id"]), emit) or job

    version_row = None
    try:
        version_row = await db.run_in_thread(adb.get_version, str(row["artifact_id"]), int(row["version"]), int(user_id))
    except Exception:  # noqa: BLE001
        version_row = None
    ref = pipeline.ref_for(row, version_row)
    status = str(row.get("status") or "")
    if status in ("completed", "completed_with_warnings"):
        warnings = list(ref.warnings)
        failed_pending = [w for w in warnings if str(w).startswith("not applied:")]
        said = [c for c in changes if not any(_quoted(c) and _quoted(c) in w for w in failed_pending)]
        not_applied = list(outcome.not_applied) + [{"op": "", "reason": w[len("not applied: "):]} for w in failed_pending]
        if any(p.get("kind") == "regenerate" for p in outcome.pending_sections) and parent_spec is not None:
            try:
                child = await db.run_in_thread(store.read_spec, store.version_dir(int(user_id), str(row["artifact_id"]), int(row["version"])))
                changed = E.changed_sections(parent_spec, child) if child is not None else []
            except Exception:  # noqa: BLE001
                changed = []
            said = [c for c in said if c != "the document was rewritten as asked"]
            # A rewrite that changed no section is NOT a change to report:
            # appending the clause made `said` non-empty, so the sentence
            # read "Updated **X** v2: rewrote the file, but no section's
            # text changed" — a claim and its own contradiction in one line
            # (measured 2026-09-16, H1). With nothing appended,
            # `_edit_sentence` says "Saved **X** v2, but nothing in it
            # changed", which is what happened.
            if changed:
                said.append("rewrote " + ", ".join(changed[:5]) + (" and more" if len(changed) > 5 else ""))
        unmet = ((row.get("progress") or {}).get("selfcheck") or {}).get("unmet") or []
        line = _edit_sentence(ref.title or title, int(ref.version), said, not_applied, unmet=unmet, data_only_note=data_only_note,
                              restored=restore_n,
                              warnings=[w for w in warnings if w not in failed_pending and w != "nothing in the file changed"]
                              + _cannot_clauses(instruction, all_warnings=warnings))
    elif status == "cancelled":
        line = "The change was cancelled before it was finished."
    elif str(row.get("error") or "").startswith(UNCHANGED_EDIT_MARK):
        # The job stopped before publication because the file would not have
        # changed (a pending section write failed): no version was published.
        why = str(row.get("error") or "")[len(UNCHANGED_EDIT_MARK):].lstrip(": ").strip()
        line = f"I didn't change **{title}** — {why or 'the change could not be made'}, so no new version was saved."
        _edit_metric("unchanged", **metrics_labels)
    else:
        line = str(row.get("error") or "") or pipeline.safe_error(str(row.get("failure_category") or "renderer_failure"))
        line = f"I couldn't finish the change: {line}"
    await emit("token", {"text": line})
    await emit("meta", {"route": "artifact", "effort": effort, "artifacts": [ref.to_json()]})
    return line


def _quoted(text: str) -> str:
    m = re.search(r"“([^”]+)”", text or "")
    return m.group(1) if m else ""


def _edit_metric(result: str, **labels: str) -> None:
    try:
        from .. import metrics

        metrics.inc("artifact_edits_total", "artifact edit turns by outcome", result=result, **labels)
    except Exception:  # noqa: BLE001
        pass


try:
    from ..artifacts import lexicon as _lexicon_mod  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    _lexicon_mod = None  # type: ignore[assignment]


async def visual_reviewer(ctx: "pipeline.ComposeContext", spec, pages: List[bytes]):
    """Installed on the pipeline for Max effort: the vision-capable model
    looks at a few rendered pages; layout defects it reports become ONE
    correction pass through the composer's `revise`. None means the pages
    looked right."""
    verdict = await C.visual_review(pages, kind=spec.kind, title=spec.title)
    issues = [i for i in (verdict or {}).get("issues", []) if isinstance(i, dict)]
    if not issues:
        return None
    if ctx.operation == "edit" and str((getattr(ctx, "job", None) or {}).get("format_reason") or "").startswith(EDIT_REASON):
        # AS3 correction 6: a planned edit keeps every untouched part as it
        # was; a whole-document revise would rewrite them. Said, not done.
        ctx.warn("the page check found layout issues; an edit keeps the rest of the file as it was, so they were not changed")
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
    # The decision's view of the text, as the rules build it — never the
    # whole message (#8); decide_with_hook keeps the original in raw_text.
    return ArtifactIntent("create", rule="model", instruction=_decision_text(text))


__all__ = ["run_artifact_engine", "compose_for_pipeline", "visual_reviewer", "classify_hook", "pick_artifact", "lineage_formats"]
