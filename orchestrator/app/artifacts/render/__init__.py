"""The renderers: an ArtifactSpec in, real files out, every one reopened.

    render_version(spec, formats, out_dir, title_slug=..., version=..., effort=..., transform=...)
        → RenderReport(files, paths, preview_pdf, preview_kind, preview_pages, warnings, validation, chart_files, timings, transform)

ORDER OF WORK. Charts first (PNG files into `out_dir`, referenced by bare
filename from the HTML and embedded by the DOCX); then every requested
format; then `preview.pdf` — for a document the PDF itself when one was
requested, else a PDF from the same spec; for a presentation a PDF whose
pages ARE the 16:9 slides drawn from the same plan as the .pptx (never a
screenshot of the .pptx); for a workbook the tabular PDF companion when one
was asked for, else a short summary PDF so the card has a thumbnail, with
`preview_kind='grid'` because the real preview is the sheet grid. Then
validate.py reopens everything it wrote.

ONE SPEC, SEVERAL FILES (CONTRACT-2 §1-§3). A workbook is delivered as any
of xlsx (role `primary`), one CSV per sheet (role `data`, titled by its
sheet, a per-sheet file name when the workbook has more than one sheet —
the pipeline shows it as "<title> — <sheet>"), and Word/PDF as the
TABULAR DOCUMENT (role `companion`: every sheet a section with its whole
table, landscape when the sheet says so or has more than six columns, the
header repeated on every page). Every file in the report carries its role,
format, filename, title, sizes and counts; `paths` are keyed
`role:format:sheet_slug` (§11); the pipeline mints the file ids.

PURE. No database, no SSE, no settings reads — everything arrives as
arguments, so the caller can run this in a subprocess (worker.py) with an
address-space limit and a CPU limit, or in a thread in a test. Library
exceptions become RenderError('renderer_failure', <one safe sentence>): a
person sees "The PDF could not be built", never a path or a traceback; the
worker logs the traceback on stderr for the job's diagnostic_ref.

Effort does not change what is rendered — a file is a file at every effort
(types.EffortBudget: "Effort decides depth ... never whether a file is
made"). It is accepted so the report can record it and so a future
renderer-side cost (a second QA render, say) has a home.
"""
from __future__ import annotations

import contextlib
import functools
import importlib.util
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .. import spec as S
from .. import types as T

log = logging.getLogger(__name__)


class RenderError(Exception):
    """A render that did not produce releasable files. `category` is one of
    types.FAILURE_CATEGORIES; `message` is safe to show a person."""

    def __init__(self, category: str, message: str):
        if category not in T.FAILURE_CATEGORIES:
            raise ValueError(f"unknown failure category {category!r}")
        super().__init__(message)
        self.category = category
        self.message = message


@dataclass
class RenderedFile(T.FileRef):
    """A FileRef plus the sheet a `data` file came from (CONTRACT-2 §11:
    the report's `sheet` key), so the pipeline can mint the id from
    (artifact, version, role, format, sheet) and name the title. In
    process the report holds these; on the wire (`to_json`) they are the
    FileRef-like dicts the contract names."""

    sheet: str = ""

    def to_json(self) -> dict:
        out = super().to_json()
        if self.sheet:
            out["sheet"] = self.sheet
        return out


@dataclass
class RenderReport:
    files: List[RenderedFile] = field(default_factory=list)
    #: `role:format:sheet_slug` → absolute path of the file written.
    paths: Dict[str, str] = field(default_factory=dict)
    preview_pdf: Optional[str] = None
    preview_kind: str = "pages"        # "pages" | "grid" | "none"
    preview_pages: int = 0
    warnings: List[str] = field(default_factory=list)
    validation: Dict[str, object] = field(default_factory=dict)
    #: Bare filenames of the chart PNGs written into out_dir.
    chart_files: List[str] = field(default_factory=list)
    #: Seconds spent per format ("pdf", "docx", ...) plus "charts" and
    #: "preview"; the pipeline's artifact_render_seconds{format} reads the
    #: format keys and would otherwise record the whole render for each.
    timings: Dict[str, float] = field(default_factory=dict)
    #: What code did to the rows (CONTRACT-2 §6): the caller's transform
    #: report, echoed, plus what the spec itself records (rows copied,
    #: generated, rewritten).
    transform: Dict[str, object] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "files": [f.to_json() for f in self.files],
            "paths": dict(self.paths),
            "preview_pdf": self.preview_pdf,
            "preview_kind": self.preview_kind,
            "preview_pages": self.preview_pages,
            "warnings": list(self.warnings),
            "validation": self.validation,
            "chart_files": list(self.chart_files),
            "timings": {k: round(float(v), 4) for k, v in self.timings.items()},
            "transform": dict(self.transform),
        }


def _installed(module: str) -> bool:
    """Is `module` importable — WITHOUT importing it. find_spec reads the
    package metadata only; `None` in sys.modules (Python's own way to block an
    import) counts as absent."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


@functools.lru_cache(maxsize=1)
def _weasyprint_loads() -> bool:
    """WeasyPrint is the one library whose presence a spec check cannot
    prove: its import loads pango/cairo through cffi and fails with an
    OSError when the native libraries are missing. Imported once per process
    (0.34 s cold, measured) and remembered — libraries do not appear or
    vanish at runtime, and /health asked every probe."""
    try:
        __import__("weasyprint")
        return True
    except Exception:
        return False


def capabilities() -> Dict[str, bool]:
    """Which formats this process can actually write. /health reports these
    on every probe, so the answer must be cheap and must not pull the Office
    writers into the API process the subprocess design keeps them out of:
    python-docx/pptx/openpyxl/pypdfium2 are checked by spec, WeasyPrint by
    one remembered import. A missing library is a dependency_unavailable
    failure at render time, not a surprise."""
    out = {"docx": _installed("docx"), "pptx": _installed("pptx"), "xlsx": _installed("openpyxl")}
    # A PDF we cannot reopen is a PDF we cannot validate.
    out["pdf"] = _installed("pypdfium2") and _weasyprint_loads()
    # The CSV writer is the standard library (render/csv.py): always here.
    out["csv"] = True
    return out


def _import_failure(fmt: str) -> RenderError:
    return RenderError("dependency_unavailable", f"The {fmt.upper()} writer is not installed on this server.")


def _safe(fmt: str, what: str) -> RenderError:
    return RenderError("renderer_failure", f"The {fmt.upper()} could not be built: {what}.")


#: What types.slug_for() produces; anything else in a filename stem is a
#: caller's bug, and a "/" or ".." would write outside out_dir.
_SLUG_RE = re.compile(r"[a-z0-9-]{1,60}")

#: The tabular PDF of a long sheet is not a page-count bomb: at ~28 rows a
#: landscape page (9.5 pt, measured 2026-09-12) the document ceiling of 60
#: pages is ~1,700 rows, and a 3,000-row audit is a legitimate table. The
#: companion PDF (and the preview it becomes) is allowed this many pages;
#: past it the sentence says to take the Excel or CSV file.
TABULAR_MAX_PAGES = 200
#: And python-docx writes ~400 rows a second (measured: 500×7 in 1.3 s), so
#: the tabular Word document is refused past this many rows in total.
TABULAR_MAX_ROWS = 6_000
#: Rows alone did not bound the render's cost (security review of
#: 2026-09-12, #13): a schema-valid 2,000 × 60 sheet — a third of the row
#: ceiling — ran the worker to its 2 GB limit in 77 s, and 6,000 × 60 in
#: 149 s, each attempt holding the single render slot. What the layout
#: engine pays for is CELLS and text: the 500×7 figure above is 3,500
#: cells; a 3,000-row, 9-column audit (27,000 cells) is a legitimate
#: table and fits; 6,000 × 7 (42,000) ran to 376 pages and was refused by
#: the page cap after 37 s, so the cell ceiling sits just under it, and
#: a 60-column sheet may run to 666 rows. The text ceiling keeps a table
#: of long comments inside the same budget: 40,000 cells × 50 characters.
#: Both are checked before the subprocess starts; the Excel and CSV files
#: still carry the whole table.
TABULAR_MAX_CELLS = 40_000
TABULAR_MAX_CHARS = 400_000  # measured 2026-09-12: 1.98 M chars of 50-char cells took 510 s in-process; the render window is 180 s


@contextlib.contextmanager
def _timed(report: RenderReport, key: str):
    """`with _timed(report, "pdf"):` adds the block's seconds to report.timings."""
    started = time.perf_counter()
    try:
        yield
    finally:
        report.timings[key] = report.timings.get(key, 0.0) + (time.perf_counter() - started)


def _key(role: str, fmt: str, sheet_slug: str = "") -> str:
    return f"{role}:{fmt}:{sheet_slug}"


def render_version(spec: S.ArtifactSpec, formats: Sequence[str], out_dir: str, *, title_slug: str, version: int,
                   effort: str = "fast", transform: Optional[Dict[str, object]] = None) -> RenderReport:
    """Render `spec` to every format in `formats` under `out_dir`, plus
    preview.pdf, validate them all, and describe the result. `transform`
    is the caller's report of what code did to a pasted table (rows,
    blanks, forward_filled, …); it is echoed in the report and read by the
    tabular document's methodology note."""
    from . import charts as C
    from . import html as H
    from . import theme
    from . import validate as V

    if not isinstance(title_slug, str) or not _SLUG_RE.fullmatch(title_slug):
        raise RenderError("invalid_request", "The file name stem is not valid.")
    out = Path(out_dir)
    if not out.is_dir():
        raise RenderError("storage_failure", "The working directory for this version does not exist.")
    allowed = T.FORMATS_FOR_KIND.get(spec.kind, ())
    wanted: List[str] = []
    for f in formats:
        if f in allowed and f not in wanted:
            wanted.append(f)
    if not wanted:
        raise RenderError("invalid_request", f"None of the requested formats can be made for a {spec.kind}.")

    report = RenderReport()
    report.transform = dict(transform or {})
    timed = functools.partial(_timed, report)
    report.validation["effort"] = effort if effort in T.EFFORT_BUDGETS else "fast"
    coverage = theme.font_coverage_warning(S.text_of(spec))
    if coverage:
        report.warnings.append(coverage)

    # 1. Charts (documents and presentations; workbook charts are native).
    with timed("charts"):
        for ordinal, chart in enumerate(H.spec_charts(spec), start=1):
            name = H.chart_filename(ordinal)
            try:
                C.render_chart_png(chart, out / name)
            except ImportError as exc:
                raise RenderError("dependency_unavailable", "The chart library is not installed on this server.") from exc
            except Exception as exc:
                log.exception("chart %d failed", ordinal)
                raise _safe("chart", f"chart {ordinal} ({chart.title or chart.type}) could not be drawn") from exc
            report.chart_files.append(name)

    # 2. Formats, then 3. preview.pdf. `paths` is keyed role:format:slug
    # (CONTRACT-2 §11) and `entries` remembers, per key, the role, format,
    # title and sheet the FileRef needs; the order is the requested order.
    paths: Dict[str, Path] = {}
    entries: Dict[str, Dict[str, str]] = {}
    body = spec.body
    max_pages = T.MAX_PAGES
    spec_for_validation = None

    def add(role: str, fmt: str, path: Path, *, title: str = "", sheet: str = "", sheet_slug: str = "") -> None:
        key = _key(role, fmt, sheet_slug)
        paths[key] = path
        entries[key] = {"role": role, "format": fmt, "title": title, "sheet": sheet}

    if isinstance(body, S.DocumentSpec):
        plan = H.plan_document(body)
        report.warnings.extend(plan.warnings)
        html = H.document_html(body, plan)
        if "pdf" in wanted:
            with timed("pdf"):
                pdf = _render_pdf(html, out, T.download_name(title_slug, version, "pdf"), "pdf")
                _warn_pages(plan, pdf, report)
                # The role follows the format, not the request (CONTRACT-2
                # §2: primary is the kind's NATIVE file): a PDF is the
                # document's companion even when it is the only file asked
                # for, and the pipeline derives the same role for a legacy
                # row, so ids and roles agree across a reload.
                add("companion", "pdf", pdf)
        if "docx" in wanted:
            with timed("docx"):
                add("primary", "docx", _render_docx(body, plan, out, T.download_name(title_slug, version, "docx"), report))
        with timed("preview"):
            preview = _preview_from(paths, html, out)
        report.preview_kind = "pages"
    elif isinstance(body, S.PresentationSpec):
        plan = H.plan_deck(body)
        report.warnings.extend(plan.warnings)
        html = H.deck_html(body, plan)
        if "pptx" in wanted:
            with timed("pptx"):
                add("primary", "pptx", _render_pptx(body, plan, out, T.download_name(title_slug, version, "pptx")))
        if "pdf" in wanted:
            with timed("pdf"):
                add("companion", "pdf", _render_pdf(html, out, T.download_name(title_slug, version, "pdf"), "pdf"))
        with timed("preview"):
            preview = _preview_from(paths, html, out)
        report.preview_kind = "pages"
    elif isinstance(body, S.WorkbookSpec):
        spec_for_validation = body
        report.transform.update(_spec_transform(body))
        tabular = [f for f in ("docx", "pdf") if f in wanted]
        if tabular:
            # Refused before ANY file is written — the xlsx included — so
            # a request that cannot end in a Word/PDF spends nothing of
            # the render slot (#13).
            _refuse_oversized_tabular(body)
        if "xlsx" in wanted:
            with timed("xlsx"):
                add("primary", "xlsx", _render_xlsx(body, out, T.download_name(title_slug, version, "xlsx"), report))
        if "csv" in wanted:
            with timed("csv"):
                for sheet, path, title, slug in _render_csvs(body, out, title_slug, version, report):
                    add("data", "csv", path, title=title, sheet=sheet.name, sheet_slug=slug)
        if tabular:
            max_pages = TABULAR_MAX_PAGES
            html = H.workbook_document_html(body, report.transform)
            if "docx" in tabular:
                with timed("docx"):
                    add("companion", "docx", _render_workbook_docx(body, out, T.download_name(title_slug, version, "docx"), report))
            if "pdf" in tabular:
                with timed("pdf"):
                    add("companion", "pdf", _render_pdf(html, out, T.download_name(title_slug, version, "pdf"), "pdf", max_pages=max_pages))
        with timed("preview"):
            pdf_key = _key("companion", "pdf")
            if pdf_key in paths:
                preview = out / T.PREVIEW_PDF_NAME
                preview.write_bytes(paths[pdf_key].read_bytes())
            else:
                preview = _render_pdf(H.workbook_summary_html(body), out, T.PREVIEW_PDF_NAME, "preview")
        report.preview_kind = "grid"
    else:  # pragma: no cover - the spec envelope admits only the three kinds
        raise RenderError("invalid_request", f"Unknown artifact kind {spec.kind!r}.")

    # 4. Validate by reopening. A validator's refusal is a ValidationFailed
    # (a ValueError) and becomes the validation_failure sentence here.
    try:
        validation = V.validate_all({key: str(p) for key, p in paths.items()}, str(preview), spec=spec_for_validation, max_pages=max_pages)
    except V.ValidationFailed as exc:
        raise RenderError("validation_failure", f"A rendered file failed its check: {exc}.") from exc
    except ImportError as exc:
        raise RenderError("dependency_unavailable", "A library needed to check the files is not installed on this server.") from exc
    except Exception as exc:
        log.exception("validation crashed")
        raise RenderError("validation_failure", "A rendered file could not be reopened for checking.") from exc
    validation.update(report.validation)
    validation["warnings"] = list(report.warnings)
    report.validation = validation

    for key, p in paths.items():
        facts = validation["files"][key]
        entry = entries[key]
        fmt = entry["format"]
        report.files.append(RenderedFile(
            format=fmt, filename=p.name, mime_type=T.MIME_TYPES[fmt], size=int(facts["size"]),
            sha256=str(facts["sha256"]), pages=facts.get("pages"), slides=facts.get("slides"), sheets=facts.get("sheets"),
            role=entry["role"], title=entry["title"], rows=facts.get("rows"), columns=facts.get("columns"), sheet=entry["sheet"],
        ))
    report.paths = {key: str(p) for key, p in paths.items()}
    report.preview_pdf = str(preview)
    report.preview_pages = int(validation["preview"]["pages"])
    if isinstance(body, S.PresentationSpec) and report.preview_pages != len(plan.slides):
        raise RenderError("validation_failure", "The slide preview does not have one page per slide.")
    return report


def _refuse_oversized_tabular(body: S.WorkbookSpec) -> None:
    """The tabular Word/PDF's three ceilings — rows, cells, characters of
    cell text (see TABULAR_MAX_ROWS / _CELLS / _CHARS) — as one
    invalid_request sentence that points at the Excel or CSV file."""
    total = sum(len(sh.rows) for sh in body.sheets)
    if total > TABULAR_MAX_ROWS:
        raise RenderError(
            "invalid_request",
            f"The Word/PDF version of this table would carry {total:,} rows; the ceiling is {TABULAR_MAX_ROWS:,}. "
            "Ask for the Excel or CSV file for a table this long.",
        )
    cells = sum(len(sh.rows) * len(sh.columns) for sh in body.sheets)
    if cells > TABULAR_MAX_CELLS:
        raise RenderError(
            "invalid_request",
            f"The Word/PDF version of this table would carry {cells:,} cells; the ceiling is {TABULAR_MAX_CELLS:,}. "
            "Ask for the Excel or CSV file for a table this wide.",
        )
    chars = sum(len(c) if isinstance(c, str) else 8 for sh in body.sheets for r in sh.rows for c in r if c is not None)
    if chars > TABULAR_MAX_CHARS:
        raise RenderError(
            "invalid_request",
            f"The Word/PDF version of this table would carry {chars:,} characters of cell text; the ceiling is {TABULAR_MAX_CHARS:,}. "
            "Ask for the Excel or CSV file for a table this large.",
        )


def _spec_transform(body: S.WorkbookSpec) -> Dict[str, object]:
    """What the spec itself records of code's work on the rows: the rows
    copied from a material table (and their blank cells), the rows
    generated, the columns rewritten. The caller's counts (forward fills,
    kept originals) come in through `transform` and win."""
    copied = [sh for sh in body.sheets if sh.rows_from]
    generated = [sh for sh in body.sheets if sh.generator is not None]
    out: Dict[str, object] = {}
    if copied:
        out["rows"] = sum(len(sh.rows) for sh in copied)
        out["blanks"] = sum(1 for sh in copied for r in sh.rows for c in r if c is None or (isinstance(c, str) and not c.strip()))
    if generated:
        out["generated"] = sum(len(sh.rows) for sh in generated)
    rewritten = [r.column for sh in body.sheets for r in sh.rewrite]
    if rewritten:
        out["rewrite_columns"] = list(dict.fromkeys(rewritten))
    return out


def _preview_from(paths: Dict[str, Path], html: str, out: Path) -> Path:
    """preview.pdf: a copy of the requested PDF, else a render of the same
    HTML — one spec, one plan, one look."""
    preview = out / T.PREVIEW_PDF_NAME
    pdf = next((p for key, p in paths.items() if key.split(":")[1] == "pdf"), None)
    if pdf is not None:
        preview.write_bytes(pdf.read_bytes())
        return preview
    return _render_pdf(html, out, T.PREVIEW_PDF_NAME, "preview")


def _warn_pages(plan, pdf_path: Path, report: RenderReport) -> None:
    from .pdf import pdf_page_count

    cap = plan.template.get("warn_pages")
    if cap:
        pages = pdf_page_count(pdf_path)
        if pages > cap:
            report.warnings.append(
                f"A {plan.template_id.replace('_', ' ')} is meant to fit on {cap} page{'s' if cap != 1 else ''}; this one runs to {pages}."
            )


def _render_pdf(html: str, out: Path, name: str, label: str, *, max_pages: int = T.MAX_PAGES) -> Path:
    from .pdf import render_html_pdf

    target = out / name
    try:
        render_html_pdf(html, target, out, max_pages=max_pages)
    except ImportError as exc:
        raise _import_failure("pdf") from exc
    except ValueError as exc:  # the page cap, or an empty PDF — already a sentence
        raise RenderError("validation_failure", f"The {label.upper()} was refused: {exc}.") from exc
    except Exception as exc:
        log.exception("%s render failed", label)
        raise _safe(label, "the layout engine reported an error") from exc
    return target


def _render_docx(body: S.DocumentSpec, plan, out: Path, name: str, report: RenderReport) -> Path:
    from .docx import render_docx

    target = out / name
    try:
        render_docx(body, target, out, plan=plan, warnings=report.warnings)
    except ImportError as exc:
        raise _import_failure("docx") from exc
    except Exception as exc:
        log.exception("docx render failed")
        raise _safe("docx", "the document writer reported an error") from exc
    return target


def _render_pptx(body: S.PresentationSpec, plan, out: Path, name: str) -> Path:
    from .pptx import render_pptx

    target = out / name
    try:
        render_pptx(body, target, plan=plan)
    except ImportError as exc:
        raise _import_failure("pptx") from exc
    except Exception as exc:
        log.exception("pptx render failed")
        raise _safe("pptx", "the presentation writer reported an error") from exc
    return target


def _render_workbook_docx(body: S.WorkbookSpec, out: Path, name: str, report: RenderReport) -> Path:
    from .docx import render_workbook_docx

    target = out / name
    try:
        render_workbook_docx(body, target, warnings=report.warnings, transform=report.transform)
    except ImportError as exc:
        raise _import_failure("docx") from exc
    except Exception as exc:
        log.exception("tabular docx render failed")
        raise _safe("docx", "the document writer reported an error") from exc
    return target


def _render_csvs(body: S.WorkbookSpec, out: Path, title_slug: str, version: int, report: RenderReport):
    """One CSV per sheet (CONTRACT-2 §2): the plain download name for a
    one-sheet workbook; `<slug>-v<N>-<sheet-slug>.csv` when there are
    several. Yields (sheet, path, title, sheet_slug), where `title` is the
    SHEET's name (§3: "title — sheet title for data files"): the pipeline
    composes the wire title "<artifact title> — <sheet>" from it
    (pipeline._validate_files), and a report that already carried the
    artifact title in front would be collapsed back to the bare title
    there. Two sheets whose names slug alike would be one file name; that
    is refused rather than overwritten."""
    from .csv import write_csv

    sheets = body.sheets[: T.MAX_SHEETS]
    several = len(sheets) > 1
    seen: Dict[str, str] = {}
    for sheet in sheets:
        slug = T.slug_for(sheet.name, fallback="part") if several else ""
        if several and slug in seen:
            raise RenderError("invalid_request", f"Sheets {seen[slug]!r} and {sheet.name!r} would share the file name {slug!r}; rename one.")
        seen[slug] = sheet.name
        name = T.download_name(title_slug, version, "csv", part=sheet.name if several else None)
        target = out / name
        rows = sheet.rows[: T.MAX_ROWS_PER_SHEET]
        try:
            facts = write_csv([c.name for c in sheet.columns], rows, target, column_types=[c.type for c in sheet.columns])
        except Exception as exc:
            log.exception("csv render failed")
            raise _safe("csv", "the data writer reported an error") from exc
        if facts.get("neutralised"):
            n = int(facts["neutralised"])
            report.warnings.append(
                f"Sheet {sheet.name!r}: {n} cell{'s' if n != 1 else ''} beginning with a formula character "
                f"{'were' if n != 1 else 'was'} written with a leading apostrophe in the CSV so no spreadsheet runs {'them' if n != 1 else 'it'}."
            )
        for w in facts.get("warnings") or []:
            report.warnings.append(f"Sheet {sheet.name!r}: {w}.")
        yield sheet, target, sheet.name, slug


def _render_xlsx(body: S.WorkbookSpec, out: Path, name: str, report: RenderReport) -> Path:
    from .xlsx import render_xlsx

    target = out / name
    try:
        render_xlsx(body, target, warnings=report.warnings)
    except ImportError as exc:
        raise _import_failure("xlsx") from exc
    except Exception as exc:
        log.exception("xlsx render failed")
        raise _safe("xlsx", "the workbook writer reported an error") from exc
    return target


__all__ = ["RenderError", "RenderReport", "RenderedFile", "render_version", "capabilities", "TABULAR_MAX_PAGES", "TABULAR_MAX_ROWS", "TABULAR_MAX_CELLS", "TABULAR_MAX_CHARS"]
