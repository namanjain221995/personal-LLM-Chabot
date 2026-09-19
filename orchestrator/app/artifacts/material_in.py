"""Everything a file turn is made FROM, gathered by code before the job.

    intent (target previous_answer | upload | artifact | conversation)
      -> gather(history, uploads, …) -> GatheredInput
           previous_answer_md   the most recent SUBSTANTIAL answer, markdown intact
           upload_docs          attached DOCX/MD/TXT imported as specs, PDF as text
           upload_tables        attached XLSX (every sheet, cached VALUES) and CSV
           answer_tables        the answer's GFM tables (provenance: assistant_answer)
           prompt_tables        numbers typed in the request (charts track's parser)
           notes                every clip, cap and approximation, said once

WHY. The artifact engine used to see `_previous_answer(history)` — the LAST
assistant turn, whitespace-collapsed — so "thanks" → "you're welcome" →
"give it in docs" exported "you're welcome", and an upload reached the
composer only as clipped conversation text. The export importer
(md_import) needs the answer's markdown; charts need the real rows.

WHAT IS NEVER DONE. A formula in an uploaded workbook is never evaluated or
copied: openpyxl reads with `data_only=True` (the cached value Excel saved),
after the zip-bomb caps. A Word upload is read as text and tables only. A
PDF's tables come from a text-column heuristic and are labelled approximate.

THE EVENT LOOP. Parsing a 200,000-row workbook or a 30,000-character DOCX
is CPU work: every reader runs in `asyncio.to_thread` under one 15-second
deadline (the Fast pre-pass lesson: a CPU-bound step on the loop stalls
every stream and heartbeat in the process).

A DATASET UPLOADED IN AN EARLIER TURN IS STILL THE MATERIAL (2026-09-17).
Until this round the conversation's dataset workspace was read only when
`intent.target == "upload"`, and that target is set only when a file is
attached to THIS turn (`main.py` builds the upload formats from
`request.pdf_uploads` alone, and intent.py refuses an upload target without
them). So the owner's "Big report" over a CSV uploaded one turn earlier
composed with `tables=[]`: every chart the model bound to the filename
became a "the table … is not available" callout, and a PNG-only request
failed outright. `gather` now loads the conversation's ready CSV/XLSX
uploads whenever the turn has no attachment and the file is being made from
the conversation — every create except an export or a previous-answer
target, and every edit — inside the SAME budget (`MAX_UPLOAD_ROWS`, five
files, `GATHER_DEADLINE_S`). The most recent dataset, or the one the words
name, is read first. This is also where `engines/artifact._upload_tables`
went: that fallback ran only when `gathered is None`, which production
never passes, so it was dead code.
"""
from __future__ import annotations

import asyncio
import base64
import csv
import datetime as _dt
import io
import logging
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import intent as I
from . import md_import

log = logging.getLogger(__name__)

PREVIOUS_ANSWER_MAX_CHARS = 120_000
MAX_UPLOAD_ROWS = 200_000
GATHER_DEADLINE_S = 15.0
_MAX_UPLOADS = 5


@dataclass
class GatheredInput:
    previous_answer_md: str = ""
    previous_answer_turn_index: Optional[int] = None
    uploads_text: str = ""
    upload_docs: List[Dict[str, Any]] = field(default_factory=list)
    upload_tables: List[Any] = field(default_factory=list)
    answer_tables: List[Any] = field(default_factory=list)
    prompt_tables: List[Any] = field(default_factory=list)
    upload_names: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def tables(self) -> List[Any]:
        return [*self.upload_tables, *self.answer_tables, *self.prompt_tables]


def _data_table(**kw: Any) -> Any:
    from .compose import DataTable

    return DataTable(**kw)


# ------------------------------------------------------------- the answer --


def previous_answer(history: Sequence[dict]) -> Tuple[str, Optional[int], List[str]]:
    """(markdown, index, notes) of the most recent substantial answer."""
    idx = I.substantial_answer_index(history)
    if idx is None:
        return "", None, []
    text = I.turn_text(history[idx])
    notes: List[str] = []
    if len(text) > PREVIOUS_ANSWER_MAX_CHARS:
        notes.append(f"The previous answer is {len(text):,} characters; the first {PREVIOUS_ANSWER_MAX_CHARS:,} were used.")
        text = text[:PREVIOUS_ANSWER_MAX_CHARS]
    return text, idx, notes


def tables_from_markdown(md: str, *, id_prefix: str, source_id: str, start: int = 1) -> List[Any]:
    """Every GFM pipe table in `md` as a DataTable (ids <prefix>1, <prefix>2…)."""
    lines = (md or "").split("\n")
    out: List[Any] = []
    i = 0
    title = ""
    while i < len(lines):
        line = lines[i]
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
        if heading:
            title = heading.group(1).strip()
        if "|" in line and i + 1 < len(lines) and md_import._TABLE_SEP_RE.match(lines[i + 1]):
            header = [md_import._inline(c, []) for c in md_import._cells(line)]
            rows: List[List[Any]] = []
            i += 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                cells = [md_import._inline(c, []) for c in md_import._cells(lines[i])]
                cells = (cells + [""] * len(header))[: len(header)]
                rows.append([c if c != "" else None for c in cells])
                i += 1
            out.append(_data_table(id=f"{id_prefix}{start + len(out)}", title=(title or f"Table {start + len(out)}")[:120],
                                   columns=header, rows=rows, source_id=source_id))
            continue
        i += 1
    return out


# --------------------------------------------------------------- uploads --


def _cell(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    return str(v)


def read_xlsx(path: str, *, name: str, start: int = 1, row_budget: int = MAX_UPLOAD_ROWS) -> Tuple[List[Any], List[str]]:
    """Every sheet of a workbook as DataTables, CACHED VALUES only. Blocking."""
    from ..core import archive

    archive.check_zip_container(path, label="spreadsheet")
    from openpyxl import load_workbook

    notes: List[str] = []
    out: List[Any] = []
    wb = load_workbook(path, read_only=True, data_only=True)
    used = 0
    try:
        for ws in wb.worksheets:
            # A read-only sheet pads every row out to the DIMENSION the file
            # declares: a forged <dimension ref="A1:XFD200000"/> turned 20k
            # two-cell rows into 20k 16,384-cell rows (0.3 s -> 4.8 s; 200k
            # rows ~50 s of GIL in a thread the deadline cannot stop).
            # Resetting reads only the cells that exist (verifier 2026-09-15).
            try:
                ws.reset_dimensions()
            except AttributeError:
                pass
            header: Optional[List[str]] = None
            rows: List[List[Any]] = []
            clipped = False
            sheet_title = ""
            pending: Optional[Sequence[Any]] = None
            pending_data: Optional[Sequence[Any]] = None
            for raw in ws.iter_rows(values_only=True):
                if raw is None or all(v in (None, "") for v in raw):
                    continue
                filled = [i for i, v in enumerate(raw) if v not in (None, "")]
                if header is None:
                    if len(filled) == 1 and pending is None:
                        # Maybe a TITLE above the table (often a merged banner),
                        # maybe the header of a one-column sheet: the next row
                        # decides. Taken as the header, a banner cut every row
                        # to one column (verifier 2026-09-15).
                        pending = raw
                        continue
                    if pending is not None and len(filled) == 1:
                        raw, pending_data = pending, raw
                        filled = [i for i, v in enumerate(raw) if v not in (None, "")]
                    elif pending is not None:
                        sheet_title = str(pending[[i for i, v in enumerate(pending) if v not in (None, "")][0]])[:80]
                    pending = None
                    last = filled[-1]
                    header = [str(v) if v not in (None, "") else f"Column {i + 1}" for i, v in enumerate(raw[: last + 1])]
                    if pending_data is None:
                        continue
                    raw, pending_data = pending_data, None
                    filled = [i for i, v in enumerate(raw) if v not in (None, "")]
                if used >= row_budget:
                    clipped = True
                    break
                if filled and filled[-1] >= len(header):
                    # A row wider than the header keeps its cells.
                    header.extend(f"Column {i + 1}" for i in range(len(header), filled[-1] + 1))
                    for r in rows:
                        r.extend([None] * (len(header) - len(r)))
                rows.append([_cell(v) for v in (list(raw) + [None] * len(header))[: len(header)]])
                used += 1
            if header is None and pending is not None:
                # A sheet holding one filled cell: that cell is its header.
                header = [str(v) for v in pending if v not in (None, "")][:1]
            if header is None:
                continue
            if clipped:
                notes.append(f"{name}: rows past {row_budget:,} in total were not read.")
            out.append(_data_table(id=f"upload{start + len(out)}", title=f"{name} · {ws.title}{(' · ' + sheet_title) if sheet_title else ''}"[:120], columns=header, rows=rows,
                                   source_id="upload"))
            if clipped:
                break
    finally:
        wb.close()
    return out, notes


def read_csv_bytes(raw: bytes, *, name: str, table_id: str, row_budget: int = MAX_UPLOAD_ROWS) -> Tuple[Optional[Any], List[str]]:
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    header = next(reader, None)
    if not header:
        return None, []
    rows: List[List[Any]] = []
    notes: List[str] = []
    for record in reader:
        if not any(c.strip() for c in record):
            continue
        if len(rows) >= row_budget:
            notes.append(f"{name}: rows past {row_budget:,} were not read.")
            break
        rows.append([(c if c != "" else None) for c in (record + [""] * len(header))[: len(header)]])
    return _data_table(id=table_id, title=name[:120], columns=[str(h) for h in header], rows=rows, source_id="upload"), notes


_PDF_ROW_SPLIT = re.compile(r"\s{2,}|\t")


def pdf_tables_approximate(pages: Sequence[str], *, name: str, start: int = 1) -> List[Any]:
    """Tables from a PDF's TEXT LAYER by column alignment: 3+ consecutive
    lines that split into the same number (>= 3) of fields on runs of two or
    more spaces. Approximate by construction, and titled so."""
    out: List[Any] = []
    for p, page in enumerate(pages):
        run: List[List[str]] = []
        for line in list(page.split("\n")) + [""]:
            fields = [f.strip() for f in _PDF_ROW_SPLIT.split(line.strip()) if f.strip()]
            if len(fields) >= 3 and (not run or len(fields) == len(run[0])):
                run.append(fields)
                continue
            if len(run) >= 3:
                out.append(_data_table(id=f"upload{start + len(out)}", title=f"{name} · page {p + 1} (approximate)"[:120],
                                       columns=run[0], rows=[[c or None for c in r] for r in run[1:]], source_id="upload_pdf_approximate"))
            run = [fields] if len(fields) >= 3 else []
    return out


def _kind_of(name: str, raw: bytes) -> str:
    low = (name or "").lower()
    if raw.startswith(b"%PDF"):
        return "pdf"
    if raw.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names = set(zf.namelist())
        except zipfile.BadZipFile:
            names = set()
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        return "binary"
    if b"\x00" in raw[:8192]:
        return "binary"
    if low.endswith((".csv", ".tsv")):
        return "csv"
    if low.endswith((".md", ".markdown")):
        return "md"
    return "txt"


def _read_one_upload(name: str, raw: bytes, *, start: int, row_budget: int) -> Dict[str, Any]:
    """Blocking. One attached file → {kind, doc?, text, tables, notes, pages}."""
    kind = _kind_of(name, raw)
    res: Dict[str, Any] = {"name": name, "kind": kind, "doc": None, "text": "", "tables": [], "notes": [], "pages": 0}
    if kind in ("docx", "xlsx"):
        fd, path = tempfile.mkstemp(suffix="." + kind)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
            if kind == "docx":
                doc, notes = md_import.docx_to_document(path, title_hint=os.path.splitext(name)[0])
                res["doc"], res["notes"] = doc, notes
                from . import spec as S

                res["text"] = S.text_of(S.ArtifactSpec(kind="document", document=doc))
                res["tables"] = [
                    _data_table(id=f"upload{start + k}", title=f"{name} · table {k + 1}"[:120], columns=list(b.table.columns),
                                rows=[list(r) for r in b.table.rows], source_id="upload")
                    for k, b in enumerate(x for x in doc.blocks if isinstance(x, S.TableBlock))
                ]
            else:
                tables, notes = read_xlsx(path, name=name, start=start, row_budget=row_budget)
                res["tables"], res["notes"] = tables, notes
                res["text"] = "\n".join(f"{t.title}: {len(t.rows)} rows; columns {', '.join(map(str, t.columns))}" for t in tables)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    elif kind == "pdf":
        from ..core.pdf import extract_pdf_pages

        pages, total = extract_pdf_pages(base64.b64encode(raw).decode("ascii"))
        res["pages"] = total
        res["text"] = "\n\n".join(f"[Page {i + 1}]\n{t}" for i, t in enumerate(pages) if t.strip())
        res["tables"] = pdf_tables_approximate(pages, name=name, start=start)
        if res["tables"]:
            res["notes"].append(f"{name}: tables were read from the PDF's text by column alignment and may be approximate.")
    elif kind == "csv":
        table, notes = read_csv_bytes(raw, name=name, table_id=f"upload{start}", row_budget=row_budget)
        res["tables"] = [table] if table is not None else []
        res["notes"] = notes
        res["text"] = raw.decode("utf-8-sig", errors="replace")[:400_000]
    elif kind in ("md", "txt"):
        text = raw.decode("utf-8", errors="replace")[:400_000]
        res["text"] = text
        try:
            res["doc"], res["notes"] = md_import.markdown_to_document(text, title_hint=os.path.splitext(name)[0])
        except ValueError:
            res["doc"] = None
    else:
        res["notes"].append(f"{name} is not a readable document or table.")
    return res


def _decode_upload(item: Any) -> Tuple[str, Optional[bytes], Optional[Any]]:
    """(name, bytes or None, pre-extracted _Doc or None) for one resolved ref."""
    if isinstance(item, tuple) and len(item) == 2:
        name, b64 = item
        data = str(b64 or "")
        if data.startswith("data:"):
            data = data.split(",", 1)[-1]
        try:
            return str(name or "document"), base64.b64decode(data), None
        except Exception:  # noqa: BLE001
            return str(name or "document"), None, None
    name = getattr(item, "name", None) or "document"
    return str(name), None, item


# --------------------------------------------------------------- gather --


async def gather(
    *,
    history: Sequence[dict],
    pdf_uploads: Sequence[Any] = (),
    pdf_data: Optional[Tuple[str, str]] = None,
    user_id: Optional[int] = None,
    conversation_id: str = "",
    workspace: str = "",
    intent: Optional[I.ArtifactIntent] = None,
    text: str = "",
    save_documents: bool = True,
    deadline_s: float = GATHER_DEADLINE_S,
) -> GatheredInput:
    """Collect the turn's material. `pdf_uploads` are the resolved document
    references of THIS turn — (name, base64) pairs or pre-extracted
    documents — and `pdf_data` an inline (name, base64). Never raises: a
    reader that fails leaves a note. `user_id` scopes nothing extra here
    (the conversation id is the isolation boundary the document store uses)."""
    out = GatheredInput()
    # CPU work over an answer and a message of any size: off the event loop
    # like every reader below (verifier 2026-09-15).
    md, idx, notes = await asyncio.to_thread(previous_answer, history)
    out.previous_answer_md, out.previous_answer_turn_index = md, idx
    out.notes.extend(notes)
    if md:
        out.answer_tables = await asyncio.to_thread(tables_from_markdown, md, id_prefix="answer", source_id="assistant_answer")

    items = list(pdf_uploads or ())[:_MAX_UPLOADS]
    if pdf_data:
        items.append(pdf_data)
    budget = MAX_UPLOAD_ROWS
    read: List[Dict[str, Any]] = []
    try:
        async with asyncio.timeout(deadline_s):
            for item in items:
                name, raw, pre = _decode_upload(item)
                out.upload_names.append(name)
                if raw is None and pre is not None:
                    read.append({"name": name, "kind": "text", "doc": None, "text": getattr(pre, "full_text", "") or "",
                                 "tables": [], "notes": [], "pages": int(getattr(pre, "total", 0) or 0)})
                    continue
                if raw is None:
                    out.notes.append(f"{name} could not be read.")
                    continue
                start = len(out.upload_tables) + 1
                try:
                    res = await asyncio.to_thread(_read_one_upload, name, raw, start=start, row_budget=budget)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — one bad file is a note
                    log.info("material_in: %s unreadable: %s", name, type(exc).__name__)
                    out.notes.append(f"{name} could not be read ({type(exc).__name__}).")
                    continue
                read.append(res)
                out.upload_tables.extend(res["tables"])
                budget -= sum(len(t.rows) for t in res["tables"])
            if not items and conversation_id:
                if intent is not None and intent.target == "upload":
                    read.extend(await asyncio.to_thread(_earlier_documents, conversation_id))
                if workspace and (wants_conversation_datasets(intent) or (intent is not None and intent.target == "upload")):
                    more, more_notes = await asyncio.to_thread(_workspace_tables, workspace, conversation_id, len(out.upload_tables) + 1, budget, text)
                    out.upload_tables.extend(more)
                    out.notes.extend(more_notes)
    except TimeoutError:
        out.notes.append(f"Reading the attached files took longer than {deadline_s:.0f} seconds; what was read in time is used.")

    for res in read:
        out.notes.extend(res.get("notes") or [])
        entry = {"name": res["name"], "kind": res["kind"], "spec_or_text": res["doc"] if res.get("doc") is not None else res.get("text", "")}
        out.upload_docs.append(entry)
    out.uploads_text = "\n\n".join(f"Document: {r['name']}\n{r.get('text', '')}" for r in read if r.get("text"))

    if save_documents and conversation_id:
        for res in read:
            if res.get("kind") in ("text",) or not (res.get("text") or "").strip():
                continue
            try:
                from .. import db

                await db.run_in_thread(db.save_document, conversation_id, res["name"], res["text"], int(res.get("pages") or 0))
            except Exception as exc:  # noqa: BLE001 — memory is an enhancement
                log.info("material_in: document not saved: %s", type(exc).__name__)

    out.prompt_tables = await asyncio.to_thread(_prompt_tables, text)
    out.notes = list(dict.fromkeys(out.notes))
    return out


def _prompt_tables(text: str) -> List[Any]:
    """Numbers typed in the request, through the charts track's parser when
    it is merged; nothing otherwise (never a guess)."""
    try:
        from . import chart_data  # type: ignore[attr-defined]
    except ImportError:
        return []
    try:
        table = chart_data.parse_prompt_data(text or "")
    except Exception:  # noqa: BLE001
        return []
    if table is None:
        return []
    try:
        table.id = "prompt1"
    except Exception:  # noqa: BLE001
        pass
    return [table]


def _earlier_documents(conversation_id: str) -> List[Dict[str, Any]]:
    """Blocking. Documents attached in EARLIER turns, read in full from the
    document store (text only; a markdown-looking text is imported)."""
    from .. import db

    try:
        docs = db.get_documents(conversation_id)
    except Exception as exc:  # noqa: BLE001
        log.info("material_in: document store unavailable: %s", type(exc).__name__)
        return []
    out: List[Dict[str, Any]] = []
    for d in docs[-_MAX_UPLOADS:]:
        text = str(d.get("text") or "")
        name = str(d.get("filename") or "document")
        doc = None
        try:
            doc, _ = md_import.markdown_to_document(text, title_hint=os.path.splitext(name)[0])
        except ValueError:
            doc = None
        out.append({"name": name, "kind": "stored", "doc": doc, "text": text, "tables": [], "notes": [], "pages": int(d.get("total_pages") or 0)})
    return out


#: Dataset files this reader opens. `.tsv` reaches `read_csv_bytes`, whose
#: sniffer already reads tabs.
DATASET_EXTENSIONS = (".csv", ".tsv", ".xlsx")

#: `uploads.notes` of a row that is NOT a dataset: the document rail writes
#: "document" and video/api writes "video". The dataset rail writes the
#: archive notes it collected, or nothing at all.
_NOT_A_DATASET = frozenset({"document", "video"})


def wants_conversation_datasets(intent: Optional[I.ArtifactIntent]) -> bool:
    """Whether this turn's file may be made FROM a dataset the conversation
    already holds.

    Every create except an export (which is the previous ANSWER, converted as
    written) or a previous_answer target, and every edit — whatever
    `intent.target` says, because that target can only be "upload" when a
    file is attached to THIS turn. A convert re-renders a stored spec with no
    model call and needs no material.

    THE EXCEPTION IS A CONVERT WHOSE WORDS ASK FOR A CHART (2026-09-18). The
    rules read the owner's second reported sentence, "also i want Plots on
    this docs", as a conversion: measured on the integrated tree, `I.decide`
    with the report's card as the last turn answers action='convert',
    rule='convert-artifact-turn', formats=['docx'], chart_request=True. A plot
    has to be DRAWN FROM the data, so refusing the dataset to that turn left
    nothing to draw and the same document was published again, twice, with no
    plot in it. `engines/artifact` runs such a turn as an edit; this is the
    same judgement on the material side, and it has to be made here because
    `gather` runs before the parent artifact is picked.

    A CHART IS DRAWN FROM DATA WHATEVER THE TARGET (hotfix 1.1, 2026-09-19).
    The previous_answer early return ran BEFORE the chart check: when the
    report came back as a text answer, "also i want Plots on this docs" is
    create / previous_answer / chart_request, no CSV was read, and every
    chart in the file became "the table 'customers-100.csv' is not
    available" (live replay on main @ 4e7cf8e). A previous_answer target
    with no chart still reads nothing: that file is the answer as written.
    """
    if intent is None:
        return False
    action = str(getattr(intent, "action", "") or "")
    if action in ("create", "edit", "convert") and bool(getattr(intent, "chart_request", False)):
        return True
    if str(getattr(intent, "target", "") or "") == "previous_answer":
        return False
    if action not in ("create", "edit"):
        return False
    return True


def _names_file(text: str, filename: str) -> bool:
    """The request names this file — with or without its extension.

    A bare stem counts only when it LOOKS like a filename (it carries a
    digit, a dash or an underscore). The plain 4-character substring test
    read the ordinary English word in "please give Big report" as naming
    report.csv, so that file was read first and became upload1 — the table
    the composer and `add_chart` bind to first, and the one that takes the
    shared row budget (QA A-F8, 2026-09-18). "sales.csv", "q3-numbers" and
    "customers-100" still match."""
    low = " ".join((text or "").split()).casefold()
    if not low or not filename:
        return False
    name = filename.casefold()
    stem = os.path.splitext(name)[0]
    return bool(name in low or (len(stem) >= 4 and re.search(r"[0-9_-]", stem) and stem in low))


def dataset_uploads(conversation_id: str, text: str = "") -> List[Dict[str, Any]]:
    """Blocking. The conversation's dataset uploads, the one the words name
    first and otherwise the most recent first, capped at `_MAX_UPLOADS`."""
    from .. import db

    try:
        uploads = db.get_uploads(conversation_id)
    except Exception as exc:  # noqa: BLE001 — no uploads is the common case
        log.info("material_in: uploads unavailable: %s", type(exc).__name__)
        return []
    rows: List[Dict[str, Any]] = []
    for up in uploads:
        name = str(up.get("filename") or "")
        if "/" in name or "\\" in name or not name.lower().endswith(DATASET_EXTENSIONS):
            continue
        if str(up.get("notes") or "").strip().casefold() in _NOT_A_DATASET:
            continue
        rows.append(up)
    # db.get_uploads orders by created_at alone, and two uploads finalised in
    # the same microsecond then come back in scan order. The id breaks the tie
    # so the same conversation always reads the same file first.
    rows.sort(key=lambda u: (str(u.get("created_at") or ""), str(u.get("id") or "")), reverse=True)
    named = [u for u in rows if _names_file(text, str(u.get("filename") or ""))]
    chosen = {str(u.get("id") or "") for u in named}
    rest = [u for u in rows if str(u.get("id") or "") not in chosen]
    return [*named, *rest][:_MAX_UPLOADS]


def _profile_tables(up: Dict[str, Any], name: str, start: int) -> List[Any]:
    """The rows the dataset profile kept whole, when the file itself is gone.

    Only `full_content` profiles: a SAMPLED profile would be a silently short
    dataset, which is worse than saying the file could not be read."""
    profiles = up.get("profile")
    if isinstance(profiles, dict):
        profiles = [profiles]
    if not isinstance(profiles, list):
        return []
    out: List[Any] = []
    for prof in profiles:
        if not isinstance(prof, dict) or prof.get("kind") != "table" or not prof.get("full_content"):
            continue
        rows_json = prof.get("full_rows")
        columns = [str(c.get("name")) for c in (prof.get("columns") or []) if isinstance(c, dict) and c.get("name")]
        if not isinstance(rows_json, list) or not columns:
            continue
        rows = [[(None if r.get(c) in (None, "") else r.get(c)) for c in columns] for r in rows_json if isinstance(r, dict)]
        if not rows:
            continue
        out.append(_data_table(id=f"upload{start + len(out)}", title=str(prof.get("file") or name)[:120],
                               columns=columns, rows=rows, source_id="upload_profile"))
    return out


def _workspace_tables(workspace: str, conversation_id: str, start: int, budget: int, text: str = "") -> Tuple[List[Any], List[str]]:
    """Blocking. The CSV/XLSX datasets of this conversation as DataTables.

    The file on disk is read first (the whole file, up to the caller's row
    budget). When the TTL sweep has taken the bytes, the stored profile's
    `full_rows` stand in — those are the complete file too, for a file small
    enough to have been kept whole. An upload that never became a dataset
    (failed, rejected, still uploading) is skipped WITH A NOTE, because a
    report written around a file the person believes was read is the failure
    this whole path exists to stop."""
    from ..core.upload_paths import UploadPathError, resolve_upload_file

    tables: List[Any] = []
    notes: List[str] = []
    for up in dataset_uploads(conversation_id, text):
        if budget <= 0:
            notes.append(f"{up.get('filename')}: the row budget was already used by the earlier files, so it was not read.")
            break
        name = str(up.get("filename") or "")
        low = name.lower()
        status = str(up.get("status") or "")
        if status != "ready":
            notes.append(f"{name} could not be used: the upload is {status or 'not ready'}.")
            continue
        path = None
        for sub in ("extracted", "_original"):
            try:
                cand = resolve_upload_file(workspace, conversation_id, str(up.get("id") or ""), name, subdir=sub)
            except (UploadPathError, TypeError):
                continue
            if cand.is_file():
                path = cand
                break
        more: List[Any] = []
        n: List[str] = []
        if path is not None:
            try:
                if low.endswith(".xlsx"):
                    more, n = read_xlsx(str(path), name=name, start=start + len(tables), row_budget=budget)
                else:
                    with open(path, "rb") as fh:
                        t, n = read_csv_bytes(fh.read(), name=name, table_id=f"upload{start + len(tables)}", row_budget=budget)
                    more = [t] if t is not None else []
            except Exception as exc:  # noqa: BLE001 — one bad file is a note
                notes.append(f"{name} could not be read ({type(exc).__name__}).")
                continue
        if not more:
            more = _profile_tables(up, name, start + len(tables))
            if more:
                n = [f"{name} is no longer stored; the {sum(len(t.rows) for t in more):,} rows kept with the upload were used."]
        if not more:
            notes.append(f"{name} is no longer stored, so it could not be read.")
            continue
        tables.extend(more)
        notes.extend(n)
        budget -= sum(len(t.rows) for t in more)
    return tables, notes


def to_material_dict(g: GatheredInput) -> Dict[str, Any]:
    """The JSON-safe view persisted with a job (specs as dicts)."""
    def doc_json(entry: Dict[str, Any]) -> Dict[str, Any]:
        value = entry.get("spec_or_text")
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        return {"name": entry.get("name"), "kind": entry.get("kind"), "spec_or_text": value}

    return {
        "previous_answer_md": g.previous_answer_md,
        "previous_answer_turn_index": g.previous_answer_turn_index,
        "uploads_text": g.uploads_text,
        "upload_docs": [doc_json(e) for e in g.upload_docs],
        "upload_tables": [dict(t.__dict__) for t in g.upload_tables],
        "answer_tables": [dict(t.__dict__) for t in g.answer_tables],
        "prompt_tables": [dict(t.__dict__) for t in g.prompt_tables],
        "upload_names": list(g.upload_names),
        "notes": list(g.notes),
    }


__all__ = ["GatheredInput", "gather", "previous_answer", "tables_from_markdown", "read_xlsx", "read_csv_bytes",
           "pdf_tables_approximate", "to_material_dict", "dataset_uploads", "wants_conversation_datasets",
           "DATASET_EXTENSIONS", "PREVIOUS_ANSWER_MAX_CHARS", "MAX_UPLOAD_ROWS"]
