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
_FORMAT_WORDS = {"pdf": "PDF", "docx": "Word", "pptx": "PowerPoint", "xlsx": "Excel", "csv": "CSV"}
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


async def compose_for_pipeline(ctx: "pipeline.ComposeContext"):
    """Installed on the pipeline at startup: the composer the runner calls
    from the compose stage — in THIS process, under the job's lease, from
    the material persisted at acceptance (so a requeued job sees the same
    evidence). Announces the composer-owned stages on the way."""
    material = _material_from_dict(ctx.material)
    material.instruction = ctx.instruction
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
    for w in result.warnings:
        ctx.warn(w)
    if result.corrections > 1:
        ctx.warn(f"{result.corrections} correction passes were made")
    kept = int((result.transform or {}).get("kept_original") or 0)
    if kept:
        ctx.warn(f"{kept} rewritten cell{'s' if kept != 1 else ''} kept the original wording (a timestamp, a quoted phrase or a figure would have changed)")
    if result.transform and hasattr(ctx, "record_transform"):
        ctx.record_transform(result.transform)
    return result.spec


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


def _sentence(ref: T.ArtifactRef, operation: str, warnings: Sequence[str], *, transform: Optional[Dict[str, Any]] = None,
              dataset: bool = False, data_only_note: str = "", instruction: str = "") -> str:
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
    said = [w for w in warnings if not _CELL_NOTE_RE.match(str(w))][:2]
    if said:
        line += " " + " ".join(f"_{w.rstrip('.')}._" for w in said)
    return line


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
    operation = intent.action if intent.action in ("edit", "convert") and not intent.new_artifact else "create"
    parent: Optional[Tuple[str, int]] = None
    parent_row: Optional[dict] = None
    if operation in ("edit", "convert"):
        parent_row = _pick_artifact(candidates, intent)
        if parent_row is None:
            # Nothing to edit: the words were about a file, but there is none — make one.
            operation = "create"
        else:
            version = int(intent.version or parent_row.get("current_version") or 1)
            parent = (str(parent_row["id"]), version)

    # 2. Kind, formats, template. `data_only_note` is set when a CSV was
    #    asked for with styling words (formats.decide): the sentence says
    #    where the styling went.
    data_only_note = ""
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
            line = f"A {_KIND_WORDS.get(kind, kind)} cannot be converted to {what}. I can make it as {' or '.join(_FORMAT_WORDS.get(f, f) for f in T.FORMATS_FOR_KIND.get(kind, ()))}."
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
            data_only_note = str(getattr(decision, "data_only_note", "") or "")
        else:
            formats, template_id = ok, str((parent_row.get("current") or {}).get("template_id") or "generic")
            reason = f"convert: {', '.join(ok)}"
            warnings = [f"{', '.join(bad)} cannot be produced for a {kind}"] if bad else []
    else:
        decision = F.decide(instruction, explicit_only=intent.formats or None)
        kind, formats, template_id, reason, warnings = decision.kind, decision.formats, decision.template_id, decision.reason, list(decision.warnings)
        data_only_note = str(getattr(decision, "data_only_note", "") or "")

    # 3. Material.
    material = C.Material(instruction=instruction, history_text=C.material_from_history(history))
    material.notes.append(f"Today is {_dt.date.today().strftime('%d %B %Y')}.")
    material.notes.append("Mode: Salesforce workspace" if mode == "salesforce" else "Mode: assistant")
    if intent.action == "export":
        material.previous_answer = _previous_answer(history)
        material.notes.append("Turn the previous answer into the file faithfully; do not add claims it did not make.")
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
        if kind == "workbook" and not pasted:
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
        line = _sentence(ref, operation, all_warnings, transform=report, dataset=dataset, data_only_note=data_only_note, instruction=instruction)
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
        typed = [sh for sh in sheets if not getattr(sh, "rows_are_code_made", False) and sh.rows]
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
    # The decision's view of the text, as the rules build it — never the
    # whole message (#8); decide_with_hook keeps the original in raw_text.
    return ArtifactIntent("create", rule="model", instruction=_decision_text(text))


__all__ = ["run_artifact_engine", "compose_for_pipeline", "visual_reviewer", "classify_hook"]
